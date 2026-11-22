import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Regression guard for the onboarding AI step. Original bugs:
//  1. the connect panel collapsed the instant the probe succeeded, so the
//     success view and its model picker were never seen;
//  2. the connected state had no way to test the endpoint and no model choice.
// No React testing library is installed, so this locks the wiring at the source
// level (the pattern the other chat/setup guards use).

const SOURCE = readFileSync(
  fileURLToPath(new URL("./ai-step.tsx", import.meta.url)),
  "utf8",
);

describe("onboarding AI step", () => {
  it("keeps the connect panel open until the operator confirms", () => {
    // The panel is explicit state, not derived from onConnected's draft update.
    expect(SOURCE).toMatch(/const \[panelOpen, setPanelOpen\] = useState/);
    expect(SOURCE).toMatch(/\{panelOpen && \(/);
    // The success view closes via onCancel, not by mutating aiKind.
    expect(SOURCE).toMatch(/onCancel=\{hasSavedConfig \? \(\) => setPanelOpen\(false\) : undefined\}/);
  });

  it("offers a real test and a model switch once connected", () => {
    expect(SOURCE).toContain("/api/agent/test");
    expect(SOURCE).toContain("/api/llm/active");
    expect(SOURCE).toContain("Test connection");
    expect(SOURCE).toMatch(/onChange=\{\(e\) => void changeModel\(e\.target\.value\)\}/);
  });

  it("does not let 'Set up later' wipe an existing connection", () => {
    expect(SOURCE).toMatch(/allowSkip=\{!hasSavedConfig\}/);
  });
});
