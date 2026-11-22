'use client';

// ── ChatDock — the floating bottom composer ──────────────────────────────────
// A Gemini-style assistant bar that floats at the bottom-centre of the app.
// Mounted once in the app shell (`stock-chat.tsx`) so it appears on every tab
// EXCEPT the ChatBot and while the Settings modal is open (the shell passes
// `visible={false}` there — see `ChatDockProps.visible`); the standalone
// `/alerts` route mounts its own. Collapsed it is just the composer; once it has
// a conversation (or the user focuses it) the transcript expands ABOVE it in the
// same floating stack.
//
// It reuses the SAME chat stack as the full ChatBot tab — `ChatHistory` /
// `ChatInput` (`ModelSelector`), `useChatModel()`, and `/api/agent/research` —
// so there is one chat implementation, not a second one. It owns its own
// conversation (`be_chat_dock_v1`) so it never fights the ChatBot tab's
// shell-owned state.
//
// Portaled to <body> (the app shell's glass-card backdrop-filter traps fixed
// positioning otherwise). It is NOT marked `data-keyboard-overlay` — it is
// always on screen, so marking it a blocking dialog would disable every page
// shortcut permanently; the input already blocks shortcuts while focused. It IS
// `data-nodrag` so the workspace drag engine ignores it, and sits at z-[90]
// above the Command Center fullscreen overlay (z-[80]) — so the shell's single
// instance also covers fullscreen and no per-overlay copy is needed.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ArrowUp, Bot, Maximize2, Minimize2, Minus, Pause, Plus, SquarePen, Trash2,
  Image as ImageIcon, Layers, Blocks,
} from '@/lib/icons';
import { VoiceInputButtonView } from './voice-input-button';
import { VoiceFireField } from './voice-fire-field';
import { useVoiceInput } from '@/hooks/use-voice-input';
import { insertTranscript } from '@/lib/voice/insert';
import { cn } from '@/lib/utils';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useCommandStore } from '@/lib/search/store';
import { sound } from '@/lib/sounds';
import { isKeyboardEventAvailable } from '@/lib/keyboard';
import { FileSourcePicker } from '@/components/workspace/file-source-picker';
import type { PickedFile } from '@/lib/chat/file-sources';
import { CHAT_FOCUS_EVENT } from '@/hooks/use-chat-focus-keybind';
import { useAgentRun } from '@/lib/agent/use-agent-run';
import { takeNextFor } from '@/lib/chat/queue-store';
import { runManager } from '@/lib/agent/run-manager';
import { useChatModel } from '@/lib/agent/use-chat-model';
import { useReasoningEffort } from '@/lib/agent/use-reasoning-effort';
import type { AssistantTurn } from '@/lib/agent/response-model';
import { turnText } from '@/lib/agent/response-model';
import { uploadSessionFile } from '@/lib/chat/session-client';
import { SESSION_LIMITS } from '@/lib/chat/session-types';
import type { Message } from '@/types/chat';
import { ChatHistory } from './chat-history';
import { NotebookProvider } from './notebook-provider';
import { ModelSelector } from './chat-input';
import { EffortControl } from './effort-control';
import { ApprovalCard } from './approval-card';
import { ClarificationCard } from './clarification-card';
import { MessageQueue } from './message-queue';
import { GoalBar } from './goal-bar';
import { useMessageQueue } from '@/lib/chat/use-message-queue';
import { useEditorContext } from '@/lib/chat/use-editor-context';
import { clearEditorContext, editorContextLabel, formatEditorContext } from '@/lib/chat/editor-context';

const STORAGE_KEY = 'be_chat_dock_v1';
/** Stable workspace-folder id for the dock, so its runs also persist output. */
const SESSION_KEY = 'be_chat_dock_session_v1';
/** Persisted collapse preference — the user decides small vs big. */
const MINIMIZED_KEY = 'be_chat_dock_minimized_v1';
/** Persisted position of the minimized pill (viewport px, top-left corner). */
const PILL_POS_KEY = 'be_chat_dock_pill_pos_v1';
const MAX_PERSISTED = 60;

function loadMinimized(): boolean {
  if (typeof window === 'undefined') return false;
  try { return window.localStorage.getItem(MINIMIZED_KEY) === '1'; } catch { return false; }
}

function saveMinimized(minimized: boolean) {
  try { window.localStorage.setItem(MINIMIZED_KEY, minimized ? '1' : '0'); } catch { /* best effort */ }
}

interface PillPos { x: number; y: number; }

function loadPillPos(): PillPos | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(PILL_POS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as Partial<PillPos>;
    if (typeof p.x !== 'number' || typeof p.y !== 'number' || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return null;
    return { x: p.x, y: p.y };
  } catch { return null; }
}

function savePillPos(pos: PillPos) {
  try { window.localStorage.setItem(PILL_POS_KEY, JSON.stringify(pos)); } catch { /* best effort */ }
}

/** Keep the pill fully on screen after a drag or a viewport resize. */
function clampPill(x: number, y: number, w: number, h: number): PillPos {
  const pad = 6;
  const maxX = Math.max(pad, window.innerWidth - w - pad);
  const maxY = Math.max(pad, window.innerHeight - h - pad);
  return { x: Math.min(Math.max(pad, x), maxX), y: Math.min(Math.max(pad, y), maxY) };
}

function loadMessages(): Message[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (m): m is Message =>
        m && typeof m.id === 'string' && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string',
    );
  } catch { return []; }
}

function saveMessages(messages: Message[]) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-MAX_PERSISTED)));
  } catch { /* best effort */ }
}

function newId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

/** A stable, server-valid session id for the dock (created once, persisted). */
function loadDockSessionId(): string {
  if (typeof window === 'undefined') return 'dock-temp';
  try {
    const existing = window.localStorage.getItem(SESSION_KEY);
    if (existing && /^[A-Za-z0-9_-]{1,64}$/.test(existing)) return existing;
    const created = `dock-${Date.now().toString(36)}`;
    window.localStorage.setItem(SESSION_KEY, created);
    return created;
  } catch {
    return `dock-${Date.now().toString(36)}`;
  }
}

export interface ChatDockProps {
  /** Extra classes for the floating stack (e.g. to clear the fullscreen Exit). */
  className?: string;
  /** Placeholder for the composer. */
  placeholder?: string;
  /** Accessible label. */
  title?: string;
  /**
   * Whether this surface is allowed to show the dock. The host passes `false`
   * on the ChatBot tab and while the Settings modal is open; the dock still
   * gates on its host panel's own display. Kept mounted (not conditionally
   * rendered) so the conversation/expansion survives a suppress → restore.
   */
  visible?: boolean;
}

export function ChatDock({
  className,
  placeholder = 'Ask a question or start a project…',
  title = 'Assistant',
  visible = true,
}: ChatDockProps) {
  const [mounted, setMounted] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  // Prompts submitted while this dock's agent is replying. They run in order
  // once the current answer finishes; each can be edited/reordered/promoted.
  const messageQueue = useMessageQueue();
  const editorContext = useEditorContext();
  // Manual expand/collapse of the transcript (independent of focus, so the user
  // can scroll back through an answer without the panel collapsing).
  const [expanded, setExpanded] = useState(false);
  // True minimize: the whole dock shrinks to a small pill the user can restore.
  // Persisted so the choice survives a reload.
  const [minimized, setMinimized] = useState(false);
  // The minimized pill is draggable; `null` means "default corner" (bottom-right).
  const [pillPos, setPillPos] = useState<PillPos | null>(null);
  const pillRef = useRef<HTMLButtonElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const queryRef = useRef(query);
  queryRef.current = query;
  const voiceSelectionRef = useRef<{ start: number; end: number } | null>(null);
  const captureVoiceSelection = useCallback(() => {
    const el = textareaRef.current;
    voiceSelectionRef.current = el
      ? { start: el.selectionStart, end: el.selectionEnd }
      : null;
  }, []);
  const insertVoiceText = useCallback(
    (text: string) => {
      const { value, caret } = insertTranscript(
        queryRef.current,
        text,
        voiceSelectionRef.current ?? undefined,
      );
      setQuery(value);
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        try {
          el.setSelectionRange(caret, caret);
        } catch {
          /* selection unavailable */
        }
      });
    },
    [setQuery],
  );
  // The dock owns its dictation session so its composer can morph into the
  // same fire field as the full composer.
  const voice = useVoiceInput(insertVoiceText);
  const voiceActive = voice.state !== 'idle';
  const cancelVoiceRef = useRef(voice.cancel);
  cancelVoiceRef.current = voice.cancel;
  useEffect(() => {
    if (!voiceActive) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      cancelVoiceRef.current();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [voiceActive]);
  const sessionIdRef = useRef<string>('dock-temp');
  const [dockSessionId, setDockSessionId] = useState<string>('dock-temp');
  const [pickerMode, setPickerMode] = useState<'all' | 'images' | null>(null);

  // Insert a token (`/` or `@`) at the end of the draft and keep focus.
  const insertToken = (token: string) => {
    const base = query.replace(/\s+$/, '');
    setQuery(base ? `${base} ${token}` : token);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      try {
        el.setSelectionRange(el.value.length, el.value.length);
      } catch {
        /* setSelectionRange unavailable */
      }
    });
  };

  // One place the turn lifecycle lives (shared with the ChatBot tab). The run
  // manager owns the stream; this dock supplies a per-run sink so a background
  // conversation can never write into the dock's transcript.
  const agentRun = useAgentRun({ chatId: dockSessionId });
  useEffect(() => { setBusy(agentRun.active); }, [agentRun.active]);

  // The same real, persisted model picker the full ChatBot composer uses. The
  // dock's assistant can therefore change its model — the choice is persisted to
  // the settings vault and sent per-turn (belt-and-suspenders), exactly as on
  // the ChatBot tab. Without this the floating assistant was stuck on whatever
  // model Settings last saved.
  const chatModel = useChatModel();
  // The composer's per-question "model intelligence" override, shared with the
  // ChatBot tab. Auto (undefined) sends no requirement — the run falls back to
  // the saved default and the runtime's task profile.
  const reasoningEffort = useReasoningEffort();

  // The action the agent is blocked on, if any (approval card), and the composer
  // element the portaled card floats above.
  const [composerEl, setComposerEl] = useState<HTMLDivElement | null>(null);
  const pendingApproval = agentRun.turn.pendingApproval
    ?? [...messages].reverse().find(m => m.role === 'assistant' && m.pendingApproval)?.pendingApproval;
  const pendingClarification = agentRun.turn.pendingClarification
    ?? [...messages].reverse().find(m => m.role === 'assistant' && m.pendingClarification)?.pendingClarification;

  // The Command Center is a KEEP-ALIVE tab: once visited it stays mounted and is
  // hidden with `display:none`. A portal to <body> is NOT hidden by an ancestor
  // `display:none`, so without this the dock would float over every other tab.
  // Gate on the host tab's own display via a MutationObserver — NOT an
  // IntersectionObserver (the dashboard scrolls; an intersection test on a
  // zero-size sentinel reported "hidden" mid-scroll and made the dock vanish).
  const [hostVisible, setHostVisible] = useState(false);
  const sentinelRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    setMounted(true);
    setMessages(loadMessages());
    setMinimized(loadMinimized());
    setPillPos(loadPillPos());
    const id = loadDockSessionId();
    sessionIdRef.current = id;
    setDockSessionId(id);
  }, []);

  // Re-clamp the pill if the window shrinks under it. Also runs once on mount
  // to bring a persisted position back on-screen: a pill saved on a larger
  // window (or a second monitor) restored at raw x/y can otherwise land fully
  // off the viewport, so the minimized assistant looks like it vanished.
  useEffect(() => {
    if (!pillPos) return;
    const clampToViewport = () => {
      const el = pillRef.current;
      const r = el?.getBoundingClientRect();
      setPillPos(p => {
        if (!p) return p;
        const next = clampPill(p.x, p.y, r?.width ?? 130, r?.height ?? 36);
        // Return the SAME reference when nothing moved, so this effect (which
        // depends on pillPos) does not loop.
        if (next.x === p.x && next.y === p.y) return p;
        savePillPos(next);
        return next;
      });
    };
    // The pill may not be mounted on the first tick; defer a frame.
    const raf = requestAnimationFrame(clampToViewport);
    window.addEventListener('resize', clampToViewport);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', clampToViewport);
    };
  }, [pillPos]);

  const toggleMinimized = useCallback(() => {
    setMinimized(prev => {
      const next = !prev;
      saveMinimized(next);
      if (next) setExpanded(false);
      else setTimeout(() => textareaRef.current?.focus(), 0);
      return next;
    });
  }, []);

  // `/` (broadcast globally) places the caret in the composer. Ignored when this
  // surface is suppressed (ChatBot tab, Settings) so the full composer keeps the
  // focus; a minimized pill restores first, then focuses.
  useEffect(() => {
    const onFocusChat = () => {
      if (!hostVisible || !visible) return;
      if (minimized) { toggleMinimized(); return; }
      textareaRef.current?.focus();
    };
    window.addEventListener(CHAT_FOCUS_EVENT, onFocusChat);
    return () => window.removeEventListener(CHAT_FOCUS_EVENT, onFocusChat);
  }, [hostVisible, visible, minimized, toggleMinimized]);

  /**
   * Pointer drag for the minimized pill. A press that moves past a small
   * threshold becomes a drag (position persisted); otherwise it is a click that
   * restores the dock. Uses real pointer captures so the drag survives leaving
   * the button.
   */
  const onPillPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const el = pillRef.current;
    if (!el) return;
    e.preventDefault();
    const start = el.getBoundingClientRect();
    const rect = { w: start.width, h: start.height };
    const startX = e.clientX;
    const startY = e.clientY;
    const originX = start.left;
    const originY = start.top;
    let moved = false;
    let latest = clampPill(originX, originY, rect.w, rect.h);
    const PID = e.pointerId;

    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== PID) return;
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (!moved && Math.hypot(dx, dy) < 4) return;
      moved = true;
      latest = clampPill(originX + dx, originY + dy, rect.w, rect.h);
      // Move the live element directly for a jank-free drag; commit on release.
      el.style.left = `${latest.x}px`;
      el.style.top = `${latest.y}px`;
      el.style.right = 'auto';
      el.style.bottom = 'auto';
    };
    const onUp = (ev: PointerEvent) => {
      if (ev.pointerId !== PID) return;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      if (moved) {
        setPillPos(latest);
        savePillPos(latest);
      } else {
        // A click (no drag) restores the dock.
        toggleMinimized();
      }
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  };

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) { setHostVisible(true); return; }
    // Gate on the HOST TAB's visibility, never on viewport intersection: the
    // dashboard scrolls, so an intersection test on a zero-size sentinel reports
    // "not visible" mid-scroll and made the composer vanish. Walk up to the
    // keep-alive panel and read its own display/visibility (the same test the
    // keyboard layer uses), re-reading whenever the tab toggles `hidden`.
    const panel = el.closest<HTMLElement>('[data-keepalive-panel]') ?? el.parentElement;
    if (!panel) { setHostVisible(true); return; }
    const read = () => {
      const style = getComputedStyle(panel);
      const painted = typeof panel.getClientRects === 'function' && panel.getClientRects().length > 0;
      setHostVisible(painted && style.display !== 'none' && style.visibility !== 'hidden');
    };
    read();
    if (typeof MutationObserver === 'undefined') return;
    const mo = new MutationObserver(read);
    mo.observe(panel, { attributes: true, attributeFilter: ['class', 'style'] });
    return () => mo.disconnect();
  }, [mounted]);

  // A conversation always shows its transcript, regardless of focus.
  const showTranscript = (expanded || messages.length > 0 || busy) && !minimized;

  // Auto-grow the composer up to a cap (mirrors the reference composer).
  const resize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);
  useEffect(() => { resize(); }, [query, resize]);

  // Collapse the transcript when dismissed with Escape (captured so it does not
  // also exit Command Center fullscreen). Only active while the panel is open.
  useEffect(() => {
    if (!showTranscript) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || !isKeyboardEventAvailable(e)) return;
      e.preventDefault();
      e.stopPropagation();
      setExpanded(false);
      textareaRef.current?.blur();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [showTranscript]);

  const messagesRef = useRef(messages);
  messagesRef.current = messages;

  const append = useCallback((next: Message[]) => {
    // Keep the ref in lockstep synchronously: a send/retry reads it immediately
    // after writing, before React re-renders.
    messagesRef.current = next;
    setMessages(next);
    saveMessages(next);
  }, []);

  const newChat = useCallback(() => {
    append([]);
    setQuery('');
    setExpanded(false);
    messageQueue.clear();
    textareaRef.current?.focus();
  }, [append, messageQueue]);

  /**
   * Run one turn: append the user prompt + an empty assistant turn, then let
   * the shared run hook fill it. `history` is the transcript BEFORE the new
   * prompt, so a retry can pass a truncated transcript explicitly.
   */
  const startTurn = async (
    history: { role: 'user' | 'assistant'; content: string }[],
    userText: string,
    opts?: { resumeRunId?: string; context?: string },
  ) => {
    setQuery('');
    setExpanded(true);

    // Gap #17: the editor snapshot travels with the HARNESS turn as reference
    // material, while the visible user bubble stays the user's own words.
    const harnessText = opts?.context ? `${userText}\n\n${opts.context}` : userText;

    const userMsg: Message = { id: newId(), role: 'user', content: userText };
    const assistantId = newId();
    const startedAt = Date.now();
    // Seed an empty assistant turn the shared run hook fills (status, streamed
    // text, activities) — the answer appears as it is written.
    append([
      ...messagesRef.current,
      userMsg,
      { id: assistantId, role: 'assistant', content: '', status: 'queued', activities: [] },
    ]);

    const onTurn = (turn: AssistantTurn) => {
      const next = messagesRef.current.map(m =>
        m.id === assistantId
          ? {
              ...m,
              content: turnText(turn),
              status: turn.status,
              activities: turn.activities,
              delegations: turn.delegations,
              changes: turn.changes,
              error: turn.error,
              research: turn.research,
              memory: turn.memory,
              vision: turn.vision,
              documents: turn.documents,
              images: turn.images,
              video: turn.video,
              analysis: turn.analysis,
              acquisition: turn.acquisition,
              codeVerification: turn.codeVerification,
              autonomy: turn.autonomy,
              capability: turn.capability,
              capabilityBuild: turn.capabilityBuild,
              responsePlan: turn.responsePlan,
              runId: turn.runId,
              model: turn.model,
              pendingApproval: turn.pendingApproval,
              pendingClarification: turn.pendingClarification,
              durationMs: Math.max(0, Date.now() - startedAt),
            }
          : m,
      );
      // Streaming frames update state only; persistence happens once the turn
      // is done (below) so a long stream does not write localStorage ~60×/s.
      messagesRef.current = next;
      setMessages(next);
      if (turn.done) saveMessages(next);
    };

    setBusy(true);
    try {
      const finalTurn = await agentRun.start({
        history,
        userText: harnessText,
        chatId: sessionIdRef.current,
        model: chatModel.displayModel || undefined,
        reasoningEffort: reasoningEffort.effort,
        resumeRunId: opts?.resumeRunId,
        onTurn,
      });
      // A finished reply is an incoming message, not a generic success.
      if (finalTurn.status === 'completed' && (finalTurn.finalText || finalTurn.text)) sound.message();
    } catch {
      // The hook already recorded the error on the turn; the sink rendered it.
    } finally {
      setBusy(false);
      // Persist the terminal transcript once (covers cancel/abort paths too).
      saveMessages(messagesRef.current);
    }
  };

  // ── Queue drain ────────────────────────────────────────────────────────
  // Run queued prompts one at a time as soon as the dock is idle. `drainingRef`
  // covers the gap between removing the head and `startTurn` marking the dock
  // busy (removing the item re-renders before the run does).
  const drainingQueueRef = useRef(false);
  useEffect(() => {
    const dockChatId = sessionIdRef.current;
    if (
      busy ||
      drainingQueueRef.current ||
      runManager.isBusy(dockChatId) ||
      messageQueue.paused(dockChatId)
    ) return;
    if (!messageQueue.peekFor(dockChatId)) return;
    drainingQueueRef.current = true;
    const history = messagesRef.current.map(m => ({ role: m.role, content: m.content }));
    void takeNextFor(dockChatId).then((next) => {
      if (!next) return;
      return startTurn(history, next.text);
    }).finally(() => {
      drainingQueueRef.current = false;
    });
    // Keyed on run state + queue contents; startTurn/refs are intentionally not
    // dependencies so the effect does not restart mid-drain.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, messageQueue.items]);

  const send = async () => {
    const text = query.trim();
    const context = editorContext ? formatEditorContext(editorContext) : undefined;
    if (!text && !context) return;
    const question = text || 'Explain the selected code.';
    // Busy means "queue it", never "drop it". The queue drains automatically as
    // each reply completes, in the order the prompts were typed. A queued prompt
    // carries the editor snapshot inline because the queue has no hidden field;
    // the drain then sends exactly what the bubble shows.
    if (busy || messageQueue.sizeFor(sessionIdRef.current) > 0) {
      const queued = context ? `${question}\n\n${context}` : question;
      if (messageQueue.enqueue(queued, undefined, sessionIdRef.current)) {
        setQuery('');
        clearEditorContext();
        sound.tick();
      }
      return;
    }
    const history = messagesRef.current.map(m => ({ role: m.role, content: m.content }));
    await startTurn(history, question, context ? { context } : undefined);
    clearEditorContext();
  };

  /** Run a follow-up turn from a response `controls` block (same path as send). */
  const handleFollowup = async (prompt: string) => {
    const text = prompt.trim();
    if (!text || busy) return;
    const history = messagesRef.current.map(m => ({ role: m.role, content: m.content }));
    await startTurn(history, text);
  };

  const cancel = () => {
    agentRun.cancel();
  };

  /** Rate an assistant turn; toggling the same rating clears it. */
  const feedbackMessage = (id: string, feedback: 'up' | 'down') => {
    const next = messagesRef.current.map(m =>
      m.id === id ? { ...m, feedback: m.feedback === feedback ? undefined : feedback } : m,
    );
    append(next);
  };

  /**
   * Retry/regenerate an assistant turn: drop it and re-run its preceding user
   * prompt. A turn that FAILED mid-run resumes its existing run from the last
   * checkpoint (the runtime reuses completed tool calls), so a long research
   * turn continues instead of starting over. Anything else re-runs fresh.
   */
  const retryMessage = (assistantId: string) => {
    if (busy) return;
    const msgs = messagesRef.current;
    const idx = msgs.findIndex(m => m.id === assistantId);
    if (idx < 1) return;
    let userId = -1;
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (msgs[i].role === 'user') { userId = i; break; }
    }
    if (userId === -1) return;
    const failed = msgs[idx];
    const resumeRunId =
      failed.role === 'assistant' &&
      failed.status === 'error' &&
      failed.runId &&
      idx === msgs.length - 1
        ? failed.runId
        : undefined;
    const history = msgs.slice(0, userId).map(m => ({ role: m.role, content: m.content }));
    const text = msgs[userId].content;
    append(msgs.slice(0, userId));
    void startTurn(history, text, { resumeRunId });
  };

  // Upload attached documents into the dock's own session folder, where the
  // front door picks them up as context for the next run.
  const handleAttach = async (files: FileList | File[] | PickedFile[] | null) => {
    if (!files) return;
    for (const item of Array.from(files as ArrayLike<File | PickedFile>)) {
      const file = item && typeof item === 'object' && 'file' in item ? item.file : (item as File);
      const meta = item && typeof item === 'object' && 'file' in item ? item.meta : undefined;
      if (file.size > SESSION_LIMITS.maxFileBytes) continue;
      const bytes = new Uint8Array(await file.arrayBuffer());
      try {
        await uploadSessionFile(sessionIdRef.current, 'context', file.name, bytes, meta);
      } catch {
        // Surface nothing inline here; the session bar lists what succeeded.
      }
    }
  };

  // A draft is sendable whether or not a reply is running: while busy the send
  // action queues it instead of dropping it.
  const canSend = query.trim().length > 0 || Boolean(editorContext);
  const canQueue = busy && canSend;

  /** Promote a queued prompt and interrupt the current reply so it runs next. */
  const runQueuedNext = (id: string) => {
    messageQueue.promote(id);
    if (busy) void cancel();
  };

  const dock = hostVisible && visible && mounted && typeof document !== 'undefined' && createPortal(
    <div
      className={cn(
        'pointer-events-none fixed inset-x-0 bottom-[calc(4.5rem+env(safe-area-inset-bottom))] z-[90] flex flex-col items-center px-3 md:bottom-[max(0.75rem,env(safe-area-inset-bottom))]',
        className,
      )}
      data-nodrag
    >
      <NotebookProvider chatId={dockSessionId}>
      <div className="pointer-events-auto flex w-[min(560px,100%)] flex-col gap-2">
        {minimized ? (
          // Minimized: a compact pill. Click restores; drag repositions (the
          // position is persisted). Defaults to the bottom-right corner, and
          // when positioned we anchor by top/left so it never fights the layout.
          <button
            ref={pillRef}
            type="button"
            onPointerDown={onPillPointerDown}
            style={pillPos ? { left: pillPos.x, top: pillPos.y, right: 'auto', bottom: 'auto' } : undefined}
            className={cn(
              'fixed z-[90] inline-flex touch-none select-none items-center gap-2 rounded-full border border-white/12 dark:border-white/8 glass-card px-3.5 py-2 text-xs font-bold text-muted-foreground shadow-2xl shadow-black/50 transition-colors hover:text-primary',
              pillPos ? 'cursor-grab active:cursor-grabbing' : 'bottom-[calc(4.5rem+env(safe-area-inset-bottom))] right-4 ml-auto cursor-grab active:cursor-grabbing md:bottom-4',
            )}
            title="Drag to move · click to expand"
            aria-label="Expand the assistant (drag to move)"
            aria-expanded={false}
          >
            <Bot size={13} className="pointer-events-none text-primary" />
            <span className="pointer-events-none hidden sm:inline">Assistant</span>
            <Maximize2 size={13} className="pointer-events-none" />
          </button>
        ) : (
          <>
        {/* Transcript — expands above the composer. */}
        {showTranscript && (
          <div
            role="dialog"
            aria-label={title}
            className="flex max-h-[min(45vh,360px)] flex-col overflow-hidden rounded-[1.75rem] border border-white/12 dark:border-white/8 glass-card shadow-2xl shadow-black/50 animate-fade-in"
          >
            <div className="flex shrink-0 items-center gap-2 border-b border-border/10 px-4 py-2.5">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg border border-primary/25 bg-primary/15 text-primary">
                <Bot size={12} />
              </span>
              <p className="min-w-0 flex-1 truncate font-serif text-sm font-semibold text-foreground">{title}</p>
              <button
                type="button"
                onClick={newChat}
                className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:text-primary cursor-pointer"
                title="New chat"
              >
                <SquarePen size={13} />
              </button>
              <button
                type="button"
                onClick={() => { append([]); setExpanded(false); }}
                disabled={messages.length === 0}
                className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:text-rose-400 disabled:cursor-default disabled:opacity-30 cursor-pointer"
                title="Clear conversation"
              >
                <Trash2 size={13} />
              </button>
              <button
                type="button"
                onClick={() => { setExpanded(false); textareaRef.current?.blur(); }}
                className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                title="Collapse"
              >
                <Minimize2 size={13} />
              </button>
            </div>

            {messages.length === 0 ? (
              <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 px-6 py-8 text-center">
                <p className="text-sm font-semibold text-foreground">Ask anything</p>
                <p className="max-w-[34ch] text-xs leading-relaxed text-muted-foreground">
                  Try “research NVDA”, “market sentiment”, or “what changed today?”.
                </p>
              </div>
            ) : (
              <ChatHistory
                messages={messages}
                compact
                onEditMessage={(id, content) => {
                  const msgs = messagesRef.current;
                  const idx = msgs.findIndex(m => m.id === id);
                  if (idx === -1) return;
                  append(msgs.slice(0, idx));
                  setQuery(content);
                }}
                onResendMessage={(id) => {
                  const msgs = messagesRef.current;
                  const idx = msgs.findIndex(m => m.id === id);
                  if (idx === -1) return;
                  append(msgs.slice(0, idx));
                  setQuery(msgs[idx].content);
                }}
                onRetryMessage={retryMessage}
                onFeedback={feedbackMessage}
                onFollowup={handleFollowup}
              />
            )}
          </div>
        )}

        {/* Queued prompts — visible while the assistant is replying, so a
            follow-up is never silently lost and order is explicit. */}
        <MessageQueue
          items={messageQueue.items.filter((item) => item.chatId === sessionIdRef.current)}
          busy={busy}
          onEdit={messageQueue.update}
          onRemove={messageQueue.remove}
          onMove={messageQueue.move}
          onRunNow={runQueuedNext}
          onClear={() => messageQueue.clear(sessionIdRef.current)}
        />

        {/* W4: the active goal / loop status strip, above the composer. */}
        <GoalBar sessionId={dockSessionId} />

        {/* Composer — always visible, bottom-centre. */}
        <div
          ref={setComposerEl}
          data-voice={voiceActive ? 'active' : undefined}
          className="rounded-[1.75rem] border border-white/12 dark:border-white/8 glass-card p-2 shadow-2xl shadow-black/50"
        >
          <div className={cn('relative', voiceActive && 'h-[84px]')}>
            {editorContext && (
              <div className="mx-2 mb-1 flex items-center gap-1.5 rounded-lg border border-primary/25 bg-primary/5 px-2 py-1 text-[10.5px]">
                <span className="shrink-0 text-muted-foreground/70">Editor</span>
                <span className="min-w-0 flex-1 truncate font-mono text-foreground/85" title={editorContextLabel(editorContext)}>
                  {editorContextLabel(editorContext)}
                </span>
                {editorContext.truncated && (
                  <span className="shrink-0 text-muted-foreground/60">shortened</span>
                )}
                <button
                  type="button"
                  onClick={() => clearEditorContext()}
                  className="shrink-0 rounded px-1 text-muted-foreground hover:text-foreground"
                  aria-label="Remove editor context"
                  title="Remove editor context"
                >
                  ×
                </button>
              </div>
            )}
            <textarea
              ref={textareaRef}
              rows={1}
              value={query}
              onChange={e => setQuery(e.target.value)}
              readOnly={voiceActive}
              onKeyDown={e => {
                if (voiceActive) {
                  if (e.key === 'Escape') {
                    e.preventDefault();
                    voice.cancel();
                    return;
                  }
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    voice.stop();
                    return;
                  }
                }
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send();
                }
              }}
              placeholder={placeholder}
              aria-label={title}
              className={cn(
                'max-h-[200px] w-full resize-none bg-transparent px-3 pb-1 pt-2.5 text-sm leading-relaxed text-foreground outline-none scrollbar-hide placeholder:text-muted-foreground/70',
                voiceActive && 'pointer-events-none select-none opacity-0',
              )}
            />
            {voiceActive && (
              <div className="absolute inset-0 z-10">
                <VoiceFireField
                  meterRef={voice.meterRef}
                  phase={
                    voice.state === 'starting'
                      ? 'starting'
                      : voice.state === 'transcribing'
                        ? 'transcribing'
                        : 'recording'
                  }
                  transcript={query}
                  interim={voice.interim}
                  height={84}
                  className="h-full"
                />
              </div>
            )}
          </div>

          <div className="flex items-center gap-1.5 px-1">
            <DropdownMenu>
              <DropdownMenuTrigger
                className="flex h-9 w-9 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground cursor-pointer focus-visible:ring-0 focus-visible:outline-none"
                title="Add context, media or a tool"
                aria-label="Add context, media or a tool"
              >
                <Plus size={17} />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="top" className="w-56 glass-command-popover">
                <DropdownMenuItem onClick={() => setPickerMode('images')}>
                  <ImageIcon size={15} className="shrink-0" />
                  <span>Images and photos</span>
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => setPickerMode('all')}>
                  <Layers size={15} className="shrink-0" />
                  <span>Context</span>
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => { useCommandStore.getState().openCanvas(); useCommandStore.getState().dispatch({ tab: 'chat' }); }}>
                  <SquarePen size={15} className="shrink-0" />
                  <span>Canvas</span>
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => insertToken('/')}>
                  <Blocks size={15} className="shrink-0" />
                  <span>Skills</span>
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => insertToken('@')}>
                  <span aria-hidden className="flex h-[15px] w-[15px] shrink-0 items-center justify-center font-mono text-sm">@</span>
                  <span>Mention</span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              type="button"
              onClick={() => setExpanded(v => !v)}
              className={cn(
                'flex h-9 w-9 items-center justify-center rounded-full transition-colors cursor-pointer hover:bg-accent',
                expanded ? 'text-primary' : 'text-muted-foreground hover:text-foreground',
              )}
              title={expanded ? 'Collapse transcript' : 'Expand transcript'}
              aria-label={expanded ? 'Collapse transcript' : 'Expand transcript'}
              aria-expanded={expanded}
            >
              {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </button>
            {/* True minimize: shrink the whole dock to a restore pill. */}
            <button
              type="button"
              onClick={toggleMinimized}
              className="flex h-9 w-9 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground cursor-pointer"
              title="Minimize"
              aria-label="Minimize assistant"
            >
              <Minus size={17} />
            </button>

            {/* Model selector — the same real, persisted model picker the full
                ChatBot composer uses. Changing it here switches the assistant's
                model for the next turn and persists it to the settings vault.
                Always rendered (the trigger self-hides its label on small
                screens) so the model is changeable on mobile too. */}
            <div className="ml-1">
              <ModelSelector state={chatModel} />
            </div>
            {/* Per-question reasoning effort — the model selector's sibling, the
                same control the full ChatBot composer offers. */}
            <EffortControl
              value={reasoningEffort.effort}
              index={reasoningEffort.index}
              onIndexChange={reasoningEffort.setIndex}
              disabled={busy}
            />

            <div className="ml-auto flex items-center gap-1.5">
              <VoiceInputButtonView
                voice={voice}
                onStart={captureVoiceSelection}
                className="h-9 w-9"
                iconSize={17}
              />
              {!voiceActive && (busy ? (
                <>
                  {canQueue && (
                    <button
                      type="button"
                      onClick={() => void send()}
                      className="flex h-9 items-center gap-1 rounded-full border-0 bg-primary px-2.5 text-primary-foreground transition-colors hover:bg-primary/90 cursor-pointer"
                      title="Queue this prompt — it runs after the current reply"
                      aria-label="Queue this prompt"
                    >
                      <ArrowUp size={14} />
                      <span className="hidden text-[11px] font-semibold min-[420px]:inline">Queue</span>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => void cancel()}
                    className="flex h-9 w-9 items-center justify-center rounded-full border-0 bg-foreground text-background transition-colors hover:bg-foreground/90 cursor-pointer"
                    title="Stop generating"
                    aria-label="Stop generating"
                  >
                    <Pause size={14} fill="currentColor" />
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => void send()}
                  disabled={!canSend}
                  className={cn(
                    'flex h-9 w-9 items-center justify-center rounded-full transition-all cursor-pointer',
                    canSend
                      ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                      : 'bg-accent/40 text-muted-foreground disabled:opacity-50 cursor-not-allowed',
                  )}
                  title="Send"
                  aria-label="Send"
                >
                  <ArrowUp size={17} />
                </button>
              ))}
            </div>
          </div>
          {pickerMode ? (
            <FileSourcePicker
              mode={pickerMode}
              chatId={sessionIdRef.current}
              onClose={() => setPickerMode(null)}
              onPick={(files) => { void handleAttach(files); }}
            />
          ) : null}
        </div>
        {pendingApproval && (
          <ApprovalCard approval={pendingApproval} anchor={composerEl} onDecide={agentRun.decide} />
        )}
        {pendingClarification && (
          <ClarificationCard
            clarification={pendingClarification}
            anchor={composerEl}
            onAnswer={agentRun.answer}
          />
        )}
          </>
        )}
      </div>
      </NotebookProvider>
    </div>,
    document.body,
  );

  return (
    <>
      {/* Visibility sentinel — stays in the host subtree so the observer sees
          the keep-alive tab's display:none. Zero-size, non-interactive. */}
      <span ref={sentinelRef} aria-hidden className="pointer-events-none absolute h-0 w-0" />
      {dock}
    </>
  );
}
