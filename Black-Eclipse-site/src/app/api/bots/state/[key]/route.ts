import { NextResponse } from "next/server";
import { guardMutation } from "@/lib/api-guard";
import { prisma } from "@/lib/db";

export const dynamic = "force-dynamic";

// Bot mode's durable presentation state. Two documents only — the per-bot chat
// threads and the group rooms (members + transcripts). The UI hydrates from
// localStorage first for instant paint, then adopts the server copy when it is
// newer. The server is the CONFLICT authority: a stale document (an older
// stateUpdatedAt) is refused instead of overwriting a newer one, so an old tab
// or a delayed push can never roll the conversation back.
const ALLOWED_KEYS = new Set(["chat", "groups", "queue"]);

// A transcript document is JSON and grows with use. 8 MB is generous for a
// local single-user app while keeping one request bounded.
const MAX_DOCUMENT_BYTES = 8 * 1024 * 1024;

function badRequest(message: string, status = 400) {
  return NextResponse.json({ error: message }, { status });
}

function isIsoTimestamp(value: unknown): value is string {
  if (typeof value !== "string") return false;
  return Number.isFinite(Date.parse(value));
}

export async function GET(
  _request: Request,
  ctx: { params: Promise<{ key: string }> },
) {
  const { key } = await ctx.params;
  if (!ALLOWED_KEYS.has(key)) return badRequest(`unknown bot state key ${key}`, 404);
  try {
    const row = await prisma.botState.findUnique({ where: { key } });
    if (!row) return NextResponse.json({ success: true, state: null });
    return NextResponse.json({
      success: true,
      state: {
        document: row.document,
        updatedAt: row.stateUpdatedAt,
        revision: row.revision,
      },
    });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to read bot state", 500);
  }
}

export async function PUT(
  request: Request,
  ctx: { params: Promise<{ key: string }> },
) {
  const blocked = guardMutation(request, "bot-state");
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });

  const { key } = await ctx.params;
  if (!ALLOWED_KEYS.has(key)) return badRequest(`unknown bot state key ${key}`, 404);

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return badRequest("invalid JSON body");
  }
  const { document, updatedAt } = (body ?? {}) as {
    document?: unknown;
    updatedAt?: unknown;
  };
  if (typeof document !== "string") return badRequest("document must be a JSON string");
  if (Buffer.byteLength(document, "utf8") > MAX_DOCUMENT_BYTES) {
    return badRequest("bot state document is too large", 413);
  }
  if (!isIsoTimestamp(updatedAt)) return badRequest("updatedAt must be an ISO timestamp");

  try {
    const existing = await prisma.botState.findUnique({ where: { key } });
    if (
      existing
      && Date.parse(updatedAt) < Date.parse(existing.stateUpdatedAt)
    ) {
      // A newer document already exists. Return it so the caller can adopt it;
      // never regress the durable transcript to an older client snapshot.
      return NextResponse.json(
        {
          success: false,
          error: "stale bot state",
          state: {
            document: existing.document,
            updatedAt: existing.stateUpdatedAt,
            revision: existing.revision,
          },
        },
        { status: 409 },
      );
    }
    const row = await prisma.botState.upsert({
      where: { key },
      create: {
        key,
        document,
        stateUpdatedAt: updatedAt,
        revision: 1,
      },
      update: {
        document,
        stateUpdatedAt: updatedAt,
        revision: { increment: 1 },
      },
    });
    return NextResponse.json({
      success: true,
      state: {
        document: row.document,
        updatedAt: row.stateUpdatedAt,
        revision: row.revision,
      },
    });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to save bot state", 500);
  }
}
