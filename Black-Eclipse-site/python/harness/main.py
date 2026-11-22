"""FastAPI application for the harness runtime.

Endpoints (called by the thin TS front door):
    GET  /health                 â€” readiness for the TS proxy
    GET  /models                 â€” universal provider catalog
    GET  /agents                 â€” agent presets
    POST /runs                   â€” execute (or background) a run
    POST /runs/{id}/cancel       â€” cooperative cancellation
    GET  /runs                   â€” list runs
    GET  /runs/{id}              â€” one run
    GET  /runs/{id}/events       â€” persisted trace
    GET  /usage                  â€” token-consumption analytics

The service never trusts the caller: pydantic validates every body.
"""

from __future__ import annotations
import asyncio, logging, os, time  # one line: this module is line-ratcheted
from typing import Any, Optional
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agents import AGENT_PRESETS, DEFAULT_AGENT_ID
from .boot import lifespan
from .deps import (
    IDENTITY_BOUNDARY,
    _LOOPBACK_HOSTS,
    _is_loopback,
    _operator_secret_configured,
    _valid_operator_token,
    deployment_exposure,
    deployment_guard,
    identity_boundary,
    require_operator,
    service,
)
from .models import list_providers
from .model_runtime import ALIASES, get_runtime
from .persistence import now_iso
from .run_stream import run_event_frames
from .research.engine import run_research
from .research.mandate import parse_mandate
from .routers.agent_presets import router as agent_presets_router
from .routers.personas import router as personas_router
from .routers.browser import browser_health, router as browser_router
from .routers.code import router as code_router
from .routers.delegations import router as delegations_router
from .routers.channels import router as channels_router
from .routers.application import router as application_router
from .routers.artifacts import router as artifacts_router
from .routers.canvas import router as canvas_router                      
from .routers.charts import router as charts_router
from .routers.notebook import router as notebook_router
from .routers.a2a import router as a2a_router
from .routers.team import router as team_router
from .routers.advisory import router as advisory_router
from .routers.automation import router as automation_router
from .routers.code_intel import router as code_intel_router
from .routers.runs import router as runs_router
from .routers.sandbox import router as sandbox_router
from .routers.computer import router as computer_router
from .routers.events import router as events_router
from .routers.evaluation import router as evaluation_router
from .routers.eval_gates import router as eval_gates_router
from .routers.evolution import router as evolution_router; from .routers.dynamic import router as dynamic_router; from .routers.kernel_ui import router as kernel_ui_router
from .routers.evolution_corpus import router as evolution_corpus_router
from .routers.execution import router as execution_router
from .routers.fabric import router as fabric_router
from .routers.financial import router as financial_router
from .routers.finance_guardrails import router as finance_guardrails_router
from .routers.git import router as git_router
from .routers.graph_assets import router as graph_assets_router
from .routers.graphs import router as graphs_router
from .routers.grants import router as grants_router
from .routers.jobs import router as jobs_router
from .routers.learning import router as learning_router
from .routers.mcp import router as mcp_router
from .routers.openbb import router as openbb_router
from .routers.market import router as market_router
from .routers.memory import router as memory_router
from .routers.ml import router as ml_router
from .routers.observability import router as observability_router
from .routers.orchestration import router as orchestration_router
from .routers.plugins import router as plugins_router
from .routers.ledger import router as ledger_router
from .routers.portfolio import router as portfolio_router
from .routers.quant import router as quant_router
from .routers.quant_desk import router as quant_desk_router
from .routers.quant_reports import router as quant_reports_router
from .routers.quant_live import router as quant_live_router
from .routers.bot_trading import router as bot_trading_router
from .routers.quant_research import router as quant_research_router
from .routers.quant_system import router as quant_system_router
from .routers.projects import router as projects_router
from .routers.gallery import router as gallery_router
from .routers.pine import router as pine_router
from .routers.research import router as research_router
from .routers.research_data import router as research_data_router
from .routers.search import router as search_router
from .routers.sources import router as sources_router
from .routers.security import router as security_router, security_health_safe
from .routers.sessions import router as sessions_router
from .routers.skills import router as skills_router
from .routers.tasks import router as tasks_router
from .routers.terminal import router as terminal_router
from .routers.trading import router as trading_router
from .routers.transcription import router as transcription_router
from .routers.ui import router as ui_router
from .routers.video import router as video_router
from .routers.workspace import router as workspace_router
from .routers.recovery import router as recovery_router
from .routers.autonomy import router as autonomy_router
from .routers.capabilities import router as capabilities_router
from .schemas import ModelSettings, RunRequest, RunResult
from .sdk.middleware import install_sdk
from .skills.draft import propose_draft
from .skills.evaluation import evaluate_skill
from .skills.learning import capture_from_run
from .skills.lifecycle import approve_promotion, reject_promotion, request_promotion, rollback
from .skills.permissions import Actor
from .skills.runtime import SkillExecutionRequest
logging.basicConfig(level=logging.INFO, format="[harness-py] %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="BlackEclipse Harness Runtime", version="0.1.0", lifespan=lifespan)
install_sdk(app)

# Route groups extracted into `harness/routers/` (public surface: `harness.main:app`).
app.include_router(agent_presets_router)
app.include_router(personas_router)
app.include_router(a2a_router)
app.include_router(team_router)
app.include_router(advisory_router)
app.include_router(application_router)
app.include_router(artifacts_router)
app.include_router(canvas_router)
app.include_router(charts_router)
app.include_router(notebook_router)
app.include_router(automation_router)
app.include_router(bot_trading_router)
app.include_router(code_router)
app.include_router(delegations_router)
app.include_router(channels_router)
app.include_router(code_intel_router)
app.include_router(runs_router)
app.include_router(sandbox_router)
app.include_router(browser_router)
app.include_router(computer_router)
app.include_router(events_router)
app.include_router(evaluation_router)
app.include_router(eval_gates_router)
app.include_router(evolution_router); app.include_router(dynamic_router); app.include_router(kernel_ui_router)
app.include_router(evolution_corpus_router)
app.include_router(execution_router)
app.include_router(fabric_router)
app.include_router(financial_router)
app.include_router(finance_guardrails_router)
app.include_router(git_router)
app.include_router(grants_router)
app.include_router(graph_assets_router)
app.include_router(graphs_router)
app.include_router(jobs_router)
app.include_router(learning_router)
app.include_router(mcp_router)
app.include_router(openbb_router)
app.include_router(market_router)
app.include_router(memory_router)
app.include_router(ml_router)
app.include_router(observability_router)
app.include_router(orchestration_router)
app.include_router(plugins_router)
app.include_router(portfolio_router)
app.include_router(ledger_router)
app.include_router(trading_router)
app.include_router(transcription_router)
app.include_router(quant_router)
app.include_router(quant_reports_router)
app.include_router(quant_desk_router)
app.include_router(quant_research_router)
app.include_router(quant_live_router)
app.include_router(quant_system_router)
app.include_router(projects_router)
app.include_router(gallery_router)
app.include_router(pine_router)
app.include_router(research_router)
app.include_router(research_data_router)
app.include_router(search_router)
app.include_router(sources_router)
app.include_router(security_router)
app.include_router(sessions_router)
app.include_router(skills_router)
app.include_router(tasks_router)
app.include_router(terminal_router)
app.include_router(ui_router)
app.include_router(video_router)
app.include_router(workspace_router)
app.include_router(recovery_router)
app.include_router(autonomy_router)
app.include_router(capabilities_router)
# Process start time (epoch ms). The TS launcher compares this against the
# build time of `python/harness/` to detect an orphaned runtime from an earlier
# `npm run dev` that is still holding the port and serving stale code.
_STARTED_AT_MS = int(time.time() * 1000)
def _jsonschema_available() -> bool:
    """Whether the full JSON Schema validator is installed (skill contracts)."""
    from .skills.schema_validation import jsonschema_available
    return jsonschema_available()
class StartRunResponse(BaseModel):
    success: bool
    runId: str
    status: str = "running"

@app.get("/health/live")
def health_live() -> dict[str, Any]:
    """Cheap liveness only — the supervisor's readiness probe.

    `/health` below is a DIAGNOSTIC document: it probes runtime providers,
    browser availability, code-intel state and a security audit. Under load
    that took 20+ seconds (measured 2026-11-13 while a build was running), so
    the front door's 10 s probe declared a perfectly healthy runtime dead,
    killed it, and respawned it in a loop — the "stuck at preparing analysis"
    symptom.

    This endpoint must stay constant-time: no I/O, no subsystem calls, no
    registries. It answers exactly the fields the supervisor reasons about.
    """
    return {
        "ok": True,
        "service": "harness-runtime",
        "startedAtMs": _STARTED_AT_MS,
        "activeRuns": len(getattr(service, "_active", {}) or {}),
        "pid": os.getpid(),
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "harness-runtime",
        "startedAtMs": _STARTED_AT_MS,
        "activeRuns": len(getattr(service, "_active", {}) or {}),  # in-flight runs; never restart a busy runtime
        "tools": service.registry.names(),
        "agents": list(AGENT_PRESETS.keys()),
        "defaultAgentId": DEFAULT_AGENT_ID,
        "providers": [p["id"] for p in list_providers()],
        "retrieval": service.retrieval_metrics(),
        # The RESOLVED memory/skills database path + non-secret identity. This is
        # what makes a cwd-mismatch observable instead of silent. Deliberately
        # lighter than /eclipse-memory/health (no integrity_check) because this
        # endpoint is polled by the launcher.
        "eclipseMemory": {
            **service.memory.store.db.identity_summary(),
            "schemaVersion": service.memory.store.migration_status()["schemaVersion"],
            "upToDate": service.memory.store.migration_status()["upToDate"],
        },
        "runStore": {"path": service.store.path},
        "skills": {
            "installed": len(service.skill_registry.all_manifests()),
            "loadErrors": service.skill_load_errors,
            "duplicates": service.skill_registry.duplicate_ids(),
            # Whether full JSON Schema contract validation is available. False
            # means skill schemas fail closed (cannot be verified), so an
            # operator can see why and repair the runtime.
            "schemaValidation": _jsonschema_available(),
        },
        # MCP (Phase 5): configured/connected server counts + registered tool
        # count. Never includes secret values or full server output.
        "mcp": service.mcp.summary(),
        "plugins": service.plugins.summary() if getattr(service, "plugins", None) is not None else {},  # noqa: E501
        # Code Intelligence & Understanding Engine (CIUE): the process-wide runtime
        # state (index size, running language tools). Content-free.
        "codeIntel": (
            service.code_intel.status() if getattr(service, "code_intel", None) is not None
            else {"available": False}
        ),
        # Identity/tenancy boundary (Phase 1): an explicit, machine-readable
        # statement that this is a single-user local app with no authentication
        # and no tenancy isolation, plus the current exposure assessment.
        "identity": identity_boundary(),
        # Application Runtime provider readiness (non-secret): which
        # orchestration/infra/cloud tools are installed AND authenticated, so a
        # missing tool is a boot-time instruction, not a surprise (60s cache).
        "runtimeProviders": _runtime_readiness(),
        # Browser Automation Runtime: honest availability + the provider list.
        # Content-free; a missing Playwright/Chromium reads as available:false.
        "browser": browser_health(),
        "security": security_health_safe(),
    }

def _runtime_readiness() -> dict[str, Any]:
    """The provider readiness summary, or an honest empty report. Never raises."""
    manager = getattr(service, "application_runtime", None)
    if manager is None:
        return {"ready": None, "providers": [], "missing": [], "unauthenticated": [], "hints": []}
    try:
        report = manager.readiness()
        # `/health` is polled: return only the summary + the gaps, not the full
        # per-provider table (that is `/runtime/providers`).
        return {
            "ready": report.get("ready"),
            "missing": report.get("missing", []),
            "unauthenticated": report.get("unauthenticated", []),
            "hints": report.get("hints", []),
            "count": len(report.get("providers", [])),
        }
    except Exception:  # noqa: BLE001 - readiness must never break /health
        return {"ready": None, "providers": [], "missing": [], "unauthenticated": [], "hints": []}

@app.get("/retrieval/metrics")
def retrieval_metrics() -> dict[str, Any]:
    """Retrieval-layer performance counters (cache hits/misses, in-flight)."""
    return {"success": True, **service.retrieval_metrics()}

class ResearchRunRequest(BaseModel):
    """A research-mandate request (Phase 4). The front door proxies this; the
    runtime owns the mandate â†’ coverage flow. `query` is the objective."""
    query: str
    ticker: Optional[str] = None
    market: Optional[str] = None
    asOf: Optional[str] = None
    callbackBaseUrl: str
    callbackToken: Optional[str] = None
    runId: Optional[str] = None

@app.post("/research/mandate")
def research_mandate(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify an objective into a typed research mandate (no retrieval)."""
    mandate = parse_mandate(
        str(payload.get("query") or ""),
        ticker=payload.get("ticker"),
        market=payload.get("market"),
        as_of=payload.get("asOf"),
    )
    return {"success": True, "mandate": mandate.model_dump()}

@app.post("/research/run")
async def research_run(request: ResearchRunRequest) -> JSONResponse:
    """Execute the evidence-grounded research flow for one objective.
    Returns the mandate, per-leg coverage, validated claims and the durable
    receipt. A general (unsupported) objective returns `supported=False` so the
    caller continues on the existing chat path.
    """
    mandate = parse_mandate(
        request.query, ticker=request.ticker, market=request.market, as_of=request.asOf
    )
    if not mandate.supported:
        return JSONResponse(
            {
                "success": True,
                "supported": False,
                "mandate": mandate.model_dump(),
                "reason": "objective is not a supported financial comparison",
            }
        )

    bridge = service.build_bridge(request.callbackBaseUrl, request.callbackToken)
    run_id = request.runId or f"research_{now_iso()}"

    async def _retrieve(req):  # noqa: ANN001 - shaped like the layer
        return await service.retrieve_for_research(bridge, req)

    outcome = await run_research(
        mandate, _retrieve, run_id=run_id,
        provider=None, model=None,
    )
    persistence_error: Optional[str] = None
    try:
        if outcome.receipt is not None:
            service.store.save_receipt(outcome.receipt.model_dump())
    except Exception as exc:  # noqa: BLE001 - persistence failure is observable, never hidden
        logging.exception("research receipt persistence failed")
        persistence_error = str(exc)

    return JSONResponse(
        {
            "success": True,
            "supported": True,
            "status": outcome.status,
            "mandate": outcome.mandate.model_dump(),
            "coverage": outcome.coverage.model_dump(),
            "metadata": outcome.metadata.model_dump() if outcome.metadata else None,
            "receipt": outcome.receipt.model_dump() if outcome.receipt else None,
            "persistenceError": persistence_error,
        }
    )


@app.get("/research/receipts")
def list_receipts(limit: int = 50) -> dict[str, Any]:
    return {"success": True, "receipts": service.store.list_receipts(limit)}

@app.get("/research/receipts/{run_id}")
def get_receipt(run_id: str) -> dict[str, Any]:
    receipt = service.store.get_receipt(run_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return {"success": True, "receipt": receipt}

@app.get("/models")
def models() -> dict[str, Any]:
    return {"success": True, "providers": list_providers()}


# â”€â”€ Model Runtime (provider-agnostic model layer) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Read-only inspection endpoints. They never call a provider and never expose
# credentials; the routing debug endpoint is pure computation.
@app.get("/model-runtime/providers")
def model_runtime_providers(_: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "providers": [p.to_dict() for p in get_runtime().providers.list_providers()]}


@app.get("/model-runtime/models")
def model_runtime_models(provider: Optional[str] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    runtime = get_runtime()
    models = runtime.models.list_models(provider_id=provider)
    return {"success": True, "models": [m.to_dict() for m in models]}


@app.post("/model-runtime/providers/{provider_id}/discover")
async def model_runtime_discover(
    provider_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)
) -> dict[str, Any]:
    """Discover the models a live provider actually serves and register them.

    A local server (Ollama, LM Studio) lists its own installed models, and a
    router lists its catalog; the static registry cannot know them. The operator
    supplies the connection details in the body (the runtime cannot read the TS
    vault); credentials are used only for this one probe and never returned or
    stored. A failed probe returns `ok: false` with an honest reason instead of
    an empty success.
    """
    runtime = get_runtime()
    provider = runtime.providers.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider_id}'")
    settings = ModelSettings(
        provider=provider_id,
        base_url=payload.get("baseUrl") or provider.default_base_url,
        api_key=payload.get("apiKey"),
        model=payload.get("model") or provider.metadata.get("defaultModel") or "",
    )
    try:
        # The raw served ids (the provider's own list) plus the merged registry
        # view. `served` is what is genuinely installed/served â€” the registry
        # view also holds declared placeholders that may not exist.
        served = await runtime.discover_served(settings, provider_id=provider_id)
        discovered = runtime.models.discover_models(provider_id, served)
    except Exception as exc:  # noqa: BLE001 - a probe failure is reported, not fatal
        return {
            "success": True,
            "ok": False,
            "detail": str(exc) or "discovery failed",
            "models": [],
            "served": [],
        }
    return {
        "success": True,
        "ok": True,
        "models": [m.to_dict() for m in discovered],
        "served": [s for s in served if s],
    }

@app.get("/model-runtime/routing")
def model_runtime_routing(_: bool = Depends(require_operator)) -> dict[str, Any]:
    runtime = get_runtime()
    return {
        "success": True,
        "taskProfiles": runtime.task_profiles.names(),
        "aliases": sorted(ALIASES.keys()),
        "policy": {
            "baseWeights": runtime.policy.base_weights,
            "fallbackDepth": runtime.policy.fallback_depth,
            "fallbackEnabled": runtime.fallback_enabled,
        },
    }


@app.get("/model-runtime/routing/decisions")
def model_runtime_decisions(limit: int = 20, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "decisions": get_runtime().recent_decisions(limit)}


@app.post("/model-runtime/route")
def model_runtime_route(payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Explain which model the router would pick for a task (debug view Â§43)."""
    runtime = get_runtime()
    decision = runtime.route(
        model=payload.get("model"),
        task=payload.get("task"),
        requirements=payload.get("requirements") or {},
        policy_overrides=payload.get("policy") or {},
    )
    return {"success": True, "decision": decision.to_dict()}


@app.get("/model-runtime/health")
def model_runtime_health(_: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, **get_runtime().health_report()}


@app.get("/model-runtime/usage")
def model_runtime_usage(_: bool = Depends(require_operator)) -> dict[str, Any]:
    runtime = get_runtime()
    summary = runtime.usage_summary()
    store = getattr(service, "model_runtime_store", None)
    if store is not None:
        summary["persisted"] = {
            "recentRequests": store.list_model_requests(50),
            "health": store.model_health_summary(),
        }
    return {"success": True, **summary}

@app.get("/agents")
def agents() -> dict[str, Any]:
    return {
        "success": True,
        "defaultAgentId": DEFAULT_AGENT_ID,
        "agents": [a.model_dump() for a in AGENT_PRESETS.values()],
    }

@app.post("/runs")
async def start_run(http_request: Request, request: RunRequest, background: bool = False, _: bool = Depends(require_operator)) -> JSONResponse:
    from .security.identity import bind_run_identity
    request = bind_run_identity(http_request, request)
    try:
        if background:
            await service.run_background(request)
            return JSONResponse(StartRunResponse(success=True, runId=request.runId).model_dump(), status_code=202)
        result: RunResult = await service.run(request)
        return JSONResponse({"success": True, **result.model_dump()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logging.exception("run failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

@app.get("/runs")
def list_runs(limit: int = 50) -> dict[str, Any]:
    return {"success": True, "runs": service.list_runs(limit)}

@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"success": True, "run": run}


@app.get("/usage")
def usage(days: int = 30) -> dict[str, Any]:
    """Token-consumption analytics over the agent-memory database.

    `byDay`/`byModel`/`recent` rows may total 0 when a provider omitted usage;
    `totals.runsWithUsage` says how many runs actually reported tokens, so the
    UI can be honest instead of showing a fabricated figure.
    """
    return {"success": True, **service.usage_summary(days)}


@app.post("/history/compact")
async def history_compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact an explicit conversation history (the `/compact` command).

    The front door already holds the conversation; this endpoint only derives
    the summary so the same compaction rules are used in one place. It never
    calls a model: the deterministic skeleton is the result, and a history with
    nothing older than `keepTurns` returns `ok: false` with a reason instead of
    inventing a summary.
    """
    raw = payload.get("history")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="history must be an array")
    turns: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower()
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        turns.append({"role": role, "content": content})
    raw_keep = payload.get("keepTurns", 6)
    try:
        keep = int(raw_keep)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="keepTurns must be an integer")
    if keep < 0 or keep > 50:
        raise HTTPException(status_code=400, detail="keepTurns must be between 0 and 50")

    from .compaction import compact_history  # noqa: PLC0415 - keep startup light

    result = await compact_history(turns, keep_turns=keep)
    return {"success": True, **result.to_dict()}


@app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """Server-Sent Events for one run: live status + token deltas.

    Emits `id: <seq>` and `event: <type>` frames. A reconnect can pass `after`
    (or `Last-Event-ID`) to resume exactly after the last event it saw; without
    it the full persisted trace is replayed first. Token deltas arrive as
    transient event types (e.g. `model.delta`) and are NOT persisted.
    """
    try:
        last_id = int(request.headers.get("last-event-id") or 0)
    except (TypeError, ValueError):
        last_id = 0
    since = max(int(after or 0), last_id)
    queue = service.bus.open_stream(run_id)
    return StreamingResponse(
        run_event_frames(service, run_id, queue, since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


class CancelResponse(BaseModel):
    success: bool
    runId: str
    cancelled: bool


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    if not service.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    cancelled = service.cancel(run_id)
    return CancelResponse(success=True, runId=run_id, cancelled=cancelled).model_dump()


@app.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, request: RunRequest, background: bool = False, _: bool = Depends(require_operator)) -> JSONResponse:
    """Resume a crashed run from its latest checkpoint (Phase 1).

    The run id in the path is authoritative. A resume of a run with no checkpoint
    returns CHECKPOINT_NOT_FOUND; a resume of a terminal run returns
    RESUME_CONFLICT â€” never a fresh run in its place. Already-executed tool calls
    are reused from the idempotency ledger, not re-run.
    """
    if request.runId != run_id:
        raise HTTPException(status_code=400, detail="body runId does not match the path")
    # Default the resume point to the latest checkpoint when the caller did not
    # name one. An explicit `resumeFrom` is honored (a specific checkpoint id).
    if not request.resumeFrom:
        latest = service.run_lineage().latest_checkpoint(run_id)
        if latest is not None:
            request = request.model_copy(update={"resumeFrom": latest["id"]})
    try:
        if background:
            await service.run_background(request)
            return JSONResponse(StartRunResponse(success=True, runId=run_id).model_dump(), status_code=202)
        result: RunResult = await service.run(request)
        return JSONResponse({"success": True, **result.model_dump()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/runs/{run_id}/lineage")
def run_lineage_endpoint(run_id: str) -> dict[str, Any]:
    """The bounded parent chain + direct children of a run (Phase 1)."""
    if not service.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return {"success": True, **service.run_lineage().lineage(run_id)}


# â”€â”€ Workflows (Phase 5) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Definitions are validated before storage. Every MUTATION route requires an
# authorized operator (transport/token â€” never a body boolean). Execution is a
# normal run and is not blanket operator-gated (an enabled read-only workflow).

class WorkflowRunRequest(BaseModel):
    """Run a saved workflow over the existing graph (Phase 5)."""

    workflowId: str  # noqa: N815
    runId: str  # noqa: N815
    input: dict[str, Any] = Field(default_factory=dict)
    agentId: Optional[str] = None  # noqa: N815
    userId: Optional[str] = None  # noqa: N815
    sessionId: Optional[str] = None  # noqa: N815
    profileId: Optional[str] = None  # noqa: N815
    projectId: Optional[str] = None  # noqa: N815
    callbackBaseUrl: str  # noqa: N815
    callbackToken: Optional[str] = None  # noqa: N815
    modelSettings: dict[str, Any]  # noqa: N815


@app.get("/workflows")
def list_workflows(limit: int = 50) -> dict[str, Any]:
    return {"success": True, "workflows": service.workflow_store().list(limit)}


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str, version: Optional[str] = None) -> dict[str, Any]:
    workflow = service.workflow_store().get(workflow_id, version)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"success": True, "workflow": workflow.model_dump()}


@app.post("/workflows")
def save_workflow(payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Create/update a workflow. Validated before storage (named reason on reject)."""
    from .workflows import Workflow, WorkflowValidationError

    try:
        workflow = Workflow(**payload)
    except Exception as exc:  # noqa: BLE001 - a malformed definition is a 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = service.workflow_store().save(workflow, known_tools=list(service.registry.names()))
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=f"{exc.reason}") from exc
    return {"success": True, **result}


@app.delete("/workflows/{workflow_id}")
def delete_workflow(workflow_id: str, version: Optional[str] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    deleted = service.workflow_store().delete(workflow_id, version)
    return {"success": True, "deleted": deleted}


@app.post("/workflows/run")
async def execute_workflow(request: WorkflowRunRequest) -> JSONResponse:
    """Execute a saved workflow over the EXISTING graph (flag-gated, default off)."""
    from .schemas import ModelSettings as _ModelSettings
    from .schemas import RunInput as _RunInput
    from .schemas import RunRequest as _RunRequest

    try:
        run_request = _RunRequest(
            runId=request.runId,
            input=_RunInput(query=str(request.input.get("query") or "workflow"), context=request.input),
            agentId=request.agentId,
            modelSettings=_ModelSettings(**request.modelSettings),
            userId=request.userId,
            sessionId=request.sessionId,
            profileId=request.profileId,
            projectId=request.projectId,
            workflowId=request.workflowId,
            callbackBaseUrl=request.callbackBaseUrl,
            callbackToken=request.callbackToken,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await service.run_workflow(run_request)
    return JSONResponse({"success": True, **result})


# â”€â”€ Skills (Phase 3) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The front door proxies these; Python owns the registry, permissions and
# lifecycle. Authorization is explicit: `operator=true` is only set by the front
# door after it authenticated the runtime secret (or a direct caller presenting
# it). The default actor is read-only and cannot create/enable/review skills.


def _actor(operator: bool = False, actor_id: str = "local") -> Actor:
    from .skills.permissions import OPERATOR_ACTOR

    return OPERATOR_ACTOR if operator else Actor(id=actor_id)

@app.get("/skills/authoring/tools")
def authorable_tools() -> dict[str, Any]:
    """Tools a user skill may require, with the safe permission vocabulary."""
    from .skills.permissions import SAFE_PERMISSIONS

    return {
        "success": True,
        "tools": service.registry.names(),
        "permissions": sorted(SAFE_PERMISSIONS),
        "categories": ["research", "quality", "finance", "system"],
        "riskLevels": ["low", "medium"],
    }


@app.post("/skills/author")
def create_user_skill(payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Create a user-authored skill (operator-gated)."""
    if not bool(payload.get("operator")):
        raise HTTPException(status_code=403, detail="skill.create requires an authorized operator")
    from .skills.authoring import SkillAuthoringError

    try:
        result = service.skill_registry.create_user_skill(
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            instructions=str(payload.get("instructions") or ""),
            category=str(payload.get("category") or "research"),
            tags=payload.get("tags"),
            required_tools=payload.get("requiredTools"),
            permissions=payload.get("permissions"),
            risk_level=str(payload.get("riskLevel") or "low"),
            skill_id=payload.get("skillId"),
        )
    except SkillAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@app.put("/skills/{skill_id}/author")
def update_user_skill(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    if not bool(payload.get("operator")):
        raise HTTPException(status_code=403, detail="skill.create requires an authorized operator")
    from .skills.authoring import SkillAuthoringError

    try:
        result = service.skill_registry.update_user_skill(skill_id, **payload)
    except SkillAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@app.delete("/skills/{skill_id}/author")
def delete_user_skill(skill_id: str, payload: Optional[dict[str, Any]] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    payload = payload or {}
    if not bool(payload.get("operator")):
        raise HTTPException(status_code=403, detail="skill.create requires an authorized operator")
    from .skills.authoring import SkillAuthoringError

    try:
        result = service.skill_registry.delete_user_skill(skill_id)
    except SkillAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@app.post("/skills/discover")
def discover_skills(payload: dict[str, Any]) -> dict[str, Any]:
    results = service.skill_registry.discover(
        query=str(payload.get("query") or ""),
        category=payload.get("category"),
        tags=payload.get("tags"),
        status=payload.get("status"),
        risk_level=payload.get("riskLevel"),
        include_non_executable=bool(payload.get("includeNonExecutable")),
    )
    return {"success": True, "results": results}


@app.post("/skills/validate-input")
def validate_skill_input(payload: dict[str, Any]) -> dict[str, Any]:
    from .skills.runtime import validate_input

    skill_id = str(payload.get("skillId") or "")
    manifest = service.skill_registry.manifest(skill_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    ok, issues = validate_input(manifest.inputSchema, payload.get("input") or {}, skill_id)
    return {"success": True, "valid": ok, "issues": issues}


@app.post("/skills/{skill_id}/execute")
async def execute_skill_endpoint(
    skill_id: str, payload: dict[str, Any], request: Request,
) -> JSONResponse:
    # Execution is a legitimate NON-operator action (an enabled read-only skill),
    # so this endpoint is not blanket operator-gated. But the body `operator`
    # flag widens the actor to the operator permission set, so it may only be
    # honoured when authorization is PROVEN by transport/token â€” never by the
    # body alone. A remote caller that sends `{"operator": true}` without proof
    # is downgraded to the safe read-only actor.
    proven = _is_loopback(request) or _valid_operator_token(request)
    body = {**payload, "skillId": skill_id, "operator": bool(payload.get("operator")) and proven}
    execution = SkillExecutionRequest(**body)
    result = await service.run_skill(execution)
    return JSONResponse({"success": True, **result.model_dump()})


@app.get("/skill-executions")
def list_skill_executions(skillId: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    return {"success": True, "executions": service.skill_store.list_executions(skillId, limit)}


@app.get("/skill-executions/{run_id}")
def get_skill_execution(run_id: str) -> dict[str, Any]:
    execution = service.skill_store.get_execution(run_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Skill execution not found")
    return {"success": True, "execution": execution}


@app.post("/skill-executions/{run_id}/cancel")
def cancel_skill_execution(run_id: str) -> dict[str, Any]:
    cancelled = service.cancel(run_id)
    return {"success": True, "runId": run_id, "cancelled": cancelled}


@app.post("/skills/{skill_id}/evaluate")
def evaluate_skill_endpoint(skill_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .config.flags import get_bool  # noqa: PLC0415

    result = evaluate_skill(
        skill_id, payload.get("version"), service.skill_registry,
        run_regression=bool(payload.get("runRegression")),
        base_version=payload.get("baseVersion"),
        metrics=payload.get("metrics"),
        base_metrics=payload.get("baseMetrics"),
        require_execution=get_bool("HARNESS_SKILL_EXEC_EVAL"),
        execution_invoker=service.build_execution_eval_invoker(
            (payload or {}).get("modelSettings")
        ),
    )
    return {"success": result.get("ok", False), **result}


@app.get("/skills/{skill_id}/evaluations")
def list_skill_evaluations(skill_id: str) -> dict[str, Any]:
    if service.skill_registry.manifest(skill_id) is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"success": True, "evaluations": service.skill_store.list_evaluations(skill_id, 50)}


@app.get("/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str) -> dict[str, Any]:
    evaluation = service.skill_store.get_evaluation(evaluation_id)
    if evaluation is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return {"success": True, "evaluation": evaluation}


@app.post("/skills/{skill_id}/enable")
def enable_skill(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    result = service.skill_registry.set_status(
        skill_id, "enabled", _actor(bool(payload.get("operator"))), payload.get("version")
    )
    return {"success": result.get("ok", False), **result}


@app.post("/skills/{skill_id}/disable")
def disable_skill(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    result = service.skill_registry.set_status(
        skill_id, "disabled", _actor(bool(payload.get("operator"))), payload.get("version")
    )
    return {"success": result.get("ok", False), **result}


@app.post("/skills/{skill_id}/deprecate")
def deprecate_skill(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    result = service.skill_registry.deprecate(
        skill_id, _actor(bool(payload.get("operator"))),
        reason=str(payload.get("reason") or "deprecated"),
        superseded_by=payload.get("supersededBy"),
    )
    return {"success": result.get("ok", False), **result}


@app.post("/skills/{skill_id}/rollback")
def rollback_skill(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    result = rollback(
        service.skill_registry, skill_id=skill_id,
        to_version=str(payload.get("version") or ""),
        actor=_actor(bool(payload.get("operator"))),
        reason=str(payload.get("reason") or "rollback"),
    )
    return {"success": result.get("ok", False), **result}


# â”€â”€ Versioned skill patches (M2) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@app.get("/skills/{skill_id}/version-history")
def skill_version_history(skill_id: str) -> dict[str, Any]:
    """Versions with patch/eval/canary/rollback metadata for the UI."""
    from .skills.versioning import version_history  # noqa: PLC0415

    return {"success": True, "skillId": skill_id, "versions": version_history(service.skill_registry, skill_id)}


@app.post("/skills/{skill_id}/patch")
def propose_skill_patch(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Propose a structured patch. Creates a NEW inert version; the active version
    is never mutated. A stale base version is rejected."""
    from .skills.versioning import propose_patch  # noqa: PLC0415

    body = {**payload, "skill": payload.get("skill") or skill_id}
    result = propose_patch(
        service.skill_registry, body, actor=_actor(bool(payload.get("operator"))),
        source_run_id=payload.get("sourceRunId"), review_id=payload.get("reviewId"),
    )
    return {"success": result.get("ok", False), **result}


@app.post("/skills/{skill_id}/patch/{version}/evaluate")
def evaluate_skill_version(skill_id: str, version: str, payload: Optional[dict[str, Any]] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    from .skills.versioning import evaluate_version  # noqa: PLC0415

    from .config.flags import get_bool  # noqa: PLC0415

    result = evaluate_version(
        service.skill_registry, skill_id, version,
        actor=_actor(bool((payload or {}).get("operator"))),
        require_execution=get_bool("HARNESS_SKILL_EXEC_EVAL"),
        execution_invoker=service.build_execution_eval_invoker(
            (payload or {}).get("modelSettings")
        ),
    )
    return {"success": result.get("ok", False), **result}


@app.post("/skills/{skill_id}/patch/{version}/activate")
def activate_skill_version(skill_id: str, version: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    from .skills.versioning import activate_version  # noqa: PLC0415

    result = activate_version(
        service.skill_registry, skill_id, version,
        actor=_actor(bool(payload.get("operator"))), canary=bool(payload.get("canary")),
        reason=str(payload.get("reason") or ""),
    )
    return {"success": result.get("ok", False), **result}


@app.post("/skills/{skill_id}/patch/{version}/promote")
def promote_skill_canary(skill_id: str, version: str, payload: Optional[dict[str, Any]] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    from .skills.versioning import promote_canary  # noqa: PLC0415

    result = promote_canary(service.skill_registry, skill_id, version, actor=_actor(bool((payload or {}).get("operator"))))
    return {"success": result.get("ok", False), **result}


@app.post("/skills/{skill_id}/patch/{version}/auto-rollback-check")
def skill_rollback_check(skill_id: str, version: str, payload: Optional[dict[str, Any]] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    from .skills.versioning import check_rollback_triggers  # noqa: PLC0415

    body = payload or {}
    result = check_rollback_triggers(
        service.skill_registry, skill_id, version,
        actor=_actor(bool(body.get("operator"))), user_corrections=int(body.get("userCorrections") or 0),
    )
    return {"success": result.get("ok", False), **result}


@app.get("/learning/journey")
def learning_journey(
    scope: Optional[str] = None,
    scopeId: Optional[str] = None,  # noqa: N803
    kind: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
) -> dict[str, Any]:
    """The learning journey / provenance graph â€” DERIVED, read-only.

    Nodes and edges are projected from canonical rows; nothing is written and no
    secrets or raw sensitive content are exposed. `scope`/`scopeId` filter by
    scope isolation; `kind`/`status`/`since` are user filters.
    """
    from .learning_graph import build_learning_journey  # noqa: PLC0415

    kinds = [k for k in (kind or "").split(",") if k] or None
    statuses = [s for s in (status or "").split(",") if s] or None
    return {
        "success": True,
        **build_learning_journey(
            service.memory_store,
            scope_id=scopeId,
            kinds=kinds,
            statuses=statuses,
            since=since,
        ),
    }


@app.post("/learning/from-source")
def learn_from_source_endpoint(
    payload: Optional[dict[str, Any]] = None,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Guided `/learn`: turn a run/document/repo/repeated-workflow/correction into
    a complete but INERT skill draft. Operator-gated; nothing is activated."""
    from .skills.learn import LearnRequest, learn_from_source  # noqa: PLC0415

    payload = payload or {}
    if not bool(payload.get("operator")):
        raise HTTPException(status_code=403, detail="skill.create requires an authorized operator")
    try:
        request = LearnRequest(
            source_kind=str(payload.get("sourceKind") or "run"),
            title=str(payload.get("title") or ""),
            objective=str(payload.get("objective") or ""),
            procedure=[str(s) for s in (payload.get("procedure") or [])],
            content=str(payload.get("content") or ""),
            source_run_id=payload.get("sourceRunId"),
            tools=[str(t) for t in (payload.get("tools") or [])],
            category=str(payload.get("category") or "research"),
            scope_note=str(payload.get("scopeNote") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = learn_from_source(
        request, service.skill_store,
        available_tools=list(service.registry.names()),
        actor_is_operator=True,
    )
    return {"success": result.get("ok", False), **result}


@app.get("/learning/candidates")
def list_candidates(status: Optional[str] = None) -> dict[str, Any]:
    return {"success": True, "candidates": service.skill_store.list_candidates(status, 100)}


@app.get("/learning/candidates/{candidate_id}")
def get_candidate(candidate_id: str) -> dict[str, Any]:
    candidate = service.skill_store.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {"success": True, "candidate": candidate}


@app.post("/learning/from-run/{run_id}")
def learn_from_run(run_id: str, payload: Optional[dict[str, Any]] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    payload = payload or {}
    result = capture_from_run(
        run_id, service.store, service.skill_store,
        actor_is_operator=bool(payload.get("operator")),
        user_intent=str(payload.get("userIntent") or ""),
    )
    return {"success": result.get("ok", False), **result}


@app.get("/learning/eligibility/{run_id}")
def learning_eligibility(run_id: str) -> dict[str, Any]:
    """Why a run is (or is not) a learning candidate â€” read-only, no creation.

    Surfaces the same deterministic eligibility logic the runtime records on the
    run, so the chat UI can show the reason and link to the Learning Queue.
    """
    from .graph import learning_eligibility_for_run

    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"success": True, **learning_eligibility_for_run(run_id, service.store)}


@app.post("/learning/candidates/{candidate_id}/generate-draft")
def generate_draft(candidate_id: str, payload: Optional[dict[str, Any]] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    payload = payload or {}
    if not bool(payload.get("operator")):
        raise HTTPException(status_code=403, detail="skill.create requires an authorized operator")
    result = propose_draft(
        candidate_id, service.skill_store, available_tools=service.registry.names(),
        user_intent=payload.get("userIntent"),
    )
    return {"success": result.get("ok", False), **result}


@app.post("/learning/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: str, payload: Optional[dict[str, Any]] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    payload = payload or {}
    if not bool(payload.get("operator")):
        raise HTTPException(status_code=403, detail="skill.review requires an authorized operator")
    candidate = service.skill_store.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    service.skill_store.update_candidate(candidate_id, status="rejected", reviewedAt=now_iso())
    return {"success": True, "candidateId": candidate_id, "status": "rejected"}


@app.post("/skill-drafts/{skill_id}/submit-review")
def submit_draft_review(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    result = request_promotion(
        service.skill_registry, skill_id=skill_id, version=payload.get("version"),
        actor=_actor(bool(payload.get("operator"))), reason=str(payload.get("reason") or "submit for review"),
        evaluation_id=payload.get("evaluationId"),
    )
    return {"success": result.get("ok", False), **result}


# â”€â”€ Skill quality / usefulness evidence (Phase 7) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Measured from real execution records. Advisory only; promotion stays
# evaluation- and operator-gated.


@app.get("/skills/{skill_id}/quality")
def skill_quality(skill_id: str, version: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
    from .eclipse_memory.skill_quality import skill_quality_metrics

    return {"success": True, **skill_quality_metrics(service.skill_store, skill_id, version=version, limit=limit)}


@app.get("/skills/{skill_id}/promotion-evidence")
def skill_promotion_evidence(skill_id: str, version: str) -> dict[str, Any]:
    from .eclipse_memory.skill_quality import promotion_evidence

    return {"success": True, **promotion_evidence(service.skill_store, skill_id, version)}


@app.get("/skills/{skill_id}/quality/compare")
def skill_quality_compare(skill_id: str, older: str, newer: str) -> dict[str, Any]:
    from .eclipse_memory.skill_quality import compare_versions

    return {"success": True, **compare_versions(service.skill_store, skill_id, older, newer)}


@app.get("/skills/{skill_id}/quality-history")
def skill_quality_history(skill_id: str, limit: int = 20) -> dict[str, Any]:
    """Per-version quality rows (trend view). Derived, read-only."""
    from .eclipse_memory.skill_quality import version_quality_history

    return {"success": True, "history": version_quality_history(service.skill_store, skill_id, limit=limit)}


@app.post("/skill-drafts/{skill_id}/approve")
def approve_draft(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    request_id = str(payload.get("requestId") or "")
    if not request_id:
        # Resolve the newest pending request for this skill (the UI holds only
        # the skill id after submit-review).
        pending = [
            r for r in service.skill_store.list_promotion_requests(skill_id, 50)
            if r["status"] == "pending"
        ]
        if not pending:
            raise HTTPException(status_code=409, detail="no pending promotion request for this skill")
        request_id = pending[0]["id"]
    result = approve_promotion(
        service.skill_registry, request_id=request_id,
        actor=_actor(bool(payload.get("operator"))), notes=str(payload.get("notes") or ""),
    )
    return {"success": result.get("ok", False), **result}


@app.post("/skill-drafts/{skill_id}/reject")
def reject_draft(skill_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    request_id = str(payload.get("requestId") or "")
    if not request_id:
        pending = [
            r for r in service.skill_store.list_promotion_requests(skill_id, 50)
            if r["status"] == "pending"
        ]
        if pending:
            request_id = pending[0]["id"]
    if request_id:
        result = reject_promotion(
            service.skill_registry, request_id=request_id,
            actor=_actor(bool(payload.get("operator"))), reason=str(payload.get("reason") or "rejected"),
        )
        return {"success": result.get("ok", False), **result}
    # No promotion request yet: reject the draft outright.
    if not bool(payload.get("operator")):
        raise HTTPException(status_code=403, detail="skill.review requires an authorized operator")
    manifest = service.skill_registry.manifest(skill_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    target = service.skill_registry.resolve_version(skill_id) or manifest.version
    service.skill_registry.store.set_version_status(skill_id, target, "rejected")
    service.skill_registry.store.save_review(
        skill_id=skill_id, version=target, actor="operator", decision="rejected",
        notes=str(payload.get("reason") or "rejected"),
    )
    return {"success": True, "skillId": skill_id, "version": target, "status": "rejected"}


def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    import os
    import uvicorn
    # Deployment guard: beyond loopback this is a network service and requires the
    # operator secret, plus HARNESS_AUTH_SECRET in multi-tenant mode.
    if host not in _LOOPBACK_HOSTS and os.environ.get("BLACK_ECLIPSE_ALLOW_INSECURE_BIND") != "1":
        from .security.identity import auth_secret, multi_tenant_required

        missing = [n for n, ok in (("CRON_SECRET", _operator_secret_configured()),
                   ("HARNESS_AUTH_SECRET", (not multi_tenant_required()) or bool(auth_secret()))) if not ok]
        if missing:
            raise SystemExit(f"refusing to bind {host!r}: missing {', '.join(missing)}. "
                             "Set the secrets and front it with authentication, or set "
                             "BLACK_ECLIPSE_ALLOW_INSECURE_BIND=1 to acknowledge the risk.")
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()
