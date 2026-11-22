---
title: Code File System Subsystem
status: Current
last-verified: 2026-11-21
---

# Code File System Subsystem

**Who this is for:** anyone extending Eclipse's file-editing capability — adding
a filesystem tool, an operation, a language parser, or a search engine.

The filesystem subsystem gives Eclipse real software-engineering ability:
inspect, search, understand, patch minimally, verify — without rewriting whole
files, corrupting sources, escaping the workspace, or applying ambiguous/stale
edits. It lives in `python/harness/code/fs/` and registers its tools in
`python/harness/code/fs_tools.py`.

## Where it sits

```
Agent runtime (graph.py, LangGraph tool loop)
      ↓  tool registry (harness/tools.py — the ONE seam)
File system tools (code/fs_tools.py)      ← 31 tools
      ↓
FileSystemService (code/fs/service.py)    ← deterministic, LLM-free facade
      ↓
core         errors.py · permissions.py · resolver.py
operations   list/read/write/create/delete/move/copy/rename/mkdir/rmdir
search       search_text/search_files/find_files (ripgrep + fallback)
symbols      search_symbols/find_references (ast + brace/regex, extensible)
editing      patch_file/replace_text/insert_text/delete_text/apply_diff/multi_file_patch
verify       SyntaxValidator / ChangeValidator / EditVerifier
metadata     file_metadata/file_hash/file_permissions
git          status/revision (additive, never required)
transactions · diff · index · watch (native + polling) · intent
      ↓
Operating system
```

The core remains deterministic and **not coupled to the LLM**. The LLM decides
*what* to change; this subsystem decides *how* to perform it safely. It is usable
by a CLI, UI, background worker, future sub-agent or MCP server, not only by a
model.

## What it does NOT re-implement

Path security is the existing authority, used unchanged:

- `code/policy.py` — `resolve_within`, `decide_write`, `PROTECTED_PATHS`,
  `IGNORED_DIRS/GLOBS`, the autonomy grants.
- `code/workspace.py` — `confine`, the realpath/symlink/junction escape checks.

`fs/resolver.py` only maps a `WorkspaceDenial` onto the standard `FsError`
vocabulary and adds write-target handling. A grep for `realpath` under
`python/harness/code/` still returns only `workspace.py` and `policy.py`.

The disposable workspace (`code/worktree.py`) is the write target. Filesystem
writes never land in the checkout directly; `edit_verify` writes go to a
worktree/snapshot copy, and `direct` mode writes to a folder the user explicitly
chose and approved. Reads use the server-resolved root (`ctx.workspace`).

## The permission model

`fs/permissions.py` is the ONE place a filesystem capability decision is made.

Capabilities: `read`, `write`, `create`, `delete`, `move`, `copy`, `rename`,
`watch`, `search`. Each maps to a code-policy verb; mutating capabilities run the
full `policy.decide_write` gate (profile + protected + ignored + confinement).
An operator config (e.g. `{"delete": false}`) can only NARROW; it can never grant
a capability the autonomy profile lacks. No tool hard-codes a permission check —
a handler calls `permissions.require` and maps the `FsError`.

A write is gated three times: the autonomy allowlist (structural — the tool is
removed from the model's schema at `plan_only`), the effect authorizer
(`make_code_authorizer` admits `FS_WRITE_TOOLS`), and the handler →
`permissions.require` → `decide_write` per path.

## The edit lifecycle

Preferred workflow for an existing file: search → read region → understand →
generate a MINIMAL patch → validate → hash-check → apply → verify. Never "read
whole file → rewrite whole file".

- **Ambiguity is refused.** `replace_text` with a repeated target returns
  `AMBIGUOUS_PATCH` and the candidate lines; it never replaces the first hit.
  `code_edit`'s `replaceAll` (or `count=0` at the engine) is the explicit way to
  change every occurrence.
- **Tolerant matching (2026-11-21).** Literal edits are tried in tiers, best
  first: exact bytes (tolerating the file's LF/CRLF convention) → typographic
  look-alikes (curly quotes, en/em dashes, NBSP/thin spaces) → per-line trailing
  whitespace. The fuzzy tiers run on an LF view and map spans back, so a CRLF
  file matches the text `read_file` showed without shifting offsets. A miss
  returns the closest numbered lines in `details["hint"]` (`_miss_hint`, 12 lines
  × 200 chars) — never the whole file.
- **Replacement endings.** A replacement written with `\n` into a CRLF file is
  converted to the file's dominant ending, so an edit cannot silently mix
  endings. A regex replacement is normalized the same way.
- **Media reads (2026-11-21).** `read_file` recognises PNG/JPEG/GIF/WebP/PDF by
  MAGIC BYTES, not extension, up to 20 MiB (`code/media.py`). It returns a media
  payload and the tool turn appends a real `image_url` (images) or `file` (PDF)
  part after the tool result, so the next model step can see it. Any other binary
  still refuses `BINARY_FILE`; media reads use a read-only `allow_ignored`
  resolution (images are in the write-ignore globs) while protected paths stay
  blocked.
- **Stale detection.** Every mutable op accepts `expectedHash`; a mismatch
  returns `STALE_FILE` with expected/actual hashes.
- **Atomic writes.** `write_text_atomic` writes a temp file, fsyncs, and
  `os.replace`s it — a crash never leaves a partial source file.
- **Preserved bytes.** Encoding (UTF-8 / UTF-8 BOM / UTF-16), BOM and the
  dominant line ending (LF/CRLF) are detected, preserved on write, and reported.
- **Rollback.** `FileTransaction` captures the original bytes (and directory
  trees) before mutation; `multi_file_patch` validates ALL patches before writing
  ANY, and rolls back on failure — a half-applied multi-file change never
  survives. Safety does NOT depend on Git.
- **Post-write formatter (2026-11-21).** After the atomic write, the PROJECT's
  formatter runs on that one file: local `prettier` (a configured dependency or
  config file), configured `ruff`, `gofmt`, or a configured `rustfmt`
  (`code/formatting.py`). It is bounded (10 s, 1 MiB), runs through the sandbox,
  and is non-fatal; `BE_FORMAT_ON_EDIT=0` disables it. The diff and hash describe
  the formatted bytes. When formatting changed the text, the exact-content
  verification is skipped (whitespace differs) and the syntax/hash checks still
  run.
- **Post-write diagnostics (2026-11-21).** The result carries diagnostics for the
  changed file. Structural syntax checks run synchronously; LSP diagnostics run
  in a daemon thread with a 2 s deadline and only when the CIUE runtime already
  has state for the workspace (`code/post_edit.py`). A slow server degrades to
  `lsp: "timeout"` with the structural result; the edit never waits on it. The
  model-facing text names the formatter and up to five diagnostics.

## File checkpoints (`code/checkpoints.py`, W7)

`FileTransaction` protects one operation and discards its snapshot on success.
W7 adds a *turn-level* undo surface on top of it:

- **Shadow-git store.** One bare repo at
  `<project>/.black-eclipse/checkpoints/store`; snapshots are commits under
  `refs/eclipse/<project-hash>/<stamp>-<checkpoint-id>`. `.black-eclipse/` is
  already self-ignored, so nothing appears in the user's `git status`.
- **One snapshot per run/turn.** The first mutating `FileSystemService` call,
  a write-shaped terminal command, or a promotion apply takes it; later changes
  reuse it. A temporary `GIT_INDEX_FILE` means the user's index is never read or
  written.
- **Excludes/protected.** `.git`, `.black-eclipse`, dependency/build dirs,
  databases, `.env*`, secrets, artifacts, symlinks outside the root and files
  over 10 MB are never captured or restored. `policy.is_protected` /
  `policy.is_ignored` remain the authority.
- **Modes.** `HARNESS_FILE_CHECKPOINTS=off|warn|enforce` (`on` aliases
  `enforce`). **Default `warn` (2026-11-21):** snapshots are taken for every
  coding turn; a missing snapshot is recorded and the write proceeds. `enforce`
  refuses the write rather than lose the undo point.
- **Per-turn undo (2026-11-21).** The graph stamps `ctx.checkpoint_turn` at each
  tool turn, so the file service takes ONE snapshot per turn (not per run).
  `checkpoint_undo_turn` restores every tracked file to the snapshot taken
  before the latest turn; the Code screen's snapshot panel exposes it as
  "Undo last turn". The restore is approval-gated and takes a `pre-restore`
  snapshot, so the undo is itself undoable.
- **Restore.** `checkpoint_list` / `checkpoint_diff` are read-only;
  `checkpoint_restore` is effectful and approval-gated. A restore takes a
  `pre-restore` snapshot first, defaults to the named paths, and `all` is
  explicit. Untracked files created after the snapshot are never deleted.
- **Routes and `/rollback`.** `/code/checkpoints*` is operator-gated; the
  routed `/rollback [id] [--all]` command never opens an agent turn.
- **Bounded.** Store size (`HARNESS_FILE_CHECKPOINT_MAX_MB`) and retention
  (`HARNESS_FILE_CHECKPOINT_RETENTION`) are enforced; a full store refuses a
  new snapshot rather than filling the disk.

## Search, symbols and the index

- `search_text` prefers `ripgrep` (fast, `.gitignore`-aware) and falls back to a
  bounded Python scan; both return the same structured shape and name the engine.
  An invalid regex is refused in-process before any engine runs. `path` may name
  a folder OR a single file (a file is searched directly instead of raising
  `NOT_A_DIRECTORY`); the summary states the match mode — literal vs regex — so a
  literal zero-match is never read as "this code does not exist". `contextLines`
  accepts 0-20.
- `search_symbols`/`find_references` use real parsers where they exist: Python
  uses the stdlib `ast` (functions, methods, decorators, classes, imports,
  constants), and JS/TS use a brace-depth line scanner that distinguishes a
  top-level function from a class method and recognises arrow-function consts.
  Go/Rust/Java/C#/C/C++ get targeted patterns. `_EXTRACTORS` is the extension point
  for a full parser (tree-sitter/LSP) per language.
- `find_references` marks each hit `definition: true` when it is the symbol's
  declaration site (from the index's `definitions` column or a live extraction),
  so the caller can tell a definition from a usage. The engine is always named
  (`index` or `regex`). The index is a CACHE, not an authority: it is refreshed
  incrementally before being trusted, so a file created or changed since the last
  indexing pass is never invisible (it skips the extra walk when an active watcher
  already keeps the index current).
- The workspace index is a **separate** SQLite store under
  `<repo>/.black-eclipse/workspace-index/<workspace>.db` — never
  `agent-memory.db`, never `eclipse-memory.db`, never `jdds.db`. It is a CACHE,
  updated INCREMENTALLY (only changed files are re-parsed); every query can fall
  back to a live scan. Schema additions (e.g. `definitions`) are applied
  additively to an existing database.
- `edit_file`'s instruction is parsed DETERMINISTICALLY (`fs/intent.py`): a
  concrete "rename X to Y" / "add X after Y" / "delete X" yields a minimal edit,
  while a vague instruction ("make it better") yields None and the tool returns
  the inspection packet instead of guessing.

## Watchers

`WorkspaceWatcher` uses a **native OS backend** when one is available — Windows
`ReadDirectoryChangesW` (an overlapped read on a directory handle) and Linux
`inotify` — both built on `ctypes`/`libc` only, so zero-config still holds
(`fs/watch_native.py`). Event kinds are normalized to
`created/modified/deleted/renamed`. A native backend that is unavailable at
creation OR fails at any point transparently degrades to the portable **polling**
watch, and the fallback re-baselines its snapshot so it reports only future
changes. Debounce collapses a burst for one path; on an event the hash/metadata
cache is invalidated and only the affected files are re-indexed. `status()`
reports which backend is active.

## Error codes

`fs/errors.py` is the ONE vocabulary. The standard set (`STALE_FILE`,
`AMBIGUOUS_PATCH`, `PATCH_CONTEXT_MISMATCH`, `FILE_EXISTS`, `INVALID_ENCODING`,
`TRANSACTION_FAILED`, …) plus the confinement codes `confine` already emits
(`PATH_TRAVERSAL`, `PATH_OUTSIDE_WORKSPACE`, `SYMLINK_ESCAPE`, `PROTECTED_PATH`,
`IGNORED_PATH`). Each refusal carries a one-sentence `recovery` hint so the agent
can recover autonomously. A tool result is `{status, code, message, path,
recovery?, details?}`.

## Verification (`fs/verify.py`)

Verification is part of the editing lifecycle, not a separate build step.

- **`SyntaxValidator`** — a per-language, cheap, deterministic syntax check.
  Python uses the stdlib `ast`; JSON/JSONC use `json`; TOML uses `tomllib`; JS/TS,
  Go, Rust, Java, C#/C/C++ use a bounded, string/comment-aware bracket-balance
  scan. It never runs a build tool per edit. Exposed as the `verify_file` tool.
- **`ChangeValidator`** — `validate_changes` checks a set of proposed edits
  WITHOUT applying any (path allowed, target present/missing as declared, hash
  intact, replacement span unambiguous). The same all-or-nothing rule
  `multi_file_patch` enforces before it writes.
- **`EditVerifier`** — every successful `write_file` / `create_file` / patch /
  insert / delete carries a `verification` object on its result: the file exists,
  is readable, its hash matches what was reported, the new content is present, and
  it parses. A failed check is DATA (never an exception): the agent sees
  `verification.verified: false` with the syntax error and line immediately.

## Git awareness (`fs/git.py`)

Additive and never a hard dependency. When the folder is a checkout it reports
each path's normalized state — `modified` / `untracked` / `staged` / `deleted` /
`renamed` / `copied` / `conflicted` — plus the branch and HEAD. It is surfaced via
the `git_status` tool and as a `git` field on `file_metadata`. Every child process
is one fixed, argument-free argv (no model text reaches git). Outside a checkout,
or with git absent, it returns `available: false` with an honest reason; the
subsystem works identically in a repository and a plain directory.

## Observability

Every tool emits a content-free `fs.*` event (`fs.write_file`, `fs.patch_file`,
`fs.patch_file.refused`, …) with tool, path, workspace id, dry-run flag, bytes
written, duration. A transactional edit (`apply_diff`, `multi_file_patch`) also
emits `fs.transaction.started` / `.committed` / `.rolled_back`, and
`index_workspace` emits `fs.index.updated`. No file content is ever logged.

## Extending it

| To add… | Do this |
|---|---|
| A filesystem operation | Add a function in `code/fs/operations.py` taking an `FsContext`, call `ctx.require(...)` + `resolver.resolve(...)`, snapshot via `FileTransaction` for mutations. |
| A tool | Add an input model + JSON schema + handler in `code/fs_tools.py`; put its name in `_READ_TOOLS`/`_WRITE_TOOLS`; if it mutates, add it to `code/policy.FS_WRITE_TOOLS`. |
| A language parser | Add an extractor to `_EXTRACTORS` in `code/fs/symbols.py` (signature `(path, text, language) -> list[Symbol]`); a JS/TS-style scanner can be added to `_regex_symbols` for a brace-based language. |
| A search engine | Add a branch in `search.search_text` returning the shared match shape; keep ripgrep as the preferred path. |
| A native watch backend | Add a `NativeWatchBase` subclass in `code/fs/watch_native.py` and return it from `create_native_watcher` for the platform; `watch.py` normalizes its raw events. |
| A new permission capability | Add it to `CAPABILITIES` and `_CAPABILITY_VERB` in `code/fs/permissions.py`; it is then available to `FsContext.require`. |
| A syntax validator | Add a language branch to `validate_syntax` in `code/fs/verify.py`; it is then used by `verify_file` and every post-write verification. |
| A git read | Add a fixed argv tuple to `GIT_ARGV` in `code/fs/git.py`; no argument is ever model-supplied. |

## Tests

`python/tests/test_fs_*.py` (operations, security matrix, editing/ambiguity/stale/
rollback, search/index/watch, encoding/diff, permissions/errors, edit_file,
limitations, verification/git) plus `python/tests/fs_helpers.py`. The full suite
runs with `npm run test:py`.
