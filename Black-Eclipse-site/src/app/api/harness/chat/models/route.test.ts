import { describe, it, expect, vi, beforeEach } from "vitest";

// The composer's model-options route must report availability from the REAL
// vault/env state and never leak a key — only booleans. This locks the contract
// the composer selector depends on.

const getLLMConfig = vi.fn();
const llmKeyEnvVar = vi.fn();
const listModelProviders = vi.fn();
const listRuntimeModels = vi.fn();
const resolveProviderKey = vi.fn();
const readDiscoveredModels = vi.fn();

vi.mock("@/lib/settings", () => ({
  getLLMConfig: (...a: unknown[]) => getLLMConfig(...a),
  llmKeyEnvVar: (...a: unknown[]) => llmKeyEnvVar(...a),
}));
vi.mock("@/lib/harness", () => ({
  listModelProviders: (...a: unknown[]) => listModelProviders(...a),
  listRuntimeModels: (...a: unknown[]) => listRuntimeModels(...a),
}));
vi.mock("@/lib/provider-keys", () => ({ resolveProviderKey: (...a: unknown[]) => resolveProviderKey(...a) }));
vi.mock("@/lib/agent/discovered-models", () => ({
  readDiscoveredModels: (...a: unknown[]) => readDiscoveredModels(...a),
}));

import { GET } from "@/app/api/harness/chat/models/route";

beforeEach(() => {
  getLLMConfig.mockReset();
  llmKeyEnvVar.mockReset();
  listModelProviders.mockReset();
  listRuntimeModels.mockReset();
  resolveProviderKey.mockReset();
  readDiscoveredModels.mockReset();
  getLLMConfig.mockResolvedValue({ provider: "deepseek", model: "deepseek-chat", baseUrl: "https://api.deepseek.com/v1" });
  llmKeyEnvVar.mockImplementation((_baseUrl: string, provider?: string) => {
    if (provider === "deepseek") return "DEEPSEEK_API_KEY";
    if (provider === "openai") return "OPENAI_API_KEY";
    if (provider === "opencode-go") return "OPENCODE_GO_API_KEY";
    if (provider === "custom") return "CUSTOM_LLM_API_KEY";
    return undefined;
  });
  listModelProviders.mockResolvedValue([
    { id: "deepseek", models: ["deepseek-chat", "deepseek-reasoner"], dynamicDiscovery: false },
    { id: "ollama", models: ["llama3.1"], dynamicDiscovery: true },
  ]);
  listRuntimeModels.mockResolvedValue([]);
  resolveProviderKey.mockResolvedValue(null);
  readDiscoveredModels.mockResolvedValue({ providers: {}, served: {}, updatedAt: new Date(0).toISOString() });
});

describe("GET /api/harness/chat/models", () => {
  it("returns the active provider/model and the preset list", async () => {
    const res = await GET();
    const body = await res.json();
    expect(body.active.provider).toBe("deepseek");
    expect(body.active.model).toBe("deepseek-chat");
    const deepseek = body.providers.find((p: { id: string }) => p.id === "deepseek");
    expect(deepseek.models).toContain("deepseek-reasoner");
    // Local/keyless providers are always available.
    const ollama = body.providers.find((p: { id: string }) => p.id === "ollama");
    expect(ollama.available).toBe(true);
    expect(ollama.keyless).toBe(true);
  });

  it("reports a hosted provider as available only when a key resolves", async () => {
    resolveProviderKey.mockImplementation(async (envVar: string) => (envVar === "OPENAI_API_KEY" ? "sk-x" : null));
    const res = await GET();
    const body = await res.json();
    const openai = body.providers.find((p: { id: string }) => p.id === "openai");
    const deepseek = body.providers.find((p: { id: string }) => p.id === "deepseek");
    expect(openai.available).toBe(true);
    expect(deepseek.available).toBe(false);
  });

  it("never returns a key-shaped value in any provider entry", async () => {
    resolveProviderKey.mockResolvedValue("secret-value");
    const res = await GET();
    const text = JSON.stringify(await res.json());
    expect(text).not.toContain("secret-value");
    expect(text).not.toContain("apiKey");
  });

  it("still returns the declared presets when the runtime catalog is unavailable", async () => {
    listModelProviders.mockRejectedValue(new Error("runtime down"));
    const res = await GET();
    const body = await res.json();
    expect(body.providers.length).toBeGreaterThan(0);
    const openai = body.providers.find((p: { id: string }) => p.id === "openai");
    expect(openai.models.length).toBeGreaterThan(0);
  });

  it("merges runtime-discovered models into the provider's list", async () => {
    // A discovered local model the static catalog has never heard of must show
    // up (and be listed first) so it survives a reload after "Discover".
    listRuntimeModels.mockResolvedValue([
      { id: "deepseek-v4.1-flash:cloud", providerId: "ollama" },
      { id: "glm-5.3:cloud", providerId: "ollama" },
    ]);
    const res = await GET();
    const body = await res.json();
    const ollama = body.providers.find((p: { id: string }) => p.id === "ollama");
    expect(ollama.models).toContain("deepseek-v4.1-flash:cloud");
    expect(ollama.models).toContain("glm-5.3:cloud");
    expect(ollama.models).toContain("llama3.1");
  });

  it("does not fail when the runtime model registry is unavailable", async () => {
    listRuntimeModels.mockRejectedValue(new Error("runtime down"));
    const res = await GET();
    const body = await res.json();
    const ollama = body.providers.find((p: { id: string }) => p.id === "ollama");
    expect(ollama.models).toContain("llama3.1");
  });

  it("reports only genuinely-served models in `served`, never declared placeholders", async () => {
    // A local server that was probed: the durable snapshot records the served
    // subset separately from the declared list, so the composer can pick a
    // default that will not 404.
    readDiscoveredModels.mockResolvedValue({
      providers: { ollama: ["llama3.1", "deepseek-v4.1-flash:cloud"] },
      served: { ollama: ["deepseek-v4.1-flash:cloud"] },
      updatedAt: new Date().toISOString(),
    });
    const res = await GET();
    const body = await res.json();
    const ollama = body.providers.find((p: { id: string }) => p.id === "ollama");
    expect(ollama.models).toContain("llama3.1");
    expect(ollama.models).toContain("deepseek-v4.1-flash:cloud");
    expect(ollama.served).toEqual(["deepseek-v4.1-flash:cloud"]);
    expect(ollama.served).not.toContain("llama3.1");
  });

  it("includes runtime-discovered (dynamic) models in `served` but not declared ones", async () => {
    listRuntimeModels.mockResolvedValue([
      { id: "deepseek-v4.1-flash:cloud", providerId: "ollama", metadata: { dynamic: true } },
      { id: "llama3.1", providerId: "ollama", metadata: { dynamic: false } },
    ]);
    const res = await GET();
    const body = await res.json();
    const ollama = body.providers.find((p: { id: string }) => p.id === "ollama");
    expect(ollama.served).toContain("deepseek-v4.1-flash:cloud");
    expect(ollama.served).not.toContain("llama3.1");
  });

  it("returns an empty `served` list when nothing has been probed", async () => {
    const res = await GET();
    const body = await res.json();
    const ollama = body.providers.find((p: { id: string }) => p.id === "ollama");
    expect(ollama.served).toEqual([]);
  });

  it("marks the active provider available from the saved credential", async () => {
    // The key can live under a slot the preset env var does not name (an edited
    // base URL, a vault-only entry). `getLLMConfig` resolved one, so the picker
    // must not report the active provider as "needs a key" and hide it.
    getLLMConfig.mockResolvedValue({
      provider: "opencode-go",
      model: "kimi-k3",
      baseUrl: "https://opencode.ai/inference/go/openai/v1",
      apiKey: "sk-real",
    });
    const res = await GET();
    const body = await res.json();
    const go = body.providers.find((p: { id: string }) => p.id === "opencode-go");
    expect(go.available).toBe(true);
    expect(go.models).toContain("kimi-k3");
    expect(body.active.provider).toBe("opencode-go");
  });

  it("resolves a named custom connection's own key and reports its endpoint", async () => {
    getLLMConfig.mockResolvedValue({
      provider: "custom",
      model: "company-llm-1",
      baseUrl: "https://llm.company.com/v1",
      connectionId: "conn-1",
      apiKey: "",
    });
    llmKeyEnvVar.mockReturnValue("CUSTOM_CONNECTION_CONN-1_API_KEY");
    resolveProviderKey.mockImplementation(async (envVar: string) =>
      envVar === "CUSTOM_CONNECTION_CONN-1_API_KEY" ? "gw-key" : null,
    );
    const res = await GET();
    const body = await res.json();
    const custom = body.providers.find((p: { id: string }) => p.id === "custom");
    expect(custom.available).toBe(true);
    expect(custom.baseUrl).toBe("https://llm.company.com/v1");
    expect(custom.models).toContain("company-llm-1");
    expect(body.active.connectionId).toBe("conn-1");
  });
});
