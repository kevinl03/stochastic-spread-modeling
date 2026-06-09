#!/usr/bin/env python3
"""
Benchmark: Python (NumPy) vs C++ signal engine.

Measures wall-clock time for the hot-path functions used in live trading
and backtesting. Uses realistic data sizes matching production workloads.

Usage:
    python benchmarks/bench_signal_engine.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Python Reference Implementations ────────────────────────────────────────

def py_estimate_ou_params(spread: np.ndarray, dt: float = 1.0):
    """OLS-based OU parameter estimation (Python/NumPy)."""
    S = spread[:-1]
    dS = np.diff(spread)
    if len(S) < 20:
        return None, None, None

    X = np.column_stack([np.ones_like(S), S])
    beta, _, _, _ = np.linalg.lstsq(X, dS, rcond=None)
    a, b = beta

    if b >= 0:
        return None, None, None

    theta = -b / dt
    mu = a / (theta * dt) if abs(theta) > 1e-10 else np.mean(spread)
    residuals = dS - (a + b * S)
    sigma = np.std(residuals) / np.sqrt(dt)

    if sigma < 1e-10:
        return None, None, None

    return theta, mu, sigma


def py_rolling_zscore(spread: np.ndarray, window: int) -> float:
    """Rolling z-score (Python/NumPy)."""
    if len(spread) < window:
        return float("nan")
    w = spread[-window:]
    m = np.mean(w)
    s = np.std(w)
    if s < 1e-10:
        return float("nan")
    return float((spread[-1] - m) / s)


def py_ou_zscore(spread: np.ndarray, window: int, dt: float = 1.0) -> float:
    """OU-based z-score (Python/NumPy)."""
    if len(spread) < window:
        return float("nan")
    w = spread[-window:]
    theta, mu, sigma = py_estimate_ou_params(w, dt)
    if theta is None:
        return float("nan")
    return float((spread[-1] - mu) / sigma)


def py_batch_ou_signals(spread: np.ndarray, window: int, dt: float = 1.0):
    """Batch OU signals for backtesting (Python loop)."""
    n = len(spread)
    out = np.full(n, np.nan)
    for i in range(window, n):
        out[i] = py_ou_zscore(spread[: i + 1], window, dt)
    return out


def py_batch_rolling_signals(spread: np.ndarray, window: int):
    """Batch rolling signals for backtesting (Python loop)."""
    n = len(spread)
    out = np.full(n, np.nan)
    for i in range(window, n):
        out[i] = py_rolling_zscore(spread[: i + 1], window)
    return out


def py_backtest(spread, window, entry_z=2.0, exit_z=0.5,
                fee_bps=17.5, max_holding=1440, dt=1.0, strategy="ou"):
    """Pure Python backtest loop."""
    n = len(spread)
    trades = []
    position = 0
    entry_idx = 0
    entry_spread = 0.0

    for i in range(window, n):
        if strategy == "ou":
            z = py_ou_zscore(spread[:i+1], window, dt)
        else:
            z = py_rolling_zscore(spread[:i+1], window)

        if np.isnan(z):
            continue

        s = spread[i]

        if position == 0:
            if z > entry_z:
                position = -1
                entry_idx = i
                entry_spread = s
            elif z < -entry_z:
                position = 1
                entry_idx = i
                entry_spread = s
        else:
            should_exit = False
            if position == -1 and z < exit_z:
                should_exit = True
            if position == 1 and z > -exit_z:
                should_exit = True
            if (i - entry_idx) >= max_holding:
                should_exit = True
            if should_exit:
                pnl = (entry_spread - s) if position == -1 else (s - entry_spread)
                trades.append((entry_idx, i, position, pnl, pnl - fee_bps))
                position = 0

    return trades


# ── Benchmark Harness ───────────────────────────────────────────────────────

def generate_ou_spread(n: int, theta: float = 0.05, mu: float = 0.0,
                       sigma: float = 1.0, seed: int = 42) -> np.ndarray:
    """Generate synthetic OU process for benchmarking."""
    rng = np.random.default_rng(seed)
    dt = 1.0
    x = np.zeros(n)
    x[0] = mu
    for i in range(1, n):
        x[i] = x[i-1] + theta * (mu - x[i-1]) * dt + sigma * np.sqrt(dt) * rng.standard_normal()
    return x


def timeit(func, *args, repeats: int = 5, **kwargs) -> float:
    """Return median wall-clock time in seconds."""
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        func(*args, **kwargs)
        times.append(time.perf_counter() - t0)
    return sorted(times)[len(times) // 2]


def format_time(t: float) -> str:
    if t < 1e-6:
        return f"{t*1e9:.0f} ns"
    if t < 1e-3:
        return f"{t*1e6:.1f} us"
    if t < 1:
        return f"{t*1e3:.2f} ms"
    return f"{t:.3f} s"


def bench_correctness(cpp_mod, spread, window):
    """Verify C++ results match Python results."""
    print("\n=== Correctness Validation ===")

    # OU params
    py_t, py_m, py_s = py_estimate_ou_params(spread, 1.0)
    cpp_result = cpp_mod.estimate_ou_params(spread, 1.0)
    cpp_t, cpp_m, cpp_s = cpp_result
    print(f"  OU params (Python): theta={py_t:.6f}, mu={py_m:.6f}, sigma={py_s:.6f}")
    print(f"  OU params (C++):    theta={cpp_t:.6f}, mu={cpp_m:.6f}, sigma={cpp_s:.6f}")
    assert abs(py_t - cpp_t) < 1e-6, f"theta mismatch: {py_t} vs {cpp_t}"
    assert abs(py_m - cpp_m) < 1e-4, f"mu mismatch: {py_m} vs {cpp_m}"
    assert abs(py_s - cpp_s) < 1e-6, f"sigma mismatch: {py_s} vs {cpp_s}"
    print("  ✓ OU params match")

    # Rolling z-score
    py_z = py_rolling_zscore(spread, window)
    cpp_z = cpp_mod.rolling_zscore(spread, window)
    assert abs(py_z - cpp_z) < 1e-10, f"rolling z mismatch: {py_z} vs {cpp_z}"
    print(f"  ✓ Rolling z-score match: {py_z:.6f}")

    # OU z-score
    py_oz = py_ou_zscore(spread, window, 1.0)
    cpp_oz = cpp_mod.ou_zscore(spread, window, 1.0)
    assert abs(py_oz - cpp_oz) < 1e-6, f"OU z mismatch: {py_oz} vs {cpp_oz}"
    print(f"  ✓ OU z-score match: {py_oz:.6f}")

    print("  All correctness checks passed.\n")


def main():
    try:
        import signal_engine as cpp
        print("C++ signal_engine loaded successfully.\n")
    except ImportError as e:
        print(f"ERROR: Could not import signal_engine: {e}")
        print("Build it first:  python setup.py build_ext --inplace")
        sys.exit(1)

    # Data sizes matching real workloads
    scenarios = {
        "Single tick (live trading, window=60)":   (500, 60),
        "1-hour lookback (60 bars)":               (500, 60),
        "4-hour lookback (240 bars)":              (1000, 240),
        "Full-day signal (1440 bars, 1 day)":      (2000, 1440),
        "Full backtest (43200 bars, 30 days)":     (43200, 120),
    }

    print("=" * 76)
    print(f"{'Benchmark':40s} {'Python':>10s} {'C++':>10s} {'Speedup':>10s}")
    print("=" * 76)

    for scenario_name, (n, window) in scenarios.items():
        spread = generate_ou_spread(n)

        print(f"\n── {scenario_name} (n={n}, window={window}) ──")

        # 1. OU parameter estimation
        py_time = timeit(py_estimate_ou_params, spread, 1.0, repeats=100)
        cpp_time = timeit(cpp.estimate_ou_params, spread, 1.0, repeats=100)
        speedup = py_time / cpp_time if cpp_time > 0 else float("inf")
        print(f"  {'estimate_ou_params':36s} {format_time(py_time):>10s} {format_time(cpp_time):>10s} {speedup:>9.1f}x")

        # 2. Rolling z-score
        py_time = timeit(py_rolling_zscore, spread, window, repeats=200)
        cpp_time = timeit(cpp.rolling_zscore, spread, window, repeats=200)
        speedup = py_time / cpp_time if cpp_time > 0 else float("inf")
        print(f"  {'rolling_zscore':36s} {format_time(py_time):>10s} {format_time(cpp_time):>10s} {speedup:>9.1f}x")

        # 3. OU z-score
        py_time = timeit(py_ou_zscore, spread, window, 1.0, repeats=100)
        cpp_time = timeit(cpp.ou_zscore, spread, window, 1.0, repeats=100)
        speedup = py_time / cpp_time if cpp_time > 0 else float("inf")
        print(f"  {'ou_zscore':36s} {format_time(py_time):>10s} {format_time(cpp_time):>10s} {speedup:>9.1f}x")

    # 4. Batch signals (the big one — full backtest)
    print(f"\n── Batch backtest signals (n=43200, window=120) ──")
    spread_full = generate_ou_spread(43200)

    # Batch OU — only time small subset for Python (it's very slow)
    subset = spread_full[:2000]
    py_time_sub = timeit(py_batch_ou_signals, subset, 120, 1.0, repeats=1)
    cpp_time_sub = timeit(cpp.batch_ou_signals, subset, 120, 1.0, repeats=1)
    speedup_sub = py_time_sub / cpp_time_sub if cpp_time_sub > 0 else float("inf")
    print(f"  {'batch_ou_signals (n=2000)':36s} {format_time(py_time_sub):>10s} {format_time(cpp_time_sub):>10s} {speedup_sub:>9.1f}x")

    # Full 43K only for C++ (Python would take minutes)
    cpp_time_full = timeit(cpp.batch_ou_signals, spread_full, 120, 1.0, repeats=3)
    py_est = py_time_sub * (43200 / 2000)  # linear extrapolation
    speedup_full = py_est / cpp_time_full
    print(f"  {'batch_ou_signals (n=43200)':36s} {'~'+format_time(py_est):>10s} {format_time(cpp_time_full):>10s} {speedup_full:>9.1f}x")

    # Batch rolling
    py_time_roll = timeit(py_batch_rolling_signals, subset, 120, repeats=1)
    cpp_time_roll = timeit(cpp.batch_rolling_signals, subset, 120, repeats=1)
    speedup_roll = py_time_roll / cpp_time_roll
    print(f"  {'batch_rolling_signals (n=2000)':36s} {format_time(py_time_roll):>10s} {format_time(cpp_time_roll):>10s} {speedup_roll:>9.1f}x")

    # 5. Full backtest engine
    print(f"\n── Full backtest engine (n=2000, window=120) ──")
    py_time_bt = timeit(py_backtest, subset, 120, repeats=1)
    cpp_time_bt = timeit(cpp.backtest, subset, 120, repeats=1)
    speedup_bt = py_time_bt / cpp_time_bt
    print(f"  {'backtest engine':36s} {format_time(py_time_bt):>10s} {format_time(cpp_time_bt):>10s} {speedup_bt:>9.1f}x")

    cpp_time_bt_full = timeit(cpp.backtest, spread_full, 120, repeats=3)
    py_est_bt = py_time_bt * (43200 / 2000)
    speedup_bt_full = py_est_bt / cpp_time_bt_full
    print(f"  {'backtest engine (n=43200)':36s} {'~'+format_time(py_est_bt):>10s} {format_time(cpp_time_bt_full):>10s} {speedup_bt_full:>9.1f}x")

    print("\n" + "=" * 76)
    print("Benchmark complete.")

    # Correctness check
    bench_correctness(cpp, generate_ou_spread(1000), 120)


if __name__ == "__main__":
    main()
