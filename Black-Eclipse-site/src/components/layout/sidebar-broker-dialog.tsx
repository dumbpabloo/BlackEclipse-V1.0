"use client";

// ── Sidebar broker button + control-center dialog ────────────────────────────
// A first-class “broker” entry point in the shell footer. The dialog is a full
// read + control surface over the existing trading-client:
//   Overview   — live account, bridge health, quote lookup, connection state
//   Positions  — open positions, close one / close all
//   Orders     — working orders, cancel one
//   Activity   — broker deals + the Eclipse order ledger
//   Settings   — edit connection, arm / disarm / kill switch, add / remove
// Every action goes through the same operator-gated Python routes as
// Settings → Broker connection. No passwords are stored; scalping the terminal
// never bypasses the arm + kill-switch contract.

import { useCallback, useEffect, useState } from "react";
import { confirmDialog } from "@/lib/ui/confirm-dialog";
import {
  Activity,
  Briefcase,
  Handshake,
  History,
  LayoutDashboard,
  List,
  Loader2,
  Lock,
  Plus,
  Settings,
  ShieldCheck,
  ShieldOff,
  Trash2,
  Zap,
} from "@/lib/icons";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TabBar } from "@/components/ui/tab-bar";
import {
  ConnectionForm,
  inputClass,
  type BrokerKindId,
} from "@/components/layout/broker-connection-form";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { BROKER_RESPONSIBILITY_NOTICE } from "@/lib/legal/disclosure";
import { Skeleton, SkeletonBlock } from "@/components/ui/skeleton";
import {
  armBrokerConnection,
  cancelBrokerOrder,
  closeAllBrokerPositions,
  closeBrokerPosition,
  createBrokerConnection,
  deleteBrokerConnection,
  disarmBrokerConnection,
  getBrokerAccount,
  getBrokerFills,
  getBrokerOrders,
  getBrokerPositions,
  getBrokerQuote,
  killBrokerConnection,
  listBrokerConnections,
  listBrokerKinds,
  listLedgerOrders,
  runBrokerSmoke,
  syncBrokerConnection,
  testBrokerConnection,
  updateBrokerConnection,
  type BrokerAccount,
  type BrokerConnection,
  type BrokerFill,
  type BrokerKind,
  type BrokerPosition,
  type BrokerQuote,
  type BrokerWorkingOrder,
  type LedgerOrder,
} from "@/lib/harness/clients/trading-client";
import {
  BrokerActivityPanel,
  BrokerOrdersPanel,
  BrokerOverviewPanel,
  BrokerPositionsPanel,
  fmtStamp,
  PanelBox,
  StatTile,
} from "./sidebar-broker-panels";

const DEFAULT_TERMINAL = "C:\\Program Files\\MetaTrader 5\\terminal64.exe";

/** Short adapter badge for the dialog header (never mislabel another broker as MT5). */
const KIND_BADGE: Record<string, string> = {
  mt5: "MT5",
  mt4_bridge: "MT4 bridge",
  alpaca: "Alpaca",
  ibkr: "IBKR",
};
const kindBadge = (kind: string | null | undefined) =>
  kind ? KIND_BADGE[kind] ?? kind.toUpperCase() : "new";


type TabId = "overview" | "positions" | "orders" | "activity" | "settings";

export function SidebarBrokerButton({
  collapsed = false,
  variant = "footer",
}: {
  collapsed?: boolean;
  variant?: "footer" | "launcher";
}) {
  const [open, setOpen] = useState(false);
  const [connected, setConnected] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    listBrokerConnections()
      .then((connections) => {
        if (alive) setConnected(connections.some((c) => c.status === "connected"));
      })
      .catch(() => {
        if (alive) setConnected(false);
      });
    return () => {
      alive = false;
    };
  }, [open]);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "relative shrink-0 text-muted-foreground transition-colors hover:bg-white/[0.04] hover:text-foreground outline-none focus-visible:ring-1 focus-visible:ring-primary/50 cursor-pointer",
          variant === "launcher"
            ? "flex h-7 w-7 items-center justify-center rounded-md"
            : "rounded-lg p-1.5 hover:bg-accent",
          collapsed && "flex h-9 w-9 items-center justify-center rounded-lg",
        )}
        title="Broker connection"
        aria-label="Broker connection"
      >
        <Lock size={variant === "launcher" ? 15 : 16} className="shrink-0" />
        {connected != null && (
          <span
            className={cn(
              "absolute right-1 top-1 h-1.5 w-1.5 rounded-full",
              connected ? "bg-emerald-400" : "bg-amber-400",
            )}
          />
        )}
      </button>
      <SidebarBrokerDialog open={open} onOpenChange={setOpen} />
    </>
  );
}

function SidebarBrokerDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [kinds, setKinds] = useState<BrokerKind[]>([]);
  const [connections, setConnections] = useState<BrokerConnection[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [account, setAccount] = useState<BrokerAccount | null>(null);
  const [positions, setPositions] = useState<BrokerPosition[]>([]);
  const [orders, setOrders] = useState<BrokerWorkingOrder[]>([]);
  const [fills, setFills] = useState<BrokerFill[]>([]);
  const [ledger, setLedger] = useState<LedgerOrder[]>([]);

  const [loading, setLoading] = useState(false);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("overview");

  const [quoteSymbol, setQuoteSymbol] = useState("EURUSD");
  const [quote, setQuote] = useState<BrokerQuote | null>(null);
  const [quoteBusy, setQuoteBusy] = useState(false);
  const [quoteError, setQuoteError] = useState<string | null>(null);

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
  const [magic, setMagic] = useState("0");
  const [maxMargin, setMaxMargin] = useState("0");
  const [editOpen, setEditOpen] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [armPhrase, setArmPhrase] = useState("");

  const selected = connections.find((c) => c.id === selectedId) ?? connections[0] ?? null;
  const selectedKind = kinds.find((k) => k.id === (selected?.kind ?? kind));

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextKinds, nextConnections] = await Promise.all([
        listBrokerKinds(),
        listBrokerConnections(),
      ]);
      setKinds(nextKinds);
      setConnections(nextConnections);
      setShowForm((prev) => (nextConnections.length === 0 ? true : prev));
      // A default kind this host cannot load would make the first "add" fail;
      // fall back to any available adapter while keeping an explicit pick.
      setKind((prev) => {
        const current = nextKinds.find((entry) => entry.id === prev);
        if (current?.available) return prev;
        return (
          nextKinds.find((entry) => entry.available)?.id ??
          nextKinds[0]?.id ??
          prev
        );
      });
      setSelectedId((prev) => {
        if (prev && nextConnections.some((c) => c.id === prev)) return prev;
        return (
          nextConnections.find((c) => c.status === "connected")?.id ??
          nextConnections[0]?.id ??
          null
        );
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "broker settings unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshLive = useCallback(async (connectionId: string) => {
    setLiveLoading(true);
    setLiveError(null);
    const results = await Promise.allSettled([
      getBrokerAccount(connectionId),
      getBrokerPositions(connectionId),
      getBrokerOrders(connectionId),
      getBrokerFills(connectionId, 50),
      listLedgerOrders(connectionId, 25),
    ]);

    const failures: string[] = [];
    const [acct, pos, ord, fl, led] = results;

    if (acct.status === "fulfilled") {
      if (acct.value.available === false) {
        failures.push(acct.value.error?.message ?? "account unavailable");
      } else {
        setAccount(acct.value.data ?? null);
      }
    } else {
      failures.push(acct.reason instanceof Error ? acct.reason.message : "account unavailable");
    }

    if (pos.status === "fulfilled") {
      if (pos.value.available === false) {
        failures.push(pos.value.error?.message ?? "positions unavailable");
      } else {
        setPositions(pos.value.data ?? []);
      }
    } else {
      failures.push(pos.reason instanceof Error ? pos.reason.message : "positions unavailable");
    }

    if (ord.status === "fulfilled") {
      if (ord.value.available === false) {
        failures.push(ord.value.error?.message ?? "orders unavailable");
      } else {
        setOrders(ord.value.data ?? []);
      }
    } else {
      failures.push(ord.reason instanceof Error ? ord.reason.message : "orders unavailable");
    }

    if (fl.status === "fulfilled") setFills(fl.value);
    else failures.push("deals unavailable");

    if (led.status === "fulfilled") setLedger(led.value);
    else failures.push("order ledger unavailable");

    setRefreshedAt(new Date());
    if (failures.length > 0) setLiveError(failures[0]);
    setLiveLoading(false);
  }, []);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  // On selection change, show the row snapshot immediately, then read live.
  useEffect(() => {
    if (!open) return;
    const connection = connections.find((c) => c.id === selectedId) ?? connections[0] ?? null;
    if (!connection) {
      setAccount(null);
      setPositions([]);
      setOrders([]);
      setFills([]);
      setLedger([]);
      return;
    }
    setAccount(connection.account ?? null);
    setPositions([]);
    setOrders([]);
    setFills([]);
    setLedger([]);
    setQuote(null);
    setQuoteError(null);
    setLabel(connection.label);
    setKind(connection.kind);
    setMode(connection.mode === "live" ? "live" : "demo");
    setTerminalPath(String(connection.config?.terminalPath ?? DEFAULT_TERMINAL));
    setBridgeUrl(String(connection.config?.bridgeUrl ?? "http://127.0.0.1:8090"));
    setBridgeSecretRef(String(connection.config?.bridgeSecretRef ?? ""));
    setBaseUrl(String(connection.config?.baseUrl ?? ""));
    setDataUrl(String(connection.config?.dataUrl ?? ""));
    setCredentialRef(String(connection.config?.credentialRef ?? ""));
    setBrokerAccountId(String(connection.config?.accountId ?? ""));
    setQuoteAsset(String(connection.config?.quoteAsset ?? ""));
    setMagic(String(connection.config?.magic ?? "0"));
    setMaxMargin(String(connection.config?.maxMargin ?? "0"));
    setEditOpen(false);
    setArmPhrase("");
    void refreshLive(connection.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, selectedId, connections.length]);

  const run = async (
    key: string,
    action: () => Promise<unknown>,
    done?: string,
    refresh = true,
  ) => {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await action();
      await load();
      if (refresh && selected) await refreshLive(selected.id);
      if (done) setNotice(done);
    } catch (err) {
      setError(err instanceof Error ? err.message : "the action failed");
    } finally {
      setBusy(null);
    }
  };

  const buildConfig = (forKind: BrokerKindId = kind): Record<string, unknown> => {
    const config: Record<string, unknown> = {};
    if (forKind === "mt4_bridge") {
      if (bridgeUrl.trim()) config.bridgeUrl = bridgeUrl.trim();
      if (bridgeSecretRef.trim()) config.bridgeSecretRef = bridgeSecretRef.trim();
    } else if (forKind === "mt5") {
      if (terminalPath.trim()) config.terminalPath = terminalPath.trim();
    } else {
      if (baseUrl.trim()) config.baseUrl = baseUrl.trim();
      if (forKind === "alpaca" && dataUrl.trim()) config.dataUrl = dataUrl.trim();
      if (brokerAccountId.trim()) config.accountId = brokerAccountId.trim();
      if (credentialRef.trim()) config.credentialRef = credentialRef.trim();
      if (quoteAsset.trim()) config.quoteAsset = quoteAsset.trim().toUpperCase();
    }
    const marginNumber = Number(maxMargin);
    if (Number.isFinite(marginNumber) && marginNumber > 0) config.maxMargin = marginNumber;
    else delete config.maxMargin;
    if (forKind === "mt5" || forKind === "mt4_bridge") {
      const magicNumber = Number(magic);
      if (Number.isFinite(magicNumber) && magicNumber > 0) config.magic = magicNumber;
      else delete config.magic;
    }
    // Editing an existing connection keeps its other caps (risk, allowlists,
    // symbols); a NEW connection never inherits another kind's settings.
    if (selected && selected.kind === forKind) {
      return { ...(selected.config ?? {}), ...config };
    }
    return config;
  };

  const add = () => {
    const fallbackLabel =
      kinds.find((entry) => entry.id === kind)?.label ??
      kind;
    void run(
      "create",
      async () => {
        const created = await createBrokerConnection({
          kind,
          label: label.trim() || fallbackLabel,
          mode,
          config: buildConfig(kind),
        });
        setSelectedId(created.id);
        await testBrokerConnection(created.id);
      },
      "Connection added and tested.",
      false,
    );
  };

  const saveEdit = () => {
    if (!selected) return;
    const forKind: BrokerKindId = selected.kind;
    const fallbackLabel =
      kinds.find((entry) => entry.id === forKind)?.label ?? forKind;
    void run(
      "save",
      () =>
        updateBrokerConnection(selected.id, {
          label: label.trim() || fallbackLabel,
          mode,
          config: buildConfig(forKind),
        }),
      "Connection updated.",
    );
  };

  const remove = async () => {
    if (!selected) return;
    if (
      !(await confirmDialog({
        title: `Remove ${selected.label} from Eclipse?`,
        description: "This does not close anything at the broker.",
        tone: "danger",
      }))
    ) {
      return;
    }
    void run(
      "delete",
      async () => {
        await deleteBrokerConnection(selected.id);
        setSelectedId(null);
      },
      "Connection removed.",
      false,
    );
  };

  const test = () => {
    if (!selected) return;
    void run("test", () => testBrokerConnection(selected.id), "Connection tested.");
  };

  const sync = () => {
    if (!selected) return;
    void run("sync", () => syncBrokerConnection(selected.id), "Fills and positions synced.");
  };

  const smoke = () => {
    if (!selected) return;
    void run(
      "smoke",
      async () => {
        const report = await runBrokerSmoke(selected.id, 7);
        if (!report.ok) throw new Error(report.error || "the smoke test failed");
      },
      "Smoke test passed (probe, reads and sync).",
    );
  };

  const closeOne = async (ticket: number | string) => {
    if (!selected) return;
    if (!(await confirmDialog({ title: `Close position #${ticket} at market?`, description: "This cannot be undone.", tone: "danger" }))) return;
    void run(`close:${ticket}`, () => closeBrokerPosition(selected.id, ticket), `Position #${ticket} closed.`);
  };

  const closeAll = async () => {
    if (!selected || positions.length === 0) return;
    if (!(await confirmDialog({ title: `Close all ${positions.length} open positions at market?`, description: "This cannot be undone.", tone: "danger" }))) {
      return;
    }
    void run("close-all", () => closeAllBrokerPositions(selected.id), "Close-all sent to the broker.");
  };

  const cancel = async (ticket: number | string) => {
    if (!selected) return;
    if (!(await confirmDialog({ title: `Cancel working order #${ticket}?`, tone: "danger" }))) return;
    void run(`cancel:${ticket}`, () => cancelBrokerOrder(selected.id, ticket), `Order #${ticket} cancelled.`);
  };

  const arm = () => {
    if (!selected) return;
    void run(
      "arm",
      () => armBrokerConnection(selected.id, armPhrase, 1800),
      "Live execution armed for 30 minutes.",
    );
  };

  const disarm = () => {
    if (!selected) return;
    void run("disarm", () => disarmBrokerConnection(selected.id), "Live execution disarmed.");
  };

  const kill = async () => {
    if (!selected) return;
    if (!(await confirmDialog({ title: "Cancel every working order Eclipse owns on this connection?", tone: "danger" }))) return;
    void run("kill", () => killBrokerConnection(selected.id), "Kill switch fired.");
  };

  const lookupQuote = async () => {
    if (!selected || !quoteSymbol.trim()) return;
    setQuoteBusy(true);
    setQuoteError(null);
    try {
      const envelope = await getBrokerQuote(selected.id, quoteSymbol.trim().toUpperCase());
      if (envelope.available && envelope.data) {
        setQuote(envelope.data);
      } else {
        setQuote(null);
        setQuoteError(envelope.error?.message ?? "no quote returned");
      }
    } catch (err) {
      setQuote(null);
      setQuoteError(err instanceof Error ? err.message : "quote unavailable");
    } finally {
      setQuoteBusy(false);
    }
  };

  const armed = Boolean(selected?.armedUntil);
  const armedUntil = selected?.armedUntil;

  const tabs = [
    { id: "overview", label: "Overview", icon: LayoutDashboard },
    {
      id: "positions",
      label: "Positions",
      icon: Briefcase,
      badge: positions.length ? <CountBadge value={positions.length} /> : undefined,
    },
    {
      id: "orders",
      label: "Orders",
      icon: List,
      badge: orders.length ? <CountBadge value={orders.length} /> : undefined,
    },
    { id: "activity", label: "Activity", icon: History },
    { id: "settings", label: "Settings", icon: Settings },
  ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[min(90dvh,900px)] w-[calc(100vw-2rem)] max-w-none flex-col gap-0 overflow-hidden rounded-2xl border border-white/10 glass-card-strong p-0 text-foreground sm:max-w-5xl">
        <DialogHeader className="shrink-0 border-b border-border/10 px-5 py-3.5">
          <DialogTitle className="flex items-center gap-2 pr-12 text-sm font-semibold">
            <Handshake size={15} className="text-muted-foreground" /> Broker connection
            <span className="rounded-full border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-sky-300">
              {kindBadge(selected?.kind)}
            </span>
            {selected && (
              <span
                className={cn(
                  "rounded-full border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide",
                  armed
                    ? "border-rose-500/30 bg-rose-500/10 text-rose-300"
                    : selected.status === "connected"
                      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                      : "border-amber-500/30 bg-amber-500/10 text-amber-300",
                )}
              >
                {armed ? "armed" : selected.status}
              </span>
            )}
            {selected?.mode === "live" && (
              <span className="rounded-full border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-rose-300">
                live
              </span>
            )}
            <span className="ml-auto font-mono text-[9.5px] font-normal text-muted-foreground">
              {connections.length} connection{connections.length === 1 ? "" : "s"}
            </span>
          </DialogTitle>
          <DialogDescription className="text-[11px] text-muted-foreground">
            Attach Eclipse to a running MetaTrader 5 terminal, a read-only MT4 bridge, or an
            Alpaca / IBKR account. Reads are always safe; credentials stay inside the Python
            runtime, never in the journal database.
          </DialogDescription>
          <p className="mt-1.5 text-[10.5px] leading-relaxed text-muted-foreground/70">
            {BROKER_RESPONSIBILITY_NOTICE}
          </p>
        </DialogHeader>

        {connections.length > 0 && (
          <div className="shrink-0 px-5 pt-3">
            <div className="flex gap-1.5 overflow-x-auto scrollbar-hide">
              {connections.map((connection) => {
                const active = connection.id === selected?.id;
                const connectionArmed = Boolean(connection.armedUntil);
                return (
                  <button
                    key={connection.id}
                    type="button"
                    onClick={() => setSelectedId(connection.id)}
                    className={cn(
                      "flex shrink-0 items-center gap-1.5 rounded-xl border px-2.5 py-1 text-[10px] transition-colors",
                      active
                        ? "border-primary/40 bg-primary/10 text-primary"
                        : "border-border/30 text-muted-foreground hover:bg-white/[0.04] hover:text-foreground",
                    )}
                  >
                    <span
                      className={cn(
                        "h-1.5 w-1.5 rounded-full",
                        connectionArmed
                          ? "bg-rose-400"
                          : connection.status === "connected"
                            ? "bg-emerald-400"
                            : "bg-amber-400",
                      )}
                    />
                    <span className="max-w-[10rem] truncate">{connection.label}</span>
                    <span className="uppercase opacity-60">{connection.mode}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {connections.length > 0 && (
          <div className="shrink-0 px-5 pt-2">
            <TabBar
              items={tabs}
              activeId={tab}
              onChange={(id) => setTab(id as TabId)}
              variant="subtle"
              className="mb-0"
            />
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="space-y-3">
            {error && <p className="text-[11px] text-rose-300">{error}</p>}
            {notice && <p className="text-[11px] text-emerald-300">{notice}</p>}
            {liveError && <p className="text-[10px] text-amber-300">{liveError}</p>}

            {loading ? (
              <div className="space-y-3" aria-label="Reading the broker">
                <div className="flex gap-1.5">
                  <Skeleton className="h-6 w-32 rounded-xl" />
                  <Skeleton className="h-6 w-24 rounded-xl" />
                  <Skeleton className="h-6 w-28 rounded-xl" />
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Skeleton className="h-16 rounded-xl" />
                  <Skeleton className="h-16 rounded-xl" />
                  <Skeleton className="h-16 rounded-xl" />
                  <Skeleton className="h-16 rounded-xl" />
                </div>
                <SkeletonBlock className="h-40 p-4">
                  <Skeleton className="h-3 w-28" />
                  <Skeleton className="mt-3 h-24 w-full" />
                </SkeletonBlock>
              </div>
            ) : !selected ? (
              <div className="space-y-3">
                <div className="rounded-xl border border-primary/15 bg-primary/[0.04] px-3 py-2.5">
                  <p className="text-[11px] font-semibold text-foreground">No broker connected yet</p>
                  <p className="mt-0.5 text-[10.5px] text-muted-foreground">
                    Connect any registered broker: MetaTrader 5, the MT4 HTTP bridge, or a REST
                    venue (Alpaca, IBKR, OANDA, Tradier, Trading212, Zerodha, Binance, Kraken,
                    Coinbase, Bitstamp, OKX, Bybit, Bitget, Gate.io, Deribit, Bitfinex, Coincheck).
                    Eclipse tests the connection immediately and never stores a password.
                  </p>
                </div>
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
                  onSubmit={add}
                  submitLabel="Add & test connection"
                />
              </div>
            ) : (
              <>
                {tab === "overview" && (
                  <BrokerOverviewPanel
                    connection={selected}
                    account={account}
                    bridge={{
                      available: selectedKind ? selectedKind.available : null,
                      reason: selectedKind?.unavailableReason ?? null,
                    }}
                    quote={quote}
                    quoteSymbol={quoteSymbol}
                    quoteBusy={quoteBusy}
                    quoteError={quoteError}
                    onQuoteSymbolChange={setQuoteSymbol}
                    onQuote={lookupQuote}
                    onRefresh={() => void refreshLive(selected.id)}
                    refreshing={liveLoading}
                    onTest={test}
                    onSync={sync}
                    onSmoke={smoke}
                    busy={busy}
                  />
                )}

                {tab === "positions" && (
                  <BrokerPositionsPanel
                    positions={positions}
                    loading={liveLoading}
                    busy={busy}
                    onClose={closeOne}
                    onCloseAll={closeAll}
                    onRefresh={() => void refreshLive(selected.id)}
                  />
                )}

                {tab === "orders" && (
                  <BrokerOrdersPanel
                    orders={orders}
                    loading={liveLoading}
                    busy={busy}
                    onCancel={cancel}
                    onRefresh={() => void refreshLive(selected.id)}
                  />
                )}

                {tab === "activity" && (
                  <BrokerActivityPanel
                    fills={fills}
                    ledger={ledger}
                    loading={liveLoading}
                    busy={busy}
                    onRefresh={() => void refreshLive(selected.id)}
                  />
                )}

                {tab === "settings" && (
                  <div className="space-y-3">
                    <PanelBox
                      title="Connection"
                      hint={`${selected.kind.toUpperCase()} · created ${fmtStamp(selected.createdAt)}`}
                      action={
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => setEditOpen((value) => !value)}
                        >
                          {editOpen ? "Close editor" : "Edit"}
                        </Button>
                      }
                    >
                      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
                        <StatTile label="Label" value={selected.label} />
                        <StatTile label="Mode" value={selected.mode.toUpperCase()} />
                        <StatTile label="Status" value={selected.status} />
                        <StatTile label="Last seen" value={fmtStamp(selected.lastSeenAt)} />
                        <StatTile label="Armed until" value={fmtStamp(selected.armedUntil)} />
                        <StatTile label="Armed by" value={selected.armedBy ?? "—"} />
                        <StatTile label="Magic" value={String(selected.config?.magic ?? "—")} />
                        <StatTile label="Max margin" value={String(selected.config?.maxMargin ?? "—")} />
                      </div>
                      <p className="truncate font-mono text-[9px] text-muted-foreground">
                        {String(selected.config?.terminalPath ?? "no terminal path")}
                      </p>
                      {editOpen && (
                        <div className="space-y-2 pt-1">
                          <ConnectionForm
                            kind={kind}
                            setKind={setKind}
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
                            busy={busy === "save"}
                            onSubmit={saveEdit}
                            submitLabel="Save changes"
                            compact
                          />
                        </div>
                      )}
                    </PanelBox>

                    {selected.mode === "live" && (
                      <PanelBox
                        title="Live execution"
                        hint="Arming is time-boxed. Every order still needs its own confirmation."
                        action={
                          <span
                            className={cn(
                              "rounded-full border px-2 py-0.5 text-[8.5px] font-bold uppercase",
                              armed
                                ? "border-rose-500/30 bg-rose-500/10 text-rose-300"
                                : "border-border/30 text-muted-foreground",
                            )}
                          >
                            {armed ? "armed" : "disarmed"}
                          </span>
                        }
                      >
                        <p className="text-[10px] text-rose-200/90">
                          Type{" "}
                          <span className="font-mono">
                            ARM LIVE {account?.armRef ?? account?.login ?? "<login>"}
                          </span>{" "}
                          to arm for 30 minutes. The kill switch cancels every working order Eclipse
                          owns.
                        </p>
                        <div className="flex gap-1.5">
                          <input
                            value={armPhrase}
                            onChange={(event) => setArmPhrase(event.target.value)}
                            placeholder={`ARM LIVE ${account?.armRef ?? account?.login ?? ""}`}
                            className={inputClass}
                          />
                          <Button
                            type="button"
                            size="sm"
                            className="shrink-0 border border-rose-500/40 bg-rose-500/10 text-rose-200 hover:bg-rose-500/15"
                            onClick={arm}
                            disabled={busy !== null || armPhrase.trim().length === 0}
                          >
                            {busy === "arm" ? <Loader2 size={12} className="animate-spin" /> : <Zap size={12} />}
                            Arm
                          </Button>
                        </div>
                        {armed && armedUntil && (
                          <p className="text-[9.5px] text-rose-200/80">
                            Armed until {fmtStamp(armedUntil)}.
                          </p>
                        )}
                        <div className="flex flex-wrap gap-1.5">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={disarm}
                            disabled={busy !== null || !armed}
                          >
                            {busy === "disarm" ? (
                              <Loader2 size={12} className="animate-spin" />
                            ) : (
                              <ShieldOff size={12} />
                            )}
                            Disarm
                          </Button>
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={kill}
                            disabled={busy !== null}
                          >
                            {busy === "kill" ? (
                              <Loader2 size={12} className="animate-spin" />
                            ) : (
                              <ShieldCheck size={12} />
                            )}
                            Kill switch
                          </Button>
                        </div>
                      </PanelBox>
                    )}

                    <PanelBox
                      title="Connections"
                      hint="Eclipse can hold several terminals; reads follow the selected one."
                      action={
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => setShowForm((value) => !value)}
                        >
                          <Plus size={12} /> {showForm ? "Hide" : "Add"}
                        </Button>
                      }
                    >
                      {showForm ? (
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
                          onSubmit={add}
                          submitLabel="Add & test connection"
                          compact
                        />
                      ) : (
                        <p className="text-[10px] text-muted-foreground">
                          Use Add to attach another MT5 terminal, MT4 bridge, or account.
                        </p>
                      )}
                    </PanelBox>

                    <PanelBox title="Remove" hint="Leaves the broker account untouched.">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="border-rose-500/30 text-rose-300 hover:bg-rose-500/10"
                        onClick={remove}
                        disabled={busy !== null}
                      >
                        {busy === "delete" ? (
                          <Loader2 size={12} className="animate-spin" />
                        ) : (
                          <Trash2 size={12} />
                        )}
                        Remove connection
                      </Button>
                    </PanelBox>

                    {refreshedAt && (
                      <p className="flex items-center gap-1.5 text-[9.5px] text-muted-foreground">
                        <Activity size={11} /> Last live read {refreshedAt.toLocaleTimeString()}
                      </p>
                    )}
                  </div>
                )}
              </>
            )}

            <p className="text-[10px] text-muted-foreground">
              Live arming, mandates and per-order approvals live in{" "}
              <span className="font-medium text-foreground">Settings → Broker connection</span>.
              Journal → Settings links this broker and imports its trades. Eclipse never stores
              your broker password.
            </p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CountBadge({ value }: { value: number }) {
  return (
    <span className="ml-0.5 rounded-full border border-border/40 px-1 text-[8.5px] leading-4 text-muted-foreground">
      {value}
    </span>
  );
}
