/*
 * signal_engine.cpp — High-performance signal computation for
 * mean-reversion spread trading.
 *
 * Phase 1: OU parameter estimation via closed-form OLS.
 *
 * The Python version uses np.linalg.lstsq which has ~30μs of dispatch
 * overhead per call.  For a 2x2 normal equation system, we can solve
 * analytically in O(n) with zero allocation.
 *
 * Build:
 *   python setup.py build_ext --inplace
 *
 * Usage:
 *   import signal_engine
 *   theta, mu, sigma = signal_engine.estimate_ou_params(spreads, dt=1.0)
 */

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <cmath>
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
 *
 * Instead of calling LAPACK lstsq (which allocates matrices and does
 * QR decomposition), we solve the 2x2 normal equations directly:
 *   [n,    sum(S) ] [a]   [sum(dS)  ]
 *   [sum(S), sum(S²)] [b] = [sum(S·dS)]
 */
OUParams estimate_ou_params_impl(const double* data, size_t n, double dt) {
    OUParams result{0.0, 0.0, 0.0, false};

    if (n < 20) return result;

    size_t m = n - 1;  // number of dS observations

    // Single pass: accumulate sufficient statistics
    double sum_s = 0.0, sum_s2 = 0.0, sum_ds = 0.0, sum_s_ds = 0.0;

    for (size_t i = 0; i < m; ++i) {
        double s = data[i];
        double ds = data[i + 1] - data[i];
        sum_s += s;
        sum_s2 += s * s;
        sum_ds += ds;
        sum_s_ds += s * ds;
    }

    // Solve 2x2 system via Cramer's rule
    double n_d = static_cast<double>(m);
    double det = n_d * sum_s2 - sum_s * sum_s;
    if (std::abs(det) < 1e-15) return result;

    double a = (sum_s2 * sum_ds - sum_s * sum_s_ds) / det;
    double b = (n_d * sum_s_ds - sum_s * sum_ds) / det;

    if (b >= 0.0) return result;  // not mean-reverting

    double theta = -b / dt;
    double mu = a / (theta * dt);

    // Compute residual std (second pass)
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
//  pybind11 Module
// ============================================================================

PYBIND11_MODULE(signal_engine, m) {
    m.doc() = "High-performance signal computation for mean-reversion spread trading";

    m.def("estimate_ou_params", &py_estimate_ou_params,
        "Estimate OU parameters (theta, mu, sigma) from spread series.\n\n"
        "Uses closed-form OLS normal equations instead of np.linalg.lstsq.\n"
        "Returns (None, None, None) if the process is not mean-reverting.",
        py::arg("spreads"), py::arg("dt") = 1.0);
}
