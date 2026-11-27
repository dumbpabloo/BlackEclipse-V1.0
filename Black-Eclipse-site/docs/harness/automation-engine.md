---
title: Native Automation Engine
status: Current
last-verified: 2026-11-26
---

# Native Automation Engine

**Who this is for:** anyone asking "what runs without me, when does it run, why
did it run or refuse to, and can I trust it after a restart?"

The Native Automation Engine decides **WHEN and WHY** work executes. The Task /
Workflow / Agent runtime decides **WHAT and HOW** it executes. It lives in
`python/harness/automation/` and persists to the ONE `agent-memory.db` connection
(never `eclipse-memory.db`, never `jdds.db`). It supersedes the earlier
`harness.jobs` scheduler (ADR-H108) while keeping the legacy `/jobs` API working
through a thin adapter (ADR-H152).

## Where it sits

```
                    ECLIPSE AUTOMATION ENGINE
                              │
             ┌────────────────┴────────────────┐
       Schedule Engine                    Trigger Engine
   (cron/interval/one-time)        (event / condition / manual)
             └────────────────┬────────────────┘
                              ▼
                    Automation Definition  (durable, versioned)
                              ▼
                     Execution Dispatcher  (engine.py)
                              ▼
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
   service.run          Task System             Orchestration
   run_skill            (service.tasks)         (parallel / DAG)
   run_workflow
        └─────────────────────┼──────────────────────┘
                              ▼
                     Delivery (bus / artifact / messaging)
```

There is **one** scheduler, **one** definition model and **one** execution ledger.
The engine never runs an agent loop, a tool registry or a workflow engine of its
own.

## The domain object

An automation is not a cron string. `AutomationDefinition` (model.py) carries:

| Field | Meaning |
|---|---|
| `trigger` | cron / interval / one_time / event / condition / manual, plus timezone, start/end |
| `condition` | an optional deterministic gate (edge-triggered) |
| `action` | what the runtime should run (see Actions) |
| `execution` | `maxRuntimeS`, misfire policy, catch-up bound, timeout |
| `retry` | attempts, backoff, jitter, retryable/non-retryable classes |
| `concurrency` | `maxConcurrentRuns` + `onConflict` |
| `limits` | max runtime / tokens / cost / tool calls / parallel tasks / retries |
| `permissions` | allow, deny (deny wins), approval requirement + scope |
| `delivery` | when + how a result reaches the user |
| `version`, `state`, `scopeId`, `tags` | versioning, lifecycle, scope FILTER, labels |

### Lifecycle vs execution state

Two SEPARATE state machines (`lifecycle.py`). Invalid transitions are rejected
with a code — never silently accepted.

- **Lifecycle:** `draft → validating → active → paused → disabled → archived`.
- **Execution:** `triggered → queued → running → waiting/retrying → completed /
  failed / cancelled / timed_out / blocked / skipped`.

`pause` stops future triggers but does **not** kill an active execution; use
`cancel_execution` for that. A paused automation can still be run **manually**
(`run_now` / the Bots UI's Run now): pause stops the scheduler, not an explicit
operator request. Every other trigger class (schedule, event, condition,
webhook, fabric) still requires `active`, so a paused bot cannot be woken by
anything except the operator.

## Trigger types

`webhook` is a first-class trigger: the runtime generates a per-bot bearer
token at create time and `POST /automations/webhook/{id}` fires the bot only
when the token matches in constant time and the bot is `active`. The route is
deliberately outside `require_operator` because the caller is an external
service; the token is the authorization. A webhook never advances a schedule.
See `python/harness/routers/automation.py` and
`src/app/api/automations/webhook/[id]/route.ts`.


- **Cron** — 5-field expression, evaluated in the trigger's IANA timezone, with
  optional `startAt`/`endAt`. DST is deterministic: a nonexistent wall time
  resolves to the instant after the gap, an ambiguous one to its first occurrence.
  An expression that can never match (e.g. `0 0 30 2 *`) produces **no next run**
  — it is retained but never due, not scheduled years into the future.
- **Interval** — structural `intervalSeconds` (30s…30d), anchored to a fixed grid
  (the slot that last fired), not to the run's completion time, so cadence never
  drifts by run duration. Honors the same `startAt`/`endAt` window as cron.
- **One-time** — a single ISO `runAt`; after firing the automation is retained
  (never deleted) with no next run.
- **Event** — an indexed subscription to an event type (any dotted name, e.g.
  `filing.created`). The vocabulary is open-ended; unknown future types work. An
  optional `eventFilter` narrows the subscription: every key must equal the
  matching field of the event's `data` (bare `ticker`, dotted `data.x`, or an
  `event.` field), and a non-matching event is not consumed.
- **Condition** — a deterministic expression evaluated on a tick against facts
  from a `FactProvider`. The default is a `CompositeFactProvider` over the market
  gateway (`<SYM>.<field>`) and the portfolio engine
  (`pf.<portfolioId>.<field>`: `weight`, `unrealized`, `cash_pct`,
  `concentration_hhi`, …); a provider that cannot resolve a variable contributes
  no fact, so the condition evaluates `UNAVAILABLE` rather than firing on a guess.
  No LLM evaluates a financial or operational condition. A closed catalogue of
  portfolio monitors builds such conditions for the user
  (`advisory/monitors.py`, `GET/POST /automations/monitors`).
- **Manual** — `run_now` / the API / a webhook. Manual execution NEVER alters the
  schedule; it records a separate execution with `triggerType = manual`.

## Condition syntax

Operators: `== != > >= < <= AND OR NOT IN NOT_IN CONTAINS MATCHES`, arithmetic
`+ - * /`, parentheses, lists, string/number/bool literals, and the domain helpers
`crosses_above(x, t)`, `crosses_below(x, t)`, `changed(x)`, `increased_by(x, %)`,
`decreased_by(x, %)`.

```
BTC.price < 90000
AND
BTC.volume > BTC.average_volume * 1.5
```

Evaluation is **three-valued** (Kleene): a referenced variable with no value makes
the comparison `UNAVAILABLE` — distinct from `FALSE` and never a trigger. The
parser is a hand-written recursive descent over a fixed grammar: no `eval`, no
attribute access, no calls beyond the helper whitelist, no way to reach the
filesystem, network or process. Unknown functions/variables are refused with a
useful error.

**Edge triggering** (default `edge: true`) fires only on FALSE→TRUE. The previous
decided result is persisted (`AutomationConditionState`), so it is restart-safe;
`UNAVAILABLE` never overwrites the last decided result, so a data gap cannot
manufacture a re-trigger.

## Event routing and deduplication

An event type maps to its subscribers through `AutomationEventSubscription` (an
indexed join), so an event never evaluates every automation. Each `(automation,
eventId)` is recorded in `AutomationEventDedup`: a redelivered event fires the
automation at most once unless it is explicitly re-fetched. An optional gate
condition on an event trigger is evaluated against the event's data (bare keys and
`data.<key>`, plus `event.type`).

**A finished run publishes a completion event.** When an execution reaches a
terminal state the engine emits `automation.completed` (or `automation.failed`
for `failed`/`timed_out`) with `{automationId, executionId, runId, status,
triggerType, teamRunId}`. The run's **output is deliberately not on the event** —
it stays on the execution, so a bus event never carries a memo or a transcript.
This is the one chain contract: a monitor bot can wake a desk with an event
trigger filtered by `automationId`, and a desk reads the monitor's last output at
run time (labelled unverified) rather than from the bus. A paused bot still cannot
be woken, because the event launch requires `active`.

## Actions

`execute_task`, `execute_workflow`, `run_agent`, `delegate_agent`, `call_tool`,
`create_task`, `send_notification`, `generate_report`, `run_code`,
`browser_action`, `custom_action`. Each maps onto the existing runtime:

- `execute_task` with a skill → `service.run_skill`; with an objective → a Task
  System task (`service.tasks`) started, run via `service.run`, then completed or
  failed.
- `run_agent` / `generate_report` / `delegate_agent` → `service.run` (a Task System
  task is created + started + finished around it).
- `execute_workflow` → `service.run_workflow` for a legacy harness workflow;
  with `action.config.graphWorkflowId` it runs a Graph Studio workflow (JDDS)
  through the front door's `/api/workflows/{id}/runs` + `/api/graph-runs/{id}`
  bridge and polls to a terminal state. The bridge uses the normal read-only
  TEST envelope.
- `call_tool` / `run_code` / `browser_action` / `custom_action` → the ONE
  `ToolRegistry.execute` with a real `ToolContext`.
- `create_task` → registers work in the Task System (no execution).
- An agent action with `config.tasks` runs through the Orchestration layer
  (`parallel_execute`) — the existing parallel primitive.

### External ACP agents

`action.type = "run_external_agent"` runs the objective through an external
[Agent Client Protocol](https://agentclientprotocol.com/) process instead of the
native loop. The executable and argv are separate (never a shell), and the
client in `python/harness/acp_client.py` performs
`initialize → session/new | session/load → session/prompt`, collects
`session/update` text and answers `session/request_permission` with **deny**
unless `config.externalAgent.permissionPolicy == "allow"` (then only an
`allow_once` option is selected). `session/load` is used when the agent
advertises `loadSession` and a `sessionId` is configured; a failed resume falls
back to a new session rather than silently using the wrong history.

MCP server descriptors (`mcpServers`) are passed through to `session/new` and
`session/load`. Filesystem and terminal client methods are negotiated ONLY when
`fsRead`, `fsWrite` or `terminal` is explicitly true; path arguments are confined
to the bot workspace, writes cannot escape it, and terminal commands are argv (no
shell). With the defaults (all false) those methods are refused. Timeouts,
malformed JSON, a missing session id and an abnormal exit are typed failures,
never a fabricated answer.

Config:
`action.config.externalAgent = {command, args, cwd?, permissionPolicy?, timeoutS?,
sessionId?, mcpServers?, fsRead?, fsWrite?, terminal?}`.
The Bots detail page exposes the command/args/allow fields and switches the
action type on save. It is deliberately **not** on `bot_create` / `bot_update`:
an agent must not be able to turn a bot into an arbitrary executable. Only the
operator UI or an operator-gated API edit can set it.
This remains a small ACP client, not the full SDK (no MCP tool-call bridging
beyond session setup, no editor integration).

A `messaging` delivery target is now a REAL send through the channel gateway:
`destination` is `<kind>:<external chat>` (for example `telegram:12345` or
`slack:C123`), and `deliver_automation` calls the one
`ChannelService.send_outbound(kind, chat, report)` with the same rendered run
report. A missing channel or a failed send is recorded per target
(`DELIVERY_FAILED`), never raised into the run. A destination without the
`kind:chat` shape keeps the legacy template event as a compatibility fallback.

## Prompts name capabilities, not tools

A scheduled job's prompt is persisted once and re-sent on every fire, so a tool
name baked into it breaks the job the day that tool is renamed or replaced — and
does so silently until a delivery is missing. The rule: **state the capability**
("new regulatory filings", "overnight news", "last price and volume"), and let the
agent route to whatever tool currently provides it.

`jobs/prompt_policy.py` is the executable rule: `lint_prompt(text, tool_names)`
returns every tool mention with its index and a rewrite hint, `lint_templates`
checks a template's `objective`/`prompt`, and `is_clean` is the boolean form.
`test_job_prompt_policy.py` runs it over every bundled job template against the
agent's real tool names, so a template cannot ship with a tool id in its prompt.
It is a lint, not a rewriter: editing user text silently would be worse than the
problem it fixes.

## Reliability

- **Durability** — the source of truth is the database, not an in-memory timer.
  On restart the engine reclaims expired leases, reconciles interrupted
  executions (marks them `INTERRUPTED`; re-launches when retry allows), recomputes
  `nextRunAt` for every active timed automation and applies the misfire policy.
- **Locking** — a lease (`AutomationLock`) guards each execution; an expired lease
  is reclaimable, so a crashed worker never leaves an execution stuck and two
  workers never process the same execution. The lease is **renewed** at a third of
  its TTL for the life of the run, so a long/retrying execution cannot outlive it.
- **Idempotency** — events are deduplicated durably; side effects (notifications,
  artifacts, external actions) are at-least-once with the dedup key.
- **Retry** — bounded attempts with exponential backoff + jitter. A non-retryable
  error class (`PERMISSION_DENIED`, `INVALID_CREDENTIAL`, `TOOL_FORBIDDEN`, …) is
  NEVER retried.
- **Concurrency** — `maxConcurrentRuns` with `skip_if_running` (default),
  `queue_if_running` (a durable queue drained as capacity frees) or
  `cancel_previous`.
- **Resource limits** — runtime is enforced as a hard timeout; tool-call/token/cost
  limits are enforced by the run where the runtime supports it and otherwise
  detected at run end (providers bill per call), failing the execution honestly
  with `RESOURCE_LIMIT_EXCEEDED`. This is documented, not hidden.
- **Misfire** — a fire that came due within `MISFIRE_GRACE_S` (60s) of startup is
  treated as ON TIME (run once) — a restart near the fire time is not an outage, so
  it never records a spurious `skipped`. Beyond that window the policy applies:
  `skip` records a skipped execution and advances the anchor; `run_once` launches
  one; `catch_up` launches up to `catchUpMax` (bounded by the operator's cap —
  default 1 — so it is never an unbounded storm). **The default is `run_once`**
  (it was `skip`): `skip` silently DROPPED every fire that came due while the app
  was closed, so a daily bot only ran if Eclipse happened to be open at that
  minute. A missed slot is recorded at most once — recovery is keyed by the slot
  (`scheduledFor`), so repeated restarts cannot spam the same skipped run.
  Existing automations saved under the old `skip` default are upgraded once to
  `run_once` by a guarded migration in `AutomationStore`; a later, deliberate
  `skip` is preserved.
- **Manual runs never move the schedule** — only a scheduling trigger type advances
  `nextRunAt`; `run_now`, a webhook, or a recovery re-run leave it untouched.
- **Breaker** — repeated failures open a breaker (reusing `mcp/circuit.py`
  thresholds); only an operator reset closes it.
- **Per-run model** — an automation may pin its own provider/model
  (`action.config.modelSettings`, set from the Bots UI). The dispatcher uses it for
  that run while credentials still resolve from the runtime's configured default,
  so a pinned model on an unconfigured provider fails honestly. The bot's
  interactive chat and the Graph Studio `agent.bot` node apply the same pin
  (provider/model from the bot, credentials and base URL from the run), so the
  bot answers with one model everywhere. `execution.timeoutS`, when set, is a tighter per-attempt cap than the
  run's `maxRuntimeS`.
- **Queue drain** — the durable `queue_if_running` queue is drained against the
  IN-FLIGHT count (not every non-terminal row), so a queued run does not count
  itself as occupying a slot and the queue actually starts once capacity frees.

## Security

- **Permissions** — the effective policy is deny-wins; an automation can never
  silently gain a permission because an agent created it.
- **Approval gates** — `permissions.requiresApproval` pauses an execution in
  `waiting` and claims a single-use, digest-bound `ApprovalStore` approval; the
  decision is the existing operator approval surface.
- **Secrets** — an automation stores only a credential REFERENCE
  (`action.credentialRef`), never a value. Validation refuses secret-like keys
  embedded in an action's config/args.
- **Snapshots** — the execution snapshot captures the effective trigger/condition/
  action/policies and NEVER credentials.

## Versioning

Every definition change bumps `version`; the prior version is written immutably to
`AutomationVersion`. Executions retain the version they ran under
(`automationVersion`), so "why did it behave this way six months ago" is
answerable from the execution snapshot.

## Bot memory (private per-conversation)

A bot is one long-lived conversation with its **own private durable memory**
(ADR-044 / ADR-H194). Bots are the automations tagged `bot` (portfolio monitors
carry `bot` + `monitor`); jobs and event rules share the same engine but are not
bots, so Bots mode, `bot_list` and the run watcher filter them out. The bucket is
the Eclipse-Memory `conversation` scope keyed
by `scopeId = bot-<automationId>` — the SAME id the interactive bot chat uses
(`bot-chat.tsx` → `sessionId`), so what a scheduled run learns is recalled when
the user next talks to the bot. A member of a group room keeps a per-room bucket
(`group-<gid>-<botid>`). A conversation reads only its own bucket, the signed-in
user's own rows and truly-global `general` knowledge — never another bot's or
another conversation's memory. Deleting a bot (from the Bots UI or `bot_delete`)
archives its conversation memory (never hard-deletes). The Bots UI Memory panel
lists the bucket and offers an explicit "Forget this conversation's memory"
control. No new database or table: this rides the ONE Eclipse-Memory pipeline.

## Observability

`GET /automations/metrics` reports automation/execution counts, retries,deliveries, open breakers, in-flight work, worker id and tick latency. Each
execution carries a timeline (`AutomationExecutionEvent`), per-target delivery
outcomes, and the run's **`output`** (the answer it produced, bounded) — so a
finished unattended run is READABLE, not just "completed". `GET
/automations/{id}/status` reports success rate, average duration, cost and tokens.
`GET /automations/statuses` returns the same lifecycle fields plus only the latest
execution per automation, in ONE request (optional `?ids=a,b`); it is the list /
sidebar read, so rendering N bots never fans out N status calls.

## API and legacy compatibility

New tool API (operator-gated): `create/get/list/update/delete_automation`,
`enable/disable/pause/resume/archive`, `run_automation_now`, `dry_run`,
`test_automation`, `validate_automation`, `get_automation_status`,
`get_automation_history`, `get_automation_execution`, `cancel_automation_execution`,
plus `reset-breaker`, `versions` and `metrics`. A WAITING execution is resumed with
`POST /automations/executions/{id}/resume` once its approval is granted — the
execution keeps its id, version and snapshot.

The legacy `/jobs` routes (`create_schedule`, `list_schedules`, `update_schedule`,
`pause/resume_schedule`, `delete_schedule`, `run_now`, webhook `trigger`) are a
thin ADAPTER over the same service. A job created through `/jobs` is an automation
in `/automations` (same id, same store) — verified by test.

## Agent scheduling (Phase 17, ADR-H187)

The chat agent can put its own recurring work on this scheduler — from a FIXED
catalogue and only behind the operator's master switch:

- `automation/catalogue.py` — nine research/analysis shapes, each referencing an
  EXISTING `jobs.templates` template (no code or deploy shape). One row per
  shape; `catalogue_issues()` fails on drift.
- `automation/scheduling_tools.py` — `job_list`, `job_describe_template`,
  `job_create` and `job_run_now` on the ONE registry. `job_create` ALWAYS saves
  a `disabled` automation tagged `job`+shape id and accepts only the shape's
  closed parameter set; the operator enables it under Scheduled Jobs.
  `job_run_now` refuses a switched-off job (mirrors `bot_run`).
- Master switch — the eclipse-memory policy key `agent_scheduling_enabled`
  (default OFF), surfaced as "Let Eclipse schedule work for you" in the Policies
  tab. `graph.surface_disabled_tools` removes the whole scheduling surface when
  off (reads included) and every handler re-checks it fail-closed.
- Unattended answer contract — `automation/delivery.py::render_run_report`: the
  FIRST line of a delivered report states the outcome in plain words, every
  limit the run hit is named, and a completed run that produced nothing says so.
  A run that left an **order awaiting the operator's decision** reports that
  instead of a generic success: `AWAITING APPROVAL — <order summary> (good for N
  minutes)` plus the approval id. The rows are found by the run that requested
  them (`McpApproval.requestedByRunId`, written by `order_preview`), never by
  parsing the answer text. A trade approval's window is 300 seconds
  (`APPROVAL_TTL_SECONDS`) and is stated in the report, because a stale market
  order is worse than none — a bot meant to be approved at the open should be
  scheduled near it. The in-app delivery event also carries `botId` and
  `approvalIds`, so the operator approves in that bot's conversation.

## Migration

`AutomationStore.migrate_legacy_jobs()` runs on first use of the automation store:
it imports every `HarnessJob` row (mapping `file_watch → file.changed` event,
`webhook → webhook.received`, skill/objective targets → actions) preserving ids,
and copies `HarnessJobRun` history into `AutomationExecution`. It is idempotent and
never deletes legacy rows. Existing data is preserved.

## Canonical example

```yaml
automation:
  name: BTC Morning Analysis
  trigger:
    type: cron
    expression: "0 8 * * *"
    timezone: "Asia/Karachi"
  condition:
    expression: "BTC.price < 90000"
    edge: true
  action:
    type: run_agent
    objective: "Analyze BTC and say whether the trend is bullish or bearish."
  execution:
    maxConcurrency: 1
    maxRuntimeS: 1800
    misfire: run_once
  retry:
    maxAttempts: 3
  delivery:
    adapter: in_app
```

## Troubleshooting

- **"It didn't run at 8am."** Check `GET /automations/{id}/status` for `nextRunAt`
  and the trigger timezone. A missed window is recorded as `skipped` (misfire
  `skip`) or launched once (`run_once`); it is never silent.
- **"It's paused."** The breaker opened after repeated failures. `reset-breaker`
  closes it.
- **"A condition never fires."** The referenced variable is `UNAVAILABLE` (no fact
  provider resolved it) — check the market gateway is configured; or the condition
  is already TRUE (edge-triggered conditions fire on the transition only).
- **"It ran twice."** Check the concurrency policy; `skip_if_running` is the
  default. Duplicate event delivery is deduplicated by `eventId`.
- **"Why did it behave that way?"** Open the execution and read its snapshot and
  timeline.

## Headless host (always-on bots)

`npm run bots:headless` starts ONLY the Python runtime (no Electron, no Next),
with the same scheduler, stores and bot definitions. It is the supported
always-on path for a VPS or spare machine: scheduled/event/condition/webhook
bots keep firing while the desktop is closed. It binds `127.0.0.1:8765` by
default (`BOTS_HOST` / `BOTS_PORT` to change it); the runtime's existing
non-loopback guard still requires `CRON_SECRET` and, in multi-tenant mode,
`HARNESS_AUTH_SECRET`. There is no UI in this process, and graph-workflow bots
that call back into the Next front door need that front door on the same host.
`scripts/bots-headless.js`.

## Operational notes

- **Always-on.** The engine runs inside the app process, so a schedule only  fires while Eclipse is running. The desktop tray's **Run in background**
  switch does both halves of "always on" — it starts Eclipse (minimized) at
  sign-in and keeps the server running when the window closes. The machine must
  stay awake: a sleeping PC has no scheduler. A fire that came due while the app
  was closed still follows the misfire policy (`run_once` by default).
- **Per-run wall clock.** `execution.maxRuntimeS` defaults to **1200 s** — the
  same 20-minute floor as one chat turn — and can be raised to 24 h.
  `execution.timeoutS`, when set, is a tighter per-attempt cap.
- `service.start_automation()` / `stop_automation()` are wired in `boot.py`;
  `start_jobs()` / `stop_jobs()` remain as deprecated aliases that delegate to the
  engine.
- The engine is single-process; the lease makes that explicit and crash-safe. A
  multi-process deployment would share the same `AutomationLock` table.
- `AutomationEventDedup` is pruned on startup (30-day retention).
