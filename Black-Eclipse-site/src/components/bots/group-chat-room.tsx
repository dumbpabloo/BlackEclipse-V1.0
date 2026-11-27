'use client';

// ── Group chat room ───────────────────────────────────────────────────────────
// A conversation shared by 2–6 bots. The user speaks to the room; the reply comes
// from the real harness with the room's member roster as standing context, and is
// attributed to the room (never to a specific bot, which we cannot guarantee).

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import {
  ArrowUp, CalendarDays, CheckCircle2, ChevronDown, Copy, FileText, Loader2, Pause, Play,
  Plus, RefreshCw, Settings, SquarePen, ThumbsDown, ThumbsUp, Trash2, Users, X,
} from '@/lib/icons';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Markdown } from '@/components/chat/markdown';
import { ParallelAgentsPanel } from '@/components/chat/parallel-agents-panel';
import { ApprovalCard } from '@/components/chat/approval-card';
import { ClarificationCard } from '@/components/chat/clarification-card';
import { TradeApprovalBanner } from '@/components/chat/trade-approval-banner';
import type { HarnessUiEvent } from '@/lib/agent/events';
import type { DelegationView } from '@/lib/agent/response-model';
import {
  applyDelegationEvent,
  dropProvisionalDelegation,
  startProvisionalDelegation,
} from '@/lib/agent/delegation-reducer';
import { groupMemberChatId, runGroupMemberTurn } from '@/lib/bots/invoke';
import {
  deleteSessionFile, fetchSession, uploadSessionFile,
} from '@/lib/chat/session-client';
import type { SessionFile } from '@/lib/chat/session-types';
import { useWorkspaceStore } from '@/lib/agent-workspace/store';
import { listAutomationHistory, type ExecutionView } from '@/lib/harness/clients/automation-browser';
import { mentionedTargets } from '@/lib/bots/mentions';
import { isDesk } from '@/lib/bots/desk';
import { forgetGroupMemory } from '@/lib/bots/purge';
import { BotAvatar } from '@/components/bots/bot-avatar';
import { GhostAvatar } from '@/components/bots/ghost-avatar';
import { LiveGhostAvatar } from '@/components/bots/live-ghost-avatar';
import { moodFromEvent, type GhostMood } from '@/lib/bots/ghost-mood';
import { GroupSettingsDialog } from '@/components/bots/group-settings-dialog';
import { avatarFor, useBotAvatarStore, useHydrateBotAvatars } from '@/lib/bots/avatar-store';
import {
  useBotGroupsStore, useHydrateBotGroups, type BotGroup, type GroupMessage,
} from '@/lib/bots/groups-store';
import { ghostSpecFromId } from '@/lib/bots/ghost';
import { formatDateTime } from '@/lib/bots/state';
import {
  pauseAutomation, resumeAutomation, runAutomationNow,
  type AutomationView,
} from '@/lib/harness/clients/automation-browser';

export interface GroupChatRoomProps {
  group: BotGroup;
  bots: AutomationView[];
  /** False while the Bots tab is hidden behind the keep-alive mount. */
  active?: boolean;
  onDeleted: (id: string) => void;
  /** Refresh the bot list after a group-wide run/pause/resume. */
  onChanged?: () => void;
}

function newId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

const NO_MESSAGES: GroupMessage[] = [];

export function GroupChatRoom({ group, bots, active = true, onDeleted, onChanged }: GroupChatRoomProps) {
  const t = useT();
  useHydrateBotAvatars();
  useHydrateBotGroups();
  const avatars = useBotAvatarStore((s) => s.avatars);
  const messages = useBotGroupsStore((s) => s.byGroup[group.id] ?? NO_MESSAGES);
  const append = useBotGroupsStore((s) => s.appendMessage);
  const appendScheduled = useBotGroupsStore((s) => s.appendScheduled);
  const setMessages = useBotGroupsStore((s) => s.setMessages);
  const updateMessage = useBotGroupsStore((s) => s.updateMessage);
  const clearMessages = useBotGroupsStore((s) => s.clearMessages);
  const deleteGroup = useBotGroupsStore((s) => s.deleteGroup);
  // The right-docked scheduled-jobs rail is the shared Agent Workspace dock.
  const schedulerOpen = useWorkspaceStore((s) => s.open);
  const toggleScheduler = useWorkspaceStore((s) => s.toggle);

  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [busyAll, setBusyAll] = useState(false);
  const [groupNote, setGroupNote] = useState<string | null>(null);
  // Per-member face while the room runs (idle/absent = resting face).
  const [memberMoods, setMemberMoods] = useState<Record<string, GhostMood>>({});
  const restTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  /** Who answers: 'all' (every member) or a single member's bot id. */
  const [audience, setAudience] = useState<string>('all');
  /** Active @mention token query (null = the mention menu is closed). */
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const controllersRef = useRef<AbortController[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Anchor for the approval / clarification cards (they portal above it).
  const [composerEl, setComposerEl] = useState<HTMLDivElement | null>(null);
  // Documents attached to the room. Each member answers in its OWN
  // `group-<id>-<bot>` session, so an upload is mirrored into every member's
  // context folder; the shared room transcript stays the single visible thread.
  const attachInputRef = useRef<HTMLInputElement | null>(null);
  const [contextFiles, setContextFiles] = useState<SessionFile[]>([]);
  const [attachBusy, setAttachBusy] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);
  // Execution ids already reflected in the room (`botId:executionId`).
  const seenRunsRef = useRef<Set<string> | null>(null);
  const botsRef = useRef(bots);
  botsRef.current = bots;

  // Stop any in-flight runs if the room unmounts so streams are not orphaned.
  useEffect(
    () => () => {
      controllersRef.current.forEach((c) => c.abort());
      Object.values(restTimers.current).forEach(clearTimeout);
      restTimers.current = {};
    },
    [],
  );

  const setMemberMood = useCallback((id: string, mood: GhostMood) => {
    setMemberMoods((prev) => (prev[id] === mood ? prev : { ...prev, [id]: mood }));
  }, []);

  /** After a finished/waiting beat, let the member's face rest again. */
  const restMember = useCallback(
    (id: string) => {
      const timers = restTimers.current;
      if (timers[id]) clearTimeout(timers[id]);
      timers[id] = setTimeout(() => {
        delete timers[id];
        setMemberMood(id, 'idle');
      }, 1800);
    },
    [setMemberMood],
  );

  const members = useMemo(
    () =>
      group.memberIds
        .map((id) => bots.find((b) => b.id === id))
        // A desk is a pipeline, not a room correspondent (older rooms may still
        // name one); it must never be run as an agent turn.
        .filter((b): b is AutomationView => b !== undefined && !isDesk(b)),
    [group.memberIds, bots],
  );

  const groupSpec = useMemo(() => ghostSpecFromId(group.id), [group.id]);
  // The latest member turn blocked on a decision, if any.
  const pendingMessage = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].pendingApproval || messages[i].pendingClarification) return messages[i];
    }
    return null;
  }, [messages]);
  const pendingApproval = pendingMessage?.pendingApproval;
  const pendingClarification = pendingMessage?.pendingClarification;

  // Every member has its own context folder; they should hold the same set.
  const memberChatIds = useMemo(
    () => members.map((m) => groupMemberChatId(group.id, m.id)),
    [members, group.id],
  );
  const firstMemberChatId = memberChatIds[0];

  useEffect(() => {
    if (!firstMemberChatId) {
      setContextFiles([]);
      return;
    }
    let cancelled = false;
    void fetchSession(firstMemberChatId)
      .then((session) => {
        if (!cancelled) setContextFiles(session.files.filter((f) => f.kind === 'context'));
      })
      .catch(() => { /* no session yet */ });
    return () => { cancelled = true; };
  }, [firstMemberChatId]);

  const refreshAttachments = useCallback(async () => {
    if (!firstMemberChatId) return;
    try {
      const session = await fetchSession(firstMemberChatId);
      setContextFiles(session.files.filter((f) => f.kind === 'context'));
    } catch {
      /* keep the current list */
    }
  }, [firstMemberChatId]);

  const attachFiles = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0 || memberChatIds.length === 0) return;
    setAttachBusy(true);
    setAttachError(null);
    try {
      for (const file of Array.from(files)) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        for (const chatId of memberChatIds) {
          await uploadSessionFile(chatId, 'context', file.name, bytes);
        }
      }
      await refreshAttachments();
    } catch (err) {
      setAttachError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setAttachBusy(false);
      if (attachInputRef.current) attachInputRef.current.value = '';
    }
  }, [memberChatIds, refreshAttachments]);

  const removeAttachment = useCallback(async (name: string) => {
    setAttachError(null);
    try {
      for (const chatId of memberChatIds) {
        await deleteSessionFile(chatId, 'context', name);
      }
      await refreshAttachments();
    } catch (err) {
      setAttachError(err instanceof Error ? err.message : 'Could not remove the attachment');
    }
  }, [memberChatIds, refreshAttachments]);

  // The room's face reflects the busiest member: talking beats working beats
  // thinking beats a resting beat. Idle when nobody is doing anything.
  const groupMood = useMemo<GhostMood>(() => {
    const moods = Object.values(memberMoods);
    if (moods.includes('speaking')) return 'speaking';
    if (moods.includes('working')) return 'working';
    if (moods.includes('thinking')) return 'thinking';
    if (moods.includes('waiting')) return 'waiting';
    if (moods.includes('happy')) return 'happy';
    if (moods.includes('sad')) return 'sad';
    return 'idle';
  }, [memberMoods]);

  const allActive = members.length > 0 && members.every((b) => b.state === 'active');

  // Surface each member's AUTONOMOUS runs (schedule / "Run now") into the room.
  // Only the latest answer per member is replayed on the first sweep; every run
  // finished afterwards is appended once, keyed by its execution id.
  useEffect(() => {
    if (!active) return;
    seenRunsRef.current = null;
    let cancelled = false;

    const finishedRuns = (executions: ExecutionView[]) =>
      executions
        .filter((e) => (e.state === 'completed' || e.state === 'failed') && (e.output ?? '').trim())
        .sort((a, b) => Date.parse(a.finishedAt ?? a.startedAt) - Date.parse(b.finishedAt ?? b.startedAt));

    const postRun = (bot: AutomationView, execution: ExecutionView) => {
      const text = (execution.output ?? '').trim();
      if (!text) return;
      appendScheduled(group.id, {
        id: `run-${execution.id}`,
        role: 'assistant',
        content: text,
        at: execution.finishedAt ?? execution.startedAt,
        authorId: bot.id,
        authorName: bot.name,
        scheduled: true,
        runId: execution.id,
        ...(execution.state === 'failed' ? { error: true } : {}),
      });
    };

    const sync = async () => {
      const liveMembers = group.memberIds
        .map((id) => botsRef.current.find((b) => b.id === id))
        .filter((b): b is AutomationView => Boolean(b));
      const histories = await Promise.all(
        liveMembers.map(async (bot) => {
          try {
            return [bot, finishedRuns(await listAutomationHistory(bot.id))] as const;
          } catch {
            return [bot, [] as ExecutionView[]] as const;
          }
        }),
      );
      if (cancelled) return;

      if (seenRunsRef.current === null) {
        const seen = new Set<string>();
        const latest: Array<{ bot: AutomationView; execution: ExecutionView }> = [];
        for (const [bot, runs] of histories) {
          let last: ExecutionView | null = null;
          for (const run of runs) {
            seen.add(`${bot.id}:${run.id}`);
            last = run;
          }
          if (last) latest.push({ bot, execution: last });
        }
        seenRunsRef.current = seen;
        for (const { bot, execution } of latest) postRun(bot, execution);
        return;
      }

      const seen = seenRunsRef.current;
      for (const [bot, runs] of histories) {
        for (const run of runs) {
          const key = `${bot.id}:${run.id}`;
          if (seen.has(key)) continue;
          seen.add(key);
          postRun(bot, run);
        }
      }
    };

    void sync();
    const timer = setInterval(() => void sync(), 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [group, active, appendScheduled]);

  useEffect(() => {
    if (!groupNote) return;
    const timer = setTimeout(() => setGroupNote(null), 6000);
    return () => clearTimeout(timer);
  }, [groupNote]);

  // Run every member's automation right now.
  const runAll = useCallback(async () => {
    if (busyAll || members.length === 0) return;
    setBusyAll(true);
    setGroupNote(null);
    try {
      await Promise.all(members.map((b) => runAutomationNow(b.id)));
      setGroupNote(t('bots.groups.ranAll'));
      onChanged?.();
    } catch (err) {
      setGroupNote(err instanceof Error ? err.message : 'run failed');
    } finally {
      setBusyAll(false);
    }
  }, [busyAll, members, onChanged, t]);

  // Pause/resume every member's automation.
  const setAllPausedState = useCallback(async (pause: boolean) => {
    if (busyAll || members.length === 0) return;
    setBusyAll(true);
    setGroupNote(null);
    try {
      await Promise.all(members.map((b) => (pause ? pauseAutomation(b.id) : resumeAutomation(b.id))));
      setGroupNote(t(pause ? 'bots.groups.pausedAll' : 'bots.groups.resumedAll'));
      onChanged?.();
    } catch (err) {
      setGroupNote(err instanceof Error ? err.message : 'action failed');
    } finally {
      setBusyAll(false);
    }
  }, [busyAll, members, onChanged, t]);

  // The thread reads top-to-bottom, so the newest message lives at the BOTTOM.
  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, []);

  const lastContent = messages.length > 0 ? messages[messages.length - 1].content : '';
  useEffect(() => { scrollToBottom(); }, [messages.length, sending, lastContent, scrollToBottom]);

  // Auto-grow the composer with its content, up to a cap, then scroll.
  const resizeComposer = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);
  useEffect(() => { resizeComposer(); }, [input, resizeComposer]);

  /**
   * One member bot answers in its OWN voice: a separate agent turn with that
   * bot's identity + instructions, streamed into its own message. Members never
   * share or merge a reply.
   */
  const runMember = useCallback(
    async (bot: AutomationView, text: string, prior: GroupMessage[], controllers: AbortController[]): Promise<string> => {
      const replyId = newId();
      append(group.id, {
        id: replyId, role: 'assistant', content: '', at: new Date().toISOString(),
        authorId: bot.id, authorName: bot.name,
      });
      const controller = new AbortController();
      controllers.push(controller);
      // A fresh run supersedes any pending "rest" beat for this member.
      if (restTimers.current[bot.id]) {
        clearTimeout(restTimers.current[bot.id]);
        delete restTimers.current[bot.id];
      }
      setMemberMood(bot.id, 'thinking');
      let streamed = '';
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
        updateMessage(group.id, replyId, { delegations }, false);
      };
      try {
        const reply = await runGroupMemberTurn({
          group,
          bot,
          text,
          prior,
          signal: controller.signal,
          onEvent: (event) => {
            applyDelegation(event);
            // A blocked member turn is actionable in the room, just like main chat.
            if (event.kind === 'approval_request') {
              updateMessage(group.id, replyId, { pendingApproval: event.approval }, false);
            } else if (event.kind === 'approval_resolved') {
              updateMessage(group.id, replyId, { pendingApproval: undefined }, false);
            } else if (event.kind === 'clarification_request') {
              updateMessage(group.id, replyId, { pendingClarification: event.clarification }, false);
            } else if (event.kind === 'clarification_answered') {
              updateMessage(group.id, replyId, { pendingClarification: undefined }, false);
            }
            const next = moodFromEvent(event);
            if (next) setMemberMood(bot.id, next);
          },
          onDelta: (chunk) => {
            streamed += chunk;
            setMemberMood(bot.id, 'speaking');
            updateMessage(group.id, replyId, { content: streamed }, false);
          },
        });
        const final = reply || streamed;
        updateMessage(
          group.id,
          replyId,
          final
            ? { content: final, pendingApproval: undefined, pendingClarification: undefined }
            : { content: `${bot.name} did not return a reply.`, error: true, pendingApproval: undefined, pendingClarification: undefined },
        );
        setMemberMood(bot.id, final ? 'happy' : 'sad');
        restMember(bot.id);
        return final;
      } catch (err) {
        if (controller.signal.aborted) {
          updateMessage(group.id, replyId, {
            ...(streamed ? { content: streamed } : { content: '(stopped)', error: true }),
            pendingApproval: undefined,
            pendingClarification: undefined,
          });
        } else {
          updateMessage(group.id, replyId, {
            content: err instanceof Error ? err.message : 'network error',
            error: true,
            pendingApproval: undefined,
            pendingClarification: undefined,
          });
        }
        setMemberMood(bot.id, 'sad');
        restMember(bot.id);
        return streamed;
      }
    },
    [append, updateMessage, group, setMemberMood, restMember],
  );

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || sending || attachBusy || members.length === 0) return;
    const prior = useBotGroupsStore.getState().byGroup[group.id] ?? [];
    append(group.id, { id: newId(), role: 'user', content: text, at: new Date().toISOString() });
    setInput('');
    setMentionQuery(null);
    setSending(true);
    // @mentions win over the audience selector: naming bots routes the turn to
    // exactly those members. Uses the SAME parser as the main composer (word
    // boundaries, longest-name-first) instead of a per-name regex.
    const memberTargets = members.map((b) => ({
      kind: 'bot' as const, id: b.id, name: b.name, description: '', memberIds: [],
    }));
    const mentionedIds = new Set(mentionedTargets(text, memberTargets).map((m) => m.id));
    const targets = mentionedIds.size
      ? members.filter((b) => mentionedIds.has(b.id))
      : audience === 'all'
        ? members
        : members.filter((b) => b.id === audience);
    const controllers: AbortController[] = [];
    controllersRef.current = controllers;
    try {
      if (group.mode === 'roundtable') {
        // Roundtable: members answer in order, and each later member receives
        // the replies already produced in THIS round. That is the difference
        // between three independent opinions and an actual discussion. The
        // round is still one reply per member; nothing is merged or hidden.
        let roundPrior: GroupMessage[] = prior;
        for (const bot of targets) {
          const reply = await runMember(bot, text, roundPrior, controllers);
          if (!reply.trim()) continue;
          roundPrior = [
            ...roundPrior,
            {
              id: `round-${bot.id}-${Date.now()}`,
              role: 'assistant',
              content: reply,
              at: new Date().toISOString(),
              authorId: bot.id,
              authorName: bot.name,
            },
          ];
        }
        // An optional chair turns independent replies into one decision. It is
        // a normal member turn (same identity, tools and memory), not a hidden
        // merge: its synthesis is visible in the room like any other reply.
        const lead = members.find((member) => member.id === group.leadId);
        const replies = roundPrior.slice(prior.length).filter((m) => m.role === 'assistant');
        if (lead && replies.length >= 2) {
          const digest = replies
            .map((m) => `${m.authorName ?? 'Member'}: ${m.content}`)
            .join('\n\n');
          const synthesis = [
            `Chair the roundtable. The user asked: "${text}".`,
            'Members replied:',
            digest,
            'Write one decision-oriented synthesis: the recommendation, the strongest disagreement or risk, and what still needs the operator. Do not invent facts and do not hide a disagreement.',
          ].join('\n\n');
          await runMember(lead, synthesis, roundPrior, controllers);
        }
      } else {
        // Independent takes: all targeted bots answer in parallel; each streams
        // into its own message so no reply is combined.
        await Promise.all(targets.map((bot) => runMember(bot, text, prior, controllers)));
      }
    } finally {
      controllersRef.current = [];
      setSending(false);
    }
  }, [input, sending, attachBusy, members, audience, append, group.id, runMember]);

  /** Stop every in-flight member reply, keeping whatever streamed so far. */
  const stop = useCallback(() => {
    controllersRef.current.forEach((c) => c.abort());
  }, []);

  /** Answer a blocking approval raised by a member's turn. */
  const decideApproval = useCallback(async (approve: boolean, remember?: boolean) => {
    const target = pendingMessage;
    const approval = target?.pendingApproval;
    if (!target || !approval) return;
    updateMessage(group.id, target.id, { pendingApproval: undefined }, false);
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
      updateMessage(group.id, target.id, { pendingApproval: approval }, false);
    }
  }, [group.id, pendingMessage, updateMessage]);

  /** Answer a paused `clarify` question from a member's turn. */
  const answerClarification = useCallback(async (answer: string) => {
    const target = pendingMessage;
    const clarification = target?.pendingClarification;
    if (!target || !clarification) return;
    updateMessage(group.id, target.id, { pendingClarification: undefined }, false);
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
      updateMessage(group.id, target.id, { pendingClarification: clarification }, false);
    }
  }, [group.id, pendingMessage, updateMessage]);

  // @mention autocomplete: detect a trailing "@word" token as the user types.
  const onInputChange = useCallback((v: string) => {
    setInput(v);
    const m = v.match(/(?:^|\s)@([\w.-]*)$/);
    setMentionQuery(m ? m[1] : null);
  }, []);

  const mentionMatches = useMemo(() => {
    if (mentionQuery === null) return [];
    const q = mentionQuery.toLowerCase();
    return members.filter((b) => b.name.toLowerCase().includes(q));
  }, [mentionQuery, members]);

  const pickMention = useCallback((bot: AutomationView) => {
    setInput((prev) => prev.replace(/@([\w.-]*)$/, `@${bot.name} `));
    setAudience(bot.id);
    setMentionQuery(null);
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

  /** Edit a sent prompt: truncate the thread there and load it into the composer. */
  const editGroupMessage = useCallback((id: string) => {
    const msgs = useBotGroupsStore.getState().byGroup[group.id] ?? [];
    const idx = msgs.findIndex((m) => m.id === id);
    if (idx < 0) return;
    setMessages(group.id, msgs.slice(0, idx));
    setInput(msgs[idx].content);
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, [group.id, setMessages]);

  /** Re-run a reply: drop just that assistant message and re-run its author. */
  const regenerateGroup = useCallback((id: string) => {
    const msgs = useBotGroupsStore.getState().byGroup[group.id] ?? [];
    const ai = msgs.findIndex((m) => m.id === id);
    if (ai < 0) return;
    let ui = ai - 1;
    while (ui >= 0 && msgs[ui].role !== 'user') ui -= 1;
    if (ui < 0) return;
    const text = msgs[ui].content;
    const author = members.find((b) => b.id === msgs[ai].authorId);
    const targets = author ? [author] : members;
    const prior = msgs.slice(0, ui); // everything before the prompt
    setMessages(group.id, msgs.slice(0, ai)); // keep the prompt, drop the reply
    const controllers: AbortController[] = [];
    controllersRef.current = controllers;
    setSending(true);
    void Promise.all(targets.map((b) => runMember(b, text, prior, controllers)))
      .finally(() => { controllersRef.current = []; setSending(false); });
  }, [group.id, members, runMember, setMessages]);

  /** Thumbs feedback on a member reply; toggling the same rating clears it. */
  const setGroupFeedback = useCallback((id: string, fb: 'up' | 'down') => {
    const cur = (useBotGroupsStore.getState().byGroup[group.id] ?? []).find((m) => m.id === id);
    updateMessage(group.id, id, { feedback: cur?.feedback === fb ? undefined : fb });
  }, [group.id, updateMessage]);

  return (
    <div className="glass-card flex h-full min-h-0 flex-col rounded-2xl">
      {/* Header — group identity + members */}
      <div className="flex items-start gap-3 border-b border-border/10 p-4">
        {group.image ? (
          <img src={group.image} alt="" className="h-12 w-12 shrink-0 rounded-2xl object-cover" />
        ) : (
          <LiveGhostAvatar spec={groupSpec} size={48} mood={groupMood} />
        )}
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-base font-semibold">{group.name}</h2>
          <div className="mt-1 flex items-center gap-2">
            <div className="flex -space-x-1.5">
              {members.slice(0, 6).map((b) => (
                <BotAvatar
                  key={b.id}
                  botId={b.id}
                  spec={avatarFor(avatars, b.id)}
                  size={22}
                  className="rounded-full ring-2 ring-background"
                  title={b.name}
                  live
                  mood={memberMoods[b.id]}
                />
              ))}
            </div>
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              <Users size={12} /> {t('bots.groups.members', { count: group.memberIds.length })}
            </span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          <Button size="sm" disabled={busyAll || members.length === 0} onClick={() => void runAll()}>
            {busyAll ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />} {t('bots.runNow')}
          </Button>
          {allActive ? (
            <Button
              size="icon-sm"
              variant="outline"
              disabled={busyAll}
              title={t('bots.groups.pauseAll')}
              aria-label={t('bots.groups.pauseAll')}
              onClick={() => void setAllPausedState(true)}
            >
              <Pause size={14} />
            </Button>
          ) : (
            <Button
              size="icon-sm"
              variant="outline"
              disabled={busyAll}
              title={t('bots.groups.resumeAll')}
              aria-label={t('bots.groups.resumeAll')}
              onClick={() => void setAllPausedState(false)}
            >
              <Play size={14} />
            </Button>
          )}
          <Button
            size="icon-sm"
            variant="ghost"
            title={t('bots.jobsPanel.open')}
            aria-label={t('bots.jobsPanel.open')}
            aria-pressed={schedulerOpen}
            onClick={toggleScheduler}
            className={schedulerOpen ? 'bg-white/[0.08] text-primary' : ''}
          >
            <CalendarDays size={14} />
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            title={t('bots.groups.settings')}
            aria-label={t('bots.groups.settings')}
            onClick={() => setSettingsOpen(true)}
          >
            <Settings size={14} />
          </Button>
          {confirmDelete ? (
            <>
              <Button
                size="sm"
                variant="destructive"
                onClick={() => {
                  const memberIds = [...group.memberIds];
                  deleteGroup(group.id);
                  onDeleted(group.id);
                  void forgetGroupMemory(group.id, memberIds);
                }}
              >
                {t('bots.groups.delete')}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>
                {t('bots.groups.cancel')}
              </Button>
            </>
          ) : (
            <Button
              size="icon-sm"
              variant="ghost"
              title={t('bots.groups.delete')}
              aria-label={t('bots.groups.delete')}
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2 size={14} />
            </Button>
          )}
        </div>
      </div>

      {/* Thread */}
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-5">
        {messages.length === 0 && (
          <div className="mx-auto max-w-md py-8 text-center">
            {group.image ? (
              <img src={group.image} alt="" className="mx-auto h-16 w-16 rounded-2xl object-cover" />
            ) : (
              <GhostAvatar spec={groupSpec} size={64} className="mx-auto" />
            )}
            <p className="mt-3 text-sm font-medium">{group.name}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('bots.groups.dialogHint')}
            </p>
          </div>
        )}

        {groupNote && (
          <div className="glass-card-subtle mx-auto w-fit rounded-full px-3 py-1 text-[11px] text-muted-foreground">
            {groupNote}
          </div>
        )}

        {/* Oldest first: the question is on top, its answer below it. */}
        {messages.map((m, i) => {
          return !m.error && m.role === 'assistant' && !m.content ? null : (
            <GroupBubble
              key={m.id}
              message={m}
              groupName={group.name}
              groupSpec={groupSpec}
              groupImage={group.image}
              onEdit={editGroupMessage}
              onRegenerate={
                i === messages.length - 1 && m.role === 'assistant' && !m.error && !m.scheduled
                  ? regenerateGroup
                  : undefined
              }
              onRetry={
                i === messages.length - 1 && m.role === 'assistant' && m.error && !m.scheduled
                  ? regenerateGroup
                  : undefined
              }
              onFeedback={setGroupFeedback}
            />
          );
        })}

        {sending && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={14} className="animate-spin" />
            {t('bots.groups.answering')}
          </div>
        )}
      </div>

      {/* Composer */}
      <div ref={setComposerEl} className="border-t border-border/10 p-3">
        {/* A member can call `order_preview`; the exact order must be
            confirmable in the room that produced it, not only in a bot chat. */}
        <TradeApprovalBanner />
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
        <div className="mb-2 flex items-center gap-2">
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <button
                  type="button"
                  className="inline-flex items-center gap-1.5 rounded-full border border-border/50 px-2.5 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:border-border hover:text-foreground"
                  title={t('bots.groups.audience')}
                  aria-label={t('bots.groups.audience')}
                />
              }
            >
              <Users size={12} />
              <span>
                {audience === 'all'
                  ? t('bots.groups.everyone')
                  : members.find((b) => b.id === audience)?.name ?? t('bots.groups.everyone')}
              </span>
              <ChevronDown size={12} />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-52">
              <DropdownMenuItem onClick={() => setAudience('all')} className="gap-2">
                <Users size={14} /> <span className="flex-1">{t('bots.groups.everyone')}</span>
              </DropdownMenuItem>
              {members.map((b) => (
                <DropdownMenuItem key={b.id} onClick={() => setAudience(b.id)} className="gap-2">
                  <BotAvatar botId={b.id} spec={avatarFor(avatars, b.id)} size={16} title={b.name} />
                  <span className="flex-1 truncate">{b.name}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          <span className="truncate text-[10px] text-muted-foreground">{t('bots.groups.audienceHint')}</span>
        </div>
        <div className="relative flex items-end gap-2">
          {mentionQuery !== null && mentionMatches.length > 0 && (
            <ul className="glass-card-strong absolute bottom-full left-0 z-30 mb-2 max-h-48 w-56 overflow-y-auto rounded-xl border border-border/40 p-1 shadow-xl shadow-black/40">
              {mentionMatches.map((b) => (
                <li key={b.id}>
                  <button
                    type="button"
                    onClick={() => pickMention(b)}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-white/[0.06]"
                  >
                    <BotAvatar botId={b.id} spec={avatarFor(avatars, b.id)} size={18} title={b.name} />
                    <span className="truncate">{b.name}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); }
              if (e.key === 'Escape' && mentionQuery !== null) { e.preventDefault(); setMentionQuery(null); }
            }}
            rows={1}
            placeholder={t('bots.groups.composer')}
            aria-label={t('bots.groups.composer')}
            className="glass-composer max-h-[200px] min-h-10 flex-1 resize-none overflow-y-auto rounded-xl border border-white/10 px-3 py-2 text-sm outline-none dark:border-white/5"
          />
          {!sending && (
            <Button
              size="icon"
              variant="ghost"
              onClick={() => attachInputRef.current?.click()}
              disabled={attachBusy}
              title="Attach a document"
              aria-label="Attach a document"
            >
              <Plus size={15} />
            </Button>
          )}
          {sending ? (
            <Button size="icon" variant="outline" onClick={stop} title={t('bots.groups.stop')} aria-label={t('bots.groups.stop')}>
              <Pause size={15} />
            </Button>
          ) : (
            <Button size="icon" onClick={() => void send()} disabled={!input.trim() || attachBusy} aria-label={t('bots.groups.composer')}>
              <ArrowUp size={15} />
            </Button>
          )}
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
          <span className="truncate">{t('bots.groups.membersTitle')}: {members.map((m) => m.name).join(', ') || '—'}</span>
          {messages.length > 0 && (
            <button type="button" onClick={() => clearMessages(group.id)} className="shrink-0 hover:text-foreground">
              {t('bots.groups.clear')}
            </button>
          )}
        </div>
      </div>

      <GroupSettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} group={group} bots={bots} />
    </div>
  );
}

function GroupBubble({
  message,
  groupName,
  groupSpec,
  groupImage,
  onEdit,
  onRegenerate,
  onRetry,
  onFeedback,
}: {
  message: GroupMessage;
  groupName: string;
  groupSpec: import('@/lib/bots/ghost').GhostSpec;
  groupImage?: string;
  onEdit: (id: string) => void;
  onRegenerate?: (id: string) => void;
  onRetry?: (id: string) => void;
  onFeedback?: (id: string, fb: 'up' | 'down') => void;
}) {
  const t = useT();
  const avatars = useBotAvatarStore((s) => s.avatars);
  const [copied, setCopied] = useState(false);
  const isUser = message.role === 'user';
  // Each reply is from a specific member now, so show ITS avatar; fall back to
  // the room identity for older/user turns.
  const avatar = message.authorId
    ? <BotAvatar botId={message.authorId} spec={avatarFor(avatars, message.authorId)} size={28} className="mt-0.5 shrink-0" title={message.authorName} />
    : groupImage
      ? <img src={groupImage} alt="" className="mt-0.5 h-7 w-7 shrink-0 rounded-full object-cover" />
      : <GhostAvatar spec={groupSpec} size={28} className="mt-0.5 shrink-0" />;
  const btn = 'flex h-5 w-5 cursor-pointer items-center justify-center rounded-md hover:bg-white/10 hover:text-foreground';
  const copy = () => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    void navigator.clipboard.writeText(message.content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => { /* clipboard unavailable */ });
  };
  const timeRow = (
    <div className="mt-1 flex items-center gap-0.5 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
      <span className="mr-1">{formatDateTime(message.at)}</span>
      <button type="button" onClick={copy} className={btn} title={copied ? t('bots.chat.copied') : t('bots.chat.copy')} aria-label={t('bots.chat.copy')}>
        {copied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
      </button>
      {!isUser && onFeedback && (
        <>
          <button
            type="button"
            onClick={() => onFeedback(message.id, 'up')}
            className={cn(btn, message.feedback === 'up' && 'text-emerald-300')}
            title={t('bots.chat.good')}
            aria-label={t('bots.chat.good')}
            aria-pressed={message.feedback === 'up'}
          >
            <ThumbsUp size={12} weight={message.feedback === 'up' ? 'fill' : 'regular'} />
          </button>
          <button
            type="button"
            onClick={() => onFeedback(message.id, 'down')}
            className={cn(btn, message.feedback === 'down' && 'text-rose-300')}
            title={t('bots.chat.bad')}
            aria-label={t('bots.chat.bad')}
            aria-pressed={message.feedback === 'down'}
          >
            <ThumbsDown size={12} weight={message.feedback === 'down' ? 'fill' : 'regular'} />
          </button>
        </>
      )}
      {isUser && !message.error && (
        <button type="button" onClick={() => onEdit(message.id)} className={btn} title={t('bots.edit')} aria-label={t('bots.edit')}>
          <SquarePen size={12} />
        </button>
      )}
      {!isUser && onRegenerate && (
        <button type="button" onClick={() => onRegenerate(message.id)} className={btn} title={t('bots.chat.regenerate')} aria-label={t('bots.chat.regenerate')}>
          <RefreshCw size={12} />
        </button>
      )}
      {!isUser && onRetry && (
        <button type="button" onClick={() => onRetry(message.id)} className={btn} title={t('bots.chat.retry')} aria-label={t('bots.chat.retry')}>
          <RefreshCw size={12} />
        </button>
      )}
    </div>
  );
  // A room reply reads as a full-width document with no glass surface; only the
  // user's own turns (and honest error turns) are framed as bubbles.
  if (!isUser && !message.error) {
    return (
      <div className="group flex gap-2.5">
        {avatar}
        <div className="min-w-0 flex-1">
          <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
            <span>{message.authorName ?? groupName}</span>
            {message.scheduled && (
              <span className="rounded-full border border-border/40 px-1.5 py-px text-[9px] font-normal uppercase tracking-wide text-muted-foreground/70">
                {t('bots.chat.autoRun')}
              </span>
            )}
          </div>
          {message.delegations && message.delegations.length > 0 && (
            <ParallelAgentsPanel delegations={message.delegations} compact />
          )}
          <Markdown content={message.content} compact className="w-full" />
          {timeRow}
        </div>
      </div>
    );
  }
  return (
    <div className={cn('group flex gap-2.5', isUser ? 'flex-row-reverse' : 'flex-row')}>
      {!isUser && avatar}
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
              <span>{message.authorName ?? groupName}</span>
              {message.scheduled && (
                <span className="rounded-full border border-border/40 px-1.5 py-px text-[9px] font-normal uppercase tracking-wide text-muted-foreground/70">
                  {t('bots.chat.autoRun')}
                </span>
              )}
            </div>
          )}
          <div className="whitespace-pre-wrap break-words">{message.content}</div>
        </div>
        {timeRow}
      </div>
    </div>
  );
}
