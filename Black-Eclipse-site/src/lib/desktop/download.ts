// ── Saving a produced file through the desktop shell ────────────────────────
// The app window is WebView2, which has no download shelf: an `<a download>`
// click or a `blob:` download can silently do nothing, which reads as "the
// download button is broken". The shell therefore owns the write: the page
// sends the bytes over the `be-save-download` core event (app-defined commands
// are denied to the loopback origin), Rust writes a real file in Downloads,
// reveals it and answers on `be-download-saved` with the absolute path.
//
// In a browser there is no bridge, so every helper here says so and the caller
// keeps the ordinary anchor download.

export const SAVE_DOWNLOAD_EVENT = 'be-save-download';
export const DOWNLOAD_SAVED_EVENT = 'be-download-saved';

type TauriEventApi = {
  emit?: (event: string, payload: unknown) => Promise<unknown> | unknown;
  listen?: (event: string, handler: (event: { payload: unknown }) => void) => Promise<() => void>;
};

type ShellBridge = {
  __TAURI__?: { event?: TauriEventApi };
  __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<unknown> | unknown };
};

function bridge(): ShellBridge | null {
  if (typeof window === 'undefined') return null;
  return window as unknown as ShellBridge;
}

/** Whether this window can ask the shell to save a file. */
export function hasDownloadBridge(): boolean {
  const api = bridge();
  return Boolean(api?.__TAURI__?.event?.emit || api?.__TAURI_INTERNALS__?.invoke);
}

/** Emit the save event. Returns false when there is no shell to ask. */
function emitSave(filename: string, dataBase64: string): boolean {
  const api = bridge();
  const payload = { name: filename || 'eclipse-download', dataBase64 };
  try {
    const emit = api?.__TAURI__?.event?.emit;
    if (emit) {
      void Promise.resolve(emit(SAVE_DOWNLOAD_EVENT, payload)).catch((error) => {
        console.warn('[download] the shell did not accept the file', error);
      });
      return true;
    }
    const invoke = api?.__TAURI_INTERNALS__?.invoke;
    if (invoke) {
      void Promise.resolve(
        invoke('plugin:event|emit', { event: SAVE_DOWNLOAD_EVENT, payload }),
      ).catch((error) => console.warn('[download] the shell did not accept the file', error));
      return true;
    }
  } catch {
    /* bridge unavailable mid-navigation */
  }
  return false;
}

/** Base64 without blowing the call stack on a multi-megabyte PDF. */
async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

/**
 * Ask the shell to save `blob` as `filename`.
 *
 * Returns true when the request was handed to the shell; false means "no shell
 * here", and the caller should fall back to an anchor download.
 */
export function saveDownloadViaShell(filename: string, blob: Blob): boolean {
  if (!hasDownloadBridge()) return false;
  // Encode asynchronously, but answer synchronously: a download click must not
  // depend on a promise the browser might never settle during navigation.
  void blobToBase64(blob)
    .then((encoded) => emitSave(filename, encoded))
    .catch((error) => console.warn('[download] could not read the file bytes', error));
  return true;
}

/**
 * Save a blob as `filename` — the one download entry point for the whole app.
 *
 * The desktop window has no download shelf, so the shell writes the file (and
 * reveals it in the OS file manager). A browser has no bridge and gets the
 * ordinary anchor download.
 */
export function saveBlob(filename: string, blob: Blob): void {
  if (saveDownloadViaShell(filename, blob)) return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/** A name from `Content-Disposition`, when the server sent one. */
function nameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim());
    } catch {
      return null;
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

/**
 * Intercept a download LINK in the desktop window: fetch the bytes and hand
 * them to the shell instead of relying on WebView2's missing download shelf.
 * Returns true when the click was handled.
 */
export function downloadFromUrlInShell(
  event: { preventDefault: () => void },
  url: string,
  filename?: string,
): boolean {
  if (!hasDownloadBridge()) return false;
  event.preventDefault();
  void (async () => {
    try {
      const response = await fetch(url, { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const name =
        filename ||
        nameFromDisposition(response.headers.get('content-disposition')) ||
        url.split('?')[0].split('/').filter(Boolean).pop() ||
        'eclipse-download';
      emitSave(name, await blobToBase64(blob));
    } catch (error) {
      console.warn('[download] the file could not be fetched', error);
    }
  })();
  return true;
}
