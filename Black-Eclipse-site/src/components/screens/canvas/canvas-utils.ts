// ─── Eclipse Canvas — pure helpers ────────────────────────────────────────────
// No React, no network. Language mapping, export metadata and the honest error
// mapping the container uses. Duck-typed against the client error classes so
// this module never needs to import (and be mocked with) the client itself.

import type { CanvasKind, CanvasProposal } from '@/lib/harness/clients/canvas-client';
import { emptyDeck, serializeDeck } from '@/lib/presentation/schema';

export const CANVAS_KINDS: CanvasKind[] = ['document', 'code', 'visual', 'presentation'];

export function kindLabel(kind: CanvasKind): string {
  switch (kind) {
    case 'document': return 'Document';
    case 'code': return 'Code';
    case 'visual': return 'Visual report';
    case 'presentation': return 'Presentation';
  }
}

export const CODE_LANGUAGES = [
  'typescript', 'javascript', 'python', 'json', 'markdown', 'html', 'css',
  'sql', 'shell', 'yaml', 'rust', 'go', 'java', 'c', 'cpp', 'plaintext',
] as const;

const MONACO_ALIASES: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', typescript: 'typescript',
  js: 'javascript', jsx: 'javascript', javascript: 'javascript',
  py: 'python', python: 'python',
  rb: 'ruby', rs: 'rust', rust: 'rust',
  sh: 'shell', bash: 'shell', shell: 'shell', zsh: 'shell',
  md: 'markdown', markdown: 'markdown',
  yml: 'yaml', yaml: 'yaml',
  html: 'html', htm: 'html', css: 'css', scss: 'scss',
  json: 'json', sql: 'sql', go: 'go', java: 'java',
  c: 'c', cpp: 'cpp', 'c++': 'cpp', cs: 'csharp', php: 'php',
  text: 'plaintext', txt: 'plaintext', plaintext: 'plaintext',
};

export function monacoLanguage(language?: string | null): string {
  if (!language) return 'plaintext';
  const key = language.toLowerCase().trim();
  return MONACO_ALIASES[key] ?? key;
}

const CONTENT_EXT: Record<string, string> = {
  typescript: 'ts', javascript: 'js', python: 'py', json: 'json',
  markdown: 'md', html: 'html', css: 'css', sql: 'sql', shell: 'sh',
  yaml: 'yml', rust: 'rs', go: 'go', java: 'java', c: 'c', cpp: 'cpp',
  csharp: 'cs', php: 'php', ruby: 'rb', scss: 'scss',
};

export function contentExtension(kind: CanvasKind, language?: string | null): string {
  if (kind === 'document') return 'md';
  if (kind === 'visual') return 'json';
  if (kind === 'presentation') return 'deck.json';
  return CONTENT_EXT[monacoLanguage(language)] ?? 'txt';
}

export function contentMime(kind: CanvasKind, language?: string | null): string {
  if (kind === 'document') return 'text/markdown';
  if (kind === 'visual') return 'application/json';
  if (kind === 'presentation') return 'application/vnd.eclipse.deck+json';
  const ext = contentExtension(kind, language);
  if (ext === 'json') return 'application/json';
  if (ext === 'html') return 'text/html';
  if (ext === 'css') return 'text/css';
  return 'text/plain';
}

export function starterContent(kind: CanvasKind): string {
  if (kind === 'visual') return JSON.stringify({ blocks: [] }, null, 2);
  if (kind === 'presentation') return serializeDeck(emptyDeck('Untitled deck'));
  return '';
}

// ── Honest error mapping ──────────────────────────────────────────────────────

export interface CanvasErrorState {
  message: string;
  status?: number;
  code?: string;
  currentVersion?: number;
  conflict: boolean;
}

export function describeCanvasError(err: unknown): CanvasErrorState {
  const e = err as {
    name?: string; message?: string; status?: number;
    code?: string; currentVersion?: number;
  } | null;
  if (e && e.name === 'CanvasConflictError') {
    return { message: e.message ?? 'This artifact changed elsewhere.', currentVersion: e.currentVersion, conflict: true };
  }
  if (e && e.name === 'CanvasApiError') {
    return {
      message: e.status === 503
        ? 'The agent runtime is unavailable. Canvas cannot load or save right now.'
        : (e.message ?? 'The canvas request failed.'),
      status: e.status,
      code: e.code,
      conflict: e.status === 409,
    };
  }
  return { message: e?.message ?? 'Something went wrong.', conflict: false };
}

// ── JSON ──────────────────────────────────────────────────────────────────────

export function parseJsonStrict(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'Invalid JSON' };
  }
}

// ── Line diff (for the review view) ───────────────────────────────────────────

export interface DiffLine {
  type: 'ctx' | 'add' | 'del';
  text: string;
}

// ── Editor contracts ──────────────────────────────────────────────────────────

export interface CanvasSelection {
  start: number;
  end: number;
  text: string;
}

export interface CanvasEditorHandle {
  undo?: () => void;
  redo?: () => void;
}

export type CanvasSaveState = 'idle' | 'saving' | 'saved' | 'error' | 'conflict';

/** The typed operations a proposal carries (deck ops win for a presentation). */
export function proposalOperations(proposal: CanvasProposal): unknown[] {
  if (proposal.deckOperations && proposal.deckOperations.length > 0) return proposal.deckOperations;
  return proposal.operations;
}

export function proposalOperationCount(proposal: CanvasProposal | null): number {
  return proposal ? proposalOperations(proposal).length : 0;
}

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split('\n');
  const b = after.split('\n');
  const n = a.length;
  const m = b.length;
  const max = 2000;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ type: 'ctx', text: a[i] });
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i] });
      i += 1;
    } else {
      out.push({ type: 'add', text: b[j] });
      j += 1;
    }
    if (out.length >= max) break;
  }
  while (i < n && out.length < max) { out.push({ type: 'del', text: a[i] }); i += 1; }
  while (j < m && out.length < max) { out.push({ type: 'add', text: b[j] }); j += 1; }
  return out;
}

export function humanTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return iso;
  }
}
