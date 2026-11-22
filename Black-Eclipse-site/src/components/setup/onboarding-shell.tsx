"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Brain,
  Check,
  Database,
  ListChecks,
  Loader2,
  Palette,
  Rocket,
  SlidersHorizontal,
} from "@/lib/icons";
import { cn } from "@/lib/utils";
import { safeSourceDefaults } from "@/lib/setup/defaults";
import { bundleSources } from "@/lib/setup/bundles";
import { getLLMPreset } from "@/lib/agent/presets";
import { seedStarterDashboard } from "@/lib/dashboard/use-dashboards";
import { setSetupOverlay } from "@/lib/setup/overlay";
import { RISK_DISCLOSURE_VERSION } from "@/lib/legal/disclosure";
import type { SetupDraft, SetupKeyStatuses, SetupState } from "./types";
import { draftAiChoice } from "./types";
import { WelcomeStep } from "./steps/welcome-step";
import { ThemeStep } from "./steps/theme-step";
import { AiStep } from "./steps/ai-step";
import { IncludedStep } from "./steps/included-step";
import { ProvidersStep } from "./steps/providers-step";
import { ReviewStep, type CheckItemView } from "./steps/review-step";

const STEPS = [
  {
    id: "welcome",
    title: "Welcome",
    short: "How you'll use Eclipse",
    description: "Pick a starting point. Everything here can be changed later.",
    icon: Rocket,
  },
  {
    id: "theme",
    title: "Look & feel",
    short: "Make it yours",
    description: "Pick the theme Eclipse opens with. It applies app-wide and can be changed anytime.",
    icon: Palette,
  },
  {
    id: "ai",
    title: "AI & Chat",
    short: "Power your assistant",
    description: "Connect a model so chat works, or add one later.",
    icon: Brain,
  },
  {
    id: "included",
    title: "Included data",
    short: "Open sources on us",
    description: "Free, open data that ships with Eclipse. No account needed.",
    icon: Database,
  },
  {
    id: "providers",
    title: "Providers",
    short: "Live prices & search",
    description: "Optional providers for quotes, crypto and web search.",
    icon: SlidersHorizontal,
  },
  {
    id: "review",
    title: "Review",
    short: "Check and finish",
    description: "Confirm what will work before you start using Eclipse.",
    icon: ListChecks,
  },
] as const;

// Finishing setup must always end in one of two states: inside the app, or a
// wizard that says exactly what went wrong and can be retried. A request with
// no deadline is neither — the button sat on "Saving…" forever whenever the
// local server stalled (a locked SQLite write, a restart, a paused update).
const FINISH_TIMEOUT_MS = 30_000;
const RECONCILE_TIMEOUT_MS = 8_000;

/** An abort signal that works on every WebView (AbortSignal.timeout is recent). */
function abortAfter(ms: number): AbortSignal | undefined {
  if (typeof AbortController === "undefined") return undefined;
  const controller = new AbortController();
  setTimeout(() => controller.abort(), ms);
  return controller.signal;
}

/**
 * Is the completion flag actually persisted?
 *
 * A failed or timed-out `POST /api/setup` is not proof the save failed: the
 * request can land after the browser gave up on it (or its reply can be lost).
 * The stored flag is the only truth, so ask it before keeping the user in the
 * wizard — otherwise a saved install is told to "try again" forever.
 */
async function onboardingIsRecorded(): Promise<boolean> {
  try {
    const res = await fetch("/api/setup", {
      cache: "no-store",
      signal: abortAfter(RECONCILE_TIMEOUT_MS),
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { completed?: unknown };
    return data.completed === true;
  } catch {
    return false;
  }
}

const PRICE_ADVANCED = ["tradingview", "yfinance", "http", "browser", "scrapling"];
const PRICE_CONNECTED = ["polygon", "twelvedata", "alphavantage", "marketstack", "finnhub"];

const BUNDLE_TEMPLATE: Record<string, string> = {
  research: "research",
  "personal-trader": "trader-pro",
  commercial: "desk",
  crypto: "chain-desk",
  custom: "research",
};

/** The starter dashboard template for the chosen bundle. */
function templateFor(draft: SetupDraft): string {
  return BUNDLE_TEMPLATE[draft.bundleId] ?? "research";
}

function initialDraft(state: SetupState): SetupDraft {
  return {
    step: 0,
    mode: state.mode,
    bundleId: state.mode === "commercial" ? "commercial" : "research",
    sources: Object.fromEntries(state.sources.map((s) => [s.id, s.enabled])),
    accepted: state.acceptedRestrictedSources ?? [],
    priceMode:
      state.mode === "commercial"
        ? PRICE_CONNECTED.some(
              (id) => state.sources.find((source) => source.id === id)?.keyConfigured === true,
            )
          ? "licensed"
          : "skip"
        : PRICE_ADVANCED.some(
              (id) => state.sources.find((source) => source.id === id)?.enabled === true,
            )
          ? "community"
          : PRICE_CONNECTED.some(
                (id) => state.sources.find((source) => source.id === id)?.keyConfigured === true,
              )
            ? "licensed"
            : "skip",
    aiKind: state.ai.configured ? "keep" : "skip",
    // Show the provider that is actually saved, not a hard-coded default.
    aiPreset: state.ai.configured ? state.ai.provider : "openai",
    aiBaseUrl: state.ai.configured ? state.ai.baseUrl : "",
    aiModel: state.ai.configured ? state.ai.model : "",
    aiApiKey: "",
    extraKeys: {},
    riskAck: state.riskAck?.accepted ?? false,
  };
}

export function OnboardingShell({
  state,
  onDone,
}: {
  state: SetupState;
  onDone: () => void;
}) {
  const [draft, setDraft] = useState<SetupDraft>(() => initialDraft(state));
  const [keyStatuses, setKeyStatuses] = useState<SetupKeyStatuses>({});
  const [checking, setChecking] = useState(false);
  const [checkItems, setCheckItems] = useState<CheckItemView[] | null>(
    () => state.lastCheck?.items ?? null,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Setup is recorded but an optional step (the AI/key choice) failed. The
  // wizard stays open so it can be fixed, with a way into the app regardless.
  const [warning, setWarning] = useState<string | null>(null);
  const [canEnter, setCanEnter] = useState(false);
  const [copiedDiag, setCopiedDiag] = useState(false);
  // A save that takes longer than a couple of seconds is unusual (the three
  // writes are tiny). Say so instead of leaving a silent spinner: a busy
  // database is the common cause and the wait is bounded.
  const [slowSave, setSlowSave] = useState(false);

  const refreshKeys = useCallback(async () => {
    try {
      const res = await fetch("/api/provider-keys", { cache: "no-store" });
      const data = (await res.json()) as { keys?: Array<{ envVar: string } & Record<string, unknown>> };
      if (Array.isArray(data.keys)) {
        setKeyStatuses(
          Object.fromEntries(data.keys.map((k) => [k.envVar, k as never])),
        );
      }
    } catch {
      /* key status is advisory; leave the map empty */
    }
  }, []);

  useEffect(() => {
    void refreshKeys();
  }, [refreshKeys]);

  // Lock the page behind the setup card. Only the card's content area scrolls.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  // While the wizard is on screen, two things must be true:
  //
  // 1. Long-lived streams step aside. A first run has no app shell, but a
  //    forced re-run (`?setup=1`) is mounted over the live shell, and an SSE
  //    would hold one of the browser's six per-origin connections against this
  //    wizard's own requests.
  // 2. The native first-run notice cannot cover the footer. It is a fixed card
  //    at bottom-right with a viewport-high z-index — exactly on top of
  //    "Finish setup" and "Skip for now" on a normal window. It stays readable
  //    at the top-right and never intercepts a click.
  useEffect(() => {
    setSetupOverlay(true);

    const nudgeNotice = (node: HTMLElement) => {
      node.style.top = "18px";
      node.style.bottom = "auto";
      node.style.pointerEvents = "none";
      node.querySelectorAll("button").forEach((button) => {
        (button as HTMLElement).style.pointerEvents = "auto";
      });
    };
    const existing = document.getElementById("be-first-run");
    if (existing) nudgeNotice(existing);
    const observer = new MutationObserver(() => {
      const node = document.getElementById("be-first-run");
      if (node) nudgeNotice(node);
    });
    observer.observe(document.body, { childList: true });

    return () => {
      observer.disconnect();
      setSetupOverlay(false);
    };
  }, []);

  useEffect(() => {
    if (!busy) {
      setSlowSave(false);
      return;
    }
    const timer = window.setTimeout(() => setSlowSave(true), 6_000);
    return () => window.clearTimeout(timer);
  }, [busy]);

  // Focus trap: the app behind the overlay must not be reachable by Tab.
  const cardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const root = cardRef.current;
    if (!root) return;
    const focusables = () =>
      Array.from(
        root.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => el.offsetParent !== null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const list = focusables();
      if (list.length === 0) return;
      const first = list[0];
      const last = list[list.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    root.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const updateDraft = (patch: Partial<SetupDraft>) => {
    setDraft((current) => {
      const next = { ...current, ...patch };
      if (patch.mode && patch.mode !== current.mode) {
        next.sources = safeSourceDefaults(patch.mode);
        next.accepted = [];
        next.bundleId = patch.mode === "commercial" ? "commercial" : "research";
        next.priceMode = "skip";
      }
      if (patch.bundleId && patch.bundleId !== current.bundleId) {
        next.sources = bundleSources(patch.bundleId, next.mode);
        next.accepted = [];
      }
      return next;
    });
  };

  const goToStep = (step: number) => {
    if (step < 0 || step >= STEPS.length) return;
    if (step === 0 && !draft.riskAck) return;
    setError(null);
    updateDraft({ step });
  };

  const saveKey = useCallback(async (envVar: string, value: string): Promise<string> => {
    try {
      const res = await fetch("/api/provider-keys", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ envVar, value }),
      });
      const data = (await res.json()) as { error?: string };
      if (!res.ok) return data.error || "Could not save the key.";
      await refreshKeys();
      return "Saved. The provider will be used on the next request.";
    } catch (err) {
      return err instanceof Error ? err.message : "Could not save the key.";
    }
  }, [refreshKeys]);

  const runCheck = async () => {
    setChecking(true);
    setCheckItems(null);
    try {
      const enabled = state.sources
        .filter((s) => draft.sources[s.id] === true)
        .map((s) => s.id);
      const preset = getLLMPreset(draft.aiPreset);
      const aiConfig =
        draft.aiKind === "keep" || draft.aiKind === "skip"
          ? undefined
          : draft.aiKind === "local"
            ? { baseUrl: draft.aiBaseUrl || preset?.baseUrl, model: draft.aiModel || preset?.model }
            : draft.aiKind === "key"
              ? {
                  baseUrl: preset?.baseUrl,
                  model: draft.aiModel || preset?.model,
                  apiKey: draft.aiApiKey || undefined,
                }
              : {
                  baseUrl: draft.aiBaseUrl,
                  model: draft.aiModel,
                  apiKey: draft.aiApiKey || undefined,
                };
      const res = await fetch("/api/setup/check", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          ai: draft.aiKind !== "skip",
          aiConfig,
          sources: enabled,
        }),
      });
      const data = (await res.json()) as { items?: CheckItemView[]; error?: string };
      if (!res.ok) throw new Error(data.error || "Check failed");
      setCheckItems(data.items ?? []);
    } catch (err) {
      setCheckItems([{
        id: "check",
        label: "Check",
        status: "failed",
        message: err instanceof Error ? err.message : "Check failed",
      }]);
    } finally {
      setChecking(false);
    }
  };

  const enter = (templateId: string) => {
    seedStarterDashboard(templateId);
    onDone();
  };

  /**
   * A support-ready line the user can paste, with no secrets in it. It is also
   * written to the console, so the desktop log carries the same failure.
   */
  const copyDiagnostics = async () => {
    const lines = [
      "BlackEclipse setup diagnostics",
      `at: ${new Date().toISOString()}`,
      `url: ${window.location.href}`,
      `flow: ${state.isNewInstall ? "first run" : "setup re-run"}`,
      `error: ${error ?? "(none)"}`,
      `warning: ${warning ?? "(none)"}`,
      `agent: ${navigator.userAgent}`,
    ].join("\n");
    console.error(`[setup] diagnostics\n${lines}`);
    try {
      await navigator.clipboard.writeText(lines);
      setCopiedDiag(true);
      window.setTimeout(() => setCopiedDiag(false), 1600);
    } catch {
      /* clipboard denied — the console line above is still there */
    }
  };

  const finish = async (skip = false) => {
    if (!draft.riskAck) {
      setError("Acknowledge the risk notice to continue.");
      setDraft((current) => ({ ...current, step: 0 }));
      return;
    }
    const templateId = BUNDLE_TEMPLATE[skip ? "research" : draft.bundleId] ?? "research";
    // Only a first run can "already be saved" while the reply says otherwise.
    // For a forced re-run the install was complete all along, so a failed save
    // must never be mistaken for success.
    const completionWasPending = !state.completed;
    setBusy(true);
    setError(null);
    setWarning(null);
    setCanEnter(false);
    const riskAck = { version: RISK_DISCLOSURE_VERSION };
    try {
      const body = skip
        ? {
            mode: "personal",
            bundleId: "research",
            sources: safeSourceDefaults("personal"),
            acceptedRestrictedSources: [],
            ai: { kind: "skip" },
            riskAck,
          }
        : {
            mode: draft.mode,
            bundleId: draft.bundleId,
            sources: draft.sources,
            acceptedRestrictedSources: draft.accepted,
            ai: draftAiChoice(draft),
            riskAck,
          };
      const res = await fetch("/api/setup", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: abortAfter(FINISH_TIMEOUT_MS),
      });
      const data = (await res.json().catch(() => ({}))) as {
        error?: string;
        warning?: string;
        completed?: boolean;
      };
      if (!res.ok) {
        // The completion write can land before a later step fails, so a
        // non-2xx reply must be checked against the stored flag before the
        // user is sent back into a wizard whose work is already done.
        if (completionWasPending && (data.completed === true || (await onboardingIsRecorded()))) {
          console.warn("[setup] finish reported an error after the save landed:", data.error);
          enter(templateId);
          return;
        }
        throw new Error(data.error || "Could not save setup");
      }
      if (typeof data.warning === "string" && data.warning.trim() !== "") {
        // Setup itself IS recorded; only the optional AI/key step failed. Show
        // why, keep the wizard on screen so Back can fix it, and offer a way
        // into the app instead of trapping the user here. The Review step owns
        // the warning, so make sure it is the visible one.
        setWarning(data.warning.trim());
        setCanEnter(true);
        setDraft((current) => ({ ...current, step: STEPS.length - 1 }));
        return;
      }
      enter(templateId);
    } catch (err) {
      const aborted =
        typeof err === "object" &&
        err !== null &&
        ((err as { name?: string }).name === "AbortError" ||
          (err as { name?: string }).name === "TimeoutError");
      // A stalled request is not a failed save: the write may have landed.
      if (completionWasPending && (await onboardingIsRecorded())) {
        enter(templateId);
        return;
      }
      console.error("[setup] finish failed:", err);
      setError(
        aborted
          ? "The local server did not answer while saving setup. Nothing was lost — press Finish setup to try again."
          : err instanceof Error
            ? err.message
            : "Could not save setup",
      );
    } finally {
      setBusy(false);
    }
  };

  const active = STEPS[draft.step];
  const ActiveIcon = active.icon;
  const progress = useMemo(() => ((draft.step + 1) / STEPS.length) * 100, [draft.step]);

  return (
    <div
      className="fixed inset-0 z-[200] overflow-hidden bg-background"
      role="dialog"
      aria-modal="true"
      aria-label="BlackEclipse setup"
    >
      {/* Same launch-page treatment: a masked 44px brand grid and a soft glow. */}
      <div
        className="pointer-events-none fixed inset-0"
        style={{
          backgroundImage:
            "linear-gradient(rgba(180,255,66,0.08) 1px, transparent 1px), linear-gradient(90deg, rgba(180,255,66,0.08) 1px, transparent 1px)",
          backgroundSize: "44px 44px",
          maskImage: "radial-gradient(closest-side at 50% 42%, #000 20%, transparent 100%)",
          WebkitMaskImage: "radial-gradient(closest-side at 50% 42%, #000 20%, transparent 100%)",
        }}
      />
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(closest-side_at_50%_30%,rgba(180,255,66,0.13),transparent_72%)]" />

      <div className="relative flex min-h-full items-center justify-center p-3 sm:p-6">
        <main className="w-full max-w-[1060px]">
          <div
            ref={cardRef}
            tabIndex={-1}
            className="glass-card-strong relative flex max-h-[calc(100vh-1.5rem)] flex-col overflow-hidden rounded-2xl border border-white/12 outline-none supports-[height:100dvh]:max-h-[calc(100dvh-1.5rem)] sm:max-h-[calc(100vh-3rem)] sm:supports-[height:100dvh]:max-h-[calc(100dvh-3rem)]"
          >
            <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/50 to-transparent" />

            <div className="grid min-h-0 flex-1 md:grid-cols-[300px_minmax(0,1fr)]">
              {/* ── Left rail: brand + stepper ───────────────────────────── */}
              <aside className="hidden min-h-0 flex-col border-r border-white/[0.07] bg-white/[0.025] px-6 py-7 md:flex">
                <img
                  src="/branding/LOGO-WORDMARK.svg"
                  alt="BlackEclipse"
                  className="h-[24px] w-auto shrink-0 self-start"
                />

                <ol className="relative mb-auto mt-9 min-h-0 overflow-y-auto pr-1">
                  {/* One continuous rail behind the discs: the icons are opaque,
                      so the line can never show through a glyph. */}
                  <span
                    aria-hidden
                    className="absolute left-[19.5px] top-5 bottom-5 w-px bg-white/[0.08]"
                  />
                  <span
                    aria-hidden
                    className="absolute left-[19.5px] top-5 w-px bg-primary/50"
                    style={{
                      height: `calc(${draft.step / (STEPS.length - 1)} * (100% - 2.5rem))`,
                    }}
                  />
                  {STEPS.map((step, index) => {
                    const done = index < draft.step;
                    const isActive = index === draft.step;
                    const Icon = step.icon;
                    // Once the risk notice is acknowledged the whole rail is
                    // navigable — a step is reachable whether it is behind or
                    // ahead. The notice itself stays a hard gate: until it is
                    // accepted, no jump (including forward) is offered.
                    const reachable = draft.riskAck;
                    const disabled = !reachable || isActive;
                    return (
                      <li key={step.id} className="relative pb-7 last:pb-0">
                        <button
                          type="button"
                          disabled={disabled}
                          aria-current={isActive ? "step" : undefined}
                          aria-label={`${step.title} — ${step.short}`}
                          onClick={() => goToStep(index)}
                          className={cn(
                            "group flex w-full gap-3.5 rounded-xl text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
                            disabled ? "cursor-default" : "cursor-pointer",
                          )}
                        >
                          <span
                            className={cn(
                              "relative z-10 flex h-10 w-10 shrink-0 items-center justify-center rounded-full border transition-colors",
                              isActive
                                ? "border-primary bg-primary shadow-[0_0_0_5px_color-mix(in_srgb,var(--primary)_14%,transparent)]"
                                : done
                                  ? "border-primary/40 bg-[color-mix(in_srgb,var(--primary)_16%,var(--card))]"
                                  : "border-white/12 bg-[color-mix(in_srgb,white_4%,var(--card))] text-muted-foreground/70",
                              !disabled && "group-hover:border-primary/50",
                            )}
                            /* Icon colour follows the disc, set inline because
                               globals.css greys any span class containing
                               `text-primary` with !important. Light accent disc →
                               near-black glyph; dark tinted disc → accent glyph. */
                            style={
                              isActive
                                ? { color: "var(--primary-foreground)" }
                                : done
                                  ? { color: "var(--primary)" }
                                  : undefined
                            }
                          >
                            {done ? <Check size={16} weight="bold" /> : <Icon size={16} />}
                          </span>
                          <span className="min-w-0 pt-0.5">
                            <span
                              className={cn(
                                "block text-[13.5px] font-semibold leading-tight transition-colors",
                                isActive
                                  ? "text-foreground"
                                  : done
                                    ? "text-foreground/80 group-hover:text-foreground"
                                    : "text-muted-foreground/60 group-hover:text-muted-foreground/80",
                              )}
                            >
                              {step.title}
                            </span>
                            <span
                              className={cn(
                                "mt-1 block text-[11.5px] leading-snug",
                                isActive ? "text-muted-foreground" : "text-muted-foreground/55",
                              )}
                            >
                              {step.short}
                            </span>
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ol>

                <div className="mt-8 shrink-0 space-y-1 border-t border-white/[0.07] pt-4">
                  <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground/40">
                    Local setup · 127.0.0.1
                  </p>
                  <p className="text-[11px] leading-relaxed text-muted-foreground/50">
                    Your keys stay on this machine.
                  </p>
                </div>
              </aside>

              {/* ── Right panel: step content + actions ──────────────────── */}
              <section className="flex min-h-0 flex-col">
                {/* Mobile brand + progress (rail is hidden below md). */}
                <div className="flex shrink-0 items-center gap-3 border-b border-white/[0.07] px-5 py-3 md:hidden">
                  <img
                    src="/branding/LOGO-WORDMARK.svg"
                    alt="BlackEclipse"
                    className="h-[20px] w-auto"
                  />
                  <span className="ml-auto text-[11px] text-muted-foreground">
                    Step {draft.step + 1} of {STEPS.length}
                  </span>
                </div>
                <div className="shrink-0 px-5 pt-4 md:hidden">
                  <div className="h-[3px] overflow-hidden rounded-full bg-white/[0.08]">
                    <div
                      className="h-full rounded-full bg-primary transition-[width] duration-500"
                      style={{ width: `${progress}%` }}
                    />
                  </div>
                </div>

                <header className="flex shrink-0 items-start gap-3.5 px-5 pb-1 pt-6 sm:px-8">
                  <span className="mt-0.5 hidden h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-primary/25 bg-primary/12 text-primary sm:flex">
                    <ActiveIcon size={20} />
                  </span>
                  <div className="min-w-0">
                    <h1 className="font-serif text-[22px] font-semibold leading-[1.25] tracking-[-0.01em] text-foreground">
                      {active.title}
                    </h1>
                    <p className="mt-1 max-w-[56ch] text-[13px] leading-relaxed text-muted-foreground [@media(max-height:620px)]:hidden">
                      {active.description}
                    </p>
                  </div>
                </header>

                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 pb-6 pt-5 sm:px-8">
                  {draft.step === 0 && (
                    <WelcomeStep state={state} draft={draft} setDraft={updateDraft} />
                  )}
                  {draft.step === 1 && <ThemeStep />}
                  {draft.step === 2 && (
                    <AiStep state={state} draft={draft} setDraft={updateDraft} />
                  )}
                  {draft.step === 3 && (
                    <IncludedStep state={state} draft={draft} setDraft={updateDraft} />
                  )}
                  {draft.step === 4 && (
                    <ProvidersStep
                      state={state}
                      draft={draft}
                      setDraft={updateDraft}
                      keyStatuses={keyStatuses}
                      onSaveKey={saveKey}
                    />
                  )}
                  {draft.step === 5 && (
                    <ReviewStep
                      state={state}
                      draft={draft}
                      keyStatuses={keyStatuses}
                      checking={checking}
                      checkItems={checkItems}
                      onCheck={() => void runCheck()}
                      error={error}
                      warning={warning}
                    />
                  )}
                </div>

                {busy && slowSave && (
                  <p className="shrink-0 border-t border-white/[0.07] bg-amber-500/[0.04] px-5 py-2 text-[11.5px] leading-relaxed text-amber-200/90 sm:px-8">
                    Still saving — the local database is taking longer than usual (usually another
                    Eclipse window or a disk scan). This is bounded; if it cannot finish, the button
                    comes back with a retry and a diagnostics option.
                  </p>
                )}

                <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-2 border-t border-white/[0.07] bg-white/[0.015] px-5 py-3.5 sm:px-8">
                  {draft.step === 0 ? (
                    <button
                      type="button"
                      className="rounded-lg px-2 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40 cursor-pointer"
                      disabled={!draft.riskAck}
                      title={draft.riskAck ? undefined : "Acknowledge the risk notice first"}
                      onClick={() => void finish(true)}
                    >
                      Skip for now
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                      onClick={() => goToStep(draft.step - 1)}
                    >
                      <ArrowLeft size={13} /> Back
                    </button>
                  )}

                  <span className="flex-1" />

                  {(error || warning) && (
                    <button
                      type="button"
                      className="rounded-lg px-2 py-1.5 text-[11.5px] font-medium text-muted-foreground underline-offset-2 transition-colors hover:text-foreground hover:underline cursor-pointer"
                      onClick={() => void copyDiagnostics()}
                      title="Copy a support-ready summary (no secrets) to the clipboard"
                    >
                      {copiedDiag ? "Copied" : "Copy diagnostics"}
                    </button>
                  )}

                  {draft.step < STEPS.length - 1 ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-[12.5px] font-semibold text-primary-foreground transition-colors hover:bg-primary/85 disabled:cursor-not-allowed disabled:opacity-40 cursor-pointer"
                      disabled={draft.step === 0 && !draft.riskAck}
                      title={
                        draft.step === 0 && !draft.riskAck
                          ? "Acknowledge the risk notice first"
                          : undefined
                      }
                      onClick={() => goToStep(draft.step + 1)}
                    >
                      Continue <ArrowRight size={13} />
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-[12.5px] font-semibold text-primary-foreground transition-colors hover:bg-primary/85 disabled:cursor-not-allowed disabled:opacity-40 cursor-pointer"
                      disabled={busy || !draft.riskAck}
                      title={draft.riskAck ? undefined : "Acknowledge the risk notice first"}
                      onClick={() => (canEnter ? enter(templateFor(draft)) : void finish(false))}
                    >
                      {busy ? (
                        <Loader2 size={13} className="animate-spin" />
                      ) : (
                        <Check size={13} weight="bold" />
                      )}
                      {busy ? "Saving…" : canEnter ? "Enter Eclipse" : "Finish setup"}
                    </button>
                  )}
                </div>
              </section>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
