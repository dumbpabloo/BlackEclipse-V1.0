'use client';

// ── UI Builder ────────────────────────────────────────────────────────────────
// A real builder over the UI Engineering runtime. It lists the saved pages and
// the registered component catalog, renders a live structural preview of the
// selected blueprint, edits widget configuration against each component's
// declared schema, and applies changes through the SAME `ui.*` operations the
// agent uses (optimistic concurrency, revision history, preview/diff). Nothing
// here invents data: every value comes from `/api/ui/**` (the Python runtime).

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { saveBlob } from '@/lib/desktop/download';
import { cn } from '@/lib/utils';
import { DesktopOnlyNotice } from '@/components/ui/desktop-only-notice';
import { Plus, Trash2, ArrowUp, ArrowDown, Sparkles, Search, Layers, History } from '@/lib/icons';
import { getWidgetDefinition, renderDashboardWidget } from '@/lib/dashboard/registry';
import { syncCoreCatalog } from '@/lib/harness/ui-sync';
import { setUiFocus } from '@/lib/ui-engineering/ui-focus';
import { LiveBoundary } from './live-boundary';
import {
  addWidget,
  classifyRequest,
  createPage,
  engineeringStatus,
  exportBlueprint,
  getCurrentPage,
  getHistory,
  importBlueprint,
  listBlueprints,
  listComponents,
  removeWidget,
  reorderWidget,
  resizeWidget,
  restoreRevision,
  startEngineering,
  updateWidgetConfig,
  type BlueprintChange,
  type BlueprintSummary,
  type UiBlueprint,
  type UiComponentSpec,
  type UiInstance,
} from '@/lib/harness/clients/ui-client';

interface LogEntry { ts: string; text: string; ok: boolean }

// The plan view returned by GET /ui/engineering/{planId} (best-effort typing —
// the runner owns the exact shape; we only read status + task statuses).
interface EngView {
  plan?: { id?: string; status?: string };
  tasks?: Array<{ id?: string; title?: string; status?: string }>;
}

const SIZE_PRESETS = [null, 160, 240, 320, 420];

function hasHostRenderer(widgetId: string): boolean {
  return getWidgetDefinition(widgetId) !== undefined;
}

function firstTarget(blueprint: UiBlueprint | null): { sectionId: string; columnId: string; index: number } | null {
  const section = blueprint?.layout.sections[0];
  const column = section?.columns[0];
  if (!section || !column) return null;
  return { sectionId: section.id, columnId: column.id, index: column.widgets.length };
}

export function UiBuilderScreen({ embedded = false }: { embedded?: boolean } = {}) {
  const [blueprints, setBlueprints] = useState<BlueprintSummary[]>([]);
  const [components, setComponents] = useState<UiComponentSpec[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [blueprint, setBlueprint] = useState<UiBlueprint | null>(null);
  const [instances, setInstances] = useState<UiInstance[]>([]);
  const [selectedInstance, setSelectedInstance] = useState<string | null>(null);
  const [configDraft, setConfigDraft] = useState<Record<string, unknown>>({});
  const [revisions, setRevisions] = useState<{ revision: number; summary: string; changes: BlueprintChange[]; createdAt: string }[]>([]);
  const [catalogSearch, setCatalogSearch] = useState('');
  const [aiText, setAiText] = useState('');
  const [aiResult, setAiResult] = useState<{ mode: string; reasons: string[]; matchedComponents: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [liveRender, setLiveRender] = useState(false);
  const [engPlanId, setEngPlanId] = useState<string | null>(null);
  const [engView, setEngView] = useState<EngView | null>(null);
  const importRef = useRef<HTMLInputElement>(null);

  const note = useCallback((text: string, ok = true) => {
    setLog((prev) => [{ ts: new Date().toLocaleTimeString(), text, ok }, ...prev].slice(0, 40));
  }, []);

  const refreshList = useCallback(async () => {
    try {
      const [b, c] = await Promise.all([listBlueprints({ limit: 100 }), listComponents({ limit: 200 })]);
      setBlueprints(b.blueprints);
      setComponents(c.components);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'runtime unavailable');
    }
  }, []);

  const openBlueprint = useCallback(async (id: string) => {
    setBusy(true);
    try {
      const [current, hist] = await Promise.all([getCurrentPage(id), getHistory(id)]);
      setSelectedId(id);
      setBlueprint(current.blueprint);
      setUiFocus({ page: { blueprintId: current.blueprint.id, title: current.blueprint.title, path: current.blueprint.path ?? undefined } });
      setInstances(current.instances);
      setRevisions(hist.revisions);
      setSelectedInstance(current.instances[0]?.instanceId ?? null);
      setConfigDraft({});
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to open page');
    } finally {
      setBusy(false);
    }
  }, []);

  const reopen = useCallback(async () => {
    if (selectedId) await openBlueprint(selectedId);
  }, [selectedId, openBlueprint]);

  useEffect(() => { void refreshList(); }, [refreshList]);

  // Seed the runtime catalog with the REAL dashboard widgets once (persisted
  // server-side), so the agent can Compose with them.
  useEffect(() => {
    void syncCoreCatalog().then((r) => { if (r.registered > 0) note(`Synced ${r.registered} catalog components`, true); });
  }, [note]);

  const selectedSpec = useMemo(() => {
    const inst = instances.find((i) => i.instanceId === selectedInstance);
    return inst ? components.find((c) => c.id === inst.componentId) ?? null : null;
  }, [instances, selectedInstance, components]);

  useEffect(() => {
    const inst = instances.find((i) => i.instanceId === selectedInstance);
    setConfigDraft(inst ? { ...inst.config } : {});
  }, [selectedInstance, instances]);

  // Poll a running engineering task's status so "build me a new widget" is
  // visible instead of a fire-and-forget. Stops on a terminal plan status.
  useEffect(() => {
    if (!engPlanId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await engineeringStatus(engPlanId);
        if (cancelled) return;
        setEngView(res.plan as unknown as EngView);
      } catch {
        /* transient — keep the last view */
      }
    };
    void poll();
    const timer = setInterval(poll, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [engPlanId]);

  const filtered = useMemo(() => {
    const q = catalogSearch.trim().toLowerCase();
    if (!q) return components;
    return components.filter((c) => `${c.id} ${c.displayName} ${c.category}`.toLowerCase().includes(q));
  }, [components, catalogSearch]);

  const run = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await reopen();
      note(label, true);
      setError(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'operation failed';
      note(`${label} — ${msg}`, false);
      setError(msg);
    } finally {
      setBusy(false);
    }
  }, [reopen, note]);

  const handleCreate = () => run('Created page', async () => {
    const { blueprint: created } = await createPage({ title: `Untitled ${blueprints.length + 1}` });
    await refreshList();
    await openBlueprint(created.id);
  });

  const handleAdd = (componentId: string) => {
    if (!selectedId || !blueprint) return;
    const target = firstTarget(blueprint);
    if (!target) return;
    void run(`Added ${componentId}`, () => addWidget(selectedId, { componentId, target }));
  };

  const handleRemove = (instanceId: string) => selectedId && void run(`Removed ${instanceId}`, () => removeWidget(selectedId, instanceId));

  const handleReorder = (instanceId: string, toIndex: number) => selectedId && void run('Reordered', () => reorderWidget(selectedId, instanceId, toIndex));

  const handleResize = (instanceId: string, height: number | null) => selectedId && void run('Resized', () => resizeWidget(selectedId, instanceId, height));

  const handleApplyConfig = () => selectedId && selectedInstance && void run('Updated config', () => updateWidgetConfig(selectedId, selectedInstance, configDraft));

  const handleRestore = (revision: number) => selectedId && void run(`Restored r${revision}`, () => restoreRevision(selectedId, revision));

  const handleClassify = async () => {
    if (!aiText.trim()) return;
    setBusy(true);
    try {
      const result = await classifyRequest(aiText, selectedId ?? undefined);
      setAiResult(result);
      note(`Mode: ${result.mode}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'classification failed');
    } finally {
      setBusy(false);
    }
  };

  const handleApplyAi = () => {
    const match = aiResult?.matchedComponents?.[0];
    if (match) handleAdd(match);
  };

  const handleEngineer = async () => {
    if (!aiText.trim()) return;
    setBusy(true);
    try {
      const res = await startEngineering(aiText, selectedId ?? undefined);
      if (res.created && res.taskId) {
        note(`Engineering task ${res.taskId} created (plan ${res.planId})`);
        setEngPlanId(res.planId ?? null);
      } else {
        note(res.message ?? 'Compose mode — no task created');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'engineering start failed');
    } finally {
      setBusy(false);
    }
  };

  const handleExport = async () => {
    if (!selectedId) return;
    try {
      const res = await exportBlueprint(selectedId);
      const blob = new Blob([JSON.stringify(res, null, 2)], { type: 'application/json' });
      saveBlob(`${blueprint?.title ?? 'blueprint'}.eclipse-ui.json`, blob);
      note('Exported blueprint', true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'export failed');
    }
  };

  const handleImportFile = async (file: File) => {
    setBusy(true);
    try {
      const payload = JSON.parse(await file.text());
      const res = await importBlueprint(payload);
      await refreshList();
      await openBlueprint(res.blueprint.id);
      note(`Imported ${res.blueprint.title}`, true);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'import failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full w-full flex-col gap-4">
      <DesktopOnlyNotice title="UI Builder" />
      <header className="flex flex-wrap items-center justify-between gap-3">
        {embedded ? null : (
          <div>
            <h1 className="font-serif text-2xl font-semibold text-foreground">UI Builder</h1>
            <p className="text-xs text-muted-foreground">Compose pages, workspaces and widgets over the live UI runtime.</p>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => void refreshList()} className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground">Refresh</button>
          <button type="button" onClick={() => void syncCoreCatalog(true).then((r) => { note(`Synced ${r.registered}/${r.total} components`, r.failed === 0); void refreshList(); })} className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground">Sync catalog</button>
          <button type="button" onClick={() => void handleExport()} disabled={!selectedId} className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground disabled:opacity-50">Export</button>
          <button type="button" onClick={() => importRef.current?.click()} disabled={busy} className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground disabled:opacity-50">Import</button>
          <input ref={importRef} type="file" accept="application/json,.json" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleImportFile(f); e.target.value = ''; }} />
          <button type="button" onClick={handleCreate} disabled={busy} className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground inline-flex items-center gap-1.5 disabled:opacity-50">
            <Plus size={14} /> New page
          </button>
        </div>
      </header>

      {error && <div className="glass-card-subtle rounded-lg px-3 py-2 text-xs text-rose-300">Runtime: {error}</div>}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[240px_minmax(0,1fr)_320px]">
        {/* Navigator */}
        <aside className="glass-card flex min-h-0 flex-col rounded-xl p-3">
          <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Pages</h2>
          <div className="flex-1 space-y-1 overflow-y-auto">
            {blueprints.length === 0 && <p className="text-xs text-muted-foreground">No pages yet.</p>}
            {blueprints.map((b) => (
              <button key={b.id} type="button" onClick={() => void openBlueprint(b.id)}
                className={cn('w-full rounded-lg px-2.5 py-2 text-left text-xs transition-colors',
                  selectedId === b.id ? 'bg-white/10 text-foreground' : 'text-muted-foreground hover:bg-white/5')}>
                <span className="block truncate font-medium text-foreground">{b.title}</span>
                <span className="text-[10px] text-muted-foreground">{b.kind} · r{b.revision} · {b.widgetCount} widgets</span>
              </button>
            ))}
          </div>

          <h2 className="mb-2 mt-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Components</h2>
          <div className="mb-2 flex items-center gap-1.5 rounded-lg bg-black/20 px-2 py-1">
            <Search size={13} className="text-muted-foreground" />
            <input value={catalogSearch} onChange={(e) => setCatalogSearch(e.target.value)} placeholder="Search catalog…"
              className="w-full bg-transparent text-xs text-foreground outline-none placeholder:text-muted-foreground" />
          </div>
          <div className="max-h-56 space-y-1 overflow-y-auto">
            {filtered.map((c) => (
              <button key={c.id} type="button" disabled={!blueprint || busy} onClick={() => handleAdd(c.id)}
                title={`Add ${c.displayName}`}
                className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-muted-foreground hover:bg-white/5 disabled:opacity-40">
                <Plus size={12} />
                <span className="min-w-0 flex-1 truncate text-foreground">{c.displayName}</span>
                <span className="text-[10px] text-muted-foreground">{c.category}</span>
              </button>
            ))}
          </div>
        </aside>

        {/* Preview canvas */}
        <section className="glass-card flex min-h-0 flex-col rounded-xl p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Preview {blueprint ? `· ${blueprint.title} (r${blueprint.revision})` : ''}
            </h2>
            <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
              <input type="checkbox" checked={liveRender} onChange={(e) => setLiveRender(e.target.checked)} />
              live render
            </label>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {!blueprint && <p className="text-xs text-muted-foreground">Select or create a page to begin.</p>}
            {blueprint?.layout.sections.map((section) => (
              <div key={section.id} className="mb-4">
                <div className="mb-2 text-[10px] uppercase tracking-wide text-muted-foreground">section {section.zone ?? 'main'}</div>
                <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.max(1, section.columns.length)}, minmax(0, 1fr))` }}>
                  {section.columns.map((column) => (
                    <div key={column.id} className="space-y-2">
                      {column.widgets.map((w, i) => {
                        const inst = instances.find((x) => x.instanceId === w.id);
                        const spec = components.find((c) => c.id === w.widgetId);
                        return (
                          <div key={w.id}
                            onClick={() => setSelectedInstance(w.id)}
                            className={cn('glass-card-subtle cursor-pointer rounded-lg p-2.5',
                              selectedInstance === w.id && 'ring-1 ring-sky-400/60')}>
                            <div className="flex items-center justify-between gap-2">
                              <span className="truncate text-xs font-medium text-foreground">{spec?.displayName ?? w.widgetId}</span>
                              <span className="shrink-0 text-[10px] text-muted-foreground">{spec?.category ?? 'unknown'}</span>
                            </div>
                            {liveRender && hasHostRenderer(w.widgetId) ? (
                              <div className="mt-1.5" style={{ height: w.height ?? spec?.layout.defaultHeight ?? 300 }}>
                                <LiveBoundary label={spec?.displayName ?? w.widgetId}>
                                  {renderDashboardWidget(w.widgetId, {
                                    id: w.id, widgetId: w.widgetId, height: w.height,
                                    config: w.config ?? undefined,
                                  })}
                                </LiveBoundary>
                              </div>
                            ) : (
                              <div className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground">
                                <span>{inst?.availability ?? 'unavailable'}</span>
                                {w.height != null && <span>· {w.height}px</span>}
                                {w.config && Object.keys(w.config).length > 0 && <span>· {Object.keys(w.config).length} cfg</span>}
                              </div>
                            )}
                            <div className="mt-1.5 flex items-center gap-1">
                              <button type="button" title="Move up" disabled={i === 0} onClick={(e) => { e.stopPropagation(); handleReorder(w.id, i - 1); }} className="text-muted-foreground hover:text-foreground disabled:opacity-30"><ArrowUp size={12} /></button>
                              <button type="button" title="Move down" disabled={i === column.widgets.length - 1} onClick={(e) => { e.stopPropagation(); handleReorder(w.id, i + 1); }} className="text-muted-foreground hover:text-foreground disabled:opacity-30"><ArrowDown size={12} /></button>
                              <button type="button" title="Remove" onClick={(e) => { e.stopPropagation(); handleRemove(w.id); }} className="text-muted-foreground hover:text-rose-300"><Trash2 size={12} /></button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* Inspector + AI + diagnostics */}
        <aside className="flex min-h-0 flex-col gap-3 overflow-y-auto">
          <div className="glass-card rounded-xl p-3">
            <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Properties</h2>
            {!selectedInstance && <p className="text-xs text-muted-foreground">Select a widget.</p>}
            {selectedInstance && (
              <div className="space-y-2">
                <p className="text-xs text-foreground">{selectedSpec?.displayName ?? 'unknown component'}</p>
                <div className="flex items-center gap-1">
                  {SIZE_PRESETS.map((h) => (
                    <button key={String(h)} type="button" onClick={() => handleResize(selectedInstance, h)}
                      className="glass-card-subtle rounded px-2 py-1 text-[10px] text-muted-foreground hover:text-foreground">{h ?? 'auto'}</button>
                  ))}
                </div>
                {(selectedSpec?.configSchema ?? []).map((field) => (
                  <label key={field.key} className="block text-[11px] text-muted-foreground">
                    {field.label}
                    {field.kind === 'boolean' ? (
                      <input type="checkbox" checked={Boolean(configDraft[field.key])}
                        onChange={(e) => setConfigDraft((d) => ({ ...d, [field.key]: e.target.checked }))} className="ml-2 align-middle" />
                    ) : field.kind === 'select' ? (
                      <select value={String(configDraft[field.key] ?? '')}
                        onChange={(e) => setConfigDraft((d) => ({ ...d, [field.key]: e.target.value }))}
                        className="mt-0.5 w-full rounded bg-black/30 px-2 py-1 text-xs text-foreground outline-none">
                        <option value="">—</option>
                        {field.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                      </select>
                    ) : (
                      <input value={Array.isArray(configDraft[field.key]) ? (configDraft[field.key] as string[]).join(', ') : String(configDraft[field.key] ?? '')}
                        onChange={(e) => setConfigDraft((d) => ({ ...d, [field.key]: field.kind === 'symbols' ? e.target.value.split(',').map((s) => s.trim()).filter(Boolean) : e.target.value }))}
                        className="mt-0.5 w-full rounded bg-black/30 px-2 py-1 text-xs text-foreground outline-none" />
                    )}
                  </label>
                ))}
                <button type="button" onClick={handleApplyConfig} disabled={busy}
                  className="glass-button w-full rounded-lg px-3 py-1.5 text-xs text-foreground disabled:opacity-50">Apply config</button>
              </div>
            )}
          </div>

          <div className="glass-card rounded-xl p-3">
            <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">AI command</h2>
            <textarea value={aiText} onChange={(e) => setAiText(e.target.value)} rows={2}
              placeholder='e.g. "add a metric card", "build a new 3D component"'
              className="w-full resize-none rounded-lg bg-black/30 px-2 py-1.5 text-xs text-foreground outline-none placeholder:text-muted-foreground" />
            <div className="mt-2 flex items-center gap-2">
              <button type="button" onClick={() => void handleClassify()} disabled={busy || !aiText.trim()}
                className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground disabled:opacity-50 inline-flex items-center gap-1.5">
                <Sparkles size={13} /> Classify
              </button>
              {aiResult?.matchedComponents[0] && aiResult.mode === 'compose' && (
                <button type="button" onClick={handleApplyAi} disabled={busy}
                  className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground disabled:opacity-50">Add {aiResult.matchedComponents[0]}</button>
              )}
              {aiResult && aiResult.mode !== 'compose' && (
                <button type="button" onClick={() => void handleEngineer()} disabled={busy}
                  className="glass-button rounded-lg px-3 py-1.5 text-xs text-foreground disabled:opacity-50">Create engineering task</button>
              )}
            </div>
            {aiResult && (
              <div className="mt-2 text-[11px] text-muted-foreground">
                <span className={cn('font-medium', aiResult.mode === 'engineer' ? 'text-amber-300' : aiResult.mode === 'both' ? 'text-sky-300' : 'text-emerald-300')}>{aiResult.mode}</span>
                {' — '}{aiResult.reasons[0]}
              </div>
            )}

            {engPlanId && (
              <div className="mt-2 rounded-lg bg-black/20 px-2 py-1.5 text-[11px] text-muted-foreground">
                <div className="flex items-center gap-1.5">
                  <span className="font-medium text-foreground">Engineering task</span>
                  <span className={cn('rounded px-1.5 py-0.5 text-[10px]',
                    engView?.plan?.status === 'COMPLETED' ? 'bg-emerald-500/20 text-emerald-300'
                    : engView?.plan?.status === 'FAILED' ? 'bg-rose-500/20 text-rose-300'
                    : 'bg-sky-500/20 text-sky-300')}>
                    {engView?.plan?.status ?? 'starting'}
                  </span>
                </div>
                {(engView?.tasks ?? []).slice(0, 4).map((t) => (
                  <div key={t.id} className="mt-0.5 flex items-center justify-between gap-2">
                    <span className="min-w-0 flex-1 truncate">{t.title ?? t.id}</span>
                    <span className="shrink-0 text-[10px]">{t.status}</span>
                  </div>
                ))}
                {(engView?.tasks?.length ?? 0) > 4 && (
                  <div className="mt-0.5 text-[10px]">+{(engView?.tasks?.length ?? 0) - 4} more</div>
                )}
                <div className="mt-1 text-[10px]">Runs in the background — see the Tasks tab for full detail.</div>
              </div>
            )}
          </div>

          <div className="glass-card rounded-xl p-3">
            <h2 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"><History size={12} /> History</h2>
            <div className="max-h-40 space-y-1 overflow-y-auto">
              {revisions.map((r) => (
                <button key={r.revision} type="button" onClick={() => handleRestore(r.revision)}
                  className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-[11px] text-muted-foreground hover:bg-white/5">
                  <span className="truncate">r{r.revision} · {r.summary}</span>
                  <span className="shrink-0 text-[10px]">{r.changes.length}Δ</span>
                </button>
              ))}
              {revisions.length === 0 && <p className="text-xs text-muted-foreground">No revisions.</p>}
            </div>
          </div>

          <div className="glass-card rounded-xl p-3">
            <h2 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"><Layers size={12} /> Diagnostics</h2>
            <div className="max-h-40 space-y-1 overflow-y-auto font-mono text-[10px]">
              {log.length === 0 && <p className="text-muted-foreground">No operations yet.</p>}
              {log.map((entry, i) => (
                <div key={i} className={cn(entry.ok ? 'text-muted-foreground' : 'text-rose-300')}>{entry.ts} {entry.text}</div>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
