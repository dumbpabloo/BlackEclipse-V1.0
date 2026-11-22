"""Broker-connectivity boot wiring.

Constructs the ONE `BrokerService`, attaches it to the process-wide service and
registers the read-only broker tools. Best-effort: a failure leaves broker
connectivity unavailable rather than blocking boot. It never opens a broker
connection at boot — connections are tested only when the operator asks.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .service import BrokerService

log = logging.getLogger("harness.trade")

_SERVICE: Optional[BrokerService] = None


def install_broker_layer(service: Any) -> Optional[BrokerService]:
    global _SERVICE
    try:
        brokers = BrokerService(
            service.store,
            approvals=getattr(service, "approvals", None),
            guardrails=getattr(service, "finance_guardrails", None),
        )
        # The supervisor pauses underperforming bots through the ONE automation
        # service. A resolver keeps boot order free (the automations may be built
        # later) and a run without them still reports.
        if hasattr(service, "automation_service"):
            from .supervisor_api import bind_automations  # noqa: PLC0415

            bind_automations(brokers, service.automation_service)
        service.brokers = brokers
        _SERVICE = brokers
        from .tools import register_broker_tools, register_supervisor_tools  # noqa: PLC0415

        register_broker_tools(service.registry)
        # The stand-down is a state change, so it is outside the read-only broker
        # contract and gets its own write/high registration.
        register_supervisor_tools(service.registry)
        return brokers
    except Exception:  # noqa: BLE001 - never block boot
        service.brokers = None
        _SERVICE = None
        log.warning("broker connectivity unavailable", exc_info=True)
        return None


def get_service() -> Optional[BrokerService]:
    return _SERVICE


def set_service(service: Optional[BrokerService]) -> None:
    global _SERVICE
    _SERVICE = service


__all__ = ["BrokerService", "get_service", "install_broker_layer", "set_service"]
