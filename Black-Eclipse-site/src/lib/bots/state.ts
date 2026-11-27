// ── Bot workspace logic (pure) ───────────────────────────────────────────────
// Everything the Bots workspace derives from server data that is NOT rendering:
// the backend→UI state vocabulary, real filters, search, human schedule
// summaries, and the draft→trigger translation used by the creation flow.
// Kept pure and dependency-free so it is unit-testable without a DOM.

import type {
  AutomationState,
  AutomationView,
  BreakerState,
  CreateAutomationInput,
  ExecutionState,
  TriggerKind,
  TriggerView,
} from '@/lib/harness/clients/automation-client';
import { draftFromTradingAction, emptyTradingDraft, type TradingDraft } from '@/lib/bots/trading/draft';

// ── Execution state vocabulary ───────────────────────────────────────────────

/** A run is finished once it reaches one of these; polling must stop. */
export const TERMINAL_EXECUTION_STATES: ReadonlySet<ExecutionState> = new Set([
  'completed', 'failed', 'cancelled', 'timed_out', 'skipped', 'blocked',
]);

/** A run is "in flight" — queued, starting, running, waiting or retrying. */
const LIVE_EXECUTION_STATES: ReadonlySet<ExecutionState> = new Set([
  'triggered', 'queued', 'running', 'waiting', 'retrying',
]);

export function isTerminalExecution(state: ExecutionState): boolean {
  return TERMINAL_EXECUTION_STATES.has(state);
}

export function isLiveExecution(state: ExecutionState): boolean {
  return LIVE_EXECUTION_STATES.has(state);
}

/** Restrained status colors — green/amber/rose/sky/muted only. */
export type Tone = 'positive' | 'warning' | 'danger' | 'info' | 'muted';

export function executionTone(state: ExecutionState): Tone {
  switch (state) {
    case 'completed':
      return 'positive';
    case 'failed':
    case 'timed_out':
      return 'danger';
    case 'blocked':
      return 'warning';
    case 'running':
    case 'retrying':
      return 'info';
    case 'waiting':
      return 'warning';
    case 'queued':
    case 'triggered':
      return 'info';
    default:
      return 'muted';
  }
}

/** A short, human label for a backend execution state. */
export function executionLabel(state: ExecutionState): string {
  const map: Record<ExecutionState, string> = {
    triggered: 'Starting',
    queued: 'Queued',
    running: 'Running',
    waiting: 'Waiting for approval',
    retrying: 'Retrying',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    timed_out: 'Timed out',
    blocked: 'Blocked',
    skipped: 'Skipped',
  };
  return map[state] ?? state;
}

export function lifecycleLabel(state: AutomationState): string {
  const map: Record<AutomationState, string> = {
    draft: 'Draft',
    validating: 'Validating',
    active: 'Active',
    paused: 'Paused',
    disabled: 'Disabled',
    archived: 'Archived',
  };
  return map[state] ?? state;
}

export function breakerLabel(state: BreakerState): string {
  const map: Record<BreakerState, string> = {
    closed: 'Healthy',
    half_open: 'Recovering',
    open: 'Paused after repeated failures',
  };
  return map[state] ?? state;
}

// ── Attention ────────────────────────────────────────────────────────────────

/** A bot needs attention when its breaker tripped or it has consecutive
 *  failures. Both are real fields on the list payload, so the filter is exact. */
export function needsAttention(bot: AutomationView): boolean {
  return bot.breaker === 'open' || bot.consecutiveFailures > 0;
}

/** The automation tag every bot carries. Monitors are bots too (tagged at
 *  creation); jobs and event rules are not, even though some of them also run
 *  an agent. The Bots workspace, @mention palette and run watcher all use this
 *  one predicate so an unrelated automation can never masquerade as a bot. */
export const BOT_TAG = 'bot';

export function isBotAutomation(
  automation: Pick<AutomationView, 'id' | 'tags'>,
): boolean {
  const tags = automation.tags ?? [];
  return (
    tags.includes(BOT_TAG) ||
    tags.includes('monitor') ||
    // Monitors created before they were tagged are still bots.
    automation.id.startsWith('mon_')
  );
}

/** A bot's pinned provider/model, or `{}` for the app default. */
export function botModelPin(bot: AutomationView): { model?: string; provider?: string } {
  const settings = bot.action.config?.modelSettings;
  if (!settings || typeof settings !== 'object') return {};
  const { model, provider } = settings as { model?: unknown; provider?: unknown };
  return {
    ...(typeof model === 'string' && model ? { model } : {}),
    ...(typeof provider === 'string' && provider ? { provider } : {}),
  };
}

/** A bot's pinned run model, or undefined for the app default. */
export function botModelOverride(bot: AutomationView): string | undefined {
  return botModelPin(bot).model;
}

// ── Filters ──────────────────────────────────────────────────────────────────

export type BotFilter = 'all' | 'active' | 'paused' | 'running' | 'attention';

export interface FilterDef {
  id: BotFilter;
  /** i18n key for the filter chip label. */
  labelKey: string;
}

export const BOT_FILTERS: readonly FilterDef[] = [
  { id: 'all', labelKey: 'bots.filter.all' },
  { id: 'active', labelKey: 'bots.filter.active' },
  { id: 'paused', labelKey: 'bots.filter.paused' },
  { id: 'running', labelKey: 'bots.filter.running' },
  { id: 'attention', labelKey: 'bots.filter.attention' },
];

/**
 * Whether a bot belongs to a filter. `running` is data-driven: the caller passes
 * the ids with a live execution (from real run records), never a guess.
 */
export function botMatchesFilter(bot: AutomationView, filter: BotFilter, runningIds: ReadonlySet<string>): boolean {
  switch (filter) {
    case 'active':
      return bot.state === 'active';
    case 'paused':
      return bot.state === 'paused';
    case 'running':
      return runningIds.has(bot.id);
    case 'attention':
      return needsAttention(bot);
    default:
      return true;
  }
}

/** Case-insensitive search over the fields a user would recognize. */
export function botMatchesSearch(bot: AutomationView, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    bot.name.toLowerCase().includes(q) ||
    (bot.description ?? '').toLowerCase().includes(q) ||
    bot.action.objective?.toLowerCase().includes(q) === true ||
    bot.tags.some((t) => t.toLowerCase().includes(q))
  );
}

// ── Triggers / schedules ─────────────────────────────────────────────────────

export function triggerKindLabel(kind: TriggerKind): string {
  const map: Record<TriggerKind, string> = {
    manual: 'On demand',
    one_time: 'One-time',
    interval: 'Repeats on an interval',
    cron: 'Recurring',
    event: 'On an event',
    condition: 'When a condition is met',
    webhook: 'On an external webhook',
  };
  return map[kind] ?? kind;
}

/** Group a trigger into the coarse "manual / one-time / recurring / event"
 *  vocabulary the UI shows. */
export function triggerClass(kind: TriggerKind): 'manual' | 'once' | 'recurring' | 'event' {
  switch (kind) {
    case 'manual':
      return 'manual';
    case 'one_time':
      return 'once';
    case 'event':
    case 'condition':
      return 'event';
    default:
      return 'recurring';
  }
}

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** Sunday is 0 in cron, but 7 is accepted as an alias; normalise so a summary
 *  never indexes past the week (`0 8 * * 7` used to render "Every undefined"). */
function normalizeDow(dow: string): string {
  return dow.split(',').map((d) => (d.trim() === '7' ? '0' : d.trim())).join(',');
}

/** A best-effort human summary of a cron expression; falls back honestly. */
export function cronSummary(expr: string): string {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return `Custom schedule (${expr})`;
  const [min, hour, dom, mon, dowRaw] = parts;
  const dow = normalizeDow(dowRaw);
  const time = /^\d+$/.test(min) && /^\d+$/.test(hour) ? `${pad2(Number(hour))}:${pad2(Number(min))}` : null;
  const at = time ? ` at ${time}` : '';

  // Sub-daily frequencies (an hour-or-minute wildcard) come first — the old
  // summary dropped the time and mislabelled `* * * * *` / `*/5 * * * *` as
  // "Every day".
  if (min === '*' && hour === '*') return 'Every minute';
  const step = /^\*\/(\d+)$/.exec(min);
  if (step && hour === '*') {
    const n = Number(step[1]);
    return `Every ${n} minute${n === 1 ? '' : 's'}`;
  }
  if (/^\d+$/.test(min) && hour === '*') return `Every hour at :${pad2(Number(min))}`;

  if (dom === '*' && mon === '*' && dow === '*') return `Every day${at}`;
  if (dom === '*' && mon === '*' && dow === '1-5') return `Every weekday${at}`;
  if (dom === '*' && mon === '*' && /^[0-6]$/.test(dow)) return `Every ${WEEKDAYS[Number(dow)]}${at}`;
  if (dom === '*' && mon === '*' && /^[0-6](,[0-6])*$/.test(dow)) {
    const names = dow.split(',').map((d) => WEEKDAYS[Number(d)]).join(', ');
    return `${names}${at}`;
  }
  if (/^\d+$/.test(dom) && mon === '*' && dow === '*') return `Monthly on day ${dom}${at}`;
  return `Custom schedule (${expr})`;
}

function intervalSummary(seconds: number): string {
  if (seconds % 86400 === 0) return `Every ${seconds / 86400} day${seconds / 86400 === 1 ? '' : 's'}`;
  if (seconds % 3600 === 0) return `Every ${seconds / 3600} hour${seconds / 3600 === 1 ? '' : 's'}`;
  if (seconds % 60 === 0) {
    const mins = Math.round(seconds / 60);
    return `Every ${mins} minute${mins === 1 ? '' : 's'}`;
  }
  return `Every ${seconds} second${seconds === 1 ? '' : 's'}`;
}

/** A single-line, plain-language schedule summary for a trigger. */
export function scheduleSummary(trigger: TriggerView): string {
  switch (trigger.type) {
    case 'interval':
      return trigger.intervalSeconds ? intervalSummary(trigger.intervalSeconds) : 'Repeats on an interval';
    case 'cron':
      return trigger.cron ? cronSummary(trigger.cron) : 'Recurring';
    case 'one_time':
      return trigger.runAt ? `Once on ${formatDateTime(trigger.runAt)}` : 'Runs once';
    case 'event':
      return `When ${trigger.eventType ?? 'an event'} occurs`;
    case 'condition':
      return 'When a condition is met';
    case 'webhook':
      return 'When an external service calls its webhook';
    default:
      return 'On demand - runs only when you start it';
  }
}

// ── Time formatting ──────────────────────────────────────────────────────────

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/** Human relative time ("in 2 hours", "5 minutes ago"). */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const delta = t - now;
  const abs = Math.abs(delta);
  const future = delta > 0;
  const units: Array<[number, string]> = [
    [86_400_000, 'day'],
    [3_600_000, 'hour'],
    [60_000, 'minute'],
  ];
  for (const [ms, unit] of units) {
    if (abs >= ms) {
      const n = Math.round(abs / ms);
      return future ? `in ${n} ${unit}${n === 1 ? '' : 's'}` : `${n} ${unit}${n === 1 ? '' : 's'} ago`;
    }
  }
  return future ? 'in a moment' : 'just now';
}

export function formatDuration(startIso: string | null | undefined, endIso: string | null | undefined): string {
  if (!startIso || !endIso) return '—';
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '—';
  const s = Math.round((end - start) / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

export function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

// ── Creation draft ───────────────────────────────────────────────────────────
// The selectable agents are NOT listed here. `useAgentChoices`
// (`src/lib/agent-presets/use-agent-choices.ts`) merges the runtime's built-ins
// with the operator's own presets, so a preset created in Settings › Agents is
// selectable everywhere. A hardcoded list here is what made user presets
// invisible in the bot dialog.

export type ScheduleChoice =
  | 'manual' | 'once' | 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'interval' | 'cron'
  | 'event' | 'condition' | 'webhook';

export interface BotDraft {
  name: string;
  objective: string;
  /**
   * What answers a chat turn: an agent (the existing loop), a saved workflow
   * (the graph's trigger receives the message as its run input), or a saved
   * quant strategy (a trading bot — deterministic decisions on a schedule).
   */
  kind: 'agent' | 'workflow' | 'strategy';
  /** Saved workflow id for a workflow bot. Empty for an agent bot. */
  workflowId: string;
  /** Trading envelope for a strategy bot (ignored by the other kinds). */
  trading: TradingDraft;
  agentId: string;
  schedule: ScheduleChoice;
  /** Event trigger preserved verbatim while an event-driven bot is edited. */
  eventType: string | null;
  eventFilter: Record<string, unknown>;
  /** Condition trigger preserved verbatim while a condition-driven bot is edited. */
  condition: TriggerView['condition'];
  /** "HH:MM" for daily/weekdays/weekly/monthly. */
  time: string;
  /** 0=Sunday..6=Saturday for weekly. */
  weekday: number;
  /** 1..28 for monthly (kept ≤28 so every month has the day). */
  dom: number;
  intervalValue: number;
  intervalUnit: 'seconds' | 'minutes' | 'hours' | 'days';
  cron: string;
  runAt: string;
  /** IANA zone the schedule is interpreted in. Preserved on edit so re-saving a
   *  bot created in another zone does not silently shift its fire times. */
  timezone: string;
  /** Optional active window carried through an event/condition edit. */
  startAt: string | null;
  endAt: string | null;
  // ── Run behaviour (engine knobs, exposed in Advanced) ──────────────────────
  /** What to do when a fire was missed while the app was closed. */
  misfire: MisfireChoice;
  /** Max catch-up launches when `misfire` is `catch_up`. */
  catchUpMax: number;
  /** What to do when a run is still going at the next fire. */
  onConflict: ConflictChoice;
  /** Max runs of this bot allowed in flight at once. */
  maxRuns: number;
  /** Max attempts per run (1 = no retry). */
  maxAttempts: number;
  /** Hard timeout for one run, in seconds. */
  maxRuntimeS: number;
  /** Optional per-bot model override for its runs. Empty = the app default. */
  modelProvider: string;
  model: string;
}

export type MisfireChoice = 'skip' | 'run_once' | 'catch_up';
export type ConflictChoice = 'skip_if_running' | 'queue_if_running' | 'cancel_previous';

export const MISFIRE_CHOICES: readonly MisfireChoice[] = ['skip', 'run_once', 'catch_up'];
export const CONFLICT_CHOICES: readonly ConflictChoice[] = ['skip_if_running', 'queue_if_running', 'cancel_previous'];

export function emptyBotDraft(): BotDraft {
  return {
    name: '',
    objective: '',
    kind: 'agent',
    workflowId: '',
    trading: emptyTradingDraft(),
    agentId: 'eclipse-agent',
    schedule: 'manual',
    eventType: null,
    eventFilter: {},
    condition: null,
    time: '08:00',
    weekday: 1,
    dom: 1,
    intervalValue: 1,
    intervalUnit: 'days',
    cron: '0 8 * * *',
    runAt: '',
    timezone: localTimezone(),
    startAt: null,
    endAt: null,
    // A fire missed while the app was closed runs once when the app returns, so
    // a scheduled bot is never silently dropped (matches the backend default).
    misfire: 'run_once',
    catchUpMax: 1,
    onConflict: 'skip_if_running',
    maxRuns: 1,
    maxAttempts: 1,
    // Matches one chat turn's 20-minute wall clock, so a scheduled bot is not
    // cut earlier than the same work in chat (`DEFAULT_CHAT_RUN_TIMEOUT_S`).
    maxRuntimeS: 1200,
    modelProvider: '',
    model: '',
  };
}

/** The action's per-run model override, or `{}` for the app default. */
export function buildModelConfig(draft: BotDraft): Record<string, unknown> {
  if (!draft.model) return {};
  const modelSettings: Record<string, unknown> = { model: draft.model };
  if (draft.modelProvider) modelSettings.provider = draft.modelProvider;
  return { modelSettings };
}

/** The engine policy objects a draft describes (execution/concurrency/retry). */
export function buildPolicies(draft: BotDraft): {
  execution: { misfire: MisfireChoice; catchUpMax: number; maxRuntimeS: number };
  concurrency: { maxConcurrentRuns: number; onConflict: ConflictChoice };
  retry: { maxAttempts: number };
} {
  return {
    execution: {
      misfire: draft.misfire,
      catchUpMax: Math.max(1, draft.catchUpMax),
      maxRuntimeS: Math.max(1, draft.maxRuntimeS),
    },
    concurrency: {
      maxConcurrentRuns: Math.max(1, draft.maxRuns),
      onConflict: draft.onConflict,
    },
    retry: { maxAttempts: Math.max(1, draft.maxAttempts) },
  };
}

function timeParts(time: string): { hour: number; minute: number } {
  const [h, m] = time.split(':').map((n) => Number(n));
  return {
    hour: Number.isFinite(h) ? h : 8,
    minute: Number.isFinite(m) ? m : 0,
  };
}

/** The interval in seconds a draft's interval fields describe (before clamping). */
export function intervalSecondsOf(draft: BotDraft): number {
  const mult = draft.intervalUnit === 'days' ? 86400
    : draft.intervalUnit === 'hours' ? 3600
    : draft.intervalUnit === 'minutes' ? 60
    : 1;
  return Math.max(1, draft.intervalValue) * mult;
}

/** The floor the backend enforces on an interval (mirrors `MIN_INTERVAL_S`). */
export const MIN_INTERVAL_SECONDS = 30;

/** Turn a draft into the trigger object the API expects. */
export function buildTrigger(draft: BotDraft): CreateAutomationInput['trigger'] {
  // Preserve the draft's zone (an edited bot keeps its original zone); fall back
  // to the browser zone only for a genuinely new draft.
  const timezone = draft.timezone || localTimezone();
  const { hour, minute } = timeParts(draft.time);
  switch (draft.schedule) {
    case 'once': {
      const at = new Date(draft.runAt);
      // A malformed/empty date must not throw a RangeError here; fall back to
      // now so the (validated) caller fails cleanly instead of crashing.
      const iso = Number.isNaN(at.getTime()) ? new Date().toISOString() : at.toISOString();
      return { type: 'one_time', runAt: iso, timezone };
    }
    case 'daily':
      return { type: 'cron', cron: `${minute} ${hour} * * *`, timezone };
    case 'weekdays':
      return { type: 'cron', cron: `${minute} ${hour} * * 1-5`, timezone };
    case 'weekly':
      return { type: 'cron', cron: `${minute} ${hour} * * ${draft.weekday}`, timezone };
    case 'monthly':
      return { type: 'cron', cron: `${minute} ${hour} ${draft.dom} * *`, timezone };
    case 'interval':
      return { type: 'interval', intervalSeconds: Math.max(MIN_INTERVAL_SECONDS, intervalSecondsOf(draft)), timezone };
    case 'cron':
      return { type: 'cron', cron: draft.cron.trim(), timezone };
    case 'event':
      return {
        type: 'event',
        eventType: draft.eventType,
        eventFilter: draft.eventFilter,
        startAt: draft.startAt,
        endAt: draft.endAt,
        timezone,
      };
    case 'condition':
      return {
        type: 'condition',
        condition: draft.condition,
        startAt: draft.startAt,
        endAt: draft.endAt,
        timezone,
      };
    case 'webhook':
      // The runtime generates and preserves the bearer token. A create from the
      // UI therefore sends no secret; an edit preserves the existing one
      // because the PATCH never includes a trigger unless the schedule changed.
      return { type: 'webhook', timezone };
    default:
      return { type: 'manual', timezone };
  }
}

export interface DraftIssue {
  field: keyof BotDraft | 'schedule';
  message: string;
}

/** Validate a draft before saving. Mirrors the backend's hard rules so the user
 *  sees the error beside the field, not as a raw API failure.
 *
 *  `knownAgentIds` is optional so existing callers/tests keep working; when a
 *  surface has the live agent list it should pass it, so a preset that was
 *  deleted/archived surfaces as a field error instead of an unknown-agent run
 *  failing later on the scheduler. */
export function validateDraft(draft: BotDraft, knownAgentIds?: readonly string[]): DraftIssue[] {
  const issues: DraftIssue[] = [];
  if (!draft.name.trim()) issues.push({ field: 'name', message: 'Give the bot a name.' });
  if (!draft.objective.trim()) issues.push({ field: 'objective', message: 'Describe what the bot should do.' });
  if (draft.kind === 'workflow' && !draft.workflowId.trim()) {
    issues.push({ field: 'workflowId', message: 'Choose the workflow this bot runs.' });
  }
  if (draft.kind === 'strategy') {
    if (!draft.trading.strategyId.trim()) {
      issues.push({ field: 'trading', message: 'Choose the strategy this bot trades.' });
    }
    if (!draft.trading.strategyVersion.trim()) {
      issues.push({ field: 'trading', message: 'Choose a strategy version.' });
    }
    if (!draft.trading.symbolsText.trim()) {
      issues.push({ field: 'trading', message: 'Add at least one symbol to trade.' });
    }
  }
  if (
    draft.kind === 'agent'
    && knownAgentIds
    && !knownAgentIds.includes(draft.agentId.trim())
  ) {
    issues.push({ field: 'agentId', message: 'Choose an agent that still exists.' });
  }
  if (draft.schedule === 'once') {
    if (!draft.runAt) issues.push({ field: 'runAt', message: 'Pick a date and time to run.' });
    else {
      const at = new Date(draft.runAt).getTime();
      if (Number.isNaN(at)) issues.push({ field: 'runAt', message: 'Choose a valid date and time.' });
      else if (at <= Date.now()) issues.push({ field: 'runAt', message: 'Choose a time in the future.' });
    }
  }
  if (draft.schedule === 'interval' && draft.intervalValue < 1) {
    issues.push({ field: 'intervalValue', message: 'The interval must be at least 1.' });
  }
  if (draft.schedule === 'cron') {
    const parts = draft.cron.trim().split(/\s+/);
    if (parts.length !== 5) issues.push({ field: 'cron', message: 'A cron schedule has five fields.' });
  }
  if (draft.schedule === 'event') {
    const name = (draft.eventType ?? '').trim();
    if (!name || !/^[a-z0-9_]+(\.[a-z0-9_]+)+$/i.test(name)) {
      issues.push({
        field: 'eventType',
        message: 'An event needs a dotted name, e.g. automation.completed.',
      });
    }
  }
  if (draft.schedule === 'condition' && !(draft.condition?.expression ?? '').trim()) {
    issues.push({ field: 'condition', message: 'Write the condition the bot waits for.' });
  }
  return issues;
}

/** The human-readable schedule line shown in the review summary. */
export function summarizeDraft(draft: BotDraft): string {
  return scheduleSummary(buildTrigger(draft) as TriggerView);
}

// ── Natural-language schedule detection ───────────────────────────────────────
// A user often writes the cadence in the bot's description ("check my portfolio
// every 5 minutes"). The creation flow used to ignore that and default every new
// bot to a MANUAL trigger, so the bot never ran on its own. `detectSchedule`
// reads the common phrasings and returns the draft patch to apply. Deterministic,
// no model call, and honest: an unrecognised phrase yields null (stay manual)
// rather than a guessed schedule.

const WEEKDAY_NAMES: ReadonlyArray<readonly string[]> = [
  ['sunday', 'sun'],
  ['monday', 'mon'],
  ['tuesday', 'tue', 'tues'],
  ['wednesday', 'wed', 'weds'],
  ['thursday', 'thu', 'thur', 'thurs'],
  ['friday', 'fri'],
  ['saturday', 'sat'],
];

export interface ScheduleDetection {
  /** The draft fields to apply (schedule + any time). */
  patch: Partial<BotDraft>;
  /** A plain-language summary of the detected schedule. */
  summary: string;
}

function parseTimeOfDay(raw: string): { hour: number; minute: number } | null {
  const mer = raw.match(/\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b/);
  if (mer) {
    let hour = Number(mer[1]);
    const minute = mer[2] ? Number(mer[2]) : 0;
    if (mer[3] === 'pm' && hour < 12) hour += 12;
    if (mer[3] === 'am' && hour === 12) hour = 0;
    if (hour <= 23 && minute <= 59) return { hour, minute };
  }
  const at = raw.match(/\bat\s+(\d{1,2})(?::(\d{2}))?\b/);
  if (at) {
    const hour = Number(at[1]);
    const minute = at[2] ? Number(at[2]) : 0;
    if (hour <= 23 && minute <= 59) return { hour, minute };
  }
  if (/\bnoon\b/.test(raw)) return { hour: 12, minute: 0 };
  if (/\bmidnight\b/.test(raw)) return { hour: 0, minute: 0 };
  if (/\bmorning\b/.test(raw)) return { hour: 8, minute: 0 };
  if (/\bafternoon\b/.test(raw)) return { hour: 14, minute: 0 };
  if (/\b(?:evening|night|tonight)\b/.test(raw)) return { hour: 18, minute: 0 };
  return null;
}

/** Detect a schedule phrase in free text and return the draft patch to apply. */
export function detectSchedule(text: string): ScheduleDetection | null {
  const raw = (text || '').toLowerCase();
  if (!raw.trim()) return null;

  const det = (patch: Partial<BotDraft>): ScheduleDetection => ({
    patch,
    summary: summarizeDraft({ ...emptyBotDraft(), ...patch }),
  });
  const at = parseTimeOfDay(raw);
  const time = at ? { time: `${pad2(at.hour)}:${pad2(at.minute)}` } : {};

  // Numbered interval: "every 5 minutes", "every 2 hours", "every 3 days".
  const num = raw.match(
    /\b(?:every|each)\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b/,
  );
  if (num) {
    const n = Math.max(1, Number(num[1]));
    const unit = num[2];
    if (/^sec/.test(unit)) return det({ schedule: 'interval', intervalValue: n, intervalUnit: 'seconds' });
    if (/^min/.test(unit)) return det({ schedule: 'interval', intervalValue: n, intervalUnit: 'minutes' });
    if (/^h/.test(unit)) return det({ schedule: 'interval', intervalValue: n, intervalUnit: 'hours' });
    if (/^week/.test(unit)) return det({ schedule: 'interval', intervalValue: n * 7, intervalUnit: 'days' });
    return det({ schedule: 'interval', intervalValue: n, intervalUnit: 'days' });
  }

  // A specific weekday: "every Monday", "each Friday at 5pm".
  for (let i = 0; i < WEEKDAY_NAMES.length; i += 1) {
    if (WEEKDAY_NAMES[i].some((name) => new RegExp(`\\b(?:every|each|on)\\s+${name}\\b`).test(raw))) {
      return det({ schedule: 'weekly', weekday: i, ...time });
    }
  }

  // Weekdays: "every weekday", "weekdays at 9".
  if (/\b(?:every\s+|each\s+)?weekdays?\b/.test(raw) || /\b(?:every|each)\s+week\s?days?\b/.test(raw)) {
    return det({ schedule: 'weekdays', ...time });
  }

  // Daily: "daily", "every day", "everyday", "once a day".
  if (/\b(?:daily|everyday|every\s+day|each\s+day|once\s+a\s+day)\b/.test(raw)) {
    return det({ schedule: 'daily', ...time });
  }

  // Hourly: "hourly", "every hour".
  if (/\b(?:hourly|every\s+hour|each\s+hour|once\s+an\s+hour)\b/.test(raw)) {
    return det({ schedule: 'interval', intervalValue: 1, intervalUnit: 'hours' });
  }

  // Every minute (no number).
  if (/\b(?:every|each)\s+minute\b/.test(raw)) {
    return det({ schedule: 'interval', intervalValue: 1, intervalUnit: 'minutes' });
  }

  // Weekly: "weekly", "every week", "once a week".
  if (/\b(?:weekly|every\s+week|each\s+week|once\s+a\s+week)\b/.test(raw)) {
    return det({ schedule: 'weekly', ...time });
  }

  // Monthly: "monthly", "every month", with an optional day ("on the 15th").
  // Clamped to 1..28 so the day exists in every month (a 31st would never fire
  // in February and would silently stall the schedule).
  if (/\b(?:monthly|every\s+month|each\s+month|once\s+a\s+month)\b/.test(raw)) {
    const dayMatch = raw.match(/\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b/)
      ?? raw.match(/\bday\s+(\d{1,2})\b/);
    const dom = dayMatch ? Math.min(28, Math.max(1, Number(dayMatch[1]))) : 1;
    return det({ schedule: 'monthly', dom, ...time });
  }

  // A time-of-day word plus a recurrence word implies a daily run
  // ("every morning", "nightly", "every evening at 6").
  if (at && /\b(?:every|each|nightly)\b/.test(raw) && /\b(?:morning|afternoon|evening|night|tonight|noon|midnight)\b/.test(raw)) {
    return det({ schedule: 'daily', ...time });
  }

  return null;
}

/** Propose a short, human name from an objective. Deterministic, no model call:
 *  strips leading verbs/filler and title-cases the first few meaningful words. */
export function suggestName(objective: string): string {
  const cleaned = objective
    .replace(/[.\n]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!cleaned) return '';
  const stop = new Set([
    'a', 'an', 'the', 'my', 'our', 'me', 'every', 'each', 'that', 'which', 'and',
    'to', 'of', 'for', 'on', 'in', 'at', 'with', 'from', 'it', 'this', 'create',
    'make', 'build', 'run', 'please', 'summarize', 'review', 'check', 'monitor',
  ]);
  const words = cleaned.split(' ').filter((w) => !stop.has(w.toLowerCase()));
  const picked = words.slice(0, 4).map((w) => w.charAt(0).toUpperCase() + w.slice(1));
  return picked.join(' ').slice(0, 60).trim();
}

/** Seed a draft from an existing bot (for editing). */
export function draftFromBot(bot: AutomationView): BotDraft {
  const base = emptyBotDraft();
  const draft: BotDraft = {
    ...base,
    name: bot.name,
    objective: bot.action.objective ?? bot.description ?? '',
    kind: bot.action.type === 'execute_workflow'
      ? 'workflow'
      : bot.action.type === 'run_strategy'
        ? 'strategy'
        : 'agent',
    workflowId: bot.action.type === 'execute_workflow'
      ? (typeof bot.action.config?.graphWorkflowId === 'string'
          ? bot.action.config.graphWorkflowId
          : bot.action.workflowId ?? '')
      : '',
    trading: bot.action.type === 'run_strategy'
      ? draftFromTradingAction(bot.action.config, bot.executionAuthority, base.trading)
      : base.trading,
    agentId: bot.action.agentId ?? 'eclipse-agent',
  };
  const trig = bot.trigger;
  // KEEP the bot's own zone: re-saving an edit must not re-stamp it to the
  // editor's browser zone and silently shift every future fire.
  if (trig.timezone) draft.timezone = trig.timezone;
  draft.misfire = bot.execution.misfire;
  draft.catchUpMax = bot.execution.catchUpMax;
  draft.maxRuntimeS = bot.execution.maxRuntimeS;
  draft.onConflict = bot.concurrency.onConflict;
  draft.maxRuns = bot.concurrency.maxConcurrentRuns;
  draft.maxAttempts = bot.retry.maxAttempts;
  const pinned = botModelOverride(bot);
  if (pinned) {
    draft.model = pinned;
    const p = (bot.action.config?.modelSettings as { provider?: unknown } | undefined)?.provider;
    draft.modelProvider = typeof p === 'string' ? p : '';
  }
  if (trig.type === 'interval') {
    const s = trig.intervalSeconds ?? 3600;
    if (s % 86400 === 0) { draft.schedule = 'interval'; draft.intervalValue = s / 86400; draft.intervalUnit = 'days'; }
    else if (s % 3600 === 0) { draft.schedule = 'interval'; draft.intervalValue = s / 3600; draft.intervalUnit = 'hours'; }
    else if (s % 60 === 0) { draft.schedule = 'interval'; draft.intervalValue = Math.round(s / 60); draft.intervalUnit = 'minutes'; }
    else { draft.schedule = 'interval'; draft.intervalValue = s; draft.intervalUnit = 'seconds'; }
  } else if (trig.type === 'cron' && trig.cron) {
    const parts = trig.cron.trim().split(/\s+/);
    if (parts.length === 5) {
      const [min, hour, dom, mon, dow] = parts;
      if (/^\d+$/.test(min) && /^\d+$/.test(hour)) draft.time = `${pad2(Number(hour))}:${pad2(Number(min))}`;
      if (dom === '*' && mon === '*' && dow === '*') draft.schedule = 'daily';
      else if (dom === '*' && mon === '*' && dow === '1-5') draft.schedule = 'weekdays';
      else if (dom === '*' && mon === '*' && /^\d$/.test(dow)) { draft.schedule = 'weekly'; draft.weekday = Number(dow); }
      else if (/^\d+$/.test(dom) && mon === '*' && dow === '*') { draft.schedule = 'monthly'; draft.dom = Number(dom); }
      else { draft.schedule = 'cron'; draft.cron = trig.cron; }
    } else {
      draft.schedule = 'cron';
      draft.cron = trig.cron;
    }
  } else if (trig.type === 'one_time') {
    draft.schedule = 'once';
    if (trig.runAt) {
      const d = new Date(trig.runAt);
      if (!Number.isNaN(d.getTime())) {
        const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
        draft.runAt = local.toISOString().slice(0, 16);
      }
    }
  } else if (trig.type === 'event') {
    draft.schedule = 'event';
    draft.eventType = trig.eventType;
    draft.eventFilter = { ...(trig.eventFilter ?? {}) };
    draft.startAt = trig.startAt;
    draft.endAt = trig.endAt;
  } else if (trig.type === 'condition') {
    draft.schedule = 'condition';
    draft.condition = trig.condition ?? bot.condition ?? { expression: '', edge: true, variables: [] };
    draft.startAt = trig.startAt;
    draft.endAt = trig.endAt;
  } else if (trig.type === 'webhook') {
    draft.schedule = 'webhook';
  } else {
    draft.schedule = 'manual';
  }
  return draft;
}
