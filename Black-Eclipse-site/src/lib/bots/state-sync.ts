// ── Durable bot-state mirror ─────────────────────────────────────────────────
// Bots mode keeps its presentation state (per-bot threads and group rooms) in
// localStorage so the UI is instant and works with the runtime offline. This
// module adds a best-effort SERVER MIRROR on top of that: one JSON document per
// key in JDdS (`BotState`), written from the client after mutations and pulled
// on hydrate. The rule is always "newest client timestamp wins", so a cleared
// browser or a second tab recovers the conversation instead of silently
// starting empty. A runtime/DB failure never loses the local copy.

'use client';

export type BotStateKey = 'chat' | 'groups' | 'queue';

/** The envelope written to localStorage. `version: 2` marks the mirrored form;
 *  a bare legacy document is upgraded on read with an epoch timestamp. */
export interface BotStateEnvelope<T> {
  version: 2;
  /** Client time of the document contents (ISO-8601). */
  updatedAt: string;
  data: T;
}

export interface RemoteBotState {
  document: string;
  updatedAt: string;
  revision: number;
}

const PULL_TIMEOUT_MS = 4_000;
const PUSH_DELAY_MS = 700;

/** Epoch timestamp for legacy (pre-mirror) localStorage documents: a real
 *  server copy should win on upgrade, but local-only data is still pushed when
 *  the server has nothing. */
export const LEGACY_UPDATED_AT = new Date(0).toISOString();

function stateUrl(key: BotStateKey): string {
  return `/api/bots/state/${key}`;
}

/** Read and normalise a localStorage envelope. Returns null when nothing
 *  usable is stored. A legacy raw document is wrapped, never discarded. */
export function readLocalEnvelope<T>(storageKey: string): BotStateEnvelope<T> | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === 'object' && (parsed as { version?: unknown }).version === 2) {
      const envelope = parsed as BotStateEnvelope<T>;
      if (typeof envelope.updatedAt !== 'string' || !('data' in envelope)) return null;
      return envelope;
    }
    // Legacy shape: the document itself was persisted.
    if (parsed && typeof parsed === 'object') {
      return { version: 2, updatedAt: LEGACY_UPDATED_AT, data: parsed as T };
    }
    return null;
  } catch {
    return null;
  }
}

export function writeLocalEnvelope<T>(storageKey: string, envelope: BotStateEnvelope<T>): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(envelope));
  } catch {
    /* quota / private mode — in-memory only */
  }
}

/** True when the server document should replace the local one. */
export function shouldAdoptRemote(
  localUpdatedAt: string | undefined,
  remoteUpdatedAt: string,
): boolean {
  const remote = Date.parse(remoteUpdatedAt);
  if (!Number.isFinite(remote)) return false;
  if (!localUpdatedAt) return true;
  const local = Date.parse(localUpdatedAt);
  if (!Number.isFinite(local)) return true;
  return remote > local;
}

/** True when the local document is strictly newer than the server copy. */
export function shouldPushLocal(localUpdatedAt: string | undefined, remoteUpdatedAt: string): boolean {
  if (!localUpdatedAt) return false;
  const local = Date.parse(localUpdatedAt);
  const remote = Date.parse(remoteUpdatedAt);
  return Number.isFinite(local) && Number.isFinite(remote) && local > remote;
}

/** Pull the mirrored document. Never throws: a missing runtime/DB is the same
 *  as "no server copy", and the local transcript remains authoritative. */
export async function pullBotState(key: BotStateKey): Promise<RemoteBotState | null> {
  if (typeof window === 'undefined') return null;
  try {
    const res = await fetch(stateUrl(key), {
      method: 'GET',
      cache: 'no-store',
      signal: AbortSignal.timeout(PULL_TIMEOUT_MS),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { state?: RemoteBotState | null };
    const state = body?.state;
    if (
      !state
      || typeof state.document !== 'string'
      || typeof state.updatedAt !== 'string'
      || !Number.isFinite(Date.parse(state.updatedAt))
    ) {
      return null;
    }
    return state;
  } catch {
    return null;
  }
}

/** PUT the document now. Returns false on any failure (offline, oversized,
 *  runtime unavailable) — the caller keeps the local copy regardless. */
export async function pushBotState<T>(
  key: BotStateKey,
  envelope: BotStateEnvelope<T>,
): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  try {
    const res = await fetch(stateUrl(key), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        document: JSON.stringify(envelope.data),
        updatedAt: envelope.updatedAt,
      }),
      signal: AbortSignal.timeout(PULL_TIMEOUT_MS * 2),
    });
    return res.ok;
  } catch {
    return false;
  }
}

const pending = new Map<BotStateKey, ReturnType<typeof setTimeout>>();

/** Coalesce rapid mutations (a burst of message appends) into one PUT. */
export function queueBotStatePush<T>(
  key: BotStateKey,
  envelope: BotStateEnvelope<T>,
  delayMs = PUSH_DELAY_MS,
): void {
  if (typeof window === 'undefined') return;
  const existing = pending.get(key);
  if (existing) clearTimeout(existing);
  const timer = setTimeout(() => {
    pending.delete(key);
    void pushBotState(key, envelope);
  }, Math.max(0, delayMs));
  pending.set(key, timer);
}

/** Cancel a queued push (tests / teardown). */
export function cancelBotStatePush(key: BotStateKey): void {
  const existing = pending.get(key);
  if (existing) clearTimeout(existing);
  pending.delete(key);
}

export function newBotStateEnvelope<T>(data: T, now = new Date()): BotStateEnvelope<T> {
  return { version: 2, updatedAt: now.toISOString(), data };
}
