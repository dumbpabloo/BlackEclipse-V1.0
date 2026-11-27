---
title: "Self-Improvement Memory (SIM) — continual RSI for the Eclipse harness"
status: Current
last-verified: 2026-11-27
---

# SIM for Eclipse — what we take from arXiv:2611.18856 and what we build

**Source:** Yu et al., *Self-Improvement Memory Enables Continual Recursive
Self-Improvement* (Google, Nov 2026). The paper studies **CRSI**: continuing
harness optimization after the benchmark or the policy model changes, starting
from an evolved harness. Its finding is that the evolved harness is not enough —
the optimizer also needs the **improvement record**: why each edit was made,
what it predicted, what actually happened, and in which setting.

This document is the plan and the status of the native Eclipse translation. It
is deliberately narrow: SIM is *memory for the harness optimizer*, never memory
for the task-solving agent (Eclipse already has that in `eclipse_memory/`).

## 1. The paper in six lines

1. A run optimizes harness `H` around a frozen policy `M` on evolve split
   `X_evo`, with a held-out `X_eval` the optimizer never sees.
2. Each round: evaluate → `F_t`; a proposer emits `k` candidates; a selector
   screens, executes some, and keeps at most one.
3. **CRSI** resumes from the final source harness in a new setting: cross-dataset
   (`M` fixed, `X` changes) or cross-model (`X` fixed, `M` changes).
4. A transferred harness can *hurt*: an Opus-evolved harness scored 68.41 on
   Gemini workspace vs 73.89 for Gemini's seed.
5. **SIM** groups edits by recurring **root-cause problem** and stores, per
   attempt: modification + rationale, **predicted** outcomes (before
   evaluation), **observed** outcomes, and provenance (domain, benchmark,
   model, round). Proposer *and* selector read it; only the selected edit is
   appended, including regressions; nothing is deleted; identical root causes
   merge.
6. Result: Full SIM beats harness-only continuation in 6/6 cross-dataset and
   4/4 cross-model settings; +2.36 points over 10 paired coding runs
   (p = 0.003). Root-cause-only memory is inconsistent — **the evidence behind
   the diagnosis is the transferable part**.

## 2. Where Eclipse already stands

| Paper need | Eclipse today | Gap |
|---|---|---|
| Harness as a bounded, versioned surface | `HarnessSnapshot` (prompt, tools, guidance, guardrails, model, ceilings) with a content digest | none |
| Measured outer loop | `hillclimb.run_hillclimb` + `utility.acceptance` + replay gate | none |
| Archive of losing states | `StateArchive` stepping stones | none |
| Why a candidate exists | `Observation(kind, signature, targetHint)` | one line, no attempts, no outcomes |
| Per-attempt result rows | `EvolutionStore.save_attempt` (candidate, ops, evaluation, success/failure) | not problem-keyed; no rationale, no prediction, no cross-setting carry, not read by the proposer |
| Outcome-linked learning for *lessons* | `LessonOutcomeStore` (injected/control lift on run ids) | per lesson, not per harness problem; no predictions |
| Problem-keyed improvement history | — | **the gap SIM fills** |
| Prediction then observation | deterministic gates only | **no recorded prediction to falsify** |
| Cross-setting carry (model / suite) | `HARNESS_RSL_LESSON_TRANSFER` for lessons | **no carried harness-problem ledger** |
| Memory read by proposer **and** selector | model proposer prompt carries observation only; selector is deterministic | **no shared improvement record** |

So Eclipse has the *search* and the *gates*, and it has outcome scoring for
lessons. It does not have a durable, problem-keyed record of its own harness
edits with predictions and observed results. That is the implementation target.

## 3. Design decisions (and the ones we deliberately reject)

1. **One JSON file, not a second database.** `ImprovementMemory` is an inert,
   append-only ledger at `<repo>/.black-eclipse/self-improvement/memory.json`
   (overridable). It is evidence, never authority: it cannot promote, enable or
   land anything. This respects the RSL rule of one store and keeps the record
   portable to another machine/run. The default path is gitignored runtime data,
   so a personal ledger never ships.
2. **Predictions are optional and never invented.** An attempt without a
   prediction renders as `no prediction recorded`; its hit rate is
   `UNMEASURED`, not zero. An attempt without an observed score is
   `unmeasured`, never `flat`.
3. **The returned edit is recorded even when it regresses or is rejected.**
   Rejected candidates remain as evidence (`accepted: false`), so later rounds
   can see which directions were already tried.
4. **Bounded rendering, honest omission.** The paper reads every entry. Eclipse
   has a hard prompt cap, so `render()` is deterministic, newest-first, and
   ends with an explicit "N omitted to fit" line. Silent truncation would look
   like the whole history.
5. **Dark by default.** `HARNESS_RSL_MEMORY=off` unless set; with it off the
   proposer prompt and the hill climb are byte-identical to today.
6. **Root-cause identity is exact-normalized text, merged, never fuzzy.**
   The paper lets the proposer decide identity and merges identical text. We do
   the same: normalized-text identity plus an alias map; no embedding model.
7. **Reject: memory for the task agent.** `eclipse_memory/` already owns
   personal, correction and skill memory. SIM is scoped to harness problems.

## 4. What was implemented

### Configuration

| env | default | meaning |
|---|---|---|
| `HARNESS_RSL_MEMORY` | `off` | enable SIM (dark by default) |
| `HARNESS_RSL_MEMORY_PATH` | `<repo>/.black-eclipse/self-improvement/memory.json` | the ledger file |
| `HARNESS_RSL_MEMORY_CHARS` | `6000` | render budget, clamped to 800…20 000 |
| `memory` / `memoryPath` / `memoryChars` | — | the same three through `POST /evolution/rsl/config` |

### `python/harness/self_improvement/improvement_memory.py`

Pure, deterministic, no model call, no I/O except an explicit `load`/`save`.

- `Provenance` — domain, model, round, run id, candidate id, state digest,
  timestamp.
- `Attempt` — edit, rationale, operator, predicted task keys, fixed / missed /
  unresolved, passed/total, score before/after, accepted, outcome
  (`improved`/`regressed`/`flat`/`unmeasured`), provenance.
- `Problem` — root cause name, opening provenance, append-only attempts.
- `ImprovementMemory` — `open_problem`, `record`, `record_result`, `merge`
  (aliases), `carry_from` (cross-setting carry), `stats`, `render`,
  `to_dict`/`from_dict`, atomic `save`, guarded `load`.
- `MemoryConfig` — `HARNESS_RSL_MEMORY`, `HARNESS_RSL_MEMORY_PATH`,
  `HARNESS_RSL_MEMORY_CHARS`.

### Wiring

- `proposer_model.propose_variants(..., memory=...)` puts the rendered record
  in the proposal prompt with the paper's usage rules (prior experience, check
  the setting, reuse the rationale, cite or open a problem, weight
  task-specific evidence less when both model and benchmark changed).
- `RSLLoop` builds the memory when enabled, feeds it to the model proposer,
  and appends one attempt per evaluated hill-climb neighbor (accepted or
  rejected) with the utility delta and aggregate pass counts.
- `GET /evolution/rsl/memory` (operator-gated, in `routers/evolution_memory.py`)
  returns the ledger, its stats and the rendered block; `GET /evolution/rsl`
  carries the same stats under `memory`.
- The ledger never blocks a decision: a read/write failure degrades to "no
  memory" and is reported, never raised into the climb.

## 5. What is *not* implemented (and what we would do next)

| Item | Why it is not in this pass | Next step |
|---|---|---|
| Selector conditioning | Eclipse's selector is deterministic (`selection.py`); there is no model selector to condition | Feed `render()` into the selector once a model selector exists, or use hit-rate to order the pool |
| Predicted-task scoring from the model proposer | The model proposer's schema does not emit per-task predictions | Add optional `predictedTasks[]` to the proposer schema and capture them before evaluation |
| Cross-run auto-carry | `carry_from` exists but no trigger decides *when* a new setting begins | Derive a setting key (model + suite + scope) at climb start; carry on key change |
| Retention/pruning policy | Hard caps exist (200 problems, 50 attempts per problem; render shows the 3 newest), but nothing is archived to a second file | Archive overflow instead of dropping the oldest problem; make the caps configurable |
| Lift estimation | Needs matched predicted/missed outcomes over many attempts | Reuse `evaluation/stats.py` Wilson intervals like `LessonOutcomeStore` |

## 6. How to verify it

```bash
# the unit + wiring suite (17 tests)
python -m pytest python/tests/test_rsl_improvement_memory.py -q
# the operator route
python -m pytest python/tests/test_evolution_rsl_routes.py -q

# inspect the ledger of a live install (SIM on)
curl -H "x-operator-token: $TOKEN" http://127.0.0.1:8000/evolution/rsl/memory
```

The tests pin the properties that make the record trustworthy rather than the
format: unmeasured stays unmeasured, predictions resolve only with evidence,
merges keep attempts and aliases, a rejected neighbor is retained, `render()`
states its omission, a verdict-leaking ledger is dropped before the prompt, and
`HARNESS_RSL_MEMORY=off` leaves the climb byte-identical (no `memory` key, no
file).

## 7. Falsifiable claim for Eclipse

If SIM is doing what the paper says, then with `HARNESS_RSL_MEMORY=on` the same
number of hill-climb generations should produce **fewer repeated attempts at
already-rejected directions** and a **non-lower final utility** than with it
off, at matched budgets. The ledger records the data needed to check this
(attempt outcome + `problemId` + provenance); the comparison itself is a
separate measured run, and we do not claim it here.

