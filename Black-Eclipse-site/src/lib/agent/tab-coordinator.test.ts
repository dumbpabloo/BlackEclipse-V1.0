import { beforeEach, describe, expect, it } from 'vitest';

class MemoryStorage {
  private data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.data.has(key) ? this.data.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
  clear(): void {
    this.data.clear();
  }
  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }
  get length(): number {
    return this.data.size;
  }
}

const storage = new MemoryStorage();

beforeEach(() => {
  storage.clear();
  (globalThis as unknown as { window?: unknown }).window = { localStorage: storage };
});

import {
  getSharedRun,
  putSharedRun,
  removeSharedRun,
  SHARED_RUNS_KEY,
  SHARED_RUN_KEY_PREFIX,
} from './tab-coordinator';

function run(chatId: string, runId: string, at = Date.now()) {
  return { chatId, runId, ownerTabId: 'tab_a', at, status: 'running' };
}

describe('shared run registry', () => {
  it('stores each conversation under its OWN key (no lost update)', () => {
    // Two tabs start two chats "at the same time". The old single-blob
    // read-modify-write could drop one of them.
    putSharedRun(run('chat_a', 'run_a'));
    putSharedRun(run('chat_b', 'run_b'));

    expect(getSharedRun('chat_a')?.runId).toBe('run_a');
    expect(getSharedRun('chat_b')?.runId).toBe('run_b');
    expect(storage.getItem(`${SHARED_RUN_KEY_PREFIX}chat_a`)).toContain('run_a');
  });

  it('removing one conversation never touches another', () => {
    putSharedRun(run('chat_a', 'run_a'));
    putSharedRun(run('chat_b', 'run_b'));

    removeSharedRun('chat_a', 'run_a');

    expect(getSharedRun('chat_a')).toBeNull();
    expect(getSharedRun('chat_b')?.runId).toBe('run_b');
  });

  it('does not remove a record when the run id differs', () => {
    putSharedRun(run('chat_a', 'run_new'));
    removeSharedRun('chat_a', 'run_old');
    expect(getSharedRun('chat_a')?.runId).toBe('run_new');
  });

  it('migrates a v1 shared-map record on read', () => {
    storage.setItem(SHARED_RUNS_KEY, JSON.stringify({ runs: { chat_old: run('chat_old', 'run_old') } }));
    expect(getSharedRun('chat_old')?.runId).toBe('run_old');
    // The next write lands under the per-chat key; the legacy map is untouched.
    putSharedRun(run('chat_old', 'run_next'));
    expect(getSharedRun('chat_old')?.runId).toBe('run_next');
  });

  it('treats an expired record as not live', () => {
    putSharedRun(run('chat_stale', 'run_stale', Date.now() - 10 * 60_000));
    expect(getSharedRun('chat_stale')).toBeNull();
  });
});
