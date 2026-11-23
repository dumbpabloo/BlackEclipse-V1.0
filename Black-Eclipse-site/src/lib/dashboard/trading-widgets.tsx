'use client';

// ── Broker-backed dashboard widgets ─────────────────────────────────────────
// These replace the old UNAVAILABLE trading stubs. They read the SAME
// `/api/trading` connection the Settings panel owns; a missing connection renders
// an honest setup note, never fake positions or fills.

import React, { useCallback, useEffect, useState } from 'react';
import { confirmDialog, promptDialog } from '@/lib/ui/confirm-dialog';
import { cn } from '@/lib/utils';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  cancelBrokerOrder,
  closeAllBrokerPositions,
  closeBrokerPosition,
  decideTradeApproval,
  listLedgerOrders,
  modifyBrokerPosition,
  previewOrder,
  submitOrder,
  type BrokerPosition,
  type BrokerWorkingOrder,
  type LedgerOrder,
  type OrderIntent,
  type OrderPreview,
  type TradeApproval,
} from '@/lib/harness/clients/trading-client';

const UP = 'text-emerald-400';
const DOWN = 'text-rose-400';
const fmt = (n?: number | null, d = 2) =>
  n == null || !Number.isFinite(n)
    ? '—'
    : n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const tone = (n: number) => (n > 0 ? UP : n < 0 ? DOWN : 'text-muted-foreground');

import { useBrokerSnapshot, invalidateBrokerSnapshot, type BrokerSnapshotState } from '@/lib/dashboard/use-broker-snapshot';

type TradingData = BrokerSnapshotState;
const useTradingData = useBrokerSnapshot;
/** Drop the shared read cache after an action so the next read is fresh. */
const invalidateTradingSnapshot = invalidateBrokerSnapshot;

function WidgetHeader({ title, hint }: { title: string; hint?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border/10 pb-2.5 mb-3">
      <h3 className="truncate font-serif text-sm font-semibold text-foreground">{title}</h3>
      {hint != null && <span className="shrink-0 font-mono text-[9px] text-muted-foreground/70">{hint}</span>}
    </div>
  );
}

function ConnectNote() {
  return (
    <div className="flex flex-1 items-center justify-center text-center">
      <p className="max-w-[26ch] text-xs italic text-muted-foreground">
        No broker connection. Open <span className="not-italic font-medium text-foreground">Settings → Broker connection</span>{' '}
        and add MetaTrader 5.
      </p>
    </div>
  );
}

function ErrorNote({ message }: { message: string }) {
  return (
    <div className="flex flex-1 items-center justify-center text-center">
      <p className="max-w-[30ch] text-xs text-amber-400/90">{message}</p>
    </div>
  );
}

function Badge({ children, tone: color }: { children: React.ReactNode; tone?: string }) {
  return (
    <span className={cn('rounded border border-border/20 px-1.5 py-0.5 font-mono text-[9px] uppercase', color)}>
      {children}
    </span>
  );
}

// ── Open positions ───────────────────────────────────────────────────────────
export function BrokerOpenPositionsWidget() {
  const { connection, positions, error, loading, refresh } = useTradingData();
  const [notice, setNotice] = useState<string | null>(null);
  const close = async (position: BrokerPosition) => {
    if (!connection) return;
    const raw = await promptDialog({
      title: `Close how much of #${position.ticket}?`,
      description: `Blank closes the whole ${position.volume} lot.`,
      inputType: 'number',
    });
    if (raw === null) return;
    const volume = raw.trim() === '' ? undefined : Number(raw);
    if (volume !== undefined && !Number.isFinite(volume)) return;
    if (!(await confirmDialog({ title: `Close ${volume ?? position.volume} of #${position.ticket} at market?`, tone: 'danger' }))) return;
    try {
      const result = await closeBrokerPosition(connection.id, position.ticket, volume);
      const detail = result.result as { partial?: boolean; remainingVolume?: number; verified?: boolean };
      setNotice(
        `Closed #${position.ticket}${detail.partial ? `; ${detail.remainingVolume} still open` : ''}` +
          `${detail.verified === false ? ' — broker did not read back as expected' : ''}`,
      );
      invalidateTradingSnapshot();
      await refresh();
    } catch {
      /* the next refresh reports the broker's state */
    }
  };
  const modify = async (position: BrokerPosition) => {
    if (!connection) return;
    const slRaw = await promptDialog({
      title: `New stop loss for #${position.ticket}`,
      description: `Blank keeps ${position.stopLoss ?? 'none'}, 0 removes it.`,
      inputType: 'number',
    });
    if (slRaw === null) return;
    const tpRaw = await promptDialog({
      title: `New take profit for #${position.ticket}`,
      description: `Blank keeps ${position.takeProfit ?? 'none'}, 0 removes it.`,
      inputType: 'number',
    });
    if (tpRaw === null) return;
    const levels: { stopLoss?: number; takeProfit?: number } = {};
    if (slRaw.trim() !== '') levels.stopLoss = Number(slRaw);
    if (tpRaw.trim() !== '') levels.takeProfit = Number(tpRaw);
    if (Object.keys(levels).length === 0) return;
    try {
      const result = await modifyBrokerPosition(connection.id, position.ticket, levels);
      const detail = result.result as { stopLoss?: number; takeProfit?: number; verified?: boolean };
      setNotice(
        `#${position.ticket} bracket: SL ${detail.stopLoss ?? '—'} / TP ${detail.takeProfit ?? '—'}` +
          `${detail.verified === false ? ' (unverified)' : ''}`,
      );
      invalidateTradingSnapshot();
      await refresh();
    } catch {
      /* the next refresh reports the broker's state */
    }
  };
  const closeAll = async () => {
    if (!connection || positions.length === 0) return;
    if (!(await confirmDialog({ title: `Close ALL ${positions.length} open position(s) at market?`, tone: 'danger' }))) return;
    try {
      await closeAllBrokerPositions(connection.id);
      invalidateTradingSnapshot();
      await refresh();
    } catch {
      /* the next refresh reports the broker's state */
    }
  };
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader title="Open Positions" hint={loading ? '…' : `${positions.length}`} />
      {!connection ? <ConnectNote /> : error ? <ErrorNote message={error} /> : positions.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-xs text-muted-foreground">No open positions.</p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto scrollbar-hide">
          {notice && <p className="pb-1 text-[9px] text-emerald-300/90">{notice}</p>}
          <div className="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-2 pb-1 text-[9px] uppercase tracking-widest text-muted-foreground">
            <span>Symbol</span><span>Vol</span><span>Open</span><span className="text-right">P&L</span><span /><span />
          </div>
          {positions.map((position) => (
            <div
              key={position.ticket}
              className="grid grid-cols-[1fr_auto_auto_auto_auto_auto] items-center gap-2 border-t border-border/10 py-1.5 text-[10px]"
            >
              <span className="min-w-0">
                <span className="block truncate font-mono font-bold text-foreground">{position.symbol}</span>
                <span className={cn('text-[9px] uppercase', position.side === 'buy' ? UP : DOWN)}>
                  {position.side} <span className="text-muted-foreground/70">#{position.ticket}</span>
                </span>
              </span>
              <span className="font-mono text-muted-foreground">{position.volume}</span>
              <span className="font-mono text-muted-foreground">{fmt(position.openPrice, 5)}</span>
              <span className={cn('text-right font-mono font-bold', tone(position.profit))}>
                {fmt(position.profit)}
              </span>
              <button
                type="button"
                onClick={() => void modify(position)}
                title={`Modify stop/target of #${position.ticket}`}
                className="rounded border border-border/20 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-muted-foreground hover:border-primary/40 hover:text-primary"
              >
                SL/TP
              </button>
              <button
                type="button"
                onClick={() => void close(position)}
                title={`Close position #${position.ticket}`}
                className="rounded border border-border/20 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-muted-foreground hover:border-rose-400/40 hover:text-rose-300"
              >
                Close
              </button>
            </div>
          ))}
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => void refresh()}
              className="flex-1 rounded-lg border border-border/20 py-1 text-[9px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
            >
              Refresh
            </button>
            <button
              type="button"
              onClick={() => void closeAll()}
              className="flex-1 rounded-lg border border-rose-400/30 py-1 text-[9px] uppercase tracking-widest text-rose-300/90 hover:border-rose-400/60 hover:text-rose-200"
            >
              Close all
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Working orders ───────────────────────────────────────────────────────────
export function BrokerWorkingOrdersWidget() {
  const { connection, orders, error, loading, refresh } = useTradingData();
  const cancel = async (ticket: number | string) => {
    if (!connection) return;
    if (!(await confirmDialog({ title: `Cancel working order #${ticket}?`, tone: 'danger' }))) return;
    try {
      await cancelBrokerOrder(connection.id, ticket);
      invalidateTradingSnapshot();
      await refresh();
    } catch {
      /* the next refresh reports the broker's state */
    }
  };
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader title="Working Orders" hint={loading ? '…' : `${orders.length}`} />
      {!connection ? <ConnectNote /> : error ? <ErrorNote message={error} /> : orders.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-xs text-muted-foreground">No working orders.</p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto scrollbar-hide">
          {orders.map((order) => (
            <div
              key={order.ticket}
              className="flex items-center justify-between gap-2 border-t border-border/10 py-1.5 text-[10px]"
            >
              <span className="min-w-0">
                <span className="block truncate font-mono font-bold text-foreground">{order.symbol}</span>
                <span className="text-[9px] uppercase text-muted-foreground">
                  {order.type} · {order.volumeCurrent}
                </span>
              </span>
              <span className="font-mono text-muted-foreground">{fmt(order.openPrice, 5)}</span>
              <button
                type="button"
                onClick={() => void cancel(order.ticket)}
                className="rounded border border-rose-500/30 px-1.5 py-0.5 text-[10px] text-rose-300 hover:bg-rose-500/10"
                aria-label={`Cancel order ${order.ticket}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Margin usage ─────────────────────────────────────────────────────────────
export function BrokerMarginUsageWidget() {
  const { connection, account, error, loading } = useTradingData();
  const used = account?.margin ?? 0;
  const equity = account?.equity ?? 0;
  const percent = equity > 0 ? Math.min(100, (used / equity) * 100) : 0;
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader title="Margin Usage" hint={account ? `${account.currency}` : undefined} />
      {!connection ? <ConnectNote /> : error ? <ErrorNote message={error} /> : !account ? (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-xs text-muted-foreground">{loading ? 'Loading…' : 'No account read.'}</p>
        </div>
      ) : (
        <div className="flex flex-1 flex-col justify-center gap-3">
          <div className="flex items-baseline justify-between">
            <span className="font-mono text-2xl font-bold text-foreground">{fmt(percent, 1)}%</span>
            <span className="text-[9px] uppercase tracking-widest text-muted-foreground">used</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-accent/40">
            <div className="h-full rounded-full bg-primary" style={{ width: `${percent}%` }} />
          </div>
          <div className="grid grid-cols-2 gap-2 text-[10px]">
            <div className="rounded-lg border border-border/20 px-2 py-1.5">
              <p className="text-[9px] uppercase text-muted-foreground">Used</p>
              <p className="font-mono text-foreground">{fmt(used)}</p>
            </div>
            <div className="rounded-lg border border-border/20 px-2 py-1.5">
              <p className="text-[9px] uppercase text-muted-foreground">Free</p>
              <p className="font-mono text-foreground">{fmt(account.marginFree)}</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Today P&L (realized from broker deals + floating) ───────────────────────
export function BrokerTodayPnlWidget() {
  const { connection, account, fills, error, loading } = useTradingData();
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const realized = fills
    .filter((fill) => fill.dealtAt >= start.getTime())
    .reduce((sum, fill) => sum + fill.profit + fill.swap + fill.commission, 0);
  const floating = account?.profit ?? 0;
  const total = realized + floating;
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader title="Today P&L" hint={account ? `${account.currency}` : undefined} />
      {!connection ? <ConnectNote /> : error ? <ErrorNote message={error} /> : !account ? (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-xs text-muted-foreground">{loading ? 'Loading…' : 'No account read.'}</p>
        </div>
      ) : (
        <div className="flex flex-1 flex-col justify-center gap-3">
          <div>
            <p className="text-[9px] uppercase tracking-widest text-muted-foreground">Realized + floating</p>
            <p className={cn('font-mono text-3xl font-bold', tone(total))}>
              {total > 0 ? '+' : ''}{fmt(total)}
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2 text-[10px]">
            <div className="rounded-lg border border-border/20 px-2 py-1.5">
              <p className="text-[9px] uppercase text-muted-foreground">Realized</p>
              <p className={cn('font-mono', tone(realized))}>{fmt(realized)}</p>
            </div>
            <div className="rounded-lg border border-border/20 px-2 py-1.5">
              <p className="text-[9px] uppercase text-muted-foreground">Floating</p>
              <p className={cn('font-mono', tone(floating))}>{fmt(floating)}</p>
            </div>
          </div>
          <p className="text-[9px] text-muted-foreground/70">Broker-reported deals + open P&L.</p>
        </div>
      )}
    </div>
  );
}

// ── Recent trades (the local ledger, newest first) ───────────────────────────
export function BrokerRecentTradesWidget() {
  const { connection, error, loading } = useTradingData();
  const [rows, setRows] = useState<LedgerOrder[]>([]);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!connection) {
      setRows([]);
      return;
    }
    setBusy(true);
    try {
      setRows(await listLedgerOrders(connection.id, 8));
      setLedgerError(null);
    } catch (err) {
      setLedgerError(err instanceof Error ? err.message : 'order ledger unavailable');
    } finally {
      setBusy(false);
    }
  }, [connection]);

  useEffect(() => {
    void load();
  }, [load]);

  const statusTone = (status: string) =>
    status === 'filled' || status === 'placed'
      ? UP
      : status === 'unknown'
        ? 'text-amber-300'
        : DOWN;

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader title="Recent Trades" hint={busy ? '…' : `${rows.length}`} />
      {!connection ? <ConnectNote /> : error || ledgerError ? (
        <ErrorNote message={ledgerError ?? error ?? 'order ledger unavailable'} />
      ) : rows.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-xs text-muted-foreground">{loading ? 'Loading…' : 'No submitted orders yet.'}</p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto scrollbar-hide">
          <div className="grid grid-cols-[1fr_auto_auto] gap-2 pb-1 text-[9px] uppercase tracking-widest text-muted-foreground">
            <span>Order</span><span className="text-right">Fill</span><span className="text-right">Status</span>
          </div>
          {rows.map((row) => (
            <div
              key={row.id}
              className="grid grid-cols-[1fr_auto_auto] items-center gap-2 border-t border-border/10 py-1.5 text-[10px]"
            >
              <span className="min-w-0">
                <span className="block truncate font-mono font-bold text-foreground">
                  {row.intent.symbol} {row.intent.side} {row.intent.volume}
                </span>
                <span className="text-[9px] text-muted-foreground/80">
                  {row.ticket ? `#${row.ticket}` : row.id} · {row.mandateId ?? 'approval'}
                </span>
              </span>
              <span className="text-right font-mono text-muted-foreground">{fmt(row.avgPrice, 5)}</span>
              <span className={cn('text-right font-mono uppercase', statusTone(row.status))}>{row.status}</span>
            </div>
          ))}
          <button
            type="button"
            onClick={() => void load()}
            className="mt-2 w-full rounded-lg border border-border/20 py-1 text-[9px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
          >
            Refresh
          </button>
        </div>
      )}
    </div>
  );
}

// ── Quick trade ──────────────────────────────────────────────────────────────
const inputClass =
  'w-full rounded-lg border border-border/30 bg-transparent px-2 py-1 text-[11px] text-foreground focus:border-primary/40 focus:outline-none';

export function QuickTradeWidget() {
  const { connection, account, error: dataError, refresh } = useTradingData();
  const [symbol, setSymbol] = useState('EURUSD');
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [type, setType] = useState<'market' | 'limit' | 'stop'>('market');
  const [volume, setVolume] = useState('0.10');
  const [price, setPrice] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<OrderPreview | null>(null);
  const [approval, setApproval] = useState<TradeApproval | null>(null);
  const [phrase, setPhrase] = useState('');

  const intent: OrderIntent = {
    symbol: symbol.trim().toUpperCase(),
    side,
    type,
    volume: Number(volume),
    ...(type !== 'market' ? { price: Number(price) } : {}),
  };
  const armed = Boolean(connection?.armedUntil);
  const needsPhrase = Boolean(preview?.requiresTypedConfirmation);
  const canSubmit =
    Boolean(approval && approval.status === 'approved') &&
    (!needsPhrase || phrase.trim() === (preview?.confirmPhrase ?? ''));

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    setError(null);
    setMessage(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'the request failed');
    } finally {
      setBusy(null);
    }
  };

  const onPreview = () => {
    if (!connection) return;
    void run('preview', async () => {
      const next = await previewOrder(connection.id, intent);
      setPreview(next);
      setApproval(next.approval);
      setPhrase('');
      if (!next.allowed) {
        setApproval(null);
        setError(next.reasons.join('; ') || 'the order was refused');
      }
    });
  };

  const onApprove = () => {
    if (!approval) return;
    void run('approve', async () => {
      const next = await decideTradeApproval(approval.id, true);
      setApproval(next);
      if (next.status !== 'approved') setError(`Approval is ${next.status}.`);
    });
  };

  const onSubmit = () => {
    if (!connection || !approval) return;
    void run('submit', async () => {
      const result = await submitOrder(connection.id, intent, approval.id, phrase);
      setMessage(
        result.status === 'filled' || result.status === 'placed'
          ? `Order ${result.status}.`
          : `${result.status}: ${(result.reasons || []).join('; ') || 'no order sent'}`,
      );
      if (result.sent) {
        setPreview(null);
        setApproval(null);
        setPhrase('');
        invalidateTradingSnapshot();
        await refresh();
      }
    });
  };

  if (!connection) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader title="Quick Trade" />
        <ConnectNote />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader
        title="Quick Trade"
        hint={
          <span className="flex items-center gap-1.5">
            <Badge tone={account?.mode === 'real' ? DOWN : 'text-sky-300'}>{account?.mode ?? '—'}</Badge>
            {connection.mode === 'live' && <Badge tone={armed ? UP : 'text-amber-300'}>{armed ? 'armed' : 'unarmed'}</Badge>}
            {account ? <span>{account.currency}</span> : null}
          </span>
        }
      />
      {(error || dataError) && <p className="mb-2 text-[10px] text-rose-300">{error || dataError}</p>}
      {message && <p className="mb-2 text-[10px] text-emerald-300">{message}</p>}
      {connection.mode === 'live' && !armed && (
        <p className="mb-2 text-[10px] text-amber-300">
          Live is not armed. Arm it in Settings → Broker connection before submitting.
        </p>
      )}

      <div className="grid grid-cols-2 gap-2">
        <label className="col-span-2 space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Symbol</span>
          <input value={symbol} onChange={(event) => setSymbol(event.target.value)} className={inputClass} />
        </label>
        <label className="space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Side</span>
          <Select value={side} onValueChange={(value) => value && setSide(value as 'buy' | 'sell')}>
            <SelectTrigger className={cn(inputClass, 'h-auto w-full justify-between py-1.5')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="buy">Buy</SelectItem>
              <SelectItem value="sell">Sell</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Type</span>
          <Select value={type} onValueChange={(value) => value && setType(value as typeof type)}>
            <SelectTrigger className={cn(inputClass, 'h-auto w-full justify-between py-1.5')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="market">Market</SelectItem>
              <SelectItem value="limit">Limit</SelectItem>
              <SelectItem value="stop">Stop</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Volume</span>
          <input value={volume} onChange={(event) => setVolume(event.target.value)} className={inputClass} inputMode="decimal" />
        </label>
        {type !== 'market' && (
          <label className="space-y-1">
            <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Price</span>
            <input value={price} onChange={(event) => setPrice(event.target.value)} className={inputClass} inputMode="decimal" />
          </label>
        )}
      </div>

      <div className="mt-2 flex gap-2">
        <button
          type="button"
          onClick={onPreview}
          disabled={busy !== null || !symbol.trim() || !(Number(volume) > 0)}
          className="flex-1 rounded-lg border border-primary/40 bg-primary/10 py-1.5 text-[10px] font-medium text-primary hover:bg-primary/15 disabled:opacity-60"
        >
          {busy === 'preview' ? 'Checking…' : 'Preview order'}
        </button>
      </div>

      {preview && (
        <div className="mt-2 space-y-2 rounded-xl border border-border/20 p-2">
          <p className="text-[10px] text-foreground">
            {preview.preview.symbol} {preview.allowed ? '· broker check ok' : '· refused'}
            {preview.preview.margin != null ? ` · margin ${fmt(preview.preview.margin)}` : ''}
            {preview.preview.retcodeName ? ` · ${preview.preview.retcodeName}` : ''}
          </p>
          {preview.reasons.length > 0 && (
            <p className="text-[10px] text-amber-300">{preview.reasons.join('; ')}</p>
          )}
          {approval && (
            <div className="flex items-center justify-between gap-2">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                Approval: {approval.status}
              </span>
              {approval.status === 'pending' && (
                <button
                  type="button"
                  onClick={onApprove}
                  disabled={busy !== null}
                  className="rounded-lg border border-emerald-500/30 px-2 py-1 text-[10px] text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-60"
                >
                  {busy === 'approve' ? 'Approving…' : 'Approve'}
                </button>
              )}
            </div>
          )}
          {needsPhrase && (
            <label className="block space-y-1">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                Type {preview.confirmPhrase}
              </span>
              <input value={phrase} onChange={(event) => setPhrase(event.target.value)} className={inputClass} />
            </label>
          )}
          <button
            type="button"
            onClick={onSubmit}
            disabled={!canSubmit || busy !== null}
            className="w-full rounded-lg border border-rose-500/30 bg-rose-500/10 py-1.5 text-[10px] font-medium text-rose-200 hover:bg-rose-500/15 disabled:opacity-50"
          >
            {busy === 'submit' ? 'Submitting…' : 'Submit order'}
          </button>
        </div>
      )}
    </div>
  );
}
