'use client';

// ── The one video player ────────────────────────────────────────────────────
// One reusable surface for every produced MP4 the app shows (the chat video job
// card, the Artifact Center detail view, an inline run artifact). It is
// deliberately DOMAIN-FREE: it takes a same-origin URL and renders playback.
//
// Why it exists: bare `<video controls>` elements were the whole player before.
// They had no poster, no honest failure state, no retry, no lifecycle handling,
// and duplicated markup in three places. This component owns those concerns:
//
//  - explicit phases (`loading → ready → playing → ended | error`), so a stream
//    that fails says why instead of showing a black box;
//  - a retry (cache-busted) and a download / open-in-browser fallback that works
//    even when the codec cannot be played in this window;
//  - one-player-at-a-time, pause-offscreen, keyboard control, mute/volume,
//    playback speed, fullscreen and a poster frame;
//  - no window access during render, so it is SSR-safe (the tests render it
//    with react-dom/server under the node environment).

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import {
  Download,
  ExternalLink,
  Loader2,
  Maximize2,
  Minimize2,
  Pause,
  Play,
  RotateCcw,
  Volume2,
  VolumeX,
  AlertTriangle,
} from '@/lib/icons';
import { cn } from '@/lib/utils';

export interface VideoPlayerProps {
  /** Same-origin URL the bytes stream from (the artifact inline route). */
  src: string;
  /** Optional real poster frame (the artifact thumbnail). */
  poster?: string;
  /** Download file name. */
  fileName?: string;
  /** Download URL when it differs from `src` (the attachment route). */
  downloadHref?: string;
  /** Intercept a download click (the desktop shell owns the write). */
  onDownload?: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
  className?: string;
  /** Compact chrome for a chat card. */
  compact?: boolean;
  /** Accessible name; defaults to "Video player". */
  label?: string;
}

type Phase = 'loading' | 'ready' | 'error';

const SPEEDS = [1, 1.25, 1.5, 2, 0.5] as const;

/** The one playing element app-wide, so two clips never talk over each other. */
let activeVideo: HTMLVideoElement | null = null;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** `m:ss` / `h:mm:ss`, with a stable placeholder before metadata arrives. */
export function formatMediaTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, '0') : String(minutes);
  return `${hours > 0 ? `${hours}:` : ''}${mm}:${String(secs).padStart(2, '0')}`;
}

/** An honest sentence for each MediaError code (never a raw number alone). */
export function describeMediaError(code: number | undefined): string {
  switch (code) {
    case 1:
      return 'Playback was interrupted. Try again.';
    case 2:
      return 'The video could not be loaded — the local runtime connection dropped. Retry, or download the file.';
    case 3:
      return 'This video could not be decoded. Download it or open it in your browser.';
    case 4:
      return 'This file is not playable in this window. Download it or open it in your browser.';
    default:
      return 'The video could not be played. Retry, or download the file.';
  }
}

function withRetry(src: string, attempt: number): string {
  if (attempt <= 0) return src;
  return `${src}${src.includes('?') ? '&' : '?'}retry=${attempt}`;
}

export function VideoPlayer({
  src,
  poster,
  fileName,
  downloadHref,
  onDownload,
  className,
  compact = false,
  label = 'Video player',
}: VideoPlayerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const seekId = useId();

  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState<Phase>('loading');
  const [errorText, setErrorText] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [ended, setEnded] = useState(false);
  const [duration, setDuration] = useState(0);
  const [current, setCurrent] = useState(0);
  const [buffered, setBuffered] = useState(0);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(1);
  const [rate, setRate] = useState<number>(1);
  const [fullscreen, setFullscreen] = useState(false);

  const playbackSrc = withRetry(src, attempt);
  const progress = duration > 0 ? clamp(current / duration, 0, 1) : 0;
  const href = downloadHref ?? src;

  // A new URL (or a retry) resets the simulated state and reloads the media.
  useEffect(() => {
    const video = videoRef.current;
    setPhase('loading');
    setErrorText(null);
    setPlaying(false);
    setWaiting(false);
    setEnded(false);
    setCurrent(0);
    setBuffered(0);
    setDuration(0);
    if (video) {
      video.load();
    }
  }, [playbackSrc]);

  // Fullscreen is entered on the CONTAINER (so the controls stay visible).
  useEffect(() => {
    const onChange = () => {
      setFullscreen(Boolean(containerRef.current && document.fullscreenElement === containerRef.current));
    };
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  // Never leave sound playing from a card that scrolled away.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (entry && !entry.isIntersecting) videoRef.current?.pause();
      },
      { threshold: 0.15 },
    );
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused || video.ended) {
      void video.play().catch(() => {
        // Autoplay policy or a vanished stream: the error handler (if any) or
        // the explicit controls remain — never a fake "playing" state.
        setPlaying(false);
      });
    } else {
      video.pause();
    }
  }, []);

  const seekTo = useCallback((seconds: number) => {
    const video = videoRef.current;
    if (!video || !Number.isFinite(seconds)) return;
    const target = clamp(seconds, 0, video.duration || 0);
    try {
      video.currentTime = target;
      setCurrent(target);
    } catch {
      // A not-yet-seekable stream keeps its old position; the player is honest.
    }
  }, []);

  const toggleMute = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !video.muted;
    if (!video.muted && video.volume === 0) video.volume = 1;
  }, []);

  const changeVolume = useCallback((next: number) => {
    const video = videoRef.current;
    if (!video) return;
    const value = clamp(next, 0, 1);
    video.volume = value;
    video.muted = value === 0;
  }, []);

  const cycleSpeed = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    const index = SPEEDS.indexOf(rate as (typeof SPEEDS)[number]);
    const next = SPEEDS[(index + 1) % SPEEDS.length];
    video.playbackRate = next;
    setRate(next);
  }, [rate]);

  const toggleFullscreen = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    if (document.fullscreenElement) {
      void document.exitFullscreen?.().catch(() => undefined);
      return;
    }
    void container.requestFullscreen?.().catch(() => undefined);
  }, []);

  const openInBrowser = useCallback(() => {
    if (typeof window === 'undefined') return;
    const absolute = new URL(playbackSrc, window.location.origin).toString();
    window.open(absolute, '_blank', 'noopener,noreferrer');
  }, [playbackSrc]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'SELECT' || target.tagName === 'BUTTON')) {
        return;
      }
      switch (event.key) {
        case ' ':
        case 'k':
        case 'K':
          event.preventDefault();
          togglePlay();
          break;
        case 'ArrowLeft':
          event.preventDefault();
          seekTo(current - 5);
          break;
        case 'ArrowRight':
          event.preventDefault();
          seekTo(current + 5);
          break;
        case 'm':
        case 'M':
          toggleMute();
          break;
        case 'f':
        case 'F':
          toggleFullscreen();
          break;
        default:
          break;
      }
    },
    [current, seekTo, toggleFullscreen, toggleMute, togglePlay],
  );

  const syncBuffered = useCallback(() => {
    const video = videoRef.current;
    if (!video || !video.buffered.length) return;
    const end = video.buffered.end(video.buffered.length - 1);
    setBuffered(video.duration > 0 ? clamp(end / video.duration, 0, 1) : 0);
  }, []);

  const retry = useCallback(() => {
    setAttempt((value) => value + 1);
  }, []);

  return (
    <div
      ref={containerRef}
      tabIndex={0}
      role="region"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn(
        'group relative w-full overflow-hidden rounded-xl border border-border/30 bg-black outline-none',
        'focus-visible:ring-2 focus-visible:ring-sky-400/70',
        fullscreen && 'flex h-full flex-col justify-center rounded-none border-0',
        className,
      )}
    >
      <video
        ref={videoRef}
        src={playbackSrc}
        poster={poster}
        preload="metadata"
        playsInline
        aria-label={label}
        className={cn(
          'block w-full bg-black',
          compact ? 'max-h-64' : 'max-h-[70vh]',
          fullscreen && 'max-h-none flex-1 object-contain',
        )}
        onClick={togglePlay}
        onDoubleClick={toggleFullscreen}
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          setDuration(Number.isFinite(video.duration) ? video.duration : 0);
          setMuted(video.muted);
          setVolume(video.volume);
          setPhase('ready');
        }}
        onCanPlay={() => setPhase('ready')}
        onPlaying={() => {
          setWaiting(false);
          setPhase('ready');
          setPlaying(true);
          setEnded(false);
        }}
        onPlay={(event) => {
          setPlaying(true);
          setEnded(false);
          const video = event.currentTarget;
          if (activeVideo && activeVideo !== video) activeVideo.pause();
          activeVideo = video;
        }}
        onPause={() => setPlaying(false)}
        onEnded={(event) => {
          setPlaying(false);
          setEnded(true);
          setCurrent(event.currentTarget.duration || duration);
        }}
        onWaiting={() => setWaiting(true)}
        onTimeUpdate={(event) => setCurrent(event.currentTarget.currentTime)}
        onProgress={syncBuffered}
        onVolumeChange={(event) => {
          setMuted(event.currentTarget.muted);
          setVolume(event.currentTarget.volume);
        }}
        onError={(event) => {
          setPhase('error');
          setPlaying(false);
          setErrorText(describeMediaError(event.currentTarget.error?.code));
        }}
      />

      {phase === 'loading' && !errorText ? (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/30">
          <Loader2 size={22} className="animate-spin text-white/80" />
          <span className="sr-only">Loading video…</span>
        </div>
      ) : null}

      {errorText ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/80 p-4 text-center">
          <AlertTriangle size={22} className="text-amber-400" />
          <p className="max-w-[46ch] text-[11.5px] leading-relaxed text-white/85">{errorText}</p>
          <div className="flex flex-wrap items-center justify-center gap-1.5">
            <button
              type="button"
              onClick={retry}
              className="inline-flex items-center gap-1 rounded-full border border-white/20 bg-white/10 px-2.5 py-1 text-[10.5px] text-white/90 transition-colors hover:bg-white/20"
            >
              <RotateCcw size={10} /> Retry
            </button>
            <a
              href={href}
              download={fileName || true}
              onClick={onDownload}
              className="inline-flex items-center gap-1 rounded-full border border-white/20 bg-white/10 px-2.5 py-1 text-[10.5px] text-white/90 transition-colors hover:bg-white/20"
            >
              <Download size={10} /> Download
            </a>
            <button
              type="button"
              onClick={openInBrowser}
              className="inline-flex items-center gap-1 rounded-full border border-white/20 bg-white/10 px-2.5 py-1 text-[10.5px] text-white/90 transition-colors hover:bg-white/20"
            >
              <ExternalLink size={10} /> Open in browser
            </button>
          </div>
        </div>
      ) : null}

      {ended && !errorText ? (
        <button
          type="button"
          onClick={() => {
            seekTo(0);
            togglePlay();
          }}
          aria-label="Replay video"
          className="absolute inset-0 flex items-center justify-center bg-black/45 transition-colors hover:bg-black/55"
        >
          <span className="flex items-center gap-1.5 rounded-full border border-white/25 bg-black/45 px-3 py-1.5 text-[11px] text-white/90">
            <RotateCcw size={12} /> Replay
          </span>
        </button>
      ) : null}

      {/* Controls — always present so a touch user (no hover) can act. */}
      <div
        className={cn(
          'absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/55 to-transparent px-2 pt-6',
          compact ? 'pb-1.5' : 'pb-2',
        )}
      >
        <div className="group/seek relative flex h-4 items-center rounded-full focus-within:ring-1 focus-within:ring-sky-400/50">
          <div className="pointer-events-none absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 overflow-hidden rounded-full bg-white/20">
            <div
              className="absolute inset-y-0 left-0 rounded-full bg-white/30"
              style={{ width: `${buffered * 100}%` }}
            />
            <div
              className="absolute inset-y-0 left-0 rounded-full bg-sky-400"
              style={{ width: `${progress * 100}%` }}
            />
          </div>
          <input
            id={seekId}
            type="range"
            min={0}
            max={duration || 0}
            step={0.05}
            value={clamp(current, 0, duration || 0)}
            disabled={duration <= 0}
            onChange={(event) => seekTo(Number(event.target.value))}
            aria-label="Seek"
            aria-valuetext={`${formatMediaTime(current)} of ${formatMediaTime(duration)}`}
            className="relative z-10 h-4 w-full cursor-pointer appearance-none bg-transparent opacity-0 disabled:cursor-default"
          />
        </div>

        <div className="mt-0.5 flex items-center gap-1.5 text-white/85">
          <button
            type="button"
            onClick={togglePlay}
            aria-label={playing ? 'Pause' : 'Play'}
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full transition-colors hover:bg-white/15"
          >
            {playing ? <Pause size={12} weight="fill" /> : <Play size={12} weight="fill" />}
          </button>

          <span className="shrink-0 font-mono text-[10px] tabular-nums text-white/75">
            {formatMediaTime(current)} / {formatMediaTime(duration)}
          </span>

          <span className="flex-1" />

          {waiting && !errorText ? <Loader2 size={10} className="animate-spin text-white/60" /> : null}

          {!compact ? (
            <button
              type="button"
              onClick={cycleSpeed}
              aria-label={`Playback speed ${rate}×`}
              title="Playback speed"
              className="hidden rounded px-1 font-mono text-[10px] text-white/75 transition-colors hover:bg-white/15 sm:inline-flex"
            >
              {rate}×
            </button>
          ) : null}

          <button
            type="button"
            onClick={toggleMute}
            aria-label={muted || volume === 0 ? 'Unmute' : 'Mute'}
            title={muted || volume === 0 ? 'Unmute' : 'Mute'}
            className="inline-flex h-6 w-6 items-center justify-center rounded-full transition-colors hover:bg-white/15"
          >
            {muted || volume === 0 ? <VolumeX size={12} /> : <Volume2 size={12} />}
          </button>

          {!compact ? (
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={muted ? 0 : volume}
              onChange={(event) => changeVolume(Number(event.target.value))}
              aria-label="Volume"
              className="hidden h-1 w-14 cursor-pointer accent-sky-400 sm:block"
            />
          ) : null}

          <a
            href={href}
            download={fileName || true}
            onClick={onDownload}
            aria-label="Download video"
            title="Download"
            className="inline-flex h-6 w-6 items-center justify-center rounded-full transition-colors hover:bg-white/15"
          >
            <Download size={12} />
          </a>

          {!compact ? (
            <button
              type="button"
              onClick={openInBrowser}
              aria-label="Open in browser"
              title="Open in browser"
              className="hidden h-6 w-6 items-center justify-center rounded-full transition-colors hover:bg-white/15 sm:inline-flex"
            >
              <ExternalLink size={12} />
            </button>
          ) : null}

          <button
            type="button"
            onClick={toggleFullscreen}
            aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            className="inline-flex h-6 w-6 items-center justify-center rounded-full transition-colors hover:bg-white/15"
          >
            {fullscreen ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
          </button>
        </div>
      </div>
    </div>
  );
}
