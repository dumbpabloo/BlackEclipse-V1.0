// ── Bot group chat store ──────────────────────────────────────────────────────
// Group rooms that gather 2–6 of the user's bots into one conversation. The
// store hydrates from localStorage for an instant UI, then mirrors the document
// to the server (`BotState`, key `groups`) so cleared browser storage or a
// second tab recovers the rooms. Local state always remains the source of truth
// while offline; the newest client timestamp wins. Replies still come from the
// real harness — nothing here fabricates a bot turn.

'use client';

import { useEffect } from 'react';
import { create } from 'zustand';
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
import {
  deleteBotRoom,
  fetchBotRooms,
  mergeRooms,
  upsertBotRoom,
  type RoomLogEntry,
} from '@/lib/bots/room-log';

export const BOT_GROUPS_KEY = 'be_bot_groups_v1';

/** How a room turns one user message into member answers.
 *  - `parallel`: every targeted member answers independently (independent takes).
 *  - `roundtable`: members answer in order and each later member sees the replies
 *    already produced in this round, so the room can deliberate instead of
 *    repeating three separate opinions.
 */
export type GroupMode = 'parallel' | 'roundtable';

export interface BotGroup {
  id: string;
  name: string;
  /** Member bot ids (2–6). */
  memberIds: string[];
  /** Optional uploaded group photo (data URL). */
  image?: string;
  /** Defaults to `parallel` for rooms created before this field existed. */
  mode?: GroupMode;
  /** Optional chair for a roundtable: synthesizes the members' replies into one
   *  decision after the round. Must be a member; null/undefined means no chair. */
  leadId?: string | null;
  /** `all` = every targeted member answers; `lead` = the lead routes an
   *  unaddressed request to the best member (or handles it itself). */
  routingMode?: 'all' | 'lead';
  createdAt: string;
  /** Server row's last write, used to resolve a local/server merge. */
  updatedAt?: string;
}

export interface GroupMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  at: string;
  /** For assistant turns: the bot id that spoke; absent means the room itself. */
  authorId?: string;
  /** Display name shown on the bubble. */
  authorName?: string;
  error?: boolean;
  /** Optional thumbs feedback on an assistant turn. */
  feedback?: 'up' | 'down';
  /** General reactions on a message (emoji → count). */
  reactions?: Record<string, number>;
  /** Parallel fan-outs this member ran, for the live agent card. */
  delegations?: import('@/lib/agent/response-model').DelegationView[];
  /** A blocking permission decision raised by this member's turn. */
  pendingApproval?: import('@/lib/agent/response-model').PendingApproval;
  /** A paused `clarify` question from this member's turn. */
  pendingClarification?: import('@/lib/agent/response-model').PendingClarification;
  /** True when this turn came from an autonomous run (schedule / Run now). */
  scheduled?: boolean;
  /** Execution id of the autonomous run — the idempotency key. */
  runId?: string;
  /** The root request this message belongs to (its own id for a request). */
  taskId?: string;
  /** The member who currently owns the task. */
  taskOwnerId?: string;
  taskStatus?: 'open' | 'running' | 'done' | 'blocked' | 'cancelled';
  /** Threaded reply target (a request or another reply). */
  replyToId?: string;
  /** The thread root a message belongs to (normally the request id). */
  threadId?: string;
  kind?: 'request' | 'reply' | 'result' | 'note';
}

interface Persisted {
  groups: BotGroup[];
  byGroup: Record<string, GroupMessage[]>;
}

interface BotGroupsStore {
  groups: BotGroup[];
  byGroup: Record<string, GroupMessage[]>;
  hydrated: boolean;
  hydrate: () => void;
  createGroup: (input: { name: string; memberIds: string[]; image?: string; mode?: GroupMode }) => BotGroup;
  renameGroup: (id: string, name: string) => void;
  setGroupImage: (id: string, dataUrl: string | null) => void;
  setGroupMode: (id: string, mode: GroupMode) => void;
  setGroupLead: (id: string, leadId: string | null) => void;
  setGroupRouting: (id: string, routingMode: 'all' | 'lead') => void;
  /** Replace the member set (clamped to 2–6). */
  setMembers: (id: string, memberIds: string[]) => void;
  /** Remove a deleted bot from every room (and drop rooms left with no members). */
  removeMember: (botId: string) => void;
  deleteGroup: (id: string) => void;
  appendMessage: (groupId: string, message: GroupMessage) => void;
  /** Append an autonomous run's answer once (deduped by execution id). */
  appendScheduled: (groupId: string, message: GroupMessage & { runId: string }) => boolean;
  /** Replace a room's whole message list (used to truncate on edit/regenerate). */
  setMessages: (groupId: string, messages: GroupMessage[]) => void;
  /** Patch one message in place — used to fill the live streaming reply. A
   *  streaming caller passes `persist=false`; the terminal patch persists. */
  updateMessage: (groupId: string, id: string, patch: Partial<GroupMessage>, persist?: boolean) => void;
  /** Bump one emoji reaction on a message (persisted with the message). */
  react: (groupId: string, id: string, emoji: string) => void;
  clearMessages: (groupId: string) => void;
}

/** Validate a decoded document from either storage medium. */
function sanitizePersisted(input: unknown): Persisted {
  const parsed = (input ?? {}) as Partial<Persisted>;
  const groups = Array.isArray(parsed.groups)
    ? parsed.groups.filter(isGroup).map((g) => ({
      ...g,
      mode: g.mode === 'roundtable' ? 'roundtable' as const : 'parallel' as const,
      leadId: g.leadId && g.memberIds.includes(g.leadId) ? g.leadId : null,
      routingMode: g.routingMode === 'lead' ? 'lead' as const : 'all' as const,
    }))
    : [];
  const byGroup: Record<string, GroupMessage[]> = {};
  if (parsed.byGroup && typeof parsed.byGroup === 'object') {
    for (const [id, list] of Object.entries(parsed.byGroup)) {
      if (Array.isArray(list)) byGroup[id] = list.filter(isMessage);
    }
  }
  return { groups, byGroup };
}

/** Persist locally and queue the server mirror (best effort). */
function persist(
  groups: BotGroup[],
  byGroup: Record<string, GroupMessage[]>,
  envelope?: BotStateEnvelope<Persisted>,
) {
  const next = envelope ?? newBotStateEnvelope({ groups, byGroup });
  writeLocalEnvelope(BOT_GROUPS_KEY, next);
  queueBotStatePush('groups', next);
}

function isGroup(v: unknown): v is BotGroup {
  if (!v || typeof v !== 'object') return false;
  const g = v as Record<string, unknown>;
  return (
    typeof g.id === 'string' &&
    typeof g.name === 'string' &&
    Array.isArray(g.memberIds) &&
    g.memberIds.every((m) => typeof m === 'string')
  );
}

function isMessage(v: unknown): v is GroupMessage {
  if (!v || typeof v !== 'object') return false;
  const m = v as Record<string, unknown>;
  return (
    typeof m.id === 'string' &&
    (m.role === 'user' || m.role === 'assistant') &&
    typeof m.content === 'string'
  );
}

function newGroupId(): string {
  return `grp_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}

function stamp(group: BotGroup): BotGroup {
  return { ...group, updatedAt: new Date().toISOString() };
}

function roomOf(group: BotGroup): RoomLogEntry {
  return {
    id: group.id,
    name: group.name,
    memberIds: group.memberIds,
    mode: group.mode === 'roundtable' ? 'roundtable' : 'parallel',
    routingMode: group.routingMode === 'lead' ? 'lead' : 'all',
    leadId: group.leadId ?? null,
    image: group.image ?? null,
    createdAt: group.createdAt,
    updatedAt: group.updatedAt,
  };
}

function syncRoom(group: BotGroup): void {
  void upsertBotRoom(roomOf(group));
}

/** Structured extras that are not columns of the durable log. */
function groupMessagePayload(message: GroupMessage): Record<string, unknown> | undefined {
  const payload: Record<string, unknown> = {};
  if (message.delegations?.length) payload.delegations = message.delegations;
  if (message.pendingApproval) payload.pendingApproval = message.pendingApproval;
  if (message.pendingClarification) payload.pendingClarification = message.pendingClarification;
  if (message.reactions && Object.keys(message.reactions).length) payload.reactions = message.reactions;
  return Object.keys(payload).length ? payload : undefined;
}

function groupMessageFromRow(row: MessageLogRow): GroupMessage {
  const payload = decodePayload(row.payload);
  return {
    id: row.id,
    role: row.role,
    content: row.content,
    at: row.at,
    ...(row.authorId ? { authorId: row.authorId } : {}),
    ...(row.authorName ? { authorName: row.authorName } : {}),
    ...(row.scheduled ? { scheduled: true } : {}),
    ...(row.runId ? { runId: row.runId } : {}),
    ...(row.error ? { error: true } : {}),
    ...(row.feedback ? { feedback: row.feedback } : {}),
    ...(payload?.delegations
      ? { delegations: payload.delegations as GroupMessage['delegations'] }
      : {}),
    ...(payload?.pendingApproval
      ? { pendingApproval: payload.pendingApproval as GroupMessage['pendingApproval'] }
      : {}),
    ...(payload?.pendingClarification
      ? { pendingClarification: payload.pendingClarification as GroupMessage['pendingClarification'] }
      : {}),
    ...(payload?.reactions
      ? { reactions: payload.reactions as Record<string, number> }
      : {}),
  };
}

export const useBotGroupsStore = create<BotGroupsStore>((set, get) => {
  const commit = (groups: BotGroup[], byGroup: Record<string, GroupMessage[]>, persistNow = true) => {
    if (persistNow) persist(groups, byGroup);
    set({ groups, byGroup });
  };

  /** Reconcile with the durable mirror without ever blocking the local render.
   *  The remote copy is adopted only when its client timestamp is newer; a
   *  newer local copy is pushed instead. */
  const syncFromServer = async () => {
    const remote = await pullBotState('groups');
    const local = readLocalEnvelope<Persisted>(BOT_GROUPS_KEY);
    if (!remote) {
      // First run after the upgrade: seed the mirror with local rooms.
      if (local) queueBotStatePush('groups', local);
    } else if (shouldAdoptRemote(local?.updatedAt, remote.updatedAt)) {
      try {
        const data = sanitizePersisted(JSON.parse(remote.document) as unknown);
        const envelope = newBotStateEnvelope(data, new Date(remote.updatedAt));
        writeLocalEnvelope(BOT_GROUPS_KEY, envelope);
        set({ groups: data.groups, byGroup: data.byGroup });
      } catch {
        /* malformed mirror — keep the local copy */
      }
    } else if (local && shouldPushLocal(local.updatedAt, remote.updatedAt)) {
      queueBotStatePush('groups', local);
    }

    // Recover each room's transcript from the per-message log, even when the
    // snapshot was lost or is behind. The log is the durable source of truth.
    const rows = await fetchBotMessages('groups');
    if (rows.length === 0) return;
    const byGroup = { ...get().byGroup };
    const grouped = new Map<string, GroupMessage[]>();
    for (const row of rows) {
      if (!row.groupId) continue;
      const list = grouped.get(row.groupId) ?? [];
      list.push(groupMessageFromRow(row));
      grouped.set(row.groupId, list);
    }
    for (const [groupId, remoteMessages] of grouped) {
      byGroup[groupId] = mergeMessageLog(byGroup[groupId] ?? [], remoteMessages);
    }
    if (grouped.size > 0) {
      persist(get().groups, byGroup);
      set({ byGroup });
    }

    // Room identity is a separate durable row. Without it a cleared snapshot
    // would leave durable messages with no room to show them in.
    const remoteRooms = await fetchBotRooms();
    if (remoteRooms.length > 0) {
      const mergedRooms = mergeRooms(get().groups, remoteRooms as BotGroup[]);
      persist(mergedRooms, get().byGroup);
      set({ groups: mergedRooms });
    }
  };

  return {
    groups: [],
    byGroup: {},
    hydrated: false,

    hydrate: () => {
      const local = readLocalEnvelope<Persisted>(BOT_GROUPS_KEY);
      const { groups, byGroup } = sanitizePersisted(local?.data);
      set({ groups, byGroup, hydrated: true });
      void syncFromServer();
    },

    createGroup: ({ name, memberIds, image, mode }) => {
      const group: BotGroup = stamp({
        id: newGroupId(),
        name: name.trim(),
        memberIds: [...memberIds],
        image,
        mode: mode === 'roundtable' ? 'roundtable' : 'parallel',
        routingMode: 'all',
        createdAt: new Date().toISOString(),
      });
      commit([group, ...get().groups], get().byGroup);
      syncRoom(group);
      return group;
    },

    renameGroup: (id, name) => {
      const next = get().groups.map((g) => (g.id === id ? stamp({ ...g, name: name.trim() || g.name }) : g));
      commit(next, get().byGroup);
      const changed = next.find((g) => g.id === id);
      if (changed) syncRoom(changed);
    },

    setGroupImage: (id, dataUrl) => {
      const next = get().groups.map((g) => {
        if (g.id !== id) return g;
        const room = stamp({ ...g });
        if (dataUrl) room.image = dataUrl;
        else delete room.image;
        return room;
      });
      commit(next, get().byGroup);
      const changed = next.find((g) => g.id === id);
      if (changed) syncRoom(changed);
    },

    setGroupMode: (id, mode) => {
      const next = get().groups.map((g) => (
        g.id === id ? stamp({ ...g, mode: mode === 'roundtable' ? 'roundtable' : 'parallel' }) : g
      ));
      commit(next, get().byGroup);
      const changed = next.find((g) => g.id === id);
      if (changed) syncRoom(changed);
    },

    setGroupRouting: (id, routingMode) => {
      const next = get().groups.map((g) => (
        g.id === id ? stamp({ ...g, routingMode: routingMode === 'lead' ? 'lead' : 'all' }) : g
      ));
      commit(next, get().byGroup);
      const changed = next.find((g) => g.id === id);
      if (changed) syncRoom(changed);
    },

    setGroupLead: (id, leadId) => {
      const next = get().groups.map((g) => {
        if (g.id !== id) return g;
        const room = stamp({ ...g });
        if (leadId && g.memberIds.includes(leadId)) room.leadId = leadId;
        else delete room.leadId;
        return room;
      });
      commit(next, get().byGroup);
      const changed = next.find((g) => g.id === id);
      if (changed) syncRoom(changed);
    },

    setMembers: (id, memberIds) => {
      const unique = [...new Set(memberIds)].slice(0, 6);
      if (unique.length < 2) return;
      const next = get().groups.map((g) => (g.id === id ? stamp({
        ...g,
        memberIds: unique,
        // A removed lead silently becomes no chair instead of pointing at a
        // bot that is no longer in the room.
        ...(g.leadId && !unique.includes(g.leadId) ? { leadId: null } : {}),
      }) : g));
      commit(next, get().byGroup);
      const changed = next.find((g) => g.id === id);
      if (changed) syncRoom(changed);
    },

    removeMember: (botId) => {
      const all = get().groups;
      const groups = all
        .map((g) => (g.memberIds.includes(botId) ? stamp({
          ...g,
          memberIds: g.memberIds.filter((id) => id !== botId),
          ...(g.leadId === botId ? { leadId: null } : {}),
        }) : g))
        .filter((g) => g.memberIds.length > 0);
      const byGroup = { ...get().byGroup };
      for (const g of all) {
        if (!groups.some((next) => next.id === g.id)) {
          delete byGroup[g.id];
          void deleteBotRoom(g.id);
        }
      }
      commit(groups, byGroup);
      for (const group of groups) {
        if (all.find((old) => old.id === group.id)?.memberIds.includes(botId)) syncRoom(group);
      }
    },

    deleteGroup: (id) => {
      const byGroup = { ...get().byGroup };
      delete byGroup[id];
      commit(get().groups.filter((g) => g.id !== id), byGroup);
      void clearBotMessages('groups', id);
      void deleteBotRoom(id);
    },

    appendMessage: (groupId, message) => {
      const byGroup = { ...get().byGroup, [groupId]: [...(get().byGroup[groupId] ?? []), message] };
      commit(get().groups, byGroup);
      void appendBotMessage('groups', groupId, message, groupMessagePayload(message));
    },

    appendScheduled: (groupId, message) => {
      const list = get().byGroup[groupId] ?? [];
      if (list.some((m) => m.runId === message.runId)) return false;
      commit(get().groups, { ...get().byGroup, [groupId]: [...list, message] });
      void appendBotMessage('groups', groupId, message, groupMessagePayload(message));
      return true;
    },

    setMessages: (groupId, messages) => {
      commit(get().groups, { ...get().byGroup, [groupId]: messages });
      void replaceBotMessages(
        'groups',
        groupId,
        messages,
        (m) => groupMessagePayload(m as GroupMessage),
      );
    },

    updateMessage: (groupId, id, patch, persistNow = true) => {
      const list = get().byGroup[groupId];
      if (!list) return;
      const updated = list.map((m) => (m.id === id ? { ...m, ...patch } : m));
      const byGroup = { ...get().byGroup, [groupId]: updated };
      commit(get().groups, byGroup, persistNow);
      if (persistNow) {
        const message = updated.find((m) => m.id === id);
        if (message) {
          void appendBotMessage('groups', groupId, message, groupMessagePayload(message));
        }
      }
    },

    clearMessages: (groupId) => {
      const byGroup = { ...get().byGroup };
      delete byGroup[groupId];
      commit(get().groups, byGroup);
      void clearBotMessages('groups', groupId);
    },

    react: (groupId, id, emoji) => {
      const list = get().byGroup[groupId];
      if (!list) return;
      const updated = list.map((message) => {
        if (message.id !== id) return message;
        const reactions = { ...(message.reactions ?? {}) };
        reactions[emoji] = (reactions[emoji] ?? 0) + 1;
        return { ...message, reactions };
      });
      commit(get().groups, { ...get().byGroup, [groupId]: updated });
      const message = updated.find((item) => item.id === id);
      if (message) {
        void appendBotMessage('groups', groupId, message, groupMessagePayload(message));
      }
    },
  };
});

export function useHydrateBotGroups() {
  const hydrate = useBotGroupsStore((s) => s.hydrate);
  const hydrated = useBotGroupsStore((s) => s.hydrated);
  useEffect(() => {
    if (!hydrated) hydrate();
  }, [hydrated, hydrate]);
  // Another tab wrote a newer document — adopt it instead of waiting for a
  // reload. The storage event only fires in OTHER tabs, so this cannot loop.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === BOT_GROUPS_KEY) hydrate();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [hydrate]);
}
