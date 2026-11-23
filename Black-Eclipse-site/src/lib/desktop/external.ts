// ── Open a link in the person's real browser ────────────────────────────────
// The desktop app is ONE WebView2 window. A `target="_blank"` anchor (the
// update download, a release page) is a new-window request the shell does not
// host, so the click silently does nothing. The page asks the shell instead
// over `be-open-external`; Rust accepts http(s) only and hands the URL to the
// operating system.
//
// In a browser there is no bridge, so the caller keeps the ordinary anchor.

export const OPEN_EXTERNAL_EVENT = 'be-open-external';

type TauriEventApi = {
  emit?: (event: string, payload: unknown) => Promise<unknown> | unknown;
};

type ShellBridge = {
  __TAURI__?: { event?: TauriEventApi };
  __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<unknown> | unknown };
};

function bridge(): ShellBridge | null {
  if (typeof window === 'undefined') return null;
  return window as unknown as ShellBridge;
}

export function hasExternalBridge(): boolean {
  const api = bridge();
  return Boolean(api?.__TAURI__?.event?.emit || api?.__TAURI_INTERNALS__?.invoke);
}

/** Ask the shell to open `url`. Returns false when there is no shell. */
export function openExternal(url: string): boolean {
  if (!url || !/^https?:\/\//i.test(url)) return false;
  const api = bridge();
  const payload = { url };
  try {
    const emit = api?.__TAURI__?.event?.emit;
    if (emit) {
      void Promise.resolve(emit(OPEN_EXTERNAL_EVENT, payload)).catch(() => undefined);
      return true;
    }
    const invoke = api?.__TAURI_INTERNALS__?.invoke;
    if (invoke) {
      void Promise.resolve(
        invoke('plugin:event|emit', { event: OPEN_EXTERNAL_EVENT, payload }),
      ).catch(() => undefined);
      return true;
    }
  } catch {
    /* bridge unavailable mid-navigation */
  }
  return false;
}

/**
 * Click handler for an external link: the shell opens it in the real browser.
 * Returns true when it was handled (caller should preventDefault).
 */
export function openExternalFromClick(
  event: { preventDefault: () => void },
  url: string | null | undefined,
): boolean {
  if (!url || !hasExternalBridge() || !/^https?:\/\//i.test(url)) return false;
  event.preventDefault();
  return openExternal(url);
}

/** Props for an external anchor that works in both the shell and a browser. */
export function externalLinkProps(url: string | null | undefined): {
  href: string | undefined;
  target: string | undefined;
  rel: string | undefined;
  onClick: ((event: { preventDefault: () => void }) => void) | undefined;
} {
  if (!url) return { href: undefined, target: undefined, rel: undefined, onClick: undefined };
  return {
    href: url,
    target: '_blank',
    rel: 'noopener noreferrer',
    onClick: (event) => {
      openExternalFromClick(event, url);
    },
  };
}
