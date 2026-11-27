"use client";

// ── General settings panel ────────────────────────────────────────────────
// The "General" tab of the merged General/Customization surface. Grouped into
// titled sections so related preferences sit together instead of one long mix:
//
//   * Startup & Display — the server-backed default market / refresh interval
//     and the local-only display currency / timezone. (The app always boots on
//     the Eclipse Agent page, so there is no start-screen control.)
//   * Live Data         — how often dashboards re-fetch prices.
//   * Safety            — destructive-action confirmation (localStorage via
//     `general-prefs`).
//
// The ChatBot page backdrop/wordmark now lives on Customization › ChatBot Page.
//
// It is self-contained: the parent passes the values it already fetched and a
// persist callback, keeping the merge additive and the parent shell thin.

import { useEffect, useState } from "react";
import { alertDialog, confirmDialog } from "@/lib/ui/confirm-dialog";
import { Loader2, Download, Monitor, RefreshCcw, RotateCcw, ShieldCheck, Sparkles } from "@/lib/icons";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { DISPLAY_CURRENCIES, getDisplayCurrency, setDisplayCurrency } from "@/lib/currency";
import { DISPLAY_TIMEZONES, browserTimeZone, getDisplayTimeZone, isValidTimeZone, setDisplayTimeZone } from "@/lib/timezone";
import { HEATMAP_MARKETS } from "@/lib/heatmap";
import {
  DEFAULT_GENERAL_PREFS, loadGeneralPrefs, saveGeneralPrefs, type GeneralPrefs,
} from "@/lib/general-prefs";
import {
  DEFAULT_UPDATE_PREFS, loadUpdatePrefs, saveUpdatePrefs, type UpdatePrefs,
} from "@/lib/updates/prefs";
import type { UpdateStatus } from "@/lib/updates/types";
import { useWindowGlass } from "@/lib/desktop/window-glass";
import { useTaskbarBadgeEnabled } from "@/lib/desktop/taskbar";
import { Field, Panel, SubSection, Toggle } from "./settings-ui";

export interface GeneralAppPreferences {
  defaultMarket: string;
  refreshMs: number;
}

// Fallback market list, used only when /api/markets has not answered yet. It is
// the full supported universe (not a hardcoded few) so the selector is never
// silently narrowed to 4 markets.
const ALL_MARKETS = HEATMAP_MARKETS.map((m) => ({ id: m.id, label: m.country }));

const REFRESH_OPTIONS = [10000, 15000, 30000, 60000];

export function GeneralSettingsPanel({
  markets,
  prefs,
  onPrefsChange,
  onGeneralChange,
}: {
  /** Markets loaded from JDDS; empty falls back to the built-in list. */
  markets: { id: string; country: string }[];
  prefs: GeneralAppPreferences;
  onPrefsChange: (patch: Partial<GeneralAppPreferences>) => void;
  /** Notify the shell so its in-memory copy (destructive-clear gate) stays fresh. */
  onGeneralChange?: (prefs: GeneralPrefs) => void;
}) {
  const [general, setGeneral] = useState<GeneralPrefs>(DEFAULT_GENERAL_PREFS);
  useEffect(() => { setGeneral(loadGeneralPrefs()); }, []);
  const windowGlass = useWindowGlass();
  const taskbarBadge = useTaskbarBadgeEnabled();
  const updateGeneral = (patch: Partial<GeneralPrefs>) => {
    setGeneral((g) => {
      const next = { ...g, ...patch };
      saveGeneralPrefs(next);
      onGeneralChange?.(next);
      return next;
    });
  };

  // Updates are a separate local-only pref (be_update_prefs_v1): the banner and
  // this panel share it, so turning the check off here stops the banner too.
  const [updatePrefs, setUpdatePrefs] = useState<UpdatePrefs>(DEFAULT_UPDATE_PREFS);
  const [updateCheck, setUpdateCheck] = useState<{
    busy: boolean;
    status: UpdateStatus | null;
    error: string | null;
  }>({ busy: false, status: null, error: null });
  useEffect(() => { setUpdatePrefs(loadUpdatePrefs()); }, []);
  const setUpdateEnabled = (enabled: boolean) => {
    const next = { ...updatePrefs, enabled };
    setUpdatePrefs(next);
    saveUpdatePrefs(next);
    if (!enabled) setUpdateCheck({ busy: false, status: null, error: null });
  };
  const checkForUpdates = async () => {
    setUpdateCheck({ busy: true, status: null, error: null });
    try {
      const res = await fetch("/api/updates?refresh=1", { cache: "no-store" });
      const data = (await res.json()) as UpdateStatus;
      if (!res.ok) throw new Error("Could not check for updates.");
      setUpdateCheck({ busy: false, status: data, error: null });
    } catch (err) {
      setUpdateCheck({
        busy: false,
        status: null,
        error: err instanceof Error ? err.message : "Could not check for updates.",
      });
    }
  };
  const updateResult = (() => {
    if (updateCheck.busy) return "Checking…";
    if (updateCheck.error) return updateCheck.error;
    const status = updateCheck.status;
    if (!status) return "";
    if (!status.ok) return status.error || "Could not check for updates.";
    if (status.available) {
      return `v${status.latestVersion} is available — you're on v${status.currentVersion}.`;
    }
    return `You're up to date (v${status.currentVersion}).`;
  })();

  const [currency, setCurrency] = useState<string>("USD");
  useEffect(() => { setCurrency(getDisplayCurrency()); }, []);
  const applyCurrency = (code: string | null) => {
    if (!code) return;
    setCurrency(code);
    setDisplayCurrency(code);
  };

  const [timeZone, setTimeZone] = useState<string>("UTC");
  useEffect(() => { setTimeZone(getDisplayTimeZone()); }, []);
  const applyTimeZone = (tz: string | null) => {
    if (!tz || !isValidTimeZone(tz)) return;
    setTimeZone(tz);
    setDisplayTimeZone(tz);
  };

  const [setupBusy, setSetupBusy] = useState(false);
  const restartOnboarding = async () => {
    if (general.confirmDestructive) {
      const ok = await confirmDialog({
        title: "Open the Eclipse setup questions again?",
        description: "Your API keys, chats and portfolio are kept.",
      });
      if (!ok) return;
    }
    setSetupBusy(true);
    try {
      const res = await fetch("/api/setup/reset", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ scope: "onboarding" }),
      });
      const data = (await res.json()) as { reload?: string; error?: string };
      if (!res.ok) throw new Error(data.error || "Could not restart setup");
      window.location.assign(data.reload || "/?setup=1");
    } catch (err) {
      await alertDialog({ title: err instanceof Error ? err.message : "Could not restart setup" });
      setSetupBusy(false);
    }
  };

  const resetAll = () => {
    setDisplayCurrency("USD");
    setCurrency("USD");
    setDisplayTimeZone("UTC");
    setTimeZone("UTC");
    onPrefsChange({ defaultMarket: "US", refreshMs: 15000 });
    updateGeneral(DEFAULT_GENERAL_PREFS);
  };

  return (
    <div className="animate-fade-in-up space-y-6">
      {/* Startup & Display — how the app opens and renders values. */}
      <SubSection icon={Monitor} title="Startup & Display">
        <Panel className="p-5 md:p-6 space-y-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Default Market">
              <Select value={prefs.defaultMarket} onValueChange={(v) => onPrefsChange({ defaultMarket: v ?? "US" })}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {markets.length > 0
                    ? markets.map((m) => <SelectItem key={m.id} value={m.id}>{m.country}</SelectItem>)
                    : ALL_MARKETS.map((m) => <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
          </div>
          <Field label="Display Currency" hint="All portfolio totals, charts and analytics show in this currency. Individual asset prices keep their native market currency.">
            <Select value={currency} onValueChange={applyCurrency}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DISPLAY_CURRENCIES.map((c) => (
                  <SelectItem key={c.code} value={c.code}>{c.code} — {c.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field
            label="Display Timezone"
            hint="Timestamps across the app (including Investor Eye event times) render in this zone. The zone is always printed with the time."
          >
            <Select value={timeZone} onValueChange={applyTimeZone}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={browserTimeZone()}>Device timezone — {browserTimeZone()}</SelectItem>
                {DISPLAY_TIMEZONES.filter((t) => t.id !== browserTimeZone()).map((t) => (
                  <SelectItem key={t.id} value={t.id}>{t.label} — {t.id}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </Panel>
      </SubSection>

      {/* Desktop-only: the native shell applies acrylic behind a transparent
          window; this switch controls the app-side translucency. */}
      {(windowGlass.available || taskbarBadge.available) && (
        <SubSection icon={Monitor} title="Window">
          <Panel className="p-5 md:p-6">
            {windowGlass.available && (
              <div className="flex items-center justify-between gap-4">
                <div className="min-w-0">
                  <p className="text-sm font-medium">Glass window</p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">
                    Translucent, blurred window over your desktop. The left sidebar stays solid.
                  </p>
                </div>
                <Toggle checked={windowGlass.enabled} onChange={windowGlass.setEnabled} />
              </div>
            )}
            {windowGlass.available && taskbarBadge.available && (
              <div className="my-4 h-px bg-white/10 dark:bg-white/5" />
            )}
            {taskbarBadge.available && (
              <div className="flex items-center justify-between gap-4">
                <div className="min-w-0">
                  <p className="text-sm font-medium">Taskbar activity</p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">
                    A red badge with the unread count when something arrives while you are
                    elsewhere, and a progress bar while Eclipse is working. Closing the window
                    keeps Eclipse in the taskbar.
                  </p>
                </div>
                <Toggle checked={taskbarBadge.enabled} onChange={taskbarBadge.setEnabled} />
              </div>
            )}
          </Panel>
        </SubSection>
      )}

      {/* Live Data — polling cadence for dashboards. */}
      <SubSection icon={RefreshCcw} title="Live Data">
        <Panel className="p-5 md:p-6">
          <Field label="Live Refresh" hint="How often dashboards re-fetch prices.">
            <Select value={String(prefs.refreshMs)} onValueChange={(v) => onPrefsChange({ refreshMs: Number(v) })}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {REFRESH_OPTIONS.map((ms) => (
                  <SelectItem key={ms} value={String(ms)}>{ms / 1000}s</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </Panel>
      </SubSection>

      {/* Setup — re-run the first-run questions without touching data. */}
      <SubSection icon={Sparkles} title="Setup">
        <Panel className="p-5 md:p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <Label className="text-xs text-muted-foreground font-medium">Run setup again</Label>
              <p className="text-[10px] text-muted-foreground/70 mt-0.5">
                Reopens the welcome, AI and data questions. Keys, chats, portfolio and research
                are kept; you can change any answer.
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              className="cursor-pointer rounded-xl"
              disabled={setupBusy}
              onClick={() => void restartOnboarding()}
            >
              {setupBusy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              Run setup again
            </Button>
          </div>
        </Panel>
      </SubSection>

      {/* Safety — destructive-action guardrails. */}
      <SubSection icon={ShieldCheck} title="Behavior & Safety">
        <Panel className="p-5 md:p-6">
          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0">
              <Label className="text-xs text-muted-foreground font-medium">Confirm before clearing data</Label>
              <p className="text-[10px] text-muted-foreground/70 mt-0.5">
                Ask first before wiping local chats, watchlist or portfolio from the Data &amp; Storage tab.
              </p>
            </div>
            <Toggle
              checked={general.confirmDestructive}
              onChange={(v) => updateGeneral({ confirmDestructive: v })}
            />
          </div>
        </Panel>
      </SubSection>

      {/* Updates — signed release notice + manual check. Local-only pref. */}
      <SubSection icon={Download} title="Updates">
        <Panel className="p-5 md:p-6 space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0">
              <Label className="text-xs text-muted-foreground font-medium">Check for updates</Label>
              <p className="text-[10px] text-muted-foreground/70 mt-0.5">
                Eclipse asks a public GitHub release for a signed version file. No account, install
                ID or local data is sent.
              </p>
            </div>
            <Toggle checked={updatePrefs.enabled} onChange={setUpdateEnabled} />
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-white/10 pt-3">
            <Button
              variant="outline"
              size="sm"
              className="cursor-pointer rounded-xl"
              disabled={!updatePrefs.enabled || updateCheck.busy}
              onClick={() => void checkForUpdates()}
            >
              {updateCheck.busy ? (
                <Loader2 size={14} className="mr-1 animate-spin" />
              ) : (
                <RefreshCcw size={14} className="mr-1" />
              )}
              Check now
            </Button>
            {updateResult && (
              <p className="text-[11px] text-muted-foreground">{updateResult}</p>
            )}
          </div>
          <p className="text-[10px] text-muted-foreground/70">
            To install an update, stop Eclipse and run <code className="font-mono">black update</code>{" "}
            in the project folder. It backs up your database, verifies the release, and rolls back
            if the build fails.
          </p>
        </Panel>
      </SubSection>

      {/* Background + landing wordmark moved to Customization › ChatBot Page,
          alongside the conversation font, so every ChatBot-page look lives in
          one place. */}

      <div className="flex justify-end">
        <Button variant="outline" size="sm" className="cursor-pointer rounded-xl" onClick={resetAll}>
          <RotateCcw className="w-3.5 h-3.5 mr-1" /> Reset General to defaults
        </Button>
      </div>
    </div>
  );
}
