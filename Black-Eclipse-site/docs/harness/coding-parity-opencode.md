---
title: Coding parity — Eclipse vs OpenCode
status: Current
last-verified: 2026-11-21
---

# Coding parity — Eclipse vs OpenCode

**Who this is for:** anyone improving Eclipse's agentic coding ability. It is a
point-in-time, evidence-backed comparison of Eclipse's coding path against
OpenCode `v2.3.0` (repository `sst/opencode`, commit `6531f17`, `dev` branch,
read from a clean clone on 2026-11-21). Every OpenCode claim cites a path and
line in that tree; every Eclipse claim cites this repository.

This is deliberately **not** a scoreboard. OpenCode is better at some things,
Eclipse at others. The last section ranks what to fix first.

## How this was measured

- Source read: `packages/core/src/**` (session runner, tools, permissions,
  agents, skills, instructions, MCP, plugins, formatter, snapshot), plus the
  prompt templates in `packages/core/src/plugin/system-prompt/`.
- Eclipse source read: `python/harness/{graph.py,tool_turn.py,tool_disclosure.py,
  compaction.py,agents.py,prompts.py,code/**,terminal/**}` and the coding UI
  under `src/components/screens/code/**`.
- Numbers below are constants from the source, not estimates, unless marked
  approximate.

Caveat on versions: OpenCode `v2` is a rewrite of the released `v1` line, and
several v1 features are visibly still being ported — the code itself carries
TODOs for snapshots, LSP diagnostics and watcher events, even though the
supporting snapshot/git modules already exist. Read every "OpenCode does not
have X" below as "v2.3.0 does not have X", not as a claim about the released
product. Eclipse is compared against the `v2` development tree because that is
the architecture its next releases ship.

## 1. The agent loop

| Concern | OpenCode `v2.3.0` | Eclipse |
|---|---|---|
| Step limit | No global default; an agent may set `steps` (`schema/src/agent.ts:33`). At the cap it appends a "respond with text only" prompt and sets `toolChoice: none` (`session/runner/llm.ts:261-280`). | Chat default 24, ceiling 32 (`graph.py:214-251`); a coding autonomy gets **48 steps / 20 min** (`graph.py:551-555`); code sessions ladder 24 / 80 / 240 (`code/session.py`); hard cap 240 (`graph.py:279`). |
| Tool parallelism | Every non-provider tool call is forked as its own fiber in the same turn (`session/runner/step.ts:149-163`), then joined with `Fiber.awaitAll` (`:185-189`). | Contiguous `parallel_safe` calls batch (`tool_turn.py:11-12,43-54`). Since 2026-11-21 the default is **on** and the read-only fs/CIUE/workspace tools opt in (`config/flags.py`, `code/fs_tools.py`, `code/intel_tools.py`, `code/tools.py`). |
| Streaming | Text/reasoning/tool-input deltas coalesced on a 100 ms batch (`runner/publish-llm-event.ts:107,207-212`); durable `Step.Started/FirstToken/Streamed` events. | SSE from the TS front door (`run-service.ts:296-300`); Python emits run events through the graph. |
| Abort | Cooperative stop: tools with a cancel gate get 5 s to settle, then fibers are interrupted (`step.ts:110-118,410`). | Cooperative `cancel_event`; sandboxed children killed with their tree (`graph.py:1511-1515`, `sandbox.py:472-481`). |
| Mid-run steering | Inbox with `steer` delivery, promoted only at a step boundary (`session/prompt.ts:52`, `runner/llm.ts:94-101`); does not interrupt an in-flight model call. | Operator steers drained at one step boundary, fenced as `<operator_steer>` (`graph.py:892-925`). **Equivalent design.** |
| Retries | 10 retries, 2 s→10 s exponential; network errors get a 1-hour budget; timeouts capped at 3 (`model-retry.ts:22-86`); a retry after output started is only transparent for an interrupted stream (`step.ts:248-263`). | One `RetryPolicy` per step; retry refused after text streamed (`step.py:9-13,147-172`); one context-overflow retry with a tighter budget (`graph.py:2008-2086`). |
| Restart / resume | Event-sourced execution claim; on boot `resumeSuspendedSessions` replays orphaned claims up to 10 times, then terminalizes with `RESUME_EXHAUSTED` (`session/execution/restart.ts:30-48,101-123`). Background shells/subagents are re-attached. | Durable run checkpoints + `resume` (`graph.py`, `/runs/{id}/resume`); recovery modules under `harness/recovery.py`, `run_recovery.py`. |
| Auto-continue | On a `length` finish it injects `CONTINUE_AFTER_OUTPUT_LIMIT` and starts a fresh assistant message; 3 consecutive truncations fail (`runner/llm.ts:38-39,347-377`). | Finalizes at the step budget (`graph.py:979`) and returns `MAX_STEPS_EXCEEDED` (`:1282-1284`); no output-limit continuation documented. |

**Read:** OpenCode's loop is built to *keep going* (unbounded steps, parallel
tools, output-limit continuation); Eclipse's is built to *stop safely* (hard
caps, duplicate suppression, honest `MAX_STEPS_EXCEEDED`). For long coding
tasks, Eclipse's ceiling and serial tool calls are the bigger handicap.

## 2. Context management

| Concern | OpenCode | Eclipse |
|---|---|---|
| Budget source | Measured per-model window `min(limit.input, limit.context)`; reserve `max(15%, 16_000)` tokens (`compaction.ts:1173-1174,126-132`). | Measured model window with a char ceiling: `MAX_HISTORY_TURNS=20`, `MAX_HISTORY_CHARS=40_000` absolute (`history.py:30-31`), plus `context_budget.py`. |
| Compaction trigger | Before each step when measured + estimated ≥ ceiling (`runner/llm.ts:253-259`, `compaction.ts:430-439`). | When the history exceeds the measured tail budget; deterministic five-section summary + optional model briefing (`compaction.py`). |
| What survives | Newest ~15 000 tokens kept verbatim; summary template with Objective / Requirements / Decisions / Work State / Next Move / Relevant Files (`compaction.ts:266,170-242`). | Verbatim tail up to **15 000 estimated tokens** (60 000 chars) on a known window, keeping the newest turn even when it alone exceeds the tail; the dropped prefix becomes OBJECTIVE / DECISIONS / WORK STATE / NEXT MOVE / FILES (`context_budget.KEEP_TAIL_TOKENS`, `compaction.build_structured_summary`). Unknown windows keep the conservative 20 turns / 40 000 chars. |
| Background precompute | Summary written 60 000 tokens before the ceiling, inside the prompt-cache lifetime, one held summary per session, max 2 failures (`compaction.ts:155-168,348-402`). | A per-session held summary is produced in an `asyncio` task at ≥35 % measured pressure, covering a prefix that leaves ≤40 % of the verbatim tail (`context/precompute.py`). One per session; model failure fails closed to the deterministic five-section block; it is swapped in only when the dropped prefix covers it, the covered turn still matches, and the kept tail fits `keep_tail_tokens`. |
| Overflow path | Transcripts shortened in 4 levels (clip → remove outputs → user only → omit), newest exchange never omitted; 85/70/50/35 % shrink ladder (`compaction.ts:124,141-147,313-317`). | One overflow retry with a tighter history budget (`graph.py:2008-2086`). |
| Tool output | 2 000 lines / 50 KiB inline cap, full text to a retained file with a resume marker (`tool-output.ts:13-16,90-142`), 7-day retention. | 24 000-char cap at the registry seam with a non-JSON truncation trailer and a run-scoped `output_id` (`tools.py:69-89`). Comparable, but no file retention/lifetime. |
| Instruction updates | Durable `InstructionState` hash deltas per source, epoch reset at compaction (`session/instruction-state.ts:106-141`). | Project rules re-read deterministically and injected per run (`project_memory.py`); no delta protocol. |

**Read:** Eclipse now keeps the same order of verbatim working set (up to ~15 k
tokens), a structured summary of the rest, and prepares that summary before the
ceiling in a background task. OpenCode still leads on cache-lifetime scheduling
(it starts inside the provider's cache window and keeps a precomputed tail
share); Eclipse starts at a measured pressure threshold and keys the held
summary to the session, the covered turn hash and the tail budget.

## 3. Tool surface

### Size and shape

- **OpenCode:** ~15 first-party coding tools, sorted alphabetically, plus MCP
  tools (`packages/core/src/tool/plugin/*.ts`; names: `read`, `write`, `edit`,
  `patch`, `shell`, `grep`, `glob`, `todowrite`, `subagent`, `question`,
  `webfetch`, `websearch`, `skill`, `session-search`, `opencode`), and one
  optional `execute` (Code Mode) appended last (`tool.ts:383-388`).
- **Eclipse:** `CODE_AGENT_TOOLS` declares **615 list entries** (duplicates
  included; ~380 unique), `CHAT_CODE_TOOLS` **324**, and `register_code_tools`
  alone registers **97** tools before the rest of the app's registry is added
  (`agents.py:789-976`, `code/tools.py:1048-1269`). Since 2026-11-21 a coding
  profile is narrowed to the **`CODING_SURFACE` preset** (52 declared tools incl.
  four discovery tools; 44 bound at `edit_verify`) before binding:
  runtime/container/cloud/orchestration/
  package/browser/computer tools are removed structurally
  (`code/policy.py`, `surface.py`). They remain discoverable via `tool_search`
  (marked `outsideSurface`) and an operator can add one with
  `BLACK_ECLIPSE_CODING_EXTRA_TOOLS`; the model cannot widen its own surface.
  On a code registry the bound coding surface is **48 tools** vs 97 registered.

Eclipse's progressive disclosure (`HARNESS_TOOL_DISCLOSURE=auto`, 8 % of a known
window) hides only **read-only** tools; every effectful/interactive tool is
always bound (`tool_disclosure.py:206-211`). With the coding preset that is now
~10 effectful tools instead of 150, so disclosure and the preset compose rather
than compete. OpenCode's effectful surface is 8 tools.

### Per-tool parity

| Capability | OpenCode | Eclipse | Verdict |
|---|---|---|---|
| Read text, paging | 2 000 lines / 50 KiB, offsets, tail reads (`tool/read-filesystem.ts:12-21`) | 2 000 lines (`fs/operations.py:33-35`), `startLine`/`offset`/`limit` | parity |
| Read images / PDF | Magic-byte detection, ≤20 MiB, returns a media part (`read-filesystem.ts:21,131-143`; `read.ts:351-365`) | Magic-byte detection, ≤20 MiB, returns a media part; after the tool result the harness appends a HumanMessage with an `image_url` (images) or `file` (PDF) part (`code/media.py`, `tool_turn.py`) | parity |
| Directory read | Entries dirs-first + `next` cursor (`read-filesystem.ts:324-340`) | `list_directory` recursive/depth/limit | parity |
| Missing-path recovery | Case-insensitive retry, re-rooting, 4-tier fuzzy + "Did you mean" (≤3) (`read.ts:201-319`) | `code_edit` offers ≤5 paths; `read_file` now offers "Did you mean" candidates too (`fs/path_hints.py`) | parity |
| Exact edit | `oldString`/`newString`, unique or `replaceAll` (`edit.ts:315-350`) | `code_edit` + read ledger, unique or `expectedOccurrences`; **now** `replaceAll` | parity (fixed below) |
| Fuzzy edit | LF view + typographic + trailing-whitespace tiers; per-region line endings via LCS (`edit.ts:45-186`) | exact + LF/CRLF needle; **now** typographic + trailing-whitespace tiers + file-ending preservation | parity (fixed below) |
| Miss hint | "already applied" check, closest 12 numbered lines (`edit.ts:188-239`) | **now** closest numbered lines via `_miss_hint`; no "already applied" check | Eclipse partial |
| Multi-file patch | Freeform Lark grammar, add/delete/update/move, all validated before write (`tool/patch-grammar.txt`, `patch.ts:98-181`) | `apply_diff` unified diff + `multi_file_patch`, validated before write (`fs/editing.py:521-644`) | parity |
| Shell | Background with **completion notification**, timeout moves to background, output file, structured exit/signal/pid (`shell.ts:33-34,164-197`) | Three tiers (`command_run`, `terminal_run`, `shell_run/spawn`); a spawned process is watched and its exit/output tail arrives at the next step boundary (`background_jobs.py`), so polling is unnecessary (`shell_tools.py:216-224`) | parity |
| Persistent PTY | `persistent-pty` package; shells survive restart (`pty/`, `persistent-pty.ts`) | `terminal/` has a real PTY and persistent sessions (`docs/harness/terminal-architecture.md`), but the code-agent prompt routes long work to `start_process`; PTY tools are not the default coding path | Eclipse partial |
| Search | ripgrep, `--hidden`, globs, 100 default, 30 s bound (`ripgrep.ts:237-275`) | ripgrep-first with bounded fallback (`fs/search.py`) | parity |
| Todo | `todowrite` full-list tool, opt-in flag (`todo.ts`) | `code_todo` full-list tool, persisted per run (`CodeTodo`), injected at the step boundary only when it changed, rendered as a Code-screen panel (`code/todos.py`, `plan-panel.tsx`) | parity |
| Ask the user | `question` with options/free-form (`question.ts`) | `clarify` tool (`clarify_tools.py`) | parity |
| Web | `webfetch` 5 MiB, 30 s/120 s, redirects; `websearch` provider form (`webfetch.ts`, `websearch.ts`) | `web_fetch`/`web_search`/research engine | parity |
| Skills | `skill://` URIs readable by `read`, auto-invoked list in prompt (`skill.ts`, `read.ts:71-109`) | `skill_list`/`skill_view`, versioned procedures (`prompts.py:155`) | parity |
| Code Mode | `execute`: a restricted interpreter with `tools`, `search`, `fetch`, nested permission-checked calls (`packages/codemode/README.md:7-9`) | `tool_code`: Python, opt-in `HARNESS_CODE_MODE=1`, 20 calls (`code_mode.py`). Read-only tools run directly; an effectful call now runs through the SAME `registry.execute` authorizer/approval chain, run surface, effect token and widening cap (refused without an authorizer or outside the surface; no recursion). | parity |
| Media generation | image/video tools (`image-generate.ts`, `video-generate.ts`) | Vision/render pipeline exists, not as model tools in code profiles | N/A for coding |

## 4. Subagents

- **OpenCode:** `subagent` is always non-blocking; each child is its own job and
  session, runs concurrently, survives restart, and reports back as a
  `<background-result kind="subagent">` synthetic message (`tool/plugin/subagent.ts:17-31`,
  `session/subagent-job.ts:23-40`, `session/subagent-completion.ts:10,40-86`).
  Depth is configurable (`experimental.subagent_depth`, default 1), the agent
  must be a subagent, and model precedence is explicit → agent → parent
  (`subagent.ts:127-171`). Parallelism is free because every call is a fiber.
- **Eclipse:** `delegate` (split / best-of, max 4 children, depth 2, per-child
  model/tools/workspace) and durable `delegate_async` with
  status/result/cancel/steer/wait (`delegation.py:37-56`,
  `delegation_async_tools.py:309-387`). Children inherit the parent's surface
  and never widen authority. The async tools are real, but the **code profile
  does not enable them by default** (`agents.py:920-923` includes `delegate`;
  the async family is separate), and children are step-bound like the parent.

**Read:** Eclipse's delegation is more governed and auditable; OpenCode's is
more usable and truly parallel by construction.

## 5. Permissions, safety, and file mutation

Eclipse is **stronger** here, and the comparison should say so:

- Eclipse confines on resolved real paths with distinct codes for traversal,
  symlink/junction escape, outside-workspace, protected and ignored paths
  (`code/policy.py:283-319`); protects `jdds.db`, `.env*`, `.git/**` and its own
  policy modules (`:57-70`); writes atomically (temp + `os.replace` + fsync;
  `fs/encoding.py:131-160`) and rolls back through an in-memory transaction
  (`fs/transactions.py`).
- OpenCode resolves paths **lexically** — no `realpath` in `file-access.ts`,
  `file-write.ts`, `file-mutation.ts` — so a symlink inside the worktree can
  point outside it. Writes are direct `fs.writeFile`, with no temp/fsync
  (`environment/local.ts:48-52`). No read-before-edit ledger exists: the model
  may overwrite a file it never read.
- Eclipse's read ledger (`FILE_NOT_READ`, `STALE_FILE`) is a genuine advantage;
  OpenCode only fails when `oldString` stops matching.

OpenCode's permission model is nevertheless more granular in one dimension:
wildcard rules with last-match-wins, deny-short-circuit, per-project persisted
"always", batched re-evaluation of sibling requests, and an unattended ruleset
for schedules (`permission.ts:102-244,340-381`). Eclipse has approvals and
autonomy profiles, but not the same rule language.

## 6. Code intelligence, formatting, undo

- **Eclipse wins on code intelligence.** A real LSP client with nine providers
  and a `lsp → tree-sitter → ast → index` degradation ladder
  (`code/intel/manager.py:9-11`, `lsp/providers.py:10-94`), 25 `code_*` tools,
  a repo map and a relevance-seeded context slice (`intel/context.py`,
  `repo_map.py`). OpenCode `v2.3.0` has **no LSP runtime**: the config schema
  exists but is unused, and diagnostics are explicit TODOs
  (`tool/plugin/edit.ts:256`, `file-mutation.ts:182`).
- **Formatting:** OpenCode runs one of 27 built-in formatters after every
  write/edit (`formatter/builtins.ts:39-270`, `file-write.ts:82-89`). Eclipse now
  runs the project's own formatter after a write as well — local prettier,
  configured ruff, gofmt, or configured rustfmt, bounded and non-fatal
  (`code/formatting.py`) — but recognises fewer formatters (4 vs 27).
- **Post-edit diagnostics:** Eclipse also returns diagnostics for the changed file
  on the edit result: structural checks immediately, LSP in a bounded thread when
  the workspace is already indexed (`code/post_edit.py`). OpenCode has no LSP
  runtime; its diagnostics are a TODO (`tool/plugin/edit.ts:256`).
- **Undo:** OpenCode captures git-tree snapshots and can stage a revert per
  message (`snapshot.ts:88-217`, `session/revert.ts:26-90`). Eclipse now takes a
  shadow-git checkpoint **per turn by default** (`warn`), and
  `checkpoint_undo_turn` restores the pre-turn bytes through the existing
  approval-gated restore; the Code screen exposes "Undo last turn". Both keep a
  pre-restore snapshot, so an undo is itself undoable.

## 6.5 Extensibility, MCP, and prompts

- **MCP is parity or better in Eclipse.** Both support stdio and HTTP transports,
  OAuth, dynamic tool lists, resources and prompts. Eclipse adds supply-chain
  screening of stdio commands, approval-gated install, redirect/origin guards and
  per-server resource/prompt toggles (`python/harness/mcp/__init__.py:3-16`,
  `mcp/screening.py:150`, `mcp/utilities.py:20-46`). OpenCode's differences are
  protocol negotiation (`legacy | auto | 2026-07-28`), pooling, and a 12-hour
  default execution timeout (`mcp/client.ts:41-46`).
- **Plugins.** Both are extensible: OpenCode has transform editors and ~20 hook
  families with prefix-diff hot reload (`plugin/supervisor.ts:142-238`); Eclipse
  has a plugin platform plus the self-editing harness (`python/harness/plugins/`).
  No coding-path gap was found here.
- **Skills and instructions.** Both discover `AGENTS.md`/`CLAUDE.md`, expose
  skills to the model, and inject updates. OpenCode's edge is *delta* instruction
  state and `skill://` file reads; Eclipse's edge is versioned skills and a
  deterministic per-run project-rules block (`code/project_memory.py:35-59`).
- **Prompts (2026-11-21).** OpenCode ships model-family prompts
  (`plugin/system-prompt/{anthropic,gpt,gpt-astra,kimi,trinity,meta}.txt`) and
  appends tool-specific guidance only for tools actually visible
  (`session/system-prompt.ts:5-28`). Eclipse keeps ONE audited base coding prompt
  (the safety rules must not fork) and appends a short family guidance block chosen
  from the resolved provider/model: anthropic, gpt, kimi, trinity, meta, google,
  generic (`prompt_variants.py`). Finance turns are byte-identical; the block is
  deterministic per model, so the prompt-cache prefix stays stable.

## 7. The front door (what the user sees while coding)

Evidence-backed gaps in `src/`:

1. **Syntax highlighting (2026-11-21, gap #15)** — the Code-screen diff now
   highlights comments, strings, numbers, keywords and literals with a
   dependency-free tokenizer (`src/lib/code/highlight.ts`), and offers unified or
   side-by-side layout with a per-file tree (`diff-viewer.tsx`,
   `diff-file-tree.tsx`). Image changes render a **before/after** pair through
   `/api/code/tasks/<id>/image` → `worktree.read_image`: `after` reads the
   workspace file, `before` reads the base revision (git size probe, then
   `git show`), both path-confined to the workspace, magic-byte checked
   (PNG/JPEG/GIF/WebP/BMP only), and bounded to 2 MiB. A snapshot workspace has no
   previous bytes, so it says so rather than guessing. Other code surfaces
   (`src/components/chat/change-cards.tsx`) still use plain `<pre>`.
2. **Hunk-level accept (2026-11-21)** — the promotion preview exposes grouped
   hunks (`path#hN`) with a changed-line preview; the reviewer can keep one hunk
   and drop another. The digest covers only the selected hunks and their content
   hashes, and apply reconstructs the partial file from the target plus the kept
   hunks (`code/promotion.py`, `promote-change.tsx`). Creates/deletes must be
   accepted whole.
3. **Live Code-task progress (2026-11-21)** — the task id is the harness run id,
   so the Code screen subscribes to the existing `/api/harness/runs/<id>/stream`
   SSE proxy and coalesces events into one refetch (`code-run-stream.ts`,
   `code-screen.tsx`). The plan panel listens to the same stream instead of a 4 s
   poll.
4. **Image input for code tasks (2026-11-21)** — the Code composer accepts up to
   four PNG/JPEG/WebP images (2 MB each); `CreateTaskRequest.images` carries the
   same bounded `RunImage` the chat path uses into `RunInput.images`, which
   `turn_content` turns into real model image parts.
5. **No plan/act toggle on the Code screen** — autonomy is fixed to
   `edit_verify` there (`task-list.tsx:67,110`); `plan_only` is reachable only
   through the classifier, a slash command, or the API.
6. **No per-hunk rollback, no "revert this tool call".**
7. **No model/reasoning choice per code task** — resolved server-side
   (`src/app/api/code/tasks/route.ts:59-62`).
8. **Editor context reaches the agent (2026-11-21, gap #17)** — the editor publishes
   the open file plus the selected line range/text (bounded to 4,000 chars, 15-minute
   TTL); the composer shows an "Editor path:line" chip and sends it as delimited
   reference context while the visible bubble stays the user's own words
   (`src/lib/chat/editor-context.ts`, `editor-screen.tsx`, `chat-dock.tsx`).
9. **Diff source modes (2026-11-22, gap #18)** — the review view can compare the
   workspace against HEAD (`Changes so far`), the stored base revision via
   `base..HEAD` (`Committed`), a caller-named branch/revision (`Compare to…`), or
   the newest turn snapshot (`Last turn`, checkpoint-backed; a task with no
   snapshot says so). `worktree.diff` builds the spec and `--name-status` file lists
   for the revision modes, so the file list can never disagree with the text
   (`/api/code/tasks/<id>/diff?mode=`). `open-in-app` remains unimplemented.
10. **Reveal a reviewed file in the OS (2026-11-22)** — the diff tree's folder button
   asks the desktop shell to reveal the changed file through the `be-reveal-path`
   core event. Rust accepts only an existing absolute path and calls Explorer
   (`/select`), Finder (`open -R`) or `xdg-open` — it never executes the file, so a
   forged payload cannot launch a program. In a browser the button is absent
   (`src/lib/desktop/reveal.ts`).

OpenCode's TUI/desktop is a different product, but the parity items above are
product-level, not cosmetic: review granularity and live feedback change how
much a user can trust a coding agent.

## 8. Where Eclipse is already better

State this plainly, because it defines what **not** to copy:

- **Verification as a ledger.** A check "passes" only with a recorded zero exit
  code; unbacked claims are marked `UNVERIFIED` (`code/verify.py:33-75`).
  OpenCode has no equivalent.
- **Bounded repair.** `.black-eclipse/verify.json` pins checks; the graph runs
  them through the same command path and feeds failures back with a
  cost/wall-clock budget (`code/repair_graph.py`, `code/repair.py:17-19`).
- **Policy and sandboxing.** Real-path confinement, protected paths, network
  denial, Job Object / process-group isolation with honest refusal when
  isolation is unavailable (`code/sandbox.py:1-20,579-762`).
- **Code intelligence.** LSP + AST + index; OpenCode v2 has none.
- **Read-before-edit and stale-hash protection.** OpenCode v2 has neither.
- **Eval and capability accounting.** `code/eval/tasks.py`,
  `code/eval/capabilities.py` grade real fixtures and report `unmeasured`
  rather than mocking.

## 9. Ranked gap list (what to fix, in order)

Each item names the symptom a user feels, not the module.

| # | Gap | User-visible symptom | Fix direction | Status |
|---|---|---|---|---|
| 1 | Edit matching is brittle (was: exact only, LF insertion into CRLF files, no bulk replace) | "It keeps saying it can't find the text"; edits mix line endings | Typographic/whitespace tiers, LF-view offset mapping, region endings, `replaceAll`, miss hints | **done** (`fs/editing.py`, `edit_tools.py`) |
| 2 | Long coding turns hit the 24/32-step ceiling and compact lossily | "It stopped halfway"; "it forgot what it was doing" | Coding profile step budget ≥ 48 and 20 min; token-budgeted 15 k-token verbatim tail + five-section summary | **done** (tail/summary + background precompute, `context/precompute.py`) |
| 3 | Serial tool calls by default | Slow exploration; one read at a time | Mark read-only fs/intel tools `parallel_safe`, default `HARNESS_PARALLEL_TOOLS=on` | **done** (`config/flags.py`, `code/*_tools.py`) |
| 4 | Background commands must be polled | "It keeps checking the log instead of working" | Completion notice delivered at the next step boundary, like steering | **done** (`background_jobs.py`, `graph.py`, `shell_tools.py`) |
| 5 | No post-edit formatting / diagnostics | Misformatted code; obvious type errors survive an edit | Run the project formatter and LSP diagnostics after a successful edit, bounded and non-fatal | **done** (`code/formatting.py`, `code/post_edit.py`, edit/write results) |
| 6 | Effectful tool surface is 100+ tools | Model picks the wrong runner (`shell_run` vs `execute_command`); schema cost | A coding surface preset (~20 tools); the rest stay discoverable and operator-enableable | **done** (`CODING_SURFACE`, `surface.py`, `tool_search`) |
| 7 | Code tasks are file-level, poll-based, image-less | Cannot accept one hunk; no live feedback; cannot show a screenshot to fix UI | Hunk-level promotion; SSE progress; bounded image attachments | **done** (`promotion.py`, `code-run-stream.ts`, `CreateTaskRequest.images`) |
| 8 | No `todowrite`-style plan tool | Long tasks lose the thread | Add a todo tool to the coding profile, rendered as a plan panel | **done** (`code_todo`, `code/todos.py`, `plan-panel.tsx`) |
| 9 | Compaction is late and lossy | Context overflow mid-task | See #2 | **done** (background precompute, `context/precompute.py`; lossiness fixed in #2) |
| 10 | No automatic per-turn undo | A bad turn must be undone by hand | Turn on checkpoints by default for `edit_verify`, expose "undo this turn" | **done** (`checkpoint_undo_turn`, default `warn`, panel button) |
| 11 | Media reading | Cannot inspect a screenshot/PDF while coding | Add media results to `read_file` | **done** (`code/media.py`, media message in `tool_turn`) |
| 12 | Code Mode is read-only and opt-in | Cannot orchestrate writes from one program | Extend `tool_code` to effectful tools behind the existing authorizer, off by default | **done** (`code_mode.py`; still opt-in via `HARNESS_CODE_MODE`) |
| 13 | One coding prompt for every model family | Weaker tool-call formatting on models with trained-in conventions | Model-family prompt variants, selected from the resolved model | **done** (`prompt_variants.py`, `surface.resolve_surface_run`) |
| 14 | Review notes are free text | Cannot point at the exact line to change | Line-anchored comments on the diff, sent back to the agent | **done** (`diff-sections.parseDiffLines`, `review-comments.ts`, `DiffViewer` anchors) |
| 15 | Diff view is plain unified text | Harder to read a multi-file change; no image diff | Syntax-highlighted split/unified diff, file tree, image diffs, hunk selection | **done** (dependency-free `highlight.ts`, `diff-model.ts`, `diff-file-tree.tsx`; before/after image pairs render through a bounded, magic-byte-checked route) |
| 16 | Activity feed groups consecutive tools only | No per-category placement or last-turn diff strip | Category projection (context/read/file/events/subagent) + turn diff row | **done** (`activity-groups.ts`, `ActivityFeed` category labels + changed-this-turn strip) |
| 17 | Composer has mentions/attachments, not editor context | The model cannot see the file/selection the user is looking at | Inject active editor file + selection as context | **done** (`chat/editor-context.ts` store + bounded prompt block; editor publishes, dock chip + switch) |
| 18 | Review view shows only the working change | Cannot inspect what was committed, compare to a branch, or isolate the last turn | Source modes: working / committed / revision / last turn | **done** (`worktree.diff` modes, `/code/tasks/{id}/diff?mode=`, checkpoint-backed turn diff, `DiffViewer` selector) |

## 10. What "best" would look like

Eclipse already has the safety spine OpenCode lacks. The winning combination is
Eclipse's verification/policy/LSP plus OpenCode's **loop persistence, context
discipline, tool ergonomics, and feedback speed**:

1. Keep the read ledger, stale-hash guard, atomic writes, real-path jail,
   repair ledger and LSP. Do not trade them for OpenCode's looseness.
2. Give coding runs room to finish: a higher step budget, parallel reads, and
   compaction that keeps a real verbatim tail and summarizes the rest before
   the window is full.
3. Make the *common* tools excellent: fuzzy edit (done), miss hints (done),
   formatter + diagnostics after edit, non-polling background commands, a todo
   plan, and a small default tool surface.
4. Make review trustworthy: hunk-level accept, syntax highlighting, live task
   streaming, and a one-click "undo this turn".
5. Only then widen Code Mode and effectful discovery.

## Appendix A — OpenCode core modules inspected, and Eclipse's counterpart

Read from `packages/core/src/` unless noted. "Parity" means the capability
exists on both sides; the section number points to the analysis.

| OpenCode module | What it is | Eclipse counterpart | Result |
|---|---|---|---|
| `session/runner/llm.ts` | Step policy, continuation, max-steps prompt | `harness/graph.py` loop | Eclipse caps earlier (§1) |
| `session/runner/step.ts` | One model stream + parallel tool fibers | `harness/tool_turn.py` | parallelism now on (§1) |
| `session/runner/publish-llm-event.ts` | Durable step/stream events | `harness/event_sequence.py`, run events | parity |
| `session/runner/retry.ts`, `model-retry.ts` | Retry policy | `harness/retry_policy.py` | parity |
| `session/compaction.ts` | Background, tail-preserving compaction | `harness/compaction.py`, `context/precompute.py` | parity; Eclipse precomputes at 35 % pressure |
| `session/history.ts` | Replay from the last compaction | `harness/history.py` | parity |
| `session/instruction-state.ts` | Hash-delta instruction epochs | `harness/code/project_memory.py` | Eclipse re-injects (§6.5) |
| `session/inbox.ts`, `session/prompt.ts` | Steering and queued prompts | `graph.py` steer drain | parity (§1) |
| `session/execution/restart.ts` | Resume orphaned claims | `harness/run_recovery.py` | parity |
| `session/session.ts`, `store.ts`, `projector.ts` | Event-sourced session store | `harness/service.py`, `persistence.py` | parity (different shape) |
| `session/stats.ts`, `session/usage.ts` | Token/cost accounting | `harness/run_cost.py` | parity |
| `session/search.ts`, `search-index.ts` | Session full-text search | `harness/session_index.py` | parity |
| `tool/plugin/read.ts`, `read-filesystem.ts` | Paged text/dir/media read | `code/fs_tools.py`, `code/media.py` | parity (§3) |
| `tool/plugin/write.ts`, `file-write.ts` | Write + format after write | `code/fs_tools.py`, `code/formatting.py` | parity (§6) |
| `tool/plugin/edit.ts`, `file-mutation.ts` | Tolerant edit, no ledger | `code/fs/editing.py`, `code/edit_tools.py` | Eclipse safe; matching now parity (§3) |
| `tool/plugin/patch.ts`, `tool/patch-grammar.txt` | Freeform multi-file patch | `code/fs/editing.py` (`apply_diff`) | parity |
| `tool/plugin/shell.ts`, `shell/*` | Background + notification + output file | `code/shell_tools.py`, `harness/terminal/**`, `background_jobs.py` | parity (§3) |
| `tool/plugin/grep.ts`, `glob.ts`, `ripgrep.ts` | ripgrep search | `code/fs/search.py` | parity |
| `tool/plugin/subagent.ts`, `session/subagent-*.ts` | Non-blocking child sessions | `harness/delegation*.py` | parity, different shape (§4) |
| `tool/plugin/todo.ts` | Plan list | `code_todo` + `CodeTodo` store + plan panel | parity (§3) |
| `tool/plugin/question.ts` | Structured question | `harness/clarify_tools.py` | parity |
| `tool/plugin/webfetch.ts`, `websearch.ts` | Web access | `harness/research/**` | parity |
| `tool/plugin/skill.ts`, `skill/*` | Skill discovery and `skill://` | `harness/skills/**` | parity (§6.5) |
| `tool/mcp.ts`, `mcp/**` | MCP tools/resources/prompts/OAuth | `harness/mcp/**` | parity or better (§6.5) |
| `tool/runtime.ts`, `tool.ts` | Schema normalization, filtering, codemode catalog | `harness/tools.py`, `tool_disclosure.py`, `code/policy.CODING_SURFACE` | parity (§3) |
| `permission.ts`, `permission/saved.ts` | Rule language + persisted allows | `code/policy.py`, approvals | Eclipse safer, OpenCode finer rules (§5) |
| `file-access.ts`, `file-write.ts` | Lexical path resolution, approvals | `code/policy.py`, `fs/resolver.py` | Eclipse stronger (§5) |
| `agent.ts`, `config/plugin/agent.ts` | Markdown/config agents, plan agent | `harness/agents.py`, presets | parity (§6.5) |
| `plugin/plan.ts` | Plan mode as an agent | `harness/plan_mode.py`, `plan_only` | parity, different enforcement |
| `instructions/*`, `instruction-discovery.ts` | AGENTS.md discovery + deltas | `code/project_memory.py` | parity (§6.5) |
| `formatter.ts`, `formatter/builtins.ts` | 27 formatters after write | `code/formatting.py` (4 built-ins) | parity for the common stack; fewer formatters (§6) |
| `snapshot.ts`, `session/revert.ts`, `git.ts` | Git-tree snapshots, staged revert | `code/checkpoints.py` (`warn` default, per-turn undo) | parity (§6) |
| `pty.ts`, `persistent-pty.ts` | Persistent terminals | `harness/terminal/**` | parity (§3) |
| `config.ts`, `config/discovery.ts`, `managed-policy.ts` | Layered config + enterprise policy | `harness/config/**` | parity (§6.5) |
| `plugin/plugin.ts`, `plugin/supervisor.ts` | Plugin hooks + hot reload | `harness/plugins/**` | parity (§6.5) |
| `plugin/system-prompt/*.txt` | Model-family prompts | `prompt_variants.py` (one audited base + family block) | parity (§6.5) |
| `packages/codemode/**` | Restricted interpreter + nested tools | `harness/code_mode.py` (`tool_code`; effectful calls through the authorizer) | parity (§3) |
| `session-ui/components/session-diff.ts` | Patch/content → pierre/diffs model, 16-entry LRU, full-vs-partial patch split | `code/fs/diff.py` + `screens/code/diff-model.ts` + `diff-viewer.tsx` | parity in shape; Eclipse tokenizes without a grammar engine |
| `session-ui/components/session-review.tsx` | Split/unified, file tree, per-LINE comments sent back to the agent, image diffs | `diff-viewer.tsx` + `diff-file-tree.tsx` + `review-comments.ts` | parity on split/tree/line comments and a before/after picture pair |
| `session-ui/components/line-comment.tsx` | Line-anchored comment editor, mention tokens, timed editor machine | `diff-viewer.tsx` line anchors + `buildReviewPrompt` | parity without mention tokens |
| `session-ui/timeline/place.ts`, `timeline/projection.ts` | Facts grouped by category (context/read/file/events/subagent), "last turn" diff row, configurable open state | `activity-groups.ts` + `ActivityFeed` | parity on categories and the turn strip; Eclipse keeps chronology (no configurable placement) |
| `session-ui/tools/tool-renderer.tsx` | 100 KB of per-tool renderers (shell, edit, read, search, patch, subagent) | generic tool cards + change cards | Eclipse specialized views are fewer |
| `tui/feature-plugins/system/diff-viewer.tsx` | Diff sources branch/committed/working/**last turn**, split/unified, file tree, hunk selection, image diff, mouse | `code/diff-viewer.tsx` + `diff-file-tree.tsx` | split/tree/hunk/before-after parity; source modes now match (working/committed/revision/last turn); mouse missing || `tui/component/prompt/index.tsx`, `prompt/local-attachment.ts` | Active editor file+selection injected as context, pasted-text placeholders, image dedupe/preview, mention realignment | `editor-context.ts` + `use-editor-context.ts` + `chat-dock.tsx` | parity on file/selection context (bounded + TTL); paste/image behaviours already exist |
| `tui/routes/session/permission.tsx` | Per-request permission cards with allow-once/always | `ApprovalCard` | parity |
| `core/src/plugin/provider/prompt-cache-key.ts` | Per-provider `supportsPromptCacheKey` compatibility flag | `prompt_segments.py` computes one key + Anthropic markers | Eclipse has the key, not the per-provider body flag |
| `core/src/plugin/provider/openai-context-caps.ts` | ChatGPT-plan context caps by model/account (272k → 500k) | `models.resolve_context_window` (catalog) | account-aware caps missing |
| `core/src/plugin/provider/refresh.ts`, `github-copilot.ts` | Credential-switch cache refresh, OAuth device flow, account-scoped endpoints | settings vault + manual model connections | OAuth/account refresh missing |
| `core/src/plugin/provider/local.ts` | Background local discovery every 30 s, tools/vision/context flags per model | `model-catalog.ts` one-shot probe | background discovery missing |

| `app/src/composer/**` | Editor, submit, suggestions machine, and a **message queue** for prompts waiting on the running turn | `chat-input.tsx`, `message-queue.ts` | parity (Eclipse has a queue + quote chip) |
| `app/src/session/timeline/**` | Virtualized timeline, cursor/quote machines, message rows | `chat-history.tsx` + `activity-groups.ts` | categories + quote + grouping parity; virtualization not confirmed |
| `app/src/session/requests/session-question-dock-*.tsx` | Structured question dock (classic/experimental) | `clarify` card | parity |
| `app/src/session/commands/use-session-commands.tsx` | Slash-command palette wired to the shared registry | `command-registry.ts` + composer | parity |
| `gui-plugins/src/review/**` | Side review panel: git/branch/**turn** diff modes, 30 s timeout + abort/backoff, file tree, line comments, open-in-app | `promote-change.tsx`, `diff-viewer.tsx`, `reveal.ts` | file tree, line comments, branch/turn sources and reveal parity; no external-editor choice (reveals in the OS file manager) |
| `gui-plugins/src/context/**` | Session context tab: token/model usage, raw messages, export | `context-panel.tsx` + sessions export | parity |
| `gui-plugins/src/file/**` | File tree, artifact view, open-in-app, find | `editor-screen.tsx`, `diff-viewer.tsx` | parity/partial |
| `gui-plugins/src/details/**` | Server/MCP/workspace details panels | settings/MCP panels | parity |

**Coding-relevant deltas these reviews expose** (added to the ranked list as
#14-#17): line-anchored review comments (**done**); syntax-highlighted split
diff with a file tree and before/after image pairs (**done**);
per-category activity placement with a "changed this turn" strip
(**done**); active-editor selection as prompt context (**done**). Provider-side:
account-aware context caps, OAuth-connected providers and background local
model discovery.

**Review complete.** Every OpenCode package that carries coding-runtime,
coding-tool, coding-UI or provider behaviour has now been read at the module
level: `core` (session, tools, permissions, agents, skills, instructions, MCP,
plugins, prompts), `tui`, `session-ui`, `app`, `gui-plugins/src` and
`core/src/plugin/provider/**`. What remains unread is generated or vendored
code — `sdk-factory.ts` consumers (generated provider clients), `@pierre/diffs`,
`@opentui/*`, test fixtures/stories — none of which changes a conclusion above.

## Appendix B - Reproducing the comparison

1. Clone OpenCode at the commit above: `git clone --depth 1
   https://github.com/sst/opencode` then `git log -1` (expect `6531f17`).
2. Constants quoted here can be checked with `rg` for the named constant in the
   cited file.
3. Eclipse-side checks: `npm run test:evals`, the targeted pytest files named in
   `docs/CHANGELOG.md`, and `npx tsc --noEmit`.

## Changelog of this comparison

- 2026-11-21 — first edition; OpenCode `2.3.0` (`6531f17`). Fixes landed with it:
  tolerant edit matching (`code/fs/editing.py`), coding tools in the disclosure
  focus set (`tool_disclosure.py`), read-only tool parallelism on by default
  (`config/flags.py` + `parallel_safe` on the fs/CIUE/workspace reads), a
  48-step / 20-minute coding budget (`graph.py`), and a token-budgeted 15 k-token
  verbatim tail plus a five-section structured summary
  (`context_budget.KEEP_TAIL_TOKENS`, `compaction.build_structured_summary`).
