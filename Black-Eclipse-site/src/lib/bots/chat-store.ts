// ── Bot chat store ───────────────────────────────────────────────────────────
// Per-bot conversation threads. localStorage gives an instant, offline-first
// render; the document is also mirrored to the server (`BotState`, key `chat`)
// so clearing browser storage or opening a second tab does not erase the
// threads. The newest client timestamp wins. Bots run real agent turns through
// the streaming agent provider (`getAgentProvider`), so a long tool-heavy run is
// not bound by the one-shot front door's 180s request cap.

'use client';

import { useEffect } from 'react';
import { create } from 'zustand';
import type { DelegationView, PendingApproval, PendingClarification } from '@/lib/agent/response-model';
import {
  newBotStateEnvelope,
  pullBotState,
  queueBotStatePush,
  readLocalEnvelope,
  shouldAdoptRemote,
  shouldPushLocal,
  writeLocalEnvelope,
  type BotStateEnvelope,
} from '@/lib/bots/state-sync';
import {
  appendBotMessage,
  clearBotMessages,
  decodePayload,
  fetchBotMessages,
  mergeMessageLog,
  replaceBotMessages,
  type MessageLogRow,
} from '@/lib/bots/message-log';

export const BOT_CHAT_KEY = 'be_bot_chat_v1';

export interface BotMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  at: string;
  /** True when the assistant turn failed (shown as an honest error bubble). */
  error?: boolean;
  /** Optional thumbs feedback on an assistant turn. */
  feedback?: 'up' | 'down';
  /** True when this assistant turn came from an AUTONOMOUS run (schedule, event,
   *  condition or manual "Run now") rather than the interactive composer. Such a
   *  turn is tagged in the thread and is not editable/regenerable. */
  scheduled?: boolean;
  /** The execution id that produced this turn — the idempotency key so an
   *  autonomous answer is appended exactly once (across polls and reloads). */
  runId?: string;
  /** Parallel fan-outs this bot ran, so the thread can show the live agent card. */
  delegations?: DelegationView[];
  /** A blocking permission decision raised by this turn. Rendered by the bot
   *  thread so an approval-gated tool can finish here, exactly as in main chat. */
  pendingApproval?: PendingApproval;
  /** A paused `clarify` question from this turn. */
  pendingClarification?: PendingClarification;
}

interface BotChatStore {
  byBot: Record<string, BotMessage[]>;
  hydrated: boolean;
  hydrate: () => void;
  setMessages: (botId: string, messages: BotMessage[]) => void;
  append: (botId: string, message: BotMessage) => void;
  /** Append an autonomous run's answer ONCE. Returns false when that execution
   *  is already in the thread (deduped by `runId`), true when it was added. */
  appendScheduled: (botId: string, message: BotMessage & { runId: string }) => boolean;
  /** Patch one message in place — used to fill the live streaming reply. A
   *  streaming caller passes `persist=false`; the terminal patch persists. */
  update: (botId: string, id: string, patch: Partial<BotMessage>, persist?: boolean) => void;
  clear: (botId: string) => void;
}

/** Persist locally and queue the server mirror (best effort). */
function persist(
  byBot: Record<string, BotMessage[]>,
  envelope?: BotStateEnvelope<Record<string, BotMessage[]>>,
) {
  const next = envelope ?? newBotStateEnvelope(byBot);
  writeLocalEnvelope(BOT_CHAT_KEY, next);
  queueBotStatePush('chat', next);
}

/** Validate a decoded document from either storage medium. */
function sanitizeByBot(input: unknown): Record<string, BotMessage[]> {
  const byBot: Record<string, BotMessage[]> = {};
  if (input && typeof input === 'object') {
    for (const [id, list] of Object.entries(input as Record<string, unknown>)) {
      if (Array.isArray(list)) byBot[id] = list.filter(isMessage);
    }
  }
  return byBot;
}

function isMessage(v: unknown): v is BotMessage {
  if (!v || typeof v !== 'object') return false;
  const m = v as Record<string, unknown>;
  return (
    typeof m.id === 'string' &&
    (m.role === 'user' || m.role === 'assistant') &&
    typeof m.content === 'string'
  );
}

/** Structured extras that are not columns of the message log. */
function messagePayload(message: BotMessage): Record<string, unknown> | undefined {
  const payload: Record<string, unknown> = {};
  if (message.delegations?.length) payload.delegations = message.delegations;
  if (message.pendingApproval) payload.pendingApproval = message.pendingApproval;
  if (message.pendingClarification) payload.pendingClarification = message.pendingClarification;
  return Object.keys(payload).length ? payload : undefined;
}

/** Rebuild a UI message from a durable log row (payload is JSON text). */
function messageFromRow(row: MessageLogRow): BotMessage {
  const payload = decodePayload(row.payload);
  return {
    id: row.id,
    role: row.role,
    content: row.content,
    at: row.at,
    ...(row.scheduled ? { scheduled: true } : {}),
    ...(row.runId ? { runId: row.runId } : {}),
    ...(row.error ? { error: true } : {}),
    ...(row.feedback ? { feedback: row.feedback } : {}),
    ...(payload?.delegations ? { delegations: payload.delegations as BotMessage['delegations'] } : {}),
    ...(payload?.pendingApproval
      ? { pendingApproval: payload.pendingApproval as BotMessage['pendingApproval'] }
      : {}),
    ...(payload?.pendingClarification
      ? { pendingClarification: payload.pendingClarification as BotMessage['pendingClarification'] }
      : {}),
  };
}

function logRowToMessage(row: MessageLogRow): BotMessage {
  return messageFromRow(row);
}

export const useBotChatStore = create<BotChatStore>((set, get) => {
  /** Reconcile with the durable snapshot AND the per-message log without
   *  blocking the local render. The log is the recovery source of truth: it
   *  restores a thread even when the snapshot is missing or behind. */
  const syncFromServer = async () => {
    const remote = await pullBotState('chat');
    const local = readLocalEnvelope<Record<string, BotMessage[]>>(BOT_CHAT_KEY);
    if (!remote) {
      if (local) queueBotStatePush('chat', local);
    } else if (shouldAdoptRemote(local?.updatedAt, remote.updatedAt)) {
      try {
        const data = sanitizeByBot(JSON.parse(remote.document) as unknown);
        writeLocalEnvelope(BOT_CHAT_KEY, newBotStateEnvelope(data, new Date(remote.updatedAt)));
        set({ byBot: data });
      } catch {
        /* malformed mirror — keep the local copy */
      }
    } else if (local && shouldPushLocal(local.updatedAt, remote.updatedAt)) {
      queueBotStatePush('chat', local);
    }

    const rows = await fetchBotMessages('chat');
    if (rows.length === 0) return;
    const byBot = { ...get().byBot };
    const grouped = new Map<string, BotMessage[]>();
    for (const row of rows) {
      if (!row.botId) continue;
      const list = grouped.get(row.botId) ?? [];
      list.push(logRowToMessage(row));
      grouped.set(row.botId, list);
    }
    for (const [botId, remoteMessages] of grouped) {
      byBot[botId] = mergeMessageLog(byBot[botId] ?? [], remoteMessages);
    }
    if (grouped.size > 0) {
      persist(byBot);
      set({ byBot });
    }
  };

  return {
    byBot: {},
    hydrated: false,

    hydrate: () => {
      const local = readLocalEnvelope<Record<string, BotMessage[]>>(BOT_CHAT_KEY);
      set({ byBot: sanitizeByBot(local?.data), hydrated: true });
      void syncFromServer();
    },

    setMessages: (botId, messages) => {
      const byBot = { ...get().byBot, [botId]: messages };
      persist(byBot);
      set({ byBot });
      // Truncate/regenerate must truncate the durable log too, or the next
      // hydrate would resurrect the removed tail.
      void replaceBotMessages('chat', botId, messages, (m) => messagePayload(m as BotMessage));
    },

    append: (botId, message) => {
      const byBot = { ...get().byBot, [botId]: [...(get().byBot[botId] ?? []), message] };
      persist(byBot);
      set({ byBot });
      void appendBotMessage('chat', botId, message, messagePayload(message));
    },

    appendScheduled: (botId, message) => {
      const list = get().byBot[botId] ?? [];
      // Idempotent by execution id: a re-poll (or a reload that re-syncs history)
      // must never double-post the same autonomous answer.
      if (message.runId && list.some((m) => m.runId === message.runId)) return false;
      const byBot = { ...get().byBot, [botId]: [...list, message] };
      persist(byBot);
      set({ byBot });
      void appendBotMessage('chat', botId, message, messagePayload(message));
      return true;
    },

    update: (botId, id, patch, persistNow = true) => {
      const list = get().byBot[botId];
      if (!list) return;
      const updated = list.map((m) => (m.id === id ? { ...m, ...patch } : m));
      const byBot = { ...get().byBot, [botId]: updated };
      if (persistNow) {
        persist(byBot);
        const message = updated.find((m) => m.id === id);
        if (message) void appendBotMessage('chat', botId, message, messagePayload(message));
      }
      set({ byBot });
    },

    clear: (botId) => {
      const next = { ...get().byBot };
      delete next[botId];
      persist(next);
      set({ byBot: next });
      void clearBotMessages('chat', botId);
    },
  };
});

export function useHydrateBotChat() {
  const hydrate = useBotChatStore((s) => s.hydrate);
  const hydrated = useBotChatStore((s) => s.hydrated);
  useEffect(() => {
    if (!hydrated) hydrate();
  }, [hydrated, hydrate]);
  // Adopt a newer document written by another tab.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === BOT_CHAT_KEY) hydrate();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [hydrate]);
}
