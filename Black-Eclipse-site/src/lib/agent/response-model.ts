// ── Response lifecycle model ────────────────────────────────────────────────
// The single typed vocabulary for "what is the agent doing" on the client. The
// Python runtime owns the raw event stream (see docs/chat-ui-audit.md); this
// module is the *client* representation the UI renders, so rendering never has
// to understand raw harness envelopes, registry ids, or error codes.
//
// Nothing here talks to the network — it is pure data + formatting.

/** The coarse lifecycle of one assistant turn. */
export type ResponseStatus =
  | 'queued'
  | 'planning'
  | 'tool_running'
  | 'streaming'
  | 'awaiting_approval'
  | 'completed'
  | 'cancelled'
  | 'error';

/**
 * One action the agent is blocked on until the operator approves or denies it.
 *
 * `id` is the single-use, digest-bound approval row id; approving POSTs a
 * decision that resumes the paused run in place. Carries only display copy the
 * runtime already derived (never file content or secrets).
 */
export interface PendingApproval {
  id: string;
  /** Discriminator, e.g. `mcp_tool`, `code_action`, `terminal_command`. */
  kind: string;
  /** Short imperative title, e.g. "Approve running a command". */
  title: string;
  /** One plain sentence describing exactly what will happen. */
  summary?: string;
  /** Effect class (`write` | `external_action`). */
  effect?: string;
  /** Declared risk (`low` | `medium` | `high`) when the runtime reports one. */
  risk?: string;
  /**
   * The `(kind, scope, pattern)` a "Always allow" choice would remember, e.g.
   * the program `python3` or the MCP tool name. Absent when the runtime did not
   * mark the action grantable — the card then shows no "Always allow" button.
   */
  grantPattern?: string;
  /** Human label for the grant, e.g. "Always allow `python3` in this workspace". */
  grantLabel?: string;
  /**
   * The money-confirm challenge (money guardrail approvals only): why the
   * action needs a deliberate confirmation, how long to wait, and the exact
   * phrase the operator must type. Absent for ordinary approvals.
   */
  challenge?: ApprovalChallenge;
}

/** The extra fields a money-movement confirmation carries. */
export interface ApprovalChallenge {
  reasons?: string[];
  coolingOffSeconds?: number;
  requiresTypedConfirmation?: boolean;
  confirmPhrase?: string;
  escalateTo?: string | null;
  amount?: number | null;
  currency?: string | null;
  counterparty?: string | null;
}

/**
 * A question the agent paused on (`clarify`). This is not an approval: the
 * answer is a choice label or free text, and it can never authorize an effect.
 */
export interface PendingClarification {
  id: string;
  /** The one question, in plain language. */
  question: string;
  /** Optional reason the choice matters (one sentence). */
  context?: string;
  /** Concrete choices the operator can pick. */
  options: string[];
  /** The option the agent recommends, when it named one. */
  recommended?: string;
  /** Whether the operator may type a free-text answer instead of choosing. */
  allowCustom: boolean;
}

/**
 * A bounded source preview for a code-execution step. The runtime redacts
 * secret-shaped spans and hard-caps `text`; `totalChars` is the original length
 * so the UI can state truncation honestly.
 */
export interface ActivityCodePreview {
  language: string;
  text: string;
  truncated: boolean;
  totalChars: number;
}

/** One thing the agent did (a tool call, a retrieval source, or a context note). */
export interface ActivityItem {
  /** Stable id — the tool's `toolCallId` when the runtime provides one. */
  id: string;
  kind: 'tool' | 'retrieval' | 'bounded' | 'recall';
  /** Raw operation name, e.g. `market_get_quote`. Kept for developer detail. */
  name: string;
  /** Human-readable label, e.g. "Retrieving market quote". Safe to display. */
  label: string;
  /**
   * What the tool is acting on — the command, the file path, or the changed
   * paths. Bounded, content-free (never file text), derived by the runtime's
   * `code/activity.py`. Absent when there is nothing safe to show.
   */
  detail?: string;
  /** Data category / provider for a retrieval item (e.g. "yfinance"). */
  source?: string;
  /**
   * The bounded, redacted program a Python execution tool is running. Present
   * only for tools that execute model-authored source (`analysis_run`,
   * `execute_python`); absent everywhere else.
   */
  codePreview?: ActivityCodePreview;
  status: 'running' | 'completed' | 'failed';
  /** Wall-clock duration when the runtime reports one. Never fabricated. */
  durationMs?: number;
  /** Failure code (`TOOL_ERROR`, `TOOL_FORBIDDEN`, …) — detail, not the label. */
  code?: string;
  /**
   * A short, user-safe result summary when the runtime provides one (e.g. a
   * retrieval source reporting "3 records"). Absent when the backend does not
   * report a result — never inferred.
   */
  summary?: string;
  /**
   * Phase 14: an i18n KEY holding one plain sentence for a refused step
   * ("this task isn't set up to make that kind of change"). The raw code stays in
   * `code`. Absent for an ordinary tool step — the component shows `label` then.
   */
  reasonKey?: import('@/lib/i18n/types').TranslationKey;
}

/**
 * The provider + model that actually served one assistant turn, taken from the
 * run record (never inferred). `provider` is absent when the runtime did not
 * report one; the chat surface then shows the model id alone.
 */
export interface AssistantModel {
  model: string;
  provider?: string | null;
}

/** A recoverable failure attached to an assistant turn. */
export interface TurnError {
  code?: string;
  message: string;
}

/** The presentation mode the Intelligent Response Orchestrator chose for a turn. */
export type ResponseMode =
  | 'text'
  | 'explanation'
  | 'visualization'
  | 'simulation'
  | 'workflow'
  | 'composite';

/**
 * The orchestrator's response-mode plan for one turn (runtime `response.plan`
 * event). It only steers presentation; it never changes the answer's content.
 * `signals` and `suggestedBlocks` are diagnostic detail — `mode` is what the UI
 * surfaces.
 */
export interface ResponsePlan {
  mode: ResponseMode;
  confidence: 'certain' | 'likely' | 'unsure';
  reason: string;
  signals: string[];
  suggestedBlocks: string[];
  learning: boolean;
}

const RESPONSE_MODES = new Set<ResponseMode>([
  'text', 'explanation', 'visualization', 'simulation', 'workflow', 'composite',
]);

/**
 * Narrow an untrusted `response.plan` payload to a `ResponsePlan`, or undefined.
 * Total: a malformed payload never throws and is simply dropped.
 */
export function coerceResponsePlan(value: unknown): ResponsePlan | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const mode = record.mode;
  if (typeof mode !== 'string' || !RESPONSE_MODES.has(mode as ResponseMode)) return undefined;
  const confidence =
    record.confidence === 'certain' || record.confidence === 'likely' || record.confidence === 'unsure'
      ? record.confidence
      : 'unsure';
  const strings = (input: unknown): string[] =>
    Array.isArray(input) ? input.filter((x): x is string => typeof x === 'string') : [];
  return {
    mode: mode as ResponseMode,
    confidence,
    reason: typeof record.reason === 'string' ? record.reason : '',
    signals: strings(record.signals),
    suggestedBlocks: strings(record.suggestedBlocks),
    learning: record.learning === true,
  };
}

/**
 * One file change an assistant turn made, with the runtime's bounded diff
 * preview. Fed by the runtime's change events (`code.patch.applied`,
 * `fs.*` with `previews`) — never re-derived on the client, never fabricated.
 */
export interface TurnChange {
  /** Workspace-relative path (for a move, the destination). */
  path: string;
  change: 'added' | 'modified' | 'deleted' | 'renamed';
  /** The previous path of a move/rename, when the runtime reported one. */
  from?: string;
  /** Added/removed line counts, only when the runtime reported them. */
  additions?: number;
  deletions?: number;
  /** Bounded, redacted unified diff text. Absent when there is no text
   *  preview (e.g. a pure move) — the card then shows no diff body. */
  diff?: string;
  /** True when the runtime capped the diff preview (honest truncation). */
  truncated?: boolean;
  /** How many separate edits this turn made to this path (1 when once). */
  edits?: number;
}

/** One delegated part of a parallel fan-out, as the runtime reported it. */
export type DelegationChildStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'refused'
  | 'timed_out'
  | 'cancelled';

export interface DelegationChildView {
  childIndex: number;
  runId?: string;
  /** The agent that ran this part (from the runtime; never inferred). */
  agentId?: string;
  /** The model this part ran on, when it differs from the turn's model. */
  model?: string;
  /** The user's own sub-question for this part (bounded by the runtime). */
  goal: string;
  /** A short angle for this attempt (e.g. "mock the API") — the row label. */
  approach?: string;
  status: DelegationChildStatus;
  /** What the part is doing right now (the child's latest step). */
  activity?: string;
  /** The last distinct activities, oldest first — the popup's timeline. */
  recent?: string[];
  /** The runtime's step counter — never a percentage we computed ourselves. */
  step?: number;
  /** The child run's own step ceiling, when it reported one. */
  maxSteps?: number;
  /** REAL tokens the part has used so far. */
  tokens?: number;
  /** REAL price for those tokens, or null when the model has no known price. */
  costUsd?: number | null;
  /** A plain sentence on failure/refusal, when the runtime sent one. */
  reason?: string;
}

/** One fan-out: the parts it split into, their live state, and its result. */
export type DelegationStrategy = 'split' | 'best_of';

export interface DelegationView {
  delegationId: string;
  aggregate: 'list' | 'compare' | 'merge_claims';
  /** `split` = different sub-questions; `best_of` = N tries at the same task. */
  strategy: DelegationStrategy;
  /** A short label for the whole fan-out (best_of defaults to its goal). */
  title?: string;
  children: DelegationChildView[];
  /** Claims the parent refused to repeat (unconfirmed) — a count, never text. */
  droppedClaims?: number;
  /** REAL tokens used across the parts. */
  tokens?: number;
  /** `best_of` only: the part the evidence selected, or null when none finished. */
  winner?: number | null;
  /** True once the runtime reported the fan-out aggregated. */
  done: boolean;
}

/** The full state of one in-flight or finished assistant turn. */
export interface AssistantTurn {
  status: ResponseStatus;
  /** Prose accumulated from streamed deltas (the live view). */
  text: string;
  /** Authoritative final text from `client.final` when it arrives. */
  finalText?: string;
  activities: ActivityItem[];
  /**
   * Parallel fan-outs this turn ran (`delegation.*` events), in arrival order.
   * Each carries its parts' live activity, real step counters, real tokens and
   * (when priced) real cost — never a fabricated percentage or price.
   * Absent for turns that delegated nothing.
   */
  delegations?: DelegationView[];
  /**
   * The code changes this turn made, in arrival order: one entry per path, the
   * latest preview winning (`edits` counts repeat edits). Empty for a turn
   * that changed no files.
   */
  changes: TurnChange[];
  error?: TurnError;
  /**
   * Phase 4 evidence-grounded research metadata carried on `client.final` for a
   * gated run. `undefined` for ordinary chat. Typed loosely here to keep the
   * client model free of a harness-runtime dependency; the chat surface narrows
   * it via `ResearchRunInfo`.
   */
  research?: unknown;
  /**
   * Phase 3-M Reliability: explicit memory-intent outcome + learning-eligibility
   * for an ordinary chat run. `undefined` when the turn had no memory action and
   * the eligibility metadata is absent. Typed loosely to keep the client model
   * free of a harness-runtime dependency.
   */
  memory?: unknown;
  /**
   * The harness run id for this turn. Carried so the chat surface can offer the
   * explicit "Learn from this run" action and read run-scoped diagnostics
   * (eligibility, events) from the originating turn.
   */
  runId?: string;
  /** A typed client intent from a routed command (`navigate`/`openCanvas`). */
  ui?: import('@/lib/harness/commands/command-ui').CommandUiAction;
  /**
   * The provider + model that actually served this turn (from the run record).
   * Absent until `client.final` arrives and for runs that did not report one.
   */
  model?: AssistantModel;
  /**
   * Phase 14: the run's code-verification ledger, present when the turn used the
   * code capability. Typed loosely to keep this model free of a harness-runtime
   * dependency; the chat surface narrows it via `coerceCodeVerification`.
   */
  codeVerification?: unknown;
  /** Phase 14: the narrow profile the turn was bound to (`plan_only` | `edit_verify`). */
  autonomy?: string | null;
  /** Phase 14 §6.4: the runtime-derived chat capability view. */
  capability?: unknown;
  /**
   * The generated-capability lifecycle this turn requested/built (`capability.*`
   * events): request, surface, tests, verification and activation state.
   * Typed loosely; the card narrows it via `coerceCapabilityBuild`.
   */
  capabilityBuild?: unknown;
  /**
   * Vision/Multimodal: the run's collected vision results (asset ids, regions,
   * OCR, confidence, provenance) for the right sidebar. `undefined` when the
   * turn used no vision tool. Typed loosely to avoid a harness-runtime
   * dependency in this client model; the chat surface narrows it via
   * `coerceVisionRun`.
   */
  vision?: unknown;
  /**
   * Document Intelligence: the run's collected document results (type, pages,
   * status, capabilities, entities, evidence counts). `undefined` when the turn
   * used no document tool; narrowed by the chat surface via `coerceDocumentRun`.
   */
  documents?: unknown;
  /**
   * Internet Image Search: the run's validated, normalized image results
   * (direct image URL, thumbnail, source page, dimensions) for the chat's inline
   * gallery. `undefined` when the turn used no image tool; narrowed by the chat
   * surface via `coerceGalleryBlob`.
   */
  images?: unknown;
  /**
   * Native video: the video jobs this turn queued (job + project ids) for the
   * chat's live job card (polling, playback, download, revise/regenerate).
   * `undefined` when the turn used no video tool; narrowed by the chat surface
   * via `coerceVideoBlob`.
   */
  video?: unknown;
  /**
   * A computed result the runtime worked out this turn (`analysis.completed`):
   * outputs, artifacts and provenance. `undefined` when the turn computed
   * nothing; narrowed by the chat surface via `coerceComputedResult`, which
   * renders a provenance-less value as UNAVAILABLE — never as retrieved data.
   */
  analysis?: unknown;
  /**
   * A dataset collection this turn completed (`acquire.completed`): the pull
   * manifest (source, window, rows, completeness, licence, attribution).
   * `undefined` when the turn collected nothing; narrowed by the chat surface
   * via `coerceAcquisitionManifest`, which can never round an unknown
   * completeness up to "complete".
   */
  acquisition?: unknown;
  /**
   * The action the agent is currently blocked on pending an operator decision,
   * or undefined when it is not waiting. Set from `approval.requested`, cleared
   * by `approval.resolved` or the decision POST.
   */
  pendingApproval?: PendingApproval;
  /**
   * A question the agent is paused on (`clarify`), or undefined when it is not
   * waiting. Set from `clarification.requested`, cleared by
   * `clarification.answered` or the answer POST.
   */
  pendingClarification?: PendingClarification;
  /**
   * The Intelligent Response Orchestrator's presentation plan for this turn
   * (runtime `response.plan`). Absent for runs that did not emit one.
   */
  responsePlan?: ResponsePlan;
  /** True once a terminal state has been reached. */
  done: boolean;
}

export function emptyTurn(): AssistantTurn {
  return { status: 'queued', text: '', activities: [], changes: [], done: false };
}

/** The text the UI should show: authoritative final text wins over deltas. */
export function turnText(turn: AssistantTurn): string {
  return turn.finalText && turn.finalText.length > 0 ? turn.finalText : turn.text;
}

const TERMINAL: ReadonlySet<ResponseStatus> = new Set(['completed', 'cancelled', 'error']);
export function isTerminal(status: ResponseStatus): boolean {
  return TERMINAL.has(status);
}

// ── Human labels ─────────────────────────────────────────────────────────────
// Registry ids are snake_case (`market_get_quote`) and retrieval source names
// are lowercase (`yfinance`). Neither is fit to show a user. This map is the
// one place a raw operation name becomes human language.

const TOOL_LABELS: Record<string, string> = {
  market_get_quote: 'Retrieving market quote',
  market_get_ohlcv: 'Retrieving price history',
  fundamentals_get_company_profile: 'Retrieving company profile',
  fundamentals_get_financials: 'Retrieving financial statements',
  research_search: 'Searching research sources',
  research_retrieve_source: 'Reading a research source',
  research_search_news: 'Searching current news',
  macro_get_series: 'Retrieving macro series',
  search_web: 'Searching the web',
  web_search: 'Searching the web',
  web_fetch: 'Reading a web page',
  get_stock_price: 'Retrieving stock price',
  get_stock_fundamentals: 'Retrieving fundamentals',
  get_company_news: 'Retrieving company news',
  calculate_rsi: 'Calculating RSI',
  calculate_sma: 'Calculating moving average',
  calculate_technical_indicators: 'Calculating technical indicators',
  // Repository navigation (read) and authoring (write) — plain job names, never
  // the mechanism. The durable copy lives in the i18n dictionary; this map is
  // the codebase's single place a raw operation id becomes human language.
  workspace_status: 'Checking the project',
  workspace_list: 'Listing the project',
  workspace_search: 'Searching the project',
  workspace_read: 'Reading a file',
  workspace_docs: 'Looking through the notes',
  workspace_patch_apply: 'Writing the change',
  workspace_diff: 'Showing what changed',
  workspace_revert: 'Undoing the change',
  command_run: 'Running a check',
  terminal_run: 'Running',
  scaffold_kit: 'Building a tool',
  bot_create: 'Creating the bot',
  bot_update: 'Updating the bot',
  bot_list: 'Listing the bots',
  bot_run: 'Running the bot',
  bot_pause: 'Pausing the bot',
  bot_resume: 'Resuming the bot',
  bot_delete: 'Deleting the bot',
  bot_history: 'Reading the bot history',
  analysis_run: 'Executing Python',
  execute_python: 'Executing Python',
  execute_code: 'Executing code',
  execute_javascript: 'Executing JavaScript',
  execute_typescript: 'Executing TypeScript',
  execute_shell: 'Running a command',
  execute_sql: 'Querying data',
  capability_external: 'Planning the external connection',
  capability_adapter: 'Drafting the adapter',
  create_workflow: 'Creating the workflow',
  python_env_install: 'Installing Python packages',
  python_env_remove: 'Removing Python packages',
  python_env_list: 'Checking installed packages',
  python_env_status: 'Checking the analysis environment',
  // Code Intelligence & Understanding Engine (CIUE) — plain job names for the
  // semantic/structural navigation surface. The mechanism (LSP/AST/index) never
  // reaches the user; only the job does.
  code_get_definition: 'Locating the definition',
  code_find_references: 'Finding where it is used',
  code_find_symbol: 'Finding a symbol',
  code_search_symbols: 'Searching symbols',
  code_get_hover: 'Inspecting the type',
  code_get_type: 'Inspecting the type',
  code_get_diagnostics: 'Checking for problems',
  code_get_document_symbols: 'Listing the file structure',
  code_get_callers: 'Finding callers',
  code_get_callees: 'Finding what it calls',
  code_get_call_hierarchy: 'Tracing the call path',
  code_get_implementations: 'Finding implementations',
  code_get_imports: 'Listing imports',
  code_get_exports: 'Listing exports',
  code_get_dependencies: 'Tracing dependencies',
  code_get_dependents: 'Finding dependents',
  code_get_project_structure: 'Mapping the project',
  code_get_dependency_graph: 'Mapping dependencies',
  code_parse_file: 'Reading the file structure',
  code_analyze_file: 'Analysing a file',
  code_rename_symbol: 'Renaming across the project',
  code_organize_imports: 'Reviewing imports',
  code_get_relevant_context: 'Gathering relevant code',
  code_index_workspace: 'Indexing the project',
  code_intel_status: 'Checking code understanding',
  code_find_related_tests: 'Finding related tests',
  // Vision/Multimodal — plain job names. The mechanism (which model / backend)
  // never reaches the user; only the job does.
  vision_analyze: 'Looking at the image',
  image_understand: 'Reading the image',
  vision_answer: 'Looking at the image',
  screenshot_analyze: 'Looking at the screen',
  clipboard_analyze: 'Looking at the clipboard image',
  ocr_extract: 'Reading the text in the image',
  visual_ground: 'Locating an element',
  visual_localize: 'Locating an element',
  chart_analyze: 'Reading the chart',
  document_vision: 'Reading the document',
  visual_compare: 'Comparing the images',
  image_diff: 'Comparing the images',
  screen_compare: 'Comparing the screen',
  image_metadata: 'Inspecting the image',
  vision_capabilities: 'Checking vision support',
  vision_asset_list: 'Listing images',
  vision_remember: 'Remembering a visual observation',
  image_generate: 'Generating an image',
  // Document Intelligence — plain job names.
  document_open: 'Opening the document',
  document_inspect: 'Inspecting the document',
  document_search: 'Searching the document',
  document_extract: 'Reading the document',
  document_analyze: 'Analyzing the document',
  document_query: 'Querying the document data',
  document_compare: 'Comparing the documents',
  document_render: 'Rendering the document',
  document_capabilities: 'Checking document support',
  // Large-corpus sources — reading in place (never copying the corpus).
  source_roots: 'Listing source folders',
  source_list: 'Browsing source files',
  source_open: 'Opening the source',
  source_read: 'Reading the source',
  source_lines: 'Reading the source',
  source_search: 'Searching the sources',
  source_sql: 'Querying the source data',
  source_index: 'Indexing the sources',
  source_stats: 'Checking the source catalog',
  // Document Authoring — the write side (Word / Excel / PowerPoint / text).
  document_create: 'Creating the document',
  eclipse_report: 'Creating the report',
  // Presentations — a versioned deck plus its PowerPoint derivative.
  presentation_create: 'Building the presentation',
  presentation_edit: 'Editing the presentation',
  presentation_export: 'Exporting the presentation',
  presentation_outline: 'Reading the presentation',
  presentation_import: 'Importing the presentation',
  // Native video — the render pipeline's plain job names.
  video_capabilities: 'Checking video support',
  video_create_project: 'Creating the video project',
  video_update_project: 'Revising the video project',
  video_list_projects: 'Listing video projects',
  video_validate_project: 'Checking the video project',
  video_resolve_data: 'Resolving the video’s market data',
  video_preview: 'Previewing the video',
  video_render_draft: 'Rendering a draft video',
  video_render_final: 'Rendering the final video',
  video_render_repair: 'Repairing the video render',
  video_inspect: 'Inspecting the video frames',
  video_job_status: 'Checking the video job',
  video_list_jobs: 'Listing video jobs',
  video_cancel_job: 'Cancelling the video job',
};

/** Turn a raw tool name into a short, human label (fallback prettifies it). */
export function toolLabel(name: string): string {
  const known = TOOL_LABELS[name];
  if (known) return known;
  const words = name.replace(/[._]+/g, ' ').trim();
  if (!words) return 'Using a tool';
  return `Using ${words}`;
}

/** Turn a retrieval source name into a display label. */
export function sourceLabel(name: string): string {
  if (!name) return 'source';
  const known: Record<string, string> = {
    jdds: 'market data',
    yfinance: 'Yahoo Finance',
    sec: 'SEC EDGAR',
    web: 'web sources',
    cached: 'cache',
  };
  return known[name.toLowerCase()] ?? name;
}

/**
 * Turn a raw tool name into a short, human label for a *running* tool. This is
 * the label shown on the activity card; `toolLabel` also serves completed
 * items, so the wording stays stable across the transition.
 */
export function toolRunningLabel(name: string): string {
  return toolLabel(name);
}
