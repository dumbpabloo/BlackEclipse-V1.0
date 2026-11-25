import { describe, it, expect } from "vitest";
import { HarnessError, toHarnessErrorJson, redactSecrets, isHarnessError } from "./errors";

describe("redactSecrets", () => {
  it("redacts bearer tokens", () => {
    expect(redactSecrets("failed Bearer abc123def456")).toContain("Bearer [redacted]");
    expect(redactSecrets("failed Bearer abc123def456")).not.toContain("abc123def456");
  });

  it("redacts sk- style keys", () => {
    expect(redactSecrets("key sk-abcdefghijklmnop")).toContain("[redacted-key]");
  });

  it("redacts key=value pairs", () => {
    const out = redactSecrets('{"api_key":"super-secret-value"}');
    expect(out).not.toContain("super-secret-value");
    expect(out).toContain("[redacted]");
  });
});

describe("HarnessError", () => {
  it("serializes to a redacted json record", () => {
    const err = new HarnessError("PROVIDER_ERROR", "Bearer topsecret1234 rejected");
    const json = err.toJSON();
    expect(json.code).toBe("PROVIDER_ERROR");
    expect(json.message).not.toContain("topsecret1234");
  });
});

describe("toHarnessErrorJson", () => {
  it("passes through HarnessError codes", () => {
    expect(toHarnessErrorJson(new HarnessError("TOOL_TIMEOUT", "x")).code).toBe("TOOL_TIMEOUT");
  });

  it("falls back for unknown values", () => {
    expect(toHarnessErrorJson("plain string").code).toBe("INTERNAL_ERROR");
  });

  it("keeps a cold-boot code distinct from an unavailable runtime", () => {
    // A boot is transient and retryable; an install problem is not. Collapsing
    // them into RUNTIME_UNAVAILABLE is what told people to install a runtime
    // that was merely still starting.
    const err = Object.assign(new Error("starting"), { code: "RUNTIME_STARTING" });
    expect(toHarnessErrorJson(err)).toMatchObject({ code: "RUNTIME_STARTING" });
  });

  it("isHarnessError identifies the type", () => {
    expect(isHarnessError(new HarnessError("INTERNAL_ERROR", "x"))).toBe(true);
    expect(isHarnessError(new Error("x"))).toBe(false);
  });
});
