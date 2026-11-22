// ─── Code capability BROWSER client ──────────────────────────────────────────
// The UI talks to the Next.js API routes (`/api/code/**`), which proxy to the
// Python runtime. This module MUST stay client-safe: no `child_process`, no `fs`,
// no `ensurePythonRuntime` (that lives in the server-only `code-client.ts`). Fetch
// only. Without this split the Phase-5 screens cannot be mounted from a client
// component — importing the server client pulls `child_process` into the browser
// bundle and the page fails to compile.

import type { Autonomy, BudgetProfile, CodeApproval, CodePlan, CodePreview, CodeScorecard, CodeSession, CodeTask, CodeTaskDiff, CodeTodo, CodeTodoView, LandCheck, LandCheckResult, LandPlan, LandResult, ProjectBrief, ProjectTarget, PreviewTarget, TaskStatus, CreateTaskInput, PromotionFile, PromotionHunk, PromotionPreview, PromotionLanding, PostLandingRecord } from './code-client';

// Re-export the wire types so a component imports everything from ONE client-safe
// module (a type-only re-export is erased at compile time — no server code ships).
export type { Autonomy, BudgetProfile, CodeApproval, CodePlan, CodePreview, CodeScorecard, CodeSession, CodeTask, CodeTaskDiff, CodeTodo, CodeTodoView, LandCheck, LandCheckResult, LandPlan, LandResult, ProjectBrief, ProjectTarget, PreviewTarget, TaskStatus, CreateTaskInput, PromotionFile, PromotionHunk, PromotionPreview, PromotionLanding, PostLandingRecord };

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  });
  const body = (await res.json().catch(() => ({}))) as T & { error?: string; detail?: string };
  if (!res.ok) throw new Error(body.error ?? body.detail ?? `request failed (${res.status})`);
  return body;
}

/**
 * The loopback base for the workspace picker. A page served from `localhost`
 * uses `127.0.0.1` (a separate connection pool) so a wedged localhost keep-alive
 * socket cannot freeze folder browsing. Other hosts use same-origin paths.
 */
function workspaceBase(): string {
  if (typeof window !== 'undefined' && window.location.hostname === 'localhost') {
    return `http://127.0.0.1:${window.location.port || '3000'}`;
  }
  return '';
}

export async function listCodeTasks(): Promise<CodeTask[]> {
  const body = await api<{ tasks: CodeTask[] }>('/api/code/tasks');
  return body.tasks ?? [];
}

export async function getCodeTask(id: string): Promise<CodeTask> {
  const body = await api<{ task: CodeTask }>(`/api/code/tasks/${encodeURIComponent(id)}`);
  return body.task;
}

/** Phase 22 / S2: the build session's goal, budget and resume state. */
export async function getCodeSession(id: string): Promise<CodeSession | null> {
  const body = await api<{ session: CodeSession | null }>(
    `/api/code/tasks/${encodeURIComponent(id)}`,
  );
  return body.session ?? null;
}

/** Phase 22 / S4: the checks this change must pass. Read-only. */
export async function getLandPlan(id: string): Promise<LandPlan> {
  const body = await api<{ plan: LandPlan }>(`/api/code/tasks/${encodeURIComponent(id)}/land`);
  return body.plan;
}

/** Run the checks. `full` includes the long test suite. */
export async function verifyLanding(
  id: string, full = false,
): Promise<{ result: LandResult; previewDigest: string | null; checkpointId: string }> {
  return api(`/api/code/tasks/${encodeURIComponent(id)}/land/verify`, {
    method: 'POST',
    body: JSON.stringify({ full }),
  });
}

/** Phase 22 / S6: the self-build scorecard, optionally for ONE project. */
export async function getCodeScorecard(days = 30, target: ProjectTarget = {}): Promise<CodeScorecard> {
  const query = new URLSearchParams({ days: String(days) });
  if (target.taskId) query.set('taskId', target.taskId);
  if (target.rootId) query.set('rootId', target.rootId);
  const body = await api<{ scorecard: CodeScorecard }>(`/api/code/scorecard?${query.toString()}`);
  return body.scorecard;
}

export async function getCodeTodos(id: string): Promise<CodeTodoView> {
  return api(`/api/code/tasks/${encodeURIComponent(id)}/todos`, { method: 'GET' });
}

// ── Running the user's project (Phase 24 / P2) ───────────────────────────────

function previewQuery(target: PreviewTarget): string {
  const query = new URLSearchParams();
  if (target.taskId) query.set('taskId', target.taskId);
  if (target.rootId) query.set('rootId', target.rootId);
  return query.toString();
}

export async function getProjectBrief(target: ProjectTarget = {}): Promise<ProjectBrief> {
  const query = new URLSearchParams();
  if (target.taskId) query.set('taskId', target.taskId);
  if (target.rootId) query.set('rootId', target.rootId);
  const body = await api<{ project: ProjectBrief }>(`/api/code/project?${query.toString()}`);
  return body.project;
}

export async function prepareProjectIndex(): Promise<Record<string, unknown>> {
  return api('/api/code/intel', { method: 'POST' });
}

export async function getCodePreview(target: PreviewTarget = {}): Promise<CodePreview> {
  const body = await api<{ preview: CodePreview }>(`/api/code/preview?${previewQuery(target)}`);
  return body.preview;
}

export async function startCodePreview(target: PreviewTarget = {}): Promise<CodePreview> {
  const body = await api<{ preview: CodePreview }>('/api/code/preview', {
    method: 'POST',
    body: JSON.stringify({ action: 'start', ...target }),
  });
  return body.preview;
}

export async function stopCodePreview(target: PreviewTarget = {}): Promise<CodePreview> {
  const body = await api<{ preview: CodePreview }>('/api/code/preview', {
    method: 'POST',
    body: JSON.stringify({ action: 'stop', ...target }),
  });
  return body.preview;
}

export async function getCodePreviewLogs(target: PreviewTarget = {}, tail = 200): Promise<string> {
  const body = await api<{ logs: string }>(
    `/api/code/preview?logs=1&tail=${tail}&${previewQuery(target)}`,
  );
  return body.logs ?? '';
}

export async function createCodeTask(input: CreateTaskInput): Promise<CodeTask> {
  const body = await api<{ task: CodeTask }>('/api/code/tasks', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return body.task;
}

export async function cancelCodeTask(id: string): Promise<{ cancelled: boolean; worktreeReleased: boolean }> {
  return api(`/api/code/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
}

export async function discardCodeTask(id: string): Promise<{ discarded: boolean }> {
  return api(`/api/code/tasks/${encodeURIComponent(id)}/discard`, { method: 'POST' });
}

export async function resumeCodeTask(id: string): Promise<unknown> {
  return api(`/api/code/tasks/${encodeURIComponent(id)}/resume`, { method: 'POST' });
}

export async function acceptCodePlan(id: string, steps: CodePlan['steps'], checkClasses: string[]): Promise<unknown> {
  return api(`/api/code/tasks/${encodeURIComponent(id)}/plan`, {
    method: 'POST',
    body: JSON.stringify({ steps, checkClasses }),
  });
}

export async function listCodeApprovals(): Promise<CodeApproval[]> {
  const body = await api<{ approvals: CodeApproval[] }>('/api/code/approvals');
  return body.approvals ?? [];
}

export async function decideCodeApproval(id: string, approve: boolean, reason = ''): Promise<unknown> {
  return api(`/api/code/approvals/${encodeURIComponent(id)}`, {
    method: 'POST',
    body: JSON.stringify({ approve, reason }),
  });
}

/** The change a task made, in everyday words plus the raw text behind "details". */
export async function getCodeTaskDiff(
  id: string,
  mode: 'working' | 'committed' | 'revision' | 'turn' = 'working',
  base?: string,
): Promise<CodeTaskDiff | null> {
  try {
    const query = new URLSearchParams({ mode });
    if (base) query.set('base', base);
    const body = await api<{ diff: CodeTaskDiff }>(
      `/api/code/tasks/${encodeURIComponent(id)}/diff?${query.toString()}`,
    );
    return body.diff ?? null;
  } catch {
    // A task with no bound work area has no diff; that is not an error.
    return null;
  }
}

/** One bounded picture from the task's change, for the review diff. */
export interface CodeTaskImage {
  path: string;
  mime: string;
  size: number;
  /** Base64, so the only transport is the JSON route. */
  bytes: string;
}

export async function getCodeTaskImage(
  id: string,
  path: string,
  side: 'before' | 'after' = 'after',
): Promise<CodeTaskImage | null> {
  try {
    const query = new URLSearchParams({ path, side });
    const body = await api<{ image: CodeTaskImage }>(
      `/api/code/tasks/${encodeURIComponent(id)}/image?${query.toString()}`,
    );
    return body.image ?? null;
  } catch {
    // A removed, oversized or non-image file has no preview; the viewer says so.
    return null;
  }
}

// ── Promotion: put a reviewed change into the real project (Phase 15) ───────
// The client-safe path: the two Next.js routes proxy to the runtime, which owns
// the preview and the single-use decision. This module only carries it.

/** Preview exactly what a task's change would put into the real project. */
export async function previewPromotion(
  taskId: string, include: string[] = [], hunks: string[] = [],
): Promise<{ preview: PromotionPreview; approval: { id: string; status: string } | null; landing?: PromotionLanding }> {
  return api(`/api/code/tasks/${encodeURIComponent(taskId)}/promote/preview`, {
    method: 'POST',
    body: JSON.stringify({ include, hunks }),
  });
}

/** Confirm a previewed change and put it into the real project. */
export async function autolandTask(
  taskId: string, full = false,
): Promise<{ ok: boolean; applied: boolean; rolledBack: boolean; rollbackId?: string | null; checks?: Record<string, unknown> }> {
  return api(`/api/code/tasks/${encodeURIComponent(taskId)}/autoland${full ? '?full=true' : ''}`, {
    method: 'POST',
  });
}

export async function applyPromotion(
  taskId: string,
  approvalId: string,
  previewDigest: string,
  include: string[] = [],
  hunks: string[] = [],
): Promise<{ ok: boolean; filesChanged: number; rollbackId?: string | null; landing?: PromotionLanding }> {
  const body = await api<{ promotion: { ok: boolean; filesChanged: number; rollbackId?: string | null; landing?: PromotionLanding } }>(
    `/api/code/tasks/${encodeURIComponent(taskId)}/promote`,
    { method: 'POST', body: JSON.stringify({ approvalId, previewDigest, include, hunks }) },
  );
  return body.promotion;
}

/** Put a previously-promoted change back — the exact original contents. */
export async function rollbackPromotion(
  rollbackId: string, runId?: string,
): Promise<{ ok: boolean; rollbackId: string; restored: string[]; filesChanged: number }> {
  const body = await api<{ rollback: { ok: boolean; rollbackId: string; restored: string[]; filesChanged: number } }>(
    `/api/code/promotions/${encodeURIComponent(rollbackId)}/rollback`,
    { method: 'POST', body: JSON.stringify({ runId }) },
  );
  return body.rollback;
}

/** Restart the Python agent runtime (the self-hosting seam). True when healthy. */
export async function restartHarnessRuntime(): Promise<boolean> {
  const body = await api<{ healthy: boolean }>('/api/harness/runtime/restart', { method: 'POST' });
  return Boolean(body.healthy);
}

/** Probe an RSL landing after the restart; the runtime rolls back on failure. */
export async function postLandingProbe(
  rollbackId: string,
  runId: string,
  alreadyRestarted = true,
  activationId?: string,
): Promise<PostLandingRecord> {
  const body = await api<{ postLanding: PostLandingRecord }>(
    `/api/code/promotions/${encodeURIComponent(rollbackId)}/post-landing`,
    {
      method: 'POST',
      body: JSON.stringify({ runId, activationId, alreadyRestarted }),
    },
  );
  return body.postLanding;
}

// ── Workspace folders + editor (client-safe) ─────────────────────────────────

import type { BrowseEntry, BrowseResult, EditorEntry, EditorFileResult, EditorTreeResult, TrustedRoot, WorkspaceFileHit, WorkspaceSearchResult } from './code-client';
export type { BrowseEntry, BrowseResult, EditorEntry, EditorFileResult, EditorTreeResult, TrustedRoot, WorkspaceFileHit, WorkspaceSearchResult };

export async function browseFolders(path?: string): Promise<BrowseResult> {
  const q = path ? `?path=${encodeURIComponent(path)}` : '';
  // In dev the page is on `localhost`; use the loopback origin so a wedged
  // localhost keep-alive socket cannot freeze the picker.
  return api<BrowseResult>(`${workspaceBase()}/api/workspace/browse${q}`, {
    signal: AbortSignal.timeout(30_000),
  });
}

/** Create one folder inside the directory the picker is showing. */
export async function createBrowsedFolder(
  path: string, name: string,
): Promise<{ path: string; name: string; parent: string }> {
  return api<{ path: string; name: string; parent: string }>(`${workspaceBase()}/api/workspace/browse/mkdir`, {
    method: 'POST',
    body: JSON.stringify({ path, name }),
    signal: AbortSignal.timeout(20_000),
  });
}

/**
 * Open the real Windows "Select Folder" dialog on the machine running the app.
 * Returns the chosen absolute path, or null when the person cancels. The call
 * blocks while the OS dialog is open, so it gets a generous timeout.
 */
export async function pickFolderNative(): Promise<string | null> {
  const body = await api<{ path?: string | null; cancelled?: boolean }>(
    `${workspaceBase()}/api/workspace/pick-folder`,
    { method: 'POST', signal: AbortSignal.timeout(310_000) },
  );
  return body.path ?? null;
}

export async function listTrustedRoots(): Promise<TrustedRoot[]> {
  const body = await api<{ roots: TrustedRoot[] }>(`${workspaceBase()}/api/workspace/roots`);
  return body.roots ?? [];
}

export interface WorkspaceRoots {
  roots: TrustedRoot[];
  /** Server-resolved folder used when no per-chat root is bound (read-only). */
  defaultRoot: string | null;
}

export async function getWorkspaceRoots(): Promise<WorkspaceRoots> {
  const body = await api<{ roots: TrustedRoot[]; defaultRoot?: string | null }>(
    `${workspaceBase()}/api/workspace/roots`,
  );
  return {
    roots: body.roots ?? [],
    defaultRoot: typeof body.defaultRoot === 'string' ? body.defaultRoot : null,
  };
}

export async function approveTrustedRoot(path: string, label?: string): Promise<TrustedRoot> {
  const body = await api<{ root: TrustedRoot }>(`${workspaceBase()}/api/workspace/roots`, {
    method: 'POST',
    body: JSON.stringify({ path, label }),
  });
  return body.root;
}

export async function selectTrustedRoot(rootId: string | null): Promise<TrustedRoot | null> {
  const body = await api<{ selected: TrustedRoot | null }>(`${workspaceBase()}/api/workspace/roots/select`, {
    method: 'POST',
    body: JSON.stringify({ rootId }),
  });
  return body.selected;
}

export async function revokeTrustedRoot(rootId: string): Promise<void> {
  await api(`${workspaceBase()}/api/workspace/roots/${encodeURIComponent(rootId)}`, { method: 'DELETE' });
}

export async function listWorkspaceFiles(path = '.', rootId?: string | null): Promise<EditorTreeResult> {
  const root = rootId ? `&rootId=${encodeURIComponent(rootId)}` : '';
  return api<EditorTreeResult>(`/api/workspace/files?path=${encodeURIComponent(path)}${root}`);
}

export async function searchWorkspaceFiles(
  query: string,
  rootId?: string | null,
  limit = 50,
): Promise<WorkspaceSearchResult> {
  const root = rootId ? `&rootId=${encodeURIComponent(rootId)}` : '';
  return api<WorkspaceSearchResult>(
    `/api/workspace/files/search?q=${encodeURIComponent(query)}&limit=${limit}${root}`,
  );
}

export async function readWorkspaceFile(path: string, rootId?: string | null): Promise<EditorFileResult> {
  return api<EditorFileResult>('/api/workspace/file/read', {
    method: 'POST',
    body: JSON.stringify({ path, ...(rootId ? { rootId } : {}) }),
  });
}

export async function writeWorkspaceFile(path: string, content: string): Promise<{ path: string; bytes: number }> {
  return api<{ path: string; bytes: number }>('/api/workspace/file/write', {
    method: 'POST',
    body: JSON.stringify({ path, content }),
  });
}

export async function makeWorkspaceFolder(path: string): Promise<{ path: string }> {
  return api<{ path: string }>('/api/workspace/file/mkdir', {
    method: 'POST',
    body: JSON.stringify({ path }),
  });
}

export async function deleteWorkspacePath(path: string): Promise<{ path: string; removed: boolean }> {
  return api<{ path: string; removed: boolean }>('/api/workspace/file/delete', {
    method: 'POST',
    body: JSON.stringify({ path }),
  });
}

// ── Pure, client-safe render helpers (mirrors of the server client) ─────────

export type CheckClaimKind =
  | 'tests_pass'
  | 'typecheck_clean'
  | 'build_ok'
  | 'lint_clean'
  | 'artifacts_verified'
  | 'change_reviewed';

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
  status: 'passed' | 'unrepairable' | 'budget_exhausted' | 'refused' | 'no_checks';
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
  /** Phase 19: artifact observations (one per check). Additive, old-row-safe. */
  artifacts: CodeArtifactRecord[];
  /** Phase 19: recorded change reviews. Additive, old-row-safe. */
  reviews: CodeReviewRecord[];
}

export interface CodeArtifactRecord {
  artifactName: string;
  kind: string;
  check: string;
  passed: boolean;
  detail: string;
}

export interface CodeReviewFinding {
  rule: string;
  summary: string;
  severity: 'blocking' | 'advisory';
  path: string;
  line: number | null;
}

export interface CodeReviewRecord {
  filesReviewed: number;
  findings: CodeReviewFinding[];
  unevaluated: string[];
}

export function claimStatusKey(supported: boolean): 'code.proof.verified' | 'code.proof.notChecked' {
  return supported ? 'code.proof.verified' : 'code.proof.notChecked';
}

function _artifactRecords(raw: unknown): CodeArtifactRecord[] {
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[])
    .filter((a): a is Record<string, unknown> => a !== null && typeof a === 'object')
    .map((a) => ({
      artifactName: String(a.artifactName ?? ''),
      kind: String(a.kind ?? ''),
      check: String(a.check ?? ''),
      passed: Boolean(a.passed),
      detail: String(a.detail ?? ''),
    }))
    .filter((a) => a.artifactName && a.check)
    .slice(0, 200);
}

function _reviewRecords(raw: unknown): CodeReviewRecord[] {
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[])
    .filter(
      (r): r is Record<string, unknown> =>
        r !== null && typeof r === 'object' && (r as Record<string, unknown>).ran === true,
    )
    .map((r) => ({
      filesReviewed: Number(r.filesReviewed ?? 0),
      findings: Array.isArray(r.findings)
        ? (r.findings as unknown[])
            .filter((f): f is Record<string, unknown> => f !== null && typeof f === 'object')
            .map((f) => ({
              rule: String(f.rule ?? ''),
              summary: String(f.summary ?? ''),
              severity: f.severity === 'blocking' ? ('blocking' as const) : ('advisory' as const),
              path: String(f.path ?? ''),
              line: typeof f.line === 'number' ? f.line : null,
            }))
            .slice(0, 50)
        : [],
      unevaluated: Array.isArray(r.unevaluated)
        ? (r.unevaluated as unknown[]).map(String).slice(0, 10)
        : [],
    }))
    .slice(0, 8);
}

export function coerceCodeVerification(raw: unknown): CodeVerification | null {
  if (raw === null || typeof raw !== 'object') return null;
  const blob = raw as Record<string, unknown>;
  const records = Array.isArray(blob.records) ? (blob.records as CodeVerificationRecord[]) : [];
  const claims = Array.isArray(blob.claims) ? (blob.claims as CodeVerificationClaim[]) : [];
  const artifacts = _artifactRecords(blob.artifacts);
  const reviews = _reviewRecords(blob.reviews);
  if (
    records.length === 0 &&
    claims.length === 0 &&
    artifacts.length === 0 &&
    reviews.length === 0 &&
    !blob.repair
  ) {
    return null;
  }
  return {
    runId: String(blob.runId ?? ''),
    records,
    claims,
    unverifiedCount: Number(blob.unverifiedCount ?? 0),
    repair: (blob.repair as CodeVerificationRepair | null) ?? null,
    truncated: Boolean(blob.truncated),
    artifacts,
    reviews,
  };
}

// -- W7: file checkpoints + /rollback -----------------------------------------

export interface FileCheckpointView {
  id: string;
  reason?: string;
  runId?: string;
  createdAt?: string;
  files?: number;
}

export interface CheckpointRestorePreviewView {
  checkpointId: string;
  all: boolean;
  changes: Array<{ status: string; path: string }>;
  count: number;
  diff: string;
}

export async function listFileCheckpoints(limit = 50): Promise<FileCheckpointView[]> {
  const body = await api<{ checkpoints: FileCheckpointView[] }>(
    `/api/code/checkpoints?limit=${encodeURIComponent(String(limit))}`,
  );
  return body.checkpoints ?? [];
}

export async function diffFileCheckpoint(
  id: string,
): Promise<{ unified: string; truncated: boolean }> {
  return api(`/api/code/checkpoints/${encodeURIComponent(id)}/diff`);
}

export async function previewFileRestore(
  id: string,
  input: { paths?: string[]; all?: boolean } = {},
): Promise<CheckpointRestorePreviewView> {
  return api(`/api/code/checkpoints/${encodeURIComponent(id)}/restore/preview`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function restoreFileCheckpoint(
  id: string,
  input: { paths?: string[]; all?: boolean } = {},
): Promise<{ restored: string[]; skipped: string[]; preRestoreId: string | null }> {
  return api(`/api/code/checkpoints/${encodeURIComponent(id)}/restore`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}
