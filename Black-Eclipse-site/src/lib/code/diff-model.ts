// ── Diff model: files, status, image detection, and split pairing ───────────
// `splitUnifiedDiff` gives per-file text; this turns it into the shape the Code
// screen renders: one entry per file with a status, counts, parsed lines and an
// image flag. Split mode pairs a removed run with the added run that replaced it,
// which is what makes a side-by-side diff readable instead of ragged.

import { parseDiffLines, type DiffLine } from './diff-sections';

export type DiffFileStatus = 'added' | 'modified' | 'deleted';

export interface DiffFile {
  path: string;
  status: DiffFileStatus;
  additions: number;
  deletions: number;
  lines: DiffLine[];
  image: boolean;
}

export interface DiffFileLists {
  added?: string[];
  modified?: string[];
  deleted?: string[];
}

export interface SplitRow {
  left?: DiffLine;
  right?: DiffLine;
  /** A pure context line spans both sides. */
  context?: boolean;
}

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|ico|svg|avif|tiff?)$/i;

export function isImagePath(path: string): boolean {
  return IMAGE_EXT.test(path);
}

function normalize(path: string): string {
  return path.replace(/^\.\//, '').replace(/^a\//, '').replace(/^b\//, '');
}

function statusOf(
  path: string,
  lists: DiffFileLists,
  text: string,
): DiffFileStatus {
  const clean = normalize(path);
  if ((lists.added ?? []).some((p) => normalize(p) === clean)) return 'added';
  if ((lists.deleted ?? []).some((p) => normalize(p) === clean)) return 'deleted';
  if ((lists.modified ?? []).some((p) => normalize(p) === clean)) return 'modified';
  if (/^new file mode /m.test(text) || /^--- \/dev\/null$/m.test(text)) return 'added';
  if (/^deleted file mode /m.test(text) || /^\+\+\+ \/dev\/null$/m.test(text)) return 'deleted';
  return 'modified';
}

/** Build the renderable file model from per-file sections plus status lists. */
export function buildDiffFiles(
  sections: Array<{ path: string; text: string }>,
  lists: DiffFileLists = {},
): DiffFile[] {
  return sections.map((section) => {
    const lines = parseDiffLines(section.text);
    return {
      path: normalize(section.path),
      status: statusOf(section.path, lists, section.text),
      additions: lines.filter((l) => l.kind === 'add').length,
      deletions: lines.filter((l) => l.kind === 'del').length,
      lines,
      image: isImagePath(section.path),
    };
  });
}

/**
 * Pair lines for a side-by-side view. A contiguous removed run is aligned with
 * the added run that follows it; context lines occupy both columns. Extra lines
 * on either side keep a blank opposite cell rather than shifting the rows.
 */
export function pairSplitRows(lines: DiffLine[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.kind === 'del') {
      const removed: DiffLine[] = [];
      const added: DiffLine[] = [];
      while (index < lines.length && lines[index].kind === 'del') removed.push(lines[index++]);
      while (index < lines.length && lines[index].kind === 'add') added.push(lines[index++]);
      const width = Math.max(removed.length, added.length);
      for (let i = 0; i < width; i += 1) {
        rows.push({ left: removed[i], right: added[i] });
      }
      continue;
    }
    if (line.kind === 'add') {
      rows.push({ right: line });
      index += 1;
      continue;
    }
    rows.push({ left: line, right: line, context: true });
    index += 1;
  }
  return rows;
}

/** Group files by their directory for a compact tree. */
export function groupFilesByDirectory(files: DiffFile[]): Array<{ directory: string; files: DiffFile[] }> {
  const groups = new Map<string, DiffFile[]>();
  for (const file of files) {
    const slash = file.path.lastIndexOf('/');
    const directory = slash > 0 ? file.path.slice(0, slash) : '.';
    const list = groups.get(directory) ?? [];
    list.push(file);
    groups.set(directory, list);
  }
  return [...groups.entries()].map(([directory, entries]) => ({ directory, files: entries }));
}
