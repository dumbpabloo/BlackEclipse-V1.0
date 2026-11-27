// ── Bot chat store ───────────────────────────────────────────────────────────
// Per-bot conversation threads. The main thread is the bot's canonical chat;
// side threads are branches created from a message. localStorage gives an
// instant, offline-first render; the document is also mirrored to the server
// (`BotState`, key `chat`) and every message has a durable row. Bots run real
// agent turns through the streaming provider, so a long tool-heavy run is not
// bound by the one-shot front door's 180s request cap.

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
  decodePayload,
  fetchBotMessages,
  mergeMessageLog,
  replaceBotMessages,
  type MessageLogRow,
} from '@/lib/bots/message-log';

export const BOT_CHAT_KEY = 'be_bot_chat_v1';
export const MAIN_THREAD = 'main';

export interface BotThread {
  id: string;
  title: string;
  createdAt: string;
  /** The thread this one was branched from, when it is a branch. */
  parentThreadId?: string;
  /** The message the branch copied through. */
  branchFromMessageId?: string;
}

export interface BotMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  at: string;
  /** True when the assistant turn failed (shown as an honest error bubble). */
  error?: boolean;
  /** Optional thumbs feedback on an assistant turn. */
  feedback?: 'up' | 'down';
  /** General reactions on a message (emoji → count). */
  reactions?: Record<string, number>;
  /** True when this assistant turn came from an AUTONOMOUS run. */
  scheduled?: boolean;
  /** The execution id that produced this turn — the idempotency key. */
  runId?: string;
  /** Side thread this message belongs to (`main` by default). */
  threadId?: string;
  /** Parallel fan-outs this bot ran. */
  delegations?: DelegationView[];
  /** A blocking permission decision raised by this turn. */
  pendingApproval?: PendingApproval;
  /** A paused `clarify` question from this turn. */
  pendingClarification?: PendingClarification;
}

interface PersistedChat {
  byBot?: Record<string, BotMessage[]>;
  threadsByBot?: Record<string, BotThread[]>;
  messagesByThread?: Record<string, BotMessage[]>;
  activeThreadByBot?: Record<string, string>;
}

interface BotChatStore {
  /** Active thread's messages (kept for every existing consumer). */
  byBot: Record<string, BotMessage[]>;
  threadsByBot: Record<string, BotThread[]>;
  messagesByThread: Record<string, BotMessage[]>;
  activeThreadByBot: Record<string, string>;
  hydrated: boolean;
  hydrate: () => void;
  setMessages: (botId: string, messages: BotMessage[]) => void;
  append: (botId: string, message: BotMessage) => void;
  /** Append to an explicit thread (used by a turn that outlived a thread switch). */
  appendTo: (botId: string, threadId: string, message: BotMessage) => void;
  appendScheduled: (botId: string, message: BotMessage & { runId: string }) => boolean;
  update: (botId: string, id: string, patch: Partial<BotMessage>, persist?: boolean) => void;
  /** Patch a message in an explicit thread. */
  updateIn: (botId: string, threadId: string, id: string, patch: Partial<BotMessage>, persist?: boolean) => void;
  clear: (botId: string) => void;
  createThread: (botId: string, title?: string) => string;
  selectThread: (botId: string, threadId: string) => void;
  branchThread: (botId: string, messageId: string, title?: string) => string | null;
  renameThread: (botId: string, threadId: string, title: string) => void;
  deleteThread: (botId: string, threadId: string) => void;
  react: (botId: string, messageId: string, emoji: string) => void;
}

export function threadKey(botId: string, threadId: string): string {
  return `${botId}::${threadId}`;
}

function mainThread(botId: string): BotThread {
  return { id: MAIN_THREAD, title: 'Main', createdAt: new Date(0).toISOString() };
}

function newId(): string {
  return `thr_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}

function isMessage(v: unknown): v is BotMessage {
  if (!v || typeof v !== 'object') return false;
  const m = v as Record<string, unknown>;
  return (
    typeof m.id === 'string'
    && (m.role === 'user' || m.role === 'assistant')
    && typeof m.content === 'string'
  );
}

function sanitizeMessages(input: unknown): BotMessage[] {
  return Array.isArray(input) ? input.filter(isMessage) : [];
}

function sanitizeByBot(input: unknown): Record<string, BotMessage[]> {
  const byBot: Record<string, BotMessage[]> = {};
  if (input && typeof input === 'object') {
    for (const [id, list] of Object.entries(input as Record<string, unknown>)) {
      if (Array.isArray(list)) byBot[id] = sanitizeMessages(list);
    }
  }
  return byBot;
}

/** Normalise the current or legacy document into the multi-thread shape. */
export function normalizePersisted(input: unknown): Required<PersistedChat> {
  const parsed = (input ?? {}) as PersistedChat;
  const threadsByBot: Record<string, BotThread[]> = {};
  const messagesByThread: Record<string, BotMessage[]> = {};
  const activeThreadByBot: Record<string, string> = {};

  if (parsed.messagesByThread && typeof parsed.messagesByThread === 'object') {
    for (const [key, list] of Object.entries(parsed.messagesByThread)) {
      messagesByThread[key] = sanitizeMessages(list);
    }
  }
  if (parsed.threadsByBot && typeof parsed.threadsByBot === 'object') {
    for (const [botId, threads] of Object.entries(parsed.threadsByBot)) {
      if (!Array.isArray(threads)) continue;
      const clean = threads.filter((thread): thread is BotThread => (
        Boolean(thread) && typeof thread.id === 'string' && typeof thread.title === 'string'
      ));
      if (clean.length) threadsByBot[botId] = clean;
    }
  }
  // Legacy: `byBot` was the whole store. Migrate it to each bot's main thread.
  const legacy = parsed.byBot && typeof parsed.byBot === 'object' ? parsed.byBot : {};
  for (const [botId, list] of Object.entries(legacy)) {
    const messages = sanitizeMessages(list);
    if (!threadsByBot[botId]) threadsByBot[botId] = [mainThread(botId)];
    messagesByThread[threadKey(botId, MAIN_THREAD)] = messages;
  }
  // Ensure every bot with messages has a thread list and a valid active id.
  for (const key of Object.keys(messagesByThread)) {
    const botId = key.split('::')[0];
    if (!threadsByBot[botId]) threadsByBot[botId] = [mainThread(botId)];
  }
  for (const [botId, threads] of Object.entries(threadsByBot)) {
    const ids = new Set(threads.map((thread) => thread.id));
    if (!ids.has(MAIN_THREAD)) threads.unshift(mainThread(botId));
    const requested = parsed.activeThreadByBot?.[botId];
    activeThreadByBot[botId] = requested && ids.has(requested) ? requested : MAIN_THREAD;
  }
  return { byBot: {}, threadsByBot, messagesByThread, activeThreadByBot };
}

function activeMessages(state: Pick<BotChatStore, 'messagesByThread' | 'activeThreadByBot'>, botId: string): BotMessage[] {
  const active = state.activeThreadByBot[botId] ?? MAIN_THREAD;
  return state.messagesByThread[threadKey(botId, active)] ?? [];
}

function persist(state: PersistedChat) {
  const data = {
    threadsByBot: state.threadsByBot ?? {},
    messagesByThread: state.messagesByThread ?? {},
    activeThreadByBot: state.activeThreadByBot ?? {},
  };
  const next = newBotStateEnvelope(data);
  writeLocalEnvelope(BOT_CHAT_KEY, next);
  queueBotStatePush('chat', next);
}

/** Structured extras that are not columns of the message log. */
function messagePayload(message: BotMessage): Record<string, unknown> | undefined {
  const payload: Record<string, unknown> = {};
  if (message.delegations?.length) payload.delegations = message.delegations;
  if (message.pendingApproval) payload.pendingApproval = message.pendingApproval;
  if (message.pendingClarification) payload.pendingClarification = message.pendingClarification;
  if (message.reactions && Object.keys(message.reactions).length) payload.reactions = message.reactions;
  return Object.keys(payload).length ? payload : undefined;
}

function messageFromRow(row: MessageLogRow): BotMessage {
  const payload = decodePayload(row.payload);
  return {
    id: row.id,
    role: row.role,
    content: row.content,
    at: row.at,
    ...(row.threadId ? { threadId: row.threadId } : {}),
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
    ...(payload?.reactions ? { reactions: payload.reactions as Record<string, number> } : {}),
  };
}

export const useBotChatStore = create<BotChatStore>((set, get) => {
  const apply = (next: PersistedChat, persistNow = true) => {
    const byBot: Record<string, BotMessage[]> = {};
    for (const botId of Object.keys(next.threadsByBot ?? {})) {
      byBot[botId] = activeMessages({
        messagesByThread: next.messagesByThread ?? {},
        activeThreadByBot: next.activeThreadByBot ?? {},
      }, botId);
    }
    if (persistNow) persist(next);
    set({
      byBot,
      threadsByBot: next.threadsByBot ?? {},
      messagesByThread: next.messagesByThread ?? {},
      activeThreadByBot: next.activeThreadByBot ?? {},
    });
  };

  const syncFromServer = async () => {
    const remote = await pullBotState('chat');
    const localEnvelope = readLocalEnvelope<PersistedChat>(BOT_CHAT_KEY);
    const local = normalizePersisted(localEnvelope?.data);
    if (!remote) {
      if (localEnvelope) queueBotStatePush('chat', localEnvelope);
    } else if (shouldAdoptRemote(localEnvelope?.updatedAt, remote.updatedAt)) {
      try {
        const parsed = normalizePersisted(JSON.parse(remote.document) as unknown);
        writeLocalEnvelope(BOT_CHAT_KEY, newBotStateEnvelope(parsed, new Date(remote.updatedAt)));
        apply(parsed, false);
      } catch {
        /* malformed mirror — keep the local copy */
      }
    } else if (localEnvelope && shouldPushLocal(localEnvelope.updatedAt, remote.updatedAt)) {
      queueBotStatePush('chat', localEnvelope);
    }

    const rows = await fetchBotMessages('chat');
    if (rows.length === 0) return;
    const state = get();
    const threadsByBot = { ...state.threadsByBot };
    const messagesByThread = { ...state.messagesByThread };
    const grouped = new Map<string, BotMessage[]>();
    for (const row of rows) {
      if (!row.botId) continue;
      const threadId = row.threadId || MAIN_THREAD;
      const key = threadKey(row.botId, threadId);
      const list = grouped.get(key) ?? [];
      list.push(messageFromRow(row));
      grouped.set(key, list);
      if (!threadsByBot[row.botId]) threadsByBot[row.botId] = [mainThread(row.botId)];
      if (!threadsByBot[row.botId].some((thread) => thread.id === threadId)) {
        threadsByBot[row.botId].push({ id: threadId, title: 'Recovered thread', createdAt: new Date().toISOString() });
      }
    }
    for (const [key, remoteMessages] of grouped) {
      messagesByThread[key] = mergeMessageLog(messagesByThread[key] ?? [], remoteMessages);
    }
    apply({
      threadsByBot,
      messagesByThread,
      activeThreadByBot: state.activeThreadByBot,
    });
  };

  return {
    byBot: {},
    threadsByBot: {},
    messagesByThread: {},
    activeThreadByBot: {},
    hydrated: false,

    hydrate: () => {
      const local = readLocalEnvelope<PersistedChat>(BOT_CHAT_KEY);
      apply(normalizePersisted(local?.data), false);
      set({ hydrated: true });
      void syncFromServer();
    },

    setMessages: (botId, messages) => {
      const state = get();
      const threadId = state.activeThreadByBot[botId] ?? MAIN_THREAD;
      const messagesByThread = {
        ...state.messagesByThread,
        [threadKey(botId, threadId)]: messages.map((message) => ({ ...message, threadId })),
      };
      apply({
        threadsByBot: state.threadsByBot,
        messagesByThread,
        activeThreadByBot: state.activeThreadByBot,
      });
      void replaceBotMessages(
        'chat', botId, messages, (m) => messagePayload(m as BotMessage), threadId,
      );
    },

    append: (botId, message) => {
      const threadId = get().activeThreadByBot[botId] ?? MAIN_THREAD;
      get().appendTo(botId, threadId, message);
    },

    appendTo: (botId, threadId, message) => {
      const state = get();
      const entry = { ...message, threadId };
      const key = threadKey(botId, threadId);
      const list = state.messagesByThread[key] ?? [];
      const threadsByBot = { ...state.threadsByBot };
      if (!threadsByBot[botId]) threadsByBot[botId] = [mainThread(botId)];
      apply({
        threadsByBot,
        messagesByThread: { ...state.messagesByThread, [key]: [...list, entry] },
        activeThreadByBot: state.activeThreadByBot,
      });
      void appendBotMessage('chat', botId, entry, messagePayload(entry));
    },

    appendScheduled: (botId, message) => {
      const state = get();
      const key = threadKey(botId, MAIN_THREAD);
      const list = state.messagesByThread[key] ?? [];
      if (message.runId && list.some((m) => m.runId === message.runId)) return false;
      const entry = { ...message, threadId: MAIN_THREAD };
      const threadsByBot = { ...state.threadsByBot };
      if (!threadsByBot[botId]) threadsByBot[botId] = [mainThread(botId)];
      apply({
        threadsByBot,
        messagesByThread: { ...state.messagesByThread, [key]: [...list, entry] },
        activeThreadByBot: state.activeThreadByBot,
      });
      void appendBotMessage('chat', botId, entry, messagePayload(entry));
      return true;
    },

    update: (botId, id, patch, persistNow = true) => {
      const threadId = get().activeThreadByBot[botId] ?? MAIN_THREAD;
      get().updateIn(botId, threadId, id, patch, persistNow);
    },

    updateIn: (botId, threadId, id, patch, persistNow = true) => {
      const state = get();
      const key = threadKey(botId, threadId);
      const list = state.messagesByThread[key];
      if (!list) return;
      const updated = list.map((m) => (m.id === id ? { ...m, ...patch } : m));
      apply(
        {
          threadsByBot: state.threadsByBot,
          messagesByThread: { ...state.messagesByThread, [key]: updated },
          activeThreadByBot: state.activeThreadByBot,
        },
        persistNow,
      );
      if (persistNow) {
        const message = updated.find((m) => m.id === id);
        if (message) {
          void appendBotMessage('chat', botId, { ...message, threadId }, messagePayload(message));
        }
      }
    },

    clear: (botId) => {
      const state = get();
      const threadId = state.activeThreadByBot[botId] ?? MAIN_THREAD;
      const key = threadKey(botId, threadId);
      apply({
        threadsByBot: state.threadsByBot,
        messagesByThread: { ...state.messagesByThread, [key]: [] },
        activeThreadByBot: state.activeThreadByBot,
      });
      void replaceBotMessages('chat', botId, [], undefined, threadId);
    },

    createThread: (botId, title) => {
      const state = get();
      const id = newId();
      const threads = state.threadsByBot[botId] ?? [mainThread(botId)];
      const thread: BotThread = {
        id,
        title: title?.trim() || `Thread ${threads.filter((t) => t.id !== MAIN_THREAD).length + 1}`,
        createdAt: new Date().toISOString(),
      };
      apply({
        threadsByBot: { ...state.threadsByBot, [botId]: [...threads, thread] },
        messagesByThread: { ...state.messagesByThread, [threadKey(botId, id)]: [] },
        activeThreadByBot: { ...state.activeThreadByBot, [botId]: id },
      });
      return id;
    },

    selectThread: (botId, threadId) => {
      const state = get();
      const threads = state.threadsByBot[botId] ?? [mainThread(botId)];
      if (!threads.some((thread) => thread.id === threadId)) return;
      apply({
        threadsByBot: state.threadsByBot,
        messagesByThread: state.messagesByThread,
        activeThreadByBot: { ...state.activeThreadByBot, [botId]: threadId },
      });
    },

    branchThread: (botId, messageId, title) => {
      const state = get();
      const sourceThread = state.activeThreadByBot[botId] ?? MAIN_THREAD;
      const source = state.messagesByThread[threadKey(botId, sourceThread)] ?? [];
      const index = source.findIndex((message) => message.id === messageId);
      if (index < 0) return null;
      const id = newId();
      const copied = source.slice(0, index + 1).map((message, offset) => ({
        ...message,
        id: `br_${id}_${offset}`,
        threadId: id,
      }));
      const threads = state.threadsByBot[botId] ?? [mainThread(botId)];
      const thread: BotThread = {
        id,
        title: title?.trim() || `Branch of ${source[index].content.slice(0, 40) || 'message'}`,
        createdAt: new Date().toISOString(),
        parentThreadId: sourceThread,
        branchFromMessageId: messageId,
      };
      apply({
        threadsByBot: { ...state.threadsByBot, [botId]: [...threads, thread] },
        messagesByThread: { ...state.messagesByThread, [threadKey(botId, id)]: copied },
        activeThreadByBot: { ...state.activeThreadByBot, [botId]: id },
      });
      void replaceBotMessages('chat', botId, copied, (m) => messagePayload(m as BotMessage), id);
      return id;
    },

    renameThread: (botId, threadId, title) => {
      const state = get();
      const threads = state.threadsByBot[botId] ?? [];
      apply({
        threadsByBot: {
          ...state.threadsByBot,
          [botId]: threads.map((thread) => (
            thread.id === threadId ? { ...thread, title: title.trim() || thread.title } : thread
          )),
        },
        messagesByThread: state.messagesByThread,
        activeThreadByBot: state.activeThreadByBot,
      });
    },

    deleteThread: (botId, threadId) => {
      if (threadId === MAIN_THREAD) return;
      const state = get();
      const threads = (state.threadsByBot[botId] ?? []).filter((thread) => thread.id !== threadId);
      const messagesByThread = { ...state.messagesByThread };
      delete messagesByThread[threadKey(botId, threadId)];
      apply({
        threadsByBot: { ...state.threadsByBot, [botId]: threads },
        messagesByThread,
        activeThreadByBot: {
          ...state.activeThreadByBot,
          [botId]: state.activeThreadByBot[botId] === threadId ? MAIN_THREAD : state.activeThreadByBot[botId],
        },
      });
      void replaceBotMessages('chat', botId, [], undefined, threadId);
    },

    react: (botId, messageId, emoji) => {
      const state = get();
      const threadId = state.activeThreadByBot[botId] ?? MAIN_THREAD;
      const key = threadKey(botId, threadId);
      const list = state.messagesByThread[key] ?? [];
      const updated = list.map((message) => {
        if (message.id !== messageId) return message;
        const reactions = { ...(message.reactions ?? {}) };
        reactions[emoji] = (reactions[emoji] ?? 0) + 1;
        return { ...message, reactions };
      });
      apply({
        threadsByBot: state.threadsByBot,
        messagesByThread: { ...state.messagesByThread, [key]: updated },
        activeThreadByBot: state.activeThreadByBot,
      });
      const message = updated.find((item) => item.id === messageId);
      if (message) {
        void appendBotMessage('chat', botId, { ...message, threadId }, messagePayload(message));
      }
    },
  };
});

export function useHydrateBotChat() {
  const hydrate = useBotChatStore((s) => s.hydrate);
  const hydrated = useBotChatStore((s) => s.hydrated);
  useEffect(() => {
    if (!hydrated) hydrate();
  }, [hydrated, hydrate]);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === BOT_CHAT_KEY) hydrate();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [hydrate]);
}
