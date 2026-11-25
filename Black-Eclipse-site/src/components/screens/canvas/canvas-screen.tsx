'use client';

// ─── Eclipse Canvas (container) ───────────────────────────────────────────────
// The data layer: list/create/load/save/restore/propose against the real canvas
// client. All rendering is delegated to <CanvasWorkspace>. Nothing fakes a
// success: a save is only reported saved after the server returns it, and a 409
// surfaces an honest conflict UI instead of overwriting.

import { useCallback, useEffect, useRef, useState, type JSX, type MutableRefObject } from 'react';
import { saveBlob } from '@/lib/desktop/download';
import { confirmDialog, promptDialog } from '@/lib/ui/confirm-dialog';
import { archiveArtifact, updateArtifact } from '@/lib/harness/clients/artifacts-browser';
import { exportPresentation } from '@/lib/harness/clients/presentations-client';
import {
  createCanvasArtifact, getCanvasArtifact, listCanvasArtifacts,
  proposeCanvasEdit, restoreCanvasVersion, saveCanvasArtifact,
  type CanvasKind, type CanvasListItem, type CanvasMode, type CanvasProposal, type CanvasSnapshot,
} from '@/lib/harness/clients/canvas-client';
import { CanvasWorkspace } from './canvas-workspace';
import {
  contentExtension, contentMime, describeCanvasError, kindLabel, proposalOperations, starterContent,
  type CanvasEditorHandle, type CanvasErrorState, type CanvasSaveState, type CanvasSelection,
} from './canvas-utils';

const CANVAS_LANGUAGE_BY_EXT: Record<string, string> = {
  '.py': 'python', '.ts': 'typescript', '.tsx': 'tsx', '.js': 'javascript', '.jsx': 'jsx',
  '.sql': 'sql', '.json': 'json', '.html': 'html', '.css': 'css', '.sh': 'bash',
  '.yml': 'yaml', '.yaml': 'yaml', '.go': 'go', '.rs': 'rust', '.java': 'java',
  '.c': 'c', '.cpp': 'cpp', '.txt': 'text',
};

function toListItem(snap: CanvasSnapshot): CanvasListItem {
  return {
    artifactId: snap.artifactId, name: snap.name, kind: snap.kind, language: snap.language,
    description: snap.description, version: snap.version, sizeBytes: snap.sizeBytes,
    updatedAt: snap.updatedAt, conversationId: snap.conversationId,
  };
}

function safeFilename(name: string, ext: string): string {
  const base = name.replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '') || 'canvas';
  return `${base}.${ext}`;
}

export function CanvasScreen({
  initialArtifactId,
  isOperator = true,
  inline = false,
  onClose,
}: {
  initialArtifactId?: string;
  isOperator?: boolean;
  /** Inline-in-chat mode: no rail, compact card with a close button. */
  inline?: boolean;
  onClose?: () => void;
}): JSX.Element {
  const editable = isOperator;

  const [items, setItems] = useState<CanvasListItem[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [query, setQuery] = useState('');

  const [selectedId, setSelectedId] = useState<string | null>(initialArtifactId ?? null);
  const [snapshot, setSnapshot] = useState<CanvasSnapshot | null>(null);
  const [content, setContent] = useState('');
  const [savedContent, setSavedContent] = useState('');
  const [loadingArtifact, setLoadingArtifact] = useState(false);
  const [artifactError, setArtifactError] = useState<CanvasErrorState | null>(null);

  const [saveState, setSaveState] = useState<CanvasSaveState>('idle');
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflictVersion, setConflictVersion] = useState<number | null>(null);

  const [selection, setSelection] = useState<CanvasSelection | null>(null);
  const [useSelection, setUseSelection] = useState(false);

  const [mode, setMode] = useState<CanvasMode>('ask');
  const [instruction, setInstruction] = useState('');
  const [proposing, setProposing] = useState(false);
  const [proposal, setProposal] = useState<CanvasProposal | null>(null);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);

  const [railCollapsed, setRailCollapsed] = useState(false);
  const [aiCollapsed, setAiCollapsed] = useState(inline);
  const [fullWidth, setFullWidth] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const editorHandleRef: MutableRefObject<CanvasEditorHandle | null> = useRef(null);
  const savingRef = useRef(false);
  const snapshotRef = useRef<CanvasSnapshot | null>(null);
  const contentRef = useRef('');
  const proposalRef = useRef<CanvasProposal | null>(null);
  snapshotRef.current = snapshot;
  contentRef.current = content;
  proposalRef.current = proposal;

  const dirty = snapshot !== null && content !== savedContent;

  // ── List ────────────────────────────────────────────────────────────────────
  const loadList = useCallback(async (q: string) => {
    setListLoading(true);
    setListError(null);
    try {
      setItems(await listCanvasArtifacts({ q: q || undefined, limit: 50 }));
    } catch (err) {
      setListError(describeCanvasError(err).message);
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    const id = window.setTimeout(() => { void loadList(query.trim()); }, 300);
    return () => window.clearTimeout(id);
  }, [query, loadList]);

  const patchListItem = useCallback((snap: CanvasSnapshot) => {
    setItems((prev) => {
      const next = toListItem(snap);
      return prev.some((i) => i.artifactId === snap.artifactId)
        ? prev.map((i) => (i.artifactId === snap.artifactId ? next : i))
        : [next, ...prev];
    });
  }, []);

  // ── Load ────────────────────────────────────────────────────────────────────
  const loadArtifact = useCallback(async (id: string) => {
    setSelectedId(id);
    setLoadingArtifact(true);
    setArtifactError(null);
    setSnapshot(null);
    setProposal(null);
    setProposalError(null);
    setSaveState('idle');
    setSaveError(null);
    setConflictVersion(null);
    setSelection(null);
    setUseSelection(false);
    setNotice(null);
    try {
      const snap = await getCanvasArtifact(id);
      setSnapshot(snap);
      setContent(snap.content);
      setSavedContent(snap.content);
    } catch (err) {
      setArtifactError(describeCanvasError(err));
    } finally {
      setLoadingArtifact(false);
    }
  }, []);

  useEffect(() => {
    if (initialArtifactId) void loadArtifact(initialArtifactId);
  }, [initialArtifactId, loadArtifact]);

  // ── Create / rename / archive ───────────────────────────────────────────────
  const createArtifact = useCallback(async (kind: CanvasKind) => {
    const fallback = kind === 'code' ? 'Untitled code'
      : kind === 'visual' ? 'Untitled visual report'
      : kind === 'presentation' ? 'Untitled deck'
      : 'Untitled document';
    const name = await promptDialog({ title: `Name for new ${kindLabel(kind)}`, defaultValue: fallback });
    if (!name || !name.trim()) return;
    setListError(null);
    try {
      const snap = await createCanvasArtifact({
        name: name.trim(), kind,
        language: kind === 'code' ? 'typescript' : undefined,
        content: starterContent(kind),
      });
      patchListItem(snap);
      setSelectedId(snap.artifactId);
      setSnapshot(snap);
      setContent(snap.content);
      setSavedContent(snap.content);
      setSaveState('idle');
      setArtifactError(null);
      setProposal(null);
    } catch (err) {
      setListError(describeCanvasError(err).message);
    }
  }, [patchListItem]);

  // Import an existing text file as a new canvas artifact (client-side read only;
  // the bytes go through the same create path and artifact store).
  const importArtifact = useCallback(async (file: File) => {
    setListError(null);
    if (file.size > 2 * 1024 * 1024) {
      setListError('That file is larger than the 2 MB canvas limit.');
      return;
    }
    const lower = file.name.toLowerCase();
    const kind: CanvasKind = lower.endsWith('.json')
      ? 'visual'
      : /\.(md|markdown)$/.test(lower)
        ? 'document'
        : 'code';
    const ext = lower.slice(lower.lastIndexOf('.'));
    const language = kind === 'code' ? (CANVAS_LANGUAGE_BY_EXT[ext] ?? 'text') : undefined;
    try {
      const text = await file.text();
      const snap = await createCanvasArtifact({ name: file.name, kind, language, content: text });
      patchListItem(snap);
      setSelectedId(snap.artifactId);
      setSnapshot(snap);
      setContent(snap.content);
      setSavedContent(snap.content);
      setSaveState('idle');
      setArtifactError(null);
      setProposal(null);
    } catch (err) {
      setListError(describeCanvasError(err).message);
    }
  }, [patchListItem]);

  const renameArtifact = useCallback(async (name: string) => {    const snap = snapshotRef.current;
    if (!snap) return;
    try {
      await updateArtifact(snap.artifactId, { name });
      setSnapshot((prev) => (prev ? { ...prev, name } : prev));
      setItems((prev) => prev.map((i) => (i.artifactId === snap.artifactId ? { ...i, name } : i)));
    } catch (err) {
      setSaveState('error');
      setSaveError(describeCanvasError(err).message);
    }
  }, []);

  const deleteArtifact = useCallback(async () => {
    const snap = snapshotRef.current;
    if (!snap) return;
    if (!(await confirmDialog({ title: `Archive “${snap.name}”?`, tone: 'danger' }))) return;
    try {
      await archiveArtifact(snap.artifactId);
      setItems((prev) => prev.filter((i) => i.artifactId !== snap.artifactId));
      setSnapshot(null);
      setSelectedId(null);
      setContent('');
      setSavedContent('');
      setSaveState('idle');
      setProposal(null);
    } catch (err) {
      setListError(describeCanvasError(err).message);
    }
  }, []);

  // ── Save ────────────────────────────────────────────────────────────────────
  const persist = useCallback(async (next: string) => {
    const snap = snapshotRef.current;
    if (!snap || savingRef.current) return;
    savingRef.current = true;
    setSaveState('saving');
    setSaveError(null);
    try {
      const result = await saveCanvasArtifact(snap.artifactId, { baseVersion: snap.version, content: next });
      if (snapshotRef.current?.artifactId !== snap.artifactId) return;
      setSnapshot(result);
      setContent(result.content);
      setSavedContent(result.content);
      setSaveState('saved');
      setConflictVersion(null);
      patchListItem(result);
    } catch (err) {
      if (snapshotRef.current?.artifactId !== snap.artifactId) return;
      const mapped = describeCanvasError(err);
      if (mapped.conflict) {
        setSaveState('conflict');
        setConflictVersion(mapped.currentVersion ?? null);
      } else {
        setSaveState('error');
        setSaveError(mapped.message);
      }
    } finally {
      savingRef.current = false;
    }
  }, [patchListItem]);

  const updateContent = useCallback((next: string) => {
    setContent(next);
    setSaveState((prev) => (prev === 'conflict' ? prev : 'idle'));
    setSaveError(null);
  }, []);

  useEffect(() => {
    if (!editable || !snapshot || saveState === 'conflict' || saveState === 'saving' || saveState === 'error') return;
    if (content === savedContent) return;
    const id = window.setTimeout(() => { void persist(content); }, 1200);
    return () => window.clearTimeout(id);
  }, [content, savedContent, snapshot, saveState, editable, persist]);

  const saveNow = useCallback(() => {
    setSaveState((prev) => (prev === 'conflict' ? prev : 'idle'));
    void persist(contentRef.current);
  }, [persist]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (editable) saveNow();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editable, saveNow]);

  const restoreVersion = useCallback(async (version: number) => {
    const snap = snapshotRef.current;
    if (!snap) return;
    setSaveState('saving');
    setSaveError(null);
    try {
      const result = await restoreCanvasVersion(snap.artifactId, { baseVersion: snap.version, version });
      setSnapshot(result);
      setContent(result.content);
      setSavedContent(result.content);
      setSaveState('saved');
      setConflictVersion(null);
      setProposal(null);
      patchListItem(result);
    } catch (err) {
      const mapped = describeCanvasError(err);
      if (mapped.conflict) { setSaveState('conflict'); setConflictVersion(mapped.currentVersion ?? null); }
      else { setSaveState('error'); setSaveError(mapped.message); }
    }
  }, [patchListItem]);

  const reloadLatest = useCallback(async () => {
    const snap = snapshotRef.current;
    if (!snap) return;
    try {
      const result = await getCanvasArtifact(snap.artifactId);
      setSnapshot(result);
      setContent(result.content);
      setSavedContent(result.content);
      setSaveState('idle');
      setConflictVersion(null);
      setProposal(null);
    } catch (err) {
      setArtifactError(describeCanvasError(err));
    }
  }, []);

  const changeLanguage = useCallback((language: string) => {
    setSnapshot((prev) => (prev ? { ...prev, language } : prev));
  }, []);

  // ── Export ──────────────────────────────────────────────────────────────────
  const exportArtifact = useCallback(async (action: 'download' | 'copy' | 'print' | 'pptx' | 'pdf' | 'png') => {
    const snap = snapshotRef.current;
    if (!snap) return;
    if (action === 'pptx' || action === 'pdf' || action === 'png') {
      const label = action === 'pptx' ? 'PowerPoint' : action === 'pdf' ? 'PDF' : 'PNG slides';
      setNotice(`Exporting ${label}…`);
      try {
        const result = await exportPresentation(snap.artifactId, { format: action });
        const overflow = result.overflowSlides.length
          ? ` · overflow on slides ${result.overflowSlides.join(', ')}`
          : '';
        setNotice(`${label} saved to ${result.path}${overflow}`);
      } catch (err) {
        setSaveState('error');
        setSaveError(err instanceof Error ? err.message : `The ${label} export failed.`);
        setNotice(null);
      }
      return;
    }
    if (action === 'print') { window.print(); return; }
    if (action === 'copy') {
      try {
        await navigator.clipboard.writeText(contentRef.current);
      } catch {
        setSaveState('error');
        setSaveError('Clipboard is unavailable in this browser.');
      }
      return;
    }
    const ext = contentExtension(snap.kind, snap.language);
    const blob = new Blob([contentRef.current], { type: contentMime(snap.kind, snap.language) });
    saveBlob(safeFilename(snap.name, ext), blob);
  }, []);

  // ── AI proposals ────────────────────────────────────────────────────────────
  const propose = useCallback(async () => {
    const snap = snapshotRef.current;
    if (!snap || instruction.trim() === '') return;
    setProposing(true);
    setProposalError(null);
    try {
      const result = await proposeCanvasEdit(snap.artifactId, {
        baseVersion: snap.version,
        instruction,
        mode,
        selection: useSelection && selection ? selection : undefined,
      });
      setProposal(result);
    } catch (err) {
      setProposalError(describeCanvasError(err).message);
    } finally {
      setProposing(false);
    }
  }, [instruction, mode, useSelection, selection]);

  const acceptProposal = useCallback(async () => {
    const snap = snapshotRef.current;
    const prop = proposalRef.current;
    if (!snap || !prop) return;
    setApplying(true);
    setProposalError(null);
    try {
      const operations = proposalOperations(prop);
      const input = operations.length > 0
        ? { baseVersion: snap.version, operations }
        : { baseVersion: snap.version, content: prop.resultContent ?? snap.content };
      const result = await saveCanvasArtifact(snap.artifactId, input);
      setSnapshot(result);
      setContent(result.content);
      setSavedContent(result.content);
      setSaveState('saved');
      setConflictVersion(null);
      setProposal(null);
      patchListItem(result);
    } catch (err) {
      const mapped = describeCanvasError(err);
      if (mapped.conflict) { setSaveState('conflict'); setConflictVersion(mapped.currentVersion ?? null); }
      else { setProposalError(mapped.message); }
    } finally {
      setApplying(false);
    }
  }, [patchListItem]);

  const handleSelectionChange = useCallback((sel: CanvasSelection | null) => {
    setSelection(sel);
    if (!sel) setUseSelection(false);
  }, []);

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <CanvasWorkspace
      inline={inline}
      onClose={onClose}
      onCreate={(kind) => void createArtifact(kind)}
      loadingArtifact={loadingArtifact}
      artifactError={artifactError}
      onRetryArtifact={() => { if (selectedId) void loadArtifact(selectedId); }}
      rail={{
        items, loading: listLoading, error: listError, query, onQueryChange: setQuery,
        onRefresh: () => void loadList(query.trim()),
        onSelect: (id) => void loadArtifact(id),
        onCreate: (kind) => void createArtifact(kind),
        onImport: (file) => void importArtifact(file),
        selectedId, collapsed: railCollapsed,
        onToggleCollapsed: () => setRailCollapsed((v) => !v),
      }}
      center={snapshot ? {
        snapshot, content, dirty, saveState, saveError, notice,
        onContentChange: updateContent,
        onSelectionChange: handleSelectionChange,
        editorHandleRef,
        onRename: (name) => void renameArtifact(name),
        onLanguageChange: changeLanguage,
        onRestore: (version) => void restoreVersion(version),
        onExport: (action) => void exportArtifact(action),
        onSaveNow: saveNow,
        onDelete: () => void deleteArtifact(),
        fullWidth,
        onToggleFullWidth: () => setFullWidth((v) => !v),
        editable,
      } : null}
      ai={{
        mode, onModeChange: setMode,
        instruction, onInstructionChange: setInstruction,
        selection, useSelection,
        onToggleUseSelection: () => setUseSelection((v) => !v),
        onPropose: () => void propose(),
        proposing, proposal,
        onAccept: () => void acceptProposal(),
        onDiscard: () => { setProposal(null); setProposalError(null); },
        applying, error: proposalError,
        conflictCurrentVersion: conflictVersion,
        onReloadLatest: () => void reloadLatest(),
        currentContent: content,
        disabled: !editable,
        collapsed: aiCollapsed,
        onToggleCollapsed: () => setAiCollapsed((v) => !v),
      }}
    />
  );
}
