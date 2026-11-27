'use client';

// ── Trade approval in the composer ───────────────────────────────────────────
// Eclipse's `order_preview` tool creates a single-use, digest-bound approval for
// the exact order. This banner is the operator's half of that contract: it shows
// the pending order, takes the typed confirmation, and submits it through the
// SAME `/trading/.../orders/submit` route the Quick Trade widget uses.
//
// It is deliberately not part of the generic approval gate: a trade approval
// lives in the broker service's store, is bound to a specific connection and
// order digest, and consumes the approval atomically on send. Approving the
// generic card could never authorize that payload, so this surface mirrors the
// trade contract instead of pretending the two are the same.

import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, ShieldAlert, ShieldCheck, X } from '@/lib/icons';
import { cn } from '@/lib/utils';
import {
  decideTradeApproval,
  listBrokerConnections,
  listTradeApprovals,
  submitOrder,
  type BrokerConnection,
  type OrderIntent,
  type TradeApproval,
} from '@/lib/harness/clients/trading-client';
import { ORDER_PREVIEW_NOTICE, TRADE_RESPONSIBILITY_LABEL } from '@/lib/legal/disclosure';

const POLL_MS = 6000;
const LIVE_ORDER_STATUSES = new Set(['pending', 'approved']);

function volumeText(volume?: number): string {
  if (volume == null || !Number.isFinite(volume)) return '';
  // Match the server's `float(...):g` formatting for the phrase.
  return String(Number(volume));
}

export function TradeApprovalBanner({
  onCancel,
  automationId,
}: { onCancel?: () => void; automationId?: string } = {}) {
  const [approvals, setApprovals] = useState<TradeApproval[]>([]);
  const [connections, setConnections] = useState<BrokerConnection[]>([]);
  const [phrases, setPhrases] = useState<Record<string, string>>({});
  const [required, setRequired] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  // The operator must explicitly own the decision before any order is sent.
  // The typed phrase proves intent; this checkbox records the responsibility.
  const [ownership, setOwnership] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<{ id: string; message: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // A denied approval is removed immediately and kept out of the poll while the
  // decision round-trips, so it can never flash back into the composer.
  const dismissed = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      const [nextApprovals, nextConnections] = await Promise.all([
        listTradeApprovals(undefined, 20),
        listBrokerConnections().catch(() => [] as BrokerConnection[]),
      ]);
      setApprovals(
        nextApprovals.filter(
          (a) => LIVE_ORDER_STATUSES.has(a.status)
            && !dismissed.current.has(a.id)
            // Scoped to the bot whose chat is open. Rows from before the
            // attribution existed have no automationId and stay visible; a row
            // that names a different bot never leaks into this chat.
            && (!automationId || !a.automationId || a.automationId === automationId),
        ),
      );
      setConnections(nextConnections);
    } catch {
      // The trading runtime may be down; the composer must keep working.
    }
  }, [automationId]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load, automationId]);

  // A filled order consumes its approval, so the card unmounts. Keep the
  // success line on screen briefly instead of silently disappearing - the
  // operator must be able to see that the order really went through.
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 10_000);
    return () => clearTimeout(timer);
  }, [notice]);

  if (approvals.length === 0) {
    return notice ? (
      <div className="mb-2" aria-live="polite">
        <p className="px-1 text-[11px] text-emerald-300">{notice}</p>
      </div>
    ) : null;
  }

  const modeFor = (connectionId?: string | null) =>
    connections.find((connection) => connection.id === connectionId)?.mode;

  const expectedPhrase = (approval: TradeApproval): string => {
    const manual = required[approval.id];
    if (manual) return manual;
    const intent = approval.intent;
    if (!intent || modeFor(approval.connectionId) !== 'live') return '';
    const symbol = (approval.preview?.symbol || intent.symbol || '').toUpperCase();
    return `LIVE ${String(intent.side).toUpperCase()} ${volumeText(approval.preview?.volume ?? intent.volume)} ${symbol}`.trim();
  };

  const decideAndSend = async (approval: TradeApproval) => {
    const intent = approval.intent as OrderIntent | null | undefined;
    if (!approval.connectionId || !intent) {
      setError({ id: approval.id, message: 'This approval is missing its order details; re-preview it.' });
      return;
    }
    setBusy(approval.id);
    setError(null);
    setNotice(null);
    try {
      if (approval.status === 'pending') {
        const decided = await decideTradeApproval(approval.id, true);
        if (decided.status !== 'approved') {
          setError({ id: approval.id, message: `Approval is ${decided.status}.` });
          return;
        }
      }
      const result = await submitOrder(
        approval.connectionId,
        intent,
        approval.id,
        phrases[approval.id] ?? '',
      );
      if (result.status === 'CONFIRMATION_REQUIRED') {
        setRequired((current) => ({
          ...current,
          [approval.id]: result.requiredPhrase || 'the confirmation phrase',
        }));
        setError({
          id: approval.id,
          message: result.requiredPhrase
            ? `Type ${result.requiredPhrase} exactly to send.`
            : 'A typed confirmation phrase is required to send.',
        });
        return;
      }
      if (result.sent) {
        setNotice(
          result.status === 'filled' || result.status === 'placed'
            ? `Order ${result.status}${result.ledger?.ticket ? ` · ticket ${result.ledger.ticket}` : ''}.`
            : `Order state: ${result.status}.`,
        );
        setPhrases((current) => ({ ...current, [approval.id]: '' }));
        setRequired((current) => ({ ...current, [approval.id]: '' }));
        setOwnership((current) => ({ ...current, [approval.id]: false }));
        await load();
        return;
      }
      setError({
        id: approval.id,
        message: (result.reasons || []).join('; ') || `Order not sent (${result.status}).`,
      });
    } catch (err) {
      setError({
        id: approval.id,
        message: err instanceof Error ? err.message : 'the order could not be sent',
      });
    } finally {
      setBusy(null);
    }
  };

  const deny = async (approval: TradeApproval) => {
    // Deny is optimistic: the card leaves at once, the turn is cancelled, and
    // the decision is confirmed with the runtime. If the decision fails the card
    // comes back with the honest error.
    setBusy(approval.id);
    setError(null);
    setNotice(null);
    dismissed.current.add(approval.id);
    setApprovals((current) => current.filter((item) => item.id !== approval.id));
    setPhrases((current) => ({ ...current, [approval.id]: '' }));
    setRequired((current) => ({ ...current, [approval.id]: '' }));
    setOwnership((current) => ({ ...current, [approval.id]: false }));
    onCancel?.();
    try {
      await decideTradeApproval(approval.id, false);
    } catch (err) {
      dismissed.current.delete(approval.id);
      setApprovals((current) =>
        current.some((item) => item.id === approval.id) ? current : [...current, approval],
      );
      setError({
        id: approval.id,
        message: err instanceof Error ? err.message : 'the approval could not be denied',
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mb-2 space-y-2" aria-live="polite">
      {notice && <p className="px-1 text-[11px] text-emerald-300">{notice}</p>}
      {approvals.map((approval) => {
        const live = modeFor(approval.connectionId) === 'live';
        const expected = expectedPhrase(approval);
        const typed = phrases[approval.id] ?? '';
        const phraseOk = !expected || typed.trim() === expected;
        const owned = ownership[approval.id] === true;
        const isBusy = busy === approval.id;
        return (
          <div
            key={approval.id}
            className="glass-card rounded-2xl border border-border/40 px-3.5 py-3"
            role="alertdialog"
            aria-label="Trade approval"
          >
            <div className="flex items-start gap-2.5">
              <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-primary/30 bg-primary/10 text-primary">
                <ShieldAlert size={14} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <p className="text-[10px] font-bold uppercase tracking-wide text-primary">
                    Trade approval
                  </p>
                  <span
                    className={cn(
                      'rounded-md border px-1.5 py-0.5 text-[9px] font-mono uppercase',
                      live
                        ? 'border-rose-500/40 text-rose-300'
                        : 'border-sky-500/30 text-sky-300',
                    )}
                  >
                    {live ? 'live' : 'demo'}
                  </span>
                  {approval.risk && (
                    <span className="rounded-md border border-border/40 px-1.5 py-0.5 text-[9px] font-mono uppercase text-muted-foreground">
                      {approval.risk} risk
                    </span>
                  )}
                </div>
                <p className="mt-1 text-[12.5px] font-semibold text-foreground">
                  {approval.actionSummary || 'Order awaiting confirmation'}
                </p>
                {(() => {
                  const risk = approval.preview?.riskSummary;
                  if (!risk) return null;
                  const currency = risk.currency ? ` ${risk.currency}` : '';
                  const rr =
                    risk.rewardRisk != null ? ` · reward:risk ${risk.rewardRisk}:1` : '';
                  return (
                    <div className="mt-1.5 space-y-1" data-testid="trade-risk-summary">
                      <p
                        className={cn(
                          'text-[10.5px] font-medium',
                          risk.verdict === 'block' ? 'text-rose-300' : 'text-foreground/90',
                        )}
                      >
                        Max loss {risk.maxLoss.toLocaleString()}
                        {currency} ({risk.riskPctOfEquity.toFixed(2)}% of equity){rr}
                        {risk.spreadPctOfRisk != null
                          ? ` · spread ${risk.spreadPctOfRisk.toFixed(0)}% of risk`
                          : ''}
                      </p>
                      {(risk.reasons?.length ? risk.reasons : risk.warnings ?? []).map(
                        (line) => (
                          <p
                            key={line}
                            className="text-[10px] leading-relaxed text-amber-300/90"
                          >
                            {line}
                          </p>
                        ),
                      )}
                    </div>
                  );
                })()}
                <p className="mt-1 text-[10.5px] leading-relaxed text-muted-foreground/80">
                  {ORDER_PREVIEW_NOTICE}
                </p>
                {error?.id === approval.id && (
                  <p className="mt-1 text-[11px] leading-relaxed text-rose-300">{error.message}</p>
                )}
                {(live || expected) && (
                  <label className="mt-2 block text-[11px] text-muted-foreground">
                    Type <span className="font-mono text-foreground">{expected || 'the phrase'}</span> to send
                    <input
                      value={typed}
                      onChange={(event) =>
                        setPhrases((current) => ({ ...current, [approval.id]: event.target.value }))
                      }
                      spellCheck={false}
                      autoComplete="off"
                      className="mt-1 w-full rounded-lg border border-border/40 bg-transparent px-2 py-1.5 text-xs text-foreground focus:border-primary/40 focus:outline-none"
                    />
                  </label>
                )}
                <label className="mt-2 flex cursor-pointer items-start gap-2 text-[10.5px] leading-relaxed text-muted-foreground">
                  <input
                    type="checkbox"
                    className="mt-0.5 accent-primary"
                    checked={owned}
                    onChange={(event) =>
                      setOwnership((current) => ({ ...current, [approval.id]: event.target.checked }))
                    }
                  />
                  <span>
                    {TRADE_RESPONSIBILITY_LABEL}
                  </span>
                </label>
                <div className="mt-2.5 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    disabled={isBusy || !phraseOk || !owned}
                    onClick={() => void decideAndSend(approval)}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-primary px-3 py-1.5 text-[11px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {isBusy ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />}
                    {live ? 'Confirm & send live' : 'Approve & send'}
                  </button>
                  <button
                    type="button"
                    disabled={isBusy}
                    onClick={() => void deny(approval)}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl border border-border/40 px-3 py-1.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent/30 hover:text-foreground disabled:opacity-50"
                  >
                    <X size={12} /> Deny
                  </button>
                  <span className="ml-auto text-[10px] text-muted-foreground">
                    Single use · expires {approval.expiresAt ? new Date(approval.expiresAt).toLocaleTimeString() : 'soon'}
                  </span>
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
