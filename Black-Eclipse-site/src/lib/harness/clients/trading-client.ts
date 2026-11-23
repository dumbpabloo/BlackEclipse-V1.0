// ── Broker connectivity client ───────────────────────────────────────────────
// Client-safe: talks only to the Next.js `/api/trading/*` front door, which
// proxies to the operator-gated Python routes. No Node APIs.

export type BrokerMode = "demo" | "live" | "unknown";

/** MetaTrader tickets are integers; REST venues use opaque ids. */
export type BrokerTicket = number | string;

export interface BrokerKind {
  id: string;
  label: string;
  auth: "attach" | "credentials" | "oauth" | "bridge";
  modes: BrokerMode[];
  assetClasses: string[];
  capabilities: string[];
  execution: boolean;
  executionModes: BrokerMode[];
  notes: string;
  available: boolean;
  unavailableReason: string | null;
}

export interface BrokerAccount {
  login: number;
  /** The account id the live-arm phrase uses (falls back to `login`). */
  armRef?: string | null;
  mode: "demo" | "contest" | "real" | "unknown";
  server: string;
  company: string;
  currency: string;
  balance: number;
  equity: number;
  margin: number;
  marginFree: number;
  marginLevel: number;
  profit: number;
  leverage: number;
  marginMode: "netting" | "hedging" | "exchange" | "unknown";
  tradeAllowed: boolean;
  expertAllowed: boolean;
}

export interface BrokerConnection {
  id: string;
  kind: string;
  label: string;
  mode: BrokerMode;
  config: Record<string, unknown>;
  status: "unconfigured" | "connected" | "unavailable";
  statusReason: string | null;
  account: BrokerAccount | null;
  armedUntil: string | null;
  armedAt: string | null;
  armedBy: string | null;
  lastSeenAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface BrokerPosition {
  /** MetaTrader uses an integer ticket; a REST venue uses an opaque id/symbol. */
  ticket: BrokerTicket;
  symbol: string;
  side: "buy" | "sell";
  volume: number;
  openPrice: number;
  currentPrice: number;
  stopLoss: number | null;
  takeProfit: number | null;
  profit: number;
  swap: number;
  magic: number;
  comment: string;
  openedAt: number;
}

export interface BrokerWorkingOrder {
  ticket: BrokerTicket;
  symbol: string;
  type: string;
  volumeCurrent: number;
  openPrice: number;
  currentPrice: number;
  stopLoss: number | null;
  takeProfit: number | null;
  state: string;
  magic: number;
  comment: string;
  placedAt: number;
}

export interface BrokerQuote {
  symbol: string;
  requestedSymbol: string;
  bid: number;
  ask: number;
  last: number;
  spreadPoints: number | null;
  digits: number;
  point: number;
  stopsLevelPoints: number;
  serverTimeMs: number;
  provider: string;
}

export interface BrokerProbe {
  available: boolean;
  connected: boolean;
  code: string | null;
  reason: string | null;
  terminalPath: string | null;
  terminal: Record<string, unknown> | null;
  account: BrokerAccount | null;
  warnings: string[];
}

export interface BrokerEnvelope<T> {
  available: boolean;
  status: "connected" | "UNAVAILABLE";
  connectionId: string;
  kind: string;
  mode: BrokerMode;
  data?: T;
  error?: { code: string; message: string };
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/trading/${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const payload = (await res.json().catch(() => ({}))) as T & { error?: string };
  if (!res.ok) {
    throw new Error(payload.error || `Broker request failed (${res.status})`);
  }
  return payload;
}

export async function listBrokerKinds(): Promise<BrokerKind[]> {
  const body = await api<{ brokers: BrokerKind[] }>("brokers");
  return body.brokers ?? [];
}

export async function listBrokerConnections(): Promise<BrokerConnection[]> {
  const body = await api<{ connections: BrokerConnection[] }>("connections");
  return body.connections ?? [];
}

export async function createBrokerConnection(input: {
  kind: string;
  label: string;
  mode?: BrokerMode;
  config?: Record<string, unknown>;
}): Promise<BrokerConnection> {
  const body = await api<{ connection: BrokerConnection }>("connections", {
    method: "POST",
    body: JSON.stringify(input),
  });
  return body.connection;
}

export async function updateBrokerConnection(
  connectionId: string,
  patch: Partial<Pick<BrokerConnection, "label" | "mode">> & { config?: Record<string, unknown> },
): Promise<BrokerConnection> {
  const body = await api<{ connection: BrokerConnection }>(`connections/${connectionId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  return body.connection;
}

export async function deleteBrokerConnection(connectionId: string): Promise<boolean> {
  const body = await api<{ deleted: boolean }>(`connections/${connectionId}`, { method: "DELETE" });
  return Boolean(body.deleted);
}

export async function testBrokerConnection(
  connectionId: string,
): Promise<{ available: boolean; probe: BrokerProbe }> {
  return api<{ available: boolean; probe: BrokerProbe }>(`connections/${connectionId}/test`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function getBrokerAccount(connectionId: string): Promise<BrokerEnvelope<BrokerAccount>> {
  return api<BrokerEnvelope<BrokerAccount>>(`connections/${connectionId}/account`);
}

export async function getBrokerPositions(
  connectionId: string,
): Promise<BrokerEnvelope<BrokerPosition[]>> {
  return api<BrokerEnvelope<BrokerPosition[]>>(`connections/${connectionId}/positions`);
}

export async function getBrokerOrders(
  connectionId: string,
): Promise<BrokerEnvelope<BrokerWorkingOrder[]>> {
  return api<BrokerEnvelope<BrokerWorkingOrder[]>>(`connections/${connectionId}/orders`);
}

export async function getBrokerQuote(
  connectionId: string,
  symbol: string,
): Promise<BrokerEnvelope<BrokerQuote>> {
  return api<BrokerEnvelope<BrokerQuote>>(
    `connections/${connectionId}/quote?symbol=${encodeURIComponent(symbol)}`,
  );
}

// ── reconciliation ───────────────────────────────────────────────────────────
export interface BrokerFill {
  dealTicket: number;
  orderTicket: number | null;
  positionTicket: number | null;
  symbol: string;
  side: string;
  entry: string | null;
  volume: number;
  price: number;
  commission: number;
  swap: number;
  profit: number;
  magic: number;
  comment: string | null;
  dealtAt: number;
  eclipseOwned: boolean;
  syncedAt: string;
}

export interface BrokerPositionSnapshot {
  ticket: BrokerTicket;
  symbol: string;
  side: "buy" | "sell";
  volume: number;
  openPrice: number;
  currentPrice: number;
  stopLoss: number | null;
  takeProfit: number | null;
  profit: number;
  swap: number;
  magic: number;
  comment: string | null;
  openedAt: number;
  syncedAt: string;
}

export interface BrokerSyncState {
  connectionId: string;
  lastSyncedAt: string | null;
  lastDealAt: number | null;
  status: "ok" | "error" | "unavailable" | "never" | string;
  error: string | null;
  positionsCount: number;
  fillsAdded: number;
  updatedAt: string;
}

export interface BrokerReconciliation {
  available: boolean;
  connectionId: string;
  state: BrokerSyncState | null;
  positions: BrokerPositionSnapshot[];
  fills: BrokerFill[];
}

export async function syncBrokerConnection(
  connectionId: string,
  options: { days?: number } = {},
): Promise<{
  ok: boolean;
  connectionId: string;
  positions?: number;
  workingOrders?: number;
  fillsAdded?: number;
  ordersUpdated?: number;
  lastDealAt?: number;
  error?: unknown;
}> {
  return api(`connections/${connectionId}/sync`, {
    method: "POST",
    body: JSON.stringify(options.days ? { days: options.days } : {}),
  });
}

export interface BrokerSmokeCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface BrokerSmokeReport {
  ok: boolean;
  connectionId: string;
  kind: string;
  mode: string;
  days: number;
  checks: BrokerSmokeCheck[];
  account: BrokerAccount | null;
  counts: { positions?: number; orders?: number; deals?: number; dealWindowMs?: number };
  sync: Record<string, unknown> | null;
  error?: string;
}

/** Probe → reads → one sync. Read-only against the broker. */
export async function runBrokerSmoke(connectionId: string, days = 7): Promise<BrokerSmokeReport> {
  return api<BrokerSmokeReport>(`connections/${connectionId}/smoke`, {
    method: "POST",
    body: JSON.stringify({ days }),
  });
}

export async function getBrokerReconciliation(
  connectionId: string,
  limit = 100,
): Promise<BrokerReconciliation> {
  return api<BrokerReconciliation>(
    `connections/${connectionId}/reconciliation?limit=${limit}`,
  );
}

export async function getBrokerFills(
  connectionId: string,
  limit = 100,
): Promise<BrokerFill[]> {
  const body = await api<{ fills: BrokerFill[] }>(
    `connections/${connectionId}/fills?limit=${limit}`,
  );
  return body.fills ?? [];
}

// ── P1 demo execution ────────────────────────────────────────────────────────
export interface OrderIntent {
  symbol: string;
  side: "buy" | "sell";
  type: "market" | "limit" | "stop";
  volume: number;
  price?: number;
  stopLoss?: number;
  takeProfit?: number;
  deviationPoints?: number;
  timeInForce?: "gtc" | "day";
  comment?: string;
  magic?: number;
}

export interface TradeApproval {
  id: string;
  kind: string;
  status: "pending" | "approved" | "denied" | "expired" | "consumed";
  actionSummary: string | null;
  risk: string | null;
  expiresAt: string | null;
  createdAt: string | null;
  /**
   * Present when the approval came from the broker service: the exact order the
   * operator is approving, so the chat card can submit it without a re-entry.
   */
  connectionId?: string | null;
  intent?: (OrderIntent & { connectionId?: string }) | null;
  preview?: {
    symbol?: string;
    volume?: number;
    margin?: number | null;
    spreadPoints?: number | null;
    retcode?: number | null;
    /** Server-computed risk for THIS order (max loss, % equity, warnings). */
    riskSummary?: TradeRiskSummary | null;
  } | null;
}

/** The order's own risk, computed by the Python trade service from broker facts. */
export interface TradeRiskSummary {
  maxLoss: number;
  riskPctOfEquity: number;
  basis: "tick-value" | "contract-size";
  equity?: number | null;
  currency?: string | null;
  verdict: "ok" | "warn" | "block";
  warnings?: string[];
  reasons?: string[];
  spreadCost?: number | null;
  spreadPctOfRisk?: number | null;
  marginUsedPct?: number | null;
  rewardRisk?: number | null;
}

export interface OrderPreview {
  available: boolean;
  allowed: boolean;
  mode: "demo" | "live";
  connectionId: string;
  intent: OrderIntent & { connectionId: string };
  digest: string;
  preview: {
    ok: boolean;
    symbol: string;
    volume: number;
    price: number | null;
    notional: number;
    margin: number | null;
    spreadPoints: number | null;
    openPositions: number;
    retcode: number | null;
    retcodeName: string | null;
    reasons: string[];
    account: BrokerAccount;
    quote: BrokerQuote;
  };
  policy: { allowed: boolean; reasons: string[]; rules: string[] };
  guardrail: { decision: string; reasons: string[]; rules: string[] };
  approval: TradeApproval | null;
  requiresTypedConfirmation: boolean;
  confirmPhrase: string | null;
  reasons: string[];
}

export interface OrderSubmission {
  available: boolean;
  sent: boolean | null;
  filled: boolean;
  status:
    | "filled"
    | "placed"
    | "rejected"
    | "unknown"
    | "REFUSED"
    | "COOLING_OFF"
    | "CONFIRMATION_REQUIRED";
  stage?: "preview" | "guardrails";
  connectionId: string;
  orderId?: string;
  ledger?: LedgerOrder;
  result?: Record<string, unknown>;
  remainingSeconds?: number;
  requiredPhrase?: string;
  reasons: string[];
}

export interface LedgerOrder {
  id: string;
  connectionId: string;
  intent: OrderIntent & { connectionId: string };
  digest: string;
  approvalId: string;
  mandateId?: string | null;
  status: string;
  ticket: BrokerTicket | null;
  retcode: number | null;
  retcodeName: string | null;
  filledVolume: number | null;
  avgPrice: number | null;
  error: string | null;
  createdAt: string;
  updatedAt: string;
}

export async function previewOrder(
  connectionId: string,
  intent: OrderIntent,
): Promise<OrderPreview> {
  return api<OrderPreview>(`connections/${connectionId}/orders/preview`, {
    method: "POST",
    body: JSON.stringify({ intent }),
  });
}

export async function submitOrder(
  connectionId: string,
  intent: OrderIntent,
  approvalId: string,
  confirmation = "",
): Promise<OrderSubmission> {
  return api<OrderSubmission>(`connections/${connectionId}/orders/submit`, {
    method: "POST",
    body: JSON.stringify({ intent, approvalId, confirmation }),
  });
}

/** Unattended demo submission under a bounded mandate (no approval card). */
export async function submitOrderWithMandate(
  connectionId: string,
  intent: OrderIntent,
  mandateId: string,
  runId = "",
): Promise<OrderSubmission> {
  return api<OrderSubmission>(`connections/${connectionId}/orders/submit`, {
    method: "POST",
    body: JSON.stringify({ intent, mandateId, runId }),
  });
}

export async function cancelBrokerOrder(
  connectionId: string,
  ticket: BrokerTicket,
): Promise<{ cancelled: boolean; result: Record<string, unknown> }> {
  return api<{ cancelled: boolean; result: Record<string, unknown> }>(
    `connections/${connectionId}/orders/${encodeURIComponent(String(ticket))}/cancel`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export async function closeBrokerPosition(
  connectionId: string,
  ticket: BrokerTicket,
  volume?: number,
  deviation = 20,
): Promise<{ closed: boolean; result: Record<string, unknown> }> {
  return api<{ closed: boolean; result: Record<string, unknown> }>(
    `connections/${connectionId}/positions/${encodeURIComponent(String(ticket))}/close`,
    { method: "POST", body: JSON.stringify({ deviation, volume }) },
  );
}

export async function modifyBrokerPosition(
  connectionId: string,
  ticket: BrokerTicket,
  levels: { stopLoss?: number; takeProfit?: number },
): Promise<{ modified: boolean; result: Record<string, unknown> }> {
  return api<{ modified: boolean; result: Record<string, unknown> }>(
    `connections/${connectionId}/positions/${encodeURIComponent(String(ticket))}/modify`,
    { method: "POST", body: JSON.stringify(levels) },
  );
}

export async function closeAllBrokerPositions(
  connectionId: string,
  tickets?: BrokerTicket[],
): Promise<{
  closed: number;
  failed: number;
  requested: number;
  results: Record<string, unknown>[];
}> {
  return api<{
    closed: number;
    failed: number;
    requested: number;
    results: Record<string, unknown>[];
  }>(`connections/${connectionId}/positions/close`, {
    method: "POST",
    body: JSON.stringify(tickets ? { tickets } : {}),
  });
}

export async function listLedgerOrders(
  connectionId: string,
  limit = 50,
): Promise<LedgerOrder[]> {
  const body = await api<{ orders: LedgerOrder[] }>(
    `connections/${connectionId}/orders/ledger?limit=${limit}`,
  );
  return body.orders ?? [];
}

export async function listTradeApprovals(
  status?: string,
  limit = 50,
): Promise<TradeApproval[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  const body = await api<{ approvals: TradeApproval[] }>(`approvals?${query.toString()}`);
  return body.approvals ?? [];
}

export async function decideTradeApproval(
  approvalId: string,
  approve: boolean,
  reason = "",
): Promise<TradeApproval> {
  const body = await api<{ approval: TradeApproval }>(
    `approvals/${approvalId}/decide`,
    { method: "POST", body: JSON.stringify({ approve, reason }) },
  );
  return body.approval;
}

// ── P2 live arming and kill switch ───────────────────────────────────────────
export interface ArmResult {
  armed: boolean;
  connectionId: string;
  armedUntil: string;
  ttlSeconds: number;
  account: BrokerAccount;
}

export async function armBrokerConnection(
  connectionId: string,
  confirmation: string,
  ttlSeconds = 1800,
): Promise<ArmResult> {
  return api<ArmResult>(`connections/${connectionId}/arm`, {
    method: "POST",
    body: JSON.stringify({ confirmation, ttlSeconds }),
  });
}

export async function disarmBrokerConnection(
  connectionId: string,
): Promise<{ armed: boolean; connectionId: string }> {
  return api<{ armed: boolean; connectionId: string }>(
    `connections/${connectionId}/disarm`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export async function killBrokerConnection(connectionId: string): Promise<{
  armed: boolean;
  connectionId: string;
  magic: number;
  kill: {
    ok: boolean;
    matched: number;
    cancelled: number;
    failed: number;
    results: Record<string, unknown>[];
  };
}> {
  return api(`connections/${connectionId}/kill`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// ── P3 bounded demo mandates ─────────────────────────────────────────────────
export interface TradingMandate {
  id: string;
  connectionId: string;
  mode: "demo" | "live";
  /** A live mandate pins the workflow version it authorizes. */
  workflowId?: string | null;
  workflowVersion?: number | null;
  symbols: string[];
  maxOrders: number;
  maxMargin: number;
  ordersUsed: number;
  remainingOrders: number;
  expiresAt: string;
  revokedAt: string | null;
  createdBy: string;
  note: string | null;
  createdAt: string;
  lastUsedAt: string | null;
}

export async function createTradingMandate(
  connectionId: string,
  input: {
    symbols: string[];
    maxOrders: number;
    maxMargin: number;
    ttlSeconds: number;
    confirmation: string;
    note?: string;
    workflowId?: string;
    workflowVersion?: number;
  },
): Promise<TradingMandate> {
  const body = await api<{ mandate: TradingMandate }>(
    `connections/${connectionId}/mandates`,
    { method: "POST", body: JSON.stringify(input) },
  );
  return body.mandate;
}

export async function listTradingMandates(
  connectionId?: string,
): Promise<TradingMandate[]> {
  const query = connectionId
    ? `?connectionId=${encodeURIComponent(connectionId)}`
    : "";
  const body = await api<{ mandates: TradingMandate[] }>(`mandates${query}`);
  return body.mandates ?? [];
}

export async function revokeTradingMandate(
  mandateId: string,
): Promise<TradingMandate> {
  const body = await api<{ mandate: TradingMandate }>(
    `mandates/${mandateId}/revoke`,
    { method: "POST", body: JSON.stringify({}) },
  );
  return body.mandate;
}
