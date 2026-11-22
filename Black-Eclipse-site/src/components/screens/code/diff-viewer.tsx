'use client';

// ── Diff viewer (Phase 5; review upgrade gap #15) ───────────────────────────
// The change, described in everyday words, with the raw text and digest behind a
// "details" affordance. It never leads with a diff hunk or a tree digest.
//
// Inside "details" the change is reviewable like a code review: a per-file tree,
// unified or side-by-side (split) layout, syntax colouring, image-change cards,
// and every line is a review anchor (gap #14). The highlighter is dependency-free
// and only changes colour — the text, and therefore a pinned note, is exact.

import { useEffect, useState } from 'react';
import { ChevronRight } from '@/lib/icons';
import { useT } from '@/lib/i18n/use-t';
import { cn } from '@/lib/utils';
import { splitUnifiedDiff, type DiffLine } from '@/lib/code/diff-sections';
import { buildDiffFiles, pairSplitRows, type DiffFile, type DiffFileLists, type SplitRow } from '@/lib/code/diff-model';
import { languageFor, tokenizeLine, type Token } from '@/lib/code/highlight';
import { anchorLabel, commentKey, type ReviewComment } from '@/lib/code/review-comments';
import { DiffFileTree } from './diff-file-tree';

export type DiffSource = 'working' | 'committed' | 'revision' | 'turn';

export interface DiffViewerProps {
  files: string[];
  unified?: string;
  digest?: string | null;
  added?: string[];
  modified?: string[];
  deleted?: string[];
  /** Which comparison the diff is showing, and how to switch it. */
  mode?: DiffSource;
  onModeChange?: (mode: DiffSource) => void;
  revision?: string;
  onRevisionChange?: (base: string) => void;
  /** An honest reason the selected source has no data. */
  note?: string;
  /** Absolute workspace folder, used to reveal a file in the OS. */
  workspaceRoot?: string;
  /** Anchored notes, owned by the parent so they survive re-renders. */
  comments?: ReviewComment[];
  onAddComment?: (comment: ReviewComment) => void;
  onRemoveComment?: (comment: ReviewComment) => void;
  /** Loads a bounded picture from the task's change (inside the workspace). */
  loadImage?: (path: string, side: 'before' | 'after') => Promise<{ mime: string; bytes: string } | null>;
}

type Mode = 'unified' | 'split';
type Draft = { path: string; line: number; side: 'new' | 'old' };

const TOKEN_CLASS: Record<Token['kind'], string> = {
  plain: '',
  keyword: 'text-sky-300',
  string: 'text-amber-200',
  number: 'text-violet-300',
  comment: 'text-muted-foreground/55 italic',
  literal: 'text-emerald-300',
};

function Highlighted({ text, language }: { text: string; language: ReturnType<typeof languageFor> }) {
  return (
    <>
      {tokenizeLine(text, language).map((token, index) => (
        <span key={index} className={TOKEN_CLASS[token.kind]}>{token.text}</span>
      ))}
    </>
  );
}

function ImageChange({
  file, loadImage,
}: {
  file: DiffFile;
  loadImage?: (path: string, side: 'before' | 'after') => Promise<{ mime: string; bytes: string } | null>;
}) {
  const t = useT();
  const [before, setBefore] = useState<string | null>(null);
  const [after, setAfter] = useState<string | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'none'>(loadImage ? 'loading' : 'idle');
  useEffect(() => {
    if (!loadImage) return;
    let alive = true;
    setState('loading');
    setBefore(null);
    setAfter(null);
    const url = (image: { mime: string; bytes: string } | null) =>
      image ? `data:${image.mime};base64,${image.bytes}` : null;
    void Promise.allSettled([loadImage(file.path, 'before'), loadImage(file.path, 'after')])
      .then(([beforeResult, afterResult]) => {
        if (!alive) return;
        const beforeSrc = beforeResult.status === 'fulfilled' ? url(beforeResult.value) : null;
        const afterSrc = afterResult.status === 'fulfilled' ? url(afterResult.value) : null;
        setBefore(beforeSrc);
        setAfter(afterSrc);
        setState(beforeSrc || afterSrc ? 'ready' : 'none');
      });
    return () => { alive = false; };
  }, [file.path, loadImage]);

  const panel = (label: string, src: string | null, missing: string) => (
    <div className="min-w-0 flex-1">
      <div className="text-[9.5px] uppercase tracking-wide text-muted-foreground/55">{label}</div>
      {src ? (
        <img
          src={src}
          alt={`${file.path} (${label})`}
          className="mt-1 max-h-52 max-w-full rounded border border-border/30 bg-[repeating-conic-gradient(#0001_0%_25%,transparent_0%_50%)] bg-[length:12px_12px] object-contain"
        />
      ) : (
        <div className="mt-1 text-muted-foreground/45">{missing}</div>
      )}
    </div>
  );

  return (
    <div className="rounded-lg border border-border/40 bg-background/40 p-2 text-[10.5px]">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-muted-foreground/80">{file.path.slice(file.path.lastIndexOf('/') + 1)}</span>
        <span className="rounded border border-border/30 px-1 py-0.5 text-[9.5px] text-muted-foreground/60">
          {t('code.diff.imageChanged')}
        </span>
      </div>
      {state === 'loading' ? (
        <div className="mt-1 text-muted-foreground/55">{t('code.diff.imageLoading')}</div>
      ) : state === 'none' ? (
        <div className="mt-1 text-muted-foreground/45">{t('code.diff.imagePreviewUnavailable')}</div>
      ) : (
        <div className="mt-1.5 flex gap-2">
          {panel(t('code.diff.imageBefore'), before, t('code.diff.imageNoPrevious'))}
          {panel(t('code.diff.imageAfter'), after, t('code.diff.imageRemoved'))}
        </div>
      )}
    </div>
  );
}

export function DiffViewer({
  files, unified, digest, added, modified, deleted, comments = [], onAddComment, onRemoveComment, loadImage,
  mode = 'working', onModeChange, revision, onRevisionChange, note, workspaceRoot,
}: DiffViewerProps) {
  const t = useT();
  const [showDetails, setShowDetails] = useState(false);
  const [splitMode, setSplitMode] = useState<Mode>('unified');
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [draftRevision, setDraftRevision] = useState(revision ?? '');
  useEffect(() => { setDraftRevision(revision ?? ''); }, [revision]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftText, setDraftText] = useState('');
  const sections = splitUnifiedDiff(unified ?? '');
  const lists: DiffFileLists = { added, modified, deleted };
  const parsed = buildDiffFiles(sections, lists);
  // Status lists can name a file whose diff was truncated away; keep those in the
  // tree so the change is never silently under-reported.
  const known = new Set(parsed.map((f) => f.path));
  const extra: DiffFile[] = [];
  for (const [status, paths] of [['added', added], ['modified', modified], ['deleted', deleted]] as const) {
    for (const path of paths ?? []) {
      if (!known.has(path)) {
        known.add(path);
        extra.push({ path, status, additions: 0, deletions: 0, lines: [], image: /\.(png|jpe?g|gif|webp|bmp|ico|svg|avif|tiff?)$/i.test(path) });
      }
    }
  }
  const allFiles = [...parsed, ...extra];
  const visible = selectedPath ? allFiles.filter((f) => f.path === selectedPath) : allFiles;
  const byAnchor = new Map(comments.map((c) => [commentKey(c), c]));
  const canComment = Boolean(onAddComment);
  const totalAdd = allFiles.reduce((n, f) => n + f.additions, 0);
  const totalDel = allFiles.reduce((n, f) => n + f.deletions, 0);

  function openDraft(path: string, line: number, side: 'new' | 'old') {
    if (!canComment) return;
    const attached = byAnchor.get(commentKey({ path, line, side }));
    setDraft({ path, line, side });
    setDraftText(attached?.text ?? '');
  }

  function saveDraft() {
    if (!draft || !draftText.trim() || !onAddComment) return;
    onAddComment({ ...draft, text: draftText.trim() });
    setDraft(null);
    setDraftText('');
  }

  function noteEditor(path: string, line: number, side: 'new' | 'old') {
    if (!draft || draft.path !== path || draft.line !== line || draft.side !== side) return null;
    return (
      <div className="my-0.5 ml-10">
        <textarea
          autoFocus
          value={draftText}
          onChange={(e) => setDraftText(e.target.value)}
          placeholder={t('code.review.linePlaceholder')}
          rows={2}
          className="w-full resize-y rounded border border-border/40 bg-background/60 px-1.5 py-0.5 text-[10.5px] text-foreground"
        />
        <div className="mt-0.5 flex gap-1.5">
          <button
            type="button"
            disabled={!draftText.trim()}
            onClick={saveDraft}
            className="rounded border border-border/40 px-2 py-0.5 text-[10px] text-foreground disabled:opacity-50"
          >
            {t('code.review.saveLine')}
          </button>
          <button
            type="button"
            onClick={() => { setDraft(null); setDraftText(''); }}
            className="rounded px-2 py-0.5 text-[10px] text-muted-foreground"
          >
            {t('code.promote.cancel')}
          </button>
        </div>
      </div>
    );
  }

  function attachedNote(path: string, line: number, side: 'new' | 'old') {
    const attached = byAnchor.get(commentKey({ path, line, side }));
    if (!attached) return null;
    return (
      <div className="my-0.5 ml-10 rounded border border-primary/30 bg-primary/5 px-1.5 py-0.5 text-[10.5px] text-foreground/90">
        <div className="flex items-start justify-between gap-2">
          <span className="min-w-0 break-words">{attached.text}</span>
          {onRemoveComment && (
            <button
              type="button"
              onClick={() => onRemoveComment(attached)}
              className="shrink-0 text-muted-foreground hover:text-destructive"
              aria-label={t('code.review.remove')}
            >
              ×
            </button>
          )}
        </div>
      </div>
    );
  }

  function lineStyle(kind: DiffLine['kind']) {
    return cn(
      kind === 'add' && 'bg-primary/10 text-primary/90',
      kind === 'del' && 'bg-destructive/10 text-destructive/85',
      kind === 'hunk' && 'text-muted-foreground/60',
      kind === 'meta' && 'text-muted-foreground/40',
    );
  }

  function unifiedLine(file: DiffFile, line: DiffLine, index: number) {
    const numbered = line.kind === 'add' || line.kind === 'del' || line.kind === 'context';
    const side: 'new' | 'old' = line.kind === 'del' ? 'old' : 'new';
    const number = side === 'old' ? line.oldLine : line.newLine;
    const clickable = numbered && number !== undefined && canComment;
    return (
      <div key={`${file.path}:${index}`}>
        <div
          role={clickable ? 'button' : undefined}
          tabIndex={clickable ? 0 : undefined}
          onClick={() => clickable && openDraft(file.path, number!, side)}
          onKeyDown={(e) => { if (clickable && e.key === 'Enter') openDraft(file.path, number!, side); }}
          title={clickable ? t('code.review.anchorHint') : undefined}
          className={cn(
            'flex gap-2 px-1 font-mono text-[10.5px] leading-tight',
            lineStyle(line.kind),
            clickable && 'cursor-pointer hover:bg-accent/40',
            numbered && number !== undefined && byAnchor.has(commentKey({ path: file.path, line: number, side })) && 'ring-1 ring-inset ring-primary/30',
          )}
        >
          <span className="w-8 shrink-0 select-none text-right text-muted-foreground/40">
            {numbered && number !== undefined ? number : ''}
          </span>
          <span className="shrink-0 select-none text-muted-foreground/50">
            {line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ' '}
          </span>
          <span className="min-w-0 whitespace-pre-wrap break-all">
            {line.kind === 'add' || line.kind === 'del' || line.kind === 'context' ? (
              <Highlighted text={line.text || ' '} language={languageFor(file.path)} />
            ) : line.text}
          </span>
        </div>
        {numbered && number !== undefined && attachedNote(file.path, number, side)}
        {numbered && number !== undefined && noteEditor(file.path, number, side)}
      </div>
    );
  }

  function splitCell(file: DiffFile, line: DiffLine | undefined, side: 'new' | 'old') {
    if (!line) {
      return <div className="min-h-[1.1rem] flex-1 bg-muted/10" />;
    }
    const number = side === 'old' ? line.oldLine : line.newLine;
    const numbered = (line.kind === 'add' || line.kind === 'del' || line.kind === 'context') && number !== undefined;
    const clickable = numbered && canComment;
    return (
      <div
        role={clickable ? 'button' : undefined}
        tabIndex={clickable ? 0 : undefined}
        onClick={() => clickable && openDraft(file.path, number!, side)}
        onKeyDown={(e) => { if (clickable && e.key === 'Enter') openDraft(file.path, number!, side); }}
        title={clickable ? t('code.review.anchorHint') : undefined}
        className={cn(
          'flex min-w-0 flex-1 gap-1.5 px-1 font-mono text-[10.5px] leading-tight',
          line.kind === 'del' ? 'bg-destructive/10 text-destructive/85' : line.kind === 'add' ? 'bg-primary/10 text-primary/90' : '',
          clickable && 'cursor-pointer hover:bg-accent/40',
          numbered && byAnchor.has(commentKey({ path: file.path, line: number!, side })) && 'ring-1 ring-inset ring-primary/30',
        )}
      >
        <span className="w-7 shrink-0 select-none text-right text-muted-foreground/40">
          {numbered ? number : ''}
        </span>
        <span className="min-w-0 whitespace-pre-wrap break-all">
          <Highlighted text={line.text || ' '} language={languageFor(file.path)} />
        </span>
      </div>
      );
  }

  function splitRow(file: DiffFile, row: SplitRow, index: number) {
    const anchors: Array<{ line?: DiffLine; side: 'new' | 'old' }> = [];
    if (row.left) anchors.push({ line: row.left, side: 'old' });
    if (row.right) anchors.push({ line: row.right, side: 'new' });
    return (
      <div key={`${file.path}:s:${index}`}>
        <div className="flex divide-x divide-border/30">
          {splitCell(file, row.left, 'old')}
          {splitCell(file, row.right, 'new')}
        </div>
        {anchors.map(({ line, side }) => {
          const number = side === 'old' ? line?.oldLine : line?.newLine;
          if (!line || number === undefined) return null;
          return (
            <div key={`${side}:${number}`}>
              {attachedNote(file.path, number, side)}
              {noteEditor(file.path, number, side)}
            </div>
          );
        })}
      </div>
    );
  }

  function imageCard(file: DiffFile) {
    return <ImageChange file={file} loadImage={loadImage} />;
  }

  function fileBody(file: DiffFile) {
    if (file.image) return imageCard(file);
    if (file.lines.length === 0) {
      return <div className="px-1 py-1 text-[10.5px] text-muted-foreground/60">{t('code.diff.noText')}</div>;
    }
    if (splitMode === 'split') {
      return (
        <div className="overflow-x-auto">
          {pairSplitRows(file.lines).map((row, index) => splitRow(file, row, index))}
        </div>
      );
    }
    return <div>{file.lines.map((line, index) => unifiedLine(file, line, index))}</div>;
  }

  return (
    <div className="glass-card-subtle rounded-xl border border-border/40 p-3 text-[12px]">
      <ul className="space-y-0.5">
        {files.map((name) => (
          <li key={name} className="text-muted-foreground">{t('code.task.changeEditedFile', { name })}</li>
        ))}
      </ul>
      {comments.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-[10.5px] text-muted-foreground">
          {comments.map((comment) => (
            <li key={commentKey(comment)} className="truncate">
              {anchorLabel(comment)} — {comment.text}
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        onClick={() => setShowDetails((v) => !v)}
        className="mt-2 flex items-center gap-1 text-[11px] text-muted-foreground/60"
      >
        <ChevronRight size={11} className={cn('transition-transform', showDetails && 'rotate-90')} />
        {t('code.proof.details')}
      </button>
      {showDetails && (
        <div className="mt-1 space-y-1.5">
          {onModeChange && (
            <div className="flex flex-wrap items-center gap-1 text-[10px]">
              <span className="text-muted-foreground/55">{t('code.diff.source')}</span>
              {(['working', 'committed', 'turn', 'revision'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => onModeChange(value)}
                  className={cn(
                    'rounded border px-1.5 py-0.5',
                    mode === value
                      ? 'border-primary/50 text-foreground'
                      : 'border-border/40 text-muted-foreground hover:bg-accent/30',
                  )}
                >
                  {value === 'working' ? t('code.diff.sourceWorking')
                    : value === 'committed' ? t('code.diff.sourceCommitted')
                    : value === 'turn' ? t('code.diff.sourceTurn')
                    : t('code.diff.sourceRevision')}
                </button>
              ))}
              {mode === 'revision' && onRevisionChange && (
                <span className="flex items-center gap-1">
                  <input
                    value={draftRevision}
                    onChange={(e) => setDraftRevision(e.target.value)}
                    placeholder={t('code.diff.revisionPlaceholder')}
                    className="w-32 rounded border border-border/40 bg-background/40 px-1 py-0.5 font-mono text-[10px] text-foreground"
                  />
                  <button
                    type="button"
                    disabled={!draftRevision.trim()}
                    onClick={() => onRevisionChange(draftRevision.trim())}
                    className="rounded border border-border/40 px-1.5 py-0.5 text-foreground disabled:opacity-50"
                  >
                    {t('code.diff.apply')}
                  </button>
                </span>
              )}
            </div>
          )}
          {note === 'NO_TURN_SNAPSHOT' ? (
            <div className="text-[10.5px] text-muted-foreground/60">{t('code.diff.noTurnSnapshot')}</div>
          ) : note ? (
            <div className="text-[10.5px] text-muted-foreground/60">{note}</div>
          ) : null}
          <div className="grid gap-2 sm:grid-cols-[minmax(120px,190px)_1fr]">
          <DiffFileTree files={allFiles} selected={selectedPath} onSelect={setSelectedPath} workspaceRoot={workspaceRoot} />
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[10.5px] text-muted-foreground/70">
                {t('code.diff.summary', { files: allFiles.length, add: totalAdd, del: totalDel })}
              </span>
              <div className="flex gap-1">
                {(['unified', 'split'] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setSplitMode(value)}
                    className={cn(
                      'rounded border px-1.5 py-0.5 text-[10px]',
                      splitMode === value
                        ? 'border-primary/50 text-foreground'
                        : 'border-border/40 text-muted-foreground hover:bg-accent/30',
                    )}
                  >
                    {value === 'unified' ? t('code.diff.unified') : t('code.diff.split')}
                  </button>
                ))}
              </div>
            </div>
            {visible.length > 0 ? visible.map((file) => (
              <details key={file.path} className="rounded-lg bg-background/40" open={selectedPath === file.path || comments.some((c) => c.path === file.path)}>
                <summary className="cursor-pointer px-2 py-1 font-mono text-[10.5px] text-muted-foreground/80">
                  {file.path}
                  <span className="ml-1.5 text-[9.5px]">
                    {file.additions > 0 && <span className="text-primary">+{file.additions}</span>}
                    {file.deletions > 0 && <span className="ml-0.5 text-destructive">-{file.deletions}</span>}
                  </span>
                </summary>
                <div className="max-h-72 overflow-auto px-1 pb-2">{fileBody(file)}</div>
              </details>
            )) : sections.length === 0 ? (
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-background/40 p-2 font-mono text-[10.5px] text-muted-foreground/70">
                {unified || '—'}
              </pre>
            ) : null}
            {digest ? (
              <div className="font-mono text-[10px] text-muted-foreground/50">{digest}</div>
            ) : null}
          </div>
          </div>
        </div>
      )}
    </div>
  );
}
