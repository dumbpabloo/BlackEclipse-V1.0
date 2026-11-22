---
title: Harness Architecture
status: Current
last-verified: 2026-11-16
---

# Harness Architecture

> **Core invariants:** `docs/harness/core-architecture.md` is authoritative for
> the five "ones" and the loop/safety/observability invariants. This file
> explains the subsystems; start there.

**Who this is for:** anyone extending BlackEclipse's agentic layer — adding an
agent, a tool, a model provider, or a front-door surface.

The **harness** turns a user request into a grounded, observable, persisted
agent run. The agentic runtime is **Python** (FastAPI + LangChain + LangGraph);
Next.js is a thin front door. This split exists so the agentic side is not
written in TypeScript, while market data stays in its one real home.

## The split

```
Browser / API client
      │  POST /api/harness/runs            (validated, rate-limited)
      ▼
Next.js front door  (src/lib/harness/, src/app/api/harness/)
      │  • validates RunInput
      │  • resolves agent preset + LLM config from the settings vault
      │  • assigns the run id
      │  HTTP  →  http://127.0.0.1:8765/runs
      ▼
Python agent runtime  (python/harness/)
      │  • owned: agents, tools, model providers, events, run persistence
      │  • LangGraph StateGraph: agent ⇄ tools loop
      │  • native finance tool may use JDdS or another planned retrieval source
      ▼
Next.js data bridge  POST /api/harness/data   (read-only)
      └─ Prisma → JDdS plus configured source adapters
```

Two boundaries, both deliberate:

- **Next.js → Python on `/runs`** — Next.js owns nothing agentic. It resolves
  what the runtime cannot (vault credentials, run id) and forwards.
- **Python → Next.js on `/api/harness/data`** — the Python tools own their
  contracts (name, schema, permission, risk); the bridge provides the existing
  JDdS-backed capabilities. JDdS is a source, not an absolute freshness or
  completeness authority. The retrieval layer may select among configured
  sources; user-enabled MCP tools may also be called directly under the normal
  harness tool policy.

## Python runtime (`python/harness/`)

| Module | Owns |
|---|---|
| `schemas.py` | pydantic wire contracts mirroring the TS types |
| `prompts.py` | system prompts as data (the **Eclipse Agent** prompt lives here) |
| `agents.py` | agent definitions + presets (**authored in Python**) |
| `models.py` | universal provider catalog + LangChain chat-model factory (the low-level construction primitive provider adapters delegate to) |
| `model_runtime/` | **the provider-agnostic model layer** (`Agent → ModelRuntime → ModelRouter → ProviderAdapter → Provider`): `runtime.py` (`ModelRuntime`, the one interface; routing + retry/fallback + accounting), `registry.py` (provider/model/capability/pricing registries seeded from `PROVIDER_CATALOG`), `routing.py` (`ModelRouter` + scoring + task profiles + aliases + `RoutingDecision` trace), `capability.py` (state-based capability detection/probes), `health.py` (`HealthManager`, `CircuitBreaker`, `RateLimitManager`), `usage.py` (`UsageStore`/`CostTracker`/`LatencyTracker`/`BudgetManager`), `structured.py` (parse/validate/repair), `providers/` (isolated adapters delegating construction to `build_chat_model`), `catalog.py` (task profiles, aliases, capability/pricing/performance data), `store.py` (`ModelRequest`/`ModelHealth` tables on `agent-memory.db`), `integration.py` (run→task-profile mapping) |
| `tools.py` | tool registry: validation, permission, risk, timeout, execute |
| `finance_tools.py` | the Phase-1 JDdS-backed tools (kept for compatibility) |
| `retrieval/` | the **Financial Retrieval Layer**: provider-agnostic hybrid retrieval (Phase 2) |
| `retrieval_tools.py` | the Phase-2 tools; they call the retrieval layer, never a provider |
| `research/` | the **Evidence-Grounded Answering** flow (Phase 4): mandate → coverage → claims → calculations → validation → receipt, plus the bounded web escalation (Phase 5) |
| `mcp/` | the **MCP integration** (Phase 5A–5C): typed stdio server/effect contract + threat boundary (`config.py`), namespacing/schema validation (`naming.py`), local stdio client over the MCP SDK (`client.py`), and the `ToolRegistry` adapter + lifecycle (`manager.py`). Only selected read-only tools activate. |
| `skills/` | the **Skill System** (Phase 3): manifests, registry, permissions, execution, learning, evaluation, lifecycle + the shipped skill library. `project_skills.py` adds repository-local skill roots (`.black-eclipse/skills/`, `.agents/skills/`) that stay **inert until an operator trusts the root** (`ProjectSkillTrust`, migration 14); precedence is project > profile > bundled, and a project skill runs through the same permission/execution gate (ADR-H59) |
| `eclipse_memory/` | the **Eclipse-Memory** subsystem (Phase 3-M + memory-maturity): dedicated `eclipse-memory.db` for memory, retrieval, corrections, mistakes + the skill/learning tables. `config.py` resolves a **repo-rooted** path (cwd-independent, `ECLIPSE_MEMORY_DB_PATH` override) and reconciles legacy cwd-based stores non-destructively; `intent.py` deterministically classifies explicit save/forget/correct/skill intent; `decision.py` is the **one shared write gate** (`save`/`suggest`/`history_only`/`ignore`) every memory/suggestion/correction/learning write passes through; `policy.py` holds the single learning-mode keys (`memory_mode`, `skill_learning_mode`) with the legacy booleans *derived*, not stored, plus the local privacy keys (`memory_enabled`, `retention_days`, per-category `capture_policy`/`recall_policy`, `memory_in_diagnostics`, `semantic_indexing_enabled`); `conflicts.py` is the authority-aware deterministic contradiction/replacement detector (subject identity/aliases + normalized tokens + reviewed concept axes + polarity/negation + explicit replacement + temporary-instruction exclusion); `normalize.py` holds the synonym/stemming/typo normalization, the `CONCEPT_AXES` polarity model and temporary-instruction detection; `semantic.py` is the optional local, bounded, fail-safe semantic *candidate* layer (default `char-ngram`, no network/model; can only propose a review, never supersede); `transfer.py` is export/import/backup/health/reset; `skill_quality.py` derives skill usefulness metrics from real executions. Retention sweeps run automatically at runtime startup (`main._lifespan` → `AgenticService.run_startup_maintenance` → `EclipseMemoryService.run_retention_maintenance`) and are observable via `MemoryMaintenance` (migration 11); semantic conflict detection is off by default and reports its active mode via `EclipseMemoryService.semantic_status()` |
| `memory_tools.py` | the agent-facing `memory`, `skill_list`, `skill_view`, `skill_invoke`, `session_search`, `session_open` tools, and the M8 provider tools `memory_search_native` / `memory_search_external` / `memory_provider_status` — a `memory` write is a *proposal* re-authorized against the user turn by `eclipse_memory/decision.py`; the session tools and provider tools are read-only |
| `profile.py` (in `eclipse_memory/`) | the compact prompt profile — a deterministic, bounded projection over active memory injected once at run start (`MemoryProfileSnapshot`, migration 12) |
| `session_index.py` | builds the bounded, redacted `SessionMessage` search index of a completed run (never a second copy of the trace) + `build_summary` (a bounded deterministic session summary; searchable, never auto-injected) |
| `learning_review.py` | the bounded in-process post-run learning-review queue (priority-ordered, per-session coalescing, idle-aware; deterministic + optional inert model proposal; never enables a skill) |
| `self_improvement/` | the native Recursive Self-Improvement Loop (R0–R8): `config.py` (`RSLConfig` from `HARNESS_RSL_MODE`, dark by default; clamped budgets incl. `k`), `models.py` (`HarnessSnapshot`/`Observation` contracts; bounded `toolGuidance` is prompt evidence, never permission; the closed-set `skillManifest`/`memoryPolicy` context bounds; the `guardrails` the runtime ENFORCES at the end of a run; the bounded `subagentPrompt` a delegated child inherits), `observer.py` (deterministic observation → inert candidate), `security.py` (`RSL_DENY_GLOBS`/`assert_landable_target`), `loop.py` (bounded conductor: scan/claim/lease/daily budget, one experiment at a time; `run_recursive` uses the configured `k`/budget for one recursive experiment, verifying up to `HARNESS_RSL_MAX_PARALLEL` independent candidates at once), `proposer.py` (v1a validated code ops + R7 `propose_many` typed `invariant`/`domain` variants from operator/agent
ops), `evaluation/candidate_invoker.py` (v1b inert `skillDraftIds` validated against the skill store and injected into the trial prompt only), `verifier.py` (worktree owning tests with `benchmark=UNMEASURED` for code; measured surface benchmark via the candidate invoker; persists the evaluator decision for metrics), `landing.py` (promotion preview only), `recorder.py` (experiment identity + event), `lessons.py` (R6: the cue+lesson+evidence gate, verdict tripwire, cue+lesson dedupe, adopted-artifact + validation-run record, and deterministic scoped retrieval that requires a COMPLETED validation run; opt-in cue-matched cross-scope transfer via HARNESS_RSL_LESSON_TRANSFER), `selection.py` (R7 seeded anonymous labels, drop-with-cause, neutral base fallback, trivial `k=1`), `composition.py` (R7 verified-only non-conflicting merge; a gain needs a measured score above the best single), `recursion.py` (R7 bounded K-candidate experiment; contrast bundle + verdict guard; one recorded `EvaluationRun`), `gate.py` (R8 deterministic offline end-to-end gate), `post_landing.py` (v3 P0: injected restart → `/health` probe → keep/rollback → re-probe, persisted as `HarnessPostLanding`; `unverified` when no restart seam exists), `runtime_settings.py` (v3 P0: the bounded, persisted `HarnessRuntimeSetting` override overlaid on the env config at boot), `surface_lab.py` (v3 P5: deterministic surface variants derived from the ADOPTED lessons, each measured against the base; the winner is a CANDIDATE, never a landing), `surface_candidate.py` (v3 P5: the ONE surface-experiment identity + row, used by the operator route and the lab). R5 adds the operator evidence/`prepare-landing` routes (`routers/evolution.py`), the typed front door and the learning-journey experiment panel. R6 adds migration 31 on `LearningCandidate` and the bounded `lesson_manifest` prompt block. R8 adds the measured `rsl_*`/`regression_rate` metric block (`evaluation/repair_metrics.py`) and wires the gate into `scripts/test-evals.js`. Operator surfaces: `GET /evolution/lessons`, `POST /evolution/candidates/{id}/lessons`, `POST /evolution/candidates/{id}/recursive` (`routers/evolution.py`), the typed front door (`evolution-client.ts`/`evolution-browser.ts`), Next.js `/api/evolution/lessons*` + candidate `lessons`/`recursive` proxies and `rsl-operator-panel.tsx` in the learning journey. Post-R8 closure: `EvolutionCandidate.subjectType`/`surfaceConfig` additive columns, `POST /evolution/surface-experiments` (one proposed surface candidate), `POST /evolution/model-experiments` (a trial-only provider/model/temperature comparison), `verifier.verify_surface_candidate` (recorded `rsl-baseline` + measured candidate run + one budget-counted attempt/event), `evaluation.started` wakes the loop, and `landing.prepare` refuses surface subjects. Operator controls: `GET /evolution/rsl` (mode/running/busy/daily budget), `POST /evolution/rsl/tick` (one bounded tick), `POST /evolution/candidates/{id}/propose` (stage validated ops without applying), and `rsl-control-panel.tsx` in the learning journey. v3 P0 adds `POST /evolution/rsl/config` (hot, persisted, clamped overrides without a restart; `GET /evolution/rsl` reports `configSource`), the `postLanding` hint on `POST /code/tasks/{runId}/promote` for `rsl-*` landings that need a restart, `POST /code/promotions/{rollbackId}/post-landing` (probe; `alreadyRestarted=true` for the front-door restart protocol) and `GET /code/promotions/post-landing`; docs `docs/self-improvement/rsl-v3-plan.md` + ADR-H278. `npm run rsl:smoke` runs the same conductor/worktree verifier plus a scripted surface benchmark in a disposable repo and temp DB. Hill-climb foundation (ADR-H239): `state.py` (`HarnessState` append-only lineage + one current per scope), `utility.py` (deterministic utility + critical-regression veto + `UNMEASURED` rejection), `replay.py` (frozen replay suite + per-task forgetting gate), `pareto.py` (v2 read-only multi-objective frontier; dominance needs two comparable objectives), `sampler.py` (v4 P0: deterministic `uniform`/`weighted`/`uct` parent sampling over measured states) + `archive.py` (v4 P0: `StateArchive` retains measured losers as inert `stepping_stone` rows and samples an exploration parent; it never accepts, promotes or activates; v4 P2 adds island-aware pools and `migrate_best` - every N measured states, a non-global scope's best state is copied into `global` as a `stepping_stone`) + `operators.py` (v4 P2: the typed operator registry - prompt/tool guidance/tool subset/ceilings/context policy/guardrail/subagent prompt/model identity/code patch/composition - plus snapshot classification, normalized tokens and the Jaccard novelty distance the `propose_many` veto uses) + `cmp.py` (v4 P1: descendant-lift metaproductivity - a depth-discounted estimate of how much a state's measured descendants improved, with an explicit `coldStart`; it ranks exploration parents and never enters acceptance) + `autonomy.py` (v4 P6: risk classes L0/L1/L2, the pure supervised-admission policy, and the persisted kill switch; L0 is never automatic, only L1 bounded surface changes are auto-eligible; `decide` never activates, while `auto_apply_surface` is the one path that may activate a measured L1 state through the existing activation + rollback row) + `hillclimb.py` (pure bounded ascent with tabu/plateau and the opt-in `HARNESS_RSL_OBJECTIVE=pareto` frontier acceptance; callers inject proposal/evaluation and persist through `on_accept`; opt-in archive sampling via `HARNESS_RSL_ARCHIVE`/`_MAX`/`_SAMPLE` proposes from the sampled parent while acceptance still compares against the champion), and the conductor entry `RSLLoop.run_hillclimb` (verify mode + `HARNESS_RSL_STRATEGY=hillclimb`; `HARNESS_RSL_CLIMB_SECONDS` schedules an injected climb runner with pause/resume; saves accepted states as `candidate`, never promotes; each evaluated neighbor counts as an `EvolutionAttempt` for the daily budget). Model proposals (`proposer_model.py`) are strict JSON, parsed by `StructuredOutputManager`, then validated by the SAME `propose_many`/`propose_ops`/frozen-target guards; the flag `HARNESS_RSL_MODEL_PROPOSER` is off by default and the propose tick persists only `proposed` child candidates. `auto` mode runs the bounded propose phase then ONE verify claim per tick; it still never lands a change. Measured code candidates (`HARNESS_RSL_CODE_BENCHMARK=on`) use `evaluation/worktree_invoker.py`: baseline then patched suite in a child process over a disposable worktree, curated non-secret env, temp DB and timeout; `MEASURED` only when the evaluator produced measured trials and the target improved with no critical regression; with `HARNESS_RSL_CANARY=on` the patched tree must also survive a second split (`evaluation/canary.py`) within tolerance; the same per-task safety/adversarial veto used by surface activation also rejects a code candidate whose aggregate improved but a critical task dropped. Operator surfaces: `GET /evolution/states`, `POST /evolution/states/{id}/promote` (state pointer only; never a code write) and `GET /evolution/lessons/{id}/outcomes`; `rsl-control-panel.tsx` shows the current state and recent candidates and is mounted in the learning journey with `rsl-operator-panel.tsx`. Live surface: `POST /evolution/states/{id}/activate-surface` (measured/accepted surface only; refuses model identity and skill drafts), `HarnessActivation` with snapshot/base/previous state, `POST /evolution/surface-activations/{id}/rollback`, and `self_improvement/live_surface.py` applied by `graph.execute_run` only when the re-derived base digest matches; only prompt, tool subset and lowered ceilings are applied. Evaluation/candidate/gate runs set `RunRequest.liveSurface=False` so a live surface cannot contaminate a measurement. `GET /evolution/states/{id}/surface-preview` returns the deterministic diff, base-digest match, accepted decision, available agent scopes and every refusal reason; scoped activation creates a new immutable state for that scope and leaves the source state untouched. A surface experiment carries its target scope (`surfaceConfig.scope`, part of its signature) and a measured surface materializes exactly one inert `candidate` state for the operator preview/activation list. `GET /evolution/candidates/{id}/state` addresses that row by candidate id; the operator panel links `Find derived state` to the same preview/activation card, so the flow is experiment → state → preview → activate. The metrics window (`windowDays`) applies to the RSL counts too; the experiment panel shows a `Today` accepted/rejected/UNMEASURED strip and refreshes it on the verify event. Experiments carry a `scope` (surface scope or `global`), and the panel's scope selector filters the list, today's chips and the scoped Today counts. Continual-learning drift: `self_improvement/drift.py` compares recent terminal run windows with the preceding baseline (Wilson intervals; `unmeasured` on thin data) and `GET /evolution/drift` exposes the read-only signal per agent scope and the 
available scopes; `GET /evolution/drift/history` returns the same signal at
successive offsets in the existing run history (no separate signal log);
`GET /evolution/drift/scopes` returns one signal per configured scope so the
operator can see which agent is drifting. `self_improvement/drift_watch.py`
reads every configured scope on `HARNESS_DRIFT_WATCH_SECONDS` (default 300; 0
disables) and publishes `self_improvement.drift_detected` only on a transition
into drift; it persists the last status per scope (`DriftWatchState`), clears it
when the run history is reset, exposes health on `/evolution/rsl`, and never
creates a candidate. The event service turns a drift transition into a durable
`notification.created` event (`kind=drift`), readable through `GET
/evolution/drift/alerts`; the learning-journey badge raises the shared
notification once per scope per day, and the Command Center has a
`drift-status` widget. A drift candidate's signature includes the newest-run
`anchor`, so a later episode is a new candidate. The conductor
panel shares one scope with the experiment panel, edits
window/baseline/threshold/minSamples, collapses unmeasured overview rows,
shows status/rates/drop/reasons plus recent history, refreshes them on the
verify event, and creates the drift candidate for the selected scope. Candidate
evidence filters its experiment list by that scope; the learning-journey header
shows a drifting-scope badge. Experiment metadata carries `subjectType`, so drift
rows are badged in the list and Today chips. `POST /evolution/drift/candidate` turns a `drift` signal into ONE inert `drift` candidate (no target/ops, deduped by scope/window/threshold) so it enters the existing propose → verify → land flow. `POST /evolution/candidates/{id}/model-propose` runs `RSLLoop.propose_with_model` for that candidate (flag-gated, validated children only); the drift card links it as `Propose with model`. Lesson feedback (`lesson_feedback.py`): `LessonOutcome` rows record the injected cohort AND eligible-but-not-injected control cohort per run; the post-run review writes the terminal outcome; `stats()` reports injected/control Wilson intervals and a lift only at sufficient evidence; `rank_lessons` promotes measured positive lift; `retire_unhelpful` is an inert lifecycle action behind `HARNESS_RSL_LESSON_RETIRE` (off by default). `POST /evolution/lessons/{id}/retire` retires one measured non-positive lesson from the operator panel; the row's `Lift` action reads `/evolution/lessons/{id}/outcomes` and shows injected/control rates and lift inline. The surface preview also carries the accepted run's metrics, up to 50 trial rows (trajectory stripped), the recorded baseline metrics, the target dimension's before/after/delta, and a measured per-task delta table (unmeasured tasks excluded; missing sides `UNMEASURED`); `surface-activation-card.tsx` renders them as expandable `Measured evidence` with the measured delta line, per-task rows, and a prominent critical-regression veto for safety/adversarial tasks. Nothing activates automatically. |
| `prompt_segments.py` | immutable prompt segments + a stable `prompt_cache_key` that covers the CONTENT hashes of the reusable (`STATIC_SEGMENTS`) segments (so a prompt edit changes the key), selective bounded skill-manifest injection, the R6 bounded lesson manifest (`build_lesson_manifest`, max 3 lessons / 800 chars, run-frozen, sourced from adopted lessons with a completed validation run), the run-scoped `runtime_context_block` date/time segment, and `prompt.plan`/`prompt.lesson_injected` observability (`build_prompt_plan`, `build_skill_manifest`, `compare_plans`; `RUN_FROZEN_SEGMENTS` excluded from reuse accounting and the key) |
| `context_budget.py` | the measured per-run history budget: sums the resolved system prompt, advertised tool schemas, per-run blocks and user turn, derives the history allowance from the resolved model's declared window (unknown → conservative ceiling), and keeps `MAX_HISTORY_CHARS` as an absolute cap (`compute_history_budget`) |
| `memory_providers/` | the M8 external-memory-provider layer: `protocol.py` (`MemoryProvider` contract + typed request/response, external `provenance`/`verified=false` defaults), `native.py`, `mcp.py` (read-only-first, L2 write-gated, no automatic writes), `registry.py`, `routing.py` (native-first + external fencing, never promotes), `diagnostics.py` |
| `eclipse_memory/profiles.py` | the M4 named-profile registry (`ProfileRegistry`) + directory projection under `.black-eclipse/profiles/<id>/`; metadata only, READ-ONLY inheritance, no memory content |
| `graph.py` | the LangGraph `StateGraph` execution loop |
| `quant_intent.py` | **quant protocol**: the deterministic, model-free classifier (`classify_quant_intent`) that separates genuine quantitative work (`light` / `standard` / `deep`) from plain quotes, news, filings and concept explanations; `quant_intent_block` renders the per-run protocol into `runtime_context` (frame → data → compute → validate → artifact → caveat, plus the model-synthesis ladder and model contract at `deep`). `quant.plan` is emitted for observability; the block grants nothing and never widens the tool surface. A trade/close/modify turn returns `none`, so `trading_intent.py` keeps execution. `apply_quant_surface` narrows a `standard`/`deep` turn to the data/compute/artifact tool set (trade precedence first, remove-only), and `evaluate_quant_outcome` derives the completeness record (tier/kind, named artifacts, missing deliverables) from the plan, tool executions and final text. The chat contract requires a catalog check before a build, reuse of a fitting artifact, and exactly one final `Artifacts: …` / `Artifacts: none — …` line; there is no separate quant UI surface. The composer's Auto/Quant/Off preference reaches the classifier as `input.context.quantMode`. A code-build task (`alpha.py`, `model.py`, `print(...)`) is explicitly excluded unless a genuine quant term is present, so it cannot lose its file tools to the quant surface |
| `quant_intent_benchmark.py` | the labelled quant-intent benchmark (`quant-intent-v1`, 53 cases): positive asks, anti-cases, explicit framing, trade precedence and mode overrides. Measures tier + kind accuracy and separately counts false-positive non-quant turns and trade-precedence violations; run with `python -m harness.quant_intent_benchmark` |
| `model_catalog.py` | the unified read-only model/research catalog (`model_catalog` tool): aggregates quant strategies + experiments, ML models + datasets, statistical features and factors into one bounded result, with kind/query/limit filters and an honest empty-registry note. Registered from `quant/bootstrap.py`; the other stores are resolved lazily at call time. It never writes or invents an entry |
| `choices_mode.py` | the composer's Auto/Choices/Off preference: `apply_choices_context` appends a per-run `runtime_context` block ONLY when `input.context.choicesMode == "on"` (and `clarify` is on the run's surface), nudging the model to ask ONE question with 2–4 concrete options before an ambiguous turn. Presentation guidance, never a grant; `off`/`auto` add nothing |
| `replan.py` | **Phase 11**: bounded replanning — the trigger (`should_replan`), the bound (`ReplanBudget`), the deterministic directive (`build_replan_instruction`) and the optional model-authored plan (`model_replan`). The `replan` node rides the SAME graph. `HARNESS_REPLAN_ENABLED` still forces it for any agent; ordinary `eclipse-agent` chat now gets the bounded path by default so a failed or stalled tool step gets one revised approach before the run ends. Custom agents keep the opt-in (ADR-H122 as amended) |
| `answer_guard.py` | **bounded answer recovery (2026-11-16)**: a PURE classifier + directive generator. `is_bare_refusal` recognises a short route-specific give-up (not a substantive partial answer, not a policy refusal); `sources_restricted` honours an explicit user source restriction; `recovery_reason` permits at most `MAX_ANSWER_RECOVERIES` (2) extra turns while the run still has steps and an untried permitted route; `build_recovery_instruction` names those routes; `render_fallback_block` puts the concrete ladder next to the user turn. The graph owns the loop (`answer_recoveries`/`recovery_pending`/`tools_used`/`unavailable_tools`); events are `answer.recovery_started` / `answer.recovered` / `answer.recovery_exhausted`, and `run_outcome.answerRecovery` reads them. A refusal after the ladder is exhausted is accepted |
| `final_answer.py` | **final-answer settlement** (extracted from `graph.py`, 2026-11-16): the no-tool-call branch — empty-final budget failure, bounded answer recovery (calls `answer_guard.maybe_recover`), one-shot `recovery_pending`, C4 `plan.finish_answer`, `answer.recovered`, the accepted state, checkpoint and step event. The graph owns the loop/edges; this module owns the branch. `RunSpec.dispatch_allowed` keeps the PRE-disclosure surface for dispatch: hiding a tool is an advertising choice, never a revocation (a policy removal is absent from both sets). |
| `tool_disclosure.py` | **progressive tool disclosure**: when the advertised schemas cross the declared window's threshold (or `ABSOLUTE_SCHEMA_BUDGET_TOKENS` = 20k when the window is unknown), effectful/interactive/core tools stay bound and the read-only tail is catalogued behind `tool_search`/`tool_invoke`. The tail is query-focused (`rank_tools` + `FOCUS_READ_TOOLS`, `DEFAULT_MAX_READ_TOOLS` = 32), so a data turn keeps its ordinary tools. `DisclosurePlan.to_dict` always records `window`/`windowUnknown`/`maxReadTools`; `execute_run` narrows `allowed` BEFORE the RunSpec/build_graph binding. `tool_schema_load.py` adds the missing third step: `load_tool_schema` returns a catalogued tool's exact `parameters` (bounded, ≤5 per call, read-only, allowlist-checked) before `tool_invoke`, and is in `CORE_TOOLS` so it stays bound exactly when narrowing is active |
| `history.py` | history bounding (`_bounded_history`/`_history_messages`) + the inert dropped-context block, extracted from `graph.py` (size ratchet) and re-exported there |
| `checkpoint_io.py` | checkpoint (de)serialization (`serialize_messages`/`restore_messages`/`checkpoint_payload_of`), extracted from `graph.py` (size ratchet) and re-exported there |
| `async_timeout.py` | the hard-deadline primitive (`await_bounded`): races a coroutine against a plain timer and raises `asyncio.TimeoutError` WITHOUT waiting for cancellation to be acknowledged, so a provider/tool that swallows `CancelledError` (observed: a run sat on `model.requested` for 10+ minutes) cannot hold a run hostage. Outer cancellation still cancels the inner task; abandoned work is drained. Used by `model_stream.py` and `ToolRegistry.execute` |
| `model_stream.py` | model invoke/streaming helpers (extracted), with the HARD per-call deadline via `await_bounded`: a hung provider stream fails the run with `PROVIDER_TIMEOUT` at the declared budget, and the abandoned stream is flagged so it stops emitting deltas |
| `run_watchdog.py` | the stall watchdog: every 30s, a run in `created`/`running` whose last recorded activity (event or start) is older than `max(its agent's model budget, the 300s per-call ceiling) + 60s grace` is failed with code `RUN_STALLED`, gets a durable `run.failed` event, its SSE stream is closed and its task is cancelled. A run legitimately waiting on a user decision (last event `approval.requested`) is exempt. A run that already emitted a durable TERMINAL event (`run.completed`/`run.failed`/`run.cancelled`/`run.interrupted` after its current `startedAt`) is NOT stalled: it finished and only the row write lagged, so the sweep reconciles the row to the proven status instead of overwriting a finished run. Started/stopped from `boot.py`. This is the guarantee that a chat stream always reaches a terminal state |
| `events.py` | per-run event emitter + live bus. It also feeds `run_timings.py` and, on the terminal event, writes ONE durable `run.timings` record (setup / TTFT / token cadence) — because `model.delta` is transient, the trace otherwise cannot explain a slow run |
| `run_timings.py` | the per-run latency recorder behind `run.timings`: a pure in-memory observer over the events the emitter already emits (`setupMs`, `ttftMs`, `firstDeltaMs`, delta-gap p50/p95/max). Absent marks are omitted, never guessed; recording is total and fail-open |
| `event_system/` | **the canonical, durable Event System** (ADR-H153): `envelope.py` (`EclipseEvent` + correlation/causation/trace context vars + secret redaction), `registry.py` (`EventRegistry`/`EventTypeSpec`), `filters.py`, `store.py` (`HarnessEventLog`/`HarnessEventConsumer`/`HarnessEventDeadLetter`/`HarnessEventSubscription` on `agent-memory.db`), `metrics.py`, `middleware.py`, `security.py`, `bus.py` (`EventBus`/`Subscription`: workers, backpressure, retry, DLQ, idempotency, request/reply, legacy bridge), `rules.py` (event→action via the Automation Engine), `replay.py`, `service.py` (`EventSystem` facade + memory-candidate/self-improvement/market-sampler consumers), `config.py`, `bootstrap.py`. It BRIDGES `events.py` (one transport, one model) rather than replacing it. Detail: `docs/harness/event-system.md` |
| `persistence.py` | `agent-memory.db` (runs, events, tool execs, retrieval/receipt records, learning-recurrence ledger) + token usage aggregates |
| `run_patch.py` | the ONE run-patch serializer (`RUN_PATCH_FIELDS`, `RUN_JSON_FIELDS`, `run_patch_sql`): allowed columns plus JSON encoding, shared by `AgentMemoryStore.update_run` and the lease-fenced writer. A divergence here silently dropped every terminal write once; never give either writer its own field list |
| `service.py` | process-wide store/registry/bus; the JDdS bridge transport itself lives in `bridge.py` |
| `bridge.py` | the thin HTTP data-bridge transport (`HttpDataBridge`, re-exported by `service.py` as `_HttpDataBridge`): the ONE read-only path the finance/retrieval/market tools use to reach JDdS through the TS accessor. TWO timeout classes: 8s for structured/vault reads (below the smallest market-tool budget so the tool reports UNAVAILABLE, never `TOOL_TIMEOUT`) and 20s for capabilities that fetch the public web (under the 30s research budget). Exactly ONE retry, only on `ConnectError` (the instant dev-server-restart refusal); a timeout is never retried |
| `run_recovery.py` | boot reconciliation for runs orphaned by a process restart: `fail_orphaned_runs` marks every `created`/`running` run row failed with code `RUN_ORPHANED` plus a durable `run.failed` event, so a client streaming a run whose process died sees a terminal state instead of waiting forever. Idempotent. `reconcile_orphaned_runs` wraps it with a **port-ownership guard**: FastAPI's lifespan runs *before* uvicorn binds, so a duplicate process that is about to fail its port bind would otherwise reconcile (and orphan) the runs a live runtime is executing — reconciliation proceeds only when the bind port is free. Called from `AgenticService.run_startup_maintenance` |
| `deps.py` | the front door's shared state + authorization seam: the ONE process-wide `AgenticService`, the `require_operator`/`require_mcp_operator` transport/token dependencies, and the identity/tenancy boundary helpers (`identity_boundary`, `deployment_exposure`) |
| `computer/` | the **Computer Use Engine**: observe and operate a real desktop (native apps, OS chrome, dialogs) with accessibility-first grounding, SOM snapshots + stale-element protection, background-first input, verification, recovery, a permission/risk policy engine, sensitive-region redaction and a **Finance Action Firewall**. Platform-independent `ComputerDriver` (Windows via `ctypes` Win32; Linux/X11 via `ctypes` X11/XTest; macOS via Core Graphics/AX; honest `NullDriver` where no backend exists). Registers the `computer_*` toolset on the ONE registry. Detail: `docs/harness/computer-use.md` |
| `run_lineage.py` | **Phase 1**: run lineage + durable checkpoints + idempotency. Owns the two additive `HarnessRun` columns (`parentRunId`/`workflowId`) and the `RunCheckpoint`/`ToolExecutionIdempotency` tables, bound to the store's per-thread connection (so ratcheted `persistence.py`
did not grow). `save_checkpoint`/`latest_checkpoint`/`mark_tool_executed`/`was_tool_executed`/`set_run_parent`/`list_child_runs` |
| `graph_context.py` | **extraction**: the per-run context blocks (recalled memory, compact profile, skill manifest) and the memory/learning outcome helpers, pure-extracted from `graph.py` to keep it under its size ratchet; re-exported by `graph.py` so callers are unchanged |
| `workflows/` | **Phase 5**: the composable workflow graph. `model.py` (Workflow/WorkflowStep contracts + closed step-kind set + bounds), `validate.py` (cycle / unbounded fan-out / out-of-scope tool / no terminal path — each with a NAMED reason), `store.py` (validated persistence), `compile.py` (compiles onto the EXISTING `execute_run`; a step's tool scope = agent allowlist ∩ step tools, enforced at the same `ToolRegistry.execute(allowed=...)` seam; approval steps reuse the single-use `ApprovalStore`; parallel branches share the run cancel event/cache). Flag-gated (`HARNESS_WORKFLOWS_ENABLED`, default OFF) |
| `eval_gates/` | **Phase 8**: the offline route_health / workflow / resume gates invoked by `scripts/test-evals.js` (the capability-matrix gate lives in TS, `scripts/eval-capability-matrix.ts`) |
| `routers/` | self-contained route groups registered by `main.py` via `app.include_router(...)`, so the front door stays navigable. `mcp.py` owns the MCP catalog/approvals/presets/diagnostics routes; `memory.py` owns all `/eclipse-memory/*` + `/memory-providers/*` routes; `sessions.py` owns sessions/profiles/session-history-search/learning-review; `skills.py` owns the skill lifecycle, project trust, bundles, authoring and learning routes. Shared state comes from `deps.py` (never by importing `main`) |
| `main.py` | FastAPI app (`/health`, `/models`, `/agents`, `/runs`, …). Shared runtime state and operator authorization live in `deps.py`; self-contained route groups are extracted into `routers/` and registered via `app.include_router(...)`. The public surface stays `harness.main:app`. |

### Agents and prompts are Python-authoritative

The default agent is **Eclipse Agent** (`id: eclipse-agent`), the platform's
general-purpose financial intelligence identity. Its system prompt lives in
`python/harness/prompts.py` as `ECLIPSE_AGENT_SYSTEM_PROMPT` and is composed
into an `AgentConfig` in `agents.py`. The prompt sets finance-specific standards
for source quality, point-in-time evidence, quantitative comparability, risk,
and capability honesty; it also carries the **finance-harness contract**: a
`# Market data semantics` block (session state, price/period basis, corporate
actions, point-in-time vs restated, FX, staleness), an `# Inputs and authority`
precedence table with the attached-document rule, an `# Answer contract` (shape,
citation format, numeric conventions, a worked example), and a
`## Runtime awareness` block (bounded steps/timeout, stop rule, truncation,
no redundant retrieval). The registered tool allowlist remains the actual
execution boundary; the run's real date/time is supplied out-of-band by the
`runtime_context` prompt segment, never inside the stable prompt head.

The front door does **not** carry a prompt copy. It sends only `agentId`, and
`resolve_agent()` in `graph.py` resolves the full `AgentConfig` (prompt + tool
allowlist + limits) from the runtime's own presets. `GET /api/harness/agents`
proxies the runtime's `/agents`. This is what keeps one source of truth: updating
the prompt is a one-file change in `prompts.py`. Because a prompt can silently
drift from the allowlist it describes, guard tests (`python/tests/
test_memory_tools.py`) assert over BOTH presets that every tool a prompt names is
in its preset's allowlist and that a prompt never denies a capability its
allowlist contains (ADR-H106).

### User personas are an append-only overlay (ADR-058)

Settings → Persona lets the operator keep named, versioned personas. A persona
is **not** a prompt replacement: it is a bounded (2,000-character) block appended
after the resolved `eclipse-agent` prompt, wrapped in a fixed "user persona"
frame that states it is guidance, not policy, and cannot add tools, grant
permissions, select data sources or override the safety/evidence/financial
rules. The tool allowlist is resolved before the persona is read, so the overlay
cannot widen it. It applies only to runs using the default chat agent
(`eclipse-agent` — ordinary chat, bots, and any graph node that selects it); a
Graph Studio custom agent preset keeps its own prompt.

Storage is `python/harness/personas.py` on the ONE `agent-memory.db` connection:
a current row plus append-only versions. Editing writes `draftInstructions`;
`activate()` promotes the draft into a new immutable version and deactivates the
previous persona (exactly one active globally); `reset` leaves no overlay;
`restore` loads an old version into the draft; `archive` keeps history. Every
write is guarded by a `revision` token (a stale save from another window is a
409, never a silent overwrite), and history is bounded to the last 50 versions
(the active version is always within the window). Text is sanitized (role
markers and code fences neutralized) and capped at write time.
The run records `persona id@version` alongside `surface`, so a later edit never
rewrites what an earlier run saw. `GET /personas/preview` returns the effective
prompt (base + overlay) for the operator UI.

### The Financial Retrieval Layer (Phase 2)

The agent requests an **objective + data type**, not a mandatory provider:

```
tool → Financial Retrieval Layer → planner (task-aware source selection)
     → cache (freshness policy) → parallel/sequential sources
     → normalization → validation → conflict resolution → evidence → tool result
```

`retrieval/` owns native financial source planning; it can select among the
configured providers for the requested data type. User-enabled MCP tools are
also available as general tools and are not required to implement this contract:

| Module | Owns |
|---|---|
| `contract.py` | the provider-agnostic request/result/evidence models |
| `policies.py` | per-data-type freshness + cache TTL + tier trust ranks |
| `sources.py` | the `RetrievalSource` contract every provider implements |
| `structured.py` | JDdS (vault) + live structured providers (yfinance etc.) + the direct SEC filings source |
| `web.py` | first-class web research (search → rank → retrieve → evidence) |
| `normalize.py` | provider payloads → one `NormalizedFinancialObject` (preserves filing identity) |
| `retry.py` | bounded retry with backoff for transient — never permanent — source failures |
| `cache.py` | TTL cache + in-flight deduplication |
| `parallel.py` | concurrency, per-task timeout, error isolation, cancellation |
| `validate.py` | valid / warning / invalid / unknown (negative earnings stay valid) |
| `conflicts.py` | preserve discrepancies; resolve only with a stated reason |
| `aggregate.py` | `EvidenceSet` keeping evidence kinds distinct |
| `authority.py` | the ONE ordering table (`AUTHORITY`) + the arbitration decision (`decide_source`); ADR-H115/H116 |
| `planner.py` | deterministic, explainable source selection (adapts `AUTHORITY` into a plan) |
| `routing.py` | route eligibility + outcome categories (ineligible / not_implemented / unconfigured / provider_failure / no_instrument / no_data / …), so a skip is never reported as an outage |
| `layer.py` | the orchestrator the tools call |

**Source selection is task-aware, not "JDdS first" or "web first".** The ordering
is data in `authority.py`; e.g. a current quote prefers a live structured reading
(the vault is a candidate, not the first-and-only — ADR-H115), a 5-year Sharpe
prefers structured history, "why did NVDA move" combines market data and web
research, and a historical series never uses web at all. The price/series/macro
types are **arbitrated**: every eligible candidate runs and a disagreement is
preserved as `CONFLICTING` rather than hidden by the first success. The selected
strategy is recorded on every result (`strategy`) and emitted as an event.

**Precise data is never web-sourced.** `historical_prices`, `macro_series` and
`derived_metric` remove WEB from the plan; when no structured dataset exists the
layer returns `UNAVAILABLE` rather than a web guess.

Tools: `market.get_quote`, `market.get_ohlcv`,
`fundamentals.get_company_profile`, `fundamentals.get_financials`,
`research.search`, `research.retrieve_source`, `research.search_news`,
`macro.get_series` (registered as `*_*` names — the registry allows
`[A-Za-z0-9_]` only).

### Evidence-grounded answering (Phase 4)

Phase 2 retrieval produces provenance, identity, freshness and conflicts. Phase 4
turns a user objective into an explicit set of facts an answer must establish and
gates the final answer against actual evidence. It is a **new package**
`python/harness/research/`, not a second retrieval system: every fact leg is
fetched through the *existing* `FinancialRetrievalLayer`.

```
objective → mandate → per-leg retrieval → coverage → structured claims
          → deterministic validation → research receipt
```

| Module | Owns |
|---|---|
| `mandate.py` | `ResearchMandate` + `FactLeg`; a narrow deterministic classifier (initially `financial_comparison`); optional model-assisted extraction validated against the schema (the model may propose, never author canonical identity) |
| `coverage.py` | per-leg `covered` / `conflicting` / `unavailable` / `unknown` / `not_applicable`; successful retrieval is *not* coverage |
| `claims.py` | `Claim`/`Synthesis`/`ValidationOutcome` + the deterministic validator and prose rendering |
| `calculations.py` | pure-Python period change, CAGR, annualized volatility, max drawdown, Sharpe — with formula/version/input provenance and hard input guards |
| `citations.py` | `Citation`/`ResponseMetadata` assembled only from retrieved evidence (stable interfaces for future workflow nodes) |
| `failures.py` | failure classification (timeout / provider / no-coverage / incomplete / stale / ambiguous / invalid / conflict) |
| `periods.py` | canonical annual/quarter/date/range parsing + matching (no substring-of-digits) |
| `comparability.py` | deterministic identity/comparability gate: blocks a change across a demerger/ticker reuse, unequal durations (9M vs 12M), consolidation basis, metric definition, currency, unit, scale or an unstated restatement, with the exact reason |
| `ids.py` | stable SHA-256 content digests for evidence/calculation identifiers |
| `synthesis.py` | the bounded, tool-less `ModelSynthesizer` (mandate + evidence packet + coverage only) |
| `supersession.py` | source-grounded, cycle-checked restatement chains |
| `eval/` | the versioned, human-labeled semantic-support + escalation sets + runners, and the NVDA labeled regression (`nvda.py`, `dataset_nvda_v1.json`) |
| `budget.py` | the bounded search/fetch/page/time/token budget + exhaustion record |
| `web_facts.py` | span-anchored deterministic extraction/validation of web facts |
| `escalation.py` | bounded structured → primary-web → secondary-web escalation |
| `run.py` | live-chat integration: routes a supported comparison into the gate before the agent graph; renders the keep-going answer |
| `receipt.py` | the bounded `ResearchReceipt` (carries per-leg diagnostics + stop reason + filing identity) |
| `engine.py` | the orchestration over an injected `retrieve` callable |
| `smoke.py` | operator-triggered live network smoke checks (never in the default suite) |

### Global market research (Phase 7)

Eclipse is not US-centric. The market-data path is provider-neutral: a request is
routed by the instrument's verified market/exchange/currency/asset class and data
type, not by a US default.

- **Truthful capability matrix.** `src/lib/harness/capability.ts` composes the
  declared registry with observed universe sizes and recorded health into a
  per-market matrix (recognition / universe / quote / history / fundamentals /
  official filings / web+documents / observed health / live-checked). The
  `get_coverage` and `get_capability_matrix` tools report it, and their text
  explicitly instructs the agent to try other eligible routes rather than stop
  when a market or ticker is missing. Statements are never implied by quotes. The
  matrix CANNOT assert an impossible fact: `reconcileAxis` represents a
  non-`unknown` observation against an unimplemented level as `matrixInconsistent`
  with a reason. The market set is 34, each with its real ISO currency (CH=CHF,
  KW=KWD, QA=QAR); provider eligibility is DECLARED (`provider-eligibility.ts`)
  rather than inferred. Decision: ADR-H95.
- **Global identity.** The JDdS master stores bare local codes (`2330`, `005930`);
  users type Yahoo-style suffixes (`2330.TW`, `005930.KS`). `splitExchangeSuffix`
  in `markets.ts` maps a curated suffix table to an implied market, and
  `resolveVaultInstrument` queries both forms. An unknown suffix is never guessed.
- **Provider eligibility.** A provider declares eligibility and is SKIPPED when
  ineligible — `SecFilingsSource.eligible` skips SEC for non-US issuers instead
  of attempting it and reporting an outage. The escalation query is market-aware
  (no global "10-K"). A missing credential disables only its route.
- **Documents and language.** The shared fetch transport sends a declared
  email-contact `User-Agent` (SEC fair-access requires it; verified), returns
  JSON/XML/XBRL/plain text as raw text, extracts text-layer PDFs, and reports a
  scanned/image PDF as `unsupported_document` and a JavaScript-only shell as
  `javascript_only` — distinct categories the caller can escalate on. Original
  language is preserved (`WebPage.language`); no silent translation.
- **Health keying.** `sourceHealth` keys each route by
  `(source × market × assetClass × capability × probeKind)`; a declared target with
  zero attempts is `unknown`. The reader is a SUMMARY PARTITION: the compact
  recorded head is the default and the ~1,200-row declared universe is withheld
  until `includeDeclared`/`recordedOnly` is set, so the recorded ledger stays
  readable inside the 24k tool-result bound (ADR-H94). `SourceHealth` carries
  `lastSuccessAt`/`lastFailureAt` per route. A bounded cross-market probe records
  one attempt per (market × implemented capability) into the SAME ledger under the
  SAME canonical key; the manual one-shot works immediately, the recurring
  schedule is OFF by default (`HARNESS_ROUTE_PROBE_ENABLED`). Fixture probes are
  always labelled and never presented as live health. The ledger surface lives in
  `src/lib/harness/health-ledger.ts`.

The Python research loop, receipt, and per-leg statuses are unchanged; this
phase changes WHICH routes are selected and how their outcomes are reported, not
the evidence contracts.

### Reliability & global-research hardening (Phase 8)

Phase 8 closes the reliability gaps the international runs exposed, without
changing the evidence contracts:

- **Capability tools are reachable.** `get_capability_matrix` and
  `get_research_health` are read-only and in the Eclipse agent allowlist. Missing
  capability data returns a structured error, never a silent fallback to
  `get_coverage`.
- **Timeouts are independent.** A tool's own `timeout_ms` is authoritative; the
  agent's `toolTimeoutMs` is only a ceiling (`tools.effective_timeout_ms`). News
  enrichment has its own bounded budget, so a slow news provider cannot cancel
  statement retrieval or partial-answer rendering. Transient-only retry is
  unchanged (`retry.py`).
- **Route outcomes are distinct.** `retrieval/routing.py` reports ineligible /
  not_implemented / unconfigured / provider_failure / no_instrument / no_data /
  no_matching_period / stale_data / invalid_document, so "we did not try it
  because it does not apply" is never reported as an outage. `capability.ts`
  names each market's real regulator filing system.
- **Capability-specific health.** `sourceHealth` keys
  `(source × market × assetClass × capability × probeKind)` — a healthy quote
  never makes statements/filings look healthy. Zero attempts stays `unknown`;
  fixture probes are labelled and excluded from observed live health.
- **Documents.** The fetch transport distinguishes `too_large`, `empty_response`,
  `javascript_only`, `unsupported_document` and `parse_failure`, preserves
  title/document-id/publication/retrieval metadata, streams to a bounded size,
  and refuses a value-less XBRL taxonomy and a search snippet as evidence.
- **Comparability gate.** `research/comparability.py` deterministically blocks an
  incompatible change (entity/ticker reuse from a demerger, unequal period
  durations, consolidation basis, metric definition, currency, unit, scale,
  unstated restatement) with the exact reason, recorded in the receipt
  (`comparability`) and metadata (`blockedComparisons`). Each verified leg is
  still reported; no misleading percentage is computed.

### Retrieval escalation (Phase 5) and resilient multi-source research (Phase 6)

Structured sources stay the preferred path, but they are no longer the only one.
`retrieval/authority.py` is the explicit, testable source policy (the planner
adapts it); for `financial_statements` it is:

```
SEC EDGAR (direct filing/XBRL, tier primary, US issuers)
  → JDDS (the structured vault)
  → primary-source web  (SEC/company IR/regulators)
  → reputable secondary web (bounded)
```

**Vault freshness (statements & ratios).** The statements cache is a cache, not
a source of truth: a snapshot older than 7 days is refreshed through the
provider chain before it is served, and when the refresh returns nothing the
persisted payload is still answered with `stale: true` + `cacheAgeDays` — never
with a disguised fresh timestamp (a FY2018/19 payload was being served
indefinitely). The `fundamentals` capability reports a `valuation` block (P/E,
forward P/E) whose `provenance` is `LIVE` when fetched this call, else `JDDS`
with the saved price's `asOf`; `get_fundamentals(refresh=true)` is the
live-ratio route.

The SEC route (`SecFilingsSource` → the `sec_filings` bridge capability → the
existing `sec-edgar` adapter) is independent of the vault and of the search
transport, and routes through the **shared identity gate** so a bare ticker
cannot answer for the wrong issuer. A second web-search provider is
operator-configurable through the settings vault (`SEARCH_API_CONFIG` +
`SEARCH_API_KEY`); the keyless DuckDuckGo HTML provider is always present, and
each provider is a small isolated adapter (`src/lib/web-search.ts`) that can be
disabled or replaced independently. There is still exactly one page-fetch
transport and one market-data bridge.

When structured retrieval is empty, stale, missing a requested period, or lacks
required metadata, `engine.py` escalates each still-unsatisfied leg through the
hierarchy. Escalation is targeted (only the unsatisfied legs), bounded by one
`ResearchBudget`, and every numeric fact must pass `web_facts.validate_fact`: its
page must have been fetched in this run, the cited `sourceSpan` must be a verbatim
quote from that page, the value must appear in the span, the span must not state a
different fiscal period, and metric/period/currency/basis must match the mandate
leg. Page text is untrusted — an instruction in a page is never a fact. A missing
consolidation basis yields a `qualified` leg (the value is present, the basis is
disclosed as unconfirmed), never a hard failure or a silent inference.

**Failure diagnostics.** Every search/fetch attempt records a
`RetrievalDiagnostic` (`src/lib/retrieval-diagnostics.ts`): provider/transport,
normalized status, category (`zero_results` vs `transport_error` / `timeout` /
`blocked` / `http_error` / `content_type_rejected` / `parse_failure` /
`not_configured` / `invalid_request`), HTTP status, latency, timeout state, result
count, bytes, parse outcome and retryability. A JSON source document is accepted
rather than rejected as "not HTML". `retrieval/retry.py` retries only *transient*
outcomes with bounded deterministic backoff; a permanent failure (blocked,
unsupported content type) is never retried. The engine preserves per-leg
retrieval + coverage status + diagnostics and a `stopReason`
(`all_required_legs_resolved` / `budget_exhausted` / `provider_routes_unavailable`
/ `unresolved_facts_remain`) on the receipt. A `research_health` capability
(`get_research_health` tool) probes each route without a research run.

**Keep-going answer.** `run.py` renders every validated fact it has, the source
routes that failed and why, and the exact unresolved facts — a partial run never
collapses to a bare "I can't find it" when other legs were verified. Verified
filing facts carry their document identity (form + filed date + period end) into
the receipt.

**Live chat.** `execute_run` classifies the objective before building the agent
graph. An unambiguous single-entity, two-period comparison is routed into the
gated flow using the run's own `ModelSettings` (no second adapter); everything
else falls through to the Phase 1–3 path unchanged. A gated run persists its
research metadata additively on the run and answers with validated/qualified
claims plus explicit gaps — it is never silently downgraded to an ungated answer.
The chat surface renders citations/coverage via `ResearchEvidencePanel` and can
read the durable receipt at `GET /api/harness/runs/[id]/receipt`.

Persistence is additive: `persistence.py` gains an idempotent `ResearchReceipt`
table in `agent-memory.db` (never `eclipse-memory.db`, never JDdS). Endpoints
`POST /research/mandate`, `POST /research/run`, `GET /research/receipts[/{id}]`
live in `main.py`; the TS front door proxies them, as with every other runtime
route. General/unsupported objectives return `supported=false` and continue on
the existing chat path. Decisions: ADR-H15 … ADR-H19 in
[`decisions.md`](./decisions.md); progress in
[`phase-4-progress.md`](./phase-4-progress.md).

### The Skill System (Phase 3)

A **skill** is a versioned, machine-readable procedure the agent executes
through the *existing* `execute_run` graph — there is no second runtime, tool
registry or agent loop. `python/harness/skills/` owns it:

| Module | Owns |
|---|---|
| `manifest.py` | the pydantic skill manifest + status/risk/category/permission enums, semver |
| `permissions.py` | the **default-deny** permission policy (enforced in code) |
| `store.py` | compatibility alias: `SkillStore = EclipseMemoryStore` (skill lifecycle lives in `eclipse-memory.db`) |
| `loader.py` | loading `skill.json` + `SKILL.md` (+ optional `tests/cases.json`) |
| `registry.py` | the single registry: discovery, validation, versions, dependencies |
| `runtime.py` | `SkillExecutionRequest`/`Result` + execution via `execute_run`; enforces manifest limits incl. `maxToolCalls` |
| `schema_validation.py` | full JSON Schema validation of skill input/output + schema self-validation |
| `learning.py` | run-trace analysis → inert learning candidates; ordinary-chat eligibility + explicit "turn this into a skill" candidates |
| `draft.py` | candidate → unvalidated draft with a real generated procedure + evaluation cases (never auto-enabled) |
| `evaluation.py` | deterministic evaluation + promotion gates |
| `execution_eval.py` | the W5.1 **behavioural** tier: runs the skill through `execute_skill` in `ExecutionMode="evaluation"`, grades the trajectory with `evaluation/graders`, and returns run ids/scores; a static-only pass never satisfies the promotion/enable gate |
| `curator.py` | the W5.2 curator: deterministic duplicate/near-duplicate/eval-debt/tag-drift scan, protected-skill rules, inert merge drafts, cadence + lock; modes `off|dry_run|propose|apply` behind `HARNESS_SKILL_CURATOR` |
| `lifecycle.py` | submit-review / approve / reject / rollback (requires execution evidence for learned skills when the W5 flag is on) |
| `patches.py` | M2 structured skill patches: JSON-pointer `replace`/`add`/`remove`, allowed-field set, forbidden identity/permission/tool/risk fields, stale-base conflict + never-canary classification |
| `versioning.py` | M2 versioned activation: propose → evaluate → canary → activate, atomic rollback (preserving both versions), automatic rollback triggers, version history |
| `library/` | the shipped skills (research, quality, finance, system) |

Execution composes an `AgentConfig` from the skill: its `SKILL.md` becomes the
system prompt (behind an authoritative runtime preamble), and its `requiredTools`
become the run's tool **allowlist** — the model can never call a tool the skill
did not declare. `SKILL.md` prose is data; the permission check, allowlist and
dependency resolution are enforced in Python code regardless of what the prose
says. Retrieved web/document content is untrusted and can never widen an actor's
grants.

Discovery is deterministic and metadata-only (`discover()`), so full instructions
load **only** for a selected skill — progressive disclosure by construction.
Skill `skill.*` events ride the same `RunEmitter`/bus/persistence as `tool.*`.

**W5 learning loop.** `skills/execution_eval.py` is the behavioural tier;
`skills/curator.py` is consolidation. Memory ranking has one hybrid branch:
`eclipse_memory/bm25.py` (corpus-level IDF) is added as a bounded tie-break in
`retrieval.retrieve_memories` behind `HARNESS_MEMORY_HYBRID`; the lexical
relevance gate, scope/status/privacy filters and contradiction guard remain
authoritative, and flag-off is the original ordering. See
[`roadmap/w5-learning-loop-plan.md`](./roadmap/w5-learning-loop-plan.md).

Details: [`phase-3-architecture.md`](./phase-3-architecture.md).

### Web research is one shared system

`src/lib/web-research.ts` is the single **page-fetch** implementation and the
diagnostic facade; `src/lib/web-search.ts` holds the ordered, independent
**search providers** (keyless DuckDuckGo HTML plus an operator-configured JSON
API). They back the `web_search`/`web_fetch` bridge capabilities the retrieval
layer calls, and the legacy `search_web` agent tool delegates to them. There is
no second page-fetch system and no second data plane; each search provider is a
small isolated adapter that can be disabled independently.

### Unified search is one shared retrieval layer

`python/harness/search/` is the ONE semantic/keyword/hybrid retrieval layer the
agent tools and the operator UI share. A process-wide `SearchService`
(`search/bootstrap.py`) owns a dedicated SQLite vector + FTS5 index
(`.black-eclipse/search/search.db`), a provider-agnostic `EmbeddingService`
(OpenAI/OpenAI-compatible, Google, Cohere, Voyage) with batching, caching and
retry, and reranking/context/citation stages. It reads its sources from the ONE
`agent-memory.db` connection and bridges results into the research
`EvidenceStore`; it never adds a second provider, index or agent loop. The
embedding provider is resolved per run from the run's `ModelSettings`;
with none configured the layer is honestly keyword-only. This is deliberately a
dedicated SQLite store rather than PostgreSQL + pgvector, to keep zero-config
(ADR-H151). See `docs/harness/search-retrieval.md`.

### Universal model coverage

`models.py` covers "all models" through two mechanisms rather than a branch per
vendor:

1. **Native LangChain integrations** for first-party APIs — Anthropic, Google
   Gemini, Mistral, Groq.
2. **The OpenAI-compatible protocol** for everything else — OpenAI, Ollama, LM
   Studio, OpenRouter, DeepSeek, xAI, Perplexity, Together, Fireworks, and any
   custom base URL + model id.

`PROVIDER_CATALOG` is data: adding a provider is one catalog entry (plus a
provider package in `requirements.txt`). Credentials arrive per run from the TS
vault and are never persisted by the runtime.

### The Model Runtime (provider-agnostic layer)

`build_chat_model` is the low-level construction primitive. Everything above it
goes through the **ModelRuntime**, which is the only model interface the rest of
Eclipse uses:

```
Agent → ModelRuntime → ModelRouter → ProviderAdapter → Provider
```

- **Selection.** The router scores every registered model against a task profile
  (capability, quality, reliability, latency, availability, context, cost) with
  configurable weights, and returns an explainable `RoutingDecision` (candidates,
  per-dimension breakdown, reason, ordered fallbacks). A concrete
  `ModelSettings.provider/model` is the explicit override; logical aliases
  (`eclipse-fast|smart|reasoning|coder|vision|cheap|local`) resolve dynamically.
  The front door stores a **bare** model id, so `resolve_for_run` qualifies it
  with the run's provider before routing: an operator-typed id the catalog has
  never seen is still recognised as the explicit choice and is **never silently
  substituted** by the router's own pick.
- **Capabilities** are states (`supported`/`unsupported`/`unknown`/`degraded`),
  never bare booleans; a required `unknown` does not satisfy a requirement.
- **Reasoning shaping** is applied only when the model exposes a provider-native
  control (Anthropic `thinking`, Gemini `thinking_budget`, a native OpenAI
  reasoning model's `reasoning_effort`). An OpenAI-compatible endpoint
  (DeepSeek, Groq, a router, a local server) is never sent `reasoning_effort` —
  that knob is a hard 400 there, not a hint.
- **Failure handling.** Errors are classified into one normalized vocabulary;
  retry is error-aware (exponential backoff + jitter) and fallback respects
  required capabilities. Both are flag-gated (`HARNESS_MODEL_FALLBACK` default
  OFF, `HARNESS_MODEL_RETRIES` default 1) and only build a candidate when the
  runtime can — same provider or a keyless local one — so a single-credential
  run never silently routes to an unconfigured paid provider.
- **Health** is tracked with rolling windows, a per-key circuit breaker and a
  rate-limit manager; **usage/cost/latency** are normalized and persisted to the
  runtime's OWN `ModelRequest`/`ModelHealth` tables on `agent-memory.db` (never
  JDdS). Pricing that is unknown stays `NULL`, never 0.
- **Streaming, tool schemas, structured output and reasoning** are normalized so
  the UI and the graph see one shape whatever the provider.
- Read-only inspection: `/model-runtime/{providers,models,routing,route,health,usage}`
  (operator-gated; `/route` is the routing debug view).
- **Runtime discovery.** `POST /model-runtime/providers/{id}/discover` probes a
  live provider's `/models` endpoint and registers what it serves, so a local
  server (Ollama, LM Studio) or a router shows its installed models rather than a
  static sample. The connection details are supplied per call and never stored; a
  failed probe returns `ok:false` + reason, never a silent empty success.
  `ProviderSpec.dynamicDiscovery` (surfaced on the catalog) marks the providers
  for which the UI offers this probe.
- **The probe reports `served` separately from the merged registry.** The
  registered `models` list is the provider's live list MERGED with the static
  catalog's declared sample, so it cannot tell an installed model from a
  placeholder that is not installed at all (Ollama declares `llama3.1`, which a
  box running only cloud models does not serve). `ModelRuntime.discover_served`
  returns the provider's OWN ids before the merge, and the discover route returns
  them as `served`. The front door stores `served` in the
  `discovered_models_v1` snapshot (replaced per probe, not merged — it must
  reflect the server *now*), `/api/harness/chat/models` reports it per provider,
  and `useChatModel` defaults to a served model so a run never 404s on an
  uninstalled placeholder. An explicit user pick is never overridden.

The model layer provides intelligence only — it is never a source of financial
truth. Evidence, provenance, citations and point-in-time validity remain owned by
the Evidence/Research engine.

### The execution graph

```
START → agent
agent  — model invoked with tools bound
         • no tool calls → END (final answer)
         • tool calls    → tools
tools  — each call validated → permission-checked → timed out → executed
         → agent
agent → … until maxSteps, then MAX_STEPS_EXCEEDED
```

Safety: max steps, per-tool timeout, run timeout, **step-scoped** duplicate-call
suppression (ADR-H90), a **bounded** native tool-result text (ADR-H91),
read-only enforcement, cooperative cancellation, and exactly one terminal status.
Events (`run.*`, `model.*`, `tool.*`) are emitted and persisted by the Python
runtime.

**Model-call integrity (Round 5 / ADR-H348).** `agent_node` does not call
`model_stream` directly. `step.invoke_model_step` is the ONE path: a
`retry_policy.RetryPolicy` (3 attempts by default, jittered 1→10 s, a 90 s retry
budget, provider `Retry-After` honored), a per-call ceiling
(`MODEL_CALL_TIMEOUT_CEILING_S = 300`), and a `RunDeadline` created when the
graph is built. The deadline is checked before every model call and every tool
turn; a spent deadline is `RUN_TIMEOUT`, not a per-call `PROVIDER_TIMEOUT`.

The run's wall clock and the per-call budget are DIFFERENT numbers. The preset's
`runTimeoutMs` bounds one model call; it is **not** the whole-run deadline.
`graph.chat_run_budget()` gives the default chat agent a 600 s wall-clock floor
(`ECLIPSE_CHAT_RUN_TIMEOUT_S` overrides, 60 s..hard cap), because a 24-step
research turn cannot fit inside one call's 120 s — treating it as the deadline
made every real turn hit `RUN_TIMEOUT`. An explicit per-run agent config keeps
its own declared value. The stall watchdog's silence budget likewise floors on
the 300 s per-call ceiling, so it never kills a healthy run mid-call.
Retries emit a durable `model.retry` event and are refused once any text has
streamed (retrying would duplicate the user-visible answer). The policy is
tunable with `HARNESS_MODEL_ATTEMPTS`, `HARNESS_MODEL_RETRY_BASE_S`,
`HARNESS_MODEL_RETRY_MAX_S` and `HARNESS_MODEL_RETRY_BUDGET_S`.

**One execution vocabulary (Round 5 / C2, ADR-H349).** The four planes named the
same lifecycle differently (`RunStatus`, `JobState`, `TaskStatus`,
`graph_runtime.RunStatus`). `harness/execution_plane.py` is the ONE mapping:
`ExecutionPhase` (`pending | queued | running | waiting | succeeded | failed |
cancelled | timed_out`), `JobKind`, and total adapters for each plane. An unknown
value maps to `None`, never to an invented phase, so a stale or foreign row is
handled by the caller instead of being mislabelled as live or terminal. It owns
no scheduler; it is the shared language the planes converge on.

**One execution substrate (Round 5 / C2, ADR-H350).** The Execution Fabric is the
only place an execution attempt runs:

- an **agent** run is a Fabric `agent` job (already);
- a **Graph Studio** run is a Fabric `graph` job (already);
- a **task** run is a Fabric `task` job: `TaskScheduler` keeps its claim, lease,
  heartbeat and verification loop (it is the producer) and submits the attempt
  with `await_result=True`; the Fabric `task` worker reconstructs the live task
  and calls the SAME `service.task_executor`. A submit failure falls back to the
  inline executor; a submitted job's failure is reported, never silently re-run;
- an **orchestration DAG** is submitted through `ExecutionManager.submit_dag`,
  which invokes the existing orchestration engine.

The legacy producers remain by design (the task claim loop must own the lease),
but they no longer own a second execution path.

**One append-only execution log (Round 5 / C2, ADR-H350).**
`harness/execution_log.py` owns `ExecutionEvent(source, entityId, seq, type, at,
data, parentId)`. The run emitter and the Fabric, graph-runtime, task and
orchestration stores append to it after writing their own row. Those tables stay
as per-plane **projections** for their existing readers, so the change is
additive and rollback-safe; a cross-plane view (for example "everything that
happened to this run, its child jobs and its tasks") reads the one log. The log
is bounded (200k chars per row), fail-open, and sequence is per entity, not
global.

**A truncated tool result keeps its text (Round 5 / C3, ADR-H352).** The
`ToolRegistry.execute` seam still bounds the model-facing text at 24k, but a
truncated result now stores the FULL text in `ToolOutput` (run-scoped,
2M-char ceiling) and returns `ToolResult.output_id`. The result text names the
handle and the next offset; `read_tool_output` pages it back (12k default, 24k
max) only for the run that produced it. Offload is fail-open: if the store is
unavailable, the old truncation still applies.

**Tools are discoverable, not all bound (Round 5 / C3, ADR-H352).**
`tool_search` returns up to 12 matching tools with their exact parameters (the
response is bounded, and drops schemas rather than flooding). `tool_invoke`
executes a discovered READ-ONLY tool by name; an effectful tool is refused there
on purpose, because the graph's tools node owns budget, duplicate suppression,
idempotency, approval and recovery for those. `HARNESS_LAZY_TOOLS=1` opts into a
narrower bound surface: every effectful tool stays bound, and the read-only long
tail is reachable through the discovery pair. The flag is OFF by default until
eval parity, as the plan requires.

**Code Mode is confined, read-only composition (Round 5 / C3, ADR-H352).**
When `HARNESS_CODE_MODE=1`, the `tool_code` tool runs a short Python program in a
`code/sandbox.py` child (process-tree kill, scrubbed env, no network, CPU/memory
caps, fresh temp cwd). The program calls `tools.call(name, **args)`; each call
crosses a JSON-lines bridge and is executed by the run's own registry/context, so
validation and permission still apply. Only READ-ONLY tools cross the bridge:
effectful tools are refused there and must be called directly, preserving the
graph's budget, approval, idempotency and recovery. The call budget is 20, the
timeout is bounded, and a missing isolation primitive is a named refusal — never
an unsandboxed fallback. Containment is process/resource/network/env plus a fresh
temp cwd, not a filesystem jail; the operator flag is for a single-user install,
and multi-tenant use waits for C6's filesystem profiles.

**Safety is a named profile plus a per-call token (Round 5 / C6, ADR-H355).**
`security/profiles.py` turns the isolation promise into a plan:
`read-only | workspace-write | network-allowlist | full`. `resolve_profile`
maps it to the active backend and reports `enforceable=False` with the exact
missing guarantees when the host cannot keep it; nothing silently downgrades. The
egress allowlist is exact-or-`*.domain`, and `network-allowlist` requires a
configured egress proxy (a raw bridge is not an allowlist). The security
authorizer mints a signed **effect token** bound to `(run, tool, args, tenant,
expiry)` and the tool executor verifies it before the handler runs, so a widened
or replayed call is refused. Tokens default to `shadow`; `HARNESS_EFFECT_TOKENS=
enforce` is the gate. Auth is required by default for a non-loopback bind or a
production build, while loopback `npm run dev` stays zero-config. The terminal
executor and background-process spawner call `executor_guard()` themselves, so a
configured profile is enforced below the gateway too; `computer_use` does the
same before input injection. Native isolation comes from `bubblewrap` on Linux,
Seatbelt on macOS and (opt-in) AppContainer on Windows; without the opt-in,
Windows keeps Job Object process/resource isolation and refuses
isolation-requiring profiles. Untrusted plugins already run out-of-process;
installs now pass a signature + review gate with Ed25519 publisher keys
(`plugin.sig`) or operator HMAC over manifest + package digest.

**Telemetry has to bite (Round 5 / C7, ADR-H356).** Every run exports
`eclipse.run → eclipse.step → eclipse.llm/eclipse.tool.*`; the step events are
emitted by the graph loop and the projector parents children to the step. A
closed failure taxonomy (`provider|timeout|transient|validation|tool|budget|
policy|sandbox|persistence|cancel|internal`) labels spans, errors and the
`eclipse_failures_total` metric. Cost is attributed per run and per step. SLOs
support ratio, percentile and rate kinds: terminal correctness, p95 latency,
resume p95, orphan rate and cost per task, plus agent/tool success. The release
gate (`npm run gate:slo`) evaluates the deterministic probe report against
`docs/harness/slo-budgets.json` and the committed baseline, and CI fails on a
breach or regression. `/observability/dashboard` renders one card per SLO.

**There is one way to spawn a child (Round 5 / C5, ADR-H354).** The `spawn`
tool submits a normal Fabric `agent` job (`parentRunId`, `maxSteps`,
`runTimeoutMs`, depth/budget caps), so the Fabric owns lease, timeout, retry and
cancellation. A child returns a `ChildOutcome` with claims + handles; its
`EvidenceClaim`s are copied into the parent ledger with a `child:<runId>`
verifier. `spawn` with up to 8 `children` runs them and returns a **deterministic
synthesis** (dedupe, conflict detection, provenance) — no model merge.
`board_write`/`board_read`/`board_search` provide one bounded blackboard per ROOT
run, reached by walking `parentRunId`; a child cannot read another tree. Budgets
are a tree: per-child caps plus a 120k total allocation, 8-child and depth-2
limits; the batch reports `allocated = used + returned`, and a child never
inherits an operator grant or the parent's autonomy. A2A/team keep their planned task rows, but their execution attempt goes
through `spawn` (`tasks/spawn_bridge.py`) when the Fabric is available; without
it, the inline executor is the fallback.

**A run can be a goal step with its own evidence (Round 5 / C4, ADR-H353).**
A request may carry `input.context.c4Plan` — an objective + acceptance + ordered
steps, or a `planId` to continue an existing plan. `harness/goals/` stores the
goal-scoped plan view (`GoalRecord`/`GoalPlan`/`GoalStep`), the evidence ledger
(`EvidenceClaim`/`EvidenceHandleRow`) and the plan iterations, on the ONE
`agent-memory.db`. The task runtime still owns execution; this is the goal view.

- Every tool result with a handle (run record, file+sha, tool-output id,
  artifact, citation, record, computation) is recorded for the current step.
- The draft answer's checkable sentences (numbers, finance shapes, URLs,
  test/build claims, produced files) inherit the run's handles.
- `VerifierRegistry` picks the most specific deterministic verifier:
  `finance_number` (a number with no computation/report/citation/record is
  rejected), `citation`, `code_check`, `artifact`, then the generic evidence
  fallback. A rejection is `unsupported`; a missing handle is `unverified`.
- A specific rejection with replan budget left diverts the graph through the
  bounded `replan` node (max 2 iterations); the failed draft's answer claims are
  superseded, its tool evidence is retained. Without budget, the step is failed
  and the answer seam can disclose the unverified claims (`HARNESS_EVIDENCE_ENFORCE=1`;
  the ledger and outcome record them either way).
- The checkpoint names `planId`/`planIteration`/`stepId`, so a later run with the
  same `planId` continues the first non-verified step. `GET /runs/{id}/outcome`
  exposes `goal` and `evidence` blocks.

**The graph takes two composition objects (Round 5 / C2.1, ADR-H351).**
`build_graph(spec, scope)` replaces a ~45-keyword signature. `RunScope`
(`harness/run_scope.py`) is the process-wide services a run reuses (registry,
bridge, stores, memory, MCP, market, portfolio, search, ...); `RunSpec` is the
facts of THIS invocation (request, emitter, resolved agent, allowlist, effect
authorizer, resume point, result sinks). Both are unpacked at the top of the
graph, so node bodies and tools see exactly the same locals. `RunSpec` copies
the allowlist, so a run cannot mutate the caller's surface. A contract test pins
the two-parameter signature.

**Duplicate suppression is scoped to one agent step** (ADR-H90). Two identical
`(tool, args)` calls in the SAME turn: the first executes, the second is refused
with `tool.failed`/`TOOL_FORBIDDEN`. A call repeated in a LATER turn executes
again — the guard exists to stop one turn repeating itself, not to block a
legitimate re-issue (next page, re-poll, retry). The retrieval layer still
deduplicates identical data fetches per run via the shared cache, so a cross-turn
re-issue does not multiply upstream calls.

**Tool-result text is bounded at the execution seam** (ADR-H91).
`ToolRegistry.execute` trims a model-facing text result to 24 000 chars with a
non-JSON marker naming shown/total and structured `truncated`/`shown_chars`/
`total_chars` fields; the graph emits `tool.truncated` once per trimmed result.
Under the bound the text is byte-identical; `ToolResult.data` is never bounded.

**The tool surface is a UNION, not a replacement (ADR-H280).** A *surface* is the
set of tools a run may use: the finance agent has one, a chat coding turn has
another (`CHAT_CODE_TOOLS`). `python/harness/surface.py` is the ONE resolver,
and it combines the two by union —

    allowed = policy_narrowed(base) UNION policy_narrowed(surface)

— with BOTH halves narrowed through the ONE policy authority
(`code.policy.allowed_tools`). So an autonomy profile still removes every
write/execute/external tool it bans, from the base half and from the surface
half, and a surface never removes a base tool: a tool may leave a run's surface
only through the autonomy profile or through an operator-disabled store, and
both are NAMED. The resolution is published on the run row and returned on the
run result (`RunResult.surface`): the surface id, what it added, what the
profile removed, what a disabled store removed, the bound tool count, and the
**measured** schema characters the surface added (ratcheted by
`MAX_SURFACE_ADDED_SCHEMA_CHARS`).

Why the union is not optional: before ADR-H280 the resolver REPLACED the base
with the surface, which cost a coding turn **441 tools** — the whole task/plan
family, the whole web/research family, the unified search family, the
truthful-capability diagnostics and the response-contract tools — without
naming one of them. The response-contract loss was a visible defect:
`response_block_schema` is what the run checks before injecting the live
response-schema digest and the response-mode plan, so a coding turn produced a
degraded structured response with no error.

**The harness has a PUBLIC surface (ADR-H298).**
`python/harness/public.py` is the whole public API - `AgenticService`,
`ToolRegistry`, the run contract, and the export functions - and it is a
closed list. `export_tool_schemas()` produces the PORTABLE tool manifest: the
MCP tool-manifest shape plus the parts a consumer needs to bind a tool
safely (`permission`, `effect`, `risk`, `version`, the deprecation fields). The
honesty contract is the point: a tool is exported with its permission and its
effect class, or it is NOT exported. A write-ish tool with an undeclared
effect is withheld, because a consumer cannot honour a rule it cannot read.
`docs/harness/tool-registry.json` is that manifest, generated, with a drift
test. The package metadata (`pyproject.toml`) declares the RUNTIME and no
data: the provider SDKs are optional extras, no package data ships, and the
app stays private.

**A shell sees no more of the environment than a command does (ADR-H297).**
The real host shell used to inherit the FULL `os.environ`, so one `shell_run`
could read a provider key, the callback token or the vault and print it into
the conversation, the run blob and the ledger - while the sandboxed command
path had been scrubbed since Phase 1. Both now use the SAME builder
(`code.sandbox.build_env`: the allowlist only), so a shell and a sandboxed
command see the same world. Command and shell OUTPUT is redacted through the
one existing primitive before it reaches the model, and the network refusal is
NAMED (`NETWORK_DENIED`) rather than being an indistinguishable message - and
whether the network was actually CONTAINED is published, because the container
tier really does run `--network none` and the local tier cannot.

**The benchmarks are GENERATED, and the live tier is a lane (ADR-H296).**
`docs/harness/benchmarks.md` is written by a generator and never hand-edited,
so a number cannot be quietly improved - a test regenerates it and compares.
The two tiers are kept APART and named: the OFFLINE tier replaces the model
with a recorded script, so it measures the RUNTIME (the tool surface, the
policy, the graders, the wiring) and says so; the LIVE tier runs a real model
and is the only tier that measures the agent. Both carry their dataset,
checksum, agent version, commit and grader version. The live lane never
fabricates: with no provider it publishes "NOT MEASURED" with the reason and
exits 0, and the nightly workflow SKIPS rather than failing when no secret is
configured.

**The decision record, the size ceiling and the release (ADR-H295).**
A decision series that cannot be referenced is not a record. The harness
series had seven DUPLICATE numbers and five gaps, so "ADR-H242" named two
different things; the later duplicate of each was renumbered and the old
number retired with a redirect, and the withdrawn numbers are reserved.
`scripts/check-adr-numbers.js` fails the release gate on a duplicate, an
unreserved gap, or an entry that does not carry Decision / Why / Consequences
- and it never renumbers anything, because renumbering is a judgement call
that belongs to a human. The shape is enforced from H288 onward; the
older entries are reported and countable rather than rewritten, because
inventing a rationale for someone else's decision is not honesty.

The harness also has a TOTAL ceiling now, not just per-module ones:
`scripts/size-budget.js` checks a committed budget for the whole tree and for
its twelve largest subsystems, so the harness can no longer double by adding
files. And the release is real - the version, a release note derived from the
CHANGELOG, a rotation script for the changelog's rolling window, and a
CODEOWNERS file for the paths where a mistake is hardest to see.

**The model is TOLD the shape of the project (ADR-H287).** A *repo map*
- the folders, and a symbol outline of the top files - is built EAGERLY and
DETERMINISTICALLY and handed to the model in the FIRST turn, together with the
CIUE context engine's answer for THIS request (the "seeded slice"), resolved
once per run with no model call. The model no longer has to guess a path to
find out what exists.

The map is BOUNDED (a file count, a symbols-per-file count, a byte budget and
a render budget - it is a summary, never a copy), DETERMINISTIC (the same tree
produces the same map and the same digest, byte for byte), and HONEST about
its engine: the outline comes from the stdlib extractor (Python's own `ast`,
a brace/regex scanner for everything else), and tree-sitter's availability is
published in the code-intel status because an optional layer that silently
no-ops is a false capability.

The map's content hash is the prompt-cache key's witness: a project whose
shape changed invalidates the cache, and one that did not change does not. It
gets its OWN segment for exactly that reason - `runtime_context` is
run-frozen and its content is not hashed, so a map appended there would have
been cache-key-invisible.

**The dropped turns are COMPACTED, not just dropped (ADR-H286).** The
history bound no longer throws the conversation away. What it removes is
carried forward as a fenced, labelled summary: the requests that were made
and the answers that were given (the DETERMINISTIC skeleton, which always
runs), plus a bounded model NARRATIVE on top of it - the decisions taken, the
open questions, the state of the work. The existing inert dropped-context
block is not replaced: both reach the model, the compaction first. The
narrative is ADDITIVE and fails CLOSED - an unavailable, silent or failing
model leaves the skeleton untouched, and the run records why. The block is
DATA, never instruction (a compaction can contain anything the user typed),
and it never enters a research claim: a summary of a conversation is not
evidence.

**Two more layers exist, both opt-in (Phase 4).** (1) A *zero-cost fold*: a turn
longer than `COLLAPSE_MIN_CHARS` is reduced to head + tail + a marker naming how
many characters were omitted and the sha256 of the full original — no model
call, idempotent, never a summary. It runs in `_bounded_history` BEFORE the
character cap so an oversized turn can stay instead of being dropped whole.
It can change which turns survive trimming, so it is OFF unless
`BE_HISTORY_COLLAPSE=1`. (2) An *iterative* narrative: when a prior briefing is
supplied, the compaction prompt asks the model to UPDATE it (keeping facts that
are still true, correcting what is wrong) instead of writing a fresh one, so the
Nth compaction does not forget what the earlier ones established.

**One edit tool, and it insists the run read the file (ADR-H285).** A *text
edit* is ONE tool: `code_edit` (`path`, `oldText`, `newText`,
`expectedOccurrences`). It replaces text; the operations that are genuinely
different - creating a file, deleting one, moving one, inserting without a
replace target, deleting a line range, applying a prepared diff - stay as the
tools they are. What went is the DUPLICATION: `replace_text`, `patch_file`,
`edit_file`, `multi_file_patch` and `workspace_patch_apply` are deprecated,
still callable, and each names its replacement.

Two rules make the one tool safe, and both are enforced in code:

- **A file may only be changed if THIS RUN read it.** The per-run READ LEDGER
  (`harness.tools.read_ledger`) records every file a read tool returned a hash
  for, and the registry feeds it in ONE place - so every read tool
  participates. An edit to a file the run never read is refused with
  `FILE_NOT_READ`, and the refusal names the read that would make it legal.
- **A file may only be changed if it has not changed since it was read.** The
  ledger's hash is passed to the edit engine as `expected_hash`, so the
  engine's own `STALE_FILE` refusal fires - the same code, the same recovery
  hint, in one place. An ambiguous match is refused too, never guessed.

`code_edit` is a WRITE tool in the ONE policy authority, so `plan_only`
removes it and `edit_verify` keeps it - exactly like the tool it replaces. A
wrong path comes back with up to five nearest candidates rather than a bare
"that file isn't there".

**The search surface is two tools, and the engine is always named (ADR-H284).**
A model asks two questions about a codebase - "what does this SAY?" and "what
files are there?" - and the harness answers them with exactly two tools:
`search_text` for file CONTENTS and `search_files` for file NAMES. The older
names (`workspace_search`, `find_files`) stay callable for one release as
DEPRECATED ALIASES; the registry warns once per run naming the replacement,
and the agent surfaces carry the canonical names only.

`search_text` is ripgrep-first with a bounded Python fallback, and **the
answer always names the engine that produced it** (`ripgrep` or
`python-scan`) - a slow result is explainable rather than mysterious. Before
ADR-H284 the workspace-level search ran its own naive Python walk while the fs
subsystem's `search_text` was already ripgrep-first, and the slow one had the
friendlier name and sat first in every surface. `search_files` now carries
every filter `find_files` had (exclude, extensions, directoriesOnly,
filesOnly, includeHidden, includeIgnored), so nothing was lost in the
consolidation.

A search goes through the code capability, so it needs a code profile - the
Phase-0 rule (no profile, no code capability), and it closes a hole: the old
workspace-level search bypassed the code policy entirely.

**The verify-and-repair loop runs in the graph (ADR-H281).** A *change* is not
finished because the model said so. After a tool turn that changed something,
the graph's `verify_repair` node runs the project's pinned check CLASSES
through the ONE command path - `code.command_tools.command_run`, the same
function the model's own `command_run` tool call goes through - so the policy
gate, the authorization, the ledger row and the events are identical to a
model-issued check. When a check fails, the failure is handed back to the
MODEL, because the model is the only thing that can fix code.

The budget is the run's: attempts, wall clock, cost, and the number of check
runs. Exhaustion is a REPORTED state, never a silent stop. The loop's outcome
is a first-class state whose word is DERIVED from the facts - `REPAIR_NOT_RUN`,
`REPAIR_PASSED`, `REPAIR_FAILED`, `REPAIR_EXHAUSTED`, `REPAIR_NOT_APPLICABLE`,
`REPAIR_REFUSED` - and the answer seam receives the real outcome, never a
hard-coded `None`. A run that never repaired and a run whose repair passed are
different facts. When the check was still failing, the answer says so.

The operator pins the checks in `.black-eclipse/verify.json` as class NAMES -
`{"checks": ["unit_test", "typecheck"]}` - not a shell string: a check that
cannot be named cannot back a "tests pass" claim, so it is not evidence and it
is not run here. A repair never widens authority: the pinned class still goes
through `code.policy.decide_command` and the profile's grants, and `plan_only`
- the read-only profile - never repairs at all.

**The outcome envelope (round 4, W1.3).** A run that did not get done must be
diagnosable from ONE record, not from a replay of scattered facts. The
envelope (`python/harness/run_outcome.py`) names the four facts the meta loop
needs: the **intent verdict** (what the router decided — recorded once per run
by the `ui_classify_request` tool, the first verdict wins because it is the
routing), the **resolved surface and allowlist** (the `prompt.plan` event
carries the agent, the tool count, the first 40 names and a digest of the
whole list), the **refusal code** (the run's terminal error code, else a
refusal-shaped event), the **capability gap** (the `ui.capability_gap` event
plus the recovery incident ids the run recorded) and the **quant outcome** (when
a `quant.plan` event exists: tier/kind, named artifacts, tool calls and the
missing deliverables, derived by `quant_intent.evaluate_quant_outcome`). It is a
DERIVED, read-only
record — `GET /runs/{id}/outcome` — and a fact that was never recorded is
`null`, never guessed. The module owns its own `RunOutcome` table (the
`run_lineage.py` pattern) so the ratcheted `persistence.py` does not grow.

**Run lineage, checkpoints and resume (Phase 1).** A run now has an identity that
survives a crash. `build_graph` writes a bounded `RunCheckpoint` at every durable
boundary (after each completed agent step, after each tool-result batch, and at
terminal status); the payload is a REFERENCE (step index, counters, pending tool
calls, resolved allowlist, and a capped serialized message tail) — never a second
copy of the event log. Each executed tool call is recorded in
`ToolExecutionIdempotency`, keyed by `sha256(runId, stepIndex, toolCallId)`. A
resume (`POST /runs/{id}/resume`) re-enters at the recorded node, REUSES a
recorded tool result instead of re-running it, and a resume of a run with no
checkpoint is `CHECKPOINT_NOT_FOUND` / of a terminal run is `RESUME_CONFLICT` —
never a silent fresh run. `GET /runs/{id}/lineage` returns the bounded parent
chain + children. Decisions: ADR-H93.

**Forced final answer.** On the last allowed step the agent node unbinds tools,
appends an in-band instruction to answer now, and strips any residual tool calls.
A tool-looping model (common with local models, which will even hallucinate a
tool that does not exist) therefore produces an answer from the evidence it
gathered instead of `MAX_STEPS_EXCEEDED`. If the forced turn still yields no
text, the run fails honestly rather than reporting a blank "completed".

### Bounded replanning (Phase 11)

When a tool step FAILS (one or more tool errors) or STALLS (the agent re-issues
the same `(tool,args)` set as the immediately preceding turn), the loop may
divert through ONE `replan` node on the SAME `tools → agent` edge before
continuing:

```
tools —(failed / stalled, budget remains)→ replan → agent
```

`replan.py` owns the trigger (`should_replan`, a stall outranks a failure), the
bound (`ReplanBudget.max_replans`, default 2) and the deterministic directive
(`build_replan_instruction`). The `replan` node appends INERT prose naming the
failures/stall; it authorizes nothing and cannot widen the allowlist. It emits a
persisted `run.replanned` event (`replan`, `reason`, `maxReplans`), so a
diversion is honest and observable. The path is FLAG-GATED
(`HARNESS_REPLAN_ENABLED`, default OFF); when off, `build_graph` adds no node and
the graph is byte-identical to before. An optional, best-effort model-authored
plan (`replan.model_replan`) is additive — it can only be text. Decision:
ADR-H122.

### The composable workflow graph (Phase 5)

Orchestration lives ON TOP of the one execution plane — it does not replace
`execute_run` and does not build a second agent loop, tool registry or model
adapter. A `Workflow` (`python/harness/workflows/`) is a versioned, bounded plan of
`WorkflowStep`s (`agent` | `skill` | `conditional` | `parallel` | `approval` |
`retrieval`). `validate.py` rejects a cycle, an unbounded fan-out, a step
referencing a tool outside its declared scope, and a step with no terminal path —
each with a NAMED reason. `compile.py` invokes the existing `execute_run` per
step; a step's effective tool scope is the INTERSECTION of the workflow agent's
allowlist and the step's declared tools, enforced at the SAME
`ToolRegistry.execute(allowed=...)` seam (a denied call is a structured
`TOOL_FORBIDDEN`). Approval steps reuse the existing single-use `ApprovalStore`.
Parallel branches share the run's cancel event and the shared retrieval cache; the
run ends in EXACTLY ONE terminal status. The step tree is emitted per step on the
existing bus (`workflow.step_started`/`workflow.step_completed`) and shown in the
run explanation.

**Non-goals.** Not a generic harness for arbitrary finance tools — agents must NOT
inherit each other's tools or access, so a step can only NARROW its scope. Not a
visual workflow designer, bot scheduler or agent-builder UI (roadmap Phases 8–9).
The path is flag-gated (`HARNESS_WORKFLOWS_ENABLED`, default OFF); a run with no
`workflowId` behaves exactly as before. Decision: ADR-H96.

### Live streaming (SSE)

The runtime streams tokens as the model writes:

```
model.astream → transient `model.delta` events (event bus, NOT persisted)
             → GET /runs/{id}/stream (SSE)
             → GET /api/harness/runs/[id]/stream   (relay proxy)
             → POST /api/harness/chat/stream        (starts the run + relays)
             → chat UI (live status + partial answer)
```

`model.delta` frames are *transient*: they ride the in-process bus to live
subscribers but are never written to the trace (thousands of fragments would
bloat `agent-memory.db`); the assembled message is still persisted by
`model.completed`. A run is started with `background=true` so its stream can be
observed, and the front door sends the terminal `client.final` with the
authoritative answer when the stream ends. If SSE is unavailable the client
falls back to the one-shot `/api/harness/chat` — a run always yields an answer
or an honest error.

**Where the time went (`run.timings`).** Because deltas are not persisted, the
trace alone cannot answer "why did this feel slow?". `RunEmitter` feeds a
`RunTimings` recorder (`run_timings.py`) from the events it already sees and, on
the run's terminal event, writes ONE durable `run.timings` record:
`setupMs` (`run.started` → first `model.requested` — prompt, memory, surface and
bridge work), `ttftMs` (the provider's own first-token latency, from
`model.first_token`), `firstDeltaMs`, and the p50/p95/max gap between
`model.delta` frames (cadence: a bursty stream and a smooth one have the same
tokens and a very different feel). Absent marks are omitted, never faked; the
record is fail-open and exists once per run, not once per token.

**Both run paths are now visible (Round 5 / ADR-H348).** `service.run` (the
foreground path) registers its task in `service._active` exactly like
`run_background`, and both paths call `bus.end_stream` on completion. So
`is_active`, `GET /runs/{id}/stream` and `POST /runs/{id}/cancel` work for a
foreground run too; before this, the stream replayed and closed while the run was
still executing and cancel was a no-op. `HARNESS_MAX_CONCURRENT_RUNS` (default
0 = unbounded) adds an optional admission semaphore without changing any
contract.

**Multiple conversations run at once.** The client owns runs per conversation
(`src/lib/agent/run-manager.ts`): one live run per `chatId`, many chats
simultaneously, each with its own turn sink and recovery record
(`eclipse.activeRuns.v2`). Queued prompts keep their originating chat, and only
the focused conversation's run projects into the single Agent Workspace
(`run-focus.ts`). On the runtime side, the market bridge and MCP sampling model
are per-run `ContextVar`s (`python/harness/run_binding.py`), not fields on the
process-wide singletons. The model gateway also has one process-wide credential
pool and a per-key admission queue (`model_runtime/credential_pool.py`,
`RateLimitManager.acquire_slot`), so parallel sessions wait on quota instead of
failing. Same-origin tabs elect one run owner (`tab-coordinator.ts`) and merge
their transcript writes. See `docs/harness/multi-session-concurrency.md`.

**Change events carry the change itself.** Run events are content-free except
for the one thing a code surface must show: a file change. `code.patch.applied`
(one event per applied op, `code/tools.py`) and every fs write event (`fs.*`,
a `previews[]` list, `code/fs_tools.py`) carry a **bounded, redacted unified
diff** (`code/events.py::diff_event_payload` — secret-shaped spans scrubbed with
the file reader's pattern set, capped at `EVENT_DIFF_MAX_CHARS=12k` with an
honest truncation flag). The chat maps them to inline change cards
(`src/lib/agent/events.ts` → `TurnChange` on the turn/message); the Agent
Workspace expands them in its Changes panel. The diff is never re-derived
client-side, never fabricated, and a capped preview says so.

### Model-aware context budgeting

History is no longer bounded by a hardcoded guess. Before a run, the runtime
measures the **actual request** — the resolved system prompt, the tool schemas
advertised for the run, the per-run blocks (profile, skill manifest, retrieved
memory, dropped-context) and the user turn — then derives the history allowance
from the **resolved model's declared context window** (`ProviderSpec.context_window`
+ `MODEL_CONTEXT_WINDOWS`, via `resolve_context_window`) minus a reserved output
allowance and a margin. `MAX_HISTORY_CHARS` remains an absolute ceiling. The
estimator is the existing `eclipse_memory/profile.py::estimate_tokens` — an
approximation, hence the margin; no tokenizer dependency is added.

An **unknown window stays unknown**: local servers, routers and a custom endpoint
make the budget inert and the bound falls back to the old
`MAX_HISTORY_TURNS` / `MAX_HISTORY_CHARS` constants, byte-identical to previous
behaviour. Every surface reports "unknown" rather than a guessed number.

A provider context-length rejection is classified as `CONTEXT_OVERFLOW`
(false-positive guarded against rate-limit/timeout/auth/quota), retried **once**
with half the allowance when it happened on the first model call (so no
side-effecting tool is re-run), and otherwise reported with a specific reason and
remedy. The success path emits `memory.history_bounded` (counts/estimates only)
so a trimmed turn is never silent; the additive `RunResult.context` blob and the
`HarnessRun.context` column carry the same facts for old-row-safe reads.
Decisions: ADR-H63, ADR-H64 in [`decisions.md`](./decisions.md).

**This runtime owns the real limit on a prompt; the front door does not.** The
front door's bound on a chat turn (`MAX_QUERY_LENGTH`,
`src/lib/harness/validation.ts`) guards ONE REQUEST BODY only — it is not a
policy about how much a person may ask, and it is mirrored by the runtime's
`RunInput.query` bound (`MAX_QUERY_CHARS`, `python/harness/schemas.py`) so the
runtime never rejects a request the app accepted. Whether a prompt FITS is
decided here, by measurement against the resolved model's window, and reported
as `CONTEXT_OVERFLOW`. See
[`docs/chat-ui-audit.md`](../chat-ui-audit.md) §21.

### Token accounting

Every `agent` node reads the model response's token usage — LangChain's
`usage_metadata`, or the `response_metadata["token_usage"]`/`["usage"]` block
that OpenAI-compatible and Anthropic endpoints use — and sums
`promptTokens`/`completionTokens`/`totalTokens` onto the run. The per-step counts
also ride on the `model.completed` event.

Providers are not uniform: a local Ollama/LM Studio server commonly omits a usage
block. A missing block records **0**, never an estimate. `GET /usage` therefore
exposes `totals.runsWithUsage` next to the token totals so the UI can distinguish
"no usage reported" from "no activity" — the same honesty rule as UNAVAILABLE
market data.

Because the graph always streams (`model_stream.py`), usage only arrives if the
provider is asked for it: the OpenAI-compatible factory in
`models.build_chat_model` sets `stream_usage=True`, which sends
`stream_options.include_usage` and yields the final usage chunk (standard OpenAI,
Ollama and most compatible servers support it). Without it a streamed run reports
0 tokens.

`GET /usage?days=N` (default 30) scopes **all** aggregates — totals, by-model,
by-day and the `recent` runs list — to the `N`-day window. `byDay` is
**zero-filled** across the whole window so the daily chart has a continuous time
axis rather than only the days that had activity.

## Code file system subsystem (`python/harness/code/fs/`)

The filesystem tools that let Eclipse inspect, search, patch and edit real code.
`FileSystemService` (`fs/service.py`) is a deterministic, LLM-free facade; the
tools in `code/fs_tools.py` are thin handlers over it. Path security is NOT
re-implemented — `fs/resolver.py` delegates to the existing `code/policy.py`
(`resolve_within`, `decide_write`) and `code/workspace.py` (`confine`), and writes
land in the disposable code workspace (`code/worktree.py`), never the checkout.
`fs/permissions.py` is the ONE capability authority (read/write/create/delete/
move/copy/rename/watch/search); an operator config can only narrow. The edit
engine refuses ambiguous and stale patches (`AMBIGUOUS_PATCH`/`STALE_FILE`),
preserves encoding/BOM/line endings, writes atomically and rolls back; multi-file
patches are all-or-nothing. Search uses ripgrep with a bounded fallback; symbols
use Python `ast` with an extensible extractor table; the workspace index is a
SEPARATE incremental SQLite store under `.black-eclipse/workspace-index/` (never
agent memory). Full architecture: `docs/harness/code-filesystem.md`.

## Next.js front door (`src/lib/harness/`)

| Module | Role |
|---|---|
| `types.ts` | the small shapes the proxy needs |
| `validation.ts` | untrusted input validation (**no agent config** — that's Python) |
| `run-service.ts` | resolve agent + vault credentials → call the runtime |
| `python-runtime.ts` | lazily spawn + health-check the Python service |
| `python-client.ts` | typed HTTP client for the runtime |
| `data-bridge.ts` | the read-only capability accessor (incl. `sec_filings`, `research_health`, `capability_matrix`, diagnostic-carrying `web_search`/`web_fetch`) |
| `capability.ts` | the truthful per-market capability matrix (recognition vs actual route capability) |
| `agent-presets.ts` | fetches + caches agent presets from the runtime (no local copy) |
| `runtime-config.ts`, `runtime-auth.ts` | package probe list, callback secret/base URL |
| `model-runtime-client.ts` | server proxy for the runtime's `/model-runtime/*` inspection routes (operator token; no model logic) |
| `model-runtime-browser.ts` | client-safe fetch wrapper + pure render helpers for the Model Control Center (no `child_process`) |
| `errors.ts`, `ids.ts` | structured errors, run id |

There is **no** run loop, tool registry or model adapter on the TS side.

### How a runtime failure reaches the browser

The front door never guesses a status: it carries the runtime's own status.
`python-client.ts` (and the sibling clients) throw a `HarnessHttpError`
(`src/lib/harness/errors.ts`) that holds the HTTP status beside the runtime's
message, and `isNotFoundError()` is the one way to ask "does the runtime not
have this?". A run the runtime does not know is therefore **404**, not 500 —
the browser can tell "this run is gone" from "the runtime is broken", and a
missing run does not read as an outage. A runtime that is unreachable stays
**503** (`HarnessRuntimeUnavailable`); any other runtime failure is **500**.
The message the operator sees is still exactly what the runtime said.

### Model Control Center

`src/components/screens/model-runtime/` renders the model layer: provider/model
lists with capability states and pricing, task routing, usage/cost, health, and
the routing debug view ("why this model"). It reads
`src/app/api/harness/model-runtime/**` (proxied by `model-runtime-client.ts`).
The UI is read-only and never sees a credential; provider configuration stays in
Settings › AI. Mounted as AppTab `model-runtime`.

### Streaming chat routes

| Route | Role |
|---|---|
| `POST /api/harness/chat/stream` | start a run in the background + relay its SSE stream; emit `client.final` with the answer |
| `GET /api/harness/runs/[id]/stream` | relay the runtime's SSE frames to the browser |
| `GET /runs/{id}/stream` (Python) | the source: replay + live bus, keep-alives, `stream.end` |

The client provider (`src/lib/agent/registry.ts`) parses the SSE frames, maps
them to coarse UI status (`Thinking…`, `Using market_get_quote…`, `Writing…`),
accumulates `model.delta` into the answer bubble, and prefers the authoritative
`client.final` text.

### Conversation checkpoints

A checkpoint is a restorable JSON snapshot of a conversation's turns
(`src/lib/chat/checkpoint-store.ts`), stored under the session folder
(`.black-eclipse/sessions/<chatId>/checkpoints/`, gitignored). The chat state
lives in the browser, so the client sends the snapshot to save and applies a
returned snapshot to restore — the Python runtime stays out of it. Routes:
`/api/chat/checkpoints` (GET list/read, POST create, DELETE). UI:
`src/components/chat/checkpoint-bar.tsx` in both chat surfaces.

### Git workflow

`src/lib/git/service.ts` wraps the repo's git CLI via `execFile` (argument
arrays, never a shell string). It resolves the repo root once (so pathspecs are
correct regardless of the app's cwd), refuses secret-looking staged paths
(`.env`, keys, `*.db`, credentials), never force-pushes, and keeps `push` a
separate explicit action from `commit`. `draftCommitMessage()` builds a subject
from the *actual* staged diff. Routes: `/api/git` (GET status/diff/log/draft;
POST stage/unstage/commit/push). UI: `src/components/screens/git-panel.tsx`,
surfaced as Settings › Git.

## Persistence — a separate database

The Python runtime owns **`agent-memory.db`** (SQLite via stdlib `sqlite3`),
distinct from JDdS (`jdds.db`) and from the Eclipse-Memory control database
(`eclipse-memory.db`). `agent-memory.db` tables: `HarnessRun`, `HarnessEvent`,
`HarnessToolExecution`, `RetrievalRecord`, `ResearchReceipt`,
`LearningRecurrence`/`LearningRecurrenceRun`, `SessionMessage`(+`SessionMessageFts`),
`ChatSession`, `LearningReview` (with `sessionId`/`profileId`/`priority`/`scheduling`),
plus the legacy `AgentMemory`/`AgentSkill` tables (superseded by Eclipse-Memory;
retained for backward compatibility, no longer the memory/skill source of truth).
`HarnessRun` and `SessionMessage` carry `sessionId`/`profileId`/`projectId`
(additive). Durable memory + the skill lifecycle live in `eclipse-memory.db` (see
the `skills/` and `eclipse_memory/` rows above). `MemoryProfileSnapshot`
(migration 12) lives in `eclipse-memory.db`; migration 13 adds patch/version
metadata to `SkillVersion`. Named profiles are stored at
`.black-eclipse/profiles/PROFILES.json` (a projection, not a second store).
DDL is idempotent, and `_migrate()` adds post-release columns
(e.g. the token columns) to an existing database on open — no manual migration
step. `HarnessRun` carries `promptTokens`/`completionTokens`/`totalTokens`;
`usage_summary()` aggregates them for analytics (`GET /usage`).

**One migration ledger (Round 5 / C2).** `harness/migrations.py` adds
`HarnessMigration(namespace, id, description, appliedAt)` and a
`MigrationRunner` that applies each id once, in order, inside one
`BEGIN IMMEDIATE` transaction (a failure rolls the whole batch back). It records
only migrations that opt in; the legacy per-subsystem idempotent DDL is
unchanged, so this is additive and rollback-safe. `RunLineage` is the first
subsystem on the ledger.

**The checkpoint is a cursor, not a truncated tail (Round 5 / C2).**
`RunCheckpoint.payload.messageSeq` points into `RunMessage`, where the full
conversation is stored untruncated (only a pathological single message is capped
at 200k chars, flagged in the row). `RunLineage.save_messages` is idempotent and
deletes a stale tail; `load_messages` restores it. Resume prefers the cursor and
falls back to the bounded 60-message legacy tail, so checkpoints written before
this change still resume. A 200-step run therefore keeps its early evidence.

**Connections are per-thread, not shared.** The store is used by FastAPI's worker
threads and async tasks, so `AgentMemoryStore` hands each thread its own
connection through `harness/sqlite_pool.py` (`ThreadLocalConnections`). A single
`sqlite3.Connection` shared across threads is unsafe even with
`check_same_thread=False` (concurrent `execute`/`commit` can raise `SystemError:
error return without exception set` or interleave transactions). Connections run
in autocommit (`isolation_level=None`) with a `busy_timeout`; WAL lets readers
run alongside the single writer and autocommit makes a second writer wait rather
than fail on a deferred read-to-write upgrade. The one multi-statement atomic
operation (`reset_memory`) issues an explicit `BEGIN IMMEDIATE`/`COMMIT`.
`AgentMemoryStore.close()` releases every connection and is called on the last
lifespan exit (the pool reconnects lazily if the runtime restarts).

## Setup / operations

- `python/requirements.txt` — the runtime stack.
- `scripts/setup-python.js` (`npm run setup:python`) installs it; runs in
  `postinstall`.
- `scripts/doctor.js` repairs it before `dev`/`start`.
- `scripts/test-python.js` runs the pytest suite (wired into `npm test`).
- Env: `HARNESS_PY_URL` / `HARNESS_PY_HOST` / `HARNESS_PY_PORT` (default
  `http://127.0.0.1:8765`), `HARNESS_CALLBACK_URL`, `AGENT_MEMORY_DB_PATH`,
  `HARNESS_REPLAN_ENABLED` (bounded replanning, default OFF),
  `HARNESS_PY_START_TIMEOUT_MS` (cold-start readiness budget; default 240000).
- The runtime is warmed at boot by `src/instrumentation.ts` (non-blocking) and
  also spawned lazily on first use. A cold start imports the whole agent stack,
  so the front door waits `HARNESS_PY_START_TIMEOUT_MS` for `/health` before
  declaring failure; the wait must outlast a loaded cold start or the still-
  starting process is killed and respawned in a loop. If it cannot start, agent
  runs return **503 "runtime unavailable"** — never a fake agent.
- **One cold start at a time.** Next can evaluate the launcher in more than one
  server context, so the in-memory start promise alone did not serialize spawns.
  `python-runtime.ts` takes an atomic file lock
  (`.black-eclipse/harness-runtime.spawn.lock`); a second context waits for that
  runtime's `/health` instead of booting a rival uvicorn. uvicorn runs the whole
  FastAPI lifespan *before* binding the port, so "no listener + no /health" is
  also the normal signature of a healthy cold start — never spawn on that alone.
  A late health probe uses a 5s budget (not 1.5s) so a slow-but-alive boot is
  not mistaken for a hung one.
- **Stable database location.** `eclipse-memory.db` and `agent-memory.db` default
  to the repository root (the dir containing `python/`), not the process cwd;
  `ECLIPSE_MEMORY_DB_PATH` / `AGENT_MEMORY_DB_PATH` override. `GET /health`
  reports the resolved path + a non-secret identity. A legacy cwd-based
  `eclipse-memory.db` is reconciled forward once, non-destructively.
- **Stale-runtime detection.** The spawned Python child outlives a Next.js
  restart (an orphan holding the port), so `ensurePythonRuntime()` compares the
  runtime's `/health` `startedAtMs` against the newest mtime in
  `python/harness/*.py`. If the process predates the code, it is ours and it is
  killed and respawned — otherwise every harness edit would silently keep
  serving old code until the port was cleared by hand.
- **A runtime already on the port is ADOPTED, never raced.** When a listener
  exists, a second `/health` probe decides: healthy means "use it" (the child
  may simply have been slow to answer the first probe during a cold start). Only
  a silent listener is cleared, and only after the busy-grace window. The
  supervisor used to fall through to a spawn when that second probe *succeeded*,
  booting a rival uvicorn that died with `WinError 10048` — the front door then
  reported "runtime unavailable" for the rest of the session (measured
  2026-11-06). A child that dies because the port is taken is now detected from
  its stderr and the live runtime is adopted. Listener PIDs are parsed from the
  exact local-address column of `netstat`, so `:18766` or a foreign `:8766` can
  never be mistaken for the runtime.

  The runtime address is an operator override that actually takes effect:
  `HARNESS_PY_URL` / `HARNESS_PY_PORT` / `HARNESS_PY_HOST` in the environment win
  over the profile default, and `prestart`/`predev` clear the port that will
  actually be used. That is the supported way to move the runtime off a port
  something else owns.

## Extension points

### Plugin kernel and composition (default boot composition)

`docs/harness/plugin-kernel-plan.md` specifies an additive kernel under
`python/harness/kernel/`: a context/service repository with `inject` coeffects,
revertible effects, per-agent scopes, profiles/bundles/ordered patches, a
reconciling supervisor and a read-only `eclipse kernel dump|diff|doctor`. The
first three phases exist and are covered by `python/tests/test_kernel_*.py`
(118 kernel tests). The kernel is the **default boot composition**:
`service.py` configures the runtime and `graph.py` resolves agents, prompt
sections and a per-run tool view through it. `ECLIPSE_KERNEL=off` restores the
original path byte-for-byte (rollback switch); `ECLIPSE_KERNEL=shadow` loads and
validates composition, services and evolution proposals without changing live
behavior (`KernelRuntime.active` is false and evolution apply is refused).
Default-on was validated by a full Python suite under the kernel (3,340 passed,
19 skipped, 0 failed). `sessions` is a read-only provider over
`AgentMemoryStore` and `policy` is the kernel-facing view of the ONE
`code.policy` authority (surface autonomy narrowing delegates through it when
the kernel is active). Bundle resolution enforces declared platforms,
kernel ranges, required bundles (including cycle refusal) and Ed25519 signatures
over the declaration plus patch digests (`ECLIPSE_KERNEL_TRUSTED_KEYS`,
`ECLIPSE_KERNEL_REQUIRE_SIGNATURE`). Hot reload is opt-in
(`ECLIPSE_KERNEL_WATCH=1`): a digest poll reloads the profile and a broken edit
keeps the previous generation. The flag-off path remains the parity oracle, and
the enabled empty-profile path is proven byte-identical. The analysis behind it
(DSH's model, strengths and weaknesses) is
`docs/harness/deepseek-harness-analysis.md`.
The shared evolution **asset contract** has a first slice in
`python/harness/evolution/` (closed kinds/ops, candidate state machine,
prompt-section and agent-preset adapters, exact rollback, the existing
`evaluate_candidate` gate wired to measured statuses, a hash-chained
intent/snapshot journal, TTL/caps and regression rollback, plus a recurrence
observer -> bounded deterministic proposer with shadow mode); tool policy is
consumed by surface resolution, model routes by the graph's same-provider
selection, and composition rows by the live supervisor.
`POST /evolution/assets/{id}/evaluate` is the HTTP measured landing path: it
refuses without baseline aggregates, runs the deterministic suite on a private
store, and records `measured_ok` / `measured_bad` / `unmeasured`. Plugin UI
contributions render through declarative kernel UI slots
(`kernel/services/ui.py`, `/kernel/ui/slots*`) with no HTML accepted.
Phase 4 adds executable plugin contributions: with the flag on, plugin-declared
agents, skills and prompt sections mount through the kernel services, gated by
`agents.contribute`/`skills.contribute`/`prompts.contribute`, cross-checked
against `provides`, and unwound on unload. `harness_inspect` is a read-only
native tool over the same tree. Plugin code can be re-imported in place with
`PluginManager.reload` (operator-only; exact unload first, `loader.evict_modules`
then re-import; bundled/signed/verified plugins are refused), and the opt-in
`HARNESS_PLUGIN_HOT_RELOAD=1` watcher polls active plugin digests. A failed
re-import leaves the plugin unloaded and `FAILED`, never half-mounted. Unload is
also the isolation boundary: every module under the plugin root is evicted, so a
disable/re-enable re-executes the source and cannot serve stale code; host
modules are untouched, and untrusted plugins still run in the separate-process
sandbox.
Profile/ordered-patch composition is applied at boot by
`kernel/profile_boot.py` after plugin startup: the same `load_profile` loader as
`eclipse kernel dump`, mounted through the supervisor, with an absent profile as
a no-op and a failed row restoring the previous generation. `builtin:` rows can
replace the live `tools`, `prompt` and `agents` services with an exact undo.
`kernel/services/wiring.py` binds plugin-contributed research/data providers,
workflows, models and UI panels to the real registries/stores at boot.
`harness/dynamic/` adds session-scoped versioned packages in the existing code
sandbox (define/run/update/stop/undefine, approval, TTL, call/wall-clock caps);
stopping or undefining kills the process tree and removes the package.

### MCP boundary and source strategy (Phase 5)

MCP is a **user-directed interoperability layer** around the existing Python
`ToolRegistry`. A user may connect MCP servers and use their tools for any
finance workflow; Eclipse does not impose a finance-task allowlist and does not
require external results to go through JDdS or the native research verifier.
JDdS is one source among several and may be stale or incomplete; native retrieval
chooses among configured sources by fit, freshness, coverage and provenance,
while directly connected MCP tools remain available under the user's selection.

An MCP result is attributed to its server/tool and retains upstream provenance
when provided. It is **never** labelled Eclipse-verified unless a separate
verification flow actually checked it. A server's description or result cannot
change system instructions, enable a tool, or authorize a call.

**Implemented (Phase 5A–5C).** A new package `python/harness/mcp/`:

| Module | Owns |
|---|---|
| `config.py` | the typed server contract (id, argv command/args, per-server env allowlist/values, cwd resolution, enabled state, per-tool selection with include/exclude filters, declared effects, coarse `trust` tier, confirmed catalog digest, lifecycle/TLS/capability toggles, startup/catalog/call/output/concurrency budgets) + `${VAR}` connection resolution, secret redaction and owner-restricted config writes. Host execution controls only — not a domain allowlist. |
| `naming.py` | collision-resistant namespacing (`mcp__<server>__<tool>` — a double-underscore boundary; a stable digest only on a genuine sanitization collision), JSON-schema validation before registration, bounded descriptions, effect classification (operator-declared → untrusted MCP annotation → `unknown`). |
| `interpolation.py` | `${VAR}`/`${env:VAR}` + `${workspaceFolder}`/`${userHome}`/`${pathSeparator}` resolution (Milestone 1): one pure function, fail-closed remote rule for `url`/`auth`, literal fallback locally. |
| `screening.py` | static screening of a server entry (Milestone 1): refuse-not-sanitize for shell inline scripts, control operators, download/execute + exfil shapes, and known-IOC package tokens; warn on a plaintext `env` secret. Applied at load, save and spawn. |
| `client.py` | the client over the maintained MCP Python SDK (`mcp`): argv launch with no shell (stdio), Streamable-HTTP and SSE for remote, bounded startup/catalog/call timeouts, serialized session access, cancellation propagation, clean process-tree shutdown, TLS/mTLS controls, structured redacted errors. Non-text content (image/audio) is reported as MIME type + decoded byte size, never an opaque omission. |
| `runtime.py` | `_ServerRuntime` per-server state, the tool descriptor/status dataclasses, and the pure helpers (`_catalog_digest`, `_unsafe_url_reason`, `_allowed_uri`, `effective_resource_schemes`) — pure-extracted from `manager.py` (Phase 6.1). |
| `introspection.py` | the `IntrospectionMixin`: `statuses`/`public_configs`/`summary`/`diagnostics`/`catalog_digest_for` (Phase 6.1 extraction). |
| `oauth_flow.py` | the `OAuthFlowMixin`: `oauth_login`/`oauth_logout` and the device-flow HTTP helpers (Phase 6.1 extraction). |
| `execution.py` | the `ExecutionMixin`: result bounding/rendering, the honest session-expiry policy, and `provider_error_line` (continuation extraction). |
| `verify.py` | the `VerifyMixin`: the operator-triggered credential Verify (one bounded read call; a content-free verdict). ADR-H78. |
| `rediscovery.py` | the `RediscoveryMixin`: handles inbound `list_changed` by rediscovering ONE server through the existing path, under the lock, and emitting `mcp.catalog.changed`. ADR-H81. |
| `circuit.py` | the per-server circuit breaker (CLOSED/OPEN/HALF_OPEN), pure state helpers and named thresholds. ADR-H82. |
| `supply_chain.py` | the spawn-path OSV malware query (fail-closed on a confirmed advisory, fail-open on a network error) with a TTL cache and an injectable fetcher. ADR-H83. |
| `utilities.py` / `lifecycle_mixin.py` | pure extractions (resources/prompts utility tools; the manager keepalive/recycle loop) so `manager.py` stays under its size ratchet. |
| `manager.py` | the `MCPManager` facade: discovery, per-tool selection, catalog-digest confirmation, the `ToolRegistry` adapter, per-call rechecks, the per-run call budget, the circuit-breaker check, bounded/redacted output, and `mcp.server.*` / `mcp.tool.*` events. |

**Lifecycle.** `AgenticService.start_mcp()` (run from the FastAPI lifespan)
connects every **enabled** server, discovers and validates its tools, then
registers only **selected**, effect-permitted tools into the ONE `ToolRegistry`.
`stop_mcp()` terminates every subprocess cleanly on shutdown and unregisters only
the names the manager owns (native tools are never touched). `/health` reports
configured/connected/registered counts (never secrets). A `test_server` probe
always tears its temporary child down (success, failure or cancellation).

**Resilience (Phase 6 M2).** A failed initial connect is retried with a jittered
exponential budget and, if it never connects, reaches a terminal state reported
with the attempt count. A call arriving at a failed/closed/lazy server triggers
one bounded reconnect (`manager._reconnect_on_call`) that re-checks selection,
catalog digest and effect before re-registering. A mid-call session expiry
classifies honestly: a **read** is replayed exactly once after a reconnect; a
**write/external_action** is never replayed and returns an explicit
`outcomeUncertain` result. One manager lifecycle task drives keepalive
(`keepalive_interval`) and stdio recycle (`idle_timeout_seconds` /
`max_lifetime_seconds`, only when no call is in flight). A `lazy` server
registers its tools from an on-disk cache keyed by a fingerprint of the
connection config (`mcp/schema_cache.py`) and spawns on first call; a missing or
stale entry falls back to eager. No MCP child outlives Eclipse: on POSIX a single
stdlib-only supervisor (`mcp/orphan_supervisor.py`) reaps registered process
groups on parent death via pipe EOF; on Windows the SDK's Job Object
(`KILL_ON_JOB_CLOSE`) already does. ADR-H68.

**Default-agent reachability.** The Eclipse agent sets `allowMcpTools: true`,
which expands its run allowlist to include the MCP tools **currently registered**
in the registry (`mcp_*`). Registration already requires an enabled server, an
explicit per-tool selection and a permitted effect, so this cannot surface a
merely discovered tool. Custom agents and skills do **not** set the flag and keep
their exact restricted allowlists.

**Discovery is not activation.** `enabled` defaults to False, `selected_tools`
defaults to empty, and only `read`-effect tools are activatable without the
effectful-approval path; a `write` / `external_action` / `unknown` tool needs the
operator to enable that effect class and a matching per-call approval (5G).
Further, an effect is trusted only from an **operator declaration**
(`tool_effects`) or from the operator confirming the server's `catalogDigest`: a
server's own `readOnlyHint` is untrusted and, on its own, leaves the tool
disabled. A changed catalog (tool set / description / schema) produces a new
digest and invalidates prior confirmation until the operator re-confirms.

**Continuation hardening (2026-09-30).** Four further properties ride the same
boundary: (1) an inbound `list_changed` notification (tools, resources OR prompts)
triggers a refresh of that ONE server's listing through the existing path; a
changed tool catalog re-engages the confirmation gate so it cannot keep a tool
active, a changed resource/prompt listing replaces the connect-time snapshot the
utility tools serve, and a content-free `mcp.catalog.changed` event fires
(ADR-H81) — but only when the digest actually moved: a `list_changed` that
announces no real change is re-discovered and produces NO event, so a server that
fires the notification gratuitously cannot spam the operator with "catalog
changed" prompts to re-confirm an identical catalog (ADR-H87). (2) A per-server
**circuit breaker** stops futile calls to a server that connects but fails every
call: only transport failures count (never a denied approval, a caller-side
error, a budget/breaker refusal or a cancellation); an open breaker refuses
immediately and honestly, re-probes once after a cooldown, and has an operator
reset (ADR-H82). (3) A **spawn-path OSV check** refuses a package with a confirmed
malware advisory and allows one on a network error, always stating which
happened, with a TTL cache (ADR-H83). (4) A **sampling model allowlist** bounds
which model a server may request; an over-long assembled tool name is clamped to
64 chars with a stable digest (ADR-H84).

**Trust tier (Phase 6 M1).** `trust` is `full` (default) or `untrusted`
(fail-closed: any unrecognized value is untrusted). An untrusted server requires
a per-call approval for **every** non-read call regardless of `enabled_effects`,
and only an operator-declared effect can activate its tools — its own annotations
never do. This is strictly additive to the operator-declared-effect rule.

**Config contract, screening, sharing (Phase 6 M1).** `${VAR}`/`${env:VAR}` and
`${workspaceFolder}`/`${userHome}`/`${pathSeparator}` are resolved from the
process environment at load/spawn (`mcp/interpolation.py`); a remote `url`/`auth`
with an unresolved reference **refuses the connect by name** (never sends a
literal placeholder), while local stdio values keep the literal. `selected_tools`
accepts the legacy exact list or `{include, exclude}` fnmatch globs (include wins;
empty include selects nothing). A static screen (`mcp/screening.py`) refuses a
shell-interpreter inline script, shell control operators, download/execute and
exfil shapes, and known-IOC package tokens — at load, at save and at spawn — and
warns on a plaintext `env` secret. Eclipse tool names are
`mcp__<server>__<tool>` (double underscore). ADR-H67.

**Config round-trip: a save never silently widens authority (Phase 6.1).**
`upsert_mcp_config` merges from the incoming model's `model_fields_set`:
**absent means preserve; explicitly present means replace** — including an
explicit `null`/`false`/`{}`/`[]` for clearing. A partial save therefore cannot
reset a field it did not mention, so `trust`, `enabled_effects`, `tool_effects`,
`data_source_tools`, `confirmed_catalog_digest` and `selected_tools` are
preserved unless a caller deliberately sends them. `env` keeps its write-only
merge. `install_mcp_integration` is create-if-absent (re-installing never resets
an operator's changes). The settings UI is a full-fidelity editor: it reaches
every field (`trust`, include/exclude filters, per-tool effects, native data
sources, `tools.resources/prompts`, `resource_schemes`, lazy/keepalive/idle/
lifetime, TLS/mTLS, limits, SSE transport, OAuth sign-in) and the
view↔draft↔input mapping is the pure module `src/lib/harness/mcp-draft.ts`.
`public_dict()` emits the raw operator-set bounds so the wire is lossless;
`public_configs()` exposes the capability toggles as `capabilityToggles` (the
`tools` key is the discovered-tool array). ADR-H74. `resource_schemes` narrows
the resource URI schemes a server may read; the global allowlist is a hard
ceiling and narrowing can only ever remove a scheme. A non-text MCP result
reports MIME type and decoded byte size rather than an opaque omission.

**Execution stays authoritative.** Every MCP call enters through
`ToolRegistry.execute`, so argument validation (pydantic), the agent allowlist,
read-only permission, timeout and persistence are enforced exactly as for native
tools. The adapter additionally re-checks, at call time, that the server is still
enabled and connected, the tool is still selected/activatable, and the effect is
permitted. Task cancellation propagates to the MCP request; no retries are issued
for a call that may have side effects. Output is bounded to the configured size,
marked truncated when cut, and redacted — source identity (`serverId`, original
`tool`) and upstream `structuredContent` are retained.

**Trust boundary.** Launch uses an argv list with no shell. Only an explicit
per-server env allowlist/values reach the child — never the Eclipse process
environment, settings vault, model credentials or callback token. A relative
cwd resolves against the repository root, so it cannot be redirected by the
launch cwd. Server descriptions and results are untrusted data and can never
widen tools, permissions or instructions.

**Configuration & credentials.** `<repo root>/mcp-servers.json` (override:
`HARNESS_MCP_CONFIG`); a missing file means MCP is simply not configured.
Discovered env VALUES are persisted in that file because the secure settings
vault is a TypeScript/Prisma store this Python process cannot read and the SDK
needs the value to spawn the child. The file is gitignored and written
owner-only (0600 where the OS supports it); the API returns env KEY names only,
and values are redacted from every diagnostic/event/result (see ADR-H40 for the
vault-integration limitation and the planned secret-reference resolution).

**Catalog selection (Phase 5E).** `python/harness/routers/mcp.py` (registered by
`main.py`) exposes the catalog over the runtime API — `GET /mcp/servers`, `POST /mcp/servers`,
`POST /mcp/servers/test`, `DELETE /mcp/servers/{id}`, `POST /mcp/reload`,
`POST /mcp/servers/{id}/confirm` — and the TS front door proxies them at
`/api/harness/mcp/*` (`src/lib/harness/clients/mcp-client.ts`,
`src/lib/harness/mcp-types.ts`). The Settings UI
(`src/components/screens/mcp-settings.tsx`, section `mcp`) lets an operator
add / test / enable / disable / remove a server, review its discovered tools and
their effect classes, select the read-only tools to activate, and confirm a
changed catalog. A save reloads the manager in place: only the MCP tools it
previously registered are dropped and re-registered, so a change takes effect
without a runtime restart and native tools are untouched. Responses are
secret-free — env VALUES are write-only and the API returns only env KEY names.
Each server also has a versioned `catalogDigest` (identity + effect + schema) so
a changed tool set is reviewable. This is not a finance-domain allowlist: the
user chooses which servers and tools to connect.

**Authorization.** MCP mutations require proof of authorization — the request
must originate from loopback (the front door / local operator) **or** present the
runtime secret (`X-Harness-Operator` / `Bearer`), never a caller-supplied JSON
boolean. As of Phase 6 M1 the previously-open Python MCP **read** routes
(`GET /mcp/servers`, `GET /mcp/approvals`, `GET /mcp/integrations`,
`GET /mcp/workflows`, `POST /mcp/diagnostics`) carry the same
`require_mcp_operator` dependency, because the TS front door already forwards the
operator token — so a directly reachable Python runtime does not expose the
configured subprocess surface to an unauthenticated caller. Tests:
`test_mcp_api.py` (`test_read_routes_refuse_a_non_operator`).

The SAME rule now guards **every** memory/skill mutation endpoint (memory
create/update/archive/review, corrections, policies, suggestion accept/dismiss,
skill author/enable/disable/deprecate/rollback, learning from-run / generate-draft
/ reject, and skill-draft submit/approve/reject). Each takes the
`require_operator` FastAPI dependency (`main.py`), which is transport/token only;
the body `operator` boolean is read afterward and can never by itself authorize.
Previously these endpoints trusted the body boolean, so a directly reachable
runtime — or a crafted proxied payload — could self-authorize; the Next.js clients
now forward the runtime secret as `X-Harness-Operator` so the front door's own
authorization is what reaches Python. Tests: `test_memory_api_auth.py`,
`test_mcp_api.py`.

**Transports (Phase 5D).** A server is `stdio` (local subprocess, default),
`http` (remote Streamable-HTTP) or `sse` (remote SSE). `MCPServerConfig.transport`
selects it; a remote transport requires a `url` and may carry
`auth: {kind: "header"|"query"|"oauth", name, prefix, secretRef}` resolved from
the vault at connect time. All transports share the SAME connection lifecycle,
discovery, effect/approval gating, output bounding and redaction — only the
stream source differs (`client._transport`). A remote URL is checked by
`harness/mcp/ssrf.py` (https-only, loopback http allowed; private addresses and
embedded credentials refused) BEFORE any outbound connection. TLS controls
(`ssl_verify: true|false|CA-path`, `client_cert`/`client_key` for mTLS) are
honored. A hand-typed header/query `auth` is the shipped path; the full OAuth
authorization-code + device flow and the redirect/origin policy remain (Phase 6
M3).

**Finance product layer (presets, workflows, diagnostics).** MCP is presented to
an investor as finance jobs, not servers. `python/harness/mcp/presets.py` is pure
data: finance **integrations** (Sheets/Excel, filings, web search, calendar,
cloud docs, Canva/Figma/Office decks, Notion — each with plain-language setup,
verification/community flags, and read/write tool-name hints) and named
**workflows** (Import Portfolio, Review Portfolio Risk, Prepare Earnings Brief,
Compare Companies, Analyze Trade Journal, Create Investment Memo, Export
Portfolio Report — each with steps, per-step integration, native-vs-MCP, and
approval steps). `python/harness/mcp/diagnostics.py` produces actionable setup
diagnostics (command/Node/npx/Python availability, vault secret references,
endpoint safety + reachability, connection, discovery, selection, and a writable
temp dir) with a `recovery` step each. Runtime routes `/mcp/integrations`,
`/mcp/workflows`, `/mcp/diagnostics`; TS proxies under `/api/harness/mcp/` and a
workflow wizard + diagnostics panel in Settings › MCP Servers. **Every MCP
result carries provenance** (`verification: external`, `eclipseVerified: false`,
source, `retrievedAt`) and is labelled external in its text; native JDdS/portfolio
data stays authoritative. An effectful approval records a plain-language
`actionSummary`, `affectedData` and `risk`. Decisions: ADR-H43.

**Source independence.** MCP results are never routed through JDdS or the native
research verifier and are never labelled Eclipse-verified; they are attributed to
their server/tool. The default prompt tells the agent that JDdS is one fallible
source; to try other eligible configured sources and selected MCP tools before
concluding evidence is unavailable. MCP-aware source planning is **wired into the
native retrieval layer**: an operator-declared MCP data tool (a server's
`data_source_tools` map) becomes a `RetrievalSource` via
`manager.mcp_sources()`, reaches the layer through `ToolContext.mcp_manager`, and
`plan_retrieval(additional_sources=...)` considers it after the native structured
sources but before generic web. The planner primitive
(`mcp/mcp_retrieval.py`) keeps three distinct freshness states
(`fresh`/`stale`/`missing`, plus `failed`) and **preserves a conflict** rather
than silently choosing; provenance is always `secondary`/`eclipseVerified=false`
and a JDdS result is never silently upgraded. A generic MCP tool that is not
declared stays directly callable by the model and is never withheld. Remote
HTTP/OAuth/SSE/TLS (`5D`), resources/prompts (`5I`), sampling/elicitation,
Eclipse-as-MCP-server (`5H`) and the finance preset/install/audit surface (`5F`)
are **implemented**; subscriptions remain a documented non-goal (see the progress
doc and ADR-H67–H73).

### MCP secret references and effectful approvals (Phase 5 extras)

**Vault-backed secret references.** A server can reference a NAMED secret instead
of storing a value: `secret_refs: { API_TOKEN: "my_server_token" }`. The secret
is stored ENCRYPTED in JDDS (`SystemConfig.key = 'mcp_secrets_v1'`, AES-256-GCM
via `connectors/secrets`) and exposed only as names/masked hints
(`/api/harness/mcp/secrets`). The Python runtime resolves the value at spawn over
the authenticated, read-only data bridge (`mcp_secret_resolve`) — a
server-to-server path. The value never reaches a browser, the model, an event, a
log, or a run trace; it is added to the connection's in-memory redaction set so a
server cannot echo it back either. Inline `env` values remain supported but are
the legacy/plaintext path; `secret_refs` is preferred.

**Effectful-tool approvals (5G).** A tool whose effect is `write` or
`external_action` is not run on the model's say-so. Two gates apply:
1. the operator must enable that effect class for the server (`enabled_effects`,
   default `["read"]`); and
2. every call must match an APPROVED, unexpired approval bound to
   `(serverId, tool, argsDigest)`, where the digest is the canonicalized
   validated arguments. A changed payload requires a new approval; a denial is
   terminal for that digest; `unknown` effect is never approvable.

Enforcement is in the ONE `ToolRegistry`: a non-read `ToolDefinition` executes
only when the run's `ToolContext.authorize_effect` authorizer approves the exact
call. With no authorizer wired, every non-read tool is refused (the prior safe
default). When a call is unapproved, the runtime records a PENDING request and
refuses it, emitting `mcp.tool.approval_required`; the operator decides in
Settings › MCP Servers (`/api/harness/mcp/approvals`). Approvals live in
`agent-memory.db` (`McpApproval`) and hold no secret material.

**An approval is single-use** (ADR-H92). A successful authorization atomically
CLAIMS the approval (guarded `UPDATE ... WHERE status='approved'` + rowcount
check) and moves it to a terminal `consumed` state with a `usedAt` stamp. One
operator decision authorizes exactly ONE call; a second identical call is refused
and produces a fresh PENDING request. Because one effectful call is authorized at
two seams (the registry gate and the MCP manager's `_execute` re-assert), the
first claim records the authorized digest on the shared `ToolContext` for that
call-scope so the second seam does not consume again; a new call starts a new
scope. `consumed` and `usedAt` ride the whole round-trip (the row, `_row()`,
`GET /mcp/approvals`, `MCPApprovalView`, and the approvals UI).

**Approval cards (pause-and-wait).** By default an unapproved effectful call is
refused and the model may retry after the operator decides (deny-and-retry). A
run can instead opt into `approvalMode: "ask"` (set by the STREAMING chat front
door only — a synchronous/headless caller leaves it unset, because a pause there
would block the HTTP request with no card to resolve it), which makes the effect
authorizer PAUSE the in-flight run: it creates the PENDING request, emits
`approval.requested` (the card's `{ approvalId, kind, title, summary, effect,
risk }`), then `await`s an in-process `asyncio.Event` keyed by
the approval id (`_GATES` in `mcp/approvals.py`). The decide route — same event
loop — calls `ApprovalStore.decide()`, which commits the decision and then
`notify_decision(id)` wakes the wait. Approved ⇒ the authorizer re-claims the
single-use approval (`consume`/`consume_kind`) and the call proceeds; denied,
expired, or timed out ⇒ the wait returns and the call is refused, exactly as
before. Pause changes only HOW LONG the run waits, never WHETHER a call is
authorized: the decision is still the single-use, digest-bound approval. The wait
is bounded by `ToolContext.approval_timeout_s` (< the run deadline), so a run
whose decision never arrives fails honestly rather than hanging; a runtime restart
mid-wait cancels the run and the deny-and-retry path still applies. The pause
helper is shared (`pause_for_decision`/`_await_then_reclaim`) so the MCP,
code-action, code-write (`workspace_patch_apply`), shell and terminal `ask` paths
behave identically. The card UI is
`src/components/chat/approval-card.tsx`, portaled to `<body>` and mounted above
the composer in both chat surfaces, driven by `HarnessUiEvent.approval_request` /
`approval_resolved` and the `awaiting_approval` status. An `approval_request` also
plays the `notification` cue and raises the shared toast/OS notice
(`ALERT_NOTIFY_EVENT`), so a paused run is never missed.

**Clarifying questions (human-in-the-loop `clarify`).** A turn that is genuinely
ambiguous, or reaches a consequential step whose intent is unclear, can ask the
operator ONE question instead of guessing. The `clarify` tool
(`python/harness/clarify_tools.py`, read permission, registered on the same
registry and allowlisted for the finance chat) takes `question`, an optional
`context`, 2–4 `options`, and a `recommended` option. It creates a
`clarification` row in the SAME `McpApproval` table (`kind="clarification"`, the
new additive `answer` column) and reuses the same in-process gate as approvals,
emitting `clarification.requested` before it `await`s `wait_for_decision`. The
answer is a choice label or free text (`""` means "decide for me"); it is returned
to the model as the tool result, so the run resumes IN PLACE with the answer.
This is deliberately NOT an authorization: the tool is `read`, an answer never
approves an effect, and `answer_clarification` refuses any row that is not a
clarification. The answer route is `POST /mcp/approvals/{id}/answer` (NOT
`/decide`), fronted by `src/app/api/harness/mcp/approvals/[id]/answer`; the card
is `src/components/chat/clarification-card.tsx`, driven by
`HarnessUiEvent.clarification_request` / `clarification_answered`. When there is
no interactive client (a bot or background/scheduled run), the tool never blocks
and never invents an answer: it returns `answered: false`, and the model must
proceed with a stated assumption. The wait is bounded by the tool's own 115s
ceiling (below the registry's 120s cap), and `run_watchdog.py` treats
`clarification.requested` as a legitimate user-decision wait.

**Composer "Choices" mode.** The operator can make that question proactive
rather than incidental: `src/lib/agent/choices-mode.ts` +
`src/components/chat/choices-mode-control.tsx` expose an Auto/Choices/Off submenu
inside the composer's "+" menu (next to the matching Quant submenu), out of the
toolbar until the operator opts in. The value flows as `input.context.choicesMode` through
both chat routes and `run-service`. `python/harness/choices_mode.py` appends a
short block to `runtime_context` ONLY for `"on"` (and only when `clarify` is on
this run's surface), telling the model to call `clarify` once with 2–4 concrete
options before starting a turn whose scope, horizon, entity, output format or
next step is genuinely open. `"off"` and `"auto"` add nothing, so the base
prompt's act-don't-ask guidance stands. It is presentation guidance only: the
tool stays read-only, one question per turn, and it never authorizes an effect.

**Composer "Plan" mode.** The same composer surface carries an Auto/Plan/Off
submenu (also inside the "+" menu, in `src/components/chat/plan-mode-control.tsx`
backed by `src/lib/agent/plan-mode.ts`). The value flows as
`input.context.planMode` through both chat routes and `run-service`.
`python/harness/plan_mode.py` appends a block to `runtime_context` ONLY for
`"on"`, telling the model to work out the goal, assumptions and ordered steps,
record them with the existing durable `create_plan` / `create_task` tools when
those are on the run's surface (otherwise write the plan in the reply), present
the plan, and then WAIT for approval before calling `run_plan`, starting effectful
work, or marking tasks IN_PROGRESS. `"off"` and `"auto"` add nothing. Like the
other two modes it is per-turn presentation guidance: it never hard-blocks a tool
call, widens the surface, or authorizes an effect — effectful steps still go
through the approval card.

**Standing grants — "always allow" (ADR-H144).** Repeatedly re-asking for the same
program is stopped by a separate, revocable permission, NOT by making an approval
repeatable. `python/harness/grants.py` (`GrantStore`, table `StandingGrant` on
`agent-memory.db`) stores `(kind, scope, pattern)` — one program/tool in one
workspace (`command` covers shell/terminal/terminal-subsystem, `mcp_tool` a tool on
a server). Every authorizer consults `grants.matches(...)` BEFORE creating a
pending request: a match authorizes immediately and emits `approval.granted`. The
`deny` classification and unknown effects are still checked first, so a grant can
never run a destructive command or an unknown effect; high-risk and
dangerous/destructive/critical Git ops are never grantable. A grant is minted ONLY
by an `ApprovalStore.remember_grant()` on a real pending approval (from the row's
own `grantPattern`/`grantKind`/`grantScope` columns), via the decide routes'
`remember` flag; `GET /grants` + `POST /grants/{id}/revoke` (`routers/grants.py`,
fronted by `src/lib/harness/clients/grants-client.ts` + `src/app/api/harness/grants/**`)
list and revoke them. The card offers **Allow once / Always allow `<program>` /
Cancel**; "Always allow" appears only when the runtime marked the action
grantable. `McpApproval` stays single-use (ADR-H92) — a grant removes asking, the
store still authorizes.

**The acquire loop (ADR-H188).** The same authorizer chain gained one layer for
capability acquisition: `make_skill_authorizer` (`skills/install_tools.py`) gates
`skill_install`/`skill_uninstall` on a single-use, digest-bound approval
(`skill_install`/`skill_uninstall` kinds in `McpApproval`, pause-in-card-mode
included, no standing grant), and `make_mcp_install_authorizer`
(`mcp/install_tools.py`) gates `mcp_install` the same way. On approval,
`skill_install` runs the staged GitHub loop (`skills/install.py`: codeload fetch →
zip-slip-safe extract → both skill formats → validate + content-scan → commit)
and trusts the install root through the project-trust store, so the registry
loads it as the `external` tier (`project > profile > external > bundled`;
`load_all(installed_home=)` / `reload()`). `mcp_install` delegates to
`mcp/install_ops.py` — the same operation as the operator route — and always
writes a DISABLED, nothing-selected config. `mcp_probe` (same authorizer, kind
`mcp_probe`) runs the Settings "Test" path once — connect + discover in a
temporary subprocess that is always torn down — and reports the observed result
without persisting or activating anything; a missing credential is reported as
`blocked-at-activation`, never a silent "installed". `code/policy.py`
(`ACQUISITION_WRITE_TOOLS`) removes all five tools at `plan_only` and the
unknown-profile fallback; they stay on the chat code profile. Installing widens
what can be used, never what may be done — the skill permission/execution gate
is unchanged.

**The portfolio family (`portfolio/authorizers.py`).** The Portfolio Accounting
Engine's mutations (`portfolio.write`: create/add/remove/update position and the
rest; the consequential `portfolio.rebalance_proposal` proposals) are admitted by
`make_portfolio_authorizer`, composed in `code/authorizers.py` beside the git/
browser/computer/bot layers. Like the Git layer, it is NOT an unconditional
admission: a mutation is authorized only by the call-scope digest already being
authorized, a standing grant (`portfolio_write` kind), or a single-use
digest-bound approval of kind `portfolio_write` (consume, or request-then-pause
for the operator's decision) — otherwise it is refused. The digest binds
scope (portfolio/workspace) + tool + canonicalized args, so a changed payload
needs a NEW approval. Before this layer, the family fell through to the MCP
authorizer and was refused (`TOOL_FORBIDDEN`) while advertised — the same defect
class `_admit_runtime_tools` fixed for the execution family.

**The `/eclipsesdk` chat command (`src/lib/harness/commands/commands.ts`).** A leading
`/command` in a chat message is a DETERMINISTIC profile switch, parsed by the
front door (`parseChatCommand`) and stripped before the run; the remainder is the
instruction. `/eclipsesdk` (alias `/esdk`, case-insensitive) forces the chat code
profile (`edit_verify`) and the analysis sandbox, so a self-edit/build request is
served by the code capability instead of the prose classifier. It sends the
additive `RunRequest.command` marker (`schemas.py`); the runtime never treats a
command turn as trivial (`graph_context.py`). A command selects the code path —
it never widens authority; the runtime still enforces the grant. An unknown
`/token` or a path (`/usr/bin`) is ordinary chat. The composer
(`src/components/chat/chat-input.tsx`) shows a command palette while a bare
`/word` is typed (arrows navigate, Enter/Tab completes, Escape clears) and an
active-command badge once an instruction follows; both are client-side only.
The streaming (`startChatRun`) and synchronous (`chatWithAgent`) paths both parse
the command.

**The extension layer (`skills/manage_tools.py`).** The agent may author, edit and
delete its own procedural **skills** — Eclipse's `skill_manage` analogue. A skill
is DECLARATIVE (a validated `SkillManifest` plus bounded instructions), never code
and never a new tool; authoring rides the SAME `SkillRegistry.create_user_skill` /
`update_user_skill` / `delete_user_skill` a person uses, so versioning, discovery
and the permission policy are unchanged. `authoring.build_user_manifest` refuses
any permission outside `SAFE_PERMISSIONS`, so an agent-authored skill can never
widen its own authority; editing makes a NEW version and a built-in can never be
edited or deleted. The three tools (`skill_create`/`skill_update`/`skill_delete`)
are `write`-permission members of `code.policy.ACQUISITION_WRITE_TOOLS` (removed
at `plan_only`) and are gated by `make_skill_manage_authorizer` — a single-use,
digest-bound operator approval per call (pause-in-card-mode included). The older
inert-draft path (`_create_explicit_skill_draft` → `skills/draft.py`) remains for
the "turn this into a skill" intent; both land in the same reviewable store.

**The lifecycle surface (`skills/lifecycle_tools.py`).** The Skills UI could read
version history and reviews and change lifecycle state, but the agent had no bound
tool for any of it — a self-improvement run asked for version history, review data,
patch activation and a learning switch and was refused on all four. Six tools now
wrap the SAME paths, never a second lifecycle: `skill_version_history`,
`skill_reviews` and `skill_learning_get` are read-only (versions via
`versioning.version_history`, reviews + evaluations via
`store.list_reviews`/`registry.evaluation_history`, the persisted mode via
`PolicyStore.get_all`); `skill_version_evaluate` runs the deterministic
`versioning.evaluate_version`, `skill_version_activate` calls
`versioning.activate_version` (a passing evaluation is still required) and
`skill_learning_set` calls `PolicyStore.set` for `skill_learning_mode` (the closed
mode vocabulary + hard floors still apply). The three writes join
`code.policy.ACQUISITION_WRITE_TOOLS` (removed at `plan_only`) and are gated by
`make_skill_lifecycle_authorizer` (single-use, digest-bound operator approval; the
evaluation/activation digests bind the version, the learning digest binds the
mode). Evaluation never enables anything — it only records the result activation
then checks.

**Self-hosting safety (`code/selfhost.py`, Track B).** A change that reaches
Eclipse's OWN live code carries hazards a disposable-worktree task does not, so
landing it goes through an extra gate. `classify_landing(paths)` is pure: a path
under `python/harness/**` needs a RUNTIME RESTART, a path under `src/**` needs a
BUILD gate, and a path in `AUTHORITY_PATHS` (`code/policy.py`,
`code/authorizers.py`, `mcp/approvals.py`, `skills/permissions.py`) is REFUSED —
never hot-swapped, because it could alter Eclipse's own permissions. `guarded_land`
orchestrates apply → (restart) → `verify()` → rollback: on a failed verify it calls
`rollback_promotion` and refuses (`SELFHOST_VERIFY_FAILED`). Every side effect is
injected, so the policy is tested without a live process; the process restart is
owned by the FRONT DOOR (the runtime cannot cleanly restart itself):
`restartPythonRuntime()` (`src/lib/harness/python-runtime.ts`) kills the runtime
listener and re-spawns it, waiting for `/health`, and is exposed at
`POST /api/harness/runtime/restart` (the "Restart Eclipse now" button in the chat
promotion panel). The front door ALSO auto-restarts on its own: `ensurePythonRuntime`
detects a running runtime whose process started before the newest harness source
and replaces it, so a landed self-edit takes effect on the next call.
The promote routes report the verdict (`landing`) so the
front door can raise a restart/build requirement; `test_selfhost_landing.py`.

| To add… | Do this |
|---|---|
| A chat slash command | Add a `CommandSpec` to `COMMAND_REGISTRY` in `src/lib/harness/commands/command-registry.ts` and its handler to `src/lib/harness/commands/command-handlers.ts`; routed commands run through the shared router, and agent-turn rewrites live in `command-agent.ts`. Profile commands also need a `ChatCommand` in `src/lib/harness/commands/commands.ts` (forced `profile`), parsed/stripped in `startChatRun`. `command-registry.contract.test.ts` fails if the handler target is missing or a high-risk command lacks the operator gate. |
| An agent-authored skill / extension | No code change: the agent calls `skill_create`/`skill_update`/`skill_delete` (approval-gated). To add a new *kind* of extension tool, add a handler + `ToolDefinition` in `python/harness/skills/manage_tools.py`, gate it in `make_skill_manage_authorizer`, and add its name to `code.policy.ACQUISITION_WRITE_TOOLS`. |
| A skill lifecycle tool (history/review/evaluate/activate/learning mode) | Add it next to the existing six in `python/harness/skills/lifecycle_tools.py`; reuse `versioning.*` / `PolicyStore.set`, gate writes in `make_skill_lifecycle_authorizer`, register it in `install_tools.register_skill_install_tools`, and add any write name to `code.policy.ACQUISITION_WRITE_TOOLS` + the agent allowlists. |
| A tool | Define a `ToolDefinition` in `python/harness/`, register it in `register_finance_tools` (or a new `register_*`). Read-only required. |
| A file system operation / tool | Add the operation in `python/harness/code/fs/operations.py` (or `editing.py`) taking an `FsContext`; add the tool in `python/harness/code/fs_tools.py`; if it mutates, add its name to `code/policy.FS_WRITE_TOOLS`. See `docs/harness/code-filesystem.md`. |
| A tool that needs JDdS data | Add a capability to `data-bridge.ts` + a `DataCapability` union member; call `ctx.data_bridge.fetch(...)`. |
| A retrieval data type | Add one row to `retrieval/authority.py` (`AUTHORITY`, a `DataTypeAuthority`), `policies.py` (`DEFAULT_FRESHNESS`, `CACHE_TTL_SECONDS`) and the `DataType` literal in `contract.py`. The authority is the ONE ordering table (ADR-H115). |
| A retrieval provider | Implement `RetrievalSource` in `retrieval/`; register it in `FinancialRetrievalLayer.__init__`. Add its name to the planner's order. |
| A capability the retrieval layer needs | Add a capability to `data-bridge.ts` + the `DataCapability` union + the API route whitelist. |
| An agent | Add a prompt to `python/harness/prompts.py` and an `AgentConfig` to `AGENT_PRESETS` in `python/harness/agents.py` (update `DEFAULT_AGENT_ID` if it should be the default). |
| A model provider | Add a `ProviderSpec` to `PROVIDER_CATALOG`; add its package to `requirements.txt` if it needs a native integration. |
| A local MCP server | Add an entry to `<repo root>/mcp-servers.json` (see `harness/mcp/config.py`): `id`, `command`, `args`, optional `env`/`env_allowlist`/`secret_refs`/`cwd`, `enabled: true`, and the `selected_tools` include list. Only read-only tools activate in this slice. Or use Settings › MCP Servers (`/api/harness/mcp/*`). A ready-to-edit set (Notion, Google Sheets, Excel, Tavily/Brave search, filesystem, Yahoo Finance, memory) is in `mcp-servers.example.json`. |
| Durable knowledge | Use `store.remember` / `store.recall` or `store.propose_skill`. |
| A runtime skill | Add a folder under `python/harness/skills/library/<category>/<name>/` with `skill.json` + `SKILL.md` (and optional `tests/cases.json`); it loads at boot via `skills/loader.py`. |
| A skill source other than a GitHub repo | Extend `parse_source`/`fetch_archive` in `python/harness/skills/install.py` (bounds and the staged validate/scan/trust pipeline stay untouched) and cover it in `python/tests/test_skill_install.py`. See ADR-H188. |
| A skill API surface | Add a proxy route under `src/app/api/skills/` (or `learning/`) and a typed call in `skills-client.ts` (server) / `skills-browser.ts` (UI). |
| A conversational surface | `POST /api/harness/chat` (`chatWithAgent`); pass `history` so follow-ups keep the thread. |

## Model endpoint guard and router probe budget

**Endpoint guard (`model_runtime/endpoint_guard.py`).** A user-supplied base URL
is an outbound request carrying the user's key. An IP literal that is private,
loopback, link-local, multicast, reserved or unspecified is refused at model
construction (`ENDPOINT_NOT_ALLOWED`); local servers stay allowed when the URL is
loopback or local-only privacy mode is on. Hostname resolution is opt-in
(`BE_ENDPOINT_PINNING=1`) and refuses a name that resolves privately.
Connect-time pinning is NOT implemented, and the module says so.

**Router probe budget (`market/router.py`).** `provider_order` stays the pure
preference order. `attempt_order` is the execution order: an OPEN breaker is
skipped unless a recent success for that provider in a DIFFERENT scope grants
this scope exactly one probe per cooldown. That is evidence, not a reset — the
failures stay counted, and a failed probe re-arms the cooldown.

