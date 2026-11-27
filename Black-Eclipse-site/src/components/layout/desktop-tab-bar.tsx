'use client';

// ── Desktop tab strip ────────────────────────────────────────────────────────
// Rendered only inside the Tauri shell. The native window is frameless, so this
// bar is also the window chrome: tabs on the left, a drag region through the
// empty space, and minimize / maximize / close at the right. The browser front
// door never mounts it — `hasDesktopShell()` gates on Tauri's global bridge.
//
// Tabs are INSTANCES, not a de-duplicated page set. `+` appends a real tab that
// starts on the Chat page with a fresh session; navigating inside that tab moves
// it to the page the user picks (sidebar, palette, deep link). Closing the last
// tab is refused. Switching a tab calls `onSelectTab`, so the existing
// keep-alive panels preserve each page.
//
// Window chrome prefers the core window API (minimize / toggleMaximize / close /
// startDragging) and falls back to the `be-window-action` event bridge used by
// older shells. Both paths are needed: the event bridge avoids app-command ACLs,
// while the core API is the one Tauri guarantees for remote origins.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useRunningConversationIds } from '@/lib/agent/use-agent-run';
import {
  ChartLine,
  Code,
  Cpu,
  Database,
  FileText,
  FlaskConical,
  Ghost,
  GitBranch,
  ListChecks,
  Minus,
  Palette,
  Plus,
  Settings,
  Square,
  X,
  type LucideIcon,
} from '@/lib/icons';
import type { AppTab } from '@/lib/app-tabs';
import { DEFAULT_SIDEBAR_SECTIONS } from '@/lib/sidebar/config';
import { navLabelKey } from '@/lib/i18n/nav';
import { useT } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { canHandleKeyboardEvent } from '@/lib/keyboard';
import { useWindowGlass } from '@/lib/desktop/window-glass';
import {
  appendTab,
  createTab,
  moveTab,
  moveTabBy,
  moveTabToEdge,
  readStoredActiveId,
  readStoredTabs,
  removeTab,
  saveStoredActiveId,
  saveStoredTabs,
  setTabConversation,
  setTabMode,
  setTabPage,
  type DesktopTab,
} from '@/lib/desktop/tabs';
import { useSidebarConfigStore } from '@/lib/sidebar/store';
import { UpdateButton } from '@/components/layout/update-button';

interface TabMeta {
  label: string;
  icon: LucideIcon;
}

const NAV_META = new Map<AppTab, TabMeta>(
  DEFAULT_SIDEBAR_SECTIONS.map((section) => [
    section.id,
    { label: section.label, icon: section.icon },
  ]),
);

/** Pages reachable without a permanent sidebar entry still deserve a tab. */
const EXTRA_META: Partial<Record<AppTab, TabMeta>> = {
  chart: { label: 'Chart', icon: ChartLine },
  settings: { label: 'Settings', icon: Settings },
  tasks: { label: 'Tasks', icon: ListChecks },
  pages: { label: 'Pages', icon: FileText },
  'ui-builder': { label: 'UI Builder', icon: Palette },
  backtest: { label: 'Backtest', icon: FlaskConical },
  'research-data': { label: 'Research Data', icon: Database },
  'factor-lab': { label: 'Factor Lab', icon: FlaskConical },
  'ml-lab': { label: 'ML Lab', icon: Cpu },
  'graph-studio': { label: 'Graph Studio', icon: GitBranch },
  code: { label: 'Code', icon: Code },
  bots: { label: 'Bots', icon: Ghost },
};

function metaFor(tab: AppTab): TabMeta {
  return (
    NAV_META.get(tab) ??
    EXTRA_META[tab] ?? {
      label: tab.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
      icon: FileText,
    }
  );
}

function hasDesktopShell(): boolean {
  if (typeof window === 'undefined') return false;
  const bridge = window as unknown as {
    __TAURI__?: unknown;
    __TAURI_INTERNALS__?: unknown;
  };
  return Boolean(bridge.__TAURI__ || bridge.__TAURI_INTERNALS__);
}

type WindowAction = 'minimize' | 'toggle-maximize' | 'close' | 'start-drag';

interface TauriWindowApi {
  minimize: () => Promise<void>;
  toggleMaximize: () => Promise<void>;
  close: () => Promise<void>;
  startDragging: () => Promise<void>;
}

/** The core window object Tauri injects into the page (`withGlobalTauri`). */
function tauriWindow(): TauriWindowApi | null {
  if (typeof window === 'undefined') return null;
  try {
    const bridge = window as unknown as {
      __TAURI__?: { window?: { getCurrentWindow?: () => TauriWindowApi } };
    };
    return bridge.__TAURI__?.window?.getCurrentWindow?.() ?? null;
  } catch {
    return null;
  }
}

/** Old-shell fallback: the injected script forwards this to Rust. */
function eventFallback(action: WindowAction): void {
  window.dispatchEvent(new CustomEvent('be-window-action', { detail: { action } }));
}

function windowAction(action: Exclude<WindowAction, 'start-drag'>): void {
  const win = tauriWindow();
  if (!win) {
    eventFallback(action);
    return;
  }
  const run =
    action === 'minimize'
      ? win.minimize
      : action === 'toggle-maximize'
        ? win.toggleMaximize
        : win.close;
  try {
    Promise.resolve(run.call(win)).catch(() => eventFallback(action));
  } catch {
    eventFallback(action);
  }
}

function beginWindowDrag(): void {
  const win = tauriWindow();
  if (!win) {
    eventFallback('start-drag');
    return;
  }
  try {
    Promise.resolve(win.startDragging()).catch(() => eventFallback('start-drag'));
  } catch {
    eventFallback('start-drag');
  }
}

function isChromeTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !(el && typeof el.closest === 'function' && el.closest('[data-be-no-drag]'));
}

export function DesktopTabBar({
  activeTab,
  conversationId,
  conversations,
  onSelectTab,
  onSelectConversation,
  onNewChat,
}: {
  activeTab: AppTab;
  /** The shell's live chat session: a conversation id or `null` for a fresh chat. */
  conversationId?: string | null;
  /** Transcript list, so a Chat tab can be named after the session it shows. */
  conversations?: ReadonlyArray<{ id: string; title: string }>;
  onSelectTab: (tab: AppTab) => void;
  /** Load an existing chat into the shell when its tab is restored. */
  onSelectConversation?: (id: string) => void;
  /** `+` starts a fresh session and lands the new tab on the Chat page. */
  onNewChat?: () => void;
}) {
  const t = useT();
  // Applies the persisted glass-window preference at shell boot (and tells Rust
  // to apply the acrylic). Mounted for the life of the app, unlike Settings.
  useWindowGlass();
  // Per-tab run indicator: the tab strip is the only place that can show which
  // conversation is still working while another tab has focus (the Agent
  // Workspace is a single projection by design).
  const runningConversationIds = useRunningConversationIds();
  const bootTab = useRef(activeTab);
  const bootId = useRef('boot');
  const [desktop, setDesktop] = useState(false);
  const [restored, setRestored] = useState(false);
  const [tabs, setTabs] = useState<DesktopTab[]>(() => [
    createTab(bootTab.current, bootId.current),
  ]);
  const [activeId, setActiveId] = useState(bootId.current);
  const [dragging, setDragging] = useState<string | null>(null);
  const tabListRef = useRef<HTMLDivElement>(null);
  // Pointer-based reorder. HTML5 drag-and-drop is the fragile path inside the
  // Tauri shell (WebView2 replaces the page's drag handler with Tauri's file-drop
  // handler unless the native window opts out), so tabs reorder with plain
  // pointer events: they work in the browser, in the desktop webview, and on a
  // shell that predates the native opt-out. A mouse/pen only — touch keeps
  // scrolling the strip.
  const pointerDragRef = useRef<{
    id: string;
    pointerId: number;
    startX: number;
    startY: number;
    active: boolean;
  } | null>(null);
  // Set when a real drag moved the tab, so the pointerup does not also select it.
  const suppressClickRef = useRef(false);
  // FLIP snapshot: the strip's rects just before a reorder. The layout effect
  // below animates each tab from its old X to its new X, so a drag reads as a
  // smooth slide instead of an instant swap.
  const flipRectsRef = useRef<Map<string, DOMRect> | null>(null);
  // Live FLIP animations, cancelled when a newer reorder lands so rapid drags
  // never stack two transforms on the same tab.
  const flipAnimationsRef = useRef<Map<string, Animation>>(new Map());

  const captureTabRects = useCallback(() => {
    const strip = tabListRef.current;
    if (!strip) return;
    const rects = new Map<string, DOMRect>();
    strip.querySelectorAll<HTMLElement>('[data-tab-id]').forEach((el) => {
      const id = el.dataset.tabId;
      if (id) rects.set(id, el.getBoundingClientRect());
    });
    flipRectsRef.current = rects;
  }, []);

  const setDragCursor = useCallback((active: boolean) => {
    if (typeof document === 'undefined') return;
    // The grab cursor belongs to an ACTIVE drag only — hovering a tab shows the
    // normal arrow, so the strip does not advertise a drag handle it does not have.
    document.documentElement.style.cursor = active ? 'grabbing' : '';
  }, []);

  useEffect(() => () => setDragCursor(false), [setDragCursor]);

  // Detect the shell after mount (the Tauri bridge does not exist during SSR).
  // Restore the previous session's tabs once, keeping the URL/boot page open.
  // The tab that was in front last time wins when it still shows the boot page,
  // so a relaunch returns to the session the user left, not merely the first tab.
  useEffect(() => {
    if (!hasDesktopShell()) return;
    setDesktop(true);
    const stored = readStoredTabs();
    if (stored.length > 0) {
      const lastActive = readStoredActiveId();
      const preferred = lastActive
        ? stored.find((tab) => tab.id === lastActive && tab.page === bootTab.current)
        : undefined;
      const boot = preferred ?? stored.find((tab) => tab.page === bootTab.current);
      if (boot) {
        // Seed an upgraded tab (stored before modes were part of the record)
        // with the mode this shell is already showing, then restore the mode
        // that tab had last time.
        const mode = useSidebarConfigStore.getState().mode;
        setTabs(setTabMode(stored, boot.id, mode));
        setActiveId(boot.id);
        if (boot.mode) useSidebarConfigStore.getState().applyTabMode(boot.mode);
      } else {
        const added = appendTab(stored, bootTab.current, useSidebarConfigStore.getState().mode);
        setTabs(added.tabs);
        setActiveId(added.tab.id);
      }
    }
    setRestored(true);
  }, []);

  // The sidebar's menu mode is per SHELL (sessionStorage), but desktop tabs are
  // in-page instances inside ONE shell — so without this every tab would share
  // one mode. Each tab records the mode it was showing and restores it when it
  // becomes active, so a Bots tab and a Command Center tab keep their own menu.
  //
  // Declared after the Sidebar's own mode-sync effect in the tree, so applying a
  // tab's saved mode wins even when that tab's page would otherwise force the
  // page's mode.
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;
  const tabsRef = useRef(tabs);
  tabsRef.current = tabs;

  useEffect(() => {
    if (!desktop) return;
    const tab = tabsRef.current.find((item) => item.id === activeId);
    if (!tab) return;
    const store = useSidebarConfigStore.getState();
    if (tab.mode) {
      if (tab.mode !== store.mode) store.applyTabMode(tab.mode);
    } else {
      setTabs((prev) => setTabMode(prev, activeId, store.mode));
    }
  }, [desktop, activeId]);

  useEffect(() => {
    if (!desktop) return;
    // Every mode change (switcher, shortcut, navigation) belongs to the tab in
    // front, so write it back into that tab's record.
    return useSidebarConfigStore.subscribe((state, prev) => {
      if (state.mode === prev.mode) return;
      setTabs((current) => setTabMode(current, activeIdRef.current, state.mode));
    });
  }, [desktop]);

  // In-app navigation (sidebar, palette, deep link) moves the ACTIVE tab. When
  // that navigation lands on Chat, the tab adopts the session now in front, so
  // a later switch back restores what this tab was actually showing.
  useEffect(() => {
    if (!desktop) return;
    setTabs((prev) => {
      const next = setTabPage(prev, activeId, activeTab);
      const live = conversationIdRef.current;
      if (activeTab !== 'chat' || !bootSessionRef.current || live === undefined) return next;
      return setTabConversation(next, activeId, live);
    });
  }, [activeTab, activeId, desktop]);

  useEffect(() => {
    if (!desktop || !restored) return;
    saveStoredTabs(tabs);
  }, [tabs, desktop, restored]);

  // Remember which tab was in front so a relaunch returns to that one instead
  // of the first stored tab (which may belong to a different session).
  useEffect(() => {
    if (!desktop || !restored) return;
    saveStoredActiveId(activeId);
  }, [desktop, restored, activeId]);

  const onSelectConversationRef = useRef(onSelectConversation);
  onSelectConversationRef.current = onSelectConversation;
  const onNewChatRef = useRef(onNewChat);
  onNewChatRef.current = onNewChat;
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;
  // True once the launch tab's own session has been settled. Until then the
  // shell's session prop is still the pre-hydration `null`, and writing it into
  // the restored tab would erase the conversation the tab was bound to.
  const bootSessionRef = useRef(false);

  // Keep the ACTIVE tab's binding current without a tab switch: sending a
  // message materialises a conversation, and picking a chat from history
  // changes it. Written to the tab record so switching away and back is stable.
  // Declared BEFORE the boot effect: on launch it must stay inert until the
  // boot session has been settled from the stored record.
  useEffect(() => {
    if (!desktop || !bootSessionRef.current || conversationId === undefined) return;
    if (activeTab !== 'chat') return;
    setTabs((prev) => setTabConversation(prev, activeId, conversationId));
  }, [desktop, activeId, activeTab, conversationId]);

  // Restore the launch tab's chat session, once. Deferred one tick so the
  // shell's own mount effects have hydrated the transcript cache first —
  // restoring earlier would be overwritten by the "open most recent chat"
  // boot path. A fresh-chat tab (`null`) keeps that boot behaviour and adopts
  // whatever the shell opened; a legacy tab (`undefined`) does the same.
  useEffect(() => {
    if (!desktop || !restored || bootSessionRef.current) return;
    bootSessionRef.current = true;
    const tab = tabsRef.current.find((item) => item.id === activeId);
    if (!tab || tab.page !== 'chat' || !tab.conversationId) return;
    const conversation = tab.conversationId;
    const timer = window.setTimeout(() => onSelectConversationRef.current?.(conversation), 0);
    return () => window.clearTimeout(timer);
  }, [desktop, restored, activeId]);

  // Persist the live session into the tab that is about to leave the front.
  // Returns the updated list, so an append/remove in the same event starts from
  // the captured bindings instead of the pre-update state. A tab showing another
  // page is not displaying the chat session, so it keeps its own binding.
  const captureSession = useCallback((): DesktopTab[] => {
    const live = conversationIdRef.current;
    const tabs = tabsRef.current;
    if (live === undefined) return tabs;
    const current = tabs.find((tab) => tab.id === activeIdRef.current);
    if (!current || current.page !== 'chat') return tabs;
    return setTabConversation(tabs, current.id, live);
  }, []);

  const restoreSession = useCallback((tab: DesktopTab) => {
    if (tab.page !== 'chat') return;
    if (tab.conversationId === null) onNewChatRef.current?.();
    else if (tab.conversationId) onSelectConversationRef.current?.(tab.conversationId);
  }, []);

  const select = useCallback(
    (tab: DesktopTab) => {
      const same = tab.id === activeIdRef.current;
      if (!same) setTabs(captureSession());
      setActiveId(tab.id);
      onSelectTab(tab.page);
      if (!same) restoreSession(tab);
    },
    [captureSession, onSelectTab, restoreSession],
  );

  const close = useCallback(
    (id: string) => {
      const base = captureSession();
      const result = removeTab(base, id, activeIdRef.current);
      if (result.tabs === base) return;
      setTabs(result.tabs);
      if (result.activeId !== activeIdRef.current) {
        const next = result.tabs.find((tab) => tab.id === result.activeId);
        setActiveId(result.activeId);
        if (next) {
          onSelectTab(next.page);
          restoreSession(next);
        }
      }
    },
    [captureSession, onSelectTab, restoreSession],
  );

  const newTab = useCallback(() => {
    // A real new tab instance: it starts as a fresh Chat session (bound to
    // `null`, so no other tab's conversation is touched), and whatever page the
    // user opens next becomes that tab's page.
    const base = captureSession();
    const { tabs: next, tab } = appendTab(
      base,
      'chat',
      useSidebarConfigStore.getState().mode,
      null,
    );
    setTabs(next);
    setActiveId(tab.id);
    onNewChatRef.current?.();
    onSelectTab('chat');
  }, [captureSession, onSelectTab]);

  const labelFor = useCallback(
    (tab: AppTab) => {
      const key = navLabelKey(tab);
      return key ? t(key) : metaFor(tab).label;
    },
    [t],
  );

  // Keyboard reorder for the active tab: Ctrl/Cmd+Shift+Home/End jump to an
  // edge, Ctrl/Cmd+Shift+PageUp/PageDown (or ←/→) step one slot. Skipped while
  // typing or behind a modal, exactly like every other chord.
  useEffect(() => {
    if (!desktop) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || !event.shiftKey || event.altKey) return;
      if (!canHandleKeyboardEvent(event)) return;
      let next: DesktopTab[] | null = null;
      switch (event.key) {
        case 'Home':
          next = moveTabToEdge(tabs, activeId, 'first');
          break;
        case 'End':
          next = moveTabToEdge(tabs, activeId, 'last');
          break;
        case 'PageUp':
        case 'ArrowLeft':
          next = moveTabBy(tabs, activeId, -1);
          break;
        case 'PageDown':
        case 'ArrowRight':
          next = moveTabBy(tabs, activeId, 1);
          break;
      }
      if (!next || next === tabs) return;
      event.preventDefault();
      setTabs(next);
      requestAnimationFrame(() => {
        tabListRef.current
          ?.querySelector('[role="tab"][aria-selected="true"]')
          ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      });
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [desktop, tabs, activeId]);

  // Slide every tab that changed slot after a reorder (pointer drag OR the
  // keyboard chords). Runs before paint, so there is no flash of the new order.
  useLayoutEffect(() => {
    const before = flipRectsRef.current;
    flipRectsRef.current = null;
    if (!before) return;
    const strip = tabListRef.current;
    if (!strip) return;
    strip.querySelectorAll<HTMLElement>('[data-tab-id]').forEach((el) => {
      const id = el.dataset.tabId;
      const prev = id ? before.get(id) : undefined;
      if (!id || !prev) return;
      const next = el.getBoundingClientRect();
      const dx = prev.left - next.left;
      if (Math.abs(dx) < 0.5 || typeof el.animate !== 'function') return;
      // Cancel a still-running slide so two transforms never stack.
      flipAnimationsRef.current.get(id)?.cancel();
      const animation = el.animate(
        [{ transform: `translateX(${dx}px)` }, { transform: 'translateX(0)' }],
        { duration: 170, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' },
      );
      flipAnimationsRef.current.set(id, animation);
      animation.onfinish = () => {
        if (flipAnimationsRef.current.get(id) === animation) {
          flipAnimationsRef.current.delete(id);
        }
      };
    });
  }, [tabs]);

  if (!desktop) return null;

  return (
    <div
      data-be-drag=""
      className="desktop-tab-bar relative z-30 flex h-10 w-full shrink-0 select-none items-stretch pl-2"
      onMouseDown={(event) => {
        if (event.button !== 0 || !isChromeTarget(event.target)) return;
        beginWindowDrag();
      }}
      onDoubleClick={(event) => {
        if (!isChromeTarget(event.target)) return;
        windowAction('toggle-maximize');
      }}
    >
      <div
        ref={tabListRef}
        role="tablist"
        aria-label="Open tabs"
        className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto scrollbar-hide py-1.5 pr-2"
      >
        {tabs.map((tab) => {
          const meta = metaFor(tab.page);
          const Icon = meta.icon;
          const active = tab.id === activeId;
          // A Chat tab is named after the session it holds, so multiple chat
          // tabs (the whole point of instances) are tellable apart.
          const sessionTitle =
            tab.page === 'chat' && tab.conversationId
              ? conversations?.find((item) => item.id === tab.conversationId)?.title
              : undefined;
          const label = sessionTitle?.trim() || labelFor(tab.page);
          return (
            <div
              key={tab.id}
              role="tab"
              aria-selected={active}
              tabIndex={0}
              data-be-no-drag=""
              data-tab-id={tab.id}
              title={`${label} — drag to reorder`}
              onClick={() => {
                // A completed drag already moved the tab; do not also activate it.
                if (suppressClickRef.current) {
                  suppressClickRef.current = false;
                  return;
                }
                select(tab);
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  select(tab);
                }
              }}
              onAuxClick={(event) => {
                if (event.button === 1) close(tab.id);
              }}
              onPointerDown={(event) => {
                if (event.button !== 0 || event.pointerType === 'touch') return;
                // The close control and other buttons keep their own behaviour.
                if ((event.target as HTMLElement).closest('button')) return;
                pointerDragRef.current = {
                  id: tab.id,
                  pointerId: event.pointerId,
                  startX: event.clientX,
                  startY: event.clientY,
                  active: false,
                };
              }}
              onPointerMove={(event) => {
                const drag = pointerDragRef.current;
                if (!drag || drag.id !== tab.id || drag.pointerId !== event.pointerId) return;
                if (!drag.active) {
                  const dx = event.clientX - drag.startX;
                  const dy = event.clientY - drag.startY;
                  if (dx * dx + dy * dy < 25) return; // 5px dead zone
                  drag.active = true;
                  suppressClickRef.current = true;
                  setDragging(tab.id);
                  setDragCursor(true);
                  try {
                    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
                  } catch {
                    /* capture is a nicety; moves still arrive on the element */
                  }
                }
                event.preventDefault();
                const strip = tabListRef.current;
                if (!strip) return;
                // Edge auto-scroll: dragging past either end of a long strip
                // keeps the destination visible.
                const box = strip.getBoundingClientRect();
                const edge = 28;
                if (event.clientX < box.left + edge) {
                  strip.scrollLeft = Math.max(0, strip.scrollLeft - 14);
                } else if (event.clientX > box.right - edge) {
                  strip.scrollLeft += 14;
                }
                // Stable insertion boundary: cross the MIDPOINT of a neighbour
                // to move past it. Hit-testing the raw hovered tab oscillates
                // when the strip reflows under the cursor; midpoints do not.
                const els = Array.from(strip.querySelectorAll<HTMLElement>('[data-tab-id]'));
                let boundary = tabs.length;
                for (let i = 0; i < els.length; i += 1) {
                  const rect = els[i].getBoundingClientRect();
                  if (event.clientX < rect.left + rect.width / 2) {
                    boundary = i;
                    break;
                  }
                }
                const from = tabs.findIndex((item) => item.id === drag.id);
                if (from === -1) return;
                const insertAt = boundary > from ? boundary - 1 : boundary;
                if (insertAt === from) return;
                // Snapshot before the reorder so the layout effect can slide
                // every displaced tab from its old slot to the new one.
                captureTabRects();
                const next = [...tabs];
                const [moved] = next.splice(from, 1);
                next.splice(insertAt, 0, moved);
                setTabs(next);
              }}
              onPointerUp={(event) => {
                const drag = pointerDragRef.current;
                if (!drag || drag.id !== tab.id) return;
                pointerDragRef.current = null;
                if (drag.active) {
                  try {
                    (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
                  } catch {
                    /* already released */
                  }
                  setDragging(null);
                  setDragCursor(false);
                  // The click that follows this pointerup must be ignored; clear
                  // the flag right after in case no click is dispatched at all.
                  window.setTimeout(() => {
                    suppressClickRef.current = false;
                  }, 0);
                }
              }}
              onPointerCancel={() => {
                pointerDragRef.current = null;
                setDragging(null);
                setDragCursor(false);
                suppressClickRef.current = false;
              }}
              className={cn(
                'group/tab flex h-7 min-w-0 max-w-[220px] shrink-0 items-center gap-1.5 rounded-md pl-2.5 pr-1 text-[11.5px] font-medium outline-none transition-[color,background-color,box-shadow,opacity] duration-150 focus-visible:ring-1 focus-visible:ring-white/20',
                dragging === tab.id &&
                  'z-10 bg-white/[0.10] text-foreground opacity-80 shadow-[0_6px_18px_rgba(0,0,0,0.45)] ring-1 ring-white/15',
                active
                  ? 'bg-white/[0.08] text-foreground shadow-[inset_0_0_0_1px_rgba(255,255,255,0.06)]'
                  : 'text-muted-foreground hover:bg-white/[0.04] hover:text-foreground',
              )}
            >
              <Icon size={12} className="shrink-0 opacity-80" />
              {tab.page === 'chat' && tab.conversationId &&
                runningConversationIds.includes(tab.conversationId) && (
                  <span
                    title="Run in progress"
                    aria-label="Run in progress"
                    className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-primary"
                  />
                )}
              <span className="truncate">{label}</span>
              <button
                type="button"
                aria-label={`Close ${label}`}
                title="Close tab"
                onClick={(event) => {
                  event.stopPropagation();
                  close(tab.id);
                }}
                className={cn(
                  'ml-0.5 grid h-4 w-4 shrink-0 place-items-center rounded text-muted-foreground/60 transition-colors hover:bg-white/10 hover:text-foreground',
                  active ? 'opacity-100' : 'opacity-0 group-hover/tab:opacity-100',
                )}
              >
                <X size={10} />
              </button>
            </div>
          );
        })}

        <button
          type="button"
          data-be-no-drag=""
          onClick={newTab}
          aria-label="New tab"
          title="New tab"
          className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-white/[0.06] hover:text-foreground outline-none focus-visible:ring-1 focus-visible:ring-white/20"
        >
          <Plus size={13} />
        </button>
      </div>

      <div data-be-no-drag="" className="flex shrink-0 items-stretch">
        <UpdateButton />
        <button
          type="button"
          aria-label="Minimize window"
          title="Minimize"
          onClick={() => windowAction('minimize')}
          className="grid w-11 place-items-center text-muted-foreground transition-colors hover:bg-white/[0.08] hover:text-foreground"
        >
          <Minus size={14} />
        </button>
        <button
          type="button"
          aria-label="Maximize or restore window"
          title="Maximize / restore"
          onClick={() => windowAction('toggle-maximize')}
          className="grid w-11 place-items-center text-muted-foreground transition-colors hover:bg-white/[0.08] hover:text-foreground"
        >
          <Square size={10} />
        </button>
        <button
          type="button"
          aria-label="Close window"
          title="Close"
          onClick={() => windowAction('close')}
          className="grid w-11 place-items-center text-muted-foreground transition-colors hover:bg-[#e81123] hover:text-white"
        >
          <X size={14} />
        </button>
      </div>
    </div>
  );
}
