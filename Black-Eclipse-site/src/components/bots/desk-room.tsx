'use client';

// -- Desk room ----------------------------------------------------------------
// The selected desk: its stages, its last run, and one honest button. A desk is
// a pipeline, not a correspondent, so this replaces the agent chat for a
// `run_team` bot. The stage timeline resolves from the automation execution's
// run id (`/team/runs/by-root/<id>`); the memo is the execution's own output.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Loader2, Pencil, Play, Users, X } from '@/lib/icons';
import { Button } from '@/components/ui/button';
import {
  getAutomationExecution,
  listAutomationHistory,
  runAutomationNow,
  type AutomationView,
  type ExecutionView,
} from '@/lib/harness/clients/automation-browser';
import { cancelTeam, getTeamRunByRoot, type TeamStatusView } from '@/lib/harness/clients/team-client';
import { deskTeamOf } from '@/lib/bots/desk';
import { TradeApprovalBanner } from '@/components/chat/trade-approval-banner';

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'timed_out', 'skipped', 'blocked']);

function tone(status: string | undefined): string {
  switch (status) {
    case 'COMPLETED':
    case 'completed':
      return 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200';
    case 'RUNNING':
    case 'IN_PROGRESS':
    case 'running':
      return 'border-sky-400/40 bg-sky-400/10 text-sky-200';
    case 'BLOCKED':
    case 'FAILED':
    case 'failed':
      return 'border-rose-400/40 bg-rose-400/10 text-rose-200';
    default:
      return 'border-border/20 bg-white/5 text-muted-foreground';
  }
}

function when(iso: string | null | undefined): string {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  return new Date(ms).toLocaleString();
}

export function DeskRoom({
  bot,
  onClose: _onClose,
  onEdit,
  onChanged,
}: {
  bot: AutomationView;
  onClose: () => void;
  onEdit: (bot: AutomationView) => void;
  onChanged: () => void;
}) {
  const team = useMemo(() => deskTeamOf(bot), [bot]);
  const [latest, setLatest] = useState<ExecutionView | null>(null);
  const [status, setStatus] = useState<TeamStatusView | null>(null);
  const [history, setHistory] = useState<ExecutionView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const stageLabels = useMemo(() => {
    const labels: Record<string, string> = {};
    for (const subtask of team?.subtasks ?? []) {
      const member = team?.members.find((m) => m.agentId === subtask.assignedAgentId);
      labels[subtask.id] = member?.label?.trim() || subtask.id;
    }
    return labels;
  }, [team]);

  const load = useCallback(async () => {
    try {
      const executions = await listAutomationHistory(bot.id);
      setHistory(executions);
      setLatest(executions[0] ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not read the desk history.');
    }
  }, [bot.id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Follow one execution until it is terminal, then its stage timeline. The team
  // run appears a moment after the execution starts, so a 404 is expected.
  useEffect(() => {
    if (!latest) return;
    let cancelled = false;
    const runId = latest.runId;
    const terminal = TERMINAL.has(latest.state);

    const fetchStatus = async () => {
      if (!runId) return;
      try {
        const next = await getTeamRunByRoot(runId);
        if (!cancelled) setStatus(next);
      } catch {
        /* the team run is not durable yet */
      }
    };
    void fetchStatus();
    if (terminal) {
      return () => {
        cancelled = true;
      };
    }

    const timer = setInterval(async () => {
      try {
        const fresh = await getAutomationExecution(latest.id);
        if (!cancelled) setLatest(fresh);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Lost contact with the run.');
      }
      await fetchStatus();
    }, 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [latest?.id, latest?.state, latest?.runId]);

  const runNow = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const { executionId } = await runAutomationNow(bot.id);
      const execution = await getAutomationExecution(executionId);
      setLatest(execution);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the desk.');
    } finally {
      setBusy(false);
    }
  }, [bot.id, busy, onChanged]);

  const cancel = useCallback(async () => {
    const teamRunId = status?.team.id;
    if (!teamRunId || busy) return;
    setBusy(true);
    setError(null);
    try {
      await cancelTeam(teamRunId);
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not cancel the desk run.');
    } finally {
      setBusy(false);
    }
  }, [status?.team.id, busy, load, onChanged]);

  const running = latest ? !TERMINAL.has(latest.state) : false;
  const stageStatus = useMemo(() => {
    const map: Record<string, string> = {};
    for (const subtask of status?.subtasks ?? []) map[subtask.subtaskId] = subtask.status;
    return map;
  }, [status]);

  return (
    <div className="glass-card flex h-full min-h-0 flex-col rounded-2xl">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/10 p-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Users size={16} className="text-muted-foreground" />
            <h2 className="truncate text-sm font-semibold">{bot.name}</h2>
            <span className={`rounded-md border px-1.5 py-0.5 text-[10px] uppercase ${tone(latest?.state)}`}>
              {latest?.state ?? 'no runs'}
            </span>
          </div>
          <p className="mt-1 max-w-xl text-xs text-muted-foreground">
            {bot.description ||
              'A desk: one question, several bots, every stage a durable task. It proposes; a trading bot executes.'}
          </p>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {team ? `${team.members.length} roles · ${team.subtasks.length} stages` : 'Definition unavailable'}
            {bot.nextRunAt ? ` · next ${when(bot.nextRunAt)}` : ' · manual'}
            {latest ? ` · last ${when(latest.startedAt)}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => onEdit(bot)}>
            <Pencil size={13} /> Edit
          </Button>
          <Button size="sm" onClick={() => void runNow()} disabled={busy || running}>
            {busy || running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
            {running ? 'Running…' : 'Run now'}
          </Button>
          {running && status?.team.id && (
            <Button size="sm" variant="ghost" onClick={() => void cancel()} disabled={busy}>
              <X size={13} /> Cancel
            </Button>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {/* A proposal stage creates a trade approval; the same self-polling banner
            the bot composer mounts means the operator can confirm it HERE, through
            the one submit route. A desk never sends an order itself. */}
        <TradeApprovalBanner />
        {error && (
          <p className="mb-3 rounded-xl border border-rose-400/20 bg-rose-400/5 p-2 text-xs text-rose-200">
            {error}
          </p>
        )}

        <div className="space-y-1.5">
          {(team?.subtasks ?? []).map((subtask) => {
            const state = stageStatus[subtask.id];
            return (
              <div
                key={subtask.id}
                className="flex items-center justify-between gap-3 rounded-xl border border-border/10 bg-white/[0.02] px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium">{stageLabels[subtask.id]}</div>
                  <div className="truncate text-[11px] text-muted-foreground">
                    {subtask.dependsOn?.length
                      ? `after ${subtask.dependsOn.map((d) => stageLabels[d] ?? d).join(', ')}`
                      : 'starts the desk'}
                    {subtask.review ? ' · reviewed' : ''}
                  </div>
                </div>
                <span className={`shrink-0 rounded-md border px-1.5 py-0.5 text-[10px] uppercase ${tone(state)}`}>
                  {state ?? 'pending'}
                </span>
              </div>
            );
          })}
          {!team && <p className="text-xs text-muted-foreground">This desk has no readable definition.</p>}
        </div>

        {latest?.errorClass && (
          <p className="mt-3 rounded-xl border border-rose-400/20 bg-rose-400/5 p-2 text-xs text-rose-200">
            {latest.errorClass}: {latest.outcomeReason ?? 'the run failed'}
          </p>
        )}

        {latest?.output ? (
          <div className="mt-4">
            <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Result
            </div>
            <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-xl border border-border/10 bg-white/[0.02] p-3 text-xs leading-relaxed text-foreground/90">
              {latest.output}
            </pre>
          </div>
        ) : (
          <p className="mt-4 text-xs text-muted-foreground">
            {running
              ? 'Stages are running. Their memos appear here when the desk finishes.'
              : 'No result yet. Run now to put the desk to work.'}
          </p>
        )}

        {history.length > 1 && (
          <div className="mt-5">
            <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              History
            </div>
            <div className="space-y-1">
              {history.slice(0, 8).map((execution) => (
                <button
                  key={execution.id}
                  type="button"
                  onClick={() => {
                    setStatus(null);
                    setLatest(execution);
                  }}
                  className="flex w-full items-center justify-between gap-3 rounded-lg px-2 py-1.5 text-left text-[11px] hover:bg-white/5"
                >
                  <span className="truncate text-muted-foreground">{when(execution.startedAt)}</span>
                  <span className={`rounded-md border px-1.5 py-0.5 uppercase ${tone(execution.state)}`}>
                    {execution.state}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
