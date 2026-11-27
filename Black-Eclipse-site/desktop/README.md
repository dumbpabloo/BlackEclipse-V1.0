# `desktop/` — native shell

A Tauri 2 window around the existing application. **The desktop is the product.**
The browser/website lane is retired (kept behind `BE_ALLOW_WEB=1` for a future
web release); it is not part of the daily runtime.

Plan and rationale: `docs/desktop-app-plan.md`.

## Channels: Stable and Dev

Two independent windows, defined once in `scripts/lib/channels.js`:

| | **Stable** | **Dev** |
| --- | --- | --- |
| Binary | installed `Black Eclipse.exe` | `desktop/src-tauri/target-dev/debug/black-eclipse-desktop.exe` |
| Identity | `com.blackeclipse.desktop` | `com.blackeclipse.desktop.dev` (own single-instance key) |
| App port | 3100 | 3101 |
| Harness | 8767 | 8768 |
| Build | `.next` → versioned release | `.next-desktop-dev` |
| Data | real data directory | `.black-eclipse/desktop-dev/` |
| Changes | only via **promote** | every `npm run desktop:dev` |

Commands: `desktop:dev`, `desktop:refresh`, `desktop:promote`,
`desktop:rollback`, `desktop:doctor`, `desktop:build:dev`,
`desktop:build:dev-shell`.

Changing `main.rs` or `capabilities/default.json` needs a shell rebuild:
`desktop:build:dev-shell` for Dev; `desktop:build` (+ package/install) for
Stable. Changing the UI (`src/**`) needs only a payload build/promote.

**Promotion.** `scripts/desktop-promote.js` copies the completed build into
`releases/<channel>/<buildId>/.next`, validates it, writes `current.json`
(keeping `previous.json`), then restarts that channel's window. A failed
validation leaves `current` untouched. `desktop:rollback` swaps the pointers.
`--from .next-desktop-dev` promotes a verified Dev build without a second
identical compile; promote refuses while any build is running.

**Liveness.** The supervisor probes `/health/live` (constant time) with a 30 s
budget, not the diagnostic `/health` document — under load the latter measured
20+ s and made a healthy runtime look dead.

## Layout

| Path | What it is |
| --- | --- |
| `src-tauri/src/main.rs` | Window, tray, prefs, and lifecycle of the one child process |
| `src-tauri/tauri.conf.json` | Bundle/app config; `frontendDist` is `../ui` |
| `src-tauri/capabilities/default.json` | Grants the splash page core window/event access only |
| `src-tauri/icons/` | Copies of `public/branding` so the binary carries the app icon |
| `ui/index.html` | The boot and offline screen, embedded into the binary at compile time (brand mark inlined; renders with no network) |
| `../scripts/desktop-server.js` | Server bootstrap; prints `@@BE:*@@` markers the shell reads |

## How it boots
```
window shows instantly (bundled splash, no network)
  └─ scripts/desktop-server.js
      ├─ a packaged install (STANDALONE): identity attach as before
      ├─ a repository checkout:
      │     ├─ port 3100 live?
      │     │     ├─ this install's own server (owner marker + same pinned
      │     │     │   build) → @@BE:ATTACH@@  (never owned, never killed)
      │     │     └─ anything else → @@BE:CONFLICT@@ (stop, explain, kill nothing)
      │     └─ port free
      │           ├─ canonical BE_DATA_DIR resolved (absolute)
      │           ├─ pin the newest COMPLETED .next into
      │           │   `.black-eclipse/desktop-dist/.next`
      │           ├─ start lock acquired (pid + TTL)
      │           ├─ next start -H 127.0.0.1 -p 3100 with
      │           │   NEXT_DIST_DIR=<the pinned copy>
      │           └─ @@BE:READY@@ → window navigates to
      │                             http://127.0.0.1:3100/
```

### Database guard (why a failed check can never brick the app)

`scripts/desktop-server.js` runs `scripts/db-guard.js` in the background after
first paint and records a non-zero exit in `<data>/.runtime/db-guard.failed`. That
marker is **re-checked on the next launch**, not trusted forever: the guard runs
again and a pass clears it. A stored failure must never be a one-way door — a
marker written when the install simply could not load its SQLite binding once
blocked every later launch, so retrying could never succeed.

Two contracts keep that safe:

- **Exit codes.** `0` healthy · `1` corrupt (may rebuild from a rolling backup,
  the seed, or an empty schema) · `2` "could not inspect the file" (missing
  native binding, locked or unreadable). Only proven corruption may rebuild;
  treating a load failure as corruption would replace healthy user data with an
  empty schema.
- **Probe native modules with the bundled runtime.** The payload ships
  `runtime/node` (pinned) while the build machine may run a different Node, and
  `better-sqlite3@12` is ABI-specific — a binding built for the builder loads
  nowhere in the install. `scripts/desktop-package.js` therefore resolves the
  runtime first, fetches the matching prebuilt into the payload, and proves it
  loads under *that* binary; `scripts/desktop-installer.js` and
  `scripts/desktop-bundle.js` repeat the probe. Probing with `process.execPath`
  passes locally and ships an app that cannot open its database.
- **The release smoke test reads the database and the runtime.**
  `scripts/desktop-smoke.js` runs the packaged binary in CI and requires
  `/api/alerts` to answer 200 *and* `/api/harness/model-runtime/health` to answer
  200 — not just that `/` renders. A payload with no usable binding serves the
  page and 500s every query; a runtime that never comes up serves the page and
  hangs every agent request. A page check alone proves nothing.

### Agent runtime readiness

The Python runtime's cold-start budget (`DEFAULT_START_TIMEOUT_MS`, 5 minutes) is
for the **background spawn**, not for a page. `ensurePythonRuntime()` waits for an
in-flight start only up to `DEFAULT_REQUEST_WAIT_MS` (45 s,
`HARNESS_PY_REQUEST_WAIT_MS`), then answers `false` so the route returns 503 with
a reason; the spawn keeps running and the next request hits the healthy TTL fast
path. Without that split a stalled runtime made every agent-backed page spin
silently for five minutes, which reads as a broken app. The release smoke test
asserts the runtime becomes healthy on a packaged build (see the gate bullet
above).

**The cold-start lock carries its owner's pid.** `.black-eclipse/harness-runtime-<port>.spawn.lock`
exists so two server contexts never boot rival uvicorns. The context that loses
the lock must NOT spawn — but it also must not wait forever on a file whose owner
is dead: a single crash used to leave the lock looking live for the full 315 s
window, so every later launch logged "Python runtime still starting", spawned
nothing, and left the app with no agent runtime. `isSpawnLockStale()` takes the
lock over immediately when the recorded pid is gone and still respects a live
owner.

### Build isolation (why an update can no longer break the window)

`.next` is mutable: `next build` deletes and rewrites it in place. Serving it
directly meant every "update Eclipse" running in another tool (a build, a
restart, `desktop:refresh`) could swap the window's client chunks mid-session
and leave it unstyled with dead navigation.

A repository checkout therefore serves an **immutable release or pinned copy**
of the last completed build. Dev builds write `.next-desktop-dev`; Stable's
`.next` is only staged by promotion. A new build is picked up on the next launch
or via `npm run desktop:promote` / `desktop:refresh` — never underneath an open
window. The copy excludes `cache`, so it is ~260 MB and takes seconds.

**Port ownership.** Stable owns **3100**, Dev owns **3101**; their harnesses are
**8767** and **8768**. A channel never attaches to the other, and moving one off
its port would silently disable notifications (Tauri's capability allowlist
names both ports explicitly).

`127.0.0.1` is a secure context, so notifications, camera and microphone work
without the TLS reverse proxy the browser path uses for `https://blackeclipse`.

## Window chrome

The window is **frameless** (`decorations(false)`): the app's
`DesktopTabBar` (`src/components/layout/desktop-tab-bar.tsx`) is the title bar.
It lives at the top of the CONTENT column, so the sidebar spans the full page
height on the left and the tabs start to its right, in line with the page. It
shows the open pages as tabs, remembers them in `localStorage`, and provides the
drag region plus minimize/maximize/close. Tabs are **instances**, not a
de-duplicated page set: `+` appends a real new tab that starts on a fresh Chat
session, and navigating inside that tab (sidebar, palette, deep link) moves the
tab to the page you chose — so two Chat (or Portfolio, …) tabs can coexist.
Each tab also **owns its chat session** (`conversationId` on the stored tab
record): switching away captures the live session, switching back restores that
tab's transcript, and the front tab is remembered separately
(`be_desktop_tabs_v2_active`) so a relaunch returns to it — two Chat tabs show
two different conversations.
Tabs can be dragged to reorder and closed with their `X` (the last tab stays
open). Keyboard: `Ctrl/Cmd+Shift+Home` moves the active tab to the first slot,
`Ctrl/Cmd+Shift+End` to the last, and `Ctrl/Cmd+Shift+PageUp/PageDown` (or ←/→)
one slot at a time.

Tab reorder uses **pointer events**, not the HTML5 drag-and-drop API. WebView2
replaces the page's drag handler with Tauri's file-drop handler unless the
native window opts out, and a shell built before that opt-out cannot drag
`draggable` elements at all — pointer events work in every shell and in the
browser, so tabs reorder without a rebuild. Reorders animate with FLIP (tabs
slide between slots), the insertion point is the neighbour's midpoint (stable
under reflow), and the grab cursor appears only for the duration of a drag.
The strip still reports the drop through the same pure `moveTab` rule.

The window is nevertheless built with `.disable_drag_drop_handler()` so the
other HTML5 drop targets (workspace widgets, chart symbol drops) work in the
desktop webview. The shell never consumes Tauri `DragDropEvent`s, so opting out
costs nothing.

Each tab also owns its **sidebar menu mode** (Terminal / Agentic / Bots /
Graph), persisted as `mode` on the tab record next to `page`. The sidebar's
mode is session-scoped (one browser shell), and all desktop tabs live inside
one shell, so the strip records the mode on switch and restores it on return —
a Bots tab and a Command Center tab keep their own menus.

Saved window bounds are clamped to the monitor's **work area** on launch
(`clamped_bounds`), so a size saved on a different taskbar/resolution can never
put the bottom of the frameless window under the Windows taskbar. Maximizing is
emulated (`fill_work_area`), not native: a borderless native maximize fills the
whole monitor (taskbar included), so the shell sizes the inner area to the work
area instead and keeps the windowed bounds to restore.

The desktop shell also always opens with the **full sidebar**. The icon rail is
a runtime choice (Ctrl/Cmd+B); `readStoredCollapsed` ignores a persisted
collapsed state when the Tauri bridge is present.

**Glass window.** The shell builds a transparent window and applies Windows
acrylic blur behind the WebView (`window-vibrancy`; legacy `apply_blur` fallback).
The app sets `html.glass-window` but does **not** make page content translucent:
pages paint the theme background (`--background`), page pattern and landing
wallpaper exactly like the browser front door, and the left sidebar stays
opaque. Tinting every page with the window acrylic made a white/light theme
read as a grey page, so only the native window behind the app is glassy.
An existing conversation keeps its normal chat surface (dark `#0a0b0b` by
default). Toggle it in **Settings › General › Window › Glass window**; the choice
persists in `be_window_glass_v1` + `desktop-prefs.json` and reaches Rust through
the `be-window` event (`set-glass`). The browser front door never sets the class.

Window actions prefer the **core window API**
(`window.__TAURI__.window.getCurrentWindow()`), which needs the explicit
`core:window:allow-minimize / allow-toggle-maximize / allow-close /
allow-start-dragging` capabilities in `capabilities/default.json`. They fall
back to the `be-window-action` → `be-window` event bridge (the same
events-not-commands rule as notifications), so a shell whose global API is not
exposed still works. The bundled splash invokes `desktop_window_action`
directly. In a browser the bar renders nothing.

## Commands
```
npm run desktop:dev          # build the Dev payload + launch the Dev window (3101)
npm run desktop:refresh      # build if needed, promote, reopen Stable
npm run desktop:promote      # stage + validate + switch Stable to the current build
npm run desktop:rollback     # switch Stable back to the previous release
npm run desktop:doctor       # one-screen health check
npm run desktop:build:dev    # build .next-desktop-dev only
npm run desktop:build:dev-shell  # build the separate Dev Tauri binary
npm run desktop:check        # cargo check only
npm run desktop:build        # cargo build --release
npm run desktop:run          # launch the release binary
npm run desktop:server       # the bootstrap alone, in a terminal
npm run desktop:selftest     # prove the OS-toast path end to end (fires a real toast)
npm run desktop:payload      # standalone Next payload -> .next-desktop
npm run desktop:package      # assemble dist/BlackEclipse (app + bundled Node + shell)
npm run desktop:brand        # re-apply the Black Eclipse icon/name to the bundled runtimes
npm run desktop:installer    # NSIS installer + SHA256SUMS + winget manifests
```

`desktop:refresh` is the stable fast path: build if needed → stage/validate the
build → move `current.json` (keeping `previous.json`) → reopen Stable. It never
races a running build and never touches the Dev channel. `desktop:dev` is the
opposite lane: it rebuilds the Dev payload and relaunches the Dev window while
Stable keeps running.

Full release chain:

```
npm run desktop:payload && npm run desktop:build && npm run desktop:package && npm run desktop:installer
```

CI runs exactly this in `.github/workflows/desktop-release.yml` on a `v*` tag.

## Install layout

```
%LOCALAPPDATA%\Programs\BlackEclipse\     app (per-user, no admin, no prerequisites)
  Black Eclipse.exe
  runtime\node.exe
  app\            server.js, .next-desktop\, node_modules\, python\, scripts\, config\
  Uninstall.exe

%LOCALAPPDATA%\BlackEclipse\data\         user data — SURVIVES uninstall
  jdds.db  harness-auth.secret  ...
```

Per-user is not a style choice: the app resolves `python/harness` and its
`.black-eclipse` locks from `process.cwd()`, so the install directory must be
writable. Program Files is not.

### Task Manager identity

Windows draws each process's icon and display name from the executable that
process runs, so the install carries **two rebranded executables**:

| File | Appears as |
| --- | --- |
| `Black Eclipse.exe` | Black Eclipse (Tauri embeds `icons/icon.ico`) |
| `runtime\node.exe` | Black Eclipse Server (Node.js) |
| `app\scripts\vendor\python\python.exe` | Black Eclipse Agent Runtime (Python) |

`scripts/brand-runtime.js` rewrites the icon and the `VERSIONINFO` strings on the
Node and Python copies at package time (`desktop:package` calls it; `desktop:brand`
re-applies it to an existing `dist/BlackEclipse`). Behaviour is untouched — only
the resources change. Upstream `node.exe`/`python.exe` are Authenticode-signed and
editing invalidates those signatures; the install is unsigned anyway. A repository
checkout (`desktop:dev`) uses the machine's own interpreters and is not branded.

The `msedgewebview2.exe` helpers are the system WebView2 runtime; their icon is
Microsoft's, not ours.

**Changing the shipped icon:** replacing `src-tauri/icons/icon.ico` alone is not
enough — `cargo build` recompiles the crate but **does not rerun `tauri-build`'s
resource step**, so the linked `Black Eclipse.exe` silently keeps the previous
icon (verified: RT_ICON blobs still decode to the old artwork). Touch
`src-tauri/tauri.conf.json` (or `cargo clean -p black-eclipse-desktop`) before
rebuilding, then confirm the embedded resources actually changed. Windows also
caches shell icons per path, so after swapping the exe run `npm run setup:desktop`
(shortcut + `SHChangeNotify`) and, if Explorer still shows the old bitmap,
restart `explorer.exe`.

## Distribution

`npm run desktop:installer` writes:

| Output | Purpose |
| --- | --- |
| `dist/BlackEclipse-Setup-<version>.exe` | NSIS installer, per-user, ~450–500 MB |
| `dist/SHA256SUMS.txt` | Checksum, published beside the asset |
| `dist/winget/BlackEclipse.BlackEclipse/<version>/*.yaml` | Three winget manifests with the real hash |

**Unsigned.** The first run shows "Windows protected your PC"; users verify the
checksum and choose *More info → Run anyway*. `winget install
BlackEclipse.BlackEclipse` strips the Mark-of-the-Web and usually skips that wall
entirely. `SHA256SUMS.txt` is published beside every asset.

## Updating

- **Check:** the app's existing signed update manifest + in-app banner
  (`config/updates.json`, `src/app/api/updates/route.ts`). No shell work needed.
- **Apply:** `winget upgrade BlackEclipse.BlackEclipse`, or run the newer
  installer. Both preserve `%LOCALAPPDATA%\BlackEclipse\data`.
- **Not implemented:** silent background self-replacement. That needs the release
  signing key wired into the shell, and it must never run migrations unattended.


## Notifications: why events, not commands

The window loads `http://127.0.0.1:3100`, which is a **remote origin** to Tauri's
ACL. Two consequences, both learned the hard way:

1. Without `remote.urls` in `capabilities/default.json`, IPC is denied entirely and
   `window.__TAURI__` is absent in the app.
2. Even with it, **app-defined commands are denied** to a remote origin —
   `invoke('desktop_notify')` fails with *"not allowed. Plugin not found"*. Core
   commands (events) **are** permitted.

So the injected `Notification` shim in `src-tauri/src/main.rs` emits
`be-notify` / `be-shortcut`, and Rust listens for both. That keeps
`src/lib/alerts/browser-notifier.ts` completely unchanged. The injected script
resolves `__TAURI__.event.emit` first and falls back to
`__TAURI_INTERNALS__.invoke('plugin:event|emit', …)`, so the same bridge (and
the window chrome) survives a shell where the global API is not exposed.

`npm run desktop:selftest` reports:

```
hn:1~perm:granted~sec:1~tauri:1~ctor:NativeNotification~shimcall:dispatched~direct:ok
```

## Taskbar presence

The browser signal (a prefixed `document.title`, a red-badged favicon,
`navigator.setAppBadge`) is invisible in the shell: the window is frameless,
WebView2 has no tab strip, and it does not implement the badge API. So the shell
renders the same fact natively, driven by the same ledger:

| Signal | Windows | macOS / Linux |
| --- | --- | --- |
| Unread badge | overlay dot on the taskbar icon (`SetOverlayIcon`) | Dock / launcher count (`set_badge_count`) |
| Working | indeterminate taskbar progress (`SetProgressState`) | app-wide progress where the launcher supports it |
| Attention | one-shot `FlashWindowEx` on a new arrival while unfocused | same request, native equivalent |
| Clear | `set_overlay_icon(None)` / progress `None` | `set_badge_count(None)` |

Transport is one more core event, `be-taskbar`, emitted by
`src/lib/desktop/taskbar.ts` with the same `__TAURI__.event.emit` →
`plugin:event|emit` fallback as the notification bridge. Payload:

```json
{ "state": "idle" | "unread", "count": 3, "flash": false, "working": true }
```

`working` is independent of `state`: a background reply can land while another
run is still streaming, so the bar and the dot are not exclusive. Older shells
ignore `working` and keep working with the badge alone.

Decisions worth keeping:

- **The page owns the state.** Rust never invents an unread count and never
  clears the badge on focus — the page re-emits on focus, and a Rust-side clear
  would race that emit and leave a stale badge. `markTabRead` in the ledger is
  the only place unread goes down.
- **"Unread" means "not seen", not "not the selected tab".** The ledger uses
  `src/lib/notifications/focus.ts` (`visibilityState` + `hasFocus`), so a reply
  that lands while the app is in the background stays unread even on the Chat
  tab. Returning to the window marks the visible tab read, which clears the dot.
  The agent-run completion alert uses the same rule — that alt-tab-away case is
  the main reason the signal exists.
- **The flash is edge-triggered.** Only a new arrival while unfocused asks for
  attention; losing focus with items already waiting does not re-flash. On mount
  the previous count is seeded with the current one, so restoring unread items
  after a reload shows the dot without a flash.
- **Progress means work, not "a run exists".** Approvals and clarifications pause
  the run on a human gate; the bar stops there rather than claiming progress
  that is not happening. The count lives outside React
  (`src/lib/desktop/activity.ts`) so several mounted run surfaces share one bar.
- **The dot is rasterized in Rust**, not decoded from a PNG sent by the page:
  one 32 px asset does not justify Tauri's `image-png` feature, and no
  page-generated bytes cross the IPC bridge.
- **The badge survives hide/show.** Windows destroys the taskbar button when the
  window hides and does not restore its overlay, so the last count and the
  working flag are kept in `AppState` and re-asserted on focus. Closing quits by
  default, so the button only needs to survive a keep-warm minimize.
- **One switch covers both affordances.** Settings › Window › "Taskbar activity"
  (`be_taskbar_badge_v1`, default ON in the shell) turns the dot, the flash and
  the bar off together.

## Guarantees

- **Identity before trust.** An existing listener is identified via
  `/api/updates` (`repository` + `currentVersion`) before the window attaches.
  Liveness alone is not treated as identity.
- **A usable build, not just an identity.** Before attaching, the bootstrap
  fetches an asset the served HTML actually references. A leftover server whose
  build directory was deleted still answers `/` and identifies itself, but every
  `/_next/static/*` request fails — the window would render without styles or
  working navigation. That state is reported as a conflict with instructions,
  and the server is still never killed.
- **A redirect is not a foreign process.** The browser-path server answers `/`
  with a 307 to the canonical TLS hostname, so it has no HTML to shape-check.
  The bootstrap skips that check for 3xx and relies on `/api/updates` (API routes
  are never canonical-redirected), letting the desktop attach to a healthy
  `npm start` instance instead of refusing it.
- **Loopback only.** The desktop path spawns
  `next start -H 127.0.0.1`; it deliberately does not use the shared
  `startServer()` helper, which binds every interface.
- **One data directory, by construction.** `BE_DATA_DIR` is resolved to an
  absolute path *before* any service starts and passed explicitly. The same
  variable is read by `src/lib/db.ts`, `python/harness/persistence.py` and
  `python/harness/eclipse_memory/config.py`.
- **Never kills what it did not start.** No `killPort` on this path. The owner
  flag is separate from attachment, and only an owned tree is terminated.
- **One starter at a time.** `.black-eclipse/desktop-server.lock` (pid + TTL)
  serializes concurrent launches; a stale lock is taken over.
- **Never builds at launch.** A missing build produces a readable error.
- **Failure is visible.** A server that dies after readiness swaps the page for
  a failure panel with Reload. A missing WebView2 runtime gets a message box with
  the download link and the browser fallback.
- **A failed navigation never shows the webview's own error page.** WebView2
  would otherwise paint its Edge/Bing-branded "can't reach this page" document,
  which the shell cannot restyle (the app document is already gone). On Windows
  the shell subscribes to `NavigationCompleted` through `webview2-com` and, on an
  unsuccessful navigation, navigates back to the bundled splash with an
  `#offline=<reason>` fragment; the splash renders the Black Eclipse mark and
  copy for "local service stopped", "offline", "timed out" or "unavailable".
  The splash's primary action (`desktop_reopen`) probes the loopback port and
  only restarts the supervisor when the service is actually down.
- **A wedged server is caught mid-session.** The bootstrap reports a child that
  exits, but a Next server whose event loop is stuck keeps the socket open and
  the last document on screen. Once the window is on the app origin, a watchdog
  probes `/api/updates` every 15 s and, after three unanswered probes (45 s),
  replaces the page with the same branded offline screen. It ignores the splash,
  so booting and the fallback are never mistaken for a hang.
- **The window never leaves the app origin.** `on_navigation` allows only the
  bundled asset origin and the loopback app port; any other http(s) URL is
  cancelled and handed to the OS browser (non-web schemes are refused). This is
  what keeps a remote link (and its failure page) out of the desktop window.
  `on_new_window` denies the new window: app-origin popouts navigate the existing
  window, and external URLs open in the OS browser.
- **Closing the window quits the app.** Closing (the tab-strip `X`, the taskbar
  menu, or Alt+F4) hides the window, stops the owned server tree and exits, so
  there is no invisible process left behind. **Run in background** is the one
  opt-in tray switch and it persists to `.black-eclipse/desktop-prefs.json`
  (window bounds live there too):
  - it writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (value
    `Black Eclipse`) with `"<exe>" --background`, so sign-in starts the window
    minimized;
  - it sets `keep_warm`, so closing the window **minimizes** it instead of
    hiding it: a hidden window has no taskbar button, which would remove the
    badge, the progress bar and the attention flash. The server stays warm and
    the scheduler keeps running; the tray and the taskbar both reopen the same
    window.
  - Both halves are one switch because either alone leaves a silent gap:
    autostart without keep-warm quits on close, keep-warm without autostart does
    not survive a reboot. A failed registry write turns the switch back off and
    logs the reason; the menu never claims background mode it did not get.
  - Unregister with the same switch, or delete the `Run` value manually.
  - `desktop-prefs.json` carries a `version` field: files written before v1 (no
    `version`) inherited `keep_warm: true` from the old default, so they are
    migrated once to `false` rather than making every existing install keep
    minimizing forever. `auto_start` defaults to `false` and is not migrated.
- **Logs:** `.black-eclipse/desktop-native.log`, truncated at 8 MB. Read
  byte-wise (lossy), so non-UTF-8 output from Python/Next cannot end the stream.
- **Downloads are written by the shell, not WebView2.** WebView2 has no download
  shelf: an `<a download>` click or a `blob:` download could silently do nothing
  ("the download button is broken"). Every download surface therefore hands its
  bytes to the shell over the `be-save-download` core event (name + base64); Rust
  writes a real file in the OS **Downloads** folder with a collision-safe name,
  reveals it in Explorer and reports the absolute path back on
  `be-download-saved`. Client helpers: `src/lib/desktop/download.ts`
  (`saveDownloadViaShell`, `downloadFromUrlInShell`), used by the response
  JSON/MD/PDF actions, the Artifact Center, chart PNG, 3D/CAD exports and
  interactive-app HTML. A browser (no bridge) keeps the anchor download.
- **External links open in the real browser.** A `target="_blank"` anchor is a
  new-window request this single-window shell does not host, so the update panel's
  installer link and "What's new" used to do nothing. They now go through
  `be-open-external` (`src/lib/desktop/external.ts`); Rust accepts `http(s)` only
  and hands the URL to the OS.

## Known limits (documented, not hidden)

- Code signing and an installer are not part of this shell yet.
- A checkout serves the **last completed** build, not unsaved source. Refresh it
  with `npm run build` + `npm run desktop:refresh`; a launch refreshes the pin
  automatically once `.next` contains a completed build.
- **Background mode needs an awake machine.** Windows sleep/hibernate stops the
  scheduler and any in-flight run; the shell does not call
  `SetThreadExecutionState`. Sign-in autostart is Windows-only today — a
  macOS/Linux shell must replace the one `set_login_item` function.
- Playwright features that expect a browser download are untested inside
  WebView2; the app's own download buttons no longer depend on it (see the
  shell-owned download bridge above).
