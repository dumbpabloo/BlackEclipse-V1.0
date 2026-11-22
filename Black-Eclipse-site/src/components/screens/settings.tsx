"use client";

// TODO(P2, deferred): this file is the Settings shell plus the inline bodies
// for General / AI / Investor Eye / Economics / Market / News / Data / System
// (~1180 lines). Those bodies depend on parent state (llm, prefs, feeds,
// markets, integrations) so splitting them cleanly means lifting that state
// into a context/provider. Low risk to leave; deferred to avoid a rushed
// refactor. Already-extracted panels live in their own files (aurora-settings,
// chart-settings, sidebar-settings, alert-notifications-settings,
// provider-registry-panel, data-sources-panel).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { confirmDialog } from "@/lib/ui/confirm-dialog";
import { notifyLlmConfigChanged } from "@/lib/agent/llm-config-events";
import {
  Cog, Bot, Globe2, Newspaper, Palette, Database,
  Trash2, RotateCcw, Check, Activity, ShieldCheck,
  ChevronRight, Cpu,
  CircleDashed, RefreshCcw, Boxes, PanelLeft, Eye as EyeIcon,
  Loader2, Search, AlertCircle, Volume2, BarChart3,
  GitBranch, Plug, Handshake, ChatsCircle, Wallet, UsersThree,
} from "@/lib/icons";
import { Button } from "@/components/ui/button";
import { GitPanel } from "./git-panel";
import { cn } from "@/lib/utils";
import { AlertNotificationsPanel } from "./alert-notifications-settings";
import { AnalyticsSettingsPanel } from "./analytics-settings";
import { SidebarSettingsPanel } from "./sidebar-settings";
import { DataSourcesPanel } from "./data-sources-panel";
import { SetupDangerZone } from "./setup-danger-zone";
import { FeaturePacksSettings } from "@/components/settings/feature-packs-settings";
import { FinancialDataConsole } from "./financial-data-console";
import { MCPSettingsPanel } from "./mcp-settings";
import { SecuritySettingsPanel } from "./security-settings";
import { MoneyGuardrailsSettings } from "./money-guardrails-settings";
import { TradingSettingsPanel } from "./trading-settings";
import { ConnectionsSettingsPanel } from "./connections-settings";
import { CustomizationSettingsPanel } from "./customization-settings";
import { GeneralSettingsPanel } from "./general-settings";
import { RiskAiSettingsPanel } from "./risk-ai-settings";
import { Panel, SectionHeader, SubSection } from "./settings-ui";
import { IconBadge } from "@/components/layout/page-header";
import { SoundSettingsPanel } from "./sound-settings";
import { VoiceSettingsPanel } from "./voice-settings";
import { ProviderRegistry, InvestorEyeFeatureMap, InvestorEyeSources } from "./provider-registry-panel";
import { AIProviderSettings } from "./ai-provider-settings";
import { AgentsSettingsPanel } from "@/components/settings/agents-settings";
import { PersonaSettingsPanel } from "@/components/settings/personas-settings";
import { NewsFeedsPanel } from "./news-feeds-panel";
import { NewsSettingsPanel } from "./news-settings-panel";
import { SearchRetrievalSettings } from "./search-retrieval-settings";
import { useModelCatalog } from "@/lib/agent/model-catalog";
import { getUserMarket, setUserMarket } from "@/lib/user-market";
import { sound } from "@/lib/sounds";
import {
  DEFAULT_GENERAL_PREFS, loadGeneralPrefs,
  type GeneralPrefs,
} from "@/lib/general-prefs";
import { getSettingsSnapshot, saveSettingsSnapshot } from "@/lib/settings-cache";


// ─── Types ──────────────────────────────────────────────────────────────

// Matches the Python runtime's `PROVIDER_CATALOG` ids exactly, so provider-
// specific construction (Anthropic, Google, Mistral, Groq) is honoured instead
// of every provider being flattened to `openai`. `auto` resolves from the
// environment; `mock` is the offline stub.
type LLMProviderName =
  | "auto" | "mock"
  | "openai" | "anthropic" | "google" | "mistral" | "groq"
  | "ollama" | "ollama-cloud" | "lmstudio" | "deepseek" | "xai"
  | "openrouter" | "opencode" | "opencode-go"
  | "together" | "perplexity" | "fireworks" | "custom";

interface LLMConfig {
  provider: LLMProviderName;
  baseUrl: string;
  apiKey: string;
  model: string;
  /** Named custom connection the active config uses (non-secret). */
  connectionId?: string;
  /** Server reports whether a key is stored; the raw key is never sent back. */
  hasApiKey?: boolean;
  /**
   * Optional operator-declared context window (tokens). Used only when the
   * runtime catalog cannot resolve one; `null`/undefined means "unknown" and
   * clears any prior declaration.
   */
  contextWindow?: number | null;
  /** Optional sampling temperature (0–2); `null`/undefined = Auto. */
  temperature?: number | null;
  /** Optional max output tokens; `null`/undefined = provider default. */
  maxTokens?: number | null;
  /**
   * Optional reasoning-effort override ("model variant"). One of
   * `low`/`medium`/`high`/`very_high`; `null`/undefined means Auto (the runtime's
   * task profile decides). Only applied to models with a native reasoning
   * control — an OpenAI-compatible endpoint without one is never force-fed it.
   */
  reasoningEffort?: "low" | "medium" | "high" | "very_high" | null;
  /** Optional coding model, same provider/key as the chat model. */
  codingModel?: string | null;
}

export type { LLMConfig };

interface NewsFeed {
  id: string;
  url: string;
  source: string;
  enabled: boolean;
}

interface AppPreferences {
  defaultMarket: string;
  refreshMs: number;
}

interface IntegrationStatus {
  llm: { provider: string; model: string; configured: boolean };
  // Only integrations actually wired at runtime — mirrors
  // `getIntegrationsStatus()` in `src/lib/settings.ts`.
  yahoo: boolean;
  fred: boolean;
  bea: boolean;
  bls: boolean;
  tradingEconomics: boolean;
  akshare: boolean;
  database: boolean;
  /** Env var → is it set. Drives honest per-provider key status. */
  envFlags: Record<string, boolean>;
}

interface MarketInfo {
  id: string;
  country: string;
  exchange: string;
  timezone: string;
  isCrypto: boolean;
  enabled: boolean;
  assetCount: number;
  pricedCount: number;
}

/**
 * The settings dialog is unmounted when closed (base-ui drops the popup), so
 * the last successful payload lives in `@/lib/settings-cache`: memory for the
 * current session plus a localStorage snapshot so even a fresh page load paints
 * the real values instantly while `/api/settings` revalidates in the background.
 */
interface SettingsSnapshot {
  llm: LLMConfig;
  feeds: NewsFeed[];
  prefs: AppPreferences;
  integrations: IntegrationStatus | null;
  markets: MarketInfo[];
}
/** Markets resolve independently of the settings snapshot; remember them too. */
let marketsCache: MarketInfo[] | null = null;

const DEFAULT_LLM: LLMConfig = {
  provider: "auto", baseUrl: "https://api.openai.com/v1", apiKey: "", model: "gpt-4o-mini",
  contextWindow: undefined,
};

const DEFAULT_PREFS: AppPreferences = { defaultMarket: "US", refreshMs: 15000 };

function readCachedSnapshot(): SettingsSnapshot | null {
  const cached = getSettingsSnapshot();
  if (!cached) return null;
  const llm = cached.llm as LLMConfig | undefined;
  const feeds = cached.newsFeeds as NewsFeed[] | undefined;
  const prefs = cached.preferences as AppPreferences | undefined;
  const integrations = cached.integrations as IntegrationStatus | null | undefined;
  if (!llm && !feeds && !prefs && !integrations) return null;
  return {
    llm: llm ? { ...llm, apiKey: "" } : DEFAULT_LLM,
    feeds: feeds ?? [],
    prefs: prefs ?? DEFAULT_PREFS,
    integrations: integrations ?? null,
    markets: marketsCache ?? [],
  };
}

let settingsCache: SettingsSnapshot | null = readCachedSnapshot();

// The Settings information architecture. Sections are named for WHAT the user
// is configuring, never for how the backend is implemented:
//   general        — startup + behavior, plus the full theme/customization tabs
//   sound          — UI audio feedback
//   voice          — read assistant replies aloud (TTS)
//   ai             — every LLM: providers, keys, base URLs, model, test
//   investor-eye   — every financial/data provider, grouped by purpose
//   market         — supported markets, exchanges, JDDS data
//   news           — RSS feeds + news providers
//   sidebar        — navigation order & shortcuts
//   alerts         — notification channels
//   data           — local chats/watchlist/portfolio + reset
//   system         — JDDS status, health, internal integrations
export type SectionId =
  | "general" | "persona" | "sound" | "voice" | "ai" | "agents" | "investor-eye" | "economics" | "market" | "news"
  | "analytics" | "sidebar" | "alerts" | "data" | "git" | "mcp" | "system" | "security"
  | "money-guardrails" | "connections" | "trading" | "risk";

/** Valid settings sub-view ids, for the palette's settings sub-pages. */
export const SETTINGS_SECTIONS: readonly SectionId[] = [
  "general", "persona", "sound", "voice", "ai", "agents", "investor-eye", "economics", "market", "news",
  "analytics", "sidebar", "alerts", "data", "git", "mcp", "system", "security",
  "money-guardrails", "connections", "trading", "risk",
] as const;

/**
 * Legacy section ids still arriving from saved subroutes / deep links. The
 * standalone `customization` section was folded into `general`; route its old
 * id to the merged section rather than rendering an empty pane.
 */
const LEGACY_SECTION_ALIASES: Record<string, SectionId> = { customization: "general" };

function resolveSection(raw: string | undefined): SectionId {
  if (!raw) return "general";
  if (raw in LEGACY_SECTION_ALIASES) return LEGACY_SECTION_ALIASES[raw];
  return (SETTINGS_SECTIONS as readonly string[]).includes(raw) ? (raw as SectionId) : "general";
}

// ─── Small building blocks ──────────────────────────────────────────────
// Panel / SectionHeader / SubSection are shared with the other settings panels
// and live in ./settings-ui.

function Chip({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border",
        className
      )}
    >
      {children}
    </span>
  );
}

function StatusDot({ ok }: { ok: boolean | undefined }) {
  return (
    <span
      className={cn(
        "inline-block w-2 h-2 rounded-full shrink-0",
        ok ? "bg-primary shadow-[0_0_8px] shadow-primary/60" : "bg-muted-foreground/40"
      )}
    />
  );
}

// ─── Main Component ─────────────────────────────────────────────────────

export function Settings({
  initialSection,
  initialAnchor,
}: {
  initialSection?: SectionId;
  /** Optional block id to scroll to once mounted (e.g. "priority"). */
  initialAnchor?: string;
} = {}) {
  const [loading, setLoading] = useState(() => !settingsCache);
  const [saving, setSaving] = useState(false);
  // Transient result toast shown in the footer status area.
  const [toast, setToast] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [section, setSection] = useState<SectionId>(resolveSection(initialSection));
  const [query, setQuery] = useState("");

  // LLM
  const [llm, setLlm] = useState<LLMConfig>(settingsCache?.llm ?? DEFAULT_LLM);
  // Declared model ids per provider, from the Python runtime catalog (a
  // convenience list, never an allowlist — a custom id is always typeable).
  const modelCatalog = useModelCatalog();

  // News
  const [feeds, setFeeds] = useState<NewsFeed[]>(settingsCache?.feeds ?? []);

  // Preferences (server-backed: default market + refresh interval)
  const [prefs, setPrefs] = useState<AppPreferences>(
    settingsCache?.prefs ?? { defaultMarket: "US", refreshMs: 15000 },
  );

  // General preferences (localStorage, presentation-layer): start screen and
  // whether destructive local-data clears need confirmation. The merged General
  // tab owns the start screen control; this shell only needs `confirmDestructive`
  // to gate the Data & Storage clears, so it reads (never writes) the prefs.
  const [general, setGeneral] = useState<GeneralPrefs>(DEFAULT_GENERAL_PREFS);
  useEffect(() => {
    setGeneral(loadGeneralPrefs());
  }, []);

  // Market data (loaded from JDDS)
  const [markets, setMarkets] = useState<MarketInfo[]>(settingsCache?.markets ?? marketsCache ?? []);
  const [selectedMarkets, setSelectedMarkets] = useState<string[]>(() => {
    const enabled = (settingsCache?.markets ?? marketsCache ?? []).filter((mk) => mk.enabled).map((mk) => mk.id);
    return enabled.length > 0 ? enabled : ["US"];
  });

  // Integrations
  const [integrations, setIntegrations] = useState<IntegrationStatus | null>(
    settingsCache?.integrations ?? null,
  );

  // Settings and markets are fetched independently: `/api/markets` can be slow
  // on a cold JDDS cache and its SQLite work used to queue ahead of the settings
  // read, making the modal sit on a skeleton. Settings is the critical path and
  // now starts first; markets fill in afterwards (and only warm the Market tab).
  const applySettings = (s: {
    llm?: LLMConfig; newsFeeds?: NewsFeed[]; preferences?: AppPreferences;
    integrations?: IntegrationStatus;
  }) => {
    if (s.llm) setLlm({ ...s.llm, apiKey: "" });
    if (s.newsFeeds) setFeeds(s.newsFeeds);
    if (s.preferences) {
      setPrefs(s.preferences);
      // Seed the shared market preference from the saved default, but never
      // override a market the user already picked on a market surface.
      if (s.preferences.defaultMarket && !getUserMarket()) {
        setUserMarket(s.preferences.defaultMarket);
      }
    }
    if (s.integrations) setIntegrations(s.integrations);
    settingsCache = {
      llm: s.llm ? { ...s.llm, apiKey: "" } : settingsCache?.llm ?? DEFAULT_LLM,
      feeds: s.newsFeeds ?? settingsCache?.feeds ?? [],
      prefs: s.preferences ?? settingsCache?.prefs ?? DEFAULT_PREFS,
      integrations: s.integrations ?? settingsCache?.integrations ?? null,
      markets: marketsCache ?? settingsCache?.markets ?? [],
    };
    // Persist the non-market payload so the next page load is instant too.
    saveSettingsSnapshot({
      llm: settingsCache.llm,
      newsFeeds: settingsCache.feeds,
      preferences: settingsCache.prefs,
      integrations: settingsCache.integrations,
    });
  };

  const applyMarkets = (list: MarketInfo[]) => {
    setMarkets(list);
    const enabled = list.filter((mk) => mk.enabled).map((mk) => mk.id);
    if (enabled.length > 0) setSelectedMarkets(enabled);
    marketsCache = list;
    if (settingsCache) settingsCache.markets = list;
  };

  const [marketsLoading, setMarketsLoading] = useState(
    () => (settingsCache?.markets ?? marketsCache ?? []).length === 0,
  );

  const fetchAll = async () => {
    if (!settingsCache) setLoading(true);
    // Settings first — the critical path. Clears the data-pending hint as soon
    // as it lands.
    try {
      const s = await fetch("/api/settings").then((r) => r.json());
      applySettings(s);
    } catch {
      setToast({ kind: "error", text: "Failed to load settings. Is the server running?" });
    } finally {
      setLoading(false);
    }
    // Markets — independent and slower; never blocks the settings paint. Fetch
    // after settings so JDDS/Prisma work cannot queue ahead of the critical read.
    setMarketsLoading((marketsCache ?? []).length === 0);
    void fetch("/api/markets")
      .then((r) => r.json())
      .then((m) => { if (Array.isArray(m.markets)) applyMarkets(m.markets); })
      .catch(() => { /* leave whatever we had */ })
      .finally(() => setMarketsLoading(false));
  };

  useEffect(() => {
    void fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Persist a partial settings payload. Every server-backed control applies
   * instantly (there is no Save button), so this is called on change — debounced
   * by the callers below to coalesce rapid keystrokes/toggles. It reports the
   * real result via the transient toast and never claims success on failure.
   */
  const persist = useCallback(async (body: Record<string, unknown>): Promise<boolean> => {
    setSaving(true);
    setToast(null);
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setToast({ kind: "error", text: data.error || "Failed to save" });
        sound.error();
        return false;
      }
      if (data.llm) setLlm({ ...data.llm, apiKey: "" });
      if (Array.isArray(data.newsFeeds)) setFeeds(data.newsFeeds);
      if (data.preferences) setPrefs(data.preferences);
      // The saved LLM config changed. Broadcast AFTER the server confirmed it so
      // every mounted composer (chat + floating dock) re-fetches the provider's
      // models instead of keeping the boot-time list.
      if (body.llm) notifyLlmConfigChanged();
      if (settingsCache) {
        if (data.llm) settingsCache.llm = { ...data.llm, apiKey: "" };
        if (Array.isArray(data.newsFeeds)) settingsCache.feeds = data.newsFeeds;
        if (data.preferences) settingsCache.prefs = data.preferences;
      }
      saveSettingsSnapshot({
        ...(data.llm ? { llm: { ...data.llm, apiKey: "" } } : {}),
        ...(Array.isArray(data.newsFeeds) ? { newsFeeds: data.newsFeeds } : {}),
        ...(data.preferences ? { preferences: data.preferences } : {}),
      });
      setToast({ kind: "ok", text: "Saved" });
      sound.success();
      return true;
    } catch {
      setToast({ kind: "error", text: "Failed to save" });
      sound.error();
      return false;
    } finally {
      setSaving(false);
    }
  }, []);

  // Debounced preference persistence (currency/timezone are local-only, but
  // theme/market/refresh live server-side). One timer coalesces rapid changes.
  const persistTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const queuePrefs = useCallback(
    (next: AppPreferences) => {
      if (persistTimer.current) clearTimeout(persistTimer.current);
      persistTimer.current = setTimeout(() => void persist({ preferences: next }), 500);
    },
    [persist],
  );
  useEffect(() => () => {
    if (persistTimer.current) clearTimeout(persistTimer.current);
  }, []);

  const updatePrefs = (patch: Partial<AppPreferences>) => {
    setPrefs((p) => {
      const next = { ...p, ...patch };
      queuePrefs(next);
      return next;
    });
  };

  // LLM config persists on change too, debounced so typing a model id or key
  // does not fire a request per keystroke. The server keeps the stored key when
  // `apiKey` is blank, so a masked field never wipes it.
  const llmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (llmTimer.current) clearTimeout(llmTimer.current);
  }, []);
  const updateLlm = (patch: Partial<LLMConfig>, debounce = 600) => {
    setLlm((p) => {
      const next = { ...p, ...patch };
      if (llmTimer.current) clearTimeout(llmTimer.current);
      // A rejected save must not leave the UI describing a configuration the
      // server does not have. Revert only if this value is still the current
      // one (a newer keystroke already replaced it).
      llmTimer.current = setTimeout(() => {
        void persist({ llm: next }).then((saved) => {
          if (!saved) {
            setLlm((current) => (current === next ? p : current));
          }
        });
      }, debounce);
      return next;
    });
  };

  // Auto-dismiss a success toast after a beat; keep errors until replaced.
  useEffect(() => {
    if (!toast || toast.kind === "error") return;
    const t = setTimeout(() => setToast(null), 2200);
    return () => clearTimeout(t);
  }, [toast]);
  const clearStorage = async (keys: string[], label: string) => {
    // Confirmation is on by default (Settings › General). A mis-click here used
    // to wipe chats/watchlist/portfolio instantly and irreversibly.
    if (general.confirmDestructive && typeof window !== "undefined") {
      const ok = await confirmDialog({ title: `Clear ${label}?`, description: "This only affects this browser and cannot be undone.", tone: "danger" });
      if (!ok) return;
    }
    keys.forEach((k) => localStorage.removeItem(k));
    setToast({ kind: "ok", text: `Cleared ${keys.length} local data item${keys.length > 1 ? "s" : ""}.` });
  };

  // Rail items carry extra `terms` so the search box can match the words a
  // user would actually type ("dark", "key", "fred", "rss") — not just labels.
  const NAV: {
    id: SectionId;
    label: string;
    desc: string;
    icon: React.ElementType;
    group: "Personalize" | "Intelligence" | "System";
    terms: string[];
  }[] = [
    { id: "general", label: "General", desc: "Startup, theme, branding & preferences", icon: Palette, group: "Personalize", terms: ["general", "start", "startup", "start screen", "home", "landing", "last screen", "resume", "market", "currency", "timezone", "refresh", "confirm", "delete", "safety", "theme", "branding", "logo", "favicon", "app name", "font", "typography", "monospace", "primary", "accent", "colors", "bullish", "bearish", "radius", "density", "glow", "aurora", "chart", "charts", "candles", "palette", "series", "animation", "preset", "language", "translate", "locale", "accessibility", "a11y", "motion", "contrast", "text size", "screen reader", "rtl", "chatbot", "chat bot", "chat page", "eclipse agent", "agent page", "conversation font", "chat font", "background", "wallpaper", "wordmark", "backdrop"] },
    { id: "persona", label: "Persona", desc: "Tone, style & chat instructions", icon: UsersThree, group: "Personalize", terms: ["persona", "personas", "personality", "system prompt", "prompt", "instructions", "tone", "voice", "style", "format", "draft", "active", "activate", "reset", "customize", "chat behavior", "behaviour"] },
    { id: "sound", label: "Sound", desc: "UI sounds & feedback", icon: Volume2, group: "Personalize", terms: ["sound", "sounds", "audio", "volume", "click", "tick", "toggle", "feedback", "notification", "voice", "pack", "mute", "muted", "silence", "chime", "beep"] },
    { id: "voice", label: "Voice", desc: "Read aloud & dictation", icon: Volume2, group: "Personalize", terms: ["voice", "tts", "text to speech", "text-to-speech", "read aloud", "read out loud", "speak", "speech", "narration", "audio", "rate", "pitch", "auto speak", "accessibility", "dictation", "microphone", "mic", "whisper", "transcribe", "transcription", "voice input"] },
    { id: "sidebar", label: "Sidebar & Navigation", desc: "Menu & shortcuts", icon: PanelLeft, group: "Personalize", terms: ["sidebar", "navigation", "menu", "shortcuts", "keys", "reorder"] },

    { id: "ai", label: "AI / LLM", desc: "Models, keys & testing", icon: Bot, group: "Intelligence", terms: ["ai", "llm", "agent", "model", "openai", "anthropic", "ollama", "groq", "gemini", "api key", "base url", "chat"] },
    { id: "agents", label: "Agents", desc: "Custom agent presets", icon: Bot, group: "Intelligence", terms: ["agent", "agents", "preset", "presets", "prompt", "system prompt", "tools", "workflow", "graph"] },
    { id: "investor-eye", label: "Investor Eye", desc: "World-state sources", icon: EyeIcon, group: "Intelligence", terms: ["investor eye", "world state", "gdelt", "usgs", "firms", "events", "geopolitics", "map", "sources"] },
    { id: "economics", label: "Economics", desc: "Macro & calendar providers", icon: Activity, group: "Intelligence", terms: ["economics", "macro", "fred", "bea", "bls", "calendar", "indicators", "yield", "inflation"] },
    { id: "market", label: "Market Data", desc: "Discover, charts & markets", icon: Globe2, group: "Intelligence", terms: ["market", "screener", "stocks", "crypto", "prices", "fundamentals", "polygon", "finnhub", "alpha vantage", "coin"] },
    { id: "news", label: "News", desc: "Cards, alerts, feeds & providers", icon: Newspaper, group: "Intelligence", terms: ["news", "rss", "feeds", "headlines", "sentiment", "finnhub", "cards", "layout", "columns", "density", "summary", "popup", "pop-up", "toast", "notification", "desktop", "sound", "alert", "watchlist", "breaking", "high impact", "releases", "customize"] },
    { id: "analytics", label: "Analytics", desc: "Token usage & runs", icon: BarChart3, group: "Intelligence", terms: ["analytics", "usage", "tokens", "token", "consumption", "cost", "spend", "runs", "steps", "agent", "llm", "metrics", "stats"] },
    { id: "mcp", label: "MCP Servers", desc: "External tool connections", icon: Plug, group: "Intelligence", terms: ["mcp", "model context protocol", "servers", "tools", "external", "stdio", "integration", "plugins"] },
    { id: "connections", label: "Connections", desc: "Remote control over Telegram, email & more", icon: ChatsCircle, group: "Intelligence", terms: ["connections", "channels", "remote", "telegram", "discord", "email", "slack", "whatsapp", "webhook", "link", "binding", "gateway", "control"] },

    { id: "alerts", label: "Alerts", desc: "Notification channels", icon: ShieldCheck, group: "System", terms: ["alerts", "notifications", "channels", "email", "webhook", "telegram", "discord", "slack"] },
    { id: "data", label: "Data & Storage", desc: "Local data & reset", icon: Database, group: "System", terms: ["data", "storage", "clear", "reset", "chats", "watchlist", "portfolio", "cache"] },
    { id: "git", label: "Git", desc: "Plan, commit & push", icon: GitBranch, group: "System", terms: ["git", "commit", "push", "branch", "stage", "diff", "version control", "repo"] },
    { id: "security", label: "Security", desc: "Approvals, capabilities & audit", icon: ShieldCheck, group: "System", terms: ["security", "approvals", "permissions", "capabilities", "audit", "floor", "policy", "network", "secrets", "sandbox", "gateway"] },
    { id: "money-guardrails", label: "Money Guardrails", desc: "Scam, impaired, crisis & limits", icon: Wallet, group: "System", terms: ["money", "guardrails", "scam", "coercion", "fraud", "impaired", "crisis", "panic", "pause", "spend", "limit", "velocity", "transfer", "withdraw", "payment", "confirm"] },
    { id: "trading", label: "Broker connection", desc: "MetaTrader 5 broker & execution", icon: Handshake, group: "System", terms: ["trading", "broker", "connection", "mt5", "metatrader", "forex", "cfd", "account", "positions", "orders", "live", "demo", "arm", "kill", "sync", "fills"] },
    { id: "risk", label: "Risk & AI", desc: "Not advice, agent limits & acknowledgment", icon: ShieldCheck, group: "System", terms: ["risk", "disclaimer", "advice", "ai", "agent", "acknowledgement", "terms", "legal", "responsibility", "trading", "loss"] },
    { id: "system", label: "System", desc: "Health & integrations", icon: Cpu, group: "System", terms: ["system", "health", "database", "jdds", "integrations", "status", "diagnostics"] },
  ];

  const GROUPS: Array<"Personalize" | "Intelligence" | "System"> = ["Personalize", "Intelligence", "System"];

  const needle = query.trim().toLowerCase();
  const visibleNav = useMemo(() => {
    if (!needle) return NAV;
    return NAV.filter((item) => {
      const hay = [item.label, item.desc, ...item.terms].join(" ").toLowerCase();
      return hay.includes(needle);
    });
  }, [needle, NAV]);

  // Keyboard navigation: `/` focuses the search, ↑/↓ move between sections
  // (within the current search), Esc clears/blurs. Ignored while typing in a
  // field so it never hijacks input.
  const searchRef = useRef<HTMLInputElement | null>(null);
  const modalRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (e.key === "Escape" && typing) {
        (target as HTMLInputElement).blur();
        return;
      }
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !typing) {
        const list = visibleNav.length > 0 ? visibleNav : NAV;
        const idx = list.findIndex((i) => i.id === section);
        const nextIdx = e.key === "ArrowDown"
          ? (idx + 1) % list.length
          : (idx - 1 + list.length) % list.length;
        e.preventDefault();
        setSection(list[nextIdx].id);
      }
    };
    const node = modalRef.current;
    node?.addEventListener("keydown", onKey);
    return () => node?.removeEventListener("keydown", onKey);
    // `loading` matters: the ref is null while the skeleton renders, so the
    // effect must re-run once the real shell (and its ref) has mounted.
  }, [visibleNav, section, loading]);

  // Scroll to a deep-linked block once content has rendered.
  useEffect(() => {
    if (loading || !initialAnchor) return;
    const t = setTimeout(() => {
      modalRef.current?.querySelector(`[data-anchor="${initialAnchor}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 150);
    return () => clearTimeout(t);
  }, [loading, initialAnchor]);

  // Paint the full shell immediately from the snapshot (or defaults on the very
  // first visit) and let `/api/settings` fill it in — a blocking skeleton made
  // the modal feel slow even when the payload was already cached.

  return (
    <div ref={modalRef} className="flex h-full w-full overflow-hidden" tabIndex={-1}>
      {/* Left nav rail — same black/charcoal `sidebar-root` surface as the main
          app sidebar so the chrome stays consistent (never a grey rail). */}
      <aside className="sidebar-root hidden md:flex w-60 shrink-0 flex-col glass-card-strong">
        <div className="flex items-center gap-3 px-5 h-16 border-b border-border/10">
          <IconBadge icon={Cog} size="sm" />
          <div>
            <p className="font-serif text-sm font-semibold leading-none text-foreground">Settings</p>
            <p className="text-[10px] text-muted-foreground mt-1 tracking-wide">BlackEclipse Console</p>
          </div>
        </div>

        {/* Rail search — answers "where do I change X?" without scanning. */}
        <div className="px-3 pt-3">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search settings…  /"
              className="w-full h-9 rounded-xl border border-border/30 bg-transparent pl-8 pr-2.5 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/40"
            />
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto scrollbar-hide p-3 space-y-1.5">
          {visibleNav.length === 0 && (
            <p className="px-3 py-6 text-[10px] text-muted-foreground text-center">
              No settings match “{query}”.
            </p>
          )}
          {GROUPS.map((group) => {
            const items = visibleNav.filter((i) => i.group === group);
            if (items.length === 0) return null;
            return (
              <div key={group} className="space-y-1.5">
                {!needle && (
                  <p className="px-3 pt-2 pb-0.5 text-[9px] uppercase tracking-[0.18em] font-bold text-muted-foreground/60">
                    {group}
                  </p>
                )}
                {items.map((item) => {
                  const Icon = item.icon;
                  const active = section === item.id;
                  return (
                    <button
                      key={item.id}
                      data-sound="tick"
                      onClick={() => setSection(item.id)}
                      className={cn(
                        "w-full flex items-center justify-between gap-2 px-3 py-2 rounded-xl text-xs font-medium transition-all cursor-pointer border",
                        active
                          ? "bg-white/[0.08] text-foreground border-border/30"
                          : "text-muted-foreground border-transparent hover:bg-accent/50 hover:text-foreground hover:border-border/20"
                      )}
                    >
                      <span className="flex items-center gap-3 min-w-0">
                        <Icon size={15} className={cn("shrink-0", active ? "text-foreground" : "text-muted-foreground")} />
                        <span className="flex flex-col items-start text-left leading-tight">
                          <span className="flex items-center gap-1.5">
                            {item.label}
                          </span>
                          <span className="text-[9px] text-muted-foreground font-normal tracking-wide hidden lg:block">{item.desc}</span>
                        </span>
                      </span>
                      {active && <ChevronRight size={13} className="text-foreground shrink-0" />}
                    </button>
                  );
                })}
              </div>
            );
          })}
        </nav>
      </aside>

      {/* Right content */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex-1 overflow-y-auto scrollbar-hide p-5 md:p-8">
          {/* Persona is a master–detail surface (library rail + editor) and
              needs a wider canvas than the single-column settings pages. */}
          <div className={cn("mx-auto", section === "persona" ? "max-w-5xl" : "max-w-3xl")}>
            {/* Mobile: a search field + horizontal, scrollable pill row
                (thumb-friendly) instead of the desktop rail, so searching and
                switching sections both work on phones. */}
            <div className="md:hidden mb-4 space-y-3">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search settings…"
                  className="w-full h-9 rounded-xl border border-border/30 bg-transparent pl-8 pr-2.5 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/40"
                />
              </div>
              <div className="-mx-5 px-5 overflow-x-auto scrollbar-hide">
                <div className="flex gap-2 w-max">
                  {visibleNav.length === 0 && (
                    <p className="px-1 py-2 text-[10px] text-muted-foreground">
                      No settings match “{query}”.
                    </p>
                  )}
                  {visibleNav.map((item) => {
                    const Icon = item.icon;
                    const active = section === item.id;
                    return (
                      <button
                        key={item.id}
                        data-sound="tick"
                        onClick={() => setSection(item.id)}
                        className={cn(
                          "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1.5 text-[11px] font-medium transition-colors cursor-pointer",
                          active
                            ? "bg-white/[0.08] text-foreground border-border/30"
                            : "text-muted-foreground border-border/30 hover:text-foreground hover:border-border/60"
                        )}
                      >
                        <Icon size={13} className={active ? "text-foreground" : "text-muted-foreground"} />
                        {item.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>

            {section === "general" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Palette}
                  title="General"
                  description="How the app opens and behaves, plus centralized theming — branding, typography, colors, radius, density and motion. Theme changes apply live across the whole app through shared design tokens."
                />
                <CustomizationSettingsPanel
                  generalTab={
                    <GeneralSettingsPanel
                      markets={markets}
                      prefs={prefs}
                      onPrefsChange={updatePrefs}
                      onGeneralChange={setGeneral}
                    />
                  }
                />
              </div>
            )}

            {section === "sound" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Volume2}
                  title="UI Sounds"
                  description="Terminal-style audio feedback for navigation, actions and alerts. Pick a voice pack, fine-tune every cue, or upload your own clips — all stored on this device."
                />
                <SoundSettingsPanel />
              </div>
            )}

            {section === "voice" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Volume2}
                  title="Voice"
                  description="Talk to Eclipse and hear it answer. Dictate prompts with Whisper or your browser's speech recognition, and read assistant replies aloud with your browser's own voices — choose the engine, language and pacing here."
                />
                <VoiceSettingsPanel />
              </div>
            )}

            {/* ── Persona ─────────────────────────────────────────────────
                A bounded, append-only overlay on the built-in prompt. Personas
                change voice, never tools or permissions; one is active. */}
            {section === "persona" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={UsersThree}
                  title="Persona"
                  description="Shape how Eclipse talks without changing what it can do. A persona appends tone, structure and emphasis guidance after the fixed Eclipse prompt; tools, permissions and safety rules stay untouched. Keep several and activate one."
                />
                <PersonaSettingsPanel />
              </div>
            )}

            {/* ── AI / LLM ────────────────────────────────────────────────
                Every LLM concern lives here — providers, keys, base URLs,
                model selection, active provider, test, status. Nothing else
                in Settings configures an LLM. */}
            {section === "ai" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Bot}
                  title="AI / LLM"
                  description="Choose the model provider that powers Chat, Research and the Investor Eye analyst. Keys, base URLs and model selection all live here — never mixed with financial/data APIs."
                />

                <AIProviderSettings llm={llm} updateLlm={updateLlm} modelCatalog={modelCatalog} />
              </div>
            )}

            {/* ── Agents ──────────────────────────────────────────────────
                Custom agent presets for Graph Studio. A preset is a pinned
                prompt + explicit tool allowlist; the runtime owns validation. */}
            {section === "agents" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Bot}
                  title="Agents"
                  description="Create your own agent presets for Graph Studio. Each preset pins a system prompt, model step bound and an explicit tool list; a workflow node pins the preset version it was saved against."
                />
                <AgentsSettingsPanel />
              </div>
            )}

            {/* ── Investor Eye ────────────────────────────────────────────
                Only what powers the Investor Eye world-state page: its own
                keyless event sources (GDELT/USGS/FIRMS) and the feature map.
                Macro, market and news providers live in their own sections. */}
            {section === "investor-eye" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={EyeIcon}
                  title="Investor Eye"
                  description="The sources behind the Investor Eye world-state map, and a map of which provider powers each feature. Providers are configured in their own sections — tap a chip to jump there."
                />

                <SubSection
                  icon={EyeIcon}
                  title="What powers each feature"
                  description="Which provider backs each Investor Eye capability, and whether it is ready. Tap a chip to jump to its home section."
                >
                  <InvestorEyeFeatureMap />
                </SubSection>

                <div className="mt-6">
                  <SubSection
                    icon={Plug}
                    title="Event sources"
                    description="The keyless world-state feeds behind the map — no configuration required."
                  >
                    <InvestorEyeSources />
                  </SubSection>
                </div>
              </div>
            )}

            {/* ── Economics ───────────────────────────────────────────────
                Only macro/economic providers. */}
            {section === "economics" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Activity}
                  title="Economics"
                  description="Providers that power the Economics calendar, indicators and time-series explorer—macro series, national accounts and central-bank data."
                />
                <SubSection
                  icon={Activity}
                  title="Macro & calendar providers"
                  description="Grouped by kind. Add a key to light up more series and wider coverage."
                >
                  <ProviderRegistry home="economics" title="Economics & macro providers" />
                </SubSection>
              </div>
            )}

            {section === "analytics" && <AnalyticsSettingsPanel />}

            {section === "sidebar" && <SidebarSettingsPanel />}

            {section === "market" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Globe2}
                  title="Market Data"
                  description="Providers that power Market Discover, charts and Fundamentals. Choose which markets and exchanges loaded in JDDS are surfaced, and review the price-source fallback chain."
                />

                <SubSection
                  icon={Globe2}
                  title="Markets & exchanges"
                  description="Toggle which markets loaded in JDDS appear across Market Discover, charts and search."
                >
                  {markets.length === 0 ? (
                    <Panel className="p-8 text-center">
                      <Boxes className="mx-auto text-muted-foreground w-8 h-8 mb-3" />
                      <p className="text-xs text-muted-foreground">
                        {marketsLoading
                          ? "Loading markets from JDDS…"
                          : "No markets found in JDDS yet. Import or scrape market data to populate the database."}
                      </p>
                    </Panel>
                  ) : (
                    <div className="space-y-2.5">
                      {markets.map((m) => {
                        const active = selectedMarkets.includes(m.id);
                        return (
                          <button
                            key={m.id}
                            onClick={() =>
                              setSelectedMarkets((prev) =>
                                prev.includes(m.id) ? prev.filter((x) => x !== m.id) : [...prev, m.id]
                              )
                            }
                            className={cn(
                              "w-full flex items-center justify-between gap-3 py-3.5 px-4 rounded-xl border transition-all cursor-pointer text-left group",
                              active
                                ? "bg-primary/10 border-primary/40 shadow-sm shadow-primary/10"
                                : "glass-card-subtle border-border/20 hover:border-border/40 hover:bg-accent/30"
                            )}
                          >
                            <div className="flex items-center gap-3 min-w-0">
                              <div
                                className={cn(
                                  "w-5 h-5 rounded-md border-2 shrink-0 flex items-center justify-center transition-colors",
                                  active ? "border-primary bg-primary/20" : "border-border/50"
                                )}
                              >
                                {active && <Check className="text-primary w-3 h-3" />}
                              </div>
                              <div className="min-w-0">
                                <span className="text-xs font-semibold text-foreground flex items-center gap-2">
                                  {m.country}
                                  {m.isCrypto && (
                                    <Chip className="text-primary bg-primary/10 border-primary/20 uppercase tracking-widest">Crypto</Chip>
                                  )}
                                </span>
                                <span className="text-[10px] text-muted-foreground block truncate font-mono">
                                  {m.exchange} · {m.assetCount} assets · {m.pricedCount} priced
                                </span>
                              </div>
                            </div>
                            <Chip
                              className={cn(
                                "shrink-0",
                                active
                                  ? "text-primary bg-primary/10 border-primary/20"
                                  : "text-muted-foreground bg-muted/40 border-transparent"
                              )}
                            >
                              {active ? "Showing" : "Hidden"}
                            </Chip>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </SubSection>

                <div className="mt-6 space-y-4">
                  <SubSection
                    icon={Plug}
                    title="Market data providers"
                    description="Price, fundamentals and crypto sources. Add a key to widen coverage and rate limits."
                  >
                    <ProviderRegistry home="market" title="Market Discover providers" />
                  </SubSection>

                  <SubSection
                    icon={ShieldCheck}
                    title="Financial Data Gateway"
                    description="Provider capabilities, routing, rate limits and recorded data-usage terms. One registry the agents and this console both read."
                  >
                    <FinancialDataConsole />
                  </SubSection>

                  <SubSection icon={Activity} title="Price-source priority & fallback">
                    <Panel className="p-5 md:p-6" data-anchor="priority">
                      <p className="text-[10px] text-muted-foreground leading-relaxed">
                        JDDS serves stored prices first, then the built-in scraper chain escalates automatically:
                        TradingView scanner → HTTP/scraper → Playwright browser → optional yfinance bridge.
                        Configure keys for alternative sources above to widen coverage and rate limits.
                      </p>
                    </Panel>
                  </SubSection>

                  <SearchRetrievalSettings />

                  <SubSection icon={Database} title="Custom data sources">
                    <DataSourcesPanel />
                  </SubSection>
                </div>
              </div>
            )}

            {section === "news" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Newspaper}
                  title="News"
                  description="How news looks and reaches you — card layout on the News tab, pop-ups, desktop notifications and sound for high-impact stories — plus the RSS feeds and news APIs that power the bulletin and sentiment."
                />
                <NewsSettingsPanel />
                <div className="mt-4">
                  <NewsFeedsPanel
                    feeds={feeds}
                    saving={saving}
                    onSave={(next) => {
                      setFeeds(next);
                      void persist({ newsFeeds: next });
                    }}
                  />
                </div>

                <div className="mt-4">
                  <ProviderRegistry home="news" title="News providers" />
                </div>
              </div>
            )}

            {section === "alerts" && <AlertNotificationsPanel />}

            {section === "data" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Database}
                  title="Data & Storage"
                  description="Add optional components, and clear locally stored chats, watchlists, or portfolio data from this device."
                />
                <Panel className="p-5 md:p-6">
                  <div className="grid gap-2 sm:grid-cols-2">
                    <Button variant="outline" size="sm" className="cursor-pointer justify-start rounded-xl" onClick={() => clearStorage(["chats_guest"], "all chats")}>
                      <Trash2 className="w-4 h-4 mr-1" /> Clear Chats
                    </Button>
                    <Button variant="outline" size="sm" className="cursor-pointer justify-start rounded-xl" onClick={() => clearStorage(["watchlist_guest"], "your watchlist")}>
                      <Trash2 className="w-4 h-4 mr-1" /> Clear Watchlist
                    </Button>
                    <Button variant="outline" size="sm" className="cursor-pointer justify-start rounded-xl" onClick={() => clearStorage(["portfolio_tx_guest", "portfolio_alerts_guest"], "your portfolio")}>
                      <Trash2 className="w-4 h-4 mr-1" /> Clear Portfolio
                    </Button>
                    <Button variant="outline" size="sm" className="cursor-pointer justify-start rounded-xl text-rose-400 hover:text-rose-300 hover:border-rose-500/40 hover:bg-rose-500/10" onClick={() => clearStorage(["chats_guest", "watchlist_guest", "portfolio_tx_guest", "portfolio_alerts_guest"], "all local data")}>
                      <RotateCcw className="w-4 h-4 mr-1" /> Reset All Local Data
                    </Button>
                  </div>
                  <p className="text-[10px] text-muted-foreground mt-4 flex items-center gap-1.5">
                    <CircleDashed className="w-3 h-3" /> Cleared data is removed from this browser only.
                  </p>
                </Panel>
                <FeaturePacksSettings />
                <SetupDangerZone />
              </div>
            )}

            {/* ── Git ─────────────────────────────────────────────────────
                Plan → review → commit → push. Commits and pushes are explicit
                actions; the server refuses secret-looking paths. */}
            {section === "git" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={GitBranch}
                  title="Git"
                  description="Review changes, stage what you mean, write a message (or draft one from the real diff), commit, then push. Push is always manual."
                />
                <GitPanel />
              </div>
            )}

            {/* ── MCP servers ─────────────────────────────────────────────
                The user-directed extension surface: connect local stdio MCP
                servers and select which read-only tools the agent may use. */}
            {section === "mcp" && <MCPSettingsPanel />}

            {/* ── System ──────────────────────────────────────────────────
                Technical/internal only: database, health and the status of
                the services BlackEclipse itself runs. No user-facing provider
                configuration lives here. */}
            {section === "security" && <SecuritySettingsPanel />}
            {section === "money-guardrails" && <MoneyGuardrailsSettings />}
            {section === "trading" && <TradingSettingsPanel />}
            {section === "risk" && <RiskAiSettingsPanel />}
            {section === "connections" && <ConnectionsSettingsPanel />}
            {section === "system" && (
              <div className="animate-fade-in-up">
                <SectionHeader
                  icon={Cpu}
                  title="System & Integrations"
                  description="Technical status only — JDDS/database health and the internal services BlackEclipse runs. Providers are configured in their own sections."
                />
                <Panel className="divide-y divide-border/10">
                  {[
                    { label: "Database (JDDS)", ok: integrations?.database, detail: "Connected" },
                    { label: "AI Model", ok: integrations?.llm.configured, detail: integrations ? `${integrations.llm.provider} · ${integrations.llm.model}` : "—" },
                    { label: "Investor Eye runtime", ok: true, detail: "Scheduler active" },
                    { label: "News ingestion", ok: true, detail: `${feeds.filter((f) => f.enabled).length} feeds enabled` },
                  ].map((row) => (
                    <div key={row.label} className="flex items-center justify-between gap-3 px-5 py-3.5 hover:bg-accent/20 transition-colors">
                      <span className="text-xs text-foreground">{row.label}</span>
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] text-muted-foreground font-mono">{row.detail}</span>
                        <StatusDot ok={row.ok} />
                      </div>
                    </div>
                  ))}
                </Panel>
              </div>
            )}
          </div>
        </div>

        {/* Status strip — there is no Save button: every control applies
            instantly. This line reports the real save state (or the last
            error) so the user always knows whether their change persisted. */}
        <div className="shrink-0 border-t border-border/10 glass-card px-4 md:px-8 py-3 flex flex-wrap items-center justify-between gap-2">
          {loading ? (
            <span className="text-xs text-muted-foreground flex items-center gap-1.5">
              <Loader2 className="w-3.5 h-3.5 animate-spin text-primary" /> Loading settings…
            </span>
          ) : toast ? (
            <span
              className={cn(
                "text-xs flex items-center gap-1.5 min-w-0",
                toast.kind === "error" ? "text-rose-400" : "text-primary"
              )}
            >
              {toast.kind === "error" ? <AlertCircle className="w-3.5 h-3.5 shrink-0" /> : <Check className="w-3.5 h-3.5 shrink-0" />}
              <span className="truncate">{toast.text}</span>
            </span>
          ) : saving ? (
            <span className="text-xs text-muted-foreground flex items-center gap-1.5">
              <Loader2 className="w-3.5 h-3.5 animate-spin text-primary" /> Saving…
            </span>
          ) : (
            <span className="text-xs text-muted-foreground flex items-center gap-1.5">
              <Activity className="w-3.5 h-3.5 text-primary" /> Changes save automatically.
            </span>
          )}
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="cursor-pointer rounded-xl"
              onClick={() => { setToast(null); void fetchAll(); }}
              title="Reload from the server"
            >
              <RefreshCcw className="w-4 h-4 mr-1" /> Reload
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
