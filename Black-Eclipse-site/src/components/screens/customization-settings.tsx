"use client";

// â”€â”€ Settings â€º Customization â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// The centralized theme/customization surface. Everything here flows through
// the Eclipse theme store (`@/lib/theme`), which flattens the theme to CSS
// design tokens on <html> â€” so changes recolor the entire app (chrome, cards,
// charts, widgets) with no component-specific hacks.
//
// Editing model:
//   * All controls edit a local *draft* theme, so you can explore without
//     committing anything.
//   * With "Live preview" on, the draft is pushed to the document (unpersisted)
//     as you edit, so the WHOLE app can be auditioned. A small inline preview
//     card also shows the draft in isolation.
//   * "Apply" persists the draft; "Discard" restores the saved theme.
//
// Tabs: Theme Â· Glass & Surfaces Â· Aurora Â· Charts Â· ChatBot Page Â· Language & Access.

import { useEffect, useRef, useState } from "react";
import { saveBlob } from "@/lib/desktop/download";
import {
  Accessibility, Languages, RotateCcw, Palette, Image as ImageIcon,
  Type, SlidersHorizontal, Sparkles, Check, Sun, AlertTriangle,
  Wand2, Activity, Layers, Square, Plus, Trash2, Pencil, Download, Upload,
  Eye, EyeOff, Save, PanelLeft, MessageSquareText,
} from "@/lib/icons";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { DEFAULT_A11Y, LOCALES, useLocaleStore, useT, type LocaleCode } from "@/lib/i18n";
import { useThrottledCommit } from "@/hooks/use-throttled-commit";
import {
  DENSITY_OPTIONS, GLASS_PRESETS, HEADING_FONT_OPTIONS, MONO_FONT_OPTIONS,
  SHADOW_OPTIONS, SIDEBAR_PRESETS, THEME_PRESETS, UI_FONT_OPTIONS,
  clearChartPalettePreview, deriveThemeColors, isValidHex, normalizeHex, normalizeTheme,
  syncChartPaletteFromTheme, themeFromBrandColor,
  useThemeStore, type EclipseTheme, type ThemeColors, type ThemeGlass, type ThemeSidebar,
  chartPaletteFromTheme,
} from "@/lib/theme";
import { useChartPaletteStore } from "@/lib/chart-palette-store";
import type { ChartPalette } from "@/lib/chart-palette";
import { useAuroraPaletteStore } from "@/lib/aurora/store";
import { auroraFromColors } from "@/lib/theme";
import type { AuroraPalette } from "@/lib/aurora-sentiment";
import {
  normalizePresetBackground,
  useBackgroundStore,
  type BackgroundPrefs,
} from "@/lib/backgrounds";
import { AuroraSettingsPanel } from "./aurora-settings";
import { ChartSettingsPanel } from "./chart-settings";
import { ChatbotPageSettings } from "./chatbot-page-settings";
import { PreviewLauncher } from "./customization-preview";
import { SubSection, Toggle } from "./settings-ui";
import { AppWideThemePresets } from "./app-wide-theme-presets";
import { PresetTile, ThemePreview, rgbTriplet } from "./theme-preview";

type CustomTab = "general" | "theme" | "glass" | "aurora" | "charts" | "chatbot" | "access";

/** The live ChatBot backdrop, captured for export / preset save. */
function currentBackgroundSnapshot(): Partial<BackgroundPrefs> {
  return { ...useBackgroundStore.getState().prefs };
}

function Row({
  label, hint, checked, onChange,
}: { label: string; hint: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <Label className="text-xs text-muted-foreground font-medium">{label}</Label>
        <p className="text-[10px] text-muted-foreground/70 mt-0.5">{hint}</p>
      </div>
      <Toggle checked={checked} onChange={onChange} />
    </div>
  );
}

function Card({
  icon: Icon, title, children,
}: { icon: React.ElementType; title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-5 rounded-2xl border border-white/10 dark:border-white/5 glass-card p-5 md:p-6">
      <div className="flex items-center gap-2 text-xs font-semibold text-foreground/80">
        <Icon className="w-4 h-4 text-primary shrink-0" />
        <span>{title}</span>
      </div>
      {children}
    </div>
  );
}

/** Labelled range slider with a live value readout. */
function Slider({
  label, value, min, max, step, format, onChange, disabled,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <Label className="text-xs text-muted-foreground font-medium">{label}</Label>
        <span className="text-[10px] font-mono text-muted-foreground">
          {format ? format(value) : value}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full cursor-pointer disabled:opacity-40"
        aria-label={label}
      />
    </div>
  );
}

/** Native color swatch + hex input pair. */
function ColorField({
  label, hint, value, onChange,
}: { label: string; hint?: string; value: string; onChange: (hex: string) => void }) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const valid = isValidHex(text);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // A native color picker fires `input` continuously while the user drags â€” far
  // more often than the app can repaint. Applying the theme on every event
  // restyles the whole glass-heavy UI each time, so events pile up faster than
  // frames are produced and the picker appears to stick. The shared hook keeps
  // the swatch local (instant) and rate-limits the app-wide preview, always
  // applying the latest value, so work can never queue.
  const { emit, flush } = useThrottledCommit<string>((hex) => onChangeRef.current(hex), 100);

  const handleText = (next: string) => {
    setText(next);
    const n = normalizeHex(next);
    if (n) emit(n);
  };
  return (
    <div className="flex items-center gap-3 min-w-0">
      <label
        className="relative w-9 h-9 shrink-0 rounded-xl border border-white/15 cursor-pointer overflow-hidden shadow-sm"
        style={{ backgroundColor: valid ? text : value }}
      >
        <input
          type="color"
          value={normalizeHex(text) ?? value}
          onChange={(e) => { setText(e.target.value); emit(e.target.value); }}
          onBlur={flush}
          className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
          aria-label={`${label} color picker`}
        />
      </label>
      <div className="flex flex-col min-w-0">
        <span className="text-xs font-medium text-foreground">{label}</span>
        {hint && <span className="text-[10px] text-muted-foreground truncate">{hint}</span>}
      </div>
      <input
        value={text}
        onChange={(e) => handleText(e.target.value)}
        spellCheck={false}
        className={cn(
          "ml-auto w-[92px] shrink-0 rounded-lg border bg-transparent px-2 py-1 font-mono text-[11px] uppercase outline-none transition-colors",
          valid ? "border-border/40 focus-visible:border-primary/50" : "border-rose-500/50 text-rose-400",
        )}
        aria-label={`${label} hex value`}
      />
    </div>
  );
}

/** Reads an image file into a data URL, capped to keep localStorage healthy. */
function useImagePicker(onPick: (dataUrl: string) => void) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const open = () => inputRef.current?.click();
  const node = (
    <input
      ref={inputRef}
      type="file"
      accept="image/png,image/jpeg,image/svg+xml,image/webp,image/x-icon"
      className="hidden"
      onChange={(e) => {
        const file = e.target.files?.[0];
        e.target.value = "";
        if (!file) return;
        if (file.size > 300_000) {
          setError("Image is too large (max 300 KB). Use a smaller file or a URL.");
          return;
        }
        const reader = new FileReader();
        reader.onload = () => {
          if (typeof reader.result === "string") { setError(null); onPick(reader.result); }
        };
        reader.readAsDataURL(file);
      }}
    />
  );
  return { open, node, error };
}

function ImageField({
  label, hint, value, onChange,
}: { label: string; hint: string; value: string; onChange: (v: string) => void }) {
  const { open, node, error } = useImagePicker(onChange);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <Label className="text-xs text-muted-foreground font-medium">{label}</Label>
          <p className="text-[10px] text-muted-foreground/70 mt-0.5">{hint}</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-border/40 bg-muted/20 overflow-hidden">
            {value
              ? <img src={value} alt="" className="h-full w-full object-contain" />
              : <ImageIcon className="w-4 h-4 text-muted-foreground" />}
          </span>
          <Button variant="outline" size="sm" className="rounded-xl cursor-pointer" onClick={open}>Upload</Button>
          {value && (
            <Button variant="ghost" size="sm" className="rounded-xl cursor-pointer" onClick={() => onChange("")}>Clear</Button>
          )}
        </div>
      </div>
      <Input
        value={value.startsWith("data:") ? "" : value}
        placeholder={value.startsWith("data:") ? "Uploaded image" : "â€¦or paste an image URL"}
        onChange={(e) => onChange(e.target.value)}
        className="font-mono text-[11px]"
      />
      {error && (
        <p className="text-[10px] text-rose-400 flex items-center gap-1">
          <AlertTriangle className="w-3 h-3" /> {error}
        </p>
      )}
      {node}
    </div>
  );
}

export function CustomizationSettingsPanel({ generalTab }: { generalTab?: React.ReactNode } = {}) {
  const t = useT();
  const locale = useLocaleStore((s) => s.locale);
  const setLocale = useLocaleStore((s) => s.setLocale);
  const a11y = useLocaleStore((s) => s.a11y);
  const setA11y = useLocaleStore((s) => s.setA11y);

  const theme = useThemeStore((s) => s.theme);
  const userPresets = useThemeStore((s) => s.userPresets);
  const saveUserPreset = useThemeStore((s) => s.saveUserPreset);
  const updateUserPreset = useThemeStore((s) => s.updateUserPreset);
  const applyUserPreset = useThemeStore((s) => s.applyUserPreset);
  const renameUserPreset = useThemeStore((s) => s.renameUserPreset);
  const deleteUserPreset = useThemeStore((s) => s.deleteUserPreset);
  const applyPreset = useThemeStore((s) => s.applyPreset);
  const previewTheme = useThemeStore((s) => s.preview);
  const cancelPreview = useThemeStore((s) => s.cancelPreview);
  const resetTheme = useThemeStore((s) => s.reset);
  const previewMode = useThemeStore((s) => s.previewMode);
  const setPreviewMode = useThemeStore((s) => s.setPreviewMode);

  // The saved chart palette is read imperatively inside the effect (not
  // subscribed) so that manual edits made on the Charts tab don't retrigger â€”
  // and clobber â€” the theme preview. Only draft/preset changes re-derive
  // chart colors. These action selectors are stable, so they never re-render.
  const setChartPreview = useChartPaletteStore((s) => s.setPreview);
  const setChartPalette = useChartPaletteStore((s) => s.setPalette);
  const previewBackground = useBackgroundStore((s) => s.preview);
  const cancelBackgroundPreview = useBackgroundStore((s) => s.cancelPreview);
  const previewAurora = useAuroraPaletteStore((s) => s.preview);
  const cancelAuroraPreview = useAuroraPaletteStore((s) => s.cancelPreview);

  const [tab, setTab] = useState<CustomTab>(generalTab ? "general" : "theme");
  const [draft, setDraft] = useState<EclipseTheme>(theme);
  // When a saved preset carried its own chart/heatmap palette, hold it here so
  // Apply restores that exact palette (including custom series colors) instead
  // of re-deriving it from the theme. Cleared by any manual color edit.
  const [pendingChartPalette, setPendingChartPalette] = useState<ChartPalette | null>(null);
  // The backdrop snapshot a selected preset carries (wallpaper / solid fill +
  // wordmark). Non-null means "this preset also restyles the ChatBot landing",
  // so the live preview auditions it and Apply persists it. Null leaves the
  // user's own backdrop untouched.
  const [pendingBackground, setPendingBackground] = useState<Partial<BackgroundPrefs> | null>(null);
  // The Aurora-glow palette a selected preset carries, so the live preview and
  // Apply restyle the ambient glow alongside the theme + backdrop.
  const [pendingAurora, setPendingAurora] = useState<AuroraPalette | null>(null);
  const [livePreview, setLivePreview] = useState(true);
  const [presetName, setPresetName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [brandColor, setBrandColor] = useState(theme.colors.primary);
  const importRef = useRef<HTMLInputElement | null>(null);

  // When the saved theme changes from outside (apply/reset/preset), resync the
  // draft â€” but never while a draft preview is active.
  useEffect(() => {
    setDraft(theme);
  }, [theme]);

  const dirty = JSON.stringify(draft) !== JSON.stringify(theme);

  // Keep the document showing the draft while live preview is on; when it is
  // turned off, restore the saved theme AND chart palette to the document.
  // Reads the current saved palette lazily so Charts-tab edits are never
  // overwritten by this effect.
  useEffect(() => {
    if (livePreview) {
      previewTheme(draft);
      // A selected preset also previews its ChatBot backdrop; a snapshot is
      // partial so it merges over the user's live background prefs. Selecting a
      // preset with no backdrop (or a manual edit) cancels any active background
      // preview, restoring the user's own saved backdrop.
      if (pendingBackground) previewBackground(pendingBackground);
      else cancelBackgroundPreview();
      // A selected preset also previews its Aurora glow, so the ambient color
      // matches the theme before Apply; a manual edit cancels it.
      if (pendingAurora) previewAurora(pendingAurora);
      else cancelAuroraPreview();
      const base = useChartPaletteStore.getState().palette;
      // A preset with its own chart palette previews that exact palette;
      // otherwise derive market colors from the draft theme.
      setChartPreview(pendingChartPalette ?? chartPaletteFromTheme(draft, base));
    } else {
      cancelPreview();
      cancelBackgroundPreview();
      cancelAuroraPreview();
      clearChartPalettePreview();
    }
  }, [draft, pendingChartPalette, pendingBackground, pendingAurora, livePreview, previewTheme, cancelPreview, previewBackground, cancelBackgroundPreview, previewAurora, cancelAuroraPreview, setChartPreview]);

  // Leaving the panel cancels an uncommitted preview so the saved theme wins.
  useEffect(() => () => {
    cancelPreview();
    cancelBackgroundPreview();
    cancelAuroraPreview();
    clearChartPalettePreview();
  }, [cancelPreview, cancelBackgroundPreview, cancelAuroraPreview]);

  // â”€â”€ Draft mutators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  const patch = (p: Partial<Omit<EclipseTheme, "colors">>) =>
    setDraft((d) => normalizeTheme({ ...d, ...p }));
  const patchColor = (key: keyof ThemeColors, hex: string) => {
    // A manual color edit supersedes a preset's captured chart palette, so the
    // market colors re-derive from what the user just typed. Skip when nothing
    // changed so a color picker re-emitting the same value never restyles the app.
    if (draft.colors[key] === hex) return;
    setPendingChartPalette(null);
    setDraft((d) => normalizeTheme({ ...d, colors: { ...d.colors, [key]: hex } }));
  };
  const patchGlass = (p: Partial<ThemeGlass>) =>
    setDraft((d) => normalizeTheme({ ...d, glass: { ...d.glass, ...p } }));
  const patchSidebar = (p: Partial<ThemeSidebar>) =>
    setDraft((d) => normalizeTheme({ ...d, sidebar: { ...d.sidebar, ...p } }));

  const applyDraft = () => {
    // Restore the preset's backdrop AND clear the preview flag in one step:
    // setPrefs persists and supersedes the preview.
    applyPreset(draft, pendingBackground ?? undefined, pendingAurora ?? undefined);
    // A preset's captured palette wins; otherwise derive from the theme onto
    // whatever chart colors are currently saved.
    const base = useChartPaletteStore.getState().palette;
    setChartPalette(pendingChartPalette ?? chartPaletteFromTheme(draft, base));
    setPendingChartPalette(null);
    setPendingBackground(null);
    setPendingAurora(null);
  };
  const discardDraft = () => {
    setDraft(theme);
    setPendingChartPalette(null);
    setPendingBackground(null);
    setPendingAurora(null);
    cancelPreview();
    cancelBackgroundPreview();
    cancelAuroraPreview();
    clearChartPalettePreview();
  };
  const resetAll = () => {
    setPendingChartPalette(null);
    setPendingBackground(null);
    setPendingAurora(null);
    cancelBackgroundPreview();
    cancelAuroraPreview();
    resetTheme();
    syncChartPaletteFromTheme(useThemeStore.getState().theme);
  };

  // ServiceNow-style "build a theme from your brand color". Because editing the
  // generated colors by hand is fiddly, generating always offers a saveable name
  // and immediately registers the derived look as the visible "unsaved" draft.
  const generateFromBrand = (hex: string) => {
    setDraft((d) => themeFromBrandColor(hex, d));
    if (!presetName.trim()) setPresetName(`Brand ${hex.toUpperCase()}`);
  };

  // Selecting a preset replaces the draft theme AND prepares its chart/heatmap
  // colors, so the Charts tab, market heatmap and native charts all reflect the
  // choice immediately (live) and on Apply (persisted).
  const selectShippedPreset = (preset: (typeof THEME_PRESETS)[number]) => {
    const next = normalizeTheme(preset.theme);
    setPendingChartPalette(null);
    setPendingBackground(normalizePresetBackground(preset.background) ?? null);
    setPendingAurora(preset.aurora ?? auroraFromColors(next.colors));
    setDraft(next);
  };
  const selectUserPreset = (id: string) => {
    const preset = userPresets.find((p) => p.id === id);
    if (!preset) return;
    setPendingChartPalette(preset.chartPalette ?? null);
    setPendingBackground(preset.background ?? null);
    setPendingAurora(preset.aurora ?? null);
    setDraft(normalizeTheme(preset.theme));
  };

  const handleSavePreset = () => {
    // Persist the draft first so the saved preset reflects what's on screen.
    applyDraft();
    const name = presetName.trim() || `My Theme ${userPresets.length + 1}`;
    saveUserPreset(name);
    setPresetName("");
  };

  const handleExport = () => {
    try {
      // Export theme + backdrop together so a shared preset reproduces the
      // whole look. `background` is a partial snapshot of the live prefs.
      const payload = { ...draft, background: currentBackgroundSnapshot() };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      saveBlob(`${(draft.appName || "eclipse").replace(/\s+/g, "-").toLowerCase()}-theme.json`, blob);
    } catch {
      /* download unavailable â€” no-op */
    }
  };

  const handleImport = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result));
        setDraft(normalizeTheme(parsed));
        setPendingBackground(normalizePresetBackground(parsed?.background) ?? null);
      } catch {
        /* invalid file â€” ignore */
      }
    };
    reader.readAsText(file);
  };

  const activePreset =
    THEME_PRESETS.find((p) => JSON.stringify(draft) === JSON.stringify(p.theme))?.id ??
    userPresets.find((p) => JSON.stringify(draft) === JSON.stringify(normalizeTheme(p.theme)))?.id ??
    null;

  // The "Custom theme" tile is the user's own look: commit an unsaved draft,
  // or — when a preset is currently selected — return to the saved custom theme
  // instead of committing that preset under the custom label.
  const handleCustomTheme = () => {
    if (activePreset === null) applyDraft();
    else discardDraft();
  };

  const COLORS: { key: keyof ThemeColors; label: string; hint: string }[] = [
    { key: "primary", label: "Primary", hint: "Brand / interactive" },
    { key: "accent", label: "Accent", hint: "Secondary brand tint" },
    { key: "background", label: "Background", hint: "Page backdrop" },
    { key: "surface", label: "Surface", hint: "Cards & panels" },
    { key: "border", label: "Border", hint: "Hairlines & inputs" },
    { key: "text", label: "Text", hint: "Primary text" },
    { key: "bullish", label: "Bullish", hint: "Gains & up candles" },
    { key: "bearish", label: "Bearish", hint: "Down candles & losses" },
    { key: "secondary", label: "Secondary", hint: "Muted surfaces & sidebar" },
    { key: "muted", label: "Muted", hint: "Quiet fills" },
    { key: "mutedForeground", label: "Muted text", hint: "Labels & captions" },
    { key: "accentSurface", label: "Accent surface", hint: "Hover washes" },
  ];

  // Resolve optional tokens so the swatches show the effective value (the
  // derived mix when the user has not pinned one).
  const resolvedColors = deriveThemeColors(draft.colors);

  const glassMatches = (g: ThemeGlass) =>
    draft.glass.enabled === g.enabled &&
    draft.glass.blur === g.blur &&
    draft.glass.opacity === g.opacity &&
    draft.glass.tint === g.tint;

  const sidebarMatches = (s: ThemeSidebar) =>
    draft.sidebar.tint === s.tint &&
    draft.sidebar.opacity === s.opacity &&
    draft.sidebar.blur === s.blur;

  const TABS: { id: CustomTab; label: string; icon: React.ElementType }[] = [
    ...(generalTab ? [{ id: "general" as CustomTab, label: "General", icon: SlidersHorizontal }] : []),
    { id: "theme", label: "Theme & Presets", icon: Palette },
    { id: "glass", label: "Glass & Surfaces", icon: Layers },
    { id: "aurora", label: "Aurora", icon: Sparkles },
    { id: "charts", label: "Charts", icon: Activity },
    { id: "chatbot", label: "Eclipse Agent Page", icon: MessageSquareText },
    { id: "access", label: "Language & Access", icon: Accessibility },
  ];

  // The draft + Apply/Discard model only applies to theme-backed tabs. General,
  // ChatBot Page (backdrop + conversation font) and Language & Access autosave
  // instantly, so the toolbar would imply unsaved state that does not exist.
  const draftBacked = tab === "theme" || tab === "glass" || tab === "aurora" || tab === "charts";
  return (
    <div className="space-y-6">
      {/* â”€â”€ Live preview toggle + Apply/Discard + full-screen preview â”€â”€â”€â”€ */}
      {draftBacked && (
      <div className="sticky top-0 z-20 -mx-1 px-1 pb-1">
        <div className="rounded-2xl border border-primary/20 glass-card-strong overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
            <div className="flex items-center gap-3 min-w-0">
              <button
                type="button"
                role="switch"
                aria-checked={livePreview}
                onClick={() => setLivePreview((v) => !v)}
                className={cn(
                  "inline-flex items-center gap-2 rounded-xl border px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer",
                  livePreview
                    ? "border-primary/40 bg-primary/10 text-foreground"
                    : "border-border/40 text-muted-foreground hover:text-foreground",
                )}
              >
                {livePreview ? <Eye size={13} className="text-primary" /> : <EyeOff size={13} />}
                Live preview
              </button>
              <span className="text-[10px] text-muted-foreground truncate hidden sm:block">
                {dirty
                  ? livePreview
                    ? "Previewing unsaved changes across the app."
                    : "Editing draft â€” turn on live preview to audition it."
                  : "No unsaved changes."}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                className="rounded-xl cursor-pointer"
                disabled={!dirty && !livePreview}
                onClick={discardDraft}
              >
                <RotateCcw className="w-3.5 h-3.5 mr-1" /> Discard
              </Button>
              <Button
                size="sm"
                className="rounded-xl cursor-pointer"
                disabled={!dirty}
                onClick={applyDraft}
              >
                <Save className="w-3.5 h-3.5 mr-1" /> Apply changes
              </Button>
            </div>
          </div>

          {/* Full-screen preview: hands the user the real, live-themed app. */}
          <div className="border-t border-border/20 px-4 py-2.5">
            <PreviewLauncher
              active={previewMode}
              dirty={dirty}
              onEnter={() => {
                setLivePreview(true);
                previewTheme(draft);
                if (pendingBackground) previewBackground(pendingBackground);
                setPreviewMode(true);
              }}
              onExit={() => setPreviewMode(false)}
              onApply={() => { applyDraft(); setPreviewMode(false); }}
              onDiscard={() => { discardDraft(); setPreviewMode(false); }}
            />
          </div>
        </div>
      </div>
      )}

      {/* Sub-navigation */}
      <div className="flex flex-wrap gap-2">
        {TABS.map((item) => {
          const Icon = item.icon;
          const active = tab === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setTab(item.id)}
              className={cn(
                "inline-flex items-center gap-2 rounded-xl border px-3.5 py-2 text-xs font-medium transition-all cursor-pointer",
                active
                  ? "bg-primary/10 text-foreground border-primary/40 shadow-sm shadow-primary/10"
                  : "text-muted-foreground border-border/30 hover:text-foreground hover:border-border/60",
              )}
            >
              <Icon size={14} className={active ? "text-primary" : "text-muted-foreground"} />
              {item.label}
            </button>
          );
        })}
      </div>

      {/* â•â• General (startup, market, display + behavior) â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      {tab === "general" && generalTab}

      {/* â•â• Glass & Surfaces â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      {tab === "glass" && (
        <div className="animate-fade-in-up space-y-6">
          <Card icon={Layers} title="Glass Mode">
            <Row
              label="Frosted glass surfaces"
              hint="When off, every card, panel and menu becomes solid with no blur."
              checked={draft.glass.enabled}
              onChange={(v) => patchGlass({ enabled: v })}
            />
            <div className="grid gap-2 sm:grid-cols-4">
              {GLASS_PRESETS.map((p) => {
                const active = glassMatches(p.glass);
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => patchGlass(p.glass)}
                    aria-pressed={active}
                    title={p.hint}
                    className={cn(
                      "flex flex-col gap-2 rounded-xl border p-3 text-left transition-all cursor-pointer",
                      active ? "border-primary/40 bg-primary/10" : "border-border/20 glass-card-subtle hover:border-border/40",
                    )}
                  >
                    <span
                      className="h-8 w-full rounded-lg border border-white/10"
                      style={{
                        backgroundColor: p.glass.enabled
                          ? `rgb(${rgbTriplet(p.glass.tint)} / ${p.glass.opacity})`
                          : p.glass.tint,
                        backdropFilter: p.glass.enabled ? `blur(${p.glass.blur}px)` : "none",
                      }}
                      aria-hidden
                    />
                    <span className="text-xs font-semibold text-foreground flex items-center gap-1.5">
                      {p.label}
                      {active && <Check className="w-3 h-3 text-primary" />}
                    </span>
                  </button>
                );
              })}
            </div>
          </Card>

          <Card icon={SlidersHorizontal} title="Fine-tune Glass">
            <Slider
              label="Blur radius"
              value={draft.glass.blur}
              min={0} max={48} step={1}
              format={(v) => `${v}px`}
              disabled={!draft.glass.enabled}
              onChange={(v) => patchGlass({ blur: v })}
            />
            <Slider
              label="Tint opacity"
              value={draft.glass.opacity}
              min={0.05} max={1} step={0.01}
              format={(v) => `${Math.round(v * 100)}%`}
              onChange={(v) => patchGlass({ opacity: v })}
            />
            <ColorField
              label="Tint color"
              hint="Base color of every glass surface"
              value={draft.glass.tint}
              onChange={(hex) => patchGlass({ tint: hex })}
            />
            <div className="flex items-start gap-3 rounded-xl border border-border/20 glass-card-subtle p-3">
              <span
                className="mt-0.5 h-12 w-20 shrink-0 rounded-lg border border-white/10"
                style={{
                  backgroundColor: `rgb(${rgbTriplet(draft.glass.tint)} / ${draft.glass.enabled ? draft.glass.opacity : 0.96})`,
                  backdropFilter: draft.glass.enabled ? `blur(${draft.glass.blur}px)` : "none",
                }}
                aria-hidden
              />
              <p className="text-[10px] text-muted-foreground leading-relaxed">
                Live preview of a themed surface. Frosted glass only reads correctly when the
                tint is translucent â€” a high opacity hides the blur and looks solid.
              </p>
            </div>
          </Card>

          <Card icon={Square} title="Depth & Shadow">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {SHADOW_OPTIONS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => patch({ shadow: s.id })}
                  title={s.hint}
                  className={cn(
                    "px-3 py-2.5 text-xs font-medium rounded-xl border transition-all cursor-pointer",
                    draft.shadow === s.id
                      ? "bg-primary/10 text-primary border-primary/40"
                      : "text-muted-foreground border-border/40 hover:text-foreground hover:border-border",
                  )}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </Card>

          {/* Sidebar surface â€” moved here from the Theme tab so every surface
              token lives on one tab. */}
          <Card icon={PanelLeft} title="Sidebar">
            <p className="text-[10px] text-muted-foreground -mt-2">
              The navigation rail has its own surface, independent of the page and
              cards â€” so it can stay a soft grey instead of matching the black page.
            </p>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
              {SIDEBAR_PRESETS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => patchSidebar(s.sidebar)}
                  aria-pressed={sidebarMatches(s.sidebar)}
                  title={s.hint}
                  className={cn(
                    "flex flex-col gap-2 rounded-xl border p-3 text-left transition-all cursor-pointer",
                    sidebarMatches(s.sidebar)
                      ? "border-primary/40 bg-primary/10"
                      : "border-border/20 glass-card-subtle hover:border-border/40",
                  )}
                >
                  <span
                    className="h-8 w-full rounded-lg border border-white/10"
                    style={{ backgroundColor: `rgb(${rgbTriplet(s.sidebar.tint)} / ${s.sidebar.opacity})` }}
                    aria-hidden
                  />
                  <span className="text-xs font-semibold text-foreground flex items-center gap-1.5">
                    {s.label}
                    {sidebarMatches(s.sidebar) && <Check className="w-3 h-3 text-primary" />}
                  </span>
                </button>
              ))}
            </div>
            <ColorField
              label="Sidebar tint"
              hint="Base color of the navigation rail"
              value={draft.sidebar.tint}
              onChange={(hex) => patchSidebar({ tint: hex })}
            />
            <div className="grid gap-5 sm:grid-cols-2">
              <Slider
                label="Sidebar opacity"
                value={draft.sidebar.opacity}
                min={0.1} max={1} step={0.01}
                format={(v) => `${Math.round(v * 100)}%`}
                onChange={(v) => patchSidebar({ opacity: v })}
              />
              <Slider
                label="Sidebar blur"
                value={draft.sidebar.blur}
                min={0} max={48} step={1}
                format={(v) => `${v}px`}
                disabled={!draft.glass.enabled}
                onChange={(v) => patchSidebar({ blur: v })}
              />
            </div>
            <p className="text-[10px] text-muted-foreground">
              Blur follows the global <strong>Frosted glass</strong> switch above.
            </p>
          </Card>

        </div>
      )}

      {/* â•â• Aurora / Charts (embedded panels) â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      {tab === "aurora" && (
        <div className="animate-fade-in-up"><AuroraSettingsPanel /></div>
      )}
      {tab === "charts" && (
        <div className="animate-fade-in-up"><ChartSettingsPanel /></div>
      )}
      {tab === "chatbot" && <ChatbotPageSettings />}

      {/* â•â• Language & Access â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      {tab === "access" && (
        <div className="animate-fade-in-up space-y-6">
          <Card icon={Languages} title={t("settings.customization.language")}>
            <div className="flex flex-col gap-1.5">
              <Select value={locale} onValueChange={(v) => v && setLocale(v as LocaleCode)}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {LOCALES.map((l) => (
                    <SelectItem key={l.code} value={l.code}>
                      <span className="inline-flex items-center gap-2">
                        {l.nativeName}
                        <span className="text-muted-foreground/60 text-[10px]">{l.name}</span>
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-[10px] text-muted-foreground/70 mt-0.5">
                {t("settings.customization.languageHint")}
              </p>
            </div>
          </Card>

          <Card icon={Accessibility} title={t("settings.accessibility.description")}>
            <Row
              label={t("settings.accessibility.reduceMotion")}
              hint={t("settings.accessibility.reduceMotionHint")}
              checked={a11y.reduceMotion}
              onChange={(v) => setA11y({ reduceMotion: v })}
            />
            <Row
              label={t("settings.accessibility.highContrast")}
              hint={t("settings.accessibility.highContrastHint")}
              checked={a11y.highContrast}
              onChange={(v) => setA11y({ highContrast: v })}
            />
            <Row
              label={t("settings.accessibility.largeText")}
              hint={t("settings.accessibility.largeTextHint")}
              checked={a11y.largeText}
              onChange={(v) => setA11y({ largeText: v })}
            />
            <div className="flex justify-end pt-1">
              <Button
                variant="outline"
                size="sm"
                className="cursor-pointer rounded-xl"
                onClick={() => setA11y(DEFAULT_A11Y)}
              >
                <RotateCcw className="w-3.5 h-3.5 mr-1" /> {t("settings.accessibility.reset")}
              </Button>
            </div>
          </Card>
        </div>
      )}

      {/* â•â• Theme & Presets (default) â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      {tab === "theme" && (
        <div className="animate-fade-in-up space-y-6">
          {/* Presets â€” the starting point: browse a look, then fine-tune below. */}
          <SubSection
            icon={Palette}
            title="Presets"
            description="Pick a ready-made look to preview it across the whole app, then fine-tune it below. Your saved themes live here too."
          >
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {THEME_PRESETS.map((preset) => (
                <PresetTile
                  key={preset.id}
                  theme={preset.theme}
                  label={preset.label}
                  description={preset.description}
                  active={activePreset === preset.id}
                  onClick={() => selectShippedPreset(preset)}
                />
              ))}

              {userPresets.map((preset) => {
                const active = activePreset === preset.id;
                const editing = editingId === preset.id;
                const presetTheme = normalizeTheme(preset.theme);
                return (
                  <div key={preset.id} className="group relative">
                    {editing ? (
                      <div className="flex flex-col gap-2 rounded-xl border border-primary/40 glass-card-subtle p-2.5">
                        <ThemePreview theme={presetTheme} />
                        <input
                          autoFocus
                          value={editingName}
                          onChange={(e) => setEditingName(e.target.value)}
                          onBlur={() => { renameUserPreset(preset.id, editingName); setEditingId(null); }}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") { renameUserPreset(preset.id, editingName); setEditingId(null); }
                            if (e.key === "Escape") setEditingId(null);
                          }}
                          className="w-full rounded-lg border border-primary/40 bg-transparent px-2 py-1 text-xs outline-none"
                        />
                      </div>
                    ) : (
                      <PresetTile
                        theme={presetTheme}
                        label={preset.label}
                        description="Your saved theme"
                        active={active}
                        onClick={() => selectUserPreset(preset.id)}
                      />
                    )}
                    {/* Actions overlay â€” hidden until hover so the preview stays clean. */}
                    <span className="absolute top-1.5 right-1.5 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                      <button
                        type="button"
                        title="Rename"
                        onClick={() => { setEditingId(preset.id); setEditingName(preset.label); }}
                        className="p-1 rounded-md glass-card-strong text-muted-foreground hover:text-foreground cursor-pointer"
                      >
                        <Pencil size={12} />
                      </button>
                      <button
                        type="button"
                        title="Overwrite with current theme"
                        onClick={() => { applyDraft(); updateUserPreset(preset.id); }}
                        className="p-1 rounded-md glass-card-strong text-muted-foreground hover:text-foreground cursor-pointer"
                      >
                        <RotateCcw size={12} />
                      </button>
                      <button
                        type="button"
                        title="Delete theme"
                        onClick={() => deleteUserPreset(preset.id)}
                        className="p-1 rounded-md glass-card-strong text-muted-foreground hover:text-rose-400 cursor-pointer"
                      >
                        <Trash2 size={12} />
                      </button>
                    </span>
                  </div>
                );
              })}

              {/* The live, unsaved draft â€” always the last tile so "tune below"
                  has an obvious home. Clicking it commits an unsaved custom
                  draft, or returns to the saved custom theme after browsing a
                  preset, mirroring the preset tiles instead of looking dead. */}
              <button
                type="button"
                onClick={handleCustomTheme}
                aria-pressed={activePreset === null}
                title={
                  activePreset === null
                    ? dirty ? "Apply your custom theme" : "Your custom theme"
                    : "Return to your custom theme"
                }
                className={cn(
                  "flex flex-col gap-2 rounded-xl border p-2.5 text-left transition-all cursor-pointer",
                  activePreset === null
                    ? dirty
                      ? "bg-primary/10 border-primary/40"
                      : "bg-primary/5 border-primary/30"
                    : "border-border/20 glass-card-subtle hover:border-border/40",
                )}
              >
                <ThemePreview theme={activePreset === null ? draft : theme} />
                <span className="flex flex-col min-w-0 px-0.5">
                  <span className="text-xs font-semibold text-foreground flex items-center gap-1.5">
                    Custom theme
                    {activePreset === null && <Check className="w-3 h-3 text-primary shrink-0" />}
                  </span>
                  <span className="text-[10px] text-muted-foreground truncate">
                    {activePreset === null
                      ? dirty ? "Unsaved â€” click to apply" : "Your custom theme"
                      : "Click to return to your custom theme"}
                  </span>
                </span>
              </button>
            </div>

            <AppWideThemePresets theme={draft} />
            {/* Save / import / export / reset */}
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <Input
                value={presetName}
                onChange={(e) => setPresetName(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") handleSavePreset(); }}
                placeholder="Name this themeâ€¦"
                className="h-9 flex-1 min-w-[180px]"
              />
              <Button size="sm" className="rounded-xl cursor-pointer" onClick={handleSavePreset}>
                <Plus className="w-3.5 h-3.5 mr-1" /> Save as preset
              </Button>
            </div>

            <div className="flex flex-wrap justify-between items-center gap-2 pt-1 border-t border-border/10">
              <div className="flex items-center gap-2">
                <Button variant="outline" size="sm" className="rounded-xl cursor-pointer" onClick={handleExport}>
                  <Download className="w-3.5 h-3.5 mr-1" /> Export
                </Button>
                <Button variant="outline" size="sm" className="rounded-xl cursor-pointer" onClick={() => importRef.current?.click()}>
                  <Upload className="w-3.5 h-3.5 mr-1" /> Import
                </Button>
                <input
                  ref={importRef}
                  type="file"
                  accept="application/json,.json"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    e.target.value = "";
                    if (file) handleImport(file);
                  }}
                />
              </div>
              <Button variant="outline" size="sm" className="cursor-pointer rounded-xl" onClick={resetAll}>
                <RotateCcw className="w-3.5 h-3.5 mr-1" /> Reset to Eclipse Default
              </Button>
            </div>
          </SubSection>

          {/* Brand color generator */}
          <Card icon={Wand2} title="Build Theme From Brand Color">
            <p className="text-[10px] text-muted-foreground -mt-2">
              Pick your organisation&apos;s brand color and Eclipse derives a coordinated
              palette â€” primary, accent and market colors â€” around it.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <label
                className="relative h-9 w-9 shrink-0 rounded-xl border border-white/15 cursor-pointer overflow-hidden shadow-sm"
                style={{ backgroundColor: brandColor }}
              >
                <input
                  type="color"
                  value={normalizeHex(brandColor) ?? draft.colors.primary}
                  onChange={(e) => { setBrandColor(e.target.value); generateFromBrand(e.target.value); }}
                  className="absolute inset-0 h-full w-full opacity-0 cursor-pointer"
                  aria-label="Brand color picker"
                />
              </label>
              <input
                value={brandColor}
                onChange={(e) => {
                  setBrandColor(e.target.value);
                  const n = normalizeHex(e.target.value);
                  if (n) generateFromBrand(n);
                }}
                spellCheck={false}
                className="w-[120px] rounded-lg border border-border/40 bg-transparent px-2 py-1 font-mono text-[11px] uppercase outline-none focus-visible:border-primary/50"
                aria-label="Brand hex value"
              />
              <Button
                variant="outline"
                size="sm"
                className="rounded-xl cursor-pointer"
                onClick={() => generateFromBrand(normalizeHex(brandColor) ?? draft.colors.primary)}
              >
                <Wand2 className="w-3.5 h-3.5 mr-1" /> Generate theme
              </Button>
              <span
                className="h-5 w-32 shrink-0 rounded-md border border-white/15"
                style={{ background: `linear-gradient(90deg, ${draft.colors.bearish} 0%, ${draft.colors.primary} 50%, ${draft.colors.accent} 100%)` }}
                aria-hidden
              />
            </div>
          </Card>

          {/* Fine-tune â€” every token that shapes the chosen preset. */}
          <SubSection
            icon={SlidersHorizontal}
            title="Fine-tune"
            description="Adjust the look token by token. Changes are previewed live; glass depth and sidebar surfaces live on their own tab."
          >

          {/* Branding */}
          <Card icon={Sparkles} title="Branding">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground font-medium">App / workspace name</Label>
                <Input
                  value={draft.appName}
                  onChange={(e) => patch({ appName: e.target.value })}
                  placeholder="BlackEclipse"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground font-medium">Logo mark (text)</Label>
                <Input
                  value={draft.logoText}
                  maxLength={4}
                  onChange={(e) => patch({ logoText: e.target.value })}
                  placeholder="B"
                />
              </div>
            </div>
            <ImageField
              label="Logo image"
              hint="Replaces the sidebar / header mark. PNG, SVG or WEBP up to 300 KB."
              value={draft.logoUrl}
              onChange={(v) => patch({ logoUrl: v })}
            />
            <ImageField
              label="Favicon"
              hint="Browser tab icon. Empty derives one from your logo mark + primary color."
              value={draft.faviconUrl}
              onChange={(v) => patch({ faviconUrl: v })}
            />
          </Card>

          {/* Typography & shape */}
          <Card icon={Type} title="Typography & Shape">
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground font-medium">UI font</Label>
                <Select value={draft.fontUi} onValueChange={(v) => v && patch({ fontUi: v })}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {UI_FONT_OPTIONS.map((f) => <SelectItem key={f.label} value={f.value}>{f.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground font-medium">Heading font</Label>
                <Select value={draft.fontHeading} onValueChange={(v) => v && patch({ fontHeading: v })}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {HEADING_FONT_OPTIONS.map((f) => <SelectItem key={f.label} value={f.value}>{f.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground font-medium">Monospace font</Label>
                <Select value={draft.fontMono} onValueChange={(v) => v && patch({ fontMono: v })}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {MONO_FONT_OPTIONS.map((f) => <SelectItem key={f.label} value={f.value}>{f.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="grid gap-5 sm:grid-cols-2">
              <Slider
                label="Text size"
                value={draft.fontScale}
                min={0.8} max={1.4} step={0.05}
                format={(v) => `${Math.round(v * 100)}%`}
                onChange={(v) => patch({ fontScale: v })}
              />
              <Slider
                label="Logo size"
                value={draft.logoScale}
                min={0.6} max={1.8} step={0.05}
                format={(v) => `${Math.round(v * 100)}%`}
                onChange={(v) => patch({ logoScale: v })}
              />
            </div>
            <Slider
              label="Border radius"
              value={draft.radius}
              min={0} max={1.6} step={0.05}
              format={(v) => `${v.toFixed(2)}rem`}
              onChange={(v) => patch({ radius: v })}
            />
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground font-medium">UI density</Label>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                {DENSITY_OPTIONS.map((d) => (
                  <button
                    key={d.id}
                    type="button"
                    onClick={() => patch({ density: d.id })}
                    title={d.hint}
                    className={cn(
                      "px-3 py-2.5 text-xs font-medium rounded-xl border transition-all cursor-pointer",
                      draft.density === d.id
                        ? "bg-primary/10 text-primary border-primary/40"
                        : "text-muted-foreground border-border/40 hover:text-foreground hover:border-border",
                    )}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </div>
          </Card>

          {/* Colors */}
          <Card icon={Palette} title="Colors">
            <div className="grid gap-3 sm:grid-cols-2">
              {COLORS.map((c) => (
                <ColorField
                  key={c.key}
                  label={c.label}
                  hint={c.hint}
                  value={resolvedColors[c.key]}
                  onChange={(hex) => patchColor(c.key, hex)}
                />
              ))}
            </div>
            <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
              <Sun className="w-3.5 h-3.5 text-primary shrink-0" />
              <span>
                The last four are derived automatically until you edit them. Charts, heatmaps and
                candles live on the Charts tab. Light/dark mode still follows General â€º Theme.
              </span>
            </div>
          </Card>

          {/* Glass & sidebar surfaces moved to their own tab to avoid two
              places editing the same tokens. Link across instead. */}
          <Card icon={Layers} title="Glass & Sidebar Surfaces">
            <p className="text-[10px] text-muted-foreground -mt-2">
              Frosted-glass depth, tint, shadows and the navigation rail surface are
              edited together on one dedicated tab.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                className="rounded-xl cursor-pointer"
                onClick={() => setTab("glass")}
              >
                <Layers className="w-3.5 h-3.5 mr-1" /> Glass &amp; Surfaces
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="rounded-xl cursor-pointer"
                onClick={() => setTab("glass")}
              >
                <PanelLeft className="w-3.5 h-3.5 mr-1" /> Sidebar surface
              </Button>
            </div>
          </Card>

          {/* Motion */}
          <Card icon={Sparkles} title="Effects & Motion">
            <Row
              label="Ambient glow"
              hint="The Aurora glow behind the app shell."
              checked={draft.glow}
              onChange={(v) => patch({ glow: v })}
            />
            <Row
              label="Interface animations"
              hint="Fades, transitions and animated accents."
              checked={draft.animations}
              onChange={(v) => patch({ animations: v })}
            />
          </Card>
          </SubSection>
        </div>
      )}
    </div>
  );
}
