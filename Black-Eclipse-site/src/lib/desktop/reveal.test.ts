import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  absoluteWorkspacePath,
  hasRevealBridge,
  isAbsolutePath,
  revealInOs,
  REVEAL_PATH_EVENT,
} from './reveal';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('isAbsolutePath', () => {
  it('accepts POSIX and Windows drive paths only', () => {
    expect(isAbsolutePath('/home/me/project/a.ts')).toBe(true);
    expect(isAbsolutePath('C:\\project\\a.ts')).toBe(true);
    expect(isAbsolutePath('C:/project/a.ts')).toBe(true);
    expect(isAbsolutePath('src/a.ts')).toBe(false);
    expect(isAbsolutePath('../a.ts')).toBe(false);
  });
});

describe('absoluteWorkspacePath', () => {
  it('joins with the root separator and normalizes the relative path', () => {
    expect(absoluteWorkspacePath('C:\\work\\app', 'src/lib/a.ts')).toBe('C:\\work\\app\\src\\lib\\a.ts');
    expect(absoluteWorkspacePath('/work/app/', '/src/a.ts')).toBe('/work/app/src/a.ts');
  });

  it('refuses a relative root or an empty relative path', () => {
    expect(absoluteWorkspacePath('work/app', 'src/a.ts')).toBeNull();
    expect(absoluteWorkspacePath('C:\\work\\app', '')).toBeNull();
    expect(absoluteWorkspacePath(null, 'src/a.ts')).toBeNull();
  });
});

describe('revealInOs', () => {
  it('emits the reveal event to the desktop bridge', () => {
    const emit = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI__: { event: { emit } } });
    expect(revealInOs('C:\\project\\a.ts')).toBe(true);
    expect(emit).toHaveBeenCalledWith(REVEAL_PATH_EVENT, { path: 'C:\\project\\a.ts' });
  });

  it('uses the raw IPC fallback when the global API is absent', () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI_INTERNALS__: { invoke } });
    expect(revealInOs('/work/a.ts')).toBe(true);
    expect(invoke).toHaveBeenCalledWith('plugin:event|emit', {
      event: REVEAL_PATH_EVENT,
      payload: { path: '/work/a.ts' },
    });
  });

  it('does nothing in a browser or for a relative path', () => {
    vi.stubGlobal('window', {});
    expect(hasRevealBridge()).toBe(false);
    expect(revealInOs('/work/a.ts')).toBe(false);
    vi.stubGlobal('window', { __TAURI__: { event: { emit: vi.fn() } } });
    expect(revealInOs('src/a.ts')).toBe(false);
  });
});
