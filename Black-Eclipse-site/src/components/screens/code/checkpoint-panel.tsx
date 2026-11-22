'use client';

// W7 file checkpoints. Read-only list + diff, with an explicit, confirmed
// restore. Restore is effectful and operator-gated; the runtime takes a
// pre-restore snapshot, so the panel can always say the restore is undoable.

import { useCallback, useEffect, useState } from 'react';
import { confirmDialog } from '@/lib/ui/confirm-dialog';
import { useT } from '@/lib/i18n/use-t';
import {
  diffFileCheckpoint,
  listFileCheckpoints,
  restoreFileCheckpoint,
  type FileCheckpointView,
} from '@/lib/harness/clients/code-browser';

export function CheckpointPanel({ rootId }: { rootId?: string | null }) {
  const t = useT();
  const [items, setItems] = useState<FileCheckpointView[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [diff, setDiff] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setItems(await listFileCheckpoints(20));
    } catch {
      setMessage(t('code.checkpoints.loadFailed'));
    }
  }, [t]);

  useEffect(() => {
    void refresh();
  }, [refresh, rootId]);

  const showDiff = useCallback(async (id: string) => {
    setSelected(id);
    setMessage(null);
    try {
      const result = await diffFileCheckpoint(id);
      setDiff(result.unified || t('code.checkpoints.noDiff'));
    } catch {
      setDiff(t('code.checkpoints.noDiff'));
    }
  }, [t]);

  const restore = useCallback(async (id: string, all: boolean) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await restoreFileCheckpoint(id, { all });
      setMessage(
        `${t('code.checkpoints.restored')}: ${result.restored.length}. ` +
          `${t('code.checkpoints.undoPoint')}: ${result.preRestoreId ?? '—'}`,
      );
      await refresh();
    } catch {
      setMessage(t('code.checkpoints.restoreFailed'));
    } finally {
      setBusy(false);
    }
  }, [refresh, t]);

  if (!items.length && !message) {
    return (
      <section className="glass-card rounded-xl p-3">
        <h3 className="text-[12px] font-medium text-white/80">{t('code.checkpoints.title')}</h3>
        <p className="mt-1 text-[11px] text-white/50">{t('code.checkpoints.empty')}</p>
      </section>
    );
  }

  return (
    <section className="glass-card rounded-xl p-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[12px] font-medium text-white/80">{t('code.checkpoints.title')}</h3>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy || !items.length}
            onClick={async () => {
              if (await confirmDialog({ title: t('code.checkpoints.undoTurnConfirm'), tone: 'danger' })) {
                void restore(items[0].id, true);
              }
            }}
            className="rounded-lg border border-amber-400/30 px-2 py-1 text-[11px] text-amber-200/80 hover:text-amber-100 disabled:opacity-50"
          >
            {t('code.checkpoints.undoTurn')}
          </button>
          <button
            type="button"
            onClick={() => void refresh()}
            className="rounded-lg border border-white/10 px-2 py-1 text-[11px] text-white/60 hover:text-white"
          >
            {t('code.checkpoints.refresh')}
          </button>
        </div>
      </div>
      <ul className="mt-2 space-y-1">
        {items.map((item) => (
          <li key={item.id} className="flex flex-wrap items-center gap-2 text-[11px]">
            <button
              type="button"
              onClick={() => void showDiff(item.id)}
              className="rounded-md bg-white/5 px-2 py-1 font-mono text-white/70 hover:bg-white/10"
            >
              {item.id}
            </button>
            <span className="text-white/40">{item.reason ?? 'before-write'}</span>
            <span className="text-white/30">{item.files ?? 0} files</span>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                if (await confirmDialog({ title: t('code.checkpoints.confirm'), tone: 'danger' })) {
                  void restore(item.id, true);
                }
              }}
              className="ml-auto rounded-md border border-amber-400/30 px-2 py-1 text-amber-200/80 hover:text-amber-100 disabled:opacity-50"
            >
              {t('code.checkpoints.restore')}
            </button>
          </li>
        ))}
      </ul>
      {selected && (
        <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-black/30 p-2 text-[10px] leading-relaxed text-white/60">
          {diff}
        </pre>
      )}
      {message && <p className="mt-2 text-[11px] text-white/60">{message}</p>}
    </section>
  );
}
