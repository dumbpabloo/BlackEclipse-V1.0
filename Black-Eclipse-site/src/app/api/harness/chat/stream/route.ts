import { guardExpensiveMutation } from "@/lib/api-guard";
import {
  startChatRun,
  getRunFromRuntime,
  HarnessError,
  HarnessRuntimeUnavailable,
  toHarnessErrorJson,
  MAX_QUERY_LENGTH,
} from "@/lib/harness";
import { HARNESS_PY_URL } from "@/lib/harness/python-runtime";
import { runtimeIdentityHeaders } from "@/lib/harness/runtime-identity";
import { loadContextDocuments, readSessionWorkspaceRootId, writeSessionFile, readStoredCompaction, writeStoredCompaction, applyStoredCompaction } from "@/lib/chat/session-store";
import { isReasoningLevel } from "@/lib/agent/reasoning";
import { parseCommandInvocation } from "@/lib/harness/commands/command-parser";
import { executeCommandFromText, isRoutedCommand } from "@/lib/harness/commands/command-router";
import { isOperatorRequest } from "@/lib/harness/clients/skills-client";
import { newRunId } from "@/lib/harness/ids";

export const dynamic = "force-dynamic";
export const maxDuration = 300;

const SSE_HEADERS = {
  "Content-Type": "text/event-stream; charset=utf-8",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
  "X-Accel-Buffering": "no",
};

// POST /api/harness/chat/stream — streaming chat.
//
// Starts the run in the background, then relays the runtime's SSE stream to the
// browser (run.started, model.delta tokens, tool.*, run.completed). When the run
// finishes, the final answer is persisted to the session folder exactly like the
// non-streaming route. Body: { query, history?, chatId?, runId? }
export async function POST(request: Request) {
  const blocked = guardExpensiveMutation(request, "harness-chat-stream", { limit: 60, windowMs: 5 * 60_000 });
  if (blocked) {
    return Response.json(blocked.body, { status: blocked.status, headers: blocked.headers });
  }

  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return Response.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const query = typeof body.query === "string" ? body.query.trim() : "";
  if (!query) return Response.json({ error: "query is required", code: "INVALID_INPUT" }, { status: 400 });
  // A turn is whatever the person pasted. The cap only bounds one request
  // body — the runtime decides whether a prompt actually fits the model's
  // context window, and says so honestly when it does not.
  if (query.length > MAX_QUERY_LENGTH) {
    return Response.json(
      {
        error: `message is too long for one turn (max ${MAX_QUERY_LENGTH} characters) — send it in smaller parts or attach it as a document`,
        code: "INVALID_INPUT",
      },
      { status: 400 },
    );
  }

  // The conversation BEFORE this turn. Parsed before command handling because
  // `/compact` needs it and a compaction is persisted against this chat id.
  const rawHistory = body.history;
  const history = Array.isArray(rawHistory)
    ? rawHistory
        .filter(
          (t): t is { role: "user" | "assistant"; content: string } =>
            t !== null &&
            typeof t === "object" &&
            ((t as { role?: unknown }).role === "user" || (t as { role?: unknown }).role === "assistant") &&
            typeof (t as { content?: unknown }).content === "string"
        )
        .map((t) => ({ role: t.role, content: t.content }))
    : [];
  const chatId = typeof body.chatId === "string" ? body.chatId : "";

  // A routed slash command (`/skills`, `/skill`, `/status`, …) is executed by the
  // shared command router and streamed back as a synthetic terminal frame — no
  // LLM provider is needed. Profile commands (`/eclipsesdk`) and ordinary prose
  // fall through to the normal agent path. An unknown `/token` also falls through,
  // so existing text that merely starts with a slash keeps working.
  const invocation = parseCommandInvocation(query);
  if (invocation?.spec && isRoutedCommand(invocation.spec)) {
    const result = await executeCommandFromText(query, {
      operator: isOperatorRequest(request),
      origin: new URL(request.url).origin,
      history,
      chatId: chatId || undefined,
    });
    if (result) {
      // A real `/compact` result is stored beside the transcript so later runs
      // send the summary instead of the replaced turns.
      if (result.compaction && chatId) {
        try {
          await writeStoredCompaction(chatId, {
            through: history.length,
            block: result.compaction.block,
            droppedTurns: result.compaction.droppedTurns,
            keptTurns: result.compaction.keptTurns,
          });
        } catch (err) {
          console.warn("[api/harness/chat/stream] could not persist compaction:", err);
        }
      }
      const cmdRunId = newRunId();
      const encoder = new TextEncoder();
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const send = (type: string, data: unknown) => {
            controller.enqueue(encoder.encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));
          };
          send("client.run", { runId: cmdRunId, documentsUsed: [] });
          send("client.final", {
            runId: cmdRunId,
            // A command that refused (unknown/invalid/unavailable/denied) still
            // RAN and produced a readable explanation, so the turn settles as
            // completed. Only a genuine execution failure surfaces as an error.
            status: result.status === "failed" ? "failed" : "completed",
            output: result.output,
            ...(result.status === "failed" ? { error: result.error?.message } : {}),
            ...(result.ui ? { ui: result.ui } : {}),
            provider: null,
            model: null,
          });
          send("stream.end", {});
          controller.close();
        },
      });
      return new Response(stream, { headers: SSE_HEADERS });
    }
  }

  // Phase 23: this conversation's approved project folder, read SERVER-SIDE so
  // the browser cannot point a run at an arbitrary directory.
  const workspaceRootId = chatId
    ? await readSessionWorkspaceRootId(chatId).catch(() => undefined)
    : undefined;
  // Bot chats and group-member turns opt into private per-conversation memory.
  // Only the one known value is honoured; anything else is ignored.
  const memoryScope = body.memoryScope === "conversation" ? ("conversation" as const) : undefined;
  const model = typeof body.model === "string" ? body.model.trim() : "";
  // A bot chat passes its own brain + pinned provider; ordinary chat omits both.
  const rawAgentId = body.agentId;
  const agentId = typeof rawAgentId === "string" && /^[A-Za-z0-9._:-]{1,120}$/.test(rawAgentId.trim())
    ? rawAgentId.trim()
    : undefined;
  const rawModelProvider = body.modelProvider;
  const modelProvider = typeof rawModelProvider === "string" && /^[A-Za-z0-9._:-]{1,80}$/.test(rawModelProvider.trim())
    ? rawModelProvider.trim()
    : undefined;
  const rawEffort = body.reasoningEffort;
  const reasoningEffort = isReasoningLevel(rawEffort) ? rawEffort : undefined;
  // The composer's Auto/Quant/Off preference. Only the explicit values are
  // forwarded; "auto"/absent leaves the runtime's classifier in charge.
  const rawQuantMode = body.quantMode;
  const quantMode = rawQuantMode === "on" || rawQuantMode === "off" ? rawQuantMode : undefined;
  // The composer's Auto/Choices/Off preference. Only the explicit values are
  // forwarded; "auto"/absent leaves the base prompt's act-don't-ask guidance.
  const rawChoicesMode = body.choicesMode;
  const choicesMode = rawChoicesMode === "on" || rawChoicesMode === "off" ? rawChoicesMode : undefined;
  // The composer's Auto/Plan/Off preference. Only the explicit values are
  // forwarded; "auto"/absent leaves the base guidance in charge.
  const rawPlanMode = body.planMode;
  const planMode = rawPlanMode === "on" || rawPlanMode === "off" ? rawPlanMode : undefined;
  const rawRunId = body.runId;
  const runId =
    typeof rawRunId === "string" && /^run_[A-Za-z0-9-]{1,64}$/.test(rawRunId) ? rawRunId : undefined;
  // Resume: continue an existing failed run from its checkpoint. The same
  // validated shape as `runId`; Retry sends it instead of minting a new run.
  const rawResumeRunId = body.resumeRunId;
  const resumeRunId =
    typeof rawResumeRunId === "string" && /^run_[A-Za-z0-9-]{1,64}$/.test(rawResumeRunId)
      ? rawResumeRunId
      : undefined;

  // Reference images attached to a bot (visual teaching). Shape/size are
  // re-validated centrally by `validateRunInput`.
  const rawImages = body.images;
  const images = Array.isArray(rawImages)
    ? (rawImages as import("@/lib/harness/types").RunImageInput[])
    : undefined;

  // Presentation-only UI focus (which page the user is on). Sanitized + bounded:
  // strings only, trimmed, capped; anything else is dropped.
  const uiContext = (() => {
    const raw = body.uiContext;
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return undefined;
    const page = (raw as { page?: unknown }).page;
    if (page === null || typeof page !== "object" || Array.isArray(page)) return undefined;
    const p = page as Record<string, unknown>;
    const str = (v: unknown) => (typeof v === "string" ? v.trim().slice(0, 200) : undefined);
    const out = { blueprintId: str(p.blueprintId), title: str(p.title), path: str(p.path) };
    if (!out.blueprintId && !out.title && !out.path) return undefined;
    return { page: out };
  })();

  // The project this turn is scoped to. The runtime resolves the id and puts
  // the project's members in front of the model as a per-run context note; an
  // unknown id is reported by the runtime, never silently ignored.
  const rawProjectId = body.projectId;
  const projectId =
    typeof rawProjectId === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(rawProjectId.trim())
      ? rawProjectId.trim()
      : undefined;

  let documents: Array<{ name: string; text: string }> = [];
  if (chatId) {
    try {
      documents = await loadContextDocuments(chatId);
    } catch (err) {
      console.warn("[api/harness/chat/stream] could not load context documents:", err);
    }
  }

  let id: string;
  try {
    const identityHeaders = await runtimeIdentityHeaders(request);
    // A stored `/compact` replaces the older turns before they reach the model.
    const effectiveHistory = applyStoredCompaction(
      history,
      chatId ? await readStoredCompaction(chatId) : null,
    );
    id = await startChatRun(query, effectiveHistory, documents, resumeRunId ?? runId, chatId || undefined, model || undefined, reasoningEffort, identityHeaders, uiContext, memoryScope, projectId, workspaceRootId, quantMode, choicesMode, planMode, agentId, modelProvider, Boolean(resumeRunId), images);
  } catch (err) {
    const error = toHarnessErrorJson(err);
    const status =
      err instanceof HarnessRuntimeUnavailable ? 503
      : err instanceof HarnessError && err.code === "PROVIDER_NOT_CONFIGURED" ? 503
      : err instanceof HarnessError && err.code === "INVALID_INPUT" ? 400
      : 500;
    return Response.json(
      { error: error.message, code: error.code },
      {
        status,
        // A cold boot is retryable; say how long to wait.
        ...(error.code === "RUNTIME_STARTING" ? { headers: { "Retry-After": "3" } } : {}),
      },
    );
  }

  // Relay the runtime stream and capture the final answer to persist on close.
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const send = (type: string, data: unknown) => {
        controller.enqueue(encoder.encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));
      };
      send("client.run", { runId: id, documentsUsed: documents.map((d) => d.name) });

      let finished = false;
      try {
        // Talk to the runtime directly: a relative fetch has no base URL on the
        // server. The runtime owns the event bus; this relays its frames.
        const upstream = await fetch(`${HARNESS_PY_URL}/runs/${encodeURIComponent(id)}/stream`, {
          headers: { Accept: "text/event-stream" },
        });
        if (upstream.ok && upstream.body) {
          const reader = upstream.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            const text = decoder.decode(value, { stream: true });
            controller.enqueue(encoder.encode(text));
            buffer += text;
            if (buffer.includes("event: run.completed") || buffer.includes("event: run.failed")) {
              finished = true;
            }
          }
        }
      } catch (err) {
        send("client.error", { message: err instanceof Error ? err.message : String(err) });
      }

      // Fetch the terminal result so the answer bubble has the final text.
      try {
        const run = await getRunFromRuntime(id);
        if (run) {
          send("client.final", {
            runId: id,
            status: run.status,
            output: run.output,
            error: run.error,
            provider: run.provider ?? null,
            model: run.model ?? null,
            // Authoritative usage/steps so the Agent Workspace Context panel can
            // fold the record into the live run (the harness events alone do not
            // carry provider/model/tokens on every path).
            promptTokens: run.promptTokens,
            completionTokens: run.completionTokens,
            totalTokens: run.totalTokens,
            modelSteps: run.modelSteps,
            toolCalls: run.toolCalls,
            toolErrors: run.toolErrors,
            research: run.research ?? null,
            memory: run.memory ?? null,
            context: run.context ?? null,
            codeVerification: run.codeVerification ?? null,
            autonomy: run.autonomy ?? null,
            workspaceId: run.workspaceId ?? null,
            capability: run.capability ?? null,
            vision: run.vision ?? null,
            documents: run.documents ?? null,
            images: run.images ?? null,
            video: run.video ?? null,
          });
          if (chatId && run.output && run.status === "completed") {
            const stamp = new Date().toISOString().replace(/[:.]/g, "-");
            await writeSessionFile(chatId, {
              name: `answer-${stamp}.md`,
              bytes: Buffer.from(run.output, "utf8"),
              kind: "output",
            }).catch(() => undefined);
          }
        } else if (!finished) {
          send("client.error", { message: "run not found after stream ended" });
        }
      } catch (err) {
        send("client.error", { message: err instanceof Error ? err.message : String(err) });
      } finally {
        send("stream.end", {});
        controller.close();
      }
    },
  });

  return new Response(stream, { headers: SSE_HEADERS });
}
