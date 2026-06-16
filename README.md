# Stochastic Spread Modeling

Cross-venue cryptocurrency statistical arbitrage using Ornstein–Uhlenbeck mean-reversion signals on high-frequency OHLCV data.

Accompanying code for the paper:
> **Stochastic Spread Modeling for Cross-Venue Cryptocurrency Trading: An Ornstein–Uhlenbeck Framework on High-Frequency OHLCV Data**
> Kevin Litvin, Tania Pocrnjic — Simon Fraser University
> *Submitted to ACM ICAIF 2026*

## Overview

The system models cross-exchange price spreads as Ornstein–Uhlenbeck processes, estimates mean-reversion parameters online, and generates trading signals when z-scores breach configurable thresholds. It supports 12 exchanges (Binance, Kraken, KuCoin, Bybit, OKX, Gate.io, etc.) via [ccxt](https://github.com/ccxt/ccxt) and runs on 1-minute OHLCV candles.

### Project Structure

```
├── experiments/
│   ├── paper_trader.py          # Live paper trading with WebSocket feeds
│   ├── backtest_historical.py   # Historical backtester on Parquet data
│   ├── download_historical_ohlcv.py
│   ├── robustness_test.py       # ADF tests, rolling window stability
│   ├── train_spread_model.py    # ML spread predictor (LightGBM)
│   └── ...
├── cpp/
│   ├── signal_engine.cpp        # C++ signal computation (pybind11)
│   └── ANALYSIS.md              # Profiling rationale for C++ port
├── benchmarks/
│   └── bench_signal_engine.py   # Python vs C++ benchmark harness
├── scripts/
│   ├── data.py                  # Exchange configs and coin universe
│   └── fees.py                  # Per-exchange taker fee schedule
├── tests/
│   └── test_core.py
├── paper/                       # LaTeX source (ACM acmart sigconf)
├── setup.py                     # C++ extension build script
└── requirements.txt
```

## Setup

### Python Dependencies

```bash
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows

pip install -r requirements.txt
```

### C++ Signal Engine (Optional)

The hot-path signal functions have C++ implementations that provide **160–200× speedup** over NumPy for batch operations. The C++ module is optional — all Python code falls back to NumPy implementations if it is not compiled.

#### Prerequisites

You need a C++17 compiler and Python development headers:

| OS      | Compiler | Install                                                                 |
|---------|----------|-------------------------------------------------------------------------|
| Windows | MSVC     | [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) — select "Desktop development with C++" |
| macOS   | Clang    | `xcode-select --install`                                                |
| Linux   | GCC      | `sudo apt install build-essential python3-dev` (Debian/Ubuntu)          |

#### Build

```bash
pip install pybind11
python setup.py build_ext --inplace
```

This compiles `cpp/signal_engine.cpp` and produces a shared library in the project root:
- **Windows**: `signal_engine.cp3XX-win_amd64.pyd`
- **macOS**: `signal_engine.cpython-3XX-darwin.so`
- **Linux**: `signal_engine.cpython-3XX-x86_64-linux-gnu.so`

#### Verify

```bash
python -c "import signal_engine; print('C++ engine loaded')"
```

## Usage

### Historical Backtesting

```bash
# Download 30 days of 1-min OHLCV data
python experiments/download_historical_ohlcv.py

# Run backtest (all assets)
python experiments/backtest_historical.py

# Specific asset with custom parameters
python experiments/backtest_historical.py --asset CRV --entry-z 1.5 --exit-z 0.3 --window 60
```

### Live Paper Trading

```bash
python experiments/paper_trader.py --asset WIF --exchanges binance,cryptocom --strategy ou
```

Press Ctrl+C to stop. Results are saved to `data/paper_trading/`.

### Long-Run Data Collection

Use the stat-arb collector to build a multi-signal dataset for research and model training:

```bash
# 7-day run, 60s cadence, slow signals every 10 snapshots
python -m experiments.collect_statarb_data --assets volatile --interval 60 --slow-every 10 --hours 168
```

Useful flags:
- `--skip-ohlcv`: disable OHLCV pulls when you want lighter network/API load.
- `--hours`: total run duration (e.g. `0.5` for 30 minutes).
- `--slow-every`: controls how often slow signals are fetched (higher = less frequent).

Output is written under `data/statarb/<run_timestamp>/` and partitioned by UTC day per signal.

#### Check Collector Bandwidth Without Stopping It (Windows PowerShell)

```powershell
# Find collector python processes and sample per-process I/O throughput.
# This does not stop or restart the collector.
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'collect_statarb_data' } |
  Select-Object ProcessId, Name, CommandLine

# Replace <PID> with the active collector PID from above.
$pidValue = <PID>
$instance = (Get-Counter '\Process(*)\ID Process').CounterSamples |
  Where-Object { [int]$_.CookedValue -eq $pidValue } |
  Select-Object -First 1 -ExpandProperty InstanceName
Get-Counter "\Process($instance)\IO Other Bytes/sec" -SampleInterval 1 -MaxSamples 30
```

### Benchmarks

```bash
python benchmarks/bench_signal_engine.py
```

Sample results (AMD Ryzen 7, Python 3.13):

| Function            | Python (NumPy) | C++     | Speedup |
|---------------------|---------------|---------|---------|
| `estimate_ou_params`| 32.1 μs       | 1.3 μs  | 25×     |
| `ou_zscore`         | 39.2 μs       | 1.0 μs  | 38×     |
| `batch_ou_signals`  | 1,520 ms      | 7.7 ms  | 196×    |
| `backtest_engine`   | 1,860 ms      | 8.9 ms  | 209×    |

### Tests

```bash
pytest tests/
```

## License

Research code — not licensed for production trading use.
