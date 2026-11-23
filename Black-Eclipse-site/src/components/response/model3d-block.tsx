'use client';

// ── Model3dBlock — an interactive, orbitable 3D model ─────────────────────────
//
// Renders a declarative `model3d` block. The scene is compiled purely by
// `lib/model3d/compile.ts`; the WebGL viewport is loaded lazily (ssr:false) so
// `ogl` never enters the initial chat bundle and never runs during SSR/tests.
// The numbered key, legend, readout and stages live in the DOM, so the figure
// stays readable and accessible even if WebGL is unavailable.

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import dynamic from 'next/dynamic';
import { createPortal } from 'react-dom';
import { ChevronLeft, ChevronRight, Download, Maximize2, Pause, Play, RotateCcw, X } from '@/lib/icons';
import { cn } from '@/lib/utils';
import { useStillness } from '@/lib/anatomy/live';
import type { Model3dBlock as Model3dBlockType } from '@/lib/response/contract';
import { compileModel3dCached, model3dStats, type CompiledModel3d } from '@/lib/model3d/compile';
import { buildGlb } from '@/lib/model3d/glb';
import { model3dToObj, model3dToStl } from '@/lib/model3d/exporters';
import { figureUrl } from '@/lib/response/share';
import { saveDownloadViaShell } from '@/lib/desktop/download';
import { Model3dFallback } from './model3d-fallback';

const Model3dCanvas = dynamic(() => import('./model3d-canvas').then((m) => m.Model3dCanvas), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center" style={{ backgroundColor: '#17181b' }}>
      <span className="h-6 w-6 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
    </div>
  ),
});

const AUTOPLAY_MS = 2600;

// Flat charcoal sketch panel: #161618 ground, always visible behind the (alpha)
// WebGL canvas, so the figure can never read as a white rectangle. The 3D solids
// fill with this same colour and are read by their #3A3E44 outlines.
const PAPER_STYLE: CSSProperties = {
  backgroundColor: '#161618',
};

export interface Model3dBlockProps {
  block: Model3dBlockType;
  className?: string;
}

function stageLitIds(lit: boolean | string[] | undefined, all: string[]): Set<string> {
  if (lit === true) return new Set(all);
  if (Array.isArray(lit)) return new Set(lit);
  return new Set();
}

function StageButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-white/15 bg-black/45 text-white/80 backdrop-blur-sm transition-colors hover:bg-black/70 hover:text-white"
    >
      {children}
    </button>
  );
}

function LegendChip({ mark, label }: { mark?: string; label: string }) {
  const m = (mark ?? '').toLowerCase();
  const dot = m.includes('red') ? 'bg-red-400'
    : m.includes('green') ? 'bg-emerald-400'
      : m.includes('blue') ? 'bg-blue-400'
        : (m.includes('warn') || m.includes('amber')) ? 'bg-amber-400'
          : m.includes('metal') ? 'bg-zinc-300'
            : 'bg-zinc-400';
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
      <span className={cn('inline-block h-2.5 w-2.5 shrink-0 rounded-sm', dot)} />
      {label}
    </span>
  );
}

function safeName(name: string): string {
  return name.replace(/[^a-z0-9-_]+/gi, '-').slice(0, 60) || 'eclipse-model';
}

function downloadBlob(blob: Blob, filename: string): void {
  // Same rule as every other download: the desktop shell writes the file.
  if (saveDownloadViaShell(filename, blob)) return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function downloadScene(scene: CompiledModel3d, name: string): void {
  const bytes = buildGlb(scene.parts, scene.stages);
  downloadBlob(new Blob([bytes.buffer as ArrayBuffer], { type: 'model/gltf-binary' }), `${safeName(name)}.glb`);
}

function downloadObj(scene: CompiledModel3d, name: string): void {
  downloadBlob(new Blob([model3dToObj(scene.parts)], { type: 'text/plain' }), `${safeName(name)}.obj`);
}

function downloadStl(scene: CompiledModel3d, name: string): void {
  const bytes = model3dToStl(scene.parts);
  downloadBlob(new Blob([bytes.buffer as ArrayBuffer], { type: 'model/stl' }), `${safeName(name)}.stl`);
}

interface BodyProps {
  block: Model3dBlockType;
  scene: CompiledModel3d;
  sceneKey: string;
  litIds: Set<string>;
  still: boolean;
  error: string | null;
  readout: string;
  onError: (message: string) => void;
  onFull?: () => void;
  large?: boolean;
  height: number;
  /** Rendered ON the viewport (stage controls), not below the figure. */
  overlay?: ReactNode;
  selected: string | null;
  onSelect: (group: string | null) => void;
}

function Model3dBody({ block, scene, sceneKey, litIds, still, error, readout, onError, onFull, large, height, overlay, selected, onSelect }: BodyProps) {
  const stats = model3dStats(scene);
  return (
    <>
      <div className="mb-3 grid grid-cols-[1fr_auto_1fr] items-center gap-2 border-b border-border/40 pb-3">
        <span className="truncate font-mono text-[10px] uppercase tracking-widest text-muted-foreground/60">
          {block.fig ?? 'Fig.'}
        </span>
        <div className="min-w-0 text-center">
          {block.title && (
            <h3 className={cn('break-words font-semibold tracking-tight text-foreground', large ? 'text-lg' : 'text-base')} title={block.title}>
              {block.title}
            </h3>
          )}
          {block.hint && <p className="mt-0.5 text-[11px] text-muted-foreground">{block.hint}</p>}
        </div>
        <div className="flex items-center justify-self-end gap-1.5">
          {onFull && (
            <button
              type="button"
              onClick={onFull}
              title="View full page"
              aria-label="View full page"
              className="inline-flex items-center gap-1 whitespace-nowrap rounded-full border border-border/50 bg-accent/30 px-2.5 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
            >
              <Maximize2 size={12} /> Full page
            </button>
          )}
        </div>
      </div>

      <div
        className="relative overflow-hidden rounded-lg border border-[#3A3E44]"
        style={{ ...PAPER_STYLE, height }}
        role="img"
        aria-label={block.description ?? `3D model with ${stats.parts} parts`}
      >
        {error ? (
          <div className="flex h-full flex-col">
            <div className="min-h-0 flex-1">
              {scene.parts.length > 0 && <Model3dFallback scene={scene} height={height} />}
            </div>
            <p className="shrink-0 px-4 pb-2 text-center text-[11px] text-white/55">{error}</p>
          </div>
        ) : scene.parts.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[12px] text-white/60">
            No parts in this model.
          </div>
        ) : (
          <Model3dCanvas
            key={sceneKey}
            scene={scene}
            litGroups={litIds}
            still={still}
            autoRotate={block.animate === true}
            selected={selected}
            onSelect={onSelect}
            onError={onError}
          />
        )}
        {!error && scene.parts.length > 0 && (
          <>
            <span className="pointer-events-none absolute bottom-2 left-3 font-mono text-[10px] text-white/35">
              drag to orbit · ctrl-drag to pan · scroll to zoom
            </span>
            {overlay && (
              <div className="pointer-events-auto absolute inset-x-0 bottom-2 flex justify-center px-3">
                <div className="flex max-w-full flex-wrap items-center justify-center gap-x-3 gap-y-1.5 rounded-lg border border-white/10 bg-black/55 px-3 py-1.5 backdrop-blur-sm">
                  {overlay}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {scene.keys.length > 0 && (
        <ol className="mt-3 space-y-1.5">
          {scene.keys.map((key, i) => {
            const active = selected === key.id;
            const stageLit = !active && litIds.has(key.id);
            return (
              <li key={key.id}>
                <button
                  type="button"
                  onClick={() => onSelect(active ? null : key.id)}
                  aria-pressed={active}
                  aria-label={`Show part ${i + 1}: ${key.label}`}
                  className={cn(
                    'flex w-full items-start gap-2 rounded-md px-1.5 py-1 text-left text-[12px] leading-snug transition-colors',
                    active ? 'bg-accent/40 text-foreground' : 'text-muted-foreground hover:bg-accent/20',
                  )}
                >
                  <span
                    className={cn(
                      'mt-px inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border font-mono text-[10px] font-semibold',
                      active
                        ? 'border-primary bg-primary text-primary-foreground'
                        : stageLit
                          ? 'border-foreground/60 bg-foreground/15 text-foreground'
                          : 'border-border/50 text-foreground/80',
                    )}
                  >
                    {i + 1}
                  </span>
                  <span>{key.label}</span>
                </button>
              </li>
            );
          })}
        </ol>
      )}

      {(block.legend.length > 0 || readout) && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
            {block.legend.map((key, i) => (
              <LegendChip key={i} mark={key.mark} label={key.label} />
            ))}
          </div>
          {readout && <span className="font-mono text-[11px] tabular-nums text-muted-foreground">{readout}</span>}
        </div>
      )}

      {block.caption && <p className="mt-2 text-[11px] leading-snug text-muted-foreground/80">{block.caption}</p>}

      {scene.diagnostics.length > 0 && (
        <p className="mt-1 text-[11px] text-amber-400/80">{scene.diagnostics.join(' ')}</p>
      )}
    </>
  );
}

export function Model3dBlock({ block, className }: Model3dBlockProps) {
  // Content signature: a streamed answer re-renders with a fresh block object
  // every frame, but the SCENE is unchanged. Keying the memo on the content (not
  // the object) avoids recomputing all recipes and, crucially, avoids tearing the
  // WebGL viewport down and re-uploading it on every token.
  const sceneKey = useMemo(() => JSON.stringify(block), [block]);
  const scene = useMemo(() => {
    try {
      return compileModel3dCached(block, sceneKey);
    } catch {
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sceneKey]);

  const [stageIndex, setStageIndex] = useState(0);
  const [playing, setPlaying] = useState(block.animate === true);
  const [full, setFull] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const still = useStillness();
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const stages = block.stages;

  const currentStage = stageIndex > 0 ? stages[stageIndex - 1] : undefined;
  const hasStages = stages.length > 0;

  useEffect(() => {
    const shouldPlay = playing && hasStages && !still;
    if (!shouldPlay) {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
      return;
    }
    timer.current = setInterval(() => setStageIndex((prev) => (prev + 1) % (stages.length + 1)), AUTOPLAY_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
    };
  }, [playing, hasStages, still, stages.length]);

  const openFull = useCallback(() => setFull(true), []);
  const closeFull = useCallback(() => setFull(false), []);

  if (!scene) {
    return <p className={cn('text-[12px] text-muted-foreground/80', className)}>This model could not be compiled.</p>;
  }

  const litIds = stageLitIds(currentStage?.lit, scene.keys.map((k) => k.id));
  const readout = currentStage?.readout ?? block.readout ?? '';

  const controls = hasStages ? (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex items-center gap-1">
        <StageButton label="Previous stage" onClick={() => { setPlaying(false); setStageIndex((p) => (p <= 0 ? stages.length : p - 1)); }}>
          <ChevronLeft className="h-3.5 w-3.5" />
        </StageButton>
        <StageButton label={playing ? 'Pause' : 'Play'} onClick={() => setPlaying((p) => !p)}>
          {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
        </StageButton>
        <StageButton label="Next stage" onClick={() => { setPlaying(false); setStageIndex((p) => (p >= stages.length ? 0 : p + 1)); }}>
          <ChevronRight className="h-3.5 w-3.5" />
        </StageButton>
        <StageButton label="Reset" onClick={() => { setPlaying(false); setStageIndex(0); }}>
          <RotateCcw className="h-3.5 w-3.5" />
        </StageButton>
      </div>
      <span className="text-[11px] text-white/70">
        {currentStage ? (
          <>
            <span className="font-medium text-white">{currentStage.label}</span>
            {currentStage.note ? ` — ${currentStage.note}` : ''}
          </>
        ) : (
          `${stages.length} stage${stages.length === 1 ? '' : 's'}`
        )}
      </span>
    </div>
  ) : null;

  const viewer = full && typeof document !== 'undefined'
    ? createPortal(
        <div className="fixed inset-0 z-[100] flex items-center justify-center" role="dialog" aria-modal="true" aria-label="3D model (full page)">
          <button aria-label="Close full page model" onClick={closeFull} className="absolute inset-0 cursor-pointer bg-black/70 backdrop-blur-md" />
          <div className="glass-card-strong relative z-10 flex h-[92vh] w-[94vw] max-w-6xl flex-col overflow-hidden rounded-2xl border border-white/10">
            <div className="flex shrink-0 items-center justify-between gap-2 border-b border-white/10 px-4 py-2.5">
              <span className="truncate font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
                {block.title ? `${block.title} · full page` : 'Model · full page'}
              </span>
              <button type="button" onClick={closeFull} aria-label="Close" className="rounded-lg p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
                <X size={16} />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-5">
              <Model3dBody block={{ ...block, fig: undefined, title: undefined, hint: undefined }} scene={scene} sceneKey={sceneKey} litIds={litIds} still={still} error={error} readout={readout} onError={setError} large height={560} overlay={controls} selected={selected} onSelect={setSelected} />
            </div>
          </div>
        </div>,
        document.body,
      )
    : null;

  return (
    <>
      <div className={cn('be-model3d glass-card w-full rounded-xl p-4 sm:p-5', className)}>
        <Model3dBody
          block={block}
          scene={scene}
          sceneKey={sceneKey}
          litIds={litIds}
          still={still}
          error={error}
          readout={readout}
          onError={setError}
          onFull={openFull}
          height={Math.min(560, block.height)}
          overlay={controls}
          selected={selected}
          onSelect={setSelected}
        />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => downloadScene(scene, block.title ?? 'eclipse-model')}
            className="inline-flex items-center gap-1.5 rounded-full border border-border/50 bg-accent/30 px-3 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
          >
            <Download size={12} /> .glb
          </button>
          <button
            type="button"
            onClick={() => downloadObj(scene, block.title ?? 'eclipse-model')}
            className="inline-flex items-center gap-1.5 rounded-full border border-border/50 bg-accent/30 px-3 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
          >
            <Download size={12} /> .obj
          </button>
          <button
            type="button"
            onClick={() => downloadStl(scene, block.title ?? 'eclipse-model')}
            className="inline-flex items-center gap-1.5 rounded-full border border-border/50 bg-accent/30 px-3 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
          >
            <Download size={12} /> .stl
          </button>
          <button
            type="button"
            onClick={() => {
              navigator.clipboard?.writeText(figureUrl(block, window.location.origin)).then(
                () => { setCopied(true); setTimeout(() => setCopied(false), 2000); },
                () => {},
              );
            }}
            className="inline-flex items-center gap-1.5 rounded-full border border-border/50 bg-accent/30 px-3 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
          >
            {copied ? 'Link copied' : 'Share link'}
          </button>
          <span className="font-mono text-[10px] text-muted-foreground/60">
            {model3dStats(scene).parts} parts · {model3dStats(scene).triangles} tris
          </span>
        </div>
      </div>
      {viewer}
    </>
  );
}

export default Model3dBlock;
