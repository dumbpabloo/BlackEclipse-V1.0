---
title: Code Intelligence & Understanding Engine (CIUE)
status: Current
last-verified: 2026-11-16
---

# Code Intelligence & Understanding Engine (CIUE)

**Who this is for:** anyone extending Eclipse's code understanding — adding a
language server, a language, a parser, an index table, or a context-selection
rule. Read this before touching `python/harness/code/intel/`.

CIUE makes Eclipse understand software **structurally and semantically** instead
of treating the repository as text. The agent asks `code_get_definition`,
`code_find_references`, `code_find_symbol`, … and CIUE decides whether the answer
comes from a language server (LSP), Tree-sitter, the AST layer, the local index or
the project analyzer — and it always names which engine answered.

## Where it sits

```
Agent runtime (graph.py, LangGraph tool loop)
      ↓  tool registry (harness/tools.py — the ONE seam)
CIUE tools (code/intel_tools.py)                 ← 26 tools
      ↓
CodeIntelligenceManager (code/intel/manager.py) ← deterministic, LLM-free
      ↓
lsp/          LSPManager · LSPClient · LSPTransport · providers
ast.py        ASTEngine (delegates to code/fs/symbols.py)
treesitter.py TreeSitterEngine (optional; honest fallback)
persistence/  CodeIndex  (a DEDICATED SQLite store)
indexer.py    IncrementalIndexer
dependencies.py · diagnostics.py · search.py · project.py · context.py · editing.py
      ↓
code/policy.py + code/workspace.py  (path safety — the ONE authority)
code/fs/                            (reading, encoding, watching — reused)
```

The engine is **not coupled to the LLM**: a CLI, a UI, a background worker or an
MCP server can call the same manager. The LLM decides *what* to find; CIUE
determines *how* to find it safely and deterministically.

## What it does NOT re-implement

- **Path security** — `code/policy.py` (`resolve_within`, `decide_write`,
  `PROTECTED_PATHS`, `IGNORED_DIRS/GLOBS`) and `code/workspace.py` (`confine`).
  `intel/editing.py` calls them; a grep for `realpath` under `python/harness/code/`
  still finds only `workspace.py` and `policy.py`.
- **File reading / encoding / watching** — `code/fs/encoding.py`,
  `code/fs/watch.py` (+ `watch_native.py`), `code/fs/symbols.py` (the reviewed,
  extensible extractor table).
- **The tool registry / event emitter / authorizer** — the existing seams
  (`harness/tools.py`, `code/events.py`, `code/tools.make_code_authorizer`).

## The unified code model

`code/intel/models.py` is the one normalized representation. LSP, Tree-sitter, the
AST engine and the index each produce different shapes; they are normalized into
`CodeSymbol`, `CodeReference`, `CodeDiagnostic`, `CodeImport`, `CodeDependency`,
`CallEdge`, `CodeLocation`, `CodeFile`, `ProjectInfo` — every one with a compact
`to_dict()`. The agent never sees a backend's private structure.

## The manager

`CodeIntelligenceManager` (`manager.py`) is the one orchestration surface. It:

- indexes lazily and **incrementally** (only changed files are re-parsed);
- tries **LSP first**, then falls back to the structured index/AST — never the
  reverse, and never a crash. Every result names its `engine`
  (`lsp` / `tree-sitter` / `ast` / `index`);
- **refreshes before answering** when the index may be stale (`_ensure_fresh`),
  so a known file mutation is never answered from stale semantic data;
- keeps the dependency graph lazy (invalidated on any index change).

## Language servers (LSP)

`code/intel/lsp/`:

- `transport.py` — content-length framed JSON-RPC over the child's stdio, one
  reader thread, request/response correlation by id, bounded reads.
- `client.py` — one server process: handshake, capabilities, document sync, and
  typed request helpers (definition, references, hover, document symbols,
  diagnostics, implementations, call hierarchy, rename, code action). The child
  gets a scrubbed environment (`code.sandbox.build_env`) and a deterministic
  tree-kill.
- `provider.py` / `providers.py` — the provider registry. **CIUE is not coupled to
  one language server**: adding a server is adding a `LanguageServerSpec` to
  `_SPECS`. The shipped set covers TypeScript/JavaScript
  (`typescript-language-server`), Python (`pyright-langserver`), Java (Eclipse
  JDT LS), C# (`csharp-ls`/OmniSharp), C/C++ (`clangd`), Go (`gopls`), Rust
  (`rust-analyzer`), PHP (`intelephense`/`phpactor`) and SQL
  (`sql-language-server`). Providers whose program is missing resolve to
  `LANGUAGE_SERVER_MISSING` and are never launched.
- `manager.py` — routes a request to the `(root, provider)` client, caches
  capabilities, bounds the number of live clients, reaps dead servers and restarts
  a crashed one a bounded number of times. Multi-root and multi-server by key.

**A language-server failure never crashes the runtime.** If LSP fails:
restart → retry → fall back to the AST/index. The failure is a structured code
(`LANGUAGE_SERVER_MISSING` / `LANGUAGE_SERVER_FAILED`), a diagnostic, and an event
(`lsp.failed`); the tool still answers from the structural layer.

### Installing / configuring a language server

CIUE does not install servers. Any of the provider executables on `PATH` is
discovered automatically. Overrides and bounds live in `code/intel/config.py`
(env vars in the docstring there): `HARNESS_CODE_INTEL` (off switch),
`HARNESS_CODE_INTEL_LSP` (disable servers only), `HARNESS_CODE_INTEL_LSP_TIMEOUT`,
`HARNESS_CODE_INTEL_INDEX_DIR`, `HARNESS_CODE_INTEL_MAX_FILES`,
`HARNESS_CODE_INTEL_REFRESH_S`.

Both index stores can be relocated away from the checkout's `.black-eclipse/`:
`HARNESS_CODE_INTEL_INDEX_DIR` for the CIUE store, `HARNESS_WORKSPACE_INDEX_DIR`
for the workspace-filesystem store (`code/fs/index.py`). The test suite sets
both to one temp dir (`python/tests/conftest.py`), so a run never leaves
per-workspace DBs behind; `npm run clean:junk` sweeps any that accumulate.

## Tree-sitter and the AST engine

Tree-sitter (`treesitter.py`) is an **optional** fast structural parser. It is
imported lazily; when no grammar is installed every method raises
`IntelUnavailable("tree-sitter")` and the AST engine proceeds without it — zero
extra installs is preserved.

`ast.py` (`ASTEngine`) is the structural ground truth. It **delegates** per-language
extraction to `code/fs/symbols.extract_symbols` (Python uses the stdlib `ast`;
JS/TS use the brace-depth scanner; others use targeted regex), augments with
tree-sitter spans when available, and normalizes into `CodeSymbol`. It also
extracts routes (framework-aware) and database models, and resolves local imports
into `CodeDependency` edges.

## The dedicated code index

`persistence/index.py` (`CodeIndex`) is a **separate SQLite store** in
`<repo>/.black-eclipse/code-index/<project>.db` — never `agent-memory.db`, never
`eclipse-memory.db`, never `jdds.db`. Tables: `projects`, `files`, `symbols`,
`references_`, `imports`, `dependencies`, `call_edges`, `diagnostics`, `meta`,
with indexes on the query columns. `schema_version` lives in `meta`.

- **Incremental only.** `indexer.IncrementalIndexer.index_workspace` walks
  ignore-aware, skips files whose `mtime/size/hash` are unchanged, re-parses
  changed files, and deletes rows for removed files. A single edit calls
  `reindex_file`. A content-identical touch refreshes mtime only.
- **A cache, never an authority.** Every query can fall back to a live scan.
- **Recovery.** A corrupt database is detected (`integrity_check`), backed up
  (`*.corrupt-<stamp>.db`), recreated and re-indexed (`CodeIndex.recover`). The
  index is never a single point of failure — the engine still works from LSP and
  the filesystem while it rebuilds.

## Incremental indexing & the watcher

`runtime.py` (`CodeIntelRuntime`, one process-wide) owns one `LSPManager` and a
per-workspace `_WorkspaceState`: an `IncrementalIndexer` plus the filesystem
subsystem's `WorkspaceWatcher` (native Windows/Linux backends, polling fallback).
The watcher's debounced events drive `apply_watch_events` → `reindex_file`/
`remove_file`, so only affected data is recomputed. Boot indexing is
**asynchronous** (`_index_async`); a query never triggers a full synchronous walk.

## Diagnostics

`diagnostics.py` merges two sources into `CodeDiagnostic`: LSP
(`publishDiagnostics` push or `textDocument/diagnostic` pull) and the structural
syntax check (`code/fs/verify.validate_syntax`, always available). "No diagnostics
available" is distinct from "no problems". The engine records diagnostics per file
in the index so the edit→diagnostics→fix loop survives across runs.

## Semantic editing

`editing.py`. Rename is **never** a regex find-and-replace:

- with a language server, CIUE asks for a semantic `WorkspaceEdit` and applies
  exactly that (`parse_workspace_edit` → `apply_file_edits`), path-confined to the
  workspace and rebased from the read-only root onto the disposable workspace
  (`rebase_edits`);
- without one, it produces a deterministic, **preview-first** edit set from the
  index (declaration + references, whole-identifier boundaries only) and names the
  engine `index` — never presented as semantic.

`code_rename_symbol` previews by default (`dryRun=true`). Applying requires
`code.write` (removed at `plan_only`) and writes through the fs subsystem's atomic
encoder, so a rename cannot escape the workspace or leave a partial file.

## The context engine

`context.py` (`CodeContextEngine`) prevents repository-wide context dumping. For a
request it extracts candidate terms deterministically (no embeddings — the
contract is explicit that deterministic code intelligence comes first), finds
matching symbols, expands each with references, callers/callees and diagnostics,
identifies related tests, and returns a compact packet of files, symbols, call
paths and tests. Everything is bounded.

`find_related_tests` identifies which tests are relevant (conventional test paths +
reference match) so the agent can run them through the existing terminal
subsystem — it is not a test runner. Its reference set is resolved ONCE PER
NAME: doing it per candidate test file re-ran the same SQL query tens of
thousands of times on a real repo (94 s of blocked event loop).

### The pre-model wall-clock budget (invariant)

The repo map and the seeded slice are assembled in a run's pre-model phase,
before the first model call. Nothing there may hold the runtime's event loop
unbounded — while it is blocked, every other route, the stall watchdog and the
user's turn are frozen.

- **The work runs on a worker thread, never the loop.**
  `graph_context._repo_map_context` builds the map + seeded slice through
  `_build_repo_context_sync` inside `asyncio.to_thread`, under
  `asyncio.wait_for(…, budget + 2 s)`. A wall-clock budget alone was not enough:
  a cold, disk-busy host turned a 5 s budget into **137 s** (measured
  2026-11-24), because a blocking file read does not observe a deadline. The
  outer timeout is the guarantee; the thread is abandoned on overrun and its
  result discarded (`code.repo_map_timeout` is emitted).
- `CodeContextEngine.select(..., budget_s=…)` checks a `time.monotonic()`
  deadline throughout its loops and returns what it has with `truncated: true`.
  Default `5 s`, env `BLACK_ECLIPSE_CODE_CONTEXT_BUDGET_S`.
- `build_repo_map(..., deadline=…)` applies the same wall-clock cap, checking it
  **per file** (not only per directory: one directory can hold thousands of
  entries) and skipping any single file larger than `MAX_OUTLINE_FILE_BYTES`
  (1 MB), since a parse cannot be interrupted. The map gets at most half the
  budget, the seed the remainder (`graph_context._repo_map_context`).
- Opening a large workspace index (seconds; hundreds of MB on disk) happens at
  BOOT on a worker thread (`CodeIntelRuntime.warm`, called from
  `boot.py::_warm_code_index`) and NEVER inside a run. Indexing itself stays
  lazy: `warm` opens the state without scanning; the first real use starts the
  asynchronous incremental index.

A code turn that runs out of budget still proceeds — the slice is a
convenience, and `code.repo_map_built` reports `truncated`/`elapsedMs`.

## The agent tools

`code/intel_tools.py`, registered by `code/tools.register_code_tools` on the SAME
registry. Read tools: `code_get_definition`, `code_find_references`,
`code_find_symbol`, `code_search_symbols`, `code_get_hover`, `code_get_type`,
`code_get_diagnostics`, `code_get_document_symbols`, `code_get_callers`,
`code_get_callees`, `code_get_call_hierarchy`, `code_get_implementations`,
`code_get_imports`, `code_get_exports`, `code_get_dependencies`,
`code_get_dependents`, `code_get_project_structure`, `code_get_dependency_graph`,
`code_parse_file`, `code_analyze_file`, `code_organize_imports`,
`code_get_relevant_context`, `code_index_workspace`, `code_intel_status`,
`code_find_related_tests`. The one write tool is `code_rename_symbol`.

Outputs are structured, compact, deterministic and bounded: a JSON `text` for the
model plus the structured `data` record for the front door. Source is returned
only as focused ranges (a location + the single context line), never a dump.

## HTTP routes

`routers/code_intel.py`: `GET /code/intel/status`, `GET /code/intel/project`,
`POST /code/intel/index`, `POST /code/intel/servers/start|stop`,
`POST /code/intel/servers/{language}/restart`. Reads and mutations are
operator-gated by transport/token (`require_operator`), mirroring the other code
routes. `/health` reports a content-free `codeIntel` block.

## Security

Workspace boundaries are respected through the existing authority: path traversal
is impossible (every write path goes through `resolve_within`), indexing never
leaves the workspace, protected/secret files are skipped, and LSP processes run
under the same scrubbed-environment, tree-kill model as the terminal subsystem.
A missing workspace is a refusal (`WORKSPACE_UNAVAILABLE`), never a guess.

## Observability

Content-free events on the run/process bus: `lsp.starting`/`lsp.started`/
`lsp.failed`/`lsp.stopped`/`lsp.shutdown`, `intel.index.completed`/
`intel.index.file`/`intel.index.incremental`, `code.intel.<method>`,
`code.intel.refused`, `code.intel.lsp_fallback`, `code.intel.rename.*`. No file
content and no secret is ever logged.

## Configuration

`code/intel/config.py` (`IntelConfig`, `load_config`) — see the env vars above.
Sensible defaults: enabled, LSP enabled, 60 000-file bound, 15 s LSP timeout, 30 s
refresh window.

## How to add another language

1. **Extraction** — add an extractor to `code/fs/symbols._EXTRACTORS` (or extend
   `_regex_symbols`) for structure. The AST engine picks it up automatically.
2. **Tree-sitter (optional)** — add the grammar to `treesitter._GRAMMAR_MODULES`
   and `_FUNCTION_NODES`/`_CLASS_NODES`.
3. **Language server** — add a `LanguageServerSpec` to `lsp/providers._SPECS`
   (`provider_id`, `languages`, `argv`, optional `fallbacks`). No other change.
4. **Dependencies / routes / models** — extend `ast.py`'s `_raw_import_targets`,
   `_ROUTE_PATTERNS`, `_MODEL_PATTERNS`.
5. **Detection** — add the extension to `language/detect._LANGUAGE_BY_EXT`.
6. Add the language to a fixture project and a test.

## How to debug LSP failures

- `code_intel_status` (tool) or `GET /code/intel/status` (route) shows each
  server's state (`running`/`failed`/`missing`), pid and restart count.
- A `LANGUAGE_SERVER_MISSING` means the executable is not on `PATH` — install it
  or point `HARNESS_CODE_INTEL_*` at a custom location.
- A `LANGUAGE_SERVER_FAILED` carries the last error; `POST
  /code/intel/servers/{language}/restart` retries after a fix.
- Every fallback emits `code.intel.lsp_fallback` with the operation and error, so
  a tool that silently used the index is still observable.

## Tests

`python/tests/test_intel.py` (manager/index/graph/diagnostics/context/editing),
`test_intel_tools.py` (the tools through the ONE registry, incl. missing/failed
LSP fallback), `test_intel_lsp.py` (a REAL fake language server subprocess —
framing, handshake, requests, crash handling), `test_intel_api.py` (the routes).
Fixtures `python/tests/fixtures/intel/{typescript,python,go}-project/`.
`python/tests/fake_lsp.py` is the fake server. Run with `npm run test:py`.
