// ─── Harness Portfolio front door ───────────────────────────────────────────
// Thin, typed proxy over the Python runtime's /portfolio routes. Adds NO
// accounting logic (the ledger is the source of truth in the Python engine):
// it validates, forwards, and returns what the runtime owns. Operator-gated
// routes present the runtime secret by transport/token, never a body boolean.

import { HARNESS_PY_URL, ensurePythonRuntime } from "../python-runtime";
import { runtimeCallbackToken } from "../runtime-auth";
import { HarnessRuntimeUnavailable } from "./python-client";
import { redactSecrets } from "../errors";

export type CostBasisMethod = "FIFO" | "LIFO" | "AVERAGE_COST" | "HIFO" | "SPECIFIC_ID";
export type Freshness = "FRESH" | "STALE" | "VERY_STALE" | "MISSING";
export type DataQualityStatus = "OK" | "PARTIAL" | "UNAVAILABLE";

export interface PortfolioSummary {
  id: string;
  name: string;
  baseCurrency: string;
  ownerId: string | null;
  description: string | null;
  costBasisMethod: CostBasisMethod;
  status: string;
  /** Every user-facing portfolio is a fund; legacy rows are migrated on open. */
  kind?: 'fund' | 'portfolio' | string;
  metadata?: Record<string, unknown> | string;
  createdAt: string;
  updatedAt: string;
}

export interface PortfolioPosition {
  symbol: string;
  name: string;
  assetClass: string;
  sector: string;
  country: string;
  strategy: string;
  currency: string;
  quantity: string;
  averageCost: string | null;
  costBasis: string;
  costBasisBase: string | null;
  price: string | null;
  marketValue: string | null;
  marketValueBase: string | null;
  fxPerBase: string | null;
  unrealizedPnl: string | null;
  unrealizedPnlBase: string | null;
  realizedPnl: string;
  realizedPnlBase: string | null;
  weight: number | null;
  priceFreshness: Freshness;
  priceTimestamp: string | null;
  priceSource: string | null;
}

export interface DataQuality {
  status: DataQualityStatus;
  missingPrices: string[];
  stalePrices: Array<{ symbol: string; freshness: Freshness; timestamp: string | null }>;
  missingFX: string[];
  unpricedPositions: string[];
  warnings: string[];
}

export interface AllocationBucket {
  key: string;
  value: string;
  weight: number | null;
}

export interface AllocationView {
  baseCurrency: string;
  dimension: string;
  totalValue: string;
  breakdown: AllocationBucket[];
  weightSum: number;
  reconciles: boolean;
}

export interface ExposureView {
  baseCurrency: string;
  dimension: string;
  grossExposure: string;
  netExposure: string;
  breakdown: Array<{ key: string; long: string; short: string; net: string; gross: string }>;
}

export interface PortfolioRead {
  portfolio: PortfolioSummary;
  positions: PortfolioPosition[];
  cash: { baseCurrency: string; balance: string; netDeposits: string; dividends: string };
  valuation: {
    baseCurrency: string;
    investedValue: string;
    cash: string;
    totalValue: string;
    dataQuality: DataQuality;
  };
  pnl?: {
    realizedPnl: string;
    unrealizedPnl: string;
    totalPnl: string;
    costBasis: string;
    dividends: string;
    bySymbol: Array<{ symbol: string; realizedPnl: string | null; unrealizedPnl: string | null; quantity: string }>;
    byAssetClass?: Array<{ key: string; realizedPnl: string; unrealizedPnl: string; totalPnl: string }>;
    bySector?: Array<{ key: string; realizedPnl: string; unrealizedPnl: string; totalPnl: string }>;
    byStrategy?: Array<{ key: string; realizedPnl: string; unrealizedPnl: string; totalPnl: string }>;
  };
  allocation?: Record<string, AllocationView>;
  exposure?: Record<string, ExposureView>;
  risk?: { status: string; metrics: Record<string, unknown>; unavailable: string[] };
  performance?: { status: string; metrics: Record<string, unknown> };
  dataQuality: DataQuality;
  calculationVersion: string;
}

export interface TransactionResult {
  status: "SUCCESS" | "FAILED" | "PARTIAL";
  duplicate?: boolean;
  transaction?: Record<string, unknown>;
  position?: Record<string, unknown>;
  warnings?: string[];
  error?: { code: string; message: string; details: Record<string, unknown> };
}

export interface TaxLotView {
  id: string;
  symbol: string;
  quantity: string;
  costPerUnit: string;
  costBasis: string;
  openedAt: string;
  currency?: string;
}

export interface TaxLotMatchView {
  id: string;
  sellTransactionId: string;
  lotId: string;
  symbol: string;
  quantity: string;
  costBasis: string;
  proceeds: string;
  realizedPnl: string;
  acquiredAt: string;
  disposedAt: string;
  /** null when the engine cannot determine the holding period honestly. */
  holdingDays: number | null;
}

export interface TaxLotsResult {
  portfolioId: string;
  method: CostBasisMethod;
  totalRealizedPnl?: string;
  symbols: Array<{
    symbol: string;
    remainingQuantity: string;
    remainingCostBasis: string;
    averageCost: string | null;
    realizedPnl: string;
    openLots: TaxLotView[];
    matches: TaxLotMatchView[];
  }>;
}

export interface RebalanceResult {
  status: string;
  method: string;
  planId?: string;
  recommendations: Array<{
    symbol: string;
    action: "BUY" | "SELL";
    quantity: string | null;
    price: string | null;
    estimatedValue: string;
    estFees: string;
    currentWeight: number;
    targetWeight: number;
    reason: string;
    taxImpact: string | null;
    estRealizedPnl: string | null;
  }>;
  estimatedTotalFees: string;
  estimatedNetCash: string;
  concentrationBefore: number | null;
  concentrationAfter: number | null;
  warnings: string[];
  execution: string;
}

/** Marks the front door passes to the runtime so valuation is real, not guessed. */
export interface MarkInput {
  price?: string | number;
  currency?: string;
  timestamp?: string;
  source?: string;
}
export type MarksMap = Record<string, MarkInput>;

async function request<T>(path: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const healthy = await ensurePythonRuntime();
  if (!healthy) {
    throw new HarnessRuntimeUnavailable(
      "The Python agent runtime is not available. Install the runtime: npm run setup:python",
    );
  }
  const { timeoutMs = 45_000, ...rest } = init;
  const operatorToken = runtimeCallbackToken();
  const res = await fetch(`${HARNESS_PY_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(operatorToken ? { "X-Harness-Operator": operatorToken } : {}),
      ...(rest.headers ?? {}),
    },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (body as { detail?: string }).detail || `runtime HTTP ${res.status}`;
    throw new Error(redactSecrets(detail));
  }
  return body as T;
}

export async function listPortfolios(): Promise<PortfolioSummary[]> {
  const body = await request<{ portfolios: PortfolioSummary[] }>("/portfolio/list", { method: "GET" });
  return body.portfolios ?? [];
}

export async function createPortfolio(input: {
  name: string;
  baseCurrency?: string;
  costBasisMethod?: CostBasisMethod;
  description?: string;
}): Promise<PortfolioSummary> {
  return request<PortfolioSummary>("/portfolio/create", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function deletePortfolio(id: string): Promise<void> {
  await request(`/portfolio/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function queryPortfolio(
  id: string,
  opts: { include?: string[]; marks?: MarksMap; confidence?: number } = {},
): Promise<PortfolioRead> {
  return request<PortfolioRead>(`/portfolio/${encodeURIComponent(id)}/query`, {
    method: "POST",
    body: JSON.stringify({ include: opts.include, marks: opts.marks, confidence: opts.confidence }),
  });
}

export async function addPosition(
  id: string,
  input: { symbol: string; quantity: string; price?: string; fees?: string; taxes?: string; currency?: string; tradeDate?: string; type?: string },
): Promise<TransactionResult> {
  return request<TransactionResult>(`/portfolio/${encodeURIComponent(id)}/position`, {
    method: "POST",
    body: JSON.stringify({
      symbol: input.symbol,
      quantity: input.quantity,
      price: input.price,
      fees: input.fees,
      taxes: input.taxes,
      currency: input.currency,
      trade_date: input.tradeDate,
      type: input.type ?? "BUY",
    }),
  });
}

export async function removePosition(
  id: string,
  input: { symbol: string; price?: string; reason?: string },
): Promise<TransactionResult> {
  return request<TransactionResult>(`/portfolio/${encodeURIComponent(id)}/position/remove`, {
    method: "POST",
    body: JSON.stringify({ symbol: input.symbol, price: input.price, reason: input.reason, use_market_price: !input.price }),
  });
}

export async function updatePosition(
  id: string,
  input: { symbol: string; targetQuantity: string; reason: string; price?: string },
): Promise<TransactionResult> {
  return request<TransactionResult>(`/portfolio/${encodeURIComponent(id)}/position/update`, {
    method: "POST",
    body: JSON.stringify({ symbol: input.symbol, target_quantity: input.targetQuantity, reason: input.reason, price: input.price }),
  });
}

export async function setPositionMetadata(
  id: string,
  input: { symbol: string; name?: string; assetClass?: string; sector?: string; country?: string; strategy?: string; currency?: string },
): Promise<{ status: string }> {
  return request(`/portfolio/${encodeURIComponent(id)}/metadata`, {
    method: "POST",
    body: JSON.stringify({
      symbol: input.symbol,
      name: input.name,
      asset_class: input.assetClass,
      sector: input.sector,
      country: input.country,
      strategy: input.strategy,
      currency: input.currency,
    }),
  });
}

export async function taxLots(id: string, symbol?: string): Promise<TaxLotsResult> {
  const q = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
  return request<TaxLotsResult>(`/portfolio/${encodeURIComponent(id)}/tax-lots${q}`, { method: "GET" });
}

export async function rebalance(
  id: string,
  input: { target: Record<string, number>; allowedDrift?: number; method?: string; feeRate?: number; marks?: MarksMap },
): Promise<RebalanceResult> {
  return request<RebalanceResult>(`/portfolio/${encodeURIComponent(id)}/rebalance`, {
    method: "POST",
    body: JSON.stringify({
      target: input.target,
      allowed_drift: input.allowedDrift,
      method: input.method,
      fee_rate: input.feeRate,
      marks: input.marks,
    }),
  });
}

export async function snapshotPortfolio(id: string, marks?: MarksMap): Promise<Record<string, unknown>> {
  return request(`/portfolio/${encodeURIComponent(id)}/snapshot`, {
    method: "POST",
    body: JSON.stringify({ marks }),
  });
}

export async function listSnapshots(id: string, limit = 365): Promise<Array<Record<string, unknown>>> {
  const body = await request<{ snapshots: Array<Record<string, unknown>> }>(
    `/portfolio/${encodeURIComponent(id)}/snapshots?limit=${limit}`,
    { method: "GET" },
  );
  return body.snapshots ?? [];
}

export interface CorporateActionResult {
  status: "SUCCESS" | "FAILED" | "PARTIAL";
  action?: string;
  transactionIds?: Array<string | null>;
  error?: { code: string; message: string };
  [key: string]: unknown;
}

export async function applyCorporateAction(
  id: string,
  input: Record<string, unknown>,
): Promise<CorporateActionResult> {
  return request<CorporateActionResult>(`/portfolio/${encodeURIComponent(id)}/corporate-action`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function recordSwap(
  id: string,
  input: { fromSymbol: string; toSymbol: string; fromQuantity: string; fromPrice: string; toQuantity?: string; toPrice?: string; fees?: string },
): Promise<CorporateActionResult> {
  return request<CorporateActionResult>(`/portfolio/${encodeURIComponent(id)}/swap`, {
    method: "POST",
    body: JSON.stringify({
      from_symbol: input.fromSymbol, to_symbol: input.toSymbol,
      from_quantity: input.fromQuantity, from_price: input.fromPrice,
      to_quantity: input.toQuantity, to_price: input.toPrice, fees: input.fees,
    }),
  });
}

export async function recordBridge(
  id: string,
  input: { fromSymbol: string; toSymbol: string; quantity: string; toQuantity?: string },
): Promise<CorporateActionResult> {
  return request<CorporateActionResult>(`/portfolio/${encodeURIComponent(id)}/bridge`, {
    method: "POST",
    body: JSON.stringify({
      from_symbol: input.fromSymbol, to_symbol: input.toSymbol,
      quantity: input.quantity, to_quantity: input.toQuantity,
    }),
  });
}

export async function recordReward(
  id: string,
  input: { type?: "STAKING_REWARD" | "AIRDROP"; symbol: string; quantity: string; price?: string; currency?: string },
): Promise<TransactionResult> {
  return request<TransactionResult>(`/portfolio/${encodeURIComponent(id)}/reward`, {
    method: "POST",
    body: JSON.stringify({ ...input, type: input.type ?? "STAKING_REWARD" }),
  });
}

export async function applyStaking(
  id: string,
  input: { action: "stake" | "unstake"; symbol: string; quantity: string; notes?: string },
): Promise<TransactionResult> {
  return request<TransactionResult>(`/portfolio/${encodeURIComponent(id)}/staking`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function rebuildPortfolio(id: string): Promise<{ status: string; symbols: number }> {
  return request(`/portfolio/${encodeURIComponent(id)}/rebuild`, { method: "POST" });
}
