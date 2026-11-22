"""Process-wide asset evolution service for operator endpoints.

The adapters bind to the live kernel services when the kernel is enabled; when
it is off the service is still inspectable but ``apply`` is refused by the API
(an asset change would not reach the running prompt/agent plane).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .adapters import AdapterRegistry, AgentPresetAdapter, PromptSectionAdapter
from .adapters_extra import (
    CompositionRowAdapter,
    MemoryPolicyAdapter,
    ModelRouteAdapter,
    SkillAdapter,
    ToolPolicyAdapter,
)
from .evaluator import AssetEvaluator
from .journal import EvolutionJournal
from .service import AssetEvolution

_SINGLETON: Optional[AssetEvolution] = None
_PIPELINE: Any = None


def journal_path() -> Path:
    configured = os.environ.get("ECLIPSE_EVOLUTION_JOURNAL")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".black-eclipse" / "evolution" / "journal.jsonl"


def asset_evolution(*, fresh: bool = False) -> AssetEvolution:
    global _SINGLETON
    if _SINGLETON is not None and not fresh:
        return _SINGLETON
    from ..kernel.services.agents import AgentCatalog  # noqa: PLC0415
    from ..kernel.services.prompt import PromptService  # noqa: PLC0415
    from ..kernel.services.runtime import kernel_runtime  # noqa: PLC0415

    adapters = AdapterRegistry()
    adapters.register(PromptSectionAdapter(kernel_runtime.prompt or PromptService()))
    adapters.register(AgentPresetAdapter(kernel_runtime.agents or AgentCatalog()))
    if kernel_runtime.skills is not None:
        adapters.register(SkillAdapter(kernel_runtime.skills))
    if kernel_runtime.policies is not None:
        adapters.register(ToolPolicyAdapter(kernel_runtime.policies))
    if kernel_runtime.model_routes is not None:
        adapters.register(ModelRouteAdapter(kernel_runtime.model_routes))
    if kernel_runtime.composition_rows is not None:
        adapters.register(CompositionRowAdapter(kernel_runtime.composition_rows))
    if getattr(kernel_runtime, "memory_policy", None) is not None:
        adapters.register(MemoryPolicyAdapter(kernel_runtime.memory_policy))
    _SINGLETON = AssetEvolution(
        adapters,
        EvolutionJournal(journal_path()),
        max_active=int(os.environ.get("ECLIPSE_EVOLUTION_MAX_ACTIVE", "10") or 10),
        ttl_seconds=int(os.environ.get("ECLIPSE_EVOLUTION_TTL_SECONDS", "0") or 0),
    )
    return _SINGLETON


def asset_evaluator(evolution: Optional[AssetEvolution] = None) -> AssetEvaluator:
    return AssetEvaluator(evolution or asset_evolution())


def asset_pipeline(evolution: Optional[AssetEvolution] = None) -> Any:
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    from .pipeline import EvolutionPipeline  # noqa: PLC0415

    _PIPELINE = EvolutionPipeline(
        evolution or asset_evolution(),
        mode=os.environ.get("ECLIPSE_EVOLUTION_MODE", "propose"),
    )
    return _PIPELINE


def reset_assets() -> None:
    global _SINGLETON, _PIPELINE
    _SINGLETON = None
    _PIPELINE = None
