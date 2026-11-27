// ── Bot run completion diffing ───────────────────────────────────────────────
// Pure helper for the global bot-run watcher. Bots ARE automations; each bot's
// latest execution is readable from the batched GET /api/automations/statuses
// (`lastExecution`). This module answers the only question per poll: which bot
// runs reached a NOTIFIABLE terminal state since the last one?
//
// First sight of an automation is never a transition (it seeds the map), so
// opening the app never replays history. A run that replaces a previous one
// and is already terminal (started and finished between two polls) is
// announced; a new run still in flight is tracked and announced when it
// finishes. Cancelled/skipped runs are deliberately quiet — the operator (or
// the conflict policy) ended those, not an outcome worth an alarm.
//
// Kept framework-free and pure so it can be unit-tested without a DOM.

import { isQuietAnswer } from '@/lib/bots/quiet';

/** The subset of a bot's latest execution this module reads. */
export interface WatchedBotRun {
  automationId: string;
  automationName: string;
  /** Latest execution id, or null when the bot has never run. */
  executionId: string | null;
  state: string;
  errorClass?: string | null;
  outcomeReason?: string | null;
  /** The run's answer (bounded), when it produced one. */
  output?: string | null;
}

interface TrackedBotRun {
  executionId: string | null;
  state: string;
}

export interface BotRunNotification {
  /** Stable dedupe key: `bot-run:<automationId>:<executionId>`. */
  id: string;
  automationId: string;
  title: string;
  body: string;
  /** `completed` → true (loud cue); every other notifiable terminal → false. */
  ok: boolean;
}

/** Terminal states the operator is told about, and how they read. */
const NOTIFIABLE_TERMINAL: Record<string, { ok: boolean; label: string }> = {
  completed: { ok: true, label: 'Completed' },
  failed: { ok: false, label: 'Failed' },
  timed_out: { ok: false, label: 'Timed out' },
  blocked: { ok: false, label: 'Blocked' },
};

/** Build the tracking map from a full status sweep (used on the first poll). */
export function seedBotRuns(views: WatchedBotRun[]): Map<string, TrackedBotRun> {
  const next = new Map<string, TrackedBotRun>();
  for (const v of views) next.set(v.automationId, { executionId: v.executionId, state: v.state });
  return next;
}

export function selectBotRunTransitions(
  views: WatchedBotRun[],
  previous: Map<string, TrackedBotRun>,
): { notifications: BotRunNotification[]; next: Map<string, TrackedBotRun> } {
  const notifications: BotRunNotification[] = [];
  const next = new Map<string, TrackedBotRun>();

  for (const v of views) {
    next.set(v.automationId, { executionId: v.executionId, state: v.state });
    if (!v.executionId) continue;

    const prev = previous.get(v.automationId);
    if (!prev) continue; // first sight — seed only

    const isNewExecution = prev.executionId !== v.executionId;
    const finishedInPlace = !isNewExecution && prev.state !== v.state;
    if (!isNewExecution && !finishedInPlace) continue;

    const terminal = NOTIFIABLE_TERMINAL[v.state];
    if (!terminal) continue;
    // A quiet completion said "nothing changed". It still advances the tracked
    // run, but it must not toast, sound, or mark the bot unread.
    if (v.state === 'completed' && isQuietAnswer(v.output)) continue;

    // Prefer the run's actual answer so a completed bot shows WHAT it produced,
    // not just that it finished; fall back to the outcome/error detail.
    const detail = notificationDetail(v);
    notifications.push({
      id: `bot-run:${v.automationId}:${v.executionId}`,
      automationId: v.automationId,
      title: `Bot run ${terminal.label.toLowerCase()}`,
      body: detail ? `${v.automationName} — ${detail}` : v.automationName,
      ok: terminal.ok,
    });
  }

  return { notifications, next };
}

/** One short line for a completion notice: the answer if there is one, else the
 *  outcome/error detail. Bounded so a long answer cannot swallow the toast. */
const BODY_MAX_CHARS = 180;

function notificationDetail(v: WatchedBotRun): string {
  const answer = (v.output || '').replace(/\s+/g, ' ').trim();
  if (answer) {
    return answer.length <= BODY_MAX_CHARS ? answer : `${answer.slice(0, BODY_MAX_CHARS)}…`;
  }
  return (v.outcomeReason || v.errorClass || '').trim();
}
