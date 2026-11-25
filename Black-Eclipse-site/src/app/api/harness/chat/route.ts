import { NextResponse } from "next/server";
import { guardExpensiveMutation } from "@/lib/api-guard";
import { chatWithAgent, HarnessError, HarnessRuntimeUnavailable, toHarnessErrorJson, MAX_QUERY_LENGTH } from "@/lib/harness";
import { runtimeIdentityHeaders } from "@/lib/harness/runtime-identity";
import { loadContextDocuments, readSessionWorkspaceRootId, writeSessionFile, readStoredCompaction, writeStoredCompaction, applyStoredCompaction } from "@/lib/chat/session-store";
import { isReasoningLevel } from "@/lib/agent/reasoning";
import { parseCommandInvocation } from "@/lib/harness/commands/command-parser";
import { executeCommandFromText, isRoutedCommand } from "@/lib/harness/commands/command-router";
import { isOperatorRequest } from "@/lib/harness/clients/skills-client";
import { newRunId } from "@/lib/harness/ids";

export const dynamic = "force-dynamic";
export const maxDuration = 180;

// POST /api/harness/chat — the chat surfaces' entry point to the Eclipse Agent.
// Body: { query, history?, chatId? }
//
// Same harness runtime as /api/harness/runs (prompt + tools + persistence), but
// shaped for conversation: it returns the agent's final text directly and has a
// chat-appropriate rate limit rather than the run limit.
//
// When `chatId` is present, the conversation's workspace folder supplies context
// documents (extracted text, injected into the run) and receives the agent's
// final answer as an output file. The Python runtime stays read-only; all
// filesystem work happens here, on the front door.
export async function POST(request: Request) {
  // The run id this request is (or will be) associated with. Declared outside
  // the try so a failure can still hand the caller a handle to the run instead
  // of an orphaned 500.
  let activeRunId: string | undefined;
  const blocked = guardExpensiveMutation(request, "harness-chat", { limit: 60, windowMs: 5 * 60_000 });
  if (blocked) {
    return NextResponse.json(blocked.body, { status: blocked.status, headers: blocked.headers });
  }
  try {
    const body = await request.json().catch(() => ({}));
    const query = typeof (body as Record<string, unknown>).query === "string"
      ? ((body as Record<string, unknown>).query as string).trim()
      : "";
    if (!query) {
      return NextResponse.json({ error: "query is required", code: "INVALID_INPUT" }, { status: 400 });
    }
    // A turn is whatever the person pasted. The cap only bounds one request
    // body — the runtime decides whether a prompt actually fits the model's
    // context window, and says so honestly when it does not.
    if (query.length > MAX_QUERY_LENGTH) {
      return NextResponse.json(
        {
          error: `message is too long for one turn (max ${MAX_QUERY_LENGTH} characters) — send it in smaller parts or attach it as a document`,
          code: "INVALID_INPUT",
        },
        { status: 400 },
      );
    }

    // The conversation BEFORE this turn. Parsed here (not after command
    // handling) because `/compact` needs it, and a compaction must be persisted
    // against the same chat id the run will use.
    const rawHistory = (body as Record<string, unknown>).history;
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
    const chatId = typeof (body as Record<string, unknown>).chatId === "string"
      ? ((body as Record<string, unknown>).chatId as string)
      : "";

    // A routed slash command (`/skills`, `/skill`, `/status`, …) is executed by the
    // shared command router. Profile commands (`/eclipsesdk`) and ordinary prose
    // fall through to the normal agent path; an unknown `/token` falls through too.
    const commandInvocation = parseCommandInvocation(query);
    if (commandInvocation?.spec && isRoutedCommand(commandInvocation.spec)) {
      const cmdResult = await executeCommandFromText(query, {
        operator: isOperatorRequest(request),
        origin: new URL(request.url).origin,
        history,
        chatId: chatId || undefined,
      });
      if (cmdResult) {
        // A real `/compact` result is stored beside the transcript so later
        // runs send the summary instead of the replaced turns.
        if (cmdResult.compaction && chatId) {
          try {
            await writeStoredCompaction(chatId, {
              through: history.length,
              block: cmdResult.compaction.block,
              droppedTurns: cmdResult.compaction.droppedTurns,
              keptTurns: cmdResult.compaction.keptTurns,
            });
          } catch (err) {
            console.warn("[api/harness/chat] could not persist compaction:", err);
          }
        }
        return NextResponse.json({
          success: cmdResult.ok,
          content: cmdResult.output,
          runId: newRunId(),
          command: cmdResult.command,
          ...(cmdResult.ui ? { ui: cmdResult.ui } : {}),
          documentsUsed: [],
        });
      }
    }

    // Phase 23: this conversation's approved project folder. Read SERVER-SIDE
    // (the browser never supplies it), so a chat is bound to a folder the user
    // chose in the picker. An absent binding keeps the existing default.
    const workspaceRootId = chatId
      ? await readSessionWorkspaceRootId(chatId).catch(() => undefined)
      : undefined;
    // Bot chats and group-member turns opt into private per-conversation memory.
    const memoryScope = (body as Record<string, unknown>).memoryScope === "conversation"
      ? ("conversation" as const)
      : undefined;
    const model = typeof (body as Record<string, unknown>).model === "string"
      ? ((body as Record<string, unknown>).model as string).trim()
      : "";
    // A bot chat passes its own brain + pinned provider. Ordinary chat omits
    // both, so the runtime default is unchanged. Bounded before it reaches the
    // preset resolver; an unknown id then fails honestly there.
    const rawAgentId = (body as Record<string, unknown>).agentId;
    const agentId = typeof rawAgentId === "string" && /^[A-Za-z0-9._:-]{1,120}$/.test(rawAgentId.trim())
      ? rawAgentId.trim()
      : undefined;
    const rawModelProvider = (body as Record<string, unknown>).modelProvider;
    const modelProvider = typeof rawModelProvider === "string" && /^[A-Za-z0-9._:-]{1,80}$/.test(rawModelProvider.trim())
      ? rawModelProvider.trim()
      : undefined;
    // Per-question reasoning-effort override ("model intelligence"). Only a
    // value from the shared vocabulary is honoured; anything else is ignored so
    // the runtime falls back to the saved default / task profile.
    const rawEffort = (body as Record<string, unknown>).reasoningEffort;
    const reasoningEffort = isReasoningLevel(rawEffort) ? rawEffort : undefined;
    // The composer's Auto/Quant/Off preference. Only the two explicit values are
    // forwarded; "auto"/absent leaves the runtime's classifier in charge.
    const rawQuantMode = (body as Record<string, unknown>).quantMode;
    const quantMode = rawQuantMode === "on" || rawQuantMode === "off" ? rawQuantMode : undefined;
    // The composer's Auto/Choices/Off preference. Only the explicit values are
    // forwarded; "auto"/absent leaves the base prompt's act-don't-ask guidance.
    const rawChoicesMode = (body as Record<string, unknown>).choicesMode;
    const choicesMode = rawChoicesMode === "on" || rawChoicesMode === "off" ? rawChoicesMode : undefined;
    // The composer's Auto/Plan/Off preference. Only the explicit values are
    // forwarded; "auto"/absent leaves the base guidance in charge.
    const rawPlanMode = (body as Record<string, unknown>).planMode;
    const planMode = rawPlanMode === "on" || rawPlanMode === "off" ? rawPlanMode : undefined;
    // Client-supplied run id so cancellation is possible mid-run; ignored when
    // absent, in which case the server mints one.
    const rawRunId = (body as Record<string, unknown>).runId;
    const runId = typeof rawRunId === "string" && /^run_[A-Za-z0-9-]{1,64}$/.test(rawRunId)
      ? rawRunId
      : undefined;

    // Documents live in the conversation's session folder and are extracted
    // here — never handed to the read-only runtime as files.
    let documents: Array<{ name: string; text: string }> = [];
    if (chatId) {
      try {
        documents = await loadContextDocuments(chatId);
      } catch (err) {
        console.warn("[api/harness/chat] could not load context documents:", err);
      }
    }

    const uiContext = (() => {
      const raw = (body as Record<string, unknown>).uiContext;
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
    const rawProjectId = (body as Record<string, unknown>).projectId;
    const projectId = typeof rawProjectId === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(rawProjectId.trim())
      ? rawProjectId.trim()
      : undefined;

    const identityHeaders = await runtimeIdentityHeaders(request);
    // A stored `/compact` replaces the older turns before they reach the model.
    // The visible transcript is untouched; only the prompt history changes.
    const effectiveHistory = applyStoredCompaction(
      history,
      chatId ? await readStoredCompaction(chatId) : null,
    );
    // One-shot chat is bounded by the front door's `maxDuration` (180 s), but a
    // writable turn is budgeted far longer (80 steps / 2,700 s). Rather than be
    // killed mid-run with no handle, stop WAITING before that ceiling and hand
    // the caller a run id + stream URL; the runtime keeps working.
    const effectiveRunId = runId ?? newRunId();
    activeRunId = effectiveRunId;
    const softDeadlineMs = Number(process.env.HARNESS_CHAT_SOFT_DEADLINE_MS || 150_000);
    const running = { status: "running" as const };
    // Reference images attached to a bot. Shape/size are re-validated centrally
    // by `validateRunInput`; the runtime only ever receives raster data URLs.
    const rawImages = (body as Record<string, unknown>).images;
    const images = Array.isArray(rawImages)
      ? (rawImages as import("@/lib/harness/types").RunImageInput[])
      : undefined;

    const result = await Promise.race([
      chatWithAgent(query, effectiveHistory, documents, effectiveRunId, chatId || undefined, model || undefined, reasoningEffort, identityHeaders, uiContext, memoryScope, projectId, workspaceRootId, quantMode, choicesMode, planMode, agentId, modelProvider, images),
      new Promise<typeof running>((resolve) => {
        const timer = setTimeout(() => resolve(running), Math.max(1, softDeadlineMs));
        // Never keep the process alive for a deadline we stopped waiting on.
        (timer as unknown as { unref?: () => void }).unref?.();
      }),
    ]);
    if (result.status === "running") {
      return NextResponse.json(
        {
          success: true,
          status: "running",
          runId: effectiveRunId,
          streamUrl: `/api/harness/runs/${effectiveRunId}/stream`,
          eventsUrl: `/api/harness/runs/${effectiveRunId}/events`,
          pollAfterMs: 2000,
          content:
            "This run is still working and has outlived the one-shot chat endpoint. " +
            `It continues in the background — follow run ${effectiveRunId} to see the result.`,
        },
        { status: 202 },
      );
    }
    if (result.status === "failed") {
      return NextResponse.json(
        { success: false, error: result.error, runId: result.runId },
        { status: 502 }
      );
    }

    // Persist the agent's answer into the session folder so it is a durable
    // output, not just a bubble. Best-effort: a failure here must not fail the
    // chat, and the output is never fabricated if the write fails.
    if (chatId && result.output) {
      try {
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        await writeSessionFile(chatId, {
          name: `answer-${stamp}.md`,
          bytes: Buffer.from(result.output, "utf8"),
          kind: "output",
        });
      } catch (err) {
        console.warn("[api/harness/chat] could not save output to session folder:", err);
      }
    }

    return NextResponse.json({
      success: true,
      content: result.output,
      runId: result.runId,
      documentsUsed: documents.map((d) => d.name),
    });
  } catch (err) {
    const error = toHarnessErrorJson(err);
    // A synchronous wait deadline is NOT a failed run: it is a still-running
    // run the caller can follow. Use 504 (recoverable) and always include the
    // handle when one exists — never a bare 500.
    const status =
      err instanceof HarnessRuntimeUnavailable ? 503
      : err instanceof HarnessError && err.code === "PROVIDER_NOT_CONFIGURED" ? 503
      : err instanceof HarnessError && err.code === "INVALID_INPUT" ? 400
      : err instanceof HarnessError && /TIMEOUT|DEADLINE/i.test(`${err.code} ${error.message}`) ? 504
      : 500;
    console.error("[api/harness/chat] POST error:", error.message);
    return NextResponse.json(
      {
        error: error.message,
        code: error.code,
        ...(activeRunId
          ? {
              runId: activeRunId,
              streamUrl: `/api/harness/runs/${activeRunId}/stream`,
              eventsUrl: `/api/harness/runs/${activeRunId}/events`,
            }
          : {}),
      },
      // A boot is retryable: tell every client (not only ours) to come back.
      { status, ...(error.code === "RUNTIME_STARTING" ? { headers: { "Retry-After": "3" } } : {}) },
    );
  }
}
