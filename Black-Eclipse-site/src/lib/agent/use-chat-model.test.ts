import { describe, expect, it } from "vitest";
import {
  pickServableModel,
  shouldDropStoredChoice,
  shouldDropStoredLocalChoice,
} from "./use-chat-model";
import type { ChatModelProvider } from "./use-chat-model";

function provider(overrides: Partial<ChatModelProvider>): ChatModelProvider {
  return {
    id: "opencode",
    label: "OpenCode Zen",
    baseUrl: "https://opencode.ai/inference/openai/v1",
    keyless: false,
    envVar: "OPENCODE_API_KEY",
    dynamicDiscovery: true,
    available: true,
    models: [],
    ...overrides,
  };
}

describe("shouldDropStoredChoice", () => {
  it("drops a Zen choice when Zen has no key and Go is the connected provider", () => {
    expect(
      shouldDropStoredChoice(
        { provider: "opencode", model: "deepseek-v4.1-flash-free" },
        [
          provider({ id: "opencode", available: false }),
          provider({
            id: "opencode-go",
            label: "OpenCode Go",
            available: true,
            envVar: "OPENCODE_GO_API_KEY",
          }),
        ],
      ),
    ).toBe(true);
  });

  it("keeps a deliberately selected provider that still has a key", () => {
    expect(
      shouldDropStoredChoice(
        { provider: "opencode", model: "glm-5.3" },
        [provider({ id: "opencode", available: true })],
      ),
    ).toBe(false);
  });

  it("never drops a keyless local provider", () => {
    expect(
      shouldDropStoredChoice(
        { provider: "ollama", model: "llama3.1" },
        [provider({ id: "ollama", keyless: true, available: false })],
      ),
    ).toBe(false);
  });
});

describe("shouldDropStoredLocalChoice", () => {
  const providers = [
    provider({ id: "ollama", label: "Ollama", keyless: true, available: true }),
    provider({ id: "opencode-go", label: "OpenCode Go", available: true }),
  ];

  it("drops a stale Ollama pick once a real provider is saved", () => {
    expect(
      shouldDropStoredLocalChoice({ provider: "ollama", model: "llama3.1" }, providers, "opencode-go"),
    ).toBe(true);
  });

  it("keeps a deliberate pick of another keyed provider", () => {
    expect(
      shouldDropStoredLocalChoice(
        { provider: "opencode", model: "glm-5.3" },
        [provider({ id: "opencode", available: true }), providers[1]],
        "opencode-go",
      ),
    ).toBe(false);
  });

  it("does nothing before the saved provider is known", () => {
    expect(
      shouldDropStoredLocalChoice({ provider: "ollama", model: "llama3.1" }, providers, "auto"),
    ).toBe(false);
    expect(
      shouldDropStoredLocalChoice({ provider: "ollama", model: "llama3.1" }, providers, "mock"),
    ).toBe(false);
  });

  it("keeps the local pick when the saved provider is that same local server", () => {
    expect(
      shouldDropStoredLocalChoice({ provider: "ollama", model: "llama3.1" }, providers, "ollama"),
    ).toBe(false);
  });

  it("does not promote a saved provider that is not usable", () => {
    expect(
      shouldDropStoredLocalChoice(
        { provider: "ollama", model: "llama3.1" },
        [...providers, provider({ id: "opencode", available: false })],
        "opencode",
      ),
    ).toBe(false);
  });
});

// The default-model reconciliation: a keyless local provider's declared default
// can be a placeholder the server does not serve (`llama3.1` on a box with only
// cloud models), which makes every Ask AI / Analyze run fail with a raw 404.
// These lock the rule that the default only changes to a model known to be
// served, and a user's explicit choice is never overridden.

describe("pickServableModel", () => {
  it("adopts the first served model when the current one is not served", () => {
    expect(
      pickServableModel(["deepseek-v4.1-flash:cloud", "glm-5.3:cloud"], "llama3.1"),
    ).toBe("deepseek-v4.1-flash:cloud");
  });

  it("keeps a current model that is already served (an explicit choice stands)", () => {
    expect(pickServableModel(["llama3.1", "deepseek-v4.1-flash:cloud"], "llama3.1")).toBeNull();
  });

  it("does nothing when nothing is known to be served", () => {
    expect(pickServableModel([], "llama3.1")).toBeNull();
  });

  it("ignores empty/garbage entries", () => {
    expect(pickServableModel(["", "glm-5.3:cloud"], "llama3.1")).toBe("glm-5.3:cloud");
    expect(pickServableModel(["", ""], "llama3.1")).toBeNull();
  });

  it("adopts a served model even when nothing is currently selected", () => {
    expect(pickServableModel(["deepseek-v4.1-flash:cloud"], "")).toBe("deepseek-v4.1-flash:cloud");
  });
});
