---
title: Updates & Release Signing
status: Current
last-verified: 2026-11-23
---

# Updates & Release Signing

**Who this is for:** anyone publishing a Black Eclipse release, or changing how
the app detects and applies one.

Eclipse is **self-hosted**: nothing runs on our servers. Most users take the
signed release installer, or the one-command scripts in
[`install.md`](./install.md); contributors run a checkout. "There is an update"
therefore means *a newer signed release exists*, and applying it means *install
that release* (a checkout additionally tags, builds and re-pins). This document
covers the whole path — publishing, notifying, and updating safely — plus the
key handling that keeps a compromised account from turning into a malicious
update.

## The trust chain

```
 offline private key
        │  signs
        ▼
 release/stable.json  ──upload──▶  GitHub Release asset (public, static)
        │                                   │
        │                                   │ anonymous HTTPS GET
        │                                   ▼
        │                          /api/updates (local Next server)
        │                                   │  verifies with
        │                                   ▼
        │                    config/update-signing-key.pub (committed)
        ▼
 black update  ── verifies commit SHA ──▶ git checkout vX.Y.Z ──▶ npm ci ──▶ build
```

Invariants — do not relax these without updating this file:

1. **The manifest is only trusted after a signature verifies.** The public key
   is committed; the private key never is.
2. **Manifest and every URL inside it must be `https:` on an allowlisted host**
   (`config/updates.json` → `allowedHosts`). A signed payload is still remote
   input, so its URLs are re-checked before the UI renders them.
3. **Nothing fetched is executed on a check.** The check is a read. `black update`
   moves to a git tag whose commit SHA equals the signed manifest's `commit`; it
   never runs a downloaded binary. The packaged updater opens an installer only
   after the downloaded bytes match the `sha256`/`size` in the signed manifest —
   a mismatch deletes the file and installs nothing.
4. **A failed check is explicit.** The API returns `ok: false`. The banner stays
   quiet; Settings says "couldn't check". Never render "up to date" when the
   answer is unknown.

### The wire format

```json
{
  "schema": 1,
  "payload": "<base64url of the exact JSON bytes>",
  "signature": "<base64url Ed25519 signature over those bytes>"
}
```

The signed payload:

```json
{
  "schema": 1,
  "channel": "stable",
  "version": "0.4.1",
  "publishedAt": "2026-11-09T12:00:00.000Z",
  "commit": "<40-char git SHA of the release tag>",
  "notesUrl": "https://github.com/<org>/<repo>/releases/tag/v0.4.0",
  "downloadUrl": "https://github.com/<org>/<repo>/archive/refs/tags/v0.4.0.zip",
  "minSupported": "0.3.0"
}
```

Signing the exact payload bytes (rather than re-canonicalising JSON) removes all
key-order/whitespace ambiguity: change one byte and verification fails.

## File map

| Concern | File |
|---|---|
| Wire format, Ed25519 verify, semver, allowlist | `scripts/lib/update-verify.js` |
| Prebuilt build: download, verify, extract, validate | `scripts/lib/build-artifact.js` |
| Produce the release build archive (`npm run release:build`) | `scripts/release-build-zip.js` |
| Checkout-vs-packaged detection (shared by runner) | `scripts/lib/git-root.js` (`findGitRoot`, `isSourceCheckout`) |
| Server mirror used by the route | `src/lib/updates/core.ts` |
| Shared types (client-safe) | `src/lib/updates/types.ts` |
| Repo slug, manifest URLs, allowed hosts | `config/updates.json` |
| Committed public key / key id `3e5f4f12096f06ac` | `config/update-signing-key.pub` |
| Generate / print fingerprint | `scripts/update-keys.js` (`npm run update:keys`) |
| Sign a release manifest | `scripts/update-sign.js` (`npm run update:sign`) |
| `black update` / `npm run update` | `scripts/update.js`, wired in `bin/black.js` |
| Local status endpoint | `src/app/api/updates/route.ts` |
| In-app notice | `src/components/layout/update-banner.tsx` (mounted in `src/app/layout.tsx`) |
| Title-bar update button + panel | `src/components/layout/update-button.tsx` (mounted by `desktop-tab-bar.tsx`) |
| Update status hook (shared) | `src/lib/updates/use-update-status.ts` |
| Stage an update in-app | `scripts/update-apply.js` (git checkout+build for a source tree; signed-installer download for a packaged install) (spawned by the shell via `be-update`, or by `src/app/api/updates/apply/route.ts` as a fallback) |
| Dependency install (staged + atomic swap, never empties the live tree) | `scripts/lib/deps-install.js` (tests in `src/lib/updates/deps-install.test.ts`) |
| Staged-job reader (spent-job filtering) | `src/lib/updates/job.ts` (tests in `job.test.ts`) |
| Restart into the staged build | `src/lib/desktop/update-bridge.ts` (Tauri event `be-update`) → `main.rs` listener |
| Dismiss/skip/enable prefs | `src/lib/updates/prefs.ts` (`be_update_prefs_v1`) |
| Settings UI | `src/components/screens/general-settings.tsx` → General › Updates |
| Tests | `src/lib/updates/core.test.ts` |

## Checkout detection (never test `<app>/.git`)

The app lives in `Black-Eclipse-site/`, but the repository root is its **parent**;
`Black-Eclipse-site/.git` does not exist. In-app updating therefore has to answer
two questions: *is this a git work tree?* and *is this a packaged payload?* Both
answers come from one place — `scripts/lib/git-root.js` (`findGitRoot`,
`isSourceCheckout`). `src/lib/updates/core.ts` mirrors it because the Next server
cannot import the CommonJS helper; the two must agree (a test asserts they do).

Detection walks up to the nearest `.git` **file or directory**, and a payload
root containing `server.js` is always packaged — even when some unrelated
repository sits above the install directory.

**Why this is written down.** A bare `existsSync(<app>/.git)` test was shipped
once: every clone (and every CI checkout) has the `.git` one level up, so
`/api/updates` reported `canSelfUpdate: false` and `POST /api/updates/apply`
answered *"This installed build cannot update in place"* — the window could not
download an update at all. `src/lib/updates/core.test.ts` now pins the
parent-root, packaged-marker and real-layout cases for **both** implementations.
Do not replace the helper with a direct `.git` check.

## Release state (current)

Updates are **published**: the repository is
`dumbpabloo/BlackEclipse-V1.0` (public) and `config/updates.json` has
`"published": true`. A live check verifies the newest signed manifest and offers
it as soon as it is newer than the running build.

**v0.4.27 is published** (2026-11-23, signed with key id `47e545f302249385`) and
carries both `builds` (`any` — `BlackEclipse-Build-0.4.27.zip`, 80,021,739 bytes, buildId
`qNUlhzNwPDc-CUqdH8B2l`, lockHash `8a0da292…`) and `installers` (`win32-x64` —
`BlackEclipse-Setup-0.4.27.exe`, 195,084,048 bytes, sha256 `9c4acc15…`), so a checkout
updates by download + verified promotion and a packaged install by the signed installer.
The public `stable.json` verifies against the committed key and
`node scripts/update.js --check` reports `latest signed release: v0.4.27`.
**Every later release must sign `--build-*`** (see *Release builds* and
*Publishing a release*); the release workflow builds and attaches the archive
but never holds the signing key, so **sign and upload `stable.json` while the
release is still a draft**. Publish without it and
`releases/latest/download/stable.json` 404s for every
user (order: *sign → upload → publish*).

**Signing key id `47e545f302249385`.** The previous key
(`3e5f4f12096f06ac`) was retired on 2026-11-21 because its private half was
unavailable, so nothing could be signed with it. Rotating leaves a one-time
scar: a build verifies manifests against the `.pub` it shipped with, so anyone
on **0.4.20 or older** cannot verify a manifest signed by the new key. They keep
running, get no notice, and rejoin the update path by installing 0.4.21 manually
once; 0.4.21 and later ship the new key and update normally.

Every release asset gap is visible from outside, so check it after publishing:
`node scripts/update.js --check` prints `HTTP 404` when the **latest** release
carries no `stable.json` (that was the state of v0.4.2 and v0.4.3: the manifest
was only ever on v0.4.1, and `releases/latest/download/stable.json` therefore
404'd for every user). The workflow asks for a **draft** and never signs a
manifest, but it can still end up published (v0.4.13 and v0.4.14 did): confirm
`draft` and which tag `releases/latest` resolves to before and after uploading
the manifest — a published release without one is the 404 state above. The
release step also goes red on `Not Found: update-a-release-asset` because each
platform artifact carries a `packs/manifest.json` and only one can win; that
failure does not withhold the installers or the release.

`published: false` remains the correct setting only for a private pre-launch
repo. While it is false:

- the local server makes **no network request** — it cannot learn anything, and
  users see no notice;
- Settings › General › Updates reports **"Updates are not published yet."**;
- `black update` says the same and changes nothing.

Do **not** flip it to true before a manifest is actually hosted: every check
would fail with "Could not reach the update service", which is worse than an
honest "not published yet".

Because the manifest is a release asset, it must be re-signed and re-uploaded for
**every** release. The current published one describes v0.4.27 - bump, re-sign,
re-upload, then publish the draft, in that order.

> Keeping the **source** private forever means there is nothing for `black update`
> to fetch. The git-based updater assumes users can reach the repo. If you later
> decide to ship packaged builds instead, that needs a separate download+verify
> path, not a config flag.

## One-time maintainer setup

1. **Publish under an organization, not a personal account.** Set
   `repository` in `config/updates.json` to `<org>/<repo>`. If the repo is not
   `black-eclipse/black-eclipse`, update it before the first release.
2. **Generate the keypair** — `npm run update:keys` — then **move
   `.black-eclipse/update-keys/update-signing-key.pem` offline** (encrypted USB,
   hardware token, or an offline machine).
   - Never commit it. `*.pem` is already gitignored; keep it that way.
   - Never put it in GitHub Actions secrets. A CI-held key can be used by anyone
     who compromises the account, which defeats the whole scheme.
3. **Commit `config/update-signing-key.pub`** in a normal reviewed release.
4. Verify the fingerprint matches what you published:
   `node scripts/update-keys.js fingerprint`.

## Publishing a release

```bash
# 1. Bump the version and commit normally
npm version 0.4.0 --no-git-tag-version
git add package.json package-lock.json && git commit -m "release: v0.4.0"

# 2. Tag and push
git tag v0.4.0
git push origin main --tags

# The tag must be on the commit you mean to ship. desktop-release.yml checks
# out the tag, not main: v0.4.2 was tagged on a branch that forked before the
# installer-branding commit, so its published setup still drew NSIS's stock
# disk. desktop:installer now verifies the finished exe's embedded icon and
# fails the release on a mismatch (`npm run installer:verify -- <setup.exe>`).

# 3. Create the GitHub release (source archive is automatic)
gh release create v0.4.0 --title "v0.4.0" --notes-file docs/releases/v0.4.0.md

# 4. Sign the manifest LOCALLY (offline key), then upload it
node scripts/update-sign.js \
  --version 0.4.0 \
  --channel stable \
  --commit "$(git rev-parse v0.4.0^{commit})" \
  --min-supported 0.3.0 \
  --installer-file dist/BlackEclipse-Setup.exe \
  --installer-url https://github.com/<org>/<repo>/releases/download/v0.4.0/BlackEclipse-Setup.exe \
  --installer-platform win32-x64 \
  --build-file dist/BlackEclipse-Build-0.4.0.zip \
  --build-url https://github.com/<org>/<repo>/releases/download/v0.4.0/BlackEclipse-Build-0.4.0.zip \
  --build-id "$(node -p "require('./.next/BUILD_ID').trim()")" \
  --build-lock-hash "$(node -e "console.log(require('crypto').createHash('sha256').update(require('fs').readFileSync('package-lock.json')).digest('hex'))")" \
  --out release/stable.json --force
gh release upload v0.4.0 release/stable.json --clobber

# The release-build job already attached BlackEclipse-Build-0.4.0.zip. If you
# are publishing by hand, create it with `npm run release:build`, attach it,
# then sign — the digest must describe the file that is actually uploaded.
```

Without `--build-*` the manifest still works: clients fall back to the
checkout + build path. Signing the build is what makes updates work from a dirty
checkout without a compile, so a release that forgets it silently degrades to
the slow, fragile path. `npm run release:build` prints every value the flags
need.

`scripts/update-sign.js` self-verifies against the committed public key before
writing the file: a manifest users cannot verify is worse than no manifest.

No `gh` CLI? The same two steps work in the browser: publish the GitHub release
from the tag, then **Edit release → Attach binaries** and upload
`release/stable.json`. The asset **must** be named exactly `stable.json`, because
the manifest URL is `releases/latest/download/stable.json`.

The app reads `stable.json` from the release's stable asset URL
(`releases/latest/download/stable.json`). For the **beta** channel, publish the
manifest on a `beta` release/tag and use the `beta` URL in
`config/updates.json`; the wire format is identical.

### Windows-first tags

A tag push builds **Windows only**, so the draft release is ready in ~15 minutes
instead of waiting for the slowest runner. Sign and publish it as usual, then
attach the other platforms by running the workflow again:

| Run | Inputs | Result |
|---|---|---|
| Tag push | — | Windows installer + `SHA256SUMS.txt`, as a draft |
| Backfill | `tag: v0.4.16`, `only: macos-latest`, `upload_release: true` | `.dmg` attached to the same release |
| Backfill | `tag: v0.4.16`, `only: ubuntu-latest`, `upload_release: true` | `.deb` attached |
| Re-tag | `tag: v0.4.16`, `upload_release: true` (blank `only`) | Windows only — name an OS for anything else |

A backfill preserves the release's existing state — `action-gh-release` updates
with `draft: existingRelease.draft`, so it never re-drafts or unpublishes what
you already shipped. macOS and Linux are not advertised (no README button and no
link in the release body) until you decide to publish them; verify a backfill
with `npm run check:downloads -- --all`.

### Iterating on a release build

A tag push builds Windows only; a manual run builds one named OS. While
iterating, run the workflow by hand (`workflow_dispatch`) instead:

- `tag: v0.4.16` names the release and checks out exactly that commit. A
  dispatch otherwise builds the branch, and a release built from a branch is not
  the commit the signed manifest points at — so set it whenever
  `upload_release` is on.
- `only: windows-latest` (or `ubuntu-latest` / `macos-latest`) builds a single
  platform. This now actually filters the matrix — it used to be accepted and
  ignored, so the "fast lane" still built all three.
- `offline_installer: false` (the default for a manual run) skips the `-full`
  Windows installer, whose pack compression is the slowest step in that job. A
  tag push always builds it.
- `upload_release: true` creates the draft release as well; leave it off for
  artifacts only.
- `smoke_tests: false` skips the post-build smoke test. A tag push always runs
  it; use it only to measure a build you are still iterating on.
- `publish: true` publishes the release instead of leaving it a draft. Use it to
  promote a finished draft.
- Upload `stable.json` right after publishing — the update check polls
  `releases/latest/download/stable.json`, and a published release without it
  answers 404.

The Tauri shell's release profile is tuned for build time (`lto = "thin"`,
parallel codegen units) because the shell is a thin WebView wrapper and is
rebuilt on every release. The Rust toolchain is pinned in the workflow for the
same reason: a floating `stable` silently invalidates the cached `target` dir.

**Why a new installer is still the update path for packaged users.** The in-app
updater checks out the signed tag and rebuilds it, which needs a git checkout
and `node_modules`. A packaged install ships only a compiled payload, so it has
neither: an installed user receives a fix by installing the new build. That is
why a Windows-first tag is the fast path, not the manifest alone.

The workflow also caches the vendored Python runtime (keyed on the requirement
files) and the pip downloads per OS, restores the Rust `target` directory even
when `Cargo.lock` changes, and no longer installs the Python stack twice on
macOS/Linux: `scripts/setup-python.js` now targets the vendored interpreter on
every platform, not only Windows.

## How users are notified

- `UpdateBanner` checks ~2 s after boot, then every 10 minutes, and revalidates
  when the window regains focus or the network returns (throttled). A new signed
  release also raises a one-time toast per version, so it is not hidden behind
  the title-bar dot.
- The local server fetches the public manifest (20 s timeout — GitHub answers
  through a redirect and a cold connection has measured 6.4 s), verifies it, and
  compares versions. The result is cached in-process for **5 minutes** (a release
  published just after a check must not stay invisible for hours). `?refresh=1`
  (Settings › Check now, and the panel's refresh) bypasses the cache; the client
  interval plus focus checks make a short TTL timely without hammering GitHub.
- The banner appears only when the manifest verifies and the version is newer.
  It **acts**, it does not just announce: **Update now** runs the same staged
  update as the title-bar panel (one shared hook, `use-update-apply.ts`), shows
  the job's phase/message, and offers **Restart Eclipse** once the build is
  staged. **What's new**, **Skip this version**, dismiss, and a "Use a terminal"
  fallback for `black update` remain. A packaged install with no shell bridge
  gets **Download vX** instead.
- A staged-but-not-restarted build is recognised, not re-downloaded: if the
  promoted release is already newer than the running one, the runner answers
  "Restart Eclipse to switch to it" instead of staging the same release again —
  the "it keeps telling me to update" loop.
- Settings › General › Updates has the on/off toggle, a manual **Check now**,
  and honest result text (up to date / available / could not check).
- The title-bar download button shows the same answer with a dot, and stages the
  release — see [In-app updates](#in-app-updates-title-bar-download-button).
- Prefs are local only (`be_update_prefs_v1`). There is no install ID, no
  account, no analytics. Turning the check off means the banner never calls the
  route. The upstream request is an anonymous GET for a public file.

## In-app updates (title-bar download button)

The signed-release check is also exposed as a **download button in the window
chrome**, immediately left of Minimize, with a dot when a newer release exists.
It opens a glass panel that reports the check honestly and can act.

**Two install shapes, two update paths.** `canSelfUpdate` on `/api/updates` is
true only when the running app has a git checkout (`<root>/.git`). A **packaged
install** — the normal user's app — ships a frozen payload under
`<install>\app` and has no repository, so the git-based updater cannot run
there. It updates with the **signed installer** instead: the verified manifest
carries `installers["<platform>-<arch>"]` (`url`, `sha256`, optional `size`), the
server exposes this host's entry as `installer` on `/api/updates`, and *Update
now* asks the desktop shell to run `scripts/update-apply.js` in installer mode —
download to the data directory, verify the SHA-256 and size, then open the
installer. The panel offers the same verified URL as a **Download vX** link when
the window has no shell bridge. `POST /api/updates/apply` still refuses a
packaged install (409): a runner spawned by the server would die with the server
during the installer handoff, so the shell path is the one that works. In-app
staging below applies to **checkouts**; a packaged install takes the installer
branch:

1. **Stage.** *Update now* asks the shell (Tauri event `be-update`,
   `{action:"apply"}`) to spawn `scripts/update-apply.js`; the shell runs it as
   its own child so the app's supervisor cannot kill it. A window without that
   listener falls back to `POST /api/updates/apply`, which spawns the same
   runner from the server. Either way the runner calls the verified updater
   (`scripts/update.js --yes --no-desktop`). With a signed `builds` entry it
   downloads and verifies the release build (see *Release builds*) and stops
   after reporting the staged directory — **no checkout, no build**. Without one
   it takes the git fallback: signature + allowlisted URLs, clean tree,
   tag == signed commit, `jdds.db` backup, checkout, `npm ci`, `npm run build`. Either
   way it then **stops**; promotion is the next step.
   - **Dependencies are staged, never installed into the live tree.** `npm ci`
     deletes `node_modules` before it writes, and a running app maps native
     modules that Windows will not let it delete — so an in-place install over a
     live app emptied the tree and then failed (`EPERM: unlink next-swc...` on
     2026-11-22). `scripts/lib/deps-install.js` fetches into `.runtime/deps-stage`
     with `--include=dev` and `NODE_ENV` cleared (the app runs with
     `NODE_ENV=production`, which would otherwise drop the dev tree a checkout
     needs for `npm run build`, `tsc` and the tests), validates the result, then
     moves the live tree to `.runtime/deps-previous` and renames the staged tree
     into place. A failed install — or a failed `postinstall` — puts the previous
     tree back, so the app keeps a complete `node_modules` whatever npm does, no
     process has to be closed, and the running window is unaffected until its
     normal restart. A replaced tree that the app still maps is parked under a
     timestamped name and removed by the next install. See ADR-083.
2. **Promote.** The runner continues with
   `desktop-refresh.js --no-build --version <signed version>`, which stages the
   new build under `releases/stable/<buildId>` and moves the pointer. The version
   comes from the SIGNED manifest, not from the checkout on disk: a prebuilt
   artifact carries no `package.json`, and recording the checkout's version made
   release metadata describe a build that did not exist ("up to date" for a
   version the window was not running). When the runner stopped the
   app itself it passes the restart through (nothing is left to serve), so the
   window reopens on the new build; otherwise `--no-restart` keeps the running
   window on the build it already loaded and the old release is kept as
   `previous`.
3. **Restart.** The panel offers *Restart Eclipse*, which emits the Tauri event
   `be-update` (`{action:"restart", channel}`). The Rust shell spawns
   `desktop-refresh.js --no-build`, which re-promotes the same build (a no-op)
   and relaunches the window. Closing and reopening Eclipse achieves the same
   thing, so an older shell that predates the `be-update` listener still ends up
   on the new build once it is reopened.
   The relaunch deliberately **waits for the old window process to exit** before
   starting the new one (`waitForShellExit`), then confirms the new process
   survived and retries once. Without that wait, Tauri's single-instance plugin
   handed the launch to the still-exiting old process: no window ran its
   bootstrap and the app sat on a black splash with no server — the "update
   blacks out my app" failure. A **bridged** install (see `environments.md`)
   also needs `update-apply.js` and `desktop-refresh.js` shims next to the
   `desktop-server.js` bridge, because the shell runs those scripts from its
   packaged root; `desktop-dev-bridge.js` writes all three.

**Why the restart lives in Rust.** Promotion itself is safe while the app runs
(atomic pointer write, previous release kept). The restart is the destructive
part: `desktop-refresh` calls `stopShell` and `stopOwnedServer`, and the latter
is `taskkill /F /T` on the desktop server — i.e. the entire server tree. A
refresh spawned by the server would be killed after its own copy but before it
relaunched anything. The shell is outside that tree, and `stopShell` kills only
the window pid, so a refresh spawned by the shell survives long enough to
finish. Do not "simplify" this by spawning the full restart from the API route;
do not remove `--no-restart` from the in-app promote.

Progress crosses the restart by **file, not IPC**: `scripts/update-apply.js`
writes `.runtime/update-job.json` (phase, message, log tail, target version,
heartbeat) and the UI polls `/api/updates/apply`. A `running` job whose heartbeat
is older than five minutes is reported as failed rather than as a spinner that
never ends (`src/lib/updates/job.ts`). A **finished** job is read against the
installed version: once the app is running a different version, the file is a
spent receipt and is not returned. Without that, a CLI update left the old
failure on screen forever.

**The version that decides "is this newer?" is the running build, not the
checkout on disk.** The window serves an immutable pinned build, so the working
tree can be ahead of (or behind) what the user is looking at. The title-bar
request therefore carries `currentVersion` (`be-update` →
`--running-version`; the HTTP fallback sets `BE_UPDATE_RUNNING_VERSION`), and
`update.js` / `update-apply.js` use that for the comparison and for the job's
`fromVersion`. A no-op job records that same version as `toVersion`. Without
this, a checkout newer than the signed release made every click exit
"already up to date" and the job was hidden as a spent receipt — the panel
went back to "Update available" with no error, forever. `black update` without
the flag still uses `package.json`.

Deliberate limits:

- **A build can only look where it was told to look.** `config/updates.json`
  (repository slug + manifest URL) is compiled into each release. A build whose
  baked slug has since been deleted or renamed cannot discover the new home: the
  check 404s and, by design, stays quiet in the banner. This already happened —
  builds released while the repo was `dumbpabloo/black-eclipse` (now 404) can
  never self-check, and no later release can reach them. The remedy for those
  installs is the signed installer / Download link, once. Never delete or rename
  the release repository again: a GitHub rename redirects old paths, a delete and
  recreate does not.
- **A release cannot be staged twice.** When the promoted build is already newer
  than the running one, the runner reports "Restart Eclipse to switch to it"
  instead of downloading the same release again. The banner and the panel show a
  Restart action for that state, so the "update again and again" loop is closed
  from both ends.
- The panel never claims to have applied anything the running window has not
  loaded. A staged job stays "ready" only while the running `version` is still
  the one it started from; after the restart it is treated as spent.
- In-app staging never empties the tree the running app serves from. The
  git-fallback path used to require the app to close so `npm ci` could replace
  `next-swc`/`skia`; with the staged install (`scripts/lib/deps-install.js`)
  dependencies are fetched elsewhere and swapped by rename, so neither path
  needs a shutdown and a failed install leaves the app bootable. The prebuilt
  path still touches no dependency at all unless the release's `lockHash`
  differs.
- Only the **stable desktop channel** can stage in-app, because `npm run build`
  always produces `.next` (the Stable channel's build dir). The Dev channel keeps
  using the CLI. Running it from an old shell that lacks the `be-update` listener
  still stages, then tells the user to run `npm run desktop:refresh`.
- Needs a shell rebuild (`npm run desktop:build` + repackage/install) to gain the
  `be-update` listener; without it the staged build still goes live the next
  time the app is opened. The web-side button is served by the pinned build as
  usual.

### Packaged installs (an installer has no git tree)

`scripts/update-apply.js` branches on what it is running inside: a packaged
`<install>\app` has no `.git`, so it cannot `git checkout` or `npm ci`. That is
the normal end-user install, not an error. In that mode the signed manifest
carries a per-platform installer:

```json
{
  "installers": {
    "win32-x64": {
      "url": "https://github.com/<org>/<repo>/releases/download/v0.4.16/BlackEclipse-Setup.exe",
      "sha256": "8a77de72…",
      "size": 188484501
    }
  }
}
```

The runner fetches the manifest itself (it never trusts the page's answer),
selects `"<platform>-<arch>"`, streams the download to
`<BE_DATA_DIR>/.runtime/updates/` (outside `$INSTDIR`), and **refuses to open
anything** whose SHA-256 or size differs from the signed values. A mismatch
deletes the file and installs nothing. It then hands the verified file to the
Windows installer through a detached PowerShell helper — an HTTP-started runner
lives inside the server tree the installer must close. The helper waits for the
runner to exit, closes this install's window and its owned server, then opens the
installer. The installer's own pages take over; the user's data directory is
outside `$INSTDIR` and is never touched.

Notes:

- The `installers` map is part of the **signed payload**. A manifest without it
  (every release through 0.4.15) makes the packaged path fail honestly with
  "open the release page", not a git error the user cannot act on.
- Only `https:` URLs on `allowedHosts` are accepted; the runner re-checks them
  even though the payload is signed, because signed content is still remote input.
- The job grows a `mode` (`git` | `installer`) and, for installer mode, the
  verified `artifact` (path/name/sha256/bytes). The panel says "downloaded and
  verified" and does **not** offer "Restart Eclipse" — the installer owns the
  restart.
- Windows only today, matching the desktop product. Other platforms fall back to
  the release page.
- `scripts/desktop-package.js` must ship `update-apply.js` and `update.js` in
  `app/scripts` (it copied only the bootstrap before) and stamps
  `app/package.json` with the install version; without both, a packaged install
  has no updater to run.

## Release builds: the release is the artifact, not a tag to rebuild

**The design rule.** An update moves a verified ARTIFACT into place. It does not
check out the release in the user's repository and compile it. This is how
shipped desktop software updates (the Tauri updater the shell already uses, VS
Code, electron-updater) and how OpenCode ships: the vendor builds once, signs
the artifact's digest, the client verifies and swaps it.

**Why.** The checkout-based updater had four compounding failure modes, and the
worst one fired on essentially every release:

| Failure | Effect |
|---|---|
| Refused a dirty tree | Every release edits the changelog (a date, some prose). The maintainer's own checkout is therefore dirty, and the update could never be applied there. |
| Checked out the tag | If the checkout was ahead of the release, updating moved the tree BACKWARD and detached HEAD. |
| Ran `npm ci` + `npm run build` | 3–5 minutes, a 12 GB heap, and any build failure became an update failure. |
| Trusted a source-hash cache | A build that landed outside `.next` still marked `.next` current, so "already built" could hide a stale build. |

None of those can happen on the artifact path. The user's source tree is not
touched, so it may be as dirty as it likes; no compiler runs; and the promoted
directory is the exact bytes whose digest the manifest signed.

**What ships.** `npm run release:build` (`scripts/release-build-zip.js`) produces
`dist/BlackEclipse-Build-<version>.zip` from a completed `.next` (Next's cache,
diagnostics, trace and standalone output are excluded). The release workflow's
`release-build` job builds and attaches it to every release. The signed payload
then carries:

```json
"builds": {
  "any": {
    "url": "https://github.com/<org>/<repo>/releases/download/v0.4.22/BlackEclipse-Build-0.4.22.zip",
    "sha256": "<64 hex>",
    "size": 135621169,
    "buildId": "iMDae0ZQf-iLEsqePNmgK",
    "lockHash": "<sha256 of package-lock.json>"
  }
}
```

- `any` is the normal key: a Next build is platform-independent JS. An exact
  `<platform>-<arch>` entry overrides it when that is ever needed.
- `buildId` is checked against the archive before promotion.
- `lockHash` makes a dependency change explicit: the runner installs before
  promoting only when it differs.
- The `sha256`/`size` are signed, so a tampered asset is refused and deleted.

## Applying an update: `black update`

```bash
black update --check            # read-only
black update                    # prompt, then apply (desktop refreshed first)
black update --yes --restart    # unattended
black update --no-desktop       # stage only; the in-app runner promotes
black update --git              # force checkout + build (contributor path)
black update --channel beta
```

Order of operations (enforced in `scripts/update.js`):

**The normal path is the prebuilt build.** The signed manifest can advertise
`builds` — the release's COMPILED `.next`, uploaded as a release asset. When it
does:

1. Verify the manifest signature and the allowlisted URLs.
2. Download the archive, hashing as it streams; **refuse** unless the bytes match
   the signed `sha256` (and `size`, when present). A mismatch deletes the file.
3. Extract into `.runtime/updates/stage-<version>` (absolute paths and `..`
   entries in the archive are rejected) and require a serviceable build:
   `BUILD_ID` (equal to the signed `buildId` when present), `routes-manifest`,
   `server/`, `static/<buildId>/`.
4. If the release's `lockHash` differs from this checkout's
   `package-lock.json`, install dependencies first (`scripts/lib/deps-install.js`
   — staged, then swapped by rename) — the build was compiled against those
   modules. Otherwise `node_modules` is not touched.
5. Promote exactly that directory (`desktop-promote.js --from …`): copy into
   `releases/stable/<buildId>/`, validate the copy, move the pointer, keep the
   previous release as the rollback target.

`git` is never read, nothing is compiled, and an uncommitted working tree is
irrelevant. That last point is the whole reason this path exists: every release
touches the changelog (a date, some prose), so "refuse if dirty" meant the
maintainer's own machine could not take an update.

**The git path is the fallback**, used when the manifest advertises no `builds`
(every release through v0.4.21) or when `--git` is passed:

1. Verify the manifest signature and the allowlisted URLs.
2. Refuse if the working tree is dirty.
3. `git fetch --tags`; require `v<version>` to resolve to the **signed commit**.
   A moved or mismatched tag aborts the update.
4. Back up `jdds.db` through `scripts/db-guard.js backup` (unless
   `--no-backup`, which is discouraged).
5. Check out the tag (detached), install dependencies into a staging directory
   and swap them in by rename (`scripts/lib/deps-install.js`), then
   `npm run build`. A failed install changes nothing, so there is nothing to
   retry and nothing to lose.
6. **Desktop first:** re-pin the native app's immutable build and restart its
   own server/reopen the window through `scripts/desktop-refresh.js`. It no-ops
   with an explanation if the desktop is not installed or is a packaged
   (non-bridged) install; `--no-desktop` skips it.
7. Any failure → check out the previous commit. If dependencies were replaced,
   restore the parked tree (`.runtime/deps-previous`) by rename and rebuild; if
   the install never ran, the previous build is still intact and reinstalling
   could only do damage. The DB backup is left untouched and reported.

**Desktop-first is a product rule, not a preference.** The native window is the
primary front door; the browser build is secondary. An update that leaves the
desktop on the previous build while the website is current is a failed update.
The pinned copy makes this safe: the window can be restarted and re-pinned
without any web server, and the reverse (a web only update) is opt-in.

Updates never auto-apply from the browser. That is deliberate: the update path
touches the user's local database and runs new code, so the person whose data it
is gets the final yes.

`black update --git` checks out the release tag in **detached HEAD** state. That
is the point of the fallback: the running code is exactly the signed commit. To
go back to developing on a branch, run `git switch main` (and `git pull`). The
prebuilt path never moves HEAD, so this only applies when you opt into it.

## Compromise and rotation

| Event | Response |
|---|---|
| Private key leaked | Generate a new keypair, commit the new `.pub`, publish a release, revoke the old key id in the release notes and here. Users on the old release still verify old manifests until they update — say so plainly. |
| Signing machine stolen | Same as leaked. The passphrase/offline storage is the only barrier; assume the key is gone. |
| GitHub account compromised | Users are still safe if the private key is offline: the attacker cannot sign a manifest. Rotate account credentials, audit releases, keep the key offline. |
| Tag moved / commit mismatch | `black update` refuses automatically. Investigate; do not add a bypass. |
| Lost private key | Users keep working; no new updates can be signed. Rotate by publishing a release from a new key (users must apply that one manually), then resume. |

## What never to do

- Never commit, paste, or CI-store the private key.
- Never distribute a zip of your working folder. Release from a clean clone or
  `git archive`, or a stray `.black-eclipse/` (private key, chats, cache) ships
  with the product. The doc above assumes releases come from git tags.
- Never host the manifest on a personal server or home network. GitHub Releases
  (or any static CDN) removes that exposure.
- Never let the client trust a URL from the manifest without the allowlist.
- Never open a downloaded installer whose SHA-256 does not match the signed
  manifest; a signed URL without a signed digest is not enough.
- Never auto-apply an update without a signature check and a DB backup.
- Never put personal contact details in `README`, `LICENSE`, `package.json`
  `author`, commit emails, or DNS WHOIS. Publish as the org; use
  `@users.noreply.github.com` and GitHub Security Advisories for reports.
- Never mark a failed check as "up to date".

## Tests & troubleshooting

```bash
npx vitest run src/lib/updates/core.test.ts   # signature, semver, allowlist
node scripts/update.js --check                # live check
node scripts/update-sign.js ... --out .black-eclipse/test.json --force
```

- **"Could not check for updates"** — the manifest URL 404s (no release yet), or
  the host is offline. Check `config/updates.json` and that `stable.json` is
  attached to the latest release.
- **"signature is invalid"** — the manifest was signed with a different private
  key than the committed `.pub`, or the bytes were edited after signing.
- **"tag points at … but the signed manifest names …"** — the tag moved, or you
  signed the wrong commit. Re-tag from the signed commit and re-sign.
- **"the working tree has N uncommitted change(s)"** — only the git fallback
  says this (a manifest with no `builds` entry, or `--git`), and it is by design:
  a build that mixes your edits with the signed tag is not the release. It names
  the files, and when the checkout is already at/past the release it says so
  (`npm run desktop:refresh` re-pins without a checkout). With a signed build,
  commit state is irrelevant. The in-app panel shows the same text, and
  `.runtime/update-job.json` keeps the log.
- **"this release advertises no prebuilt build"** — that manifest predates the
  artifact path (every release through v0.4.21), so the updater fell back to
  checkout + build. Re-sign the manifest with `--build-*` to fix it.
- **"The downloaded build failed its signed checksum"** — the asset and the
  manifest disagree (a stale CDN edge, a re-uploaded file, or tampering). The
  download was deleted and nothing was installed; retry, then re-check the
  uploaded asset against `npm run release:build`'s printed sha256.
