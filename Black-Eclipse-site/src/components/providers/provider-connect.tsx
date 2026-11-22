"use client";

// ── ProviderConnect — the one connect experience ───────────────────────────
// Used by onboarding and Settings › AI. Designed for someone who has never
// configured an API: paste a key, the provider is detected, the key is tested
// with the provider's REAL auth method, models are discovered, and the working
// connection is saved in one action.
//
// The old flow had three hidden ordering rules (choose provider → save key →
// choose provider again). Getting any of them wrong left the UI showing one
// provider while the runtime used another. This component removes the ordering
// problem entirely: there is only "Connect".

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  CircleDashed,
  ExternalLink,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Plug,
  Server,
  ShieldCheck,
} from "@/lib/icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { ProviderLogo } from "@/components/providers/provider-logo";
import {
  PROVIDERS,
  POPULAR_PROVIDERS,
  detectProviderFromKey,
  getProvider,
  type ProviderDescriptor,
} from "@/lib/providers/registry";
import {
  CUSTOM_AUTH_STYLES,
  type CustomAuthStyle,
} from "@/lib/agent/custom-auth";
import { notifyLlmConfigChanged } from "@/lib/agent/llm-config-events";

export interface ProviderConnectResult {
  providerId: string;
  providerName?: string;
  baseUrl: string;
  model: string;
  models: string[];
  message: string;
  connectionId?: string;
  connectionName?: string;
}

interface Props {
  /** Pre-select a provider (used when a card's Connect button is clicked). */
  initialProviderId?: string;
  initialBaseUrl?: string;
  initialModel?: string;
  /** Edit an existing named custom connection. */
  initialConnectionId?: string;
  initialConnectionName?: string;
  initialAuthStyle?: CustomAuthStyle;
  initialApiKeyHeader?: string;
  initialApiKeyPrefix?: string;
  /** Called once a connection succeeds. */
  onConnected?: (result: ProviderConnectResult) => void;
  /** Called when the user dismisses the panel. */
  onCancel?: () => void;
  /** Onboarding shows a "Set up later" escape hatch. */
  allowSkip?: boolean;
  onSkip?: () => void;
  compact?: boolean;
}

const TIER_LABEL: Record<ProviderDescriptor["tier"], string> = {
  free: "Free",
  "free-tier": "Free tier",
  paid: "Paid",
};

type Phase = "idle" | "connecting" | "testing" | "done";

interface ConnectError {
  message: string;
  detail?: string;
  code?: string;
}

function ProviderOption({
  provider,
  active,
  onSelect,
}: {
  provider: ProviderDescriptor;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onSelect}
      className={cn(
        "flex items-center gap-2.5 rounded-xl border px-3 py-2.5 text-left transition-colors cursor-pointer",
        active
          ? "border-primary/50 bg-primary/[0.08]"
          : "border-white/10 bg-white/[0.02] hover:border-white/20 hover:bg-white/[0.04]",
      )}
    >
      <span
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border",
          active
            ? "border-primary/40 bg-primary/15 text-primary"
            : "border-white/10 bg-white/[0.04] text-foreground/80",
        )}
      >
        <ProviderLogo provider={provider.id} size={16} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[12.5px] font-semibold text-foreground">
            {provider.name}
          </span>
          {provider.local && (
            <span className="rounded-full border border-emerald-500/25 bg-emerald-500/10 px-1.5 py-0.5 text-[8px] font-bold text-emerald-300">
              LOCAL
            </span>
          )}
        </span>
        <span className="mt-0.5 block truncate text-[10.5px] text-muted-foreground">
          {TIER_LABEL[provider.tier]}
          {provider.id === "custom"
            ? " · key optional"
            : provider.keyless
              ? " · no key"
              : ""}
        </span>
      </span>
      {active && <Check size={13} className="shrink-0 text-primary" />}
    </button>
  );
}

export function ProviderConnect({
  initialProviderId,
  initialBaseUrl,
  initialModel,
  initialConnectionId,
  initialConnectionName,
  initialAuthStyle,
  initialApiKeyHeader,
  initialApiKeyPrefix,
  onConnected,
  onCancel,
  allowSkip,
  onSkip,
  compact,
}: Props) {
  const initialProvider = getProvider(initialProviderId ?? "");
  const [providerId, setProviderId] = useState(initialProvider?.id ?? "");
  const [apiKey, setApiKey] = useState("");
  const [reveal, setReveal] = useState(false);
  const [baseUrl, setBaseUrl] = useState(initialBaseUrl ?? initialProvider?.baseUrl ?? "");
  const [model, setModel] = useState(initialModel ?? "");
  const [connectionName, setConnectionName] = useState(initialConnectionName ?? "");
  const [authStyle, setAuthStyle] = useState<CustomAuthStyle>(initialAuthStyle ?? "bearer");
  const [apiKeyHeader, setApiKeyHeader] = useState(initialApiKeyHeader ?? "");
  const [apiKeyPrefix, setApiKeyPrefix] = useState(initialApiKeyPrefix ?? "");
  const [showMore, setShowMore] = useState(false);
  const [advanced, setAdvanced] = useState(Boolean(initialBaseUrl || initialModel));
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<ConnectError | null>(null);
  const [testNote, setTestNote] = useState<{ ok: boolean; message: string } | null>(null);
  const [result, setResult] = useState<ProviderConnectResult | null>(null);
  const [switchingModel, setSwitchingModel] = useState(false);

  const provider = getProvider(providerId);
  const detected = useMemo(() => detectProviderFromKey(apiKey), [apiKey]);
  const busy = phase === "connecting" || phase === "testing";

  // Auto-select a provider when the pasted key is unambiguous. Never override a
  // provider the user picked deliberately unless we are on "custom" (where the
  // key is the only signal we have).
  useEffect(() => {
    if (!detected) return;
    if (!providerId || providerId === "custom") {
      setProviderId(detected.id);
    }
  }, [detected, providerId]);

  // When the provider changes, reset the endpoint/model to that provider's
  // defaults unless the user explicitly edited them in Advanced.
  const lastProvider = useRef(providerId);
  useEffect(() => {
    if (lastProvider.current === providerId) return;
    lastProvider.current = providerId;
    const next = getProvider(providerId);
    // A custom endpoint has no sensible default URL; the operator must supply it.
    setBaseUrl(next?.id === "custom" ? "" : next?.baseUrl ?? "");
    setModel("");
    setAdvanced(next?.id === "custom");
    setResult(null);
    setError(null);
    setTestNote(null);
    setPhase("idle");
  }, [providerId]);

  const selectProvider = (next: ProviderDescriptor) => {
    setProviderId(next.id);
    // Custom endpoints get a dedicated Base URL field above; Advanced is only
    // for the model override.
    setAdvanced(false);
  };

  const isCustom = providerId === "custom";

  const payload = () => ({
    providerId,
    baseUrl: baseUrl.trim() || undefined,
    model: model.trim() || undefined,
    apiKey: apiKey.trim() || undefined,
    ...(isCustom
      ? {
          name: connectionName.trim() || undefined,
          authStyle,
          apiKeyHeader: authStyle === "header" ? apiKeyHeader.trim() || undefined : undefined,
          apiKeyPrefix: authStyle === "header" ? apiKeyPrefix : undefined,
          connectionId: initialConnectionId,
        }
      : {}),
  });

  const connect = async () => {
    if (!providerId) {
      setError({
        message: detected
          ? "Pick the provider above to finish."
          : "Choose a provider below, or paste a key we can recognise.",
      });
      return;
    }
    if (!provider?.keyless && !apiKey.trim()) {
      setError({ message: `Paste your ${provider?.name ?? "provider"} API key to connect.` });
      return;
    }
    if (isCustom) {
      if (!baseUrl.trim()) {
        setError({ message: "Paste the gateway's base URL (for example https://llm.company.com/v1)." });
        return;
      }
      if (authStyle === "header" && !apiKeyHeader.trim()) {
        setError({ message: "Name the header that carries your key (for example x-api-key)." });
        return;
      }
    }
    setPhase("connecting");
    setError(null);
    setTestNote(null);
    try {
      const res = await fetch("/api/llm/connect", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload()),
      });
      const data = (await res.json().catch(() => ({}))) as Partial<ProviderConnectResult> & {
        ok?: boolean;
        message?: string;
        detail?: string;
        code?: string;
        models?: string[];
      };
      if (!res.ok || !data.ok) {
        setError({
          message: data.message || "The connection could not be verified.",
          detail: data.detail,
          code: data.code,
        });
        setPhase("idle");
        return;
      }
      const next: ProviderConnectResult = {
        providerId: data.providerId ?? providerId,
        providerName: data.providerName ?? provider?.name,
        baseUrl: data.baseUrl ?? baseUrl,
        model: data.model ?? model,
        models: data.models ?? [],
        message: data.message ?? "Connected",
        connectionId: data.connectionId,
        connectionName: data.connectionName,
      };
      setResult(next);
      setPhase("done");
      // Tell every mounted composer (the chat screen and the floating dock are
      // separate instances) that the saved config changed, and let it replace a
      // stale local/Ollama pick with this newly connected provider.
      notifyLlmConfigChanged({ resetChoice: true });
      onConnected?.(next);
    } catch (err) {
      setError({
        message: "Could not reach the local server. Nothing was changed.",
        detail: err instanceof Error ? err.message : String(err),
      });
      setPhase("idle");
    }
  };

  const test = async () => {
    if (!providerId) {
      setError({ message: "Choose a provider first." });
      return;
    }
    // Never send an empty key to a keyed provider: OpenRouter's public /models
    // endpoint would pass, then the chat probe would return a confusing
    // "Missing Authentication header".
    if (provider && !provider.keyless && !apiKey.trim()) {
      setError({ message: `Paste your ${provider.name} API key before testing.` });
      return;
    }
    setPhase("testing");
    setError(null);
    setTestNote(null);
    try {
      const res = await fetch("/api/llm/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload()),
      });
      const data = (await res.json().catch(() => ({}))) as {
        ok?: boolean;
        message?: string;
        detail?: string;
        code?: string;
      };
      if (data.ok) {
        setTestNote({ ok: true, message: data.message || "Connection works." });
      } else {
        setError({
          message: data.message || "The test failed.",
          detail: data.detail,
          code: data.code,
        });
      }
    } catch {
      setError({ message: "Could not reach the local server to run the test." });
    } finally {
      setPhase("idle");
    }
  };

  const changeModel = async (nextModel: string) => {
    if (!nextModel || nextModel === result?.model) return;
    setSwitchingModel(true);
    try {
      const res = await fetch("/api/llm/active", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          providerId: result?.providerId ?? providerId,
          baseUrl: result?.baseUrl ?? baseUrl,
          model: nextModel,
          connectionId: result?.connectionId ?? initialConnectionId,
        }),
      });
      const data = (await res.json().catch(() => ({}))) as { ok?: boolean; message?: string };
      if (res.ok && data.ok) {
        setResult((current) => (current ? { ...current, model: nextModel } : current));
        // The active model changed; refresh every composer's model label without
        // discarding their per-chat provider choice.
        notifyLlmConfigChanged();
      } else {
        setError({ message: data.message || "Could not switch model." });
      }
    } finally {
      setSwitchingModel(false);
    }
  };

  // ── Success view ───────────────────────────────────────────────────────────
  if (result && phase === "done") {
    const active = getProvider(result.providerId);
    const allModels = result.models.length > 0 ? result.models : result.model ? [result.model] : [];
    // A provider's full list also contains embeddings/audio/image models; the
    // picker is for chat, so hide those unless the active model is one of them.
    const nonChat = /(embed|whisper|tts|dall-e|moderation|image|audio|rerank|veo|imagen|sora)/i;
    const chatModels = allModels.filter((m) => !nonChat.test(m));
    const models =
      result.model && !chatModels.includes(result.model)
        ? [result.model, ...chatModels]
        : chatModels;
    return (
      <div className="space-y-4">
        <div className="rounded-2xl border border-emerald-500/25 bg-emerald-500/[0.06] p-4">
          <div className="flex items-start gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-emerald-500/30 bg-emerald-500/15 text-emerald-300">
              <Check size={16} weight="bold" />
            </span>
            <div className="min-w-0">
              <p className="text-[13px] font-semibold text-foreground">
                {result.providerName || active?.name || "Provider"} connected
              </p>
              <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
                {result.message}
                {result.models.length > 0 ? ` · ${result.models.length} available` : ""}
              </p>
              <p className="mt-1 font-mono text-[10.5px] text-emerald-300/90">
                {result.model}
              </p>
            </div>
          </div>
        </div>

        {models.length > 1 && (
          <div className="space-y-1.5">
            <Label className="text-xs font-medium text-muted-foreground">
              Active model
              {switchingModel && <Loader2 size={11} className="ml-1.5 inline animate-spin" />}
            </Label>
            <select
              value={result.model}
              onChange={(e) => void changeModel(e.target.value)}
              disabled={switchingModel}
              className="w-full rounded-lg border border-white/12 bg-transparent px-3 py-2 font-mono text-[12px] text-foreground focus:border-primary/40 focus:outline-none disabled:opacity-60"
            >
              {models.map((m) => (
                <option key={m} value={m} className="bg-background text-foreground">
                  {m}
                </option>
              ))}
            </select>
            <p className="text-[10.5px] text-muted-foreground">
              Change this any time in Settings → AI.
            </p>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            className="cursor-pointer rounded-lg"
            onClick={() => {
              onConnected?.(result);
              onCancel?.();
            }}
          >
            <Check size={13} className="mr-1.5" /> Use this connection
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="cursor-pointer rounded-lg"
            onClick={() => {
              setResult(null);
              setPhase("idle");
              setApiKey("");
            }}
          >
            Connect a different provider
          </Button>
        </div>
      </div>
    );
  }

  const visibleProviders = showMore ? PROVIDERS : POPULAR_PROVIDERS;
  const extraCount = PROVIDERS.length - POPULAR_PROVIDERS.length;

  return (
    <div className="space-y-4">
      <div>
        <p className="text-[13px] font-semibold text-foreground">Connect an AI provider</p>
        <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
          Paste an API key — Eclipse detects the provider for you. Or run a free local model.
        </p>
      </div>

      {/* Key first: this is the one thing most people have. Custom endpoints may
          need a key even though the provider itself is marked keyless. */}
      {(!provider || !provider.keyless || provider.id === "custom") && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="provider-connect-key" className="text-xs font-medium text-muted-foreground">
              {provider
                ? provider.id === "custom"
                  ? "API key (optional)"
                  : `${provider.name} API key`
                : "API key"}
            </Label>
            {provider?.docsUrl && (
              <a
                href={provider.docsUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[10.5px] font-medium text-primary/90 hover:text-primary"
              >
                {provider.keyless ? "Docs" : "Get a key"} <ExternalLink size={10} />
              </a>
            )}
          </div>
          <div className="relative">
            <KeyRound className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="provider-connect-key"
              type={reveal ? "text" : "password"}
              value={apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
                setError(null);
                setTestNote(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") void connect();
              }}
              placeholder={
                initialConnectionId
                  ? "Leave blank to keep the current key"
                  : provider
                    ? "Paste your key…"
                    : "Paste any API key — we'll detect the provider"
              }
              className="h-10 pl-9 pr-10 font-mono text-[12px]"
              autoComplete="off"
              spellCheck={false}
            />
            <button
              type="button"
              onClick={() => setReveal((v) => !v)}
              aria-label={reveal ? "Hide key" : "Show key"}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1.5 text-muted-foreground hover:text-foreground cursor-pointer"
            >
              {reveal ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          {detected && (
            <p className="flex items-center gap-1.5 text-[10.5px] text-emerald-300/90">
              <Check size={11} />
              {(() => {
                const article = /^(anthropic|openai|xai)/i.test(detected.name) ? "an" : "a";
                return detected.id === providerId
                  ? `Looks like ${article} ${detected.name} key — selected below.`
                  : `This looks like ${article} ${detected.name} key — select ${detected.name} below.`;
              })()}
            </p>
          )}
          {!detected && apiKey.trim().length > 8 && !providerId && (
            <p className="text-[10.5px] text-muted-foreground">
              We could not recognise this key. Choose your provider below and press Connect.
            </p>
          )}
        </div>
      )}

      {isCustom && (
        <div className="space-y-3 rounded-xl border border-white/10 bg-white/[0.02] p-3.5">
          <div className="space-y-1.5">
            <Label htmlFor="custom-connection-name" className="text-[10.5px] text-muted-foreground">
              Connection name (optional)
            </Label>
            <Input
              id="custom-connection-name"
              value={connectionName}
              onChange={(e) => setConnectionName(e.target.value)}
              placeholder="e.g. Company gateway, Local vLLM"
              className="h-8 text-[11.5px]"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="custom-connection-url" className="text-[10.5px] text-muted-foreground">
              Base URL
            </Label>
            <Input
              id="custom-connection-url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://llm.company.com/v1"
              className="h-8 font-mono text-[11.5px]"
              autoComplete="off"
              spellCheck={false}
            />
            <p className="text-[10px] leading-relaxed text-muted-foreground">
              The OpenAI-compatible root, usually ending in <span className="font-mono">/v1</span>.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="custom-auth-style" className="text-[10.5px] text-muted-foreground">
              How the key is sent
            </Label>
            <select
              id="custom-auth-style"
              value={authStyle}
              onChange={(e) => setAuthStyle(e.target.value as CustomAuthStyle)}
              className="w-full rounded-lg border border-white/12 bg-transparent px-3 py-2 text-[11.5px] text-foreground focus:border-primary/40 focus:outline-none"
            >
              {CUSTOM_AUTH_STYLES.map((style) => (
                <option key={style.value} value={style.value} className="bg-background text-foreground">
                  {style.label}
                </option>
              ))}
            </select>
            <p className="text-[10px] text-muted-foreground">
              {CUSTOM_AUTH_STYLES.find((s) => s.value === authStyle)?.hint}
            </p>
          </div>

          {authStyle === "header" && (
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="custom-auth-header" className="text-[10.5px] text-muted-foreground">
                  Header name
                </Label>
                <Input
                  id="custom-auth-header"
                  value={apiKeyHeader}
                  onChange={(e) => setApiKeyHeader(e.target.value)}
                  placeholder="x-api-key"
                  className="h-8 font-mono text-[11.5px]"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="custom-auth-prefix" className="text-[10.5px] text-muted-foreground">
                  Value prefix (optional)
                </Label>
                <Input
                  id="custom-auth-prefix"
                  value={apiKeyPrefix}
                  onChange={(e) => setApiKeyPrefix(e.target.value)}
                  placeholder="Token "
                  className="h-8 font-mono text-[11.5px]"
                />
              </div>
            </div>
          )}
        </div>
      )}

      {provider?.keyless && provider.id !== "custom" && (
        <div className="flex items-start gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/[0.04] p-3">
          <ShieldCheck size={14} className="mt-0.5 shrink-0 text-emerald-300" />
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            {provider.local
              ? "This runs on your computer. No key and no data leaves your machine. Make sure the app is running, then press Connect."
              : "This provider needs no key. Press Connect to verify the endpoint."}
          </p>
        </div>
      )}

      {/* Provider picker */}
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <Label className="text-xs font-medium text-muted-foreground">
            {detected ? "Provider" : "Choose a provider"}
          </Label>
          {extraCount > 0 && (
            <button
              type="button"
              onClick={() => setShowMore((v) => !v)}
              className="inline-flex items-center gap-1 text-[10.5px] font-semibold text-primary/90 hover:text-primary cursor-pointer"
            >
              <ChevronDown size={12} className={cn("transition-transform", showMore && "rotate-180")} />
              {showMore ? "Show popular only" : `More providers (${extraCount})`}
            </button>
          )}
        </div>
        <div className={cn("grid gap-2", compact ? "grid-cols-1" : "sm:grid-cols-2")}>
          {visibleProviders.map((p) => (
            <ProviderOption
              key={p.id}
              provider={p}
              active={p.id === providerId}
              onSelect={() => selectProvider(p)}
            />
          ))}
        </div>
        {provider && (
          <p className="text-[10.5px] leading-relaxed text-muted-foreground">{provider.description}</p>
        )}
      </div>

      {/* Advanced: almost nobody needs this. */}
      <button
        type="button"
        onClick={() => setAdvanced((v) => !v)}
        className="inline-flex items-center gap-1.5 text-[10.5px] font-semibold text-muted-foreground hover:text-foreground cursor-pointer"
      >
        <Server size={11} />
        {advanced ? "Hide advanced settings" : "Advanced settings (base URL, model)"}
      </button>
      {advanced && (
        <div className="grid gap-3 rounded-xl border border-white/10 bg-white/[0.02] p-3 sm:grid-cols-2">
          {providerId !== "custom" && (
            <div className="space-y-1.5 sm:col-span-2">
              <Label className="text-[10.5px] text-muted-foreground">Base URL (optional)</Label>
              <Input
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder={provider?.baseUrl ?? "https://api.example.com/v1"}
                className="h-8 font-mono text-[11.5px]"
              />
            </div>
          )}
          <div className="space-y-1.5 sm:col-span-2">
            <Label className="text-[10.5px] text-muted-foreground">Model (optional — auto-detected)</Label>
            <Input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={provider?.model ?? "model-id"}
              className="h-8 font-mono text-[11.5px]"
            />
          </div>
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-rose-500/25 bg-rose-500/[0.06] p-3">
          <p className="flex items-start gap-2 text-[11.5px] font-semibold leading-relaxed text-rose-200">
            <AlertCircle size={13} className="mt-0.5 shrink-0" />
            {error.message}
          </p>
          {error.detail && (
            <p className="mt-1 break-words font-mono text-[10px] text-rose-200/60">{error.detail}</p>
          )}
          {provider?.docsUrl && error.code === "KEY_REJECTED" && (
            <a
              href={provider.docsUrl}
              target="_blank"
              rel="noreferrer"
              className="mt-1.5 inline-flex items-center gap-1 text-[10.5px] font-semibold text-rose-100 underline-offset-2 hover:underline"
            >
              Open {provider.name} key page <ExternalLink size={10} />
            </a>
          )}
        </div>
      )}
      {testNote && (
        <p className="flex items-center gap-1.5 text-[11px] text-emerald-300">
          <Check size={11} /> {testNote.message}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          className="cursor-pointer rounded-lg"
          disabled={busy || !providerId || Boolean(provider && !provider.keyless && !apiKey.trim())}
          onClick={() => void connect()}
        >
          {phase === "connecting" ? (
            <Loader2 size={13} className="mr-1.5 animate-spin" />
          ) : (
            <Plug size={13} className="mr-1.5" />
          )}
          {phase === "connecting" ? "Testing & connecting…" : "Connect"}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="cursor-pointer rounded-lg"
          disabled={busy || !providerId || Boolean(provider && !provider.keyless && !apiKey.trim())}
          onClick={() => void test()}
        >
          {phase === "testing" ? <Loader2 size={13} className="mr-1.5 animate-spin" /> : null}
          {phase === "testing" ? "Testing…" : "Test only"}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" size="sm" className="cursor-pointer rounded-lg" onClick={onCancel}>
            Cancel
          </Button>
        )}
        {allowSkip && onSkip && (
          <button
            type="button"
            onClick={onSkip}
            className="ml-auto text-[11px] font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline cursor-pointer"
          >
            Set up later
          </button>
        )}
      </div>

      {provider && !provider.keyless && !apiKey.trim() && (
        <p className="text-[10.5px] leading-relaxed text-amber-300/90">
          Paste the API key to enable Connect and Test. A model list alone never
          counts as connected.
        </p>
      )}

      <p className="flex items-start gap-1.5 text-[10px] leading-relaxed text-muted-foreground/80">
        <CircleDashed size={10} className="mt-0.5 shrink-0" />
        The key is stored encrypted on this machine and only sent to the provider you choose.
        “Test only” makes one tiny request and saves nothing.
      </p>
    </div>
  );
}

export default ProviderConnect;
