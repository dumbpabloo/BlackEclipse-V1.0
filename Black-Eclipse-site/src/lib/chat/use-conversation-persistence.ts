'use client';

// ── Conversation persistence ─────────────────────────────────────────────────
// The ChatBot transcript store's durability lives here, out of the SPA shell
// (which is pinned at its size budget): the instant localStorage cache is read
// on boot, the server transcript is hydrated + merged over it, and every change
// is mirrored back to the server (debounced). Extracted behavior-preserving from
// `stock-chat.tsx`; the shell keeps only the refs and setters it already owns.

import { useCallback, useEffect, useRef } from 'react';
import {
  CONVERSATIONS_STORAGE_KEY,
  mergeConversations,
  mostRecentConversation,
  normalizeConversations,
  saveConversationsCache,
  type Conversation,
} from './chat-organization';
import {
  deleteServerConversation,
  getServerConversation,
  listServerConversations,
  saveServerConversation,
} from './transcript-client';
import { createSerialLane } from './serial-lane';
import { useWorkspaceStore } from '../agent-workspace/store';
import { restoreWorkspaceForChat } from '../agent-workspace/restore';
import type { Message } from '@/types/chat';

// All transcript writes/deletes share ONE serial lane (see serial-lane.ts):
// a first-run migration can queue hundreds of large upserts, and sending them
// in parallel saturates the browser's ~6 connections per origin — the boot's
// own summary/settings/health fetches then queue behind them and the app sits
// on its global "LOADING" screen. One at a time keeps boot responsive.
const queueTranscriptWrite = createSerialLane();

export interface ConversationPersistenceOptions {
  /** False until the shell has mounted (SSR-safe gate). */
  mounted: boolean;
  /** The live transcript list (dependency of the server mirror). */
  conversations: Conversation[];
  /** Synchronous mirror of `conversations` the async handlers read post-await. */
  conversationsRef: { current: Conversation[] };
  /** Synchronous mirror of the active conversation id. */
  activeConversationIdRef: { current: string | null };
  setConversations: (updated: Conversation[]) => void;
  setActiveConversationId: (id: string | null) => void;
  setMessages: (messages: Message[]) => void;
  /** Loads folders / colors / pin / rename from the organization store. */
  loadOrganization: () => void;
}

/**
 * True when the server holds more turns than the local copy has. The boot list
 * carries metadata only (`messages: []`), so this is usually how an unopened
 * conversation announces that its bodies must be fetched; it also catches a
 * localStorage cache that went stale on an older build or was truncated at the
 * quota boundary.
 */
export function needsHydration(conv: Conversation | null | undefined): boolean {
  return Boolean(conv && (conv.messageCount ?? 0) > conv.messages.length);
}

export interface ConversationPersistence {
  /** Fetch and attach one conversation's message bodies, on open. */
  hydrateConversation: (chatId: string) => Promise<Message[]>;
  /** Make a conversation active and show it, hydrating its bodies if needed. */
  activateConversation: (chatId: string) => void;
}

export function useConversationPersistence(opts: ConversationPersistenceOptions): ConversationPersistence {
  const {
    mounted,
    conversations,
    conversationsRef,
    activeConversationIdRef,
    setConversations,
    setActiveConversationId,
    setMessages,
    loadOrganization,
  } = opts;

  // Another same-origin tab wrote the cache. Merge instead of replacing: a
  // background run in tab A must not be lost because tab B saved a new prompt.
  useEffect(() => {
    if (!mounted || typeof window === 'undefined') return;
    const onStorage = (event: StorageEvent) => {
      if (event.key !== CONVERSATIONS_STORAGE_KEY || !event.newValue) return;
      try {
        const incoming = normalizeConversations(JSON.parse(event.newValue));
        const merged = mergeConversations(conversationsRef.current, incoming);
        conversationsRef.current = merged;
        setConversations(merged);
        const activeId = activeConversationIdRef.current;
        if (activeId) {
          const active = merged.find((c) => c.id === activeId);
          if (active) setMessages(active.messages);
        }
      } catch {
        /* malformed cross-tab write: keep the in-memory transcript */
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
    // The setters are shell-provided wrappers; re-subscribing is cheap.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mounted]);

  // Server transcript sync. `serverHydratedRef` gates the diff-sync until the
  // first successful server load (so a failed load cannot delete server rows).
  // `syncedRef` remembers the last serialized form we pushed per conversation id.
  const serverHydratedRef = useRef(false);
  const syncedRef = useRef<Map<string, string>>(new Map());

  /**
   * Replace a summary row with the server's full transcript. Idempotent: a row
   * whose bodies are already loaded returns them without a request, so opening
   * a chat twice costs nothing. A failed load leaves the summary in place (the
   * server copy is still safe; the next open retries).
   */
  const hydrateConversation = useCallback(async (chatId: string): Promise<Message[]> => {
    const found = conversationsRef.current.find((c) => c.id === chatId);
    if (!found || !needsHydration(found)) return found?.messages ?? [];
    try {
      const full = await getServerConversation(chatId);
      if (!full || full.messages.length === 0) return found.messages;
      // Union, not replace: a turn appended in this tab since the last mirror
      // must survive the server copy arriving.
      const combined = mergeConversations([found], [full])[0] ?? found;
      const messages = combined.messages;
      const next = conversationsRef.current.map((c) =>
        c.id === chatId
          ? { ...c, ...combined, messages, messageCount: Math.max(messages.length, full.messageCount ?? 0) }
          : c,
      );
      conversationsRef.current = next;
      setConversations(next);
      if (activeConversationIdRef.current === chatId) setMessages(messages);
      return messages;
    } catch {
      return found.messages;
    }
  }, [conversationsRef, activeConversationIdRef, setConversations, setMessages]);

  const activateConversation = useCallback((chatId: string) => {
    activeConversationIdRef.current = chatId;
    setActiveConversationId(chatId);
    const found = conversationsRef.current.find((c) => c.id === chatId);
    if (!found) {
      setMessages([]);
      useWorkspaceStore.getState().clear();
      return;
    }
    if (needsHydration(found)) {
      // The boot list carries metadata only; show the shell immediately and
      // fill the thread when the bodies arrive.
      setMessages([]);
      useWorkspaceStore.getState().clear();
      void hydrateConversation(chatId).then((messages) => {
        if (activeConversationIdRef.current !== chatId) return;
        setMessages(messages);
        void restoreWorkspaceForChat(messages, chatId);
      });
      return;
    }
    setMessages(found.messages);
    void restoreWorkspaceForChat(found.messages, chatId);
  }, [conversationsRef, activeConversationIdRef, setActiveConversationId, setMessages, hydrateConversation]);

  // Load conversations + folders from local storage.
  useEffect(() => {
    if (!mounted) return;
    loadOrganization();
    const saved = localStorage.getItem(CONVERSATIONS_STORAGE_KEY);
    if (saved) {
      try {
        // Normalize on load: backfills createdAt + folder/color/pin fields on
        // legacy rows so older transcripts gain organization support cleanly.
        const parsed = normalizeConversations(JSON.parse(saved));
        conversationsRef.current = parsed;
        setConversations(parsed);
        // Open the most recently active chat, not the first stored one.
        const start = mostRecentConversation(parsed);
        if (start) {
          activeConversationIdRef.current = start.id;
          setActiveConversationId(start.id);
          setMessages(start.messages);
          void restoreWorkspaceForChat(start.messages, start.id);
        } else {
          activeConversationIdRef.current = null;
          setActiveConversationId(null);
          setMessages([]);
          useWorkspaceStore.getState().clear();
        }
      } catch (err) {
        console.error('Error loading conversations:', err);
      }
    } else {
      conversationsRef.current = [];
      setConversations([]);
      activeConversationIdRef.current = null;
      setActiveConversationId(null);
      setMessages([]);
      useWorkspaceStore.getState().clear();
    }
  }, [mounted, loadOrganization]);

  // Server transcripts are the durable copy. localStorage is origin-scoped, so
  // closing the server or opening the app from a different origin silently lost
  // every chat; the Python runtime kept the runs but the UI could not read a
  // transcript back. On boot, hydrate from the server and merge it with the
  // local cache: the server wins for ids it has, local-only conversations are
  // kept and pushed up. localStorage remains the instant first paint above.
  useEffect(() => {
    if (!mounted) return;
    let cancelled = false;
    (async () => {
      try {
        const server = await listServerConversations();
        if (cancelled) return;
        const serverIds = new Set(server.map((c) => c.id));
        const local = conversationsRef.current;
        const localOnly = local.filter((c) => !serverIds.has(c.id));
        // Union by id; prefer the most recent activity, keeping the local copy on
        // a tie (it may carry an un-synced organization edit we push up).
        const byId = new Map<string, Conversation>();
        for (const c of [...server, ...local]) {
          const prev = byId.get(c.id);
          if (!prev || (c.timestamp ?? 0) > (prev.timestamp ?? 0)) byId.set(c.id, c);
        }
        const merged = [...byId.values()].sort((a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0));

        // The boot list is metadata-only. Fetch the OPEN conversation's bodies
        // now (one request) instead of every conversation's; the local cache
        // above usually already has them, so this is the fallback for a chat
        // that outlived localStorage (a new origin, or quota eviction).
        const initial = activeConversationIdRef.current
          ? merged.find((c) => c.id === activeConversationIdRef.current)
          : mostRecentConversation(merged);
        if (initial && needsHydration(initial)) {
          const full = await getServerConversation(initial.id).catch(() => null);
          if (cancelled) return;
          if (full && full.messages.length > 0) {
            const at = merged.findIndex((c) => c.id === initial.id);
            if (at !== -1) {
              const combined = mergeConversations([merged[at]], [full])[0] ?? merged[at];
              merged[at] = {
                ...merged[at],
                ...combined,
                messages: combined.messages,
                messageCount: Math.max(combined.messages.length, full.messageCount ?? 0),
              };
            }
          }
        }

        conversationsRef.current = merged;
        setConversations(merged);
        // Seed the sync ledger with what the server already holds, then push the
        // conversations that exist only locally (first-run migration).
        for (const c of merged) syncedRef.current.set(c.id, JSON.stringify(c));
        // Queued, never parallel: one request at a time so a large migration
        // cannot starve the boot's own fetches.
        for (const c of localOnly) void queueTranscriptWrite(() => saveServerConversation(c));
        serverHydratedRef.current = true;
        // Refresh the OPEN transcript from the merged (durable) copy. The
        // localStorage first paint above can be stale: it is origin-scoped and
        // silently stops updating once the whole transcript list outgrows the
        // quota (`setItem` throws QuotaExceededError, swallowed above), so the
        // cache keeps returning an old snapshot. Without this, a restart/refresh
        // showed that stale transcript until the user switched chats, even
        // though the server already held the new turns.
        const currentId = activeConversationIdRef.current;
        const active = currentId ? merged.find((c) => c.id === currentId) : null;
        if (active) {
          setMessages(active.messages);
          void restoreWorkspaceForChat(active.messages, active.id);
        } else {
          const start = mostRecentConversation(merged);
          if (start) {
            activeConversationIdRef.current = start.id;
            setActiveConversationId(start.id);
            setMessages(start.messages);
            void restoreWorkspaceForChat(start.messages, start.id);
          } else {
            activeConversationIdRef.current = null;
            setActiveConversationId(null);
            setMessages([]);
            useWorkspaceStore.getState().clear();
          }
        }
        // Evicts the least valuable chats rather than freezing the cache when
        // the merged list exceeds the quota (the server copy is authoritative).
        saveConversationsCache(localStorage, merged);
      } catch {
        // Runtime unavailable: stay local-only; the cache still works this session.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mounted]);

  // Mirror conversation changes to the server (debounced). Upserts always run so
  // a conversation is durable even if the initial hydration failed or was slow;
  // the DELETE pass is gated on a successful load so a failed load can never
  // trigger spurious deletions of the server's own rows.
  useEffect(() => {
    if (!mounted) return;
    const timer = setTimeout(() => {
      const list = conversationsRef.current;
      const seen = new Set<string>();
      for (const c of list) {
        seen.add(c.id);
        const signature = JSON.stringify(c);
        if (syncedRef.current.get(c.id) === signature) continue;
        syncedRef.current.set(c.id, signature);
        void queueTranscriptWrite(() => saveServerConversation(c)).catch(() => {
          // A failed write must be retried on the next change.
          syncedRef.current.delete(c.id);
        });
      }
      if (!serverHydratedRef.current) return;
      for (const id of [...syncedRef.current.keys()]) {
        if (seen.has(id)) continue;
        syncedRef.current.delete(id);
        void queueTranscriptWrite(() => deleteServerConversation(id)).catch(() => undefined);
      }
    }, 700);
    return () => clearTimeout(timer);
  }, [conversations, mounted]);

  return { hydrateConversation, activateConversation };
}
