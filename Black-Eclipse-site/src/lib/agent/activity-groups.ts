// ── Activity placement (gap #16) ────────────────────────────────────────────
// Eclipse's feed was one flat list of steps. This groups it the way a coding
// session is actually read: context, navigation/read, file changes, processes and
// delegated agents, in chronological run order. It also extracts the files the
// turn changed, which the feed shows as a "changed this turn" strip so the user
// can see the edit before the prose.
//
// Pure: no React, no network. The classifier is name-based because the runtime's
// activity payloads are content-free by contract; anything unrecognised is
// `other`, never guessed into a category that implies a file changed.

import type { ActivityItem } from './response-model';

export type ActivityCategory = 'context' | 'read' | 'file' | 'events' | 'subagent' | 'other';

const CATEGORY_ORDER: ActivityCategory[] = ['context', 'read', 'file', 'events', 'subagent', 'other'];

const CONTEXT_NAMES = new Set([
  'code_todo', 'project_rules', 'workspace_docs', 'workspace_status', 'memory_get', 'memory_search',
  'recall', 'clarify', 'tool_search', 'load_tool_schema', 'read_tool_output', 'plan',
]);

const READ_NAMES = new Set([
  'read_file', 'list_directory', 'search_text', 'search_files', 'search_symbols',
  'code_get_diagnostics', 'read_files', 'grep', 'glob', 'review_change',
]);

const FILE_NAMES = new Set([
  'code_edit', 'write_file', 'create_file', 'delete_file', 'move_file', 'apply_diff',
  'multi_file_patch', 'edit_file',
]);

const EVENT_PREFIXES = ['command_', 'terminal_', 'execute_', 'start_process', 'stop_process', 'shell_', 'process_', 'git_'];
const EVENT_NAMES = new Set([
  'command_run', 'terminal_run', 'execute_command', 'start_process', 'stop_process',
  'shell_spawn', 'process_output', 'checkpoint_restore', 'checkpoint_undo_turn',
]);

const SUBAGENT_NAMES = new Set(['delegate', 'subagent', 'task', 'run_team', 'parallel_agents']);

/** The category one step belongs to. Unknown names are `other`. */
export function activityCategory(item: ActivityItem): ActivityCategory {
  const name = item.name.toLowerCase();
  if (item.kind === 'retrieval' || item.kind === 'recall') return 'context';
  if (SUBAGENT_NAMES.has(name) || name.startsWith('subagent')) return 'subagent';
  if (FILE_NAMES.has(name)) return 'file';
  if (READ_NAMES.has(name)) return 'read';
  if (CONTEXT_NAMES.has(name)) return 'context';
  if (EVENT_NAMES.has(name) || EVENT_PREFIXES.some((prefix) => name.startsWith(prefix))) return 'events';
  return 'other';
}

export interface ActivityGroup {
  category: ActivityCategory;
  items: ActivityItem[];
}

/**
 * Group CONSECUTIVE steps of the same category. Chronology is preserved: a
 * read that happens again after an edit is a second `read` group, not merged
 * backwards, because the order is the causal record.
 */
export function groupActivities(items: ActivityItem[]): ActivityGroup[] {
  const groups: ActivityGroup[] = [];
  for (const item of items) {
    const category = activityCategory(item);
    const last = groups[groups.length - 1];
    if (last && last.category === category) last.items.push(item);
    else groups.push({ category, items: [item] });
  }
  return groups;
}

/** Counts per category, in display order, omitting empty categories. */
export function categoryCounts(items: ActivityItem[]): Array<{ category: ActivityCategory; count: number }> {
  const counts = new Map<ActivityCategory, number>();
  for (const item of items) {
    const category = activityCategory(item);
    counts.set(category, (counts.get(category) ?? 0) + 1);
  }
  return CATEGORY_ORDER
    .filter((category) => (counts.get(category) ?? 0) > 0)
    .map((category) => ({ category, count: counts.get(category)! }));
}

// A detail is "what the step acted on". For a file tool that is one path or a
// comma/newline-separated list; for a command it can be anything, so only a path
// SHAPED detail is read, and only when the step is classified as a file change.
const PATH_SHAPE = /^[^\s]+$/;

function pathShaped(value: string): boolean {
  return PATH_SHAPE.test(value) && (value.includes('/') || value.includes('\\') || /\.[A-Za-z0-9]+$/.test(value));
}

export function changedFiles(items: ActivityItem[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of items) {
    if (activityCategory(item) !== 'file') continue;
    const detail = (item.detail ?? '').trim();
    if (!detail) continue;
    for (const raw of detail.split(/[,\n]/)) {
      const path = raw.trim().replace(/\s+\(.*\)$/, '');
      if (!path || !pathShaped(path) || seen.has(path)) continue;
      seen.add(path);
      out.push(path);
    }
  }
  return out;
}
