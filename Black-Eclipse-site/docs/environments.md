---
title: Development and production servers
status: Current
last-verified: 2026-11-21
---

# Desktop channels — Stable and Dev

BlackEclipse is a **desktop product**. There is no browser/website runtime in
the daily loop. Two independent desktop channels can run at the same time:

| | **Stable** | **Dev** |
|---|---|---|
| Start | installed `Black Eclipse.exe` (or `npm run desktop:refresh`) | `npm run desktop:dev` |
| Window | `Black Eclipse.exe` | `black-eclipse-desktop.exe` (`target-dev`, own identifier) |
| URL | `http://127.0.0.1:3100` | `http://127.0.0.1:3101` |
| Python harness | `http://127.0.0.1:8767` | `http://127.0.0.1:8768` |
| Build | `.next` → versioned release | `.next-desktop-dev` |
| Served artifact | `releases/stable/<buildId>/.next` (immutable) | `releases/dev/<buildId>/.next` or its pin |
| Data | repository root (checkout) / `%LOCALAPPDATA%\BlackEclipse\data` (install) | `.black-eclipse/desktop-dev/` (isolated) |
| Changes | only via `npm run desktop:refresh` / `promote` (or live mode) | every `npm run desktop:dev` |
| Iterating on UI | `npm run desktop:live` (hot reload) | `npm run desktop:live -- --channel dev` |
| Rollback | `npm run desktop:rollback` | re-run `desktop:dev` |

The channel table lives in **one file**: `scripts/lib/channels.js`. Ports are
fixed because Tauri's IPC capability allowlist cannot wildcard a port; moving a
channel would silently disable notifications.

**Why this split exists.** A build/test used to share `.next`, port 3100 and the
harness port with the app the user was working in. That is why "I built it, then
it broke" kept happening. Now a Dev build cannot rewrite Stable's artifact, the
two harnesses are independent, and killing Dev cannot touch Stable.

**Promotion is the only way Stable changes.** `npm run desktop:promote` copies
the completed `.next` into `releases/stable/<buildId>/`, validates the copy,
writes `current.json` (keeping `previous.json`), then restarts the Stable
window. A build that fails validation never becomes current. `npm run
desktop:rollback` flips back. `npm run desktop:refresh` is build-if-needed +
bridge + promote + reopen.

**A refresh also repairs a stale install.** A packaged Stable shell normally
serves the frozen UI bundled at `<install>\app`, so promoting
`releases/stable/<buildId>/.next` alone changes nothing the window shows. Before
promoting, `desktop:refresh` checks for the dev bridge; when it is missing it
stops the window and its owned server, bridges the install to this checkout
(`scripts/desktop-dev-bridge.js`, preserving the bundled payload as
`app.bundled-<version>`), then promotes and relaunches. This happens after the
build, so the old window keeps working during the slow compile.

**Orphaned server recovery.** A window can crash, be force-closed, or be
promoted while its server tree is still exiting. The next launch then finds the
channel port held. `scripts/desktop-server.js` records the bootstrap pid in
`<data dir>/.runtime/desktop-owned.json`; when the listener is still that
recorded bootstrap (command line re-verified, so a reused PID is never killed),
the new launch stops the orphan and starts a fresh server instead of parking on
the "leftover server" Retry screen. It also attaches to a healthy own server
when its build matches the promoted release — not just the ad-hoc pin — and
detects a bound-but-unresponsive listener (a liveness probe can miss a blocked
event loop) before spawning, so `EADDRINUSE` no longer surfaces as a bare
"server exited" panel. A listener with no matching owner marker is still never
killed.

## Live mode — the window updates on every save

The immutable pin is right for a shipped build and wrong for development: a
source edit is invisible until the window is rebuilt, re-pinned and reopened.
That gap is why a change can be live in the Python runtime (it runs from
`python/` source) while the window still shows the previous UI.

**`npm run desktop:live`** closes the channel's window/server and relaunches it
in live mode: `scripts/desktop-server.js` starts `next dev` on the channel port
instead of `next start` against a pin, so saving a file under `src/` hot-reloads
the open window. There is no build, no promotion and no relaunch per change.

- It writes to its own dist dir (`.next-live-stable` / `.next-live-dev`), so a
  live session can never clobber the production `.next` that `desktop:refresh`
  builds and promotes.
- Live mode refuses to start if the channel port is already held; the launcher
  closes the previous window and its owned server first.
- Python is never pinned, so harness changes need only a runtime restart - live
  mode does not change that.
- Live mode hot-reloads the **Next.js UI only**. The Rust shell
  (`desktop/src-tauri`) is a compiled binary, so a shell change needs a new
  binary: `npm run desktop:build:dev-shell` (Dev) or `npm run
  desktop:installer` (Stable). The launcher compares the Rust source mtimes with
  the binary and names the command when they drift, so a stale shell is never a
  silent surprise.
- Stable needs the installed shell to run this checkout's bootstrap. The
  launcher does that itself (`scripts/desktop-dev-bridge.js`, which preserves the
  bundled payload as `app.bundled-<version>`), so there is no separate setup
  step; the Dev shell is built once on first use.
- Leaving live mode is an ordinary command: `npm run desktop:refresh` (build,
  promote, reopen Stable) or `npm run desktop:dev` (Dev channel).
- Stable requires the dev bridge (`npm run desktop:dev-bridge`) so the installed
  shell runs the repo's bootstrap. Both `desktop:live` and `desktop:refresh`
  apply the bridge automatically (it is reversible), so there is no manual setup
  step; a failure is reported with the command to run by hand. Before renaming
  `app`, the bridge stops any process tree still running from the install
  directory — an orphaned bundled `server.js`/Python harness otherwise holds the
  folder and Windows answers `EBUSY`.

**Health is cheap.** The harness serves `/health/live` (constant time) for the
supervisor's readiness probe; `/health` remains the diagnostic document. The
probe budget is 30 s, because the full document was measured at 20+ s under a
concurrent build and used to be mistaken for a dead runtime.

**The browser lane is deleted, not just gated.** `npm run dev`, `up`, `start`,
`proxy`, `setup:hostname`, `setup:tls`, `web-guard.js`, `local-host.js`,
`local-proxy*.js`, `start-desktop.js`, `start-eclipse.vbs`, `lib/profiles.js`,
`lib/tls.js` and the proxy/TLS/splash helpers are gone. `black` (the CLI) no
longer defaults to a website: it opens the native app, and `black eclipse`,
`black start` and `black proxy` print where the functionality went and exit 1.
There is no flag to bring the browser server back. Validation is `npm test`
(while iterating: `npm run test:fast` = `tsc` plus only the tests reachable from
your working-tree changes; before reporting done: `npm run verify` = the FULL
suite for each domain you changed - `src/` → vitest, `python/` → pytest, the
memory subsystem → eval floors. Neither is a substitute for `npm test` on a
release),
`npx tsc --noEmit` and a desktop build — no server.

**Nothing advertises a browser address.** `bin/black.js`, `setup:desktop` and
the shortcut all target the installed `Black Eclipse.exe`; the shortcut
installer fails loudly instead of creating an icon for a launcher that no longer
exists. `scripts/lib/channels.js` keeps `webLaneAllowed()`/`refuseWebLane()` as
a last-resort guard, but nothing in the tree calls them.

## What is shared, and what is not

Shared on purpose:

- `node_modules`, the vendored Python interpreter, the source tree and the git
  worktree.
- The build lock (`.next-build.lock`), so only one build runs at a time on the
  checkout even across channels.

Isolated:

- **Everything a channel serves.** Stable reads a versioned release or its pin;
  Dev reads `.next-desktop-dev` / its own pin. Neither reads the other's build
  while it is being written.
- **Data.** Stable uses the real data directory. Dev uses
  `.black-eclipse/desktop-dev/`, seeded once with a consistent `VACUUM INTO`
  snapshot of `jdds.db`. A dev experiment can never write the real journals or
  memory.
- **Processes and ports.** App 3100/3101, harness 8767/8768, owner markers and
  start locks live under each channel's data directory.

Builds are **serialized**: `npm run build`, `desktop:build:dev`,
`desktop:refresh`, `desktop:promote` and the desktop launcher all take
`.next-build.lock` before running `next build`. A second build waits
instead of wiping the same directory underneath it. The lock lives outside
`.next` because the build deletes that directory; it is advisory, so a raw
`npx next build` bypasses it.

**The production build needs more than Node's default heap — or Turbopack.**
The module graph exceeds Node 22's ~4 GB ceiling, so the managed entry points
(`scripts/lib/server.js` → `build()`, and Dev's `scripts/desktop-build.js`)
default `NODE_OPTIONS` to `--max-old-space-size=12288` unless the caller already
set an explicit heap size. Both paths now pass `--turbopack` in production
(`BE_BUILD_WEBPACK=1` restores webpack): the webpack compile of this graph grew
past 15 GB on a 16 GB host, while Turbopack builds the same tree in a few
minutes. The 12 GB ceiling remains for the webpack fallback and the release
workflow's Windows payload builds through v0.4.10. Override with
`BE_BUILD_HEAP_MB` on a tighter or roomier machine. A 4 GB crash is the
signature of that default being bypassed.

Two builds at once still cannot fit on a 16 GB host — a raw `npx next build`
bypasses `.next-build.lock`, so it can run beside a managed build and both die
with `Ineffective mark-compacts near heap limit`. `next.config.ts` also sets
`experimental.webpackMemoryOptimizations` for the fallback (a little slower,
less heap). A production build that OOMs is not an update bug: check
`Get-CimInstance Win32_Process -Filter "Name='node.exe'"` for a second
`next build` before raising the heap. The git fallback of the in-app updater
inherits this same ceiling because it runs `npm run build` on the user's machine;
the normal path downloads the release's prebuilt build instead of compiling, and
a packaged install does not build at all (see `docs/UPDATES.md`).

**Build freshness is one hash, so the build must land where the hash lives.**
`build()` records the source hash in `.next/.build-hash` and `needsBuild()`
compares against it, which is what lets `desktop:refresh` / `npm run build` skip
a three-minute compile. That only holds if the compile actually writes `.next`:
`next build` takes its output directory from `NODE_ENV` (dev → `.next-dev`) and
`NEXT_DIST_DIR` (live/dev/payload lanes), so `build()` pins both to production
and `.next` for the child. Inheriting a dev `NODE_ENV` from the shell used to
send the output to `.next-dev` while still marking `.next` current — the next
refresh then announced "the current source is already built", promoted the OLD
build, and the app looked like it had ignored the update. If a build ever
claims to be current while the window still reports an older version, delete
`.next/.build-hash` (runtime data, never committed) to force a real rebuild.

**A plain `npm run build` refuses while a channel is live.** The window is safe
(it serves a pinned copy), but a build started under a running app saturates the
CPU — which starves the Python runtime's cold start — and leaves `.next` without
a `BUILD_ID` until it finishes. `scripts/lib/build-guard.js` owns the policy
(pure + unit-tested); `build-prod.js` probes the channel ports and owner markers.
The managed paths set `BE_ALLOW_BUILD_WHILE_RUNNING=1` because they own the pin
and the restart: `npm run desktop:refresh` (via `scripts/desktop-build.js`) and
`black update`.

Reaching Stable requires **promotion**, never an in-place rewrite:
`desktop:promote` validates the staged copy, moves `current.json` (keeping
`previous.json`), and restarts the window. See
[`desktop/README.md`](../desktop/README.md#channels-stable-and-dev).

## Coding tool surface

The code-agent profile runs a 48-tool coding preset (`CODING_SURFACE` in
`python/harness/code/policy.py`; 52 declared, the four ship actions are removed
below `approve_external`): project/read/search, CIUE diagnostics, edit/write/
apply, the local git commit path (stage/commit/branch/restore/stash), the
verification path (`build_project`/`test_project`/`run_verification`/
`execution_get`), `command_run`/`terminal_run`/`shell_spawn`, ship actions (still
approval-gated), and the discovery pair. Runtime/container/cloud/orchestration/
package/browser/computer tools are removed before the model sees them.

- `BLACK_ECLIPSE_CODING_SURFACE=0` disables the preset (the full declared
  surface returns).
- `BLACK_ECLIPSE_CODING_EXTRA_TOOLS=container_run,package_install` adds named
  tools back for the next run. The extras still pass through the autonomy
  profile, so a write tool cannot be added to `plan_only`.
- The model cannot widen its own surface. `tool_search` still names an outside
  tool and marks it `outsideSurface` / `requiresEnable`; `tool_invoke` refuses
  it.

## Commands

| Command | Effect |
|---|---|
| `npm run desktop:dev` | Build/pin the Dev payload and launch the Dev window (3101) |
| `npm run desktop:live` | Launch the window against `next dev` — hot reload on every save (use `-- --channel dev` for Dev) |
| `npm run desktop:refresh` | Build if needed, promote, reopen Stable |
| `npm run desktop:promote` | Stage + validate the current build, make it Stable, restart |
| `npm run desktop:rollback` | Flip Stable back to the previous release |
| `npm run desktop:doctor` | One-screen health: window, ports, build, harness, provider |
| `npm run desktop:build:dev` | Build `.next-desktop-dev` only (no launch) |
| `npm run desktop:build:dev-shell` | Build the separate Dev Tauri binary |
| `npm run build` | Build Stable's `.next` (still the source for promotion) |
| `npm run app` / `black` | Open the installed Stable desktop app |
| `black status` / `black stop` | Channel up/down, and stop only the tree it owns |

`black eclipse`, `black start` and `black proxy` are retired shells that print
the desktop commands and exit non-zero. No npm script starts a browser server.

## Dev chunk caching, and the "module factory is not available" overlay

A Turbopack **dev** chunk is named after the MODULE it serves
(`src_components_screens_a6343f43._.js`), not after the bytes it holds. Edit,
rename or delete a module and the bytes behind that same URL change while the
URL stays put. So dev chunks must never be served `immutable`: the browser
would keep the OLD bytes, ask the runtime for a module factory the server no
longer has, and the page would die behind "the module factory is not available.
It might have been deleted in an HMR update".

Dev chunks therefore **revalidate** (`public, max-age=0, must-revalidate`, see
`next.config.ts`). The dev server already sends `ETag`/`Last-Modified`, so a
second tab still gets a cheap `304` instead of re-downloading the whole client
graph, and a changed chunk is always fetched fresh. Production is unaffected:
a real build is content-hashed and immutable.

A browser that already cached the old immutable copies needs one repair:
`GET /api/dev/cache-reset` (dev only) answers with `Clear-Site-Data: "cache"`
— the HTTP cache only, never cookies or localStorage, so the operator stays
signed in. Then reload the app.

## Docker

**Removed (2026-11-13).** `Dockerfile`, `docker-compose.yml`,
`docker-entrypoint.sh` and `.dockerignore` were deleted with the browser lane —
a container running `next start` was a server deployment of a product that has no
server. `python/Dockerfile.quantsdk` remains: it is a sandbox image for the
Python quant SDK, not an app deployment.
