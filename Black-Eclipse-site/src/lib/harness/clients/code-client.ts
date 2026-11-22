// ─── Code capability front door (Phases 4–5) ────────────────────────────────
// Thin, typed proxy over the Python runtime's /code routes plus the read-only
// `codeVerification` blob types. This module adds NO agentic logic (mirrors
// python-client.ts): it validates, forwards, and returns what the runtime owns.
// Operator-gated routes present the runtime secret by transport/token, never a
// body boolean.

import { HARNESS_PY_URL, ensurePythonRuntime } from "../python-runtime";
import { runtimeCallbackToken } from "../runtime-auth";
import { HarnessRuntimeUnavailable } from "./python-client";
import { redactSecrets } from "../errors";

export type Autonomy = "plan_only" | "edit_verify" | "approve_external";
export type TaskStatus =
  | "created" | "plan_review" | "completed" | "failed" | "cancelled" | "discarded";

export interface CodePlan {
  planDigest: string;
  steps: Array<{ id: string; description: string; files: string[] }>;
  files: string[];
  checkClasses: string[];
}

export interface CodeTask {
  id: string;
  status: TaskStatus;
  autonomy: Autonomy;
  workspaceId: string | null;
  query: string;
  plan: CodePlan | null;
  verification: CodeVerification | null;
  repair: CodeVerificationRepair | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface CodeApproval {
  id: string;
  kind: string;
  workspaceId: string | null;
  serverId: string;
  effect: string;
  status: string;
  actionSummary: string | null;
  createdAt: string;
}

export interface CreateTaskInput {
  query: string;
  autonomy: Autonomy;
  workspaceId?: string;
  /** Phase 23: the approved project root this build belongs to (per-chat id). */
  workspaceRootId?: string;
  modelSettings: Record<string, unknown>;
  callbackBaseUrl?: string;
  callbackToken?: string;
  /** Edit the selected real folder directly instead of a disposable copy. */
  direct?: boolean;
  /** Phase 22 / S2: the build session's budget profile (default "standard"). */
  budgetProfile?: BudgetProfile;
  /** What "done" means, in the caller's words; recorded with the session. */
  acceptance?: string[];
  /** Gap #7b: bounded reference images (screenshots/designs) for the task. */
  images?: import("@/lib/harness/types").RunImageInput[];
}

export type BudgetProfile = "small" | "standard" | "deep";

// ── Running the user's project (Phase 24 / P2) ───────────────────────────────
export interface CodePreview {
  status: "idle" | "ok" | "refused" | "unavailable" | "failed";
  code?: string;
  reason?: string;
  taskKey?: string;
  serviceId?: string;
  processId?: string;
  command?: string;
  label?: string;
  root?: string;
  url?: string;
  port?: number;
  ready?: boolean;
  running?: boolean;
  source?: string;
  processState?: string;
  logs?: string;
  plan?: Record<string, unknown>;
}

export interface ProjectTarget {
  taskId?: string;
  rootId?: string;
}

export type PreviewTarget = ProjectTarget;

/** The onboarding project brief shown before the first build. */
export interface ProjectBrief {
  available: boolean;
  reason?: string;
  name?: string;
  path?: string;
  stack?: string[];
  git?: { available: boolean; branch: string | null; dirty: number };
  run?: { command: string; source: string };
  checks?: Array<{ id: string; label: string }>;
  checksSource?: string;
  warnings?: string[];
  hasRules?: boolean;
  isHarnessRepo?: boolean;
}

export async function getProjectBrief(target: ProjectTarget = {}): Promise<ProjectBrief> {
  const query = new URLSearchParams();
  if (target.taskId) query.set("taskId", target.taskId);
  if (target.rootId) query.set("rootId", target.rootId);
  const body = await request<{ project: ProjectBrief }>(`/code/project?${query.toString()}`, {
    method: "GET",
  });
  return body.project;
}

/** Index the selected project so symbol search and repo context are ready. */
export async function prepareProjectIndex(): Promise<Record<string, unknown>> {
  return request("/code/intel/index", { method: "POST", timeoutMs: 300_000 });
}

export async function getCodePreview(target: PreviewTarget = {}): Promise<CodePreview> {
  const query = new URLSearchParams();
  if (target.taskId) query.set("taskId", target.taskId);
  if (target.rootId) query.set("rootId", target.rootId);
  const body = await request<{ preview: CodePreview }>(`/code/preview?${query.toString()}`, {
    method: "GET",
  });
  return body.preview;
}

export async function startCodePreview(target: PreviewTarget = {}): Promise<CodePreview> {
  const body = await request<{ preview: CodePreview }>("/code/preview/start", {
    method: "POST",
    body: JSON.stringify(target),
    // Starting a dev server waits for a listening port (bounded server-side).
    timeoutMs: 90_000,
  });
  return body.preview;
}

export async function stopCodePreview(target: PreviewTarget = {}): Promise<CodePreview> {
  const body = await request<{ preview: CodePreview }>("/code/preview/stop", {
    method: "POST",
    body: JSON.stringify(target),
  });
  return body.preview;
}

export async function getCodePreviewLogs(target: PreviewTarget = {}, tail = 200): Promise<string> {
  const query = new URLSearchParams({ tail: String(tail) });
  if (target.taskId) query.set("taskId", target.taskId);
  if (target.rootId) query.set("rootId", target.rootId);
  const body = await request<{ preview: CodePreview }>(`/code/preview/logs?${query.toString()}`, {
    method: "GET",
  });
  return body.preview.logs ?? "";
}

export interface LandCheck {
  id: string;
  label: string;
  kind: string;
  argv: string[];
  required: boolean;
  timeoutSeconds: number;
  when: string;
}

export interface LandPlan {
  changed: string[];
  checks: LandCheck[];
  source: string;
  warnings: string[];
}

export interface LandCheckResult {
  id: string;
  label: string;
  status: "passed" | "failed" | "error" | "skipped";
  exitCode: number | null;
  outputDigest: string;
  outputTail: string;
  required: boolean;
  durationMs: number;
}

export interface LandResult {
  ok: boolean;
  failed: string | null;
  results: LandCheckResult[];
}

export interface CodeScorecard {
  windowDays: number;
  checkedAt: string;
  metrics: {
    runs: number;
    builds: number;
    checked: number;
    firstPassChecks: number;
    landed: number;
    rolledBack: number;
    successRate: number | "UNKNOWN";
    rollbackRate: number | "UNKNOWN";
    firstPassRate: number | "UNKNOWN";
    averageSteps: number | "UNKNOWN";
    totalTokens: number | "UNKNOWN";
    medianTimeToFirstChangeSeconds: number | "UNKNOWN";
    costUsd: "UNKNOWN";
    humanEditsAfterLanding: "UNKNOWN";
  };
  byModel?: Array<{
    provider: string;
    model: string;
    builds: number;
    checked: number;
    firstPassChecks: number;
    landed: number;
    rolledBack: number;
  }>;
  unmeasured: string[];
}

/** Phase 22 / S6: the honest self-build scorecard. */
export async function getCodeScorecard(
  days = 30, target: ProjectTarget = {},
): Promise<CodeScorecard> {
  const query = new URLSearchParams({ days: String(days) });
  if (target.taskId) query.set("taskId", target.taskId);
  if (target.rootId) query.set("rootId", target.rootId);
  return request(`/code/scorecard?${query.toString()}`, { method: "GET" });
}

/** Gap #8: the coding plan the model recorded for this run. Read-only. */
export interface CodeTodo {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
}

export interface CodeTodoView {
  todos: CodeTodo[];
  summary: {
    total: number;
    counts: Record<string, number>;
    active: string[];
  };
  version: string;
}

export async function getCodeTodos(id: string): Promise<CodeTodoView> {
  return request(`/code/tasks/${encodeURIComponent(id)}/todos`, { method: "GET" });
}

/** Phase 22 / S4: the ordered checks a change must pass. Read-only. */
export async function getLandPlan(id: string): Promise<LandPlan> {
  const body = await request<{ plan: LandPlan }>(
    `/code/tasks/${encodeURIComponent(id)}/land`,
    { method: "GET" },
  );
  return body.plan;
}

/** Run the landing checks. `full` includes the long test suite. */
export async function verifyLanding(
  id: string, full = false,
): Promise<{ result: LandResult; previewDigest: string | null; checkpointId: string }> {
  return request(
    `/code/tasks/${encodeURIComponent(id)}/land/verify?full=${full ? "1" : "0"}`,
    { method: "POST", timeoutMs: 2_000_000 },
  );
}

export interface CodeSession {
  id: string;
  goal: string;
  acceptance: string[];
  budget: { profile?: string; maxSteps?: number; runTimeoutSeconds?: number };
  workspaceId: string | null;
  status: string;
  phase: string;
  nextAction: string;
  land: { ok: boolean; full: boolean; failed: string | null; at: string | null } | null;
  progress: {
    boundaries: number;
    lastBoundary: string | null;
    checkpoints: number;
    createdAt: string | null;
  };
  resume: { resumable: boolean; code: string | null; checkpointId: string | null };
}

async function request<T>(path: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const healthy = await ensurePythonRuntime();
  if (!healthy) {
    throw new HarnessRuntimeUnavailable(
      "The Python agent runtime is not available. Install the runtime: npm run setup:python",
    );
  }
  const { timeoutMs = 120_000, ...rest } = init;
  const operatorToken = runtimeCallbackToken();
  const res = await fetch(`${HARNESS_PY_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(operatorToken ? { "X-Harness-Operator": operatorToken } : {}),
      ...(rest.headers ?? {}),
    },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    // A Python error detail may be a string OR a structured object
    // (`{ code, message }`); never pass a non-string to redactSecrets.
    const raw = (body as { detail?: unknown }).detail;
    const detail =
      typeof raw === "string"
        ? raw
        : raw && typeof raw === "object" && "message" in raw
          ? String((raw as { message: unknown }).message)
          : `runtime HTTP ${res.status}`;
    throw new Error(redactSecrets(detail));
  }
  return body as T;
}

export async function listCodeTasks(): Promise<CodeTask[]> {
  const body = await request<{ tasks: CodeTask[] }>("/code/tasks", { method: "GET" });
  return body.tasks ?? [];
}

export async function getCodeTask(id: string): Promise<CodeTask> {
  const body = await request<{ task: CodeTask }>(`/code/tasks/${encodeURIComponent(id)}`, { method: "GET" });
  return body.task;
}

/** Phase 22 / S2: the durable session lens for a code task (goal, budget, resume). */
export async function getCodeSession(id: string): Promise<CodeSession | null> {
  const body = await request<{ session: CodeSession | null }>(
    `/code/tasks/${encodeURIComponent(id)}`,
    { method: "GET" },
  );
  return body.session ?? null;
}

/** One runtime read for both halves, so the front door does not call twice. */
export async function getCodeTaskWithSession(
  id: string,
): Promise<{ task: CodeTask; session: CodeSession | null }> {
  const body = await request<{ task: CodeTask; session: CodeSession | null }>(
    `/code/tasks/${encodeURIComponent(id)}`,
    { method: "GET" },
  );
  return { task: body.task, session: body.session ?? null };
}

export async function createCodeTask(input: CreateTaskInput): Promise<CodeTask> {
  const body = await request<{ task: CodeTask }>("/code/tasks", {
    method: "POST",
    body: JSON.stringify(input),
    timeoutMs: 600_000,
  });
  return body.task;
}

export async function cancelCodeTask(id: string): Promise<{ cancelled: boolean; worktreeReleased: boolean }> {
  return request(`/code/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export async function discardCodeTask(id: string): Promise<{ discarded: boolean }> {
  return request(`/code/tasks/${encodeURIComponent(id)}/discard`, { method: "POST" });
}

export async function acceptCodePlan(id: string, steps: CodePlan["steps"], checkClasses: string[]): Promise<unknown> {
  return request(`/code/tasks/${encodeURIComponent(id)}/plan/accept`, {
    method: "POST",
    body: JSON.stringify({ steps, checkClasses }),
  });
}

export interface CodeTaskDiff {
  available: boolean;
  unified: string;
  digest: string;
  filesChanged: number;
  added: string[];
  modified: string[];
  deleted: string[];
  /** Which source produced this change: working | committed | turn. */
  mode?: string;
  base?: string;
  /** Absolute workspace folder, for "reveal in file manager". */
  workspaceRoot?: string;
  /** Present when the requested source has no data (e.g. no turn snapshot). */
  reason?: string;
  truncated?: boolean;
}

/** The change a task made, in everyday words plus the raw text behind "details". */
export async function getCodeTaskDiff(id: string): Promise<CodeTaskDiff | null> {
  try {
    const body = await request<{ diff: CodeTaskDiff }>(
      `/code/tasks/${encodeURIComponent(id)}/diff`,
      { method: "GET", timeoutMs: 30_000 },
    );
    return body.diff ?? null;
  } catch {
    // A task with no bound work area has no diff; that is not an error.
    return null;
  }
}

export async function resumeCodeTask(id: string): Promise<unknown> {
  return request(`/code/tasks/${encodeURIComponent(id)}/resume`, { method: "POST" });
}

// ─── Promotion: put a reviewed change into the real project (Phase 15) ──────
// A task's change lives in a disposable copy. `previewPromotion` names exactly
// which files would change in the user's real folder and records a single-use
// approval; `applyPromotion` writes them — only if neither copy changed since the
// preview. The runtime owns the decision; this module only carries it.

export interface PromotionHunk {
  id: string;
  header: string;
  beforeStart: number;
  beforeLines: number;
  afterStart: number;
  afterLines: number;
  preview: { kind: "add" | "remove"; text: string }[];
}

export interface PromotionFile {
  path: string;
  op: "create" | "overwrite" | "delete";
  beforeHash: string | null;
  afterHash: string | null;
  /** Gap #7: per-file change slices a reviewer can accept independently. */
  hunks?: PromotionHunk[];
  /** Present on a partially accepted file: the hunk IDs that will land. */
  selectedHunks?: string[];
  partial?: boolean;
}

export interface PromotionPreview {
  workspaceId: string;
  runId: string;
  checkoutRoot: string;
  strategy: string;
  files: PromotionFile[];
  filesChanged: number;
  added: number;
  modified: number;
  deleted: number;
  previewDigest: string;
  unified: string;
  truncated: boolean;
}

export interface PromotionApproval {
  id: string;
  status: string;
  actionSummary: string | null;
}

/** Self-hosting verdict for a promoted change (Track B). */
export interface PromotionLanding {
  restartRequired: boolean;
  buildRequired: boolean;
  authorities: string[];
}

/** Preview exactly what a task's change would put into the real project. */
export async function previewPromotion(
  taskId: string, include: string[] = [], hunks: string[] = [],
): Promise<{ preview: PromotionPreview; approval: PromotionApproval | null; landing?: PromotionLanding }> {
  return request(`/code/tasks/${encodeURIComponent(taskId)}/promote/preview`, {
    method: "POST",
    body: JSON.stringify({ include, hunks }),
    timeoutMs: 60_000,
  });
}

/** Trust Mode: apply, verify and auto-undo in one call. */
export interface AutolandResult {
  ok: boolean;
  applied: boolean;
  rolledBack: boolean;
  rollbackId?: string | null;
  checks?: { ok?: boolean; failed?: string[]; error?: string } & Record<string, unknown>;
  preview?: PromotionPreview;
}

export async function autolandTask(taskId: string, full = false): Promise<AutolandResult> {
  return request(`/code/tasks/${encodeURIComponent(taskId)}/autoland${full ? "?full=true" : ""}`, {
    method: "POST",
    timeoutMs: full ? 900_000 : 180_000,
  });
}

/** Confirm a previewed change and put it into the real project. */
export async function applyPromotion(
  taskId: string,
  approvalId: string,
  previewDigest: string,
  include: string[] = [],
  hunks: string[] = [],
): Promise<{ ok: boolean; filesChanged: number; previewDigest: string; rollbackId?: string | null; landing?: PromotionLanding }> {
  const body = await request<{ promotion: { ok: boolean; filesChanged: number; previewDigest: string; rollbackId?: string | null }; landing?: PromotionLanding }>(
    `/code/tasks/${encodeURIComponent(taskId)}/promote`,
    {
      method: "POST",
      body: JSON.stringify({ approvalId, previewDigest, include, hunks }),
      timeoutMs: 120_000,
    },
  );
  return { ...body.promotion, landing: body.landing };
}

/** The bounded post-landing probe record (restart → health → keep/rollback). */
export interface PostLandingRecord {
  id: string;
  runId: string;
  activationId: string | null;
  rollbackId: string;
  status:
    | "running"
    | "healthy"
    | "rolled_back"
    | "rollback_failed"
    | "restart_failed"
    | "unverified";
  reason: string;
  evidence: Record<string, unknown>;
  startedAt: string;
  finishedAt: string | null;
  createdAt: string;
}

/**
 * Probe an RSL landing after the front door restarted the runtime.
 * `alreadyRestarted` is the supervisor protocol: probe now, roll back on failure.
 */
export async function postLandingProbe(
  rollbackId: string,
  runId: string,
  alreadyRestarted = true,
  activationId?: string,
): Promise<PostLandingRecord> {
  const body = await request<{ postLanding: PostLandingRecord }>(
    `/code/promotions/${encodeURIComponent(rollbackId)}/post-landing`,
    {
      method: "POST",
      body: JSON.stringify({ runId, activationId, alreadyRestarted }),
      timeoutMs: 180_000,
    },
  );
  return body.postLanding;
}

/** Put a previously-promoted change back — the exact original contents. */
export async function rollbackPromotion(
  rollbackId: string, runId?: string,
): Promise<{ ok: boolean; rollbackId: string; restored: string[]; filesChanged: number }> {
  const query = runId ? `?runId=${encodeURIComponent(runId)}` : "";
  const body = await request<{ rollback: { ok: boolean; rollbackId: string; restored: string[]; filesChanged: number } }>(
    `/code/promotions/${encodeURIComponent(rollbackId)}/rollback${query}`,
    { method: "POST", timeoutMs: 120_000 },
  );
  return body.rollback;
}

export async function listCodeApprovals(): Promise<CodeApproval[]> {
  const body = await request<{ approvals: CodeApproval[] }>("/code/approvals", { method: "GET" });
  return body.approvals ?? [];
}

export async function decideCodeApproval(id: string, approve: boolean, reason = ""): Promise<unknown> {
  return request(`/code/approvals/${encodeURIComponent(id)}/decide`, {
    method: "POST",
    body: JSON.stringify({ approve, reason }),
  });
}

// ─── Chat request classification (Phase 14) ────────────────────────────────
// The classifier lives in the runtime; the front door calls it rather than
// re-implementing the rules (one runtime, ADR-H117). Both routes are read-only.

export type CodeIntentVerdict = "code_change" | "code_question" | "data_task" | "app_control" | "not_code";

export interface CodeIntent {
  verdict: CodeIntentVerdict;
  confidence: "certain" | "likely";
  reason: string;
  signals: string[];
}

export interface ShellResult {
  command: string;
  cwd: string;
  exitCode: number | null;
  stdout: string;
  stderr: string;
  durationMs: number;
  timedOut: boolean;
  truncated: boolean;
  status: string;
}

export async function runShell(
  command: string,
  opts: { cwd?: string; timeoutS?: number } = {},
): Promise<ShellResult> {
  const body = await request<{ success: boolean; result: ShellResult }>("/code/shell", {
    method: "POST",
    body: JSON.stringify({ command, cwd: opts.cwd, timeoutS: opts.timeoutS }),
    timeoutMs: (opts.timeoutS ?? 120) * 1000 + 10_000,
  });
  return body.result;
}

export async function classifyCodeIntent(
  query: string,
  history: Array<{ role: "user" | "assistant"; content: string }> = [],
): Promise<CodeIntent> {
  const body = await request<{ intent: CodeIntent }>("/code/intent", {
    method: "POST",
    body: JSON.stringify({ query, history }),
    timeoutMs: 10_000,
  });
  return body.intent;
}

/** The runtime's server-resolved code root. Read-only; used by proxy routes. */
export async function getCodeRoot(): Promise<string | null> {
  try {
    const body = await request<{ root: string | null; available: boolean }>("/code/root", {
      method: "GET",
      timeoutMs: 10_000,
    });
    return body.available ? body.root : null;
  } catch {
    return null;
  }
}



// ─── Workspace folders (folder picker) ─────────────────────────────────────
// Thin proxy over the runtime's /workspace routes. Approving a folder is the one
// way a new root becomes workable; selecting it makes it the active workspace.

export interface BrowseEntry {
  name: string;
  path: string;
  isDir: boolean;
  hidden: boolean;
}

export interface BrowseResult {
  path: string | null;
  parent: string | null;
  entries: BrowseEntry[];
  truncated: boolean;
  reason: string | null;
}

export interface TrustedRoot {
  id: string;
  path: string;
  label: string | null;
  createdAt: string;
  lastUsedAt: string | null;
  selected: boolean;
}

export async function browseFolders(path?: string): Promise<BrowseResult> {
  const q = path ? `?path=${encodeURIComponent(path)}` : "";
  const body = await request<BrowseResult & { success: boolean }>(`/workspace/browse${q}`, {
    method: "GET",
    timeoutMs: 15_000,
  });
  return body;
}

export async function listTrustedRoots(): Promise<TrustedRoot[]> {
  const body = await request<{ roots: TrustedRoot[] }>("/workspace/roots", { method: "GET" });
  return body.roots ?? [];
}

export interface WorkspaceRoots {
  roots: TrustedRoot[];
  /**
   * The server-resolved folder a run uses when no per-chat root is bound:
   * a selected trusted root, an operator override, or the built-in repo.
   * Read-only context for the UI — it is not itself an approved root id.
   */
  defaultRoot: string | null;
}

/** Approved roots plus the default (unbound) workspace folder. */
export async function getWorkspaceRoots(): Promise<WorkspaceRoots> {
  const body = await request<{ roots: TrustedRoot[]; defaultRoot?: string | null }>(
    "/workspace/roots",
    { method: "GET" },
  );
  return {
    roots: body.roots ?? [],
    defaultRoot: typeof body.defaultRoot === "string" ? body.defaultRoot : null,
  };
}

/** Create one folder inside the directory the picker is showing. */
export async function createBrowsedFolder(
  path: string, name: string,
): Promise<{ path: string; name: string; parent: string }> {
  const body = await request<{ success: boolean; path: string; name: string; parent: string }>(
    "/workspace/browse/mkdir",
    { method: "POST", body: JSON.stringify({ path, name }), timeoutMs: 20_000 },
  );
  return body;
}

export async function approveTrustedRoot(path: string, label?: string): Promise<TrustedRoot> {
  const body = await request<{ root: TrustedRoot }>("/workspace/roots", {
    method: "POST",
    body: JSON.stringify({ path, label }),
  });
  return body.root;
}

export async function selectTrustedRoot(rootId: string | null): Promise<TrustedRoot | null> {
  const body = await request<{ selected: TrustedRoot | null }>("/workspace/roots/select", {
    method: "POST",
    body: JSON.stringify({ rootId }),
  });
  return body.selected;
}

export async function revokeTrustedRoot(rootId: string): Promise<void> {
  await request(`/workspace/roots/${encodeURIComponent(rootId)}`, { method: "DELETE" });
}

// ─── Editor: file tree + read + write ───────────────────────────────────────

export interface EditorEntry {
  name: string;
  path: string;
  isDir: boolean;
}

export interface EditorTreeResult {
  root: string;
  path: string;
  entries: EditorEntry[];
  truncated: boolean;
}

export interface EditorFileResult {
  path: string;
  content: string;
  redacted: boolean;
  lines: number;
}

export interface WorkspaceFileHit {
  name: string;
  path: string;
  bytes: number | null;
}

export interface WorkspaceSearchResult {
  root: string;
  query: string;
  files: WorkspaceFileHit[];
  truncated: boolean;
}

function rootQuery(rootId?: string | null): string {
  return rootId ? `&rootId=${encodeURIComponent(rootId)}` : "";
}

export async function listWorkspaceFiles(path = ".", rootId?: string | null): Promise<EditorTreeResult> {
  const body = await request<EditorTreeResult & { success: boolean }>(
    `/workspace/files?path=${encodeURIComponent(path)}${rootQuery(rootId)}`,
    { method: "GET", timeoutMs: 20_000 },
  );
  return body;
}

export async function searchWorkspaceFiles(
  query: string,
  rootId?: string | null,
  limit = 50,
): Promise<WorkspaceSearchResult> {
  const body = await request<WorkspaceSearchResult & { success: boolean }>(
    `/workspace/files/search?q=${encodeURIComponent(query)}&limit=${limit}${rootQuery(rootId)}`,
    { method: "GET", timeoutMs: 20_000 },
  );
  return body;
}

export async function readWorkspaceFile(path: string, rootId?: string | null): Promise<EditorFileResult> {
  const body = await request<EditorFileResult & { success: boolean }>("/workspace/file/read", {
    method: "POST",
    body: JSON.stringify({ path, ...(rootId ? { rootId } : {}) }),
    timeoutMs: 20_000,
  });
  return body;
}

export async function writeWorkspaceFile(path: string, content: string): Promise<{ path: string; bytes: number }> {
  const body = await request<{ success: boolean; path: string; bytes: number }>("/workspace/file/write", {
    method: "POST",
    body: JSON.stringify({ path, content }),
    timeoutMs: 30_000,
  });
  return { path: body.path, bytes: body.bytes };
}

export async function makeWorkspaceFolder(path: string): Promise<{ path: string }> {
  const body = await request<{ success: boolean; path: string }>("/workspace/file/mkdir", {
    method: "POST",
    body: JSON.stringify({ path }),
    timeoutMs: 20_000,
  });
  return { path: body.path };
}

export async function deleteWorkspacePath(path: string): Promise<{ path: string; removed: boolean }> {
  const body = await request<{ success: boolean; path: string; removed: boolean }>("/workspace/file/delete", {
    method: "POST",
    body: JSON.stringify({ path }),
    timeoutMs: 20_000,
  });
  return { path: body.path, removed: body.removed };
}

// ─── Code-verification wire types (Phase 4) ─────────────────────────────────
// Mirror of the Python `code/verify.py` shapes. This module holds NO agentic
// logic — the panel renders what the runtime recorded, and never upgrades an
// unsupported claim.

export type CheckClaimKind = "tests_pass" | "typecheck_clean" | "build_ok" | "lint_clean";

export interface CodeVerificationRecord {
  commandExecutionId: string;
  commandClass: string;
  exitCode: number;
  durationMs: number;
  outputDigest: string;
  argv: string[];
  startedAt: string;
  workspaceId: string | null;
}

export interface CodeVerificationClaim {
  sentence: string;
  kind: CheckClaimKind;
  matchedClasses: string[];
  supported: boolean;
  evidenceExecutionId: string | null;
  evidenceDigest: string | null;
  evidenceExitCode?: number | null;
}

export interface CodeVerificationRepair {
  status: "passed" | "unrepairable" | "budget_exhausted" | "refused" | "no_checks";
  repairAttempts: number;
  maxAttempts: number;
  failedClass: string | null;
  failedOutputDigest: string | null;
  stoppedReason: string | null;
}

export interface CodeVerification {
  runId: string;
  records: CodeVerificationRecord[];
  claims: CodeVerificationClaim[];
  unverifiedCount: number;
  repair: CodeVerificationRepair | null;
  truncated: boolean;
}

/**
 * The ONLY place a claim's status word is chosen. A claim that is not supported
 * can never be reported as verified — the panel calls this, so "verified" and an
 * unsupported claim can never coincide.
 */
export function claimStatusKey(supported: boolean): 'code.proof.verified' | 'code.proof.notChecked' {
  return supported ? 'code.proof.verified' : 'code.proof.notChecked';
}

/** Narrow an unknown run field to a CodeVerification blob, or null. */
export function coerceCodeVerification(raw: unknown): CodeVerification | null {
  if (raw === null || typeof raw !== "object") return null;
  const blob = raw as Record<string, unknown>;
  const records = Array.isArray(blob.records) ? (blob.records as CodeVerificationRecord[]) : [];
  const claims = Array.isArray(blob.claims) ? (blob.claims as CodeVerificationClaim[]) : [];
  if (records.length === 0 && claims.length === 0 && !blob.repair) return null;
  return {
    runId: String(blob.runId ?? ""),
    records,
    claims,
    unverifiedCount: Number(blob.unverifiedCount ?? 0),
    repair: (blob.repair as CodeVerificationRepair | null) ?? null,
    truncated: Boolean(blob.truncated),
  };
}

// -- W7: file checkpoints + /rollback -----------------------------------------

export interface FileCheckpoint {
  id: string;
  commit: string;
  reason?: string;
  runId?: string;
  turnKey?: string;
  createdAt?: string;
  files?: number;
}

export interface CheckpointRestorePreview {
  checkpointId: string;
  all: boolean;
  changes: Array<{ status: string; path: string }>;
  count: number;
  diff: string;
}

export async function listFileCheckpoints(limit = 50): Promise<FileCheckpoint[]> {
  const body = await request<{ checkpoints: FileCheckpoint[] }>(
    `/code/checkpoints?limit=${encodeURIComponent(String(limit))}`,
    { method: "GET" },
  );
  return body.checkpoints ?? [];
}

export async function diffFileCheckpoint(id: string): Promise<{ unified: string; truncated: boolean }> {
  return request(`/code/checkpoints/${encodeURIComponent(id)}/diff`, { method: "GET" });
}

export async function previewFileRestore(
  id: string,
  input: { paths?: string[]; all?: boolean } = {},
): Promise<CheckpointRestorePreview> {
  return request(`/code/checkpoints/${encodeURIComponent(id)}/restore/preview`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function restoreFileCheckpoint(
  id: string,
  input: { paths?: string[]; all?: boolean } = {},
): Promise<{ restored: string[]; skipped: string[]; preRestoreId: string | null }> {
  return request(`/code/checkpoints/${encodeURIComponent(id)}/restore`, {
    method: "POST",
    body: JSON.stringify(input),
    timeoutMs: 120_000,
  });
}
