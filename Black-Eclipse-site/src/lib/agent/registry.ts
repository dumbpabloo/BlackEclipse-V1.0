import type { AgentProvider, AgentStatusEvent, AgentTool, ChatMessage, ChatOptions } from './types';
import { normalizeHarnessEvent, unwrapEventPayload } from './events';
import { getQuantMode } from './quant-mode';
import { getChoicesMode } from './choices-mode';
import { getPlanMode } from './plan-mode';
import { ingestHarnessFrame } from '@/lib/agent-workspace/ingest';
import { getActiveProjectId } from '@/lib/projects/project-client';
import { isTransientFetchError } from '@/lib/fetch-retry';

export { unwrapEventPayload } from './events';

// ── Active-run recovery ──────────────────────────────────────────────────────
// A dropped SSE connection must not start a SECOND run. We remember the run id
// the server started, per conversation, and on a stream failure poll the run's
// durable status until it terminates, then return its output. The map survives
// a reload so every live conversation can reattach, not just the last one.
const ACTIVE_RUN_KEY = 'eclipse.activeRuns.v2';
const LEGACY_ACTIVE_RUN_KEY = 'eclipse.activeRun.v1';

interface ActiveRunRecord {
  runId: string;
  chatId?: string;
  at: number;
}

interface ActiveRunBook {
  version: 2;
  runs: Record<string, ActiveRunRecord>;
}

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

function readBook(): ActiveRunBook {
  try {
    const raw = storage()?.getItem(ACTIVE_RUN_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as ActiveRunBook;
      if (parsed && parsed.version === 2 && parsed.runs && typeof parsed.runs === 'object') {
        return parsed;
      }
    }
    // Migrate the single-record key from an older client.
    const legacy = storage()?.getItem(LEGACY_ACTIVE_RUN_KEY);
    if (legacy) {
      const record = JSON.parse(legacy) as ActiveRunRecord;
      if (record && typeof record.runId === 'string') {
        const key = record.chatId || '__default__';
        const book: ActiveRunBook = { version: 2, runs: { [key]: record } };
        storage()?.setItem(ACTIVE_RUN_KEY, JSON.stringify(book));
        storage()?.removeItem(LEGACY_ACTIVE_RUN_KEY);
        return book;
      }
    }
  } catch {
    // A corrupt/unavailable store is not fatal; recovery is best-effort.
  }
  return { version: 2, runs: {} };
}

function writeBook(book: ActiveRunBook): void {
  try {
    if (Object.keys(book.runs).length === 0) {
      storage()?.removeItem(ACTIVE_RUN_KEY);
      return;
    }
    storage()?.setItem(ACTIVE_RUN_KEY, JSON.stringify(book));
  } catch {
    // sessionStorage can be unavailable/blocked; recovery is best-effort.
  }
}

export function rememberActiveRun(runId: string, chatId?: string): void {
  const book = readBook();
  const key = chatId && chatId.length > 0 ? chatId : '__default__';
  book.runs[key] = { runId, chatId, at: Date.now() };
  writeBook(book);
}

export function forgetActiveRun(runId?: string): void {
  const book = readBook();
  if (!runId) {
    writeBook({ version: 2, runs: {} });
    return;
  }
  let changed = false;
  for (const [key, record] of Object.entries(book.runs)) {
    if (record.runId === runId) {
      delete book.runs[key];
      changed = true;
    }
  }
  if (changed) writeBook(book);
}

export function loadActiveRun(chatId?: string): ActiveRunRecord | null {
  const book = readBook();
  if (chatId) {
    const exact = book.runs[chatId];
    if (exact) return exact;
    const fallback = book.runs.__default__;
    if (fallback && (!fallback.chatId || fallback.chatId === chatId)) return fallback;
    return null;
  }
  const records = Object.values(book.runs);
  if (records.length === 0) return null;
  return records.reduce((latest, record) => (record.at > latest.at ? record : latest), records[0]);
}

/** Every run the client knows about, for a reload/reattach sweep. */
export function loadActiveRuns(): ActiveRunRecord[] {
  return Object.values(readBook().runs);
}

export async function getRunSnapshot(
  runId: string,
  signal?: AbortSignal,
): Promise<Record<string, unknown> | null> {
  const res = await fetch(`/api/harness/runs/${encodeURIComponent(runId)}`, {
    cache: 'no-store',
    signal,
  });
  if (!res.ok) return null;
  const body = (await res.json()) as { run?: Record<string, unknown> } & Record<string, unknown>;
  return (body.run ?? body) as Record<string, unknown>;
}

/**
 * Poll a run to a terminal state and return its answer. Used after a stream
 * drop: the run keeps executing in the runtime, so reconnecting must never
 * submit the prompt again.
 */
export async function recoverRun(runId: string, signal?: AbortSignal): Promise<string> {  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const res = await fetch(`/api/harness/runs/${encodeURIComponent(runId)}`, {
      cache: 'no-store',
      signal,
    });
    if (res.ok) {
      const body = (await res.json()) as { run?: Record<string, unknown> } & Record<string, unknown>;
      const run = (body.run ?? body) as Record<string, unknown>;
      const status = typeof run.status === 'string' ? run.status : '';
      if (status === 'completed') {
        forgetActiveRun(runId);
        return typeof run.output === 'string' ? run.output : '';
      }
      if (status === 'failed' || status === 'cancelled') {
        forgetActiveRun(runId);
        const error = run.error as { message?: unknown } | undefined;
        throw new Error(
          typeof error?.message === 'string'
            ? error.message
            : `run ${status}`,
        );
      }
      if (status === 'interrupted') {
        // Do not silently re-run an interrupted effect. The run panel has the
        // explicit resume path; the chat reports the honest state.
        throw new Error('run was interrupted by a restart; resume it from the run panel');
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  throw new Error('run is still executing; reconnect from the run panel');
}


/**
 * Registry of agent providers (client-safe: no server imports).
 *
 * Chat is routed through the HARNESS, so the assistant is the Eclipse Agent —
 * system prompt, JDdS tools, persisted run — not a bare completion. The server
 * owns provider configuration (settings vault), so this client layer never
 * decides whether an LLM exists; it forwards and reports the server's answer
 * honestly (a 503 means the runtime is unavailable, never a silently faked
 * reply).
 *
 * The primary path streams: `/api/harness/chat/stream` relays the runtime's SSE
 * events so the UI can show the agent working and writing. If streaming is
 * unavailable (old server, proxy that buffers, runtime spawn failure) it falls
 * back to the one-shot `/api/harness/chat` — a run always produces an answer or
 * an honest error, never a blank.
 */

// Map a raw harness event to a coarse UI status. Kept here (not the route) so
// the labels stay in one place and are reusable by any streaming surface.
export function statusFromEvent(type: string, data: Record<string, unknown> | undefined): AgentStatusEvent | null {
  switch (type) {
    case "run.started":
      return { phase: "starting", label: "Getting ready…", eventType: type };
    case "model.requested":
      return { phase: "thinking", label: "Thinking…", eventType: type };
    case "tool.requested": {
      const tool = typeof data?.name === "string" ? data.name : undefined;
      return { phase: "tool", label: tool ? `Using ${tool}…` : "Using a tool…", eventType: type, tool };
    }
    case "tool.completed": {
      const tool = typeof data?.name === "string" ? data.name : undefined;
      return { phase: "tool", label: tool ? `${tool} done` : "Tool done", eventType: type, tool };
    }
    case "model.delta":
      return { phase: "writing", label: "Writing…", eventType: type };
    case "run.completed":
      return { phase: "done", label: "Done", eventType: type };
    default:
      return null;
  }
}

function buildPayload(messages: ChatMessage[], options?: ChatOptions) {
  const last = [...messages].reverse().find((m) => m.role === 'user');
  const query = last?.content ?? '';
  // Skip empty turns: a cancelled/failed assistant reply is persisted with
  // `content: ""`, and sending it would make the front door reject the whole
  // run (400 INVALID_INPUT) — bricking the conversation after one failure.
  const history = messages
    .filter((m) => m !== last && (m.role === 'user' || m.role === 'assistant') && m.content.trim().length > 0)
    .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content }));
  // The client owns the run id so cancellation is possible while in flight.
  // A Resume reuses the failed run's id so the runtime continues that run from
  // its checkpoint instead of starting over.
  const runId = options?.resumeRunId ??
    (typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? `run_${crypto.randomUUID()}`
      : `run_${Date.now().toString(36)}`);
  options?.onRunId?.(runId);
  // The project this turn is scoped to (a local preference, not a grant). The
  // runtime resolves it and puts the project's members in front of the model.
  const projectId = getActiveProjectId() ?? undefined;
  return {
    query,
    history,
    chatId: options?.chatId,
    agentId: options?.agentId,
    runId,
    resumeRunId: options?.resumeRunId,
    model: options?.model,
    modelProvider: options?.modelProvider,
    reasoningEffort: options?.reasoningEffort,
    quantMode: options?.quantMode ?? getQuantMode(),
    choicesMode: options?.choicesMode ?? getChoicesMode(),
    planMode: options?.planMode ?? getPlanMode(),
    uiContext: options?.uiContext,
    memoryScope: options?.memoryScope,
    projectId,
    images: options?.images,
  };
}

/**
 * A chat request the front door refused, carrying its error code and status.
 *
 * Why: the chat card used to show a bare "Something went wrong" plus the
 * server's sentence, with no code — so a rejected request was
 * indistinguishable from a runtime failure. The code is what lets the UI badge
 * the failure, and the type is what tells the retry logic that repeating the
 * SAME request cannot help (a refused request fails the same way every time).
 */
export class ChatRequestError extends Error {
  /** The front door's machine-readable code (e.g. `INVALID_INPUT`), when it sent one. */
  readonly code?: string;
  /** The HTTP status of the refused request. */
  readonly status: number;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'ChatRequestError';
    this.status = status;
    this.code = code;
  }
}

/** Read a refused response into a typed error, keeping the server's code. */
async function readRequestError(res: Response): Promise<ChatRequestError> {
  const data = (await res.json().catch(() => ({}))) as { error?: unknown; code?: unknown };
  const message =
    typeof data.error === 'string' && data.error.trim()
      ? data.error
      : `Agent run failed (${res.status})`;
  const code = typeof data.code === 'string' && data.code ? data.code : undefined;
  return new ChatRequestError(message, res.status, code);
}

/** One-shot fallback used when SSE is unavailable. */
async function chatOnce(messages: ChatMessage[], options?: ChatOptions): Promise<string> {
  const payload = buildPayload(messages, options);
  const res = await fetch('/api/harness/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: options?.signal,
  });
  if (!res.ok) throw await readRequestError(res);
  const data = await res.json().catch(() => ({}));
  return data.content as string;
}

/**
 * Parse an SSE byte stream into (event, data) frames. Handles partial frames
 * split across reads and the `\r\n` line endings some proxies introduce.
 */
function createSseParser(onFrame: (event: string, data: Record<string, unknown>) => void) {
  let buffer = '';
  return (chunk: string) => {
    buffer += chunk;
    let idx: number;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = 'message';
      const dataLines: string[] = [];
      for (const line of raw.split('\n')) {
        const clean = line.replace(/\r$/, '');
        if (clean.startsWith('event:')) event = clean.slice(6).trim();
        else if (clean.startsWith('data:')) dataLines.push(clean.slice(5).trim());
      }
      if (dataLines.length === 0) continue;
      try {
        onFrame(event, JSON.parse(dataLines.join('\n')) as Record<string, unknown>);
      } catch {
        // Ignore a malformed frame rather than killing the stream.
      }
    }
  };
}

/**
 * How long a chat POST keeps retrying while the Python runtime cold-boots.
 *
 * A cold start imports the whole agent stack and legitimately takes up to a
 * minute on a busy machine; the front door answers a request after ~3 s while
 * that boot runs. Retrying here (with a visible "Starting Eclipse…" status)
 * turns "your first message after a restart failed" into "the message was sent
 * when the runtime was ready" — the failure mode this budget exists to prevent.
 */
const RUNTIME_START_RETRY_BUDGET_MS = 120_000;
const RUNTIME_START_RETRY_BASE_MS = 500;
const RUNTIME_START_RETRY_CAP_MS = 5_000;

function retryDelayMs(attempt: number): number {
  return Math.min(RUNTIME_START_RETRY_CAP_MS, RUNTIME_START_RETRY_BASE_MS * 2 ** attempt);
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function announceRuntimeStarting(options: ChatOptions | undefined, elapsedMs: number): void {
  const seconds = Math.max(1, Math.round(elapsedMs / 1000));
  options?.onStatus?.({
    phase: 'starting',
    label: seconds >= 5 ? `Starting Eclipse… (${seconds}s)` : 'Starting Eclipse…',
    // Not a harness event: this is the client waiting on the runtime itself.
    eventType: 'runtime.starting',
  });
}

/**
 * POST the chat turn, riding out a cold runtime boot and a server restart.
 *
 * Only two failures are repeated: a `503 RUNTIME_STARTING` (a boot, not a
 * refusal) and a transport-level `Failed to fetch` (the server was mid-restart,
 * so the request never reached it). Every other status is returned unchanged
 * for the caller to turn into an error.
 */
async function postChatStream(
  payload: ReturnType<typeof buildPayload>,
  options?: ChatOptions,
): Promise<Response> {
  const startedAt = Date.now();
  let attempt = 0;
  for (;;) {
    try {
      const res = await fetch('/api/harness/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: options?.signal,
      });
      if (res.status !== 503) return res;
      // Inspect a CLONE: the original body must survive for the caller's error.
      const probe = res.clone();
      const data = (await probe.json().catch(() => ({}))) as { code?: unknown };
      if (data.code !== 'RUNTIME_STARTING') return res;
      if (Date.now() - startedAt >= RUNTIME_START_RETRY_BUDGET_MS) return res;
      announceRuntimeStarting(options, Date.now() - startedAt);
      await sleep(retryDelayMs(attempt++), options?.signal);
    } catch (err) {
      // A transport failure means the request never got a response. The run id
      // is client-owned and the runtime's lease guard ignores a duplicate
      // submission, so re-POSTing the same turn cannot start a second run.
      if (options?.signal?.aborted || !isTransientFetchError(err)) throw err;
      if (Date.now() - startedAt >= RUNTIME_START_RETRY_BUDGET_MS) throw err;
      announceRuntimeStarting(options, Date.now() - startedAt);
      await sleep(retryDelayMs(attempt++), options?.signal);
    }
  }
}

async function chatStreaming(messages: ChatMessage[], options?: ChatOptions): Promise<string> {
  const payload = buildPayload(messages, options);
  const res = await postChatStream(payload, options);
  if (!res.ok) {
    // A missing streaming ENDPOINT (an older front door) still has the
    // one-shot path — fall back to it. Anything else means the REQUEST was
    // refused: repeating it would only produce the same refusal, so fail fast
    // with the real reason (and the front door's code) instead.
    if (res.status === 404 || res.status === 405) {
      return chatOnce(messages, { ...options, onRunId: undefined, resumeRunId: undefined, signal: options?.signal });
    }
    throw await readRequestError(res);
  }
  if (!res.body) {
    // Streaming not available — fall back to the one-shot path. Drop the abort
    // signal so a cancellation that raced the fallback still works, and drop
    // onRunId so the run id is regenerated for the one-shot call.
    return chatOnce(messages, { ...options, onRunId: undefined, resumeRunId: undefined, signal: options?.signal });
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let assembled = '';
  let finalText: string | null = null;
  let streamError: string | null = null;
  let streamRunId: string | undefined;

  const parse = createSseParser((event, data) => {
    // Feed the Agent Workspace observability layer from the same stream. This is
    // additive: it never changes what the chat does with the frame.
    ingestHarnessFrame(event, data);

    // Normalize first — this is the ONE place raw frames become typed events.
    const uiEvent = normalizeHarnessEvent(event, data);
    if (uiEvent) options?.onEvent?.(uiEvent);

    // Keep the legacy coarse callbacks working for any consumer that still uses
    // them (onStatus/onDelta). `unwrapEventPayload` gives the same payload the
    // old code read when calling `statusFromEvent`.
    const payload = unwrapEventPayload(data);
    const runId =
      (typeof data.runId === 'string' ? data.runId : undefined) ??
      (typeof payload.runId === 'string' ? payload.runId : undefined);
    if (runId && runId !== streamRunId) {
      streamRunId = runId;
      options?.onRunId?.(runId);
      rememberActiveRun(runId, options?.chatId);
    }
    const status = statusFromEvent(event, payload);
    if (status) options?.onStatus?.(status);

    if (uiEvent?.kind === 'delta') {
      assembled += uiEvent.text;
      options?.onDelta?.(uiEvent.text);
    } else if (event === 'client.final') {
      if (typeof payload.output === 'string') finalText = payload.output;
      if (typeof payload.error === 'string') streamError = payload.error;
    } else if (event === 'client.error') {
      streamError = typeof payload.message === 'string' ? payload.message : 'stream error';
    }
  });

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      parse(decoder.decode(value, { stream: true }));
    }
  } catch (err) {
    if (options?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (streamRunId) {
      try {
        return await recoverRun(streamRunId, options?.signal);
      } catch (recoverErr) {
        if (options?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        throw recoverErr instanceof Error ? recoverErr : err;
      }
    }
    throw err;
  }

  if (streamRunId) forgetActiveRun(streamRunId);
  // Prefer the authoritative final text; fall back to the assembled deltas.
  if (finalText) return finalText;
  if (assembled) return assembled;
  if (streamError) throw new Error(streamError);
  return '';
}

const eclipseAgent: AgentProvider = {
  id: 'eclipse-agent',
  label: 'Eclipse Agent',
  isConfigured: () => true,
  chat: async (messages: ChatMessage[], _tools?: AgentTool[], options?: ChatOptions) => {
    try {
      return await chatStreaming(messages, options);
    } catch (err) {
      // A user cancellation is not a failure to recover from.
      if (options?.signal?.aborted) throw err;
      // A request the server REFUSED cannot be fixed by repeating it (and the
      // run may not even have started). Only a broken stream is retried.
      if (err instanceof ChatRequestError) throw err;
      // A streaming failure must not lose the run: retry once, one-shot.
      if (options?.onDelta || options?.onStatus || options?.onEvent) {
        return chatOnce(messages, { ...options, onRunId: undefined, resumeRunId: undefined });
      }
      throw err;
    }
  },
};

export function getAgentProvider(): AgentProvider {
  return eclipseAgent;
}
