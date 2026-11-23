'use client';

// ── Versions and export ─────────────────────────────────────────────────────
// Strategy versions are immutable and content-hashed by the runtime. This panel
// lists them, shows the STRUCTURAL diff between two, and exports the portable
// bundle as JSON. It adds no version logic of its own.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { saveBlob } from '@/lib/desktop/download';
import {
  backtestClient,
  type StrategyDiff,
  type StrategyRecord,
  type StrategyVersionMeta,
} from '@/lib/backtest/backtest-client';
import { Download, GitBranch, Loader2 } from '@/lib/icons';
import { cn } from '@/lib/utils';

function shortHash(hash?: string | null): string {
  return hash ? hash.slice(0, 10) : '—';
}

function renderValue(value: unknown): string {
  if (value === undefined) return '—';
  if (typeof value === 'string') return value;
  try {
    const text = JSON.stringify(value);
    return text.length > 80 ? `${text.slice(0, 77)}…` : text;
  } catch {
    return String(value);
  }
}

export function VersionPanel({
  strategy,
  onError,
  onImported,
}: {
  strategy: StrategyRecord;
  onError: (message: string | null) => void;
  onImported?: () => void;
}) {
  const [versions, setVersions] = useState<StrategyVersionMeta[]>([]);
  const [a, setA] = useState('');
  const [b, setB] = useState('');
  const [diff, setDiff] = useState<StrategyDiff | null>(null);
  const [busy, setBusy] = useState(false);
  const [importNote, setImportNote] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const list = await backtestClient.listVersions(strategy.id);
      setVersions(list);
      const latest = list[list.length - 1]?.version ?? strategy.version;
      setB(latest);
      setA(list.length > 1 ? list[list.length - 2].version : latest);
    } catch (err) {
      onError(err instanceof Error ? err.message : 'could not list versions');
    }
  }, [onError, strategy.id, strategy.version]);

  useEffect(() => {
    void load();
  }, [load]);

  const compare = async () => {
    if (!a || !b) return;
    setBusy(true);
    onError(null);
    try {
      setDiff(await backtestClient.diffVersions(strategy.id, a, b));
    } catch (err) {
      onError(err instanceof Error ? err.message : 'could not diff versions');
    } finally {
      setBusy(false);
    }
  };

  const importFile = async (file: File) => {
    setBusy(true);
    onError(null);
    setImportNote(null);
    setImportError(null);
    try {
      const parsed = JSON.parse(await file.text()) as Record<string, unknown>;
      // Accept both the downloaded bundle and a `{ bundle: … }` wrapper.
      const bundle =
        parsed.bundle && typeof parsed.bundle === 'object' && !Array.isArray(parsed.bundle)
          ? (parsed.bundle as Record<string, unknown>)
          : parsed;
      const result = await backtestClient.importBundle(bundle, true);
      if (!result.valid) {
        const first = result.issues?.[0];
        setImportError(
          `Bundle is invalid: ${result.issues?.length ?? 0} issue(s)${first ? ` — ${first.message}` : ''}.`,
        );
        return;
      }
      const imported = result.strategy as { id?: string; name?: string; version?: string } | undefined;
      setImportNote(
        `Imported ${imported?.name ?? imported?.id ?? 'strategy'}${imported?.version ? ` v${imported.version}` : ''}.`,
      );
      onImported?.();
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'could not import the bundle');
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const exportBundle = async () => {
    setBusy(true);
    onError(null);
    try {
      const bundle = await backtestClient.exportBundle(strategy.id, b || strategy.version);
      if (typeof window === 'undefined') return;
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
      saveBlob(`${strategy.id}@${b || strategy.version}.bundle.json`, blob);
    } catch (err) {
      onError(err instanceof Error ? err.message : 'could not export the bundle');
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="glass-card rounded-2xl p-4">
      <summary className="flex cursor-pointer flex-wrap items-center gap-2 text-sm font-semibold">
        <GitBranch className="h-4 w-4 text-primary" />
        Versions &amp; export
        <span className="font-mono text-[10px] font-normal text-muted-foreground">
          {versions.length ? `${versions.length} version${versions.length === 1 ? '' : 's'}` : 'loading…'}
        </span>
        <span className="ml-auto font-mono text-[10px] font-normal text-muted-foreground">immutable · content-hashed</span>
      </summary>

      <div className="mt-3 flex flex-col gap-3">
        <div className="overflow-auto">
          <table className="w-full text-left font-mono text-[10px]">
            <thead className="text-muted-foreground">
              <tr>
                <th className="px-1 py-0.5">Version</th>
                <th className="px-1 py-0.5">Created</th>
                <th className="px-1 py-0.5">Content hash</th>
                <th className="px-1 py-0.5">Schema</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((version) => (
                <tr key={version.version} className="border-t border-white/5">
                  <td className="px-1 py-0.5 text-foreground">
                    v{version.version}
                    {version.version === strategy.version ? (
                      <span className="ml-1 text-[9px] text-emerald-400">shown</span>
                    ) : null}
                  </td>
                  <td className="px-1 py-0.5">{version.createdAt?.slice(0, 16).replace('T', ' ')}</td>
                  <td className="px-1 py-0.5">{shortHash(version.contentHash)}</td>
                  <td className="px-1 py-0.5">{version.schemaVersion}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
            from
            <select value={a} onChange={(event) => setA(event.target.value)} className="glass-card-subtle rounded-lg px-2 py-1 font-mono text-[10px] text-foreground">
              {versions.map((version) => (
                <option key={version.version} value={version.version}>v{version.version}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
            to
            <select value={b} onChange={(event) => setB(event.target.value)} className="glass-card-subtle rounded-lg px-2 py-1 font-mono text-[10px] text-foreground">
              {versions.map((version) => (
                <option key={version.version} value={version.version}>v{version.version}</option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => void compare()}
            disabled={busy || !a || !b}
            className="glass-button flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[10px] disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <GitBranch className="h-3 w-3" />}
            Compare
          </button>
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={busy}
            className="glass-button flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[10px] disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3 rotate-180" />}
            Import bundle
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void importFile(file);
            }}
          />
          <button
            type="button"
            onClick={() => void exportBundle()}
            disabled={busy}
            className="glass-button ml-auto flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[10px] disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
            Export v{b || strategy.version} bundle
          </button>
        </div>

        {diff ? (
          <div className="rounded-xl border border-white/10 p-2">
            <p className="mb-1 font-mono text-[10px] text-muted-foreground">
              {diff.sameContent
                ? 'the two versions have the same content hash'
                : `${diff.changeCount} structural change${diff.changeCount === 1 ? '' : 's'} · ${Object.entries(diff.bySection)
                    .map(([section, count]) => `${section} (${count})`)
                    .join(', ')}`}
            </p>
            {diff.changes.length ? (
              <div className="max-h-[220px] overflow-auto">
                <table className="w-full text-left font-mono text-[10px]">
                  <thead className="text-muted-foreground">
                    <tr>
                      <th className="px-1 py-0.5">Change</th>
                      <th className="px-1 py-0.5">Path</th>
                      <th className="px-1 py-0.5">Before</th>
                      <th className="px-1 py-0.5">After</th>
                    </tr>
                  </thead>
                  <tbody>
                    {diff.changes.slice(0, 60).map((change, index) => (
                      <tr key={`${change.path}-${index}`} className="border-t border-white/5">
                        <td
                          className={cn(
                            'px-1 py-0.5',
                            change.kind === 'added'
                              ? 'text-emerald-400'
                              : change.kind === 'removed'
                                ? 'text-rose-400'
                                : 'text-amber-300',
                          )}
                        >
                          {change.kind}
                        </td>
                        <td className="px-1 py-0.5 text-foreground">{change.path}</td>
                        <td className="px-1 py-0.5 text-muted-foreground">{renderValue(change.a)}</td>
                        <td className="px-1 py-0.5 text-muted-foreground">{renderValue(change.b)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        ) : null}

        {importNote ? <p className="text-[10px] text-emerald-300">{importNote}</p> : null}
        {importError ? <p className="text-[10px] text-rose-300">{importError}</p> : null}

        <p className="text-[9px] leading-relaxed text-muted-foreground/60">
          A version that has experiments is immutable. The bundle is the portable record (spec + provenance); it does
          not carry your local experiments. Import validates first and saves only a valid bundle.
        </p>
      </div>
    </details>
  );
}
