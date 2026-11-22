'use client';

// ── Task detail (Phase 5) ───────────────────────────────────────────────────
// The run: a plain progress line, the change in everyday words, the proof it
// works, and the audit timeline. The mechanism never leads.

import { useCallback, useState } from 'react';
import { useT } from '@/lib/i18n/use-t';
import type { TranslationKey } from '@/lib/i18n/types';
import type { CodeSession, CodeTask } from '@/lib/harness/clients/code-browser';
import { getCodeTaskImage } from '@/lib/harness/clients/code-browser';
import { navigateTo } from '@/lib/sdk/navigate';
import { buildReviewPrompt, removeComment, upsertComment, type ReviewComment } from '@/lib/code/review-comments';
import { CheckResults } from './check-results';
import { CodePlanPanel } from './plan-panel';
import { DiffViewer } from './diff-viewer';
import { LandChecks } from './land-checks';
import { PlanReview } from './plan-review';
import { WorktreeActions } from './worktree-actions';
import { PromoteChange } from '@/components/chat/promote-change';

export interface TaskDetailProps {
  task: CodeTask;
  session?: CodeSession | null;
  diffFiles?: string[];
  diffUnified?: string;
  diffDigest?: string | null;
  diffAdded?: string[];
  diffModified?: string[];
  diffDeleted?: string[];
  diffMode?: 'working' | 'committed' | 'revision' | 'turn';
  onDiffMode?: (mode: 'working' | 'committed' | 'revision' | 'turn') => void;
  diffBase?: string;
  onDiffBase?: (base: string) => void;
  diffNote?: string;
  diffRoot?: string;
  onAcceptPlan?: () => void;
  onRejectPlan?: () => void;
  onResume?: () => void;
  onCancel?: () => void;
  onDiscard?: () => void;
  onChecked?: () => void;
}

export function TaskDetail({
  task, session, diffFiles = [], diffUnified, diffDigest,
  diffAdded, diffModified, diffDeleted,
  diffMode, onDiffMode, diffBase, onDiffBase, diffNote,
  diffRoot,
  onAcceptPlan, onRejectPlan, onResume, onCancel, onDiscard, onChecked,
}: TaskDetailProps) {
  const t = useT();
  const [note, setNote] = useState('');
  const [notePath, setNotePath] = useState('');
  const [noteLine, setNoteLine] = useState('');
  const [comments, setComments] = useState<ReviewComment[]>([]);
  const loadImage = useCallback(
    async (path: string, side: 'before' | 'after') => {
      const image = await getCodeTaskImage(task.id, path, side);
      return image ? { mime: image.mime, bytes: image.bytes } : null;
    },
    [task.id],
  );
  const landReady = task.status === 'completed'
    && session?.land?.ok === true && session.land.full === true;
  const phaseKey = session ? (`code.session.phase.${session.phase}` as TranslationKey) : null;
  const nextKey = session ? (`code.session.next.${session.nextAction}` as TranslationKey) : null;
  const progressKey = task.status === 'failed' ? 'code.task.resultFailed'
    : task.status === 'completed' ? 'code.task.resultDone'
    : 'code.task.working';
  return (
    <div className="space-y-2.5">
      <div className="glass-card rounded-xl border border-border/40 p-3 text-[12px]">
        <div className="text-foreground">{task.query}</div>
        <div className="mt-1 text-muted-foreground">{t(progressKey)}</div>
      </div>
      <CodePlanPanel taskId={task.id} />
      {session && (
        <div className="glass-card-subtle rounded-xl border border-border/40 p-3 text-[12px]">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-medium text-foreground">{t('code.session.title')}</div>
            <div className="text-muted-foreground">
              {t('code.session.budgetValue', {
                steps: String(session.budget.maxSteps ?? '-'),
                minutes: String(Math.round((session.budget.runTimeoutSeconds ?? 0) / 60)),
              })}
            </div>
          </div>
          {phaseKey && (
            <div className="mt-1 text-muted-foreground">
              {t(phaseKey)}{nextKey ? ` · ${t(nextKey)}` : ''}
            </div>
          )}
          {session.acceptance.length > 0 && (
            <>
              <div className="mt-2 text-muted-foreground">{t('code.session.acceptance')}</div>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-foreground/90">
                {session.acceptance.map((item) => <li key={item}>{item}</li>)}
              </ul>
            </>
          )}
          {!session.resume.resumable && (
            <div className="mt-2 text-muted-foreground">{t('code.session.finished')}</div>
          )}
        </div>
      )}
      {task.status === 'plan_review' && task.plan && (
        <PlanReview plan={task.plan} onAccept={onAcceptPlan} onReject={onRejectPlan} />
      )}
      {diffFiles.length > 0 && (
        <DiffViewer
          files={diffFiles}
          unified={diffUnified}
          digest={diffDigest}
          added={diffAdded}
          modified={diffModified}
          deleted={diffDeleted}
          comments={comments}
          onAddComment={(comment) => setComments((current) => upsertComment(current, comment))}
          onRemoveComment={(comment) => setComments((current) => removeComment(current, comment))}
          loadImage={loadImage}
          mode={diffMode}
          onModeChange={onDiffMode}
          revision={diffBase}
          onRevisionChange={onDiffBase}
          note={diffNote}
          workspaceRoot={diffRoot}
        />
      )}
      <CheckResults verification={task.verification} />
      {task.autonomy !== 'plan_only' && (
        <PromoteChange
          runId={task.id}
          disabled={Boolean(session) && !landReady}
          disabledHint={t('code.promote.gate')}
        />
      )}
      <LandChecks taskId={task.id} onVerified={() => onChecked?.()} />
      <div className="glass-card-subtle rounded-xl border border-border/40 p-3 text-[12px]">
        <div className="font-medium text-foreground">{t('code.review.title')}</div>
        <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
          <input
            value={notePath}
            onChange={(e) => setNotePath(e.target.value)}
            placeholder={t('code.review.file')}
            className="rounded-lg border border-border/40 bg-background/40 px-2 py-1 font-mono text-[11px] text-foreground"
          />
          <input
            value={noteLine}
            onChange={(e) => setNoteLine(e.target.value)}
            inputMode="numeric"
            placeholder={t('code.review.line')}
            className="rounded-lg border border-border/40 bg-background/40 px-2 py-1 font-mono text-[11px] text-foreground"
          />
        </div>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t('code.review.placeholder')}
          rows={2}
          className="mt-1.5 w-full resize-y rounded-lg border border-border/40 bg-background/40 px-2 py-1 text-[11px] text-foreground"
        />
        <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[10.5px] text-muted-foreground">
            {comments.length > 0
              ? t('code.review.anchoredCount', { count: comments.length })
              : t('code.review.hint')}
          </span>
          <button
            type="button"
            disabled={!note.trim() && comments.length === 0}
            onClick={() => {
              const goal = session?.goal || task.query;
              const manual: ReviewComment[] = notePath.trim()
                ? [{
                    path: notePath.trim(),
                    line: Number(noteLine.trim()) || 1,
                    side: 'new',
                    text: note.trim() || t('code.review.seeLine'),
                  }]
                : [];
              const prompt = buildReviewPrompt({
                goal,
                comments: [...comments, ...manual],
                note: manual.length > 0 ? undefined : note,
              });
              navigateTo({ tab: 'chat', params: { prompt } });
              setNote('');
              setComments([]);
            }}
            className="glass-button rounded-lg px-2.5 py-1 text-[11px] text-foreground disabled:opacity-50"
          >
            {t('code.review.send')}
          </button>
        </div>
      </div>
      <WorktreeActions autonomy={task.autonomy} onResume={onResume} onCancel={onCancel} onDiscard={onDiscard} />
    </div>
  );
}
