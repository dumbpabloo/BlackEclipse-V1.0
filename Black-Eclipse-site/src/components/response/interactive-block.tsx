'use client';

// ── InteractiveBlock — a model-authored mini-app, running sandboxed ──────────
//
// The card owns the lifecycle: compose the document, load an optional market
// series through the host, mount the sandboxed frame, and show honest states.
// The widget itself is untrusted; this component never touches its DOM.
//
// The app owns its palette; the shell sets only structural defaults. The host's
// theme/locale/motion travel as `Eclipse.context`, and visibility (off-screen or
// hidden tab) as `Eclipse.visible` so animations can stop.
//
// Surface controls: reload, download the composed HTML, source/copy, full page
// (focus-trapped, Escape to exit), and a drag/keyboard resize handle whose
// height is remembered per app.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import {
  AlertTriangle,
  Check,
  Code,
  Copy,
  Download,
  Link2,
  Maximize2,
  Minimize2,
  RefreshCw,
  Sparkles,
} from '@/lib/icons';
import { cn } from '@/lib/utils';
import { composeInteractiveDocument, type InteractiveContext } from '@/lib/interactive/document';
import {
  loadInteractiveDataset,
  staticDatasetEnvelope,
  type InteractiveDatasetEnvelope,
} from '@/lib/interactive/dataset';
import type { InteractiveBlock as InteractiveBlockData } from '@/lib/response/blocks/interactive';
import { appUrl } from '@/lib/response/share';
import { saveDownloadViaShell } from '@/lib/desktop/download';
import { InteractiveFrame } from './interactive-frame';

const MIN_HEIGHT = 240;
const MAX_HEIGHT = 900;

function clampHeight(value: number | undefined): number {
  if (!Number.isFinite(value)) return 420;
  return Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, Math.round(value as number)));
}

function makeNonce(): string {
  const cryptoApi = globalThis.crypto as Crypto | undefined;
  if (cryptoApi?.randomUUID) return cryptoApi.randomUUID().replace(/-/g, '');
  return `${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

function readHostContext(): InteractiveContext {
  if (typeof document === 'undefined') return { theme: 'dark' };
  const root = document.documentElement;
  return {
    theme: root.classList.contains('dark') ? 'dark' : 'light',
    reducedMotion:
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    locale: typeof navigator !== 'undefined' ? navigator.language : undefined,
  };
}

function readStoredHeight(key: string): number | null {
  try {
    const raw = globalThis.localStorage?.getItem(key);
    if (!raw) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? clampHeight(value) : null;
  } catch {
    return null;
  }
}

function writeStoredHeight(key: string, value: number): void {
  try {
    globalThis.localStorage?.setItem(key, String(value));
  } catch {
    /* storage is optional */
  }
}

function datasetLabel(envelope: InteractiveDatasetEnvelope): string | null {
  if (envelope.status !== 'ready' || envelope.bars.length === 0) return null;
  const asOf = envelope.asOf ? `${envelope.stale ? 'stale · ' : ''}${envelope.asOf}` : null;
  return [envelope.source, envelope.interval, asOf].filter(Boolean).join(' · ');
}

export function InteractiveBlock({
  block,
  onFollowup,
}: {
  block: InteractiveBlockData;
  onFollowup?: (prompt: string) => void;
}) {
  const storageKey = useMemo(() => {
    const seed = block.id || `${block.title ?? ''}:${block.html.length}`;
    return `be:interactive-height:${seed}`;
  }, [block.id, block.title, block.html.length]);

  const [nonce, setNonce] = useState(makeNonce);
  const [reloadKey, setReloadKey] = useState(0);
  const [height, setHeight] = useState(() => readStoredHeight(storageKey) ?? clampHeight(block.height));
  const [full, setFull] = useState(false);
  const [ready, setReady] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [copied, setCopied] = useState(false);
  const [shared, setShared] = useState(false);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [context, setContext] = useState<InteractiveContext>(readHostContext);
  const [intersecting, setIntersecting] = useState(true);
  const [tabVisible, setTabVisible] = useState(true);
  const [envelope, setEnvelope] = useState<InteractiveDatasetEnvelope>(() =>
    block.dataset ? { status: 'loading' } : staticDatasetEnvelope(),
  );

  const sectionRef = useRef<HTMLElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const dragHeightRef = useRef(height);

  // The document is composed with the context at mount; later theme/motion
  // changes are pushed live over postMessage so the app is not remounted.
  const initialContext = useRef(context).current;

  const dataKey = useMemo(() => JSON.stringify(block.data ?? null), [block.data]);
  const datasetKey = block.dataset ? JSON.stringify(block.dataset) : '';

  // Follow the host theme + reduced-motion preference.
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const update = () => setContext(readHostContext());
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    const motion =
      typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-reduced-motion: reduce)')
        : null;
    motion?.addEventListener?.('change', update);
    window.addEventListener('focus', update);
    return () => {
      observer.disconnect();
      motion?.removeEventListener?.('change', update);
      window.removeEventListener('focus', update);
    };
  }, []);

  // Pause the app when the tab is hidden.
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const update = () => setTabVisible(document.visibilityState !== 'hidden');
    update();
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);

  // Pause the app when the card scrolls out of view.
  useEffect(() => {
    if (full || typeof IntersectionObserver === 'undefined') {
      setIntersecting(true);
      return;
    }
    const node = sectionRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      ([entry]) => setIntersecting(entry?.isIntersecting ?? true),
      { threshold: 0.01 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [full]);

  useEffect(() => {
    if (!block.dataset) {
      setEnvelope(staticDatasetEnvelope());
      return;
    }

    const controller = new AbortController();
    setEnvelope({ status: 'loading' });
    loadInteractiveDataset(block.dataset, { signal: controller.signal }).then((next) => {
      if (!controller.signal.aborted) setEnvelope(next);
    });
    return () => controller.abort();
    // `datasetKey` is the stable serialization of `block.dataset`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetKey]);

  const doc = useMemo(
    () =>
      composeInteractiveDocument({
        html: block.html,
        css: block.css,
        js: block.js,
        data: block.data,
        context: initialContext,
        nonce,
      }),
    // `dataKey` is the stable serialization of `block.data`; `initialContext`
    // is captured once on purpose (live updates use postMessage).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [block.html, block.css, block.js, dataKey, nonce],
  );

  const onResize = useCallback((next: number) => {
    setHeight((current) => (Math.abs(current - next) > 2 ? next : current));
  }, []);

  const reload = useCallback(() => {
    setNonce(makeNonce());
    setReloadKey((key) => key + 1);
    setReady(false);
    setRuntimeError(null);
    setHeight(readStoredHeight(storageKey) ?? clampHeight(block.height));
    if (block.dataset) {
      setEnvelope({ status: 'loading' });
      loadInteractiveDataset(block.dataset).then(setEnvelope);
    }
  }, [block.dataset, block.height, storageKey]);

  // A fresh frame (reload / full-page toggle) starts hidden until it is ready.
  useEffect(() => {
    setReady(false);
  }, [nonce, reloadKey, full]);

  // Full page: lock the page scroll, trap Tab, restore focus/scroll on close.
  useEffect(() => {
    if (!full || typeof document === 'undefined') return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    const scrollY = window.scrollY;
    document.body.style.overflow = 'hidden';
    const dialog = dialogRef.current;
    dialog?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setFull(false);
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute('disabled'));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = previousOverflow;
      window.scrollTo(0, scrollY);
      previouslyFocused?.focus?.();
    };
  }, [full]);

  const beginResize = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startY = event.clientY;
      const startHeight = height;
      dragHeightRef.current = startHeight;
      const move = (moveEvent: PointerEvent) => {
        const next = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, Math.round(startHeight + moveEvent.clientY - startY)));
        dragHeightRef.current = next;
        setHeight(next);
      };
      const end = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', end);
        window.removeEventListener('pointercancel', end);
        writeStoredHeight(storageKey, dragHeightRef.current);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', end);
      window.addEventListener('pointercancel', end);
    },
    [height, storageKey],
  );

  const onHandleKey = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
      event.preventDefault();
      const next = Math.max(
        MIN_HEIGHT,
        Math.min(MAX_HEIGHT, height + (event.key === 'ArrowDown' ? 24 : -24)),
      );
      setHeight(next);
      writeStoredHeight(storageKey, next);
    },
    [height, storageKey],
  );

  const resetHeight = useCallback(() => {
    const next = clampHeight(block.height);
    setHeight(next);
    try {
      globalThis.localStorage?.removeItem(storageKey);
    } catch {
      /* storage is optional */
    }
  }, [block.height, storageKey]);

  const share = useCallback(async () => {
    if (typeof window === 'undefined') return;
    try {
      await navigator.clipboard.writeText(appUrl(block, window.location.origin));
      setShared(true);
      setTimeout(() => setShared(false), 1800);
    } catch {
      /* clipboard may be unavailable */
    }
  }, [block]);

  const copySource = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(doc);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard may be unavailable */
    }
  }, [doc]);

  const title = block.title?.trim() || 'Interactive app';
  const provenance = datasetLabel(envelope);
  const loading = envelope.status === 'loading';
  const unavailable = envelope.status === 'unavailable';
  const onScreen = tabVisible && (full || intersecting);

  const fixPrompt = `The interactive app "${title}" hit a runtime error: ${runtimeError ?? ''}. Re-emit a corrected version (one interactive block or one html fence) with the same purpose that works.`;

  const errorBanner = runtimeError ? (
    <div className="flex items-start gap-2 border-b border-rose-400/20 bg-rose-500/5 px-3 py-2 text-[11px] text-rose-200/90">
      <AlertTriangle size={13} className="mt-0.5 shrink-0" />
      <span className="min-w-0 flex-1 break-words">App error — {runtimeError}</span>
      {onFollowup && (
        <button
          type="button"
          onClick={() => onFollowup(fixPrompt)}
          className="shrink-0 rounded-full border border-rose-300/30 bg-rose-400/10 px-2 py-0.5 text-[10px] font-medium text-rose-100 transition-colors hover:bg-rose-400/20"
        >
          Ask Eclipse to fix
        </button>
      )}
    </div>
  ) : null;

  const marketOverlay = (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-background/40 backdrop-blur-[1px]">
      <span className="inline-flex items-center gap-2 rounded-full border border-border/50 bg-background/80 px-3 py-1.5 text-[11px] text-muted-foreground">
        <span className="h-3 w-3 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-primary" />
        Loading market data…
      </span>
    </div>
  );

  const skeleton = (
    <div className="pointer-events-none absolute inset-0 overflow-hidden p-4">
      <div className="flex h-full w-full animate-pulse flex-col gap-3">
        <div className="h-4 w-1/3 rounded-md bg-accent/30" />
        <div className="min-h-0 flex-1 rounded-lg bg-accent/15" />
      </div>
    </div>
  );

  const badges = (
    <>
      {block.provenance === 'illustrative' && (
        <span className="shrink-0 rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[10px] text-amber-300/90">
          Illustrative
        </span>
      )}
      {provenance && (
        <span className="hidden shrink-0 truncate font-mono text-[10px] text-muted-foreground/60 sm:inline" title={provenance}>
          {provenance}
        </span>
      )}
    </>
  );

  const iconButton =
    'shrink-0 inline-flex items-center gap-1 rounded-full border border-border/50 bg-accent/30 px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40';

  // The composed document is already self-contained, so the user can keep it.
  const download = useCallback(() => {
    if (typeof document === 'undefined') return;
    const blob = new Blob([doc], { type: 'text/html;charset=utf-8' });
    const filename = `${title.replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'interactive-app'}.html`;
    if (saveDownloadViaShell(filename, blob)) return;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }, [doc, title]);

  // Full page: a viewport-filling overlay. It remounts the app (a fresh run of
  // the same document) rather than trying to move a live iframe between trees.
  if (full && typeof document !== 'undefined') {
    return createPortal(
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="fixed inset-0 z-[120] flex flex-col bg-background/95 backdrop-blur-xl focus:outline-none"
        role="dialog"
        aria-modal="true"
        aria-label={`${title} (full page)`}
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2 sm:px-4">
          <Sparkles size={14} className="shrink-0 text-primary/80" />
          <h4 className="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight text-foreground" title={title}>
            {title}
          </h4>
          {badges}
          <button type="button" onClick={reload} title="Reload the app" aria-label="Reload the app" className={iconButton}>
            <RefreshCw size={12} />
            <span className="hidden sm:inline">Reload</span>
          </button>
          <button type="button" onClick={download} title="Download the app as HTML" aria-label="Download the app as HTML" className={iconButton}>
            <Download size={12} />
            <span className="hidden sm:inline">Download</span>
          </button>
          <button type="button" onClick={share} title="Copy a share link" aria-label="Copy a share link" className={iconButton}>
            <Link2 size={12} />
            <span className="hidden sm:inline">{shared ? 'Copied' : 'Share'}</span>
          </button>
          <button type="button" onClick={() => setFull(false)} title="Exit full page" aria-label="Exit full page" className={iconButton}>
            <Minimize2 size={12} />
            <span className="hidden sm:inline">Exit</span>
          </button>
        </header>

        {unavailable && (
          <div className="flex shrink-0 items-start gap-2 border-b border-amber-400/20 bg-amber-400/5 px-3 py-2 text-[11px] text-amber-200/90">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <span>Data unavailable — {envelope.reason}</span>
          </div>
        )}

        {errorBanner}

        <div className="relative min-h-0 flex-1">
          <InteractiveFrame
            key={`full-${reloadKey}`}
            srcDoc={doc}
            title={title}
            height="100%"
            nonce={nonce}
            envelope={envelope}
            context={context}
            onScreen={onScreen}
            visible={ready}
            onResize={onResize}
            onReady={() => setReady(true)}
            onError={setRuntimeError}
          />
          {loading ? marketOverlay : !ready ? skeleton : null}
        </div>

        {block.caption && (
          <p className="shrink-0 border-t border-border/30 px-3 py-1.5 text-[10px] text-muted-foreground/60">
            {block.caption}
          </p>
        )}
      </div>,
      document.body,
    );
  }

  return (
    <section
      ref={sectionRef}
      className="glass-card-subtle w-full max-w-none overflow-hidden rounded-xl border border-border/40"
      aria-label={title}
    >
      <header className="flex items-center gap-2 border-b border-border/40 px-3 py-2">
        <Sparkles size={13} className="shrink-0 text-primary/80" />
        <h4 className="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight text-foreground" title={title}>
          {title}
        </h4>
        {badges}
        <button type="button" onClick={reload} title="Reload the app" aria-label="Reload the app" className={iconButton}>
          <RefreshCw size={12} />
        </button>
        <button
          type="button"
          onClick={() => setShowSource((value) => !value)}
          title={showSource ? 'Hide source' : 'View source'}
          aria-label={showSource ? 'Hide source' : 'View source'}
          aria-pressed={showSource}
          className={iconButton}
        >
          <Code size={12} />
        </button>
        <button type="button" onClick={download} title="Download the app as HTML" aria-label="Download the app as HTML" className={iconButton}>
          <Download size={12} />
        </button>
        <button type="button" onClick={share} title="Copy a share link" aria-label="Copy a share link" className={iconButton}>
          <Link2 size={12} />
          <span className="hidden sm:inline">{shared ? 'Copied' : 'Share'}</span>
        </button>
        <button
          type="button"
          onClick={() => setFull(true)}
          title="Open full page"
          aria-label="Open full page"
          className={iconButton}
        >
          <Maximize2 size={12} />
          <span className="hidden sm:inline">Full page</span>
        </button>
      </header>

      {block.description && (
        <p className="border-b border-border/30 px-3 py-2 text-[12px] text-muted-foreground">{block.description}</p>
      )}

      {unavailable && (
        <div className="flex items-start gap-2 border-b border-amber-400/20 bg-amber-400/5 px-3 py-2 text-[11px] text-amber-200/90">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>Data unavailable — {envelope.reason}</span>
        </div>
      )}

      {errorBanner}

      <div className="relative w-full" style={{ height }}>
        <InteractiveFrame
          key={reloadKey}
          srcDoc={doc}
          title={title}
          height={height}
          nonce={nonce}
          envelope={envelope}
          context={context}
          onScreen={onScreen}
          visible={ready}
          onResize={onResize}
          onReady={() => setReady(true)}
          onError={setRuntimeError}
        />
        {loading ? marketOverlay : !ready ? skeleton : null}
      </div>

      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize app (drag, arrow keys; double-click to reset)"
        tabIndex={0}
        onPointerDown={beginResize}
        onKeyDown={onHandleKey}
        onDoubleClick={resetHeight}
        className="group/resize flex h-3 w-full cursor-ns-resize touch-none items-center justify-center border-t border-border/20 transition-colors hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
      >
        <span className="h-0.5 w-10 rounded-full bg-border transition-colors group-hover/resize:bg-primary/60" />
      </div>

      {showSource && (
        <div className="border-t border-border/40">
          <div className="flex items-center justify-between gap-2 px-3 py-1.5">
            <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground/60">Source</span>
            <button type="button" onClick={copySource} className={iconButton} aria-label="Copy source">
              {copied ? <Check size={12} /> : <Copy size={12} />}
              <span>{copied ? 'Copied' : 'Copy'}</span>
            </button>
          </div>
          <pre className="max-h-72 overflow-auto border-t border-border/30 bg-black/20 p-3 font-mono text-[11px] leading-relaxed text-foreground/80">
            <code>{doc}</code>
          </pre>
        </div>
      )}

      {block.caption && (
        <p className={cn('border-t border-border/30 px-3 py-1.5 text-[10px] text-muted-foreground/60')}>
          {block.caption}
        </p>
      )}
    </section>
  );
}
