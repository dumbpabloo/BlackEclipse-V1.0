// ─── Eclipse Canvas browser client ───────────────────────────────────────────
// The typed front door to the Python canvas routes. Content is canonical
// artifact bytes; saves are revision-guarded. Nothing here invents content.

export type CanvasKind = "document" | "code" | "visual" | "presentation";
export type CanvasMode = "ask" | "edit" | "review" | "build";
export type CanvasOp =
  | "append"
  | "replace_text_range"
  | "replace_section"
  | "insert_after_section"
  | "replace_document"
  | "apply_code_patch";

export interface CanvasVersionSummary {
  versionNumber: number;
  changeNote?: string | null;
  sizeBytes?: number | null;
  createdAt: string;
  createdBy?: string | null;
}

export interface CanvasSnapshot {
  artifactId: string;
  name: string;
  description: string;
  kind: CanvasKind;
  language?: string | null;
  mimeType: string;
  content: string;
  version: number;
  sizeBytes?: number | null;
  updatedAt: string;
  conversationId?: string | null;
  versions: CanvasVersionSummary[];
}

export interface CanvasListItem {
  artifactId: string;
  name: string;
  kind: CanvasKind;
  language?: string | null;
  description: string;
  version: number;
  sizeBytes?: number | null;
  updatedAt: string;
  conversationId?: string | null;
}

export interface EditOperation {
  op: CanvasOp;
  content?: string;
  start?: number;
  end?: number;
  expectedText?: string;
  heading?: string;
  search?: string;
  replace?: string;
  explanation?: string;
}

/** A typed deck operation (the vocabulary in presentations/ops.py). */
export interface DeckOp {
  op: string;
  slideId?: string;
  slide?: unknown;
  at?: number;
  to?: number;
  field?: string;
  value?: string;
  text?: string;
  bullets?: string[];
  chart?: unknown;
  table?: unknown;
  metrics?: unknown[];
  preset?: string;
  accent?: string;
  explanation?: string;
}

export interface CanvasProposal {
  artifactId: string;
  baseVersion: number;
  currentVersion: number;
  mode: CanvasMode;
  answer: string;
  summary: string;
  operations: EditOperation[];
  /** Deck ops for a presentation artifact; exactly one list is non-empty. */
  deckOperations?: DeckOp[];
  resultContent?: string | null;
  notes: string[];
}

export class CanvasConflictError extends Error {
  currentVersion: number;
  constructor(message: string, currentVersion: number) {
    super(message);
    this.name = "CanvasConflictError";
    this.currentVersion = currentVersion;
  }
}

export class CanvasApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "CanvasApiError";
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/canvas${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: { code?: string; message?: string; currentVersion?: number } }).detail;
    const message =
      detail?.message ??
      (data as { error?: string }).error ??
      `Canvas request failed (${res.status})`;
    if (res.status === 409 && detail?.currentVersion) {
      throw new CanvasConflictError(message, detail.currentVersion);
    }
    throw new CanvasApiError(message, res.status, detail?.code);
  }
  return data as T;
}

export async function listCanvasArtifacts(params?: {
  kind?: CanvasKind;
  q?: string;
  limit?: number;
}): Promise<CanvasListItem[]> {
  const search = new URLSearchParams();
  if (params?.kind) search.set("kind", params.kind);
  if (params?.q) search.set("q", params.q);
  if (params?.limit) search.set("limit", String(params.limit));
  const suffix = search.toString() ? `?${search.toString()}` : "";
  const data = await request<{ artifacts: CanvasListItem[] }>(`/artifacts${suffix}`);
  return data.artifacts ?? [];
}

export async function createCanvasArtifact(input: {
  name: string;
  kind: CanvasKind;
  language?: string;
  content?: string;
  description?: string;
  conversationId?: string;
  tags?: string[];
}): Promise<CanvasSnapshot> {
  return request<CanvasSnapshot>("/artifacts", { method: "POST", body: JSON.stringify(input) });
}

export async function getCanvasArtifact(artifactId: string): Promise<CanvasSnapshot> {
  return request<CanvasSnapshot>(`/artifacts/${encodeURIComponent(artifactId)}`);
}

export async function saveCanvasArtifact(
  artifactId: string,
  input: {
    baseVersion: number;
    content?: string;
    operations?: unknown[];
    changeNote?: string;
    idempotencyKey?: string;
  },
): Promise<CanvasSnapshot> {
  return request<CanvasSnapshot>(`/artifacts/${encodeURIComponent(artifactId)}`, {
    method: "PUT",
    body: JSON.stringify(input),
  });
}

export async function restoreCanvasVersion(
  artifactId: string,
  input: { baseVersion: number; version: number; changeNote?: string },
): Promise<CanvasSnapshot> {
  return request<CanvasSnapshot>(`/artifacts/${encodeURIComponent(artifactId)}/restore`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function proposeCanvasEdit(
  artifactId: string,
  input: {
    baseVersion: number;
    instruction: string;
    mode: CanvasMode;
    selection?: { start: number; end: number; text?: string };
    model?: string;
  },
): Promise<CanvasProposal> {
  return request<CanvasProposal>(`/artifacts/${encodeURIComponent(artifactId)}/edits`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}
