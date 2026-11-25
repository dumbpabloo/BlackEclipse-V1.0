'use client';

// ─── Presentation editor (Canvas kind `presentation`) ─────────────────────────
// The deck JSON is the artifact; this is its editing surface. Every change is
// written back as content and saved by the canvas revision guard. Geometry and
// fit come from the shared layout engine, so what the stage shows is what the
// exporter will draw.

import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from 'react';
import {
  ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Copy, LayoutTemplate,
  Plus, Play, Trash2, X,
} from '@/lib/icons';
import {
  DECK_SIZES, SLIDE_LAYOUTS, deckChartSchema, deckTableSchema, deckSlideTitle,
  nextSlideId, parseDeck, serializeDeck, type DeckSpec, type Slide, type SlideLayout,
} from '@/lib/presentation/schema';
import { THEME_PRESETS } from '@/lib/presentation/theme';
import { cn } from '@/lib/utils';
import { SlideStage, SlideThumbnail } from './render-slide';
import type { CanvasEditorHandle } from './canvas-utils';

interface PresentationEditorProps {
  content: string;
  editable?: boolean;
  docKey: string;
  onChange: (next: string) => void;
  handleRef?: MutableRefObject<CanvasEditorHandle | null>;
}

function withDefaults(slide: Slide): Slide {
  if (slide.layout === 'metric_row' && slide.metrics.length === 0) {
    return { ...slide, metrics: [{ label: 'Metric', value: 'n/a' }] };
  }
  if (slide.layout === 'chart' && !slide.chart) {
    return { ...slide, chart: deckChartSchema.parse({ kind: 'line' }) };
  }
  if (slide.layout === 'table' && !slide.table) {
    return { ...slide, table: deckTableSchema.parse({}) };
  }
  return slide;
}

function newSlide(deck: DeckSpec, layout: SlideLayout = 'bullets'): Slide {
  return withDefaults({
    id: nextSlideId(deck), layout, title: 'New slide', bullets: [''], metrics: [],
  });
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground/60">{label}</span>
      {children}
    </label>
  );
}

const inputClass =
  'w-full rounded-md border border-border/40 bg-black/20 px-2 py-1 text-[12px] text-foreground outline-none focus:border-primary';

function PresentMode({ deck, start, onClose }: { deck: DeckSpec; start: number; onClose: () => void }) {
  const [index, setIndex] = useState(start);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'ArrowRight' || event.key === ' ') setIndex((prev) => Math.min(prev + 1, deck.slides.length - 1));
      if (event.key === 'ArrowLeft') setIndex((prev) => Math.max(prev - 1, 0));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [deck.slides.length, onClose]);
  return (
    <div className="fixed inset-0 z-[80] flex flex-col bg-black" role="dialog" aria-label="Present deck">
      <div className="flex items-center justify-between px-3 py-2 text-[11px] text-white/60">
        <span>{deck.meta.title}</span>
        <span className="flex items-center gap-2">
          {index + 1} / {deck.slides.length}
          <button type="button" aria-label="Exit presentation" onClick={onClose} className="rounded p-1 hover:bg-white/10">
            <X className="h-4 w-4" />
          </button>
        </span>
      </div>
      <div className="flex min-h-0 flex-1 items-center justify-center px-6 pb-6">
        <SlideStage deck={deck} slide={deck.slides[index]} page={index + 1} interactive className="max-h-full w-full max-w-[1400px] shadow-2xl" />
      </div>
      <div className="flex items-center justify-center gap-3 pb-3">
        <button type="button" aria-label="Previous slide" onClick={() => setIndex((prev) => Math.max(prev - 1, 0))}
          className="rounded p-1.5 text-white/70 hover:bg-white/10"><ChevronLeft className="h-4 w-4" /></button>
        <button type="button" aria-label="Next slide" onClick={() => setIndex((prev) => Math.min(prev + 1, deck.slides.length - 1))}
          className="rounded p-1.5 text-white/70 hover:bg-white/10"><ChevronRight className="h-4 w-4" /></button>
      </div>
    </div>
  );
}

export default function PresentationEditor({
  content, editable = true, docKey, onChange, handleRef,
}: PresentationEditorProps) {
  const parsed = useMemo(() => {
    try {
      return { deck: parseDeck(content), error: null as string | null };
    } catch (error) {
      return { deck: null, error: error instanceof Error ? error.message : 'Invalid deck' };
    }
  }, [content]);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [presenting, setPresenting] = useState(false);
  const history = useRef<{ past: string[]; future: string[] }>({ past: [], future: [] });
  const contentRef = useRef(content);
  contentRef.current = content;

  useEffect(() => {
    // A new artifact/version resets the selection; an edit does not.
    if (parsed.deck && !parsed.deck.slides.some((slide) => slide.id === selectedId)) {
      setSelectedId(parsed.deck.slides[0]?.id ?? null);
    }
  }, [docKey, parsed.deck, selectedId]);

  const update = useCallback((next: DeckSpec) => {
    const previous = contentRef.current;
    history.current.past.push(previous);
    if (history.current.past.length > 50) history.current.past.shift();
    history.current.future = [];
    onChange(serializeDeck(next));
  }, [onChange]);

  const undo = useCallback(() => {
    const previous = history.current.past.pop();
    if (previous === undefined) return;
    history.current.future.push(contentRef.current);
    onChange(previous);
  }, [onChange]);

  const redo = useCallback(() => {
    const next = history.current.future.pop();
    if (next === undefined) return;
    history.current.past.push(contentRef.current);
    onChange(next);
  }, [onChange]);

  useEffect(() => {
    if (handleRef) handleRef.current = { undo, redo };
  }, [handleRef, undo, redo]);

  if (!parsed.deck) {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-2">
        <p className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-200">
          This deck is not valid JSON for the deck schema. Fix the content below, or ask the copilot to repair it.
        </p>
        <textarea
          className={cn(inputClass, 'min-h-[40vh] flex-1 font-mono')}
          value={content}
          readOnly={!editable}
          onChange={(event) => onChange(event.target.value)}
          spellCheck={false}
        />
      </div>
    );
  }

  const deck = parsed.deck;
  const selectedIndex = Math.max(0, deck.slides.findIndex((slide) => slide.id === selectedId));
  const selected = deck.slides[selectedIndex] ?? deck.slides[0];

  const patchSlide = (patch: Partial<Slide>) => {
    if (!editable || !selected) return;
    update({
      ...deck,
      slides: deck.slides.map((slide) => (slide.id === selected.id ? { ...slide, ...patch } : slide)),
    });
  };

  const addSlide = () => {
    const slide = newSlide(deck);
    update({ ...deck, slides: [...deck.slides, slide] });
    setSelectedId(slide.id);
  };

  const duplicateSlide = () => {
    if (!selected) return;
    const copy = { ...selected, id: nextSlideId(deck) };
    const slides = [...deck.slides];
    slides.splice(selectedIndex + 1, 0, copy);
    update({ ...deck, slides });
    setSelectedId(copy.id);
  };

  const deleteSlide = () => {
    if (!selected || deck.slides.length <= 1) return;
    const slides = deck.slides.filter((slide) => slide.id !== selected.id);
    update({ ...deck, slides });
    setSelectedId(slides[Math.max(0, selectedIndex - 1)]?.id ?? null);
  };

  const moveSlide = (direction: -1 | 1) => {
    if (!selected) return;
    const target = selectedIndex + direction;
    if (target < 0 || target >= deck.slides.length) return;
    const slides = [...deck.slides];
    const [moved] = slides.splice(selectedIndex, 1);
    slides.splice(target, 0, moved);
    update({ ...deck, slides });
  };

  const changeLayout = (layout: SlideLayout) => patchSlide(withDefaults({ ...selected, layout }));

  return (
    <div className="flex min-h-0 flex-1 gap-2">
      {/* Slide rail */}
      <aside className="flex w-[112px] shrink-0 flex-col gap-1 overflow-y-auto pr-1" aria-label="Slides">
        {deck.slides.map((slide, index) => (
          <button
            key={slide.id}
            type="button"
            onClick={() => setSelectedId(slide.id)}
            className={cn(
              'group relative rounded-md border p-0.5 text-left transition-colors',
              slide.id === selected?.id ? 'border-primary/70 bg-primary/10' : 'border-border/30 hover:border-border/60',
            )}
            aria-label={`Slide ${index + 1}: ${deckSlideTitle(slide)}`}
          >
            <SlideThumbnail deck={deck} slide={slide} page={index + 1} />
            <span className="absolute bottom-0.5 right-1 rounded bg-black/60 px-1 text-[9px] text-white/80">{index + 1}</span>
          </button>
        ))}
        {editable ? (
          <button type="button" onClick={addSlide}
            className="flex items-center justify-center gap-1 rounded-md border border-dashed border-border/40 py-1.5 text-[11px] text-muted-foreground hover:text-foreground">
            <Plus className="h-3 w-3" /> Slide
          </button>
        ) : null}
      </aside>

      {/* Stage + inspector */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
          <LayoutTemplate className="h-3.5 w-3.5" />
          <span>{deck.slides.length} slides</span>
          <span className="mx-1 text-border">·</span>
          <select
            aria-label="Deck size"
            value={deck.size}
            disabled={!editable}
            onChange={(event) => update({ ...deck, size: event.target.value as DeckSpec['size'] })}
            className="rounded border border-border/40 bg-transparent px-1 py-0.5 text-[11px]"
          >
            {DECK_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
          <select
            aria-label="Theme"
            value={deck.theme.preset}
            disabled={!editable}
            onChange={(event) => update({
              ...deck, theme: { ...deck.theme, preset: event.target.value as DeckSpec['theme']['preset'] },
            })}
            className="rounded border border-border/40 bg-transparent px-1 py-0.5 text-[11px]"
          >
            {THEME_PRESETS.map((preset) => <option key={preset} value={preset}>{preset}</option>)}
          </select>
          <span className="ml-auto flex items-center gap-1">
            <button type="button" aria-label="Move slide up" disabled={!editable} onClick={() => moveSlide(-1)}
              className="rounded p-1 hover:bg-white/5 disabled:opacity-30"><ChevronUp className="h-3.5 w-3.5" /></button>
            <button type="button" aria-label="Move slide down" disabled={!editable} onClick={() => moveSlide(1)}
              className="rounded p-1 hover:bg-white/5 disabled:opacity-30"><ChevronDown className="h-3.5 w-3.5" /></button>
            <button type="button" aria-label="Duplicate slide" disabled={!editable} onClick={duplicateSlide}
              className="rounded p-1 hover:bg-white/5 disabled:opacity-30"><Copy className="h-3.5 w-3.5" /></button>
            <button type="button" aria-label="Delete slide" disabled={!editable || deck.slides.length <= 1} onClick={deleteSlide}
              className="rounded p-1 hover:bg-white/5 disabled:opacity-30"><Trash2 className="h-3.5 w-3.5" /></button>
            <button type="button" aria-label="Present" onClick={() => setPresenting(true)}
              className="ml-1 flex items-center gap-1 rounded border border-border/40 px-2 py-0.5 hover:bg-white/5">
              <Play className="h-3 w-3" /> Present
            </button>
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-border/30 bg-black/20 p-2">
          {selected ? <SlideStage deck={deck} slide={selected} page={selectedIndex + 1} interactive className="mx-auto w-full max-w-[980px] rounded-lg shadow-lg" /> : null}
        </div>

        {editable && selected ? (
          <div className="grid shrink-0 grid-cols-2 gap-2 rounded-xl border border-border/30 bg-black/10 p-2 lg:grid-cols-4">
            <Field label="Layout">
              <select value={selected.layout} onChange={(event) => changeLayout(event.target.value as SlideLayout)} className={inputClass}>
                {SLIDE_LAYOUTS.map((layout) => <option key={layout} value={layout}>{layout}</option>)}
              </select>
            </Field>
            <Field label="Title">
              <input value={selected.title ?? ''} onChange={(event) => patchSlide({ title: event.target.value })} className={inputClass} />
            </Field>
            <Field label="Subtitle">
              <input value={selected.subtitle ?? ''} onChange={(event) => patchSlide({ subtitle: event.target.value })} className={inputClass} />
            </Field>
            <Field label="Source / provenance">
              <input value={selected.source ?? ''} onChange={(event) => patchSlide({ source: event.target.value })} className={inputClass} />
            </Field>
            <Field label="Bullets (one per line)">
              <textarea
                value={(selected.bullets.length ? selected.bullets : ['']).join('\n')}
                onChange={(event) => patchSlide({ bullets: event.target.value.split('\n').slice(0, 7) })}
                className={cn(inputClass, 'min-h-[56px]')}
              />
            </Field>
            <Field label="Body / statement">
              <textarea value={selected.body ?? selected.statement ?? ''}
                onChange={(event) => patchSlide(selected.layout === 'statement' || selected.layout === 'quote'
                  ? { statement: event.target.value } : { body: event.target.value })}
                className={cn(inputClass, 'min-h-[56px]')} />
            </Field>
            <Field label="Speaker notes">
              <textarea value={selected.notes ?? ''} onChange={(event) => patchSlide({ notes: event.target.value })}
                className={cn(inputClass, 'min-h-[56px]')} />
            </Field>
            <Field label="Image artifact id (optional)">
              <input value={selected.image?.artifactId ?? ''}
                onChange={(event) => patchSlide({ image: { artifactId: event.target.value || undefined, alt: selected.image?.alt ?? '', fit: selected.image?.fit ?? 'cover' } })}
                className={inputClass} />
            </Field>
          </div>
        ) : null}
      </div>

      {presenting ? <PresentMode deck={deck} start={selectedIndex} onClose={() => setPresenting(false)} /> : null}
    </div>
  );
}
