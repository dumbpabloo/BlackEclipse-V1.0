// ── Quiet scheduled runs ─────────────────────────────────────────────────────
// A scheduled bot may honestly have nothing to report. The Python dispatcher
// instructs the model to answer with exactly one marker and keeps the execution
// row for audit; this module is the ONE client-side definition of that contract
// so the bot thread and the completion watcher cannot disagree.

/** The whole-answer markers a quiet run may use. */
export const QUIET_MARKERS = ['[[no-update]]', '[SILENT]'] as const;

/** True only when the ENTIRE answer is a quiet marker (never part of a report). */
export function isQuietAnswer(text: string | null | undefined): boolean {
  const value = (text ?? '').trim();
  return (QUIET_MARKERS as readonly string[]).includes(value);
}
