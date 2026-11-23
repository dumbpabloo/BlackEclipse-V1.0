"use client";

// ── Broker control-center panels (presentational) ────────────────────────────
// Read + account surfaces for the sidebar broker dialog. All data comes from the
// existing trading-client; nothing here invents numbers. When the broker is
// unavailable the panels render an explicit UNAVAILABLE note instead.

import type { ReactNode } from "react";
import { Loader2, RefreshCw, X } from "@/lib/icons";
import { cn } from "@/lib/utils";
import type {
  BrokerAccount,
  BrokerConnection,
  BrokerFill,
  BrokerPosition,
  BrokerQuote,
  BrokerWorkingOrder,
  LedgerOrder,
  OrderIntent,
} from "@/lib/harness/clients/trading-client";

// ── formatting ───────────────────────────────────────────────────────────────

export function fmtMoney(value: number | null | undefined, currency = ""): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const text = value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return currency ? `${text} ${currency}` : text;
}

export function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtTime(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return "—";
  return new Date(ms).toLocaleString();
}

export function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

function Pnl({ value }: { value: number }) {
  return (
    <span
      className={cn(
        "font-mono",
        value > 0 ? "text-emerald-300" : value < 0 ? "text-rose-300" : "text-foreground",
      )}
    >
      {fmtMoney(value)}
    </span>
  );
}

// ── shared primitives ────────────────────────────────────────────────────────

export function StatTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  tone?: "danger" | "good" | "warn";
}) {
  return (
    <div className="rounded-lg border border-border/25 bg-white/[0.02] px-2.5 py-1.5">
      <p className="text-[8.5px] uppercase tracking-widest text-muted-foreground">{label}</p>
      <p
        className={cn(
          "truncate font-mono text-[11px] text-foreground",
          tone === "danger" && "text-rose-300",
          tone === "good" && "text-emerald-300",
          tone === "warn" && "text-amber-300",
        )}
      >
        {value}
      </p>
    </div>
  );
}

export function PanelBox({
  title,
  hint,
  action,
  children,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="space-y-2 rounded-xl border border-border/20 bg-white/[0.015] p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-[11px] font-semibold text-foreground">{title}</p>
          {hint && <p className="text-[9.5px] text-muted-foreground">{hint}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function MiniAction({
  children,
  onClick,
  busy,
  tone,
  disabled,
  title,
}: {
  children: ReactNode;
  onClick: () => void;
  busy?: boolean;
  tone?: "danger" | "default";
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || disabled}
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[10px] transition-colors disabled:opacity-50",
        tone === "danger"
          ? "border-rose-500/30 text-rose-300 hover:bg-rose-500/10"
          : "border-border/40 text-muted-foreground hover:bg-white/[0.05] hover:text-foreground",
      )}
    >
      {busy ? <Loader2 size={11} className="animate-spin" /> : null}
      {children}
    </button>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-xl border border-dashed border-border/25 px-3 py-4 text-center text-[10px] text-muted-foreground">
      {children}
    </p>
  );
}

function TableShell({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-border/20">
      <table className="w-full min-w-[560px] border-collapse text-[10px]">{children}</table>
    </div>
  );
}

function Th({ children, align }: { children: ReactNode; align?: "right" }) {
  return (
    <th
      className={cn(
        "whitespace-nowrap px-2 py-1.5 text-[8.5px] font-semibold uppercase tracking-widest text-muted-foreground",
        align === "right" ? "text-right" : "text-left",
      )}
    >
      {children}
    </th>
  );
}

// ── Overview ─────────────────────────────────────────────────────────────────

export function BrokerOverviewPanel({
  connection,
  account,
  bridge,
  quote,
  quoteSymbol,
  quoteBusy,
  quoteError,
  onQuoteSymbolChange,
  onQuote,
  onRefresh,
  refreshing,
  onTest,
  onSync,
  onSmoke,
  busy,
}: {
  connection: BrokerConnection;
  account: BrokerAccount | null;
  bridge: { available: boolean | null; reason: string | null };
  quote: BrokerQuote | null;
  quoteSymbol: string;
  quoteBusy: boolean;
  quoteError: string | null;
  onQuoteSymbolChange: (value: string) => void;
  onQuote: () => void;
  onRefresh: () => void;
  refreshing: boolean;
  onTest: () => void;
  onSync: () => void;
  onSmoke: () => void;
  busy: string | null;
}) {
  const armed = Boolean(connection.armedUntil);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/20 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={cn(
              "h-2 w-2 shrink-0 rounded-full",
              connection.status === "connected"
                ? armed
                  ? "bg-rose-400"
                  : "bg-emerald-400"
                : "bg-amber-400",
            )}
          />
          <div className="min-w-0">
            <p className="truncate text-[11px] font-semibold text-foreground">
              {connection.label} · {connection.kind.toUpperCase()}
            </p>
            <p className="truncate text-[9.5px] text-muted-foreground">
              {connection.status}
              {connection.statusReason ? ` — ${connection.statusReason}` : ""}
              {account ? ` · ${account.mode.toUpperCase()}` : ""}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <MiniAction busy={refreshing} onClick={onRefresh} title="Re-read the broker">
            <RefreshCw size={11} /> Refresh
          </MiniAction>
          <MiniAction busy={busy === "test"} onClick={onTest}>
            Test
          </MiniAction>
          <MiniAction busy={busy === "sync"} onClick={onSync}>
            Sync
          </MiniAction>
          <MiniAction busy={busy === "smoke"} onClick={onSmoke} title="Probe, read, and sync once">
            Smoke
          </MiniAction>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2 rounded-xl border border-border/20 px-3 py-2">
        <div>
          <p className="text-[10px] font-medium text-foreground">
            {connection.kind === "mt5"
              ? "MT5 bridge"
              : connection.kind === "mt4_bridge"
                ? "MT4 bridge"
                : connection.kind === "alpaca"
                  ? "Alpaca adapter"
                  : connection.kind === "ibkr"
                    ? "IBKR gateway"
                    : "Broker adapter"}
          </p>
          <p className="text-[9.5px] text-muted-foreground">
            {bridge.available == null
              ? "Checking the runtime…"
              : bridge.available
                ? connection.kind === "mt5"
                  ? "Ready — Algo Trading must be enabled in MT5 to send orders."
                  : "Ready — reads and reconciliation are available."
                : bridge.reason ?? "Unavailable."}
          </p>
        </div>
        <span
          className={cn(
            "shrink-0 rounded-full border px-2 py-0.5 text-[8.5px] font-bold uppercase tracking-wide",
            bridge.available == null
              ? "border-border/30 bg-white/[0.04] text-muted-foreground"
              : bridge.available
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                : "border-amber-500/30 bg-amber-500/10 text-amber-300",
          )}
        >
          {bridge.available == null ? "checking" : bridge.available ? "ready" : "unavailable"}
        </span>
      </div>

      {account ? (
        <PanelBox
          title="Account"
          hint={`${account.company || "Broker"} · ${account.server || "no server"}`}
          action={
            <span
              className={cn(
                "rounded-full border px-2 py-0.5 text-[8.5px] font-bold uppercase",
                account.mode === "real"
                  ? "border-rose-500/30 bg-rose-500/10 text-rose-300"
                  : "border-sky-500/30 bg-sky-500/10 text-sky-300",
              )}
            >
              {account.mode}
            </span>
          }
        >
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            <StatTile label="Login" value={String(account.login)} />
            <StatTile label="Balance" value={fmtMoney(account.balance, account.currency)} />
            <StatTile label="Equity" value={fmtMoney(account.equity, account.currency)} />
            <StatTile label="Used margin" value={fmtMoney(account.margin, account.currency)} />
            <StatTile label="Free margin" value={fmtMoney(account.marginFree, account.currency)} />
            <StatTile
              label="Margin level"
              value={account.marginLevel > 0 ? `${fmtNum(account.marginLevel)}%` : "—"}
              tone={account.marginLevel > 0 && account.marginLevel < 100 ? "danger" : undefined}
            />
            <StatTile label="Floating P/L" value={<Pnl value={account.profit} />} />
            <StatTile label="Leverage" value={`1:${fmtNum(account.leverage, 0)}`} />
            <StatTile label="Margin mode" value={account.marginMode} />
            <StatTile
              label="Trading"
              value={account.tradeAllowed ? "allowed" : "disabled"}
              tone={account.tradeAllowed ? "good" : "danger"}
            />
            <StatTile
              label="Algo trading"
              value={account.expertAllowed ? "allowed" : "disabled"}
              tone={account.expertAllowed ? "good" : "warn"}
            />
            <StatTile label="Currency" value={account.currency || "—"} />
          </div>
        </PanelBox>
      ) : (
        <Empty>No live account read yet. Use Test or Refresh while MT5 is running.</Empty>
      )}

      <div className="grid gap-1.5 sm:grid-cols-2">
        <StatTile label="Connection mode" value={connection.mode.toUpperCase()} />
        <StatTile
          label="Live arming"
          value={armed ? `armed until ${fmtStamp(connection.armedUntil)}` : "not armed"}
          tone={armed ? "danger" : undefined}
        />
        <StatTile label="Last seen" value={fmtStamp(connection.lastSeenAt)} />
        <StatTile label="Created" value={fmtStamp(connection.createdAt)} />
      </div>

      <PanelBox title="Quote lookup" hint="Live bid/ask from the attached terminal.">
        <div className="flex gap-1.5">
          <input
            value={quoteSymbol}
            onChange={(event) => onQuoteSymbolChange(event.target.value.toUpperCase())}
            onKeyDown={(event) => {
              if (event.key === "Enter") onQuote();
            }}
            placeholder="EURUSD"
            className="w-full rounded-lg border border-border/40 bg-transparent px-2 py-1 font-mono text-[11px] uppercase text-foreground focus:border-primary/40 focus:outline-none"
          />
          <MiniAction busy={quoteBusy} onClick={onQuote} disabled={!quoteSymbol.trim()}>
            Get
          </MiniAction>
        </div>
        {quoteError && <p className="text-[9.5px] text-rose-300">{quoteError}</p>}
        {quote && (
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
            <StatTile label="Symbol" value={quote.symbol} />
            <StatTile label="Bid" value={fmtNum(quote.bid, quote.digits)} />
            <StatTile label="Ask" value={fmtNum(quote.ask, quote.digits)} />
            <StatTile label="Spread" value={quote.spreadPoints == null ? "—" : `${quote.spreadPoints} pts`} />
            <StatTile label="Last" value={fmtNum(quote.last, quote.digits)} />
            <StatTile label="Digits" value={String(quote.digits)} />
            <StatTile label="Stops level" value={`${quote.stopsLevelPoints} pts`} />
            <StatTile label="Server time" value={fmtTime(quote.serverTimeMs)} />
          </div>
        )}
      </PanelBox>
    </div>
  );
}

// ── Positions ────────────────────────────────────────────────────────────────

export function BrokerPositionsPanel({
  positions,
  loading,
  busy,
  onClose,
  onCloseAll,
  onRefresh,
}: {
  positions: BrokerPosition[];
  loading: boolean;
  busy: string | null;
  onClose: (ticket: number | string) => void;
  onCloseAll: () => void;
  onRefresh: () => void;
}) {
  const total = positions.reduce((sum, position) => sum + position.profit + position.swap, 0);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] text-muted-foreground">
          {loading
            ? "Reading positions…"
            : `${positions.length} open position${positions.length === 1 ? "" : "s"} · floating `}
          {!loading && <Pnl value={total} />}
        </p>
        <div className="flex gap-1.5">
          <MiniAction busy={busy === "refresh"} onClick={onRefresh}>
            <RefreshCw size={11} /> Refresh
          </MiniAction>
          <MiniAction
            tone="danger"
            busy={busy === "close-all"}
            disabled={positions.length === 0}
            onClick={onCloseAll}
          >
            Close all
          </MiniAction>
        </div>
      </div>

      {positions.length === 0 ? (
        <Empty>{loading ? "Reading the broker…" : "No open positions on this account."}</Empty>
      ) : (
        <TableShell>
          <thead>
            <tr className="border-b border-border/20">
              <Th>Symbol</Th>
              <Th>Side</Th>
              <Th align="right">Volume</Th>
              <Th align="right">Open</Th>
              <Th align="right">Now</Th>
              <Th align="right">SL / TP</Th>
              <Th align="right">P/L</Th>
              <Th align="right">Magic</Th>
              <Th align="right">Opened</Th>
              <Th align="right"> </Th>
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => (
              <tr key={position.ticket} className="border-b border-border/10 last:border-0">
                <td className="px-2 py-1.5 font-mono text-[11px] text-foreground">{position.symbol}</td>
                <td className={cn("px-2 py-1.5 uppercase", position.side === "buy" ? "text-emerald-300" : "text-rose-300")}>
                  {position.side}
                </td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(position.volume, 2)}</td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(position.openPrice, 5)}</td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(position.currentPrice, 5)}</td>
                <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                  {position.stopLoss == null ? "—" : fmtNum(position.stopLoss, 5)} /{" "}
                  {position.takeProfit == null ? "—" : fmtNum(position.takeProfit, 5)}
                </td>
                <td className="px-2 py-1.5 text-right">
                  <Pnl value={position.profit + position.swap} />
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                  {position.magic || "0"}
                </td>
                <td className="px-2 py-1.5 text-right text-muted-foreground">
                  {fmtTime(position.openedAt)}
                </td>
                <td className="px-2 py-1.5 text-right">
                  <MiniAction
                    tone="danger"
                    busy={busy === `close:${position.ticket}`}
                    onClick={() => onClose(position.ticket)}
                    title={`Close ${position.symbol} #${position.ticket}`}
                  >
                    <X size={10} /> Close
                  </MiniAction>
                </td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
      <p className="text-[9px] text-muted-foreground">
        Close sends a market order to the broker for the full position volume. It cannot be undone.
      </p>
    </div>
  );
}

// ── Working orders ───────────────────────────────────────────────────────────

export function BrokerOrdersPanel({
  orders,
  loading,
  busy,
  onCancel,
  onRefresh,
}: {
  orders: BrokerWorkingOrder[];
  loading: boolean;
  busy: string | null;
  onCancel: (ticket: number | string) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] text-muted-foreground">
          {loading ? "Reading working orders…" : `${orders.length} working order${orders.length === 1 ? "" : "s"}`}
        </p>
        <MiniAction busy={busy === "refresh"} onClick={onRefresh}>
          <RefreshCw size={11} /> Refresh
        </MiniAction>
      </div>

      {orders.length === 0 ? (
        <Empty>{loading ? "Reading the broker…" : "No pending limit/stop orders."}</Empty>
      ) : (
        <TableShell>
          <thead>
            <tr className="border-b border-border/20">
              <Th>Symbol</Th>
              <Th>Type</Th>
              <Th align="right">Volume</Th>
              <Th align="right">Order</Th>
              <Th align="right">Now</Th>
              <Th align="right">SL / TP</Th>
              <Th>State</Th>
              <Th align="right">Magic</Th>
              <Th align="right">Placed</Th>
              <Th align="right"> </Th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr key={order.ticket} className="border-b border-border/10 last:border-0">
                <td className="px-2 py-1.5 font-mono text-[11px] text-foreground">{order.symbol}</td>
                <td className="px-2 py-1.5 uppercase text-muted-foreground">{order.type}</td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(order.volumeCurrent, 2)}</td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(order.openPrice, 5)}</td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(order.currentPrice, 5)}</td>
                <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                  {order.stopLoss == null ? "—" : fmtNum(order.stopLoss, 5)} /{" "}
                  {order.takeProfit == null ? "—" : fmtNum(order.takeProfit, 5)}
                </td>
                <td className="px-2 py-1.5 text-muted-foreground">{order.state}</td>
                <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">{order.magic || "0"}</td>
                <td className="px-2 py-1.5 text-right text-muted-foreground">{fmtTime(order.placedAt)}</td>
                <td className="px-2 py-1.5 text-right">
                  <MiniAction
                    tone="danger"
                    busy={busy === `cancel:${order.ticket}`}
                    onClick={() => onCancel(order.ticket)}
                    title={`Cancel order #${order.ticket}`}
                  >
                    <X size={10} /> Cancel
                  </MiniAction>
                </td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </div>
  );
}

// ── Activity ─────────────────────────────────────────────────────────────────

export function BrokerActivityPanel({
  fills,
  ledger,
  loading,
  busy,
  onRefresh,
}: {
  fills: BrokerFill[];
  ledger: LedgerOrder[];
  loading: boolean;
  busy: string | null;
  onRefresh: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] text-muted-foreground">
          {loading ? "Reading deals…" : `${fills.length} recent deal${fills.length === 1 ? "" : "s"}`}
        </p>
        <MiniAction busy={busy === "refresh"} onClick={onRefresh}>
          <RefreshCw size={11} /> Refresh
        </MiniAction>
      </div>

      {fills.length === 0 ? (
        <Empty>{loading ? "Reading the broker…" : "No deals reported by the broker yet."}</Empty>
      ) : (
        <TableShell>
          <thead>
            <tr className="border-b border-border/20">
              <Th>Dealt</Th>
              <Th>Symbol</Th>
              <Th>Side</Th>
              <Th align="right">Volume</Th>
              <Th align="right">Price</Th>
              <Th align="right">P/L</Th>
              <Th align="right">Commission</Th>
              <Th align="right">Deal #</Th>
              <Th align="right">Owner</Th>
            </tr>
          </thead>
          <tbody>
            {fills.map((fill) => (
              <tr key={fill.dealTicket} className="border-b border-border/10 last:border-0">
                <td className="px-2 py-1.5 text-muted-foreground">{fmtTime(fill.dealtAt)}</td>
                <td className="px-2 py-1.5 font-mono text-[11px] text-foreground">{fill.symbol}</td>
                <td className="px-2 py-1.5 uppercase text-muted-foreground">{fill.side}</td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(fill.volume, 2)}</td>
                <td className="px-2 py-1.5 text-right font-mono">{fmtNum(fill.price, 5)}</td>
                <td className="px-2 py-1.5 text-right">
                  <Pnl value={fill.profit + fill.swap} />
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                  {fmtMoney(fill.commission)}
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                  {fill.dealTicket}
                </td>
                <td className="px-2 py-1.5 text-right">
                  <span
                    className={cn(
                      "rounded-full border px-1.5 py-0.5 text-[8px] font-bold uppercase",
                      fill.eclipseOwned
                        ? "border-primary/30 bg-primary/10 text-primary"
                        : "border-border/30 text-muted-foreground",
                    )}
                  >
                    {fill.eclipseOwned ? "eclipse" : "manual"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}

      <PanelBox
        title="Eclipse order ledger"
        hint="Orders submitted through Eclipse on this connection."
      >
        {ledger.length === 0 ? (
          <Empty>No orders submitted through Eclipse yet.</Empty>
        ) : (
          <div className="space-y-1">
            {ledger.map((order) => {
              const intent = order.intent as OrderIntent;
              return (
                <div
                  key={order.id}
                  className="flex items-center justify-between gap-2 rounded-lg border border-border/20 px-2 py-1"
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-[10px] text-foreground">
                      {intent?.symbol ?? "—"} {intent?.side ?? ""} {intent?.volume ?? ""}
                      {order.ticket ? ` · #${order.ticket}` : ""}
                    </p>
                    <p className="truncate text-[9px] text-muted-foreground">
                      {fmtStamp(order.createdAt)} · {order.retcodeName ?? order.error ?? "no broker code"}
                    </p>
                  </div>
                  <span className="shrink-0 rounded-full border border-border/30 px-1.5 py-0.5 text-[8px] font-bold uppercase text-muted-foreground">
                    {order.status}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </PanelBox>
    </div>
  );
}
