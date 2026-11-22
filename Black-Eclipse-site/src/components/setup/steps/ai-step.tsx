"use client";

import { useEffect, useState } from "react";
import { Activity, Check, CircleDashed, Loader2 } from "@/lib/icons";
import { Button } from "@/components/ui/button";
import { ProviderLogo } from "@/components/providers/provider-logo";
import { notifyLlmConfigChanged } from "@/lib/agent/llm-config-events";
import { getLLMPreset } from "@/lib/agent/presets";
import { ProviderConnect, type ProviderConnectResult } from "@/components/providers/provider-connect";
import type { SetupDraft, SetupState } from "../types";

/**
 * Onboarding › AI & Chat.
 *
 * One job: get a working model with as few decisions as possible. The shared
 * `ProviderConnect` panel handles paste-detection, the real test and model
 * discovery, and persists the connection atomically.
 *
 * After connecting, the panel's success view STAYS OPEN until the operator
 * confirms with "Use this connection". It is the place where they pick which of
 * the provider's models to use; collapsing it immediately (the old behaviour)
 * hid both the success state and the model list. The connected card that follows
 * keeps a real "Test connection" action, because a saved config is not proof
 * that the endpoint still answers.
 */
export function AiStep({
  state,
  draft,
  setDraft,
}: {
  state: SetupState;
  draft: SetupDraft;
  setDraft: (patch: Partial<SetupDraft>) => void;
}) {
  // The connect panel is the editor. It opens on a fresh install and whenever
  // "Change" is pressed; it is not closed by the success callback.
  const [panelOpen, setPanelOpen] = useState(() => !state.ai.configured);
  const [saved, setSaved] = useState<{
    providerId: string;
    baseUrl: string;
    model: string;
    connectionId?: string;
  } | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [switching, setSwitching] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; message: string; latencyMs?: number } | null>(null);

  const hasSavedConfig = state.ai.configured || Boolean(draft.aiModel);
  const connected = !panelOpen && draft.aiKind === "keep" && hasSavedConfig;

  const providerId = saved?.providerId ?? (draft.aiPreset || state.ai.provider);
  const providerName =
    providerId === "custom"
      ? "Custom endpoint"
      : (getLLMPreset(providerId)?.name ?? providerId) || "Provider";
  const baseUrl = saved?.baseUrl ?? (draft.aiBaseUrl || state.ai.baseUrl);
  const activeModel = saved?.model ?? (draft.aiModel || state.ai.model);

  // Load the provider/model that was actually saved (never trust the boot-time
  // setup snapshot) plus the real model list, so the connected card can offer a
  // model choice and tell the truth about the endpoint.
  useEffect(() => {
    if (!connected) return;
    let live = true;
    void Promise.all([
      fetch("/api/settings", { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null),
      fetch("/api/harness/chat/models", { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null),
    ]).then(([settings, catalog]) => {
      if (!live) return;
      const llm = (settings as { llm?: Record<string, unknown> } | null)?.llm ?? {};
      const savedProvider =
        typeof llm.provider === "string" && llm.provider !== "auto" && llm.provider !== "mock"
          ? llm.provider
          : draft.aiPreset || state.ai.provider;
      const savedBaseUrl =
        typeof llm.baseUrl === "string" && llm.baseUrl ? llm.baseUrl : draft.aiBaseUrl || state.ai.baseUrl;
      const savedModel =
        typeof llm.model === "string" && llm.model ? llm.model : draft.aiModel || state.ai.model;
      const connectionId = typeof llm.connectionId === "string" ? llm.connectionId : undefined;
      setSaved({
        providerId: savedProvider,
        baseUrl: savedBaseUrl,
        model: savedModel,
        ...(connectionId ? { connectionId } : {}),
      });
      const providers = (catalog as { providers?: Array<{ id?: string; models?: string[] }> } | null)
        ?.providers;
      const group = Array.isArray(providers)
        ? providers.find((p) => p.id === savedProvider)
        : undefined;
      const list = Array.isArray(group?.models) ? group.models : [];
      setModels(
        savedModel && !list.includes(savedModel) ? [savedModel, ...list] : list,
      );
    });
    return () => {
      live = false;
    };
  }, [
    connected,
    draft.aiPreset,
    draft.aiBaseUrl,
    draft.aiModel,
    state.ai.provider,
    state.ai.baseUrl,
    state.ai.model,
  ]);

  const onConnected = (result: ProviderConnectResult) => {
    // The connect call already persisted the provider, key, base URL and model,
    // so the wizard must NOT re-apply it at Finish. `keep` means "leave the
    // saved configuration alone". The panel stays open until the operator picks
    // a model and confirms.
    setDraft({
      aiKind: "keep",
      aiPreset: result.providerId,
      aiBaseUrl: result.baseUrl,
      aiModel: result.model,
      aiApiKey: "",
    });
    setTest(null);
    setModelError(null);
  };

  const changeModel = async (next: string) => {
    if (!saved || !next || next === saved.model) return;
    setSwitching(true);
    setModelError(null);
    try {
      const res = await fetch("/api/llm/active", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          providerId: saved.providerId,
          baseUrl: saved.baseUrl,
          model: next,
          connectionId: saved.connectionId,
        }),
      });
      const data = (await res.json().catch(() => ({}))) as { ok?: boolean; message?: string };
      if (!res.ok || !data.ok) throw new Error(data.message || "Could not switch model.");
      setSaved((current) => (current ? { ...current, model: next } : current));
      setDraft({ aiKind: "keep", aiModel: next });
      notifyLlmConfigChanged();
    } catch (err) {
      setModelError(err instanceof Error ? err.message : "Could not switch model.");
    } finally {
      setSwitching(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      const res = await fetch("/api/agent/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = (await res.json().catch(() => ({}))) as {
        ok?: boolean;
        message?: string;
        error?: string;
        latencyMs?: number;
      };
      setTest({
        ok: Boolean(data.ok),
        message: data.message || data.error || "No response",
        latencyMs: typeof data.latencyMs === "number" ? data.latencyMs : undefined,
      });
    } catch {
      setTest({ ok: false, message: "Could not run the test." });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-5">
      {connected && (
        <div className="space-y-3 rounded-xl border border-emerald-500/20 bg-emerald-500/[0.05] p-3.5">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-emerald-500/25 bg-emerald-500/10">
              <ProviderLogo provider={providerId} size={16} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="flex items-center gap-1.5 text-[12.5px] font-semibold text-foreground">
                <Check size={13} className="text-emerald-400" />
                Connected
                <span className="font-normal text-muted-foreground">· {providerName}</span>
              </p>
              <p className="mt-0.5 truncate font-mono text-[10.5px] text-muted-foreground" title={baseUrl}>
                {baseUrl || "—"}
              </p>
            </div>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="cursor-pointer rounded-lg text-[11.5px]"
              onClick={() => {
                setPanelOpen(true);
                setTest(null);
                setModelError(null);
              }}
            >
              Change
            </Button>
          </div>

          <div className="grid gap-2 sm:grid-cols-2">
            <div className="min-w-0 rounded-lg border border-white/10 bg-white/[0.02] px-2.5 py-1.5">
              <p className="text-[9.5px] uppercase tracking-wider text-muted-foreground">
                Model
                {switching && <Loader2 size={10} className="ml-1 inline animate-spin" />}
              </p>
              {models.length > 1 ? (
                <select
                  value={activeModel}
                  disabled={switching}
                  onChange={(e) => void changeModel(e.target.value)}
                  title="Change the chat model"
                  className="mt-0.5 w-full cursor-pointer truncate rounded-md border border-white/10 bg-transparent py-0.5 font-mono text-[11.5px] font-semibold text-foreground focus:border-primary/40 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {models.map((m) => (
                    <option key={m} value={m} className="bg-background text-foreground">
                      {m}
                    </option>
                  ))}
                </select>
              ) : (
                <p className="mt-0.5 truncate font-mono text-[11.5px] font-semibold text-foreground" title={activeModel}>
                  {activeModel || "—"}
                </p>
              )}
            </div>
            <div className="min-w-0 rounded-lg border border-white/10 bg-white/[0.02] px-2.5 py-1.5">
              <p className="text-[9.5px] uppercase tracking-wider text-muted-foreground">Endpoint</p>
              <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground" title={baseUrl}>
                {baseUrl || "—"}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="cursor-pointer rounded-lg text-[11.5px]"
              disabled={testing}
              onClick={() => void runTest()}
            >
              {testing ? (
                <Loader2 size={12} className="mr-1.5 animate-spin" />
              ) : (
                <Activity size={12} className="mr-1.5" />
              )}
              {testing ? "Testing…" : "Test connection"}
            </Button>
            {test && (
              <span
                className={`inline-flex items-center gap-1.5 text-[11px] ${
                  test.ok ? "text-emerald-300" : "text-rose-300"
                }`}
              >
                {test.ok ? <Check size={11} /> : <CircleDashed size={11} />}
                {test.message}
                {test.latencyMs !== undefined && (
                  <span className="text-muted-foreground/60">· {test.latencyMs} ms</span>
                )}
              </span>
            )}
          </div>
          {modelError && (
            <p className="text-[11px] leading-relaxed text-rose-300">{modelError}</p>
          )}
        </div>
      )}

      {panelOpen && (
        <>
          <ProviderConnect
            allowSkip={!hasSavedConfig}
            onConnected={onConnected}
            onSkip={() => {
              setDraft({ aiKind: "skip" });
              setPanelOpen(false);
            }}
            onCancel={hasSavedConfig ? () => setPanelOpen(false) : undefined}
          />
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            You can connect or change the model later in Settings → AI. Skipping only turns chat
            off; every other part of Eclipse keeps working.
          </p>
        </>
      )}

      {draft.aiKind === "skip" && !connected && !panelOpen && (
        <div className="space-y-2.5 rounded-xl border border-amber-500/20 bg-amber-500/[0.04] p-3">
          <p className="text-[11.5px] leading-relaxed text-amber-200/90">
            Chat will be unavailable until you connect a model. You can do that any time in
            Settings → AI.
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="cursor-pointer rounded-lg text-[11.5px]"
            onClick={() => setPanelOpen(true)}
          >
            Connect a model
          </Button>
        </div>
      )}
    </div>
  );
}
