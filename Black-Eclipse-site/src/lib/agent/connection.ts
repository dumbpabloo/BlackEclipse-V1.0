// ── Provider connection engine (server-only) ────────────────────────────────
// One place that knows how to *really* test an LLM endpoint and how to make a
// connection atomic. The old code probed every provider with
// `Authorization: Bearer` against `${baseUrl}/models`, so a valid Anthropic or
// Gemini key was reported as rejected. This module uses each provider's real
// auth style and protocol.
//
// Connection = validate → probe with the correct auth → discover models →
// persist the key + active config in one step. A failed probe never writes a
// half-configured state.

import {
  authHeadersFor,
  getProvider,
  normalizeApiKey,
  normalizeBaseUrl,
  nativeOrigin,
  providerForBaseUrl,
  type ProviderDescriptor,
} from "@/lib/providers/registry";
import { saveProviderKey, storedProviderKey } from "@/lib/provider-keys";
import { llmKeyEnvVar, setLLMConfig, type LLMProviderName } from "@/lib/settings";
import { assertModelAllowed } from "@/lib/privacy";
import {
  authForConnection,
  customConnectionKeyEnvVar,
  getCustomConnection,
  saveCustomConnection,
  type CustomAuthStyle,
} from "./custom-connections";

export type ConnectionErrorCode =
  | "INVALID_INPUT"
  | "MISSING_KEY"
  | "KEY_REJECTED"
  | "RATE_LIMITED"
  | "UNREACHABLE"
  | "MODEL_REJECTED"
  | "REQUEST_REJECTED"
  | "UNVERIFIED"
  | "UNKNOWN_PROVIDER";

export interface ProbeResult {
  ok: boolean;
  /** Machine-readable reason; absent when ok. */
  code?: ConnectionErrorCode;
  /** Plain-language sentence safe to show a non-technical user. */
  message: string;
  /** Raw detail for the technician; never a secret. */
  detail?: string;
  httpStatus?: number;
  latencyMs?: number;
  models: string[];
  /** The model the probe actually used (after discovery/fallback). */
  model?: string;
}

interface Target {
  provider: ProviderDescriptor;
  baseUrl: string;
  apiKey: string;
  model: string;
  /** Overrides the registry's auth style (named custom connections). */
  auth?: { header: string; prefix: string };
  /** Stable per-probe session id for gateways that require one. */
  sessionId?: string;
}

/** A stable-enough opaque id for a single probe/test action. */
function newSessionId(): string {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  } catch {
    // fall through
  }
  return `be-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function resolveTarget(input: {
  providerId?: string;
  baseUrl?: string;
  apiKey?: string;
  model?: string;
  authStyle?: CustomAuthStyle;
  apiKeyHeader?: string;
  apiKeyPrefix?: string;
}): Target | null {
  const fallback = input.baseUrl ? providerForBaseUrl(input.baseUrl) : undefined;
  const provider = (input.providerId ? getProvider(input.providerId) : undefined) ?? fallback;
  if (!provider) return null;
  const baseUrl = normalizeBaseUrl(input.baseUrl || provider.baseUrl);
  if (!baseUrl) return null;
  const auth = input.authStyle
    ? authForConnection({
        authStyle: input.authStyle,
        apiKeyHeader: input.apiKeyHeader,
        apiKeyPrefix: input.apiKeyPrefix,
      })
    : undefined;
  return {
    provider,
    baseUrl,
    apiKey: normalizeApiKey(input.apiKey ?? ""),
    // Empty means "let discovery choose"; never silently pin a stale default.
    model: (input.model ?? "").trim(),
    ...(auth ? { auth } : {}),
    ...(provider.sessionHeader ? { sessionId: newSessionId() } : {}),
  };
}

/** The auth headers for a target, honouring a named connection's style. */
function authHeadersForTarget(target: Target, apiKey: string): Record<string, string> {
  const headers: Record<string, string> = {
    // Gateways fingerprint generic SDK clients; identify ourselves honestly.
    "user-agent": "BlackEclipse/0.4.19 (provider-connect)",
  };
  if (target.provider.sessionHeader) {
    headers[target.provider.sessionHeader] = target.sessionId ?? newSessionId();
  }
  if (!apiKey) return headers;
  if (target.auth) {
    headers[target.auth.header.toLowerCase()] = `${target.auth.prefix}${apiKey}`;
    return headers;
  }
  return { ...headers, ...authHeadersFor(target.provider, apiKey) };
}

/**
 * The model to actually send. An explicit choice always wins. Otherwise prefer
 * the provider's declared default when the live list contains it, then the
 * first CHAT-like id (a model list also contains embeddings, audio, image and
 * moderation models — picking `data[0]` blindly could probe `whisper-1`), then
 * the declared default, then the first id.
 */
export function effectiveModel(
  target: { provider: { model: string }; model: string },
  models: string[],
): string {
  if (target.model) return target.model;
  const preferred = target.provider.model;
  if (preferred && (models.length === 0 || models.includes(preferred))) return preferred;
  const nonChat = /(embed|whisper|tts|dall-e|moderation|image|audio|rerank|veo|imagen|sora)/i;
  const chatLike = models.find((m) => !nonChat.test(m));
  return chatLike || preferred || models[0] || "";
}

function modelsEndpoint(target: Target): string {
  const { provider, baseUrl } = target;
  if (provider.modelsUrl) return provider.modelsUrl;
  if (provider.protocol === "anthropic") {
    return `${nativeOrigin(provider, baseUrl)}/v1/models`;
  }
  if (provider.protocol === "google") {
    return `${nativeOrigin(provider, baseUrl)}/v1beta/models`;
  }
  return `${baseUrl}/models`;
}

function chatEndpoint(target: Target, model: string): string {
  const { provider, baseUrl } = target;
  if (provider.protocol === "anthropic") {
    return `${nativeOrigin(provider, baseUrl)}/v1/messages`;
  }
  if (provider.protocol === "google") {
    return `${nativeOrigin(provider, baseUrl)}/v1beta/models/${encodeURIComponent(
      model,
    )}:generateContent`;
  }
  return `${baseUrl}/chat/completions`;
}

function chatBody(target: Target, model: string): Record<string, unknown> {
  if (target.provider.protocol === "anthropic") {
    return {
      model,
      max_tokens: 1,
      messages: [{ role: "user", content: "ping" }],
    };
  }
  if (target.provider.protocol === "google") {
    return {
      contents: [{ role: "user", parts: [{ text: "ping" }] }],
      generationConfig: { maxOutputTokens: 1, temperature: 0 },
    };
  }
  return {
    model,
    messages: [{ role: "user", content: "ping" }],
    max_tokens: 1,
    temperature: 0,
    stream: false,
  };
}

/** Pull model ids out of every shape the common providers return. */
export function parseModelList(payload: unknown): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  const push = (value: unknown) => {
    if (typeof value !== "string") return;
    const clean = value.replace(/^models\//, "").trim();
    if (clean && !seen.has(clean)) {
      seen.add(clean);
      out.push(clean);
    }
  };
  const walk = (node: unknown) => {
    if (!node) return;
    if (Array.isArray(node)) {
      for (const entry of node) {
        if (typeof entry === "string") {
          push(entry);
        } else if (entry && typeof entry === "object") {
          const record = entry as Record<string, unknown>;
          // Prefer the wire id. A display `name` (e.g. "DeepSeek V4.1 Flash 1017")
          // is NOT a valid model id and must not double the list.
          if (typeof record.id === "string") push(record.id);
          else if (typeof record.name === "string") push(record.name);
          else push(record.model);
        }
      }
      return;
    }
    if (typeof node === "object") {
      const record = node as Record<string, unknown>;
      for (const key of ["data", "models", "model_list", "available_models"]) {
        if (key in record) walk(record[key]);
      }
    }
  };
  walk(payload);
  return out;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function readDetail(response: Response): Promise<string | undefined> {
  try {
    const text = await response.text();
    if (!text) return undefined;
    return text.slice(0, 400);
  } catch {
    return undefined;
  }
}

function friendlyHttpFailure(
  provider: ProviderDescriptor,
  status: number,
): { code: ConnectionErrorCode; message: string } {
  if (status === 401 || status === 403) {
    return {
      code: "KEY_REJECTED",
      message: `${provider.name} rejected the key. Copy it again from the provider's key page.`,
    };
  }
  if (status === 429) {
    return {
      code: "RATE_LIMITED",
      message: `${provider.name} is rate-limiting requests right now. Wait a moment and try again.`,
    };
  }
  if (status === 404) {
    return {
      code: "MODEL_REJECTED",
      message: `${provider.name} answered, but it does not serve that model. Pick a model from the list.`,
    };
  }
  if (status === 400) {
    return {
      code: "REQUEST_REJECTED",
      message: `${provider.name} rejected the request (HTTP 400). The detail below explains why.`,
    };
  }
  return {
    code: "UNREACHABLE",
    message: `${provider.name} answered with an error (HTTP ${status}). Check the key and try again.`,
  };
}

/** Probe the provider's model list with the correct auth and protocol. */
export async function probeModels(
  target: Target,
  timeoutMs = 12_000,
): Promise<ProbeResult> {
  const { provider } = target;
  const local = provider.local || provider.keyless;
  if (!local && !target.apiKey) {
    return {
      ok: false,
      code: "MISSING_KEY",
      message: `Paste your ${provider.name} API key to connect.`,
      models: [],
    };
  }

  const started = Date.now();
  try {
    const response = await fetch(modelsEndpoint(target), {
      method: "GET",
      headers: {
        ...authHeadersForTarget(target, target.apiKey),
        accept: "application/json",
      },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const latencyMs = Date.now() - started;

    if (response.ok) {
      const models = parseModelList(await readJson(response));
      return {
        ok: true,
        message: models.length
          ? `${provider.name} connected · ${models.length} model${
              models.length === 1 ? "" : "s"
            } found`
          : `${provider.name} connected`,
        httpStatus: response.status,
        latencyMs,
        models,
      };
    }

    // Some OpenAI-compatible servers do not expose /models. A 404/405 means the
    // host is reachable but cannot list models; the chat probe is authoritative.
    if (response.status === 404 || response.status === 405) {
      return {
        ok: true,
        message: `${provider.name} reachable (no model list available)`,
        detail: `HTTP ${response.status}`,
        httpStatus: response.status,
        latencyMs,
        models: [],
      };
    }

    const failure = friendlyHttpFailure(provider, response.status);
    return {
      ok: false,
      ...failure,
      detail: (await readDetail(response)) ?? `HTTP ${response.status}`,
      httpStatus: response.status,
      latencyMs,
      models: [],
    };
  } catch (err) {
    return {
      ok: false,
      code: "UNREACHABLE",
      message: `Could not reach ${provider.name}. Check your internet connection, VPN or proxy.`,
      detail: err instanceof Error ? err.message : String(err),
      latencyMs: Date.now() - started,
      models: [],
    };
  }
}

/** One minimal real chat request, proving the selected model actually answers. */
export async function probeChat(
  target: Target,
  model = target.model || target.provider.model,
  timeoutMs = 20_000,
): Promise<ProbeResult> {
  const { provider } = target;
  const started = Date.now();
  const headers = {
    ...authHeadersForTarget(target, target.apiKey),
    "content-type": "application/json",
    accept: "application/json",
  };
  try {
    const send = (body: Record<string, unknown>) =>
      fetch(chatEndpoint(target, model), {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });

    let response = await send(chatBody(target, model));
    // Some reasoning models reject `max_tokens`/`temperature` (OpenAI's o-series
    // and GPT-5 want `max_completion_tokens`, others reject both). A connection
    // must not be blocked by a probe-only parameter, so retry once with the
    // minimal body when the provider complains about one.
    if (response.status === 400) {
      const text = await response.clone().text().catch(() => "");
      if (/max_tokens|max_completion_tokens|temperature|unsupported|not supported/i.test(text)) {
        const minimal: Record<string, unknown> = {
          model,
          messages: [{ role: "user", content: "ping" }],
          stream: false,
        };
        response = await send(minimal);
      }
    }
    const latencyMs = Date.now() - started;
    if (response.ok) {
      return {
        ok: true,
        message: `${provider.name} answered · ${model}`,
        httpStatus: response.status,
        latencyMs,
        models: [],
      };
    }
    const failure = friendlyHttpFailure(provider, response.status);
    return {
      ok: false,
      ...failure,
      detail: (await readDetail(response)) ?? `HTTP ${response.status}`,
      httpStatus: response.status,
      latencyMs,
      models: [],
    };
  } catch (err) {
    return {
      ok: false,
      code: "UNREACHABLE",
      message: `Could not reach ${provider.name}. Check your internet connection, VPN or proxy.`,
      detail: err instanceof Error ? err.message : String(err),
      latencyMs: Date.now() - started,
      models: [],
    };
  }
}

export interface TestConnectionInput {
  providerId?: string;
  baseUrl?: string;
  apiKey?: string;
  model?: string;
  /** Named custom connection being edited/tested (reuses its stored key). */
  connectionId?: string;
  authStyle?: CustomAuthStyle;
  apiKeyHeader?: string;
  apiKeyPrefix?: string;
  /** Also send one tiny chat request. Default true — a model list alone is not
   * proof that the selected model works. */
  verifyChat?: boolean;
}

/**
 * Test a draft connection without persisting anything. This is what the
 * onboarding/Settings "Test" action and `/api/agent/test` call.
 */
export async function testConnection(input: TestConnectionInput): Promise<ProbeResult> {
  const target = resolveTarget(input);
  if (!target) {
    return {
      ok: false,
      code: "UNKNOWN_PROVIDER",
      message: "Choose a provider and enter a valid base URL.",
      models: [],
    };
  }
  try {
    assertModelAllowed(target.baseUrl, target.provider.id);
  } catch (err) {
    return {
      ok: false,
      code: "UNREACHABLE",
      message: err instanceof Error ? err.message : String(err),
      models: [],
    };
  }

  // A keyed provider must have a key BEFORE any request. Some gateways expose a
  // PUBLIC /models endpoint (OpenRouter does), so an empty key passed the model
  // probe and then failed the chat probe with "Missing Authentication header" —
  // which we wrongly reported as "key rejected". Say "paste a key" instead.
  const needsKey =
    target.provider.authStyle !== "none" &&
    !target.provider.keyless &&
    !target.provider.local;
  if (needsKey && !target.apiKey) {
    return {
      ok: false,
      code: "MISSING_KEY",
      message: `Paste your ${target.provider.name} API key to connect.`,
      models: [],
    };
  }

  const models = await probeModels(target);
  // A missing/rejected key, an unreachable host or a rate limit cannot be fixed
  // by a second request — return the honest failure before the chat probe. The
  // old code only short-circuited on KEY_REJECTED, so an empty key still fired a
  // chat request and surfaced a confusing "key rejected" instead of "paste a
  // key".
  if (!models.ok) {
    // A keyless "Custom" endpoint that answers 401 is not rejecting a key the
    // operator supplied — it is asking for one.
    if (!target.apiKey && models.code === "KEY_REJECTED") {
      return {
        ...models,
        message: "This endpoint needs an API key. Paste a key above and try again.",
      };
    }
    return models;
  }

  const chosen = effectiveModel(target, models.models);
  const verifyChat = input.verifyChat !== false;
  if (!verifyChat) {
    // `ok` means "a real chat request succeeded". A model list alone — especially
    // OpenRouter's PUBLIC list — is never proof of a working key, so a
    // list-only probe is explicitly UNVERIFIED, not connected.
    return {
      ok: false,
      code: "UNVERIFIED",
      message: "Model list reachable, but the chat request was not verified.",
      httpStatus: models.httpStatus,
      latencyMs: models.latencyMs,
      models: models.models,
      model: chosen,
    };
  }

  const chat = await probeChat(target, chosen);
  if (!chat.ok) {
    // The model list can be correct while the selected model is not served.
    // Report the chat failure but keep any models we did discover.
    return { ...chat, models: models.models, model: chosen };
  }
  return {
    ok: true,
    message: models.models.length ? models.message : chat.message,
    latencyMs: (models.latencyMs ?? 0) + (chat.latencyMs ?? 0),
    httpStatus: chat.httpStatus,
    models: models.models,
    model: chosen,
  };
}

export interface ConnectInput {
  providerId?: string;
  baseUrl?: string;
  apiKey?: string;
  model?: string;
  /** Display name for a named custom connection. */
  name?: string;
  /** Edit an existing named custom connection instead of creating one. */
  connectionId?: string;
  authStyle?: CustomAuthStyle;
  apiKeyHeader?: string;
  apiKeyPrefix?: string;
}

export interface ConnectResult extends ProbeResult {
  providerId?: string;
  providerName?: string;
  baseUrl?: string;
  model?: string;
  connectionId?: string;
  connectionName?: string;
}

/**
 * Make a connection atomic:
 *   1. probe the draft (correct auth + a real chat request),
 *   2. only on success store the key in the encrypted vault,
 *   3. persist the active provider/base URL/model.
 *
 * A bad key therefore never replaces a working configuration.
 */
export async function connectProvider(input: ConnectInput): Promise<ConnectResult> {
  const existing = input.connectionId ? await getCustomConnection(input.connectionId) : null;
  const authStyle = input.authStyle ?? existing?.authStyle;
  const apiKeyHeader = input.apiKeyHeader ?? existing?.apiKeyHeader;
  const apiKeyPrefix = input.apiKeyPrefix ?? existing?.apiKeyPrefix;

  // Editing a named connection without re-entering its key: reuse the stored one.
  let apiKey = normalizeApiKey(input.apiKey ?? "");
  if (!apiKey && existing) {
    apiKey = (await storedProviderKey(customConnectionKeyEnvVar(existing.id))) ?? "";
  }

  const target = resolveTarget({
    providerId: input.providerId ?? (existing ? "custom" : undefined),
    baseUrl: input.baseUrl ?? existing?.baseUrl,
    apiKey,
    model: input.model ?? existing?.model,
    authStyle,
    apiKeyHeader,
    apiKeyPrefix,
  });
  if (!target) {
    return {
      ok: false,
      code: "UNKNOWN_PROVIDER",
      message: "Choose a provider and enter a valid base URL.",
      models: [],
    };
  }

  const probe = await testConnection({
    providerId: target.provider.id,
    baseUrl: target.baseUrl,
    apiKey: target.apiKey,
    model: target.model,
    ...(authStyle ? { authStyle } : {}),
    ...(apiKeyHeader ? { apiKeyHeader } : {}),
    ...(apiKeyPrefix ? { apiKeyPrefix } : {}),
    verifyChat: true,
  });
  if (!probe.ok) {
    return {
      ...probe,
      providerId: target.provider.id,
      providerName: target.provider.name,
      baseUrl: target.baseUrl,
      model: target.model,
    };
  }

  // The probe reports the model it actually used: an explicit choice, else the
  // first model the provider listed, else the declared default. This means the
  // connect flow never fails just because a hard-coded default was retired.
  const chosenModel = probe.model || target.model || probe.models[0] || target.provider.model;

  // Persist the models the probe just found. Without this the composer (and the
  // picker in Settings) only knew the catalog default until someone clicked
  // "Discover" — so a freshly connected API appeared to have one model, or only
  // the local Ollama list stayed visible. Best effort: a snapshot failure must
  // never fail a verified connection.
  const rememberModels = async () => {
    const ids = probe.models.filter((m): m is string => typeof m === "string" && m.length > 0);
    if (ids.length === 0) return;
    try {
      const { recordDiscoveredModels } = await import("./discovered-models");
      // Everything the provider listed in this probe is genuinely served.
      await recordDiscoveredModels(target.provider.id, ids, ids);
    } catch {
      // The in-memory runtime registry still knows them for this session.
    }
  };

  try {
    if (target.provider.id === "custom") {
      // A named connection is the source of truth for endpoint + auth routing.
      const saved = await saveCustomConnection({
        id: existing?.id,
        name: input.name ?? existing?.name,
        baseUrl: target.baseUrl,
        model: chosenModel,
        authStyle: authStyle ?? "bearer",
        apiKeyHeader,
        apiKeyPrefix,
      });
      const envVar = customConnectionKeyEnvVar(saved.id);
      if (target.apiKey) await saveProviderKey(envVar, target.apiKey);
      await setLLMConfig({
        provider: "custom",
        connectionId: saved.id,
        baseUrl: target.baseUrl,
        model: chosenModel,
        apiKey: "",
      });
      await rememberModels();
      return {
        ...probe,
        providerId: "custom",
        providerName: saved.name,
        baseUrl: target.baseUrl,
        model: chosenModel,
        models: probe.models,
        connectionId: saved.id,
        connectionName: saved.name,
      };
    }

    // `keyless` means "a key is OPTIONAL", not "a key must be discarded". A
    // custom OpenAI-compatible gateway is keyless in the descriptor (it may be
    // local) but usually needs the key the operator just pasted. `authStyle:
    // none` is the real "this endpoint ignores credentials" signal.
    if (target.apiKey && target.provider.authStyle !== "none") {
      // Store under the SAME env var that getLLMConfig will read back, including
      // a named provider with an edited base URL.
      const envVar =
        llmKeyEnvVar(target.baseUrl, target.provider.id) ??
        target.provider.envVar ??
        "CUSTOM_LLM_API_KEY";
      await saveProviderKey(envVar, target.apiKey);
    }

    await setLLMConfig({
      provider: target.provider.id as LLMProviderName,
      baseUrl: target.baseUrl,
      model: chosenModel,
      // The key lives in the vault, never plaintext in llm_config.
      apiKey: "",
    });
    await rememberModels();
  } catch (err) {
    return {
      ok: false,
      code: "INVALID_INPUT",
      message: "The provider answered, but Eclipse could not save the connection.",
      detail: err instanceof Error ? err.message : String(err),
      models: probe.models,
      model: chosenModel,
      providerId: target.provider.id,
      providerName: target.provider.name,
      baseUrl: target.baseUrl,
    };
  }

  return {
    ...probe,
    providerId: target.provider.id,
    providerName: target.provider.name,
    baseUrl: target.baseUrl,
    model: chosenModel,
    models: probe.models,
  };
}

/** Persist only the active model for an already-connected provider. */
export async function setActiveModel(input: {
  providerId: string;
  baseUrl?: string;
  model: string;
  connectionId?: string;
}): Promise<{ ok: boolean; message: string }> {
  const provider = getProvider(input.providerId);
  if (!provider) return { ok: false, message: "Unknown provider." };
  const model = input.model.trim();
  if (!model) return { ok: false, message: "Choose a model first." };
  const baseUrl = normalizeBaseUrl(input.baseUrl || provider.baseUrl) ?? provider.baseUrl;
  await setLLMConfig({
    provider: provider.id as LLMProviderName,
    baseUrl,
    model,
    ...(input.connectionId ? { connectionId: input.connectionId } : {}),
    apiKey: "",
  });
  return { ok: true, message: `Using ${model}` };
}
