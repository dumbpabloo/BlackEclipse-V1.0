---
title: Bot Team Orchestration
status: Current
last-verified: 2026-11-26
---

# Bot Team Orchestration

**Who this is for:** anyone running several Bots on one objective, debugging a
team run, or extending the delegation / concurrency / review behaviour.

A **team run** lets an authorised set of specialist Bots collaborate on one
objective through a dependency-aware task graph, with evidence-based conflict
reconciliation and an independent quality review. It is a *composition layer*:
it adds no agent loop, no provider router, no memory store and no task database.
It composes the subsystems that already exist.

## The one rule (reuse, never fork)

> A delegated Bot IS a normal `service.run`. A team run IS a durable Task/Plan.
> A conflict and a review are durable records.

- Delegation goes through the **A2A layer** (`a2a/`): the versioned Agent
  Registry, least-privilege authorization, per-root-run budgets and the durable
  message bus. A team never grants a receiving Bot a permission it does not hold.
- The task graph is the **Task / Plan runtime** (`tasks/`): durable
  `HarnessPlan`/`HarnessTask` rows with atomic claims, leases, retries,
  cancellation cascade and crash recovery.
- Concurrency is the **existing `TaskScheduler`** (`tasks/scheduler.py`); this
  layer only sets per-subtask and per-plan limits (below), it does not run a loop
  of its own.
- Findings are reconciled deterministically; an unresolved conflict is disclosed,
  never averaged away.

## Package map (`python/harness/team/`)

| File | Owns |
|---|---|
| `model.py` | Closed vocabularies + validated contracts: `TeamDefinition`, `TeamMember`, `TeamSubtask`, `TeamPolicy`, `TeamRun`, `TeamFinding`, `TeamConflict`, `ReviewReport`, `RepairAttempt`. |
| `graph.py` | Pure task-graph math (validation, cycle detection, topological layers, readiness), reusing `tasks.dag` so a team graph and a plan graph cannot disagree. |
| `conflicts.py` | Deterministic conflict detection + reconciliation (`detect_conflicts`, `reconcile`, `reconcile_async`). |
| `review.py` | The independent reviewer (`review`) + bounded repair decision (`repair_plan`, `repeat_signature`). |
| `service.py` | `TeamService`: validate → plan → delegate → start → collect → reconcile → review → repair. |
| `store.py` | The durable `TeamRun` table on the ONE `agent-memory.db` connection. |
| `tools.py` | The model-facing `TEAM_TOOLS` (`team_run`/`team_status`/`team_result`/`team_review`/`team_cancel`), on the ONE registry. |
| `preset_library.py` | The bundled finance team library: `load_presets`, `list_presets`, `get_preset`, `render_preset`, `validate_definition`. Validates ids, dependencies, cycles and assignees at load time. |
| `presets/*.yaml` | `investment_committee`, `quant_strategy_desk`, `portfolio_risk_review`, `earnings_review_desk`, `macro_rates_fx_desk`. Data, not code — shipped via `package-data`. |
| `bootstrap.py` | `install_team_layer(service)`; called from the runtime lifespan after A2A + Tasks. |

### The bundled team library

A preset is a **documented collaboration pattern**: members with instructions, a
dependency-aware subtask graph, and a policy. `preset_library.render_preset`
substitutes declared `{variables}` (`{target}`, `{horizon}`, …) into a concrete
`TeamDefinition`; a missing variable raises rather than shipping a literal
`{target}` in a prompt.

`TeamSubtask.evidence_required` is what makes the library more than a prompt
pack: in `TeamService.finalize`, a completed gated subtask with no evidenced
finding becomes a BLOCKING finding, so the reviewer cannot approve it silently.
Every bundled preset gates its research and verdict subtasks.
Structural problems (duplicate ids, unknown/cyclic dependencies, an assignee
that is not a member, a non-orchestrator lead that is not a member) fail at load
time with the file name.


Routes: `python/harness/routers/team.py` (`/team/*`, operator-gated). Front door:
`src/lib/harness/team-client.ts` + `src/app/api/team/[[...path]]/route.ts`. The
**chatbot remains the primary interface**: the `team_*` tools are on the default
agent's allowlist, so a user can start, inspect and review a team in chat.

## Feature A — Bot-to-Bot delegation

`TeamService.run` resolves each subtask's Bot by explicit id or declared
capability **within the authorised member set**, then calls
`A2AService.delegate_task(..., plan_id=<the team plan>, dependencies=[…],
autostart=False)`. That reuses the A2A permission check (`can_delegate`, target
active, capability held, least-privilege tool intersection), the per-root-run
budget reservation (depth/agents/tasks) and the durable `DELEGATION` +
`TASK_REQUEST` messages. The child is a durable `HarnessTask` assigned to the
Bot's **real runtime agent id**, so it executes with its own instructions, tools
and model policy through `service.run` — never a relabelled prompt.

The team plan is started **once**, after every subtask and dependency edge is
durable, so no ready task runs early. `A2AService.delegate_task` now has
`autostart` (default `True`) and `plan_id`; the default path also starts a plan
left in `PLANNING`, closing the gap where an A2A delegation was created but never
dispatched.

## Feature B — Concurrent specialist execution

Independent subtasks run concurrently by construction: the `TaskScheduler` claims
each ready task and launches its run as its own asyncio task, up to the global
in-flight cap. Two new, additive bounds are honoured per task/plan:

- per-**Bot**: `task.metadata.maxConcurrency` (set from `TeamMember.max_concurrency`),
  else the `TaskScheduler(max_per_agent=…)` default (`HARNESS_TASK_MAX_PER_AGENT`);
- per-**team run**: `plan.metadata.maxConcurrency` (from `TeamPolicy.max_concurrency`).

Dependencies are enforced before execution; a failed required dependency blocks a
dependent (it never silently unlocks). Leases, retry/backoff, timeouts and crash
recovery are the scheduler's existing behaviour. `python/tests/test_team_service.py`
proves real overlap (max simultaneous > 1) rather than "marked parallel".

## Feature C — Dependency-aware task graph

`validate_graph` detects duplicate ids, unknown dependencies and cycles before
anything is persisted (cycle detection reuses `tasks.dag`). A team is refused
(`TEAM_INVALID`) when the graph is invalid, a subtask names a non-member, a
capability cannot be routed, `delegationEnabled` is false, or the subtask count
exceeds `maxChildren`. Subtask status is a projection of the durable Task status
(`graph.map_task_status`): pending / running / blocked / completed / failed /
cancelled. A required failed subtask stops the run being reported as a clean
success — the run ends `FAILED`, or `PARTIAL` when a material conflict is
disclosed.

## Feature D — Evidence-based conflict reconciliation

`team/conflicts.py` groups findings by a deterministic subject key and compares
their values (0.5% relative tolerance, the same as the research engine). A
difference is **only** classified benign when metadata explains it — a different
period, unit, currency or accounting basis, an estimate-vs-reported figure, or a
stale source (`STALE_DATA`). Otherwise it stays `UNRESOLVED` and material. Claims
with no verifiable evidence (checked against the real `EvidenceStore` when one is
attached) become `UNSUPPORTED_CLAIM` and are never promoted. Original findings and
their provenance are preserved. An optional investigator (targeted research or an
independent reviewer) is the only path by which a genuine conflict becomes
`RESOLVED`, and its method and evidence are recorded. Values are never averaged,
voted on, or replaced by the newest source.

## Feature E — Independent review and repair

`team/review.py` inspects the actual artifacts: each subtask's terminal status,
the run's output reference, the recorded findings/evidence and the detected
conflicts. It returns a structured `ReviewReport` (`APPROVED` /
`APPROVED_WITH_LIMITATIONS` / `REVISION_REQUIRED` / `REJECTED` /
`UNABLE_TO_VERIFY`) with per-finding severity, evidence refs and actionable repair
instructions. **Independence is enforced:** if the reviewer's agent id is one of
the producers of the work, the verdict is `UNABLE_TO_VERIFY` — a producing Bot can
never approve its own work.

On `REVISION_REQUIRED`, `TeamService.finalize` queues a **targeted repair task**
for each blocking finding, assigned to the producing Bot, and reopens the plan if
the failure had closed it. The repair loop is bounded by attempt count and detects
a repeated failing signature; on exhaustion or no progress the run terminates
explicitly (`FAILED`/`PARTIAL`) rather than looping.

## Lifecycle

1. `team_run` (chat, tool or `POST /team/runs`) calls `TeamService.run`: validate,
   register missing member agents, budget, create one plan, delegate every
   subtask, start the plan, persist the `TeamRun`.
2. The `TaskScheduler` dispatches ready subtasks through `service.run` under the
   concurrency limits.
3. `team_review` / `POST /team/runs/{id}/review` calls `finalize`: reconcile,
   review, queue repairs, and settle a terminal run status.
4. `team_result` returns a deterministic synthesis that discloses unresolved
   conflicts and unsupported claims.
5. `team_cancel` propagates cancellation to every subtask (cascade).

## Settings (on `TeamPolicy`)

`delegationEnabled`, `memberFanout`, `maxDepth`, `maxChildren`, `maxConcurrency`,
`perAgentConcurrency`, `maxTasks`, `maxCost`, `maxRuntimeMs`, `requireReview`,
`reviewStrictness` (NONE/LENIENT/STANDARD/STRICT),
`optionalFailurePolicy` (ALLOW_PARTIAL/REQUIRE_ALL), `allowedProviders`,
`requireApproval`.

`memberFanout` (default **off**) lets a team MEMBER use the `delegate` tool —
split fan-out or `best_of` — inside its own subtask. Enabling it only adds the
tool NAME to a member's declared surface; the delegation's own caps, number
bounds, tool narrowing and permissions still apply, so it grants no authority.
A member with no declared tool surface already inherits the tool.

## Security

Least privilege is enforced in the runtime, at the delegation seam: a member's
tools are intersected by `A2A.permissions`, a disabled or non-capable target is
refused, and a task always executes under its own agent preset's allowlist. A
team cannot widen a Bot's permissions. `allowedProviders` is checked at run time
when a provider is pinned. Consequential actions inside a delegated task still
require the runtime's normal per-action approval.

## Tests

`python/tests/test_team_graph.py`, `test_team_conflicts.py`, `test_team_review.py`,
`test_team_service.py` (delegation, dependency gating, measured concurrency,
per-Bot and per-plan limits, cancellation, durability across a restart, review →
repair → re-verification, conflict disclosure), `test_team_e2e.py` (the real
`AgenticService` + `TaskScheduler` + `service.run`, only the LLM scripted).

## Budgets and approvals (enforced)

- **`requireApproval` is a hard gate.** When a team sets it, `TeamService.run`
  refuses (`TEAM_APPROVAL_REQUIRED`) unless a **granted** single-use operator
  approval exists in the ONE A2A approval store (`kind="team_run"`, `subject=team
  id`). A missing store fails closed.
- **Token and tool-call usage is metered and enforced.** The production task
  executor (`tasks/bootstrap.py`) records each A2A-dispatched run's
  `totalTokens`/`toolCalls` onto the root run's `BudgetTracker`, and refuses new
  work with a non-retryable `BUDGET_EXCEEDED` once the token budget is spent.

## Measured adoption (round 4, W12)

Concurrency is used **only where the benchmark shows a gain** (ADR-H213).
`evaluation/team_benchmark.py` runs a frozen multi-step build both ways on the
same task — one agent, and a 2–3 specialist team through this exact stack
(`TeamService` → A2A → `TaskScheduler` → `execute_run`) — and
`experiments.multi_agent_compare` grades both on the real resulting state,
reporting quality, wall clock and tool calls with a deterministic adoption
verdict (quality must not regress; adopt only on a ≥15% wall-clock gain or
strictly fewer tool calls). The frozen set is
`evaluation/datasets/team_v1.json`; each task carries both its single-agent
program and its team decomposition. The first recorded measurement (2026-10-18,
offline scripted tier, 3 trials × 2 tasks) found equal quality (1.0 vs 1.0) with
+53.6% wall time and more tool calls, so **the default path does not route work
to a team**; the harness is the gate for re-measuring on the live tier. See
`docs/harness/agent-evaluation.md` §13.1.

## Preset composition: the Institution committee

The first shipped preset is the Institution (`docs/harness/institution-preset.md`),
an analyst -> debate -> trader -> risk -> PM graph. It is a thin layer over this
service: one role spec in `institution/cast.py` produces the agent presets, A2A
definitions, `TeamDefinition` and bot specs. Downstream stages read a completed
prerequisite memo by run id with the read-only `read_run_output` tool. The
debate record discloses unresolved disagreements and never votes or averages.
A scheduled preset run uses the `run_team` automation action, which opens a team
run and waits for its terminal synthesis. `POST /team/runs` still starts any
team; the preset routes are `GET /team/presets`,
`GET /team/presets/institution`, `POST /team/presets/institution/run`.

## Known limitations (with exact cause + remediation)

- **Cost and runtime are metered at the task boundary, not pre-empted.** A run
  result's optional `totalCost`/`runtimeMs` (or `cost`/`durationMs`) is now
  recorded on the root `BudgetTracker` by `tasks/bootstrap.py`, alongside tokens
  and tool calls, so a team over `maxCost`/`maxRuntimeMs` refuses NEW work at the
  next task boundary. A provider call already in flight still completes and is
  billed (providers bill per call); the team reports the exhausted budget rather
  than pretending the cap was never crossed. If a result omits cost/runtime,
  those counters remain zero for that run (honest absence, never invented).
- **A team can now be saved and authored as a "desk".** A `run_team` automation
  carries its `TeamDefinition` in `action.config.team`; the Bots builder maps a
  draft to that definition and `dispatcher._run_team` runs it (the Institution
  preset remains the fallback when `config.team` is absent). See
  `docs/harness/desks-plan.md` and the Bots doc.
- **The rich Agent Studio graph / live-execution views are not built.** The
  chatbot (via the `team_*` tools), the `/team/*` routes and the Task screen
  expose the underlying data.
