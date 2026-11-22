"""Deterministic technical indicators.

Every function is **causal**: the value at index `i` is computed only from
values at indices `<= i`. The warm-up region is `NaN` (never a fabricated 0) so
a strategy can never act on an indicator that has not "warmed up". This is the
foundation of lookahead-bias safety — the engine additionally only exposes bars
whose close has occurred.

Implemented with numpy for speed. A fresh copy is returned for every call, so a
caller may mutate the result freely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np


class IndicatorError(ValueError):
    pass


@dataclass(frozen=True)
class IndicatorSpec:
    """A declared indicator: `type` + params, optionally a custom `id`."""

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None
    source: Optional[str] = None

    def resolved_id(self) -> str:
        if self.id:
            return self.id
        parts = [self.type]
        for key in sorted(self.params):
            parts.append(str(self.params[key]))
        return "_".join(parts)


# ── Primitive building blocks ────────────────────────────────────────────────

def sma(values: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    if period <= 0:
        raise IndicatorError("sma period must be > 0")
    out = np.full(values.shape, np.nan, dtype=float)
    if values.shape[0] < period:
        return out
    csum = np.cumsum(np.insert(values.astype(float), 0, 0.0))
    out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    if period <= 0:
        raise IndicatorError("ema period must be > 0")
    out = np.full(values.shape, np.nan, dtype=float)
    n = values.shape[0]
    if n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = float(np.mean(values[:period]))
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = alpha * float(values[i]) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (used by RSI and ATR)."""
    period = int(period)
    out = np.full(values.shape, np.nan, dtype=float)
    n = values.shape[0]
    if n < period:
        return out
    alpha = 1.0 / period
    seed = float(np.mean(values[:period]))
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = alpha * float(values[i]) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def wma(values: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    out = np.full(values.shape, np.nan, dtype=float)
    n = values.shape[0]
    if n < period:
        return out
    weights = np.arange(1, period + 1, dtype=float)
    wsum = weights.sum()
    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1].astype(float)
        out[i] = float(np.dot(window, weights) / wsum)
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = high.shape[0]
    tr = np.full(n, np.nan, dtype=float)
    if n == 0:
        return tr
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        prev_close = close[i - 1]
        tr[i] = max(high[i] - low[i], abs(high[i] - prev_close), abs(low[i] - prev_close))
    return tr


def rsi(values: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    n = values.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < period + 1:
        return out
    delta = np.diff(values.astype(float))
    # A missing bar makes its delta unknown: keep the NaN instead of turning it into
    # a zero gain, so the reading stays undefined while the state still carries.
    gains = np.where(np.isnan(delta), np.nan, np.where(delta > 0, delta, 0.0))
    losses = np.where(np.isnan(delta), np.nan, np.where(delta < 0, -delta, 0.0))
    avg_gain = _rma_forward(gains, period)
    avg_loss = _rma_forward(losses, period)
    for i in range(period, n):
        if np.isnan(gains[i - 1]) or np.isnan(losses[i - 1]):
            continue  # this bar's delta is unknown, so the reading is undefined
        g = avg_gain[i - 1]
        l = avg_loss[i - 1]
        if np.isnan(g) or np.isnan(l):
            continue
        if l == 0:
            # Parity with Vibe-Trading's `compute_rsi`: no losses and no gains is
            # UNDEFINED (NaN), not 100. Only "gains, no losses" is 100.
            out[i] = np.nan if g == 0 else 100.0
        else:
            rs = g / l
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    tr = true_range(high, low, close)
    out = rma(tr[1:], period) if tr.shape[0] > 1 else np.full(1, np.nan)
    padded = np.full(high.shape[0], np.nan, dtype=float)
    padded[1:] = out
    return padded


def stddev(values: np.ndarray, period: int, ddof: int = 0) -> np.ndarray:
    period = int(period)
    n = values.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1].astype(float)
        out[i] = float(np.std(window, ddof=ddof))
    return out


def roc(values: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    n = values.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for i in range(period, n):
        base = values[i - period]
        if base != 0:
            out[i] = (values[i] - base) / base * 100.0
    return out


def rolling_max(values: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    n = values.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for i in range(period - 1, n):
        out[i] = float(np.max(values[i - period + 1:i + 1]))
    return out


def rolling_min(values: np.ndarray, period: int) -> np.ndarray:
    period = int(period)
    n = values.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for i in range(period - 1, n):
        out[i] = float(np.min(values[i - period + 1:i + 1]))
    return out


def _nan_rolling_mean(values: np.ndarray, period: int) -> np.ndarray:
    """Rolling mean that stays NaN unless the whole window is defined."""
    period = int(period)
    n = values.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1]
        if not np.isnan(window).any():
            out[i] = float(np.mean(window))
    return out


def _rma_forward(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing that starts at the first defined value (NaN-aware)."""
    period = int(period)
    n = values.shape[0]
    out = np.full(n, np.nan, dtype=float)
    valid = np.where(~np.isnan(values))[0]
    if len(valid) == 0:
        return out
    start = int(valid[0])
    if start + period > n:
        return out
    seed = float(np.mean(values[start:start + period]))
    out[start + period - 1] = seed
    prev = seed
    for i in range(start + period, n):
        v = values[i] if not np.isnan(values[i]) else prev
        prev = (prev * (period - 1) + float(v)) / period
        out[i] = prev
    return out


def stochastic(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int,
               d_period: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Stochastic oscillator: (%K, %D). Range with zero width is NaN."""
    hh = rolling_max(high, period)
    ll = rolling_min(low, period)
    rng = hh - ll
    with np.errstate(invalid="ignore", divide="ignore"):
        k = np.where(rng > 0, (close - ll) / rng * 100.0, np.nan)
    d = _nan_rolling_mean(k, int(d_period))
    return k, d


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        period: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Average Directional Index and the +DI/-DI lines (Wilder)."""
    n = high.shape[0]
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
    atr_line = atr(high, low, close, period)
    plus_s = _rma_forward(plus_dm, period)
    minus_s = _rma_forward(minus_dm, period)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = np.where(atr_line > 0, 100.0 * plus_s / atr_line, np.nan)
        minus_di = np.where(atr_line > 0, 100.0 * minus_s / atr_line, np.nan)
        denom = plus_di + minus_di
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)
    adx_line = _rma_forward(dx, period)
    return adx_line, plus_di, minus_di


def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """On-Balance Volume (cumulative signed volume)."""
    n = close.shape[0]
    out = np.zeros(n, dtype=float)
    for i in range(1, n):
        if close[i] > close[i - 1]:
            out[i] = out[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            out[i] = out[i - 1] - volume[i]
        else:
            out[i] = out[i - 1]
    return out


def mfi(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray,
        period: int) -> np.ndarray:
    """Money Flow Index (volume-weighted RSI of typical price)."""
    n = close.shape[0]
    tp = (high + low + close) / 3.0
    pos = np.zeros(n)
    neg = np.zeros(n)
    for i in range(1, n):
        flow = tp[i] * volume[i]
        if tp[i] > tp[i - 1]:
            pos[i] = flow
        elif tp[i] < tp[i - 1]:
            neg[i] = flow
    out = np.full(n, np.nan, dtype=float)
    for i in range(period, n):
        p = float(np.sum(pos[i - period + 1:i + 1]))
        ng = float(np.sum(neg[i - period + 1:i + 1]))
        out[i] = 100.0 if ng == 0 else 100.0 - 100.0 / (1.0 + p / ng)
    return out


def williams_r(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int) -> np.ndarray:
    """Williams %R (0 to -100)."""
    hh = rolling_max(high, period)
    ll = rolling_min(low, period)
    rng = hh - ll
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(rng > 0, -100.0 * (hh - close) / rng, np.nan)


def cci(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Commodity Channel Index."""
    tp = (high + low + close) / 3.0
    mean = sma(tp, period)
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for i in range(period - 1, n):
        window = tp[i - period + 1:i + 1]
        dev = float(np.mean(np.abs(window - mean[i])))
        out[i] = (tp[i] - mean[i]) / (0.015 * dev) if dev > 0 else np.nan
    return out


# ── Source resolution ────────────────────────────────────────────────────────

_PRICE_SOURCES = {"open", "high", "low", "close", "hl2", "hlc3", "ohlc4"}


def _source_array(source: str, ohlcv: dict[str, np.ndarray]) -> np.ndarray:
    s = (source or "close").lower()
    if s == "open":
        return ohlcv["open"]
    if s == "high":
        return ohlcv["high"]
    if s == "low":
        return ohlcv["low"]
    if s == "close":
        return ohlcv["close"]
    if s == "volume":
        return ohlcv["volume"]
    if s == "hl2":
        return (ohlcv["high"] + ohlcv["low"]) / 2.0
    if s == "hlc3":
        return (ohlcv["high"] + ohlcv["low"] + ohlcv["close"]) / 3.0
    if s == "ohlc4":
        return (ohlcv["open"] + ohlcv["high"] + ohlcv["low"] + ohlcv["close"]) / 4.0
    raise IndicatorError(f"unknown price source {source!r}")


# ── Catalog ──────────────────────────────────────────────────────────────────

# type → (required params, optional params). Used by strategy validation so an
# unknown indicator or a bad parameter is rejected before a run starts.
INDICATOR_CATALOG: dict[str, dict[str, Any]] = {
    "sma": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "ema": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "wma": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "rma": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "rsi": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "atr": {"required": ["period"], "optional": [], "outputs": ["{id}"]},
    "stddev": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "roc": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "highest": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "lowest": {"required": ["period"], "optional": ["source"], "outputs": ["{id}"]},
    "bollinger": {
        "required": ["period"], "optional": ["num_std", "source"],
        "outputs": ["{id}_upper", "{id}_middle", "{id}_lower"],
    },
    "macd": {
        "required": [], "optional": ["fast", "slow", "signal", "source"],
        "outputs": ["{id}", "{id}_signal", "{id}_hist"],
    },
    "vwap": {"required": ["period"], "optional": [], "outputs": ["{id}"]},
    "stochastic": {
        "required": ["period"], "optional": ["d_period"],
        "outputs": ["{id}_k", "{id}_d"],
    },
    "adx": {
        "required": ["period"], "optional": [],
        "outputs": ["{id}", "{id}_plus_di", "{id}_minus_di"],
    },
    "donchian": {
        "required": ["period"], "optional": [],
        "outputs": ["{id}_upper", "{id}_middle", "{id}_lower"],
    },
    "keltner": {
        "required": ["period"], "optional": ["mult", "atr_period"],
        "outputs": ["{id}_upper", "{id}_middle", "{id}_lower"],
    },
    "obv": {"required": [], "optional": [], "outputs": ["{id}"]},
    "mfi": {"required": ["period"], "optional": [], "outputs": ["{id}"]},
    "williams_r": {"required": ["period"], "optional": [], "outputs": ["{id}"]},
    "cci": {"required": ["period"], "optional": [], "outputs": ["{id}"]},
}

_PARAM_DEFAULTS: dict[str, dict[str, Any]] = {
    "stochastic": {"d_period": 3},
    "keltner": {"mult": 2.0, "atr_period": 10},
}


def resolve_spec(spec: IndicatorSpec) -> IndicatorSpec:
    """Fill defaults and normalize an IndicatorSpec (its id + source)."""
    t = spec.type.lower()
    if t not in INDICATOR_CATALOG:
        raise IndicatorError(f"unknown indicator type {spec.type!r}")
    meta = INDICATOR_CATALOG[t]
    for req in meta["required"]:
        if req not in spec.params and not (t == "macd"):
            raise IndicatorError(f"{t} requires parameter {req!r}")
    params = dict(spec.params)
    if t == "macd":
        params.setdefault("fast", 12)
        params.setdefault("slow", 26)
        params.setdefault("signal", 9)
    if t == "bollinger":
        params.setdefault("num_std", 2.0)
    for key, val in _PARAM_DEFAULTS.get(t, {}).items():
        params.setdefault(key, val)
    source = spec.source or params.get("source")
    return IndicatorSpec(type=t, params=params, id=spec.id, source=source)


def compute_one(spec: IndicatorSpec, ohlcv: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Compute one indicator into a dict of output-key → array."""
    s = resolve_spec(spec)
    base = s.resolved_id()
    t = s.type
    p = s.params
    out: dict[str, np.ndarray] = {}
    if t == "sma":
        out[base] = sma(_source_array(s.source or "close", ohlcv), p["period"])
    elif t == "ema":
        out[base] = ema(_source_array(s.source or "close", ohlcv), p["period"])
    elif t == "wma":
        out[base] = wma(_source_array(s.source or "close", ohlcv), p["period"])
    elif t == "rma":
        out[base] = rma(_source_array(s.source or "close", ohlcv), p["period"])
    elif t == "rsi":
        out[base] = rsi(_source_array(s.source or "close", ohlcv), p["period"])
    elif t == "atr":
        out[base] = atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], p["period"])
    elif t == "stddev":
        out[base] = stddev(_source_array(s.source or "close", ohlcv), p["period"])
    elif t == "roc":
        out[base] = roc(_source_array(s.source or "close", ohlcv), p["period"])
    elif t == "highest":
        out[base] = rolling_max(_source_array(s.source or "high", ohlcv), p["period"])
    elif t == "lowest":
        out[base] = rolling_min(_source_array(s.source or "low", ohlcv), p["period"])
    elif t == "bollinger":
        src = _source_array(s.source or "close", ohlcv)
        mid = sma(src, p["period"])
        sd = stddev(src, p["period"])
        k = float(p.get("num_std", 2.0))
        out[f"{base}_middle"] = mid
        out[f"{base}_upper"] = mid + k * sd
        out[f"{base}_lower"] = mid - k * sd
    elif t == "macd":
        src = _source_array(s.source or "close", ohlcv)
        fast = ema(src, p["fast"])
        slow = ema(src, p["slow"])
        macd_line = fast - slow
        # Seed the signal EMA only on the valid MACD segment; zero-filling the
        # warm-up would bias the signal line (and the histogram) toward zero for
        # the first `signal` valid bars.
        signal = np.full(macd_line.shape, np.nan, dtype=float)
        valid = ~np.isnan(macd_line)
        if valid.any():
            first = int(np.argmax(valid))
            signal[first:] = ema(macd_line[first:], p["signal"])
        out[base] = macd_line
        out[f"{base}_signal"] = signal
        out[f"{base}_hist"] = macd_line - signal
    elif t == "vwap":
        period = int(p["period"])
        tp = (ohlcv["high"] + ohlcv["low"] + ohlcv["close"]) / 3.0
        vol = ohlcv["volume"]
        n = tp.shape[0]
        out[base] = np.full(n, np.nan, dtype=float)
        for i in range(period - 1, n):
            v = vol[i - period + 1:i + 1]
            pv = tp[i - period + 1:i + 1]
            tot = float(np.sum(v))
            if tot > 0:
                out[base][i] = float(np.sum(pv * v) / tot)
            else:
                out[base][i] = float(np.mean(pv))
    elif t == "stochastic":
        k, d = stochastic(ohlcv["high"], ohlcv["low"], ohlcv["close"], p["period"],
                          p.get("d_period", 3))
        out[f"{base}_k"] = k
        out[f"{base}_d"] = d
    elif t == "adx":
        adx_line, plus_di, minus_di = adx(ohlcv["high"], ohlcv["low"], ohlcv["close"], p["period"])
        out[base] = adx_line
        out[f"{base}_plus_di"] = plus_di
        out[f"{base}_minus_di"] = minus_di
    elif t == "donchian":
        up = rolling_max(ohlcv["high"], p["period"])
        lo = rolling_min(ohlcv["low"], p["period"])
        out[f"{base}_upper"] = up
        out[f"{base}_lower"] = lo
        out[f"{base}_middle"] = (up + lo) / 2.0
    elif t == "keltner":
        mid = ema(_source_array(s.source or "close", ohlcv), p["period"])
        atr_line = atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], int(p.get("atr_period", 10)))
        band = float(p.get("mult", 2.0)) * atr_line
        out[f"{base}_middle"] = mid
        out[f"{base}_upper"] = mid + band
        out[f"{base}_lower"] = mid - band
    elif t == "obv":
        out[base] = obv(ohlcv["close"], ohlcv["volume"])
    elif t == "mfi":
        out[base] = mfi(ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"], p["period"])
    elif t == "williams_r":
        out[base] = williams_r(ohlcv["high"], ohlcv["low"], ohlcv["close"], p["period"])
    elif t == "cci":
        out[base] = cci(ohlcv["high"], ohlcv["low"], ohlcv["close"], p["period"])
    else:  # pragma: no cover - resolve_spec already rejects unknown types
        raise IndicatorError(f"unhandled indicator {t!r}")
    return out


def compute_indicators(
    specs: list[IndicatorSpec], ohlcv: dict[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """Compute every declared indicator; keys are the resolvable output names.

    A duplicate output key is an error (ambiguous reference) — rejected rather
    than silently overwritten.
    """
    result: dict[str, np.ndarray] = {}
    for spec in specs:
        for key, arr in compute_one(spec, ohlcv).items():
            if key in result:
                raise IndicatorError(f"duplicate indicator output {key!r}")
            result[key] = arr
    return result


def output_keys(spec: IndicatorSpec) -> list[str]:
    """The exact output keys an indicator will produce (for rule validation)."""
    s = resolve_spec(spec)
    base = s.resolved_id()
    return [tpl.format(id=base) for tpl in INDICATOR_CATALOG[s.type]["outputs"]]


__all__ = [
    "IndicatorError",
    "IndicatorSpec",
    "INDICATOR_CATALOG",
    "adx",
    "atr",
    "cci",
    "compute_indicators",
    "compute_one",
    "ema",
    "mfi",
    "obv",
    "output_keys",
    "resolve_spec",
    "rma",
    "roc",
    "rolling_max",
    "rolling_min",
    "rsi",
    "sma",
    "stddev",
    "stochastic",
    "true_range",
    "williams_r",
    "wma",
]
