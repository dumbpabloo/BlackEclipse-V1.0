import { NextResponse } from "next/server";
import { guardMutation } from "@/lib/api-guard";
import { prisma } from "@/lib/db";

export const dynamic = "force-dynamic";

// Durable room identity for Bots mode. `BotState` is the instant snapshot; this
// table is what lets a cleared browser rebuild the rooms that own the durable
// transcripts in `BotGroupMessage`.
const MAX_MEMBERS = 50;
const MAX_NAME = 200;
const MAX_IMAGE = 512 * 1024;

interface RoomInput {
  id: string;
  name: string;
  memberIds: string[];
  mode?: string;
  leadId?: string | null;
  image?: string | null;
  createdAt?: string;
}

function badRequest(message: string, status = 400) {
  return NextResponse.json({ error: message }, { status });
}

function parseRoom(value: unknown): RoomInput | null {
  if (!value || typeof value !== "object") return null;
  const room = value as Record<string, unknown>;
  if (typeof room.id !== "string" || !room.id.trim()) return null;
  if (typeof room.name !== "string" || !room.name.trim()) return null;
  if (!Array.isArray(room.memberIds)) return null;
  const memberIds = [...new Set(room.memberIds.filter((id): id is string => typeof id === "string"))];
  if (memberIds.length < 2 || memberIds.length > MAX_MEMBERS) return null;
  const image = typeof room.image === "string" ? room.image : null;
  if (image && image.length > MAX_IMAGE) return null;
  const mode = room.mode === "roundtable" ? "roundtable" : "parallel";
  const leadId = typeof room.leadId === "string" && memberIds.includes(room.leadId)
    ? room.leadId
    : null;
  return {
    id: room.id.slice(0, 120),
    name: room.name.trim().slice(0, MAX_NAME),
    memberIds,
    mode,
    leadId,
    image,
    createdAt: typeof room.createdAt === "string" ? room.createdAt : new Date().toISOString(),
  };
}

function toView(row: {
  id: string;
  name: string;
  memberIds: string;
  mode: string;
  leadId: string | null;
  image: string | null;
  createdAt: string;
  updatedAt: Date;
}) {
  let memberIds: string[] = [];
  try {
    const parsed = JSON.parse(row.memberIds) as unknown;
    if (Array.isArray(parsed)) memberIds = parsed.filter((id): id is string => typeof id === "string");
  } catch {
    memberIds = [];
  }
  return {
    id: row.id,
    name: row.name,
    memberIds,
    mode: row.mode === "roundtable" ? "roundtable" : "parallel",
    leadId: row.leadId,
    image: row.image,
    createdAt: row.createdAt,
    updatedAt: row.updatedAt.toISOString(),
  };
}

export async function GET() {
  try {
    const rows = await prisma.botRoom.findMany({ orderBy: { createdAt: "asc" } });
    return NextResponse.json({ success: true, rooms: rows.map(toView) });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to read rooms", 500);
  }
}

export async function PUT(request: Request) {
  const blocked = guardMutation(request, "bot-rooms");
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return badRequest("invalid JSON body");
  }
  const input = (body ?? {}) as { room?: unknown };
  const room = parseRoom(input.room);
  if (!room) return badRequest("invalid room");

  try {
    const saved = await prisma.botRoom.upsert({
      where: { id: room.id },
      create: {
        id: room.id,
        name: room.name,
        memberIds: JSON.stringify(room.memberIds),
        mode: room.mode ?? "parallel",
        leadId: room.leadId ?? null,
        image: room.image ?? null,
        createdAt: room.createdAt ?? new Date().toISOString(),
      },
      update: {
        name: room.name,
        memberIds: JSON.stringify(room.memberIds),
        mode: room.mode ?? "parallel",
        leadId: room.leadId ?? null,
        image: room.image ?? null,
      },
    });
    return NextResponse.json({ success: true, room: toView(saved) });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to save room", 500);
  }
}

export async function DELETE(request: Request) {
  const blocked = guardMutation(request, "bot-rooms");
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });
  const id = new URL(request.url).searchParams.get("id")?.trim();
  if (!id) return badRequest("id is required");
  try {
    await prisma.botRoom.deleteMany({ where: { id } });
    return NextResponse.json({ success: true });
  } catch (err) {
    return badRequest(err instanceof Error ? err.message : "failed to delete room", 500);
  }
}
