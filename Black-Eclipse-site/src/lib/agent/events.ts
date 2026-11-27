import { APP_TABS } from '@/lib/app-tabs';
import type {
  ActivityCodePreview,
  ActivityItem,
  DelegationChildStatus,
  DelegationChildView,
  DelegationView,
  ApprovalChallenge,
  PendingApproval,
  PendingClarification,
  ResponsePlan,
  ResponseStatus,
  TurnChange,
} from './response-model';
import { coerceResponsePlan, sourceLabel, toolLabel } from './response-model';
import type { CapabilityEventPhase } from '@/lib/harness/capability-types';
import { refusalKey } from '@/lib/i18n/refusal-copy';

/**
 * Normalize the harness SSE stream into a small, typed UI event union.
 *
 * This is the ONE place raw harness frames are interpreted. It replaces the
 * ad-hoc `statusFromEvent` mapping so the UI can react to tool failures,
 * retrieval activity, cancellation and errors — not just a generic "Thinking…".
 *
 * Raw shapes (see docs/chat-ui-audit.md §2):
 *  - Runtime events: full envelope `{ id, runId, type, seq, at, data, transient? }`
 *  - Front-door events (`client.*`): payload fields at the top level
 */

export type HarnessUiEvent =
  | { kind: 'status'; status: ResponseStatus }
  /** A transient explanation for the working indicator (waiting/reconnecting). */
  | { kind: 'notice'; text: string }
  | { kind: 'delta'; text: string }
  | { kind: 'activity_start'; item: ActivityItem }
  | {
      kind: 'activity_end';
      id?: string;
      name: string;
      status: 'completed' | 'failed';
      durationMs?: number;
      code?: string;
      /** Short user-safe result summary (e.g. "3 records") when reported. */
      summary?: string;
    }
  /**
   * File changes the runtime reported this step (with its bounded, redacted
   * diff previews). A multi-file change arrives as several items at once.
   */
  | { kind: 'changes'; items: TurnChange[] }
  | {
      kind: 'final';
      output?: string;
      error?: string;
      errorCode?: string;
      status?: string;
      research?: unknown;
      memory?: unknown;
      runId?: string;
      /** The provider + model that actually served this turn (from the run). */
      provider?: string;
      model?: string;
      /** Phase 14: the run's code-verification ledger + code profile, when any. */
      codeVerification?: unknown;
      autonomy?: string | null;
      workspaceId?: string | null;
      /** Phase 14 A6.4: the runtime-derived chat capability view. */
      capability?: unknown;
      /** Vision/Multimodal: the run's collected vision results. */
      vision?: unknown;
      /** Document Intelligence: the run's collected document results. */
      documents?: unknown;
      /** Internet Image Search: the run's validated image results. */
      images?: unknown;
      /** Native video: the video jobs this run queued (live job card). */
      video?: unknown;
      /** A typed client intent from a routed command (`navigate`/`openCanvas`). */
      ui?: import('@/lib/harness/commands/command-ui').CommandUiAction;
    }
  | { kind: 'error'; message: string; code?: string }
  /** The run is blocked on an operator decision (approval card). */
  | { kind: 'approval_request'; approval: PendingApproval }
  /** A previously requested approval was decided (or expired/timed out). */
  | { kind: 'approval_resolved'; id?: string; approved?: boolean }
  /** The run paused on a `clarify` question (a question card, not an approval). */
  | { kind: 'clarification_request'; clarification: PendingClarification }
  /** The question was answered (or timed out and the run resumed). */
  | { kind: 'clarification_answered'; id?: string }
  /**
   * A completed computation (runtime `analysis.completed`): the bounded result
   * blob (outputs, artifacts, provenance) the chat renders as "worked out by
   * Eclipse". A computed result is never shown as retrieved data.
   */
  | { kind: 'analysis'; result: unknown }
  /**
   * A dataset collection completed (runtime `acquire.completed`): the pull
   * manifest (source, window, rows, completeness, licence, attribution) the
   * chat renders as a readable panel. A partial pull is shown partial — never
   * presented as the dataset.
   */
  | { kind: 'acquisition'; manifest: unknown }
  /**
   * Generated-capability lifecycle (runtime `capability.*`): the request, build,
   * test result and activation decision the chat renders as one card. The view
   * is coerced by `capability-types.ts`; this event never invents a state.
   */
  | { kind: 'capability'; phase: CapabilityEventPhase; view: unknown }
  /**
   * The Intelligent Response Orchestrator chose this turn's presentation mode
   * (runtime `response.plan`). Display-only �?" it never changes the answer.
   */
  | { kind: 'response_plan'; plan: ResponsePlan }
  /**
   * A parallel fan-out began (`delegation.started`): one row per part, with the
   * user's own sub-question. The rows then update from `delegation.progress`.
   */
  | { kind: 'delegation_start'; delegation: DelegationView }
  /** One part's live snapshot (`delegation.progress`) — bounded and content-free. */
  | { kind: 'delegation_progress'; child: DelegationChildView & { delegationId: string } }
  /** A part reached a terminal state (`delegation.child.*`). */
  | {
      kind: 'delegation_child';
      delegationId: string;
      childIndex: number;
      status: DelegationChildStatus;
      tokens?: number;
      reason?: string;
    }
  /** The fan-out was aggregated (`delegation.aggregated`): done + real totals. */
  | {
      kind: 'delegation_done';
      delegationId: string;
      droppedClaims?: number;
      tokens?: number;
      /** `best_of` winner (a child index), or null when none finished. */
      winner?: number | null;
    };

/**
 * Normalize a frame's payload.
 *
 * Runtime events are emitted as the full envelope (`{ type, data: {...} }`),
 * while the front door's `client.*` events put their fields at the top level.
 * Returning the inner `data` for envelopes keeps consumers reading the same
 * shape regardless of who produced the frame.
 */
export function unwrapEventPayload(data: Record<string, unknown>): Record<string, unknown> {
  const inner = data.data;
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
    return { ...(inner as Record<string, unknown>), eventType: data.type };
  }
  return data;
}

function str(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function num(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/** A malformed code preview is dropped, never half-rendered. */
function codePreview(value: unknown): ActivityCodePreview | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const text = str(record.text);
  if (!text) return undefined;
  return {
    language: str(record.language) ?? 'code',
    text,
    truncated: record.truncated === true,
    totalChars: num(record.totalChars) ?? text.length,
  };
}

const DELEGATION_CHILD_STATUSES: ReadonlySet<DelegationChildStatus> = new Set([
  'running', 'completed', 'failed', 'refused', 'timed_out', 'cancelled',
]);

function childStatus(value: unknown): DelegationChildStatus {
  return typeof value === 'string' && DELEGATION_CHILD_STATUSES.has(value as DelegationChildStatus)
    ? (value as DelegationChildStatus)
    : 'running';
}

function aggregateMode(value: unknown): DelegationView['aggregate'] {
  return value === 'compare' || value === 'merge_claims' ? value : 'list';
}

/** `delegation.started` → the initial fan-out view, or null when malformed. */
function delegationFromStart(payload: Record<string, unknown>): DelegationView | null {
  const delegationId = str(payload.delegationId);
  if (!delegationId) return null;
  const agents = Array.isArray(payload.agents) ? payload.agents.filter(isRecord) : [];
  const children: DelegationChildView[] = agents
    .map((agent, index): DelegationChildView => {
      const agentId = str(agent.agentId);
      const model = str(agent.model);
      const approach = str(agent.approach);
      return {
        childIndex: num(agent.childIndex) ?? index,
        goal: str(agent.goal) ?? '',
        status: 'running',
        ...(agentId ? { agentId } : {}),
        ...(model ? { model } : {}),
        ...(approach ? { approach } : {}),
      };
    })
    .sort((a, b) => a.childIndex - b.childIndex);
  const title = str(payload.title);
  return {
    delegationId,
    aggregate: aggregateMode(payload.aggregate),
    strategy: payload.strategy === 'best_of' ? 'best_of' : 'split',
    ...(title ? { title } : {}),
    children,
    done: false,
  };
}

/** `delegation.progress` → one child's live snapshot, or null when malformed. */
function childFromProgress(
  payload: Record<string, unknown>,
): (DelegationChildView & { delegationId: string }) | null {
  const delegationId = str(payload.delegationId);
  const childIndex = num(payload.childIndex);
  if (!delegationId || childIndex == null) return null;
  const view: DelegationChildView = {
    childIndex,
    goal: str(payload.goal) ?? '',
    status: childStatus(payload.status),
  };
  const runId = str(payload.runId);
  const agentId = str(payload.agentId);
  const model = str(payload.model);
  const approach = str(payload.approach);
  const activity = str(payload.activity);
  const step = num(payload.step);
  const maxSteps = num(payload.maxSteps);
  const tokens = num(payload.tokens);
  const reason = str(payload.reason);
  if (runId) view.runId = runId;
  if (agentId) view.agentId = agentId;
  if (model) view.model = model;
  if (approach) view.approach = approach;
  if (activity) view.activity = activity;
  if (Array.isArray(payload.recent)) {
    const recent = payload.recent.filter((item): item is string => typeof item === 'string');
    if (recent.length > 0) view.recent = recent.slice(-12);
  }
  if (step != null) view.step = step;
  if (maxSteps != null) view.maxSteps = maxSteps;
  if (tokens != null) view.tokens = tokens;
  // Cost is REAL or explicitly unknown (null) — never defaulted to 0.
  if (payload.costUsd === null) view.costUsd = null;
  else if (num(payload.costUsd) != null) view.costUsd = num(payload.costUsd);
  if (reason) view.reason = reason;
  return { ...view, delegationId };
}

const STATUS_BY_EVENT: Record<string, ResponseStatus> = {
  'run.created': 'queued',
  'run.started': 'planning',
  'model.requested': 'planning',
  'model.completed': 'planning',
  'tool.requested': 'tool_running',
  'run.completed': 'completed',
  'run.failed': 'error',
  'run.cancelled': 'cancelled',
};

/** The runtime's operation word → the change kind a card shows. */
const CHANGE_BY_OP: Record<string, TurnChange['change']> = {
  create: 'added',
  add: 'added',
  copy: 'added',
  create_file: 'added',
  delete: 'deleted',
  remove: 'deleted',
  delete_file: 'deleted',
  move: 'renamed',
  rename: 'renamed',
  move_file: 'renamed',
  rename_file: 'renamed',
};

function coerceCommandUi(value: unknown): import('@/lib/harness/commands/command-ui').CommandUiAction | undefined {
  if (!isRecord(value)) return undefined;
  if (
    value.type === 'navigate' &&
    typeof value.tab === 'string' &&
    (APP_TABS as readonly string[]).includes(value.tab)
  ) {
    return value as unknown as import('@/lib/harness/commands/command-ui').CommandUiAction;
  }
  if (value.type === 'openCanvas') {
    return value as unknown as import('@/lib/harness/commands/command-ui').CommandUiAction;
  }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * One change record from a runtime payload (an fs `previews[]` entry, a
 * `code.patch.applied` event, or a single-file write). Returns null when the
 * record carries no path. The diff body and counts are the runtime's own —
 * never re-derived here.
 */
function turnChangeFrom(record: Record<string, unknown>): TurnChange | null {
  const path = str(record.path) ?? str(record.to);
  if (!path) return null;
  const op = (str(record.op) ?? str(record.operation) ?? '').toLowerCase();
  const from = str(record.from);
  const diff = str(record.diff);
  return {
    path,
    change: CHANGE_BY_OP[op] ?? 'modified',
    additions: num(record.additions),
    deletions: num(record.deletions),
    ...(from ? { from } : {}),
    ...(diff ? { diff } : {}),
    ...(record.diffTruncated === true || record.truncated === true ? { truncated: true } : {}),
  };
}

/**
 * Map one raw `(event, payload)` frame to a typed UI event, or `null` when the
 * frame carries nothing the UI should act on.
 */
export function normalizeHarnessEvent(
  event: string,
  rawPayload: Record<string, unknown>,
): HarnessUiEvent | null {
  const payload = unwrapEventPayload(rawPayload);

  if (event === 'model.queued') {
    const reason = str(payload.reason) ?? '';
    return {
      kind: 'notice',
      text: reason === 'concurrency'
        ? 'Waiting for provider capacity…'
        : reason === 'credential_cooldown' || reason === 'rate_limit'
          ? 'Waiting for the provider rate limit to reset…'
          : 'Waiting for the provider…',
    };
  }
  if (event === 'model.rate_limited') {
    return { kind: 'notice', text: 'Provider capacity is exhausted — retrying…' };
  }
  if (event === 'model.stalled') {
    return { kind: 'notice', text: 'The provider stopped streaming — recovering…' };
  }

  if (event === 'model.delta') {
    const text = str(payload.text) ?? '';
    return text ? { kind: 'delta', text } : null;
  }

  if (event === 'tool.requested') {
    const name = str(payload.name) ?? 'tool';
    const id = str(payload.toolCallId) ?? `tool:${name}`;
    const detail = str(payload.detail);
    const preview = codePreview(payload.codePreview);
    return {
      kind: 'activity_start',
      item: {
        id,
        kind: 'tool',
        name,
        label: toolLabel(name),
        status: 'running',
        ...(detail ? { detail } : {}),
        ...(preview ? { codePreview: preview } : {}),
      },
    };
  }

  // Context budgeting: the runtime trimmed history to fit the resolved model's
  // window. Surface it as one plain activity line (counts only — never content).
  if (event === 'memory.history_bounded') {
    const dropped = num(payload.droppedTurns) ?? 0;
    const kept = num(payload.keptTurns) ?? 0;
    if (dropped <= 0) return null;
    const windowKnown = payload.windowKnown === true;
    const window = num(payload.window);
    const detail =
      windowKnown && window != null
        ? `${window.toLocaleString()}-token window`
        : 'context window unknown';
    return {
      kind: 'activity_start',
      item: {
        id: 'history_bounded',
        kind: 'bounded',
        name: 'memory.history_bounded',
        label: `Trimmed ${dropped} older turn${dropped === 1 ? '' : 's'} to fit the context window`,
        status: 'completed',
        summary: `${kept} kept · ${detail}`,
      },
    };
  }

  // Memory recall: the runtime injected selected long-term memories into this
  // turn. Surface it as one plain activity line — counts and character budget
  // only, never memory content, ids or excluded text. Mirrors `history_bounded`.
  if (event === 'memory.retrieved') {
    const selected = num(payload.selected) ?? 0;
    if (selected <= 0) return null;
    const usedChars = num(payload.usedChars);
    return {
      kind: 'activity_start',
      item: {
        id: 'memory_recalled',
        kind: 'recall',
        name: 'memory.retrieved',
        label: `Recalled ${selected} memor${selected === 1 ? 'y' : 'ies'} from long-term memory`,
        status: 'completed',
        summary: usedChars != null ? `${usedChars.toLocaleString()} chars recalled` : undefined,
      },
    };
  }

  if (event === 'tool.completed' || event === 'tool.failed') {
    const name = str(payload.name) ?? 'tool';
    const failed = event === 'tool.failed';
    const summary = str(payload.summary);
    return {
      kind: 'activity_end',
      id: str(payload.toolCallId),
      name,
      status: failed ? 'failed' : 'completed',
      durationMs: num(payload.durationMs),
      code: failed ? str(payload.code) : undefined,
      ...(summary ? { summary } : {}),
    };
  }

  // A refused code step (Phase 14): the runtime emits `code.refused` /
  // `code.patch.refused` with a stable code. Show it as ONE plain sentence, not
  // the raw code; the code stays in `code` behind "details". "I couldn't do this
  // and here's exactly why" is a first-class answer.
  if (event === 'code.refused' || event === 'code.patch.refused') {
    const code = str(payload.code) ?? 'REFUSED';
    const tool = str(payload.tool) ?? str(payload.op) ?? 'code';
    // `reasonKey` is always set (the map has a fallback); the component renders
    // it via `useT`, so no locale lives in this reducer. `label`/`name` stay
    // technical and are only shown behind "details".
    return {
      kind: 'activity_start',
      item: {
        id: `code_refused:${code}:${tool}`,
        kind: 'tool',
        name: tool,
        label: toolLabel(tool),
        status: 'completed',
        code,
        reasonKey: refusalKey(code),
      },
    };
  }

  // Retrieval activity — the sources the agent consulted *inside* a tool call.
  if (event === 'retrieval.source_start') {
    const source = str(payload.source) ?? 'source';
    const dataType = str(payload.dataType) ?? '';
    const id = `retrieval:${source}:${dataType}`;
    return {
      kind: 'activity_start',
      item: {
        id,
        kind: 'retrieval',
        name: source,
        label: `Consulting ${sourceLabel(source)}`,
        source,
        status: 'running',
      },
    };
  }

  if (event === 'retrieval.source_ok' || event === 'retrieval.source_unavailable') {
    const source = str(payload.source) ?? 'source';
    const dataType = str(payload.dataType) ?? '';
    const failed = event === 'retrieval.source_unavailable';
    const objects = num(payload.objects);
    return {
      kind: 'activity_end',
      id: `retrieval:${source}:${dataType}`,
      name: source,
      status: failed ? 'failed' : 'completed',
      code: failed ? str(payload.error) : undefined,
      // Only report a count when the runtime actually sent one.
      summary: !failed && objects != null ? `${objects} record${objects === 1 ? '' : 's'}` : undefined,
    };
  }

  // An approval card request (runtime `approval.requested`): the run is now
  // blocked until the operator decides. The legacy `*.approval_required` events
  // are intentionally NOT mapped here — they fire on the deny-and-retry path and
  // the run is not waiting, so surfacing a blocking card would be dishonest.
  if (event === 'approval.requested') {
    const id = str(payload.approvalId);
    if (!id) return null;
    const tool = str(payload.tool) ?? '';
    const title = str(payload.title) ?? (tool ? `Approve using ${tool}` : 'This task requires approval');
    return {
      kind: 'approval_request',
      approval: {
        id,
        kind: str(payload.kind) ?? 'tool',
        title,
        summary: str(payload.summary),
        effect: str(payload.effect),
        risk: str(payload.risk),
        // The "always allow" target: the program/tool and the label the runtime
        // derived. Absent when the action is not grantable (never offered).
        grantPattern: str(payload.grantPattern),
        grantLabel: str(payload.grantLabel),
        challenge: coerceChallenge(payload.challenge),
      },
    };
  }

  if (event === 'approval.resolved') {
    return { kind: 'approval_resolved', id: str(payload.approvalId), approved: payload.approved === true };
  }

  // A human-in-the-loop question (runtime `clarification.requested`). The run
  // stays open on the gate until the operator answers, so this is a blocking
  // card, not a terminal state. A malformed frame is dropped rather than shown
  // as an empty question.
  if (event === 'clarification.requested') {
    const id = str(payload.clarificationId);
    const question = str(payload.question);
    if (!id || !question) return null;
    const rawOptions = Array.isArray(payload.options) ? payload.options : [];
    const options = rawOptions
      .map((option) => str(option))
      .filter((option): option is string => Boolean(option));
    return {
      kind: 'clarification_request',
      clarification: {
        id,
        question,
        context: str(payload.context),
        options,
        recommended: str(payload.recommended),
        allowCustom: payload.allowCustom !== false,
      },
    };
  }

  if (event === 'clarification.answered') {
    return { kind: 'clarification_answered', id: str(payload.clarificationId) };
  }

  // A completed computation (runtime `analysis.completed`). The payload carries
  // the SAME bounded result blob the runtime persists (outputs, artifacts,
  // provenance), so the chat can show what Eclipse worked out — clearly labelled
  // as computed, never as retrieved data.
  if (event === 'analysis.completed') {
    const result = payload.result;
    if (!result || typeof result !== 'object' || Array.isArray(result)) return null;
    return { kind: 'analysis', result };
  }

  // A completed dataset collection (runtime `acquire.completed`). The payload
  // carries the SAME manifest the runtime writes beside the CSV, so the chat
  // shows source/window/rows/completeness/licence without a second contract.
  // A malformed payload is dropped — never a half-rendered manifest.
  if (event === 'acquire.completed') {
    const manifest = payload.manifest;
    if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) return null;
    return { kind: 'acquisition', manifest };
  }

  const capabilityPhase: CapabilityEventPhase | undefined =
    event === 'capability.requested' ? 'requested'
    : event === 'capability.offer' ? 'requested'
    : event === 'capability.built' ? 'built'
    : event === 'capability.activated' ? 'activated'
    : event === 'capability.verification_failed' ? 'verification_failed'
    : event === 'capability.disabled' ? 'disabled'
    : event === 'capability.retired' ? 'retired'
    : undefined;
  if (capabilityPhase) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    if (!str(payload.capabilityId) && !str(payload.need)) return null;
    return { kind: 'capability', phase: capabilityPhase, view: payload };
  }

  // The Intelligent Response Orchestrator's chosen presentation mode
  // (runtime `response.plan`). Display only; a malformed payload is dropped.
  if (event === 'response.plan') {
    const plan = coerceResponsePlan(payload);
    return plan ? { kind: 'response_plan', plan } : null;
  }

  // A standing grant short-circuited the prompt (runtime `approval.granted`).
  // No card is shown; the run proceeds. Surfaced only so the UI can close any
  // stale card for the same step.
  if (event === 'approval.granted') {
    return { kind: 'approval_resolved', approved: true };
  }

  // Front-door terminal frame: carries the authoritative answer.
  if (event === 'client.final') {
    const err = payload.error;
    // `error` is a structured `{ code, message }` from the run record; the
    const errorMessage =
      err && typeof err === 'object'
        ? str((err as Record<string, unknown>).message)
        : str(err);
    const errorCode =
      err && typeof err === 'object' ? str((err as Record<string, unknown>).code) : undefined;
    return {
      kind: 'final',
      output: str(payload.output),
      error: errorMessage,
      errorCode,
      status: str(payload.status),
      research: payload.research ?? undefined,
      memory: payload.memory ?? undefined,
      runId: str(payload.runId),
      provider: str(payload.provider),
      model: str(payload.model),
      codeVerification: payload.codeVerification ?? undefined,
      autonomy: typeof payload.autonomy === 'string' ? payload.autonomy : payload.autonomy === null ? null : undefined,
      workspaceId: typeof payload.workspaceId === 'string' ? payload.workspaceId : payload.workspaceId === null ? null : undefined,
      capability: payload.capability ?? undefined,
      vision: payload.vision ?? undefined,
      documents: payload.documents ?? undefined,
      images: payload.images ?? undefined,
      video: payload.video ?? undefined,
      ui: coerceCommandUi(payload.ui),
    };
  }

  if (event === 'client.error') {
    return { kind: 'error', message: str(payload.message) ?? 'stream error' };
  }

  if (event === 'run.failed') {
    const error = payload.error;
    const message =
      error && typeof error === 'object'
        ? str((error as Record<string, unknown>).message)
        : undefined;
    const code =
      error && typeof error === 'object' ? str((error as Record<string, unknown>).code) : undefined;
    return { kind: 'error', message: message ?? 'The run failed.', code };
  }

  // File changes — the runtime's bounded, redacted diff previews. Fed by the
  // fs write events (`previews[]`) and the workspace patch engine (one event
  // per applied op). This is what lets the chat show WHAT changed, not only
  // that a tool ran.
  if (event.startsWith('fs.') || event === 'code.patch.applied') {
    const records: unknown[] =
      event === 'code.patch.applied' ? [payload] : (payload.previews as unknown[] | undefined) ?? [];
    const items = records
      .filter(isRecord)
      .map(turnChangeFrom)
      .filter((c): c is TurnChange => c !== null);
    return items.length > 0 ? { kind: 'changes', items } : null;
  }

  // Parallel fan-out: the rows, their live progress, their terminal states and
  // the final honest totals. Every field is the runtime's own; nothing here
  // computes a percentage or a price.
  if (event === 'delegation.started') {
    const delegation = delegationFromStart(payload);
    return delegation ? { kind: 'delegation_start', delegation } : null;
  }

  if (event === 'delegation.progress') {
    const child = childFromProgress(payload);
    return child ? { kind: 'delegation_progress', child } : null;
  }

  if (
    event === 'delegation.child.completed' ||
    event === 'delegation.child.failed' ||
    event === 'delegation.child.refused'
  ) {
    const delegationId = str(payload.delegationId);
    const childIndex = num(payload.childIndex);
    if (!delegationId || childIndex == null) return null;
    const tokens = num(payload.tokens);
    const reason = str(payload.reason);
    return {
      kind: 'delegation_child',
      delegationId,
      childIndex,
      status: childStatus(payload.status),
      ...(tokens != null ? { tokens } : {}),
      ...(reason ? { reason } : {}),
    };
  }

  if (event === 'delegation.aggregated' || event === 'delegation.completed') {
    const delegationId = str(payload.delegationId);
    if (!delegationId) return null;
    const droppedClaims = num(payload.droppedClaims);
    const tokens = num(payload.tokens);
    const winner = payload.winner === null ? null : num(payload.winner);
    return {
      kind: 'delegation_done',
      delegationId,
      ...(droppedClaims != null ? { droppedClaims } : {}),
      ...(tokens != null ? { tokens } : {}),
      ...(winner !== undefined ? { winner } : {}),
    };
  }

  const status = STATUS_BY_EVENT[event];
  if (status) return { kind: 'status', status };

  return null;
}

/**
 * Coerce a raw money-confirm challenge into typed display data. Only known
 * fields are copied; a malformed value yields undefined so the card degrades to
 * an ordinary approval instead of showing invented detail.
 */
function coerceChallenge(raw: unknown): ApprovalChallenge | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const value = raw as Record<string, unknown>;
  const reasons = Array.isArray(value.reasons)
    ? value.reasons.filter((r): r is string => typeof r === 'string' && r.trim().length > 0)
    : undefined;
  const cooling = num(value.coolingOffSeconds);
  const amount = num(value.amount);
  return {
    ...(reasons && reasons.length > 0 ? { reasons } : {}),
    ...(cooling != null ? { coolingOffSeconds: cooling } : {}),
    ...(typeof value.requiresTypedConfirmation === 'boolean'
      ? { requiresTypedConfirmation: value.requiresTypedConfirmation }
      : {}),
    ...(typeof value.confirmPhrase === 'string' ? { confirmPhrase: value.confirmPhrase } : {}),
    ...(typeof value.escalateTo === 'string' ? { escalateTo: value.escalateTo } : {}),
    ...(amount != null ? { amount } : {}),
    ...(typeof value.currency === 'string' ? { currency: value.currency } : {}),
    ...(typeof value.counterparty === 'string' ? { counterparty: value.counterparty } : {}),
  };
}
