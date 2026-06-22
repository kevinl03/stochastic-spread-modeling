"""
Live 1-minute OHLCV poller for LOW-RETENTION venues.

Some exchanges (Kraken, Gate.io, Phemex) do not serve 1-minute OHLCV history
more than a short window into the past, so it cannot be backfilled. This small
poller captures their candles live, going forward, without touching the main
stat-arb collector. For deep-history venues (Binance, OKX, etc.) prefer the
historical backfill (`download_historical_ohlcv.py`) instead.

Each poll fetches the most recent `--lookback` 1-minute candles per
asset/exchange and appends them as JSONL. Overlapping candles across polls are
expected; dedup by (exchange, coin, timestamp) at consolidation time.

Output (day-partitioned, crash-safe append + fsync):
    <output-dir>/<exchange>/<YYYYMMDD>.jsonl

Usage:
    python -m experiments.collect_ohlcv_live --hours 60
    python -m experiments.collect_ohlcv_live --exchanges kraken gateio phemex --interval 60
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.data import EXCHANGES, COIN_MARKETS, VOLATILE_COINS

# Venues whose 1-minute history is too short to backfill reliably.
DEFAULT_LOW_RETENTION = ["kraken", "gateio", "phemex"]
TIMEFRAME = "1m"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_exchange(name: str):
    ex_class = type(EXCHANGES[name])
    return ex_class({"timeout": 15000, "enableRateLimit": True})


class DayWriter:
    """Append JSONL per exchange, partitioned by UTC day, with fsync."""

    def __init__(self, base: Path):
        self.base = base
        self._handles: dict = {}

    def _path(self, exchange: str) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return self.base / exchange / f"{day}.jsonl"

    def write(self, exchange: str, records: list[dict]) -> None:
        if not records:
            return
        p = self._path(exchange)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())


def main() -> None:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Live 1-minute OHLCV poller for low-retention venues")
    parser.add_argument("--exchanges", nargs="+", default=DEFAULT_LOW_RETENTION,
                        help=f"Venues to poll (default: {' '.join(DEFAULT_LOW_RETENTION)})")
    parser.add_argument("--assets", nargs="+", default=list(VOLATILE_COINS),
                        help="Coins to poll (default: volatile set)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between polls (default: 60)")
    parser.add_argument("--lookback", type=int, default=3,
                        help="Recent 1-min candles to fetch per poll (default: 3, covers brief gaps)")
    parser.add_argument("--hours", type=float, default=60.0,
                        help="Total run duration in hours (default: 60)")
    parser.add_argument("--output-dir", type=str, default="data/ohlcv_live",
                        help="Output directory (default: data/ohlcv_live)")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    writer = DayWriter(out_dir)

    exchanges = {}
    for name in args.exchanges:
        if name in EXCHANGES:
            exchanges[name] = _mk_exchange(name)
        else:
            print(f"  [WARN] unknown exchange '{name}', skipping")

    # Pre-resolve symbols per (exchange, coin)
    plan = []
    for name in exchanges:
        for coin in args.assets:
            sym = COIN_MARKETS.get(coin, {}).get(name)
            if sym:
                plan.append((name, coin, sym))

    print(f"=== Live OHLCV poller (low-retention venues) ===")
    print(f"  run_id: {run_id}")
    print(f"  exchanges: {', '.join(exchanges)}")
    print(f"  assets: {len(args.assets)}  •  pairs: {len(plan)}")
    print(f"  interval: {args.interval}s  •  lookback: {args.lookback} candles  •  hours: {args.hours}")
    print(f"  output: {out_dir.resolve()}")
    print(f"  Press Ctrl+C to stop.\n", flush=True)

    end_time = time.time() + args.hours * 3600
    poll = 0
    while time.time() < end_time:
        poll += 1
        t0 = time.time()
        per_ex: dict[str, list[dict]] = {name: [] for name in exchanges}
        ok = err = 0
        for name, coin, sym in plan:
            try:
                # Always pass `since`: some venues (e.g. Phemex) reject a bare
                # limit-only kline request ("check input arguments").
                since_ms = exchanges[name].milliseconds() - (args.lookback + 1) * 60_000
                candles = exchanges[name].fetch_ohlcv(
                    sym, timeframe=TIMEFRAME, since=since_ms, limit=args.lookback + 1)
                for c in candles:
                    per_ex[name].append({
                        "type": "ohlcv",
                        "exchange": name,
                        "coin": coin,
                        "symbol": sym,
                        "timestamp": c[0],
                        "datetime": datetime.fromtimestamp(c[0] / 1000, timezone.utc).isoformat(),
                        "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5],
                        "fetched_at": _now_iso(),
                    })
                ok += 1
            except Exception as e:  # noqa: BLE001 - keep polling through per-pair errors
                err += 1
                per_ex[name].append({
                    "type": "ohlcv", "exchange": name, "coin": coin, "symbol": sym,
                    "error": str(e)[:200], "fetched_at": _now_iso(),
                })
        for name, recs in per_ex.items():
            writer.write(name, recs)
        dt = time.time() - t0
        print(f"  [poll {poll}] ok={ok} err={err}  ({dt:.1f}s)", flush=True)
        sleep = max(0, args.interval - dt)
        time.sleep(sleep)

    print(f"[DONE] polls={poll}")


if __name__ == "__main__":
    main()
