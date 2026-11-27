---
title: Agent-to-Agent (A2A) Subsystem
status: Current
last-verified: 2026-11-26
---

# Agent-to-Agent (A2A) Subsystem

**Who this is for:** anyone adding an agent, wiring delegation, or debugging a
multi-agent run.

The A2A layer is how Eclipse runs specialised agents that delegate to, question,
monitor and merge each other's work. It is a first-class subsystem built **on
top of** the existing runtime — it never forks the execution plane, the tool
registry, the event bus, the Task runtime or the persistence layer.

## The one rule

> A delegated task IS a normal `service.run`. A message IS a durable row.

Delegation creates a durable **Task** (via the Task runtime) assigned to the
target agent; the existing `TaskScheduler` dispatches it through the ordinary run
path. Messaging persists an `AgentEnvelope` plus one delivery row per recipient.
There is no second agent loop, no in-memory-only queue, and no separate database:
everything lands on `agent-memory.db`.

## Package map (`python/harness/a2a/`)

| File | Owns |
|---|---|
| `model.py` | Closed vocabularies (status/type/delivery/priority) + validated contracts: `AgentDefinition`, `AgentEnvelope`, `AgentBudget`, `Finding`, `Conflict`, `A2AResult`, `MergedResult`, `DelegationRecord`. |
| `protocol.py` | Typed messages, per-type payload validation, correlation, the **idempotency key** (identity is content — correlation is excluded so retries dedupe). |
| `store.py` | Durable tables: `A2AAgent`, `A2AAgentVersion`, `A2AMessage`, `A2AMessageDelivery`, `A2ADelegation`, `A2AEvent`, `A2ABudget`, `A2AApproval`, `A2AResult`. |
| `registry.py` | `AgentRegistry`: register / update (versioned) / remove / get / list / enable / disable / validate, plus the capability index. |
| `bus.py` | `MessageBus`: publish (idempotent) / consume / acknowledge / reject (retry) / dead-letter. Wakes live consumers on the ONE `EventBus`; the row is truth. |
| `budget.py` | `BudgetTracker`: per-root-run limits (agents/depth/tasks/tokens/cost/runtime/tool-calls); child budgets only narrow. |
| `permissions.py` | Least privilege: capability/tool/effect authorization, the trading approval gate, and the spawn **intersection**. |
| `results.py` | Normalize, deduplicate, link evidence, detect numeric conflicts, compute coverage/confidence, synthesize. |
| `supervisor.py` | `review_results` / `qa_checklist` → PASS / REWORK_REQUIRED / BLOCKED with rework task ids. |
| `service.py` | `A2AService`: delegate_task, spawn_agent, send/receive/ask/broadcast, wait/cancel, merge, review, approvals, supervise/QA. |
| `agents_seed.py` | The built-in agent catalogue (orchestrator, supervisor, research, financial, valuation, risk, market-data, news, coding, QA, security, trading) + `AgentConfig` presets. |
| `tools.py` | `A2A_TOOLS` + `register_a2a_tools` + the per-run `a2a_for(ctx)` cache. |
| `bootstrap.py` | `install_a2a_layer(service)` / `recover_a2a(service)`. |

## Agent registry (§5, §6)

An agent is **data**, not code: a versioned `AgentDefinition` with `capabilities`
(nouns), `tools`, `permissions`, `memory_policy`, `evidence_policy`, delegation
rights, `max_concurrency` and `timeout_ms`. The capability index maps each noun to
the active agents that declare it, so the planner can ask *"who can do this?"*
without guessing. The catalogue seeds both the registry **and** `AgentConfig`
presets, so `service.run` can dispatch to an agent id (`valuation-agent`, …).

Least privilege is enforced at every A2A seam (`permissions.action_allowed`):
holding a capability is not the same as being allowed to act on it — approval-
gated capabilities (trading, deployment, external actions) require an explicit,
single-use approval. `trading-agent` holds **no analysis tools** and can only act
on an already-approved order.

## Protocol + bus (§10, §11)

Every message is an `AgentEnvelope` with a closed `MessageType`
(TASK_REQUEST/RESPONSE, FINDING, EVIDENCE, QUESTION, ANSWER, DELEGATION, STATUS,
FAILURE, APPROVAL_*, REVIEW_*, CANCEL). Payloads are validated per type. A
message with no recipient is a **broadcast** (one delivery per active agent).

`MessageBus.publish` is idempotent (a UNIQUE `idempotencyKey`), durable, and
retryable: `consume` claims pending deliveries, `acknowledge` completes them,
`reject` counts attempts and **dead-letters** past the limit. Expired (TTL)
messages are dead-lettered, never delivered.

## Delegation, spawning, waiting (§13, §14, §15, §18)

- `delegate_task` resolves the target (explicit id, or the first capable agent),
  checks the sender may delegate, checks the target holds the capability, reserves
  budget (depth/agents/tasks), creates a durable child **Task**, records the
  `DelegationRecord`, and publishes DELEGATION + TASK_REQUEST. It returns a task
  handle **immediately** — delegation is non-blocking.
- `spawn_agent` builds a temporary agent from `parent ∩ base` (tools and
  capabilities intersected; `can_delegate` forced off) and delegates to it. It can
  never widen privileges.
- `wait_agent` is **event-driven**: it awaits the task's terminal bus event, never
  a poll or a sleep.
- `ask_agent` sends a QUESTION and delegates a task to answer it; optional `wait`.

## Results, conflicts, supervisor, QA (§19–§25)

Each agent returns a structured `A2AResult` (summary, findings, evidence refs,
outputs, conflicts, gaps, confidence, metrics, next actions). A `Finding` is a
claim with confidence and **evidence references** — never bare prose.

`merge_results` normalizes, deduplicates, links evidence, detects numeric
conflicts (same subject, different value), names gaps, and computes a
coverage-and-evidence-weighted confidence. A finding with no verifiable evidence
is kept but listed as **unsupported** and its confidence is capped — it is never
promoted to fact. Findings are verified against the real `EvidenceStore` when one
is attached.

`supervisor.py` turns that into a verdict (PASS / REWORK_REQUIRED / BLOCKED) with
the exact task ids to rework, so the orchestrator can route work back.

## Observability (§36, §37)

`A2AService` persists `agent.*`, `message.*`, `delegation.*`, `approval.*` and
`result.*` events to `A2AEvent` and publishes them on the ONE process bus. Budget
consumption (agents/tasks/tokens/cost/runtime/tool-calls) is durable per root run
and exposed on `/a2a/budget/{runId}`.

## Routes (`/a2a/*`, operator-gated)

`GET /a2a/agents`, `GET /a2a/agents/{id}`, `GET /a2a/agents/{id}/status`,
`POST /a2a/agents`, enable/disable/delete, `GET /a2a/capabilities`,
`POST /a2a/delegate`, `POST /a2a/spawn`, `GET /a2a/delegations`,
`GET|POST /a2a/messages`, `GET /a2a/messages/dead-letters`, `POST /a2a/merge`,
`GET /a2a/qa/{runId}`, `GET|POST /a2a/approvals…`, `GET /a2a/events/{runId}`,
`GET /a2a/budget/{runId}`, `GET /a2a/status`. Every route requires operator auth.

## Not (yet) built

The Agent Studio **graph / live-execution / event-stream** views (spec §41) are
not built; the `/a2a/*` routes and the Tasks screen expose the underlying data.
Cross-run agent conversations remain durable messages a later run consumes — the
runtime is not a continuously-listening daemon.

The Bots scheduler dock does expose a compact **Teammates** panel over the same
routes (`/api/a2a/*`, `src/components/bots/bots-team-panel.tsx`): it lists the
registered agents and newest envelopes and can post a `STATUS` message. That is
a visibility/send surface, not a replacement for the Agent Studio views.

## Tests

`python/tests/test_a2a_{core,bus,delegation,results,security,tools_api}.py`
(registry/versioning, protocol idempotency, durable delivery + dead-lettering,
delegation/spawn/wait/cancel/ask, merge/conflict/QA, least-privilege/trading/
injection, tools + operator-gated routes).
