import { describe, expect, it } from "vitest";
import { isAllowedTradingRoute } from "./trading-routes";

describe("trading route allowlist", () => {
  it("allows the read surface", () => {
    expect(isAllowedTradingRoute("GET", ["brokers"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "account"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "positions"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "orders"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "events"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "reconciliation"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "fills"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "sync"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "smoke"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "quote"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "symbols", "XAUUSD"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "symbols", "EURUSD.a"])).toBe(true);
  });

  it("allows connection management and a probe", () => {
    expect(isAllowedTradingRoute("POST", ["connections"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "test"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "arm"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "disarm"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "kill"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "mandates"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["mandates"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["mandates", "mnd_abc123", "revoke"])).toBe(true);
    expect(isAllowedTradingRoute("PATCH", ["connections", "brk_abc123"])).toBe(true);
    expect(isAllowedTradingRoute("DELETE", ["connections", "brk_abc123"])).toBe(true);
  });

  it("allows the demo execution flow but not a direct send", () => {
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "orders", "preview"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "orders", "submit"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "orders", "12345", "cancel"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "orders", "12345", "modify"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "positions", "12345", "close"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "positions", "12345", "modify"])).toBe(true);
    // REST venues return opaque ids: UUIDs, exchange ids, and INSTRUMENT:SIDE
    // tickets. They must survive the proxy's path allowlist.
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "orders",
      "b9a1e5c4-7f00-4a3e-9d1c-0e6a2b7f1234", "cancel"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "positions",
      "EUR_USD:LONG", "close"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "positions",
      "BTCUSDT:0", "modify"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "positions",
      "BTC-USD", "close"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "positions", "close"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "orders", "ledger"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["orders", "ord_abc123"])).toBe(true);
    expect(isAllowedTradingRoute("GET", ["approvals"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["approvals", "appr_abc123", "decide"])).toBe(true);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "orders"])).toBe(false);
    expect(isAllowedTradingRoute("POST", ["orders"])).toBe(false);
  });

  it("never proxies an order route", () => {
    expect(isAllowedTradingRoute("POST", ["orders"])).toBe(false);
    expect(isAllowedTradingRoute("POST", ["connections", "brk_abc123", "orders"])).toBe(false);
    expect(isAllowedTradingRoute("DELETE", ["connections"])).toBe(false);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "password"])).toBe(false);
    expect(isAllowedTradingRoute("DELETE", ["connections", "brk_abc123", "positions", "12345", "close"])).toBe(false);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "positions", "close"])).toBe(false);
  });

  it("rejects path traversal and unknown ids", () => {
    expect(isAllowedTradingRoute("GET", ["connections", "..", "account"])).toBe(false);
    expect(isAllowedTradingRoute("GET", ["connections", "brk_abc123", "account", "extra"])).toBe(false);
    expect(isAllowedTradingRoute("PUT", ["connections", "brk_abc123"])).toBe(false);
  });
});
