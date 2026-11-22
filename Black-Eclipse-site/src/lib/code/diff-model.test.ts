import { describe, expect, it } from 'vitest';

import { splitUnifiedDiff } from './diff-sections';
import { buildDiffFiles, groupFilesByDirectory, isImagePath, pairSplitRows } from './diff-model';

const DIFF = [
  'diff --git a/src/a.ts b/src/a.ts',
  '--- a/src/a.ts',
  '+++ b/src/a.ts',
  '@@ -1,3 +1,3 @@',
  ' const x = 1',
  '-const y = 2',
  '+const y = 3',
  ' console.log(x, y)',
  'diff --git a/logo.png b/logo.png',
  '--- a/logo.png',
  '+++ b/logo.png',
  'Binary files a/logo.png and b/logo.png differ',
].join('\n');

describe('buildDiffFiles', () => {
  it('produces one file with counts, status and an image flag', () => {
    const files = buildDiffFiles(splitUnifiedDiff(DIFF), { added: ['assets/new.png'] });
    expect(files.map((f) => f.path)).toEqual(['src/a.ts', 'logo.png']);
    expect(files[0]).toMatchObject({ status: 'modified', additions: 1, deletions: 1, image: false });
    expect(files[1].image).toBe(true);
  });

  it('uses the status lists, and falls back to the diff headers', () => {
    const newFile = ['diff --git a/x.ts b/x.ts', 'new file mode 100644', '--- /dev/null', '+++ b/x.ts', '@@ -0,0 +1 @@', '+hi'].join('\n');
    const files = buildDiffFiles(splitUnifiedDiff(newFile));
    expect(files[0].status).toBe('added');
    const byList = buildDiffFiles(splitUnifiedDiff(DIFF), { deleted: ['logo.png'] });
    expect(byList[1].status).toBe('deleted');
  });

  it('groups files by directory for the tree', () => {
    const files = buildDiffFiles(splitUnifiedDiff(DIFF));
    const groups = groupFilesByDirectory(files);
    expect(groups.map((g) => g.directory)).toEqual(['src', '.']);
  });

  it('detects image extensions only', () => {
    expect(isImagePath('a/b/icon.PNG')).toBe(true);
    expect(isImagePath('a/b/icon.svg')).toBe(true);
    expect(isImagePath('a/b/icon.ts')).toBe(false);
  });
});

describe('pairSplitRows', () => {
  it('aligns a removed run with the added run that replaced it', () => {
    const lines = [
      { kind: 'context' as const, text: 'same', oldLine: 1, newLine: 1 },
      { kind: 'del' as const, text: 'old', oldLine: 2 },
      { kind: 'add' as const, text: 'new', newLine: 2 },
      { kind: 'context' as const, text: 'tail', oldLine: 3, newLine: 3 },
    ];
    const rows = pairSplitRows(lines);
    expect(rows).toHaveLength(3);
    expect(rows[0].context).toBe(true);
    expect(rows[1].left?.text).toBe('old');
    expect(rows[1].right?.text).toBe('new');
    expect(rows[2].context).toBe(true);
  });

  it('keeps a blank opposite cell when one side is longer', () => {
    const rows = pairSplitRows([
      { kind: 'del' as const, text: 'a', oldLine: 1 },
      { kind: 'del' as const, text: 'b', oldLine: 2 },
      { kind: 'add' as const, text: 'c', newLine: 1 },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[1].left?.text).toBe('b');
    expect(rows[1].right).toBeUndefined();
  });

  it('stands a lone added line on the right', () => {
    const rows = pairSplitRows([{ kind: 'add' as const, text: 'x', newLine: 9 }]);
    expect(rows[0].left).toBeUndefined();
    expect(rows[0].right?.text).toBe('x');
  });
});
