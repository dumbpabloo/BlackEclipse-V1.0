'use client';

// ── ActivityFeed — the tools/retrieval a turn used, as a collapsible group ───
// Sits above the assistant prose. Expanded by default: EVERY step is visible —
// the feed never traps steps behind its own scrollbar. Collapsing is the
// user's choice via the header. The header names the latest running step, so
// the current operation is readable even after the user collapses the list.
//
// Two presentation modes share this one component:
//   compact  — the floating dock (tight rows)
//   detailed — full-page chat (roomier rows)
// Both render the same `ActivityItemRow`; only density differs.

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { AlertCircle, Check, ChevronRight, Loader2 } from '@/lib/icons';
import { cn } from '@/lib/utils';
import type { ActivityItem, ResponseStatus } from '@/lib/agent/response-model';
import { categoryCounts, changedFiles, groupActivities, type ActivityCategory } from '@/lib/agent/activity-groups';
import { ActivityItemRow } from './activity-item';

export interface ActivityFeedProps {
  activities: ActivityItem[];
  /** The live turn status — drives auto-expand and the header state. */
  status?: ResponseStatus;
  /** Tighter styling for the floating dock. */
  compact?: boolean;
  /**
   * `true` (default) auto-expands while the run is live and collapses when it
   * ends. `false` keeps it collapsed unless the user opens it.
   */
  autoExpand?: boolean;
}

/** Distinct sources/providers consulted, for the collapsed summary. */
function sourceCount(items: ActivityItem[]): number {
  const set = new Set(items.filter((a) => a.kind === 'retrieval').map((a) => a.name));
  return set.size;
}

export function ActivityFeed({ activities, status, compact = false, autoExpand = true }: ActivityFeedProps) {
  const [open, setOpen] = useState(true);
  const userToggled = useRef(false);
  const feedId = useId();

  const working = status === 'queued' || status === 'planning' || status === 'tool_running' || status === 'streaming';
  const failures = useMemo(() => activities.filter((a) => a.status === 'failed').length, [activities]);
  const sources = useMemo(() => sourceCount(activities), [activities]);
  // The step the agent is on right now (or the last one it finished). Named in
  // the header so the newest step stays visible even when the list is collapsed.
  const latest = useMemo(() => {
    for (let i = activities.length - 1; i >= 0; i -= 1) {
      if (activities[i].status === 'running') return activities[i];
    }
    return activities[activities.length - 1];
  }, [activities]);
  const groups = useMemo(() => groupActivities(activities), [activities]);
  const counts = useMemo(() => categoryCounts(activities), [activities]);
  const filesChanged = useMemo(() => changedFiles(activities), [activities]);

  // Expanded by default (the steps stay visible). While the agent works we keep
  // it open; a user who collapses it keeps that choice for the turn.
  useEffect(() => {
    if (!autoExpand || userToggled.current) return;
    if (working && activities.length > 0) setOpen(true);
  }, [working, activities.length, autoExpand]);

  // No tool activity means there is nothing to show; the capability card that
  // used to fill this branch was removed from the transcript as noise.
  if (activities.length === 0) return null;

  const toggle = () => {
    userToggled.current = true;
    setOpen((v) => !v);
  };

  const summary = (() => {
    const parts: string[] = [`${activities.length} step${activities.length === 1 ? '' : 's'}`];
    if (sources > 0) parts.push(`${sources} source${sources === 1 ? '' : 's'}`);
    if (filesChanged.length > 0) parts.push(`${filesChanged.length} file${filesChanged.length === 1 ? '' : 's'}`);
    if (failures > 0) parts.push(`${failures} failed`);
    return parts.join(' · ');
  })();

  const CATEGORY_LABEL: Record<ActivityCategory, string> = {
    context: 'Context',
    read: 'Reading',
    file: 'Changes',
    events: 'Running',
    subagent: 'Delegated',
    other: 'Steps',
  };
  const countOf = (category: ActivityCategory) =>
    counts.find((entry) => entry.category === category)?.count ?? 0;

  return (
    <div className={cn('w-full', compact ? 'mb-2 text-[11px]' : 'mb-3')}>
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-controls={feedId}
        className="flex w-full items-center gap-2 px-0.5 py-1 text-left transition-colors cursor-pointer"
      >
        <span className={cn('shrink-0', working ? 'text-primary' : failures > 0 ? 'text-amber-400' : 'text-muted-foreground/70')}>
          {working ? <Loader2 size={compact ? 11 : 13} className="animate-spin" />
            : failures > 0 ? <AlertCircle size={compact ? 11 : 13} />
            : <Check size={compact ? 11 : 13} />}
        </span>
        <span className={cn('min-w-0 flex-1 truncate', compact ? 'text-[11px]' : 'text-[12px]')}>
          <span className="font-medium text-foreground">{working ? 'Working' : 'Activity'}</span>
          {working && latest && (
            <span className="ml-1.5 text-foreground/80">
              {latest.label}{latest.detail ? ` · ${latest.detail}` : ''}
            </span>
          )}
          <span className="ml-1.5 text-muted-foreground">{summary}</span>
        </span>
        <ChevronRight
          size={compact ? 12 : 14}
          className={cn('shrink-0 text-muted-foreground/50 transition-transform', open && 'rotate-90')}
        />
      </button>

      {open && (
        <div
          id={feedId}
          className={cn(compact ? 'space-y-0.5' : 'space-y-1')}
        >
          {filesChanged.length > 0 && (
            <div className="mb-1 flex flex-wrap items-center gap-1 rounded-md border border-border/30 bg-background/30 px-1.5 py-1">
              <span className="text-[9.5px] uppercase tracking-wide text-muted-foreground/60">
                Changed this turn
              </span>
              {filesChanged.slice(0, 8).map((path) => (
                <span
                  key={path}
                  title={path}
                  className="max-w-[13rem] truncate rounded border border-border/30 px-1 py-0.5 font-mono text-[10px] text-foreground/80"
                >
                  {path.slice(path.lastIndexOf('/') + 1)}
                </span>
              ))}
              {filesChanged.length > 8 && (
                <span className="text-[10px] text-muted-foreground/60">+{filesChanged.length - 8} more</span>
              )}
            </div>
          )}
          {groups.map((group, groupIndex) => (
            <div key={`${group.category}:${groupIndex}`} className={cn(groupIndex > 0 && 'pt-0.5')}>
              {groups.length > 1 && (
                <div className="flex items-center gap-1.5 px-0.5 pb-0.5 text-[9.5px] uppercase tracking-wide text-muted-foreground/55">
                  <span>{CATEGORY_LABEL[group.category]}</span>
                  <span className="text-muted-foreground/40">{countOf(group.category)}</span>
                </div>
              )}
              {group.items.map((item) => (
                <ActivityItemRow
                  key={item.id}
                  item={item}
                  compact={compact}
                  latest={working && latest?.id === item.id}
                />
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
