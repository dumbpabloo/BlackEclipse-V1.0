'use client';

// ── Portfolio screen — engine-backed funds only ──────────────────────────────
// One user-facing object: a fund. The Python Portfolio Accounting Engine is the
// durable book (transactions, lots, valuation); this screen reads and mutates it
// through the front door. The old localStorage fund book is migrated once, then
// only used as the source for that migration.

import React, { useCallback, useEffect, useState } from 'react';
import { ChartPie } from '@/lib/icons';
import { loadFunds } from './fund-store';
import { migrateFundsToEngine } from '@/lib/portfolio/migrate-to-engine';
import {
  addEngineFundTransaction,
  createEngineFund,
  deleteEngineFund,
  importEngineFunds,
  listEngineFunds,
  updateEngineFund,
} from '@/lib/portfolio/engine-funds';
import { FundManager } from './fund-manager';
import { FundWorkspace } from './fund-workspace';
import { useCommandStore } from '@/lib/search/store';
import type { Fund, Transaction } from '@/lib/portfolio/types';

export function PortfolioScreen() {
  const [funds, setFunds] = useState<Fund[] | null>(null);
  const [openFundId, setOpenFundId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await listEngineFunds();
      setFunds(next);
      setError(null);
    } catch (err) {
      setFunds((current) => current ?? []);
      setError(err instanceof Error ? err.message : 'fund engine unavailable');
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // One-time migration of the legacy localStorage book into the engine.
      try {
        const legacy = loadFunds().funds;
        if (legacy.length) await migrateFundsToEngine(legacy);
      } catch {
        /* migration is best-effort; the engine read below reports availability */
      }
      if (!cancelled) await refresh();
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  // Consume a fund id dispatched by the global search bar (⌘K → fund result).
  useEffect(() => {
    const check = () => {
      const id = useCommandStore.getState().consumeFundId();
      if (id) setOpenFundId(id);
    };
    check();
    const unsub = useCommandStore.subscribe((state, prev) => {
      if (state.pendingFundId !== prev.pendingFundId && state.pendingFundId) check();
    });
    return unsub;
  }, []);

  const openFund = funds?.find((fund) => fund.id === openFundId) ?? null;

  const handleCreate = useCallback(async (input: {
    name: string; icon: string; accent: string; goalValue: number | null; startingCash: number;
  }) => {
    try {
      await createEngineFund(input);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'create fund failed');
    }
  }, [refresh]);

  const handleUpdate = useCallback(async (
    fundId: string,
    patch: {
      name?: string;
      goalValue?: number | null;
      costBasisMethod?: Fund['costBasisMethod'];
    },
  ) => {
    try {
      await updateEngineFund(fundId, patch);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'update fund failed');
    }
  }, [refresh]);

  const handleDelete = useCallback(async (fundId: string) => {
    try {
      await deleteEngineFund(fundId);
      setOpenFundId((current) => (current === fundId ? null : current));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'delete fund failed');
    }
  }, [refresh]);

  const handleAddTransaction = useCallback(async (fund: Fund, tx: Transaction) => {
    try {
      await addEngineFundTransaction(fund, tx);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'add transaction failed');
    }
  }, [refresh]);

  const handleImport = useCallback(async (imported: Fund[]) => {
    try {
      const result = await importEngineFunds(imported);
      if (result.errors.length) setError(result.errors.join(' · '));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'import failed');
    }
  }, [refresh]);

  return (
    <div className="relative flex h-full w-full flex-col">
      {error && (
        <div className="mb-3 rounded-xl border border-rose-500/25 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          Fund engine: {error}
        </div>
      )}
      <div className="min-h-0 flex-1">
        {openFund ? (
          <FundWorkspace
            key={openFund.id}
            fund={openFund}
            onBack={() => setOpenFundId(null)}
            onAddTransaction={(tx) => handleAddTransaction(openFund, tx)}
            onSetCostBasis={(method) => handleUpdate(openFund.id, { costBasisMethod: method })}
          />
        ) : (
          <FundManager
            funds={funds}
            onOpenFund={setOpenFundId}
            onCreate={handleCreate}
            onUpdate={handleUpdate}
            onDelete={handleDelete}
            onAddTransaction={(fundId, tx) => {
              const fund = funds?.find((f) => f.id === fundId);
              if (fund) return handleAddTransaction(fund, tx);
            }}
            onImport={handleImport}
          />
        )}
      </div>
    </div>
  );
}

export { ChartPie as PortfolioTabIcon };
