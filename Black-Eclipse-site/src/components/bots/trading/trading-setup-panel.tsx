"use client";

// ── Trading setup panel (bot creation) ──────────────────────────────────────
// One place where the operator answers: what does this bot trade, where, how
// much, how often, and who may it act for? Everything it shows comes from the
// pure domain modules, so the numbers here are the numbers the server enforces.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ImagePlus, ShieldCheck, Trash2, AlertTriangle, RefreshCw } from "@/lib/icons";
import { backtestClient, type StrategySummary } from "@/lib/backtest/backtest-client";
import { listBrokerConnections } from "@/lib/harness/clients/trading-client";
import { useStrategyCatalog, refreshStrategyCatalog, isBacktestComplete } from "@/lib/strategies/catalog";
import { authorizeBlockReason } from "@/lib/bots/trading/authorize";
import { loadLastVenue, pickInitialVenue, rememberVenue } from "@/lib/bots/trading/venue-memory";
import { StrategyPicker } from "@/components/bots/trading/strategy-picker";
import {
  APPETITE_BANDS,
  effectiveEnvelope,
  expectedConsentPhrase,
  grantConsent,
  revokeConsent,
  consentStatus,
  addVisual,
  removeVisual,
  updateVisualCaption,
  visualSummary,
  type TradingConsent,
  type TradingContext,
  type TradingDraft,
  type TradingIssue,
} from "@/lib/bots/trading";
import { cn } from "@/lib/utils";

const inputCls =
  "w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring dark:bg-input/30";
const labelCls = "mb-1 block text-[11px] font-medium text-muted-foreground";

interface Props {
  draft: TradingDraft;
  consent: TradingConsent | null;
  issues: TradingIssue[];
  disabled?: boolean;
  /** A strategy bot pins a strategy; an agent bot uses its own tools. */
  brain?: "strategy" | "agent";
  onChange: (patch: Partial<TradingDraft>) => void;
  onConsent: (consent: TradingConsent | null) => void;
  onContext: (context: TradingContext) => void;
}

interface BrokerRow {
  id: string;
  label: string;
  mode: string;
  armedUntil?: string | null;
  status?: string;
  statusReason?: string | null;
}

export function TradingSetupPanel({
  draft,
  consent,
  issues,
  disabled,
  brain = "strategy",
  onChange,
  onConsent,
  onContext,
}: Props) {
  const catalog = useStrategyCatalog();
  const [connections, setConnections] = useState<BrokerRow[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [warmup, setWarmup] = useState(false);
  const [phraseError, setPhraseError] = useState<string | null>(null);
  const [visualError, setVisualError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadConnections = useCallback(async () => {
    setConnectionsLoading(true);
    setConnectionsError(null);
    try {
      const rows = await listBrokerConnections();      setConnections(
        rows.map((item) => ({
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
      setConnectionsError(
        err instanceof Error ? err.message : "Could not read broker connections.",
      );
    } finally {
      setConnectionsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConnections();
  }, [loadConnections]);

  // Always read the catalog fresh when the picker opens: an agent may have
  // saved a strategy since the last visit.
  useEffect(() => {
    void refreshStrategyCatalog();
  }, []);

  // A cold Python runtime is common right after launch; say so instead of
  // showing an unexplained empty list.
  useEffect(() => {
    if (!connectionsLoading) {
      setWarmup(false);
      return;
    }
    const timer = setTimeout(() => setWarmup(true), 3000);
    return () => clearTimeout(timer);
  }, [connectionsLoading]);

  useEffect(() => {
    onContext({
      strategies: catalog.items.map((item) => ({
        id: item.id,
        name: item.name,
        version: item.latestVersion,
        hasBacktest: isBacktestComplete(item.latestBacktest?.status),
        approved: item.approval?.status === "approved",
      })),
      connections: connections.map(({ id, label, mode, armedUntil }) => ({
        id,
        label,
        mode,
        armedUntil,
      })),
      venueSymbols: null,
    });
  }, [catalog.items, connections, onContext]);

  // Preselect the last venue, only when the draft has none (never overwrite a
  // configured bot) and the remembered one still exists in that mode.
  const preselected = useRef(false);
  useEffect(() => {
    if (preselected.current || connections.length === 0 || draft.connectionId) return;
    if (draft.mode === "shadow") return;
    preselected.current = true;
    const initial = pickInitialVenue({
      currentId: draft.connectionId,
      mode: draft.mode,
      remembered: loadLastVenue(),
      available: connections,
    });
    if (initial) onChange({ connectionId: initial });
  }, [connections, draft.connectionId, draft.mode, onChange]);

  const connection = useMemo(
    () => connections.find((item) => item.id === draft.connectionId),
    [connections, draft.connectionId],
  );
  const grantBlock = authorizeBlockReason({
    mode: draft.mode,
    tier: draft.tier,
    connection: connection ?? null,
  });
  const strategy = useMemo(
    () => catalog.items.find((item) => item.id === draft.strategyId),
    [catalog.items, draft.strategyId],
  );

  const phrase = expectedConsentPhrase(
    draft,
    connection?.label ?? "your connection",
    brain,
  );
  const status = consentStatus(consent);
  const envelope = effectiveEnvelope(
    {
      strategyId: draft.strategyId,
      strategyVersion: draft.strategyVersion,
      mode: draft.mode,
      connectionId: draft.connectionId,
      symbols: [],
      excludedSymbols: [],
      dynamicSymbols: draft.dynamicSymbols,
      maxSymbols: draft.maxSymbols,
      timeframe: draft.timeframe,
      volume: draft.volume,
      stopLossPoints: draft.stopLossPoints,
      takeProfitPoints: draft.takeProfitPoints,
      maxOrdersPerDay: draft.maxOrdersPerDay,
      session: null,
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
    },
    {
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
  );

  const selectStrategy = useCallback(
    async (item: StrategySummary) => {
      onChange({ strategyId: item.id, strategyVersion: item.latestVersion });
      try {
        const record = await backtestClient.getStrategy(item.id, item.latestVersion);
        const universe = (record.spec as { universe?: { symbols?: unknown } })?.universe
          ?.symbols;
        const symbols = Array.isArray(universe)
          ? universe.map((symbol) => String(symbol)).join(", ")
          : "";
        if (symbols) {
          onChange({
            strategyId: item.id,
            strategyVersion: record.version,
            symbolsText: symbols,
          });
        }
      } catch {
        // Choosing the strategy must not block on a spec read; the server
        // re-validates the pin at prepare time.
      }
    },
    [onChange],
  );

  const grant = () => {
    if (grantBlock) {
      setPhraseError(grantBlock);
      return;
    }
    const modes = draft.mode === "shadow" ? [] : [draft.mode as "demo" | "live"];
    const result = grantConsent({
      modes,
      limits: {
        symbols: draft.symbolsText.split(/[\s,;]+/).filter(Boolean),
        maxOrdersPerDay: draft.maxOrdersPerDay,
        maxMarginPerOrder: draft.maxMarginPerOrder,
        maxOpenPositions: draft.maxOpenPositions,
      },
      phrase: draft.consentPhrase,
      expectedPhrase: phrase,
      autoRenew: draft.autoRenew,
    });
    if (!result.consent) {
      setPhraseError(result.error);
      return;
    }
    setPhraseError(null);
    onConsent(result.consent);
  };

  const addFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    let next = draft.visuals;
    let failure: string | null = null;
    for (const file of Array.from(files).slice(0, 4)) {
      const dataUrl = await readFile(file).catch(() => null);
      if (!dataUrl) {
        failure = "That file could not be read.";
        continue;
      }
      const result = addVisual(next, { dataUrl, caption: "" });
      if (result.error) {
        failure = result.error;
        break;
      }
      next = result.visuals;
    }
    onChange({ visuals: next });
    setVisualError(failure);
    if (fileRef.current) fileRef.current.value = "";
  };

  const tradingIssues = issues.filter(
    (issue) => !issue.field.startsWith("strategy") || issue.field === "strategyId",
  );

  return (
    <div className="space-y-4 rounded-xl border border-border/15 p-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-xs font-semibold">Trading</div>
          <p className="text-[11px] text-muted-foreground">
            A strategy bot decides on the last closed bar and trades through the
            broker layer. Nothing is sent in shadow mode.
          </p>
        </div>
        <ShieldCheck size={16} className="shrink-0 text-emerald-300/80" />
      </div>

      {(connectionsError || warmup) && (
        <p className="text-[11px] text-amber-300">
          {connectionsError ??
            "Agent runtime is starting — broker connections will appear in a moment."}
          {connectionsError && (
            <button type="button" onClick={() => void loadConnections()} className="ml-1 underline">
              Retry
            </button>
          )}
        </p>
      )}

      {/* Mode + venue */}
      <div className="grid gap-2 sm:grid-cols-2">
        <div>
          <label className={labelCls} htmlFor="trading-mode">Mode</label>
          <select
            id="trading-mode"
            className={inputCls}
            value={draft.mode}
            disabled={disabled}
            onChange={(event) => {
              const mode = event.target.value as TradingDraft["mode"];
              onChange({
                mode,
                connectionId: mode === "shadow" ? "" : draft.connectionId,
                tier: mode === "live" ? ("l2" as const) : draft.tier,
              });
            }}
          >
            <option value="shadow">Shadow — decide, send nothing</option>
            <option value="demo">Demo — fill a demo account</option>
            <option value="live">Live — real money</option>
          </select>
        </div>
        <div>
          <div className="flex items-center justify-between">
            <label className={labelCls} htmlFor="trading-venue">Venue</label>
            <button
              type="button"
              onClick={() => void loadConnections()}
              disabled={connectionsLoading}
              aria-label="Refresh broker connections"
              className="mb-1 rounded p-0.5 text-muted-foreground hover:bg-white/5 hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw size={11} className={cn(connectionsLoading && "animate-spin")} />
            </button>
          </div>
          <select
            id="trading-venue"
            className={inputCls}
            value={draft.connectionId}
            disabled={disabled || draft.mode === "shadow" || connectionsLoading}
            onChange={(event) => {
              const next = event.target.value;
              onChange({ connectionId: next });
              const row = connections.find((item) => item.id === next);
              if (row) rememberVenue({ id: row.id, mode: row.mode });
            }}
          >
            <option value="">
              {draft.mode === "shadow" ? "No venue (shadow)" : "Choose a connection…"}
            </option>
            {connections
              .filter((item) => draft.mode === "shadow" || item.mode === draft.mode)
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label} · {item.mode}
                  {item.status === "unavailable" ? " · unavailable" : ""}
                  {item.armedUntil && new Date(item.armedUntil).getTime() > Date.now()
                    ? " · armed"
                    : ""}
                </option>
              ))}
          </select>
          {draft.mode !== "shadow" && !connectionsLoading &&
            connections.filter((item) => item.mode === draft.mode).length === 0 && (
              <p className="mt-1 text-[11px] text-amber-300">
                No {draft.mode} connection yet. Add one in Settings › Trading.
              </p>
            )}
          {connection?.status === "unavailable" && (
            <p className="mt-1 text-[11px] text-amber-300">
              {connection.statusReason || "This broker connection is not available."}
            </p>
          )}
          {connection?.status === "connected" && (
            <p className="mt-1 text-[11px] text-emerald-300/90">Broker connected.</p>
          )}
        </div>
      </div>

      {/* Strategy */}
      {brain === "strategy" && (
        <div>
          <StrategyPicker
            strategyId={draft.strategyId}
            strategyVersion={draft.strategyVersion}
            disabled={disabled}
            onSelect={(item) => void selectStrategy(item)}
          />
          {strategy && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              {isBacktestComplete(strategy.latestBacktest?.status)
                ? `Backtest evidence present${strategy.approval?.status === "approved" ? " and approved." : "; approval is still needed."}`
                : "No completed backtest for this version yet — the server refuses to trade it until one exists."}
            </p>
          )}
        </div>
      )}

      {/* Universe */}
      <div>
        <label className={labelCls} htmlFor="trading-symbols">
          Symbols (comma separated)
        </label>
        <input
          id="trading-symbols"
          className={inputCls}
          value={draft.symbolsText}
          disabled={disabled}
          placeholder="EURUSD, GBPUSD"
          onChange={(event) => onChange({ symbolsText: event.target.value })}
        />
        <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
          <label className="inline-flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={draft.dynamicSymbols}
              onChange={(event) => onChange({ dynamicSymbols: event.target.checked })}
            />
            Scan the universe each bar and take the best signal
          </label>
          <label className="inline-flex items-center gap-1.5">
            Scan up to
            <input
              type="number"
              min={1}
              max={32}
              value={draft.maxSymbols}
              onChange={(event) => onChange({ maxSymbols: Number(event.target.value) })}
              className="w-14 rounded border border-input bg-transparent px-1 py-0.5"
            />
            symbols
          </label>
        </div>
      </div>

      {/* Risk envelope */}
      <div className="grid gap-2 sm:grid-cols-2">
        <div>
          <label className={labelCls} htmlFor="trading-appetite">Risk appetite</label>
          <select
            id="trading-appetite"
            className={inputCls}
            value={draft.appetite}
            disabled={disabled}
            onChange={(event) =>
              onChange({ appetite: event.target.value as TradingDraft["appetite"] })
            }
          >
            <option value="conservative">Conservative · 0.25% / 1 position</option>
            <option value="normal">Normal · 0.5% / 2 positions</option>
            <option value="aggressive">Aggressive · 1% / 3 positions</option>
          </select>
        </div>
        <div>
          <label className={labelCls} htmlFor="trading-tier">Authority tier</label>
          <select
            id="trading-tier"
            className={inputCls}
            value={draft.tier}
            disabled={disabled}
            onChange={(event) =>
              onChange({ tier: event.target.value as TradingDraft["tier"] })
            }
          >
            <option value="l0">L0 — propose only</option>
            <option value="l1">L1 — fill demo</option>
            <option value="l2">L2 — fill live</option>
          </select>
          <p className="mt-1 text-[11px] text-muted-foreground">
            L2 needs an armed connection and a mandate. Raise it once the bot has a
            demo track record.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <NumberField label="Volume" value={draft.volume} step={0.01} min={0.01} onChange={(volume) => onChange({ volume })} />
        <NumberField label="Stop (pts)" value={draft.stopLossPoints} min={0} onChange={(stopLossPoints) => onChange({ stopLossPoints })} />
        <NumberField label="Target (pts)" value={draft.takeProfitPoints} min={0} onChange={(takeProfitPoints) => onChange({ takeProfitPoints })} />
        <NumberField label="Orders / day" value={draft.maxOrdersPerDay} min={1} max={20} onChange={(maxOrdersPerDay) => onChange({ maxOrdersPerDay })} />
        <NumberField label="Max positions" value={draft.maxOpenPositions} min={1} max={10} onChange={(maxOpenPositions) => onChange({ maxOpenPositions })} />
        <NumberField label="Max margin" value={draft.maxMarginPerOrder} min={0} onChange={(maxMarginPerOrder) => onChange({ maxMarginPerOrder })} />
        <NumberField label="Cooldown (s)" value={draft.cooldownS} min={0} onChange={(cooldownS) => onChange({ cooldownS })} />
        <NumberField label="Daily loss %" value={draft.maxDailyLossPct} min={0} max={50} onChange={(maxDailyLossPct) => onChange({ maxDailyLossPct })} />
      </div>

      {/* Session */}
      <div>
        <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <input
            type="checkbox"
            checked={draft.sessionEnabled}
            onChange={(event) => onChange({ sessionEnabled: event.target.checked })}
          />
          Only trade inside a session window
        </label>
        {draft.sessionEnabled && (
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            <input
              className={inputCls}
              value={draft.sessionTimezone}
              aria-label="Session timezone"
              onChange={(event) => onChange({ sessionTimezone: event.target.value })}
            />
            <input
              className={inputCls}
              type="time"
              value={draft.sessionStart}
              aria-label="Session start"
              onChange={(event) => onChange({ sessionStart: event.target.value })}
            />
            <input
              className={inputCls}
              type="time"
              value={draft.sessionEnd}
              aria-label="Session end"
              onChange={(event) => onChange({ sessionEnd: event.target.value })}
            />
          </div>
        )}
      </div>

      {/* Standing consent */}
      <div className="rounded-lg border border-border/15 p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-[11px] font-semibold">
            Standing consent{" "}
            <span
              className={cn(
                "ml-1 rounded px-1.5 py-0.5 text-[10px] uppercase",
                status === "active"
                  ? "bg-emerald-500/15 text-emerald-300"
                  : "bg-amber-500/15 text-amber-300",
              )}
            >
              {status}
            </span>
          </div>
          <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={draft.autoMode}
              onChange={(event) => onChange({ autoMode: event.target.checked })}
            />
            Auto mode (never ask again)
          </label>
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
          Type this once. The bot then renews its own bounded mandate until the
          consent expires, you revoke it, or you edit the bot.
        </p>
        {draft.mode === "shadow" ? (
          <p className="mt-2 text-[11px] text-emerald-300/90">
            Shadow mode needs no consent — it never sends an order.
          </p>
        ) : (
          <>
            <code className="mt-2 block rounded bg-black/30 px-2 py-1.5 text-[11px] text-amber-200">
              {phrase}
            </code>
            <div className="mt-2 flex flex-wrap gap-2">
              <input
                className={cn(inputCls, "flex-1 min-w-40")}
                value={draft.consentPhrase}
                placeholder="Type the phrase exactly"
                onChange={(event) => onChange({ consentPhrase: event.target.value })}
              />
              <button
                type="button"
                disabled={Boolean(grantBlock)}
                className="rounded-lg bg-white/[0.09] px-3 py-1.5 text-xs font-semibold hover:bg-white/[0.14] disabled:cursor-not-allowed disabled:opacity-50"
                onClick={grant}
              >
                Authorize
              </button>
              {consent && (
                <button
                  type="button"
                  className="rounded-lg px-3 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10"
                  onClick={() => onConsent(revokeConsent(consent))}
                >
                  Revoke
                </button>
              )}
            </div>
            {phraseError && <p className="mt-1 text-[11px] text-rose-300">{phraseError}</p>}
            {grantBlock && (
              <p className="mt-1 text-[11px] text-amber-300">{grantBlock}</p>
            )}
          </>
        )}
      </div>

      {/* Learning */}
      <div className="rounded-lg border border-border/15 p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-[11px] font-semibold">Learn from repeated failures</div>
          <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={draft.learningEnabled}
              onChange={(event) => onChange({ learningEnabled: event.target.checked })}
            />
            On
          </label>
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
          After repeated failures the bot brings you a concrete fix and waits for
          your yes. It can only tighten on its own.
        </p>
        {draft.learningEnabled && (
          <label className="mt-2 inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
            Ask me after
            <input
              type="number"
              min={2}
              max={10}
              value={draft.improveAfterFailures}
              onChange={(event) =>
                onChange({ improveAfterFailures: Number(event.target.value) })
              }
              className="w-14 rounded border border-input bg-transparent px-1 py-0.5"
            />
            failures
          </label>
        )}
      </div>

      {/* Visual teaching */}
      <div>
        <div className="flex items-center justify-between">
          <label className={labelCls}>Reference images (teach the setup)</label>
          <span className="text-[11px] text-muted-foreground">
            {visualSummary(draft.visuals)}
          </span>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          multiple
          className="hidden"
          onChange={(event) => void addFiles(event.target.files)}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-border/50 px-3 py-2 text-[11px] text-muted-foreground hover:border-border hover:text-foreground"
        >
          <ImagePlus size={14} /> Add an image (PNG, JPEG, WebP · up to 2 MB)
        </button>
        {visualError && <p className="mt-1 text-[11px] text-rose-300">{visualError}</p>}
        {draft.visuals.length > 0 && (
          <div className="mt-2 space-y-2">
            {draft.visuals.map((visual) => (
              <div key={visual.id} className="flex gap-2 rounded-lg border border-border/15 p-2">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={visual.dataUrl} alt="" className="h-14 w-14 shrink-0 rounded object-cover" />
                <input
                  className={cn(inputCls, "h-9 flex-1")}
                  value={visual.caption}
                  placeholder="What does this teach? (e.g. enter after the retest)"
                  onChange={(event) =>
                    onChange({
                      visuals: updateVisualCaption(draft.visuals, visual.id, event.target.value),
                    })
                  }
                />
                <button
                  type="button"
                  aria-label="Remove image"
                  className="shrink-0 rounded px-2 text-rose-300 hover:bg-rose-500/10"
                  onClick={() => onChange({ visuals: removeVisual(draft.visuals, visual.id) })}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Effective limits */}
      <div className="rounded-lg bg-white/[0.03] p-2.5 text-[11px] text-muted-foreground">
        <div className="mb-1 font-semibold text-foreground">What will actually apply</div>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>{envelope.riskPerTradePct}% risk / trade</span>
          <span>{envelope.maxPositions} position(s)</span>
          <span>{envelope.maxOrdersPerDay} orders/day</span>
          <span>{envelope.requireStop ? "stop required" : "stop optional"}</span>
          <span>{Math.round(envelope.cooldownS / 60)}m cooldown</span>
          <span>{APPETITE_BANDS[draft.appetite].riskPerTradePct}% band</span>
        </div>
      </div>

      {tradingIssues.length > 0 && (
        <div className="space-y-1">
          {tradingIssues.map((issue) => (
            <p
              key={`${issue.field}-${issue.code}`}
              className={cn(
                "flex items-start gap-1.5 text-[11px]",
                issue.severity === "error" ? "text-rose-300" : "text-amber-300",
              )}
            >
              <AlertTriangle size={11} className="mt-0.5 shrink-0" />
              {issue.message}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <label className="block">
      <span className={labelCls}>{label}</span>
      <input
        type="number"
        className={inputCls}
        value={Number.isFinite(value) ? value : 0}
        min={min}
        max={max}
        step={step}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("read failed"));
    reader.readAsDataURL(file);
  });
}
