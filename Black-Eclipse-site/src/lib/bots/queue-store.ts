// ── Durable bot turn queue ───────────────────────────────────────────────────
// A message composed while the bot is running is queued, not dropped. The queue
// is local-first and mirrored to `BotState` key `queue`; reorder/cancel/steer are
// explicit operator actions. "Steer" stops the running turn and promotes the
// queued message to the front, so the next turn starts with it immediately.

'use client';

import { useEffect } from 'react';
import { create } from 'zustand';
import {
  newBotStateEnvelope,
  pullBotState,
  queueBotStatePush,
  readLocalEnvelope,
  shouldAdoptRemote,
  writeLocalEnvelope,
} from '@/lib/bots/state-sync';

export const BOT_QUEUE_KEY = 'be_bot_queue_v1';

export interface QueuedTurn {
  id: string;
  botId: string;
  threadId: string;
  text: string;
  createdAt: string;
}

interface PersistedQueue {
  queuedByBot: Record<string, QueuedTurn[]>;
}

interface BotQueueStore {
  queuedByBot: Record<string, QueuedTurn[]>;
  hydrated: boolean;
  hydrate: () => void;
  enqueue: (input: { botId: string; threadId: string; text: string }) => QueuedTurn;
  remove: (botId: string, id: string) => void;
  move: (botId: string, id: string, direction: -1 | 1) => void;
  promote: (botId: string, id: string) => void;
  clear: (botId: string) => void;
  takeNext: (botId: string) => QueuedTurn | undefined;
}

function sanitize(input: unknown): PersistedQueue {
  const out: PersistedQueue = { queuedByBot: {} };
  if (!input || typeof input !== 'object') return out;
  const raw = (input as PersistedQueue).queuedByBot;
  if (!raw || typeof raw !== 'object') return out;
  for (const [botId, list] of Object.entries(raw)) {
    if (!Array.isArray(list)) continue;
    out.queuedByBot[botId] = list.filter((item): item is QueuedTurn => (
      Boolean(item)
      && typeof item.id === 'string'
      && typeof item.text === 'string'
      && typeof item.threadId === 'string'
    ));
  }
  return out;
}

function persist(queuedByBot: Record<string, QueuedTurn[]>) {
  const envelope = newBotStateEnvelope({ queuedByBot });
  writeLocalEnvelope(BOT_QUEUE_KEY, envelope);
  queueBotStatePush('queue', envelope);
}

export const useBotQueueStore = create<BotQueueStore>((set, get) => {
  const commit = (queuedByBot: Record<string, QueuedTurn[]>, persistNow = true) => {
    if (persistNow) persist(queuedByBot);
    set({ queuedByBot });
  };

  const sync = async () => {
    const remote = await pullBotState('queue');
    const local = readLocalEnvelope<PersistedQueue>(BOT_QUEUE_KEY);
    if (remote && shouldAdoptRemote(local?.updatedAt, remote.updatedAt)) {
      try {
        const data = sanitize(JSON.parse(remote.document) as unknown);
        writeLocalEnvelope(BOT_QUEUE_KEY, newBotStateEnvelope(data, new Date(remote.updatedAt)));
        set({ queuedByBot: data.queuedByBot });
      } catch {
        /* malformed mirror — keep local */
      }
    } else if (local && remote) {
      queueBotStatePush('queue', local);
    }
  };

  return {
    queuedByBot: {},
    hydrated: false,

    hydrate: () => {
      const local = readLocalEnvelope<PersistedQueue>(BOT_QUEUE_KEY);
      set({ queuedByBot: sanitize(local?.data).queuedByBot, hydrated: true });
      void sync();
    },

    enqueue: ({ botId, threadId, text }) => {
      const turn: QueuedTurn = {
        id: `q_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        botId,
        threadId,
        text,
        createdAt: new Date().toISOString(),
      };
      const list = [...(get().queuedByBot[botId] ?? []), turn];
      commit({ ...get().queuedByBot, [botId]: list });
      return turn;
    },

    remove: (botId, id) => {
      const list = (get().queuedByBot[botId] ?? []).filter((turn) => turn.id !== id);
      commit({ ...get().queuedByBot, [botId]: list });
    },

    move: (botId, id, direction) => {
      const list = [...(get().queuedByBot[botId] ?? [])];
      const index = list.findIndex((turn) => turn.id === id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= list.length) return;
      [list[index], list[target]] = [list[target], list[index]];
      commit({ ...get().queuedByBot, [botId]: list });
    },

    promote: (botId, id) => {
      const list = [...(get().queuedByBot[botId] ?? [])];
      const index = list.findIndex((turn) => turn.id === id);
      if (index <= 0) return;
      const [turn] = list.splice(index, 1);
      list.unshift(turn);
      commit({ ...get().queuedByBot, [botId]: list });
    },

    clear: (botId) => {
      commit({ ...get().queuedByBot, [botId]: [] });
    },

    takeNext: (botId) => {
      const list = [...(get().queuedByBot[botId] ?? [])];
      const next = list.shift();
      commit({ ...get().queuedByBot, [botId]: list });
      return next;
    },
  };
});

export function useHydrateBotQueue() {
  const hydrate = useBotQueueStore((s) => s.hydrate);
  const hydrated = useBotQueueStore((s) => s.hydrated);
  useEffect(() => {
    if (!hydrated) hydrate();
  }, [hydrated, hydrate]);
}
