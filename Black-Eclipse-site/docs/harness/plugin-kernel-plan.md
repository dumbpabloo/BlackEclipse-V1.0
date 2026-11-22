---
title: Eclipse Plugin Kernel and Evolution Engine - gap analysis and implementation plan
status: Draft
last-verified: 2026-11-21
---

# Eclipse Plugin Kernel and Evolution Engine

**Who this is for:** implementers and reviewers of the program that makes
Eclipse a true "everything is a plugin" harness - with a first-party,
evidence-gated self-evolution engine - without giving up Eclipse's security,
financial-integrity and verification guarantees.

**Companion analysis:** `docs/harness/deepseek-harness-analysis.md` (what DSH
does, what it proves, and where it is weak).

**Status of this document:** design and phased plan. Phase 1 (kernel core),
Phase 2 (composition/profiles/dump/CLI), Phase 3 (tools/prompt/agents services),
and Phase 4's executable agent/skill/prompt/provider/workflow/model/UI
contributions plus `harness_inspect` have tested implementations in
`python/harness/kernel/` (118 kernel tests), the plugin suites, and
`python/harness/evolution/` (46 tests). The kernel is the
**default boot composition** (`ECLIPSE_KERNEL` unset = `on`); `ECLIPSE_KERNEL=off`
is the documented rollback switch and `shadow` validates without changing live
behavior. Default-on was validated by a full Python suite under the kernel
(**3,340 passed, 19 skipped, 0 failed**) plus the green default-baseline run
(**3,400 passed, 6 skipped**) and `vitest` (**5,184 passed, 1 skipped**). The
enabled path is also proven byte-identical for the empty profile
(`test_resolve_surface_is_byte_identical_through_the_runtime`). Later phases are
not implemented. Each phase below has an exit criterion and a test list so a
later status update can mark it Built/Partial/Missing with evidence.

### Implemented first slice (2026-11-21)

| Area | File | Covered |
|---|---|---|
| Services, coeffects, effects | `kernel/context.py` | provide/get/require/inject, pending until deps exist, dependents disposed on provider removal, replace guard |
| Lifetime | `kernel/fiber.py` | PENDING/LOADING/ACTIVE/FAILED/DISPOSED, reverse-order effects, idempotent dispose, failing disposer isolation |
| Resolution | `kernel/plugin.py` | module:attr, file.py:attr, function/object/class, `inject`/`optional_inject` |
| Events | `kernel/events.py` | emit/waterfall/serial/bail/parallel; listener removal with its owner |
| Scopes | `kernel/scope.py`, `Context.scope/group` | inherit down, shadow, sibling isolation, ownership |
| Composition | `kernel/composition.py` | insert/override/disable/remove, whole-config replacement, diagnostics, closed expressions, no eval |
| Profiles/bundles | `kernel/profile.py` | ordered bundle patches, profile patch, overlays, skipped-bundle diagnostics |
| Reconcile/rollback | `kernel/supervisor.py` | add/update/remove/disable, generation restore on failure, pending report, rollback |
| Inspection | `kernel/dump.py`, `kernel/cli.py` | deterministic dump, diff, doctor; `eclipse kernel` group |
| Tool seam | `kernel/services/tools.py` | scoped views over the ONE `ToolRegistry`, local shadowing, inherited agent/run scopes, concurrent isolation, execution through the existing pipeline |
| Prompt seam | `kernel/services/prompt.py` | existing surface resolution unchanged, ordered scoped sections, byte-identical built-in prompts |
| Agent seam | `kernel/services/agents.py` | built-ins, stored presets, local agents; one resolution order |
| Runtime seam | `kernel/services/runtime.py` | `ECLIPSE_KERNEL` gate, per-run tool view, prompt/agent resolution, status |

Wired in `service.py` (configure the runtime after the registry exists) and
`graph.py` (`resolve_agent` and the per-run tool view; surface resolution via
the runtime). With the flag off both are no-ops.

Phase 6's contract + evaluator + observer/proposer slice is in
`harness/evolution/`: the closed asset/mutation vocabulary, candidate state
machine and digest (`assets.py`), adapters (`adapters.py`), propose -> admit ->
apply -> rollback (`service.py`), the existing evaluation gate wired to measured
statuses (`evaluator.py`), a hash-chained intent/snapshot journal with crash
recovery (`journal.py`), and the recurrence observer -> bounded deterministic
proposer -> candidate pipeline with shadow mode (`observer.py`, `proposer.py`,
`pipeline.py`). TTL, active caps, post-apply regression rollback and the operator
`/evolution/assets*` API are live. All adapters reach their real consumers (tool
policy -> surface resolution, model route -> same-provider graph selection,
composition rows -> live supervisor), and `POST /evolution/assets/{id}/evaluate`
runs the deterministic suite and lands `measured_ok` / `measured_bad` /
`unmeasured` with no invented baseline. Kernel UI contributions render through
`kernel/services/ui.py` and `/kernel/ui/slots*` (declarative specs only, no HTML).

Not yet: the default-composition rollout is gated on field `shadow` telemetry and explicit approval (see `Default-composition readiness`).

---


## 1. Objective and non-goals

### Objective

A user can create a variant of Eclipse - a different agent, tool set, prompt,
model route, UI surface or whole product profile - **without forking the
repository**, and Eclipse can improve its own declared assets from observed
failures, with the same evidence standard it already applies to finance.

Concretely:

1. **Composition is data.** A running Eclipse is a plugin tree assembled from
   ordered profile/bundle/patch layers, dumpable and diffable before boot.
2. **Every core capability is a service seam.** Model adapter, tool registry,
   prompt assembly, session store, agent loop, skill catalog, policy, retrieval,
   jobs and UI contributions resolve by key, not by import.
3. **Registrations are reversible effects.** Disabling, replacing or hot-
   reloading a plugin unwinds exactly what it registered.
4. **Per-agent composition is real.** A session can mount its own tools, prompt
   sections and skills, and dispose them, without mutating another session.
5. **One evolution engine, many asset kinds.** Prompt sections, skills, tool
   policy, hooks, presets, composition rows and plugin bundles share one
   candidate/evidence/landing contract.
6. **Safety is a precondition, not a feature.** Signed packages, closed
   permissions, out-of-process untrusted code, SSRF/secret boundaries and
   effect approvals remain mandatory at every new surface.
7. **Evidence is honest.** `MEASURED` requires a frozen evaluator, a held-out
   split, paired trials and a significance gate. Everything else is
   `UNMEASURED` and cannot auto-apply.

### Non-goals (deliberate)

- No second runtime, queue, database or event bus. Reuse `agent-memory.db`,
  `EventBus`, `ToolRegistry`, `model_runtime`, `evaluation` and `fabric`.
- No arbitrary in-process third-party Python. Untrusted plugins stay in the
  existing sandbox child process.
- No autonomous code merge, broker action, policy edit or capability widening.
  Those remain operator-only (the existing invariants).
- No JavaScript `!!js` execution in composition. Expressions are a closed,
  declarative vocabulary.
- No Big Bang rewrite of `graph.py`. The loop is adapted behind an interface and
  migrated incrementally.

---

## 2. Current Eclipse inventory (what we build on)

| Concern | Current owner | State |
|---|---|---|
| Plugin manifest, lifecycle, permissions, signing, sandbox | `python/harness/plugins/` (`manifest.py`, `model.py`, `manager.py`, `registry.py`, `runtime.py`, `sandbox.py`, `signing.py`) | **Built and strong** |
| Plugin SDK and contribution decorators | `python/harness/plugins/sdk.py` | **Built** for tools/hooks/events; skills/agents/providers are descriptors |
| Tool registry | `python/harness/tools.py` (`ToolRegistry`), one instance in `service.py:88` | **Built**; global, no scoped layers |
| Agent definitions | `python/harness/agents.py` (`ECLIPSE_AGENT`, `CODE_AGENT`, `BOOKKEEPER`, `AGENT_PRESETS`), `graph.resolve_agent` | **Static constants** + DB presets |
| User agent presets | `python/harness/agent_presets.py` (versioned `AgentPreset` rows) | **Built**; prompt + allowlist + steps only |
| Prompt assembly | `python/harness/prompts.py`, `prompt_segments.py`, `surface.py` | **Static text + runtime overlays** |
| Agent loop | `python/harness/graph.py` (`build_graph`, `agent_node`, `tools_node`) | **Monolith**; no interface |
| Model providers | `python/harness/model_runtime/` (`registry.py`, `routing.py`, adapters) | **Built**, provider seam exists |
| Events and hooks | `event_system/`, `harness/events.py`, `plugins/events.py`, `plugins/hooks.py` | **Built**, durable journal plus bridge |
| Self-improvement (RSL) | `python/harness/self_improvement/` (~45 modules), `code/evolution*.py`, `routers/evolution.py` | **Built and deep**; code/surface/skill subjects |
| Evaluation gates | `evaluation/` (datasets, gates, regression, significance, candidate invoker) | **Built** |
| Run/session persistence, lineage, recovery | `persistence.py`, `run_lineage.py`, `run_recovery.py`, `event_system/` | **Built** |
| Operator UI | `src/components/screens/plugins/`, evolution routes and screens | **Built but fragmented** |
| Evolution asset contract | `python/harness/evolution/` (`assets.py`, `adapters.py`, `service.py`) | **Partial**: contract + prompt/agent apply/rollback; no evaluator |

**The gap is not capability.** Eclipse has more verification machinery than DSH.
The gap is that those capabilities are wired together by direct imports and
hardcoded constants, so a user cannot recompose them.

---

## 3. Gap matrix against DSH

Legend: **Missing** = no equivalent; **Partial** = pieces exist but the contract
is not closed; **Built** = present and exercised.

| DSH capability | Eclipse today | Gap | Priority |
|---|---|---|---|
| Context/service repository (`ctx.<key>`) | `AgenticService` attributes + module singletons (`deps.py:26`) | **Missing** | P0 |
| `inject` coeffects + activate/deactivate on provider change | Imports and boot order; no dependency gating | **Missing** | P0 |
| Revertible effects + automatic disposal | Plugin `_unload` unregisters tools/events/hooks; no generic effect ownership | **Partial** | P0 |
| Nested contexts / fibers / groups / isolate | None | **Missing** | P0 |
| Typed event modes (`emit`/`waterfall`/`serial`/`bail`/`parallel`) | `EventBus` + `HookRegistry` ordered handlers | **Partial** | P1 |
| Scoped registries (`agent.ctx`) | Agent allowlists; one global `ToolRegistry`; `set_active_context` | **Missing** | P1 |
| Profiles + ordered bundles + patch dialect + `--dump-config` | None; boot imports a fixed set | **Missing** | P1 |
| Per-session agent presets (Cordis child tree) | DB preset: prompt/allowlist/steps only | **Partial** | P2 |
| Hot recomposition / HMR | Plugin enable/disable at runtime; no row reconcile or config HMR | **Partial** | P2 |
| Replaceable agent loop | `build_graph` hardcoded | **Missing** | P2 |
| Replaceable prompt assembly | Static prompts + surface overlays | **Partial** | P2 |
| Replaceable model adapter | `model_runtime` registry | **Built** | - |
| Capability seams (Definition/Provider/Consumer) | `model_runtime`, MCP, retrieval; tools/session less so | **Partial** | P2 |
| Plugin contributions for agents/skills/providers/workflows | Declared in manifest; only descriptors, not mounted | **Partial** | P3 |
| Runtime inspection for agents (`cordis_inspect`) | None | **Missing** | P3 |
| Ephemeral dynamic packages (define/run/stop) | Persistent install only; sandbox exists | **Missing** | P4 |
| Plugin marketplace + install UX | Registry client + plugins screen | **Built** (weaker discovery) | P4 |
| First-party self-evolution | RSL: observation, candidates, vetoes, lineage, gates | **Partial** (subjects limited) | P5 |
| Asset-level evolution (prompt/skill/policy/hook/preset/composition) | Surface snapshot covers prompt/skills/policy partially; no plugin/preset/composition | **Partial** | P5 |
| Crash-resumable evolution journal | Candidate claim/lease + attempts | **Partial** (not per-asset intent) | P5 |
| Post-apply regression watch + auto-rollback | Drift watch; skill canary; no generic applied-asset watch | **Partial** | P5 |
| One operator surface for all evolution | `/evolution/*` + `/learning/*` + plugin routes | **Partial** | P6 |
| Signed/attested plugin supply chain | Ed25519/HMAC + SHA-256 + SBOM | **Built (stronger than DSH)** | keep |
| Permissioned plugin capabilities | Closed vocabulary + operator elevation | **Built (stronger than DSH)** | keep |
| Out-of-process untrusted plugin | `plugins/sandbox.py` + runner | **Built (stronger than DSH)** | keep |

### Defect register (measured)

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| D1 | Skills, agents, research/data providers, workflows are **descriptor-only** | `plugins/registry.py:115-131` records them; no call into `skills/`, `agents.py`, retrieval or `workflows/` | **Closed for the executable surfaces (Phase 4):** agents/skills/prompts mount live; providers/workflows/models/UI register in the kernel contribution catalog with exact unload |
| D2 | Workflow contributions are also **unvalidated** | `manifest.all_contribution_names()` omits workflows; `registry._impl_kind` has no mapping | **Closed:** workflows are in the name index and mapping; execution remains open |
| D3 | 12 of 14 hook points are **dead vocabulary** | `plugins/hooks.py:25-33`; only `before_tool_call`/`after_tool_call` are called (`tool_turn.py:299,448`) | docs claim lifecycle coverage the runtime does not have |
| D4 | UI contribution `entry` is parsed then discarded | `manifest.py:170`; `registry.py:105-106` | only a panel view spec is possible; no page/sidebar/settings slot |
| D5 | Commands are not on the app command surface | `manager.commands`; only `plugin_run_command` / HTTP | a plugin command cannot be typed in chat |
| D6 | Plugin workers are ad-hoc asyncio tasks | `manager.py:605-632` | no Fabric durability/retry/restart |
| D7 | ~~No hot reload / no module isolation~~ (fixed) | `loader.evict_modules` + `module_names_under` + `PluginManager.reload` and the opt-in `plugins/hot_reload.py` watcher; `_unload` evicts every module under the plugin root so disable/re-enable re-executes source and host modules are untouched; pinned/bundled/signed plugins are refused | OS-level isolation for untrusted plugins remains the existing sandbox (separate process) |
| D8 | `set_config` updates only `ctx.config` | `manager.py:777-781` | HTTP allowlist/storage/auth keep stale values |
| D9 | `AGENT_PRESETS` is a static dict | `agents.py:1148`; `graph.resolve_agent:188` | no service owns the agent catalog; plugins/presets live on different paths |
| D10 | One global `ToolRegistry` with `set_active_context` | `service.py:88`; `tools.py:582` | allowlist filtering, not scoped registration; concurrent runs share state |
| D11 | Prompt assembly is constants + static segments | `prompts.py:12`; `prompt_segments.py:24-41` | no ordered, replaceable section registry |
| D12 | `graph.py` (~135 KB) owns loop/routing/budget/repair/effects | `graph.build_graph:463` | no `AgentLoop` seam; replacement means editing the monolith |
| D13 | Model adapters are an `if kind ==` chain | `models.py:422-553`; `model_runtime/` is a second path | no contribution type; two provider truths |
| D14 | Session store is a single SQLite class | `persistence.py:348` | no `SessionPersistence` seam |
| D15 | No service container / inject / effects / scopes | `deps.py:26` module singleton; `PluginContext` withholds registries | plugins cannot replace or depend on host services |
| D16 | RSL cannot mutate agents/graph/plugins/UI composition | frozen subjects are code/surface/skill; no composition asset | self-evolution cannot improve the harness shape |
| D17 | RSL denylist misses core authority files | `self_improvement/security.py:18-41` | only operator promotion protects `agents.py`, `graph.py`, `tools.py`, `plugins/**`, `code/sandbox.py` |
| D18 | Strongest search paths are dark in production | `boot.py` builds `RSLLoop` without `climb_runner`; `surface_lab` off; `SessionWatcher` 0 | capability exists, is not exercised |
| D19 | No read-only live API catalog for agents | no equivalent of `cordis_inspect` | authors work from docs, not the running tree |
| D20 | Composition is implicit in boot imports | `service.py:91-661` explicit registration calls | no dump, diff, profile, bundle or rollback before boot |

The first slice deliberately targets D9-D20 foundations; D1-D8 become executable
contributions in Phase 4.

---

## 4. Target architecture

```
Next.js front door
  |  validates input, resolves LLM config, proxies
  v
AgenticService (transport, auth, tenancy)          unchanged
  |
  +--> Kernel (harness/kernel)                     NEW
  |      Context / Service / inject / effect / scope
  |      Composition (profiles, bundles, patches, presets)
  |      Supervisor (reconcile, HMR, rollback, health)
  |      Inspection (read-only catalog)
  |      Built-in services:
  |        tools      -> scoped ToolRegistry
  |        llm        -> model_runtime registry
  |        sessions   -> persistence
  |        prompt     -> ordered prompt sections
  |        agents     -> agent catalog (built-ins + presets + plugins)
  |        skills     -> skill catalog
  |        policy     -> surface/autonomy/effect authorizer
  |        retrieval  -> existing retrieval stack
  |        jobs       -> fabric/jobs
  |        memory     -> eclipse_memory
  |        artifacts  -> artifact center
  |        events     -> EventBus + typed dispatch
  |
  +--> Evolution Engine (harness/evolution)        NEW
  |      AssetRegistry (declared evolvable kinds)
  |      Observer -> Proposer -> Admission -> Evaluator
  |              -> Selector -> Land -> Watch
  |      Evidence ledger (reuses EvaluationRun/EvolutionCandidate)
  |
  +--> Existing subsystems                          unchanged
         graph.py, tools.py, model_runtime, evaluation,
         plugins, tasks, fabric, event_system, security
```

The kernel is **thin**. It owns lifecycle, composition and service lookup; it
does not own agent logic, finance logic or persistence. The existing subsystems
become providers behind service keys; their public functions stay callable
during migration.

### 4.1 Kernel contracts

```python
# harness/kernel/context.py
class Plugin(Protocol):
    name: str
    inject: tuple[str, ...]                  # required coeffects
    optional_inject: tuple[str, ...]          # optional coeffects
    Config: type[BaseModel] | None
    async def apply(self, ctx: "Context", config: Any) -> None: ...

class Context:
    # services
    def provide(self, key: str, value: Any, *, replace: bool = False) -> Disposer
    def get(self, key: str, default: Any = None) -> Any
    def require(self, key: str) -> Any            # raises ServiceUnavailable
    def has(self, key: str) -> bool
    # effects (every registration returns ownership)
    def effect(self, factory: Callable[[], Disposer]) -> Disposer
    def on(self, event: str, handler, *, mode: str = "emit", prepend: bool = False) -> Disposer
    def emit(self, event: str, *args) -> None
    async def waterfall(self, event: str, *args, fallback) -> Any
    async def serial(self, event: str, *args) -> Any
    async def bail(self, event: str, *args) -> Any
    async def parallel(self, event: str, *args) -> None
    # composition
    def plugin(self, plugin: Plugin | str, config: Any = None) -> "Fiber"
    def group(self, *, isolate: tuple[str, ...] = ()) -> "Context"
    def scope(self, key: Any) -> "Context"        # per-agent registration world
    async def dispose(self) -> None
```

Guarantees:

- A registration belongs to exactly one context; disposing that context
  unwinds it in reverse order (async disposers run concurrently inside one
  effect, as in Cordis).
- A plugin with unmet required services stays `PENDING`; when they appear it
  loads. When a required provider disappears, its dependents dispose first.
- A scope inherits ancestor services and registrations; ancestor listeners may
  observe descendant events; descendant registrations are not visible upward.
- No raw mutable registry is exposed to untrusted code; plugins receive a
  permission-checked facade from `plugins/runtime.py`.

### 4.2 Composition contract

Row (same vocabulary as DSH, deliberately):

```yaml
- id: prompt                  # stable, addressable
  name: harness.services.prompt:PromptService
  config: { maxSections: 40 }
  disabled: false
  isolate: []                 # service keys isolated in this group
  group: false
```

Patch file (closed vocabulary, no code evaluation):

```yaml
- insert:
    - id: my-tool
      name: mypkg.plugin:MyTool
      config: { greeting: hello }
- id: prompt
  config: { maxSections: 60 }      # replaces the whole config
- id: legacy-tool
  disabled: true
- remove: old-row
```

Expressions are declarative only:

```yaml
disabled: { $env: "ECLIPSE_DISABLE_MY_TOOL", $default: false }
config:
  model: { $ref: "settings.llm.model", $default: "deepseek-chat" }
  enabled: { $eq: [{ $env: "MODE" }, "dev"] }
```

Layers, low to high: installation bundles -> profile bundle list -> profile
patch -> home patch -> invocation `--patch` overlays -> per-session preset.

- Applying a patch to an unknown id yields a diagnostic (warning for optional,
  error for required).
- `--dump-config` prints the effective rows with source labels.
- `--dump-config-schema` prints each plugin's JSON Schema (trusted profiles only).
- The supervisor diffs desired vs live and applies: add, update (dispose+remount),
  disable, remove. On failure it restores the previous generation and reports
  the failing row; it never leaves a half-mounted tree.

### 4.3 Profile and bundle contract

`~/.black-eclipse/profiles/<name>/profile.json`:

```json
{
  "name": "my-research",
  "from": "web",
  "bundles": ["base", "web-app", "my-bundle"],
  "dependencies": { "black-eclipse-plugin-sec": "1.2.0" },
  "patch": "cordis.patch.yaml"
}
```

A plugin package declares an optional bundle in `plugin.yaml`:

```yaml
bundle:
  patch: cordis.patch.yaml
  platforms: [win32, linux, darwin]
profile:
  bundles: ["my-bundle"]
```

Bundle install reuses the existing signed plugin install; the new part is that
the patch rows are composed and reconciled rather than imported by hand.
Per-session presets are the same row list with `config.plugins`, stored in the
existing versioned `AgentPreset` table under a new `compositionJson` column
(append-only, hash-pinned).

### 4.4 Plugin manifest v2 (additive)

Keep the existing closed contract; add optional declarations:

```yaml
provides:
  - key: research.provider
    version: 1
requires:
  - key: tools
    optional: false
evolvable:
  - kind: prompt_section
    names: ["research.guidance"]
  - kind: skill
    names: ["learned.*"]
```

`provides`/`requires` are cross-checked against the runtime object, exactly as
`contributes` is today. A plugin that declares a service must actually provide
it; a missing required service is a load failure, not a silent import error.
Contribution permissions are per contribution (tool effect already is; skills,
prompt sections and hooks gain a `permission` field).

### 4.5 Evolution engine contract

One store, one vocabulary, one evidence format.

```python
AssetKind = Literal[
    "prompt_section", "skill", "tool_policy", "hook",
    "agent_preset", "composition_row", "plugin_bundle",
    "memory_policy", "model_route",
]

Mutation = {
  "op": "add" | "patch" | "retire" | "attach" | "detach" | "tune",
  "kind": AssetKind,
  "target": str,                 # stable asset id
  "content": dict,               # schema-validated per kind
  "rationale": str,
  "expected_effect": str,
}

Candidate = {
  "id", "parent_id", "kind", "target", "mutations", "digest",
  "status", "evidence_refs", "budget", "created_at",
}
# status: inert -> proposed -> admitted -> evaluating
#         -> measured_ok | measured_bad | unmeasured | refused
#         -> canaried -> applied -> rolled_back | retired
```

Pipeline and gates:

```
observe (run outcome envelope, mistakes, recovery, drift, user correction)
  -> cluster/dedupe by stable signature (min recurrence, distinct sessions)
diagnose (root-cause class; evidence refs)
propose (deterministic templates + opt-in LLM; closed mutation schema)
admit (schema + permission + sandbox apply + diff + frozen-policy check)
evaluate (frozen evaluator; baseline vs candidate; paired trials; split)
select (utility + critical-regression veto + significance; Pareto/novelty)
land (atomic kernel activation; TTL; caps; operator gate for effects)
watch (post-apply window; same failure key recurs => auto-rollback)
```

Rules carried over from the existing RSL, unchanged:

- `UNMEASURED` never passes.
- A critical regression always vetoes.
- A candidate touching a frozen path is refused, never skipped.
- The judge, split, policy and budgets are not modifiable by a candidate.
- Code candidates are evaluated in a disposable worktree and landed only by the
  existing operator promotion path.
- Every external action has a durable intent and idempotency key before launch;
  resume reconciles exactly once.
- One writer (the conductor) per run id; claims are atomic leases.

New rules for asset kinds:

- **Prompt section / skill / memory policy**: declarative, read-only; eligible
  for supervised auto-apply with TTL and caps (same class as learned skills).
- **Tool policy / hook / preset / composition row**: declarative but
  behavior-changing; auto-apply only in `shadow` or with operator approval;
  never widens tools.
- **Plugin bundle**: code; admission in the sandbox, evaluation in a capsule,
  landing only by signed install + operator approval.
- **Model route**: never auto-applied (live identity is resolved by the front
  door; keep `SURFACE_LIVE_UNSUPPORTED`).

### 4.6 Runtime inspection contract

Read-only tool `harness_inspect`, operator/agent scoped:

- `services`: live keys, versions, provider plugin, methods (from a declared
  catalog, never by reflecting private attributes).
- `events`: name, mode, signature, producers/consumers.
- `config`: effective row list, source layer, schema, current value, status.
- `plugins`: installed/enabled/health/contributions/permissions.
- `presets`: available compositions and their rows.
- `assets`: evolvable assets and their current versions.
- `doctor`: why a row is inactive, which service is missing, which permission
  was denied.

No method invocation, no code execution, no secret values. This is the
"author against the live API" surface DSH has and Eclipse lacks.

### 4.7 DSH concept to Eclipse implementation

| DSH (Cordis) | Eclipse target | Existing asset |
|---|---|---|
| `Context` / `ctx.<key>` | `kernel.Context` with `provide/get/require` | new (`kernel/context.py`) |
| `inject` coeffect | `Plugin.inject`, pending fiber, dispose on provider loss | new |
| `ctx.effect()` | `Context.effect/track`, `Fiber` effects | new |
| Fiber state machine | `FiberState` + idempotent `dispose` | new |
| `ctx.on` / five dispatch modes | `EventBus` with `on/emit/waterfall/serial/bail/parallel` | new; existing `EventBus` stays the durable transport |
| `ctx.plugin` / group / isolate | `Context.plugin/group`, `_isolated` services | new |
| Scope / `agent.ctx` | `Context.scope` + `ScopedRegistry` | new |
| Profile + bundle + patch | `profile.load_profile`, `composition.compose` | new |
| Preset (`config.plugins`) | `AgentPreset.compositionJson` (proposed) | `agent_presets.py` |
| HMR / recomposition | `Supervisor.apply` + a future watcher | new |
| `cordis_inspect` | `harness_inspect` read-only tool | new (Phase 4) |
| Dynamic packages | sandboxed, session-scoped, versioned packages | `plugins/sandbox.py` |
| Plugin Manager | install/enable/disable + bundle selection | `plugins/manager.py` |
| Self-evolution plugins | `harness/evolution/` one engine over declared assets | RSL + `evaluation/` |

This mapping is the implementation contract. If a later phase introduces a
concept that does not fit a row here, it needs an ADR before code.

---


## 5. Security model (non-negotiable)

Every new surface inherits the existing floor:

1. **Package trust**: signature verification (Ed25519/HMAC), SHA-256 integrity,
   path-escape guard, operator review gate. A bundle is a plugin package; it
   carries the same checks.
2. **Runtime trust**: `community`/`untrusted` Python runs in the existing
   sandbox child; trusted code runs in-process. Dynamic packages are sandboxed
   and TTL-bound; they never become persistent without promotion to a signed
   bundle.
3. **Capability permissions**: plugins declare a closed permission set; the host
   grants exactly that set. Service access is a permission
   (`service.tools.read`, `service.llm.invoke`, ...), not raw `ctx`.
4. **Effects**: any contributed tool with a non-read permission routes through
   the existing effect authorizer. Composition rows that mount effectful plugins
   require operator elevation.
5. **Data boundaries**: secrets are per-plugin, encrypted, never in config,
   model context or logs. The controlled HTTP client remains the only network
   path, with SSRF and host allowlists.
6. **Expression safety**: no `eval`, no `!!js`, no imports in composition.
   `$env`/`$ref`/boolean operators only; `$ref` resolves against a declared,
   non-secret settings namespace.
7. **Finance invariants**: the kernel cannot bypass `finance_guardrails`,
   `order_submit` mandates, `UNAVAILABLE` honesty, or the operator approval
   chain. A composition row that would remove a guardrail is refused.
8. **Authority freeze**: the kernel, composition parser, permission checker,
   signing, sandbox, evaluation judge, datasets and policy are frozen targets
   for the evolution engine. Extend `RSL_DENY_GLOBS` to cover
   `harness/kernel/**`, `harness/evolution/**`, `agents.py`, `graph.py`,
   `tools.py`, `schemas.py`, `prompt_segments.py`, `plugins/**` and
   `code/sandbox.py`.

Threat model additions: a malicious bundle can mount a row that shadows a
service key. Mitigation: service replacement requires `service.replace`
permission + operator approval; same-key replacement by a lower-trust plugin is
refused; every replacement is recorded with the previous provider.

---

## 6. Phased plan

Effort assumes one senior engineer; dates are not promised.

### Phase 0 - Contracts and inventory (0.5 day) - this document

Deliverables: this plan, the companion analysis, ADR-H2xx draft, updated
NAVIGATION/CHANGELOG.

Exit: a reviewer can trace every claim to a file; the gap matrix is accepted.

### Phase 1 - Kernel core (3-4 days)

Files:

```
python/harness/kernel/__init__.py
python/harness/kernel/errors.py        # named codes
python/harness/kernel/events.py        # typed dispatcher (5 modes)
python/harness/kernel/context.py       # Context, effects, services, scopes
python/harness/kernel/fiber.py         # state machine, ownership, disposal
python/harness/kernel/plugin.py        # Plugin protocol, loader, config schema
python/harness/kernel/supervisor.py    # desired-vs-live reconcile, rollback
python/harness/kernel/registry.py      # service-key catalog + inspection data
python/tests/test_kernel_context.py
python/tests/test_kernel_events.py
python/tests/test_kernel_scope.py
python/tests/test_kernel_supervisor.py
```

Exit criteria:

- provide/require/inject gating; provider removal disposes dependents and
  reloads them on return;
- effect disposal is reverse-order and idempotent; a failing disposer is
  reported, never swallowed;
- scopes inherit down, route up, isolate across siblings, and dispose cleanly;
- supervisor applies add/update/disable/remove and restores the previous
  generation on failure;
- no behavior change to the running app (not wired yet).

### Phase 2 - Composition, profiles, bundles, presets (4-5 days)

Files:

```
python/harness/kernel/composition.py   # rows, patches, layers, expressions
python/harness/kernel/profile.py       # profiles, bundles, resolution
python/harness/kernel/dump.py          # --dump-config, --dump-schema, diff
python/harness/kernel/cli.py           # eclipse kernel ...
python/tests/test_kernel_composition.py
python/tests/test_kernel_profiles.py
python/tests/test_kernel_dump.py
```

Exit criteria:

- the same row list reproduces today's boot composition for the `web` profile;
- override/insert/disable/remove and layer precedence are tested;
- unknown ids and invalid expressions fail with source-labelled diagnostics;
- bundle resolution checks signatures, platform, version and cycles;
- `--dump-config` is deterministic and diffable;
- feature flag `ECLIPSE_KERNEL=off|shadow|on`; default `off`.

**Status - implemented 2026-11-21 (profile rows, not core replacement).**
`load_profile` + `Supervisor` + `eclipse kernel dump|diff|doctor` are live.
`kernel/profile_boot.py` now loads the active profile (`ECLIPSE_PROFILE`,
`ECLIPSE_PROFILES_DIR`) after plugin startup, mounts its ordered rows through
the supervisor, reports `absent`/`disabled` as no-ops, restores the previous
generation on a failed row, and unmounts on shutdown. `test_kernel_profile_boot.py`
proves mount/unmount, dump parity, failure rollback and the absent/off no-op.
Core service rows are replaceable through `builtin:` rows (`tools`, `prompt`, `agents`) with exact undo.

Bundle hardening is live in `kernel/profile.py` (`BundlePolicy`,
`bundle_signature_payload`, `bundle_policy_from_env`): declared platforms and
kernel ranges are enforced, required bundles are checked (unknown requirement or
dependency cycle skips every member), and Ed25519 signatures over the declaration
plus patch digests are verified. `ECLIPSE_KERNEL_TRUSTED_KEYS` (hex public keys)
configures trust; `ECLIPSE_KERNEL_REQUIRE_SIGNATURE=1` makes an unsigned bundle a
skip. A present but invalid signature is always refused, never downgraded.

`ECLIPSE_KERNEL=shadow` loads and validates composition, services and evolution
proposals but changes nothing live: `KernelRuntime.active` is false, graph/surface/
plugin consumers take the original path, profile boot reports `status: shadow`,
and evolution apply returns `KERNEL_SHADOW_READONLY`. Hot reload is opt-in via
`ECLIPSE_KERNEL_WATCH=1` (`ECLIPSE_KERNEL_WATCH_INTERVAL`, seconds): a digest poll
reloads the profile, a broken edit keeps the previous generation, and a failed
mount rolls back to it (`kernel/watch.py`, `ProfileBoot.reload`). Tests
`test_kernel_bundles.py`, `test_kernel_shadow.py`, `test_kernel_watch.py`.

### Phase 3 - Service seams and scoped registries (5-7 days)

Files:

```
python/harness/kernel/services/tools.py     # scoped view over ToolRegistry
python/harness/kernel/services/prompt.py    # ordered scoped sections
python/harness/kernel/services/agents.py    # built-ins + presets + local agents
python/harness/kernel/services/runtime.py   # ECLIPSE_KERNEL gate + graph helpers
python/tests/test_kernel_services_tools.py
python/tests/test_kernel_services_prompt.py
python/tests/test_kernel_services_agents.py
python/tests/test_kernel_runtime_wiring.py
```

Exit criteria:

- a tool registered in one agent scope is invisible to another; disposal removes
  it; global tools remain visible;
- prompt sections have stable orders and a section can be added per agent
  without touching `prompts.py`;
- the built-in agents resolve through the catalog; DB presets resolve through
  the same service; local agents can be registered and disposed;
- `graph.resolve_agent` delegates to the service under the flag; byte-identical
  prompts for every built-in agent across settings/autonomy are proven;
- concurrency test: 20 parallel scopes do not leak tools.

**Status - implemented 2026-11-21.** All five criteria are covered by
`test_kernel_services_*` and `test_kernel_runtime_wiring.py`; `test_graph.py` +
`test_graph_agent_node.py` pass with `ECLIPSE_KERNEL=on` (64 tests) and off.
`sessions` (read-only over `AgentMemoryStore`) and `policy` (over the ONE
`code.policy` authority) are now kernel services, consumed by a plugin-row
injection test and by surface autonomy narrowing; `skills` was already live.

### Phase 4 - Plugin platform v2 (4-5 days)

- Executable contributions: agent, skill, prompt section, research/data
  provider, workflow, model adapter, UI slot.
- Capability-scoped service access for plugins; per-contribution permissions.
- Hot enable/disable through the supervisor; restart not required for
  config-only rows.
- `harness_inspect` tool and `/kernel/inspect` route.
- Manifest v2 `provides`/`requires`/`evolvable`.
- Tests: a bundled plugin declares and runs a custom agent; a denied service is
  refused; a plugin reload unwinds all contributions; inspection cannot read a
  secret.

**Status - implemented 2026-11-21 for all declarative executable kinds.** The
manifest gained `provides`/`requires`, `prompts` and `models`; the SDK gained
`@prompt_section`, `@model_provider` and `provided_services()`;
`ContributionRegistry` mounts agents/prompts/skills through the kernel services
and registers research/data providers, workflows, models and UI handlers in the
kernel contribution catalog, behind `ECLIPSE_KERNEL`. Every kind requires its
permission (`agents.contribute`, `skills.contribute`, `prompts.contribute`,
`models.contribute`, `workflow.execute`, `research.execute`, `ui`), is
cross-checked against the manifest, and stores an exact undo per plugin.
`harness_inspect` is registered read-only on every boot (it reports
`enabled:false` when the kernel is off) and includes the contribution catalog.
`bundled/kernel-demo` is the regression fixture; 13 tests cover mount,
disable/unwind, permission denial, provides mismatch, requires missing,
inspection and the extra catalog. The catalog is bound at boot to the retrieval
provider registry, search registry, workflow store, model provider registry and
UI dynamic panels (`kernel/services/wiring.py`); unload removes the same entries.
Still open: the planner chooses only descriptors it is given, kernel UI slot
rendering beyond dynamic panels, and live composition rows.

### Phase 5 - Dynamic packages (4-6 days, optional)

- Session-scoped, versioned immutable packages; define/run/update/stop/undefine.
- Execution in the existing sandbox child; no network by default; TTL; size and
  call budgets; approval for UI halves.
- Never persistent unless promoted to a signed bundle.
- Tests: version pinning, stop unwinds, crash does not affect the host, a
  denied capability returns a teaching error.

**Status - implemented 2026-11-21.** `python/harness/dynamic/` adds
`DynamicPackages` (`define`/`run`/`update`/`stop`/`undefine`, session ownership,
immutable package digests, TTL, call/wall-clock/package caps) and a child
`runner.py` launched through `code.sandbox.spawn` (scrubbed env, process-tree
kill, output/memory limits). `run`/`update` require explicit approval; the
operator API is `/dynamic*`. `stop`/`undefine` kill the process tree and remove
the package directory. 7 dynamic-package tests cover lifecycle, session
isolation, budgets, TTL, cap/empty source and the approval refusal. Not yet: a
kernel UI surface for dynamic packages, signed promotion, and model-facing
creation (deliberately absent).

### Phase 6 - Evolution engine v2 (7-10 days)

- `harness/evolution/` with `assets.py`, `observer.py`, `diagnose.py`,
  `propose.py`, `admit.py`, `evaluate.py`, `select.py`, `land.py`, `watch.py`,
  `journal.py`, `api.py`.
- Reuse `EvolutionCandidate`/`EvaluationRun` (extend, do not duplicate); migrate
  existing RSL candidates behind the same façade.
- Asset adapters for prompt section, skill, tool policy, hook, preset,
  composition row, plugin bundle, memory policy.
- Crash-resumable journal with intent/idempotency; one writer.
- Post-apply regression watch with automatic rollback; TTL; caps; shadow mode.
- Operator API: `GET /evolution/v2/status|candidates|assets`, `POST propose/
  admit/evaluate/apply/rollback/pause`.
- Tests: each kind admits and evaluates; a neutral change is rejected; a
  regression is rolled back; a killed process resumes without double-apply; a
  frozen target is refused.

**Status - contract + evaluator + observer/proposer slice implemented 2026-11-21.**
`harness/evolution/` (`assets.py`, `adapters.py`, `service.py`, `evaluator.py`,
`journal.py`, `observer.py`, `proposer.py`, `pipeline.py`) defines the closed
asset/mutation vocabulary, candidate state machine, digest, propose -> admit ->
apply -> rollback, a hash-chained intent/snapshot journal with crash recovery,
TTL/active caps, post-apply regression rollback, the existing `evaluate_candidate`
gate wired to `measured_ok` / `measured_bad` / `unmeasured`, and a recurrence
observer -> bounded deterministic proposer -> candidate pipeline with
`observe`/`propose`/`shadow`/`apply` modes. All asset adapters are implemented
(prompt, agent, skill, hook, memory policy, tool policy, model route, composition
row; plugin bundle is operator-only); 46 tests cover exact rollback,
accepted/rejected/unmeasured evaluation, journal replay without a second apply,
tamper detection, regression, TTL, recurrence, bounded proposals, shadow refusal,
every adapter's apply/undo, live consumer narrowing/routing/mounting, and the HTTP
evaluator (baseline required, no fake acceptance).

### Phase 7 - Operator surface (4-5 days)

- One screen: assets, candidates, evidence, diff, apply/rollback, budget, kill
  switch. Reuse the frosted-glass system.
- Plugin page shows composition rows, source layer, inactive reason, and the
  inspection catalog.
- Profile editor: create from template, toggle bundles, edit patch, dump/diff.
- Tests: UI contract tests, no raw HTML in plugin panels, accessibility.

### Phase 8 - Hardening and migration (5 days)

- Compatibility: old manifests, old presets, old evolution rows.
- Performance: mount/unmount p95, 100-row recomposition, memory ceiling.
- Security review: replacement attacks, expression abuse, sandbox escape,
  permission confusion, secret leakage.
- Docs: ARCHITECTURE, plugin platform, NAVIGATION, DECISIONS, runbooks.
- Remove dead code only after a deprecation window.

**Total:** roughly 6-8 engineer-weeks. Phase 1-2 is the smallest useful slice and
can ship behind a flag without touching runtime behavior.

---

## 7. How this is better than DSH, specifically

| Dimension | DSH | Eclipse target |
|---|---|---|
| Composition | profiles/bundles/patches/presets | same, with `--dump/diff/schema` and cycle diagnostics |
| Kernel | Cordis (effects + coeffects) | same semantics in Python, smaller surface |
| Per-agent scope | scopes, presets | same, plus allowlist/policy intersection and finance guardrails |
| Plugin trust | in-process, unsigned, no permissions | signed packages, closed permissions, sandbox for untrusted |
| Plugin contributions | full service/UI composition | full, with per-contribution permissions |
| Evolution | four incompatible plugins, no shared evidence | one engine, one asset contract, one ledger |
| Evidence | single replay or fixed-replay engineering proof | frozen evaluator + paired trials + held-out split + significance + replay gate |
| Crash safety | controller journal (one plugin) | journal-first for every asset, idempotent, resume |
| Regression | per-plugin watch | global post-apply watch with auto-rollback and drift |
| Honesty | UNMEASURED labels in the best plugins | UNMEASURED is a first-class, non-passing status everywhere |
| Operator surface | one page per plugin | one surface for all assets |
| Finance | none | guardrails, mandates, UNAVAILABLE, no fabricated data |

**The differentiator is not "more plugins".** It is that Eclipse can *prove* a
self-change improved the harness without weakening its safety floor, because it
already owns the evaluator, the datasets, the lineage and the approvals. DSH's
architecture makes change easy; Eclipse's makes change *accountable*.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Two registries during migration (kernel + globals) | One adapter per service; golden tests prove parity; flag off by default; delete the old path only after two releases |
| Composition becomes an expert-only surface | Templates, profile editor, `doctor`, source-labelled diagnostics, diff |
| A plugin shadows a service key | `service.replace` permission + operator approval + previous-provider audit |
| Hot reload corrupts in-flight runs | Reconcile only at safe boundaries; pin a generation per run; defer while runs are active |
| Evolution auto-applies a harmful change | `UNMEASURED` cannot pass; critical veto; caps/TTL; shadow first; regression watch |
| Sandbox escape via dynamic packages | Separate process, no network, TTL, capability facade, never persistent without signature |
| Performance regression from scoped lookups | Cache resolved scopes per run; profile with benchmarks; budget on row count |
| Documentation drift | Each phase updates the owning doc; ADR per contract change; CHANGELOG entry per change |
| Scope creep into a rewrite | The kernel is additive; `graph.py` is adapted, not replaced, until parity is proven |

---

## 9. Acceptance metrics

Tracked on the existing evaluation/scorecard surface, not invented:

- **Composition parity**: effective rows and rendered prompt byte-identical to
  today for `web`, `headless` and `code` profiles.
- **Unload cleanliness**: zero leaked tools/hooks/listeners after 100
  enable/disable cycles (registry counts return to baseline).
- **Scope isolation**: 0 cross-agent leaks in a 20-run concurrency test.
- **Composition failure handling**: 100% of injected failures restore the
  previous generation; 0 half-mounted trees.
- **Evolution precision**: share of applied asset changes that pass the gate and
  show no critical regression (target 1.0 on the fixed suite).
- **Regression rate**: landed changes that later trip a gate (target 0).
- **Time to repair**: incident -> verified candidate (trend down).
- **Unauthorized action rate**: 0 in the security suite.
- **Cost per verified improvement**: measured, labelled `UNMEASURED` until the
  sample is sufficient.

---

## 10. First slice (kernel, services, profile boot)

Phase 1 + Phase 2 + Phase 3 + Phase 4 first slices, additive, flag-reserved:

1. `harness/kernel/{errors,events,scope,fiber,plugin,context,composition,profile,supervisor,dump,cli}.py` - done.
2. `harness/kernel/cli.py` exposing `eclipse kernel dump|diff|doctor` - done, plus the `kernel` group in `harness/cli.py`.
3. Tests `python/tests/test_kernel_{context,events,composition,profiles,supervisor,cli}.py` - 42 passing.
4. `harness/kernel/services/{tools,prompt,agents,runtime}.py` + four service suites - 41 passing (83 kernel total).
5. `service.py` configures the runtime; `graph.py` uses the per-run view and the catalog/prompt helpers **only when `ECLIPSE_KERNEL=on`**. Default off is byte-identical.
6. `boot.py` applies `kernel/profile_boot.py` after plugin startup; an absent profile is a no-op, a failed row restores the previous generation, and shutdown unmounts. `test_graph.py` + `test_graph_agent_node.py` pass with the flag on (64) and off; the 20-scope concurrency test proves no tool leak; `test_kernel_profile_boot.py` proves dump parity and rollback.

7. Kernel UI contributions render through `kernel/services/ui.py` and `/kernel/ui/slots*`; core rows replace `tools`/`prompt`/`agents` through `builtin:` composition with exact undo; `POST /evolution/assets/{id}/evaluate` is the measured landing path.

Next slice: the default-composition rollout below (field `shadow` telemetry + explicit approval).


---

## 11. Open decisions (resolve by running, not reading)

1. **Expression vocabulary**: is `$env/$ref/$eq/$and/$or/$not` enough for real
   variants, or do bundles need a small plugin-provided function registry?
2. **Preset storage**: extend `AgentPreset` with `compositionJson`, or add a
   `SessionComposition` table? The former is smaller; the latter separates
   prompt/allowlist from row trees.
3. **Hot reload boundary**: reconcile on every patch save, or only at turn
   boundaries with a "pending generation" banner? Start with turn boundaries.
4. **Service keys**: one flat namespace (`tools`, `llm`) or dotted
   (`core.tools`)? Flat matches DSH and is easier to read; dotted leaves room
   for plugin namespaces.
5. **Evolution asset scope**: should `plugin_bundle` be evolvable in v1, or only
   declarative assets? Recommendation: declarative first; bundles after the
   sandbox capsule is proven.
6. **Tier B defaults**: keep `HARNESS_SELF_MODIFICATION=enable` for learned
   skills, but make asset auto-apply default `shadow` until the regression watch
   has a measured track record.

---

## 12. ADR proposals

- **ADR-H2xx - Kernel and composition model.** Cordis-class context, effects,
  coeffects, scopes; profiles/bundles/patches; no code evaluation.
- **ADR-H2xx - Capability permissions for services and contributions.** Every
  service access and contribution is permissioned; replacement requires
  operator elevation.
- **ADR-H2xx - One evolution contract.** A single asset/candidate/evidence
  vocabulary and one operator surface; `MEASURED` requires the frozen evaluator,
  paired trials, a held-out split and significance.
- **ADR-H2xx - Dynamic packages are ephemeral and sandboxed.** Never persistent
  without promotion to a signed bundle; no network by default; TTL and budgets.
- **ADR-H2xx - Kernel and evolution are frozen targets.** Extend the RSL
  denylist to the new authority set.

---

## Default-composition readiness

**Verdict: flipped. The kernel is now the default boot composition.** The
rollout gates were met by evidence, not assertion:

- the enabled empty-profile path is proven byte-identical for every built-in
  agent's surface and prompt
  (`test_resolve_surface_is_byte_identical_through_the_runtime`);
- `test_graph.py`, `test_graph_agent_node.py` and `test_graph_service.py` pass
  with the kernel on (72) and off, and the 20-scope test proves no tool leak;
- core services (`tools`, `prompt`, `agents`) are replaceable through `builtin:`
  rows with exact undo; `sessions` and `policy` are read-only providers over the
  existing store/authority; `shadow` remains available for validation;
- plugin-code hot reload and in-process module isolation are live; untrusted
  plugins keep the separate-process sandbox;
- the **full Python suite is green with the kernel on**: **3,340 passed, 19
  skipped, 0 failed** (1,172 files), and the default-baseline run (before the
  flip) was **3,400 passed, 6 skipped**; `vitest` is **5,184 passed / 1 skipped**.

Rollback switch: `ECLIPSE_KERNEL=off` restores the original paths byte-for-byte.
`ECLIPSE_KERNEL=shadow` keeps validation without live behavior changes.

What remains is operational monitoring, not an engineering gate: watch
`/kernel/inspect` diagnostics and profile status in the field, and keep the
`shadow`/`off` switches available for a rollback.

---

## Related

- `docs/harness/deepseek-harness-analysis.md` - the upstream analysis.
- `docs/harness/plugin-platform.md` - the existing plugin contract.
- `docs/harness/self-evolution-roadmap.md` - the RSL program.
- `docs/self-improvement/SELF_MODIFICATION.md` - current Tier B policy.
- `docs/self-improvement/LOOP_MAP.md` - closed-loop status.
- `docs/harness/architecture.md` - current runtime boundaries.
