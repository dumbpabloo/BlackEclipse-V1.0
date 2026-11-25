'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { Download, Loader2, Play, RotateCcw, X } from '@/lib/icons';
import { cn } from '@/lib/utils';
import { downloadUrl, inlineUrl, thumbnailUrl } from '@/lib/harness/clients/artifacts-browser';
import { downloadFromUrlInShell } from '@/lib/desktop/download';
import { VideoPlayer } from '@/components/media/video-player';
import { VideoApiError, cancelVideoJob, getVideoJob, renderVideoProject } from '@/lib/harness/clients/video-client';
import {
  coerceVideoBlob,
  coerceVideoJobStatus,
  videoJobLabel,
  type VideoJobRef,
  type VideoJobStatus,
} from '@/lib/harness/video-view';

/**
 * The native-video job card — the ONE renderer for video work queued in chat.
 *
 * Attached to an assistant turn whose `video_*` tools queued jobs. Polls the
 * real job records until terminal, then shows what ACTUALLY exists: a final
 * render's inline player + download once its artifact is registered, a
 * preview's image count, an inspection's findings, or the honest failure
 * (errorClass + message — a failed render is never shown as a video). The
 * actions are the real service ops: cancel a running job, regenerate a render,
 * or hand a revise instruction back to the agent.
 */
export function VideoPanel({
  video,
  compact = false,
  onFollowup,
}: {
  video: unknown;
  compact?: boolean;
  onFollowup?: (prompt: string) => void;
}) {
  const initialRefs = useMemo(() => coerceVideoBlob(video), [video]);
  const [refs, setRefs] = useState<VideoJobRef[]>(initialRefs);
  const [statuses, setStatuses] = useState<Record<string, VideoJobStatus>>({});
  const [open, setOpen] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    setRefs(initialRefs);
  }, [initialRefs]);

  // Keep a ref of the latest statuses so the polling effect depends only on
  // WHICH jobs are pending — a status update must not tear down the interval.
  const statusesRef = useRef(statuses);
  statusesRef.current = statuses;
  const pendingKey = refs
    .filter((ref) => !statusesRef.current[ref.jobId]?.terminal)
    .map((ref) => ref.jobId)
    .join(',');

  useEffect(() => {
    if (!pendingKey) return;
    const ids = pendingKey.split(',');
    let cancelled = false;
    const tick = async () => {
      for (const id of ids) {
        try {
          const job = await getVideoJob(id);
          if (cancelled) return;
          setStatuses((prev) => ({ ...prev, [id]: coerceVideoJobStatus(id, job) }));
        } catch {
          // Transient poll failure — keep trying until the job is terminal.
        }
      }
    };
    void tick();
    const timer = setInterval(() => void tick(), 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pendingKey]);

  if (refs.length === 0) return null;

  const summary = `${refs.length} job${refs.length === 1 ? '' : 's'}`;

  const handleCancel = async (ref: VideoJobRef) => {
    setBusy(ref.jobId);
    setActionError(null);
    try {
      await cancelVideoJob(ref.jobId);
      const job = await getVideoJob(ref.jobId);
      setStatuses((prev) => ({ ...prev, [ref.jobId]: coerceVideoJobStatus(ref.jobId, job) }));
    } catch (err) {
      setActionError(videoErrorText(err));
    } finally {
      setBusy(null);
    }
  };

  const handleRegenerate = async (ref: VideoJobRef) => {
    setBusy(ref.jobId);
    setActionError(null);
    try {
      const job = await renderVideoProject(ref.projectId, ref.mode ?? 'draft');
      const nextRef: VideoJobRef = {
        jobId: job.jobId,
        projectId: ref.projectId,
        projectName: ref.projectName,
        kind: 'render',
        mode: ref.mode ?? 'draft',
        ofJobId: null,
      };
      setRefs((prev) => [...prev.filter((r) => r.jobId !== nextRef.jobId), nextRef]);
      setStatuses((prev) => ({ ...prev, [nextRef.jobId]: coerceVideoJobStatus(nextRef.jobId, job) }));
    } catch (err) {
      setActionError(videoErrorText(err));
    } finally {
      setBusy(null);
    }
  };

  const handleRevise = (ref: VideoJobRef) => {
    if (!onFollowup) return;
    const name = ref.projectName ?? ref.projectId;
    onFollowup(
      `Revise the video "${name}" (project ${ref.projectId}): propose a revised storyboard for the current one and render a new draft to compare.`,
    );
  };

  return (
    <div className={cn('mt-3 w-full rounded-xl border border-border/20 glass-card-subtle', compact ? 'p-2' : 'p-2.5')}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="mb-1 flex w-full items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
      >
        <Play size={11} className="shrink-0 text-primary" />
        <span className="font-medium text-foreground/90">Video</span>
        <span className="text-muted-foreground/80">· {summary}</span>
      </button>
      {open ? (
        <div className={cn('space-y-1.5', compact && 'text-[10.5px]')}>
          {refs.map((ref) => (
            <VideoJobRow
              key={ref.jobId}
              ref_={ref}
              status={statuses[ref.jobId]}
              compact={compact}
              busy={busy === ref.jobId}
              canRevise={Boolean(onFollowup) && (ref.kind === 'render' || ref.kind === 'repair')}
              onCancel={() => void handleCancel(ref)}
              onRegenerate={() => void handleRegenerate(ref)}
              onRevise={() => handleRevise(ref)}
            />
          ))}
          {actionError ? (
            <p className="px-1 text-[11px] text-amber-500/90">{actionError}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function videoErrorText(err: unknown): string {
  if (err instanceof VideoApiError) return err.message;
  return err instanceof Error ? err.message : 'The video request failed.';
}

function VideoJobRow({
  ref_,
  status,
  compact,
  busy,
  canRevise,
  onCancel,
  onRegenerate,
  onRevise,
}: {
  ref_: VideoJobRef;
  status?: VideoJobStatus;
  compact: boolean;
  busy: boolean;
  canRevise: boolean;
  onCancel: () => void;
  onRegenerate: () => void;
  onRevise: () => void;
}) {
  const label = videoJobLabel(ref_);
  const running = !status || !status.terminal;
  const failed = Boolean(status && (status.failed || status.timedOut || status.cancelled));
  return (
    <div className={cn('rounded-lg border border-border/20 bg-black/10', compact ? 'p-1.5' : 'p-2')}>
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className="font-medium text-foreground/90">{label}</span>
        {ref_.projectName ? (
          <span className="min-w-0 truncate text-muted-foreground/80">· {ref_.projectName}</span>
        ) : null}
        <StatusChip status={status} running={running} />
      </div>

      {running ? (
        <div className="mt-1.5 flex items-center gap-2">
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-muted/30">
            <div
              className="h-full rounded-full bg-primary/70 transition-[width] duration-500"
              style={{ width: `${Math.round((status?.progress ?? 0) * 100)}%` }}
            />
          </div>
          <span className="font-mono text-[10px] tabular-nums text-muted-foreground/70">
            {Math.round((status?.progress ?? 0) * 100)}%
          </span>
        </div>
      ) : null}

      {failed ? (
        <div className="mt-1 space-y-0.5">
          <p className="break-words text-[11px] text-rose-200/80">
            {status?.error ?? 'The job did not succeed.'}
          </p>
          {status?.errorClass ? (
            <span className="inline-block rounded-full border border-rose-500/25 bg-rose-500/10 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-rose-300/80">
              {status.errorClass}
            </span>
          ) : null}
        </div>
      ) : null}

      {status?.terminal && !failed ? <SuccessDetail ref_={ref_} status={status} /> : null}
      {status?.artifactId ? (
        <VideoPlayer
          src={inlineUrl(status.artifactId)}
          poster={status.hasThumbnail ? thumbnailUrl(status.artifactId) : undefined}
          downloadHref={downloadUrl(status.artifactId)}
          fileName={status.artifactName ?? 'eclipse-video.mp4'}
          onDownload={(event) =>
            downloadFromUrlInShell(
              event,
              downloadUrl(status.artifactId!),
              status.artifactName ?? 'eclipse-video.mp4',
            )
          }
          compact
          label={status.artifactName ? `Video: ${status.artifactName}` : 'Rendered video'}
          className="mt-2"
        />
      ) : null}

      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {running ? (
          <ActionButton label="Cancel" icon={<X size={10} />} onClick={onCancel} disabled={busy} />
        ) : null}
        {status?.artifactId ? (
          <a
            href={downloadUrl(status.artifactId)}
            download
            onClick={(event) => downloadFromUrlInShell(event, downloadUrl(status.artifactId!), 'eclipse-video.mp4')}
            className="inline-flex items-center gap-1 rounded-full border border-border/40 bg-accent/40 px-2 py-0.5 text-[10.5px] text-foreground/85 transition-colors hover:bg-accent/60"
          >
            <Download size={10} /> Download
          </a>
        ) : null}
        {!running && (ref_.kind === 'render' || ref_.kind === 'repair') ? (
          <>
            <ActionButton
              label={failed ? 'Regenerate' : 'Again'}
              icon={busy ? <Loader2 size={10} className="animate-spin" /> : <RotateCcw size={10} />}
              onClick={onRegenerate}
              disabled={busy}
            />
            {canRevise ? <ActionButton label="Revise with agent" icon={null} onClick={onRevise} disabled={busy} /> : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

function SuccessDetail({ ref_, status }: { ref_: VideoJobRef; status: VideoJobStatus }) {
  if (ref_.kind === 'preview') {
    return (
      <p className="mt-1 text-[11px] text-muted-foreground/80">
        {status.previewCount ?? 0} preview image{(status.previewCount ?? 0) === 1 ? '' : 's'} registered —
        no video was rendered.
      </p>
    );
  }
  if (ref_.kind === 'inspect') {
    if (status.findingsTotal === 0) {
      return <p className="mt-1 text-[11px] text-emerald-300/80">No frame problems found.</p>;
    }
    return (
      <p className="mt-1 text-[11px] text-amber-500/90">
        {status.findingsTotal ?? 0} finding{(status.findingsTotal ?? 0) === 1 ? '' : 's'}
        {status.findingsErrors ? ` (${status.findingsErrors} error-level)` : ''}.
      </p>
    );
  }
  if (ref_.mode === 'final' && status.artifactId) {
    return (
      <p className="mt-1 text-[11px] text-muted-foreground/80">
        Rendered{status.durationSeconds != null ? ` · ${Math.round(status.durationSeconds)}s` : ''}
        {status.artifactName ? ` · ${status.artifactName}` : ''}
      </p>
    );
  }
  if (ref_.mode === 'draft') {
    return (
      <p className="mt-1 text-[11px] text-muted-foreground/80">
        Draft rendered (half-resolution, job workspace — not an Artifact Center entry).
      </p>
    );
  }
  return null;
}

function StatusChip({ status, running }: { status?: VideoJobStatus; running: boolean }) {
  const label = running ? (status?.status ?? 'QUEUED') : (status?.status ?? 'UNKNOWN');
  return (
    <span
      className={cn(
        'ml-auto shrink-0 rounded-full border px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide',
        running
          ? 'border-border/40 bg-accent/40 text-muted-foreground/80'
          : status?.failed || status?.timedOut
            ? 'border-rose-500/25 bg-rose-500/10 text-rose-300/80'
            : status?.cancelled
              ? 'border-border/40 bg-accent/40 text-muted-foreground/70'
              : 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300/80',
      )}
    >
      {label}
    </span>
  );
}

function ActionButton({
  label,
  icon,
  onClick,
  disabled,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="inline-flex items-center gap-1 rounded-full border border-border/40 bg-accent/40 px-2 py-0.5 text-[10.5px] text-foreground/85 transition-colors hover:bg-accent/60 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
    >
      {icon}
      {label}
    </button>
  );
}
