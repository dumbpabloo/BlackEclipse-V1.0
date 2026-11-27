// ── Same-origin tab coordination ────────────────────────────────────────────
//
// One chat can be open in several tabs. They must agree on WHO starts a run,
// while every tab can still display it. The run itself stays server-side, so a
// follower never needs to start a second run; it only needs the run id and
// snapshot.
//
// Web Locks provide the owner election. BroadcastChannel makes snapshots and
// lifecycle events immediate; localStorage keeps the shared record durable
// across a reload and is also the fallback election when Web Locks is absent.

'use client';

import type { AssistantTurn } from './response-model';

const SHARED_RUNS_KEY = 'eclipse.sharedRuns.v1';
/** v3: ONE localStorage key per conversation, so two tabs never clobber each
 *  other's record with a read-modify-write of a shared map. */
const SHARED_RUN_KEY_PREFIX = 'eclipse.sharedRun.v3:';
const CHANNEL_NAME = 'eclipse.chat.v1';
const PENDING_RUN = '__pending__';
// A background tab's timers are throttled; keep the record live long enough
// that a real crash (not throttling) is what expires it.
const LIVE_WINDOW_MS = 90_000;

export interface SharedRun {
  chatId: string;
  runId: string;
  ownerTabId: string;
  at: number;
  status?: string;
}

export type TabEvent =
  | { type: 'run.started'; chatId: string; runId: string; ownerTabId: string }
  | {
      type: 'run.snapshot';
      chatId: string;
      runId: string;
      ownerTabId: string;
      turn: AssistantTurn;
    }
  | {
      type: 'run.done';
      chatId: string;
      runId: string;
      ownerTabId: string;
      status: string;
    };

type Listener = (event: TabEvent) => void;

const TAB_ID =
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `tab-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

export function currentTabId(): string {
  return TAB_ID;
}

function localStore(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

let channel: BroadcastChannel | null = null;
function getChannel(): BroadcastChannel | null {
  if (typeof window === 'undefined' || typeof BroadcastChannel === 'undefined') return null;
  if (!channel) channel = new BroadcastChannel(CHANNEL_NAME);
  return channel;
}

export function readSharedRuns(): Record<string, SharedRun> {
  try {
    const raw = localStore()?.getItem(SHARED_RUNS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as { runs?: Record<string, SharedRun> };
    return parsed?.runs && typeof parsed.runs === 'object' ? parsed.runs : {};
  } catch {
    return {};
  }
}

function runKey(chatId: string): string {
  return `${SHARED_RUN_KEY_PREFIX}${chatId}`;
}

function readSharedRun(chatId: string): SharedRun | null {
  try {
    const raw = localStore()?.getItem(runKey(chatId));
    if (!raw) {
      // One-time migration from the shared map. Writing the per-chat key on the
      // next put means the old map is only read, never rewritten.
      return readSharedRuns()[chatId] ?? null;
    }
    const parsed = JSON.parse(raw) as SharedRun;
    return parsed && typeof parsed.chatId === 'string' ? parsed : null;
  } catch {
    return null;
  }
}

export function putSharedRun(run: SharedRun): void {
  try {
    localStore()?.setItem(runKey(run.chatId), JSON.stringify(run));
  } catch {
    /* shared coordination is best-effort */
  }
}

export function removeSharedRun(chatId: string, runId?: string): void {
  try {
    const store = localStore();
    if (!store) return;
    const current = readSharedRun(chatId);
    if (runId && current && current.runId !== runId) return;
    store.removeItem(runKey(chatId));
    // Also drop the legacy map entry so a stale v1 record cannot resurrect.
    const legacy = readSharedRuns();
    if (legacy[chatId]) {
      delete legacy[chatId];
      if (Object.keys(legacy).length === 0) store.removeItem(SHARED_RUNS_KEY);
      else store.setItem(SHARED_RUNS_KEY, JSON.stringify({ runs: legacy }));
    }
  } catch {
    /* shared coordination is best-effort */
  }
}

export function getSharedRun(chatId: string | null | undefined): SharedRun | null {
  if (!chatId) return null;
  const run = readSharedRun(chatId);
  if (!run) return null;
  return Date.now() - run.at <= LIVE_WINDOW_MS ? run : null;
}

export function isSharedRunLive(chatId: string | null | undefined): boolean {
  return getSharedRun(chatId) !== null;
}

export function publishTabEvent(event: TabEvent): void {
  try {
    getChannel()?.postMessage(event);
  } catch {
    /* a closed channel must not break a run */
  }
}

export function subscribeTabEvents(listener: Listener): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const onMessage = (event: MessageEvent<TabEvent>) => {
    if (event.data && typeof event.data.type === 'string') listener(event.data);
  };
  const onStorage = (event: StorageEvent) => {
    if (event.key !== SHARED_RUNS_KEY && !(event.key ?? '').startsWith(SHARED_RUN_KEY_PREFIX)) return;
    // localStorage is the durable fallback; a snapshot broadcast carries the
    // richer turn, so a storage-only change is only a lifecycle hint.
  };
  const ch = getChannel();
  ch?.addEventListener('message', onMessage);
  window.addEventListener('storage', onStorage);
  return () => {
    ch?.removeEventListener('message', onMessage);
    window.removeEventListener('storage', onStorage);
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Run `fn` only if this tab wins the per-chat lock. A follower gets
 * `{ acquired: false }` and should attach to the shared run instead.
 */
export async function withChatStartLock<T>(
  chatId: string,
  fn: () => Promise<T>,
): Promise<{ acquired: boolean; value?: T }> {
  const locks = typeof navigator !== 'undefined'
    ? (navigator as Navigator & { locks?: LockManager }).locks
    : undefined;
  if (locks?.request) {
    // The DOM lib models the callback's return as T, not `Promise<T>`; the
    // platform awaits an async callback. Cast through unknown for that seam.
    return (await locks.request(
      `eclipse-chat:${chatId}`,
      { ifAvailable: true },
      async (lock) => {
        if (!lock) return { acquired: false } as { acquired: boolean; value?: T };
        return { acquired: true, value: await fn() } as { acquired: boolean; value?: T };
      },
    )) as unknown as { acquired: boolean; value?: T };
  }

  // No shared storage means there is no second tab to coordinate with.
  if (!localStore()) return { acquired: true, value: await fn() };

  // Fallback election (no Web Locks): a short-lived localStorage claim.
  const existing = getSharedRun(chatId);
  if (existing && existing.ownerTabId !== TAB_ID) return { acquired: false };
  putSharedRun({
    chatId,
    runId: PENDING_RUN,
    ownerTabId: TAB_ID,
    at: Date.now(),
    status: 'starting',
  });
  await sleep(12);
  const claimed = getSharedRun(chatId);
  if (!claimed || claimed.ownerTabId !== TAB_ID) return { acquired: false };
  try {
    return { acquired: true, value: await fn() };
  } finally {
    removeSharedRun(chatId, PENDING_RUN);
  }
}

export { PENDING_RUN, SHARED_RUNS_KEY, SHARED_RUN_KEY_PREFIX };
