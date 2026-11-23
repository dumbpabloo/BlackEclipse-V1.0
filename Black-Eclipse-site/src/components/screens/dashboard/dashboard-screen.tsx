'use client';

// ── DashboardScreen — the customizable Financial Command Center ───────────────
// Hosts the workspace kernel (WorkspaceCanvas) for the active dashboard, plus
// the dashboard switcher, onboarding, widget library, and config panel.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { saveBlob } from '@/lib/desktop/download';
import { confirmDialog, promptDialog } from '@/lib/ui/confirm-dialog';
import { createPortal } from 'react-dom';
import { usePageShortcuts } from '@/hooks/use-page-shortcuts';
import {
  ChevronDown, ChevronLeft, ChevronRight, Copy, Download, Globe, History, LayoutDashboard, List, Lock, Maximize2, Minimize2, Plus, RefreshCw, RotateCcw, Settings2, Star, Trash2, Undo2, Upload,
} from '@/lib/icons';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/use-media-query';
import { useDashboards, isOnboarded, markOnboarded } from '@/lib/dashboard/use-dashboards';
import { DASHBOARD_WIDGET_IDS, widgetIcon, renderDashboardWidget, buildMarketViewLayout } from '@/lib/dashboard/registry';
import { useWidgets } from '@/lib/workspace/use-widgets';
import { eclipseSdk, type SdkDashboardRevision } from '@/lib/sdk';
import { PENDING_DASHBOARD_KEY } from '@/lib/sdk/navigate';
import { undoRevision } from '@/lib/sdk/dashboard-history';
import { getDashboardTemplate } from '@/lib/dashboard/templates';
import { getDefaultBoard } from '@/lib/dashboard/default-board';
import { useMarketViews } from '@/lib/dashboard/market-views';
import { CustomizableWorkspace, StatusBar } from '@/components/workspace';
import {
  fetchDashboardLayout,
  isOwnPublish,
  publishDashboardLayout,
  stableLayoutKey,
} from '@/lib/ui-engineering/dashboard-blueprint';
import { loadWorkspace, saveWorkspace } from '@/lib/workspace/persistence';
import { isSetupOverlayOpen, subscribeSetupOverlay } from '@/lib/setup/overlay';
import type { WorkspaceLayout } from '@/lib/workspace/types';
import { TopBar } from '@/components/layout/top-bar';
import { PageHeader } from '@/components/layout/page-header';
import { AuroraGlow } from '@/components/layout/aurora-glow';
import { DashboardOnboarding, type OnboardingResult } from './onboarding';
import { MarketTabs } from './market-tabs';

function formatWhen(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function DashboardScreen({ active = true }: { active?: boolean }) {
  const dashboards = useDashboards();
  const markets = useMarketViews();
  const widgets = useWidgets({ surface: 'dashboard' });
  const isMobile = useIsMobile();
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [watchlistOpen, setWatchlistOpen] = useState(true);
  const [marketOpen, setMarketOpen] = useState(true);
  // In-app fullscreen: the Command Center expands to a fixed full-viewport
  // overlay (its own slim bar), hiding the app chrome for a focused board.
  const [isFullscreen, setIsFullscreen] = useState(false);

  // The active layout's id + default market. The layout id keys the workspace
  // (each named layout owns its own widgets); the market drives "follow the tab"
  // widgets. A widget can still be pinned to any market from its own header.
  const layoutId = markets.active?.id ?? 'US';
  const activeMarket = markets.active?.market ?? 'US';

  // Seed the starting workspace for this layout. Priority:
  //   1. a layout explicitly created from a persona preset (its own templateId);
  //   2. the SHIPPED curated board (DEFAULT_BOARD_JSON) — the product default
  //      every new user downloads into;
  //   3. the dashboard's persona template (onboarding pick / legacy).
  // Side rails (Watchlist / Market) are layered on top in every case.
  const explicitTemplateId = markets.active?.templateId;
  const dashboardTemplateId = dashboards.active?.templateId ?? 'tape-reader';
  const defaultLayout = useMemo(() => {
    const known = new Set(DASHBOARD_WIDGET_IDS);
    // A layout created from a persona preset always honours that preset;
    // everything else (new users, resets) starts from the SHIPPED curated board
    // when one is configured. `buildMarketViewLayout` frames it with the side
    // rails and dedupes globally, so a curated widget that also lives in a rail
    // is never rendered twice.
    const curated = explicitTemplateId ? null : getDefaultBoard(known);
    const base = curated ?? (() => {
      const t = getDashboardTemplate(explicitTemplateId ?? dashboardTemplateId);
      return t ? t.build(DASHBOARD_WIDGET_IDS) : undefined;
    })();
    return buildMarketViewLayout(activeMarket, DASHBOARD_WIDGET_IDS, base ?? undefined);
  }, [activeMarket, explicitTemplateId, dashboardTemplateId]);

  // Per-dashboard + per-layout storage key. v7 is a deliberate reset into the
  // track board; pre-v7 arrangements are NOT carried forward (the old band
  // grammar was inherently gappy), so no legacy market-key seeding here.
  const storageKey = useMemo(() => {
    const baseKey = dashboards.active?.storageKey ?? 'be_ws_dashboard_none';
    return `${baseKey}__m_${layoutId}`;
  }, [dashboards.active?.storageKey, layoutId]);

  const knownWidgetIds = useMemo(() => {
    const ids = new Set<string>([...DASHBOARD_WIDGET_IDS, ...widgets.map((widget) => widget.id)]);
    const local = loadWorkspace(storageKey, defaultLayout);
    if (!local.corrupt) {
      for (const section of local.layout.sections) {
        for (const column of section.columns) {
          for (const widget of column.widgets) ids.add(widget.widgetId);
        }
      }
    }
    return [...ids];
  }, [storageKey, defaultLayout, widgets]);

  // First-run onboarding.
  const [onboarded, setOnboarded] = useState<boolean>(() => isOnboarded());
  const showOnboarding = dashboards.loaded && !onboarded;

  const handleOnboard = (result: OnboardingResult) => {
    markOnboarded();
    setOnboarded(true);
    if (dashboards.list.length === 1 && dashboards.list[0].name === 'Main') {
      dashboards.rename(dashboards.list[0].id, result.name);
    }
  };

  const rootRef = useRef<HTMLDivElement>(null);
  const createDashboard = async () => {
    setSwitcherOpen(false);
    const name = await promptDialog({ title: 'New dashboard name' });
    if (name) dashboards.create(name, 'tape-reader');
  };

  const toggleFullscreen = () => setIsFullscreen(v => !v);

  // Mirror the board to a UI blueprint (debounced) so Eclipse can see and edit
  // the dashboard the user actually has. Best-effort — a failure never affects
  // the UI. See docs/harness/capability-extension-plan.md (Phase E).
  const publishTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handleLayoutChange = useCallback((layout: WorkspaceLayout) => {
    const active = dashboards.active;
    if (!active) return;
    if (publishTimer.current) clearTimeout(publishTimer.current);
    publishTimer.current = setTimeout(() => {
      void publishDashboardLayout({ dashboardId: active.id, title: active.name, layout });
    }, 2500);
  }, [dashboards.active]);

  // Reverse sync (Phase E): an agent (or another client) edit to this
  // dashboard's mirrored blueprint lands on the live board. On a revision this
  // client did NOT publish, the fetched layout is persisted into this
  // dashboard's storage and the workspace remounts (`remoteRevision` keys it).
  // The immediate first poll runs BEFORE the mount publish (2.5 s debounce), so
  // an edit made while the page was closed is applied, not overwritten.
  const [remoteRevision, setRemoteRevision] = useState(0);
  const syncSeen = useRef<Map<string, number>>(new Map());
  const activeDashboardId = dashboards.active?.id;

  // A lock is enforced by the runtime; show it here so the user knows why edits
  // are refused without opening the SDK tab.
  const [dashboardLocked, setDashboardLocked] = useState(false);
  const refreshDashboardLock = useCallback(async () => {
    if (!activeDashboardId) {
      setDashboardLocked(false);
      return;
    }
    try {
      const { locks } = await eclipseSdk.locks.list({
        resourceType: 'dashboard',
        resourceId: activeDashboardId,
      });
      setDashboardLocked(locks.length > 0);
    } catch {
      setDashboardLocked(false);
    }
  }, [activeDashboardId]);
  useEffect(() => {
    void refreshDashboardLock();
    const onChanged = () => void refreshDashboardLock();
    window.addEventListener('eclipse:locks-changed', onChanged);
    const timer = active ? window.setInterval(() => void refreshDashboardLock(), 30_000) : null;
    return () => {
      window.removeEventListener('eclipse:locks-changed', onChanged);
      if (timer) window.clearInterval(timer);
    };
  }, [refreshDashboardLock, active]);

  useEffect(() => {
    if (!active || !activeDashboardId || typeof window === 'undefined') return;
    let cancelled = false;
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const snapshot = await fetchDashboardLayout(activeDashboardId);
        if (cancelled || !snapshot) return;
        const seen = syncSeen.current.get(activeDashboardId) ?? 0;
        if (snapshot.revision <= seen) return;
        syncSeen.current.set(activeDashboardId, snapshot.revision);
        if (isOwnPublish(activeDashboardId, snapshot.revision)) return;
        const local = loadWorkspace(storageKey, defaultLayout);
        if (!local.corrupt && stableLayoutKey(local.layout) === stableLayoutKey(snapshot.layout)) {
          return;
        }
        saveWorkspace(storageKey, snapshot.layout);
        setRemoteRevision((value) => value + 1);
      } finally {
        inFlight = false;
      }
    };
    void poll();

    let source: EventSource | null = null;
    let fallback: ReturnType<typeof setInterval> | null = null;
    const startFallback = () => {
      if (!fallback) fallback = setInterval(() => void poll(), 60_000);
    };
    // Closed while the setup wizard is overlaid so it cannot hold one of the
    // browser's six per-origin connections against the wizard's save.
    const connect = () => {
      if (source || isSetupOverlayOpen()) return;
      try {
        source = new EventSource('/api/ui/stream');
        source.addEventListener('changed', () => void poll());
        source.addEventListener('error', startFallback);
      } catch {
        source = null;
        startFallback();
      }
    };
    const disconnect = () => {
      source?.close();
      source = null;
    };
    connect();
    const unsubscribeOverlay = subscribeSetupOverlay((open) => {
      if (open) {
        disconnect();
        if (fallback) {
          clearInterval(fallback);
          fallback = null;
        }
      } else {
        connect();
        void poll();
      }
    });
    return () => {
      cancelled = true;
      unsubscribeOverlay();
      disconnect();
      if (fallback) clearInterval(fallback);
    };
  }, [activeDashboardId, storageKey, defaultLayout, active]);

  useEffect(() => {
    if (!dashboards.loaded || !dashboards.activeId) return;
    void eclipseSdk.dashboards.setActive(dashboards.activeId).catch(() => {});
  }, [dashboards.loaded, dashboards.activeId]);

  // The SDK/chat can ask the shell to open a specific board (`eclipse:open-dashboard`).
  // A ref keeps the listener stable while still reading the latest dashboard list.
  const dashboardStateRef = useRef(dashboards);
  dashboardStateRef.current = dashboards;
  useEffect(() => {
    const onOpen = (event: Event) => {
      const dashboardId = (event as CustomEvent<{ dashboardId?: string }>).detail?.dashboardId;
      if (!dashboardId) return;
      const current = dashboardStateRef.current;
      if (current.loaded && current.list.some((entry) => entry.id === dashboardId)) {
        try { window.localStorage.removeItem(PENDING_DASHBOARD_KEY); } catch { /* ignore */ }
        current.switchTo(dashboardId);
      }
      // Not loaded yet: the pending key is consumed by the effect below.
    };
    window.addEventListener('eclipse:open-dashboard', onOpen);
    return () => window.removeEventListener('eclipse:open-dashboard', onOpen);
  }, []);

  // The SDK can open a board before this screen mounts; consume the pending id
  // once the dashboard index is available.
  useEffect(() => {
    if (!dashboards.loaded) return;
    let pending: string | null = null;
    try { pending = window.localStorage.getItem(PENDING_DASHBOARD_KEY); } catch { /* ignore */ }
    if (!pending) return;
    try { window.localStorage.removeItem(PENDING_DASHBOARD_KEY); } catch { /* ignore */ }
    if (dashboards.list.some((entry) => entry.id === pending)) dashboards.switchTo(pending);
    // The list/switchTo change identity every render; `loaded` is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dashboards.loaded]);

  // Revision history / undo. The mirror blueprint retains bounded revisions;
  // restoring records a NEW revision, so an undo is itself undoable.
  const [historyOpen, setHistoryOpen] = useState(false);
  const [revisions, setRevisions] = useState<SdkDashboardRevision[]>([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const historyRequestRef = useRef(0);

  const loadHistory = useCallback(async () => {
    const id = dashboards.active?.id;
    if (!id) return;
    const requestId = ++historyRequestRef.current;
    setHistoryBusy(true);
    try {
      const { revisions: fetched } = await eclipseSdk.dashboards.history(id);
      if (requestId !== historyRequestRef.current) return;
      setRevisions(fetched);
      setHistoryError(null);
    } catch (err) {
      if (requestId === historyRequestRef.current) {
        setHistoryError(err instanceof Error ? err.message : 'history unavailable');
      }
    } finally {
      if (requestId === historyRequestRef.current) setHistoryBusy(false);
    }
  }, [dashboards.active?.id]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const restoreDashboardRevision = useCallback(async (revision: number) => {
    const id = dashboards.active?.id;
    if (!id || historyBusy) return;
    setHistoryBusy(true);
    setHistoryError(null);
    try {
      await eclipseSdk.dashboards.restore(id, revision);
      const snapshot = await fetchDashboardLayout(id);
      if (snapshot) {
        saveWorkspace(storageKey, snapshot.layout);
        syncSeen.current.set(id, snapshot.revision);
        setRemoteRevision((value) => value + 1);
      }
      setHistoryOpen(false);
      await loadHistory();
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : 'restore failed');
    } finally {
      setHistoryBusy(false);
    }
  }, [dashboards.active?.id, historyBusy, loadHistory, storageKey]);

  const handleUndo = useCallback(async () => {
    if (historyBusy) return;
    let entries = revisions;
    if (entries.length < 2) {
      const id = dashboards.active?.id;
      if (!id) return;
      try {
        const { revisions: fetched } = await eclipseSdk.dashboards.history(id);
        entries = fetched;
        setRevisions(fetched);
      } catch (err) {
        setHistoryError(err instanceof Error ? err.message : 'history unavailable');
        return;
      }
    }
    const target = undoRevision(entries);
    if (target) await restoreDashboardRevision(target.revision);
  }, [dashboards.active?.id, historyBusy, restoreDashboardRevision, revisions]);

  usePageShortcuts(rootRef, [
    { id: 'dashboard-new', label: 'New dashboard', keys: 'shift+d', run: createDashboard, enabled: dashboards.loaded && !showOnboarding },
    { id: 'dashboard-fullscreen', label: 'Toggle fullscreen', keys: 'shift+f', run: toggleFullscreen, enabled: dashboards.loaded && !showOnboarding },
    { id: 'dashboard-undo', label: 'Undo dashboard change', keys: 'mod+z', run: () => void handleUndo(), enabled: dashboards.loaded && !showOnboarding && !historyBusy },
  ]);

  // Lock page scroll while fullscreen so the reclaimed space is usable and the
  // background can't drift behind the overlay. Esc exits.
  useEffect(() => {
    if (!isFullscreen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    // Esc exits — unless the workspace already consumed it (active drag/grab),
    // in which case the card gesture should win.
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.defaultPrevented) setIsFullscreen(false); };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener('keydown', onKey);
    };
  }, [isFullscreen]);

  // The workspace is rendered either inline (normal) or inside the fullscreen
  // portal. It is hoisted into a variable so both paths share one instance.
  const workspaceEl = (
    <CustomizableWorkspace
      key={`${storageKey}:${remoteRevision}`}
      storageKey={storageKey}
      defaultLayout={defaultLayout}
      widgets={widgets}
      knownWidgetIds={knownWidgetIds}
      widgetIcon={widgetIcon}
      renderWidget={(widgetId, placement) => renderDashboardWidget(widgetId, placement)}
      onLayoutChange={handleLayoutChange}
      dense
      zones={{
        left: {
          className: 'hidden xl:block w-64',
          resizerClassName: 'hidden xl:block',
          collapsed: !watchlistOpen,
          onToggle: () => setWatchlistOpen(v => !v),
          header: (
            <div className="mb-2 flex items-center justify-between rounded-2xl glass-card px-3 py-2">
              <span className="flex items-center gap-1.5 text-xs font-bold text-foreground">
                <List size={13} className="text-primary" /> Watchlist
              </span>
              <button
                onClick={() => setWatchlistOpen(false)}
                className="glass-button rounded-lg p-1 text-muted-foreground hover:text-foreground cursor-pointer"
                title="Collapse watchlist panel"
              >
                <ChevronLeft size={14} />
              </button>
            </div>
          ),
        },
        right: {
          className: 'hidden lg:block w-72',
          resizerClassName: 'hidden lg:block',
          collapsed: !marketOpen,
          onToggle: () => setMarketOpen(v => !v),
          header: (
            <div className="mb-2 flex items-center justify-between rounded-2xl glass-card px-3 py-2">
              <span className="flex items-center gap-1.5 text-xs font-bold text-foreground">
                <Globe size={13} className="text-primary" /> Market
              </span>
              <button
                onClick={() => setMarketOpen(false)}
                className="glass-button rounded-lg p-1 text-muted-foreground hover:text-foreground cursor-pointer"
                title="Collapse market panel"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          ),
        },
      }}
      statusBar={isMobile ? undefined : <StatusBar version="v0.1.0" layoutStatus="saved" pendingOrders={0} notifications={0} />}
    />
  );

  // Fullscreen, portaled to <body>: the app shell is wrapped in a glass-card
  // (backdrop-filter creates a containing block), so a bare `position: fixed`
  // would be trapped and the global nav would stay on top. Same reason the
  // Chart Workspace portals its full-page overlay. Early-returned so exactly
  // one keyboard scope / rootRef exists at a time.
  if (isFullscreen && typeof document !== 'undefined') {
    return createPortal(
      <div
        ref={rootRef}
        tabIndex={-1}
        data-keyboard-scope="Command Center"
        className="fixed inset-0 z-[80] flex h-[100dvh] w-screen flex-col overflow-hidden bg-background/95 backdrop-blur-md outline-none"
      >
        {/* Aurora is mounted in the app shell at z-0, but this overlay portals
            to <body> above it, so fullscreen needs its own copy of the glow.
            Rendered first (behind the TopBar + workspace). */}
        <AuroraGlow />
        {/* The REAL app TopBar — price ribbon + global ⌘K search. No custom
            bar; fullscreen keeps the live market chrome the app already has. */}
        <div className="relative z-10 flex min-h-0 flex-1 flex-col">
          <div className="flex shrink-0 items-center gap-2">
            <div className="min-w-0 flex-1">
              <TopBar activeTab="dashboard" />
            </div>
            {/* Exit lives in the top bar row, aligned with the app chrome,
                instead of floating over the bottom of the board. */}
            <button
              onClick={toggleFullscreen}
              className="glass-button mr-3 inline-flex shrink-0 items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer md:mr-6"
              title="Exit fullscreen (Esc)"
            >
              <Minimize2 size={13} /> Exit
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">{workspaceEl}</div>
        </div>
        {/* The app-wide ChatDock (mounted once in StockChat) portals to <body>
            at z-[90], above this z-[80] overlay, so fullscreen needs no copy. */}
        {showOnboarding && (
          <DashboardOnboarding open onComplete={(r) => handleOnboard(r)} />
        )}
      </div>,
      document.body,
    );
  }

  return (
    <div ref={rootRef} data-keyboard-scope="Command Center" className="animate-fade-in-up w-full">
      {/* Header + toolbar — denser than a content page; this is a dashboard. */}
      <PageHeader
        icon={LayoutDashboard}
        title="Command Center"
        description={
          <>
            <p className="hidden sm:block">Arrange widgets, sections and columns to suit how you trade.</p>
            {dashboardLocked && (
              <span className="mt-1 inline-flex items-center gap-1 rounded border border-amber-500/25 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-wide text-amber-300">
                <Lock size={9} /> Locked
              </span>
            )}
          </>
        }
        actions={
          <>
          {/* Dashboard switcher */}
        <div className="relative">
          <button
            onClick={() => setSwitcherOpen(v => !v)}
            className="glass-button inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer"
          >
            {dashboards.active?.name ?? 'Dashboard'}
            <ChevronDown size={13} />
          </button>
          {switcherOpen && (
            <div className="absolute right-0 top-full z-40 mt-2 w-64 max-w-[90vw] rounded-2xl border border-white/15 dark:border-white/10 glass-card-strong shadow-2xl shadow-black/40 p-2 animate-fade-in">
              <div className="flex flex-col gap-1">
                {dashboards.list.map(d => (
                  <div
                    key={d.id}
                    className={cn(
                      'group flex items-center gap-2 rounded-xl px-2.5 py-2 text-xs cursor-pointer transition-colors',
                      d.id === dashboards.activeId ? 'bg-primary/10 text-primary' : 'hover:bg-accent/40 text-foreground',
                    )}
                    onClick={() => { dashboards.switchTo(d.id); setSwitcherOpen(false); }}
                  >
                    <span className="flex-1 truncate font-semibold">{d.name}</span>
                    {d.isDefault && <Star size={11} className="text-amber-400 shrink-0" />}
                    <span className="hidden group-hover:flex items-center gap-1 shrink-0">
                      <button
                        onClick={async (e) => { e.stopPropagation(); const n = await promptDialog({ title: 'Rename dashboard', defaultValue: d.name }); if (n) dashboards.rename(d.id, n); }}
                        className="rounded p-1 text-muted-foreground hover:text-primary cursor-pointer" title="Rename"
                      ><Settings2 size={11} /></button>
                      <button
                        onClick={(e) => { e.stopPropagation(); dashboards.duplicate(d.id); }}
                        className="rounded p-1 text-muted-foreground hover:text-primary cursor-pointer" title="Duplicate"
                      ><Copy size={11} /></button>
                      <button
                        onClick={(e) => { e.stopPropagation(); dashboards.setDefault(d.id); }}
                        className="rounded p-1 text-muted-foreground hover:text-amber-400 cursor-pointer" title="Set default"
                      ><Star size={11} /></button>
                      <button
                        onClick={async (e) => { e.stopPropagation(); if (await confirmDialog({ title: `Delete "${d.name}"?`, tone: 'danger' })) dashboards.remove(d.id); }}
                        className="rounded p-1 text-muted-foreground hover:text-rose-400 cursor-pointer" title="Delete"
                      ><Trash2 size={11} /></button>
                    </span>
                  </div>
                ))}
              </div>
              <div className="mt-1 border-t border-border/10 pt-1.5">
                <button
                  onClick={createDashboard}
                  title="New dashboard (Shift+D)"
                  className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-xs font-semibold text-primary hover:bg-primary/10 cursor-pointer"
                >
                  <Plus size={13} /> New dashboard
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={toggleFullscreen}
            className="glass-button inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer"
            title="Fullscreen (Shift+F)"
          >
            <Maximize2 size={13} /> <span className="hidden sm:inline">Fullscreen</span>
          </button>
          <button
            onClick={() => void handleUndo()}
            disabled={historyBusy}
            className="glass-button hidden items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer disabled:opacity-40 sm:inline-flex"
            title="Undo the last board change (Ctrl/Cmd+Z)"
          >
            <Undo2 size={13} /> <span className="hidden sm:inline">Undo</span>
          </button>
          <div className="relative">
            <button
              onClick={() => {
                setHistoryOpen((open) => !open);
                if (!historyOpen) void loadHistory();
              }}
              aria-expanded={historyOpen}
              className="glass-button hidden items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer sm:inline-flex"
              title="Revision history"
            >
              <History size={13} /> <span className="hidden sm:inline">History</span>
            </button>
            {historyOpen && (
              <div className="absolute right-0 top-full z-40 mt-2 w-80 max-w-[90vw] rounded-2xl border border-white/15 p-2 shadow-2xl shadow-black/40 glass-card-strong animate-fade-in dark:border-white/10">
                <div className="mb-1 flex items-center justify-between px-1">
                  <span className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Revision history</span>
                  <button
                    onClick={() => void loadHistory()}
                    disabled={historyBusy}
                    className="rounded p-1 text-muted-foreground hover:text-primary disabled:opacity-40"
                    title="Refresh history"
                  >
                    <RefreshCw size={11} />
                  </button>
                </div>
                {historyError && <p className="px-1 py-1 text-[10px] text-rose-300">{historyError}</p>}
                <div className="max-h-72 space-y-1 overflow-y-auto">
                  {revisions.length === 0 && !historyBusy && (
                    <p className="px-1 py-2 text-[11px] italic text-muted-foreground/70">No retained revisions yet.</p>
                  )}
                  {revisions.map((entry, index) => (
                    <div key={entry.revision} className="flex items-start justify-between gap-2 rounded-lg border border-border/20 px-2 py-1.5">
                      <div className="min-w-0">
                        <p className="text-[11px] font-semibold text-foreground">
                          r{entry.revision}{index === 0 ? ' · current' : ''}
                        </p>
                        <p className="truncate text-[10px] text-muted-foreground">{entry.summary || 'Board change'}</p>
                        <p className="text-[9px] text-muted-foreground/60">{formatWhen(entry.createdAt)}</p>
                      </div>
                      {index > 0 && (
                        <button
                          onClick={() => void restoreDashboardRevision(entry.revision)}
                          disabled={historyBusy}
                          className="glass-button shrink-0 rounded px-2 py-1 text-[10px] disabled:opacity-40"
                        >
                          Restore
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
          <button
            onClick={() => {
              const json = dashboards.exportLayout(dashboards.activeId!);
              if (!json) return;
              const blob = new Blob([json], { type: 'application/json' });
              saveBlob(`${dashboards.active?.name ?? 'dashboard'}.json`, blob);
            }}
            className="glass-button hidden items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer sm:inline-flex"
            title="Export this dashboard layout"
          >
            <Download size={13} /> <span className="hidden sm:inline">Export</span>
          </button>
          <button
            onClick={() => {
              const input = document.createElement('input');
              input.type = 'file';
              input.accept = '.json';
              input.onchange = async () => {
                const file = input.files?.[0];
                if (!file) return;
                const text = await file.text();
                dashboards.importLayout(text);
              };
              input.click();
            }}
            className="glass-button hidden items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer sm:inline-flex"
            title="Import a dashboard layout"
          >
            <Upload size={13} /> <span className="hidden sm:inline">Import</span>
          </button>
          <button
            onClick={() => dashboards.reset(dashboards.activeId!)}
            className="glass-button hidden items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-primary transition-all cursor-pointer sm:inline-flex"
            title="Reset this dashboard to its template"
          >
            <RotateCcw size={13} /> <span className="hidden sm:inline">Reset</span>
          </button>
        </div>
          </>
        }
        className="mb-3"
      />

      {/* Market tabs — pick a country/market; each keeps its own widget layout. */}
      <MarketTabs />

      {/* Reusable customizable workspace — left, centre and right are ONE
          workspace. The single Customize button drives all three, cards drag
          between them, and the left/right panels can be removed from Customize. */}
      {workspaceEl}

      {/* Onboarding */}
      {showOnboarding && (
        <DashboardOnboarding
          open
          onComplete={(r) => handleOnboard(r)}
        />
      )}
    </div>
  );
}


