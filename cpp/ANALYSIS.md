# C++ Signal Engine: Hot-Path Analysis

## Problem
The paper trading system (`experiments/paper_trader.py`) and historical
backtester (`experiments/backtest_historical.py`) spend most of their time
in a small set of numerical functions.  In live trading, these run on
**every tick** (~100-500ms).  In backtesting, they run on **every bar** of
a 43,200-bar dataset (30 days of 1-min candles × 50+ exchange pairs).

A single backtest sweep currently takes ~2 seconds in Python.  With
parameter grid search (5 entry-z × 5 exit-z × 5 windows × 50 pairs),
that is 2 × 6250 ≈ 3.5 hours.  This is the bottleneck.

## Profiling
Informal profiling (`cProfile` on `backtest_historical.py --asset WIF`)
shows the following call-time breakdown:

| Function                  | % of runtime | Calls  | Notes                           |
|---------------------------|-------------|--------|---------------------------------|
| `estimate_ou_params()`    | ~45%        | 43,000 | np.linalg.lstsq per bar         |
| `compute_ou_zscore()`     | ~25%        | 43,000 | Calls estimate_ou_params + norm |
| `compute_zscore_rolling()`| ~15%        | 43,000 | np.mean + np.std per bar        |
| Spread construction       | ~10%        | 1      | Pandas merge (one-time)         |
| Trade logic               | ~5%         | 43,000 | Pure Python comparisons         |

## Why these functions are slow in Python/NumPy

1. **`estimate_ou_params()`**: Allocates a new 2D array (`np.column_stack`)
   on every call, then calls LAPACK via `np.linalg.lstsq` which has ~30μs
   of overhead just for the Python→C→LAPACK dispatch, even on tiny arrays.
   Our arrays are only 60-240 elements — the overhead dominates the actual
   linear algebra.

2. **`compute_ou_zscore()` / `compute_zscore_rolling()`**: Each call creates
   a new slice (`spreads[-window:]`), computes mean/std with full NumPy
   dispatch.  The Python function-call overhead is ~5μs per call, which
   adds up at 43,000 calls.

3. **Per-bar Python loop**: The backtest iterates `for i in range(warmup, n)`
   and calls the signal function inside the loop.  Each iteration crosses
   the Python interpreter boundary.

## What to port to C++

The key insight: we don't need to optimize the *algorithm* — the OLS
regression and z-score math are already correct.  We need to eliminate the
**per-call overhead**:

- Python object allocation (numpy array creation per call)
- Python→C dispatch latency (lstsq, mean, std)
- Python interpreter loop overhead (43K iterations)

### Functions to port:
1. `estimate_ou_params(spreads, dt)` — OLS via closed-form normal equations
   instead of lstsq.  Avoids matrix allocation entirely.
2. `rolling_zscore(spreads, window)` — Single-pass mean/variance.
3. `ou_zscore(spreads, window)` — Combines #1 and #2.
4. `batch_ou_signals(spreads, window)` — Runs #3 for all bars at once,
   returning a full numpy array.  This eliminates the Python loop entirely.
5. `backtest_engine(...)` — The complete trading loop (signal + trade logic)
   in one C++ call.  Zero Python overhead per bar.

### Why closed-form OLS instead of lstsq:
The OU regression is always 2×2 (intercept + slope).  The normal equations
have an analytic solution: no need for QR decomposition or SVD.  This
reduces the problem from O(n) LAPACK overhead to O(n) arithmetic with
zero allocation.

### Why C++ over Rust:
- pybind11 is mature with excellent NumPy interop (zero-copy array access)
- MSVC on Windows, Clang/GCC on macOS/Linux — no cross-compilation needed
- Better fit for quant finance resume positioning (C++ is the dominant
  language at HRT, Citadel, Jane Street for low-latency systems)

## Expected speedup
- Per-call functions: 10-30x (eliminating Python/NumPy overhead)
- Batch operations: 100-200x (eliminating the Python loop entirely)
- Full backtest grid search: 3.5 hours → ~1-2 minutes
