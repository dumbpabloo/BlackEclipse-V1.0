import { describe, expect, it } from 'vitest';

import {
  anchorLabel,
  buildReviewPrompt,
  commentKey,
  removeComment,
  upsertComment,
  type ReviewComment,
} from './review-comments';

const line = (line: number, text = 'fix this'): ReviewComment => ({
  path: 'src/app.ts',
  line,
  side: 'new',
  text,
});

describe('review comments', () => {
  it('keeps ONE note per anchor and separates the old side', () => {
    let comments: ReviewComment[] = [];
    comments = upsertComment(comments, line(4, 'first'));
    comments = upsertComment(comments, line(4, 'replaced'));
    expect(comments).toHaveLength(1);
    expect(comments[0].text).toBe('replaced');
    comments = upsertComment(comments, { ...line(4, 'removed line note'), side: 'old' });
    expect(comments).toHaveLength(2);
    expect(commentKey(comments[1])).not.toBe(commentKey(comments[0]));
  });

  it('removes only the targeted anchor', () => {
    const comments = [line(4), line(9)];
    expect(removeComment(comments, { path: 'src/app.ts', line: 4, side: 'new' })).toEqual([comments[1]]);
  });

  it('labels an anchor with its path and line', () => {
    expect(anchorLabel(line(12))).toBe('src/app.ts:12');
    expect(anchorLabel({ ...line(12), side: 'old' })).toBe('src/app.ts:12 (removed line)');
  });

  it('builds ONE steering prompt with every anchored note', () => {
    const prompt = buildReviewPrompt({
      goal: 'Make the tests pass',
      note: 'Keep the public API stable.',
      comments: [line(4, 'guard the empty case'), line(9, 'rename this helper')],
    });
    expect(prompt.split('\n')[0]).toBe('/eclipsesdk Make the tests pass');
    expect(prompt).toContain('- Keep the public API stable.');
    expect(prompt).toContain('- src/app.ts:4: guard the empty case');
    expect(prompt).toContain('- src/app.ts:9: rename this helper');
    expect(prompt.trimEnd().endsWith('Make these changes and re-run the checks.')).toBe(true);
  });

  it('works with anchored notes and no free-form summary', () => {
    const prompt = buildReviewPrompt({ goal: 'g', comments: [line(2)] });
    expect(prompt).toContain('- src/app.ts:2: fix this');
    expect(prompt).not.toContain('Review notes from the Code screen:\n- \n');
  });
});
