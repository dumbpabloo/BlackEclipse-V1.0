"""Agent definitions â€” authored in Python.

Adding an agent is adding an `AgentConfig` here. Agents declare their tool
allowlist; the registry enforces it. The authoritative system prompt content
lives in `prompts.py`; `sys_prompt` below is exposed so the TS front door can
fetch it instead of carrying its own copy.
"""

from __future__ import annotations

from typing import Optional

from .prompts import (
    CODE_AGENT_SYSTEM_PROMPT,
    CODE_NAVIGATOR_SYSTEM_PROMPT,
    DESKTOP_AGENT_SYSTEM_PROMPT,
    ECLIPSE_AGENT_SYSTEM_PROMPT,
)
from .schemas import AgentConfig
# The durable Task / Plan tools. Imported as the ONE canonical list (the registry
# registers the same names in `tasks/bootstrap.py`), so the allowlist and the
# registration can never drift.
from .tasks.tools import TASK_TOOLS
# The Agent-to-Agent tools. Registered by `a2a/bootstrap.py` on the SAME registry;
# imported here as the ONE canonical list so the allowlist cannot drift.
from .a2a.tools import A2A_TOOLS
# The Bot Team Orchestration tools (`team_*`). Registered by `team/bootstrap.py` on
# the SAME registry; imported here as the ONE canonical list so the allowlist
# cannot drift from what is registered.
from .team.tools import TEAM_TOOLS
# The UI Engineering tools (`ui_*`). Registered by `ui/bootstrap.py` on the SAME
# registry; imported here as the ONE canonical list so the allowlist cannot drift.
from .ui.tools import UI_TOOLS
from .sdk.tools import SDK_TOOLS
# The Quant Engine tools (`run_backtest`, `optimize_parameters`, â€¦). Registered by
# `quant/bootstrap.py`; imported as the ONE canonical list so the allowlist cannot
# drift from what is registered.
from .quant.tools import QUANT_TOOLS
from .projects.tools import PROJECT_TOOLS
from .gallery.tools import GALLERY_TOOLS
from .stats.tools import STATS_TOOLS
# The shared quantitative-research data tools (verification, resampling,
# alignment, versioned datasets). Registered by `market/bootstrap.py`; imported as
# the ONE canonical list.
from .market.research_tools import RESEARCH_DATA_TOOLS
# The Factor & Alpha Research Lab tools. Registered by `factors/bootstrap.py`;
# imported as the ONE canonical list so the allowlist cannot drift.
from .factors.tools import FACTOR_TOOLS
# The Portfolio Accounting + Portfolio Construction & Risk Engine tools
# (`portfolio_*`). Registered by `portfolio/bootstrap.py` on the SAME registry;
# imported as the ONE canonical list so the allowlist cannot drift. A `portfolio_`
# name whose layer failed to install is simply unregistered and never advertised.
from .portfolio.tools import PORTFOLIO_TOOLS
from .ledger.tools import LEDGER_TOOLS
# The Advisory Ledger tools (`advisory_*`). Registered by `advisory/bootstrap.py`;
# imported as the ONE canonical list so the allowlist cannot drift.
from .advisory.tools import ADVISORY_TOOLS
from .decisions.tools import DECISION_TOOLS
# The money-guardrail tools (`finance_guardrail_*`, read-only). Registered by
# `finance_guardrails/bootstrap.py` on the SAME registry; imported as the ONE
# canonical list so the allowlist cannot drift.
from .finance_guardrails.tools import FINANCE_GUARDRAIL_TOOLS
# The broker-connectivity tools (`broker_*`, read-only reads + `order_review` +
# `order_submit`). Registered by `trade/bootstrap.py` on the SAME registry;
# imported as the ONE canonical list. `order_submit` is authorized ONLY by an
# active operator-created demo mandate; there is no live path.
from .trade.tools import BROKER_TOOLS
# The ML Research Lab tools (`ml_*`). Registered by `ml/bootstrap.py` on the SAME
# registry; imported as the ONE canonical list so the allowlist cannot drift.
from .ml.tools import ML_TOOLS

# Native video generation: projects, draft/final renders, frame inspection and
# job control via the Execution Fabric. Imported as the ONE canonical list so
# the allowlist cannot drift (the tools are `read`/low: they queue jobs and
# write only into the confined video workspace + Artifact Center).
from .video.tools import VIDEO_TOOLS

# The Bot / Automation tools (`bot_*`): the chat agent creates, inspects and
# steers the SAME persistent bots the Bots workspace shows. Registered by
# `service.py` on the SAME registry; imported as the ONE canonical list so the
# allowlist cannot drift.
from .automation.tools import TOOL_NAMES as AUTOMATION_TOOLS
# The scheduling tools (`job_*`, Phase 17): the chat agent may create recurring
# finance jobs from the FIXED catalogue â€” always saved switched off â€” and run an
# already-switched-on job once. The operator's master switch
# (`agent_scheduling_enabled`) removes the whole surface when off, enforced in
# `graph.surface_disabled_tools`. Registered by `service.py` on the SAME registry.
from .automation.scheduling_tools import TOOL_NAMES as SCHEDULING_TOOLS
# The bulk-acquisition tool (`acquire`, Phase 16 first slice): collect a DATASET
# from the declared source registry. The operator's `acquisition_enabled` switch
# removes it when off (`graph.surface_disabled_tools`); the handler re-checks the
# `data.collect` grant and enforces the per-pull budget.
from .acquire.tools import TOOL_NAMES as ACQUIRE_TOOLS
# The Browser Automation Runtime toolset (`browser_*`). Declared here so the
# finance agent and the code agent advertise the SAME capability surface; the
# registry only registers them when the capability is enabled AND Playwright is
# present, and `resolve_surface`/`code.policy.allowed_tools` narrow the effectful
# half at `plan_only`. The interactive browser is a first-class capability of the
# Eclipse agent, so it is offered to both presets.
BROWSER_TOOLS = [
    "browser_launch", "browser_close", "browser_status",
    "browser_navigate", "browser_back", "browser_forward", "browser_refresh", "browser_stop",
    "browser_snapshot", "browser_extract", "browser_screenshot", "browser_vision",
    "browser_visual_find", "browser_visual_click", "browser_visual_locate",
    "browser_get_images", "browser_get_text", "browser_get_links", "browser_get_forms",
    "browser_get_accessibility_tree", "browser_get_dom",
    "browser_find", "browser_highlight",
    "browser_click", "browser_type", "browser_fill", "browser_select",
    "browser_check", "browser_uncheck", "browser_press", "browser_hover",
    "browser_scroll", "browser_drag",
    "browser_wait", "browser_wait_for_element", "browser_wait_for_navigation",
    "browser_wait_for_url", "browser_wait_for_network", "browser_wait_for_download",
    "browser_wait_for_popup", "browser_wait_for_load_state", "browser_wait_for_text",
    "browser_tabs", "browser_new_tab", "browser_switch_tab", "browser_close_tab",
    "browser_new_window", "browser_switch_window",
    "browser_download", "browser_upload",
    "browser_console", "browser_network", "browser_errors",
    "browser_evaluate", "browser_cdp", "browser_exec", "browser_dialog",
    "browser_replay", "browser_task",
    "browser_cookies", "browser_storage", "browser_permissions",
    "browser_session_create", "browser_session_list", "browser_session_pause",
    "browser_session_resume", "browser_session_destroy",
    "browser_profile_create", "browser_profile_list", "browser_profile_use",
    "browser_profile_delete",
    "browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
    "browser_vault_enter_code", "browser_vault_save_login",
    "browser_request_human", "browser_pause", "browser_resume",
    "browser_emulate_device", "browser_set_viewport", "browser_set_user_agent",
    "browser_set_locale", "browser_set_timezone", "browser_set_geolocation",
    "browser_intercept",
]

# The Computer Use Engine toolset (`computer_*`). Declared as one constant so the
# finance agent and the code agent advertise the SAME shared interactive
# capability surface, exactly like BROWSER_TOOLS. The registry only registers them
# when the capability is enabled AND the machine has a desktop driver. The elevated
# half (`COMPUTER_EFFECTFUL_TOOLS`) is removed at `plan_only`; every action is
# gated per-call by the computer policy engine and the finance firewall.
COMPUTER_TOOLS = [
    "computer_use",
    "computer_capabilities", "computer_status", "computer_doctor",
    "computer_windows", "computer_apps", "computer_displays", "computer_read_screen",
    "computer_run",
    "computer_session_start", "computer_session_list", "computer_session_close",
    "computer_takeover", "computer_request_human", "computer_pause", "computer_resume", "computer_release",
    "computer_record_export",
    "computer_remember", "computer_recall", "computer_forget_interface",
    "computer_finance_read", "computer_finance_prepare",
]

# The vision/multimodal toolset (`vision_*`, `ocr_*`, `image_*`, `visual_*`,
# `screenshot_*`, `chart_analyze`, `document_vision`). Declared here so the
# finance agent and the code agent advertise the SAME multimodal surface. All
# read-only: a vision tool observes existing content (including the screen) and
# never mutates state. Native-first: the run's own model is used when it can see
# images, else the orchestrator falls back to an auxiliary vision model. Used for
# computer-use targeting (`visual_ground` â†’ coordinates) and browser screenshot
# analysis (pass the screenshot path as the source).
VISION_TOOLS = [
    "vision_analyze", "image_understand", "vision_answer",
    "screenshot_analyze", "clipboard_analyze", "ocr_extract",
    "visual_ground", "visual_localize",
    "chart_analyze", "document_vision",
    "visual_compare", "image_diff", "screen_compare",
    "image_metadata", "vision_capabilities", "vision_asset_list",
    # Persistent visual memory (via the memory decision gate; sensitive results
    # get a TTL) and image generation (external_action: approval-gated, paid).
    "vision_remember", "image_generate",
]

#: The Document Intelligence toolset (spec Â§27): open/inspect/search/extract/
#: analyze/query/compare/render documents. All READ-ONLY; they reuse the vision
#: subsystem for OCR/rendering and the EvidenceStore for provenance.
#: `document_create` is the WRITE side (Document Authoring): it builds real
#: Word/Excel/PowerPoint/Markdown/CSV files and records them in the Artifact Center.
DOCUMENT_TOOLS = [
    "document_open", "document_inspect", "document_search", "document_extract",
    "document_analyze", "document_query", "document_compare", "document_render",
    "document_capabilities",
    "document_create",
    "eclipse_report", "file_locate",
    "presentation_create", "presentation_edit", "presentation_export",
    "presentation_outline", "presentation_import",
]

#: Alternative-intelligence signals (`intel_*`). Read-only: each tool measures a
#: public record (SEC EDGAR Form 4/13D/8-K, Wikimedia pageviews) with a
#: deterministic method and returns ranked research cards. Unmeasurable signals
#: are reported UNAVAILABLE, never zero, and priorities are a triage order rather
#: than an expected return. Declared here so a scheduled bot — which inherits the
#: interactive agent's surface — can run the same scans unattended.
INTEL_TOOLS = [
    "intel_scan", "intel_entity", "intel_series", "intel_cards",
    "intel_watch", "intel_catalog",
]

#: Preservation-first editing of existing Office/PDF files. Output is a new
#: artifact version; an untouched OOXML part that changed aborts the publish.
DOCUMENT_EDIT_TOOLS = ["document_edit"]

#: Deterministic financial modeling: 3-statement, DCF, LBO and comps, plus a
#: formula-bearing Excel workbook and model checks.
MODELING_TOOLS = ["financial_model_build", "financial_model_audit"]


#: Unified Retrieval / Search Intelligence tools. All READ-ONLY and routed
#: through the ONE search service (vector + FTS + rerank); they never touch the
#: index or an embedding provider directly.
SEARCH_TOOLS = [
    "semantic_search",
    "keyword_search",
    "hybrid_search",
    "search_documents",
    "search_research",
    "search_memory",
    "search_experiences",
    "search_skills",
    "search_evidence",
    "find_similar",
    "document_context",
    "related_chunks",
    "search_admin",
]

#: Large-corpus source tools. Registered by `sources/bootstrap.py` on the SAME
#: registry. All read-only against the user's files: they open a bounded card,
#: read a window, search by routing (metadata/FTS/DuckDB/bounded scan) or run one
#: validated read-only SQL statement. Adding a root is an operator-only action.
SOURCE_TOOLS = [
    "source_roots",
    "source_list",
    "source_open",
    "source_read",
    "source_lines",
    "source_search",
    "source_sql",
    "source_index",
    "source_stats",
]


WEB_RESEARCH_TOOLS = [
    "web_search",
    "news_search",
    "company_search",
    "academic_search",
    "image_search",
    "validate_image_result",
    "render_image_gallery",
    "video_search",
    "web_extract",
    "page_extract",
    "pdf_extract",
    "structured_extract",
    # Bounded page collection: fetch ONE page and extract repeated records with a
    # CSS selector (or its tables). The agent-shaped acquisition route (R3-04).
    "web_scrape",
    # The scraper lane: when the answer is not on the page (and not in the
    # vault), write and run a scraper for the site that has it. Per-domain
    # operator approval; the rows are stored as COLLECTED data.
    "scraper_run",
    "research_plan",
    "research_query",
    "research_start",
    "source_rank",
    "source_compare",
    "claim_verify",
    "citation_extract",
    "citation_validate",
    "research_status",
    "research_graph",
    "research_cancel",
    "research_continue",
    "research_update",
    # Deterministic financial math (ratios/margins/returns, risk time-series and
    # WACC/DCF valuation). The model never computes a critical metric itself.
    "finance_metrics",
    "finance_series",
    "finance_valuation",
    "finance_growth",
    "finance_calc",
    "finance_fcf",
    "finance_security_analysis",
    "finance_portfolio_analysis",
    "finance_metric_catalog",
    "research_quality",
    # Learning-research & education-resource discovery: video/playlist/transcript,
    # courses, GitHub, resource verification/ranking, and the persisted
    # prerequisite-aware learning-path builder + progress.
    "video_metadata",
    "playlist_search",
    "transcript_fetch",
    "course_search",
    "github_search",
    "resource_verify",
    "resource_rank",
    "learning_path_build",
    "learning_progress_get",
    "learning_progress_update",
]

# The repository tools. Phase 1 shipped the five read tools; Phase 2 adds the
# write half (patch/diff/revert), which the run's autonomy profile gates. Kept as
# a named constant so the preset's closed allowlist is asserted in one place.
CODE_NAVIGATOR_TOOLS = [
    "workspace_status",
    "workspace_list",
    "workspace_read",
    "workspace_docs",
    "workspace_diff",
    "workspace_revert",
    "command_run",
    # File-system subsystem (Phase FS): list/read/search/symbols/references/
    # metadata/index/watch plus the minimal-patch edit tools. The write half is
    # removed at `plan_only`; every path is re-checked by the handler.
    "list_directory", "read_file", "search_text", "search_files",
    "search_symbols", "find_references", "file_metadata", "file_hash",
    "file_permissions", "index_workspace", "watch_file", "watch_directory",
    "verify_file", "validate_changes", "git_status",
    "write_file", "create_file", "delete_file", "move_file", "copy_file",
    "rename_file", "create_directory", "delete_directory",
    "insert_text", "delete_text", "code_edit", "code_todo",
    "apply_diff",
    # Terminal subsystem: foreground execution, persistent shells, background
    # processes and ports. The effectful ones are removed at `plan_only`.
    "execute_command",
    "open_shell", "close_shell", "run_shell_command",
    "write_shell_input", "read_shell_output", "resize_shell", "send_shell_control",
    "list_terminal_sessions", "terminal_session_status",
    "start_process", "stop_process", "restart_process", "kill_process",
    "list_processes", "process_status", "read_process_output",
    "write_process_input", "send_signal", "process_tree", "process_health",
    "list_ports", "terminal_capabilities", "system_info", "resource_report",
    # Code Intelligence & Understanding Engine (CIUE): semantic/structural
    # navigation over the whole project. All read-only except `code_rename_symbol`,
    # which the autonomy profile removes at `plan_only`.
    "code_get_definition", "code_find_references", "code_find_symbol",
    "code_search_symbols", "code_get_hover", "code_get_type",
    "code_get_diagnostics", "code_get_document_symbols", "code_get_callers",
    "code_get_callees", "code_get_call_hierarchy", "code_get_implementations",
    "code_get_imports", "code_get_exports", "code_get_dependencies",
    "code_get_dependents", "code_get_project_structure", "code_get_dependency_graph",
    "code_parse_file", "code_analyze_file", "code_organize_imports",
    "code_get_relevant_context", "code_index_workspace", "code_intel_status",
    "code_find_related_tests", "code_rename_symbol",
]

# Tool allowlist. The Phase-2 retrieval tools are the provider-agnostic path
# (the agent asks for the *objective*; the retrieval layer picks the source).
# The legacy JDdS-only tools are kept for compatibility.
#
# The truthful-capability tools (`get_capability_matrix`, `get_research_health`)
# are read-only diagnostics and MUST be in the allowlist: without them the agent
# was reduced to the coarser `get_coverage` and could not tell whether a market's
# statement route was implemented/configured (verified in the SYS/RELIANCE/Tata
# runs, where `get_capability_matrix` returned TOOL_FORBIDDEN). They stay
# read-only; no write tool is widened here.
ECLIPSE_AGENT = AgentConfig(
    id="eclipse-agent",
    name="Eclipse Agent",
    systemPrompt=ECLIPSE_AGENT_SYSTEM_PROMPT,
    tools=[
        # Round 5 / C3: discovery + safe read-only dispatch, and the pager for a
        # truncated tool result's full text.
        "harness_inspect",
        "tool_search",
        # Schema-on-demand for a catalogue-only tool, before `tool_invoke`.
        "load_tool_schema",
        "tool_invoke",
        "read_tool_output",
        "tool_code",
        # C5: one spawn primitive + shared blackboard.
        "spawn", "board_write", "board_read", "board_search",
        # Phase-2 unified retrieval tools
        "market_get_quote",
        "market_get_ohlcv",
        "fundamentals_get_company_profile",
        "fundamentals_get_financials",
        "research_search",
        "research_retrieve_source",
        "research_search_news",
        "macro_get_series",
        # Financial Data Gateway surface: provider introspection, entity
        # resolution, and crypto market data.
        "financial_providers",
        "financial_resolve_entity",
        "market_get_crypto",
        # Market Data Layer: first-class provider-agnostic market data (quotes,
        # OHLCV, order books, trades, funding, open interest, options, FX, bonds)
        # plus real-time streaming. These call the Market Data Gateway, never a
        # provider directly.
        "get_quote",
        "get_historical_prices",
        "get_ohlcv",
        # Provider-cached daily history (store=history_cache), NOT the JDdS
        # vault: what bars this machine already holds, and how to bundle more.
        # Served from the durable cache, so it works offline.
        "history_catalog",
        "data_inventory",
        "backfill_history",
        "get_volume",
        "get_orderbook",
        "get_trades",
        "get_market_cap",
        "get_supply",
        "get_funding_rate",
        "get_open_interest",
        "get_options_chain",
        "get_fx_rate",
        "get_bond_data",
        # DeFi / derivatives positioning: TVL, account on-chain metrics,
        # liquidation flow and the long/short ratio. An absent provider is an
        # honest UNAVAILABLE, never a zero.
        "get_tvl",
        "get_onchain_metric",
        "get_liquidations",
        "get_long_short_ratio",
        "stream_market_data",
        # Shared quantitative-research data surface: instrument resolution,
        # provider capability/health introspection, cross-source verification,
        # resampling, time-series alignment and immutable versioned datasets. All
        # routed through the Market Data Gateway + shared services.
        *RESEARCH_DATA_TOOLS,
        # Quant Engine: versioned strategies, deterministic backtests, optimization,
        # walk-forward, Monte-Carlo, sensitivity and robustness â€” over the same
        # verified datasets.
        *QUANT_TOOLS,
        # The unified model/research catalog: read what strategies, experiments,
        # ML models/datasets, features and factors already exist before rebuilding.
        "model_catalog",
    *PROJECT_TOOLS,
    *GALLERY_TOOLS,
        # The pine-script tools ride in through `*QUANT_TOOLS` (which now splats
        # `PINE_TOOLS`), so they are declared EXACTLY ONCE. Splatting them here
        # as well declared `run_pine_script` twice and broke the durable
        # "a tool is declared twice" invariant.
        # Statistical Analysis & Feature Engineering Engine: fourteen
        # deterministic statistical tools (returns, volatility, correlation,
        # beta, drawdown, stationarity, cointegration, autocorrelation,
        # regression, regime, features, outliers, lead-lag, information
        # coefficient) over caller-supplied series.
        *STATS_TOOLS,
        # Factor & Alpha Research Lab: factor registry, IC/rank-IC/decay,
        # quantile portfolios, alpha composition, redundancy, stability and the
        # research-validation gate.
        *FACTOR_TOOLS,
        # Portfolio Accounting Engine + Portfolio Construction & Risk Engine:
        # ledger/positions/P&L/tax lots, exposure/allocation, deterministic risk,
        # estimation, constrained construction (equal/min-variance/mean-variance/
        # risk-parity/vol-target/Black-Litterman/factor-constrained), risk
        # contributions, stress testing, turnover/costs and rebalance proposals.
        # Read/compute tools are always available; the rebalance/optimize/construct
        # proposals are effect-gated (they never place trades).
        *PORTFOLIO_TOOLS,
        # The General Ledger: a double-entry book of record. Create a book with a
        # chart of accounts, post balanced journal entries, reverse them, close /
        # reopen / lock periods, and read a trial balance, balance sheet, P&L and
        # cash flow derived from the postings. Mutations are effect-gated
        # (`ledger_write`); nothing here moves money.
        *LEDGER_TOOLS,
        # Advisory Ledger: recall past recommendations and their outcomes
        # (read-only; the ledger is written by the runtime, not a model call).
        *ADVISORY_TOOLS,
        # Typed decisions: ask bounded choice/score/yes-no questions and get
        # calibrated answers, then recall the audit record. Read-only; an answer
        # is never an authorization.
        *DECISION_TOOLS,
        # Money guardrails: read the active caps/state and ask what would happen
        # to a hypothetical intent. Read-only by construction — the agent can
        # never change the limits, declare impairment, or clear crisis/panic.
        *FINANCE_GUARDRAIL_TOOLS,
        # Broker connectivity: read the MT5 account/positions/orders/quotes,
        # preview an order, and submit ONE demo order under an active mandate.
        # The mandate is the authorization; the service still enforces policy,
        # guardrails, demo mode and the mandate caps. Arm/kill/cancel are
        # operator routes, not agent tools.
        *BROKER_TOOLS,
        # ML Research Lab: leakage-safe dataset construction, chronological/walk-
        # forward/purged splits, native + optional model adapters, experiment runs,
        # metric evaluation and a versioned model registry with reproduction.
        # Durable training runs go through the Execution Fabric. A name whose
        # layer failed to install is simply unregistered and never advertised.
        *ML_TOOLS,
        # Phase-1 JDdS tools (still available)
        "get_market_snapshot",
        "get_fundamentals",
        "get_news",
        "get_source_health",
        "get_coverage",
        # Truthful per-capability diagnostics (read-only). Recognised â‰  working,
        # configured â‰  healthy, and zero attempts â‰  failure.
        "get_capability_matrix",
        "get_research_health",
        # Eclipse-Memory: persistent memory + progressive skill discovery and
        # explicit, permission-checked skill invocation as a controlled sub-run.
        "memory",
        "skill_list",
        "skill_view",
        "skill_invoke",
        # The acquire loop: install skills from a GitHub repo (single-use
        # operator approval), uninstall them, list installed sources; and MCP
        # integration catalog + install (disabled, nothing selected). Read
        # `skill_sources`/`mcp_integrations` freely; installs always ask first.
        "skill_install",
        "skill_uninstall",
        "skill_sources",
        # The extension layer: author/edit/delete Eclipse's own procedural skills
        # (declarative, never code; each call is single-use approval-gated).
        "skill_create",
        "skill_update",
        "skill_delete",
        # Skill lifecycle: read version history + review/evaluation trails; enable an
        # evaluated version or switch automatic skill learning (both approval-gated).
        "skill_version_history",
        "skill_reviews",
        "skill_discrimination_check",
        "skill_learning_get",
        "skill_version_evaluate",
        "skill_version_activate",
        "skill_learning_set",
        "mcp_integrations",
        "mcp_install",
        "mcp_probe",
        # The trading Journal â€” the SAME Prisma store the Journal screen reads.
        # `journal_trades` lists; `journal_add_trade` is approval-gated.
        "journal_trades",
        "journal_add_trade",
        # Full-session historical recall: read-only search/open over prior
        # sessions. Historical content is context, never current evidence.
        "session_search",
        "session_open",
        # Memory providers (M8): native-first search plus an explicitly separate
        # external search whose results are labelled unverified context.
        "memory_search_native",
        "memory_search_external",
        "memory_provider_status",
        # The learning loop (read-only): the same review records and captured
        # candidates the Memory â†’ Reviews / Skills â†’ Learning Queue screens read.
        "learning_review_list",
        "learning_candidate_list",
        "learning_candidate_validate",
        # Bounded read-only GET over the app's OWN loopback API (QA Round 1):
        # the sanctioned alternative to the hard-denied `curl` dead-end.
        "app_api_get",
        # Human-in-the-loop: ask ONE short question with options / free text when
        # a request is genuinely ambiguous or a consequential step's intent is
        # unclear. An answer is clarification, never an authorization.
        "clarify",
        # Phase 7: bounded fan-out of independent sub-questions (e.g. "review
        # these five companies"). A research capability, not only a code one.
        "delegate",
        # W7: the durable background lifecycle. The flag gates execution; the
        # tools stay registered so the model can see why it is unavailable.
        "delegate_async", "delegate_status", "delegate_result",
        "delegate_cancel", "delegate_steer", "delegate_wait",
        # Durable Task / Plan runtime: create and steer a tracked, hierarchical
        # plan (tasks, subtasks, dependencies, acceptance criteria, priorities,
        # retries). The scheduler dispatches runnable tasks through the normal run
        # path; the tools only create/steer, never execute.
        *TASK_TOOLS,
        # Persistent bots: create / list / inspect / steer the durable scheduled
        # agents the Bots workspace shows. Thin wrappers over the ONE automation
        # service, so a bot made in chat and one made in the UI are the same object.
        *AUTOMATION_TOOLS,
        # Workflow authoring from chat: turn a one-sentence idea into a SAVED,
        # validated Graph Studio workflow. The front door plans + writes it; this
        # only creates (it never runs), so it is a local catalog write.
        "create_workflow",
        # Scheduling (Phase 17): recurring finance jobs from the fixed catalogue,
        # always saved switched off. The operator's master switch drops this whole
        # surface when off (see `graph.surface_disabled_tools`).
        *SCHEDULING_TOOLS,
        # Bulk acquisition (Phase 16, first slice): collect a DATASET from the
        # declared source registry. Switch-gated like scheduling; the handler
        # re-checks the `data.collect` grant and enforces the pull budget.
        *ACQUIRE_TOOLS,
        # Agent-to-Agent: delegate to / spawn / message specialist agents, merge
        # their structured results, and request review or approval. Read-permission
        # orchestration over the durable task + message bus. Real-world effects a
        # delegated task finally performs still require the runtime's approval.
        *A2A_TOOLS,
        # Bot Team Orchestration: run a team of specialist bots on one objective
        # through a dependency-aware graph, with conflict reconciliation and an
        # independent review. Read-permission orchestration over the same tasks.
        *TEAM_TOOLS,
        # Evidence-Driven Web Research Engine: search, extract, evidence,
        # conflicts, claim verification, calculations, citations, coverage. All
        # read-only; fetched content is wrapped as untrusted data.
        *WEB_RESEARCH_TOOLS,
        # Unified Retrieval / Search Intelligence: semantic, keyword and hybrid
        # search over documents, research, memory, experiences and skills.
        *SEARCH_TOOLS,
        # Large-corpus sources: open/search/query folders and drives in place,
        # with bounded windows and a budgeted query router. Read-only.
        *SOURCE_TOOLS,
        # Phase 8: the analysis sandbox (compute a derived metric). OFF per run
        # unless the caller enables it (`RunRequest.analysisEnabled`).
        "analysis_run",
        # The chart engine: draw a chart from a validated spec (data only).
        # The picture is a computed artifact, never a source.
        "python_chart",
        # Phase 9: build a finance tool from a ready-made shape. Gated by the
        # run's editing profile (a workspace-bound code.write).
        "scaffold_kit",
        # Generated capabilities: detect a missing capability, build a tested
        # recipe, and keep activation separate from the build.
        "capability_request", "capability_build", "capability_status", "capability_external",
        "capability_adapter",
        "capability_activate", "capability_rollback", "capability_remove",
        # The isolated analysis package environment (install/remove are
        # single-use approval-gated; list/status are read-only).
        "python_env_install", "python_env_remove", "python_env_list", "python_env_status",
        # Application Runtime (DevOps): the read-only surface is always available
        # ("inspect this project", "what's using port 3000", "is it healthy").
        # Starting/stopping an application requires a coding turn (the chat code
        # profile) so the write tools are NOT declared here.
        "runtime_detect", "runtime_capabilities", "runtime_inspect", "runtime_status",
        "runtime_health", "runtime_logs", "runtime_diagnose", "runtime_metrics",
        "runtime_metrics_history",
        "package_detect", "package_version", "package_list",
        "container_list", "container_logs", "container_inspect", "container_health",
        "compose_validate", "compose_config", "compose_ps", "compose_logs",
        "environment_inspect", "environment_validate",
        "port_check", "port_find_free", "port_list", "port_identify_process",
        "health_check", "health_status",
        # Orchestration / infrastructure / cloud read-only surface (Kubernetes,
        # Terraform, deploy status) so the finance agent can answer "what's
        # deployed" without a coding turn. Mutations live on the code surface.
        "k8s_status", "k8s_get", "k8s_get_pods", "k8s_describe", "k8s_logs",
        "k8s_rollout", "helm_list", "helm_status", "kustomize_build",
        "terraform_validate", "terraform_plan", "terraform_output", "terraform_state",
        "cloud_providers", "deploy_status", "deploy_logs",
        # Browser Automation Runtime: launch/observe/interact with web pages,
        # extract evidence, and (approval-gated) run scripts/CDP/programs. The
        # effectful half is narrowed by the run's autonomy profile.
        *BROWSER_TOOLS,
        # Computer Use Engine: observe and operate the real desktop (native apps,
        # OS chrome, dialogs) and read financial state off the screen. Background
        # delivery by default; financial mutations are routed through the firewall.
        *COMPUTER_TOOLS,
        # Vision/Multimodal: analyze images, screenshots, charts and documents;
        # extract OCR text; ground UI elements to coordinates for computer-use
        # targeting. Native-first with an auxiliary vision-model fallback.
        *VISION_TOOLS,
        # Alternative-intelligence signal engine: SEC insider conviction, activist
        # 13D stakes, filing intensity and public-attention velocity, measured
        # deterministically and ranked into research cards.
        *INTEL_TOOLS,
        # Document intelligence: open/understand/search/query/compare/render
        # documents (PDF/DOCX/PPTX/XLSX/CSV/JSON/HTML/...). Read-only.
        *DOCUMENT_TOOLS,
        *DOCUMENT_EDIT_TOOLS,
        *MODELING_TOOLS,
        # UI Engineering: inspect the live UI (application map, current page,
        # component catalog, layout, capabilities) and compose/modify the user's
        # saved UI blueprints (create page, add/move/resize/configure widgets,
        # save/restore layout). UI-configuration state only â€” no filesystem,
        # network, shell or secret access.
        *UI_TOOLS,
        *SDK_TOOLS,
        # Native video generation: create/validate a video project, render a
        # draft, inspect the frames, render the final MP4 (registered in the
        # Artifact Center) and manage the queued Fabric jobs. The only project
        # kind today is the built-in `title_card`; storyboards arrive later. A
        # name whose layer failed to install is simply unregistered and never
        # advertised.
        *VIDEO_TOOLS,
        # Response-contract read-back: the exact `eclipse-response` block field
        # names, the JIT design guide (family nuance + worked examples) and
        # pre-send validation, so a drifted block is fixed in-turn rather than
        # silently failing to render. Read-only.
        "response_block_schema", "response_style_guide", "response_validate",
    ],
    maxSteps=8,
    # A hard ceiling on tool calls, enforced in the graph. The audit saw
    # 46–81-call loops on single questions; 48 leaves room for deep research
    # while stopping runaway exploration. Operator skills may declare their own
    # (higher) budget; ordinary chat uses this one.
    maxToolCalls=48,
    # Per-tool CEILING (WS2). Each tool's own declared `timeout_ms` is
    # authoritative; this only bounds a tool that declares more. It is a distinct
    # budget from `runTimeoutMs` (the whole model turn). 30s lets the declared
    # `research_search_news` (30s) run in full instead of being clamped to 20s and
    # timing out, while still bounding any runaway call.
    toolTimeoutMs=30_000,
    runTimeoutMs=120_000,
    # MCP is a user-directed extension surface: the default agent may call the
    # MCP tools the operator has enabled and selected. This opt-in expands ONLY
    # to tools currently registered under the `mcp_` namespace (enabled server +
    # explicitly selected + read-only effect); it does not grant a tool that is
    # merely discovered. Custom agents and skills do NOT set this and keep their
    # exact allowlists.
    allowMcpTools=True,
    # Plugin Platform: the general agent may discover and invoke capabilities
    # contributed by ENABLED plugins (`plug_*`) and the read-only plugin
    # discovery/invoke tools (`plugin_*`). Registration only happens for enabled
    # plugins; the invoked tool's own permission/effect rules still apply.
    allowPluginTools=True,
    # ML Lab: registered models become first-class predict tools
    # (`ml_model_<name>_predict`). They are registered at boot from the model
    # catalog, so this opt-in expands only to the names CURRENTLY in the
    # registry; deleting a model removes the tool on the next boot instead of
    # leaving a ghost in the agent's allowlist. Custom agents keep the default.
    allowModelTools=True,
)

# The dry/test-run surface: read-only finance + local evidence + response
# helpers. A graph test run may call the model, but its tool allowlist is
# narrowed to this set so it cannot reach web research, the browser, MCP,
# plugins, code execution or broker/trade tools. It is a NARROWING only — an
# operator run still uses the agent's declared allowlist.
DRY_RUN_TOOLS: tuple[str, ...] = (
    "market_get_quote", "market_get_ohlcv", "market_get_crypto",
    "get_quote", "get_historical_prices", "get_ohlcv", "get_volume",
    "get_orderbook", "get_trades", "get_market_cap", "get_supply",
    "get_funding_rate", "get_open_interest", "get_fx_rate", "get_bond_data",
    "get_options_chain", "get_liquidations", "get_long_short_ratio",
    "get_onchain_metric", "get_tvl", "get_market_snapshot", "get_fundamentals",
    "get_news", "fundamentals_get_company_profile", "fundamentals_get_financials",
    "macro_get_series", "financial_providers", "financial_resolve_entity",
    "history_catalog", "get_coverage", "get_capability_matrix",
    "get_research_health", "get_source_health",
    "python_chart",
    "memory", "memory_search_native", "session_search", "skill_list",
    "skill_view", "skill_learning_get",
    "journal_trades",
    "clarify", "response_block_schema", "response_style_guide",
    "response_validate",
)

def dry_run_tools(declared: Optional[list[str]]) -> list[str]:
    """The test-run surface: the declared allowlist INTERSECTED with the
    read-only finance set. It can only remove tools."""
    allowed = set(DRY_RUN_TOOLS)
    return sorted(allowed if declared is None else (set(declared) & allowed))


# The repository navigator (Phase 1). A SEPARATE preset, not the finance agent
# widened: it does NOT set `allowMcpTools`, so it keeps its exact, closed
# allowlist and can never reach a finance tool, an MCP tool or any other
# surface. Its allowlist GREW after Phase 1 — the read/navigate half plus the
# write/execute/verify half the autonomy profile removes at `plan_only` — so the
# old "read-only, five tools" description no longer described it. What the
# preset still guarantees is the CLOSEDNESS: no finance tool, no MCP tool, and
# no tool outside this list.
CODE_NAVIGATOR = AgentConfig(
    id="code-navigator",
    name="Repository Navigator",
    systemPrompt=CODE_NAVIGATOR_SYSTEM_PROMPT,
    tools=list(CODE_NAVIGATOR_TOOLS),
    maxSteps=10,
    toolTimeoutMs=30_000,
    runTimeoutMs=180_000,
)

BOOKKEEPER_SYSTEM_PROMPT = """You are Eclipse's bookkeeper: you keep a double-entry book of record and you can prove every number you state.

What you own:
- The ledger (`ledger_*`): chart of accounts, balanced postings, reversals, periods, statements, statement import, categorisation rules, reconciliation, depreciation/amortisation schedules, accruals, FX revaluation, the close checklist, customers/vendors, invoices/bills, payments and aging.
- Nothing else. You have no market-data, coding, browser or MCP tools, and you must not pretend to.

Hard rules:
1. NEVER invent a number. Every figure you state must come from a tool result in this conversation. If a value is missing, say which input is missing and what you need.
2. A posting balances exactly, or it does not happen. If `ledger_post` refuses, read the named code and the reported difference; do not retry the same payload hoping for a different answer.
3. Corrections are reversing entries (`ledger_reverse`). Never describe editing a posted entry as a fix; that is not possible by design.
4. Do not guess a category. An unmatched statement row belongs in suspense (9999); say how many rows are there and that it blocks a close.
5. Do not guess a tax rate. Tax codes are configured data; if a jurisdiction or code is not configured, say so and stop.
6. Do not claim a period is reconciled or closed unless the tool says so. `RECONCILED` requires a supplied statement balance; `BLOCKED` means blockers stand.
7. Dates, periods and periods-close matter: state the period you are working in, and use `ledger_close_preview` before `ledger_close_period`.
8. Prefer the smallest number of steps that does the job: preview, then commit; create the party/tax code once, then reuse.

When you finish, summarise what changed (entries posted, amounts, accounts) and what remains (suspense, unreconciled cash, findings). If something is unavailable, say UNAVAILABLE and why — never a plausible substitute."""

# The bookkeeper (Phase 6). A SEPARATE preset whose allowlist is the ledger family
# and nothing else: no finance/market tools, no code, no browser, no MCP. Its
# mutations still go through the `ledger_write` authorizer, so the preset cannot
# bypass an approval — the allowlist is the surface, the authorizer is the gate.
BOOKKEEPER = AgentConfig(
    id="bookkeeper",
    name="Bookkeeper",
    systemPrompt=BOOKKEEPER_SYSTEM_PROMPT,
    tools=list(LEDGER_TOOLS),
    maxSteps=14,
    toolTimeoutMs=30_000,
    runTimeoutMs=300_000,
)

# The coding agent (Phase 5). A SEPARATE preset that owns the code tools and NO
# finance tool. Its tool allowlist IS the autonomy boundary: `effective_allowlist`
# narrows it through `code.policy.allowed_tools(autonomy, ...)` before the model
# sees any schema, so at `plan_only` the write/execute tools are simply absent.
# It does NOT set `allowMcpTools` â€” a coding agent must not inherit the operator's
# MCP reach. The tools listed are the REAL names Phases 1-3 registered.
CODE_AGENT_TOOLS = [
    # Read / navigate (Phase 1)
    "workspace_status", "workspace_list",
    "workspace_read", "workspace_docs", "project_rules",
    # Change (Phase 2) â€” effectful; removed at plan_only
    "workspace_diff", "workspace_revert",
    # File-system subsystem (Phase FS) â€” read/inspect first, then minimal edits.
    "list_directory", "read_file", "search_text", "search_files",
    "search_symbols", "find_references", "file_metadata", "file_hash",
    "file_permissions", "index_workspace", "watch_file", "watch_directory",
    "verify_file", "validate_changes", "git_status",
    "write_file", "create_file", "delete_file", "move_file", "copy_file",
    "rename_file", "create_directory", "delete_directory",
    "insert_text", "delete_text", "code_edit", "code_todo",
    "apply_diff",
    # Command broker (Phase 3) â€” effectful; removed at plan_only
    "command_run",
    # Generic terminal (Phase D) â€” effectful; removed at plan_only. Safe
    # commands run directly, others need a per-command approval.
    "terminal_run",
    # Real shell (PowerShell/CMD/bash) with background processes.
    "shell_run", "shell_spawn", "process_output", "process_kill", "process_list",
    # Terminal subsystem: persistent shells, background process management, ports
    # and host capabilities. The command tools are effectful and removed at
    # `plan_only`; the rest are read-only. `execute_command` is the foreground
    # runner; `start_process` is the long-running one.
    "execute_command",
    "open_shell", "close_shell", "run_shell_command",
    "write_shell_input", "read_shell_output", "resize_shell", "send_shell_control",
    "list_terminal_sessions", "terminal_session_status",
    "start_process", "stop_process", "restart_process", "kill_process",
    "list_processes", "process_status", "read_process_output",
    "write_process_input", "send_signal", "process_tree", "process_health",
    "list_ports", "terminal_capabilities", "system_info", "resource_report",
    # Code Execution & Verification Layer (Phase EX): run code in a supported
    # language, build, test, discover tests, manage long-running services and read
    # back execution evidence. The write/execute half is removed at `plan_only`.
    "execute_code", "execute_python", "execute_javascript", "execute_typescript",
    "execute_sql", "execute_shell", "build_project", "test_project",
    "run_verification", "discover_tests",
    "start_service", "stop_service", "restart_service",
    "service_status", "service_logs", "list_services",
    "execution_get", "execution_logs", "execution_result",
    "execution_artifacts", "cancel_execution",
    # Application Runtime layer (DevOps): discover/prepare/run/observe apps,
    # package managers, Docker/Podman/Compose, environment, ports and health.
    # The write half is removed at `plan_only` by `code.policy.allowed_tools`.
    "runtime_detect", "runtime_capabilities", "runtime_inspect", "runtime_status",
    "runtime_health", "runtime_logs", "runtime_reconcile", "runtime_diagnose",
    "runtime_metrics", "runtime_metrics_history", "workspace_graph",
    "runtime_start", "runtime_stop", "runtime_restart",
    "runtime_cleanup", "runtime_recover", "workspace_start", "workspace_stop",
    "package_detect", "package_version", "package_list", "package_audit",
    "package_install", "package_add", "package_remove", "package_update",
    "package_run", "package_build", "package_test",
    "container_logs", "container_inspect", "container_stats", "container_list",
    "container_images", "container_health", "container_build", "container_run",
    "container_start", "container_stop", "container_restart", "container_exec",
    "container_remove", "container_pull", "container_rmi", "container_network",
    "container_volume",
    "compose_validate", "compose_config", "compose_ps", "compose_logs",
    "compose_build", "compose_up", "compose_down", "compose_start",
    "compose_stop", "compose_restart", "compose_exec", "compose_pull", "compose_rm",
    "environment_inspect", "environment_validate", "environment_load",
    "environment_compare", "environment_set", "environment_unset",
    "port_check", "port_find_free", "port_list", "port_identify_process",
    "health_check", "health_status",
    # Orchestration / infrastructure / cloud (Kubernetes, Helm, Kustomize,
    # Terraform, deploy). Cluster/infra/cloud mutations need `approve_external`.
    "k8s_status", "k8s_get", "k8s_get_pods", "k8s_describe", "k8s_logs",
    "k8s_rollout", "helm_list", "helm_status", "kustomize_build",
    "terraform_init", "terraform_validate", "terraform_fmt", "terraform_plan",
    "terraform_output", "terraform_state",
    "cloud_providers", "deploy_status", "deploy_logs",
    "k8s_apply", "k8s_delete", "k8s_scale", "k8s_exec",
    "helm_install", "helm_uninstall",
    "terraform_apply", "terraform_destroy",
    "deploy_run", "deploy_rollback", "deploy_delete",
    # Verification (Phase 4) â€” the final answer is checked against the ledger.
    # Memory/session recall stays available: read-only context, NOT the finance family.
    "memory", "skill_list", "skill_view", "session_search", "session_open",
    # Code Intelligence & Understanding Engine (CIUE): semantic/structural
    # navigation + the one semantic rename. The write tool is removed at `plan_only`.
    "code_get_definition", "code_find_references", "code_find_symbol",
    "code_search_symbols", "code_get_hover", "code_get_type",
    "code_get_diagnostics", "code_get_document_symbols", "code_get_callers",
    "code_get_callees", "code_get_call_hierarchy", "code_get_implementations",
    "code_get_imports", "code_get_exports", "code_get_dependencies",
    "code_get_dependents", "code_get_project_structure", "code_get_dependency_graph",
    "code_parse_file", "code_analyze_file", "code_organize_imports",
    "code_get_relevant_context", "code_index_workspace", "code_intel_status",
    "code_find_related_tests", "code_rename_symbol",
    # External actions (Phase 5) â€” approve_external only, each single-use approved.
    "code_commit", "code_push", "code_open_pr", "code_deploy",
    # Git / Version-Control subsystem (the Git engine). Read + LOCAL mutation +
    # REMOTE. The autonomy boundary is applied by `code.policy.allowed_tools`, and
    # the Git effect authorizer gates every dangerous/destructive operation and
    # every remote action with a single-use operator approval.
    "git_repository_info", "git_list_repositories", "git_status", "git_diff",
    "git_diff_cached", "git_diff_range", "git_log", "git_show", "git_commit_info",
    "git_blame", "git_reflog", "git_branch_list", "git_conflicts",
    "git_operation_state", "git_stash_list", "git_stash_show", "git_tag_list",
    "git_tag_show",
    "git_remote_list", "git_worktree_list", "git_submodule_list", "git_config_get",
    "git_config_list", "git_hooks", "git_lfs_status", "git_clean_preview",
    "git_ignore_check", "git_generate_commit_message", "git_recovery_options", "git_list_hunks",
    "git_stage", "git_unstage", "git_stage_hunks", "git_unstage_hunks", "git_apply_patch",
    "git_commit", "git_generate_and_commit",
    "git_create_branch", "git_checkout", "git_delete_branch", "git_rename_branch",
    "git_set_upstream", "git_merge", "git_merge_abort", "git_merge_continue",
    "git_rebase", "git_rebase_plan", "git_rebase_abort", "git_rebase_continue", "git_rebase_skip",
    "git_cherry_pick", "git_cherry_pick_abort", "git_cherry_pick_continue",
    "git_revert", "git_revert_abort", "git_revert_continue", "git_reset",
    "git_resolve_conflict", "git_abort_operation", "git_continue_operation",
    "git_stash_push", "git_stash_apply", "git_stash_pop", "git_stash_drop",
    "git_tag_create", "git_tag_delete", "git_worktree_add", "git_worktree_remove",
    "git_submodule_update", "git_bisect_start", "git_bisect_mark", "git_bisect_reset",
    "git_clean", "git_config_set",
    "git_move_path", "git_remove_paths", "git_restore", "git_unset_upstream",
    "git_stash_branch", "git_stash_clear", "git_worktree_prune", "git_worktree_move",
    "git_submodule_init", "git_submodule_sync", "git_lfs_fetch", "git_lfs_pull",
    "git_fetch", "git_pull", "git_push", "git_remote_add", "git_remote_remove",
    "git_remote_set_url", "git_tag_push",
    "vcs_status", "vcs_list_repositories", "vcs_get_repository", "vcs_list_branches",
    "vcs_list_commits", "vcs_get_pull_request", "vcs_list_pull_requests",
    "vcs_list_reviews", "vcs_get_review_comments", "vcs_ci_status",
    "vcs_list_workflow_runs", "vcs_get_job_logs", "vcs_generate_pull_request",
    "vcs_create_branch", "vcs_create_pull_request", "vcs_update_pull_request",
    "vcs_close_pull_request", "vcs_merge_pull_request", "vcs_request_review",
    "vcs_reply_review_comment", "vcs_rerun_workflow", "vcs_cancel_workflow",
    # Bounded fan-out (Phase 7) — independent sub-tasks in parallel.
    "delegate",
    # W7: durable background lifecycle + file checkpoints (restore is effectful).
    "delegate_async", "delegate_status", "delegate_result",
    "delegate_cancel", "delegate_steer", "delegate_wait",
    "checkpoint_list", "checkpoint_diff", "checkpoint_restore", "checkpoint_undo_turn",
    # Durable Task / Plan runtime: track a multi-step build as a hierarchical plan
    # whose tasks are dispatched through the normal run path. Create/steer only.
    *TASK_TOOLS,
    # Evidence-Driven Web Research Engine â€” research a technical question,
    # compare sources, verify a claim. Read-only; untrusted content is data.
    *WEB_RESEARCH_TOOLS,
    # Unified Retrieval / Search Intelligence — find evidence across documents,
    # research, memory, experiences and skills.
    *SEARCH_TOOLS,
    # Large-corpus sources — open, search and query folders/drives in place.
    *SOURCE_TOOLS,
    # Analysis sandbox (Phase 8) â€” compute a derived metric in isolation.
    "analysis_run",
    # Scaffold kits (Phase 9) â€” build a finance tool from a ready-made shape.
    "scaffold_kit",
    # Persistent bots: a code-session request for "a bot that runs every morning"
    # must reach bot_create here too. It is the SAME Automation surface and the
    # same single-use bot_schedule approval as the chat agent; without it, a
    # scheduled-agent request in a code task dead-ends.
    *AUTOMATION_TOOLS,
    # Generated capabilities: detect, build, test and (only when safe) activate.
    "capability_request", "capability_build", "capability_status", "capability_external",
    "capability_adapter",
    "capability_activate", "capability_rollback", "capability_remove",
    # Analysis package environment (install/remove approval-gated).
    "python_env_install", "python_env_remove", "python_env_list", "python_env_status",
    # Browser Automation Runtime â€” inspect/debug a local or public web app,
    # extract content and capture evidence. Local/private addresses always run
    # locally; the effectful half is narrowed by the autonomy profile.
    *BROWSER_TOOLS,
    # Computer Use Engine â€” drive a native desktop app (Excel, VS Code, a terminal
    # emulator, a PDF reader) when the UI is genuinely required. Prefer the
    # filesystem/code/terminal tools for their native work; CUA is the UI fallback.
    *COMPUTER_TOOLS,
    # Vision/Multimodal â€” analyze screenshots, UI mockups, charts, diagrams and
    # documents; OCR text; ground a UI element to coordinates before an action.
    *VISION_TOOLS,
    *DOCUMENT_TOOLS,
    # Change review (Phase 19) â€” report the repo's own rules over this task's
    # diff; read-only, and it records the review so a claim about it can be
    # checked against the ledger.
    "review_change",
    # Round 5 / C3: discovery, safe read-only dispatch, full-output pager,
    # confined read-only code over the catalog.
    "tool_search",
    # Schema-on-demand for a catalogue-only tool, before `tool_invoke`.
    "load_tool_schema",
    "tool_invoke",
    "read_tool_output",
    "tool_code",
    # C5: one spawn primitive + shared blackboard.
    "spawn", "board_write", "board_read", "board_search",
]

CODE_AGENT = AgentConfig(
    id="code-agent",
    name="Eclipse Code Agent",
    systemPrompt=CODE_AGENT_SYSTEM_PROMPT,
    tools=list(CODE_AGENT_TOOLS),
    maxSteps=24,               # a code task legitimately needs more turns than a query
    toolTimeoutMs=120_000,     # a unit-test/build run is slower than a data fetch
    # The AgentConfig run ceiling is 600_000; the Phase 4 repair wall-clock budget
    # is enforced separately inside the loop.
    runTimeoutMs=600_000,
    # Deliberately ABSENT: allowMcpTools.
)

# The CHAT code profile (Phase 14 Â§6.2). ADDITIVE: `ECLIPSE_AGENT`'s declared
# tools are NOT touched. When the front door recognizes a coding request it sends
# an `autonomy`; the resolver then UNIONS this set with the finance agent's own
# declared tools for that turn only, exactly as the code-agent narrows through
# `code.policy`. Round 3 / W2 (ADR-H280) made the union real: the resolver used
# to REPLACE the allowlist with this set, so a coding turn silently lost 441
# tools - the whole task/plan family, the whole web/research family, the
# unified search family, and the response-contract tools. Both halves are
# narrowed through the ONE policy authority, so `plan_only` still removes the
# write/execute tools and `edit_verify` still keeps them.
#
# It is still narrow: the read/navigate tools plus the write/execute/verify tools,
# and deliberately NO external action â€” `code_commit`/`code_push`/`code_open_pr`/
# `code_deploy` stay on the Code Tasks surface where the approval panel lives.
CHAT_CODE_TOOLS: tuple[str, ...] = (
    "workspace_status", "workspace_list",
    "workspace_read", "workspace_docs", "project_rules",
    "workspace_diff", "workspace_revert",
    "command_run", "terminal_run", "shell_run", "shell_spawn",
    "process_output", "process_kill", "process_list",
    # File-system subsystem (Phase FS): read/inspect always; the write/edit half
    # is removed at `plan_only` by the ONE policy authority.
    "list_directory", "read_file", "search_text", "search_files",
    "search_symbols", "find_references", "file_metadata", "file_hash",
    "file_permissions", "index_workspace", "watch_file", "watch_directory",
    "verify_file", "validate_changes", "git_status",
    "write_file", "create_file", "delete_file", "move_file", "copy_file",
    "rename_file", "create_directory", "delete_directory",
    "insert_text", "delete_text", "code_edit", "code_todo",
    "apply_diff",
    # Terminal subsystem (persistent shells + background processes + ports).
    "execute_command",
    "open_shell", "close_shell", "run_shell_command",
    "write_shell_input", "read_shell_output", "resize_shell", "send_shell_control",
    "list_terminal_sessions", "terminal_session_status",
    "start_process", "stop_process", "restart_process", "kill_process",
    "list_processes", "process_status", "read_process_output",
    "write_process_input", "send_signal", "process_tree", "process_health",
    "list_ports", "terminal_capabilities", "system_info", "resource_report",
    "scaffold_kit", "analysis_run", "eclipse_report",
    "capability_request", "capability_build", "capability_status", "capability_external",
    "capability_adapter",
    "capability_activate", "capability_rollback", "capability_remove",
    "python_env_install", "python_env_remove", "python_env_list", "python_env_status",
    # Code Execution & Verification Layer: run/build/test/discover + services +
    # evidence. The execute/build half is removed at `plan_only` by policy.
    "execute_code", "execute_python", "execute_javascript", "execute_typescript",
    "execute_sql", "execute_shell", "build_project", "test_project",
    "run_verification", "discover_tests",
    "start_service", "stop_service", "restart_service",
    "service_status", "service_logs", "list_services",
    "execution_get", "execution_logs", "execution_result",
    "execution_artifacts", "cancel_execution",
    # Application Runtime layer (DevOps) for a chat coding turn. The write half is
    # removed at `plan_only` by the ONE policy authority, so a question turn keeps
    # only the read-only surface.
    "runtime_detect", "runtime_capabilities", "runtime_inspect", "runtime_status",
    "runtime_health", "runtime_logs", "runtime_diagnose", "runtime_metrics",
    "runtime_metrics_history", "workspace_graph",
    "runtime_start", "runtime_stop", "runtime_restart", "runtime_recover",
    "workspace_start", "workspace_stop",
    "package_detect", "package_version", "package_list",
    "package_install", "package_run", "package_build", "package_test",
    "container_logs", "container_inspect", "container_list", "container_health",
    "compose_validate", "compose_config", "compose_ps", "compose_logs",
    "compose_up", "compose_down",
    "environment_inspect", "environment_validate",
    "port_check", "port_find_free", "port_list", "port_identify_process",
    "health_check", "health_status",
    # Orchestration / infra / cloud for a chat coding turn. Cluster/infra/cloud
    # mutations are removed at `edit_verify`/`plan_only` by the ONE policy authority.
    "k8s_status", "k8s_get", "k8s_get_pods", "k8s_describe", "k8s_logs",
    "k8s_rollout", "helm_list", "helm_status", "kustomize_build",
    "terraform_init", "terraform_validate", "terraform_plan",
    "terraform_output", "terraform_state",
    "cloud_providers", "deploy_status", "deploy_logs",
    "k8s_apply", "k8s_delete", "k8s_scale", "k8s_exec",
    "helm_install", "helm_uninstall", "terraform_apply", "terraform_destroy",
    "deploy_run", "deploy_rollback", "deploy_delete",
    # Code Intelligence & Understanding Engine (CIUE): semantic/structural
    # navigation + the one semantic rename (removed at `plan_only`).
    "code_get_definition", "code_find_references", "code_find_symbol",
    "code_search_symbols", "code_get_hover", "code_get_type",
    "code_get_diagnostics", "code_get_document_symbols", "code_get_callers",
    "code_get_callees", "code_get_call_hierarchy", "code_get_implementations",
    "code_get_imports", "code_get_exports", "code_get_dependencies",
    "code_get_dependents", "code_get_project_structure", "code_get_dependency_graph",
    "code_parse_file", "code_analyze_file", "code_organize_imports",
    "code_get_relevant_context", "code_index_workspace", "code_intel_status",
    "code_find_related_tests", "code_rename_symbol",
    # parallel read-only explore children for large codebases
    "delegate",
    "delegate_async", "delegate_status", "delegate_result",
    "delegate_cancel", "delegate_steer", "delegate_wait",
    "checkpoint_list", "checkpoint_diff", "checkpoint_restore", "checkpoint_undo_turn",
    # Git / Version-Control subsystem: read + LOCAL mutation only. External
    # actions (fetch/pull/push/PR/CI) stay on the Code Tasks surface beside the
    # approval panel, exactly like `code_commit`/`code_push`.
    "git_repository_info", "git_list_repositories", "git_status", "git_diff",
    "git_diff_cached", "git_diff_range", "git_log", "git_show", "git_commit_info",
    "git_blame", "git_reflog", "git_branch_list", "git_conflicts",
    "git_operation_state", "git_stash_list", "git_stash_show", "git_tag_list",
    "git_tag_show",
    "git_remote_list", "git_worktree_list", "git_submodule_list", "git_config_get",
    "git_config_list", "git_hooks", "git_lfs_status", "git_clean_preview",
    "git_ignore_check", "git_generate_commit_message", "git_recovery_options", "git_list_hunks",
    "git_stage", "git_unstage", "git_stage_hunks", "git_unstage_hunks", "git_apply_patch",
    "git_commit", "git_generate_and_commit",
    "git_create_branch", "git_checkout", "git_delete_branch", "git_rename_branch",
    "git_set_upstream", "git_merge", "git_merge_abort", "git_merge_continue",
    "git_rebase", "git_rebase_plan", "git_rebase_abort", "git_rebase_continue", "git_rebase_skip",
    "git_cherry_pick", "git_cherry_pick_abort", "git_cherry_pick_continue",
    "git_revert", "git_revert_abort", "git_revert_continue", "git_reset",
    "git_resolve_conflict", "git_abort_operation", "git_continue_operation",
    "git_stash_push", "git_stash_apply", "git_stash_pop", "git_stash_drop",
    "git_tag_create", "git_tag_delete", "git_worktree_add", "git_worktree_remove",
    "git_submodule_update", "git_bisect_start", "git_bisect_mark", "git_bisect_reset",
    "git_clean", "git_config_set",
    "git_move_path", "git_remove_paths", "git_restore", "git_unset_upstream",
    "git_stash_branch", "git_stash_clear", "git_worktree_prune", "git_worktree_move",
    "git_submodule_init", "git_submodule_sync",
    # read-only context, unchanged from ECLIPSE_AGENT
    "memory", "skill_list", "skill_view", "session_search", "session_open",
    "learning_review_list", "learning_candidate_list",
    "learning_candidate_validate", "app_api_get",
    "skill_version_history", "skill_reviews", "skill_discrimination_check",
    "skill_learning_get",
    # The acquire loop stays available in a coding turn: "install this skill /
    # MCP integration" is a valid code request and must not fall off the surface
    # when the front door classifies a mixed turn as `code_change`.
    "skill_install", "skill_uninstall", "skill_sources",
    "skill_create", "skill_update", "skill_delete",
    "skill_version_evaluate", "skill_version_activate", "skill_learning_set",
    "mcp_integrations", "mcp_install", "mcp_probe",
    # finance tools stay available: a code change may need the vault for a fixture
    "market_get_quote", "fundamentals_get_financials", "research_search",
    # Scheduling (Phase 17): a coding-turn run keeps the same scheduling surface as
    # an ordinary finance turn; the operator's master switch still governs it.
    *SCHEDULING_TOOLS,
    # Change review (Phase 19): the repo's own rules over this turn's diff.
    "review_change",
    # Browser Automation Runtime for a chat coding turn (debug a dev server on
    # localhost, QA a page). `plan_only` removes the effectful half via policy.
    *BROWSER_TOOLS,
    # Computer Use Engine for a chat coding turn (drive a native dev tool's UI).
    # `plan_only` removes the elevated half via the ONE policy authority.
    *COMPUTER_TOOLS,
    # UI Engineering: app-surface introspection + Eclipse's OWN saved-page
    # composition. A turn that mentions "repository", "tabs" or "list" is often
    # routed to this code profile, and without these the agent loses
    # `ui_get_application_map` exactly when it is asked where something lives in
    # the app (QA Round 1). UI configuration only â€” no filesystem/network/shell.
    *UI_TOOLS,
)
# Deliberately ABSENT: code_commit, code_push, code_open_pr, code_deploy.

# The dedicated Computer Use agent. A compact, purpose-built toolset (instead of
# the 366-tool finance agent) so tool selection is about the DESKTOP task, and a
# protocol prompt that makes the model verify → look → act one step at a time.
# It can delegate bulk data work to the code agent and verify the result visually.
DESKTOP_AGENT = AgentConfig(
    id="desktop-agent",
    name="Desktop Agent",
    systemPrompt=DESKTOP_AGENT_SYSTEM_PROMPT,
    tools=[
        "computer_use", "computer_read_screen", "computer_capabilities",
        "computer_status", "computer_doctor", "computer_windows", "computer_apps",
        "computer_displays", "computer_session_start", "computer_session_list",
        "computer_session_close", "computer_takeover", "computer_request_human",
        "computer_pause", "computer_resume", "computer_release",
        "computer_remember", "computer_recall",
        "computer_finance_read", "computer_finance_prepare",
        "visual_ground", "vision_analyze", "screenshot_analyze",
        "delegate", "delegate_status", "delegate_result",
    ],
    maxSteps=32,
    toolTimeoutMs=60_000,
    runTimeoutMs=600_000,
    maxToolCalls=120,
)

AGENT_PRESETS: dict[str, AgentConfig] = {
    ECLIPSE_AGENT.id: ECLIPSE_AGENT,
    CODE_NAVIGATOR.id: CODE_NAVIGATOR,
    CODE_AGENT.id: CODE_AGENT,
    BOOKKEEPER.id: BOOKKEEPER,
    DESKTOP_AGENT.id: DESKTOP_AGENT,
}

DEFAULT_AGENT_ID = ECLIPSE_AGENT.id


def get_agent(agent_id: str) -> AgentConfig:
    agent = AGENT_PRESETS.get(agent_id)
    if agent is None:
        known = ", ".join(AGENT_PRESETS.keys())
        raise ValueError(f"Unknown agent {agent_id!r}. Known: {known}")
    return agent


__all__ = [
    "AGENT_PRESETS",
    "BROWSER_TOOLS",
    "DRY_RUN_TOOLS",
    "dry_run_tools",
    "CHAT_CODE_TOOLS",
    "CODE_AGENT",
    "CODE_AGENT_TOOLS",
    "CODE_NAVIGATOR",
    "CODE_NAVIGATOR_TOOLS",
    "COMPUTER_TOOLS",
    "DEFAULT_AGENT_ID",
    "DESKTOP_AGENT",
    "ECLIPSE_AGENT",
    "SEARCH_TOOLS",
    "get_agent",
]
