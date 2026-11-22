// ── Reveal a reviewed file in the OS file manager ───────────────────────────
// The Code review tree can show a changed file where it lives. The desktop shell
// answers the `be-reveal-path` core event (the same transport as notifications,
// because Tauri denies app-defined commands to the loopback origin). Rust
// validates the path and only REVEALS it — the file is never executed.
//
// In a browser (no shell) this is a no-op, honestly: the button says so.

export const REVEAL_PATH_EVENT = 'be-reveal-path';

type RevealBridge = {
  __TAURI__?: { event?: { emit?: (event: string, payload: unknown) => Promise<unknown> | unknown } };
  __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<unknown> | unknown };
};

function bridge(): RevealBridge | null {
  if (typeof window === 'undefined') return null;
  return window as unknown as RevealBridge;
}

/** Whether the window can ask the desktop shell to reveal a path. */
export function hasRevealBridge(): boolean {
  const api = bridge();
  return Boolean(api?.__TAURI__?.event?.emit || api?.__TAURI_INTERNALS__?.invoke);
}

/** Windows drive paths and POSIX absolute paths only. */
export function isAbsolutePath(path: string): boolean {
  return path.startsWith('/') || /^[A-Za-z]:[\\/]/.test(path);
}

/**
 * Ask the shell to reveal `absolutePath`. Returns false (and emits nothing) in a
 * browser or for a relative path, so a caller can hide/disable the affordance.
 */
export function revealInOs(absolutePath: string): boolean {
  if (!absolutePath || !isAbsolutePath(absolutePath) || !hasRevealBridge()) return false;
  const api = bridge();
  const payload = { path: absolutePath };
  try {
    const emit = api?.__TAURI__?.event?.emit;
    if (emit) {
      void Promise.resolve(emit(REVEAL_PATH_EVENT, payload)).catch(() => undefined);
      return true;
    }
    const invoke = api?.__TAURI_INTERNALS__?.invoke;
    if (invoke) {
      void Promise.resolve(
        invoke('plugin:event|emit', { event: REVEAL_PATH_EVENT, payload }),
      ).catch(() => undefined);
      return true;
    }
  } catch {
    /* bridge unavailable mid-navigation */
  }
  return false;
}

/** Join a workspace root and a repository-relative path for the reveal call. */
export function absoluteWorkspacePath(root: string | null | undefined, relative: string): string | null {
  if (!root || !relative) return null;
  if (!isAbsolutePath(root)) return null;
  const separator = root.includes('\\') && !root.includes('/') ? '\\' : '/';
  const left = root.replace(/[\\/]+$/, '');
  const right = relative.replace(/\\/g, '/').replace(/^\/+/, '');
  if (!right) return null;
  return `${left}${separator}${right.split('/').join(separator)}`;
}
