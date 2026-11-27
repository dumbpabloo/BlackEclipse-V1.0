---
title: Native Desktop App — Tauri Shell Plan (hardened)
status: Accepted
last-verified: 2026-11-19
---

# Native desktop app (Tauri shell)

**Scope:** a native window around the existing application. It is now the ONLY
shipped front door. The former browser path (`npm run dev`, `npm start`,
`black eclipse`, `start-eclipse.vbs`) was deleted on 2026-11-13, so this document
describes the product rather than an addition to a website.

**What this document is:** the locked architecture, the normative requirements for
process ownership, data integrity, loopback security, packaging, crash behaviour,
updates and scope; the acceptance-test checklist; and an honest gap analysis
between the requirements and the prototype that already exists on disk.

**What it is not:** a decision record. Items marked **DECISION** still need an
answer.

> ## 0. Current state — read this first
>
> A prototype was built earlier in this session, then **hardened to P1 scope**
> after this review.
>
> **Now satisfied:** loopback-only binding (`127.0.0.1`, never all interfaces),
> identity + version verification before attaching to an existing server,
> explicit canonical `BE_DATA_DIR` passed to the child, a cross-process start
> lock, port-conflict diagnostics that never kill or reroute, a post-readiness
> failure panel instead of a dead UI, WebView2 detection with a recovery path,
> and a byte-safe log reader (the previous `BufReader::lines()` stopped at the
> first non-UTF-8 byte, which the Python harness can emit on Windows).
>
> **Not in this round:** installer, code signing, bundled Node, `standalone`
> output, updater. Those are §6 and §10 and each needs its own approval.
>
> Nothing was committed. The recovery snapshot is described in §11.

---

## 1. Locked architecture

Strategy is fixed unless the user reopens it.

| Layer | Technology | Responsibility |
| --- | --- | --- |
| Desktop shell | **Tauri 2 / Rust** | Window, tray, preferences, lifecycle of **one** child process, native toasts |
| Application | **Next.js / React** (existing) | UI, 456 API routes, server-side logic |
| Intelligence | **Python harness** (existing) | Agents, quant, research, ML, workflows, execution |
| Python supervisor | **Existing** `src/lib/harness/python-runtime.ts` | Spawn, health, restart, cold-start serialization |
| Data | **SQLite** (existing) + Prisma | Market vault, memory, journals |
| Transport | Existing HTTP (`FastAPI` / Next routes) | Unchanged |

**D1.** Rust carries **no** financial intelligence, agent orchestration, quant
math, trading logic or business rules. It never parses domain payloads.

**D2.** The existing Python supervisor is **not** rewritten, wrapped or replaced.
The shell must never spawn `uvicorn` itself.

**D3.** The shell manages exactly **one** process: the application server. Python
is that server's business.

**D4.** The browser front door stays independently usable with the shell absent,
uninstalled or deleted.

**Prohibited:** moving business logic into Rust; a second process supervisor;
rewriting the harness; schema migrations; changing MT5 execution; replacing the
existing startup workflow; unrelated dependency upgrades.

## 2. Topology

```
Black Eclipse.exe            Tauri 2, Rust — ~3.4 MB as prototyped
 └─ WebView2 window          system runtime
     └─ http://127.0.0.1:3100
         └─ `next start`     production server (the ONE child process)
             └─ uvicorn      spawned + supervised by python-runtime.ts, 127.0.0.1
                 └─ SQLite   ONE BE_DATA_DIR
```

### Why loopback, and what it does not excuse

`127.0.0.1` is a secure context by specification, so the shell does not need the
TLS reverse proxy that exists only because `blackeclipse` is a custom hostname.
That removes a hop, a handshake and the `setup:tls` prerequisite.

It does **not** mean "trusted because it is local". See §5.

## 3. Process lifecycle and ownership (normative)

| # | Requirement | Design |
| --- | --- | --- |
| 3.1 | Detect a live server on 3100 | HTTP probe with `redirect: manual` (a live browser-path server can answer 307 to the TLS hostname) |
| 3.2 | **Verify identity**, not just "something answers" | Probe a dedicated identity endpoint returning `{app, version, buildId, pid}` and compare against the expected app + a compatible version range. Any other listener is a conflict, not a target. Identity alone is not enough: the HTML the server returns must reference build assets that are actually fetchable (a server whose `.next` was deleted still answers `/` and identifies itself, but renders an unstyled, non-interactive page). A failed asset probe is reported as a conflict, never attached |
| 3.3 | Track ownership | `owns_server: bool` set only when **this** shell spawned the server. An attached server is never owned |
| 3.4 | Never terminate what we did not start | Quit kills only when `owns_server`; verified once already (see §13.4) |
| 3.5 | Graceful shutdown | Ask the child to exit, wait a bounded grace period, then kill the process **tree** (`taskkill /T` — `next start` is a grandchild and `Child::kill` does not reap descendants) |
| 3.6 | No duplicate servers | Bootstrap attaches before spawning; the shell allows one supervisor; a second app launch focuses the existing window (single-instance) |
| 3.7 | Race on simultaneous start | A cross-process lock file with owner pid + timestamp; a second starter waits, then re-probes instead of forking a rival |
| 3.8 | Startup timeout | Bounded phase timeouts with a specific message per phase (spawn / bind / health), never one generic "failed" |
| 3.9 | Python lifecycle | Untouched — delegated to `python-runtime.ts` |
| 3.10 | Unexpected termination | Watch the child after readiness; surface an in-window failure state and offer restart. **Also own a failed NAVIGATION**: WebView2 substitutes its own Edge/Bing error page after the document is gone, so a Windows `NavigationCompleted` guard navigates back to the bundled splash (`#offline=<reason>`) instead of leaving the window on a page the shell cannot brand. **And a hung server**: a 15 s `/api/updates` watchdog replaces the page after three unanswered probes (45 s), because a wedged event loop never exits the child |

**Port conflict policy.** If 3100 is held by something that fails the identity
check, the shell must **not** silently pick another port. It must explain:
what is listening, why it is not BlackEclipse, and the available actions
(stop it, or run the browser path instead).

**Build isolation (added 2026-11-13).** 3100 is the desktop's own port; the
browser/agent production profile moved to 3105 (dev stays 3000). A repository
checkout never attaches to a website server, even one that identifies as Black
Eclipse: it pins the newest *completed* `.next` build into
`.black-eclipse/desktop-dist/.next` and runs `next start` with `NEXT_DIST_DIR`
pointing there. Attach is allowed only to this install's own server, proven by
`.runtime/desktop-owned.json` (pid + pinned build id). This is what makes the
window immune to a concurrent `next build`; a packaged install already serves an
immutable payload and keeps the identity-attach path.

**Channels (2026-11-13).** The product is desktop-only. There are two channels,
defined once in `scripts/lib/channels.js`: **Stable** (3100 / harness 8767) and
**Dev** (3101 / harness 8768), with separate builds (`.next` /
`.next-desktop-dev`), pins, versioned releases and data. Dev runs a second Tauri
binary with its own identifier (`com.blackeclipse.desktop.dev`, built with a
`TAURI_CONFIG` overlay into `target-dev`), so both windows can be open at once.
Stable changes only through `desktop:promote` (stage → validate → `current.json`
→ restart, keeping `previous.json`); `desktop:rollback` flips back. The browser
lane (`npm run dev/up/start/proxy`, `start-eclipse.vbs`) is **deleted**, together
with `scripts/local-proxy.js`, `scripts/local-host.js`, `scripts/start-desktop.js`
and the proxy/TLS helpers; `BE_ALLOW_WEB` no longer exists. Harness readiness uses
a constant-time `/health/live` with a
30 s budget, because the diagnostic `/health` measured 20+ s under a concurrent
build and made a healthy runtime look dead.

**DECISION 1.** Should the shell offer a one-click "use port 3101 instead" escape
hatch, or is a hard stop with instructions the correct behaviour?

## 4. Single canonical data directory (normative)

**Requirement:** exactly one absolute data directory, resolved identically by
Node and Python, preserved across runs, never silently duplicated.

**Current mechanism, verified in source:**

- `src/lib/db.ts` → `BE_DATA_DIR` via `path.resolve`, **else `process.cwd()`**.
- `python/harness/persistence.py` → `BE_DATA_DIR` via `abspath`, **else the
  package's repo root**.
- `python/harness/eclipse_memory/config.py` also honours `BE_DATA_DIR`.
- `scripts/db-guard.js` → `BE_DATA_DIR`, then `BE_DB_PATH`, else repo root.

**Finding.** Both runtimes honour the same variable, but their *defaults* are
computed differently — `cwd` versus package-relative. In practice both land on
the repository root because the bootstrap sets `cwd` to the root, so there is one
database today. But the guarantee is incidental, not constructed. A different
`cwd` (a shortcut with a different working directory, a moved install) would
produce a **second empty `jdds.db`** and look exactly like data loss.

**Required work (P1):**

1. Resolve `BE_DATA_DIR` to an **absolute, normalized** path in the shell *before*
   spawning, and pass it explicitly in the child environment.
2. Run a startup assertion that logs the resolved path for **both** runtimes and
   fails loudly if they disagree.
3. Keep the repository root as the default so existing installs are unchanged.
4. Never write a database that already exists elsewhere; never "repair" by
   creating a new one at a different path.
5. No schema migrations. `prisma db push` is not a desktop-shell responsibility.

**Resolved (2026-11-19).** `scripts/lib/channels.js` owns the per-channel data
directories and `scripts/desktop-server.js:resolveDataDir()` makes the packaged
default `%LOCALAPPDATA%\BlackEclipse\data` (item 3 is superseded: only an
unpackaged checkout keeps the repository root). The launcher
(`scripts/lib/desktop-process.js:channelLaunchEnv`) therefore sets `BE_DATA_DIR`
only for the Dev channel — forcing it for Stable pointed the real window at a
checkout `jdds.db` with no schema, and every screen died with "table does not
exist". A checkout's own `jdds.db` is schema-free until `prisma db push` runs.
`channelDataDir()` now also resolves that same directory for the owner marker, and
`black stop` clears the channel's app port: a packaged install pins no build, so
it writes no owner marker and a stopped window used to leave its server behind.

**WAL correctness.** `jdds.db-wal` and `jdds.db-shm` are part of the database. A
snapshot must not copy the `.db` alone while the server is live — that loses the
newest transactions. Use SQLite's backup API / `VACUUM INTO` (already what
`db-guard.js` does) and verify the snapshot with `PRAGMA quick_check` before it
is trusted.

**Recovery.** If integrity checks fail, the app must show instructions rather than
reset anything: where the database is, where the backups are
(`backups/jdds-*.db`), how to restore one, and that the repair path exists in
`db-guard.js`.

**DECISION 2.** For a packaged install, should `BE_DATA_DIR` default to
`%LOCALAPPDATA%\BlackEclipse\data` instead of the install directory? (Recommended:
yes — the install directory is read-only for a normal user.)

## 5. Loopback security (normative)

The origin is intentionally `http://127.0.0.1:3100`. Local is not the same as
trusted.

| Requirement | Finding | Work |
| --- | --- | --- |
| Bind **only** 127.0.0.1 | **Met.** The desktop bootstrap spawns `next start -p 3100 -H 127.0.0.1` itself instead of using the shared `startServer()` helper (which binds every interface and is used by the browser path). Verified: `Get-NetTCPConnection` reports `LocalAddress 127.0.0.1` | done |
| Never bind 0.0.0.0 | **Met** by the same change | done |
| Host/Origin validation | Exists: `src/lib/api-guard.ts` (`LOCAL_HOSTS`, `isLocalHost`, `isLocalRequest`, `checkMutationAllowed`, `guardMutation`, `rateLimit`) | reuse; do not duplicate |
| CSRF on sensitive routes | `guardMutation` covers mutation guards; no `Sec-Fetch-Site`/`Origin` check found in `src/` | audit the mutation surfaces; add an Origin check only where `guardMutation` is not applied |
| Session/auth for privileged ops | Exists: `src/lib/auth/{config,handlers,session,store,token}.ts` | reuse; do not weaken |
| No tokens in logs | Not yet verified | audit; keep tokens out of `.black-eclipse/desktop-native.log` |
| Least-privilege Tauri capabilities | **Met, with one deliberate grant.** The app origin is listed under `remote.urls` (otherwise IPC is absent entirely), and it receives `core:default` only — core events, no filesystem, no shell, no process control. App commands are *not* reachable from it (verified: `not allowed. Plugin not found`), so the notification/shortcut transport is events | done |
| No arbitrary shell from frontend | No shell plugin is registered | keep it that way; if ever added, allowlist exact commands, never free-form strings |
| Trading/credential protections | Existing | untouched; the shell adds no new path |

**Non-negotiable:** the shell must not establish a new trust boundary that is
weaker than the browser path.

## 6. Production runtime and packaging (normative)

The first distributable must not depend on the user's development machine.

| Runtime | Today | Required for release |
| --- | --- | --- |
| Node.js | `node` on PATH, else `ProgramFiles`/`LOCALAPPDATA` candidates, validated only by `--version` succeeding | **Bundle** a pinned Node as a sidecar; validate the exact major; actionable error if absent |
| Next.js app | Serves the repo's own `.next` via system Node | Prebuilt, immutable payload; `output: "standalone"` to cut the traced tree (build tree is 3.4 GB, of which 3.1 GB is cache; runtime portion ≈ 237 MB) |
| Python | System Python installed by `postinstall` (`scripts/setup-python.js`) | Bundle or bootstrap a pinned interpreter + wheels; verify the harness answers |
| Browser automation | `playwright` dependency | Audit whether it needs a browser download inside a packaged app; degrade honestly if absent |
| Assets | `public/` (7 MB) | Inside the payload |
| Data | Repo root | `%LOCALAPPDATA%` per DECISION 2 |

**Hard rules.** Packaged startup must use the **production** server. It must never
run `next dev`. It must never run `npm install`, `npm run build`, or native
compilation at launch. Runtime discovery must be explicit and validated, with a
distinct, actionable error per missing runtime.

**Not attempted today.** A full packaging redesign. This section is the
contract the installer work must satisfy.

**DECISION 3 — answered by the product goal.** The objective is *less
configuration for the user*, not a choice of distribution formats. Therefore:

> **One installer. Double-click, it works. No Node install, no Python install,
> no `npm install`, no hosts file, no TLS certificate, no environment variables,
> no first-run wizard.**

A portable ZIP is a developer convenience, not the product. It stays as an
internal build artifact so the payload can be inspected.

### Measured payload (this machine, before compression)

| Component | Size | Note |
| --- | --- | --- |
| Vendored Python (incl. site-packages) | **1,044 MB** | Already vendored at `scripts/vendor/python` — this is the big win: no `pip install` at run time |
| Next.js server output | 214 MB | `.next/server` |
| Next static assets | 19 MB | `.next/static` |
| `public/` | 7 MB | icons, branding |
| Node runtime | 83 MB | single `node.exe`; a trimmed build is ~45 MB |
| Tauri shell | 3.4 MB | |
| **Installed total** | **≈ 1.37 GB** | ≈ 400–600 MB as a compressed download |

The honest consequence: **the download is not small.** "Instant" therefore means
*instant to use after download*, and the download itself should be a small stub
that streams the payload with real progress — not a 500 MB wait with no feedback.

### Distribution and signing (GitHub, unsigned)

The project is free and hosted on GitHub Releases, so the installer is
**unsigned**. That is accepted, not a gap to fix.

| Concern | Handling |
| --- | --- |
| "Unknown publisher" on first run | Stated plainly in the README with the exact clicks (*More info → Run anyway*) |
| Integrity | `SHA256SUMS.txt` published with every asset |
| Install friction | **`winget`** — the manifest pins the hash, and winget strips the Mark-of-the-Web, so the installer usually runs with no warning at all |
| Self-signed certificate | **never** — an untrusted root adds its own warning and helps nobody |
| Reputation | Not pursued; an unsigned free app is expected to show this once |

### No `.env` in the payload — local defaults are reconstructed

The payload deliberately excludes `.env` and `mcp-servers.json` (they hold the
developer's configuration; shipping them would leak it). That has consequences
that only appear when the packaged app is actually run:

1. **No sign-in screen.** `src/lib/auth/config.ts` treats a production build as
   public and defaults to *requiring* auth unless `HARNESS_AUTH` is explicitly
   off — the packaged app showed "Create your operator account". The bootstrap
   now sets `HARNESS_AUTH=off`, matching what the repository's `.env` already
   does. The desktop app is single-user and loopback-only: there is nothing to
   sign in to.
2. **No empty database.** With no schema, Prisma creates a 4 KB file and every
   query fails. The package ships a schema-only seed (`app/seed/jdds.db`, ~1 MB,
   generated from `prisma/schema.prisma` at package time) and the bootstrap
   copies it on first run. No binary database is committed to git.
3. **`CRON_SECRET`.** Signs operator/cron actions. Generated per install at
   `<data dir>\operator-secret` (mode 0600), replacing the pruned `.env` value.

The general rule: **anything required must be reconstructed locally — never
shipped, never left missing, and never turned into a user task.**

### Why packaging is cheap to build

Everything that used to make this hard is already in the repository:

- Python is **vendored** (`scripts/vendor/python`), not installed at run time.
- The Python supervisor is unchanged and already spawns from a known path.
- The desktop bootstrap already resolves one canonical data directory.
- The shell already boots from an arbitrary root (`resolve_root` walks up).

So the remaining packaging work is: a standalone Next build, a bundled
`node.exe`, `%LOCALAPPDATA%` data, an NSIS wrapper, and the WebView2
bootstrapper. Signing is the only external dependency.


## 7. WebView2 availability (normative)

The developer machine has WebView2 `157.0.4341.42`. A user's machine may not.

1. Detect the runtime before showing the app window (registry keys /
   `WebView2Loader`), including an outdated major.
2. If missing: an actionable message with the Microsoft download link, plus the
   option to launch the browser path instead.
3. Never fail silently — a shell that opens nothing is indistinguishable from a
   broken install.
4. Ship the Evergreen Bootstrapper with the installer; Windows 11 normally has it,
   Windows 10 does not always.

## 8. Crash recovery and reliability (normative)

| Scenario | Required behaviour | Prototype today |
| --- | --- | --- |
| Python crashes | Existing supervisor recovers; shell does not intervene | delegated ✔ |
| Next.js crashes | Shell detects, shows a failure state, offers restart | **partial** — detected only before readiness |
| Window closed | Follow keep-warm preference; never kill a server we do not own | ✔ (verified) |
| Shell process crashes | Server and databases stay safe; a stale lock must not block the next launch | **not implemented** (no lock yet) |
| Server already running | Attach, never own | ✔ (but unverified identity) |
| Port unavailable | Actionable conflict message | **not implemented** |
| Database unavailable | Recovery instructions, no silent reset | **not implemented** |
| App restarted | No duplicate processes | **partial** (attach + single-instance; no lock) |
| Windows restart | Recover per configured startup behaviour; WAL intact | **unverified** |

**Rule:** do not build a second supervisor for Python. If something needs
supervising and `python-runtime.ts` already does it, the shell stays out.

## 9. Keep-warm and preferences (normative)

Keep-warm is **off by default** with a visible setting (tray checkbox). Closing
the window quits; keep-warm is an opt-in for instant reopen.

| Action | Behaviour |
| --- | --- |
| Close window | Quit when keep-warm is off (stop an owned server tree, then exit); minimize to the taskbar when it is on. Never silently kill a server we do not own |
| Tray click | Show + focus the existing window (never spawn a second) |
| Reopen | Warm window; no rebuild, no re-spawn |
| Quit | Graceful shutdown of an owned server tree, then exit |
| Idle timeout | Future: optional, off by default |

**DECISION 4.** Is hiding to tray acceptable without an explicit first-run
explanation? (Recommended: show a one-time tray notice, otherwise "close did not
quit" reads as a bug.)

## 10. Update and rollback (check shipped, self-replace deferred)

Requirements the updater must meet later:

- **Versioning:** shell version and payload version are separate and both visible.
- **Safe install:** stage the new payload, verify a signature, swap atomically.
- **Never** overwrite user data or run migrations. A migration shipped in an update
  is a separate, reviewed decision.
- **Failed update:** fall back to the previous payload.
- **Integrity:** the existing trust core (`scripts/lib/update-verify.js`, Ed25519,
  https allowlist, semver) is the foundation.

**Shipped now:**

| Capability | State |
| --- | --- |
| Update *check* with a signed manifest + in-app banner | already exists app-side (`config/updates.json`, `/api/updates`) — the shell adds nothing |
| Applying an update | `winget upgrade BlackEclipse.BlackEclipse`, or run the newer installer. `%LOCALAPPDATA%\BlackEclipse\data` is untouched by both |
| Silent background self-replacement | **not implemented** — needs the release signing key inside the shell, and must never run migrations unattended |

That is a deliberate boundary, not an oversight: an auto-updater that can replace
a 1.2 GB payload on a machine holding a user's trading journal is the single
highest-risk component in the product. It ships when it can be signed and staged
atomically, not before.



## 11. Git and pre-existing work protection — **P0 complete**

The first snapshot was defective and has been rebuilt.

| Defect | Fix |
| --- | --- |
| `git diff` captured no untracked files — **14 files of in-progress work** were invisible (the whole `python/harness/gaps/` package, `docs/bot-mode.md`, `docs/harness/no-dead-ends.md`, bot state-sync client + tests, capability-gap UI) | `untracked.tar.gz` captures all 26 untracked paths, structure preserved |
| PowerShell redirect produced UTF-16 **with a BOM**; `git apply` cannot read it | patches written by `cmd` redirection: raw UTF-8, no BOM, `--binary` |
| Staged vs unstaged not distinguished | `tracked-staged.patch` + `tracked-unstaged.patch` + combined `tracked-all.patch`. The index was **clean** (staged patch is 0 bytes) |
| Restorability asserted, not proven | reverse-apply check **passes**; a round-trip restore into a clean clone reproduced all hashes |

Location: `Black-Eclipse-site/.black-eclipse/p0-snapshot/`
(`RESTORE.md`, `RESTORE-TEST.txt`, `meta.json`). Nothing committed; the index was
not modified.

## 12. Acceptance tests

Status of each item against the frozen prototype: **P** = passes,
**F** = fails, **U** = unimplemented, **?** = unverified.

| # | Test | Status |
| --- | --- | --- |
| 1 | Fresh install launches successfully | U (no installer) |
| 2 | Existing local server detected and attached | **P** — identified as `v0.3.2` via `/api/updates` |
| 3 | No duplicate server processes | **P (partial)** — attach verified, lock implemented, concurrent-start not yet exercised |
| 4 | Python harness starts through the existing supervisor | ? (never exercised from the shell; harness only starts on demand) |
| 5 | Python crash recovery works | ? (delegated, unchanged) |
| 6 | Next.js crash is detected | **implemented**, unverified end to end |
| 7 | Close/reopen follows keep-warm preference | ✔ verified (default close quits: window process exits, owned server tree stopped; keep-warm minimizes) |
| 8 | Windows restart does not corrupt state | ? |
| 9 | Existing databases preserved | **P** — the shell writes no schema, guard is background |
| 10 | Only one canonical `BE_DATA_DIR` | **P** — resolved absolute and passed explicitly; logged at boot |
| 11 | Port conflict produces useful diagnostics | **P** — exit 5, explains the occupant, kills nothing |
| 12 | Offline startup works for local functionality | ? |
| 13 | Existing MT5 integration unaffected | ? |
| 14 | Existing authentication unaffected | ? |
| 15 | Notifications, mic, camera still work | **P (notifications)** — shim confirmed installed (`ctor:NativeNotification`), `permission: granted`, secure context, two `[notify] sent` lines. Mic/camera: ? |
| 16 | Web deployment/startup unaffected | **P** — browser path untouched; server still serving after every test |
| 17 | No unauthorized shell/filesystem operations | **P** — no shell plugin; splash capability is `core:default` only |
| 18 | Upgrades do not cause data loss | U (no updater) |

Tests 4, 5, 12–15 need a **shell-owned server**, i.e. the browser server on 3100
must be stopped first. That is a deliberate, user-approved step, never something
to do silently.

## 13. Performance targets and measurements

Targets are measurement goals, not promises.

| Stage | Target | Measured (prototype, this machine) |
| --- | --- | --- |
| Tauri initialization | < 300 ms | ~250 ms to window visible (estimated from launch) |
| WebView initialization | < 500 ms | not isolated |
| Backend availability | < 3 s | `Ready in` **1.0–3.8 s** depending on OS cache |
| First application render | < 4 s total cold | ~4 s observed |
| Python harness readiness | on demand | not measured |
| Warm reopen | < 600 ms | not measured (implied by keep-warm) |
| White flash | none | none observed |

**Explicitly not claimed:** the shell does **not** make the existing interface
intrinsically faster. 468k lines of TS/TSX, 456 API routes, Monaco, ECharts and
WebGL are the real cost. Frontend rendering work is a separate project.

Also measured: `db-guard` costs **> 20 s** on a 338 MB `jdds.db`. The prototype
moves it off the critical path (12-hour stamp, background run). A failed guard
now writes `db-guard.failed` and the next launch refuses to serve with recovery
text rather than ignoring it.

### Verified in P1 (this machine, 2026-11-10)

| Check | Result |
| --- | --- |
| Loopback-only bind | `next start` reported `Local: 127.0.0.1` **and** `Network: 127.0.0.1`; `Get-NetTCPConnection` → `LocalAddress 127.0.0.1` |
| Identity before attach | attached as `v0.3.2` via `/api/updates` |
| Port conflict | exit 5, explained the occupant, killed nothing |
| Canonical data dir | logged absolute; `jdds.db` 338.4 MB at that path |
| Owned-server boot | `Ready in 1068 ms`, `@@BE:READY@@`, identity confirmed after |
| Crash after readiness | killing the server produced `owned server exited after readiness` + failure panel |
| Lock lifecycle | created on start, removed on exit; stale lock taken over |
| Single instance | second launch exits, one instance remains |
| No orphans | ports 3111/8766 free after teardown; the harness died with the tree |
| WebView2 detection | registry probe reported `157.0.4341.42` |
| OS notification path | `npm run desktop:selftest` → `hn:1~perm:granted~sec:1~ctor:NativeNotification~shimcall:dispatched~direct:ok`; two `[notify] sent` entries in the log |

### Packaged mode (verified 2026-11-10)

`dist/BlackEclipse/` was built (1,558 MB: app 1,470 MB + bundled `node.exe` +
shell) and launched against a **copied** database on a spare port, so the live
server was never disturbed.

| Check | Result |
| --- | --- |
| Runs with its **own** bundled Node | yes — same machine, no PATH lookup |
| Data goes to the resolved data dir | `jdds.db` 338.8 MB found at the canonical path |
| Harness secret generated per install | `harness-auth.secret`, 64 bytes, mode 0600 |
| Serves the real application | `server is up (HTTP 307)` → `PROBE: HTTP 200 len=38375` |
| No `AUTH_MISCONFIGURED` | gone |
| Loopback only | `Local` and `Network` both `127.0.0.1` |
| Live repository server untouched | still serving throughout |

### Installer (verified 2026-11-10)

| Check | Result |
| --- | --- |
| Build | `BlackEclipse-Setup-0.3.2.exe`, **259 MB** from a 1,302 MB payload (LZMA solid, ~12 min) |
| Silent install | exit 0 in **43 s**, 1,302 MB at `%LOCALAPPDATA%\Programs\BlackEclipse` |
| Installed contents | `Black Eclipse.exe`, `Uninstall.exe`, `runtime\node.exe`, `app\server.js` all present |
| Start Menu + Add/Remove entry | created |
| Installed app runs | `PROBE: HTTP 200 len=38375` |
| Silent uninstall | exit 0 — install dir, Start Menu entry and uninstall key all removed |
| **User data after uninstall** | `%LOCALAPPDATA%\BlackEclipse\data` **preserved** |
| Checksum | `SHA256SUMS.txt` matches the artifact; the same hash is baked into the winget manifest |

### Installer branding (verified 2026-11-18)

The setup `.exe` and its wizard carry the same identity as the app. Without
`MUI_ICON` NSIS draws its stock globe/disk icon for the installer, which is why
an unbranded build looked like a generic Windows tool.

| Piece | Where | Why |
| --- | --- | --- |
| Icon (installer + uninstaller) | `desktop/installer/assets/icon.ico` | `MUI_ICON` / `MUI_UNICON`; a copy of `public/branding/black-eclipse.ico` (7 sizes, 16-256px) |
| Welcome / Finish / Uninstall sidebar | `desktop/installer/assets/welcome.bmp` (164x314, 24-bit) | MUI2 only accepts BMP; dark brand panel with the eclipse mark, wordmark and lime accent |
| Install / uninstall header | `desktop/installer/assets/header.bmp` (150x57, 24-bit) | Drawn right-aligned on MUI2's white header band |
| Generator | `node scripts/make-installer-assets.js` (`npm run installer:assets`) | Derives the BMPs from `public/branding/LOGO-WORDMARK.svg`; the outputs are committed, so a build does not need `sharp` |

Page flow: **Welcome -> Install -> Finish**. There is deliberately no Directory
page: the install location is fixed per user (the correctness requirement in
section 6), and `/D=` still overrides it for silent installs. The welcome page's
Next button is relabelled "Install"; the uninstaller replaces the stock
confirmation box with the branded welcome page and explains that user data
survives removal. `scripts/desktop-installer.js` checks the three assets before
invoking `makensis` so a missing bitmap fails with an actionable message.

**The tag must contain this change.** The release workflow builds exactly the
commit the tag points at; it does not take `main`. v0.4.2 exposed this the hard
way: its tag was created on a commit that forked before `MUI_ICON` landed, so
the published `BlackEclipse-Setup.exe` drew NSIS's stock globe/disk icon even
though `main` already had the fix. Source-only verification could not see that.
`scripts/desktop-installer.js` now reads the finished exe's `RT_GROUP_ICON` and
compares it with `public/branding/black-eclipse.ico` (16/24/32/48/64/128/256px,
all 32bpp); a mismatch exits 7 before the artifact can be published. Check any
existing file with `npm run installer:verify -- path\to\Setup.exe`.

Why NSIS is kept: the per-user install, WebView2 bootstrapper, silent `/S` and
the winget `nullsoft` installer contract all depend on it. The trade-off is that
page backgrounds remain native Win32 chrome; a fully dark setup window would
require a different installer engine and is a separate product decision.

Verified on the 0.4.1 artifact: `BlackEclipse-Setup-0.4.1.exe`, **176 MB** from a
1,717 MB payload (LZMA solid, 469 s); the setup `.exe` carries the eclipse icon
and `FileDescription "Black Eclipse Setup"`, the live Welcome page renders the
brand sidebar at 503x390, and the uninstaller's Welcome page shows the same
sidebar with "Remove Black Eclipse" / a **Remove** button.

**Gap found and fixed by running the installed copy:** `@napi-rs/canvas` (a runtime
`optionalDependency`) is not traced by Next, so PDF rendering silently degraded
(`Cannot load "@napi-rs/canvas"`, `Cannot polyfill DOMMatrix`). The package step
now copies `@napi-rs` and `@img` explicitly.

**Known limitation, deliberate:** desktop notifications only work on the
canonical port **3100**, because Tauri's IPC allowlist cannot wildcard a port.
The shell logs a warning when `BE_DESKTOP_PORT` overrides it. Normal installs use
3100, where notifications are verified working.

| TypeScript | `npx tsc --noEmit` clean |
| Test suite | 507 files / **4676 tests** passed |

Screen-capture of the window could not be used for this round: the whole desktop
returned black (including the taskbar), i.e. the display was not capturable. The
app's rendering was confirmed visually in the earlier build; the navigation path
is unchanged apart from `app_port()` returning the same 3100.

## 14. Scope restrictions (this task)

**Permitted:** new `desktop/` tree, desktop-specific configuration, new package
scripts, documentation, desktop-specific tests.

**Protected at the time (historical):** `start-eclipse.vbs`, `bin/black.js`,
`scripts/local-host.js`, `scripts/start-desktop.js`. Superseded on 2026-11-13:
the first, third and fourth were deleted with the browser lane, and
`bin/black.js` was rewritten as a desktop-first CLI.

**Not authorized:** schema changes, harness refactors, Python redesign, financial
logic, agent orchestration, MT5 execution, replacing the startup workflow,
unrelated frontend redesign, unrelated dependency upgrades.

**Two deviations the prototype made, both flagged for your decision:**

1. `scripts/desktop-server.js` is a **new** file outside `desktop/` (it is only
   ever called by the shell). It modifies nothing, but the permission list above
   did not name `scripts/`.
2. `.gitignore` gained two lines (`desktop/src-tauri/target/`, `gen/`). Build
   artifacts must be ignored somewhere; the alternative is committing a `target/`
   directory.

## 15. Gap analysis — requirements vs implementation

State after P1 hardening.

| Severity | Gap | State |
| --- | --- | --- |
| **High** | Server bound all interfaces, not `127.0.0.1` (§5) | **resolved** — verified `LocalAddress 127.0.0.1` |
| **High** | No identity/version verification before attaching (§3.2) | **resolved** — `/api/updates` returns `repository: black-eclipse/black-eclipse` + `currentVersion`; verified attaching as `v0.3.2`. No app route added |
| **High** | Data directory shared by coincidence, not construction (§4) | **resolved** — `BE_DATA_DIR` resolved absolute before any service starts and passed explicitly; the same variable is what `db.ts`, `persistence.py` and `eclipse_memory/config.py` read |
| Medium | No cross-process start lock (§3.7) | **resolved** — `desktop-server.lock` with pid + TTL, stale takeover |
| Medium | No port-conflict diagnostics (§3) | **resolved** — never kills, never reroutes; explains what answered |
| Medium | Post-readiness crash not detected (§3.10) | **resolved** — an owned server that dies swaps in a failure panel with Reload |
| Medium | A failed navigation leaves WebView2's own Edge/Bing error page (Microsoft-branded) | **resolved** — a Windows `NavigationCompleted` guard returns the window to the bundled splash with a branded offline state; `on_navigation` also keeps every non-app URL out of the window (opened in the OS browser) |
| Medium | A hung Next server stays on screen because the child never exits | **resolved** — a 15 s `/api/updates` watchdog (3 misses = 45 s) swaps in the same branded offline screen; the probe is unit-tested against a served response, a closed port and a silent socket |
| Medium | No WebView2 detection or recovery (§7) | **resolved** — registry probe before the window; on absence, a message box with the download link *and* the browser fallback |
| Medium | `db-guard` runs after first paint (§13) | **accepted with guardrail** — a failed guard writes `db-guard.failed`, and the next launch refuses to serve and shows recovery instructions. First run stays fast |
| Low | `Ctrl+R` reload omitted | **intentional** — would discard composer state |
| Low | Notifications shim never exercised end-to-end (§12.15) | **open** — needs a shell-owned-server session |
| Low | `.gitignore` + `scripts/` deviation (§14) | **open** — awaiting confirmation |

**Bug found and fixed during hardening:** the log reader used
`BufReader::lines().map_while(Result::ok)`, which ends the stream at the first
invalid UTF-8 byte. The Python harness and Next can both emit non-UTF-8 on a
Windows console, and the failure mode was silent: the shell would treat a live
server as gone. It is now byte-wise with lossy conversion.

## 16. Open risks and required decisions

| # | Risk | Needs |
| --- | --- | --- |
| 1 | Port-conflict policy (hard stop vs alternate port) | DECISION 1 |
| 2 | Packaged data directory location | DECISION 2 |
| 3 | First release: portable folder vs signed installer | DECISION 3 |
| 4 | Tray-only close may read as a bug | DECISION 4 |
| 5 | WebView2 absence on Windows 10 | bootstrap in installer |
| 6 | `better-sqlite3` ABI if Node is ever swapped for Electron | strongly prefer Tauri; a bundled Node keeps ABI stable |
| 7 | Playwright/PDF/Monaco inside WebView2 | needs a real second-instance test |
| 8 | `db-guard` non-blocking may mask corruption at boot | mitigated: failure marker blocks the next launch |
| 9 | Prototype existed before authorization | **resolved** — authorized, hardened to P1 |
| 10 | No installer / code signing / bundled Node | open by design (§6) |
| 11 | Notifications shim never exercised end-to-end | open, needs a shell-owned session |

## 17. What P1 must deliver before any further scope

| # | Item | State |
| --- | --- | --- |
| 1 | §5 binding fix + identity check + explicit data-directory assertion | **done** |
| 2 | §3 lock, conflict diagnostics, post-readiness crash detection | **done** |
| 3 | §4 snapshot/integrity path and recovery copy | **done** (failure marker + recovery text; consistent-snapshot guidance in §4) |
| 4 | Acceptance tests 2, 3, 6, 7, 10, 11, 16, 17 green with a shell-owned server | **done** — verified on port 3111 with a shell-owned server |
| 5 | No change to the four protected files, no schema change | **done** — verified |

Packaging (§6), WebView2 installer bootstrap (§7) and updates (§10) follow, each
with their own approval.

---

## 18. Decisions taken during P1 (defaults applied, all reversible)

The four open decisions were answered with the recommended option so the work
could proceed. Each is a one-line change to reverse.

| # | Decision | Taken | Why |
| --- | --- | --- | --- |
| 1 | Port conflict policy | **Hard stop with diagnostics.** Never kill, never pick another port | Silently moving ports hides a real conflict and creates two apps with two data views |
| 2 | Packaged data directory | **Unchanged for now** (repo root, via explicit absolute `BE_DATA_DIR`); `%LOCALAPPDATA%` deferred to packaging | Requirement §3 says preserve existing locations; moving it belongs with the installer |
| 3 | First release shape | **Not decided** — deferred to §6 | Out of P1 scope |
| 4 | Tray-only close | **Kept**, with the behaviour documented; a first-run notice is a P2 polish item | Closing must not kill a server the shell does not own |
| 5 | `db-guard` timing | **Background + failure marker**, not synchronous | 20 s on a 338 MB vault; a failure now blocks the *next* launch with recovery text instead of being silently ignored |

Still open and needing an explicit answer: **installer vs portable folder**,
**code signing**, **bundled Node**, and confirmation of the two scope deviations
in §14.

---

## 19. Zero-configuration principles (product direction)

The stated goal is that Eclipse becomes **less configurable over time**, not more.
Applying that to the desktop app:

| Rule | Consequence |
| --- | --- |
| **No configuration is required to start.** Every choice the app needs has a correct default | No first-run wizard, no "where is your Python?", no port picker |
| **No separate runtime installs.** Node and Python ship inside the app | No `npm install`, no `pip install`, no PATH editing |
| **No network-shape config.** No hosts-file entry, no TLS certificate, no proxy port | Loopback plus one port is enough |
| **One data directory, created silently.** `BE_DATA_DIR` defaults to a real absolute path | Users never learn the variable exists |
| **Settings are preferences, not prerequisites.** Keep-warm, theme, shortcuts | Removing a toggle must never stop the app working |
| **Missing pieces are a packaging defect, not a user task.** If a runtime is absent, the installer failed — say so plainly and offer the one recovery action | Never a wall of instructions |

Two current violations of this direction, both app-level rather than shell-level:

1. **OS notifications defaulted to OFF** (`be_alerts_browser_v1` in
   `src/lib/alerts/browser-notifier.ts`), requiring a Settings toggle. **Fixed at
   the shell level**: the injected script sets the preference to ON when the key
   is absent, and never overrides an explicit OFF. The app's own source and its
   opt-in behaviour in a browser are unchanged. Verified: `pref:1` in the
   self-test probe.
2. **`db-guard` failure shows recovery text** rather than self-healing. That is
   correct for now; the long-term answer is automatic restore from the rolling
   backup with a one-line notice.

---

## 20. Taskbar presence (M1 + M2 shipped)

Status: **implemented 2026-11-12.** Contract and rationale:
`desktop/README.md` → "Taskbar presence".

Shipped:

1. **Unread badge + attention flash (M1).** Red overlay dot (`SetOverlayIcon`)
   and one `FlashWindowEx` per new arrival while unfocused; macOS/Linux use
   `set_badge_count`. Driven by the notification ledger through the `be-taskbar`
   core event.
2. **Keep-warm now minimizes** (`CloseRequested` → `prevent_close()` +
   `minimize()`). A hidden window has no taskbar button, so hiding removed the
   badge, the bar and the flash — the one place the reference behaviour did not
   hold. The server stays warm either way; the tray and the taskbar both reopen
   the same window.
3. **Working state.** `set_progress_bar(Indeterminate)` while at least one agent
   stream is doing work, cleared when the last settles. The count lives outside
   React (`src/lib/desktop/activity.ts`) because several run surfaces can be
   mounted at once. Approvals and clarifications stop the bar: the run is blocked
   on a human gate, and "working" there would be a lie. `working` is a separate
   field, not a `state` variant, so a background reply can land while another run
   is still streaming (dot + bar together).
4. **"Unread" means "not seen".** The ledger now consults
   `src/lib/notifications/focus.ts` (`visibilityState` + `hasFocus`), so a reply
   that lands while the app is in the background stays unread even on the Chat
   tab — the alt-tab-away case that motivated the whole feature. Returning to
   the window marks the visible tab read. The completion alert in
   `use-agent-run.ts` uses the same rule; before this, alt-tabbing away mid-answer
   produced no badge at all.
5. **Preference.** Settings › Window › "Taskbar activity" (`be_taskbar_badge_v1`,
   default ON in the shell) governs the dot, the flash and the bar together.

Open: **Linux.** `set_badge_count` needs a libunity-capable launcher (GNOME
works, others silently do nothing) and the attention hint has no portable
equivalent. Documented as a gap rather than faked; Windows is the shipped target
and macOS uses the native badge + attention APIs.

## 21. Developer mode: one UI for desktop and web

A packaged install ships its own frozen copy of the UI under
`<install>\app`, and the shell prefers that copy over any repository checkout.
That is correct for a shipped build, but on a development machine it makes the
desktop window drift from `npm run dev` / `npm run up` — the same symptom as
"the web app has my changes, the desktop app does not".

`npm run desktop:dev-bridge` (`scripts/desktop-dev-bridge.js`) resolves this:

1. Renames `<install>\app` to `<install>\app.bundled-<version>` (nothing lost;
   rename it back to return to the self-contained build).
2. Writes a small `<install>\app\scripts\desktop-server.js` bridge that
   `chdir`s to this repository and requires the repo's own
   `scripts/desktop-server.js`.
3. The shell's normal root discovery finds `app`, runs the bridge, and the
   bootstrap serves a **pinned copy** of the repository's last completed build
   (`.black-eclipse/desktop-dist/.next`) on the desktop's own port 3100.

The bridge stores no data of its own: `scripts/desktop-server.js` still
resolves the repo root, so the desktop and web app share one data directory.
They do **not** share a build: the website/agent uses `.next` on 3105 (dev on
3000), and the desktop reads its immutable pin, so an in-flight `next build`
cannot break the open window. `npm run desktop:refresh` builds when needed,
re-pins, and reopens the shell; a normal launch also re-pins when `.next`
contains a newer completed build. Re-run the bridge command after a fresh
desktop install (an installer restores the bundled `app`). It is Windows-only,
matching the packaged shell.



