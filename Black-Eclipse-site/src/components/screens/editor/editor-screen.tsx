'use client';

// ── Editor — file tree + text editor over the selected workspace folder ──────
// The client side of the folder picker + editor. It talks ONLY to the client-safe
// code-browser (which calls the Next.js /api/workspace routes); it holds no
// filesystem logic. Reading, writing and listing are all confined to the selected
// folder by the runtime — the same confinement the agent tools use — so this is a
// second door to the SAME boundary, never a weaker one.
//
// The tree behaves like an editor explorer: click a file to open it, click a
// folder to expand it in place, use the "+" to create a file or folder (inline,
// named with Enter), and hover a row to delete it. No dependency is bundled; this
// is the working core.

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronRight, FilePlus, FolderPlus, MessageSquare, Plus, Trash2, X } from '@/lib/icons';
import { useT } from '@/lib/i18n/use-t';
import { cn } from '@/lib/utils';
import { ATTACH_PROJECT_FILE_EVENT } from '@/lib/chat/file-sources';
import { captureEditorContext, clearEditorContext, setEditorContext } from '@/lib/chat/editor-context';
import {
  deleteWorkspacePath,
  listTrustedRoots,
  listWorkspaceFiles,
  makeWorkspaceFolder,
  readWorkspaceFile,
  writeWorkspaceFile,
  type EditorEntry,
  type TrustedRoot,
} from '@/lib/harness/clients/code-browser';
import { FolderPicker } from '@/components/workspace/folder-picker';

export function EditorScreen() {
  const t = useT();
  const [roots, setRoots] = useState<TrustedRoot[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  const refreshRoots = useCallback(async () => {
    try {
      setRoots(await listTrustedRoots());
      setError(null);
    } catch {
      setError(t('editor.listFailed'));
    }
  }, [t]);

  useEffect(() => { void refreshRoots(); }, [refreshRoots]);

  const selected = roots.find((r) => r.selected) ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-4">
      <header className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-[15px] font-medium text-foreground">{t('editor.heading')}</h2>
          {selected && (
            <p className="truncate text-[11px] text-muted-foreground" title={selected.path}>
              {t('editor.currentFolder')}: <span className="text-foreground/90">{selected.path}</span>
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => setPicking(true)}
          className="glass-button shrink-0 rounded-lg px-3 py-1.5 text-[12px] text-foreground"
        >
          {t('editor.openFolder')}
        </button>
      </header>

      {error && (
        <div className="glass-card-subtle rounded-xl border border-red-500/30 p-2.5 text-[12px] text-red-300">
          {error}
        </div>
      )}

      {!selected ? (
        <div className="glass-card-subtle rounded-xl p-4 text-[13px] text-muted-foreground">
          {t('editor.noFolder')}
        </div>
      ) : (
        <FileWorkspace key={selected.id} root={selected} />
      )}

      {picking && (
        <FolderPicker
          onClose={() => setPicking(false)}
          onApproved={() => { setPicking(false); void refreshRoots(); }}
          selectGlobally
        />
      )}
    </div>
  );
}

// ── The tree + editor ────────────────────────────────────────────────────────

interface TreeState {
  /** Directory rel path → its loaded children. */
  children: Record<string, EditorEntry[]>;
  expanded: Set<string>;
}

function joinPath(dir: string, name: string): string {
  return dir === '.' || dir === '' ? name : `${dir}/${name}`;
}

function FileWorkspace({ root }: { root: TrustedRoot }) {
  const t = useT();
  const [tree, setTree] = useState<TreeState>({ children: {}, expanded: new Set(['.']) });
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [original, setOriginal] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Inline creation: { dir, kind } — a name input appears under that folder.
  const [creating, setCreating] = useState<{ dir: string; kind: 'file' | 'folder' } | null>(null);
  const [draftName, setDraftName] = useState('');
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Gap #17: publish the file + selection the user is looking at, bounded. The
  // composer attaches a fresh snapshot as reference context; the store is shared
  // within the window, so no prop threading is needed between the two surfaces.
  const publishSelection = useCallback((el: HTMLTextAreaElement, value: string) => {
    if (!openFile) {
      clearEditorContext();
      return;
    }
    setEditorContext(captureEditorContext({
      path: openFile,
      content: value,
      selectionStart: el.selectionStart ?? 0,
      selectionEnd: el.selectionEnd ?? 0,
      rootId: root.id,
      rootLabel: root.label ?? null,
    }));
  }, [openFile, root.id, root.label]);

  useEffect(() => () => clearEditorContext(), []);

  const loadDir = useCallback(async (dir: string): Promise<EditorEntry[]> => {
    const result = await listWorkspaceFiles(dir);
    setTree((prev) => ({ ...prev, children: { ...prev.children, [dir]: result.entries } }));
    return result.entries;
  }, []);

  useEffect(() => { void loadDir('.').catch(() => setStatus(t('editor.listFailed'))); }, [loadDir, root.id, t]);

  useEffect(() => { if (creating) inputRef.current?.focus(); }, [creating]);

  const toggle = useCallback(async (entry: EditorEntry) => {
    if (!entry.isDir) { void open(entry.path); return; }
    setTree((prev) => {
      const expanded = new Set(prev.expanded);
      if (expanded.has(entry.path)) {
        expanded.delete(entry.path);
      } else {
        expanded.add(entry.path);
      }
      return { ...prev, expanded };
    });
    if (!tree.children[entry.path]) {
      try { await loadDir(entry.path); } catch { setStatus(t('editor.listFailed')); }
    }
  }, [tree.children, loadDir, t]);

  const open = useCallback(async (file: string) => {
    try {
      const result = await readWorkspaceFile(file);
      setOpenFile(result.path);
      setContent(result.content);
      setOriginal(result.content);
      setStatus(result.redacted ? t('editor.redacted') : null);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : t('editor.readFailed'));
    }
  }, [t]);

  const save = useCallback(async () => {
    if (!openFile) return;
    setBusy(true);
    try {
      await writeWorkspaceFile(openFile, content);
      setOriginal(content);
      setStatus(t('editor.saved'));
    } catch (err) {
      setStatus(err instanceof Error ? err.message : t('editor.saveFailed'));
    } finally {
      setBusy(false);
    }
  }, [openFile, content, t]);

  const beginCreate = useCallback((dir: string, kind: 'file' | 'folder') => {
    setCreating({ dir, kind });
    setDraftName(kind === 'file' ? t('editor.newFileName') : t('editor.newFolderName'));
  }, [t]);

  const commitCreate = useCallback(async () => {
    if (!creating) return;
    const name = draftName.trim();
    if (!name) { setCreating(null); return; }
    const target = joinPath(creating.dir, name);
    try {
      if (creating.kind === 'folder') {
        await makeWorkspaceFolder(target);
      } else {
        await writeWorkspaceFile(target, '');
        await open(target);
      }
      await loadDir(creating.dir);
      setCreating(null);
      setStatus(null);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : t('editor.createFailed'));
    }
  }, [creating, draftName, loadDir, open, t]);

  const remove = useCallback(async (entry: EditorEntry) => {
    try {
      await deleteWorkspacePath(entry.path);
      const parent = entry.path.includes('/') ? entry.path.split('/').slice(0, -1).join('/') : '.';
      await loadDir(parent);
      if (openFile === entry.path) { setOpenFile(null); setContent(''); setOriginal(''); }
    } catch (err) {
      setStatus(err instanceof Error ? err.message : t('editor.deleteFailed'));
    }
  }, [loadDir, openFile, t]);

  const dirty = content !== original;

  // Render a directory's children recursively (folders expand in place).
  const renderDir = (dir: string, depth: number) => {
    const entries = tree.children[dir] ?? [];
    return (
      <>
        {entries.map((entry) => {
          const isOpenFolder = entry.isDir && tree.expanded.has(entry.path);
          const isActive = !entry.isDir && openFile === entry.path;
          return (
            <div key={entry.path}>
              <div
                className={cn(
                  'group flex items-center gap-1 rounded pr-1 text-[12px] transition-colors',
                  isActive ? 'bg-primary/20 text-foreground' : 'text-foreground/80 hover:bg-white/5',
                )}
                style={{ paddingLeft: 4 + depth * 12 }}
              >
                <button
                  type="button"
                  onClick={() => void toggle(entry)}
                  className="flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left"
                >
                  {entry.isDir ? (
                    <ChevronRight size={12} className={cn('shrink-0 text-muted-foreground/60 transition-transform', isOpenFolder && 'rotate-90')} />
                  ) : (
                    <span className="w-3 shrink-0" />
                  )}
                  <span className="truncate">{entry.name}</span>
                </button>
                {!entry.isDir && (
                  <button
                    type="button"
                    title="Attach this file to the active chat"
                    onClick={() => window.dispatchEvent(new CustomEvent(ATTACH_PROJECT_FILE_EVENT, {
                      detail: { path: entry.path, rootId: root.id, rootLabel: root.label ?? null },
                    }))}
                    className="shrink-0 rounded p-0.5 text-muted-foreground/0 transition-colors hover:text-primary group-hover:text-muted-foreground/60"
                  >
                    <MessageSquare size={12} />
                  </button>
                )}
                <button
                  type="button"
                  title={t('editor.delete')}
                  onClick={() => void remove(entry)}
                  className="shrink-0 rounded p-0.5 text-muted-foreground/0 transition-colors hover:text-rose-300 group-hover:text-muted-foreground/60"
                >
                  <Trash2 size={12} />
                </button>
              </div>
              {entry.isDir && (
                <>
                  {isOpenFolder && renderDir(entry.path, depth + 1)}
                  {creating?.dir === entry.path && (
                    <NameInput
                      inputRef={inputRef}
                      value={draftName}
                      onChange={setDraftName}
                      depth={depth + 1}
                      onCommit={() => void commitCreate()}
                      onCancel={() => setCreating(null)}
                    />
                  )}
                </>
              )}
            </div>
          );
        })}
      </>
    );
  };

  return (
    <div className="grid min-h-0 flex-1 gap-3 lg:grid-cols-[280px_1fr]">
      <div className="glass-card flex min-h-0 flex-col overflow-hidden">
        <div className="flex items-center justify-between gap-1 border-b border-white/5 px-2.5 py-1.5">
          <span className="text-[11px] font-medium text-muted-foreground">{t('editor.files')}</span>
          <div className="flex shrink-0 items-center gap-0.5">
            <button
              type="button"
              title={t('editor.newFile')}
              onClick={() => beginCreate('.', 'file')}
              className="rounded p-1 text-muted-foreground hover:bg-white/5 hover:text-foreground"
            >
              <FilePlus size={13} />
            </button>
            <button
              type="button"
              title={t('editor.newFolder')}
              onClick={() => beginCreate('.', 'folder')}
              className="rounded p-1 text-muted-foreground hover:bg-white/5 hover:text-foreground"
            >
              <FolderPlus size={13} />
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-1">
          {creating?.dir === '.' && (
            <NameInput
              inputRef={inputRef}
              value={draftName}
              onChange={setDraftName}
              depth={0}
              onCommit={() => void commitCreate()}
              onCancel={() => setCreating(null)}
            />
          )}
          {renderDir('.', 0)}
          {(tree.children['.'] ?? []).length === 0 && !creating && (
            <p className="p-2 text-[11px] text-muted-foreground">{t('editor.emptyFolder')}</p>
          )}
        </div>
      </div>

      <div className="glass-card flex min-h-0 flex-col overflow-hidden">
        {openFile ? (
          <>
            <div className="flex items-center justify-between gap-2 border-b border-white/5 px-2.5 py-1.5">
              <span className="truncate text-[11px] text-foreground/90">{openFile}{dirty ? ' •' : ''}</span>
              <div className="flex shrink-0 gap-1.5">
                {dirty && (
                  <button
                    type="button"
                    onClick={() => setContent(original)}
                    className="rounded px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    {t('editor.revert')}
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy || !dirty}
                  onClick={() => void save()}
                  className="glass-button rounded-lg px-2.5 py-1 text-[11px] text-foreground disabled:opacity-40"
                >
                  {busy ? t('editor.saving') : t('editor.save')}
                </button>
              </div>
            </div>
            <textarea
              value={content}
              onChange={(e) => {
                setContent(e.target.value);
                publishSelection(e.currentTarget, e.target.value);
              }}
              onSelect={(e) => publishSelection(e.currentTarget, content)}
              onFocus={(e) => publishSelection(e.currentTarget, content)}
              spellCheck={false}
              className="min-h-0 flex-1 resize-none bg-transparent p-3 font-mono text-[12px] leading-5 text-foreground outline-none"
            />
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center p-4 text-[12px] text-muted-foreground">
            {t('editor.selectFile')}
          </div>
        )}
        {status && (
          <div className="border-t border-white/5 px-2.5 py-1 text-[11px] text-muted-foreground">{status}</div>
        )}
      </div>
    </div>
  );
}

function NameInput({
  inputRef, value, onChange, depth, onCommit, onCancel,
}: {
  inputRef: React.RefObject<HTMLInputElement | null>;
  value: string;
  onChange: (v: string) => void;
  depth: number;
  onCommit: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="flex items-center gap-1 py-0.5 pr-1" style={{ paddingLeft: 4 + depth * 12 }}>
      <Plus size={11} className="shrink-0 text-primary/70" />
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); onCommit(); }
          if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
        }}
        onBlur={onCommit}
        className="min-w-0 flex-1 rounded border border-primary/40 bg-background/60 px-1 py-0.5 font-mono text-[11px] text-foreground outline-none"
      />
      <button type="button" onClick={onCancel} className="shrink-0 rounded p-0.5 text-muted-foreground/60 hover:text-foreground">
        <X size={11} />
      </button>
    </div>
  );
}

export default EditorScreen;
