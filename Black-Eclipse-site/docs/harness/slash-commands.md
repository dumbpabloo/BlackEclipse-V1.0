---
title: Slash Commands
status: Current
last-verified: 2026-11-25
---

# Slash Commands

**Who this is for:** anyone changing how a `/command` is defined, autocompleted,
routed, or executed in the chat surfaces.

## What this is (and why)

Eclipse's chat is chatbot-first. A leading `/` in the composer names a **slash
command**. There are three kinds, and the distinction is the whole design:

- **Profile commands** (e.g. `/eclipsesdk`) do **not** execute anything on their
  own; they force a deterministic run *profile* for the next agent turn. They are
  parsed in TS (`src/lib/harness/commands/commands.ts`) and the runtime still enforces the
  actual grant.
- **Routed commands** (`/skills`, `/status`, `/market`, â€¦) are executed by the
  **shared command router** and return a rendered result immediately. They never
  open an LLM turn.
- **Agent-turn commands** (`/skill`, `/anatomy`, `/visual`, `/deck`) rewrite the chat
  query into a directive and run as a **normal agent turn**, so the chatbot does
  the work (and streaming + conversation history are preserved). `/skill <name>
  <what it should do>` tells Eclipse to author a new reusable skill; `/anatomy
  <concept>` tells it to explain the concept as an interactive **3D model** â€” an
  orbitable machine composed from mechanism recipes, rendered inline as a
  `model3d` block with a numbered key, stages and `.glb` export; `/visual <what
  to chart>` tells it to render ONE inline data **`chart`** block, choosing from
  the full chart-type set and plotting only real values. The rewrites live in
  `src/lib/harness/commands/command-agent.ts`.

Profile commands exist because the alternative â€” asking a classifier to infer "is
this a coding request?" from prose â€” is fuzzy. The set of profile commands is
deliberately tiny. Agent-turn commands run under the **default** profile (no
classifier narrowing) so the write-capable `skill_create` tool stays bound.

## The contract

Everything starts from **one canonical registry**:
`src/lib/harness/commands/command-registry.ts`.

A `CommandSpec` describes a command declaratively: stable `id`, canonical `name`,
`aliases`, `summary`/`description`, `category`, `tags`, `version`, lifecycle
`status`, typed `args`, `flags`, a `handler` `{ type, target }`, `permissions`,
`risk`, `executionMode`, timeouts/retries/idempotency, `output`, and `examples`.

Invariants the registry enforces:

- **One registry, two consumers.** The composer palette and the router both read
  the same list, so they can never disagree about what exists.
- **Only handler-backed commands are `available`.** `isCommandAvailable()` is the
  single source of truth. `status: "unavailable"` commands are still registered
  (so `/help` and the palette are honest and complete) but a routed invocation
  returns an explicit, structured **unsupported** error â€” never a fake success.
- **No duplicate names/aliases.** `detectRegistryConflicts()` makes a collision
  visible to tests/CI; `resolveCommand()` is first-wins.

## Parse â†’ Resolve â†’ Validate â†’ Authorize â†’ Execute â†’ Render â†’ Audit

The router (`src/lib/harness/commands/command-router.ts`) is the ONE execution path for
every routed command.

1. **Parse** â€” `command-parser.ts`. A real tokenizer (not `split(' ')`) that
   preserves quoted strings, Unicode, filenames with spaces, and backslash
   escapes, then binds tokens to the command's declared schema. Unknown flags are
   errors unless the command opts into `passThroughFlags` (how
   `/skills run <id> --objective "â€¦"` builds a skill's input object).
2. **Resolve** â€” a leading `/token` that matches no command yields an `unknown`
   result. `/usr/bin` is not a command (the token must be followed by whitespace
   or end-of-text).
3. **Validate** â€” missing/invalid args produce a readable usage error.
4. **Authorize** â€” a command declaring `approval: "operator"` is denied unless the
   caller passed the operator gate (transport/token). This is defense in depth:
   the Python runtime re-enforces its own default-deny policy.
5. **Execute** â€” dispatch on `handler.target` to a handler in
   `src/lib/harness/commands/command-handlers.ts` that calls the **existing** systems
   (skills-client, tasks-client, workflows-client, python-client, and loopback
   calls to the app's own `/api/...` front doors). The router owns no agent loop,
   task queue, or skill database. The router also enforces the declared contract:
   `timeoutMs` (a handler that overruns returns `COMMAND_TIMEOUT`), `retries`
   only when `idempotent: true`, and a deprecation notice before a deprecated
   command's output. Idempotency is what makes retrying safe; a side-effecting
   command is never retried implicitly.
6. **Render** â€” build validated `eclipse-response` blocks (`command-render.ts`);
   each block is checked against `src/lib/response/contract.ts` before it is
   emitted.
7. **Audit** â€” a structured, redacted audit event (command id, actor, operator,
   status, handler target, skill/run id, duration) is logged.

Handlers are injected through `CommandDeps`, so the router is fully
unit-testable without a live runtime.

### High-risk confirmation, rate limits and audit

- **Two-step confirmation.** A `high` or `critical` risk command does **not** run
  on its first invocation. The router returns `status: "pending"` with a warning
  card and the exact re-run command, including a single-use `--confirm <token>`.
  The token is bound to the command id, its positionals/flags, the actor and the
  operator flag, expires after five minutes, and is consumed once
  (`command-confirm.ts`). Replaying it, changing an argument, or using it from a
  different actor returns `pending` again with a fresh token. `/terminal` and
  `/code` are the current high-risk commands; the contract test requires every
  future high/critical command to declare a `confirm` flag.
- **Risk-based rate limits.** The router keeps a sliding window per
  actor + command id (`command-rate-limit.ts`): critical 4/min, high 6/min,
  medium 20/min, low 60/min, none 120/min. A command can override it with
  `rateLimit: { windowMs, limit }`. A refused call returns `denied` with
  `RATE_LIMITED` and a retry-after. The check runs after authorization, so a
  non-operator cannot burn a bucket.
- **Durable audit.** Every terminal result (completed, failed, denied, pending,
  invalid, unknown) is appended to `.black-eclipse/command-audit.jsonl` as one
  redacted JSON line (`command-audit.ts`): command id/name, actor, operator,
  status, risk/approval, duration, skill/run id and an error **code** â€” never
  output, arguments or secrets. The file rotates at 2 MB to `â€¦jsonl.1`.
  `/history [limit]` reads it newest-first; `/usage --commands` aggregates it
  (status counts, runs per command, p50/p95 duration, high-risk count).
  The sink is injected through `CommandDeps.audit`, so tests never touch the
  real file.

### Project command packs (local extensions)

A user can add their own commands in `.black-eclipse/commands.json` without
touching the source tree:

```json
{
  "version": 1,
  "commands": [{
    "name": "morning-brief",
    "aliases": ["brief"],
    "summary": "Produce my morning market brief.",
    "description": "Summarize overnight moves, my watchlist and the calendar.",
    "category": "research",
    "args": [{ "name": "focus", "description": "Optional focus.", "variadic": true }],
    "prompt": "Produce my morning brief. Focus: {{focus}}."
  }]
}
```

- They are **agent-turn commands only** (`handler: agent.plugin`): the pack
  supplies a prompt template (`{{arg}}`, `{{flag.name}}`, `{{args}}`), and the
  normal agent turn runs under the normal tool policy. A pack can never register
  a routed handler, widen authority, or shadow a builtin name/alias.
- `command-plugins.ts` validates names, aliases, categories, args/flags, caps
  (50 commands, 10 args, 10 flags) and length limits. Invalid entries are
  reported in the manifest's `errors[]`, never silently executed.
- `run-service.ts` loads the pack for a slash-leading turn and passes it to
  `buildAgentInstruction(text, projectCommands)`; the composer fetches
  `GET /api/harness/commands` once and merges the `source: "project"` entries
  into the palette (with a **project** badge).
- **Manifest.** `GET /api/harness/commands` returns the canonical catalog
  (builtins + validated project commands) with `available`, args/flags, risk,
  examples and `source`. It omits `agentPrompt` and sets a stable ETag so a
  revalidating client gets `304`.

### Typed UI intents (navigation without markdown links)

A command can return a `ui` intent in its result:

```ts
{ type: "navigate", tab: "alerts", subroute?: string, params?: Record<string, string> }
{ type: "openCanvas", artifactId?: string }
```

- The router carries it on `CommandResult.ui`; the chat stream adds it to
  `client.final`, `events.ts` validates it (`navigate` needs a string `tab`,
  `openCanvas` has no required fields), `use-agent-run` stores it on the turn,
  and `stock-chat.tsx` dispatches it through the SAME destination store as the
  âŒ˜K bar (`useCommandStore.dispatch`, or openCanvas).
- It is **data, not code**. Non-UI surfaces (remote channels, CLI) receive the
  text result and ignore the intent.
- Commands using it today:
  - `/canvas [artifactId]` â†’ `openCanvas`
  - `/open <tab>` (alias `/go`) â†’ `navigate`; the tab is validated against the
    single `APP_TABS` vocabulary.
- The intent is dispatched once per completed turn, after the answer is
  persisted. It never runs on the server and cannot execute tools.

### Contract enforcement (not just documentation)

- **Handler targets are verified.** `command-registry.contract.test.ts` fails CI
  if a routed command names a handler target that is not in the dispatch table,
  if two commands share an id, if a high/critical-risk command lacks the
  operator gate, if a completion provider is unknown, or if an `available`
  command is missing its summary/examples/output/version.
- **Risk is visible.** The composer palette shows an `operator` chip when
  `permissions.approval` is `operator` and a red risk chip for `high`/`critical`
  commands. The audit event records the declared `risk` and `approval` alongside
  the command id and status.
- **Search is typo tolerant.** Prefix/alias/substring matches still win, but a
  query within edit distance 2 of a command name (or 1 of an alias) is offered
  as a fallback, so `/help quotte` suggests `/quote`.

## Where it is wired

- **Composer palette** â€” `src/components/chat/command-palette.tsx` renders the
  inline palette (`useCommandPalette` + `CommandPaletteBody`): grouped by
  category, with Pinned â†’ Recent at the top, each row's argument hint,
  availability, risk/operator gate and a detail footer (description, usage,
  aliases). `command-palette-model.ts` owns the pure ordering (so the highlighted
  index always matches the rendered list). Typing `/` never executes; selection
  only inserts `/name `.
- **âŒ˜K / Ctrl+K** â€” `src/components/chat/command-dialog.tsx` opens the same
  surface as a centered dialog with its own search box and a two-column
  list/detail layout. It is independent of the draft; selection inserts
  `/name ` into the composer. Only the visible composer answers the shortcut,
  so a keep-alive tab cannot open a second dialog.
- **Recents and pins** â€” `src/lib/harness/commands/command-history.ts` stores the last
  50 command names and up to 24 pins in `localStorage` (`be.command.history.v1`,
  `be.command.favorites.v1`). This is a UI convenience, never an authority; the
  server audit remains the record of what ran.
- **Argument completion** â€” `src/lib/harness/commands/command-suggest.ts` detects the
  completion-bearing argument under a command and calls
  `GET /api/harness/suggest?provider=â€¦&q=â€¦` (skills, symbols, portfolios,
  workflows, models). `command-suggestions.tsx` renders the candidates with
  arrow/Enter/Tab/Esc handling. The route is read-only and degrades to an empty
  list (never a blocking error) when a subsystem is unavailable.
- **Chat surfaces** â€” both `/api/harness/chat` and `/api/harness/chat/stream`
  intercept a routed command *before* starting an agent run. The stream route
  emits a synthetic `client.run` / `client.final` / `stream.end` sequence, so the
  existing chat UI and renderer handle a command result with no special casing.
  Profile commands, agent-turn commands, and ordinary prose fall through to the
  normal agent path; unknown `/tokens` also fall through, so text that merely
  starts with a slash keeps working.
- **Direct entry** â€” `POST /api/harness/command` runs the same router for routed
  commands and the chat path for agent-turn commands.

## Commands implemented today

Backed by real handlers/endpoints:

- Runtime: `/help` (bare = grouped catalog; `/help <command>` = one command's
  full contract with arguments, flags, risk, approval and examples), `/status`
  (alias `/health`), `/compact`, `/tasks` (alias `/plans`), `/workflows`,
  `/agents`, `/models` (alias `/providers`), `/tools`, `/usage` (alias `/cost`;
  `/usage --commands` summarizes the command audit), `/history` (alias
  `/audit` â€” recent redacted command executions).
- Markets / portfolio / memory / MCP: `/market` (gateway providers + health),
  `/alerts` (alert watches + running monitor count), `/news [query|--ticker]`
  (stored headlines with source/sentiment/time; never a live fetch), `/quote`
  (`/quotes` â€” JDdS quotes with honest freshness), `/portfolio` (real
  portfolios), `/theses [symbol]` (tracked theses), `/journal [all|open|closed]`
  (recorded trades + the journal's own P&L), `/memory` (overview + recent, or
  `/memory <query>` to search), `/mcp` (configured servers).
- Research / web: `/research`, `/deep-research` (alias `/deep`), `/web`
  (alias `/browse`), `/read` (alias `/summarize`) â€” all through the web research
  engine, returning the answer with its sources.
- Quant: `/backtest` â€” lists saved strategies, or backtests one by id.
- Runtime surfaces: `/open <tab>` (alias `/go` â€” navigate via a typed UI intent),
  `/jobs` (scheduled/watched jobs), `/artifacts` (recent
  Artifact Center rows), `/sessions` (durable chat sessions), and `/stop`
  (aliases `/cancel` â€” list active runs, or cancel one by id; operator-gated).
  All are read-only except an explicit `/stop <runId>`, which calls the same
  cooperative cancellation route as the Stop button.
- Dev: `/code` (start a code task), `/git [status|diff|log] [--staged]`
  (read-only repository inspection; stage/commit/push are intentionally not chat
  commands), `/canvas [artifactId]` (open the inline Canvas via a typed UI
  intent), `/terminal` (aliases `/run`, `/sh` â€” run a
  shell command; operator- and effect-gated). `/terminal` takes
  `--shell powershell|cmd|wsl|bash` and `--cwd <path>`; on Windows the host
  default is PowerShell, so use `--shell cmd` for cmd.exe-specific syntax. The
  result card shows the requested shell, the resolved working directory, the
  exit code, duration, and honest `timedOut` / `truncated` markers. The handler
  unwraps the Python route's `{ success, result }` envelope, so stdout/stderr are
  real output rather than an empty result. Both `/terminal` and `/code` are
  high-risk: they return a `pending` confirmation card first and run only when
  re-sent with the single-use `--confirm <token>`.
- Visual: `/anatomy` (aliases `/visualize`, `/mechanism`) â€” explain a concept as
  an interactive **3D model** (an orbitable `model3d` block with labelled parts,
  mechanism recipes and step-through stages); an agent-turn command that authors a
  declarative scene and never writes meshes or code (see
  `docs/interactive-response-engine.md`, "3D mechanism models").
- Visual: `/visual` (aliases `/chart`, `/plot`) â€” render a request as ONE inline
  data **`chart`** block. Eclipse picks the best of the sixteen chart types
  (line, area, bar, stacked_bar, grouped_bar, pie, scatter, bubble, histogram,
  heatmap, treemap, sunburst, funnel, gauge, radar, waterfall) and plots only
  real values it can source â€” never an invented curve. Agent-turn; see
  `docs/interactive-response-engine.md`, "Chart & diagram presentation".
- Visual: `/deck` (aliases `/slides`, `/presentation`) â€” build a real presentation
  (a versioned **deck** artifact plus a `.pptx` export) from a subject, with
  `--theme eclipse|paper|midnight|slate` and `--slides N`. Agent-turn: the model
  composes slides with sourced data only; an unresolved chart becomes an explicit
  UNAVAILABLE slide. The deck opens in Canvas for editing and presenting. See
  `docs/harness/presentations.md`.
- Skills: `/skills` (list/search/category/show/**run**/test/validate),
  `/skill` (**author a new skill** â€” agent turn), `/skill-explain`, `/skill-test`,
  `/skill-enable`, `/skill-disable`, `/skill-create` (learning-engine draft).
- Profile: `/app` (control the Eclipse app through the typed SDK; alias
  `/eclipse`) and `/eclipsesdk` (edit Eclipse's own source code; alias `/esdk`).
- Agent-turn: `/skill` (authors a skill for Eclipse; see Skill integration).

Every registered command has a real handler (the palette `soon` badge is gone).
Commands that call a subsystem (research, quant, code, terminal, quotes) reuse
the **same front-door APIs the UI uses** â€” the router makes a loopback call to
`/api/...`, so a command never re-implements an engine. `/terminal` requires the
operator gate; a non-operator is refused before anything runs.

The workspace readers (`/alerts`, `/news`, `/theses`, `/journal`, `/jobs`,
`/artifacts`, `/sessions`, `/git`) are thin read-only projections of the app's
existing front doors: they return only rows the API returned and never write.
`/stop <runId>` is the one mutation in this group and uses the same cooperative
cancellation route as the Stop button (operator-gated). They live in
`src/lib/harness/commands/command-handlers-workspace.ts`, so adding a new reader does not
grow the main dispatch file.

## Manual compaction â€” `/compact`

The chat already compacts automatically when the runtime bounds history
(`python/harness/compaction.py`, `history.py`). `/compact` is the **manual**
counterpart: the user asks for room now, without waiting for a limit.

- **What is replaced.** The turns older than `--keep N` (default 6) are replaced
  by the fenced `<compacted-conversation>` block. The most recent N turns stay
  verbatim. `--keep` is clamped to 0â€“50.
- **The transcript is not rewritten.** Every message stays visible in the chat.
  Only the history sent to the model changes â€” the same behaviour as automatic
  compaction.
- **It is deterministic.** `/compact` calls `compact_history()` in
  `python/harness/compaction.py`, which builds the skeleton and **never calls a
  model**. It therefore works when no provider is reachable. The model narrative
  remains an opt-in policy (`history_compaction_model`) on the automatic path.
- **It persists.** The chat routes store the result as `compaction.json` beside
  the conversation transcript (`src/lib/chat/session-store.ts`). On every later
  turn, `applyStoredCompaction()` prepends the block and slices the client
  history at the stored `through` boundary, so the effect survives a reload.
  A stored compaction is ignored when the client history is shorter (the
  conversation was cleared or rewritten).
- **It fails honestly.** No history in the request â†’ `NO_HISTORY`; fewer turns
  than the keep window â†’ "Nothing to compact" and nothing is stored; runtime
  unreachable â†’ the normal runtime-unavailable error is surfaced.
- **Endpoint.** `POST /history/compact` on the Python runtime
  (`{"history": [...], "keepTurns": 6}`) returns
  `{ok, block, droppedTurns, keptTurns, chars, reason}`. The TS command handler
  (`native.compact`) is the only caller.

## Skill integration

### Run a skill â€” `/skills run <id> [--<input> <value>]â€¦`

Resolves the skill against the authoritative registry and calls the existing
`executeSkill`, which runs through the Python runtime's real executor (allowlist,
permissions, dependency DAG, and budget enforced in Python â€” the `SKILL.md` text
is prompt-delivered and can never widen authority). An unknown id returns
`SKILL_NOT_FOUND`; a disabled/incompatible skill is refused by Python.

The skill's declared input is passed either as pass-through flags
(`/skills run user.my-skill --objective "Review ACME"`, any `--key value`) or as
trailing free text, which becomes `objective`. This matters because every
user-authored skill declares `objective` as a required input; sending an empty
input makes the runtime block the run with `INPUT_VALIDATION_FAILED` before the
skill starts. `--version <v>` pins a version (it is a declared flag, so it is not
treated as input).

### Author a skill â€” `/skill <name> <what it should do>`

Typing `/skill â€¦` tells Eclipse to **make a skill for itself**. It is an
agent-turn command: the query is rewritten (`command-agent.ts`) into an authoring
directive, and the normal chat turn runs. The chatbot generates the skill (a
one-line description, concise steps, minimal tools/permissions, input schema) and
persists it with the existing `skill_create` tool â€” so it lands in the Python
skill library and **appears on the Skills page** (`src/components/screens/skills/`)
where the user can view, edit, enable, evaluate, or run it. Creation is gated by a
single-use operator approval (an approval card in the streaming chat), and the
agent is told never to claim a skill was created unless the tool result confirms
it. `/skill-create` remains available for the deterministic learning-engine path
(`--from-task`, `--from-workflow`, `--objective`).

### Automatic selection during normal chat

The user does not have to remember a skill name. The Python runtime already binds
`skill_list` / `skill_view` / `skill_invoke` to the `eclipse-agent` (see
`python/harness/agents.py`), and the system prompt instructs the agent to discover
a relevant ENABLED skill and run it as a controlled sub-run when a request
matches. Discovery is metadata-only (bounded context); execution re-enforces the
skill's allowlist/permissions and links the child run to the turn. This is the
existing runtime behaviour, not a second planner â€” the TS side only owns the
explicit command surfaces.

## Security notes

- Agent-generated text, skill descriptions, and user input are treated as
  untrusted data â€” the router does not evaluate them.
- Operator-only commands are gated at the front door AND re-enforced in Python.
- Command output is redacted (`redactSecrets`) before it is returned.
- The router cannot enable live trading or external side effects by adding a
  command; a handler still goes through the same authorization path as any tool.

## Tests

- `src/lib/harness/commands/command-registry.test.ts` â€” resolution, aliases, conflicts,
  availability, search, argument hints.
- `src/lib/harness/commands/command-registry.contract.test.ts` â€” registryâ†”handler
  coverage, self-describing commands, risk/approval invariants, the `confirm`
  flag on high-risk commands, completion providers, deprecation/unavailability
  metadata, `/help <command>`.
- `src/lib/harness/commands/command-audit.test.ts` â€” status/risk aggregation, per-command
  percentiles, empty and single-row audits.
- `src/lib/harness/commands/command-palette-model.test.ts` â€” pinned â†’ recent â†’ category
  ordering, de-duplication, search-section behavior, flat/section agreement.
- `src/lib/harness/commands/command-history.test.ts` â€” recents de-duplication and cap,
  pin/unpin, clear-history-keeps-pins, empty-name rejection.
- `src/lib/harness/commands/command-suggest.test.ts` â€” completion argument detection,
  variadic token replacement, flags/prose/commands without a provider.
- `src/app/api/harness/suggest/route.test.ts` â€” provider allowlist, skills/
  symbols/portfolios/workflows/models mapping, unavailable degradation.
- `src/lib/harness/commands/command-plugins.test.ts` â€” valid packs, builtin name/alias
  collisions, invalid names/prompts/args, missing vs invalid JSON.
- `src/app/api/harness/commands/route.test.ts` â€” builtin + project manifest,
  omitted prompt template, stable ETag/304.
- `src/lib/harness/commands/command-parser.test.ts` â€” quoting, escapes, Unicode, enum and
  required-arg validation, pass-through flags.
- `src/lib/harness/commands/command-agent.test.ts` â€” `/skill` rewrites into a skill-authoring
  directive, `/anatomy` into a 3D-model (`model3d`) directive, and `/visual` into a
  `chart` directive; non-agent commands and prose return null.
- `src/lib/harness/commands/command-router.test.ts` â€” unknown/invalid/unavailable/denied
  paths, `/help`, `/status`, `/skills`, `/skills run`, operator gating,
  `/skill-create` requiring a real source, the workspace readers
  (`/alerts`, `/news`, `/theses`, `/journal`, `/jobs`, `/artifacts`,
  `/sessions`, `/git`, `/stop`, `/memory <query>`), the confirmation/replay
  flow, rate limiting + recovery, `/history`, `/usage --commands`, audit
  append, and the `/canvas` / `/open` UI intents.
- `src/lib/agent/events.test.ts` â€” `client.final` carries a validated typed UI
  intent.
- `src/app/api/harness/command/route.test.ts` â€” the direct route, including the
  agent-turn path.

## Known gaps / follow-ups

- `/skills`, `/research`, `/backtest`, `/code`, `/terminal`, `/market`,
  `/portfolio`, `/memory`, `/mcp` require the Python runtime up; without it they
  report a clean runtime-unavailable error (never a fabricated result).
- `/quote` reads JDdS directly (no runtime needed) and labels a stale price as
  stale.
- The direct `POST /api/harness/command` path runs `/skill` non-streaming, so the
  `skill_create` approval is deny-and-retry there; use the chat surfaces for the
  approval-card flow.
- `command-handlers.ts` still carries the original domain handlers (~1,000
  lines); the read-only workspace readers already live in
  `command-handlers-workspace.ts`. The next extraction candidates are the
  research/quant/dev renderers and the per-domain command specs.
- The confirmation card is **copy-paste** â€” it shows the exact `--confirm`
  command. There is no click-to-approve button yet because the response contract
  has no typed confirm action.
- The command audit is a local JSONL file with a 2 MB roll, not a database; it
  does not sync across machines and a server restart invalidates pending
  confirmation tokens.
- **Mentions (`@`).** The `@` sibling â€” naming a bot or group chat so the main
  chat delegates the turn to it â€” is a separate surface documented in
  `docs/harness/mentions.md`.
