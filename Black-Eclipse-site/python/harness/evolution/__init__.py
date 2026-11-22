"""The shared evolution asset contract (Phase 6 first slice)."""

from .adapters import (
    AdapterRegistry,
    AgentPresetAdapter,
    AssetAdapter,
    PromptSectionAdapter,
)
from .adapters_extra import (
    CompositionRowAdapter,
    HookAdapter,
    MemoryPolicyAdapter,
    ModelRouteAdapter,
    PluginBundleAdapter,
    SkillAdapter,
    ToolPolicyAdapter,
)
from .assets import (
    ASSET_KINDS,
    MUTATION_OPS,
    STATUSES,
    AssetError,
    AssetKind,
    Candidate,
    CandidateStatus,
    Mutation,
    MutationOp,
    candidate_from_dict,
    mutation_digest,
    new_candidate,
)
from .evaluator import AssetEvaluator
from .journal import EvolutionJournal, JournalRecord
from .observer import EvolutionObserver, Observation, Suggestion
from .pipeline import EvolutionPipeline
from .proposer import AssetProposer
from .service import AssetEvolution

__all__ = [
    "ASSET_KINDS",
    "MUTATION_OPS",
    "STATUSES",
    "AdapterRegistry",
    "AgentPresetAdapter",
    "AssetAdapter",
    "AssetError",
    "AssetEvaluator",
    "AssetEvolution",
    "AssetProposer",
    "CompositionRowAdapter",
    "HookAdapter",
    "MemoryPolicyAdapter",
    "ModelRouteAdapter",
    "PluginBundleAdapter",
    "SkillAdapter",
    "ToolPolicyAdapter",
    "AssetKind",
    "Candidate",
    "CandidateStatus",
    "Mutation",
    "MutationOp",
    "PromptSectionAdapter",
    "EvolutionJournal",
    "EvolutionObserver",
    "EvolutionPipeline",
    "JournalRecord",
    "Observation",
    "Suggestion",
    "candidate_from_dict",
    "mutation_digest",
    "new_candidate",
]
