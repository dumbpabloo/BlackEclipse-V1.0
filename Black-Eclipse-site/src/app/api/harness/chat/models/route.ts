import { NextResponse } from "next/server";
import { getLLMConfig, llmKeyEnvVar } from "@/lib/settings";
import { listModelProviders, listRuntimeModels } from "@/lib/harness";
import { LLM_PRESETS } from "@/lib/agent/presets";
import { resolveProviderKey } from "@/lib/provider-keys";
import { readDiscoveredModels } from "@/lib/agent/discovered-models";
import { sortModelsNewestFirst } from "@/lib/agent/model-recency";

export const dynamic = "force-dynamic";

// GET /api/harness/chat/models — the composer's model selector options.
//
// Returns the ACTIVE provider/model plus every provider the operator can use
// right now, each with the models the runtime catalog declares for it. A
// provider is `available` when it is keyless (a local server) or a key resolves
// from the environment/vault — determined server-side where the vault lives, and
// only ever reported as a boolean (a key is never returned).
//
// The active provider resolves its key through `llmKeyEnvVar`, the SAME helper
// `getLLMConfig` uses, so an edited base URL or a named custom connection (whose
// key lives under its own vault entry) is reported honestly instead of showing
// as "needs a key" and disappearing from the composer.
//
// The runtime catalog is best-effort: when the Python runtime is unavailable the
// declared presets are still returned (with their default model), so the picker
// never renders empty.
export async function GET() {
  let active: { provider: string; model: string; baseUrl: string; connectionId?: string } = {
    provider: "auto",
    model: "",
    baseUrl: "",
  };
  // True when the saved config has a resolvable credential (never exposed). A
  // saved connection is therefore marked available even if its key sits under a
  // non-preset vault slot (e.g. `CUSTOM_LLM_…` after an edited base URL).
  let activeHasKey = false;
  try {
    const cfg = await getLLMConfig();
    active = {
      provider: cfg.provider,
      model: cfg.model,
      baseUrl: cfg.baseUrl,
      ...(cfg.connectionId ? { connectionId: cfg.connectionId } : {}),
    };
    activeHasKey = Boolean(cfg.apiKey);
  } catch {
    // Settings unavailable — the picker still works from the preset list.
  }

  let catalog: Array<{ id?: string; models?: string[]; dynamicDiscovery?: boolean }> = [];
  try {
    catalog = (await listModelProviders()) as typeof catalog;
  } catch {
    catalog = [];
  }
  const byId = new Map(catalog.filter((p) => p?.id).map((p) => [p.id as string, p]));

  // Discovered models live in the runtime's in-process registry (registered by
  // a "Discover installed models" probe). The static catalog above never lists
  // them, so a local server's installed models would vanish after a reload.
  // Merge them in best-effort, grouped by provider, so the composer keeps
  // showing everything the runtime actually knows about. The durable
  // SystemConfig snapshot is merged first (it survives a runtime restart), then
  // the runtime registry on top (freshest).
  const discoveredByProvider = new Map<string, string[]>();
  // The freshest complete list from the last successful probe. It is replaced
  // (not merged) on every probe, so it — not the accumulated set — is the truth
  // about what a provider still serves.
  const freshByProvider = new Map<string, string[]>();
  // The subset the provider is KNOWN to actually serve (genuinely probed, not
  // the static catalog's declared placeholders). Used by the composer to pick a
  // default that will not 404 — a keyless local server may not have the
  // catalog's default model installed at all.
  const servedByProvider = new Map<string, string[]>();
  const addServable = (providerId: string, id: string) => {
    if (!providerId || !id) return;
    const list = servedByProvider.get(providerId) ?? [];
    if (!list.includes(id)) list.push(id);
    servedByProvider.set(providerId, list);
  };
  try {
    const snapshot = await readDiscoveredModels();
    for (const [id, ids] of Object.entries(snapshot.providers)) {
      discoveredByProvider.set(id, [...ids]);
    }
    for (const [id, ids] of Object.entries(snapshot.served)) {
      if (ids.length) freshByProvider.set(id, [...ids]);
      for (const model of ids) addServable(id, model);
    }
  } catch {
    // No durable snapshot — the runtime registry below still contributes.
  }
  try {
    const models = await listRuntimeModels();
    for (const m of models) {
      const list = discoveredByProvider.get(m.providerId) ?? [];
      if (m.id && !list.includes(m.id)) list.push(m.id);
      discoveredByProvider.set(m.providerId, list);
      // A model the runtime only knows because it was DISCOVERED is genuinely
      // served; a declared catalog entry is not (`dynamic` is false).
      if (m.id && m.metadata?.dynamic === true) addServable(m.providerId, m.id);
    }
  } catch {
    // Runtime unavailable — the declared presets below still render.
  }

  const providers = await Promise.all(
    LLM_PRESETS.map(async (preset) => {
      const cat = byId.get(preset.id);
      const declared = (cat?.models && cat.models.length ? cat.models : [preset.model]).filter(Boolean);
      // Prefer the last probe's replaced list; fall back to the accumulated one.
      const discovered = freshByProvider.get(preset.id) ?? discoveredByProvider.get(preset.id) ?? [];
      const isActive = preset.id === active.provider;
      const models = sortModelsNewestFirst(
        Array.from(
          new Set([...(isActive && active.model ? [active.model] : []), ...discovered, ...declared]),
        ).filter(Boolean),
      );
      let available = Boolean(preset.keyless) || (isActive && activeHasKey);
      if (!available) {
        // Resolve the SAME slot the runtime reads: a named custom connection has
        // its own key, and an edited base URL may store under `CUSTOM_LLM_…`.
        const envVar = isActive
          ? llmKeyEnvVar(active.baseUrl || preset.baseUrl, active.provider, active.connectionId) ??
            preset.envVar
          : preset.envVar;
        if (envVar) {
          try {
            available = Boolean(await resolveProviderKey(envVar));
          } catch {
            available = false;
          }
        }
      }
      return {
        id: preset.id,
        label: preset.name,
        baseUrl: isActive ? active.baseUrl || preset.baseUrl : preset.baseUrl,
        keyless: Boolean(preset.keyless),
        envVar: preset.envVar ?? null,
        dynamicDiscovery: Boolean(cat?.dynamicDiscovery),
        available,
        models,
        // The subset the provider is known to actually serve (see above). Empty
        // when no successful probe result is known yet.
        served: servedByProvider.get(preset.id) ?? [],
      };
    }),
  );

  return NextResponse.json({ success: true, active, providers });
}
