---
title: Hermes vs Eclipse — Feature Gap Matrix
status: Current
last-verified: 2026-11-26
legend: "Status: ✅ implemented · ◐ partial · ○ stub · — absent. Gap: A architectural · I implementation · F feature. Priority: P0–P3."
---

# Feature Gap Matrix

Status is based on inspected code, not marketing. "Implemented" means the
mechanism exists and is on the normal path; "partial" means it exists but is
gated, unused, or weaker than the comparison; "absent" means no implementation
was found after searching.

Paths are abbreviated. `BE/` = `Black-Eclipse-site/python/harness/`; `H/` =
Hermes repository root.

---

## 1. Core architecture and system design

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `run_agent.py`, `agent/conversation_loop.py`, `agent/turn_*.py`, `hermes_state*.py` | `BE/graph.py`, `BE/service.py`, `BE/main.py`, `docs/harness/core-architecture.md` |
| Shape | Synchronous multi-mixin loop; 2,109 py files / ~664k LOC core; 20+ channel gateway | FastAPI + LangGraph + Next.js; 1,749 py / ~431k LOC; one page SPA + 31 tabs |
| Gap | **I** — provider/channel maturity | **A** — stronger seams, but many modules |
| Priority | P0/P1 | — |

## 2. Agent runtime and execution lifecycle

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `agent/conversation_loop.py`, `turn_tool_round.py`, `IterationBudget`; 500 default iterations | `BE/graph.py:1345-1378`, `step.py`, `run_lease.py`, `run_lineage.py` |
| Strength | Interruptible calls, mid-session fallback, 3 API modes | Lease/fencing/checkpoint/resume/effect ledger |
| Gap | **A/I** — Hermes has no resumable run | **I** — counters unfenced, checkpoints non-atomic |
| Priority | — | P0 |

## 3. Tool calling and orchestration

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `tools/registry.py`, `agent/tool_executor.py:136` (`_MAX_TOOL_WORKERS=8`), `tools/tool_search.py` | `BE/tools.py:580`, `BE/graph.py:962` |
| Strength | Concurrent batch, ordered results, guardrails, default disclosure | Per-tool permission/risk/effect, validation, output spill, policy |
| Gap | **I** — sequential Eclipse tools; lexical opt-in search | **A** — stronger contract model |
| Priority | — | P1 (parallel, disclosure), P1 (guardrails) |

## 4. Planning, reasoning and task decomposition

| | Hermes | Eclipse |
|---|---|---|
| Status | ◐ (prompt + `/goal`) | ◐ (plan mode, replan off, goals runtime) |
| Evidence | `tools/todo_tool.py`, `website/docs/user-guide/features/goals.md` | `BE/plan_mode.py`, `BE/replan.py`, `BE/goals/runtime.py`, `BE/graph_runtime/planner.py` |
| Strength | Judge-driven continuation, completion contracts, quality gates | Typed plans, evidence claims, verifier registry |
| Gap | **F** — no goal driver in Eclipse | **F** — no enforceable plan in Hermes |
| Priority | — | P1 |

## 5. Persistent memory and context management

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ (small built-in + providers) | ✅ (structured) |
| Evidence | `tools/memory_tool.py` (2,200/1,375 chars), `agent/memory_manager.py`, `tools/session_search_tool.py` | `BE/eclipse_memory/*`, `BE/memory_providers/*`, `BE/session_index.py` |
| Strength | Provider hooks, prefetch/sync, session search UX | Conflicts, authority, evidence, retention, FTS5 session search |
| Gap | **I** — Eclipse retrieval is lexical; `hybrid.py` not wired; no periodic consolidation | **A/F** — Hermes' built-in memory is shallow |
| Priority | — | P1 |

## 6. Skills architecture and dynamic creation

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `agent/prompt_builder.py:1240+`, `agent/curator.py`, `skills/`, 44 bundled + 141 optional | `BE/skills/*`, 91 manifests, `skills/evaluation.py`, `draft.py`, `versioning.py` |
| Strength | `/learn`, curator, hub, use-time patching | Trust, scan, versions, eval, promotion, rollback |
| Gap | **I/F** — keyword-only matching, static eval, no curator | **I** — no consolidation, less authoring ergonomics |
| Priority | — | P0 (behavioural eval), P1 (curator/`/learn`) |

## 7. Learning from mistakes and self-improvement

| | Hermes | Eclipse |
|---|---|---|
| Status | ◐ (prompt nudges + curator) | ✅ but mostly dark |
| Evidence | `agent/prompt_builder.py:261`, `agent/curator.py` | `BE/eclipse_memory/mistakes.py`, `BE/skills/learning.py`, `BE/self_improvement/*` |
| Strength | Actually closes the loop | Mistake taxonomy, recurrence, lessons, validation, drift, gates |
| Gap | **A** — no deterministic ledger | **I/F** — RSL off, lessons operator-only |
| Priority | — | P1 |

## 8. Long-running and autonomous execution

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ◐ |
| Evidence | `/goal`, `/loop`, `/heartbeat`, `cron/`, `kanban` | `BE/jobs/`, `BE/automation/`, `BE/tasks/`, `BE/fabric/`, `BE/goals/` |
| Strength | Coherent user-facing autonomy | Durable substrate, budgets, leases |
| Gap | **F** — no in-session goal/loop/heartbeat in Eclipse | **A** — Hermes lacks Eclipse's durability |
| Priority | — | P1 |

## 9. Background agents and parallel execution

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `tools/delegate_tool.py`, `tools/async_delegation.py`, `tools/process_registry.py` | `BE/delegation.py`, `BE/spawn/`, `BE/fabric/`, `BE/orchestration/`, `BE/a2a/` |
| Strength | Async completion, worktrees, images, schema repair | Durable jobs, budgets, blackboard, deterministic merge |
| Gap | **A** — Eclipse lacks async UX/steering | **A** — Hermes lacks durable child jobs |
| Priority | — | P2 |

## 10. Multi-agent orchestration and delegation

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `delegate_task`, `agent/subagent_lifecycle.py`; batch 10 | `BE/team/review.py`, `BE/institution/preset.py`, `BE/orchestration/`, `BE/spawn/board.py` |
| Strength | Ease of fan-out, lifecycle API | Independence, conflict blocking, claim merge, institution roles |
| Gap | **I** — Eclipse ergonomics | **A** — Hermes merge is prose |
| Priority | — | P2 |

## 11. Shell, terminal and filesystem

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `tools/terminal_tool_backends.py` (6 built-in + plugin backends), `tools/process_registry.py`, `tools/checkpoint_manager.py` | `BE/code/sandbox.py`, `BE/terminal/*`, `BE/code/fs/*`, `BE/git/*`, `BE/code/worktree.py`; per-bot roots in `BE/code/workspace.py` (`bot:<id>` / `.black-eclipse/bots/<id>`) |
| Strength | Remote backends, background watch, rollback | Policy, secret scrub, git risk, process ownership, per-bot workspace isolation across chat and scheduled runs |
| Gap | **I** — Eclipse `code/shell.py` bypass; no remote; no file rollback | **I** — Hermes less policy depth |
| Priority | — | P0 (bypass), P1 (rollback), P2 (remote) |

## 12. Browser automation and computer use

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `tools/browser_*`, multiple cloud/local backends; `tools/computer_use_tool.py` | `BE/browser/*` (~100 tools), `BE/computer/*` (22 tools) |
| Strength | Cloud/real-profile breadth | Policy, finance firewall, evidence, verification |
| Gap | **I** — mutating browser tools declared `read` at registry | **I** — fewer backends |
| Priority | — | P2 |

## 13. MCP and external integrations

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `tools/mcp_tool*.py`, OAuth/device flow, spend limits | `BE/mcp/*`, SSRF/OSV/screening/trust/approvals/audit |
| Strength | OAuth breadth, spend limits, tool-search integration | Security review, digest approvals |
| Gap | **I** — Eclipse lacks spend metadata; stdio unsandboxed | **I** — Hermes less security depth |
| Priority | — | P2 |

## 14. Scheduling and recurring tasks

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ (three paths) |
| Evidence | `cron/`, no-agent mode, event triggers, preflight | `BE/jobs/*`, `BE/automation/*`, Graph Studio schedules; headless host `scripts/bots-headless.js` |
| Strength | One product surface, preflight, skips tokens on misconfig | Conditions, misfire, breaker, DAG, always-on Python-only host |
| Gap | **A** — Eclipse has multiple schedule owners | **I** — Hermes less workflow-native |
| Priority | — | P1 (consolidate) |

## 15. Session management and continuity

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `hermes_state*.py`, FTS5+trigram, admissions, lineage | `BE/persistence.py`, `BE/session_index.py`, `BE/routers/sessions.py`; per-bot/per-room transcripts `BotChatMessage`/`BotGroupMessage` + `BotRoom` |
| Strength | Canonical writer, admission ledger, recovery | Run lineage, filters, per-run index, durable bot transcripts |
| Gap | **I** — Eclipse summary not injected; no canonical writer rule | **I** — Hermes no run lineage |
| Priority | — | P2 |

## 16. Checkpointing, recovery and fault tolerance

| | Hermes | Eclipse |
|---|---|---|
| Status | ◐ (files) | ✅ (runs) |
| Evidence | `tools/checkpoint_manager.py`, persist-before-tool, admissions | `BE/run_lineage.py`, `run_lease.py`, `run_effects.py`, `run_watchdog.py`, `run_recovery.py` |
| Strength | File rollback, process cleanup | Run resume, fencing, at-most-once |
| Gap | **A** — different targets; Eclipse needs file rollback | **I** — atomicity/lease-aware watchdog |
| Priority | — | P0 (writes), P1 (file rollback) |

## 17. Permissions, security and sandboxing

| | Hermes | Eclipse |
|---|---|---|
| Status | ◐ (personal-agent model) | ✅ (production model) |
| Evidence | `tools/approval.py`, `approval_floors.py`, `approval_detection.py`; YOLO | `BE/security/gateway.py`, `floor.py`, `effect_tokens.py`, `audit.py`, `profiles.py` |
| Strength | Approval UX, hardline, deny globs | One chain, effect tokens, tenant, audit chain |
| Gap | **A** — Hermes command-pattern model; **I** — Eclipse shadow default/best-effort gateway | **I** — Eclipse attachment of effect tokens |
| Priority | — | P0 (enforce/floor), P2 (UX) |

## 18. Observability, tracing and audit

| | Hermes | Eclipse |
|---|---|---|
| Status | ◐ (metrics/logs) | ✅ |
| Evidence | `hermes_cli/observability/*`, `agent/insights.py`, `agent/trace_upload.py` | `BE/observability/*`, `BE/event_system/*`, `BE/run_observability.py`, `BE/run_outcome.py`; bot audit `AutomationAudit` + `/automations/audit` + Bots Audit section |
| Strength | Product metrics, failure classifier, trace upload | Span tree, SLO, exporters, replay, audit, bot CRUD/lifecycle audit |
| Gap | **A** — no run-span/SLO in Hermes; **I** — Eclipse two buses/seq bug | — |
| Priority | — | P0 (seq), P1 (unify) |

## 19. Model/provider abstraction and routing

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ◐ |
| Evidence | `runtime_provider.py`, `error_classifier.py`, `credential_pool.py`, fallback | `BE/model_runtime/runtime.py:454`, `routing.py:191`; graph bypasses `invoke` |
| Strength | Failover, 18+ providers, 3 modes, aux routing | Capability/quality scoring, cost, health |
| Gap | **I** — Eclipse router unused on live path | **I** — Hermes less scoring |
| Priority | — | **P0** |

## 19a. External agent interop (ACP)

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ (own runtime + provider adapters) | ✅ (minimal ACP client) |
| Evidence | `acp_adapter/`, provider plugins | `BE/acp_client.py` (initialize, session/new, session/load, session/prompt, `session/update`), `run_external_agent` action, Bots detail field, `test_acp_client.py` |
| Strength | Mature own loop; broad providers | External CLI/agent as a bot brain without adopting its loop |
| Gap | **I** — Hermes does not drive an arbitrary ACP agent as a bot | **I** — no full editor integration; MCP tool-call bridging is session setup, not a client-side tool proxy |
| Priority | — | P2 (full SDK surface) |

Opt-in fs/terminal is explicitly negotiated (`fsRead`/`fsWrite`/`terminal`), path-confined to the bot workspace, and denied by default; MCP server descriptors are passed to `session/new`/`session/load`. Session resume uses `session/load` when advertised.

## 20. Context compression and token efficiency

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ◐ |
| Evidence | `agent/context_compressor.py`, `conversation_compression.py`, 50/85 gates, usage anchor, cooldown | `BE/context_budget.py`, `history.py`, `compaction.py`; heuristic, first-step overflow |
| Strength | Accuracy, cache, ladder, fallback | Deterministic, auditable skeleton |
| Gap | **I** — Eclipse heuristic/shallow | — |
| Priority | — | **P0/P1** |

## 21. Artifact generation and file handling

| | Hermes | Eclipse |
|---|---|---|
| Status | ◐ (files + media) | ✅ |
| Evidence | `tools/file_tools.py`, image/video/TTS, document extraction | `BE/artifacts/*`, `documents/*`, `canvas/*`, `charts/*` |
| Strength | Media generation | Registry, versions, provenance, plan/apply/verify editing |
| Gap | **F** — Eclipse lacks packaged media generation | **I** — Hermes lacks artifact registry |
| Priority | — | P3 |

## 22. Testing, evaluation and benchmarking

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ (tests) / ◐ (evals) | ✅ |
| Evidence | 8,137 test files in tree; `evals/` 33 scripts; Hermes Bench external | 957 test files; `BE/evaluation/*` 43 modules; 17 graders; corpus; scorecard |
| Strength | Test volume, provider matrix | Real-runtime eval, statistics, gates, contamination |
| Gap | **A** — Hermes no scored corpus; **I** — Eclipse lacks external adapters | — |
| Priority | — | P1/P3 |

## 23. Extensibility and plugin architecture

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | `hermes_cli/plugins.py`, provider plugins, memory/context/browser/terminal providers, 106 plugin manifests | `BE/plugins/*`, signing/sandbox, `BE/capabilities/*`, SDK |
| Strength | Provider breadth, catalog | Signing, review, sandbox, generated capabilities |
| Gap | **I** — Eclipse lacks a public catalog/provider SDK | **I** — Hermes less distribution security |
| Priority | — | P2/P3 |

## 24. Developer experience and usability

| | Hermes | Eclipse |
|---|---|---|
| Status | ✅ | ✅ |
| Evidence | One-line install, TUI, desktop, 20+ gateways, slash commands | `npm install && npm run dev`, desktop, 31 tabs, chat dock |
| Strength | Small command surface, first-run | Domain workbench |
| Gap | **I/F** — Eclipse command surface is larger and heavier | **I** — Hermes lacks finance UI |
| Priority | — | P1 (autonomy commands), P3 (light profile) |

## 25. Performance, reliability and scalability

| | Hermes | Eclipse |
|---|---|---|
| Status | ◐ | ◐ |
| Evidence | 8-worker tools, SQLite WAL, single gateway writer; no distributed lease | Async, leases, Fabric, multiple schedulers; sequential tools, multiple caps |
| Strength | Per-turn latency, context efficiency | Durability, effect safety, audit |
| Gap | **A/I** — Eclipse per-turn latency/cost; Hermes multi-process durability | — |
| Priority | — | P0/P1 |

---

## Summary counts

| Status | Hermes | Eclipse |
|---|---:|---:|
| Implemented (✅) | 17 | 20 |
| Partial (◐) | 7 | 5 |
| Stub/absent | 0 | 0 |

Eclipse's partials are concentrated in **harness mechanics** (model gateway,
context, tool concurrency/disclosure, autonomy UX, observability unification).
Hermes' partials are concentrated in **trust and correctness** (run durability,
security boundary, evidence, evaluation, finance).

## What this means

- Eclipse does not need a new architecture. It needs eight harness mechanisms
  wired into seams that already exist.
- Hermes cannot close Eclipse's gaps by adopting a feature; it would need a
  different data model, a different policy model and a different evaluation
  model. That is Eclipse's moat.
- The highest-leverage work is P0-1/P0-2 (model + run writes), then P1-1/P1-2
  (context + tools), then P1-5 (autonomy). Everything else compounds from
  there.

## Verification notes

- Hermes test-file count is a `git ls-tree` count of `tests/**/*.py` at commit
  `72c8803`; the docs' "~25,000 tests" claim was not independently executed.
- Eclipse tool count: `docs/harness/tool-registry.json` contains 1,237 `name`
  entries (some may be resources/aliases); the docs cite ~1,091 tools. Treat
  "over 1,000" as the safe claim.
- Hermes has no official `hermes-code` repository; the coding posture is
  `_CODING_TOOLS` in `toolsets.py`.
- No financial/quant/portfolio/trading implementation was found in Hermes'
  inspected source; finance references were billing roles and compression
  terminology, not market functionality.
- Eclipse tests run for this investigation: 15 lease/effect/watchdog tests and
  46 memory/skill/self-modification/lifecycle tests passed. No production code
  was modified.
