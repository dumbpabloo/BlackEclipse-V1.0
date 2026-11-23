"use client";

// ── Trading section (bot detail) ────────────────────────────────────────────
// What the bot is allowed to do, what it actually did, and the lessons it wants
// to apply. Everything here is read from the broker/run history or computed by
// the pure learning module — no optimistic numbers.

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, RefreshCw, ShieldOff, TrendingUp } from "@/lib/icons";
import {
  getBotTrading,
  panicBotTrading,
  prepareBotTrading,
  scanBotTrading,
  updateAutomation,
  type AutomationView,
  type BotTradingStatusView,
  type TradingScanResult,
} from "@/lib/harness/clients/automation-browser";
import {
  analyzeFailures,
  applyProposal,
  buildTradingAuthority,
  consentStatus,
  decisionNeedsAttention,
  decisionTone,
  draftFromTradingAction,
  expectedConsentPhrase,
  explainDecision,
  formatDecisionLine,
  grantConsent,
  learningSummary,
  parseSymbols,
  proposeImprovements,
  type DecisionRecord,
  type ImprovementProposal,
  type TradingDraft,
} from "@/lib/bots/trading";
import { l2Eligibility } from "@/lib/bots/trading/tier";
import { listBrokerConnections } from "@/lib/harness/clients/trading-client";
import { authorizeBlockReason } from "@/lib/bots/trading/authorize";
import {
  loadLastVenue,
  pickInitialVenue,
  rememberVenue,
} from "@/lib/bots/trading/venue-memory";
import { StrategyPicker } from "@/components/bots/trading/strategy-picker";
import { AuthorityStrip } from "@/components/bots/trading/authority-strip";
import { cn } from "@/lib/utils";

interface Props {
  bot: AutomationView;
  onChanged: () => void;
}

export function BotTradingSection({ bot, onChanged }: Props) {
  const isStrategy = bot.action.type === "run_strategy";
  const [status, setStatus] = useState<BotTradingStatusView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [scan, setScan] = useState<TradingScanResult | null>(null);
  const [scanning, setScanning] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await getBotTrading(bot.id, 40));
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not read trading status");
    } finally {
      setLoading(false);
    }
  }, [bot.id]);

  useEffect(() => {
    void refresh();
  }, [refresh, bot.version]);

  const draft = useMemo(
    () => draftFromTradingAction(bot.action.config, bot.executionAuthority),
    [bot.action.config, bot.executionAuthority],
  );
  const decisions = useMemo(
    () => (status?.decisions ?? []) as unknown as DecisionRecord[],
    [status],
  );
  const patterns = useMemo(
    () =>
      analyzeFailures(decisions, {
        threshold: draft.improveAfterFailures,
        window: 60,
      }),
    [decisions, draft.improveAfterFailures],
  );
  const proposals = useMemo(
    () =>
      draft.learningEnabled
        ? proposeImprovements(patterns, {
            config: {
              strategyId: draft.strategyId,
              strategyVersion: draft.strategyVersion,
              mode: draft.mode,
              connectionId: draft.connectionId,
              symbols: draft.symbolsText.split(/[\s,;]+/).filter(Boolean),
              excludedSymbols: draft.excludedText.split(/[\s,;]+/).filter(Boolean),
              dynamicSymbols: draft.dynamicSymbols,
              maxSymbols: draft.maxSymbols,
              timeframe: draft.timeframe,
              volume: draft.volume,
              stopLossPoints: draft.stopLossPoints,
              takeProfitPoints: draft.takeProfitPoints,
              maxOrdersPerDay: draft.maxOrdersPerDay,
              session: draft.sessionEnabled
                ? {
                    timezone: draft.sessionTimezone,
                    weekdays: draft.sessionWeekdays,
                    start: draft.sessionStart,
                    end: draft.sessionEnd,
                  }
                : null,
              trialId: draft.trialId,
              dryRun: draft.dryRun,
              consent: draft.consent,
              visuals: draft.visuals,
              learning: {
                enabled: draft.learningEnabled,
                threshold: draft.improveAfterFailures,
                tightenOnly: true,
                requireConsent: true,
                lastAcceptedAt: null,
                lastProposedAt: null,
                proposed: [],
              },
            },
            authority: {
              tier: draft.tier,
              appetite: draft.appetite,
              sizingMode: draft.sizingMode,
              requireStop: draft.requireStop,
              maxOpenPositions: draft.maxOpenPositions,
              maxMarginPerOrder: draft.maxMarginPerOrder,
              cooldownS: draft.cooldownS,
              maxDailyLossPct: draft.maxDailyLossPct,
              autoMode: draft.autoMode,
              autoRenew: draft.autoRenew,
              learningEnabled: draft.learningEnabled,
              improveAfterFailures: draft.improveAfterFailures,
            },
          })
        : [],
    [patterns, draft],
  );

  const applyLesson = async (proposal: ImprovementProposal) => {
    if (busy) return;
    setBusy(proposal.id);
    setError(null);
    try {
      const consent = draft.consent;
      const config = {
        strategyId: draft.strategyId,
        strategyVersion: draft.strategyVersion,
        mode: draft.mode,
        connectionId: draft.connectionId,
        symbols: draft.symbolsText.split(/[\s,;]+/).filter(Boolean),
        excludedSymbols: draft.excludedText.split(/[\s,;]+/).filter(Boolean),
        dynamicSymbols: draft.dynamicSymbols,
        maxSymbols: draft.maxSymbols,
        timeframe: draft.timeframe,
        volume: draft.volume,
        stopLossPoints: draft.stopLossPoints,
        takeProfitPoints: draft.takeProfitPoints,
        maxOrdersPerDay: draft.maxOrdersPerDay,
        session: draft.sessionEnabled
          ? {
              timezone: draft.sessionTimezone,
              weekdays: draft.sessionWeekdays,
              start: draft.sessionStart,
              end: draft.sessionEnd,
            }
          : null,
        trialId: draft.trialId,
        dryRun: draft.dryRun,
        consent,
        visuals: draft.visuals,
        learning: {
          enabled: draft.learningEnabled,
          threshold: draft.improveAfterFailures,
          tightenOnly: true,
          requireConsent: true,
          lastAcceptedAt: null,
          lastProposedAt: null,
          proposed: [],
        },
      };
      const authority = {
        tier: draft.tier,
        appetite: draft.appetite,
        sizingMode: draft.sizingMode,
        requireStop: draft.requireStop,
        maxOpenPositions: draft.maxOpenPositions,
        maxMarginPerOrder: draft.maxMarginPerOrder,
        cooldownS: draft.cooldownS,
        maxDailyLossPct: draft.maxDailyLossPct,
        autoMode: draft.autoMode,
        autoRenew: draft.autoRenew,
        learningEnabled: draft.learningEnabled,
        improveAfterFailures: draft.improveAfterFailures,
      };
      const applied = applyProposal(config, authority, proposal, true);
      if (!applied.applied.length) {
        setError("That lesson would not change anything yet.");
        return;
      }
      await updateAutomation(bot.id, {
        action: {
          ...bot.action,
          config: { ...bot.action.config, ...applied.config } as unknown as Record<string, unknown>,
        },
        executionAuthority: applied.authority,
      });
      // The version bumped, so the mandate pin is stale. Re-prepare under the
      // standing consent: auto mode means the operator already said yes.
      await prepareBotTrading(bot.id, {
        strategyId: applied.config.strategyId,
        strategyVersion: applied.config.strategyVersion,
        mode: applied.config.mode,
        connectionId: applied.config.connectionId,
        symbols: applied.config.symbols,
        timeframe: applied.config.timeframe,
        volume: applied.config.volume,
        stopLossPoints: applied.config.stopLossPoints,
        takeProfitPoints: applied.config.takeProfitPoints,
        maxOrders: applied.config.maxOrdersPerDay,
        maxMargin: applied.authority.maxMarginPerOrder,
        confirmation: "",
      });
      onChanged();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not apply the lesson");
    } finally {
      setBusy(null);
    }
  };

  const findTrades = async () => {
    if (scanning || busy) return;
    setScanning(true);
    setError(null);
    try {
      const symbols = draft.symbolsText.split(/[\s,;]+/).filter(Boolean);
      setScan(await scanBotTrading(bot.id, symbols));
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not scan for candidates");
    } finally {
      setScanning(false);
    }
  };

  const adoptScan = async () => {
    if (!scan?.actionable.length || busy) return;
    setBusy("adopt");
    setError(null);
    try {
      await updateAutomation(bot.id, {
        action: {
          ...bot.action,
          config: {
            ...bot.action.config,
            symbols: scan.actionable,
          } as unknown as Record<string, unknown>,
        },
      });
      // The version bumped: re-prepare under the standing consent (auto mode).
      await prepareBotTrading(bot.id, {
        strategyId: draft.strategyId,
        strategyVersion: draft.strategyVersion,
        mode: draft.mode,
        connectionId: draft.connectionId,
        symbols: scan.actionable,
        timeframe: draft.timeframe,
        volume: draft.volume,
        stopLossPoints: draft.stopLossPoints,
        takeProfitPoints: draft.takeProfitPoints,
        maxOrders: draft.maxOrdersPerDay,
        maxMargin: draft.maxMarginPerOrder,
        confirmation: "",
      });
      setScan(null);
      onChanged();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not adopt the candidates");
    } finally {
      setBusy(null);
    }
  };

  const panic = async () => {    if (busy) return;
    setBusy("panic");
    try {
      await panicBotTrading(bot.id);
      onChanged();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not stop trading");
    } finally {
      setBusy(null);
    }
  };

  const equity = readNumber(status?.account, ["equity", "balance", "bookValue"]);
  const positions = countRows(status?.positions);
  const consent = draft.consent;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <Badge label={draft.mode.toUpperCase()} tone={draft.mode === "live" ? "danger" : "info"} />
        <Badge label={draft.tier.toUpperCase()} tone={draft.tier === "l2" ? "danger" : "info"} />
        <Badge label={isStrategy ? "strategy" : "agent"} tone="muted" />
        <Badge
          label={`consent ${consentStatus(consent)}`}
          tone={consentStatus(consent) === "active" ? "positive" : "warning"}
        />
        {isStrategy && (
          <span className="text-muted-foreground">
            {draft.strategyId || "no strategy"} @ {draft.strategyVersion || "?"}
          </span>
        )}
        <button
          type="button"
          onClick={() => void refresh()}
          className="ml-auto inline-flex items-center gap-1 rounded px-2 py-1 text-muted-foreground hover:bg-white/[0.06] hover:text-foreground"
        >
          <RefreshCw size={11} className={cn(loading && "animate-spin")} /> Refresh
        </button>
      </div>

      {error && <p className="text-[11px] text-rose-300">{error}</p>}
      {!isStrategy && <p className="text-[11px] text-muted-foreground">
        This agent proposes orders at L0. Raise its tier in Settings to let it fill.
      </p>}

      {/* Live state */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Equity" value={equity === null ? "UNAVAILABLE" : equity.toLocaleString()} />
        <Stat label="Positions" value={positions === null ? "UNAVAILABLE" : String(positions)} />
        <Stat
          label="Trial"
          value={status?.trial ? `${status.trial.status} · ${status.trial.ordersUsed}/${status.trial.maxOrders}` : "none"}
        />
        <Stat
          label="Last decision"
          value={decisions.length ? formatDecisionLine(decisions[decisions.length - 1]) : "—"}
        />
      </div>
      {status?.unavailable?.length ? (
        <p className="text-[11px] text-amber-300">
          {status.unavailable.map((item) => `${item.field}: ${item.reason}`).join(" · ")}
        </p>
      ) : null}

      {/* What keeps the bot authorized, and when it renews itself */}
      {status?.authority?.available && draft.mode !== "shadow" && (
        <AuthorityStrip
          botId={bot.id}
          authority={status.authority}
          disabled={busy !== null}
          onRenewed={refresh}
        />
      )}

      {/* Decisions */}
      <div>
        <div className="mb-1 text-[11px] font-semibold text-muted-foreground">
          Recent decisions
        </div>
        {decisions.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            No decisions yet. Run the bot once, or wait for its next schedule.
          </p>
        ) : (
          <ul className="space-y-1">
            {decisions
              .slice(-5)
              .reverse()
              .map((decision, index) => (
                <li
                  key={`${decision.barTs ?? index}-${index}`}
                  className="rounded-lg border border-border/10 px-2 py-1.5 text-[11px]"
                >
                  <div className="flex items-center gap-1.5">
                    {decisionNeedsAttention(decision) && (
                      <AlertTriangle size={11} className="text-amber-300" />
                    )}
                    <span className={toneClass(decisionTone(decision))}>
                      {formatDecisionLine(decision)}
                    </span>
                  </div>
                  <p className="mt-0.5 text-muted-foreground">{explainDecision(decision)}</p>
                </li>
              ))}
          </ul>
        )}
      </div>

      {/* Learning */}
      {draft.learningEnabled && (
        <div className="rounded-lg border border-border/15 p-2.5">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold">
            <TrendingUp size={12} className="text-sky-300" /> Learning
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {learningSummary(patterns, {
              enabled: draft.learningEnabled,
              threshold: draft.improveAfterFailures,
              tightenOnly: true,
              requireConsent: true,
              lastAcceptedAt: null,
              lastProposedAt: null,
              proposed: [],
            })}
          </p>
          {proposals.length > 0 && (
            <ul className="mt-2 space-y-2">
              {proposals.slice(0, 3).map((proposal) => (
                <li key={proposal.id} className="rounded-lg bg-white/[0.03] p-2">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="text-[11px] font-semibold">{proposal.title}</div>
                      <p className="mt-0.5 text-[11px] text-muted-foreground">{proposal.rationale}</p>
                    </div>
                    <Badge
                      label={proposal.riskDelta}
                      tone={proposal.riskDelta === "higher" ? "warning" : "positive"}
                    />
                  </div>
                  <div className="mt-1.5 flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() => void applyLesson(proposal)}
                      className={cn(
                        "rounded px-2 py-1 text-[11px] font-semibold",
                        proposal.riskDelta === "higher"
                          ? "bg-amber-500/20 text-amber-200 hover:bg-amber-500/30"
                          : "bg-emerald-500/15 text-emerald-200 hover:bg-emerald-500/25",
                      )}
                    >
                      {busy === proposal.id
                        ? "Applying…"
                        : proposal.riskDelta === "higher"
                          ? "Approve & apply"
                          : "Apply"}
                    </button>
                    <span className="text-[10px] text-muted-foreground">
                      Nothing changes until you say yes.
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Authorize one-time trading consent (the "auto mode" grant) */}
      {isStrategy && (
        <AuthorizeTrading
          bot={bot}
          draft={draft}
          onDone={() => {
            onChanged();
            void refresh();
          }}
        />
      )}

      {isStrategy && (
        <ChangeStrategy
          bot={bot}
          draft={draft}
          onDone={() => {
            onChanged();
            void refresh();
          }}
        />
      )}

      <TierLadder
        bot={bot}
        draft={draft}
        decisions={decisions}
        onDone={() => {
          onChanged();
          void refresh();
        }}
      />

      {/* Find trades — the bot's own candidate scan on the last closed bar */}
      {isStrategy && (
      <div className="rounded-lg border border-border/15 p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-[11px] font-semibold">Find trades</div>
          <button
            type="button"
            disabled={scanning || busy !== null}
            onClick={() => void findTrades()}
            className="rounded px-2 py-1 text-[11px] font-semibold text-sky-300 hover:bg-sky-500/10"
          >
            {scanning ? "Scanning…" : "Scan the last bar"}
          </button>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground">
          Evaluates every configured symbol with this strategy and returns the
          actionable ones. A scan is a condition, not an order.
        </p>
        {scan && (
          <div className="mt-2 space-y-1">
            {scan.results.map((row) => (
              <div key={row.symbol} className="flex items-center justify-between gap-2 text-[11px]">
                <span className={row.actionable ? "text-emerald-300" : "text-muted-foreground"}>
                  {row.symbol} · {row.signal}
                </span>
                <span className="truncate text-muted-foreground" title={row.reason}>
                  {row.reason}
                </span>
              </div>
            ))}
            {scan.actionable.length > 0 ? (
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => void adoptScan()}
                className="mt-1 rounded bg-emerald-500/15 px-2 py-1 text-[11px] font-semibold text-emerald-200 hover:bg-emerald-500/25"
              >
                Trade {scan.actionable.join(", ")}
              </button>
            ) : (
              <p className="text-[11px] text-muted-foreground">
                No actionable signal on the last bar.
              </p>
            )}
          </div>
        )}
      </div>
      )}

      {/* Panic */}
      {isStrategy && (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void panic()}
            className="inline-flex items-center gap-1 rounded-lg bg-rose-500/15 px-2.5 py-1.5 text-[11px] font-semibold text-rose-200 hover:bg-rose-500/25"
          >
            <ShieldOff size={12} /> Stop trading
          </button>
          <span className="text-[10px] text-muted-foreground">
            Revokes the mandate and pauses the bot. Open positions are left alone.
          </span>
        </div>
      )}
    </div>
  );
}

// ── Authority strip ─────────────────────────────────────────────────────────
// Extracted to `./trading/authority-strip` so this file stays under the size
// ratchet; the component owns its own renewal call and countdown labels.

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-white/[0.03] px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="truncate text-[11px] font-semibold" title={value}>
        {value}
      </div>
    </div>
  );
}

function Badge({ label, tone }: { label: string; tone: "positive" | "warning" | "danger" | "info" | "muted" }) {
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase", badgeClass(tone))}>
      {label}
    </span>
  );
}

function badgeClass(tone: string): string {
  switch (tone) {
    case "positive":
      return "bg-emerald-500/15 text-emerald-300";
    case "warning":
      return "bg-amber-500/15 text-amber-300";
    case "danger":
      return "bg-rose-500/15 text-rose-300";
    case "info":
      return "bg-sky-500/15 text-sky-300";
    default:
      return "bg-white/[0.06] text-muted-foreground";
  }
}

function toneClass(tone: string): string {
  if (tone === "positive") return "text-emerald-300";
  if (tone === "danger") return "text-rose-300";
  if (tone === "warning") return "text-amber-300";
  if (tone === "info") return "text-sky-300";
  return "text-muted-foreground";
}

function readNumber(payload: unknown, keys: string[]): number | null {
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  const data =
    record.data && typeof record.data === "object"
      ? (record.data as Record<string, unknown>)
      : record;
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function countRows(payload: unknown): number | null {
  if (!payload) return null;
  if (Array.isArray(payload)) return payload.length;
  if (typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["data", "positions", "rows", "items"]) {
      const value = record[key];
      if (Array.isArray(value)) return value.length;
      if (value && typeof value === "object") {
        const nested = (value as Record<string, unknown>).positions;
        if (Array.isArray(nested)) return nested.length;
      }
    }
  }
  return null;
}

// ── Tier ladder ─────────────────────────────────────────────────────────────
// L0 proposes · L1 fills demo · L2 fills live. Raising a tier is an operator
// decision; L2 is offered only when the measurable gates are met, and the server
// still requires an armed connection + a bot-pinned mandate at submit time.
function TierLadder({
  bot,
  draft,
  decisions,
  onDone,
}: {
  bot: AutomationView;
  draft: TradingDraft;
  decisions: DecisionRecord[];
  onDone: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [ack, setAck] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tier = bot.executionAuthority?.tier ?? "l0";
  const shadowDecisions = decisions.filter((decision) => decision.outcome !== "none").length;
  const daysActive = bot.createdAt
    ? Math.max(0, Math.floor((Date.now() - new Date(bot.createdAt).getTime()) / 86_400_000))
    : 0;
  const eligibility = l2Eligibility({ shadowDecisions, daysActive });

  const raise = async (next: "l1" | "l2") => {
    if (busy) return;
    setBusy(next);
    setError(null);
    try {
      await updateAutomation(bot.id, {
        executionAuthority: {
          ...(bot.executionAuthority ?? buildTradingAuthority(draft)),
          tier: next,
        },
      });
      setAck(false);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not change the tier");
    } finally {
      setBusy(null);
    }
  };

  const rows: Array<{ id: "l0" | "l1" | "l2"; label: string; detail: string }> = [
    { id: "l0", label: "L0 · Propose", detail: "Previews orders for your approval. Cannot fill." },
    { id: "l1", label: "L1 · Demo fills", detail: "May fill a demo account unattended." },
    { id: "l2", label: "L2 · Live fills", detail: "May fill live under a bot-pinned mandate." },
  ];

  return (
    <div className="rounded-lg border border-border/15 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold">Authority</div>
        <Badge label={`current ${tier.toUpperCase()}`} tone="info" />
      </div>
      <div className="mt-2 space-y-1">
        {rows.map((row) => {
          const current = tier === row.id;
          return (
            <div
              key={row.id}
              className={cn(
                "flex items-start justify-between gap-2 rounded-lg px-2 py-1.5",
                current ? "bg-white/[0.06]" : "bg-transparent",
              )}
            >
              <div className="min-w-0">
                <div className="text-[11px] text-foreground">
                  {row.label}
                  {current ? <span className="ml-1 text-[9px] text-emerald-300">active</span> : null}
                </div>
                <div className="text-[10px] text-muted-foreground">{row.detail}</div>
              </div>
              {row.id === "l1" && tier === "l0" && (
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void raise("l1")}
                  className="shrink-0 rounded px-2 py-1 text-[10px] font-semibold text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-50"
                >
                  {busy === "l1" ? "Enabling…" : "Enable L1"}
                </button>
              )}
              {row.id === "l2" && tier !== "l2" && (
                <button
                  type="button"
                  disabled={busy !== null || !ack}
                  onClick={() => void raise("l2")}
                  className="shrink-0 rounded px-2 py-1 text-[10px] font-semibold text-amber-200 hover:bg-amber-500/10 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy === "l2" ? "Requesting…" : "Grant L2"}
                </button>
              )}
            </div>
          );
        })}
      </div>

      {tier !== "l2" && (
        <div className="mt-1 space-y-1">
          <div className="text-[10px] text-muted-foreground">
            L2 evidence: {shadowDecisions} decision{shadowDecisions === 1 ? "" : "s"} · {daysActive} day
            {daysActive === 1 ? "" : "s"} active
          </div>
          {eligibility.blockers.length > 0 ? (
            <ul className="list-inside list-disc text-[10px] text-amber-300/90">
              {eligibility.blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          ) : (
            <>
              <label className="flex items-start gap-1.5 text-[10px] text-muted-foreground">
                <input
                  type="checkbox"
                  checked={ack}
                  onChange={(event) => setAck(event.target.checked)}
                  className="mt-0.5"
                />
                I understand L2 can fill live orders. The connection must be armed and
                the mandate is still capped.
              </label>
              {eligibility.warnings.map((warning) => (
                <p key={warning} className="text-[10px] text-muted-foreground/80">
                  {warning}
                </p>
              ))}
            </>
          )}
        </div>
      )}
      {error && <p className="mt-1 text-[11px] text-rose-300">{error}</p>}
    </div>
  );
}

// ── Change the pinned strategy ─────────────────────────────────────────────
// The trial is pinned to a strategy version, so changing the strategy must
// re-prepare. With a standing consent that happens without a phrase; without
// one the server says so and the operator authorizes first.
function ChangeStrategy({
  bot,
  draft,
  onDone,
}: {
  bot: AutomationView;
  draft: TradingDraft;
  onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const change = async (item: { id: string; latestVersion: string }) => {
    setBusy(true);
    setError(null);
    try {
      await updateAutomation(bot.id, {
        action: {
          ...bot.action,
          config: {
            ...bot.action.config,
            strategyId: item.id,
            strategyVersion: item.latestVersion,
          } as unknown as Record<string, unknown>,
        },
      });
      await prepareBotTrading(bot.id, {
        strategyId: item.id,
        strategyVersion: item.latestVersion,
        mode: draft.mode,
        connectionId: draft.connectionId,
        symbols: parseSymbols(draft.symbolsText),
        timeframe: draft.timeframe,
        volume: draft.volume,
        stopLossPoints: draft.stopLossPoints,
        takeProfitPoints: draft.takeProfitPoints,
        maxOrders: draft.maxOrdersPerDay,
        maxMargin: draft.maxMarginPerOrder,
        confirmation: "",
      });
      setOpen(false);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not change the strategy");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-border/15 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] font-semibold">
          Strategy pin{" "}
          <span className="font-mono text-[10px] text-muted-foreground">
            {draft.strategyId || "none"}@{draft.strategyVersion || "?"}
          </span>
        </div>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="text-[11px] text-sky-300 hover:underline"
        >
          {open ? "Cancel" : "Change"}
        </button>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        Changing it re-pins the trial and re-prepares under the existing consent.
      </p>
      {open && (
        <div className="mt-2">
          <StrategyPicker
            strategyId={draft.strategyId}
            strategyVersion={draft.strategyVersion}
            disabled={busy}
            onSelect={(item) => void change(item)}
          />
          {busy && <p className="mt-1 text-[11px] text-muted-foreground">Re-preparing…</p>}
          {error && <p className="mt-1 text-[11px] text-rose-300">{error}</p>}
        </div>
      )}
    </div>
  );
}

// ── One-time authorization ──────────────────────────────────────────────────
// The seed creates a SHADOW bot. This form is how the operator gives it money
// authority once: pick demo/live, type the server's phrase, done. The grant is
// stored on the bot, so later re-preparations (after an edit or a lesson) do not
// ask again.
function AuthorizeTrading({
  bot,
  draft,
  onDone,
}: {
  bot: AutomationView;
  draft: TradingDraft;
  onDone: () => void;
}) {
  const status = consentStatus(draft.consent);
  const active = status === "active";
  const [open, setOpen] = useState(!active);
  const [mode, setMode] = useState<"demo" | "live">(draft.mode === "live" ? "live" : "demo");
  const [connections, setConnections] = useState<
    Array<{
      id: string;
      label: string;
      mode: string;
      armedUntil?: string | null;
      status?: string;
      statusReason?: string | null;
    }>
  >([]);
  const [connectionsLoading, setConnectionsLoading] = useState(false);
  const [connectionId, setConnectionId] = useState(draft.connectionId);
  const [phrase, setPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadConnections = useCallback(async () => {
    setConnectionsLoading(true);
    setError(null);
    try {
      const list = await listBrokerConnections();
      setConnections(
        list.map((item) => ({
          id: item.id,
          label: item.label,
          mode: item.mode,
          armedUntil: item.armedUntil,
          status: item.status,
          statusReason: item.statusReason,
        })),
      );
    } catch (err) {
      setConnections([]);
      setError(err instanceof Error ? err.message : "could not read broker connections");
    } finally {
      setConnectionsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void loadConnections();
  }, [open, loadConnections]);

  // Preselect the last venue the operator used, only when this bot has none.
  useEffect(() => {
    if (!open || connectionId || connections.length === 0) return;
    const remembered = loadLastVenue();
    const initial = pickInitialVenue({
      currentId: connectionId,
      mode,
      remembered,
      available: connections,
    });
    if (initial) setConnectionId(initial);
  }, [open, connectionId, connections, mode]);

  const connection = connections.find((item) => item.id === connectionId);
  const expected = expectedConsentPhrase(
    { ...draft, mode },
    connection?.label ?? "the connection",
  );
  const tier = bot.executionAuthority?.tier ?? "l0";
  const blockReason = authorizeBlockReason({
    mode,
    tier,
    connection: connection ?? null,
  });

  const authorize = async () => {
    if (busy) return;
    if (blockReason) {
      setError(blockReason);
      return;
    }
    const grant = grantConsent({
      modes: [mode],
      limits: {
        symbols: draft.symbolsText.split(/[\s,;]+/).filter(Boolean),
        maxOrdersPerDay: draft.maxOrdersPerDay,
        maxMarginPerOrder: draft.maxMarginPerOrder,
        maxOpenPositions: draft.maxOpenPositions,
      },
      phrase,
      expectedPhrase: expected,
      autoRenew: draft.autoRenew,
    });
    if (!grant.consent) {
      setError(grant.error);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const authority = bot.executionAuthority
        ? { ...bot.executionAuthority }
        : { ...buildTradingAuthority(draft) };
      // Demo is L1's rung. Live is granted by this explicit authorization; the
      // connection must already be armed and the typed phrase is checked.
      if (mode === "demo" && authority.tier === "l0") authority.tier = "l1";
      if (mode === "live") authority.tier = "l2";
      await updateAutomation(bot.id, {
        action: {
          ...bot.action,
          config: {
            ...bot.action.config,
            mode,
            connectionId,
            dryRun: false,
          } as unknown as Record<string, unknown>,
        },
        executionAuthority: authority,
      });
      await prepareBotTrading(bot.id, {
        strategyId: draft.strategyId,
        strategyVersion: draft.strategyVersion,
        mode,
        connectionId,
        symbols: parseSymbols(draft.symbolsText),
        timeframe: draft.timeframe,
        volume: draft.volume,
        stopLossPoints: draft.stopLossPoints,
        takeProfitPoints: draft.takeProfitPoints,
        maxOrders: draft.maxOrdersPerDay,
        maxMargin: draft.maxMarginPerOrder,
        confirmation: phrase,
      });
      setPhrase("");
      setOpen(false);
      rememberVenue({ id: connectionId, mode });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not authorize trading");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-border/15 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold">Trading authorization</div>
        <div className="flex items-center gap-2">
          <Badge
            label={active ? `authorized · ${draft.mode}` : status}
            tone={active ? "positive" : "warning"}
          />
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            className="text-[11px] text-sky-300 hover:underline"
          >
            {open ? "Hide" : active ? "Change" : "Authorize"}
          </button>
        </div>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        Type the phrase once. The bot then renews its own bounded mandate — it never
        asks again until the consent expires, you revoke it, or the bot is edited.
      </p>

      {open && (
        <div className="mt-2 space-y-2">
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-[11px] text-muted-foreground">Mode</span>
              <select
                className="w-full rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-xs"
                value={mode}
                onChange={(event) => {
                  setMode(event.target.value as "demo" | "live");
                  setConnectionId("");
                }}
              >
                <option value="demo">Demo account</option>
                <option value="live">Live account — real money</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
                Venue
                <button
                  type="button"
                  onClick={() => void loadConnections()}
                  disabled={connectionsLoading}
                  aria-label="Refresh broker connections"
                  className="rounded p-0.5 hover:bg-white/5 disabled:opacity-50"
                >
                  <RefreshCw size={11} className={cn(connectionsLoading && "animate-spin")} />
                </button>
              </span>
              <select
                className="w-full rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-xs"
                value={connectionId}
                onChange={(event) => {
                  const next = event.target.value;
                  setConnectionId(next);
                  const row = connections.find((item) => item.id === next);
                  if (row) rememberVenue({ id: row.id, mode: row.mode });
                }}
              >
                <option value="">Choose a {mode} connection…</option>
                {connections
                  .filter((item) => item.mode === mode)
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.label}
                      {item.status === "unavailable" ? " · unavailable" : ""}
                      {item.armedUntil && new Date(item.armedUntil).getTime() > Date.now()
                        ? " · armed"
                        : ""}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          {connection?.status === "unavailable" && (
            <p className="text-[11px] text-amber-300">
              {connection.statusReason || "This broker connection is not available."}
            </p>
          )}
          {!connectionsLoading && connections.filter((item) => item.mode === mode).length === 0 && (
            <p className="text-[11px] text-amber-300">
              No {mode} connection yet. Add one in Settings › Trading.
            </p>
          )}
          {blockReason && (
            <p className="text-[11px] text-amber-300">{blockReason}</p>
          )}
          <code className="block rounded bg-black/30 px-2 py-1.5 text-[11px] text-amber-200">
            {expected}
          </code>
          <div className="flex flex-wrap gap-2">
            <input
              className="min-w-40 flex-1 rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-xs"
              value={phrase}
              placeholder="Type the phrase exactly"
              onChange={(event) => setPhrase(event.target.value)}
            />
            <button
              type="button"
              disabled={busy || Boolean(blockReason)}
              onClick={() => void authorize()}
              className="rounded-lg bg-emerald-500/15 px-2.5 py-1.5 text-[11px] font-semibold text-emerald-200 hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? "Authorizing…" : mode === "live" ? "Authorize live" : "Authorize demo"}
            </button>
          </div>
          {mode === "live" && (
            <p className="text-[11px] text-amber-300">
              Live also needs an armed connection. Positions are never closed automatically.
            </p>
          )}
          {error && <p className="text-[11px] text-rose-300">{error}</p>}
        </div>
      )}
    </div>
  );
}
