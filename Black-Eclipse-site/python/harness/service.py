"""Agentic service â€” the runtime the TS API delegates to.

Holds the process-wide store, tool registry (with native finance tools) and
event bus, and exposes start/status/events/cancel over the request contracts.
The TS front door only validates + forwards; all agentic state lives here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .bridge import HttpDataBridge as _HttpDataBridge
from .code.tools import register_code_tools
from .code.session import apply_chat_build_budget, record_chat_build
from .eclipse_memory import EclipseMemoryService
from .events import EventBus, RunEmitter
from .finance_tools import register_finance_tools
from .terminal.tools import register_terminal_tools
from .graph import execute_run
from .memory_tools import register_memory_tools
from .model_runtime import get_runtime
from .persistence import AgentMemoryStore, now_iso
from .retrieval.cache import RetrievalCache
from .retrieval_tools import register_retrieval_tools
from .schemas import HarnessError, RunRequest, RunResult
from .skills import SkillRegistry
from .tools import DataBridge, ToolRegistry

log = logging.getLogger("harness.service")

# The MCP integration is an optional capability: when the `mcp` SDK is not
# importable (a trimmed install), the runtime reports MCP UNAVAILABLE honestly
# rather than failing to boot or pretending a fake agent exists.
try:  # noqa: SIM105
    from .mcp import MCPManager
    from .mcp.approvals import ApprovalStore

    MCP_AVAILABLE = True
    MCP_IMPORT_ERROR: Optional[str] = None
except Exception as _mcp_exc:  # noqa: BLE001 - a missing SDK is a supported state
    MCPManager = None  # type: ignore[assignment]
    ApprovalStore = None  # type: ignore[assignment]
    MCP_AVAILABLE = False
    MCP_IMPORT_ERROR = str(_mcp_exc)


def _workflow_enabled() -> bool:
    """Whether the composable-workflow path is enabled (operator flag, DEFAULT OFF)."""
    return os.environ.get("HARNESS_WORKFLOWS_ENABLED") in ("1", "true", "True")


_T = TypeVar("_T")


async def _with_run_slot(
    slots: Optional[asyncio.Semaphore],
    factory: Callable[[], Awaitable[_T]],
) -> _T:
    """Await a run inside the optional global concurrency limit.

    Module-level so the parity tests can call `AgenticService.run` on a light
    stub without booting the service (and without inheriting a method). The
    coroutine is created only AFTER a slot is acquired, so a run cancelled while
    queued never leaves an un-awaited coroutine behind.
    """
    if slots is None:
        return await factory()
    async with slots:
        return await factory()


def _default_callback_base_url() -> str:
    """The Next.js front door base URL for process-wide bridge calls.

    The runtime is spawned by the front door; `HARNESS_CALLBACK_URL` is the
    explicit override, otherwise loopback on the app port.
    """
    return os.environ.get("HARNESS_CALLBACK_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '3000')}"


class AgenticService:
    def __init__(self, store: Optional[AgentMemoryStore] = None) -> None:
        self.store = store or AgentMemoryStore()
        self.registry = ToolRegistry()
        # Kernel seam (off by default): scoped services + read-only inspection.
        from .kernel.services.runtime import kernel_runtime  # noqa: PLC0415
        from .kernel.inspect_tools import register_inspect_tool  # noqa: PLC0415

        kernel_runtime.configure(self.registry)
        kernel_runtime.attach_sessions(self.store)
        self.kernel_runtime = kernel_runtime
        register_inspect_tool(self.registry)
        # The legacy JDdS-only tools remain for compatibility; the Phase-2
        # retrieval tools are the provider-agnostic path the agent should prefer.
        register_finance_tools(self.registry)
        register_retrieval_tools(self.registry)
        # World-state events (Investor Eye) â€” the same JDdS feed the UI renders,
        # exposed read-only on the SAME registry.
        from .world_tools import register_world_tools  # noqa: PLC0415

        register_world_tools(self.registry)
        # The memory/skill tools the system prompt promises â€” on the SAME
        # registry, so validation/permission/timeout machinery is shared.
        register_memory_tools(self.registry)
        # Schema-on-demand, registered at boot so the portable manifest is truthful.
        from .tool_schema_load import install_tool_schema_load  # noqa: PLC0415
        install_tool_schema_load(self.registry)
        # Bounded read-only GET over the app's OWN loopback API (QA Round 1).
        from .app_api_tools import register_app_api_tools  # noqa: PLC0415

        register_app_api_tools(self.registry)
        # Workflow authoring from chat: creates and SAVES a Graph Studio workflow
        # through the front door; the store lives in JDDS/Prisma, not this runtime.
        from .graph_tools import register_graph_tools  # noqa: PLC0415

        register_graph_tools(self.registry)
        # A human-in-the-loop question tool. Read-permission: an answer is not an
        # authorization, and an effectful step still needs the approval card.
        from .clarify_tools import register_clarify_tools  # noqa: PLC0415

        register_clarify_tools(self.registry)
        # The acquire loop: install skills from a GitHub repo (approval-gated),
        # uninstall them, and list installed sources. Handlers in
        # `skills/install_tools.py`; the effect authorizer is composed in
        # `code/authorizers.py` like every other capability.
        from .skills.install_tools import register_skill_install_tools  # noqa: PLC0415

        register_skill_install_tools(self.registry)
        # The read-only repository navigator (Phase 1): five read tools on the
        # SAME registry. No write/execute tool is registered.
        register_code_tools(self.registry)
        from .charts import install_chart_tools as _install_chart_tools  # noqa: PLC0415  # chart engine: SPEC data, never code
        _install_chart_tools(self.registry)
        # Change review (Phase 19): report the repo's rules over the task diff.
        from .code.review_tools import register_review_tools  # noqa: PLC0415

        register_review_tools(self.registry)
        # The Evidence-Driven Web Research Engine: search/extract/verify tools on
        # the SAME registry. The engine reuses the existing retrieval layer, the TS
        # web transport (via the bridge), the browser runtime, the SSRF guard and
        # the ONE agent-memory connection for its EvidenceStore â€” it adds no second
        # provider, browser, memory or agent loop.
        try:
            from .research.web_store import EvidenceStore  # noqa: PLC0415
            from .research.web_tools import register_research_engine_tools  # noqa: PLC0415

            self.evidence_store = EvidenceStore(self.store)
            register_research_engine_tools(self.registry)
            from .research.web_collect import register_web_collect_tool  # noqa: PLC0415

            register_web_collect_tool(self.registry)
        except Exception:  # noqa: BLE001 - the research engine must never block boot
            self.evidence_store = None
            log.warning("web research engine unavailable", exc_info=True)
        # The scraper lane: collect data the vault does not have, by running a
        # generated program whose only way out is the host-mediated page fetch.
        # It reuses the SAME bridge transport and the analysis sandbox â€” it adds
        # no second network path.
        try:
            from .scraper.tools import register_scraper_tools  # noqa: PLC0415

            register_scraper_tools(self.registry)
        except Exception:  # noqa: BLE001 - the scraper lane must never block boot
            log.warning("scraper lane unavailable", exc_info=True)
        # The Market Data Layer: canonical market-data tools, a provider-routing
        # gateway, a mandatory data-quality engine, provenance into the Evidence &
        # Research Engine, and real-time streaming. Boot wiring lives in
        # `market/bootstrap.py` (keeping this module under its size ratchet); it
        # reuses the ONE read-only bridge transport and the ONE agent-memory
        # connection, and registers its tools on the SAME registry.
        from .market.bootstrap import install_market_layer  # noqa: PLC0415

        install_market_layer(self)
        # The Portfolio Accounting Engine: transaction ledger â†’ tax lots â†’
        # positions â†’ valuation/P&L/exposure/allocation/risk/rebalancing. Boot
        # wiring lives in `portfolio/bootstrap.py`; it reuses the ONE market
        # gateway (prices/FX) and the ONE agent-memory connection, and registers
        # its tools on the SAME registry (read + effect-authorized mutations).
        from .portfolio.bootstrap import install_portfolio_layer  # noqa: PLC0415

        install_portfolio_layer(self)
        # The General Ledger: a double-entry book of record (chart of accounts,
        # balanced postings, periods, trial balance / balance sheet / P&L / cash
        # flow). Boot wiring lives in `ledger/bootstrap.py`; it reuses the ONE
        # agent-memory connection and registers its tools on the SAME registry
        # (read + effect-authorized `ledger_write` mutations).
        from .ledger.bootstrap import install_ledger_layer  # noqa: PLC0415

        install_ledger_layer(self)
        # The Advisory Ledger: every Fiduciary Gate verdict becomes a durable
        # decision (+ basis + outcome). Boot wiring lives in `advisory/bootstrap.py`;
        # it reuses the ONE agent-memory connection and registers its read tools on
        # the SAME registry.
        from .advisory.bootstrap import install_advisory_layer  # noqa: PLC0415

        install_advisory_layer(self)
        # Typed decisions (System One-shaped): a state plus choice/score/noul
        # questions in, typed answers + calibrated confidence out. Boot wiring
        # lives in `decisions/bootstrap.py`; it reuses the ONE agent-memory
        # connection for the audit record and registers read-only tools on the
        # SAME registry. No remote provider is installed implicitly.
        from .decisions.bootstrap import install_decision_layer  # noqa: PLC0415

        install_decision_layer(self)
        # Alternative-intelligence signals (SEC insider conviction, activist
        # stakes, filing intensity, public-attention velocity) → deterministic
        # observations → ranked research cards. Wiring in `intel/bootstrap.py`; it
        # reuses the ONE agent-memory connection for observations/watchlist/cards
        # and registers its read-only tools on the SAME registry. No network call
        # happens at boot; only an actual `intel_scan` reaches EDGAR/Wikipedia.
        from .intel.bootstrap import install_intel_layer  # noqa: PLC0415

        install_intel_layer(self)
        # The Quant Engine: strategy specs â†’ event-driven backtests â†’ metrics â†’
        # experiments. Boot wiring lives in `quant/bootstrap.py`; it reuses the
        # ONE agent-memory connection (strategies + experiment metadata) and the
        # ONE market gateway (historical bars), and registers its tools on the
        # SAME registry.
        from .quant.bootstrap import install_quant_layer  # noqa: PLC0415

        install_quant_layer(self)
        # Statistical Analysis & Feature Engineering Engine (14 tools); wiring in
        # `stats/bootstrap.py`, registered on the SAME registry.
        from .stats.bootstrap import install_stats_layer  # noqa: PLC0415
        install_stats_layer(self)
        # Factor & Alpha Research Lab: factor registry + IC/rank-IC + quantile
        # portfolios + alpha composition + validation gate. Wiring in
        # `factors/bootstrap.py`; registered on the SAME registry.
        from .factors.bootstrap import install_factor_layer  # noqa: PLC0415

        install_factor_layer(self)
        # ML Research Lab: immutable datasets, temporal splits, model adapters,
        # experiment store and a versioned model registry. Wiring in
        # `ml/bootstrap.py`; registered on the SAME registry.
        from .ml.bootstrap import install_ml_layer  # noqa: PLC0415

        install_ml_layer(self)
        # Native video pipeline: draft/final renders and frame inspection as
        # queued Execution-Fabric jobs driving the isolated fframes Rust worker
        # (never inline). Wiring in `video/bootstrap.py`; final renders register
        # into the Artifact Center.
        from .video.bootstrap import install_video_layer  # noqa: PLC0415

        install_video_layer(self)
        # Projects: a named scope that holds the strategies, backtests, datasets
        # and artifacts that belong to one piece of work. Wiring in
        # `projects/bootstrap.py`; registered on the SAME registry.
        from .projects.bootstrap import install_project_layer  # noqa: PLC0415

        install_project_layer(self)
        # Eclipse-Memory is the dedicated control database for memory AND
        # skills. One service owns it; the skill tables live here (migrated out
        # of agent-memory.db) so there is exactly one skill registry.
        self.memory = EclipseMemoryService()
        self.memory_store = self.memory.store
        self.skill_store = self.memory_store  # same object â€” one persistence path
        # Learning-research layer: a durable curriculum + progress store on the
        # SAME eclipse-memory connection, plus the education tools on the SAME
        # registry. A failure only leaves `self.learning_store` None, so it never
        # blocks boot.
        try:
            from .research.education_tools import register_learning_tools  # noqa: PLC0415
            from .research.learning_store import LearningStore  # noqa: PLC0415

            self.learning_store = LearningStore(self.memory_store)
            register_learning_tools(self.registry)
        except Exception:  # noqa: BLE001 - learning layer must never block boot
            self.learning_store = None
            log.warning("learning research layer unavailable", exc_info=True)
        # One-time, non-destructive import of Phase-3 skill rows that previously
        # lived in agent-memory.db (a no-op on fresh installs / after import).
        try:
            self.memory.import_legacy_skills(self.store.path)
        except Exception:  # noqa: BLE001 - migration must not block boot
            log.warning("legacy skill import skipped", exc_info=True)
        self.skill_registry = SkillRegistry(self.skill_store)
        from .kernel.services.runtime import kernel_runtime  # noqa: PLC0415

        kernel_runtime.attach_skills(self.skill_registry)
        from .skills.install import installed_skills_home  # noqa: PLC0415

        self.skills_home = installed_skills_home()
        self.skill_load_errors = self.skill_registry.load_all(
            project_root=self._project_root(),
            project_discovery=self._project_discovery_enabled(),
            installed_home=self.skills_home,
        )
        self.skill_registry.attach_tool_registry(self.registry)
        self.bus = EventBus()
        # Round 5 / C2: EVERY run (foreground or background) is registered here,
        # so SSE and cancel can see it. Before this, only background runs were
        # tracked and a foreground stream closed while the run was still going.
        self._active: dict[str, asyncio.Task[Any]] = {}
        # Optional admission control. 0 (the default) keeps today's behaviour;
        # a positive value bounds concurrent runs without changing any contract.
        try:
            _limit = int((os.environ.get("HARNESS_MAX_CONCURRENT_RUNS") or "0").strip() or "0")
        except ValueError:
            _limit = 0
        self._run_slots: Optional[asyncio.Semaphore] = (
            asyncio.Semaphore(_limit) if _limit > 0 else None
        )
        # ONE process-wide retrieval cache. It is safe to share: in-flight
        # deduplication is keyed per retrieval, so two runs asking for the same
        # dataset at the same moment share a single provider call (Milestone 2H),
        # and a fresh cached result is reused across runs.
        self.retrieval_cache = RetrievalCache()
        # Terminal subsystem (Phase 1 of the execution backbone): ONE manager
        # shared by the agent tools and the HTTP/UI surface. It is constructed
        # BEFORE registration so the tool handlers resolve it. Its events ride the
        # process-wide bus. Persistence + boot reconciliation are wired below.
        from .terminal.runtime import build_terminal_manager, set_terminal_manager  # noqa: PLC0415
        from .terminal.store import TerminalStore  # noqa: PLC0415

        self.terminal_store = TerminalStore(self.store)
        self.terminal = build_terminal_manager(store=self.terminal_store, bus=self.bus)
        set_terminal_manager(self.terminal)
        # The terminal tools are registered on the SAME registry, so validation,
        # permission and the effect authorizer apply unchanged.
        register_terminal_tools(self.registry)
        # Response-contract read-back: the model can look up exact
        # `eclipse-response` field names and validate a drafted payload before
        # sending it (schema derived live from the TS renderer's contract through
        # the ONE read-only bridge). Read-only; never blocks boot.
        from .response_tools import register_response_contract_tools  # noqa: PLC0415

        register_response_contract_tools(self.registry)
        # Round 5 / C3: the full-text handle pager and the read-only discovery /
        # dispatch pair. Registered on the ONE registry so validation,
        # permission and the effect authorizer apply unchanged.
        try:
            from .tool_outputs import register_tool_output_tools  # noqa: PLC0415
            from .tool_surface import register_tool_surface_tools  # noqa: PLC0415
            from .code_mode import register_code_mode_tool  # noqa: PLC0415
            from .spawn.tools import register_spawn_tools  # noqa: PLC0415

            register_tool_output_tools(self.registry)
            register_tool_surface_tools(self.registry)
            register_code_mode_tool(self.registry)
            register_spawn_tools(self.registry)
        except Exception:  # noqa: BLE001 - a collision must not block boot
            log.warning("tool output/surface tools unavailable", exc_info=True)
        # The Code Execution & Verification Layer: the orchestrator, runtime
        # registry, scheduler, service manager and execution history, all reusing
        # the terminal manager for long-running services and `code/sandbox.py` for
        # one-shot runs. Its tools register on the SAME registry. Built BEFORE the
        # code store so the orchestrator can persist results once the store exists.
        from .execution.runtime import set_execution_service  # noqa: PLC0415
        from .execution.service import ExecutionService  # noqa: PLC0415
        from .execution.tools import register_execution_tools  # noqa: PLC0415

        self.execution = ExecutionService(store=self.store, terminal=self.terminal)
        set_execution_service(self.execution)
        register_execution_tools(self.registry)
        # The Application Runtime layer (DevOps): discovery, package managers,
        # Docker/Podman/Compose, health checks, ports, diagnostics, bounded
        # recovery. Built on the SAME terminal manager and `code/sandbox.py`; its
        # tools register on the SAME registry, so agent and UI share one engine.
        from .execution.application.manager import ApplicationRuntimeManager, build_runtime_registry  # noqa: PLC0415
        from .execution.application.runtime import set_application_runtime  # noqa: PLC0415
        from .execution.application.store import ApplicationRuntimeStore  # noqa: PLC0415
        from .execution.application.tools import register_application_tools  # noqa: PLC0415

        self.application_store = ApplicationRuntimeStore(self.store)
        self.application_runtime = ApplicationRuntimeManager(
            registry=build_runtime_registry(), terminal=self.terminal,
            store=self.application_store, bus=self.bus,
        )
        set_application_runtime(self.application_runtime)
        register_application_tools(self.registry)
        # Disposable code workspaces (Phase 2): rows in agent-memory.db bound to
        # the one store connection.
        from .code.store import CodeWorkspaceStore  # noqa: PLC0415

        self.code_store = CodeWorkspaceStore(self.store)
        # Git / Version-Control subsystem: ONE process-wide service bound to the
        # same agent-memory connection (journal + active-operation tables). Its
        # journal is owned by the service; the tools resolve it through the module
        # singleton so a tool handler never carries the store. Registered on the
        # SAME registry like the terminal and execution subsystems.
        from .git.register import register_git_subsystem  # noqa: PLC0415
        from .git.service import GitService, set_git_service  # noqa: PLC0415

        self.git = GitService(self.store)
        set_git_service(self.git)
        register_git_subsystem(self.registry)
        # The Code Intelligence & Understanding Engine (CIUE): ONE process-wide
        # runtime holding the LSP manager and a per-workspace code index/watcher.
        # Its events ride the process-wide bus. Built before any run so the
        # `code_*` tools resolve a shared runtime. A failure is a diagnostic.
        try:
            from .code.intel.runtime import build_code_intel_runtime, set_code_intel_runtime  # noqa: PLC0415

            self.code_intel = build_code_intel_runtime(on_event=self._publish_code_intel_event); set_code_intel_runtime(self.code_intel)
        except Exception:  # noqa: BLE001 - CIUE must never block boot
            self.code_intel = None
            log.warning("code intelligence runtime unavailable", exc_info=True)
        # Trusted workspace roots (folder picker): the persisted allowlist of
        # directories a person approved. The SELECTED root is what
        # `workspace.default_root()` resolves to, after the env override and
        # before the built-in repo root â€” so zero-config is unchanged and a
        # chosen folder takes effect on the next run without a restart.
        from .code.roots import TrustedRootStore  # noqa: PLC0415
        from .code.workspace import set_trusted_root_lookup, set_trusted_root_provider  # noqa: PLC0415

        self.trusted_roots = TrustedRootStore(self.store)
        set_trusted_root_provider(self._selected_trusted_root)
        # Phase 23: a per-chat project binds ONE approved root by id, without
        # making it the global selection. The lookup can only return a row that
        # exists in TrustedRoot, so the per-run path is still an approved one.
        set_trusted_root_lookup(self._trusted_root_by_id)
        # Browser Automation Runtime: ONE process-wide manager owning every live
        # session, the provider registry/router, the vault and the profile store.
        # Its tools register on the SAME registry, so validation, permission and
        # the effect authorizer apply unchanged. Construction only reads config;
        # no browser is launched until a `browser_*` tool requests one. When the
        # capability is disabled (or Playwright is absent) the toolset is simply
        # not registered, so the model is never told about a tool that refuses.
        try:
            from .browser.runtime import build_browser_manager, set_browser_manager  # noqa: PLC0415
            from .browser.tools import register_browser_tools  # noqa: PLC0415

            self.browser = build_browser_manager(agent_store=self.store, bus=self.bus)
            set_browser_manager(self.browser)
            register_browser_tools(self.registry)
        except Exception:  # noqa: BLE001 - the browser is optional; never block boot
            self.browser = None
            log.warning("browser subsystem unavailable", exc_info=True)
        # Computer Use Engine: ONE process-wide manager owning every desktop
        # session, the platform driver, the accessibility/OCR/vision engines, the
        # takeover leases and the finance firewall. Its tools register on the SAME
        # registry, so validation, permission and the effect authorizer apply
        # unchanged. Construction only reads config and builds a driver; nothing is
        # captured or clicked until a `computer_*` tool asks. When the capability is
        # disabled (or the machine has no driver) the toolset is simply not
        # registered, so the model is never told about a tool that refuses.
        try:
            from .computer.runtime import build_computer_manager, set_computer_manager  # noqa: PLC0415
            from .computer.tools import register_computer_tools  # noqa: PLC0415

            self.computer = build_computer_manager(
                agent_store=self.store, bus=self.bus, memory_service=self.memory,
            )
            set_computer_manager(self.computer)
            register_computer_tools(self.registry)
        except Exception:  # noqa: BLE001 - Computer Use is optional; never block boot
            self.computer = None
            log.warning("computer use subsystem unavailable", exc_info=True)
        # Vision / Multimodal subsystem: ONE process-wide content-addressed asset
        # store bound to the same agent-memory connection (binary lives on the
        # gitignored filesystem tier; only metadata is relational). Its tools
        # register on the SAME registry and route every model call through the
        # existing model runtime â€” native-first, with an auxiliary vision-model
        # fallback. A failure to construct only means the tools report honest
        # capability states, so it never blocks boot.
        try:
            from .vision import build_asset_store  # noqa: PLC0415
            from .vision.tools import register_vision_tools  # noqa: PLC0415

            self.vision_assets = build_asset_store(self.store)
            register_vision_tools(self.registry)
        except Exception:  # noqa: BLE001 - vision is optional; never block boot
            self.vision_assets = None
            log.warning("vision subsystem unavailable", exc_info=True)
        # Document Intelligence: ONE process-wide document store bound to the same
        # agent-memory connection (binaries live in the vision asset store; only
        # metadata + text are relational). Its tools register on the SAME registry
        # and reuse the vision subsystem for OCR/rendering/model calls and the
        # EvidenceStore for provenance. A construction failure only makes the tools
        # report honest capability states, so it never blocks boot.
        try:
            from .document_intelligence.store import DocumentStore  # noqa: PLC0415
            from .document_intelligence.tools import register_document_tools  # noqa: PLC0415

            self.document_store = DocumentStore(self.store)
            register_document_tools(self.registry)
        except Exception:  # noqa: BLE001 - documents are optional; never block boot
            self.document_store = None
            log.warning("document intelligence subsystem unavailable", exc_info=True)
        # Unified Retrieval / Search Intelligence Layer: a dedicated SQLite
        # vector + FTS index, a provider-agnostic embedding layer (batching,
        # caching, retry), hybrid retrieval, reranking, provenance and citations.
        # It reads its sources from the ONE agent-memory connection and bridges
        # results into the EvidenceStore; its tools register on the SAME registry.
        # Boot wiring lives in `search/bootstrap.py`; a failure only makes the
        # tools report search unavailable, so it never blocks boot.
        from .search.bootstrap import install_search_layer  # noqa: PLC0415

        install_search_layer(self)
        # Large-corpus sources: registered folders catalogued in place, opened
        # through bounded cards, searched by a routing planner (metadata / FTS /
        # DuckDB / bounded scan). Boot wiring lives in `sources/bootstrap.py`;
        # a failure only makes the tools report the layer unavailable.
        from .sources.bootstrap import install_source_layer  # noqa: PLC0415

        install_source_layer(self)
        # MCP (Phase 5): configured local stdio servers. Construction only reads
        # config; nothing is launched until `start_mcp()`. Selected read-only MCP
        # tools register into THIS registry, so execution stays authoritative.
        self.mcp = MCPManager() if MCP_AVAILABLE else None
        self.mcp_registered_tools: list[str] = []
        # Effectful-tool approvals (5G): persisted in the runtime's own DB and
        # consulted by every run's effect authorizer.
        self.approvals = ApprovalStore(self.store) if MCP_AVAILABLE else None
        # MCP integration agent tools (catalog + install). The install operation
        # itself lives in `mcp/install_ops.py`, shared with the operator route.
        from .mcp.install_tools import register_mcp_install_tools  # noqa: PLC0415

        register_mcp_install_tools(self.registry)
        # The trading Journal is front-door-owned (Prisma); these tools reach the
        # screen's own route over the loopback callback (ADR: one store, no second
        # book). The add is approval-gated; the list is read-only.
        from .journal_tools import register_journal_tools  # noqa: PLC0415

        register_journal_tools(self.registry)
        # The money-movement guardrails: scam/coercion, impaired spending, crisis,
        # aggregate limits and the money confirm gate. Boot wiring lives in
        # `finance_guardrails/bootstrap.py`; it reuses the ONE agent-memory
        # connection and the ONE single-use approval store, and registers its
        # read-only status/check tools on the SAME registry.
        from .finance_guardrails.bootstrap import install_finance_guardrail_layer  # noqa: PLC0415

        install_finance_guardrail_layer(self)
        # Broker connectivity (P0, read-only): the adapter registry, the durable
        # connection store and the read-only broker tools. Boot wiring lives in
        # `trade/bootstrap.py`; nothing connects to a broker at boot and there is
        # no order-submission path here.
        from .trade.bootstrap import install_broker_layer  # noqa: PLC0415

        install_broker_layer(self)
        # The Security subsystem (ADR-H159): the ONE decision path (Security
        # Gateway + Permission/Policy/Risk/Approval/Capability engines + Security
        # Floor + secrets + network policy + audit) over every privileged effect.
        # It is additive to the per-subsystem policies and never replaces the
        # approval store; best-effort so it can never block construction.
        try:
            from .security.bootstrap import install_security_layer  # noqa: PLC0415

            self.security = install_security_layer(self)
        except Exception:  # noqa: BLE001 - security must never block construction
            self.security = None
            log.warning("security subsystem unavailable", exc_info=True)
        # Per-tenant physical data isolation: each tenant's runs/sessions/memory
        # live in their own agent-memory file under the tenant root. The default
        # store above stays the single-tenant/operator store.
        try:
            from .security.tenant_store import TenantStoreRegistry  # noqa: PLC0415

            self.tenant_stores = TenantStoreRegistry()
        except Exception:  # noqa: BLE001
            self.tenant_stores = None
        # Channel Gateway: connect external messaging surfaces (Telegram, Discord,
        # Email, Slack, â€¦) so a user can see and drive Eclipse remotely. Boot wiring
        # lives in `channels/bootstrap.py`; it reuses the ONE agent-memory
        # connection, the ONE tool registry (for the operator `/do` bridge), the
        # Security Gateway (secrets + audit + effect authorization) and the ONE
        # `service.run` agent path. Provider pollers start in the lifespan.
        try:
            from .channels.bootstrap import install_channel_layer  # noqa: PLC0415

            install_channel_layer(self)
        except Exception:  # noqa: BLE001 - channels must never block construction
            log.warning("channel gateway unavailable", exc_info=True)
        # Automatic learning review (M3): a lightweight in-process queue that
        # reviews completed runs after the fact. It creates ONLY inert records /
        # suggestions and never enables a skill. Started/stopped with the runtime.
        from .learning_review import LearningReviewQueue

        self.learning_review = LearningReviewQueue(self.memory, self.store)

        # Memory providers (M8): the native provider plus a candidate provider per
        # configured MCP server. Discovery is not activation â€” the router only
        # reaches an external provider when policy enables it AND the caller
        # explicitly allows it AND the server is connected.
        from .memory_providers import build_memory_router

        self.memory_router = build_memory_router(self.memory, mcp_manager=self.mcp)

        # Model Runtime (provider-agnostic model layer): route usage/health into
        # this service's own store so model requests are persisted in
        # agent-memory.db â€” never in the financial JDdS vault.
        try:
            from .model_runtime.store import ModelRuntimeStore

            self.model_runtime_store = ModelRuntimeStore(self.store)
            get_runtime().install_persistence(self.model_runtime_store)
            get_runtime().set_observer(self._observe_model_event)
        except Exception:  # noqa: BLE001 - accounting wiring must not block boot
            self.model_runtime_store = None
            log.warning("model runtime persistence wiring skipped", exc_info=True)

        # The Task / Plan runtime: the durable execution backbone. Boot wiring
        # lives in `tasks/bootstrap.py`; it reuses the ONE agent-memory connection
        # for its tables, the process-wide bus for live events, and dispatches each
        # claimed task through THIS service's `run` (never a second agent loop). A
        # failure only leaves `self.tasks` None, so it never blocks boot.
        from .tasks.bootstrap import install_task_layer  # noqa: PLC0415

        install_task_layer(self)
        # The Artifact Center: the durable catalog of produced files. Boot wiring
        # lives in `artifacts/bootstrap.py`; it reuses the ONE agent-memory
        # connection for its tables and a confined, content-addressed local store
        # for bytes. A failure only leaves `self.artifacts` None, so it never
        # blocks boot. A bounded reconciler is started in the lifespan.
        from .artifacts.bootstrap import install_artifact_layer  # noqa: PLC0415

        install_artifact_layer(self)
        # User personas (Settings â†’ Persona): a bounded, append-only overlay on
        # the built-in prompt, stored on the same connection. Independent of the
        # graph layer; a failure only leaves personas unavailable.
        from .personas import install_persona_store  # noqa: PLC0415

        install_persona_store(self)
        # The Graph runtime: the compiler + deterministic executor for the
        # canonical Workflow IR the visual editor authors. Boot wiring lives in
        # `graph_runtime/bootstrap.py` (NOT `graph.py`, which is the tool loop);
        # it reuses the ONE agent-memory connection for run rows and owns no
        # scheduler. A failure only leaves `self.graphs` None.
        from .graph_runtime.bootstrap import install_graph_layer  # noqa: PLC0415

        install_graph_layer(self)
        # Document Authoring: the WRITE side of the document capability. Registers
        # `document_create`, which builds real Word/Excel/PowerPoint/Markdown/CSV
        # files from a neutral vocabulary and records them in the Artifact Center
        # above. It owns no store; a missing writer library or artifact service is
        # reported honestly per call, so it never blocks boot.
        try:
            from .documents.tools import register_document_authoring_tools  # noqa: PLC0415

            register_document_authoring_tools(self.registry)
        except Exception:  # noqa: BLE001 - authoring is optional; never block boot
            log.warning("document authoring subsystem unavailable", exc_info=True)
        # UI Engineering runtime: the component catalog + declarative blueprint
        # engine + `ui_*` agent tools, on the ONE agent-memory connection. A
        # failure only leaves `self.ui` None, so it never blocks boot.
        from .ui.bootstrap import install_ui_layer  # noqa: PLC0415

        install_ui_layer(self)
        from .kernel.services.wiring import install_kernel_surfaces  # noqa: PLC0415

        install_kernel_surfaces(self)
        # Eclipse SDK: the user-facing composition surface over the UI and
        # Portfolio services (declarative widgets, composed pages, fund
        # portfolios, design + passphrase locks). It owns no second engine; the
        # lock registry is enforced by those services. Best-effort.
        from .sdk.bootstrap import install_sdk_layer  # noqa: PLC0415

        install_sdk_layer(self)
        # The Plugin Platform: ONE process-wide manager owning the registry, the
        # runtime instances, the event bridge, the hook registry, per-plugin
        # config/secrets/storage and health. Boot wiring lives in
        # `plugins/bootstrap.py`; it reuses the ONE agent-memory connection, the
        # ONE tool registry and the process-wide bus. Plugins enabled before the
        # last shutdown are reloaded by `start_plugins` in the lifespan.
        from .plugins.bootstrap import install_plugin_layer  # noqa: PLC0415

        install_plugin_layer(self)
        # Bot / Automation tools: the chat agent manages the SAME bots the Bots
        # workspace shows. Thin wrappers over `self.automation_service()` on the
        # ONE tool registry â€” never a second scheduler. A failure never blocks boot.
        try:
            from .automation.tools import register_automation_tools, set_automation_service  # noqa: PLC0415

            set_automation_service(self.automation_service())
            register_automation_tools(self.registry)
            # Scheduling tools (Phase 17): `job_*` over the SAME catalogue +
            # service. The operator's master switch (`agent_scheduling_enabled`)
            # removes the whole surface when off; creation is always disabled.
            from .automation.scheduling_tools import register_scheduling_tools  # noqa: PLC0415

            register_scheduling_tools(self.registry)
            # Bulk acquisition (Phase 16 slice): the `acquire` tool.
            from .acquire.tools import register_acquire_tools  # noqa: PLC0415

            register_acquire_tools(self.registry)
        except Exception:  # noqa: BLE001 - bot tools are optional; never block boot
            log.warning("automation tools wiring skipped", exc_info=True)
        # Observability platform: ONE process-wide hub fed by this service's
        # universal event bus (runs, models, tools, jobs, plugins, A2A, events)
        # and the model-runtime observer. It is fail-open: a telemetry backend
        # outage can never break a run. Installed LAST so every subsystem it
        # observes already exists. Never blocks boot.
        try:
            from .observability.integration import install_observability  # noqa: PLC0415

            install_observability(self)
        except Exception:  # noqa: BLE001 - observability is optional; never block boot
            self.observability = None
            log.warning("observability platform unavailable", exc_info=True)

    def _observe_model_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Forward model-runtime observability events onto the harness bus.

        These carry no run id (a routing decision can precede the run's emitter)
        so they are published as process-level events, not persisted per run.
        """
        try:
            self.bus.publish({"type": event_type, "data": data, "at": __import__("time").time()})
        except Exception:  # noqa: BLE001 - observability must never break a run
            pass

    def _publish_code_intel_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Forward CIUE observability events (LSP start/stop/crash, indexing) to the bus.

        Process-level events (an LSP start has no run id), so they are published
        for any global subscriber and never persisted against a run.
        """
        try:
            self.bus.publish({"type": event_type, "data": data, "at": __import__("time").time()})
        except Exception:  # noqa: BLE001
            pass

    def start_code_intel(self) -> dict[str, Any]:
        """Best-effort CIUE startup. Deliberately does NOT index at boot.

        Indexing the whole repository at boot would run on every process start and
        every test client â€” expensive and unnecessary. The index is built LAZILY and
        incrementally: the first `code_*` query refreshes the index on a worker
        thread (`manager._ensure_fresh`), and the per-workspace watcher keeps it
        current afterwards. Language servers also start lazily on first use. A
        failure here is a diagnostic, never a boot blocker.
        """
        runtime = getattr(self, "code_intel", None)
        if runtime is None:
            return {"available": False}
        try:
            from .code.workspace import default_root  # noqa: PLC0415

            root = default_root()
            return {
                "available": True,
                "root": str(root) if root else None,
                "supportedLanguages": runtime.lsp.supported_languages(),
            }
        except Exception:  # noqa: BLE001
            log.warning("CIUE startup probe failed", exc_info=True)
            return {"available": True, "root": None}

    async def stop_code_intel(self) -> None:
        runtime = getattr(self, "code_intel", None)
        if runtime is None:
            return
        try:
            runtime.stop()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            log.warning("CIUE shutdown failed", exc_info=True)

    def _selected_trusted_root(self) -> Optional[str]:
        """The path of the selected trusted root, or None. Never raises."""
        try:
            selected = self.trusted_roots.selected()
            return selected.path if selected else None
        except Exception:  # noqa: BLE001 - a root lookup must never break a run
            return None

    def _trusted_root_by_id(self, root_id: str) -> Optional[str]:
        """The path of an APPROVED root id, or None. Never raises."""
        from pathlib import Path  # noqa: PLC0415

        try:
            root = self.trusted_roots.get(str(root_id))
        except Exception:  # noqa: BLE001 - a root lookup must never break a run
            return None
        if root is None:
            return None
        try:
            return root.path if Path(root.path).is_dir() else None
        except Exception:  # noqa: BLE001
            return None

    def trusted_root_store(self) -> Any:
        """The process-wide trusted-roots registry (folder picker)."""
        return self.trusted_roots

    def _project_root(self) -> Optional[str]:
        """The project root whose local skills may apply, or None.

        The runtime is spawned by the front door from the repo root, so the
        default is the repo containing the harness. `BLACK_ECLIPSE_PROJECT_ROOT`
        overrides it (tests / a workspace pointed at another checkout).
        """
        override = os.environ.get("BLACK_ECLIPSE_PROJECT_ROOT")
        if override:
            return os.path.abspath(override)
        try:
            from .eclipse_memory.config import repo_root  # noqa: PLC0415
            return repo_root()
        except Exception:  # noqa: BLE001
            return None

    def _project_discovery_enabled(self) -> bool:
        try:
            return bool(self.memory.policy.get("project_skill_discovery"))
        except Exception:  # noqa: BLE001 - a policy read failure must fail closed
            return False

    def reload_project_skills(self) -> list[str]:
        """Re-scan every skill tier after a trust/install decision (no restart).

        Delegates to the registry's remembered boot configuration, so installed
        (GitHub) skills are re-read alongside project skills; untrusted roots
        contribute notices, not skills.
        """
        self.skill_load_errors = self.skill_registry.reload()
        return self.skill_load_errors

    def refresh_memory_router(self) -> None:
        """Rebuild the provider router after an MCP config change."""
        from .memory_providers import build_memory_router

        external = False
        try:
            external = bool(self.memory.policy.get("external_memory_search_enabled"))
        except Exception:  # noqa: BLE001
            external = False
        self.memory_router = build_memory_router(
            self.memory, mcp_manager=self.mcp, external_enabled=external
        )

    def _secret_resolver(self, bridge: DataBridge):
        """Bind an async vault-secret resolver to a run's data bridge.

        The MCP manager calls this at child spawn; the value is fetched through
        the authenticated, read-only bridge and never stored or logged.
        """

        async def resolve(name: str) -> Optional[str]:
            try:
                payload = await bridge.fetch("mcp_secret_resolve", {"name": name})
            except Exception:  # noqa: BLE001 - an unreachable vault is not a value
                return None
            if isinstance(payload, dict) and payload.get("status") == "OK":
                value = payload.get("value")
                return str(value) if value else None
            return None

        return resolve

    def start_terminal(self) -> dict[str, Any]:
        """Reconcile persisted terminal state on boot (never blocks startup).

        The registry/manager were built in `__init__`; this only reconciles the
        rows left from a previous process â€” a dead pid is marked EXITED, a live
        one UNKNOWN (owned but unmanaged), every session is closed. It NEVER
        fabricates a running process and never raises.
        """
        try:
            summary = self.terminal.recovery.reconcile()
            log.info(
                "terminal recovery reconciled %s process record(s), %s session record(s)",
                summary.get("processes"), summary.get("sessions"),
            )
            # Pre-warm one idle shell in the background, so the FIRST UI open
            # adopts an already-running shell and shows a prompt instantly
            # instead of paying PowerShell's startup cost. Best-effort.
            try:
                threading.Thread(target=self.terminal.warm_shell, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
            return summary
        except Exception:  # noqa: BLE001 - recovery must never block boot
            log.warning("terminal recovery failed", exc_info=True)
            return {"processes": 0, "sessions": 0, "reconciled": [], "error": True}

    async def stop_terminal(self) -> None:
        """Stop every background process and close every session on shutdown."""
        try:
            self.terminal.close_all()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            log.warning("terminal shutdown failed", exc_info=True)

    def start_browser(self) -> dict[str, Any]:
        """Wire the browser vault's secret resolver and start the idle sweeper.

        The resolver uses the SAME authenticated, read-only bridge the MCP secret
        path uses, so a browser credential is resolved through Eclipse's existing
        encrypted vault tier â€” never a plaintext store invented here. A failure is
        a diagnostic, never a boot blocker.
        """
        manager = getattr(self, "browser", None)
        if manager is None:
            return {"available": False}
        try:
            bridge = self.build_bridge(_default_callback_base_url(), None)
            manager.vault.set_resolver(self._secret_resolver(bridge))
        except Exception:  # noqa: BLE001 - an unreachable vault is simply no value
            log.warning("browser vault resolver wiring skipped", exc_info=True)
        return {"available": True, "provider": manager.config.default_provider}

    async def start_browser_sweeper(self) -> None:
        manager = getattr(self, "browser", None)
        if manager is None:
            return
        try:
            await manager.start()
        except Exception:  # noqa: BLE001 - the sweeper must never block boot
            log.warning("browser sweeper failed to start", exc_info=True)

    async def stop_browser(self) -> None:
        """Close every browser and stop the sweeper on shutdown (never raises)."""
        manager = getattr(self, "browser", None)
        if manager is None:
            return
        try:
            await manager.stop()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            log.warning("browser shutdown failed", exc_info=True)

    def start_computer(self) -> dict[str, Any]:
        """Start the Computer Use driver and the idle sweeper (never blocks boot).

        A failure is a diagnostic, never a boot blocker â€” exactly like the browser.
        The vision router is built best-effort from the resolved model settings so
        a configured multimodal model is available without extra wiring.
        """
        manager = getattr(self, "computer", None)
        if manager is None:
            return {"available": False}
        try:
            vision = self._build_vision_router()
        except Exception:  # noqa: BLE001 - vision is optional; a router failure is fine
            vision = None
        self._vision_start_task = asyncio.ensure_future(manager.start(vision=vision))
        return {"available": True, "driver": manager.driver.id}

    def _build_vision_router(self) -> Any:
        """Build a vision router from the runtime's default model settings.

        Local vs cloud is decided by the resolved provider; a model that cannot
        accept images simply reports unavailable. This never invents a model. An
        explicit `computer.vision_model` overrides the resolved model id (the
        config field was previously parsed but ignored). A cloud model is wired as
        the CLOUD backend, never the local one, so a sensitive frame is refused
        rather than silently sent to a cloud provider.
        """
        from .computer.config import computer_config  # noqa: PLC0415
        from .computer.vision.router import VisionRouter  # noqa: PLC0415
        from .computer.vision.backends import LocalModelVision, UnavailableVision  # noqa: PLC0415

        cfg = computer_config()
        try:
            from .model_runtime import get_runtime  # noqa: PLC0415

            runtime = get_runtime()
            resolution = runtime.resolve_for_run(None, task="vision")
            settings = getattr(resolution, "settings", None)
            if settings is None:
                return VisionRouter(local=UnavailableVision())
            if cfg.vision_model:
                try:
                    settings = settings.model_copy(update={"model": cfg.vision_model})
                except Exception:  # noqa: BLE001 - a non-pydantic settings object
                    pass
            provider = str(getattr(settings, "provider", "") or "").lower()
            local = any(marker in provider for marker in ("ollama", "local", "lmstudio", "vllm"))
            if local:
                return VisionRouter(local=LocalModelVision(settings=settings, local=True))
            # A cloud model is a CLOUD backend; sensitive frames stay refused.
            return VisionRouter(
                local=UnavailableVision("no local vision model is configured"),
                cloud=LocalModelVision(settings=settings, local=False),
            )
        except Exception:  # noqa: BLE001 - no model â‡’ honest unavailable vision
            return VisionRouter(local=UnavailableVision())

    async def start_computer_sweeper(self) -> None:
        manager = getattr(self, "computer", None)
        if manager is None:
            return
        try:
            task = getattr(self, "_vision_start_task", None)
            if task is not None:
                await task
            await manager.start_sweeper()
        except Exception:  # noqa: BLE001 - the sweeper must never block boot
            log.warning("computer sweeper failed to start", exc_info=True)

    async def stop_computer(self) -> None:
        """Stop every deskop session and the driver on shutdown (never raises)."""
        manager = getattr(self, "computer", None)
        if manager is None:
            return
        try:
            await manager.stop()
        except Exception:  # noqa: BLE001
            log.warning("computer shutdown failed", exc_info=True)

    def stop_execution(self) -> None:
        """Stop execution services and the scheduler on shutdown (never raises)."""
        layer = getattr(self, "execution", None)
        if layer is None:
            return
        try:
            layer.stop()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            log.warning("execution shutdown failed", exc_info=True)

    def reconcile_application_runtime(self) -> dict[str, Any]:
        """Reconcile the application runtimes left by a previous process (never raises).

        A runtime that was RUNNING is re-examined: if its processes/containers are
        gone it becomes DEGRADED. Eclipse never fabricates a running runtime.
        """
        manager = getattr(self, "application_runtime", None)
        if manager is None:
            return {"status": "ok", "reconciled": [], "note": "no application runtime"}
        try:
            result = manager.reconcile()
            log.info("application runtime reconciled %s runtime(s)", result.get("count", 0))
        except Exception:  # noqa: BLE001 - reconciliation must never block boot
            log.warning("application runtime reconciliation failed", exc_info=True)
            return {"status": "error", "reconciled": []}
        return result

    def stop_application_runtime(self) -> None:
        """Stop every application this process started, on shutdown (never raises)."""
        manager = getattr(self, "application_runtime", None)
        if manager is None:
            return
        try:
            for handle in list(manager._runtimes.values()):  # noqa: SLF001
                if handle.state.value not in ("STOPPED", "FAILED"):
                    manager.stop(handle.runtime_id)
        except Exception:  # noqa: BLE001 - shutdown must never raise
            log.warning("application runtime shutdown failed", exc_info=True)

    async def start_mcp(self) -> None:
        """Connect enabled MCP servers and register selected read-only tools.

        Called once at service startup. A server failure is recorded as a
        diagnostic and does not block the runtime or other servers. When the MCP
        SDK is absent, this is a no-op and MCP reports UNAVAILABLE honestly.
        """
        if self.mcp is None:
            return
        try:
            # A process-wide bridge is used only for secret resolution at spawn;
            # per-run tools still receive their own run bridge.
            bridge = self.build_bridge(
                _default_callback_base_url(), None
            )
            self.mcp.set_secret_resolver(self._secret_resolver(bridge))
            self.mcp.set_bridge_fetch(self._raw_bridge_fetch(bridge))
            # Process-level MCP events (e.g. `mcp.catalog.changed`) have no run id;
            # publish them on the shared bus so any global subscriber sees them.
            # The server card additionally surfaces the same facts via lifecycle
            # status, so the signal is not lost when nothing is subscribed.
            self.mcp.set_event_sink(self._publish_mcp_event)
            await self.mcp.start()
            self.mcp_registered_tools = self.mcp.register_selected_tools(self.registry)
        except Exception:  # noqa: BLE001 - MCP must never prevent the runtime booting
            log.warning("MCP startup failed", exc_info=True)

    async def _publish_mcp_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish a process-level MCP event on the shared bus (no run id).

        Content-free: MCP events carry counts and identity only, never content.
        """
        from .persistence import now_iso

        self.bus.publish({
            "id": f"mcp_{event_type}_{now_iso()}",
            "runId": None,
            "type": event_type,
            "seq": 0,
            "at": now_iso(),
            "data": data or {},
        })

    def _raw_bridge_fetch(self, bridge: DataBridge):
        """A raw `(capability, params) -> dict` fetch for the OAuth token store."""

        async def fetch(capability: str, params: dict[str, Any]) -> dict[str, Any]:
            return await bridge.fetch(capability, params)

        return fetch

    def _sampling_model_factory(self, request: RunRequest):
        """Build the `(policy, prompt) -> str` factory for a server sampling call.

        Uses the runtime's OWN model adapter (`models.build_chat_model`) with the
        RUN's credentials, capped by the policy's max tokens. Sampling stays off
        by default; this only ever runs when the operator enabled it for a server.
        """
        settings = request.modelSettings

        async def factory(policy: Any, prompt: str) -> str:
            from langchain_core.messages import HumanMessage

            from .models import build_chat_model

            runtime = get_runtime()
            resolution = runtime.resolve_for_run(settings, task="simple_chat")
            model = runtime.build(
                resolution.settings, builder=build_chat_model, adapter=resolution.adapter
            )
            # Best-effort token cap (providers that accept `max_tokens`).
            try:
                bound = model.bind(max_tokens=policy.clamped_max_tokens())
            except Exception:  # noqa: BLE001 - a provider may not accept the kwarg
                bound = model
            response = await bound.ainvoke([HumanMessage(content=str(prompt))])
            content = getattr(response, "content", None)
            if isinstance(content, list):
                return "\n".join(str(getattr(b, "text", b)) for b in content)
            return str(content if content is not None else response)

        return factory

    async def stop_mcp(self) -> None:
        if self.mcp is None:
            return
        try:
            await self.mcp.close()
        except Exception:  # noqa: BLE001
            log.warning("MCP shutdown failed", exc_info=True)

    def run_startup_maintenance(self) -> dict[str, Any]:
        """Run one-time memory + skill maintenance at runtime startup.

        Currently the retention sweep (due AUTOMATIC memories become inert) plus
        the deterministically-computed skill maintenance pass (report-only by
        default; it can only age enabled, unpinned, unused skills and never
        deletes). Both are idempotent, best-effort, and NEVER raise â€” a failure
        must not stop the application or agent runtime from starting. There is no
        new daemon or thread; this reuses the existing FastAPI lifespan hook.
        """
        try:
            result = self.memory.run_retention_maintenance()
        except Exception:  # noqa: BLE001 - startup maintenance must never block boot
            log.warning("startup memory maintenance failed", exc_info=True)
            result = {"ok": False, "error": "maintenance failed"}
        try:
            result["skillMaintenance"] = self.run_skill_maintenance()
        except Exception:  # noqa: BLE001 - skill maintenance must never block boot
            log.warning("startup skill maintenance failed", exc_info=True)
            result["skillMaintenance"] = {"ok": False, "error": "maintenance failed"}
        # W5.2 curator: report-only by default, never blocks boot, never enables.
        try:
            from .skills.curator import CuratorPolicy, maybe_run_curator  # noqa: PLC0415

            policy = CuratorPolicy.from_flags()
            if policy.mode != "off":
                result["skillCurator"] = maybe_run_curator(
                    self.skill_store, self.skill_registry, policy=policy,
                    maintenance_policy=self.memory.policy.get_all(),
                )
        except Exception:  # noqa: BLE001 - the curator must never block boot
            log.warning("startup skill curator failed", exc_info=True)
            result["skillCurator"] = {"ok": False, "error": "curator failed"}
        # Learning queue hygiene: legacy/nonconforming candidates are retained as
        # rejected, so they cannot masquerade as a ready backlog.
        try:
            from .self_improvement.loop_health import (  # noqa: PLC0415
                quarantine_nonconforming_candidates,
            )

            result["candidateQuarantine"] = quarantine_nonconforming_candidates(
                self.skill_store, dry_run=False,
            )
        except Exception:  # noqa: BLE001 - quarantine must never block boot
            log.warning("candidate quarantine failed", exc_info=True)
            result["candidateQuarantine"] = {"ok": False, "error": "quarantine failed"}
        # Autonomous creation: draft every detected, falsifiable,
        # recurrence-backed candidate. Drafts are inert; evaluation and
        # promotion remain operator actions.
        try:
            from .skills.draft import auto_draft_ready_candidates  # noqa: PLC0415

            result["candidateDrafts"] = auto_draft_ready_candidates(
                self.skill_store, limit=100,
            )
        except Exception:  # noqa: BLE001 - drafting must never block boot
            log.warning("candidate auto-draft failed", exc_info=True)
            result["candidateDrafts"] = {"ok": False, "error": "draft failed"}
        # Creation â†’ deterministic evaluation is automatic; enabling is not.
        try:
            from .skills.draft import evaluate_drafted_skills  # noqa: PLC0415

            execution_invoker = self.build_execution_eval_invoker()
            result["learnedSkillEvaluation"] = evaluate_drafted_skills(
                self.skill_registry, limit=50, execution_invoker=execution_invoker,
            )
            from .skills.draft import evaluate_used_skills  # noqa: PLC0415

            result["learnedSkillUsageEvaluation"] = evaluate_used_skills(
                self.skill_registry, min_uses=3, limit=50,
                execution_invoker=execution_invoker,
            )
        except Exception:  # noqa: BLE001 - evaluation must never block boot
            log.warning("learned skill evaluation failed", exc_info=True)
            result["learnedSkillEvaluation"] = {"ok": False, "error": "evaluation failed"}
        # Agent-curated memory: merge exact/near duplicates, but leave a genuine
        # contradiction for review (resolve_conflicts=False). Rows are retained.
        try:
            from .eclipse_memory.consolidation import consolidate_active  # noqa: PLC0415

            result["memoryConsolidation"] = consolidate_active(
                self.memory.store, limit=200, dry_run=False,
                resolve_conflicts=False,
            )
            result["memoryPromotion"] = self.memory.promote_confirmed_proposals()
        except Exception:  # noqa: BLE001 - consolidation must never block boot
            log.warning("startup memory consolidation failed", exc_info=True)
            result["memoryConsolidation"] = {"ok": False, "error": "consolidation failed"}
        # Legacy review debt: mark pre-contract rows once; never rewrite history.
        try:
            from .self_improvement.loop_health import CONTRACT_SINCE  # noqa: PLC0415

            result["legacyReviewsMarked"] = self.store.mark_legacy_reviews(
                CONTRACT_SINCE,
            )
            advisory = getattr(self, "advisory", None)
            if advisory is not None and hasattr(advisory.store, "mark_legacy_unscored"):
                result["legacyAdviceMarked"] = advisory.store.mark_legacy_unscored(
                    CONTRACT_SINCE,
                )
        except Exception:  # noqa: BLE001 - marking must never block boot
            log.warning("legacy review/advice marking failed", exc_info=True)
            result["legacyReviewsMarked"] = 0
            result["legacyAdviceMarked"] = 0
        # Cross-session recall: index completed runs that predate indexing.
        try:
            from .session_index import backfill_run_index  # noqa: PLC0415

            result["sessionIndexBackfill"] = backfill_run_index(self.store, limit=200)
        except Exception:  # noqa: BLE001 - backfill must never block boot
            log.warning("session index backfill failed", exc_info=True)
            result["sessionIndexBackfill"] = {"ok": False, "error": "backfill failed"}
        try:
            from .code.worktree import sweep_expired  # noqa: PLC0415

            result["codeWorkspaceSweep"] = {"released": sweep_expired()}
        except Exception:  # noqa: BLE001 - the sweep must never block boot
            log.warning("code workspace sweep failed", exc_info=True)
            result["codeWorkspaceSweep"] = {"ok": False, "error": "sweep failed"}
        # Vision assets: a sensitive frame is stored with an expiry; the sweep
        # deletes expired asset rows AND their files. Without this the TTL would
        # be a claim rather than a behavior (and disk would grow unbounded).
        try:
            vision_assets = getattr(self, "vision_assets", None)
            if vision_assets is not None:
                result["visionAssetSweep"] = {"removed": vision_assets.sweep_expired()}
        except Exception:  # noqa: BLE001 - the sweep must never block boot
            log.warning("vision asset sweep failed", exc_info=True)
            result["visionAssetSweep"] = {"ok": False, "error": "sweep failed"}
        # Orphaned agent runs: the runtime executes runs in-process, so any run
        # left `created`/`running` by a previous process can never finish. Mark
        # it failed with a durable run.failed event so a watching client sees a
        # terminal state instead of waiting forever. Guarded so a duplicate
        # process that is about to fail its port bind does NOT reconcile (and
        # orphan) the runs a live runtime is executing â€” see `run_recovery`.
        try:
            from .run_lease import RunLeaseManager  # noqa: PLC0415
            from .run_recovery import reconcile_orphaned_runs  # noqa: PLC0415

            lease_manager = RunLeaseManager(self.store)
            self.run_lease = lease_manager
            conflicts = lease_manager.register_runtime()
            if conflicts:
                log.warning(
                    "runtime identity: another live runtime shares this database: %s",
                    ", ".join(conflicts),
                )
            result["runtimeId"] = lease_manager.runtime_id
            result["runtimeConflicts"] = conflicts
            result["orphanedRuns"] = reconcile_orphaned_runs(
                self.store,
                "the agent runtime restarted while this run was in flight",
                environment=lease_manager.environment,
            )
        except Exception:  # noqa: BLE001 - reconciliation must never block boot
            log.warning("orphaned run reconciliation failed", exc_info=True)
            result["orphanedRuns"] = {"ok": False, "error": "reconciliation failed"}
        # Forensics: a restart must leave a record of which process owns which
        # database and how many runs it reconciled. Without this, an orphaned
        # run cannot be attributed to a boot, a kill, or a second runtime.
        try:
            schema_version = None
            try:
                from .migrations import MigrationRunner  # noqa: PLC0415
                from .run_lineage import RunLineage  # noqa: PLC0415

                schema_version = MigrationRunner(
                    RunLineage(self.store)._db, "agent-memory"
                ).current_version()
            except Exception:  # noqa: BLE001 - a log field must never block boot
                schema_version = None
            log.info(
                "runtime boot: pid=%s env=%s port=%s db=%s schema=%s orphaned=%s",
                os.getpid(),
                os.environ.get("BE_PROFILE") or "prod",
                os.environ.get("HARNESS_PY_PORT") or "unknown",
                getattr(self.store, "path", getattr(self.store, "_path", "unknown")),
                schema_version,
                result.get("orphanedRuns"),
            )
        except Exception:  # noqa: BLE001 - forensics must never block boot
            pass
        return result

    def build_execution_eval_invoker(self, model_settings: Any = None) -> Any:
        """The W5.1 execution runner.

        Precedence: an explicit ``model_settings`` from the calling request (the
        front door resolves the app's configured model), then
        ``ECLIPSE_SKILL_EVAL_MODEL=provider:model``. With neither, returns
        ``None`` and the evaluator records ``needs_review`` — a missing model is
        never simulated into a pass.
        """
        import os  # noqa: PLC0415

        try:
            from .schemas import ModelSettings  # noqa: PLC0415
            from .skills.execution_eval import RuntimeSkillInvoker  # noqa: PLC0415

            if isinstance(model_settings, ModelSettings):
                settings = model_settings
            elif isinstance(model_settings, dict) and model_settings.get("provider") and model_settings.get("model"):
                settings = ModelSettings.model_validate(model_settings)
            else:
                raw = (os.environ.get("ECLIPSE_SKILL_EVAL_MODEL") or "").strip()
                if not raw or ":" not in raw:
                    return None
                provider, model = raw.split(":", 1)
                provider, model = provider.strip(), model.strip()
                if not provider or not model:
                    return None
                settings = ModelSettings(
                    provider=provider,
                    model=model,
                    api_key=os.environ.get("ECLIPSE_SKILL_EVAL_API_KEY"),
                    base_url=os.environ.get("ECLIPSE_SKILL_EVAL_BASE_URL"),
                )
            return RuntimeSkillInvoker(
                registry=self.skill_registry,
                tool_registry=self.registry,
                data_bridge=self.server_bridge(),
                memory_store=self.store,
                model_settings=settings,
                bus=self.bus,
                memory_service=self.memory,
            )
        except Exception:  # noqa: BLE001 - an eval runner is optional, never boot-critical
            log.warning("skill execution eval runner unavailable", exc_info=True)
            return None

    def run_skill_maintenance(self) -> dict[str, Any]:
        """One skill-maintenance pass (report-only unless policy opts in).

        Idempotent, never raises, no new daemon. Wired into the existing startup
        lifespan exactly like the retention sweep.
        """
        from .skills.maintenance import run_skill_maintenance  # noqa: PLC0415

        return run_skill_maintenance(
            self.skill_store, self.skill_registry, self.memory.policy.get_all(),
        )

    def skill_maintenance_report(self) -> dict[str, Any]:
        """Read-only skill-maintenance report (stale skills, reasons, actions)."""
        from .skills.maintenance import maintenance_report  # noqa: PLC0415

        return maintenance_report(self.skill_store, self.memory.policy.get_all())

    async def start_learning_review(self) -> None:
        """Start the bounded learning-review queue workers (idempotent)."""
        try:
            await self.learning_review.start()
        except Exception:  # noqa: BLE001 - review is optional and must not block boot
            log.warning("learning review queue failed to start", exc_info=True)

    async def stop_learning_review(self) -> None:
        try:
            await self.learning_review.stop()
        except Exception:  # noqa: BLE001
            log.warning("learning review shutdown failed", exc_info=True)

    async def reload_mcp(self) -> dict[str, Any]:
        """Apply a persisted MCP config change without a runtime restart.

        Removes only the MCP tools this manager added, then reconnects and
        re-registers from the current config file. Returns non-secret status.
        """
        from .mcp.config import load_mcp_configs

        if self.mcp is None:
            return {"configured": 0, "connected": 0, "registeredTools": 0, "loadDiagnostics": [], "unavailable": True}
        configs, diagnostics = load_mcp_configs()
        self.mcp.load_diagnostics = diagnostics
        self.mcp_registered_tools = await self.mcp.reload(configs)
        # Rebuild the provider router so newly configured/removed MCP servers are
        # (or are no longer) candidate memory providers. Discovery is not
        # activation: the router still requires explicit policy + call opt-in.
        self.refresh_memory_router()
        return self.mcp.summary()

    async def oauth_login(self, server_id: str, *, client_id: Any = None, scope: str = "") -> dict[str, Any]:
        """Run an OAuth device-code login for a remote MCP server."""
        if self.mcp is None:
            return {"success": False, "reason": "MCP is unavailable (runtime missing the mcp SDK)"}
        return await self.mcp.oauth_login(server_id, client_id=client_id, scope=scope)

    async def oauth_logout(self, server_id: str) -> dict[str, Any]:
        """Clear a server's stored credentials; it stays configured but disconnected."""
        if self.mcp is None:
            return {"success": False, "reason": "MCP is unavailable"}
        return await self.mcp.oauth_logout(server_id)

    def install_mcp_integration(self, integration_id: str) -> dict[str, Any]:
        """Install a preset integration as a DISABLED, nothing-selected config.

        Thin delegate: the operation lives in `mcp/install_ops.py` so this route
        and the agent's `mcp_install` tool share ONE implementation. Nothing
        ships enabled or selected; the operator reviews and activates.
        """
        from .mcp.install_ops import install_integration  # noqa: PLC0415

        if self.mcp is not None:
            self.mcp.load_diagnostics = []
        return install_integration(integration_id)

    async def verify_mcp_server(
        self, server_id: str, *, tool: str, arguments: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Operator-triggered credential verification (one bounded read call).

        Never automatic, never at boot, never in a loop. Distinct from Test,
        which only proves the process started and listed tools.
        """
        if self.mcp is None:
            return {"success": False, "reason": "MCP is unavailable (runtime missing the mcp SDK)"}
        result = await self.mcp.verify_credentials(server_id, tool=tool, arguments=arguments or {})
        return {"success": True, **result}

    def reset_mcp_circuit(self, server_id: str) -> dict[str, Any]:
        """Operator reset of a server's circuit breaker (back to CLOSED)."""
        if self.mcp is None:
            return {"success": False, "reason": "MCP is unavailable"}
        return self.mcp.reset_circuit(server_id)

    async def confirm_mcp_catalog(self, server_id: str) -> dict[str, Any]:
        """Snapshot the live catalog digest as the operator's reviewed version.

        This is the explicit review act that lets an annotation-sourced read-only
        tool activate. Doing it re-reads config, records the digest, and reloads
        so activation takes effect immediately.
        """
        from .mcp.config import load_mcp_configs, upsert_mcp_config

        if self.mcp is None:
            return {"success": False, "reason": "MCP is unavailable"}
        digest = self.mcp.catalog_digest_for(server_id)
        if not digest:
            return {"success": False, "reason": "server is not connected; test it first"}
        configs, _ = load_mcp_configs()
        target = next((c for c in configs if c.id == server_id), None)
        if target is None:
            return {"success": False, "reason": "server not found"}
        upsert_mcp_config(target.model_copy(update={"confirmed_catalog_digest": digest}))
        await self.reload_mcp()
        return {"success": True, "serverId": server_id, "catalogDigest": digest, **self.mcp.summary()}

    def build_bridge(self, base_url: str, token: Optional[str]) -> DataBridge:
        """Build the read-only JDdS bridge for a research request."""
        return _HttpDataBridge(base_url, token)

    def server_bridge(self) -> DataBridge:
        """A bridge for a server-initiated (HTTP route) data fetch.

        Uses the configured front-door callback URL and the operator secret, so
        an operator-triggered backtest can load market data through the SAME
        read-only bridge the agent uses.
        """
        import os  # noqa: PLC0415

        return _HttpDataBridge(_default_callback_base_url(), os.environ.get("CRON_SECRET"))

    def server_market_gateway(self) -> Any:
        """The market gateway for a SERVER-initiated fetch, with a bridge.

        The gateway's bridge is bound per RUN (the front door hands the runtime
        its own callback URL). A route that fetches outside a run - a chart
        that NAMES its series, for instance - had no bridge at all, so the
        fetch could not even be attempted. This binds the server bridge once
        and returns the gateway.
        """
        gateway = getattr(self, "market_gateway", None)
        if gateway is None:
            return None
        if getattr(gateway, "_bridge", None) is None:
            gateway._bridge = self.server_bridge()  # noqa: SLF001 - the same deliberate rebind
        return gateway

    async def retrieve_for_research(self, bridge: DataBridge, request: Any) -> Any:
        """Run ONE retrieval for the research flow through the shared layer/cache.

        Reuses the process-wide retrieval cache and the durable store, exactly
        like the agent tools do â€” research is not a parallel retrieval system.
        """
        from .retrieval import FinancialRetrievalLayer

        layer = FinancialRetrievalLayer(
            bridge, cache=self.retrieval_cache, store=self.store
        )
        return await layer.retrieve(request)

    async def run(self, request: RunRequest) -> RunResult:
        # Phase 22 / S2: a writing chat turn is a build (budget floor, hard-capped).
        apply_chat_build_budget(request)
        record_chat_build(self.store, request, agent_id=str(getattr(request, "agentId", "") or ""))
        bridge = _HttpDataBridge(request.callbackBaseUrl, request.callbackToken)
        # Per-run bindings (market transport + MCP sampling model) travel in a
        # ContextVar. The gateway/MCP manager are process-wide singletons, so
        # writing these onto them would let two concurrent runs overwrite each
        # other. Every await below runs inside `bind_run`.
        sampling_factory = (
            self._sampling_model_factory(request)
            if getattr(self, "mcp", None) is not None
            else None
        )
        # Foreground activity: defer expensive background reviews to a short idle.
        try:
            self.learning_review.mark_user_active()
        except Exception:  # noqa: BLE001 - scheduling hint only
            pass
        # The retrieval layer is built lazily by the retrieval tools from the
        # run context (so its events reach the emitter); only the shared cache is
        # passed here.
        async def _execute() -> RunResult:
            try:
                # One live owner per run. A duplicate submission with the same
                # runId must not execute a second time.
                from .run_binding import bind_run  # noqa: PLC0415
                from .run_lease import RunLeaseGuard  # noqa: PLC0415

                with bind_run(bridge=bridge, sampling_factory=sampling_factory):
                    with RunLeaseGuard(self.store, request.runId) as lease:
                        if not lease.owned:
                            # W6: never write over a run another runtime owns. The
                            # duplicate submission gets a live-only signal; the
                            # owner's row, counters and event log are untouched.
                            self.bus.publish({
                                "id": f"evt_{request.runId}_lease_conflict",
                                "runId": request.runId, "type": "run.lease_conflict",
                                "seq": -1, "at": now_iso(),
                                "data": {"code": "RUN_LEASE_CONFLICT"}, "transient": True,
                            })
                            return RunResult(runId=request.runId, status="failed")
                        return await execute_run(
                            request, self.registry, bridge, self.store, self.bus,
                            runtime_id=(lease.manager.runtime_id if lease.manager else None),
                            **self._run_kwargs(),
                        )
            finally:
                # Close live SSE consumers whatever the terminal status, exactly
                # like the background path does.
                self.bus.end_stream(request.runId)

        # Run in a child task so `cancel()` has something to cancel and
        # `is_active()` is true for the whole run. The done callback owns the
        # registry entry, so an HTTP disconnect cannot leave a stale entry.
        task = asyncio.create_task(
            _with_run_slot(getattr(self, "_run_slots", None), _execute)
        )
        self._active[request.runId] = task
        task.add_done_callback(lambda _t: self._active.pop(request.runId, None))
        try:
            result = await task
        except asyncio.CancelledError:
            if not task.cancelled():
                # The *caller* went away (HTTP disconnect). The run keeps its
                # own task and stays cancellable; do not fake a terminal state.
                raise
            # `cancel()` arrived before execute_run could record its own
            # terminal status (e.g. while waiting for an admission slot).
            self.store.update_run(request.runId, status="cancelled", finishedAt=now_iso())
            await RunEmitter(request.runId, self.store, self.bus).emit(
                "run.cancelled", {"reason": "cancelled"},
            )
            return RunResult(runId=request.runId, status="cancelled")
        await self._schedule_review(request.runId, result.status)
        return result

    def _run_kwargs(self) -> dict[str, Any]:
        """The ONE subsystem-bundle set `execute_run` receives, shared by both
        run paths so the streaming path can never drop a bundle."""
        return {
            "retrieval_cache": self.retrieval_cache,
            "memory_service": self.memory, "skill_registry": self.skill_registry,
            "approvals": self.approvals, "memory_router": self.memory_router,
            "mcp_manager": self.mcp, "code_store": self.code_store,
            "market": self._market_bundle(),
            "portfolio": self._portfolio_bundle(),
            "ledger": self._ledger_bundle(),
            "advisory": getattr(self, "advisory", None),
            "decision": getattr(self, "decision", None),
            "quant": self._quant_bundle(),
            "project_store": getattr(self, "project_store", None),
            "code_intel_runtime": getattr(self, "code_intel", None),
            "evidence_store": getattr(self, "evidence_store", None),
            "learning_store": getattr(self, "learning_store", None),
            "search": getattr(self, "search", None),
            "artifact_service": getattr(self, "artifacts", None),
            "ui_service": getattr(self, "ui", None),
            "task_manager": getattr(self, "tasks", None),
        }

    def _quant_bundle(self) -> Any:
        """The Quant Engine subsystem handed to each run (store)."""
        from .quant.bootstrap import QuantBundle  # noqa: PLC0415

        store = getattr(self, "quant_store", None)
        if store is None:
            return None
        return QuantBundle(store=store, dataset_store=getattr(self, "dataset_store", None))

    def _market_bundle(self) -> Any:
        """The market subsystem handed to each run (gateway/store/stream)."""
        from .market.bootstrap import MarketBundle  # noqa: PLC0415

        gateway = getattr(self, "market_gateway", None)
        if gateway is None:
            return None
        return MarketBundle(gateway=gateway, store=getattr(self, "market_store", None),
                            stream=getattr(self, "market_stream", None))

    def _portfolio_bundle(self) -> Any:
        """The portfolio subsystem handed to each run (service + store)."""
        from .portfolio.bootstrap import PortfolioBundle  # noqa: PLC0415

        portfolio = getattr(self, "portfolio", None)
        if portfolio is None:
            return None
        return PortfolioBundle(service=portfolio, store=getattr(self, "portfolio_store", None))

    def _ledger_bundle(self) -> Any:
        """The general-ledger subsystem handed to each run (service + store)."""
        from .ledger.bootstrap import LedgerBundle  # noqa: PLC0415

        ledger = getattr(self, "ledger", None)
        if ledger is None:
            return None
        return LedgerBundle(service=ledger, store=getattr(self, "ledger_store", None))

    def run_lineage(self) -> Any:
        """The process-wide lineage/checkpoint store (Phase 1)."""
        from .run_lineage import RunLineage

        if getattr(self, "_lineage", None) is None:
            self._lineage = RunLineage(self.store)
        return self._lineage

    def code_tasks(self) -> Any:
        """The process-wide code-task view over the ONE run lineage (Phase 5)."""
        from .code.tasks import TaskStore  # noqa: PLC0415

        if getattr(self, "_code_tasks", None) is None:
            self._code_tasks = TaskStore(self.store)
        return self._code_tasks

    def workflow_store(self) -> Any:
        """The process-wide workflow definition store (Phase 5)."""
        from .workflows import WorkflowStore

        if getattr(self, "_workflow_store", None) is None:
            self._workflow_store = WorkflowStore(self.store)
        return self._workflow_store

    def evolution_store(self) -> Any:
        """The process-wide evolution store over the ONE agent-memory connection (Phase 10)."""
        from .code.evolution_store import EvolutionStore  # noqa: PLC0415

        if getattr(self, "_evolution_store", None) is None:
            self._evolution_store = EvolutionStore(self.store)
        return self._evolution_store

    def operator_actor(self) -> Any:
        """The operator Actor used by the evolution routes (already authorized)."""
        from .skills.permissions import OPERATOR_ACTOR  # noqa: PLC0415

        return OPERATOR_ACTOR

    def evolution_context(self, *, run_id: str) -> Any:
        """A ToolContext for an evolution evaluation run (broker + ledger + store)."""
        from .code.store import CodeWorkspaceStore  # noqa: PLC0415
        from .code.verify import VerificationLedger  # noqa: PLC0415
        from .tools import ToolContext  # noqa: PLC0415

        try:
            self.store.create_run(run_id, "evolution", {"query": "self-improvement evaluation"})
        except Exception:  # noqa: BLE001 - a duplicate run id is fine
            pass
        return ToolContext(
            run_id=run_id,
            log=lambda *_: None,
            memory_store=self.store,
            code_store=self.code_store or CodeWorkspaceStore(self.store),
            code_ledger=VerificationLedger(run_id),
            memory_service=self.memory,
            skill_registry=self.skill_registry,
            tool_registry=self.registry,
        )

    def jobs_store(self) -> Any:
        """The process-wide job store over the ONE agent-memory connection (Phase 6)."""
        from .jobs.store import JobStore  # noqa: PLC0415

        if getattr(self, "_jobs_store", None) is None:
            self._jobs_store = JobStore(self.store)
        return self._jobs_store

    def automation_store(self) -> Any:
        """The process-wide automation store over the ONE agent-memory connection.

        On first construction it MIGRATES legacy `HarnessJob` rows into
        `Automation` (preserving ids + run history), so upgrading is non-destructive.
        """
        from .automation.store import AutomationStore  # noqa: PLC0415

        if getattr(self, "_automation_store", None) is None:
            store = AutomationStore(self.store)
            try:
                result = store.migrate_legacy_jobs()
                if result.get("migrated"):
                    log.info("automation: migrated %d legacy jobs", result["migrated"])
            except Exception:  # noqa: BLE001 - migration must never block first use
                log.warning("legacy job migration failed", exc_info=True)
            self._automation_store = store
        return self._automation_store

    def automation_service(self) -> Any:
        """The process-wide automation service (CRUD + lifecycle + history)."""
        from .automation.service import AutomationService  # noqa: PLC0415

        if getattr(self, "_automation_service", None) is None:
            self._automation_service = AutomationService(
                self, self.automation_store(), self.automation_engine()
            )
        return self._automation_service

    def automation_engine(self) -> Any:
        """The process-wide automation engine (one durable scheduler)."""
        from .automation.engine import AutomationEngine  # noqa: PLC0415

        if getattr(self, "automation", None) is None:
            self.automation = AutomationEngine(self, self.automation_store())
        return self.automation

    async def start_automation(self) -> None:
        """Start the Native Automation Engine. Idempotent; never a boot blocker."""
        try:
            await self.automation_engine().start()
        except Exception:  # noqa: BLE001 - the engine must never block boot
            log.warning("automation engine failed to start", exc_info=True)

    async def stop_automation(self) -> None:
        engine = getattr(self, "automation", None)
        if engine is None:
            return
        try:
            await engine.stop()
        except Exception:  # noqa: BLE001
            log.warning("automation engine shutdown failed", exc_info=True)

    async def start_advisory_cadence(self) -> None:
        """Start the bounded advisory-outcome scorer. Never blocks boot."""
        try:
            if getattr(self, "_advisory_cadence", None) is None:
                from .advisory.cadence import AdvisoryCadence  # noqa: PLC0415

                self._advisory_cadence = AdvisoryCadence(self)
            await self._advisory_cadence.start()
        except Exception:  # noqa: BLE001 - a cadence failure must not block boot
            log.warning("advisory cadence failed to start", exc_info=True)

    async def stop_advisory_cadence(self) -> None:
        cadence = getattr(self, "_advisory_cadence", None)
        if cadence is None:
            return
        try:
            await cadence.stop()
        except Exception:  # noqa: BLE001
            log.warning("advisory cadence shutdown failed", exc_info=True)

    def advisory_cadence_status(self) -> dict[str, Any]:
        cadence = getattr(self, "_advisory_cadence", None)
        if cadence is None:
            from .advisory.cadence import AdvisoryCadenceConfig  # noqa: PLC0415

            config = AdvisoryCadenceConfig.from_env()
            return {
                "enabled": bool(config.enabled),
                "intervalSeconds": int(config.interval_seconds),
                "running": False,
                "lastRunAt": None,
                "lastResult": {},
                "lastError": None,
            }
        return cadence.status()

    async def run_advisory_cadence_once(self) -> dict[str, Any]:
        if getattr(self, "_advisory_cadence", None) is None:
            from .advisory.cadence import AdvisoryCadence  # noqa: PLC0415

            self._advisory_cadence = AdvisoryCadence(self)
        return await self._advisory_cadence.run_now()

    async def start_self_modification(self) -> None:
        """Start the class-authorized auto-accept/read-only-enable executor."""
        try:
            if getattr(self, "_self_modification", None) is None:
                from .self_improvement.self_modification import (  # noqa: PLC0415
                    SelfModificationExecutor,
                )

                self._self_modification = SelfModificationExecutor(self)
            from .self_improvement.effectful_widening import refresh as refresh_effectful  # noqa: PLC0415
            from .self_improvement.widening_runtime import refresh  # noqa: PLC0415

            refresh(self.store)
            refresh_effectful(self.store)
            await self._self_modification.start()
        except Exception:  # noqa: BLE001 - never block boot
            log.warning("self-modification executor failed to start", exc_info=True)

    async def stop_self_modification(self) -> None:
        executor = getattr(self, "_self_modification", None)
        if executor is None:
            return
        try:
            await executor.stop()
        except Exception:  # noqa: BLE001
            log.warning("self-modification shutdown failed", exc_info=True)

    def self_modification_status(self) -> dict[str, Any]:
        executor = getattr(self, "_self_modification", None)
        if executor is None:
            from .self_improvement.self_modification import (  # noqa: PLC0415
                SelfModificationPolicy,
            )

            policy = SelfModificationPolicy.from_env()
            return {
                "mode": policy.mode, "enabled": policy.enabled,
                "autoAccept": policy.auto_accept, "autoEnable": policy.auto_enable,
                "ttlDays": policy.ttl_days,
                "maxActiveSkills": policy.max_active_skills,
                "running": False, "lastRunAt": None, "lastResult": {},
            }
        return executor.status()

    async def run_self_modification_once(self) -> dict[str, Any]:
        if getattr(self, "_self_modification", None) is None:
            from .self_improvement.self_modification import (  # noqa: PLC0415
                SelfModificationExecutor,
            )

            self._self_modification = SelfModificationExecutor(self)
        return await self._self_modification.run_now()

    def pause_self_modification(self) -> dict[str, Any]:
        executor = getattr(self, "_self_modification", None)
        return executor.pause() if executor is not None else {"paused": True}

    def resume_self_modification(self) -> dict[str, Any]:
        executor = getattr(self, "_self_modification", None)
        return executor.resume() if executor is not None else {"paused": False}

    def revoke_self_modification(self, action_id: str) -> dict[str, Any]:
        executor = getattr(self, "_self_modification", None)
        if executor is None:
            return {"ok": False, "reason": "executor_unavailable"}
        return executor.revoke(action_id)

    async def start_jobs(self) -> None:
        """Deprecated alias â€” jobs are now automations (ADR-H114). Starts the engine."""
        await self.start_automation()

    async def stop_jobs(self) -> None:
        await self.stop_automation()

    async def start_tasks(self) -> None:
        """Start the task scheduler. Idempotent; a failure is a diagnostic."""
        scheduler = getattr(self, "task_scheduler", None)
        if scheduler is None:
            return
        try:
            # Re-queue goal turns stranded by the old `start_task` enqueue path
            # before the scheduler starts, or they stay invisible forever.
            manager = getattr(self, "tasks", None)
            if manager is not None:
                repaired = manager.repair_orphaned_goal_tasks()
                if repaired:
                    log.warning("recovered %d stranded goal turn(s)", repaired)
            # A turn queued inside a COMPLETED plan is equally invisible; reopen
            # the plan for an active/paused goal that still has work.
            autonomy = getattr(self, "autonomy", None)
            if autonomy is not None:
                reopened = autonomy.repair_open_turns()
                if reopened:
                    log.warning("reopened %d goal plan(s) with queued turns", reopened)
        except Exception:  # noqa: BLE001 - a repair must never block boot
            log.warning("stranded goal repair failed", exc_info=True)
        try:
            await scheduler.start()
        except Exception:  # noqa: BLE001 - the scheduler must never block boot
            log.warning("task scheduler failed to start", exc_info=True)

    async def stop_tasks(self) -> None:
        scheduler = getattr(self, "task_scheduler", None)
        if scheduler is None:
            return
        try:
            await scheduler.stop()
        except Exception:  # noqa: BLE001
            log.warning("task scheduler shutdown failed", exc_info=True)

    async def start_artifacts(self) -> None:
        """Start the artifact reconciler. Idempotent; a failure is a diagnostic."""
        try:
            from .artifacts.bootstrap import start_artifact_reconciler  # noqa: PLC0415

            start_artifact_reconciler(self)
        except Exception:  # noqa: BLE001 - the reconciler must never block boot
            log.warning("artifact reconciler failed to start", exc_info=True)

    async def stop_artifacts(self) -> None:
        try:
            from .artifacts.bootstrap import stop_artifact_reconciler  # noqa: PLC0415

            await stop_artifact_reconciler(self)
        except Exception:  # noqa: BLE001
            log.warning("artifact reconciler shutdown failed", exc_info=True)

    async def run_workflow(self, request: RunRequest) -> Any:
        """Execute a saved workflow by id over the existing graph (Phase 5).

        The workflow path is FLAG-GATED and DEFAULT OFF. With no `workflowId`
        a run behaves exactly as today (the ordinary `run` path). When enabled
        and the workflow exists, it compiles onto the existing graph.
        """
        from .workflows import compile_workflow

        if not _workflow_enabled():
            return {
                "workflowId": request.workflowId,
                "status": "disabled",
                "note": "the workflow path is disabled; set HARNESS_WORKFLOWS_ENABLED=1 to enable it",
            }
        workflow = self.workflow_store().get(request.workflowId)
        if workflow is None:
            return {
                "workflowId": request.workflowId,
                "status": "not_found",
                "note": f"no workflow {request.workflowId!r} is stored",
            }
        bridge = _HttpDataBridge(request.callbackBaseUrl, request.callbackToken)
        result = await compile_workflow(
            workflow, request, self.registry, bridge, self.store, bus=self.bus,
            retrieval_cache=self.retrieval_cache, memory_service=self.memory,
            skill_registry=self.skill_registry, approvals=self.approvals,
            memory_router=self.memory_router, mcp_manager=self.mcp,
        )
        return result.to_dict()

    async def _schedule_review(self, run_id: str, status: str) -> None:
        """Queue a bounded post-run learning review (never blocks the response)."""
        if status not in ("completed", "failed"):
            return
        try:
            run = self.store.get_run(run_id) or {}
            # Deterministic priority: a failed run is a stronger signal than a
            # completed one. Explicit corrections would be `critical`.
            priority = "medium" if status == "failed" else "normal"
            await self.learning_review.enqueue(
                run_id, priority=priority,
                session_id=run.get("chatId") or run.get("sessionId"),
                profile_id=run.get("userId"),
            )
        except Exception:  # noqa: BLE001 - scheduling must never affect the run
            log.warning("learning review scheduling failed", exc_info=True)

    async def run_skill(self, request: Any) -> Any:
        """Execute a skill through the existing runtime.

        Imported lazily so `service` stays importable without the skills package
        (and to keep the module import graph flat).
        """
        from .skills.runtime import execute_skill

        bridge = _HttpDataBridge(request.callbackBaseUrl, request.callbackToken)
        return await execute_skill(
            request,
            self.skill_registry,
            self.registry,
            bridge,
            self.store,
            bus=self.bus,
            memory_service=self.memory,
        )

    async def run_background(self, request: RunRequest) -> None:
        apply_chat_build_budget(request)
        record_chat_build(self.store, request, agent_id=str(getattr(request, "agentId", "") or ""))
        bridge = _HttpDataBridge(request.callbackBaseUrl, request.callbackToken)
        sampling_factory = (
            self._sampling_model_factory(request)
            if getattr(self, "mcp", None) is not None
            else None
        )

        async def _task() -> None:
            try:
                from .run_binding import bind_run  # noqa: PLC0415
                from .run_lease import RunLeaseGuard  # noqa: PLC0415

                with bind_run(bridge=bridge, sampling_factory=sampling_factory):
                    with RunLeaseGuard(self.store, request.runId) as lease:
                        if not lease.owned:
                            # W6: see the foreground path — a duplicate submission
                            # never overwrites the live owner's run.
                            self.bus.publish({
                                "id": f"evt_{request.runId}_lease_conflict",
                                "runId": request.runId, "type": "run.lease_conflict",
                                "seq": -1, "at": now_iso(),
                                "data": {"code": "RUN_LEASE_CONFLICT"}, "transient": True,
                            })
                            return
                        result = await _with_run_slot(
                            getattr(self, "_run_slots", None),
                            lambda: execute_run(
                                request, self.registry, bridge, self.store, self.bus,
                                runtime_id=(lease.manager.runtime_id if lease.manager else None),
                                **self._run_kwargs(),
                            ),
                        )
                    await self._schedule_review(request.runId, result.status)
            finally:
                # Signal live SSE consumers that the run is over, whatever the
                # terminal status, so their streams close promptly.
                self.bus.end_stream(request.runId)

        task = asyncio.create_task(_task())
        self._active[request.runId] = task
        task.add_done_callback(lambda _t: self._active.pop(request.runId, None))

    def retrieval_metrics(self) -> dict[str, Any]:
        """Performance counters from the shared retrieval cache/layer."""
        return {"cache": self.retrieval_cache.stats()}

    def cancel(self, run_id: str) -> bool:
        task = self._active.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def is_active(self, run_id: str) -> bool:
        task = self._active.get(run_id)
        return task is not None and not task.done()

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        from .run_cost import with_cost  # noqa: PLC0415

        return with_cost(run, self.store)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        from .run_cost import with_cost  # noqa: PLC0415

        return [with_cost(run, self.store) for run in self.store.list_runs(limit)]

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.list_events(run_id)

    def list_tool_executions(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.list_tool_executions(run_id)

    def list_receipts(self, limit: int = 50) -> list[dict[str, Any]]:
        """Research receipts for the read-only Eclipse-as-server surface."""
        return self.store.list_receipts(limit)

    def get_receipt(self, run_id: str) -> Optional[dict[str, Any]]:
        return self.store.get_receipt(run_id)

    def usage_summary(self, days: int = 30) -> dict[str, Any]:
        return self.store.usage_summary(days)

    # Memory now lives in Eclipse-Memory (the dedicated database), not the run
    # store. These delegate so existing callers keep working with the richer
    # write pipeline (type/scope/dedupe/evidence) instead of a raw insert.
    def remember(self, **kwargs: Any) -> dict[str, Any]:
        return self.memory.write_memory(**kwargs)

    def recall(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.memory.retrieve(**kwargs).memories

    def close(self) -> None:
        self.store.close()
        # `skill_store` IS `memory_store` â€” one connection, closed once.
        self.memory.close()


__all__ = ["AgenticService", "HarnessError"]

