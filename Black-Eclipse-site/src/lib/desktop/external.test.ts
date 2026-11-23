import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  externalLinkProps,
  hasExternalBridge,
  openExternal,
  openExternalFromClick,
  OPEN_EXTERNAL_EVENT,
} from './external';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('openExternal', () => {
  it('emits the URL to the desktop shell', () => {
    const emit = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI__: { event: { emit } } });
    expect(openExternal('https://github.com/blackeclipse/releases')).toBe(true);
    expect(emit).toHaveBeenCalledWith(OPEN_EXTERNAL_EVENT, {
      url: 'https://github.com/blackeclipse/releases',
    });
  });

  it('falls back to the raw IPC event emit', () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI_INTERNALS__: { invoke } });
    expect(openExternal('https://example.com/a')).toBe(true);
    expect(invoke).toHaveBeenCalledWith('plugin:event|emit', {
      event: OPEN_EXTERNAL_EVENT,
      payload: { url: 'https://example.com/a' },
    });
  });

  it('refuses non-http(s) URLs and a browser with no shell', () => {
    vi.stubGlobal('window', { __TAURI__: { event: { emit: vi.fn() } } });
    expect(openExternal('file:///C:/Windows/System32/cmd.exe')).toBe(false);
    vi.stubGlobal('window', {});
    expect(hasExternalBridge()).toBe(false);
    expect(openExternal('https://example.com')).toBe(false);
  });
});

describe('externalLinkProps', () => {
  it('keeps a normal anchor in a browser and intercepts in the shell', () => {
    vi.stubGlobal('window', {});
    const browser = externalLinkProps('https://example.com');
    const browserEvent = { preventDefault: vi.fn() };
    browser.onClick?.(browserEvent);
    expect(browserEvent.preventDefault).not.toHaveBeenCalled();
    expect(browser.href).toBe('https://example.com');

    const emit = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI__: { event: { emit } } });
    const shellEvent = { preventDefault: vi.fn() };
    externalLinkProps('https://example.com/download')!.onClick?.(shellEvent);
    expect(shellEvent.preventDefault).toHaveBeenCalled();
    expect(emit).toHaveBeenCalled();
  });

  it('returns empty props when there is no URL', () => {
    expect(externalLinkProps(null)).toEqual({
      href: undefined,
      target: undefined,
      rel: undefined,
      onClick: undefined,
    });
  });

  it('openExternalFromClick handles only real http(s) URLs', () => {
    const emit = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI__: { event: { emit } } });
    const event = { preventDefault: vi.fn() };
    expect(openExternalFromClick(event, 'mailto:someone@example.com')).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});
