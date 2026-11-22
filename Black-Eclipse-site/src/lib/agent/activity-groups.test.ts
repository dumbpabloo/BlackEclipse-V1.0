import { describe, expect, it } from 'vitest';

import type { ActivityItem } from './response-model';
import {
  activityCategory,
  categoryCounts,
  changedFiles,
  groupActivities,
} from './activity-groups';

function item(name: string, extra: Partial<ActivityItem> = {}): ActivityItem {
  return {
    id: `${name}-${Math.random().toString(36).slice(2, 7)}`,
    kind: 'tool',
    name,
    label: name,
    status: 'completed',
    ...extra,
  };
}

describe('activityCategory', () => {
  it('places read, file, process, delegation and context steps', () => {
    expect(activityCategory(item('read_file'))).toBe('read');
    expect(activityCategory(item('search_text'))).toBe('read');
    expect(activityCategory(item('write_file'))).toBe('file');
    expect(activityCategory(item('apply_diff'))).toBe('file');
    expect(activityCategory(item('command_run'))).toBe('events');
    expect(activityCategory(item('git_commit'))).toBe('events');
    expect(activityCategory(item('delegate'))).toBe('subagent');
    expect(activityCategory(item('code_todo'))).toBe('context');
    expect(activityCategory(item('market_get_quote'))).toBe('other');
  });

  it('treats retrieval as context regardless of the name', () => {
    expect(activityCategory(item('anything', { kind: 'retrieval' }))).toBe('context');
    expect(activityCategory(item('x', { kind: 'recall' }))).toBe('context');
  });
});

describe('groupActivities', () => {
  it('groups CONSECUTIVE steps and preserves chronology', () => {
    const groups = groupActivities([
      item('read_file'),
      item('search_text'),
      item('code_edit'),
      item('read_file'),
    ]);
    expect(groups.map((g) => g.category)).toEqual(['read', 'file', 'read']);
    expect(groups[0].items).toHaveLength(2);
    expect(groups[2].items).toHaveLength(1);
  });

  it('counts every category once, in display order', () => {
    const counts = categoryCounts([
      item('code_edit'), item('read_file'), item('read_file'), item('command_run'),
    ]);
    expect(counts).toEqual([
      { category: 'read', count: 2 },
      { category: 'file', count: 1 },
      { category: 'events', count: 1 },
    ]);
  });
});

describe('changedFiles', () => {
  it('extracts only path-shaped details from file-change steps', () => {
    const files = changedFiles([
      item('code_edit', { detail: 'src/app.ts' }),
      item('write_file', { detail: 'src/lib/x.py, README.md' }),
      item('command_run', { detail: 'npm test' }),
      item('read_file', { detail: 'package.json' }),
      item('apply_diff', { detail: 'not a path' }),
    ]);
    expect(files).toEqual(['src/app.ts', 'src/lib/x.py', 'README.md']);
  });

  it('deduplicates a repeated path and keeps first-seen order', () => {
    const files = changedFiles([
      item('code_edit', { detail: 'b.ts' }),
      item('write_file', { detail: 'a.ts' }),
      item('code_edit', { detail: 'b.ts' }),
    ]);
    expect(files).toEqual(['b.ts', 'a.ts']);
  });

  it('returns nothing when no step changed a file', () => {
    expect(changedFiles([item('read_file', { detail: 'src/a.ts' })])).toEqual([]);
  });
});
