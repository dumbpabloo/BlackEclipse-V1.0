"""One-action landing with automatic undo.

Trust Mode's backend: apply a run's prepared change, run the landing checks
against the project on disk, and roll the change back when a check fails. The
caller is already operator-authorized; this module does not bypass the path
safety, digest or backup rules in `promotion`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from . import promotion


def _default_verify(files: list[str], root: Path, *, full: bool) -> dict[str, Any]:
    from .land import LandPlan, plan_land_checks, run_land_plan  # noqa: PLC0415

    plan = plan_land_checks(files, root=root)
    if not full:
        plan = LandPlan(
            changed=plan.changed,
            checks=tuple(check for check in plan.checks if check.id != "tests"),
            source=plan.source, warnings=plan.warnings,
        )
    result = run_land_plan(plan, root=root)
    return result.to_dict()


def autoland(
    run_id: str,
    *,
    code_store: Any,
    checkout_root: Optional[Path] = None,
    verify: Optional[Callable[[list[str], Path], dict[str, Any]]] = None,
    full: bool = False,
) -> dict[str, Any]:
    """Apply, verify, and keep or undo in one call.

    Returns `{ok, applied, rolledBack, rollbackId, preview, checks}`. A failed
    check is not an error: the change is already rolled back, and the caller can
    show the failing check to the user. A verify callback that raises is treated
    the same way (the exception is reported, the change is still reverted).
    """
    preview = promotion.build_preview(run_id, code_store=code_store, checkout_root=checkout_root)
    files = [str(item.get("path") or "") for item in (preview.get("files") or [])]
    root = Path(checkout_root or preview.get("checkoutRoot") or ".")
    result = promotion.apply_promotion(preview)
    rollback_id = str(result.get("rollbackId") or "")
    runner = verify or (lambda names, base: _default_verify(names, base, full=full))
    try:
        checks = runner(files, root)
    except Exception as exc:  # noqa: BLE001 - a crash is a failed check, never a pass
        checks = {"ok": False, "failed": ["verification"], "error": str(exc)}
    if not bool(checks.get("ok")):
        undone = False
        if rollback_id:
            try:
                promotion.rollback_promotion(rollback_id, checkout_root=root)
                undone = True
            except promotion.PromotionError:
                undone = False
        return {
            "ok": False,
            "applied": False,
            "rolledBack": undone,
            "rollbackId": rollback_id,
            "preview": promotion.public_preview(preview),
            "checks": checks,
        }
    return {
        "ok": True,
        "applied": True,
        "rolledBack": False,
        "rollbackId": rollback_id,
        "preview": promotion.public_preview(preview),
        "checks": checks,
        "promotion": result,
    }


__all__ = ["autoland"]
