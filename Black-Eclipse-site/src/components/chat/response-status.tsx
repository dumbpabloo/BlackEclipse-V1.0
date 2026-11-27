'use client';

// ── ResponseStatusLine — the working indicator above an assistant turn ──────
// Replaces the single generic "Thinking…" string with a state-aware line. It
// reads the typed lifecycle (not raw events) so it never claims the agent is
// thinking while it is waiting on a tool, and never keeps spinning after a
// terminal state.

import { AlertTriangle, Check, Loader2, OctagonX, ShieldAlert } from '@/lib/icons';
import { cn } from '@/lib/utils';
import type { ActivityItem, ResponseStatus } from '@/lib/agent/response-model';

export interface ResponseStatusLineProps {
  status: ResponseStatus;
  /** Latest activity, used to name what the agent is doing right now. */
  activities?: ActivityItem[];
  /** Explicit wait/reconnect explanation, when the runtime reported one. */
  note?: string;
  /** Tighter type for the floating dock. */
  compact?: boolean;
}

/** The label shown for each lifecycle state. */
function statusLabel(status: ResponseStatus, activities: ActivityItem[]): string {
  switch (status) {
    case 'queued':
      return 'Preparing analysis';
    case 'planning':
      return 'Reviewing available data';
    case 'tool_running': {
      const running = [...activities].reverse().find((a) => a.status === 'running');
      return running?.label ?? 'Running financial calculations';
    }
    case 'streaming':
      return 'Preparing response';
    case 'awaiting_approval':
      return 'Waiting for your approval';
    case 'completed':
      return 'Done';
    case 'cancelled':
      return 'Stopped';
    case 'error':
      return 'Something went wrong';
  }
}

/**
 * Only `queued`/`planning`/`tool_running`/`streaming` are "working" states — the
 * indicator animates for those and resolves for the rest.
 */
function isWorking(status: ResponseStatus): boolean {
  return status === 'queued' || status === 'planning' || status === 'tool_running' || status === 'streaming';
}

export function ResponseStatusLine({ status, activities = [], note, compact = false }: ResponseStatusLineProps) {
  const working = isWorking(status);
  // A live wait/reconnect note outranks the generic lifecycle label, so the
  // operator can tell "waiting for provider capacity" from "preparing response".
  const label = working && note ? note : statusLabel(status, activities);

  const Icon =
    status === 'completed' ? Check
    : status === 'error' ? AlertTriangle
    : status === 'cancelled' ? OctagonX
    : status === 'awaiting_approval' ? ShieldAlert
    : Loader2;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex items-center gap-1.5 font-mono',
        compact ? 'text-[10px]' : 'text-[11px]',
        status === 'error' ? 'text-rose-400'
          : status === 'cancelled' ? 'text-muted-foreground'
          : status === 'completed' ? 'text-muted-foreground/80'
          : status === 'awaiting_approval' ? 'text-amber-300'
          : 'text-muted-foreground',
      )}
    >
      <Icon
        size={compact ? 10 : 12}
        className={cn(
          'shrink-0',
          working && 'animate-spin text-primary',
          status === 'completed' && 'text-primary',
        )}
        aria-hidden
      />
      <span>{label}</span>
    </div>
  );
}
