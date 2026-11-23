import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  downloadFromUrlInShell,
  hasDownloadBridge,
  saveDownloadViaShell,
  SAVE_DOWNLOAD_EVENT,
} from './download';

afterEach(() => {
  vi.unstubAllGlobals();
});

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('hasDownloadBridge', () => {
  it('is false in a browser and true with either shell API', () => {
    vi.stubGlobal('window', {});
    expect(hasDownloadBridge()).toBe(false);
    vi.stubGlobal('window', { __TAURI__: { event: { emit: vi.fn() } } });
    expect(hasDownloadBridge()).toBe(true);
    vi.stubGlobal('window', { __TAURI_INTERNALS__: { invoke: vi.fn() } });
    expect(hasDownloadBridge()).toBe(true);
  });
});

describe('saveDownloadViaShell', () => {
  it('returns false without a shell so the caller can keep the anchor path', () => {
    vi.stubGlobal('window', {});
    const blob = new Blob(['hello'], { type: 'text/plain' });
    expect(saveDownloadViaShell('a.txt', blob)).toBe(false);
  });

  it('sends the file bytes and name to the shell over the save event', async () => {
    const emit = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI__: { event: { emit } } });
    const blob = new Blob(['%PDF-1.4'], { type: 'application/pdf' });
    expect(saveDownloadViaShell('report.pdf', blob)).toBe(true);
    await tick();
    expect(emit).toHaveBeenCalledTimes(1);
    const [event, payload] = emit.mock.calls[0] as [string, { name: string; dataBase64: string }];
    expect(event).toBe(SAVE_DOWNLOAD_EVENT);
    expect(payload.name).toBe('report.pdf');
    expect(payload.dataBase64.length).toBeGreaterThan(0);
  });
});

describe('downloadFromUrlInShell', () => {
  it('intercepts the click, fetches the bytes and saves them through the shell', async () => {
    const emit = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('window', { __TAURI__: { event: { emit } } });
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(new Blob(['bytes']), {
          status: 200,
          headers: { 'content-disposition': 'attachment; filename="Q3 report.pdf"' },
        }),
      ),
    );
    const preventDefault = vi.fn();
    expect(downloadFromUrlInShell({ preventDefault }, '/api/artifacts/a1/download')).toBe(true);
    expect(preventDefault).toHaveBeenCalled();
    await tick();
    await tick();
    expect(emit).toHaveBeenCalledTimes(1);
    expect((emit.mock.calls[0][1] as { name: string }).name).toBe('Q3 report.pdf');
  });

  it('leaves the link alone in a browser', () => {
    vi.stubGlobal('window', {});
    const preventDefault = vi.fn();
    expect(downloadFromUrlInShell({ preventDefault }, '/api/artifacts/a1/download')).toBe(false);
    expect(preventDefault).not.toHaveBeenCalled();
  });
});
