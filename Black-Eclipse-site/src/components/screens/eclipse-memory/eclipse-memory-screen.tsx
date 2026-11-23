"use client";

// ── Eclipse Memory — the Memory Control Room (Phase 3-M) ────────────────────
// Tabs come from MEMORY_TABS (src/lib/app-screens.ts) — the same list the
// agent's surface map reads. Reuses the Skills
// Control Room primitives (frosted glass, tab bar, badges) — no new visual
// language. All counts are real; no fabricated activity.

import { useCallback, useEffect, useRef, useState } from "react";
import { saveBlob } from "@/lib/desktop/download";
import { promptDialog } from "@/lib/ui/confirm-dialog";
import {
  Activity, AlertCircle, Brain, Database, FileText, MessageSquare, Plus,
  RefreshCcw, Search, Settings2, ShieldCheck, Sparkles, Trash2, Wrench, X, type LucideIcon,
} from "@/lib/icons";
import { MEMORY_TABS, screenTab, type MemoryTabId } from "@/lib/app-screens";
import { TabBar } from "@/components/ui/tab-bar";
import { useSubroute } from "@/hooks/use-subroute";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ServerPaginationFooter } from "@/components/ui/pagination";
import { cn } from "@/lib/utils";
import {
  memoryHealth, memoryOverview, listMemoriesPage, getMemory, createMemory,
  archiveMemory, reviewMemory, restoreMemory, renewMemory, memoryLifecycle, memoryRetention,
  memoryUsageReport,
  listCorrectionsPage,
  listMistakesPage, retrieveMemories,
  clientIsOperator,
  exportMemory, importMemory, backupMemory, memoryDatabaseHealth, resetMemory,
  listSuggestionsPage, acceptSuggestion, dismissSuggestion, type MemorySuggestion,
  memoryConflictReview, resolveMemoryConflict,
  listCompaction, detectCompaction, acceptCompaction, rejectCompaction,
  memoryProfile, rebuildProfile,
  sessionSearch, sessionSearchHealth, sessionOpen, sessionDelete,
  listChatSessions, listChatSessionsPage, getChatSession, updateChatSession, forkChatSession, deleteChatSession, exportChatSession, getSessionSummary,
  memoryProviderStatus,
  learningReviewHealth, listLearningReviewsPage, runLearningReview, decideLearningReview,
  getMemoryFiles, regenerateMemoryFiles, previewMarkdownImport, importMarkdown, restoreMemoryFiles,
  editMemoryFile, listFileHistory, rollbackMemoryFile, type FileEditOperation, type FileRevision,
} from "@/lib/harness/clients/eclipse-memory-browser";
import {
  EmptyBox, ErrorBox, FilterRow, Header, MEMORY_STATUSES, MEMORY_TYPES, Meta, Muted,
  OriginBadge, Section, SegmentedTabs, Spinner, StatusPill, TypeBadge,
} from "./memory-ui";
import { MemoryDrawer, MemoryModal } from "./memory-overlay";
import { Preferences } from "./preferences-panel";
import { Policies, Profiles, Providers } from "./settings-panel";
import type {
  CorrectionRecord, EclipseMemoryHealth, MemoryDatabaseHealth, MemoryDetail,
  MemoryExportDocument, MemoryLifecycleView,
  MemoryLifecycleResponse, MemoryOverview,
  MemoryProfileResponse,
  MemoryRetentionStatus,
  MemoryUsageReport,
  MemoryRecord, MemoryRetrievalDiagnostics, MistakeRecord, MemoryConflictReview, CompactionProposal,
  SessionSearchHealth, SessionSearchHit,
  ChatSessionRecord, MemoryProviderStatus,
  LearningReviewHealth, LearningReviewRecord,
} from "@/lib/harness/eclipse-memory-types";

type Tab = MemoryTabId;
type SectionMap = {
  facts: "browse" | "lifecycle";
  sessions: "list" | "search";
  review: "suggestions" | "reviews";
  preferences: "learned" | "snapshot";
  settings: "policies" | "providers" | "profiles";
  maintenance: "diagnostics" | "files" | "health";
};

const DEFAULT_SECTIONS: SectionMap = {
  facts: "browse",
  sessions: "list",
  review: "suggestions",
  preferences: "learned",
  settings: "policies",
  maintenance: "diagnostics",
};

// Legacy deep links must not 404 now that 16 tabs became 7. This is the single
// alias → (tab, section) map; `MEMORY_TABS` keeps the aliases for the palette.
const SUBROUTE_TARGET: Record<string, { tab: Tab; section?: keyof SectionMap; value?: string }> = {
  overview: { tab: "overview" },
  facts: { tab: "facts", section: "facts", value: "browse" },
  memories: { tab: "facts", section: "facts", value: "browse" },
  explorer: { tab: "facts", section: "facts", value: "browse" },
  lifecycle: { tab: "facts", section: "facts", value: "lifecycle" },
  sessions: { tab: "sessions", section: "sessions", value: "list" },
  history: { tab: "sessions", section: "sessions", value: "search" },
  search: { tab: "sessions", section: "sessions", value: "search" },
  "session-search": { tab: "sessions", section: "sessions", value: "search" },
  review: { tab: "review", section: "review", value: "suggestions" },
  suggestions: { tab: "review", section: "review", value: "suggestions" },
  reviews: { tab: "review", section: "review", value: "reviews" },
  learning: { tab: "review", section: "review", value: "reviews" },
  preferences: { tab: "preferences", section: "preferences", value: "learned" },
  prefs: { tab: "preferences", section: "preferences", value: "learned" },
  profile: { tab: "preferences", section: "preferences", value: "snapshot" },
  context: { tab: "preferences", section: "preferences", value: "snapshot" },
  settings: { tab: "settings", section: "settings", value: "policies" },
  policies: { tab: "settings", section: "settings", value: "policies" },
  providers: { tab: "settings", section: "settings", value: "providers" },
  profiles: { tab: "settings", section: "settings", value: "profiles" },
  maintenance: { tab: "maintenance", section: "maintenance", value: "diagnostics" },
  activity: { tab: "maintenance", section: "maintenance", value: "diagnostics" },
  diagnostics: { tab: "maintenance", section: "maintenance", value: "diagnostics" },
  files: { tab: "maintenance", section: "maintenance", value: "files" },
  health: { tab: "maintenance", section: "maintenance", value: "health" },
  "health-export": { tab: "maintenance", section: "maintenance", value: "health" },
};

// Labels + ids come from `MEMORY_TABS` (the same list the agent's surface map
// reads); only the icons are screen-local presentation.
const TAB_ICONS: Record<Tab, LucideIcon> = {
  overview: Brain, facts: Database, sessions: MessageSquare, review: Activity,
  preferences: Sparkles, settings: Settings2, maintenance: ShieldCheck,
};

const STATUSES = MEMORY_STATUSES;

/** Confidence as a percentage, tolerant of a null/undefined value.
 *
 * A memory row can lack confidence (a partial/suggestion-shaped object), and the
 * previous `(confidence * 100)` threw "Cannot read properties of null" when the
 * user typed in the memory search box (the reported `None` conversion error).
 * Render an honest placeholder instead of crashing the screen. */
function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(0)}%`;
}

export function EclipseMemoryScreen({ isOperator = clientIsOperator() }: { isOperator?: boolean }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [sections, setSections] = useState<SectionMap>(DEFAULT_SECTIONS);
  const setSection = <K extends keyof SectionMap>(key: K, value: SectionMap[K]) =>
    setSections((current) => ({ ...current, [key]: value }));

  // Deep links land on the matching tab AND internal section. The shell strips
  // the `memory.` prefix before dispatching, so pass the slug as-is. Unknown
  // slugs fall back to the tab list's alias resolution.
  useSubroute("memory", (req) => {
    const slug = req.subroute ?? "";
    const target = SUBROUTE_TARGET[slug];
    if (target) {
      setTab(target.tab);
      if (target.section && target.value) {
        setSections((current) => ({ ...current, [target.section!]: target.value }));
      }
      return;
    }
    const def = screenTab(MEMORY_TABS, slug);
    if (def) setTab(def.id);
  });

  return (
    <div className="w-full px-4 py-6 md:px-8 pb-24">
      <TabBar
        items={MEMORY_TABS.map((t) => ({ id: t.id, label: t.label, icon: TAB_ICONS[t.id] }))}
        activeId={tab}
        onChange={(id) => setTab(id as Tab)}
        variant="subtle"
        wrap
      />
      {tab === "overview" && <Overview />}
      {tab === "facts" && (
        <>
          <SegmentedTabs
            items={[{ id: "browse", label: "Browse" }, { id: "lifecycle", label: "Lifecycle" }]}
            activeId={sections.facts}
            onChange={(id) => setSection("facts", id as SectionMap["facts"])}
          />
          {sections.facts === "browse" ? <Memories isOperator={isOperator} /> : <Lifecycle isOperator={isOperator} />}
        </>
      )}
      {tab === "sessions" && (
        <>
          <SegmentedTabs
            items={[{ id: "list", label: "Sessions" }, { id: "search", label: "Search history" }]}
            activeId={sections.sessions}
            onChange={(id) => setSection("sessions", id as SectionMap["sessions"])}
          />
          {sections.sessions === "list" ? <Sessions isOperator={isOperator} /> : <History />}
        </>
      )}
      {tab === "review" && (
        <>
          <SegmentedTabs
            items={[{ id: "suggestions", label: "Suggestions" }, { id: "reviews", label: "Learning reviews" }]}
            activeId={sections.review}
            onChange={(id) => setSection("review", id as SectionMap["review"])}
          />
          {sections.review === "suggestions" ? <Suggestions isOperator={isOperator} /> : <Reviews isOperator={isOperator} />}
        </>
      )}
      {tab === "preferences" && (
        <>
          <SegmentedTabs
            items={[{ id: "learned", label: "Learned preferences" }, { id: "snapshot", label: "Context snapshot" }]}
            activeId={sections.preferences}
            onChange={(id) => setSection("preferences", id as SectionMap["preferences"])}
          />
          {sections.preferences === "learned" ? <Preferences isOperator={isOperator} /> : <Profile isOperator={isOperator} />}
        </>
      )}
      {tab === "settings" && (
        <>
          <SegmentedTabs
            items={[{ id: "policies", label: "Policies" }, { id: "providers", label: "Providers" }, { id: "profiles", label: "Profiles" }]}
            activeId={sections.settings}
            onChange={(id) => setSection("settings", id as SectionMap["settings"])}
          />
          {sections.settings === "policies" && <Policies isOperator={isOperator} />}
          {sections.settings === "providers" && <Providers />}
          {sections.settings === "profiles" && <Profiles isOperator={isOperator} />}
        </>
      )}
      {tab === "maintenance" && (
        <>
          <SegmentedTabs
            items={[{ id: "diagnostics", label: "Diagnostics" }, { id: "files", label: "Files" }, { id: "health", label: "Health & data" }]}
            activeId={sections.maintenance}
            onChange={(id) => setSection("maintenance", id as SectionMap["maintenance"])}
          />
          {sections.maintenance === "diagnostics" && <ActivityView />}
          {sections.maintenance === "files" && <FilesTab isOperator={isOperator} />}
          {sections.maintenance === "health" && <Health isOperator={isOperator} />}
        </>
      )}
    </div>
  );
}

// ── Lifecycle (active / suggestions / expired / superseded / conflicts) ──────

const LIFECYCLE_VIEWS: Array<{ id: MemoryLifecycleView; label: string; hint: string }> = [
  { id: "active", label: "Active", hint: "Usable and eligible for recall." },
  { id: "stale", label: "Stale", hint: "Active but not confirmed/used in a long time — still recalled; review or renew." },
  { id: "never-used", label: "Never used", hint: "Active but never recalled — candidates for review." },
  { id: "frequently-recalled", label: "Frequently recalled", hint: "Recalled 3+ times — likely useful." },
  { id: "automatic", label: "Automatically learned", hint: "Learned from runs/conventions, not stated by the user." },
  { id: "expired", label: "Expired", hint: "Retention elapsed — inert, retained for audit." },
  { id: "superseded", label: "Superseded / conflicts", hint: "Replaced by a later memory; excluded from recall." },
  { id: "rejected", label: "Rejected", hint: "Refused at review; never recalled." },
  { id: "archived", label: "Archived", hint: "Deliberately retired; retained for audit." },
];

function Lifecycle({ isOperator }: { isOperator: boolean }) {
  const [overview, setOverview] = useState<MemoryLifecycleResponse | null>(null);
  const [view, setView] = useState<MemoryLifecycleView>("active");
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [retention, setRetention] = useState<MemoryRetentionStatus | null>(null);
  const [usage, setUsage] = useState<MemoryUsageReport | null>(null);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const PAGE_SIZE = 50;

  const load = useCallback(async (target: MemoryLifecycleView, nextOffset: number) => {
    setLoading(true); setError(null);
    try {
      // Fan the reads out in parallel — they are independent, and doing
      // them sequentially tripled the tab's latency.
      const [body, retentionBody, usageBody] = await Promise.all([
        memoryLifecycle(target, undefined, PAGE_SIZE, nextOffset),
        memoryRetention(),
        memoryUsageReport(30),
      ]);
      setOverview(body);
      setMemories(body.memories ?? []);
      setTotal(Number(body.total ?? body.memories?.length ?? 0));
      setRetention(retentionBody);
      setUsage(usageBody);
    } catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { setOffset(0); }, [view]);
  useEffect(() => { void load(view, offset); }, [load, view, offset]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { await fn(); setToast(message); await load(view, offset); }
    catch (err) { setToast(err instanceof Error ? err.message : "action failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 4000); }
  };

  const active = LIFECYCLE_VIEWS.find((v) => v.id === view);

  return (
    <div className="animate-fade-in-up">
      <Header
        title="Memory Lifecycle"
        hint="Every memory has an explicit state. Expired, superseded, rejected and archived memory is never recalled; conflicts do not coexist as active truth."
      />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}

      {overview && (
        <div className="mb-4 grid grid-cols-2 md:grid-cols-4 gap-2">
          {[
            { label: "Active", value: overview.counts.active },
            { label: "Explicit", value: overview.counts.explicit },
            { label: "Automatic", value: overview.counts.automatic },
            { label: "Never used", value: overview.counts.neverUsed },
            { label: "Frequently recalled", value: overview.counts.frequentlyRecalled },
            { label: "Superseded", value: overview.counts.superseded },
            { label: "Expired", value: overview.counts.expired },
            { label: "Rejected", value: overview.counts.rejected },
          ].map((tile) => (
            <div key={tile.label} className="rounded-2xl border border-white/10 glass-card p-3">
              <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">{tile.label}</p>
              <p className="mt-1 text-[18px] font-semibold text-foreground">{tile.value}</p>
            </div>
          ))}
        </div>
      )}

      {/* Diagnostics: retention + contradiction side-by-side on wide screens so
          they no longer stack as full-width blocks that bury the memory list. */}
      <div className="mb-4 grid gap-3 md:grid-cols-2">
        {retention && (
          <div className="rounded-2xl border border-white/10 glass-card p-3.5">
            <p className="text-[11px] font-semibold text-foreground">Retention</p>
            <p className="mt-1 text-[10px] text-muted-foreground">
              {retention.automaticExpiryEnabled
                ? `Automatically learned memory expires after ${retention.retentionDays} day(s); explicit memory is never silently expired.`
                : "Automatic retention is off (0 days) — automatic memory never expires by policy. Explicit memory is never silently expired."}
            </p>
            {retention.nextMaintenanceAction && (
              <p className="mt-1 text-[10px] text-foreground/80">Next: {retention.nextMaintenanceAction}</p>
            )}
            {retention.lastSweep ? (
              <p className={cn("mt-1 text-[10px]", retention.lastSweep.ok ? "text-muted-foreground" : "text-amber-400")}>
                Last sweep {retention.lastSweep.ranAt}: inspected {retention.lastSweep.inspected}, expired {retention.lastSweep.expired}, skipped {retention.lastSweep.skipped}
                {!retention.lastSweep.ok && retention.lastSweep.error ? ` — failed: ${retention.lastSweep.error}` : ""}
              </p>
            ) : (
              <p className="mt-1 text-[10px] text-muted-foreground">No sweep recorded yet.</p>
            )}
          </div>
        )}

        <div className="rounded-2xl border border-white/10 glass-card p-3.5">
          <p className="text-[11px] font-semibold text-foreground">Contradiction detection</p>
          <p className="mt-1 text-[10px] text-muted-foreground">
            Deterministic lexical detection only. No network, no model, no embeddings.
            The optional semantic candidate layer was removed — deterministic
            normalization closed the measured gap, so it added no recall at a safe
            threshold.
          </p>
        </div>
      </div>

      {usage && (
        <div className="mb-4 rounded-2xl border border-white/10 glass-card p-3.5">
          <p className="text-[11px] font-semibold text-foreground">Real usage (last {usage.windowDays} days)</p>
          <p className="mt-1 text-[10px] text-muted-foreground">
            Measured from actual persisted rows — not a synthetic set. A labeled eval proves
            the detector implements its rules, not that the rules match reality. Early
            numbers are small; treat them as such.
          </p>
          {usage.insufficientData ? (
            <p className="mt-1 text-[10px] text-muted-foreground/80">No usage in this window yet.</p>
          ) : (
            <>
              <p className="mt-1.5 text-[10px] text-foreground/80">
                Writes: {usage.durableWrites.saved} saved · {usage.durableWrites.suggested} suggested ·{" "}
                {usage.durableWrites.historyOnly} history-only · {usage.durableWrites.ignored} ignored
                {usage.durableWrites.explicitRequestRate != null
                  ? ` · explicit ${Math.round(usage.durableWrites.explicitRequestRate * 100)}% of saves`
                  : ""}
              </p>
              <p className="mt-1 text-[10px] text-foreground/80">
                Conflicts: {usage.conflicts.raised} raised · {usage.conflicts.accepted} accepted ·{" "}
                {usage.conflicts.dismissed} dismissed · {usage.conflicts.pending} pending
                {usage.conflicts.falsePositiveRate != null
                  ? ` · ${Math.round(usage.conflicts.falsePositiveRate * 100)}% dismissed (proxy)`
                  : ""}
              </p>
              <p className="mt-1 text-[10px] text-foreground/80">
                Ground truth (operator-labelled): {usage.conflicts.labeled} labelled
                {usage.conflicts.precision != null
                  ? ` · precision ${Math.round(usage.conflicts.precision * 100)}%` +
                    (usage.conflicts.precisionInterval
                      ? ` (95% CI ${Math.round(usage.conflicts.precisionInterval[0] * 100)}–${Math.round(usage.conflicts.precisionInterval[1] * 100)}%)`
                      : "") +
                    ` · ${usage.conflicts.labeledTrue} true / ${usage.conflicts.labeledFalsePositive} false`
                  : " · no labels yet — precision unknown, not perfect"}
              </p>
              {usage.trend && usage.trend.confident === false && (
                <p className="mt-1 text-[10px] text-muted-foreground/80">
                  Trend: insufficient data in one or both halves to name a direction.
                </p>
              )}
              {usage.trend && usage.trend.confident && (
                <p className="mt-1 text-[10px] text-foreground/80">
                  Trend: decisions {usage.trend.direction} (older {usage.trend.decisionsOlder} → recent {usage.trend.decisionsRecent})
                </p>
              )}
              {usage.smallSample && (
                <p className="mt-1 text-[10px] text-amber-300/90">
                  Sample is small — treat these numbers as directional, not reliable.
                </p>
              )}
              {usage.likelyMissedConflicts.count > 0 && (
                <p className="mt-1 text-[10px] text-amber-300/90">
                  {usage.likelyMissedConflicts.count} subject(s) have more than one active same-type
                  record — possible missed conflicts for human review (no text shown here).
                </p>
              )}
            </>
          )}
        </div>
      )}

      <CompactionPanel isOperator={isOperator} onChanged={() => void load(view, offset)} />

      <div className="mb-4 flex flex-wrap gap-1.5">
        {LIFECYCLE_VIEWS.map((v) => (
          <button
            key={v.id}
            onClick={() => setView(v.id)}
            className={cn(
              "rounded-xl px-2.5 py-1 text-[10px] border cursor-pointer",
              view === v.id
                ? "border-primary/40 bg-primary/10 text-primary"
                : "border-white/10 text-muted-foreground hover:text-foreground"
            )}
          >
            {v.label}
          </button>
        ))}
      </div>
      {active && <p className="mb-3 text-[10px] text-muted-foreground">{active.hint}</p>}

      {loading ? <Spinner /> : error ? <ErrorBox message={error} /> : memories.length === 0 ? (
        <EmptyBox title="Nothing in this view" hint="Memory only appears here once it exists in that lifecycle state." />
      ) : (
        <div className="space-y-2">
          {memories.map((memory) => (
            <button
              key={memory.id}
              onClick={async () => setDetail(await getMemory(memory.id))}
              className="w-full text-left rounded-2xl border border-white/10 glass-card p-3.5 hover:border-primary/30 cursor-pointer"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <TypeBadge type={memory.memoryType} />
                    <StatusPill status={memory.status} />
                    <span className="text-[9px] text-muted-foreground">
                      {memory.authority === "automatic" ? "automatic" : "explicit"}
                    </span>
                    {memory.subject && <span className="text-[9px] text-primary">#{memory.subject}</span>}
                  </div>
                  <p className="mt-1.5 text-[12px] text-foreground leading-relaxed line-clamp-2">{memory.content}</p>
                  {memory.statusReason && (
                    <p className="mt-0.5 text-[9px] text-muted-foreground/80">{memory.statusReason}</p>
                  )}
                </div>
                <span className="shrink-0 text-[9px] text-muted-foreground">
                  {memory.lastUsedAt ? `used ${memory.lastUsedAt.slice(0, 10)}` : "never used"}
                </span>
              </div>
            </button>
          ))}
        </div>
      )}

      <ServerPaginationFooter
        total={total}
        limit={PAGE_SIZE}
        offset={offset}
        count={memories.length}
        onOffset={(next) => setOffset(Math.max(0, next))}
        label="memories"
        disabled={loading}
      />

      <MemoryDrawer
        open={Boolean(detail)}
        onClose={() => setDetail(null)}
        title="Memory detail"
        subtitle={detail ? `${detail.memory.memoryType} · ${detail.memory.status} · ${detail.memory.authority ?? "explicit"}` : undefined}
        width="md"
        footer={detail ? (
          <>
            <div className="flex flex-wrap gap-2">
              {detail.memory.status === "active" && (
                <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act(() => renewMemory(detail.memory.id), "Memory confirmed / renewed")}>
                  Confirm & renew
                </Button>
              )}
              {(detail.memory.status === "archived" || detail.memory.status === "expired" || detail.memory.status === "superseded") && (
                <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act(() => restoreMemory(detail.memory.id), "Memory restored")}>
                  Restore
                </Button>
              )}
              {detail.memory.status === "proposed" && (
                <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act(() => reviewMemory(detail.memory.id, true), "Memory approved")}>
                  Approve
                </Button>
              )}
              {detail.memory.status !== "archived" && (
                <Button size="sm" variant="outline" className="rounded-xl cursor-pointer text-rose-300" disabled={!isOperator || busy}
                  onClick={() => act(() => archiveMemory(detail.memory.id), "Memory archived")}>
                  <Trash2 size={12} className="mr-1" /> Archive
                </Button>
              )}
            </div>
            {!isOperator && <p className="mt-2 text-[10px] text-amber-300/90">Read-only session — changes require operator authorization.</p>}
          </>
        ) : undefined}
      >
        {detail && (
          <>
            <p className="text-[12px] text-foreground leading-relaxed">{detail.memory.content}</p>
            {detail.explanation && (
              <div className="mt-4 rounded-2xl border border-white/10 glass-card-subtle p-3">
                <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Why this memory exists</p>
                <p className="mt-1 text-[11px] text-foreground/90">{detail.explanation.saveReason}</p>
                <p className="mt-1 text-[11px] text-foreground/90">{detail.explanation.recallDisposition}</p>
                <p className="mt-1 text-[10px] text-muted-foreground">
                  authority: {detail.explanation.authority}
                  {detail.explanation.statusReason ? ` · ${detail.explanation.statusReason}` : ""}
                </p>
              </div>
            )}
            <dl className="mt-4 text-[11px]">
              <Meta label="Status" value={detail.memory.status} />
              <Meta label="Authority" value={detail.memory.authority ?? "explicit"} />
              <Meta label="Expires" value={detail.memory.expiresAt ?? "never"} />
              <Meta label="Last confirmed" value={detail.memory.lastConfirmedAt ?? "—"} />
              <Meta label="Last used" value={detail.memory.lastUsedAt ?? "never"} />
              <Meta label="Last reviewed" value={detail.memory.lastReviewedAt ?? detail.memory.reviewedAt ?? "—"} />
              {detail.memory.supersededBy && <Meta label="Superseded by" value={detail.memory.supersededBy} />}
            </dl>
            {detail.explanation?.conflict && (
              <div className="mt-3 rounded-2xl border border-amber-400/20 glass-card-subtle p-3">
                <p className="text-[10px] uppercase tracking-widest font-bold text-amber-400">Possible contradiction</p>
                <p className="mt-1 text-[11px] text-foreground/90">{detail.explanation.conflict.reason}</p>
                <p className="mt-1 text-[10px] text-muted-foreground">
                  detector: {detail.explanation.conflict.detector ?? "lexical"}
                  {detail.explanation.conflict.similarity != null ? ` · similarity ${detail.explanation.conflict.similarity}` : ""}
                  {detail.explanation.conflict.authority ? ` · authority ${detail.explanation.conflict.authority}` : ""}
                </p>
                <p className="mt-1 text-[10px] text-muted-foreground/80">
                  {detail.explanation.conflict.requiresConfirmation
                    ? "This is NOT active truth until you confirm. The existing memory stays authoritative."
                    : "Both records are retained for audit; the authoritative one is the active memory."}
                </p>
              </div>
            )}
            {detail.links.filter((l) => l.relation === "superseded").length > 0 && (
              <div className="mt-3">
                <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Conflict history</p>
                <ul className="mt-1 space-y-1">
                  {detail.links.filter((l) => l.relation === "superseded").map((link) => (
                    <li key={link.id} className="text-[10px] text-muted-foreground">
                      superseded {link.toId === detail.memory.id ? link.fromId : link.toId}
                      {link.note ? ` — ${link.note}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </MemoryDrawer>
    </div>
  );
}

// ── Conservative compaction (near-duplicate proposals) ──────────────────────

function CompactionPanel({ isOperator, onChanged }: { isOperator: boolean; onChanged: () => void }) {
  const [proposals, setProposals] = useState<CompactionProposal[]>([]);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setProposals(await listCompaction("proposed")); } catch { setProposals([]); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true); setToast(null);
    try { await fn(); setToast(message); await load(); onChanged(); }
    catch (err) { setToast(err instanceof Error ? err.message : "action failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 5000); }
  };

  return (
    <div className="mb-4 rounded-2xl border border-white/10 glass-card p-3.5">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[11px] font-semibold text-foreground">Memory compaction</p>
        <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
          onClick={() => act(() => detectCompaction(), "Checked for near-duplicates")}>
          Find duplicates
        </Button>
      </div>
      <p className="mt-1 text-[10px] text-muted-foreground">
        Near-duplicate facts in the same scope/subject can be merged. Nothing changes until you accept a
        proposal; originals are superseded (retained) and the merge is reversible.
      </p>
      {toast && <p className="mt-1 text-[10px] text-primary">{toast}</p>}
      {proposals.length > 0 && (
        <div className="mt-2 space-y-2">
          {proposals.map((p) => (
            <div key={p.id} className="rounded-xl border border-border/20 bg-background/30 p-2.5">
              <p className="text-[10px] text-muted-foreground">{p.scope}{p.scopeId ? `:${p.scopeId}` : ""} · {p.subject ?? "no subject"} · {p.memberIds.length} memories</p>
              <p className="mt-1 text-[11px] text-foreground">{p.mergedContent}</p>
              {p.rationale && <p className="mt-1 text-[10px] text-muted-foreground/80">{p.rationale}</p>}
              <div className="mt-2 flex gap-2">
                <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act(() => acceptCompaction(p.id), "Merged")}>Accept merge</Button>
                <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act(() => rejectCompaction(p.id), "Proposal dismissed")}>Dismiss</Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Overview ────────────────────────────────────────────────────────────────

// ── Profile (always-available compact prompt context) ───────────────────────

function Profile({ isOperator }: { isOperator: boolean }) {
  const [data, setData] = useState<MemoryProfileResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setData(await memoryProfile()); }
    catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const rebuild = async () => {
    setBusy(true);
    try { await rebuildProfile(); setToast("Profile snapshot rebuilt"); await load(); }
    catch (err) { setToast(err instanceof Error ? err.message : "rebuild failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 4000); }
  };

  if (loading) return <Spinner />;
  if (error) return <ErrorBox message={error} />;
  if (!data) return null;
  const snap = data.snapshot;

  return (
    <div className="animate-fade-in-up">
      <Header title="Persistent Profile" hint="A bounded, deterministic snapshot injected into every run as personalization context — never financial evidence and never an instruction channel. Memory saved during a run appears in later runs only." />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}

      <div className="rounded-2xl border border-white/10 glass-card p-4">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-[11px] text-muted-foreground">
          <span>{snap.selectedIds.length} selected</span>
          <span>{snap.charCount} chars</span>
          <span>~{snap.tokenEstimate} tokens</span>
          <span>{snap.excluded.length} excluded</span>
          <span>schema v{snap.memoryVersion}</span>
          {snap.snapshotId && <span>snapshot {snap.snapshotId.slice(0, 18)}…</span>}
        </div>
        <div className="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-3">
          <ProfileSection title="User profile" entries={snap.userEntries} budget={snap.limits.userChars} />
          <ProfileSection title="Project context" entries={snap.projectEntries} budget={snap.limits.projectChars} />
        </div>
      </div>

      {snap.excluded.length > 0 && (
        <div className="mt-4 rounded-2xl border border-white/10 glass-card p-4">
          <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground mb-2">Excluded ({snap.excluded.length})</p>
          <ul className="space-y-0.5 max-h-48 overflow-auto">
            {snap.excluded.slice(0, 40).map((item, i) => (
              <li key={`${item.id}-${i}`} className="text-[10px] text-muted-foreground">
                {item.id ? `${item.id.slice(0, 16)}…` : "entry"} — {item.reason}
              </li>
            ))}
            {snap.excluded.length > 40 && (
              <li className="text-[10px] text-muted-foreground/70">
                Showing 40 of {snap.excluded.length}; the rest are omitted from this preview.
              </li>
            )}
          </ul>
        </div>
      )}

      <div className="mt-4 rounded-2xl border border-white/10 glass-card p-4">
        <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground mb-2">Prompt preview</p>
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-xl border border-border/20 bg-background/40 p-3 text-[10px] text-foreground/80">
          {data.prompt || "(empty — no eligible memory yet)"}
        </pre>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={load}>
          <RefreshCcw size={13} className="mr-1" /> Refresh
        </Button>
        <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy} onClick={rebuild}>
          Rebuild snapshot
        </Button>
      </div>
      {!isOperator && <p className="mt-3 text-[10px] text-amber-300/90">Read-only session — rebuilding requires operator authorization.</p>}
    </div>
  );
}

function ProfileSection({ title, entries, budget }: { title: string; entries: MemoryProfileResponse["snapshot"]["userEntries"]; budget?: number }) {
  return (
    <div className="rounded-2xl border border-white/10 glass-card-subtle p-3">
      <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground mb-2">
        {title} <span className="font-normal normal-case tracking-normal">({entries.length}{budget ? ` · ≤${budget} chars` : ""})</span>
      </p>
      {entries.length === 0 ? <Muted>None selected.</Muted> : (
        <ul className="space-y-1.5">
          {entries.map((entry) => (
            <li key={entry.id} className="text-[11px] text-foreground/90">
              <span className="text-foreground/95">{entry.content}</span>
              <span className="ml-1 text-[9px] text-muted-foreground">
                [{entry.memoryType} · {entry.provenance} · {Math.round(entry.confidence * 100)}%]
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Reviews (automatic learning review queue) ───────────────────────────────

function Reviews({ isOperator }: { isOperator: boolean }) {
  const [health, setHealth] = useState<LearningReviewHealth | null>(null);
  const [reviews, setReviews] = useState<LearningReviewRecord[]>([]);
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const PAGE_SIZE = 25;
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);

  const load = useCallback(async () => {
    try {
      const [h, page] = await Promise.all([
        learningReviewHealth(),
        listLearningReviewsPage(undefined, PAGE_SIZE, offset),
      ]);
      setHealth(h); setReviews(page.reviews); setTotal(page.total);
    } catch (err) { setError(err instanceof Error ? err.message : "failed"); }
  }, [offset]);
  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { await fn(); setToast(message); await load(); }
    catch (err) { setToast(err instanceof Error ? err.message : "action failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 5000); }
  };

  return (
    <div className="animate-fade-in-up">
      <Header title="Automatic Learning Review" hint="A bounded, safe post-run review proposes inert outcomes only. Nothing here enables a skill automatically, and every durable write passes the shared decision gate." />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}
      {error && <ErrorBox message={error} />}

      {health && (
        <div className="rounded-2xl border border-white/10 glass-card p-4">
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-muted-foreground">
            <span>{health.total} review(s)</span>
            <span>queued: {health.queue.queued}</span>
            <span>running: {health.queue.running}</span>
            <span>max concurrent: {health.queue.maxConcurrent}</span>
            <span>timeout: {health.queue.timeoutMs}ms</span>
          </div>
        </div>
      )}

      <div className="mt-4 rounded-2xl border border-white/10 glass-card p-4">
        <p className="text-[11px] font-semibold text-foreground">Run a review for a completed run</p>
        <div className="mt-2 flex gap-2">
          <Input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="run id…" className="rounded-xl bg-background/60" />
          <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy || !runId.trim()}
            onClick={() => act(() => runLearningReview(runId.trim()), "Review complete")}>Deterministic</Button>
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy || !runId.trim()}
            onClick={() => act(() => runLearningReview(runId.trim(), true), "Review complete (model)")}>+ Model</Button>
        </div>
        <p className="mt-2 text-[10px] text-muted-foreground">The model variant adds an optional bounded review; its output is inert and re-checked by the gate.</p>
      </div>

      <div className="mt-4 space-y-2">
        {health && reviews.length > 0 && (
          <p className="text-[10px] text-muted-foreground">
            Showing {reviews.length} of {total} review(s).
          </p>
        )}
        {reviews.length === 0 ? (
          <EmptyBox title="No reviews yet" hint="Completed runs are reviewed automatically after they finish." />
        ) : reviews.map((review) => (
          <div key={review.id} className="rounded-2xl border border-white/10 glass-card p-3.5">
            <div className="flex items-center gap-2 flex-wrap">
              <span className={cn("rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide",
                review.status === "completed" ? "border-emerald-400/30 text-emerald-300" :
                review.status === "failed" ? "border-rose-400/30 text-rose-300" : "border-border/40 text-muted-foreground")}>
                {review.status}
              </span>
              <span className="text-[9px] text-muted-foreground">{review.mode}</span>
              <span className="text-[9px] text-muted-foreground/70">run {review.runId.slice(0, 16)}…</span>
              <span className="text-[9px] text-muted-foreground">{review.createdAt.slice(0, 19)}</span>
              {review.model && <span className="text-[9px] text-muted-foreground">model: {review.model}</span>}
            </div>
            {review.error && <p className="mt-1 text-[10px] text-rose-300">{review.error}</p>}
            {review.result?.outcomes?.length ? (
              <ul className="mt-2 space-y-1">
                {review.result.outcomes.map((outcome, index) => (
                  <li key={index} className="flex items-start justify-between gap-2 rounded-xl border border-border/20 bg-background/30 p-2">
                    <div className="min-w-0">
                      <span className="rounded-full border border-border/40 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-muted-foreground">{outcome.category}</span>
                      <span className="ml-2 text-[10px] text-muted-foreground">{outcome.inert ? "inert" : "active"}{outcome.created ? " · created" : ""}</span>
                      {outcome.reason && <p className="mt-0.5 text-[10px] text-foreground/80">{outcome.reason}</p>}
                    </div>
                    {isOperator && (outcome.category === "durable_memory_proposal" || outcome.category === "memory_suggestion") && (
                      <div className="flex shrink-0 gap-1">
                        <Button size="sm" className="rounded-lg h-6 px-2 text-[10px] cursor-pointer" disabled={busy}
                          onClick={() => act(() => decideLearningReview(review.id, index, "accept"), "Outcome accepted")}>Accept</Button>
                        <Button size="sm" variant="outline" className="rounded-lg h-6 px-2 text-[10px] cursor-pointer" disabled={busy}
                          onClick={() => act(() => decideLearningReview(review.id, index, "reject"), "Outcome rejected")}>Reject</Button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-[10px] text-muted-foreground">No outcomes recorded.</p>
            )}
          </div>
        ))}
      </div>

      <ServerPaginationFooter
        total={total}
        limit={PAGE_SIZE}
        offset={offset}
        count={reviews.length}
        onOffset={(next) => setOffset(Math.max(0, next))}
        label="reviews"
      />

      {!isOperator && <p className="mt-3 text-[10px] text-amber-300/90">Read-only session — running or deciding a review requires operator authorization.</p>}
    </div>
  );
}

// ── History (full-session search) ───────────────────────────────────────────

function History() {
  const PAGE_SIZE = 20;
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SessionSearchHit[]>([]);
  const [health, setHealth] = useState<SessionSearchHealth | null>(null);
  const [engine, setEngine] = useState<string>("");
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<{ runId: string; messages: SessionSearchHit[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    sessionSearchHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  const run = async (nextOffset = 0, append = false) => {
    if (!query.trim()) return;
    setBusy(true); setError(null);
    try {
      const result = await sessionSearch({ q: query, limit: PAGE_SIZE, offset: nextOffset });
      setResults((current) => (append ? [...current, ...result.results] : result.results));
      setEngine(result.engine);
      setHasMore(result.hasMore);
      setOffset(nextOffset);
    } catch (err) { setError(err instanceof Error ? err.message : "search failed"); }
    finally { setBusy(false); }
  };

  const openRun = async (runId: string) => {
    try {
      const window = await sessionOpen(runId);
      setOpen({ runId, messages: window.messages });
    } catch (err) { setToast(err instanceof Error ? err.message : "open failed"); setTimeout(() => setToast(null), 4000); }
  };

  return (
    <div className="animate-fade-in-up">
      <Header title="Session Search" hint="Search what was discussed, decided, corrected or discovered in prior sessions. Historical content is CONTEXT — never current market evidence; re-retrieve current facts separately." />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}
      <div className="rounded-2xl border border-white/10 glass-card p-4">
        <div className="flex gap-2">
          <Input value={query} onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void run(0); }}
            placeholder="Search prior sessions…" className="rounded-xl bg-background/60" />
          <Button size="sm" className="rounded-xl cursor-pointer" disabled={busy} onClick={() => void run(0)}>Search</Button>
        </div>
        {health && (
          <p className="mt-2 text-[10px] text-muted-foreground">
            {health.indexed} message(s) indexed · FTS5: {health.fts5 ? "available" : "unavailable"} ·
            indexing: {health.indexingEnabled ? "on" : "off"} · retention {health.retentionDays}d
          </p>
        )}
      </div>

      {error && <ErrorBox message={error} />}

      {engine && (
        <p className="mt-3 text-[10px] text-muted-foreground">
          {results.length} result(s) via {engine}
        </p>
      )}

      <div className="mt-3 space-y-2">
        {results.map((hit) => (
          <div key={hit.messageId} className="rounded-2xl border border-white/10 glass-card p-3.5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="rounded-full border border-border/40 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide text-muted-foreground">{hit.role}/{hit.kind}</span>
                  <span className="text-[9px] text-muted-foreground">{hit.createdAt.slice(0, 10)}</span>
                  <span className="text-[9px] text-muted-foreground/70">run {hit.runId.slice(0, 14)}…</span>
                </div>
                <p className="mt-1 text-[12px] text-foreground/90">{hit.snippet}</p>
              </div>
              <div className="flex shrink-0 gap-1.5">
                <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={() => openRun(hit.runId)}>Open</Button>
                <Button size="sm" variant="outline" className="rounded-xl cursor-pointer text-rose-300"
                  onClick={async () => {
                    try { await sessionDelete(hit.runId); setToast(`Removed index for ${hit.runId.slice(0, 12)}…`); setTimeout(() => setToast(null), 4000); }
                    catch (err) { setToast(err instanceof Error ? err.message : "delete failed"); setTimeout(() => setToast(null), 4000); }
                  }}>Forget</Button>
              </div>
            </div>
          </div>
        ))}
      </div>

      {hasMore && (
        <div className="mt-3 flex justify-center">
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={busy}
            onClick={() => void run(offset + PAGE_SIZE, true)}>
            Load more results
          </Button>
        </div>
      )}

      <MemoryModal
        open={Boolean(open)}
        onClose={() => setOpen(null)}
        title={open ? `Run ${open.runId}` : ""}
        width="lg"
      >
        <div className="space-y-2">
          {open?.messages.map((m) => (
            <div key={m.messageId} className="rounded-xl border border-border/20 bg-background/30 p-2.5">
              <p className="text-[9px] uppercase tracking-wide text-muted-foreground">{m.role}/{m.kind} · {m.createdAt}</p>
              <p className="mt-1 text-[11px] text-foreground/90 whitespace-pre-wrap">{m.content ?? m.snippet}</p>
            </div>
          ))}
        </div>
      </MemoryModal>

      <p className="mt-3 text-[10px] text-amber-300/90">Historical results are context only. They can never answer a current price/fundamental question — retrieve fresh data for that.</p>
    </div>
  );
}

function Overview() {
  const [data, setData] = useState<MemoryOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [provider, setProvider] = useState<MemoryProviderStatus | null>(null);
  const [sessions, setSessions] = useState<ChatSessionRecord[]>([]);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setData(await memoryOverview()); }
    catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
    // Default-view extras — each is best-effort so one failure cannot blank the page.
    memoryProviderStatus().then(setProvider).catch(() => setProvider(null));
    listChatSessions({ limit: 5 }).then(setSessions).catch(() => setSessions([]));
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (loading) return <Spinner />;
  if (error) return <ErrorBox message={error} />;
  if (!data) return null;

  const tiles = [
    { label: "Facts", value: data.memories.total, sub: `${Object.keys(data.memories.byType).length} types` },
    { label: "Active facts", value: data.memories.byStatus.active ?? 0, sub: "recallable now" },
    { label: "Skills", value: data.skills.installed, sub: "installed" },
    { label: "Pending suggestions", value: data.suggestions, sub: "awaiting your review" },
    { label: "Learning changes", value: data.candidates, sub: "candidates in queue" },
    { label: "Corrections", value: data.corrections, sub: "captured corrections" },
    { label: "Providers", value: provider?.available?.length ?? 0, sub: `of ${provider?.providerCount ?? 0} available` },
    { label: "Recent sessions", value: sessions.length, sub: "last 5" },
  ];

  return (
    <div className="animate-fade-in-up">
      <Header
        title="Eclipse Memory"
        hint="Dedicated persistence for agent memory, sessions, skills, learning and providers. Real counts only — nothing is fabricated."
      />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {tiles.map((tile) => (
          <div key={tile.label} className="rounded-2xl border border-white/10 dark:border-white/5 glass-card p-4">
            <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">{tile.label}</p>
            <p className="mt-1.5 font-serif text-2xl font-semibold text-foreground tabular-nums">{tile.value}</p>
            <p className="text-[10px] text-muted-foreground">{tile.sub}</p>
          </div>
        ))}
      </div>

      {data.memories.total === 0 && (
        <div className="mt-6 rounded-2xl border border-border/20 glass-card-subtle p-8 text-center">
          <Brain size={26} className="mx-auto text-muted-foreground/60" />
          <p className="mt-3 text-sm font-semibold text-foreground">No memory yet</p>
          <p className="mt-1 text-[11px] text-muted-foreground max-w-md mx-auto">
            Memory is learned selectively from explicit requests and strong, repeated signals. New durable
            facts pass the memory gate; financial facts and secrets are never stored automatically.
          </p>
        </div>
      )}

      {Object.keys(data.memories.byType).length > 0 && (
        <div className="mt-6 rounded-2xl border border-white/10 glass-card p-4">
          <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground mb-3">Memory by type</p>
          <div className="flex flex-wrap gap-2">
            {Object.entries(data.memories.byType).map(([type, count]) => (
              <span key={type} className="rounded-lg border border-border/30 bg-accent/30 px-2 py-1 text-[11px] text-foreground">
                {type} <span className="text-muted-foreground">×{count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      <Button variant="outline" size="sm" className="mt-6 rounded-xl cursor-pointer" onClick={load}>
        <RefreshCcw size={13} className="mr-1" /> Refresh
      </Button>
    </div>
  );
}

// ── Sessions (named, with lifecycle + export/fork) ──────────────────────────

function Sessions({ isOperator }: { isOperator: boolean }) {
  const PAGE_SIZE = 25;
  const [sessions, setSessions] = useState<ChatSessionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SessionSearchHit[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<{ session: ChatSessionRecord; runs: string[]; summary: Record<string, unknown> | null; messages: SessionSearchHit[] } | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const page = await listChatSessionsPage({ limit: PAGE_SIZE, offset });
      setSessions(page.sessions);
      setTotal(page.total);
    }
    catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, [offset]);
  useEffect(() => { void load(); }, [load]);

  const notify = (message: string) => { setToast(message); setTimeout(() => setToast(null), 4000); };

  const act = async (fn: () => Promise<unknown>, message: string) => {
    try { await fn(); notify(message); await load(); }
    catch (err) { notify(err instanceof Error ? err.message : "action failed"); }
  };

  const search = async () => {
    if (!query.trim()) return;
    try { setResults((await sessionSearch({ q: query, limit: 20 })).results); }
    catch (err) { notify(err instanceof Error ? err.message : "search failed"); }
  };

  const openDetail = async (session: ChatSessionRecord) => {
    try {
      const [full, summary, exported] = await Promise.all([
        getChatSession(session.id),
        getSessionSummary(session.id).catch(() => ({ summary: null })),
        exportChatSession(session.id).catch(() => ({ messages: [] as SessionSearchHit[] })),
      ]);
      setDetail({
        session: full.session,
        runs: full.runs,
        summary: (summary.summary as Record<string, unknown>) ?? full.session.summary,
        messages: exported.messages ?? [],
      });
    } catch (err) { notify(err instanceof Error ? err.message : "open failed"); }
  };

  return (
    <div className="animate-fade-in-up">
      <Header title="Sessions" hint="Long-lived named sessions. A run belongs to one session; a session belongs to one profile. Historical content is context, never current financial evidence." />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}
      <div className="mb-3 flex gap-2">
        <Input value={query} onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void search(); }}
          placeholder="Search across all sessions…" className="rounded-xl bg-background/60" />
        <Button size="sm" className="rounded-xl cursor-pointer" onClick={search}>Search</Button>
      </div>
      {results.length > 0 && (
        <div className="mb-4 rounded-2xl border border-white/10 glass-card p-3">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">{results.length} result(s)</p>
            <button className="text-[10px] text-muted-foreground hover:text-foreground cursor-pointer" onClick={() => setResults([])}>Clear</button>
          </div>
          <div className="space-y-1.5">
            {results.map((hit) => (
              <p key={hit.messageId} className="text-[11px] text-foreground/90">
                <span className="text-muted-foreground">[{hit.role}/{hit.kind}]</span> {hit.snippet}
              </p>
            ))}
          </div>
        </div>
      )}
      {loading ? <Spinner /> : error ? <ErrorBox message={error} /> : (
        <div className="space-y-2">
          {sessions.length === 0 && <p className="text-[11px] text-muted-foreground">No sessions recorded yet.</p>}
          {sessions.map((session) => (
            <div key={session.id} className="rounded-2xl border border-white/10 glass-card p-3.5">
              <div className="flex items-start justify-between gap-3">
                <button className="min-w-0 text-left cursor-pointer" onClick={() => void openDetail(session)}>
                  <p className="truncate text-[13px] font-medium text-foreground hover:text-primary">{session.title || session.id}</p>
                  <p className="text-[10px] text-muted-foreground">
                    {session.status} · profile {session.profileId ?? "default"} · {session.lastActivityAt?.slice(0, 16) ?? session.createdAt.slice(0, 16)}
                    {session.sourceSessionId ? ` · forked from ${session.sourceSessionId.slice(0, 12)}…` : ""}
                  </p>
                </button>
                <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
                  <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={() => void openDetail(session)}>Open</Button>
                  {isOperator && (
                    <>
                      <Button size="sm" variant="outline" className="rounded-xl cursor-pointer"
                        onClick={async () => {
                          const title = await promptDialog({ title: "Rename session", defaultValue: session.title ?? "" });
                          if (title !== null) void act(() => updateChatSession(session.id, { title }), "Renamed");
                        }}>Rename</Button>
                      <Button size="sm" variant="outline" className="rounded-xl cursor-pointer"
                        onClick={() => void act(() => updateChatSession(session.id, { status: session.status === "archived" ? "active" : "archived", archivedAt: session.status === "archived" ? null : new Date().toISOString() }), "Updated")}>
                        {session.status === "archived" ? "Restore" : "Archive"}
                      </Button>
                      <Button size="sm" variant="outline" className="rounded-xl cursor-pointer"
                        onClick={() => void act(() => forkChatSession(session.id), "Forked")}>Fork</Button>
                      <Button size="sm" variant="outline" className="rounded-xl cursor-pointer"
                        onClick={async () => {
                          try {
                            const body = await exportChatSession(session.id);
                            const blob = new Blob([JSON.stringify(body, null, 2)], { type: "application/json" });
                            saveBlob(`${session.id}.json`, blob);
                          } catch (err) { notify(err instanceof Error ? err.message : "export failed"); }
                        }}>Export</Button>
                      <Button size="sm" variant="outline" className="rounded-xl cursor-pointer text-rose-300"
                        onClick={() => void act(() => deleteChatSession(session.id, true), "Deleted")}>Delete</Button>
                    </>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <ServerPaginationFooter
        total={total}
        limit={PAGE_SIZE}
        offset={offset}
        count={sessions.length}
        onOffset={(next) => setOffset(Math.max(0, next))}
        label="sessions"
        disabled={loading}
      />

      <MemoryModal
        open={Boolean(detail)}
        onClose={() => setDetail(null)}
        title={detail ? (detail.session.title || detail.session.id) : ""}
        width="lg"
      >
        {detail && (
          <>
            <p className="-mt-1 text-[10px] text-muted-foreground">
              {detail.runs.length} run(s) · {detail.messages.length} indexed message(s) · profile {detail.session.profileId ?? "default"}
            </p>

            {detail.summary && (
              <div className="mt-3 rounded-xl border border-border/20 bg-background/30 p-3">
                <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Summary</p>
                {detail.summary.objective ? <p className="mt-1 text-[11px] text-foreground/90">Objective: {String(detail.summary.objective)}</p> : null}
                {Array.isArray(detail.summary.nextActions) && detail.summary.nextActions.length > 0 && (
                  <p className="mt-1 text-[11px] text-foreground/90">Next: {(detail.summary.nextActions as string[]).join("; ")}</p>
                )}
                {Array.isArray(detail.summary.instruments) && detail.summary.instruments.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {(detail.summary.instruments as string[]).map((t) => (
                      <span key={t} className="rounded-md border border-border/30 bg-accent/30 px-1.5 py-0.5 font-mono text-[9px] text-foreground">{t}</span>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div className="mt-3 space-y-1.5">
              {detail.messages.slice(0, 60).map((m) => (
                <div key={m.messageId} className="rounded-xl border border-border/20 bg-background/30 p-2.5">
                  <p className="text-[9px] uppercase tracking-wide text-muted-foreground">{m.role}/{m.kind} · {m.createdAt?.slice(0, 16)}</p>
                  <p className="mt-1 text-[11px] text-foreground/90 whitespace-pre-wrap">{m.content ?? m.snippet}</p>
                </div>
              ))}
              {detail.messages.length > 60 && (
                <p className="text-[10px] text-muted-foreground/70">
                  Showing the 60 most recent of {detail.messages.length} indexed message(s).
                </p>
              )}
            </div>
          </>
        )}
      </MemoryModal>
    </div>
  );
}

// ── Files (Markdown projection + edit history) ──────────────────────────────

// Client-safe parser for the visible entry markers (the server owns the
// authoritative parse; this only renders the preview). Matches memory-files.ts.
function parseClientEntries(text: string): Array<{ id: string | null; version: string | null; content: string }> {
  const marker = /<!--\s*eclipse:id=([^;\s]+);type=([^;\s]+);scope=([^;\s]+)(?:;version=([^;\s]+))?\s*-->/i;
  const lines = text.split(/\r?\n/);
  const entries: Array<{ id: string | null; version: string | null; content: string }> = [];
  for (let i = 0; i < lines.length; i += 1) {
    const m = /^\s*-\s+(.*\S)\s*$/.exec(lines[i]);
    if (!m) continue;
    const content = m[1];
    if (!content || content === "_None._" || content.startsWith("#")) continue;
    const found = marker.exec(lines[i + 1] ?? "");
    if (!found) continue;
    entries.push({ id: found[1], version: found[4] ?? null, content });
  }
  return entries;
}

function FilesTab({ isOperator }: { isOperator: boolean }) {
  const [files, setFiles] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ entries: number; ignored: number } | null>(null);
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<FileRevision[]>([]);
  const [diff, setDiff] = useState<Array<{ type: "add" | "remove" | "same"; line: string }> | null>(null);
  const [editing, setEditing] = useState<{ memoryId: string; expectedVersion: string; content: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { const body = await getMemoryFiles(); setFiles(body.files ?? {}); }
    catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const notify = (m: string) => { setToast(m); setTimeout(() => setToast(null), 4000); };

  const openHistory = async (file: string, revision?: string) => {
    setActiveFile(file); setDiff(null);
    try {
      const body = await listFileHistory(file, revision);
      setRevisions(body.revisions ?? []);
      if (body.diff) setDiff(body.diff);
    } catch (err) { notify(err instanceof Error ? err.message : "history failed"); }
  };

  const applyEdit = async (op: FileEditOperation, memoryId?: string, content?: string, expectedVersion?: string) => {
    if (!activeFile) return;
    try {
      const result = await editMemoryFile({ operation: op, file: activeFile, memoryId, content, expectedVersion });
      if (!result.ok) { notify(`Edit refused: ${result.error}`); return; }
      notify(`${op} applied — import to persist`);
      await load(); await openHistory(activeFile);
    } catch (err) { notify(err instanceof Error ? err.message : "edit failed"); }
  };

  if (loading) return <Spinner />;
  if (error) return <ErrorBox message={error} />;

  const fileNames = Object.keys(files);
  const current = activeFile ? files[activeFile] ?? "" : "";
  const parsed = activeFile ? parseClientEntries(current) : [];

  return (
    <div className="animate-fade-in-up">
      <Header title="Memory Files" hint="A human-readable Markdown projection of structured memory. SQLite stays canonical; edits are atomic, versioned and reversible, then imported through the gated pipeline." />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}
      {isOperator && (
        <div className="mb-3 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={async () => {
            try { const r = await regenerateMemoryFiles(); notify(`Wrote ${r.files.length} file(s)`); await load(); }
            catch (err) { notify(err instanceof Error ? err.message : "regenerate failed"); }
          }}>Regenerate (atomic + backup)</Button>
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={async () => {
            try { const p = await previewMarkdownImport(); setPreview({ entries: p.entries.length, ignored: p.ignored.length }); notify(`Preview: ${p.entries.length} entr(ies)`); }
            catch (err) { notify(err instanceof Error ? err.message : "preview failed"); }
          }}>Preview import</Button>
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={async () => {
            try { const r = await importMarkdown(false); notify(`Imported ${r.imported}, skipped ${r.skipped}`); await load(); }
            catch (err) { notify(err instanceof Error ? err.message : "import failed"); }
          }}>Apply import</Button>
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={async () => {
            try { await restoreMemoryFiles(); notify("Restored previous files"); await load(); }
            catch (err) { notify(err instanceof Error ? err.message : "restore failed"); }
          }}>Restore backup</Button>
        </div>
      )}
      {preview && <p className="mb-3 text-[11px] text-muted-foreground">Preview: {preview.entries} importable entr(ies), {preview.ignored} ignored.</p>}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        {/* File content */}
        <div className="min-w-0 space-y-3">
          {fileNames.map((name) => (
            <div key={name} className={cn("rounded-2xl border glass-card p-3", activeFile === name ? "border-primary/40" : "border-white/10")}>
              <div className="mb-1 flex items-center justify-between gap-2">
                <p className="font-mono text-[11px] font-semibold text-foreground">{name}</p>
                <button onClick={() => void openHistory(name)} className="text-[10px] text-primary hover:underline cursor-pointer">History &amp; edit</button>
              </div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-[10px] text-muted-foreground">{files[name]}</pre>
            </div>
          ))}
        </div>

        {/* Detail / history / edit */}
        <div className="min-w-0 space-y-3">
          {!activeFile && (
            <div className="rounded-2xl border border-white/10 glass-card-subtle p-4 text-center">
              <FileText size={20} className="mx-auto text-muted-foreground/60" />
              <p className="mt-2 text-[11px] text-muted-foreground">Select “History &amp; edit” on a file to review revisions, diff a change and edit an entry.</p>
            </div>
          )}
          {activeFile && (
            <div className="rounded-2xl border border-white/10 glass-card p-3.5">
              <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Entries · {activeFile}</p>
              <div className="mt-2 max-h-60 space-y-1.5 overflow-auto">
                {parsed.length === 0 && <p className="text-[11px] text-muted-foreground">No marked entries.</p>}
                {parsed.map((entry, i) => (
                  <div key={`${entry.id}-${i}`} className="rounded-xl border border-border/20 bg-background/30 p-2">
                    <p className="text-[11px] text-foreground/90">{entry.content}</p>
                    <div className="mt-1 flex items-center justify-between gap-2">
                      <span className="font-mono text-[9px] text-muted-foreground">{entry.id ?? "(new)"} · {entry.version ?? "no version"}</span>
                      {isOperator && entry.id && (
                        <div className="flex gap-1.5">
                          <button className="text-[10px] text-primary hover:underline cursor-pointer"
                            onClick={() => { setEditing({ memoryId: entry.id!, expectedVersion: entry.version ?? "", content: entry.content }); }}>Edit</button>
                          <button className="text-[10px] text-rose-300 hover:underline cursor-pointer"
                            onClick={() => void applyEdit("replace", entry.id!, entry.content + " ", entry.version ?? undefined)}>Touch</button>
                        </div>
                      )}
                    </div>                  </div>
                ))}
              </div>

              {editing && (
                <div className="mt-3 rounded-xl border border-border/20 bg-background/30 p-2.5">
                  <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Edit {editing.memoryId} (v {editing.expectedVersion || "—"})</p>
                  <textarea value={editing.content} onChange={(e) => setEditing({ ...editing, content: e.target.value })}
                    className="mt-2 w-full rounded-lg border border-border/30 bg-background/60 p-2 text-[11px] text-foreground" rows={3} />
                  <div className="mt-2 flex gap-2">
                    <Button size="sm" className="rounded-xl cursor-pointer"
                      onClick={async () => { await applyEdit("replace", editing.memoryId, editing.content, editing.expectedVersion); setEditing(null); }}>
                      Save (stale-safe)
                    </Button>
                    <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={() => setEditing(null)}>Cancel</Button>
                  </div>
                  <p className="mt-1 text-[9px] text-muted-foreground">Rejected if the entry changed since it was loaded (expectedVersion guard).</p>
                </div>
              )}

              <div className="mt-3 border-t border-border/20 pt-3">
                <div className="flex items-center justify-between">
                  <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Revision history ({revisions.length})</p>
                  {isOperator && revisions.length > 0 && (
                    <button className="text-[10px] text-primary hover:underline cursor-pointer"
                      onClick={async () => {
                        try { await rollbackMemoryFile(activeFile, revisions[revisions.length - 1].revision); notify("Rolled back"); await load(); await openHistory(activeFile); }
                        catch (err) { notify(err instanceof Error ? err.message : "rollback failed"); }
                      }}>Roll back latest</button>
                  )}
                </div>
                <div className="mt-1.5 space-y-1">
                  {revisions.length === 0 && <p className="text-[11px] text-muted-foreground">No revisions recorded.</p>}
                  {revisions.map((rev) => (
                    <button key={rev.revision} onClick={() => void openHistory(activeFile, rev.revision)}
                      className="block w-full rounded-lg border border-border/20 bg-background/20 px-2 py-1 text-left text-[10px] hover:bg-accent/20 cursor-pointer">
                      <span className="text-foreground/90">{rev.reason}</span>
                      <span className="text-muted-foreground"> · {rev.actor} · {rev.at.slice(0, 16)}</span>
                    </button>
                  ))}
                </div>
                {diff && (
                  <div className="mt-2 max-h-48 overflow-auto rounded-lg border border-border/20 bg-background/30 p-2 font-mono text-[10px]">
                    {diff.map((line, i) => (
                      <p key={i} className={cn(line.type === "add" ? "text-emerald-300" : line.type === "remove" ? "text-rose-300" : "text-muted-foreground")}>
                        {line.type === "add" ? "+ " : line.type === "remove" ? "- " : "  "}{line.line}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Memories (explorer) ─────────────────────────────────────────────────────

function Memories({ isOperator }: { isOperator: boolean }) {
  const PAGE_SIZE = 50;
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [type, setType] = useState("all");
  const [status, setStatus] = useState("all");
  const [query, setQuery] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [showSave, setShowSave] = useState(false);
  const [conflictId, setConflictId] = useState<string | null>(null);
  const [draft, setDraft] = useState({ content: "", memoryType: "semantic", scope: "general", subject: "" });

  useEffect(() => {
    const timer = setTimeout(() => setSearchTerm(query.trim()), 300);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => { setOffset(0); }, [type, status, searchTerm]);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const page = await listMemoriesPage({
        memoryType: type === "all" ? undefined : type,
        status: status === "all" ? undefined : status,
        search: searchTerm || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setMemories(page.memories);
      setTotal(page.total);
    } catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, [type, status, searchTerm, offset]);
  useEffect(() => { void load(); }, [load]);

  const open = async (id: string) => { setDetail(await getMemory(id)); };

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { await fn(); setToast(message); await load(); if (detail) await open(detail.memory.id); }
    catch (err) { setToast(err instanceof Error ? err.message : "action failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 4000); }
  };

  const save = async () => {
    if (!draft.content.trim()) return;
    await act(
      () => createMemory({
        memoryType: draft.memoryType,
        content: draft.content,
        source: "manual",
        scope: draft.scope,
        subject: draft.subject || undefined,
      }),
      "Memory saved"
    );
    setShowSave(false);
    setDraft({ content: "", memoryType: "semantic", scope: "general", subject: "" });
  };

  return (
    <div className="animate-fade-in-up">
      <div className="mb-5 flex items-start justify-between gap-4">
        <Header
          title="Facts"
          hint="Durable, declarative facts recalled on relevant turns. A proposed fact is inert until you approve it; archived and superseded facts are retained for audit, never deleted. Task procedures belong in Skills; old conversations belong in Session History."
        />
        <Button size="sm" className="rounded-xl cursor-pointer shrink-0" disabled={!isOperator} onClick={() => setShowSave((v) => !v)}>
          {showSave ? <X size={13} className="mr-1" /> : <Plus size={13} className="mr-1" />}
          {showSave ? "Cancel" : "Save memory"}
        </Button>
      </div>
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}

      {showSave && (
        <div className="mb-4 rounded-2xl border border-primary/20 glass-card p-4 space-y-2">
          <textarea
            value={draft.content}
            onChange={(e) => setDraft({ ...draft, content: e.target.value })}
            rows={2}
            placeholder="A durable fact to remember (declarative, not an instruction)…"
            className="w-full rounded-xl border border-border/30 bg-background/60 px-3 py-2 text-[12px] outline-none focus:border-primary/40"
          />
          <div className="flex flex-wrap gap-2">
            <select value={draft.memoryType} onChange={(e) => setDraft({ ...draft, memoryType: e.target.value })}
              className="rounded-xl border border-border/30 bg-background/60 px-2 py-1.5 text-[11px] outline-none">
              {MEMORY_TYPES.filter((t) => t !== "all").map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select value={draft.scope} onChange={(e) => setDraft({ ...draft, scope: e.target.value })}
              className="rounded-xl border border-border/30 bg-background/60 px-2 py-1.5 text-[11px] outline-none">
              {["general", "user", "market", "task"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <Input value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
              placeholder="subject (optional)" className="w-40 rounded-xl bg-background/60" />
            <Button size="sm" className="rounded-xl cursor-pointer" disabled={busy || !draft.content.trim()} onClick={save}>Save</Button>
          </div>
        </div>
      )}

      <div className="mb-4 flex flex-col gap-3">
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search memory…" className="pl-9 rounded-xl bg-background/60" />
        </div>
        <FilterRow label="Type" options={MEMORY_TYPES} value={type} onChange={setType} />
        <FilterRow label="Status" options={STATUSES} value={status} onChange={setStatus} />
      </div>

      {loading ? <Spinner /> : error ? <ErrorBox message={error} /> : memories.length === 0 ? (
        <EmptyBox title="No memories match" hint="Memory comes from explicit saves, corrections and gated automatic learning. Financial facts and secrets are never stored." />
      ) : (
        <div className="space-y-2">
          {memories.map((memory) => (
            <button
              key={memory.id}
              onClick={() => open(memory.id)}
              className="w-full text-left rounded-2xl border border-white/10 dark:border-white/5 glass-card p-3.5 hover:border-primary/30 cursor-pointer"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <TypeBadge type={memory.memoryType} />
                    <StatusPill status={memory.status} />
                    <OriginBadge memory={memory} />
                    <span className="text-[9px] text-muted-foreground">scope: {memory.scope}{memory.scopeId ? `:${memory.scopeId}` : ""}</span>
                    {memory.subject && <span className="text-[9px] text-primary">#{memory.subject}</span>}
                  </div>
                  <p className="mt-1.5 text-[12px] text-foreground leading-relaxed line-clamp-2">{memory.content}</p>
                </div>
                <span className="shrink-0 text-[9px] text-muted-foreground">
                  {formatConfidence(memory.confidence)} · {memory.verification}
                </span>
              </div>
            </button>
          ))}
        </div>
      )}

      <ServerPaginationFooter
        total={total}
        limit={PAGE_SIZE}
        offset={offset}
        count={memories.length}
        onOffset={(next) => setOffset(Math.max(0, next))}
        label="facts"
        disabled={loading}
      />

      <MemoryDrawer
        open={Boolean(detail)}
        onClose={() => setDetail(null)}
        title="Fact detail"
        subtitle={detail ? `${detail.memory.memoryType} · ${detail.memory.status} · ${detail.memory.authority ?? "explicit"}` : undefined}
        width="md"
        footer={detail ? (
          <>
            <div className="flex flex-wrap gap-2">
              {detail.memory.status === "proposed" && (
                <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act(() => reviewMemory(detail.memory.id, true), "Memory approved")}>
                  Approve
                </Button>
              )}
              {(detail.memory.status === "archived" || detail.memory.status === "expired" || detail.memory.status === "superseded") && (
                <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act(() => restoreMemory(detail.memory.id), "Memory restored")}>
                  Restore
                </Button>
              )}
              {detail.memory.status !== "archived" && (
                <Button size="sm" variant="outline" className="rounded-xl cursor-pointer text-rose-300" disabled={!isOperator || busy}
                  onClick={() => act(() => archiveMemory(detail.memory.id), "Memory archived")}>
                  <Trash2 size={12} className="mr-1" /> Archive
                </Button>
              )}
            </div>
            {!isOperator && <p className="mt-2 text-[10px] text-amber-300/90">Read-only session — review/archive require operator authorization.</p>}
          </>
        ) : undefined}
      >
        {detail && (
          <>
            <div className="flex items-center gap-2 flex-wrap">
              <TypeBadge type={detail.memory.memoryType} />
              <StatusPill status={detail.memory.status} />
              <OriginBadge memory={detail.memory} />
            </div>
            <p className="mt-3 text-[12px] text-foreground leading-relaxed">{detail.memory.content}</p>
            {detail.explanation && (
              <div className="mt-3 rounded-2xl border border-white/10 glass-card-subtle p-3">
                <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Why this memory exists</p>
                <p className="mt-1 text-[11px] text-foreground/90">{detail.explanation.saveReason}</p>
                <p className="mt-1 text-[11px] text-foreground/90">{detail.explanation.recallDisposition}</p>
                {detail.explanation.statusReason && (
                  <p className="mt-1 text-[11px] text-amber-300/90">
                    Status: {detail.memory.status} — {detail.explanation.statusReason}
                  </p>
                )}
                {detail.memory.expiresAt && detail.memory.status === "expired" && (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    Expired {detail.memory.expiresAt}: automatic memories retire after the retention window; explicit memories never expire silently.
                  </p>
                )}
                {detail.memory.supersededBy && (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    Superseded by a later record ({detail.memory.supersededBy}) and excluded from recall; retained for audit.
                  </p>
                )}
              </div>
            )}
            <dl className="mt-4 text-[11px]">
              <Meta label="Scope" value={`${detail.memory.scope}${detail.memory.scopeId ? `:${detail.memory.scopeId}` : ""}`} />
              <Meta label="Subject" value={detail.memory.subject ?? "—"} />
              <Meta label="Source" value={detail.memory.source ?? "—"} />
              <Meta label="Run" value={detail.memory.sourceRunId ?? "—"} />
              <Meta label="Confidence" value={formatConfidence(detail.memory.confidence)} />
              <Meta label="Verification" value={detail.memory.verification} />
              <Meta label="Authority" value={detail.memory.authority ?? "explicit"} />
              <Meta label="Accesses" value={String(detail.memory.accessCount)} />
              <Meta label="Last used" value={detail.memory.lastUsedAt ?? "never"} />
              <Meta label="Last confirmed" value={detail.memory.lastConfirmedAt ?? "—"} />
              <Meta label="Expires" value={detail.memory.expiresAt ?? "never"} />
              <Meta label="Created" value={detail.memory.createdAt} />
            </dl>
            {detail.explanation?.conflict && (
              <div className="mt-3 rounded-2xl border border-amber-400/25 bg-amber-400/5 p-3">
                <p className="text-[10px] uppercase tracking-widest font-bold text-amber-300">Competing memory</p>
                <p className="mt-1 text-[11px] text-foreground/90">
                  {detail.explanation.conflict.reason}
                </p>
                <p className="mt-1 text-[10px] text-muted-foreground">
                  Detector: {detail.explanation.conflict.detector}
                  {detail.explanation.conflict.similarity != null ? ` · similarity ${detail.explanation.conflict.similarity.toFixed(2)}` : ""}
                  {detail.explanation.conflict.requiresConfirmation ? " · needs your review" : ""}
                </p>
                <Button size="sm" variant="outline" className="mt-2 rounded-xl cursor-pointer"
                  onClick={() => setConflictId(detail.memory.id)}>
                  Review conflict
                </Button>
              </div>
            )}

            {detail.evidence.length > 0 && (
              <div className="mt-4">
                <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Evidence</p>
                <ul className="mt-1 space-y-1">
                  {detail.evidence.map((ev) => (
                    <li key={ev.id} className="text-[10px] text-muted-foreground">
                      [{ev.sourceType}/{ev.verificationStatus}] {ev.sourceUrl ?? ev.sourceRef ?? "—"}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {detail.links.length > 0 && (
              <div className="mt-4">
                <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Links</p>
                <ul className="mt-1 space-y-1">
                  {detail.links.map((link) => (
                    <li key={link.id} className="text-[10px] text-muted-foreground">{link.relation}: {link.toId === detail.memory.id ? link.fromId : link.toId}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </MemoryDrawer>

      {conflictId && (
        <ConflictReview
          memoryId={conflictId}
          isOperator={isOperator}
          onClose={() => setConflictId(null)}
          onResolved={async (message) => {
            setToast(message);
            await load();
            if (detail) await open(detail.memory.id);
          }}
        />
      )}
    </div>
  );
}

// ── Conflict review (side-by-side competing memories + safe actions) ────────

function ConflictReview({
  memoryId, isOperator, onClose, onResolved,
}: {
  memoryId: string;
  isOperator: boolean;
  onClose: () => void;
  onResolved: (message: string) => void | Promise<void>;
}) {
  const [review, setReview] = useState<MemoryConflictReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [mergeText, setMergeText] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    memoryConflictReview(memoryId).then((r) => { if (!cancelled) { setReview(r); setLoading(false); } })
      .catch(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [memoryId]);

  const act = async (action: string, targetId?: string, mergedContent?: string) => {
    setBusy(true); setMessage(null);
    try {
      const result = await resolveMemoryConflict(memoryId, action, { targetId, mergedContent, reason: "resolved in conflict review" });
      if (!result.ok) { setMessage(result.error ?? "action failed"); return; }
      await onResolved(`Conflict ${action} recorded`);
      onClose();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "action failed");
    } finally { setBusy(false); }
  };

  const Row = ({ record, label }: { record: MemoryConflictReview["self"]; label: string }) => (
    <div className="rounded-2xl border border-white/10 glass-card-subtle p-3">
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-[9px] font-bold uppercase tracking-wide text-primary">{label}</span>
        <TypeBadge type={record.memoryType} />
        <StatusPill status={record.status} />
        <span className="text-[9px] text-muted-foreground">{record.authority}</span>
      </div>
      <p className="mt-1.5 text-[12px] text-foreground leading-relaxed">{record.content}</p>
      <p className="mt-1 text-[10px] text-muted-foreground">
        {record.scope}{record.scopeId ? `:${record.scopeId}` : ""} · {record.provenance ?? "—"} ·
        conf {formatConfidence(record.confidence)} · confirmed {record.lastConfirmedAt ?? "—"} ·
        used {record.accessCount ?? 0}×
      </p>
      {record.status === "active" && record.id !== memoryId && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          <button onClick={() => act("supersede", record.id)} disabled={!isOperator || busy}
            className="rounded-lg border border-border/40 px-2 py-0.5 text-[10px] cursor-pointer disabled:opacity-50">Supersede</button>
          <button onClick={() => act("dismiss", record.id)} disabled={!isOperator || busy}
            className="rounded-lg border border-border/40 px-2 py-0.5 text-[10px] cursor-pointer text-rose-300 disabled:opacity-50">Dismiss</button>
          <button onClick={() => act("confirm_both", record.id)} disabled={!isOperator || busy}
            className="rounded-lg border border-border/40 px-2 py-0.5 text-[10px] cursor-pointer text-emerald-300 disabled:opacity-50">Confirm both</button>
          <button onClick={() => { setMergeText(record.content); }} disabled={!isOperator || busy}
            className="rounded-lg border border-border/40 px-2 py-0.5 text-[10px] cursor-pointer disabled:opacity-50">Merge…</button>
        </div>
      )}
    </div>
  );

  return (
    <MemoryDrawer
      open
      onClose={onClose}
      title="Conflict review"
      subtitle="Resolve competing facts safely — every action is audited and reversible."
      width="lg"
      footer={review ? (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
            onClick={() => act("keep")}>Keep as-is</Button>
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
            onClick={() => act("defer")}>Defer review</Button>
        </div>
      ) : undefined}
    >
      {loading ? <Spinner /> : !review ? (
        <p className="text-[11px] text-muted-foreground">No conflict review available for this memory.</p>
      ) : (
        <>
          <p className="text-[11px] text-muted-foreground">{review.recommendation}</p>
          {message && <p className="mt-2 text-[11px] text-rose-300">{message}</p>}
          <div className="mt-3 space-y-2">
            <Row record={review.self} label="This memory" />
            {review.peers.length === 0 ? (
              <p className="text-[11px] text-muted-foreground">No competing record is linked.</p>
            ) : review.peers.map((peer) => <Row key={peer.id} record={peer} label="Competing" />)}
          </div>
          {mergeText && (
            <div className="mt-3 rounded-2xl border border-primary/20 glass-card p-3">
              <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground">Merge into a new fact</p>
              <textarea value={mergeText} onChange={(e) => setMergeText(e.target.value)} rows={3}
                className="mt-1 w-full rounded-xl border border-border/30 bg-background/60 px-3 py-2 text-[12px] outline-none focus:border-primary/40" />
              <div className="mt-2 flex gap-2">
                <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                  onClick={() => act("merge", review.peers.find((p) => p.status === "active")?.id ?? review.peers[0]?.id, mergeText)}>
                  Create merged fact
                </Button>
                <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={() => setMergeText("")}>Cancel</Button>
              </div>
            </div>
          )}
          <p className="mt-3 text-[10px] text-muted-foreground">
            Every action is audited and reversible; no record is hard-deleted. Semantic
            similarity never resolves a conflict automatically.
          </p>
        </>
      )}
    </MemoryDrawer>
  );
}

// ── Suggestions (inert Save/Dismiss proposals) ──────────────────────────────

function Suggestions({ isOperator }: { isOperator: boolean }) {
  const [suggestions, setSuggestions] = useState<MemorySuggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const PAGE_SIZE = 25;
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const page = await listSuggestionsPage("pending", PAGE_SIZE, offset);
      setSuggestions(page.items);
      setTotal(page.total);
    }
    catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, [offset]);
  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { await fn(); setToast(message); await load(); }
    catch (err) { setToast(err instanceof Error ? err.message : "action failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 4000); }
  };

  return (
    <div className="animate-fade-in-up">
      <Header
        title="Memory Suggestions"
        hint="A suggestion is NOT memory: nothing is stored or recalled until you Save it. Dismissed suggestions are remembered so the same item is not offered again."
      />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}
      {loading ? <Spinner /> : error ? <ErrorBox message={error} /> : suggestions.length === 0 ? (
        <EmptyBox title="No pending suggestions" hint="Suggestions appear only when automatic suggestions are enabled and a casually-stated durable preference is detected." />
      ) : (
        <div className="space-y-2">
          {suggestions.map((s) => (
            <div key={s.id} className="rounded-2xl border border-white/10 glass-card p-3.5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <TypeBadge type={s.memoryType} />
                    <span className="text-[9px] text-muted-foreground">{s.scope}{s.scopeId ? `:${s.scopeId}` : ""}</span>
                    <span className="text-[9px] text-muted-foreground">{s.createdAt.slice(0, 10)}</span>
                  </div>
                  <p className="mt-1 text-[12px] text-foreground/90">{s.content}</p>
                  {s.reason && <p className="mt-0.5 text-[10px] text-muted-foreground/80">{s.reason}</p>}
                </div>
                <div className="flex shrink-0 gap-1.5">
                  <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                    onClick={() => act(() => acceptSuggestion(s.id), "Saved to memory")}>Save</Button>
                  <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                    onClick={() => act(() => dismissSuggestion(s.id), "Suggestion dismissed")}>Dismiss</Button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      <ServerPaginationFooter
        total={total}
        limit={PAGE_SIZE}
        offset={offset}
        count={suggestions.length}
        onOffset={(next) => setOffset(Math.max(0, next))}
        label="suggestions"
        disabled={loading}
      />
      {!isOperator && <p className="mt-3 text-[10px] text-amber-300/90">Read-only session — saving or dismissing requires operator authorization.</p>}
    </div>
  );
}

// ── Activity (retrieval / corrections / mistakes) ───────────────────────────

function ActivityView() {
  const [corrections, setCorrections] = useState<CorrectionRecord[]>([]);
  const [mistakes, setMistakes] = useState<MistakeRecord[]>([]);
  const [diag, setDiag] = useState<MemoryRetrievalDiagnostics | null>(null);
  const [probe, setProbe] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const PAGE_SIZE = 10;
  const [correctionsOffset, setCorrectionsOffset] = useState(0);
  const [correctionsTotal, setCorrectionsTotal] = useState(0);
  const [mistakesOffset, setMistakesOffset] = useState(0);
  const [mistakesTotal, setMistakesTotal] = useState(0);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [c, m] = await Promise.all([
        listCorrectionsPage(PAGE_SIZE, correctionsOffset),
        listMistakesPage(PAGE_SIZE, mistakesOffset),
      ]);
      setCorrections(c.items); setCorrectionsTotal(c.total);
      setMistakes(m.items); setMistakesTotal(m.total);
    } catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, [correctionsOffset, mistakesOffset]);
  useEffect(() => { void load(); }, [load]);

  const runProbe = async () => {
    if (!probe.trim()) return;
    try { setDiag((await retrieveMemories({ query: probe, limit: 8 })).diagnostics); }
    catch (err) { setError(err instanceof Error ? err.message : "retrieval failed"); }
  };

  if (loading) return <Spinner />;
  if (error) return <ErrorBox message={error} />;

  return (
    <div className="animate-fade-in-up">
      <Header title="Activity & Diagnostics" hint="Corrections and mistakes are captured records. The retriever probe shows exactly what would be recalled and why." />

      <div className="rounded-2xl border border-white/10 glass-card p-4 mb-3">
        <p className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground mb-2">Retrieval probe</p>
        <div className="flex gap-2">
          <Input value={probe} onChange={(e) => setProbe(e.target.value)} placeholder="What would be recalled for…" className="rounded-xl bg-background/60" />
          <Button size="sm" className="rounded-xl cursor-pointer" onClick={runProbe}>Run</Button>
        </div>
        {diag && (
          <div className="mt-3 text-[11px] text-muted-foreground">
            <p>{diag.selected} selected of {diag.candidates} candidates · {diag.usedChars}/{diag.budgetChars} chars · {diag.durationMs}ms</p>
            <ul className="mt-1 list-disc list-inside space-y-0.5">
              {diag.selectionReasons.slice(0, 6).map((reason, i) => <li key={i}>{reason}</li>)}
              {diag.excluded.slice(0, 4).map((item, i) => <li key={`x${i}`}>excluded {item.id}: {item.reason}</li>)}
            </ul>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Section title={`Corrections (${correctionsTotal})`} icon={Wrench}>
          {corrections.length === 0 ? <Muted>None recorded.</Muted> : corrections.map((c) => (
            <div key={c.id} className="rounded-xl border border-border/20 bg-background/30 p-2.5 mb-2">
              <p className="text-[11px] text-foreground">{c.subject ?? "general"}: {c.wrong} → {c.correction}</p>
              <p className="mt-1 text-[9px] text-muted-foreground">{c.providedBy} · {c.scope} · generalizable: {c.generalizable ? "yes" : "no"}</p>
            </div>
          ))}
          <ServerPaginationFooter
            total={correctionsTotal}
            limit={PAGE_SIZE}
            offset={correctionsOffset}
            count={corrections.length}
            onOffset={setCorrectionsOffset}
            label="corrections"
            disabled={loading}
          />
        </Section>
        <Section title={`Mistakes (${mistakesTotal})`} icon={AlertCircle}>
          {mistakes.length === 0 ? <Muted>None captured.</Muted> : mistakes.map((m) => (
            <div key={m.id} className="rounded-xl border border-border/20 bg-background/30 p-2.5 mb-2">
              <p className="text-[11px] text-foreground">{m.category} · {m.severity}</p>
              <p className="mt-1 text-[9px] text-muted-foreground">{m.expected} → {m.actual} · reusable: {m.reusable ? "yes" : "no"}</p>
            </div>
          ))}
          <ServerPaginationFooter
            total={mistakesTotal}
            limit={PAGE_SIZE}
            offset={mistakesOffset}
            count={mistakes.length}
            onOffset={setMistakesOffset}
            label="mistakes"
            disabled={loading}
          />
        </Section>
      </div>
    </div>
  );
}


// ── Health + Transfer ───────────────────────────────────────────────────────

function Health({ isOperator }: { isOperator: boolean }) {
  const [health, setHealth] = useState<EclipseMemoryHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dbHealth, setDbHealth] = useState<MemoryDatabaseHealth | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);
  const [pendingImport, setPendingImport] = useState<MemoryExportDocument | null>(null);
  const [importPreview, setImportPreview] = useState<{
    imported?: number; importable?: number; skipped?: number;
    rejected?: unknown[]; checksumOk?: boolean | null;
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [healthBody, dbHealthBody] = await Promise.all([
        memoryHealth(),
        memoryDatabaseHealth(),
      ]);
      setHealth(healthBody);
      setDbHealth(dbHealthBody);
    }
    catch (err) { setError(err instanceof Error ? err.message : "failed"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { const result = await fn(); setToast(message); await load(); return result; }
    catch (err) { setToast(err instanceof Error ? err.message : "action failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 5000); }
  };

  const downloadExport = async () => {
    setBusy(true);
    try {
      const payload = await exportMemory();
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      saveBlob(`eclipse-memory-export-${new Date().toISOString().slice(0, 10)}.json`, blob);
      setToast(`Exported ${payload.counts.memories} memories`);
    } catch (err) { setToast(err instanceof Error ? err.message : "export failed"); }
    finally { setBusy(false); setTimeout(() => setToast(null), 5000); }
  };

  if (loading) return <Spinner />;
  if (error) return <ErrorBox message={error} />;
  if (!health) return null;

  return (
    <div className="animate-fade-in-up">
      <Header title="Database Health & Recovery" hint="eclipse-memory is a separate SQLite database with its own schema and migrations. Export, back up, import and reset are local operator actions." />
      {toast && <p className="mb-3 text-[11px] text-primary">{toast}</p>}
      <div className="rounded-2xl border border-white/10 glass-card p-4">
        <div className="flex items-center gap-2">
          <span className={cn("h-2 w-2 rounded-full", health.ok ? "bg-emerald-400" : "bg-rose-400")} />
          <span className="text-sm font-semibold text-foreground">{health.ok ? "Healthy" : "Unhealthy"}</span>
        </div>
        <dl className="mt-3 text-[11px]">
          <Meta label="Path" value={health.path} />
          <Meta label="Integrity" value={health.integrity ?? "—"} />
          <Meta label="Foreign-key violations" value={String(dbHealth?.foreignKeyViolations ?? "—")} />
          <Meta label="Free-page ratio" value={dbHealth?.freeRatio !== undefined ? String(dbHealth.freeRatio) : "—"} />
          <Meta label="Schema version" value={String(health.schemaVersion ?? "—")} />
          <Meta label="Up to date" value={health.upToDate ? "yes" : "no"} />
          <Meta label="Tables" value={String(health.tables ?? "—")} />
          {health.counts && Object.entries(health.counts).map(([k, v]) => <Meta key={k} label={k} value={String(v)} />)}
        </dl>
      </div>

      <div className="mt-4 rounded-2xl border border-white/10 glass-card p-4">
        <p className="text-[11px] font-semibold text-foreground">Export & Backup</p>
        <p className="mt-1 text-[10px] text-muted-foreground">A readable JSON export you own, plus a consistent copy of the database file. Backups never overwrite an existing file.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={busy} onClick={downloadExport}>
            Download JSON export
          </Button>
          <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
            onClick={() => act(() => backupMemory(), "Backup created")}>
            Back up database
          </Button>
        </div>
      </div>

      <div className="mt-4 rounded-2xl border border-white/10 glass-card p-4">
        <p className="text-[11px] font-semibold text-foreground">Import</p>
        <p className="mt-1 text-[10px] text-muted-foreground">Records are validated and written through the normal memory pipeline; secrets and unsafe content are rejected with reasons.</p>
        <input ref={importInputRef} type="file" accept="application/json,.json" className="hidden"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (!file) return;
            try {
              const document = JSON.parse(await file.text()) as MemoryExportDocument;
              setPendingImport(document);
              setImportPreview(null);
            } catch (err) {
              setToast(err instanceof Error ? err.message : "invalid file");
            }
          }}
        />
        <Button size="sm" variant="outline" className="mt-3 rounded-xl cursor-pointer" disabled={!isOperator || busy}
          onClick={() => importInputRef.current?.click()}>
          Choose JSON file…
        </Button>
        {pendingImport && (
          <div className="mt-3 rounded-xl border border-primary/20 glass-card-subtle p-2.5">
            <p className="text-[10px] text-muted-foreground">Selected file — preview before applying (dry run validates and counts, writes nothing).</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                onClick={async () => {
                  try {
                    const report = await importMemory(pendingImport, true);
                    setImportPreview(report);
                  } catch (err) { setToast(err instanceof Error ? err.message : "preview failed"); }
                }}>
                Preview (dry run)
              </Button>
              <Button size="sm" className="rounded-xl cursor-pointer" disabled={!isOperator || busy}
                onClick={async () => {
                  const result = await act(() => importMemory(pendingImport), "Import complete");
                  if (result && typeof result === "object" && "rejected" in result) {
                    const rejected = (result as { rejected: unknown[] }).rejected;
                    if (rejected.length) setToast(`Import finished — ${rejected.length} record(s) rejected`);
                  }
                  setPendingImport(null); setImportPreview(null);
                }}>
                Apply import
              </Button>
              <Button size="sm" variant="ghost" className="rounded-xl cursor-pointer" onClick={() => { setPendingImport(null); setImportPreview(null); }}>
                Cancel
              </Button>
            </div>
            {importPreview && (
              <p className="mt-2 text-[10px] text-foreground/80">
                Would import {importPreview.importable ?? importPreview.imported ?? 0}
                {(importPreview.skipped ?? 0) > 0 ? `, skip ${importPreview.skipped} duplicate(s)` : ""}
                {(importPreview.rejected?.length ?? 0) > 0 ? `, reject ${importPreview.rejected?.length}` : ""}
                {importPreview.checksumOk === false ? " · checksum mismatch" : importPreview.checksumOk ? " · checksum ok" : ""}
              </p>
            )}
          </div>
        )}
      </div>

      <div className="mt-4 rounded-2xl border border-rose-400/20 glass-card p-4">
        <p className="text-[11px] font-semibold text-rose-300">Reset all memory</p>
        <p className="mt-1 text-[10px] text-muted-foreground">Deletes memory records, suggestions, corrections and audit events only. Skills, learning candidates and unrelated application data are never touched. A backup is taken first.</p>
        {confirmReset ? (
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" variant="outline" className="rounded-xl cursor-pointer text-rose-300" disabled={!isOperator || busy}
              onClick={async () => { await act(() => resetMemory(true), "Memory reset (backup taken)"); setConfirmReset(false); }}>
              Confirm reset
            </Button>
            <Button size="sm" variant="outline" className="rounded-xl cursor-pointer" onClick={() => setConfirmReset(false)}>Cancel</Button>
          </div>
        ) : (
          <Button size="sm" variant="outline" className="mt-3 rounded-xl cursor-pointer text-rose-300" disabled={!isOperator || busy}
            onClick={() => setConfirmReset(true)}>
            Reset memory…
          </Button>
        )}
      </div>

      <Button variant="outline" size="sm" className="mt-4 rounded-xl cursor-pointer" onClick={load}>
        <RefreshCcw size={13} className="mr-1" /> Refresh
      </Button>
    </div>
  );
}
