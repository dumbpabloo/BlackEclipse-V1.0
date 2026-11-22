import { describe, expect, it } from 'vitest';

import { parseDiffLines, splitUnifiedDiff } from './diff-sections';

describe('splitUnifiedDiff', () => {
  it('splits a multi-file diff and names each file', () => {
    const diff = [
      'diff --git a/src/a.ts b/src/a.ts',
      '--- a/src/a.ts',
      '+++ b/src/a.ts',
      '@@ -1 +1 @@',
      '-old',
      '+new',
      'diff --git a/src/b.ts b/src/b.ts',
      '--- a/src/b.ts',
      '+++ b/src/b.ts',
      '@@ -1 +1 @@',
      '-x',
      '+y',
    ].join('\n');
    const sections = splitUnifiedDiff(diff);
    expect(sections.map((section) => section.path)).toEqual(['src/a.ts', 'src/b.ts']);
    expect(sections[0].text).toContain('+new');
    expect(sections[1].text).toContain('+y');
  });

  it('falls back to the +++ path when there is no diff header', () => {
    const sections = splitUnifiedDiff(['--- a/x.py', '+++ b/x.py', '@@ -1 +1 @@'].join('\n'));
    expect(sections).toHaveLength(1);
    expect(sections[0].path).toBe('x.py');
  });

  it('returns nothing for an empty diff', () => {
    expect(splitUnifiedDiff('')).toEqual([]);
  });
});

describe('parseDiffLines', () => {
  it('numbers both sides of a hunk so a comment can anchor to a real line', () => {
    const lines = parseDiffLines([
      'diff --git a/src/a.ts b/src/a.ts',
      '--- a/src/a.ts',
      '+++ b/src/a.ts',
      '@@ -3,3 +3,4 @@',
      ' keep',
      '-old',
      '+new one',
      '+new two',
      ' tail',
    ].join('\n'));
    expect(lines.map((l) => l.kind)).toEqual(['meta', 'meta', 'meta', 'hunk', 'context', 'del', 'add', 'add', 'context']);
    const removed = lines.find((l) => l.kind === 'del')!;
    expect(removed.oldLine).toBe(4);
    const added = lines.filter((l) => l.kind === 'add');
    expect(added.map((l) => l.newLine)).toEqual([4, 5]);
    expect(lines.at(-1)).toMatchObject({ kind: 'context', oldLine: 5, newLine: 6 });
  });

  it('starts every hunk from its own header numbers', () => {
    const lines = parseDiffLines(['@@ -10 +20 @@', '-a', '+b'].join('\n'));
    expect(lines.find((l) => l.kind === 'del')?.oldLine).toBe(10);
    expect(lines.find((l) => l.kind === 'add')?.newLine).toBe(20);
  });

  it('leaves header and no-newline markers unnumbered', () => {
    const lines = parseDiffLines(['+++ b/x', '\\ No newline at end of file'].join('\n'));
    expect(lines.every((l) => l.newLine === undefined && l.oldLine === undefined)).toBe(true);
    expect(lines.map((l) => l.kind)).toEqual(['meta', 'meta']);
  });
});
