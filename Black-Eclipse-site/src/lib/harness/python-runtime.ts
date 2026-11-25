// ─── Python runtime launcher ─────────────────────────────────────────────────
// The agentic runtime is the Python service in `python/harness/`. The Next.js
// process spawns it on first use (lazily, once) and talks to it over HTTP.
//
// Why spawn rather than require an operator to run it:
//   • keep the app a single `npm run dev` process tree;
//   • the interpreter is the same Python the repo already provisions for the
//     yfinance bridge (vendored embedded Python on Windows);
//   • readiness is health-checked, and a failure is reported honestly — never
//     silently degraded into a fake agent.

import { spawn, execFile, execSync, type ChildProcess } from "child_process";
import { promisify } from "util";
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  statSync,
  unlinkSync,
  writeSync,
} from "fs";
import path from "path";
import { findProjectRoot, findPython } from "@/lib/scraper/fetchers/python";
import { resolveLLMProviderId } from "@/lib/agent/presets";
import { runtimeCallbackToken } from "./runtime-auth";
import {
  PYTHON_CHART_PACKAGES,
  PYTHON_RUNTIME_DEPS,
  chartEngineReadiness,
  chartEngineReadinessFromHealth,
  type ChartEngineReadiness,
} from "./runtime-config";
import { isRecentlyHealthy, resolveRequestWaitMs, resolveStartTimeoutMs, shouldReplaceStaleRuntime } from "./runtime-supervision";

// The URL is authoritative. When an operator overrides HARNESS_PY_URL, the
// spawned uvicorn must bind the SAME host/port — otherwise health checks would
// pass on one port while the server listens on another.
function parseUrl(raw: string): { host: string; port: number } | null {
  try {
    const url = new URL(raw);
    return { host: url.hostname, port: Number(url.port || "80") };
  } catch {
    return null;
  }
}

export const HARNESS_PY_URL =
  process.env.HARNESS_PY_URL ?? `http://${process.env.HARNESS_PY_HOST ?? "127.0.0.1"}:${process.env.HARNESS_PY_PORT ?? 8765}`;
const parsed = parseUrl(HARNESS_PY_URL);
export const HARNESS_PY_HOST = parsed?.host ?? "127.0.0.1";
export const HARNESS_PY_PORT = parsed?.port ?? 8765;

interface RuntimeGlobal {
  __harnessPyProc?: ChildProcess;
  __harnessPyStart?: Promise<boolean>;
  /** Last successful health verification (ms). Short-TTL cache so a burst of
   * requests (one page load fans out several API routes) shares a single probe
   * instead of each doing its own round-trip + directory scan. */
  __harnessPyHealthyAt?: number;
  /** In-flight ensure() so concurrent callers coalesce onto one probe. */
  __harnessPyEnsure?: Promise<boolean>;
  /** Last chart-engine capability probe. Short TTL: a pip install must become
   * visible without a restart, but drawing several charts must not spawn an
   * interpreter each time. */
  __chartEngineProbe?: { value: ChartEngineReadiness; at: number };
}

const g = globalThis as unknown as RuntimeGlobal;

const execFileAsync = promisify(execFile);

// A chart render must not spawn an interpreter every time, but a `pip install`
// must become visible without restarting the app. 60 s is the compromise.
const CHART_PROBE_TTL_MS = 60_000;

/** The runtime's own drawing capability (`GET /charts/health`), or null when
 * it does not answer. This is the process that will draw the chart, so its
 * answer is authoritative; a separate interpreter probe is only a fallback. */
async function runtimeChartHealth(): Promise<ChartEngineReadiness | null> {
  try {
    const token = runtimeCallbackToken();
    const res = await fetch(`${HARNESS_PY_URL}/charts/health`, {
      headers: token ? { "X-Harness-Operator": token } : {},
      signal: AbortSignal.timeout(6_000),
      cache: "no-store",
    });
    if (!res.ok) return null;
    const body = (await res.json()) as Record<string, unknown>;
    return chartEngineReadinessFromHealth(body);
  } catch {
    return null;
  }
}

/**
 * Can the chart engine actually DRAW?
 *
 * `ensurePythonRuntime()` only proves the agent stack can boot. It says nothing
 * about matplotlib/plotly, which are imported lazily at render time — so a
 * machine could (and did) report a healthy runtime while every chart 503'd.
 *
 * The runtime IT asked is the only answer that matters: the process that draws
 * the chart is the one whose imports count. `GET /charts/health` reports
 * `sys.executable` and the modules importable IN THAT PROCESS, so we ask it
 * first. Probing a separately-resolved interpreter used to disagree with the
 * running runtime (a stale or platform-mismatched vendored path, or a system
 * Python without the user-site packages) and turned a working chart engine into
 * `CHART_RENDER_UNAVAILABLE` — every chart a 503 while the runtime could draw.
 *
 * The local interpreter probe is the fallback for a runtime that is not up
 * (the caller then knows the capability even before it starts), and it is
 * cached with the same short TTL so a `pip install` becomes visible.
 */
export async function probeChartEngine(): Promise<ChartEngineReadiness> {
  const cached = g.__chartEngineProbe;
  if (cached && Date.now() - cached.at < CHART_PROBE_TTL_MS) return cached.value;

  const fromRuntime = await runtimeChartHealth();
  if (fromRuntime) {
    g.__chartEngineProbe = { value: fromRuntime, at: Date.now() };
    return fromRuntime;
  }

  const py = await findPython();
  if (!py) {
    const unavailable = chartEngineReadiness(null, []);
    g.__chartEngineProbe = { value: unavailable, at: Date.now() };
    return unavailable;
  }

  // `find_spec` is a cheap existence check (no heavy imports); `sys.executable`
  // rides along so an operator can see WHICH python was probed.
  const script = [
    "import importlib.util, json, sys",
    `mods = ${JSON.stringify([...PYTHON_CHART_PACKAGES])}`,
    "print(json.dumps({",
    '  "python": sys.executable,',
    '  "available": [m for m in mods if importlib.util.find_spec(m) is not None],',
    "}))",
  ].join("\n");

  try {
    const { stdout } = await execFileAsync(py.cmd, ["-c", script], {
      timeout: 30_000,
      windowsHide: true,
      maxBuffer: 1024 * 1024,
    });
    const parsed = JSON.parse(String(stdout).trim()) as {
      python?: string;
      available?: string[];
    };
    const value = chartEngineReadiness(parsed.python ?? py.cmd, parsed.available ?? []);
    g.__chartEngineProbe = { value, at: Date.now() };
    return value;
  } catch {
    // The probe itself failed (interpreter gone, killed). Report the worst
    // honest case: no interpreter, every chart module unproven.
    const value: ChartEngineReadiness = {
      ready: false,
      python: py.cmd,
      missing: [...PYTHON_CHART_PACKAGES],
    };
    g.__chartEngineProbe = { value, at: Date.now() };
    return value;
  }
}

// A memory/skills tab load fans out several API routes at once; without this,
// each route re-probed `/health` and re-scanned the source directory on every
// call. A short TTL is safe: staleness is still detected within the window, and
// an actually-down runtime re-probes immediately after the TTL.
const HEALTH_TTL_MS = 15_000;

// Readiness uses the CONSTANT-TIME liveness route, never the diagnostic
// `/health`. The diagnostic document probes providers, browser, code-intel and
// a security audit; under load it was measured at 20+ seconds, which made the
// old 10 s probe declare a healthy runtime dead and restart it forever.
// The budget is generous because a starved machine can still be slow to accept.
const LIVENESS_PATH = "/health/live";
const READINESS_PROBE_TIMEOUT_MS = 30_000;

/**
 * Cross-context cold-start lock. Next can evaluate this module in more than one
 * server context, so the in-memory `__harnessPyStart` promise is NOT enough to
 * serialize a spawn. Two cold starts mean two uvicorns importing the whole
 * agent stack, both touching SQLite, and both competing for the CPU — the
 * second one usually cannot bind the already-held port but still burns minutes
 * booting. The lock is a plain file: creating it with `wx` is atomic across
 * processes/threads, and its mtime bounds how long a crashed owner can block a
 * retry.
 */
const RUNTIME_SPAWN_LOCK = path.join(
  process.cwd(),
  ".black-eclipse",
  // One lock PER RUNTIME PORT: dev (8765) and production (8766) cold-start
  // independently, so a shared lock would make one environment wait for the
  // other's boot and then give up.
  `harness-runtime-${HARNESS_PY_PORT}.spawn.lock`
);

/**
 * Is a lock file left by someone else safe to take over?
 *
 * A crashed or force-killed app leaves the file behind with no one to release
 * it, and the old rule — wait out the whole cold-start budget — meant the next
 * launches answered "Python runtime still starting" for five minutes, spawned
 * nothing, and never recovered on their own. Any user who ever force-quit the
 * app (or lost power, or had it killed by antivirus) would hit that.
 *
 * The owner's pid is written into the lock, so a lock whose owner is gone is
 * stale IMMEDIATELY. Locks with no readable owner (written by an older build)
 * fall back to the age rule, and a live owner is always respected.
 */
export function isSpawnLockStale(
  contents: string,
  mtimeMs: number,
  now: number,
  staleAfterMs: number,
): boolean {
  const pid = Number(String(contents).trim());
  if (Number.isInteger(pid) && pid > 0 && !pidLooksAlive(pid)) return true;
  return now - mtimeMs >= staleAfterMs;
}

/** True when the lock file was written by this very process (see acquireSpawnLock). */
export function isSpawnLockMine(contents: string, selfPid: number): boolean {
  const owner = Number(String(contents).trim());
  return Number.isInteger(owner) && owner > 0 && owner === selfPid;
}

/** `process.kill(pid, 0)` probes without signalling: ESRCH means no such process. */
function pidLooksAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "EPERM";
  }
}

/**
 * Take the cold-start lock.
 * @returns a file descriptor (>= 0) when this caller owns the start, `null`
 *          when another context is already starting the runtime, or `-1` when
 *          locking is unavailable (the caller may still spawn; the in-memory
 *          guards remain in force).
 */
function acquireSpawnLock(): number | null {
  try {
    mkdirSync(path.dirname(RUNTIME_SPAWN_LOCK), { recursive: true });
  } catch {
    return -1;
  }
  try {
    const fd = openSync(RUNTIME_SPAWN_LOCK, "wx");
    // Record the owner so a later launch can tell "someone is starting it" from
    // "someone started it and died".
    try {
      writeSync(fd, String(process.pid));
    } catch {
      /* the pid is an optimisation; the age rule still applies */
    }
    return fd;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "EEXIST") return -1;
    try {
      const staleAfterMs = resolveStartTimeoutMs(process.env.HARNESS_PY_START_TIMEOUT_MS) + 15_000;
      let contents = "";
      try {
        contents = readFileSync(RUNTIME_SPAWN_LOCK, "utf8");
      } catch {
        contents = "";
      }
      // A lock owned by THIS process is never a rival. The in-memory start guard
      // already prevents a second spawn from the same context, and this file may
      // be a leftover from an attempt that never reached its release. Waiting on
      // ourselves was a deadlock: the process held the lock, then waited the full
      // start budget for the very start the lock was blocking — a runtime that
      // never came up, on a fresh install, with no crash involved.
      const mine = isSpawnLockMine(contents, process.pid);
      if (!mine && !isSpawnLockStale(contents, statSync(RUNTIME_SPAWN_LOCK).mtimeMs, Date.now(), staleAfterMs)) {
        return null;
      }
      unlinkSync(RUNTIME_SPAWN_LOCK);
    } catch {
      return null;
    }
    try {
      const fd = openSync(RUNTIME_SPAWN_LOCK, "wx");
      try {
        writeSync(fd, String(process.pid));
      } catch {
        /* see above */
      }
      return fd;
    } catch {
      return null;
    }
  }
}

function releaseSpawnLock(fd: number): void {
  try {
    closeSync(fd);
  } catch {
    /* already closed */
  }
  try {
    unlinkSync(RUNTIME_SPAWN_LOCK);
  } catch {
    /* already removed */
  }
}

/** True while a child THIS (or a sibling context's) ensure call is alive. */
function trackedRuntimeAlive(): boolean {
  return Boolean(g.__harnessPyProc && g.__harnessPyProc.exitCode === null);
}

/** Kill the child we track, including any descendants (Windows: /T). */
function killTrackedRuntime(): void {
  const proc = g.__harnessPyProc;
  g.__harnessPyProc = undefined;
  if (!proc || proc.exitCode !== null) return;
  try {
    if (process.platform === "win32" && proc.pid) {
      execSync(`taskkill /F /T /PID ${proc.pid}`, { stdio: "ignore", windowsHide: true });
    } else {
      proc.kill("SIGKILL");
    }
  } catch {
    try {
      proc.kill("SIGKILL");
    } catch {
      /* already gone */
    }
  }
}

interface HealthInfo {
  ok?: boolean;
  service?: string;
  startedAtMs?: number;
  /** OS pid of the runtime process (advisory; distinguishes two processes). */
  pid?: number;
  /** Agent runs in flight in that process. 0/absent = idle. */
  activeRuns?: number;
}

async function getHealth(timeoutMs = 8_000): Promise<HealthInfo | null> {
  try {
    const res = await fetch(`${HARNESS_PY_URL}/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) return null;
    return (await res.json()) as HealthInfo;
  } catch {
    return null;
  }
}

/**
 * Is the runtime reachable WITHOUT spawning it? Used by non-agent checks (the
 * SDK design lock) that must not pay a cold-start cost or start Python merely
 * to look for locks. A runtime that is down cannot hold locks.
 */
export async function pythonRuntimeReachable(timeoutMs = 1_200): Promise<boolean> {
  if (g.__harnessPyHealthyAt && Date.now() - g.__harnessPyHealthyAt < HEALTH_TTL_MS) return true;
  return (await getHealth(timeoutMs)) !== null;
}

/**
 * Liveness for readiness: the cheap `/health/live` route, with a fallback to
 * the full document for an older runtime that predates it. Returns the same
 * shape the supervisor reasons about (`startedAtMs`, `activeRuns`, `pid`).
 */
async function getLiveness(timeoutMs = READINESS_PROBE_TIMEOUT_MS): Promise<HealthInfo | null> {
  try {
    const res = await fetch(`${HARNESS_PY_URL}${LIVENESS_PATH}`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    // An older runtime has no liveness route: fall back to the full document.
    if (res.status === 404) return getHealth(timeoutMs);
    if (!res.ok) return null;
    return (await res.json()) as HealthInfo;
  } catch {
    return null;
  }
}

async function isHealthy(timeoutMs = READINESS_PROBE_TIMEOUT_MS): Promise<boolean> {
  return (await getLiveness(timeoutMs)) !== null;
}

/**
 * Newest mtime of the Python runtime source. A running runtime whose
 * `startedAtMs` predates this is serving stale code — the classic case being an
 * orphaned uvicorn from a previous `npm run dev` (spawned as a child, it
 * survives the Next restart on Windows) that still holds the port. Without this
 * check every harness code change silently does nothing until the port is
 * cleared by hand.
 */
function harnessSourceMtimeMs(): number {
  // Walk the WHOLE harness tree, not just the top level: most of the runtime
  // lives in subpackages (`code/`, `mcp/`, `terminal/`, `skills/`, …). A
  // top-level-only scan missed every edit there, so a running runtime was never
  // detected as stale and kept serving old code after a change.
  try {
    const root = path.join(findProjectRoot(), "python", "harness");
    let newest = 0;
    const walk = (dir: string, depth: number): void => {
      if (depth > 6) return;
      let entries;
      try {
        entries = readdirSync(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        if (entry.isDirectory()) {
          if (entry.name === "__pycache__" || entry.name.startsWith(".")) continue;
          walk(path.join(dir, entry.name), depth + 1);
        } else if (entry.isFile() && entry.name.endsWith(".py")) {
          const m = statSync(path.join(dir, entry.name)).mtimeMs;
          if (m > newest) newest = m;
        }
      }
    };
    walk(root, 0);
    return newest;
  } catch {
    return 0;
  }
}

/**
 * PIDs LISTENING on `port`, parsed from `netstat -ano` output. Pure.
 *
 * The LOCAL address column is matched exactly (`127.0.0.1:8766`,
 * `[::]:8766`, `0.0.0.0:8766`) — matching the bare `:8766` substring also
 * matched a listener on `:18766` and a line whose FOREIGN address was
 * `:8766`, which could get an unrelated process killed.
 */
export function parseListenerPids(netstat: string, port: number): string[] {
  const pids = new Set<string>();
  for (const raw of netstat.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line.includes("LISTENING")) continue;
    const parts = line.split(/\s+/);
    if (parts.length < 4) continue;
    const local = parts[1] ?? "";
    const pid = parts[parts.length - 1];
    if (!local.endsWith(`:${port}`)) continue;
    // `[::1]:8766` / `[::]:8766` / `127.0.0.1:8766` — the port must be the
    // whole last segment, never a suffix of a longer number.
    const portPart = local.slice(local.lastIndexOf(":") + 1);
    if (portPart !== String(port)) continue;
    if (pid && /^\d+$/.test(pid) && pid !== "0") pids.add(pid);
  }
  return [...pids];
}

/** Does this `/health` payload identify OUR detector service? Pure. */
export function isHarnessHealth(health: HealthInfo | null): boolean {
  if (!health) return false;
  if (health.service) {
    return health.service === "eclipse-harness" || health.service === "harness-runtime";
  }
  // Older runtimes reported `{ok:true}` without a service name.
  return typeof health.ok === "boolean";
}

/** Did a uvicorn stderr line report "the port is already taken"? Pure. */
export function isBindConflict(text: string): boolean {
  return /10048|address already in use|only one usage of each socket address/i.test(text);
}

/** PIDs currently LISTENING on the runtime port (empty when none / unavailable). */
function listenerPidsOnPort(): string[] {
  try {
    if (process.platform === "win32") {
      const out = execSync("netstat -ano", { encoding: "utf8", windowsHide: true });
      return parseListenerPids(out, HARNESS_PY_PORT);
    }
    const out = execSync(`lsof -t -i:${HARNESS_PY_PORT}`, { encoding: "utf8" }).trim();
    return out ? out.split("\n") : [];
  } catch {
    // No listener, or netstat/lsof unavailable — nothing to report.
    return [];
  }
}

/** Kill whatever is listening on the runtime port, if it is our stale service. */
function killListenerOnPort(): void {
  for (const pid of listenerPidsOnPort()) {
    try {
      if (process.platform === "win32") {
        execSync(`taskkill /F /PID ${pid}`, { stdio: "ignore", windowsHide: true });
      } else {
        execSync(`kill -9 ${pid}`, { stdio: "ignore" });
      }
    } catch {
      /* already gone */
    }
  }
}

async function waitForHealth(deadlineMs: number): Promise<boolean> {
  const start = Date.now();
  // A cold start imports the whole agent stack; under CPU contention that can
  // be a minute or more. Say so periodically, so a long wait is visible in the
  // log instead of looking like a hang.
  let nextNoticeAt = 15_000;
  while (Date.now() - start < deadlineMs) {
    // A 1.5s probe is right for the fast readiness path, but during a cold
    // start the process can be importing/initializing for minutes and may be
    // slow to answer /health. Aborting that answer counted a perfectly healthy
    // boot as "never came up" and triggered another spawn.
    if (await isHealthy(READINESS_PROBE_TIMEOUT_MS)) return true;
    const waited = Date.now() - start;
    if (waited >= nextNoticeAt) {
      console.log(`[harness] Python runtime still starting (${Math.round(waited / 1000)}s elapsed)…`);
      nextNoticeAt += 15_000;
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

/**
 * Wait for an in-flight start, but never longer than a page should wait.
 *
 * Returns false on timeout WITHOUT cancelling the work: the spawn keeps running
 * and the next request picks up the healthy runtime from the TTL fast path. The
 * caller gets an honest "still starting" instead of a request that blocks for the
 * runtime's whole five-minute cold-start budget.
 */
async function waitWithinRequestBudget(work: Promise<boolean>, budgetMs: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<"timeout">((resolve) => {
    timer = setTimeout(() => resolve("timeout"), budgetMs);
  });
  try {
    return (await Promise.race([work, expired])) === true;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Whether a cold Python start is genuinely STILL IN FLIGHT.
 *
 * `ensurePythonRuntime()` answers "not ready yet" both when a cold start is
 * progressing and when the runtime can never start (no interpreter, missing
 * deps). Those are different facts for the person waiting: one resolves itself
 * in under a minute, the other needs an install. This tells the caller which
 * one it is, so the chat can retry a boot instead of showing an install error.
 *
 * It is exact: the in-flight `__harnessPyEnsure` promise is cleared only when
 * the start settles, so a pending promise IS a start in progress.
 */
export function pythonRuntimeIsStarting(): boolean {
  if (g.__harnessPyHealthyAt && Date.now() - g.__harnessPyHealthyAt < HEALTH_TTL_MS) {
    return false;
  }
  return g.__harnessPyEnsure !== undefined;
}

/**
 * Ensure the Python runtime is up. Returns true when healthy; false when it
 * could not be started (missing interpreter, dependencies) OR when it is still
 * starting past the request budget. Callers must treat `false` as "agent runtime
 * unavailable", not fall back to a fake agent.
 */
export async function ensurePythonRuntime(): Promise<boolean> {
  // Fast path: a recent successful check, and no reason to suspect it changed.
  if (g.__harnessPyHealthyAt && Date.now() - g.__harnessPyHealthyAt < HEALTH_TTL_MS) {
    return true;
  }
  // Coalesce concurrent callers (a page load fires several routes at once) onto
  // a single probe so the runtime is not asked N times for the same page.
  if (!g.__harnessPyEnsure) {
    g.__harnessPyEnsure = (async () => {
      const ok = await ensurePythonRuntimeUncached();
      if (ok) g.__harnessPyHealthyAt = Date.now();
      return ok;
    })().finally(() => {
      g.__harnessPyEnsure = undefined;
    });
  }

  const budget = resolveRequestWaitMs(process.env.HARNESS_PY_REQUEST_WAIT_MS);
  const ok = await waitWithinRequestBudget(g.__harnessPyEnsure, budget);
  if (!ok) {
    console.warn(
      `[harness] Python runtime still starting after ${Math.round(budget / 1000)}s — ` +
        `answering "unavailable" and letting the start finish in the background`
    );
  }
  return ok;
}

async function ensurePythonRuntimeUncached(): Promise<boolean> {
  // The liveness document carries startedAtMs/activeRuns/pid — everything the
  // staleness policy needs — without the diagnostic endpoint's subsystem probes.
  const health = await getLiveness();
  if (health) {
    // A healthy runtime that started before our source was last edited is an
    // orphan serving stale code (e.g. one left by a previous `npm run dev`).
    // Replace it so code changes actually take effect — but NEVER while it is
    // actively serving runs, since a restart orphans them (see the helper).
    // Only do this for a runtime that identifies itself as ours.
    if (shouldReplaceStaleRuntime(health, harnessSourceMtimeMs())) {
      console.warn(
        "[harness] stale Python runtime detected (started before the latest harness code) — restarting it"
      );
      killListenerOnPort();
      await new Promise((r) => setTimeout(r, 500));
    } else {
      return true;
    }
  }
  if (g.__harnessPyStart) return g.__harnessPyStart;

  // A child we already spawned may still be importing/booting. uvicorn runs the
  // whole FastAPI lifespan BEFORE binding its socket, so "no listener and no
  // /health answer" is also the NORMAL cold-start signature. Spawning a second
  // uvicorn in that window boots a rival stack that fights for SQLite and CPU.
  if (trackedRuntimeAlive()) {
    if (await waitForHealth(resolveStartTimeoutMs(process.env.HARNESS_PY_START_TIMEOUT_MS))) {
      return true;
    }
    killTrackedRuntime();
  }

  // Not healthy, and we are not mid-start. A process may be holding the runtime
  // port without answering /health — a hung/zombie runtime from a previous
  // `npm run dev` (spawned as a child, it survives the Next restart on Windows).
  // It cannot serve a request and it blocks a fresh spawn, so clear it. A second,
  // longer probe guards against a transient timeout on a genuinely healthy
  // runtime under load (only a still-silent LISTENER is cleared; a cold start with
  // nothing listening goes straight to the spawn).
  if (listenerPidsOnPort().length > 0) {
    const recheck = await getLiveness(READINESS_PROBE_TIMEOUT_MS);
    if (recheck) {
      // The first probe missed but the port answered on the second: that is a
      // runtime that is UP (slow, or busy booting), not a zombie. ADOPT it.
      // It used to fall through to the spawn below, booting a second uvicorn
      // that died with `WinError 10048` — and the app was left reporting
      // "runtime unavailable" for the rest of the session. Measured 2026-11-06.
      return true;
    }
    // A runtime that answered recently is far more likely to be BUSY than
    // genuinely hung (a running agent can briefly delay a probe). Killing a
    // busy runtime orphans its runs, so require that we have NOT seen it
    // healthy inside the busy-grace window before clearing the port.
    const recentlyHealthy = isRecentlyHealthy(g.__harnessPyHealthyAt, Date.now());
    if (recentlyHealthy) {
      console.warn(
        "[harness] runtime port is silent but the runtime answered recently — treating it as busy, not killing it"
      );
      // Do NOT fall through to the spawn below. A second uvicorn would fail to
      // bind the still-held port (`WinError 10048`) — but only AFTER FastAPI
      // runs its lifespan boot, whose `fail_orphaned_runs` reconciliation would
      // mark the very runs this busy runtime is executing as RUN_ORPHANED. A
      // busy runtime is up; let a later probe (past the grace window) re-check.
      return true;
    }
    killListenerOnPort();
    await new Promise((r) => setTimeout(r, 300));
  }

  // Cross-context lock: if another server context is already cold-starting the
  // runtime, wait for it instead of booting a second uvicorn.
  const spawnLock = acquireSpawnLock();
  if (spawnLock === null) {
    // Another server context owns the cold start. Say so: without this, a
    // packaged runtime that never answers logs only "still starting" and the
    // real reason (this wait) is invisible in a release smoke failure.
    console.log("[harness] another context is already starting the Python runtime — waiting");
    return waitForHealth(resolveStartTimeoutMs(process.env.HARNESS_PY_START_TIMEOUT_MS));
  }

  g.__harnessPyStart = (async () => {
    const projectRoot = findProjectRoot();
    const appDir = path.join(projectRoot, "python");
    if (!existsSync(path.join(appDir, "harness", "main.py"))) {
      console.error("[harness] python/harness/main.py not found — runtime unavailable");
      return false;
    }

    // Require the agent stack to be importable before spawning, so a missing
    // dependency is a clear message rather than a crash loop.
    const py = await findPython(PYTHON_RUNTIME_DEPS);
    if (!py) {
      console.error(
        `[harness] Python with agent dependencies not found. Run: npm run setup:python`
      );
      return false;
    }

    // Autonomous runs (automations/bots, jobs, tasks, fabric) execute inside the
    // runtime with no per-run model override — so they fall back to the runtime's
    // env default (`HARNESS_JOB_MODEL`, historically "llama3.1"). Interactive chat
    // passes the operator's saved model per run, which is why chat worked while a
    // scheduled bot failed with "model not found". Publish the SAME saved model
    // config into the child env so a bot uses the operator's configured provider
    // and model. A real env var always wins; this only fills the gaps.
    const extraEnv: Record<string, string> = {};
    // Provider keys saved from Settings live encrypted in JDDS and are copied
    // into THIS process's env during startup. The child is often spawned from
    // instrumentation BEFORE that copy runs, so without an explicit apply here
    // the runtime starts keyless and every agent/vision call fails with
    // "provider is not configured (missing API key)" until it happens to be
    // restarted later. `applyStoredKeysToEnv` never overwrites a real env var,
    // so a deployment-provided key still wins.
    try {
      const { applyStoredKeysToEnv } = await import("@/lib/provider-keys");
      await applyStoredKeysToEnv();
    } catch (err) {
      console.warn("[harness] could not apply stored provider keys for the runtime:", err);
    }
    try {
      const { getLLMConfig } = await import("@/lib/settings");
      const cfg = await getLLMConfig();
      const provider = resolveLLMProviderId(cfg);
      if (provider && !process.env.HARNESS_JOB_PROVIDER) extraEnv.HARNESS_JOB_PROVIDER = provider;
      if (cfg.model && !process.env.HARNESS_JOB_MODEL) extraEnv.HARNESS_JOB_MODEL = cfg.model;
      if (cfg.baseUrl && !process.env.HARNESS_JOB_BASE_URL) extraEnv.HARNESS_JOB_BASE_URL = cfg.baseUrl;
      if (cfg.apiKey && !process.env.HARNESS_JOB_API_KEY) extraEnv.HARNESS_JOB_API_KEY = cfg.apiKey;
      // W5.1: the boot/draft execution evaluation needs a model. Use the SAME
      // operator-configured model rather than requiring a second eval-only
      // credential. A real env var wins; with no configured model the tier
      // stays an honest `needs_review` (never simulated).
      if (provider && cfg.model && !process.env.ECLIPSE_SKILL_EVAL_MODEL) {
        extraEnv.ECLIPSE_SKILL_EVAL_MODEL = `${provider}:${cfg.model}`;
        if (cfg.baseUrl && !process.env.ECLIPSE_SKILL_EVAL_BASE_URL) {
          extraEnv.ECLIPSE_SKILL_EVAL_BASE_URL = cfg.baseUrl;
        }
        if (cfg.apiKey && !process.env.ECLIPSE_SKILL_EVAL_API_KEY) {
          extraEnv.ECLIPSE_SKILL_EVAL_API_KEY = cfg.apiKey;
        }
      }
    } catch (err) {
      console.warn("[harness] could not resolve saved model config for the runtime:", err);
    }

    console.log(`[harness] starting Python runtime: ${py.cmd} (port ${HARNESS_PY_PORT})`);
    const proc = spawn(
      py.cmd,
      ["-m", "uvicorn", "harness.main:app", "--host", HARNESS_PY_HOST, "--port", String(HARNESS_PY_PORT), "--app-dir", appDir],
      { cwd: projectRoot, stdio: ["ignore", "pipe", "pipe"], windowsHide: true, env: { ...process.env, ...extraEnv, PYTHONPATH: appDir } }
    );
    g.__harnessPyProc = proc;
    let bindConflict = false;
    let childExited = false;
    const exited = new Promise<number | null>((resolve) =>
      proc.once("exit", (code) => {
        childExited = true;
        resolve(code);
      })
    );

    proc.stdout?.on("data", (d) => process.stdout.write(`[harness-py] ${d}`));
    proc.stderr?.on("data", (d) => {
      const text = d.toString();
      if (isBindConflict(text)) bindConflict = true;
      process.stderr.write(`[harness-py] ${text}`);
    });
    proc.on("exit", (code) => {
      if (code !== 0 && code !== null) console.error(`[harness-py] exited with code ${code}`);
      g.__harnessPyProc = undefined;
      g.__harnessPyStart = undefined;
    });
    // A spawn that cannot even start (missing interpreter/DLL, EACCES) must be
    // visible: otherwise the only symptom is "still starting" for the whole
    // budget, which is what hid a broken release runtime for three releases.
    proc.on("error", (err) => {
      console.error(`[harness-py] failed to start (${py.cmd}): ${err.message}`);
    });

    const timeoutMs = resolveStartTimeoutMs(process.env.HARNESS_PY_START_TIMEOUT_MS);
    const ready = await Promise.race([
      waitForHealth(timeoutMs),
      // A spawn that dies (bad interpreter, taken port) must not make boot wait
      // the whole cold-start budget before reporting the failure.
      exited.then(() => false),
    ]);
    if (ready) return true;

    if (bindConflict) {
      // Another runtime already owns the port. Adopt it when it answers, rather
      // than leaving the app in a permanent "runtime unavailable" state.
      if (await waitForHealth(10_000)) {
        console.warn(
          `[harness] runtime port ${HARNESS_PY_PORT} was already owned by a live runtime — using it`
        );
        return true;
      }
      console.error(
        `[harness] runtime port ${HARNESS_PY_PORT} is held by a process that is not answering /health. ` +
          `Stop it (or set HARNESS_PY_PORT) and restart.`
      );
    } else if (!childExited) {
      // Still importing after the full budget. A cold start on a loaded machine
      // can outlast it (the budget is a timeout, not a verdict). KILLING here is
      // what made a slow boot permanent: the child was healthy but slow, and
      // nothing retries on its own, so agent features 503'd for the rest of the
      // session until someone POSTed /api/harness/runtime/restart by hand.
      // Leave it running; the next ensure() sees trackedRuntimeAlive() and waits
      // for it again, adopting it as soon as /health answers.
      console.warn(
        `[harness] Python runtime still starting after ${timeoutMs}ms — leaving it running; ` +
          `a later request will adopt it when /health answers`
      );
      return false;
    } else {
      console.error(`[harness] Python runtime did not become healthy within ${timeoutMs}ms`);
    }
    killTrackedRuntime();
    return false;
  })();

  try {
    return await g.__harnessPyStart;
  } finally {
    // The spawn attempt is finished (ready or not). Health is the source of
    // truth from here on: keeping the resolved promise around would let a later
    // /health miss return a stale `true` and mask a runtime that has since hung.
    g.__harnessPyStart = undefined;
    if (spawnLock >= 0) releaseSpawnLock(spawnLock);
  }
}

/** Stop a spawned runtime (test/teardown helper). */
export function stopPythonRuntime(): void {
  killTrackedRuntime();
  g.__harnessPyStart = undefined;
  g.__harnessPyHealthyAt = undefined;
  g.__harnessPyEnsure = undefined;
}

/**
 * Force the managed Python runtime to restart — the self-hosting seam (Track B).
 *
 * A change under `python/harness/**` only takes effect after a restart, and the
 * runtime cannot cleanly restart itself. The FRONT DOOR owns it: kill whatever is
 * listening on the runtime port (only our own service is spawned here), clear the
 * health cache, then spawn a fresh process and wait for `/health`. Returns true
 * only when the new runtime is healthy; false means "it did not come back", which
 * `code/selfhost.py`'s guarded landing treats as a rollback trigger.
 */
export async function restartPythonRuntime(): Promise<boolean> {
  killListenerOnPort();
  await new Promise((r) => setTimeout(r, 500));
  stopPythonRuntime();
  return ensurePythonRuntimeUncached();
}
