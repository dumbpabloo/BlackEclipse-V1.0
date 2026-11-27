'use client';

import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type ComponentType } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, ArrowDown, Clock, Cpu, Pencil, RotateCcw, Check, X, Copy, ThumbsUp, ThumbsDown, SquarePen, GitBranch, MessageSquareText } from '@/lib/icons';
import { cn } from '@/lib/utils';
import { Message, type MessageQuote, type ShellRun } from '@/types/chat';
import { boundQuote } from '@/lib/chat/quote';
import { shellOutput } from '@/lib/chat/shell';
import { selectionGapFillers, type Filler } from '@/lib/chat/selection-band';
import { QuoteCard } from './quote-card';
import { ShellRunCard } from './shell-run-card';
import { Markdown } from './markdown';
import { ResponseRuntimeProvider } from '@/components/response/figure-context';
import { buildFigureResolver } from '@/lib/response/refs';
import { hasResponseFence } from '@/lib/response/fence-check';
import { ResponseStatusLine } from './response-status';
import { ActivityFeed } from './activity-feed';
import { ParallelAgentsPanel } from './parallel-agents-panel';
import { ChangeCards } from './change-cards';
import { ResearchEvidencePanel } from './research-evidence-panel';
import { VisionPanel } from './vision-panel';
import { ImageGalleryPanel } from './image-gallery';
import { VideoPanel } from './video-panel';
import { DocumentPanel } from './document-panel';
import { MemoryActionPanel } from './memory-action-panel';
import { CanvasInlineNotice } from '@/components/screens/canvas/canvas-banner';

import { AcquisitionPanel } from './acquisition-panel';
import { CapabilityCard } from './capability-card';
import { MessageRail } from './message-rail';
import { MessageAttachments } from './message-attachments';
import { TtsButton } from './tts-button';
import { useAutoSpeak } from '@/lib/tts';
import { AI_OUTPUT_FULL } from '@/lib/legal/disclosure';
import { BotAvatar } from '@/components/bots/bot-avatar';
import { GhostAvatar } from '@/components/bots/ghost-avatar';
import { avatarFor, useBotAvatarStore } from '@/lib/bots/avatar-store';
import { ghostSpecFromId } from '@/lib/bots/ghost';
import type { ResponseMode, TurnError } from '@/lib/agent/response-model';

/**
 * True when a turn's markdown merely restates its own error. A failed command
 * streams BOTH a readable `output` (which becomes the turn text) and an
 * `error.message`; without this the transcript shows the same sentence twice —
 * once in the error card, once in the body. Compared with punctuation/whitespace
 * stripped so `**Command failed.** <detail>` matches `<detail>`.
 */
function restatesError(content: string, error?: TurnError): boolean {
  if (!error?.message) return false;
  const strip = (s: string) => s.replace(/[*_`~>#\-\s]+/g, ' ').trim().toLowerCase();
  const body = strip(content);
  const message = strip(error.message);
  return message.length > 0 && body.includes(message);
}

interface ChatHistoryProps {
  messages: Message[];
  /**
   * `false` (default) is the full-page ChatBot layout (centered wide column,
   * user bubbles only — assistant text renders wide like a code agent).
   * `true` fills a narrow container (e.g. the floating ChatDock) edge-to-edge.
   */
  compact?: boolean;
  /**
   * Called when the user edits a previously-sent prompt. The caller decides
   * what "editing" means (typically: truncate the thread at that message and
   * re-send) — the component only reports the new text.
   */
  onEditMessage?: (id: string, content: string) => void;
  /** Re-send a user prompt as-is (the "redirect" action on a user bubble). */
  onResendMessage?: (id: string) => void;
  /** Retry a failed assistant turn (re-runs its preceding user prompt). */
  onRetryMessage?: (id: string) => void;
  /** Open a document a user turn carried (paste preview or session file). */
  onOpenAttachment?: (name: string) => void;
  /** Rate an assistant turn. The caller persists the choice on the message. */
  onFeedback?: (id: string, feedback: 'up' | 'down') => void;
  /** Quote a selected passage (or a whole message) into the composer. */
  onQuote?: (quote: MessageQuote) => void;
  /** Copy the conversation through this message into a new chat. */
  onForkMessage?: (id: string) => void;
  /** Run a follow-up agent turn (used by response `controls` blocks). */
  onFollowup?: (prompt: string) => void;
  /** Open this assistant message's content in the Canvas reading dock. */
  onOpenCanvas?: (msg: Message) => void;
  /**
   * The assistant message whose response is currently open in Canvas. Its body
   * is replaced by a compact notice (the content lives in the Canvas surface).
   */
  canvasMessageId?: string | null;
  /**
   * Optional external ref to the transcript's scroll element. The full-page
   * ChatBot wrapper uses it to forward wheel/trackpad scrolling from the
   * composer/toolbar area (siblings of the transcript in the DOM, so the browser
   * cannot chain their wheel events to it) back onto the transcript.
   */
  scrollRef?: React.RefObject<HTMLDivElement | null>;
  /**
   * Extra bottom padding (px) added to the scroll area. The full-page ChatBot
   * uses this to reserve room for the FLOATING composer that overlays the
   * transcript, so the last message can scroll clear of it instead of hiding
   * underneath.
   */
  bottomInset?: number;
}

/** How close to the bottom counts as "following the stream". */
const NEAR_BOTTOM_PX = 96;

/**
 * A user turn longer than this is collapsed to an excerpt. A pasted doc is an
 * attachment now, but a long typed prompt (or a paste on a surface without the
 * attachment hook) must not paint an unscrollable wall of text.
 */
const LONG_USER_MESSAGE_CHARS = 2_000;
const USER_MESSAGE_PREVIEW_CHARS = 800;

/** The floating "Quote" chip anchored to a text selection. */
interface SelectionAction {
  /** Fixed-position chip anchor (the cursor for a pointer selection). */
  x: number;
  top: number;
  /** No room above the anchor — draw the chip below it instead. */
  below: boolean;
  text: string;
  messageId: string;
  role: 'user' | 'assistant';
  author?: string;
  /**
   * Empty bands between the selected lines, positioned relative to the
   * quote-content wrapper (rendered BEHIND the prose, never over it). The
   * native highlight only paints glyph boxes, so the transcript's line-height
   * and block rhythm leave stripes that make a selection look broken.
   */
  fillers: Filler[];
}

/**
 * The presentation mode the Intelligent Response Orchestrator chose, as a short
 * human label. `text` is omitted — it is the default and not worth a chip.
 */
const RESPONSE_MODE_LABELS: Partial<Record<ResponseMode, string>> = {
  explanation: 'Explanation',
  visualization: 'Visualization',
  simulation: 'Simulation',
  workflow: 'Workflow',
  composite: 'Composite',
};

/** Compact, honest turn duration: "820ms", "2.3s", "1m 5s". */
function formatTurnDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

/**
 * Assistant prose is the expensive part to render (full Markdown parse). It only
 * changes when its text does, so memoizing it stops a streaming turn from
 * re-parsing every *other* turn in the transcript.
 *
 * The structured renderer (Zod contract, block registry, planners) is loaded on
 * demand: plain Markdown paints first, then the answer upgrades in place once
 * that chunk is ready. Streaming turns stay on Markdown so a half-written block
 * never flickers into a structured render.
 */
type AssistantContentProps = {
  content: string;
  compact: boolean;
  streaming: boolean;
  onFollowup?: (prompt: string) => void;
};

const AssistantContent = memo(function AssistantContent(props: AssistantContentProps) {
  const [Ready, setReady] = useState<ComponentType<AssistantContentProps> | null>(null);

  useEffect(() => {
    let live = true;
    void import('./assistant-content').then((m) => {
      if (live) setReady(() => m.AssistantContent);
    });
    return () => {
      live = false;
    };
  }, []);

  if (Ready) return <Ready {...props} />;
  return (
    <Markdown
      content={props.content}
      compact={props.compact}
      streaming={props.streaming}
      onFollowup={props.onFollowup}
      className="w-full"
    />
  );
});

export function ChatHistory({ messages, compact = false, onEditMessage, onResendMessage, onRetryMessage, onOpenAttachment, onFeedback, onQuote, onForkMessage, onFollowup, onOpenCanvas, canvasMessageId, scrollRef: externalScrollRef, bottomInset }: ChatHistoryProps) {
  const internalScrollRef = useRef<HTMLDivElement>(null);
  const scrollRef = externalScrollRef ?? internalScrollRef;
  const rootRef = useRef<HTMLDivElement>(null);
  // Wrapper around all messages; carries selection gap fillers behind the text.
  const quoteContentRef = useRef<HTMLDivElement>(null);
  const avatars = useBotAvatarStore((s) => s.avatars);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [copiedId, setCopiedId] = useState<string | null>(null);
  // One long user turn expanded at a time; the rest stay compact.
  const [expandedId, setExpandedId] = useState<string | null>(null);
  // Selected-text quote action (null when there is no usable selection).
  const [selectionAction, setSelectionAction] = useState<SelectionAction | null>(null);
  // A source message flashed briefly after "Jump to".
  const [flashId, setFlashId] = useState<string | null>(null);
  // The selection listener must not re-subscribe on every streamed token.
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  // The "Latest" pill is portaled to <body> so its backdrop-filter samples the
  // real page. The transcript sits inside the app shell's own `.glass-card`,
  // which is a backdrop root and would silently flatten a nested glass surface —
  // and a CSS transform on an ancestor does the same. This tracks the
  // transcript's on-screen box so the portaled pill can be positioned over it.
  const [rootRect, setRootRect] = useState<{ left: number; top: number; width: number; bottom: number } | null>(null);

  // Opt-in: speak each newly completed assistant reply once. Primed on mount so
  // loading a saved conversation never replays its history.
  useAutoSpeak(messages);

  // Track whether the viewport is pinned to the bottom. When it is, we follow the
  // stream and hide the scrollbar; when the user scrolls up we reveal the
  // scrollbar and a "jump to latest" button so the thread still reads scrollable.
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);
  // A short custom scroll indicator. The native transcript bar spans the FULL
  // height of the scroll container, which reads as a long band running down the
  // right edge and under the header. It is hidden and replaced by this compact
  // thumb (capped at ~28% of the track) that tracks the reading position.
  const [bar, setBar] = useState<{ top: number; height: number } | null>(null);
  // The thumb is an auto-hiding overlay: it appears while scrolling, hovering the
  // transcript or dragging, then fades out when idle.
  const [barVisible, setBarVisible] = useState(false);
  const barHideTimer = useRef<number | null>(null);
  const barDraggingRef = useRef(false);
  // True while the transcript is scrolled by code (auto-follow), so those scroll
  // events don't reveal the scrollbar.
  const programmaticScrollRef = useRef(false);
  const clearBarTimer = useCallback(() => {
    if (barHideTimer.current != null) {
      window.clearTimeout(barHideTimer.current);
      barHideTimer.current = null;
    }
  }, []);
  const scheduleBarHide = useCallback((delay = 1200) => {
    clearBarTimer();
    barHideTimer.current = window.setTimeout(() => {
      if (!barDraggingRef.current) setBarVisible(false);
    }, delay);
  }, [clearBarTimer]);
  const revealBar = useCallback(() => {
    setBarVisible(true);
    scheduleBarHide();
  }, [scheduleBarHide]);
  useEffect(() => () => clearBarTimer(), [clearBarTimer]);
  const updateBar = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const { scrollTop, scrollHeight, clientHeight } = el;
    const overflow = scrollHeight - clientHeight;
    if (overflow <= 1) { setBar(null); return; }
    const height = Math.max(28, Math.min(clientHeight * 0.28, clientHeight * (clientHeight / scrollHeight)));
    const top = (scrollTop / overflow) * (clientHeight - height);
    setBar({ top, height });
  }, []);
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const next = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    atBottomRef.current = next;
    setAtBottom(next);
    updateBar();
    // Auto-follow (new/streamed messages) moves the transcript programmatically;
    // that is not "using" the scrollbar, so it must not reveal it. Only genuine
    // user scrolling/dragging does.
    if (!programmaticScrollRef.current) revealBar();
  }, [updateBar, revealBar]);

  const scrollToBottom = useCallback((smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    atBottomRef.current = true;
    setAtBottom(true);
  }, []);

  // Drag the custom scroll thumb: the transcript hides the native bar
  // (`scrollbar-hide`), so this thumb IS the scrollbar — it must be grabbable,
  // not just an indicator, or scrolling reads as broken.
  const [barDragging, setBarDragging] = useState(false);
  const onBarPointerDown = useCallback((e: { clientY: number; preventDefault: () => void }) => {
    const el = scrollRef.current;
    if (!el || !bar) return;
    const track = el.clientHeight - bar.height;
    const overflow = el.scrollHeight - el.clientHeight;
    if (track <= 0 || overflow <= 0) return;
    e.preventDefault();
    const startY = e.clientY;
    const startTop = el.scrollTop;
    setBarDragging(true);
    barDraggingRef.current = true;
    revealBar();
    const move = (ev: PointerEvent) => {
      const delta = ev.clientY - startY;
      el.scrollTop = startTop + (delta / track) * overflow;
      onScroll();
    };
    const up = () => {
      setBarDragging(false);
      barDraggingRef.current = false;
      scheduleBarHide(500);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      document.body.style.userSelect = '';
    };
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  }, [bar, onScroll, revealBar, scheduleBarHide]);

  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = scrollRef.current;
    if (!el) return;
    programmaticScrollRef.current = true;
    el.scrollTop = el.scrollHeight;
    // Clear on the next frame, after the resulting scroll event has fired.
    const id = requestAnimationFrame(() => { programmaticScrollRef.current = false; });
    return () => cancelAnimationFrame(id);
  }, [messages]);

  // Keep the thumb's size/position current on layout changes. It stays HIDDEN
  // here (no `revealBar()`) — the scrollbar only appears once the user actually
  // scrolls or drags it.
  useLayoutEffect(() => {
    updateBar();
  }, [updateBar, messages]);

  // Keep the portaled "Latest" pill aligned with the transcript box, and keep
  // the short scroll thumb sized to the (possibly resized) transcript.
  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const update = () => {
      const r = el.getBoundingClientRect();
      setRootRect({ left: r.left, top: r.top, width: r.width, bottom: r.bottom });
      updateBar();
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    if (scrollRef.current) observer.observe(scrollRef.current);
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      observer.disconnect();
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [messages, updateBar]);

  const jumpToMessage = useCallback((messageId: string) => {
    const el = scrollRef.current?.querySelector<HTMLElement>(
      `[data-message-id="${typeof CSS !== 'undefined' && typeof CSS.escape === 'function' ? CSS.escape(messageId) : messageId}"]`,
    );
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setFlashId(messageId);
    window.setTimeout(() => setFlashId((current) => (current === messageId ? null : current)), 1400);
  }, [scrollRef]);

  const quoteMessage = (msg: Message) => {
    if (!onQuote || !msg.content.trim()) return;
    const bounded = boundQuote(msg.content);
    if (!bounded.text) return;
    onQuote({
      text: bounded.text,
      messageId: msg.id,
      role: msg.role,
      ...(msg.author?.name ? { author: msg.author.name } : {}),
      ...(bounded.truncated ? { truncated: true } : {}),
    });
  };

  // Selected-text quoting. Only selections inside a `[data-quotable-content]`
  // block count, so quoting an activity row, toolbar or attachment chip cannot
  // happen by accident. The chip is portaled to <body> so glass/backdrop and
  // overflow clipping cannot hide it, and it is anchored to the pointer (the
  // cursor the user released on), not to the middle of a multi-line range — the
  // old centre-top anchor covered prose and read as if it were behind it.
  useEffect(() => {
    if (!onQuote) return;
    const root = rootRef.current;
    if (!root) return;

    // Keyboard selections have no pointer; fall back to the range's own box.
    let pointer: { x: number; y: number } | null = null;
    // Rough pill box used only for viewport clamping (the chip is translate-
    // centred, so half a typical width is enough to keep it on screen).
    const CHIP_HALF_W = 56;
    const CHIP_H = 32;
    const EDGE = 8;

    const readSelection = (): SelectionAction | null => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null;
      const range = selection.getRangeAt(0);
      const node = range.startContainer;
      const startEl = node.nodeType === Node.ELEMENT_NODE
        ? (node as Element)
        : node.parentElement;
      const content = startEl?.closest('[data-quotable-content]');
      if (!content || !root.contains(content)) return null;
      const messageEl = content.closest('[data-message-id]');
      const messageId = messageEl?.getAttribute('data-message-id');
      if (!messageId) return null;
      const message = messagesRef.current.find((m) => m.id === messageId);
      if (!message) return null;
      const bounded = boundQuote(selection.toString());
      if (!bounded.text) return null;
      const rect = range.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return null;

      // The chip follows the cursor HORIZONTALLY but always floats above the
      // passage itself, so it never covers the text it is quoting.
      const anchorX = pointer ? pointer.x : rect.left + rect.width / 2;
      const x = Math.min(
        Math.max(anchorX, EDGE + CHIP_HALF_W),
        Math.max(EDGE + CHIP_HALF_W, window.innerWidth - EDGE - CHIP_HALF_W),
      );
      const above = rect.top - 10;
      const below = above - CHIP_H < EDGE;
      const top = below ? rect.bottom + 10 : above;

      // Fillers live in the content wrapper's coordinate space so they scroll
      // with the transcript and paint behind the prose (negative z-index).
      const host = quoteContentRef.current;
      const hostRect = host?.getBoundingClientRect();
      const fillers = hostRect
        ? selectionGapFillers(Array.from(range.getClientRects())).map((filler) => ({
            left: filler.left - hostRect.left,
            top: filler.top - hostRect.top,
            width: filler.width,
            height: filler.height,
          }))
        : [];

      return {
        x,
        top,
        below,
        text: bounded.text,
        messageId,
        role: message.role,
        ...(message.author?.name ? { author: message.author.name } : {}),
        fillers,
      };
    };

    const update = () => setSelectionAction(readSelection());
    const clear = () => setSelectionAction(null);
    // A new gesture starts a new anchor; selectionchange during the drag uses
    // the range, pointerup swaps to the release point.
    const onPointerDown = () => { pointer = null; };
    const onPointerUp = (event: PointerEvent) => {
      if (event.button !== 0) return;
      pointer = { x: event.clientX, y: event.clientY };
      window.setTimeout(update, 0);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        clear();
        return;
      }
      // Keyboard-grown selections are not cursor-anchored.
      pointer = null;
      update();
    };
    const onSelectionChange = () => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed) clear();
      else update();
    };
    const onScroll = () => clear();

    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('pointerup', onPointerUp);
    document.addEventListener('keyup', onKeyUp);
    document.addEventListener('selectionchange', onSelectionChange);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('pointerup', onPointerUp);
      document.removeEventListener('keyup', onKeyUp);
      document.removeEventListener('selectionchange', onSelectionChange);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
    };
  }, [onQuote]);

  const startEdit = (msg: Message) => {
    setEditingId(msg.id);
    setDraft(msg.content);
  };

  const commitEdit = () => {
    if (editingId && draft.trim()) onEditMessage?.(editingId, draft.trim());
    setEditingId(null);
  };

  const copyMessage = async (msg: Message) => {
    try {
      // Copy READABLE text: a structured answer is projected to Markdown instead
      // of copying the raw eclipse-response JSON. The projector (and its Zod
      // contract) is loaded only when a copy actually needs it.
      const text = hasResponseFence(msg.content)
        ? (await import('@/lib/response/projection')).projectContentMarkdown(msg.content)
        : msg.content;
      await navigator.clipboard.writeText(text);
      setCopiedId(msg.id);
      setTimeout(() => setCopiedId((c) => (c === msg.id ? null : c)), 1500);
    } catch {
      // Clipboard unavailable — leave the button unchanged.
    }
  };

  const [copiedShellId, setCopiedShellId] = useState<string | null>(null);

  const copyShellOutput = async (messageId: string, run: ShellRun) => {
    try {
      await navigator.clipboard.writeText(shellOutput(run));
      setCopiedShellId(messageId);
      setTimeout(() => setCopiedShellId((c) => (c === messageId ? null : c)), 1500);
    } catch {
      // Clipboard unavailable — leave the button unchanged.
    }
  };

  return (
    <div
      ref={rootRef}
      className="relative flex-1 min-h-0 w-full"
      style={{ fontFamily: 'var(--chat-font, var(--font-sans))' }}
    >
      <div
        ref={scrollRef}
        onScroll={onScroll}
        style={bottomInset ? { paddingBottom: bottomInset } : undefined}
        className={cn(
          'h-full w-full overflow-y-auto min-h-0 scrollbar-hide selection:bg-primary/30',
          compact ? 'p-3 pr-1.5' : 'px-4 md:px-6 pr-6 md:pr-5 pb-6',
        )}
      >
      <div ref={quoteContentRef} className="relative isolate">
        {selectionAction?.fillers.length ? (
          // Behind the prose: `-z-10` inside the isolated wrapper paints above
          // the page surface but under every glyph, so the bands fill the
          // rhythm without tinting or overlapping selected text.
          <div aria-hidden className="pointer-events-none absolute inset-0 -z-10">
            {selectionAction.fillers.map((filler, i) => (
              <span
                key={i}
                className="absolute rounded-[2px] bg-primary/30"
                style={{ left: filler.left, top: filler.top, width: filler.width, height: filler.height }}
              />
            ))}
          </div>
        ) : null}
        <div className={compact ? 'space-y-3' : 'space-y-6'}>
        {messages.map((msg) => {
          const working = msg.role === 'assistant' && msg.status != null && msg.status !== 'completed';
          const inCanvas = msg.role === 'assistant' && canvasMessageId != null && canvasMessageId === msg.id;
          return (
            <div
              key={msg.id}
              data-message-id={msg.id}
              className={cn(
                'group animate-fade-in-up flex flex-col',
                msg.role === 'user' ? 'items-end' : 'items-start',
                flashId === msg.id && 'rounded-2xl ring-2 ring-primary/40 ring-offset-2 ring-offset-background transition-shadow',
              )}
            >
              {/* A settled error renders as the error card below (not the status
                  line), so the failure reads once, not as two "something went
                  wrong" lines stacked. */}
              {msg.role === 'assistant' && msg.status != null && !msg.error && !(msg.activities && msg.activities.length > 0) && !(msg.delegations && msg.delegations.length > 0) && (
                <div className={cn(msg.content ? 'mb-2' : '')}>
                  <ResponseStatusLine status={msg.status} activities={msg.activities} note={msg.statusNote} compact={compact} />
                </div>
              )}

              {msg.role === 'assistant' && msg.activities && msg.activities.length > 0 && (
                <ActivityFeed
                  activities={msg.activities}
                  status={msg.status}
                  compact={compact}
                />
              )}

              {/* Delegated work sits WITH the activity rows (below them), not as a
                  card above the turn: it is runtime detail, not the answer. */}
              {msg.role === 'assistant' && msg.delegations && msg.delegations.length > 0 && (
                <ParallelAgentsPanel delegations={msg.delegations} compact={compact} />
              )}

              {msg.role === 'assistant' ? (
                <>
                  {msg.author && (
                    // A reply from a bot/group the user `@`-mentioned, not the
                    // main Eclipse agent — show who produced it.
                    <div className="mb-1 flex items-center gap-1.5">
                      {msg.author.kind === 'group'
                        ? <GhostAvatar spec={ghostSpecFromId(msg.author.id)} size={18} title={msg.author.name} />
                        : <BotAvatar botId={msg.author.id} spec={avatarFor(avatars, msg.author.id)} size={18} title={msg.author.name} />}
                      <span className="text-[11px] font-semibold text-muted-foreground">{msg.author.name}</span>
                    </div>
                  )}
                  {msg.error && (
                    <div
                      role="alert"
                      className="relative mb-3 w-full overflow-hidden rounded-2xl border border-rose-500/25 glass-card"
                    >
                      {/* Rose wash sits OVER the glass so the surface still
                          frosts (a solid rose fill would kill the blur). */}
                      <span
                        aria-hidden
                        className="pointer-events-none absolute inset-0 bg-gradient-to-br from-rose-500/[0.14] via-rose-500/[0.05] to-transparent"
                      />
                      <div className="relative flex items-start gap-3 px-3.5 pb-4 pt-3">
                        <span className="mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-xl border border-rose-500/30 bg-rose-500/15 text-rose-300">
                          <AlertTriangle size={14} />
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            <p className="text-[13px] font-semibold tracking-tight text-rose-100">
                              Something went wrong
                            </p>
                            {msg.error.code && (
                              <span className="rounded-full border border-rose-500/25 bg-rose-500/10 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-rose-300/80">
                                {msg.error.code}
                              </span>
                            )}
                          </div>
                          <p className="mt-1 break-words text-[12.5px] leading-relaxed text-rose-200/80">
                            {msg.error.message}
                          </p>
                          {onRetryMessage && (
                            <button
                              type="button"
                              onClick={() => onRetryMessage(msg.id)}
                              className="mt-2.5 inline-flex items-center gap-1.5 rounded-full border border-rose-500/30 bg-rose-500/10 px-3 py-1 text-[11px] font-semibold text-rose-100 transition-colors hover:bg-rose-500/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400/40 cursor-pointer"
                            >
                              <RotateCcw size={11} /> Retry
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  )}
                  {inCanvas ? (
                    <CanvasInlineNotice />
                  ) : (
                    <>
                  {msg.changes && msg.changes.length > 0 && (
                    <ChangeCards changes={msg.changes} compact={compact} />
                  )}
                  {msg.content && !restatesError(msg.content, msg.error) && (
                    <div data-quotable-content className="w-full">
                      <ResponseRuntimeProvider
                        value={{ figureResolver: buildFigureResolver({ analysis: msg.analysis }), stateKey: msg.id }}
                      >
                        <AssistantContent content={msg.content} compact={compact} streaming={working} onFollowup={onFollowup} />
                      </ResponseRuntimeProvider>
                    </div>
                  )}
                  {!working && msg.content && !msg.error && !restatesError(msg.content, msg.error) && (
                    <p className="mt-2 text-[10.5px] leading-relaxed text-muted-foreground/60">
                      {AI_OUTPUT_FULL}
                    </p>
                  )}
                  {msg.research && (
                    <div className="mt-2 flex w-full flex-col gap-2">
                      <div className="w-full min-w-0 [&>div]:mt-0">
                        <ResearchEvidencePanel research={msg.research} compact={compact} />
                      </div>
                    </div>
                  )}
                  {msg.vision ? (
                    <VisionPanel vision={msg.vision} compact={compact} />
                  ) : null}
                  {msg.acquisition ? (
                    <AcquisitionPanel manifest={msg.acquisition} compact={compact} />
                  ) : null}
                  {msg.capabilityBuild ? (
                    <CapabilityCard
                      view={msg.capabilityBuild}
                      compact={compact}
                      onFollowup={onFollowup}
                    />
                  ) : null}
                  {msg.images ? (
                    <ImageGalleryPanel images={msg.images} compact={compact} />
                  ) : null}
                  {msg.video ? (
                    <VideoPanel video={msg.video} compact={compact} onFollowup={onFollowup} />
                  ) : null}
                  {msg.documents ? (
                    <DocumentPanel documents={msg.documents} compact={compact} />
                  ) : null}
                    </>
                  )}
                  {msg.memory ? (
                    <MemoryActionPanel memory={msg.memory} runId={msg.runId} compact={compact} />
                  ) : null}
                  {!working && (msg.content || msg.model?.model || msg.durationMs != null) && (
                    <div className="hover-reveal mt-1 flex w-full flex-wrap items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                      {msg.responsePlan && RESPONSE_MODE_LABELS[msg.responsePlan.mode] && (
                        <span
                          className={cn(
                            'inline-flex items-center gap-1 rounded-full border border-border/50 bg-accent/40 px-2 py-0.5 font-medium text-muted-foreground',
                            compact ? 'text-[10px]' : 'text-[11px]',
                          )}
                          title={`Presented as: ${RESPONSE_MODE_LABELS[msg.responsePlan.mode]} (chosen by the response orchestrator)`}
                        >
                          {RESPONSE_MODE_LABELS[msg.responsePlan.mode]}
                        </span>
                      )}
                      {msg.durationMs != null && (
                        <span
                          className={cn(
                            'inline-flex items-center gap-1 font-mono text-muted-foreground/60 tabular-nums',
                            compact ? 'text-[10px]' : 'text-[11px]',
                          )}
                          title="Time this reply took"
                        >
                          <Clock size={compact ? 10 : 11} className="shrink-0 opacity-70" />
                          {formatTurnDuration(msg.durationMs)}
                        </span>
                      )}
                      {msg.content && (
                        <>
                          <button
                            type="button"
                            onClick={() => void copyMessage(msg)}
                            className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                            title={copiedId === msg.id ? 'Copied' : 'Copy response'}
                            aria-label="Copy response"
                          >
                            {copiedId === msg.id ? <Check size={11} /> : <Copy size={11} />}
                          </button>
                          <TtsButton content={msg.content} speakKey={msg.id} />
                          {onQuote && (
                            <button
                              type="button"
                              onClick={() => quoteMessage(msg)}
                              className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                              title="Quote this response"
                              aria-label="Quote this response"
                            >
                              <MessageSquareText size={11} />
                            </button>
                          )}
                          {onForkMessage && (
                            <button
                              type="button"
                              onClick={() => onForkMessage(msg.id)}
                              className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                              title="Fork conversation from here"
                              aria-label="Fork conversation from here"
                            >
                              <GitBranch size={11} />
                            </button>
                          )}
                          {onOpenCanvas && (
                            <button
                              type="button"
                              onClick={() => onOpenCanvas(msg)}
                              className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                              title="Open in Canvas (read at full width)"
                              aria-label="Open in Canvas"
                            >
                              <SquarePen size={11} />
                            </button>
                          )}
                          {onRetryMessage && (
                            <button
                              type="button"
                              onClick={() => onRetryMessage(msg.id)}
                              className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                              title="Regenerate response"
                              aria-label="Regenerate response"
                            >
                              <RotateCcw size={11} />
                            </button>
                          )}
                          {onFeedback && (
                            <span className="ml-0.5 flex items-center gap-0.5 border-l border-border/40 pl-1.5">
                              <button
                                type="button"
                                onClick={() => onFeedback(msg.id, 'up')}
                                aria-pressed={msg.feedback === 'up'}
                                className={cn(
                                  'rounded-full p-1.5 transition-colors cursor-pointer hover:bg-accent',
                                  msg.feedback === 'up' ? 'text-primary' : 'text-muted-foreground hover:text-foreground',
                                )}
                                title="Good response"
                                aria-label="Good response"
                              >
                                <ThumbsUp size={11} />
                              </button>
                              <button
                                type="button"
                                onClick={() => onFeedback(msg.id, 'down')}
                                aria-pressed={msg.feedback === 'down'}
                                className={cn(
                                  'rounded-full p-1.5 transition-colors cursor-pointer hover:bg-accent',
                                  msg.feedback === 'down' ? 'text-rose-400' : 'text-muted-foreground hover:text-foreground',
                                )}
                                title="Bad response"
                                aria-label="Bad response"
                              >
                                <ThumbsDown size={11} />
                              </button>
                            </span>
                          )}
                        </>
                      )}
                      {msg.model?.model && (
                        <div
                          className={cn(
                            'ml-auto inline-flex max-w-full items-center gap-1.5 font-mono text-muted-foreground/70',
                            compact ? 'text-[10px]' : 'text-[11px]',
                          )}
                          title={`Answered by ${msg.model.model}${msg.model.provider ? ` · ${msg.model.provider}` : ''}`}
                        >
                          <Cpu size={compact ? 10 : 11} className="shrink-0 text-primary/60" />
                          <span className="truncate">{msg.model.model}</span>
                          {msg.model.provider && (
                            <>
                              <span className="text-muted-foreground/40">·</span>
                              <span className="truncate text-muted-foreground/50">{msg.model.provider}</span>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </>
              ) : editingId === msg.id ? (
                <div className="w-full flex flex-col gap-2">
                  <textarea
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') setEditingId(null);
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) commitEdit();
                    }}
                    autoFocus
                    rows={Math.min(8, Math.max(2, draft.split('\n').length))}
                    className="w-full resize-none rounded-2xl border border-primary/40 bg-card px-4 py-3 text-[14px] md:text-[15px] leading-relaxed text-foreground outline-none focus:ring-1 focus:ring-primary"
                  />
                  <div className="flex items-center justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setEditingId(null)}
                      className="flex items-center gap-1 rounded-full px-3 py-1.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                    >
                      <X size={13} /> Cancel
                    </button>
                    <button
                      type="button"
                      onClick={commitEdit}
                      disabled={!draft.trim()}
                      className="flex items-center gap-1 rounded-full bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40 transition-colors cursor-pointer"
                    >
                      <Check size={13} /> Resend
                    </button>
                  </div>
                </div>
              ) : (
                <div className={cn('group flex flex-col', 'items-end', 'w-full')}>
                  <div className={cn(
                    // `.chat-bubble` paints a scheme-aware fill from
                    // `--chat-bubble-bg-light/-dark` (Settings › ChatBot Page ›
                    // Chat background › Prompt bubble), defaulting to a neutral
                    // tint that reads on both the light and dark chat surfaces.
                    'chat-bubble leading-relaxed break-words whitespace-pre-wrap text-foreground',
                    compact
                      ? 'px-2.5 py-1.5 max-w-[90%] text-[12px] rounded-2xl'
                      // Grow with the text up to the full column width: short
                      // prompts stay a compact pill, long ones use every pixel
                      // instead of wrapping early inside a fixed-size bubble.
                      : 'w-fit max-w-full px-3.5 py-2 md:px-4 md:py-2.5 text-[13px] md:text-[14px] rounded-2xl',
                  )}>
                    {msg.quote && (
                      <QuoteCard quote={msg.quote} onJump={jumpToMessage} compact={compact} />
                    )}
                    {msg.shell && (
                      <ShellRunCard
                        run={msg.shell}
                        copied={copiedShellId === msg.id}
                        onCopy={() => void copyShellOutput(msg.id, msg.shell!)}
                      />
                    )}
                    <div data-quotable-content>
                      {/* A shell turn IS its card: repeating `!cmd` above the
                          run would duplicate the command. */}
                      {msg.shell ? null : msg.content.length > LONG_USER_MESSAGE_CHARS && expandedId !== msg.id
                        ? `${msg.content.slice(0, USER_MESSAGE_PREVIEW_CHARS).trimEnd()}…`
                        : msg.content}
                      {!msg.shell && msg.content.length > LONG_USER_MESSAGE_CHARS && (
                        <button
                          type="button"
                          onClick={() => setExpandedId((id) => (id === msg.id ? null : msg.id))}
                          aria-expanded={expandedId === msg.id}
                          className="mt-1.5 block whitespace-normal text-[11px] font-medium text-primary/80 underline-offset-2 transition-colors hover:text-primary hover:underline"
                        >
                          {expandedId === msg.id
                            ? 'Show less'
                            : `Show full message · ${msg.content.length.toLocaleString()} characters`}
                        </button>
                      )}
                    </div>
                    <MessageAttachments
                      attachments={msg.attachments}
                      onOpen={onOpenAttachment}
                    />
                  </div>
                  <div className="hover-reveal mt-1 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                    <button
                      type="button"
                      onClick={() => void copyMessage(msg)}
                      className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                      title={copiedId === msg.id ? 'Copied' : 'Copy prompt'}
                      aria-label="Copy prompt"
                    >
                      {copiedId === msg.id ? <Check size={11} /> : <Copy size={11} />}
                    </button>
                    {onQuote && (
                      <button
                        type="button"
                        onClick={() => quoteMessage(msg)}
                        className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                        title="Quote this prompt"
                        aria-label="Quote this prompt"
                      >
                        <MessageSquareText size={11} />
                      </button>
                    )}
                    {onForkMessage && (
                      <button
                        type="button"
                        onClick={() => onForkMessage(msg.id)}
                        className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                        title="Fork conversation from here"
                        aria-label="Fork conversation from here"
                      >
                        <GitBranch size={11} />
                      </button>
                    )}
                    {onResendMessage && (
                      <button
                        type="button"
                        onClick={() => onResendMessage(msg.id)}
                        className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                        title="Redirect — re-send this prompt"
                        aria-label="Redirect"
                      >
                        <RotateCcw size={11} />
                      </button>
                    )}
                    {onEditMessage && (
                      <button
                        type="button"
                        onClick={() => startEdit(msg)}
                        className="flex items-center rounded-full p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
                        title="Edit prompt"
                        aria-label="Edit prompt"
                      >
                        <Pencil size={11} />
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
        </div>
      </div>
      </div>

      {/* Custom scroll indicator — a drag-grabbable thumb (the native bar is
          hidden), inset from the edges so it never runs under the header. */}
      {bar && (
        <div
          aria-label="Scroll conversation"
          onPointerDown={onBarPointerDown}
          className={cn(
            'absolute right-0.5 z-20 w-1.5 rounded-full bg-foreground/20 cursor-grab touch-none transition-opacity duration-200 hover:bg-foreground/40',
            barDragging && 'bg-foreground/50 cursor-grabbing',
            barVisible ? 'opacity-100' : 'opacity-0 pointer-events-none',
          )}
          style={{ top: bar.top, height: bar.height }}
        />
      )}

      {/* Jump rail is a desktop affordance (tiny ticks, hover preview); hidden
          on phones where it only overlapped content. */}
      <div className="hidden md:block">
        <MessageRail messages={messages} scrollRef={scrollRef} />
      </div>

      {selectionAction && createPortal(
        <button
          type="button"
          // Keep the selection alive while the button is pressed.
          onPointerDown={(e) => e.preventDefault()}
          onClick={() => {
            if (!onQuote) return;
            onQuote({
              text: selectionAction.text,
              messageId: selectionAction.messageId,
              role: selectionAction.role,
              ...(selectionAction.author ? { author: selectionAction.author } : {}),
            });
            window.getSelection()?.removeAllRanges();
            setSelectionAction(null);
          }}
          style={{
            position: 'fixed',
            left: selectionAction.x,
            top: selectionAction.top,
            transform: selectionAction.below ? 'translate(-50%, 0)' : 'translate(-50%, -100%)',
          }}
          className="quote-chip z-[150] flex select-none items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1.5 text-[11px] font-medium text-foreground shadow-lg transition-colors hover:text-primary cursor-pointer animate-fade-in"
          title="Quote this passage into your next message"
          aria-label="Quote selected text"
        >
          <MessageSquareText size={12} /> Quote
        </button>,
        document.body,
      )}

      {!atBottom && rootRect &&
        createPortal(
          <button
            type="button"
            onClick={() => scrollToBottom(true)}
            style={{
              position: 'fixed',
              left: rootRect.left + rootRect.width / 2,
              // Sit ABOVE the floating composer overlay, not behind it. When the
              // transcript reserves a `bottomInset` for the composer, anchor the
              // pill's bottom to that boundary plus a small gap; otherwise fall
              // back to the compact/inline offsets.
              top: rootRect.bottom - (bottomInset ?? 0) - (compact ? 8 : 16),
              transform: 'translate(-50%, -100%)',
            }}
            className="z-30 flex items-center gap-1.5 rounded-full glass-tooltip px-3 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:text-primary cursor-pointer animate-fade-in"
            title="Scroll to latest"
            aria-label="Scroll to latest"
          >
            <ArrowDown size={13} /> Latest
          </button>,
          document.body,
        )}
    </div>
  );
}
