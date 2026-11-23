'use client';

// ── Title-bar update button ─────────────────────────────────────────────────
// The download affordance next to minimize/maximize/close. It is the manual face
// of the same signed-release check the banner uses, plus the one place a user can
// act from inside the app.
//
// The apply flow is two steps on purpose:
//   1. Stage  — POST /api/updates/apply runs the verified CLI updater in
//               build-only mode (signature, DB backup, checkout, npm ci, build).
//               Nothing the running window serves is touched.
//   2. Restart — the shell promotes the staged build and relaunches. This kills
//               the window and its server, so it must be requested from the
//               shell, not run by the server (see update-bridge.ts).
//
// No step is silent: every failure is shown verbatim from the updater, and a
// check that cannot run never renders as "up to date".

import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Check,
  Download,
  ExternalLink,
  Loader2,
  RefreshCw,
  ShieldCheck,
  X,
} from '@/lib/icons';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { canRestartFromShell } from '@/lib/desktop/update-bridge';
import { saveUpdatePrefs } from '@/lib/updates/prefs';
import { useUpdateApply } from '@/lib/updates/use-update-apply';
import { useUpdateStatus } from '@/lib/updates/use-update-status';
import type { UpdateJobPhase } from '@/lib/updates/types';

const PHASE_PERCENT: Record<UpdateJobPhase, number> = {
  starting: 6,
  verifying: 15,
  'backing-up': 25,
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

function copyText(text: string) {
  try {
    void navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function UpdateButton() {
  const { status, checking, prefs, refresh } = useUpdateStatus();
  // One staging implementation for the banner and this panel; see
  // `use-update-apply.ts`. It polls the job file, falls back to the HTTP route
  // when the shell has no listener, and never claims a restart happened.
  const { job, starting, error, running, apply, restart, reload } = useUpdateApply();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const shell = typeof window !== 'undefined' && canRestartFromShell();

  const latest = status?.latestVersion ?? null;
  const available = Boolean(
    status?.ok && status.available && latest && latest !== prefs.skippedVersion,
  );
  // A packaged install has no checkout, but it can still update in place: the
  // desktop shell runs the verified installer runner (download + SHA-256
  // check). Only a window with no shell bridge is limited to a download link.
  const inAppUpdate = Boolean(available && status && (status.canSelfUpdate || shell));
  const downloadOnly = Boolean(available && status && !inAppUpdate);
  const downloadHref = status?.installer?.url ?? status?.notesUrl ?? status?.downloadUrl ?? null;
  // A finished stage is only worth showing while it is still the *next* version.
  // After the restart, the running build already equals it and the job is spent.
  const ready =
    job?.status === 'ready' && Boolean(job.toVersion) && job.toVersion !== status?.currentVersion;
  const failed = job?.status === 'failed' && !ready;
  const stagedVersion = ready ? job?.toVersion ?? latest : null;
  const installerMode = job?.mode === 'installer';
  const buildMode = job?.mode === 'build';

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const skip = () => {
    if (!latest) return;
    saveUpdatePrefs({ ...prefs, skippedVersion: latest });
    setOpen(false);
  };

  const copyCommand = () => setCopied(copyText('black update'));

  const percent = job ? PHASE_PERCENT[job.phase] ?? 50 : 0;

  return (
    <div ref={rootRef} data-be-no-drag="" className="relative flex items-stretch">
      <button
        type="button"
        aria-label={available ? 'Update available' : 'Eclipse updates'}
        title={available ? `Eclipse v${latest} is available` : 'Updates'}
        aria-expanded={open}
        onClick={() => {
          setOpen((v) => !v);
          if (!open) void reload();
        }}
        className={cn(
          'relative grid w-11 place-items-center transition-colors hover:bg-white/[0.08] hover:text-foreground',
          available ? 'text-primary' : 'text-muted-foreground',
        )}
      >
        {running ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
        {available && !running && (
          <span className="absolute right-3.5 top-2.5 h-1.5 w-1.5 rounded-full bg-primary shadow-[0_0_7px_currentColor]" />
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Eclipse updates"
          className="glass-card-strong absolute right-1 top-[calc(100%+6px)] z-[80] w-[20rem] rounded-2xl border border-white/10 p-4 text-left shadow-2xl"
        >
          <div className="flex items-start gap-2.5">
            <span
              className={cn(
                'mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg',
                failed ? 'bg-red-500/15 text-red-300' : 'bg-primary/15 text-primary',
              )}
            >
              {failed ? <AlertTriangle size={14} /> : <Download size={14} />}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-semibold tracking-tight">
                {running
                  ? 'Updating Eclipse'
                  : ready
                    ? `v${stagedVersion} is ready`
                    : failed
                      ? 'Update did not finish'
                      : available
                        ? `Eclipse v${latest} is available`
                        : 'Eclipse updates'}
              </p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                {running
                  ? job?.message
                  : ready
                    ? installerMode
                      ? 'Downloaded and verified. The installer is opening — follow its steps.'
                      : buildMode
                        ? 'The signed build was downloaded and verified. Restart Eclipse — or quit and reopen — to switch.'
                        : 'Built and published. Restart Eclipse — or quit and reopen — to switch.'
                    : failed
                      ? job?.error || 'The updater stopped.'
                      : available
                        ? downloadOnly
                          ? `You're on v${status?.currentVersion}. This window cannot apply updates itself — download the signed installer below.`
                          : status?.canSelfUpdate
                            ? `You're on v${status?.currentVersion}. The release manifest was signature-checked.`
                            : `You're on v${status?.currentVersion}. Update now downloads the signed installer and verifies its SHA-256 before it runs.`
                        : checking
                          ? 'Checking for a newer signed release…'
                          : status?.error
                            ? status.error
                            : status?.ok
                              ? `You're up to date (v${status.currentVersion}).`
                              : !prefs.enabled
                                ? 'Update checks are off — enable them in Settings › General › Updates.'
                                : 'No update check has completed yet.'}
              </p>
            </div>
            <button
              type="button"
              aria-label="Close update panel"
              onClick={() => setOpen(false)}
              className="shrink-0 rounded-md p-0.5 text-muted-foreground transition-colors hover:text-foreground"
            >
              <X size={13} />
            </button>
          </div>

          {running && (
            <div className="mt-3 space-y-2">
              <div className="h-1 overflow-hidden rounded-full bg-white/10">
                <div
                  className="h-full rounded-full bg-primary transition-[width] duration-500"
                  style={{ width: `${percent}%` }}
                />
              </div>
              {job?.status === 'running' && (
                <p className="text-[10px] text-amber-200/80">
                  {buildMode
                    ? 'Verifying the build — the window may go quiet for a moment.'
                    : 'Eclipse is preparing the update — this can take a few minutes.'}
                </p>
              )}
              <p className="text-[10px] text-muted-foreground/70">
                {installerMode
                  ? 'The signed installer keeps your data directory untouched.'
                  : buildMode
                    ? 'Checking the signed checksum before anything is installed.'
                    : 'Your database was backed up first. Keep the app open.'}
              </p>
            </div>
          )}

          {ready && (
            <div className="mt-3 space-y-2">
              {error && <p className="text-[10px] text-red-300">{error}</p>}
              {installerMode ? (
                <p className="text-[10px] leading-relaxed text-muted-foreground/80">
                  Eclipse closes while the installer runs. It reopens from the installer&apos;s
                  last page when you are done.
                </p>
              ) : (
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" className="cursor-pointer rounded-xl" onClick={() => restart(status?.channel ?? 'stable')}>
                    <RefreshCw size={12} className="mr-1" />
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
              )}
              {!shell && !installerMode && (
                <p className="text-[10px] text-muted-foreground/80">
                  Closing and reopening Eclipse also picks up the new build.
                </p>
              )}
            </div>
          )}

          {!running && !ready && (
            <div className="mt-3 space-y-2">
              {error && <p className="text-[10px] text-red-300">{error}</p>}
              {failed && job?.log?.length ? (
                <pre className="max-h-24 overflow-auto rounded-lg border border-white/10 bg-black/30 p-2 font-mono text-[10px] leading-relaxed text-muted-foreground">
                  {job.log.slice(-6).join('\n')}
                </pre>
              ) : null}
              <div className="flex flex-wrap items-center gap-2">
                {downloadOnly && downloadHref && (
                  <a
                    href={downloadHref}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded-xl bg-primary px-2.5 py-1.5 text-[11px] font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                  >
                    <Download size={12} />
                    Download v{latest}
                  </a>
                )}
                {available && inAppUpdate && (
                  <Button
                    size="sm"
                    className="cursor-pointer rounded-xl"
                    disabled={checking || starting}
                    onClick={() => void apply(status?.channel ?? 'stable', status?.currentVersion)}
                  >
                    {starting ? (
                      <Loader2 size={12} className="mr-1 animate-spin" />
                    ) : (
                      <Download size={12} className="mr-1" />
                    )}
                    {failed ? 'Try again' : 'Update now'}
                  </Button>
                )}
                {!available && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="cursor-pointer rounded-xl"
                    disabled={checking}
                    onClick={() => void refresh()}
                  >
                    <RefreshCw size={12} className={cn('mr-1', checking && 'animate-spin')} />
                    Check again
                  </Button>
                )}
                {status?.notesUrl && (
                  <a
                    href={status.notesUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded-xl border border-white/10 px-2.5 py-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  >
                    What&apos;s new
                    <ExternalLink size={11} />
                  </a>
                )}
                {available && (
                  <button
                    type="button"
                    onClick={skip}
                    className="ml-auto text-[10px] text-muted-foreground/70 underline-offset-2 hover:text-muted-foreground hover:underline"
                  >
                    Skip
                  </button>
                )}
              </div>
              <div className="flex items-center justify-between gap-2 border-t border-white/5 pt-2">
                <p className="flex items-center gap-1 text-[10px] text-muted-foreground/70">
                  <ShieldCheck size={11} className="shrink-0" />
                  Signed release, database backup, rollback on failure.
                </p>
                <button
                  type="button"
                  onClick={copyCommand}
                  className="shrink-0 text-[10px] text-muted-foreground/70 underline-offset-2 hover:text-muted-foreground hover:underline"
                >
                  {copied ? (
                    <span className="inline-flex items-center gap-1">
                      <Check size={10} /> Copied
                    </span>
                  ) : (
                    'Copy command'
                  )}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
