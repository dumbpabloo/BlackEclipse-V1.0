// Split a unified diff into per-file sections for review. Pure, tolerant of
// quoted paths and of a diff that has no `diff --git` header.

export interface DiffSection {
  path: string;
  text: string;
}

/**
 * One rendered line of a unified diff, with the file line number each side had.
 * `hunk` and `meta` lines carry no number; review comments anchor to `newLine`
 * (falling back to `oldLine` for a removed line) so a note names a real line in
 * the file the user is looking at.
 */
export type DiffLineKind = 'add' | 'del' | 'context' | 'hunk' | 'meta';

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
  oldLine?: number;
  newLine?: number;
}

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Parse one file section into lines with old/new line numbers. */
export function parseDiffLines(text: string): DiffLine[] {
  const out: DiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  for (const line of text.split(/\r?\n/)) {
    const hunk = line.match(HUNK_HEADER);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      out.push({ kind: 'hunk', text: line });
      continue;
    }
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) {
      out.push({ kind: 'meta', text: line });
      continue;
    }
    if (line.startsWith('+')) {
      out.push({ kind: 'add', text: line.slice(1), newLine });
      newLine += 1;
      continue;
    }
    if (line.startsWith('-')) {
      out.push({ kind: 'del', text: line.slice(1), oldLine });
      oldLine += 1;
      continue;
    }
    // Context (also the `\ No newline` marker, which has no number).
    if (line.startsWith('\\')) {
      out.push({ kind: 'meta', text: line });
      continue;
    }
    out.push({ kind: 'context', text: line.startsWith(' ') ? line.slice(1) : line, oldLine, newLine });
    oldLine += 1;
    newLine += 1;
  }
  return out;
}

const GIT_HEADER = /^diff --git a\/(.+?) b\/(.+)$/;
const PLUS_HEADER = /^\+\+\+ b\/(.+)$/;

export function splitUnifiedDiff(unified: string): DiffSection[] {
  if (!unified || !unified.trim()) return [];
  const sections: Array<{ path: string; lines: string[] }> = [];
  let current: { path: string; lines: string[] } | null = null;

  for (const line of unified.split(/\r?\n/)) {
    const header = line.match(GIT_HEADER);
    if (header) {
      current = { path: clean(header[2]), lines: [line] };
      sections.push(current);
      continue;
    }
    if (!current) {
      current = { path: 'change', lines: [] };
      sections.push(current);
    }
    if (current.path === 'change') {
      const plus = line.match(PLUS_HEADER);
      if (plus) current.path = clean(plus[1]);
    }
    current.lines.push(line);
  }

  return sections.map((section) => ({
    path: section.path,
    text: section.lines.join('\n'),
  }));
}

function clean(path: string): string {
  let value = path.trim();
  if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
  return value.replace(/^b\//, '');
}
