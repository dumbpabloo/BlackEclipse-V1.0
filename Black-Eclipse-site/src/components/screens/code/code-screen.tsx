'use client';

// ── Code tasks — the shell container (Phase 5 surface, mounted Phase 12.5) ────
// The seven code panels were built in Phase 5 but nothing mounted them, so no
// user could reach them. This is the thin container that wires them to the
// EXISTING code-client (which proxies the existing /code routes) and renders them
// through the shell's keep-alive tab. It holds NO agentic logic: it fetches,
// passes props, and shows what the runtime recorded — the same division the jobs
// screen uses. Every string is an i18n key.

import { useCallback, useEffect, useState } from 'react';
import { useT } from '@/lib/i18n/use-t';
import { useSubroute } from '@/hooks/use-subroute';
import { subscribeCodeRun } from '@/lib/harness/code-run-stream';
import { cn } from '@/lib/utils';
import {
  acceptCodePlan,
  cancelCodeTask,
  decideCodeApproval,
  discardCodeTask,
  getCodeScorecard,
  getCodeSession,
  getCodeTask,
  getCodeTaskDiff,
  listCodeApprovals,
  listCodeTasks,
  resumeCodeTask,
  type CodeApproval,
  type CodeScorecard,
  type CodeSession,
  type CodeTask,
  type CodeTaskDiff,
} from '@/lib/harness/clients/code-browser';
import { ScorecardStrip } from './scorecard-strip';
import { ProjectBar } from './project-bar';
import { CheckpointPanel } from './checkpoint-panel';
import { PreviewPanel } from './preview-panel';
import { TaskList } from './task-list';
import { TaskDetail } from './task-detail';
import { CodeApprovals } from './code-approvals';
import { StandingGrantsPanel } from './standing-grants';

type CodeView = 'tasks' | 'approvals';

export function CodeScreen() {
  const t = useT();
  const [tasks, setTasks] = useState<CodeTask[]>([]);
  const [approvals, setApprovals] = useState<CodeApproval[]>([]);
  const [scorecard, setScorecard] = useState<CodeScorecard | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CodeTask | null>(null);
  const [session, setSession] = useState<CodeSession | null>(null);
  const [diff, setDiff] = useState<CodeTaskDiff | null>(null);
  const [diffMode, setDiffMode] = useState<'working' | 'committed' | 'revision' | 'turn'>('working');
  const [diffBase, setDiffBase] = useState('');
  const [view, setView] = useState<CodeView>('tasks');
  const [error, setError] = useState<string | null>(null);
  const [projectRootId, setProjectRootId] = useState<string | null>(null);
  const [scope, setScope] = useState<'project' | 'all'>('project');

  // A `code.approvals` palette request brings the "waiting for you" panel forward;
  // `code.tasks` returns to the list. Both panels stay mounted either way.
  useSubroute('code', (req) => {
    setView(req.subroute.split('.')[1] === 'approvals' ? 'approvals' : 'tasks');
  });

  const refreshScorecard = useCallback(async () => {
    try {
      const target = scope === 'project' && projectRootId ? { rootId: projectRootId } : {};
      setScorecard(await getCodeScorecard(30, target));
    } catch {
      // Keep the last card rather than blanking a measured number.
    }
  }, [scope, projectRootId]);

  const refresh = useCallback(async () => {
    try {
      const [nextTasks, nextApprovals] = await Promise.all([
        listCodeTasks(), listCodeApprovals(),
      ]);
      setTasks(nextTasks);
      setApprovals(nextApprovals);
      setError(null);
    } catch {
      setError(t('code.task.loadFailed'));
    }
  }, [t]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { void refreshScorecard(); }, [refreshScorecard]);

  // Load the selected task's detail + its change (the diff is optional: a task
  // with no bound work area simply has none).
  useEffect(() => {
    if (!selectedId) { setDetail(null); setDiff(null); setSession(null); return; }
    let alive = true;
    void (async () => {
      try {
        const [task, change, nextSession] = await Promise.all([
          getCodeTask(selectedId),
          getCodeTaskDiff(
            selectedId,
            diffMode,
            diffMode === 'revision' ? diffBase.trim() || undefined : undefined,
          ),
          getCodeSession(selectedId),
        ]);
        if (alive) { setDetail(task); setDiff(change); setSession(nextSession); }
      } catch {
        if (alive) { setDetail(null); setDiff(null); setSession(null); }
      }
    })();
    return () => { alive = false; };
  }, [selectedId, tasks, diffMode, diffBase]);

  const after = useCallback(async () => { await refresh(); await refreshScorecard(); }, [refresh, refreshScorecard]);
  // Gap #7b: live progress. The task id is the harness run id, so the existing
  // SSE stream pushes step/tool/task/checkpoint/plan events; each burst is
  // coalesced into ONE refetch instead of a 4 s poll.
  useEffect(() => {
    if (!selectedId) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const bump = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => { void after(); }, 250);
    };
    const unsubscribe = subscribeCodeRun(selectedId, (type) => {
      bump();
      window.dispatchEvent(new CustomEvent('eclipse:code-run-event', {
        detail: { runId: selectedId, type },
      }));
    });
    return () => {
      unsubscribe();
      if (timer) clearTimeout(timer);
    };
  }, [selectedId, after]);
  const refreshSession = useCallback(() => {
    if (!selectedId) return;
    void getCodeSession(selectedId).then(setSession).catch(() => {});
  }, [selectedId]);

  return (
    <div className="space-y-3 p-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-[15px] font-medium text-foreground">{t('code.task.heading')}</h2>
        </div>
        <div className="flex shrink-0 flex-wrap gap-1.5">
          <button
            type="button"
            onClick={() => setView('tasks')}
            className={cn('glass-card-subtle rounded-lg px-2.5 py-1 text-[11px] transition-colors',
              view === 'tasks' ? 'border border-primary/50 text-foreground' : 'text-muted-foreground')}
          >
            {t('code.task.listHeading')}
          </button>
          <button
            type="button"
            onClick={() => setView('approvals')}
            className={cn('glass-card-subtle rounded-lg px-2.5 py-1 text-[11px] transition-colors',
              view === 'approvals' ? 'border border-primary/50 text-foreground' : 'text-muted-foreground')}
          >
            {t('code.approvals.title')}
          </button>
          <button
            type="button"
            onClick={() => { void refresh(); }}
            className="glass-button rounded-lg px-3 py-1.5 text-[12px] text-foreground"
          >
            {t('code.task.reload')}
          </button>
        </div>
      </header>

      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => setScope('project')}
          className={cn('glass-card-subtle rounded-lg px-2.5 py-1 text-[10.5px] transition-colors',
            scope === 'project' ? 'border border-primary/50 text-foreground' : 'text-muted-foreground')}
        >
          {t('code.scorecard.scope.project')}
        </button>
        <button
          type="button"
          onClick={() => setScope('all')}
          className={cn('glass-card-subtle rounded-lg px-2.5 py-1 text-[10.5px] transition-colors',
            scope === 'all' ? 'border border-primary/50 text-foreground' : 'text-muted-foreground')}
        >
          {t('code.scorecard.scope.all')}
        </button>
      </div>

      <ScorecardStrip card={scorecard} />

      <ProjectBar onChange={setProjectRootId} />

      <CheckpointPanel rootId={projectRootId} />

      <PreviewPanel taskId={selectedId} rootId={projectRootId} />

      {error && (
        <div className="glass-card-subtle rounded-xl border border-red-500/30 p-2.5 text-[12px] text-red-300">
          {error}
        </div>
      )}

      <div className={cn('grid gap-3 lg:grid-cols-2', view !== 'tasks' && 'hidden')}>
        <TaskList tasks={tasks} selectedId={selectedId} onSelect={setSelectedId} onChanged={after} workspaceRootId={projectRootId} />
        {detail ? (
          <TaskDetail
            task={detail}
            session={session}
            diffFiles={diff ? [...diff.added, ...diff.modified, ...diff.deleted] : []}
            diffUnified={diff?.unified}
            diffDigest={diff?.digest ?? null}
            diffAdded={diff?.added}
            diffModified={diff?.modified}
            diffDeleted={diff?.deleted}
            diffMode={diffMode}
            onDiffMode={setDiffMode}
            diffBase={diffBase}
            onDiffBase={setDiffBase}
            diffNote={diff && diff.available === false ? diff.reason : undefined}
            diffRoot={diff?.workspaceRoot}
            onAcceptPlan={() => {
              if (!detail.plan) return;
              void acceptCodePlan(detail.id, detail.plan.steps, detail.plan.checkClasses).then(after);
            }}
            onRejectPlan={() => setSelectedId(null)}
            onResume={() => { void resumeCodeTask(detail.id).then(after); }}
            onCancel={() => { void cancelCodeTask(detail.id).then(after); }}
            onDiscard={() => { void discardCodeTask(detail.id).then(() => { setSelectedId(null); return after(); }); }}
            onChecked={refreshSession}
          />
        ) : (
          <div className="glass-card-subtle rounded-xl p-3 text-[12px] text-muted-foreground">
            {t('code.task.detailPlaceholder')}
          </div>
        )}
      </div>

      <div className={cn(view !== 'approvals' && 'hidden', 'space-y-3')}>
        <CodeApprovals
          approvals={approvals}
          onDecide={(id, approve) => { void decideCodeApproval(id, approve).then(after); }}
        />
        <StandingGrantsPanel />
      </div>
    </div>
  );
}
