"""Multiple-testing and overfitting diagnostics.

These answer a question the ordinary Sharpe ratio cannot: *given how many
configurations were tried, is the selected result better than what the best of
that many random tries would produce anyway?*

Two standard, referenced methods are implemented from first principles (no
SciPy):

  * **Deflated Sharpe Ratio** (Bailey & López de Prado, 2014) — the probability
    that the selected strategy's Sharpe is positive after deflating for the
    number of independent trials and the non-normality (skew/kurtosis) of returns.
  * **Probability of Backtest Overfitting** via Combinatorially Symmetric
    Cross-Validation (Bailey, Borwein, López de Prado & Zhu, 2016) — over all
    balanced IS/OOS partitions of the return matrix, how often the in-sample best
    configuration lands in the bottom half out-of-sample.

Both are only computed when their required inputs exist. Missing inputs yield a
typed `available: false` with a reason — never a fabricated statistic. Neither is
"applied mechanically": the interpretation states its assumptions and limits.
"""

from __future__ import annotations

import itertools
import math
from typing import Any, Optional

import numpy as np

from .errors import QuantError

_EULER_GAMMA = 0.5772156649015329


class MultipleTestingError(QuantError):
    code = "MULTIPLE_TESTING_ERROR"


# ── Normal distribution helpers (no SciPy) ───────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation)."""
    if not (0.0 < p < 1.0):
        raise MultipleTestingError("norm_ppf domain is (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def _sharpe(values: np.ndarray) -> float:
    if values.size < 2:
        return 0.0
    sd = float(np.std(values, ddof=1))
    return float(np.mean(values) / sd) if sd > 0 else 0.0


# ── Deflated Sharpe Ratio ────────────────────────────────────────────────────

def deflated_sharpe(
    returns: list[float],
    *,
    trial_sharpes: Optional[list[float]] = None,
    trial_count: Optional[int] = None,
    sr_variance: Optional[float] = None,
) -> dict[str, Any]:
    """Deflated Sharpe Ratio for a selected return series.

    Requires the variance of the trial Sharpe ratios — supplied either as
    `trial_sharpes` (per-period SRs across the trials) or directly as
    `sr_variance` + `trial_count`. Without it the DSR is `available: false`.
    All Sharpes are per-period (non-annualized); annualization cancels in the
    deflation formula and must not be mixed in.
    """
    r = np.asarray([float(x) for x in returns], dtype=float)
    if r.size < 3:
        return {"available": False,
                "reason": f"need >= 3 return observations, got {r.size}"}

    if trial_sharpes is not None and len(trial_sharpes) >= 2:
        srs = np.asarray([float(x) for x in trial_sharpes], dtype=float)
        v = float(np.var(srs, ddof=1))
        n = int(srs.size)
    elif sr_variance is not None and trial_count is not None and trial_count >= 2:
        v = float(sr_variance)
        n = int(trial_count)
    else:
        return {"available": False,
                "reason": "deflation needs the trial Sharpe variance — pass "
                          "`trialSharpes` or `srVariance`+`trialCount`"}

    if v <= 0:
        return {"available": False,
                "reason": "trial Sharpe variance is zero; deflation is undefined"}

    sr = _sharpe(r)
    t = int(r.size)
    skew = float(_skew(r))
    kurt = float(_kurtosis(r))  # non-excess (normal = 3)
    denom_term = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    if denom_term <= 0:
        return {"available": False,
                "reason": "the DSR variance term is non-positive for these moments"}

    sr0 = math.sqrt(v) * (
        (1.0 - _EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n)
        + _EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n * math.e))
    )
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom_term)
    dsr = norm_cdf(z)
    return {
        "available": True,
        "deflatedSharpeRatio": dsr,
        "probabilitySharpePositive": dsr,
        "selectedSharpe": sr,
        "expectedMaxSharpe": sr0,
        "trialCount": n,
        "trialSharpeVariance": v,
        "observations": t,
        "skew": skew,
        "kurtosis": kurt,
        "method": "deflated_sharpe_bailey_lopez_de_prado_2014",
        "interpretation": (
            f"After accounting for {n} trials, the probability the strategy's true "
            f"Sharpe exceeds the expected best-of-{n} is {dsr:.3f}. Values below ~0.95 "
            "mean the result is not distinguishable from selection luck."
        ),
        "assumptions": [
            "per-period (non-annualized) Sharpe ratios",
            "trials are independent / the SR variance reflects the search width",
            "returns' skew/kurtosis are estimated from the same sample (IID caveat)",
        ],
    }


#: Fewest observations for which skew/kurtosis-corrected statistics mean anything.
#: The sqrt(T-1) scaling is not the binding constraint; the moment estimates are.
MIN_OBSERVATIONS = 30

#: Fourth standardised moment of a Gaussian. NOT zero: the Bailey-Lopez de Prado
#: variance term uses the non-excess form (3 for a normal), and passing an excess
#: kurtosis makes the confidence too high.
GAUSSIAN_KURTOSIS = 3.0


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    n_observations: int,
    benchmark_sharpe: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = GAUSSIAN_KURTOSIS,
) -> float:
    """Confidence that a strategy's true Sharpe exceeds a benchmark.

    Parity with Vibe-Trading's `quantlib/multipletesting.py`:

        PSR = Phi[ (SR - SR*) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4-1)/4*SR^2) ]

    Negative skew and fat tails both make a given Sharpe less trustworthy, which
    is why this is not a plain t-test. Sharpe must be **per observation** and
    matched to ``n_observations``: an annualised Sharpe with a daily ``T``
    inflates the statistic by roughly sqrt(252).

    Raises ``MultipleTestingError`` on a non-finite input, fewer than
    :data:`MIN_OBSERVATIONS` observations, a kurtosis below 1 (impossible for any
    distribution, and the signature of an excess-kurtosis caller), or a
    non-positive variance term (mutually inconsistent moments).
    """
    for name, value in (("observed_sharpe", observed_sharpe),
                        ("n_observations", n_observations),
                        ("benchmark_sharpe", benchmark_sharpe),
                        ("skew", skew), ("kurtosis", kurtosis)):
        if not math.isfinite(float(value)):
            raise MultipleTestingError(f"{name} must be finite, got {value!r}")
    if n_observations < MIN_OBSERVATIONS:
        raise MultipleTestingError(
            f"need at least {MIN_OBSERVATIONS} observations, got {n_observations}"
        )
    if kurtosis < 1.0:
        raise MultipleTestingError(
            f"kurtosis must be the non-excess fourth moment (>= 1, 3 for a "
            f"Gaussian), got {kurtosis}; excess kurtosis was probably passed"
        )
    variance_term = (1.0 - skew * observed_sharpe
                     + (kurtosis - 1.0) / 4.0 * observed_sharpe ** 2)
    if variance_term <= 0.0:
        raise MultipleTestingError(
            f"the Sharpe variance term is {variance_term:.4f}, which is not "
            "positive; the supplied skew and kurtosis are inconsistent with this Sharpe"
        )
    z = ((observed_sharpe - benchmark_sharpe) * math.sqrt(n_observations - 1)
         / math.sqrt(variance_term))
    return norm_cdf(z)


def benjamini_hochberg(p_values: list[float], *, fdr: float = 0.05) -> dict[str, Any]:
    """Benjamini-Hochberg false-discovery-rate control (step-up).

    Parity with the reference's ``benjamini_hochberg``. Screening hundreds of
    factors with Bonferroni is far too strict; BH bounds the expected *fraction*
    of rejections that are false, which is the quantity a research pipeline
    actually cares about.

    Returned in the caller's order: ``rejected`` is a list of booleans,
    ``adjustedPValues`` is the monotone-non-decreasing adjusted p-value array
    (running minimum from the largest rank down, clipped at 1), ``threshold`` is
    the largest raw p-value rejected (0.0 when none), and ``nRejected`` counts.
    """
    if not (0.0 < fdr < 1.0):
        raise MultipleTestingError(f"fdr must be in (0, 1), got {fdr}")
    values = np.asarray([float(x) for x in p_values], dtype=float).ravel()
    if values.size == 0:
        raise MultipleTestingError("p_values is empty")
    if not np.isfinite(values).all():
        raise MultipleTestingError("p_values holds a non-finite value")
    if ((values < 0.0) | (values > 1.0)).any():
        raise MultipleTestingError("every p-value must lie in [0, 1]")

    n = values.size
    order = np.argsort(values, kind="stable")
    sorted_p = values[order]
    ranks = np.arange(1, n + 1)
    below = sorted_p <= (ranks / n) * fdr
    if below.any():
        cutoff_rank = int(ranks[below].max())
        threshold = float(sorted_p[cutoff_rank - 1])
    else:
        cutoff_rank = 0
        threshold = 0.0

    adjusted_sorted = np.minimum.accumulate((n / ranks * sorted_p)[::-1])[::-1]
    adjusted_sorted = np.clip(adjusted_sorted, 0.0, 1.0)
    rejected_sorted = ranks <= cutoff_rank

    rejected = np.empty(n, dtype=bool)
    adjusted = np.empty(n, dtype=float)
    rejected[order] = rejected_sorted
    adjusted[order] = adjusted_sorted
    return {
        "rejected": [bool(x) for x in rejected],
        "adjustedPValues": [float(x) for x in adjusted],
        "nRejected": int(rejected.sum()),
        "threshold": threshold,
        "fdr": fdr,
    }


def _skew(x: np.ndarray) -> float:
    n = x.size
    if n < 2:
        return 0.0
    m = float(np.mean(x))
    s = float(np.std(x, ddof=0))
    if s == 0:
        return 0.0
    return float(np.mean(((x - m) / s) ** 3))


def _kurtosis(x: np.ndarray) -> float:
    n = x.size
    if n < 2:
        return 3.0
    m = float(np.mean(x))
    s = float(np.std(x, ddof=0))
    if s == 0:
        return 3.0
    return float(np.mean(((x - m) / s) ** 4))


# ── Probability of Backtest Overfitting (CSCV) ───────────────────────────────

def pbo_cscv(returns_matrix: list[list[float]], *, n_splits: int = 16) -> dict[str, Any]:
    """Probability of Backtest Overfitting via CSCV.

    `returns_matrix` is `N` configuration return series, each of length `T`
    (configs x periods). The periods are split into `n_splits` disjoint blocks;
    for every balanced half used as in-sample, the in-sample best configuration's
    out-of-sample rank decides a win/loss. PBO is the fraction of partitions where
    that configuration lands in the worse half out-of-sample.
    """
    if not returns_matrix or len(returns_matrix) < 2:
        return {"available": False, "reason": "PBO needs >= 2 configurations"}
    lengths = {len(row) for row in returns_matrix}
    if len(lengths) != 1:
        return {"available": False, "reason": "all configuration series must share one length"}
    t = lengths.pop()
    if t < 4:
        return {"available": False, "reason": f"need >= 4 periods, got {t}"}
    n_configs = len(returns_matrix)
    s = int(n_splits)
    if s % 2:
        s += 1  # CSCV needs an even number of blocks
    s = max(2, min(s, t if t % 2 == 0 else t - 1))
    if s < 2:
        return {"available": False, "reason": "not enough periods to form blocks"}

    mat = np.asarray(returns_matrix, dtype=float)  # N x T
    # Split T periods into s contiguous blocks of near-equal size.
    bounds = np.linspace(0, t, s + 1).astype(int)
    blocks = [mat[:, bounds[i]:bounds[i + 1]] for i in range(s)]

    combos = list(itertools.combinations(range(s), s // 2))
    overfit = 0
    logits: list[float] = []
    for chosen in combos:
        is_idx = list(chosen)
        oos_idx = [b for b in range(s) if b not in chosen]
        is_ret = np.concatenate([blocks[b] for b in is_idx], axis=1)
        oos_ret = np.concatenate([blocks[b] for b in oos_idx], axis=1)
        is_perf = np.array([_sharpe(is_ret[i]) for i in range(n_configs)])
        oos_perf = np.array([_sharpe(oos_ret[i]) for i in range(n_configs)])
        best = int(np.argmax(is_perf))
        # Average-rank of the IS-best config out-of-sample (1 = worst).
        less = int(np.sum(oos_perf < oos_perf[best]))
        equal = int(np.sum(oos_perf == oos_perf[best]))
        rank = less + (equal + 1) / 2.0
        omega = rank / (n_configs + 1)
        if omega <= 0.5:
            overfit += 1
        logits.append(math.log(omega / (1.0 - omega)))

    pbo = overfit / len(combos)
    return {
        "available": True,
        "probabilityOfBacktestOverfitting": pbo,
        "combinations": len(combos),
        "configurations": n_configs,
        "periods": t,
        "splits": s,
        "method": "cscv_bailey_borwein_lopez_de_prado_zhu_2016",
        "performanceStatistic": "sharpe",
        "logitSummary": {
            "mean": float(np.mean(logits)),
            "p5": float(np.percentile(logits, 5)),
            "p95": float(np.percentile(logits, 95)),
        },
        "interpretation": (
            f"{pbo * 100:.1f}% of balanced in-sample/out-of-sample partitions put the "
            "in-sample-best configuration in the worse out-of-sample half. High PBO means "
            "the selection does not generalise."
        ),
        "assumptions": [
            "configurations are compared on the SAME periods",
            "Sharpe is the selection/performance statistic",
            "contiguous time blocks preserve local serial structure within a block",
        ],
    }


__all__ = [
    "MultipleTestingError",
    "GAUSSIAN_KURTOSIS",
    "MIN_OBSERVATIONS",
    "benjamini_hochberg",
    "deflated_sharpe",
    "norm_cdf",
    "norm_ppf",
    "pbo_cscv",
    "probabilistic_sharpe_ratio",
]
