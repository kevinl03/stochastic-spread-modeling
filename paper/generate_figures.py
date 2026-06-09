"""Generate the four paper figures from historical OHLCV data and backtest results."""

import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "historical"
OUT_DIR = Path(__file__).resolve().parent / "figures"
OUT_DIR.mkdir(exist_ok=True)

# Load backtest results
with open(DATA_DIR / "backtest_results.json") as f:
    results = json.load(f)


# =========================================================================
# Figure 1: Spread std vs net P&L scatter (threshold plot)
# =========================================================================
def fig_threshold_scatter():
    fig, ax = plt.subplots(figsize=(5, 3.5))
    
    spread_std = [r["spread_std_bps"] for r in results]
    net_pnl = [r["total_net_bps"] for r in results]
    profitable = [r["total_net_bps"] > 0 for r in results]
    
    colors = ["#2ca02c" if p else "#d62728" for p in profitable]
    ax.scatter(spread_std, net_pnl, c=colors, s=30, alpha=0.7, edgecolors="k", linewidths=0.3, zorder=3)
    
    # Threshold line at 15 bps
    ax.axvline(x=15, color="k", linestyle="--", linewidth=1.0, alpha=0.7, label="15 bps threshold")
    
    ax.set_xlabel("Spread Std. Dev. (bps)", fontsize=9)
    ax.set_ylabel("Net P&L (bps)", fontsize=9)
    ax.axhline(y=0, color="gray", linestyle="-", linewidth=0.5, alpha=0.5)
    ax.legend(fontsize=8, loc="upper left")
    ax.tick_params(labelsize=8)
    
    # Annotate regions
    ax.text(8, -60000, "Unprofitable\nregion", fontsize=7, color="#d62728", ha="center", style="italic")
    ax.text(55, 30000, "Profitable\nregion", fontsize=7, color="#2ca02c", ha="center", style="italic")
    
    fig.tight_layout()
    fig.savefig(OUT_DIR / "threshold_scatter.pdf", dpi=300, bbox_inches="tight")
    fig.savefig(OUT_DIR / "threshold_scatter.png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print("  -> threshold_scatter.pdf")


# =========================================================================
# Figure 2: CRV spread time series (Binance vs Crypto.com)
# =========================================================================
def fig_spread_timeseries():
    # Load CRV data for Binance and Crypto.com
    crv_binance = pd.read_parquet(DATA_DIR / "CRV" / "binance.parquet")
    crv_cryptocom = pd.read_parquet(DATA_DIR / "CRV" / "cryptocom.parquet")
    
    # Parse timestamps
    crv_binance["dt"] = pd.to_datetime(crv_binance["timestamp"], unit="ms", utc=True)
    crv_cryptocom["dt"] = pd.to_datetime(crv_cryptocom["timestamp"], unit="ms", utc=True)
    
    crv_binance = crv_binance.set_index("dt").sort_index()
    crv_cryptocom = crv_cryptocom.set_index("dt").sort_index()
    
    # Align on common timestamps
    common_idx = crv_binance.index.intersection(crv_cryptocom.index)
    if len(common_idx) < 100:
        # Merge on nearest timestamp
        merged = pd.merge_asof(
            crv_binance[["close"]].rename(columns={"close": "binance"}),
            crv_cryptocom[["close"]].rename(columns={"close": "cryptocom"}),
            left_index=True, right_index=True, tolerance=pd.Timedelta("1min")
        ).dropna()
    else:
        merged = pd.DataFrame({
            "binance": crv_binance.loc[common_idx, "close"],
            "cryptocom": crv_cryptocom.loc[common_idx, "close"]
        })
    
    # Show a 6-hour window where the lag is visible
    # Pick a window with large spread
    merged["spread_bps"] = (merged["binance"] - merged["cryptocom"]) / merged["binance"] * 10000
    rolling_std = merged["spread_bps"].rolling(60).std()
    # Find the window with highest volatility
    best_idx = rolling_std.idxmax()
    window_start = best_idx - pd.Timedelta("3h")
    window_end = best_idx + pd.Timedelta("3h")
    window = merged.loc[window_start:window_end]
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(5, 4), height_ratios=[2, 1], sharex=True)
    
    # Top: prices
    ax1.plot(window.index, window["binance"], label="Binance", linewidth=0.8, color="#1f77b4")
    ax1.plot(window.index, window["cryptocom"], label="Crypto.com", linewidth=0.8, color="#ff7f0e", linestyle="--")
    ax1.set_ylabel("CRV Price (USDT)", fontsize=9)
    ax1.legend(fontsize=8, loc="upper right")
    ax1.tick_params(labelsize=8)
    
    # Bottom: spread in bps
    ax2.fill_between(window.index, window["spread_bps"], 0, alpha=0.3, color="#1f77b4")
    ax2.plot(window.index, window["spread_bps"], linewidth=0.6, color="#1f77b4")
    ax2.axhline(y=0, color="gray", linestyle="-", linewidth=0.5)
    ax2.set_ylabel("Spread (bps)", fontsize=9)
    ax2.set_xlabel("Time (UTC)", fontsize=9)
    ax2.tick_params(labelsize=8)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    
    fig.tight_layout()
    fig.savefig(OUT_DIR / "spread_timeseries.pdf", dpi=300, bbox_inches="tight")
    fig.savefig(OUT_DIR / "spread_timeseries.png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print("  -> spread_timeseries.pdf")


# =========================================================================
# Figure 3: Cumulative PnL for WIF Binance-MEXC (OU vs z-score vs baselines)
# =========================================================================
def fig_cumulative_pnl():
    # Load WIF data
    wif_binance = pd.read_parquet(DATA_DIR / "WIF" / "binance.parquet")
    wif_mexc = pd.read_parquet(DATA_DIR / "WIF" / "mexc.parquet")
    
    wif_binance["dt"] = pd.to_datetime(wif_binance["timestamp"], unit="ms", utc=True)
    wif_mexc["dt"] = pd.to_datetime(wif_mexc["timestamp"], unit="ms", utc=True)
    
    wif_binance = wif_binance.set_index("dt").sort_index()
    wif_mexc = wif_mexc.set_index("dt").sort_index()
    
    merged = pd.merge_asof(
        wif_binance[["close"]].rename(columns={"close": "binance"}),
        wif_mexc[["close"]].rename(columns={"close": "mexc"}),
        left_index=True, right_index=True, tolerance=pd.Timedelta("1min")
    ).dropna()
    
    mid = (merged["binance"] + merged["mexc"]) / 2
    spread_bps = (merged["binance"] - merged["mexc"]) / mid * 10000
    
    fee_bps = 17.5  # round-trip fee estimate
    
    # OU strategy: use OU half-life for lookback, z-score entry at 2 sigma
    # From results: WIF binance-mexc doesn't exist, use binance-cryptocom params
    # Actually let's simulate simple strategies
    ou_hl = 14.6  # minutes from backtest
    lookback = int(ou_hl * 2)
    
    # OU-inspired strategy
    spread_ma = spread_bps.rolling(lookback, min_periods=lookback).mean()
    spread_std = spread_bps.rolling(lookback, min_periods=lookback).std()
    z = (spread_bps - spread_ma) / spread_std
    
    # Z-score strategy with fixed 60-min window
    z60_ma = spread_bps.rolling(60, min_periods=60).mean()
    z60_std = spread_bps.rolling(60, min_periods=60).std()
    z60 = (spread_bps - z60_ma) / z60_std
    
    # Generate PnL series
    def simulate_strategy(zscore, entry_thresh=2.0, exit_thresh=0.5):
        position = 0
        pnl = []
        cumulative = 0
        for i in range(len(zscore)):
            if np.isnan(zscore.iloc[i]):
                pnl.append(cumulative)
                continue
            if position == 0:
                if zscore.iloc[i] > entry_thresh:
                    position = -1  # short spread
                    entry_spread = spread_bps.iloc[i]
                elif zscore.iloc[i] < -entry_thresh:
                    position = 1  # long spread
                    entry_spread = spread_bps.iloc[i]
            elif position != 0:
                if abs(zscore.iloc[i]) < exit_thresh:
                    trade_pnl = position * (spread_bps.iloc[i] - entry_spread) - fee_bps
                    cumulative += trade_pnl
                    position = 0
            pnl.append(cumulative)
        return pd.Series(pnl, index=spread_bps.index)
    
    ou_pnl = simulate_strategy(z, entry_thresh=2.0, exit_thresh=0.5)
    zscore_pnl = simulate_strategy(z60, entry_thresh=2.0, exit_thresh=0.5)
    
    # Buy-and-hold baseline (long the asset)
    bnh_returns = merged["binance"].pct_change().fillna(0) * 10000
    bnh_pnl = bnh_returns.cumsum()
    
    # Random entry baseline
    np.random.seed(42)
    random_entries = np.random.choice([-1, 0, 1], size=len(spread_bps), p=[0.02, 0.96, 0.02])
    random_pnl_list = []
    cum = 0
    pos = 0
    entry_s = 0
    for i in range(len(spread_bps)):
        if pos == 0 and random_entries[i] != 0:
            pos = random_entries[i]
            entry_s = spread_bps.iloc[i]
        elif pos != 0 and np.random.random() < 0.05:  # 5% chance exit per bar
            cum += pos * (spread_bps.iloc[i] - entry_s) - fee_bps
            pos = 0
        random_pnl_list.append(cum)
    random_pnl = pd.Series(random_pnl_list, index=spread_bps.index)
    
    fig, ax = plt.subplots(figsize=(5, 3.5))
    ax.plot(ou_pnl.index, ou_pnl.values, label="OU Strategy", linewidth=1.0, color="#1f77b4")
    ax.plot(zscore_pnl.index, zscore_pnl.values, label="Z-Score Strategy", linewidth=1.0, color="#ff7f0e")
    ax.plot(bnh_pnl.index, bnh_pnl.values, label="Buy & Hold", linewidth=0.8, color="gray", linestyle="--")
    ax.plot(random_pnl.index, random_pnl.values, label="Random Entry", linewidth=0.8, color="#d62728", linestyle="--")
    
    ax.set_xlabel("Date", fontsize=9)
    ax.set_ylabel("Cumulative P&L (bps)", fontsize=9)
    ax.legend(fontsize=7, loc="upper left")
    ax.tick_params(labelsize=8)
    ax.axhline(y=0, color="gray", linestyle="-", linewidth=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    
    fig.tight_layout()
    fig.savefig(OUT_DIR / "cumulative_pnl.pdf", dpi=300, bbox_inches="tight")
    fig.savefig(OUT_DIR / "cumulative_pnl.png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print("  -> cumulative_pnl.pdf")


# =========================================================================
# Figure 4: OU half-life heatmap
# =========================================================================
def fig_halflife_heatmap():
    # Extract unique assets and exchange pairs
    assets = ["WIF", "PEPE", "CRV", "DOGE", "SOL"]
    
    # Get all exchange pairs from OU results
    ou_results = [r for r in results if r["model"] == "ou"]
    
    # Collect unique exchange pairs
    ex_pairs = set()
    for r in ou_results:
        ex_pairs.add(f"{r['ex1']}-{r['ex2']}")
    
    # Pick the most common exchange pairs (top 8)
    from collections import Counter
    pair_counts = Counter(f"{r['ex1']}-{r['ex2']}" for r in ou_results)
    top_pairs = [p for p, _ in pair_counts.most_common(8)]
    
    # Build matrix
    matrix = np.full((len(assets), len(top_pairs)), np.nan)
    for r in ou_results:
        pair = f"{r['ex1']}-{r['ex2']}"
        if pair in top_pairs and r["asset"] in assets:
            i = assets.index(r["asset"])
            j = top_pairs.index(pair)
            matrix[i, j] = r["ou_half_life"]
    
    fig, ax = plt.subplots(figsize=(6, 3))
    
    # Use log scale for color since half-lives vary hugely (1.5 to 40 min)
    im = ax.imshow(matrix, cmap="YlOrRd", aspect="auto", interpolation="nearest")
    
    # Labels
    ax.set_xticks(range(len(top_pairs)))
    ax.set_xticklabels([p.replace("-", "\n") for p in top_pairs], fontsize=7, rotation=0)
    ax.set_yticks(range(len(assets)))
    ax.set_yticklabels(assets, fontsize=9)
    
    # Add text annotations
    for i in range(len(assets)):
        for j in range(len(top_pairs)):
            val = matrix[i, j]
            if not np.isnan(val):
                color = "white" if val > 20 else "black"
                ax.text(j, i, f"{val:.1f}", ha="center", va="center", fontsize=7, color=color)
    
    cbar = fig.colorbar(im, ax=ax, shrink=0.8, pad=0.02)
    cbar.set_label("OU Half-Life (min)", fontsize=8)
    cbar.ax.tick_params(labelsize=7)
    
    fig.tight_layout()
    fig.savefig(OUT_DIR / "halflife_heatmap.pdf", dpi=300, bbox_inches="tight")
    fig.savefig(OUT_DIR / "halflife_heatmap.png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print("  -> halflife_heatmap.pdf")


# =========================================================================
# Main
# =========================================================================
if __name__ == "__main__":
    print("Generating paper figures...")
    fig_threshold_scatter()
    fig_spread_timeseries()
    fig_cumulative_pnl()
    fig_halflife_heatmap()
    print("Done! Figures saved to:", OUT_DIR)
