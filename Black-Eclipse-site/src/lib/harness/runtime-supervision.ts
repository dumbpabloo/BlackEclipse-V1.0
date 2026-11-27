// ─── Runtime supervision policy (pure, no I/O) ───────────────────────────────
// The front door's decision to REPLACE a Python runtime is dangerous: a restart
// orphans every run in flight — including one paused on an operator-approval
// card, whose change is then lost. The policy therefore lives here, separated
// from the spawn/netstat plumbing, so it is exhaustively unit-testable.

/** The subset of the runtime's `/health` payload the supervisor reasons about. */
export interface RuntimeHealth {
  ok?: boolean;
  service?: string;
  startedAtMs?: number;
  /** OS pid of the runtime process (advisory; distinguishes two processes). */
  pid?: number;
  /** Agent runs in flight in that process. 0/absent = idle. */
  activeRuns?: number;
  /**
   * Runs parked on a human decision (approval / clarification). They are not
   * "active" in the task registry but a restart would destroy them just the same,
   * so they protect the process too.
   */
  parkedRuns?: number;
}

/**
 * How long after a confirmed-healthy probe we still treat a runtime as "busy,
 * not hung". Killing a runtime that answered moments ago orphans its in-flight
 * runs, so a transient probe miss inside this window never triggers a kill.
 */
export const BUSY_GRACE_MS = 30_000;

/**
 * Default budget for a COLD Python runtime start: how long to wait for
 * `/health` after spawning uvicorn before declaring the runtime unavailable.
 *
 * The old hard-coded 30s was enough only on an idle machine. This app's
 * interpreter imports the whole agent stack (LangChain/LangGraph, browser,
 * terminal, quant, ML, …) and, on a memory-constrained host under load, that
 * cold start can take well over a minute (measured 36 s standalone and 51 s
 * while the rest of the app was busy; observed >3 minutes on the reference
 * Windows host under a concurrent build/test load, which is where the old
 * 240 s budget returned a spurious 503 while the process was still importing).
 * A short budget also made the front door kill a still-starting process and
 * respawn it forever. The default therefore survives the measured worst case
 * with margin; `HARNESS_PY_START_TIMEOUT_MS` overrides it.
 */
export const DEFAULT_START_TIMEOUT_MS = 300_000;

/**
 * Resolve the cold-start budget from an env value. Non-positive / unparseable
 * values fall back to the default so a typo can never disable the wait.
 */
export function resolveStartTimeoutMs(raw: string | undefined): number {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_START_TIMEOUT_MS;
}

/**
 * How long ONE request may wait on a cold start that is already running before
 * it gets an honest "still starting" answer instead of spinning.
 *
 * The budget above belongs to the BACKGROUND spawn and is deliberately long —
 * five minutes, because this stack imports langchain/browser/quant/ML and a
 * constrained host can genuinely need minutes. Letting a *page request* inherit
 * that budget is what makes a stuck runtime look like a broken app: the page
 * waits silently for five minutes with no reason shown. Past this window the
 * request fails fast with a 503 and the spawn keeps going, so the next attempt
 * succeeds without a restart. `HARNESS_PY_REQUEST_WAIT_MS` overrides it.
 *
 * 45 s was still a multi-minute boot in disguise: a browser allows SIX
 * concurrent HTTP/1.1 connections per origin, so six boot requests parked on
 * this timeout held every socket and every later request (including the chat
 * transcript, which does not even call Python) queued behind them — measured
 * 45–55 s on a cold start. Three seconds is enough to ride out a normal warm
 * runtime and short enough that a cold one frees the sockets immediately; the
 * UI shows its honest "runtime starting" state and the next poll succeeds.
 */
export const DEFAULT_REQUEST_WAIT_MS = 3_000;

export function resolveRequestWaitMs(raw: string | undefined): number {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_REQUEST_WAIT_MS;
}

/**
 * Whether a healthy-looking runtime should be REPLACED because it serves code
 * older than the source on disk (a restart is how a `python/harness/**` edit
 * takes effect).
 *
 * It refuses to replace a runtime that is actively serving runs
 * (`activeRuns > 0`): a restart would orphan those runs — including one paused
 * on an operator-approval card — and their changes would be lost. The restart
 * is deferred to a later probe, when the runtime is idle.
 */
export function shouldReplaceStaleRuntime(
  health: RuntimeHealth | null,
  sourceMtimeMs: number,
): boolean {
  if (!health || health.service !== "harness-runtime") return false;
  const startedAt = health.startedAtMs ?? 0;
  if (!(sourceMtimeMs > 0) || !(startedAt > 0) || !(startedAt < sourceMtimeMs)) return false;
  if (hasRunsToProtect(health)) return false;
  return true;
}

/**
 * A runtime that answered within the busy-grace window is treated as BUSY, not
 * hung. Used before clearing a silent-but-listening port: a transient probe miss
 * must not cost the runtime its in-flight runs.
 */
export function isRecentlyHealthy(lastHealthyAt: number | undefined, now: number): boolean {
  return lastHealthyAt !== undefined && now - lastHealthyAt < BUSY_GRACE_MS;
}

/**
 * How long a listening-but-silent runtime may keep failing the readiness probe
 * before the supervisor clears the port. Deliberately much longer than one agent
 * step: a single synchronous tool or a long model call can stop the event loop
 * for many seconds, and killing the process then orphans EVERY chat in flight
 * (observed 2026-11-26: two live runs were killed while they were mid-stream and
 * were later relabelled `RUN_STALLED` by the watchdog).
 */
export const SILENT_KILL_WINDOW_MS = 120_000;

/** Consecutive probe misses required before a silent listener is cleared. */
export const SILENT_MISSES_BEFORE_KILL = 3;

export interface SilentProbeState {
  /** Consecutive readiness misses since the last successful answer. */
  misses: number;
  /** When the current miss streak began (ms), or undefined before the first miss. */
  firstMissAt?: number;
  /** Last confirmed-healthy probe (ms), or undefined if never seen. */
  lastHealthyAt?: number;
  /** Now, in ms. */
  now: number;
}

/**
 * Whether a silent-but-LISTENING runtime may be killed.
 *
 * The policy is intentionally conservative because the cost is asymmetric: a
 * missed probe costs one failed request (the next probe adopts the runtime),
 * while a wrong kill destroys every in-flight run. A kill therefore requires
 * ALL of:
 *
 *   * at least `SILENT_MISSES_BEFORE_KILL` consecutive misses (one slow moment
 *     is never enough),
 *   * the miss streak lasting `SILENT_KILL_WINDOW_MS` (a long agent step is not
 *     a hang), and
 *   * no confirmed-healthy answer inside that same window.
 *
 * Never returns true before the first miss is recorded.
 */
export function shouldClearSilentRuntime(state: SilentProbeState): boolean {
  const { misses, firstMissAt, lastHealthyAt, now } = state;
  if (misses < SILENT_MISSES_BEFORE_KILL || firstMissAt === undefined) return false;
  if (isRecentlyHealthy(lastHealthyAt, now)) return false;
  return now - firstMissAt >= SILENT_KILL_WINDOW_MS;
}

/**
 * Should a run be treated as parked-on-a-human (approval / clarification) and
 * therefore never replaced, cancelled, or stalled out? The runtime reports
 * `parkedRuns` separately from `activeRuns`; both keep the process alive.
 */
export function hasRunsToProtect(health: RuntimeHealth | null): boolean {
  if (!health) return false;
  return (health.activeRuns ?? 0) > 0 || (health.parkedRuns ?? 0) > 0;
}
