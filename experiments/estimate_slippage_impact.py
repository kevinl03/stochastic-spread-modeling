#!/usr/bin/env python3
"""
Estimate the impact of realistic L2 slippage on backtest results.

Reads existing backtest_results.json and re-computes net PnL under several
slippage assumptions: 0 bps (current), 1/2/3/5 bps per-side (round-trip = 2x),
and the paper-trader default of 2 bps/side.

Shows which pairs remain profitable and how Sharpe/PnL degrade as a function
of market impact.

Usage:
    python experiments/estimate_slippage_impact.py
    python experiments/estimate_slippage_impact.py --results data/historical/backtest_results.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def load_results(path: str) -> list:
    with open(path) as f:
        return json.load(f)


def estimate_impact(results: list, slippage_levels: list[float]) -> None:
    """Re-compute net PnL for each pair under different slippage assumptions."""

    # Filter to pairs with trades
    active = [r for r in results if r["n_trades"] > 0]
    if not active:
        print("No pairs with trades found.")
        return

    print(f"\n{'='*110}")
    print(f"  SLIPPAGE IMPACT ANALYSIS — {len(active)} pair-models with trades")
    print(f"{'='*110}\n")

    # Header
    slip_cols = "  ".join(f"{'@' + str(s) + 'bps':>10}" for s in slippage_levels)
    print(f"  {'Asset':<6} {'Pair':<22} {'Model':<7} {'Trades':>6} "
          f"{'Gross':>8} {'Fees':>8}  {slip_cols}")
    print(f"  {'-'*6} {'-'*22} {'-'*7} {'-'*6} {'-'*8} {'-'*8}  "
          + "  ".join("-" * 10 for _ in slippage_levels))

    # Track aggregate stats per slippage level
    agg = {s: {"profitable": 0, "total_net": 0.0, "total_trades": 0} for s in slippage_levels}

    for r in active:
        n = r["n_trades"]
        gross = r["total_gross_bps"]
        fees = r["total_fees_bps"]
        fee_per = fees / n if n > 0 else 0

        nets = []
        for slip_per_side in slippage_levels:
            # Round-trip slippage = 2 × per-side × n_trades
            # But more precisely: each trade has entry + exit on both legs,
            # so total slippage per trade = 4 × slip_per_side
            # However, the paper_trader applies it as a lump sum per trade,
            # so we model it as: total_slippage_per_trade = 2 × slip_per_side
            # (entry side A + entry side B = 2 sides, then same on exit)
            # Actually in our _close_position: total_slippage = entry_a + entry_b + exit_a + exit_b
            # = 4 × slip_per_side. But in the old code it was just self.slippage_bps (flat).
            # Let's be consistent with how the backtester works:
            # backtester fee_bps = fee_a + fee_b per trade (both legs combined).
            # So slippage should also be per-trade total.
            # paper_trader old code: net = gross - fee - slippage_bps (where slippage_bps was 2.0 flat)
            # That 2.0 was meant as 1 bps per side × 2 sides. So for fair comparison:
            # slippage_per_trade = 2 × slip_per_side  (both legs)
            slippage_per_trade = 2 * slip_per_side
            total_slip = slippage_per_trade * n
            net = gross - fees - total_slip
            nets.append(net)
            agg[slip_per_side]["total_net"] += net
            agg[slip_per_side]["total_trades"] += n
            if net > 0:
                agg[slip_per_side]["profitable"] += 1

        net_strs = "  ".join(f"{net:>+10.1f}" for net in nets)
        print(f"  {r['asset']:<6} {r['ex1']}-{r['ex2']:<17} {r['model']:<7} "
              f"{n:>6} {gross:>8.1f} {fees:>8.1f}  {net_strs}")

    # Summary table
    print(f"\n{'='*80}")
    print(f"  SUMMARY — IMPACT OF SLIPPAGE ON PORTFOLIO\n")
    print(f"  {'Slippage/side':>14} {'Profitable':>11} {'Total Net':>12} "
          f"{'Avg Net/Trade':>14} {'vs 0bps':>10}")

    base_net = agg[0.0]["total_net"] if 0.0 in agg else agg[slippage_levels[0]]["total_net"]

    for s in slippage_levels:
        a = agg[s]
        avg = a["total_net"] / a["total_trades"] if a["total_trades"] > 0 else 0
        delta = a["total_net"] - base_net
        delta_str = f"{delta:+.1f}" if s != slippage_levels[0] else "—"
        print(f"  {s:>12.1f} bps {a['profitable']:>6}/{len(active)}   "
              f"{a['total_net']:>+12.1f} {avg:>+14.2f} {delta_str:>10}")

    # Breakeven analysis: at what slippage does each pair flip from profitable to unprofitable?
    print(f"\n{'='*80}")
    print(f"  BREAKEVEN SLIPPAGE PER PAIR\n")
    print(f"  {'Asset':<6} {'Pair':<22} {'Model':<7} {'Trades':>6} "
          f"{'Gross/Trade':>11} {'Fee/Trade':>10} {'Breakeven':>10}")
    print(f"  {'-'*6} {'-'*22} {'-'*7} {'-'*6} {'-'*11} {'-'*10} {'-'*10}")

    breakevens = []
    for r in active:
        n = r["n_trades"]
        gross = r["total_gross_bps"]
        fees = r["total_fees_bps"]
        gross_per = gross / n
        fee_per = fees / n
        # Net per trade = gross_per - fee_per - 2*slip_per_side = 0
        # => slip_per_side = (gross_per - fee_per) / 2
        margin_per_trade = gross_per - fee_per
        be_slip = margin_per_trade / 2  # per-side breakeven slippage

        breakevens.append(be_slip)
        print(f"  {r['asset']:<6} {r['ex1']}-{r['ex2']:<17} {r['model']:<7} "
              f"{n:>6} {gross_per:>+11.2f} {fee_per:>10.2f} "
              f"{be_slip:>9.1f} bps")

    be_arr = np.array([b for b in breakevens if b > 0])
    if len(be_arr) > 0:
        print(f"\n  Breakeven slippage distribution (per-side):")
        print(f"    Min:    {be_arr.min():>6.1f} bps")
        print(f"    Median: {np.median(be_arr):>6.1f} bps")
        print(f"    Mean:   {be_arr.mean():>6.1f} bps")
        print(f"    Max:    {be_arr.max():>6.1f} bps")
        print(f"\n  Interpretation:")
        print(f"    If typical L2 slippage is < {np.median(be_arr):.0f} bps/side, "
              f"majority of pairs remain profitable.")
        print(f"    The paper-trader 2 bps/side assumption eliminates pairs "
              f"with breakeven < 2 bps ({sum(1 for b in breakevens if b < 2)}/{len(breakevens)} pairs).")
        print(f"    For $1000 trades on BTC/ETH, typical L2 slippage is 0.1-0.5 bps.")
        print(f"    For $1000 trades on alts (WIF, CRV), expect 1-5 bps depending on book depth.")


def main():
    parser = argparse.ArgumentParser(description="Estimate slippage impact on backtest results")
    parser.add_argument("--results", default="data/historical/backtest_results.json",
                        help="Path to backtest_results.json")
    parser.add_argument("--slippage-levels", type=str, default="0,0.5,1,2,3,5",
                        help="Comma-separated slippage levels per side in bps")
    args = parser.parse_args()

    results = load_results(args.results)
    levels = [float(x) for x in args.slippage_levels.split(",")]

    estimate_impact(results, levels)


if __name__ == "__main__":
    main()
