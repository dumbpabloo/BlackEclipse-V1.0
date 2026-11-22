"""The input contract for zoo factors that need data OHLCV does not carry.

Some published alphas use **vwap**, which cannot be derived from open/high/low/
close without fabricating it: the usual `(high + low + close) / 3` is a typical
price, not a volume-weighted average, and substituting it would make the factor's
IC look real while measuring something else.

So the contract is explicit:

  * a factor that needs it declares `requiresInputs: ["vwap"]`;
  * `require_inputs(factor, fields)` raises `MISSING_INPUT` naming what is
    missing — a refusal, not a silent NaN column;
  * `missing_inputs(factor, fields)` is the non-raising form a UI can use to grey
    a factor out and say *why*.

This keeps "available" honest: an alpha is runnable when its declared inputs are
present, and the caller learns which input it must supply.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional


class FactorInputError(ValueError):
    """A factor cannot run on the supplied inputs, with a stable `code`."""

    def __init__(self, code: str, message: str, *, missing: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing = tuple(missing)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "missing": list(self.missing)}


def required_inputs(factor: Mapping[str, Any]) -> tuple[str, ...]:
    """The inputs a factor declares beyond the OHLCV surface."""
    declared = factor.get("requiresInputs") or ()
    if isinstance(declared, str):
        declared = (declared,)
    return tuple(str(name) for name in declared)


def missing_inputs(factor: Mapping[str, Any], available: Iterable[str]) -> list[str]:
    """Declared inputs that `available` does not provide, in declaration order."""
    have = {str(name) for name in available}
    # A factor's own `inputs` list is what its expression reads; a declared
    # requirement that the expression does not use would be a metadata bug, so
    # both sets are honoured rather than trusting one.
    return [name for name in required_inputs(factor) if name not in have]


def require_inputs(factor: Mapping[str, Any], available: Iterable[str]) -> None:
    """Raise `MISSING_INPUT` when a declared input is absent."""
    missing = missing_inputs(factor, available)
    if missing:
        raise FactorInputError(
            "MISSING_INPUT",
            f"{factor.get('name') or 'factor'} needs {', '.join(missing)}, which the "
            "caller must supply (it is not derivable from OHLCV)",
            missing=missing,
        )


def describe(factor: Mapping[str, Any], available: Iterable[str]) -> dict[str, Any]:
    """A small, serialisable statement of what this factor needs and has."""
    missing = missing_inputs(factor, available)
    return {
        "name": factor.get("name"),
        "requiresInputs": list(required_inputs(factor)),
        "runnable": not missing,
        "missing": missing,
        "reason": (
            f"needs {', '.join(missing)}" if missing else "inputs satisfied"
        ),
    }


#: The status vocabulary a field can carry. `failed` and `unknown` are different
#: facts: a REQUIRED input that is absent failed, an optional one that is absent is
#: merely unknown — and neither may be presented as usable.
FIELD_STATUSES = ("verified", "assumed", "unknown", "failed")

#: Fields whose availability mode is a bar close rather than a filing/effective date.
_BAR_FIELDS = frozenset({"open", "high", "low", "close", "volume", "amount", "vwap"})


def availability_mode(field: str) -> str:
    """When a field can be known, by its own kind.

    Parity with Vibe-Trading's `factors/data_availability.py` field policy: a bar is
    known at its close, a fundamental at its filing date, a valuation at the session
    date, and universe membership at an effective date.
    """
    if field == "universe:membership" or field == "sector":
        return "effective_date"
    if field.startswith("fund:"):
        return "filing_date"
    if field.startswith("val:"):
        return "session_date"
    if field in _BAR_FIELDS:
        return "bar_close"
    return "unknown"


def field_availability(
    fields: Iterable[str],
    *,
    present: Iterable[str] = (),
    required: Iterable[str] = (),
    sources: Optional[Mapping[str, str]] = None,
    explicit: Optional[Mapping[str, str]] = None,
    pit_membership: bool = False,
) -> list[dict[str, Any]]:
    """Per-field status and point-in-time safety, in a stable order.

    The decision order is the reference's, and the order matters:

      1. an explicit status wins over anything inferred;
      2. **absent** → ``failed`` when the field was required, else ``unknown``;
      3. a **fundamental** with a filing date but no publication timestamp stays
         ``unknown`` — a one-bar shift is useful protection, not proof, so claiming
         ``verified`` there would overstate what the data supports;
      4. universe membership is ``verified`` only with a point-in-time flag;
      5. a field with a named source is ``assumed``;
      6. anything else is ``unknown``.

    ``pitSafe`` is true only for ``verified``: the rest is exactly the data that
    looks available and is not safe to treat as known-at-the-time.
    """
    have = {str(name) for name in present}
    need = {str(name) for name in required}
    source_map = dict(sources or {})
    explicit_map = dict(explicit or {})

    out: list[dict[str, Any]] = []
    for raw in fields:
        field = str(raw)
        is_present = field == "universe:membership" or field in have
        source = source_map.get(field)
        status = explicit_map.get(field)
        reason: Optional[str] = None

        if status not in FIELD_STATUSES:
            if not is_present:
                status = "failed" if field in need else "unknown"
            elif field.startswith("fund:"):
                status = "unknown"
                reason = (
                    "source exposes a filing date but no precise publication "
                    "timestamp"
                )
            elif field == "universe:membership":
                status = "verified" if pit_membership else "unknown"
            elif source:
                status = "assumed"
            else:
                status = "unknown"

        record = {
            "field": field,
            "status": status,
            "pitSafe": status == "verified",
            "availabilityMode": availability_mode(field),
            "source": source,
        }
        if reason:
            record["reason"] = reason
        out.append(record)
    return out


__all__ = [
    "FIELD_STATUSES",
    "FactorInputError",
    "availability_mode",
    "describe",
    "field_availability",
    "missing_inputs",
    "require_inputs",
    "required_inputs",
]
