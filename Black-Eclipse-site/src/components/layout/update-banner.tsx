"use client";

// ── In-app update notice ────────────────────────────────────────────────────
// Mounted once at the app root (src/app/layout.tsx), mirroring the other
// watchers. It asks the LOCAL server (/api/updates) whether a newer SIGNED
// release exists and shows a single glass notice when there is one.
//
// Deliberate choices:
//   * The banner ACTS, not just notifies. It used to offer only "copy this
//     command", which made the most visible update affordance in the product the
//     least useful one: users clicked it and nothing happened. It now runs the
//     same staged update as the title-bar panel (one shared hook,
//     `use-update-apply.ts`) and offers the restart when the build is ready.
//   * Dismiss / skip are per-version and local-only.
//   * A failed check is silent here. "Couldn't check" belongs in Settings, where
//     the user asked; a banner that cries wolf on every offline launch gets
//     ignored.
//   * It renders through a portal so the frosted glass samples the real page.

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Download, ExternalLink, Loader2, RefreshCw, ShieldCheck, X } from "@/lib/icons";
import { Button } from "@/components/ui/button";
import { saveUpdatePrefs } from "@/lib/updates/prefs";
import { useUpdateApply } from "@/lib/updates/use-update-apply";
import { useUpdateStatus } from "@/lib/updates/use-update-status";
import { externalLinkProps } from "@/lib/desktop/external";
import type { UpdateJobPhase, UpdateStatus } from "@/lib/updates/types";

const PHASE_PERCENT: Record<UpdateJobPhase, number> = {
  starting: 6,
  verifying: 15,
  "backing-up": 25,
  checkout: 38,
  installing: 52,
  building: 74,
  downloading: 60,
  unpacking: 68,
  promoting: 90,
  stopping: 60,
  ready: 100,
  failed: 100,
};

export function UpdateBanner() {
  const [mounted, setMounted] = useState(false);
  // The title-bar button uses the same hook; the route's 5 min manifest cache
  // keeps two consumers at one upstream fetch.
  const { status: checked, prefs } = useUpdateStatus();
  const { job, error: applyError, running, canApplyFromShell, apply, restart } = useUpdateApply();
  const [showCommand, setShowCommand] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const hidden =
    !checked?.available ||
    !checked.latestVersion ||
    checked.latestVersion === prefs.dismissedVersion ||
    checked.latestVersion === prefs.skippedVersion;

  if (!mounted || hidden || !checked) return null;

  const status: UpdateStatus = checked;
  // A packaged install has no repository, but the shell can still run the
  // verified installer runner; only a window with no shell bridge is limited to
  // a download link.
  const manual = !status.canSelfUpdate && !canApplyFromShell;
  const downloadHref = status.notesUrl ?? status.downloadUrl;
  const ready = job?.status === "ready" && Boolean(job.toVersion) && job.toVersion !== status.currentVersion;
  const failed = job?.status === "failed" && !ready;
  const percent = job ? PHASE_PERCENT[job.phase] ?? 50 : 0;

  const dismiss = () => {
    saveUpdatePrefs({ ...prefs, dismissedVersion: status.latestVersion });
  };

  const skip = () => {
    saveUpdatePrefs({ ...prefs, skippedVersion: status.latestVersion });
  };

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText("black update");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setShowCommand(true);
    }
  };

  return createPortal(
    <div
      className="fixed bottom-4 right-4 z-[70] w-[calc(100vw-2rem)] max-w-sm"
      role="status"
      aria-live="polite"
    >
      <div className="glass-card-strong rounded-2xl border border-primary/25 p-4 shadow-2xl space-y-3">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-primary/15 text-primary">
            {running ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold tracking-tight">
              {running
                ? `Updating to v${status.latestVersion}`
                : ready
                  ? `v${job?.toVersion} is ready`
                  : failed
                    ? "Update did not finish"
                    : `Eclipse v${status.latestVersion} is available`}
            </p>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              {running
                ? job?.message ?? "Working…"
                : ready
                  ? "The signed build is staged. Restart Eclipse to switch to it."
                  : failed
                    ? job?.error ?? "The updater stopped. Your current build is untouched."
                    : manual
                      ? `You're on v${status.currentVersion}. Download the signed installer below.`
                      : `You're on v${status.currentVersion}. The release manifest is signature-checked before this notice appears.`}
            </p>
          </div>
          <button
            type="button"
            onClick={dismiss}
            aria-label="Dismiss update notice"
            className="shrink-0 rounded-lg p-1 text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
          >
            <X size={14} />
          </button>
        </div>

        {running && (
          <div className="h-1 overflow-hidden rounded-full bg-white/10">
            <div
              className="h-full rounded-full bg-primary transition-[width] duration-500"
              style={{ width: `${percent}%` }}
            />
          </div>
        )}

        {status.mandatory && !ready && (
          <p className="rounded-lg border border-amber-500/25 bg-amber-500/10 px-2.5 py-1.5 text-[10px] text-amber-200/90">
            This release is required to keep receiving data updates.
          </p>
        )}

        {(() => {
          const message = applyError;
          return message ? <p className="text-[10px] text-red-300">{message}</p> : null;
        })()}
        {failed && job?.log?.length ? (
          <pre className="max-h-20 overflow-auto rounded-lg border border-white/10 bg-black/30 p-2 font-mono text-[10px] leading-relaxed text-muted-foreground">
            {job.log.slice(-4).join("\n")}
          </pre>
        ) : null}

        {ready ? (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              className="cursor-pointer rounded-xl"
              onClick={() => restart(status.channel)}
            >
              <RefreshCw size={13} className="mr-1" />
              Restart Eclipse
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="cursor-pointer rounded-xl"
              onClick={() => window.location.reload()}
            >
              Reload now
            </Button>
          </div>
        ) : running ? (
          <p className="flex items-center gap-1.5 text-[10px] text-muted-foreground/80">
            <ShieldCheck size={11} className="shrink-0" />
            Signed checksum verified before anything is installed. Keep Eclipse open.
          </p>
        ) : showCommand ? (
          <div className="space-y-2">
            <p className="text-[11px] text-muted-foreground">
              Prefer a terminal? Stop Eclipse and run this in the project folder:
            </p>
            <div className="flex items-center gap-2">
              <code className="flex-1 rounded-lg border border-white/10 bg-black/30 px-2.5 py-1.5 font-mono text-[11px]">
                black update
              </code>
              <Button
                size="sm"
                variant="outline"
                className="cursor-pointer rounded-lg"
                onClick={() => void copyCommand()}
              >
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {manual ? (
              downloadHref && (
                <a
                  {...externalLinkProps(downloadHref)}
                  className="inline-flex items-center gap-1 rounded-xl bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                >
                  <Download size={13} />
                  Download v{status.latestVersion}
                </a>
              )
            ) : (
              <Button
                size="sm"
                className="cursor-pointer rounded-xl"
                onClick={() => void apply(status.channel, status.currentVersion)}
              >
                <Download size={13} className="mr-1" />
                {failed ? "Try again" : "Update now"}
              </Button>
            )}
            {/* A refused in-app update (older shell, packaged install) must not be
                a dead end: the verified installer is one click away. */}
            {applyError && downloadHref && (
              <a
                {...externalLinkProps(downloadHref)}
                className="inline-flex items-center gap-1 rounded-xl border border-white/10 px-2.5 py-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
              >
                <Download size={11} />
                Download v{status.latestVersion}
              </a>
            )}
            {status.notesUrl && (
              <a
                {...externalLinkProps(status.notesUrl)}
                className="inline-flex items-center gap-1 rounded-xl border border-white/10 px-2.5 py-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
              >
                What&apos;s new
                <ExternalLink size={11} />
              </a>
            )}
            <button
              type="button"
              onClick={() => setShowCommand((v) => !v)}
              className="text-[10px] text-muted-foreground/70 underline-offset-2 hover:text-muted-foreground hover:underline cursor-pointer"
            >
              {showCommand ? "Hide command" : "Use a terminal"}
            </button>
            <button
              type="button"
              onClick={skip}
              className="ml-auto text-[10px] text-muted-foreground/70 underline-offset-2 hover:text-muted-foreground hover:underline cursor-pointer"
            >
              Skip this version
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
