"""Turn a method into a valid StrategySpec.

The synthesizer is the bridge from "a method I understand" to "a spec the engine
can test". It does not decide whether the method works; it expresses it with
explicit costs, sizing, stops and risk so the test is fair.

Some methods are not single-asset rule strategies (cross-sectional momentum,
pairs, carry, ML, options income). For those the synthesizer refuses with a
`requires` message instead of forcing them into an inappropriate DSL.
"""

from __future__ import annotations

from typing import Any, Optional

from ..errors import QuantError
from ..strategy import parse_strategy, validate_strategy
from .taxonomy import family_by_id


def infer_asset_class(symbol: str) -> str:
    text = str(symbol or "").upper()
    if any(x in text for x in ("USDT", "USDC", "BTC", "ETH", "SOL")) or text.endswith("-USD"):
        return "crypto"
    if text.endswith("=F") or text.startswith("GC") or text.startswith("CL"):
        # `SUPPORTED_ASSET_CLASSES` spells this singular ("future"). Returning
        # "futures" made every synthesized futures spec fail validation with
        # UNSUPPORTED_ASSET_CLASS, so the compute path refused a gold Donchian
        # built on GC=F even though the rule set was sound.
        return "future"
    if len(text) == 6 and text.isalpha():
        return "fx"
    return "equity"


def _costs() -> dict[str, Any]:
    return {
        "commission": {"type": "percentage", "value": 0.05},
        "slippage": {"type": "percentage", "value": 0.05},
        "spread": {"type": "bps", "value": 2.0},
    }


def _base(method_id: str, symbol: str, interval: str, name: str,
          indicators: list[dict[str, Any]], rules: dict[str, Any],
          *, allow_short: bool = True, sizing: Optional[dict[str, Any]] = None,
          risk: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "strategy": {
            "id": f"{method_id}_{symbol}".lower().replace("-", "_").replace("/", "_"),
            "name": name,
            "version": "1.0.0",
        },
        "asset_class": infer_asset_class(symbol),
        "universe": {"symbols": [symbol]},
        "timeframes": {"primary": interval},
        "indicators": indicators,
        "rules": rules,
        "position_sizing": sizing or {"type": "percent_equity", "value": 50},
        "risk": risk or {"allow_short": allow_short, "max_risk_per_trade_pct": 2.0},
        "execution": _costs(),
    }


def _ema_cross(params: dict[str, Any]) -> tuple[list, dict, bool]:
    fast = int(params.get("fast", 20))
    slow = int(params.get("slow", 50))
    atr = int(params.get("atr_period", 14))
    indicators = [
        {"id": "ema_fast", "type": "ema", "params": {"period": fast}},
        {"id": "ema_slow", "type": "ema", "params": {"period": slow}},
        {"id": "atr", "type": "atr", "params": {"period": atr}},
    ]
    rules = {
        "entry": {
            "long": {"type": "group", "combine": "all", "conditions": [
                {"left": {"ref": "ema_fast"}, "op": "cross_above", "right": {"ref": "ema_slow"}},
            ]},
            "short": {"type": "group", "combine": "all", "conditions": [
                {"left": {"ref": "ema_fast"}, "op": "cross_below", "right": {"ref": "ema_slow"}},
            ]},
        },
        "exit": {
            "long": {"conditions": [
                {"left": {"ref": "ema_fast"}, "op": "cross_below", "right": {"ref": "ema_slow"}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "ema_fast"}, "op": "cross_above", "right": {"ref": "ema_slow"}},
            ]},
            "stop_loss": {"type": "atr", "value": float(params.get("stop_atr", 2.0))},
            "take_profit": {"type": "risk_reward", "value": float(params.get("target_r", 2.0))},
        },
    }
    return indicators, rules, True


def _donchian(params: dict[str, Any]) -> tuple[list, dict, bool]:
    look = int(params.get("lookback", 20))
    exit_look = int(params.get("exit_lookback", 10))
    atr = int(params.get("atr_period", 14))
    indicators = [
        {"id": "entry_channel", "type": "donchian", "params": {"period": look}},
        {"id": "exit_channel", "type": "donchian", "params": {"period": exit_look}},
        {"id": "atr", "type": "atr", "params": {"period": atr}},
    ]
    rules = {
        "entry": {
            "long": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_above", "right": {"ref": "entry_channel_upper"}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_below", "right": {"ref": "entry_channel_lower"}},
            ]},
        },
        "exit": {
            "long": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_below", "right": {"ref": "exit_channel_lower"}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_above", "right": {"ref": "exit_channel_upper"}},
            ]},
            "stop_loss": {"type": "atr", "value": float(params.get("stop_atr", 2.5))},
        },
    }
    return indicators, rules, True


def _bollinger(params: dict[str, Any]) -> tuple[list, dict, bool]:
    period = int(params.get("period", 20))
    entry_z = float(params.get("entry_z", 2.0))
    atr = int(params.get("atr_period", 14))
    indicators = [
        {"id": "bb", "type": "bollinger", "params": {"period": period, "num_std": entry_z}},
        {"id": "atr", "type": "atr", "params": {"period": atr}},
    ]
    rules = {
        "entry": {
            "long": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_below", "right": {"ref": "bb_lower"}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_above", "right": {"ref": "bb_upper"}},
            ]},
        },
        "exit": {
            "long": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_above", "right": {"ref": "bb_middle"}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_below", "right": {"ref": "bb_middle"}},
            ]},
            "stop_loss": {"type": "atr", "value": float(params.get("stop_atr", 2.0))},
        },
    }
    return indicators, rules, True


def _rsi(params: dict[str, Any]) -> tuple[list, dict, bool]:
    period = int(params.get("period", 14))
    oversold = float(params.get("oversold", 30))
    overbought = float(params.get("overbought", 70))
    atr = int(params.get("atr_period", 14))
    indicators = [
        {"id": "rsi", "type": "rsi", "params": {"period": period}},
        {"id": "atr", "type": "atr", "params": {"period": atr}},
    ]
    rules = {
        "entry": {
            "long": {"conditions": [
                {"left": {"ref": "rsi"}, "op": "cross_below", "right": {"value": oversold}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "rsi"}, "op": "cross_above", "right": {"value": overbought}},
            ]},
        },
        "exit": {
            "long": {"conditions": [
                {"left": {"ref": "rsi"}, "op": "cross_above", "right": {"value": 50}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "rsi"}, "op": "cross_below", "right": {"value": 50}},
            ]},
            "stop_loss": {"type": "atr", "value": float(params.get("stop_atr", 2.0))},
        },
    }
    return indicators, rules, True


def _vol_breakout(params: dict[str, Any]) -> tuple[list, dict, bool]:
    look = int(params.get("breakout_lookback", 20))
    atr = int(params.get("atr_period", 14))
    indicators = [
        {"id": "channel", "type": "donchian", "params": {"period": look}},
        {"id": "atr", "type": "atr", "params": {"period": atr}},
    ]
    rules = {
        "entry": {
            "long": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_above", "right": {"ref": "channel_upper"}},
            ]},
            "short": {"conditions": [
                {"left": {"ref": "close"}, "op": "cross_below", "right": {"ref": "channel_lower"}},
            ]},
        },
        "exit": {
            "stop_loss": {"type": "atr", "value": float(params.get("stop_atr", 2.0))},
            "time_stop_bars": int(params.get("hold_bars", 30)),
        },
    }
    return indicators, rules, True


_BUILDERS = {
    "ema_cross": (_ema_cross, "EMA crossover"),
    "donchian": (_donchian, "Donchian breakout"),
    "bollinger": (_bollinger, "Bollinger mean reversion"),
    "rsi": (_rsi, "RSI exhaustion"),
    "vol_breakout": (_vol_breakout, "Volatility breakout"),
}


def build_strategy(method_id: str, *, symbol: str, interval: str = "1d",
                   params: Optional[dict[str, Any]] = None,
                   name: Optional[str] = None) -> dict[str, Any]:
    family = family_by_id(method_id)
    if family is None:
        raise QuantError(f"unknown method family {method_id!r}")
    builder = _BUILDERS.get(family.builder)
    if builder is None:
        requires = ", ".join(family.requires) or "a research lab"
        raise QuantError(
            f"{family.name} is not a single-asset rule strategy; it needs {requires}. "
            "Use the corresponding lab (factor/ML/options/futures) instead of forcing "
            "it into the rule DSL.",
            detail={"methodId": method_id, "requires": list(family.requires)},
        )
    fn, default_name = builder
    merged = {**family.params, **(params or {})}
    indicators, rules, allow_short = fn(merged)
    return _base(
        method_id, symbol, interval, name or f"{default_name} — {symbol}",
        indicators, rules, allow_short=allow_short,
    )


def synthesize_method(method_id: str, *, symbol: str, interval: str = "1d",
                      params: Optional[dict[str, Any]] = None,
                      name: Optional[str] = None) -> dict[str, Any]:
    """Build AND validate, returning a spec + every validation issue."""
    spec = build_strategy(method_id, symbol=symbol, interval=interval,
                          params=params, name=name)
    issues = validate_strategy(parse_strategy(spec))
    return {
        "methodId": method_id,
        "spec": spec,
        "valid": not issues,
        "issues": [issue.to_dict() if hasattr(issue, "to_dict") else issue
                   for issue in issues],
    }


def build_combination(primary_id: str, filter_id: str, *, symbol: str,
                      interval: str = "1d",
                      primary_params: Optional[dict[str, Any]] = None,
                      filter_params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Compose two single-asset methods as a conjunction of their entry rules.

    The exit and risk come from the primary method. This is a real (if simple)
    way to *create* a new method: a trend entry confirmed by a volatility
    breakout, or a mean-reversion entry filtered by a trend condition.
    """
    primary = build_strategy(primary_id, symbol=symbol, interval=interval,
                             params=primary_params)
    secondary = build_strategy(filter_id, symbol=symbol, interval=interval,
                               params=filter_params)
    ind = {item["id"]: item for item in primary.get("indicators", [])}
    for item in secondary.get("indicators", []):
        if item["id"] in ind and ind[item["id"]] != item:
            item = {**item, "id": f"f_{item['id']}"}
        ind[item["id"]] = item
    # Rename refs in the secondary entry rules when a collision was renamed.
    rename = {}
    for item in secondary.get("indicators", []):
        if item["id"] not in {i["id"] for i in primary.get("indicators", [])}:
            continue
        rename[item["id"]] = f"f_{item['id']}"
    secondary_entry = _rename_refs(secondary.get("rules", {}).get("entry", {}), rename)
    entry = {}
    for side in ("long", "short"):
        a = primary.get("rules", {}).get("entry", {}).get(side)
        b = secondary_entry.get(side)
        if a is None or b is None:
            continue
        entry[side] = {"type": "group", "combine": "all", "conditions": [
            *_group_conditions(a), *_group_conditions(b),
        ]}
    combined = {
        **primary,
        "strategy": {
            **primary["strategy"],
            "id": f"{primary_id}_x_{filter_id}_{symbol}".lower().replace("-", "_").replace("/", "_"),
            "name": f"{primary['strategy']['name']} + {filter_id} filter",
        },
        "indicators": list(ind.values()),
        "rules": {**primary.get("rules", {}), "entry": entry},
    }
    combined["derivedFrom"] = [primary_id, filter_id]
    issues = validate_strategy(parse_strategy(combined))
    if issues:
        raise QuantError("the combination is not a valid strategy",
                         detail={"issues": [i.to_dict() for i in issues]})
    return combined


def _group_conditions(node: Any) -> list[dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    if node.get("type") == "group":
        return list(node.get("conditions") or [])
    return [node]


def _rename_refs(value: Any, rename: dict[str, str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "ref" and isinstance(v, str) and v in rename:
                out[k] = rename[v]
            else:
                out[k] = _rename_refs(v, rename)
        return out
    if isinstance(value, list):
        return [_rename_refs(v, rename) for v in value]
    return value


__all__ = ["build_combination", "build_strategy", "infer_asset_class", "synthesize_method"]
