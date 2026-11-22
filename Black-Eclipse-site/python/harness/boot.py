"""The runtime's FastAPI lifespan (boot/shutdown wiring).

Extracted from `main.py` so the route surface and the subsystem start/stop
sequence are separate concerns (and `main.py` stays under its size ratchet). The
sequence is unchanged: each subsystem starts once on the first lifespan entry and
stops on the last exit, and every step is best-effort — a failure is a diagnostic,
never a boot blocker.

The service is process-wide, but FastAPI may enter/exit the lifespan more than
once (nested test clients, reload). Reference-count so only the LAST exit tears the
subsystems down — an inner lifespan must never disconnect the servers a sibling
still uses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from .deps import deployment_guard, service

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger("harness")

_lifespan_refs = 0


@asynccontextmanager
async def lifespan(_app: "FastAPI") -> AsyncIterator[None]:
    global _lifespan_refs
    _lifespan_refs += 1
    if _lifespan_refs == 1:
        await _boot()
    try:
        yield
    finally:
        _lifespan_refs -= 1
        if _lifespan_refs == 0:
            await _shutdown()


def _warm_code_index(service: Any) -> None:
    """Open the default workspace's code index OFF the boot/event path.

    Opening a large index is seconds (hundreds of MB here); doing it lazily
    inside a run put that cost in the run's pre-model phase, on the event loop.
    The initial SCAN stays lazy: `warm` opens the state without indexing it.
    Best-effort; disabled with `BLACK_ECLIPSE_CODE_INTEL_WARM=0`.
    """
    if os.environ.get("BLACK_ECLIPSE_CODE_INTEL_WARM", "1") == "0":
        return
    runtime = getattr(service, "code_intel", None)
    if runtime is None:
        return

    def _run() -> None:
        try:
            from .code.workspace import resolve_workspace  # noqa: PLC0415

            workspace = resolve_workspace(None)
            if workspace is not None:
                runtime.warm(workspace)
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            log.warning("CIUE index warm-up failed", exc_info=True)

    threading.Thread(target=_run, name="eclipse-intel-warm", daemon=True).start()


async def _boot() -> None:
    """Start every subsystem exactly once. Never a boot blocker."""
    _t = time.monotonic()

    def _mark(step: str) -> None:
        """Log the elapsed time of one boot step (operator diagnostics)."""
        nonlocal _t
        now = time.monotonic()
        log.info("[harness-py] boot %-22s %6d ms", step, int((now - _t) * 1000))
        _t = now

    # W0: strict flag validation is opt-in. When enabled, a typo in a known
    # feature flag fails boot instead of silently running the default path.
    from .config.flags import assert_valid  # noqa: PLC0415

    assert_valid()

    # Deployment guard (end-to-end): the Python bind AND the front door's auth
    # posture, so a public bind that is reachable without a login — or a required
    # login that cannot work — is named here, not discovered later. A public bind
    # behind an authenticated front door is a legitimate single-operator
    # deployment (docs/DEPLOYMENT.md): a notice, not a warning. Never blocks boot
    # (the local dev bind is the normal case).
    guard = deployment_guard()
    log.info(
        "[harness-py] deployment: bind=%s frontDoorAuth=%s(%s) multiUserSafe=%s",
        guard["host"] or "default",
        guard["frontDoorAuth"]["required"],
        guard["frontDoorAuth"]["source"],
        guard["multiUserSafe"],
    )
    for notice in guard["notices"]:
        log.info("[harness-py] DEPLOYMENT NOTICE: %s", notice)
    for warning in guard["warnings"]:
        log.warning("[harness-py] DEPLOYMENT WARNING: %s", warning)
    # Retention: mark due AUTOMATIC memories expired on startup. Idempotent and
    # best-effort.
    service.run_startup_maintenance()
    _mark("startup-maintenance")
    # Native video: prune old job workspaces to their count/byte budgets. The
    # final MP4 is copied into the Artifact Center at registration, so a job
    # workspace is an audit aid — never the deliverable. Bounded, off the event
    # loop, and best-effort (a locked file is retried next boot).
    try:
        from .video.gc import prune_video_workspace  # noqa: PLC0415

        pruned = await asyncio.to_thread(prune_video_workspace)
        if pruned.get("removed"):
            log.info(
                "[harness-py] video workspace pruned: %s job(s), %.1f MB freed",
                len(pruned["removed"]), pruned.get("freedBytes", 0) / 1_048_576,
            )
    except Exception:  # noqa: BLE001 - hygiene must never block boot
        log.warning("video workspace prune failed", exc_info=True)
    # Failure Intelligence & Adaptive Recovery: construct the classifier/controller
    # and register its read-only inspection tools. Additive; capture itself is
    # wired after the Event System starts.
    from .recovery.bootstrap import install_recovery_layer

    install_recovery_layer(service)
    _mark("recovery-install")
    # Terminal subsystem recovery: reconcile persisted process/session rows.
    service.start_terminal()
    # Application Runtime reconciliation: re-examine the runtimes left by a
    # previous process.
    service.reconcile_application_runtime()
    _mark("terminal+runtime-reconcile")
    # Code Intelligence & Understanding Engine (CIUE): warm the code index in the
    # background. Language servers start lazily on first use.
    service.start_code_intel()
    _warm_code_index(service)
    _mark("code-intel")
    # Browser + Computer-Use runtimes: wire resolvers, start idle sweepers. No
    # browser/desktop is launched until a tool asks.
    service.start_browser()
    _mark("browser")
    await service.start_browser_sweeper()
    _mark("browser-sweeper")
    service.start_computer()
    _mark("computer")
    await service.start_computer_sweeper()
    _mark("computer-sweeper")
    # Connect configured MCP servers and register selected read-only tools.
    await service.start_mcp()
    _mark("mcp")
    # The bounded learning-review queue: reviews completed runs in the background.
    # It only creates inert records/suggestions and never enables a skill.
    await service.start_learning_review()
    _mark("learning-review")
    # RSL (R0–R4): one bounded conductor task beside learning review. It is dark
    # by default (`HARNESS_RSL_MODE=off`); observe mode is a no-op here because
    # the post-run observer owns observation.
    from .self_improvement.loop import RSLLoop
    service._rsl_loop = RSLLoop(service)
    await service._rsl_loop.start()
    # Drift watch: read-only cadence over every configured scope. It publishes a
    # transition into drift and never creates a candidate or mutates a surface.
    # Disabled with HARNESS_DRIFT_WATCH_SECONDS=0.
    from .self_improvement.drift_watch import DriftWatcher  # noqa: PLC0415

    service._drift_watch = DriftWatcher(service)
    await service._drift_watch.start()
    # Graph health watch: the bounded telemetry surface is read on a cadence and
    # a status TRANSITION publishes graph.health_degraded/recovered. It never
    # touches a run. Disabled with HARNESS_GRAPH_HEALTH_SECONDS=0.
    if getattr(service, "graphs", None) is not None:
        from .graph_runtime.health_watch import GraphHealthWatcher  # noqa: PLC0415

        service._graph_health_watch = GraphHealthWatcher(service)
        await service._graph_health_watch.start()
    # The unattended RSL session: on a cadence, ONE measured surface session
    # stages its winner as an inert candidate. Off by default
    # (HARNESS_RSL_SESSION_SECONDS=0); it never lands anything.
    from .self_improvement.session_watch import SessionWatcher  # noqa: PLC0415

    service._rsl_session_watch = SessionWatcher(service)
    await service._rsl_session_watch.start()
    # Native Automation Engine (ADR-H152): one durable scheduler/trigger router
    # beside the three TS refresh loops, which stay exactly as they are.
    await service.start_automation()
    # Broker reconciliation: an OPT-IN loop (TRADING_RECONCILE_SECONDS, default 0)
    # that syncs fills/positions/order state from the broker. It never places an
    # order and never fabricates a fill.
    brokers = getattr(service, "brokers", None)
    if brokers is not None:
        try:
            await brokers.start_reconciler()
        except Exception:  # noqa: BLE001 - never block boot
            log.warning("broker reconciler did not start", exc_info=True)
        # Portfolio supervisor: OPT-IN (TRADING_SUPERVISOR_SECONDS, default 0).
        # Each pass records the day/week equity baseline and pauses bots that
        # have lost the right to keep trading. It closes nothing.
        try:
            from .trade.supervisor_api import start_supervisor_loop

            await start_supervisor_loop(brokers)
        except Exception:  # noqa: BLE001 - never block boot
            log.warning("trading supervisor did not start", exc_info=True)
    # Advisory outcome scoring: a bounded interval that records how past
    # recommendations turned out. It never issues advice and never touches an
    # approval gate. Disabled with HARNESS_ADVISORY_AUTO_EVALUATE=0.
    await service.start_advisory_cadence()
    # Brazilian official datasets: OFF unless BE_BR_SYNC=1. When on, an hourly
    # pass syncs whatever is due (COTAHIST / CVM / BCB series / Tesouro) through
    # the same `sync_dataset` the manual path uses. Never runs at boot time.
    try:
        from .br_data.schedule import start_br_sync

        start_br_sync(service)
    except Exception:  # noqa: BLE001 - never block boot
        log.warning("brazilian dataset sync did not start", exc_info=True)
    # Class-authorized self-modification: auto-accept contract-clean candidates
    # and auto-enable only read-only learned skills (TTL + cap + audit). Effectful
    # and financial actions remain operator-only.
    await service.start_self_modification()
    # The gallery: a local catalogue of published strategies. Registered here so
    # the agent and the UI share ONE catalogue.
    from .gallery.bootstrap import install_gallery_layer

    install_gallery_layer(service)
    # Project auto-run: a project may act on its own, but only where its policy
    # says so (off by default) and only inside that policy's budget. It runs
    # COMPUTE tools only; it can never act on the world by itself. The loop and
    # its policy live in `python/harness/projects/`.
    from .projects.auto import start_auto_loop

    start_auto_loop(service)
    # Event System: the durable event store + bus. Started AFTER the subsystems
    # that publish, so their legacy run-bus emissions are ingested from boot.
    from .event_system.bootstrap import start_events
    await start_events(service)
    _mark("events")
    # Failure Intelligence & Adaptive Recovery: subscribe to the now-live event
    # bus so failures from every subsystem are classified and recorded as durable
    # incidents. Bounded by the Event System's loop guard + the recovery budget.
    from .recovery.bootstrap import start_recovery

    await start_recovery(service)
    _mark("recovery-start")
    # Run stall watchdog: even if some strand of the runtime hangs past its own
    # per-call hard deadlines (a swallowed cancellation, an unforeseen subsystem
    # strand), every non-terminal run still reaches a terminal state and its
    # stream closes. Best-effort, bounded interval, no new daemon beyond one task.
    from .run_watchdog import start_run_watchdog

    await start_run_watchdog(service)
    # The Task / Plan runtime: one durable scheduler task that claims runnable
    # tasks and dispatches each through the ordinary run path. Started after jobs
    # so a task-triggering job sees a live scheduler.
    await service.start_tasks()
    _mark("tasks")
    # The Artifact Center: a bounded background reconciler that repairs catalog/
    # storage inconsistencies (missing objects, recovered rows). Best-effort.
    await service.start_artifacts()
    # The Plugin Platform: reload every plugin enabled before the last shutdown.
    # Durable state means a restart restores the same active plugin set; a plugin
    # that fails to reload is quarantined as FAILED, never silently dropped.
    from .plugins.bootstrap import start_plugins

    await start_plugins(service)
    # Phase 2 composition: mount the active profile's ordered rows through the
    # kernel supervisor. No profile / kernel off is a no-op; a failed row is
    # restored and reported without stopping the app.
    from .kernel.profile_boot import start_profile
    service._kernel_profile_status = await start_profile(service)
    # Parallel execution & DAG orchestration: register the tools and recover any
    # execution left mid-flight by a previous process. Best-effort, never a boot
    # blocker.
    from .orchestration.bootstrap import install_orchestration_layer, recover_orchestration

    install_orchestration_layer(service)
    await recover_orchestration(service)
    # Agent-to-Agent: the versioned Agent Registry + durable Message Bus + budgets,
    # and the A2A tools. Delegated tasks are executed by the TaskScheduler above.
    from .a2a.bootstrap import install_a2a_layer, recover_a2a

    install_a2a_layer(service)
    recover_a2a(service)
    # Bot Team Orchestration: composes the A2A + Task layers above into team runs
    # (delegation, dependency-aware concurrency, conflict reconciliation, review).
    from .team.bootstrap import install_team_layer

    install_team_layer(service)
    # Institution multi-agent preset: registers the committee's role presets, its
    # A2A definitions and the `read_run_output` handoff tool on the SAME registry.
    # A composition over the team layer above, never a second runtime.
    from .institution.bootstrap import install_institution_layer

    install_institution_layer(service)
    # Execution Fabric (ADR-H156): the durable queue + worker pool + execution-mode
    # router. Installed AFTER the subsystems it workers over (agent runtime,
    # tools, automation, events, plugins) so its event rules can subscribe to a
    # live Event System. It recovers any job left mid-flight by a previous process.
    from .fabric.bootstrap import install_fabric_layer, start_fabric

    install_fabric_layer(service)
    await start_fabric(service)
    _mark("fabric")
    # Unified search: build the vector/FTS index from existing sources in the
    # background (non-blocking). With no embedding provider this is an honest
    # keyword-only index; disabled by HARNESS_SEARCH_WARM_INDEX=0.
    search_layer = getattr(service, "search", None)
    if search_layer is not None:
        try:
            search_layer.schedule_warm_index()
        except Exception:  # noqa: BLE001 - warm-up must never block boot
            log.warning("[harness-py] search warm index skipped", exc_info=True)
    # Observability platform: report readiness once every subsystem is live.
    try:
        from .observability.integration import start_observability

        start_observability(service)
    except Exception:  # noqa: BLE001 - telemetry startup must never block boot
        log.warning("[harness-py] observability startup skipped", exc_info=True)
    # Security subsystem: attach the now-live event system + observability metrics
    # to the audit log (the gateway itself was installed at construction). Last so
    # every sink it reports into already exists.
    try:
        from .security.bootstrap import start_security

        await start_security(service)
    except Exception:  # noqa: BLE001 - security startup must never block boot
        log.warning("[harness-py] security startup skipped", exc_info=True)
    # Channel Gateway: start the enabled local long-poll providers (Telegram). No
    # public endpoint is required for these, so zero-config local use is unchanged.
    try:
        from .channels.bootstrap import start_channels

        await start_channels(service)
    except Exception:  # noqa: BLE001 - channels must never block boot
        log.warning("[harness-py] channel gateway startup skipped", exc_info=True)


async def _shutdown() -> None:
    """Stop every subsystem on the last exit. Cancel queued work and terminate
    subprocesses cleanly so none leak across restarts. Never raises."""
    # Stop channel pollers FIRST — they are pure inbound and must not fire a
    # dispatch into a half-torn-down runtime.
    from .channels.bootstrap import stop_channels

    await stop_channels(service)
    # The project auto-run loop: stop it before the stores it writes to go away.
    from .projects.auto import stop_auto_loop

    await stop_auto_loop(service)
    from .plugins.bootstrap import stop_plugins

    # Unmount profile rows before the plugins/services they may depend on.
    from .kernel.profile_boot import stop_profile
    await stop_profile(service)
    await stop_plugins(service)
    # Flush + close telemetry sinks FIRST so terminal-job/plugin events reach a
    # live exporter before the bus is torn down.
    from .security.bootstrap import stop_security

    await stop_security(service)
    from .observability.integration import stop_observability

    stop_observability(service)
    # Drain the Execution Fabric BEFORE the event system so its terminal
    # publications still reach a live bus, releasing locks and cancelling work.
    from .fabric.bootstrap import stop_fabric

    await stop_fabric(service)
    # Detach the recovery subscription before the event bus is torn down.
    from .recovery.bootstrap import stop_recovery

    await stop_recovery(service)
    # Stop the stall watchdog before the bus it closes streams on.
    from .run_watchdog import stop_run_watchdog

    await stop_run_watchdog()
    from .event_system.bootstrap import stop_events
    await service.stop_artifacts()
    await service.stop_tasks()
    drift_watch = getattr(service, "_drift_watch", None)
    if drift_watch is not None:
        await drift_watch.stop()
    graph_health_watch = getattr(service, "_graph_health_watch", None)
    if graph_health_watch is not None:
        await graph_health_watch.stop()
    session_watch = getattr(service, "_rsl_session_watch", None)
    if session_watch is not None:
        await session_watch.stop()
    await stop_events(service)
    await service.stop_advisory_cadence()
    await service.stop_self_modification()
    await service.stop_automation()
    brokers = getattr(service, "brokers", None)
    if brokers is not None:
        try:
            await brokers.stop_reconciler()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            log.warning("broker reconciler shutdown failed", exc_info=True)
    orchestration = getattr(service, "orchestration", None)
    if orchestration is not None:
        await orchestration.primary.shutdown()
    rsl_loop = getattr(service, "_rsl_loop", None)
    if rsl_loop is not None:
        await rsl_loop.stop()
    await service.stop_learning_review()
    await service.stop_code_intel()
    await service.stop_browser()
    await service.stop_computer()
    await service.stop_mcp()
    service.stop_application_runtime()
    service.stop_execution()
    # Stop the warm analysis kernels: close each worker's process tree and
    # remove its staging directory before the interpreter exits. Off-loop because
    # close waits for a killed process to be reaped. Best-effort.
    try:
        from .code.kernel import reset_kernel_pool  # noqa: PLC0415

        await asyncio.to_thread(reset_kernel_pool)
    except Exception:  # noqa: BLE001 - shutdown must never raise
        log.warning("analysis kernel shutdown failed", exc_info=True)
    await service.stop_terminal()
    # Release SQLite connections LAST, once every subsystem is idle. The pool
    # reconnects lazily, so a later lifespan entry still works.
    try:
        service.store.close()
    except Exception:  # noqa: BLE001 - shutdown must never raise
        log.warning("agent-memory store close failed", exc_info=True)


__all__ = ["lifespan"]
