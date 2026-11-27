import { describe, expect, it } from "vitest";
import {
  BUSY_GRACE_MS,
  DEFAULT_REQUEST_WAIT_MS,
  DEFAULT_START_TIMEOUT_MS,
  hasRunsToProtect,
  isRecentlyHealthy,
  resolveRequestWaitMs,
  resolveStartTimeoutMs,
  shouldClearSilentRuntime,
  SILENT_KILL_WINDOW_MS,
  SILENT_MISSES_BEFORE_KILL,
  shouldReplaceStaleRuntime,
} from "./runtime-supervision";

const ours = (over: Partial<Parameters<typeof shouldReplaceStaleRuntime>[0]> = {}) => ({
  ok: true,
  service: "harness-runtime",
  startedAtMs: 1000,
  activeRuns: 0,
  ...over,
});

describe("shouldReplaceStaleRuntime", () => {
  it("keeps a runtime whose code is newer than the source on disk", () => {
    expect(shouldReplaceStaleRuntime(ours({ startedAtMs: 2000 }), 1000)).toBe(false);
  });

  it("replaces a stale IDLE runtime so a harness edit takes effect", () => {
    expect(shouldReplaceStaleRuntime(ours({ startedAtMs: 1000, activeRuns: 0 }), 2000)).toBe(true);
  });

  it("NEVER replaces a runtime that is actively serving runs", () => {
    // A running agent (or one paused on an approval card) must survive.
    expect(shouldReplaceStaleRuntime(ours({ startedAtMs: 1000, activeRuns: 1 }), 2000)).toBe(false);
    expect(shouldReplaceStaleRuntime(ours({ startedAtMs: 1000, activeRuns: 4 }), 9999)).toBe(false);
  });

  it("ignores a service that is not ours", () => {
    expect(shouldReplaceStaleRuntime(ours({ service: "something-else", startedAtMs: 1000 }), 2000)).toBe(false);
  });

  it("is conservative when health or timestamps are missing", () => {
    expect(shouldReplaceStaleRuntime(null, 2000)).toBe(false);
    expect(shouldReplaceStaleRuntime(ours({ startedAtMs: 0 }), 2000)).toBe(false);
    expect(shouldReplaceStaleRuntime(ours({ startedAtMs: 1000 }), 0)).toBe(false);
  });
});

describe("isRecentlyHealthy", () => {
  it("treats a runtime that answered inside the grace window as busy, not hung", () => {
    const now = 1_000_000;
    expect(isRecentlyHealthy(now - 1, now)).toBe(true);
    expect(isRecentlyHealthy(now - (BUSY_GRACE_MS - 1), now)).toBe(true);
  });

  it("allows a kill only when the last success is older than the grace window", () => {
    const now = 1_000_000;
    expect(isRecentlyHealthy(now - BUSY_GRACE_MS, now)).toBe(false);
    expect(isRecentlyHealthy(now - 10 * BUSY_GRACE_MS, now)).toBe(false);
  });

  it("does not treat an unknown last-success as recent", () => {
    expect(isRecentlyHealthy(undefined, 1_000_000)).toBe(false);
  });
});

describe("shouldClearSilentRuntime", () => {
  const now = 10_000_000;

  it("NEVER clears the port on one or two transient probe misses", () => {
    // One synchronous tool or a long model call can stop the event loop for
    // longer than a readiness timeout. Killing then orphans every live chat.
    expect(shouldClearSilentRuntime({ misses: 1, firstMissAt: now - 60_000, now })).toBe(false);
    expect(shouldClearSilentRuntime({ misses: 2, firstMissAt: now - 60_000, now })).toBe(false);
  });

  it("requires the miss streak to outlast the long silence window", () => {
    expect(shouldClearSilentRuntime({
      misses: SILENT_MISSES_BEFORE_KILL,
      firstMissAt: now - (SILENT_KILL_WINDOW_MS - 1),
      now,
    })).toBe(false);
    expect(shouldClearSilentRuntime({
      misses: SILENT_MISSES_BEFORE_KILL,
      firstMissAt: now - SILENT_KILL_WINDOW_MS,
      now,
    })).toBe(true);
  });

  it("never clears a runtime that answered inside the grace window", () => {
    expect(shouldClearSilentRuntime({
      misses: 10,
      firstMissAt: now - 10 * SILENT_KILL_WINDOW_MS,
      lastHealthyAt: now - 1_000,
      now,
    })).toBe(false);
  });

  it("is conservative without a recorded first miss", () => {
    expect(shouldClearSilentRuntime({ misses: 10, now })).toBe(false);
  });
});

describe("hasRunsToProtect", () => {
  it("protects active and parked (approval/clarification) runs", () => {
    expect(hasRunsToProtect({ activeRuns: 1 })).toBe(true);
    expect(hasRunsToProtect({ parkedRuns: 1 })).toBe(true);
    expect(hasRunsToProtect({ activeRuns: 0, parkedRuns: 0 })).toBe(false);
    expect(hasRunsToProtect(null)).toBe(false);
  });

  it("a parked approval protects a stale runtime from replacement", () => {
    expect(shouldReplaceStaleRuntime(ours({ startedAtMs: 1000, activeRuns: 0, parkedRuns: 1 }), 2000)).toBe(false);
  });
});

describe("resolveStartTimeoutMs", () => {
  it("uses a generous default when unset", () => {
    expect(resolveStartTimeoutMs(undefined)).toBe(DEFAULT_START_TIMEOUT_MS);
    // The measured worst case is >240 s under a concurrent build/test load;
    // the default must keep margin above it, or a still-importing runtime is
    // reported as unavailable while it is healthy.
    expect(DEFAULT_START_TIMEOUT_MS).toBeGreaterThanOrEqual(300_000);
  });

  it("honours a positive override so an operator can tune the budget", () => {
    expect(resolveStartTimeoutMs("240000")).toBe(240_000);
  });

  it("falls back to the default on a blank, non-numeric, or non-positive value", () => {
    expect(resolveStartTimeoutMs("")).toBe(DEFAULT_START_TIMEOUT_MS);
    expect(resolveStartTimeoutMs("soon")).toBe(DEFAULT_START_TIMEOUT_MS);
    expect(resolveStartTimeoutMs("0")).toBe(DEFAULT_START_TIMEOUT_MS);
    expect(resolveStartTimeoutMs("-1")).toBe(DEFAULT_START_TIMEOUT_MS);
  });
});

describe("resolveRequestWaitMs", () => {
  it("keeps a page wait far shorter than the background cold-start budget", () => {
    // A page must not inherit the 5-minute start budget: that is what made a
    // stuck runtime look like a broken app with no reason shown.
    expect(DEFAULT_REQUEST_WAIT_MS).toBeLessThan(DEFAULT_START_TIMEOUT_MS / 2);
    // A boot page has six sockets, so a long wait parks every other request
    // behind the cold start. Keep it in seconds, not tens of seconds.
    expect(DEFAULT_REQUEST_WAIT_MS).toBeLessThanOrEqual(5_000);
    expect(resolveRequestWaitMs(undefined)).toBe(DEFAULT_REQUEST_WAIT_MS);
  });

  it("honours a positive override so a slow host can be tuned", () => {
    expect(resolveRequestWaitMs("90000")).toBe(90_000);
  });

  it("falls back to the default on a blank, non-numeric, or non-positive value", () => {
    expect(resolveRequestWaitMs("")).toBe(DEFAULT_REQUEST_WAIT_MS);
    expect(resolveRequestWaitMs("later")).toBe(DEFAULT_REQUEST_WAIT_MS);
    expect(resolveRequestWaitMs("0")).toBe(DEFAULT_REQUEST_WAIT_MS);
    expect(resolveRequestWaitMs("-5")).toBe(DEFAULT_REQUEST_WAIT_MS);
  });
});
