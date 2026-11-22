---
title: Open-Source Publication Readiness
status: Current
last-verified: 2026-11-16
---

# Open-Source Publication Readiness

**Who this is for:** whoever is about to make this repository public, and whoever
has to prove the public tree is safe.

This doc owns the **publication mechanics** (what makes a private working
repository safe to publish). It does not repeat the product gate or the
deployment gate:

| Question | Owning doc |
|---|---|
| Is this build releasable, and what is still open for production? | [`PRODUCTION-READINESS.md`](./PRODUCTION-READINESS.md) |
| What stops an accidental force-push or secret commit? | [`repository-guards.md`](./repository-guards.md) |
| What is the licence, and what is different about `pine-engine/`? | [`../LICENSE`](../../LICENSE), [`../pine-engine/NOTICE.md`](../pine-engine/NOTICE.md) |
| What legal documents exist and what is still Draft? | [`legal/README.md`](./legal/README.md) |

The app is **MIT**. The repository went **public on 2026-11-17** from a freshly
created history (see §2); publication was a repository operation, not a licence
change. This page stays as the record of what had to be true first.

---

## 1. The one-screen answer

Most of the public surface already exists: `LICENSE`, `CLA.md`, `TRADEMARK.md`,
`SECURITY.md`, `CONTRIBUTING.md`, `DCO`, `CODEOWNERS`, CI, Dependabot, hooks, a
canonical docs tree, the `v0.4.0` release note, and a generated SBOM/notices pair. What is
missing is **history hygiene on stale refs**, **credential rotation**, and the
owner's legal + GitHub-settings pass.

| # | Blocker | Evidence |
|---|---|---|
| O1 | ~~Git history contains `jdds.db` snapshots on stale refs~~ **resolved 2026-11-16** | Branches were deleted, but that was not the whole story: GitHub permanently serves `refs/pull/N/head`, and 11 of 13 PR refs still reached the 60 MB blob (a 38 MB `jdds.db` object was fetchable from `refs/pull/1/head`). `check:oss` now audits that namespace. Resolved by publishing from a **new** repository; see §2. |
| O2 | ~~History has never been scanned for credentials~~ **resolved in the mirror** | `scripts/scan-secrets.js` scans **tracked HEAD only**. A `gitleaks` 8.30.1 scan over `--all` found **two real hardcoded tokens** in old commits; both were scrubbed with `--replace-text` and **must still be rotated**. The final mirror scans clean with `.gitleaks.toml` (reviewed allowlist for fixtures). |
| O3 | ~~Tracked files contain absolute home-directory paths~~ **resolved** | `npm run check:oss` reports no absolute home-directory path. |
| O4 | ~~Personal author e-mail is on **one stale ref only**~~ **resolved 2026-11-16** | After the rewrite every ref, including the old PR refs, is `@users.noreply.github.com`; a fresh clone of the new repository confirms it. |
| O5 | ~~Community files were incomplete~~ **resolved** | Root `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue/PR templates, `DCO`, `CLA.md`, `TRADEMARK.md`, `check:oss`. |
| O6 | Legal/IP review for public distribution | `docs/legal/*` are Draft and clearly scoped to a source-only self-hosted release. A "not affiliated with CFA Institute" notice now sits in `.black-eclipse/skills/CFA-NOTICE.md` and every `cfa-*` skill; the owner still needs a trademark read of the skills and the "Bloomberg-inspired" wording. |
| O7 | ~~Branch protection is unavailable here~~ **resolved 2026-11-17** | Private + Free returned `403`. The repository is public, `.github/ruleset.json` is applied (no deletion, no force-push, PR required, the CI check required in strict mode) and an admin bypass keeps a single maintainer and a parallel agent session unblocked. |
| O8 | ~~No code scanning~~ **resolved** | `.github/workflows/codeql.yml`. |
| O9 | ~~No SBOM or third-party notices~~ **resolved** | `npm run sbom` generates `sbom.cdx.json` (CycloneDX 1.5, 1,320 components) and `THIRD-PARTY-NOTICES.md` from the locked trees; `--check` runs in CI and `release:bundle` regenerates them. |
| O10 | ~~The tagged `v0.4.0` had no product release note~~ **resolved** | `docs/releases/v0.4.0.md`, built from the generated release bundle rather than hand-typed numbers. |

**Do not flip the existing private repository to public.** That publishes the
stale O1/O4 history and anything the old commits contain. Create the public
repository from a rewritten history (§2), or delete the stale refs and prove a
fresh clone is clean first.

**Done 2026-11-16.** The public repository is a *new* repository built from the
rewritten history; the previous private one remains as
`BlackEclipse-V1.0-archive` — **private, and it must stay that way**, because it
still holds every leaking PR ref and the 720 MB draft release. (The account and
the repository were renamed since: the live home is
`dumbpabloo/BlackEclipse-V1.0`.)
A fresh clone of the new repository shows zero `refs/pull/*`, zero `.db` paths in
history, no reachable 38 MB blob, and noreply-only authors.

---

## 2. Scrub the history (the hard blocker)

**Already done on the mainline.** `main` and every tag were rewritten; they
contain no `.db` blob and no personal author e-mail. The stale branches were
deleted too, but **deleting branches is not sufficient**: GitHub keeps
`refs/pull/N/head` forever for every closed and squash-merged PR, and those refs
kept 11 PRs' worth of pre-rewrite commits reachable — including a 38 MB
`jdds.db` object. Nothing on the client side can remove a `refs/pull` ref.

That is why the resolution was a new repository rather than a cleanup, and why
`npm run oss:prune-refs` now inspects the PR namespace before declaring the
history clean.

Confirm for yourself:

```bash
git log --all --oneline -- Black-Eclipse-site/jdds.db
git branch -a --contains "$(git log --all --format=%H -- Black-Eclipse-site/jdds.db | head -1)"
```

### Close it: delete the stale refs

`npm run oss:prune-refs` finds exactly the refs that still expose an old
database snapshot or the personal author address (by history, not by name),
refuses to touch `main`, the checked-out branch, or tags, and is **dry-run by
default**:

```bash
cd Black-Eclipse-site
npm run oss:prune-refs              # lists the refs and the exact command
npm run oss:prune-refs -- --apply   # deletes them locally/remotely, then prunes
npm run check:oss                   # must now exit 0
```

It prints the equivalent manual command, so the operation is reviewable:

```bash
git push origin --delete \
  backup-before-email-rewrite backup/main-wip-20261025-231230 production-ready \
  feature/round-3-harness-parity fix/harness-data-integrity plugin-platform \
  refactor/harness-folders feat/graph-quant-packs feat/eclipse-mandates-service \
  release/graph-quant-packs \
  dependabot/github_actions/actions/checkout-7 \
  dependabot/pip/Black-Eclipse-site/python/python-runtime-0c19635c87
git fetch --prune
```

If any pull request still references an old commit, open a fresh repository and
push `main` plus re-created tags into it; that is the only way to guarantee the
old objects are unreachable. Do not publish from the old repository until a
`gitleaks --all` scan on a fresh clone is clean.

**That is the path this repository took (2026-11-16).** `oss:prune-refs` reports
the PR refs it cannot delete and exits non-zero; the release then went out from a
new repository, and `check:oss` (which now audits `refs/pull/*`) passes there.

### If you ever need to redo the rewrite

`git filter-repo` is the maintained tool (install it separately; it is not a
repo dependency). A helper wraps the same steps and never touches the live
clone:

```powershell
pwsh -File scripts/scrub-public-history.ps1 -SourceUrl <private-clone-url>
# optional: -Replacements .\replacements.txt
```

It clones a mirror to a temp directory, strips the database paths, verifies
they are gone, and prints the push steps. It does **not** push.

```bash
# 1. Work on a mirror, never on the live clone.
git clone --mirror <private-clone-url> BlackEclipse-public.git
cd BlackEclipse-public.git

# 2. Remove every database snapshot from every commit.
git filter-repo \
  --invert-paths \
  --path Black-Eclipse-site/jdds.db \
  --path-glob 'Black-Eclipse-site/*.db' \
  --path-glob 'Black-Eclipse-site/*.db-wal' \
  --path-glob 'Black-Eclipse-site/*.db-shm'

# 3. Rewrite personal paths and author e-mail. Keep this file OUTSIDE the repo.
#    Use placeholders in the doc; the real values live only in your shell.
cat > ../replacements.txt <<'EOF'
C:\\Users\\<your-user>\\==>C:\\Users\\<user>\\
/Users/<your-user>/==>/Users/<user>/
<personal-email>==><id>+<handle>@users.noreply.github.com
EOF
git filter-repo --replace-text ../replacements.txt

# 4. Prove the result before pushing.
git log --all --oneline -- '*jdds.db'        # must print nothing
git rev-list --objects --all \
  | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | sort -k3 -n -r | head                    # no multi-MB .db blobs

# 5. Push the rewritten mirror to the NEW public repository.
git remote add public <public-repo-url>
git push --mirror public
```

Notes:

- **Every commit hash and tag changes.** Re-create annotated tags on the
  rewritten commits (`v0.4.0` etc.) rather than pushing the old tag objects.
- **The mirror is a snapshot.** Re-run the script after the last commit you
  intend to publish; do not push a mirror built from an earlier tree.
- The old private repository keeps its history; do not delete it. It is the
  provenance record.
- If a rewrite is too risky, the alternative is a **fresh repository with one
  initial commit** of the current tree. You lose history but the public snapshot
  is trivially clean. Say which one you chose in the release note.

### After the rewrite

Run the credential scan over history, not just HEAD:

```bash
gitleaks detect --source . --log-opts="--all" --redact
# or
trufflehog git file://. --only-verified
```

Use the committed config so the scan is repeatable:

```bash
gitleaks detect --source <mirror-or-worktree> --config .gitleaks.toml \
  --log-opts="--all" --redact
```

The config allowlists only triaged non-secrets (test fixtures, `STORAGE_KEY`
constants, eval `"key"` fields, a generated cache hash, and the fake
`AKIA...EXAMPLE` string). A hit outside that list is a real finding.

Any hit is a real incident: **rotate the credential before pushing**, then
scrub it with `--replace-text`. Deleting a token from history does not make it
un-leaked; the scan found two live service tokens in this repository's past, so
rotation is mandatory even after the mirror is clean.

---

## 3. The re-runnable public-release preflight

`npm run check:oss` (from `Black-Eclipse-site/`) checks the things a release
gate does not:

- no tracked `.env`, `*.db`, key, or connector artifact;
- the community files above exist;
- `sbom.cdx.json` and `THIRD-PARTY-NOTICES.md` exist and match the lockfiles;
- `package.json` has `repository`, `homepage`, and `bugs`;
- **history** has no `.db` or secret-bearing path;
- tracked files have no absolute home-directory path;
- personal author e-mails are reported;
- the normal `scan-secrets.js` track scan passes.

It is intentionally **not** part of `check:all`: a private working repository
legitimately fails O1 until it is rewritten. Run `check:oss` on the public
snapshot; it must exit 0 before you flip visibility.

```bash
cd Black-Eclipse-site
npm run check:oss          # human output; exit 1 means do not publish
npm run check:oss -- --json
```

`.github/workflows/oss-preflight.yml` runs the same check on every push and pull
request **once the repository is public**. On a private repository the job is
skipped, because the pre-rewrite branches legitimately fail it; use the
`workflow_dispatch` trigger to run it by hand before flipping visibility.

---

## 4. Publication-day checklist

Repository content:

- [ ] `npm run oss:prune-refs` reports no stale refs (apply with `-- --apply`).
- [ ] `npm run check:oss` exits 0 on the public snapshot.
- [ ] `git status --short` is empty; the tree is a tagged commit.
- [ ] `npm run check:all` exits 0 on that commit.
- [ ] `gitleaks`/`trufflehog` over `--all` is clean.
- [ ] `node scripts/generate-sbom.js --check` exits 0 (run `npm run sbom` first
      if a lockfile changed); `sbom.cdx.json` and `THIRD-PARTY-NOTICES.md` are current.
- [ ] `docs/releases/<version>.md` and the `CHANGELOG.md` entry exist.
- [ ] Root `README.md` names the current version and the measured live number.
- [ ] `CONTRIBUTING.md` → `Black-Eclipse-site/CONTRIBUTING.md` → `DCO` resolves.
- [ ] Issue templates, PR template, and Code of Conduct are present.
- [ ] `media/` contains no generated build artifacts or machine-local paths.
- [ ] If publication uses a **new repository URL**, re-point `package.json`
      (`repository`, `homepage`, `bugs`), the README download/release links,
      `.github/ISSUE_TEMPLATE/config.yml`, and the API URL in
      `repository-guards.md`; the current links all name
      `dumbpabloo/BlackEclipse-V1.0`.

GitHub settings (all free once public):

- [ ] Enable **secret scanning** and **push protection**.
- [ ] Enable **Dependabot alerts** (`.github/dependabot.yml` is already committed).
- [ ] Import `.github/ruleset.json` on `main` (see `repository-guards.md`).
- [ ] Turn on **private vulnerability reporting** (`SECURITY.md` depends on it).
- [ ] Confirm the first **CodeQL** run is green.
- [ ] Confirm the **Public-release preflight** job runs (it is skipped while the
      repository is private and becomes a gate as soon as it is public).
- [ ] Create the `@BlackEclipse/maintainers` team or replace it in `CODEOWNERS`.
- [ ] Enable GitHub **Discussions** (the issue chooser links to it) or re-point
      that link in `.github/ISSUE_TEMPLATE/config.yml`.
- [ ] Set repository description, topics, and the social preview.
- [ ] Publish the release note and tag.

---

## 5. Legal and IP review before public distribution

These are judgement calls for the owner, not code defects. They are listed so
they are not discovered after launch.

- **MIT + AGPL scoping.** Root `LICENSE` scopes the repository to MIT and carves
  out `pine-engine/` as AGPL-3.0-only. Keep that carve-out in every public
  mirror and in the release assets.
- **Contract pack is Draft.** `docs/legal/*` is not legal advice and is not
  reviewed. A **source-only, self-hosted, single-operator** release can ship with
  the README saying so; a hosted service cannot (C11/C12 in
  `PRODUCTION-READINESS.md`).
- **`cfa-*` skills.** `.black-eclipse/skills/cfa-*` are original study notes.
  `CFA` is a CFA Institute trademark, so every skill now opens with a "not
  affiliated with CFA Institute" notice and `.black-eclipse/skills/CFA-NOTICE.md`
  carries the full statement. The owner should still confirm the module list and
  naming are acceptable before distribution.
- **"Bloomberg-inspired".** Descriptive comparison is usually nominative, but do
  not use the Bloomberg name or trade dress as branding. Keep the wording
  descriptive.
- **Market-data redistribution.** Do not ship a seeded `jdds.db`. The app
  fetches and stores data locally at runtime; that is a different act from
  redistributing a snapshot. This is the same reason O1 must be scrubbed.
- **Third-party dependency licences.** `pine-engine/` is the only known copyleft
  component. If a dependency audit is required, generate a notices file from the
  locked tree rather than hand-writing claims.

---

## 6. What is outside this doc

- The **production** gates (soak, alert webhook, container, secrets, second
  reviewer) live in `PRODUCTION-READINESS.md`. Publication does not close them;
  a public repository is not a production sign-off.
- The **organisation** actions (creating the team, buying a plan, counsel) are
  inputs only a person can supply.
- The **website/browser lane** is retired. Do not restore it while preparing a
  public release; the desktop app is the product.

---

## 7. Progress log (append-only)

| Date | State | Evidence |
|---|---|---|
| 2026-11-14 | Personal-data sweep complete | Removed the generated `media/blackeclipse-launch/.hyperframes/` tree, sanitized three baseline JSON evidence files to `<tmp>`, replaced personal names in the memory test fixture, and dropped the personal address from `install-git-hooks.js`. Final mirror: `check:oss` **PASS / 0 warnings**, `gitleaks` **no leaks**, `git log -S` finds none of the removed tokens, email, names or old home paths, `jdds.db` is absent, 40.3 MiB. **The two tokens still require rotation.** |
| 2026-11-14 | History credential scan | **2 real tokens found and scrubbed** | `gitleaks 8.30.1` over `--all` on the first clean mirror found two hardcoded service tokens in old commits (not present at HEAD). A second mirror was produced with `--replace-text` and a mailmap; the tokens are absent (`git log -S` empty) and the history scan is green with the new `.gitleaks.toml`. **Both tokens must be rotated**; history removal is not revocation. Remaining findings were triaged false positives (test fixtures, storage-key literals, eval `"key"` fields, a cache hash, the fake `AKIA...EXAMPLE`). |
| 2026-11-14 | Clean mirror produced and verified | Ran `scripts/scrub-public-history.ps1` on a mirror of this repository: `jdds.db`/`eclipse-memory.db`/`agent-memory.db` are absent from every commit, pack size fell from 79.9 MiB to 40.3 MiB, and a clone of the mirror passes `npm run check:oss` with **0 warnings** after a mailmap rewrite of the personal author email. The live private history is unchanged and must not be flipped to public. Not pushed. Remaining before push: O2 (gitleaks/trufflehog over `--all`), create the new public repository, re-create/push tags. |
| 2026-11-14 | Scaffolding added | Root `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue/PR templates, `package.json` metadata, CodeQL workflow, `scripts/oss-preflight.js` (`npm run check:oss`), and the mirror-only `scripts/scrub-public-history.ps1`. Personal absolute paths were removed from the bulk importers (now `BE_IMPORT_ROOT`, default `/data/imports/`, gitignored) and the roadmap/media docs. `check:oss` now reports exactly one blocker: `jdds.db` in history (O1). O2 (history credential scan) and the history rewrite remain operator steps. |
| 2026-11-16 | SBOM and notices generated | `npm run sbom` (`scripts/generate-sbom.js`) writes `sbom.cdx.json` (CycloneDX 1.5, **1,320 components**: 1,185 npm + 135 Python, 27 licence groups) and `THIRD-PARTY-NOTICES.md` from `package-lock.json` + `python/requirements.lock`. Python licences are marked `NOASSERTION` rather than guessed. `--check` fails on drift. O9 closed. |
| 2026-11-16 | Tagged release note added | `docs/releases/v0.4.0.md` documents the C2-C9 core-integrity work and publishes the generated bundle numbers (1,091 tools, scorecard 8/8, conformance 12/12, wheel sha256) instead of hand-typed claims. `v0.4.0` was tagged without a product note; O10 closed. |
| 2026-11-16 | Trademark notice and ruleset posture | `.black-eclipse/skills/CFA-NOTICE.md` plus a one-line notice in all 12 `cfa-*` skills (not affiliated with CFA Institute). `.github/ruleset.json` now requires **1** approval; `repository-guards.md` explains the public posture and the solo-maintainer consequence. `SECURITY.md` now states the Python lockfile truth and lists the residual risks accepted for the source release. O6 narrowed to the owner's legal read. |
| 2026-11-16 | Current preflight state | `check:oss` still stops on **one** blocker: `jdds.db` reachable from eleven stale remote branch refs. `main` and all tags are clean, and `main` carries only the noreply author address; the personal address survives only on `origin/backup-before-email-rewrite`. Deleting those twelve remote branches + two local backups (and purging cached PR objects) is the remaining repository operation. |
