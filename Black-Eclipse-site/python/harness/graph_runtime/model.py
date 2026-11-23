"""Eclipse Workflow IR — the runtime-side contracts.

Mirrors the TypeScript canonical model in ``src/lib/graph-studio/ir.ts``: same
``irVersion``, same field names (camelCase via aliases). The IR is UNTRUSTED —
it arrives over HTTP from the front door (which loads it from JDDS) and is
re-validated here with pydantic before anything is compiled or executed.

Keep this file in lockstep with the TS schema. A change to either side without
the other is an IR break and must bump ``irVersion`` in BOTH.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# The IR schema versions this runtime can compile. Unknown versions are refused
# with an explicit diagnostic rather than coerced.
SUPPORTED_IR_VERSIONS: tuple[int, ...] = (1, 2)

# Bounds — a workflow is a plan, never an unbounded program.
MAX_NODES = 500
MAX_EDGES = 2_000
MAX_RUN_TIMEOUT_MS = 3_600_000
MAX_NODE_TIMEOUT_MS = 600_000
MAX_PREVIEW_CHARS = 4_000
# A subworkflow may nest, but never unboundedly. The front door also refuses a
# larger/cyclic bundle before the runtime sees it.
MAX_SUBWORKFLOW_DEPTH = 5


class PortType(str, Enum):
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING = "string"
    OBJECT = "object"
    ARRAY = "array"
    DATETIME = "datetime"
    # A broker order/position id. MetaTrader uses an integer; a REST venue
    # uses an opaque id, so this port accepts a number OR a string and the
    # trade layer normalises it (`ticket_ref`).
    TICKET = "ticket"
    INSTRUMENT = "instrument"
    OHLCV = "ohlcv"
    INDICATOR = "indicator"
    SIGNAL = "signal"
    PORTFOLIO = "portfolio"
    ORDER_INTENT = "order_intent"
    RESEARCH_FINDING = "research_finding"
    EVIDENCE = "evidence"
    AGENT_RESULT = "agent_result"
    EXECUTION_RESULT = "execution_result"
    # Local asset references (graph asset store / vision asset store). They
    # carry a small metadata object, never the bytes themselves.
    FILE = "file"
    IMAGE = "image"
    ANY = "any"


class PortKind(str, Enum):
    DATA = "data"
    CONTROL = "control"


class ExecutionMode(str, Enum):
    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class SideEffect(str, Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    TRADING = "trading"
    # A model-provider call. It costs tokens but does not act on the world, so a
    # test run may perform it (its tool surface is narrowed separately). This is
    # deliberately distinct from NETWORK, which stays denied in a test run.
    MODEL = "model"
    # Local scratch (the runtime's own store). It survives the run but cannot
    # affect the world, so a test run may perform it while external write,
    # network and trading effects stay denied.
    LOCAL_WRITE = "local_write"


class RunStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class PortDef(_Model):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    type: PortType
    kind: PortKind = PortKind.DATA
    optional: bool = False
    nullable: bool = False
    description: str = ""


class NodePosition(_Model):
    x: float = 0.0
    y: float = 0.0


class WorkflowNode(_Model):
    id: str = Field(min_length=1, max_length=80)
    type: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=32)
    label: Optional[str] = Field(default=None, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)
    # Literal values for input ports (wire wins). Optional on the node so an
    # older document stays readable; the compiler and executor treat it as the
    # fallback source for an unwired required port.
    input_values: dict[str, Any] = Field(default_factory=dict, alias="inputValues")
    layout: NodePosition = Field(default_factory=NodePosition)


class WorkflowEdge(_Model):
    id: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=80)
    source_handle: str = Field(alias="sourceHandle", min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=80)
    target_handle: str = Field(alias="targetHandle", min_length=1, max_length=64)
    condition: Optional[str] = Field(default=None, max_length=500)


class WorkflowSettings(_Model):
    execution_mode: ExecutionMode = Field(default=ExecutionMode.RESEARCH, alias="executionMode")
    fail_fast: bool = Field(default=False, alias="failFast")
    max_concurrency: int = Field(default=4, alias="maxConcurrency", ge=1, le=64)
    timeout_ms: int = Field(default=0, alias="timeoutMs", ge=0, le=MAX_RUN_TIMEOUT_MS)


class WorkflowMeta(_Model):
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")
    tags: list[str] = Field(default_factory=list)


class WorkflowIR(_Model):
    ir_version: int = Field(alias="irVersion", ge=1)
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2_000)
    meta: WorkflowMeta = Field(default_factory=WorkflowMeta)
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=MAX_NODES)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=MAX_EDGES)
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    required_capabilities: list[str] = Field(default_factory=list, alias="requiredCapabilities")

    def node(self, node_id: str) -> Optional[WorkflowNode]:
        return next((n for n in self.nodes if n.id == node_id), None)


class Diagnostic(_Model):
    severity: Severity
    code: str
    message: str
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    edge_id: Optional[str] = Field(default=None, alias="edgeId")


# ── Compilation plan ────────────────────────────────────────────────────────


class PlanNode(_Model):
    node_id: str = Field(alias="nodeId")
    node_type: str = Field(alias="nodeType")
    version: str
    layer: int
    capabilities: list[str] = Field(default_factory=list)
    side_effects: SideEffect = Field(alias="sideEffects")
    timeout_ms: int = Field(alias="timeoutMs")
    max_attempts: int = Field(alias="maxAttempts")


class ExecutionPlan(_Model):
    """The compiler's output. Serializable, auditable, and what the executor runs."""

    workflow_id: str = Field(alias="workflowId")
    ir_version: int = Field(alias="irVersion")
    mode: ExecutionMode
    test_run: bool = Field(alias="testRun")
    executable: bool
    layers: list[list[str]] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    nodes: list[PlanNode] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list, alias="requiredCapabilities")
    diagnostics: list[Diagnostic] = Field(default_factory=list)


# ── Run records ─────────────────────────────────────────────────────────────


class NodeRun(_Model):
    node_id: str = Field(alias="nodeId")
    node_type: str = Field(alias="nodeType")
    label: str = ""
    status: NodeStatus
    layer: int = 0
    attempt: int = 1
    started_at: Optional[str] = Field(default=None, alias="startedAt")
    finished_at: Optional[str] = Field(default=None, alias="finishedAt")
    duration_ms: Optional[int] = Field(default=None, alias="durationMs")
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    error_code: Optional[str] = Field(default=None, alias="errorCode")


class RunEvent(_Model):
    at: str
    kind: str
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    message: str = ""


class GraphRun(_Model):
    id: str
    workflow_id: str = Field(alias="workflowId")
    workflow_version: Optional[int] = Field(default=None, alias="workflowVersion")
    ir_version: int = Field(alias="irVersion")
    mode: ExecutionMode
    test_run: bool = Field(alias="testRun")
    status: RunStatus
    started_at: str = Field(alias="startedAt")
    finished_at: Optional[str] = Field(default=None, alias="finishedAt")
    duration_ms: Optional[int] = Field(default=None, alias="durationMs")
    error: Optional[str] = None
    owner_id: str = Field(default="local", alias="ownerId")
    # The terminal value(s) of the run. One terminal -> its value; multiple
    # terminals -> {nodeId: value}. A subworkflow node returns this as its output.
    output: Any = None
    node_runs: list[NodeRun] = Field(default_factory=list, alias="nodeRuns")
    events: list[RunEvent] = Field(default_factory=list)
    plan: Optional[ExecutionPlan] = None


class DeferredWait(Exception):
    """A durable wait node asks the run to pause instead of blocking a thread.

    `rerun=True` is the approval-gate variant: the node has NOT finished, so it
    is left PENDING and the resumed run re-enters it to read the decision.
    """

    def __init__(
        self, wake_after_seconds: float, outputs: dict[str, Any], *, rerun: bool = False,
    ) -> None:
        super().__init__(f"defer for {wake_after_seconds:.3f}s")
        self.wake_after_seconds = max(0.0, float(wake_after_seconds))
        self.outputs = outputs
        self.rerun = bool(rerun)


class RunStopRequest(_Model):
    """A node asked the RUN to end with an explicit outcome (`logic.stop`).

    It travels as the reserved ``_stop`` key of an executor result — the same
    convention as ``_control`` — so the node itself still succeeds while the run
    status is decided by the node, not by the absence of failures.
    """

    status: Literal["succeeded", "failed"] = "succeeded"
    message: str = ""


class RunDeferred(Exception):
    """The executor paused a durable run; the Fabric job is requeued at wake time."""

    def __init__(self, run: GraphRun, wake_after_seconds: float) -> None:
        super().__init__(f"run {run.id} deferred for {wake_after_seconds:.3f}s")
        self.run = run
        self.wake_after_seconds = max(0.0, float(wake_after_seconds))


__all__ = [
    "SUPPORTED_IR_VERSIONS",
    "MAX_NODES",
    "MAX_EDGES",
    "MAX_RUN_TIMEOUT_MS",
    "MAX_NODE_TIMEOUT_MS",
    "MAX_PREVIEW_CHARS",
    "MAX_SUBWORKFLOW_DEPTH",
    "PortType",
    "PortKind",
    "ExecutionMode",
    "SideEffect",
    "RunStatus",
    "NodeStatus",
    "Severity",
    "PortDef",
    "NodePosition",
    "WorkflowNode",
    "WorkflowEdge",
    "WorkflowSettings",
    "WorkflowMeta",
    "WorkflowIR",
    "DeferredWait",
    "RunDeferred",
    "RunStopRequest",
    "Diagnostic",
    "PlanNode",
    "ExecutionPlan",
    "NodeRun",
    "RunEvent",
    "GraphRun",
]
