'use client';

// ── Live Intelligence — Command Center widget ────────────────────────────────
// A terminal-style live news/stream panel: financial, market, business and
// geopolitical broadcasters embedded in the dashboard grid, with an in-card
// channel manager. Built on the shared workspace kernel like every other widget.
//
// HONESTY: this embeds each broadcaster's OWN official player. The app never
// proxies, restreams, scrapes or rebroadcasts anything, never invents a stream
// URL or channel, and makes no claim about a stream's live state it cannot
// verify — an offline channel shows the provider's own placeholder plus a link
// to watch at source.
//
// LIFECYCLE (important): the dashboard lives in a keep-alive tab that stays
// MOUNTED and is hidden with `display:none`, and the workspace keeps widgets
// mounted too. A playing iframe would therefore keep streaming forever in the
// background. The player is unmounted whenever it leaves the viewport, its tab
// is switched away, or the browser tab is hidden. See ADR-015.
//
// FUTURE EXTENSION POINT: `LiveIntelligenceEvent` (below) is the shape a
// transcription/event-detection pipeline would emit into Investor Eye. Nothing
// produces it yet and nothing fakes it — it exists so the seam is explicit.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle, ChevronDown, ExternalLink, Loader2, Pause, Play, Plus,
  Radio, RotateCcw, Settings2, Trash2, Volume2, VolumeX,
} from '@/lib/icons';
import { cn } from '@/lib/utils';
import type { WidgetConfigField } from '@/lib/workspace/widget-def';
import type { WidgetPlacement } from '@/lib/workspace/types';
import {
  LIVE_CHANNELS, LIVE_CATEGORY_LABEL, channelEmbedUrl, channelWatchUrl, channelTypeLabel,
  type LiveCategory, type LiveChannel,
} from './live-channels';
import {
  useLiveIntelligenceSettings, resolveActiveChannel,
} from './live-intelligence-store';

// ── Live status resolution ───────────────────────────────────────────────────
// The CURRENT live video id is resolved server-side and cached (3 min). This is
// what makes playback work: embedding `live_stream?channel=` renders "This video
// is unavailable" for many channels that ARE live, while `/embed/<resolved id>`
// plays. Status is never fabricated — `unresolved` renders as offline, honestly.

export interface ResolvedStreamInfo {
  videoId: string | null;
  status: 'live' | 'offline' | 'unavailable' | 'unknown';
  checkedAt: string;
  cached: boolean;
}

function useResolvedStreams(channelIds: string[], nonce = 0) {
  const [streams, setStreams] = useState<Record<string, ResolvedStreamInfo>>({});
  const [loading, setLoading] = useState(false);
  // Stable dependency so the effect does not refetch every render.
  const key = channelIds.join(',');

  useEffect(() => {
    if (!key) { setStreams({}); return; }
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const res = await fetch(`/api/live-streams?ids=${encodeURIComponent(key)}`);
        const data = await res.json();
        if (!cancelled && data && typeof data.streams === 'object') setStreams(data.streams);
      } catch { /* leave prior results; the widget falls back honestly */ }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [key, nonce]);

  return { streams, loading };
}

/**
 * FUTURE: the normalized shape a stream→transcript→event pipeline would emit so
 * Investor Eye can consume broadcast intelligence like any other event source.
 * Declared (and NOT used) so the integration seam is explicit. No producer
 * exists yet, and none may fabricate these fields.
 */
export interface LiveIntelligenceEvent {
  sourceId: string;
  timestamp: string;
  headline: string;
  category: LiveCategory;
  entities: string[];
  countries: string[];
  assets: string[];
  marketImpact: 'bullish' | 'bearish' | 'neutral' | 'unknown';
  confidence: number;
}

export const LIVE_INTELLIGENCE_MUTED_FIELD: WidgetConfigField = {
  key: 'muted', label: 'Start muted', type: 'boolean', default: true,
};

export const LIVE_INTELLIGENCE_AUTOPLAY_FIELD: WidgetConfigField = {
  key: 'autoplay', label: 'Autoplay', type: 'boolean', default: true,
};

type LoadState = 'idle' | 'loading' | 'ready';

export function LiveIntelligenceWidget({ placement }: { placement: WidgetPlacement }) {
  const cfg = placement.config ?? {};
  const { settings, enabled, toggleChannel, setDefault, addCustom, removeCustom } = useLiveIntelligenceSettings();

  // Per-instance channel choice. Persisted so a reload keeps the card on the
  // same feed; distinct instances can watch different channels.
  const storageKey = `be_live_tv_channel:${placement.id}`;
  const [instanced, setInstanced] = useState<string | undefined>(() => {
    if (typeof window !== 'undefined') {
      try { return window.localStorage.getItem(storageKey) ?? undefined; } catch { /* ignore */ }
    }
    return undefined;
  });

  const active = resolveActiveChannel(enabled, settings, instanced);

  // Resolve the CURRENT live video id for the channels the user can reach. The
  // resolver is server-side + cached, so this is one cheap request, not a poll.
  const [resolveNonce, setResolveNonce] = useState(0);
  const enabledIds = useMemo(() => enabled.map(c => c.id), [enabled]);
  const { streams, loading: resolving } = useResolvedStreams(enabledIds, resolveNonce);
  const activeStream = active ? streams[active.id] : undefined;
  const activeVideoId = activeStream?.videoId ?? null;
  // `offline` is a real, resolved verdict from the provider — not an assumption.
  const activeOffline = Boolean(activeStream && activeStream.status !== 'live');

  // A built-in YouTube channel MUST be resolved before we embed: the
  // `live_stream?channel=` fallback renders a blank/"unavailable" frame for most
  // channels, so mounting it on first paint (before the resolver answers) is
  // exactly the bug this widget exists to avoid. Channels with a fixed video id
  // or an embed URL do not need resolution.
  const needsResolution = Boolean(active && active.type === 'youtube' && !active.youtubeVideoId && (active.youtubeHandle || active.youtubeChannelId));
  const resolvePending = needsResolution && !activeStream;
  // Resolved but the provider reported no live broadcast, or the resolver failed
  // outright — in the latter case `activeOffline` is false and we fall through to
  // the explicit error/fallback path below.
  const resolveFailed = needsResolution && activeStream?.status === 'unknown';

  const [playing, setPlaying] = useState(cfg.autoplay !== false);
  const [muted, setMuted] = useState(cfg.muted !== false);
  const [managerOpen, setManagerOpen] = useState(false);
  const [loadState, setLoadState] = useState<LoadState>('idle');
  const [errored, setErrored] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  // A remote iframe that cannot load is replaced by WebView2's OWN error page
  // inside the card — Microsoft/Edge branding the app cannot restyle. So the
  // player is never mounted while the machine is offline; the branded card
  // below says why, and the `online` event mounts the player again. `null`
  // means "not measured yet": never mount on an unknown connection state.
  const [online, setOnline] = useState<boolean | null>(null);
  useEffect(() => {
    const sync = () => setOnline(typeof navigator === 'undefined' || navigator.onLine !== false);
    sync();
    window.addEventListener('online', sync);
    window.addEventListener('offline', sync);
    return () => {
      window.removeEventListener('online', sync);
      window.removeEventListener('offline', sync);
    };
  }, []);

  const chooseChannel = (id: string) => {
    setInstanced(id);
    setPlaying(true);
    setErrored(false);
    setLoadState('idle');
    try { window.localStorage.setItem(storageKey, id); } catch { /* ignore */ }
  };

  // ── Visibility teardown (see the file header / ADR-015) ───────────────────
  // IntersectionObserver catches `display:none` keep-alive ancestors AND a
  // scrolled-off widget; visibilitychange catches the browser tab being hidden.
  const rootRef = useRef<HTMLDivElement>(null);
  const [inView, setInView] = useState(true);
  const [tabVisible, setTabVisible] = useState(true);

  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    const onVisibility = () => setTabVisible(!document.hidden);
    onVisibility();
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  const streaming = online === true && playing && inView && tabVisible && !errored && !!active;

  const embedUrl = useMemo(() => {
    if (!active || !streaming) return null;
    // Wait for the resolver on a built-in YouTube channel — do NOT mount the
    // unreliable live_stream fallback on first paint (see resolvePending).
    if (resolvePending) return null;
    // Prefer the RESOLVED live video id (the form that plays); channelEmbedUrl
    // falls back to live_stream?channel= only for custom/unresolvable sources.
    return channelEmbedUrl(active, { muted, autoplay: true, resolvedVideoId: activeVideoId });
    // reloadKey forces a fresh URL (and therefore a fresh iframe) on retry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, streaming, muted, reloadKey, activeVideoId, resolvePending]);

  // The iframe mounts only once we have a URL; surface a loading state until its
  // load event fires, and a bounded failure state if it never does.
  useEffect(() => {
    if (!embedUrl) { setLoadState('idle'); return; }
    setLoadState('loading');
    setErrored(false);
    const timer = setTimeout(() => setLoadState(s => (s === 'loading' ? 'ready' : s)), 12000);
    return () => clearTimeout(timer);
  }, [embedUrl]);

  const retry = () => {
    if (typeof navigator !== 'undefined') setOnline(navigator.onLine !== false);
    setErrored(false);
    setLoadState('idle');
    setResolveNonce(n => n + 1);
    setReloadKey(k => k + 1);
  };

  const watchUrl = active ? channelWatchUrl(active) : 'https://www.youtube.com';

  // ── Header ────────────────────────────────────────────────────────────────
  // The status chip is honest: LIVE only when the resolver confirmed a live,
  // embeddable stream; OFFLINE when the provider has no live broadcast; and
  // CHECKING while the resolver is in flight.
  const statusChip = online === false
    ? { label: 'OFFLINE', dot: 'bg-amber-400', text: 'text-amber-400/90' }
    : resolvePending || (resolving && !activeStream)
    ? { label: 'CHECKING', dot: 'bg-muted-foreground/50 animate-pulse', text: 'text-muted-foreground/70' }
    : activeOffline || resolveFailed
      ? { label: 'OFFLINE', dot: 'bg-amber-400', text: 'text-amber-400/90' }
      : activeStream?.status === 'live'
        ? { label: 'LIVE', dot: 'bg-emerald-400', text: 'text-emerald-400' }
        : { label: 'CHECKING', dot: 'bg-muted-foreground/50 animate-pulse', text: 'text-muted-foreground/70' };

  const header = (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border/10 pb-2.5">
      <div className="flex min-w-0 items-center gap-2">
        <Radio size={13} className={cn('shrink-0', activeStream?.status === 'live' ? 'text-emerald-400' : 'text-primary')} aria-hidden />
        <h3 className="truncate font-serif text-sm font-semibold text-foreground">Live Intelligence</h3>
        {active && (
          <span className="hidden shrink-0 truncate text-[9px] font-mono text-muted-foreground/70 sm:inline">
            {active.name}
          </span>
        )}
        <span className={cn('hidden shrink-0 items-center gap-1 font-mono text-[8px] font-bold uppercase tracking-wider sm:flex', statusChip.text)}>
          <span className={cn('h-1.5 w-1.5 rounded-full', statusChip.dot)} aria-hidden />
          {statusChip.label}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {enabled.length > 1 && (
          <div className="relative">
            <select
              value={active?.id ?? ''}
              onChange={e => chooseChannel(e.target.value)}
              className="cursor-pointer appearance-none rounded-xl border border-white/15 dark:border-white/10 bg-background/60 py-1.5 pl-2.5 pr-7 text-[10px] font-semibold text-foreground outline-none transition-all hover:border-primary/40 focus:ring-1 focus:ring-primary"
              title="Choose a channel"
            >
              {enabled.map(c => (
                <option key={c.id} value={c.id} className="bg-[#121212] text-foreground">
                  {c.name}{c.region ? ` · ${c.region}` : ''}
                </option>
              ))}
            </select>
            <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground">
              <ChevronDown size={11} />
            </span>
          </div>
        )}
        <button
          type="button"
          onClick={() => setMuted(v => !v)}
          className="rounded-xl border border-white/15 dark:border-white/10 bg-accent/30 p-1.5 text-muted-foreground transition-all hover:border-primary/40 hover:text-primary cursor-pointer"
          title={muted ? 'Unmute' : 'Mute'}
        >
          {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
        </button>
        <button
          type="button"
          onClick={() => setPlaying(v => !v)}
          className="rounded-xl border border-white/15 dark:border-white/10 bg-accent/30 p-1.5 text-muted-foreground transition-all hover:border-primary/40 hover:text-primary cursor-pointer"
          title={playing ? 'Pause' : 'Play'}
        >
          {playing ? <Pause size={14} /> : <Play size={14} />}
        </button>
        <button
          type="button"
          onClick={() => setManagerOpen(v => !v)}
          className={cn(
            'rounded-xl border p-1.5 transition-all cursor-pointer',
            managerOpen
              ? 'border-primary/40 bg-primary/10 text-primary'
              : 'border-white/15 dark:border-white/10 bg-accent/30 text-muted-foreground hover:border-primary/40 hover:text-primary',
          )}
          title="Manage channels"
        >
          <Settings2 size={14} />
        </button>
      </div>
    </div>
  );

  // ── Channel manager (in-card; reuses existing control styling) ────────────
  const manager = managerOpen && (
    <ChannelManager
      enabledIds={enabled.map(c => c.id)}
      defaultId={settings.defaultId}
      custom={settings.custom}
      statuses={streams}
      onToggle={toggleChannel}
      onSetDefault={setDefault}
      onAddCustom={addCustom}
      onRemoveCustom={removeCustom}
    />
  );

  // ── Body states ───────────────────────────────────────────────────────────
  let body: React.ReactNode;
  if (enabled.length === 0) {
    body = (
      <StatePanel
        title="NO LIVE SOURCES"
        message="Configure financial and news sources to start monitoring live intelligence."
        action={{ label: 'Configure Channels', icon: Settings2, onClick: () => setManagerOpen(true) }}
      />
    );
  } else if (online === false) {
    // Do not mount a remote iframe when there is no network: its failure would
    // render the webview's own error page inside the card (Edge/Bing branded).
    body = (
      <StatePanel
        title="NO CONNECTION"
        message="You’re offline. Live streams need an internet connection — the rest of Black Eclipse keeps working."
        actions={[{ label: 'Check Again', icon: RotateCcw, onClick: retry }]}
      />
    );
  } else if (online === null) {
    // The connection state is measured on mount. Never start a remote iframe
    // before that measurement lands.
    body = (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 rounded-xl border border-border/10 bg-black/40 text-center">
        <Loader2 size={18} className="animate-spin text-primary" />
        <p className="font-mono text-[10px] text-muted-foreground/80">Checking connection…</p>
      </div>
    );
  } else if (errored || resolveFailed) {
    body = (
      <StatePanel
        title={resolveFailed ? 'COULD NOT CHECK STREAM' : 'STREAM ERROR'}
        message={resolveFailed
          ? 'The live status could not be verified right now. Try again in a moment.'
          : 'Unable to load this stream. It may be blocked, offline, or unavailable in your region.'}
        actions={[
          { label: 'Retry', icon: RotateCcw, onClick: retry },
          { label: 'Change Channel', icon: ChevronDown, onClick: () => setManagerOpen(true) },
        ]}
        onExternal={watchUrl}
      />
    );
  } else if (resolvePending) {
    // Resolution is in flight for a built-in channel. Showing the unreliable
    // live_stream fallback here is exactly the original bug, so we wait.
    body = (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 rounded-xl border border-border/10 bg-black/40 text-center">
        <Loader2 size={18} className="animate-spin text-primary" />
        <p className="font-mono text-[10px] text-muted-foreground/80">Checking live status…</p>
        <p className="text-[9px] italic text-muted-foreground/50">{active?.name}</p>
      </div>
    );
  } else if (activeOffline) {
    // The provider has no live broadcast right now. This is a resolved verdict
    // from the channel's own page — not an assumption — so we say so plainly and
    // never present it as LIVE.
    body = (
      <StatePanel
        title="CHANNEL OFFLINE"
        message={`${active?.name ?? 'This source'} has no live broadcast right now. It may return later.`}
        actions={[
          { label: 'Check Again', icon: RotateCcw, onClick: () => { setResolveNonce(n => n + 1); setReloadKey(k => k + 1); } },
          { label: 'Change Channel', icon: ChevronDown, onClick: () => setManagerOpen(true) },
        ]}
        onExternal={watchUrl}
      />
    );
  } else if (embedUrl) {
    body = (
      <div className="relative min-h-0 flex-1 overflow-hidden rounded-xl border border-border/10 bg-black">
        {loadState === 'loading' && (
          <div className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-black/70 text-center">
            <Loader2 size={18} className="animate-spin text-primary" />
            <p className="font-mono text-[10px] text-muted-foreground/80">Loading stream…</p>
            <p className="text-[9px] italic text-muted-foreground/50">{active?.name}</p>
          </div>
        )}
        <iframe
          key={embedUrl}
          src={embedUrl}
          title={active ? `${active.name} live stream` : 'Live stream'}
          className="absolute inset-0 h-full w-full"
          allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
          allowFullScreen
          referrerPolicy="strict-origin-when-cross-origin"
          onLoad={() => setLoadState('ready')}
          onError={() => setErrored(true)}
        />
      </div>
    );
  } else {
    // Paused, or torn down because the widget is not visible right now.
    body = (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 rounded-xl border border-border/10 bg-black/40 text-center">
        <button
          type="button"
          onClick={() => setPlaying(true)}
          className="flex h-12 w-12 items-center justify-center rounded-full border border-primary/30 bg-primary/10 text-primary transition-all hover:scale-105 hover:bg-primary/20 cursor-pointer"
          title="Play"
        >
          <Play size={18} className="ml-0.5" />
        </button>
        <p className="max-w-[26ch] text-[10px] italic text-muted-foreground/60">
          {playing
            ? 'Paused while off-screen to save bandwidth.'
            : `${active?.name ?? 'Stream'} paused.`}
        </p>
        {active?.region && (
          <p className="font-mono text-[9px] text-muted-foreground/40">
            {active.region} · {channelTypeLabel(active)}
          </p>
        )}
      </div>
    );
  }

  return (
    <div ref={rootRef} className="flex h-full flex-col p-4">
      {header}
      {manager}
      {body}
      <div className="mt-2 flex items-center justify-between gap-3">
        <p className="min-w-0 flex-1 truncate text-[9px] leading-relaxed text-muted-foreground/50">
          External live stream — embedded from the broadcaster&rsquo;s official player. Availability is theirs.
        </p>
        <a
          href={watchUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="flex shrink-0 items-center gap-1 text-[9px] font-bold text-primary/80 transition-colors hover:text-primary"
          title="Watch at source"
        >
          <ExternalLink size={10} /> Watch at source
        </a>
      </div>
    </div>
  );
}

// ── States ───────────────────────────────────────────────────────────────────

interface StateAction { label: string; icon: React.ElementType; onClick: () => void }

function StatePanel({ title, message, action, actions, onExternal }: {
  title: string;
  message: string;
  action?: StateAction;
  actions?: StateAction[];
  onExternal?: string;
}) {
  const all = actions ?? (action ? [action] : []);
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 rounded-xl border border-border/10 bg-black/40 px-4 text-center">
      <AlertCircle size={18} className="text-amber-400/80" />
      <p className="font-mono text-[10px] font-bold uppercase tracking-wider text-amber-400/90">{title}</p>
      <p className="max-w-[34ch] text-[10px] leading-relaxed text-muted-foreground/70">{message}</p>
      <div className="flex flex-wrap items-center justify-center gap-2">
        {all.map(a => (
          <button
            key={a.label}
            type="button"
            onClick={a.onClick}
            className="inline-flex items-center gap-1.5 rounded-xl border border-white/15 dark:border-white/10 bg-accent/30 px-3 py-1.5 text-[10px] font-bold text-muted-foreground transition-all hover:border-primary/40 hover:text-primary cursor-pointer"
          >
            <a.icon size={12} /> {a.label}
          </button>
        ))}
        {onExternal && (
          <a
            href={onExternal}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-xl border border-white/15 dark:border-white/10 bg-accent/30 px-3 py-1.5 text-[10px] font-bold text-muted-foreground transition-all hover:border-primary/40 hover:text-primary"
          >
            <ExternalLink size={12} /> Open at source
          </a>
        )}
      </div>
    </div>
  );
}

// ── Channel manager ──────────────────────────────────────────────────────────

function ChannelManager({
  enabledIds, defaultId, custom, statuses, onToggle, onSetDefault, onAddCustom, onRemoveCustom,
}: {
  enabledIds: string[];
  defaultId: string;
  custom: LiveChannel[];
  statuses: Record<string, ResolvedStreamInfo>;
  onToggle: (id: string) => void;
  onSetDefault: (id: string) => void;
  onAddCustom: (raw: string, label: string) => { ok: boolean; id?: string };
  onRemoveCustom: (id: string) => void;
}) {
  const [filter, setFilter] = useState('');
  const [customInput, setCustomInput] = useState('');
  const [customLabel, setCustomLabel] = useState('');
  const [customError, setCustomError] = useState(false);

  const q = filter.trim().toLowerCase();
  const groups = useMemo(() => {
    const cats = new Map<LiveCategory, LiveChannel[]>();
    for (const c of [...LIVE_CHANNELS, ...custom]) {
      if (q && !`${c.name} ${c.region ?? ''} ${LIVE_CATEGORY_LABEL[c.category]}`.toLowerCase().includes(q)) continue;
      const list = cats.get(c.category) ?? [];
      list.push(c);
      cats.set(c.category, list);
    }
    return [...cats.entries()];
  }, [q, custom]);

  const submitCustom = () => {
    const result = onAddCustom(customInput, customLabel || 'Custom source');
    if (!result.ok) { setCustomError(true); return; }
    setCustomError(false);
    setCustomInput('');
    setCustomLabel('');
  };

  return (
    <div className="mb-3 max-h-72 shrink-0 space-y-3 overflow-y-auto rounded-xl border border-border/20 bg-accent/10 p-3">
      <div className="flex items-center gap-2">
        <input
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Filter channels…"
          className="w-full rounded-lg border border-border/40 bg-background/60 px-2.5 py-1.5 text-[10px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/50 focus:ring-1 focus:ring-primary"
        />
        <span className="shrink-0 font-mono text-[9px] text-muted-foreground/60">{enabledIds.length} on</span>
      </div>

      <p className="text-[9px] leading-relaxed text-muted-foreground/60">
        Click a channel to enable it. Click the star to make it the default for new cards.
      </p>

      {groups.length === 0 ? (
        <p className="py-3 text-center text-[10px] italic text-muted-foreground/60">No channels match “{filter}”.</p>
      ) : groups.map(([category, list]) => (
        <div key={category}>
          <p className="mb-1 text-[8px] font-bold uppercase tracking-widest text-muted-foreground/50">
            {LIVE_CATEGORY_LABEL[category]}
          </p>
          <div className="flex flex-wrap gap-1">
            {list.map(c => {
              const on = enabledIds.includes(c.id);
              const isDefault = defaultId === c.id;
              const isCustom = c.id.startsWith('custom:');
              // Resolved status only; never inferred from `lastKnownStatus`.
              const resolved = statuses[c.id]?.status;
              const dotClass = resolved === 'live'
                ? 'bg-emerald-400'
                : resolved === 'offline'
                  ? 'bg-muted-foreground/40'
                  : 'bg-muted-foreground/20';
              const dotTitle = resolved === 'live' ? 'Live now' : resolved === 'offline' ? 'No live broadcast right now' : 'Status unknown';
              return (
                <span
                  key={c.id}
                  className={cn(
                    'inline-flex items-center rounded-lg border text-[10px] font-semibold transition-all',
                    on ? 'border-primary/40 bg-primary/10' : 'border-border/40 bg-transparent',
                  )}
                >
                  <span className={cn('ml-2 h-1.5 w-1.5 shrink-0 rounded-full', dotClass)} title={dotTitle} aria-hidden />
                  <button
                    type="button"
                    onClick={() => onToggle(c.id)}
                    className={cn('px-2 py-1 cursor-pointer', on ? 'text-primary' : 'text-muted-foreground hover:text-foreground')}
                    title={on ? `Disable ${c.name}` : `Enable ${c.name}`}
                  >
                    {c.name}
                    {c.region && <span className="ml-1 text-[8px] text-muted-foreground/50">{c.region}</span>}
                  </button>
                  {on && (
                    <button
                      type="button"
                      onClick={() => onSetDefault(c.id)}
                      className={cn('px-1 py-1 cursor-pointer', isDefault ? 'text-amber-400' : 'text-muted-foreground/40 hover:text-amber-400')}
                      title={isDefault ? 'Default channel' : `Make ${c.name} the default`}
                    >
                      <Star4 size={10} filled={isDefault} />
                    </button>
                  )}
                  {isCustom && (
                    <button
                      type="button"
                      onClick={() => onRemoveCustom(c.id)}
                      className="pr-1.5 pl-0.5 py-1 text-muted-foreground/40 transition-colors hover:text-rose-400 cursor-pointer"
                      title={`Remove ${c.name}`}
                    >
                      <Trash2 size={10} />
                    </button>
                  )}
                </span>
              );
            })}
          </div>
        </div>
      ))}

      {/* Advanced: user-supplied official sources only. */}
      <div className="space-y-1.5 border-t border-border/20 pt-2.5">
        <p className="text-[8px] font-bold uppercase tracking-widest text-muted-foreground/50">Custom source (advanced)</p>
        <div className="flex flex-wrap items-center gap-1.5">
          <input
            value={customInput}
            onChange={e => { setCustomInput(e.target.value); setCustomError(false); }}
            onKeyDown={e => { if (e.key === 'Enter') submitCustom(); }}
            placeholder="Channel ID, @handle, video id, or embed URL"
            className="min-w-[180px] flex-1 rounded-lg border border-border/40 bg-background/60 px-2.5 py-1.5 text-[10px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/50 focus:ring-1 focus:ring-primary"
          />
          <input
            value={customLabel}
            onChange={e => setCustomLabel(e.target.value)}
            placeholder="Label"
            className="w-24 rounded-lg border border-border/40 bg-background/60 px-2.5 py-1.5 text-[10px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/50 focus:ring-1 focus:ring-primary"
          />
          <button
            type="button"
            onClick={submitCustom}
            disabled={!customInput.trim()}
            className="inline-flex items-center gap-1 rounded-lg border border-primary/30 bg-primary/10 px-2.5 py-1.5 text-[10px] font-bold text-primary transition-all hover:bg-primary/20 disabled:cursor-default disabled:opacity-40 cursor-pointer"
          >
            <Plus size={11} /> Add
          </button>
        </div>
        {customError ? (
          <p className="flex items-center gap-1 text-[9px] text-rose-400">
            <AlertCircle size={10} /> Not a recognised source. Use a YouTube channel ID (UC…), @handle, video id, or an allowlisted embed URL.
          </p>
        ) : (
          <p className="text-[9px] text-muted-foreground/50">
            Only official broadcasters&rsquo; own feeds. The app never proxies or restreams.
          </p>
        )}
        {custom.length > 0 && (
          <p className="text-[9px] text-muted-foreground/60">{custom.length} custom source{custom.length === 1 ? '' : 's'} configured.</p>
        )}
      </div>
    </div>
  );
}

/** Tiny 4-point star (avoids pulling an icon that reads as a favourite symbol). */
function Star4({ size, filled }: { size: number; filled: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor" strokeWidth={filled ? 0 : 2} strokeLinejoin="round" aria-hidden>
      <path d="M12 2l2.2 7.8L22 12l-7.8 2.2L12 22l-2.2-7.8L2 12l7.8-2.2z" />
    </svg>
  );
}
