---
title: Install
status: Current
last-verified: 2026-11-21
---

# Install

**Who this is for:** someone who wants the app running, and anyone changing how
it is delivered. For running a checkout see [`QUICKSTART.md`](./QUICKSTART.md).

There are two install lanes and they produce the same app:

| Lane | Command | Requires |
|---|---|---|
| **Installer / one command** | the download button, or the scripts below | nothing |
| **Source (contributor)** | `npm install` then `npm run desktop:refresh` | Node 20+, the installed shell or Rust |

The reverse ordering matters: a source checkout does **not** replace the
installer. The window is a compiled Rust shell, and `desktop:refresh` makes the
installed shell run your checkout (`scripts/desktop-dev-bridge.js`). Only
`desktop:dev` builds a shell from scratch, and that needs `cargo`.

## One-command install

These scripts download the **published release artifact** - the same bytes the
README buttons serve - verify it, and install it. They never compile anything
and never read the git tree, so they are not a source install.

```powershell
# Windows
irm https://raw.githubusercontent.com/dumbpabloo/black-eclipse/main/Black-Eclipse-site/scripts/install-release.ps1 -OutFile install-release.ps1
./install-release.ps1
```

Files: [`scripts/install-release.ps1`](../scripts/install-release.ps1),
[`scripts/install-release.sh`](../scripts/install-release.sh). The `.sh` script
still handles Linux/macOS, but **those builds are not published yet**, so there is
no advertised one-command install for them — see
[macOS and Linux](#macos-and-linux-not-published-yet).

| Flag (`.ps1` / `.sh`) | Effect |
|---|---|
| `-Version <tag>` / `--version <tag>` | Install a specific release instead of the latest |
| `-Sha256 <hex>` / `--sha256 <hex>` | Pin the exact digest; a mismatch aborts |
| `-Full` | Windows only: the offline edition with the Python feature packs |
| `-NoLaunch` / `--no-launch` | Install without starting the app |
| `-DryRun` / `--dry-run` | Print the resolved release and target, download nothing |
| `-Repo <owner/name>` / `--repo <owner/name>` | Override the release repository |

Windows installs per-user to `%LOCALAPPDATA%\Programs\BlackEclipse` (no
administrator prompt). Linux installs the `.deb` through `apt-get`/`dpkg`, and
macOS copies `Black Eclipse.app` to `/Applications` — both paths work in the
scripts but are dormant until those artifacts are published.

The Windows setup window reports a few **coarse phases** — removing the previous
version, installing application files, offline packs — instead of one line per
file. The file-by-file list still exists behind **Show details**, and it is long
by nature: the payload is roughly 100,000 files (the Next server, `node_modules`
and the vendored Python tree). An install or update therefore takes minutes and
the progress bar is the honest signal; it is not stuck.

## What the scripts verify - and what they do not

The builds are unsigned, so there is no OS-level signature to check. What the
scripts do:

- refuse anything but `https://github.com/<repo>/releases/...`;
- download the package and compare its SHA-256 with `SHA256SUMS.txt` from the
  same release, aborting on a mismatch;
- print the computed digest unconditionally, so it can be compared with the
  release page by hand;
- honour `--sha256` as an out-of-band pin that wins over everything else.

What they do **not** do: prove the release was published by the maintainers. A
checksum served from the same host protects against a corrupted or truncated
download, not against that host being compromised. Authenticity is a separate,
stronger chain: the Ed25519-signed update manifest that the app itself verifies
(see [`UPDATES.md`](./UPDATES.md)). That manifest is published but not yet
enabled (`config/updates.json` `published: false`), so no install path claims
signature verification today.

Windows is the only platform that currently publishes `SHA256SUMS.txt`; the
scripts say so and continue with the printed digest when it is absent.

## After install

The app opens offline, seeds its local vault and provisions the agent runtime on
first boot. Add a hosted model key in **Settings > AI**, or point it at Ollama.
Data lives in the platform data directory and survives uninstall.

### Optional components (feature packs)

The slim installer ships the **core** only. Five Python capabilities — browser
automation, local ML, document reading, desktop control and advanced charting —
are separate archives that are downloaded from the same release as the app.

Open **Settings → Data & Storage → Feature packs** and press **Download** on
anything marked not installed. The panel shows real received/total bytes, and
nothing is written until the archive matches the SHA-256 recorded when the app
was built. A pack becomes usable as soon as its files land — no restart. Use
**Remove** to reclaim the space; it only disables that capability.

If the panel says this build has no feature-pack manifest, you are running from
a source checkout (packs are installed only by the packaged app), or the build
was produced before `config/packs.json` existed — use the offline **`-full`**
installer, which already contains every pack.

## Uninstall

- Windows: **Settings > Apps > Black Eclipse**, or `Uninstall.exe` in the
  install directory.
- Linux: `sudo apt remove blackeclipse`
- macOS: drag `/Applications/Black Eclipse.app` to the Trash.

Removing the app does not delete your data directory; delete it yourself if you
want a truly clean slate.

## macOS and Linux (not published yet)

The release workflow still produces both — a `.dmg` (Apple silicon) and a `.deb`
(amd64) — but neither is published or advertised: only Windows has a download
button and a release asset. That is a scope decision, not a missing build.

**Turning macOS on later:**

1. Join the Apple Developer Program and add `APPLE_ID`, `APPLE_TEAM_ID` and
   `APPLE_APP_PASSWORD` as repository secrets. The workflow's notarisation step
   (C5) is already written and stays skipped until they exist. An unsigned
   `.dmg` still installs, but Gatekeeper blocks the first launch until the user
   right-clicks → **Open** (or runs
   `xattr -dr com.apple.quarantine "/Applications/Black Eclipse.app"`) — a poor
   first impression for a fund or a home user.
2. Run the workflow by hand: `tag: vX.Y.Z`, `only: macos-latest`,
   `upload_release: true`. It attaches the `.dmg` to the same release, without
   re-drafting or unpublishing it.
3. Restore the macOS button in `README.md`
   (`releases/latest/download/BlackEclipse-mac-arm64.dmg`), and re-add its row to
   the signing/checksum table.

Linux is the same minus notarisation: run `only: ubuntu-latest` and restore the
button.

**How those users update later.** Exactly like Windows today: a packaged install
has no git checkout, so the in-app updater cannot rebuild it — they install the
new `.dmg`/`.deb`. The release page is the download page; re-running the backfill
is what publishes the new build. True in-app updates for packaged installs on
every platform would mean adopting Tauri's updater plugin — a separate, larger
change.

## Changing the delivery path

Probe the buttons before announcing a release:

```bash
cd Black-Eclipse-site
npm run check:downloads                          # latest
npm run check:downloads -- --version 0.4.0       # a specific tag
npm run check:downloads -- --repo owner/name     # a fork or a rename
```

A bare 404 hides three different faults, and the probe separates them: the
repository is not publicly readable (a private repo 404s anonymously and serves
no release files), no published release is visible (the workflow creates
**drafts**, and `/releases/latest` ignores both drafts and prereleases, so green
CI does not mean a working button), or the asset name changed. It needs the
network, so it is deliberately not part of `npm run check:all`.

The artifact names are a contract: the README buttons and these scripts resolve
`BlackEclipse-Setup.exe`, `BlackEclipse-linux-amd64.deb` and
`BlackEclipse-mac-arm64.dmg` through `releases/latest/download/`. Rename one and
both the buttons and the scripts 404. The build side is
[`../.github/workflows/desktop-release.yml`](../../.github/workflows/desktop-release.yml);
`scripts/desktop-installer.js` (NSIS), `scripts/desktop-bundle.js` (`dpkg-deb`,
`hdiutil`) and `scripts/desktop-payload.js` produce the payload.

