"use client";

// ── Offline banner ───────────────────────────────────────────────────────────
// A single, app-wide signal that the machine has no network. Black Eclipse is
// local-first: the app itself keeps working, so the banner must be calm and
// truthful — it never claims the app is broken and never blocks a click.
//
// `useSyncExternalStore` reads the browser's own `online`/`offline` events, so
// every mount agrees and there is no polling. Mounted at the root (layout.tsx)
// because network-dependent surfaces are spread across every tab.

import { useEffect, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { Unplug } from "@/lib/icons";

function subscribe(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

function getSnapshot() {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}

/** The server cannot know the client's connection; render online on the server. */
function getServerSnapshot() {
  return false;
}

export function OfflineBanner() {
  const offline = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  // Portal only after mount: document.body does not exist during SSR.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted || !offline) return null;

  return createPortal(
    <div
      className="pointer-events-none fixed bottom-4 left-4 z-[75] max-w-[min(92vw,34rem)]"
      role="status"
      aria-live="polite"
    >
      <div className="glass-card-subtle flex items-center gap-2 rounded-full border border-amber-400/25 px-3 py-1.5 text-[11px] text-amber-200/90 shadow-lg">
        <Unplug size={13} className="shrink-0" aria-hidden />
        <span className="font-semibold tracking-tight">You&apos;re offline</span>
        <span className="hidden truncate text-amber-200/60 sm:inline">
          Local features keep working — live data resumes when you reconnect.
        </span>
      </div>
    </div>,
    document.body,
  );
}
