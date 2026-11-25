// ─── Python runtime HTTP client ──────────────────────────────────────────────
// Thin typed wrapper over the Python service. This is the only place Next.js
// talks to the runtime; it adds no agentic logic of its own.

import { HARNESS_PY_URL, ensurePythonRuntime, pythonRuntimeIsStarting } from "../python-runtime";
import { runtimeCallbackToken } from "../runtime-auth";
import type { HarnessErrorJson, HarnessEvent, RunRecord, RunResult, UsageSummary } from "../types";
import { redactSecrets, HarnessHttpError, isNotFoundError } from "../errors";

/**
 * A cold start is a BOOT, not a failure.
 *
 * The front door answers one request per ~3 s while uvicorn imports the agent
 * stack, so the first message sent right after an app restart used to end in a
 * hard "install the runtime" error even though the runtime was coming up
 * normally. The two cases now carry different codes so the client can retry a
 * boot and only report an install problem when it really is one.
 */
const RUNTIME_STARTING_MESSAGE =
  "Eclipse is starting its agent runtime. This normally takes under a minute; " +
  "your message will be sent automatically as soon as it is ready.";
const RUNTIME_MISSING_MESSAGE =
  "The Python agent runtime is not available. Install the runtime: npm run setup:python";

export class HarnessRuntimeUnavailable extends Error {
  /** `RUNTIME_STARTING` = a cold boot in progress; `RUNTIME_UNAVAILABLE` = cannot start. */
  readonly code: "RUNTIME_UNAVAILABLE" | "RUNTIME_STARTING";
  constructor(
    message: string,
    code: "RUNTIME_UNAVAILABLE" | "RUNTIME_STARTING" = "RUNTIME_UNAVAILABLE"
  ) {
    super(message);
    this.name = "HarnessRuntimeUnavailable";
    this.code = code;
  }
}

export interface RunRequestPayload {
  runId: string;
  input: {
    query: string;
    ticker?: string;
    market?: string;
    context?: Record<string, unknown>;
    history?: Array<{ role: "user" | "assistant"; content: string }>;
    documents?: Array<{ name: string; text: string; truncated?: boolean; originalChars?: number }>;
  };
  /** Authoritative agent id; Python resolves the config + prompt from it. */
  agentId: string;
  modelSettings: {
    provider: string;
    baseUrl?: string;
    apiKey?: string;
    /** Header that carries the key for a named custom connection. */
    apiKeyHeader?: string;
    /** Optional prefix for that header value (e.g. "Bearer "). */
    apiKeyPrefix?: string;
    /** Stable per-conversation id for gateways that require one. */
    sessionId?: string;
    model: string;
    temperature?: number;
    /** Optional max output tokens for one response; absent = provider default. */
    maxTokens?: number;
    /** Optional operator-declared context window; absent = unknown. */
    contextWindow?: number;
  };
  userId?: string;
  // M3/M4 request-scoped isolation identity (optional).
  sessionId?: string;
  profileId?: string;
  projectId?: string;
  /**
   * Private per-conversation memory (optional). When set, durable memory
   * captured in this run is owned by this conversation id (used by bot chats and
   * group-room member turns). Absent for ordinary chat, whose behaviour is
   * unchanged.
   */
  conversationId?: string;
  /** Phase 1 (optional): bind this run to a read-only workspace. */
  workspace?: { root: string; baseRevision?: string };
  /**
   * Phase 23 (optional): the APPROVED workspace root chosen for this chat
   * session. An id, never a path — the runtime resolves it against TrustedRoot
   * and refuses an unknown/revoked id.
   */
  workspaceRootId?: string;
  /** Phase 14 (optional): the code profile for this run, when the turn is a
   * coding request. Only `plan_only` | `edit_verify` are ever sent from chat. */
  autonomy?: "plan_only" | "edit_verify";
  /** Phase 8 (optional): enable the analysis sandbox for this turn. */
  analysisEnabled?: boolean;
  /**
   * The slash command that forced this turn's profile (e.g. "eclipsesdk"), when
   * the user invoked one. Additive/optional: an ordinary turn omits it. The
   * runtime never treats a command turn as trivial.
   */
  command?: string;
  /**
   * Approval cards (optional): "ask" makes a non-read tool with no matching
   * approval PAUSE the run until the operator decides via the approvals decide
   * route, then resume in place. Absent keeps the deny-and-retry default.
   */
  approvalMode?: "ask";
  callbackBaseUrl: string;
  callbackToken?: string;
}

async function request<T>(
  path: string,
  init: RequestInit & { timeoutMs?: number } = {}
): Promise<T> {
  const healthy = await ensurePythonRuntime();
  if (!healthy) {
    // Distinguish "still booting" from "will never boot": the first is retried
    // by the client, the second is an install instruction.
    const starting = pythonRuntimeIsStarting();
    throw new HarnessRuntimeUnavailable(
      starting ? RUNTIME_STARTING_MESSAGE : RUNTIME_MISSING_MESSAGE,
      starting ? "RUNTIME_STARTING" : "RUNTIME_UNAVAILABLE",
    );
  }
  const { timeoutMs = 180_000, ...rest } = init;
  const operatorToken = runtimeCallbackToken();
  const res = await fetch(`${HARNESS_PY_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(operatorToken ? { "X-Harness-Operator": operatorToken } : {}),
      ...(rest.headers ?? {}),
    },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (body as { detail?: string }).detail || `runtime HTTP ${res.status}`;
    throw new HarnessHttpError(res.status, redactSecrets(detail), detail);
  }
  return body as T;
}

export async function runtimeHealth(): Promise<{ ok: boolean; tools?: string[]; providers?: string[] }> {
  if (!(await ensurePythonRuntime())) return { ok: false };
  return request<{ ok: boolean; tools?: string[]; providers?: string[] }>("/health", { method: "GET" });
}

export async function listModelProviders(): Promise<Array<Record<string, unknown>>> {
  const body = await request<{ providers: Array<Record<string, unknown>> }>("/models", { method: "GET" });
  return body.providers ?? [];
}

export async function listAgentsFromRuntime(): Promise<{
  agents: Array<Record<string, unknown>>;
  defaultAgentId: string;
}> {
  const body = await request<{ agents: Array<Record<string, unknown>>; defaultAgentId?: string }>("/agents", {
    method: "GET",
  });
  return { agents: body.agents ?? [], defaultAgentId: body.defaultAgentId ?? "eclipse-agent" };
}

export async function runViaRuntime(
  payload: RunRequestPayload,
  opts: { background?: boolean; headers?: Record<string, string> } = {}
): Promise<RunResult | { runId: string; status: string }> {
  const query = opts.background ? "?background=true" : "";
  return request<RunResult | { runId: string; status: string }>(`/runs${query}`, {
    method: "POST",
    body: JSON.stringify(payload),
    ...(opts.headers ? { headers: opts.headers } : {}),
  });
}

/**
 * Start a run in the background so its SSE stream can be observed live.
 * Returns the run id; the caller then streams `/api/harness/runs/:id/stream`.
 */
export async function startRunBackground(
  payload: RunRequestPayload,
  headers?: Record<string, string>
): Promise<string> {
  const body = await runViaRuntime(payload, { background: true, ...(headers ? { headers } : {}) });
  return (body as { runId: string }).runId;
}

export async function getRunFromRuntime(runId: string): Promise<RunRecord | null> {
  try {
    const body = await request<{ run: RunRecord }>(`/runs/${encodeURIComponent(runId)}`, { method: "GET" });
    return body.run;
  } catch (err) {
    if (isNotFoundError(err)) return null;
    throw err;
  }
}

export async function listRunsFromRuntime(limit = 50): Promise<RunRecord[]> {
  const body = await request<{ runs: RunRecord[] }>(`/runs?limit=${limit}`, { method: "GET" });
  return body.runs ?? [];
}

export async function getUsageSummaryFromRuntime(days = 30): Promise<UsageSummary> {
  return request<UsageSummary>(`/usage?days=${days}`, { method: "GET" });
}

export interface RuntimeHistoryCompaction {
  ok: boolean;
  block: string;
  droppedTurns: number;
  keptTurns: number;
  chars: number;
  reason?: string | null;
}

/**
 * Compact an explicit conversation history through the ONE runtime
 * implementation (`python/harness/compaction.py`). This is the `/compact`
 * command's endpoint: the front door already owns the conversation, so it
 * sends the turns and receives the fenced summary block. It never calls a
 * model, and `ok: false` means there was nothing older than `keepTurns`.
 */
export async function compactHistoryInRuntime(
  history: Array<{ role: "user" | "assistant"; content: string }>,
  keepTurns = 6,
): Promise<RuntimeHistoryCompaction> {
  return request<RuntimeHistoryCompaction>("/history/compact", {
    method: "POST",
    body: JSON.stringify({ history, keepTurns }),
    timeoutMs: 30_000,
  });
}

export async function getEventsFromRuntime(
  runId: string
): Promise<{ events: HarnessEvent[]; toolExecutions: Array<Record<string, unknown>> }> {
  const body = await request<{ events: HarnessEvent[]; toolExecutions: Array<Record<string, unknown>> }>(
    `/runs/${encodeURIComponent(runId)}/events`,
    { method: "GET" }
  );
  return { events: body.events ?? [], toolExecutions: body.toolExecutions ?? [] };
}

/**
 * Fetch the durable research receipt for a run (Phase 4).
 *
 * Read-only. Returns null when no receipt exists (ordinary chat or an older
 * run), so callers can render an honest "unavailable" state.
 */
export async function getRunReceiptFromRuntime(
  runId: string
): Promise<Record<string, unknown> | null> {
  try {
    const body = await request<{ receipt: Record<string, unknown> }>(
      `/research/receipts/${encodeURIComponent(runId)}`,
      { method: "GET" }
    );
    return body.receipt ?? null;
  } catch (err) {
    if (isNotFoundError(err)) return null;
    throw err;
  }
}

/** Run-level memory/skill explanation (Phase 7). Derived, read-only, redacted. */
export async function getRunExplanationFromRuntime(
  runId: string
): Promise<Record<string, unknown> | null> {
  try {
    return await request<Record<string, unknown>>(
      `/runs/${encodeURIComponent(runId)}/explain`,
      { method: "GET" }
    );
  } catch (err) {
    if (isNotFoundError(err)) return null;
    throw err;
  }
}

export async function cancelRunInRuntime(runId: string): Promise<boolean> {
  const body = await request<{ cancelled: boolean }>(`/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  });
  return Boolean(body.cancelled);
}

/**
 * Resume a checkpointed run after a runtime restart. The caller supplies the
 * original RunRequest-shaped payload (input + agent + model settings); the run
 * id in the path is authoritative. `background=true` returns 202 immediately
 * and the run continues; the client follows the existing stream.
 */
export async function resumeRunInRuntime(
  runId: string,
  payload: Record<string, unknown>
): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `/runs/${encodeURIComponent(runId)}/resume?background=true`,
    {
      method: "POST",
      body: JSON.stringify({ ...payload, runId }),
      timeoutMs: 30_000,
    }
  );
}

export type { HarnessErrorJson };
