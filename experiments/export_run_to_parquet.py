"""
Convert a collected stat-arb run (day-partitioned JSONL) into Parquet.

Why Parquet: columnar + compressed (zstd), so multi-GB JSONL shrinks a lot;
queryable directly as a database via DuckDB (`SELECT ... FROM 'ticker.parquet'`);
and it is the native, free, multi-GB-friendly format for Hugging Face datasets
(the dataset viewer reads Parquet automatically).

The output uses ONE unified schema per signal so the same data can be loaded
into Parquet / SQLite / Postgres / Hugging Face interchangeably:

    run_id, ts, snapshot_idx, exchange, coin, market, symbol, error, payload

`payload` keeps the full original JSON record (lossless), while the promoted
columns make the common filters fast.

Streaming line-by-line with a bounded row-group buffer keeps memory flat even
on the ~700 MB trades files.

Usage:
    python -m experiments.export_run_to_parquet \
        --run-dir data/statarb/20260613_234819 \
        --out-dir data/exports/statarb_2026-06-13_run1 \
        --max-snapshot 3909
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from scripts.data import signal_files


DEFAULT_SIGNALS = [
    "ticker",
    "orderbook",
    "trades",
    "spread_matrix",
    "funding_rate",
    "open_interest",
    "withdrawal_status",
    "exchange_status",
]

SCHEMA = pa.schema([
    ("run_id", pa.string()),
    ("ts", pa.string()),
    ("snapshot_idx", pa.int64()),
    ("exchange", pa.string()),
    ("coin", pa.string()),
    ("market", pa.string()),
    ("symbol", pa.string()),
    ("error", pa.string()),
    ("payload", pa.string()),
])


def _iter_jsonl(path: Path) -> Iterable[Dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _batch_to_table(rows: List[Dict], run_id: str) -> pa.Table:
    cols: Dict[str, list] = {name: [] for name in SCHEMA.names}
    for rec in rows:
        cols["run_id"].append(run_id)
        cols["ts"].append(rec.get("ts"))
        si = rec.get("snapshot_idx")
        cols["snapshot_idx"].append(int(si) if isinstance(si, (int, float)) else None)
        cols["exchange"].append(rec.get("exchange"))
        cols["coin"].append(rec.get("coin"))
        cols["market"].append(rec.get("market"))
        cols["symbol"].append(rec.get("symbol"))
        cols["error"].append(rec.get("error"))
        cols["payload"].append(json.dumps(rec, default=str, separators=(",", ":")))
    return pa.table(cols, schema=SCHEMA)


def _export_signal(
    run_id: str,
    run_dir: Path,
    out_dir: Path,
    signal: str,
    max_snapshot: Optional[int],
    min_snapshot: Optional[int],
    batch_rows: int,
    compression: str,
) -> Dict[str, int]:
    files = signal_files(run_dir, signal)
    if not files:
        return {"rows": 0, "kept": 0, "bytes": 0}

    out_path = out_dir / f"{signal}.parquet"
    writer: Optional[pq.ParquetWriter] = None
    buf: List[Dict] = []
    total = kept = 0

    def _flush():
        nonlocal writer, buf
        if not buf:
            return
        table = _batch_to_table(buf, run_id)
        if writer is None:
            writer = pq.ParquetWriter(out_path, SCHEMA, compression=compression)
        writer.write_table(table)
        buf = []

    for p in files:
        for rec in _iter_jsonl(Path(p)):
            total += 1
            si = rec.get("snapshot_idx")
            if max_snapshot is not None and isinstance(si, (int, float)) and si > max_snapshot:
                continue
            if min_snapshot is not None and isinstance(si, (int, float)) and si < min_snapshot:
                continue
            kept += 1
            buf.append(rec)
            if len(buf) >= batch_rows:
                _flush()
    _flush()
    if writer is not None:
        writer.close()

    size = out_path.stat().st_size if out_path.exists() else 0
    return {"rows": total, "kept": kept, "bytes": size}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a stat-arb run's JSONL signals to Parquet."
    )
    parser.add_argument("--run-dir", required=True,
                        help="Run directory, e.g. data/statarb/20260613_234819")
    parser.add_argument("--out-dir", required=True,
                        help="Output directory for Parquet files")
    parser.add_argument("--signals", nargs="+", default=DEFAULT_SIGNALS,
                        help=f"Signals to export (default: {' '.join(DEFAULT_SIGNALS)})")
    parser.add_argument("--max-snapshot", type=int, default=None,
                        help="Keep only records with snapshot_idx <= this "
                             "(use to isolate the first run before an outage).")
    parser.add_argument("--min-snapshot", type=int, default=None,
                        help="Keep only records with snapshot_idx >= this "
                             "(combine with --max-snapshot to carve out a batch).")
    parser.add_argument("--batch-rows", type=int, default=50000,
                        help="Rows buffered per Parquet row group (default: 50000)")
    parser.add_argument("--compression", default="zstd",
                        choices=["zstd", "snappy", "gzip", "none"],
                        help="Parquet compression codec (default: zstd)")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        raise SystemExit(f"[ERROR] Run dir not found: {run_dir}")
    run_id = run_dir.name
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    compression = "none" if args.compression == "none" else args.compression

    print(f"[START] run_id={run_id}")
    print(f"  run_dir={run_dir}")
    print(f"  out_dir={out_dir}")
    print(f"  snapshot_range=[{args.min_snapshot}, {args.max_snapshot}]  compression={compression}")

    grand_kept = grand_bytes = 0
    for signal in args.signals:
        stats = _export_signal(
            run_id=run_id,
            run_dir=run_dir,
            out_dir=out_dir,
            signal=signal,
            max_snapshot=args.max_snapshot,
            min_snapshot=args.min_snapshot,
            batch_rows=max(1, args.batch_rows),
            compression=compression,
        )
        grand_kept += stats["kept"]
        grand_bytes += stats["bytes"]
        mb = stats["bytes"] / 1e6
        print(f"  [{signal:<18}] kept {stats['kept']:>8} / {stats['rows']:>8} rows"
              f"  ->  {mb:8.1f} MB")

    print(f"[DONE] kept_total={grand_kept}  parquet_total={grand_bytes/1e6:.1f} MB")


if __name__ == "__main__":
    main()
