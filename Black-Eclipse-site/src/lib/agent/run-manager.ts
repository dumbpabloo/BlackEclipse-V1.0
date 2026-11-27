// ── Conversation-scoped run manager ─────────────────────────────────────────
//
// The chat used to own exactly one `useAgentRun` instance, so starting a second
// conversation was impossible while the first was still streaming. The manager
// owns the streams instead: one run per conversation, many conversations at
// once. It lives outside React so a run survives unmounting/switching the chat
// surface, and every stream writes to the conversation that started it.
//
// It is deliberately NOT a second agent backend: it drives the same
// `getAgentProvider().chat` transport, the same typed events and the same
// recovery endpoints the hook used.

'use client';

import {
  ChatRequestError,
  forgetActiveRun,
  getRunSnapshot,
  loadActiveRun,
  recoverRun,
  rememberActiveRun,
  getAgentProvider,
} from './registry';
import type { ChatMessage } from './types';
import type { HarnessUiEvent } from './events';
import type { AssistantTurn, PendingApproval, PendingClarification } from './response-model';
import { emptyTurn } from './response-model';
import { mergeCapabilityBuild, type CapabilityBuildView } from '@/lib/harness/capability-types';
import { endActivity, initialActivityState, startActivity } from './activity-reducer';
import {
  applyDelegationEvent,
  dropProvisionalDelegation,
  startProvisionalDelegation,
} from './delegation-reducer';
import { ALERT_NOTIFY_EVENT } from '@/lib/alerts/browser-notifier';
import { beginDesktopActivity } from '@/lib/desktop/activity';
import { isViewerFocused } from '@/lib/notifications/focus';
import { useNotificationsStore } from '@/lib/notifications/store';
import { getUiFocus } from '@/lib/ui-engineering/ui-focus';
import { bindRunChat, unbindRunChat, setFocusedChat } from './run-focus';
import type { ActivityState } from './activity-reducer';
import {
  PENDING_RUN,
  currentTabId,
  getSharedRun,
  putSharedRun,
  publishTabEvent,
  removeSharedRun,
  subscribeTabEvents,
  withChatStartLock,
  type SharedRun,
  type TabEvent,
} from './tab-coordinator';

/** The key used before a conversation id exists (a brand-new chat). */
const NEW_CHAT = '__new__';

export interface StartRunArgs {
  /** Conversation turns BEFORE the new user message (role + content). */
  history: { role: 'user' | 'assistant'; content: string }[];
  /** The new user prompt. */
  userText: string;
  /** Workspace/session id, so the run's output is persisted server-side. */
  chatId?: string;
  /** The composer's model choice for this turn (overrides the saved model). */
  model?: string;
  /** The composer's per-question reasoning-effort override. */
  reasoningEffort?: import('./reasoning').ReasoningLevel;
  /**
   * Continue this existing (failed) run from its checkpoint instead of starting
   * a new run. Used by Retry so a timeout resumes the work already done.
   */
  resumeRunId?: string;
  /** Per-run turn sink. Called even when the conversation is not on screen. */
  onTurn?: (turn: AssistantTurn) => void;
  /** Fired once the run reaches a terminal state. */
  onDone?: (turn: AssistantTurn) => void;
}

export interface RunSnapshot {
  chatId: string | null;
  turn: AssistantTurn;
  active: boolean;
  runId: string | null;
}

interface LiveRun {
  chatId: string;
  controller: AbortController;
  turn: AssistantTurn;
  activityState: ActivityState;
  snapshot: RunSnapshot;
  active: boolean;
  runId: string | null;
  onTurn?: (turn: AssistantTurn) => void;
  onDone?: (turn: AssistantTurn) => void;
  raf: number | null;
  releaseActivity: (() => void) | null;
  /** An owner starts the run; a follower only watches the shared run. */
  owner: boolean;
  lastBroadcastAt?: number;
  resolve?: (turn: AssistantTurn) => void;
  followerTimer?: ReturnType<typeof setInterval> | null;
}

const EMPTY_TURN = emptyTurn();
const EMPTY_SNAPSHOT: RunSnapshot = {
  chatId: null,
  turn: EMPTY_TURN,
  active: false,
  runId: null,
};

type Listener = () => void;

function keyFor(chatId: string | null | undefined, tabId = ''): string {
  if (chatId && chatId.length > 0) return chatId;
  // A brand-new, unsaved chat has no id yet. It must still be unique PER TAB:
  // one shared '__new__' key made a second tab's fresh chat attach to the first
  // tab's run (and believe it was busy), so only one of them ever ran.
  return tabId ? `${NEW_CHAT}:${tabId}` : NEW_CHAT;
}

function announceCompletedRun(
  text: string,
  runId: string | null,
  chatId: string,
  focusedChatId: string | null,
): void {
  if (typeof window === 'undefined') return;
  const watchingThisChat =
    useNotificationsStore.getState().activeTab === 'chat' && focusedChatId === chatId;
  if (watchingThisChat && isViewerFocused()) return;
  const body = text.trim();
  if (!body) return;
  window.dispatchEvent(
    new CustomEvent(ALERT_NOTIFY_EVENT, {
      detail: {
        id: runId ? `run-done:${runId}` : undefined,
        title: 'Reply ready',
        body: body.length > 140 ? `${body.slice(0, 140)}…` : body,
        tab: 'chat',
        kind: 'agent',
        sound: 'taskdone',
      },
    }),
  );
}

class RunManager {
  private runs = new Map<string, LiveRun>();
  private listeners = new Set<Listener>();
  private lifecycleListeners = new Set<Listener>();
  private focusedChatId: string | null = null;
  private readonly tabId = currentTabId();

  constructor() {
    // Same-origin follower updates. A snapshot is broadcast by the owner; a
    // follower applies it without ever starting a second run.
    subscribeTabEvents((event) => this.onTabEvent(event));
  }

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Subscribes to start/finish only (never to token deltas). */
  subscribeLifecycle = (listener: Listener): (() => void) => {
    this.lifecycleListeners.add(listener);
    return () => this.lifecycleListeners.delete(listener);
  };

  /** Stable key of conversations with a live run, for sidebar indicators. */
  getActiveKey = (): string =>
    [...this.runs.values()]
      .filter((run) => run.active && run.chatId !== NEW_CHAT)
      .map((run) => run.chatId)
      .sort()
      .join('|');

  private notifyLifecycle(): void {
    for (const listener of this.lifecycleListeners) {
      try {
        listener();
      } catch {
        /* a broken subscriber must not stop the others */
      }
    }
  }

  /** Stable snapshot for `useSyncExternalStore`. */
  get(chatId: string | null | undefined): RunSnapshot {
    const run = this.runs.get(keyFor(chatId, this.tabId));
    return run ? run.snapshot : EMPTY_SNAPSHOT;
  }

  hasActive(chatId: string | null | undefined): boolean {
    const run = this.runs.get(keyFor(chatId, this.tabId));
    return Boolean(run?.active);
  }

  /** Busy locally OR by another tab. Queue drains must respect both. */
  isBusy(chatId: string | null | undefined): boolean {
    if (this.hasActive(chatId)) return true;
    const shared = getSharedRun(chatId);
    return Boolean(shared && shared.ownerTabId !== this.tabId);
  }

  /** Tell the Agent Workspace which conversation's run it should project. */
  focus(chatId: string | null | undefined): void {
    if (!chatId) return;
    this.focusedChatId = chatId;
    setFocusedChat(chatId);
    const shared = getSharedRun(chatId);
    if (shared && shared.ownerTabId !== this.tabId && shared.runId !== PENDING_RUN) {
      void this.attach(chatId, shared.runId, undefined, undefined);
    }
  }

  /** Every conversation currently streaming — used for sidebar indicators. */
  activeChatIds(): string[] {
    return [...this.runs.values()].filter((run) => run.active).map((run) => run.chatId);
  }

  private notify(): void {
    for (const listener of this.listeners) {
      try {
        listener();
      } catch {
        /* a broken subscriber must not stop the others */
      }
    }
  }

  private setSnapshot(run: LiveRun): void {
    run.snapshot = {
      chatId: run.chatId,
      turn: run.turn,
      active: run.active,
      runId: run.runId,
    };
  }

  private broadcastSnapshot(run: LiveRun, immediate = false): void {
    if (!run.owner || !run.runId) return;
    const now = Date.now();
    if (!immediate && now - (run.lastBroadcastAt ?? 0) < 180) return;
    run.lastBroadcastAt = now;
    if (run.active) {
      putSharedRun({
        chatId: run.chatId,
        runId: run.runId,
        ownerTabId: this.tabId,
        at: now,
        status: run.turn.status,
      });
    } else {
      removeSharedRun(run.chatId, run.runId);
    }
    publishTabEvent({
      type: 'run.snapshot',
      chatId: run.chatId,
      runId: run.runId,
      ownerTabId: this.tabId,
      turn: run.turn,
    });
  }

  private publish(run: LiveRun, immediate: boolean): void {
    const emit = () => {
      run.raf = null;
      this.setSnapshot(run);
      this.notify();
      this.broadcastSnapshot(run, immediate);
      run.onTurn?.(run.turn);
    };
    if (immediate) {
      if (run.raf != null) {
        cancelAnimationFrame(run.raf);
        run.raf = null;
      }
      emit();
      return;
    }
    if (run.raf == null) {
      run.raf = requestAnimationFrame(emit);
    }
  }

  private emit(run: LiveRun, immediate: boolean): void {
    if (typeof requestAnimationFrame === 'undefined') {
      this.setSnapshot(run);
      this.notify();
      this.broadcastSnapshot(run, immediate);
      run.onTurn?.(run.turn);
      return;
    }
    this.publish(run, immediate);
  }

  private applyEvent(run: LiveRun, event: HarnessUiEvent): void {
    const t = run.turn;
    switch (event.kind) {
      case 'status': {
        t.status = event.status;
        this.emit(run, true);
        break;
      }
      case 'delta': {
        if (t.status !== 'streaming') t.status = 'streaming';
        t.statusNote = undefined;
        t.text += event.text;
        this.emit(run, false);
        break;
      }
      case 'notice': {
        t.statusNote = event.text;
        this.emit(run, true);
        break;
      }
      case 'activity_start': {
        startActivity(run.activityState, event.item);
        t.activities = [...run.activityState.activities];
        if (event.item.name === 'delegate') {
          t.delegations = startProvisionalDelegation(t.delegations ?? [], event.item.id);
        }
        this.emit(run, true);
        break;
      }
      case 'activity_end': {
        if (event.name === 'delegate') {
          t.delegations = dropProvisionalDelegation(t.delegations ?? []);
        }
        endActivity(run.activityState, {
          id: event.id,
          name: event.name,
          status: event.status,
          durationMs: event.durationMs,
          code: event.code,
          summary: event.summary,
        });
        t.activities = [...run.activityState.activities];
        this.emit(run, true);
        break;
      }
      case 'changes': {
        const byPath = new Map(t.changes.map((c) => [c.path, c] as const));
        for (const item of event.items) {
          const previous = byPath.get(item.path);
          byPath.set(item.path, {
            ...item,
            edits: previous ? (previous.edits ?? 1) + 1 : 1,
          });
        }
        t.changes = [...byPath.values()];
        this.emit(run, true);
        break;
      }
      case 'final': {
        t.delegations = dropProvisionalDelegation(t.delegations ?? []);
        if (event.output) t.finalText = event.output;
        if (event.research) t.research = event.research;
        if (event.memory) t.memory = event.memory;
        if (event.runId) t.runId = event.runId;
        if (event.model) t.model = { model: event.model, provider: event.provider ?? null };
        if (event.codeVerification) t.codeVerification = event.codeVerification;
        if (event.autonomy !== undefined) t.autonomy = event.autonomy;
        if (event.capability) t.capability = event.capability;
        if (event.vision) t.vision = event.vision;
        if (event.documents) t.documents = event.documents;
        if (event.images) t.images = event.images;
        if (event.video) t.video = event.video;
        if (event.ui) t.ui = event.ui;
        t.pendingApproval = undefined;
        t.pendingClarification = undefined;
        t.statusNote = undefined;
        if (event.error) {
          t.error = { code: event.errorCode, message: event.error };
          t.status = 'error';
        } else {
          t.status = 'completed';
        }
        t.done = true;
        if (!event.error) {
          announceCompletedRun(
            event.output || t.text,
            t.runId ?? null,
            run.chatId,
            this.focusedChatId,
          );
        }
        this.emit(run, true);
        break;
      }
      case 'error': {
        t.delegations = dropProvisionalDelegation(t.delegations ?? []);
        t.error = { code: event.code, message: event.message };
        t.status = 'error';
        t.pendingApproval = undefined;
        t.pendingClarification = undefined;
        t.statusNote = undefined;
        t.done = true;
        this.emit(run, true);
        break;
      }
      case 'approval_request': {
        t.pendingApproval = event.approval;
        t.status = 'awaiting_approval';
        this.emit(run, true);
        if (typeof window !== 'undefined') {
          const grant = event.approval.grantPattern ? ' You can also allow it always.' : '';
          window.dispatchEvent(
            new CustomEvent(ALERT_NOTIFY_EVENT, {
              detail: {
                id: `approval:${event.approval.id}`,
                title: 'Approval needed',
                body: `${event.approval.title}.${grant}`,
                tab: 'chat',
                kind: 'approval',
                sound: 'notification',
              },
            }),
          );
        }
        break;
      }
      case 'approval_resolved': {
        if (!event.id || t.pendingApproval?.id === event.id) t.pendingApproval = undefined;
        this.emit(run, true);
        break;
      }
      case 'clarification_request': {
        t.pendingClarification = event.clarification;
        t.status = 'awaiting_approval';
        this.emit(run, true);
        if (typeof window !== 'undefined') {
          window.dispatchEvent(
            new CustomEvent(ALERT_NOTIFY_EVENT, {
              detail: {
                id: `clarification:${event.clarification.id}`,
                title: 'Eclipse has a question',
                body: event.clarification.question,
                tab: 'chat',
                kind: 'approval',
                sound: 'notification',
              },
            }),
          );
        }
        break;
      }
      case 'clarification_answered': {
        if (!event.id || t.pendingClarification?.id === event.id) t.pendingClarification = undefined;
        this.emit(run, true);
        break;
      }
      case 'analysis': {
        const incoming = event.result as Record<string, unknown> | null;
        const previous = t.analysis as Record<string, unknown> | null;
        if (incoming && previous && typeof incoming === 'object' && typeof previous === 'object') {
          t.analysis = {
            ...previous,
            ...incoming,
            outputs: {
              ...((previous.outputs as Record<string, unknown>) ?? {}),
              ...((incoming.outputs as Record<string, unknown>) ?? {}),
            },
          };
        } else {
          t.analysis = event.result;
        }
        this.emit(run, true);
        break;
      }
      case 'acquisition': {
        t.acquisition = event.manifest;
        this.emit(run, true);
        break;
      }
      case 'capability': {
        t.capabilityBuild = mergeCapabilityBuild(
          t.capabilityBuild as CapabilityBuildView | undefined,
          event.view,
          event.phase,
        );
        this.emit(run, true);
        break;
      }
      case 'response_plan': {
        t.responsePlan = event.plan;
        this.emit(run, true);
        break;
      }
      case 'delegation_start':
      case 'delegation_progress':
      case 'delegation_child':
      case 'delegation_done': {
        t.delegations = applyDelegationEvent(t.delegations ?? [], event);
        this.emit(run, true);
        break;
      }
    }
  }

  async start(args: StartRunArgs): Promise<AssistantTurn> {
    const chatId = args.chatId && args.chatId.length > 0 ? args.chatId : keyFor(args.chatId, this.tabId);
    const key = keyFor(chatId, this.tabId);
    const existing = this.runs.get(key);
    if (existing?.active) return existing.turn;

    // A run started by another tab: attach instead of starting a second one.
    const shared = getSharedRun(chatId);
    if (shared && shared.ownerTabId !== this.tabId && shared.runId !== PENDING_RUN) {
      return this.attach(chatId, shared.runId, args.onTurn, args.onDone);
    }

    // Two attempts cover the tiny window where the previous owner has finished
    // and released its record but the lock has not been re-observed yet.
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const locked = await withChatStartLock(chatId, () => this.startOwned(args, chatId));
      if (locked.acquired) return locked.value as AssistantTurn;

      const theirs = await this.waitForSharedRun(chatId);
      if (theirs) return this.attach(chatId, theirs.runId, args.onTurn, args.onDone);
    }
    return existing?.turn ?? emptyTurn();
  }

  private async startOwned(args: StartRunArgs, chatId: string): Promise<AssistantTurn> {
    const key = keyFor(chatId, this.tabId);
    const controller = new AbortController();
    const run: LiveRun = {
      chatId,
      controller,
      turn: emptyTurn(),
      activityState: initialActivityState(),
      snapshot: EMPTY_SNAPSHOT,
      active: true,
      runId: null,
      onTurn: args.onTurn,
      onDone: args.onDone,
      raf: null,
      releaseActivity: beginDesktopActivity(),
      owner: true,
    };
    run.turn.status = 'queued';
    this.runs.set(key, run);
    this.setSnapshot(run);
    this.notify();
    this.notifyLifecycle();
    args.onTurn?.(run.turn);

    const messages: ChatMessage[] = [
      ...args.history.map((m) => ({ role: m.role, content: m.content })),
      { role: 'user' as const, content: args.userText },
    ];

    try {
      const reply = await getAgentProvider().chat(messages, undefined, {
        chatId: args.chatId,
        model: args.model,
        reasoningEffort: args.reasoningEffort,
        resumeRunId: args.resumeRunId,
        uiContext: getUiFocus(),
        signal: controller.signal,
        onRunId: (id) => {
          run.runId = id;
          bindRunChat(id, chatId);
          rememberActiveRun(id, args.chatId);
          putSharedRun({
            chatId,
            runId: id,
            ownerTabId: this.tabId,
            at: Date.now(),
            status: 'running',
          });
          publishTabEvent({
            type: 'run.started',
            chatId,
            runId: id,
            ownerTabId: this.tabId,
          });
          // A heartbeat independent of rAF: a hidden owner tab must not look
          // dead to a follower (background frames are suspended).
          run.followerTimer = setInterval(() => {
            if (!run.active || !run.runId) return;
            this.broadcastSnapshot(run, true);
          }, 5_000);
          this.emit(run, true);
        },
        onEvent: (event) => this.applyEvent(run, event),
      });

      const t = run.turn;
      if (!t.done) {
        if (reply) {
          t.finalText = reply;
          t.status = 'completed';
        } else {
          t.status = 'cancelled';
        }
        t.done = true;
      }
    } catch (err) {
      const t = run.turn;
      if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
        t.status = 'cancelled';
      } else {
        t.status = 'error';
        const code = err instanceof ChatRequestError ? err.code : undefined;
        t.error = {
          message: err instanceof Error ? err.message : String(err),
          ...(code ? { code } : {}),
        };
      }
      t.done = true;
    } finally {
      run.active = false;
      run.releaseActivity?.();
      run.releaseActivity = null;
      if (run.followerTimer) {
        clearInterval(run.followerTimer);
        run.followerTimer = null;
      }
      if (run.runId) {
        unbindRunChat(run.runId);
        forgetActiveRun(run.runId);
        removeSharedRun(chatId, run.runId);
      }
      this.setSnapshot(run);
      this.notify();
      // Final snapshot first: a follower must see the answer before `done`.
      this.broadcastSnapshot(run, true);
      if (run.runId) {
        publishTabEvent({
          type: 'run.done',
          chatId,
          runId: run.runId,
          ownerTabId: this.tabId,
          status: run.turn.status,
        });
      }
      this.notifyLifecycle();
      run.onTurn?.(run.turn);
      run.onDone?.(run.turn);
    }

    return run.turn;
  }

  private async waitForSharedRun(chatId: string): Promise<SharedRun | null> {
    for (let i = 0; i < 20; i += 1) {
      const shared = getSharedRun(chatId);
      if (shared && shared.runId !== PENDING_RUN) return shared;
      await new Promise((resolve) => setTimeout(resolve, 40));
    }
    const last = getSharedRun(chatId);
    return last && last.runId !== PENDING_RUN ? last : null;
  }

  /**
   * Watch a run owned by another tab. Broadcast snapshots provide live tokens;
   * polling is the fallback when the owner tab disappears.
   */
  attach(
    chatId: string,
    runId: string,
    onTurn?: (turn: AssistantTurn) => void,
    onDone?: (turn: AssistantTurn) => void,
  ): Promise<AssistantTurn> {
    const key = keyFor(chatId, this.tabId);
    const existing = this.runs.get(key);
    if (existing?.active) return Promise.resolve(existing.turn);

    const run: LiveRun = {
      chatId,
      controller: new AbortController(),
      turn: emptyTurn(),
      activityState: initialActivityState(),
      snapshot: EMPTY_SNAPSHOT,
      active: true,
      runId,
      onTurn,
      onDone,
      raf: null,
      releaseActivity: beginDesktopActivity(),
      owner: false,
      followerTimer: null,
    };
    run.turn.status = 'streaming';
    this.runs.set(key, run);
    this.setSnapshot(run);
    this.notify();
    this.notifyLifecycle();

    const done = new Promise<AssistantTurn>((resolve) => {
      run.resolve = resolve;
    });
    void this.followShared(run);
    return done;
  }

  private async followShared(run: LiveRun): Promise<void> {
    while (run.active && run.runId) {
      try {
        const snapshot = await getRunSnapshot(run.runId);
        const status = typeof snapshot?.status === 'string' ? snapshot.status : '';
        if (status === 'completed' || status === 'failed' || status === 'cancelled') {
          const output = typeof snapshot?.output === 'string' ? snapshot.output : '';
          if (output) run.turn.finalText = output;
          run.turn.status = status === 'completed' ? 'completed' : status === 'failed' ? 'error' : 'cancelled';
          if (status === 'failed' && typeof snapshot?.error === 'string') {
            run.turn.error = { message: snapshot.error };
          }
          run.turn.done = true;
          this.finishFollower(run);
          return;
        }
        if (status && run.turn.status !== 'awaiting_approval') run.turn.status = 'streaming';
        this.emit(run, true);
      } catch {
        /* a transient poll failure is not the run's terminal state */
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }

  private finishFollower(run: LiveRun): void {
    if (!run.active) return;
    run.active = false;
    run.releaseActivity?.();
    run.releaseActivity = null;
    this.setSnapshot(run);
    this.notify();
    this.notifyLifecycle();
    run.onTurn?.(run.turn);
    run.onDone?.(run.turn);
    run.resolve?.(run.turn);
  }

  private onTabEvent(event: TabEvent): void {
    if (event.ownerTabId === this.tabId) return;
    const key = keyFor(event.chatId, this.tabId);
    const run = this.runs.get(key);
    if (event.type === 'run.started') {
      if (!run && event.chatId === this.focusedChatId) {
        void this.attach(event.chatId, event.runId);
      }
      return;
    }
    if (!run || run.owner || run.runId !== event.runId) return;
    if (event.type === 'run.snapshot') {
      run.turn = event.turn;
      this.setSnapshot(run);
      this.notify();
      run.onTurn?.(run.turn);
      return;
    }
    if (event.type === 'run.done' && run.active) {
      removeSharedRun(event.chatId, event.runId);
      this.finishFollower(run);
    }
  }

  cancel(chatId: string | null | undefined): void {
    const run = this.runs.get(keyFor(chatId, this.tabId));
    const shared = getSharedRun(chatId);
    const runId = run?.runId ?? shared?.runId;
    if (run?.active && !run.owner) {
      // A follower does not own the stream; ask the runtime directly.
      if (runId) {
        void fetch(`/api/harness/runs/${encodeURIComponent(runId)}/cancel`, {
          method: 'POST',
        }).catch(() => undefined);
      }
      return;
    }
    if (!run || !run.active) {
      if (runId && runId !== PENDING_RUN) {
        void fetch(`/api/harness/runs/${encodeURIComponent(runId)}/cancel`, {
          method: 'POST',
        }).catch(() => undefined);
      }
      return;
    }
    run.controller.abort();
    if (runId) {
      void fetch(`/api/harness/runs/${encodeURIComponent(runId)}/cancel`, {
        method: 'POST',
      }).catch(() => undefined);
    }
  }

  async decide(
    chatId: string | null | undefined,
    approve: boolean,
    remember = false,
  ): Promise<void> {
    const run = this.runs.get(keyFor(chatId, this.tabId));
    const pending: PendingApproval | undefined = run?.turn.pendingApproval;
    if (!run || !pending) return;
    run.turn.pendingApproval = undefined;
    if (run.turn.status === 'awaiting_approval') run.turn.status = 'tool_running';
    this.emit(run, true);
    try {
      const res = await fetch(
        `/api/harness/mcp/approvals/${encodeURIComponent(pending.id)}/decide`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ approve, remember: approve && remember }),
        },
      );
      if (!res.ok) throw new Error(`decision failed (${res.status})`);
    } catch {
      if (!run.turn.done) {
        run.turn.pendingApproval = pending;
        run.turn.status = 'awaiting_approval';
        this.emit(run, true);
      }
    }
  }

  async answer(chatId: string | null | undefined, value: string): Promise<void> {
    const run = this.runs.get(keyFor(chatId, this.tabId));
    const pending: PendingClarification | undefined = run?.turn.pendingClarification;
    if (!run || !pending) return;
    run.turn.pendingClarification = undefined;
    if (run.turn.status === 'awaiting_approval') run.turn.status = 'tool_running';
    this.emit(run, true);
    try {
      const res = await fetch(
        `/api/harness/mcp/approvals/${encodeURIComponent(pending.id)}/answer`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answer: value }),
        },
      );
      if (!res.ok) throw new Error(`answer failed (${res.status})`);
    } catch {
      if (!run.turn.done) {
        run.turn.pendingClarification = pending;
        run.turn.status = 'awaiting_approval';
        this.emit(run, true);
      }
    }
  }

  reset(chatId: string | null | undefined): void {
    const key = keyFor(chatId, this.tabId);
    const run = this.runs.get(key);
    if (run) {
      run.controller.abort();
      run.releaseActivity?.();
      run.releaseActivity = null;
      if (run.followerTimer) clearInterval(run.followerTimer);
      if (run.runId) unbindRunChat(run.runId);
      if (run.owner && run.runId) removeSharedRun(chatId ?? '', run.runId);
      if (run.raf != null) cancelAnimationFrame(run.raf);
      this.runs.delete(key);
      this.notify();
      this.notifyLifecycle();
    }
  }

  /**
   * Reattach to a run that is still executing after a reload/switch. One
   * recovery per conversation; a terminal record is discarded.
   */
  reattach(chatId: string | null | undefined): void {
    if (!chatId) return;
    const key = keyFor(chatId, this.tabId);
    if (this.runs.has(key)) return;
    const record = loadActiveRun(chatId);
    if (!record || Date.now() - record.at > 10 * 60_000) return;

    const controller = new AbortController();
    const run: LiveRun = {
      chatId,
      controller,
      turn: emptyTurn(),
      activityState: initialActivityState(),
      snapshot: EMPTY_SNAPSHOT,
      active: true,
      runId: record.runId,
      raf: null,
      releaseActivity: beginDesktopActivity(),
      owner: false,
    };
    run.turn.status = 'streaming';
    bindRunChat(record.runId, chatId);
    this.runs.set(key, run);
    this.setSnapshot(run);
    this.notify();
    this.notifyLifecycle();

    void (async () => {
      try {
        const snapshot = await getRunSnapshot(record.runId).catch(() => null);
        const status = typeof snapshot?.status === 'string' ? snapshot.status : '';
        if (!status || status === 'completed' || status === 'failed' || status === 'cancelled') {
          forgetActiveRun(record.runId);
          if (run.active) {
            run.active = false;
            run.turn.status = status === 'completed' ? 'completed' : 'cancelled';
            run.turn.done = true;
          }
          return;
        }
        if (status === 'interrupted') {
          // The run panel owns recovery for an interrupted run.
          run.active = false;
          run.releaseActivity?.();
          run.releaseActivity = null;
          unbindRunChat(record.runId);
          this.runs.delete(key);
          this.notify();
          this.notifyLifecycle();
          return;
        }
        const output = await recoverRun(record.runId, controller.signal);
        run.turn.finalText = output;
        run.turn.status = 'completed';
        run.turn.done = true;
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          run.turn.status = 'cancelled';
        } else {
          run.turn.status = 'error';
          run.turn.error = {
            message: err instanceof Error ? err.message : String(err),
          };
        }
        run.turn.done = true;
      } finally {
        run.active = false;
        run.releaseActivity?.();
        run.releaseActivity = null;
        unbindRunChat(record.runId);
        forgetActiveRun(record.runId);
        removeSharedRun(chatId, record.runId);
        this.setSnapshot(run);
        this.notify();
        this.notifyLifecycle();
      }
    })();
  }
}

export const runManager = new RunManager();
