"""Wire contracts for the harness runtime.

These are pydantic mirrors of `src/lib/harness/types/contracts.ts`. The TS side
is authoritative for the shape; this module must stay in lockstep with it. Keep
field names identical so the JSON crossing the bridge is self-describing.

Every model validates external input — the runtime never trusts the caller.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

RunStatus = Literal["created", "running", "completed", "failed", "cancelled"]
HarnessErrorCode = Literal[
    "INVALID_INPUT",
    "PROVIDER_NOT_CONFIGURED",
    "PROVIDER_ERROR",
    "PROVIDER_TIMEOUT",
    # The RUN's own wall clock expired (the graph emits `RUN_TIMEOUT`), which is
    # distinct from one provider call timing out. It is a valid terminal error:
    # without it, building the failed RunResult raised a validation error while
    # reporting the timeout, masking the real outcome.
    "RUN_TIMEOUT",
    "CONTEXT_OVERFLOW",
    "TOOL_NOT_FOUND",
    "TOOL_VALIDATION_FAILED",
    "TOOL_FORBIDDEN",
    "TOOL_TIMEOUT",
    "TOOL_ERROR",
    "MAX_STEPS_EXCEEDED",
    "TOOL_BUDGET_EXCEEDED",
    "CHECKPOINT_NOT_FOUND",
    "RESUME_CONFLICT",
    "CANCELLED",
    # W6: a durable boundary failed, or this worker no longer owns the run.
    "CHECKPOINT_WRITE_FAILED",
    "RUN_LEASE_LOST",
    "PERSISTENCE_ERROR",
    # Surface guardrails (v3 P3): the finished answer broke a rule the
    # activated surface set. The run fails instead of shipping the answer.
    "GUARDRAIL_TOOL_REQUIRED",
    "GUARDRAIL_CITATION_MISSING",
    "GUARDRAIL_ANSWER_TOO_LONG",
    "INTERNAL_ERROR",
]

# The largest single turn the runtime accepts, in characters. It MIRRORS the
# front door's `MAX_QUERY_LENGTH` (`src/lib/harness/validation.ts`): the front
# door rejects anything larger, so the runtime must accept everything the front
# door lets through — otherwise a request the app accepted fails here with a
# bare pydantic 422.
#
# This is NOT the real limit on a prompt. The real limit is the resolved
# model's context window, which the runtime measures against the assembled
# request (`context_budget.py`) and reports as an honest CONTEXT_OVERFLOW.
MAX_QUERY_CHARS = 100_000

# The largest text one attached document may carry. It matches `MAX_QUERY_CHARS`
# because a big paste becomes an attached document (the composer hands it over
# as one): it must arrive as readable as the same text would have as a message.
MAX_DOCUMENT_CHARS = 100_000
#: Reference images attached to a bot: same budget the UI enforces (4 × ≤2 MB,
#: base64 is ~4/3 the byte size, with headroom for the data-URL prefix).
MAX_RUN_IMAGES = 4
MAX_RUN_IMAGE_CHARS = 3_000_000

# The largest single PRIOR turn carried as history. It matches
# `MAX_QUERY_CHARS` so a message the composer accepted is not silently cut
# down when it returns as history. The runtime still bounds the ASSEMBLED
# history against the model's window (oldest turns dropped first).
MAX_TURN_CONTENT_CHARS = 100_000


class HarnessError(BaseModel):
    code: HarnessErrorCode
    message: str
    detail: Optional[str] = None


class ConversationTurn(BaseModel):
    """One prior turn, so a chat follow-up has context."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_TURN_CONTENT_CHARS)


class RunDocument(BaseModel):
    """Extracted text of a document the user attached to the conversation.

    The front door performs extraction (the runtime has no filesystem access);
    this is the already-readable text, injected into the user turn.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)
    # The front door SHORTENS an over-large document (per-doc then combined caps).
    # `truncated` marks it so the runtime can say so instead of answering from
    # only the first part; `original_chars` is the pre-truncation length when the
    # extractor knew it.
    truncated: bool = False
    original_chars: Optional[int] = Field(None, ge=0)


class RunImage(BaseModel):
    """A reference image the operator attached to a bot (visual teaching).

    The front door already bounded the file (≤2 MB, PNG/JPEG/WebP); the runtime
    re-checks the shape so a hand-crafted request cannot send arbitrary bytes or
    a remote URL. The image is passed to the model as a real image part — never
    quoted as market data (see the prompt block in `automation.trading_policy`).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    data_url: str = Field(min_length=32, max_length=MAX_RUN_IMAGE_CHARS)
    caption: str = Field("", max_length=280)
    mime: str = Field("image/png", max_length=40)

    @field_validator("data_url")
    @classmethod
    def _must_be_raster_data_url(cls, value: str) -> str:
        if not value.lower().startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
            raise ValueError("image must be a PNG/JPEG/WebP base64 data URL")
        return value


class RunInput(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    ticker: Optional[str] = None
    market: Optional[str] = None
    context: Optional[dict[str, Any]] = None
    # Prior conversation turns (oldest first). Bounded so a long chat cannot
    # blow the context window; the runtime keeps the most recent MAX_HISTORY.
    history: Optional[list[ConversationTurn]] = None
    # Attached documents, already extracted to text by the front door.
    documents: Optional[list[RunDocument]] = None
    # Reference images attached to a bot (visual teaching). Additive/optional.
    images: Optional[list[RunImage]] = Field(default=None, max_length=MAX_RUN_IMAGES)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be blank")
        return stripped


class ModelSettings(BaseModel):
    """Provider + credentials + model, resolved by the TS side from the vault.

    Credentials arrive here because the Python service cannot read the JDdS
    settings vault. They are never persisted or logged by this service.

    The TS front door sends camelCase (`baseUrl`/`apiKey`), so aliases are
    generated with `to_camel` AND `populate_by_name` keeps the snake_case
    constructor used by tests/internal callers working. Without this the
    camelCase fields were silently dropped by pydantic and the runtime fell back
    to the catalog default base URL with no key.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    provider: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    # Named custom connections choose where the key travels. For a standard
    # Bearer endpoint this is absent (ChatOpenAI's normal Authorization path).
    # For x-api-key / api-key / a custom header the runtime adds a default
    # header so the gateway receives the credential exactly as it expects.
    api_key_header: Optional[str] = None
    api_key_prefix: Optional[str] = None
    # Stable per-conversation id for gateways that require one (OpenCode Go's
    # `x-opencode-session`). Absent = a process-stable fallback id.
    session_id: Optional[str] = None
    model: str
    temperature: Optional[float] = None
    # Optional max output tokens for one response. Absent = the provider's own
    # default (the runtime sends nothing). Mapped per provider: OpenAI-compatible
    # / Anthropic / Mistral / Groq `max_tokens`, Gemini `max_output_tokens`.
    max_tokens: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    # Optional operator-declared context window (tokens) for this run. Used ONLY
    # when the catalog cannot resolve one (a local server, a router, a custom
    # endpoint). Never fabricated: absent/None leaves the window unknown and the
    # conservative ceiling applies. Additive, so old callers are unaffected.
    context_window: Optional[int] = Field(default=None, ge=1, le=10_000_000)


#: The context policies a live surface may set. Both are CLOSED SETS: an
#: unknown value is refused, never silently treated as the default.
SKILL_MANIFEST_POLICIES: tuple[str, ...] = ("none", "top3", "top8")
MEMORY_POLICIES: tuple[str, ...] = ("off", "profile_only", "profile_plus_search")


class AgentConfig(BaseModel):
    id: str
    name: str
    systemPrompt: str = Field(min_length=1)  # noqa: N815 - mirror TS field name
    tools: Optional[list[str]] = None
    maxSteps: Optional[int] = Field(default=8, ge=1, le=32)  # noqa: N815
    toolTimeoutMs: Optional[int] = Field(default=20_000, ge=100, le=120_000)  # noqa: N815
    runTimeoutMs: Optional[int] = Field(default=120_000, ge=1000, le=600_000)  # noqa: N815
    # The maximum number of tool calls this run may execute in total. A skill
    # manifest declares `limits.maxToolCalls`; the runtime carries it here so it
    # is ENFORCED in the graph, not merely advertised in metadata/prompts. None
    # means unbounded (ordinary chat agents). Enforced per call in `tools_node`.
    maxToolCalls: Optional[int] = Field(default=None, ge=0, le=200)  # noqa: N815
    # The cumulative prompt+completion token budget for this run. When reached,
    # the graph stops binding tools and forces a final answer from the evidence
    # already gathered (never a silent extra loop). None = unbounded.
    maxTokens: Optional[int] = Field(default=None, ge=1_000, le=50_000_000)  # noqa: N815
    # Narrow opt-in: when True, the run's effective allowlist is expanded to also
    # include the MCP tools CURRENTLY registered, selected and namespaced in the
    # registry (`mcp_*`). Default False, so a custom/skill agent keeps its exact
    # restricted allowlist and can never call an MCP tool it did not name. It is
    # an expansion over registration state, not permission to call arbitrary
    # tools: registration itself already enforces enabled + selected + read-only.
    allowMcpTools: bool = False  # noqa: N815

    # When True, the run's effective allowlist is expanded to include the Plugin
    # Platform tools (`plugin_*` discovery/invoke) and the enabled plugins'
    # contributed tools (`plug_*`). Default False, so a custom/skill agent keeps
    # its exact allowlist. Contributed plugin tools are only registered when their
    # plugin is enabled, which is an operator decision.
    allowPluginTools: bool = False  # noqa: N815

    # When True, the run's effective allowlist is expanded to include the ML
    # Lab's per-model predict tools (`ml_model_*_predict`) that are CURRENTLY
    # registered. Those tools are created at boot from the model catalog, so
    # they cannot be named in a static allowlist and must never be appended to
    # the shared agent config (that leaked deleted models into the surface).
    # Default False, so a custom/skill agent keeps its exact allowlist.
    allowModelTools: bool = False  # noqa: N815

    # Surface-controlled CONTEXT policy (v3 P3). None means the runtime
    # default; a value narrows how much context the agent receives. Neither
    # field grants a tool, a permission or an effect: they only bound what is
    # injected into the prompt.
    #   skillManifest: "none" | "top3" | "top8"  -> how many skills are listed
    #   memoryPolicy:  "off" | "profile_only" | "profile_plus_search"
    skillManifest: Optional[str] = None  # noqa: N815
    memoryPolicy: Optional[str] = None  # noqa: N815

    # Surface-controlled ANSWER RULES (v3 P3). A guardrail is checked by the
    # RUNTIME at the end of the run: a violation fails the run with a named
    # code. It can only reject an answer, never grant a tool or a permission.
    guardrails: tuple[str, ...] = ()
    maxAnswerChars: Optional[int] = Field(default=None, ge=1, le=200_000)  # noqa: N815

    # The bounded prompt a DELEGATED child inherits (the parent's own prompt
    # plus this). It is prompt evidence: it cannot add a tool, a permission or
    # an effect, and the child's allowlist is still the parent's narrowed one.
    subagentPrompt: Optional[str] = Field(default=None, max_length=4_000)  # noqa: N815

    model_config = {"populate_by_name": True}

    @field_validator("guardrails")
    @classmethod
    def _known_guardrails(cls, value: Any) -> tuple[str, ...]:
        from .self_improvement.guardrails import GUARDRAILS  # noqa: PLC0415

        names = tuple(str(name) for name in (value or ()))
        unknown = sorted(set(names) - set(GUARDRAILS))
        if unknown:
            raise ValueError(
                f"unknown guardrail(s): {', '.join(unknown)} "
                f"(known: {', '.join(GUARDRAILS)})"
            )
        return names

    @field_validator("skillManifest")
    @classmethod
    def _known_skill_manifest_policy(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in SKILL_MANIFEST_POLICIES:
            raise ValueError(
                f"skillManifest must be one of {SKILL_MANIFEST_POLICIES}, got {value!r}"
            )
        return value

    @field_validator("memoryPolicy")
    @classmethod
    def _known_memory_policy(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in MEMORY_POLICIES:
            raise ValueError(
                f"memoryPolicy must be one of {MEMORY_POLICIES}, got {value!r}"
            )
        return value


class RunWorkspace(BaseModel):
    """An optional narrowing/confirmation of the server-resolved workspace root.

    Phase 1 binds only the SERVER-RESOLVED root; a caller-supplied `root` can
    only CONFIRM it. A mismatch means the run gets no workspace at all.
    """

    root: str = Field(min_length=1, max_length=1024)
    baseRevision: Optional[str] = Field(default=None, max_length=128)  # noqa: N815


class RunRequest(BaseModel):
    runId: str  # noqa: N815
    input: RunInput
    # Python is authoritative for agents: the front door sends an id and the
    # runtime resolves the AgentConfig (including its system prompt) from its own
    # presets. `agent` is an optional explicit override for callers that need a
    # bespoke configuration; when both are absent the default agent is used.
    agentId: Optional[str] = None  # noqa: N815
    agent: Optional[AgentConfig] = None
    modelSettings: ModelSettings  # noqa: N815
    userId: Optional[str] = None  # noqa: N815
    # The verified tenant for this run. Set by the API boundary from the verified
    # edge identity — never trusted from the client body when multi-tenant.
    tenantId: Optional[str] = None  # noqa: N815
    # M3/M4: the request-scoped isolation identity. A run belongs to one session,
    # profile and project. All are optional for backward compatibility; when
    # absent the runtime uses the default profile and derives a session id.
    sessionId: Optional[str] = None  # noqa: N815
    profileId: Optional[str] = None  # noqa: N815
    projectId: Optional[str] = None  # noqa: N815
    # Private per-conversation memory (additive, optional). When set, durable
    # memory captured in this run is owned by this conversation id and is
    # readable only by it (plus truly-global `general` knowledge and the
    # signed-in user's own rows). Used by bot chats and group-room member turns;
    # absent for ordinary chat, whose behaviour is unchanged.
    conversationId: Optional[str] = None  # noqa: N815
    # Phase 3: an optional skill bundle to expand into the run's skill manifest.
    # The bundle is a reference only — a member that is missing/disabled is
    # reported and skipped, never enabled.
    bundleId: Optional[str] = None  # noqa: N815
    # ── Run lineage + resume (Phase 1) ─────────────────────────────────────
    # The run that spawned this one (e.g. a skill invocation, a workflow step).
    # None for a top-level run. Recorded so lineage survives a restart.
    parentRunId: Optional[str] = None  # noqa: N815
    # The workflow this run belongs to, when the composable workflow path is used.
    workflowId: Optional[str] = None  # noqa: N815
    # A checkpoint id to resume from. When set, the runtime re-enters the graph
    # at the recorded node instead of starting a fresh run; a missing checkpoint
    # fails with CHECKPOINT_NOT_FOUND and a terminal run with RESUME_CONFLICT.
    resumeFrom: Optional[str] = None  # noqa: N815
    # Phase 1 (additive, optional): bind this run to a read-only workspace. Absent
    # ⇒ the run has no code capability at all (the default-actor rule, Phase 0).
    workspace: Optional[RunWorkspace] = None
    # Phase 23 (additive, optional): the APPROVED workspace root chosen for this
    # chat session. An id, never a path — the runtime resolves it against
    # TrustedRoot and refuses an unknown/revoked id. Absent ⇒ the globally
    # selected root, then the repo root (zero-config unchanged).
    workspaceRootId: Optional[str] = Field(default=None, max_length=200)  # noqa: N815
    # Phase 2 (additive, optional): the run's autonomy profile. Only a workspace-
    # bound run with a known profile may hold code verbs (Phase 0). "edit_verify"
    # is what grants code.write; absent/unknown grants nothing (fail-closed).
    autonomy: Optional[str] = None
    # Phase 5 (additive, optional): the workspace this run is bound to. A code
    # tool called with no binding is refused (Phase 0: no workspace ⇒ no code
    # permissions, whatever the profile).
    workspaceId: Optional[str] = None  # noqa: N815
    # W7 (internal): an opaque capability token for a scratch worktree created
    # by `worktree.allocate`. Only the delegation worker sets it; one token
    # binds exactly one run, and it carries no caller-supplied path.
    workspaceScratchToken: Optional[str] = Field(default=None, max_length=128)  # noqa: N815
    # Phase 22 (additive, optional): a build session's budget. Absent means the
    # agent/profile default. The runtime clamps both to its hard ceilings, so a
    # caller can never buy an unbounded run.
    maxSteps: Optional[int] = Field(None, ge=1, le=240)  # noqa: N815
    runTimeoutMs: Optional[int] = Field(None, ge=1_000, le=10_800_000)  # noqa: N815
    # Phase 8 (additive, optional): enable the analysis sandbox for THIS run.
    # Default False — discovery is not activation. A computed number is always
    # labelled "worked out by Eclipse" and never presented as retrieved data.
    analysisEnabled: bool = False  # noqa: N815
    # A recognized chat command that FORCED this run's profile (e.g.
    # "eclipsesdk"), when the user invoked one. Additive/optional: an ordinary
    # turn omits it. A command turn is never treated as trivial, and the profile
    # was selected by the command rather than inferred by the classifier.
    command: Optional[str] = None
    # Approval cards (additive, optional): "ask" makes a non-read tool with no
    # matching approval PAUSE the run and wait for the operator's decision via the
    # approvals decide route, then resume in place. Absent/None keeps the default
    # deny-and-retry behavior. The decision itself is still the single-use,
    # digest-bound approval — this only changes whether the run waits for it.
    approvalMode: Optional[str] = None  # noqa: N815
    # Folder-selection (additive, optional): when True, the task edits the
    # SELECTED trusted root DIRECTLY instead of a disposable copy. None = resolve
    # from the selected root at run time (a folder a person chose edits directly;
    # the built-in repo stays isolated). An explicit True/False always wins.
    direct: Optional[bool] = None
    # Base URL of the TS front door, used ONLY as the read-only JDdS data bridge.
    callbackBaseUrl: str  # noqa: N815
    # Shared secret for the data bridge (matches CRON_SECRET on the TS side).
    callbackToken: Optional[str] = None  # noqa: N815
    # Whether an operator-activated live surface may override this run. Normal
    # chat/automation runs keep the default; evaluation, gates and candidate
    # trials set False so a live surface can never contaminate a measurement.
    liveSurface: bool = True  # noqa: N815


class RunResult(BaseModel):
    runId: str  # noqa: N815
    status: RunStatus
    output: Optional[str] = None
    error: Optional[HarnessError] = None
    # ── Run lineage (Phase 1, additive) ─────────────────────────────────────
    parentRunId: Optional[str] = None  # noqa: N815
    childRunIds: list[str] = Field(default_factory=list)  # noqa: N815
    modelSteps: int = 0  # noqa: N815
    toolCalls: int = 0  # noqa: N815
    toolErrors: int = 0  # noqa: N815
    promptTokens: int = 0  # noqa: N815
    completionTokens: int = 0  # noqa: N815
    totalTokens: int = 0  # noqa: N815
    # Phase 4 (additive, optional): evidence-grounded research metadata for a
    # gated run. Absent/None for ordinary chat and for older persisted rows, so
    # existing clients remain compatible.
    research: Optional[dict[str, Any]] = None  # noqa: N815
    # Phase 3-M Reliability (additive, optional): the deterministic outcome of
    # explicit user memory intent for this turn (saved/forgotten/corrected/
    # refused/needs_confirmation) and whether the run is eligible to become a
    # learning candidate. Absent for skill runs and older rows.
    memory: Optional[dict[str, Any]] = None  # noqa: N815
    # Context budgeting (additive, optional): how the resolved model's window
    # bounded this run's history. Absent for older persisted rows and for runs
    # that did not trim, so existing clients remain compatible.
    context: Optional[dict[str, Any]] = None
    # Phase 4 (additive, optional): the run's code verification ledger + repair
    # outcome (records, claims, unverified count). Absent/None for ordinary chat
    # and older rows, so existing clients remain compatible.
    codeVerification: Optional[dict[str, Any]] = None  # noqa: N815
    # Phase 5 (additive, optional): the run's resolved autonomy profile and the
    # workspace it was bound to. Absent/None for ordinary finance runs.
    autonomy: Optional[str] = None
    workspaceId: Optional[str] = None  # noqa: N815
    # Phase 14 (additive, optional): the chat capability view for a coding turn —
    # the profile, an i18n headline KEY, and the verification blob it composes.
    # Absent/None for ordinary chat and older rows.
    capability: Optional[dict[str, Any]] = None
    # Round 3 / W2 (additive, optional): the run's tool-surface resolution - the
    # surface id, the tools it ADDED, the tools the autonomy profile removed, the
    # tools a disabled store removed, and the measured bound size. It exists so a
    # missing tool is always explainable: a surface never removes a tool quietly.
    # Absent/None for runs written before the surface was published.
    surface: Optional[dict[str, Any]] = None
    # Vision/Multimodal (additive, optional): the run's collected vision results
    # (asset ids, regions, OCR, confidence, provenance) for the right sidebar.
    # Absent/None when a run used no vision tool, so existing clients remain
    # compatible.
    vision: Optional[dict[str, Any]] = None
    # Document Intelligence (additive, optional): the run's collected document
    # results (type, pages, status, capabilities, entities, evidence counts) for
    # the right sidebar. Absent/None when a run used no document tool, so existing
    # clients remain compatible.
    documents: Optional[dict[str, Any]] = None
    # Internet Image Search (additive, optional): the run's validated, normalized
    # image results (direct image URL, thumbnail, source page, dimensions) for the
    # chat's INLINE gallery. Absent/None when a run used no image tool, so existing
    # clients remain compatible.
    images: Optional[dict[str, Any]] = None
    # Native Video (additive, optional): the video jobs this run QUEUED (job and
    # project ids) for the chat's live job card — progress, playback, download.
    # Absent/None when a run used no video tool, so existing clients remain
    # compatible.
    video: Optional[dict[str, Any]] = None


EventLiteral = Literal[
    "run.created",
    "run.started",
    "model.requested",
    "model.completed",
    "tool.requested",
    "tool.completed",
    "tool.failed",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "run.replanned",
]


class HarnessEvent(BaseModel):
    runId: str  # noqa: N815
    type: EventLiteral
    data: Optional[dict[str, Any]] = None


class ToolExecutionRequest(BaseModel):
    callId: str  # noqa: N815
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResponse(BaseModel):
    toolCallId: str  # noqa: N815
    name: str
    ok: bool
    text: str
    error: Optional[HarnessError] = None
    durationMs: int = 0  # noqa: N815
