'use client';

// ── Code tasks — the task list (Phase 5) ────────────────────────────────────
// One row per task: a plain status, a plain progress line, and the autonomy
// choice as a plain sentence. The mechanism (workspace, checks, argv) is never
// on this surface; it lives behind "details" and in the trace.
//
// Phase 14 adds two actions on each row, both using EXISTING contracts:
//   * "Build this" — re-runs the task's request through `createCodeTask`
//     (`/api/code/tasks`), the same call the separate screen has always used.
//   * "Schedule" — creates a Phase-6 job (`/api/jobs`) whose target is the
//     code-agent at `edit_verify`, created DISABLED (discovery is not
//     activation). The folder comes from the runtime's own resolved root.

import { useState } from 'react';
import { useT } from '@/lib/i18n/use-t';
import type { TranslationKey } from '@/lib/i18n/types';
import { cn } from '@/lib/utils';
import { createCodeTask, type CodeTask, type TaskStatus } from '@/lib/harness/clients/code-browser';
import { createJob } from '@/lib/harness/clients/jobs-browser';

const STATUS_KEY: Record<TaskStatus, TranslationKey> = {
  created: 'code.task.status.created',
  plan_review: 'code.task.status.planReview',
  completed: 'code.task.status.completed',
  failed: 'code.task.status.failed',
  cancelled: 'code.task.status.cancelled',
  discarded: 'code.task.status.discarded',
};

const AUTONOMY_KEY: Record<string, TranslationKey> = {
  plan_only: 'code.task.badge.planOnly',
  edit_verify: 'code.task.badge.editVerify',
  approve_external: 'code.task.badge.approveExternal',
};

export interface TaskListProps {
  tasks: CodeTask[];
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  /** Called after a "Build this" starts so the container can refresh. */
  onChanged?: () => void;
  /** The approved project new builds name; null/absent = the runtime default. */
  workspaceRootId?: string | null;
}

type RowState = { busy: 'build' | 'schedule' | null; note: string | null; error: boolean };

export function TaskList({ tasks, selectedId, onSelect, onChanged, workspaceRootId }: TaskListProps) {
  const t = useT();
  const [rows, setRows] = useState<Record<string, RowState>>({});
  const [draft, setDraft] = useState('');
  const [starting, setStarting] = useState(false);
  const [images, setImages] = useState<import('@/lib/harness/types').RunImageInput[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);

  // Name the project explicitly when one is chosen; otherwise the runtime binds
  // its own server-resolved default (the historical behaviour).
  const project = workspaceRootId ? { workspaceRootId } : {};

  const setRow = (id: string, next: RowState) =>
    setRows((prev) => ({ ...prev, [id]: next }));

  const buildThis = async (task: CodeTask) => {
    setRow(task.id, { busy: 'build', note: null, error: false });
    try {
      // The existing contract: the route resolves the model settings + binds the
      // runtime's own code root server-side.
      await createCodeTask({ query: task.query || t('code.task.title'), autonomy: 'edit_verify', modelSettings: {}, ...project });
      setRow(task.id, { busy: null, note: t('code.chat.buildStarted'), error: false });
      onChanged?.();
    } catch {
      setRow(task.id, { busy: null, note: t('code.chat.actionFailed'), error: true });
    }
  };

  const schedule = async (task: CodeTask) => {
    setRow(task.id, { busy: 'schedule', note: null, error: false });
    try {
      // A schedule needs the folder the code work happens in. The runtime owns
      // the server-resolved root; it confirms that value, never the caller's.
      const rootRes = await fetch('/api/code/root');
      const rootBody = (await rootRes.json().catch(() => ({}))) as { root?: string | null };
      const root = rootBody.root ?? '';
      if (!root) {
        setRow(task.id, { busy: null, note: t('code.chat.actionFailed'), error: true });
        return;
      }
      await createJob({
        name: task.query || t('code.task.title'),
        kind: 'manual',
        target: {
          kind: 'objective',
          objective: task.query,
          agentId: 'code-agent',
          autonomy: 'edit_verify',
          workspaceRoot: root,
        },
        delivery: { adapter: 'in_app' },
      });
      setRow(task.id, { busy: null, note: t('code.chat.scheduleStarted'), error: false });
    } catch {
      setRow(task.id, { busy: null, note: t('code.chat.actionFailed'), error: true });
    }
  };

  const attachImages = async (files: FileList | null) => {
    if (!files?.length) return;
    setImageError(null);
    const next = [...images];
    for (const file of Array.from(files)) {
      if (next.length >= 4) break;
      if (!file.type.startsWith('image/')) continue;
      if (file.size > 2 * 1024 * 1024) {
        setImageError(t('code.chat.imageTooLarge'));
        continue;
      }
      try {
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result || ''));
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(file);
        });
        next.push({ dataUrl, caption: file.name, mime: file.type });
      } catch {
        setImageError(t('code.chat.imageFailed'));
      }
    }
    setImages(next);
  };

  const startNew = async () => {
    const query = draft.trim();
    if (!query || starting) return;
    setStarting(true);
    try {
      await createCodeTask({
        query, autonomy: 'edit_verify', modelSettings: {}, ...project,
        images: images.length ? images : undefined,
      });
      setDraft('');
      setImages([]);
      onChanged?.();
    } catch {
      // Leave the draft in place so the user can retry.
    } finally {
      setStarting(false);
    }
  };

  const composer = (
    <div className="glass-card-subtle mb-1.5 rounded-xl border border-border/40 p-2">
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') void startNew(); }}
        placeholder={t('code.chat.newPlaceholder')}
        className="w-full rounded-lg bg-background/60 px-2.5 py-1.5 text-[13px] text-foreground outline-none"
      />
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <label className="glass-button cursor-pointer rounded-lg px-2.5 py-1 text-[11px] text-foreground">
          {t('code.chat.attachImage')}
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            className="hidden"
            onChange={(e) => { void attachImages(e.target.files); e.currentTarget.value = ''; }}
          />
        </label>
        <button
          type="button"
          disabled={!draft.trim() || starting}
          onClick={() => void startNew()}
          className="glass-button rounded-lg px-2.5 py-1 text-[11px] text-foreground disabled:opacity-50"
        >
          {t('code.chat.buildThis')}
        </button>
      </div>
      {images.length > 0 && (
        <ul className="mt-1 flex flex-wrap gap-1">
          {images.map((image, index) => (
            <li key={`${index}:${image.caption}`} className="flex items-center gap-1 rounded-md bg-background/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={image.dataUrl} alt={image.caption || ''} className="h-6 w-6 rounded object-cover" />
              <span className="max-w-[8rem] truncate">{image.caption}</span>
              <button
                type="button"
                onClick={() => setImages(images.filter((_, i) => i !== index))}
                className="text-muted-foreground/70 hover:text-foreground"
                aria-label={t('code.chat.removeImage')}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {imageError && <div className="mt-1 text-[10px] text-destructive">{imageError}</div>}
    </div>
  );

  if (tasks.length === 0) {
    return (
      <div className="space-y-1.5">
        {composer}
        <div className="glass-card-subtle rounded-xl p-3 text-[12px] text-muted-foreground">{t('code.task.empty')}</div>
      </div>
    );
  }
  return (
    <ul className="space-y-1.5">
      <li>{composer}</li>
      {tasks.map((task) => {
        const row = rows[task.id];
        return (
          <li key={task.id} className="glass-card-subtle rounded-xl border border-border/40 p-2.5">
            <button
              type="button"
              onClick={() => onSelect?.(task.id)}
              className={cn(
                'flex w-full items-center gap-2 rounded-lg text-left text-[12px] transition-colors',
                selectedId === task.id && 'text-primary',
              )}
            >
              <span className="min-w-0 flex-1 truncate text-foreground">{task.query || t('code.task.title')}</span>
              <span className="shrink-0 text-muted-foreground/70">
                {t(AUTONOMY_KEY[task.autonomy] ?? 'code.task.badge.planOnly')}
              </span>
              <span className="shrink-0 text-muted-foreground">{t(STATUS_KEY[task.status] ?? 'code.task.status.created')}</span>
            </button>
            <div className="mt-1.5 flex items-center gap-1.5">
              <button
                type="button"
                disabled={row?.busy != null}
                onClick={() => void buildThis(task)}
                className="glass-button rounded-lg px-2.5 py-1 text-[11px] text-foreground disabled:opacity-50"
              >
                {t('code.chat.buildThis')}
              </button>
              <button
                type="button"
                disabled={row?.busy != null}
                onClick={() => void schedule(task)}
                className="glass-button rounded-lg px-2.5 py-1 text-[11px] text-foreground disabled:opacity-50"
              >
                {t('code.chat.schedule')}
              </button>
              {row?.note && (
                <span className={cn('truncate text-[11px]', row.error ? 'text-rose-300' : 'text-muted-foreground')}>
                  {row.note}
                </span>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
