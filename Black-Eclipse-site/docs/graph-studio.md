---
title: Graph Studio — Visual Workflow Engineering
status: Current
last-verified: 2026-11-04
---

# Graph Studio

**What this is:** the visual graph-engineering surface inside Eclipse — a canvas
for designing, validating, versioning and (progressively) executing workflows,
quant strategies and multi-agent systems that all compile to ONE canonical
workflow model.

This document is the **living implementation tracker** for the Graph Studio
program (Phases 1–10). It records the repository audit, the architecture, what is
implemented, and what remains. It is updated as each phase lands.

---

## Repository audit (before any change)

The audit established what already exists and what must NOT be rebuilt.

### Application shell

- Next.js 15 App Router, **tabbed SPA (no router)**. Screens mount as keep-alive
  blocks in `src/components/screens/stock-chat.tsx`; the tab union is the ONE
  declaration in `src/lib/app-tabs.ts`; sidebar items in
  `src/lib/sidebar/config.ts`; nav labels in `src/lib/i18n/nav.ts` +
  `dictionaries/en.ts`. There is a compile-time + runtime guard
  (`src/lib/app-tabs.test.ts`, `sidebar/config.test.ts`).
- Test infra: vitest (node env), `npm run test` = vitest + Python suites;
  `npx tsc --noEmit`; `npm run build`.

### Backend

- **Python FastAPI harness** is the agentic runtime. The browser reaches it
  through thin **front-door proxies** `src/app/api/<name>/[[...path]]/route.ts`
  → Python `/<name>/*` (guarded by `guardMutation` + runtime auth headers). The
  TypeScript side validates, resolves config and proxies; it does NOT reimplement
  agent loops, tools or model adapters.
- **Databases:** JDDS (`jdds.db`, Prisma/SQLite — market data + app tables) and
  `agent-memory.db` (Python runtime's own store). `doctor.js` runs
  `prisma db push` on boot, so adding Prisma models is zero-config.

### Existing graph / orchestration / finance infrastructure (reuse, don't rebuild)

- `python/harness/workflows/` — a bounded **step-plan** model (`Workflow`,
  `WorkflowStep`), validation, `WorkflowStore` (agent-memory.db) and a compiler
  onto the existing agent loop. This is the runtime *plan*, not a visual graph.
- `python/harness/quant/` — typed **strategy graph → IR compiler**, node registry,
  deterministic backtesting engine, indicators, costs, metrics, optimization.
- `python/harness/portfolio|factors|stats|ml/` — risk, factor, statistical and ML
  engines, each with an immutable-versioned store and a REST surface.
- `python/harness/fabric/` — durable execution fabric (modes, queues, workers,
  retries, DLQ, idempotency) — the Phase 4 orchestration substrate.
- `python/harness/tasks/`, `events/`, `automation/`, `artifacts/`, `security/`,
  `observability/` — durable plans, event bus/triggers, artifact storage,
  authorization ("ONE decision path"), telemetry.
- Market data via the **FinancialDataRouter / JDDS** — the single ingestion path;
  Graph Studio must read through it, never build a second one.

**Conclusion:** the runtime, scheduling, durability, finance engines and security
layer largely exist. The genuine gap is a **visual editor + a canonical
workflow model that unifies them**. Graph Studio is that layer.

---

## Architecture

```
 editor state (transient)         persistent document
 ┌───────────────────┐            ┌──────────────────────────┐
 │ selection, dirty  │            │ Eclipse Workflow IR      │
 │ undo/redo, errors │            │  irVersion + nodes/edges │
 └─────────┬─────────┘            │  + ports/config/modes    │
           │ dispatch(patch)      └────────────┬─────────────┘
           ▼                                   │ validate (pure)
   patches.ts (pure, validated) ──────────────►validation.ts (registry-driven)
                                               │ compile (Phase 3)
                                               ▼
                                        execution plan → runtime
```

Hard separations held by design:

- **The canvas is not the runtime.** React Flow renders the IR; edits route back
  through validated patches. React Flow's own node/edge types are never the
  canonical format.
- **Layout is non-semantic.** Node `x/y` is persisted for a usable reopen, but the
  validator/compiler ignore it — moving a node cannot change what a workflow does.
- **The editor state is transient.** Selection, dirty flag and undo history live
  in the zustand store, never in the persisted IR.
- **Authorization is runtime-side.** The IR declares required capabilities and an
  execution mode; the editor's mode selector is a *request*, enforced later by the
  security layer — a node or agent can never widen its own scope.

### Canonical Workflow IR (`src/lib/graph-studio/`)

| Module | Responsibility |
| --- | --- |
| `types.ts` | The vocabulary: `PortType`, `WorkflowNode/Edge`, `WorkflowSettings`, `NodeDefinition`, `Diagnostic`. |
| `ir.ts` | zod schema + `createWorkflowIR`/`parseWorkflowIR`; the ONE document model. |
| `migrations.ts` | Versioned migrations; `readWorkflowIR` = migrate → validate. |
| `registry.ts` | Data-only node registry + SDK contract (ports, config schema, capabilities, side-effect class, allowed modes). |
| `manifest.ts` / `registry.manifest.json` | Renders the TS registry to a deterministic cross-language manifest. Python asserts it matches its own registry node for node. |
| `validation.ts` | Frontend-independent validator: types, config, required inputs, cycles, reachability, terminals, mode/capability checks. All diagnostics name a node/edge. |
| `patches.ts` | Pure, individually-validated graph mutations; `canConnect` powers the canvas connection guard. |
| `layout.ts` | Deterministic layered auto-layout (no heavy layout dependency). |
| `editor-store.ts` | Transient editor state + undo/redo; every mutation via `dispatch(patch)`. |
| `persistence-client.ts` / `server-store.ts` | `/api/workflows*` client and the Prisma/append-only-version store. |
| `templates.ts` | Real, valid starter graphs. |

### Cross-language node contract

The editor registry (`registry.ts`) and the runtime registry
(`python/harness/graph_runtime/nodes.py`) are separate code on purpose: one
drives the palette, the other authorizes execution. They must describe the SAME
contracts.

- `manifest.ts` renders the TS registry into `registry.manifest.json` (ports,
  config field names/types and their bounds — `maxLength`, `enum`, `min`/`max`,
  `integer` — capabilities, side effects, modes, terminal).
- After changing ANY node contract run `npm run graph:manifest`.
- `src/lib/graph-studio/manifest.test.ts` fails when the checked-in JSON is
  stale; `python/tests/test_graph_registry_parity.py` fails when the Python
  registry disagrees with it — including when the two sides disagree about a
  field's bounds. A node that exists in only one side is an error.
- Additive changes (an optional port, a new defaulted config field, a widened
  port type) keep `type@version`; removing or renaming a contract does not
  (ADR-H344).

The manifest covers **54 node types**, and every registered type has an
executor in the runtime. `flow.subworkflow` is the one special case: the
executor handles it directly (`_run_subworkflow`) instead of binding a plain
`executor` function, so it has no executor attribute by design. The parity test
asserts that no other node is a dead contract.

`data.set_variables` is the first new core node added through this pipeline: it
merges configured named values over an optional input object (configured keys
win).

### Persistence

`Workflow` (container: name, tags, owner, `currentVersion`) + immutable
`WorkflowVersion` (JSON IR + content hash), in JDDS. Saving appends the next
version; the IR is re-validated server-side before it is stored. Routes:
`/api/workflows` (list/create), `/api/workflows/[id]` (get/save/delete),
`/api/workflows/[id]/versions`.

**One identity.** A run, a schedule and a subworkflow lookup are all addressed
by the persisted `Workflow` row id — never by the IR document id (`wf_…`). The
front door aligns the IR id to the row id before dispatching, and aligns each
bundled child IR to its pinned row id, so `/api/workflows/[id]/runs` (History)
and nested subworkflow rows use the same key.

---

## Navigation and workspace

The **Workflows** mode (`NavGroup 'graph'`, opened with `Alt+G` or the mode
switcher) is its own top-level workspace. Unlike Bots, it does **not** replace
the sidebar with a custom panel: the left sidebar uses the **standard nav list
filtered to graph sections** (**Overview**, Tables, plus the shared ChatBot
item), so the shell looks and behaves like every other page. A fresh shell still
boots in **Terminal**; a tab that chose Workflows keeps it for that session.

**Overview is the one Graph-mode page** (`overview` AppTab). There is no separate
`graph-studio` nav item: Overview hosts the whole workspace — the workflow
gallery *and* the editor. Opening/creating a workflow flips the page into the
editor in place; the editor's **Overview** back action returns to the gallery.
Old `?tab=graph-studio` links alias to `?tab=overview`. The **gallery keeps the
app top bar**; opening a workflow hides it so the **editor is full-bleed**. The
chat dock is hidden in both; the left sidebar stays visible.

The screen has two internal views (no router):

- **Workflows home** (`graph-home.tsx`) — a full-width, searchable **card
  gallery** of saved workflows (name, description, version, tags, updated time),
  the template starter set, prompt-to-graph generation, and a "continue editing"
  card when the editor still holds unsaved work. Data tables live on their own
  top-level **Tables** page (`docs/tables.md`), not inside Graph Studio.
- **Editor** (`graph-studio-screen.tsx` → `graph-canvas.tsx`) — a 48px header
  (Overview breadcrumb, a content-sized workflow name with dirty-state dot,
  version, execution mode, undo/redo, issues, Save, Test, and a More menu), the
  canvas, an optional right inspector dock and a collapsible execution dock.
  The canvas is **transparent** over the shell's frosted background, and node
  bodies use the `.node-card` material (a layered translucent surface with an inset highlight); floating
  palettes/menus stay on the glass overlay tiers.
  Nodes are **compact by default**: a 46px identity header (32px icon, 13px
  title, 9.5px category, quiet run dot, 2px black bezel) plus up to two **inline
  parameter fields**. A scalar value can therefore be read and changed without
  opening the inspector; object/array values stay panel-only. Fields whose input
  is supplied by an edge render a read-only **From wire** chip instead of an
  input, so a value cannot silently shadow the connected wire. `config-fields.ts`
  owns the presentation metadata (label, kind, select options, `inline`) and
  falls back to the definition's `defaultConfig()` shape for nodes without
  overrides — persisted config and semantics are unchanged. Auto-layout gaps keep
  enough vertical room for a node with two inline fields.

### Reading a node

A node has to answer four questions without a manual:

- **What does it do?** The card's subtitle is the node's own one-line
  `description` ("Routes the run down one of two branches"), not its category.
  The category is in the tooltip and the inspector.
- **Which connector is which?** Every port carries a visible label beside its
  handle — `If true`, `If false`, `No match`, `Any match`, `Items`, `Done` —
  on a translucent chip so an edge cannot hide it. Labels sit outside the card
  (outputs right, inputs left); single-port nodes keep just the dot.
- **Is it a branch or a value?** Control ports are **square**, data ports are
  **round**. A branch handle can never be mistaken for a value handle.
- **Which output do I pick?** Branch outputs come first, in the order the
  sentence reads. A `Switch` shows one output per configured case (labelled with
  the case value), then `No match` and `Any match`; its `Case index` data output
  only appears on the card when something is wired to it. "Any match" fires on
  every match, so wiring both it and a `caseN` runs both branches — the validator
  warns about that (`switch_double_branch`).

```
                         ┌──────────────────────────────┐
   Value ●───────────────┤  If / else                   ├──── ■ If true
        (round = value)  │  Routes the run down one of  ├──── ■ If false
                         │  two branches                ├──── ● Result
                         └──────────────────────────────┘
                              ▲                              ▲
                     the label is the port name      square = branch (run control)
```

Every handle is labelled with its port name; the card subtitle is the node's
one-line description. An amber ring means a required input is still unwired.

The inspector's Settings tab lists the **same** visible ports (a Switch no longer
lists eight unused cases) and marks each one `control` or `data`. The canvas and
the inspector derive their ports from one helper (`node-ports.ts`), so they cannot
disagree.

**A node that needs something says so.** A required input that is neither wired
nor given a node literal puts an amber ring on the card and a `Connect: Items`
line under the header; the same list is the tooltip on the warning icon. It is
the card's version of the validator's `missing_required_input`, so a half-wired
node no longer looks finished. The palette shows `N in · M out` and a `branch`
marker, and the inspector header repeats the node's one-line description.

**A branch that goes nowhere is reported.** When a node with more than one
control output (`If / else`, `Switch`) fires a branch and nothing is wired to it,
the run log records `branch If true fired but nothing is connected to it — the run
continues with no effect`. Nodes with a single `done` control stay quiet: an
unwired `done` is not a decision.

The canvas `?` panel opens with a three-line **Reading a node** legend (round =
value, square = branch, amber ring = needs a connection).

**Inserting a node explains the port it lands on.** The add-a-step panel is
already filtered to nodes that can be wired at the chosen point; it now says
*why*: `After If / else · If true — a branch (run control). Only nodes with a Run
control input can follow it.` or `After Filter · Items (array). The new node must
accept that type.` For an insertion on an edge it names both sides. When the list
is empty it explains the rule instead of saying "no node can be wired here".

**Every logic node has a worked example.** The inspector's Settings tab shows an
input → output line (from `node-examples.ts`, presentation only, not part of the
contract), e.g. `cases "buy, sell, hold" · value "sell" → case2 fires (plus Any
match), Case index = 1`.

### Visual language

The editor deliberately keeps the app's frosted-glass theme rather than painting
an opaque canvas: the dot grid sits directly on the shell background, nodes are
translucent glass cards, and the small hover toolbars/edge controls use a
semi-transparent `bg-background/90` so they blend with both light and dark
themes. A solid canvas fill was removed because it flattened the rest of the
workspace and made the editor look like a separate product.

The left sidebar no longer hosts the node library. Adding a step happens **on
the canvas**, where the insertion point is known, and opens a **right-side node
library drawer** (a portaled `glass-drawer`, 400px) with search, category
sections and keyboard navigation. Picking a node inserts it at the pending
target and closes the drawer. On a phone the drawer becomes a full-width sheet.

| Gesture | Result |
| --- | --- |
| `Ctrl/Cmd+Tab` (canvas focused) / empty-state "Add first step" / right-edge **Add node** button | Search panel inserts a node under the pointer / view centre |
| Canvas controls → **Tidy up layout** (or the editor's More menu) | Deterministic auto-layout of every node |
| `+` on a node output | Search panel appends a node and wires that output |
| `+` on a hovered edge | Search panel inserts a node *into* the edge (splits it) |
| Drag on empty pane | Pan the canvas (grab/grabbing cursor) |
| Middle-mouse drag / hold `Space` + drag | Pan the canvas (same as plain drag) |
| `Shift` + drag on empty pane | Marquee selection box (crosshair cursor; n8n default) |
| Click node | Select it; the in-node widgets stay editable (drag still moves it) |
| Double-click node | Open the Parameters / Settings / Output inspector |
| `Ctrl/Cmd` + click node | Add/remove from the selection |
| Right-click pane / node | Context menu: add/paste/select-all/tidy/fit, or duplicate/copy/delete |
| Canvas controls → **?** | Canvas shortcuts panel |
| `Ctrl/Cmd+D` / `C` / `V` / `A` | Duplicate / copy / paste / select all |
| `Delete` / `Backspace` | Delete selected nodes and edges |

The search panel filters the registry to nodes that can actually be wired at
the chosen point (typed ports and acyclicity are checked by the same
`canConnect`/`portsCompatible` rules). Compound edits are expressed as ordinary
validated patches in `edit-ops.ts`, so undo/redo and the server-side revalidation
seam are unchanged.

**Node details.** **Double-clicking** a node opens a ~380px inspector dock;
a single click only selects it, so the in-node widgets can be edited without the
dock covering the canvas. Dragging still moves the node, and clicking empty
canvas closes the dock.
The card's inline fields and the inspector's Parameters tab are the **same
renderer** (`config-field.tsx`) at two densities: text/number edits commit on
blur or Enter (Escape reverts), booleans and selects commit immediately, and
consecutive edits to one control collapse into a single undo step
(`dispatch(patch, { coalesceKey })`). Inline inputs are `nodrag nopan nowheel`, so
typing never pans the canvas, drags the node, or opens the inspector mid-edit.

The Parameters tab is the full n8n-style form: the node's description and docs,
a friendly label plus one-line help for every field, required markers, selects
and booleans, a JSON editor only for object/array values, and an **Advanced: edit
config as JSON** escape hatch for fields the form does not expose. Field kinds
now include **textarea**, **key/value rows** (`data.set_variables`), **code**
(JSON with validate/format), **combobox** (free text with suggestions) and a
**slider**; the presentation map is still `config-fields.ts`, so semantics and
the persisted config never change. Dynamically resolved fields (agent preset,
broker connection/mandate, pinned body workflow) render as pickers. **Settings**
holds free-form **Notes** (saved with the node, never executed), ports with
types, capabilities, allowed modes and the node id. **Output** shows the last
run's bounded inputs/outputs for that node, read from the persisted run record.
Issues, History and Health are reachable from the header instead of sharing five
cramped tabs.

**Port literals (IR v2).** Above the parameters the inspector shows an
**Inputs** section for every data port. An unwired port can carry a literal
(`WorkflowNode.inputValues`, keyed by port id) so a node is usable without a
producer — for example Parse JSON carrying its text, or Load dataset carrying
its ref. A connected wire **always wins**; the inspector says "wired" and the
validator warns that the literal is ignored. Required inputs are satisfied by a
wire **or** a literal. Control ports never take literals (they gate execution,
not values). Type-aware editors cover text, number, boolean, ticket (an MT5
integer or a REST opaque id) and JSON values, and
the node **Test** tab prefills from the literals. The runtime resolves
`wire → literal → optional/null` in `executor.py`, and the compiler accepts the
same rule, so the editor and the Python runtime cannot disagree.

**Data-node controls.** Three data nodes also accept an editable config fallback
so they are useful without an upstream wire: Parse JSON has a **JSON text**
editor, Save dataset has **Payload (JSON)**, and Load dataset has a **Saved
datasets** picker backed by `GET /graphs/datasets` (ref, name, type, rows,
bytes) plus a manual ref field. Resolution is `wire → literal → config`, and the
validator/compiler/executor share the same fallback table (`CONFIG_FALLBACK` in
`validation.ts`, mirrored in `compiler.py`/`executor.py`). Set variables uses the
key/value row editor described above.

**Execution.** The header's **Start workflow** button is the primary run control,
inline with the other editor actions; when there are unsaved edits it becomes
**Save & run** (save, then execute the persisted version). The execution dock's
**Test run** action does the same without the save step. The bottom
**execution dock** collapses to a status strip and expands into the existing
run console; node statuses also render on the canvas (border tint, status badge,
animated edges while running). Operator runs open an explicit authorization
dialog listing only the effects this graph declared. History and Health open in
the same resizable right rail as the Assistant.

While a run is active the dock consumes the run's **SSE stream**
(`GET /graphs/runs/{id}/stream` → Next proxy `/api/graph-runs/[runId]/stream`).
The stream replays the persisted event log from a cursor (`Last-Event-ID`), tails
new events from the `HarnessGraphEvent` table, updates node statuses in place,
and ends with `stream.end` once the run is terminal. The browser only fetches
the full run record at that point, so outputs/durations/errors are authoritative.
If the stream disconnects, the UI falls back to bounded polling.

## Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| **1 — Foundation** | Workspace, canvas, custom nodes, editor state, persistence, dedicated mode | **Implemented & verified** |
| 2 — Canonical IR & node system | IR, registry/SDK, typed ports, validation, patches | **Implemented in TS AND Python** (editor + runtime registries in lockstep, now enforced by a generated cross-language manifest); migrations seam live |
| **3 — Compiler & execution engine** | IR → plan, deterministic executor, inspection, isolation | **Implemented & verified** |
| **4 — Control flow, scheduling & triggers** | Control-flow node pack, unattended schedules, webhook trigger, run recovery | **Implemented & verified** (distributed/Fabric-backed durability is the remaining piece) |
| **5 — Finance & quant node library** | Market-data + indicator node executors | **Implemented & verified** (first pack: market data, indicator, returns, z-score, volatility) |
| 6 — Backtest & simulation | Strategy graphs, metrics | **Implemented & verified** (backtest, walk-forward, optimizer, Monte Carlo and out-of-sample holdout; all reuse `quant/`) |
| 7 — AI agents & Chat-to-Graph | Agent nodes, chat→IR, in-editor assistant | **Implemented** (research agent, structured step planner, bounded task delegation, live web research, validated chat→IR, streaming tool-calling graph assistant with bounded test-run/fix loop) |
| 8 — Paper trading & safety | Execution adapter, risk controls, modes | **Implemented & verified** (`trading.risk_check` + `trading.paper_order` with a durable paper account, PAPER-only; live and ungranted trading refused) |
| 9 — Advanced engineering | Subworkflows, debugging, diffing | **Implemented** (subworkflows; post-run step inspector; structural version diff) |
| 10 — Hardening & verification | Tests, security, observability | **Implemented** (operator health telemetry: run/node counts, durations, queue wait, failure codes, slowest nodes) |

### Phase 3 — the runtime (`python/harness/graph_runtime/`)

The compiler and executor live in **Python** — the agentic runtime — not in
Next.js. The front door never executes a graph; it loads the persisted IR from
JDDS and forwards it.

> Naming trap: `python/harness/graph.py` is the EXISTING agent tool loop. The
> workflow runtime is the package `python/harness/graph_runtime/`. Do not create
> `harness/graph/` — it would shadow the module and break `execute_run`.

| Module | Responsibility |
| --- | --- |
| `model.py` | The IR contracts (pydantic, camelCase aliases mirroring the TS schema) + run/plan DTOs. Untrusted input: every field is re-validated. |
| `registry.py` | The node SDK: typed ports, config fields, capabilities, side-effect class, allowed modes, executor, timeout/retry. |
| `nodes.py` | The built-in PURE executors and the registered-but-unavailable contracts for later phases. Re-exports the formula and template helpers so existing imports keep working. |
| `formula.py` | The bounded safe formula language (tokenizer + recursive-descent parser, fixed function table, hard token/length bounds, lazy `if()`) — never `eval`. Shared by the formula/map/filter/reduce nodes and loop conditions. |
| `template.py` | The bounded template renderer (dotted paths, format specs, nested `{{#each}}` row blocks, output/row/depth caps spent while rendering). |
| `compiler.py` | IR → `ExecutionPlan`: structural validation, longest-path **layers** (the parallelizable set) + deterministic topological order, mode checks and the **permission envelope**. |
| `executor.py` | Deterministic layer scheduler: `pending → ready → running → succeeded \| failed \| cancelled` (+ `skipped`), per-node timeout, retries, cancellation, output-schema validation, bounded input/output previews, event log. |
| `store.py` | Runs, **normalized node runs**, events and per-node checkpoints on `agent-memory.db`. Large node outputs stay as previews but make the run non-resumable. |
| `service.py` | Compile / run_sync / durable dispatch / execute_job / poll / cancel. |
| `worker.py` | The Fabric graph worker: runs `execute_job` in the loop's thread pool and forwards cancellation into a threading event. |
| `bootstrap.py` | Idempotent boot wiring; `routers/graphs.py` exposes `/graphs/*`. |

**Trust boundary.** `POST /api/workflows/[id]/runs` (Next.js) loads the
authoritative persisted version — the client never supplies the IR — and forwards
it with `testRun: true`. The runtime independently enforces the envelope: a
**test run never performs external write, network or trading effects**, and a
node whose declared capabilities were not granted is refused. Local scratch
writes (`local_write`, used by datasets) are allowed and bounded. Editor
visibility is not authorization.

**Run envelopes.** The default is a **test run**: no world effects, local reads
and local scratch only. An **operator run** is an explicit UI action; the front
door derives its grants from the registry and the persisted IR — never from the
caller. It grants exactly the union of node capabilities, enables network only
when the graph needs it and the user confirms, and keeps external write nodes
refused. Three effects may be enabled explicitly: PAPER trading
(`trading.paper_order` requires `executionMode: "paper"`, an explicit
confirmation and `allowTrading`), LIVE broker execution (`trading.broker_*`
requires `executionMode: "live"`, `allowLiveTrading`, an armed live connection
and a workflow-pinned live mandate), the
whitelisted LOCAL video render (`video.render` requires an explicit local-render
confirmation and `allowWrite`), and scoped file writes (`file.write` requires
the operator to confirm each named scope it uses; `allowedWriteScopes` carries
only those scopes). The Python compiler re-checks the same envelope.

**Scoped writes.** `file.write` never chooses an absolute path. A write names a
**scope** configured by the operator (`HARNESS_GRAPH_WRITE_ROOTS`, a JSON map;
`workspace` always defaults to `.black-eclipse/workspace`). The path is relative,
`..` and absolute paths are refused, the resolved target (after following
symlinks) must stay inside the scope root, the payload is capped at 8 MB, and a
normal write is atomic (temp file + replace). A test run never writes; an
operator run is granted only the scopes it explicitly confirmed.
`integration.http@1.0.0` is kept for saved graphs. `integration.http@1.1.0`
adds query params (`queryJson`), body modes (`json` / `text` / `form`), response
type control, a configurable response cap with `error`/`truncate`, opt-in
redirects, retries with `Retry-After`, `failOnHttpError`, and
`headers`/`ok`/`contentType`/`durationMs`/`truncated` outputs. Both versions are
SSRF-checked, loopback refused, bounded to 60 s, and accept plain custom headers
(`headersJson`) plus credential REFERENCES (`credentialEnvJson`: header → env-var
name, resolved by the runtime and never stored). Transport-managed headers are
refused; a header cannot come from both sources.

**Redirects are the security-critical part.** The executor does not let
`urllib` follow redirects implicitly: `http_client.py` requests each hop with a
no-redirect opener and re-runs the SSRF check on every `Location`, caps the hop
count, and refuses an HTTPS → HTTP downgrade. (The previous 1.0.0 path checked
only the first URL, so a public URL could redirect into loopback/link-local
space.) Request URLs are logged without their query string, and credential
values never appear in a log, run record, or error. Known residual: the check is
host-based on the URL and each hop; it does not pin the resolved DNS address, so
a hostile resolver/rebinding attack is outside this node's guarantee.

`integration.webhook_send` is the signed outbound mirror: it POSTs JSON with
`X-Eclipse-Signature` (`sha256` over `"<timestamp>.<body>"`),
`X-Eclipse-Timestamp` and `X-Eclipse-Delivery`. Its signing secret is an ENV VAR
name, not a value.

**Determinism.** Executors are pure; the scheduler orders each layer by node id;
node inputs come from succeeded upstream writers only (a node whose required
input never materialized is `skipped`, never run with a null placeholder). Two
runs of the same graph with the same inputs produce identical outputs (tested).

**Durability.** A run is a DURABLE **Execution Fabric** job: the Fabric owns the
lease, retry/dead-letter and restart recovery, and the graph worker executes the
synchronous scheduler in its thread pool. The run row is `queued → running →
succeeded | failed | cancelled`; each node writes a normalized
`HarnessGraphNodeRun` row and an event, and a successful node stores a bounded
checkpoint. When the Fabric requeues a job after a crash, `execute_job` resumes:
completed nodes are seeded from their checkpoints and never re-executed. A graph
that fails a NODE is still a succeeded Fabric job (the run row is authoritative),
so the queue never retries a deterministic graph failure. If the Fabric is
disabled/unavailable the legacy in-process thread path still works, with the same
persistence but no automatic resume.

### Phase 4 — control flow, schedules and triggers

**Control ports gate EXECUTION, not values.** A node fires the control outputs it
selected by returning reserved `_control` names; a node that declares control
inputs runs only when an upstream fired one of them (`flow.gate`). Downstream
values then follow ordinary dataflow, so a branch that did not fire is `skipped`
and everything below it is skipped too — the if/else semantics fall out of the
scheduler rather than special-casing.

New nodes (both registries, Python executors, `scripts`-free and bounded):

| Node | Semantics |
| --- | --- |
| `flow.gate` | Runs only when its control input fires — the branch primitive. |
| `logic.switch` | Fires the matching `caseN` (plus `matched`), else `default`; reports `caseIndex`. The card draws one output per configured case, labelled with its value. |
| `logic.map` / `logic.filter` / `logic.reduce` | Bounded (`10 000` items) numeric loops over the safe formula language. |
| `logic.wait` | Bounded pause; a durable job DEFERS itself instead of blocking. Cancel-aware; a TEST run allows at most 5 s. |
| `logic.stop` | Ends the run here with an explicit outcome (`status`, `message`, optional `value`); downstream nodes become `skipped`. |
| `flow.subworkflow` | Runs a **pinned** saved workflow (`workflowId@version`) as a nested run. |

**Durable waits.** In a durable Fabric job, `logic.wait` checkpoints itself and
pauses the RUN (status `queued`); the worker returns a `deferred` outcome and the
Fabric requeues the job at the wake time without consuming a retry, then resumes
from the wait checkpoint. It is bounded to 24 h and never holds a worker thread.
A non-durable run (e.g. `run_sync`) still blocks, capped at 30 s; a test run is
capped at 5 s by the compiler. A blocking wait polls cancellation every 100 ms and
reports the time it **actually** waited, so a cancelled run returns promptly
instead of holding a worker for the requested duration. Model settings stay in
memory across the deferral, so a worker restart before the resume still makes
agent nodes fail honestly.

**Subworkflows are pinned and authority-preserving.** The node's parent input is
delivered to the child's trigger as the run input; the child's terminal value
becomes the node output (`{nodeId: value}` when the child has several terminals).
The front door (`subworkflow-bundle.ts`) resolves every referenced workflow from
JDDS and sends the IRs with the run, so the runtime never reaches into JDDS. The
runtime compiles each child with the SAME grants, so a subworkflow can only
narrow authority, never widen it. Cycles and nesting deeper than 5 are refused
by both the bundle loader and the compiler. After a nested run finishes, the
service gives it its OWN durable run row (`<parentRunId>:<nodeId>`) with its node
runs and events, so it is inspectable from the child workflow's History; the
parent node returns that child `runId`. The inspector picks a saved workflow and
pins its version from a dropdown instead of typing ids.

**Loops are internal and bounded, never graph cycles.** The compiler still
refuses a cyclic graph. Iteration is a node with a pinned body workflow and a
declared cap:

| Node | Semantics | Bounds |
| --- | --- | --- |
| `flow.for_each` | Runs the body once per array item; returns `results` | `maxIterations` (≤ 1 000), `maxDurationMs` |
| `flow.repeat` | Runs the body `count` times, passing the previous output on | `count` (≤ 1 000), `maxDurationMs` |
| `flow.while` | Runs while a safe numeric condition holds (`value`, `index`, `iterations`) | `maxIterations`, `maxDurationMs` |
| `flow.parallel` | Runs the body once per array item **concurrently**; results keep the input order | `maxItems` (≤ 100), `concurrency` (≤ 8, never above the graph's `maxConcurrency`) |

**Parallelism.** Independent nodes already run in the same layer concurrently,
bounded by the workflow's `maxConcurrency` setting. `flow.parallel` is the
dynamic case: a bounded fan-out over data. Its concurrency is
`min(config, workflow.maxConcurrency, itemCount, 8)` — a node can never fan out
wider than the graph was authorized to, and never wider than a hard cap that
protects this machine. Results are collected in **input order** (`pool.map`
yields in order), so a run stays deterministic even though completion order is
not. Each item is a durable child run with the loop's id scheme
(`<parent>:<node>:i<N>`), so a resumed parent reuses a succeeded item. A failed
item is recorded as `{"error": ...}` in `results` and counted in `summary`;
`continueOnError` off makes the node fail naming the first failure. The body is
compiled with the SAME grants, so a parallel item can never widen authority.

Every iteration is its own durable child run (`<parent>:<node>:i<N>`), which is
also the loop checkpoint: a parent resumed after a crash reuses a succeeded
iteration's output instead of re-running it. `flow.for_each` can continue past a
failed item (`continueOnError`) and records the error in `results`; the other
shapes fail the node. A loop without a pinned body is invalid in both the editor
and the compiler.

**Schedules.** `trigger.schedule` is **zero-config**: the node declares no cron
or timezone. Its schedule is a `WorkflowSchedule` row (JDDS) linked by `nodeId`,
edited in the node inspector's **Schedule** tab. The tab is a builder — repeat
(minutes / hourly / daily / weekly / monthly / custom), a real time picker,
weekday chips, an IANA timezone select, catch-up policy and version pin — and it
previews the next three fires with the same cron engine the tick uses. The node
and the scheduler therefore cannot drift apart; `nodeId` is null only on older
schedules created from the execution dock, which still fire.

The **node card** shows the live schedule itself: an Active/Paused dot, a human
summary (`Weekdays at 09:00 · Asia/Karachi`), the next fire in that timezone, and
the last error when a fire failed. It refreshes after any schedule mutation and
once a minute as the tick advances `nextRunAt`; a node with no linked schedule
shows “No schedule yet — open the Schedule tab”. The Schedule tab remains the
editor.

A schedule whose linked `trigger.schedule` node has been deleted is **paused**
with an error on the next tick — it can never keep firing a workflow from a
trigger the graph no longer contains. Schedules created from the old dock (no
`nodeId`) are unaffected. For an unattended host, drive the tick from system
cron as described below; otherwise schedules only fire while the Eclipse server
is running.

**Workflow bots.** A saved workflow can be the brain of a bot in Bots mode:
*New bot → Brain → Workflow*, then pick the workflow. Each chat message is sent
as the workflow's one-off `runInput` (`{ message, botId, context }`) and the
bot replies with the graph's terminal output (a string, or pretty JSON). The
bot card and chat header show a **Workflow** badge; the workflow editor shows
that bot's avatar in its header. Scheduled workflow bots run through the
automation engine's `execute_workflow` action with
`action.config.graphWorkflowId`; the Python runtime cannot read JDDS, so it
starts the run through the front door and polls the graph run record. Both chat
turns and scheduled runs use the normal **read-only TEST envelope** — a
workflow bot cannot place orders, write files or hit the network just because
it was messaged or scheduled.

`src/lib/graph-studio/cron.ts` is a dependency-free, timezone-aware 5-field cron
evaluator (cached `Intl` formatters, 40-day search bound). A 60 s in-process
timer — started early in `instrumentation.ts`, before the long boot pipeline —
fires due schedules through the SAME test-run dispatch as the UI, so a scheduled
run can read and compute but never write, trade or use network effects. Each due
fire is **claimed atomically** (compare-and-set on the exact `nextRunAt`) before
a run starts, and a process-level lock prevents overlapping ticks, so two ticks
or two server processes cannot double-fire. Missed fires follow the schedule's
policy — `skip` (advance only) or `coalesce` (run once now) — so a closed app
never storms on boot. A fire records `started`; a later tick reconciles the
terminal run status, so “last status” cannot stay stuck at `started`. **Run now**
fires one schedule immediately in the same envelope without moving its clock.

**Triggers.** All three trigger nodes are zero-config. Manual = the toolbar /
UI; schedule = a `WorkflowSchedule` linked to the node (above); webhook = a
`WorkflowWebhook` linked to the node, edited in the inspector's **Webhook** tab.

The legacy `POST /api/workflows/[id]/trigger` route stays for local/CRON_SECRET
callers and keeps the test-run envelope. The real inbound endpoint is
`POST|GET /api/webhooks/[slug]` (public by middleware design, authenticated by
the handler):

- **Proof.** HMAC-SHA256 over `"<timestamp>.<raw body>"` with a 300 s replay
  window, or an `X-Eclipse-Token` header, or either — chosen per webhook. The
  secret is AES-256-GCM encrypted at rest (the connector-secret store), shown
  once on create/rotate, and never returned by a list response. A per-endpoint
  rate limit and an optional `X-Eclipse-Delivery` idempotency receipt prevent
  storms and double runs.
- **Safe mode (default).** The delivery starts the normal read-only TEST run:
  the webhook node emits the body/headers/query/method, but network, write and
  trading nodes are still refused.
- **Armed mode.** An operator can arm the webhook: it pins the current workflow
  version, content hash and the network grant. Deliveries run that pinned
  version as an operator run with only the confirmed grants. The digest is
  re-checked on every delivery; a missing/changed pinned version, a write,
  trading or agent/model node, or a graph that needs network without the grant
  is refused with `409`. `trigger.webhook` itself is `sideEffects: none` and carries the
  `webhook.receive` capability, so it never counts as an outbound network need.
- **Response.** `immediate` returns `202 { accepted, runId }`; `wait` polls the
  run for up to 30 s and returns `{ ok, runId, status, output }`, falling back to
  `202` on timeout. A dedicated "respond to webhook" node is deferred; the
  terminal output is the response body.
- **Node outputs.** `start` (the parsed body, also the run input), `headers`,
  `query`, `method`, `receivedAt`, and `meta`
  (`webhookId`, `deliveryId`, `signatureVerified`). The run carries this as a
  `triggerContext`; it is never written into the workflow.

`trigger.manual` is deliberately **zero-config**, like n8n's Manual Trigger: it
has no declared config fields. `Start` is the one-off run input (the Execution
dock's **Run input** editor, or the webhook request body) or `null`. The input is
bounded to 64 KiB, validated on both the editor and the route, and never written
into the persisted workflow. **Stored defaults belong in a `data.value` or
`data.set_variables` node, not on the trigger.** Workflows saved before this
change may still carry `payload`/`payloadType`/`seed` in their config; those keys
are no longer shown or declared, but `_exec_trigger` still honours them so an
existing graph does not silently change behaviour.

**Per-node testing.** `POST /api/graph-runs/node-test` → Python
`POST /graphs/nodes/test` runs ONE registered node (never a graph, never a
persisted run) with explicit per-port `inputs` and the node's declared config.
The runtime re-validates the config and inputs, enforces the node's own hard
timeout, and allows `none`/`read` side effects by default: write, trading, model
and local-write nodes are refused with a 403 and must go through an authorized
run. A `network` node is refused **unless** the operator explicitly confirms it
(`allowNetwork`): the inspector's Test tab and the card's hover Test action show
the exact target URL and make only that one request. The default remains
"refused"; the confirmation is never implied by a saved graph.

Every node exposes this in three places: the inspector's **Test** tab, a **▶
Test** button in the node's hover toolbar, and the footer of the **quick edit**
panel. A test runs with the current (possibly unsaved) config, pre-fills the
last run's inputs when available, and shows outputs, logs and duration. For a
trigger, the output is exactly the `Start` value it would emit. Test state is
ephemeral: a small dot on the card (running / succeeded / failed) plus the result
in the quick edit panel; nothing is written to the workflow or JDDS.

**In-node controls.** Parameters are edited directly on the node, not in a
popover. Two priority fields are always visible; `ConfigFieldDef.quick` (1 =
highest) chooses them, otherwise declaration order wins, and a **+N more
parameters** row expands the rest *inside* the node (with **Show less** to
collapse). Controls are Blender/ComfyUI-style: number fields drag horizontally to
scrub (Shift = fine, Ctrl/Cmd = coarse, click without dragging to type), selects
are dropdowns, booleans are checkboxes, text is an input and JSON is a compact
single-line value box. Every control dispatches the same `update_node_config`
patch as the inspector, so the two surfaces cannot diverge. A **single click
selects** the node (so the in-node widgets stay usable); **double-click** opens
the inspector for advanced details.

The hover toolbar's **Test** (play) button, next to Duplicate, runs the node
through the same safe endpoint. The result renders inside the node body: running,
succeeded or failed, duration, the error, and an expandable output preview. The
toolbar also keeps Duplicate / Delete / More. Nodes with side effects show a lock
instead of Test and explain that an authorized run is required.

**Recovery.** A run left `running` by a previous process is marked failed
(`interrupted by a restart`) when the runtime boot-wires the graph layer, so a
phantom in-flight row cannot survive a crash.

### Phase 5 — the finance + quant node pack

Executors live in `python/harness/graph_runtime/finance_nodes.py` and obey two
rules: **reuse the existing data path** and **reuse the tested math**.

| Node | Data / math |
| --- | --- |
| `finance.market_data` | Reads the runtime's **persistent bar series** (`harness/market/series_store.py`) — the same vault the research-data tools fill. No second ingestion. |
| `finance.indicator` | `sma/ema/rsi/macd/atr/adx/vwap/bollinger` via `harness/quant/indicators.py` (`compute_one`), with warm-up nulls, per-output series and `last`. |
| `quant.returns` | Simple or log returns over a period. |
| `quant.zscore` | Rolling standard score. |
| `quant.volatility` | Rolling std-dev of log returns, optional annualization. |

**Gateway-backed data.** `finance.quote` and `finance.history` call the EXISTING
`MarketDataGateway` (bound to the server bridge at boot), so live quotes and
OHLCV use the same provider router, quality/freshness metadata and durable
history cache as the agent tools. A gateway error is an explicit node failure
(`DATA_NOT_AVAILABLE`, `SYMBOL_NOT_FOUND`, …), never a fabricated price.
`finance.history` emits the standard OHLCV payload, so it feeds
`finance.indicator` directly.

**The rest of the gateway read surface.** `market_read_nodes.py` adds
`finance.instrument`, `finance.orderbook`, `finance.trades`, `finance.volume`,
`finance.market_cap`, `finance.fx_rate`, `finance.options_chain`,
`finance.bond`, `finance.funding_rate`, `finance.open_interest`,
`finance.supply`, `finance.tvl`, `finance.onchain`, `finance.liquidations` and
`finance.long_short_ratio`. Each one calls the SAME `MarketDataGateway` and
returns three outputs: the full record, a compact `summary` digest (spread,
funding rate, liquidation totals, …) so the node card and Output tab are
readable, and a `provenance` object derived from the record's own metadata
(provider, freshness, quality, as-of, retrieval time). A capability the gateway
rejects is the node's error, never a zero, an empty book or a guessed rate. All
are `market.read` / `read`, so a test run may execute them.

**Pure risk/return metrics.** `quant_metrics_nodes.py` adds
`quant.risk_metrics`, `quant.drawdown`, `quant.correlation` and `quant.beta`.
They compute over a series only — no engine, no provider, no state — so the
result is fully deterministic. `quant.risk_metrics` and `quant.drawdown` accept
an INDICATOR series, a raw number list or OHLCV bars (their close); the detected
source picks the default interpretation and `inputKind` still overrides it.
`quant.correlation` and `quant.beta` take INDICATOR series and support an
optional rolling window. An undefined ratio (a flat series has no
Sharpe ratio) is `null` with a note, never `0`.

Starter templates `Risk snapshot`, `Derivatives monitor` and `FX + bond yield`
wire the new read/metric nodes end to end; the template validation test builds
and validates all three.

**Validation: what a measurement is worth.** `quant_validation_nodes.py` adds
adapters over engines that already existed but were unreachable from a graph.
`quant.grade` runs `harness/quant/verdict.py` (`grade_edge`) over a
backtest's metrics, trades, optional Monte Carlo and optional result object,
and returns an A–F `verdict` with the four components, the flags and a
one-sentence `summary`. An axis it cannot judge (no Monte Carlo, no trades) has
its weight redistributed and is named in `assumptions.notGradedOn` — never
scored as zero or as perfect — and a critical flag caps the grade.
`quant.benchmark` runs `harness/quant/benchmark.py` against a benchmark OHLCV
series and returns alpha, beta, correlation, tracking error and information
ratio; it reads the equity curve from a backtest result, so that backtest must
run with `includeSeries` on (the error says so). Fewer than three aligned
returns is `available: false` with the reason, never a zero.
`quant.resample` runs `harness/quant/transform.py` to aggregate bars into a
wider interval — open=first, high=max, low=min, close=last, volume=sum in UTC
buckets — marks a bucket with too few members `partial`, and refuses a finer
target rather than manufacturing prices. It is the node that lets a 1m or 1h
feed drive a daily-parameter strategy.

Three more nodes answer "would this survive being traded?".
`quant.robustness` runs `harness/quant/robustness.py`: a one-at-a-time
parameter sweep (from `paramSpaceJson`, or the preset's documented default
space), a cost-multiplier curve (`costMultipliersJson`), trade concentration
and chronological regime splits, combined into a deterministic verdict —
`robust`, `fragile` or `insufficient_evidence` — plus plain-language
`conclusions` that each name the measurement behind them. A strategy is never
called robust unless every check passes.
`quant.trade_analysis` reads a trade log against the conditions it traded in
(`harness/quant/trade_log.py`): regime/trend/volatility at each entry,
per-regime P&L, concentration and coverage. A trade whose symbol has no bars is
counted in the totals and reported `unclassified` — it is never given a regime
it was not measured in.
`quant.multiple_testing` runs `harness/quant/multiple_testing.py`: the
**Deflated Sharpe Ratio** (Bailey & López de Prado 2014) over the selected
return series and the trial Sharpes — wired from `quant.optimize`'s
leaderboard/trials, or supplied as `trialSharpesJson` — and the **Probability
of Backtest Overfitting** (CSCV) over a `returnsMatrixJson` of one return series
per configuration. The node de-annualizes trial Sharpes with
`periodsPerYear`, because the engine's deflation formula is defined on
per-period Sharpes. A missing input is `available: false` with the reason; when
neither statistic can be computed the node fails and names both.

**Structure: timeframes, regimes, size.** `quant_structure_nodes.py` adds the
three pieces a single-series workflow was missing. `quant.align` runs
`transform.align_rows` over a `{symbol: OHLCV rows}` map and returns the shared
timestamp axis plus per-symbol series and missing counts; `fill: none` is the
default and keeps a gap `null`, while `ffill`/`zero` impute only when asked and
the payload's `imputed` flag records that they did. `quant.regime` runs
`regime.classify_series` and returns, per symbol, the trend × volatility label
and confidence for every bar, the label counts and `warmupBars` — the leading
bars before the trend filter has a reading are `flat`, and the payload says how
many there were. `quant.position_size` runs `sizing.compute_quantity` with the
same `PositionSizing` contract the engine executes, so the size a graph computes
is the size a backtest would have used: `percent_risk` needs the wired `stop`
distance, `atr` needs the wired ATR, Kelly needs the configured win rate and
payoff, and a method whose input is missing fails naming it rather than
returning zero. The quantity is a plain number, so it feeds an order intent
(the trading nodes take a full intent, not a bare size).

**Portfolio, costs, report.** Three Tier-3 nodes close the remaining gaps.
`quant.portfolio_backtest` runs `harness/quant/engine.py`'s
`run_portfolio_backtest` over the same `{symbol: OHLCV}` map `quant.align`
produces — one shared book, shared cash and one equity curve, with each
strategy bound to its own symbols. By default it builds one preset strategy per
symbol in the map; `strategiesJson` replaces that with explicit
`StrategySpec`s that must each declare `universe.symbols`. A symbol claimed by
two strategies, referenced but absent from the map, or an invalid spec is an
explicit failure (the engine's own conflict check is what rejects the double
claim). `quant.costs` runs `harness/quant/costs.py` for one cost family —
`transaction`, `slippage`, `spread` or `impact` — over an order list and returns
per-order values, the total and the exact model used. The execution block is
passed as `executionJson` (the engine's own `commission`/`slippage`/`spread`/
`marketImpact` shape); a model that lacks its input (volume for impact, a bar
spread for historical spread) contributes zero **and says so in a warning**,
rather than inventing a cost. `quant.edge_report` runs
`harness/quant/edge_report.py` to produce the Markdown Edge Report and its
bounded chart specs from the same artifact the verdict grades; a report without
a Monte Carlo or an equity curve names the missing figures instead of drawing
empty ones.

**Instruments: options and futures.** `quant_instruments_nodes.py` adds the two
instrument-specific nodes. `quant.options_strategy` runs
`harness/quant/options.py` over a leg list
(`[{type, strike, quantity, premium}]`, optional `multiplier`/`iv`) and returns
the expiry payoff curve, break-evens, net premium, max profit/loss and
boundedness. A leg set with an unbounded tail reports `maxProfit: null` with
`bounded: false` — it never invents a finite maximum. Net Greeks are added only
when **both** a valuation spot and a time-to-expiry are wired, because a Greek
without them would be a fabrication. The payoff range is either configured
(`spotMin`/`spotMax`) or derived from the strikes and the wired spot; the
payload records which in `spotRangeSource`. `quant.futures_continuous` runs
`harness/quant/futures.py` over ordered contracts plus `rollDatesJson` and
returns an OHLCV series with `none`/`difference`/`ratio` adjustment. The output
is explicitly a **proxy** (`proxy: true`, the engine's own note): a continuous
series is not a deliverable contract, and the most recent contract stays
unadjusted.

**Quant backtest.** `quant.backtest` adapts the graph's OHLCV payload into a
`BarSeries`/`MarketDataset` and calls `harness/quant/engine.py`. It accepts the
documented `ema_cross` and `rsi_reversion` presets, or a full `StrategySpec` JSON
(`strategyJson`). The input series symbol is authoritative (a JSON strategy
cannot backtest a different symbol). It returns the full result object, the flat
metrics, and the trade list; series curves are omitted unless `includeSeries` is
on. Runs are deterministic (`randomSeed`) and bounded by `maxBars`.

**Walk-forward.** `quant.walkforward` rolls train/test windows through
`harness/quant/walkforward.py`; parameters are optimized on the training window
only and the chosen configuration is evaluated out-of-sample. The report carries
in-sample, out-of-sample and combined metrics plus the IS/OOS divergence. A
default parameter space is derived from the preset; a custom strategy needs
`paramSpaceJson`. Insufficient history is returned as an honest
`INSUFFICIENT_HISTORY` status, not an exception.

**Optimizer, Monte Carlo and OOS holdout.** `quant.optimize` drives
`harness/quant/optimize.py` over `paramSpaceJson` (grid/random/evolutionary/
bayesian) and returns the best trial, leaderboard, stability and Pareto set -
it reports a choice, it never auto-adopts one. `quant.montecarlo` drives
`harness/quant/montecarlo.py` (trade reshuffle, return bootstrap, regime blocks,
parameter/execution perturbation) and returns return percentiles, loss/ruin
probability and drawdowns. `quant.oos` makes a chronological split (fraction or
index), backtests both sides with the SAME deterministic engine, and reports
in-sample vs out-of-sample metrics, their objective divergence and a
block-bootstrap confidence interval for the OOS mean return. A materially
weaker OOS objective is an explicit `IS_OOS_DIVERGENCE` warning, never a
silently "validated" strategy.

**Research agent.** `agent.research` is a thin adapter over the EXISTING agent
run plane (`AgenticService.run`), not a second agent loop. Its `query` input is
optional: the node runs on a wired query **or** the configured objective (a
node with neither is invalid at compile time). It resolves the named agent
preset and its tools, caps the turn at `maxSteps`, and returns a structured
`finding` plus the run's `research` evidence object. The adapter treats the run
plane's terminal `completed` status (and `succeeded` for substitutes) as
success; a failed/cancelled run reports its own error message. Agent nodes are a
**`model` side effect**, not `network`: a test/dry run may call the model while
its tool surface is narrowed to `DRY_RUN_TOOLS` (read-only finance, local
evidence, response helpers) — web research, browser, MCP, plugins, code and
broker tools are removed. An operator run keeps the preset's declared surface.
The front door resolves model credentials from the vault and sends them in
memory only; `GraphService` deliberately excludes them from the durable Fabric
job payload. A restart before the agent node runs therefore makes that node fail
honestly instead of persisting a key.

**Research & evidence.** `research.evidence`, `research.citations`,
`research.conflicts`, `research.completeness`, `research.claim_verify` and
`research.report` read the existing `EvidenceStore` from the web research
engine: evidence, claims, citations, conflicts, coverage, deterministic claim
verification and a Markdown report. They are local reads (`research.read` is
granted to test runs) - no web request and no model call.

The analysis pack (`research_analysis_nodes.py`, all pure `research.read`) adds
the questions the raw reads cannot answer: `research.latest` resolves an exact
run or the newest run matching a question; `research.sources` lists stored
sources with domain/type/date filters and content/family dedupe;
`research.gaps` names unanswered questions, rejected claims and uncited
material claims; `research.freshness` applies the engine's fail-closed
point-in-time rules and returns every rejection reason; `research.conflict_scan`
runs the deterministic detector over evidence that was never scanned;
`research.citation_audit` builds citations when none are wired and validates
traceability; `research.supersession` exposes restatement/amendment chains;
`research.quality` surfaces the engine's explainable quality score with its
disclaimer; `research.entities` reads the evidence-anchored entity graph; and
`research.claims_verify` verifies many claims at once. All of them may run in a
test run. Conflict rows are normalized at the read boundary (legacy `OPEN` is
`UNRESOLVED`, `DISMISSED` is `RESOLVED`) and `candidates` is decoded, so the
nodes and the engine speak one vocabulary. `report` no longer fails without a
run row: it emits an `UNAVAILABLE` answer from the stored rows and reports
`runRow: false`.

Two bounded local writes complete a cold-start graph:
`research.start_run` creates the run row (question/objective/as-of) and is
idempotent per question/objective, and `research.snapshot` freezes selected
sections into the store's immutable snapshot table. Both declare
`research.write`, which is not a test-run capability, so a test run is refused
by the existing compiler gate.

The network adapters reuse the same engine and bridge:
`research.fetch_many` fetches 1-10 known URLs (search results) with bounded
concurrency, content-hash dedupe and optional evidence persistence, and reports
a failed URL as a row while an entirely failed batch is an explicit error;
`research.scrape` collects table/record rows through the bridge's `web_collect`
capability with no model; `research.extract_facts` runs the engine's
model-assisted, span-validated fact extractor over fetched pages — a proposal
that does not validate is a rejection note, never evidence; and
`research.deep_dive` invokes the existing `ResearchEngine` end to end with its
own budget caps and a hard timeout, returning the run id so the local read
nodes can inspect it. All are network effects (except `extract_facts`, a model
effect) and are refused in a test run.

External research can be brought into the same store with
`research.ingest`: it normalizes an `agent.research` blob, an MCP tool result or
a manual claim set into `Evidence`/`Claim`/`Source` rows, keeping the origin and
`eclipseVerified: false` on every row. It never verifies a claim and never copies
an external answer as the run's answer; a later `research.claim_verify` is the
deterministic local check. `research.diff` compares two runs answering the same
question (snapshots preferred when present): evidence added/removed by content
hash, claim-status changes, opened/resolved conflicts, source changes and
coverage deltas. Runs answering different questions are refused as
incomparable rather than loosely diffed. `research.queries` closes the loop: it
turns a question, the named gaps and weak claims into concrete search queries
(including a disconfirming query per weak claim and an optional primary-source
`site:` query) with no store, network or model, so the plan is inspectable and
reproducible before any search runs. The `Research brief` starter template wires
the read path end to end: stored run → evidence → conflict scan → citation
audit → report.

The live half reuses the same engine instead of inventing a second web path:
`research.web_search` calls the configured search-provider chain (results are
pointers, never evidence) and `research.web_fetch` fetches one page through the
SSRF-hardened `web_fetch` bridge capability and returns its extracted text. When
`web_fetch` has a `researchId`, it saves the same `Evidence` the `web_extract`
tool saves, so a graph can search, fetch, then verify with the local nodes.
Search over-fetches, then applies the domain filters and URL dedupe, so the
requested count is not silently reduced; crawl reports
`linkDiscovery: "failed"` instead of pretending a page had no links.
`research.claim_verify` now applies the run's conflicts plus `asOf`/`freshness`
(the same rules the engine uses), and with no wired ids it falls back to the
claim's recorded evidence links.
Both are declared `network` side effects: a test run refuses them and an
operator run must explicitly confirm network access. They depend on the app's
data bridge (`context.callbackBaseUrl`), so a headless runtime with no front
door fails honestly rather than fetching directly.

**Paper trading & safety.** `trading.risk_check` is a pure pre-trade gate over
`harness/quant/risk.py` (position, gross exposure, leverage, open-position,
daily-loss and risk-per-trade limits; `0` disables a limit). It may reduce an
order's size or reject it, and a rejection is an outcome with reasons, not an
exception. It reads the state it gates on from a wired account and position list
(`broker_account` / `broker_positions` / the paper account) before falling back to
its config, and its `state` output names the source of every figure (`input`,
`account`, `positions` or `config`) so a gate cannot silently run on a stale
number. Because it has no side effect it also runs in `research` and `backtest`
mode, so a backtest can gate entries with the same limits as the live path.
Gross exposure and per-symbol value stay explicit: a position list carries lots,
not notional.

`trading.paper_order` is the execution adapter: it fills at a
supplied reference price with explicit slippage and commission and returns a
paper fill. By default it also records the fill in a durable PAPER account
(`GraphPaperAccount` / `GraphPaperFill` on `agent-memory.db`): cash, signed
position, average cost and realized PnL survive across runs, so a scheduled
paper graph keeps its book. Accounting is cash + cost basis, not a
mark-to-market valuation, and the fill price is always the one the graph
supplied. It never contacts a broker, never reads a live quote, and only runs
in PAPER mode behind the operator's paper-trading confirmation. There is no
live adapter; LIVE mode is refused.

**Broker reads and exits.** `trading.broker_account` and
`trading.broker_positions` read the broker's own account and position rows;
`broker_orders` and `broker_fills` read its working orders and reconciled deals;
`broker_quote` and `broker_bars` read its quotes and OHLCV series (with a
deterministic ATR(14), 20-bar range and contract size derived from the bars).
Every read and every exit node returns `ok` and `reason` alongside the payload, so
a `logic.condition` branches on a boolean instead of string comparing `status`.
`trading.order_ledger` reads Eclipse's own submitted-order rows (with the
`runId`/`nodeId` that placed each) and `trading.paper_account` reads the durable
PAPER book, emitting its positions as rows so they can feed a risk check.
`trading.broker_close` closes one ticket (full or partial), a
whole symbol, or everything; `broker_modify` and `broker_cancel` move a bracket
or cancel a working order. `trading.broker_modify_order` re-prices a **working**
order (keeping its type/time/expiration, read back as `verified`) and
`trading.broker_symbol` returns static symbol facts with no tick required — trade
mode, contract size, volume limits, stops/freeze levels — so a closed market is
still answerable. All of these are risk-reducing or reads: they carry no mandate,
approval or live arm — but they still require a pinned `connectionId`.

**Building and dry-running an order.** `trading.order_intent` builds the validated
intent (`quantity`/`volume`, market/limit/stop, SL/TP, time-in-force) instead of
hand-assembling a JSON object; a pending order without a price is an error.
`trading.order_preview` then answers "would this be allowed?" through the broker's
`order_check`, the connection policy, the money guardrails and (optionally) the
mandate scope — sending nothing and creating no approval card. `ok` means the
check ran; `allowed` means the order would pass. `trading.broker_order` with
`dryRun` takes the same path, so a workflow can be validated before it can send.

**Trading-system primitives.** Pure nodes that never touch a broker:
`trading.size_by_risk` (equity risk budget + stop distance → volume, floored to
the symbol step and capped at its maximum), `trading.bracket` (ATR × multiple or
an explicit stop distance → direction-aware SL/TP with reward:risk),
`trading.spread_guard` (a trade passes only while the broker's spread is within
the cap) and `trading.session_filter` (broker `tradeMode` plus the broker's own
`sessionDeals` in server time when the terminal reports them, then an explicit
`sessionsJson` calendar, then an explicit UTC window; `basis` says which). Three
risk-reducing management nodes
need no arm, approval or mandate: `trading.break_even` (moves the stop to entry
after `triggerR` of profit measured from the position's own stop),
`trading.trail_stop` (tightens only, never widens) and `trading.partial_close`
(a fraction or explicit volume, floored to the symbol step from a wired quote or
the broker's own `symbol_info`). Each reports `moved`/`closed` plus `ok` and the
broker's refusal reason.

**Lifecycle and reliability.** `trading.await_fill` polls the ledger to a terminal
state or a deadline (`timedOut` = still working, not an error). `trading.reconcile`
refreshes the durable broker mirror; `trading.drift_check` reports in-flight ledger
orders with no broker match, broker positions the ledger never recorded, and a
mirror older than `maxAgeSeconds` — `syncFirst` refreshes the mirror first (the
broker stays authoritative) and remaining drift is only reported. Both
`trading.broker_order` and `trading.paper_order` take
`dedupe: none | key | day`: the claim is recorded before the send, so a scheduled
graph that fires twice sends once and a skipped run returns `status: "SKIPPED"` with
`skipped: true`. A refusal that never reached the broker
(`REFUSED`/`CONFIRMATION_REQUIRED`/`COOLING_OFF`) gives the claim back, while
`REJECTED`/`UNKNOWN` keep it; a test run never writes the broker mirror, so
`trading.reconcile` and a `syncFirst` drift check are refused there.
`trading.paper_valuation` marks the PAPER book to prices the graph
supplies (equity, market value, unrealized/total PnL); a symbol with no price is
listed in `unpriced`, never valued at zero.

`trading.broker_order` is the demo-broker adapter added in P3. It takes an
`intent` plus a `connectionId` and `mandateId`, runs only in PAPER mode, and calls
the SAME `BrokerService.submit_order` path as a manual order. An optional wired
`decision` is honored: a rejected risk check stops the order before it reaches
the broker, so wiring the gate to the intent alone can never send a refused
order. `ok` is true only for a filled or placed order and `reason` carries the
refusal. The operator-created
mandate is the authorization (allowed symbols, per-order `maxMargin`, `maxOrders`,
expiry; immediate revoke), and each use is counted atomically. The connection must
be a demo MT5 connection; live trading nodes remain refused by the compiler and the
front door. The ledger row records the `runId` and `nodeId` that placed it. See
`docs/trading.md`.

**Custom agent presets.** Settings › Agents creates an operator-authored preset:
a name, system prompt, max-step bound, an explicit tool allowlist and optional
MCP/plugin expansion. Presets are stored on `agent-memory.db` by the runtime
(`AgentPreset` + immutable `AgentPresetVersion`), so a preset is runtime state,
not a Next.js setting. Every edit appends a version; the agent node pins
`agent@version` (0 = current), and a pinned run keeps its snapshot. A preset that
names a write/external tool requires the explicit “Allow effectful tools”
acknowledgement; unknown tool names are refused at save time. The three built-ins
remain code-resolved in `agents.py`. Presets resolve on EVERY run path through
`graph.resolve_agent` → `agent_presets.agent_config_from_preset`, the same
projection `graph_runtime/bootstrap._agent_runner` uses — so one preset behaves
identically in a graph node, an interactive bot turn and a bot's scheduled
`run_agent` (which enters through `resolve_agent`, not the front door). A saved
id the runtime does not know still raises the honest "Unknown agent" error.

**Planner and delegate.** `agent.plan` makes ONE bounded model call with a strict
step schema and returns 1–8 steps (stable ids, optional `dependsOn` on earlier
steps). It never executes them. `agent.delegate` runs each task as one bounded
turn on the existing agent plane (up to `maxTasks`, default 4) and returns every
finding; a failed part is reported as `failed` with its error and the summary is
`PARTIAL`, never a hidden success. Both are `agent.run` + `model` nodes, so a
test/dry run may execute them with the narrowed read-only tool surface, while an
operator run keeps the preset's declared surface. Neither re-implements the
agent loop or tool registry.

**Human approval.** `logic.human_approval` is the gate an unattended run needs.
It creates ONE approval of kind `graph_approval` in the runtime's existing
single-use approval store (the same rows the Permissions panel decides), keyed by
`graph:<runId>:<nodeId>` + a digest of the request text, and passes only after an
operator approves; a denial fails the node and names the approver. The decision
is **not consumed** — it gates that run+node, so a resumed run reads the same
answer instead of asking again. A durable run does not hold a worker: the node
raises `DeferredWait(..., rerun=True)`, which the executor treats differently
from a time wait — the node is left **PENDING** (nothing checkpointed) and the
resumed job re-enters it. A synchronous run polls up to `maxWaitS` (default
300 s) and then fails honestly, leaving the request waiting in Approvals. A test
run never creates a request.

**Committee, charts and notifications.** `agent.team` runs the Institution
committee through the existing team runtime (run → wait → finalize → synthesize)
and returns the reconciled `decision`, the deterministic `synthesis` and the
terminal `status`; COMPLETED/PARTIAL succeed, anything else fails with the
status. It never places an order, never registers a trigger and refuses a test
run (a committee is many model calls plus task/A2A state). `output.chart`
normalizes a payload (a full spec, a number list, `{values, categories}`,
`{series, categories}` or `{rows}` with X/Y fields) into a validated `ChartSpec`
and the plotly figure the chart engine draws; when plotly is unavailable it
returns `rendered: false` with the spec rather than a fake image.
`output.notify` sends one message through the ONE channel service
(`send_outbound`) to a configured channel + destination; a provider refusal fails
the node with its reason, and the `network` effect means a test run can never
send. `research.source_rank` is pure: it ranks evidence by the `confidence` and
freshness the items ALREADY carry (exponential decay of publishedAt/retrievedAt
with a configurable half-life); an item with neither is sorted last and counted
`insufficient_signal` — no score is invented.

**Utilities and memory.** Three deterministic primitives make unattended graphs
stoppable and readable: `logic.assert` fails the node (and, with fail-fast, the
run) with a named message unless the value is `truthy`, `falsy` or `present`
(null/`""`/`[]`/`{}` are absent — the same rule `logic.merge` uses);
`logic.fallback` returns the first *present* of three inputs plus which port it
came from; `data.extract` reads a dotted path out of an object/array (a numeric
segment indexes a list) and reports `found: false` + null — or fails by name when
`onMissing` is `error` — rather than inventing a value. `memory.recall` and
`memory.remember` are thin adapters over the ONE Eclipse memory service, so its
policy, dedupe/conflict resolution and retention apply; `recall` returns the same
bounded `context` block an agent receives. `remember` writes durable state, so it
declares the `memory.write` capability and a test run is refused (at compile
time by the capability envelope, and again in the executor).

The memory pack now covers the read/write lifecycle. `memory.context` produces a
deterministic prompt block under a token budget (lowest-importance records are
dropped first and the count is reported, no model call); `memory.list` browses by
scope/type/status without inventing a query; `memory.update` corrects
content/subject/importance; `memory.forget` soft-archives (never hard-deletes).
Scopes are `general`, `workflow` (`workflow:<id>`), `run` (`run:<id>`) or a
literal named scope; a missing workflow id fails by name rather than silently
writing to `general`. Update/forget are operator-run only.

**Agent task (n8n-style).** `agent.task` is the general Agent node: Instructions
+ Task + Agent preset + tool allowlist + memory mode + output shape. It runs ONE
bounded turn on the existing agent plane; instructions are appended to the
preset's system prompt and the tool allowlist can only NARROW the preset (an
unavailable tool fails by name). `memoryMode` is `none`/`read`/`write`/
`read_write`; a write stores the final answer as a `summary` memory and is
refused in a test run. `outputMode: json` parses the answer and checks it against
`outputSchemaJson`; a mismatch returns `data: null`, `valid: false` and the
issues — never a coerced object. The wired Input and Context are appended as
bounded JSON, never spliced in as instructions.

**Vision images (read-only).** `image.attach` uploads/references an image in the
vision asset store; `image.analyze` (describe/answer/ground/extract),
`image.ocr`, `image.read_chart`, `image.compare` and `image.batch` wrap the ONE
vision engine (`vision/orchestrate`) — no rendering, no image generation. Every
result is labelled `kind: "observation"`, `verified: false`, with the
provider/model/warnings. `image.read_chart` sets `verified: true` only when the
engine's own market-data check confirmed the reading. `image.batch` applies the
same 1–5 questions to 1–8 candidate images and returns one row per image; a
failed image is a row with `ok: false` and the batch never aborts silently.
Vision calls are a model effect (no tools), so a test run may read an image.
Image assets are **owner-scoped**: the front door forwards the verified identity
token, the runtime resolves the owner from it (never from a query string when
auth is on), and a non-local owner gets a scoped asset id. Graph runs use the
same verified owner, so a node reads only the assets its run owner uploaded.
Uploads are bounded and rate-limited per owner. Existing image rows migrate with
`ownerId='local'`, so if authentication is enabled after upgrading, re-upload
(or deliberately migrate) older images for non-local users.

**Structured calls.** Four nodes are ONE bounded model call each, not an agent
turn: `agent.classify` (text → the one declared label + confidence + scores),
`agent.extract` (text → a JSON object, checked against a small schema subset:
`type`/`required`/`properties`/`items`), `agent.summarize` (string, array or
object → summary + key points) and `agent.critique` (finding + evidence →
`supported`/`weak`/`contradicted`, issues, score). They use the model runtime
directly with a strict response schema, so they cannot reach a tool at all and
they have no `agent`/`maxSteps` knobs — there is no preset or tool loop to
configure. The node owns the contract: a label outside the declared set, a
verdict outside the three, a confidence/score outside 0..1 or an empty summary
FAILS the node instead of being coerced; an extraction that does not match the
schema reports `valid: false` with the issues on the run log rather than
pretending the shape matched. `agent.critique`'s `score` is nullable because the
model may omit it, and the node never invents one. They are `agent.run` +
`model` nodes, so a test/dry run may call the model (no tools are bound at all).

**Saved bots.** `agent.bot` runs a bot that already exists in Bots mode. It
resolves the id against the automation store at run time (bound at boot), checks
the same bot rule the workspace uses (`bot`/`monitor` tag or an `mon_` id), and
runs ONE bounded turn with the bot's own agent preset and objective, so a saved
bot can be a step in a graph. It never registers a trigger and never runs the
bot's schedule — the graph is the caller. The bot's provider/model pin is applied
to the turn (credentials and base URL still come from the graph run's in-memory
settings), so the same bot answers with the model it uses when scheduled. A
**workflow** bot is
refused by name (`use flow.subworkflow`): nesting a scheduler run inside a graph
run is a second execution plane. An optional `context` object is appended to the
objective as bounded, labelled `Context (JSON)` data, never as instructions. A
deleted bot makes the node fail by name; the inspector's picker lists the live
bots and marks workflow ones disabled rather than hiding them.

**Skills.** `skill.invoke` runs an installed skill through the EXISTING skill
runtime (`AgenticService.run_skill` → `skills/runtime.execute_skill`); the node
only decides *which* `skill@version` and the inputs. It is ONE generic node, not
one per installed skill: skills are runtime data while the registry is a static
contract mirrored by the manifest, so the inspector resolves `skillId` against
the live skill list (`listSkills`) instead. `inputJson` and the optional `input`
port are merged per key, the wire winning. Status is the contract: only
`completed` produces `output`/`result`/`evidence`; `blocked`, `denied`,
`failed`, `cancelled` and `unavailable` all FAIL the node with the runtime's own
code and message (a failed skill's partial transcript is never surfaced as an
output). The skill's permission check runs in code inside the runtime; the node
passes the run's `operator` flag (an operator run is `not testRun`) so an
operator-authored permission set is honoured. A test/dry run may execute a skill
only when every declared tool is in the read-only `DRY_RUN_TOOLS` set — otherwise
the node fails and names the tools instead of running the procedure with its
tools silently removed. It reuses the `agent.run` capability (a skill call is a
model call on the same plane), and its `skills` category is the "Skills" group in
the node library.

**Chat-to-graph.** `POST /api/graphs/plan` → Python `POST /graphs/plan` turns a
natural-language objective into a candidate workflow. The model receives the
runtime's ACTUAL node catalog (types, ports, config defaults, side effects) and
returns strict JSON. `graph_runtime/planner.py` accepts the canonical edge fields
plus common model aliases (`from`/`to`, `sourcePort`/`targetPort`, `{node, port}`
handles), then validates every node type,
port compatibility, config and edge, derives `requiredCapabilities` and picks
`research` mode - or `paper` when the proposal contains a trading node - and
dry-runs the compiler. Only a plan with zero errors becomes a `WorkflowIR`
(saved by the normal workflow create path, never automatically executed); a
rejected plan returns its diagnostics. Model credentials are resolved by the
front door and used in memory only. The planner is one bounded model call: no
auto-repair loop, no subworkflows, and no live mode.

**Creating a workflow from chat (terminal mode).** The ChatBot can create a saved
workflow from a one-sentence idea: the `create_workflow` tool
(`python/harness/graph_tools.py`) posts the objective to `POST
/api/workflows/generate`, which resolves the model credentials, runs the SAME
planner above, re-validates the returned IR and writes it through the normal
workflow store. The tool returns the saved id/name plus node/edge counts; it
never runs the graph, and it needs no approval (it is a reversible catalog
write — `create_workflow` is in `GRAPH_WRITE_TOOLS`, so `plan_only` removes it).
A plan that does not validate is reported with the planner's diagnostics and
nothing is saved. Nothing else in the chat surface writes workflows, and the
gallery refreshes when its keep-alive tab becomes visible again, so a workflow
created from chat appears without a reload.

**In-editor assistant (tool-calling, not one JSON blob).** `POST
/api/graphs/assist` → Python `POST /graphs/assist` is the assistant that edits
the graph the operator is looking at. It deliberately does NOT ask a model to
emit a whole workflow: `graph_runtime/assistant.py` runs a bounded tool loop
(max 12 rounds, max 30 operations per turn) over a **working copy** of the IR.
Tools are `search_nodes` and `get_node_schema` (both read the real registry, so
the model can find "any node" without holding all ~170 definitions in context),
`add_node` (optional `after_node_id` auto-wire), `connect_nodes` (typed-port and
cycle checks), `update_node_config` (coerced + validated), `remove_node`,
`rename_node`, `validate_graph` and `ask_user`. Ambiguous requests get a
question instead of a guess.

Every mutating tool records one operation in the same closed vocabulary the
editor already understands (`add_node`, `add_edge`, `remove_node`,
`update_node_config`, `update_node_label`, `remove_edge`, `set_settings`,
`set_meta`). The server compiles the final document before returning it, so an
operation list that would produce an invalid graph is reported with diagnostics
and never applied. Trading nodes switch the working copy to `paper` with an
explicit warning. The client (`assistant-client.ts` → `assistant-ops.ts`) maps
ops to `Patch[]` and applies them through `dispatchMany`, which makes the whole
turn ONE undo step. Safe, warning-free edits are applied to the canvas
immediately (n8n-style) and the canvas centres the new nodes; a proposal with
warnings or validation errors is shown for review with Apply/Discard. The panel
is the editor's right sidebar (`assistant-panel.tsx`): a vertical tab strip
(Assistant / Node / Issues / History / Health) with a resizable width and
persisted open state (`be_graph_rail_v1`). It is a layout column, not a modal —
there is no portal, backdrop or overlay, and selecting a node switches tabs
without destroying the conversation. Credentials never cross the bridge:
secret-like config keys (`apiKey`, `token`, `secret`, `password`,
`authorization`, …) and `headersJson` are redacted from the graph context before
it reaches the model. If the configured model returns prose without calling a
single tool, the response says so instead of pretending the graph changed.

The same turn also streams over SSE (`POST /api/graphs/assist/stream` →
Python `POST /graphs/assist/stream`): `assistant.py` yields a `run` event, then
`text_delta`, `status`, `tool_start`, `tool_end`, `result`, `cancelled` and
`error` events from ONE generator, so the stream can never drift from the
request/response API. Text arrives token-by-token via the model runtime's
`stream()` seam (a runtime without streaming falls back to one text delta). The
first event carries a `runId`; the panel's Pause / Resume / Stop controls call
`POST /api/graphs/assist/runs/{id}/pause|resume|cancel`
(Python `/graphs/assist/runs/{id}/…`). Pause is cooperative: the loop checks
before each model round and before each tool call, so a tool result is never
cut in half. Stop cancels the run and the route also cancels on client
disconnect. If the SSE connection drops, the client polls
`GET /api/graphs/assist/runs/{id}/status` and recovers the validated result from
the run record instead of re-running the model. Run state lives in
`graph_runtime/assistant_runs.py` (live control plane only; the transcript and
document stay with the editor). The control plane is deliberately in-process: a
hard runtime restart loses an in-flight model call, but the durable transcript
and pending approval survive, so the turn can be retried honestly. A large
graph sends full configs only for the selection and its neighbours (bounded
`MAX_FULL_CONFIG_NODES`); the rest carry type/label only. The transcript is
persisted per workflow in `localStorage` as a small thread list (title, messages,
pending proposal, document id), so a reload keeps the conversation and restores
a pending approval card. The request also carries the last run's status, failed
node and error, so a failed run offers **Fix the failed run** and the model can
propose a targeted correction.

Human decisions are explicit. With Auto-apply off (the request carries
`requireApproval`), a validated edit creates a single-use `graph_edit` row in
the SAME `ApprovalStore` used by effectful tools; the panel renders Allow once /
Always allow / Deny (the decision POSTs to
`/api/harness/mcp/approvals/{id}/decide`). The assistant's own `run_workflow`
tool is gated the same way (a `graph_run` approval) when Auto-apply is off — the
stream emits `approval_requested`, waits on the same store, then continues or
records a refusal. `ask_user` likewise creates a real `clarification` row and
the panel renders an answer card, not plain prose. A denial is terminal for that
operation digest, and "Always allow" creates a standing grant scoped to the
workflow (`graph_edit:<workflowId>` / `graph_run:<workflowId>`). Approvals are
bound to the workflow and the exact normalized op list, so a proposal cannot be
applied to a different document.

The assistant can also close the repair loop itself. `run_workflow` executes the
working graph through the SAME `GraphService.run_sync` as the editor, but always
as a **test run** (`RunGrants(test_run=True)`): network, trading and external
writes are refused by the compiler, and the model receives that refusal plus
the diagnostics. It returns the status, every failed node with its real error,
and the terminal output, so the model can fix a config/node and run again. The
run tool is bounded to `MAX_ASSIST_RUNS` (2) per turn and is never offered in
Ask mode. An effectful graph still needs the operator run (header Start →
authorization dialog); the assistant can read that run's failure through
`lastRun` and fix the graph, but it cannot grant itself network/trading/write.
Test runs are persisted like any other run, so the user can inspect them in the
History panel.

Auto-apply is a per-browser preference
(`be_graph_assistant_autoapply_v1`, on by default); turning it off makes every
edit wait for the approval card (and creates the `graph_edit` approval row).
A proposal is bound to the IR id it was computed against, so switching
workflows can never apply it to the wrong document. Tests:
`python/tests/test_graph_assistant.py`,
`python/tests/test_graph_assistant_runs.py`,
`src/lib/graph-studio/assistant-ops.test.ts`,
`src/lib/graph-studio/assistant-store.test.ts`.

**Tabular transforms.** `transform.select`, `transform.sort_limit`,
`logic.aggregate`, `logic.deduplicate` and `transform.join` operate on arrays of
JSON objects (bars, fundamentals, filings, screener rows) with dotted field
paths. They are bounded to 100 000 rows, deterministic, and refuse non-object
rows. `logic.aggregate` supports sum/avg/min/max/count/median/stddev/percentile
with optional `groupBy`; `stddev` is the sample form (n-1), `percentile`
interpolates linearly at the configured `percentile` (0–100, default 50), numeric
strings are coerced, and groups are ordered by value (a group key of `"10"` sorts
after `"9"`, not before it). An empty input is `null` for every op except `count`
(which is `0`, because zero rows really is zero) — the node never invents a
zero that a downstream consumer could mistake for data. `logic.deduplicate` uses
the same identity rule as `transform.join`, so `1`, `1.0` and `"1"` are one row.

Paths resolve identically everywhere (`graph_runtime/values.py`): a missing
segment reads as `MISSING`, which the node turns into a null column, a dropped
row or an error via `transform.select`'s `onMissing` — never a silent null.
`transform.select` keeps the typed path as the column name unless `alias=path`
was written (`pe=fundamentals.pe`), and refuses a `rename` that maps two columns
to one name.

`transform.sort_limit` coerces numeric strings, so `"100"` sorts as a number and
not lexicographically; `field` accepts several keys (`date desc, symbol`) with
`order` as the default for keys without a suffix; nulls always sort last in both
directions; `caseInsensitive` folds text keys; `limit` 0 means "no limit" and
`offset` windows the rows.

`transform.join` normalises keys (`1`, `1.0` and `"1"` match; `caseInsensitive`
for text), accepts comma-separated composite keys (`symbol,date`), and supports
inner/left/right/full/anti. Duplicate right-side keys are never silently dropped:
`onDuplicate` picks first/last/all (all is bounded to 100 000 output rows), and
`suffix` keeps a colliding right column instead of letting the left value win.

**Text transforms.** `transform.template` renders `{{a}}`, dotted paths
(`{{a.price}}`), number/date formats (`{{a.price:.2f}}`,
`{{a.ts:date:%Y-%m-%d}}`) and `{{a?}}` for a value that is legitimately null. A
bare `{{a}}` whose value is null is a node error rather than a blank, and the
rendered text is capped at 50 000 characters. `{{#each a}}…{{/each}}` renders one
block per row with the row as the lookup scope (`{{symbol}}`, plus `{{this}}` and
`{{index}}`), nests up to five deep, is bounded to 10 000 rows per block, and
works inside a format spec; `\{{` emits a literal `{{`. `transform.formula`
evaluates the same safe language as `logic.map`/`filter`/`reduce` (never `eval`),
with `if(cond,a,b)` evaluated lazily so the untaken branch cannot fail, plus
`div`, `nz`, `clamp`, `sign`, `log10` and half-up `round`; its `x`/`y` ports
accept a series payload (`{last, values}`) and resolve to the latest point,
exactly like `logic.condition`.

**Try it.** `npm run demo:transform` runs a self-checking walkthrough of every
node above against the real runtime (`python/examples/transform_demo.py`, via
`scripts/demo-transform.js`). It prints PASS/FAIL per documented behaviour and
exits non-zero on a regression, so it doubles as a smoke test.

**Loops, end to end.** `npm run demo:loop` builds two real IR documents with the
editor registry and runs the pair through the runtime
(`python/examples/loop_demo.py`, via `scripts/demo-loop.js`): a pinned body
(`Loop body — 7% growth step` = trigger → `x * 1.07` → result) and a parent
(`Compounding planner (loops)`) that exercises all three loop shapes against it —
`flow.repeat` for a 12-year horizon with an if/else on the target, `flow.while`
until the balance clears 2 000, and `flow.for_each` over three principals. The
body is supplied to the parent exactly as the front door supplies it (pinned
`workflowId@version`), so the check proves the same path a saved run uses.
`npm run graph:loop` (without `--dry`) creates both workflows in JDDS so they can
be opened and started from the gallery.

Two wiring rules the demo encodes: a loop node's body **must** be a saved,
pinned workflow (it is what makes an iteration a resumable child run), and a
transform node chained after a gate **must** take that gate's `done` on its `in`
control port — otherwise it runs on dataflow alone with a null input when the
branch is not taken.

**Control ports on transform nodes.** Every tabular/text node — `transform.template`,
`transform.formula`, `transform.select`, `transform.sort_limit`, `transform.join`,
`logic.aggregate`, `logic.deduplicate`, `logic.map`, `logic.filter`,
`logic.reduce`, `data.dataset.save` and `data.dataset.load` — declares an
optional control `in` and a `done` control output. Wiring `in` makes the node run
only when an upstream control fires (If/else, Switch, Gate), and `done` fires
after a successful execution so a branch can own a chain without a helper
`flow.gate`. Both ports are optional, so existing graphs keep running on dataflow
alone. The executor refuses a `_control` name the node does not declare, because
firing an undeclared port routes to nowhere and would look like a successful
branch.

**Logic & flow control surface.** The same rule covers the rest of the category,
with one deliberate exception each way: `logic.condition` and `logic.switch` take
the optional control `in` but keep their exclusive branch outputs (`true`/`false`
and `matched`/`default`) instead of a `done`, because "done" would have to pick a
branch; `logic.merge`, `flow.gate`, `logic.wait`, `flow.subworkflow` and the three
loops (`flow.for_each`/`repeat`/`while`) take `in` and fire `done`.
`logic.stop` is terminal: it takes `in` and an optional `value` (which becomes the
run output), ends the run, and its `status` field is authoritative — `failed`
makes the run fail with the configured message instead of silently reporting
success; downstream nodes are skipped either way.
`logic.switch` accepts a JSON array for `cases` when a case itself contains a
comma (`["a,b", "c"]`). It fires `matched` **and** the per-case output that
matched (`case1` … `case8`, first match wins), or `default` when nothing matches,
so a 3-way branch is one node instead of two extra conditions; a case beyond the
eighth still fires `matched` for legacy graphs that only wire it.

**Switch presents only the cases you configured.** The contract declares
`case1…case8` so one node can own an eight-way branch, but the card would
otherwise draw ten anonymous handles and look broken. `node-ports.ts`
(presentation only — handle ids never change) shows one output per configured
case, labelled with the case value, plus `default`/`matched`/`caseIndex`; a case
handle stays visible when an edge still uses it, so removing a case never orphans
a wire. Handle spacing compresses as the fan grows, and the per-port `+` shortcut
is withdrawn when more than four outputs would overlap. The validator reports
`invalid_switch_cases` for cases that are not valid JSON and warns
(`switch_case_not_configured`) when a branch uses a case the node no longer
configures. The runtime behaviour is unchanged.
`flow.repeat`/`flow.while` accept `continueOnError` like `flow.for_each` already
did.

`flow.gate` takes up to three control inputs (`in`, `in2`, `in3`) and a `require`
policy: `any` (default) runs when a control input fires, `all` makes it a **join**
— every *wired* control input must have fired or the gate is skipped
(`error_code: skipped_by_node`) and its branch does not continue. This is
implemented with `NodeExecContext.control_fired`, not a scheduler special-case:
the executor returns a reserved `_skip` reason and the executor marks the node
skipped.

`logic.deduplicate` takes `keep: first|last` and `caseInsensitive`, and compares
keys with the same identity rule as `transform.join` (`1`, `1.0` and `"1"` are
one row). `logic.while` conditions can read the body's object fields — including
booleans — so a body returning `{done: true}` can be tested with `done != 1`
instead of silently reading `0` and exiting on the first iteration; the legacy
numeric projection (`value`) is unchanged.

`logic.merge` treats a branch as **present** unless it is null or empty, so an
empty `A` no longer shadows a populated `B` (the one case the node exists for);
`0` and `false` are real values and still win. `first` is the legacy name for
`first_present`. A skipped node's error now names the control wire that did not
fire (`its control input did not fire ("In" from cond was skipped)`), so a broken
branch is diagnosable from the run record.

`logic.map`/`filter`/`reduce` bind only the fields a row actually has: referencing
a missing non-numeric field is an error, never a silent `0`. A silent zero used to
make `filter` drop every row of a text field — the loop nodes now fail honestly
instead.

**Local datasets.** `data.dataset.save` writes a JSON payload to the runtime's
own `HarnessGraphDataset` table on `agent-memory.db` and returns a small
`gds:` ref plus metadata; `data.dataset.load` reads it back. This is the
large-payload seam: a series can travel as a ref instead of being embedded in a
node's config or the run document. It is deliberately **local scratch**, not a
market-data source and not a second JDDS. Limits: 8 MB per dataset, 200
datasets, optional TTL (max 8 760 h); expired refs are purged and fail
explicitly. Datasets are OWNER-SCOPED by the run's user id: another owner's ref
is reported as not found, not as a forbidden row. The `local_write` side-effect
class exists so a test run can use datasets while external
write/network/trading remain refused.

**Local files.** `data.file.attach` turns an uploaded CSV/TSV/JSON/XLSX/text
file into a graph input. The bytes live in the runtime's own
`HarnessGraphAsset` table on `agent-memory.db` (content-addressed per owner), and
the workflow stores only the `gas_` ref plus metadata — never the file itself.
The inspector has an upload/replace picker; the front door proxies the bytes to
`/graph/assets` (`src/app/api/graph-studio/assets`). `data.file.parse` runs the
same parser over a File reference produced upstream (choose a sheet for Excel).
`data.file.export` writes rows back to a downloadable local asset (CSV/JSON/
XLSX); it is a `local_write`, so a test run may export. Limits: 25 MB per file,
200 files and 512 MB total per owner, plus 20 000 rows / 200 000 cells per parse;
truncation is reported, never silently dropped. A missing or expired ref is an
explicit failure. The parser is deliberately local: no network fetch, no second
JDDS, and no fabricated rows.

**Honest data.** Every payload carries provenance (`provider`, `symbol`,
`interval`, `adjusted`, `rows`, `asOf`, `retrievedAt`). An empty vault is a
FAILURE with an actionable message ("fetch them first … the graph never
fabricates a price") — never a synthetic series, never a silent zero.

**Test-run policy.** A test run implicitly grants the read-only / local-compute
capabilities (`market.read`, `quant.compute`, `compute.safe`, `dataset.read`,
`dataset.write`, `asset.read`, `asset.write`, `image.read`, `model.vision`) plus
`agent.run` — a model call is not a world effect — so a
finance graph can be exercised from the UI. Agent tools are narrowed to
`DRY_RUN_TOOLS` in that run. External write/network/trading nodes stay refused.

**Media demo.** `npm run demo:graph-media` seeds two real workflows into
Overview: **File import (demo)** (generated CSV → File → Select → Result) and
**Candle option review (demo)** (two generated candle images → vision batch →
Result). The sample assets are written through the SAME graph/vision stores the
nodes use, so the demo runs on real local data. Both were verified end-to-end
through the app API: the file demo returns 24 rows, and the image demo returns
two real observations (9 and 7 candles read, ~850–900 tokens each) from
`qwen2.5-vl` via the configured provider. If no vision model is reachable the
batch fails loudly with the provider reason instead of letting an agent reason
over no readings. The seeder is idempotent — it reuses an existing demo rather
than piling up versions; delete the workflow and re-run it to regenerate from the
current templates.

**Templates.** `Market scan (SMA z-score)` — stored bars → SMA(50) → z-score(20)
→ branch → result, plus a second output carrying the data provenance.
`Data pipeline` — the six basic data/write nodes wired end to end: constant →
parse JSON → set variables → save/load dataset → write file. Its write step is
refused in a test run by design, so it is meant for an operator run that grants
the `workspace` scope.

### Phase 9 — inspection and diffing

**Step inspector** (`src/lib/graph-studio/run-debug.ts` + the Run panel). A
finished or partially failed run is walked step by step: node order, status
(including attempts and skips), bounded inputs/outputs, the node's own events,
and whether its checkpoint is resumable. It is a read-only inspector over the
persisted run record — no re-execution, no synthetic success. The runtime now
exposes `nodeRuns[].resumable` so the UI can say "checkpoint saved" truthfully
instead of inferring it.

**Structural diff** (`src/lib/graph-studio/ir-diff.ts` + History > Versions >
Diff). Two immutable versions are compared without running either: added,
removed and changed nodes (type, version, label, position, config), added/
removed/changed edges, changed settings and a changed workflow name. Config
comparison is key-order-insensitive. A removed node/edge is reported by id; a
first version says there is nothing to compare rather than showing an empty
diff.

### Phase 10 — run telemetry and hardening

`GET /graphs/health?windowHours=N` (operator-gated) is a BOUNDED read over the
persisted run tables: run counts by status, success/failure rate, duration
p50/p95, queue wait (created → started), throughput, failed-node codes, the
slowest node types, and checkpoint coverage. Reads are capped (1 000 runs /
5 000 node rows, 1–168 h) and the response says `truncated` when a cap was hit.
Missing values are `null`, never zero. The Graph Studio **Health** tab shows the
same figures for 1 h / 24 h / 7 d with an explicit refresh. Additive contract:
`nodeRuns[].resumable` (Phase 9) and `/graphs/health` (this phase); no new store,
queue or scheduler.

**Health watch.** `graph_runtime/health_watch.py` reads the same telemetry on a
cadence and persists one verdict (`unmeasured` / `insufficient` / `healthy` /
`degraded`) in `GraphHealthWatchState`; the health response carries it as
`watch`. It publishes `graph.health_degraded` / `graph.health_recovered` only on a
transition, never every tick; each transition is also appended to a bounded
`GraphHealthAlert` history that the Health tab lists (newest first). Thin data
is `insufficient`, not healthy. Tuning:
`HARNESS_GRAPH_HEALTH_SECONDS` (default 300; `0` disables),
`HARNESS_GRAPH_HEALTH_WINDOW_HOURS` (default 1),
`HARNESS_GRAPH_HEALTH_MIN_RUNS` (default 5),
`HARNESS_GRAPH_HEALTH_FAILURE_RATE` (default 0.5) and
`HARNESS_GRAPH_HEALTH_QUEUE_MS` (default 60000). It never creates or changes a
run.

**Headless schedules.** The in-process 60 s timer is sufficient while the app is
up. For an unattended host, drive the same tick from system cron (with
`CRON_SECRET` set):

`curl -fsS -X POST -H "Authorization: Bearer $CRON_SECRET" http://127.0.0.1:3000/api/workflow-schedules/tick`

The tick is idempotent per due schedule; missed fires still follow each
schedule's `skip`/`coalesce` policy.

### Phase 1 — acceptance criteria (verified)

- Graph Studio opens within Eclipse (new `graph-studio` AppTab + sidebar item). ✓
- Create, position, connect, configure and delete nodes. ✓
- Zoom, pan, selection (React Flow Controls + MiniMap + selection state). ✓
- A graph can be saved and reopened (append-only versions). ✓
- Existing routes/features unaffected — full vitest suite green. ✓
- `npx tsc --noEmit`, the full vitest suite (3440 tests) and `npm run build` pass. ✓

---

### Phase 3 — acceptance criteria (verified)

- A simple graph executes from its trigger through its terminal. ✓
- Node outputs are validated against their declared port schemas. ✓
- Failed nodes produce inspectable errors (message, `errorCode`, duration, the
  inputs that produced them) and downstream nodes are `skipped`. ✓
- Execution status appears on the canvas (node status chips + border tint) and in
  the Run panel. ✓
- Test execution cannot trigger live trading (no trading node is authorized; the
  envelope refuses trading/network/external-write regardless of the request).
  Local dataset scratch is the one bounded write a test run may perform. ✓
- The same workflow produces reproducible results for identical inputs. ✓

## Verification

Phase 1:

- `npx tsc --noEmit` — clean; full vitest suite green; `npm run build` succeeds.

Phases 3–4:

- `npx tsc --noEmit` — clean.
- `npx vitest run src/lib/graph-studio` — **16 files / 85 tests pass**.
- `npm run build` — production build succeeds; `/api/graph-runs/*`,
  `/api/workflows/[id]/{runs,trigger,schedules}` and `/api/workflow-schedules/*`
  registered.
- Python graph suites — **482 tests pass** (3 expected skips: two opt-in
  agent E2E checks and a Windows symlink case). Coverage: model/registry/
  formula, compiler, executor, control flow, service, HTTP API, SSE replay,
  finance/video nodes, gateway quote/history, the market-read surface, tabular
  transforms, the quant pack (backtest/walk-forward/optimizer/Monte Carlo/OOS,
  metrics, validation, structure, portfolio, options/futures, costs, report),
  paper risk gate/execution adapter and LIVE refusal, agent
  planning/delegation, chat-to-graph validation, health telemetry/watch, bounded
  crawl, local research/evidence + live web search/fetch/claim verification,
  agent node, operator-run/HTTP safety, registry parity, datasets,
  subworkflows, durable dispatch/resume, plus a real
  Fabric manager + graph worker integration test and full-stack API tests on a
  temp database. `test_graph_quant_e2e.py` is the quant pack's end-to-end
  guard: it runs every added quant node through `/graphs/runs` in one graph.
  `test_graph_agent_e2e.py` is an OPT-IN real-provider check:
  skipped unless `HARNESS_E2E=1` plus provider/model env vars (API key only for
  remote providers). It passed locally against the configured Ollama model,
  covering chat-to-graph, step planning and an agent node.

### Navigation regressions fixed (post-Phase 4)

Selecting the Graph mode blanked the app. Root cause: the mobile bottom nav
renders `TAB_META[id].icon` for every tab a mode lists, `TAB_META` was typed
`Record<string, …>`, and `graph-studio` had no entry — `undefined.icon` threw
inside the top-level ErrorBoundary. Because the chosen mode is persisted, every
subsequent load could hit the same path. Fixed by adding the entry, typing
`TAB_META` by `AppTab`, guarding both render sites, and adding a coverage test
(`mobile-bottom-nav.test.ts`). Two neighbours were fixed with the same pass: the
persisted-mode allowlist dropped `'graph'` (the mode never stuck) and
`APP_TABS` in `search/deep-link.ts` omitted the new tabs, so `?tab=`/boot-tab
links fell back to ChatBot. `APP_TABS` is now an exhaustive `Record<AppTab, true>`
(adding a tab without listing it is a compile error) and `NAV_GROUPS` is the ONE
mode list. Verified in a real browser: the mode switcher, the panel's
open/create path and `?tab=graph-studio` all mount the workspace with no console
or HTTP errors.

## Known limitations

- **Durable workers, one host.** Runs are durable Fabric jobs and resume from
  node checkpoints after a worker/process restart. Several processes on the SAME
  host may share `agent-memory.db` and split workers by
  `HARNESS_FABRIC_WORKERS` / `python -m harness.fabric.standalone`; claims are
  atomic. A multi-machine fleet is not supported by the local SQLite store. The
  legacy in-process fallback (Fabric disabled) has no automatic resume.
- **Checkpoint storage.** A successful node's output is stored in the DB up to
  2 MB by default (`HARNESS_GRAPH_CHECKPOINT_MAX_BYTES`, bounded to 16 MB).
  Above that it spills to a confined JSON file under
  `<db-dir>/graph-checkpoints/<runId>/` (up to 256 MB) and the run stays
  resumable. Above the file cap the run is non-resumable and starts fresh on a
  retry. Spilled files are deleted with the run.
- Schedules tick while the app is up (a 60 s in-process timer). A fully
  headless/unattended deployment must drive `POST /api/workflow-schedules/tick`
  from external cron (see "Headless schedules"); the app cannot tick while it is
  stopped.
- `logic.wait` defers a durable job (up to 24 h) instead of blocking. Only a
  non-durable `run_sync` still blocks, capped at 30 s; a test run is capped at 5 s.
- Loop primitives accept numeric arrays AND object rows: a row's numeric fields
  are bound by name (plus `value`, `index`, `acc`), so `close - open` works. A
  nested object/array field is not flattened; project it first.
- Run history is browsable in the History panel; node runs and events are
  normalized tables, and successful nodes have resumable checkpoints.
- Operator runs allow network, PAPER trading, LIVE broker execution under the
  two-key model, the whitelisted local `video.render`, and `file.write` only
  inside the named scopes the operator confirmed. Other external write nodes and
  ungranted trading are refused. Live orders need an armed live connection **and**
  an active workflow-pinned live mandate (24 h max TTL); a paper-only node in live
  mode is refused by the compiler. `file.write` is confined to its scope
  (relative paths, no `..`, no symlink escape, 8 MB cap, atomic replacement). The paper account persists cash,
  positions and realized PnL, but it is cost-basis accounting (no mark-to-market)
  and fills only at a supplied price - it never fetches a live quote.
  `persist: false` keeps a single-node fill stateless.
- Agent model credentials are held in memory for the run only and are never
  written into the durable job payload. After a process restart the worker
  re-resolves them from the front door (`POST /api/graph-runs/{id}/credentials`,
  operator-gated) using the persisted callback URL. If the front door is down or
  no provider is configured, that node fails honestly. Agent turns also resume
  from the agent plane's own checkpoint (`agent_run_identity`): a crashed turn
  re-enters the same run when a checkpoint exists; without one the turn restarts
  under a fresh id (safe — a model call has no world effect). The nodes remain
  bounded turns/plans (no autonomous loop). A real-provider e2e harness exists
  (`test_graph_agent_e2e.py`) but is opt-in and needs credentials.
- Web research nodes use the app's data bridge (a real operator run has one; a
  headless runtime does not). With `allowBrowser`, `research.web_fetch` escalates
  a thin/failed page to the existing Browser Runtime; without it (the default) it
  fetches exactly the URL it was given. `research.web_crawl` walks a BOUNDED
  same-site graph (1–10 pages) using the bridge's deterministic link collector,
  never a general crawler. `research.claim_verify` requires the evidence ids
  explicitly and verifies against the local EvidenceStore only.
- A nested subworkflow gets its own durable run row AFTER it finishes; a child
  interrupted by a process crash has no partial row (the parent resumes from its
  node checkpoint). The child row is keyed by `<parentRunId>:<nodeId>`.
- `live` mode is settable in the IR but no executor honours it and the compiler
  refuses a trading node in LIVE mode. Paper trading is the only trading effect
  Graph Studio can authorize today.
- **`video.render` runs the existing pipeline inline in the graph worker.** It
  builds a bound recap storyboard and calls `VideoService.run_render_job`, so it
  writes project files and an Artifact Center entry; it is not queued as a
  separate Fabric job. It requires an operator run with the explicit local-render
  grant and the fframes Rust worker installed (a missing worker is an honest
  failure).

## Trade learning pack (`trade.*`)

A second, small node pack turns the journal into a learning loop. It is
deliberately **not** a self-editing mechanism: the loop may improve one bounded
thing — the playbook envelope the risk gate enforces — and only toward tighter
risk. It never places an order and never edits code.

| Node | Class | What it does |
| --- | --- | --- |
| `trade.journal` | read | Loads a bounded window of JDDS trades + the execution log through the authenticated, read-only bridge (`journal_trades`), owner-scoped to the run's user. |
| `trade.review` | none (pure) | Deterministic detectors (`no_stop`, `risk_over_limit`, `stop_widened`, `target_reduced`, `early_exit`, `held_past_stop`, `overtraded`, `revenge_trade`, `no_plan`, `checklist_violation`, `off_session`, `off_symbol`, `duplicate_entry`, `unreviewed`). Every finding cites the field it saw. |
| `trade.learn` | local_write | Persists findings, bumps the ONE `LearningRecurrence` ledger keyed by trade id, applies recurrence/coverage/cost thresholds, and drafts an INERT RSL candidate. Operator run only — a test run is refused so it cannot inflate the ledger. |
| `trade.improve` | none (pure) | The recursive climb: variants are mutated from previously measured states (including losers), acceptance compares against the champion on a chronological **holdout** window, and every number is labelled `simulated over recorded trades`. |
| `trade.commit` | write | Appends a playbook version. Operator run only; the runtime re-validates bounds and tighten-only and refuses any widening. |
| `trade.playbook` | read | Reads the active envelope + history, for display or to wire the gate. |

**What a "mistake" is.** A process violation with a recorded field, not a loss.
A losing trade that followed the plan is clean; a winning trade with no stop is
still a mistake. A field that is absent (common for manual trades) makes the
detector report `unavailable` — never a guess.

**What "self-improvement" is.** The playbook is the trading model: risk per
trade, max trades/day, max consecutive losses, max daily loss in R, require a
stop, minimum planned RR, symbol blocklist, session allowlist, suspended
strategies. The operator sets **bounds**; the loop may only move a knob
*toward tighter*, one step per cycle, inside those bounds. A widening change is
refused in the runtime (`NOT_A_TIGHTENING` / `OUT_OF_BOUNDS`) regardless of what
the graph asked for. The climb reads the last N trades, reports fit and holdout
separately, and only accepts a candidate that improves the holdout with a
minimum sample.

**Recurrence.** One occurrence is never a pattern. Eligible requires ≥3
occurrences inside the window, ≥80% data coverage for the fields involved, and
(at least) 1R of measured cost when R is available. The output names the reason
when it is `not_yet`, so a quiet result is explainable.

**Enforcement.** `trading.risk_check` gained an optional `policy` input. When
wired, the gate applies only the learned limits it can enforce statelessly — a
required stop and a shrunken per-trade risk cap — and lists the rest
(`symbolBlocklist`, per-day counts, streaks, session window) under
`state.playbook.notEnforced` rather than pretending they are live.

**Storage.** Findings and playbook versions live on `agent-memory.db`
(`TradeFinding`, `TradePlaybook`); the journal stays in JDDS and is read through
the bridge. A playbook version is append-only and a full snapshot, so rollback
is a pointer change (`TradeLearningStore.revert`), and `revertsAt` is set on
every applied version.

**Template.** `Trade learning loop` (`trade-learning`) wires trigger → journal →
review → learn → improve → commit → result. It sets conservative operator bounds
(`requireStop`, risk 0.25–5%, 1–10 trades/day) so the first run proposes a small
step. A manual run with no input falls through to the journal bridge; passing a
trades array as the run input uses that instead. The Journal learning panel is
the remaining optional surface.

## Typed decision pack (`decision.*`)

The decision layer's workflow half. Two nodes expose the SAME
`DecisionService.ask` path the `decision_ask` tool uses, so a graph can branch on
a typed judgment without inventing a second provider path. An answer is
**information, never authorization** — these nodes cannot place an order, move
money, or widen a run's grants. Branch on the `status` output, never on the
presence of an answer.

| Node | Class | What it does |
| --- | --- | --- |
| `decision.ask` | model | Takes a `state` (wired input, or `stateJson`) plus a closed question template (`questionsJson`), asks the configured provider, applies the confidence policy, and returns typed `answers` + `status` + `summary` + `provider` + `recordId` + `reason`. |
| `decision.demo` | local_write | The zero-config demonstration: a deterministic rules-only scenario, no key and no network, still written through the same request → policy → audit pipeline. A wired `state` overrides the built-in sample. |

**Question template.** `questionsJson` is an object keyed by stable question id:
`{id: {type: "choice" | "score" | "noul", instructions, criteria?, rule?}}`. A
`rule` is the deterministic offline path used by the demo and by tests; remote
providers ignore it. A partial answer, a type mismatch or a provider error
becomes `status: "unavailable"` with an explicit `reason` — never a guess.

**Status is the only branch.** `answered` cleared every policy threshold;
`review` is inside the abstain band and is a hint, not a decision; `unavailable`
means nothing was inferred. The same request is recorded on the immutable
`DecisionRecord` trail on `agent-memory.db`, and `recordId` names it.

**Privacy.** `ECLIPSE_PRIVACY=local-models` blocks any non-local provider before
the call, so `decision.ask` fails closed to `unavailable` rather than reaching a
remote model. The demo is always local.

**Template.** `Typed decision (demo)` (`decision-demo`) wires trigger →
`decision.demo` (`risk_gate`) → two result nodes (answers, summary). Pressing
Run is the whole test: no key, no network, no setup beyond `npm install`.

**Testing.** A workflow **test run** may execute both nodes: `decision.ask` is a
model call (the reduced envelope already permits agent/model work) and the demo
only writes its own audit record. The per-node "Test node" panel runs an even
narrower envelope (`none`/`read`) and therefore refuses `decision.demo`; use the
template's test run to exercise it. A test run can never trade, send or widen
grants, so it cannot turn a judgment into an action.

## Next milestone

**Post-phase hardening**: the ten phases are implemented, including external-cron
guidance, health alerting and same-host multi-process Fabric workers. The
remaining item is a genuinely multi-machine fleet, which would require replacing
the local SQLite store with a shared database/queue; the rest are scoped bounds
listed under "Known limitations".

