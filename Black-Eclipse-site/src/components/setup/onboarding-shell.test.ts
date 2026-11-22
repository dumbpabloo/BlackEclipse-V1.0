import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Regression guard for the setup wizard frame. The rails/footer live in one
// source file; no React testing library is installed (vitest runs in `node`),
// so this asserts the layout contract at the source level, the same pattern the
// other surface guards use.
//
// Original bug: with a short window the left rail's full stepper forced a
// min-content height on the grid row, the card overflowed, and `overflow-hidden`
// clipped the footer — Continue/Skip disappeared. The rail was also navigable
// only on the title text, and only backwards.

const SOURCE = readFileSync(
  fileURLToPath(new URL("./onboarding-shell.tsx", import.meta.url)),
  "utf8",
);

describe("onboarding shell layout", () => {
  it("lets the rail and section shrink instead of pushing the footer out", () => {
    expect(SOURCE).toMatch(/<aside className="[^"]*\bmin-h-0\b/);
    expect(SOURCE).toMatch(/<ol className="[^"]*\boverflow-y-auto\b/);
    expect(SOURCE).toMatch(/className="mt-8 shrink-0 space-y-1/);
  });

  it("pins the action footer and keeps a 100vh fallback for the card", () => {
    expect(SOURCE).toMatch(/className="flex shrink-0 flex-wrap items-center/);
    expect(SOURCE).toContain("max-h-[calc(100vh-1.5rem)]");
    expect(SOURCE).toContain("supports-[height:100dvh]:max-h-[calc(100dvh-1.5rem)]");
  });
});

describe("onboarding stepper navigation", () => {
  it("makes the whole row (disc included) a button", () => {
    expect(SOURCE).toMatch(/aria-label=\{`\$\{step\.title\} — \$\{step\.short\}`\}/);
    expect(SOURCE).toMatch(/onClick=\{\(\) => goToStep\(index\)\}/);
  });

  it("allows any step once the risk notice is acknowledged", () => {
    expect(SOURCE).toMatch(/const reachable = draft\.riskAck;/);
    expect(SOURCE).toMatch(/const disabled = !reachable \|\| isActive;/);
    // The old forward-only block must not come back.
    expect(SOURCE).not.toContain("index < draft.step + 1");
  });
});
