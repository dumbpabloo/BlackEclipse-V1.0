// ── Line-anchored review comments ───────────────────────────────────────────
// A coding review is only actionable when it names the exact place to change.
// OpenCode's session review attaches comments to diff lines; Eclipse anchors a
// comment to `path:line` and folds all of them into ONE steering prompt for the
// agent. This module is pure so the prompt shape is unit-tested.

export type ReviewSide = 'new' | 'old';

export interface ReviewComment {
  /** Repository-relative path exactly as the diff names it. */
  path: string;
  /** File line number on the side the comment was made against. */
  line: number;
  /** `new` for an added/context line, `old` for a removed line. */
  side?: ReviewSide;
  text: string;
}

export function commentKey(comment: Pick<ReviewComment, 'path' | 'line' | 'side'>): string {
  return `${comment.path}:${comment.side ?? 'new'}:${comment.line}`;
}

/** Add or replace the comment at one anchor (one note per line). */
export function upsertComment(
  comments: ReviewComment[],
  next: ReviewComment,
): ReviewComment[] {
  const key = commentKey(next);
  const index = comments.findIndex((c) => commentKey(c) === key);
  if (index === -1) return [...comments, next];
  const copy = comments.slice();
  copy[index] = next;
  return copy;
}

export function removeComment(
  comments: ReviewComment[],
  target: Pick<ReviewComment, 'path' | 'line' | 'side'>,
): ReviewComment[] {
  const key = commentKey(target);
  return comments.filter((c) => commentKey(c) !== key);
}

/** `path:line` (with a ` (removed line)` marker for the old side). */
export function anchorLabel(comment: ReviewComment): string {
  return `${comment.path}:${comment.line}${comment.side === 'old' ? ' (removed line)' : ''}`;
}

/**
 * Build the one chat turn that sends anchored notes back to the agent. The
 * optional free-form note is kept so a user can explain the theme behind the
 * individual line comments.
 */
export function buildReviewPrompt(options: {
  goal: string;
  comments: ReviewComment[];
  note?: string;
}): string {
  const { goal, comments, note } = options;
  const lines = [`/eclipsesdk ${goal}`, '', 'Review notes from the Code screen:'];
  if (note && note.trim()) lines.push(`- ${note.trim()}`);
  for (const comment of comments) {
    lines.push(`- ${anchorLabel(comment)}: ${comment.text.trim()}`);
  }
  lines.push('', 'Make these changes and re-run the checks.');
  return lines.join('\n');
}
