"use client";

// ── Trading & brokers settings ───────────────────────────────────────────────
// Connect any registered broker (MetaTrader 5, the MT4 bridge, or a REST venue),
// probe it, read the account and manage the live arm / kill switch. The form is
// shared with the sidebar dialog so both surfaces list the same registry kinds.
// Trading actions themselves stay on the operator routes and Graph Studio; this
// panel never places an order.

import { useCallback, useEffect, useState } from "react";
import { confirmDialog } from "@/lib/ui/confirm-dialog";
import { Handshake, Loader2, RefreshCw, Trash2, Wallet } from "@/lib/icons";
import { cn } from "@/lib/utils";
import { Field, Panel, SectionHeader } from "./settings-ui";
import {
  ConnectionForm,
  type BrokerKindId,
} from "@/components/layout/broker-connection-form";
import { BROKER_RESPONSIBILITY_NOTICE } from "@/lib/legal/disclosure";
import {
  armBrokerConnection,
  createBrokerConnection,
  createTradingMandate,
  deleteBrokerConnection,
  disarmBrokerConnection,
  getBrokerAccount,
  killBrokerConnection,
  listBrokerConnections,
  listBrokerKinds,
  listTradingMandates,
  revokeTradingMandate,
  syncBrokerConnection,
  testBrokerConnection,
  updateBrokerConnection,
  type BrokerConnection,
  type BrokerKind,
  type TradingMandate,
} from "@/lib/harness/clients/trading-client";

const DEFAULT_TERMINAL = "C:\\Program Files\\MetaTrader 5\\terminal64.exe";
const inputClass =
  "w-full rounded-xl border border-border/40 bg-transparent px-2.5 py-1.5 text-xs text-foreground focus:border-primary/40 focus:outline-none";

export function TradingSettingsPanel() {
  const [kinds, setKinds] = useState<BrokerKind[]>([]);
  const [connections, setConnections] = useState<BrokerConnection[]>([]);
  const [mandates, setMandates] = useState<TradingMandate[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [kind, setKind] = useState<BrokerKindId>("mt5");
  const [label, setLabel] = useState("");
  const [mode, setMode] = useState<"demo" | "live">("demo");
  const [terminalPath, setTerminalPath] = useState(DEFAULT_TERMINAL);
  const [bridgeUrl, setBridgeUrl] = useState("http://127.0.0.1:8090");
  const [bridgeSecretRef, setBridgeSecretRef] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [dataUrl, setDataUrl] = useState("");
  const [credentialRef, setCredentialRef] = useState("");
  const [brokerAccountId, setBrokerAccountId] = useState("");
  const [quoteAsset, setQuoteAsset] = useState("");
  const [autoLaunch, setAutoLaunch] = useState(false);
  const [magic, setMagic] = useState("0");
  const [maxMargin, setMaxMargin] = useState("0");
  const [phrases, setPhrases] = useState<Record<string, string>>({});
  const [mandateSymbols, setMandateSymbols] = useState("EURUSD");
  const [mandateOrders, setMandateOrders] = useState("2");
  const [mandateMargin, setMandateMargin] = useState("50");
  const [mandateTtl, setMandateTtl] = useState("3600");
  const [mandatePhrase, setMandatePhrase] = useState("");
  const [mandateWorkflowId, setMandateWorkflowId] = useState("");
  const [mandateWorkflowVersion, setMandateWorkflowVersion] = useState("1");

  const load = useCallback(async () => {
    setError(null);
    try {
      const [nextKinds, nextConnections, nextMandates] = await Promise.all([
        listBrokerKinds(),
        listBrokerConnections(),
        listTradingMandates(),
      ]);
      setKinds(nextKinds);
      setConnections(nextConnections);
      setMandates(nextMandates);
      // Never leave the form on a kind this host cannot load: the default is
      // the first available adapter, and an explicit user pick is preserved.
      setKind((prev) => {
        const current = nextKinds.find((entry) => entry.id === prev);
        if (current?.available) return prev;
        return (
          nextKinds.find((entry) => entry.available)?.id ??
          nextKinds[0]?.id ??
          prev
        );
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "broker settings unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (key: string, action: () => Promise<unknown>, done?: string) => {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await action();
      await load();
      if (done) setNotice(done);
    } catch (err) {
      setError(err instanceof Error ? err.message : "the action failed");
    } finally {
      setBusy(null);
    }
  };

  /**
   * Config always follows the selected kind's auth style. A kind never inherits
   * another adapter's endpoint or credential fields.
   */
  const buildConfig = (): Record<string, unknown> => {
    const config: Record<string, unknown> = {};
    if (kind === "mt4_bridge") {
      if (bridgeUrl.trim()) config.bridgeUrl = bridgeUrl.trim();
      if (bridgeSecretRef.trim()) config.bridgeSecretRef = bridgeSecretRef.trim();
    } else if (kind === "mt5") {
      if (terminalPath.trim()) config.terminalPath = terminalPath.trim();
      config.autoLaunchTerminal = autoLaunch;
    } else {
      if (baseUrl.trim()) config.baseUrl = baseUrl.trim();
      if (kind === "alpaca" && dataUrl.trim()) config.dataUrl = dataUrl.trim();
      if (brokerAccountId.trim()) config.accountId = brokerAccountId.trim();
      if (credentialRef.trim()) config.credentialRef = credentialRef.trim();
      if (quoteAsset.trim()) config.quoteAsset = quoteAsset.trim().toUpperCase();
    }
    const magicNumber = Number(magic);
    const marginNumber = Number(maxMargin);
    if (Number.isFinite(magicNumber) && magicNumber > 0) config.magic = magicNumber;
    if (Number.isFinite(marginNumber) && marginNumber > 0) config.maxMargin = marginNumber;
    return config;
  };

  const add = async () => {
    await run("create", async () => {
      const fallbackLabel = kinds.find((entry) => entry.id === kind)?.label ?? kind;
      const connection = await createBrokerConnection({
        kind,
        label: label.trim() || fallbackLabel,
        mode,
        config: buildConfig(),
      });
      await testBrokerConnection(connection.id);
    }, "Connection added.");
  };

  const test = (connection: BrokerConnection) =>
    run(`test:${connection.id}`, async () => {
      await testBrokerConnection(connection.id);
      await getBrokerAccount(connection.id).catch(() => null);
    }, "Connection tested.");

  const sync = (connection: BrokerConnection) =>
    run(`sync:${connection.id}`, () => syncBrokerConnection(connection.id), "Sync complete.");

  const remove = async (connection: BrokerConnection) => {
    if (!(await confirmDialog({ title: `Remove ${connection.label}?`, description: "This does not close anything at the broker.", tone: "danger" }))) {
      return;
    }
    void run(`delete:${connection.id}`, () => deleteBrokerConnection(connection.id), "Connection removed.");
  };

  const arm = (connection: BrokerConnection) =>
    run(
      `arm:${connection.id}`,
      () => armBrokerConnection(connection.id, phrases[connection.id] ?? "", 1800),
      "Live execution armed for 30 minutes.",
    );

  const disarm = (connection: BrokerConnection) =>
    run(`disarm:${connection.id}`, () => disarmBrokerConnection(connection.id), "Live execution disarmed.");

  const kill = async (connection: BrokerConnection) => {
    if (!(await confirmDialog({ title: "Cancel every working order Eclipse owns on this connection?", tone: "danger" }))) return;
    void run(`kill:${connection.id}`, () => killBrokerConnection(connection.id), "Kill switch fired.");
  };

  const createMandate = () => {
    const connection = connections[0];
    if (!connection) return;
    void run(
      "mandate:create",
      () =>
        createTradingMandate(connection.id, {
          symbols: mandateSymbols
            .split(",")
            .map((symbol) => symbol.trim().toUpperCase())
            .filter(Boolean),
          maxOrders: Number(mandateOrders) || 1,
          maxMargin: Number(mandateMargin) || 0,
          ttlSeconds: Number(mandateTtl) || 3600,
          confirmation: mandatePhrase,
          note: "Settings autopilot mandate",
          ...(connection.mode === "live"
            ? {
                workflowId: mandateWorkflowId.trim(),
                workflowVersion: Number(mandateWorkflowVersion) || 1,
              }
            : {}),
        }),
      `Mandate created for ${connection.mode}. Pin it in the matching Graph Studio broker node.`,
    );
  };

  const revokeMandate = async (mandate: TradingMandate) => {
    if (!(await confirmDialog({ title: `Revoke mandate ${mandate.id}?`, tone: "danger" }))) return;
    void run(`mandate:revoke:${mandate.id}`, () => revokeTradingMandate(mandate.id), "Mandate revoked.");
  };

  const availableKinds = kinds.filter((entry) => entry.available);
  const unavailableKinds = kinds.filter((entry) => !entry.available);
  return (
    <div className="max-w-3xl space-y-6">
      <SectionHeader
        icon={Handshake}
        title="Broker connection"
        description="Connect any broker the runtime registers — MetaTrader 5, the MT4 bridge, or a REST venue. Reads are always safe; live execution requires a typed arm and a per-order confirmation."
      />

      <Panel className="p-4">
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          {BROKER_RESPONSIBILITY_NOTICE}
        </p>
      </Panel>

      {error && <p className="text-xs text-rose-300">{error}</p>}
      {notice && <p className="text-xs text-emerald-300">{notice}</p>}

      <Panel className="p-5 space-y-2">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-xs font-semibold text-foreground">Broker adapters</p>
            <p className="text-[11px] text-muted-foreground">
              {loading
                ? "Checking the runtime…"
                : `${availableKinds.length} of ${kinds.length} registered brokers are ready on this runtime.`}
            </p>
          </div>
          <span
            className={cn(
              "rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
              availableKinds.length > 0
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                : "border-amber-500/30 bg-amber-500/10 text-amber-300",
            )}
          >
            {availableKinds.length > 0 ? "ready" : "none ready"}
          </span>
        </div>
        {unavailableKinds.length > 0 && (
          <div className="space-y-1">
            {unavailableKinds.map((entry) => (
              <p key={entry.id} className="text-[10px] text-amber-200/80">
                <span className="font-medium text-foreground">{entry.label}</span> —{" "}
                {entry.unavailableReason ?? "unavailable on this host"}
              </p>
            ))}
          </div>
        )}
      </Panel>

      {loading ? (
        <Panel className="p-6">
          <p className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={14} className="animate-spin" /> Loading connections…
          </p>
        </Panel>
      ) : (
        connections.map((connection) => {
          const account = connection.account;
          const armed = Boolean(connection.armedUntil);
          return (
            <Panel key={connection.id} className="p-5 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="text-xs font-semibold text-foreground">{connection.label}</p>
                  <p className="text-[11px] text-muted-foreground">
                    {connection.kind.toUpperCase()} · {connection.mode.toUpperCase()} ·{" "}
                    {connection.status}
                    {connection.statusReason ? ` — ${connection.statusReason}` : ""}
                  </p>
                </div>
                <span
                  className={cn(
                    "rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
                    account?.mode === "real"
                      ? "border-rose-500/30 bg-rose-500/10 text-rose-300"
                      : "border-sky-500/30 bg-sky-500/10 text-sky-300",
                  )}
                >
                  {account?.mode ?? "unknown"}
                </span>
              </div>

              {account ? (
                <div className="grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-4">
                  <Stat label="Login" value={String(account.login)} />
                  <Stat label="Balance" value={`${account.balance.toLocaleString()} ${account.currency}`} />
                  <Stat label="Equity" value={account.equity.toLocaleString()} />
                  <Stat label="Free margin" value={account.marginFree.toLocaleString()} />
                  <Stat label="Server" value={account.server || "—"} />
                  <Stat label="Leverage" value={`1:${account.leverage}`} />
                  <Stat label="Margin mode" value={account.marginMode} />
                  <Stat label="Trading" value={account.tradeAllowed ? "allowed" : "disabled"} />
                </div>
              ) : (
                <p className="text-[11px] text-muted-foreground">
                  No account read yet. Test the connection.
                </p>
              )}

              {connection.mode === "live" && (
                <div className="rounded-xl border border-rose-500/20 bg-rose-500/5 p-3 space-y-2">
                  <p className="text-[11px] text-rose-200">
                    Live execution is off until armed. Type{" "}
                    <span className="font-mono">ARM LIVE {account?.login ?? "<login>"}</span> to arm
                    for 30 minutes. Every order also needs its own <span className="font-mono">LIVE …</span>{" "}
                    confirmation and a single-use approval.
                  </p>
                  <div className="flex gap-2">
                    <input
                      value={phrases[connection.id] ?? ""}
                      onChange={(event) =>
                        setPhrases((current) => ({ ...current, [connection.id]: event.target.value }))
                      }
                      placeholder={`ARM LIVE ${account?.login ?? ""}`}
                      className={inputClass}
                    />
                    <button
                      type="button"
                      disabled={busy === `arm:${connection.id}`}
                      onClick={() => void arm(connection)}
                      className="shrink-0 rounded-xl border border-rose-500/40 px-3 py-1.5 text-[11px] text-rose-200 hover:bg-rose-500/10 disabled:opacity-60"
                    >
                      Arm
                    </button>
                  </div>
                  {armed && (
                    <p className="text-[10px] text-rose-200/80">
                      Armed until {connection.armedUntil}. Disarm or fire the kill switch at any time.
                    </p>
                  )}
                </div>
              )}

              <RiskLimits connection={connection} onSaved={load} />
              <ConnectionEditor connection={connection} kinds={kinds} onSaved={load} />

              <div className="flex flex-wrap items-center gap-2">
                <Action busy={busy === `test:${connection.id}`} onClick={() => void test(connection)}>
                  Test
                </Action>
                <Action busy={busy === `sync:${connection.id}`} onClick={() => void sync(connection)}>
                  <RefreshCw size={12} /> Sync
                </Action>
                {connection.mode === "live" && armed && (
                  <Action busy={busy === `disarm:${connection.id}`} onClick={() => void disarm(connection)}>
                    Disarm
                  </Action>
                )}
                {connection.mode === "live" && (
                  <Action busy={busy === `kill:${connection.id}`} onClick={() => kill(connection)}>
                    Kill switch
                  </Action>
                )}
                <Action busy={busy === `delete:${connection.id}`} onClick={() => remove(connection)}>
                  <Trash2 size={12} /> Remove
                </Action>
              </div>
            </Panel>
          );
        })
      )}

      {connections[0] && (
        <Panel className="p-5 space-y-4">
          <div className="flex items-center gap-2">
            <Handshake size={14} className="text-muted-foreground" />
            <div>
              <p className="text-xs font-semibold text-foreground">Autopilot mandates</p>
              <p className="text-[10px] text-muted-foreground">
                A bounded authorization an unattended graph can submit under. A live mandate also
                requires the connection to be armed and pins the workflow version it authorizes.
              </p>
            </div>
          </div>

          {mandates.filter((m) => !m.revokedAt && m.remainingOrders > 0).length === 0 ? (
            <p className="text-[11px] text-muted-foreground">
              No active mandate. Create one below, then ask Eclipse to analyze and trade under it.
            </p>
          ) : (
            <div className="space-y-2">
              {mandates
                .filter((m) => !m.revokedAt && m.remainingOrders > 0)
                .map((mandate) => (
                  <div
                    key={mandate.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/20 px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="truncate font-mono text-[10px] text-foreground">{mandate.id}</p>
                      <p className="text-[10px] text-muted-foreground">
                        {mandate.mode ?? "demo"} · {mandate.symbols.join(", ")} ·{" "}
                        {mandate.remainingOrders}/{mandate.maxOrders} orders · max margin{" "}
                        {mandate.maxMargin} · expires {new Date(mandate.expiresAt).toLocaleString()}
                        {mandate.workflowId ? ` · workflow ${mandate.workflowId}@${mandate.workflowVersion}` : ""}
                      </p>
                    </div>
                    <Action
                      busy={busy === `mandate:revoke:${mandate.id}`}
                      onClick={() => revokeMandate(mandate)}
                    >
                      Revoke
                    </Action>
                  </div>
                ))}
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Symbols" hint="Comma-separated, e.g. EURUSD, GBPUSD.">
              <input value={mandateSymbols} onChange={(event) => setMandateSymbols(event.target.value)} className={inputClass} />
            </Field>
            <Field label="Max orders">
              <input value={mandateOrders} onChange={(event) => setMandateOrders(event.target.value)} className={inputClass} />
            </Field>
            <Field label="Max margin per order">
              <input value={mandateMargin} onChange={(event) => setMandateMargin(event.target.value)} className={inputClass} />
            </Field>
            <Field label="TTL (seconds)">
              <input value={mandateTtl} onChange={(event) => setMandateTtl(event.target.value)} className={inputClass} />
            </Field>
            {connections[0].mode === "live" && (
              <>
                <Field label="Workflow id" hint="The live mandate is pinned to this exact workflow.">
                  <input
                    value={mandateWorkflowId}
                    onChange={(event) => setMandateWorkflowId(event.target.value)}
                    className={inputClass}
                    placeholder="workflow row id"
                  />
                </Field>
                <Field label="Workflow version">
                  <input
                    value={mandateWorkflowVersion}
                    onChange={(event) => setMandateWorkflowVersion(event.target.value)}
                    className={inputClass}
                  />
                </Field>
              </>
            )}
            <Field
              label="Typed confirmation"
              hint={`Type MANDATE ${connections[0].mode.toUpperCase()} ${connections[0].label} exactly.`}
            >
              <input
                value={mandatePhrase}
                onChange={(event) => setMandatePhrase(event.target.value)}
                placeholder={`MANDATE ${connections[0].mode.toUpperCase()} ${connections[0].label}`}
                className={inputClass}
              />
            </Field>
          </div>
          <button
            type="button"
            disabled={busy === "mandate:create"}
            onClick={createMandate}
            className="inline-flex items-center gap-2 rounded-xl border border-primary/40 bg-primary/10 px-3 py-1.5 text-[11px] font-medium text-primary hover:bg-primary/15 disabled:opacity-60"
          >
            {busy === "mandate:create" ? <Loader2 size={12} className="animate-spin" /> : <Handshake size={12} />}
            Create demo mandate
          </button>
        </Panel>
      )}

      <Panel className="p-5 space-y-4">
        <div className="flex items-center gap-2">
          <Wallet size={14} className="text-muted-foreground" />
          <div>
            <p className="text-xs font-semibold text-foreground">Add a broker connection</p>
            <p className="text-[10px] text-muted-foreground">
              Pick any registered kind. The fields follow that broker's auth style; a credential is
              a NAME (env var or secret:// ref), never a value.
            </p>
          </div>
        </div>
        {kinds.length === 0 && !loading ? (
          <p className="text-[11px] text-amber-200/80">
            The broker registry is unavailable. Start the Python runtime and retry.
          </p>
        ) : (
          <ConnectionForm
            kind={kind}
            setKind={setKind}
            kinds={kinds}
            allowKindChange
            label={label}
            setLabel={setLabel}
            mode={mode}
            setMode={setMode}
            terminalPath={terminalPath}
            setTerminalPath={setTerminalPath}
            bridgeUrl={bridgeUrl}
            setBridgeUrl={setBridgeUrl}
            bridgeSecretRef={bridgeSecretRef}
            setBridgeSecretRef={setBridgeSecretRef}
            baseUrl={baseUrl}
            setBaseUrl={setBaseUrl}
            dataUrl={dataUrl}
            setDataUrl={setDataUrl}
            credentialRef={credentialRef}
            setCredentialRef={setCredentialRef}
            brokerAccountId={brokerAccountId}
            setBrokerAccountId={setBrokerAccountId}
            quoteAsset={quoteAsset}
            setQuoteAsset={setQuoteAsset}
            magic={magic}
            setMagic={setMagic}
            maxMargin={maxMargin}
            setMaxMargin={setMaxMargin}
            busy={busy === "create"}
            onSubmit={() => void add()}
            submitLabel="Add & test connection"
          />
        )}
        {kind === "mt5" && <AutoLaunchToggle checked={autoLaunch} onChange={setAutoLaunch} />}
      </Panel>
    </div>
  );
}

function RiskLimits({
  connection,
  onSaved,
}: {
  connection: BrokerConnection;
  onSaved: () => Promise<void>;
}) {
  const config = connection.config ?? {};
  const book = (config.risk ?? {}) as Record<string, number | undefined>;
  const [risk, setRisk] = useState(String(config.riskPerTradePct ?? 0));
  const [daily, setDaily] = useState(String(config.maxDailyLossPct ?? 0));
  const [notional, setNotional] = useState(String(config.maxSymbolNotional ?? 0));
  // Account-level limits read by `harness/trade/risk.py` on every plan/preview.
  const [bookRisk, setBookRisk] = useState(String(book.maxRiskPct ?? 2));
  const [openMax, setOpenMax] = useState(String(book.maxOpenPositions ?? 10));
  const [symbolMax, setSymbolMax] = useState(String(book.maxSymbolPositions ?? 3));
  const [marginMax, setMarginMax] = useState(String(book.maxMarginPct ?? 50));
  const [levelMin, setLevelMin] = useState(String(book.minMarginLevelPct ?? 150));
  const [spreadMax, setSpreadMax] = useState(String(book.maxSpreadPctOfRisk ?? 25));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const next: Record<string, unknown> = { ...config };
      const fields: Array<[string, string]> = [
        ["riskPerTradePct", risk],
        ["maxDailyLossPct", daily],
        ["maxSymbolNotional", notional],
      ];
      for (const [key, raw] of fields) {
        const value = Number(raw);
        if (Number.isFinite(value) && value > 0) next[key] = value;
        else delete next[key];
      }
      const nextBook: Record<string, number> = {};
      const bookFields: Array<[string, string]> = [
        ["maxRiskPct", bookRisk],
        ["maxOpenPositions", openMax],
        ["maxSymbolPositions", symbolMax],
        ["maxMarginPct", marginMax],
        ["minMarginLevelPct", levelMin],
        ["maxSpreadPctOfRisk", spreadMax],
      ];
      for (const [key, raw] of bookFields) {
        const value = Number(raw);
        if (Number.isFinite(value) && value > 0) nextBook[key] = value;
      }
      if (Object.keys(nextBook).length) next.risk = nextBook;
      else delete next.risk;
      await updateBrokerConnection(connection.id, { config: next });
      await onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not save risk limits");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 rounded-xl border border-border/20 p-3">
      <p className="text-[10px] uppercase tracking-widest text-muted-foreground">Risk limits</p>
      <div className="grid grid-cols-3 gap-2">
        <Field label="Risk / trade %" hint="Equity risk at the stop. 0 = off.">
          <input value={risk} onChange={(event) => setRisk(event.target.value)} className={inputClass} />
        </Field>
        <Field label="Daily loss %" hint="Rolling 24h brake. 0 = off.">
          <input value={daily} onChange={(event) => setDaily(event.target.value)} className={inputClass} />
        </Field>
        <Field label="Max notional" hint="Per-order symbol cap. 0 = off.">
          <input
            value={notional}
            onChange={(event) => setNotional(event.target.value)}
            className={inputClass}
          />
        </Field>
      </div>
      <p className="pt-1 text-[10px] uppercase tracking-widest text-muted-foreground">
        Account fit
      </p>
      <div className="grid grid-cols-3 gap-2">
        <Field label="Max risk %" hint="Per plan, from the stop. Blocks above.">
          <input value={bookRisk} onChange={(event) => setBookRisk(event.target.value)} className={inputClass} />
        </Field>
        <Field label="Max positions" hint="Open positions allowed.">
          <input value={openMax} onChange={(event) => setOpenMax(event.target.value)} className={inputClass} />
        </Field>
        <Field label="Per symbol" hint="Positions allowed in one symbol.">
          <input value={symbolMax} onChange={(event) => setSymbolMax(event.target.value)} className={inputClass} />
        </Field>
        <Field label="Margin %" hint="Margin after the order vs equity.">
          <input value={marginMax} onChange={(event) => setMarginMax(event.target.value)} className={inputClass} />
        </Field>
        <Field label="Margin level %" hint="Broker floor before new risk.">
          <input value={levelMin} onChange={(event) => setLevelMin(event.target.value)} className={inputClass} />
        </Field>
        <Field label="Spread / risk %" hint="Spread cost vs planned risk.">
          <input value={spreadMax} onChange={(event) => setSpreadMax(event.target.value)} className={inputClass} />
        </Field>
      </div>
      {error && <p className="text-[10px] text-rose-300">{error}</p>}
      <Action busy={busy} onClick={() => void save()}>
        Save limits
      </Action>
    </div>
  );
}

function ConnectionEditor({
  connection,
  kinds,
  onSaved,
}: {
  connection: BrokerConnection;
  kinds: BrokerKind[];
  onSaved: () => Promise<void>;
}) {
  const config = connection.config ?? {};
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState(connection.label);
  const [mode, setMode] = useState<"demo" | "live">(connection.mode === "live" ? "live" : "demo");
  const [terminalPath, setTerminalPath] = useState(
    String(config.terminalPath ?? DEFAULT_TERMINAL),
  );
  const [bridgeUrl, setBridgeUrl] = useState(String(config.bridgeUrl ?? "http://127.0.0.1:8090"));
  const [bridgeSecretRef, setBridgeSecretRef] = useState(String(config.bridgeSecretRef ?? ""));
  const [baseUrl, setBaseUrl] = useState(String(config.baseUrl ?? ""));
  const [dataUrl, setDataUrl] = useState(String(config.dataUrl ?? ""));
  const [credentialRef, setCredentialRef] = useState(String(config.credentialRef ?? ""));
  const [brokerAccountId, setBrokerAccountId] = useState(String(config.accountId ?? ""));
  const [quoteAsset, setQuoteAsset] = useState(String(config.quoteAsset ?? ""));
  const [magic, setMagic] = useState(String(config.magic ?? ""));
  const [maxMargin, setMaxMargin] = useState(String(config.maxMargin ?? ""));
  const [autoLaunch, setAutoLaunch] = useState(configFlag(config.autoLaunchTerminal));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      // Keep the connection's other caps (risk, allowlists, symbols) and only
      // rewrite the fields that belong to this adapter's auth style.
      const next: Record<string, unknown> = { ...config };
      if (connection.kind === "mt4_bridge") {
        if (bridgeUrl.trim()) next.bridgeUrl = bridgeUrl.trim();
        if (bridgeSecretRef.trim()) next.bridgeSecretRef = bridgeSecretRef.trim();
      } else if (connection.kind === "mt5") {
        if (terminalPath.trim()) next.terminalPath = terminalPath.trim();
        next.autoLaunchTerminal = autoLaunch;
      } else {
        if (baseUrl.trim()) next.baseUrl = baseUrl.trim();
        else delete next.baseUrl;
        if (connection.kind === "alpaca" && dataUrl.trim()) next.dataUrl = dataUrl.trim();
        if (brokerAccountId.trim()) next.accountId = brokerAccountId.trim();
        else delete next.accountId;
        if (credentialRef.trim()) next.credentialRef = credentialRef.trim();
        else delete next.credentialRef;
        if (quoteAsset.trim()) next.quoteAsset = quoteAsset.trim().toUpperCase();
        else delete next.quoteAsset;
      }
      const magicNumber = Number(magic);
      const marginNumber = Number(maxMargin);
      if (Number.isFinite(magicNumber) && magicNumber > 0) next.magic = magicNumber;
      else delete next.magic;
      if (Number.isFinite(marginNumber) && marginNumber > 0) next.maxMargin = marginNumber;
      else delete next.maxMargin;
      await updateBrokerConnection(connection.id, {
        label: label.trim() || connection.label,
        mode,
        config: next,
      });
      await onSaved();
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not save the connection");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 rounded-xl border border-border/20 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] uppercase tracking-widest text-muted-foreground">
          Connection settings
        </p>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="rounded-lg border border-border/40 px-2 py-1 text-[10px] text-muted-foreground hover:bg-white/[0.05] hover:text-foreground"
        >
          {open ? "Close" : "Edit"}
        </button>
      </div>
      {open && (
        <>
          {mode === "live" && (
            <p className="text-[10px] text-rose-200/80">
              A live connection places real orders. A non-zero magic and a positive max margin are
              required before you can arm it.
            </p>
          )}
          {error && <p className="text-[10px] text-rose-300">{error}</p>}
          <ConnectionForm
            kind={connection.kind}
            setKind={() => {}}
            kinds={kinds}
            allowKindChange={false}
            label={label}
            setLabel={setLabel}
            mode={mode}
            setMode={setMode}
            terminalPath={terminalPath}
            setTerminalPath={setTerminalPath}
            bridgeUrl={bridgeUrl}
            setBridgeUrl={setBridgeUrl}
            bridgeSecretRef={bridgeSecretRef}
            setBridgeSecretRef={setBridgeSecretRef}
            baseUrl={baseUrl}
            setBaseUrl={setBaseUrl}
            dataUrl={dataUrl}
            setDataUrl={setDataUrl}
            credentialRef={credentialRef}
            setCredentialRef={setCredentialRef}
            brokerAccountId={brokerAccountId}
            setBrokerAccountId={setBrokerAccountId}
            quoteAsset={quoteAsset}
            setQuoteAsset={setQuoteAsset}
            magic={magic}
            setMagic={setMagic}
            maxMargin={maxMargin}
            setMaxMargin={setMaxMargin}
            busy={busy}
            onSubmit={() => void save()}
            submitLabel="Save connection"
            compact
          />
          {connection.kind === "mt5" && (
            <AutoLaunchToggle checked={autoLaunch} onChange={setAutoLaunch} />
          )}
        </>
      )}
    </div>
  );
}

/** Old configs stored flags as strings, so treat any truthy spelling as true. */
function configFlag(value: unknown): boolean {
  return value === true || value === 1 || ["true", "1", "yes", "on"].includes(String(value ?? "").trim().toLowerCase());
}

function AutoLaunchToggle({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2 rounded-xl border border-border/20 p-3 text-[11px] text-muted-foreground">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 accent-primary"
      />
      <span>
        Start MetaTrader 5 automatically when it is closed.
        <span className="mt-0.5 block text-[10px] text-muted-foreground/70">
          Off by default. Eclipse only attaches to a terminal that is already running and logged in,
          so it never pops MT5 open in the background. Turning this on lets any broker read launch
          terminal64.exe.
        </span>
      </span>
    </label>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/30 px-2 py-1.5">
      <p className="text-[9px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="truncate text-[11px] text-foreground">{value}</p>
    </div>
  );
}

function Action({
  children,
  onClick,
  busy,
}: {
  children: React.ReactNode;
  onClick: () => void;
  busy?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={busy}
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-xl border border-border/40 px-2.5 py-1.5 text-[11px] text-muted-foreground hover:bg-white/[0.05] hover:text-foreground disabled:opacity-60"
    >
      {busy ? <Loader2 size={12} className="animate-spin" /> : null}
      {children}
    </button>
  );
}
