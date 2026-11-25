// ─── Presentation browser client ──────────────────────────────────────────────
// The front door to /presentations: theme tokens and .pptx export. Deck CRUD
// goes through the Canvas client (a deck is a canvas artifact).

export interface PresentationThemeTokens {
  preset: string;
  background: string;
  surface: string;
  surfaceAlt: string;
  text: string;
  muted: string;
  accent: string;
  accentText: string;
  border: string;
  positive: string;
  negative: string;
  fontHeading: string;
  fontBody: string;
  chartPalette: string[];
  type: Record<string, number>;
}

export interface PresentationExportResult {
  artifact: { id: string; name: string; mimeType: string };
  path: string;
  slides: number;
  warnings: Array<{ slideId: string; code: string }>;
  overflowSlides: string[];
}

export class PresentationApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'PresentationApiError';
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/presentations${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: { code?: string; message?: string } }).detail;
    throw new PresentationApiError(
      detail?.message ?? (data as { error?: string }).error ?? `Presentation request failed (${res.status})`,
      res.status,
      detail?.code,
    );
  }
  return data as T;
}

export async function listPresentationThemes(): Promise<PresentationThemeTokens[]> {
  const data = await request<{ themes: PresentationThemeTokens[] }>('/themes');
  return data.themes ?? [];
}

export type PresentationExportFormat = 'pptx' | 'pdf' | 'png';

export async function exportPresentation(
  artifactId: string,
  options: { format?: PresentationExportFormat; saveTo?: string; filename?: string } = {},
): Promise<PresentationExportResult> {
  return request<PresentationExportResult>(`/${encodeURIComponent(artifactId)}/export`, {
    method: 'POST',
    body: JSON.stringify({ format: options.format ?? 'pptx', saveTo: options.saveTo, filename: options.filename }),
  });
}
