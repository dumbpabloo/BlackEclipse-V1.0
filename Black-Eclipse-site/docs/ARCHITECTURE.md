---
title: Architecture
status: Current
last-verified: 2026-11-20
---

# Architecture

**Who this is for:** a developer about to make a structural change, or anyone
trying to understand how the pieces fit.

This is the *map*. For "why", read [`DECISIONS.md`](./DECISIONS.md). For
"where is the file", read [`NAVIGATION.md`](./NAVIGATION.md). For the data path,
read [`DATA-FLOW.md`](./DATA-FLOW.md).

> The authoritative, more detailed engineering brain lives in
> [`../skills/ARCHITECT.md`](../skills/ARCHITECT.md). Where the two overlap, that
> file is the deeper reference and this one is the readable summary.

## Layer 0 — The shell

`src/app/page.tsx` renders `<StockChat />` inside an `ErrorBoundary`. That is the
whole routing story for the app: **one page**.

`src/components/screens/stock-chat.tsx` is the SPA shell and owns:

- the `activeTab` state machine,
- keep-alive tabs (mount on first visit, then `display:none`),
- conversation persistence: `localStorage` (`chats_guest`) is only the instant
  cache; a **durable per-conversation transcript** is written server-side to
  `.black-eclipse/sessions/<chatId>/transcript.json` and hydrated on boot (see
  "Chat session workspaces" below), so history survives a server restart or a
  change of browser origin — `localStorage` is origin-scoped, so a different
  host/port (or cleared storage) otherwise dropped every chat. The
  chat-history organization layer is owned by `src/lib/chat/chat-organization.ts`
  (conversation + folder types — nested folders via `parentId`/`order`, tags,
  archive + trash stamps, color/pin — persisted-storage helpers, ranked search,
  folder tree, and the pure filter/sort/group view logic),
  `src/lib/chat/chat-export.ts` (versioned JSON export/import), and
  `src/lib/chat/use-chat-organization.ts` (the state/handlers hook); folders
  persist under `chat_folders_v1` and per-chat organization fields live on each
  stored conversation,
- deep-link boot (`/?symbol=AAPL` opens the chart),
- the command-palette destination bus, and the `be:navigate` custom event,
- the full-viewport **Aurora** glow driven by combined sentiment.

Screen chunks are loaded with `next/dynamic` + `ssr: false` so first paint only
compiles the chat shell. This is a deliberate memory/perf decision — do not
convert these to static imports.

### Keyboard ownership

Function-key navigation shares the persisted sidebar configuration; it does not
introduce a second router. Untouched legacy defaults migrate to assigned keys,
while customized assignments and disabled sections are preserved.

The sidebar has four top-level modes — **Terminal** (market tools),
**Agentic**, **Bots** and **Graph Studio** — selected by a switcher at the top
of the menu and by **Alt+T / Alt+A / Alt+B / Alt+G**. The active mode lives in
the sidebar store (`mode`) and is **per shell**: each browser tab/window keeps
its own choice in `sessionStorage`, so switching to Bots in one tab never drags
another open tab out of its mode. The switcher, keyboard dispatch and
programmatic navigation still read/write the one value for that shell. The
desktop shell's browser-style tabs are in-page instances inside ONE shell, so
they cannot use `sessionStorage` for this: each `DesktopTab` records its own
`mode` and the tab strip replays it on switch (`applyTabMode` never echoes to
the app-wide nav store), keeping a Bots tab and a Command Center tab separate.
The slim **chat session is per tab too**: a `DesktopTab` also records the
`conversationId` it is showing (`null` = its own fresh chat), the strip writes
the live session into the tab on switch/new/close, and restoring a tab loads
that conversation — so `+` starts a new session without replacing the old tab's
transcript. `be_desktop_tabs_v2_active` remembers the front tab across a
relaunch (the launch tab's own session is restored after the transcript cache
hydrates). Order
and visibility are mirrored app-wide through `/api/sdk/app` (JDDS
`SystemConfig` key `eclipse_app_shell_nav`); `localStorage` is the instant cache
and the shared mode seed for newly opened shells, and SSE
(`/api/sdk/app/stream`) applies section changes without overriding an open
shell's mode. Keyboard shortcuts stay local. ChatBot belongs to both Terminal
and Agentic; when a terminal/agentic-only screen is
opened by the palette or a deep link the switcher follows it. The switcher is
hidden while collapsed, but the selected mode still filters the icons — switch
with Alt+T / Alt+A. The Agentic mode lists **ChatBot, Skills, Eclipse Memory and
Artifacts**; the other agentic capabilities (Code, Editor, Jobs, Tasks, Pages,
UI Builder, Plugins, Model Control Center, Search, Research, Backtest) are not
sidebar tabs and are reached from ChatBot through its tools. The Eclipse SDK is
the user-facing composition surface — declarative widgets, composed pages, the
Command Center, typed themes, fund portfolios and passphrase locks — driven
through ChatBot tools or the typed `eclipseSdk` client; there is no dedicated
SDK page (the former `?tab=sdk` surface was removed). See
`docs/eclipse-sdk.md`. Every SDK mutation appends to `SdkAudit` on
`agent-memory.db` (bounded, no secrets); front-door-owned writes (theme, app
shell, dashboard templates) record through the same loopback audit route.
The Command Center toolbar
exposes retained-revision Undo/History; a restore is a new revision, so undo is
itself undoable. The dashboard is not a separate
store: `/dashboard/<id>` is a UI blueprint mirrored by `DashboardScreen`, and
layout writes flow through the same UI Engineering engine, revisions and locks.
Persona templates stay in TS: `dashboards.applyTemplate/reset` build a full
board from `src/lib/dashboard/templates.ts` and write it through that same
revision-guarded layout API. `/api/ui/stream` carries a dashboard change kind so a durable edit lands on the
live board without the old 20s poll. Theme creation is a closed `ThemeSpec` over
the existing app-wide theme store; named presets live in `SystemConfig` and every
theme write checks both `theme:global` and `design:global` locks. `/app` forces
the app-control surface (no code profile); `/eclipsesdk` remains the source-code
path.

`src/hooks/use-page-shortcuts.ts` registers page actions against visible DOM
roots, with matching and guards in `src/lib/keyboard.ts`. Keep-alive screens
must never receive actions while hidden. Focused nested scopes take precedence;
editable controls, composition, repeated presses, consumed events and blocking
dialogs suppress page commands. Custom overlays must declare dialog semantics
or `data-keyboard-overlay="true"` to participate in this guard. Chart drawing
commands additionally require ownership by the focused chart.

The root-mounted `src/components/layout/keyboard-shortcuts.tsx` exposes Ctrl/Cmd+/
(and the conventional `?`) help using the sidebar settings and registered action
metadata. The global catalogue lives in `src/lib/shortcuts.ts` so the dialog and
its predicate helpers cannot drift; page groups reflect mounted screens, so
visiting a screen registers its contextual commands. Handlers reuse existing UI
actions; creation shortcuts open forms rather than submit financial mutations.
Global additions alongside the existing help/search/terminal/workspace chords are
Ctrl/Cmd+B (collapse the sidebar, shared through the sidebar store), Ctrl/Cmd+,
(Settings) and Ctrl/Cmd+Shift+L (light/dark scheme). Matching for chords whose
`event.key` differs under AltGr or non-US layouts falls back to the physical key
`code`. Browser/OS-reserved function keys cannot be guaranteed by a web app and
remain user-reassignable in Sidebar settings; F1/F5/F11/F12 are labelled as
browser-owned in the help dialog and Settings rather than failing silently.

Three other keyboard affordances build on the same dispatcher rather than adding
listeners:

- **Executable palette commands** (`src/lib/search/commands.ts`, rendered by
  `global-search.tsx`). `Ctrl+K` runs real parameterized actions — quick alert,
  watchlist add, chart — reusing existing helpers such as `createQuickAlert`.
  The static/fund/Settings "Commands" group was removed from the palette; the
  only executable rows now are the parameterized verbs (`alert`/`watch`/`chart`
  + symbol). Side effects live on an `ExecutableCommand.run` field, never on
  registry actions, because the resolver calls `action.run()` on every keystroke.
- **List navigation** (`src/hooks/use-list-navigation.ts`). Roving selection for
  news, screener tables, alerts, journal trades and the Investor Eye feed. It
  tracks selection by stable id, not index, so sorting or a refresh cannot move
  the selection. Screens opt in with their existing root scope.
- **Sub-tabs and history.** `Alt+1..9`/`Alt+0` select sub-tabs (Journal,
  Economics). Because there is no router, `src/lib/navigation-history.ts`
  keeps a capped in-memory stack of visited tabs that `Alt+←` walks back through.

**Deep links (open in a new tab).** The SPA has no router, so `src/lib/search/deep-link.ts`
maps every `Destination` to a real URL — `?tab=<tab>&sub=<subroute>&<params>` —
and back (`parseDeepLink`). The shell boots from the URL on load, re-applies it
on browser back/forward, and keeps it in sync (via `replaceState`) as the active
tab changes; a bare `?symbol=AAPL` still boots the chart tab for back-compat.
Sidebar nav items and command-palette rows are rendered as real `<a>` anchors
whose `href` is `destinationToHref(destination)`, so **Ctrl/Cmd-click,
middle-click and "open in new tab"** work everywhere a page or instrument is
listed; plain left-clicks stay in-page. The screener's symbol pivot builds its
new-tab URL through the same helper (`/?tab=chart&symbol=…`). When the URL has
no explicit destination, the app always boots on the **Eclipse Agent** (`chat`)
tab: the agent is the front door, so a launch never resumes whatever screen was
last open. An explicit deep link is the only way to open another screen
directly. There is no saved start-screen preference (`src/lib/general-prefs.ts`
keeps only the destructive-action confirmation; a legacy `startScreen` value in
stored JSON is ignored).

**When the app is not running.** The desktop shell owns its own boot screen
(`desktop/ui/index.html`, embedded at compile time) and
`scripts/desktop-server.js` reports progress with `@@BE:ATTEMPT` /
`@@BE:ATTACH` / `@@BE:READY` / `@@BE:ERROR` markers, so a failed boot offers
Retry instead of a blank window. There is **no reverse proxy and no separate
front-door process**: the window loads `http://127.0.0.1:3100` directly, so a
stopped server is a shell-level error panel, never a browser error page.

There is no offline fallback layer left to maintain: the service worker
(`public/sw.js`), the `/offline` route and `OfflineReady` were deleted with the
browser lane, because outside a plain browser they had nothing to protect — the
shell owns the pre-ready state entirely.

## Layer 1 — Screens & components

```
src/components/
  screens/        full-page views + their data/hook modules
  layout/         sidebar, top-bar, price-ribbon, global-search, agentation
  chat/           chat-specific pieces
  ui/             reusable primitives (button, card, dialog, tab-bar, …)
  chart/          the ECharts/TradingView chart widget (being retired)
  charts/         the chat chart surface — the Plotly view and the drawn-picture shell
  journal/        trading journal UI
  market-workspace/  the multi-panel market grid UI
  workspace/      the customizable widget-grid UI
  graph-studio/   the visual workflow editor (React Flow over the Workflow IR)
```

Conventions: `'use client'` at the top of client files, **named exports**, and
the `cn()` helper from `src/lib/utils.ts`. UI primitives come from
`src/components/ui/`; do not hand-roll a new one.

## Layer 2 — Client data layer

Two mechanisms:

1. **DataHub** (`src/lib/workspace/data-hub.ts`) — a singleton client-side
   pub/sub. Widgets declare `dataDeps` (topic ids); the hub batches one fetch per
   topic, caches with a TTL, auto-refreshes on an interval, and fans the result
   out. Topics are registered with `registerTopic(jsonTopic(...))` at module load
   and consumed with `useTopic<T>(id)` / `useRefreshAll()`.
   - Hidden widgets unsubscribe, which stops the refresh timer — this is the
     rate-limit protection.
   - A failed topic becomes `UNAVAILABLE`; it never takes down the dashboard.
   - Provenance is explicit: `LIVE | CACHED | HISTORICAL | DEMO | ESTIMATED |
     UNAVAILABLE`.
2. **Zustand stores** for UI state (sidebar config, dashboard layouts, search,
   market-workspace, aurora palette, i18n/a11y, centralized theme) — persisted to
   `localStorage`, not the DB.
   **Exception — the centralized theme is app-wide, not per-origin.** Its source
   of truth is the JDDS `SystemConfig` table (key `eclipse_theme`, read/written by
   `src/app/api/theme/route.ts` + `src/lib/theme/server.ts`), so every browser
   origin (`localhost:3000`, `127.0.0.1:3000`, `https://blackeclipse`) and every
   device show the SAME look. `localStorage` (`be_theme_v1`) is demoted to a
   per-origin first-paint cache: on boot the store hydrates the cache, then
   `hydrateFromServer()` adopts the saved app-wide theme. If the server has never
   been initialized, the client **migrates** its pre-existing per-origin theme up
   (so an existing look is kept, not wiped); once the theme has been explicitly
   reset server-side, a null read clears the local override back to the shipped
   default. Edits push to the server (`PUT /api/theme`) and Reset clears it
   (`DELETE`). The middleware can additionally enforce ONE canonical origin when
   `BE_CANONICAL_HOST` is set. No launcher sets it now that the browser lane is
   gone — the desktop always loads `http://127.0.0.1:3100`, so origin-scoped
   storage cannot diverge in the first place.
     - **Centralized theming.** `src/lib/theme/` owns one serializable
      `EclipseTheme` (branding, fonts, colors, radius, density, text/logo scale,
      glass, shadow, background, **scheme**, glow/animation). The store flattens
      it to CSS custom properties and injects them via a managed
      `<style id="eclipse-theme-overrides">`; color tokens are scoped by the
      theme's `scheme` — `html.dark` for dark, `html:not(.dark)` for light — while
      typography/shape/scale/glass tokens apply globally. The theme (and the
      page-background vars) is hydrated once at the **root** by `ThemeHydrator`
      in `src/app/layout.tsx`, so standalone routes (`/events`, `/fabric`,
      `/recovery`, `/theses`, `/alerts`, `/terminal`, `/login`, `/app-view`,
      `/figure`) paint the saved theme too — they used to fall back to the
      shipped `:root` light tokens (a grey page). **`scheme` is the source
      of truth for the `<html class="dark">` flag**: the store toggles it on every
      apply (it was previously hardcoded in `layout.tsx`, which now only seeds the
      dark default plus a small pre-paint script that removes `dark` for a saved
      light theme to avoid a flash). This is what makes the **white-background
      presets** (`White`, `Paper`, `Snow`, `Ivory`) work end to end: selecting one
      from the normal preset grid drops `dark`, activating both the `:root` light
      token block and every `html:not(.dark)` recipe (glass, sidebar, calendar,
      ws-glass). There is **no separate light/dark mode toggle** — a preset owns
      the scheme, and light presets ship dark ink text, white surfaces, a light
      landing and a dark wordmark. Components that hardcoded dark-only neutral
      utilities
      (`border-white/*`, `bg-black/*`, `bg-white/*`, `text-white/*`, a few
      near-black hexes) are remapped to light equivalents by a scoped
      compatibility layer at the bottom of `globals.css`; canvas charts resolve
      their grid/axis/tooltip chrome from the scheme in `eclipse-provider.ts`.
      Because components and
      charts already read `--primary` / `--font-mono` / `--radius` / `--bullish` /
      `--bearish` / `--font-scale`, one theme object recolors the whole app with no
      per-component wiring. `ThemeColors` exposes the eight core tokens plus four
      **optional derived tokens** (`secondary`, `muted`, `mutedForeground`,
      `accentSurface`); when a user pins one it wins, otherwise
      `deriveThemeColors()` mixes it from the core colors (single source of truth
      for `themeCssVars`). The `EclipseTheme` also carries a dedicated
      **`sidebar`** surface (`tint`/`opacity`/`blur`), emitted as
      `--sidebar-rgb/-opacity/-blur`; `.sidebar-root` consumes these so the
      navigation rail keeps its own dark-charcoal frost independent of the page
      and card glass (edited via the **Sidebar** card in General › Theme).
      `themeFromBrandColor()` is the white-label entry point:
      it derives a coordinated primary/accent/bullish palette from a single brand
      color (ServiceNow-style), with bearish overridable. **Glass mode** is driven
      by `--glass-blur/-opacity/-tint` tokens that all `.glass-*` recipes consume;
      the `theme-glass-off` root class removes blur and makes surfaces opaque, so
      users can disable frosted glass app-wide from Settings (no component reads the
      preference). `--glass-tint` is a dark-scoped color with a `--glass-tint-light`
      counterpart, so a custom dark tint never leaks into light mode. The token
      glass override is gated by the `theme-glass-active` root class, which is only
      added once the user changes a glass/shadow setting — so the shipped per-tier
      glass hierarchy is preserved untouched by default. **Never write
      `-webkit-backdrop-filter` next to `backdrop-filter`** in these rules: Next's
      lightningcss minifier collapses the pair down to the `-webkit-` form alone,
      which modern Chrome no longer supports, so every glass surface silently loses
      its blur — the `theme-glass-active` rules shipped broken this way once and are
      now standard-property only. **User themes** are saved
      by name into `be_theme_presets_v1` (save / rename / update / delete) and can be
      exported/imported as JSON. A saved preset also captures the chart/heatmap
      palette in effect (via `bridge.ts`), and re-applying it restores both the
      theme and chart colors.
      A preset is **holistic**: it also carries an optional partial
      `background` snapshot (`ThemePreset.background` / `UserThemePreset.background`,
      validated by `normalizePresetBackground` in `backgrounds.ts`). This is what
      makes one click restyle the **ChatBot landing** too — shipped presets ship a
      coordinated solid fill + wordmark (e.g. Midnight's flat, near-neutral
      dark landing with an "ECLIPSE AGENT" wordmark), and the panel pushes the
      snapshot to
      `useBackgroundStore` on preview/apply via `applyPreset(theme, background)`.
      Because the snapshot is **partial and merged over live prefs**, a preset that
      only themes the wordmark keeps the user's chosen wallpaper and vice versa;
      `saveUserPreset`/`updateUserPreset` capture the current prefs, and export/import
      round-trip the snapshot alongside the theme. The panel uses a **draft + preview** model: edits
      go to a local draft, `preview()` applies it to the document without
      persisting, and Apply/Discard commit or revert (uncommitted previews are
      cancelled on unmount). Live/draft preview also overlays the chart palette
      (`useChartPaletteStore.setPreview`) so candles and market heatmaps recolor
      instantly without persisting, and overlays a selected preset's ChatBot
      backdrop (`useBackgroundStore.preview`, which sets `previewing` +
      `previewPrefs`; `ChatWordmark` reads `previewPrefs ?? prefs` so the glyphs
      update live, not just the CSS layer). `cancelPreview` restores the saved
      backdrop; a manual background edit (`setPrefs`) also supersedes a preview.
      Auditioning is **full-screen on the real app**:
      the "Enter full-screen preview" action sets `previewMode`, which hides (not
      unmounts) the Settings dialog in `stock-chat.tsx` so the live-themed app is
      visible and interactive, with a portaled floating **Apply / Discard / Exit**
      bar (the panel keeps its draft because it stays mounted; dialog close is
      suppressed while previewing). The old tiny inline mock was removed.
      **App background** is a separate, immediately-applied preference owned by
      `src/lib/backgrounds.ts` (Customization › ChatBot Page): mode
      `image | solid`. `image` picks one of the shipped wallpapers
      (`public/backgrounds/eclipse bg-1.svg`/`-3.png`/`-4.svg`/`-5.svg`) or the
      user's own picture; `solid` paints the landing from `solidFill`
      (`flat | gradient | pattern`): a flat `solidColor`, a two-color
      `solidColor`→`solidColor2` gradient (linear at `solidGradientAngle`, or
      `radial`; empty stops follow `--background` and the brand tint), or a
      texture (`solidPattern`: grid / dots / diagonal / ASCII, at
      `solidPatternOpacity` and `solidPatternScale` px pitch) over the base
      fill. A `flat` fill is a genuine solid: the color is emitted as
      `--app-bg-color` (light / theme background) and `--app-bg-color-dark`
      (dark) onto the layer's `background-color` (no `background-image`
      gradient function at all) — with no explicit pick the light fill follows
      `var(--background)` while dark defaults to `#0a0b0b`
      (`DEFAULT_SOLID_DARK_COLOR`) on every theme, so the page behind the
      wordmark reads the same dark surface regardless of the active theme, while
      `gradient`/`pattern` fills are emitted as
      a layered `background-image` stack (a bare color is not a valid
      `background-image`) and the pattern layers
      use `rgb(var(--brand-rgb) …)` for the ink. Shipped dark theme presets use
      `flat` and leave `solidColor` UNSET in the `preset()` helper, so every dark
      ChatBot landing resolves the scheme-aware near-black default (`#0a0b0b`,
      `DEFAULT_SOLID_DARK_COLOR`) rather than a per-theme accent tint — a uniform
      dark surface behind the wordmark. Light presets (`lightPreset`) still mix
      their accent toward a light base (`LANDING_BASE_LIGHT`) with a dark
      wordmark. The dark wordmark is a whiteish `#e8e8ea`. A stored landing color
      matching a retired preset fill (`LEGACY_DARK_LANDING_COLORS`) is cleared on
      hydrate (`migrateLegacyLandingColor`) so old saves adopt the default without
      re-applying a theme. A preset also carries an
      **Aurora palette snapshot** (`ThemePreset.aurora`, derived from the theme's
      bullish/bearish colors), pushed to `useAuroraPaletteStore` on apply so the
      ambient glow recolors with the theme; the panel previews it live and user
      presets capture/restore it. An author may still pin an explicit
      `solidColor`/`textColor` to override. Each solid style carries a
      fully tunable wordmark: text, font, color, `textScale` (vw), `textWeight`,
      `textWidth` (font-stretch), `textLetterSpacing`, `textLineHeight`,
      `textItalic`, `textUppercase`, alignment and vertical position. The
      wordmark is forced on in `solid` (it IS the backdrop) and opt-in in
      `image` via the `textEnabled` toggle; `textColor` empty follows
      `--foreground`. (The old `pattern` mode — flat/gradient/grid — was
      removed: it duplicated the theme's own `gradient`/`grid` treatment,
      its `solid` default rendered nothing, and it is now a real Image/Solid
      choice instead.)
      Because a full-res wallpaper must not share the ~5 MB localStorage budget
      with themes/chats, custom image bytes live in **IndexedDB** (`be_background`
      / `custom`) while the small prefs live in `be_background_v1`; uploads are
      downscaled (~2560px, JPEG/PNG) in the browser before storing. Wallpapers
      paint the **ChatBot landing only** (on an empty transcript) through an
      `.app-bg-layer` that fills the **main content panel** (right of the
      sidebar) — it resizes with the panel, so opening/closing the sidebar keeps
      the image fully visible rather than cropping it (no `background-attachment:
      fixed`, which would size the image to the viewport and crop it). Resize is
      controlled by `fit` (cover/contain/tile), a separate `scale` zoom
      (50–300%), and X/Y focal `position`s. The image is painted on the layer's
      `::before` (so the zoom scales only the art) while the `dim` scrim sits on
      the element itself; the layer reads
      `--app-bg-color/-color-dark/-image/-size/-repeat/-position/-zoom` + `--app-bg-scrim`, and is
      fed by `useHydrateBackground` in `stock-chat.tsx` (initial load) and
      re-asserted by `refreshBackground()` after every theme write, so the
      background's suppression/solid-fill vars stay authoritative over the
      theme's own `--page-bg-image`. The theme store and background store do not
      import each other (that would be a cycle); the theme store calls the
      background store's adapter instead. The default for new
      users is a **Solid** backdrop (no image): `DEFAULT_BACKGROUND.mode` is
      `solid`, so a fresh install paints the theme fill plus a modest centered
      wordmark (`textScale` 7) and never loads a wallpaper until the user picks
      one. The shipped **Eclipse I** wallpaper (`DEFAULT_BACKGROUND_ID`) stays
      the pre-selected entry in Image mode. The landing keeps an uncluttered
      backdrop: no Aurora glow is mounted over it.
      An **chat-conversation surface** (on by default:
      `chatSurfaceEnabled` defaults true) paints a flat fill
      (`chatSurfaceColor` dark / `chatSurfaceColorLight` light, empty → theme
      `--card`) or a two-color gradient (`…`→`chatSurfaceColor2`, empty end →
      brand tint) behind the ChatBot conversation — transcript + composer — at
      `chatSurfaceOpacity` so the wallpaper can show through. The defaults are
      `#0a0b0b` (dark) and a matte `#f4f4f5` (light), so replies sit on a calm
      surface instead of a busy wallpaper. It is a scheme-aware
      `.chat-surface-layer` at the same `z-0` tier as the wallpaper/wordmark
      (below the `z-10` content), fed by `--chat-surface-bg-light/-dark` +
      `--chat-surface-opacity`; it is independent of the page backdrop, renders
      only once a conversation exists (so the landing wallpaper/wordmark still
      show), and paints nothing when disabled.
      The **prompt bubble** (the user's own messages) has its own scheme-aware
      fill (`chatBubbleColor` dark / `chatBubbleColorLight` light; empty → a
      neutral `color-mix(var(--foreground) 10%, transparent)` tint in light,
      `#2e2e2e` charcoal in dark so the bubble separates from the near-black
      surface without going light), applied as
      `--chat-bubble-bg-light/-dark` onto a `.chat-bubble` class by
      `ChatHistory`; unlike the surface it has no enable toggle.
      The giant landing wordmark (`ChatWordmark`) is **opt-in for Image** (the
      `textEnabled` pref defaults false, so the wallpaper shows alone) and
      **always on for Solid** (the wordmark is the point of that mode); the
      authored `text`/font/color/size are retained while off. The 17 wordmark
       typefaces are **self-hosted**: the files live in `public/fonts/` and are
       declared in `src/app/fonts.css` (16 `--font-*` variables on `:root`, no
       runtime request) — elegant serif, sci-fi display, pixel and ASCII/mono
       faces. Keep that list in sync with the variables in
       `src/app/fonts.css`. They are vendored rather than fetched from Google
       at build time: a live `next/font/google` fetch made the BUILD depend on
       the network, and when Google answered a query with more than one font
       entry (it splits the CSS per subset and per weight) Turbopack could not
       resolve the import and the whole app failed to compile.
       The ChatBot page's own look — the conversation
      typeface plus the backdrop/wordmark above — lives together on the
      **Customization › ChatBot Page** tab (the backdrop controls moved there
      out of General). The conversation font is a separate, immediately-applied
      preference owned by `src/lib/chat/appearance.ts` (`be_chat_appearance_v1`):
      it offers the same self-hosted faces (`CHAT_FONT_OPTIONS`, plus a default
      that follows the UI font), is applied as `--chat-font` on `<html>`, and is
      read by `ChatHistory` and the chat shell via
      `var(--chat-font, var(--font-sans))`, so picking a face changes the
      messages and composer live.
      This replaced the old hardcoded, dead
      `/backgrounds/eclipse bg.svg` block in `stock-chat.tsx`. The Settings
      panel is tabbed (**General · Theme · Glass & Surfaces · Aurora · Charts ·
      Language & Access**) and embeds the existing Aurora and Chart panels, so all
      appearance controls live under the merged General section. The General tab is
      supplied by the Settings shell (`general-settings.tsx`) and holds startup,
      default market, refresh, currency, timezone, the destructive-clear
      confirmation and the Background card; the remaining tabs are the
      theme/customization surface.
      The `bridge.ts` helper derives the native chart palette from the theme: on
      explicit user edits it persists (`syncChartPaletteFromTheme`) and during
      preview it overlays without persisting (`previewChartPaletteFromTheme`),
      never on hydration, so a separately-saved chart palette survives reload. The
      categorical series palette is always preserved across theme edits. The
      shipped brand lives in
      `public/branding/` (`FAVICON.svg` mark, `LOGO-WORDMARK.svg` lockup), exported
      as `BRAND_MARK_SRC` / `BRAND_WORDMARK_SRC` from `tokens.ts`. They are the
      defaults for the sidebar/header mark, the wordmark, and the favicon; a
      user-uploaded `logoUrl` / `faviconUrl` always overrides them (the sidebar
      collapsed mark, the expanded wordmark and the mobile header all read
      `theme.logoUrl` first, falling back to the shipped assets — the sidebar
      falls back further to the `logoText` monogram). The Heading-font control now
      reaches `font-heading` elements: `fontHeading` emits both `--font-serif` and
      a dedicated `--font-heading` (previously `--font-heading` aliased
      `--font-sans`, so choosing a Heading font silently did nothing for
      `font-heading` utilities). This is a
      presentation-only layer; it never touches stored financial data.
      **Continuous controls are throttled.** A native `<input type="color">` (and
      sliders) emit far faster than the app can repaint; committing every event
      makes the picker stall the main thread. The shared `useThrottledCommit`
      hook (`src/hooks/use-throttled-commit.ts`, with a pure
      `createThrottledCommit` core for tests) applies the first value immediately,
      then coalesces to at most one commit per interval with the latest value
      (trailing + unmount flush), and is used by the theme, Charts and Aurora
      color pickers. `applyThemeToDocument` and the chart `setPreview` additionally
      skip no-op writes.
3. **Portfolio engine** (`src/hooks/use-portfolio-engine.ts`) — pure FIFO/LIFO
   cost-basis and stats logic, consumed by both the portfolio screens and the
   dashboard portfolio widgets.
4. **Broker connectivity (demo + armed live, P2)** - `python/harness/trade/`
   owns a broker-kind registry, a durable `BrokerConnection` / `BrokerOrder` store
   on `agent-memory.db`, and a `BrokerAdapter` contract (with `TradingAdapter` as
   the execution extension). The first adapter attaches to a running, logged-in
   **MetaTrader 5** terminal over local IPC and reads account, positions, working
   orders and quotes. `trade/authorizer.py` runs every order through
   `trade/policy.py`, the money guardrails and a single-use, digest-bound approval
   before `order_send`. Demo and live are separate gates: the connection record and
   the broker's `account_info()` must agree, live also requires a real account, a
   time-boxed operator arm, a non-zero magic, a positive `maxMargin` and a typed
   per-order `LIVE …` phrase, and the kill switch cancels only Eclipse's working
   orders (by magic) before disarming. An ambiguous send is recorded as `unknown`
   and never retried. P3 adds Graph Studio's `trading.broker_order` (paper mode only)
   under a bounded, revocable demo mandate; unattended live trading remains refused.
   The graph surface reads the broker directly (`broker_account`,
   `broker_positions`, `broker_quote`, `broker_bars`), gates on the broker's own
   state (`trading.risk_check` derives equity and open positions from a wired
   account and names every figure's source), and honors a rejected risk decision
   before an order is sent. Every read and exit returns `ok`/`reason`, so a graph
   branches on an outcome instead of a status string.
   P4 adds durable reconciliation (`trade/reconcile.py`) of broker deals, positions
   and order state; the background pass is opt-in and a failed sync is an error,
   never a fabricated fill. `trade/management.py` is the deterministic exit layer:
   a pure policy engine (break-even + buffer, one-way ATR/percent trail, giveback
   floor, time stop, partials) plus `PositionManager`, which applies only
   risk-reducing actions. `trade/management_api.py` adds an opt-in sweep
   (`TRADING_MANAGEMENT_SECONDS`, default off) beside the supervisor; both read
   per-connection `config.management`, `config.risk` and `config.supervisor`
   through the validated, allowlisted connection store. `trade/heat.py` adds the
   portfolio layer: open risk at stops as heat, deterministic FX/crypto
   concentration clusters, a drawdown governor, volatility-targeted stop distance
   and a caller-supplied event blackout, all fed into `trade_plan` before an
   approval card exists. Paper fills remain Graph Studio's `trading.paper_order`. The
   `trading`-category widgets (Quick Trade, Open Positions, Working Orders, Margin
   Usage, Today P&L) read the first saved connection and poll it every 15 s; a
   missing or logged-out connection renders the named reason, never a fabricated
   fill. See `docs/trading.md`.

## Layer 3 — API routes

`src/app/api/**/route.ts`. Route handlers are thin and server-only. Mutating
routes are protected by `src/lib/api-guard.ts`:

- `checkMutationAllowed` / `guardMutation` — a three-tier policy: same-origin
  browser requests always pass; a correct `CRON_SECRET` (Bearer or `x-api-key`)
  passes; otherwise the request must come from localhost.
- `rateLimit` / `guardExpensiveMutation` — in-memory fixed-window per-IP limiter
  for expensive endpoints.
- `checkOutboundUrl` / `filterSafeUrls` — SSRF allowlist for user-configurable
  feed URLs. **RSS feeds only.** Arbitrary operator APIs go through the stricter
  connector path in `src/lib/connectors/security.ts` (DNS validation + pinning,
  redirect blocking, size cap) — see "The Universal Data Connector Layer".

Key route groups: `stocks`, `quote`, `price-history`, `markets` (+`global`),
`indices`, `forex`, `commodities`, `heatmap`, `news` (+`status`, `alert`, `alerts`, `alerts/policy`),
`economics` (+`snapshot`, `calendar`), `earnings`, `search` (+`global`, `index`), `agent`
(+`chat`, `research`, `activity`), `journal/*` (largest group), `alerts/*`,
`scraper` (+`import`), `refresh`, `settings`, `cron` (+`run`, `refresh-news`),
`data-sources` (+`test`), `datasets`, `data/[capability]`, `providers`,
`asset-details`, `symbols`, `commodities`.

## Layer 4 — JDDS (the data vault)

**JDDS = "Joint Data Distribution System".** One SQLite file (`jdds.db`) accessed
through Prisma with the `better-sqlite3` adapter. `src/lib/db.ts` owns the client
and exports `prisma` plus `yieldToEventLoop()` (critical: better-sqlite3 is
synchronous, so bulk writers must yield or they freeze the server).

The schema (`prisma/schema.prisma`) is organized in domains:

| Domain | Models |
|---|---|
| Market universe | `Market`, `Asset`, `Price`, `IndexQuote` |
| News | `NewsItem`, `SourceHealth` |
| Macro | `MacroSeries` |
| Scheduling & config | `CronJob`, `SystemConfig` |
| Agentic research memory | `ResearchReport`, `AgentRun`, `ResearchFinding` |
| Alerts | `PriceAlert`, `PriceAlertEvent`, `Candles` |
| Data connectors | `DataConnector`, `CustomDataset`, `CustomDatasetField`, `CustomDatasetRecord`, `ConnectorMapping` |
| Investor Eye | `Country`, `GeopoliticalEvent`, `EventEntity`, `Relationship`, `SourceRun` (`GeopoliticalEvent.category` distinguishes hazard sub-types via a nullable column — additive, no migration needed) |
| Trading journal | `JournalProfile`, `TradingAccount`, `Strategy`, `StrategyRule`, `Setup`, `Tag`, `JournalDay`, `DailyReview`, `DailyEmotion`, `Trade`, `TradeExecution`, `TradeScreenshot`, `TradeAnnotation`, `TradeTag`, `MarketContext`, `JournalEntry`, `Review` |
| Graph Studio | `Workflow`, `WorkflowVersion` (append-only versions of the canonical Eclipse Workflow IR — see `docs/graph-studio.md`), `WorkflowSchedule` (timezone-aware unattended runs; `nextRunAt` is precomputed so the 60 s tick is one indexed query) |

Two design notes that matter:

- **Dedup by constraint.** `Asset` is unique on `[marketId, symbol]`; `Price` is
  keyed by `assetId`; `NewsItem` has a `dedupKey`. Imports are idempotent upserts,
  not inserts.
- **Auth-ready, not auth-ful.** The journal carries an optional owner concept so
  a `User` relation can be added later without a rewrite. There is still no user
  auth — the app is a guest app — but the journal now surfaces **trading accounts**
  (`TradingAccount`) for multi-account P&L separation.

### Earnings: one calendar, two embeds (no standalone page)

The month-grid earnings calendar is a single component
(`src/components/screens/earnings-calendar.tsx`), rendered in two places:
dense inside the Command Center `earnings-calendar` widget, and roomy at the
**top of the Market Screener** as a hideable section (`earnings` key in the
screener's section-visibility system). There is deliberately NO standalone
earnings tab — the calendar lives where the user already is, rather than adding
another sidebar page. Data comes from the `earnings:calendar` DataHub topic
(`GET /api/earnings`, Alpha Vantage server-side with a Nasdaq fallback; explicit
UNAVAILABLE when neither is reachable).

### One Add-Trade entry point

The Journal header mounts a single `QuickAddTrade` dialog
(`src/components/journal/quick-add-trade.tsx`), reachable from every sub-tab and
via `Shift+N`. It wraps the shared `TradeForm`, owns the
`POST /api/journal/trades` + parent `refresh()`, and can be pre-filled from a
calendar day. The Trades and Calendar tabs used to carry divergent inline copies
(Trades had its own duplicated form-state type); both now open this one dialog,
so the add-trade contract lives in one file.

### Journal capabilities beyond the core ledger

- **Costs are first-class.** `Trade` carries `commission` and `fee` plus a
  stored `grossPnl`; the deterministic `pnl` is **net** (gross − commission −
  fee). `computeTradeMath` owns this, so analytics and the discipline score see
  the same net numbers.
- **Multi-account.** `TradingAccount` rows are managed from the Journal header
  **Settings** dialog (`journal-settings.tsx`) alongside the trader profile
  (`currency`, `accountSize`, `riskPerTrade`). A trade's `accountId` is set in
  the trade form. The profile's `accountSize × riskPerTrade` now drives the
  **currency equity curve** and the discipline **risk rule** (replacing the old
  hard-coded 1%-of-notional proxy).
- **Agent reachability.** The Journal is also reachable from the agent:
  `journal_trades` (read) and `journal_add_trade` (write, single-use
  approval-gated) in `python/harness/journal_tools.py` reach the screen's own
  `/api/journal/trades` route (GET/POST) over the loopback callback — the SAME
  Prisma store, never a second book. The add RE-READS the store to verify the
  write before it is reported as done. The GET honours `limit`/`take`, `status`
  and `symbol` (filters run before the limit; response adds `count`/`total`), so
  an agent turn asks for the rows it needs instead of serialising the whole book
  on every call.
- **List controls.** The Trades tab filters on symbol/strategy/setup/account/
  tag/emotion/session/direction/result/date-range, sorts by column, paginates,
  and exports **CSV** (server route, round-trips through the importer) and
  **PDF** (client-side jsPDF).
- **Depth analytics** (`analytics/advanced.ts`): monthly returns, rolling
  win-rate, MAE/MFE efficiency, confidence-vs-outcome scatter and holding-time
  buckets — all deterministic.
- **Journaling depth.** Free-form **Notebook** entries (`JournalEntry`), a
  per-trade letter grade, a pre-trade checklist derived from the selected
  strategy's rules, and inline custom-tag creation in the trade form.
- **Execution history.** The trade detail shows the `TradeExecution` log
  (created / stop & target modifications / edits) via
  `GET /api/journal/trades/[id]/executions`.

### Journal demo data is real rows, clearly labelled

The journal is DB-backed, unlike the localStorage portfolio demo, so a fresh
install would otherwise open on eight empty tabs. `src/lib/journal/demo.ts` seeds
~90 days of deterministic sample trades at boot (`src/instrumentation.ts`) and
from `scripts/seed-journal.ts`.

Three invariants make shipping demo rows into a user's database safe:

1. **`Trade.source`** (`'manual' | 'demo'`) marks provenance. The UI shows a
   banner whenever demo rows are present, and "Remove demo data" deletes only
   `source: 'demo'` rows — anything the user added is untouched.
2. **One-time marker.** A `SystemConfig` key (`journal_demo_seeded_v1`) records
   that seeding ran, so clearing the demo data does not bring it back on the next
   boot. The seeder also refuses to run when ANY trade already exists.
3. **Deterministic + internally coherent.** A seeded PRNG means identical data on
   every install. Positions are sized as `riskTarget / stopDistance` against the
   profile's own `accountSize × riskPerTrade`, so risk-per-trade varies gently
   (CV ≈ 0.1) instead of by orders of magnitude — which is what makes the risk
   analytics (risk consistency, the 1% rule, expectancy in R) meaningful rather
   than noise.

### Indices are tickers, not `Asset` rows

Indices are addressed by their **Yahoo ticker** (`^GSPC`, `^NSEI`, `^FTSE`,
`000001.SS`), not by the bare TradingView ids (`SPX`, `DJI`, `NIFTY`) — Yahoo has
no `SPX` instrument, so a bare alias silently produces an empty series. They also
are not in `Asset`; only `IndexQuote` holds their latest level. `src/lib/indices.ts`
is therefore the single source of truth: `resolveIndex()` maps any alias to the
canonical ticker (case- and caret-insensitive), and `searchIndices()` powers
autocomplete. Aliases that collide with real listed instruments (`DAX`, `STI`,
`SSE`) are `searchOnly` — they surface in search but never resolve implicitly, so
charting `DAX` still means the ETF.

Most indices come from the Yahoo chart endpoint. Yahoo **delisted PSX**, so the
Pakistan benchmarks are `provider: 'psx'` and served by
`src/lib/scraper/fetchers/psx-index.ts` from the exchange's own endpoints
(`dps.psx.com.pk/historical` for daily OHLC, `/timeseries/eod/<sym>` for the
latest quote).

### Live prices are read-through, not scrape-cadence-bound

`Price` is written by the background cycle on a ~15-minute interval
(`DEFAULT_INTERVAL_MIN`), but the chart panels, header ribbon and portfolio poll
`/api/quote` every 30 seconds. Serving the stored row alone therefore made "live"
prices up to 15 minutes stale while appearing to poll. `src/lib/scraper/live-quote.ts`
closes that gap with a **bounded read-through**: for any non-index asset whose
stored row is older than `STORED_QUOTE_FRESH_MS` (60s), `/api/quote` fetches the
live price from Yahoo's chart `meta.regularMarketPrice` (one browser-UA HTTP
request via `scraper/yahoo-chart.ts` — deliberately **not** the Python bridge,
which is too slow per-call for a 30s poll). A 15-second shared in-memory cache
plus in-flight dedup means many clients share one upstream call per TTL window,
so frequent polling never fans out into a request storm. Indices still resolve
from `IndexQuote`; PSX benchmarks still use their exchange endpoint.

The read-through never fabricates: a failed fetch returns nothing and the stored
row is served under its real `Price.source` / `updatedAt`, so the UI can show it
as stale rather than pretending it is live. This mirrors (and is consistent with)
the timeframe-aware cache on the candle path — see `chart/price-history-ttl.ts`.

### `Price.volume` is not one unit — turnover is the comparable metric

`Price.volume` means different things depending on how the market was ingested:

| Source | What `volume` holds |
|---|---|
| Binance (`connectors/providers/crypto.ts`) | quote-currency **notional** (`quoteVolume`, already in USD) |
| TradingView / yfinance (equities) | **share count** |

Summing share counts across differently-priced stocks is dimensionally
meaningless, and multiplying a notional by price double-counts. So the volume
tooling (`src/lib/heatmap.ts`) normalises both into **turnover** — traded value
in the market's own currency:

- notional markets (crypto): `turnover = volume` (no price multiply)
- share markets (everything else): `turnover = volume × price`

`isNotionalVolume(market)` / `turnoverFor(volume, price, market)` are the single
source of truth; `summarizeVolume()` builds the whole-market profile (total,
mean/median, top contributors, concentration HHI, contribution bands). Bands are
each instrument's **share of market turnover**, not an absolute amount, so they
stay meaningful across USD/PKR/IDR. `GET /api/heatmap?volume=1` serves this as
`HeatmapPayload.volume`, scanned over EVERY priced instrument (not just the
tiles). Feed markets (FOREX/COMMODITIES) have no real traded volume and omit it.

### Portfolio analytics are date-aligned, not position-aligned

`src/lib/portfolio/matrix.ts` owns the fund workspace's correlation and risk
decomposition. Two invariants make its numbers trustworthy:

1. **Date alignment.** Two markets have different trading calendars (NYSE vs PSX
   vs crypto's 7-day week), so `closes[i]` is NOT the same day for two symbols.
   Every statistic is computed on the **intersection of dates** for the pair it
   concerns; comparing position *i* to position *i* would silently correlate
   different calendar days, which is the classic way a correlation matrix lies.
   Pairs with fewer than `MIN_OBSERVATIONS` (20) shared returns render as an
   explicit `·`, never a plausible-looking `0.00`.

2. **Returns are currency-neutral.** r = P_t/P_{t−1} − 1 is a ratio, so native and
   USD-converted series of the same asset give identical returns. Correlations
   are therefore computed on native-currency closes and are correct for a mixed
   USD/PKR/INR book with no FX step. (A currency's own move is a separate
   exposure and is not captured — the widget says so.)

Risk is decomposed with the Euler method: `σ_p = √(wᵀΣw)`, `MCTRᵢ = (Σw)ᵢ/σ_p`,
`CTRᵢ = wᵢ·MCTRᵢ`, and each holding's **share of total risk** is `CTRᵢ/σ_p` (sums
to 100%). `Σ` is assembled as `corr_ij · σ_i · σ_j` so it agrees with what the
correlation widget displays. Pairwise-complete correlation is not guaranteed
positive-semidefinite, so variance is clamped at 0 and negative contributions are
dropped rather than surfaced as negative risk.

Mixed currency: `useLiveQuotes` keeps the **real** `marketId` and currency that
`/api/quote` returns (it used to re-derive a USD/PKR binary from a hardcoded
symbol allowlist, mislabelling an Indian holding as USD). `computePortfolioStats`
converts any currency via a live `fxToUsd`, falling back to a documented
approximation table and flagging the holding `fxMissing` so the UI can call that
USD value an estimate.

## Background workers

All in-process, all started from `src/instrumentation.ts`:

| Worker | Cadence | Source |
|---|---|---|
| Price refresh | configurable, default 15 min | `src/lib/scraper/scheduler.ts` |
| Universe sync | daily | `src/lib/scraper/scheduler.ts` |
| News ingest | default 5 min | `src/lib/news/ingest.ts` |
| News alert scan | after each news ingest (opt-in) | `src/lib/news/alert-scan.ts` |
| Boot warm-up | once, prod-only by default | `src/lib/scraper/scheduler.ts` |
| Alert monitor | polling loop | `src/lib/alerts/alertMonitor.ts` |
| Dataset refresh | per-dataset, min 5 min | `src/lib/connectors/refresh-scheduler.ts` |
| Investor Eye ingest | default 10 min | `src/lib/investor-eye/scheduler.ts` |

They coordinate through mutex flags (`isRunning`, `syncBusy`, `warmUpBusy`) so
two heavy SQLite write storms never run at once. This is not incidental — running
them concurrently is what used to freeze the API.

They are all kicked off from `runBackgroundBootTasks()` AFTER
`instrumentation.register()` resolves, so boot readiness (and the first page
load) never waits on a heavy job — `register()` itself awaits only the
deployment-exposure log and the LLM-config flag the client reads.

**A first run defers the heavy half.** `runBackgroundBootTasks()` first runs the
cheap essentials (US universe, stored provider keys, one-time journal demo) and
starts the **Python runtime warm-up**, then waits for the first-run flag
(`onboarding_v1.completed`, polled every 2 s, capped at 30 min) before starting
the market warm-up, ingests, schedules and backups. A new install can only see
the setup wizard, so those jobs would compete for the same CPU, SQLite write
path and network — which is exactly where setup used to feel slow.

The Python warm-up is deliberately **not** deferred: its first import from a
cold payload measured over three minutes, and users spend that time in the
wizard. Deferring it moved the cold start to the first chat after setup
(“runtime still starting” behind every agent page) and also failed the packaged
release smoke test. Everything else starts as soon as the wizard writes the
completion flag (the reloaded app benefits within a couple of seconds).

**A page request never joins that cold start for long.** `ensurePythonRuntime()`
coalesces callers onto one background spawn, but a request waits at most
`DEFAULT_REQUEST_WAIT_MS` (3 s) before returning its honest 503. The old 45 s
budget was effectively a multi-minute boot: a browser allows six concurrent
HTTP/1.1 connections, so six requests parked on the runtime held every socket
and queued even Python-free routes (the chat transcript) behind them for
45–55 s. `HARNESS_PY_REQUEST_WAIT_MS` overrides the budget per host.

### Alert delivery

A fired price alert is dispatched from `src/lib/alerts/notifier.ts`
(`dispatchAlert`, the single dispatch point). It always writes a
`PriceAlertEvent` — the durable in-app notice **and** history that
`GET /api/alerts/live` serves directly (there is no separate in-process ring
buffer, which would reset on restart and diverge across workers) — then sends to
every enabled AND configured external channel (Telegram / Discord / ntfy /
Email / webhook, config under `SystemConfig["alert_channels"]`).

Two client surfaces consume the fired-alert stream:

1. **In-app notices** — the `/alerts` screen polls `/api/alerts/live` every 5s
   and plays `sound.notification()` on a genuinely new event. Because that screen
   is only mounted on its own tab, this is silent elsewhere in the app.
2. **App-wide delivery** — `AlertNotificationProvider` (mounted once in
   `src/app/layout.tsx`) polls the same `/api/alerts/live` feed globally (10s) and
   surfaces each new event two ways: an **always-on in-app toast** (bottom-right,
   every tab, no permission needed) and a **native OS notification** when the user
   has opted in and granted permission. It is the single owner of fired-alert
   delivery; any surface (e.g. the Settings test button) raises the same pair via
   the `be:alert-notify` window event rather than calling the notifier directly.
   `src/lib/alerts/browser-notifier.ts` wraps the Web Notification API (opt-in
   `be_alerts_browser_v1`, default OFF). New events are found with
   `src/lib/alerts/event-diff.ts`, which tracks a bounded set of seen event ids
   (not just the last one) precisely because the feed is capped at 50 rows. This is the one delivery
   path that reaches the laptop itself. It is **opt-in** (localStorage
   `be_alerts_browser_v1`, default OFF), requests permission only from an
   explicit user gesture, and is a no-op unless permission is actually granted —
   the Settings toggle reports `blocked`/`off`/`on` honestly. The API also
   requires a **secure context** (`https://…`, or the loopback names
   `localhost`/`127.0.0.1`). The desktop window loads
   `http://127.0.0.1:3100`, which is loopback and therefore secure, so desktop
   notifications work without any TLS tooling (the `blackeclipse` hostname and
   its self-signed certificate are gone with the browser lane).
   The Settings panel names this when the origin is insecure (`secureContext()`
   in `browser-notifier.ts`). There is **no service worker**, so notifications
   appear only while a BlackEclipse page is open in the browser; the UI states
   this rather than hiding it. Toggle + "Send test notification" live in Settings
   › Alerts/Notifications (`alert-notifications-settings.tsx`).

### The Channel Gateway (inbound, remote control)

Alerts are outbound; the **Channel Gateway** is their inbound counterpart. It is
a core Python subsystem (`python/harness/channels/`, ADR-H199) that lets a user
connect an external account (Telegram, Discord, email, …) and drive Eclipse from
it. `ChannelService.handle_inbound` is the ONE entry: it enforces idempotency
(native message ids), resolves the sender to a **verified `ChannelBinding`**
(created by a short-lived, single-use `/link <code>`), rate-limits per binding,
and dispatches. `ChannelDispatcher` gates by control scope (`viewer` <
`assistant` < `operator`), routes `/commands`, agent turns (through the ONE
`service.run`) and the operator `/do` tool bridge (the ONE `ToolRegistry` + the
Security Gateway effect authorizer), and requires a single-use in-channel
`/confirm` for mutations. It forks nothing — bindings live on the ONE
`agent-memory.db`, secrets resolve through the gateway's `SecretsManager`
(`secret://` refs), and every dispatch is audited (`category="channel"`).
Telegram ships first because it long-polls from the local machine, so no public
endpoint is needed and zero-config is unchanged. The operator surface is
Settings › Connections (`connections-settings.tsx`); front door
`/api/channels/**`. See `docs/harness/channel-gateway.md`.

**The notification center.** Every raise also lands in an app-wide ledger —
`src/lib/notifications/store.ts` (zustand, persisted at `be_notifications_v1`,
newest-first, capped at 100). Each entry is tagged with its owning `AppTab`,
`sound` and dedupe `id`; the provider parses the same `be:alert-notify` detail
(`{ id, title, body, tab, kind, sound, alertId, test }`), so price alerts, agent
approval requests (`use-agent-run.ts`), durable task completions and bot runs
all flow through ONE delivery point. The sidebar renders a red unread badge per
tab (count, `9+` cap) and an aggregate badge on the Terminal/Agentic/Bots menu
switcher for tabs the active menu hides; opening a tab (the sidebar is the
shell's visible-tab signal) marks that tab's entries read, and an entry raised
for the visible tab is recorded read on arrival — it is already in front of the
operator. The ledger is also mirrored to the durable `AppNotification` table
(`POST /api/notifications`, idempotent on the producer id; the local ledger is
backfilled once on hydrate). Alerts › **History** reads the union of that archive
and the price engine's `PriceAlertEvent` log through `GET /api/alerts/history`
(search + kind/tab + 24h/7d/30d/all filters), so every alert ever received stays
findable even after the in-memory ring rolls over or site data is cleared. The
header adds the **in-app notification bar**:
`src/components/layout/notification-center.tsx` renders a bell with the unread
count and a frosted panel listing the full ledger, filterable by kind (alerts,
tasks, approvals, agent, bots). Selecting an entry marks its tab read and
dispatches the shell's `be:navigate` event; **Mark all read** clears every tab
(`markAllRead` on the store). Opening the panel alone does not mark anything
read, so unread items stay scannable. The **browser tab itself** is the
out-of-app surface: a root-mounted `src/lib/notifications/tab-indicator.tsx`
turns the ledger into a tab signal while anything is unread — the document
title carries the count (`(3) BlackEclipse — …`, `(99+)` cap; pure helpers in
`title.ts`), the favicon becomes a red-badged copy of the LIVE icon
(canvas-composited so a theme-customized favicon is respected; the plain icon
returns at zero), and `navigator.setAppBadge` is set where supported. The theme
store composes its branded title through the same prefix helper
(`composeDocumentTitle`), so a branding write never silently drops the count.

Background **task completion** is its own producer: `task-watcher.tsx` (mounted
once in the root layout) polls `GET /api/tasks` every 15s, diffs task statuses
via the pure `task-diff.ts` (first poll seeds; a task that reaches `COMPLETED`
or `FAILED` since the last poll is announced once, keyed by
`task:<id>:<completedAt|updatedAt>`), and raises a `tasks`-tab notification —
completed tasks with the loud `taskdone` cue. **Bot runs** have their own
watcher: `bots-run-watcher.tsx` (also mounted in the root layout) sweeps
`GET /api/automations` plus one status call per active bot (capped at 20, 30s)
and diffs each bot's `lastExecution` via the pure `bots-run-diff.ts`; a run that
reaches a notifiable terminal state gets a `bots`-tab notification (completed →
`taskdone`, failed/timed out/blocked → the alert cue; cancelled/skipped stay
quiet). Separately, the bot chat surface (`bot-chat.tsx`) polls the selected
bot's execution history and mirrors every terminal run that produced an answer
INTO the conversation thread as a tagged **Auto run** assistant turn —
idempotently, keyed by execution id (`useBotChatStore.appendScheduled`) — so an
unattended run is readable in the thread itself, not only in Details → Run
history and a toast. A finished chat reply raised while the operator is on
another tab is announced the same way from `use-agent-run.ts` (ChatBot tab,
`taskdone` cue);
while the ChatBot is visible the surfaces keep their soft `message` cue.

**High-alert news** is a root producer too: `news-watcher.tsx` polls
`GET /api/news/alerts` every two minutes (watchlist symbols sharpen the rank),
seeds the seen set on the first poll without notifying, then raises a
`news`-tab `alert` for each newly seen `critical`/`high` story and each
high-impact release inside a 90-minute horizon. It deliberately does NOT
backfill the current set into the ledger, so historical news cannot crowd out
price-alert/task/agent entries (the other watchers seed the same way). The News
tab itself no longer renders a separate High Alert band — the same stories are
ranked into the main feed with `sort=importance`, most-critical first. The
header panel always shows every kind filter (count 0 included) so all producer
lanes are discoverable.

Delivery is user-controlled in Settings › News › Pop-ups & notifications
(`src/lib/news/notification-prefs.ts`, `be_news_notify_v1`): background watch
(off stops polling and clears the seen set), in-app pop-up, desktop
notification, alert sound, upcoming releases, watchlist-only stories and the
minimum importance score (55/65/75). The ledger entry is still recorded while
the watch is on, so silencing the visual lanes never hides a story from the
bell. Each lane maps onto `NotificationRequest.toast`/`desktop`/`sound`, which
the one provider honours — every other producer keeps both visual lanes on by
default. Card layout on the News tab is the same `be_news_card_settings`
surface, editable both inline and in Settings › News; `saveCardSettings`
emits `be:news-card-settings` so a mounted News tab updates live.

## The two workspace systems (read this twice)

| | Widget workspace | Market workspace |
|---|---|---|
| Engine | `src/lib/workspace/` | `src/lib/market-workspace/` |
| UI | `src/components/workspace/` | `src/components/market-workspace/` |
| Powers | Command Center dashboard | Chart Workspace tab |
| Unit | widget card in a column | chart/market panel |
| Shape | track board: sections → columns (tracks) → widgets, packed masonry, + `left`/`right` zones | panels with presets/ops |
| Persistence | `localStorage` via `use-workspace` | `localStorage` via `use-market-workspace` |

### Widget workspace zones (three-column shape)

A layout is a flat list of sections, each tagged with an optional
`zone: 'left' | 'main' | 'right'` (undefined = `main`). The canvas renders all
`left` sections in a narrow column, all `main` sections in the centre, and all
`right` sections in a second narrow column — **all under one `DndContext`** and
**one Customize surface**, so a card drags freely between the three. Each
section's columns are its vertical tracks; within a multi-track section the
compose-time masonry packer balances cards (see the track-board grammar above),
and cards size to content with a per-widget floor.

Key invariants:

- **No second engine / no second customization surface** for a side column. The
  Command Center's Watchlist (left) and Market (right) are ordinary workspace
  sections, not separate panels.
- **Legacy `rail` = `right`.** Stored layouts from the two-zone era are mapped
  `rail → right` by `normalizeLayout` on read (no storage-prefix bump).
- **Side-zone migration.** `useWorkspace` seeds missing default `left`/`right`
  sections once (persisted), so existing dashboards gain the side columns without
  losing their centre arrangement.
- **Remove ≠ delete.** `setZoneHidden(zone, hidden)` records the zone in
  `layout.hiddenZones`; its sections stay in the tree so re-showing restores the
  exact widgets. Exposed as the **Left panel / Right panel** toggles in Customize.
- Per-zone presentation (`className`, `header`, `collapsed`, `onToggle`) is passed
  via `CustomizableWorkspace`'s `zones` prop; the old `rail*` props are deprecated
  aliases for `zones.right`.
- **Layout tabs are drag-to-reorder.** `TabBar` takes an `onReorder(fromId, toId)`
  prop (HTML5 drag on each tab); `market-tabs.tsx` maps it through
  `useMarketViews.reorder`. Double-click still renames and widget
  rearrangement (drag/click-to-place) is unchanged.
- **Density is a host preference, not a card mode.** `CustomizableWorkspace` /
  `WorkspaceCanvas` take a `dense` prop (default `false`) that only tightens
  gutters — inter-zone `gap-4→gap-1.5`, section stack `gap-3→gap-1.5`, column
  and card gaps `gap-2.5→gap-1.5`, section wrapper `p-1→p-0.5`, and header /
  edit-bar / toolbar margins `mb-3→mb-2`. The Command Center passes `dense`.
  It changes NO sizing semantics (no fixed card height) — it is not the removed
  compact/row-height mode.
- **An emptied centre is still a drop target.** When the `main` zone has no
  section, the canvas renders an `EmptyMainZone` placeholder tagged
  `data-emptyzone`; `computeTarget` resolves a drop or library add-project click
  on it to `columnId:'__newmain'`, which commits through
  `ops.addWidgetToNewSection` / `moveWidgetToNewSection` (always creating a
  `main` section). Without this, an emptied centre made `computeTarget` return
  `null` and both drag-drop and library adds silently did nothing.
- **Side panels are drag-resizable.** A vertical handle between each side zone and
  the centre sets that zone's width live (clamped 180–520px) and commits on
  release through `ops.setZoneWidth`, persisted in `WorkspaceLayout.zoneWidths`
  (presentation-only — it never touches the section tree). Resizing works in both
  normal and edit mode; `main` is not resizable.

Both workspace systems are real and both are supported. The **retired** thing is
`src/lib/customize/` — an earlier kernel that is documented as superseded and
must not receive new features. See ADR-005 and ADR-006.

### Daily Dashboard (`home` tab) — the fixed decision surface

The **Dashboard** tab (`AppTab 'home'`, `/?tab=home`, F2 in the shipped defaults)
is deliberately *not* the Command Center. The Command Center is the user-built
widget board; the Dashboard is a fixed, read-only daily brief in the AlphaGen
homepage mould: personal brief → summary tiles → leadership/regime → portfolio
health/trend mix → attention buckets → portfolio watch → watchlist radar →
concentration → full-universe CTA → alerts archive. It is global-first (default
scope `GLOBAL`) with a market selector persisted in
`localStorage['be_home_dashboard_market_v1']`.

Data path (real JDDS only; no simulated series):

- `src/lib/dashboard/use-home-data.ts` fans out to `/api/regime`,
  `/api/heatmap?market=…`, `/api/global-overview` (GLOBAL only),
  `/api/price-history?symbol=…&market=…&range=6mo` (benchmark) and `/api/alerts`,
  while the existing portfolio/watchlist hooks supply held positions and saved
  names. A market-scoped view filters holdings by the quote's real `marketId`.
- The **regime** card renders `/api/regime` verbatim. When its core
  price/breadth input is stale or missing, `coreAvailable` is false and the card
  says **Unavailable** — it never turns that into a neutral call. The hero may
  fall back to a **breadth-derived weather** tone (`weatherTone`: cap-weighted
  market change + advancing count), explicitly labelled `Breadth-derived`.
- Portfolio **health** is Eclipse's own deterministic trend score, not a
  fundamental rating: positions are classified `uptrend` / `weak_uptrend` /
  `downtrend` from SMA20 vs the long average, weighted by USD value, with a
  concentration penalty above 35% single-name weight. Coverage below 30% of book
  value returns an unavailable score. The sample fund book is seeded demo data
  and is labelled **Demo book**.
- **Leadership** is relative strength: GLOBAL ranks markets from cached index
  changes in the global overview; a country view ranks `/api/heatmap` sectors
  against the cap-weighted market change. "Advancing" and "beating benchmark"
  are separate counts.
- **Watchlist radar** shows unheld saved names by absolute session move; it does
  not claim a breakout. **Concentration** groups held value by the quote's JDDS
  sector (falling back to `Unclassified`) and folds the tail into `Other`.

UI lives in `src/components/screens/dashboard/home-dashboard.tsx` (layout,
selector, skeletons), `home-cards.tsx` (cards) and `home-primitives.tsx`
(glass tiles, health ring, sector donut, SVG sparklines). Pure scoring lives in
`src/lib/dashboard/home-model.ts` and is covered by `home-model.test.ts`.

### Dashboard layout grammar (personas + track board)

The *default* arrangement of the Command Center is composed by the pure,
unit-tested engine in `src/lib/dashboard/layout.ts` (registry stays about
widgets, this stays about arrangement):

1. **Templates** (`src/lib/dashboard/templates.ts`) are authored as **widget-id
   specs**, never registry indices — inserting a widget in the registry can no
   longer silently repoint a template. A template is **centre-only**. Each
   persona authors one or more bands (`full(...)` or `board(widgets, tracks)`).
2. **`buildMarketViewLayout`** frames the template with the two thin side zones
   (left = Watchlist; right = the market's asset-class `mp-*` stat tiles) and
   composes everything through `composeBoard`.
3. **`composeBoard`** is the single enforcement point for the invariants:
   - **global first-wins dedupe** across every zone (a widget renders once, never
     in both the centre and a rail),
   - **build-time 4-track cap**, clamped to the card count so a band never
     creates an empty track,
   - **content sizing** in two modes — a FRAME (`defaultHeight`, for charts /
     maps / live video / scrollable tables & feeds) whose body stretches to fill
     the card, or a GROW floor (`minHeight`, for compact tiles). See
     `WIDGET_FRAME_HEIGHT` / `WIDGET_MIN_HEIGHT`; a `BoardCell` of `{ id, h }`
     can override a widget's frame for deliberate asymmetry,
   - **masonry balance** — `rebalanceColumns` (in `src/lib/workspace/pack.ts`)
     distributes each section's cards into the shortest track by estimated
     height,
   - unknown ids dropped.

The side rails are each a **single stacked section**, not one section per tile.
The old per-widget sections wrapped every tile in its own section gap + padding,
which is what made the dashboard look full of gaps.

**Gap-free by construction.** In the retired row-band grammar a section was a
rigid full-width row of content-height columns (`items-start`), so any short
widget left dead space beside a tall sibling, and the side rails (pinned 300px /
4×190px) left ~490px blank under the Watchlist. The track board instead treats a
section's **columns as vertical tracks** and packs cards into the shortest one, so
the next card flows underneath a short one. Cards size to content with a floor
(no oversized pinned bands to clip against), and the centre defaults to
**`CENTRE_TRACKS` (3)** balanced tracks — enough room that KPI tiles never
truncate. The 4-track engine capability remains for user drag/drop.

**Packing is compose-time, not edit-time.** `rebalanceColumns` runs when building
defaults, templates and imports — never inside the store's `commit`. Re-packing on
every edit would silently reshuffle cards the user just arranged (masonry has no
stable fixed point for equal-height cards), so a drag commits exactly where it was
dropped and the packer only shapes starting layouts.

**Twelve personas ship, each with a genuinely different SHAPE (band structure)
and widget set — not index shuffles:** `tape-reader` (full chart → wide tape →
two-up → TV), `desk` (dense 4-track: chart + heatmap + TV top row), `allocator`
(portfolio: performance hero → holdings → risk), `rates-desk` (economic chart +
rates + calendar), `chain-desk` (crypto hero row → chart → venue grid),
`fx-desk` (FX pairs hero → chart → drivers/macro), `screener` (heatmap +
screener full → momentum grid), `map-room` (map hero → event feed → conflict
layers), `newsroom` (news + calendar lead, charts second), `risk-desk` (regime +
risk above the chart), `trader-pro` (dual charts + candle + tape), `research`
(news categories + agent work + charts).

A band cell may be a plain widget id or `{ id, h, min, frame }`, letting a
persona pin a specific frame height where the default would read badly.

**A `defaultHeight` card gets a definite `height`, not `min-height`.** Frame
widgets (charts, maps, live video, scrollable tables) put `absolute inset-0` /
`h-full` children inside a `flex-1` body; those only resolve against a definite
height. Rendering a frame as `min-height` left the ECharts canvas at **0px** —
the "blank charts/maps/heatmaps" bug. `cardStyle` therefore emits `height` for
`defaultHeight`, and `minHeight` remains a grow floor for compact tiles. Widgets
added from the library carry their intrinsic sizing (`addWidget(..., sizing)`).

**Storage v8 is a deliberate RESET.** v7 replaced the row-band grammar with the
track board; v8 fixes the persona rename. Because the nine personas gained new
ids but stored layouts keep the OLD id in `templateId`, `getDashboardTemplate`
returned `undefined` and the centre rendered EMPTY (only the side rails).
`templates.ts` now ships `LEGACY_TEMPLATE_IDS` + `resolveTemplateId` (old ids
resolve to their renamed persona; unknown/empty ids fall back to the first
persona), `use-workspace` self-heals a fully blank stored layout, and the
storage prefix reseeds everyone (`be_dashboards_index_v8` /
`be_ws_dashboard_v8_`; older keys are absent from `LEGACY_INDEX_KEYS`; fund
layouts are `be_ws_fund_*_v3`). **When renaming a persona, always add its old id
to `LEGACY_TEMPLATE_IDS`.**

`WorkspaceLayout.version` is `2`; `normalizeLayout` upgrades any stored payload
to v2 on read (mapping legacy `rail → right`, preserving `minHeight` /
`defaultHeight` and `zoneWidths`).

### Command Center fullscreen

`DashboardScreen` has an in-app fullscreen mode (`Shift+F`; header button; `Esc`
exits). It renders the **real app `<TopBar activeTab="dashboard" />`** — the live
price ribbon and the global ⌘K search — so fullscreen keeps the market chrome
rather than inventing its own bar. There is no dedicated full-width bar; a
floating **Exit** button sits bottom-right. The overlay is **portaled to
`<body>`**: the app shell is wrapped in a glass-card whose `backdrop-filter`
establishes a containing block, so a plain `position: fixed` overlay would be
trapped inside it and the global nav would stay on top — the same reason the
Chart Workspace portals its full-page overlay. Body scroll is locked while open;
the workspace owns the height and scrolls.

Because the overlay sits ABOVE the app shell, the shell's Aurora (`z-0`) is
covered — so fullscreen mounts its own **`<AuroraGlow>`**
(`src/components/layout/aurora-glow.tsx`). `AuroraGlow` is the single reusable
glow: it owns the Market Regime subscription (`regime:<scope>:<market>` topics
via `src/lib/regime/client.ts`) and the user configuration (palette + motion +
shape), and renders the masked top band. The shell and fullscreen both use it, so
the sentiment model stays in one place (`src/lib/regime/`). Any future
full-viewport overlay must do the same — the shell's glow does not reach above it.

### Market Regime (the Aurora sentiment model)

Aurora's color/motion is driven by an explainable **Market Regime** reading, not
a raw blend. The model lives in `src/lib/regime/`:

- `types.ts` — the serializable contract (`RegimeResult`, `RegimeComponent`,
  `RegimeScope`, `RegimeId`).
- `scoring.ts` — pure, dependency-free scoring: per-component scores,
  weight renormalization, coverage/freshness/confidence, and the regime
  classifier. Unit-tested in `scoring.test.ts`.
- `client.ts` — a DataHub hook (`useRegime`) over `GET /api/regime`.
- `store.ts` — persisted scope (`selected` | `us` | `global`).
- `visuals.ts` — regime → Aurora `{ score, dim }` mapping.

`GET /api/regime?scope=&market=` assembles five components from **JDDS only**
(never a second data source): `price_breadth` (IndexQuote + Asset sectors),
`volatility` (IndexQuote ^VIX, MacroSeries VIXCLS fallback), `macro_risk`
(economics calendar surprises), `news_narrative` (NewsItem, tier-weighted,
recency-decayed, deduplicated, market-relevant), and `positioning` (honestly
UNAVAILABLE until a survey such as AAII is ingested). Each component exposes its
score, the weight **actually used**, availability, timestamp, source and
confidence. Missing inputs lower confidence; a stale input is excluded; a
missing core `price_breadth` input makes the whole reading `unavailable` — never
a false neutral. The taxonomy is transparent: Risk-on broadening / Fragile rally
/ Mixed / Risk-off / Narrative shock / Unavailable.

The background glow stays decorative (`pointer-events-none`, `aria-hidden`).
The interactive, accessible surface is the top-bar **Market Regime** control
(`src/components/layout/market-regime-control.tsx`, mounted in `top-bar.tsx`):
a frosted-glass panel showing regime/score/confidence/freshness, the
contribution breakdown, "what changed since last update", drivers, and actions
(open Market Overview, filter news, ask Eclipse, create a regime-change alert).

### Aurora customization (Settings › Aurora)

A user can recolor, reshape AND restyle the glow without ever touching the
sentiment model. The full customization is an `AuroraConfig` in
`src/lib/aurora/config.ts`: the **palette** (the three sentiment color pairs,
unchanged), **motion** (intensity/speed/response multipliers), **shape** (height
in px, softness, wave detail) and **style** (how the field is rendered). `motion`
and `shape` are applied as **multipliers on top of** the baseline that
`resolveAuroraVisuals(score, palette)` already produces (plus the `uNoiseScale`
uniform), and `style` selects a fragment-shader branch — so how sentiment maps to
color is never altered. Styles are `ribbon` (shipped), `dots` (LED dot matrix),
`scanlines`, `pillars` and `blocks`; each non-ribbon style uses a `grid` cell size
in px. The zustand store `src/lib/aurora/store.ts` holds the four slices and
persists them as one config under `be_aurora_config_v2` (migrating a legacy v1
palette forward); the panel is `src/components/screens/aurora-settings.tsx`,
embedded in the Customization tab. `Aurora.tsx` accepts the scale/style props and
updates the corresponding uniforms per frame (the WebGL context is never
rebuilt). All changes apply live and are device-local — no server round-trip.

### Chat session workspaces (context documents + agent output)

Each chat conversation owns a real folder on disk holding the documents the user
attaches as context and the answers the agent produces:

```
<Black-Eclipse-site>/.black-eclipse/sessions/<chatId>/
  context/         files the user added (extracted to text, injected into runs)
  output/          files the agent produced (one `answer-<stamp>.md` per run)
  transcript.json  the conversation's durable transcript (server-side copy)
```

- **The transcript is durable and origin-independent.** The ChatBot tab's
  conversation lives in browser `localStorage` (`chats_guest`) as an instant
  cache, but `src/lib/chat/transcript-client.ts` also mirrors each conversation
  to `transcript.json` through the route `api/chat/transcript` (GET one/all,
  POST upsert, DELETE), backed by `readTranscript`/`writeTranscript`/
  `listTranscripts`/`deleteTranscript` in `src/lib/chat/session-store.ts`. On
  boot `stock-chat.tsx` hydrates from the server and merges it with the local
  cache (server wins per id, local-only conversations are pushed up), so a chat
  survives a server restart, a different origin, or cleared storage. Sync is
  debounced; conversation deletions remove the server copy. This is what fixes
  the "chat is deleted when the server restarts / the URL changes" class of bug.
- **Boot reads metadata, not every message body.** `GET /api/chat/transcript`
  with no query is the full archive (every message of every conversation — 16 MB
  on the reference host); the boot path uses `?summary=1`, which returns the same
  rows with `messages: []` plus `messageCount`. The client hydrates the open
  conversation (and any conversation opened from the sidebar) with
  `?chatId=…`, so opening the app no longer serializes, gzips and parses the
  whole archive on the single Node thread. Keep both modes: the full list is
  still what backups/migrations and any external caller expect.

- **The front door owns the filesystem; the Python runtime stays read-only.**
  Every harness tool is `permission="read"` and `tools.py` hard-rejects writes,
  so documents reach the agent by *extraction*, not filesystem access:
  `src/lib/chat/extract.ts` turns a stored file into text and
  `loadContextDocuments` feeds it into `RunInput.documents`. `python/harness/
  graph.py` (`_user_message`) wraps them as `<document name="...">` blocks in the
  user turn — user-supplied evidence, clearly attributed, never presented as
  retrieved JDdS data.
- **Storage lives in `src/lib/chat/session-store.ts`** (server-only) and the
  routes `api/chat/session` (list/upload/delete) + `api/chat/session/file`
  (download). The chat id is a path segment, so it is validated against
  `^[A-Za-z0-9_-]{1,64}$` and every resolved path is re-checked to stay inside
  the session root; file names reject traversal and dotfiles.
- **`/api/harness/chat` does the wiring** when given a `chatId`: it loads the
  context documents, passes them to the agent, and best-effort writes the output
  to `output/`. A session failure never fails the chat and never fabricates
  output.
- **Root is gitignored** (`.black-eclipse/`), the same tier as `agent-memory.db`.
  Override with `BLACK_ECLIPSE_SESSION_ROOT` (used by tests).
- **UI:** adding documents is the composer's **"+" button** (there is one attach
  affordance, not two). `src/lib/chat/use-session.ts` is the shared client
  controller — the "+" uploads into it and `src/components/chat/session-bar.tsx`
  displays the folder path, a Context/Output file list with download + remove,
  and a "context used" meter (`contextChars / contextLimitChars` — a real
  measurement from extraction, never an estimate). The bar is display-only and
  renders nothing until the conversation has files. Supported types: txt/md/csv/
  json/log/yml/yaml/xml/html/sql/ini/toml and simple text-stream PDFs; binary
  types are rejected at extraction.
- **The composer tracks the run.** The client generates the run id
  (`crypto.randomUUID`) and passes it with the chat request, because the server
  only reports a run id once the run is already finished. That id drives the
  **pause button** (shown while `busy`), which POSTs to
  `api/harness/runs/[id]/cancel` — the runtime's cooperative cancellation. The
  client-side run id is validated server-side (`^run_[A-Za-z0-9-]{1,64}$`) and
  ignored when malformed.
- **The composer model picker is real, not decorative.** `src/lib/agent/
  use-chat-model.ts` backs the composer's provider pill as an ALWAYS-VISIBLE
  dropdown (`ModelSelector` in `chat-input.tsx`) of the models the operator can
  actually use, across providers. Options come from
  `GET /api/harness/chat/models`, which reports each preset's models (runtime
  catalog, falling back to the preset default when the runtime is down) and its
  availability derived server-side from the real env/vault (`available` is a
  boolean — a key is never returned). The top level lists the models of EVERY
  provider usable right now (keyless local servers + any provider with a
  resolving key), the active provider first, so a second connected provider is
  usable directly without first switching the active provider — selecting a
  model switches the active provider to that model's provider. Selecting a model
  persists it to the settings vault (`POST /api/settings`) — the single source
  the runtime reads — so the next message uses it, AND sends it per-turn as
  `model` for belt-and-suspenders; switching provider persists the provider +
  base URL + its default model together. A failed save shows the error and
  reverts. Local/router providers get a live `discover` action; a "Custom model
  id…" entry types any id. It never fabricates a list — with no data it still
  renders and lets a custom id be entered. (The old hardcoded
  "Flash-Lite/Flash/Pro" selector was removed precisely because it was fake.)
- **Discovered models are durable, replaced per probe, and shown newest-first.**
  A "Discover installed models" probe registers the provider's models into the
  Python runtime's in-process `ModelRegistry`, which is rebuilt on every runtime
  restart and was lost on reload — forcing the operator to click Discover again
  and again. The `POST /model-runtime/providers/{id}/discover` route also writes
  the found ids to a durable snapshot in `SystemConfig`
  (`src/lib/agent/discovered-models.ts`, key `discovered_models_v1`; a
  convenience list, never an allowlist). `providers[providerId]` is **replaced**
  by each successful probe (not merged), so a model the provider no longer lists
  stops appearing; other providers are untouched. Both catalog routes
  (`/api/harness/models`, `/api/harness/chat/models`) prefer the snapshot's
  `served` set (the provider's OWN ids, replaced per probe) and fall back to the
  accumulated set + runtime registry, so discovered models survive a reload AND a
  runtime restart without re-probing. `served` distinguishes an installed model
  from a declared placeholder the server does not serve, so the composer's
  default can resolve to a model that will not `404` (the
  `llama3.1`-on-a-cloud-only-Ollama failure); an explicit user pick is never
  overridden.
- **Recency is a first-class ordering, not incidental.** Providers that report a
  `created` timestamp (the OpenAI-protocol families, Anthropic) are sorted
  newest-first by the probe (`python/harness/model_runtime/providers/`), and the
  TS surfaces apply `sortModelsNewestFirst` (`src/lib/agent/model-recency.ts`):
  provider timestamp first, then version tuple within the same family, then a
  `-latest` tiebreak, otherwise the incoming order is preserved. The Settings
  picker silently re-probes a provider when its last attempt is older than
  `AUTO_DISCOVER_TTL_MS` (throttled even on failure, stored under
  `be_model_autodiscover_v1`), so a release since the last visit appears without a
  manual click. An uncurated compact list is newest-first *before* it is capped at
  `MODEL_LIST_SOFT_LIMIT`, so truncation keeps the latest models rather than
  cutting them off the bottom.
### AI provider connections (the one connect path)

An LLM connection is a first-class object with one lifecycle, not a set of
independent fields. `src/lib/providers/registry.ts` is the single provider table
(the UI presets in `agent/presets.ts` are derived from it, and a parity test
checks it against `python/harness/models.py`). It declares each provider's
protocol, auth style, base URL, default model, vault env var and key prefixes.

- **Paste-first, one action.** Onboarding and Settings both mount
  `components/providers/provider-connect.tsx`. A pasted key is detected from its
  prefix; `POST /api/llm/connect` validates → probes with the provider's real
  auth (`bearer` / `x-api-key` / `x-goog-api-key`) → discovers models → saves the
  key and active config atomically. A failed probe writes nothing, so a bad key
  never replaces a working provider.
- **A config may exist before its key.** `setLLMConfig` no longer rejects a
  remote provider with no key; the connection is simply reported as
  `needs-key`, and chat fails with `PROVIDER_NOT_CONFIGURED`. The old rejection
  was the root cause of "the screen says connected but chat uses the old
  provider": the UI had already changed but the server refused the write.
- **Custom gateways are plural and auth-aware.** Named connections live in
  `llm_custom_connections_v1`; each has its own vault entry
  (`CUSTOM_LLM_KEY_<ID>`) and an auth style (Bearer / `x-api-key` / `api-key` /
  custom header+prefix). `llm_config` stores the `connectionId`, and
  `getLLMConfig` resolves that connection's key + header on every read.
  `modelAuthFields` adds `apiKeyHeader`/`apiKeyPrefix` to every runtime
  `modelSettings`, and `python/harness/models.py` maps them to
  `ChatOpenAI(default_headers=…)` with a placeholder API key so the real key is
  not also sent as a Bearer token.
- **Named providers keep their own key.** `llmKeyEnvVar` prefers the named
  provider over the base URL, so editing the URL to a regional endpoint/proxy
  still resolves `OPENAI_API_KEY`; only `provider: custom` uses
  `CUSTOM_LLM_API_KEY`.
- **The test is a real request.** `/api/llm/test` (and the compatibility
  `/api/agent/test`) make one tiny chat request and retry without probe-only
  parameters when a reasoning model rejects them. A 2xx model list alone is never
  reported as "working".
- **No fake connected state.** Keyless/local providers are not listed as
  connected until they are the active configuration or their key is stored; the
  UI distinguishes `Active` / `Connected` / `Not connected`.

See `docs/model-connections.md` for the contract, routes and test map.

- **Every provider can enumerate its live models — not just local servers.**
  Discovery used to be gated on local servers + routers (`Ollama`/LM Studio/
  OpenRouter); a hosted API (OpenAI, Groq, DeepSeek, Mistral, Anthropic,
  Gemini, xAI, Together, Perplexity, Fireworks, OpenCode) showed only its
  declared sample. Now `ProviderSpec.dynamic_discovery` is true for all of them,
  and the probe is protocol-generic in `python/harness/model_runtime/providers/`:
  `probe_openai_models` (`openai.py`) handles `GET {base}/models` with a
  `Bearer` key for every OpenAI-protocol family (`OpenAIAdapter`,
  `OpenAICompatibleAdapter`, and Mistral/Groq via `first_party.py`), while
  `AnthropicAdapter`/`GoogleAdapter` override `discover_models` for their own
  list shapes. `OpenCodeAdapter` is the one adapter whose chat and catalog bases
  differ: chat is `…/inference/openai/v1`, but the gateway publishes models at
  `…/inference/v1/models` (`opencode_models_url` rewrites the base). Because the
  browser never holds a provider key, the Next discover
  route (`src/app/api/harness/model-runtime/providers/[id]/discover/route.ts`)
  resolves it server-side from the env/vault (`resolveProviderKey(preset.envVar)`)
  and falls back to the preset base URL, so a private endpoint (which would 401
  keyless) enumerates too. The result is cached in the durable snapshot above.
- **Big catalogs are curated, not dumped: the Manage models dialog.** A provider
  can list hundreds or thousands of models (OpenRouter returned 466; Ollama can
  list many), which is unusable in a dropdown. Discovery now flows into a
  **Manage models** dialog (`src/components/screens/manage-models-dialog.tsx`):
  a searchable, checkbox list of the full catalog where the operator enables only
  the models they use. Enabled ids persist per provider in localStorage via the
  shared zustand store `src/lib/agent/enabled-models.ts` (`be_enabled_models_v1`).
  The pure `compactModels()` helper applies that curation wherever a compact
  picker renders (Settings `ModelPicker` and the composer `ModelSelector`): a
  curated provider shows exactly its enabled ids (capped otherwise at
  `MODEL_LIST_SOFT_LIMIT`, with a "Manage" affordance), while an uncurated one
  shows the declared list up to the limit. This is a client-side FILTER, never an
  allowlist — the runtime still accepts any id and the "Custom model id…" entry
  always works. Both surfaces share one dialog and one store, so a curation made
  in Settings is immediately reflected in the composer. The dialog is portaled at
  `z-[130]`, above the Settings modal (`z-[100]`) and the composer menu
  (`z-[110]`); opening it from a provider menu closes that menu first, and the
  no-arg "Manage models…" path falls back to the active/first usable provider, so
  the action is never a silent no-op.
- **Reasoning effort is the per-run "model variant".** The runtime has no
  per-model variant registry; its real knob is reasoning effort
  (`none/low/medium/high/very_high/max`, `python/harness/model_runtime/catalog.py`
  `REASONING_LEVELS`), normally chosen by the task profile. One vocabulary is
  shared by every surface (`src/lib/agent/reasoning.ts`): Auto / Minimal /
  Standard / High / Ultra, mapping Auto → no override and Minimal/Standard/High/
  Ultra → `low`/`medium`/`high`/`very_high`.
  - **Settings › AI** pins a GLOBAL default as `LLMConfig.reasoningEffort`
    (`setLLMConfig`; `null` clears it back to Auto), round-tripped through
    `/api/settings`.
  - **The chat composer** (`EffortControl`, `src/components/chat/effort-control.tsx`)
    is a PER-QUESTION override on the Faster→Smarter slider. It renders only when
    `ChatInput`/`EmptyState` is given an `effortState` (from `useReasoningEffort`,
    persisted client-side at `be_reasoning_effort_v1`); the chosen level is sent
    with the next run only and never writes the saved default. Auto sends no
    override.
  - The run front door forwards the effective effort as `input.context.reasoning`
    (`run-service.ts` `reasoningContext`: per-run override **outranks** the saved
    default), which the router reads as a `reasoning` requirement and
    `prepare_model` applies to a model's NATIVE control only (OpenAI reasoning
    models, Anthropic `thinking`, Gemini `thinking_budget`) — an
    OpenAI-compatible endpoint that does not declare reasoning is never
    force-fed the knob. An explicit run effort outranks the task profile's
    default, and with neither the task profile decides.
  - **Minimal and Ultra, precisely.** `low` (Minimal) is a deliberate no-op:
    the runtime leaves the model untouched for `none`/`low` (it never forces
    thinking *below* a model's default). `very_high` (Ultra) is normalized to
    the runtime's `max` tier in `prepare_model`, which each adapter maps to a
    real ceiling — Anthropic `thinking` budget 16384 and Gemini `thinking_budget`
    24576 (up from 8192 at High); native OpenAI's `reasoning_effort` has no tier
    above `high`, so its adapter caps Ultra at `high` (honest, not a silent
    downgrade that looks stronger than it is).
- **Sampling defaults are global and opt-in.** Settings › AI also pins optional
  `LLMConfig.temperature` (0–2, clamped in `setLLMConfig`) and
  `LLMConfig.maxTokens` (1…1M). Both are `null`-clearable back to **Auto** and
  round-tripped through `/api/settings`. `resolveModelSettings` forwards them as
  `modelSettings.temperature` / `modelSettings.maxTokens` **only when set**, so
  an untouched install sends the same payload as before. The Python
  `ModelSettings` contract maps the cap per provider (OpenAI-compatible,
  Anthropic, Mistral, Groq → `max_tokens`; Gemini → `max_output_tokens`);
  `build_chat_model` omits both when absent. Temperature applies to every model
  call in a run and is ignored by some reasoning models — the UI says so rather
  than implying a guarantee.

### ChatDock (the Command Center assistant composer)

`src/components/chat/chat-dock.tsx` is the floating, **bottom-centre** assistant
bar (collapsed = composer only; the transcript expands above it once there is a
conversation). It is a **view over the existing chat stack**, not a second one:
it renders `ChatHistory`, calls `getAgentProvider()` for ordinary questions and
`/api/agent/research` for research intents (the same heuristic the ChatBot tab
uses). Invariants:

- **Mounted once, app-wide, in `stock-chat.tsx` — not per screen.** The shell
  renders `<ChatDock visible={activeTab !== 'chat' && !settingsOpen} />` inside
  the keep-alive panel region, so it appears on every tab EXCEPT the ChatBot
  (where it would be a second composer beside the full chat) and while the
  Settings modal is open. The `visible` prop suppresses the dock without
  unmounting it, so its conversation/expansion survives a suppress → restore. The
  standalone `/alerts` route is outside the shell and mounts its own `<ChatDock>`;
  the Command Center's fullscreen overlay is covered by the shell dock (z-90 over
  z-80) and does NOT mount a copy.

- **It owns its own conversation.** Persisted to `localStorage`
  (`be_chat_dock_v1`), separate from the ChatBot tab's shell-owned `chats_guest`
  state, so opening it never mutates or races the full chat screen.
- **A body portal must gate itself on the HOST TAB's display, never on viewport
  intersection.** The Command Center is a keep-alive tab hidden with
  `display:none`, and a `createPortal` to `<body>` is NOT hidden by an ancestor's
  `display:none`. The dock walks up to the keep-alive panel
  (`[data-keepalive-panel]`, set by `stock-chat.tsx`) and reads its
  `display`/`visibility`, re-reading via a `MutationObserver` when the tab's
  `class` toggles. Do **not** use an `IntersectionObserver` here: the dashboard
  scrolls, a zero-size sentinel scrolls out of view, the observer reports
  "hidden", and the always-on composer disappears mid-scroll.
- **Always-on overlays are NOT `data-keyboard-overlay`.** That marker declares a
  *blocking dialog* and would disable every page shortcut while the dock is
  mounted. The dock is therefore only `data-nodrag`; the focused composer input
  already suppresses shortcuts via the editable-element guard.
- **The assistant can change its model — and its reasoning effort.** The
  composer renders the SAME `ModelSelector` and `EffortControl` the full ChatBot
  composer uses, wired to `useChatModel()` and `useReasoningEffort()`. Selecting
  a model persists it to the settings vault and is also sent per-turn
  (`agentRun.start({ model, reasoningEffort })`) — so the dock is never stuck on
  whatever model Settings last saved. The selector labels the real, persisted
  choice; it never fabricates a model name.
- **Glass tier is `.glass-card`, not `-strong`.** The pill, transcript and
  composer use `.glass-card` (tint 0.55 + blur 18). `.glass-card-strong`'s 0.85
  tint is above the frosted threshold, so its blur is invisible and the dock read
  as a flat panel.
- **Icon is `Bot` (a robot)** — the dock's minimized pill, transcript header and
  provider pill all use the robot glyph, matching the ChatBot nav item.
- **A restored pill position is clamped to the current viewport.** The minimized
  pill persists its x/y (`be_chat_dock_pill_pos_v1`); one saved on a larger
  window (or a second monitor) restored raw would sit fully off-screen, so the
  minimized assistant looked like it had vanished. The clamp effect runs once on
  mount (deferred a frame) as well as on resize, and only writes when it moves a
  value so it cannot self-loop.

It renders at `z-[90]` (above the fullscreen overlay's `z-[80]`) and takes a
`className` so fullscreen can lift it clear of the Exit button. Two independent
collapse controls: the **transcript** expand/collapse (and Escape, captured so it
does not also exit fullscreen), and a true **minimize** that shrinks the whole
dock to a restore pill — persisted (`be_chat_dock_minimized_v1`) so the user's
small-vs-big choice survives reload. The minimized pill is **draggable**: it
defaults to the viewport bottom-right corner and, once moved, anchors by
`top`/`left` with its position persisted (`be_chat_dock_pill_pos_v1`) and
re-clamped on resize (`clampPill`). Click (no drag) restores the dock; a press
that passes a 4px threshold becomes a drag. The composer overlays the board; it
does not reserve layout space.

### Shipped default dashboard board

`src/lib/dashboard/default-board.ts` exports `DEFAULT_BOARD_JSON`. Paste a
Command Center **Export** payload's `layout` there and every NEW user (and every
Reset) starts from that curated arrangement instead of a generic persona.
`getDefaultBoard()` validates it through `normalizeLayout` against the live
registry, so an unknown/retired widget id or a malformed paste degrades to the
persona template — a bad paste can never ship an empty or broken first-run
board. `DEFAULT_BOARD_JSON = null` means "ship the persona templates unchanged".
A layout explicitly created from a persona preset still honours that preset.

### Compact mode was removed

The widget workspace no longer has a compact/row-height mode. It forced every
card to a fixed 40px height, crushing widget content. Cards now size to their
widget (auto, filling the column) or to a per-placement pinned height.

## The command center (⌘K)

`src/components/layout/global-search-mount.tsx` (the always-mounted keybinds) owns
the `⌘K` toggle and the `/` chat-focus shortcut, and lazily loads
`src/components/layout/global-search.tsx` (`GlobalSearchOverlay`, the palette UI)
as a separate chunk; `src/components/layout/top-bar.tsx` (header trigger) also
opens it. Both sit on top of `src/lib/search/`. The flow is two pure steps:

1. **Grammar** (`search/grammar.ts`) — `parseQuery(raw)` splits the raw query into a
   *scope* (a leading prefix: `>` commands, `#` symbols, `@` indicators, `/` pages,
   `~` your data, `?` help), *free text*, and *field operators* (`kind:`, `class:`,
   `market:`, `symbol:`/`asset:`). It is framework-free and unit-tested
   (`grammar.test.ts`) so the vocabulary can grow without touching the UI.
2. **Resolver** (`search/resolver.ts`) — classifies intent, scores the static
   registry + FlexSearch universe, and applies the parsed scope/filters
   (`scopeKinds`, `matchesFilters`) plus a recency multiplier. It returns
   `ResolvedSearch` including the `parsed` query and a `showHelp` flag.

Invariants: search is **fully offline and deterministic** — no LLM on the query
path (only the explicit "Ask AI" escalation row). Navigation is a *destination
bus*: the palette dispatches a `Destination`, the shell subscribes to
`pendingDestination`, so no component reaches into the router. The command-center
vocabulary (scopes/operators) is a user-facing contract — extending it means
editing `grammar.ts` and its tests together.

**What is registered, and how it stays in sync.** The registry
(`search/registry.ts`) is the single source of truth for pages/sub-pages, but
three of the tab bars render from shared modules and the registry *generates*
its sub-page entries from the same arrays (`MEMORY_TABS`/`SKILLS_TABS` in
`lib/app-screens.ts`; `JOURNAL_TABS` in `lib/journal-tabs.ts`, which the Journal
screen also consumes) — ids, labels and aliases cannot drift. The empty-query
"Pages" launcher is derived from `LAUNCHER_ORDER` in the registry (sidebar
order + Bots; the old hand-maintained list had already drifted). The sync is
**enforced by build**: `registry-sync.test.ts` fails if any `AppTab` lacks a
top-level page (retired `chart` and the legacy `graph-studio` alias are
allowlisted), any rendered Memory/Skills/Journal tab lacks a sub-page, or a
sidebar section is missing from the launcher. Screens consume `Destination.subroute`
via `useSubroute` with the `<tab>.` prefix **already stripped** by the shell —
match on the bare slug. Server-side entities
(`/api/search/global`: journal rows, trades, news, reports, and the user's
**saved blueprint pages**, fetched from the Python runtime's `/sdk/pages` with
the operator token, dashboard mirrors excluded, best-effort) are merged once
per session; selecting a saved-page row deep-links to `/?tab=pages&page=<id>`
and the shell pokes the keep-alive Pages screen with a synthetic `popstate`
(the SDK's `openPage` pattern). The JDDS universe wire format is versioned by
one shared `INDEX_VERSION` (`lib/search/index-version.ts`, client + server).

**Intelligence & actions.** Beyond the resolver:
- `resolver.ts` `expandTerms` (a curated synonym/acronym table) turns "gold",
  "us jobs data", "fed rate" into the right symbol at a score discount, so an
  explicit match still wins. Ranking is personalized by entity *visit* history
  (`recencyScore` opt; the palette feeds it `visitRecencyScore` from the store).
- `calc.ts` powers an inline offline calculator + indicative currency conversion;
  `commands.ts` detects parameterized verbs (`alert`, `watch`, `chart`) and
  offers argument autocomplete. Both are pure and unit-tested. The palette's
  static command launcher was removed — only these parameterized verbs render as
  executable rows.
- Parameterized commands **run real side effects**: `alert SYMBOL` creates a
  ±3% `CHANGE_PCT` watch via `lib/alerts/quick-alert.ts` (`POST /api/alerts`)
  and opens the Alerts tab; `watch SYMBOL` calls `addToWatchlist`
  (`lib/dashboard/watchlist-store.ts`) and opens the dashboard; `chart SYMBOL`
  opens the chart. Registry actions declare `requiresArg`, so they are excluded
  from ordinary action matching and can never render as a bare command that
  navigates without acting.

**Registry actions are not result rows.** The resolver does *not* push registry
actions (`Open Portfolio`, `Start new chat`, `Open Settings`, …) into ordinary
results — their destinations duplicate Page entities (already shown under
Navigation) and rendering them produced a redundant "Commands" group while
typing. The top action match is still returned as `primaryAction` so Enter runs
it; action rows appear only under the explicit `>` command scope.

**Cross-tab pivots.** `resolveQuery` returns `related` rows for a resolved
instrument. These are honest about what each destination can do: the news pivot
passes `params.symbol`, which `MarketNews` consumes via its `symbol` prop (it
seeds the server-side `ticker:` query operator — the news API has no other
per-symbol filter); the screener pivot passes `symbol` + `focus:'chart'`, which
pre-fills `MarketOverviewScreen`'s `initialSymbol` → `stockSearch` (the screener
has no single-symbol query — it is a paginated universe partition, so a ticker
narrows the search box rather than defining a new filter dimension).

**Result UX.** The resolver returns `total` (untruncated match count, for
"showing N of M"), a `suggestion` "did you mean" on a zero-hit query (static
registry only), and `related` cross-tab pivots for a resolved instrument. The
palette groups results by entity kind with per-group counts and collapsible
headers, pins entities (persisted in `localStorage`, replayed from a serialized
destination), and copies symbols. Pins, visits, and collapse state live in the
command store — the resolver stays pure.

**Sub-page list + scrolling.** The palette's bottom **Pages** group lists the
**sub-pages only** (the top-level tabs live in the sidebar and still resolve
through search). It is **generated from `getPageEntries().filter(p => p.child)`**
— not hand-listed — so registering a sub-page adds a row automatically. Labels
are qualified by parent tab (`Journal · Analytics`, `Screener · Crypto`) because
several share a name. The group is collapsible (shared `collapsedGroups` key
`'nav'`) and is part of the same flat keyboard list. Navigation keys: ↑/↓ (wrap),
PageUp/PageDown (jump `PAGE_SIZE`), Home/End, Enter to run. A bottom gradient
fades in only while the list can still scroll (`canScrollDown`, tracked via
scroll + ResizeObserver), so overflow is visible without a permanent shadow.

**Sub-route routing (sub-pages).** A `Destination.subroute` is how the palette
targets an internal view (`economics.calendar`, `journal.analytics`,
`discovery.crypto`, `alerts.create`, `settings.general`). The shell's `apply()`
publishes it to `useCommandStore.requestSubroute(tab, subroute, focus)` — a
*channel*, not a single slot, so two screens never steal each other's request.
The owning screen consumes it with `useSubroute(tab, handler)`; both live in
`src/hooks/use-subroute.ts`. Two invariants:
- **Subscribe, don't read once.** Screens are keep-alive (`display:none`), so a
  tab revisit does not remount — a mount-time prop would silently never fire
  again. `useSubroute` subscribes and clears the request once applied.
- **`settings` is a modal, not a tab.** `tab: 'settings'` is routed to the
  Settings dialog (keyed by section), because no tab screen exists for it —
  switching to it would render nothing.

**Accessibility.** The input is an ARIA combobox driving a `role="listbox"`
result list with `aria-activedescendant`; a polite live region announces result
counts. Rows are memoized so selection changes don't re-render the whole list.

**Header price ribbon.** `src/components/layout/price-ribbon.tsx` is the live,
**user-customizable** ticker strip mounted in `top-bar.tsx` beside the search
trigger. The configured symbols live in `src/lib/ribbon/store.ts` (localStorage,
same storage-event pattern as the guest watchlist) and are **routed by family**:

- catalog instruments (commodities `GC=F`, FX `EURUSD=X`; registered in
  `src/lib/market-catalog.ts`, which is also the single source of truth for
  `/api/commodities` and `/api/forex`) come from the shared `market:commodities`
  / `market:forex` topics;
- everything else (indices, stocks, crypto) goes to a dynamic
  `ribbon:quote:<sorted symbols>` topic over `/api/quote?symbols=…`.

A symbol the upstream cannot price is omitted rather than shown as a fake zero.
Clicking a quote dispatches a `{ tab: 'chart', params: { symbol } }` destination
on the command bus; the trailing gear opens a portaled customization popover
(add via the same database autocomplete the pin inputs use, remove, reset).
Presentation: **no background** (bare text, reads as market chrome), and the
marquee (`.price-ribbon` / `.price-ribbon-track` in `globals.css`) renders two
copies and translates `from -50% to 0` for continuous **left→right** motion,
pausing on hover. The ticker is deliberately **EXEMPT from reduce-motion /
animations-off**: motion is its whole purpose, so `.price-ribbon-track` keeps
`infinite` duration against the global `.a11y-reduce-motion` / `.theme-no-anim`
rules and the `prefers-reduced-motion` media query (a higher-specificity,
`!important` override). Without that exemption the global rules froze the ticker.

## The Universal Data Connector Layer

`src/lib/connectors/` is a capability-addressed layer that lets the operator
bring their own REST API into BlackEclipse. It is **additive**: the scraper
chain still runs first, and route-level demo fallbacks are untouched.

```
Scraper chain (existing)  →  Connector tier (new)  →  route-level demo (where it already exists)
```

**Provider ≠ capability.** Consumers ask for `PRICE`; the resolver
(`resolver.ts`) picks a connector. Precedence is deterministic: built-ins
(`kind: 'builtin'`, currently Binance crypto) outrank operator connectors, and
ties break by id. There is **no automatic cross-provider fallback** — if the
primary connector fails, the result is `UNAVAILABLE`. The resolver never
fabricates demo data.

| File | Owns |
|---|---|
| `types.ts` | `DataConnector`, `Capability`, `ConnectorResult`, `Provenance` |
| `registry.ts` | id → connector map (anchored on `globalThis`, like `db.ts`) |
| `resolver.ts` | capability → ordered connectors (built-in first) |
| `runtime.ts` | `runCapability`, `syncCustomConnectors` |
| `custom-rest.ts` | declarative REST connector (GET, `{{param}}` substitution) |
| `draft.ts` | unsaved wizard-draft validation (shared by test + save) |
| `security.ts` | SSRF/IP classification, pinned DNS, capped body, safe GET |
| `secrets.ts` | AES-GCM credential encryption + key resolution |
| `mapping.ts` | `recordPath`, field coercion, `recordKey` dedup |
| `refresh.ts` | multi-value pull expansion + `PullSummary` |
| `refresh-scheduler.ts` | the `dataset_refresh` cron |
| `health.ts` | application-level health state (no Prisma enum) |
| `store.ts` | JDDS persistence (connectors, datasets, records) |
| `catalog.ts` | the browsable provider catalog (grouped, honest status) |
| `builtins.ts` | built-in connector registration (avoids a barrel cycle) |
| `providers/crypto.ts` | Phase-2 proof: the migrated crypto fetcher |

**Security.** Every outbound connector request goes through `safeGet`:
https-only, WHATWG-canonicalized, DNS resolved and validated **then pinned at
connect** (rebinding-safe; TLS/SNI preserved), `redirect: manual`, a streamed
5 MB cap, and a ≤10s timeout. Never `res.text()` on an untrusted response.

**Secrets.** Credentials are AES-256-GCM encrypted before they touch JDDS.
Single-node installs auto-generate `.connector-key` (gitignored); hosted
deployments must set `CONNECTOR_SECRET_KEY` because an instance-local file would
differ per instance. The browser sees only `hasSecret: boolean` after saving.

**Data integrity.** Custom records live in `CustomDataset` /
`CustomDatasetField` / `CustomDatasetRecord`. `(datasetId, recordKey)` is
unique, so re-imports upsert. `Asset`/`Price`/`Candles` are **never** written by
this layer — see ADR-010.

**Honest import accounting.** `ingestRecords` returns
`{ written, extracted, rejected, rejectedRequired, rejectedIdentity, deduped }`
so the UI reports "Imported 8 of 10 records · 2 rejected" instead of a blanket
success. Every extracted row is accounted for as written, deduped, or rejected.
A connector that needs parameters it did not receive returns **400**
(`invalid_config`/`missing_param`), not an opaque 502.

**Multi-value pulls.** A parameter value may be a comma-separated list
(`BTC,ETH,SOL`); `refresh.ts` expands it into **one request per value**, capped
at `MAX_VALUES_PER_PULL = 25` with the overflow count reported, never a silent
truncation, and requests spaced 250ms apart. `pullDataset` returns a
`PullSummary`; partial failure returns HTTP 207.

**Scheduled refresh.** `refresh-scheduler.ts` runs a `dataset_refresh` cron
(per-dataset due check, 90s boot grace). It reuses the scraper scheduler's
discipline: a mutex, sequential runs, and it **skips a tick while `syncBusy()`
holds the write path** — better-sqlite3 is synchronous. A failed dataset still
advances `lastRefreshAt` so it cannot retry every tick.

**Provider catalog.** `catalog.ts` is a map of what BlackEclipse can talk to,
grouped by purpose (AI, economics, market, crypto, custom). Economics entries are
**derived from `ECON_SOURCES`**, so the catalog cannot drift from the fetch
layer. Status is computed from the real environment (`builtin` / `connected` /
`not_configured` / `available`) and buttons never claim to have saved a key:
env-gated providers route to the screen that actually sets it.

**Providers live where they are configured (purpose-first IA).** Settings is
organised around *what the user is configuring*, never backend concepts. There
is no "API Keys", "Integrations", "Data Sources" or "AI Agent" menu overlap:

- **AI / LLM** — every LLM concern (preset picker, provider keys, base URL,
  model, active provider, **Test Connection**, status). Strictly separated from
  financial/data APIs. A provider here is ONE row in `LLM_PRESETS`
  (`src/lib/agent/presets.ts`) **and** ONE `ProviderSpec` with the *same id* in
  `PROVIDER_CATALOG` (`python/harness/models.py`): the preset id is handed to the
  runtime as the provider id, and the runtime raises "Unknown model provider" for
  an id it does not know. The key lives in the vault under the preset's `envVar`
  (`src/lib/provider-keys.ts`); a custom endpoint uses `CUSTOM_LLM_API_KEY`, and
  only local base URLs may be saved without a key.
- **Investor Eye** — every financial/data provider grouped by the purpose it
  serves (market, fundamentals, economics, news, earnings, SEC/company, crypto)
  plus a feature→provider map and the Data Sources wizard for custom APIs.
- **Market Data** — supported markets/exchanges and the price-source fallback
  chain.
- **News** — RSS feeds, news API providers, and the high-alert push policy
  (enabled/min score/lookback/categories/tickers/quiet hours).
- **System** — technical/internal only: JDDS/database status, health, all-provider
  technical view.

`src/lib/providers/directory.ts` is the single source of "why does this provider
exist, who uses it, and where does it live" (purpose, `usedBy`, free/paid tier,
and a `home`). `home` is what guarantees **every provider appears in exactly one
Settings section** — it is the anti-duplication invariant, filtered by
`providerHome()`. `provider-registry-panel.tsx` renders one
`Provider → Purpose → Features → Status → API key → Test` list per section, with
inline (never-echoed) key management, so a section does not also render a second
"API Keys" list of the same providers. Investor Eye additionally shows its own
keyless event sources (`InvestorEyeSources`) and a feature→provider map whose
chips deep-link to each provider's home section. Provider buckets currently:
AI/LLM (14), Economics (44), News (Finnhub), Market (15), Investor Eye (GDELT/
USGS/FIRMS, keyless).

**Persistence is instant and uniform.** There is no Save button. Server-backed
controls (`preferences`, `llm`, `newsFeeds`) POST to `/api/settings` on change —
debounced 500–600ms for prefs/LLM, immediate for feed toggles. A status strip
reports "Saving…", the real result, or the real error. The rail groups sections
under Personalize / Intelligence / System and carries a keyword search (each
section declares `terms`). Long registries filter, sort configured-first, and
collapse past 8 rows (economics is instead sub-grouped by kind:
Central banks / Multilateral / Official statistics / Market data). A status
header shows `N ready · M need a key`, and rail sections with unresolved
providers show an amber dot. Keyboard: `/` focuses the rail search, ↑/↓ move
between sections, Esc blurs. Deep links may carry a block anchor
(`settings.market#priority`) which scrolls to the matching `data-anchor`.

**One user market.** `src/lib/user-market.ts` holds a single persisted
"which market is the user looking at" value (`eclipse_user_market`), shared by
the Screener and Market Bubbles. Resolution: explicit hint → stored choice →
Settings `defaultMarket` → browser locale → `US`. The Screener writes it on
market select; Bubbles opens on it instead of a hard-coded `CRYPTO`.

**One LLM key.** The AI section's key is edited through the provider-key vault
(keyed by the selected preset's env var), not a separate `llm.apiKey` field.
`getLLMConfig()` resolves the effective key as raw config → env var → vault
(`storedProviderKey`) for the matching preset; `setLLMConfig` merges over the
RAW config so a vault-owned key is never copied into the plaintext row. "Used
by" labels in the registry are clickable and navigate to the consuming page
(`consumingPage` in `lib/providers/directory.ts`).

**AI presets are one source of truth.** `src/lib/agent/presets.ts` declares every
OpenAI-compatible provider (base URL + default model). The AI Agent picker and
the catalog's AI entries are both derived from it, so adding a provider is one
edit. Every preset speaks the protocol `OpenAICompatibleProvider` already sends,
so "Use this" is a real choice. Local servers (Ollama, LM Studio) are marked
`keyless` and `setLLMConfig` permits them without an API key (`isLocalBaseUrl`).

**Key status has one list.** `CATALOG_ENV_VARS` is the single set of env vars the
UI reports; `/api/providers` and `/api/settings` both read it. Status is computed
from the real environment and buttons never claim to have saved a key.

**Server-side resolution.** DataHub stays client-side. It may return a
provenance envelope (`{ data, source }`) from a topic, and `capabilityTopic()`
wraps `GET /api/data/[capability]`; the route decides the connector and reports
provenance so widgets never invent a tag.

**New routes:** `GET/POST/PATCH/DELETE /api/data-sources`,
`POST /api/data-sources/test` (outbound → `guardExpensiveMutation`),
`GET/POST/PUT/DELETE /api/datasets`, `GET /api/data/[capability]`,
`GET /api/providers`, `POST /api/agent/test` (real LLM endpoint check).
**UI:** Settings → **Investor Eye** (`data-sources-panel.tsx` embedded) — the
provider registry, the connector list, the add wizard (Configure → Authenticate →
Test → Map & define dataset), and a **Datasets** browser with **Pull**,
per-dataset parameter inputs, records with provenance, auto-refresh, and delete.

**Agent access.** Read-only `list_datasets` / `describe_dataset` /
`query_dataset` tools in `src/lib/agent/dataset-tools.ts`, spread into `TOOLS`;
every result states provenance.

**Deferred:** webhooks, POST/PUT bodies, pagination beyond value lists, OAuth,
`~` dataset search, AI-generated connectors, multi-user credentials.

## Fundamentals (the investor-analysis surface)

Fundamentals is the **filings-backed analysis** rendered inside the Market
Screener's stock detail popup — not a standalone sidebar tab. A stock row opens
the popup, whose **Fundamentals** tab shows the provider snapshot *and* the live
quote metrics on one page. It is a **separate provider tier** from the connector
layer: rather than picking one connector for a capability, it MERGES
complementary sources into one normalized document.

```
Provider → Normalization → JDDS → Fundamentals UI (Market Screener popup tab)
```

**The UI depends only on the Eclipse schema.** `src/lib/fundamentals/types.ts`
defines `FundamentalsSnapshot` (company, periods, metrics, growth, earnings,
ownership, insights, timeline). The panel in
`src/components/screens/fundamentals/` talks only to `/api/fundamentals` — it
never imports a Yahoo/SEC/FMP module.

| File | Owns |
|---|---|
| `fundamentals/types.ts` | The Eclipse schema (`FundamentalPeriod`, `FundamentalMetric`, `FundamentalsSnapshot`, `FundamentalsProvider`) |
| `fundamentals/normalize.ts` | Provider rows → one period shape; derives per-period ratios |
| `fundamentals/compute.ts` | Metric assembly, CAGRs, valuation, **data-driven insights/risks** |
| `fundamentals/format.ts` | Display formatting (percent metrics are FRACTIONS everywhere) |
| `fundamentals/providers/` | `sec-edgar`, `financialmodelingprep`, `yahoo` + precedence registry |
| `fundamentals/index.ts` | `getFundamentals(symbol, market)` — merge, cache, persist |
| `components/screens/fundamentals/panel.tsx` | The shared presentational sections (`FundamentalsBody`, `TickerHeader`, metric tiles, charts, states) |
| `components/screens/fundamentals/use-fundamentals.ts` | The client data hook |
| `scripts/yfinance_fundamentals.py` | The Yahoo statements/profile/estimates bridge |

**Precedence and merging.** `PROVIDER_ORDER` is
`sec-edgar → financialmodelingprep → yahoo`. The **first provider that returns
statement periods is the primary source**; quote, valuation multiples, earnings
and ownership are filled from whichever provider has them. A missing source is
never a fatal error — it fills what it can and marks the rest `UNAVAILABLE`.

- **SEC EDGAR** — preferred for US issuers, keyless. Ticker → CIK via the cached
  `company_tickers.json` map, then `companyfacts.json`. Only `10-K`/`20-F`/`40-F`
  annual values are read (latest filing wins per end date); **tag families are
  unioned** so a preferred tag that stops being reported (e.g. NVIDIA's revenue
  tag after FY2022) cannot blank a newer period, and sub-annual windows that
  share a fiscal end date are ignored. **EBITDA is derived** (operating income +
  D&A) and explicitly labelled, because the SEC does not tag it. Non-US markets
  skip this provider (`supports()` gates it).
- **Yahoo Finance** (`yfinance` bridge) — the universal fallback and the only
  source for non-US markets when no vendor key exists. Returns up to 5 fiscal
  years of real statements plus profile/ratios.
- **Financial Modeling Prep** — optional; enabled only when
  `FINANCIALMODELINGPREP_API_KEY` is set in Settings › API Keys or env. This is
  the reference secondary provider; Alpha Vantage can be added as another
  `FundamentalsProvider` with one file and one registry line.

**Caching / JDDS.** `getFundamentals` fronts the `FundamentalsCache` table
(`@@unique([symbol, market])`, `payload` = JSON snapshot) with an in-memory map.
A fresh entry (< 24h) is served directly; a stale one is served immediately while
a background refresh runs (stale-while-revalidate); beyond 7 days the request
blocks so the UI cannot present years-old data as current. Concurrent requests
for the same symbol share one in-flight fetch. Provider failures return the stale
copy rather than blanking the screen.

**Honesty rules.** Percent metrics are stored as fractions; a metric with no
inputs is `null` and renders `UNAVAILABLE` (never 0). Every metric carries
`value`, `period`, `yoy`, `source` (`sec-edgar` / `financialmodelingprep` /
`yahoo` / `derived`) and `updatedAt`. Insights are synthesized only from actual
statement inputs and list the metric keys they are grounded in. The valuation
history chart reconstructs multiples from the CURRENT price and each year's
reported EPS/revenue — it says so in the caption.

**Route:** `GET /api/fundamentals?symbol=…&market=…[&refresh=1]` (rate-limited).
**UI:** the single-page company detail overlay (`tradingview-chart.tsx`)
renders the shared `panel.tsx` sections — Overview (insight strip, metric
grid, revenue/EPS/FCF/margin/debt/valuation charts), Statements (period tabs +
statement table + growth), Valuation (multiples, earnings, ownership), Timeline
(filings + profile) — followed by the live price-history/performance/quote
sections of the old "All Metrics" tab. The market id is derived with
`countryToMarketId()` from `src/lib/markets.ts` because JDDS `Asset.country` is
inconsistent ("United States" vs "US").

## Stock-detail market intelligence (cross-source charts)

The single-page company detail overlay renders a `Market intelligence` section
between the price chart and the AI brief. It adds five sourced charts: 1Y price
trend, drawdown from the running high, benchmark-relative total return,
historical P/E at fiscal year-ends, and earnings surprise. This is the first
slice of the wider "less JDdS-dependent" stock research surface; web/AI-extracted
facts stay separate from precise historical series.

```
GET /api/stocks/[symbol]/intelligence?market=...
  -> resolve market from JDdS Asset rows   (screener rows can omit country)
  -> getFundamentals(symbol, market)       (merged provider snapshot)
  -> daily closes: Yahoo, then Stooq (US)  (numeric time series only)
  -> derived chart specs + provenance      (no fabricated points)
```

`price-trend` and `drawdown` depend only on price history, so they are the first
cards to become ready. Every requested chart is still rendered as a card when
unavailable, with the exact reason — the panel never collapses to a blank box or
a single generic warning.

### Source verification (web-backed, asynchronous)

The overlay also mounts `Source verification`, backed by:

```
GET /api/stocks/[symbol]/verification?market=...&name=...&price=...
  -> existing web_search capability (same provider chain as research/Python)
  -> official-host checks + search pointers
  -> per-check evidence + VERIFIED / CORROBORATED / UNVERIFIED / UNAVAILABLE
```

Checks cover company identity, listing/exchange, filings, and a quote
cross-check. An official host (regulator/exchange/filing archive) is required
for `VERIFIED`; two independent results can only `CORROBORATE`; a snippet can
corroborate a quote but never makes it official. Results are cached in memory
for 30 minutes, so page opens do not run four live searches every time. The
route reuses the existing web-search transport — no second search stack.

| File | Owns |
|---|---|
| `src/lib/stock-intelligence/types.ts` | The response contract (`StockChart`, `StockProvenance`, `StockSourceRef`) |
| `src/lib/stock-intelligence/charts.ts` | Pure derived series: price trend, drawdown, relative performance, fiscal-year-end P/E, earnings surprise |
| `src/lib/stock-intelligence/verification.ts` | Pure official-host/identity/listing/filing/quote verification rules |
| `src/lib/stock-intelligence/use-stock-intelligence.ts` | Client hook for the read-only intelligence route |
| `src/lib/stock-intelligence/use-stock-verification.ts` | Client hook for the verification route |
| `src/app/api/stocks/[symbol]/intelligence/route.ts` | Market resolution, provider fallback, 5-minute cache, provenance assembly |
| `src/lib/scraper/stooq-chart.ts` | Keyless US daily-close fallback when the primary chart provider returns nothing |
| `src/app/api/stocks/[symbol]/verification/route.ts` | Existing web-search fan-out, evidence assembly, 30-minute cache |
| `src/components/screens/stock-detail/charts/` | Chart cards and the `SourceBadge` |
| `src/components/screens/stock-detail/verification-panel.tsx` | Web-backed source-verification cards |
| `src/components/screens/tradingview-chart.tsx` | Mounts both sections in the overlay |

**Honesty rules.** Every chart carries a `state` (`READY` / `UNAVAILABLE`) and a
`provenance` block naming its sources and a state such as `HISTORICAL` or
`DERIVED`. Missing benchmark history, unprofitable fiscal years, or absent
actual-vs-estimate EPS produce an explicit reason, never a synthetic point. The
relative-return chart uses only dates present in both series; the P/E chart uses
the actual close nearest each fiscal year-end; earnings surprise is derived from
provider-reported actual and estimate EPS, not an Eclipse forecast. Price-history
sources fall back from Yahoo to Stooq (US) and are labelled with the provider
that actually returned the series. Web search and AI extraction are not used for
these historical numeric series; verification is a separate, explicit layer.

## Investor Eye (the world-state surface)

`investor-eye` is a sidebar tab (keep-alive, `next/dynamic`-split like every
other screen) that renders a large interactive world and the intelligence
underneath it. The long-term model is:

```
world events → geopolitical conditions → economic transmission → markets
→ companies → securities → portfolios
```

**It is a new, additive tier — it does not replace or duplicate the scraper,
news, portfolio, or market systems.** The full audit, Phase 1/2 scope, and the
list of deliberate non-additions live in
[`investor-eye-architecture.md`](./investor-eye-architecture.md). ADR-011 records
the decision.

### Layers (the stack)

```
SOURCE (GDELT, USGS, NASA FIRMS, /api/indices, /api/economics + FRED CSV, JDDS Asset/Price)
  ↓ src/lib/investor-eye/sources/*     fetch via the shared connectors `safeGet`
RAW
  ↓ classify.ts / cluster.ts / dedup.ts / confidence.ts   (pure, unit-tested)
normalized + clustered events
  ↓ state.ts        assemble, cache (SWR), persist, provenance, SourceRun rows
JDDS (Country, GeopoliticalEvent, EventEntity, SourceRun) + in-memory cache
  ↓ /api/investor-eye/*  → DataHub topics (provenance envelope) → Zustand store
  ↓ layers.ts (pure builders) → MapLayer[] → <WorldMap> (ECharts)
```

Every source also has a `source-catalog.ts` governance entry (licence,
commercial-use, attribution, rate limit, freshness), and a test enforces that a
registered adapter cannot exist without one — see *Data governance* below.

| File | Owns |
|---|---|
| `types.ts` | `NormalizedEvent`, `EventSource`, `MapLayer`, `InvestorEyeState`, provenance |
| `countries.ts` | canonical country gazetteer (ISO codes, region, centroid, aliases, market link) |
| `admin-boundaries.ts` | point-in-polygon country lookup against the shipped Natural Earth GeoJSON (exact, cached) |
| `source-catalog.ts` | source governance: licence, commercial use, attribution, rate limit, freshness |
| `src/lib/provider-keys.ts` | the encrypted vault + registry for every operator credential (`/api/provider-keys`, settings → API Keys) |
| `classify.ts` | event type / severity / channels / commodities / sectors (pure) |
| `cluster.ts` | dedup clustering + internal data-quality confidence (pure) |
| `dedup.ts` | stable `dedupKey` for idempotent upserts |
| `geo.ts` | grid aggregation so dense events never render raw |
| `sources/` | `registry.ts` + `gdelt.ts` + `usgs.ts` + `news.ts` + `firms.ts` (`EventSource` adapters) |
| `state.ts` | `assembleState`, market observations, country summaries, `SourceRun` bookkeeping |
| `phase2.ts` | real economic metrics (FRED CSV), companies (JDDS `Asset`), price history |
| `analytics.ts` | risk decomposition, exposure, transmissions, analogues, NL `planQuery` |
| `graph.ts` | pure edge → nodes/links aggregation (per-kind caps, honest truncation) |
| `graph-data.ts` | derives + persists `Relationship` / `EventEntity` / `Country`; `queryEdges` |
| `cameo.ts` | CAMEO root code → event type, Goldstein → severity, **FIPS 10-4 → ISO2** |
| `relevance.ts` | investor-relevance score (0..1) + reasons; the feed's display threshold |
| `sources/news.ts` | turns existing `NewsItem` rows into events (no new feed/credentials) |
| `sources/firms.ts` | NASA FIRMS keyless fire CSV → grid-clustered detections (pixels, not fires) |
| `history.ts` | candle backfill + coverage, reusing the alerts yfinance path |
| `layers.ts` | pure `MapLayer` builders (events, markets, conflict, shipping, trade, energy, commodities, weather, fires, economy, companies, portfolio) |
| `map-echarts.ts` | the ONLY ECharts-aware module; layer → option + event bridge |
| `app-geo.ts` | loads/registers `public/geo/world.geo.json`; **draws the glowing globe texture** (gradient ocean, dark land, coastline bloom, city stipple) |
| `starfield.tsx` | the canvas starfield + nebula backdrop behind the map (deterministic, drawn once) |
| `cache.ts` / `scheduler.ts` | server SWR cache + persisted last-known; in-process ingestion cron |
| `store.ts` / `topics.ts` | Zustand view state; DataHub topics |

### Routes

`GET /api/investor-eye/state` · `/events` · `/events/:id` · `/countries/:id` ·
`/markets` · `/layers/:layer` (+ guarded `POST` rebuild) · `/economy` ·
`/companies` · `POST /portfolio` · `/risk` · `/analogues` · `/query` ·
`/graph` · `/history` (+ guarded `POST` backfill) · `/health`.

**Feed sources.** GDELT is read from its **bulk 15-minute Event export**
(`data.gdeltproject.org`) — fast, complete and not rate-limited — decoded with
Node's built-in `zlib` via `safeGet({ binary: true })`. The DOC API is only a
best-effort fallback: it was measured at >13s/request (the transport caps at
10s) and 429s on bursts. `sources/news.ts` additionally converts the
already-ingested `NewsItem` rows into events, so macro/market coverage costs no
extra feed. `sources/firms.ts` reads NASA FIRMS' keyless global archive CSV
(the Area API needs a `MAP_KEY`, which would break zero-config) and **aggregates
at ingestion**: ~25,600 single-pixel detections become ≤400 grid clusters, each
reporting its real detection count and summed FRP. GDELT's FIPS country codes
are mapped to ISO2 in `cameo.ts` — they conflict with ISO for several important
countries.

**Data governance.** `source-catalog.ts` holds one entry per upstream source
(licence, commercial-use, attribution, rate limit, freshness). It is deliberately
separate from the adapters: a licence question should not require constructing a
fetcher. A test asserts every registered adapter has an entry (and vice versa),
so a new source cannot ship without declaring its licence.
`/api/investor-eye/health` joins the catalogue onto the `SourceRun` accounting
and returns the required attribution lines; `SourceHealthPanel` renders them and
flags a runnable-but-undeclared source as a governance gap. "Public API" is never
treated as "free for commercial use" — each entry records what the provider
states, with a link.

**Domain layers match type OR channel.** `classify.ts` assigns each event one
type but many channels, so a `conflict` event can still carry the Shipping
channel. Filtering on type alone drew a fraction of the real signal (measured:
28 shipping-channel events vs 11 shipping-typed in a 24h window). `buildDomainLayer`
therefore accepts either, and `shipping`/`commodities` are separate layers.

**Exact country attribution.** `admin-boundaries.ts` does a real
point-in-polygon test against the *same* Natural Earth GeoJSON the map ships
(`public/geo/world.geo.json`), cached in-module. This is what makes a precise
coordinate resolvable at all: the gazetteer's nearest-*centroid* match put only
15% of live fire clusters in a country, point-in-polygon puts 97%. It also draws
on the boundary asset's own ISO2/name when the curated gazetteer has no entry
(the asset covers 173 features; the gazetteer fewer), so a proven hit is never
discarded.

**Optional work never blocks the map.** The candle-history backfill (~170s
across 30 markets) is fire-and-forget inside `assembleState`; awaiting it
blocked the first world-state response and made the map show "0 events".

**Operator credentials (`provider-keys.ts`).** Every key the app can consume is
declared once in `PROVIDER_KEYS` and surfaced in Settings → API Keys. Values are
AES-256-GCM encrypted in `SystemConfig` (the existing `connectors/secrets` path)
and never returned to the browser — the API reports only `stored`, `fromEnv` and
a masked hint. `applyStoredKeysToEnv()` copies vault keys into `process.env` at
boot, but a REAL env var always wins and is never overwritten; removal only
clears variables this process itself populated. That keeps the existing env-based
consumers working with no per-provider refactor while preserving deployment
authority. "Test" performs a real request and reports the real status code.
The Python agent runtime inherits this env when it is spawned, and
`python-runtime.ts` calls `applyStoredKeysToEnv()` again immediately before the
spawn: the child is often started from instrumentation *before* the boot copy
runs, and a keyless runtime fails every agent/vision call with "provider is not
configured (missing API key)" until it happens to restart.

**Do not run `prisma generate` without restarting the dev server.** A client
generated before a schema change silently fails every write to that table;
`persistEvents` catches per-row errors by design, so the failure is invisible —
the map shows events that exist nowhere in the database.

**Shared, per-window cache.** The assembled-state cache and its in-flight
assembly guard live in `cache.ts` **anchored on `globalThis` and keyed by time
window**. Next evaluates each route handler in its own module instance, so a
module-level `let` gave every endpoint a separate cache — `/state`, `/risk`,
`/markets` and `/layers` each cold-ran the full source ingestion. One shared
cache/guard means a route fan-out triggers exactly one ingestion per window.
Do not "simplify" this back to module-level state.

**Agent access (the AI Analyst).** `src/lib/agent/investor-eye-tools.ts` adds
seven read-only tools (`get_world_events`, `get_country_intelligence`,
`get_world_markets`, `get_event_detail`, `get_relationships`,
`get_portfolio_exposure`, `get_risk_state`) spread into `TOOLS`, plus a `world`
research agent in `research.ts`. Every result states provenance and labels
`INFERRED` links, so the model cites structured data instead of inventing it.

### Honesty and failure rules (non-negotiable)

- A source that fails is `UNAVAILABLE` — never a fabricated zero. A healthy
  source observing zero events may honestly report `0`.
- Provenance is computed server-side; the client never invents a tag.
- `confidence` is internal data quality, never presented as certainty.
- Observed facts are separated from **potential exposure** and **inferred**
  relationships in every tooltip/panel (`OBSERVED | HISTORICAL | INFERRED |
  AI-INTERPRETED`).
- Partial failure does not break the page: the map renders from whatever the
  remaining sources produced, and the status chip reports each source's state.

## Earnings calendar (the Command Center)

`src/lib/earnings.ts` (server-only) + `GET /api/earnings` serve upcoming company
earnings to the `earnings-calendar` widget (topic `earnings:calendar`).
**Alpha Vantage `EARNINGS_CALENDAR?horizon=3month`** is the PRIMARY source, using
the `ALPHAVANTAGE_API_KEY` from the provider-key vault (read with
`resolveProviderKey` server-side; the key is **never** sent to the browser). A
zero-config install has no key, so the module falls back to **Nasdaq's public
earnings calendar** (`api.nasdaq.com/api/calendar/earnings`, no key) for the next
a bounded rolling window (last 7 → +35 days, weekends skipped, ~6 requests pooled
at a time) — so the widget works out of the box and labels the real source
(`Nasdaq` / `Alpha Vantage`). A present-but-rejected key also falls through to
Nasdaq, and the Alpha Vantage reason is preserved in the error text. Parsers
accept Alpha Vantage's header aliases (`estimate`/`estimatedEPS`) and session
spellings (`pre-market`/`post-market`, `time-pre-market`/`time-after-hours` →
BMO/AMC), plus parenthesised negative Nasdaq EPS (`($1.08)`). Caching is 6h with
a shared in-flight promise, and a stale cached result is served on failure. Only
when BOTH sources fail does it return `source: 'unavailable'` with a human
`reason`; rows are never fabricated. The widget renders a **month grid calendar**
(cells group rows by ISO day, with ticker chips; a selected-day detail list),
and clicking a company dispatches `{ tab: 'chart', params: { symbol } }` on the
command store.

## Live Intelligence (the Command Center media widget)

"Live Intelligence" is a Command Center dashboard widget that streams financial,
market, business and geopolitical broadcasters inside the widget grid. It is a
normal `WidgetDefinition` — it moves, resizes, configures and persists through
the shared workspace kernel like every other widget, and introduces no second
framework.

- **Channel registry** — `src/lib/dashboard/live-channels.ts` is the single
  source of truth. Every channel is a broadcaster's OWN official feed, and every
  channel id AND `@handle` was resolved from that network's page. The catalog is
  finance/market-scoped by design (entertainment, sport and lifestyle are
  excluded). `LiveCategory` groups the picker; `LiveStreamType` is
  `youtube | video | embed`. `lastKnownStatus` records only what a real playback
  test observed; the runtime resolver is authoritative.
- **Live resolver (server-side, cached)** — `src/lib/dashboard/live-resolver.ts`
  + `GET /api/live-streams` resolve each channel's CURRENT live video id and
  status, cached 3 minutes with a shared in-flight guard. **This exists because
  embedding `live_stream?channel=<id|handle>` is unreliable** — YouTube renders
  "This video is unavailable" inside the frame for many channels that *are* live,
  which is the original "UNAVAILABLE" bug. Two findings are load-bearing:
  1. `/embed/<resolved-video-id>` plays where `live_stream?channel=` does not.
  2. YouTube's `/channel/<UC>/live` page silently fails to expose a canonical
     live video for many channels whose `/@handle/live` resolves correctly — so
     resolution tries the handle first.
  Live ids rotate, so none are hardcoded. Status is classified from YouTube's own
  page metadata (`canonical watch?v=` + `"isLive":true` +
  `playableInEmbed`) — never fabricated.
- **Selection store** — `src/lib/dashboard/live-intelligence-store.ts` holds the
  app-level configuration (enabled channels, default channel, validated custom
  sources) in localStorage with the same storage-event pattern as the price
  ribbon and guest watchlist. The channel a *specific* card is watching is
  per-instance state, persisted by the widget under its own placement id — two
  cards can watch different feeds.
- **Player** — `src/lib/dashboard/live-intelligence-widget.tsx` embeds the
  provider's official player, preferring the resolved video id
  (`/embed/<id>`), falling back to `live_stream?channel=<handle>`, or an
  allowlisted embed. States: checking, live, offline (a *resolved* verdict — the
  provider has no live broadcast right now), stream error (retry / change
  channel / open at source), no channels configured, and paused/off-screen.
  Custom sources are validated by `parseCustomSource` + a host allowlist
  (`safeEmbedUrl`) so a user URL can never load an arbitrary origin.
  The status chip shows `LIVE` only when the resolver confirmed a live,
  embeddable stream, `OFFLINE` for a resolved no-broadcast, `CHECKING` otherwise.
- **Teardown** — the widget owns its own lifecycle (ADR-015). Because the shell
  keeps visited tabs MOUNTED and hidden with `display:none`, and
  `WidgetDefinition.lazy` is never read, an `IntersectionObserver` plus a
  `visibilitychange` listener unmount the iframe whenever the card is hidden,
  scrolled off, or the browser tab is backgrounded, and re-mount it on return.
- **No proxy.** The app never proxies, restreams, scrapes or rebroadcasts
  anything, never fabricates a stream URL, and makes no unverifiable claim about
  a stream's live state. The resolver reads a public page's metadata only.
- **Extension point** — `LiveIntelligenceEvent` is declared (and unused) as the
  shape a future transcript → event-detection pipeline would emit for Investor
  Eye. No producer exists yet, and none may fabricate its fields.

## Design system

All surfaces use the shared frosted-glass classes from
`src/app/globals.css`: `.glass-card`, `.glass-card-strong`,
`.glass-card-subtle`, `.glass-drawer`, `.glass-button`. Tab bars use `TabBar`
(`src/components/ui/tab-bar.tsx`), which has two container variants: `glass`
(default, heavy frosted strip) and `subtle` (the restrained `.tab-bar-subtle`
control-strip tier for dense/toolbar-adjacent rows, e.g. the Command Center
market tabs). There are hard rules about `backdrop-filter`, tint opacity, and
backdrop roots — see the
[`black-eclipse-ui` skill](../../.opencode/skills/black-eclipse-ui/SKILL.md).
Do not introduce raw `backdrop-blur-*` or flat fills for cards.

**Native browser dialogs are banned.** `window.confirm` / `window.alert` /
`window.prompt` render as OS chrome pinned to the top of the desktop WebView.
Use the queued in-app primitive instead: `confirmDialog`, `alertDialog` and
`promptDialog` from `src/lib/ui/confirm-dialog.ts` (deliberately UI-free, so
`src/lib` modules may call it), rendered by the single `ConfirmDialogHost`
(`src/components/ui/confirm-dialog-host.tsx`) mounted in `src/app/layout.tsx`.
It opens a centred frosted-glass dialog; pass `tone: 'danger'` for destructive
confirmations.

**UI sounds.** Optional terminal-style audio feedback lives in
`src/lib/sounds/`. `sound-manager.ts` is a defensive singleton over the **Web
Audio API** (no Howler dependency): one `AudioContext` is created on mount and
kept for the life of the page. `SoundProvider` (mounted once in
`src/app/layout.tsx`) creates the context on mount (so it is armed before the
first gesture) and decodes the cue pack only once the browser is idle
(`sound.warmIdle()` — warming fetches + decodes ~340KB of WAV, so it is kept off
the first paint), and *arms* playback
on **every** pointer/key interaction and on every visibility/focus restore —
not just the first gesture. `play()` resumes a suspended context itself and
waits for the resume before starting the decoded buffer, so an async cue (a
finished chat reply, a fired alert) is never silently dropped; a context that
later drifts to `suspended` is resumed via `onstatechange`. This persistent,
re-armed context is what fixes the old intermittent-silence behaviour. Every
failure path is a silent no-op. The cue set is declared once in `config.ts`
(`click`, `tick`, `toggle`, `success`, `error`, `notification`, `message`,
`taskdone`) with per-cue gain, throttle, an `essential` flag and the optional
`notification` / `gainFloor` flags. Two classes are deliberate:

* **Interaction cues** (`click`, `tick`, `toggle`, `success`, `error`) are the
  optional click/tick "haptics". They are **strictly opt-in** — the master
  switch defaults OFF, and a stored preference without an explicit
  `enabled: true` cannot turn them back on.
* **Event notifications** (`message` = AI result, `notification` = alert /
  approval, `taskdone` = background task or automation finished) announce
  something happening outside the current view. They play independently of the
  interaction master switch and essential-only mode; each keeps a `gainFloor`
  (0.5) so the deliberately subtle default volume cannot make it inaudible.
  Per-cue mute is the explicit opt-out: `message` (AI result) and
  `notification` (alert) are **on by default** so an AI result/alert is audible
  without enabling the click/tick layer, while the loud `taskdone` alarm is
  opt-in. The AI-result cue is deliberately **pack-independent**: it resolves
  from `public/sounds/message/<variant>.wav` (`chime` default, plus `marimba`,
  `glass`, `blip`) and is selected in Settings › Sound › AI result voice. A
  pack can have a crisp interaction tick while the reply stays a calm chime;
  `scripts/generate-message-cues.py` regenerates the four assets.

Assets live in every pack (`public/sounds/<pack>/taskdone.wav`). One delegated
listener plays `toggle` for `role="switch"` / checkbox / radio and `click` for
buttons, links and menu options. Anything can opt into a precise cue with
`data-sound="tick|click|…"`; `sound.*()` or the `eclipse:sound` CustomEvent
covers async outcomes — `sound.message()` fires when an assistant reply
finishes in either chat surface. Deliberately excluded: hover, typing,
scrolling, chart movement and constantly updating market data.

Sound is **fully customizable** through a dedicated Settings › Sound tab
(`src/components/screens/sound-settings.tsx`), not a nested card. It exposes
master on/off + volume, a **mode** (`all` vs `essential`, where essential mutes
confirmations), a **voice-pack** picker, and per-cue volume / mute / custom
upload. The notification cues (`message` / AI result, `notification`,
`taskdone`) are split into their own **Notifications & alarms** card so the
decision is explicit, and the same toggles are mirrored in Settings › Alerts
next to the notification channels — both write the same `useSoundStore` per-cue
prefs. Voices are bundled as local WAV packs under `public/sounds/<pack>/` —
`eclipse` (warm harmonic-synthesis ticks, the refreshed default), `mechanical`,
`crisp`, `typewriter`, `soft`, `marimba`, `glass`, `digital`, `retro` — each
providing all eight cues. The manager resolves a cue's source per play: a
user-uploaded `custom` data URL wins, otherwise the active pack asset; the
decoded buffer is cached by source URL, so a pack switch only re-fetches the
sources that actually changed. All of it persists through
the client zustand `useSoundStore` (`be_sounds_v4`; `_v1`→`_v2` when UI audio
first became opt-in, `_v2`→`_v3` when the cue set gained `message` and the
engine moved to Web Audio, `_v3`→`_v4` when interaction audio became strictly
opt-in and notification cues were split from the master switch). Interaction
audio defaults are OFF at 12% volume (deliberately subtle), mode `all`, pack
`eclipse`; the AI-result and alert cues are on until individually muted, and
the task-done alarm starts muted.

**Read aloud (TTS).** Spoken replies are a separate subsystem from UI cues:
`src/lib/tts/` wraps the browser's **Web Speech API**
(`speechSynthesis`), so there is no key, no dependency and no server round-trip —
reply text never leaves the device. `speech.ts` is the singleton: it cancels any
prior utterance before speaking, chunks prose to ~220 characters (Chrome
truncates long utterances), resolves the system voice by explicit `voiceURI` or
the browser locale, and guards every `onend`/`onerror` with a generation token so
a cancelled run cannot flip the UI. `text.ts` is the pure projection that makes a
reply worth hearing: fenced code and `eclipse-response` JSON are dropped, link
labels are kept while URLs are removed, tables become comma-separated rows, and
Markdown markers are stripped. Structured turns reuse
`src/lib/response/projection.ts` (the same projector as Copy/PDF) on demand.
`TtsProvider` is mounted once in `src/app/layout.tsx` and hydrates the persisted
`useTtsStore` (`be_tts_v1`); preferences are device-local (voice, rate, pitch,
volume, master enable, and an **off-by-default** `autoSpeak`). `useAutoSpeak`
speaks each *newly completed* assistant turn once and primes on mount, so loading
history never replays it. The Speak control is shared
(`src/components/chat/tts-button.tsx`) by the transcript footer, structured
response header and bot messages; it hides itself when the API or voices are
unavailable rather than offering a dead action. Settings › Voice
(`src/components/screens/voice-settings.tsx`) is the operator surface. v1 is
deliberately browser-native; a cloud voice (OpenAI/ElevenLabs) would be an
`/api/tts` proxy added later, never a replacement for the honest offline path.

**Voice input (dictation).** The mic in both composers is a separate pipeline:
`src/lib/voice/` captures audio (`MediaRecorder`), or uses the browser's
`SpeechRecognition` as the zero-config fallback, and inserts the transcript at
the caret via the pure `insert.ts` helper. `use-voice-input.ts` owns the engine
choice (`auto` = Whisper when a provider is ready, else browser; an explicit
choice never silently falls back), permissions, the hard duration cap and
cleanup. Whisper runs server-side through the Next front door
(`/api/voice/transcribe`: guard + rate limit + 15 MB/120 s/MIME caps) into the
Python runtime's operator-gated `/voice/transcribe`, which calls one
OpenAI-compatible `/audio/transcriptions` endpoint (OpenAI, Groq, or a keyless
local whisper.cpp server). Provider choice/model live in JDDS SystemConfig
(`voice_input_config`); keys stay in the shared vault (`OPENAI_API_KEY`,
`GROQ_API_KEY`, optional `TRANSCRIBE_API_KEY`). Audio is validated, transcribed
once and discarded — never written to disk, JDDS or memory. Settings › Voice
(`voice-input-settings.tsx`) reports readiness honestly; a denied microphone or
missing provider surfaces in the composer instead of being swallowed.

## The chart engine (Python-authored Plotly figures; ECharts elsewhere)

The **chat's** charts are authored in Python: a `chart` block is mapped to the
shared `ChartSpec` (`src/lib/response/chart-spec.ts`), a named `dataSource` is
resolved through the app's own `/api/price-history` route (`chart-data.ts`),
and the engine returns both a transparent PNG and a live **Plotly** figure.
`plotly-chart.tsx` paints the PNG immediately, then loads plotly.js lazily and
cross-fades to the interactive chart. The rest of the app (journal, portfolio,
dashboard, backtest, workspace widgets, the price chart and the Investor Eye
map) stays on ECharts, where it draws from live data at interactive rates.

The **Python** chart engine also records analysis pictures: the same spec is
drawn with `matplotlib` into a transparent PNG and recorded in the Artifact
Center (`python/harness/charts/`, `routers/charts.py`). An unsupported picture
mark is a named refusal, never a blank picture.

```
src/lib/response/chart-spec.ts          block -> shared ChartSpec
src/lib/response/chart-data.ts          named dataSource -> /api/price-history
src/lib/response/chart-capabilities.ts  drawability allowlist (shared with validate)
src/components/response/response-chart.tsx  the chart surface (PNG first, live chart lazily)
src/components/charts/plotly-chart.tsx      the live-chart binding (lazy plotly.js)
src/app/api/harness/charts/plotly/route.ts  the chart library route
python/harness/charts/                  spec -> Plotly figure + matplotlib PNG artifact
```

Full contract: [`docs/chart-engine.md`](chart-engine.md). Decision: **ADR-050**
(amended 2026-10-22).

## Module boundaries (terminal · shared · backend · harness)

The repository is organized by **layer**, not by feature silo. Every file belongs
to exactly one of these four zones; a change that blurs them is a boundary
violation and should be corrected before it lands.

| Zone | Lives in | Owns |
|---|---|---|
| **Frontend / terminal UI** | `src/components/` | Screens, layout, chat, chart, journal, workspace/UI shells. Client-only; `'use client'` where stateful. |
| **Shared infrastructure** | `src/lib/`, `src/hooks/`, `src/types/` | Pure logic, data access, domain modules, React hooks, shared types. Imported by both frontend and backend. |
| **Backend** | `src/app/api/**/route.ts`, `src/app/api/**` handlers, `scripts/`, `prisma/` | Thin route handlers, background workers (`src/instrumentation.ts`), importers, schema/DB. |
| **Harness** | `python/harness/` (runtime) + `src/lib/harness/` (front door) | The agent runtime (agents, tools, providers, loop, events, persistence) is Python; Next.js validates/resolves/proxies and serves the read-only JDdS bridge. The one `StateGraph` loop supports bounded, flag-gated replanning (`HARNESS_REPLAN_ENABLED`, `replan.py`, ADR-H122) on the same `tools → agent` edge — never a second loop. See below and `docs/harness/architecture.md`. |
| **Native render worker** | `rust/fframes-worker/` + `python/harness/video/` (Phases 1–8) | The fframes video renderer: a Rust binary in its own process (CPU backend, pinned `fframes =1.2.0` + committed lockfile + generated `SBOM.json`, `compile-time-svgtree`), driven by a queued Execution-Fabric job layer (`video` queue/worker-kind: projects, PNG previews, draft/final renders, a frame-inspection gate, cancel + lease recovery, Artifact Center registration on final). Storyboards are versioned (`schemaVersion` 2/3) specs over a seven-type scene library; v3 figures are evidence-bound — resolved through the EXISTING Market Data Gateway at render time with an EvidenceManifest (source, retrieval time, units, freshness), missing data fails, stale data renders flagged; revisions re-render without rebuilding anything. Preview jobs render contact sheets + per-scene frames as image artifacts and report frame findings before any render; failures are handled by a bounded repair policy (`video/repair.py`): transient classes get capped in-job retries, content failures are repaired via `repairOf`-linked revisions, and the per-failure-line repair cap refuses with `REPAIR_LIMIT_REACHED`. Hardening (Phase 8): per-project queue caps (`TOO_MANY_PENDING`), workspace GC budgets at boot (`video/gc.py`), pinned isolation (specs cannot name paths; argv + reported outputs confined to the job workspace; no OS sandbox, stated honestly), the worker SBOM drift-gated against `Cargo.lock`, a doctor probe (report-only), and the `daily-market-recap-video` job template on the ordinary agent runtime. Phase 7 put it on the app's own surfaces: an operator-gated `/video/*` HTTP route group behind the `src/app/api/video` proxy + `video-client.ts`; a `video` run blob (queued job ids, collected by `video/collector.py`) rendered as the chat's `VideoPanel` (progress polling, inline player via range-capable artifact URLs, download, cancel/regenerate/revise); conversation/run association on produced artifacts; and REAL poster thumbnails for video artifacts (an FFmpeg extractor registered on the artifact preview engine). Exposed to the agent through the fourteen `video_*` tools + the bundled `system.video-production` skill; the Graph Studio `video.render` node is DECLARED (executor-less, `since_phase`) until async graph execution + an effectful-run envelope exist; see `docs/video-pipeline.md`. |

**Rules that follow from this:**
- A backend route may import from `src/lib/`, `src/hooks/`, `src/types/` — but
  **never** from `src/components/`. Pure logic that a route needs belongs in
  `src/lib/` (this is why screener data/formatting moved to `src/lib/screener/`,
  and why sector/industry synonym normalization lives in
  `src/lib/screener/taxonomy.ts` — shared by the route and the client filter so
  the two never disagree).
- A component may import from `src/lib/`, `src/hooks/`, `src/types/` and other
  components. Server-only code must never be reachable from a client component.
- `src/hooks/` holds only React hooks; framework-free helpers live in `src/lib/`.
- Prefer `@/` path aliases over deep relative paths across zones.

### Harness placement — Python runtime + TS front door

The agentic harness is a **Python** runtime. Next.js is the front door and the
JDdS data bridge. The pre-existing research pipeline in `src/lib/agent/` remains
for the existing chat/research surfaces.

| Harness concern | Home | Notes |
|---|---|---|
| Agents, tools, model providers, LangGraph loop, events | `python/harness/` (**built**) | FastAPI + LangChain + LangGraph. The runtime owns execution; TS never runs an agent loop. |
| Model selection / the provider-agnostic Model Runtime | `python/harness/model_runtime/` (**built**) | `Agent → ModelRuntime → ModelRouter → ProviderAdapter → Provider`. Routing, fallback, retry, health/circuit breaker, rate limits, usage/cost/latency/budget, capabilities, aliases, structured output, decision tracing. `build_chat_model` is the adapter's construction primitive; no agent names a provider. ADR-H123. |
| Typed decisions (System One-shaped) | `python/harness/decisions/` (**built**) | Ask `choice`/`score`/`noul` questions about a `state`; get typed answers with probabilities/confidence. Separate from the chat runtime (no messages/tools/streaming). A provider registry (rules built; TypeSafe/LLM planned), a fail-closed confidence policy (`answered`/`review`/`unavailable`), and an immutable audit record on `agent-memory.db`. A decision is never an authorization or a verified fact. See `docs/decision-models.md`, ADR-070. |
| Run persistence + agent memory/skills | `python/harness/persistence.py` + `python/harness/eclipse_memory/` (**built**) | Two separate SQLite stores: `agent-memory.db` (runs, events, tool executions, retrieval/receipt records) and `eclipse-memory.db` (durable memory + the skill lifecycle). Neither is JDdS. See `harness/decisions.md` ADR-H5 and ADR-026. |
| Personal Memory (facts, transcript search, learned preferences) | `python/harness/eclipse_memory/` + `python/harness/session_index.py` (**built**) | One control store (`eclipse-memory.db`) and one write gate. Learned preferences are inferred only from repeated style/format behaviour across distinct runs, staged as inert `MemorySuggestion` rows with evidence in `structuredData`, and written as user-scoped `preference` memories only when accepted. See `docs/harness/personal-memory.md`. |
| Front door (validation, vault resolution, proxy) | `src/lib/harness/` (**built**) | `run-service.ts` resolves agent + LLM config; `python-client.ts`/`python-runtime.ts` proxy and spawn the service. |
| Hybrid financial data + web research (Phase 2) | `python/harness/retrieval/` (**built**) | Provider-agnostic retrieval layer: task-aware source selection, structured + web research, normalization, freshness cache, parallel retrieval, validation, conflict handling, evidence aggregation. |
| Data access for retrieval/tools | `src/lib/harness/data-bridge.ts` (**built**) | Read-only capability bridge: JDdS plus live providers (scraper chain, Yahoo bridge, economics sources) and the shared web-research transport. Still one market-data layer. |
| Web search + page retrieval | `src/lib/web-research.ts` (**built**) | The ONE shared search+fetch implementation (SSRF-hardened transport); backs the bridge and the legacy agent tool. |
| Existing chat/research pipeline | `src/lib/agent/` (**exists**) | Unchanged; its provider/tools remain for the pre-harness surfaces. `search_web` now delegates to `web-research.ts`. |
| Workflows | `src/lib/workflows/` (reserved) | Multi-step orchestration graphs over tools/skills. |
| Orchestration | `src/lib/orchestration/` (reserved) | Scheduler/planner coordinating agents, workflows and background workers. |

Reserved folders are **documentation only** until their first real module lands.
Full harness detail: [`docs/harness/architecture.md`](./harness/architecture.md).

## Architectural rules (short list)

1. Reuse existing hooks/services/primitives before writing new ones.
2. Do not duplicate business logic (`use-screener-filters.ts`,
   `use-portfolio-engine.ts`, the agent layer).
3. Server-only logic stays in API routes — never in client components.
4. Do not change data shapes screens depend on without updating all consumers.
5. Do not add a dependency without justification; prefer the lighter option.
6. Keep files small — memory is the top constraint on this machine.
7. Simulated data is always labeled; missing data is `UNAVAILABLE`, never faked.
8. Respect the module boundaries: backend routes import `src/lib`/`src/hooks`/
   `src/types`, never `src/components`; framework-free logic belongs in `src/lib`,
   not in a component folder.

