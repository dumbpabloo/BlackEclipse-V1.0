'use client';

// ── Promote a made change (Phase 15) ─────────────────────────────────────────
// A chat or code turn edits a disposable copy. This control lets the USER put the
// change into their real project: it previews exactly which files would change,
// then applies them only after an explicit confirmation. The runtime records a
// single-use decision bound to the previewed set; this component never decides.
//
// Every string is an i18n key under `code.promote.*` and the mechanism words
// (patch/diff/repo/commit/branch) never appear.

import { useState } from 'react';
import { Check, X } from '@/lib/icons';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n/use-t';
import type { TranslationKey } from '@/lib/i18n/types';
import { previewPromotion, applyPromotion, autolandTask, rollbackPromotion, restartHarnessRuntime, postLandingProbe } from '@/lib/harness/clients/code-browser';
import type { PromotionPreview, PromotionLanding } from '@/lib/harness/clients/code-browser';

type Phase = 'idle' | 'loading' | 'preview' | 'applying' | 'done' | 'rolling_back' | 'rolled_back' | 'error';

const MAX_SHOWN_FILES = 12;

function opKey(op: PromotionPreview['files'][number]['op']): TranslationKey {
  if (op === 'create') return 'code.promote.fileNew';
  if (op === 'delete') return 'code.promote.fileRemoved';
  return 'code.promote.fileEdited';
}

function errorKey(message: string): TranslationKey {
  const code = message.toUpperCase();
  if (code.includes('BASE_CHANGED')) return 'code.promote.error.base';
  if (code.includes('PREVIEW_STALE')) return 'code.promote.error.stale';
  if (code.includes('OUT_OF_SCOPE')) return 'code.promote.error.scope';
  if (code.includes('NO_CHANGE')) return 'code.promote.empty';
  return 'code.promote.error.generic';
}

export interface PromoteChangeProps {
  /** The run whose prepared change is offered. */
  runId: string;
  compact?: boolean;
  /** Phase 22 / S5: the Code screen can gate landing on a full check pass. */
  disabled?: boolean;
  disabledHint?: string;
}

export function PromoteChange({ runId, compact = false, disabled = false, disabledHint }: PromoteChangeProps) {
  const t = useT();
  const [phase, setPhase] = useState<Phase>('idle');
  const [preview, setPreview] = useState<PromotionPreview | null>(null);
  const [allFiles, setAllFiles] = useState<PromotionPreview['files']>([]);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [excludedHunks, setExcludedHunks] = useState<Set<string>>(new Set());
  const [approvalId, setApprovalId] = useState<string | null>(null);
  const [rollbackId, setRollbackId] = useState<string | null>(null);
  const [landing, setLanding] = useState<PromotionLanding | null>(null);
  const [restart, setRestart] = useState<'idle' | 'working' | 'done' | 'error'>('idle');
  const [postLanding, setPostLanding] = useState<'idle' | 'checking' | 'healthy' | 'rolled_back' | 'unverified' | 'error'>('idle');
  const [messageKey, setMessageKey] = useState<TranslationKey>('code.promote.error.generic');

  const hunkToken = (path: string, id: string) => `${path}#${id}`;

  function selectedHunks(files: PromotionPreview['files'], skip: Set<string>): string[] {
    const out: string[] = [];
    for (const file of files) {
      for (const hunk of file.hunks ?? []) {
        const token = hunkToken(file.path, hunk.id);
        if (!skip.has(token)) out.push(token);
      }
    }
    return out;
  }

  async function review(includeOverride?: string[], hunksOverride?: string[]) {
    setPhase('loading');
    try {
      const sourceFiles = allFiles.length > 0 ? allFiles : preview?.files ?? [];
      const excludedPaths = includeOverride === undefined
        ? [...excluded]
        : sourceFiles.filter((f) => !includeOverride.includes(f.path)).map((f) => f.path);
      const include = includeOverride
        ?? sourceFiles.filter((f) => !excluded.has(f.path)).map((f) => f.path);
      const hunks = hunksOverride ?? selectedHunks(sourceFiles, excludedHunks);
      const { preview: result, approval, landing: verdict } = await previewPromotion(runId, include, hunks);
      if (!result || result.filesChanged === 0 || !approval?.id) {
        setMessageKey('code.promote.empty');
        setPhase('error');
        return;
      }
      if (allFiles.length === 0) setAllFiles(result.files);
      setExcluded(new Set(excludedPaths));
      setPreview(result);
      setApprovalId(approval.id);
      setLanding(verdict ?? null);
      setPhase('preview');
    } catch (err) {
      setMessageKey(errorKey(err instanceof Error ? err.message : ''));
      setPhase('error');
    }
  }

  async function confirm() {
    if (!preview || !approvalId) return;
    setPhase('applying');
    try {
      const include = allFiles.filter((f) => !excluded.has(f.path)).map((f) => f.path);
      const result = await applyPromotion(
        runId, approvalId, preview.previewDigest, include, selectedHunks(allFiles, excludedHunks),
      );
      setRollbackId(result.rollbackId ?? null);
      if (result.landing) setLanding(result.landing);
      setPhase('done');
    } catch (err) {
      setMessageKey(errorKey(err instanceof Error ? err.message : ''));
      setPhase('error');
    }
  }

  async function rollback() {
    if (!rollbackId) return;
    setPhase('rolling_back');
    try {
      await rollbackPromotion(rollbackId, runId);
      setPhase('rolled_back');
    } catch (err) {
      setMessageKey(errorKey(err instanceof Error ? err.message : ''));
      setPhase('error');
    }
  }

  async function doRestart() {
    setRestart('working');
    setPostLanding('idle');
    try {
      const ok = await restartHarnessRuntime();
      if (!ok) {
        setRestart('error');
        return;
      }
      setRestart('done');
      if (!rollbackId) return;
      // The front door restarted the process; the runtime now probes its own
      // health and puts the previous files back when it did not come up.
      setPostLanding('checking');
      try {
        const record = await postLandingProbe(rollbackId, runId, true);
        if (record.status === 'healthy') setPostLanding('healthy');
        else if (record.status === 'rolled_back') setPostLanding('rolled_back');
        else setPostLanding('unverified');
      } catch {
        setPostLanding('error');
      }
    } catch {
      setRestart('error');
    }
  }

  function reset() {
    setPhase('idle');
    setPreview(null);
    setAllFiles([]);
    setExcluded(new Set());
    setExcludedHunks(new Set());
    setApprovalId(null);
    setRollbackId(null);
    setLanding(null);
    setRestart('idle');
    setPostLanding('idle');
  }

  async function quickApply() {
    setPhase('applying');
    try {
      const result = await autolandTask(runId);
      if (!result.ok) {
        setMessageKey('code.promote.error.generic');
        setPhase('error');
        return;
      }
      setRollbackId(result.rollbackId ?? null);
      setPhase('done');
    } catch (err) {
      setMessageKey(errorKey(err instanceof Error ? err.message : ''));
      setPhase('error');
    }
  }

  const size = compact ? 'text-[10.5px]' : 'text-[11.5px]';

  if (phase === 'idle') {
    if (disabled) {
      return (
        <div className={cn('glass-card-subtle mt-2 rounded-lg border border-border/40 px-3 py-1 text-muted-foreground', size)}>
          {disabledHint ?? t('code.promote.gate')}
        </div>
      );
    }
    return (
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={() => void quickApply()}
          className={cn(
            'glass-card-subtle rounded-lg border border-border/40 px-3 py-1 text-foreground hover:bg-accent',
            size,
          )}
          title="Apply now. If a check fails, the change is undone automatically."
        >
          Apply now
        </button>
        <button
          type="button"
          onClick={() => void review()}
          className={cn(
            'glass-card-subtle rounded-lg border border-border/40 px-3 py-1 text-foreground hover:bg-accent',
            size,
          )}
        >
          {t('code.promote.review')}
        </button>
      </div>
    );
  }

  if (phase === 'loading' || phase === 'applying' || phase === 'rolling_back') {
    const key: TranslationKey =
      phase === 'loading' ? 'code.promote.reviewing'
      : phase === 'rolling_back' ? 'code.promote.rollingBack'
      : 'code.promote.working';
    return (
      <div className={cn('mt-2 text-muted-foreground', size)}>
        {t(key)}
      </div>
    );
  }

  if (phase === 'rolled_back') {
    return (
      <div className={cn('glass-card-subtle mt-2 flex items-center gap-2 rounded-lg border border-border/40 px-3 py-1.5 text-foreground', size)}>
        <Check size={compact ? 11 : 12} className="shrink-0 text-primary" />
        <span>{t('code.promote.rolledBack')}</span>
      </div>
    );
  }

  if (phase === 'done') {
    return (
      <div className={cn('glass-card-subtle mt-2 rounded-lg border border-border/40 px-3 py-1.5 text-foreground', size)}>
        <div className="flex items-center gap-2">
          <Check size={compact ? 11 : 12} className="shrink-0 text-primary" />
          <span>{t('code.promote.done')}</span>
        </div>
        {landing?.restartRequired && (
          <div className="mt-1 text-muted-foreground/80">
            {t('code.promote.restartNote')}
            {(restart === 'idle' || postLanding === 'rolled_back') && (
              <button
                type="button"
                onClick={() => void doRestart()}
                className="ml-2 rounded-lg border border-border/40 px-2.5 py-0.5 text-foreground hover:bg-accent"
              >
                {t('code.promote.restartNow')}
              </button>
            )}
            {restart === 'working' && <span className="ml-2">{t('code.promote.restarting')}</span>}
            {restart === 'done' && postLanding === 'checking' && (
              <span className="ml-2">{t('code.promote.postLandingChecking')}</span>
            )}
            {restart === 'done' && postLanding === 'healthy' && (
              <span className="ml-2 text-primary">{t('code.promote.restarted')}</span>
            )}
            {restart === 'done' && postLanding === 'unverified' && (
              <span className="ml-2 text-muted-foreground">{t('code.promote.postLandingUnverified')}</span>
            )}
            {restart === 'done' && postLanding === 'rolled_back' && (
              <span className="ml-2 text-destructive">{t('code.promote.postLandingRolledBack')}</span>
            )}
            {restart === 'done' && postLanding === 'idle' && (
              <span className="ml-2 text-primary">{t('code.promote.restarted')}</span>
            )}
            {restart === 'error' && (
              <span className="ml-2 text-destructive">{t('code.promote.restartFailed')}</span>
            )}
            {postLanding === 'error' && (
              <span className="ml-2 text-destructive">{t('code.promote.error.generic')}</span>
            )}
          </div>
        )}
        {landing?.buildRequired && (
          <div className="mt-1 text-muted-foreground/80">{t('code.promote.buildNote')}</div>
        )}
        {rollbackId && (
          <button
            type="button"
            onClick={() => void rollback()}
            className="mt-1.5 rounded-lg border border-border/40 px-2.5 py-0.5 text-muted-foreground hover:bg-accent"
          >
            {t('code.promote.rollback')}
          </button>
        )}
      </div>
    );
  }

  if (phase === 'error') {
    return (
      <div className={cn('glass-card-subtle mt-2 flex items-center gap-2 rounded-lg border border-border/40 px-3 py-1.5 text-muted-foreground', size)}>
        <X size={compact ? 11 : 12} className="shrink-0 text-destructive" />
        <span className="min-w-0 flex-1">{t(messageKey)}</span>
        <button type="button" onClick={reset} className="shrink-0 underline-offset-2 hover:underline">
          {t('code.promote.review')}
        </button>
      </div>
    );
  }

  // preview
  const files = allFiles.length > 0 ? allFiles : preview?.files ?? [];
  const shown = files.slice(0, MAX_SHOWN_FILES);
  const extra = files.length - shown.length;
  const toggle = (path: string) => {
    const next = new Set(excluded);
    if (next.has(path)) next.delete(path);
    else {
      if (next.size + 1 >= files.length) return; // keep at least one file
      next.add(path);
    }
    const include = files.filter((f) => !next.has(f.path)).map((f) => f.path);
    void review(include, selectedHunks(files, excludedHunks));
  };
  const toggleHunk = (path: string, id: string) => {
    const token = hunkToken(path, id);
    const nextHunks = new Set(excludedHunks);
    if (nextHunks.has(token)) nextHunks.delete(token);
    else nextHunks.add(token);
    // A file with every part excluded is excluded; a file with any part kept is
    // included. The last kept part cannot be turned off.
    const nextFiles = new Set(excluded);
    const selected = selectedHunks(files, nextHunks);
    for (const file of files) {
      const ids = (file.hunks ?? []).map((h) => hunkToken(file.path, h.id));
      if (ids.length === 0) continue;
      const kept = ids.some((t) => selected.includes(t));
      if (kept) nextFiles.delete(file.path);
      else nextFiles.add(file.path);
    }
    if (nextFiles.size >= files.length) return;
    setExcludedHunks(nextHunks);
    setExcluded(nextFiles);
    const include = files.filter((f) => !nextFiles.has(f.path)).map((f) => f.path);
    void review(include, selected);
  };
  return (
    <div className={cn('glass-card mt-2 rounded-xl border border-border/40 p-2.5', size)}>
      <div className="text-foreground">{t('code.promote.title')}</div>
      <div className="mt-1 text-[11px] text-muted-foreground/70">{t('code.promote.includeHint')}</div>
      <ul className="mt-1 space-y-0.5 font-mono text-muted-foreground/80">
        {shown.map((f) => {
          const kept = !excluded.has(f.path);
          return (
            <li key={`${f.op}:${f.path}`} className={cn('truncate', !kept && 'opacity-40')}>
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={kept}
                  onChange={() => toggle(f.path)}
                  aria-label={f.path}
                  className="h-3 w-3 shrink-0"
                />
                <span className="truncate">{t(opKey(f.op), { name: f.path })}</span>
              </div>
              {kept && (f.hunks?.length ?? 0) > 1 && (
                <ul className="ml-5 mt-0.5 space-y-0.5 border-l border-border/30 pl-2">
                  {(f.hunks ?? []).map((hunk, index) => {
                    const token = hunkToken(f.path, hunk.id);
                    const hunkKept = !excludedHunks.has(token);
                    return (
                      <li key={hunk.id} className={cn(!hunkKept && 'opacity-40')}>
                        <label className="flex items-center gap-1.5" title={t('code.promote.hunkHint')}>
                          <input
                            type="checkbox"
                            checked={hunkKept}
                            onChange={() => toggleHunk(f.path, hunk.id)}
                            aria-label={`${f.path} ${hunk.id}`}
                            className="h-2.5 w-2.5 shrink-0"
                          />
                          <span className="shrink-0 text-muted-foreground/70">
                            {t('code.promote.hunk', { n: index + 1 })}
                          </span>
                          <span className="truncate text-muted-foreground/50">{hunk.header}</span>
                        </label>
                        {hunk.preview.length > 0 && (
                          <div className="ml-4 font-mono text-[10px] leading-tight">
                            {hunk.preview.slice(0, 4).map((line, lineIndex) => (
                              <div
                                key={lineIndex}
                                className={cn('truncate', line.kind === 'add' ? 'text-primary/80' : 'text-destructive/70')}
                              >
                                {line.kind === 'add' ? '+' : '-'}
                                {line.text}
                              </div>
                            ))}
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
        {extra > 0 && <li className="text-muted-foreground/60">{t('code.promote.more', { count: extra })}</li>}
      </ul>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void confirm()}
          className="rounded-lg bg-primary/90 px-3 py-1 text-primary-foreground hover:bg-primary"
        >
          {t('code.promote.confirm')}
        </button>
        <button
          type="button"
          onClick={reset}
          className="rounded-lg border border-border/40 px-3 py-1 text-muted-foreground hover:bg-accent"
        >
          {t('code.promote.cancel')}
        </button>
      </div>
    </div>
  );
}
