// ── Engine transaction → view model ──────────────────────────────────────────
// Pure: no React, no fetch, no client-only imports, so the mapping can be
// unit-tested in isolation.
//
// The Python engine stores many event kinds (splits, corporate actions,
// crypto, fees, taxes, funding). The old adapter kept only five and returned
// `null` for everything else, which silently corrupted the displayed book.
// This adapter preserves every row. The client preview engine only acts on
// BUY/SELL/DIVIDEND, so unknown types are harmless there — but they are never
// dropped from the view.

import type { Transaction, TxType } from './types';

export interface EngineTransactionRow {
  id: string;
  type: string;
  symbol?: string | null;
  quantity?: string | number | null;
  price?: string | number | null;
  grossAmount?: string | number | null;
  netAmount?: string | number | null;
  fees?: string | number | null;
  fee?: string | number | null;
  taxes?: string | number | null;
  tax?: string | number | null;
  tradeDate?: string | null;
  createdAt?: string | null;
  notes?: string | null;
}

export function toNumber(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(String(value ?? '').replace(/[,\s]/g, ''));
  return Number.isFinite(n) ? n : 0;
}

/**
 * Cash-only events: the engine records them with a signed gross amount rather
 * than quantity × price. They still belong in the view because fees, taxes,
 * interest and funding move realized P&L and cash.
 */
export const CASH_EVENT_TYPES: ReadonlySet<string> = new Set([
  'DEPOSIT',
  'WITHDRAWAL',
  'DIVIDEND',
  'INTEREST',
  'FEE',
  'TAX',
  'FUNDING',
  'GAS_FEE',
]);

export function isCashEventType(rawType: string): boolean {
  return CASH_EVENT_TYPES.has(String(rawType || '').toUpperCase());
}

/**
 * Convert one engine row to the app's `Transaction` view. Every row is
 * returned; an unknown engine type keeps its own name rather than being
 * relabelled as something it is not.
 */
export function engineTransactionToView(tx: EngineTransactionRow): Transaction {
  const rawType = String(tx.type || '').toUpperCase();
  const type = (rawType || 'ADJUSTMENT') as TxType;
  const gross = toNumber(tx.grossAmount ?? tx.netAmount);
  const fee = toNumber(tx.fees ?? tx.fee);
  const taxes = toNumber(tx.taxes ?? tx.tax);
  const date = String(tx.tradeDate || tx.createdAt || new Date().toISOString());

  if (isCashEventType(rawType)) {
    // Dividends: the preview engine derives income from quantity × price, so
    // map the settled gross amount to 1 × gross. The same convention keeps a
    // fee/tax/interest line readable without inventing a price.
    const isDividend = rawType === 'DIVIDEND';
    return {
      id: String(tx.id),
      type,
      symbol: String(tx.symbol || (rawType === 'DEPOSIT' || rawType === 'WITHDRAWAL' ? 'CASH' : 'CASH')).toUpperCase(),
      quantity: isDividend ? 1 : (rawType === 'DEPOSIT' || rawType === 'WITHDRAWAL' ? (gross || toNumber(tx.quantity)) : 1),
      price: isDividend ? gross : 1,
      fee,
      taxes,
      grossAmount: gross,
      date,
      notes: tx.notes || undefined,
    };
  }

  return {
    id: String(tx.id),
    type,
    symbol: String(tx.symbol || '').toUpperCase(),
    quantity: toNumber(tx.quantity),
    price: toNumber(tx.price),
    fee,
    taxes,
    grossAmount: gross,
    date,
    notes: tx.notes || undefined,
  };
}
