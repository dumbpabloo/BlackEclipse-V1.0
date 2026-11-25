// ── VideoPlayer SSR tests ────────────────────────────────────────────────────
// The vitest environment is `node` (no jsdom), so the component is rendered with
// `react-dom/server`. That pins the parts a broken player gets wrong: the real
// stream URL, metadata preload, inline playback, the poster, download fallback
// and accessible control labels. Interactive behaviour is covered by the pure
// helpers and by the browser-level acceptance check.

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import { VideoPlayer, describeMediaError, formatMediaTime } from './video-player';

const SRC = '/api/artifacts/art_1/download?inline=1';

function render(props: Partial<Parameters<typeof VideoPlayer>[0]> = {}): string {
  return renderToStaticMarkup(<VideoPlayer src={SRC} {...props} />);
}

describe('formatMediaTime', () => {
  it('formats seconds, minutes and hours', () => {
    expect(formatMediaTime(0)).toBe('0:00');
    expect(formatMediaTime(9.8)).toBe('0:09');
    expect(formatMediaTime(75)).toBe('1:15');
    expect(formatMediaTime(3671)).toBe('1:01:11');
  });

  it('never prints NaN for missing metadata', () => {
    expect(formatMediaTime(Number.NaN)).toBe('0:00');
    expect(formatMediaTime(-4)).toBe('0:00');
  });
});

describe('describeMediaError', () => {
  it('explains every MediaError code without pretending it played', () => {
    for (const code of [1, 2, 3, 4, 99]) {
      const text = describeMediaError(code);
      expect(text.length).toBeGreaterThan(10);
      expect(text.toLowerCase()).toMatch(/try again|download|browser/);
      expect(text.toLowerCase()).not.toContain('playing');
    }
  });
});

describe('VideoPlayer', () => {
  it('renders a metadata-preload, inline, poster-backed video from the inline URL', () => {
    const html = render({ poster: '/api/artifacts/art_1/thumbnail', fileName: 'recap.mp4' });
    expect(html).toContain('<video');
    expect(html).toContain(`src="${SRC}"`);
    expect(html).toContain('preload="metadata"');
    expect(html.toLowerCase()).toContain('playsinline');
    expect(html).toContain('poster="/api/artifacts/art_1/thumbnail"');
    expect(html).toContain('aria-label="Video player"');
  });

  it('offers the real controls and a download that cannot dead-end', () => {
    const html = render({ downloadHref: '/api/artifacts/art_1/download', fileName: 'recap.mp4' });
    expect(html).toContain('aria-label="Play"');
    expect(html).toContain('aria-label="Mute"');
    expect(html).toContain('aria-label="Seek"');
    expect(html).toContain('aria-label="Fullscreen"');
    expect(html).toContain('aria-label="Download video"');
    expect(html).toContain('href="/api/artifacts/art_1/download"');
    expect(html).toContain('download="recap.mp4"');
  });

  it('keeps the browser fallback available in the full-size player', () => {
    expect(render()).toContain('aria-label="Open in browser"');
    expect(render({ compact: true })).not.toContain('aria-label="Open in browser"');
  });
});
