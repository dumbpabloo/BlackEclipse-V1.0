// ── Durable per-message log ──────────────────────────────────────────────────
// `BotState` is the instant-hydration snapshot; this is the recovery/audit
// record behind it. Every append/terminal update is mirrored here, and hydrate
// merges the server rows back into the local store. Message ids are the
// client's ids, so an append is idempotent and a streamed final answer upserts
// the same row.

'use client';

export type BotMessageLogKind = 'chat' | 'groups';

export interface LoggedMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  at: string;
  scheduled?: boolean;
  runId?: string | null;
  error?: boolean;
  feedback?: 'up' | 'down' | null;
  authorId?: string | null;
  authorName?: string | null;
  /** Bot chat side thread id (`main` or a branch id). */
  threadId?: string | null;
  /** Structured UI extras (delegations, pending approval/clarification). */
  payload?: unknown;
}

export interface MessageLogRow extends Omit<LoggedMessage, 'payload'> {
  botId?: string;
  groupId?: string;
  /** JSON string as stored in SQLite. */
  payload?: string | null;
  revision?: number;
  updatedAt?: string;
}

const REQUEST_TIMEOUT_MS = 6_000;

function base(kind: BotMessageLogKind): string {
  return `/api/bots/messages?kind=${encodeURIComponent(kind)}`;
}

function noStore(init: RequestInit = {}): RequestInit {
  return { ...init, cache: 'no-store' };
}

export async function appendBotMessage(
  kind: BotMessageLogKind,
  scopeId: string,
  message: LoggedMessage,
  payload?: unknown,
): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  try {
    const res = await fetch('/api/bots/messages', noStore({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind,
        scopeId,
        message: { ...message, payload: payload ?? message.payload },
      }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    }));
    return res.ok;
  } catch {
    return false;
  }
}

/** Replace the whole log for one bot/room (used by clear, truncate/regenerate). */
export async function replaceBotMessages(
  kind: BotMessageLogKind,
  scopeId: string,
  messages: LoggedMessage[],
  payloadFor?: (message: LoggedMessage) => unknown,
  threadId?: string,
): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  try {
    const res = await fetch('/api/bots/messages', noStore({
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind,
        scopeId,
        ...(threadId ? { threadId } : {}),
        messages: messages.map((m) => ({
          ...m,
          payload: payloadFor ? payloadFor(m) : m.payload,
        })),
      }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS * 2),
    }));
    return res.ok;
  } catch {
    return false;
  }
}

export async function clearBotMessages(
  kind: BotMessageLogKind,
  scopeId: string,
): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  try {
    const res = await fetch(`${base(kind)}&scopeId=${encodeURIComponent(scopeId)}`, noStore({
      method: 'DELETE',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    }));
    return res.ok;
  } catch {
    return false;
  }
}

/** Read the durable log. `scopeId` may be omitted to recover every thread. */
export async function fetchBotMessages(
  kind: BotMessageLogKind,
  scopeId?: string,
): Promise<MessageLogRow[]> {
  if (typeof window === 'undefined') return [];
  const scope = scopeId ? `&scopeId=${encodeURIComponent(scopeId)}` : '';
  try {
    const res = await fetch(`${base(kind)}${scope}&limit=5000`, noStore({
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS * 2),
    }));
    if (!res.ok) return [];
    const body = (await res.json()) as { messages?: MessageLogRow[] };
    return Array.isArray(body.messages) ? body.messages : [];
  } catch {
    return [];
  }
}

/** JSON payload → the object a store renders (never throws). */
export function decodePayload(payload: string | null | undefined): Record<string, unknown> | undefined {
  if (!payload) return undefined;
  try {
    const parsed = JSON.parse(payload) as unknown;
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Merge the durable log into the local list. A server row wins on the same id
 * (it is the latest terminal write), local-only rows are kept, and the result is
 * ordered by `at`. Pure so the merge cannot depend on React or the network.
 */
export function mergeMessageLog<T extends { id: string; at: string }>(
  local: readonly T[],
  remote: readonly T[],
): T[] {
  const byId = new Map<string, T>();
  for (const message of local) byId.set(message.id, message);
  for (const message of remote) byId.set(message.id, message);
  return [...byId.values()].sort((a, b) => {
    const left = Date.parse(a.at);
    const right = Date.parse(b.at);
    if (Number.isFinite(left) && Number.isFinite(right) && left !== right) return left - right;
    return a.id.localeCompare(b.id);
  });
}
