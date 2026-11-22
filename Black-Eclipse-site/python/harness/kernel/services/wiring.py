"""Bind the kernel contribution catalog to the real consumers at boot.

One call from `AgenticService.__init__` attaches the retrieval provider
registry, the research search registry, the workflow store, the model provider
registry and (when present) the UI service. Existing catalog entries are
replayed by `ContributionService.attach_*`, so plugins loaded later and plugins
loaded before this call both reach the same consumer.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("harness.kernel.wiring")


def install_kernel_surfaces(service: Any) -> None:
    runtime = getattr(service, "kernel_runtime", None)
    if runtime is None or not getattr(runtime, "enabled", False):
        return
    try:
        runtime.attach_retrieval()
    except Exception:  # noqa: BLE001 - a catalog attachment must not block boot
        log.warning("kernel retrieval registry attachment failed", exc_info=True)
    try:
        runtime.attach_search()
    except Exception:  # noqa: BLE001
        log.warning("kernel search registry attachment failed", exc_info=True)
    try:
        runtime.attach_workflows(service.workflow_store())
    except Exception:  # noqa: BLE001
        log.warning("kernel workflow store attachment failed", exc_info=True)
    try:
        from ...model_runtime import get_runtime  # noqa: PLC0415

        runtime.attach_models(get_runtime().providers)
    except Exception:  # noqa: BLE001
        log.warning("kernel model registry attachment failed", exc_info=True)
    ui = getattr(service, "ui", None)
    if ui is not None:
        try:
            runtime.attach_ui(ui)
        except Exception:  # noqa: BLE001
            log.warning("kernel UI attachment failed", exc_info=True)
    memory = getattr(service, "memory", None)
    if memory is not None and getattr(memory, "policy", None) is not None:
        try:
            runtime.attach_memory_policy(memory.policy)
        except Exception:  # noqa: BLE001
            log.warning("kernel memory-policy attachment failed", exc_info=True)
