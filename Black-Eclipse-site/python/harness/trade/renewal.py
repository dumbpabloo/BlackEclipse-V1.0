"""Keep an unattended bot's bounded authority alive, without widening it.

`docs/trading.md` and the `bot_trading` router both promise that, in auto mode, a
bot renews its own mandate while its standing consent is valid. That promise had
no implementation: a live arm died after eight hours and a mandate after one day,
so "autonomous" trading silently stopped until a human re-armed it.

This module is that promise, and nothing more:

  * it only EXTENDS an authorization the operator already granted;
  * the live arm is extended only while it is still valid - a gap (machine off,
    app closed) needs a human, so a stale arm is never re-armed by itself;
  * a replacement mandate keeps the SAME bot pin and is clamped DOWN to the ramp
    stage the bot's real live results have earned, never up;
  * the exhausted/expired mandate is revoked only after the replacement exists,
    so a failed renewal never leaves the bot with no authority;
  * every renewal is written to the connection audit, and `authority_status`
    reports a countdown the UI can show.

It never places, sizes, modifies or closes an order, and it never touches a
strategy trial other than to point it at the renewed mandate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .mandates import DEFAULT_TTL_SECONDS, LIVE_MAX_TTL_SECONDS
from .model import ConnectionMode, TradeRefused

#: The broker's own arm ceiling (`BrokerService.arm_connection` clamps to this).
MAX_ARM_TTL_SECONDS = 28_800
#: Renew an arm with less than this much life left (the UI arms for 30 minutes,
#: so the first entry after a manual arm normally refreshes it straight away).
ARM_REFRESH_SECONDS = 3_600
#: Renew a mandate with less than this much life left.
MANDATE_REFRESH_SECONDS = 2 * 3_600
#: Renew a mandate with this few orders left, even if it is not near expiry.
MIN_REMAINING_ORDERS = 1


def _parse(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _seconds_left(value: Any) -> Optional[int]:
    moment = _parse(value)
    if moment is None:
        return None
    return int((moment - datetime.now(timezone.utc)).total_seconds())


def _future(value: Any) -> bool:
    left = _seconds_left(value)
    return left is not None and left > 0


def _mandate_for(service: Any, connection_id: str, mandate_id: str) -> Optional[dict[str, Any]]:
    if not mandate_id:
        return None
    try:
        rows = service.list_mandates(connection_id, limit=200)
    except Exception:  # noqa: BLE001 - an unreadable list is "no mandate"
        return None
    return next((row for row in rows if str(row.get("id")) == str(mandate_id)), None)


def authority_status(service: Any, live: Any, trial: dict[str, Any]) -> dict[str, Any]:
    """Read-only view of the arm + mandate that back one trial.

    Never invents a number: a missing/unreadable arm or mandate is `None` with a
    reason, so the UI can say "not readable" instead of "fine".
    """
    mode = str(trial.get("mode") or "live")
    connection_id = str(trial.get("connectionId") or "")
    status: dict[str, Any] = {
        "available": True,
        "mode": mode,
        "connectionId": connection_id,
        "armedUntil": None,
        "armSecondsLeft": None,
        "mandateId": trial.get("mandateId") or None,
        "mandateExpiresAt": None,
        "mandateSecondsLeft": None,
        "ordersRemaining": None,
        "maxOrders": None,
        "needsArmRenewal": False,
        "needsMandateRenewal": False,
    }
    if mode == "shadow" or not connection_id:
        status["available"] = False
        status["reason"] = "shadow" if mode == "shadow" else "no connection"
        return status

    try:
        connection = service.get_connection(connection_id)
    except Exception as exc:  # noqa: BLE001
        status["available"] = False
        status["reason"] = f"connection unreadable: {exc}"
        return status

    armed_until = connection.get("armedUntil")
    status["armedUntil"] = armed_until
    status["armSecondsLeft"] = _seconds_left(armed_until)
    if mode == "live":
        status["needsArmRenewal"] = not _future(armed_until) or (
            int(status["armSecondsLeft"] or 0) <= ARM_REFRESH_SECONDS
        )

    mandate = _mandate_for(service, connection_id, str(trial.get("mandateId") or ""))
    if mandate is None:
        status["needsMandateRenewal"] = True
        return status

    status["mandateExpiresAt"] = mandate.get("expiresAt")
    status["mandateSecondsLeft"] = _seconds_left(mandate.get("expiresAt"))
    status["maxOrders"] = mandate.get("maxOrders")
    status["ordersRemaining"] = mandate.get("remainingOrders")
    remaining = int(mandate.get("remainingOrders") or 0)
    status["needsMandateRenewal"] = bool(
        mandate.get("revokedAt")
        or int(status["mandateSecondsLeft"] or 0) <= MANDATE_REFRESH_SECONDS
        or remaining <= MIN_REMAINING_ORDERS
    )
    return status


def renew_arm(
    service: Any,
    connection_id: str,
    *,
    ttl_seconds: int = MAX_ARM_TTL_SECONDS,
    refresh_seconds: int = ARM_REFRESH_SECONDS,
) -> dict[str, Any]:
    """Extend a *currently valid* live arm. Never resurrects an expired one.

    An expired arm is an operator checkpoint, not a bug: the app may have been
    closed past the window, and silently re-arming would erase that boundary.
    """
    connection = service._connection(connection_id)  # noqa: SLF001 - audited seam
    if connection.mode != ConnectionMode.LIVE:
        return {"renewed": False, "reason": "not-live", "armedUntil": None}
    if int(service._magic(connection)) <= 0:  # noqa: SLF001
        raise TradeRefused(
            "LIVE_MAGIC_REQUIRED",
            "set a non-zero magic on the connection before renewing a live arm",
            status_code=400,
        )
    if float(connection.config.get("maxMargin") or 0) <= 0:
        raise TradeRefused(
            "LIVE_CAP_REQUIRED",
            "set a positive maxMargin on the connection before renewing a live arm",
            status_code=400,
        )
    armed_until = connection.armed_until
    left = _seconds_left(armed_until)
    if left is None or left <= 0:
        return {
            "renewed": False,
            "reason": "ARM_EXPIRED",
            "armedUntil": armed_until,
            "message": "the live arm expired; arm the connection again to resume",
        }
    if left > int(refresh_seconds):
        return {"renewed": False, "reason": "not-due", "armedUntil": armed_until}

    ttl = max(60, min(int(ttl_seconds or MAX_ARM_TTL_SECONDS), MAX_ARM_TTL_SECONDS))
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat().replace(
        "+00:00", "Z"
    )
    service.connections.set_arm(connection.id, until=until, by="auto-renew")
    service.connections.record_event(
        connection.id,
        "arm",
        "renewed",
        {"until": until, "ttlSeconds": ttl},
    )
    return {"renewed": True, "armedUntil": until, "ttlSeconds": ttl}


def _live_caps(
    service: Any, definition: Any, automation_id: str, trial: dict[str, Any], mode: str,
) -> tuple[int, float]:
    """Clamp a renewed LIVE mandate to the stage the bot has actually earned."""
    max_orders = max(1, int(trial.get("maxOrders") or 1))
    max_margin = float(trial.get("maxMargin") or 0)
    if mode != "live" or definition is None or not automation_id:
        return max_orders, max_margin
    try:
        from .ramp import live_evidence, ramp_limits  # noqa: PLC0415 - avoids a cycle

        limits = ramp_limits(definition, live_evidence(service, automation_id))
        max_orders = max(1, min(max_orders, int(limits.get("maxOrders") or 1)))
        if max_margin > 0:
            max_margin = min(max_margin, float(limits.get("maxMargin") or max_margin))
    except Exception:  # noqa: BLE001 - creation re-checks; never widen on an error
        pass
    return max_orders, max_margin


def ensure_authority(
    service: Any,
    live: Any,
    trial_id: str,
    *,
    automation_id: str = "",
    bot_version: Optional[int] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Renew the arm/mandate backing one trial when they are due.

    Called only on the entry path, so a no-signal bar costs nothing. Raises the
    broker's own typed refusal when a renewal is needed but not permitted (the
    ordinary gates - tier, proof, ramp, arm - still decide).
    """
    trial = live.get(trial_id)
    mode = str(trial.get("mode") or "live")
    if mode == "shadow" or not str(trial.get("connectionId") or ""):
        return {"renewed": False, "reason": "shadow" if mode == "shadow" else "no connection"}

    # An edit to the bot BUMPS its version and must invalidate the grant. The
    # runner refuses the order with BOT_VERSION_STALE; renewal must refuse too,
    # instead of minting a mandate pinned to the new version behind the
    # operator's back. The operator re-grants through the normal prepare form.
    pinned_version = trial.get("botVersion")
    if (
        str(trial.get("botId") or "")
        and bot_version is not None
        and pinned_version is not None
        and int(pinned_version) != int(bot_version)
    ):
        raise TradeRefused(
            "BOT_VERSION_STALE",
            "this bot changed since its trading authority was granted; "
            "re-grant trading authority",
            status_code=409,
        )

    connection_id = str(trial["connectionId"])
    arm: Optional[dict[str, Any]] = None
    if mode == "live":
        arm = renew_arm(service, connection_id)
        if force and arm.get("reason") == "ARM_EXPIRED":
            # A manual "renew now" must say why nothing happened instead of
            # reporting success while the arm is still dead.
            raise TradeRefused(
                "LIVE_ARM_EXPIRED",
                str(arm.get("message") or "the live arm expired; arm the connection again"),
                status_code=409,
            )

    before = authority_status(service, live, trial)
    if not force and not before.get("needsMandateRenewal"):
        return {"renewed": False, "arm": arm, "status": before}

    connection = service.get_connection(connection_id)
    label = str(connection.get("label") or "")
    max_orders, max_margin = _live_caps(
        service, _definition(automation_id), automation_id, trial, mode,
    )
    ttl = LIVE_MAX_TTL_SECONDS if mode == "live" else DEFAULT_TTL_SECONDS
    payload: dict[str, Any] = {
        "mode": mode,
        "symbols": list(trial.get("symbols") or ["*"]),
        "maxOrders": max_orders,
        "maxMargin": max_margin,
        "ttlSeconds": ttl,
        "confirmation": f"MANDATE {mode.upper()} {label}",
        "note": "auto-renewed while the standing consent is valid",
    }
    pinned_bot = str(trial.get("botId") or "")
    if pinned_bot:
        payload["botId"] = pinned_bot
        payload["botVersion"] = bot_version if bot_version is not None else trial.get("botVersion")
    else:
        payload["workflowId"] = str(trial.get("pin") or "")
        payload["workflowVersion"] = 1

    previous_id = str(trial.get("mandateId") or "")
    mandate = service.create_mandate(connection_id, payload)
    # Point the trial at the replacement BEFORE revoking the old grant, so a
    # failed write below leaves the bot with a usable (new) mandate.
    live._update(trial_id, mandateId=mandate["id"])  # noqa: SLF001 - audited seam
    if previous_id and previous_id != mandate["id"]:
        try:
            service.revoke_mandate(previous_id)
        except Exception:  # noqa: BLE001 - expiry is also a revocation
            pass
    service.connections.record_event(
        connection_id,
        "mandate",
        "renewed",
        {"mandateId": mandate["id"], "replaces": previous_id or None},
    )
    after = authority_status(service, live, live.get(trial_id))
    return {"renewed": True, "arm": arm, "mandate": mandate, "status": after}


def _definition(automation_id: str) -> Any:
    if not automation_id:
        return None
    try:
        from .authority import bot_definition  # noqa: PLC0415 - lazy, avoids a cycle

        return bot_definition(automation_id)
    except Exception:  # noqa: BLE001 - the broker still re-checks the pin
        return None


__all__ = [
    "ARM_REFRESH_SECONDS",
    "MANDATE_REFRESH_SECONDS",
    "MAX_ARM_TTL_SECONDS",
    "MIN_REMAINING_ORDERS",
    "authority_status",
    "ensure_authority",
    "renew_arm",
]
