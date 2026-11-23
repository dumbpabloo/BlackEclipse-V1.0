'use client';

// ── Legacy fund store (migration source) ─────────────────────────────────────
// The live Portfolio screen is engine-backed (`engine-funds.ts`). This module is
// retained for the one-time localStorage → engine migration, the fund style
// constants, and import/export helpers. Seeded demo funds are protected from
// deletion only in this legacy book.

import {
  Briefcase, Car, Coins, Flame, Gem, GraduationCap, Heart, Home, Landmark,
  PiggyBank, Plane, Rocket, Shield, Sprout, Target, TrendingUp,
} from '@/lib/icons';
import { saveBlob } from '@/lib/desktop/download';
import type { ComponentType } from 'react';
import { newTxId, type Fund, type Transaction } from '@/lib/portfolio/types';

const FUNDS_KEY = 'be_portfolio_funds_v1';

export const FUND_ICONS = [
  'Briefcase', 'Rocket', 'Shield', 'Coins', 'Landmark', 'Sprout', 'Gem', 'Flame',
  'Target', 'Home', 'GraduationCap', 'Plane', 'Car', 'Heart', 'PiggyBank', 'TrendingUp',
];

export const FUND_ICON_COMPONENTS: Record<string, ComponentType<{ size?: number; className?: string; style?: React.CSSProperties }>> = {
  Briefcase, Rocket, Shield, Coins, Landmark, Sprout, Gem, Flame,
  Target, Home, GraduationCap, Plane, Car, Heart, PiggyBank, TrendingUp,
};
export const FUND_ACCENTS = ['#34d399', '#60a5fa', '#fbbf24', '#f472b6', '#a78bfa', '#fb7185', '#2dd4bf', '#f97316', '#4ade80', '#e879f9', '#38bdf8', '#facc15'];

// ── Seeded demo book ─────────────────────────────────────────────────────────

function demoTxs(rows: Array<[Transaction['type'], string, number, number, number, string]>): Transaction[] {
  return rows.map(([type, symbol, quantity, price, fee, date]) => ({
    id: newTxId(), type, symbol, quantity, price, fee, date,
  }));
}

function demoFunds(): Fund[] {
  return [
    {
      id: 'demo-core-growth',
      name: 'Core Growth',
      icon: 'Rocket',
      accent: FUND_ACCENTS[0],
      goalValue: 100000,
      createdAt: new Date('2026-01-15').toISOString(),
      seeded: true,
      transactions: demoTxs([
        ['BUY', 'AAPL', 40, 178.2, 1, '2026-02-03'],
        ['BUY', 'MSFT', 25, 402.5, 1, '2026-02-10'],
        ['BUY', 'NVDA', 60, 118.4, 1, '2026-03-05'],
        ['BUY', 'BTC', 0.15, 66500, 10, '2026-03-18'],
        ['DIVIDEND', 'AAPL', 40, 0.25, 0, '2026-05-12'],
      ]),
    },
    {
      id: 'demo-dividend-income',
      name: 'Dividend Income',
      icon: 'Shield',
      accent: FUND_ACCENTS[2],
      goalValue: 50000,
      createdAt: new Date('2026-02-01').toISOString(),
      seeded: true,
      transactions: demoTxs([
        ['BUY', 'KO', 120, 61.3, 1, '2026-02-20'],
        ['BUY', 'JNJ', 45, 152.8, 1, '2026-02-25'],
        ['DIVIDEND', 'KO', 120, 0.485, 0, '2026-04-01'],
        ['DIVIDEND', 'JNJ', 45, 1.24, 0, '2026-03-05'],
        ['DEPOSIT', 'CASH', 5000, 1, 0, '2026-02-01'],
      ]),
    },
    {
      id: 'demo-tech-satellite',
      name: 'Tech Satellite',
      icon: 'Gem',
      accent: FUND_ACCENTS[3],
      goalValue: 75000,
      createdAt: new Date('2026-03-10').toISOString(),
      seeded: true,
      transactions: demoTxs([
        ['BUY', 'AMD', 60, 148.5, 1, '2026-03-12'],
        ['BUY', 'META', 20, 492.4, 1, '2026-03-16'],
        ['BUY', 'GOOGL', 30, 172.5, 1, '2026-04-02'],
      ]),
    },
    {
      id: 'demo-psx-value',
      name: 'PSX Value',
      icon: 'Landmark',
      accent: FUND_ACCENTS[6],
      createdAt: new Date('2026-01-20').toISOString(),
      seeded: true,
      transactions: demoTxs([
        ['BUY', 'UBL', 100, 322.0, 45, '2026-01-22'],
        ['BUY', 'MEBL', 80, 188.5, 40, '2026-02-04'],
        ['BUY', 'OGDC', 200, 214.0, 60, '2026-03-08'],
        ['DIVIDEND', 'UBL', 100, 12.0, 0, '2026-05-15'],
      ]),
    },
  ];
}

// ── Load / save ──────────────────────────────────────────────────────────────

const SEED_VERSION = 2;
const SEED_FLAG_KEY = 'be_portfolio_seed_v';

/**
 * Loads the fund book and migrates seeded demo content. When the stored book
 * predates the current seed version, any demo funds the user does not yet
 * have are appended once (existing funds and deletions of non-demo funds are
 * always respected).
 */
export function loadFunds(): { funds: Fund[]; seeded: boolean } {
  let stored: Fund[] | null = null;
  try {
    const raw = localStorage.getItem(FUNDS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        stored = parsed.filter((f): f is Fund =>
          f && typeof f === 'object' && typeof f.id === 'string' && typeof f.name === 'string' && Array.isArray(f.transactions));
      }
    }
  } catch { /* corrupt storage → reseed below */ }

  let seedVersion = 0;
  try { seedVersion = Number(localStorage.getItem(SEED_FLAG_KEY)) || 0; } catch { /* ignore */ }

  if (stored === null) {
    const funds = demoFunds();
    try {
      localStorage.setItem(FUNDS_KEY, JSON.stringify(funds));
      localStorage.setItem(SEED_FLAG_KEY, String(SEED_VERSION));
    } catch { /* ignore */ }
    return { funds, seeded: false };
  }

  let funds = stored;
  if (seedVersion < SEED_VERSION) {
    const missing = demoFunds().filter(d => !funds.some(f => f.id === d.id));
    if (missing.length > 0) funds = [...funds, ...missing];
    try {
      localStorage.setItem(FUNDS_KEY, JSON.stringify(funds));
      localStorage.setItem(SEED_FLAG_KEY, String(SEED_VERSION));
    } catch { /* ignore */ }
  }
  return { funds, seeded: true };
}

export function saveFunds(funds: Fund[]): void {
  try { localStorage.setItem(FUNDS_KEY, JSON.stringify(funds)); } catch { /* ignore */ }
  // Notify any same-tab listeners (Command Center portfolio widgets) so they
  // re-read the book without requiring a reload.
  try { window.dispatchEvent(new CustomEvent(FUNDS_CHANGED_EVENT)); } catch { /* ignore */ }
}

/** Event fired whenever the guest fund book is written. */
export const FUNDS_CHANGED_EVENT = 'be:funds-changed';
export const FUNDS_STORAGE_KEY = FUNDS_KEY;

export function newFund(name: string, icon: string, accent: string, goalValue?: number | null): Fund {
  return {
    id: `fund-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
    name, icon, accent,
    goalValue: goalValue ?? null,
    createdAt: new Date().toISOString(),
    transactions: [],
  };
}

export const isDemoFund = (f: Fund) => Boolean(f.seeded);

// ── Cash tracking (engine skips DEPOSIT/WITHDRAWAL) ──────────────────────────

export function computeCash(transactions: Transaction[]): number {
  return transactions.reduce((acc, tx) => {
    if (tx.type === 'DEPOSIT') return acc + tx.quantity * tx.price;
    if (tx.type === 'WITHDRAWAL') return acc - tx.quantity * tx.price;
    if (tx.type === 'BUY') return acc - (tx.quantity * tx.price + tx.fee);
    if (tx.type === 'SELL') return acc + (tx.quantity * tx.price - tx.fee);
    return acc; // dividends are engine-side income, not cash deposits
  }, 0);
}

// ── Export / import (JSON + CSV) ─────────────────────────────────────────────

function triggerDownload(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  saveBlob(filename, blob);
}

const csvEscape = (v: unknown) => {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

export function exportFundJson(fund: Fund) {
  triggerDownload(JSON.stringify(fund, null, 2), `${fund.name.replace(/\W+/g, '-')}.json`, 'application/json');
}

export function exportFundCsv(fund: Fund) {
  const header = 'id,type,symbol,quantity,price,fee,date,notes';
  const rows = fund.transactions.map(t =>
    [t.id, t.type, t.symbol, t.quantity, t.price, t.fee, t.date, t.notes ?? ''].map(csvEscape).join(','));
  triggerDownload([header, ...rows].join('\n'), `${fund.name.replace(/\W+/g, '-')}.csv`, 'text/csv');
}

export function exportAllFundsJson(funds: Fund[]) {
  triggerDownload(JSON.stringify({ version: 1, funds }, null, 2), 'black-eclipse-funds.json', 'application/json');
}

export interface ImportResult {
  funds: Fund[];
  errors: string[];
}

function txFromCsvRow(r: Record<string, string>): Transaction | null {
  const type = (r.type || '').toUpperCase() as Transaction['type'];
  if (!['BUY', 'SELL', 'DEPOSIT', 'WITHDRAWAL', 'DIVIDEND'].includes(type)) return null;
  if (!r.symbol) return null;
  return {
    id: r.id || newTxId(),
    type,
    symbol: String(r.symbol).toUpperCase(),
    quantity: Number(r.quantity) || 0,
    price: Number(r.price) || 0,
    fee: Number(r.fee) || 0,
    date: r.date || new Date().toISOString().slice(0, 10),
    notes: r.notes || undefined,
  };
}

/** Parse a JSON or CSV payload into funds. Never throws. */
export function parseFundsPayload(text: string): ImportResult {
  const errors: string[] = [];
  try {
    if (text.trim().startsWith('{') || text.trim().startsWith('[')) {
      const parsed = JSON.parse(text);
      const arr = Array.isArray(parsed) ? parsed : Array.isArray(parsed.funds) ? parsed.funds : [parsed];
      const funds: Fund[] = [];
      for (const f of arr) {
        if (!f || typeof f !== 'object' || !f.name || !Array.isArray(f.transactions)) {
          errors.push('Skipped an entry without a name/transactions.');
          continue;
        }
        funds.push({
          ...newFund(String(f.name), FUND_ICONS.includes(f.icon) ? f.icon : FUND_ICONS[0],
            /^#[0-9a-fA-F]{6}$/.test(f.accent) ? f.accent : FUND_ACCENTS[0]),
          id: typeof f.id === 'string' ? f.id : `fund-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
          transactions: f.transactions.filter((t: unknown): t is Transaction =>
            Boolean(t) && typeof (t as Transaction).symbol === 'string'),
          goalValue: typeof f.goalValue === 'number' ? f.goalValue : null,
        });
      }
      return { funds, errors };
    }
    // CSV — Fincept-style ledger with a header row.
    const lines = text.trim().split(/\r?\n/);
    if (lines.length < 2) return { funds: [], errors: ['CSV is empty.'] };
    const header = lines[0].split(',').map(h => h.trim().toLowerCase());
    const rows = lines.slice(1).map(line => {
      const cells: string[] = [];
      let cur = '', inQ = false;
      for (const ch of line) {
        if (ch === '"') inQ = !inQ;
        else if (ch === ',' && !inQ) { cells.push(cur); cur = ''; }
        else cur += ch;
      }
      cells.push(cur);
      const rec: Record<string, string> = {};
      header.forEach((h, i) => { rec[h] = (cells[i] ?? '').trim(); });
      return rec;
    });
    const txs = rows.map(txFromCsvRow).filter((t): t is Transaction => t !== null);
    if (txs.length === 0) return { funds: [], errors: ['No valid transactions found in CSV.'] };
    const name = rows[0].fund || 'Imported Fund';
    return { funds: [{ ...newFund(name, FUND_ICONS[0], FUND_ACCENTS[0]), transactions: txs }], errors };
  } catch (e) {
    return { funds: [], errors: [e instanceof Error ? e.message : 'Failed to parse file.'] };
  }
}

