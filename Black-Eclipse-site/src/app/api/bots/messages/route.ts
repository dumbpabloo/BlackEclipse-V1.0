import { NextResponse } from "next/server";
import { guardMutation } from "@/lib/api-guard";
import { prisma } from "@/lib/db";

export const dynamic = "force-dynamic";

// Per-message durable log for Bots mode. The JSON `BotState` document is still
// the instant-hydration snapshot; this route is the recovery/audit record that
// survives a cleared browser or a lost snapshot. Message ids are client-owned,
// so an append is an idempotent upsert.
type Kind = "chat" | "groups";

const MAX_CONTENT = 200_000;
const MAX_LIMIT = 5_000;

function badRequest(message: string, status = 400) {
  return NextResponse.json({ error: message }, { status });
}

function parseKind(value: unknown): Kind | null {
  return value === "chat" || value === "groups" ? value : null;
}

function isMessage(value: unknown): value is {
  id: string;
  role: "user" | "assistant";
  content: string;
  at: string;
  scheduled?: boolean;
  runId?: string | null;
  error?: boolean;
  feedback?: "up" | "down" | null;
  authorId?: string | null;
  authorName?: string | null;
  threadId?: string | null;
  payload?: unknown;
} {
  if (!value || typeof value !== "object") return false;
  const m = value as Record<string, unknown>;
  return (
    typeof m.id === "string"
    && (m.role === "user" || m.role === "assistant")
    && typeof m.content === "string"
    && typeof m.at === "string"
    && m.content.length <= MAX_CONTENT
  );
}

function payloadJson(value: unknown): string | null {
  if (value == null) return null;
  try {
    return JSON.stringify(value);
  } catch {
    return null;
  }
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const kind = parseKind(url.searchParams.get("kind"));
  const scopeId = url.searchParams.get("scopeId")?.trim() || null;
  if (!kind) return badRequest("kind must be chat or groups");
  const rawLimit = Number(url.searchParams.get("limit") ?? 2000);
  const limit = Number.isFinite(rawLimit)
    ? Math.max(1, Math.min(MAX_LIMIT, Math.trunc(rawLimit)))
    : 2000;

  try {
    // No scopeId = every conversation (used to recover after local storage was
    // cleared). A scopeId keeps the read bounded to one bot/room.
    const rows = kind === "chat"
      ? await prisma.botChatMessage.findMany({
        where: scopeId ? { botId: scopeId } : undefined,
        orderBy: { at: "asc" },
        take: limit,
      })
      : await prisma.botGroupMessage.findMany({
        where: scopeId ? { groupId: scopeId } : undefined,
        orderBy: { at: "asc" },
        take: limit,
      });
    return NextResponse.json({ success: true, messages: rows });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to read messages", 500);
  }
}

export async function POST(request: Request) {
  const blocked = guardMutation(request, "bot-messages");
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return badRequest("invalid JSON body");
  }
  const input = (body ?? {}) as { kind?: unknown; scopeId?: unknown; message?: unknown };
  const kind = parseKind(input.kind);
  const scopeId = typeof input.scopeId === "string" ? input.scopeId.trim() : "";
  if (!kind) return badRequest("kind must be chat or groups");
  if (!scopeId) return badRequest("scopeId is required");
  if (!isMessage(input.message)) return badRequest("invalid message");
  const message = input.message;

  const common = {
    role: message.role,
    content: message.content,
    at: message.at,
    scheduled: Boolean(message.scheduled),
    runId: message.runId ?? null,
    error: Boolean(message.error),
    feedback: message.feedback ?? null,
    payload: payloadJson(message.payload),
  };

  try {
    if (kind === "chat") {
      await prisma.botChatMessage.upsert({
        where: { id: message.id },
        create: {
          id: message.id,
          botId: scopeId,
          threadId: message.threadId || "main",
          ...common,
        },
        update: {
          ...common,
          ...(message.threadId ? { threadId: message.threadId } : {}),
          revision: { increment: 1 },
        },
      });
    } else {
      await prisma.botGroupMessage.upsert({
        where: { id: message.id },
        create: {
          id: message.id,
          groupId: scopeId,
          ...common,
          authorId: message.authorId ?? null,
          authorName: message.authorName ?? null,
        },
        update: {
          ...common,
          authorId: message.authorId ?? null,
          authorName: message.authorName ?? null,
          revision: { increment: 1 },
        },
      });
    }
    return NextResponse.json({ success: true });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to save message", 500);
  }
}

export async function PUT(request: Request) {
  const blocked = guardMutation(request, "bot-messages");
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return badRequest("invalid JSON body");
  }
  const input = (body ?? {}) as {
    kind?: unknown;
    scopeId?: unknown;
    messages?: unknown;
    threadId?: unknown;
  };
  const kind = parseKind(input.kind);
  const scopeId = typeof input.scopeId === "string" ? input.scopeId.trim() : "";
  const threadId = typeof input.threadId === "string" && input.threadId.trim()
    ? input.threadId.trim().slice(0, 120)
    : null;
  if (!kind) return badRequest("kind must be chat or groups");
  if (!scopeId) return badRequest("scopeId is required");
  if (!Array.isArray(input.messages)) return badRequest("messages must be an array");
  const messages = input.messages.filter(isMessage);
  if (messages.length !== input.messages.length) return badRequest("invalid message");
  if (messages.length > MAX_LIMIT) return badRequest("too many messages", 413);

  try {
    await prisma.$transaction(async (tx) => {
      if (kind === "chat") {
        await tx.botChatMessage.deleteMany({
          where: {
            botId: scopeId,
            ...(threadId === "main"
              ? { OR: [{ threadId: "main" }, { threadId: null }] }
              : threadId ? { threadId } : {}),
          },
        });
        for (const message of messages) {
          await tx.botChatMessage.create({
            data: {
              id: message.id,
              botId: scopeId,
              threadId: threadId || message.threadId || "main",
              role: message.role,
              content: message.content,
              at: message.at,
              scheduled: Boolean(message.scheduled),
              runId: message.runId ?? null,
              error: Boolean(message.error),
              feedback: message.feedback ?? null,
              payload: payloadJson(message.payload),
            },
          });
        }
      } else {
        await tx.botGroupMessage.deleteMany({ where: { groupId: scopeId } });
        for (const message of messages) {
          await tx.botGroupMessage.create({
            data: {
              id: message.id,
              groupId: scopeId,
              role: message.role,
              content: message.content,
              at: message.at,
              scheduled: Boolean(message.scheduled),
              runId: message.runId ?? null,
              error: Boolean(message.error),
              feedback: message.feedback ?? null,
              payload: payloadJson(message.payload),
              authorId: message.authorId ?? null,
              authorName: message.authorName ?? null,
            },
          });
        }
      }
    });
    return NextResponse.json({ success: true });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to replace messages", 500);
  }
}

export async function DELETE(request: Request) {
  const blocked = guardMutation(request, "bot-messages");
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });

  const url = new URL(request.url);
  const kind = parseKind(url.searchParams.get("kind"));
  const scopeId = url.searchParams.get("scopeId")?.trim();
  if (!kind) return badRequest("kind must be chat or groups");
  if (!scopeId) return badRequest("scopeId is required");

  try {
    if (kind === "chat") {
      await prisma.botChatMessage.deleteMany({ where: { botId: scopeId } });
    } else {
      await prisma.botGroupMessage.deleteMany({ where: { groupId: scopeId } });
    }
    return NextResponse.json({ success: true });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to clear messages", 500);
  }
}
