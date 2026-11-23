'use client';

// ─── Artifact Center ─────────────────────────────────────────────────────────
// The central catalog of everything Eclipse (and the user) has produced. Real,
// server-backed data only: search/filter/sort/pagination are server-side; every
// visible action (download, preview, rename, favorite, archive, restore, delete,
// bulk) calls the artifact API. No mock cards, no local-only state.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { confirmDialog, promptDialog } from '@/lib/ui/confirm-dialog';
import { createPortal } from 'react-dom';
import {
  AlertTriangle, ChartLine, Check, Download, FileCode2,
  FileText, Filter, FolderOpen, Image as ImageIcon, LayoutGrid, List,
  Loader2, Redo2, Search, Star, Trash2, X, SquarePen,
} from '@/lib/icons';
import { cn } from '@/lib/utils';
import { useCommandStore } from '@/lib/search/store';
import { ServerPaginationFooter } from '@/components/ui/pagination';
import { useT } from '@/lib/i18n/use-t';
import type { TranslateFn } from '@/lib/i18n/translate';
import type { TranslationKey } from '@/lib/i18n/types';
import {
  archiveArtifact, bulkAction, deleteArtifact, downloadUrl,
  getPreview, inlineUrl, listArtifacts, listTags, listVersions, restoreArtifact,
  thumbnailUrl, updateArtifact,
  type Artifact, type ArtifactCategory, type ArtifactPreview,
  type ArtifactVersion, type ListParams,
} from '@/lib/harness/clients/artifacts-browser';
import { downloadFromUrlInShell } from '@/lib/desktop/download';

type ViewMode = 'grid' | 'list';

const SORTS: { value: string; key: TranslationKey }[] = [
  { value: 'newest', key: 'artifacts.sort.newest' },
  { value: 'oldest', key: 'artifacts.sort.oldest' },
  { value: 'modified', key: 'artifacts.sort.modified' },
  { value: 'name', key: 'artifacts.sort.name' },
  { value: 'size', key: 'artifacts.sort.size' },
];

const CATEGORIES: ArtifactCategory[] = [
  'document', 'image', 'spreadsheet', 'dataset', 'chart', 'code',
  'script', 'log', 'audio', 'video', 'archive', 'report', 'financial',
  'backtest', 'presentation', 'other',
];

function categoryIcon(category: ArtifactCategory) {
  switch (category) {
    case 'image': return ImageIcon;
    case 'chart': return ChartLine;
    case 'code':
    case 'script': return FileCode2;
    case 'audio':
    case 'video': return FileCode2;
    default: return FileText;
  }
}

function humanSize(bytes: number | null): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
}

function humanDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

function statusTone(status: Artifact['status']): string {
  switch (status) {
    case 'ACTIVE': return 'text-emerald-400';
    case 'PROCESSING': return 'text-sky-400';
    case 'ARCHIVED': return 'text-amber-400';
    case 'MISSING':
    case 'FAILED': return 'text-rose-400';
    case 'DELETED':
    case 'PURGED': return 'text-muted-foreground/70';
    default: return 'text-muted-foreground';
  }
}

// ── Card / row ────────────────────────────────────────────────────────────────

function ArtifactCard({
  artifact, selected, onToggle, onOpen, t,
}: {
  artifact: Artifact;
  selected: boolean;
  onToggle: (id: string) => void;
  onOpen: (artifact: Artifact) => void;
  t: TranslateFn;
}) {
  const Icon = categoryIcon(artifact.category);
  const isVideo = artifact.category === 'video';
  // Only request the thumbnail when the runtime actually recorded one. Older
  // image/video artifacts without a generated thumbnail used to render a
  // 404 <img> that collapsed to an empty block — they now fall back to the
  // category icon instead of a blank card.
  const [thumbFailed, setThumbFailed] = useState(false);
  const hasPoster = !thumbFailed && Boolean(artifact.thumbnailKey) && (artifact.category === 'image' || isVideo);
  return (
    <div
      className={cn(
        'glass-card group relative flex flex-col rounded-xl overflow-hidden cursor-pointer transition-transform hover:-translate-y-0.5',
        selected && 'ring-2 ring-sky-400/60',
      )}
      onClick={() => onOpen(artifact)}
      data-sound="tap"
    >
      <button
        type="button"
        aria-label={selected ? 'Deselect' : 'Select'}
        onClick={(e) => { e.stopPropagation(); onToggle(artifact.id); }}
        className={cn(
          'absolute left-2 top-2 z-10 h-5 w-5 rounded-md glass-card-subtle flex items-center justify-center',
          selected ? 'text-sky-400' : 'text-muted-foreground/50 opacity-0 group-hover:opacity-100',
        )}
      >
        {selected ? <Check className="h-3.5 w-3.5" weight="bold" /> : null}
      </button>
      <div className="h-28 w-full flex items-center justify-center bg-black/20 overflow-hidden">
        {hasPoster ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={thumbnailUrl(artifact.id)}
            alt=""
            className="h-full w-full object-cover"
            onError={() => setThumbFailed(true)}
          />
        ) : (
          <Icon className="h-9 w-9 text-muted-foreground/70" />
        )}
      </div>
      <div className="p-3 flex flex-col gap-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm font-medium text-foreground" title={artifact.name}>
            {artifact.name}
          </span>
          {artifact.favorite ? <Star className="h-3.5 w-3.5 text-amber-400 shrink-0" weight="fill" /> : null}
        </div>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground/70">
          <span className={cn('uppercase tracking-wide', statusTone(artifact.status))}>
            {artifact.status === 'ACTIVE' ? t('artifacts.status.active') : artifact.status.toLowerCase()}
          </span>
          <span>·</span>
          <span>{artifact.category}</span>
          <span>·</span>
          <span>{humanSize(artifact.sizeBytes)}</span>
        </div>
        <div className="text-[10px] text-muted-foreground/50">{humanDate(artifact.createdAt)}</div>
      </div>
    </div>
  );
}

function ArtifactRow({
  artifact, selected, onToggle, onOpen, t,
}: {
  artifact: Artifact;
  selected: boolean;
  onToggle: (id: string) => void;
  onOpen: (artifact: Artifact) => void;
  t: TranslateFn;
}) {
  const Icon = categoryIcon(artifact.category);
  return (
    <div
      className={cn(
        'glass-card-subtle flex items-center gap-3 rounded-lg px-3 py-2 cursor-pointer hover:bg-white/5',
        selected && 'ring-1 ring-sky-400/60',
      )}
      onClick={() => onOpen(artifact)}
    >
      <button
        type="button"
        aria-label={selected ? 'Deselect' : 'Select'}
        onClick={(e) => { e.stopPropagation(); onToggle(artifact.id); }}
        className={cn('h-5 w-5 rounded-md glass-card-subtle flex items-center justify-center',
          selected ? 'text-sky-400' : 'text-muted-foreground/40')}
      >
        {selected ? <Check className="h-3.5 w-3.5" weight="bold" /> : null}
      </button>
      <Icon className="h-5 w-5 text-muted-foreground/70 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm text-foreground">{artifact.name}</span>
          {artifact.favorite ? <Star className="h-3 w-3 text-amber-400" weight="fill" /> : null}
        </div>
        <div className="text-[11px] text-muted-foreground/60 truncate">
          {artifact.description || artifact.mimeType}
        </div>
      </div>
      <span className="hidden md:inline text-[11px] text-muted-foreground/60 w-20">{artifact.category}</span>
      <span className="hidden md:inline text-[11px] text-muted-foreground/60 w-20 text-right">{humanSize(artifact.sizeBytes)}</span>
      <span className={cn('text-[11px] w-24 text-right', statusTone(artifact.status))}>
        {artifact.status === 'ACTIVE' ? t('artifacts.status.active') : artifact.status.toLowerCase()}
      </span>
      <span className="hidden lg:inline text-[11px] text-muted-foreground/50 w-40 text-right">
        {humanDate(artifact.createdAt)}
      </span>
    </div>
  );
}

// ── Preview rendering ─────────────────────────────────────────────────────────

function CsvPreview({ text }: { text: string }) {
  const rows = useMemo(() => {
    const lines = text.split(/\r?\n/).filter((l) => l.length > 0).slice(0, 51);
    return lines.map((line) => line.split(','));
  }, [text]);
  if (rows.length === 0) return null;
  const [header, ...body] = rows;
  return (
    <div className="overflow-auto max-h-72 rounded-lg glass-card-subtle">
      <table className="w-full text-[11px]">
        <thead className="sticky top-0 bg-black/40">
          <tr>{header.map((h, i) => <th key={i} className="px-2 py-1 text-left text-muted-foreground">{h}</th>)}</tr>
        </thead>
        <tbody>
          {body.map((row, i) => (
            <tr key={i} className="border-t border-white/5">
              {row.map((cell, j) => <td key={j} className="px-2 py-1 text-foreground/80">{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PreviewBody({ artifact, preview }: { artifact: Artifact; preview: ArtifactPreview | null }) {
  if (!preview) return <div className="text-sm text-muted-foreground/60">Loading…</div>;
  if (artifact.status === 'MISSING') {
    return <PreviewUnavailable />;
  }
  if (!preview.hasDownload) {
    return <PreviewUnavailable />;
  }
  switch (preview.kind) {
    case 'image':
      // eslint-disable-next-line @next/next/no-img-element
      return <img src={downloadUrl(artifact.id)} alt={artifact.name} className="max-h-80 rounded-lg" />;
    case 'video':
      // Plays inline (the download route serves `inline=1` and honors byte
      // ranges, so seeking works). A honest fallback below when the browser
      // cannot play the codec.
      return (
        <video
          controls
          preload="metadata"
          playsInline
          src={inlineUrl(artifact.id)}
          className="max-h-80 w-full rounded-lg bg-black/40"
        />
      );
    case 'pdf':
      return (
        <iframe
          src={downloadUrl(artifact.id)}
          title={artifact.name}
          sandbox=""
          className="w-full h-80 rounded-lg glass-card-subtle"
        />
      );
    case 'table':
      return preview.text
        ? <CsvPreview text={preview.text} />
        : <PreviewUnavailable />;
    case 'json':
    case 'code':
    case 'text':
    case 'markdown':
      return (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg glass-card-subtle p-3 text-[11px] leading-relaxed text-foreground/85">
          {preview.text ?? ''}
        </pre>
      );
    default:
      return <PreviewUnavailable />;
  }
}

function PreviewUnavailable() {
  return (
    <div className="glass-card-subtle rounded-lg p-4 text-sm text-muted-foreground/70 flex items-center gap-2">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      No preview is available — you can still download the file.
    </div>
  );
}

// ── Detail drawer ─────────────────────────────────────────────────────────────

function DetailDrawer({
  artifact, onClose, onChanged, t,
}: {
  artifact: Artifact;
  onClose: () => void;
  onChanged: () => void;
  t: TranslateFn;
}) {
  const [detail, setDetail] = useState<Artifact>(artifact);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(artifact);
    setPreview(null);
    getPreview(artifact.id).then((p) => { if (!cancelled) setPreview(p); }).catch(() => {});
    listVersions(artifact.id).then((v) => { if (!cancelled) setVersions(v); }).catch(() => {});
    return () => { cancelled = true; };
  }, [artifact]);

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setBusy(false);
    }
  }, [onChanged]);

  const rename = async () => {
    const next = await promptDialog({ title: 'Rename artifact', defaultValue: detail.name });
    if (next && next.trim() && next !== detail.name) {
      run(async () => {
        const updated = await updateArtifact(detail.id, { name: next.trim() });
        setDetail(updated);
      });
    }
  };

  const toggleFavorite = () => run(async () => {
    const updated = await updateArtifact(detail.id, { favorite: !detail.favorite });
    setDetail(updated);
  });

  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <aside className="relative glass-card-strong w-full max-w-lg h-full overflow-y-auto p-5 flex flex-col gap-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-foreground truncate" title={detail.name}>{detail.name}</h2>
            <p className="text-xs text-muted-foreground/70">
              {detail.category} · {detail.mimeType} · {humanSize(detail.sizeBytes)} · v{detail.versionNumber}
            </p>
          </div>
          <button type="button" aria-label={t('artifacts.close')} onClick={onClose}
            className="glass-card-subtle rounded-lg p-1.5 text-muted-foreground hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>

        {error ? (
          <div className="glass-card-subtle rounded-lg border border-rose-500/30 p-2 text-xs text-rose-300">{error}</div>
        ) : null}

        <PreviewBody artifact={detail} preview={preview} />

        <div className="flex flex-wrap gap-2">
          <a href={downloadUrl(detail.id)} onClick={(event) => downloadFromUrlInShell(event, downloadUrl(detail.id), detail.name)} className="glass-card-subtle rounded-lg px-3 py-1.5 text-xs text-foreground flex items-center gap-1.5 hover:bg-white/5">
            <Download className="h-3.5 w-3.5" /> {t('artifacts.download')}
          </a>
          <button type="button" onClick={rename} className="glass-card-subtle rounded-lg px-3 py-1.5 text-xs text-foreground hover:bg-white/5">
            {t('artifacts.rename')}
          </button>
          <button type="button" onClick={toggleFavorite} className="glass-card-subtle rounded-lg px-3 py-1.5 text-xs text-foreground hover:bg-white/5 flex items-center gap-1.5">
            <Star className="h-3.5 w-3.5" weight={detail.favorite ? 'fill' : 'regular'} />
            {detail.favorite ? t('artifacts.unfavorite') : t('artifacts.favorite')}
          </button>
          {detail.status === 'ARCHIVED' ? (
            <button type="button" onClick={() => run(async () => { setDetail(await restoreArtifact(detail.id)); })}
              className="glass-card-subtle rounded-lg px-3 py-1.5 text-xs text-foreground hover:bg-white/5">
              {t('artifacts.restore')}
            </button>
          ) : (
            <button type="button" onClick={() => run(async () => { setDetail(await archiveArtifact(detail.id)); })}
              className="glass-card-subtle rounded-lg px-3 py-1.5 text-xs text-foreground hover:bg-white/5">
              {t('artifacts.archive')}
            </button>
          )}
          <button type="button"
            onClick={async () => {
              if (await confirmDialog({ title: t('artifacts.deleteConfirm'), tone: 'danger' })) {
                run(async () => { await deleteArtifact(detail.id); onClose(); });
              }
            }}
            className="glass-card-subtle rounded-lg px-3 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10 flex items-center gap-1.5">
            <Trash2 className="h-3.5 w-3.5" /> {t('artifacts.delete')}
          </button>
        </div>

        {detail.tags.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {detail.tags.map((tag) => (
              <span key={tag} className="glass-card-subtle rounded-full px-2 py-0.5 text-[10px] text-muted-foreground/80">{tag}</span>
            ))}
          </div>
        ) : null}

        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs uppercase tracking-wide text-muted-foreground/60">{t('artifacts.provenance')}</h3>
          <dl className="grid grid-cols-2 gap-1 text-[11px]">
            {(['agentId', 'taskId', 'runId', 'projectId', 'sourceTool'] as const).map((k) =>
              detail[k] ? (
                <div key={k} className="col-span-1 flex flex-col">
                  <dt className="text-muted-foreground/50">{k}</dt>
                  <dd className="text-foreground/80 font-mono truncate" title={String(detail[k])}>{String(detail[k])}</dd>
                </div>
              ) : null,
            )}
          </dl>
          {detail.taskId ? (
            <a href={`/?tab=chat&taskId=${encodeURIComponent(detail.taskId)}`}
              className="text-[11px] text-sky-400 hover:underline">{t('artifacts.openTask')}</a>
          ) : null}
          {detail.metadata && (detail.metadata as { canvas?: unknown }).canvas ? (
            <button
              type="button"
              onClick={() => useCommandStore.getState().dispatch({ tab: 'chat', params: { artifactId: detail.id } })}
              className="inline-flex items-center gap-1 self-start text-[11px] text-sky-400 hover:underline"
            >
              <SquarePen className="h-3 w-3" /> Edit in Canvas
            </button>
          ) : null}
        </section>

        {versions.length > 1 ? (
          <section className="flex flex-col gap-1.5">
            <h3 className="text-xs uppercase tracking-wide text-muted-foreground/60">{t('artifacts.versions')}</h3>
            {versions.map((v) => (
              <div key={v.id} className="glass-card-subtle rounded-lg px-2.5 py-1.5 flex items-center justify-between text-[11px]">
                <span className="text-foreground/80">
                  {t('artifacts.version')} {v.versionNumber}
                  {v.changeNote ? <span className="text-muted-foreground/60"> · {v.changeNote}</span> : null}
                </span>
                <span className="flex items-center gap-2 text-muted-foreground/60">
                  {humanSize(v.sizeBytes)}
                  <a href={downloadUrl(detail.id, v.versionNumber)} onClick={(event) => downloadFromUrlInShell(event, downloadUrl(detail.id, v.versionNumber), detail.name)} className="text-sky-400 hover:underline">
                    <Download className="h-3.5 w-3.5" />
                  </a>
                </span>
              </div>
            ))}
          </section>
        ) : null}

        {busy ? <div className="text-[11px] text-muted-foreground/60 flex items-center gap-1.5"><Loader2 className="h-3 w-3 animate-spin" /> Working…</div> : null}
      </aside>
    </div>,
    document.body,
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

const DEFAULT_PAGE_SIZE = 24;
const MAX_PAGE_SIZE = 200;

export function ArtifactsScreen() {
  const t = useT();
  const [items, setItems] = useState<Artifact[]>([]);
  const [total, setTotal] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [category, setCategory] = useState<ArtifactCategory | ''>('');
  const [sort, setSort] = useState('newest');
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [view, setView] = useState<ViewMode>('grid');
  const [tags, setTags] = useState<string[]>([]);
  const [activeTag, setActiveTag] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<Artifact | null>(null);
  const reqSeq = useRef(0);

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 300);
    return () => window.clearTimeout(id);
  }, [query]);

  const params = useCallback((): ListParams => ({
    q: debounced || undefined,
    category: category || undefined,
    sort,
    favorite: favoritesOnly ? true : undefined,
    includeArchived,
    tags: activeTag ? [activeTag] : undefined,
    limit: pageSize,
    offset: 0,
  }), [debounced, category, sort, favoritesOnly, includeArchived, activeTag, pageSize]);

  // Server-side pagination: fetch only the requested page. `load` replaces the
  // list (no more append-only "Load more"), so the footer can show real page
  // numbers over the full filtered total.
  const load = useCallback(async (nextOffset = 0) => {
    const seq = ++reqSeq.current;
    setLoading(true);
    setError(null);
    try {
      const page = await listArtifacts({ ...params(), offset: nextOffset });
      if (seq !== reqSeq.current) return; // stale response — ignore
      // A delete can empty the last page; step back to the new last page.
      if (page.artifacts.length === 0 && nextOffset > 0 && page.total > 0) {
        const lastOffset = Math.max(0, (Math.ceil(page.total / pageSize) - 1) * pageSize);
        if (lastOffset !== nextOffset) { void load(lastOffset); return; }
      }
      setItems(page.artifacts);
      setTotal(page.total);
      setOffset(nextOffset);
    } catch (e) {
      if (seq === reqSeq.current) setError(e instanceof Error ? e.message : 'Failed to load');
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [params, pageSize]);

  // Any filter/sort/page-size change starts again at the first page.
  useEffect(() => { void load(0); }, [load]);
  useEffect(() => { listTags().then(setTags).catch(() => {}); }, []);

  // Lightweight realtime: refresh the current page periodically while mounted.
  useEffect(() => {
    const id = window.setInterval(() => { if (!document.hidden) void load(offset); }, 12_000);
    return () => window.clearInterval(id);
  }, [load, offset]);

  const toggle = (id: string) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });

  const runBulk = async (action: string) => {
    if (selected.size === 0) return;
    if (action === 'delete' && !(await confirmDialog({ title: `Delete ${selected.size} artifact(s)?`, tone: 'danger' }))) return;
    try {
      await bulkAction([...selected], action);
      setSelected(new Set());
      await load(offset);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Bulk action failed');
    }
  };

  const retry = () => void load(offset);

  return (
    <div className="flex flex-col gap-4 w-full">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold text-foreground">{t('artifacts.title')}</h1>
        <p className="text-sm text-muted-foreground/70">{t('artifacts.subtitle')}</p>
      </header>

      <div className="glass-card rounded-xl p-3 flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[12rem]">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/50" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('artifacts.search')}
            aria-label={t('artifacts.search')}
            className="glass-card-subtle w-full rounded-lg pl-8 pr-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground/40 outline-none"
          />
        </div>

        <label className="glass-card-subtle rounded-lg px-2 py-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          <Filter className="h-3.5 w-3.5" />
          <select value={category} onChange={(e) => setCategory(e.target.value as ArtifactCategory | '')}
            className="bg-transparent text-foreground outline-none">
            <option value="">{t('artifacts.filter.all')}</option>
            {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>

        <label className="glass-card-subtle rounded-lg px-2 py-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          <select value={sort} onChange={(e) => setSort(e.target.value)}
            className="bg-transparent text-foreground outline-none">
            {SORTS.map((s) => <option key={s.value} value={s.value}>{t(s.key)}</option>)}
          </select>
        </label>

        <button type="button" onClick={() => setFavoritesOnly((v) => !v)}
          aria-pressed={favoritesOnly}
          className={cn('glass-card-subtle rounded-lg px-2.5 py-1.5 text-xs flex items-center gap-1.5',
            favoritesOnly ? 'text-amber-400' : 'text-muted-foreground')}>
          <Star className="h-3.5 w-3.5" weight={favoritesOnly ? 'fill' : 'regular'} />
        </button>

        <button type="button" onClick={() => setIncludeArchived((v) => !v)}
          aria-pressed={includeArchived}
          className={cn('glass-card-subtle rounded-lg px-2.5 py-1.5 text-xs',
            includeArchived ? 'text-amber-400' : 'text-muted-foreground')}>
          {t('artifacts.filter.archived')}
        </button>

        <div className="glass-card-subtle rounded-lg p-0.5 flex items-center gap-0.5">
          <button type="button" aria-label={t('artifacts.grid')} onClick={() => setView('grid')}
            className={cn('rounded-md p-1.5', view === 'grid' ? 'text-foreground bg-white/10' : 'text-muted-foreground')}>
            <LayoutGrid className="h-4 w-4" />
          </button>
          <button type="button" aria-label={t('artifacts.list')} onClick={() => setView('list')}
            className={cn('rounded-md p-1.5', view === 'list' ? 'text-foreground bg-white/10' : 'text-muted-foreground')}>
            <List className="h-4 w-4" />
          </button>
        </div>
      </div>

      {tags.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          <button type="button" onClick={() => setActiveTag('')}
            className={cn('glass-card-subtle rounded-full px-2.5 py-0.5 text-[11px]', !activeTag ? 'text-foreground' : 'text-muted-foreground/70')}>
            {t('artifacts.filter.all')}
          </button>
          {tags.map((tag) => (
            <button key={tag} type="button" onClick={() => setActiveTag((cur) => (cur === tag ? '' : tag))}
              className={cn('glass-card-subtle rounded-full px-2.5 py-0.5 text-[11px]',
                activeTag === tag ? 'text-sky-400' : 'text-muted-foreground/70')}>
              {tag}
            </button>
          ))}
        </div>
      ) : null}

      {selected.size > 0 ? (
        <div className="glass-card rounded-xl px-3 py-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-foreground">{t('artifacts.selected', { n: selected.size })}</span>
          <button type="button" onClick={() => runBulk('favorite')} className="glass-card-subtle rounded-lg px-2 py-1 hover:bg-white/5">{t('artifacts.favorite')}</button>
          <button type="button" onClick={() => runBulk('archive')} className="glass-card-subtle rounded-lg px-2 py-1 hover:bg-white/5">{t('artifacts.bulkArchive')}</button>
          <button type="button" onClick={() => runBulk('delete')} className="glass-card-subtle rounded-lg px-2 py-1 text-rose-300 hover:bg-rose-500/10">{t('artifacts.bulkDelete')}</button>
          <button type="button" onClick={() => setSelected(new Set())} className="ml-auto text-muted-foreground/60 hover:text-foreground">{t('artifacts.close')}</button>
        </div>
      ) : null}

      {error ? (
        <div className="glass-card-subtle rounded-xl border border-rose-500/30 p-4 flex items-center gap-3 text-sm text-rose-300">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span className="flex-1">{error}</span>
          <button type="button" onClick={retry} className="glass-card-subtle rounded-lg px-2.5 py-1 text-xs flex items-center gap-1.5">
            <Redo2 className="h-3.5 w-3.5" /> {t('artifacts.retry')}
          </button>
        </div>
      ) : null}

      {items.length === 0 && !loading && !error ? (
        <div className="glass-card rounded-xl p-10 flex flex-col items-center gap-2 text-center">
          <FolderOpen className="h-8 w-8 text-muted-foreground/50" />
          <p className="text-sm text-muted-foreground/70">
            {debounced || category || favoritesOnly || activeTag ? t('artifacts.emptyFiltered') : t('artifacts.empty')}
          </p>
        </div>
      ) : view === 'grid' ? (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-3">
          {items.map((a) => (
            <ArtifactCard key={a.id} artifact={a} selected={selected.has(a.id)} onToggle={toggle} onOpen={setOpen} t={t} />
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {items.map((a) => (
            <ArtifactRow key={a.id} artifact={a} selected={selected.has(a.id)} onToggle={toggle} onOpen={setOpen} t={t} />
          ))}
        </div>
      )}

      <div className="flex items-center justify-center gap-3 py-2">
        {loading ? <Loader2 className="h-4 w-4 animate-spin text-muted-foreground/60" /> : null}
      </div>

      <ServerPaginationFooter
        total={total}
        limit={pageSize}
        offset={offset}
        count={items.length}
        onOffset={(next) => void load(next)}
        onLimit={setPageSize}
        maxPageSize={MAX_PAGE_SIZE}
        numbered
        disabled={loading}
        label="artifacts"
      />

      {open ? (
        <DetailDrawer artifact={open} onClose={() => setOpen(null)} onChanged={() => void load(offset)} t={t} />
      ) : null}
    </div>
  );
}
