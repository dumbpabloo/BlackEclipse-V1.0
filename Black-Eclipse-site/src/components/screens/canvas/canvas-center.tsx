'use client';

// ─── Canvas center region ─────────────────────────────────────────────────────
// Title with inline rename, kind badge, code-language selector, the save-status
// chip and the toolbar (undo/redo, versions, export, full-width). Hosts the
// editor selected by artifact kind.

import { useEffect, useRef, useState, type MutableRefObject } from 'react';
import {
  AlertTriangle, Check, Download, FileOutput, History, Loader2,
  Maximize2, Minimize2, MoreHorizontal, Redo2, Save, Trash2, Undo2, Upload, X,
} from '@/lib/icons';
import type { CanvasSnapshot } from '@/lib/harness/clients/canvas-client';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { CanvasEditor } from './canvas-editors';
import {
  CODE_LANGUAGES, humanTime, monacoLanguage,
  type CanvasEditorHandle, type CanvasSaveState, type CanvasSelection,
} from './canvas-utils';

export interface CanvasCenterProps {
  snapshot: CanvasSnapshot;
  content: string;
  dirty: boolean;
  saveState: CanvasSaveState;
  saveError: string | null;
  onContentChange: (next: string) => void;
  onSelectionChange: (selection: CanvasSelection | null) => void;
  editorHandleRef: MutableRefObject<CanvasEditorHandle | null>;
  onRename: (name: string) => void;
  onLanguageChange: (language: string) => void;
  onRestore: (version: number) => void;
  onExport: (action: 'download' | 'copy' | 'print' | 'pptx' | 'pdf' | 'png') => void;
  onSaveNow: () => void;
  onDelete: () => void;
  fullWidth: boolean;
  onToggleFullWidth: () => void;
  editable: boolean;
  /** A transient result message (e.g. the exported PowerPoint path). */
  notice?: string | null;
  /** Inline in the chat dock: no inner card chrome, close button in the header. */
  inline?: boolean;
  onClose?: () => void;
}

function SaveChip({ state, dirty, error }: { state: CanvasSaveState; dirty: boolean; error: string | null }) {
  let label = 'Saved';
  let tone = 'text-emerald-400';
  let icon = <Check className="h-3.5 w-3.5" />;
  if (state === 'saving') { label = 'Saving…'; tone = 'text-muted-foreground'; icon = <Loader2 className="h-3.5 w-3.5 animate-spin" />; }
  else if (state === 'error') { label = 'Save failed'; tone = 'text-rose-400'; icon = <AlertTriangle className="h-3.5 w-3.5" />; }
  else if (state === 'conflict') { label = 'Conflict'; tone = 'text-amber-400'; icon = <AlertTriangle className="h-3.5 w-3.5" />; }
  else if (dirty) { label = 'Unsaved changes'; tone = 'text-amber-400'; icon = <AlertTriangle className="h-3.5 w-3.5" />; }
  else if (state === 'idle') { label = 'Up to date'; tone = 'text-muted-foreground'; }
  return (
    <span className={cn('flex items-center gap-1.5 text-[11px]', tone)} title={error ?? label}>
      {icon}
      {label}
    </span>
  );
}

const LANGUAGES = Array.from(new Set(CODE_LANGUAGES));

export function CanvasCenter(props: CanvasCenterProps) {
  const {
    snapshot, content, dirty, saveState, saveError, onContentChange, onSelectionChange,
    editorHandleRef, onRename, onLanguageChange, onRestore, onExport, onSaveNow, onDelete,
    fullWidth, onToggleFullWidth, editable, notice = null, inline = false, onClose,
  } = props;

  const displayName = snapshot.name.replace(/\.(deck\.json|json|md|markdown)$/i, '') || 'Canvas';
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(displayName);
  const titleRef = useRef<HTMLInputElement>(null);

  useEffect(() => { setTitleDraft(displayName); }, [displayName]);
  useEffect(() => { if (editingTitle) titleRef.current?.select(); }, [editingTitle]);

  const commitTitle = () => {
    const next = titleDraft.trim();
    setEditingTitle(false);
    if (next && next !== displayName) onRename(next);
    else setTitleDraft(displayName);
  };

  return (
    <section
      className={cn(
        'flex min-h-0 min-w-0 flex-1 flex-col gap-2',
        inline ? '' : 'rounded-2xl glass-card p-4',
      )}
      aria-label="Canvas editor"
    >
      <header className="flex min-w-0 items-center gap-1.5">
        {editingTitle ? (
          <input
            ref={titleRef}
            value={titleDraft}
            aria-label="Artifact name"
            onChange={(e) => setTitleDraft(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitTitle();
              if (e.key === 'Escape') { setTitleDraft(displayName); setEditingTitle(false); }
            }}
            className="min-w-0 flex-1 rounded-md border border-border/40 bg-black/20 px-2 py-1 text-[13px] font-semibold text-foreground outline-none focus:border-primary"
          />
        ) : (
          <button
            type="button"
            onClick={() => editable && setEditingTitle(true)}
            disabled={!editable}
            title={editable ? 'Rename' : displayName}
            className="min-w-0 flex-1 truncate rounded-md px-1 py-0.5 text-left text-[13px] font-semibold text-foreground hover:bg-accent disabled:cursor-default"
          >
            {displayName}
          </button>
        )}
        {snapshot.kind === 'code' ? (
          <select
            aria-label="Language"
            value={monacoLanguage(snapshot.language)}
            onChange={(e) => onLanguageChange(e.target.value)}
            className="rounded-md border border-border/40 bg-transparent px-1.5 py-0.5 text-[11px] text-muted-foreground outline-none"
          >
            {LANGUAGES.map((lang) => <option key={lang} value={lang}>{lang}</option>)}
          </select>
        ) : null}
        <SaveChip state={saveState} dirty={dirty} error={saveError} />
        {notice ? (
          <span className="max-w-[220px] truncate text-[10px] text-emerald-400" title={notice}>{notice}</span>
        ) : null}
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" aria-label="More canvas actions" />}>
            <MoreHorizontal className="h-4 w-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-52">
            <DropdownMenuItem onClick={onSaveNow} disabled={!editable}>
              <Save className="h-4 w-4" /> Save now
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => editorHandleRef.current?.undo?.()} disabled={!editable}>
              <Undo2 className="h-4 w-4" /> Undo
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => editorHandleRef.current?.redo?.()} disabled={!editable}>
              <Redo2 className="h-4 w-4" /> Redo
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <History className="h-4 w-4" /> Versions
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="w-[280px]">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>Version history</DropdownMenuLabel>
                  {snapshot.versions.length === 0 ? (
                    <div className="px-1.5 py-1 text-xs text-muted-foreground/60">No history yet.</div>
                  ) : (
                    snapshot.versions.map((v) => (
                      <DropdownMenuItem
                        key={v.versionNumber}
                        onClick={() => onRestore(v.versionNumber)}
                        disabled={v.versionNumber === snapshot.version}
                      >
                        <span className="flex-1">
                          <span className="block text-xs text-foreground">v{v.versionNumber}{v.versionNumber === snapshot.version ? ' · current' : ''}</span>
                          <span className="block truncate text-[10px] text-muted-foreground/60">
                            {v.changeNote ?? 'No note'} · {humanTime(v.createdAt)}
                          </span>
                        </span>
                      </DropdownMenuItem>
                    ))
                  )}
                </DropdownMenuGroup>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <Download className="h-4 w-4" /> Export
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="w-[200px]">
                {snapshot.kind === 'presentation' ? (
                  <>
                    <DropdownMenuItem onClick={() => onExport('pptx')}>
                      <FileOutput className="h-3.5 w-3.5" /> Export PowerPoint (.pptx)
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => onExport('pdf')}>
                      <FileOutput className="h-3.5 w-3.5" /> Export PDF
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => onExport('png')}>
                      <FileOutput className="h-3.5 w-3.5" /> Export PNG slides (.zip)
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                  </>
                ) : null}
                <DropdownMenuItem onClick={() => onExport('download')}>
                  <Download className="h-3.5 w-3.5" /> Download file
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => onExport('copy')}>
                  <Upload className="h-3.5 w-3.5" /> Copy to clipboard
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => onExport('print')}>
                  <FileOutput className="h-3.5 w-3.5" /> Print / PDF
                </DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            {!inline ? (
              <DropdownMenuItem onClick={onToggleFullWidth}>
                {fullWidth ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
                {fullWidth ? 'Show AI panel' : 'Full-width editor'}
              </DropdownMenuItem>
            ) : null}
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={onDelete} disabled={!editable}>
              <Trash2 className="h-4 w-4" /> Delete artifact
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {inline && onClose ? (
          <Button variant="ghost" size="icon-sm" aria-label="Close Canvas" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        ) : null}
      </header>

      <CanvasEditor
        kind={snapshot.kind}
        content={content}
        language={snapshot.language}
        editable={editable}
        docKey={`${snapshot.artifactId}:${snapshot.version}`}
        onChange={onContentChange}
        onSelectionChange={onSelectionChange}
        handleRef={editorHandleRef}
      />
    </section>
  );
}
