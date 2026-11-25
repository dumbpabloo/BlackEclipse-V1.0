// ── Native-video run view model ──────────────────────────────────────────────
// Pure coercion of the runtime `video` run blob (`VideoJobCollector.to_blob()`)
// and of the live job records `/api/video/jobs/{id}` returns. Both are untrusted
// input: only well-formed records survive, and a job is shown as RUNNING or
// DONE strictly from what the runtime reported — this module never invents a
// status, a progress value or an artifact id.

export type VideoJobKind = 'preview' | 'render' | 'repair' | 'inspect';

export interface VideoJobRef {
  jobId: string;
  projectId: string;
  projectName: string | null;
  kind: VideoJobKind;
  mode: 'draft' | 'final' | null;
  ofJobId: string | null;
}

export interface VideoJobStatus {
  jobId: string;
  status: string;
  progress: number;
  terminal: boolean;
  failed: boolean;
  cancelled: boolean;
  timedOut: boolean;
  errorClass: string | null;
  error: string | null;
  /** The Artifact Center entry (final renders only). */
  artifactId: string | null;
  artifactName: string | null;
  /** The runtime recorded a real poster frame for this artifact. */
  hasThumbnail: boolean;
  durationSeconds: number | null;
  /** Preview jobs: how many PNGs registered. */
  previewCount: number | null;
  /** Inspection jobs: findings summary. */
  findingsTotal: number | null;
  findingsErrors: number | null;
}

const KINDS: readonly VideoJobKind[] = ['preview', 'render', 'repair', 'inspect'];

const TERMINAL_STATUSES = new Set([
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'CANCEL_REQUESTED',
  'TIMED_OUT',
  'DEAD_LETTER',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Coerce one collected job reference; null for a malformed record. */
export function coerceVideoJobRef(raw: unknown): VideoJobRef | null {
  if (!isRecord(raw)) return null;
  const jobId = str(raw.jobId);
  const projectId = str(raw.projectId);
  const kind = str(raw.kind) as VideoJobKind | null;
  if (!jobId || !projectId || !kind || !KINDS.includes(kind)) return null;
  const mode = str(raw.mode);
  return {
    jobId,
    projectId,
    projectName: str(raw.projectName),
    kind,
    mode: mode === 'draft' || mode === 'final' ? mode : null,
    ofJobId: str(raw.ofJobId),
  };
}

/** Coerce the runtime `video` run blob into the job references it queued. */
export function coerceVideoBlob(blob: unknown): VideoJobRef[] {
  if (!isRecord(blob)) return [];
  const jobs = Array.isArray(blob.jobs) ? blob.jobs : [];
  const out: VideoJobRef[] = [];
  const seen = new Set<string>();
  for (const candidate of jobs) {
    const ref = coerceVideoJobRef(candidate);
    if (!ref || seen.has(ref.jobId)) continue;
    seen.add(ref.jobId);
    out.push(ref);
    if (out.length >= 8) break;
  }
  return out;
}

/** Coerce a live job record (`/api/video/jobs/{id}`) into a display status. */
export function coerceVideoJobStatus(jobId: string, raw: unknown): VideoJobStatus {
  const base: VideoJobStatus = {
    jobId,
    status: 'UNKNOWN',
    progress: 0,
    terminal: false,
    failed: false,
    cancelled: false,
    timedOut: false,
    errorClass: null,
    error: null,
    artifactId: null,
    artifactName: null,
    hasThumbnail: false,
    durationSeconds: null,
    previewCount: null,
    findingsTotal: null,
    findingsErrors: null,
  };
  if (!isRecord(raw)) return base;
  const status = str(raw.status) ?? 'UNKNOWN';
  const output = isRecord(raw.output) ? raw.output : {};
  const artifact = isRecord(output.artifact) ? output.artifact : {};
  const terminal =
    raw.terminal === true || TERMINAL_STATUSES.has(status);
  const progressRaw = num(raw.progress) ?? 0;
  const previews = Array.isArray(output.previews) ? output.previews : null;
  return {
    jobId,
    status,
    progress: Math.max(0, Math.min(1, progressRaw)),
    terminal,
    failed: status === 'FAILED' || status === 'DEAD_LETTER',
    cancelled: status === 'CANCELLED' || status === 'CANCEL_REQUESTED',
    timedOut: status === 'TIMED_OUT',
    errorClass: str(raw.errorClass) ?? str(output.errorClass),
    error: str(raw.error) ?? str(output.error),
    artifactId: str(artifact.id),
    artifactName: str(artifact.name),
    hasThumbnail: typeof artifact.thumbnailKey === 'string' && artifact.thumbnailKey.length > 0,
    durationSeconds: num(output.durationSeconds),
    previewCount: previews ? previews.length : null,
    findingsTotal: num(output.findingsTotal),
    findingsErrors: num(output.findingsErrors),
  };
}

/** A short human label for one job reference. */
export function videoJobLabel(ref: VideoJobRef): string {
  if (ref.kind === 'preview') return 'Preview';
  if (ref.kind === 'inspect') return 'Frame inspection';
  if (ref.kind === 'repair') {
    return ref.mode === 'final' ? 'Repair (final)' : 'Repair (draft)';
  }
  return ref.mode === 'final' ? 'Final render' : 'Draft render';
}
