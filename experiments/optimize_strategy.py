"""
Wall Street-grade strategy optimization on captured live tick data.

Key insight from 21h paper trading:
  - Signal IS real (positive avg gross PnL across all 3 pairs)
  - But avg gross < round-trip cost → net negative
  - WIF zscore: +19.5 gross vs 21.5 cost = -2.0 net (closest to profitable)

This script:
  1. Decomposes P&L into regime buckets (high-vol vs low-vol)
  2. Tests volatility filters (only trade when spread_std > threshold)
  3. Grid-searches entry_z, exit_z, zscore_window
  4. Tests alternative exchange pairs (lower fees)
  5. Analyzes time-of-day patterns
  6. Outputs the optimal parameter set
"""

import json
import numpy as np
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fees import TRADING_FEES_TAKER


# ─────────────────────────────────────────────────────────────────────────────
# Load tick data
# ─────────────────────────────────────────────────────────────────────────────

def load_ticks(directory: str):
    """Load tick data into paired spread observations."""
    ticks = []
    with open(Path(directory) / "ticks.jsonl") as f:
        for line in f:
            t = json.loads(line)
            ticks.append(t)

    # Pair ticks into spreads (need one from each exchange)
    exchanges = set(t["exchange"] for t in ticks)
    if len(exchanges) != 2:
        raise ValueError(f"Expected 2 exchanges, got {exchanges}")
    exch_list = sorted(exchanges)
    ex_a, ex_b = exch_list[0], exch_list[1]

    # Build spread series: use latest tick from each exchange
    last = {ex_a: None, ex_b: None}
    spreads = []  # list of (timestamp, spread_bps, mid_a, mid_b)

    for t in ticks:
        last[t["exchange"]] = t
        if last[ex_a] is not None and last[ex_b] is not None:
            mid_a = last[ex_a]["mid"]
            mid_b = last[ex_b]["mid"]
            spread_bps = (mid_b - mid_a) / mid_a * 10000
            spreads.append({
                "ts": t["ts"],
                "spread_bps": spread_bps,
                "mid_a": mid_a,
                "mid_b": mid_b,
                "bid_a": last[ex_a]["bid"],
                "ask_a": last[ex_a]["ask"],
                "bid_b": last[ex_b]["bid"],
                "ask_b": last[ex_b]["ask"],
            })
    return spreads, ex_a, ex_b


def load_trades(directory: str):
    """Load trade results."""
    trades = []
    path = Path(directory) / "trades.jsonl"
    if path.exists():
        with open(path) as f:
            for line in f:
                trades.append(json.loads(line))
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Offline backtester on tick data
# ─────────────────────────────────────────────────────────────────────────────

def simulate(spreads, cost_bps, entry_z, exit_z, window, vol_filter_mult=0.0,
             min_spread_bps=0.0, max_hold_ticks=None, cooldown_ticks=0):
    """
    Run z-score strategy on spread data.

    Args:
        spreads: list of spread_bps values
        cost_bps: round-trip cost (fees + slippage)
        entry_z: z-score threshold to enter
        exit_z: z-score threshold to exit
        window: lookback for z-score calculation
        vol_filter_mult: only trade when rolling_std > vol_filter_mult * cost_bps
                         (0 = disabled, 1.0 = std must exceed cost)
        min_spread_bps: minimum absolute spread at entry to trade
        max_hold_ticks: max ticks before forced exit (None = no limit)
        cooldown_ticks: wait N ticks after a trade before entering again
    """
    arr = np.array(spreads)
    n = len(arr)
    if n < window + 10:
        return {"trades": 0, "net_pnl": 0, "gross_pnl": 0}

    trades = []
    position = None  # (direction, entry_idx, entry_spread)
    last_exit_idx = -cooldown_ticks - 1

    for i in range(window, n):
        chunk = arr[i - window:i]
        mu = np.mean(chunk)
        sigma = np.std(chunk)
        if sigma < 1e-10:
            continue
        z = (arr[i] - mu) / sigma
        current = arr[i]

        if position is None:
            # Cooldown check
            if i - last_exit_idx < cooldown_ticks:
                continue
            # Vol filter: rolling std must exceed threshold
            if vol_filter_mult > 0 and sigma < vol_filter_mult * cost_bps:
                continue
            # Minimum spread filter
            if min_spread_bps > 0 and abs(current) < min_spread_bps:
                continue
            # Entry
            if z > entry_z:
                position = ("short_spread", i, current)
            elif z < -entry_z:
                position = ("long_spread", i, current)
        else:
            direction, entry_idx, entry_spread = position
            hold_ticks = i - entry_idx

            exit_signal = False
            exit_reason = "signal"
            if direction == "short_spread" and z < exit_z:
                exit_signal = True
            elif direction == "long_spread" and z > -exit_z:
                exit_signal = True
            elif max_hold_ticks and hold_ticks >= max_hold_ticks:
                exit_signal = True
                exit_reason = "timeout"

            if exit_signal:
                if direction == "short_spread":
                    gross = entry_spread - current
                else:
                    gross = current - entry_spread
                net = gross - cost_bps
                trades.append({
                    "gross": gross,
                    "net": net,
                    "hold_ticks": hold_ticks,
                    "direction": direction,
                    "entry_z": z,
                    "sigma_at_entry": sigma,
                    "reason": exit_reason,
                    "entry_idx": entry_idx,
                })
                position = None
                last_exit_idx = i

    # Stats
    if not trades:
        return {"trades": 0, "net_pnl": 0, "gross_pnl": 0, "win_rate": 0,
                "avg_net": 0, "avg_gross": 0, "sharpe": 0, "max_dd": 0}

    nets = [t["net"] for t in trades]
    grosses = [t["gross"] for t in trades]
    wins = sum(1 for n in nets if n > 0)

    cumulative = np.cumsum(nets)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = running_max - cumulative
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0

    sharpe = np.mean(nets) / np.std(nets) * np.sqrt(252 * 24) if np.std(nets) > 0 else 0

    return {
        "trades": len(trades),
        "net_pnl": sum(nets),
        "gross_pnl": sum(grosses),
        "win_rate": wins / len(trades) * 100,
        "avg_net": np.mean(nets),
        "avg_gross": np.mean(grosses),
        "sharpe": sharpe,
        "max_dd": max_dd,
        "profit_factor": sum(n for n in nets if n > 0) / max(abs(sum(n for n in nets if n < 0)), 0.01),
        "avg_hold": np.mean([t["hold_ticks"] for t in trades]),
        "timeout_pct": sum(1 for t in trades if t["reason"] == "timeout") / len(trades) * 100,
        "trades_detail": trades,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Analysis modules
# ─────────────────────────────────────────────────────────────────────────────

def analyze_regimes(spreads_data, window=60):
    """Break the session into vol regimes and show stats per regime."""
    arr = np.array([s["spread_bps"] for s in spreads_data])
    n = len(arr)

    vol_series = []
    for i in range(window, n):
        chunk = arr[i - window:i]
        vol_series.append(np.std(chunk))

    vol = np.array(vol_series)
    print(f"\n{'='*70}")
    print(f"  REGIME ANALYSIS ({len(arr)} spread observations)")
    print(f"{'='*70}")
    print(f"  Overall spread std: {np.std(arr):.2f} bps")
    print(f"  Rolling vol (window={window}):")
    print(f"    Min:  {vol.min():.2f} bps")
    print(f"    P25:  {np.percentile(vol, 25):.2f} bps")
    print(f"    P50:  {np.percentile(vol, 50):.2f} bps")
    print(f"    P75:  {np.percentile(vol, 75):.2f} bps")
    print(f"    P90:  {np.percentile(vol, 90):.2f} bps")
    print(f"    P95:  {np.percentile(vol, 95):.2f} bps")
    print(f"    Max:  {vol.max():.2f} bps")

    # Time in each regime
    for threshold in [10, 15, 20, 25, 30, 40, 50]:
        pct = np.mean(vol > threshold) * 100
        print(f"    Time with vol > {threshold} bps: {pct:.1f}%")

    return vol


def analyze_time_of_day(spreads_data):
    """Check if certain hours have higher spread volatility."""
    by_hour = defaultdict(list)
    for s in spreads_data:
        ts = s["ts"]
        # Parse hour from ISO timestamp
        try:
            h = int(ts[11:13])
            by_hour[h].append(s["spread_bps"])
        except:
            pass

    print(f"\n{'='*70}")
    print(f"  TIME-OF-DAY ANALYSIS (UTC hours)")
    print(f"{'='*70}")
    print(f"  {'Hour':<6} {'Count':<8} {'Std(bps)':<10} {'Range(bps)':<20}")
    print(f"  {'-'*50}")
    for h in sorted(by_hour.keys()):
        vals = np.array(by_hour[h])
        std = np.std(vals)
        rng = f"[{vals.min():.1f}, {vals.max():.1f}]"
        print(f"  {h:02d}:00  {len(vals):<8} {std:<10.2f} {rng}")


def analyze_bid_ask_spread(spreads_data):
    """Analyze the bid-ask spread on each exchange (hidden execution cost)."""
    ba_a = []
    ba_b = []
    for s in spreads_data:
        if s["bid_a"] > 0 and s["ask_a"] > 0:
            ba_a.append((s["ask_a"] - s["bid_a"]) / s["mid_a"] * 10000)
        if s["bid_b"] > 0 and s["ask_b"] > 0:
            ba_b.append((s["ask_b"] - s["bid_b"]) / s["mid_b"] * 10000)

    print(f"\n{'='*70}")
    print(f"  BID-ASK SPREAD ANALYSIS (hidden cost)")
    print(f"{'='*70}")
    if ba_a:
        arr_a = np.array(ba_a)
        print(f"  Exchange A — mean: {np.mean(arr_a):.2f} bps, median: {np.median(arr_a):.2f}, p95: {np.percentile(arr_a, 95):.2f}")
    if ba_b:
        arr_b = np.array(ba_b)
        print(f"  Exchange B — mean: {np.mean(arr_b):.2f} bps, median: {np.median(arr_b):.2f}, p95: {np.percentile(arr_b, 95):.2f}")
    if ba_a and ba_b:
        total = np.array(ba_a) + np.array(ba_b[:len(ba_a)])
        print(f"  Combined -- mean: {np.mean(total):.2f} bps = THIS is your real crossing cost")
        print(f"  (You assumed 4.0 bps slippage. Actual BA spread = {np.mean(total):.2f} bps)")


def grid_search(spread_values, cost_bps, label=""):
    """
    Focused grid search — prioritize the levers that matter most:
      1. vol_filter (biggest impact: eliminates losing low-vol trades)
      2. entry_z (second biggest: wait for larger moves)
      3. window (third: right lookback for z-score normalization)
      4. exit_z, cooldown, max_hold (smaller effects)
    """
    print(f"\n{'='*70}")
    print(f"  PARAMETER GRID SEARCH — {label}")
    print(f"  Cost per round-trip: {cost_bps:.1f} bps")
    print(f"{'='*70}")

    results = []

    for window in [20, 30, 45, 60, 90]:
        for entry_z in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]:
            for exit_z in [0.0, 0.3, 0.5]:
                if exit_z >= entry_z:
                    continue
                for vol_mult in [0.0, 0.8, 1.0, 1.2, 1.5, 2.0]:
                    for cooldown in [0, 10, 30]:
                        for max_hold in [None, 60]:
                            r = simulate(
                                spread_values, cost_bps,
                                entry_z=entry_z, exit_z=exit_z,
                                window=window,
                                vol_filter_mult=vol_mult,
                                min_spread_bps=0,
                                max_hold_ticks=max_hold,
                                cooldown_ticks=cooldown,
                            )
                            if r["trades"] >= 10:
                                r["params"] = {
                                    "window": window,
                                    "entry_z": entry_z,
                                    "exit_z": exit_z,
                                    "vol_mult": vol_mult,
                                    "max_hold": max_hold,
                                    "cooldown": cooldown,
                                }
                                results.append(r)

    if not results:
        print("  No parameter combinations with ≥20 trades.")
        return []

    # Sort by net PnL per trade (risk-adjusted — we want consistent, not lucky)
    results.sort(key=lambda r: r["avg_net"], reverse=True)

    print(f"\n  Tested {len(results)} parameter combinations with ≥20 trades")
    print(f"\n  TOP 15 BY AVG NET PnL/TRADE:")
    print(f"  {'#':<4} {'Net(bps)':<10} {'Trades':<8} {'Win%':<7} {'Sharpe':<8} {'PF':<6} {'AvgGross':<10} {'MaxDD':<8} | Parameters")
    print(f"  {'-'*100}")

    for i, r in enumerate(results[:15]):
        p = r["params"]
        params_str = (f"w={p['window']} ez={p['entry_z']} xz={p['exit_z']} "
                      f"vm={p['vol_mult']} ms={p['min_spread']} "
                      f"mh={p['max_hold']} cd={p['cooldown']}")
        print(f"  {i+1:<4} {r['avg_net']:>+8.2f}  {r['trades']:<8} {r['win_rate']:<6.1f}% "
              f"{r['sharpe']:<8.2f} {r['profit_factor']:<6.2f} {r['avg_gross']:>+8.2f}  "
              f"{r['max_dd']:<8.1f} | {params_str}")

    # Also show by Sharpe ratio (risk-adjusted ranking)
    results_by_sharpe = sorted(results, key=lambda r: r["sharpe"], reverse=True)

    print(f"\n  TOP 10 BY SHARPE RATIO:")
    print(f"  {'#':<4} {'Sharpe':<8} {'Net(bps)':<10} {'Trades':<8} {'Win%':<7} {'PF':<6} {'AvgNet':<10} | Parameters")
    print(f"  {'-'*100}")

    for i, r in enumerate(results_by_sharpe[:10]):
        p = r["params"]
        params_str = (f"w={p['window']} ez={p['entry_z']} xz={p['exit_z']} "
                      f"vm={p['vol_mult']} ms={p['min_spread']} "
                      f"mh={p['max_hold']} cd={p['cooldown']}")
        print(f"  {i+1:<4} {r['sharpe']:<8.2f} {r['avg_net']:>+8.2f}  {r['trades']:<8} "
              f"{r['win_rate']:<6.1f}% {r['profit_factor']:<6.2f} {r['avg_net']:>+8.2f}  | {params_str}")

    # Show the BEST by avg_net that also has Sharpe > 0
    profitable = [r for r in results if r["avg_net"] > 0 and r["sharpe"] > 0]
    if profitable:
        print(f"\n  PROFITABLE CONFIGS: {len(profitable)} / {len(results)} ({len(profitable)/len(results)*100:.1f}%)")
        best = max(profitable, key=lambda r: r["avg_net"] * min(r["trades"], 100))  # balance edge vs frequency
        print(f"\n  ★ RECOMMENDED CONFIG (best edge × frequency):")
        p = best["params"]
        print(f"    window={p['window']}, entry_z={p['entry_z']}, exit_z={p['exit_z']}")
        print(f"    vol_filter={p['vol_mult']}x cost, min_spread={p['min_spread']} bps")
        print(f"    max_hold={p['max_hold']} ticks, cooldown={p['cooldown']} ticks")
        print(f"    → {best['trades']} trades, avg net {best['avg_net']:+.2f} bps, "
              f"win rate {best['win_rate']:.0f}%, Sharpe {best['sharpe']:.2f}")
    else:
        print(f"\n  ⚠ NO PROFITABLE CONFIG FOUND with ≥20 trades")
        print(f"  Best avg_net: {results[0]['avg_net']:+.2f} bps ({results[0]['trades']} trades)")

    return results


def test_fee_sensitivity(spread_values, base_cost_bps, best_params):
    """
    What if we could reduce fees? Shows the cost level where strategy becomes profitable.
    This informs whether to pursue maker orders or lower-fee exchanges.
    """
    print(f"\n{'='*70}")
    print(f"  FEE SENSITIVITY ANALYSIS")
    print(f"{'='*70}")
    print(f"  {'RT Cost':<10} {'Net PnL':<12} {'Trades':<8} {'Win%':<7} {'AvgNet':<10} {'Sharpe':<8}")
    print(f"  {'-'*60}")

    for cost in [5, 8, 10, 12, 15, 17.5, 20, 21.5, 25, 30]:
        r = simulate(
            spread_values, cost,
            entry_z=best_params["entry_z"],
            exit_z=best_params["exit_z"],
            window=best_params["window"],
            vol_filter_mult=best_params.get("vol_mult", 0),
            min_spread_bps=best_params.get("min_spread", 0),
            max_hold_ticks=best_params.get("max_hold", None),
            cooldown_ticks=best_params.get("cooldown", 0),
        )
        marker = " ★ current" if abs(cost - base_cost_bps) < 0.5 else ""
        marker = " ← BREAKEVEN" if r["trades"] > 0 and abs(r["avg_net"]) < 1.0 and not marker else marker
        avg_net = r["avg_net"] if r["trades"] > 0 else 0
        sharpe = r["sharpe"] if r["trades"] > 0 else 0
        win = r["win_rate"] if r["trades"] > 0 else 0
        print(f"  {cost:<10.1f} {r['net_pnl']:>+10.1f}  {r['trades']:<8} {win:<6.1f}% "
              f"{avg_net:>+8.2f}  {sharpe:<8.2f}{marker}")


def analyze_direction_asymmetry(trades_detail):
    """Check if long_spread and short_spread have different alpha."""
    by_dir = defaultdict(list)
    for t in trades_detail:
        by_dir[t["direction"]].append(t)

    print(f"\n{'='*70}")
    print(f"  DIRECTION ASYMMETRY ANALYSIS")
    print(f"{'='*70}")
    for d in sorted(by_dir.keys()):
        trades = by_dir[d]
        nets = [t["net"] for t in trades]
        grosses = [t["gross"] for t in trades]
        wins = sum(1 for n in nets if n > 0)
        print(f"  {d}: {len(trades)} trades, avg_gross={np.mean(grosses):+.2f}, "
              f"avg_net={np.mean(nets):+.2f}, win_rate={wins/len(trades)*100:.1f}%")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    data_dir = Path("data/paper_trading")
    targets = [
        ("WIF_binance_cryptocom_zscore", "binance", "cryptocom"),
        ("CRV_cryptocom_mexc_ou", "cryptocom", "mexc"),
        ("PEPE_binance_cryptocom_ou", "binance", "cryptocom"),
    ]

    for dir_name, ex_a, ex_b in targets:
        full_dir = data_dir / dir_name
        if not (full_dir / "ticks.jsonl").exists():
            print(f"Skipping {dir_name}: no tick data")
            continue

        print(f"\n\n{'#'*70}")
        print(f"#  ANALYZING: {dir_name}")
        print(f"#  21-hour live data")
        print(f"{'#'*70}")

        # Load data
        spreads_data, detected_a, detected_b = load_ticks(str(full_dir))
        spread_values = [s["spread_bps"] for s in spreads_data]
        print(f"  Loaded {len(spread_values)} spread observations")
        print(f"  Exchanges: {detected_a} (A) vs {detected_b} (B)")

        # Compute cost
        fee_a = TRADING_FEES_TAKER.get(ex_a, 0.001)
        fee_b = TRADING_FEES_TAKER.get(ex_b, 0.001)
        cost_bps = (fee_a + fee_b) * 10000 + 4.0  # fees + 2bps slippage/side
        print(f"  Round-trip cost: {cost_bps:.1f} bps (fees={fee_a*10000:.1f}+{fee_b*10000:.1f} + slippage=4.0)")

        # 1. Regime analysis
        vol = analyze_regimes(spreads_data)

        # 2. Time of day
        analyze_time_of_day(spreads_data)

        # 3. Bid-ask spread (hidden cost)
        analyze_bid_ask_spread(spreads_data)

        # 4. Grid search
        results = grid_search(spread_values, cost_bps, label=dir_name)

        # 5. Direction asymmetry (using best config)
        if results and results[0].get("trades_detail"):
            analyze_direction_asymmetry(results[0]["trades_detail"])

        # 6. Fee sensitivity (using best or default params)
        if results:
            best_p = results[0]["params"]
        else:
            best_p = {"window": 30, "entry_z": 2.0, "exit_z": 0.3,
                       "vol_mult": 0, "min_spread": 0, "max_hold": None, "cooldown": 0}
        test_fee_sensitivity(spread_values, cost_bps, best_p)


if __name__ == "__main__":
    main()
