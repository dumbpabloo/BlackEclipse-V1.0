'use client';

// ── Staging an update, shared by every surface that offers it ────────────────
// The banner (bottom-right) and the title-bar panel used to have separate flows;
// the banner had none at all, so its only action was "copy this command". This
// hook is the one implementation both use:
//
//   1. Prefer the shell: it spawns the runner outside the server tree, so the
//      app's own supervisor cannot kill it mid-build.
//   2. Fall back to POST /api/updates/apply when the window has no shell bridge
//      (or an older shell without the `be-update` listener).
//   3. Poll the job file while the run is in flight and surface its message,
//      phase and log verbatim. Dependencies are installed in a staging
//      directory, so a burst of failed polls no longer means "node_modules is
//      being rewritten under the server".
//
// Restarting is a separate, explicit action: the runner only STAGES.

import { useCallback, useEffect, useRef, useState } from 'react';

import { canRestartFromShell, requestUpdateApply, requestUpdateRestart } from '@/lib/desktop/update-bridge';
import type { UpdateJob } from './types';

const POLL_MS = 1_800;
const RESTART_GRACE_MS = 6_000;

export interface UseUpdateApply {
  job: UpdateJob | null;
  starting: boolean;
  error: string | null;
  running: boolean;
  /** This window can ask its shell to stage an update. */
  canApplyFromShell: boolean;
  apply: (channel: string, currentVersion?: string | null) => Promise<void>;
  restart: (channel: string) => void;
  /** Re-read the job file (e.g. when a surface is opened). */
  reload: () => Promise<void>;
  clearError: () => void;
}

export function useUpdateApply(): UseUpdateApply {
  const [job, setJob] = useState<UpdateJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restartTimer = useRef<number | null>(null);

  const canApplyFromShell = typeof window !== 'undefined' && canRestartFromShell();

  useEffect(
    () => () => {
      if (restartTimer.current) window.clearTimeout(restartTimer.current);
    },
    [],
  );

  const fetchJob = useCallback(async (): Promise<UpdateJob | null> => {
    try {
      const res = await fetch('/api/updates/apply', { cache: 'no-store' });
      if (!res.ok) return null;
      const data = (await res.json()) as { job?: UpdateJob | null };
      return data.job ?? null;
    } catch {
      return null;
    }
  }, []);

  const reload = useCallback(async () => {
    setJob(await fetchJob());
  }, [fetchJob]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const running = job?.status === 'running' || starting;

  // Poll while a run is in flight.
  useEffect(() => {
    if (!running) return;
    let alive = true;
    const tick = async () => {
      const next = await fetchJob();
      if (!alive) return;
      if (next) {
        setJob(next);
        if (next.status !== 'running') setStarting(false);
      }
    };
    const timer = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [running, fetchJob]);

  const apply = useCallback(
    async (channel: string, currentVersion?: string | null) => {
      setError(null);
      setStarting(true);
      const baseline = job?.id ?? null;

      const waitForNewJob = async (attempts: number): Promise<UpdateJob | null> => {
        for (let i = 0; i < attempts; i++) {
          await new Promise((resolve) => setTimeout(resolve, 250));
          const next = await fetchJob();
          if (next && next.id !== baseline) return next;
        }
        return null;
      };

      // Preferred: the shell spawns the runner outside the server tree.
      if (requestUpdateApply(channel, currentVersion)) {
        const staged = await waitForNewJob(16);
        if (staged) {
          setJob(staged);
          return;
        }
        // No new job appeared — an older shell without the `be-update` listener.
        // Fall through to the HTTP route instead of stranding the user.
      }

      try {
        const res = await fetch(`/api/updates/apply?channel=${encodeURIComponent(channel)}`, {
          method: 'POST',
        });
        const data = (await res.json().catch(() => ({}))) as { job?: UpdateJob | null; error?: string };
        if (!res.ok) {
          if (data.job) setJob(data.job);
          else setError(data.error || 'Could not start the update.');
          setStarting(false);
          return;
        }
        setJob(data.job ?? null);
        if (!data.job) {
          const started = await waitForNewJob(12);
          if (started) setJob(started);
          else {
            setError('The updater did not report back. Run `black update` in a terminal.');
            setStarting(false);
          }
        }
      } catch {
        setError('Could not reach the updater. Is Eclipse still running?');
        setStarting(false);
      }
    },
    [fetchJob, job?.id],
  );

  const restart = useCallback((channel: string) => {
    setError(null);
    if (!requestUpdateRestart(channel)) {
      setError('This window cannot restart itself. Quit Eclipse and open it again to use the new build.');
      return;
    }
    // An older shell has no `be-update` listener, so nothing happens. Say so
    // instead of leaving a dead button.
    if (restartTimer.current) window.clearTimeout(restartTimer.current);
    restartTimer.current = window.setTimeout(() => {
      setError('The window did not restart. Quit Eclipse and open it again to use the new build.');
    }, RESTART_GRACE_MS);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return { job, starting, error, running, canApplyFromShell, apply, restart, reload, clearError };
}
