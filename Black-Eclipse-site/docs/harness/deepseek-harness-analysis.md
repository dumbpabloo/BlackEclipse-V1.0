---
title: DeepSeek Harness - an unbiased analysis of its plugin and self-evolution model
status: Current
last-verified: 2026-11-21
---

# DeepSeek Harness - an unbiased analysis

**Who this is for:** anyone deciding how Eclipse should adopt the
"everything is a plugin" and "self-evolving harness" ideas.

**Evidence base.** This analysis reads the upstream tree, not marketing copy:
`github.com/deepseek-ai/deepseek-harness` at commit `2e39e704` (`package.json`
version `0.2.2-rc.5`, fetched 2026-11-21), its `docs/`, `packages/`, `SAFETY.md`
and `AGENTS.md`; the Cordis paper (arXiv:2608.25512, "A Programming Paradigm for
Spatiotemporal Composability"); and four published self-evolution plugins
(`dsh-self-evolving`, `dsh-auto-evolve`, `dsh-skill-evolution`,
`dsh-self-improved`). Claims are separated from evidence throughout.

---

## 0. Executive summary

1. **DeepSeek Harness (DSH) is a real "everything is a plugin" harness, not a
   slogan.** The model adapter, tool registry, session log, prompt assembler,
   agent loop, UI, persistence and even the profile loader are plugins mounted
   on one Cordis context. There is no privileged core to patch: you add a plugin
   beside the others.
2. **The enabling mechanism is a composition model built on two formal ideas:**
   *revertible effects* (every registration carries its inverse, so unloading a
   plugin is complete) and *reactive coeffects* (`inject` declares required
   services; the runtime activates/deactivates dependents as providers appear
   and disappear). Profiles, bundles, ordered YAML patches, per-session presets
   and per-agent scopes are all applications of those two primitives.
3. **DSH itself does not ship a self-evolution system.** It ships the *substrate*
   that makes self-evolution possible: replaceable loop/model/tools, per-session
   presets, runtime inspection, and a dynamic package runner. "Self-evolving"
   is a third-party plugin category with at least four incompatible designs.
   That fragmentation is a weakness, not a feature.
4. **The published self-evolution plugins are honest but small.** The strongest
   (`dsh-self-evolving`) verifies a *fixed-replay engineering effect*, not a
   benchmark improvement; it needs Ubuntu, Docker, Bubblewrap and an API key.
   The most usable (`dsh-auto-evolve`) validates by replaying one failing
   episode with and without a change, which is directionally useful but not
   statistically meaningful. None has a shared evidence standard.
5. **DSH's security posture is the weakest part of the story.** The safety
   notice says it has not been audited; installed Host code runs in-process
   outside the workspace sandbox; plugin installation runs pnpm lifecycle
   scripts with the user's privileges; the dynamic runner's `node:vm` sandbox is
   explicitly "not containment"; plugin indexes are unsigned. The power that
   makes the plugin model good is not matched by an authority boundary.
6. **Eclipse should copy the composition substrate, not the trust model.**
   Eclipse already has stronger plugin security (signed packages, a closed
   permission vocabulary, an out-of-process sandbox, SSRF guards, effect
   approvals) and a far stronger evidence/self-improvement engine (RSL,
   measured vs UNMEASURED, lineage, gates, replay, significance). The right move
   is to add DSH's *kernel and composition layer* underneath Eclipse's existing
   safety and evidence machinery, then expose one first-party evolution engine
   over declared assets - which is more than DSH has.

---

## 1. The composition model: profiles, bundles, patches, presets

A running DSH is one plugin tree assembled from ordered layers
(`docs/architecture.md`, `packages/boot/app-boot/README.md`).

```
installation bundles (base, web-app, ...)
        + profile's ordered bundle list
        + profile's cordis.patch.yml
        + home-level cordis.patch.yml
        + --patch overlays
        = effective plugin rows (id, name, config, disabled, isolate, group)
```

- **Bundle**: a package that declares `dsh.bundle.patch` in `package.json` -
  one patch file or an ordered list. A bundle is a distribution format for
  config rows *and the code they mount*, so rows stay patchable by layers above.
  `dsh-base` is shared by `web`, `headless`, `sdk` and `acp`; `web-app` adds the
  browser application; `sdk-minimal` is a standalone exception.
- **Profile**: a named composition in `$DSH_HOME/profiles/<name>`. It lists
  bundles, dependencies, and its own patch. `web`, `headless`, `sdk`,
  `sdk-minimal` and `acp` ship as templates; users create variants with
  `dsh --profile <name> --from-default-profile <template>`.
- **Patch dialect**: `insert: [...]` adds rows; a row `id` plus `config`
  replaces that row's *whole config* (not a merge); `disabled` toggles it;
  `isolate` gives a group its own service instances; `group: true` nests plugins.
  Unknown ids warn; an empty patch file fails boot (`[]` disables the layer).
- **Preset**: `@deepseek-ai/dsh-agent-preset` stores `config.plugins` - a child
  Cordis tree mounted per session. A preset is how one session gets a different
  capability set (tools, prompt sections, skills) without touching its
  neighbours. `agent-preset-registry` owns selection and revision retention.
- **HMR**: the `dsh-hmr` plugin watches profile manifests, patch files and
  overlays and recomposes; package operations go through Plugin Manager's
  serialization. Without HMR, changes are `restart-required`.

**Why this works.** A user does not fork the harness to get a variant. They
write a patch row, a bundle, or a preset; the launcher reconciles the desired
row list against live fibers; removed rows are disposed and their registrations
unwind. The same mechanism serves "disable this tool", "replace the model
adapter", "put a different loop in this preset" and "ship a whole product
surface".

**Where it is fragile.** Config replacement is whole-row, so a user restating a
row silently drops defaults; layer precedence is order-sensitive; `restart-
required` paths are common; recomposition is a complex transactional surface
(the README documents rollback of removed rows, publication failures and
`package-moved`). Composition is powerful but not simple.

---

## 2. The kernel: Cordis in five ideas

From `docs/cordis-primer.md` and the paper:

| Idea | Mechanism |
|---|---|
| A plugin is an object implementing `apply(ctx)` (function, object or `Service` subclass) | `export function apply(ctx)`; optional `name`, `inject` |
| A context is a repository of services | `ctx.tools`, `ctx.llm`, `ctx.agents`, `ctx.sessions`; any plugin can `ctx.provide('key', value)` |
| Dependencies are declared, not sequenced | `inject: ['tools']`; the plugin stays PENDING until the service exists; if it disappears the plugin unloads and reloads when it returns |
| Communication is typed events with explicit dispatch semantics | `emit` (observe), `waterfall` (around-middleware, must call `next()`), `parallel`, `serial`, `bail` |
| Registrations are reversible effects | `ctx.on()`, `ctx.tools.register()`, `ctx.effect(fn)` all return ownership to the fiber; disposal runs in reverse registration order |

Lifecycle (`docs/user/develop/framework/index.md`):

```
PENDING -> LOADING -> ACTIVE
                 \-> FAILED
ACTIVE -> UNLOADING -> DISPOSED
```

Nested contexts (`ctx.plugin(child)`) have independent lifetimes and unload with
their parent. HMR is just unload + load, correct because every registration is
an effect.

**Scopes** (`packages/core/scope/README.md`) are the second-order primitive:
`createScope(ctx, key)` mints a context whose registrations are visible to that
scope and its descendants and disposed with it. `agent.ctx` is such a scope, so
a tool registered through it is visible only to that agent. The README is explicit
that a scope is **not** a sandbox or authority boundary.

**Formal framing (the paper).** The paper names the two properties DSH relies
on: *temporal composability* (a component's side effects can be completely
reverted on removal) and *spatial composability* (inter-component dependencies
are declared and reactively managed). It lifts effects and coeffects to runtime
mechanisms, unifies them in one context type, and proves observational
equivalence for interleaved components. This is the right vocabulary for
Eclipse's design: every Eclipse extension point should be an effect with an
inverse; every dependency should be a coeffect that drives activation.

---

## 3. What is actually a plugin in DSH

`docs/architecture.md` names the core packages and their `ctx` keys:

| Package | Owns | Key |
|---|---|---|
| `core/session` | append-only `SessionEvent` log | `ctx.sessions` |
| `core/system-prompt` | prompt sections, variables, tool schemas | `ctx.systemPrompt` |
| `core/tools` | tool registry and guarded execution pipeline | `ctx.tools` |
| `core/agent` | `Agent` interface and live registry | `ctx.agents` |
| `core/agent-loop` | the default driver behind that interface | `ctx.agentLoop` |
| `core/scope` | scoped registration primitive | library |
| `llm/llm` | message/stream vocabulary and adapter seam | `ctx.llm` |

The "where new behavior goes" table is the clearest statement of the model:
add a model provider on `ctx.llm`; a tool on `ctx.tools`; a shell backend on
`ctx.shell`; a session backend by implementing `SessionPersistence`; a prompt
section or variable on `ctx.systemPrompt`; an agent-scoped capability through
`agent.ctx`; a UI node through a `ConversationNodeDefinition`; a webhook rule on
`ctx.webhookRuntime`. Capability seams split a capability into **Service
Definition / Service Provider / Consumer** so a provider swap (local vs remote
filesystem, local vs remote subprocess) moves every consumer with it.

What is *not* a plugin: the `dsh` launcher, native/system code, the Electron
shell and the profile resolver. These are the "constitution" - small, stable,
and deliberately not user-replaceable. That is a sensible boundary, and Eclipse
should copy it: a tiny kernel, everything else a plugin.

**Plugin authoring surfaces.** Functions, objects or `Service` subclasses;
`defineTool` with typed parameters/output; `Config` schemas; bundles; the
Plugin Manager; `dsh plugin` CLI; the marketplace (index -> install through the
manager); and `cordis_inspect`, a read-only tool that lets an agent query live
Service/Event/Config/Runtime APIs before writing plugin code. Creator mode pairs
inspection with `plugin_manager` so the agent can *author a bundle, install it,
and observe it activate* in the same session.

**Dynamic packages.** `cordis-host-runner` defines versioned, immutable Plugin
Packages (`define`/`run`/`update`/`stop`/`undefine`), evaluates Host halves in
`node:vm`, and requires a human approval for a browser half. Definitions are
session-scoped and process-local; restart clears them. The runner's own README
says the sandbox "is not a security boundary" and treats a dynamic package like
bash access. Crucially, **no shipped model tool creates definitions** - the
Creator workflow installs persistent bundles instead.

---

## 4. How a user creates their own variant

There are four escalating levels:

1. **Patch one row** in `~/.dsh/cordis.patch.yml`: replace a config, disable a
   plugin, insert a local file plugin. One file, no fork.
2. **Write a bundle**: a package with `dsh.bundle.patch`, installed with
   `dsh plugin add`. It can add tools, UI, providers, or a whole application
   surface. It is patchable by the layers above it.
3. **Write an agent preset**: ordinary Cordis YAML assigning `config.plugins`
   to one session. This is how a user gives one agent a different tool set,
   prompt sections and skills.
4. **Generate or install at runtime**: Plugin Manager (persistent profile
   changes, approval-gated, HMR or restart) or the dynamic runner (ephemeral,
   session-scoped, approval for browser halves).

The critical property is **uniformity**: a first-party tool and a third-party
tool register the same way; a first-party model and a user's adapter are both
`ctx.llm` providers; a shipped UI and a plugin UI both use the slot registry.
There is no "extension API" separate from the product's own construction.

### 4.1 A worked example

A user wants a research assistant with a private model and one extra tool.

```ts
// my-research/src/plugin.ts
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'my-research'
export const inject = ['tools']

export function apply(ctx: Context) {
  ctx.tools.register(defineTool({
    name: 'filings_search',
    description: 'Search the local filing index.',
    parameters: { query: { type: 'string', required: true } },
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute(args) { return search(args.query) },
  }))
}
```

```yaml
# my-research/cordis.patch.yml
- insert:
    - id: my-research
      name: './src/plugin.ts'
    - id: agent-loop
      config: { agents: [{ id: main, provider: local, model: my-model }] }
```

```json
// package.json
{ "name": "my-research", "version": "0.1.0", "dsh": { "bundle": { "patch": "./cordis.patch.yml" } } }
```

`dsh plugin --profile web add ./my-research` installs and selects the bundle.
The tool appears in the next session; a per-session preset can narrow it to one
agent. **Nothing in the harness was forked, and the same file could have
replaced the model adapter, the loop, or a UI panel.**

The cost is that the user now owns a package with arbitrary in-process code and
no permission declaration. On Eclipse, the same bundle must declare its
permissions, be signed, and (if untrusted) run in the sandbox child - more
friction, less blast radius. That trade is the central design choice of this
plan.


---

## 5. The self-evolution story, separated from the hype

### 5.1 What DSH core provides

DSH core provides no evolve loop, no candidate store, no evaluator, no rollout
policy. It provides:

- **swappable everything** (so a plugin can change behavior without a fork);
- **presets and scopes** (so a change can be per-session or per-agent);
- **reversible mounting** (so a failed change can be unmounted);
- **inspection** (so a proposer can read live APIs and config);
- **Plugin Manager** (so a validated change can be installed persistently);
- **dynamic packages** (so an ephemeral change can be run and stopped);
- **events and session traces** (so a plugin can observe outcomes).

That is a *substrate for* self-evolution, and it is genuinely well designed.
It is not an implementation of self-evolution.

### 5.2 The four published designs

| Plugin | Observe | Propose | Validate | Apply | Evidence claim |
|---|---|---|---|---|---|
| `dsh-self-evolving` | dev-observed evidence | networkless proposer sandbox, bounded multi-file Cordis plugin candidates | deterministic builder -> one-shot real Loader in an isolated capsule -> Harbor/Terminal-Bench | controller is sole writer; hash-chain journal; budget ledger; crash-resumable | `ENGINEERING_EFFECT_VERIFIED` on a fixed replay; **no Terminal-Bench improvement claim** |
| `dsh-auto-evolve` | `tools/result`, `agent/request-error`; dedup + thresholds | LLM fills a closed mutation vocabulary (`add`/`patch`/`retire` over skill/post-processor/prompt-section/guard-policy) | sandboxed sub-agent replays the failing episode once baseline, once trial; compares completion/failures/cost | live `ctx.skills.register` + disposer kept; ledger; rollback; regression watch; budget caps | "measurable improvement" in a single replay; no statistical claim |
| `dsh-skill-evolution` | `turn/end` success, complexity, recovery, repetition | LLM review `SAVE` / `FOLD_INTO` / `NOTHING_TO_SAVE` | hard do-not-capture list; corroboration gate; dedup | versioned `SKILL.md` in `generated/`; re-register on restart | qualitative; no benchmark claim |
| `dsh-self-improved` | conversation history | extraction of facts/preferences/events/instructions | SQLite L0-L3 consolidation | inject relevant memory before each turn | memory continuity; no capability claim |

The strongest engineering is `dsh-self-evolving`: controller-as-single-writer,
intent-before-launch, idempotency keys, hash-chain journal, one-shot real Loader
admission, credential isolation, and an explicit refusal to claim a benchmark
result it did not measure. The weakest part is its operational surface: Ubuntu
24.04, Docker, Bubblewrap, `uv`, Python 3.12, a DeepSeek API key, and a manual
`init`/`run`/`status`/`audit` workflow. It is a research instrument, not a
product feature.

`dsh-auto-evolve` is the most product-shaped: modes, budgets, operator tools,
rollback, convergence pause. Its validation is the weakest link: one replay of
one episode, no paired trials, no significance, no held-out split. It is a
useful heuristic, not evidence.

### 5.3 Why the ecosystem fragmented

Because DSH offers primitives but no shared **asset/evidence contract**, every
plugin invents its own observation schema, mutation vocabulary, store, ledger,
validator and UI. They cannot compose: `dsh-auto-evolve` cannot feed
`dsh-self-evolving`; `dsh-skill-evolution`'s skills are not the same objects as
`dsh-auto-evolve`'s genome assets. A user who installs three gets three loops,
three budgets and three truths.

**This is the gap Eclipse should close.** Eclipse already has the hard part -
one evaluation/lineage/evidence system - and should expose it as the *only*
evolution contract, with plugin composition for the assets.

---

## 6. Security and trust: the honest assessment

DSH's `SAFETY.md` is unusually candid:

- experimental developer preview, no security audit, not production-ready;
- it executes model-generated code and commands, loads third-party plugins,
  and accesses network, processes, credentials and files;
- sandboxing and approvals reduce risk but do not guarantee isolation.

Concrete properties found in code/docs:

| Area | DSH behavior | Consequence |
|---|---|---|
| Installed plugins | run **in-process, outside the workspace sandbox**; `plugin_manager` says so explicitly | a malicious plugin has the host user's authority |
| Install | pnpm add + lifecycle scripts; build-script approval exists but is separate | supply-chain risk before any plugin code loads |
| Signatures | no package signature verification in the install path; marketplace indexes are unsigned | index/registry is trusted by URL, not by publisher |
| Permissions | no per-plugin permission manifest; capability is whatever `ctx` offers | no least-privilege declaration or enforcement |
| Dynamic runner | `node:vm` facade; README: "not containment" | treat like bash access; only for cooperative code |
| Approvals | Plugin Manager tool needs `danger-full-access` or per-call approval; dynamic browser halves need a human | coarse-grained; one approval can cover future versions |
| Compatibility | `engines.dsh` checks at composition boundaries; docs conflict on whether manifest readers enforce it | version skew is a real failure mode in a fast-moving preview |
| Data | profile changes affect every session; dynamic definitions vanish on restart | not a tenancy boundary |

The design philosophy is "trusted same-process plugins", which is coherent for a
developer tool. It is not a model Eclipse can adopt: Eclipse ships to users with
broker credentials, personal portfolios and a local vault. Eclipse's existing
signed-package + sandbox + permission model is strictly stronger and must be the
floor for any new kernel.

---

## 7. Strengths, weaknesses and failure modes

### Strengths worth copying

1. **One composition model for every variant** - patch, bundle, preset, dynamic
   package. Users do not fork.
2. **Dependency-driven lifecycle** - `inject` removes boot-order bugs; provider
   disappearance unloads consumers instead of leaving stale references.
3. **Reversible registration** - unload/HMR is correct by construction; no
   manual `removeListener`.
4. **Capability seams** - Definition/Provider/Consumer lets one provider swap
   move a whole family of features.
5. **Per-agent scopes** - the same registry yields a different capability set per
   session without global mutation.
6. **Typed event dispatch** - the mode is part of the contract; `waterfall` is a
   real middleware seam, not an afterthought.
7. **Runtime inspection** - agents can discover the live API before writing
   against it; this is the missing half of most plugin systems.
8. **Honest evidence culture** - the self-evolution plugins label what they did
   not prove, and the core docs separate "Model Experience" and "Known
   Limitations" for every package.

### Weaknesses and failure modes

1. **Power without an authority boundary.** In-process arbitrary code, no
   signatures, no permissions. The plugin system's greatest strength is its
   greatest risk.
2. **Composition complexity leaks to users.** Whole-row config replacement,
   layer precedence, `restart-required`, recomposition rollback and
   `package-moved` are expert-level concepts.
3. **No first-party evolution contract.** Four incompatible loops; no shared
   asset schema, evidence format, budget model or lineage.
4. **Validation is often a demo.** Single-replay A/B, no significance, no
   held-out split, no regression suite in the common case.
5. **Persistence boundaries are unclear.** Dynamic definitions are process-local;
   persistent changes are profile-wide; per-session composition is possible but
   not the default.
6. **Developer-preview churn.** Docs disagree (e.g. whether `engines.dsh` is
   enforced), APIs break, and compatibility is a range check rather than a
   contract test.
7. **The kernel is subtle.** Effects, coeffects, fibers, dispatcher modes and
   loader expressions are a large conceptual surface; a wrong `next()` or a
   missed disposer silently changes behavior.
8. **UI composition is two-sided.** Host and browser halves, approval round
   trips, slot lifetimes and render-failure reporting are powerful but expensive
   to author and debug.

---

## 8. What Eclipse should take, and what it should invert

**Take:**

- the context/service/inject/effect/scope kernel;
- profiles, bundles, ordered patches and per-session presets;
- reversible registrations and a reconciliation loop with rollback;
- capability seams (Definition/Provider/Consumer);
- per-agent scoped registries;
- typed event dispatch;
- read-only runtime inspection for agents;
- a plugin authoring path that is the same path the product uses.

**Invert:**

- **Trust**: keep Eclipse's signatures, closed permission vocabulary,
  out-of-process sandbox, effect approvals and SSRF/secret boundaries. A plugin
  never gets raw `ctx` authority; it gets a scoped, permission-checked facade.
- **Evidence**: one evolution contract, not four. A change is `MEASURED` only
  with a frozen evaluator, a held-out split, paired trials and a significance
  gate; otherwise it is `UNMEASURED` and cannot auto-apply.
- **Scope of mutation**: declared assets (prompt sections, skills, tool policy,
  hooks, presets, composition rows, plugin bundles) are first-class evolvable
  objects. Code stays operator-landed.
- **Persistence**: every candidate, intent and activation is durable and
  crash-resumable before it runs; no in-memory-only evolution state.
- **Operator surface**: one status/candidates/apply/rollback/pause surface over
  all asset kinds, not one per plugin.

The detailed target architecture, gap matrix and phased implementation plan are
in `docs/harness/plugin-kernel-plan.md`.

---

## Appendix - evidence index

Upstream files this analysis relies on (commit `2e39e704`, `0.2.2-rc.5`):

| Claim | File |
|---|---|
| Everything is a plugin; profiles/bundles/patches | `docs/architecture.md`, `packages/boot/app-boot/README.md` |
| Context/services/inject/effects/events | `docs/cordis-primer.md`, `docs/user/develop/framework/*.md` |
| Scopes are not an authority boundary | `packages/core/scope/README.md` |
| Tool registry and five event seams | `packages/core/tools/README.md` |
| Swappable loop behind `ctx.agents` | `packages/core/agent/README.md`, `packages/core/agent-loop/README.md` |
| Prompt assembly registry | `packages/core/system-prompt/README.md` |
| Per-session presets | `packages/preset/agent-preset/README.md` |
| Plugin Manager install/enable/HMR/approval | `packages/boot/plugin-manager/README.md` |
| Bundle manifest fields | `packages/util/package-manifest/README.md` |
| Read-only runtime inspection | `packages/extensions/tool-cordis/README.md`, `docs/subsystems/extensions.md` |
| Dynamic packages are process-local and not containment | `packages/extensions/cordis-host-runner/README.md`, `src/sandbox.ts` |
| In-process installed code | `packages/boot/plugin-manager/README.md` ("installed Host code executes in-process outside the workspace sandbox") |
| Safety posture | `SAFETY.md` |
| Ecosystem evolution designs | `dsh-self-evolving/README.md`, `dsh-auto-evolve/README.md`, `dsh-skill-evolution/README.md`, `dsh-self-improved/README.md` |
| Formal model | arXiv:2608.25512, "A Programming Paradigm for Spatiotemporal Composability" |

Where the repository's own docs disagree (for example whether `engines.dsh` is
enforced by manifest readers), this analysis prefers the behavior described in
`app-boot`/`plugin-manager` and treats the contradiction as a documentation
defect, not as a feature.

