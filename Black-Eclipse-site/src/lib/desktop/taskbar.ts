'use client';

// ── Desktop taskbar presence ────────────────────────────────────────────────
// The native shell owns the OS affordances: a badge drawn on the taskbar icon
// (`SetOverlayIcon`, or the Dock/launcher badge off-Windows), an indeterminate
// progress bar while a stream is working, and a one-shot attention flash when an
// answer lands while the window is in the background. This module owns the
// front-end half: the pure "what should the taskbar show" rule, the stored
// preference, and the event that tells Rust.
//
// It is deliberately separate from `notifications/tab-indicator.tsx`: that one
// is the BROWSER signal (title prefix, favicon, `setAppBadge`), which WebView2
// renders nowhere. Both read the same ledger, so they can never disagree.
//
// The browser front door has no Tauri bridge, so every entry point here is a
// no-op outside the desktop shell.

import { useEffect, useState } from 'react';
import { hasDesktopShell } from './window-glass';

export { hasDesktopShell };

/** Rust listens for this; app-defined commands are denied to the remote app
 *  origin, so core events are the transport (same as `be-notify`). */
export const TASKBAR_EVENT = 'be-taskbar';

/** Preference-change event so mounted surfaces refresh their toggle state. */
export const TASKBAR_PREF_EVENT = 'be:taskbar-badge-pref';

const STORAGE_KEY = 'be_taskbar_badge_v1';

export type TaskbarState = 'idle' | 'unread';

export interface TaskbarSignal {
  state: TaskbarState;
  /** Unread total; Windows paints it as the count on the taskbar badge. */
  count: number;
  /** Request ONE attention flash. Rust clears it as soon as the window is focused. */
  flash: boolean;
  /** A stream is doing work: show indeterminate taskbar progress. */
  working: boolean;
}

export interface TaskbarInputs {
  unread: number;
  /** Is the app window the operator's foreground window? */
  focused: boolean;
  /** The stored preference. */
  enabled: boolean;
  /**
   * Unread total at the previous evaluation. The flash is edge-triggered: only a
   * NEW arrival while unfocused asks for attention, so focusing and unfocusing
   * an app that is already waiting never re-flashes.
   */
  previousUnread: number;
  /** Is at least one agent stream doing work right now? */
  working: boolean;
}

/**
 * Pure decision: what the taskbar should show for this state.
 *
 * A badge follows unread, not focus — an item you have not looked at is still
 * unread after you click the window. Only the flash is focus-sensitive.
 * The preference governs both affordances (one switch, one mental model).
 */
export function deriveTaskbarSignal(inputs: TaskbarInputs): TaskbarSignal {
  const count = Math.max(0, Math.floor(inputs.unread));
  const working = inputs.enabled && inputs.working;
  if (!inputs.enabled || count === 0) {
    return { state: 'idle', count: 0, flash: false, working };
  }
  return {
    state: 'unread',
    count,
    flash: !inputs.focused && count > Math.max(0, Math.floor(inputs.previousUnread)),
    working,
  };
}

/** Default ON in the desktop shell (a badge is expected there); never in a browser. */
export function readTaskbarBadgeEnabled(): boolean {
  if (!hasDesktopShell()) return false;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw === null ? true : raw === '1';
  } catch {
    return true;
  }
}

export function setTaskbarBadgeEnabled(enabled: boolean): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0');
  } catch {
    /* storage disabled — the in-memory toggle still applies */
  }
  window.dispatchEvent(new CustomEvent(TASKBAR_PREF_EVENT, { detail: { enabled } }));
}

/**
 * Read/apply the setting and keep every mounted consumer in sync. Safe in the
 * browser (returns unavailable), and SSR-safe (resolved in an effect).
 */
export function useTaskbarBadgeEnabled(): {
  available: boolean;
  enabled: boolean;
  setEnabled: (enabled: boolean) => void;
} {
  const [available, setAvailable] = useState(false);
  const [enabled, setEnabledState] = useState(false);

  useEffect(() => {
    const desktop = hasDesktopShell();
    setAvailable(desktop);
    if (!desktop) return;
    setEnabledState(readTaskbarBadgeEnabled());
    const onChange = (event: Event) => {
      const detail = (event as CustomEvent<{ enabled?: boolean }>).detail;
      if (typeof detail?.enabled === 'boolean') setEnabledState(detail.enabled);
    };
    window.addEventListener(TASKBAR_PREF_EVENT, onChange);
    return () => window.removeEventListener(TASKBAR_PREF_EVENT, onChange);
  }, []);

  return { available, enabled, setEnabled: setTaskbarBadgeEnabled };
}

/**
 * Tell the shell what the taskbar should show.
 *
 * `__TAURI__.event.emit` is the documented path; the raw
 * `plugin:event|emit` IPC is the fallback for a shell where the global API is
 * not exposed. Both are core commands, which ARE allowed on the capability-
 * listed remote origin — unlike app-defined commands such as `invoke('...')`.
 */
export function emitTaskbarSignal(signal: TaskbarSignal): void {
  if (typeof window === 'undefined' || !hasDesktopShell()) return;
  const payload = {
    state: signal.state,
    count: signal.count,
    flash: signal.flash,
    working: signal.working,
  };
  try {
    const bridge = window as unknown as {
      __TAURI__?: { event?: { emit?: (event: string, payload: unknown) => Promise<unknown> } };
      __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<unknown> };
    };
    const api = bridge.__TAURI__?.event;
    if (api && typeof api.emit === 'function') {
      void Promise.resolve(api.emit(TASKBAR_EVENT, payload)).catch(() => undefined);
      return;
    }
    const internals = bridge.__TAURI_INTERNALS__;
    if (internals && typeof internals.invoke === 'function') {
      void Promise.resolve(
        internals.invoke('plugin:event|emit', { event: TASKBAR_EVENT, payload }),
      ).catch(() => undefined);
    }
  } catch {
    /* bridge unavailable mid-navigation — the next state change retries */
  }
}
