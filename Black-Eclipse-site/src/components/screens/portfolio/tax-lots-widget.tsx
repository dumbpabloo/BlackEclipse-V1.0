'use client';

// ── Tax lots & realized gains ────────────────────────────────────────────────
// Open tax lots and realized lot matches, read straight from the engine's
// `/portfolio/{id}/tax-lots`. This is the report the engine always computed
// but the UI never showed: per-disposal proceeds, basis, realized gain and
// holding period, plus a CSV export for a tax preparer.

import { Layers } from '@/lib/icons';
import { cn } from '@/lib/utils';
import { EmptyNote, Tile, WidgetHeader, type WidgetProps } from './widget-primitives';
import type { TaxLotMatchView, TaxLotView } from '@/lib/harness/clients/portfolio-client';

interface RealizedRow extends TaxLotMatchView {
  symbol: string;
}

interface OpenLotRow extends TaxLotView {
  symbol: string;
}

function nativeMoney(value: string | number, currency = 'USD'): string {
  const n = typeof value === 'number' ? value : Number(String(value).replace(/[,\s]/g, ''));
  if (!Number.isFinite(n)) return '—';
  return `${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;
}

function csvCell(value: unknown): string {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Download the engine's realized lot matches — the report a tax preparer needs. */
function downloadRealizedGains(
  rows: RealizedRow[],
  currencyFor: Map<string, string>,
  base: string,
): void {
  const header = ['disposed_at', 'symbol', 'quantity', 'proceeds', 'cost_basis', 'realized_pnl', 'holding_days', 'currency'];
  const body = rows.map((row) => [
    String(row.disposedAt ?? '').slice(0, 10),
    row.symbol,
    row.quantity,
    row.proceeds,
    row.costBasis,
    row.realizedPnl,
    row.holdingDays ?? '',
    currencyFor.get(row.symbol) ?? base,
  ]);
  const csv = [header, ...body].map((line) => line.map(csvCell).join(',')).join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `realized-gains-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

export function TaxLotsWidget({ fund, data }: WidgetProps) {
  const lots = data.taxLots;
  const base = fund.baseCurrency || 'USD';
  if (!lots) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader icon={Layers} title="Tax Lots & Realized Gains" />
        <EmptyNote>{data.taxLotsError ?? 'Waiting for the portfolio engine…'}</EmptyNote>
      </div>
    );
  }

  const currencyFor = new Map<string, string>();
  for (const symbol of lots.symbols) {
    const currency = symbol.openLots.find((lot) => lot.currency)?.currency;
    if (currency) currencyFor.set(symbol.symbol, currency);
  }

  const openLots: OpenLotRow[] = lots.symbols.flatMap((symbol) =>
    symbol.openLots.map((lot) => ({ ...lot, symbol: symbol.symbol })),
  );
  const matches: RealizedRow[] = lots.symbols
    .flatMap((symbol) => symbol.matches.map((match) => ({ ...match, symbol: symbol.symbol })))
    .sort((a, b) => String(b.disposedAt).localeCompare(String(a.disposedAt)));
  const realizedTotal = lots.totalRealizedPnl != null
    ? Number(lots.totalRealizedPnl)
    : matches.reduce((sum, m) => sum + (Number(m.realizedPnl) || 0), 0);

  // Group by tax year AND currency: summing EUR and USD gains into one number
  // would be a fabricated figure, so each currency stays separate.
  const byYearCurrency = new Map<string, number>();
  for (const match of matches) {
    const year = String(match.disposedAt).slice(0, 4) || '—';
    const currency = currencyFor.get(match.symbol) ?? base;
    const key = `${year} · ${currency}`;
    byYearCurrency.set(key, (byYearCurrency.get(key) ?? 0) + (Number(match.realizedPnl) || 0));
  }

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader
        icon={Layers}
        title="Tax Lots & Realized Gains"
        hint={`${lots.method} · ${openLots.length} open`}
      />

      <div className="mb-3 grid grid-cols-2 gap-2.5 lg:grid-cols-4 shrink-0">
        <Tile label="Method" value={lots.method.replace('_', ' ')} sub="saved on fund" />
        <Tile label="Open Lots" value={String(openLots.length)} sub={`${lots.symbols.length} symbols`} />
        <Tile
          label="Realized"
          value={nativeMoney(realizedTotal, base)}
          tone={realizedTotal > 0 ? 'text-emerald-400' : realizedTotal < 0 ? 'text-rose-400' : undefined}
          sub="engine lot matches"
        />
        <Tile label="Disposals" value={String(matches.length)} sub="all time" />
      </div>

      {byYearCurrency.size > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5 shrink-0">
          {[...byYearCurrency.entries()].sort((a, b) => b[0].localeCompare(a[0])).map(([key, total]) => (
            <span
              key={key}
              className={cn(
                'rounded-lg border px-2 py-1 font-mono text-[9px] font-bold',
                total > 0
                  ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-300'
                  : total < 0 ? 'border-rose-400/25 bg-rose-400/10 text-rose-300' : 'border-border/30 bg-accent/20 text-muted-foreground',
              )}
            >
              {key}: {total.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </span>
          ))}
        </div>
      )}

      {matches.length > 0 && (
        <div className="mb-2 flex shrink-0 justify-end">
          <button
            type="button"
            onClick={() => downloadRealizedGains(matches, currencyFor, base)}
            className="glass-button rounded-lg px-2.5 py-1 font-mono text-[10px] font-bold text-muted-foreground transition-colors hover:text-primary"
            title="Download realized lot matches as CSV (native currency)"
          >
            Export realized gains CSV
          </button>
        </div>
      )}

      <div className="flex-1 overflow-auto custom-scrollbar rounded-xl glass-card-subtle">
        {matches.length === 0 ? (
          <p className="p-4 text-xs italic text-muted-foreground/70">
            No realized disposals yet. Open lots are listed below.
          </p>
        ) : (
          <table className="w-full text-left border-separate border-spacing-0 text-xs">
            <thead className="sticky top-0 z-10">
              <tr className="glass-card-subtle">
                {['Disposed', 'Symbol', 'Qty', 'Proceeds', 'Basis', 'Gain', 'Held'].map((h, i) => (
                  <th
                    key={h}
                    className={cn(
                      'border-b border-white/10 py-2.5 px-3 text-[9px] uppercase tracking-widest text-muted-foreground/80 font-mono font-semibold whitespace-nowrap',
                      i <= 1 ? 'text-left' : 'text-right',
                    )}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="font-mono">
              {matches.slice(0, 40).map((match) => {
                const currency = currencyFor.get(match.symbol) ?? base;
                const gain = Number(match.realizedPnl) || 0;
                return (
                  <tr key={match.id} className="transition-colors hover:bg-white/[0.05]">
                    <td className="border-b border-white/5 py-2 px-3 text-muted-foreground whitespace-nowrap">
                      {String(match.disposedAt).slice(0, 10)}
                    </td>
                    <td className="border-b border-white/5 py-2 px-3 font-bold text-foreground whitespace-nowrap">{match.symbol}</td>
                    <td className="border-b border-white/5 py-2 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{Number(match.quantity).toFixed(4)}</td>
                    <td className="border-b border-white/5 py-2 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{nativeMoney(match.proceeds, currency)}</td>
                    <td className="border-b border-white/5 py-2 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{nativeMoney(match.costBasis, currency)}</td>
                    <td className={cn('border-b border-white/5 py-2 px-3 text-right font-bold tabular-nums whitespace-nowrap', gain > 0 ? 'text-emerald-400' : gain < 0 ? 'text-rose-400' : 'text-foreground')}>
                      {nativeMoney(match.realizedPnl, currency)}
                    </td>
                    <td className="border-b border-white/5 py-2 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">
                      {match.holdingDays == null ? '—' : `${match.holdingDays}d`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {openLots.length > 0 && (
        <div className="mt-2 max-h-24 shrink-0 overflow-auto custom-scrollbar rounded-xl glass-card-subtle p-2">
          <div className="flex flex-wrap gap-1.5">
            {openLots.slice(0, 24).map((lot) => (
              <span key={lot.id} className="rounded-lg border border-border/30 bg-accent/20 px-2 py-1 font-mono text-[9px] text-muted-foreground">
                {lot.symbol} · {Number(lot.quantity).toFixed(4)} @ {Number(lot.costPerUnit).toFixed(2)} · {String(lot.openedAt).slice(0, 10)}
              </span>
            ))}
          </div>
        </div>
      )}

      <p className="mt-2 shrink-0 font-mono text-[9px] leading-relaxed text-muted-foreground/60">
        Read from the Python engine&apos;s lot ledger. Amounts are in each symbol&apos;s native currency;
        holding days are the engine&apos;s lot-match period, not a tax-jurisdiction determination.
      </p>
    </div>
  );
}
