'use client';

// ── Bot chat ─────────────────────────────────────────────────────────────────
// The bot workspace's main surface: a conversation WITH the bot. You can rename
// it and edit its purpose inline, chat with it (real agent turns through
// `/api/harness/chat`, with the bot's identity + instructions as standing
// context), run it as an autonomous task now, and open Details for its schedule,
// capabilities, memory and full run history. Modeled on Grok/Hermes bots.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import {
  ArrowLeft, ArrowUp, CalendarDays, CheckCircle2, Copy, FileText, Loader2, Pause, Play,
  Plus, RefreshCw, Settings, SquarePen, ThumbsDown, ThumbsUp, X,
} from '@/lib/icons';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Sheet, SheetContent } from '@/components/ui/sheet';
import { Markdown } from '@/components/chat/markdown';
import { TtsButton } from '@/components/chat/tts-button';
import { ParallelAgentsPanel } from '@/components/chat/parallel-agents-panel';
import { ApprovalCard } from '@/components/chat/approval-card';
import { ClarificationCard } from '@/components/chat/clarification-card';
import { TradeApprovalBanner } from '@/components/chat/trade-approval-banner';
import type { DelegationView } from '@/lib/agent/response-model';
import type { HarnessUiEvent } from '@/lib/agent/events';
import {
  applyDelegationEvent,
  dropProvisionalDelegation,
  startProvisionalDelegation,
} from '@/lib/agent/delegation-reducer';
import { botChatId, runBotTurn } from '@/lib/bots/invoke';
import {
  deleteSessionFile, fetchSession, uploadSessionFile,
} from '@/lib/chat/session-client';
import type { SessionFile } from '@/lib/chat/session-types';
import { listAutomationHistory, type ExecutionView } from '@/lib/harness/clients/automation-browser';
import { BotAvatar } from '@/components/bots/bot-avatar';
import { BotDetail } from '@/components/bots/bot-detail';
import { BotsTextSizeMenu } from '@/components/bots/bots-text-size';
import { BreakerPill, LifecyclePill } from '@/components/bots/status-pill';
import {
  pauseAutomation, resumeAutomation, updateAutomation,
  type AutomationView,
} from '@/lib/harness/clients/automation-browser';
import { avatarFor, useBotAvatarStore, useHydrateBotAvatars } from '@/lib/bots/avatar-store';
import { moodFromEvent, type GhostMood } from '@/lib/bots/ghost-mood';
import { useBotChatStore, useHydrateBotChat, type BotMessage } from '@/lib/bots/chat-store';
import { useBotsStore } from '@/lib/bots/bots-store';
import { isQuietAnswer } from '@/lib/bots/quiet';
import { useWorkspaceStore } from '@/lib/agent-workspace/store';
import {
  buildTrigger, detectSchedule, emptyBotDraft, formatDateTime, formatRelative, scheduleSummary,
} from '@/lib/bots/state';

export interface BotChatProps {
  bot: AutomationView;
  /** False while the Bots tab is hidden behind the keep-alive mount. */
  active?: boolean;
  busy: boolean;
  refreshKey?: number;
  onClose: () => void;
  onChanged: () => void;
  onRunNow: (bot: AutomationView) => Promise<boolean> | boolean | void;
  onDuplicate: (bot: AutomationView) => void;
  onDelete: (bot: AutomationView) => void;
}

function newId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// A module-level empty array so the zustand selector returns a STABLE reference
// when a bot has no messages yet. Returning `?? []` inline creates a new array
// every render, which breaks useSyncExternalStore's snapshot caching and throws
// "Maximum update depth exceeded".
const NO_MESSAGES: BotMessage[] = [];

export function BotChat({
  bot, active = true, busy, refreshKey = 0, onClose, onChanged, onRunNow, onDuplicate, onDelete,
}: BotChatProps) {
  const t = useT();
  useHydrateBotAvatars();
  useHydrateBotChat();
  const avatars = useBotAvatarStore((s) => s.avatars);
  const messages = useBotChatStore((s) => s.byBot[bot.id] ?? NO_MESSAGES);
  const append = useBotChatStore((s) => s.append);
  const appendScheduled = useBotChatStore((s) => s.appendScheduled);
  const updateMessage = useBotChatStore((s) => s.update);
  const clearMessages = useBotChatStore((s) => s.clear);
  const setMessages = useBotChatStore((s) => s.setMessages);
  // Clears this bot's unread dot in the sidebar once its latest run is on screen.
  const markSeen = useBotsStore((s) => s.markSeen);
  // The scheduler sits beside the bot's Settings gear in the header, opening the
  // right-docked Agent Workspace (its scheduled jobs rail). Shared state with the
  // top-bar toggle and Ctrl/Cmd+Shift+E keybind keeps them in sync.
  const schedulerOpen = useWorkspaceStore((s) => s.open);
  const toggleScheduler = useWorkspaceStore((s) => s.toggle);
  // Interactive turns use the bot's own brain and pinned provider/model
  // (Details → Schedule), never the app-wide composer default. There is no
  // inline global selector here: a bot's model must not change every other chat.
  // The pin is applied in `runBotTurn` so chat and scheduled runs agree.

  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  // The face the header avatar wears while a turn runs (idle at rest).
  const [mood, setMood] = useState<GhostMood>('idle');
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [name, setName] = useState(bot.name);
  const [description, setDescription] = useState(bot.description ?? '');
  const [savingIdentity, setSavingIdentity] = useState(false);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [runBusy, setRunBusy] = useState(false);
  const [runNote, setRunNote] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Anchor for the approval / clarification cards (they portal above it).
  const [composerEl, setComposerEl] = useState<HTMLDivElement | null>(null);
  // Documents attached to this bot's conversation. They live in the SAME
  // `bot-<id>` session the chat run uses, so the front door injects them exactly
  // as it does for main chat — no bot-specific attachment path.
  const attachInputRef = useRef<HTMLInputElement | null>(null);
  const botSessionId = botChatId(bot.id);
  const [contextFiles, setContextFiles] = useState<SessionFile[]>([]);
  const [attachBusy, setAttachBusy] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);
  // Execution ids already reflected in the thread, so a re-poll does not
  // re-post an autonomous answer. `null` means "not yet seeded from history".
  const seenRunsRef = useRef<Set<string> | null>(null);

  useEffect(() => {
    setName(bot.name);
    setDescription(bot.description ?? '');
  }, [bot.id, bot.name, bot.description]);

  // Stop the in-flight run if the surface goes away so the stream is not orphaned.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Load the documents already attached to this bot's conversation.
  useEffect(() => {
    let cancelled = false;
    void fetchSession(botSessionId)
      .then((session) => {
        if (!cancelled) setContextFiles(session.files.filter((f) => f.kind === 'context'));
      })
      .catch(() => { /* no session yet */ });
    return () => { cancelled = true; };
  }, [botSessionId]);

  const refreshAttachments = useCallback(async () => {
    try {
      const session = await fetchSession(botSessionId);
      setContextFiles(session.files.filter((f) => f.kind === 'context'));
    } catch {
      /* keep the current list */
    }
  }, [botSessionId]);

  const attachFiles = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setAttachBusy(true);
    setAttachError(null);
    try {
      for (const file of Array.from(files)) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        await uploadSessionFile(botSessionId, 'context', file.name, bytes);
      }
      await refreshAttachments();
    } catch (err) {
      setAttachError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setAttachBusy(false);
      if (attachInputRef.current) attachInputRef.current.value = '';
    }
  }, [botSessionId, refreshAttachments]);

  const removeAttachment = useCallback(async (name: string) => {
    setAttachError(null);
    try {
      await deleteSessionFile(botSessionId, 'context', name);
      await refreshAttachments();
    } catch (err) {
      setAttachError(err instanceof Error ? err.message : 'Could not remove the attachment');
    }
  }, [botSessionId, refreshAttachments]);

  const instructions = bot.action.objective ?? bot.description ?? '';
  const dirty = name.trim() !== bot.name || description.trim() !== (bot.description ?? '');
  // Run now only works on an active (enabled) automation, so we can explain the
  // disabled state instead of leaving a dead button with no reason.
  // A paused bot is still runnable ON DEMAND: pausing stops scheduled fires,
  // not an explicit "Run now". Draft/disabled/archived stay blocked.
  const canRun = bot.state === 'active' || bot.state === 'paused';
  // The latest blocked turn, if any. Sourced from the thread (not a transient
  // flag) so the card survives re-renders and a completed card never lingers.
  const pendingMessage = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].pendingApproval || messages[i].pendingClarification) return messages[i];
    }
    return null;
  }, [messages]);
  const pendingApproval = pendingMessage?.pendingApproval;
  const pendingClarification = pendingMessage?.pendingClarification;

  // A bot whose instructions NAME a cadence ("…every 3 mins") but whose trigger
  // is still MANUAL will never run on its own — exactly what happens to a bot
  // created before the description parser existed. Offer a one-click fix rather
  // than silently rescheduling (or leaving it dead).
  const suggested = useMemo(() => {
    if (bot.trigger.type !== 'manual') return null;
    return detectSchedule(instructions);
  }, [bot.trigger.type, instructions]);

  // The thread reads top-to-bottom, so the newest message lives at the BOTTOM.
  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, []);

  // Follow the newest message, including the streamed content of the live reply.
  const lastContent = messages.length > 0 ? messages[messages.length - 1].content : '';
  useEffect(() => {
    scrollToBottom();
  }, [messages.length, sending, lastContent, scrollToBottom]);

  const saveIdentity = useCallback(async () => {
    if (savingIdentity) return;
    if (!name.trim()) return;
    setSavingIdentity(true);
    setIdentityError(null);
    try {
      await updateAutomation(bot.id, { name: name.trim(), description: description.trim() });
      setEditOpen(false);
      onChanged();
    } catch (err) {
      setIdentityError(err instanceof Error ? err.message : 'save failed');
    } finally {
      setSavingIdentity(false);
    }
  }, [savingIdentity, name, description, bot.id, onChanged]);

  const doAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      setActionBusy(true);
      setActionError(null);
      try {
        await fn();
        onChanged();
      } catch (err) {
        setActionError(err instanceof Error ? err.message : 'action failed');
      } finally {
        setActionBusy(false);
      }
    },
    [onChanged],
  );

  // Apply the cadence named in the bot's own instructions as its real trigger.
  const applySuggestedSchedule = useCallback(() => {
    if (!suggested) return;
    const trigger = buildTrigger({ ...emptyBotDraft(), ...suggested.patch });
    return doAction(() => updateAutomation(bot.id, { trigger }));
  }, [suggested, doAction, bot.id]);

  // Queue a run and give visible feedback: the API only enqueues server-side, so
  // without a note the click looked like it did nothing. Cleared automatically.
  const handleRunNow = useCallback(async () => {
    if (runBusy || busy || actionBusy || !canRun) return;
    setRunBusy(true);
    setRunNote(null);
    try {
      const ok = await onRunNow(bot);
      setRunNote(ok === false ? t('bots.runFailed') : t('bots.runQueued'));
    } catch (err) {
      setRunNote(err instanceof Error ? err.message : 'run failed');
    } finally {
      setRunBusy(false);
    }
  }, [runBusy, busy, actionBusy, canRun, onRunNow, bot, t]);

  useEffect(() => {
    if (!runNote) return;
    const timer = setTimeout(() => setRunNote(null), 6000);
    return () => clearTimeout(timer);
  }, [runNote]);

  // A finished turn (happy or sad) is a moment, not a state: let the face rest again.
  useEffect(() => {
    if (mood !== 'happy' && mood !== 'sad') return;
    const timer = setTimeout(() => setMood('idle'), 1800);
    return () => clearTimeout(timer);
  }, [mood]);

  // Auto-grow the composer with its content, up to a cap, then scroll.
  const resizeComposer = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);
  useEffect(() => {
    resizeComposer();
  }, [input, resizeComposer]);

  // Surface AUTONOMOUS runs into the thread. The composer only ever shows turns
  // you typed; a scheduled/event/condition/"Run now" run finishes inside the
  // runtime and its answer would otherwise be invisible here (it lived only in
  // Details → Run history and a toast). Poll the bot's execution history and
  // append every terminal run that produced output — once each, keyed by
  // execution id. The first sweep seeds from history WITHOUT replaying it (only
  // the latest answer is surfaced, so an old thread is not dumped into view).
  useEffect(() => {
    // Hidden keep-alive tab: do not poll history in the background.
    if (!active) return;
    seenRunsRef.current = null;
    let cancelled = false;

    const sync = async () => {
      let executions: ExecutionView[];
      try {
        executions = await listAutomationHistory(bot.id);
      } catch {
        return; // runtime down / not ready — try again on the next tick
      }
      if (cancelled) return;
      const finished = executions
        .filter((e) => (e.state === 'completed' || e.state === 'failed') && (e.output ?? '').trim())
        // A quiet run ("nothing changed") is kept in run history but never
        // posted into the conversation as if it were a report.
        .filter((e) => !isQuietAnswer(e.output))
        .sort(
          (a, b) =>
            Date.parse(a.finishedAt ?? a.startedAt) - Date.parse(b.finishedAt ?? b.startedAt),
        );
      if (seenRunsRef.current === null) {
        seenRunsRef.current = new Set(finished.map((e) => e.id));
        const latest = finished[finished.length - 1];
        if (latest) postRun(latest);
        return;
      }
      const seen = seenRunsRef.current;
      for (const e of finished) {
        if (seen.has(e.id)) continue;
        seen.add(e.id);
        postRun(e);
      }
    };

    const postRun = (e: ExecutionView) => {
      const text = (e.output ?? '').trim();
      if (!text) return;
      // appendScheduled dedupes by runId, so a reseed can never double-post.
      appendScheduled(bot.id, {
        id: `run-${e.id}`,
        role: 'assistant',
        content: text,
        at: e.finishedAt ?? e.startedAt,
        scheduled: true,
        runId: e.id,
        ...(e.state === 'failed' ? { error: true } : {}),
      });
    };

    void sync();
    const timer = setInterval(sync, 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [bot.id, active, appendScheduled]);

  // Instagram-style read receipt: while this bot's thread is open, its latest run
  // is "seen". We key off the SAME id the sidebar flags as unread
  // (`statusById[id].lastExecution.id`) so opening the bot always clears its dot,
  // and a run that lands while you are looking at the thread never raises one.
  const lastRunId = useBotsStore((s) => s.statusById[bot.id]?.lastExecution?.id ?? null);
  useEffect(() => {
    if (lastRunId) markSeen(bot.id, lastRunId);
  }, [lastRunId, bot.id, markSeen]);

  const runTurn = useCallback(async (raw: string) => {
    const text = raw.trim();
    if (!text || sending || attachBusy) return;
    // Read the FRESH thread (not the render snapshot) so an edit/regenerate that
    // truncated the store is respected, then append the new user turn.
    const prior = useBotChatStore.getState().byBot[bot.id] ?? [];
    append(bot.id, { id: newId(), role: 'user', content: text, at: new Date().toISOString() });
    setInput('');
    setSending(true);
    setMood('thinking');
    // Placeholder the stream fills in place, so a long tool-heavy run shows the
    // answer as it is written instead of a blank wait.
    const replyId = newId();
    append(bot.id, { id: replyId, role: 'assistant', content: '', at: new Date().toISOString() });

    const controller = new AbortController();
    abortRef.current = controller;
    let streamed = '';
    // The same fan-out state the main chat keeps, captured from the turn's
    // events so the bot thread can render the live agent card.
    let delegations: DelegationView[] = [];
    const applyDelegation = (event: HarnessUiEvent) => {
      if (event.kind === 'activity_start' && event.item.name === 'delegate') {
        delegations = startProvisionalDelegation(delegations, event.item.id);
      } else if (event.kind === 'activity_end' && event.name === 'delegate') {
        delegations = dropProvisionalDelegation(delegations);
      } else if (
        event.kind === 'delegation_start' ||
        event.kind === 'delegation_progress' ||
        event.kind === 'delegation_child' ||
        event.kind === 'delegation_done'
      ) {
        delegations = applyDelegationEvent(delegations, event);
      } else if (event.kind === 'final' || event.kind === 'error') {
        delegations = dropProvisionalDelegation(delegations);
      } else {
        return;
      }
      updateMessage(bot.id, replyId, { delegations }, false);
    };
    try {
      // The streaming agent provider (background run + SSE), NOT the one-shot
      // `/api/harness/chat`: that path aborts every run at the front door's
      // 180s cap, so a real research turn (e.g. "find me a good psx stock",
      // ~4 min, dozens of tool calls) died as "Failed to fetch". Here the run
      // is observed live and survives its full duration. The persona/history and
      // the private `bot-<id>` memory scope live in the shared `runBotTurn`.
      const reply = await runBotTurn({
        bot,
        text,
        prior,
        signal: controller.signal,
        onEvent: (event) => {
          applyDelegation(event);
          // A blocked turn must be actionable HERE, not only in main chat: a
          // permission decision or a `clarify` question otherwise strands the
          // run with no way to answer it.
          if (event.kind === 'approval_request') {
            updateMessage(bot.id, replyId, { pendingApproval: event.approval }, false);
          } else if (event.kind === 'approval_resolved') {
            updateMessage(bot.id, replyId, { pendingApproval: undefined }, false);
          } else if (event.kind === 'clarification_request') {
            updateMessage(bot.id, replyId, { pendingClarification: event.clarification }, false);
          } else if (event.kind === 'clarification_answered') {
            updateMessage(bot.id, replyId, { pendingClarification: undefined }, false);
          }
          const next = moodFromEvent(event);
          if (next) setMood(next);
        },
        onDelta: (chunk) => {
          streamed += chunk;
          setMood('speaking');
          // Streaming frames update memory only; the terminal update below
          // persists the finished thread once instead of on every token.
          updateMessage(bot.id, replyId, { content: streamed }, false);
        },
      });
      const final = reply || streamed;
      updateMessage(
        bot.id,
        replyId,
        final
          ? { content: final, pendingApproval: undefined, pendingClarification: undefined }
          : { content: 'The bot did not return a reply.', error: true, pendingApproval: undefined, pendingClarification: undefined },
      );
      setMood(final ? 'happy' : 'sad');
    } catch (err) {
      if (controller.signal.aborted) {
        updateMessage(bot.id, replyId, {
          ...(streamed ? { content: streamed } : { content: '(cancelled)', error: true }),
          pendingApproval: undefined,
          pendingClarification: undefined,
        });
      } else {
        updateMessage(bot.id, replyId, {
          content: err instanceof Error ? err.message : 'network error',
          error: true,
          pendingApproval: undefined,
          pendingClarification: undefined,
        });
      }
      setMood('sad');
    } finally {
      abortRef.current = null;
      setSending(false);
    }
  }, [sending, attachBusy, append, updateMessage, bot]);

  const send = useCallback(() => {
    void runTurn(input);
  }, [runTurn, input]);

  /** Stop the in-flight reply (keeps whatever streamed so far). */
  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /** Answer a blocking approval raised by this bot's turn. */
  const decideApproval = useCallback(async (approve: boolean, remember?: boolean) => {
    const target = pendingMessage;
    const approval = target?.pendingApproval;
    if (!target || !approval) return;
    // Clear optimistically, restore on failure so the operator can retry.
    updateMessage(bot.id, target.id, { pendingApproval: undefined }, false);
    try {
      const res = await fetch(
        `/api/harness/mcp/approvals/${encodeURIComponent(approval.id)}/decide`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ approve, remember: approve && Boolean(remember) }),
        },
      );
      if (!res.ok) throw new Error(`decision failed (${res.status})`);
    } catch {
      updateMessage(bot.id, target.id, { pendingApproval: approval }, false);
    }
  }, [bot.id, pendingMessage, updateMessage]);

  /** Answer a paused `clarify` question from this bot's turn. */
  const answerClarification = useCallback(async (answer: string) => {
    const target = pendingMessage;
    const clarification = target?.pendingClarification;
    if (!target || !clarification) return;
    updateMessage(bot.id, target.id, { pendingClarification: undefined }, false);
    try {
      const res = await fetch(
        `/api/harness/mcp/approvals/${encodeURIComponent(clarification.id)}/answer`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answer }),
        },
      );
      if (!res.ok) throw new Error(`answer failed (${res.status})`);
    } catch {
      updateMessage(bot.id, target.id, { pendingClarification: clarification }, false);
    }
  }, [bot.id, pendingMessage, updateMessage]);

  /** Edit a sent message: truncate the thread there and load it into the composer. */
  const editMessage = useCallback(
    (id: string) => {
      const idx = messages.findIndex((m) => m.id === id);
      if (idx < 0) return;
      setMessages(bot.id, messages.slice(0, idx));
      setInput(messages[idx].content);
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [messages, setMessages, bot.id],
  );

  /** Re-run the last user prompt (drops the previous answer). */
  const regenerate = useCallback(() => {
    const msgs = useBotChatStore.getState().byBot[bot.id] ?? [];
    for (let i = msgs.length - 1; i >= 0; i -= 1) {
      if (msgs[i].role === 'user') {
        const text = msgs[i].content;
        setMessages(bot.id, msgs.slice(0, i));
        void runTurn(text);
        return;
      }
    }
  }, [bot.id, setMessages, runTurn]);

  /** Thumbs feedback on an assistant turn; toggling the same rating clears it. */
  const setFeedback = useCallback(
    (id: string, fb: 'up' | 'down') => {
      const current = (useBotChatStore.getState().byBot[bot.id] ?? []).find((m) => m.id === id);
      updateMessage(bot.id, id, { feedback: current?.feedback === fb ? undefined : fb });
    },
    [bot.id, updateMessage],
  );

  const lastRun = useMemo(() => bot.lastRunAt, [bot.lastRunAt]);
  const spec = avatarFor(avatars, bot.id);

  return (
    <div className="glass-card flex h-full min-h-0 flex-col rounded-2xl">
      {/* Header — bot identity + autonomous controls (compact banner). */}
      <div className="flex items-center gap-2.5 border-b border-border/10 px-3 py-2">
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg p-1.5 text-muted-foreground hover:bg-white/10 hover:text-foreground lg:hidden"
          aria-label={t('common.close')}
        >
          <ArrowLeft size={16} />
        </button>
        <button
          type="button"
          onClick={() => setDetailsOpen(true)}
          title={t('bots.customize')}
          className="shrink-0 rounded-2xl transition-transform hover:scale-105"
        >
          <BotAvatar botId={bot.id} spec={spec} size={36} title={bot.name} live mood={mood} />
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <h2 className="truncate text-sm font-semibold">{bot.name}</h2>
            <button
              type="button"
              onClick={() => {
                setName(bot.name);
                setDescription(bot.description ?? '');
                setIdentityError(null);
                setEditOpen(true);
              }}
              className="flex h-6 w-6 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-white/10 hover:text-foreground active:scale-95"
              aria-label={t('bots.edit')}
              title={t('bots.edit')}
            >
              <SquarePen size={14} />
            </button>
            <LifecyclePill state={bot.state} />
            {bot.action.type === 'execute_workflow' && (
              <span
                title="This bot answers by running its workflow"
                className="rounded-full border border-primary/25 bg-primary/10 px-1.5 py-0.5 text-[9px] font-medium text-primary"
              >
                Workflow
              </span>
            )}
            {bot.breaker !== 'closed' && <BreakerPill state={bot.breaker} />}
          </div>
          <p className="mt-0.5 line-clamp-1 text-[11px] text-muted-foreground">
            {bot.description || t('bots.emptyBody')}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[10px] text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <CalendarDays size={11} /> {scheduleSummary(bot.trigger)}
            </span>
            {bot.nextRunAt && (
              <span>{t('bots.nextRun')}: <span className="text-foreground/80">{formatRelative(bot.nextRunAt)}</span></span>
            )}
            <span>
              {t('bots.lastRun')}: {lastRun ? formatRelative(lastRun) : t('bots.never')}
            </span>
          </div>
        </div>

        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <div className="flex items-center gap-1.5">
            <Button
              size="sm"
              disabled={busy || actionBusy || runBusy || !canRun}
              title={canRun ? undefined : t('bots.runNowBlocked')}
              onClick={() => void handleRunNow()}
            >
              {runBusy ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />} {t('bots.runNow')}
            </Button>
            {bot.state === 'active' ? (
              <Button
                size="icon-sm"
                variant="outline"
                disabled={actionBusy}
                title={t('bots.pause')}
                aria-label={t('bots.pause')}
                onClick={() => doAction(() => pauseAutomation(bot.id))}
              >
                <Pause size={14} />
              </Button>
            ) : bot.state === 'paused' ? (
              <Button
                size="icon-sm"
                variant="outline"
                disabled={actionBusy}
                title={t('bots.resume')}
                aria-label={t('bots.resume')}
                onClick={() => doAction(() => resumeAutomation(bot.id))}
              >
                <Play size={14} />
              </Button>
            ) : null}
            <Button
              size="icon-sm"
              variant="ghost"
              title={t('bots.jobsPanel.open')}
              aria-label={t('bots.jobsPanel.open')}
              aria-pressed={schedulerOpen}
              onClick={toggleScheduler}
              className={schedulerOpen ? 'text-primary bg-white/[0.08]' : ''}
            >
              <CalendarDays size={14} />
            </Button>
            <BotsTextSizeMenu className="p-1.5" />
            <Button
              size="icon-sm"
              variant="ghost"
              title={t('bots.detail.settings')}
              aria-label={t('bots.detail.settings')}
              onClick={() => setDetailsOpen(true)}
            >
              <Settings size={14} />
            </Button>
          </div>
          {actionError && <span className="text-[10px] text-rose-300">{actionError}</span>}
        </div>
      </div>

      {/* Thread */}
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-5">
        {messages.length === 0 && (
          <div className="mx-auto max-w-md py-8 text-center">
            <BotAvatar botId={bot.id} spec={spec} size={64} className="mx-auto" />
            <p className="mt-3 text-sm font-medium">{bot.name}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {instructions || t('bots.emptyBody')}
            </p>
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              <SuggestionChip label="Summarize what you can do" onPick={setInput} />
              <SuggestionChip label="Run your task now" onPick={setInput} />
              <SuggestionChip label="Change your schedule" onPick={setInput} />
            </div>
          </div>
        )}

        {runNote && (
          <div className="glass-card-subtle mx-auto w-fit rounded-full px-3 py-1 text-[11px] text-muted-foreground">
            {runNote}
          </div>
        )}

        {/* Oldest first: the question is on top, its answer below it. */}
        {messages.map((m, i) => {
          return !m.error && m.role === 'assistant' && !m.content ? null : (
            <ChatBubble
              key={m.id}
              message={m}
              botId={bot.id}
              botName={bot.name}
              spec={spec}
              onEdit={editMessage}
              onFeedback={setFeedback}
              onRegenerate={
                i === messages.length - 1 && m.role === 'assistant' && !m.error && !m.scheduled
                  ? regenerate
                  : undefined
              }
              onRetry={
                i === messages.length - 1 && m.role === 'assistant' && m.error && !m.scheduled
                  ? regenerate
                  : undefined
              }
            />
          );
        })}

        {sending && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={14} className="animate-spin" />
            {bot.name} is thinking…
          </div>
        )}
      </div>

      {/* Composer */}
      <div ref={setComposerEl} className="border-t border-border/10 p-3">
        {/* The SAME trade approval surface the main chat composer mounts: a
            pending `order_preview` is the operator's half of the order contract,
            and a bot conversation must be able to complete it in place rather
            than sending the user to another screen. Store-driven and
            self-polling, so a scheduled run's proposal appears here too. */}
        <TradeApprovalBanner automationId={bot.id} />
        {pendingApproval && (
          <ApprovalCard
            approval={pendingApproval}
            anchor={composerEl}
            onDecide={(approve, remember) => { void decideApproval(approve, remember); }}
          />
        )}
        {pendingClarification && (
          <ClarificationCard
            clarification={pendingClarification}
            anchor={composerEl}
            onAnswer={(value) => { void answerClarification(value); }}
          />
        )}
        <input
          ref={attachInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => void attachFiles(e.target.files)}
        />
        {(contextFiles.length > 0 || attachError) && (
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            {contextFiles.map((file) => (
              <span
                key={file.name}
                className="inline-flex max-w-full items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] px-2 py-1 text-[10px] text-foreground/80"
                title={file.name}
              >
                <FileText size={11} className="shrink-0 text-primary/80" />
                <span className="max-w-[12rem] truncate">{file.name}</span>
                <button
                  type="button"
                  onClick={() => void removeAttachment(file.name)}
                  className="shrink-0 rounded-full p-0.5 text-muted-foreground hover:text-foreground"
                  aria-label={`Remove ${file.name}`}
                >
                  <X size={10} />
                </button>
              </span>
            ))}
            {attachBusy && <Loader2 size={12} className="animate-spin text-muted-foreground" />}
            {attachError && <span className="text-[10px] text-rose-300">{attachError}</span>}
          </div>
        )}
        {suggested && (
          <div className="mb-2 flex items-center gap-2 rounded-xl border border-primary/25 bg-primary/[0.06] px-3 py-2 text-[11px] text-foreground/90">
            <CalendarDays size={13} className="shrink-0 text-primary" />
            <span className="min-w-0 flex-1">
              {t('bots.scheduleSuggestion', { schedule: suggested.summary })}
            </span>
            <Button
              size="sm"
              disabled={actionBusy}
              onClick={() => void applySuggestedSchedule()}
            >
              {t('bots.scheduleApply')}
            </Button>
          </div>
        )}
        <div className="relative flex w-full items-end gap-1.5 rounded-3xl border border-white/10 glass-composer px-1.5 py-1 shadow-lg shadow-black/20 transition-colors focus-within:border-primary sm:px-2 dark:border-white/5">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send();
              }
            }}
            rows={1}
            data-bot-composer="true"
            placeholder={`Message ${bot.name}…`}
            aria-label={`Message ${bot.name}`}
            wrap={input.length === 0 ? 'off' : 'soft'}
            className="chat-composer-scroll min-w-0 flex-1 resize-none self-center overflow-x-hidden overflow-y-auto border-0 bg-transparent px-3 py-2 text-sm leading-relaxed text-foreground outline-none placeholder:text-muted-foreground focus:outline-none focus:ring-0"
          />
          <div className="mb-0.5 flex shrink-0 items-center gap-1 sm:gap-2">
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => attachInputRef.current?.click()}
              disabled={attachBusy || sending}
              title="Attach a document"
              aria-label="Attach a document"
            >
              <Plus size={14} />
            </Button>
            {sending ? (
              <Button
                size="icon"
                onClick={stop}
                title={t('bots.chat.stop')}
                aria-label={t('bots.chat.stop')}
                className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-full border-0 bg-foreground p-1.5 text-background transition-colors hover:bg-foreground/90 focus-visible:outline-none focus-visible:ring-0"
              >
                <Pause size={15} fill="currentColor" />
              </Button>
            ) : (
              <Button
                size="icon"
                onClick={send}
                disabled={!input.trim() || attachBusy}
                aria-label={`Message ${bot.name}`}
                className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-full bg-primary p-1.5 text-primary-foreground transition-opacity hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-0 disabled:opacity-40 disabled:hover:bg-primary"
              >
                <ArrowUp size={16} />
              </Button>
            )}
          </div>
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
          <span className="truncate">{t('bots.schedule')}: {scheduleSummary(bot.trigger)}</span>
          <span className="hidden shrink-0 sm:inline">{t('bots.chat.hint')}</span>
          {messages.length > 0 && (
            <button
              type="button"
              onClick={() => clearMessages(bot.id)}
              className="hover:text-foreground"
            >
              {t('bots.chat.clear')}
            </button>
          )}
        </div>
      </div>

      {/* Details drawer — schedule, capabilities, memory, history, settings. */}
      <Sheet open={detailsOpen} onOpenChange={setDetailsOpen}>
        <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-3xl">
          <div className="h-full p-4">
            <BotDetail
              bot={bot}
              busy={busy}
              refreshKey={refreshKey}
              onClose={() => setDetailsOpen(false)}
              onChanged={onChanged}
              onRunNow={onRunNow}
              onDuplicate={onDuplicate}
              onDelete={(b) => { setDetailsOpen(false); onDelete(b); }}
            />
          </div>
        </SheetContent>
      </Sheet>

      {/* Edit bot — a proper dialog for identity, not an inline header form. */}
      <Dialog open={editOpen} onOpenChange={(open) => { if (!savingIdentity) setEditOpen(open); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('bots.edit')}</DialogTitle>
            <DialogDescription>{t('bots.dialog.subtitle')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <label htmlFor="bot-edit-name" className="mb-1 block text-xs font-medium text-muted-foreground">
                {t('bots.create.name')}
              </label>
              <Input
                id="bot-edit-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('bots.namePlaceholder')}
                autoFocus
              />
            </div>
            <div>
              <label htmlFor="bot-edit-description" className="mb-1 block text-xs font-medium text-muted-foreground">
                {t('bots.create.purpose')}
              </label>
              <textarea
                id="bot-edit-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={5}
                placeholder={t('bots.objectivePlaceholder')}
                className="min-h-24 w-full resize-y rounded-lg border border-input bg-transparent px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
              />
            </div>
            {identityError && <p className="text-xs text-rose-300">{identityError}</p>}
          </div>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => { setEditOpen(false); setIdentityError(null); }}
              disabled={savingIdentity}
            >
              {t('bots.cancelEdit')}
            </Button>
            <Button onClick={() => void saveIdentity()} disabled={savingIdentity || !name.trim() || !dirty}>
              {savingIdentity ? t('bots.saving') : t('bots.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function SuggestionChip({ label, onPick }: { label: string; onPick: (v: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onPick(label)}
      className="rounded-full border border-border/50 px-3 py-1 text-[11px] text-muted-foreground hover:border-border hover:text-foreground"
    >
      {label}
    </button>
  );
}

/** A small tag marking an assistant turn that came from an autonomous run
 *  (schedule/event/condition/"Run now") rather than the interactive composer. */
function ScheduledTag() {
  const t = useT();
  return (
    <span className="rounded-full border border-border/40 px-1.5 py-px text-[9px] font-normal uppercase tracking-wide text-muted-foreground/70">
      {t('bots.chat.autoRun')}
    </span>
  );
}

function ChatBubble({
  message,
  botId,
  botName,
  spec,
  onEdit,
  onRegenerate,
  onRetry,
  onFeedback,
}: {
  message: BotMessage;
  botId: string;
  botName: string;
  spec: import('@/lib/bots/ghost').GhostSpec;
  onEdit: (id: string) => void;
  onRegenerate?: () => void;
  onRetry?: () => void;
  onFeedback?: (id: string, fb: 'up' | 'down') => void;
}) {
  const isUser = message.role === 'user';
  // A bot reply is the room's/main content: it reads as a full-width document
  // with no glass surface. Only the user's own turns (and honest error turns)
  // are framed as bubbles.
  if (!isUser && !message.error) {
    return (
      <div className="group flex gap-2.5">
        <BotAvatar botId={botId} spec={spec} size={28} className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
            <span>{botName}</span>
            {message.scheduled && <ScheduledTag />}
          </div>
          {message.delegations && message.delegations.length > 0 && (
            <ParallelAgentsPanel delegations={message.delegations} compact />
          )}
          <Markdown content={message.content} compact className="w-full" />
          <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
            <span>{formatDateTime(message.at)}</span>
            <MessageActions
              content={message.content}
              isUser={false}
              feedback={message.feedback}
              onFeedback={onFeedback ? (fb) => onFeedback(message.id, fb) : undefined}
              onRegenerate={onRegenerate}
            />
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className={cn('group flex gap-2.5', isUser ? 'flex-row-reverse' : 'flex-row')}>
      {!isUser && <BotAvatar botId={botId} spec={spec} size={28} className="mt-0.5 shrink-0" />}
      <div className={cn('flex min-w-0 max-w-[80%] flex-col gap-1', isUser ? 'items-end' : 'items-start')}>
        <div
          className={cn(
            'rounded-2xl px-3.5 py-2 text-sm',
            isUser
              ? 'chat-bubble text-foreground'
              : 'border border-rose-500/30 bg-rose-500/5 text-rose-200',
          )}
        >
          {!isUser && (
            <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
              <span>{botName}</span>
              {message.scheduled && <ScheduledTag />}
            </div>
          )}
          <div className="whitespace-pre-wrap break-words">{message.content}</div>
        </div>
        {/* Time + actions sit below the bubble (not inside it, which made the
            bubble tall) and only appear on hover/focus. */}
        <div className="flex items-center gap-2 px-1 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
          <span>{formatDateTime(message.at)}</span>
          {isUser && !message.error ? (
            <MessageActions content={message.content} isUser onEdit={() => onEdit(message.id)} />
          ) : (
            <MessageActions content={message.content} isUser={false} onRetry={onRetry} />
          )}
        </div>
      </div>
    </div>
  );
}

function MessageActions({
  content,
  isUser,
  feedback,
  onEdit,
  onRegenerate,
  onRetry,
  onFeedback,
}: {
  content: string;
  isUser: boolean;
  feedback?: 'up' | 'down';
  onEdit?: () => void;
  onRegenerate?: () => void;
  onRetry?: () => void;
  onFeedback?: (fb: 'up' | 'down') => void;
}) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const btn =
    'flex h-5 w-5 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-white/10 hover:text-foreground';
  const copy = () => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    void navigator.clipboard.writeText(content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => { /* clipboard unavailable */ });
  };
  return (
    <span className="flex items-center gap-0.5">
      <button
        type="button"
        onClick={copy}
        className={btn}
        title={copied ? t('bots.chat.copied') : t('bots.chat.copy')}
        aria-label={t('bots.chat.copy')}
      >
        {copied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
      </button>
      {!isUser && <TtsButton content={content} />}
      {!isUser && onFeedback && (
        <>
          <button
            type="button"
            onClick={() => onFeedback('up')}
            className={cn(btn, feedback === 'up' && 'text-emerald-300')}
            title={t('bots.chat.good')}
            aria-label={t('bots.chat.good')}
            aria-pressed={feedback === 'up'}
          >
            <ThumbsUp size={12} weight={feedback === 'up' ? 'fill' : 'regular'} />
          </button>
          <button
            type="button"
            onClick={() => onFeedback('down')}
            className={cn(btn, feedback === 'down' && 'text-rose-300')}
            title={t('bots.chat.bad')}
            aria-label={t('bots.chat.bad')}
            aria-pressed={feedback === 'down'}
          >
            <ThumbsDown size={12} weight={feedback === 'down' ? 'fill' : 'regular'} />
          </button>
        </>
      )}
      {isUser && onEdit && (
        <button type="button" onClick={onEdit} className={btn} title={t('bots.edit')} aria-label={t('bots.edit')}>
          <SquarePen size={12} />
        </button>
      )}
      {!isUser && onRegenerate && (
        <button
          type="button"
          onClick={onRegenerate}
          className={btn}
          title={t('bots.chat.regenerate')}
          aria-label={t('bots.chat.regenerate')}
        >
          <RefreshCw size={12} />
        </button>
      )}
      {!isUser && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className={btn}
          title={t('bots.chat.retry')}
          aria-label={t('bots.chat.retry')}
        >
          <RefreshCw size={12} />
        </button>
      )}
    </span>
  );
}
