'use client';

// ── Bot scheduler dock ────────────────────────────────────────────────────────
// The right-docked column the app shows in Bots mode: the same Agent Workspace
// dock, but carrying the scheduled-jobs scheduler. Its visibility is the shared
// `useWorkspaceStore.open`, so the left-sidebar "Open scheduler" control, the
// top-bar Agent Workspace toggle and this panel are all one control. Mounted by
// `stock-chat.tsx` in place of `<AgentWorkspace />` while the Bots tab is active.

import { useState } from 'react';
import { CalendarDays, X } from '@/lib/icons';
import { useT } from '@/lib/i18n';
import { useWorkspaceStore } from '@/lib/agent-workspace/store';
import { useBotsStore } from '@/lib/bots/bots-store';
import { ScheduledJobsPanel } from '@/components/bots/scheduled-jobs-panel';
import { MonitorPanel } from '@/components/bots/monitor-panel';
import { MCPApprovalsPanel } from '@/components/screens/mcp/approvals-panel';
import { BotsTeamPanel } from '@/components/bots/bots-team-panel';

export function BotSchedulerDock() {
  const t = useT();
  const open = useWorkspaceStore((s) => s.open);
  const setOpen = useWorkspaceStore((s) => s.setOpen);
  const openCreate = useBotsStore((s) => s.openCreate);
  const select = useBotsStore((s) => s.select);
  const hydrate = useBotsStore((s) => s.hydrate);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  if (!open) return null;

  return (
    <div
      role="complementary"
      aria-label={t('bots.jobsPanel.title')}
      className="relative z-20 flex w-full shrink-0 flex-col overflow-hidden glass-card-strong md:h-full md:w-[22rem]"
    >
      <header className="flex shrink-0 items-center gap-2 px-3 py-2">
        <CalendarDays size={15} className="shrink-0 text-primary" />
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold text-foreground">
          {t('bots.jobsPanel.title')}
        </span>
        <button
          type="button"
          onClick={() => setOpen(false)}
          aria-label={t('bots.jobsPanel.collapse')}
          title={t('bots.jobsPanel.collapse')}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-white/[0.06] hover:text-foreground"
        >
          <X size={13} />
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3 pt-1">
        {/* Bot mode keeps a real approvals surface: scheduled/manual runs can
            block on an effectful tool while the operator is elsewhere. */}
        {approvalError && (
          <p className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-2 py-1.5 text-[11px] text-rose-300">
            {approvalError}
          </p>
        )}
        <MCPApprovalsPanel busy={false} onError={setApprovalError} />
        <BotsTeamPanel />
        <MonitorPanel onArmed={() => void hydrate()} />
        <ScheduledJobsPanel
          bare
          onCreate={openCreate}
          onSelectBot={(id) => select(id)}
          onCollapse={() => setOpen(false)}
        />
      </div>
    </div>
  );
}
