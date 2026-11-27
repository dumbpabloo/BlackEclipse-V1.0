---
title: Desktop Cross-Platform Plan
status: Proposed
last-verified: 2026-11-11
---

# Cross-platform plan

**Goal:** ship the desktop app on **Windows, macOS and Linux** from one codebase,
without forking the application or rewriting the UI.

## Status (2026-11-11)

| Phase | State |
| --- | --- |
| **C0** shell portability | **verified** — compiles for Linux in CI; six call sites guarded |
| **C1** Linux packaging | **verified** — `black-eclipse-linux` artifact (133 MB) produced by `ubuntu-latest` |
| **C2** macOS packaging | **verified** — `black-eclipse-macos` artifact (147 MB) produced by `macos-latest` |
| **C3** per-platform packs | **done** — manifest carries `target`; archives are per-platform |
| **C4** CI matrix | **verified** — run `43066805555`: **windows ✅ macOS ✅ linux ✅** |
| **C5** notarisation | **skeleton, gated on secrets** — `notarytool` + `stapler` skip cleanly without them |

### CI evidence (run 43084460874, commit 9180767) — builds **and runs**

| Runner | Result | Artifact | Runtime smoke |
| --- | --- | --- | --- |
| `windows-latest` | **success** | `black-eclipse-windows` | **PASS — HTTP 200 after 15 s** |
| `ubuntu-latest` | **success** | `black-eclipse-linux` | **PASS — HTTP 200 after 16 s** (installed `.deb`, Xvfb) |
| `macos-latest` | **success** | `black-eclipse-macos` | **PASS — HTTP 200 after 4 s** (`.app` binary) |

The smoke test installs/launches the real artifact, waits for the local server to
answer, and dumps the shell log on failure. All three answer.

Seven real defects were found by running it rather than reasoning about it:

1. **Node heap** — the Next build exceeded the default ~4 GB heap on all three
   runners; `NODE_OPTIONS=--max-old-space-size=6144`.
2. **`codesign --deep`** — descended into the payload and failed on
   `node_modules/next/dist/compiled/lodash.curry`.
3. **Python requirements missing** — `npm ci --ignore-scripts` skips the hook that
   installs `python/requirements.txt`, so four of five packs were empty.
4. **Offline NSIS edition hard-coded all five packs** — aborted where `winsdk`
   has no Linux wheel; the list is generated at build time.
5. **Flaky apt mirror** — `Acquire::Retries=5` plus three attempts.
6. **Runtime state in the install directory** — a system `.deb` lives under
   `/usr/lib` (root-owned), so the bootstrap could not create its lock directory
   and exited. Runtime state now lives beside the user data
   (`<data>/.runtime`), with a fallback in the shell.
7. **Incomplete build manifests** — Next's standalone output carries only a
   subset of the top-level manifests; the payload was missing
   `routes-manifest.json`, `prerender-manifest.json`, `export-marker.json`,
   `images-manifest.json`, `trace` and the `.nft.json` files. The assembler now
   copies every top-level file.

### What was actually verified on this machine (no Linux, no Docker, no WSL)

| Check | Result |
| --- | --- |
| Python-build-standalone pins (linux-x64, darwin-arm64, darwin-x64) | **HTTP 200** |
| Node archives (linux-x64, darwin-arm64, darwin-x64) | **HTTP 200** |
| Windows payload fetch | downloaded from nodejs.org, **SHASUMS256 verified** |
| `.deb` staging tree | packages, launcher, desktop entry, `DEBIAN/control`, correct version |
| `.app` staging tree | `Contents/MacOS/{binary,app,runtime}`, valid `Info.plist` keys |
| Workflow YAML | parses; jobs `build`+`release`; matrix windows/ubuntu/macos; 17 steps |
| `cargo check` (Windows) | clean |

**Still unverified, and only a real runner can settle it:** the shell actually
compiling against Linux's webkit2gtk, the vendored interpreter booting on macOS,
and `dpkg-deb`/`hdiutil` producing installable artifacts.

What changed in C0:

| Was | Now |
| --- | --- |
| `taskkill /T /F` only | Windows keeps `taskkill`; Unix starts the child in its own session (`setsid`) and signals the group, so the Next server and the Python harness still die together |
| `MessageBoxW` | `#[cfg(windows)]`; no cross-platform dialog needed because the only caller is the WebView2 check |
| WebView2 registry probe | Windows-only; macOS/Linux use the OS webview and log that no probe is needed |
| Failed-navigation branding | Windows subscribes to WebView2 `NavigationCompleted` (`webview2-com`) and returns to the bundled splash. The `on_navigation` / `on_new_window` origin policy is cross-platform; a WKWebView (`didFailProvisionalNavigation`) / WebKitGTK (`load-failed`) hook is a follow-up, and those webviews do not render a Microsoft-branded error page |
| `runtime/node.exe` | `runtime/node[.exe]` — the payload fetches the **platform's** Node from nodejs.org and verifies `SHASUMS256.txt` |
| `%LOCALAPPDATA%` | `%LOCALAPPDATA%` / `~/Library/Application Support` / `$XDG_DATA_HOME` |
| `%ProgramFiles%\nodejs` fallback | Windows-only; Unix falls back to `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin` |

New scripts:

```
scripts/desktop-runtime.js          platform Node, checksum-verified, cached
scripts/setup-portable-python.js    relocatable CPython for macOS/Linux
scripts/desktop-bundle.js           .deb (dpkg-deb) / .app+.dmg (hdiutil)
```

**Reality check up front:** the *application* is already portable — it is a Next
server plus a Python harness, both cross-platform. What is Windows-bound is the
**shell, the bundled runtimes, and the installer**. That is a few hundred lines of
Rust and some packaging scripts, not a rewrite.

---

## 1. What is already portable (measured 2026-11-11)

| Layer | Portable? | Evidence |
| --- | --- | --- |
| Next.js application | **yes** | plain Node; no OS calls in the server |
| Python harness | **almost** | only 5 Windows imports in the whole tree: `msvcrt` ×2, `win32` ×2, `MetaTrader5` ×1 |
| Tauri 2 shell | **framework yes, code no** | 6 Windows-only call sites, listed in §2 |
| Shared UI | **yes** | 468k lines of React, no OS coupling |
| Optional packs | **no** | wheels are per-platform (see §4) |

## 2. The six Windows-only spots in the shell

| # | Where | Windows call | Replacement |
| --- | --- | --- | --- |
| 1 | `kill_tree` | `taskkill /PID /T /F` | `#[cfg]`: taskkill on Windows; `libc::kill(-pgid, SIGKILL)` on Unix, or the `sysinfo` crate for all three |
| 2 | `message_box` | `MessageBoxW` (user32) | `tauri-plugin-dialog` — cross-platform, smaller than a Win32 shim |
| 3 | `webview2_version` | registry read | `#[cfg(windows)]` only. macOS uses WKWebView, Linux WebKitGTK — both OS-provided, nothing to detect |
| 4 | `find_node` | `%ProgramFiles%\nodejs` | bundled runtime is always used in a packaged install; the PATH fallback is dev-only |
| 5 | `creation_flags(CREATE_NO_WINDOW)` | Win32 flag | already `#[cfg(windows)]`; Unix needs no equivalent |
| 6 | data dir | `%LOCALAPPDATA%\BlackEclipse\data` | `app.path().app_data_dir()` → `~/Library/Application Support/…`, `~/.local/share/…` |

**Nothing else in the shell is OS-specific.** Tray, single-instance, keep-warm,
window bounds, the crash panel and the boot screen are already cross-platform.

## 3. Bundled runtimes, per platform

| Runtime | Windows today | macOS / Linux needed |
| --- | --- | --- |
| Node | `node.exe` copied from the build machine | download the platform binary in `desktop-package.js` (`.tar.gz` for darwin/linux) |
| Python | `setup-embedded-python.js` provisions an embedded interpreter | same approach, different archive, or a venv created at package time |
| Vendored `.so`/`.dylib` deps | win32 wheels | per-platform wheels — the pack build must run **on** the target OS |

**Consequence:** the payload cannot be cross-built. Each platform's installer must
be produced on that platform (or in a matching CI runner).

## 4. Optional packs are per-platform

`playwright`, `scipy`, `xgboost`, `pymupdf`, `winsdk` all ship compiled wheels.
The pack archives must be built per target triple:

```
packs/darwin-arm64/ml.zip
packs/linux-x64/ml.zip
packs/win32-x64/ml.zip
```

`manifest.json` gains a `target` field; the downloader requests the matching one.

**Windows-only pack:** `computer` (`winsdk`). On macOS it would need
`pyobjc`/Accessibility APIs; on Linux, AT-SPI. Ship it **Windows-only** and mark
the feature unavailable elsewhere — do not pretend.

## 5. Packaging and distribution

| Platform | Artifact | Signing | Cost |
| --- | --- | --- | --- |
| Windows | NSIS `.exe` (done) | none (accepted) | $0 |
| **Linux** | `.deb` + `.AppImage` | none needed | **$0** |
| **macOS** | `.dmg` (arm64 + x64) | ad-hoc for dev; **notarisation for users** | **$99/yr** Apple Developer |

**macOS warning, plainly:** without an Apple Developer account, a downloaded `.dmg`
is blocked by Gatekeeper with "cannot be opened because the developer cannot be
verified". Users must right-click → Open, or `xattr -dr com.apple.quarantine`.
That is a much harsher wall than Windows' SmartScreen. There is no free
equivalent of "run anyway" on a modern Mac.

## 6. Phases

| Phase | Work | Outcome | Effort |
| --- | --- | --- | --- |
| **C0** | Replace the 6 Windows call sites; add `#[cfg]` guards; move data dir to `app_data_dir()` | shell compiles for all three targets | 1 session |
| **C1** | **Linux first** — `.deb` + AppImage, per-platform Node, venv-based Python | first non-Windows build; free, no signing | 2–3 sessions |
| **C2** | macOS arm64 `.dmg`, ad-hoc signed for local use | Mac users can run it (right-click to open) | 2–3 sessions |
| **C3** | Per-platform pack builds + `target` in the manifest | packs work everywhere they make sense | 1–2 sessions |
| **C4** | CI matrix (`windows-latest`, `ubuntu-latest`, `macos-latest`) building all artifacts | one tag → all releases | 1–2 sessions |
| **C5** | Apple Developer + notarisation | Mac download opens normally | $99/yr + 1 session |

**Why Linux first:** it costs nothing, it has no gatekeeper, and it is the fastest
way to prove the shell port is correct before paying Apple.

## 7. Feature availability matrix (honest)

| Feature | Windows | macOS | Linux |
| --- | --- | --- | --- |
| App, chat, agents, charts | ✅ | ✅ | ✅ |
| OS notifications | ✅ | ✅ | ✅ (libnotify) |
| Tray | ✅ | ✅ (menu bar) | ✅ (needs libappindicator) |
| Browser automation pack | ✅ | ✅ | ✅ |
| ML / documents / charts packs | ✅ | ✅ | ✅ |
| Computer control (OCR, UI automation) | ✅ | ❌ | ❌ |
| **MT5 integration** | ✅ | ❌ | ❌ |

MT5 and computer-control are **permanently Windows-only** — MetaTrader does not
exist on macOS/Linux. Everything else is portable.

## 8. Risks

| Risk | Handling |
| --- | --- |
| WebKitGTK (Linux) is older than Chromium | test the heavy screens (Monaco, ECharts, WebGL) early in C1; fall back to `softwarerendering` if needed |
| macOS notarisation fee | accepted cost, deferred to C5; C2 ships an unsigned build with instructions |
| Per-platform Python wheels missing for a package | the pack build fails loudly per target; drop that pack for that OS rather than ship a broken import |
| Three installers to test | one acceptance checklist (install → run → first-run DB → uninstall keeps data) per platform |
| Divergence over time | CI builds all three from the same tag; a platform that stops building is visible immediately |

## 9. Decisions needed

1. **Order:** Linux first (recommended, free) or macOS first (needs $99)?
2. **macOS without notarisation** — acceptable for v1 with right-click instructions, or wait until C5?
3. **Computer-control pack** — Windows-only forever, or invest in `pyobjc`/AT-SPI later?
4. **Universal macOS build** (arm64 + x64 in one `.dmg`, ~2× size) or separate downloads?

## 10. What does **not** change

- The application code, the UI, the harness logic, the data model
- The Windows release already shipped; C0–C1 must not regress it
- The browser front door stays cross-platform at zero cost, as it is today
