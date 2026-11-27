---
title: Data & Privacy — where your personal data lives and how it is protected
status: Current
last-verified: 2026-11-17
---

# Data & privacy

**Who this is for:** anyone deciding whether to put a real portfolio, journal,
client notes, documents or credentials into BlackEclipse, and anyone operating
the app for someone else.

## The one-sentence model

BlackEclipse is a **local-first single-user workbench**: your data lives in files
on your machine, and it leaves the machine only through the endpoints you
configure (model provider, web search, brokers). Protecting those files at rest
is therefore an **operating-system job** — the app provides the structure, the
switch and the wipe, the OS provides the encryption.

## Where the data is

| Path | Contents | Sensitivity |
|---|---|---|
| `jdds.db` | Portfolio, journal, watchlists, settings, provider-key vault | **High** |
| `agent-memory.db` | Conversations, memory, runs, tool traces, security audit | **High** |
| `eclipse-memory.db` | Learned preferences and skills | **High** |
| `.black-eclipse/auth.db` | Accounts + sessions (scrypt hashes, not passwords) | High |
| `.black-eclipse/artifacts/` | Generated/uploaded files and previews | **High** |
| `.black-eclipse/documents/` | Document IR derived from your uploads | **High** |
| `.black-eclipse/browser/` | Browser profiles, screenshots, downloads | **High** |
| `.black-eclipse/computer/` | Desktop screenshots and recordings | **Very high** |
| `.black-eclipse/sessions/` | Agent session trajectories | High |
| `backups/`, `_backups/` | Rolling and manual database copies | **High** |
| `mcp-servers.json` | MCP server config, possibly env values (secrets) | **Critical** |
| `.connector-key`, `python/.black-eclipse/channels/secret.key` | Encryption keys | **Critical** |
| `.env` | API keys, signing secrets | **Critical** |

Everything above is gitignored and covered by `scripts/scan-secrets.js`, which
fails a commit if one of them is ever staged.

## What the app protects by itself

- **Passwords**: scrypt, per-user salt; a missing user still pays the hash cost.
- **Sessions**: HttpOnly, `SameSite=Lax`, `Secure` over HTTPS, 7-day TTL.
- **Credential vaults**: AES-256-GCM for connector credentials, channel secrets
  and provider keys. Ciphertext only in the database.
- **Agent boundaries**: the security floor hard-denies reading `.env`, `.ssh`,
  `*.pem`, `*.key` and credential paths, even for reads.
- **Logs/events**: key-name redaction; optional PII redaction
  (`python/harness/observability/redaction.py`).
- **No analytics or phone-home**: there is no PostHog/Segment/GA/Sentry.
- **Network**: loopback by default; auth auto-requires on a non-loopback bind;
  `/api/cron/*` behind `CRON_SECRET`.

## What only the OS can protect

The databases are SQLite and are **not encrypted by the app**. Anyone who can
read the files (your Windows session, a stolen disk, a copied folder, an
unencrypted backup) can read the data. Two supported answers:

### 1. Encrypted data vault (recommended)

`scripts/setup-data-vault.ps1` creates a BitLocker-encrypted virtual disk, moves
every personal path onto it, and points the app there:

```powershell
# Plan first — changes nothing:
npm run vault:plan

# Execute (elevated PowerShell; BitLocker password prompt):
npm run vault:setup
```

BitLocker is required for encryption (Windows Pro/Enterprise). If this Windows
edition lacks the BitLocker cmdlets, or you prefer VeraCrypt, mount your own
encrypted volume as `V:` and run the script with `-SkipBitLocker` — it will move
and link the data without touching the encryption.

What the script does:

1. Creates `%USERPROFILE%\BlackEclipseVault\blackeclipse-vault.vhdx` (20 GB,
   expandable), mounts it, formats NTFS, assigns `V:`.
2. Enables BitLocker (XtsAes256, used-space-only) and saves the recovery key next
   to the VHDX — **store that key off this machine**.
3. Moves the databases to `V:\BlackEclipseData` and sets `BE_DATA_DIR`, so SQLite
   writes even its `-wal`/`-shm` sidecars on the encrypted volume.
4. Moves `.black-eclipse/`, `python/.black-eclipse/`, `backups/`, `_backups/`,
   `mcp-servers.json` and `.connector-key` onto the volume and leaves junctions/
   symlinks at the original paths, so every subsystem keeps working unchanged.
5. Writes `BE_VAULT_PATH` so a locked or detached volume **fails closed**:
   `scripts/doctor.js` refuses to start instead of creating empty databases.

Already using VeraCrypt? Mount the volume as `V:` and run
`npm run vault:setup -- -SkipBitLocker` (or run the PowerShell file directly with
`-SkipBitLocker -Apply`).

**Unlock once per boot.** While it is locked, the app will not start and says so.

### 2. Full-disk encryption only

BitLocker/VeraCrypt on the system drive protects the same files when the machine
is off, but they stay readable to anything running in your Windows session. Use
this only if you accept that trade-off; it requires no app configuration.

## Keeping conversations on the machine

```ini
# .env
ECLIPSE_PRIVACY=local-models
```

- Refuses every remote model endpoint **before a request is built**, in
  `python/harness/models.py` (all agent runs, jobs, automations, vision
  fallbacks) and in `src/lib/privacy.ts` (settings save, connectivity test, TS
  chat provider).
- Allows Ollama / LM Studio / any loopback endpoint and the offline `mock`
  provider.
- A blocked call is an error, never a silent fallback to the cloud.

**Scope:** this governs *model* traffic. Web search/fetch, brokers and MCP
servers still contact their own endpoints. For a true air gap, disable those
integrations or disconnect the network; `ECLIPSE_PRIVACY=local-models` will not
pretend otherwise.

## Resetting vs deleting everything

Pick the smallest scope that solves the problem:

| Scope | Where | What it removes | What it keeps |
|---|---|---|---|
| Re-run onboarding | Settings → General, or `black reset --onboarding` | `onboarding_v1` | Keys, AI config, chats, portfolio, research |
| Reset setup & keys | Settings → Data & Storage, or `black reset --setup` | onboarding, `data_source_prefs_v1`, `llm_config`, the provider-key vault | Chats, portfolio, journal, research, artifacts |
| Factory reset | Settings → Data & Storage, or `black reset --factory` | everything below under "Deleting everything" | Source code only |

A factory reset started from Settings is **scheduled**, not run in the web
process: it writes `.black-eclipse/reset-request.json`, and `scripts/doctor.js`
consumes it on the next start **before** opening `jdds.db`. A backup is taken by
default to `reset-backups/<timestamp>/` (outside the wipe targets); `.env` is
only removed with the explicit `--include-env` / checkbox. `black reset --list`
prints every path first.

## Deleting everything

```sh
node scripts/wipe-personal-data.js                  # show what would be deleted
node scripts/wipe-personal-data.js --confirm=WIPE   # delete it
node scripts/wipe-personal-data.js --confirm=WIPE --include-env
```

Removes the three databases + sidecars, `.black-eclipse/`, `python/.black-eclipse/`,
`backups/`, `_backups/`, `mcp-servers.json`, `.connector-key` and (with
`--include-env`) `.env`. It refuses paths outside the repo/vault roots.

This is a delete, not a secure erase: on an SSD the old bytes can survive in free
space or shadow copies. The guarantee comes from the **encrypted volume** — once
the files are gone and the volume is re-encrypted, only ciphertext remains.

## Backups

- Backups live under `backups/` (rolling, db-guard) and `_backups/` (manual).
- With the vault they are on the encrypted volume automatically.
- Without the vault, encrypt any backup you move off the machine.
- A backup is a full copy of your personal data — treat it exactly like the live
  files.

## Threat model (short)

| Adversary | With the vault | Without it |
|---|---|---|
| Someone who steals the powered-off machine | Protected | **Exposed** |
| Another user on the same PC | Protected while locked; exposed while you are signed in and the vault is unlocked | **Exposed** |
| Malware running as you | Exposed while unlocked | **Exposed** |
| A leaked git clone / public repo | Protected (data never tracked) | Protected (data never tracked) |
| An installer you build and hand to someone | Protected (payload scrubbed at package time; the build fails if runtime state survives) | Protected |
| A cloud model provider | Protected with `ECLIPSE_PRIVACY=local-models` | Provider sees prompts |
| A stolen backup file | Protected on the vault; otherwise **exposed** | **Exposed** |

The app cannot defend against an attacker who already controls your Windows
session or your unlocked vault. No local app can.

## What a release installer contains

The installer is assembled from a working checkout, so "not tracked by git" is
not by itself enough: `scripts/desktop-package.js` copies whole trees. Two gates
keep this machine's state out of the payload:

- `scripts/desktop-payload.js` prunes the compiled server tree and **refuses**
  (`exit 3`) to hand over a payload containing `.env*`, `mcp-servers.json`,
  `.connector-key`, `jdds.db`, `agent-memory.db`, `eclipse-memory.db` or
  `.black-eclipse/`. It also prunes local build/release/runtime trees that Next's
  standalone tracing copies from the checkout root (`releases/`, `.runtime/`,
  `.next-desktop-dev/`, `dist/`, `release/`) — they are not read at run time and
  made the payload multiple gigabytes.
- `scripts/lib/scrub-app-state.js` does the same for the trees copied after that
  (`.black-eclipse/`, `csess_*/` session trajectories, `__pycache__`,
  `.pytest_cache`, `*.egg-info`, `*.db*`, `*.log`), then
  `assertNoLocalState()` fails the build if anything survived.

The only database in a release is `seed/jdds.db`, regenerated at package time
from `prisma/schema.prisma` plus the deterministic demo journal — never a copy of
your vault. A packaged install therefore starts empty of your data and opens the
onboarding wizard, because a fresh database has no `onboarding_v1` row.

## Related

- `docs/harness/authentication.md` — who is allowed in.
- `docs/harness/security.md` — what the agent is allowed to do.
- `docs/DEPLOYMENT.md` — container hardening for a remote deployment.
- `SECURITY.md` (repository root) — how to report a vulnerability.
