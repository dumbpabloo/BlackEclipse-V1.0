// ── Durable room identity ─────────────────────────────────────────────────────
// `BotGroupMessage` keeps the transcript; this keeps the room that owns it
// (name, members, mode, chair, picture). It is what lets a cleared browser
// rebuild rooms instead of orphaning durable messages.

'use client';

export interface RoomLogEntry {
  id: string;
  name: string;
  memberIds: string[];
  mode?: 'parallel' | 'roundtable';
  leadId?: string | null;
  image?: string | null;
  createdAt: string;
  updatedAt?: string;
}

const REQUEST_TIMEOUT_MS = 6_000;

export async function fetchBotRooms(): Promise<RoomLogEntry[]> {
  if (typeof window === 'undefined') return [];
  try {
    const res = await fetch('/api/bots/rooms', {
      cache: 'no-store',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!res.ok) return [];
    const body = (await res.json()) as { rooms?: RoomLogEntry[] };
    return Array.isArray(body.rooms) ? body.rooms : [];
  } catch {
    return [];
  }
}

export async function upsertBotRoom(room: RoomLogEntry): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  try {
    const res = await fetch('/api/bots/rooms', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room }),
      cache: 'no-store',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function deleteBotRoom(id: string): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  try {
    const res = await fetch(`/api/bots/rooms?id=${encodeURIComponent(id)}`, {
      method: 'DELETE',
      cache: 'no-store',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** Merge server rooms into local rooms. The newer `updatedAt` wins; a server
 *  row with no local twin is restored (the cleared-snapshot case). */
export function mergeRooms<T extends { id: string; updatedAt?: string }>(
  local: readonly T[],
  remote: readonly T[],
): T[] {
  const byId = new Map<string, T>();
  for (const room of local) byId.set(room.id, room);
  for (const room of remote) {
    const current = byId.get(room.id);
    if (!current) {
      byId.set(room.id, room);
      continue;
    }
    const remoteAt = Date.parse(room.updatedAt ?? '');
    const localAt = Date.parse(current.updatedAt ?? '');
    if (!Number.isFinite(localAt) || (Number.isFinite(remoteAt) && remoteAt >= localAt)) {
      byId.set(room.id, { ...current, ...room });
    }
  }
  return [...byId.values()];
}
