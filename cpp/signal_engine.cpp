/*
 * signal_engine.cpp — High-performance signal computation for
 * cross-venue spread trading.
 *
 * Implements the computational hot paths in C++ with pybind11 bindings:
 *   1. OU parameter estimation (OLS-based MLE)
 *   2. Rolling z-score computation
 *   3. OU-based z-score computation
 *   4. Batch backtest signal generation
 *
 * These functions are called on every tick in live trading and on every
 * bar in backtesting. Moving them from Python/NumPy to C++ eliminates
 * interpreter overhead, memory allocation, and Python object boxing.
 *
 * Build:
 *   python setup.py build_ext --inplace
 *
 * Usage:
 *   import signal_engine
 *   theta, mu, sigma = signal_engine.estimate_ou_params(spreads, dt=1.0)
 *   z = signal_engine.ou_zscore(spreads, window=60)
 */

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include <cmath>
#include <vector>
#include <algorithm>
#include <numeric>
#include <tuple>
#include <stdexcept>

namespace py = pybind11;

// ============================================================================
//  Core: OU Parameter Estimation via OLS
// ============================================================================

struct OUParams {
    double theta;
    double mu;
    double sigma;
    bool valid;
};

/*
 * Estimate OU parameters from a spread time series using OLS regression.
 *
 * Model: dS_t = (a + b * S_t) * dt + sigma * dW_t
 * where theta = -b/dt, mu = a/(theta*dt), sigma = std(residuals)/sqrt(dt)
 *
 * This is the discretized Euler-Maruyama scheme for the OU process:
 *   dX_t = theta * (mu - X_t) * dt + sigma * dW_t
 */
OUParams estimate_ou_params_impl(const double* data, size_t n, double dt) {
    OUParams result{0.0, 0.0, 0.0, false};

    if (n < 20) return result;

    size_t m = n - 1;  // number of dS observations

    // Compute OLS: dS = a + b*S + residual
    // Using normal equations: [sum(1), sum(S); sum(S), sum(S^2)] * [a; b] = [sum(dS); sum(S*dS)]
    double sum_s = 0.0, sum_s2 = 0.0, sum_ds = 0.0, sum_s_ds = 0.0;

    for (size_t i = 0; i < m; ++i) {
        double s = data[i];
        double ds = data[i + 1] - data[i];
        sum_s += s;
        sum_s2 += s * s;
        sum_ds += ds;
        sum_s_ds += s * ds;
    }

    double n_d = static_cast<double>(m);
    double det = n_d * sum_s2 - sum_s * sum_s;
    if (std::abs(det) < 1e-15) return result;

    double a = (sum_s2 * sum_ds - sum_s * sum_s_ds) / det;
    double b = (n_d * sum_s_ds - sum_s * sum_ds) / det;

    if (b >= 0.0) return result;  // not mean-reverting

    double theta = -b / dt;
    double mu = a / (theta * dt);

    // Compute residual std
    double sse = 0.0;
    for (size_t i = 0; i < m; ++i) {
        double predicted = a + b * data[i];
        double residual = (data[i + 1] - data[i]) - predicted;
        sse += residual * residual;
    }
    double sigma = std::sqrt(sse / n_d) / std::sqrt(dt);

    if (sigma < 1e-10) return result;

    result.theta = theta;
    result.mu = mu;
    result.sigma = sigma;
    result.valid = true;
    return result;
}

// Python-facing wrapper
py::tuple py_estimate_ou_params(py::array_t<double> spreads, double dt = 1.0) {
    auto buf = spreads.request();
    if (buf.ndim != 1) throw std::runtime_error("spreads must be 1-D");

    auto result = estimate_ou_params_impl(
        static_cast<const double*>(buf.ptr), buf.shape[0], dt
    );

    if (!result.valid)
        return py::make_tuple(py::none(), py::none(), py::none());

    return py::make_tuple(result.theta, result.mu, result.sigma);
}


// ============================================================================
//  Rolling Z-Score
// ============================================================================

/*
 * Compute z-score of the last element relative to a rolling window.
 * Returns NaN if window is insufficient or std is near zero.
 */
double rolling_zscore(const double* data, size_t n, int window) {
    if (static_cast<int>(n) < window || window < 2)
        return std::numeric_limits<double>::quiet_NaN();

    size_t start = n - window;
    double sum = 0.0, sum2 = 0.0;
    for (size_t i = start; i < n; ++i) {
        sum += data[i];
        sum2 += data[i] * data[i];
    }

    double mean = sum / window;
    double var = (sum2 / window) - (mean * mean);
    if (var < 1e-20) return std::numeric_limits<double>::quiet_NaN();

    double std = std::sqrt(var);
    return (data[n - 1] - mean) / std;
}

double py_rolling_zscore(py::array_t<double> spreads, int window) {
    auto buf = spreads.request();
    if (buf.ndim != 1) throw std::runtime_error("spreads must be 1-D");
    return rolling_zscore(static_cast<const double*>(buf.ptr), buf.shape[0], window);
}


// ============================================================================
//  OU-Based Z-Score
// ============================================================================

/*
 * Compute z-score using OU-estimated mu and sigma.
 * z = (current_spread - mu) / sigma
 */
double ou_zscore(const double* data, size_t n, int window, double dt = 1.0) {
    if (static_cast<int>(n) < window || window < 20)
        return std::numeric_limits<double>::quiet_NaN();

    // Estimate OU on trailing window
    const double* window_start = data + (n - window);
    auto params = estimate_ou_params_impl(window_start, window, dt);

    if (!params.valid)
        return std::numeric_limits<double>::quiet_NaN();

    return (data[n - 1] - params.mu) / params.sigma;
}

double py_ou_zscore(py::array_t<double> spreads, int window, double dt = 1.0) {
    auto buf = spreads.request();
    if (buf.ndim != 1) throw std::runtime_error("spreads must be 1-D");
    return ou_zscore(static_cast<const double*>(buf.ptr), buf.shape[0], window, dt);
}


// ============================================================================
//  Batch Signal Generation (for backtesting)
// ============================================================================

/*
 * Generate z-score signals for an entire spread series at once.
 * This is the main backtest hot path — avoids per-bar Python overhead.
 *
 * Returns a numpy array of z-scores, one per bar (NaN for warmup period).
 */
py::array_t<double> batch_ou_signals(
    py::array_t<double> spreads, int window, double dt = 1.0
) {
    auto buf = spreads.request();
    if (buf.ndim != 1) throw std::runtime_error("spreads must be 1-D");

    size_t n = buf.shape[0];
    const double* data = static_cast<const double*>(buf.ptr);

    auto result = py::array_t<double>(n);
    auto res_buf = result.request();
    double* out = static_cast<double*>(res_buf.ptr);

    // Fill warmup with NaN
    for (size_t i = 0; i < static_cast<size_t>(window) && i < n; ++i)
        out[i] = std::numeric_limits<double>::quiet_NaN();

    // Compute signals
    for (size_t i = window; i < n; ++i) {
        out[i] = ou_zscore(data, i + 1, window, dt);
    }

    return result;
}

py::array_t<double> batch_rolling_signals(
    py::array_t<double> spreads, int window
) {
    auto buf = spreads.request();
    if (buf.ndim != 1) throw std::runtime_error("spreads must be 1-D");

    size_t n = buf.shape[0];
    const double* data = static_cast<const double*>(buf.ptr);

    auto result = py::array_t<double>(n);
    auto res_buf = result.request();
    double* out = static_cast<double*>(res_buf.ptr);

    for (size_t i = 0; i < static_cast<size_t>(window) && i < n; ++i)
        out[i] = std::numeric_limits<double>::quiet_NaN();

    for (size_t i = window; i < n; ++i) {
        out[i] = rolling_zscore(data, i + 1, window);
    }

    return result;
}


// ============================================================================
//  Batch Backtest Engine
// ============================================================================

struct TradeResult {
    size_t entry_idx;
    size_t exit_idx;
    int direction;  // 1 = long_spread, -1 = short_spread
    double entry_spread;
    double exit_spread;
    double pnl_gross;
    double pnl_net;
};

/*
 * Run a complete mean-reversion backtest in C++.
 * Returns a list of trades with P&L.
 */
std::vector<TradeResult> backtest_engine(
    py::array_t<double> spreads,
    int window,
    double entry_z,
    double exit_z,
    double fee_bps,
    int max_holding,
    double dt = 1.0,
    const std::string& strategy = "ou"
) {
    auto buf = spreads.request();
    if (buf.ndim != 1) throw std::runtime_error("spreads must be 1-D");

    size_t n = buf.shape[0];
    const double* data = static_cast<const double*>(buf.ptr);

    std::vector<TradeResult> trades;
    int position = 0;  // 0 = flat, 1 = long, -1 = short
    size_t entry_idx = 0;
    double entry_spread = 0.0;

    for (size_t i = window; i < n; ++i) {
        double z;
        if (strategy == "ou") {
            z = ou_zscore(data, i + 1, window, dt);
        } else {
            z = rolling_zscore(data, i + 1, window);
        }

        if (std::isnan(z)) continue;

        double s = data[i];

        if (position == 0) {
            if (z > entry_z) {
                position = -1;
                entry_idx = i;
                entry_spread = s;
            } else if (z < -entry_z) {
                position = 1;
                entry_idx = i;
                entry_spread = s;
            }
        } else {
            bool should_exit = false;
            if (position == -1 && z < exit_z) should_exit = true;
            if (position == 1 && z > -exit_z) should_exit = true;
            if (static_cast<int>(i - entry_idx) >= max_holding) should_exit = true;

            if (should_exit) {
                double pnl_gross = (position == -1)
                    ? (entry_spread - s)
                    : (s - entry_spread);
                trades.push_back({
                    entry_idx, i, position,
                    entry_spread, s,
                    pnl_gross, pnl_gross - fee_bps
                });
                position = 0;
            }
        }
    }

    return trades;
}


// ============================================================================
//  pybind11 Module
// ============================================================================

PYBIND11_MODULE(signal_engine, m) {
    m.doc() = "High-performance signal computation for cross-venue spread trading";

    m.def("estimate_ou_params", &py_estimate_ou_params,
        "Estimate OU parameters (theta, mu, sigma) from spread series",
        py::arg("spreads"), py::arg("dt") = 1.0);

    m.def("rolling_zscore", &py_rolling_zscore,
        "Compute rolling z-score of last element",
        py::arg("spreads"), py::arg("window"));

    m.def("ou_zscore", &py_ou_zscore,
        "Compute OU-based z-score using trailing window",
        py::arg("spreads"), py::arg("window"), py::arg("dt") = 1.0);

    m.def("batch_ou_signals", &batch_ou_signals,
        "Generate OU z-score signals for entire series (backtest)",
        py::arg("spreads"), py::arg("window"), py::arg("dt") = 1.0);

    m.def("batch_rolling_signals", &batch_rolling_signals,
        "Generate rolling z-score signals for entire series (backtest)",
        py::arg("spreads"), py::arg("window"));

    m.def("backtest", &backtest_engine,
        "Run complete backtest in C++, returns list of trades",
        py::arg("spreads"), py::arg("window"),
        py::arg("entry_z") = 2.0, py::arg("exit_z") = 0.5,
        py::arg("fee_bps") = 17.5, py::arg("max_holding") = 1440,
        py::arg("dt") = 1.0, py::arg("strategy") = "ou");

    py::class_<TradeResult>(m, "TradeResult")
        .def_readonly("entry_idx", &TradeResult::entry_idx)
        .def_readonly("exit_idx", &TradeResult::exit_idx)
        .def_readonly("direction", &TradeResult::direction)
        .def_readonly("entry_spread", &TradeResult::entry_spread)
        .def_readonly("exit_spread", &TradeResult::exit_spread)
        .def_readonly("pnl_gross", &TradeResult::pnl_gross)
        .def_readonly("pnl_net", &TradeResult::pnl_net);
}
