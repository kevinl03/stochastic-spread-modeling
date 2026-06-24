"""
Load collected stat-arb JSONL signals into PostgreSQL.

This keeps collector output files as the durable raw source while giving us a
queryable, industry-standard SQL store for analysis and dashboards.

Usage examples:
    python -m experiments.load_statarb_to_postgres \
        --run-dir data/statarb/20260613_234819 \
        --db-url postgresql://user:pass@localhost:5432/statarb

    python -m experiments.load_statarb_to_postgres \
        --run-dir data/statarb/20260613_234819 \
        --db-url postgresql://user:pass@localhost:5432/statarb \
        --signals ticker orderbook trades spread_matrix
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import psycopg

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


def _iter_jsonl(path: Path) -> Iterable[Dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _rows_for_signal(run_id: str, records: Iterable[Dict]) -> Iterable[Tuple]:
    for rec in records:
        yield (
            run_id,
            rec.get("ts"),
            rec.get("snapshot_idx"),
            rec.get("exchange"),
            rec.get("coin"),
            rec.get("market"),
            rec.get("symbol"),
            rec.get("error"),
            json.dumps(rec, default=str),
        )


def _ensure_schema(cur, schema: str, signal: str) -> None:
    table = f"{schema}.{signal}"
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id BIGSERIAL PRIMARY KEY,
            run_id TEXT NOT NULL,
            ts TIMESTAMPTZ NULL,
            snapshot_idx INTEGER NULL,
            exchange TEXT NULL,
            coin TEXT NULL,
            market TEXT NULL,
            symbol TEXT NULL,
            error TEXT NULL,
            payload JSONB NOT NULL,
            inserted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{signal}_run_snap "
        f"ON {table} (run_id, snapshot_idx)"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{signal}_ts "
        f"ON {table} (ts)"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{signal}_ex_coin_ts "
        f"ON {table} (exchange, coin, ts)"
    )


def _load_signal(
    conn: psycopg.Connection,
    run_id: str,
    run_dir: Path,
    schema: str,
    signal: str,
    batch_size: int,
) -> int:
    total = 0
    files = signal_files(run_dir, signal)
    if not files:
        return 0

    with conn.cursor() as cur:
        _ensure_schema(cur, schema, signal)
        table = f"{schema}.{signal}"
        insert_sql = (
            f"INSERT INTO {table} "
            "(run_id, ts, snapshot_idx, exchange, coin, market, symbol, error, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
        )

        batch: List[Tuple] = []
        for p in files:
            for row in _rows_for_signal(run_id, _iter_jsonl(Path(p))):
                batch.append(row)
                if len(batch) >= batch_size:
                    cur.executemany(insert_sql, batch)
                    total += len(batch)
                    batch.clear()
            conn.commit()

        if batch:
            cur.executemany(insert_sql, batch)
            total += len(batch)
            conn.commit()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load stat-arb run JSONL signals into PostgreSQL."
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to run directory, e.g. data/statarb/20260613_234819",
    )
    parser.add_argument(
        "--db-url",
        required=True,
        help="Postgres connection URL, e.g. postgresql://user:pass@localhost:5432/db",
    )
    parser.add_argument(
        "--schema",
        default="statarb_raw",
        help="Target PostgreSQL schema (default: statarb_raw)",
    )
    parser.add_argument(
        "--signals",
        nargs="+",
        default=DEFAULT_SIGNALS,
        help=f"Signals to load (default: {' '.join(DEFAULT_SIGNALS)})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Rows per DB batch insert (default: 1000)",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    run_id = run_dir.name
    if not run_dir.exists():
        raise SystemExit(f"[ERROR] Run dir not found: {run_dir}")

    print(f"[START] run_id={run_id}")
    print(f"  run_dir={run_dir}")
    print(f"  schema={args.schema}")
    print(f"  signals={args.signals}")

    with psycopg.connect(args.db_url) as conn:
        loaded_total = 0
        for signal in args.signals:
            count = _load_signal(
                conn=conn,
                run_id=run_id,
                run_dir=run_dir,
                schema=args.schema,
                signal=signal,
                batch_size=max(1, args.batch_size),
            )
            loaded_total += count
            print(f"  [{signal}] loaded {count} rows")

    print(f"[DONE] loaded_total={loaded_total}")


if __name__ == "__main__":
    main()
