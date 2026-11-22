"use client";

// ── AI provider settings ────────────────────────────────────────────────────
// The active connection is the most important thing on this screen, so it is
// stated at the top in plain language, followed by one obvious action:
// "Connect a provider". The shared `ProviderConnect` panel (also used by
// onboarding) does paste-detection, the real test and model discovery in one
// atomic request, which removes the old choose-provider → save-key → choose-
// provider-again ordering trap.
//
// Advanced controls (base URL, sampling, context window, coding model) live in
// a collapsed section below. Non-technical operators never need to open it.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  Brain,
  Check,
  ChevronDown,
  CircleDashed,
  Cpu,
  KeyRound,
  Loader2,
  Plug,
  Ruler,
  Server,
  SlidersHorizontal,
  Trash2,
} from "@/lib/icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { IconBadge } from "@/components/layout/page-header";
import { ProviderLogo } from "@/components/providers/provider-logo";
import { ProviderConnect, type ProviderConnectResult } from "@/components/providers/provider-connect";
import { isLocalBaseUrl, presetForBaseUrl, LLM_PRESETS, type LLMPreset } from "@/lib/agent/presets";
import { PROVIDERS, getProvider } from "@/lib/providers/registry";
import { CUSTOM_AUTH_STYLES, type CustomAuthStyle } from "@/lib/agent/custom-auth";
import { customConnectionKeyEnvVar } from "@/lib/agent/custom-key";
import { notifyLlmConfigChanged } from "@/lib/agent/llm-config-events";
import { REASONING_OPTIONS, type ReasoningLevel } from "@/lib/agent/reasoning";
import { loadModelCatalog, type ModelCatalog } from "@/lib/agent/model-catalog";
import { ModelPicker } from "./model-picker";
import type { LLMConfig } from "./settings";

interface KeyStatus {
  envVar: string;
  configured: boolean;
  publicValue: boolean;
  fromEnv: boolean;
  stored: boolean;
  testable: boolean;
  hint?: string;
}

function useKeyStatuses() {
  const [map, setMap] = useState<Record<string, KeyStatus>>({});
  const reload = useCallback(() => {
    fetch("/api/provider-keys")
      .then((r) => r.json())
      .then((data: { keys?: KeyStatus[] }) => {
        if (Array.isArray(data.keys)) setMap(Object.fromEntries(data.keys.map((k) => [k.envVar, k])));
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);
  return { keys: map, reload };
}

interface CustomConnectionRow {
  id: string;
  name: string;
  baseUrl: string;
  model: string;
  authStyle: CustomAuthStyle;
  apiKeyHeader?: string;
  apiKeyPrefix?: string;
  keyEnvVar: string;
  hasKey: boolean;
}

function useCustomConnections() {
  const [connections, setConnections] = useState<CustomConnectionRow[]>([]);
  const reload = useCallback(() => {
    fetch("/api/llm/connections")
      .then((r) => r.json())
      .then((data: { connections?: CustomConnectionRow[] }) => {
        if (Array.isArray(data.connections)) setConnections(data.connections);
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);
  return { connections, reload };
}

type ProviderState = "active" | "connected" | "needs-key";

function providerState(
  preset: LLMPreset,
  keys: Record<string, KeyStatus>,
  activeId: string | undefined,
): ProviderState {
  if (preset.id === activeId) return "active";
  if (preset.envVar && keys[preset.envVar]?.configured) return "connected";
  return "needs-key";
}

function StatusChip({ state }: { state: ProviderState }) {
  const map: Record<ProviderState, string> = {
    active: "border-emerald-500/25 bg-emerald-500/10 text-emerald-300",
    connected: "border-white/12 bg-white/[0.04] text-emerald-300/90",
    "needs-key": "border-white/10 text-muted-foreground",
  };
  const label: Record<ProviderState, string> = {
    active: "Active",
    connected: "Connected",
    "needs-key": "Not connected",
  };
  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-bold", map[state])}>
      {state === "needs-key" ? <CircleDashed size={9} /> : <Check size={9} />}
      {label[state]}
    </span>
  );
}

/** The active connection, stated plainly, with a real test. */
function ActiveConnection({
  llm,
  keys,
  connectionName,
  busy,
  modelCatalog,
  onModelChange,
  onOpenConnect,
  onReplaceKey,
  onRemoveKey,
}: {
  llm: LLMConfig;
  keys: Record<string, KeyStatus>;
  connectionName?: string;
  busy: boolean;
  modelCatalog: ModelCatalog;
  onModelChange: (model: string) => void;
  onOpenConnect: () => void;
  onReplaceKey: () => void;
  onRemoveKey: () => void;
}) {
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; message: string; latencyMs?: number } | null>(null);

  // A saved base URL is not an active provider. When the config is `mock` the
  // app has no provider, even if the leftover URL still looks like Ollama.
  const preset =
    llm.provider === "mock"
      ? undefined
      : presetForBaseUrl(llm.baseUrl) ?? getProvider(llm.provider);
  const envVar = llm.connectionId
    ? customConnectionKeyEnvVar(llm.connectionId)
    : preset?.envVar ?? (llm.provider === "custom" ? "CUSTOM_LLM_API_KEY" : undefined);
  const hasKey = Boolean(envVar && keys[envVar]?.configured) || Boolean(llm.hasApiKey);
  const local = isLocalBaseUrl(llm.baseUrl);
  // A keyless endpoint (Ollama/LM Studio) is valid without a key. A NAMED custom
  // connection is assumed keyed unless its URL is local; its own row reports
  // "Needs a key" when the vault entry is missing.
  const keyless = Boolean(preset && getProvider(preset.id)?.keyless) && !llm.connectionId;
  const configured = llm.provider !== "mock" && (hasKey || local || keyless);
  // Quick model switch: the live catalog has all served ids (OpenCode Go, for
  // example, lists 31). Keep the saved id even if the catalog is stale.
  const catalogProvider = preset ? modelCatalog[preset.id] : modelCatalog[llm.provider];
  const catalogModels = catalogProvider?.models ?? [];
  const modelOptions =
    llm.model && !catalogModels.includes(llm.model)
      ? [llm.model, ...catalogModels]
      : catalogModels;

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      const res = await fetch("/api/agent/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          providerId: preset?.id ?? llm.provider,
          baseUrl: llm.baseUrl,
          model: llm.model,
        }),
      });
      const data = (await res.json()) as {
        ok?: boolean;
        message?: string;
        error?: string;
        latencyMs?: number;
      };
      setTest({ ok: Boolean(data.ok), message: data.message || data.error || "No response", latencyMs: data.latencyMs });
    } catch {
      setTest({ ok: false, message: "Could not run the test." });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-4 rounded-2xl border border-white/10 glass-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <IconBadge icon={Cpu} size="sm" />
          <div className="min-w-0">
            <p className="text-[10px] font-bold uppercase tracking-[0.15em] text-primary">
              Active connection
            </p>
            <p className="flex items-center gap-2 truncate text-sm font-semibold text-foreground">
              <ProviderLogo provider={preset?.id ?? llm.provider} size={14} />
              {connectionName ??
                preset?.name ??
                (llm.provider === "mock" ? "No provider connected" : llm.provider)}
            </p>
          </div>
        </div>
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-bold",
            configured
              ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
              : "border-amber-500/25 bg-amber-500/10 text-amber-200",
          )}
        >
          {configured ? <Check size={10} /> : <CircleDashed size={10} />}
          {configured ? (local ? "Local endpoint" : "Connected") : "Needs setup"}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="min-w-0 rounded-xl border border-white/10 bg-white/[0.02] px-3 py-2">
          <p className="text-[9.5px] uppercase tracking-wider text-muted-foreground">
            Model {modelOptions.length > 0 ? `· ${modelOptions.length} available` : ""}
          </p>
          {modelOptions.length > 0 ? (
            <select
              value={llm.model}
              onChange={(e) => onModelChange(e.target.value)}
              disabled={busy}
              title="Change the active model"
              className="mt-0.5 w-full cursor-pointer truncate rounded-md border border-white/10 bg-transparent py-1 font-mono text-[12px] font-semibold text-foreground focus:border-primary/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
            >
              {modelOptions.map((m) => (
                <option key={m} value={m} className="bg-background text-foreground">
                  {m}
                </option>
              ))}
            </select>
          ) : (
            <p className="truncate font-mono text-[12px] font-semibold text-foreground" title={llm.model}>
              {llm.model || "—"}
            </p>
          )}
        </div>
        <div className="min-w-0 rounded-xl border border-white/10 bg-white/[0.02] px-3 py-2">
          <p className="text-[9.5px] uppercase tracking-wider text-muted-foreground">Endpoint</p>
          <p className="truncate font-mono text-[11px] text-muted-foreground" title={llm.baseUrl}>
            {llm.baseUrl || "—"}
          </p>
        </div>
      </div>

      {!configured && (
        <p className="text-[11.5px] leading-relaxed text-muted-foreground">
          Chat needs a model. Press{" "}
          <span className="font-semibold text-foreground">Connect a provider</span>{" "}
          {local
            ? "and make sure your local server is running — Eclipse checks it before saving."
            : "and paste a key — Eclipse tests it before saving."}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          className="cursor-pointer rounded-lg"
          onClick={onOpenConnect}
        >
          <Plug size={13} className="mr-1.5" /> {configured ? "Change provider" : "Connect a provider"}
        </Button>
        {configured && !local && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="cursor-pointer rounded-lg"
            onClick={onReplaceKey}
          >
            <KeyRound size={13} className="mr-1.5" /> Replace key
          </Button>
        )}
        {configured && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="cursor-pointer rounded-lg text-rose-300 hover:text-rose-200"
            disabled={busy}
            onClick={onRemoveKey}
          >
            {busy ? <Loader2 size={13} className="mr-1.5 animate-spin" /> : <Trash2 size={13} className="mr-1.5" />}
            Remove key
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="cursor-pointer rounded-lg"
          disabled={testing || !configured}
          onClick={() => void runTest()}
        >
          {testing ? <Loader2 size={13} className="mr-1.5 animate-spin" /> : <Activity size={13} className="mr-1.5" />}
          {testing ? "Testing…" : "Test connection"}
        </Button>
        {test && (
          <span className={cn("inline-flex items-center gap-1.5 text-[11px]", test.ok ? "text-emerald-300" : "text-rose-300")}>
            {test.ok ? <Check size={11} /> : <CircleDashed size={11} />}
            {test.message}
            {test.latencyMs !== undefined && (
              <span className="text-muted-foreground/60">· {test.latencyMs} ms</span>
            )}
          </span>
        )}
      </div>
    </div>
  );
}

/** One provider row. Connect opens the shared panel targeted at that provider. */
function ProviderRow({
  preset,
  state,
  busy,
  onConnect,
  onSelect,
  onDisconnect,
}: {
  preset: LLMPreset;
  state: ProviderState;
  busy: boolean;
  onConnect: () => void;
  onSelect: () => void;
  onDisconnect: () => void;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-3 rounded-xl border px-3.5 py-3 transition-colors",
        state === "active"
          ? "border-white/15 bg-white/[0.04]"
          : "border-white/10 glass-card-subtle",
      )}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.04] text-foreground/85">
        <ProviderLogo provider={preset.id} size={15} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-1.5 text-[12px] font-semibold text-foreground">
          {preset.name}
          {preset.recommended && (
            <span className="rounded-full border border-white/10 bg-white/[0.06] px-1.5 py-0.5 text-[8px] font-bold text-foreground">
              RECOMMENDED
            </span>
          )}
        </p>
        <p className="mt-0.5 line-clamp-1 text-[10px] text-muted-foreground">{preset.description}</p>
      </div>
      <StatusChip state={state} />
      <div className="flex items-center gap-1.5">
        {state === "active" ? (
          <Button variant="outline" size="sm" className="cursor-pointer rounded-lg text-[10px] h-7 px-2.5" disabled>
            In use
          </Button>
        ) : state === "connected" ? (
          <Button variant="outline" size="sm" className="cursor-pointer rounded-lg text-[10px] h-7 px-2.5" onClick={onSelect}>
            Use
          </Button>
        ) : (
          <Button size="sm" className="cursor-pointer rounded-lg text-[10px] h-7 px-2.5" onClick={onConnect}>
            <Plug size={11} className="mr-1" /> Connect
          </Button>
        )}
        {state !== "needs-key" && !preset.keyless && (
          <Button
            variant="ghost"
            size="sm"
            className="cursor-pointer rounded-lg text-[10px] h-7 px-2 text-muted-foreground hover:text-rose-400"
            disabled={busy}
            onClick={onDisconnect}
          >
            <Trash2 size={11} />
          </Button>
        )}
      </div>
    </div>
  );
}

/** One named custom gateway. It is a provider row plus its own key + auth. */
function CustomConnectionItem({
  connection,
  active,
  busy,
  onUse,
  onEdit,
  onDelete,
}: {
  connection: CustomConnectionRow;
  active: boolean;
  busy: boolean;
  onUse: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const authLabel =
    CUSTOM_AUTH_STYLES.find((s) => s.value === connection.authStyle)?.label ?? connection.authStyle;
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-3 rounded-xl border px-3.5 py-3",
        active ? "border-white/15 bg-white/[0.04]" : "border-white/10 glass-card-subtle",
      )}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.04] text-foreground/85">
        <ProviderLogo provider="custom" size={15} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[12px] font-semibold text-foreground">{connection.name}</p>
        <p className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">
          {connection.baseUrl} · {authLabel}
        </p>
      </div>
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-bold",
          active
            ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
            : "border-white/12 bg-white/[0.04] text-muted-foreground",
        )}
      >
        {active ? <Check size={9} /> : <CircleDashed size={9} />}
        {active ? "Active" : connection.hasKey ? "Connected" : "Needs a key"}
      </span>
      <div className="flex items-center gap-1.5">
        {!active && (
          <Button variant="outline" size="sm" className="cursor-pointer rounded-lg text-[10px] h-7 px-2.5" onClick={onUse}>
            Use
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          className="cursor-pointer rounded-lg text-[10px] h-7 px-2.5"
          onClick={onEdit}
        >
          Edit
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="cursor-pointer rounded-lg text-[10px] h-7 px-2 text-muted-foreground hover:text-rose-400"
          disabled={busy}
          onClick={onDelete}
        >
          <Trash2 size={11} />
        </Button>
      </div>
    </div>
  );
}

function ReasoningEffortPicker({
  value,
  onChange,
}: {
  value: ReasoningLevel | null;
  onChange: (value: ReasoningLevel | null) => void;
}) {
  const selected = REASONING_OPTIONS.find((o) => o.value === value) ?? REASONING_OPTIONS[0];
  return (
    <div className="flex flex-col gap-2">
      <Label className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Brain size={12} className="text-primary" />
        Reasoning effort
        <span className="font-normal text-muted-foreground/60">— model variant</span>
      </Label>
      <div role="radiogroup" aria-label="Reasoning effort" className="flex flex-wrap gap-1.5">
        {REASONING_OPTIONS.map((option) => {
          const active = option.value === value;
          return (
            <button
              key={option.label}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => onChange(option.value ?? null)}
              title={option.hint}
              className={cn(
                "cursor-pointer rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors",
                active
                  ? "border-primary/50 bg-primary/15 text-primary"
                  : "border-border/40 text-muted-foreground hover:border-border/60 hover:text-foreground",
              )}
            >
              {option.label}
            </button>
          );
        })}
      </div>
      <p className="text-[10px] leading-relaxed text-muted-foreground">{selected.hint}</p>
    </div>
  );
}

const TEMPERATURE_PRESETS: { label: string; value: number | null; hint: string }[] = [
  { label: "Auto", value: null, hint: "Use the provider's own default temperature." },
  { label: "Precise", value: 0, hint: "Most consistent — best for analysis and repeat runs." },
  { label: "Balanced", value: 0.7, hint: "A middle ground for everyday chat." },
  { label: "Creative", value: 1.2, hint: "More varied wording — not for financial analysis." },
];

function AdvancedSettings({
  llm,
  updateLlm,
  modelCatalog,
}: {
  llm: LLMConfig;
  updateLlm: (patch: Partial<LLMConfig>, debounce?: number) => void;
  modelCatalog: ModelCatalog;
}) {
  const [open, setOpen] = useState(false);
  const preset = presetForBaseUrl(llm.baseUrl);
  const providerId = preset?.id ?? (llm.provider !== "auto" && llm.provider !== "mock" ? llm.provider : undefined);
  const catalogProvider = providerId ? modelCatalog[providerId] : undefined;
  const modelOptions = catalogProvider?.models ?? [];
  const window_ = catalogProvider?.modelWindows?.[llm.model] ?? catalogProvider?.contextWindow ?? null;
  const shown = llm.temperature ?? TEMPERATURE_PRESETS[2].value ?? 0.7;

  return (
    <section className="rounded-2xl border border-white/10 glass-card-subtle">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full cursor-pointer items-center gap-2 px-4 py-3 text-left"
      >
        <SlidersHorizontal size={13} className="text-primary" />
        <span className="text-xs font-semibold text-foreground">Model & sampling</span>
        <span className="text-[10px] text-muted-foreground">optional</span>
        <ChevronDown size={13} className={cn("ml-auto text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="space-y-4 border-t border-white/[0.07] p-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs font-medium text-muted-foreground">Model</Label>
              <ModelPicker
                value={llm.model}
                options={modelOptions}
                windowKnown={window_ != null}
                windowLabel={window_ ? `${window_.toLocaleString()}-token context window` : null}
                onChange={(m) => updateLlm({ model: m }, 0)}
                providerId={providerId}
                baseUrl={llm.baseUrl}
                apiKey={llm.apiKey}
                onDiscovered={() => void loadModelCatalog(true)}
                discoverSignal={llm.hasApiKey ? "key" : "no-key"}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs font-medium text-muted-foreground">Base URL</Label>
              <div className="relative">
                <Server className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={llm.baseUrl}
                  onChange={(e) => updateLlm({ baseUrl: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                  className="pl-8 font-mono"
                />
              </div>
            </div>
          </div>

          {window_ == null && (
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs font-medium text-muted-foreground">Context window (optional)</Label>
              <Input
                type="number"
                min={1}
                value={llm.contextWindow ?? ""}
                onChange={(e) => {
                  const raw = e.target.value.trim();
                  const parsed = raw ? Number(raw) : null;
                  updateLlm({
                    contextWindow: parsed && Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : null,
                  });
                }}
                placeholder="Leave blank if unknown"
                className="font-mono !border-white/12"
              />
            </div>
          )}

          <ReasoningEffortPicker
            value={llm.reasoningEffort ?? null}
            onChange={(v) => updateLlm({ reasoningEffort: v }, 0)}
          />

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between gap-2">
                <Label className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                  <SlidersHorizontal size={12} className="text-primary" /> Temperature
                </Label>
                <span className="font-mono text-xs font-semibold text-foreground">
                  {llm.temperature != null ? llm.temperature.toFixed(1) : "Auto"}
                </span>
              </div>
              <div role="radiogroup" aria-label="Temperature preset" className="flex flex-wrap gap-1.5">
                {TEMPERATURE_PRESETS.map((p) => {
                  const active =
                    p.value == null
                      ? llm.temperature == null
                      : llm.temperature != null && Math.abs(llm.temperature - p.value) < 0.05;
                  return (
                    <button
                      key={p.label}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      title={p.hint}
                      onClick={() => updateLlm({ temperature: p.value }, 0)}
                      className={cn(
                        "cursor-pointer rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors",
                        active
                          ? "border-primary/50 bg-primary/15 text-primary"
                          : "border-border/40 text-muted-foreground hover:border-border/60 hover:text-foreground",
                      )}
                    >
                      {p.label}
                    </button>
                  );
                })}
              </div>
              <input
                type="range"
                min={0}
                max={2}
                step={0.1}
                value={shown}
                disabled={llm.temperature == null}
                aria-label="Temperature"
                onChange={(e) => updateLlm({ temperature: Number(e.target.value) }, 0)}
                className="w-full cursor-pointer accent-primary disabled:cursor-not-allowed disabled:opacity-40"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <Ruler size={12} className="text-primary" /> Max output tokens
              </Label>
              <Input
                type="number"
                min={1}
                value={llm.maxTokens ?? ""}
                onChange={(e) => {
                  const raw = e.target.value.trim();
                  const parsed = raw ? Number(raw) : null;
                  updateLlm({
                    maxTokens: parsed != null && Number.isFinite(parsed) && parsed >= 1 ? Math.floor(parsed) : null,
                  });
                }}
                placeholder="Auto — provider default"
                className="font-mono !border-white/12"
              />
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label className="text-xs font-medium text-muted-foreground">Coding model (optional)</Label>
            <Input
              value={llm.codingModel ?? ""}
              onChange={(e) => updateLlm({ codingModel: e.target.value.trim() || null })}
              placeholder="Same provider and key; used for code tasks"
              className="font-mono !border-white/12"
            />
          </div>
        </div>
      )}
    </section>
  );
}

export function AIProviderSettings({
  llm,
  updateLlm,
  modelCatalog,
}: {
  llm: LLMConfig;
  updateLlm: (patch: Partial<LLMConfig>, debounce?: number) => void;
  modelCatalog: ModelCatalog;
}) {
  const { keys, reload } = useKeyStatuses();
  const { connections, reload: reloadConnections } = useCustomConnections();
  const [showAll, setShowAll] = useState(false);
  const [connectTarget, setConnectTarget] = useState<string | null>(null);
  const [editingConnection, setEditingConnection] = useState<CustomConnectionRow | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [removingKey, setRemovingKey] = useState(false);

  // `mock` means "no provider". Do not infer one from the leftover base URL —
  // that made a disconnected install show Ollama as "Active / In use".
  const activePreset = llm.provider === "mock" ? undefined : presetForBaseUrl(llm.baseUrl);
  const activeId =
    llm.provider === "mock"
      ? undefined
      : activePreset?.id ?? (llm.provider !== "auto" ? llm.provider : undefined);
  const activeConnection = connections.find((c) => c.id === llm.connectionId);

  const states = useMemo(
    () => new Map(LLM_PRESETS.map((p) => [p.id, providerState(p, keys, activeId)])),
    [keys, activeId],
  );

  const connected = useMemo(
    () =>
      LLM_PRESETS.filter(
        (p) =>
          p.id !== "custom" &&
          (states.get(p.id) === "active" || states.get(p.id) === "connected"),
      ),
    [states],
  );
  const popular = useMemo(
    () => LLM_PRESETS.filter((p) => PROVIDERS.find((d) => d.id === p.id)?.popular),
    [],
  );
  const more = useMemo(
    () => LLM_PRESETS.filter((p) => !popular.includes(p) && !connected.includes(p)),
    [popular, connected],
  );

  const selectProvider = (preset: LLMPreset) => {
    updateLlm(
      {
        provider: preset.id as LLMConfig["provider"],
        baseUrl: preset.baseUrl,
        model: preset.model,
        connectionId: undefined,
        contextWindow: null,
      },
      0,
    );
    // A different provider is now active; the composer must stop showing the
    // old provider's models.
    notifyLlmConfigChanged({ resetChoice: true });
  };

  const disconnect = async (preset: LLMPreset) => {
    setBusyId(preset.id);
    try {
      await fetch(`/api/llm/connect?providerId=${encodeURIComponent(preset.id)}`, { method: "DELETE" });
      if (preset.id === activeId) {
        updateLlm({ provider: "mock", apiKey: "", model: "" }, 0);
      }
      reload();
      notifyLlmConfigChanged({ resetChoice: true });
    } finally {
      setBusyId(null);
    }
  };

  const onConnected = (result: ProviderConnectResult) => {
    // Keep the parent's optimistic state in step with what the server saved.
    // The panel stays open so the operator can see the success state and pick a
    // model; its "Use this connection" action closes it.
    updateLlm(
      {
        provider: result.providerId as LLMConfig["provider"],
        baseUrl: result.baseUrl,
        model: result.model,
        // Explicitly clear it for a named provider so a stale custom id cannot
        // make the active card describe the wrong endpoint.
        connectionId: result.connectionId,
      },
      0,
    );
    reload();
    reloadConnections();
  };

  const activateConnection = async (connection: CustomConnectionRow) => {
    setBusyId(connection.id);
    try {
      const res = await fetch("/api/llm/active", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          providerId: "custom",
          connectionId: connection.id,
          baseUrl: connection.baseUrl,
          model: connection.model,
        }),
      });
      if (res.ok) {
        updateLlm(
          {
            provider: "custom",
            connectionId: connection.id,
            baseUrl: connection.baseUrl,
            model: connection.model,
          },
          0,
        );
        notifyLlmConfigChanged({ resetChoice: true });
      }
    } finally {
      setBusyId(null);
    }
  };

  const deleteConnection = async (connection: CustomConnectionRow) => {
    setBusyId(connection.id);
    try {
      await fetch(`/api/llm/connections?id=${encodeURIComponent(connection.id)}`, {
        method: "DELETE",
      });
      if (llm.connectionId === connection.id) {
        updateLlm({ provider: "mock", apiKey: "", model: "" }, 0);
      }
      reloadConnections();
      reload();
      notifyLlmConfigChanged({ resetChoice: true });
    } finally {
      setBusyId(null);
    }
  };

  const openConnect = (providerId: string) => {
    setEditingConnection(null);
    setConnectTarget(providerId);
  };

  const removeActiveKey = async () => {
    setRemovingKey(true);
    try {
      if (llm.provider === "custom" && llm.connectionId) {
        await fetch(`/api/llm/connections?id=${encodeURIComponent(llm.connectionId)}`, {
          method: "DELETE",
        });
      } else if (llm.provider !== "mock" && llm.provider !== "auto") {
        await fetch(`/api/llm/connect?providerId=${encodeURIComponent(llm.provider)}`, {
          method: "DELETE",
        });
      }
      updateLlm({ provider: "mock", apiKey: "", model: "", connectionId: undefined }, 0);
      reload();
      reloadConnections();
      notifyLlmConfigChanged({ resetChoice: true });
    } finally {
      setRemovingKey(false);
    }
  };

  const replaceActiveKey = () => {
    if (activeConnection) {
      setEditingConnection(activeConnection);
      setConnectTarget("custom");
      return;
    }
    openConnect(activeId ?? "");
  };

  const renderRow = (preset: LLMPreset) => (
    <ProviderRow
      key={preset.id}
      preset={preset}
      // The generic "Custom / other" row always means "add a named gateway";
      // the real ones (and their status) are in the Custom connections section.
      state={preset.id === "custom" ? "needs-key" : states.get(preset.id) ?? "needs-key"}
      busy={busyId === preset.id}
      onConnect={() => openConnect(preset.id)}
      onSelect={() => selectProvider(preset)}
      onDisconnect={() => void disconnect(preset)}
    />
  );

  return (
    <div className="space-y-6">
      <ActiveConnection
        llm={llm}
        keys={keys}
        connectionName={activeConnection?.name}
        busy={removingKey}
        modelCatalog={modelCatalog}
        onModelChange={(model) => updateLlm({ model }, 0)}
        onOpenConnect={() => {
          // Editing the active custom gateway should edit THAT connection, not
          // create a second one with the same endpoint.
          if (activeConnection) {
            setEditingConnection(activeConnection);
            setConnectTarget("custom");
          } else {
            openConnect(activeId ?? "");
          }
        }}
        onReplaceKey={replaceActiveKey}
        onRemoveKey={() => void removeActiveKey()}
      />

      {connectTarget !== null && (
        <div className="rounded-2xl border border-primary/25 bg-primary/[0.03] p-4 sm:p-5">
          <ProviderConnect
            initialProviderId={connectTarget || undefined}
            initialBaseUrl={
              editingConnection?.baseUrl ??
              (connectTarget === activeId ? llm.baseUrl : undefined)
            }
            initialModel={
              editingConnection?.model ?? (connectTarget === activeId ? llm.model : undefined)
            }
            initialConnectionId={editingConnection?.id}
            initialConnectionName={editingConnection?.name}
            initialAuthStyle={editingConnection?.authStyle}
            initialApiKeyHeader={editingConnection?.apiKeyHeader}
            initialApiKeyPrefix={editingConnection?.apiKeyPrefix}
            onConnected={onConnected}
            onCancel={() => {
              setConnectTarget(null);
              setEditingConnection(null);
            }}
          />
        </div>
      )}

      {connections.length > 0 && (
        <section>
          <div className="mb-2.5 flex items-center gap-2">
            <Server size={14} className="text-primary" />
            <h3 className="text-xs font-semibold text-foreground">Custom connections</h3>
            <span className="text-[10px] text-muted-foreground">
              {connections.length} named gateway{connections.length === 1 ? "" : "s"}
            </span>
            <button
              type="button"
              onClick={() => openConnect("custom")}
              className="ml-auto text-[10.5px] font-semibold text-primary hover:text-primary/80 cursor-pointer"
            >
              Add another
            </button>
          </div>
          <div className="grid grid-cols-1 gap-2.5">
            {connections.map((connection) => (
              <CustomConnectionItem
                key={connection.id}
                connection={connection}
                active={llm.provider === "custom" && llm.connectionId === connection.id}
                busy={busyId === connection.id}
                onUse={() => void activateConnection(connection)}
                onEdit={() => {
                  setEditingConnection(connection);
                  setConnectTarget("custom");
                }}
                onDelete={() => void deleteConnection(connection)}
              />
            ))}
          </div>
        </section>
      )}

      {connected.length > 0 && (
        <section>
          <div className="mb-2.5 flex items-center gap-2">
            <Plug size={14} className="text-emerald-400" />
            <h3 className="text-xs font-semibold text-foreground">Connected providers</h3>
            <span className="text-[10px] text-muted-foreground">{connected.length}</span>
          </div>
          <div className="grid grid-cols-1 gap-2.5">{connected.map(renderRow)}</div>
        </section>
      )}

      <section>
        <div className="mb-2.5 flex items-center gap-2">
          <Plug size={14} className="text-primary" />
          <h3 className="text-xs font-semibold text-foreground">Popular providers</h3>
          <span className="text-[10px] text-muted-foreground">one click to connect</span>
        </div>
        <div className="grid grid-cols-1 gap-2.5">
          {popular.filter((p) => !connected.includes(p)).map(renderRow)}
        </div>
      </section>

      <section>
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="inline-flex cursor-pointer items-center gap-1.5 text-[11px] font-semibold text-primary hover:text-primary/80"
        >
          <ChevronDown size={14} className={cn("transition-transform", showAll && "rotate-180")} />
          {showAll ? "Hide additional providers" : `Show more providers (${more.length})`}
        </button>
        {showAll && <div className="mt-3 grid grid-cols-1 gap-2.5">{more.map(renderRow)}</div>}
      </section>

      <AdvancedSettings llm={llm} updateLlm={updateLlm} modelCatalog={modelCatalog} />
    </div>
  );
}
