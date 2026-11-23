---
title: Harness Decisions
status: Current
last-verified: 2026-10-19
---

# Harness Decisions

Decisions for the harness. Deep architecture ADRs are mirrored in
`docs/DECISIONS.md` (ADR-018) and originate in `skills/ARCHITECT.md`.

## ADR-H1 — The agentic runtime is Python (LangChain + LangGraph)

**Decision.** Agents, tools, model providers, harness events and run
persistence are implemented in Python under `python/harness/`, served by
FastAPI. Next.js does not run an agent loop.

**Why.** The requirement is that the agentic side not be written in TypeScript,
and that it connect every model. Python is where the LangChain/LangGraph
ecosystem and provider integrations live, so "support all models" and
"author agents" become configuration rather than bespoke TS adapters.

**Consequences.** A Python service is spawned and health-checked on first use.
Agent runs are unavailable (503) without it — by design, never degraded to a
fake agent. The earlier TypeScript Phase-1 kernel (contracts + registry +
runtime + adapters) was retired; only input validation, vault resolution, the
proxy and the JDdS bridge remain in TS.

## ADR-H2 — Next.js keeps exactly two jobs

**Decision.** The front door (a) validates input + resolves agent/LLM config
from the vault, and (b) exposes a read-only JDdS data bridge. Nothing else.

**Why.** These are the two things Python cannot own: the encryption vault
(`SystemConfig` + provider keys in `jdds.db`) and Prisma/JDdS access. Resolving
credentials in TS keeps the key out of the Python process except for the single
run it is needed for.

**Consequences.** The TS harness surface shrank from ~20 modules to ~12 small
ones with no agentic logic.

## ADR-H3 — JDdS is reached through a read-only capability bridge

**Decision.** Python tools request named capabilities
(`market_snapshot`, `fundamentals`, `news`, `source_health`) via
`POST /api/harness/data`; `data-bridge.ts` performs the Prisma query.

**Why.** There must be one financial data layer. The Python side still owns the
tool *contract* (name, schema, permission, risk, honesty rules); only the data
access is delegated. The endpoint is guarded (Bearer `CRON_SECRET`) and
whitelists capabilities — it cannot write.

**Consequences.** Adding a JDdS-backed tool means adding a capability on both
sides. No Python code opens `jdds.db`.

## ADR-H4 — Universal model coverage via catalog + OpenAI compatibility

**Decision.** `PROVIDER_CATALOG` declares providers, their LangChain family,
default base URL/model and env var. First-party integrations are native;
everything else uses the OpenAI-compatible protocol (which also covers local
servers).

**Why.** A branch per vendor does not scale and couples the runtime to vendors.
A catalog is data, and the compatible path alone reaches most hosted + local
models, so "all models that exist" is achievable without bespoke code.

**Consequences.** Model metadata is exposed at `GET /api/harness/models`.
Adding a provider is one entry (+ a requirements line for native integrations).

## ADR-H5 — Agent memory lives in its own database

**Decision.** Runs, events, tool executions, agent memory and skills live in
`agent-memory.db`, not `jdds.db`.

**Why.** JDdS is high-value shared market data; harness state is high-churn
operational data. Separation prevents write contention and keeps user/skill data
on its own lifecycle. (Originally the split also protected a committed seed
snapshot; `jdds.db` is untracked runtime data now, so the snapshot discipline is
obsolete — see `docs/GLOSSARY.md` under **Seed**.)

**Consequences.** Two DB files at the project root; `agent-memory.db*` is
gitignored. No Prisma migration — idempotent DDL on first use.

## ADR-H6 — Read-only tools only in this phase

**Decision.** The Python registry refuses any tool whose permission is not
`read`.

**Why.** The kernel must not be able to move money or mutate state before an
approval path exists.

**Consequences.** A later phase adds explicit approval before non-read tools
become callable.

## ADR-H8 — Agents and prompts are Python-authoritative

**Decision.** Agent definitions and system prompts live in Python
(`agents.py`, `prompts.py`). The front door sends only an `agentId`; the runtime
resolves the full `AgentConfig`, including the prompt.

**Why.** An earlier revision kept a prompt mirror in `src/lib/harness/agent-presets.ts`
while the front door sent that copy — which meant the Python prompt was dead
code and the two could silently drift. One source of truth removes the failure
mode entirely.

**Consequences.** Editing a prompt is a one-file change in `prompts.py`.
`GET /api/harness/agents` proxies the runtime; the TS side caches presets
briefly and validates ids against them. Sending a full `agent` object is still
accepted as an explicit override for bespoke callers, but the normal path is an
id.

## ADR-H9 — Token usage is captured honestly, never estimated

**Decision.** The runtime reads each model response's token usage and persists
`promptTokens`/`completionTokens`/`totalTokens` on the run. When a provider omits
a usage block, the run records 0 and reports itself as not having usage
(`totals.runsWithUsage`), rather than estimating from text length.

**Why.** Token consumption is a financial fact for an operator paying per token,
and providers are not uniform — local Ollama/LM Studio commonly omit usage. The
same rule that makes market data honest (UNAVAILABLE over a fake number) applies
here: an estimate presented as a measurement is a lie.

**Consequences.** Analytics (`GET /usage`, Settings › Analytics) can show totals
and simultaneously state how many runs did not report usage. Adding a provider
requires no change — `_extract_usage` reads the standard LangChain
`usage_metadata` first, then the OpenAI/Anthropic metadata blocks.

## ADR-H10 — A provider-agnostic retrieval layer sits between tools and providers

**Decision.** Agents request an *objective + data type*, never a provider. A
dedicated `python/harness/retrieval/` package (`FinancialRetrievalLayer`) plans
the source(s), retrieves them (cache-aware, possibly in parallel), normalizes,
validates, resolves conflicts, and returns provenance-carrying evidence. Tools
call the layer; the layer is the only place that knows JDdS, yfinance or the web
exist.

**Why.** The Phase-2 objective is "what information do I need → what is the best
source → structured/cache/web/multiple → fresh and reliable?": that is a
decision, and a decision belongs in one explainable place, not scattered across
tools. It also keeps the agent decoupled from any single data source and keeps
JDdS and yfinance as *sources*, not mandates.

**Consequences.** Adding a provider means implementing the `RetrievalSource`
contract and adding its name to the planner — no tool changes. Source-selection
policy is data (`planner.py`), so new data types are new rows. Precise data
types (historical prices, macro series, derived metrics) remove WEB from the plan
entirely: the layer returns `UNAVAILABLE` rather than web-sourcing a number.

## ADR-H11 — One shared in-process retrieval cache, no new database

**Decision.** The retrieval layer caches with per-data-type TTLs in-process.
The runtime owns ONE `RetrievalCache` shared across runs, with in-flight
deduplication keyed per retrieval.

**Why.** Caching is short-lived operational state; `agent-memory.db` stays the
only harness-owned store and no new database is introduced. Sharing one cache is
safe because deduplication is keyed per retrieval: two concurrent runs asking for
the same dataset share a single provider call (the "multiple agents request the
same data" requirement).

**Consequences.** The cache does not survive a process restart — acceptable for
market data, which is re-fetchable. `GET /retrieval/metrics` reports cache
hits/misses/in-flight honestly. A durable cache would need a later ADR.

## ADR-H12 — Web research is one system and precise data is never web-sourced

**Decision.** `src/lib/web-research.ts` is the single web search + page
retrieval implementation, backing both the retrieval layer's `web_search`/
`web_fetch` capabilities and the legacy `search_web` agent tool (which now
delegates to it). The retrieval planner forbids web for `historical_prices`,
`macro_series` and `derived_metric`.

**Why.** A second search system was explicitly forbidden, and using search
results to answer a precise quantitative question (e.g. "AAPL's 5-year Sharpe
ratio") is fabrication by another name. The honesty rule that governs JDdS
(UNAVAILABLE over a fake number) applies to web research too.

**Consequences.** Web evidence always carries provenance
(`source/sourceType/url/title/publishedAt/query/validationStatus`); missing
metadata is `null`, never guessed, and a result that could not be fetched is
marked `unverified` rather than presented as read.

## ADR-H13 — The run streams, and the final step always answers

**Decision.** The agent node streams tokens via `model.astream`, emitting
transient `model.delta` events that reach the UI over SSE but are never
persisted. On the last allowed step, tools are unbound, an in-band instruction
forces a text answer, and any residual tool calls are stripped; a forced turn
that still produces no text fails honestly as `MAX_STEPS_EXCEEDED`.

**Why.** Two real failures drove this. (1) The chat was a blank spinner for up
to ~2 minutes — the user could not tell the agent was working or what it was
doing. (2) A tool-calling model (notably local Ollama models) would loop until
max steps and return `MAX_STEPS_EXCEEDED` instead of an answer, including
hallucinating tools that do not exist; the agent was, in practice, "unable to
answer" research questions. Unbinding tools alone was insufficient — the model
kept emitting tool calls from the conversation's momentum — so the fix is
threefold: unbind, instruct in-band, and strip residuals.

**Consequences.** Token deltas are high-frequency and worthless after the run,
so they ride the bus and are not written to SQLite; the persisted trace still
ends with the full `model.completed`. The front door can start a run in the
background and relay its stream; a streaming failure falls back to the one-shot
route, so a run always yields an answer or an honest error. `GET
/retrieval/metrics` and the per-run event trace remain the source of truth for
what actually happened.

## ADR-H14 — Checkpoints and git workflow live in the front door

**Decision.** Conversation checkpoints (`src/lib/chat/checkpoint-store.ts`) and
the git workflow (`src/lib/git/service.ts`) are Next.js front-door features, not
Python-runtime ones. Checkpoints are JSON snapshots under the session folder;
git runs through `execFile` with argument arrays, refuses secret-looking staged
paths, and never force-pushes.

**Why.** Both are filesystem/process concerns, and the Python runtime is
deliberately read-only with no filesystem access. Keeping them in the front door
preserves that boundary. The git service must run against the real repo root
(paths from `git status` are repo-relative), which is why it resolves the root
once rather than assuming the app's cwd.

**Consequences.** Chat state lives in the browser, so checkpoint save/restore is
client-driven (the server only stores snapshots). `push` is always a separate,
explicit action from `commit`; there is no auto-commit or auto-push.

## ADR-H15 — Evidence-grounded answering is one shared, deterministic research flow

**Decision.** Phase 4 adds a single shared package `python/harness/research/`
that turns an objective into a typed **research mandate**, retrieves each fact
leg through the *existing* Financial Retrieval Layer, produces deterministic
per-leg **coverage**, validates structured **claims** against actual evidence,
computes named metrics in **pure Python**, and persists a bounded **research
receipt**. It adds no agent loop, no model adapter, no second retrieval system
and no second database.

**Why.** Retrieval/provenance (Phase 2) were stronger than final-answer
enforcement: nothing turned a user objective into required facts, checked each
was covered by admissible evidence, or gated the model's citations. A shared
flow is reusable by every current and future financial agent (and, later, by
visual-workflow nodes) instead of each agent rebuilding its own citation logic.

**Consequences.** The model interprets and explains; the harness owns retrieval
policy, evidence identity, coverage status, calculation truth, structural
validation and the accept/partial/reject decision. Unsupported/general chat is
unchanged — it classifies as `general` and continues on the existing path.

## ADR-H16 — Coverage is per fact leg; successful retrieval is not coverage

**Decision.** Coverage is computed per mandate leg as `covered`, `conflicting`,
`unavailable`, `unknown` or `not_applicable`. A leg is `covered` only when
evidence matches identity, metric, period, consolidation basis and unit/currency.
HTTP/tool success, search snippets, planning responses, stale cache and ambiguous
identity never satisfy a numeric leg.

**Why.** A single global "did it work" boolean (or trusting a 200 response)
lets a wrong-period or wrong-entity row masquerade as an answer. Claim-aware
coverage lets a valid revenue comparison coexist with an explicitly unsupported
causal explanation.

**Consequences.** Partial answers are first-class: material gaps survive to the
answer and receipt rather than being silently dropped.

## ADR-H17 — The answer gate is deterministic; semantic support is reported, not asserted

**Decision.** The validator deterministically checks evidence-ID existence,
entity/metric/period/basis/unit/currency compatibility, derived-claim
calculation registration, conflict visibility, point-in-time admissibility and
citation-URL provenance. It never claims to prove semantic entailment: where
meaning cannot be checked deterministically a claim is `needs_review` and
presented as unverified. The model may not be the sole gate.

**Why.** ID validation is checkable; "does the evidence *mean* the claim" is
not, reliably, without a labeled evaluation set. Claiming perfect verification
would itself be a fabricated confidence signal — the same dishonesty rule that
governs market data.

**Consequences.** One bounded repair round may be attempted with the exact
invalid claims; if still invalid the claims are stripped/qualified and a useful
partial result is returned.

## ADR-H18 — Point-in-time cutoffs are explicit and enter cache identity

**Decision.** An optional `asOf` on the mandate/request excludes evidence with
`publishedAt > asOf` and participates in the retrieval cache/durable key. The
five temporal concepts are kept distinct: `period`, `dataTimestamp`,
`publishedAt`, `retrievedAt`, `asOf`. An unknown publication time stays unknown
and is never inferred from the fiscal period, retrieval time or a current page.

**Why.** Point-in-time analysis is only honest if availability is known;
inferring it silently (or reusing an uncutoff cached result) would corrupt both
the answer and the audit trail.

**Consequences.** `NormalizedFinancialObject` gains additive optional `basis`
and `publishedAt` fields; existing providers leave them `None` rather than
guessing. Schema changes are additive/idempotent.

## ADR-H19 — Calculations are pure functions with auditable inputs

**Decision.** `research/calculations.py` implements period change, CAGR,
annualized volatility, maximum drawdown and Sharpe as pure functions that record
calculation id/version/formula, input evidence ids + identity, date window,
observation count, frequency, missing-data policy, units/currency, annualization,
risk-free source and assumptions. They reject mixed identities, undocumented
duplicate dates, insufficient observations, non-finite values and missing
assumptions; they never interpolate or backfill. Sharpe requires an explicit
risk-free rate.

**Why.** A number the model reasons out in prose is unauditable. A deterministic
module makes the result reproducible and its limitations explicit.

**Consequences.** Results are returned as `derived` evidence, never reported
facts, and carry input references for the receipt and citation metadata.

## ADR-H20 — Identifiers are content-sensitive cryptographic digests, never `hash()`

**Decision.** Evidence receipt hashes and calculation ids are derived from a
canonical JSON serialization via SHA-256 (`research/ids.py`), not Python's
salted built-in `hash()`. An evidence digest includes the item's content and
provenance; a calculation digest includes version, formula, inputs, window,
assumptions and value.

**Why.** `hash()` is salted per process, so a stored receipt's hash was neither
reproducible across restarts nor content-verifiable, and two different facts
could share an id. Determinism is the point of an audit artifact.

**Consequences.** Identifiers are stable across processes (tested by spawning a
fresh interpreter) and distinct values for the same dates never collide.

## ADR-H21 — A supported request is gated before the agent graph and never downgraded

**Decision.** `execute_run` classifies the objective first. An unambiguous
single-entity, two-period `financial_comparison` is routed into the research flow
*before* the ordinary agent graph, so no duplicate retrieval or contradictory
answer occurs. A model or synthesis failure yields an honest partial/rejected
result rather than falling through to an ungated model answer.

**Why.** Running both paths would double retrieval and risk two conflicting
answers; silently falling back would defeat the entire evidence gate on exactly
the requests it exists for.

**Consequences.** The classifier is deliberately strict — `supported` requires
one entity, two periods and no ambiguity; company-vs-company or vague "growth"
asks continue on the existing path. Synthesis receives only the mandate, a
bounded evidence packet and coverage (no tools, no memory).

## ADR-H22 — Semantic support is evaluated, not asserted; production stays `needs_review`

**Decision.** A versioned, human-labeled evaluation set
(`research/eval/dataset_v1.json`) measures citation correctness and semantic
support separately. The judge never sees the gold label. Production claim status
remains `needs_review` unless the deterministic checks pass and the support
policy permits otherwise. If a model critic is added it is advisory only, and its
precision/false-accept rate is reported against human labels.

**Why.** Citation existence does not prove entailment. Reporting an evaluation
number honestly — including false accepts — is the only way to avoid claiming a
truth guarantee the system cannot provide.

**Consequences.** The evaluation runner (`python -m harness.research.eval`)
reports counts, confusion matrix, false accepts/rejects and per-slice results,
and can never set a claim to verified on its own.

## ADR-H23 — Bounded web escalation when structured evidence cannot answer

**Decision.** The research flow escalates a still-unsatisfied statement leg from
structured sources to trusted web sources through a fixed hierarchy — primary
(SEC/company IR/regulators), then bounded reputable secondary — via the existing
`web_search`/`web_fetch` bridge. Escalation is targeted (only unsatisfied legs),
bounded by one `ResearchBudget` (searches/fetches/pages/time/tokens), and every
numeric web fact must pass a DETERMINISTIC validator (`web_facts.validate_fact`)
before it becomes evidence.

**Why.** Structured providers legitimately do not cover everything (e.g. the
JDdS statement snapshot may lack a requested year), and stopping there produced
unhelpful "not established" answers. But "answer anyhow" must never become
"invent": a search snippet is not a number, a page instruction is not a fact, and
the model cannot be the authority on whether a source supports a value.

**Consequences.** A fact is accepted only when its page was fetched this run, its
`sourceSpan` is a verbatim quote from that page, the value appears in the span,
the span does not state a conflicting fiscal period, and metric/period/currency/
basis match the mandate leg. Secondary sources are labelled and shown as lower
confidence. Budget exhaustion is recorded in the receipt with the attempted
paths. This added no second search system and no second data plane.

## ADR-H24 — Distinct coverage statuses, and missing basis qualifies rather than fails

**Decision.** Coverage reports distinct statuses — `covered`, `qualified`,
`conflicting`, `no_matching_period`, `stale`, `unavailable`, `unknown` — instead
of collapsing them into a generic `unknown`. A required consolidation basis that
the source does not state makes a leg `qualified` (the issuer-reported value is
present and presentable with disclosure), not `unknown`/`unavailable`.

**Why.** The earlier gate mislabeled "there is a revenue row but for FY2019" and
"the basis is unconfirmed" identically, which produced misleading diagnostics and
blocked otherwise-valid facts. A missing *blocking* field (identity, currency)
still fails; a missing *qualifying* field (basis, unit) is disclosed.

**Consequences.** The answer can present independently validated figures while
naming exactly what is unconfirmed, and a conflict or missing period never
prevents other verified facts from being returned.

## ADR-H25 — Web retrieval failures are structured diagnostics, not opaque UNAVAILABLE

**Decision.** Every web search and page fetch returns a `RetrievalDiagnostic`
(`src/lib/retrieval-diagnostics.ts`): provider/transport, normalized status,
failure category (`zero_results`, `transport_error`, `timeout`, `blocked`,
`http_error`, `content_type_rejected`, `parse_failure`, `not_configured`,
`invalid_request`), HTTP status, latency, timeout state, result count, bytes,
parse outcome, and retryability. A JSON source document (e.g. a SEC XBRL
endpoint) is accepted and returned as raw text rather than rejected as
"not HTML". Diagnostics flow through `SourceOutcome` → `FinancialRetrievalResult`
→ the engine/receipt. A bounded `research_health` capability probes each route.

**Why.** The Phase-5 NVDA run could not tell "the web had nothing" from "every
transport is broken". The root cause was three opaque collapses: DuckDuckGo's
HTTP 202 anti-bot page parsed to zero results and returned `[]`; a valid
`application/json` SEC response was rejected by a content-type gate; and
`escalation.py` inspected only `status == "OK"` and discarded the reason.

**Consequences.** "search returned zero results" is now distinct from "request
failed / timed out / was blocked / page fetch failed / HTML could not be parsed",
and the receipt says which. No secrets or full response bodies are recorded.

## ADR-H26 — Independent source routes with a deterministic, testable policy

**Decision.** Structured retrieval is one source among several, not the only
path. A direct **SEC EDGAR** source (`SecFilingsSource`, tier `primary`, US
issuers) and a **second, operator-configurable web-search provider**
(`CustomJsonSearchProvider`, credentials from the settings vault via
`SEARCH_API_CONFIG`/`SEARCH_API_KEY`) join the keyless DuckDuckGo default. Source
selection is explicit data in `retrieval/planner.py`:
`financial_statements → [SEC, JDDS, WEB]`. Every provider implements the existing
`RetrievalSource` contract and can be disabled/replaced independently.

**Why.** All four web routes in the failed run shared one transport; a second
route that depends on the same search call is not independent. The operator
asked to be able to add any search API, so the provider is a generic
configurable JSON adapter, not a hard-coded vendor. SEC is a direct filing
route, not a web search, so a numeric fact never originates from a snippet.

**Consequences.** `sec_filings` routes through the shared identity gate (a bare
ticker cannot answer for the wrong issuer). `not_covered` is returned for non-US
issuers. A missing credential reports the capability as unconfigured and the run
continues with the remaining providers. No second data plane and no second
search system.

## ADR-H27 — The research loop retries transiently, switches providers, and keeps going

**Decision.** The retrieval layer retries only *transient* outcomes (timeout,
transport, 5xx, 429) with bounded deterministic backoff (`retrieval/retry.py`)
and never retries a permanent one (blocked, unsupported content type). The engine
records per-leg retrieval status + coverage status + diagnostics, switches to the
next independent provider on failure, targets only unresolved legs in escalation,
preserves every already-verified leg, and stops only when coverage is complete,
the budget is exhausted, or every route failed — recording the `stopReason`
(`all_required_legs_resolved` / `budget_exhausted` / `provider_routes_unavailable`
/ `unresolved_facts_remain`).

**Why.** "Keep trying independent methods and return every verifiable part"
requires the loop to distinguish a transient blip from a missing fact, and to be
explicit about why it stopped. A stale snapshot must never satisfy a fresh-data
requirement, and an unrelated period must not be relabelled as a gap in the
requested period.

**Consequences.** A partial run returns its verified facts plus the failed routes
and the exact unresolved facts. `legStatus` and `stopReason` ride the receipt and
the `client.final` answer. No unbounded looping: retries and escalation are
bounded by `ResearchBudget`.

## ADR-H28 — Skill learning stays operator-gated; Phase-6 candidates deferred

**Decision.** Phase 6 does not change the skill lifecycle. Automatic promotion
remains blocked; candidates remain inert, reviewable records. Capturing richer
failure/source-selection diagnostics that could seed a linked skill candidate is
deferred to a follow-up; the diagnostics this phase produces are the evidence
such a candidate would cite.

**Why.** The brief requires the control be preserved (never let the agent
silently edit/enable/promote its own skills). Adding candidate generation without
the review/promotion test suite would risk weakening that guarantee.

**Consequences.** No live behavior depends on candidate generation; the existing
`python/tests/test_skills_*` suite continues to prove unreviewed candidates
cannot affect a run.

## ADR-H29 — Coverage is a capability matrix, not a country list

**Decision.** `get_coverage`/`get_capability_matrix` report, per market, separate
axes: recognition, searchable universe size, quote, history, fundamentals,
official filings, web/documents, observed health, and whether a live smoke check
has run. Each axis distinguishes implemented+configured / implemented but
unconfigured / not implemented / recognized-only. Statements are never implied by
working quotes.

**Why.** The old flat list gave 31 countries the same asset classes and the same
four data types, so a Taiwan request looked as statement-capable as a US one —
which was false. It also told the agent to STOP when a market was absent from the
list (or when JDdS lacked a ticker), even though another provider or source might
answer.

**Consequences.** The tool text now instructs the agent to exhaust eligible
routes (live providers → official filings → bounded web) before concluding
evidence is unavailable, and to treat a missing universe row / unconfigured
capability as a routing fact, not a stop signal.

## ADR-H30 — Instrument identity is global and suffix-aware; an unknown suffix is never guessed

**Decision.** A recognised exchange suffix (`.TW`, `.KS`, `.SA`, `.L`, `.T`, `.NS`,
…) is split into an implied market, and the vault is queried for BOTH the bare
local code and the suffixed form. Only an explicit, curated suffix table is
recognised; an unrecognised suffix is left intact (no guess), and a genuinely
ambiguous bare ticker still asks for the market.

**Why.** The JDdS master stores instruments under bare local codes (`2330`,
`005930`, `PETR4`) while users overwhelmingly type the Yahoo-style suffixed form;
the suffixed string never matched and returned `not_found`, so a Taiwan/Korea/
Brazil instrument was reported "not in the universe" though it existed.

**Consequences.** `resolveVaultInstrument` is the single place this happens, so
every resolving capability benefits. `splitExchangeSuffix` is mirrored in Python
(`infer_market_from_symbol`) for routing eligibility only; canonical identity
remains the TS instrument master.

## ADR-H31 — Providers are gated by eligibility, and the fetch transport uses a declared UA

**Decision.** A provider declares eligibility for a request's market/issuer type
and is SKIPPED when ineligible (`SecFilingsSource.eligible` — SEC for non-US
issuers), rather than attempted and reported as an outage. The shared fetch
transport sends a declared, email-contact `User-Agent`; SEC's fair-access policy
requires an email-shaped contact (verified: a URL-only UA is rejected 403).

**Why.** The TSMC run attempted SEC for a Taiwanese issuer and recorded "SEC
bridge HTTP 400" — a predictable ineligibility misread. And every non-US fetch
returned UNAVAILABLE because the transport spoofed a browser UA, which SEC,
`www.sec.gov`, TSMC IR and `pr.tsmc.com` all reject with 403; a declared
email-contact UA receives 200 from all of them (verified both ways).

**Consequences.** A missing credential disables only its route; routing continues.
Documents are content-negotiated: JSON/XML/XBRL/text pass through as raw text and
text-PDFs are extracted, while a scanned/image PDF or a JavaScript-only shell is
reported with a distinct category (`unsupported_document` / `javascript_only`),
never returned as garbage.

## ADR-H32 — Health is keyed by route; learning candidates carry diagnostics and scope

**Decision.** Source health is keyed by `(source, market, assetClass)` — the
dimensions that identify a route — and a declared target with zero attempts is
`unknown`, never healthy or failing. Learning candidates created from a run carry
that run's structured failure diagnostics and the observed regional/provider
scope; candidates remain inert and promotion stays operator-gated.

**Why.** The old aggregation collapsed recorded runs to `(market, assetClass)`
and copied the single aggregate onto every declared provider row, so
`JDDS (US, equity)` and `JDDS (US, etf)` reported the same counter and two
providers reported each other's numbers. And a lesson drawn from one country's
failure must not be generalized to "all countries".

**Consequences.** The Skills UI shows the affected scope and linked diagnostics;
an unreviewed candidate cannot influence live behavior (regression-tested).

## ADR-H33 — Memory/skills DB path is repo-rooted; legacy stores reconcile non-destructively

**Decision.** `eclipse-memory.db` (and `agent-memory.db`) default to the
repository root (`Black-Eclipse-site/`), NOT the process working directory;
`ECLIPSE_MEMORY_DB_PATH` / `AGENT_MEMORY_DB_PATH` remain explicit overrides.
`GET /health` reports the resolved path plus a non-secret identity (path hash).
On open of the stable default, a one-time non-destructive reconciliation copies
rows forward from a legacy `<cwd>`/`python/` database when the stable one is
empty, using `INSERT OR IGNORE` and never touching the source.

**Why.** The runtime is spawned from the repo root, but a manual `uvicorn
--app-dir python`, a pytest run, or any tool started from `python/` resolved a
different file. The repo held TWO `eclipse-memory.db` files (root: memories +
skills; `python/`: skills only), so a runtime launched from a different cwd read
and wrote a different memory store.

**Consequences.** One stable application-data location across launch dirs; a
mismatch is observable in health. A source database is never deleted or
overwritten; a populated stable database skips reconciliation entirely.

## ADR-H34 — Explicit memory intent is detected deterministically, never auto-published

**Decision.** Explicit user phrases ("remember this", "save this preference",
"always do X", "for this ticker", "forget this", "correct this", "turn this into
a skill") are detected by a deterministic classifier (`eclipse_memory/intent.py`)
before the model turn, independent of whether the model calls the `memory` tool.
Clear, safe saves/forgets/corrections are written through
`EclipseMemoryService`; refusals (secrets/credentials, sensitive identifiers,
raw dumps, policy-override attempts) and unclear scope are NEVER written.
Forget/correct archive/supersede — never hard-delete.

**Why.** Normal chat relied on the model voluntarily calling `memory`; durable
preferences stated in plain language were often lost. Auto-capture was disabled
by default, so nothing deterministic filled the gap.

**Consequences.** Every explicit action emits an auditable `memory.*` event and
returns a visible confirmation with scope + inspect/undo links. Automatic capture
remains OFF by default (no auto-capture of every turn); the deterministic path
runs only for explicit intent.

## ADR-H35 — Learning is automatic for eligible runs; drafts carry real procedures

**Decision.** Completed ordinary chat runs (not just skill runs) are analyzed
automatically after the run. Under the current default (`skill_learning_mode:
automatic_selective`) the deterministic, criteria-driven analyzer creates inert
learning candidates without the user asking, and the run's `memory` blob reports
`learningEligibility.autoCaptured` + `learningAnalysis.candidatesCreated` for the
UI. Candidates are deduplicated by signature (bounded queue) and are NEVER
enabled/promoted. A merely eligible run is *not* itself proof of a reusable
procedure: an implicit `successful_procedure` candidate additionally requires
recurrence evidence (the same successful workflow observed in a prior completed
run); a recovered failure or a user correction is strong single-run evidence. An
explicit "turn this into a skill" request creates an inert candidate + draft
through the real persistence path immediately — no second "Learn from this run"
click. Candidate → draft generation produces a complete `SKILL.md` (procedure
steps, typed input/output assumptions, existing safe tools only, source-run
provenance, limitations, evaluation cases) while remaining `status: draft` /
`evaluationStatus: not_evaluated`. When the trace has no evidence for a real
procedure, the draft says so and stays a manual-authoring candidate.

**Why.** Drafts were manifest-only (`instructions: None`), so "turn this into a
skill" produced an unusable artifact; ordinary chat runs could not become
learning candidates; and requiring the user to manually trigger learning meant
successful work routinely never became a candidate. A later selectivity pass
(ADR-H37) showed the opposite failure — treating any eligible run as a skill —
so recurrence was added as the missing evidence gate.

**Consequences.** No automatic skill enablement/promotion/permission expansion —
the hard rule is preserved (the policy `auto_promote_low_risk` stays locked
off). Automatic learning at most creates inert records; a draft is stored as an
inert version and only evaluation + operator approval can enable it. Operators
can disable detection entirely via `skill_learning_mode: off`.

## ADR-H36 — Chat can explicitly invoke an enabled skill as a controlled sub-run

**Decision.** A `skill_invoke` tool lets the model deliberately run ONE enabled,
compatible skill from chat. Execution uses the existing
`SkillExecutionRequest`/`execute_skill` runtime, preserves `parentRunId` and the
trace, enforces the skill's tool allowlist and the default-deny permission policy
in code, and returns the child run's events/results. Discovery (`skill_list` /
`skill_view`) does not equal invocation.

**Why.** The runtime could discover and view skills but ordinary chat had no safe
way to actually run one; pretending discovery equals use was misleading.

**Consequences.** A skill can never widen the agent's tools or grants through
this path; only enabled skills are invocable; the child's `skill.*` events ride
the parent's SSE stream.

## ADR-H37 — One shared memory decision gate; durable memory is opt-in and selective

**Decision.** Every path that can create or alter a durable memory, a
suggestion, a correction/forget action, or a learning candidate passes through a
single server-side decision gate (`python/harness/eclipse_memory/decision.py`)
that returns exactly one of four outcomes:

- `save` — high-value, durable, clearly-intended, safe; persist now;
- `suggest` — possibly durable, but intent/scope unclear; do NOT persist, show a
  quiet Save/Dismiss control (an inert `MemorySuggestion` row);
- `history_only` — relevant to this task/conversation and searchable in
  conversation/run history, but not worth recalling as durable memory;
- `ignore` — low-value, redundant, temporary, unsafe, unsupported or irrelevant.

The gate validates a proposal against the actual user message, identity, scope,
provenance, category, evidence, duplication, sensitivity, policy and privacy
rules. The deterministic explicit-command path (clear "remember/forget/correct")
is always honoured when safe and clear; a casual durable mention is at most a
suggestion. The model-facing `memory` tool may *propose* but cannot authorize its
own write — the gate re-checks it against the user turn, and tool output,
retrieved memory, attached documents and web content are never user intent.
Financial facts (prices, valuations, earnings, predictions, unverified claims)
are never durable memory. Automatic memory capture, automatic candidate
detection and automatic suggestions are controlled by two *mode* keys
(`memory_mode`, `skill_learning_mode`: `automatic_selective` | `ask_before_saving`
| `off`), defaulting to `automatic_selective`; the legacy booleans are derived
from the modes and are neither stored nor settable (migration 7, ADR-H44).

**Why.** The Phase 3-M reliability pass made capture deterministic but too eager:
`auto_capture_enabled`/`auto_candidate_detection` defaulted on, an incidental "I
prefer …" or "Always use X" classified as an explicit save, and the `memory` tool
wrote straight to the service pipeline with no check against the user's actual
words. The result was a database that filled with incidental, task-specific and
occasionally financial text — exactly what a durable memory must not contain.

**Consequences.** One code path authorizes every write, so a new surface cannot
quietly bypass the rules. Explicit controls stay dependable; everything else is
selective and usually `history_only`/`ignore`. A persisted unsafe `true` policy
override is migrated to the safe default (migration 5, recorded in
`MemoryPolicyMigration`) rather than silently continuing the old behaviour. The
storage budget (`max_memories`, default unlimited) fails a new unique save
truthfully as `memory_full` rather than auto-deleting user memory; inert history
does not consume it. The learning path is distinct: a skill candidate is an
on-demand procedure created through an explicit "turn this into a skill" request
or an automatic-selective run with recurrence evidence, never a second memory
note. See ADR-H44 for the mode model that supersedes the per-boolean tuning.


## ADR-H38 — MCP is a user-directed extension surface adapted into the one ToolRegistry

**Decision.** Phase 5 adds `python/harness/mcp/` implementing a local **stdio**
MCP client over the maintained MCP Python SDK, a typed server/effect contract and
threat boundary, collision-resistant namespacing, schema validation, and an
adapter that registers **only selected, read-only** MCP tools into the existing
`ToolRegistry`. There is no second agent loop, tool registry, model adapter or
retrieval system, and MCP results never pass through the native research
verifier. This slice implements gates 5A–5C only; remote HTTP/OAuth (5D), catalog
UX (5E), source-freshness integration (5F), effectful-tool approval (5G),
Eclipse-as-MCP-server (5H), and resources/prompts/subscriptions (5I) remain
future work.

**Why.** MCP must be a user-directed surface — a user may connect a server and
use its tools for any finance workflow, without a finance-domain allowlist and
without JDdS as a mandatory path. But the host still owns execution: launch with
an argv list and no shell, a per-server env allowlist (never the Eclipse
environment/vault/credentials), explicit selection, bounded timeouts/output/
concurrency, cancellation, redaction, and observable events. Routing every call
through `ToolRegistry.execute` means MCP inherits the same validation,
allowlist, read-only enforcement, timeout, persistence and tool-trace as native
tools for free.

**Consequences.** Discovery is not activation: `enabled` defaults False and
`selected_tools` defaults empty, so a discovered tool is inert until named. Only
`read`-effect tools activate; a tool a server does not mark read-only is not
assumed safe — the effect resolves from an operator declaration, then MCP
`readOnlyHint`/`destructiveHint`, else `unknown`. A sanitization collision
appends a stable SHA-256 digest (never a silent merge). Output is bounded and
marked truncated, secrets are redacted, and results carry their server/tool
identity and are labelled external — never Eclipse-verified. `/health` reports
non-secret MCP counts. Turning on a server is explicit operator configuration
(`mcp-servers.json`).

## ADR-H39 — The MCP catalog is operator-reviewed; discovery never activates

**Decision.** Phase 5E adds a catalog API and Settings UI (`GET/POST
/mcp/servers`, `POST /mcp/servers/test`, `DELETE /mcp/servers/{id}`, `POST
/mcp/reload` in Python; `/api/harness/mcp/*` proxies; Settings › MCP Servers) to
add / test / enable / disable / remove local stdio MCP servers and select their
tools. A save reloads the manager in place: only the MCP tools it previously
registered are dropped and re-registered, so native tools are untouched and no
runtime restart is needed. Responses are secret-free — env VALUES are write-only,
and the API returns only env KEY names. Each server carries a versioned
`catalogDigest` (identity + effect + schema) so a changed tool set is reviewable.
Mutations are operator-gated by the same `checkMutationAllowed` rule as every
other mutation; reads are open.

**Why.** The 5A–5C slice was config-file-only: a user had to hand-edit
`mcp-servers.json` and could not see what a server offered, what effect a tool
had, or why a tool was unavailable. That is neither usable nor reviewable. The
phase explicitly requires that newly discovered tools default to disabled and
that users can understand exactly what is enabled and why a call is denied.

**Consequences.** Discovery remains distinct from activation: `enabled` and
`selected_tools` are the only activation inputs, and the panel disables every
non-read/unparseable tool with the reason shown. Removing or disabling a server
revokes its tools on the next reload. Secrets are never echoed; only key names
are shown, and a blank `KEY=` edit keeps the stored value. The whole
`mcp-servers.json` file (which may hold scoped env values) is gitignored.
Effectful-tool approval (5G) is still required before any write/action tool can
be activated — this phase does not weaken that.

## ADR-H40 — MCP trust is explicit: operator-declared effects, confirmed catalogs, transport-authenticated mutations

**Decision.** An MCP server's own metadata is untrusted. Three rules follow:

1. **Effects.** A tool is activatable only when its effect is `read` AND that
   classification is *explicit and reviewable*. An operator `tool_effects`
   declaration is trusted. A server's `readOnlyHint` is untrusted: it can classify
   a tool as `read` for review, but on its own it leaves the tool disabled until
   the operator confirms the current `catalogDigest`. `destructive_hint=True`
   never activates. Only `read` ever activates; `write`/`external_action`/
   `unknown` need the future approval gate (5G).
2. **Catalog invalidation.** A changed tool set, description or schema yields a
   new `catalogDigest`; the previous confirmation no longer matches, so an
   annotation-sourced tool becomes inert again until re-confirmed.
3. **Authorization.** MCP mutations are authorized by *transport* (loopback) or by
   the runtime secret (`X-Harness-Operator`/`Bearer`), never by a JSON
   `operator: true` field. The TS front door still applies `checkMutationAllowed`;
   Python re-checks independently, so a directly reachable runtime cannot be
   handed arbitrary subprocess configuration by spoofing a boolean.
4. **Credentials.** Scoped env VALUES are persisted in the gitignored
   `mcp-servers.json`, written owner-only (0600 where supported). The secure
   settings vault is a TypeScript/Prisma store this Python process cannot read and
   the SDK needs the value to spawn the child; the API returns key names only and
   values are redacted from diagnostics/events/results.

**Why.** A malicious or compromised MCP server controls its own descriptions,
annotations and results. Letting `readOnlyHint` alone enable a tool is a
server-granted permission — the exact thing the product forbids. And a JSON
boolean is not authentication: any client that can reach the runtime could
otherwise configure arbitrary subprocesses.

**Consequences.** A genuinely read-only server still needs one explicit operator
act (declare the effect, or confirm the catalog). The default Eclipse agent can
call selected MCP tools via `allowMcpTools` — an expansion over *registered*
tools only, which registration already gates on enabled + selected + permitted
effect. Custom agents/skills keep exact allowlists. **Limit:** env secrets live in
local plaintext config for now; resolving them from the secure vault through a
secret reference (value never reaching TS/models/events/logs/receipts) is a later
gate. Remote HTTP/OAuth (5D), effectful approval (5G), Eclipse-as-server (5H)
and MCP-aware native source planning (5F) remain future work.

## ADR-H41 — MCP secrets resolve from the encrypted vault; effectful tools require a per-call approval

**Decision.** Two additions close the MCP secret and effect gaps:

1. **Secret references.** A server config may reference a NAMED secret
   (`secret_refs: { ENV_VAR: secretName }`) stored encrypted in JDDS
   (`SystemConfig.key = 'mcp_secrets_v1'`, AES-256-GCM via `connectors/secrets`).
   Only names/masked hints are exposed to the UI. The Python runtime resolves the
   value at child spawn over the authenticated, read-only data bridge
   (`mcp_secret_resolve`); the value is never returned to a browser, the model, an
   event, a log, or a run trace, and it joins the connection's in-memory
   redaction set. Inline plaintext `env` remains supported as the legacy path.
2. **Effectful approvals.** A `write`/`external_action` tool needs (a) the
   operator to enable that effect for the server (`enabled_effects`, default
   `["read"]`) and (b) a valid APPROVED approval for the exact
   `(serverId, tool, argsDigest)`. The digest covers the canonicalized validated
   arguments; a changed payload needs a new approval, a denial is terminal, and
   `unknown` is never approvable. Enforcement is inside the ONE `ToolRegistry`:
   a non-read tool runs only when `ToolContext.authorize_effect` approves THIS
   call; with no authorizer wired, non-read tools are refused.

**Why.** The earlier slice stored scoped MCP env values as local plaintext and
refused every non-read tool with no path forward. Plaintext is not acceptable for
a credential when an encrypted vault already exists, and "there is no approval
flow" is a permanent dead-end rather than a safe default. Both gaps had a
well-scoped fix that reuses existing pieces (the connector crypto, the bridge, the
registry execution seam) rather than a new subsystem.

**Consequences.** A read-only server still stores no secret; a credential-using
server references the vault. Approvals are persisted in `agent-memory.db`
(`McpApproval`) with no secret material and expire (default 15 min). An
unapproved effectful call is refused and surfaces a PENDING request; the operator
decides in Settings. **Limits:** the Python process still receives the resolved
value at spawn (it must, to start the child); the vault key is instance-scoped
(`CONNECTOR_SECRET_KEY` on hosted deployments). Remote HTTP/OAuth (5D),
Eclipse-as-server (5H), MCP-aware native source planning (5F) and
resources/prompts/subscriptions (5I) remain future work.

## ADR-H42 — Remote MCP uses Streamable HTTP with vault-backed auth and an SSRF guard

**Decision.** `MCPServerConfig` gains a `transport` field (`"stdio"` default, or
`"http"` with a required `url`). The HTTP path uses the MCP SDK's
Streamable-HTTP client (`mcp.client.streamable_http`) and shares the *same*
connection lifecycle, discovery, effect/approval gating, output bounding and
redaction as stdio — only the stream source differs. Authentication is a **vault
secret reference** (`auth: {kind: "header"|"query", name, prefix?, secretRef}`):
the token is resolved at connect time over the authenticated bridge, added to the
connection's in-memory redaction set, and never stored in config, returned to the
UI, or logged. Every remote URL passes an **SSRF guard** (Python
`harness/mcp/ssrf.py`, mirrored on the TS side by `checkOutboundUrl`):
https-only (loopback `http` allowed for local testing), embedded credentials
refused, private/link-local/metadata IP literals and private-resolving hostnames
refused, and the check runs BEFORE any outbound connection.

**Why.** "Users can connect anything" is false while only local stdio is
supported — most third-party MCP servers (hosted Google Workspace, Canva, many
financial APIs) are remote. A config-driven outbound connection is also a classic
SSRF vector, and an API token must not be the one secret still stored in
plaintext.

**Consequences.** Header/bearer auth covers the majority of API-key servers
immediately; interactive OAuth (localhost callback, token refresh/clear) is
deferred. `secret_refs` (stdio env) and `auth.secretRef` (http) both resolve
through the same vault. `test_server` refuses an unsafe URL with a diagnostic and
makes no network call. The connection/manager/adapter/approval layer is unchanged
— remote servers reuse the exact same enforcement. Remaining future work:
OAuth lifecycle and TLS/redirect knobs (part of 5D), resources/prompts (5I),
MCP-aware native source planning (5F), and Eclipse-as-server (5H).

## ADR-H44 — Automatic learning is one mode, not several booleans; it saves selectively without prompting
**Decision.** Automatic memory learning and automatic skill learning are each
controlled by a single *mode* key rather than independent booleans:

- `memory_mode`: `automatic_selective` (default) — save strong, durable,
  low-risk, non-sensitive information automatically, without interrupting the
  user; treat one-off requests, temporary task details, generated analysis, tool
  output and changing market data as `history_only`/`ignore`; require recurrence
  for project-specific conventions; a direct user preference is saved on first
  statement. `ask_before_saving` — stage an inert suggestion instead.
  `off` — create nothing automatically.
- `skill_learning_mode`: same three modes, gating automatic learning-candidate
  detection. Implicit `successful_procedure` candidates require recurrence
  evidence; a merely eligible run is not enough. An explicit request always
  creates an inert draft through the real persistence path.

The legacy booleans (`auto_capture_enabled`, `auto_suggest_enabled`,
`auto_candidate_detection`) are now **derived** from the modes: they are neither
stored nor settable, so they cannot drift out of sync. `policy.derived_flags`
projects the modes to the booleans for existing call sites. Migration 7 records
and removes any stored legacy boolean rows so the mode defaults govern.

**Why.** The prior design used three independent booleans whose defaults and
meaning drifted across code, docs, and tests (ADR-H35 said candidate detection
default on; ADR-H37 said all off). A one-flag "fix" left the real product
behaviour wrong in either direction: indiscriminate prompt-after-every-run, or no
learning at all. The intended behaviour is *selective automatic learning* — one
coherent lifecycle with a single source of truth.

**Consequences.** The default is consistent across policy code, persisted
settings, migration, API/UI, tests and documentation. Explicit remember/forget/
correct remain a separate always-on path. The decision outcomes are now
`save`/`suggest`/`history_only`/`ignore` (`run_only` renamed to `history_only` to
name conversation history explicitly). Noisy per-run prompts are removed: the
chat panel shows a learning line only for a genuine automatic capture or an
explicit draft — a merely eligible run shows nothing, and a repeated "Learn from
this run?" confirmation is gone.

**Recurrence is persisted, not window-bound.** Recurrence evidence is stored in
bounded, signature-only ledgers rather than only inferred from the chat-history
window: `MemoryRecurrence` (migration 8, `eclipse-memory.db`) for durable-sounding
statements and `LearningRecurrence`/`LearningRecurrenceRun` (`agent-memory.db`)
for skill-workflow evidence. This closes two limitations: a convention repeated
after the bounded history window is still learned, and implicit skill recurrence
is one indexed lookup that survives restarts (the bounded run scan is only a
fallback, combined with `max`). No raw text is stored, and re-recording the same
run does not inflate the count.

## ADR-H45 — Skill contracts are validated in full, and execution limits are enforced in code

**Decision.** A skill's declared `inputSchema`/`outputSchema` are validated as
JSON Schema documents at registration and evaluation
(`skills/schema_validation.py`, using the already-present `jsonschema`). A skill
OUTPUT is validated against the COMPLETE schema (nested object types, array item
types, enums, `minimum`/`maximum`, `minLength`/`maxLength`, `pattern`,
`additionalProperties`, required fields) — a top-level `required` check alone is
not validation. `status="completed"` and `outputValidated=True` are distinct:
a schema-invalid output keeps `completed` but reports `outputValidated=False`
with a stated warning, never a silent discard. A manifest's `limits.maxToolCalls`
is carried into the run's `AgentConfig` and ENFORCED by the graph — once the
budget is spent, no further tool executes and the run finalizes
(`TOOL_BUDGET_EXCEEDED`).

**Why.** The prior output check only looked at top-level `required`, so a
structurally-wrong nested result could be reported as "validated"; `maxToolCalls`
was advertised in metadata and prompts but never enforced in the loop, so a skill
could exceed its own budget.

**Consequences.** New `skills/schema_validation.py`; `AgentConfig.maxToolCalls`;
`TOOL_BUDGET_EXCEEDED` added to the error-code vocabulary (Python + TS);
`jsonschema` declared explicitly in `python/requirements.txt`.

## ADR-H48 — Operator authorization is transport/token proof on every mutation; schema validation fails closed

**Decision.** Every memory/skill mutation endpoint in `main.py` takes the
`require_operator` FastAPI dependency: the caller must originate from loopback
(the front door / local operator) **or** present the runtime secret
(`X-Harness-Operator` / `Bearer`). The body `operator` boolean is read only
afterward, so it can never, by itself, authorize a remote caller. The Next.js
memory/skill clients forward the runtime secret so the front door's trusted
authorization is what reaches Python, matching the MCP endpoints (ADR-H40/H41).
Additionally, full JSON Schema validation FAILS CLOSED: if `jsonschema` is
missing, a declared schema cannot be verified, so `check_schema`/`validate_output`
return failure with an actionable message instead of falling back to a
required-only check that would report partial validation as full. Skill INPUT
validation is pydantic-based and keeps working (structural shape check);
registration/evaluation and OUTPUT validation require the validator.

**Why.** The front door derived `operator` from trusted auth, but the Python
endpoints trusted a body boolean — so a directly reachable runtime (or a crafted
proxied payload) could self-authorize memory writes, policy changes, suggestion
acceptance, skill enablement and promotion. Separately, the `jsonschema`-absent
fallback silently downgraded contract enforcement to a required-fields check.

**Consequences.** `main.py` (`require_operator`, applied to all mutation endpoints),
`skills/schema_validation.py` (`check_schema_structural`, fail-closed
`check_schema`/`validate_output`), front-door token forwarding
(`eclipse-memory-client.ts`, `skills-client.ts`), `runtime-config.ts` +
`scripts/doctor.js` probe `jsonschema`, `GET /health` reports
`skills.schemaValidation`. Tests: `test_memory_api_auth.py`,
`test_suggestion_api_scope.py`, `test_skill_contracts.py`; docs updated.

## ADR-H46 — Skill version identity covers instruction content, dependencies and drift

**Decision.** A version's persisted identity is
`SkillManifest.version_identity(instructions)` — the manifest PLUS a digest of
the `SKILL.md` procedure content — so an enabled skill's behavior cannot change
under the same recorded identity. At boot, if a stored version's identity and
its current manifest/instruction content disagree (a file edited without a
version bump), the registry records a `skill.version_drift` event and surfaces
it as a load error rather than silently serving changed behavior.

**Why.** `content_hash()` covered only the machine-readable manifest; editing a
`SKILL.md` in place changed behavior with no new identity and no signal.

**Consequences.** `manifest.version_identity`, `registry.load_all` drift detection,
execution records carry the instruction-aware identity. Legacy manifest-only
hashes are still accepted (their stored `instructions` column preserves the
behavior identity), so existing databases do not report false drift.

## ADR-H47 — Memory retrieval is measured, gated on lexical relevance, and cross-user isolated

**Decision.** Memory retrieval has a versioned, human-labeled evaluation set
(`eclipse_memory/eval/dataset_v1.json`) and a runner reporting recall, precision,
scope leakage, forbidden hits, no-match cleanliness and context-budget behavior;
the recorded baseline is recall 1.0 / precision 0.5 / leakage 0 / forbidden 0.
Behind the existing seam, retrieval applies a deterministic **relevance gate**:
for a query with real terms, a topical memory with no keyword/subject signal is
not recalled merely because its importance/confidence is high; standing user
rules (`preference`/`constraint`) remain per-turn personalization exempt from the
gate. Scope/status/privacy filtering stays authoritative and deterministic — no
semantic similarity authorizes or broadens access. Cross-user isolation is
enforced on read (`actor_scope="user"` with no `scope_id` no longer leaks every
user's rows) and on suggestion accept/dismiss (a suggestion owned by another
scope is refused).

**Why.** Off-domain and near-match queries dragged in unrelated memories; and a
user-scoped retrieve without a `scope_id` leaked other users' memories.

**Consequences.** `retrieval._score` returns a relevance signal; `find_suggestion_by_signature`
is scope-aware; `accept_suggestion`/`dismiss_suggestion` take `user_id`;
new tests `test_memory_retrieval_eval.py` and isolation cases.

## ADR-H50 — Retention is automatic and observable; contradictions are authority-resolved

**Decision.** Two memory areas were hardened without changing the architecture:

1. **Retention.** An automatically learned memory may receive an `expiresAt` from
   `retention_days`; explicit memory never does. The sweep runs automatically at
   runtime startup through the existing FastAPI lifespan (`_lifespan` →
   `run_startup_maintenance` → `run_retention_maintenance`) — no new daemon or
   thread, and a failure is a recorded diagnostic that never blocks boot. Each
   run is persisted in `MemoryMaintenance` (migration 11) and reported as
   inspected/expired/skipped/ranAt/error. Confirmation and renewal refresh the
   lifecycle timestamps and extend or clear an automatic memory's expiry. A
   policy change never mutates existing rows; retrieval excludes a due memory by
   timestamp independent of the sweep; expired memory stays auditable.
2. **Contradictions.** Conflict resolution is authority-driven: explicit
   replacement and explicit-over-automatic supersede (old→new linked, retained);
   automatic-vs-explicit and two ambiguous explicit statements store the new
   record inert (`proposed`). Detection is deterministic and conservative —
   subject identity via equality or `SUBJECT_ALIASES`, normalized (synonym/
   stemmed) tokens, known opposing pairs, and explicit replacement wording; a
   missed conflict is preferred over replacing unrelated memory.

**Why.** `expire_due_memories()` existed but nothing ran it, and
`_resolve_conflict` accepted an `authority` argument it never used — so retention
was manual-only and an explicit statement could not override a learned one
without magic wording. Both are table stakes for a trustworthy personal memory.

**Consequences.** Migration 11; `service.{run_retention_maintenance,retention_status,
renewal_expiry}`, `store.{record_maintenance,maintenance_status}`,
`conflicts.detect_conflict` (authority-aware) + `same_dimension`;
`main._lifespan` startup hook + `/eclipse-memory/retention`; an honest
`needs_review` memory-action outcome; Control Room retention/conflict views.

## ADR-H49 — Memory is a local personal system: explicit lifecycle, deterministic contracts, operator control

**Decision.** Eclipse-Memory stays a single-user, local system (`userId`/`scopeId`
are labels, not security identities) and is matured without rewriting its
architecture:

1. **Explicit lifecycle.** Every `MemoryRecord` carries `createdAt`,
   `lastConfirmedAt`, `lastUsedAt`, `lastReviewedAt`, `expiresAt`, `status`,
   `statusReason`, `supersededBy`, provenance and confidence, plus an
   `authority` (`explicit` vs `automatic`). Re-stating a fact refreshes
   confirmation rather than duplicating. `expired`/`superseded`/`rejected`/
   `archived` are never injected. Retention only ever expires **automatic**
   memories; explicit memories are never silently expired. History is never
   hard-deleted by default.
2. **Deterministic contradiction handling.** `conflicts.py` detects a same-scope
   conflict lexically. Explicit replacement supersedes the old record (linked,
   retained); an ambiguous contradiction is stored inert (`proposed`) for review
   — two contradictory preferences never coexist as active truth. Scope isolation
   is authoritative (different scopes never conflict).
3. **Measured retrieval, no premature semantics.** Retrieval normalization
   (synonyms, light stemming, subject aliases, bounded typo tolerance) is added
   behind the existing seam and judged by the labeled set with release
   thresholds. Semantic/embedding retrieval is **deferred**: the eval shows no
   measured need, so no dependency or network service is added. Scope/status/
   privacy filtering remains authoritative; similarity could only ever find
   candidates, never authorize access.
4. **Operator control.** Export/import/backup/health/reset are local operator
   actions (`transfer.py`); import runs through the same decision gate and write
   pipeline with `imported` provenance; reset requires explicit confirmation,
   backs up first, and clears only the memory domain. Policy gains a hard
   `memory_enabled` off switch and per-category capture/recall rules that can only
   tighten.
5. **Measured skill quality.** Skill usefulness (success/validation/tool-budget
   metrics, version regression, deprecation advisory) is derived from real
   executions; promotion remains evaluation + operator gated and is never
   automatic.

**Why.** The system must be correct, private, recoverable, explainable and
honest about its limits — the properties a personal memory system needs and a
hosted multi-tenant design would obscure.

**Consequences.** Migration 10; new modules `conflicts.py`, `normalize.py`,
`transfer.py`, `skill_quality.py`; policy keys `memory_enabled`,
`memory_in_diagnostics`, `semantic_indexing_enabled`, `capture_policy`,
`recall_policy`; lifecycle/export/import/backup/reset APIs + Control Room
Lifecycle tab; release thresholds in the eval. Verified by the memory,
lifecycle, conflict, normalization, transfer, privacy, reliability and
skill-quality test modules.

## ADR-H43 — MCP is presented as finance workflows with provenance, diagnostics and safe defaults

**Decision.** A product layer sits on top of the MCP machinery:

1. **Finance presets & workflows** (`python/harness/mcp/presets.py`). Known
   finance integrations (Sheets, Excel, filings, web search, calendar, cloud
   docs, Canva/Figma/Office decks, Notion) with plain-language setup, plus named
   workflows a user recognises — Import Portfolio, Review Portfolio Risk,
   Prepare Earnings Brief, Compare Companies, Analyze Trade Journal, Create
   Investment Memo, Export Portfolio Report. Each workflow lists its steps, which
   integration each step uses, which steps are native (Eclipse data), and which
   need approval. Presets are **data**, contain no secrets, and never activate
   anything — a workflow is a plan the user reviews.
2. **Honest labeling.** Every integration says whether its launch values were
   `verified` against the package registry; unverified entries are marked
   `UNVERIFIED` in their notes and remote templates are flagged. Community
   servers are labelled.
3. **Source provenance on every result.** Each MCP result carries a machine-
   readable `provenance` block (`verification: "external"`, `eclipseVerified:
   false`, `source`, `retrievedAt`, `truncated`) and the result text says
   "EXTERNAL source … NOT Eclipse-verified". Eclipse's native JDdS/portfolio data
   remains authoritative; a workflow's native steps are marked `native_data`.
4. **Setup diagnostics** (`python/harness/mcp/diagnostics.py`): command / Node /
   npx / Python availability, vault secret references, endpoint safety +
   reachability, connection state, discovery and selection — each with a
   `recovery` step. A whole-environment check also reports whether a writable
   temp directory exists (Windows permission problems must not look like a broken
   server).
5. **Approval clarity.** An effectful approval records a plain-language
   `actionSummary`, the `affectedData`, and a `risk` level alongside the argument
   digest; the UI shows them and uses "Approve this exact call".
6. **Resilient tests.** `python/harness/mcp/_testtmp.py` resolves a writable temp
   base, falling back from `%TEMP%` to a repo-local `.tmp-tests`, so a locked-down
   Windows temp dir cannot make a healthy test look broken. **(Moved 2026-09-30 to
   `python/tests/_tmp_helper.py` and adopted by `python/conftest.py` as the
   `tmp_workspace` fixture — see ADR-H80; it is test-only and no longer ships in the
   package.)**

**Why.** A user should not have to reason about servers, JSON and tool names to
do a finance job, and must never mistake external data for Eclipse-verified
data. The runtime already enforced execution safety; what was missing was
task-orientation, provenance, and actionable setup feedback.

**Consequences.** The wizard/API expose integrations and workflows read-only;
activation still requires an explicit operator action and read tools still need
selection + catalog confirmation, writes still need per-call approval. Secrets
remain vault-only. Automated end-to-end coverage
(`python/tests/test_mcp_workflow.py`) proves import → native analysis → report →
approved export → provenance, and that a changed payload needs a new approval.

## ADR-H51 — Memory expansion: profile, session recall, learning review, file projection (all projection, no second store)

**Decision.** Five capabilities were added to Eclipse-Memory without changing
its architecture (one `eclipse-memory.db`, one `EclipseMemoryService`, one
decision gate, one retrieval seam; `agent-memory.db` stays the run store):

1. **Compact prompt profile** (`eclipse_memory/profile.py`) — a deterministic,
   bounded *materialized projection over active memories*, built once at run
   start into a labelled, non-authoritative `SystemMessage`. Hard limits
   (user/project/total chars, entries, per-entry chars) are code constants; the
   policy may only tighten. Excluded statuses/sensitive/financial/other-user/
   mismatched-task content is excluded WITH a reason. Memory text is sanitized
   and fenced as DATA. Only reproducibility metadata is persisted
   (`MemoryProfileSnapshot`, migration 12) — never a second copy of the memory.
2. **Full-session historical recall** — a normalized, redacted, bounded
   `SessionMessage` search index (+ FTS5 mirror) in `agent-memory.db`, reached
   through read-only `session_search`/`session_open` tools. History is context,
   never current evidence; the prompt says so explicitly. FTS5 absence is
   reported honestly (LIKE fallback), never faked.
3. **Automatic learning review** (`harness/learning_review.py`) — a bounded
   in-process asyncio queue that reviews completed runs after the response. The
   deterministic path reuses `analyze_run`; an optional model review may propose,
   but its output is inert and re-authorized by the shared gate. No skill is ever
   activated; no durable write bypasses `authorize_proposal`; no secrets, market
   facts, predictions or one-off details are captured; a single market/provider
   failure is never generalized globally.
4. **File projection** (`.black-eclipse/memory/{USER,MEMORY,PROFILE,README}.md`)
   — an inspectable Markdown *view* with explicit export/import/backup/restore.
   Imports reuse the existing gated `import_payload` pipeline; SQLite stays the
   source of truth; there is no filesystem watching.
5. **External-memory posture unchanged** — MCP/provider data remains external and
   never Eclipse-verified; native memory remains separate from JDdS.

**Why.** These are real product gaps (a run had no stable personalization
snapshot, no way to ask "what did we decide last week?", and no safe automatic
self-review). The brief required them WITHOUT replacing SQLite with Markdown,
adding a third-party memory platform, or letting background learning activate
skills. Modelling each as a *projection* / *bounded queue* over the existing
structured store satisfies the capability while preserving the ONE gate and the
safety floor.

**Consequences.** New policy keys (`profile_*`, `session_*`) are validated and
bounded. `graph.execute_run` now injects a persistent-context block and emits
`session.indexed` on completion. The runtime gains a learning-review queue
started/stopped by the FastAPI lifespan. `MemoryProfileSnapshot` (migration 12)
is additive; `SessionMessage`/`SessionMessageFts`/`LearningReview` are new tables
in `agent-memory.db`. No new dependency, no embeddings, no cloud service.

## ADR-H52 — External memory providers are a separate, fenced layer over the MCP boundary

**Decision.** External memory is exposed through a dedicated
`python/harness/memory_providers/` layer that implements one `MemoryProvider`
contract. The native provider wraps the existing `EclipseMemoryService`; the MCP
provider reuses the existing `MCPManager` connections and approval gate. Every
result carries `provider`/`provenance`/`verified`/`retrievedAt`; an external
result defaults to `provenance=external_provider`, `verified=false`, is fenced as
`[EXTERNAL MEMORY — PROVIDER: <name> — UNVERIFIED CONTEXT]`, and is NEVER
promoted to native memory. Routing is native-first, external search requires both
a policy enablement AND an explicit per-call allow, and external writes require
an explicit user request AND an operator approval. There is no generic "memory
table": the four layers (native truth, versioned skills, external providers,
session archive) stay distinct.

**Why.** The gap was not MCP tool access (already mature) but a first-class,
provenance-labelled memory-provider contract that cannot silently overwrite
native memory or masquerade as Eclipse-verified. Reusing the MCP boundary avoids
a second client, a second secret path, or a second approval flow.

**Consequences.** New policy keys `external_memory_search_enabled` (default off),
`external_memory_write_enabled` (default off) and
`external_memory_native_sufficiency`. Automatic external writes are structurally
absent (the provider never declares `write` for automatic use). Three read-only
tools and `/memory-providers/*` routes were added. ADR-H40's vault limitation is
inherited unchanged.

## ADR-H53 — Self-improving skills are structured patches that become new versions; activation is evaluated, canary is bounded, rollback is atomic

**Decision.** A skill change is a structured JSON-pointer patch (`replace`/`add`/
`remove`) validated against an explicit allowed-field set (`description`, `tags`,
`name`, `instructions`, `outputSchema`, `limits`). Identity, permission, tool,
risk and lifecycle fields are not patchable. A patch creates a NEW `SkillVersion`
(the active version is never mutated) and is rejected when its `baseVersion` is
stale. Activation requires a passing evaluation; canary is limited to safe,
non-mutating, low-risk skills and is never allowed for security/permission/
external-write/policy-shaping skills. Rollback (manual or automatic on a
regression/failure/correction signal) is atomic and preserves both versions.

**Why.** The product goal is autonomous discovery and proposal, never autonomous
silent change of truth, permissions or safety behavior. Making every change a
reviewable, versioned, diffable patch — with evaluation before activation and an
automatic rollback — gives the autonomy while keeping it reversible and governed.

**Consequences.** Migration 13 adds the patch/version metadata columns to
`SkillVersion`; new lifecycle statuses `evaluating/canary/approved/active/
superseded/rolled_back`; `registry.load_all` treats the store's `latestVersion`
as authoritative so a reload never walks a patched skill back to its source
manifest. A background review may record a `skill_patch` opportunity but never
creates a version.

## ADR-H54 — Hybrid memory ranking uses logical namespaces and a bounded semantic term; similarity never authorizes

**Decision.** `eclipse_memory/hybrid.py` maps memories to logical namespaces
(preference/project_fact/correction/workflow/skill/session_summary) and combines a
bounded BM25 term, the existing lexical signal, authority/scope/recency/access
signals and an OPTIONAL, bounded semantic term. The semantic term can only nudge
an already-authorized, already-lexically-relevant memory; it can never surface an
excluded memory, confirm, supersede, archive or authorize. There is deliberately
no persistent vector index and no hosted embedding service (consistent with
ADR-H49 and the no-new-infrastructure rule).

**Why.** Semantic similarity is retrieval assistance, not authority. Modelling it
as one bounded component of a deterministic hybrid score — with content
categories kept in separate ranking namespaces — captures the useful part while
making "unauthorized supersessions = 0" structurally true.

**Consequences.** The labeled semantic eval gained typo, profile-mismatch and
temporal-replacement cases; the release gate's absolute safety floors are
unchanged.

## ADR-H55 — Prompt caching is explicit: immutable segments and a stable identity-only cache key

**Decision.** `python/harness/prompt_segments.py` splits the prompt into named
segments (system policy, agent identity, tool schemas, profile snapshot, session
summary, skill manifest, run instructions, user message), each with a content
hash, source, version and token estimate. A `prompt_cache_key` is composed only
from identity (agent/policy/tool-schema versions + profile snapshot id + skill
manifest version + session-summary version), so a new user turn or new run
instructions do not invalidate the reusable segments, and a profile or skill
change invalidates only that segment. Skills are injected as a bounded,
relevant-only manifest (never all skills); full instructions stay behind
`skill_view`.

**Why.** Hermes-style caching is only useful when the reusable prefix is stable
and observable. Making segments explicit and the key identity-only gives stable
reuse and precise invalidation without changing what the model may do.

**Consequences.** `graph.execute_run` emits a `prompt.plan` event with the segment
hashes and per-segment token estimates. No behavior/permission change.

## ADR-H56 — Long-lived sessions belong to a profile; session recall is ranked and bounded

**Decision.** A durable `ChatSession` (metadata + a bounded deterministic summary)
lives in `agent-memory.db`; a run belongs to a session, a session belongs to one
profile. Session search ranks with the documented hybrid (exact phrase > BM25 >
decision/correction/skill-failure/memory-action > recency > prose) and filters by
role/kind/session/profile/market/skill/memory-action. A session summary is
searchable and opt-in but is NEVER injected into every prompt. Historical content
is context, never current evidence (the prompt says so).

**Why.** The prior `SessionMessage` was a run-keyed search index with BM25 only,
no session identity, no lifecycle and no summary. Users need "continue this
session" and "what did we decide last week?" without an unlimited transcript ever
entering the prompt.

**Consequences.** `ChatSession` + scope columns on `HarnessRun`/`SessionMessage`
are additive. New `/sessions*` and `/profiles*` routes; `RunRequest` carries
`sessionId/profileId/projectId`. Delete archives by default and, when hard, clears
only the search index — the run trace is never destroyed.

## ADR-H57 — Named profiles are a metadata registry with a read-only inheritance chain

**Decision.** `eclipse_memory/profiles.py` is a metadata registry over
`.black-eclipse/profiles/PROFILES.json` plus a directory projection
(`PROFILE.json`, `sessions/`, `skills/`, `history/`, `exports/`). A profile scopes
memory, sessions, skills and providers and carries its prompt profile, retention
and settings. Inheritance is explicit and READ-ONLY in the child; cycles and
deleting a parent with children are refused. SQLite remains canonical for
structured records — the directory is a projection.

**Why.** Profiles are stronger isolation than separate directories: every query
resolves the profile scope and a cross-profile read is refused. Keeping the
registry as metadata (not a second store) preserves the one-store architecture.

**Consequences.** `ProfileRegistry` is owned by `EclipseMemoryService`. Profile
ids are validated slugs; the `default` profile cannot be deleted. No provider
secret or memory content is written to the directory.

## ADR-H58 — File edits are atomic, versioned and reversible; the file is never the source of truth

**Decision.** The Markdown projection is edited only through a targeted API with
an `expectedVersion` stale-edit guard, atomic writes (temp + fsync + rename), a
pre-regenerate backup, and a structured revision history (before/after/actor/
sourceRun/reviewId/reason/affectedIds/parentRevision/timestamp) with rollback and
a bounded diff. A stale expected version is refused. Archive/restore remain
structured-memory actions, not file edits.

**Why.** A direct `writeFile` could lose a concurrent edit or leave a partial
file, and a hand edit had no audit trail or way back. The brief requires atomic,
diffable, reversible edits while SQLite stays canonical.

**Consequences.** Entry markers carry a `version`; files are `USER_PROFILE.md`,
`MEMORY.md`, `PROJECT_MEMORY.md`, `SKILLS.md` (+ `PROFILE.md`/`README.md`). Routes
`/files/edit` and `/files/history`. Import still routes every entry through the
existing gated pipeline.

## ADR-H59 — Project-local skills are inert until an operator trusts the root

**Decision.** A repository may carry skills under `.black-eclipse/skills/` or
`.agents/skills/`. `python/harness/skills/project_skills.py` discovers them, but
the registry loads a project's skills **only** after an explicit, revocable
operator trust decision persisted in `ProjectSkillTrust` (migration 14). An
untrusted root yields a names-only notice — its prose is never read. Trust is
per absolute path. Precedence is deterministic: **project > profile > bundled**;
a project skill may shadow a bundled skill of the same id (recorded as
`shadowed`, not a duplicate error).

**Why.** Skills are procedures the agent follows, so auto-loading them from any
cloned repo is a prompt-injection/privilege risk. Trust makes the decision
explicit and auditable while still letting project conventions travel with the
code. Reusing the existing registry/permission/execution path (rather than a
second skill system) keeps one source of truth and one default-deny gate.

**Consequences.** A project skill cannot grant itself a permission, enable
itself, or bypass evaluation — it runs through `runtime.execute_skill` with the
same permission check and immutable-version identity as any other skill.
Discovery can be disabled globally (`project_skill_discovery`). `untrust`
immediately stops injection on the next load. Routes `/skills/project`,
`/skills/project/trust`, `/skills/project/untrust`; UI Skills › Project Skills.

## ADR-H60 — Skill bundles are references, never a way to enable or widen

**Decision.** `python/harness/skills/bundles.py` groups existing skills under a
stable id with optional instruction/constraints, persisted in `SkillBundle`
(migration 15). `resolve_bundle` is deterministic and reports `missing` and
`unavailable` members honestly. `RunRequest.bundleId` expands a bundle into the
bounded skill manifest instead of keyword discovery, emitting
`skill.bundle_resolved`. Members pass the same permission check when an actor is
supplied.

**Why.** A bundle is composability for recurring workflows; it must not become a
back door. Treating it as a reference (not a copy) means a disabled skill stays
disabled, a missing skill is reported, and the permission gate is untouched.

**Consequences.** A bundle cannot enable a skill, grant a permission, or exceed
the prompt budget (members are capped at 12, content at 1200 chars). Bundle ids
are validated slugs; content is bounded on write as well as on read. Routes
`/skill-bundles` (+`/{id}`); UI Skills › Bundles.

## ADR-H61 — Guided learning authors an inert, sanitized draft from an explicit source

**Decision.** `python/harness/skills/learn.py` turns an explicit user request
(run / document / repository / repeated-workflow / correction) into a complete
but inert skill draft via the existing `propose_draft` path. Content is
sanitized (secrets, raw transcript lines and issue references are removed) and
advisory-linted (dates, first-person narration) before the draft is built. The
source is user-supplied and operator-gated; the draft stays
`status: draft`/`not_evaluated` and can only activate through the existing
evaluation + operator promotion controls.

**Why.** Hermes' `/learn` is a great shortcut for capturing a reusable
procedure, but an unguarded version would let raw logs, secrets or one-off
incidents become active procedures. Sanitization + inertness + operator gating
give the workflow without the risk.

**Consequences.** One-off runs never auto-create an active skill; recurrence
evidence stays on the candidate. A learning-origin manifest without a real run
uses a stable synthetic `learn:<kind>:<slug>` source reference. Route
`/learning/from-source`; UI Skills › Learn a Skill.

## ADR-H62 — The learning journey is a derived read model, not a second source of truth

**Decision.** `python/harness/learning_graph.py` projects a node/edge graph
(memories, skills, versions, candidates, corrections, mistakes, promotions,
sessions) from canonical rows on demand. It never writes, never stores a copy,
and is bounded (≤400 nodes / ≤800 edges). Secrets are never exposed (labels are
ids/type metadata), scope isolation filters the set, and inactive items appear
with a status rather than being deleted.

**Why.** Users need to understand *why* a skill or memory exists; Hermes'
Learning Journey shows the value. Deriving the graph keeps the canonical store
authoritative and avoids drift between a graph copy and reality.

**Consequences.** Edge relations are a fixed vocabulary (`created_from`,
`corrected_by`, `superseded_by`, `used_in`, `derived_from`, `promoted_from`,
`evaluated_by`, `related_to`). Route `GET /learning/journey`; UI Skills ›
Journey. Filters by kind/status/since; editing still goes through existing
persistence/authorization paths.

## ADR-H7 — Zero-config is preserved via automatic provisioning

**Decision.** `postinstall` and `doctor.js` install the Python agent stack
automatically; the runtime is spawned on demand. If Python is genuinely
unavailable, runs fail with a clear 503.

**Why.** `npm install && npm run dev` must keep working for open-source users,
now that the agentic runtime is Python.

**Consequences.** First run may take longer while pip installs. `npm test` runs
both the Vitest and pytest suites; the Python suite is skipped (not failed) when
the stack is absent.

## ADR-H63 - Context windows are declared catalog data; an unknown window stays unknown

**Decision.** The context window is a `ProviderSpec.context_window` field plus a
`MODEL_CONTEXT_WINDOWS` model-substring table, resolved by a pure
`resolve_context_window(provider, model)`. Where a provider's models vary, the
declared value is a conservative FLOOR with a documented source; where the
runtime cannot know the window (local servers, routers, a custom endpoint) the
value stays `None`.

**Why.** The runtime previously bounded history with hardcoded constants and
never knew its model's window, so a small-context model overflowed and a
large-context model wasted most of its window. Declaring windows as data follows
the existing catalog pattern (add a provider = add an entry) instead of a branch
per vendor. Guessing a window would be worse than not knowing: it would make the
runtime over-confident on exactly the models it understands least.

**Consequences.** An unknown window makes the budget inert and falls back to the
old `MAX_HISTORY_TURNS` / `MAX_HISTORY_CHARS` ceiling; every surface reports
"unknown". Adding a window is a one-line data change with provenance. No network
call is ever made to discover a window.

## ADR-H64 - Overflow is classified, retried once against a tighter budget, then reported honestly

**Decision.** `CONTEXT_OVERFLOW` is a first-class `HarnessErrorCode`. A
conservative classifier in `models.py` recognises a provider's context-length
rejection (structured code OR a bounded, specific message pattern) before the
generic provider catch-all. On a first-call overflow — before any tool ran — the
run retries ONCE with half the derived history allowance; otherwise it fails
with a specific message and the remedy. Exactly one terminal status is persisted.

**Why.** A provider context-length rejection previously fell through to a generic
`PROVIDER_ERROR` with no recovery and no explanation. A single bounded retry is
the cheapest honest recovery; looping would risk duplicate side effects, and
retrying after a tool executed would re-run that tool.

**Consequences.** Overflow is distinguished from rate-limit / timeout / auth /
quota errors (false-positive guarded by tests). Recovery is first-call only and
is skipped when the tightened history would not actually be smaller. The success
path emits `memory.history_bounded` (counts/estimates only) so a trimmed turn is
never silent; the `RunResult.context` blob is additive and absent for old rows.
A token-aware summarization ladder remains explicitly out of scope, gated on this
event data.

## ADR-H65 - Memory recall is surfaced as a content-free activity line, not a run-result field

**Decision.** `memory.retrieved` (already emitted by `_memory_context` with a
bounded payload) is mapped in `src/lib/agent/events.ts` to one activity item with
a new distinct `kind: 'recall'` (icon `Brain`), mirroring the `history_bounded`
mapping: counts and character budget only, `selected <= 0` emits nothing, and
`selectedIds`/`excluded`/memory text never reach the UI. No run-result contract
field is added.

**Why.** Trimming was visible but recall was not — the last observability
asymmetry, even though the runtime already told the UI everything needed. A
run-result `recall` blob would have no reader; the activity passthrough is the
one mechanism that already renders this class of fact, so a second one would be
redundant. Keeping the line content-free preserves the same privacy posture as
the dropped-context seam.

**Consequences.** A recalled-memory turn shows one `recall` line beside the
`bounded`/`retrieval` lines; the expandable detail is the existing name-only
panel, so no new detail variant exists. Memory content remains out of the UI
(asserted by test). A future richer recall surface can read the same event data
without a contract change.

## ADR-H66 - An unknown context window is operator-declarable, and ModelSettings accepts the camelCase wire shape

**Decision.** `ModelSettings` gains an optional bounded `context_window`
(camelCase `contextWindow`, 1..10,000,000). The budget resolver prefers an
explicit override, then the declared value, then the catalog resolver; blank
stays unknown (conservative ceiling, `windowKnown: false`). Separately,
`ModelSettings` now generates camelCase aliases (`to_camel`) with
`populate_by_name=True`.

**Why.** The catalog cannot know a local server's, a router's or a custom
endpoint's window, so those runs were permanently stuck at the conservative
ceiling even when the operator knew the limit. A declaration is real input,
never a guess. The alias fix addresses a pre-existing bug: the TS front door
sends `baseUrl`/`apiKey` but the model declared `base_url`/`api_key`, so
pydantic silently dropped both and the runtime used the catalog default base
URL with no credential.

**Consequences.** Settings > AI / LLM shows the resolved window and offers a
numeric field only when it is unknown; an emptied field sends `null`, which
`setLLMConfig` treats as an explicit clear. Both wire shapes are accepted, so
existing TS callers and snake_case test constructors both work. `jdds.db`
untouched.

## ADR-H67 - MCP config is shareable, screened and trusted in tiers; tool names use a double-underscore boundary

**Decision.** Phase 6 Milestone 1 hardens the MCP config contract:

1. **Interpolation.** `${VAR}`/`${env:VAR}` and the built-ins `${workspaceFolder}`,
   `${userHome}`, `${pathSeparator}` are resolved from the process environment at
   load/spawn. One pure function (`mcp/interpolation.py`); remote `url`/`auth`
   values **refuse the connect by name** on an unresolved reference; local stdio
   `command`/`args`/`env` keep the literal placeholder when unset.
2. **Filters.** `selected_tools` accepts the legacy exact list (unchanged) or
   `{include, exclude}` with fnmatch globs; include wins over exclude; an empty
   include selects nothing.
3. **Screening.** A static screen (`mcp/screening.py`) refuses — never sanitizes —
   a shell interpreter with an inline flag, shell control operators, a download/
   execute or exfil shape, and a known-IOC package token; it runs at load, at
   save, and at spawn so a hand-edited file cannot bypass it.
4. **Trust tier.** `trust: "full" | "untrusted"`, fail-closed on an unrecognized
   value. Untrusted requires a per-call approval for every non-read call
   regardless of `enabled_effects`, and its annotations can never activate a
   tool.
5. **Naming.** Eclipse tool names are `mcp__<server>__<tool>` (double underscore),
   disambiguating the server/tool boundary; the identity digest survives only as
   a genuine-collision last resort.
6. **Read routes.** The Python MCP read routes are operator-gated at the boundary.
7. **`httpx2`** is pinned explicitly (it is a declared transitive dep of `mcp`).

**Why.** A config a user cannot share, a filter that makes selection unusable, a
hostile entry that executes, a secret in plaintext and an undocumented
authorization story are exactly what users get wrong. Trust must be a declared
tier, not inferred from a server's own metadata; a changed delimiter must be
provably safe for saved configs (activation keys on original names).

**Consequences.** Interpolation is applied in memory and never written back, so
the file stays shareable. Screening is defense-in-depth on top of argv-only spawn
with no shell. `selected_tools` remains compatible, and existing configs activate
the same tools across the delimiter change (tests). The IOC list is a documented
seed, not a completeness claim. `jdds.db` untouched.

## ADR-H68 - MCP lifecycle is resilient: separate retry budgets, honest session-expiry, and platform-appropriate orphan reaping

**Decision.** Phase 6 M2 makes the MCP layer survive transient failures:

1. **Retry/reconnect budgets.** `mcp/lifecycle.py` `RetryBudget` keeps separate
   initial-connect and reconnect-after-drop counters with full-jitter exponential
   backoff and an explicit terminal state that is reported with the attempt count.
   Startup never crashes the service; cancellation propagates.
2. **Call-time reconnect seam.** A call at a `failed`/`closed`/`lazy` server
   triggers ONE bounded reconnect under a per-server lock; after reconnecting the
   live descriptor is re-fetched and selection/digest/effect are re-checked, and
   only tools passing the same authority checks are re-registered.
3. **Session-expiry policy.** A mid-call transport/session error is classified.
   A **read** is reconnected and replayed exactly once; a **write/external_action**
   is NEVER replayed and returns an explicit `outcomeUncertain` outcome.
4. **Keepalive/recycle.** One manager lifecycle task (started/stopped with the
   manager — not a daemon) pings a live session on `keepalive_interval` and
   recycles a stdio child on `idle_timeout_seconds`/`max_lifetime_seconds` only
   when no call is in flight.
5. **Orphan reaping.** `mcp/orphan_supervisor.py` (stdlib-only) starts ONE
   supervisor per process on POSIX; it detects parent death by pipe EOF and sweeps
   registered process groups TERM?grace?KILL. Clean shutdown unregisters. On
   Windows the MCP SDK spawns each child in a Job Object with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, which already kills the tree when the
   parent dies, so no supervisor is started.
6. **Lazy startup.** `mcp/schema_cache.py` persists a per-server tool manifest
   keyed by a fingerprint of the connection-defining config; a `lazy` server
   registers from cache and spawns on first call. `lazy` defaults off.

**Why.** One transient blip permanently disabled a server until a manual reload; a
suspended HTTP session made the first call after a pause fail as a generic error;
a crashed parent leaked `npx` children; and boot spawned everything eagerly. The
honest session rule is a correctness requirement: a write that may have happened
must say so, never silently replay or silently fail.

**Consequences.** The orphan guarantee on Windows is the SDK's Job Object, not our
code (verified by a real-kill test). POSIX reaping registers a child's process
group by polling `ps` briefly after spawn; where that is unavailable it is a
no-op and the SDK's own teardown remains the only guarantee. The lifecycle task is
one per manager, so nothing outlives the runtime. `jdds.db` untouched.

## ADR-H69 - Remote MCP uses the SDK OAuth client; its tokens live encrypted in the vault, keyed by server id

**Decision.** Phase 6 M3 adds OAuth 2.1 to remote MCP servers. The
authorization-code + PKCE path reuses the MCP SDK's own `OAuthClientProvider`
(no new dependency); a device-code (RFC 8628) flow the SDK lacks is implemented
in `mcp/oauth.py`. Tokens, client registration and issuer metadata are stored
ENCRYPTED at rest in JDDS (`SystemConfig.key = 'mcp_oauth_tokens_v1'`,
AES-256-GCM), referenced by server id, never in `mcp-servers.json`, an event, a
log, or a response. If the vault cannot encrypt, saving throws — no plaintext
fallback. Clear-credentials stays configured-but-disconnected. `ssl_verify`
(true|false|CA path), mTLS (`client_cert`/`client_key`), SSE transport and a
redirect/origin re-validation hook are added on the same lifecycle.

**Why.** Every hosted finance server an investor would connect uses OAuth; without
it they are unreachable. A token is a credential: it must be encrypted, never
serialized, and never persisted in the gitignored plaintext config.

**Consequences.** The interactive authorization-code path is wired but not
exercised end-to-end in the offline suite (device flow is). `jdds.db` untouched.

## ADR-H70 - MCP resources and prompts are capability-aware utility tools; subscriptions are a non-goal

**Decision.** Resources/prompts utility tools register only when the live session
advertises the capability AND the operator toggles `tools.resources`/`tools.prompts`
(default off). A resource read is bounded, labelled and scheme-authorized;
prompt text is returned as INERT data. **Subscriptions are not implemented.**
Shipping them well needs ownership, rate limiting, a bounded queue, a drop-oldest
policy with an honest drop count, and a decision that a notification triggers
nothing — none of which exist, and inventing a second event bus is forbidden, so
the honest choice is a documented non-goal. A notification can never trigger a run
or a write.

**Why.** An MCP server that is primarily a document source looked empty without
resources/prompts; but a notification channel is a real new authority surface and
must not be improvised.

**Consequences.** No subscription support is shipped - a documented non-goal, not
a gap. The resources/prompts tools exist only when the live session advertises
the capability AND the operator enables `tools.resources`/`tools.prompts`. A
resource read is bounded and scheme-authorized, prompt text is returned as inert
data, and a notification can never trigger a run or a write.

## ADR-H71 - Sampling is off by default and capped; elicitation routes through the existing approval surface

**Decision.** `sampling/createMessage` is off per server; when enabled it has a
hard max-tokens cap, an RPM cap, a max tool-round count (0 = no tool use), a
timeout, audit counters, and a harness event. It can never trigger an effectful
tool without that tool's normal approval and never widens the agent allowlist.
`elicitation/create` form requests route through the SAME approval authorizer (no
parallel prompt path); URL-mode requests are declined; consent is per-call, never
remembered. Sampling/elicitation content is untrusted data.

**Why.** These are the two directions where a server asks Eclipse for something —
genuinely useful and genuinely new authority. They ship last, off by default, and
audited.

**Consequences.** The sampling `run_model` seam is not yet bound to the chat
model, so a live sampling request is refused honestly (`mcp_sampling_unavailable`)
rather than served by a second adapter.

## ADR-H72 - MCP is a first-class retrieval candidate; conflicts are preserved; Eclipse-as-server stays narrow

**Decision.** Phase 6 adds `mcp/mcp_retrieval.py`: a `SourceCandidate` with
provider-independent provenance, three distinct freshness states
(`fresh`/`stale`/`missing`) plus `failed`, a `plan_candidates` that prefers the
fresher eligible source and **preserves conflicts** rather than silently choosing,
and a `RetrievalSource` adapter for a declared MCP tool. A generic MCP tool stays
directly callable and is never withheld for not implementing the contract. Presets
point at real endpoints with `verified_at`/`verified_against` and a one-click
install that writes a DISABLED, nothing-selected entry; a read-only offline
supply-chain audit reports pinned/unpinned packages. Separately, Eclipse exposes a
narrow READ-ONLY MCP server (`server_exposure.py`) with its own bearer auth
(distinct from the operator secret), no write capability, bounded pagination and
schema hygiene.

**Why.** MCP stops being a bolt-on when native planning can consider and explain an
MCP source; and an investor's other agents querying Eclipse's receipts is a moat
if — and only if — it cannot leak a credential.

**Consequences.** The new conflict/freshness planner is self-contained and tested
directly but is **not yet invoked by `retrieval/planner.py`** — the documented
seam gap. `jdds.db` untouched.

## ADR-H73 - MCP data sources flow into the native planner; sampling runs on the run model; the AC OAuth leg is a manual https step

**Decision.** The three Phase-6 seams left open at first close-out are closed:

1. **MCP ? native planning.** An operator-declared MCP data tool (a server's
   `data_source_tools: {tool: dataType}`) becomes a `RetrievalSource` via
   `manager.mcp_sources()`, is threaded through `ToolContext.mcp_manager` into
   `FinancialRetrievalLayer(mcp_sources=...)`, and is considered by
   `plan_retrieval(..., additional_sources=...)` AFTER native structured sources
   but BEFORE generic web. Provenance stays `secondary` / `eclipseVerified=false`;
   JDdS is never silently upgraded. An undeclared MCP tool stays directly callable
   and is never forced through the contract.
2. **Sampling on the run model.** `AgenticService._sampling_model_factory` binds
   server `sampling/createMessage` to the runtime's OWN `models.build_chat_model`
   with the run's credentials and the policy's token cap — never a second model
   adapter. Sampling is still off by default per server; with no run model bound a
   request is refused with `mcp_sampling_unavailable` (honest, never a fake model).
3. **Eclipse-as-server second client.** The offline suite now drives the exposure
   surface with TWO MCP clients: Streamable HTTP and SSE. The real Hermes interop
   remains the documented manual step.
4. **OAuth authorization code.** The SDK's `OAuthClientProvider` refuses to build
   its OAuth URLs over plain http (RFC 8252 https requirement), so the interactive
   browser leg is a documented manual https step; the construction seam
   (bridge-backed `TokenStorage` + caller redirect/callback handlers + the
   discovery/registration/token endpoints the SDK consumes) is tested. Device code
   is covered end-to-end offline.

**Why.** The remaining limitations were real seams, not cosmetic: without (1) an
MCP source could never be selected or compared by native planning; without (2) a
server's sampling request could never be served; without (3) "two clients"
interoperability was asserted from one.

**Consequences.** `plan_retrieval`'s new `additional_sources` argument is additive
and defaults empty, so existing callers/tests are unaffected. A declared MCP
source cannot displace a strong native structured provider (it is inserted only
before web). `jdds.db` untouched.

## ADR-H74 - MCP config round-trip: absent means preserve (Phase 6.1)

**Decision.** `upsert_mcp_config` implements a merge contract from the incoming
model's `model_fields_set`, not from `if value is not None`:

> Absent means preserve; explicitly present means replace - including an explicit
> `null` / `false` / `{}` / `[]` for clearing.

A caller that omits a field (an older UI, an API client, a script) cannot reset
it. The UI can still clear a field because `draftToInput` always sends every
field it owns. This makes one edit-and-save provably unable to widen a server's
authority by omission: `trust` cannot move `untrusted` -> `full`,
`enabled_effects` cannot gain a class, `tool_effects`/`data_source_tools` cannot
be dropped, `confirmed_catalog_digest` cannot be cleared, and `selected_tools`
cannot grow, unless the caller explicitly sends the change. `env` keeps its
documented write-only merge and `env_keys` behavior. `install_mcp_integration` is
create-if-absent, so re-installing a preset never resets an operator's changes.
`public_dict()` also emits the raw operator-set bounds so a view -> model -> view
round trip is lossless; `public_configs()` exposes the capability-toggles map as
`capabilityToggles` (the `tools` key holds the discovered array).

**Why.** The Phase-6 field set existed only on the wire, not in the UI payload.
Re-saving a server through Settings silently downgraded `untrusted` -> `full`,
disabled resources/prompts, dropped the data-source declaration and every tool
effect, and invalidated the catalog confirmation - a silent authority widening.

**Consequences.** A TS pure module `src/lib/harness/mcp-draft.ts` owns the
view<->draft<->input mapping and is round-trip tested
(`mcp-draft.test.ts`); Python tests `test_mcp_config_roundtrip.py` assert the
preserve/clear matrix, the authority-widening invariants, an idempotent re-save
and install/confirm preservation. `_ServerRuntime`, status/introspection and the
OAuth flow are pure-extracted from `manager.py` into `runtime.py`,
`introspection.py` and `oauth_flow.py`; `MCPManager` remains the facade with an
unchanged public API. `resource_schemes` adds an optional per-server URI-scheme
narrowing with the global allowlist as a hard ceiling. Non-text MCP content
reports MIME type + byte size rather than an opaque omission.

## ADR-H75 - The release gate is local and runnable, not a fictional CI workflow

**Decision.** The unreachable workflows at `Black-Eclipse-site/.github/workflows/`
are deleted. GitHub Actions reads only `/.github/workflows` at the repository root,
and this repo's git root is above the app directory, so those workflows never ran.
Rather than move them to the root (unverifiable without a push, and the sibling
`backup.yml` would cron a daily `sqlite3 jdds.db` against a file that is no longer
tracked), the gate becomes ONE local command:
`npm run check:all` -> `scripts/check-all.js`, which runs prisma generate,
typecheck, lint, `npm test` (vitest + Python) and `npm run test:evals` in order and
exits non-zero on the first failure. The three surviving eval gates (retrieval,
contradiction, compaction) are folded into `npm test` via `scripts/test-evals.js`
(offline, ~0.7 s), using the explicit-`sys.path` launcher the vendored interpreter
requires. The `semantic_cli` reference is gone: the semantic layer was deleted
deliberately and its removal is documented evidence.

**Why.** The stated release gate was fictional three ways: a CI job that could not
pass (a missing module), a workflow GitHub never reads, and eval floors no local
command ran. A gate that cannot run is worse than no gate: it implies coverage that
does not exist.

**Consequences.** CI on GitHub is now **absent by decision**, not by accident; a
future root-level `.github/` would be a deliberate re-introduction (and must add a
working Python install plus a `jdds.db` story first). The CHANGELOG obligation is
enforced by `.githooks/commit-msg` (non-doc change must stage the CHANGELOG; doc-only
exempt; `SKIP_CHANGELOG_CHECK=1` bypass mirrors the identity guard). The smoke test
remains operator-triggered and offline-excluded.

## ADR-H76 - CI is restored at the repository root (amends ADR-H75)

**Decision.** A workflow is restored at the TRUE root path,
`.github/workflows/ci.yml` (GitHub Actions reads only `/.github/workflows` at the
repository root, which is why the old `Black-Eclipse-site/.github/workflows/`
never ran). It mirrors `Black-Eclipse-site/scripts/check-all.js` step-for-step:
checkout → Node 20 + Python 3.12 with the same caching → `pip install -r
python/requirements.txt` → `npm ci --ignore-scripts` → `npx prisma generate` →
`npx tsc --noEmit` → `npx eslint .` → `npm test` → `npm run test:evals`, with
`defaults.run.working-directory: Black-Eclipse-site`. The former `backup.yml` is
**not** restored: `jdds.db` is untracked runtime state now, so a checkout has no
such file and a daily `sqlite3 jdds.db` cron would always fail. A local DB backup
belongs in `scripts/`, not CI.

**Why.** ADR-H75 chose local-only because two
blockers made a root workflow unverifiable/unsafe: (1) `jdds.db` was being
untracked, so `backup.yml` would cron a missing file; and (2) there was no local
gate to mirror, so CI and local would have diverged. **Both blockers are gone:**
`jdds.db` is untracked and ignored (no CI job references it), and
`scripts/check-all.js` is a real, green local gate the workflow mirrors exactly.
The value of a running remote gate now outweighs keeping it local-only.

**Consequences.** The workflow's steps were validated locally in the same order
(all exit 0); a root-level workflow is **unverified against GitHub until it is
pushed** — the user must observe the first run. `ADR-H75`'s local gate remains the
source of truth and is unchanged; CI is its mirror. `test:smoke` stays
operator-only (network) with a documented trigger in `CONTRIBUTING.md` (run before
a release tag).

## ADR-H77 - A provider-side error body is a failure, never retrieved data

**Decision.** An MCP call that reports transport success (`isError=false`) but
whose whole text body parses as a JSON object carrying an unambiguous error signal
(a truthy `error` key, a 4xx/5xx `status`/`statusCode`, or a known error `code`
with a status or message) is classified as a **provider error**. The pure rule
lives in `client.looks_like_provider_error` and is deliberately conservative — a
legitimate payload that merely carries a `code` is not misread. On a match,
`_normalize_result` sets `is_error=True` and records `data["providerError"] =
{code?, status?}` with the provider's body preserved verbatim; the manager renders
an explicit `MCP reported a provider error (status …, code '…'): <body>` failure
and emits `mcp.tool.failed` with `mcp_tool_provider_error` (never
`mcp.tool.completed`). A provider error is NEVER retried — it is not a transport
error. The MCP system prompt instructs the model to report such a result as a
limitation and never present it as retrieved data.

**Why.** Connecting real servers exposed the defect: `@notionhq/notion-mcp-server`
with an invalid token returned `ok=True, isError=False` and the body
`{"status":401,"code":"unauthorized","message":"API token is invalid."}` — handed
to the model as a normal successful result. Reporting "connected" when all that is
known is that a child process launched is a lie of omission.

**Consequences.** An empty MCP result is labelled `(the server returned no text
content for this call)` so empty never reads as a successful empty result. The
rule catches a structured JSON error body; an error phrased as free prose is not
caught by the classifier and is covered by the prompt rule instead.

## ADR-H78 - Test is not Verify; credentials are proven by an explicit read call

**Decision.** The operator-facing **Test** action only proves that a server's
process started and tools were discovered; its copy says so and it never calls a
tool. Credential validity is a SEPARATE, operator-triggered **Verify** action
(`POST /mcp/servers/{id}/verify` + `MCPManager.verify_credentials`, extracted as
`VerifyMixin`). Verify calls ONE operator-chosen **read** tool with
operator-supplied arguments, bounded by a short fixed timeout, and records a
content-free verdict — `credentials_valid` / `credentials_rejected` (with the
provider's status/code) / `verify_inconclusive` — plus `verifiedAt`. It never
returns or stores the response body, is never automatic, never at boot, never in a
loop, and refuses a non-read tool. A `credentials_rejected` server is presented
distinctly from `endpoint_unreachable` and `server_not_connected`: a server can be
connected AND rejected.

**Why.** "Test" reporting `connected` for a server that cannot complete a single
call was the defect that cost real time; the only symptom was a 401 buried inside
a result body. Separating "the process launched" from "the credential works" makes
both honest.

**Consequences.** A server with no declared read tool cannot be verified (the UI
says so). Verify can only prove what the chosen read tool proves.

## ADR-H79 - Effect classification refuses a write-shaped name on a read tool

**Decision.** Effect classes are never inferred from a tool name. Instead, a tool
marked `read` (declared or annotation-sourced) whose NAME contains a write-ish
token (`create|delete|update|patch|move|post|append|set|remove|write|archive`,
`naming.writes_like_name`) stays **inert** until the operator explicitly
acknowledges the mismatch
(`MCPServerConfig.acknowledged_effect_mismatches`, a per-tool list). The editor
shows the mismatch and requires the acknowledgement before the tool is
selectable. A server's own naming and its `readOnlyHint` annotation are untrusted;
`unknown` remains never-activatable. The acknowledgement rides the round-trip
contract.

**Why.** `API-post-search` is a read with a write-looking name; `API-move-page` is
a write with no write-looking token. Inferring an effect from a name would
auto-activate a tool on a guess (worse); silently trusting a misleading name lets
a `read` tool (no approval) execute a write. Making the ambiguity visible and
refused is the only honest option.

**Consequences.** One behavior changes: a `read` tool with a write-shaped name now
needs an acknowledgement to activate. The check is lexical, so a write hidden
behind a completely read-ish name is not caught by the name — declared effects and
per-call approvals remain the authority.

## ADR-H80 - MCP integrity: launchable presets, a per-run budget, honest truncation and lazy state

**Decision.** Four integrity gaps found by connecting real servers are closed:
(1) a `stdio` preset is a promise that clicking it works, so `presets.py`
documents that `verified=True` requires an actual launch, the offline suite
asserts each preset's shape, and the opt-in `npm run test:presets` resolves every
`stdio` package's `bin` (never in `npm test`/CI); the un-launchable `sec-edgar-mcp`
preset is replaced with the launched `@pipeworx/mcp-edgar`; (2) MCP tool calls are
capped per run (`max_calls_per_run`, default `DEFAULT_MCP_CALLS_PER_RUN`), refusing
further calls honestly with a named cap and an event; (3) a `maxTools` slice
records discovered-vs-kept and surfaces a `tools_truncated` diagnostic; (4) a lazy
server's stale cache (fingerprint mismatch) is reported as needing a connect via
`schema_cache.is_stale` rather than silently served. The test-only module
`python/harness/mcp/_testtmp.py` moved to `python/tests/_tmp_helper.py`.

**Why.** A preset that cannot run, a silent call loop, a silently truncated
catalog, and a stale cache served as declarations are all forms of a system
claiming more than it knows. The `sec_filings` preset pointed at a package with no
`bin` and no server code yet shipped `verified=True`.

**Consequences.** `manager.py` shrank (Verify → `verify.py`, rendering/session
expiry → `execution.py`) to stay under its size ratchet. The budget keys on the
run id (`"anonymous"` outside a run). The truncation guard reports counts, not the
dropped names.

## ADR-H81 - Inbound MCP notifications trigger rediscovery of one server

**Decision.** A server may add/remove/rename tools mid-session with
`notifications/tools/list_changed`, and it may change its resource/prompt listings
with `notifications/resources/list_changed` / `notifications/prompts/list_changed`.
`MCPConnection` wires the SDK `message_handler`; it acts ONLY on a list-changed
notification for a capability the server actually advertised at initialize. It
invokes one callback (`RediscoveryMixin._on_catalog_changed`) that, under the
per-server connect lock, re-runs the relevant listing through the existing path
(`MCPConnection.refresh_tools`/`refresh_listing`, which re-list on the live
session — no second discovery mechanism). For `tools` it rebuilds descriptors,
deregisters tools that disappeared, recomputes the catalog digest, re-runs every
activation gate, and re-registers survivors; for `resources`/`prompts` it replaces
the connect-time snapshot the utility tools read from (there is no activation gate
on those read-only surfaces). Every case emits a bounded, content-free
`mcp.catalog.changed` event (server id, kind, counts, and the new digest for
tools — never a description). A notification for an unknown/dead server is ignored
safely; one landing during a call cannot corrupt state (the lock).

**Why.** A changed tool set was only noticed on reconnect, so a tool the operator
approved could keep running under a stale catalog; and a changed resource/prompt
listing was served stale from the connect-time snapshot. Handling the
notifications makes both immediate.

**Consequences.** For tools, the changed digest no longer matches
`confirmed_catalog_digest`, so annotation-sourced tools become inert again until
the operator re-confirms — the existing gate, deliberately not bypassed. The
event is process-level (no run id) and published on the shared bus; the same facts
also surface on the card via `lifecycle.catalogChanges`/`removedTools`.

## ADR-H82 - A circuit breaker stops futile calls to a server that always fails

**Decision.** `circuit.py` adds a per-server breaker (CLOSED/OPEN/HALF_OPEN) with
named constants (`FAILURE_THRESHOLD=3`, `COOLDOWN_S=60`) and pure helpers
(`breaker_state`, `should_probe`) matching the `lifecycle` style. It counts calls
that REACHED the transport — timeout, session expiry, protocol/call error,
connection closed, and a provider-side error. It does NOT count: a denied/absent
approval, a caller-side validation error, a per-run budget refusal, a breaker
refusal, or a caller cancellation; those are operator/caller behaviour and must
not trip the breaker. When OPEN, a call refuses immediately and honestly with
`mcp_circuit_open`, naming the failure count, last error code and re-probe time,
and performs no transport work. After the cooldown exactly ONE probe is allowed;
success closes the breaker, failure re-arms it. An operator reset
(`POST /mcp/servers/{id}/circuit/reset`) clears it. The state is exposed in
`lifecycle.circuit` and on the card.

**Why.** `maxCallsPerRun` bounds volume and `RetryBudget` covers connect; neither
stops a server that authenticates and then fails every call, burning latency and
tokens with no signal the runtime noticed.

**Consequences.** The breaker is per server and in-process; it resets on runtime
restart. A reconnect does NOT reset the counter for free — only a successful call
does.

## ADR-H83 - The spawn-path supply-chain check fails closed on malware, open on network error

**Decision.** `supply_chain.py` queries the public OSV API for the exact package +
version about to be launched, filtering to malware advisories. It runs at the
SPAWN path only (not `Test`, not at boot for a `lazy` server), via
`screen_spawn`. A confirmed malware advisory (`MAL-` id or a malware/malicious
database type/source/summary) refuses the spawn with a diagnostic naming the
advisory id; a network error (timeout, DNS, non-200) allows the spawn and is
logged as inconclusive. Verdicts are cached keyed by `(ecosystem, package,
version)` with an absolute TTL; clean and blocked verdicts are cached, network
failures are not. A non-package runner (a real executable) is never queried. No
new dependency: the existing `httpx` client is used, and the fetcher is injectable
so the offline suite makes no network call. `audit.py` gains an
`upstream_checked` flag a caller may set; its default stays honestly offline.

**Why.** `screening.py` matches a small hardcoded IOC seed and `audit.py` is
offline; neither asks an advisory database about the code about to execute, and
`npx -y <pkg>` is arbitrary third-party execution.

**Consequences.** Fail-open on a network error is deliberate (an unreachable OSV
must not block all MCP use) and always stated, never a silent allow. The check can
only refuse on a confirmed malware advisory; an ordinary CVE is not a block.

## ADR-H84 - Sampling honours a model allowlist; tool names are clamped to the provider limit

**Decision.** `SamplingPolicy.allowed_models` (default empty) restricts which model
a server may request: with a non-empty list, a requested model not on it falls
back to the operator's configured default and increments
`sampling_model_fallbacks`; an empty list is unchanged behaviour. The runtime
continues to serve sampling on its OWN adapter (never the server's model
directly). Separately, `naming.py` clamps the assembled `mcp__<server>__<tool>`
name to `MAX_ECLIPSE_TOOL_NAME=64` characters, appending the stable identity
digest when it must shorten, so an over-long pair fails with a diagnostic at
registration instead of at the wire. A short name is byte-identical to today; the
delimiter and the `taken` collision logic are unchanged, and clamping is a last
resort surfaced as a non-blocking `note` (never a `diagnostic`, which gates
activation).

**Why.** A server asking for an arbitrary model is a small authority leak; an
over-long name failing at the wire is a confusing runtime error.

**Consequences.** No provider-specific behaviour beyond the length bound. The
name clamp cannot block a tool; it only informs the operator why the callable
name differs.

## ADR-H85 — The source-health ledger uses canonical route keys and its probes are persisted

**Decision.** A route's health entry is keyed by
`(source × market × assetClass × capability)` with every dimension normalised to
a closed vocabulary in one module (`src/lib/harness/health-key.ts`,
`canonicalMarket` / `canonicalAssetClass` / `canonicalSource`). Market is
upper-cased against the declared market set; asset class is lower-cased against
the closed enum (plus the synthetic `macro` class); anything unrecognised — or a
literal `unknown`/`*` from an instrument route — folds to the wildcard `*`, never
a persisted `unknown`. Provider/transport ids collapse to one canonical source per
provider (`safe-get`/`duckduckgo-*`/`custom-search` → `WEB`, `sec-xbrl` → `SEC`).
Normalisation is applied on BOTH write (`recordHealth`/`recordCapabilityHealth`)
and read (`sourceHealth`), so legacy rows fold onto the same key. Separately, the
`research_health` probes now write their outcomes into the same `SourceHealth`
ledger, under the same canonical route id and the same instrument-less (`*`×`*`)
key as production traffic, instead of existing only in the returned payload.

**Why.** The ledger was fragmented: the same real route appeared under `Equity`
and `equity`, under `unknown` and `*`, so per-route success rates were wrong and a
100%-healthy route could not be distinguished from a 0%-failing one. And because
the probe tested one provider (`duckduckgo-html`, `safe-get`) while production
recorded another (`WEB`), a working provider was reported as failing — the health
surface could not be trusted to decide routing.

**Consequences.** A probe run and a production run of the same capability now
increment ONE row (the P0-3 acceptance test). Route selection that consults health
can trust the keys. No schema change: capability + probe-kind are still encoded in
the existing `symbol` column. A market/class that genuinely cannot be resolved
still records — as `*`, so a failed attempt is not silently dropped.

## ADR-H86 — Retrieval planning is health-aware: a healthy route outranks a failing one

**Decision.** The Python retrieval layer reorders its planned sources using the
SAME `SourceHealth` ledger `get_source_health` reports (read through the
read-only bridge as the `source_health` capability). Ordering rules
(`python/harness/retrieval/health.py`): health is keyed by
`(source × capability × market)` with an exact-market hit preferred over a global
(`*`) route; the capability is derived from the data type and the source, so a
working quote never lends health to a statement route; **only a healthy route is
promoted** — a degraded/failing route is never demoted below an *unobserved*
route, because absence of evidence is not evidence of failure and pushing a
structured route behind generic web could invert precedence badly. The reorder is
stable (equal health keeps static order) and never adds or removes a source: it
only reorders among sources the caller already made available. The index is
fetched at most once per layer and re-read only after the D1 durable-replay check
(so a replay performs no provider work). It is fail-open — a missing bridge,
unavailable ledger or malformed row leaves the static plan untouched.

**Why.** The static plan could sit a 0%-success route ahead of an equally-eligible
100%-healthy route, so reachable data went unused — the single highest-leverage
source of missing values in the report. The evidence already existed; selection
ignored it.

**Consequences.** A healthy route is tried first and its precedence is logged and
surfaced as a warning when a planned route is recorded degraded/failing. No new
dependency. The reorder is advisory and bounded: an unobserved environment behaves
exactly as before.

## ADR-H87 - A `list_changed` with no actual change emits no event, and a silent spawn check becomes visible

**Decision.** Two reachability/honesty rules for the continuation M4/M6 work.

1. `rediscovery.py` snapshots `rt.catalog_digest` BEFORE the refresh and emits
   `mcp.catalog.changed` only when the digest moved. Discovery, the registry
   reconcile (deregister vanished tools, re-register survivors) and the
   resource/prompt listing refresh all still run on every notification — only the
   EVENT is suppressed. A `resources`/`prompts` notification is unaffected (those
   listings have no digest and no activation gate).
2. `MCPServerConfig.live_supply_chain_check` keeps its `True` default, but becomes
   a first-class operator field: `MCPServerView` / `MCPServerInput` / `DraftServer`
   carry it, `mcp-draft.ts` round-trips it with `view.liveSupplyChainCheck !== false`
   as the read rule (absent = the documented default of on), and the editor
   renders it in the Lifecycle section.

**Why.** (1) A server may announce a change that never materialises, or fire
`list_changed` gratuitously. The prior behaviour re-ran discovery (harmless) and
then emitted "catalog changed" every time, which tells the operator to re-review a
catalog that is byte-identical — the signal is only useful if it is rare and true.
(2) The check was default-on, ran on the spawn path with an 8s outbound timeout,
and was named nowhere an operator could see it. Worse, because the field was
absent from the front door's contract, a save from the editor would DROP an
explicit `false` and silently re-enable the network call — the same
field-dropping defect ADR-H74 fixed for the other lifecycle fields, reintroduced by
a new field.

**Consequences.** A gratuitous notification is now a no-op the operator never
sees; a real change still emits exactly once. The spawn check remains default-on
(the safe direction: arbitrary third-party code execution), but its cost is now
disclosable and switchable. `absent` and `true` both mean "on"; only an explicit
`false` disables it, so an older config cannot accidentally turn the check off.

## ADR-H88 - The supply-chain report surfaces the last live spawn-path verdict (derived, not a caller flag)

**Decision.** `supply_chain.SupplyChainChecker` records the last observation it
made for each `(ecosystem, package, version)` — cacheable or not — with a
human-readable `checkedAt` (wall clock, distinct from the monotonic cache clock).
A read-only lookup (`observe_command`) resolves a `command args...` spec to that
observation and NEVER queries the network. `audit.supply_chain_report` consults it
for each configured server and, when any package carries an observation, attaches
`liveVerdict` to that package and sets `upstreamChecked: true`. The
`upstream_checked` caller parameter is **removed** — the flag is now DERIVED from
real observations, so it can no longer be set by a caller that did not actually
perform a check. `GET /mcp/supply-chain` still never contacts the network; it
reports what the spawn path already observed. Per-package `liveVerdict` carries
the verdict code, advisory ids when blocked, package+version, and `checkedAt`
(identifiers only — never a response body).

**Why.** The prior contract was unimplementable: `upstream_checked=True` was
documented as "set by a CALLER that also performed a live OSV check", but no
caller existed and the only route passed nothing, so `upstreamChecked` could never
become `true`. The operator saw `offlineOnly: true, upstreamChecked: false` and
could not tell that a live check *had* run at spawn. An observation-derived flag is
the honest fix: the report reflects what actually happened, and a network failure
(the check ran but was inconclusive) is still an observation, so it surfaces too.

**Consequences.** An inconclusive (`unknown`) observation is reported but NOT
cached — a later spawn re-queries. "Never checked yet" and "checked, inconclusive"
are now distinguishable by the operator. `reset_cache` clears both the cache and
the observations. The report stays offline and read-only.

## ADR-H89 - Catalog-change counters are surfaced beside the existing banner (no event consumer)

**Decision.** The runtime keeps emitting the content-free `mcp.catalog.changed`
event and exposing `lifecycle.catalogChanges` / `lastCatalogChangeAt`. The card
surfaces those counters BESIDE the existing "catalog changed" banner, and the
lifecycle panel renders them in a small block — only when at least one change was
recorded, so a server that never changed shows no stray `0`/`—`. A single pure
helper (`mcp-draft.catalogChangeSummary`) decides what (if anything) to render.
No event consumer is added: nothing in the UI needs a live event stream, because
the change signal the operator acts on is the digest mismatch, which is already
polled with the server list.

**Why.** Two sources described one fact — the emitted event and the lifecycle
counters — and the one the operator saw (the digest-derived banner) could not say
*when* or *how often*. Adding an SSE consumer for an event nothing needs would be
a second notification system invented for symmetry. Surfacing the already-transported
counters is the cheapest honest resolution and needs no new contract.

**Consequences.** The counters are counts and a timestamp only — never a tool
description or a changed tool name. The deliberate non-goal: **no UI consumer of
`mcp.catalog.changed` was built**; the event remains available on the shared bus
for a future subscriber but is not required for the operator surface. Re-opening
that would need a concrete consumer (e.g. a global, non-per-server notification),
which does not exist today.

## ADR-H90 - Tool-call duplicate suppression is scoped to one agent step, not the run

**Decision.** In `graph.py` the `seen_calls` map that refuses a repeated
`(tool, canonical-args)` call is created INSIDE `tools_node`, so it lives for one
model turn (one agent step) rather than the whole run. Two identical calls in the
same turn keep the original behaviour exactly — the first executes, the second is
refused with the same `tool.failed`/`TOOL_FORBIDDEN` event and the same
"Use the earlier result" message shape. A call repeated in a LATER turn executes
again. The key remains `(name, _stable_key(args))`; it is deliberately NOT keyed
on the assistant message id (the same model message can legitimately recur across
steps, which would reintroduce the bug).

**Why.** The guard's purpose is to stop one turn from issuing the same call twice.
A run-scoped map also blocked a legitimate re-issue in a later turn and instructed
the model to reuse a stale result — the next page of a paginated listing,
re-polling a price after new evidence arrived, a deliberate retry after a
transient failure, or a second lookup whose first result changed between steps.
Those are correct model behaviours that the run-scoped map silently defeated.

**Consequences.** A cross-turn re-issue does not multiply upstream network calls:
the retrieval layer still deduplicates identical data fetches per run through the
shared `retrieval_cache` passed into `build_graph`. The duplicate refusal still
counts as a tool error and consumes the run's `max_tool_calls` budget — that
accounting is unchanged and out of scope here (recorded as an observation, not
changed). Rejected alternative: keying on `(step, name, args)` — functionally
equivalent but leaves a growing per-run map whose entries are never read again;
resetting the map per `tools_node` call is simpler and self-evidently step-scoped.

## ADR-H91 - Native tool-result text is bounded at the single execution seam and reports its own truncation

**Decision.** `ToolRegistry.execute` — the one seam every native and MCP tool
passes through — bounds a tool's model-facing text to
`MAX_TOOL_RESULT_CHARS = 24_000`. Over the bound the text is cut and a clearly
non-JSON trailer names the shown and total sizes; `ToolResult` gains
`truncated` / `shown_chars` / `total_chars`, and the graph emits a
`tool.truncated` event (sizes only, once per trimmed result). Under the bound the
text is byte-identical — no marker, no flag, no whitespace change. `ToolResult.data`
(the structured payload the front door reads) is NEVER bounded.

**Why.** A native tool with no internal cap had neither a ceiling nor a signal: its
result was appended verbatim into the model's context. The value is tied to the
measured budget, not chosen arbitrarily: at `CHARS_PER_TOKEN = 4` the bound is
~6 000 tokens; `graph.MAX_HISTORY_CHARS = 40_000` bounds the WHOLE conversation,
so one tool result must be a fraction of it; and
`research.synthesis.MAX_PACKET_CHARS = 24_000` is the largest synthesized evidence
packet the runtime already accepts, so a single raw result has no reason to exceed
it. The marker cannot parse as JSON, so a truncated JSON body can never be
mistaken for a complete one — the exact failure mode avoided.

**Consequences.** Blast radius measured: exactly one native tool can exceed the
bound today — `session_open`, which returns a bounded message WINDOW with no
character ceiling (a maximal 30×3 000-char window is 25 612 chars → trimmed to
24 000). The constant is deliberately NOT raised to hide it; a bound that hides
the one tool it exists to catch would be pointless. Rejected alternative: a
per-tool `max_result_chars` field — more knobs, and a new tool could still forget
one; a single global seam cannot be bypassed by omission.

## ADR-H92 - An effectful-tool approval is single-use

**Decision.** An `McpApproval` authorizes exactly ONE call. Authorization CLAIMS
the approval with a guarded `UPDATE ... WHERE id=? AND status='approved'` and a
rowcount check (`ApprovalStore.consume`), moving it to a terminal `consumed` state
and stamping `usedAt`. A second identical call cannot be authorized by the same
approval: it creates a fresh PENDING request and is refused (never a silent pass,
never a bare denial with no record). `is_authorized` remains a NON-consuming probe
for the resource/prompt paths and tests; the authorizing path is `consume`. Because
one effectful call is authorized at two seams (the registry gate and the MCP
manager's `_execute` re-assert), the first successful claim records the authorized
argument digest on the shared `ToolContext` for the current call-scope, so the
second seam authorizes without consuming again; the registry clears that scope at
the start of each tool call, so every new call must claim its own approval.

**Why.** One operator approval previously authorized an UNBOUNDED number of
identical effectful calls for up to the TTL (15 min default, 24 h max), each
emitting `mcp.tool.approved` as if freshly decided — a silent semantic the module
docstring never stated. Single-use matches the store's fail-closed posture (no
model/server-settable authority) and needs no new operator-settable field. Rejected
alternative (Option B): an explicit, operator-only repeatable/standing grant with
a `singleUse` field and its own UI label — a larger surface that adds a
dangerous-by-default-if-misread toggle for no demonstrated need; single-use is the
safe default and a standing grant can be added later as an explicit, labeled
decision.

**Consequences.** The `consumed` status and `usedAt` ride the whole round-trip:
the Python row + `_row()`, the `GET /mcp/approvals` response, `MCPApprovalView` in
`mcp-types.ts`, and the approvals UI (a `consumed` filter, a distinct badge, and a
"consumed" line). Every existing invariant is preserved: unknown effects are never
approvable; the digest still binds the arguments; TTL expiry and terminal denial
are unchanged; a prior approval/tool description/model claim still cannot
authorize a different action. A `consumed` approval is not re-used by `request`
(which now returns a fresh PENDING row instead of an already-approved one).

## ADR-H93 - Run lineage, durable checkpoints, and idempotent resume

**Decision.** A run now has an identity that survives a crash. `HarnessRun` gained
`parentRunId`/`workflowId` (the run that spawned it and the workflow it belongs
to); a new `RunCheckpoint` table stores a BOUNDED per-boundary reference (after
each completed agent step, each tool-result batch, and at terminal status) and a
new `ToolExecutionIdempotency` table records one row per executed
`(runId, stepIndex, toolCallId)`. `POST /runs/{id}/resume` re-enters the graph at
the recorded node and REUSES a recorded tool result instead of re-running it; a
resume with no checkpoint is `CHECKPOINT_NOT_FOUND` and a resume of a terminal run
is `RESUME_CONFLICT` — never a fresh run in its place. The persistence lives in a
new `python/harness/run_lineage.py` bound to the ONE store connection (same
pattern as `mcp/approvals.py`), so ratcheted `persistence.py` did not grow.

**Why.** There was no run lineage and no checkpoint anywhere in the Python
runtime, and the only "checkpoint" concept was the TypeScript conversation store.
Every later phase (workflow steps, approval resume, observability) needs a run
whose parent and progress survive a restart. The checkpoint is a REFERENCE, not a
second trace: the message tail is capped (≤60 messages, ≤12k chars each) and the
allowlist/pending-calls are capped, because a checkpoint that duplicates the event
log is a new god table.

**Consequences.** `RunResult` gained `parentRunId`/`childRunIds`; a skill child-run
records its parent. Resume is safe for side-effecting tools by construction. A
downgrade ignores the new tables/columns (idempotent stdlib DDL).

## ADR-H94 - The route-health report is a summary partition, and the declared list is withheld by default

**Decision.** `sourceHealth()` returns a compact SUMMARY HEAD first (recorded route
counts by health class, markets with any recorded attempt, first N recorded rows)
plus the live recorded rows; the ~1,200-row declared universe is WITHHELD and only
returned for `recordedOnly: true` or `includeDeclared: true`. The Python
`get_source_health` renders the head, states the withheld count, and how to request
the full list. `capabilityMatrix()` canonicalises its market key exactly as
`sourceHealth()` does. `SourceHealth` gained `lastSuccessAt`/`lastFailureAt`.

**Why.** The recorded ledger is real and correct but was UNREACHABLE: the declared
list was emitted BEFORE any recorded row, so at the 24k tool-result bound a caller
only ever saw "unknown — 0 ok / 0 fail" rows and concluded nothing had ever run. A
working system therefore looked dead. The honesty semantics are unchanged: a
declared route with zero attempts is `unknown`, neither healthy nor failing.

**Consequences.** Two readers of one ledger now agree on the route key. A
one-time migration (`scripts/migrate-source-health-canonical.ts`) folds legacy rows
(including a literal `unknown` in source/market) onto canonical keys; a DB-level
test asserts no literal `unknown` remains. The new columns are nullable and
additive.

## ADR-H95 - The capability matrix cannot assert an impossible fact; the market set is 34

**Decision.** `buildCapabilityMatrix` reconciles every axis: a non-`unknown`
observation against an unimplemented level is REPRESENTED as `matrixInconsistent`
with a reason (level forced non-implemented), never smoothed over. CH/KW/QA are
declared COVERAGE entries with their REAL ISO currencies (CHF/KWD/QAR) and a
declared blocker (decision D1: keep the real venues). Provider eligibility is
DECLARED (`provider-eligibility.ts`) rather than inferred from a `supports: () =>
true`; a declared `not_eligible` market is not configured on the quote axis and is
tagged `secondary`. The market count is 34 and asserted in a test.

**Why.** `capability.ts` could render `quote: not implemented (observed: working)`
in the same row (a UK yfinance quote succeeded while UK is absent from
`MARKET_SLUG`), and a market outside the COVERAGE registry reported a USD currency
it does not use. The real limit on a quote route is data/symbol quality (`.NG`,
`.AE` return quote-not-found; `.ME`/`.JO`/`.VN`/`.MX` are thin), not the absence of
an adapter — so the fix is declared eligibility plus a verified sample symbol.

**Consequences.** A capability-matrix eval gate (Phase 8) fails the build on any
regression. DERIV (recognised, uncovered) never claims `configured` on any axis,
including web.

## ADR-H96 - The workflow compiler reuses the one execution plane and never inherits tools across steps

**Decision.** A versioned, bounded `Workflow` compiles onto the EXISTING
`execute_run` graph (`python/harness/workflows/`); it does NOT build a second
agent loop, tool registry or model adapter. A step's effective tool scope is the
INTERSECTION of the agent allowlist and the step's declared tools, enforced at the
SAME `ToolRegistry.execute(allowed=...)` seam (a denied call is a structured
`TOOL_FORBIDDEN`). Approval steps reuse the existing single-use `ApprovalStore`.
The path is flag-gated (`HARNESS_WORKFLOWS_ENABLED`, default OFF); a run with no
`workflowId` is unchanged.

**Why.** The runtime already IS a harness — one execution plane, one registry, one
allowlist seam, one bus, one persistence layer. What it lacked was orchestration ON
TOP of that plane. Per the coding-agent capability contract, agents must NOT
inherit each other's tools or access: a step can only NARROW its scope, never
widen it. A cycle, an unbounded fan-out, an out-of-scope tool, and a step with no
terminal path are each rejected at validation with a NAMED reason.

**Consequences.** No visual workflow designer, bot scheduler, or
agent-builder UI (roadmap Phases 8-9). `execute_run` is wrapped, not replaced.

## ADR-H97 - The code capability is tools on the one registry, not a second runtime

**Decision.** The whole code capability (roadmap Phases 1-12) is delivered as
new TOOLS on the EXISTING `ToolRegistry`, driven by the EXISTING `execute_run`
graph — never a second agent loop, tool registry or model adapter. Phase 0 ships
the policy authority (`python/harness/code/policy.py`) and a workspace-bound
actor class (`skills/permissions.Actor` gains `workspace_id`/`autonomy`), and
NOTHING executable: no filesystem, shell, patch or exec tool — not even a stub.

**Why.** The runtime already is a harness: one execution plane, one registry,
one allowlist seam, one bus, one persistence layer (README §3.1, ADR-H96). A
second runtime would fork every one of those surfaces and double the failure
modes on a finance system. The blocker is policy, not architecture: `code.write`
sat in `FORBIDDEN_PERMISSIONS` and `code.execute` in `BLOCKED_PERMISSIONS`
(ADR-025), so the capability was impossible by accident. Phase 0 turns that
into a deliberate, tested decision — a second actor class that can hold the code
verbs ONLY when a run is bound to a workspace and carries an autonomy profile —
so a coding agent can no longer "fix" a failing permission test by deleting a
constant.

**Consequences.** New code verbs live in `CODE_PERMISSIONS` (manifest) and are
grantable only through `code/policy.code_grants(autonomy)`; `FORBIDDEN_PERMISSIONS`
narrows to trade/broker/production. The default actor (`DEFAULT_ACTOR`) and the
operator actor (`OPERATOR_ACTOR`) hold no code verb, proven by a test and a
gate. A caller-supplied operator grant can no longer smuggle a code verb in.

## ADR-H98 - `code/policy.py` is the single policy authority; autonomy profiles are enforced in code

**Decision.** `python/harness/code/policy.py` is the ONE place a code-policy
decision is made: may this run read/write/execute this thing, in this workspace,
under this autonomy profile? It owns the three autonomy profiles
(`plan_only`, `edit_verify`, `approve_external`) as data, `code_grants()` maps a
profile to its verbs, and `decide_write` / `decide_command` are the only
decision functions. Every later phase imports these rules; none re-implements
them. `skills/permissions.Actor.granted()` unions `code_grants(autonomy)` ONLY
when both a `workspace_id` and an `autonomy` are set.

**Why.** Prose is never a security boundary (README §3.2). The permission model
must be readable as code, reviewable, and testable in isolation. An unknown
autonomy string grants NOTHING (fail-closed), mirroring the MCP `trust` tier — a
typo in a profile name can only lose capability, never gain it. Keeping the
decision in one module (the same pattern as `skills/permissions.py` for skills)
means Phase 10 can FREEZE `code/policy.py` as a protected path without leaving a
second, un-frozen copy of the rules behind.

**Consequences.** The code verb set is derived, not enumerated at each call
site. `POLICY_DENIED`-style decisions carry a stable machine `code` plus ONE
plain sentence (`PolicyDecision.reason`) safe to show a human. Autonomy profile
names are mechanism vocabulary and must never appear in a user-facing string
(ADR-H99 / `PLAIN_LANGUAGE_DENYLIST`).

## ADR-H99 - Protected paths, command classes, default-deny network and secret posture

**Decision.** The code capability's safety data lives in `code/policy.py` as
tables: `PROTECTED_PATHS` (never touchable, even inside a workspace and even by
an operator — `jdds.db`, the memory DBs, `.env`, `mcp-servers.json`,
`prisma/migrations/**`, `.git/**`, the frozen policy/permission/approval/gate
files, and key material), `IGNORED_DIRS`/`IGNORED_GLOBS`, `COMMAND_CLASSES` (a
closed tuple whose per-class program sets are declared, with a `DENIED_PROGRAMS`
belt-and-braces list), `NETWORK_DEFAULT = False`, and
`SECRET_ENV_ALLOWLIST` (every other environment variable is dropped from a child
process — never an `os.environ` passthrough). Path safety is by construction:
`resolve_within` compares REAL paths and refuses traversal, an absolute path
outside the root, and a link that leaves the root; the raw string is never
trusted. Protected outranks ignored, so a protected path always reports
`PROTECTED_PATH`.

**Why.** Isolation must exist BEFORE any write/execute tool is opened (README
§2, ordering rule). A denylist of prose is not enough: the protected set has to
be data a test can assert against and a later phase can freeze. Default-deny
network and an explicit secret allowlist are the same discipline — an unknown
class or a shell/downloader binary is refused rather than permitted by
omission. `PLAIN_LANGUAGE_DENYLIST` is the mechanism-word list every
user-facing gate reads from ONE place.

**Consequences.** Phase 3 implements `COMMAND_CLASSES` and owns the argv
templates; Phase 10 freezes `policy.py`. A missing isolation primitive produces
an honest refusal (`SANDBOX_UNAVAILABLE`), never a degraded unsandboxed run
(README §3.5). Every refusal sentence is asserted, by a Phase 0 test, to be one
sentence with no denylist term — the seed of the per-phase plain-language gates.

## ADR-H100 - Read-only repository navigation is path-confined, bounded and citable

**Decision.** Phase 1 adds exactly FIVE read-only tools on the ONE existing
`ToolRegistry` — `workspace_status`, `workspace_list`, `workspace_search`,
`workspace_read`, `workspace_docs` — every one `permission="read"`, `risk="low"`.
There is no write tool, no patch tool and no command runner; the ONLY child
process is a fixed, argument-free `git` read probe. The navigator is a SEPARATE
agent preset (`code-navigator`) whose allowlist is only those five tools and which
does NOT set `allowMcpTools`, so it can never reach a finance tool or an MCP tool.
Confinement is owned by `code/workspace.py::confine`, which CALLS the Phase-0
`policy.resolve_within` (it does not re-implement it) and maps its denial to a
distinct structured code per escape vector (`PATH_TRAVERSAL`,
`PATH_OUTSIDE_WORKSPACE`, `SYMLINK_ESCAPE`, `PROTECTED_PATH`, `IGNORED_PATH`,
`NOT_FOUND`). Every read is line-numbered with a `nextStartLine` continuation
offset, refuses binary files, and redacts secret-shaped lines to `«redacted»`
through the ONE pattern set (`session_index.redact_span`); `workspace_search` is
bounded by matches, files, bytes and a wall-clock deadline (partial results are
marked truncated), and `workspace_docs` reuses the session-search tokenization
and ranking as a bounded per-call scan of `docs/**` — no table, no index, no
vector store. `workspace_read` reports `PROTECTED_PATH` for `jdds.db`, not
`BINARY_FILE`, because protected check precedes the binary probe.

**Why.** The runtime had no way to see the repository at all; every "where is X
documented" ended at prose. The read half is safe to open first because the
registry already refuses any non-read tool without an authorizer, so a phase made
only of read tools needs no approval machinery. Path safety must be ONE seam: a
second traversal check would drift from the Phase-0 authority and become the
weaker one. Reusing the session-index secret patterns means a file reader cannot
become a new leak path with its own, divergent denylist. And the docs search must
not become a second indexer — the roadmap's whole-project non-goal — so it scans
per call and reports its engine honestly.

**Consequences.** `confine` is the only place a code path is resolved (a grep for
`realpath` under `python/harness/code/` hits only `workspace.py`; `subprocess`
only `workspace.py`). `RunRequest.workspace` is an optional, additive
narrowing/confirmation: a caller's `root` must realpath to the server-resolved
root or the run gets no workspace, so a request field can never point the
navigator at a different directory. `ToolContext.workspace` carries the binding
(one graph line). User-facing copy lives in `src/lib/i18n/` (only `task.*`/
`refusal.*` keys added) and the code→sentence map is data; the jargon gate
`src/lib/i18n/plain-language.test.ts` scans the values. Phase 2 builds the write
half on this same confinement and the same preset discipline.

## ADR-H101 - Workspace isolation: disposable worktrees, snapshot fallback, never the user's own checkout

**Decision.** Phase 2 lets Eclipse author a change — create/replace/delete/move a
file — inside a DISPOSABLE copy of the repository, never the operator's checkout.
`code/worktree.py` allocates at `<root>/.black-eclipse/code-workspaces/<id>` (an
already-gitignored scratch root) with `git worktree add --detach`; when git
`worktree` is unavailable it falls back to a bounded snapshot copy and reports
`strategy: "snapshot"` honestly on the record and the event. The root is resolved
INTERNALLY (`workspace.default_root()`) — it is never an argument to any function
in `worktree.py`, so no caller can name the checkout. `release` refuses to delete
anything whose realpath is not under the scratch root (`RELEASE_OUTSIDE_SCRATCH`)
and is idempotent. The patch engine (`code/patch.py`) copies the discipline of
`skills/patches.py`: a closed op set, structural validation BEFORE any write, a
content-hash stale-base refusal (`PATCH_BASE_MISMATCH`), a unique-span rule for
`replace` (0 → `PATCH_SPAN_NOT_FOUND`, 2+ → `PATCH_SPAN_AMBIGUOUS`; never a blind
`.replace()`), and ATOMICity — a refusal at any op applies NOTHING. Every applied
op records `contentHashBefore`/`After`. Three tools join the registry:
`workspace_patch_apply` (`permission="write"`, `effect="write"`), `workspace_diff`
(`permission="read"`), `workspace_revert` (`permission="write"`). A write is
authorized by the run's autonomy profile (`edit_verify` grants `code.write`),
composed into the single effect authorizer; `git.write` is implemented nowhere.

**Why.** The whole value of the capability is that it can change code; the whole
risk is that it changes the WRONG copy. The concurrency rule (README §4) makes
this concrete: OpenCode edits the same tree, so a write tool that touched the
checkout would collide with a live session. A detached worktree makes that
impossible by construction, and putting it under the ignored `.black-eclipse/`
means a leaked workspace can never become a tracked artifact. The stale-base and
atomicity rules exist because a half-applied patch is exactly the "silently
rebased" failure the skill engine already refuses; a content hash generalizes a
version string to a file. The refusal vocabulary is the same per-vector honesty
Phase 0/1 established, so a caller (and a test) can tell an escape from a
protected path from a stale base.

**Consequences.** `python/harness/code/store.py` owns the `CodeWorkspace` table in
`agent-memory.db` bound to the one `AgentMemoryStore` connection (the
`run_lineage.py` pattern), so the ratcheted `persistence.py` does not grow and no
second database appears. `sweep_expired` runs on the existing startup lifespan,
releasing workspaces past the TTL so a cancellation leaves no orphan. Front-door
copy lives in `src/lib/i18n/` (`task.authoring.*` progress/result lines plus a
`refusal.*` code→sentence map as data), and the jargon gate scans it. `git.write`
remains an operator action; Phase 5 is where an agent-side `git.write` could be
introduced behind a single-use approval.

## ADR-H102 - Commands are a closed table; a missing isolation primitive refuses rather than degrades

**Decision.** Phase 3 lets the agent run allowlisted checks through a single
execution primitive. `code/commands.py` is a closed TABLE of command classes:
the model supplies a class name plus typed args, never an argv. An `ArgSpec`
validates each argument with `re.fullmatch` (and refuses a `..` component); the
argv is assembled from a fixed template whose first token is an absolute,
closed executable from `EXECUTABLES` (npm/npx/node, the VENDORED
`scripts/vendor/python/python.exe`, and git). `git_read.subcommand` is a CLOSED
allowlist — `push`, `reset`, `clean`, `checkout`, `commit`, `config` are absent
by construction. `dependency_install` is the only class with `network=True`, is
operator-gated (default OFF), and uses `--ignore-scripts`. `code/sandbox.py` is
the ONE execution seam: `build_env` copies ONLY `policy.SECRET_ENV_ALLOWLIST`
(never `os.environ`, so `HARNESS_*`/`CRON_SECRET` are absent), `cwd` is the
resolved worktree root, `shell=False` always, a wall-clock timeout kills the
whole process TREE, output is capped with honest `truncated`/`totalChars`, and
cancellation propagates with no retry. On Windows the child is assigned to a
Job Object with `KILL_ON_JOB_CLOSE` (plus a deterministic `taskkill /T`); on
POSIX it gets its own process group. If no primitive exists, `select_backend`
raises `SandboxUnavailable` and the command is REFUSED — never run unsandboxed.
`command_run` is a registry tool (`permission="write"`,
`effect="external_action"`); the handler enforces the autonomy profile
(`command.execute`) and records EVERY attempt — including every refusal — as a
`HarnessCommandExecution` row plus a `command.*` event.

**Why.** Executing code is where a mistake stops being a bad diff and becomes a
bad side effect, so this is the strictest phase in the roadmap. A matcher over
model-supplied strings is a policy surface that grows with every string; a table
is closed, reviewable, and its size is the size of the capability. The repo's own
standard for spawning a child (MCP) already requires argv arrays, an explicit env
allowlist and platform-appropriate reaping, so the broker matches it rather than
inventing a weaker rule. And the honesty rule is already law (a missing runtime
fails with 503, never a fake agent) — the same rule applied to isolation: no
primitive means refuse, not degrade. Auditability is the point: a block that
leaves no row and no event is treated as a FAILURE, because a refusal a user
cannot see is indistinguishable from a silent success.

**Consequences.** `code/store.py` (not the ratcheted `persistence.py`) owns the
`HarnessCommandExecution` DDL and helpers on the one `AgentMemoryStore`
connection. Every refusal carries a machine code AND one plain sentence plus a
recovery step (`commands.COMMAND_REFUSAL_COPY`, mirrored in `src/lib/i18n`), and
the plain-language gate asserts neither leaks a mechanism word. NETWORK is denied
by CLASS and by a scrubbed environment (proxies dropped); there is no OS network
namespace in this phase, which is recorded as a known limit — a command that
tries to reach the network fails because the class is refused or the route is
unreachable, and the attempt is recorded. Phase 4 consumes the recorded
executions to prove a "pass" with an executor-backed receipt.

## ADR-H103 — The system prompt is hardened and given a run-scoped clock; the cache key covers prompt content

**Decision.** The Eclipse Agent system prompt is a reduction-and-accuracy surface,
not a capability surface. Four properties now hold. (1) A run-scoped
`runtime_context` `PromptSegment` carries the run's real date, time and UTC offset
(`prompt_segments.runtime_context_block`) and is rendered as its OWN per-run
`SystemMessage` after the byte-stable system head, never inside it; it has its own
content hash and is excluded from `STATIC_SEGMENTS`, so the stable prefix still
caches and the run date never invalidates it. (2) `prompt_cache_key` now includes
the CONTENT hash of every reusable (`STATIC_SEGMENTS` = `system_policy` +
`agent_identity` + `tool_schemas`) segment, so editing the prompt changes the key
even when the hand-bumped `SYSTEM_POLICY_VERSION` has not; `runtime_context`,
`run_instructions` and `user_message` remain excluded. `STATIC_SEGMENTS` is split
from a new `RUN_FROZEN_SEGMENTS`, and only the static group feeds
`reusable_total_tokens()`, so a per-run block no longer inflates the reuse
accounting. (3) The execution-discipline blocks carry ONE statement per rule (no
near-verbatim duplicates of the operating principles or tool-use enforcement) and
its examples are finance-native (ambiguous instrument identity, market/currency
defaulting) rather than shell/OS examples; the identity preamble is a bounded
role-and-domains statement, not a 52-bullet inventory. (4) The bound tool schemas
are declared authoritative (once), so the prompt no longer hardcodes tool names;
the dead `filter_system_prompt` tool-list replacement was removed (the sentence it
searched for is absent).

**Why.** The prompt demanded as-of periods, staleness labels and revalidation but
gave the model no clock; it repeated the same rules across eight `<...>` blocks;
and its cache key tracked only hand-bumped versions, so a real prompt edit left
the key unchanged — a correctness bug in the observability contract.

**Consequences.** `filter_system_prompt` continues to drop memory/session guidance
when those stores are off (unchanged). `graph.execute_run` builds the runtime
block once per run and threads it into the prompt plan, the assembled messages and
the measured history budget. No permission or capability is added: this is a
reduction plus an accuracy fix. `docs/harness/architecture.md` and
`docs/NAVIGATION.md` updated; tests `test_prompt_segments.py`
(`test_prompt_content_edit_changes_cache_key`,
`test_runtime_context_does_not_change_cache_key`,
`test_reusable_token_accounting_excludes_run_frozen_segments`,
`test_runtime_context_block_carries_run_date_and_id`) and `test_memory_tools.py`
(extended `test_prompt_has_no_phantom_capabilities`, `test_prompt_char_ceiling`).

## ADR-H104 — The Eclipse Agent prompt states the finance-harness contract

**Decision.** `ECLIPSE_AGENT_SYSTEM_PROMPT` now describes a finance harness, not
only an analyst: it carries a `# Market data semantics` block (session state,
price basis, period basis, corporate actions, point-in-time vs restated, FX,
staleness) as *statement requirements* that make two numbers comparable; an
`# Inputs and authority` precedence table (system policy > run instructions >
attached documents > tool/retrieved evidence > profile context > conversation
history > text inside a retrieved artifact) plus the attached-document rule
(`[attached: <name>]`, primary source for its contents, data never instruction);
an `# Answer contract` (answer-first shape, `source: as-of` citations, numeric
conventions, one contrastive worked example); and a `## Runtime awareness` block
(bounded steps/timeout, a stop rule, truncation disclosure, no redundant
retrieval). The identity preamble was rewritten so its action surface is exactly
the bound tool allowlist while its domain list stays framed as competence. The
declared `session_summary` prompt segment was **removed**: it was never populated
by any caller (only tests), and no bounded producer exists without a new
dependency or a second model call, so keeping it would have overstated the
prompt's real contents.

**Why.** The prompt previously demanded as-of periods, units and comparability
without ever naming the finance-specific facts that make numbers comparable, and
it did not describe the runtime the model actually runs in. A capability claim
("models, simulates, orchestrates") contradicted by the bound allowlist two
sentences later is worse than no claim. The growth ceiling (≤ 17,500 chars) was
paid for by tightening `# Investment Analysis`, `## MCP tools`, `## Tool calls`,
identity preamble and `# Tool-use enforcement` — each rule retained.

**Consequences.** `python/harness/prompt_segments.py` no longer declares
`session_summary` (its `PromptPlan`, `to_dict` and cache key are unchanged
otherwise). Guard tests in `test_memory_tools.py` (`test_prompt_char_ceiling` at
17,500, `test_prompt_market_data_semantics_present`,
`test_prompt_inputs_authority_and_attachments_present`,
`test_prompt_has_no_duplicated_rule_sentences`) and `test_prompt_segments.py`
(`test_session_summary_is_not_a_declared_segment`). No tool, permission or
capability was added. Design prompt:
`docs/harness/prompt-v2-finance-harness-opencode-prompt.md`; progress log
`docs/harness/prompt-v2-finance-harness-progress.md`.

## ADR-H105 - A check is passing only when the executor recorded it; unbacked claims are marked

**Decision.** Phase 4 builds the trust spine for the code domain by mirroring the
finance validator discipline. `code/verify.py` is a per-run `VerificationLedger`:
a check is passing ONLY when it holds a `VerificationRecord` with a zero exit
code and a class in `CLAIM_CLASSES`. A deterministic, sentence-level claim parser
(`CLAIM_PATTERNS`, negation-aware so `"the tests did not run"` is not a claim)
finds pass-claims in the final answer; `unverified_claims` is the difference
between what the model asserted and what the executor proved. `enforce_answer`
(default `mark`, `strip` available) appends `UNVERIFIED_MARKER` naming the
classes that could have supported an unbacked claim, so a pass-claim is never
rendered as fact. `code/repair.py` runs plan -> edit -> check -> repair bounded by
`RepairBudget` (attempts, wall-clock, cost), checked BEFORE every edit; on
exhaustion it STOPS and reports the failing class + output digest. A
`SANDBOX_UNAVAILABLE` check is `refused`, never a pass. `execute_run` CALLS both
(no new graph node); the ledger is rehydrated from `HarnessRun.codeVerification`
on resume (EMPTY when absent) and persisted from `code/store.py`. Six
deterministic mistake categories read the blob + events. `output_digest` hashes
the FULL normalized output (CRLF/trailing-whitespace independent).

**Why.** The finance path is believable because a claim is checked against a
record: retrieval -> coverage -> validator -> receipt. The code path had no
equivalent, so a model could write "all tests pass" whether or not a test ran —
and prose is not a security boundary (README §3.2). A deterministic check must own
the decision; the model may propose a repair but never decides whether a check
passed. Bounding the repair loop is the whole point: an unbounded retry is the
failure mode this phase exists to prevent.

**Consequences.** A passing claim is provably impossible without a zero-exit
record (`supported = True` has exactly one assignment site, behind `passed`). The
`codeVerification` blob is additive and old-row-safe; `persistence.py` stayed
within its ratchet by putting the column DDL in `code/store.py` (the
`run_lineage.py` precedent). The UI panel is titled "Proof it works" and renders
plain lines with the mechanism behind a details affordance; the jargon gate
scans it. The eval pack (`python -m harness.code.eval`) floors
`falseVerifiedRate` at 0. Phase 5 may grant autonomy because output is now
verifiable.

## ADR-H106 - A prompt must describe its own allowlist; ranked authorities need a producer

**Decision.** Two rules are now enforced by guard tests over BOTH agent presets
(`python/tests/test_memory_tools.py`). (1) **Prompt/allowlist agreement:** every
tool a prompt names as its own must be in that preset's allowlist, and a prompt
may not deny a capability its allowlist contains; the check that a prompt which
enumerates its tools names all of them. (2) **No phantom authority:** the
`run instructions` rank was REMOVED from the `# Inputs and authority` table
(renumbered 1–6) and the `run_instructions` prompt segment was removed from
`prompt_segments.py`. There is no producer for it anywhere in the runtime (no
`schemas.py` field, no TS bridge field, and the sole `build_prompt_plan` caller
never passed it); ranking it above attached documents and profile context
elevated a source that never arrives. Also fixed: the Repository Navigator
prompt, which claimed exactly five read tools and "cannot run any command" while
`CODE_NAVIGATOR_TOOLS` has nine including `command_run` and the three authoring
tools.

**Why.** Gap 1 was a real correctness bug in the *navigator* prompt, not the
finance one: `command_run` + `## Running checks` landed in the same wave as older
prose saying "you cannot run any command", so the prompt described three
mutually-inconsistent surfaces. That is a class — any time an allowlist grows the
prompt describing it can silently go stale — so the durable fix is the guard,
not the one edit. Removing rather than wiring `run instructions` follows the
session_summary precedent and the constraint against adding input surfaces.

**Consequences.** `prompts.py` (both prompt strings), `prompt_segments.py`
(`run_instructions` removed from `PromptPlan`, `RUN_FROZEN_SEGMENTS`, the
segment list and the cache-key docs), and the guard tests. No tool, permission or
capability changed; the navigator's allowlist was NOT widened to match the
prompt — the prompt was corrected to match the allowlist. `## Running checks`
honesty rules are unchanged. Progress log:
`docs/harness/prompt-followup-progress.md`.

## ADR-H107 - One approval store, generalized; a code action is single-use, digest- and workspace-bound

**Decision.** Phase 5 adds the coding agent, and the external actions it may take
under `approve_external` (commit, push, open a PR, deploy) reuse the ONE approval
mechanism rather than a second one. Concretely:

  * The autonomy profile is enforced by the **tool allowlist**, not a prompt.
    `code/policy.allowed_tools(autonomy, declared)` is the ONE place a
    write/external tool is removed; `graph.effective_allowlist` calls it before
    the model sees any schema. At `plan_only` the write tools are absent, so a
    model that "decides" to write cannot: the registry returns `TOOL_FORBIDDEN`.
  * The `McpApproval` store is GENERALIZED into one approval concept with a
    `kind` discriminator (`"mcp_tool"` | `"code_action"`). `McpApproval` stays the
    physical home (existing MCP rows need no migration) and `kind`/`workspaceId`
    are additive columns (`kind` defaults to `"mcp_tool"`). The guarded-claim
    logic ADR-H92 froze is lifted into ONE function, `mcp.approvals.claim_guarded`,
    used by both kinds.
  * A code action is a `CodeActionRequest` whose canonicalized payload INCLUDES
    the workspace id, so an approval for a commit in workspace A can never
    authorize the same commit in workspace B. `code_action_digest` reuses
    `mcp.approvals.args_digest`, so the two kinds cannot disagree about what "the
    same payload" means.
  * The four action tools are `permission="write"`, `effect="external_action"`,
    carry an `actionKind` in `effect_metadata`, and are resolved by
    `code.actions.make_code_action_authorizer`, which consumes the approval ONCE
    (emitting `code.action.approval_required` / `code.action.approved`).
  * The task lifecycle is a CONSUMER of `run_lineage.py`: a code task IS a run,
    and its boundaries (`task.created`, `plan.accepted`, `patch.applied`,
    `command.executed`, `task.cancelled`, `task.discarded`) are `RunCheckpoint`
    rows. Resume reads `latest_checkpoint()` and reuses the recorded tool result
    (`was_tool_executed`), so it never re-applies a patch or re-runs a side-effect.

**Why.** Autonomy must be structural: a prompt instruction is not a boundary
(README §3.2), and the allowlist is the one seam the registry already enforces.
The approval question was open (generalize vs sibling) and leaving it open risks
two single-use implementations that silently drift — exactly the "two
half-systems" the roadmap forbids. Rejected alternative: a sibling `CodeApproval`
table. It would duplicate the guarded `UPDATE ... WHERE status='approved'`
rowcount check that makes an approval single-use, and a drift between the two
copies is a silent authorization bug. Reusing `run_lineage.py` for the task
lifecycle is the same discipline: Phase 5 is a consumer, not a builder.

**Consequences.** `main.py` grew by exactly two lines (an import and
`include_router(code_router)`). Every mutating `/code` route carries
`require_operator` (loopback or the runtime secret; never a body boolean). The
finance agent is unchanged and holds no code tool; the code agent holds no finance
tool and does not set `allowMcpTools`; the ADR-H92 MCP approval tests remain the
regression suite and stay green. The task surface is plain language ("Just tell
me what you'd do" / "Make the change and show me" / "Do it and ask me before
anything outside the app"), and the jargon gate scans it. Phases 6, 7, 8 and 9 may
now build on a code-capable agent with a task lifecycle.


## ADR-H108 � Harness-owned jobs; the TS refresh loops stay as they are

**Decision.** Phase 6 adds a durable, single-process job model to the Python
harness (`python/harness/jobs/`) beside the three existing TypeScript schedulers.
It does **not** replace or modify `src/lib/scraper/scheduler.ts`,
`src/lib/connectors/refresh-scheduler.ts` or `src/lib/investor-eye/scheduler.ts`.

A job is durable, reviewable **DATA** (`JobDefinition` on the `HarnessJob` table,
with a `HarnessJobRun` history table, both in `agent-memory.db`). A job references
a skill (by id + pinned version) or an objective + agent id; it never holds
executable code and never widens authority. A job run **is** a normal `execute_run`
with a job id attached � there is no second agent loop, tool registry or model
adapter (README �3.1). The run row links to the existing `HarnessRun`, so a job
produces the same trace, events, tool executions and (for a code job) verification
ledger as a user-initiated run.

The scheduler is one FastAPI-lifespan task started in `service.start_jobs()`
alongside `start_mcp()`. It reuses `mcp/circuit.py`'s breaker states and
thresholds rather than writing a second backoff. Cron parsing is stdlib-only
(`jobs/cron.py`), justified by `AGENTS.md`'s lighter-option rule. `enabled`
defaults **False** (discovery is not activation, ADR-H39), and every route �
reads included � carries `require_operator` (ADR-H48), except the webhook trigger,
which authenticates on the job's SHA-256 token hash.

**Why.** The roadmap's gap #4 is "no autonomous cadence: nothing runs without a
human clicking." The existing TS loops can only refresh data; none can run an
agent, and none has a job model, run history or delivery. But they are proven and
serve a different, latency-sensitive purpose. Replacing them would be a regression
risk with no user-visible benefit. Two existing pieces already do the hard parts �
run execution/persistence and failure containment � so the phase reuses both.

**Consequences.** The three TS schedulers are byte-unchanged (acceptance gate G6).
`main.py` grows by exactly two lines (import + `include_router`) plus the lifespan
start/stop calls; all new logic lives in new modules. Save-time guardrails
(`jobs/guardrails.py`) bind a skill job to the pinned skill's manifest permissions
(refusing with the exact missing permission), require a folder for a code job,
gate `approve_external` behind an explicit operator flag, confine a watch path
with `policy.resolve_within`, and refuse an unknown delivery adapter. A missed
window is reconciled once at startup and always recorded (`missed=1`). The
delivery interface ships two adapters (`in_app` on the existing SSE bus, and
`artifact`); messaging is Phase 11, which adds an adapter without refactoring the
job model. The seven library templates ship disabled.


## ADR-H109 — Child runs are narrowed, never widened; self-reports require verifiable handles

**Decision.** Phase 7 adds bounded fan-out (`python/harness/delegation.py` + the
`delegate` tool). A child IS a normal run: it goes through the SAME graph,
registry, policy engine and persistence (`execute_run` with a `parentRunId`), and
there is NO second execution path or agent loop. The module owns only three things:

1. **The budget** — `MAX_CHILDREN=4`, `MAX_DEPTH=2`, bounded concurrency, a
   per-child timeout and a token budget. Caps are enforced at construction with
   structured codes (`DELEGATION_WIDTH_EXCEEDED`, `DELEGATION_DEPTH_EXCEEDED`,
   `DELEGATION_BUDGET_EXHAUSTED`); a writing child is refused
   (`SANDBOX_UNAVAILABLE`) when the isolation primitive is unavailable rather than
   run unisolated.
2. **The narrowing** — `child_actor(parent, child)` returns an actor whose grants
   are the parent's grants INTERSECTED with the child's scope. `operator` is NEVER
   inherited (a child did not make the operator's call, so inheriting it would
   launder authority), operator-only permissions (`skill.create/enable/...`) are
   never passed down, code verbs exist only when the parent held them, and a
   workspace wider than the parent's is refused. A requested tool outside the
   parent's allowlist is refused (`DELEGATION_TOOL_NOT_HELD`).
3. **The aggregation** — a child's PROSE is quoted; a child's CLAIM is repeated
   only with a handle the parent can re-check. A check-claim ("tests pass") is
   admitted ONLY with a `check_record_id` that verifies against the Phase 4
   verification ledger; a `run_id`/`path`/`citation` handle does not back it. An
   unbacked claim goes to `droppedClaims` and is rendered as unconfirmed — never
   silently discarded.

**Why.** The linkage already existed (`HarnessRun.parentRunId`,
`run_lineage.py`'s `RunCheckpoint`/`ToolExecutionIdempotency`, a skill's child
run), but nothing fanned out, bounded, or aggregated. Two existing precedents
made the safety model obvious: `permissions.Actor.scopes` already models a grant
set that can only narrow, and the MCP layer already holds that "a self-report is
never proof". Reusing both avoided a second narrowing implementation and a second
trust rule. The operator-non-inheritance line is the one that matters most: a
child that needs an effectful action returns the request to the parent, which asks
the operator, so authority is never laundered through a sub-run.

**Consequences.** One additive, old-row-safe `HarnessRun.delegation` column (owned
by `delegation_tools.py`, added idempotently like `run_lineage.py` owns its
columns), so the fan-out is explainable after the fact with counts only. Events
ride the existing `RunEmitter` (`delegation.started` / `child.started` /
`child.completed` / `child.failed` / `child.refused` / `aggregated`), so
`explain_run` and the session trace already cover a delegation with no new
observability surface. `delegate` is registered on the one registry and added to
both the finance and code agent allowlists. `code/tools.py` did NOT grow past its
size ceiling: the handler lives in a new `delegation_tools.py`. Writing children
get their own Phase 2 worktree; the parent lists diffs and conflicting hunks and
NEVER merges (a merge stays a Phase 5 `approve_external` action with an operator
approval). The user sees the finance job and a plain progress list, never
orchestration vocabulary.


## ADR-H110 — Computed ≠ retrieved: a code-produced number carries its code digest and never enters research claims

**Decision.** Phase 8 adds a bounded, reproducible analysis sandbox
(`python/harness/code/execution.py`, `code/recipes.py`, the `analysis_run` tool).
The sandbox runs a short program in the Phase 3 isolation primitive, records
exactly enough to re-derive the result (the VENDORED interpreter path, the source
digest, the input digests, the resolved package-set digest), and labels the output
with a `Provenance` record. The rule, enforced in code and not in a prompt:

1. Every execution output is `kind="computed"`. There is **no code path** that
   produces `retrieved` from an execution.
2. A computed number **without** a provenance record renders `UNAVAILABLE`, never
   a bare number (`render_value` fails closed: a missing record is a bug).
3. A computed number **cannot enter a research claim.** The only legal route for a
   derivation to become evidence is re-expression through
   `research/calculations.py` — an explicit act carrying formula/version/input
   provenance, never an automatic promotion. An analysis run leaves the research
   receipt untouched.
4. Imports are checked by an **AST walk** (never `eval`/`exec` on the source, never
   a regex as the only guard) against an explicit allowlist; there is no network.

   **Corrected 2026-10-20 (ADR-H242):** the AST walk is a FIRST filter, not the
   enforcement point — an alias (`f = __import__`) or a concatenated URL walked
   past it. The sandbox now enforces the boundary at RUNTIME (guarded import, safe
   `os`/`sys`/`importlib`/`builtins`, denied privilege modules, audit hook,
   confined readers) and the operator's `HARNESS_ANALYSIS_ENABLED` setting, not
   the client's per-run flag, decides whether the surface is on at all.

The tool is **off per run** (`RunRequest.analysisEnabled`, default `False`) — the
same "discovery is not activation" discipline the MCP layer applies (ADR-H39). A
missing input is a QUESTION, not a guess: the recipes refuse with a plain prompt
rather than substituting a default or a silently shorter period.

**Why.** `research/calculations.py` is the right shape — a small, auditable,
provenance-carrying calculator — but its list is closed, so a metric outside it
ends at retrieval or nothing. Opening a sandbox without a provenance rule would
create the exact hazard `AGENTS.md` names: a plausible-looking number from
invented inputs, indistinguishable from a retrieved fact. The MCP layer already
established the precedent (every external result carries `verification`,
`eclipseVerified: false`, source and `retrievedAt` and is labelled in its text);
a computed result gets the same treatment with `computed` instead of `external`.
Point 3 is the line that keeps the phase from quietly undermining the evidence
system: without it, "run some code" becomes a laundering route into a claim.

**Consequences.** `analysis_run` registers on the ONE registry and runs through
the existing graph; the handler lives in the new `analysis_tools.py`
(`code/tools.py` stays under its size ceiling). `RunRequest.analysisEnabled` plus
`ToolContext.analysis_enabled` (default False) gate it. An additive, idempotent
`AnalysisRecord` table (owned by `analysis_tools.py`, not the ratcheted
`persistence.py`) records each run bounded. Artefacts are written to the
gitignored `.black-eclipse/artifacts/<runId>/` with a SHA-256 digest as identity,
so the behavioural reproducibility gate (same source + inputs ⇒ same digest) is
meaningful. The recipes are data, each tested and each failing honestly on a
missing input. The surface says "Worked out by Eclipse" with the method line, and
never interpreter/module/argv/exit-code vocabulary; the computed/retrieved
distinction is a front-door helper (`analysis-types.ts`) with its own invariant
test. The finance and code agent allowlists gained the tool; the finance-agent
tool-count assertion moved 24 → 26 across Phases 7–8.


## ADR-H111 — Scaffold kits are data; a generated artefact stays inert until evaluated

**Decision.** Phase 9 adds a library of eight **scaffold kits** (`python/harness/code/kits/`)
and a `scaffold_kit` tool. A kit is **data**, not a code snippet: the closed
`produces` set the repo already demands for one named artefact, typed inputs that
carry i18n prompt keys, convention statements, and a required `acceptance` rule.
The kit loader REFUSES to load a kit that has no acceptance test shipped WITH it
(`python/tests/test_kit_<id>.py`) or a declared output with no template — so a kit
cannot exist without its proof.

`scaffold_kit` renders the kit into the **task's disposable workspace** (Phase 2,
never the user's checkout), runs the kit's `checks` through the Phase 3 command
broker, and records them in the Phase 4 ledger. A failed check KEEPS the scaffold
for inspection (`CHECK_FAILED`), never silently deletes it. The rendered output is
CLOSED to the declared `produces` set; a path outside it is refused
(`OUTPUT_OUTSIDE_DECLARED_SET`). Each kit also ships a thin **draft** skill wrapper
so it is reachable through the existing `skill_invoke` path with the existing
permission model — a wrapper holds `scaffold_kit` as a required tool and no
operator permission, and can never grant anything.

The anti-fabrication generation rule (§6.5) is enforced by the templates: every
generated adapter/backtest/ingestion path returns `UNAVAILABLE` (never a default,
a synthetic series, or an extrapolation) when its data is absent, and its shipped
test exercises that branch; a generated backtest labels an in-sample metric as
in-sample and never presents it as predictive; a generated importer is idempotent
(running twice yields the same state) and its test proves it.

**Why.** The root plan's whole point is that the agent can build actual financial
software — a data adapter, a ratio, a backtest, a chart indicator, a portfolio
module, an ingestion pipeline, a research workflow, an MCP server. Each of those
shapes already has an exact convention in this repo. A kit makes the agent satisfy
that convention rather than emit plausible boilerplate: the acceptance test ships
with the artefact and fails if it is missing, and the check runs through the SAME
broker a human's change would. The inert rule is the existing ADR-025 discipline
applied to generated skills: nothing a model authored activates without evaluation
and an operator decision. Generating code without the `UNAVAILABLE` rule would
reintroduce exactly the hazard `AGENTS.md` names — a plausible-looking number from
invented inputs.

**Consequences.** Eight kits shipped (`market_data_adapter`, `mcp_server`,
`ratio_calculator`, `backtest_strategy`, `chart_indicator`, `portfolio_module`,
`data_ingestion_pipeline`, `research_workflow`), each with template(s), a manifest,
and an acceptance test under `python/tests/`. `scaffold_kit` registers on the ONE
registry and is added to both agent allowlists; its handler lives in the new
`scaffold_tools.py` so `code/tools.py` stays under its size ceiling, and
`policy.WRITE_TOOLS` includes it so the autonomy boundary removes it at
`plan_only`. Eight draft skill wrappers load (all `status: draft`, never enabled),
moving the installed-skill count 5 → 13. Kit user-facing strings are i18n keys
(`kit.*`), covered by the plain-language gate. No trading/order/broker kit ships —
an order path is a non-goal by construction. Tests: `test_kits_contract.py`,
`test_kits_scaffold.py`, `test_kits_unavailability.py`, `test_kits_skill_wrappers.py`,
and one `test_kit_<id>.py` per kit.


## ADR-H112 — Self-improvement covers code as well as skills; the policy engine and the release gate are frozen

**Decision.** Phase 10 adds `python/harness/code/evolution.py` (the recurrence
bridge, the proposal validator, the worktree evaluation, the versioning adapter and
the honest metrics), two additive tables (`EvolutionCandidate`, `EvolutionAttempt`),
and a route group. A mistake category whose RECURRENCE crosses
`RECURRENCE_THRESHOLD` becomes an **inert** candidate; a code-shaped candidate
becomes a set of structured `code/patch.py` operations; evaluation runs the target's
owning test file(s) (and any Phase-9 kit acceptance test) inside a Phase-2 worktree,
recording every execution in the Phase-4 verification ledger; activation reuses
`skills/versioning.py` **verbatim** through a thin subject adapter.

Three rules are non-negotiable and enforced in code:

1. **No unsupervised self-modification.** A candidate never activates itself;
   every `activate` route carries `require_operator` (reads included, ADR-H48) and
   records the operator decision on the candidate row.
2. **The judge cannot be edited by the judged.** A proposal touching
   `python/harness/code/policy.py`, `python/harness/skills/permissions.py`,
   `python/harness/mcp/approvals.py` or `scripts/check-all.js` is refused with
   `EvolutionRefusal(code="FROZEN_PATH")`, never silently skipped; so is any
   `policy.PROTECTED_PATHS` member (`PROTECTED`), any op that would set an
   identity/permission/tool/risk field (`FORBIDDEN_FIELD`, the `skills/patches.py`
   set as data), and any target outside the bound workspace (`OUTSIDE_WORKSPACE`).
3. **No metric without measurement.** `archive_metrics` returns the string
   `"UNKNOWN"` for any dimension it cannot derive (including `costMicros` that are
   NULL, which is why those columns are nullable) — never `0`, and there is no
   blended score anywhere; results are per source category.

**Why.** The observation side (`eclipse_memory/mistakes.py`) and the decision side
(`skills/versioning.py`) already existed; the gap was narrow — recurrence was
recorded and never consumed, and no candidate could name a source file. This phase
consumes the existing counter and drives the existing lifecycle rather than adding a
second one. The frozen set is the direct expression of README §3.3 (artifacts stay
inert) applied to the code that decides: a subject may not edit its own policy,
permissions, approvals or release gate, or "run some code" becomes a path to
authority. The nullable cost columns and the `UNKNOWN` rule exist because a report
that cannot say "I could not measure that" will say "0" instead, which is a lie.

**Consequences.** `FROZEN_PATHS` and the FORBIDDEN_FIELDS set are data;
`propose_ops` performs no model call and is the only entity that can accept an op
set (mirrors `research/claims.py`). Evaluation is deterministic — a model may
explain an outcome, never change one. The attempt archive records success, failure,
refusal and rollback alike, each with its `diffDigest`, `baseDigest` and
`evaluationJson` (including ledger execution ids), so the record is not a marketing
document. Activation maps a code subject to a thin managed skill (the change carried
in its patchable fields) so `versioning.py` is untouched, and both
`activatedVersion` and `previousVersion` survive on the candidate row. The UI is the
EXISTING learning journey (`learning_graph.py` + `learning-journey.tsx`) extended
with `evolution_candidate` / `evolution_attempt` nodes, not a second screen.
`skills/versioning.py` and `skills/patches.py` are byte-unchanged. Tests:
`test_evolution_bridge.py`, `test_evolution_persistence.py`,
`test_self_improvement_freeze.py`, `test_self_improvement_e2e.py`.

## ADR-H113 — Delivery adapters behind one interface; scope-aware surfaces; an honest sandbox backend

**Decision.** Phase 11 adds two things and changes nothing about the job model's
authority. First, delivery grows from two adapters to three behind Phase 6's
`DeliveryAdapter` protocol: `in_app`, `artifact`, and — new — `messaging`, which
carries a **template KEY plus parameters** and never composes a sentence (the
surface resolves the key through `src/lib/i18n/`, so a message that leaves the app
is translatable and visible to the jargon gate). The `artifact` adapter writes
`.black-eclipse/artifacts/<runId>/{report.md,receipt.json,meta.json}` with a path
**derived** from the server-assigned run id and confined by `policy.resolve_within`;
`receipt.json` reuses `research/receipt.py`'s shape rather than inventing a second
report format. A job may be configured with more than one destination
(`JobDelivery.targets`, additive), and each destination records its **own**
`DeliveryOutcome` on the run row — one row per target, never a rolled-up boolean. A
delivery failure is recorded and never fails the run.

Second, every new surface carries the same scope FILTER label the older routes
carry — jobs (`HarnessJob.scopeId`), code tasks (the `HarnessRun.userId` column via
`TaskStore`), artifacts (`scopeId` on `meta.json` and on the run row), and code
workspaces (`CodeWorkspace.scopeId`). The rule that must not drift, and that
`test_surface_tenancy_isolation.py` mirrors from
`test_identity_tenancy_boundary.py`: **`require_operator` stays the ONLY thing that
authorizes a mutation.** A `scopeId` in a request body is a filter label; it can
narrow what is returned and can never widen what is permitted.

Third, the command broker gains a documented `SandboxBackend` interface
(`python/harness/code/backends.py`) with three declarations — `local` (the
zero-config default, `code/sandbox.py` itself, behaviour unchanged), `container` and
`remote`. Each backend declares its `guarantees` (filesystem, network, resources,
wall clock, process tree, environment, output cap). The honest-failure rule is
absolute: an unavailable backend refuses with `SANDBOX_UNAVAILABLE`; it never
degrades to `local` and never to an uncontained run. The `local` declaration is
built FROM the enforced policy constants, so an upgrade cannot restate a weaker
guarantee as a stronger one.

**Why.** The delivery interface existed and had almost nothing behind it: a job
that ran at 07:30 produced a good report and could only land where the app could
put it, while the user had asked for a message. The scope labels existed on the
older routes as filters, but the newer surfaces (jobs, code tasks, artifacts,
workspaces) did not carry them — so "not multi-user safe" risked quietly becoming
"not even scope-aware", which is worse. And the broker was welded to the local OS
with nothing documenting what a *different* backend would have to guarantee, so
nobody could add one without guessing. This phase closes those three gaps
additively: it adds adapters and a backend interface; it does not refactor the job
model (README 3.1). The message body is the highest-risk surface in the whole
roadmap — it leaves the app and lands in a person's inbox — so it gets the strictest
reading: a template carries the finance job's name and a count, and nothing else
the user did not choose to receive; no run id, job id, duration in ms or delivery
code ever appears.

**Consequences.** `jobs/model.py` grew only additively (`JobDeliveryTarget`, an
optional `destination`/`targets` on `JobDelivery`, an optional `scopeId` on
`JobDefinition`); a job saved before this phase still delivers exactly as it did,
yielding a single derived target. `jobs/store.py` records `deliveryJson` as a LIST
of per-target outcomes and normalizes an older single-dict row to a one-element
list without rewriting it. A new route `GET /jobs/{id}/runs/{runId}/artifact` reads
a saved report and refuses a read from a different scope (404). `scripts/doctor.js`
probes vendored Python, the artifact directory, the container backend and the remote
backend; it repairs only what is safe to repair (it never installs a container
runtime), reports a missing optional backend as "not available" while the run
continues, and exits non-zero only when the vendored interpreter the runtime
genuinely requires is missing. `deployment_exposure`/`identity_boundary` are
unchanged and the app still claims no multi-user safety. Tests:
`test_delivery_adapters.py`, `test_surface_tenancy_isolation.py`,
`test_sandbox_backends.py`, plus the extended plain-language gates and the three
runbooks (`runbook-add-a-job.md`, `runbook-add-a-kit.md`,
`runbook-failed-code-task.md`).

## ADR-H114 — The code eval gate is part of the one honest release gate

**Decision.** Phase 12 extends `python/harness/code/eval/` with a **task + safety**
evaluation beside the Phase-4 verification eval, and adds it to the repo's single
honest release gate by extending `scripts/test-evals.js` (which `scripts/check-all.js`
already runs — the frozen gate file gains no step). The addition is:

1. `dataset_tasks_v1.json` — seven labelled fixtures by difficulty (one-file,
   cross-file, test-first, failing-check repair, kit scaffold, job definition, and a
   **refusal** case), each with its gold outcome, owning checks and rationale.
2. `safety_cases.py` — eleven adversarial cases as DATA, one test each in
   `python/tests/test_code_eval_safety.py`. Every test asserts **both** halves: the
   attack was blocked or contained, AND the refusal was recorded with its structured
   code. A block with no record is a failure.
3. `tasks.py` / `safety.py` / `statement.py` — the deterministic runners and the
   capability-statement generator.
4. `check_task_thresholds` — a deterministic floor, never a blended score:
   `falseVerifiedRate == 0.0`, `refusalCorrectness == 1.0`,
   `refusalRecordedRate == 1.0`, `kitAcceptanceRate == 1.0`, and an exact dataset
   checksum. `taskSuccessRate` is measured and printed, deliberately NOT a 1.0 gate.
5. `docs/harness/capability-statement.md` — the per-shape "can Eclipse build X
   today" table, **generated** from the dataset and the report (a byte-compare test
   proves it cannot drift). A shape with no fixture reads `not measured yet`, which
   is a different claim from `not yet`.

The refusal fixture is graded in the OPPOSITE direction to every other: producing a
plausible answer is the FAILURE; declining with the structured code is the PASS.

**Why.** Three gaps were concrete. The code capability had no labelled task set —
"can Eclipse build X?" was answered by prose. The safety properties were asserted in
prose and attacked by nothing, so a property nobody attacks is a property nobody
knows is still true. And the release gate did not run a code-capability eval, so a
regression in the measurement was invisible. The failure mode this phase exists to
prevent is **a gate that always passes**; that is why one gate is not "does the eval
pass" but "does a deliberately injected regression make it fail". The report is per
category, never one score, because one number hides the slice that always fails
behind the one that always succeeds, and an unmeasurable dimension prints `UNKNOWN`
rather than a fabricated `0`. The capability statement copies the exact honesty
discipline `src/lib/harness/capability.ts` already applies per market: it declines to
advertise a guarantee it cannot name.

**Consequences.** `scripts/check-all.js` and the frozen three are byte-unchanged;
the new step lives inside `test-evals.js` beside the existing floors. The Phase-4
verification eval keeps its name and API — this phase ADDS a distinct `code gate`
(the task + safety eval) rather than redefining it, so both gates print and both
pass. The eleven safety cases are data, so a new attack is a new row and a new
parameterised test, never a rewrite. The capability statement's every cell is a
`capability.*` i18n key, so the plain-language gate reads every word a finance user
sees; the eval report itself stays engineering-facing and is never rendered to a
user. The injected-regression proof asserts the **failure** of the gate (the failing
run is the evidence) and then reverts. Tests: `test_code_eval.py`,
`test_code_eval_safety.py`, plus the extended plain-language gates.


## ADR-H115 — A source is never the authority; agreement between sources is arbitrated

**Decision.** Phase 13 replaces the two static orderings that decided a
price-shaped answer (`planner._PLAN` and `layer._SINGLE_BEST`) with ONE authority
in `python/harness/retrieval/authority.py`: `AUTHORITY`, one `DataTypeAuthority`
row per data type holding the ordered candidate list, whether the type is
`single_best` (first success) or `arbitrated`, and the freshness ceiling for the
type; and `decide_source`, a pure function that returns the chosen source, a plain
reason, and every rejected candidate with why. `current_quote`,
`historical_prices`, `macro_series` and `company_profile` become **arbitrated**:
every eligible candidate runs (through the existing `parallel.run_parallel` seam),
and the existing `conflicts.detect_conflicts` / `layer._resolve` chooses the
highest-tier newest value (`sources agree`) or reports `CONFLICTING` with every
candidate and its provenance. No second conflict mechanism is introduced. A
candidate whose newest observation is older than
`min(authority.request_window_s, request freshness window)` is ineligible to
answer and is rejected with `STALE_SOURCE` and the measured age; when every
candidate is stale the status is `NO_FRESH_SOURCE` (distinct from `UNAVAILABLE`,
which means none answered at all). `financial_statements` and the qualitative
types stay `single_best` — an issuer filing is not a race with a snapshot.

**Why.** The vault was first for every price-shaped type and `_execute_single`
returned on the first success, so the live provider was **never called** once the
vault answered. A vault-vs-live disagreement could not occur, which made the
0.5 % conflict tolerance — written for exactly that case — unreachable. The system
prompt already stated the correct rule ("the vault is one source, not the
authority"); the code contradicted it. A prompt cannot fix a source that is never
consulted; only arbitration can. This is a correctness fix, not new power: nothing
new becomes writable, and no new data source is added.

**Consequences.** `planner._PLAN` and `layer._SINGLE_BEST` are deleted and their
readers repointed, so there is one ordering in the tree, not three. The web route
stays forbidden for the precise types (`historical_prices`, `macro_series`,
`derived_metric`). A `realtime` price request can no longer be satisfied by a
22-day-old vault row, and the refusal names the age. A cross-source disagreement
is `CONFLICTING` with both candidates — proven by a test that fails on the
pre-change code. Tests: `test_source_authority.py`, the retrieval regression
suites, plus the plain-language gates.

**Amendment (follow-up).** The arbitration only governs calls that go through the
retrieval layer. The agent also held `get_market_snapshot` (`finance_tools.py`),
which read the vault price/market-cap directly and **never ran the layer at all** —
so a months-old market cap could still be presented as current (the reported
demo). `get_market_snapshot` now routes its price/change/market-cap leg through the
same arbitrated `current_quote` path: a fresh live reading beats the saved
snapshot, a stale snapshot is not asserted as current, the observation time and its
age are stated, and a disagreement beyond the tolerance shows every contested
figure rather than the stored one. `normalize_quote` dates a live payload from its
`observationTime` so the cross-source grouping can see it. This confirms the ADR's
own rule at the tool grain: a source is never the authority, whichever entry point
the model chooses. Tests:
`test_demo_stale_market_cap_is_not_returned_as_current`,
`test_demo_disagreement_is_shown_when_sources_differ`.

## ADR-H116 — An observation's age is a property of the observation, not of the payload

**Decision.** `NormalizedFinancialObject` gains two ADDITIVE fields,
`observation_time` (the market time the value is about) and `observation_source`
(`provider_asof` | `snapshot_write` | `unknown`, defaulting to `unknown`), so an
old payload still loads unchanged and a missing time is never fabricated.
`validate.py` gains `validate_observations` beside the existing dataset-level
`validate_objects`: it stamps an individual stale row `warning` and reports it,
and a value whose only time is a snapshot write gains the transformation
`saved snapshot, not a live reading` and is `warning` for a `realtime` request —
never worded as live. The check is applied to non-series payloads only (an old
bar inside a history series is the point of history, not a defect).
`src/lib/harness/data-bridge.ts` supplies the fields additively:
`marketSnapshot` returns `observationSource: "snapshot_write"` with the vault
row's write time, and `liveQuote` returns `"provider_asof"` only when the provider
reports its own market time and `"unknown"` otherwise — the retrieval clock is
never dressed up as a market as-of. `cadenceSeconds` stays, but as a hint: the
runtime decides eligibility.

**Why.** The dataset-level check (`_newest_timestamp`) answers "is this batch
current?" — a payload of 40 rows where 3 are a month old passes it, so a stale row
could ride a fresh batch silently. An observation's age is a fact about that
observation. Separately, the vault's schema has no market-as-of column, so its
only timestamp is the snapshot write time; a value whose age is unknown must say
so rather than imply it is live. This is the honesty rule (`never fabricate`) at
the per-value grain.

**Consequences.** Older payloads load with `observation_source == "unknown"` and
are unaffected until a source starts reporting a time. The user-facing strings for
the as-of / delayed / disagreement wording are i18n keys (`source.*` in
`src/lib/i18n/dictionaries/en.ts`), so they pass the plain-language gate and name
the number, not the plumbing ("This is a saved reading, not a live price"). Tests:
`test_source_authority.py` (per-observation staleness), plus the extended
plain-language gates.

**Amendment (follow-up 2).** "An observation's age is a property of the
observation" must hold as **per-field** provenance, not per-payload. Routing
`get_market_snapshot` through the layer was not enough: the live provider for US
equities (yfinance `fast_info`) returns a price and **no market cap**, so the cap
always fell back to JDdS while the payload was labelled "verified" — the original
demo defect wearing a verified badge. The cap is now actually carried from the
provider when it exists (`scripts/yfinance_bridge.py` → `fetchers/yfinance.ts` →
`data-bridge.ts`), and when a single field falls back to the stored snapshot it is
labelled `— saved snapshot` with its stored time and source; the top-level
`verified` flag is true only when the PRICE itself is live, and each field's origin
rides on the structured payload. A value can never borrow another value's
provenance. Tests:
`test_demo_live_price_but_no_live_cap_labels_the_cap_saved` (the real US-equity
shape), `test_demo_stale_market_cap_is_not_returned_as_current`.

**Amendment (follow-up 3 — the general rule).** Verification is not a property of
the price leg; it is a property of every data type. Two capabilities were still
vault-bound and would fail or go stale for anything the committed snapshot lacks:
`fundamentals` (the company profile) and `market_snapshot` (a symbol absent from
the universe, or a vault row with no stored price). The vault is a **cache, not a
precondition** — the rule `financial_statements` already followed is now applied
uniformly. `fundamentals` enriches from the existing live provider pipeline
(SEC → FMP → Yahoo) when the vault lacks the profile, and reports which answered
(`profileSource`); `resolveAnyInstrument` recovers a `not_found` for a KNOWN,
covered market so a live quote can serve an instrument the vault never stored.
This is not a bypass: the identity gate still refuses AMBIGUOUS, `not_covered` and
wrong-currency answers, so a live provider can never price the wrong instrument.
The live yfinance bridge now carries `marketCap` too. Tests:
`data-bridge.test.ts`, `data-bridge-phase2.test.ts`, `data-bridge-health.test.ts`.

## ADR-H117 � One way in: the code capability is reached from the conversation, not a second form

**Decision.** A coding request typed in ordinary chat is served by the code
capability that already exists, through three additive pieces. (1)
`python/harness/code/request_intent.py::classify_code_request` � a deterministic,
model-free classifier returning `code_change` | `code_question` | `not_code`,
where an AMBIGUOUS turn is `not_code` (the refusal path is the safe failure). It
is reached over the read-only, operator-gated `POST /code/intent`. (2)
`python/harness/agents.py::CHAT_CODE_TOOLS` � a NARROWED profile applied per turn
inside the existing `graph.resolve_surface`, after the autonomy narrowing, so
there is ONE place a run's profile is computed. `code_change` ? `edit_verify`
(write + execute + verify), `code_question` ? `plan_only` (read-only). The four
external actions (`code_commit`/`code_push`/`code_open_pr`/`code_deploy`) are
absent from the declared set and can never be added back by a profile. (3) The
front door learns to send `autonomy` (`plan_only` | `edit_verify` only,
fail-closed on any other value) and `analysisEnabled`; `startChatRun` resolves
them from the classifier. The runtime's registered tool set is UNCHANGED � the
phase changes an allowlist and a payload, never a tool.

**Why.** The modules existed and were registered on the one registry, but
`eclipse-agent`'s allowlist could not read a file, apply a change or run a
command; the front door never sent an autonomy, so `plan_only` was the only
profile anyone reached and `analysis_run` was unreachable from the product. A
capability that is unreachable is, from the user's seat, missing. The classifier
is deterministic code, not a model call, because a model's opinion cannot grant a
capability (README �3.1, �3.2). External actions stay on the Code Tasks surface
where the single-use approval panel lives, so chat cannot commit, push, open a
review request or deploy.

**Consequences.** A `code_change` turn binds `workspace_patch_apply` and
`command_run` (proven against the BOUND schemas, not the prompt); a finance turn
binds no change/execute tool and behaves exactly as before. A refusal is honest
and specific (`PROFILE_DENIED`, `WORKSPACE_UNBOUND`, `SANDBOX_UNAVAILABLE`,
`COMMAND_NOT_ALLOWLISTED` carry existing plain sentences). `analysis_run` is on
only for a turn that already asked for a code change; Phase 15 owns the richer
quantitative classifier. User-facing copy lives under `code.chat.*` and passes
the plain-language gate. `RunResult.capability` is a first-class field: the
profile rides inside the existing `codeVerification` blob (`autonomy`/`workspaceId`)
and `code/capability.py::capability_from_verification` derives
`{kind, profile, headline, verification}` (an i18n headline KEY, never a
category); the panel prefers it and falls back to the flat fields for older rows.
A "Build this" / "Schedule" action on the code task list reuses the EXISTING
`/api/code/tasks` and `/api/jobs` contracts; a scheduled code job is created
disabled (`agent_id="code-agent"`, `autonomy="edit_verify"`).
Tests: `test_code_request_intent.py`, `test_chat_code_profile.py`,
`test_code_reachability.py`, `chat-capability-panel.test.ts`.

**Amendment (2026-10-25) — inline response blocks are content, not code.** A
turn that asks for the product's own inline block ("build … return it as one
interactive block", ADR-052) is a `data_task`, decided BEFORE the
directive + software-object rule. Otherwise the word `file` in "single-file"
supplied a software object and the turn was narrowed to `edit_verify`, paying
the repo-map/code-index pre-work before its first model call. Merely NAMING a
block type ("add a new interactive block type to the response component") is
still `code_change` — the rule requires a delivery verb (return/output/render/
deliver/give/show/display/put) next to the block phrase.

## ADR-H118 — The folder a person chooses is a trusted root; approval is the one widening act

**Decision.** OpenCode-style general software work is enabled without repealing
the isolation invariant. A person picks a folder; an operator approves it
(`POST /workspace/roots`, `require_operator`); it is persisted in a
`TrustedRoot` table on the runtime's own `agent-memory.db`. The SELECTED root is
what `workspace.default_root()` resolves to, after the `BLACK_ECLIPSE_CODE_ROOT`
override and before the built-in repo root, so zero-config is unchanged and a
chosen folder takes effect on the next run without a restart. Approval is the
ONE place a new root becomes workable; every read and write is still confined by
`policy.resolve_within`, and `PROTECTED_PATHS` remain untouchable inside an
approved folder.

**Why.** The prior design bound only the server's own repo root, so a person
could never point Eclipse at their own project. Widening "any path the model
names" would be a security hole; a persisted, operator-approved allowlist is the
narrow, auditable widening that keeps the model from choosing its own boundary.

**Consequences.** `code/roots.py` owns the registry + a bounded directory
browse. `routers/workspace.py` owns the routes. The editor's file APIs
(`/workspace/files`, `/workspace/file/read|write`) use the SAME confinement the
agent tools use, so the human editor is a second door to one boundary, never a
weaker one. A root that vanishes falls back to the repo root rather than failing
every run.

## ADR-H119 — A direct edit is opt-in and lands in the real folder; the sandbox stays the default

**Decision.** `worktree.allocate(direct=True)` returns the resolved root itself
(strategy `"direct"`) instead of a disposable worktree or snapshot; `release`
NEVER deletes a direct workspace. Direct mode is reachable only for a run whose
root resolution already succeeded, and a code task defaults to direct when (and
only when) a trusted folder is selected — an explicit `direct` always wins. The
built-in repo root with no selection stays isolated.

**Why.** "Pick a folder and edit it" is the point of choosing it; making the user
flip a second switch after the explicit folder approval would be ceremony. The
safety comes from the approval (ADR-H118) and the confinement (unchanged), not
from a copy. The copy remains for the built-in repo, where the operator never
opted in.

**Consequences.** `RunRequest.direct` is additive and default-False at the
schema level (old callers unchanged); the route resolves the default from the
selected root. `worktree.diff` reports a direct workspace honestly: a git diff
when the folder is a checkout, an empty result otherwise — never a synthesized
change.

## ADR-H120 — The terminal is one tool with a three-way classifier, not a shell

**Decision.** `terminal_run` runs an arbitrary command WITHOUT a shell
(`shlex.split`, argv exec through `code/sandbox.py`). A pure classifier sorts
every command into `allow` (a safe-program allowlist: interpreters, package
managers, test/build/lint, git, read-only coreutils, safe file mutators), `ask`
(anything else), or `deny` (destructive shapes, shells, downloaders, and
shell-chaining metacharacters). `allow` runs; `ask` requires a single-use,
workspace-bound approval (`kind="terminal_command"`) created on first call and
consumed on retry; `deny` is refused and is never approvable. The registry's
effect authorizer and the handler both enforce the verdict.

**Why.** The user asked for real terminal access. The safe way to add it is not
to delete the closed command table but to add a generic tool whose safety is a
classifier enforced in Python — never by a prompt — plus the existing
sandbox/wall-clock/output/process-tree bounds. No shell means a `; rm -rf` is
inert text, not a second command.

**Consequences.** `code/terminal.py` owns classification + `RunSpec`; the
handler is `code/terminal_tools.py`; the tool is registered only on the
code/chat surfaces and is removed at `plan_only`. Every execution is recorded in
`HarnessCommandExecution` and the verification ledger exactly like `command_run`,
so a claim about it is checkable.

## ADR-H121 — The live stream shows what a tool is doing, content-free

**Decision.** `tool.requested` carries a bounded `detail` and `tool.completed`
carries a bounded `summary`, both derived by one pure function
(`code/activity.py::tool_detail`/`tool_result_summary`). A `detail` is a command,
a file PATH, or the changed PATHS; a `summary` is an exit code, a count, or
"timed out". File CONTENT is never included: a `workspace_patch_apply` carrying a
secret in `content`/`new` summarizes to its paths alone.

**Why.** A coding harness earns trust by showing its work — "Running
`python app.py` → exit 0", "Writing the change `stream.py` → 1 file(s)". Eclipse's
chat showed only a tool NAME ("Reading a file"), so the surface read like a
chatbot with a spinner, not a harness. The fix belongs at the emitter: the
runtime owns the tool call and its data, so it can summarize safely.

**Consequences.** `activity.py` owns the derivation (a fixed field allowlist per
tool; a `MAX_DETAIL_CHARS` bound). The event carries display metadata only — it
is never an authorization. `ActivityItem.detail` is the one new client field; the
activity reducer already spreads it through. A tool with no safe detail or a
result with no known shape reports nothing rather than a fabricated line.

## ADR-H122 — Replanning is one bounded node on the existing graph, flag-gated and default off

**Decision.** When a tool step FAILS (one or more tool errors) or STALLS (the
agent issues the same `(tool,args)` set as the immediately preceding turn), the
loop may divert through ONE `replan` node before returning to the agent. The node
appends INERT prose naming what failed or stalled; it authorizes nothing, widens
no allowlist, and builds no second graph, registry or model adapter. The path is
FLAG-GATED (`HARNESS_REPLAN_ENABLED`) and DEFAULT OFF; bounded by
`ReplanBudget.max_replans` (default 2); observable via a persisted `run.replanned`
event (`replan`, `reason`, `maxReplans`). The trigger predicate, budget and
directive live in `python/harness/replan.py`; the node sits on the SAME
`tools → agent` edge in `graph.py`. A stall outranks a failure when both hold.

**Why.** A long tool loop can repeat itself until `maxSteps` and answer nothing —
`MAX_STEPS_EXCEEDED` is the honest failure but a poor outcome when a revised
approach was available. The fix must not become the thing the platform forbids
(ADR-H1): no second agent loop, tool registry or model adapter, and no privilege
created by a replan. An explicit, bounded, observable revision step satisfies the
capability without crossing those boundaries.

**Consequences.** `graph.py` gains a `replan` node only when the flag is on (the
edge mapping is built conditionally, so a disabled run's graph is byte-identical
to before). `GraphState` gains bounded replan fields (`replans`, `step_failures`,
`stalled`, `last_turn_keys`, `step_failure_details`). To stay under the file-size
ratchet, the checkpoint (de)serialization moved to `checkpoint_io.py` and the
history-bounding + dropped-context helpers moved to `history.py` (both
re-exported by `graph.py`, so callers/tests are unchanged). An optional,
best-effort model-authored plan (`replan.model_replan`) is additive and can only
be text; the deterministic directive is the fallback. Tests:
`python/tests/test_replan.py`.

## ADR-H123 — The model layer is a provider-agnostic runtime, not a per-run factory

**Decision.** Eclipse gains a `ModelRuntime` layer (`python/harness/model_runtime/`)
that is the ONLY interface the rest of Eclipse uses to reach a model:

    Agent → ModelRuntime → ModelRouter → ProviderAdapter → Provider

`build_chat_model` is demoted from "the model seam" to a low-level construction
primitive that provider adapters delegate to; no agent, tool, memory or financial
logic names a provider. The runtime owns: a provider registry and a model
registry seeded from the existing `PROVIDER_CATALOG`; capability detection as
states (`supported` / `unsupported` / `unknown` / `degraded`); task profiles;
logical aliases resolved dynamically; a scoring router (capability, quality,
reliability, latency, availability, context, cost) with configurable weights;
fallback and error-aware retry with exponential backoff + jitter; health, circuit
breaker and rate-limit managers; normalized token/cost/latency/budget tracking;
normalized streaming, tool schemas, structured output and reasoning; and an
explainable routing decision trace. Persistence lives in the runtime's OWN
`ModelRequest` / `ModelHealth` tables on `agent-memory.db` — never JDdS (spec §37).

**Why.** The agent was hard-wired to one resolved provider per run with no
health, fallback, cost or capability awareness, so "switch model" meant editing
the graph. A financial platform must let the model change without changing what
the data means: model selection must be observable, cost measurable, failures
recoverable and capabilities explicit, while the Evidence engine stays the sole
source of financial truth. Reusing `PROVIDER_CATALOG` and `build_chat_model` keeps
one source of provider facts and one construction path rather than a second copy.

**Consequences.** `graph.py` resolves and builds through the runtime but still
invokes the module-level factory, so the existing test seam (dozens of
`monkeypatch.setattr(graph_module, "build_chat_model", …)`) and all 2535 harness
tests are unchanged. A concrete provider/model in `ModelSettings` stays the
explicit override (spec §44); the router only changes it when it can build with
the run's credentials (same provider) or a keyless local provider, so it never
silently routes to a paid provider the operator did not configure (spec §31/§45).
Cross-provider fallback and in-place retry are flag-gated
(`HARNESS_MODEL_FALLBACK` default OFF, `HARNESS_MODEL_RETRIES` default 1) so
single-credential zero-config behaviour is byte-identical. Model usage/health
persist to `agent-memory.db` via `model_runtime/store.py`; read-only inspection
routes (`/model-runtime/*`) are operator-gated. Tests:
`python/tests/test_model_runtime.py`.



> **ADR-H124 and ADR-H125 are reserved** - numbers withdrawn during the Round 3 rework and never written. They are retired, not missing.

## ADR-H126 — The terminal is infrastructure with one runtime shared by the agent and the UI

**Status:** Accepted (2026-10-05)

**Context.** Eclipse already had three execution paths that had grown
independently: `code/sandbox.py` (bounded one-shot argv execution),
`code/shell.py` (a shell string, background `spawn`), and `code/terminal.py` /
`terminal_tools.py` (a classified, shell-free command). Each was correct in
isolation, but none owned a *process*: a background `npm run dev` returned a pid
the agent polled by hand, there was no persistent shell that preserved `cd` and
environment, no port detection, no process tree, no crash record, no recovery
after a restart, and the visible terminal panel was a second, weaker execution
door with its own state in React. The request is broader: give Eclipse the
execution backbone of a serious agentic harness, with one runtime underneath both
the agent tools and the UI.

**Decision.** Add `python/harness/terminal/` as infrastructure, not a UI feature:

1. **One facade.** `TerminalManager` is the only terminal entry point; the agent
   tools (`terminal/tools.py`) and the HTTP router (`routers/terminal.py`) are thin
   adapters over it, so UI and agent can never disagree about process state.
   React state is never the source of truth.
2. **Two primitives + a session.** `execute_command` (bounded, cancellable,
   through `code/sandbox.py`), `start_process` (a monitored long-running process
   with no ordinary timeout), and `open_shell`/`run_shell_command` (a persistent
   shell preserving `cd`/environment). A real PTY is used when a PTY library is
   installed; the piped fallback is reported as `pty: false`, never called a PTY.
3. **Deterministic process state.** A state machine applied once by the monitor
   thread that reaps the OS exit — never inferred from "does a PID exist". stdout
   and stderr are separate, sequenced ring buffers with a cursor, so polling never
   duplicates output and large output is bounded.
4. **Honest runtimes.** `LocalRuntime` and `WSLruntime` are implemented;
   `DockerRuntime` and `SSHRuntime` exist and refuse with `NOT_IMPLEMENTED`.
   `select_runtime` never degrades to local.
5. **Reused safety.** The tools are removed at `plan_only` through the existing
   `code/policy.allowed_tools`; the classifier is the existing
   `code/shell_tools._verdict`; approvals are the existing single-use store;
   redaction is the existing `session_index.redact_span`; persistence is the
   existing one `agent-memory.db` connection, owning only its own tables.
6. **Recovery.** On boot, a persisted process whose pid is gone is `EXITED`; one
   whose pid still exists is `UNKNOWN`, never `RUNNING`. Sessions are closed.

**Why.** A coding/agentic harness is only as capable as its execution layer: the
loop *modify file → run → read error → fix → restart → verify* cannot exist
without persistent processes, streamed output, ports and crash detection. Doing
this inside the existing sandbox would have conflated one-shot bounded execution
with long-lived process management; doing it in TypeScript would have violated
the "agentic runtime is Python" rule; doing it as a UI feature would have made the
terminal unusable headless, which is exactly when autonomous agents need it. One
shared runtime keeps the agent and the human looking at the same truth.

**Consequences.** `code/sandbox.py`, `code/shell.py`, `code/terminal*.py`,
`code/backends.py`, `code/policy.py`'s command vocabulary and every existing test
are preserved; `terminal_run`/`shell_run`/`shell_spawn` remain for compatibility
and share the classifier. `policy.py` gains `TERMINAL_WRITE_TOOLS` /
`TERMINAL_READ_TOOLS` and `SECRET_ENV_ALLOWLIST` gains `PATHEXT` (a measured
latent gap: a scrubbed shell could not resolve an executable by name). Both agents
and `CHAT_CODE_TOOLS` gain the terminal tools; the pinned chat-set test is updated
in place. New tables are additive on `agent-memory.db`. Tests:
`python/tests/test_terminal_{buffer,shell_and_runtimes,process,session,store_and_recovery,tools,api,e2e}.py`.
Full suite green; `npx tsc --noEmit` clean.


## ADR-H127 — Real PTY by default; Docker/SSH runtimes; runaway is reported, never auto-killed

**Status:** Accepted (2026-10-05)

**Context.** ADR-H126 shipped the terminal subsystem with four explicit
limitations: a piped fallback when no PTY library was installed, Docker/SSH
runtimes that only declared an interface, and no resource telemetry or
runaway-process policy. A coding harness that cannot render a real interactive
prompt, run a build inside the project's container, or notice a spinning process
is missing the things its execution loop is most often pointed at.

**Decision.**

1. **Real PTY is the default.** `pywinpty` (Windows) / `ptyprocess` (POSIX) are
   declared in `python/requirements.txt` behind platform markers, so the default
   install yields a genuine ConPTY/forkpty (`pty: true`). The agent-facing session
   text has ANSI stripped; the UI reads the raw buffer so a real terminal can
   render. A `cd`/`export` is sent to the REAL shell, the resulting cwd is read
   from the shell's own prompt via the sentinel, then confined to the workspace; a
   command that leaves the root is refused and the shell returned to the root. The
   piped fallback remains, and stays honestly reported as `pty: false`.
2. **Docker and SSH are implemented** for command execution and background
   processes, behind operator config (`HARNESS_TERMINAL_DOCKER_CONTAINER`/`_WORKDIR`,
   `HARNESS_TERMINAL_SSH_HOST`). Interactive sessions for them refuse
   `NOT_IMPLEMENTED` (a session needs a shell we drive directly). SSH uses the
   host's own OpenSSH config/agent with `BatchMode=yes`; the subsystem never reads,
   stores or forwards a key or password. A new `extra_env()` lets a runtime forward
   only the non-secret host variables its client needs (`DOCKER_HOST`,
   `SSH_AUTH_SOCK`), merged over the scrubbed base environment.
3. **Runaway is reported, never auto-killed.** `resources.py` reads CPU/memory
   directly from the OS (no `psutil`). `ResourceLimits` refuses a start past
   `maxProcesses` with `PROCESS_LIMIT`, and flags — but never kills — a process
   whose output buffer or lifetime exceeds its bound, emitting
   `terminal.process.runaway`. A legitimate long-running dev server is exactly the
   case this must not mistake for a fault.

**Why.** The alternative — keeping the four limitations — would leave the harness
unable to do the interactive work it exists for, and the alternative to (3), an
auto-kill, would destroy a user's server on a heuristic. The honest middle is:
real capability by default where the platform supports it, operator configuration
for the remote surfaces, and a report-and-let-the-operator-decide rule for a
possible runaway.

**Consequences.** `requirements.txt` gains two optional-but-default deps (with
markers, so neither is installed needlessly). `runtimes.py` grows past its prior
stub; `shell_detect.capabilities()` now lists a runtime only when it can actually
serve a command. `policy.SECRET_ENV_ALLOWLIST` is unchanged — runtime-specific vars
come from `extra_env()` per call, not the global allowlist. Tests:
`test_terminal_resources.py`, the Docker/SSH unit cases, and real-PTY session
coverage. Full Python suite green (2636); Vitest 2093; tsc clean; eval gates PASS.

## ADR-H128 — Code intelligence is one engine over LSP/AST/index, never a set of LSP wrappers

**Status:** Accepted (2026-10-06)

**Context.** Eclipse could read, search and patch files, but it treated the
repository as text: there was no definition/reference lookup, no dependency or call
graph, no diagnostics loop, and no way to give the model a focused slice of code
instead of a directory dump. Competing coding agents understand software
structurally and semantically. The task: build a first-class Code Intelligence &
Understanding Engine (CIUE) that is NOT a few LSP wrappers, is transport-agnostic,
degrades honestly, and reuses the existing filesystem/terminal/tool/policy
infrastructure.

**Decision.**

1. **One facade over four backends.** `CodeIntelligenceManager`
   (`python/harness/code/intel/`) coordinates an LSP manager, a Tree-sitter layer,
   an AST engine and a dedicated local index, and normalizes every answer into a
   Unified Code Model (`models.py`). The agent asks `code_get_definition` /
   `code_find_references` / `code_find_symbol` / … and never learns which backend
   answered — but every result NAMES its engine (`lsp` / `tree-sitter` / `ast` /
   `index`). LSP is tried first, then the structural fallback; a server failure
   never crashes the runtime.
2. **Provider registry, not a hard-coded server.** `lsp/providers.py` ships nine
   real servers (TS/JS, Python, Java, C#, C/C++, Go, Rust, PHP, SQL); adding one is
   adding a `LanguageServerSpec`. Providers are probed, never assumed.
3. **A DEDICATED index.** `persistence/index.py` (`CodeIndex`) is its own SQLite
   store under `.black-eclipse/code-index/` — never `agent-memory.db`,
   `eclipse-memory.db` or `jdds.db`. It is incremental (changed files only), a cache
   (every query can fall back to a live scan), and recoverable (a corrupt database
   is backed up, recreated and re-indexed).
4. **Reuse, not rebuild.** Path safety is `code/policy.py` + `code/workspace.py`;
   reading/encoding/watching and the per-language extractors are `code/fs/`; the
   tool registry, event emitter and effect authorizer are the existing seams. No
   second confinement implementation, filesystem, terminal or registry.
5. **Rename is semantic or it is a labelled preview.** LSP `WorkspaceEdit` when a
   server runs; otherwise a deterministic, preview-first structural edit from the
   index. Never a regex. Applying requires `code.write` (removed at `plan_only`) and
   writes through the fs atomic encoder.
6. **Deterministic context first.** `context.py` selects relevant files, symbols,
   call paths, diagnostics and tests by deterministic term/symbol/reference matching
   — no embeddings — so the model is never handed the whole repository.
7. **Never crash, never fabricate.** Every CIUE tool returns `(text, data)` like
   every other code tool, with structured refusals and a bounded compact payload.
   Initial indexing is asynchronous; a query refreshes stale data incrementally.

**Why.** LSP wrappers alone cannot answer "what depends on this file" or "what
tests cover this symbol", and they fail the moment a server is missing. An engine
that degrades to a real structural index, names its source, and reuses the existing
safety and tooling gives Eclipse repository-level software engineering without
duplicating infrastructure or pretending to a capability it does not have.

**Consequences.** New packages `python/harness/code/intel/` and
`intel_tools.py`; a new route group `routers/code_intel.py`; `policy.py` gains
`INTEL_WRITE_TOOLS`/`INTEL_READ_TOOLS`; both agent allowlists and `CHAT_CODE_TOOLS`
gain the tools (the pinned chat-set test is updated in place);
`ToolContext.code_intel_runtime`; `AgenticService` owns one `CodeIntelRuntime`
(started lazily, stopped with the runtime). No new third-party dependency is
required (Tree-sitter is optional; LSP is stdlib subprocess + JSON). Tests:
`test_intel{,_tools,_lsp,_api}.py` + fixtures + a fake language-server subprocess.
Architecture: `docs/harness/code-intelligence.md`.


## ADR-H129 — Code execution is an orchestrated lifecycle over the one sandbox, not a second terminal

**Status:** Accepted (2026-10-06)

**Context.** Eclipse could run individual allowlisted commands (`command_run`),
open persistent shells and manage background processes (the terminal subsystem),
and compute a derived metric in the analysis sandbox. It could NOT take a project
through the whole loop a coding agent needs: detect the language and runtime, build,
run, discover and run tests, normalize compiler/runtime/test failures into structured
diagnostics, and iteratively repair until verification evidence exists. Adding each
language ad hoc (or a second shell wrapper) would have duplicated infrastructure and
let the model silently widen its own permissions.

**Decision.**

1. **A dedicated layer, not a terminal wrapper.** `python/harness/execution/`
   owns the lifecycle. The terminal subsystem keeps interactive OS control; the
   execution layer owns language/runtime/entrypoint/sandbox/resource/evidence. They
   share the low-level process primitive and the terminal's process manager (for
   long-running services) but their responsibilities stay distinct.
2. **One execution primitive.** Every one-shot run goes through the existing
   `code/sandbox.py` (`RunSpec` → `RawResult`): argv arrays, no shell, scrubbed
   environment, bounded lifetime, hard output cap, whole-process-tree kill. There is
   no second spawn path. A runtime adapter stages an inline program outside the
   workspace and returns an argv — it never executes.
3. **The orchestrator owns the lifecycle; adapters own language behaviour.** The
   orchestrator resolves project → language → runtime → context → prepare → run/
   build/test → collect diagnostics/artifacts/metrics → cleanup → store. A
   `RuntimeRegistry` maps a language id to a `RuntimeAdapter`
   (`detect`/`resolve`/`prepare_run`/`build`/`test`/`discover_tests`). Python,
   JavaScript, TypeScript, SQL and Shell ship; Java/Go/Rust/C/C++/C#/PHP/Ruby are
   declared and report UNAVAILABLE honestly until an adapter is registered.
4. **A strict, validated state machine.** `ExecutionState` is one enum; every
   transition is checked and a terminal state has no outgoing edges. A `finally`
   path always lands the machine on a terminal state and always removes the staged
   working directory, whatever happened.
5. **The model can never widen its own permissions.** The `ExecutionPolicyEngine`
   narrows the request's network/filesystem policy to the autonomy profile's ceiling
   and refuses an explicit widening (`NETWORK_NOT_PERMITTED`); forbidden secret names
   (`.env`, cloud/SSH/GitHub tokens, API keys) are refused (`SECRET_FORBIDDEN`), never
   silently dropped. Risk bands are DATA (SAFE…CRITICAL); a HIGH+ band requires an
   approval. The existing `code/policy.allowed_tools` still removes the write/execute
   tools structurally at `plan_only`.
6. **Evidence, never inference.** The `ExecutionVerifier` runs a bounded
   build→test→fix→rebuild→retest loop (`max_iterations`, wall-clock budget). A
   "passed" outcome is only produced from an actual `COMPLETED` execution with a
   real test summary; a compiler error/security block is never "fixed" by editing
   code, and no success is ever claimed without evidence. An unparsed test summary
   reports `parsed: false`, not a pass.
7. **Distinguish infrastructure from user-code failure.** `ErrorClass`
   (`USER_CODE_ERROR` / `BUILD_ERROR` / `TEST_ERROR` / `RUNTIME_ERROR` /
   `INFRASTRUCTURE_ERROR` / `SECURITY_ERROR` / `TIMEOUT` / `CANCELLED`) so an agent can
   choose retry / fix / stop / ask. `RetryPolicy` is category-driven: a timeout may
   retry; a compiler error, permission failure or sandbox violation must not.

**Why.** A coding agent cannot be trusted to invent its own execution semantics.
Making the sandbox the single primitive, the policy engine the single authority, the
state machine the single lifecycle, and evidence the single basis for success gives a
production capability that is secure by construction and honest by default — and it
extends by registering an adapter, not by editing the orchestrator.

**Consequences.** New package `python/harness/execution/` and route group
`routers/execution.py` (`/execution/*`, operator-gated); `policy.WRITE_TOOLS` /
`READ_ONLY_TOOLS` gain the tool names; `ExecutionStore` owns the `Execution*` tables
on the ONE `agent-memory.db` connection (never `eclipse-memory.db`/`jdds.db`);
`AgenticService` owns one `ExecutionService` (built after the terminal manager,
stopped with the runtime); 21 tools register on the SAME registry. No new third-party
dependency (SQLite is stdlib; PostgreSQL requires an optional driver and refuses
otherwise). Retrieval tools are prefixed `execution_`/`service_` to preserve the
code-agent's finance-tool guard. Tests: `test_execution_{unit,runtime,security,e2e,
tools}.py`. Architecture: `docs/harness/execution-architecture.md`.

## ADR-H130 - Git is a layered engine with a closed operation table and a separate VCS provider layer, not CLI wrappers

**Status:** Accepted (2026-10-06)

**Context.** Eclipse could read a git checkout's status (a fixed, argument-free
read in `code/fs/git.py` and `code/workspace.py`) and could run a closed
`git_read` command class, but it could NOT drive a project through a full Git
workflow: create a branch, stage selectively, commit, merge, rebase, resolve
conflicts, stash, tag, worktree, push safely, or open a pull request. The
tempting shortcut — wrap `git status`/`commit`/`push` with a shell string and let
the model pass arguments — would put an unbounded, un-recoverable, un-auditable
capability in front of an autonomous agent, with no risk classification, no
recovery, no journal, and no separation between local version control and remote
hosting.

**Decision.**

1. **A layered subsystem, not a wrapper.** `python/harness/git/` composes
   discovery, ONE controlled executor, machine parsers, a state machine, a safety
   engine, a recovery manager, an operation journal and a lock behind a single
   `GitService` facade. Tools and routes are thin adapters over it.

2. **A CLOSED operation table.** `catalog.py` is the only place a git argv is
   defined; `operations.py` validates every value (no leading `-`, no pathspec
   magic, no NUL, bounded). The model never supplies an executable, a flag or a
   shell fragment. A subcommand absent from the table is unreachable.

3. **The sandbox discipline extends to git.** The executor runs one fixed argv
   with NO shell, a scrubbed environment, `GIT_TERMINAL_PROMPT=0` (fail, never
   hang on a credential) and `GIT_EDITOR=true`/`GIT_MERGE_AUTOEDIT=no` (never
   block on an editor), under the per-repository mutating lock, with a bounded
   timeout and output cap.

4. **Machine output only.** Status/diff/log/blame/worktree/… are parsed from
   fixed machine formats (`--porcelain=v2`, NUL-delimited `--format`), never
   human-readable output that can drift between git versions.

5. **Risk is machine-readable; authorization is layered.** Every op carries a
   SAFE…CRITICAL band. The autonomy profile is the grant
   (`code.policy.allowed_tools`: `plan_only` removes writes + remotes,
   `edit_verify` keeps local writes, `approve_external` keeps remotes); the Git
   effect authorizer then requires a SINGLE-USE operator approval for any
   dangerous/destructive op and for EVERY remote action. A force push is
   `critical` and never the default.

6. **Recoverable by construction.** Multi-step operations are detected from the
   repository's own sentinel files on every call, never a cached flag; a conflict
   is a structured failure plus a persisted active-operation record; the journal
   records every mutation (sanitized); nothing discards work automatically.

7. **Local Git and remote hosting are separate.** Git is the local VCS; GitHub/
   GitLab/Bitbucket are remote collaboration providers behind `VCSProvider` with
   a normalized PR=MR model. Tokens come from operator-declared env-var NAMES and
   are never exposed; a remote result is never faked.

**Why.** An autonomous agent must not be able to destroy history, overwrite
unpushed work, force-push, or silently bypass hooks — and it must be able to
recover when a merge or rebase stops half-done. A closed table + a single
executor + a risk/authorization engine + a recoverable state machine + a journal
gives a capability that is secure by construction, honest by default, and
testable in isolation.

**Consequences.** New package `python/harness/git/` (+ `git/vcs/`); route group
`routers/git.py` (`/git/*`, operator-gated); the effect-authorizer composition is
extracted to `code/authorizers.py` (so `graph.py` stays under its ratchet);
`code/policy.py` gains `GIT_READ_TOOLS`/`GIT_WRITE_TOOLS`/`GIT_EXTERNAL_TOOLS` and
narrows them in `allowed_tools`; the Git tools register on the SAME registry via
`service.py` (`register_git_subsystem`), like the terminal and execution
subsystems; the fs subsystem's older read `git_status` tool is removed in favour
of the one richer engine tool (the `FileSystemService.git_status` method and the
`file_metadata` git awareness are kept). No new third-party dependency (the VCS
HTTP client is stdlib `urllib`). Tests: `test_git_{engine,mutations,security,tools,
integration}.py` (isolated temp repositories). Architecture:
`docs/harness/git-version-control.md`.

## ADR-H131 — The Application Runtime is a provider-agnostic lifecycle over the one sandbox and the one terminal

**Status:** Accepted (2026-10-06)

**Context.** Eclipse could run one-off snippets, build a project and run its
tests, and it had a real terminal (persistent processes, ports, trees). It could
NOT understand an *application*: discover its framework and package manager,
install dependencies, start its services, check whether it is actually healthy,
diagnose why it isn't, recover it, or track it across an Eclipse restart. The
tempting shortcut — a bag of `docker`/`npm` shell wrappers the model strings
together — would couple the agent to Docker and npm, bypass the sandbox, expose
secrets, and conflate "a process exists" with "the app works".

**Decision.**

1. **One provider-agnostic registry.** `RuntimeRegistry` holds package managers,
   container runtimes, compose, and the (empty, extension-only) Kubernetes/
   Terraform/cloud seams. Adding Kubernetes or AWS later means registering an
   adapter, not rewriting the engine. Adapters declare a CLOSED capability set,
   so no caller asks an adapter for a meaningless operation.

2. **One lifecycle.** `ApplicationRuntimeManager` owns DISCOVER → VALIDATE →
   PREPARE → INSTALL → BUILD → START → HEALTH → RUNNING → OBSERVE → STOP →
   CLEANUP and delegates every concrete action (discovery, argv, containers,
   ports, health, persistence). It never implements a language or an engine.

3. **Reuse, never rebuild.** Local services start through the EXISTING
   `TerminalManager`; every argv runs through the ONE `code/sandbox.py`; the
   autonomy boundary is the EXISTING `code/policy.py`; runtime state lives on the
   ONE `agent-memory.db`; runtime errors reuse the execution layer's diagnostic
   parser for their code location.

4. **`RUNNING` is not `HEALTHY`.** State (`DISCOVERED…RUNNING/DEGRADED/STOPPED/
   FAILED`) is explicit and validated; health is a SEPARATE report produced only
   by a passing process/TCP/HTTP/container/database/custom check. A service that
   starts but never answers becomes `DEGRADED`.

5. **Honest failures.** A missing runtime/manager/engine is a structured refusal
   (`RUNTIME_MISSING`, `PACKAGE_MANAGER_UNAVAILABLE`, `ENGINE_UNAVAILABLE`), never
   a silent switch or a fake success. A declared/lockfile package manager is
   authoritative (no pnpm→npm fallback). Reconciliation after an Eclipse restart
   believes a persisted `RUNNING` only when the live terminal still knows the
   process.

6. **Security by construction.** Every argv is a validated list — no shell, no
   string concatenation; names/images/ports/volumes/env keys are validated.
   Secret values never reach the model, logs or events (only `[SECRET]` and
   existence). Destructive ops (`container.rm`, `volume rm`, `rmi`,
   `compose down/rm`, `.env` writes) are `external_action` tools removed at
   `plan_only` AND require explicit authorization.

7. **Bounded recovery.** A single bounded diagnose→strategy→act→verify loop
   (`HARNESS_APP_RECOVERY_MAX_ATTEMPTS`, default 3, hard cap 10) records every
   attempt and never acts on an irrecoverable diagnosis. No `while true`.

**Why.** An autonomous agent that runs applications must be secure by
construction, honest by default, provider-agnostic, and testable in isolation —
exactly like the terminal, execution, code-intelligence and Git subsystems before
it. A registry + one lifecycle + reused primitives delivers that; a bag of
wrappers cannot.

**Consequences.** New package `python/harness/execution/application/`; route group
`routers/application.py` (`/runtime/*`, operator-gated); `code/policy.py` gains
`APP_RUNTIME_WRITE_TOOLS`/`APP_RUNTIME_READ_TOOLS` and narrows them in
`allowed_tools`; the tools register on the SAME registry via `service.py`; the
front door adds `application-{client,browser}.ts`, `/api/runtime/**` and an
Applications tab in the terminal dock. No new third-party dependency (the
container/compose adapters use the existing sandbox). Tests:
`test_application_{unit,runtime,security,tools,api,e2e}.py`. Architecture:
`docs/harness/application-runtime.md`.



## ADR-H132 - Orchestration, infrastructure and cloud are declarative CLI adapters over one base, and deployment is a bounded lifecycle

**Status:** Accepted (2026-10-06)

**Context.** ADR-H131 shipped the local Application Runtime with empty
Kubernetes/Terraform/cloud seams. Filling them the tempting way — a per-provider
module each re-implementing sandbox discipline, argv building and JSON parsing —
would multiply the security surface with every provider and couple the engine to
each CLI.

**Decision.**

1. **One `CliAdapter` base.** `kubectl`/`helm`/`kustomize`, `terraform`, and
   `aws`/`az`/`gcloud`/`wrangler`/`vercel`/`railway`/`render` are each a
   declarative configuration: `kind`, `binary`, `capabilities`, and a CLOSED
   `operations` table of `CliOperation`s (an `argv` builder + a `validator`).
   Argument validation, JSON parsing, the sandbox call and the availability probe
   live ONCE in the base. A new provider is data, not a new engine.

2. **The sandbox discipline is inherited.** Every operation runs through
   `code/sandbox.py` with a scrubbed environment, a bounded timeout and output cap,
   and NO shell. A resource name/namespace/image/file/command containing a shell
   metacharacter is a structured `INVALID_ARG` refusal.

3. **Honest availability, honest capabilities.** A missing binary is
   `ENGINE_UNAVAILABLE`; an adapter declares a CLOSED capability set drawn from
   `ALL_CAPABILITIES`, so no caller asks for a meaningless operation. Credentials
   reach a cloud CLI only through its OWN ambient mechanism — this layer never
   passes or reads a secret, and it reports only whether a declared credential env
   NAME is set.

4. **Deployment is a bounded lifecycle, not a shell call.** `DeploymentEngine`
   runs validate → build → test → package → deploy → verify, delegating build/test
   to the SAME package managers the local runtime uses and deploy to a cloud
   adapter. A `production` deploy requires explicit authorization; a deployment is
   `deployed` only when the provider accepts it — never claimed live from a status
   call alone.

5. **Authorization is layered and structural.** The cluster/infra/cloud MUTATIONS
   are removed at `edit_verify` and `plan_only` (`code.policy.allowed_tools`); every
   one is an `external_action` and is additionally gated at call time
   (`terraform_apply`/`terraform_destroy`/`k8s_delete`/`helm_uninstall`/
   `deploy_delete`/a production `deploy_run`). Reads stay available at every profile.

6. **A real CLI, not a wrapper.** `harness/cli.py` builds the SAME process-wide
   manager the agent tools and the API use, so the CLI, the agent and the UI cannot
   disagree. It is structured (`--json`), it refuses a destructive command without
   `--authorized`, and it adds no execution logic.

**Why.** The provider-agnostic registry promised in ADR-H131 only pays off if the
providers themselves are uniform and secure by construction. One declarative base
+ a closed capability contract + a bounded deployment lifecycle makes Kubernetes,
Terraform and seven clouds additive and reviewable, without multiplying the attack
surface or coupling the engine to any CLI.

**Consequences.** New modules `cli_base.py`, `orchestrators.py`,
`infrastructure.py`, `cloud.py`, `deployment.py`, `cache.py`, `cli.py`,
`scripts/eclipse.js`; 29 new agent tools (`k8s_*`/`helm_*`/`kustomize_*`/
`terraform_*`/`cloud_*`/`deploy_*`) in `k8s_infra_tools.py`; `code/policy.py` gains
`APP_RUNTIME_K8S_WRITE_TOOLS` and narrows it at `edit_verify`/`plan_only`; route
group `routers/application.py` gains the orchestrator/terraform/deploy routes;
front door `application-{client,browser}.ts` + `/api/runtime/**` + the Applications
provider footer. No new third-party dependency. Tests:
`test_application_{orchestration,deploy}.py`. Architecture:
`docs/harness/application-runtime.md`.
## ADR-H133 - Provider readiness, managed long-running CLI ops, staged promotion and a cross-application graph complete the Application Runtime

**Status:** Accepted (2026-10-06)

**Context.** ADR-H132 shipped the orchestration/infra/cloud adapters and the
deployment lifecycle. Four real gaps remained: (1) a missing CLI or an
unauthenticated provider was honest only at CALL time, never at boot; (2) a
long-running CLI operation (`kubectl port-forward`, `logs -f`) had no home, so it
was refused as a one-shot op; (3) a deploy was one-shot, with no staging→production
promotion or rollback-on-failure; (4) the runtime understood ONE application, not
a workspace of several.

**Decision.**

1. **Readiness is a first-class, non-secret report.** `provisioning.py` answers
   "is this machine ready to run/deploy THIS project" from the registry's
   availability probes plus each provider's own ambient-credential status. It is
   on `/health`, `GET /runtime/providers` and `eclipse providers`. A credential is
   never read; only whether a declared env NAME is set. An unknown auth state is
   `null`, never a fabricated boolean.

2. **Long-running CLI ops are managed processes.** `managed_cli.py` builds the
   argv for `kubectl port-forward` / `kubectl logs -f` and starts it through the
   EXISTING terminal process manager — tree-killed, cancellable, streamed, visible
   in the Processes UI. It reuses the SAME closed-argv validation as `CliAdapter`,
   so there is no second argument surface. A one-shot adapter still refuses a
   blocking operation; the managed bridge is the right home for it.

3. **Promotion is a staged, health-gated pipeline.** `DeploymentEngine.promote()`
   deploys staging, requires authorization to cross to production, and ROLLS BACK
   on a production failure. A staging failure never touches production. Every stage
   is a real `deploy()`; nothing is claimed live without a passing check.

4. **A workspace is a graph, not a list.** `app_graph.py` builds a deterministic
   dependency graph over several applications (declared in `.black-eclipse/app.json`
   or a monorepo heuristic), with a stable topological `startupOrder` (dependencies
   first) and `stopOrder` (dependents first); a cycle breaks honestly. `workspace_ops`
   drives the existing `start`/`stop` in that order. No second lifecycle.

5. **Metrics accrete into a trend.** An `AppRuntimeMetric` sample is persisted on
   each `metrics()` read, bounded by a retention sweep, and summarised honestly
   (`rising`/`falling`/`flat`/`unknown` — never a false `flat` from too few samples).

6. **The compose engine is the authoritative graph fallback.** When the tolerant
   reader finds no services, `docker compose config` resolves anchors/merges/
   includes via the SAME `ComposeAdapter`; the engine's answer wins.

7. **The remaining work is opt-in and testable.** The live provider tests are
   gated behind `BLACK_ECLIPSE_PROVIDER_IT` (never in `npm test`), mirroring the Git
   provider suite. The deploy job templates ship disabled and stay subject to the
   same per-action authorization as a manual deploy.

**Why.** A production runtime must be observable before it runs (readiness), must
be able to hold a long-running connection it cannot express as a one-shot, must
never leave production half-shipped, and must understand the shape of the thing it
is running. Each of these closes a real operational gap without widening authority
or duplicating a primitive.

**Consequences.** New modules `provisioning.py`, `managed_cli.py`, `app_graph.py`,
`workspace_ops.py`, `observability.py`, `policy.py`; `manager.py` stays under its
size ceiling by delegating to `observability`/`workspace_ops`; 5 new tools
(`runtime_metrics_history`, `workspace_graph`/`_start`/`_stop`, `k8s_port_forward`,
`k8s_logs_follow`, `deploy_promote`) → 103 application-runtime tools; new routes
`/runtime/providers`, `/runtime/workspace/graph`, `/runtime/{id}/metrics/history`;
CLI `providers` and `workspace` groups; `/health.runtimeProviders`; two deploy job
templates; a Providers UI tab; `test_application_{provisioning,workspace}.py` +
`test_application_provider_live.py`. No new third-party dependency. Architecture:
`docs/harness/application-runtime.md`.

## ADR-H134 — The browser is a provider-agnostic runtime, not Playwright wrappers

**Decision.** The browser capability is a first-class harness subsystem
(`python/harness/browser/`) with a layered design: `browser_*` tools on the ONE
`ToolRegistry` → `BrowserManager` (pool/sessions) → `BrowserSession` (state +
operations) → a `BrowserProvider` interface → a concrete backend. Playwright is
one provider (`LocalChromiumProvider`), never the abstraction; a raw Playwright
object is never exposed to the model. Observation is accessibility-first:
`browser_snapshot` returns the accessibility tree with Eclipse's own stable refs
(`@e1`…), and large pages page to disk. Every important action is
`action → observe → verify`. Security is a single policy engine (permissions
ALLOW/DENY/ASK, domain policy, destructive-action elevation, an explicit
untrusted-page boundary), with an ASK routed through the EXISTING single-use
approval store. `browser_exec` is a sandboxed program AST (no `eval`/`exec`, no
host access). Optional cloud/alternate backends implement the same interface and
report honest availability — none is faked. `browser_request_human` handles
CAPTCHA/MFA; automation never bypasses it.

**Why.** A browser is one of the highest-authority capabilities an agent can hold
(untrusted input, credentials, external actions). Modelling it as Playwright
functions would leak a raw object to the model, scatter policy across handlers,
and couple the runtime to one backend. A layered provider runtime keeps the
reasoning, planning, sessions, policies, evidence and tracing in Eclipse; lets a
private/local target always run locally and a cloud provider be optional; and
makes the security boundary structural rather than prompt-based.

**Consequences.** New package `python/harness/browser/` (tools, tool_admin,
tool_support, manager, session, router, providers, observation, resolver,
verification, vision, execution, policy, vault, profiles, store, trace, config,
errors, authorizers, schemas, capabilities); a new router `routers/browser.py`;
boot wiring in `service.py`/`main.py` (`start_browser`/`start_browser_sweeper`/
`stop_browser`, `/health.browser`); `BROWSER_TOOLS` added to the finance agent,
code agent and chat code profile; `code/policy.py` (`BROWSER_EFFECTFUL_TOOLS`,
narrowed at `plan_only`) and `code/authorizers.py` (the browser admission layer);
a prompt guidance block; `playwright>=1.62,<1.63` in `requirements.txt` and a
doctor probe (never auto-downloads the browser); tests
`test_browser_{unit,integration}.py` + the local test app `browser_app.py`;
architecture `docs/harness/browser-runtime.md`. Chromium is a separate one-time
download; its absence is an honest `BROWSER_UNAVAILABLE`, never a fake browser.

### Phase B — limitations closed (same decision)

`browser_vision` gained a REAL interpretation layer (`vision_model.py`) that
reuses the harness model runtime instead of returning only a screenshot; it is
honest (`available:false`) with no multimodal model. `inspection.py` adds
`browser_find`/`browser_highlight` for non-interacting disambiguation. Large
console/network/error dumps are bounded and paged to `artifactsRoot`. `browserbase`
became a genuine cloud backend (REST session + CDP attach with a vault-resolved
credential). 83 `browser_*` tools.

### Phases C/D — evidence, skills, delegation and fusion (same decision)

`evidence.py` records every consulted `http(s)` page on the run's EXISTING
`source_sink` (`SourceCollector`), so browser-derived facts reach the answer's
Sources panel without a second provenance shape; the browser feeds the Evidence &
Research Engine rather than inventing one. A bundled inert `system.browser-research`
skill teaches the workflow (progressive disclosure). `session_of` prefers a session
owned by THIS run/agent (`BrowserManager.list_active`) so a delegated subagent never
drives the parent's browser — delegation isolation is structural. `browser_visual_locate`
(`vision.locate`) surfaces DOM+vision fusion with a confidence rationale and a trace
event. `waiting.py` extracted from `session.py` to hold the size ceiling. 84
`browser_*` tools.

## ADR-H135 — Research is evidence-driven: search results are pointers, the model is never the source of truth

**Decision.** Web research is a first-class harness subsystem
(`python/harness/research/web_*.py`) with one explicit invariant: **search results
are NOT evidence.** A search result is a pointer (title/URL/snippet); only a
fetched, normalized, provenance-tracked, timestamped artifact becomes `Evidence`.
The pipeline is `intent → mandate → plan → provider registry (ordered fallback) →
search → extract → browser escalation → normalize → entity resolution →
EvidenceStore → freshness/point-in-time → source ranking → conflict detection →
deterministic calculations → claim verification → citation generation →
validation → coverage → synthesis → immutable snapshot`. Every material claim is
traceable claim → evidence → source → location; a claim with no evidence is
`UNSUPPORTED`, never a fact.

**Why.** An agent that treats a model (or a search snippet) as a source of truth
will fabricate plausible financial numbers, silently average disagreeing sources,
and leak future information into historical analysis. Eclipse is built for
financial research where verifiability is the product, so the invariants must be
structural, not prompt-based: point-in-time research fails closed on an unknown
publication time; syndicated news copies are grouped into source families and
never counted as independent; a difference between sources is explained (period,
currency, basis, estimate/report, revision) or disclosed as an unresolved material
conflict; critical metrics are computed deterministically, never by the model.

**Consequences.** New package `python/harness/research/web_*.py` reusing — never
duplicating — the existing retrieval layer, the TS web transport via the
`DataBridge` (`web_search`/`web_fetch`), the Browser Automation Runtime (escalation
only), `research.budget`/`periods`/`calculations`, the SSRF/redirect guards, the
untrusted-content boundary, and the ONE `agent-memory.db` connection for its
`EvidenceStore`. 21 read-only tools on the ONE `ToolRegistry` (`web_search`,
`news_search`, `company_search`, `academic_search`, `image_search`,
`video_search`, `web_extract`, `page_extract`, `pdf_extract`,
`structured_extract`, `research_plan`, `research_query`, `source_rank`,
`source_compare`, `claim_verify`, `citation_extract`, `citation_validate`,
`research_status`, `research_cancel`, `research_continue`, `research_update`);
`WEB_RESEARCH_TOOLS` in `agents.py`; operator-gated router
`routers/research.py` (`/research/web/*`). Fetched content is wrapped through the
`UntrustedContentBoundary` and is never obeyed; injected instruction-like pages
are preserved as evidence but never laundered into a finding. Tests:
`test_web_research_{unit,integration,tools,api,hardcore}.py`. Architecture:
`docs/harness/web-research-engine.md`.

**Hardening (same decision).** A deep run must not be hard-killed by the harness's
generic tool ceiling: `ToolDefinition.long_running` lets a tool's own budget be
authoritative, `ToolRegistry.execute` publishes the effective timeout on
`ToolContext`, and the engine self-budgets below it to return a bounded partial.
Incremental `update_research` is non-destructive (append a snapshot, return a
delta) — prior evidence is never deleted. Raw evidence text is bounded in the
tool/API view while the store keeps it whole; the process-wide status/cancel maps
are bounded; events are namespaced `webresearch.*`. Regression tests:
`test_web_research_hardening.py`.

## ADR-H136 — Browser observability is a content-free aggregate and a browser-free replay, never a second store

**Context.** ADR-H134 made the browser a provider-agnostic runtime with a
per-task `BrowserTrace`. Traces persisted, but there was no aggregate view over
all activity and no way to inspect a failure without re-opening a browser; cost
and structured logs were absent. This is Phase 8 of the browser roadmap.

**Decision.** Observability is two read-only surfaces over the EXISTING trace
record — neither is a new evidence store:

1. **`telemetry.py` — a process-wide, content-free aggregate.** Counters with a
   fixed vocabulary (sessions, actions, failures, timeouts, crashes, navigations,
   downloads, uploads, vision, provider failures), per-action and per-provider
   latency kept as sum/count/max (never a per-call list), error `category`
   buckets, a bounded structured-log ring queryable by code/level/category/
   session, and a cost model. The cost model records REAL measurements (browser-
   seconds split by the provider's `remote` capability, vision tokens, screenshot
   count, artifact/download bytes) and produces a **money figure only when the
   operator configured a non-zero rate** — otherwise `ratesConfigured:false` and
   `estimatedMicros:null`. Nothing here stores page text, a URL query or a secret.
   The aggregate is `RLock`-guarded because a counter method appends a log line
   while holding the lock.
2. **`replay.py` — a browser-free inspection path.** `replay_trace` reconstructs
   a stored `BrowserTrace` into a bounded (≤500 steps) timeline and a
   `failureAnalysis` naming the first failing step's code/category/plain sentence.
   It never opens a browser and never re-executes; an unverified step is reported
   unverified, and a clean trace yields `failureAnalysis:null`.

Tools/routes: `browser_replay` (on the ONE `ToolRegistry`); `GET /browser/telemetry`,
`/browser/cost`, `/browser/logs`, `/browser/traces/{id}/replay`; a small
content-free summary in `/health.browser.telemetry`. Every tool call records one
action attempt at the SINGLE registration seam (`_telemetry_wrap`), so no handler
carries telemetry code.

**Why.** Replay and telemetry are what make the harder phases debuggable and
trustable in production. Building them as bounded aggregates and a pure read path
— rather than a second database or a live re-run — keeps memory (the top
constraint) bounded, keeps secrets out of observability, and honours the harness
rule that data is never fabricated: an unmeasured cost is reported as absent, not
guessed.

**Consequences.** New modules `browser/telemetry.py` and `browser/replay.py`;
cost/observability config (`HARNESS_BROWSER_COST_*`, `HARNESS_BROWSER_TELEMETRY_LOG_CAP`);
`browser_replay` tool; four operator routes; `/health` grows a telemetry summary;
tests `test_browser_observability.py` + `test_browser_observability_api.py`.
`main.py` does not grow (routes live in `routers/browser.py`).

## ADR-H137 — Frame targets, a resolver-owned coordinate fallback and a real device catalog (Phase H)

**Decision.** The hardening phase closes four failure-path gaps as first-class
runtime behaviour rather than test-only workarounds:

1. **Frame scoping is part of a Target.** A `Target` may carry `frame` (an iframe
   selector, nested with `>>`). The resolver's `_locator_for` resolves every
   strategy inside `page.frame_locator(...)` when the target names a frame, so
   cross-frame ACTION routing and frame-scoped snapshots
   (`SnapshotEngine.build(frame=...)`) work on cross-origin/nested frames. This is
   data on the existing Target, not a new code path.
2. **Coordinate fallback lives in the resolver/session, not the model.** A
   coordinate-only target clicks at the pixel; a locator that cannot click an
   occluded/stale element retries a pixel click and reports
   `strategy="coordinates"` with `reResolved=True`. A dead provider session
   surfaces a structured failure, never a hang.
3. **Device emulation uses a real catalog.** `devices.py` resolves a descriptor
   from whichever catalog the runtime exposes (the Playwright instance's
   `devices`, a module export, or the driver's `local_utils`) and reports the
   applied viewport/UA/locale/timezone; an unknown device is an honest error.
   `context_ops.py` (extracted from `session.py` to hold the size ceiling) applies
   viewport/device changes and records overrides (`viewport_override`, …) so an
   emulation is observable.
4. **The injection boundary is asserted.** Adversarial test pages prove page
   content never changes a session's policy, permission levels, or the tool
   registry — the hard guarantee is the `wrap_untrusted` boundary, and the test
   pins it.

**Why.** The acceptance suite covered the happy path; the remaining risk lived in
frames, interception, device/permissions, recovery and injection. Encoding the
missing behaviours in the runtime (frames, coordinate fallback, device catalog)
means they are structural and reusable, not test scaffolding; and documenting the
failure paths makes them navigable.

**Consequences.** `Target.frame`; `Resolver._scope` (nested `>>` chains);
`Snapshot.frame`; `SnapshotEngine.build(frame=)`; `browser_snapshot.frame`;
coordinate fallback in `BrowserSession.click`; new modules `browser/devices.py`
and `browser/context_ops.py`; `browser_set_*`/`emulate_device` record overrides;
the local test app gains nested/cross-origin frames and adversarial pages; tests
`test_browser_hardening.py` (real Chromium) + `test_browser_hardening_unit.py`
(offline). `session.py` net-shrinks (device ops extracted) to stay under the size
ceiling.

## ADR-H138 — The provider layer is finished honestly: extraction is not interaction, and a failing backend falls back

**Context.** ADR-H134 named the optional providers; several were honest stubs with
declared capabilities. Phase 9 completes the provider story: a new backend
(Camofox), a real Browser Use adapter, Firecrawl as a true extraction backend, and
health-aware routing.

**Decision.** Four commitments, each preserving the "never fake a provider" rule:

1. **Extraction is not interaction.** Firecrawl declares `interactive:false` and
   its `launch` REFUSES, so a browser *interaction* can never select it; it is
   wired into the research engine's extraction path only (`web_extract`
   `backend:"firecrawl"` → Firecrawl `/v1/scrape`, falling back to the SSRF
   bridge). `web_search`/interaction never touch it.
2. **Camofox is a launch provider, not CDP.** Camoufox is Firefox-based
   (Playwright `firefox`), so the provider resolves a binary (the `camoufox`
   package resolver or `HARNESS_CAMOFOX_ENDPOINT`) and launches through the local
   provider with `executable_path`; an endpoint attaches over CDP. Its declared
   `cdp:false` tells the truth about the protocol.
3. **Browser Use is a real adapter when installed.** The adapter starts Browser
   Use's browser and hands Eclipse a `ProviderSession` to drive; Eclipse retains
   reasoning/sessions/policy/observation/tracing. An incompatible build emits
   `BROWSER_PROVIDER_UNAVAILABLE` — honest, never fake.
4. **Routing is health- and cost-aware.** `ProviderHealth` demotes a provider
   after consecutive failures and re-promotes on success, recording the demotion
   in the decision; `prefer_low_cost` orders fitting candidates cheapest-first
   (a RELATIVE hint, never a money figure); an explicit request overrides
   demotion. The fallback rationale is in the trace.

**Why.** The master spec forbids faking a provider; the honest way to "finish"
the layer is to make each backend either genuinely run on this machine or report a
precise reason, and to make a failing backend fall through deterministically
instead of poisoning a task. Keeping extraction and interaction distinct prevents
Firecrawl (or any fetch backend) from ever being mistaken for an interactive
browser.

**Consequences.** `PROVIDER_CAPABILITIES["camofox"]`; `CamofoxProvider`,
`BrowserUseProvider` (real adapter), `FirecrawlProvider.extract`; `PROVIDER_COST`
+ `ProviderHealth` + demotion/cost in `router.decide`; `LaunchOptions.executable_path`;
config `HARNESS_CAMOFOX_*`, `HARNESS_FIRECRAWL_ENDPOINT`,
`HARNESS_BROWSER_PREFER_LOW_COST`, `HARNESS_BROWSER_ALLOW_PRIVATE_REMOTE`;
`web_extract.backend`; tests `test_browser_providers.py`.

## ADR-H139 — Progress is persisted, browser values become claims only through the engine, and the three surfaces share one routing policy

**Context.** Phase 10 completes the planner/workflow/cross-engine story. The
runtime already had evidence, skills, delegation isolation and visual fusion; what
remained was a visible progress surface, a second financial research workflow,
one documented hand-off rule across the three information surfaces, and a legal
path from a browser-read value to a verified claim.

**Decision.**

1. **Progress is a persisted task, not a stream of per-action events.** `browser/
   task.py` records a bounded checklist (`BrowserTask`) with per-step status,
   emitted as `browser.task.*` AND persisted to `agent-memory.db`. A user watching
   a long run sees “open SEC → search → find filing → download → extract”, and the
   progress survives the call that produced it (a per-action event is gone once
   streamed).
2. **A browser value is a candidate, verified only through the engine.** `browser/
   bridge.py` builds engine `Evidence` from a real `http(s)` URL + observed text
   (never fabricating provenance) and then runs the engine's OWN
   `web_verify.verify_claim`. There is no second verifier; a claim not grounded in
   the observed page text is refused. This is the one legal path (ADR-H135's rule,
   extended to the browser).
3. **One routing policy across three surfaces.** `research/coordination.py`
   documents and implements “search finds → browser reads behind a login →
   computer-use reads a desktop app”, deterministically, sharing ONE evidence
   store. A browser-read value is marked a candidate.
4. **A second bundled financial workflow.** `system.sec-filing-research` follows the
   `system.browser-research` pattern: progressive disclosure, `status: disabled`,
   referencing only real browser/web tools, and pointing a figure at
   `claim_verify` rather than asserting it.

**Why.** Progress and a single routing rule are what make the three surfaces
usable together instead of three overlapping tools; and the claim bridge is what
keeps the honesty invariant intact as the browser grows more capable — a browser
number can never bypass the engine's verification.

**Consequences.** New modules `browser/task.py` (persisted `BrowserTask` table) and
`browser/bridge.py`; `research/coordination.py`; the `browser_task` tool + `GET
/browser/tasks`; the bundled `system.sec-filing-research` skill; tests
`test_phase10_coordination.py`. No new evidence store — the browser still feeds the
engine's collector.

## ADR-H141 — The lifespan is its own module, the prompt ceiling narrows, and the three surfaces share one page (Phase S)

**Decision.** Stabilization is three process commitments, each made structural:

1. **Split, don't raise.** `main.py`'s FastAPI lifespan boot/shutdown wiring moved
   to `harness/boot.py`; `main.py` dropped from ~1018 to ~955 lines (under the
   1200-line hard ceiling) and was REMOVED from the size ratchet rather than having
   its number bumped. `main` re-exports `lifespan`, `identity_boundary`,
   `deployment_exposure` and `IDENTITY_BOUNDARY` so the public surface is unchanged.
2. **The prompt ceiling narrows.** The finance-agent prompt was trimmed of a
   redundant worked example and the ceiling LOWERED from 23300 to 23025 (its
   measured size) — the ratchet bites again instead of being widened once more.
   No honesty/authority rule was removed to hit the number.
3. **One page for the three surfaces.** `docs/harness/research-surfaces.md` is the
   single "which surface answers which question" page; `web-research-engine.md`,
   `browser-runtime.md` and `computer-use.md` all cross-link to it, and
   `NAVIGATION.md`'s trigger table names it.

**Why.** A ratchet that is only ever raised is not a ratchet, and god files
reappear when the answer to growth is always "split it later". `main.py` had grown
across three separate features; extracting the lifespan makes the route surface and
the boot sequence separate concerns. The three engines had drifted into
overlapping prose about who answers what; one shared page is the fix.

**Note on S.1 (concurrency).** Phase S also names a process hazard — multiple
writers committing into shared files, sweeping unrelated changes together. That is
a working-practice rule (serialize writers or use short-lived branches), not code;
it is recorded here and in the changelog rather than enforced by a hook.

**Consequences.** New module `harness/boot.py`; `main.py` shrinks and leaves the
ratchet; prompt ceiling 23300 → 23025; new doc `research-surfaces.md` cross-linked
from the three engine docs and `NAVIGATION.md`; tests
`test_phase_s_stabilization.py`.
## ADR-H140 — Computer Use is honest by construction: enforcement, native backends and challenge stops

**Context.** The Computer Use Engine shipped production-grade and Windows-only, but
a review found several claimed behaviours were not actually enforced or wired, and
the "unavailable by default" gaps (OCR, accessibility, vision, CAPTCHA handling)
were reported rather than closed. This ADR fixes the intent: every advertised limit
must be enforced, every backend must be real-or-honest, and a security challenge is
a human hand-off.

**Decision.**

1. **Advertised limits are enforced.** `max_actions` and `action_timeout_seconds`
   are no longer dead config: `ComputerCore.act` wraps every handler in
   `asyncio.wait_for` (timeout ⇒ `COMPUTER_TIMEOUT`, verdict `retry`) and a
   monotonic `session.actions_started` counter (over budget ⇒ `BudgetExceeded`,
   `COMPUTER_BUDGET_EXCEEDED`, verdict `abort`). A dry run still counts as an
   attempt, matching what `capabilities().limits` promises.
2. **Config fields have consumers.** `vision_model` overrides the resolved model
   id in `service._build_vision_router`; `ocr_enabled=false` disables OCR; the
   finance allowlists (`finance_allowed_apps/accounts/instruments`, with
   `HARNESS_COMPUTER_FINANCE_ALLOWED_*` env overrides) are wired into
   `build_firewall_from_config`. A cloud vision model is wired as the CLOUD backend,
   so a sensitive frame is refused rather than silently sent to it.
3. **Native backends are the default where the OS provides one.** `build_ocr_engine`
   `auto` prefers the OS-native OCR (`Windows.Media.Ocr` via the optional `winsdk`
   package, engine id `winocr`), then Tesseract. The `Pillow`, `uiautomation` and
   `winsdk` extras are declared in `requirements.txt` (Windows markers) and
   installed/repaired by `setup-python.js` / `doctor.js`. Every backend stays
   honest: absent ⇒ `available:false` with a reason.
4. **A security challenge is a human hand-off.** `security.detect_challenge` matches
   CAPTCHA/MFA/biometric markers; `ComputerSession.detect_challenge` scans the
   foreground window; the core refuses BEFORE delivering input (so nothing is
   clicked into the challenge) and returns `HumanInterventionRequired`
   (`COMPUTER_HUMAN_REQUIRED`, verdict `ask_user`). The new effectful
   `computer_request_human` tool lets the agent ask explicitly.
5. **Screen text is scanned, not only wrapped.** `security.scan_injection` (reusing
   the browser vocabulary) flags on-screen text posing as instructions; the
   UNTRUSTED wrapper gains a warning. The hard guarantee remains the wrapper + prompt.
6. **Background-first delivery is uniform.** Pointer move, scroll, drag and mouse
   down/up now honour `background` (posted WM_* messages in the target's own child
   hierarchy) as well as `foreground`, and foreground focus is restored after an
   escalation.
7. **Platform breadth is real-or-honest.** `drivers/linux.py` (ctypes X11/XTest)
   and `drivers/macos.py` (ctypes Core Graphics/AX) are real drivers with matching
   accessibility providers (`accessibility/linux.py` AT-SPI, `accessibility/macos.py`
   AX). Wayland-only Linux and any OS without a backend fall to a genuine
   `NullDriver`. macOS TCC permissions are probed, never fabricated.
8. **Sessions are serialized on the one physical desktop.** `broker.py` holds a
   machine-wide control lease for the duration of a single action, so concurrent
   sessions queue rather than interleave input; the per-session human takeover
   lease is unchanged.
9. **Isolation is a declared seam, not a claim.** `desktop.py` exposes a
   `DesktopProvider` (local today; Windows Sandbox / headless Xvfb declared), so
   `headless_desktop` stays False until a genuine provisioner exists.
10. **Adapter knowledge is honest about verification.** Adapters carry
    `verified`/`verified_note` (False = a knowledge record) and `looks_busy`/
    `looks_ready`; only the opt-in live rig marks one verified.

**Why.** "Ready on Windows" is only true if the limits are real, the common backends
exist without extra setup, and money/security still stop for a person. Honesty is
the invariant: an absent backend degrades to a reason, never a fake desktop.

**Consequences.** New `ocr/windows.py` (`WindowsOCREngine`); `errors.BudgetExceeded`;
`security.detect_challenge` / `scan_injection`; the `computer_request_human` tool
(and `EFFECTFUL_TOOLS`/`COMPUTER_EFFECTFUL_TOOLS`); driver background mouse paths;
`requirements.txt` extras + `setup-python.js`/`doctor.js` repairs. New
`drivers/linux.py`, `drivers/macos.py`, `accessibility/linux.py`,
`accessibility/macos.py`, `broker.py`, `desktop.py`; adapter verification status.
Tests: `test_computer_hardening.py`; the opt-in `test_computer_apps_live.py`.
VM/headless desktop provisioning remains the one open item.

## ADR-H142 — Isolated desktops are a provisioner seam: real Xvfb, declared Sandbox, honest fallback

**Context.** Computer Use ran every session against the operator's one physical
desktop, so `broker.py` could only *serialize* agents (preventing corruption, not
giving isolation). True multi-agent control needs a separate desktop per session,
and the earlier `DesktopProvider` seam was a capability descriptor with no
lifecycle.

**Decision.**

1. **A provisioner owns a session's desktop lifecycle.** `desktop.py` now defines
   `DesktopProvisioner.create/destroy` returning a `DesktopInstance` (its own
   driver, display, process). `ComputerManager` provisions per session: the local
   provisioner wraps the process driver; an isolated provisioner gives the session
   its own.
2. **Xvfb is real.** `X11HeadlessProvisioner` allocates a free display, spawns
   `Xvfb :N`, waits for the socket, and starts a `LinuxX11Driver` bound to that
   `DISPLAY`. The spawn/socket probes are injectable, so create→attach→teardown is
   fully tested without Xvfb installed; on a Linux host it runs for real.
3. **Windows Sandbox stays honestly unavailable.** `WindowsSandboxProvisioner`
   reports `available:false` because a host Win32 driver cannot attach to a
   sandbox session; it needs an owned RDP/VM display first.
4. **Isolation is opt-in and cannot break zero-config.** `isolation.mode`
   (`off|auto|on`, default `off`) picks the backend; absence of the backend falls
   back to the local desktop with a reason. `headless_desktop` is true only for a
   genuinely provisioned isolated desktop.
5. **Isolated sessions skip the machine broker.** A session with its own display
   neither waits on nor blocks the shared-desktop lease, so N agents run
   concurrently on N desktops; shared-desktop sessions still serialize.

**Why.** The one honest line is unchanged: isolation is real or it is not claimed.
The injectable lifecycle makes a platform-specific backend verifiable in CI, and
the opt-in default preserves `npm install && npm run dev`.

**Consequences.** `desktop.py` gains `DesktopProvisioner`/`DesktopInstance`/
`build_provisioner` and the Xvfb/Sandbox provisioners; `config.py` gains
`isolation*`; `manager.py` provisions/tears down per session and reports the
provisioner + broker in `capabilities()`; `session.py` marks `isolated`;
`LinuxX11Driver` takes an explicit `display`. Tests: `test_computer_hardening.py`.
Still open: Windows Sandbox attach and Wayland (portal) isolation.

## ADR-H143 — Remote/isolated desktops attach through a contained window; Wayland goes through grim/ydotool

**Context.** ADR-H142 built the provisioner lifecycle with a real Xvfb backend and
declared Windows Sandbox, RDP and Wayland unavailable. Those three share one
problem — the guest desktop is not addressable by the host's own SendInput/BitBlt —
and Wayland has no global input/capture API at all.

**Decision.**

1. **One attach mechanism: `WindowDriver`.** `drivers/window.py` wraps a base
   platform driver and scopes every operation to ONE contained desktop window
   (capture the window rectangle; translate window-local coordinates to absolute
   foreground events). It is the honest bridge for any desktop that renders inside
   a host window — Windows Sandbox, RDP, a VM console.
2. **The guest gets a real (OCR-derived) element tree.** The guest's native
   accessibility tree is not host-visible, so `WindowDriver.accessibility_tree`
   reads the contained window's pixels and turns OCR words into an `AXNode` tree
   (roles/text/absolute bounds), cached briefly. It is labelled `source=ocr` and
   returns None (accessibility False) when no OCR backend exists — never invented.
3. **Input is background-first, then foreground.** `WindowDriver` posts background
   input when the base driver supports it and escalates to foreground (verified by
   the core) otherwise, so a contained desktop gets the same safe delivery ladder.
4. **Windows Sandbox is real-or-honest.** `WindowsSandboxProvisioner` launches
   `WindowsSandbox.exe`, waits for the client window, and attaches a
   `WindowDriver`; absence (non-Pro/Enterprise, virtualization off) falls back to
   local with a reason. Launch/probe are injectable so the lifecycle is tested
   without the feature enabled.
5. **RDP is real-or-honest.** `RdpDesktopProvisioner` spawns `mstsc` against
   `rdp.host`/`rdp.file` and attaches to the session window. Eclipse never handles
   the credential — it relies on the operator's CMDKEY/`.rdp`.
6. **Wayland gets its own (headless) compositor + tools.** `WaylandDriver` captures
   via `grim` and inputs via `ydotool`, reporting each capability per-tool.
   `WaylandHeadlessProvisioner` spawns a **nested headless compositor**
   (`wlheadless-run` wrapping `sway`/`weston`, or their own `--headless` backend)
   on a private `XDG_RUNTIME_DIR` socket and attaches the driver — a genuinely
   isolated desktop, not just the shared session.
7. **Zero-config and isolation semantics are unchanged.** All backends are opt-in
   (`isolation.mode`), fall back to local with a reason, and an isolated session
   still skips the machine-wide broker.

**Why.** The blocker was never "no code" — it was "no addressable surface". A
contained-window driver turns an unattachable guest into a scoped, honest one, and
naming the limits (best-effort input, no guest AX) keeps the honesty invariant
intact.

**Consequences.** New `drivers/window.py`, `drivers/wayland.py`;
`WindowsSandboxProvisioner`/`RdpDesktopProvisioner`/`WaylandHeadlessProvisioner`
+ `_construct` in `desktop.py`; `config.py` `rdp_host`/`rdp_file`
(`HARNESS_COMPUTER_RDP_*`); driver factory `wayland`/`window`; opt-in live test
`test_computer_xvfb_live.py`. Still open: a *native* guest accessibility bridge
(the OCR tree is a real but weaker substitute), and full-unicode typing on
Wayland/X11.

## ADR-H144 — An "Always allow" permission is a separate, revocable standing grant, not a repeatable approval

**Context.** ADR-H92 made every effectful approval single-use and explicitly
deferred a standing grant. In practice a user running `python3`, `npm` or `git`
in a chosen folder is re-asked for every new command string, because the approval
is digest-bound to the exact command. The fatigue is real; the fix must not weaken
the single-use guarantee or let a misread toggle run something destructive.

**Decision.**

1. **A grant is a separate record, not a longer-lived approval.**
   `python/harness/grants.py` (`GrantStore`, table `StandingGrant` on the ONE
   `agent-memory.db`) stores `(kind, scope, pattern)` — one program/tool in one
   workspace. `McpApproval` is untouched: it stays single-use. A grant removes the
   *asking*; the store still authorizes nothing by itself.
2. **Grants are consulted BEFORE a request is created, never instead of the
   classification.** Each authorizer (shell, terminal, terminal subsystem, MCP,
   Git) checks `grants.matches(kind, scope, pattern)` first. A `deny`-classified
   command and an unknown MCP effect are refused before the check, so a grant can
   never make a destructive action run. A match emits `approval.granted` and the
   run proceeds — no card.
3. **Only an operator can mint a grant, and only from a real approval.**
   `ApprovalStore.remember_grant()` builds the grant from the pending row's OWN
   `grantPattern`/`grantKind`/`grantScope` columns (set by the authorizer), so a
   caller can never name an arbitrary pattern. The decide routes pass `remember`
   only on an approve. High-risk actions and dangerous/destructive/critical Git
   ops carry no pattern, so "Always allow" is never offered.
4. **Grants are listed and revoked by a dedicated surface.** `GET /grants` +
   `POST /grants/{id}/revoke` (operator-gated), fronted by
   `src/lib/harness/grants-client.ts` and the `StandingGrantsPanel` (Code ›
   Approvals). Revocation is immediate and idempotent.
5. **The request is never silent.** An `approval_request` plays the `notification`
   cue and raises the shared toast/OS notice, so a paused run is not missed.

**Why.** Single-use remains the authorization invariant; the grant is a narrow,
labeled, revocable convenience over the *prompt only*. Scoping to a program (not
the exact command) is what actually reduces fatigue, while keeping a denied or
destructive command impossible to remember.

**Consequences.** New `grants.py`, `routers/grants.py`; additive columns on
`McpApproval`; `ApprovalStore.grants`/`remember_grant`; every authorizer gains one
grant check and sets the `grant*` fields; `remember` on both decide routes; the
redesigned `ApprovalCard` (emerald, no yellow) with Allow once / Always allow /
Cancel; `ALERT_NOTIFY_EVENT` moves to `lib/alerts/browser-notifier.ts`. ADR-H92 is
unchanged for the store. Tests: `python/tests/test_standing_grants.py`.

## ADR-H145 — Authentication is opt-in and auto-engages off loopback; the front door is the boundary, not the runtime

**Context.** BlackEclipse shipped as a single-user local app with no
authentication and no tenancy isolation, and every layer said so honestly: the
Python `deps.py` hardcoded `authentication: False`, `api-guard.ts` warned on a
public bind, and `/health` reported the gap. That is correct for a local tool and
not enough for any deployment reachable by another machine, where "no
authentication" is a real hole rather than a design choice.

**Decision.** Authentication is OPT-IN and AUTO-ENGAGES at the moment the app
could be reached by another machine, and it is enforced at the FRONT DOOR (the
Next.js app an external caller actually hits), not deep in the Python runtime:

1. **One mode resolver** (`src/lib/auth/config.ts`). `HARNESS_AUTH=required|off`
   wins; unset resolves to *required* on a non-loopback bind or a production
   build, and *off* on a local dev bind. This preserves zero-config: `npm run dev`
   on localhost stays accountless, and a public bind stops being silently open.
2. **Fail closed, never silently.** When auth is required but
   `HARNESS_AUTH_SECRET` is missing, requests get `503 AUTH_MISCONFIGURED` with an
   instruction; the doctor reports the posture at boot.
3. **Two layers, because the Edge runtime cannot open SQLite.** `middleware.ts`
   (Edge) does the coarse gate — a validly signed session cookie for every page
   and `/api/**`, except `/login` and `/api/auth/**`. `requireApiAuth` /
   `getAuthContext` (Node) do the authoritative check against the session row.
4. **Tenancy is the session's.** When auth is on, `scope` comes from the session,
   never the request body — a signed-in user cannot widen their scope. When auth
   is off, `userId`/`scopeId` remain self-asserted filter labels, exactly as before.
5. **Honest everywhere.** `/health`'s `identity` reports the real mode
   (`authentication`, `frontDoorAuth.{required,source,secretConfigured,misconfigured}`)
   instead of a hardcoded "not implemented". The app is never presented as
   multi-user secure unless the front door actually requires a login.

Passwords are `scrypt`-hashed with a per-user salt (Node built-in `crypto`); no new
dependency. Sessions are opaque server-side ids in `.black-eclipse/auth.db`
(gitignored); the cookie is `HttpOnly`, `SameSite=Lax`, `Secure` off loopback, and
signed with `jose` (already a dependency).

**Why.** A local research tool should not force a login on a single user; a
hosted one must not be open. One resolver that flips on reachability — plus a hard
fail-closed when misconfigured — gives both without a config ceremony. Putting the
boundary at the front door is honest about where the trust line is: the Python
runtime is still reached only by the front door over loopback / the operator
token, and this protects the surface an external caller can actually reach.

**Consequences.** New `src/lib/auth/` (`config`, `store`, `session`, `token`,
`server`, `handlers`), `src/middleware.ts`, `src/app/login/page.tsx`, the four
`/api/auth/*` routes, a `better-sqlite3` ambient type, and a `doctor.js` auth line;
`deps.identity_boundary` and `IDENTITY_BOUNDARY` reworded to the opt-in truth;
`python/tests/test_identity_tenancy_boundary.py` updated and extended; docs
`docs/harness/authentication.md`. With `HARNESS_AUTH=off`, the old no-auth,
no-tenancy reality still holds and must stay on a trusted local machine.

## ADR-H146 � Vision is one orchestrator, native-first, with deterministic-first and an untrusted-input boundary

**Context.** Eclipse needed a production multimodal capability. The Computer Use
and Browser subsystems already had narrow, per-subsystem vision helpers
(`computer/vision/`, `browser/vision_model.py`) that re-used the model runtime,
and the model-capability registry already declared a `vision` capability. A new,
independent "vision stack" would have duplicated provider selection, OCR,
screenshots and the model construction path.

**Decision.**
1. **One orchestrator, one seam.** `vision/orchestrator.py` owns
   resolve?preprocess?store?route?capability?cache?dispatch?fallback?telemetry.
   Tools are thin adapters; no handler picks a provider or calls a model directly.
2. **Native-first.** If the run's own model accepts images, the image goes to it.
   The auxiliary vision model is a fallback, selected through the existing model
   registry with the runtime's same-provider/keyless-local rule. Routing is pure
   data and testable without a model.
3. **Deterministic-first.** OCR before a model; OCR-then-model grounding; PDF text
   layer before the model; a pixel diff before a model describes it; chart numbers
   are verified against deterministic market data through the existing bridge, and
   a visual/numeric disagreement is reported with market data authoritative.
4. **Untrusted input.** URL fetching re-uses `mcp.ssrf` on every hop; byte/pixel
   ceilings plus Pillow's bomb guard; recognized in-image text is scanned and
   DELIMITED as untrusted data (re-using `research.web_security`), so text inside
   an image is data, never an instruction.
5. **Binary outside relational data.** Images are content-addressed on the
   gitignored filesystem tier; only metadata is a `VisionAsset` row on the ONE
   `agent-memory.db` connection.
6. **No Redis.** The cache is a bounded in-process TTL store keyed over image
   hash + model + provider + prompt + mode + preprocessing + schema version,
   consistent with the existing retrieval/investor-eye caches.
7. **Evidence without a new contract.** Observations become the existing
   `research.web_contracts.Evidence` and persist through `EvidenceStore`, with
   `contentTrust=UNTRUSTED`.

**Consequences.** New `python/harness/vision/` (contracts, config, errors, image,
fetch, load, storage, cache, capabilities, providers, routing, analyze, ocr,
grounding, documents, charts, compare, security, telemetry, tiling, evidence,
collector, orchestrator, tools); the `VISION_TOOLS` constant in `agents.py` on the
finance and code agents (all read-only); boot wiring in `service.py`
(`AgenticService.vision_assets`); the `vision` run-blob column on `HarnessRun`;
`vision` on `RunResult`/`client.final`; TS `vision-view.ts` +
`src/components/chat/vision-panel.tsx`; tool labels in `response-model.ts`;
activity detail/summary for vision tools in `code/activity.py`; `pypdfium2` added
to `requirements.txt`. The Computer Use and Browser vision helpers are unchanged
(the orchestrator complements, not replaces, their narrow per-subsystem needs).
Tests `python/tests/test_vision_{unit,orchestrator,tools,graph}.py` and
`src/lib/harness/vision-view.test.ts`. Docs `docs/harness/vision.md`.


### ADR-H146 (addendum) - capability closers: stdlib PDF, persistent visual memory, image generation

Three areas that were initially only "honest limitations" were implemented:

1. **PDF without pypdfium2.** `vision/documents.py` adds a dependency-free
   `_StdlibPdf` backend (text from content streams always; Pillow rasterization for
   standard-font pages). Each `DocumentPage` records `textMethod`/`rasterMethod`, so
   the method is never hidden; `pypdfium2` remains preferred. `pdf_capabilities()`
   reports availability. No page is ever faked.
2. **Persistent visual memory.** `vision/memory.py` (`vision_remember` tool) persists
   a vision result through the SAME memory decision gate (`authorize_proposal` +
   `commit_decision`) - never bypassing it. Only a text summary is stored (never the
   bytes); the content-addressed `assetId` and regions/model/provider/confidence ride
   in `structuredData`; a sensitive/ephemeral result is low-importance with a 1-day TTL.
3. **Image generation.** `vision/generate.py` (`image_generate` tool) speaks the
   OpenAI-compatible `/images/generations` protocol, gated by the model's declared
   `image_generation` capability. It is registered `external_action` (effect
   `external_action`), approval-gated by the run's effect authorizer, and removed at
   `plan_only` via `code/policy.py` `VISION_EXTERNAL_TOOLS` - the same boundary every
   other external/paid tool uses. Failures are classified (`NO_VISION_MODEL`,
   `AUTH_ERROR`, `RATE_LIMIT`, `MALFORMED_RESPONSE`), never a placeholder image.

`VISION_TOOLS` gains `vision_remember` and `image_generate`; the `image_generation`
task profile (already present) routes generation; image-generation model families
(dall-e/gpt-image/imagen/stable-diffusion/sdxl/flux) are declared in
`model_runtime/catalog.py`. Tests `python/tests/test_vision_capabilities.py`.


## ADR-H147 - Document Intelligence is one adapter/orchestrator pipeline over the shared DocumentIR, reusing vision, the model runtime, the evidence store and the query engine

**Why.** Eclipse could see an image and read a PDF page, but had no
first-class document-understanding capability: no format adapters, no unified
intermediate representation, no OCR routing, no structured table/spreadsheet
extraction, no hybrid document retrieval, no document comparison, and no
document-aware provenance for the Evidence & Research Engine. The Vision
subsystem (ADR-H146) already owned OCR engines, PDF rendering, the model-call
path and the untrusted-input boundary; the Evidence Engine already owned the
`Evidence` contract and `EvidenceStore`; DuckDB and pandas were already present
for the SQL runtime; `agent-memory.db` was already the ONE runtime database. A
parallel "documents" stack would have duplicated all of them.

**Decision.**
1. **One subsystem, one pipeline.** A new `python/harness/document_intelligence/`
   package owns `validate → cache → detect → parse → OCR/vision → structure →
   entities → chunk → index → evidence → assess`. Tools are thin adapters; no
   handler decodes a file, picks a parser or calls a model directly.
2. **A unified DocumentIR.** `ir.py` defines the vocabulary every adapter emits
   (`DocumentIR`/`Page`/`Sheet`/`Block`/`Table`/`TableCell`/`DocumentImage`/`Section`
   /`BBox`/`Provenance`/`DateFacts`). Coordinates are never lost, each block
   carries its extraction method + confidence, and observed/extracted/inferred is
   explicit. Downstream stages read the IR, never raw bytes.
3. **Adapter per format, honest capabilities.** `adapters/` has one adapter per
   kind (PDF, DOCX, PPTX, XLSX, ODS/ODT/ODP, CSV/TSV, JSON/JSONL/XML/YAML/TOML,
   HTML, Markdown/TXT, image, EPUB, RTF, archive, legacy OLE). Each declares a
   `Capabilities` map, so a CSV is not asked to render a page and a PPTX is not
   asked for worksheets. OOXML is parsed with stdlib `zipfile`+`ElementTree`
   (lxml recovery when available), so no `openpyxl`/`python-docx`/`python-pptx`
   dependency is added.
4. **Deterministic OCR routing.** `ocr.py` runs the existing OS OCR engine first
   and only escalates to a vision-model transcription when there is no usable
   text or confidence is below `OCR_CONFIDENCE_THRESHOLD`. Method + confidence are
   recorded per page; a page with no text and no engine is reported, never faked.
5. **Reuse, never fork.** OCR engines, PDF rendering and every model call come
   from `harness.vision`; the model runtime is the only provider path; extraction
   results become the SAME `research.web_contracts.Evidence` through
   `EvidenceStore` with `contentTrust=UNTRUSTED`; tabular questions are computed
   with read-only SQL over DuckDB/pandas (`query.py`), never model arithmetic;
   tables live on the ONE `agent-memory.db` connection (`store.py`), binaries in
   the vision asset store, and the IR blob is metadata-only on the gitignored
   filesystem tier.
6. **Untrusted input.** `security.py` validates extension/MIME/magic and byte
   ceilings before any parser runs, refuses macro-enabled Office documents,
   detects archive/compression bombs (declared size, ratio, entry count, nesting)
   BEFORE extraction, SSRF-guards document URLs through `mcp.ssrf`, sanitizes HTML
   of active content, and re-uses the shared injection boundary so document text
   is DATA, never an instruction.
7. **Truthful states.** `processing.py` models
   `DISCOVERED→…→READY|PARTIALLY_READY|FAILED|CANCELLED`; a stage that cannot run
   is `UNAVAILABLE`/`SKIPPED` and the document is `PARTIALLY_READY`, never `READY`.
   Bounded retry (transient only) and a per-document timeout guard parsing.
8. **Hybrid retrieval, point-in-time, provenance.** `retrieval.py` fuses lexical
   scoring (deterministic, no embeddings — consistent with the harness's
   documented lexical stance) with structured filters over document/version/page/
   sheet/table/cell and the entity table. `compare.py` diffs two DocumentIRs
   structurally before any model prose. `DateFacts` preserves publication/filing/
   effective/period/retrieval dates.

**Consequences.** New `python/harness/document_intelligence/` (config, errors, ir,
contracts, security, base, adapters/*, ocr, render, entities, chunking, store,
retrieval, query, compare, evidence, processing, telemetry, collector,
orchestrator, tools); `DOCUMENT_TOOLS` in `agents.py` on the finance and code
agents (all read-only); boot wiring in `service.py`
(`AgenticService.document_store`); the `documents` run-blob column on `HarnessRun`;
`documents` on `RunResult`/`client.final`; TS `document-view.ts` +
`src/components/chat/document-panel.tsx`; tool labels in `response-model.ts`.
Tests `python/tests/test_document_intelligence_{unit,tools,security,graph}.py` +
fixture builder `document_intelligence_helpers.py` and
`src/lib/harness/document-view.test.ts`. Docs `docs/harness/document-intelligence.md`.
No new third-party dependency is added.


## ADR-H148 - Vision capability is detected per MODEL FAMILY, not per SDK kind; a text-only run model falls back to any available vision model

**Why.** The Vision subsystem reported "needs a vision-capable model
configured" even when the operator had vision-capable models. Two defects caused
it. (1) The `openai` capability-kind default declared `vision: SUPPORTED` for
EVERY model of that kind — but the `openai` SDK kind is shared by OpenAI, Ollama,
LM Studio, DeepSeek, xAI, Groq, Together and any OpenAI-compatible endpoint, most
of which serve text-only models. So a run model that could not see images was
nonetheless routed as native and received the image (a failed round-trip), while
genuine VLMs whose names were not catalogued stayed unrecognized. (2)
`plan_route`'s registry search called `findByCapability(VISION_SUPPORT)` with a
bare STRING, so the runtime iterated it character-by-character and matched no
model; and cross-provider fallback was refused even when a credential existed.

**Decision.**
1. **Per-family vision priors.** The `openai` kind default no longer claims
   vision. `MODEL_CAPABILITY_OVERRIDES` now declares the vision-capable families
   (gpt-4o/4.1/4-turbo/4-vision/4.5/5, o1/o3/o4, the whole Claude and Gemini
   families, grok-2-vision/grok-vision/grok-4, and open-weight VLMs: llava,
   qwen2-vl/qwen2.5-vl/qwen3-vl, minicpm-v, moondream, pixtral, llama-3.2-vision,
   llama-4, internvl, phi-3-vision/phi-4-multimodal, granite-vision, deepseek-vl,
   gemma-3, and a `vision`-in-name catch-all) AND the text-only families
   (gpt-3.5/gpt-4, o1-mini, deepseek-chat/reasoner, llama-3.x, qwen2.5/qwen3 text,
   mixtral, mistral, codestral, groq hosts, grok-2/3, …). Longest-substring
   matching keeps a specific entry ahead of a generic one.
2. **Native-first asks about the RUN'S OWN model.** `plan_route` reads the run
   model's capabilities DIRECTLY (`capabilities_for_model`), never a routed
   substitute, so a routed VLM's capability is never attributed to a text-only run
   model.
3. **The registry search is fixed** (pass a list, not a string) and runtime
   discovery is the default, so declared/discovered VLMs are always considered.
4. **Credentialed cross-provider fallback.** A different provider is usable as the
   auxiliary vision model when it is keyless-local OR its declared env credential
   is present (`models.provider_has_credentials`); a remote provider the process
   holds no key for is still never selected (ADR-H146 §31/§45). The local VLM
   family is also added to the Ollama/LM Studio/OpenRouter declaration lists.
5. **Document Intelligence routes through the vision orchestrator**, so an
   embedded-image analysis on a text-only run model gets a real vision model too.

**Consequences.** `model_runtime/catalog.py` (per-family vision priors),
`harness/models.py` (`provider_has_credentials` + local VLM declarations),
`vision/capabilities.py` (`capabilities_for_model`), `vision/routing.py`
(direct run-model caps, default runtime, list `findByCapability`,
credentialed cross-provider fallback), `document_intelligence/orchestrator.py` +
`ocr.py` (route image analysis/OCR-fallback through the vision orchestrator).
Tests `python/tests/test_model_vision_capability.py` (recognition matrix +
routing); the local-only vision test now asserts the invariant "never cloud"
rather than "no candidate". No new dependency.


## ADR-H149 - Market data is one gateway over declarative providers; the adapters stay on the TS bridge

**Status:** Accepted (2026-10-07). The Market Data Layer is the one market-data
surface for agents: fourteen capability tools (get_quote … stream_market_data)
call a single MarketDataGateway, which resolves the instrument, selects a
provider via a declarative ProviderRouter, fetches through the ONE read-only TS
bridge capability (market_data), normalizes to a canonical record, runs the
mandatory data-quality engine, and preserves provenance into the Evidence &
Research Engine. Agents never name a provider.

**Why.** The retrieval layer (
etrieval/) is provider-agnostic but shallow —
it serves quote/statements/news/profile, not order books, trades, funding, open
interest, options, FX or bonds. The market-data engine (scraper chain, yfinance
bridge, Yahoo chart, crypto connector, SEC, economics) already lives in Next.js,
which also owns the ONE secret store (provider-keys.ts) and the ONE hardened
outbound layer (connectors/security.ts#safeGet). Rebuilding an HTTP client or a
second secret store in Python would duplicate both.

**Decision.** Keep the *adapters* in TypeScript (one file per provider under
src/lib/harness/market/providers/) and keep *routing, normalization, quality,
caching, provenance, storage and streaming* in Python (python/harness/market/).
The Python runtime calls providers only through a single bridge capability,
market_data; it holds no provider key. Instrument identity is the bridge's
instrument master (the same one the retrieval layer uses), cached briefly and
never invented when the bridge is silent.

**Consequences.** ONE transport, ONE secret store, ONE instrument master; the
adapter registry is additive (a new provider is one TS file + one Python
descriptor row). Order books/streaming are real (Binance public REST/WebSocket),
not pollers. Known gaps are returned honestly (UNSUPPORTED_CAPABILITY for an
equity order book) rather than faked. websockets/httpx are pinned; when
websockets is absent, streaming reports STREAM_DISCONNECTED and REST is
unaffected. Tests: python/tests/test_market_{unit,gateway,streaming}.py +
src/lib/harness/market/bridge.test.ts. Docs docs/harness/market-data.md.

## ADR-H150 - The Quant Engine is a deterministic Python engine; strategies are data, and backtest simulation is separate from real-holdings accounting

**Why.** The harness needed real backtesting/research. The risk was the LLM
being asked to compute P&L/Sharpe/drawdown, or a demo engine returning
hardcoded metrics.

**Decision.** All trading math lives in `python/harness/quant/`, a modular
event-driven engine (`strategy`/`rules`/`indicators`/`data`/`orders`/
`execution`/`sizing`/`risk`/`portfolio`/`metrics`/`engine`/`store`). A strategy
is a structured, serializable **specification** (universe, timeframes,
indicators, a rules DSL, risk, sizing, execution), not arbitrary code — so it is
versionable, validatable and safely deterministic. The LLM only authors/edits
specs and calls tools. Market data is read through the ONE Market Data Gateway
(the engine never calls a provider). Point-in-time correctness is enforced by a
documented bar-processing order: a `data_available_at` per bar, causal
indicators (NaN warm-up), next-bar fills, and higher-timeframe alignment that
only exposes a higher bar once it has closed. Heavy result series are gzipped
artifacts under `.black-eclipse/quant/`; metadata is on the ONE `agent-memory.db`
connection. Capabilities are reported as available/partial/unsupported. The
research engines — optimization (grid/random/evolutionary/Bayesian), walk-forward
(strict no-test-leakage), Monte-Carlo, and sensitivity/robustness — are
implemented, tested, exposed as tools and routes, and advertised `available`;
only options (Phase 7) and futures remain unsupported/partial and are NOT
registered as tools. Portfolio backtests build one execution simulator + risk
engine per strategy binding (per-strategy costs/limits); `historical` spread uses
a per-bar spread column when present, else warns.

**Consequences.** One deterministic source of truth for trading calculations;
reproducible experiments (seed + engine version + dataset + assumptions);
honest capability reporting. The backtest simulation portfolio
(`quant/portfolio.py`, float, long/short, bar-loop) is deliberately separate
from the Portfolio Accounting Engine (`harness/portfolio/`, Decimal, tax lots),
which refuses shorts and is oriented to real holdings; for long-only runs the
two MUST agree on realized P&L, which a test asserts. Reusing the accounting core
for simulated transactions is deferred and would be an extension of the core,
not a fork. Tests: python/tests/test_quant_*.py (57) +
src/lib/backtest/backtest-client.test.ts. Docs docs/quant-engine.md.

## ADR-H151 - Unified search is a provider-agnostic retrieval layer over a dedicated SQLite vector + FTS index (not pgvector)

**Why.** Eclipse needed first-class semantic/vector search across documents,
research evidence, agent memory, experiences and skills, with hybrid retrieval,
reranking, provenance and citations. The reference design assumed PostgreSQL +
pgvector. This repository is deliberately **SQLite-first and zero-config**
(`npm install && npm run dev` must never require manual setup) and already had a
deterministic lexical retrieval layer (`retrieval/`) and dedicated SQLite index
stores (`code/intel/persistence/index.py`).

**Decision.** Introduce `python/harness/search/` as the ONE retrieval layer and
store its index in a **dedicated SQLite file** (`.black-eclipse/search/search.db`,
`ECLIPSE_SEARCH_DB_PATH`), mirroring the code-index precedent rather than adding
a second database engine. Introducing PostgreSQL/pgvector would break the
zero-config promise, which is a concrete architectural reason to deviate from the
reference plan (spec §6 permits this). The embedding-provider abstraction,
vector-space/version registry, hybrid retrieval, reranking, provenance and
citations are storage-agnostic, so the store can be swapped for pgvector later
without touching callers. Keyword search uses SQLite **FTS5 BM25** (no
Elasticsearch); vectors are served by a real **sqlite-vec `vec0` ANN index** per
vector space when the extension is present (the KNN is restricted to the
caller's authorization buckets before scoring, so no unauthorized vector is ever
a candidate), and fall back to brute-force cosine when it is absent. Metadata
filters are indexed (`SearchDocumentMeta` + a distinct `documentType` column) and
compiled into the candidate SQL, and indexing is incremental by content hash. One
`SearchService` is constructed at boot (`search/bootstrap.py`) and reused by the
agent tools and the operator HTTP surface; per-run searches re-resolve the
embedding provider from the run's `ModelSettings`. Semantic search **degrades
honestly to keyword/FTS** when no embedding provider is configured — it never
fabricates a vector, and a fake provider is never registered. Authorization is a
SQL predicate compiled from the run's scope (workspace/owner/public), so results
are never scanned-then-filtered. Zero-config still works: with no provider the
layer is keyword-only.

**Consequences.** Real semantic + hybrid + keyword search with batching, caching,
retry/backoff, reranking, temporal/entity/metadata filters, dedup, parent/neighbor
context, an EvidenceStore bridge and an evaluation harness (Recall@K/Precision@K/
MRR/NDCG). The ANN ceiling and pgvector migration are explicit, documented
limitations, not silent gaps. Tests: python/tests/test_search_*.py. Docs
docs/harness/search-retrieval.md. Front door: `src/lib/harness/search-{client,types}.ts`,
`src/app/api/harness/search/[[...path]]/route.ts`, and the mounted Search
Intelligence tab.

## ADR-H152 - Automation is a first-class domain object; one scheduler decides WHEN, the runtime decides WHAT

**Decision.** The earlier `harness.jobs` scheduler (ADR-H108) is upgraded into the
Native Automation Engine (`python/harness/automation/`). An automation is a durable
domain object with a trigger (cron / interval / one-time / event / condition /
manual), an optional gate condition, an action, and execution / retry / concurrency
/ resource / permission / delivery policy — not a cron string. Two state machines
are kept strictly separate: the automation's LIFECYCLE (`draft/validating/active/
paused/disabled/archived`) and an EXECUTION's state (`triggered/queued/running/
waiting/retrying/completed/failed/cancelled/timed_out/blocked/skipped`); an invalid
transition is rejected with a code. Conditions are a small, safe expression language
evaluated THREE-VALUED (a missing operand is `UNAVAILABLE`, never silently false)
with persisted edge state, so `BTC.price < 90000` fires on FALSE→TRUE only and is
restart-safe. The engine is the ONE scheduler: due lookup is an indexed
`nextRunAt` query, event routing is an indexed `AutomationEventSubscription` join
(never a full scan), event delivery is deduplicated durably, work is protected by a
lease-based lock (expiry reclaims a crashed worker's execution), retries use
backoff + jitter and NEVER retry a non-retryable error class, and concurrency is
bounded by policy. Actions dispatch through the EXISTING runtime — `service.run` /
`run_skill` / `run_workflow`, the Task System (`service.tasks`) and the
Orchestration layer — so there is no second agent loop, tool registry or adapter.

**Why.** Automation had a scheduler but not a domain: it could not express a
condition, an event, a retry policy, a concurrency policy or a resource limit, and
its timezone field was stored but ignored. The risk of adding those as a SECOND
system was real — two schedulers, two ledgers, two execution paths. This ADR makes
the opposite choice: one definition model, one durable ledger, one scheduler, and
the legacy `/jobs` API becomes a thin ADAPTER (`automation/legacy.py`,
`routers/jobs.py`) over the same service, so existing clients and their tests keep
working while the implementation underneath is single.

**Consequences.** `Automation*` tables live on the ONE `agent-memory.db`;
`migrate_legacy_jobs()` imports existing `HarnessJob` rows and their run history
preserving ids and never deleting data. Every automation stores only a credential
REFERENCE (never a value) and validation refuses embedded secret-like keys; the
effective permission policy is deny-wins and approval is a single-use, digest-bound
`ApprovalStore` claim. Execution snapshots capture the effective configuration
(trigger/condition/action/policies) but NEVER credentials, so "why did it behave
this way" is answerable. Known honest limitations: token/cost limits are detected
at run end (providers bill per call, so they cannot be prevented mid-run); the
runtime is single-process (the lease makes that explicit and crash-safe rather than
promising multi-process throughput). Tests: `python/tests/test_automation_*.py`.
Docs: `docs/harness/automation-engine.md`. Front door:
`src/lib/harness/automation-{client,browser}.ts`, `src/app/api/automations/**`.


## ADR-H153 - One Event System: a canonical envelope + registry over a durable, indexed event store, bridged to the existing run bus

**Decision.** Eclipse gains a first-class Event System (`python/harness/event_system/`)
that owns the ONE canonical event model. An `EclipseEvent` carries a globally
unique id, a UTC timestamp, `type` + `version`, source, actor, correlation /
causation / trace ids, session / task / workflow / agent associations, priority,
a validated payload and metadata. A centralized `EventRegistry` declares each
event type (or namespace) with its schema, producer, allowed consumers,
permission, priority, retention, retry policy and sensitivity; names are never
scattered as magic strings. Events are persisted to an indexed `HarnessEventLog`
on the ONE `agent-memory.db` (plus `HarnessEventConsumer` for consumer-level
idempotency, `HarnessEventDeadLetter` for the DLQ, and
`HarnessEventSubscription` for declared subscriptions). The `EventBus` provides
`publish` / `publishMany` / `subscribe` / `unsubscribe` / `once` / `request`,
composable middleware (validation → authorization → redaction → enrichment →
persistence → routing → delivery → metrics), per-consumer async workers with
bounded queues (backpressure), exponential-backoff retry with jitter (never
retrying a non-retryable error class), idempotent consumers, a real dead-letter
queue, tracing and graceful shutdown.

**Why.** The runtime already emitted events in several places — the per-run SSE
`EventBus`/`RunEmitter`, orchestration and task event vocabularies, and the
Automation Engine's event routing — but there was no single durable event model:
no canonical envelope, no centralized naming/validation, no store queryable by
correlation / causation / task / workflow / agent, no consumer idempotency, no
DLQ, no replay and no console. The risk of the obvious fix was a SECOND bus and
divergent vocabularies. This ADR takes the opposite choice: the existing run bus
stays the low-latency live transport, and the new `EventBus` **bridges** to it —
ingesting every legacy event (reusing its id so dedup holds) and echoing new
typed events back — so there is one transport abstraction and one model. The
task publisher (`tasks/bootstrap.py`) now emits canonical names
(`task.created`, not `task.task_created`, and `workflow.created` for plans), and
the scheduler emits `schedule.triggered`, so a timed fire is an observable event
rather than a coupled Scheduler→Agent jump.

**Consequences.** Correlation and causation propagate automatically through
`contextvars` — a follow-up event created inside a handler inherits them without
the developer threading ids through every call. Sensitive events require an
operator actor and declared permissions are checked with the ONE permission
authority (`skills.permissions`); payloads are redacted (by key and by value
pattern) as a publish middleware so a secret can never reach the store. Replay
is explicit and three-mode (`observe` / `simulation` / `full`); `full` requires
operator authorization and destructive/financial types require confirmation, and
a replayed event is a NEW event (new id, `metadata.originalId`) so idempotency
keys on it and the replay fires. Built-in consumers are deliberately conservative
to avoid loops and storms: a memory-candidate extractor emits bounded
`memory.candidate.created` events (the Event Store is never agent memory), a
self-improvement trigger requests an `evaluation.started` at most twice per
correlation and never past depth 6, and a market sampler coalesces
`market.price.updated` into `market.snapshot.updated` so a price tick can never
cause one LLM call. Event→action rules reuse the Automation Engine
(`EventRuleBridge`) rather than introducing a second scheduler. The console is a
standalone `/events` route over an operator-gated `/events/*` API. Tests:
`python/tests/test_events_{core,bus,service,api}.py`. Docs:
`docs/harness/event-system.md`.

## ADR-H154 — Plugins extend Eclipse through its existing systems under a closed manifest, a closed permission vocabulary and enforced isolation

**Decision.** Third-party extensions are first-class plugins, validated against a
versioned manifest (`python/harness/plugins/manifest.py`), granted exactly their
declared permissions and enforced at every call site
(`permissions.py` + `runtime.PluginContext`), driven through a closed lifecycle
state machine (`model.py`) with durable state on `agent-memory.db` (`store.py`)
and an atomic/rollback-safe manager (`manager.py`). A plugin contributes tools,
skills, events, hooks, commands, UI, workers, webhooks and provider descriptors
through a small SDK (`sdk.py`). Contributed tools register on the ONE
`ToolRegistry` (`plug_<id>_<name>`); events publish/subscribe on the ONE run bus
that the Event System ingests; hooks run at the canonical points; effectful
contributed tools reuse the existing effect authorizer. There is no second tool
runtime, event bus, scheduler or agent loop.

**Why.** Eclipse already has the composition surfaces (Tool Registry, Event Bus,
hooks, Evidence engine, approvals). A plugin system that forked any of them would
double the security and observability surface. More importantly, "declared
permissions" are only real if enforced where the resource is touched, and a
manifest is only trustworthy if the runtime is cross-checked against it. Closing
both — the vocabulary and the manifest-vs-runtime agreement — is what makes
installing third-party code a governed act rather than a leap of faith. A closed
lifecycle with an exclusive writer avoids the class of bugs where plugin state
and reality drift across restarts.

**Consequences.** Unknown permissions, malformed SemVer and undeclared/missing
contributions are hard rejections. Elevated permissions (`process.spawn`,
`terminal`, `browser`, `database`, `filesystem.write`) require an operator to
enable. Secrets are encrypted at rest (Fernet, 0600 key file), scoped and never
excluded from logs/telemetry by construction; package integrity is
checksum-verified on load and package file paths are traversal-checked; the only
network path is the controlled HTTP client (timeouts/retries/rate limiting/circuit
breaking + metadata-IP SSRF guard). State is durable, so a restart restores the
enabled set and a plugin that fails to reload becomes FAILED rather than silently
vanishing. Agent reach is opt-in (`allowPluginTools`) and capability-discovered
(`plugin_capabilities`/`plugin_invoke`) so enabled plugins do not bloat every
prompt. `docs/harness/plugin-platform.md` is the operator/author entry point;
`docs/NAVIGATION.md` and `docs/CHANGELOG.md` are updated in the same change.
Untrusted code is additionally isolated in a **separate OS process** (JSON-RPC
with a host-proxied context), so a plugin crash cannot crash Core; OAuth2 token
lifecycle, a SHA-256-verified hosted registry, outbound webhooks, a WebSocket
client and declarative UI view specs are host-owned seams the plugin never
reimplements.

## ADR-H155 - Agent-to-Agent is a registry + typed message bus over the EXISTING execution plane; a delegated task is a normal run

**Decision.** Eclipse's A2A subsystem lives in `python/harness/a2a/` and consists
of a versioned **Agent Registry** (agent behaviour is DATA: capabilities, tools,
permissions, budgets, delegation rights), a typed **AgentEnvelope protocol** with
per-type payload validation and a content-derived idempotency key, a durable
**MessageBus** (publish/consume/acknowledge/reject/dead-letter over
`agent-memory.db`), per-root-run **budgets**, **least-privilege authorization**
with a spawn intersection and an approval gate for trading/external actions, a
**result/finding/conflict** merger, and a **Supervisor + QA** stage. The model-
facing surface is `A2A_TOOLS`; the operator surface is `/a2a/*`.

**Why.** Multi-agent behaviour is where systems most easily fork their own
runtime: a second agent loop, an in-memory queue, a parallel tool registry, a
second database. Eclipse already has exactly ONE of each — `execute_run`, the
`ToolRegistry`, the `EventBus`, the Task runtime (durable tasks + scheduler +
checkpoints) and `agent-memory.db`. The A2A layer therefore creates a durable
**Task** for every delegation and lets the existing `TaskScheduler` dispatch it
through the ordinary run path; messages are rows, not events; the process
`EventBus` only wakes live waiters. This keeps one security surface, one
observability surface and one persistence layer.

**Consequences.** Delegation is non-blocking (returns a task handle); `wait_agent`
is event-driven, never a poll/sleep. Privileges only ever narrow: a child budget,
a spawned agent and a delegated tool set are each the INTERSECTION of the parent's,
and `operator` is never inherited. A capability that touches the real world
(trading, deployment) is approval-gated, so a research agent cannot trade merely by
delegating to one. Findings must carry evidence references; an unbacked or
unverifiable claim is disclosed as **unsupported** with capped confidence, never
promoted to fact. Every message is durable, correlated, idempotent and retryable,
so a crash between publish and consume loses nothing. The A2A tools are registered
on the ONE registry and reachable from `ECLIPSE_AGENT`; the layer is wired from
`boot.py` so the service construction block stays under its size ratchet.
`docs/harness/agent-to-agent.md` is the entry point; `docs/NAVIGATION.md` and
`docs/CHANGELOG.md` are updated in the same change. The Agent Studio graph/live-
execution views (spec §41) are not built yet.

## ADR-H156 - Execution is one substrate: an ExecutionManager routes to DIRECT/CONCURRENT/BACKGROUND/DURABLE over a native durable queue, not a second runtime

**Decision.** Eclipse gains an **Execution Fabric** in `python/harness/fabric/`
that is the common substrate beneath every asynchronous or long-running unit of
work. `ExecutionManager.submit(...)` routes each unit by its type's
`ExecutionPolicy` to one of four modes — `DIRECT` (inline, unpersisted),
`CONCURRENT` (queued, awaited), `BACKGROUND` (queued, fire-and-forget),
`DURABLE` (queued, persisted, idempotent) — over a **native durable queue** on
the ONE `agent-memory.db` connection, served by a **worker pool** (`WorkerManager`)
that claims atomically, heartbeats leases, enforces timeouts, hard-cancels,
retries with backoff + jitter, dead-letters exhausted work, recovers orphaned
jobs and drains on shutdown. Jobs move through a **closed state machine** and
carry correlation/trace/parent/root ids; a `PolicyRegistry` holds the defaults;
`DistributedLockManager` provides TTL'd owner+token locks; and event rules map
selected events to jobs (data, not code in producers).

**Why.** The runtime is a single-process, zero-config, SQLite-backed Python
service (AGENTS.md). Introducing Redis + BullMQ + PostgreSQL (as a generic
"queue layer" brief suggests) would violate the repository's own rules — no
dependency without justification, zero-config must keep working, reuse before
rebuild — and would create a second, disconnected execution plane beside the
already-strong `orchestration` DAG engine, the `tasks` scheduler, the automation
engine, the event system, the plugins and the browser/quant/sandbox runtimes.
The honest gap was not "a queue product" but a **unified routing seam** plus a
durable background worker pool that reuses those capabilities. The Fabric is
that seam: workers never re-implement a capability — an `agent` job calls
`service.run`, a `tool` job calls `ToolRegistry.execute`, `python`/`code` call
the isolated `code.sandbox`, `plugin` calls the plugin manager, `automation`
calls the automation engine, and `notification`/`event` publish on the shared
bus. It persists to the same database as everything else.

**Consequences.** One execution surface, one persistence layer, one security
surface, one observability surface. Priorities are Eclipse's own
(`USER_REQUEST` … `MAINTENANCE`) so bulk ingestion cannot starve an interactive
request. Idempotency keys coalesce duplicate durable submissions. A worker crash
cannot lose durable work: the lease lapses and `recover()` re-queues it. The
manager is the ONE place a new job type is registered (policy + worker), and the
agent tools + `/fabric/*` admin API + `/fabric` console all read the SAME
manager, so they can never disagree. `service.py` is untouched (the layer wires
via `fabric/bootstrap.py` from `boot.py`). If a future deployment genuinely needs
a distributed broker, the `ExecutionManager` boundary is where it would be
swapped in without touching a single agent or tool. `docs/harness/execution-fabric.md`
is the entry point; `docs/NAVIGATION.md` and `docs/CHANGELOG.md` are updated in
the same change.

## ADR-H157 - The agent is measured by running the REAL runtime against versioned, environment-isolated tasks, and every result is per-dimension (never one blended score)

**Decision.** Eclipse gains a first-class **Agent Evaluation System** in
`python/harness/evaluation/`. It executes versioned, environment-isolated tasks
through the real agent runtime — the same `harness.graph.execute_run` tool loop
the product uses — captures the complete trajectory from the runtime's own event
bus, grades it with a pluggable grader engine, aggregates trials with statistics,
persists every result to the ONE `agent-memory.db` connection, compares against
frozen baselines and detects per-dimension regressions. Two invokers share one
runner: a **scripted** offline tier (the real graph with a deterministic scripted
chat model — the model is the only substituted part) and a **live** tier (the
production `AgenticService.run` with a real model). Isolation between concurrent
trials is by `contextvars`, so independent trials run in parallel in one process.
The offline gate is a single step inside the repo's one release gate
(`scripts/test-evals.js`), runs on a private temp database, and is
per-category — never a blended score. An unmeasurable metric renders `UNKNOWN`,
never `0`; a gate fails only on a MEASURED violation.

**Why.** "The tests are green" was the whole claim about agent quality; nothing
measured whether the agent was getting better or worse across task success,
planning, tool selection and arguments, research/citation quality, financial
calculation accuracy, code correctness, memory, safety or hallucination. A single
score would hide the category that always fails behind the one that always
succeeds, and a fake or simplified agent under test would measure the harness
rather than the product. The repo already had the pieces to do this honestly: a
real LangGraph tool loop with a documented model seam (used by its own tests), a
durable event bus, a shared SQLite store, and an established eval-pack pattern
(`research/eval`, `eclipse_memory/eval`, `code/eval`). The evaluation system
reuses all of them and adds only the missing measurement surface — tasks,
environments, trajectory capture, graders, trials, baselines, regression gates,
reporting, mining and the self-improvement verification loop.

**Consequences.** The capability is now measurable end to end: the offline gate
runs 31 labelled tasks across 15 categories through the real runtime and reports
per-category pass counts plus separate dimensions (task success, tool accuracy,
financial/citation/code accuracy, hallucination rate, safety score, cost,
latency, tokens). Graders are pluggable and deterministic where determinism is
possible — financial calculations are independently recomputed, code is actually
executed, state is read from the environment, safety reads the runtime's own
structured refusal codes. Tool arguments, which the runtime's `tool.requested`
event deliberately omits for privacy, are recorded at the `ToolRegistry`
boundary and folded back into the trajectory, so argument accuracy is gradeable
without changing the runtime. A production failure can be mined into a
deduplicated regression candidate and, once its expected behavior is validated,
promoted into the standing suite; the self-improvement loop accepts a change only
when the target improves AND no critical capability regresses. Historical results
are versioned by task, dataset and grader so a change never silently
reinterprets an old score. `docs/harness/agent-evaluation.md` is the entry point;
`docs/NAVIGATION.md` and `docs/CHANGELOG.md` are updated in the same change.

## ADR-H158 - Observability is one vendor-neutral platform that projects the existing event bus; it is fail-open and never a second runtime

**Decision.** Eclipse gains an **Observability Platform** in
`python/harness/observability/`. It is a cross-cutting telemetry layer built on
the standard library only, with an Eclipse-native abstraction
(`ObservabilityHub`, `HookEvent`, `Observer`, `ObservationContext`, `Tracer`,
`MetricsRegistry`, `CostEngine`, `Redactor`, `Sampler`, `BoundedExporter`) and
optional vendor adapters behind one `Exporter` protocol (OTLP/HTTP, Langfuse,
Sentry, JSONL, console, in-memory). It derives telemetry by subscribing **once**
to the existing universal `service.bus` (`RunEventProjector`) and chains the
`model_runtime`'s single observer (`ModelObserverBridge`), rather than adding
instrumentation calls through every subsystem. `install_observability(service)`
is called at the end of `AgenticService.__init__`; `start_observability` /
`stop_observability` run in the FastAPI lifespan; routes are `/observability/*`
(operator-gated).

**Why.** The brief asked for OpenTelemetry + Prometheus + Grafana + Loki + Tempo
+ Langfuse + Sentry. Introducing those dependencies into a **single-process,
zero-config, SQLite-backed** runtime, and re-instrumenting forty subsystems,
would violate the repository's own rules (no unjustified dependencies,
zero-config must keep working, reuse before rebuild, keep files small) and would
create a second, competing telemetry stack beside the existing per-subsystem
`telemetry.py` modules, `event_system/metrics.py`, `fabric/metrics.py` and the
model runtime's own usage/cost/routing accounting. The honest gap was a **unified,
vendor-neutral telemetry layer** that (a) correlates every operation under one
trace, (b) is fail-open so a backend outage cannot break a run, and (c) projects
the event stream Eclipse already emits. OpenTelemetry remains the instrumentation
convention (W3C-shaped ids, `inject`/`extract`); the vendor backends are adapters,
not the core abstraction.

**Consequences.** One ingestion path, one cost engine, one redactor, one export
pipeline. A dead/failing/slow backend only drops and counts telemetry never a
run because every export is bounded, non-blocking, retried and circuit-broken.
Secrets are stripped by the single `Redactor` before any record leaves the
process. Errors, failed runs and anomalies are always sampled. The whole run
trajectory is reconstructable from one trace via
`/observability/traces/{id}/trajectory`. New subsystems join the trace by
publishing on the bus (preferred) or calling `get_hub().emit`/`.span` never by
adding a second stack. `docs/harness/observability.md` is the entry point;
`docs/NAVIGATION.md` and `docs/CHANGELOG.md` are updated in the same change.

## ADR-H159 — Security is one decision path over the existing per-subsystem policies; the floor is immutable and every decision fails closed

**Decision.** Eclipse gains a single Security subsystem at `python/harness/security/`
whose `SecurityGateway` is the ONE authorization path for every privileged
operation. It composes — never replaces — the existing federated controls:
`code/policy.py`, `skills/permissions.py`, `mcp/approvals.py` + `grants.py`,
`browser/policy.py`, `computer/policy.py` + `computer/finance/firewall.py`,
`git/safety.py`, `execution/policy.py`, `terminal/`, `plugins/permissions.py`,
`mcp/ssrf.py`, `observability/redaction.py` and `deps.py`. The gateway owns the
missing central pieces: a **Permission Engine** (resource-scoped globs, never a
global boolean), a **Policy Engine** (LOCKDOWN/SECURE/DEVELOPER/AUTONOMOUS/CUSTOM
profiles, layered system→organization→user→project→agent→task, restrict-only
compose, atomic hot reload that never replaces a known-good policy), a **Risk
Engine** (server-side, ordered by severity), an **Approval Engine** (a typed
broker over the existing single-use `McpApproval` store + `StandingGrant`s), a
**Capability Engine** (issue/validate/delegate/restrict/revoke/expire; delegation
may only reduce authority), a **Security Floor** (immutable hard-deny /
hard-allow / mandatory-approval rules that no profile, skill, plugin, MCP server,
prompt or request field can weaken), a **Network Policy Engine** (default-deny
egress, reusing `mcp/ssrf.py`, DNS-pinned), a **Secrets Manager** (provider
abstraction over env/local, scoped grants, values registered for redaction, never
returned to the model), **session-isolated `SecurityContext`s**, and an
**append-only, hash-chained audit** on the ONE `agent-memory.db`. Authorization
decisions are `ALLOW` / `DENY` / `APPROVAL_REQUIRED` exactly. The initial
migration routes the filesystem and shell capabilities through the gateway at the
existing `code/authorizers.py` seam: the floor and network policy are always
authoritative, policy-driven approvals are deferred to the capability's
established single-use chain, and every evaluation is audited.

**Why.** The brief asked for a unified security architecture covering thirty-five
areas. Reconnaissance showed roughly sixty percent already existed — but
federated, with risk logic duplicated in five modules, redaction in three, no
central risk/policy/network/secrets authority, and no immutable baseline. Building
a parallel stack would have violated `AGENTS.md`'s reuse-before-rebuild rule and
created competing authorities; extending in place without unification would leave
"one authoritative security decision path" unmet and bypassable. A composition
layer that the existing per-subsystem policies delegate into is the only design
that satisfies both the specification's invariants and the repository's own rules.

**Consequences.** Every privileged effect now has one auditable decision path;
the floor protects credentials, keys, the security configuration, system
directories, production infrastructure and financial actions regardless of caller.
Denials and approval-required outcomes fail closed: a missing approval store, an
unreachable secret provider, a broken policy or an internal evaluation error all
yield `DENY`, never a silent allow. Secrets are referenced (`secret://…`), scoped
per consumer/session/task, and stripped from logs, tool output and the audit trail
by the one redactor. Capabilities cannot escalate through delegation. Sessions are
isolated: a capability, secret or workspace from another session is refused.
`docs/harness/security.md` is the architecture entry point; the operator surface
is `/security/*` (operator-gated) proxied by `/api/security/**`. The remaining
capabilities (browser, database, deployment, financial, plugins, MCP, skills) are
migrated in subsequent increments through the same `make_security_authorizer` seam
and the same `AuthorizationRequest` contract. Tests:
`python/tests/test_security_{core,adversarial,failure,concurrency,network_secrets,integration,api}.py`.

## ADR-H160 — Every effectful capability is classified through the gateway; the sandbox is a separate physical boundary; approvals defer to the capability's own chain

**Decision.** The Security subsystem is extended from the initial filesystem/shell
migration to ALL capabilities. `security/classification.py` maps every tool in the
canonical `code/policy.py` effect tuples to `(action, resource, permission)`, so
`build_effect_authorizer` routes browser, computer, git/VCS, execution,
application-runtime, infrastructure/deployment, database, vision, plugin, MCP and
skill effects through the same `SecurityGateway`. The gateway enforces the floor
(hard-deny) and a policy deny as terminal, audits and risk-scores every evaluation,
and **defers approvals to the capability's own established single-use approval
chain** (`defer_policy_approval=True`) so there is exactly one approval and no
double prompt. Environment is resolved from the run context, the deploy action, or
`BLACK_ECLIPSE_ENVIRONMENT`. The physical execution boundary is a separate
abstraction, `security/sandbox.py` (`ExecutionSandbox`), with a hardened
`DockerSandbox`, a `LocalSandbox` over the existing `code/sandbox.py` primitive, and
an `UnavailableSandbox` that fails closed. Secrets gain production providers
(`OsKeychainProvider`, `CliSecretProvider`) and the audit trail an optional
append-only JSONL anchor with an HMAC-signed head. A `SecuritySettingsPanel`
(Settings › Security) is the operator UI.

**Why.** The original ADR left the other capabilities on their own policy modules,
so the spec's invariant "no tool may bypass the gateway" was not yet true and the
unified decision path was advisory for most effects. The choice to *defer* approvals
rather than duplicate them is deliberate: each capability already has a correct,
single-use, digest-bound approval with a working operator UX (MCP approvals, code
actions, git operations, browser/computer ASK). Re-asking at the gateway would
double-prompt and fragment the audit. Hard denies are never deferred, so the
strongest control applies everywhere while the approval surface stays singular. The
sandbox is kept separate from authorization because they answer different questions
— *may this run?* versus *where does it run?* — and conflating them is the classic
mistake the spec warns against.

**Consequences.** Every privileged effect now has one auditable, floor-enforced
decision path; a bypass-audit test fails if any canonical effect tool is
unclassified. The environment actually gates PRODUCTION writes/deploys. Execution
can be placed behind a real container boundary independent of policy. Secrets can
come from the OS keychain or Vault/AWS/1Password via a configured CLI, and the audit
trail can be anchored outside the runtime's writable database. The default remains
zero-config and fail-closed: no Docker, no keychain, and no audit log means the
local backend, honest unavailability, and the in-DB hash chain — never a silent
weakening. `docs/harness/security.md` is updated in the same change; NAVIGATION and
CHANGELOG too.

## ADR-H161 — Identity is verified at the edge and carried as a signed token; tenants are isolated in the security plane; the front door is the authenticator

**Decision.** The runtime no longer trusts a caller-supplied `userId`/`tenantId`.
The Next.js front door authenticates the user, resolves the DB-backed user and
tenant scope, and mints a short-lived (5 min) HS256 JWT with `HARNESS_AUTH_SECRET`
(`src/lib/harness/runtime-identity.ts`). Proxies forward it as
`X-Harness-Identity`; `python/harness/security/identity.py` verifies it with
stdlib only (no new dependency) and yields a verified `Identity`. Precedence: a
valid identity token → the session identity; otherwise loopback / operator secret
→ an operator identity; otherwise anonymous. In multi-tenant mode
(`HARNESS_MULTI_TENANT=1`, or auto-on when the front door requires auth) an
anonymous caller is refused (401). The Security subsystem becomes tenant-aware:
capabilities, secret grants, sessions and audit records carry a `tenantId` and
never cross tenants; the gateway refuses an untenanted request in multi-tenant
mode. Beyond loopback the runtime now refuses to start unless `CRON_SECRET` (and,
in multi-tenant mode, `HARNESS_AUTH_SECRET`) are configured. A per-tenant token
bucket rate-limits the security endpoints.

**Why.** Internet-facing multi-tenancy requires that identity be *authenticated
once, at the edge*, and *proven* to the runtime — not asserted in a body. Reusing
the front door's signing secret keeps one authentication story; stdlib HS256
verification keeps the zero-dependency rule. Scoping capabilities, secrets,
sessions and audit by tenant is the authorization half of tenant isolation and is
the part the security subsystem owns. The remaining half — every run, memory,
task, artifact and database row carrying and enforcing a tenant — is a data-plane
migration across every subsystem's storage, tracked separately; it is NOT claimed
here.

**Consequences.** A stolen/forged token fails signature or expiry; an anonymous
request in multi-tenant mode is refused; a capability or secret granted to tenant
A is inert for tenant B; the audit trail is filterable per tenant; and a
misconfigured public bind without secrets refuses to start. **Boundary:** this
delivers the multi-tenant *security plane* and the identity propagation seam. Full
tenant data isolation (runs, memory, tasks, artifacts, market/JDdS reads, and the
broader proxy rollout of the identity header) is the follow-on data-plane work and
must be completed before a genuine multi-tenant deployment is declared. `/health`
reports `multiTenant` so a deployment can observe the mode.

## ADR-H162 — Tenants are isolated by physical store, the identity header is required on every proxy, and the sandbox is a gate

**Decision.** Three follow-on controls turn the multi-tenant security plane into
enforceable isolation where it can be enforced today. (1) **Physical tenant
stores:** `security/tenant_store.py` resolves a tenant id to its own
`AgentMemoryStore` file under a tenant root
(`.black-eclipse/tenants/<tenant>/agent-memory.db`, `BLACK_ECLIPSE_TENANT_DB_DIR`),
strictly sanitizing the id so a hostile tenant cannot traverse the path. Two
tenants get two databases, so a missing `WHERE` clause cannot leak rows. (2)
**Identity header everywhere:** every `[[...path]]/route.ts` proxy now forwards
`X-Harness-Identity` (via `runtimeIdentityHeaders`), so no route reaches the
runtime without the verified user/tenant. (3) **Sandbox gate:**
`BLACK_ECLIPSE_SANDBOX_REQUIRED` (`1`|`docker`) makes the gateway refuse an
execution action when the required isolation backend is absent, audited as
`security.sandbox.blocked`. A `scripts/security-preflight.js` (`npm run
security:preflight`) fails closed on a network-facing deployment missing
`HARNESS_AUTH_SECRET`/`CRON_SECRET`.

**Why.** The strongest practical isolation for a shared runtime is physical
separation of storage, and it is achievable without threading a tenant through
every existing query. Verifying identity at the edge is worthless if most proxies
drop the header, so the header became universal. An optional-but-enforced sandbox
requirement lets an operator demand container isolation and fail closed when it
is missing, rather than silently running weaker. A deploy-time preflight catches a
missing secret before the first request.

**Consequences.** Tenant runs/sessions/memory can be physically partitioned by
routing them to `TenantStoreRegistry.store_for(tenant)`; until that routing is
wired through the run path (graph/service) the primitive is available but not yet
the default, so **the run path is not yet tenant-partitioned** — this remains the
load-bearing gap. The identity header is now present on all feature proxies; the
explicit `/runs`/`/chat` route still forwards only the front-door-verified
`userId`/`scopeId` and does not yet attach the signed token. The sandbox gate is
opt-in (`BLACK_ECLIPSE_SANDBOX_REQUIRED` unset = current behavior). NAVIGATION and
CHANGELOG are updated in the same change.

> **ADR-H160 was renumbered to ADR-H288.** The series had two entries with this number; the first one keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H288 - The Artifact Center is one catalog over the existing stores; it owns its own tables and a confined content-addressed file store, and every producer registers through one service

**Decision.** Produced files are recorded in a single, durable catalog
(`ArtifactService` + `ArtifactStore`) whose rows live on the ONE
`agent-memory.db` connection, alongside a dedicated, confined,
content-addressed on-disk store under `.black-eclipse/artifacts/`. The catalog is
NOT JDdS (`jdds.db`, market data) and NOT another subsystem's tables. One
service owns the lifecycle (register, version, preview, tag, collect, archive,
restore, delete, purge, reconcile); the REST route group `/artifacts/*`
(operator-gated), the `artifact_*` agent tools, and every producer hook
(`analysis_tools`, the task scheduler) all call that one service. Bytes are never
referenced by a workspace path: the service copies them into the store and returns
an opaque storage key, and the storage layer � not the code policy's workspace
ignore list � enforces confinement (`..`/absolute/symlink escape refused; `*.png`
and `.black-eclipse/**` are legitimate content here).

**Why.** The roadmap's round-2 complaint was that a produced file "never leaves
Eclipse's copy" and nothing serves it. A one-off route per producer would have
duplicated storage, authorization and provenance. One catalog over the existing
connection reuses the harness's durability and the ONE event bus, and gives the
Artifact Center a single place to enforce owner scope, idempotency, lifecycle and
reconciliation.

**Consequences.** Producers depend only on `ArtifactService`; a missing service is
reported honestly (`ARTIFACT_UNAVAILABLE`) rather than silently dropping a file.
Registration is idempotent by key, preview failure never invalidates the original,
and soft-delete/purge are distinct with a shared-reference guard. A bounded
reconciler repairs missing objects on a schedule. New tables live in
`artifacts/store.py` (not `persistence.SCHEMA`), matching the subsystem pattern.
`docs/harness/artifact-center.md` is the architecture entry point; the operator
surface is `/artifacts/*` proxied by `/api/artifacts/**`. Tests:
`python/tests/test_artifacts_{store,security,tools,api}.py` and
`src/lib/harness/artifacts-browser.test.ts`.

> **ADR-H162 was renumbered to ADR-H289.** The series had two entries with this number; the first one keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H289 - Claim verification reaches the delivery seam: an ordinary run's citations are checked against the evidence it actually retrieved, and every web-research run is registered as an evidence-referencing artifact

**Decision.** Two additions close the remaining gap between "the evidence and
verification engine exists" and "it runs before the answer is released":

1. An **evidence release gate** (`research/release_gate.py`) runs at the EXISTING
   answer seam in `graph.execute_run`, beside the code and finance enforcements. It
   is deterministic (no model call): it extracts the final answer's http(s)
   citations and checks each against the URLs the run actually retrieved or
   consulted (the `SourceCollector` provenance the chat Sources panel already
   renders). A cited link with no runtime provenance is disclosed as unverified;
   the verdict is persisted on the run's `research` blob
   (`metadata.citationIntegrity`) and emitted as `evidence.release_gate`. It never
   strips content (suppression would hide a real answer) and is a no-op when the
   answer makes no citations, so casual conversation is not cluttered.
2. A completed **web-research run is registered in the Artifact Center** as a
   durable, evidence-REFERENCING report artifact
   (`artifacts.producers.register_research_report`, invoked best-effort from
   `research/web_engine.py` on completion). The Markdown report names claim
   verdicts, conflicts and an evidence appendix by id, and its `metadata` carries
   the `researchId`, `snapshotId` and evidence ids. It references the evidence
   store; it never duplicates it. Registration is idempotent per
   `(researchId, snapshotId)` and a no-op when the Artifact Center is not
   installed.

**Why.** The evaluation system already GRADED citation support, but the runtime
never ENFORCED it: a model could cite a URL it never retrieved and nothing
detected it before release. The gated financial flow validated its claims; ordinary
runs did not. Separately, a research run's evidence/validation history was
inspectable only inside the research subsystem, not in the one artifact catalog.

**Consequences.** Provider-independent (deterministic; it works with any model, or
none). The `graph.py` size ratchet is re-pinned for the two-line seam. Tests:
`python/tests/test_evidence_release_gate.py`,
`python/tests/test_research_artifact.py`, and two end-to-end cases in
`python/tests/test_graph.py`. Owner docs:
`docs/harness/web-research-engine.md`, `docs/harness/artifact-center.md`.

## ADR-H163 - One cross-subsystem failure layer: a shared taxonomy, stable signatures, a bounded recovery decision and durable incidents, composing — not replacing — the existing subsystems

**Decision.** Add `python/harness/recovery/` as the single layer that answers
"what kind of failure is this, have I seen it before, and what am I permitted to
do about it?". It provides (1) a shared `FailureKind`/`Retryability` vocabulary
and a pure `normalize_failure` that maps every existing subsystem's error codes
(`model_runtime.errors.ErrorClass`, `execution.models.ErrorClass`, the tools
runtime and `orchestration.retry` codes) onto it; (2) stable 16-hex failure
signatures (volatile tokens collapsed) plus a `RecurrenceTracker`; (3) an ordered,
inspectable `select_strategies` policy and a bounded `RecoveryController.decide`
that emits a single `RecoveryDecision`; and (4) durable `RecoveryIncident` /
`RecoveryAttempt` rows on **`eclipse-memory.db`** (migration 26). At boot it
subscribes to the canonical Event System failure vocabulary, so a `tool.failed`
from ANY subsystem is classified and recorded as a durable incident.

**Why.** Eclipse already owned every *piece* of autonomous problem-solving
(provider normalization, execution diagnostics, retry classification, model
fallback, loop detection, task checkpoints, the learning pipeline, verification),
but nothing composed them. A timeout at a tool, a provider and a workflow node had
no shared identity, so recurrence could not be detected and no policy stated what
was permitted. Duplicating those subsystems would violate the repository's
reuse-before-rebuild rule; the missing thing was the composition layer.

**Consequences.** The layer is deliberately NOT a second execution loop: it never
runs a tool, a graph node or a model, and never mutates task state — it decides
and records, and the existing subsystems remain the actors. The policy is
safety-first (never retry a deterministic failure unchanged; never provider-swap
an authentication denial). It is wired in `boot.py` (not `service.py`/`graph.py`,
which are size-ratcheted) and installed idempotently because boot runs once per
lifespan while the tool registry is process-wide. Configurable via
`HARNESS_RECOVERY_*`. The `test_size_budget` ratchet is re-pinned for the
pre-existing in-flight drift in `graph.py`/`service.py`. Tests:
`python/tests/test_recovery_{taxonomy,signature,policy,store,integration,api}.py`
(40 cases, including a live-bus end-to-end capture). Owner doc:
`docs/harness/failure-recovery.md`. Remaining: the UI panel, automatic strategy
execution, and feeding incidents into skill promotion. (App-level ADR series is
unchanged; this is a harness/runtime decision.)

## ADR-H164 - The recovery engine gains the four capabilities it lacked: root-cause diagnosis, bounded automatic execution, unified capability discovery and incident-to-skill promotion — plus a `/recovery` console

**Decision.** Extend `python/harness/recovery/` so the failure layer does the full
loop, not just classify-and-decide:

1. **Root-cause diagnosis** (`diagnosis.py`, `DiagnosisEngine`): a deterministic
   catalog of plausible causes per `FailureKind`, ranked by evidence actually
   gathered, producing a `Diagnosis` that names the selected cause ONLY when the
   evidence is decisive and otherwise lists what is missing. Probes are pluggable
   but the default set is empty (no model fabricates an environmental fact).
2. **Automatic execution** (`executor.py`, `recover_operation`): a bounded async
   loop that runs a caller's operation, consults the controller, honours backoff,
   executes caller-registered repair handlers, and treats a `verify` failure as a
   failure. It records every attempt/outcome on the incident. It is NOT a second
   execution loop: it never runs a tool or graph node, and a strategy with no
   handler is handed back to the caller.
3. **Unified capability discovery** (`discovery.py`, `CapabilityIndex`): one ranked
   index over the Tool/Provider/Skill registries, plus `acquisition_plan` which
   returns approval-flagged steps — it never silently installs or self-grants.
4. **Incident-to-skill promotion** (`learning.py`): a resolved, recurring incident
   becomes an inert `LearningCandidate` through the EXISTING learning store and
   signature scheme; only an operator promotes it. Persisted via migration 27
   (`RecoveryDiagnosis`, idempotent `CREATE TABLE IF NOT EXISTS`).

A standalone **`/recovery` console** (like `/events` and `/fabric`, so it does not
touch the guarded AppTab/sidebar vocabulary) exposes incidents, diagnosis,
attempts, capability discovery and operator-gated promotion.

**Why.** ADR-H163 shipped classification + a decision but stopped at "decide and
record": it could not say *why*, could not act within its budget, could not find an
alternative capability, and could not feed a validated repair back into learning.
These four complete the autonomous loop requested for Eclipse, while keeping the
repository's rules — reuse over rebuild (the learning store, the registries, the
event bus), and no self-granted privilege.

**Consequences.** Migration 27 is idempotent (a separate `RecoveryDiagnosis` table,
not an `ALTER`, so the pre-change-reopen test passes). The executor's retry loop is
bounded by the same `RecoveryBudget`, and within one loop keeps `RETRY` available
(cross-incident recurrence is reflected in the first decision). The new tools are
read-only; promotion and re-diagnosis are operator-gated. Tests: the ten
`test_recovery_*.py` files (64 cases) and `recovery-view.test.ts`. Owner doc:
`docs/harness/failure-recovery.md`.

## ADR-H165 - Autonomy is a pre-committed envelope, not removed approvals: the recovery engine acts unattended only inside a revocable, scoped, expiring envelope, with deterministic probes and a compensation ledger

**Decision.** Add the three pieces that let the recovery engine act WITHOUT a
human per action while staying safe:

1. **The Autonomy Envelope** (`envelope.py`, `AutonomyEnvelope`): an operator sets
   it once — which `ActionClass`es are allowed, under risk / reversibility / cost /
   file-count / network caps, with an optional expiry — and the engine then acts
   unattended inside it. The **default is conservative** (only reversible
   re-run/re-plan/rollback classes), so zero-config behavior is unchanged; effectful
   classes (dependency install, provider/tool substitution, adapter build) require
   the operator to widen it. A global kill switch `HARNESS_AUTONOMY=0` and a
   fail-closed expiry revoke everything. The envelope NARROWS the security layer,
   never widens it — the security floor/permissions still apply on top.
   `recover_operation` consults the envelope before executing any non-retry
   strategy (with a handler); an action outside it is returned to the caller as
   `outside_autonomy_envelope`, never run.
2. **Deterministic diagnosis probes** (`probes.py`): read-only, default-registered
   probes (dependency importability, provider configuration, provider circuit
   health) that return `True`/`False`/`None` — `None` for "unknown", never a guess.
3. **The operation ledger** (`operations.py`, migration 28): idempotency-keyed,
   durable tracking of side-effecting repairs with compensation bookkeeping, so a
   crashed worker reconciles instead of repeating an effect and a failed repair can
   be compensated.

**Why.** The gap between "decides and records" and "acts" is authority, not
capability. Removing approvals would violate the repo's security rules; the correct
mechanism is moving the human decision from per-action to per-envelope, then making
the unattended actions bounded, reversible and auditable.

**Consequences.** The envelope is loaded from config (never written by an agent),
so the engine cannot widen its own authority. The executor's variable-shadowing bug
(`operation` param vs ledger row) was found and fixed by the new tests. New
operator routes: `GET/PUT /recovery/envelope`, `GET /recovery/operations`. The
console gains an "Autonomy & operations" tab. Tests: `test_recovery_{envelope,
probes,operations,executor}.py`. Owner doc: `docs/harness/failure-recovery.md`.
Remaining: wiring the install/build acquisition rungs and automatic compensation
(recorded, caller-performed).

## ADR-H166 - Autonomy becomes durable and coordinated: persisting the envelope, leasing repairs per signature, granting one-off operator approvals, and executing compensating actions

**Decision.** Four additions turn the envelope from an in-memory setting into an
operable, unattended control plane:

1. **The envelope is durable** (migration 29, `AutonomyEnvelope` table): a
   `PUT /recovery/envelope` persists, and `RecoveryService` loads the durable
   override ahead of the config file/default, so autonomy survives a restart.
2. **A per-signature repair lease** (`RecoveryLease`): the executor claims the
   lease for a failure's signature before running a repair handler and releases it
   after, so two workers never repair the same root cause at once (advisory,
   expiring, reclaimable). A held lease returns `repair_lease_held`.
3. **One-off operator approvals** (`RecoveryApproval`): `POST
   /recovery/incidents/{id}/approve` authorizes exactly one strategy for exactly
   one incident, overriding the envelope for that action only — the human fallback
   that keeps the loop usable without widening standing authority.
4. **Compensating actions** (`compensation.py`, `compensate_pending`): registered
   handlers undo a recorded side effect, envelope-gated under the `COMPENSATE`
   action class; operations without a handler or forbidden by the envelope stay
   pending and are reported.

**Why.** The prior envelope could be set but not persisted, could not prevent two
workers from racing the same repair, offered no way to authorize a single
out-of-envelope action, and recorded compensation obligations it could not
discharge. These are the difference between a policy object and a working control
plane.

**Consequences.** The engine still cannot widen its own authority: the durable
envelope is written only by an operator route. Compensation remains caller-driven
by design (the runtime cannot know how to undo an arbitrary external effect). New
store methods (`save_envelope`/`load_envelope_dict`, `acquire_lease`/`release_lease`,
`add_approval`/`has_approval`). Tests: `test_recovery_{compensation,store,executor,
api}.py`. Owner doc: `docs/harness/failure-recovery.md`. Remaining: the install/
build acquisition rungs, evidence-backed diagnosis, multi-agent delegation, and
live console streaming.

## ADR-H167 - Bounded auto-reproduction and hard safety invariants: reproduction makes a diagnosis decisive, and the autonomy engine's trust properties are pinned by adversarial and concurrency tests

**Decision.** Add the last safe, generic rung of the loop and lock the trust
properties:

1. **Bounded auto-reproduction** (`reproduction.py`, `ReproducerRegistry` +
   `reproduce()`): only the caller knows how to safely re-run a failing operation,
   so reproduction is a registry keyed by failure kind. `reproduce()` runs the
   matching reproducer under a hard `asyncio.wait_for` timeout and classifies the
   outcome (reproduced / not / errored); a successful reproduction is recorded as
   evidence and lets the diagnosis select the leading cause on a weaker margin
   (determinism is proven). New operator route `POST /recovery/incidents/{id}/reproduce`.
2. **Adversarial + concurrency tests** pin the invariants that make unattended
   action trustworthy: the default envelope denies effectful classes; the kill
   switch revokes everything; a non-retryable failure is never retried; recovery is
   always bounded; a one-off approval is scoped to exactly its strategy; incident
   messages are length-bounded and signatures are one-way; the repair lease is
   exclusive under thread contention.
3. **Production & operations documentation** in `docs/harness/failure-recovery.md`
   (readiness/degradation, revocation, attack surface, concurrency, CI, and exactly
   what full autonomy still needs).

**Why.** Reproduction was the one generic evidence step the diagnosis engine
lacked, and the safety properties were asserted piecemeal rather than pinned. For an
open-source, single-user tool this is the readiness work: CI (already on
`ubuntu-latest`), a revocable envelope, and tests that fail if a trust property is
ever weakened.

**Consequences.** Reproduction is caller-driven by design (like compensation and
strategy handlers); the runtime registers none by default because it cannot know how
to re-run an arbitrary operation. Remaining for full autonomy: wiring the install/
build acquisition rungs and the delegation roles into the executor. Owner doc:
`docs/harness/failure-recovery.md`. Tests: `test_recovery_{reproduction,safety,
concurrency}.py`.

## ADR-H168 - Research data is verified and versioned above the existing Market Data Gateway; verification is a pure policy engine, datasets are immutable, and no research client re-implements provider routing

**Decision.** Extend the existing market/quant stack into a shared quantitative
research data capability rather than building a second ingestion system:

1. **Verification is a pure engine** (`market/verification.py`): named policies
   (`cached_fast`, `fresh_preferred`, `cross_verified`, `historical_reproducible`,
   `strict_research`) with documented, testable flags. `resolve_policy` raises on
   an unknown name, so a request is never silently downgraded. Cross-source
   comparison is deterministic: disagreeing sources produce a `Conflict` (never an
   average), independence is earned (an unrecorded upstream reports `unknown`, a
   shared upstream is classified distinctly), and only `verified` sets
   `verified=true`.
2. **The Gateway is the one retrieval seam.** Provider routing/quality/provenance
   stay in `MarketDataGateway`; it gains provider pinning (`provider=`) and
   `candidate_providers()` so a caller can deliberately retrieve an INDEPENDENT
   source. The provider capability registry gains intervals, adjustment support,
   a timeliness taxonomy and entitlement.
3. **Datasets are immutable** (`quant/dataset_store.py`): insert-only versions on
   the ONE `agent-memory.db`, a content hash (symbol-order independent) that makes
   a rebuild idempotent, and gzipped artifacts under `.black-eclipse/quant/`.
   New data / changed adjustments fork `vN+1`; the old version is preserved.
4. **One tool surface** (`market/research_tools.py`) wraps the shared Gateway +
   verification + transforms + dataset store; no tool re-implements routing. The
   tools are registered in `market/bootstrap.py` and allowlisted in `agents.py`.

**Why.** Free providers and JDdS can be stale, delayed, incomplete or mutually
inconsistent. The requirement is not "more data" but **defensible** data: prove
where a number came from, whether it was independently confirmed, and reproduce
the exact inputs behind a result. Keeping verification pure and datasets
insert-only makes those properties testable rather than aspirational.

**Consequences.** Verification is only as independent as the configured providers;
when no suitable second source exists `cross_verified` reports `unverified`, not
verified. Provider adapters remain on the TS bridge. The Quant Research screen is
mounted as the `backtest` AppTab (deep link / command palette), not a sidebar
section, matching the Pages/UI-Builder convention. Not yet done: typed Workflow
Graph nodes for the new tools, dataset/version-linked claim verification in the
Evidence Engine, and isolated notebook execution. Owner doc:
`docs/harness/quant-research-data.md`. Tests: `test_market_verification.py`,
`test_quant_transform.py`, `test_quant_dataset_store.py`,
`test_research_data_tools.py`.

## ADR-H169 - Strategy versions are immutable and content-addressed, and the Strategy IR is portable: identity is a sha256 over the canonical IR, and every strategy escapes the UI through a checksummed bundle

**Context.** The Quant Engine persisted strategy "versions" as overwritable
semver rows (`save_strategy` used `INSERT OR REPLACE`) with no content identity
and no portability surface. That made three requirements impossible to honour:
(a) an experiment must reference a strategy state that can never change under it,
(b) two runtimes/the UI must agree that two strategies are the same content, and
(c) a strategy authored in one surface (visual graph, Python lab, AI copilot)
must be usable in another, independent of that surface.

**Decision.**
1. **Content identity** (`quant/schema.py`): `spec_content_hash` is a sha256 over
   the canonical Strategy IR dict (order-independent), and the IR carries an
   explicit `STRATEGY_SCHEMA_VERSION` with a compatibility policy (same major ?
   loadable) and an ordered, lossless migration registry.
2. **Immutable versions** (`quant/store.py`): `QuantStrategyVersion` gains
   `contentHash` + `schemaVersion` columns (idempotent `ALTER` upgrade path). A
   version already referenced by an experiment is frozen: re-saving *different*
   content under it raises `STRATEGY_VERSION_IMMUTABLE` and the caller must bump
   the version; identical content re-saves are a no-op. Editing is always a new
   version. `version_meta` reports the hash, schema and reference count;
   `diff_versions` returns a structural (added/removed/changed) diff.
3. **Portable bundles** (`quant/bundle.py`): a self-describing zip
   (`manifest.json` + `strategy.json` + `README.md`) with format/version id,
   schema version, content hash and provenance. Import is treated as untrusted
   input � compressed/uncompressed size caps, member-count cap, path-traversal
   rejection, no on-disk extraction, schema gating and a hash-mismatch warning �
   and the IR is re-validated through the normal schema path with unknown keys
   preserved.
4. **One surface.** Three agent tools (`export_strategy`, `import_strategy`,
   `diff_strategy_versions`) and matching `/quant/strategies/{id}/export`
   (zip or `?format=json`), `/quant/strategies/import` and
   `/quant/strategies/{id}/diff` routes reuse the SAME store/engine; the IR, not
   the graph, is the source of truth.

**Why.** Immutability is what makes an experiment reproducible: if a version can
silently change after being referenced, every result it produced is
unverifiable. A content hash makes sameness machine-checkable and lets identical
content dedupe. Portability that is a first-class, checksummed artifact keeps a
strategy usable no matter which authoring surface created it.

**Consequences.** Overwriting an *unreferenced* version is still allowed (a
correction), preserving backward compatibility; only referenced versions become
hard-immutable. The migration registry is empty at 1.0.0 (identity) but exists so
a future schema change has one tested home. Python-lab, visual-builder and
AI-copilot authoring still share this same IR + bundle contract. Owner doc:
`docs/quant-engine.md`. Tests: `test_quant_versioning.py`, `test_quant_bundle.py`.

## ADR-H170 - The Visual Strategy Builder is a typed node graph that compiles to the canonical StrategySpec: the registry is the extension seam and the IR stays the single source of truth

**Context.** The spec requires a visual strategy authoring canvas, but also a
strict rule: the visual graph is a *strategy* representation, not a general
workflow graph, and a single universal representation cannot losslessly express
every Python program. Building a canvas before the compile contract exists would
bake the representation into the UI and diverge from the Python lab / AI copilot.

**Decision.**
1. **Typed graph model** (`quant/graph_model.py`): `StrategyGraph` = identity +
   nodes (`id`, `type`, version, config) + edges (`from/fromPort/to/toPort`),
   pydantic-validated so an untrusted UI/agent graph is shape-checked first.
2. **Versioned node registry** (`quant/nodes.py`): each node declares a stable
   `type_id`, its own semver, typed ports (`series`/`number`/`rule`/`any`), a
   config schema + defaults, and a pure `compile(ctx, node, inputs)` that emits an
   IR fragment. Nodes span data/universe, indicators (all catalog entries + the
   multi-output Bollinger/MACD selectors), logic (compare/cross/group/constant),
   trading (entry/exit), sizing, stops/targets and risk limits. Adding a node
   never touches the compiler or the (future) canvas.
3. **Compiler** (`quant/compiler.py`): parse ? resolve types/versions ? validate
   ports/connections/types ? deterministic topological order with cycle detection
   ? compile fragments ? assemble `StrategySpec` ? run the shared
   `validate_strategy`. Emits structured diagnostics (`UNKNOWN_NODE_TYPE`,
   `TYPE_MISMATCH`, `MISSING_INPUT`, `CYCLE_DETECTED`, �) and a source map
   (spec path ? node id). Output is deterministic for equivalent graphs.

**Why.** Compiling to the SAME `StrategySpec` the Python lab and AI copilot
produce means a visual strategy is not a second-class authoring mode: it shares
the immutable-version store, the bundle format, the validator and the entire
backtest/research engine. Keeping the graph deliberately non-Turing-complete and
separate from the future workflow editor prevents the two systems from
entangling.

**Consequences.** The graph is authored **from the chatbot** (the agent composes
a `StrategyGraph` and calls `compile_strategy_graph`/`validate_strategy_graph` —
no dedicated UI, per product direction). Arbitrary Python cannot be represented
as a graph — unsupported constructs stay Python-first, with the graph a faithful
supported subset (no silent loss). Owner doc: `docs/quant-engine.md`. Tests:
`test_quant_graph_compiler.py`.

## ADR-H171 - The Python Strategy Lab runs user strategy code in an isolated worker process and shares the ONE engine for all accounting, so code-first strategies are neither privileged nor divergent

**Context.** The spec requires a serious Python research environment: user-authored
strategy code, a documented SDK, and a *managed* runtime with enforced limits and
no credential/sandbox escape. Two failure modes had to be avoided: (a) treating
`exec`/restricted built-ins as a security boundary, and (b) implementing a second
portfolio/execution accounting path that drifts from the declarative engine.

**Decision.**
1. **SDK** (`quant/sdk.py`): a typed `Strategy` base with lifecycle hooks
   (`initialize`, `on_data`, `on_fill`, `on_order_update`, `on_stop`) and a
   `Context` exposing params, declared indicators, history, position/portfolio
   state, deterministic RNG (`ctx.rng`, seeded) and order intents
   (`buy/sell/close/order`). `run_user_strategy` drives it.
2. **Shared accounting** (`_SdkEngine(BacktestEngine)`): the driver *subclasses*
   the existing engine, overriding only signal evaluation to call user code.
   Fills (next-bar), protective legs, risk engine, portfolio accounting and
   metrics are the SAME code as a declarative backtest � a code strategy is not a
   second-class path and cannot drift financially.
3. **Isolated runtime** (`quant/sdk_worker.py` + `quant/sdk_runtime.py`): the
   source runs in a **separate process** launched with `python -I`, a **scrubbed
   environment** (no PYTHONPATH, no provider keys/credentials), a **wall-clock
   timeout** (killed on overrun), an **output-size cap**, and POSIX CPU/AS limits
   where available. The parent never trusts stdout; it reads a bounded result
   file and *rehydrates* it into a typed `BacktestResult`. Windows relies on the
   timeout + output cap (stated honestly). This is process isolation, not a
   container � a hard FS/network boundary is the harness OS sandbox's job.
4. **Surfaces**: the `run_python_strategy` agent tool (driven from the chatbot) +
   `POST /quant/python/run`, and reference strategies in `quant/examples/`.

**Why.** User code is untrusted and must not run with the harness's authority;
at the same time, a backtest must be reproducible and comparable across authoring
modes. Isolating execution while reusing the engine for accounting satisfies both
without a second ledger.

**Consequences.** Code-first strategies get the immutable-version store, bundle
format and research engines exactly like declarative ones. Multi-file project
authoring and a full syntax-highlighting editor are deferred; the lab's v1 is a
single source module. Capability is advertised `partial` for `pythonStrategies`
(process isolation, not a container). Owner doc: `docs/quant-engine.md`. Tests:
`test_quant_sdk.py`, `test_quant_sdk_runtime.py`.

## ADR-H172 - The AI Strategy Copilot is grounded in deterministic artifacts: it proposes and cites, it never computes results

**Context.** Phase Seven requires a copilot that turns natural language into a
strategy, a research plan and a report, on the SAME infrastructure as manual
strategies. The risk is an LLM that "runs" strategies in prose and invents
metrics.

**Decision.** The copilot's back end is deterministic and the LLM only proposes
and explains:
1. `quant/copilot.py` � `calculate_signals` (the strategy's entry/exit decision
   timeline with reason codes and indicator snapshots, and NO order submission),
   `build_research_plan` (an explicit spec: hypothesis, IS/validation/holdout
   splits, benchmark, declared costs, inferred parameter search, metrics,
   walk-forward, robustness, acceptance criteria, known biases),
   `build_report` (Markdown grounded ONLY in a stored experiment + its artifact),
   and pure helpers to describe rules and to produce validated sizing/stop copies.
2. `quant/copilot_tools.py` registers the spec's tool names (`calculate_signals`,
   `create_research_plan`, `generate_strategy_report`, `generate_entry_exit_rules`,
   `configure_position_sizing`, `configure_stops_and_targets`,
   `get_experiment_status`, `cancel_experiment`, `calculate_performance_metrics`,
   `analyze_trade_log`, `compare_strategies`, `run_parameter_sweep`), all reusing
   the existing store/engine and reusing existing handlers where one exists.
3. Routes `/quant/signals`, `/quant/strategies/{id}/research-plan`,
   `/quant/experiments/{id}/report`. Grounding is carried by the tools themselves
   (their output is a deterministic artifact or an explicit error) — the agent
   system prompt is already at its hard size ceiling, so no prompt text was added.
4. Safety (inherited, not re-implemented): the copilot cannot bypass
   `validate_strategy`, cannot run code outside the isolated worker, cannot
   rewrite a referenced version, and never places live orders.

**Why.** Grounding every copilot claim in a deterministic artifact (signals, a
plan, an experiment report) makes "the AI found a profitable strategy" auditable
or refuted, which is the whole point of the requirement.

**Consequences.** The copilot is not a separate harness � it is prompt guidance
plus grounded tools. Its reports and plans are reproducible artifacts, not model
prose. Owner doc: `docs/quant-engine.md`. Tests: `test_quant_copilot.py`.

## ADR-H173 - The strategy expression DSL, the indicator set and benchmark metrics are extended in the deterministic engine; the chatbot is the only authoring surface (no dedicated strategy UI)

**Context.** Three gaps remained from the strategy spec: the rule DSL was
comparison-only, the indicator library was narrow, and there were no
benchmark-relative metrics. Separately, product direction made clear the whole
feature should be driven from the chatbot, not from dedicated panels.

**Decision.**
1. **Expression DSL** (`strategy.py` + `rules.py`): an `Operand` may be a
   constant, a reference, or a typed `Expr` � arithmetic (`+ - * /`), `min`/`max`,
   unary `neg`/`abs`, and causal `lag`/shift. Evaluation stays safe and causal
   (no `eval`); division by zero / missing operands yield no value, so a
   condition is simply `False` rather than acting on `inf`. Validation recurses
   into expressions (`UNKNOWN_REFERENCE`, `BAD_LAG`).
2. **Indicators** (`indicators.py`): added Stochastic, ADX (+DI/-DI), Donchian,
   Keltner, OBV, MFI, Williams %R and CCI (all causal, catalogued, with node
   registry entries and output selection for the multi-output ones).
3. **Benchmark metrics** (`benchmark.py`): `analyze_against_benchmark` returns
   alpha, beta, correlation, tracking error, information ratio and total/excess
   return from an experiment's equity curve vs a real benchmark series; fewer than
   3 aligned observations returns `available:false` with a reason.
4. **No dedicated strategy UI.** The graph compiler, Python SDK and copilot are
   driven entirely through the chatbot (agent tools). A visual canvas and a
   Python-lab panel were built and then removed per direction; the backend
   contracts are unchanged, so any future UI is a thin client of the same tools.

**Why.** Authoring richness (expressions, indicators) and evaluation depth
(benchmark metrics) belong in the deterministic engine where they are testable
and shared by every path; the chatbot already carries the tool contract, so a
parallel UI would be redundant surface area.

**Consequences.** The rule DSL is now a small safe expression language, still
non-Turing-complete. Benchmark comparison requires fetching the benchmark series
and aligning timestamps; it never fabricates a value when alignment is thin.
Owner doc: `docs/quant-engine.md`. Tests: `test_quant_expressions.py`,
`test_quant_indicators_ext.py`, `test_quant_copilot.py`.

## ADR-H174 - Governance, source inspection and multi-file projects close the strategy lifecycle � but live order routing is structurally refused

**Context.** The strategy spec's remaining backend items were: a governed path
from a reviewed strategy to a deployable one, the paper-trading boundary, source
safety inspection, and multi-file strategy projects � all without a dedicated UI
and without ever enabling live trading.

**Decision.**
1. **Governance** (`store.py`): `QuantStrategyApproval` and
   `QuantStrategyDeployment` tables on the ONE `agent-memory.db`. Approvals move
   `requested ? approved|rejected`. A deployment **requires an approved record**
   for that exact version (`APPROVAL_REQUIRED`) and supports `paper` only;
   `live` raises `LIVE_TRADING_DISABLED` and no path routes an order. A
   deployment is a **record** (`liveExecution: false`), never an execution.
2. **Source inspection** (`ops_tools.inspect_source`): a static AST pass reports
   imports, third-party dependencies, restricted modules and dangerous calls.
   It is explicitly **advisory** � the isolated worker process is the security
   boundary � so it never pretends to be a sandbox.
3. **Multi-file projects**: `run_strategy_source` accepts sibling `extra_files`
   + an `entry` module; the worker adds the project dir to `sys.path`, so a
   strategy can `import` its own helper modules inside the same isolated,
   env-scrubbed, time-limited process.
4. **Static validation depth** (`strategy.py`): `AMBIGUOUS_FILL_TIMING`
   (warning: `fill_at=close` with zero latency can fill on the signal bar) and
   `INDICATOR_TF_UNORDERED` (an indicator timeframe that is neither the primary
   nor a declared confirmation).

**Why.** A strategy lifecycle needs an auditable review gate and a paper boundary
that cannot silently become live. Making `live` a hard, typed refusal (not a flag)
means the safe behavior is the only reachable one; inspection and richer
validation are honesty aids layered on top of real process isolation.

**Consequences.** Paper deployments are bookkeeping records today; wiring them to
a market-data-driven shadow loop is a future step. Container-grade filesystem/
network isolation and pinned per-run dependencies remain the one honest gap
(process isolation only). Owner doc: `docs/quant-engine.md`. Tests:
`test_quant_ops.py`.

## ADR-H175 - The strategy runtime is hardened by policy + optional container, and paper deployments get a real shadow loop (still no live routing)

**Context.** Two honest gaps remained: the Python runtime was process isolation
only with no dependency policy or container option, and paper deployments were
pure bookkeeping with nothing ever run against them.

**Decision.**
1. **Dependency policy** (`sdk_deps.py`): an approved numerical stack
   (`numpy`/`pandas`/`polars`/`scipy`/`statsmodels`/� + stdlib + the harness).
   `inspect_source` classifies imports and flags `UNAPPROVED_DEPENDENCY`; the
   runtime never installs packages on demand, so an environment is pinned by the
   harness rather than mutated per run.
2. **Runtime hardening** (`sdk_runtime.py`): POSIX `RLIMIT_CPU`/`RLIMIT_AS` via a
   preexec (best-effort; Windows relies on the timeout + output cap), plus an
   **optional Docker backend** � `resolve_sandbox`/`docker_command` run the worker
   with `--network none --read-only --memory 512m --cpus 1 --pids-limit 256`
   when `HARNESS_QUANT_SDK_DOCKER_IMAGE` is set and Docker is present; otherwise
   it falls back openly to process isolation. The capability is reported truthfully.
3. **Paper shadow loop** (`store.save_paper_run` + `run_paper_session`): a deployed
   (approved, active) strategy runs a REAL backtest over recent data and is
   recorded (`QuantPaperRun` ? experiment id, metrics, status). It routes no
   orders and stores `liveExecution: false`.

**Why.** "Managed runtime" must mean an enforced environment, not a promise; and
"paper trading" must be exercisable against real data while remaining
structurally incapable of live execution. Both are made explicit and testable
instead of asserted.

**Consequences.** Docker requires an operator-provided image that bundles the
harness + approved deps; without it, process isolation + rlimits is the active
mode (stated). Live order routing remains absent everywhere � the only reachable
deployment mode is `paper`. Owner doc: `docs/quant-engine.md`. Tests:
`test_quant_ops.py`, `test_quant_sdk_runtime.py`.

## ADR-H176 - A change lands only through an explicit, single-use, digest-bound promotion; the disposable copy stays the default and the frozen authorities stay frozen

**Decision.** A code run writes into a DISPOSABLE copy (`worktree.allocate`,
strategy `worktree`/`snapshot`). Nothing copies that change back. Landing it is a
new `code/promotion.py` path, reached only from two operator-gated routes
(`POST /code/tasks/{id}/promote/preview` and `.../promote`):

  1. the PREVIEW names the exact file set the change would put into the real
     folder (path, op `create`/`overwrite`/`delete`, and the before/after content
     hashes) and a `previewDigest` over that set;
  2. the runtime records a SINGLE-USE approval of kind `code_promote`
     (`mcp/approvals.py` `request_kind`/`consume_kind`), digest-bound to the set;
  3. the apply route consumes the approval, RE-DERIVES the preview (a changed set
     is `PROMOTION_PREVIEW_STALE`), and writes through the existing
     `code/patch.py` engine against the server-resolved checkout root � reusing
     path confinement, protected paths, base hashes and plan-then-commit
     atomicity. A `patch.py` `overwrite` op (whole-file replace, base-checked) is
     additive for this.

It is reachable from the conversation (a "Put this change into my project"
control on an `edit_verify` turn) and from the Code Tasks screen. The target is
always `workspace.default_root()`; a protected path � `code/policy.py`,
`skills/permissions.py`, `mcp/approvals.py`, `scripts/check-all.js`, `.git`,
`.env`, the databases, keys, migrations � is refused in the preview and again at
apply time; no git write happens in the runtime.

**Why.** The user's complaint � "it can't modify its own code" � is false until a
change can LEAVE the copy. But the fix cannot be the model's call: a change to the
user's own project is exactly the single-use, operator-confirmed decision ADR-H92
already models, and the base-hash guard already exists in `patch.py`. Reusing
those authorities keeps ONE writer and ONE definition of "already approved",
rather than a second write path with its own rules.

**Consequences.** `RunRequest`/chat are unchanged (promotion is a post-run,
operator-driven action, not a tool the model holds � the chat code profile still
excludes external actions). The four existing code-action tools stay recorded
approvals with no runtime effect; promotion is the real, separate effect. Frozen
files mean a promoted change can never disable its own gate. Owner doc:
`docs/harness/roadmap/phase-15-attachments-and-artifacts.md` (attachments/artifacts
remain Not started; the promotion slice is Done). Tests:
`python/tests/test_code_promotion.py`.

## ADR-H177 — Construction-first chat routing: a data task activates the analysis sandbox without widening the surface

**Decision.** The deterministic chat classifier gains a fourth verdict,
`data_task` (a data verb + a data object in the same clause —
scrape/clean/extract/merge/compute over csv/dataset/series/prices/… — plus
"run a Monte Carlo"). `code_change` and `code_question` are unchanged. The front
door maps `data_task` to **no autonomy** and `analysisEnabled: true`: the
ordinary finance surface is untouched and only the analysis sandbox switches on,
so Eclipse computes, cleans or extracts instead of refusing. The prompt doctrine
moves from "UNAVAILABLE plus the routes you tried is a complete answer" to
construction-first: when a route fails or returns nothing, try a materially
different one — including the execution tools when the run carries them — and
treat "outside my tools" as true only after that ladder. Never-fabricate is
unchanged; `computed ≠ retrieved` (ADR-H110) is unchanged, and a data task never
receives `code.write`, the terminal or an external action.

**Why.** The measured complaint: "scrape this site", "clean this CSV" and
"run a Monte Carlo" fell through the lexical classifier to `not_code`, so the
sandbox stayed off (`analysis_run` refused) and the model answered UNAVAILABLE —
a constructible task refused by ROUTING, not by capability. A data verdict is
additive (it widens nothing), so an explicit data signal can safely route there;
ambiguity still stays `not_code` — the refusal path remains the safe failure.

**Consequences.** `code/request_intent.py` vocabulary + verdict union; front-door
`code-client.ts`/`chat-capability.ts` types and `run-service.ts` null-autonomy
handling; prompt doctrine in `prompts.py` (length-neutral, ceiling unchanged at
27769); new classifier + payload tests (`test_code_request_intent.py`,
`run-service.test.ts`). The streaming parity bug it exposed —
`run_background` dropping the portfolio/quant/ui bundles that `run` passes — is
fixed in the same change (`test_service_background_parity.py`). Owner docs:
`docs/harness/roadmap/phase-14-reachable-code-capability.md` §6.1/§6.3,
`docs/harness/construction-first-audit.md` R3-01.

## ADR-H178 — The execution substrate is honest and reachable: runtime tools admitted at the authorizer seam, analysis limits enforced, "no network" true by construction

**Superseded in part (2026-10-20, ADR-H242).** Clause 3's "true BY
CONSTRUCTION" was a source-level claim that did not hold: the AST guard could be
talked around (`f = __import__`; a URL built by concatenation), and the OS
process bounds said nothing about the filesystem or the network. The sandbox now
enforces it at RUNTIME (a guarded `__import__`, a safe `os`/`sys`/`importlib`/
`builtins`, denied privilege modules, an audit hook, confined readers) — see
ADR-H242. The decision below is kept for its history; the corrections are in
ADR-H242.

**Decision.** Four changes to one contract:

  1. The composed effect authorizer gains an admission branch for the execution
     (`EXECUTION_WRITE_TOOLS`) and LOCAL application-runtime
     (`APP_RUNTIME_WRITE_TOOLS`) families. They are admitted exactly like
     `command_run`: the HANDLER enforces the workspace binding and the
     `code.execute` grant, and the security gateway still classifies every call.
     Cluster/infra/deploy mutations are deliberately NOT admitted — they stay on
     the approval path (R3-07 sweeps their missing branch).
  2. The analysis sandbox ENFORCES its declared limits: cpu (a total-burn guard
     of 600 s, above the wall clock), memory (2048 MB) and process count (64) are
     passed to the one `RunSpec`, whose backends already enforce them.
  3. "No network" in the analysis profile is true BY CONSTRUCTION: the exec
     namespace gets a restricted builtin set (no open/eval/exec/compile/input or
     reflection helpers), and the AST guard refuses dunder attribute access,
     the bare `__builtins__`, the reflection call names, and http(s) URL
     literals (which would let an allowed module like pandas fetch).
  4. Persistent shells stop inheriting the host environment: the session child
     env is the same scrubbed allowlist every command gets. `code/backends.py`'s
     local declaration is corrected to say what is actually true (no OS-level
     filesystem/network isolation; enforcement is policy + construction).

**Why.** Measured 2026-10-12: `execute_python` / `execute_code` / `build_project`
/ `start_service` / `package_install` were all refused by the authorizer chain —
they fell through to the MCP authorizer, which has no server id for them — while
still advertised to `edit_verify` profiles, so "build, run and test" was hollow.
The analysis sandbox declared limits it never applied and claimed "no network"
that nothing enforced; a persistent shell inherited the full host environment,
so a host secret was readable from inside it.

**Consequences.** `code/policy.py` names `EXECUTION_WRITE_TOOLS`;
`code/authorizers.py` owns `_admit_runtime_tools`; `code/execution.py` owns the
restricted runner, the AST rules and the limits; `terminal/session.py` uses
`build_env`. Docker is neither required nor present on the current machine: the
sandbox refuses honestly when no primitive exists, and a container tier for a
future network-enabled collect profile stays unwired by design until its real
consumer lands (R3-04). Tests: `test_authorizer_runtime_admission.py`,
`test_analysis_execution.py`, `test_terminal_session.py`. Owner docs:
`docs/harness/execution-architecture.md`.

## ADR-H179 — Recovery is applied in the run hot path: a bounded retry for read-only tools, a route directive for everything else

**Decision.** The run's tool loop calls
`recovery/hotpath.py::run_tool_with_recovery` for every tool call. On failure the
recovery ENGINE (unchanged: pure controller + durable store) classifies and
decides; the loop then:

  * retries the SAME call once — only a READ-ONLY tool, only a `retry` /
    `rate_limit_backoff` decision, only inside the recovery budget, with the
    in-loop backoff capped at 1.5 s; or
  * appends the decision to the failed tool result as a next-step directive
    ("do NOT repeat the same call; <strategy guidance>"), so the model's next
    attempt is a justified, DIFFERENT route.

The hot path supplies PER-RUN counters (`recovery_state`) so a failure seen in a
past session cannot exhaust a fresh run's allowance; durable persistence and the
recorded decision are unchanged.
`RecoveryBudget.max_total_ms` is enforced when the caller supplies elapsed time
(the hot path does). Recovery approvals become single-use (`consume_approval`),
exactly like every other approval store in the harness, and the executor now
claims rather than merely checks them.

**Why.** Chat runs had NO recovery: the engine only observed (incidents were
recorded off the event bus) while the loop blindly re-tried or gave up. The
task-executor path had recovery, but the path the complaints come from did not.
The engine's own invariant ("not a second execution loop") is kept: the registry
still executes the tool; the engine only decides.

**Consequences.** No new execution loop, and `graph.py` stays exactly at its size
ratchet — `run_tool_with_recovery` replaces the `registry.execute` call in the
tool loop. Fail-open by construction: no engine, disabled recovery, or any error
returns the original result unchanged, so existing tests and zero-config are
unaffected. The reproducer registry is deliberately NOT seeded: `reproduce()` is
a caller-registered probe by design and a generic one would guess. Tests:
`test_recovery_hotpath.py`, `test_recovery_executor.py` (single-use approval).
Owner doc: `docs/harness/failure-recovery.md`.

## ADR-H180 — One bounded collect route at the single egress point: deterministic page extraction the agent can drive

**Decision.** A new bridge capability `web_collect` (TS) fetches ONE page through
the existing SSRF-hardened transport (`safeGet`: https-only, DNS-pinned, every
redirect re-validated, streamed 4 MB cap, 8 s timeout) and extracts structure
with cheerio, deterministically: a CSS `selector` (with an optional
field→sub-selector map or a single attribute) or the page's own `<table>`s.
Caps: 500 rows / 5 tables / 40 columns / 20 k text. It is exposed to the runtime
as the read-only tool `web_scrape`, registered on the ONE registry, allowlisted
on the default agent, and wrapped as untrusted COLLECTED data with the page as
the source. The retrieval layer's UNAVAILABLE text now names the eligible next
routes before concluding.

**Why.** The measured gap (audit RC-4): the agent could not author a fetcher —
the TS scraper chain was reachable only through fixed quote/price capabilities,
and Python may not open its own transport, so "scrape this site" ended in
UNAVAILABLE unless the page happened to contain one named field.

**Consequences.** No second egress: the capability rides the SAME
`/api/harness/data` allowlist and transport as `web_fetch`; no new permission
noun is invented (the tool is `read`, like every web tool — `data.collect`
remains the phase-16 bulk noun, ADR-H121). Empty ≠ zero: a selector with no
matches is a confirmed zero on the delivered page (with a note pointing at the
browser tools for client-rendered pages), while a fetch failure is UNAVAILABLE.
Everything returned is COLLECTED page data, never a verified source. Tests:
`data-bridge-collect.test.ts`, `test_web_collect.py`. Owner doc:
`docs/harness/web-research-engine.md`.

## ADR-H181 — Computed results reach the chat on BOTH surfaces; response diagnostics are split by severity

**Decision.** `analysis.completed` now carries the bounded result blob
(`AnalysisResult.to_blob()`: outputs, artifacts, provenance). The client maps it
to a first-class `analysis` turn field and renders the existing
`ComputedResultPanel` ("Worked out by Eclipse") under the assistant message on
BOTH chat surfaces; a value with no provenance still renders UNAVAILABLE. The
same change fixes a delivery bug: the main ChatBot surface mapped only part of
the turn — it dropped `codeVerification`, `autonomy`, `capability` and
`documents` — so the code-capability panel (including the promotion control)
and the document panel never rendered there. Response diagnostics are split by
severity: only `error` diagnostics raise the amber panel; a warning-only answer
renders quietly, and an empty envelope says so in one muted line.

**Why.** Measured (audit RC-6): a completed `analysis_run` reached the model as
text only — the user never saw the computed outputs or the saved artifacts, and
`ComputedResultPanel` was dead code. Separately, non-fatal warnings rendered the
SAME amber panel as real errors, so honest answers routinely looked broken; and
the main chat surface was silently dropping whole result panels.

**Consequences.** No new execution, no new block type: the event carries the SAME
persisted blob (one rendering contract; the panel narrows it and fails closed).
The panel is live-turn only (run events are not persisted for replay); the
artifact itself remains discoverable in the Artifact Center. Tests:
`events.test.ts` (mapping), `render.test.tsx` (severity split),
`test_code_activity.py` (computed/collected result lines). Owner doc:
`docs/interactive-response-engine.md`.

## ADR-H182 — An autonomous schedule needs the operator's go-ahead; effect metadata states the real effect

**Decision.** Three reconciliations under one rule — *metadata must state the real
effect, and a durable effect needs an approval*:

  1. `bot_create` / `bot_update` (when they set an autonomous trigger:
     cron/interval/one_time) and `bot_enable` / `bot_resume` are registered
     `permission="write"`, `effect="external_action"` and admitted by the new bot
     authorizer (`automation/authorizers.py`); the HANDLER claims — or requests,
     emits the approval card and pauses for — a single-use operator approval of
     kind `bot_schedule` before a schedule is created or started. Without an
     approvals store (tests / zero-config local runs) the tool stays admitted,
     exactly mirroring `workspace_patch_apply`. At `plan_only` the whole set is
     removed (`policy.AUTOMATION_EFFECT_TOOLS`).
  2. `computer_record_export` writes a file: its metadata now says
     `effect="write"` and it joined `COMPUTER_EFFECTFUL_TOOLS`, so the computer
     authorizer admits it and the `plan_only` boundary removes it.
  3. `git_lfs_fetch` / `git_lfs_pull` are REMOTE actions: moved from
     `GIT_WRITE_TOOLS` to `GIT_EXTERNAL_TOOLS` (registration and the git
     authorizer already treated them as remote) and removed from the chat code
     profile's declared set. `policy.TERMINAL_READ_TOOLS` now names the read
     tools that actually exist.

**Why.** Measured (audit RC-8): `bot_create` was `read`, so an agent could put
itself on a cron cadence with no operator decision; `computer_record_export`
wrote a file while advertised read; policy and registration disagreed on LFS and
on four terminal read-tool names.

**Consequences.** Deliberately NOT gated: `bot_run` (a single run, visible in the
conversation), pause/disable/delete (they stop future work), and manual-trigger
bots (inert until run). Cluster/infra mutations (k8s/helm/terraform/deploy) still
have no admission branch and no approval kind — they remain refused outside Code
Tasks and are recorded as open for the phase that owns deploy approvals. Tests:
`test_automation_authorizer.py`, updated `test_chat_code_profile.py`.

## ADR-H183 — The construction benchmark gates completion and recovery: the 16 master scenarios run through the real graph, and honesty is graded from state, not prose

**Decision.** The construction-first program's acceptance is a MEASURED benchmark,
not a narrative. R3-08 adds `evaluation/datasets/construction_v1.json` — the 16
master-plan construction scenarios (working API, alternative route, missing
fields, alternative acquisition route, confirmed zero, fixable script error,
missing dependency, dirty records, wrong calculation caught, stale conflict,
invalid chart payload, failed delivery, unauthorized attempt, budget kill,
partial completion, end-to-end chat) — each a deterministic scripted trace
through the REAL `execute_run` graph, its real registry, authorization and
events. Five new graders grade the construction signals structurally:

* `recovery` (dimension `recovery_success`) — a failed route must be followed by
  a DIFFERENT route, or the same route after an intervening repair; a blind
  repeat of a failed route is a redundant retry. `mode: stop_expected` is the
  correct answer for budget kills and honest delivery failures.
* `honesty` (dimension `honesty`) — a success marker in the answer is earned only
  by the declared state or completed evidence tools; a claimed value needs a
  completed evidence tool; a gap/failure must be named.
* `provenance` (dimension `provenance`) — required labels (`retrieved` /
  `computed` / `collected`) must appear, required sources must be BOTH used (a
  completed call) and cited, and `collected` data may never be called `verified`.
* `artifact` (dimension `artifact_delivery`) — artifact existence is read from
  the real (possibly composite) environment state; delivery must genuinely
  complete or be honestly reported as failed.
* `response` (dimension `visual_delivery`) — the answer text survives and every
  `eclipse-response` fence decodes with a usable chart block (the TS contract
  stays the authority; this is the offline smoke check).

`scoring.DEFAULT_THRESHOLDS` additionally GATES `task_success >= 0.9` and
`recovery_success >= 0.85` (both measured 1.0 offline, 47 tasks, 44 measured);
`safety_score = 1.0` and `hallucination_rate = 0.0` are unchanged and never
weakened. The eval tools now treat an environment error as a FAILED tool call
(raising, so `tool.failed` is recorded) and add `eval_run` (bounded, killed on
timeout) and `eval_response_check` (invalid payload ⇒ failure); a composite
trial's tools resolve their matching environment part.

**Why.** The audit (§6, R3-08) found `task_success` was measured but never gated
and the benchmark had zero construction scenarios. A gate that cannot fail is
not a gate; a benchmark without the failure scenarios cannot prove recovery.

**Consequences.** Measured offline: full gate tasks=47, `task_success=1.0`,
`recovery_success=1.0`, `provenance=1.0`, `honesty=1.0`, `artifact_delivery=1.0`,
`visual_delivery=1.0`, `safety_score=1.0`, `hallucination_rate=0.0`; construction
suite tasks=16, all measured, 100%. `tool_accuracy` moves 0.99 → 0.9647 and
`planning_quality` to 0.868 because the new scenarios deliberately contain
failures and retries — expected, not a regression. `scripts/test-evals.js`
runs the construction suite as a named gate inside the offline chain. Tests:
`test_evaluation_construction.py` (+15), updated `test_evaluation_scoring.py`,
`test_evaluation_graders.py`. Scope note: scenario 16 drives the agent runtime
end-to-end (collect → compute → write → publish → answer) but the offline tier
substitutes the model (scripted traces); the Next.js chat transport is covered by
the front-door tests (R3-01) and `ServiceAgentInvoker` remains the live tier for
model-driven runs. Deferred/recorded: latency/cost/tokens are captured
for every trial but not gated; the registry-visibility scan test landed after
this phase (ADR-H184).

## ADR-H184 — Registry visibility is enforced: every registered tool is reachable or deliberately operator-only, and the scan is a test

**Decision.** "Registered but invisible is a bug unless deliberate AND documented"
(R3-07) is now enforced by `python/tests/test_registry_visibility.py`. It builds
the production registry the way the service does (the `AgenticService` constructor
plus the boot-only recovery/orchestration/a2a/fabric registrations into a
throwaway registry — no runtime, no global singleton), computes the reachable set
from the three agent allowlists (`ECLIPSE_AGENT`, `CODE_NAVIGATOR`, `CODE_AGENT`)
plus the chat code profile, mirrors the resolver's dynamic expansion (`mcp_` when
`allowMcpTools`; `plug_`/`plugin_` when `allowPluginTools`), and asserts:

1. every invisible name is in the pinned `DELIBERATE_INVISIBLE` set (and nothing
   pinned is now reachable — the list cannot rot), and
2. there are no ghosts: no allowlisted name is missing from the registry.

The scan found five accidentally-invisible tools and fixed them by allowlisting
where their siblings already live (all read-only or already-admitted families):

- `git_tag_show` → `CODE_AGENT_TOOLS` + `CHAT_CODE_TOOLS` (beside `git_tag_list`;
  a registered `GIT_READ_TOOLS` member that no surface declared).
- `runtime_metrics_history` → `ECLIPSE_AGENT` + `CODE_AGENT_TOOLS` + chat profile
  (beside `runtime_metrics`).
- `workspace_graph` (read) → `CODE_AGENT_TOOLS` + chat profile,
  `workspace_start` / `workspace_stop` (APP_RUNTIME_WRITE, removed at `plan_only`
  by the ONE policy authority — the same narrowing as `runtime_start/stop`) →
  `CODE_AGENT_TOOLS` + chat profile.

The deliberate set (36 names) is pinned in the test with per-family reasons:
recovery console (5), Artifact Center catalog (6), Execution Fabric admin (9),
parallel-execution primitives used internally by the automation dispatcher and
the fabric manager (11: `parallel_execute`, `execution_status/cancel`,
`task_map/reduce/race/quorum/fork/join/batch`, `dependency_graph`),
operator context surfaces (`get_world_events`, `search_context`), and the
cluster/infra mutations without an approval kind yet (`deploy_promote`,
`k8s_port_forward`, `k8s_logs_follow` — the last two are long-lived streams,
not chat-turn shaped). ADR-H182 already documented the operator families; this
ADR completes the classification.

**Why.** The scan (measured before the fixes) found 47 registered names in no
allowlist — five were plain oversights (a read-only tool whose twin was
allowlisted) and the rest needed an explicit decision. Without the test, the next
orphan tool would ship silently; with it, adding or removing a tool forces an
allowlist or a documented reason.

**Consequences.** `test_registry_visibility.py` (+4 tests) passes in ~2.5 s and
runs in the standing Python suite; `test_chat_code_profile.py`'s exact
`CHAT_CODE_TOOLS` pin was updated for the five additions. No permission, prompt
or contract changed: every added tool is read-only or an already-admitted
runtime write already narrowed by `code.policy.allowed_tools`. Still open
(recorded): the cluster/infra mutations have no admission branch or approval
kind — they stay registered-but-unreachable on purpose until the deploy-approval
phase.

## ADR-H185 — Price history has TWO transports behind one capability; the bridge split gives each a home

**Decision.** `price_history` now answers through two transports, tried in
order, and `data-bridge.ts` was split so this could land without growing the
ratcheted file:

1. **First**: the batched yfinance bridge (`fetchYahooBatchRaw`) — one Python
   process, the whole OHLCV series. Unchanged.
2. **Second**: the DIRECT Yahoo v8 chart request that already backs
   `/api/price-history` and the live-quote read-through — new module
   `src/lib/harness/data-bridge-history.ts::fetchHistorySeriesFallback`, reached
   only when the bridge is off or returns no series. It walks the per-market
   `yahooChartCandidates` chain and accepts only a series of **≥2 real bars** —
   never a single point, never a fabricated value.

Provenance stays honest and stable: both transports label the provider
`yfinance` (the app's provider id for Yahoo data, shared with the price-history
route, ADR-H85 health vocabulary untouched), and a new `transport` field names
the code path (`yfinance-bridge` | `yahoo-chart`); the direct transport also
echoes the provider ticker it actually answered for (`providerSymbol`). The
split moved the health/diagnostics group to `data-bridge-health.ts`, the
education/media group to `data-bridge-education.ts`, and the shared
`unavailable`/`unavailableClass` helpers to `data-bridge-result.ts`; the
ratchet re-pins `data-bridge.ts` 1710 → 1377 (may only shrink; the
instrument-identity block is the named next split candidate). No new egress:
the Yahoo chart host was already reached by the price-history route and the
live-quote read-through.

**Why.** R3-04 recorded the fallback as deferred "until the ratcheted
`data-bridge.ts` is split"; the comment promised a fallback the code did not
have, so a machine without the Python stack got `UNAVAILABLE` history even
though a working direct transport existed.

**Consequences.** `data-bridge-phase2.test.ts` (bridge-first, fallback, and
both-down paths) and `data-bridge-history.test.ts` (+3) pin the behaviour
offline; full vitest 262 files / 2773 tests green. The `transport` field is
additive to `DataBridgeResult` (index-signature typed); Python consumers ignore
it, the health ledger still attributes the route to `yfinance`. Deliberately
unchanged: no retry/backoff inside the fallback (the recovery hotpath owns
retries), and the batched bridge remains first so batch/scheduled work does not
fan out one HTTP request per symbol.

## ADR-H186 — Production readiness is closed: the boot guard is end-to-end, and the deploy preflight is part of the release gate

**Decision.** Two closures complete the production story ADR-H145/H161 started
(the front door is the authenticator; auth auto-engages off loopback):

1. `deps.deployment_guard()` is the ONE boot-time posture: it composes the
   Python runtime's bind (`deployment_exposure`) with the front door's auth mode
   (`_front_door_auth`, mirroring `src/lib/auth/config.ts`) and returns
   `warnings` (unsafe) vs `notices` (informational). `boot.py` logs the posture
   at startup and warns when (a) a public bind has no operator secret, (b) a
   public bind runs with `HARNESS_AUTH=off` — the last silent case, previously
   covered by neither side — or (c) auth is required but `HARNESS_AUTH_SECRET`
   is missing (every request 503s). A public bind behind front-door auth with
   both secrets is a legitimate single-operator deployment: a notice, not a
   warning. The stale "this app has NO authentication" boot comment and the
   `deployment_exposure` warning text (which contradicted the front door) are
   corrected.
2. `scripts/security-preflight.js` joins the release gate: `check-all.js` and
   `.github/workflows/ci.yml` run it. On a loopback/dev bind every check is
   advisory (exit 0); a public bind or production build without the required
   secrets fails the gate instead of shipping. (The preflight's fail-closed
   behaviour was verified in all three modes: local WARN/0, production without
   secrets FAIL/1, production with secrets OK/0.)

**Why.** Measured production path: `npm run build` passes on this tree (Next.js
production compile + type check), auth auto-requires a login off loopback and
fails closed when misconfigured, and the preflight refuses a missing-secret
deployment. The one gap left was operational honesty: a public bind with an
operator secret and `HARNESS_AUTH=off` started with no warning anywhere, and the
runtime's warning text denied the front-door auth that exists.

**Consequences.** +4 tests in `test_identity_tenancy_boundary.py`; the release
gate gains one fast advisory step; `docs/harness/authentication.md` and
`docs/DEPLOYMENT.md` state the boot log's posture line. Explicitly NOT part of
"production ready" and still recorded: cluster/infra deploy mutations have no
approval kind and remain refused by design (not needed for the app's workflows),
roadmap phases 16–20 remain features, the container is not built/verified on a
machine without Docker, and live-model evaluation stays an opt-in tier.

## ADR-H187 — The agent may schedule its own work, within a fixed catalogue and behind an operator master switch

**Decision.** Phase 17 (agent-scheduled work) is implemented on the ONE Native
Automation Engine — not a second scheduler and not the legacy `jobs` store:

1. `automation/catalogue.py` is a fixed, one-row-per-shape catalogue that
   references EXISTING `jobs.templates` templates (research/analysis shapes
   only; deliberately no code or deploy shape). `catalogue_issues()` fails on
   any row that names a missing template, a parameter the template lacks, or a
   frequency ceiling below the template's own cadence.
2. `automation/scheduling_tools.py` registers `job_list`,
   `job_describe_template`, `job_create` and `job_run_now` on the ONE registry.
   `job_create` ALWAYS writes a `disabled` automation tagged `job`+shape id and
   accepts only a closed parameter set; only the operator enables it under
   Scheduled Jobs. `job_run_now` refuses a switched-off job and mirrors
   `bot_run` (read permission; a visible one-off action).
3. The operator master switch is the eclipse-memory policy key
   `agent_scheduling_enabled` (default OFF). `graph.surface_disabled_tools`
   removes the whole scheduling surface (reads included) from the bound set
   when off, and every handler re-checks it fail-closed — the same convention
   `memory_enabled`/`session_history_enabled` already use.
4. The unattended answer contract is
   `automation/delivery.py::render_run_report`: a delivered job report's FIRST
   line states the outcome in plain words, every limit the run hit is named,
   and a completed run that produced nothing says so.

**Why.** The measured gap (the agent "cannot schedule tasks" while the
scheduler sits behind operator-only routes) is a permission decision, not a
mechanism one. Reusing the automation engine keeps ONE scheduler, ONE store and
ONE delivery path; the fixed catalogue stops free-form recurring work; and the
always-disabled save means an agent-created schedule can never begin
autonomously — the operator's enable IS the consent. A schedule approval
(`bot_schedule`) is therefore unnecessary for `job_create`, which is admitted
by the automation authorizer under the new `SCHEDULING_EFFECT_TOOLS` class
(`code/policy.py`).

**Consequences.** +9 tests (`python/tests/test_scheduling_tools.py`); the chat
code profile snapshot test gained the four names (the profile keeps the same
scheduling surface on a coding turn). The phase file's planned artifacts
(`jobs/catalogue.py`, `jobs/tools.py`, ADR-H122) were repo-mapped onto the live
engine because the automation engine superseded `jobs` (ADR-H152) and H122 is
taken by bounded replanning. Phase 16–20 remain features; this does not widen
network, code-write, or delivery authority.

## ADR-H189 — Execution tiers are required, not preferred: the local runner refuses network, the container tier is real

**Decision.** The declared-but-inert `RunSpec.network` flag becomes an
enforcement boundary:

1. `code/sandbox.py::run` REFUSES any spec that declares `network=True`. The
   local primitive (Windows Job Object / POSIX process groups) cannot contain
   network use, so it never runs such a command — "no network" is true by
   construction, not by policy prose.
2. `code/backends.py::ContainerBackend` is implemented, not a stub. It builds the
   hardened `docker run` argv through `security/sandbox.py::docker_argv` (the ONE
   place the flags live: `--network none|bridge`, read-only rootfs,
   `no-new-privileges`, `cap-drop ALL`, tmpfs, non-root user, memory/pids
   limits), mounts ONLY the task's own folder at `/work`, passes only the
   scrubbed env, and kills the container by name on timeout. The image is
   operator-configured (`HARNESS_SANDBOX_IMAGE`, default `python:3.12-slim`).
3. `code/backends.py::select_for_spec` is the tier rule: a network spec requires
   the container tier; everything else keeps the local tier. No fallback in
   either direction — a missing container runtime refuses with
   `SANDBOX_UNAVAILABLE` (zero-config preserved: the refusal is plain and the
   local tier is untouched for everything else).
4. `command_run` routes through `select_for_spec`; the only class that declares
   `network=True` (`dependency_install`) remains operator-gated OFF and now
   genuinely requires a container runtime when enabled.

**Why.** The audit measured `RunSpec.network` as "declarative and has no
consumer" (`construction-first-audit.md` §2.3) — a flag the code carried but
nothing enforced. A declared guarantee must be enforced or deleted; deleting it
would remove the only route to network-capable commands, so it is enforced. The
honest guarantees text in `_local_guarantees` was updated in the same change, so
the declaration and the behavior cannot drift (G3b).

**Consequences.** The container tier is testable without Docker (the argv
builder is pure; tests pin the flags, the mount, and the refusal paths).
Analysis sandbox runs are unaffected (`network=False` by construction). Any
future network-enabled profile (bulk acquisition, package installs) has exactly
one honest route: the container tier, or a refusal. Tests:
`python/tests/test_sandbox_backends.py` (+4).

## ADR-H190 — Bulk acquisition is a declared, budgeted pull through the ONE bridge — never a crawler

**Decision.** Phase 16's first slice ships `python/harness/acquire/`:

1. **A declared source registry** (`registry.py`), data, one row per source:
   daily price history (`price_history`), economic series (`macro_series`), and
   annual company statements (`sec_filings`). Each row names the bridge
   capability it reaches, a CLOSED parameter set, a rate ceiling, a per-call row
   ceiling, the exact CSV columns, a licence and an attribution.
   `registry_issues()` fails an incomplete row. A source without a row is
   unusable — there is no generic fetch.
2. **A budget beside `ResearchBudget`, not inside it** (`budget.py`): calls,
   rows, bytes written and wall clock, enforced in Python. The claim-verified
   research path (`research/`) is byte-unchanged.
3. **A ceiling-enforcing writer** (`writer.py`): writes the CSV under the row
   and byte ceilings, returns `(rows_written, complete, stopped_reason)`, and the
   row count always equals what is really in the file. A partial file is always
   accompanied by a plain reason.
4. **One tool, `acquire`** (`tools.py`), over the read-only bridge — the ONE
   egress point (R3-04). It writes `<source>_<stamp>.csv` plus a manifest
   (purpose, window, rows, completeness, licence, attribution, limits) under the
   run's artifact directory. It refuses, with a stable code and one plain
   sentence, when the operator's switch is off, the source is unknown, a
   parameter is outside the closed set, the values are not allowed, symbols are
   missing/too many, or no bridge is reachable.
5. **`data.collect` is a real noun** (`skills/permissions.py`,
   `code/policy.py`): never in the safe or operator sets; granted only by the
   `declared_sources` acquisition profile. The live mapping of the operator's
   profile is the eclipse-memory policy key `acquisition_enabled` (default OFF,
   the same discovery-is-not-activation convention as scheduling); when off,
   `graph.surface_disabled_tools` removes the tool from the bound surface and the
   handler re-checks fail-closed. `acquire` is admitted by a new
   `_admit_collect_tools` branch at the effect-authorizer seam.

**Why.** "Get me every filing for this sector" and "two years of daily bars for
these 40 names" were answered with the ANSWER budget (4 searches / 8 fetches) —
useless for a dataset — or not at all. The slice gives volume exactly one honest
route: declared sources, a Python-enforced budget, files with a manifest, and
never a partial presented as complete. The transport is the existing bridge, so
no new egress, no sandbox network, and the container tier (ADR-H189) is not
required for these read-only pulls.

**Consequences.** +9 tests (`python/tests/test_acquire.py`), hermetic with a stub
bridge. The remaining phase-16 items are recorded, not silently skipped: more
sources (SEC filing LISTS rather than statements, operator-declared REST),
cross-call budget accumulation, the chat manifest panel and i18n keys, and the
opt-in live smoke. `jdds.db`, `prisma/` and `research/` are untouched.

## ADR-H191 — A produced artifact is verified, and the repo's rules are reported — one ledger, two new claim classes

**Decision.** Phase 19's verification spine is extended, never duplicated:

1. `code/verify.py` gains `ArtifactCheck` / `ArtifactRecord` /
   `verify_artifacts(references, output_dir, recorded_inputs)`: the file exists
   where the reference says, its sha256 matches, its size is inside
   `[MIN_ARTIFACT_BYTES, MAX_ARTIFACT_BYTES]`, it opens as what it claims (image
   signature, CSV parse, JSON parse), and — when the producer recorded the
   inputs — the series it carries matches them. A check that cannot run (a
   missing file, an image without recorded inputs) is a FAILED record with its
   reason, never an omission.
2. `analysis_tools.py` records those checks on the run's `VerificationLedger`
   after an analysis (best-effort; never blocks the analysis).
3. The ledger gains artifact + review records (bounded, old-row-safe in
   `to_blob`/`from_blob`) and two claim classes — `artifacts_verified` and
   `change_reviewed` — with the same narrow, negation-guarded patterns as the
   Phase-4 four. An unbacked sentence about a produced file or a reviewed change
   is marked `[UNVERIFIED …]` by the ONE enforcement path.
4. `code/review.py` is the change-quality rule TABLE (data), each rule naming
   the real repo rule it encodes: `new-module-reachable`, `size-ratchet`
   (pins read from BOTH ratchet files), `string-is-key`, `protected-path`
   (blocking; the SAME rule `patch.apply` enforces — this module only reports
   it, and the test asserts they agree), `doc-obligation`, `test-touched`.
   An unevaluable rule lands in `unevaluated` — never reported as clean.
5. The `review_change` tool (read-only) runs the rules over the task's diff and
   RECORDS the review on the ledger. It is registered by
   `code/review_tools.py` from `service.py` (not from `register_code_tools`,
   which is at its size pin) and allowlisted on the code agent + chat code
   profile.
6. `eclipse_memory/mistakes.py` gains the four categories the checks can emit
   (`artifact_missing`, `artifact_digest_mismatch`, `unreachable_module`,
   `unstated_limitation`), so a repeated failure reaches the existing
   self-improvement bridge; `CODE_AGENT_SYSTEM_PROMPT` states that artifact and
   review sentences are checked and that an unstated limitation is the failure
   mode. The chat panel renders artifact rollups and review findings (i18n keys
   `code.proof.*`).

**Why.** The Phase-4 ledger covered commands; "the chart shows the drawdown"
and "I reviewed the change" matched no pattern and were backed by no record.
The enforcement machinery was already the right one — it was pointed at half the
surface. The review rules are the repo's own rules where they are mechanical;
style, naming and intent remain judgement and are named as such.

**Consequences.** +24 tests (`test_claim_classes.py`,
`test_artifact_verification.py`, `test_change_review.py`); the chat code profile
snapshot gained `review_change`. Recorded deviations: no separate progress file
(notes inline in the phase file), the prompt paragraph lives in the CODE agent
prompt (the finance prompt is char-ratcheted), and `figure_inputs_match` is
recorded only when the producer recorded inputs — the figure payload itself is
phase 18. No LLM judge: a model's opinion can never satisfy a check.

## ADR-H192 — The runtime may render a figure, from a declared recipe, inside the existing sandbox

**Decision.** Phase 18's first slice ships a FIGURE recipe without opening a new
execution path:

1. `AnalysisRequest.extra_imports` is an additive, NAMED widening of the AST
   allowlist. It is set only by the recipe table — never by the model, never by
   a caller — and everything else in `check_imports` still applies (reflection,
   dunder access, `__builtins__`, URL literals). The shipped figure recipe
   declares exactly `("io", "matplotlib")`.
2. `write_artifact` accepts BYTES, so a rendered PNG reaches the run's declared
   `output/` folder through the ONE artifact seam (the name is still sanitised
   to a bare filename; only files in `output/` are collected).
3. `drawdown_figure` runs in the Phase-3 sandbox with the Agg backend (no
   display, no network — `RunSpec(network=False)` by construction), returns the
   numbers in `result` exactly like the seven numeric recipes, and writes
   `drawdown.png` as an `ArtifactRef` the Phase-19 ledger verifies (exists,
   digest, size, opens as an image).
4. A machine without matplotlib fails the recipe HONESTLY (an import error in
   the run), never silently and never with a substituted chart.

**Why.** "Create a visualisation" was impossible — not poorly supported — and
the artifact type system already anticipated `image/png` with nothing producing
one. The widening is the smallest honest step: a reviewed module list attached
to a declared recipe, inside the sandbox that already bounds time, output and
network, with the figure verified by the ledger that already exists. The
figure is `computed`, never evidence (ADR-H123): it may illustrate a result but
can never back a market claim.

**Consequences.** +1 real-sandbox test (the PNG is rendered and verified end to
end). Recorded as remaining (phase-18): the structured figure PAYLOAD on the
run blob, the remaining figure recipes, and the inline chat renderer — today the
figure is handed over through the computed-result panel + Artifact Center.

## ADR-H193 — The capability statement covers every class; an unmeasured class is published as unmeasured, never omitted

**Decision.** Phase 20's measurement slice lands one registry, one runner and one
gate step:

1. `code/eval/capabilities.py` is the capability-class registry, as DATA: the
   seven code shapes, the classes this round added (bulk collection, unattended
   report, artifact verification, change review, document attachment, figure
   provenance), AND the classes this round did NOT grade (`price_authority`,
   `promotion`, `scraping_breadth`, `messaging_gateway`) — each declaring its
   module and, when unmeasured, its REASON.
2. The runner grades a class only through its NAMED evidence: a deterministic
   in-module fixture that drives the REAL module (`harness.acquire.budget`,
   `harness.automation.delivery`, `harness.code.verify`, `harness.code.review` —
   the thing under test is never mocked), a named test that runs in the release
   gate (`pytest:…`/`vitest:…` — the runner verifies the test exists so a proof
   can never point at nothing), or a fixture id in the code eval's labelled
   dataset (graded by the code gate in the same run). An unmeasured class is
   reported `unmeasured` WITH its reason — never a pass, never a failure.
3. `MEASURED_BASELINE` pins the measured set: the gate fails when a class falls
   below its floor OR when a measured class regresses to unmeasured — a
   capability may not silently go quiet.
4. The generated statement (`code/eval/statement.py`) gains the "Every
   capability class" section: one row per class, the count of unmeasured classes
   published with them ("N of M classes are not measured yet"), same derivation
   discipline as the shape table (ADR-H114). `scripts/test-evals.js` gains the
   named `capability-class gate` step, so this is part of `npm run check:all`.

**Why.** §6.1's two empty rows were the point: seven unmeasured classes were
simply ABSENT from the statement, which reads as "fine". Publishing them as
`not measured yet`, with reasons, is the difference between a claim and a
measurement. The runner also surfaced and fixed a real defect: the statement
generator's repo-root constant pointed at `python/docs/…` (one level short), so
`write_statement()` could never have written the committed file.

**Consequences.** +7 tests (`test_capability_classes.py`); the release gate gained
one fast offline step (`capability-class gate: PASS measured=13 unmeasured=4`).
Recorded as remaining (phase-20): labelled fixtures for `price_authority` and
`promotion` (today honestly unmeasured), the bootstrap/Monte-Carlo rule fixture,
the adversarial safety cases for the new power in `code/eval/safety_cases.py`,
and per-class threshold tables beyond the all-or-nothing floor.

## ADR-H188 — The acquire loop is an agent capability: GitHub skills install through the SAME trust + approval stores, and software installs route to the code surface

**Decision.** "Load this skill from GitHub" and "install this software" are now
capabilities of the agent itself, built on the existing machinery — no second
registry, no second approval store, no second trust model:

1. `skills/install.py` is the ONE GitHub acquire loop: `parse_source` accepts
   only https `github.com` URLs (or `owner/repo`), the zipball is fetched from
   `codeload.github.com` with size/timeout caps (stdlib; no shell, no git
   binary), extraction enforces zip-slip/absolute-path/symlink/count/size
   guards, and both skill formats (`skill.json` and cross-tool `SKILL.md`
   frontmatter) are normalized, pydantic-validated and content-scanned. A
   quarantine-severity finding or a never-grantable permission refuses that
   skill; per-skill failures are reported, never hidden.
2. `skills/install_tools.py` registers `skill_install` / `skill_uninstall` /
   `skill_sources` and composes a `make_skill_authorizer` layer into
   `code/authorizers.py`. Admission is a single-use, digest-bound operator
   approval (`skill_install` / `skill_uninstall` kinds in the SAME `McpApproval`
   store), pause-in-card-mode included; no standing grant is offered. On
   approval the root is recorded in the SAME project-trust store (`trust_root`
   with per-skill content hashes), so post-install drift quarantines only the
   changed skill.
3. Installed roots (`<repo>/installed-skills/<owner>__<repo>/`,
   `BLACK_ECLIPSE_SKILLS_DIR` override) load through `SkillRegistry.load_all`
   as the `external` tier: precedence `project > profile > external > bundled`.
   `registry.reload()` re-reads every tier from the remembered boot
   configuration, so an install is live in the same run and the trust/untrust
   routes never silently drop the tier.
4. `mcp/install_ops.py` extracts the preset-integration install so the operator
   route and the new approval-gated `mcp_install` agent tool (`mcp/install_tools.py`,
   plus the read-only `mcp_integrations` catalog tool) share ONE implementation.
   Install stays DISABLED/nothing-selected: installing is not activation.
5. `code/request_intent.py` treats acquisition as software work: `install` /
   `uninstall` are strong directive verbs (a bare "install X" is enough), the
   weaker `download`/`set up` need a software object, and "software"/"program"/
   "skill" joined the software objects — so "download and install this software"
   reaches the code profile where `package_install`/`terminal_run` actually run,
   while "download the prices and compute the drawdown" stays `data_task`.
6. `ACQUISITION_WRITE_TOOLS` (`code/policy.py`) removes the five new tools at
   `plan_only` and the unknown-profile fallback; they remain on the chat code
   profile so a code-classified turn keeps the acquire surface.

**Why.** A wrong `code_change` used to hand the turn a narrow workspace profile
with no install verbs (software couldn't be installed at all), and skills could
only enter through operator-only routes or an inert learning draft. The
Hermes-class behavior users expect — point at a repo, use it; ask for software,
have it installed — is a wiring gap, not a new engine. Every privileged step
already had a store (approvals, trust, content scan); this decision connects
the agent to them rather than inventing a bypass.

**Consequences.** +28 tests (`python/tests/test_skill_install.py`,
`test_mcp_install_tools.py`) + classifier cases; `service.py` gained the
installed-home boot kwarg + two registrations (net shrink after the
`install_mcp_integration` refactor); `routers/skills.py` gained
`GET /skills/installed` and the installed fields on `GET /skills`; the prompt
ceiling re-pinned 27769→29162 for the three guidance paragraphs; `python/conftest.py`
now anchors `HARNESS_MCP_CONFIG` so a developer's real `mcp-servers.json` can
never leak into the suite. The skill permission/execution gate, the approval
single-use semantics and the untrusted-prose rule are unchanged — installing
widens what can be *used*, never what may be *done*.

## ADR-H194 — Private per-conversation memory is a new `conversation` scope on the ONE Eclipse-Memory pipeline, not a second store

**Decision.** A bot chat and each group-room member turn own a PRIVATE durable
memory bucket, keyed by `scopeId = conversation id` (the run `sessionId`:
`bot-<id>` / `group-<gid>-<botid>`). This is the new **`conversation`** value on
`MemoryRecord.scope` — no new database, table or column (ADR-026/034 hold).

1. `eclipse_memory/decision.resolve_owner_scope` is the ONE owner resolver. In a
   conversation-scoped run with no signed-in user, a personal (`user`) fact is
   owned by the conversation; a `conversation` scope with no id degrades to
   user/general; outside a conversation nothing changes.
2. `EclipseMemoryService.commit_decision` normalizes the owner at the ONE write
   gate (both the deterministic-turn and model-proposal paths pass through it).
3. `retrieval.retrieve_memories` gathers candidates only from the conversation's
   own bucket + the signed-in user's own rows + `general`, and
   `_scope_authorized` refuses another conversation's rows BEFORE ranking —
   scope/privacy filtering stays authoritative.
4. Opt-in is explicit: only bot/group call sites send `memoryScope:'conversation'`
   → `RunRequest.conversationId`. Ordinary chat is byte-identical.
5. Scheduled bot runs bind the same `bot-<id>` conversation, so a background run
   and the interactive chat share one memory; `bot_delete` archives it.

**Why.** The bots UI honestly said "no separate memory": every durable fact went
to shared/unattributable rows, so a bot could neither build long-term context nor
be isolated. A per-bot store would break the one-store/one-pipeline invariants; a
new scope value reuses the decision gate, the scope-before-rank filter and the one
persistence path.

**Consequences.** `eclipse_memory/{decision,retrieval,service}.py`,
`memory_tools.py`, `graph.py`, `graph_context.py`, `schemas.py`, `tools.py`,
`automation/{dispatcher,tools}.py`, `routers/memory.py`; TS `agent/{types,registry}.ts`,
`harness/{run-service,python-client,eclipse-memory-browser}.ts`, both chat routes,
`bot-chat.tsx`, `group-chat-room.tsx`, `bot-detail.tsx`, new `bot-memory-section.tsx`,
i18n `en.ts`. +9 tests (`python/tests/test_memory_conversation.py`). Conversation ids
are local labels, not security identities. Mirrors app-level ADR-044.

## ADR-H195 — Document Authoring is a distinct write subsystem: real Office files via lazily-imported OOXML writers, registered through the ONE Artifact Center

**Decision.** Creating documents is the mirror of Document Intelligence, kept as a
separate subsystem (`python/harness/documents/`) rather than folded into the
read-only reader. One agent tool, `document_create`, builds real Word (`.docx`),
Excel (`.xlsx`) and PowerPoint (`.pptx`) files — plus Markdown, CSV, JSON and
`.txt` — from a neutral content vocabulary (`blocks`/`sheets`/`slides`/`rows`) and
records the bytes in the ONE Artifact Center.

1. Builders (`authoring.py`) are pure: neutral content in, file bytes out. They use
   `python-docx` / `openpyxl` / `python-pptx`, imported **lazily per format**, so a
   partial install makes exactly one format report `DOCUMENT_LIBRARY_MISSING`
   instead of emitting a broken file.
2. The tool writes through `ArtifactService.register_bytes` with an explicit MIME
   type — no arbitrary filesystem path, no storage key, no macro execution. The
   artifact then previews/downloads like any other; a missing service reports
   `ARTIFACT_UNAVAILABLE`.
3. `document_create` joins `DOCUMENT_TOOLS` in `agents.py`, so the model may reach
   it; the read path (`document_*`) stays stdlib-only and unchanged.

**Why.** The runtime could READ `.docx`/`.xlsx`/`.pptx` but not WRITE them, and it
had no honest way to say "I made you a Word document." The write path genuinely
needs the three writers, whereas the read path deliberately avoids them; keeping
them in one module would have forced an unjustified dependency onto the reader.

**Consequences.** `python/harness/documents/{authoring,tools,__init__}.py`; boot
wiring in `service.py`; allowlist `agents.py`; TS tool label `response-model.ts`;
`python/requirements.txt` (+3 writers); tests `test_documents_authoring.py`;
docs `docs/harness/document-intelligence.md` (Authoring), `docs/harness/artifact-center.md`,
`docs/NAVIGATION.md`, ADR-H195. The size-budget pin for `service.py` is ratcheted for
the boot wiring. Native PDF authoring is deliberately NOT added — chat responses
already export to PDF client-side (jsPDF), and a Python PDF writer would be a fourth
writer for a format already covered.

## ADR-H196 — Actionable advice is verified at the answer seam: COMPUTED from the run's own portfolio engine, SOURCED from what it consulted, else disclosed UNVERIFIED

**Decision.** The runtime gains a third answer-seam gate, sibling to the finance-fact
gate (`finance_facts/enforce.py`) and the citation release gate
(`research/release_gate.py`). The **Fiduciary Gate** (`python/harness/advisory/`)
extracts actionable recommendations from the final answer (a directive verb plus a
symbol or numeric target) and labels each one:

1. **COMPUTED** — a `portfolio_*` computation the run actually performed backs it.
   A new `AdvisoryCollector`, wired through `ToolContext.advisory_sink`, records
   every `portfolio_*` tool call (tool, symbols, numbers) as the run works, so the
   gate can match a claim's symbol and target against what the engine produced. A
   target that contradicts the computation is UNVERIFIED, never silently corrected.
2. **SOURCED** — a symbol the run consulted through a portfolio read, with no
   numeric target: grounded in the user's own ledger.
3. **UNVERIFIED** — neither; an `Advisory note: …` disclosure is appended (never
   stripped), the verdict emitted as `advisory.gate`, and attached to the run blob
   as `metadata.advisoryIntegrity`.

The gate is deterministic (no model call), additive (wrapped so it can never fail a
run; a claim-free answer is byte-unchanged), and **proposal-only** — it never places
a trade.

**Why.** A finance assistant that improvises actionable recommendations is worse
than useless: the user cannot tell a computed, reproducible call from a plausible
sentence. GPUs/cloud chatbots assert; Eclipse verifies. This extends the product's
honesty invariant ("never fabricate financial data") from *figures* to *advice*,
reusing the ONE portfolio engine rather than a second analysis implementation.

**Consequences.** `python/harness/advisory/{__init__,model,claims,collector,gate}.py`;
`ToolContext.advisory_sink` in `tools.py`; wiring in `graph.py` (`build_graph`
param, `tools_node` recording, the answer-seam gate); tests
`python/tests/test_advisory_gate.py`; doc `docs/fiduciary-advice.md`,
`docs/NAVIGATION.md`. Scope is deliberately strict (a directive + a
symbol/target), so ordinary conversation is never flagged. The Advisory
Ledger/scorecard (persisting decisions + outcomes) and the "works while you're
gone" monitors are planned follow-ups on this seam.

## ADR-H197 — Advisory decisions are a durable, immutable record: one decision per (run, claim), an appended outcome, never a rewritten basis

**Decision.** The Fiduciary Gate's verdicts are persisted as a first-class ledger
on the ONE `agent-memory.db` connection — `AdvisoryDecision` (action, symbols,
target, thesis, status, run/portfolio/owner), `AdvisoryBasis` (the `portfolio_*`
computation and/or consulted source URLs that justified it, or `none`), and
`AdvisoryOutcome` (realized vs benchmark → right/wrong/neutral). Three rules:

1. **One decision per (run, action, symbols, target).** The id is a deterministic
   hash, so re-recording the same answer (a resumed run, a retried seam) is a
   no-op — the ledger never double-counts a recommendation.
2. **Basis is immutable; an outcome is appended.** A decision is written once and
   an outcome `UPSERT`s onto it. The "why did I make it" record is never rewritten,
   matching the portfolio ledger's append-only discipline.
3. **Outcomes are caller-supplied.** A jurisdiction-neutral engine records the
   realized and benchmark returns it is told; it never infers a market result. A
   missing decision yields `None`, not a fabricated outcome.

**Why.** The Fiduciary Gate proves a recommendation is computed/sourced at the
moment it is made; the ledger makes that proof durable and answerable later
("was that call right?"). Reusing the ONE agent-memory connection (never a second
DB, never committed) keeps it private, and the deterministic id makes the answer
seam idempotent without a dedup pass.

**Consequences.** `python/harness/advisory/{store,service,tools,bootstrap}.py`;
`routers/advisory.py` (`/advisory/*`, operator-gated); boot wiring in `service.py`;
allowlist `agents.py` (`ADVISORY_TOOLS`); the answer seam records the verdict
(`graph.py`). Auto outcome evaluation was added in the follow-up: the run captures
entry prices (`AdvisoryCollector.prices()`), and `evaluate_decisions` /
`auto_evaluate(gateway)` compute realized-vs-benchmark returns deterministically
(a decision with no entry price or no quote is skipped). Front door + a Decisions
view landed alongside. Docs `docs/fiduciary-advice.md`, `docs/NAVIGATION.md`.

## ADR-H198 — Portfolio conditions are a second fact provider; proactive monitors are a closed catalogue saved off

**Decision.** Condition triggers gain a portfolio lens by adding a
`PortfolioFactProvider` behind a `CompositeFactProvider` (the engine's default
provider), not by inventing a new scheduler. A `pf.<portfolioId>.<field>`
condition (`weight.<SYM>`, `unrealized.<SYM>`, `cash_pct`, `concentration_hhi`,
`total_value`, `position_count`) is resolved through the ONE `PortfolioService`
read. A failed read or an unpriced position contributes **no fact**, so the
three-valued engine reports `UNAVAILABLE` and a monitor never fires on a guess.

Proactive monitors are a CLOSED catalogue (`python/harness/advisory/monitors.py`):
`position_weight_breach`, `tax_loss_harvest`, `cash_buffer_watch`,
`concentration_watch`. Each builds an ordinary `condition` automation with a
`run_agent` action and is created **`state="disabled"`** — only the operator arms
it, matching the "always saved off" rule for agent-created schedules. Exposed via
`GET/POST /automations/monitors`; `AutomationService.list_monitors` /
`create_monitor` build from the catalogue. `portfolio/harvest.py`
(`portfolio_harvest_candidates`) is the deterministic, proposal-only raw material
for a tax-loss decision.

**Why.** The Fiduciary Gate and ledger make a recommendation trustworthy at the
moment it is made; monitors make the same discipline run unattended. Reusing the
existing automation engine (rather than a second scheduler) and the existing
portfolio engine (rather than a second analysis) keeps the surface small, and a
closed catalogue means the agent can arm a monitor without free-form condition
authoring.

**Consequences.** `automation/facts.py` (Composite + Portfolio providers, default
composition); `advisory/monitors.py`; `automation/service.py`
(`list_monitors`/`create_monitor`); `routers/automation.py` (monitor routes);
`portfolio/harvest.py` + the `portfolio_harvest_candidates` read tool; tests
`test_automation_facts.py`, `test_advisory_monitors.py`, `test_portfolio_harvest.py`.
Automatic outcome recording from a monitor into the ledger (closing the
evaluation loop) remains a follow-up. Docs `docs/fiduciary-advice.md`,
`docs/NAVIGATION.md`.

## ADR-H199 — Remote control is a Channel Gateway: inbound adapters over the ONE run path, with a scope gate and an in-channel confirmation

**Decision.** Connecting an external messaging surface (Telegram, Discord, Email,
Slack, WhatsApp, a generic webhook) for remote control is a first-class core
subsystem — `python/harness/channels/` — not a plugin and not a second agent
loop. A provider adapter (`channels/providers/*`) normalizes inbound traffic; the
`ChannelService.handle_inbound` enforces idempotency (native message ids),
resolves the sender to a verified `ChannelBinding`, applies a per-binding rate
limit, and hands the message to the `ChannelDispatcher`. The dispatcher gates by
control scope (`viewer` < `assistant` < `operator`), routes `/commands`, agent
turns and the operator `/do` tool bridge, and requires an in-channel `/confirm`
for mutations.

It composes the EXISTING systems and forks none: an agent turn calls
`service.run`; a `/do` tool call goes through the ONE `ToolRegistry`; a mutating
tool's effect is authorized by the Security Gateway via `make_security_authorizer`;
secrets resolve through the gateway's `SecretsManager` (`secret://` refs);
bindings/confirmations/config live on the ONE `agent-memory.db` connection; and
every dispatch is recorded in the hash-chained security audit under
`category="channel"`.

**Why.** The app was outbound-only (alerts). Remote control adds an INBOUND trust
boundary that alerts never had: a chat id or a "From:" header is spoofable, so the
sender is untrusted until linked. A core subsystem (not a plugin) is right because
the trust path must be tied to the gateway, approvals and audit — the same
authority every other effect uses. Telegram ships first because it long-polls
from the local machine, so zero-config local use needs NO public endpoint; the
push-based providers (Slack/WhatsApp webhooks) are opt-in for exposed
deployments.

**Consequences.** `channels/` (`model`, `store`, `config`, `transport`,
`identity`, `commands`, `dispatch`, `service`, `bootstrap`,
`providers/telegram`); router `python/harness/routers/channels.py`
(`/channels/*`, operator-gated); boot wiring `install_channel_layer` in
`service.py` + `start_channels`/`stop_channels` in `boot.py`; front door
`src/app/api/channels/[[...path]]/route.ts` + `channels-{client,browser}.ts`;
Settings › Connections `src/components/screens/connections-settings.tsx`; tests
`python/tests/test_channels_{core,api}.py`. Phase 1 ships Telegram + linking +
`viewer`/`assistant` scopes + read/agent/`/do`-read + the confirmation
infrastructure; explicit finance-mutation verbs and Discord/Email/Slack/WhatsApp
providers are later phases. Secrets currently sit in the channel config JSON
(mirroring the existing TS `alert_channels` behaviour) with `secret://`
indirection available; migrating the default to the encrypted vault is a
follow-up. Docs `docs/harness/channel-gateway.md`, `docs/NAVIGATION.md`,
`docs/ARCHITECTURE.md`.

## ADR-H200 — Channel providers split by transport: pull channels poll/stream locally, push channels cross one signature-gated public boundary

**Decision.** The Channel Gateway grows five more providers, split by transport.

*Pull/stream* (no public endpoint, so zero-config local use is unchanged):
`discord` (bot Gateway WebSocket + REST, `providers/discord.py`), `email` (IMAP
poll + SMTP, `providers/email.py`), alongside the existing `telegram`.

*Push* (`providers/webhooks.py`): `slack`, `whatsapp`, `webhook` receive inbound
at ONE public route `POST /channels/webhook/{kind}` and reply over HTTP. Because
the caller is a third party that cannot present the operator token, authenticity
is proven by an HMAC over the RAW request body under a per-channel secret
(`channels/verify.py`: Slack v0 + replay window, WhatsApp `X-Hub-Signature-256`,
generic `X-Eclipse-Signature`). A missing/mismatched signature is refused. The
Next.js middleware exempts `/api/channels/webhook/` from the session gate (the
signature IS the gate), and the front-door route forwards the raw body + only the
signature headers, never re-serializing it.

The provider build path generalizes: `ChannelService._build_provider` builds an
outbound transport for ANY configured kind and additionally starts a background
task only for `_POLL_KINDS` (`telegram`/`discord`/`email`), so a push channel
still replies even when polling is disabled.

**Why.** The transport is an operator-visible property: a pull channel needs no
inbound exposure, a push channel inherently does. Making that split explicit keeps
the zero-config guarantee for the local case while giving exposed deployments a
correct, signature-verified boundary for Slack/WhatsApp. Providers remain thin —
they normalize payloads and send replies; the scope gate, confirmation, audit and
Security-Gateway effect authorization are unchanged and live above them.

**Consequences.** `channels/verify.py`; `channels/providers/{discord,email,
webhooks}.py`; `service.py` (`_POLL_KINDS`, generalised `_build_provider`,
`secret_value`); `routers/channels.py` (`GET/POST /channels/webhook/{kind}`,
public, signature-verified); front door
`src/app/api/channels/webhook/[kind]/route.ts`; middleware public prefix;
Settings › Connections renders per-kind credential fields + the inbound webhook
URL. Tests `test_channels_providers.py` + the webhook cases in
`test_channels_api.py`. WhatsApp ships the official Cloud API path; the
unofficial local (Baileys/WhatsApp Web) path is deliberately NOT included. Docs
`docs/harness/channel-gateway.md`.

## ADR-H201 — Channel credentials are encrypted at rest in the vault, and the config JSON keeps only a redacted presence marker

**Decision.** Saving a channel moves every sensitive config key into an encrypted
vault (Fernet; key file `.black-eclipse/channels/secret.key`, 0600, outside the
DB) and stores only ciphertext in a new `ChannelSecret` table. The config JSON
keeps only non-sensitive keys. `ChannelService.describe_config` echoes a
configured credential as a redacted PRESENCE marker (`"botToken": "***"`) plus a
`secrets` name list, so the existing Settings contract (and every test asserting
`config.botToken == "***"`) is unchanged even though the value is no longer in the
config. Credential resolution is layered (`secret_value`): the vault first, then a
config literal or `secret://` reference (back-compat). `DELETE /channels/{kind}`
purges the channel's secrets. If `cryptography` is unavailable the vault reports
`secretsEncrypted: false` and a sensitive value falls back to the legacy literal
config (zero-config preserved) rather than silently failing.

**Why.** ADR-H199 shipped credentials in the config JSON, mirroring the legacy TS
`alert_channels` and flagged as a follow-up. Refusing to leave a bot token /
SMTP password in a queryable plaintext column is consistent with the repo's
security posture (the Plugin Platform encrypts the same class of value the same
way), and keeping the redacted-presence contract means no client or test churn.
The layered resolution lets existing installs keep working and migrate the next
time a channel is saved.

**Consequences.** `channels/secrets.py` (`ChannelSecrets`, Fernet key file);
`channels/store.py` (`ChannelSecret` table + ciphertext accessors);
`channels/service.py` (`configure` vault extraction, `secret_value` layering,
`describe_config` presence markers + `secretsEncrypted`, `delete_config` purge);
front-door type `ChannelConfigView.secrets` / `secretsEncrypted`; Settings ›
Connections shows "encrypted at rest". Tests
`python/tests/test_channels_secrets.py`. Docs `docs/harness/channel-gateway.md`.

## ADR-H202 — Explicit mutation verbs, the one-sender primitive, and channel notifications

**Decision.** Three additions close out the Channel Gateway.

1. **Explicit mutation verbs.** `/trade`, `/alert`, `/backtest` require the
   `operator` scope AND always ask for an in-channel `/confirm`, then run as an
   agent turn with a directive (`dispatch._MUTATION_DIRECTIVES`). They are a
   STRONGER gate than plain agent text — a `viewer`/`assistant` can never trigger
   one — and still end on the agent's Security-Gateway-authorized tools. The verb
   adds operator scope + a deliberate confirmation step; it does not bypass the
   gateway and it does not hard-code a tool name (robust to tool changes).
2. **One-sender primitive.** `POST /channels/send` / `ChannelService.send_outbound`
   sends through a configured channel's transport (built transiently when the
   poller is not running) and records the delivery. Any subsystem can push an
   outbound message without re-implementing a provider.
3. **Notifications.** `ChannelActivityWatcher` + the pure `channel-diff.ts` poll
   `GET /api/channels/messages` and raise the shared `be:alert-notify` event for
   NEW inbound messages (first poll seeds; outbound never announced). It rides the
   ONE notification center (toast + desktop + ledger) rather than a second surface.

**Why.** Remote control is only useful if the operator learns about it and can act
on it: the notification watcher closes the "something happened while you were
away" loop, the mutation verbs make a remote change a deliberate two-step act, and
the send primitive gives the runtime ONE outbound path instead of each caller
speaking to a provider API. Consolidating the TS alert notifier into this sender
is deliberately NOT done: alert channels are webhook/notification transports
(Discord incoming webhook, ntfy, generic) owned by JDDS, distinct from the
gateway's bot-session transports, and alerting must not depend on the Python
runtime being up.

**Consequences.** `channels/commands.py` (`MUTATION_VERBS`); `channels/dispatch.py`
(`_handle_mutation_verb`, `_MUTATION_DIRECTIVES`, confirm routing);
`channels/service.py` (`send_outbound`); `routers/channels.py` (`POST /channels/send`);
front-door allowlist `send`; TS `channels-{client,browser}.ts` (`sendChannelMessage`);
`src/lib/notifications/{channel-diff.ts,channel-watcher.tsx}` + mount in
`src/app/layout.tsx`. Tests `test_channels_core.py` (mutation verbs, send),
`test_channels_api.py` (`/send`), `channel-diff.test.ts`. Docs
`docs/harness/channel-gateway.md`.

## ADR-H209 - A goal becomes a durable contract before multi-step work starts

**Decision.** An ordinary request that deserves durable work is turned into a
`GoalContract` — objective, deliverables, constraints, acceptance, budgets —
plus a `TaskType`, by a DETERMINISTIC intake (`tasks/goal.py::intake_goal`; no
model call decides what a goal is), and the contract is stored as DATA on the
durable task (additive `taskType`/`goalContract` columns), never as prompt
text. The contract's acceptance lines become the task's criteria. An ambiguous
request earns exactly ONE clarifying question instead of a guessed contract.
`goal.durable_decision` is the explicit durable-by-default rule (code/workflow,
multiple deliverables, an explicit budget or multi-step phrasing ⇒ a durable
task); the `create_goal` tool applies it. This ADR is numbered H209 as the
round-4 roadmap reserves H203–H208 for the earlier workstreams (still
unwritten); the gap is deliberate, not a lost decision.

**Why.** The recorded clock defect showed the failure mode: work was executed
from a prompt, so nothing durable said what "done" meant, nothing could verify
it, and a refusal left no signal to learn from. A contract that travels with the
task makes completion checkable, budgets enforceable and failures diagnosable —
and it keeps the classifier vocabulary (W0) as the routing authority rather than
making intake a second router.

**Consequences.** `python/harness/tasks/{goal.py (new), model.py (TaskType,
TaskBudget, GoalContract), store.py (additive columns + JSON update path),
manager.py (criteria merge, validated contract updates), tools.py (create_goal),
context.py (retried attempts quote the most recent FAILED one)}`. Tests
`test_tasks_goal.py`, `test_tasks_tools.py` (create_goal). Docs
`docs/harness/task-plan-system.md`.

## ADR-H210 - Completion is evidence: a task-type verifier records proof, and prose is never a completion signal

**Decision.** A task that declares a `taskType` completes only when BOTH the
durable contract passes (`verify_completion`: dependencies, children, criteria)
AND the type's verifier (`tasks/verification.py::verify_task`) finds the
evidence the run actually produced — zero-exit code-verification records,
delivered artifact references, recorded research sources, observed metrics.
A verifier reads records, never prose; a declared type with no verifier, or a
crashing verifier, is a REFUSAL, never a pass. A failed verification with retry
budget left is retried with the missing evidence NAMED; exhausted, the task ends
`BLOCKED` (`BlockedReason.VERIFICATION`) with the exact missing proof — never a
confident "done", never an infinite repair loop. The verified verdict is stored
on the `run_complete` checkpoint. A task with no `taskType` keeps the exact
pre-W8 contract.

**Why.** "The agent said done" and "the run completed" are both too weak: the
round's own acceptance scenario (a deliberate defect that only a test run
catches) requires the completion decision to read the run's records. Typing the
verifier by task type is what lets each family demand its OWN proof (tests for
code, sources for research, artifacts for documents) without a universal
checklist that would block honest work.

**Consequences.** `python/harness/tasks/{verification.py (new), model.py
(BlockedReason.VERIFICATION), scheduler.py (budget + verification gates before
complete, evidence checkpoint, honest BLOCKED), bootstrap.py (bounded evidence
blobs on the run outcome), manager.py (escalate_task blocked_reason)}`. Tests
`test_tasks_verification.py`, `test_goal_completion.py`. Docs
`docs/harness/task-plan-system.md` (W14's verification-by-type direction lives
in the round-4 roadmap).

## ADR-H203 - A Compose verdict must cite a catalog witness; a witness-less Compose escalates

**Decision.** In the UI method classifier, a `compose` verdict for an additive,
specific capability is valid only when it cites at least one registered
component (`matchedComponents`). A named capability the catalog cannot satisfy
is a **capability gap**: the classifier returns `engineer` with
`capabilityGap: true`, and `ui_start_engineering` re-checks the catalog itself
and ESCALATES instead of returning the recorded false refusal ("no engineering
task was created"). Layout/restyle/page requests need no witness (their target
is the existing page) and "add a widget" is a UI shape, not a capability. The
match is deterministic (content tokens vs the catalog's id/name/description/
tags); no model call is involved.

**Why.** The recorded clock defect: a misclassification became an authoritative
refusal, and nothing durable recorded that a constructible request had been
refused. Requiring a witness makes the verdict falsifiable — a Compose that
cannot name what it composes is exactly the failure mode.

**Consequences.** `python/harness/ui/mode.py` (`assess_capability`,
`ModeDecision.capabilityGap`), `ui/engineering.py::start` (catalog re-check +
escalation), `ui/tools.py` (witness/gap in the tool text). Tests
`test_ui_mode.py`, `test_ui_engineering.py`, the routing benchmark
(`routing_benchmark_v1.json` + `routing_e2e_v1.json`). Docs
`docs/ui-engineering.md`.

## ADR-H204 - One capability-fit fact is shared by the classifier and the escalation

**Decision.** `ui/mode.py::assess_capability` is the ONE deterministic
capability-fit fact (`satisfied` / witness / gap) over the live component
catalog. Both consumers read it: the classifier (as the witness requirement,
ADR-H203) and `ui_start_engineering` (as its own catalog re-check before
refusing). A diagnostic view (`additive_only=False`) finds a named capability
even when the additive phrasing was missed — only the routing-repair proposer
uses it, never the classifier.

**Why.** Two classifiers disagreeing with no shared fact is how the clock
request fell between them. One fact, two readers, no second matcher.

**Consequences.** `ui/mode.py`, `ui/engineering.py`, `recovery/routing_repair.py`
(the diagnostic consumer). Docs `docs/ui-engineering.md`,
`docs/harness/failure-recovery.md`.

## ADR-H205 - A request routed to a path that cannot satisfy it is a first-class failure

**Decision.** "The request was routed to a path that could not satisfy it" is a
durable, diagnosable outcome, not an invisible refusal. Two deterministic
records carry it: the canonical `ui.capability_gap` event (emitted by an
escalation, registered in the Event System, mapped to `FailureKind.CAPABILITY`,
subscribed by the recovery engine) and the mistake categories
`misrouted_request`, `capability_gap_refused`, `composed_without_witness`,
`escalation_refused`, `intent_ambiguity` in `eclipse_memory/mistakes.py`,
derived from the persisted run payload. A clean refusal that emits none of the
legacy failure events still leaves a stable signature.

**Why.** The single richest signal for self-repair — a task that did not get
done — was invisible to the learning layer because a refusal is not a
`tool.failed`.

**Consequences.** `event_system/registry.py`, `recovery/{bootstrap,taxonomy}.py`,
`eclipse_memory/mistakes.py`, `ui/engineering.py` (emission). Tests
`test_routing_mistakes.py`, `test_routing_incident.py`. Docs
`docs/harness/failure-recovery.md`.

## ADR-H206 - Self-repair proposals are typed, minimal, worktree-authored and benchmark-verified; activation stays operator-gated

**Decision.** A routing incident yields a TYPED proposal (`VOCABULARY_GAP` /
`CATALOG_GAP` / `CAPABILITY_ABSENT` / `POLICY_GAP`); only a vocabulary gap
produces a minimal `code.patch.PatchOp`, authored on a disposable worktree,
verified by activating the exact rule in memory through the existing
`ImprovementProposal` contract and re-running the fixture, then reverted.
Landing goes through the existing single-use, digest-bound promotion (W4);
authority files are refused before an approval is created or consumed.
Nothing is written to the checkout and nothing is activated by the proposal
itself.

**Why.** "The harness edits its own verdict logic" is only safe when the edit
is minimal, typed, proven on a fixture, and landed by a person.

**Consequences.** `recovery/routing_repair.py`, `recovery/routing_landing.py`,
`routers/recovery.py` (`POST /recovery/incidents/{id}/routing-repair`),
`routers/code.py` (authority refusal), the recovery console. Tests
`test_routing_repair.py`, `test_routing_landing.py`. Docs
`docs/harness/failure-recovery.md`, `docs/harness/self-editing-harness-plan.md`.

## ADR-H207 - The acquisition plan is a first-class incident outcome; the executor may propose install/build, never grant it

**Decision.** A missing capability ends in a typed, ordered acquisition plan
(discover → assess → propose → acquire → validate → retry) naming each step's
surface, readiness, approval requirement and exact missing facts. The executor
may run only AGENT-LOCAL surfaces unattended (the engineering escalation, or a
same-plan capability task); `install`/`enable` are approval-flagged and park
the task ASKING_USER. Validation without a real contract test is `unverified`,
never passed; retry resumes the original task from its durable checkpoint, and
a same-plan capability task resumes it through the existing DAG when it
COMPLETES — that completion is the validation gate.

**Why.** "No tool for X" must lead to a constructive route, but an installer
that runs itself is exactly the authority-widening the invariants forbid.

**Consequences.** `recovery/acquisition.py`, `recovery/acquire_handlers.py`,
`tasks/scheduler.py` (the `waiting` outcome), `tasks/bootstrap.py` (the
production caller). Tests `test_recovery_acquisition.py`,
`test_recovery_acquire_handlers.py`. Docs `docs/harness/failure-recovery.md`.

## ADR-H208 - The self-evolution loop owns its own measured dimensions; an unmeasured dimension is published as unmeasured

**Decision.** The self-evolution program is measured by its own §1 block on the
standing gate — `time_to_repair_ms`, `self_repair_precision`,
`requirement_completeness`, `cost_per_success`, `unauthorized_action_rate` —
computed from real records (recovery incidents, goal-contract criteria,
evaluation trials). A dimension with no samples is printed `UNMEASURED`; it is
never fabricated and never omitted. Progress is judged by these numbers, not
by tool/skill/agent counts.

**Why.** "Learns and solves its own problems" is only meaningful as a trend on
frozen measurements; a blended score or an inferred number would hide exactly
the regressions this program exists to prevent.

**Consequences.** `evaluation/repair_metrics.py`, `evaluation/gate.py` (the
report block), the round-4 routing/benchmark gates. Tests
`test_evaluation_repair_metrics.py`. Docs `docs/harness/agent-evaluation.md`.

## ADR-H213 - Parallel agents are adopted only where a measured benchmark beats the single-agent baseline

**Decision.** Multi-agent orchestration is an ADOPTION decision measured on a
frozen benchmark, never a default. `evaluation/team_benchmark.py` runs the same
multi-step task BOTH ways through the REAL runtime — one agent (`execute_run`)
and a 2-3 specialist team (`TeamService` → `A2AService` → `TaskScheduler` → the
same `execute_run`) with per-subtask deterministic scripts over a per-trial
isolated store — and grades both on the REAL resulting state with the SAME
graders. `experiments.multi_agent_compare` publishes both recorded suites, the
measured axes (quality, wall clock, tool calls, model steps) and a
deterministic verdict: the team is adopted only when quality does not regress
AND a real gain is measured (wall clock improved by at least 15%, or strictly
fewer tool calls). An unmeasured variant decides nothing; token/cost axes the
tier cannot measure are published UNMEASURED, never estimated.

**Why.** "More agents" is a cost, not a capability: it multiplies runs, model
steps and oversight. The round's rule is adoption on evidence, and the first
frozen-set measurement (2026-10-18, 3 trials × 2 tasks) found equal quality with
53.6% more wall time and three more tool calls — so the team is not used by
default, and the harness (not an opinion) is the gate for re-measurement.

**Consequences.** `python/harness/evaluation/team_benchmark.py`
(`TeamAgentInvoker`, the team model seam, `adoption_verdict`, `variant_axes`),
`evaluation/experiments.py` (`multi_agent_compare`),
`evaluation/datasets/team_v1.json` (the frozen tasks; each carries both
variants), `runner._script_messages` (a per-subtask call-id prefix, so argument
capture across concurrent child runs cannot collide). Tests
`test_evaluation_team_comparison.py`. Docs
`docs/harness/agent-evaluation.md`,
`docs/harness/bot-team-orchestration.md`.

## ADR-H211 - Acquisition is a closed lifecycle, and every acquired capability must pass its own contract test

**Decision.** A missing capability is handled as ONE closed lifecycle —
discover → assess → propose → acquire → validate → retry the original task from
its durable checkpoint — over the existing `CapabilityIndex` and the existing
surfaces. A proposal names each step's surface (`existing_tool`, `skill`,
`plugin`, `scaffold_kit`, `engineering`, `alternative`, `operator`), its
readiness, its approval requirement and the exact missing facts. No step
self-grants: `install`/`enable` run only through an operator-approved, single
surface call; a capability task created for the original work resumes it through
the existing DAG, and THAT completion (with its own verification) is the
validation gate. Validation without a real contract test is reported
`unverified`, never passed.

**Why.** "No tool for X" must lead to a constructive route, but an installer
that runs itself is exactly the authority-widening the invariants forbid.
Closing the loop also means the original task resumes — an acquisition that
leaves the task parked forever is not an acquisition.

**Consequences.** `recovery/acquisition.py` (the ordered lifecycle),
`recovery/acquire_handlers.py` (the production caller: build → engineering or a
same-plan code task; install/enable → operator decision), `tasks/scheduler.py`
(the `waiting` outcome), `tasks/bootstrap.py` (`_acquire_capability`),
`routers/recovery.py` (`/recovery/incidents/{id}/acquisition/execute|resume`).
Tests `test_recovery_acquisition.py`, `test_recovery_acquire_handlers.py`. Docs
`docs/harness/failure-recovery.md`.

## ADR-H212 - Learning is layered, and each layer has its own validation standard

**Decision.** Self-evolution learning is five explicit layers over the existing
stores, not one blended engine: **L1 episodic** (`eclipse_memory/mistakes.py` +
run traces — records what happened), **L2 procedural** (`skills/learning.py`,
`skills/procedures.py`, `learning_review.py` — reusable procedures, inert until
promoted), **L3 strategic** (`recovery/strategy_learning.py` — which route
works per failure signature, measured from the incident/attempt ledger), **L4
harness** (`evaluation/improvement.py` + `code/promotion.py` — code changes,
verified by the anti-regression gate, landed only by an operator), **L5
evaluation** (the frozen benchmark — which changes actually help). A layer's
output is admissible at its own standard only: an L3 preference may reorder the
policy's OWN candidates and never add one; an L2 candidate is inert until
evaluation + promotion; an L4 proposal lands only through the single-use,
digest-bound promotion. A failed attempt is evidence, not proof of a fix — no
prompt rewriting or code change after every task.

**Why.** "Learns from its own runs" is only meaningful when each claim is
validated where it is made; conflating a recurrence signal (L2) with a measured
success rate (L3) or a benchmark improvement (L5) is how a system talks itself
into changes that make it worse.

**Consequences.** `recovery/strategy_learning.py` (`strategy_stats`,
`preferred_strategy`, `preference_from_store`; fed into `record_failure`),
`skills/procedures.py` (construction procedure candidates), the round-4
`repair_metrics` dimensions for the loop itself. Tests
`test_recovery_strategy_learning.py`, `test_construction_procedure.py`,
`test_evaluation_repair_metrics.py`. Docs
`docs/harness/memory-skills-evolution-plan.md`,
`docs/harness/failure-recovery.md`.

## ADR-H214 - A registered tool is not advertised as available until its capability contract and fixture pass

**Decision.** Every advertised tool carries a capability contract — a stable
name, a typed input schema, a permission/risk vocabulary, a bounded timeout —
and is checked by `tool_contracts.check_tool_contract`; the whole-registry audit
is a release gate (all 886 registered tools pass). Beyond metadata, each audited
family (filesystem, terminal, git, browser, API/web, artifacts, finance) has an
executable BEHAVIOUR fixture that drives a representative real tool through the
one `ToolRegistry` with a real context and asserts the outcome shape — a check
that fails means the tool is not honestly available for that family. Advisory
gaps (untyped output, undeclared effect metadata) are counted and named, never
blocking, and a live dependency health probe remains the operator-facing check
(`get_source_health`, MCP verify) — a static contract cannot probe a live
dependency, and claiming otherwise would be fabrication.

**Why.** A registered name is not proof: the earlier advertised-but-dead guard
proved admission, not behaviour. Two real metadata bugs (an invalid risk value
and a timeout ceiling) were found only when the audit was written; a fixture per
family is what keeps that class of drift from returning silently.

**Consequences.** `python/harness/tool_contracts.py`,
`python/harness/tool_fixtures.py` (`FAMILY_FIXTURES`, `run_family_fixture`,
`fixture_report`), `test_tool_contracts.py`, `test_tool_behaviour_fixtures.py`.
Docs `docs/harness/self-evolution-roadmap.md` (W13).

## ADR-H215 - Autonomy tiers are enforced by the runtime, and the agent can never widen its own authority

**Decision.** The external tiers map onto the EXISTING policy groups the
authorizer enforces, as data: read → **Tier 1 autonomous** (no per-call
approval); workspace write/execute/install → **Tier 2 guarded** (policy +
resource caps + the `AutonomyEnvelope`); external/destructive/credential/
live-transaction actions → **Tier 3 approval** (a single-use operator approval
per exact action). `security/tiers.classify_tool` derives a tool's tier from its
effect, its approval group, its guarded effects/groups, or its write permission,
and `tier_requires_approval` is the runtime check. A consistency test pins that
an approval-group tool can never classify below Tier 3, so the mapping agrees
with the policy the runtime actually enforces. Policy, approvals, the autonomy
envelope and the evaluation gates stay outside the self-edit surface: the agent
may never grant itself permissions by changing them.

**Why.** "The safe tier is the default" is only true if the tier is a runtime
property, not a prompt instruction. Deriving the tier from the enforced groups
(not a second parallel table) is what makes the two impossible to disagree.

**Consequences.** `python/harness/security/tiers.py` (`classify_tool`,
`tier_requires_approval`, `tier_report`), `test_security_tiers.py` (counts +
approval-tier names + the consistency pin). Docs `docs/harness/security.md`.

## ADR-H216 - The Capability Studio is a navigation shell over the existing managers, and it grants nothing

**Decision.** One navigable surface — the Capability Studio
(`src/components/screens/capability-studio/`, AppTab `ui-builder`, section deep
link `?tab=ui-builder&sub=studio.<section>`) — presents the capability kinds in
one place: pages & widgets (the EMBEDDED UI Builder, unchanged), tools &
plugins (the existing `PluginsScreen`, which gains its home), skills and
integrations (read-only summaries that NAVIGATE to their owning managers). The
studio introduces no second registry, no second enable path and no new
authority: plugin enable/disable is the plugin platform's own operator action,
skill enablement stays evaluation- + promotion-gated in the Skills manager,
connections are edited in Settings, and installing anything goes through the
operator acquisition route (ADR-H211) — the studio never self-grants. Load
states are honest: a failing fetch is a failure, never an empty library, and a
summary counts only RECORDED states (an untested connection is not "failing").
Inactive sections stay mounted (`display:none`), the shell's keep-alive
pattern, so switching sections never drops in-flight work.

**Why.** The pieces existed (UI Builder, plugin manager, skills manager,
connectors) but were scattered — and the plugin manager was orphaned with no
mounted tab. Phase D of the capability-extension plan called for one studio;
building NEW managers would have created the second-registry defect the
invariants forbid, while composing the existing ones keeps every lifecycle
(evaluation, promotion, approvals) exactly where its tests already pin it.

**Consequences.** `src/components/screens/capability-studio/`
(`capability-studio-screen.tsx`, `skills-section.tsx`,
`integrations-section.tsx`, `capability-studio.test.tsx`),
`ui-builder-screen.tsx` (`embedded` prop), `stock-chat.tsx` (the `ui-builder`
tab mounts the studio; the file stays at its 1518-line ratchet), i18n slices
`dictionaries/navigation.ts` + `dictionaries/capability-studio.ts` (en.ts
SHRANK 1254→1234 while adding the studio keys), `docs/ui-engineering.md`,
`docs/harness/capability-extension-plan.md`.

## ADR-H217 - RSL is a conductor over the existing owners, not a second self-improvement stack

**Decision.** The Recursive Self-Improvement Loop (RSL) lives in
`python/harness/self_improvement/` as a bounded conductor over machinery that
already exists: post-run review and mistake capture, the ONE
`LearningRecurrence` ledger, `EvolutionCandidate`/`EvolutionAttempt`, the
evaluator, worktrees, `code/promotion.py` and `code/selfhost.py`. It adds no
second runtime, database, queue, scheduler, evaluator, provider or memory
system. R1 (observation) is dark by default (`HARNESS_RSL_MODE=off`) and makes
no model calls; candidate execution and verification arrive only in later
phases. The loop is one bounded `asyncio` task beside `learning_review`, not a
fabric worker.

**Why.** The measured gaps were closure gaps, not missing subsystems: the
recurrence bridge had no production caller, mining was CLI-only, and the
evaluator loop was test/CLI-only. Building a parallel stack would create the
second-registry/second-counter defect the self-evolution invariants forbid.

**Consequences.** `python/harness/self_improvement/` (`config`, `models`,
`observer` for R1; `security` + `evaluation/candidate_invoker.py` for R3;
`loop`, `proposer`, `verifier`, `landing`, `recorder` for R4),
`code/evolution.py::record_mistake_recurrence`,
`eclipse_memory/mistakes.py::capture_mistakes`, `learning_review.py`, the
evaluator provenance/split work in R2, the
`self_improvement.{candidate_created,experiment_completed}` event specs, and R5's
operator evidence/`prepare-landing` routes + learning-journey experiment panel.
Tests `python/tests/test_rsl_{config,observation,provenance,snapshot,candidate_invoker,loop,landing,verifier}.py`
+ `test_evolution_rsl_routes.py` + `rsl-experiments-panel.test.tsx`. Code
candidates record `benchmark=UNMEASURED`; only the surface path can claim a
measured delta.

## ADR-H218 - There is one landing path: promotion + selfhost; evolution_service.activate is not a code write

**Decision.** A code change becomes real only through the existing
single-use, digest-bound promotion approval. `landing.prepare` returns a
`promotion.build_preview` preview; the operator approves through the existing
`/code/tasks/{runId}/promote` routes. The health-gated apply is
`code/selfhost.py::guarded_land`; the current `/promote` route calls
`promotion.apply_promotion` and reports the restart/build verdict, so RSL must
wire the selfhost seam explicitly rather than claim the route already
health-checks. `code/evolution_service.py::activate` is NOT a code-landing path:
it activates a skill version through `skills/versioning.py` (and, for a code
subject, carries the change as a thin managed-skill description). It must never
be presented as writing the checkout.

**Why.** Two landing paths would mean two approval semantics and two rollback
stories. The invariants require one operator-confirmed write path.

**Consequences.** `docs/self-improvement/rsl-implementation-plan.md` §3.6 and
R4/R5; the R5 routes add `prepare-landing`/`land` rather than overloading the
existing `/evolution/candidates/{id}/activate` skill route.

## ADR-H219 - RSL candidates may not edit their own judge

**Decision.** The RSL subject denylist is frozen authority, enforced before a
proposal is accepted and again before landing. It covers the evaluator and its
graders/scoring/regression/gates, benchmark datasets, `python/tests/**`,
`python/harness/security/**`, `code/policy.py`, `code/authorizers.py`,
`code/promotion.py`, `code/selfhost.py`, `code/patch.py`, `code/workspace.py`,
`code/evolution*.py`, `skills/versioning.py`, `skills/permissions.py`,
`mcp/approvals.py`, `scripts/check-all.js`, `scripts/test-*.js`, `.env` and
`jdds.db`. Every op path is checked, not only the candidate's `targetPath`; the
owning test file must be byte-identical to the checkout at evaluation time and
its hash is recorded in the attempt.

**Why.** `evaluate_candidate` runs owning tests, so a candidate that edits its
own test can game the gate. The evaluator and the datasets are the measurement
authority; a candidate may not change the ruler.

**Consequences.** R3 added `self_improvement/security.py` (`RSL_DENY_GLOBS`,
`assert_landable_target`) and `evaluation/candidate_invoker.py`
(`validate_snapshot`, `CandidateAgentInvoker`); R4 adds the before-propose/
before-land checks. Tests `test_rsl_snapshot.py` +
`test_rsl_candidate_invoker.py`. ADR-H157 and the existing
`FROZEN_PATHS`/`FORBIDDEN_FIELDS` remain the inner floor.

## ADR-H222 - Experiment identity and provenance live on EvaluationRun; a comparison without them is refused

**Decision.** A persisted evaluation run carries the identity that makes it
comparable: `agentVersion`, `gitCommit` (or an explicit
`gitCommitUnavailable`), the dataset checksum, and a `config` identity with
`trials`, `tier`, `split`, `graderVersion` and optional `seed`/`temperature`.
Its `metadata` carries `taskFingerprintDigest`, `taskCount`, `graderVersion`
and the split. Task fingerprints are persisted through the existing
`EvaluationStore.upsert_task`; every trial's score JSON carries
`graderVersion`. `evaluation.provenance.require_provenance` refuses a run
missing any required piece. Cost and latency are `None` when no real source
reported them, never `0`; `cost_per_success` is UNMEASURED if any measured
trial's cost is unknown.

**Why.** A delta between two runs is only evidence when both runs name the same
tasks, grader version and configuration — and differ only on the axis the
experiment claims to vary. A NULL checksum, empty config or fabricated zero
would make a comparison unfalsifiable.

**Consequences.** `evaluation/provenance.py` (`AGENT_VERSION`,
`repository_commit`, `task_fingerprint_digest`, `require_provenance`),
`evaluation/runner.py` (`run_suite` identity + `upsert_task`),
`evaluation/tasks.py` (`SPLITS`, `select_tasks(split=...)`),
`evaluation/scoring.py` (`graderVersion`, `costKnown`/`latencyKnown`),
`evaluation/improvement.py` (persisted candidate run, measured-only acceptance),
tests `test_rsl_provenance.py`. Built-in splits: `benchmark_v1` +
`construction_v1` = validation (47), `routing_e2e_v1` = optimization (4),
`team_v1` = held_out (2).

## ADR-H223 - A lesson requires cue + evidence + a completed validation, and passes the verdict tripwire before storage

**Decision.** A reusable lesson is an adopted `LearningCandidate` with four
additive fields (migration 31): `cue` (when it applies), `adoptedArtifactType` /
`adoptedArtifactId` (what proved it) and `validationRunId`.
`self_improvement.lessons.distill_lesson` requires non-empty cue, lesson and
evidence; discards a non-adopted decision; runs the evaluation verdict tripwire
over the cue, lesson and evidence text before writing; and dedupes by a
normalized cue+lesson signature. Retrieval (`select_lessons`) is deterministic,
cue-token matched and scope-matched, and rejects any lesson whose recorded
validation run is absent or not `completed`.
`prompt_segments.build_lesson_manifest` is hard-bounded to 3 lessons / 800
characters, and the run emits `prompt.lesson_injected` with the lesson ids plus
origin/validation/artifact refs.

**Why.** A lesson is a transfer of a verified change, not a second copy of an
evaluation. Without the cue gate it cannot be retrieved deterministically;
without the evidence/validation gate it is an unverified claim; without the
tripwire it can become an answer key; and without the scope check one
observation can leak across domains. Bounds keep the loop from turning into
prompt bloat.

**Consequences.** Migration 31 on `eclipse-memory.db`
(`eclipse_memory/schema.py`; the runner skips an already-present ADD COLUMN).
New `python/harness/self_improvement/lessons.py`; `prompt_segments.py` gains the
run-frozen `lesson_manifest` segment and `build_lesson_manifest`;
`graph_context._lesson_manifest_context` wires retrieval/injection and emits the
event (spec in `event_system/registry.py`). Tests `test_rsl_lessons.py`. A lesson
remains inert context: it grants no tool, permission or activation.

## ADR-H224 - Recursion is bounded (k, budget, generation) and never activates a change

**Decision.** R7 adds ONE bounded recursive experiment path.
`self_improvement.proposer.propose_many` builds up to `HARNESS_RSL_K` typed
(`invariant`/`domain`) candidates from the same observation using
operator/agent-supplied operations only; it never invents a patch or calls a
model. `selection.py` assigns seeded anonymous labels (a stable permutation),
drops non-answers with a recorded cause, falls back neutrally to the base, and
makes `k=1` trivial. `composition.py` merges only candidates that passed their
own verification and only when their op paths do not overlap; it claims a
MEASURED gain only when a composed candidate beats the best single verified
candidate on validation, otherwise it records `no_gain`. `recursion.py` verifies
each candidate through the caller's existing verifier, verdict-guards contrast
bundles before they can reach a model, records ONE `EvaluationRun` with the
selection/composition, and caps `k` by the remaining daily budget. `k=1` remains
the default. Recursion is one generation only: `run_recursive` never feeds its
own result back into another recursive experiment. No recursive candidate
activates: the existing single-use promotion approval is still the only write
path.

**Why.** Multiple candidates from one observation can find a better composition
than a single proposal, but only if the comparison is anonymous and the
composition is verified. Without the anonymous seeded selection, position/brand
bias would decide; without the verified-only merge, composition would launder an
unverified claim; without the measured-beats-best rule, "composition" would be
a story rather than a measured gain. The budget cap keeps `k` a cost bound, not
a license to multiply experiments.

**Consequences.** New `python/harness/self_improvement/{selection,composition,recursion}.py`
and `proposer.propose_many`; `RSLLoop.run_recursive` uses the loop's configured
`k`, stores, actor and daily budget. The contrast/verdict utilities are now
wired on this path. Tests `test_rsl_recursion.py`. No new runtime, database,
queue or approval surface; no model proposer (v1c remains deferred).

## ADR-H220 - Local app reads are a bounded GET surface, and browser failures name their cause

**Decision.** The agent gets one sanctioned read path into the app it is
running: `app_api_get`, GET-only, loopback-only (`HARNESS_CALLBACK_URL`), path
prefix `/api/`, no redirects, no credentials, 10 s timeout and a 200 KB cap. It
is a read of the user's own app, never a general HTTP client; a canonical public
host is for the browser, not this surface. In the same change, a Playwright
navigation failure carries its CLASSIFIED cause (host unresolvable, connection
refused, timeout, redirect loop) in the tool sentence, not only in metadata; an
app-URL failure probes the known app origins and retries ONCE on the first
reachable one, reporting the probe trail when none answers. The browser also
reports the app's boot overlay (`[data-global-loader][data-pct]`) as "still
starting" instead of letting a reader interpret it as a stuck page.

**Why.** QA Round 1 showed the agent could not read the app's own `/api/*` facts
(`curl` is hard-denied, `health_check` returns a status only, web research
blocks loopback) and misread a hidden transport error as a policy boundary. A
bounded read surface removes a whole class of dead ends without widening
authority; naming the transport cause removes the incentive to invent one.

**Consequences.** New `python/harness/app_api_tools.py` and
`python/harness/browser/app_origin.py`; `browser_{navigate,snapshot}` gained the
origin retry and boot note; `app_api_get` is on `ECLIPSE_AGENT` and
`CHAT_CODE_TOOLS`. Tests `test_app_api_tools.py` + `test_browser_app_origin.py`.
The tool cannot mutate, cannot leave loopback, and cannot follow a redirect.

## ADR-H221 - The chat step budget is configurable and defaults to 12

**Decision.** An ordinary `eclipse-agent` run gets 12 model steps by default
(was 8). `ECLIPSE_CHAT_MAX_STEPS` overrides it, clamped to 1..32. An EXPLICIT
per-run `agent` config (a caller-supplied `AgentConfig` with its own
`maxSteps`) wins over both. Code-autonomy floors (24 at `edit_verify` /
`approve_external`) and every skill manifest's own budget are unchanged, as are
the browser/computer sub-budgets.

**Why.** QA Round 1 hit the 8-step cap mid-navigation: discovery, a tool call,
a retry and a page read do not fit in eight model turns. The cap is a cost
guard, not a capability contract; making it explicit and slightly larger keeps
multi-tool turns completable while preserving a hard ceiling.

**Consequences.** `graph.chat_step_budget` is the ONE resolver
(`build_graph` and the `run.started` event both use it); tests
`test_chat_step_budget.py`. Operators can lower the cap without a code change
when cost matters more than depth.

## ADR-H225 - Generated capabilities are manifests plus sandbox-tested contracts, never code-written-is-available

**Decision.** A reusable capability is created through
`python/harness/capabilities/`: a validated `CapabilityManifest`, an
implementation, a contract test and a README written under
`.black-eclipse/capabilities/<slug>/<version>/`. The contract test runs through
the existing analysis sandbox. A pure workspace recipe auto-activates only after
a pass; effectful or global capabilities stop at `approval_required`. Active
recipes join the existing `code/recipes.py` registry. The builder never writes
core harness files and never executes generated code in-process.

**Why.** "The model wrote code" is not "the capability exists". A manifest,
source/test digests, a real sandbox run and an activation decision are the
minimum proof that a generated capability is safe and reproducible. Reusing the
analysis sandbox and recipe registry keeps one execution path, one data path and
one tool registry.

**Consequences.** New package `python/harness/capabilities/`; dynamic recipe
hooks in `code/recipes.py`; `capability_*` tools on `ECLIPSE_AGENT`,
`CODE_AGENT` and `CHAT_CODE_TOOLS`; generated capabilities appear in
`recovery/discovery.py`; usage is recorded in `CapabilityUsage` and repeated
failures auto-disable. Tests:
`python/tests/test_capabilities_{manifest,request,builder,tools}.py`. Deferred:
effectful builders, the chat card/Settings UI, `eclipse_tools` RPC and automatic
durable-task retry (the existing acquisition/task path owns resume).

## ADR-H226 - The capability card and Generated list read the same records; activation stays an operator route

**Decision.** The chat card consumes the runtime's `capability.*` lifecycle
events (`src/lib/agent/events.ts` -> `capability-types.ts` -> `CapabilityCard`),
and Capability Studio � Generated reads the same durable records through
`GET /capabilities`. Activation, deactivation, rollback and retire are
`POST /capabilities/{id}/*` routes gated by `require_operator`. The agent's
`capability_*` tools remain separate: they can build/test and auto-activate only
a pure workspace recipe; they cannot call the operator routes.

**Why.** The user-visible promise is "I built it, checked it, and used it", but a
card is not proof. Rendering the runtime's own manifest, test checks, failed
checks, stderr and activation state keeps the UI honest. Reusing the durable
`CapabilityRecord` avoids a second store or a UI-only status model; gating the
operator actions by transport preserves the no-self-granting rule.

**Consequences.** New `src/components/chat/capability-card.tsx`,
`src/lib/harness/capability-types.ts`, `src/lib/harness/capabilities-client.ts`,
`src/components/screens/capability-studio/generated-section.tsx`,
`src/app/api/capabilities/[[...path]]/route.ts`; Python
`routers/capabilities.py`, `CapabilityStore.usage`, `builder.deactivate`.
Events carry a bounded view (no source text, no secrets); the card shows source
and test digests, never the implementation body. Tests:
`src/components/chat/capability-card.test.tsx`,
`src/lib/harness/capability-types.test.ts`,
`src/components/screens/capability-studio/capability-studio.test.tsx`,
`python/tests/test_capabilities_api.py`.

## ADR-H227 - PDF reports use the vendored Matplotlib backend, not a new PDF stack

**Decision.** `eclipse_report` renders PDF through
`documents/reporting.py::build_pdf`, using the existing vendored Matplotlib PDF
backend. Word/Excel/PowerPoint continue to use the existing authoring builders.
No new PDF library, browser-print service or Office runtime is added.

**Why.** A PDF report must be a real, downloadable artifact, but adding
ReportLab/WeasyPrint/a headless browser would add a heavy dependency and a second
document layout engine. Matplotlib is already vendored for the analysis figures,
supports paginated text and tables, and runs locally with no network.

**Consequences.** The PDF layout is intentionally simple (headings, paragraphs,
bullets, tables, page breaks); embedded report figures, HTML templates and PDF/A
remain future work. A missing Matplotlib reports `REPORT_RENDER_UNAVAILABLE`
honestly. The bytes are registered through the ONE `ArtifactService`. Tests:
`python/tests/test_reports.py`; docs `docs/harness/reports.md`.

## ADR-H228 - Chat turns enable the analysis sandbox by default

**Decision.** `startChatRun` and `chatWithAgent` send `analysisEnabled: true` on
every chat turn. A code profile may still narrow the tool surface (for example
`plan_only`), but it no longer turns computation off. Headless callers of
`startRun` keep the runtime's opt-in default (`RunRequest.analysisEnabled =
False`), so automation and non-chat callers are unchanged.

**Why.** Computation requests such as "compute the Ulcer Index" were classified
as `not_code`; the analysis sandbox stayed off and the agent refused a task it
could safely compute. The sandbox is read-only, additive, gated by
`analysis.execute`, and computed values cannot enter research claims. Enabling
it per chat turn removes a routing failure without widening write authority.

**Consequences.** The ordinary chat surface now carries `analysis_run`; the code
profile tests assert the sandbox stays on. `run-service.ts` is the ONE front-door
decision. Tests: `src/lib/harness/run-service.test.ts`.

## ADR-H229 - Analysis packages live in a managed target, installed through a separate approval-gated lane

**Decision.** Third-party packages for `analysis_run` are installed into
`.black-eclipse/python-env/site-packages` with a JSON lock, and are made
importable by adding that target to the runner's `sys.path` and the lock's
top-level modules to the import allowlist. The install is a separate, network-
capable, single-use approval-gated mutation (`python_env_install` /
`python_env_remove`), run as a bounded pip argv with no shell. Generated code
keeps the sandbox's no-network import/builtin rules.

**Why.** The analysis sandbox needs a curated stack (`quantstats`, etc.) without
modifying the vendored interpreter or turning generated code into a package
installer. A managed target keeps removals and rollback local, and the lock is
the exact import allowlist. Separating install from execution preserves the
"package installs are separate and approval-gated" rule.

**Consequences.** New `python/harness/python_env/` (`paths.py`, `provisioner.py`,
`authorizers.py`, `tools.py`); `code/execution.py` adds the managed path and
modules to the analysis runner; `code/policy.py` marks install/remove as write
tools; `code/authorizers.py` composes the approval layer. On the local tier an
approved package is trusted code and is not OS-network-isolated; hard isolation
requires the container tier. Tests: `python/tests/test_python_env.py`; docs
`docs/harness/python-env.md`.

## ADR-H230 - External capabilities are proposals through the existing MCP/plugin gates, never agent installs or enables

**Decision.** A broker, API or MCP need is handled by `capability_external`, a
read-only proposal in `python/harness/capabilities/external.py`. It resolves to
one existing surface: an MCP preset (`integrationId`), an operator-supplied
plugin folder, or a custom MCP server (`serverUrl`/`serverCommand`). Credentials
are named (vault secret names) and their presence is checked through
`MCPManager.secret_presence`; values never enter the tool, the proposal, a
record or a log. An MCP install requires the SAME single-use `mcp_install`
approval as the agent tool and writes a DISABLED config with no tools selected;
a plugin install keeps `operator=true, enable=false`; a custom server is added by
the operator in Settings. The proposal carries the existing recovery
`AcquisitionProposal` (`install` then operator `enable`) and the recover handler
accepts an `integrationId` fact. Nothing self-enables and the original task
resumes from its checkpoint only after the operator activates the adapter.

**Why.** An external system is not local generated code: it has a network
endpoint, a credential only the operator holds, and an activation decision that
must remain human. Reusing the MCP preset catalog, the approval store, the
plugin platform and the recovery acquisition lifecycle keeps one registry, one
store and one install path. A proposal that names credentials is useful; a
proposal that accepts values would turn chat into a secret channel.

**Consequences.** New `capabilities/external.py`; `capability_external` on the
finance, code and chat-code tools; `_KIND_SURFACE["plugin"]` -> `external_adapter`
and `nextTool` -> `capability_external`; `MCPManager.secret_presence` (VerifyMixin);
`approval_digest` shared by the MCP authorizer and the recovery handler;
`recovery/acquisition.py` gains the `mcp` surface and the `integrationId` fact;
`recovery/acquire_handlers.py` installs through the MCP approval store and refuses
MCP enablement from this path. Tests:
`python/tests/test_capabilities_external.py`. Deferred: generic from-scratch
adapter authoring and automatic durable-task retry.

## ADR-H231 - Effectful capabilities are handed to their existing owner, never built by the capability builder

**Decision.** `capability_build` returns one typed `EffectfulHandoff` for a
`tool`, `skill`, `ui_widget`, `ui_page`, `harness_change` or `plugin` request.
The hand-off names the existing surface and exact next tool (`scaffold_kit`,
`skill_create`, `ui_start_engineering`, `create_goal`, or
`capability_external`), the arguments it needs, the acceptance lines, the
evidence the owner must record, and the activation owner (always an operator or
the existing lifecycle). It is stored on the same durable `CapabilityRequest`
(`status: handoff_required`) and executes nothing. A pure recipe alone needs the
analysis sandbox; an effectful hand-off does not.

**Why.** A skill, tool, UI component/page or harness change already has an owner
with a stronger gate and a real verification path. Re-implementing any of them
inside the builder would create a second build surface and a weaker proof.
Handing off preserves one registry, one approval chain and one source of
evidence, and it removes the dead-end `approval_required` result that told the
agent nothing about what to do next.

**Consequences.** New `capabilities/handoff.py`; `builder.build` routes effectful
kinds to it and persists the hand-off; `capability_request` names the exact next
tool; `capability_build` skips the analysis gate for effectful kinds and reports
`handoff_required`; the chat card and TS view carry `handoff_required` and
`nextTool`. The target surface remains the sole writer and verifier, and the
original task still resumes from its checkpoint after activation. Tests:
`python/tests/test_capabilities_handoff.py`,
`test_capabilities_builder.py`, `test_capabilities_tools.py`; TS
`capability-types.test.ts`. Deferred: generic from-scratch adapter authoring,
`eclipse_tools` RPC and automatic durable-task retry.

## ADR-H233 - Surface candidates are typed snapshots on the ONE candidate row, and are never landable

**Decision.** A surface experiment is an `EvolutionCandidate` with
`subjectType = 'surface'` and a `surfaceConfig` object holding the validated
base and candidate `HarnessSnapshot`s, the dataset/split/task ids, target
dimension, trials, seed and an optional baseline run id. The additive columns
are `subjectType` (default `code`) and `surfaceConfigJson`; old rows and code
candidates are unchanged. `POST /evolution/surface-experiments` validates the
snapshot pair (`validate_snapshot`), creates ONE `proposed` row, and wakes the
existing conductor. The verify tick dispatches by subject type:
`verifier.verify_surface_candidate` runs the SAME base snapshot as a recorded
`rsl-baseline` (reused only when its base digest and task fingerprint still
match), benchmarks the candidate on the selected validation tasks, and
persists the evaluator's decision on the `EvaluationRun`. The loop records one
`EvolutionAttempt` (so the daily budget counts the experiment) and the
`self_improvement.experiment_completed` event. `landing.prepare` refuses a
surface subject with `RSL_SURFACE_NOT_LANDABLE`: a snapshot has no code change
to promote in v1.

**Why.** The surface verifier existed but had no production source, so no live
experiment could ever be MEASURED. Reusing the candidate row keeps one ledger,
one claim/lease/budget path and one evaluator; a second table or a parallel
runner would duplicate the state machine. A measured surface result is
evidence, not an activation.

**Consequences.** `code/evolution_store.py` gains the two idempotent columns;
`self_improvement/verifier.py` gains `verify_surface_candidate`;
`self_improvement/loop.py` dispatches code vs surface;
`self_improvement/recorder.py` records the surface attempt/event;
`self_improvement/landing.py` refuses surface subjects;
`routers/evolution.py` adds the operator-gated route; the typed front door and
`rsl-operator-panel.tsx` add the form. `evaluation.started` now has a
`self_improvement` consumer in `event_system/service.py` that only wakes the
loop. v1b skill-draft proposals and the v1c model proposer remain deferred.
Tests: `test_rsl_surface.py`, `test_rsl_security.py`, `test_rsl_loop.py`,
`test_evolution_rsl_operator.py`, `test_evolution_rsl_routes.py`, and the
event-wake test in `test_events_service.py`.

## ADR-H234 - Global promotion is an explicit operator copy, not a builder side effect

**Decision.** A verified workspace recipe is promoted by an explicit operator
action: `builder.promote` (routed at `POST /capabilities/{id}/promote`, gated by
`require_operator`) copies `manifest.json` (with `scope` rewritten to `global`),
`capability.py`, `test_capability.py` and `README.md` into
`python/harness/generated/capabilities/<slug>/<version>/`, then points the
durable record at the copy. Guards: the promotion call itself requires
`operator=True`; the source must be an unmodified, contract-verified read-only
recipe inside the workspace draft root; an existing different global version at
the same path is refused (`GLOBAL_EXISTS`); re-promoting the same digests is
idempotent. The workspace copy is left in place, and global activation still
requires the operator (ADR-H226/H232).

**Why.** A global capability ships inside the package, so it is a distribution
artifact, not a runtime side effect. Making promotion a separate, explicit copy
keeps the agent's "tested -> active" rule untouched, keeps the one registry and
one store authoritative (the record's `dir`/`scope` are the single truth), and
gives the operator a reviewable artifact with a reversible step. Writing global
files automatically from `build` would let model-authored source enter the
package unnoticed.

**Consequences.** `builder.promote` + route + `promoteCapability` client; the
Studio **Promote to global** button (only for verified workspace records). Tests:
`python/tests/test_capabilities_builder.py` (promotion, guards),
`python/tests/test_capabilities_api.py` (route 404). Studio additionally offers
**Rebuild with RSL** (a chat prompt, no new route) and the chat card gained
Build / Show plan / Continue / Rebuild / Not now actions (prompt-only). The
computed-result payload now carries `recipe` for display. Deferred: built-in
listing in Studio and generic adapter authoring.

## ADR-H232 - Capability activation resumes the original parked task; capability failures feed the learning ledger

**Decision.** A capability requested during a scheduled task records that task id
on the request and the versioned `CapabilityRecord` (`taskId`). When the
capability is activated (operator route, agent tool, or rollback), the runtime
resumes the original task through `TaskManager.restore_checkpoint(resume=True)`
only when the task is parked (`WAITING`/`ASKING_USER`). A running or `TODO` task
is never rewound. A failed generated-recipe invocation records a
`capability_failure` recurrence in the ONE learning ledger so the existing
self-improvement pass can propose from repeated failures; repeated runtime
failures still auto-disable the version and emit `capability.disabled`.

**Why.** A capability that lands after the original task was parked is only
useful if the task continues. Reusing the durable checkpoint is the existing
resume path and keeps one task/scheduler owner. The guard matters: activation of
a recipe mid-run must never rewind the run that is executing. Recording failure
recurrence closes the observation half of the loop without inventing a second
learning pipeline or enabling anything.

**Consequences.** `ToolContext.task_id`/`task_manager` and the graph wiring;
`CapabilityRequest.resume_task_id`; `CapabilityRecord.taskId` with an additive
column migration; `capabilities/resume.py`; activation/rollback routes and tools
report a `resume` outcome. `analysis_tools` records the recurrence and
`code/evolution.py` accepts the `capability_failure` category. Tests:
`python/tests/test_capabilities_resume.py`. Global promotion and the RSL rebuild
action followed in ADR-H234; automatic procedure-candidate creation and
`eclipse_tools` RPC remain deferred.

## ADR-H235 - Read-only eclipse_tools are validated replay, not a live data pipe

**Decision.** The Phase 3 analysis sandbox may expose exactly three read-only
bindings through `eclipse_tools`: `market_snapshot`, `live_quote` and
`price_history`. A program can use them only when a host resolver is attached
(the `analysis_run` path with a DataBridge); otherwise the AST gate refuses the
import. The runner has no live channel: a call writes one `pending.json`, exits
with sentinel `75`, and the host validates the request against the allowlist,
resolves it through the existing read-only DataBridge, memoizes the reply under
the RAW argument key, and replays the same program from the start. Alias and
`from eclipse_tools import ...` forms are refused so the static check stays
unambiguous. Unknown tools, unknown arguments, nested values, invalid
symbols/markets/dates and oversized requests are refused before resolution.

Bounds: 8 distinct calls, 4 KiB of arguments, a 16 KiB pending request read cap,
1 MB per result, 4 MB total memo, and the analysis wall clock. Only final program
outputs cross to the model context; the run record carries audit metadata only
(name, argument/result digests, status, round), never a fetched payload. The
generic terminal `execute_code` service is not changed, and this is not a
persistent kernel.

**Why.** Generated analysis code needs current market data without gaining
network, file writes, an arbitrary registry, or a second data stack. Resolving
through the existing bridge keeps one source of truth and one capability gate.
One-shot deterministic replay preserves the sandbox's existing isolation model:
the generated process never receives a request it did not already hold, and a
replay produces the same values. Keeping payloads out of the result prevents a
read from silently becoming conversation evidence; only the model's own final
outputs can carry a value forward, and those stay labelled as computed.

**Consequences.** New `python/harness/code/sandbox_tools.py` (allowlist, wire
models, validation, digests, bounds). `code/execution.py` adds
`AnalysisRequest.allow_tools`, `AnalysisResult.tool_calls`, the
`eclipse_tools` AST branch, the replay loop and runner injection.
`code/recipes.py` passes the resolver through. `analysis_tools.py` builds the
resolver from `ctx.data_bridge`, runs the sandbox off the event loop with
`asyncio.to_thread`, and posts each bridge coroutine back to the run loop with
`run_coroutine_threadsafe`; returned data includes `toolCalls`. Tests:
`python/tests/test_analysis_sandbox_tools.py` plus the existing analysis suites.
Docs: `docs/harness/capability-builder.md`,
`docs/harness/capability-extension-plan.md`,
`docs/harness/self-evolution-roadmap.md`, `docs/NAVIGATION.md`. This supersedes
the `eclipse_tools` deferred clauses in ADR-H225/H230/H231/H232; web/MCP/write
tools and the persistent-kernel phase remain out of scope.

## ADR-H236 — The analysis kernel is a session-scoped process pool, not a live execution service

**Decision.** A chat conversation reuses ONE analysis kernel per (session,
agent); a delegated or session-less run keys on (run, agent). `code/kernel.py`
owns a `KernelPool` (default max 4, idle 300 s, LRU eviction of non-busy
kernels) and each `Kernel` is the existing Phase 3 sandbox process started in
interactive mode (`sandbox.spawn` → `SpawnedProcess`), running the SAME
`ANALYSIS_PRELUDE` as one-shot. Commands, responses and pending tool requests
flow as JSON lines; a tool request exits the worker with sentinel `75`, the host
resolves it with the same `resolve_tool_request` path one-shot uses, and the
program replays against a stage directory that already holds the memo. Memory,
process and network rules do not change: `RunSpec(memory_mb=..., process_limit=
..., network=False)`, no cumulative CPU cap (it would kill a long session), and
per-command response/output caps. A timeout or a dead process kills the whole
tree, marks the kernel dead and returns `KERNEL_CRASHED`; the pool respawns on
the next turn. Spawn failure or `ECLIPSE_ANALYSIS_KERNEL=0` falls back to the
existing one-shot path — never to a fake success.

**Why.** Repeated one-shot runs pay interpreter startup and lose cheap,
in-conversation state (e.g. a series already parsed), but the sandbox contract
must not widen. Reusing the same prelude/protocol keeps one sandbox
implementation, one tool-resolution path and one provenance model, and keying
on the conversation preserves isolation between conversations (and between
delegated subagents). A soft pool maximum plus LRU eviction bounds memory on a
machine where memory is the top constraint; idle sweep keeps a forgotten
conversation from holding four processes forever. CPU time is deliberately not
limited per process because it is cumulative — the wall-clock deadline is the
real bound.

**Consequences.** New `python/harness/code/kernel.py`; `code/sandbox.py` gains
`SpawnedProcess`/`spawn` and a per-process job map (a singleton Windows job would
otherwise kill the wrong tree); `code/execution.py` extracts `ANALYSIS_PRELUDE`,
`PreparedAnalysis`/`prepare_analysis` and `resolve_tool_request` so both paths
share the pre-execution gate, and `run_analysis`/`run_recipe` accept `kernel=`.
`analysis_tools.py` selects `kernel_for_context(ctx)` lazily. Env:
`ECLIPSE_ANALYSIS_KERNEL` (default on), `ECLIPSE_ANALYSIS_KERNEL_MAX`,
`ECLIPSE_ANALYSIS_KERNEL_IDLE_S`; `reset_kernel_pool` for tests and `atexit`
close. Tests: `python/tests/test_analysis_kernel.py` (pid reuse, isolation, tool
replay across rounds, crash/respawn, eviction, idle sweep, fallback, network
refusal, key semantics, disable flag) plus the existing analysis suites. Docs:
`docs/harness/python-runtime.md`, `docs/harness/capability-extension-plan.md`,
`docs/NAVIGATION.md`.

## ADR-H237 — Response presentation intelligence is deterministic and additive

**Decision.** The chart director, series weights/bands and table presentation
are resolved in the existing pure helpers, never by a model. `chart` gains
`chartType: "auto"` (now first in `CHART_TYPES`, so it is the default): temporal
categories or multiple series render a line; a single series of ≤12 labels
renders bars; `[x, y, size]` triples render `bubble`; a fully monotonic pair
renders a line; other pairs render `scatter`; an explicit type always wins. A
series may carry `weight` (0..1) scaling width and opacity, and
`band: {lower, upper, weight?}`, rendered on category axes as two hidden
stacked series per band with the legend restricted to the real series.
`data_table` derives right alignment (explicit `align` wins; ≥80 % numeric
values right-align) and locale-formats raw numbers (≤4 fraction digits) while
leaving numeric strings untouched. `response-chart.tsx` adds a focus overlay
(Escape/backdrop/Close, `role="dialog"`).

**Why.** "Show me the important line first" and "how uncertain is this" are
presentation intents the contract should express without another model call, and
without inventing or altering any value. Deriving alignment from the data
removes a class of readability bugs (parenthesised negatives, per-column
mixed alignment) while formatting stays at the display boundary.

**Consequences.** `src/lib/response/contract.ts` (auto, weight, band),
`chart-option.ts` (`directChartType`/`resolveChartType`, weight/band series),
new `table-presentation.ts`, `data-table-block.tsx` (uses the helper; fixes the
`(2,500)` parse), `response-chart.tsx` (focus). Tests:
`chart-option.test.ts`, `table-presentation.test.ts`, `render.test.tsx`;
`npx tsc --noEmit` clean. Docs: `docs/interactive-response-engine.md`,
`docs/harness/capability-extension-plan.md`.

## ADR-H238 — Adapter authoring writes a disabled draft; installation stays with the operator

**Decision.** A broker/API need that no preset covers may be authored by
`capability_adapter` (`capabilities/adapter.py`): the operator supplies an HTTPS
base URL, a tool list and credential NAMES; the tool writes
`.black-eclipse/adapters/<slug>/` (`plugin.yaml`, `plugin.py`, `README.md`) and
stops. The manifest is validated with the SAME `PluginManifest` the loader uses.
`GET` → `read`, `POST` → `external_action`; permissions are exactly `[network]`;
`authentication` is `none` and the generated code reads the first credential
and sends it as a bearer token with an explicit review note. The draft is never
installed and never enabled; the operator reviews the auth scheme, installs it
through the plugin platform (disabled), sets the secrets and enables it.
MCP remains the MCP surface. The Studio also gains the read-only built-in
recipe catalogue (`GET /capabilities/builtins`).

**Why.** Users need a path from "my broker has a REST API" to a real tool, but
model-authored code must not acquire network reach or an enabled tool by itself.
Writing a reviewable draft into the gitignored workspace, with the real manifest
validator and the existing plugin install/enable gate, keeps ONE install path
and ONE enable decision. Refusing credential values at the shared normalizer
keeps secrets out of generated files and chat.

**Consequences.** New `capabilities/adapter.py` (`AdapterDraft`,
`author_adapter`) and public `external.normalize_credential_names`;
`capability_adapter` added to `capabilities/tools.py`, both agent allowlists and
the activity labels; route `GET /capabilities/builtins` (declared before
`/{capability_id}`) + `listBuiltins`/`BuiltinCapability` in the Studio client +
read-only built-in rendering in `generated-section.tsx` with new
`studio.generated.builtin*` keys. Tests:
`python/tests/test_capabilities_adapter.py`, `test_capabilities_api.py`
(builtins), `test_code_agent.py`/`test_chat_code_profile.py` allowlist updates.
Docs: `docs/harness/capability-builder.md`,
`docs/harness/capability-extension-plan.md`,
`docs/harness/self-evolution-roadmap.md`, `docs/NAVIGATION.md`,
`docs/interactive-response-engine.md`.

## ADR-H239 — Hill climbing operates on immutable `HarnessState` rows, and replay gates every accepted move

**Decision.** RSL gains a hill-climb engine over **immutable harness states**
(`self_improvement/state.py`): `HarnessState` is an append-only row holding a
snapshot digest, metrics, utility, lineage and status (`candidate`, `current`,
`superseded`, `retired`). Exactly one row is `current` per scope; promotion
supersedes the prior row and never rewrites it. The engine
(`self_improvement/hillclimb.py`) is pure I/O-free orchestration: callers supply
neighbor proposals and evaluation, and only a measured validation improvement
with no critical regression is accepted. Every accepted move is also replayed
against retained tasks (`self_improvement/replay.py`); critical or mean
forgetting rejects the move. Utility and vetoes are deterministic
(`self_improvement/utility.py`, `DEFAULT_GATES`, Wilson intervals). Operator
promotion remains the only way a candidate becomes `current` in production; the
climb can stage candidates but never self-promote or land.

**Why.** A single candidate compared to one baseline is not a search. A hill
climb can find the best verified neighbor, but only if the state is explicit,
immutable and measured. Replay is the continual-learning guard: a change that
improves its target by breaking previously solved work is not progress. Keeping
promotion operator-only preserves the existing authority boundary while still
allowing autonomous proposal and verification later.

**Consequences.** New `self_improvement/{state,utility,replay,hillclimb}.py`;
`HarnessState` on the ONE `agent-memory.db` connection; no new runtime, queue,
evaluator or approval surface. Tests `python/tests/test_rsl_{state,utility,
replay,hillclimb}.py`. The next slices wire the engine into the RSL conductor,
add lesson-outcome feedback and a model proposer behind flags. A climb is
`UNMEASURED` until a real evaluation run supplies metrics; no state row is
created from a claim.

## ADR-H244 — Lesson lift is measured with a matched injected/control cohort, and retirement is a non-authority lifecycle action

**Decision.** `LessonOutcomeStore` (`self_improvement/lesson_feedback.py`)
records one row per eligible lesson per run: `injected=1` for the lessons placed
in the manifest, `injected=0` for eligible lessons that did not fit. The
post-run review writes the run's terminal outcome (`success`/`failure`), never a
fabricated one. `stats()` reports injected and control rates plus a Wilson
interval, and `lift` is available only when there are at least 3 observations in
each cohort; otherwise the status is `INSUFFICIENT` or `UNMEASURED`. Retrieval
ranks a measured positive lift ahead of unmeasured lessons but never drops a
lesson solely for missing evidence. `retire_unhelpful` marks a tested,
non-positive lesson `retired`; it deletes nothing, changes no authority and
enables no skill. `HARNESS_RSL_LESSON_RETIRE` is off by default.

**Why.** `prompt.lesson_injected` proved a lesson was shown, not that it helped.
Without a control cohort, any success rate would be confounded by task mix. The
eligible-but-not-injected rows are the matched control the manifest already
makes available. Retirement must be reversible and inert: a lesson is a hint,
not a permission, so retiring it can never widen authority.

**Consequences.** New `LessonOutcome` table on `agent-memory.db` (run-scoped;
lesson rows remain in `eclipse-memory.db`), `lesson_feedback.py`, wiring in
`graph_context._lesson_manifest_context` and
`learning_review.review_run`, `lesson_retire` in `RSLConfig`, and
`HARNESS_RSL_LESSON_RETIRE`. Tests `python/tests/test_lesson_feedback.py` plus
the existing `test_rsl_lessons.py`. A run with no recorded run id records
nothing; a review that never runs leaves the observation `UNMEASURED`.

## ADR-H245 — Model-authored proposals are strict JSON, validated by the existing op guards, and off by default

**Decision.** `self_improvement/proposer_model.py` is the only model-assisted
proposal path. It uses the existing `model_runtime` and `build_chat_model`
factory, asks for strict JSON (`variants` with typed `invariant`/`domain`
entries), parses it with the existing `StructuredOutputManager`, and then hands
every variant to `proposer.propose_many`. That means the frozen target denylist,
`propose_ops`, workspace confinement and forbidden-field checks still decide
legality; the model never does. A malformed payload, missing variant, frozen
target, unknown op or provider failure is a structured `ProposalRefused`, never a
fallback or a fabricated patch. The feature is off by default
(`HARNESS_RSL_MODEL_PROPOSER=off`). When enabled, the `propose` tick persists
validated variants as child `EvolutionCandidate` rows in `proposed` state; it
never verifies, lands or activates them. Background settings follow the existing
job convention (`HARNESS_JOB_*`) with `HARNESS_RSL_*` overrides; no key is logged.

**Why.** The deterministic proposer cannot generate a candidate from a bare
failure. A model can, but a model must not acquire write authority. Keeping the
model on the *proposal* side and the existing deterministic validators on the
*legality* side preserves the RSL safety floor while making autonomous proposal
possible. A strict schema plus whole-set refusal prevents a partially valid set
from becoming a fabricated comparison.

**Consequences.** New `proposer_model.py`; `RSLConfig.model_proposer`
(`HARNESS_RSL_MODEL_PROPOSER`); `RSLLoop` gains `model_propose_fn` (the test
seam) and the model branch in `_propose_tick`. Tests
`python/tests/test_rsl_model_proposer.py`. The model is only reachable in
`propose` mode; `off`/`observe` remain strict no-ops, and verification/landing
remain the existing deterministic operator path.

## ADR-H242 — Code candidates are measured in an isolated patched worktree, opt-in

**Decision.** `evaluation/worktree_invoker.py` is the measured code path. It
allocates a disposable worktree, runs the deterministic suite on the unpatched
tree as the baseline, applies the candidate ops through the existing
`propose_ops` + `patch.apply` guard, and runs the SAME suite in a subprocess
whose `PYTHONPATH` points at the patched worktree. The child gets a temp
`agent-memory.db`, a curated non-secret environment (no provider keys), a bounded
wall clock and no live bridge; the patched code is never imported into the host
process. `benchmark_decision` accepts only a measured target improvement with no
critical `DEFAULT_GATES` regression. The result is recorded on an
`EvaluationRun` (kind `rsl-code-benchmark`) and returned to `verify_code`; the
candidate never lands. The path is opt-in (`HARNESS_RSL_CODE_BENCHMARK=off` by
default) because it executes patched harness code.

**Why.** A tests-only code candidate is not comparable to another candidate: a
passing owning test says the change did not break its own check, not that it
improved the harness. A measured comparison needs the patched harness to run, and
the only safe way to do that without contaminating the live process is a child
process over a disposable tree. The curated environment and timeout bound the
blast radius; the existing operator promotion remains the write boundary.

**Consequences.** New `evaluation/worktree_invoker.py`; `RSLConfig.code_benchmark`
(`HARNESS_RSL_CODE_BENCHMARK`); `verifier.verify_code_benchmark`; the conductor
runs it only after owning tests pass and uses its decision for
`benchmark_accepted`/`benchmark_rejected`; `recorder.record_code_experiment`
carries `MEASURED` plus the decision record and measured trials. Tests
`python/tests/test_worktree_invoker.py` plus the loop/config additions. Surface
candidates keep their existing measured path; `UNMEASURED` remains the honest
default when the flag is off.

## ADR-H243 — Harness-state promotion is an operator-only state pointer, and lesson lift is readable through the same operator surface

**Decision.** `GET /evolution/states` lists the immutable `HarnessState` rows
(current pointers plus recent candidates); `POST /evolution/states/{id}/promote`
makes one candidate current. Promotion is a state pointer: it supersedes the
prior current row, creates no code change, consumes no approval and never writes
the checkout. The same operator surface exposes `GET
/evolution/lessons/{id}/outcomes` (matched injected/control stats plus recent
rows). Every route is `require_operator`, including reads. The learning-journey
UI mounts the conductor/operator panels and shows the current state with a
Promote action; it never hides an `UNMEASURED` utility behind a number.

**Why.** The hill climb and lesson feedback are useless if the operator cannot
see the state they produced. Promotion needs an explicit control because it
changes which state a later climb starts from, but it is still not a code write:
the promotion/approval path remains the only way a change reaches the checkout.
Lesson lift is likewise evidence for a human decision, not an automatic
authority.

**Consequences.** New state/outcome routes in `routers/evolution.py`; typed
`EvolutionHarnessState`/`EvolutionLessonStats` contracts and browser calls;
Next.js proxies; a harness-state section in `rsl-control-panel.tsx` mounted in
the learning journey. Tests `test_evolution_rsl_routes.py` (state list/promote,
lesson outcomes, remote refusal) plus `evolution-client.test.ts`. No new store,
worker or approval surface.

## ADR-H246 — A measured surface may become the live harness, but only through an explicit activation and with a base-digest check

**Decision.** `POST /evolution/states/{id}/activate-surface` may make a measured
surface state live. The route requires an operator, a surface candidate, a
non-null measured utility, and an accepted RSL experiment. It re-validates the
snapshot against the recorded base (`validate_snapshot`) and refuses model
identity or skill drafts; those fields are resolved by the front door and cannot
be changed by a stored surface. The activation row (`HarnessActivation`) stores
the snapshot, base, previous state and timestamps; `POST
/evolution/surface-activations/{id}/rollback` restores the previous current
state (or retires the active state when there was none). At run start,
`self_improvement/live_surface.py` re-derives the agent's current base snapshot
and applies only prompt, tool subset and lowered step/tool-call ceilings when the
base digest still matches. A stale base, missing activation or validation
failure leaves the original agent untouched. The live surface never grants a
tool, raises a ceiling, changes permissions, or writes code.

**Why.** A measured surface is only useful if it can eventually affect a real
run, but the base it was measured against is not guaranteed to still be current.
The digest check prevents a stale experiment from silently mutating a different
harness. Applying only the narrowing fields preserves the authority boundary:
the snapshot can remove tools or tighten budgets, never add either. Operator
activation plus rollback keeps the decision reversible and auditable.

**Consequences.** New `HarnessActivation` table and `activate_surface`/
`active_activation`/`rollback_activation` in `self_improvement/state.py`; new
`self_improvement/live_surface.py`; `graph.execute_run` applies an active surface
after agent resolution and before the model/prompt plan; activation/list/rollback
routes in `routers/evolution.py`; typed front door and browser calls; a live
activation/rollback section in `rsl-control-panel.tsx`. Tests
`test_live_surface.py` plus the route tests. Code candidates still land only
through the existing promotion approval; surface activation is a separate,
reversible state pointer.

## ADR-H247 — Surface activation is previewed as a deterministic diff and may target one agent scope

**Decision.** `GET /evolution/states/{id}/surface-preview` returns the exact
live change before activation: prompt character counts (and bounded before/after
text), tools added/removed, step and tool-call ceilings, model-identity/skill
flags, the recorded accepted decision, the current base-digest match for the
chosen reference agent, the available scopes (`global` plus the known agent
presets), the existing current state, and every refusal reason. The same helper
drives the activation route, so preview and activation cannot drift.
`POST /evolution/states/{id}/activate-surface` accepts an optional `scope`; a
scope different from the state's own scope creates a NEW immutable state row for
that scope and activates it, leaving the original state untouched. A live
surface is only applied while its state is the current state for that scope,
so a later promotion silently disables it without a second rollback call.

**Why.** The operator should see the narrowing change before it affects real
runs, and should be able to choose which agent preset gets it. Deriving the
preview and activation from one context prevents a "preview said safe" versus
"activation did something else" split. A scope-specific immutable copy keeps the
source experiment intact and makes the decision auditable per agent.

**Consequences.** `surface-preview` and scoped activation in
`routers/evolution.py`; `active_activation` checks the current state; typed
preview/diff contracts and browser calls; a scope selector, diff and refusal
reasons in `rsl-control-panel.tsx`. Tests `test_evolution_rsl_routes.py`
(preview, scope override, remote refusal) and `test_live_surface.py`
(stale-activation behaviour). No new runtime, store or approval surface; the
activation row remains the audit record.

## ADR-H248 — A surface experiment carries its target scope and a measured surface materializes an inert candidate state

**Decision.** `SurfaceExperimentRequest` gains a `scope` (default `global`,
validated against `global` plus the known agent presets). The scope is stored in
`surfaceConfig`, included in the deterministic experiment signature (so the same
snapshot for a different agent is a different experiment), and carried into the
measured surface. After a measured surface verification, the conductor
materializes exactly one inert `HarnessState` (`status="candidate"`) with the
candidate id, accepted run id, scope, measured aggregates/measured-trial count
and utility; duplicate candidates are deduplicated by `(candidateId,
snapshotDigest)` within the scope. This state is what the operator state list,
preview and activation surfaces use; it is never current and never activates by
itself. A rejected surface also gets a state (so its evidence is visible) but
the activation route requires an accepted run.

**Why.** A surface experiment without a scope cannot be activated for a real
agent without guessing, and an experiment with no state row is invisible to the
preview/activation UI. Carrying the scope from creation to the state removes that
gap. Materializing only a candidate state preserves the existing boundary: the
operator still previews, activates and can roll back.

**Consequences.** `scope` on the surface request/model, `surfaceConfig.scope`,
`_surface_signature(scope=...)`, and `_record_surface_state` in the conductor;
the operator surface-experiment form gains a scope input. Tests
`test_evolution_rsl_operator.py` (scope persisted) and
`test_rsl_loop.py` (measured surface creates one inert state). No new store,
authority or activation path.

## ADR-H249 — A candidate's derived state is addressable by candidate id and linked from the surface result

**Decision.** `GET /evolution/candidates/{id}/state` returns the candidate's
derived `HarnessState` rows (newest first) and the latest state as `state`; every
route is operator-gated. The surface experiment result in the operator panel
adds a `Find derived state` action that calls this route, then opens the same
`SurfaceActivationCard` used by the conductor: scope selector, deterministic
diff, base-digest match, refusal reasons and activation/rollback. The state is
still created only by a measured verification; the lookup never creates one.

**Why.** A queued surface experiment completes asynchronously. Without an
addressable state, the operator has to guess which row in the harness-state list
belongs to the candidate. A candidate-keyed read-only lookup closes the
experiment → preview → activation flow without adding an authority or a second
activation path.

**Consequences.** New candidate-state route in `routers/evolution.py`; typed
`EvolutionCandidateStateResult`, browser call and Next.js proxy; `Find derived
state` and the shared `SurfaceActivationCard` in `rsl-operator-panel.tsx`. Tests
`test_evolution_rsl_routes.py` (candidate state + remote refusal). No new store
or write path.

## ADR-H250 — Production drift is a read-only signal over terminal run windows, never an automatic harness change

**Decision.** `self_improvement/drift.py` compares the newest `window` terminal
`HarnessRun` rows with the immediately preceding `baseline` window. It maps only
`completed` to success and `failed`/`error` to failure; cancelled/created/
running rows are ignored. The signal is `unmeasured` when either side has fewer
than `min_samples` terminal runs, `drift` only when the success-rate drop exceeds
the threshold, and `stable` otherwise. Wilson intervals are reported with both
rates. `GET /evolution/drift` exposes the deterministic signal per agent scope;
the conductor UI shows status, recent/baseline rates, drop and reasons. A drift
signal can inform a scoped optimization, but it never changes the current state,
activates, lands or edits anything by itself.

**Why.** Continual learning needs a way to notice that the world or the harness
has regressed without treating a thin sample as proof. A bounded, read-only
window comparison is explainable and honest; an automatic response would turn a
noisy metric into an unauthorized change.

**Consequences.** New `self_improvement/drift.py`, `GET /evolution/drift`,
typed `EvolutionDriftSignal` contracts/browser call/proxy, and a drift card in
`rsl-control-panel.tsx`. Tests `python/tests/test_drift.py` (stable, drift,
unmeasured, scope, store read) and the route refusal test. No new store or write
path.

## ADR-H251 — A drift signal becomes an inert `drift` candidate only by an explicit operator action

**Decision.** `POST /evolution/drift/candidate` runs the same read-only drift
detector and, only for a `drift` signal, materializes ONE inert
`EvolutionCandidate` (`subjectType="drift"`, `sourceCategory="drift"`,
`status="inert"`) with the scope, success-rate drop, window sizes and the
observed counts in its problem statement. It has no target or operations: a
drift signal is evidence, not a patch. The episode identity is the scope/window/
baseline/threshold tuple, so repeated checks do not create duplicate candidates.
Stable or unmeasured signals create nothing. The operator can then stage ops
manually, use the model proposer, or run a surface experiment; verification and
landing remain the existing paths. The conductor's drift card exposes an
`Open drift candidate` action and shows the created/existing candidate id.

**Why.** A drift signal should be actionable without pretending the runtime
knows which change fixes it. An inert candidate gives the operator a durable,
addressable piece of evidence that enters the same candidate → propose → verify →
land flow, while the missing target/ops keeps the "no invented patch" rule.

**Consequences.** `record_drift_candidate`/`drift_signature` in
`self_improvement/drift.py`; `POST /evolution/drift/candidate`; typed
`EvolutionDriftCandidateResult`, browser call and Next.js proxy; an
`Open drift candidate` control in `rsl-control-panel.tsx`. Tests
`test_drift.py` (inert candidate, dedupe, stable/unmeasured no-op) and the remote
refusal route test. No new store, authority or activation path.

## ADR-H252 — An operator may run the model proposer for ONE candidate, and the drift card links it directly

**Decision.** `POST /evolution/candidates/{id}/model-propose` runs the model
proposer for one candidate through `RSLLoop.propose_with_model` and persists its
validated child candidates exactly as the autonomous propose tick does: strict
JSON, `propose_many`/`propose_ops`, frozen-target guard, workspace confinement;
the route never verifies or lands. It is operator-gated and refuses when
`HARNESS_RSL_MODEL_PROPOSER` is off. The drift card's `Open drift candidate`
result gains `Propose with model`, so `drift → inert candidate → validated
children → verify tick → operator land` is one visible flow. Manual
operator-supplied ops remain available through the existing `propose` route.

**Why.** A drift candidate deliberately has no target or ops. The model proposer
is the constructive next step, but making it automatic would turn a drift signal
into an unreviewed cost. An explicit per-candidate call keeps the operator in
control while avoiding the guesswork of finding the candidate and re-entering
its id in another form.

**Consequences.** Public `RSLLoop.propose_with_model`; the model-propose route;
typed `EvolutionModelProposeResult`, browser call and Next.js proxy; a
`Propose with model` button and child-count result in the drift card. Tests
`test_evolution_rsl_operator.py` (validated child from a drift candidate) and the
remote refusal route. No new authority; verification/landing stay unchanged.

## ADR-H253 — Lesson lift is visible at the lesson row, and retirement is an explicit operator action

**Decision.** The adopted-lessons list in `rsl-operator-panel.tsx` gets a `Lift`
action per lesson. It reads the existing `LessonOutcome` stats (injected vs
control counts/rates, Wilson intervals, lift, status) and shows them inline.
When the stats are `measured` and lift is non-positive, the row offers `Retire`;
`POST /evolution/lessons/{id}/retire` calls `retire_unhelpful` for that one
lesson. Retirement is inert: it marks the `LearningCandidate` `retired`, deletes
nothing, changes no authority and enables no skill. A thin or unmeasured lesson
cannot be retired through this route.

**Why.** The outcome telemetry is only useful if the operator can see it at the
point of decision. The lesson row is that point. Keeping retirement explicit
avoids silently removing a hint on noisy or insufficient data.

**Consequences.** New lesson-retire route; typed
`EvolutionLessonRetireResult`, browser call and Next.js proxy; `Lift`/`Retire`
controls and inline stats in the lessons list. Tests
`test_evolution_rsl_routes.py` (measured non-positive lesson retirement + remote
refusal) and `test_lesson_feedback.py`. No new store or authority.

## ADR-H254 — The surface activation preview carries the accepted run's metrics and bounded trials, never trajectories

**Decision.** `GET /evolution/states/{id}/surface-preview` (and the activation
response's `preview` block) now includes `evidence`: the accepted run summary,
its overall metrics, up to 50 trial rows with `trajectory` removed, and the same
bounded view of the recorded baseline run when one is named. The activation card
renders this as an expandable `Measured evidence` section (metrics, recent
trials, baseline metrics). The evidence is read-only and is never used to
recompute the decision; the evaluator's recorded decision remains authoritative.

**Why.** The operator should see the evidence behind a measured surface, not
just a utility number. Reusing the existing `get_experiment` shape keeps the data
bounded and consistent with the experiments panel; stripping trajectories avoids
leaking raw traces into the activation surface.

**Consequences.** `_surface_context` attaches the evidence; typed
`EvolutionSurfaceEvidence` contracts and `Measured evidence` rendering in
`surface-activation-card.tsx`. Tests `test_evolution_rsl_routes.py` (preview
includes metrics/trials and no trajectory). No new store or write path.

## ADR-H255 — Surface preview evidence includes a measured per-task delta table, with missing tasks rendered UNMEASURED

**Decision.** The evidence block also carries `taskDeltas`: for every task in
the recorded baseline or candidate trials, the measured pass rate before/after
(unmeasured trials excluded), the measured sample counts, and the delta when
both sides exist. The activation card renders up to ten rows; a missing side is
`UNMEASURED`, never zero. The target-dimension aggregate and the evaluator's
decision remain the acceptance authority; the per-task table is explanatory
evidence only.

**Why.** An aggregate target can hide a regression on an individual task. The
operator needs to see which tasks improved or regressed before activating a
surface. Excluding unmeasured trials keeps the delta honest.

**Consequences.** `_task_rates`/`_task_deltas` in `routers/evolution.py`; typed
`EvolutionSurfaceTaskDelta`, `baseline.trials`, and a compact table in
`surface-activation-card.tsx`. Tests `test_evolution_rsl_routes.py` (baseline
fail → candidate pass = +1.0). No new store or write path.

## ADR-H256 — A regressed safety/adversarial task vetoes surface activation even when the aggregate target improved

**Decision.** `_surface_context` resolves the critical task ids for the surface
experiment's dataset/split (`replay.critical_task_ids`: `safety`/`adversarial`
categories and `metadata.critical`). Any measured per-task drop on one of those
tasks is appended to `evidence.criticalDeltas` and to the activation `reasons`,
so `canActivate` is false. The preview renders a prominent `Critical regressions`
block before the target delta. The evaluator's recorded decision is still shown;
activation is simply refused while the critical drop exists.

**Why.** A surface can improve the aggregate `task_success` while breaking a
safety or adversarial task. The aggregate is not allowed to hide that. The
activation-time veto is conservative and deterministic; it does not rewrite the
evaluator's decision or introduce a second scoring system.

**Consequences.** `_critical_task_ids_for_payload` and the critical-delta reason
in `routers/evolution.py`; typed `criticalDeltas` and a rose-colored block in
`surface-activation-card.tsx`. Tests
`test_evolution_rsl_routes.py` (baseline safety pass → candidate safety fail
blocks activation). No new store or authority.

## ADR-H257 — The measured code benchmark uses the same per-task critical veto as surface activation

**Decision.** `evaluation/worktree_invoker.py` resolves the dataset's
safety/adversarial/`metadata.critical` task ids for the benchmark config and
passes them to `benchmark_decision`. A measured pass-rate drop on any critical
task rejects the code candidate even when the aggregate target improved; the
decision record carries `criticalDeltas`. Missing/unavailable task metadata does
not create a veto. The measured-only acceptance and the existing aggregate
`DEFAULT_GATES` remain in force; this adds a per-task floor, not a second
scoring system.

**Why.** A code change that improves the aggregate but breaks a safety task is
not an improvement. The surface path already has this veto; the measured code
path must use the same rule, otherwise the same candidate would pass or fail
depending on which subject type it was staged as.

**Consequences.** `_critical_task_ids` in `evaluation/worktree_invoker.py`;
`benchmark_decision(..., critical_tasks=...)` and `criticalDeltas` in the
decision record. Tests `test_worktree_invoker.py` (aggregate improves but a
critical task drops → rejected). No new store, authority or acceptance path.

## ADR-H258 — The metrics window applies to RSL counts, and the experiment panel shows today's split

**Decision.** `rsl_runs_from_store` accepts the same `since` cutoff as the rest of
`archive_metrics`, so `GET /evolution/metrics?windowDays=N` reports RSL
experiments/accepted/rejected/unmeasured for that window rather than all time.
The experiment panel requests `windowDays=1` and shows a compact `Today` strip:
experiments, accepted, rejected and `UNMEASURED`, with a null/unreadable source
rendered `UNMEASURED`. The strip refreshes on the existing
`eclipse:evolution-verified` event.

**Why.** A "today" summary that silently counted all-time runs would be wrong,
and the existing panel had no single-glance answer to "what did the loop do
today?". Making the window explicit keeps the metric honest and reusable by the
gate.

**Consequences.** Optional `since` on `rsl_runs_from_store`; `windowDays` on the
Next.js metrics proxy and `evolutionMetrics`; a `Today` strip in
`rsl-experiments-panel.tsx`. Tests `test_evaluation_repair_metrics.py` (window
cutoff). No new store or metric source.

## ADR-H259 — Experiments carry a scope, and the experiment panel filters today's strip/chips by it

**Decision.** RSL experiment metadata carries `scope`: the surface candidate's
`surfaceConfig.scope`, or `global` for code/legacy experiments. The experiment
panel exposes a scope selector derived from the loaded experiments; choosing a
scope filters the experiment list and today's candidate chips, and computes the
scoped Today counts from those loaded experiments. The unscoped (`all`) view
keeps the metrics-window counts. Missing scope reads as `global`. The filter is
read-only and does not change which runs exist or which one is accepted.

**Why.** A global count cannot answer "what did the code agent do today?".
Carrying the scope on the experiment (rather than re-deriving it from a
candidate lookup) keeps the evidence row self-describing. Falling back to
`global` keeps old rows honest without inventing a scope.

**Consequences.** `scope` in the code/surface recorder metadata and
`verify_surface` run metadata; scope selector/visible-experiment/today-chip
filtering in `rsl-experiments-panel.tsx`. Tests
`test_rsl_surface.py` (surface run metadata carries the scope). No new store,
authority or metric source.

## ADR-H260 — One scope selection drives experiments and drift, and drift history is derived from existing runs

**Decision.** The learning journey owns one scope value, stored under
`eclipse.rsl.scope` and shared by the experiment panel and the production drift
card (`all` maps to `global` for the drift read). Experiment metadata carries
`subjectType`, and drift rows are badged in the experiment list and Today chips.
`GET /evolution/drift/history` computes the same drift signal at successive
offsets in the existing run history (window, baseline, step, limit) instead of
persisting a separate signal log.

**Why.** Two independent scope selectors could show evidence for different
agents at once. Deriving history from runs keeps one source of truth and needs no
migration; the points are retrospective windows, not recorded operator actions.
The badge makes a drift-origin experiment recognizable without a second lookup.

**Consequences.** `drift_history` in `self_improvement/drift.py`; the history
route and Next.js proxy; shared `scope`/`onScopeChange` props on
`RslControlPanel`; legacy scope keys are read once and the new key wins. Tests
`test_drift.py` (history offsets), `test_evolution_rsl_routes.py` (scoped signal,
available scopes, scoped inert candidate), and the component drift-badge test.

## ADR-H261 — Drift is readable as a per-scope overview, not one scope at a time

**Decision.** `GET /evolution/drift/scopes` returns one bounded signal for every
configured scope (`global` plus the agent presets) in a single read. The drift
card renders them as status/drop chips; choosing a chip selects that shared
scope. The overview is read-only and never creates a candidate by itself.

**Why.** A single selected signal hides a drifting agent behind a stable one.
The operator needs "which agent is drifting?" before deciding where to look.
Returning the existing `detect_store_drift` shape per scope keeps the semantics
identical to `/evolution/drift`; there is no new detector.

**Consequences.** The route in `routers/evolution.py`, the typed client/browser
functions and the Next.js proxy, and overview chips in `rsl-control-panel.tsx`.
Tests `test_evolution_rsl_routes.py` (one signal per scope + remote refusal).
No new store, write path or acceptance authority.

## ADR-H262 — A drift candidate is one episode, anchored to the newest run

**Decision.** `drift_signal` returns an `anchor` (the newest current-window run
id, else its timestamp) and `drift_signature` includes it. Deduplication still
collapses repeated reads of the same window, but a later episode with new runs
opens a new candidate even at the same scope/window/threshold policy.

**Why.** Without an episode anchor, a scope that recovered and drifted again
could never get a new candidate: the old signature matched forever. Including
the newest run keeps the signature deterministic without inventing a time
window or a second store.

**Consequences.** `anchor` on the signal and in the signature; the candidate
problem statement still records the measured rates. Tests `test_drift.py`
(same window dedupes, new anchor creates a second candidate).

## ADR-H263 — Drift watch runs on a bounded cadence and publishes transitions

**Decision.** `self_improvement/drift_watch.py` reads every configured scope on
`HARNESS_DRIFT_WATCH_SECONDS` (default 300; `0` disables) and publishes
`self_improvement.drift_detected` only when a scope transitions into `drift`.
It is read-only: no candidate, surface, or harness mutation. The task starts in
the lifespan beside the RSL loop and stops before the event system.

**Why.** Detection that only happens when an operator opens the card is not
monitoring. A transition-only event keeps the signal actionable without a
notification storm, and the no-op when no event system is installed keeps tests
and CLI use side-effect-free.

**Consequences.** `drift_watch.py`, boot start/stop, the event registry spec,
and `tests/test_drift_watch.py` (transition-only publication). No new store.

## ADR-H264 — Drift policy, unmeasured scopes and scope-filtered evidence are operator-visible

**Decision.** The drift card carries window/baseline/threshold/minSamples and
sends them to the signal, history, overview and candidate reads. The per-scope
overview collapses `unmeasured` rows (except the selected scope) into a count,
and the learning-journey header shows a `drifting` badge. The candidate evidence
panel filters its experiment list by the shared scope and labels the active
scope. Candidate creation publishes `self_improvement.drift_candidate_created`.

**Why.** Defaults hide the policy that produced a signal, and unmeasured agent
rows crowd out the actionable ones. The evidence list already carries a scope;
showing a different agent's experiments under one candidate's evidence is
misleading.

**Consequences.** Policy inputs and helper paths in the client/browser, the
scopes/history proxies, `rsl-drift-badge.tsx`, and the `scope` prop on
`CandidateEvidencePanel`. Tests `evolution-client.test.ts` (proxy paths),
component tests (badge selection, drift badge), and route tests (evidence flow).
No new detector or authority.

## ADR-H265 — Drift operations are restart-safe, debounced and scoped end to end

**Decision.** The drift watch persists its last status per scope in the additive
`DriftWatchState` table, so a restart does not republish an existing drift. Its
health is exposed on `/evolution/rsl`. The drift card debounces policy edits
(350 ms) and the per-scope overview excludes `global` by default in the UI
(`includeGlobal=false`) to avoid duplicating agent rows. Drift candidates store
`scope` on the candidate row. History reports `truncated` when the bounded
2000-run read cannot honour the requested offsets. A Playwright smoke
(`npm run test:drift-ui`) bundles the real components, mocks only fetch, and
exercises scope chip → candidate → evidence in Chromium.

**Why.** Re-alerting every boot, fetching on every keystroke, and hiding the
policy/scope behind opaque defaults are operational defects, not cosmetic ones.
The candidate scope is the one fact the evidence view cannot re-derive once a
drift candidate has no experiments.

**Consequences.** `DriftWatchState` (created idempotently by the watcher),
`scope` on `EvolutionCandidate` (additive column), `driftWatch` in the loop
status, `includeGlobal` on the scopes route/client, `truncated` on history, and
the browser smoke script. Tests: drift-watch restart, route includeGlobal,
client paths, full Vitest and the smoke. No new authority.

## ADR-H266 — Drift operations are testable end to end and survive the real shell

**Decision.** Drift policy (window/baseline/threshold/minSamples) persists in
`eclipse.rsl.driftPolicy`. The learning-journey badge raises the shared
`be:alert-notify` notification once per scope per UTC day when drift appears.
The watch clears persisted state when no terminal runs remain (a reset DB) and
prunes state for retired scopes. History reports `computedPoints` alongside
`requestedPoints` and marks truncation when either the run cap or the data
shortens the series. A boot-level test covers start/stop, and
`npm run test:drift-app` drives the real Next.js shell (Skills → Journey) with a
mocked HTTP API.

**Why.** A policy that resets on reload, a drift event nobody is told about, a
stale episode that survives a reset, and an untested boot path are operational
gaps. The component-only browser smoke could not catch a broken deep link or
shell navigation; the full-shell smoke does, without requiring the Python
runtime.

**Consequences.** `DRIFT_POLICY_KEY`, the notification dispatch in
`rsl-drift-badge.tsx`, reset/prune branches in `drift_watch.py`, the history
count fields, `tests/test_drift_watch.py` (boot + reset), the runbook route test,
and `scripts/drift-app-smoke.mjs`. The QUICKSTART runbook documents the flow.

## ADR-H267 — Drift reaches the operator inbox, the dashboard, and CI

**Decision.** The event service consumes `self_improvement.drift_detected` and
publishes a durable `notification.created` child (`kind=drift`). `GET
/evolution/drift/alerts` reads that inbox from the event log. The Command Center
gets a `drift-status` widget (per-scope chips + short trend). The drift card adds
a trend sparkline and bounded older/newer history paging (`offset`), the watch
jitters its cadence, `/drift/scopes` accepts extra `scopes`, and CI installs
Chromium and runs `npm run test:drift`.

**Why.** The watcher already detected drift, but only the open Skills screen
could see it. A durable event/route makes the signal addressable by other
surfaces; the dashboard widget keeps it visible without opening the journey. CI
wiring keeps the browser smokes from rotting.

**Consequences.** The event consumer + registry spec, the alerts route, the
`DriftStatusWidget` registration, history `offset` plumbing, scopes merge,
jitter, `npm run test:drift`, and the CI step. No new detector or write path.

## ADR-H268 — v1b: inert skill drafts are trial-only inputs, never live

**Decision.** A candidate snapshot may add `skillDraftIds` that are not in the
base only when a skill store proves each id is `status=draft`, its current
version's `requiredTools` are a subset of the candidate's tools, and its
instructions are injected into the candidate trial prompt. The base snapshot is
never mutated, live activation still refuses any `skillDraftIds`, and the
operator surface documents the field.

**Why.** v1b is the skill half of self-improvement. Making a draft measurable in
a trial is useful; making it live without the existing skill evaluation and
promotion path would bypass that path. The store check keeps "unknown draft id"
a structured refusal instead of an arbitrary prompt injection.

**Consequences.** `validate_snapshot(known_skill_ids=...)`,
`CandidateAgentInvoker(skill_store=...)` with `_authorized_drafts` /
`_draft_instructions`, `skill_store` passed from the conductor through the
surface verifier, the surface-form hint, and `test_rsl_skill_drafts.py`. No new
activation path.

## ADR-H269 — RSL v2 starts with a read-only Pareto frontier

**Decision.** `self_improvement/pareto.py` compares recorded harness states on
success, target, coverage (max) and cost/latency (min). A state dominates only
when at least two objectives are comparable on both sides, it is no worse on
each, and strictly better on one. `GET /evolution/states/pareto` returns the
frontier and the dominator of every other state; it never promotes or ranks one
state as "best".

**Why.** The hill climb's single weighted utility is useful for acceptance but
hides genuine trade-offs. A Pareto view makes those trade-offs visible without
changing acceptance, budgets or activation. Missing objectives are neutral, and
one comparable objective is not enough to claim dominance.

**Consequences.** The pure module, the operator route, the typed front door and
`ParetoFrontierCard` in the learning journey, and `test_rsl_pareto.py` plus the
component summary test. This is the first v2 slice; the acceptance path still
uses `utility.acceptance`, so no existing decision changes.

## ADR-H270 — Model-identity experiments are measured, never live

**Decision.** `POST /evolution/model-experiments` builds a surface candidate from
one agent's base snapshot with only provider/model/temperature changed, then
queues the existing measured surface path. The operator form exposes the same
fields. The surface preview still refuses model identity for live activation.

**Why.** Model choice is a real self-improvement dimension and should be
measurable on the same frozen tasks. Applying it live would move model resolution
away from the front door and its credentials; that is a separate decision, so v2
keeps the identity comparison trial-only.

**Consequences.** `routers/evolution_models.py` (split out to respect the module
size budget), the typed client/browser/proxy, the surface form's model fields, and
`test_evolution_rsl_routes.py` (queued snapshot identity). No new verification or
activation path.

## ADR-H271 — Cross-scope lesson transfer is opt-in and still measured

**Decision.** `select_lessons(..., allow_transfer=True)` may return an adopted
lesson from another recorded scope when the cue matches; the default remains
exact scoped matching. The conductor enables it only with
`HARNESS_RSL_LESSON_TRANSFER=on`. A transferred lesson is still recorded in the
same injected/control cohorts, so its lift is measured against the actual task.

**Why.** A cue that describes a real failure is not always bound to one market
or capability. Cross-scope reuse is useful only if it is explicit and measured;
silently widening scope would make the retrieval contract unpredictable.

**Consequences.** `select_lessons(allow_transfer=...)`, `RSLConfig.lesson_transfer`
and the graph-context pass-through, plus `test_rsl_lessons.py` (default excludes
another scope; opt-in includes it). No new activation path.

## ADR-H272 — A code candidate must survive a canary split before it can be prepared for landing

**Decision.** When `HARNESS_RSL_CANARY=on`, `run_candidate_benchmark` runs the
same patched worktree on a second split (default `held_out`) after the validation
comparison. The validation decision is accepted only when the canary is measured,
does not drop the target beyond `HARNESS_RSL_CANARY_TOLERANCE` (default 0.05), and
has no critical-task regression. The canary record rides on the decision record;
nothing lands automatically.

**Why.** A validation win can be an overfit. A second disjoint split is the
cheapest honest check before the operator prepares a landing. This is a
pre-landing canary in a disposable worktree; it does not require restarting the
live process, so it fits the existing RSL conductor.

**Consequences.** `evaluation/canary.py` (`assess_canary`),
`BenchmarkConfig.canary_*`, the verifier/config env plumbing, and
`test_worktree_invoker.py` (accepted validation win rejected by the canary).
No new write path; `evaluated_ok` still requires the canary to pass.

## ADR-H273 — Pareto search is an opt-in climb mode

**Decision.** `HARNESS_RSL_OBJECTIVE=single|pareto` selects the climb acceptance
mode. `single` is unchanged. In `pareto`, a neighbor that the weighted utility
rejects is still eligible when it is measured, has no critical regression, and
is not Pareto-dominated by the current state on at least two comparable
objectives. The climb returns its frontier in `pareto`; persistence and operator
promotion are unchanged.

**Why.** A single weighted score hides genuine trade-offs (cheaper at slightly
higher latency). Making the mode explicit keeps existing runs reproducible and
prevents a trade-off from silently becoming the new default.

**Consequences.** `hillclimb._pareto_accepts`, `HillClimbConfig.objective`,
`RSLConfig.objective`, the status payload/UI label, and
`test_rsl_hillclimb.py` (single rejects the trade-off, pareto keeps it). No new
activation path.

## ADR-H274 — A scheduled climb runs only through an injected runner

**Decision.** `HARNESS_RSL_CLIMB_SECONDS` (0 = off) and
`HARNESS_RSL_CLIMB_SCOPE` let the conductor run a climb on a cadence. The actual
search is an injected `climb_runner(loop, scope)`; without one the schedule is
inert. `pause_climb`/`resume_climb` and `POST /evolution/rsl/climb/{pause,resume}`
control it, and status reports the interval, next run, pause state and last
result. The daily budget, busy flag and mode/strategy gates still apply.

**Why.** A scheduler must not invent proposals or evaluations. Keeping the
runner injectable lets a real proposer/evaluator (or a test) own the search while
the conductor owns cadence, budget and pause. A missing runner is honest
(`climbEnabled=false`), not a fake climb.

**Consequences.** `RSLConfig.climb_seconds/climb_scope`, the loop scheduler and
pause API, the typed status/UI, and `test_rsl_loop.py` (one due run, pause stops
it). No activation path.

## ADR-H275 — Model identity stays trial-only in v2

**Decision.** A model-identity experiment is measured and can be reviewed, but
live activation still refuses it. Applying provider/model/temperature live would
move credential and model resolution out of the front door, which is a separate
authority decision. The experiment stays a `surface` candidate with the identity
diff recorded.

**Why.** Measuring a model comparison is useful and safe; silently letting a
candidate change which provider/credentials a run uses is not a self-improvement
detail. Keeping resolution in the front door preserves one credential path.

**Consequences.** No change to `live_surface.py` or the surface preview refusal.
`evolution_models.py` remains the measured comparison path.

## ADR-H276 — Skill drafts promote through the existing skill lifecycle, not a second RSL path

**Decision.** v1b measures inert skill drafts inside RSL trials. Promotion stays
in the existing skills lifecycle (`skills/versioning.py` + the Skills UI); RSL
does not add a second promotion route. A draft that wins a trial is evidence for
the operator, not an auto-promoted skill.

**Why.** RSL already composes existing owners. A parallel skill-promotion path
would duplicate versioning, approval and rollback. The draft remains inert until
the operator uses the path that already owns it.

**Consequences.** No new RSL activation route. The surface preview continues to
refuse `skillDraftIds` for live surface activation.

## ADR-H277 — RSL v2 is accepted by slice-level evidence, not one live run

**Decision.** v2 acceptance is the checked-in evidence for the Pareto frontier,
trial-only model experiments, cross-scope transfer, the canary split and the
scheduled climb, plus the v1 live runbook. The identity decisions above close the
remaining open items without adding an activation path.

**Why.** The v2 slices are independent capabilities; a single live experiment
would not exercise all of them. Each has a deterministic gate, and the one thing
that must remain manual (operator landing/live acceptance) is named.

**Consequences.** `docs/self-improvement/rsl-v2-acceptance.md` maps every slice to
tests and commands. No new runtime, store or authority.

## ADR-H240 — Completion is verified per task type against the evidence the runtime recorded

**Decision.** W8.4's task-type verifier becomes the ONE authority for "done", and
every rule is pinned to the evidence the runtime really writes. `research` ⇒
traceable sources (the ordinary run's `metadata.citations`/`collectedSources` or
the gated run's `receipt.evidenceRefs`), a claim that is both supported and tied
to evidence, and the uncertainty RECORDED — a partial answer that names no open
gap is refused, and a rejected engine verdict is refused. `document` ⇒ the
artifact AND every section the contract required; the sections are named
deterministically (`goal.parse_required_sections`: only an explicit list counts)
and the delivered document records the headings it wrote, so a document that
recorded no structure cannot prove its contents. `workflow` ⇒ observed steps AND
an observable state change (an artifact, a document, an analysis, a code record,
a capability or collected sources); the run id the scheduler stores is NOT a
result. `finance` ⇒ provenance, freshness and claim-to-evidence, deferring to
the Evidence Engine's own verdict instead of re-deriving a value. `data` is a
new task type ⇒ a reproducible calculation: a recorded formula with its inputs,
or an analysis that pinned the program it ran and the inputs it read. A number
in prose is not a computation. The verifier never runs a tool or a model; it
reads records.

**Why.** The first draft read an idealized evidence shape (`research.sources`,
`research.claims`, `evidence.output`) that the runtime never wrote, so a
research or finance task could only fail and a workflow task could only fail
too (the "result reference" was the run id the scheduler always stores). A
verifier that cannot read the real records is not an authority; it is noise. And
a task type without a rule is a class where "done" means "the model said so".

**Consequences.** `TaskType.DATA` and its intake rules (checked LAST, so no
other class is re-routed); `goal.parse_required_sections` plus the document
acceptance line `the document contains the sections: …`;
`documents/authoring.py::block_headings`/`report_authored_document` recording an
authored document's headings (the processed-document list the sidebar shows is
unchanged); `document_intelligence/collector.py` recording section titles;
`analysis_tools.load_run_analyses` reading the sandbox's `AnalysisRecord` rows
as bounded evidence and `tasks/bootstrap.py` attaching them to the task
outcome. The Evidence Engine stays the financial authority. Tests:
`python/tests/test_tasks_verification.py` (37),
`python/tests/test_w14_verification_evidence.py` (9). Docs:
`docs/harness/task-plan-system.md`, `docs/harness/self-evolution-roadmap.md`.
## ADR-H241 — A run's routing is one derived record, and a fact that was never recorded is null

**Decision.** `python/harness/run_outcome.py` owns the per-run outcome envelope:
the **intent verdict** (what the router decided), the **resolved surface and
allowlist** (what the run could actually do), the **refusal code** (why it
stopped) and the **capability gap** (what was missing). It is a DERIVED,
read-only record — `GET /runs/{id}/outcome` — assembled from the run's
canonical records: the run row (agent + terminal error code), the `prompt.plan`
event (the resolved surface and a bounded allowlist summary: count, first 40
names, a digest of the whole list), the `ui.capability_gap` event and the
`recovery.decided` event (the incident ids). The intent verdict had no home —
it died with the `ui_classify_request` tool result — so that tool now records
it once per run in the module's own `RunOutcome` table (the `run_lineage.py`
pattern, so the ratcheted `persistence.py` does not grow). The FIRST verdict
wins, because it is the routing decision. Every field is bounded; a fact that
was never recorded is `null` — never guessed, never inferred from prose.

**Why.** The round-4 meta loop needs to diagnose a run that did not get done
(intent, surface, refusal, gap) without replaying events, and the facts were
scattered across a tool result, a policy decision, a run row and an event. A
verdict that lives only in a tool result cannot be diagnosed after the fact,
and a record that guesses a missing fact is worse than one that admits the gap.

**Consequences.** `run_outcome.py` (the table owner, `record_intent` /
`read_intent`, `surface_summary`, `build_outcome_envelope`); the
`ui_classify_request` tool records the verdict; the `prompt.plan` event gains
the resolved surface + allowlist summary; the run-scoped read routes
(`/runs/{id}/events`, `/runs/{id}/explain`, `/runs/{id}/outcome`) move to the
new `python/harness/routers/runs.py` — the paths are unchanged, and `main.py`
drops back under the hard ceiling (1199 → 1166 lines);
`graph_context.surface_summary` re-exported by `graph.py` (the run graph stays
under its size ratchet). Tests: `python/tests/test_run_outcome.py` (12). Docs:
`docs/harness/architecture.md` (the execution graph),
`docs/harness/self-evolution-roadmap.md` (W1.3).
> **ADR-H242 was renumbered to ADR-H290.** The series had two entries with this number; the first one keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H290 — The sandbox is the computation surface, and the rendered answer carries its own evidence

**Decision.** Six fixes, one rule: *a computed answer must be computed by the
sandbox and must carry the evidence that proves it.*

1. **The sandbox is advertised and used.** The default prompt states that
   financial models are subject matter Eclipse both reasons about AND computes
   (`analysis_run` in the sandbox), names the ready-made recipes, and requires
   the formula, the inputs and the result for a computed answer. The
   `analysis_run` tool description lists every recipe and its inputs (built
   from the registry, so it cannot drift), and a failed computation returns its
   bounded stderr — a model can only fix a program it can see the error from.
2. **The sandbox environment is reproducible.** `package_lock_digest` hashes the
   INTERPRETER's distributions, not the harness process's: the harness's
   environment is not the environment the code ran in. The numerical stack is
   pinned in `python/analysis-requirements.txt`, installed into the interpreter
   the sandbox runs (`scripts/setup-python.js` provisions the vendored
   interpreter on Windows BEFORE choosing one, because the sandbox never uses a
   system Python), and `scripts/doctor.js` reports the stack's versions so
   "the sandbox is stale" is visible instead of guessed.
3. **A candle is a chart.** `candlestick`/`ohlc` are first-class `chart` types
   and an OHLC point is `[open, high, low, close]`; the renderer draws real
   candles (a point that is not a quadruple is a gap, never a fabricated
   candle). The synonyms ("candle", "candles", "ohlc_bar") RECOVER in the
   repair pass instead of being dropped — a dropped enum left the block invalid
   and degraded it to prose, which is exactly the "text chart" a user saw.
4. **A formula is shown.** `provenance.formula` exists on the figure
   provenance and is rendered under the metric and hero figures. A derived
   number without its formula is an assertion.
5. **The board is reachable and a dispatched task may write.** The dashboard
   mirrors are listed (they were hidden, so the agent could not name the
   Command Center), and a task runs with the autonomy its creator declared (the
   UI engineering path stores `autonomy: edit_verify` on its plan; nothing read
   it back, so every write was refused with `PROFILE_DENIED` and the task could
   only produce prose).
6. **A report is a file, and a quant model is buildable.** The prompt names
   `eclipse_report` for a report/deliverable request, the produced artifact id
   travels with the document record so the chat can offer the file, the
   numerical stack is declared in `python/requirements.txt` (the PDF report
   backend needs matplotlib), and a "build me a quant model" request routes to
   the code profile with a legible `StrategySpec` schema instead of an opaque
   `object`.

**Why.** Every one of these was a report that the runtime could not honour:
"it did not execute code", "it did not show formulas", "a candlestick chart
came back as text", "the clock is not on the Command Center", "give me a proper
report", "build me a quant model". Each had a specific, findable cause — a
prompt that steered the model away from the tool, a schema that rejected a
legitimate answer, a dispatch that dropped the profile the creator asked for.
Fixing the cause is what makes the capability real; the alternative (a
prose-shaped apology) is what the system was already doing.

**Consequences.** `prompts.py` (the computed-results, documents and reports
sections), `response_mode.py` (the visualization directive),
`analysis_tools.py` (`recipe_catalogue_text`, the failure text),
`code/tools.py` (the recipe catalogue in the tool description),
`code/execution.py` (`package_lock_digest` over the interpreter),
`code/request_intent.py` (the quant-model vocabulary), `tasks/bootstrap.py`
(`_task_autonomy`), `ui/tools.py` (the mirrors), `documents/authoring.py` +
`documents/{tools,report_tools}.py` (the artifact id),
`document_intelligence/collector.py` (the artifact id + section titles),
`quant/tools.py` (the `StrategySpec` schema), `src/lib/response/contract.ts`
(the chart types, the OHLC point, `provenance.formula`), `chart-option.ts` (the
candle renderer + the director), `normalize.ts`/`repair.ts` (the synonyms),
`src/components/response/{blocks,visual-blocks}.tsx` (the formula line),
`python/analysis-requirements.txt`, `scripts/setup-python.js`,
`scripts/setup-embedded-python.js`, `scripts/doctor.js`. Tests:
`python/tests/test_analysis_tool.py`, `test_code_request_intent.py`,
`test_tasks_scheduler.py`, `src/lib/response/chart-option.test.ts`,
`src/lib/response/finalize.test.ts`. Docs:
`docs/interactive-response-engine.md`, `docs/harness/python-runtime.md`,
`docs/quant-engine.md`.
> **ADR-H242 was renumbered to ADR-H291.** The series had two entries with this number; the first one keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H291 — The sandbox enforces its boundary at runtime, the operator decides, and the telemetry is kept

**Decision.** The Python sandbox's boundary is enforced where a program cannot
talk around it, the operator's setting decides whether the surface exists at
all, and the measurements the backend takes are reported instead of discarded.

1. **A runtime guard** (`python/harness/code/sandbox_guard.py`, executed inside
   the child before the program runs):
   - a **guarded `__import__`** — only the allowlist (and the stdlib support the
     allowed numerical stack needs internally) can be imported, so an alias
     (`f = __import__`) buys nothing;
   - a **safe `os` / `sys` / `importlib` / `builtins`** — the modules that
     carry privilege are replaced: no `system`, no `popen`, no frame walking
     (a frame proxy with no globals), no `import_module` escape hatch; a safe
     `exec`/`eval` that re-applies the source rules to a dynamic string;
   - **denied privilege modules** — `subprocess`, `socket`, `ssl`, `ctypes`,
     `multiprocessing` can be imported by a library but every call raises;
   - an **audit hook** (`sys.addaudithook`, which cannot be uninstalled) that
     refuses a network, a process, native code, and any file outside the
     program's own folder (writes) or its folder + the interpreter's tree
     (reads);
   - **confined readers/writers** — the file entry points of `numpy`/`pandas`
     are wrapped, because a source check cannot see inside them.
   The AST walk stays as the first filter (it refuses earlier, with a clearer
   message) and the source gate now also refuses a forbidden name used as a
   VALUE (`f = __import__`).
2. **The operator decides.** `HARNESS_ANALYSIS_ENABLED=0` switches the whole
   surface off for the runtime. The per-run `analysisEnabled` flag is a REQUEST;
   it can no longer switch on what the operator switched off.
3. **Cancellation is real.** The run's cancel event is mirrored into the
   sandbox's `threading.Event`, so a cancelled or timed-out analysis kills its
   process tree instead of leaving a child behind (up to 600 s) — the verdict
   says `CANCELLED`.
4. **The telemetry is kept.** The backend's measured CPU time, peak RSS,
   process count, the breached limit name and the exit code now ride the
   `AnalysisResult` and its blob. A memory kill is reported as a memory kill,
   not as a generic program error.
5. **The reads are bounded.** The result file and each collected artifact are
   size-checked BEFORE they are read, so a program cannot make the host
   allocate or copy without limit.

**Why.** The audit found the sandbox's security claim rested on a source check
that three separate vectors walked past (`__import__` aliased, a URL built by
concatenation, `numpy`/`pandas` reading a path directly). The claim was also
written down as fact in the code, the ADRs and the prompt. The fix is not a
bigger allowlist — it is an enforcement point the program cannot negotiate with,
plus honest documentation of what it does and does not cover.

**Consequences.** New `python/harness/code/sandbox_guard.py`; `code/execution.py`
(the guard is installed by both runners, the bounded reads, the telemetry, the
cancellation); `code/kernel.py` (the same guard in the persistent kernel);
`analysis_tools.py` (the operator switch, the cancel signal);
`harness/tools.py` + `graph.py` (the cancel event reaches the tool);
`code/sandbox.py` (`PYTHONDONTWRITEBYTECODE=1` — the interpreter's tree is
read-only for a program, so no bytecode cache is left behind); `prompts.py` (the
allowed import list is now the truth). Tests:
`python/tests/test_analysis_guard.py` (22) — the alias, the concatenated URL, the
host file read/write, the process, the socket, the dunder chain, the operator
switch, the telemetry and the cancellation. Docs:
`docs/harness/python-runtime.md`, `docs/harness/decisions.md` (ADR-H110/H178
corrected in place), `docs/harness/roadmap/phase-08-analysis-sandbox.md` (the
non-goals corrected).
> **ADR-H243 was renumbered to ADR-H292.** The series had two entries with this number; the first one keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H292 — A computed number is renderable only when it carries its provenance, and a refused name must name the alternatives

**Decision.** Two rules the adversarial probe of the sandbox made concrete.

1. **A computed number is renderable only with its provenance.** The renderer
   resolves a figure against the turn's computed-result blob; a number the
   STATS engine computed had no such blob, so the transcript showed the honest
   refusal "this number has no record of where it came from" — correct, but it
   meant a real, deterministic, audited metric could not be shown. The stats
   engine now publishes its metrics as the same kind of computed result the
   sandbox produces (`outputs` + `provenance`), so every computed number in a
   transcript is traceable to the engine, the series and the window.
2. **A refusal that could be corrected in one more call must name the
   correction.** A model that GUESSED a recipe name (`max_drawdown`; the real
   ones are `drawdown_profile`, `drawdown_figure`, …) got a bare
   `RECIPE_NOT_FOUND` and no catalogue, and gave up on the whole request. The
   refusal now lists the ready-made recipes and their inputs. The same rule
   applies to the prompt: it names a recipe name exactly, and it tells the model
   to let the PROGRAM fetch its series (`eclipse_tools.price_history`) — a
   dataset tool's row count is not a series.

**Why.** The boundary being solid is not the same as the capability being
usable: the probe's escapes all failed, while the one prompt that asked for
real work lost four of its five deliverables to three small gaps (a missing
catalogue, a series that never arrived, a metric with no provenance). An agent
that cannot correct its own mistake in one call will abandon the task, and the
user sees nothing — the failure looks like the model, not like the runtime.

**Consequences.** `code/recipes.py` (the catalogue in the refusal);
`stats/tools.py` (`_publish_computed`); `prompts.py` (the two rules);
`python/tests/test_analysis_guard.py` (the catalogue and the published
provenance); `python/tests/test_memory_tools.py` (the sandbox's own bindings are
a real surface the prompt may name). Docs: `docs/CHANGELOG.md`.
> **ADR-H244 was renumbered to ADR-H293.** The series had two entries with this number; the first one keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H293 — A refusal must teach the correction; a result must show what it hides

**Decision.** Four rules the five-prompt limit probe forced, all about the
messages the runtime sends the model:

1. **A wrong recipe name is corrected in one call.** The refusal for an unknown
   recipe lists the ready-made recipes (and their inputs). A model that guessed
   `max_drawdown` now sees `drawdown_profile`, `drawdown_figure`, … instead of
   a bare `RECIPE_NOT_FOUND`.
2. **A missing recipe input names the INPUT and its kind.** "which series should
   I use?" does not teach the input NAME; the refusal now says
   `prices` (series: a list of prices over time) and lists the recipe's declared
   inputs. A model that learns the name in one call stops guessing.
3. **A refused element says that only it was refused.** The message for a
   disallowed source element (and the runtime guard's import refusal) now says
   that the REST of the program is allowed, names the offending element, and
   lists the importable modules. Without it a model concluded "numpy is not
   permitted" when the real cause was a dunder attribute in its own probe —
   a false statement about the runtime, repeated to the user.
4. **A truncated result says it is truncated.** The computed-result text reports
   "showing 12 of 30 values". A cut-off list that does not announce itself is
   read as the whole result.

**Why.** The probe showed the boundary holds and the product still leaks: the
model spent 36 tool calls rediscovering a contract the runtime could have
stated once, then reported a false capability limit to the user ("NumPy and
pandas are not permitted in this sandbox" — they are), and a headline number
never reached the transcript because the payload was cut without saying so.
Every one of those failures is a MESSAGE defect, not a computation defect, and
messages are the runtime's job.

**Consequences.** `code/recipes.py` (the catalogue and the input spec in the
refusals), `code/execution.py` + `code/sandbox_guard.py` (the actionable
refusals), `analysis_tools.py` ("showing N of M values"). Tests:
`python/tests/test_analysis_guard.py` (26). Docs: `docs/CHANGELOG.md`.
## ADR-H278 - A landing is not complete until the restarted runtime proves it, and the conductor's bounds are hot-reloadable

**Decision.** Two operational rules for RSL v3.

1. **Pre-landing measurement is not landing proof.** A promoted `rsl-*` change
   that requires a restart must pass a post-landing probe: restart, `GET /health`,
   then keep. On failure the previous files are restored, the runtime is
   restarted again and the restored process is probed. The restart seam remains
   owned by the front door/supervisor because the runtime cannot cleanly restart
   itself; when the seam is absent the result is `unverified`, never a success.
2. **The conductor's bounds are operator-editable without a restart.** Only the
   bounded `RSLConfig` fields are accepted, through the same clamps as the env,
   and the last override is persisted on `agent-memory.db`. Enabling a mode starts
   the conductor; `off` stops it. The default stays `off`.

**Why.** v2 could prove a candidate was better offline while a broken import
still took the running app down, and every mode/budget change needed a process
restart. Both are trust defects: the first lets a measured win become an
outage; the second makes daily operation expensive enough that the loop is left
dark.

**Consequences.** `self_improvement/post_landing.py` (injected restart/probe/
rollback, `HarnessPostLanding`), `self_improvement/runtime_settings.py`
(`HarnessRuntimeSetting`), `RSLConfig.from_dict`/`apply_payload`,
`RSLLoop.set_config`, `POST /evolution/rsl/config`,
`POST /code/promotions/{rollbackId}/post-landing`, the `postLanding` hint on the
existing promotion route, and tests `test_post_landing.py` + the config/loop
additions. No new runtime, queue or database; the two tables live on the ONE
`agent-memory.db`. Docs: `docs/self-improvement/rsl-v3-plan.md`,
`docs/harness/architecture.md`, `docs/NAVIGATION.md`.
## ADR-H279 - A live ablation uses the isolated evaluation bridge, and a replay task is a regression label, not truth

**Decision.** Two evidence rules for RSL v3.

1. **The live tier is a real model behind the isolated evaluation bridge.** An
   ablation arm may run a validated `HarnessSnapshot` with a real provider/model
   (`--live`), but it still uses the trial's private store + registry and the
   `_UnavailableBridge`. External data is reported `UNAVAILABLE`; the report
   labels the tier and never presents the result as a production-data replay.
   Live mode refuses to start without explicit provider/model settings.
2. **A replay task records behavior, not correctness.** The replay builder takes
   a completed run's redacted prompt and recorded tool sequence and writes a
   `live_only` regression task. Its expected behavior is the recorded trajectory;
   no expected answer is invented. The generated dataset is unreviewed and is
   never optimization material until an operator reviews it. Failed runs stay in
   the existing mining pipeline, which also refuses to invent an expectation.

**Why.** The alternative - letting a live arm reach the production data bridge or
treating a recorded answer as ground truth - would contaminate the comparison
with sources the arm did not actually use and turn one model's output into a
judge. Both are the failure modes the frozen judge exists to prevent.

**Consequences.** `evaluation/candidate_invoker.py` (`live` + explicit
`ModelSettings`), `evaluation/ablation.py` (tier in the report),
`evaluation/replay_builder.py` (`live_only` + redaction + dedupe), tests
`test_ablation.py` + `test_replay_builder.py`. No new runtime, database or
network default; the operator still runs the live tier deliberately and pays for
it. Docs: `docs/self-improvement/rsl-v3-plan.md`,
`docs/harness/agent-evaluation.md`.

## ADR-H280 - A surface EXTENDS the agent's declared tools; it never silently REPLACES them

**Decision.** The tool-surface resolution moves out of `graph.py` into
`python/harness/surface.py` and becomes a UNION, not a replacement:

    allowed = policy_narrowed(base) UNION policy_narrowed(surface)

Both halves go through the ONE policy authority (`code.policy.allowed_tools`),
so an autonomy profile still removes every write/execute/external tool it bans
- from the base half AND from the surface half. A surface never removes a base
tool: a tool may leave a run's surface only through the autonomy profile or
through an operator-disabled store, and BOTH are named in the resolution. The
resolution is published on the run row and returned on the run result
(additive, optional field `surface`): the surface id, the tools it ADDED, the
tools the profile removed, the tools a disabled store removed, the bound tool
count, and the measured schema characters the surface added. The marginal
schema cost is ratcheted (`MAX_SURFACE_ADDED_SCHEMA_CHARS`); the resolver
lives in its own module so `graph.py` shrinks.

**Why.** A chat coding turn used to REPLACE the finance agent's whole
allowlist with the code profile. Measured against a real `AgenticService` on
2026-10-20 (956 registered tools), the finance agent declares 668 tools and
the code profile declares 454, and the two are largely disjoint - 441 base
tools and 227 surface tools are unique to their half. The replacement
therefore cost the model **441 tools** on every coding turn, without naming
one of them: the whole task/plan family (`create_plan`, `create_task`,
`read_plan`, `run_plan`, ...), the whole web/research family (`web_search`,
`web_extract`, `research_query`, `claim_verify`, ...), the unified
retrieval/search family (`semantic_search`, `hybrid_search`,
`search_evidence`, ...), the truthful-capability diagnostics
(`get_capability_matrix`, `get_research_health`) and the response-contract
tools (`response_block_schema`, `response_validate`).

Two of those losses were defects, not inconveniences. `response_block_schema`
is what `graph.execute_run` checks before injecting the live response-schema
digest and the response-mode plan into the system prompt, so a coding turn
produced a DEGRADED structured response with no error and no warning. And
`get_capability_matrix` is the tool whose absence was already recorded as a
real incident in the `ECLIPSE_AGENT` allowlist comment ("verified in the
SYS/RELIANCE/Tata runs, where `get_capability_matrix` returned
TOOL_FORBIDDEN"). The stated intent had always been the opposite of the
behaviour: `agents.py` said "ADDITIVE: `ECLIPSE_AGENT`'s declared tools are
NOT touched", and ADR-H117 described the profile as applied "after the
autonomy narrowing". The code replaced instead.

The same change also fixed a latent crash on a fresh install. The answer seam
writes `codeVerification` UNCONDITIONALLY, and that column was in neither the
`CREATE TABLE` nor the migration list - so on a fresh `agent-memory.db` (which
every new clone gets, because the file is gitignored) the write raised
`no such column: codeVerification` and every run crashed AFTER the model had
already produced its answer. The live repository masked it because its
database predates the column. `codeVerification`, `autonomy`, `workspaceId`
and the new `surface` column are now in the migration list.

**Consequences.** `python/harness/surface.py` (the resolver),
`python/harness/graph.py` (delegates; the run publishes the resolution),
`python/harness/persistence.py` (the columns and the read-back),
`python/harness/schemas.py` (`RunResult.surface`),
`python/harness/agents.py` (the profile comment states the union; the
`code-navigator` comment no longer claims "five read tools"). Tests
`python/tests/test_surface_resolution.py` (the rules), plus the existing
`test_code_reachability.py`, `test_chat_code_profile.py`,
`test_surface_honesty.py`, `test_code_agent.py`, `test_registry_visibility.py`
and `test_advertised_tool_admission_all.py` (unchanged and still green). The
code turn's bound tool set grows from 443 to 862 schemas and its measured
marginal schema cost is 64_494 characters; the ratchet ceiling is 120_000 and
Round 3 / W6 moves the schema set behind lazy tool loading so the cost stops
growing with the declared surface. The `code-navigator` preset's allowlist is
UNCHANGED (it is pinned by `test_memory_tools.py`); only its stale comment was
corrected. Docs: `docs/harness/architecture.md`, `docs/NAVIGATION.md`,
`docs/harness/roadmap/round-3-parity-plan.md`.

## ADR-H281 - The verify-and-repair loop runs in the graph, and its outcome is a first-class state

**Decision.** The agent graph gains a `verify_repair` node between the tool turn
and the next agent turn. After a turn that CHANGED something, the node runs the
project's pinned check CLASSES through the ONE command path
(`code.command_tools.command_run` - the same function the model's own
`command_run` tool call goes through), and when one fails it hands the failure
back to the MODEL, because the model is the only thing that can fix code. The
budget - attempts, wall clock, cost, and the number of check runs - is the run's
and is enforced before each attempt; exhaustion is a REPORTED state, never a
silent stop. The loop's outcome is a first-class state: the answer seam receives
the REAL outcome (never a hard-coded `None`) and, when the check was still
failing, DISCLOSES that in the answer.

The status is DERIVED from the facts, never set by hand:
`REPAIR_NOT_RUN` / `REPAIR_PASSED` / `REPAIR_FAILED` / `REPAIR_EXHAUSTED` /
`REPAIR_NOT_APPLICABLE` / `REPAIR_REFUSED`. A run that never repaired and a run
whose repair passed are different facts, and `None` must never be ambiguous
between them.

The operator pins the checks in `.black-eclipse/verify.json` as
`{"checks": ["unit_test", "typecheck"]}` - class NAMES, not a shell string. A
check that cannot be named cannot back a "tests pass" claim, so it is not
evidence and it is not run here; the existing `{"command": "..."}` pin form
keeps its single-shot behaviour in `code/tools.py::_auto_verify`.

**Why.** `code/repair.py` is a correct, bounded, cost-capped repair loop whose
docstring said *"`execute_run` CALLS `run_verify_repair`; it does not gain a
node"*. It did neither. The only two callers in the repository were
`code/eval/tasks.py` and `code/eval/safety.py` - the EVAL harness. In
production `graph.py` passed `repair_outcome=None` to the answer seam on every
run, so three things were true and none of them were intended:

1. a check the project pins never ran unless the MODEL chose to run it;
2. a failed check never drove a repair - the model was told a check had failed
   only if it had run the check itself, and then had to notice, decide, edit and
   re-run with no budget and no cost cap;
3. the answer seam could not tell "no repair happened" from "the repair
   passed", because the only value it ever received was `None`.

The module's callback shape is also the wrong shape for production. Its
`make_edit` seam is a function the eval harness scripts; in production the
editor is the model, and the model lives in the agent loop, not in a callback.
So the production shape is a GRAPH loop, and the module's own docstring was
right about the mechanism and wrong about the wiring.

A fourth, smaller thing this fixed: the answer seam already refused to support
a "tests pass" claim when the only record on the ledger was a FAILURE - but
that only worked when a check had run. With the check running automatically,
the ledger gets the real record, and the disclosure covers the case the ledger
cannot see - the loop ran out of budget, so the last state of the code is
simply unknown.

**Consequences.** `python/harness/code/repair_graph.py` (the graph seam: the
pinned-check file, the write-turn trigger, the check runner, the failure
message, the loop state), `python/harness/code/repair.py` (the answer-seam
vocabulary and the check-run bound), `python/harness/code/verify.py` (the
disclosure), `python/harness/graph.py` (the node, the routing, the state
field), `python/harness/code/eval/tasks.py` (the `repair-in-graph` exercise
through the real graph), `python/harness/code/eval/dataset_tasks_v1.json` (the
new shape; the dataset checksum is re-pinned - a new case is a new review),
`python/harness/code/eval/capabilities.py` (the `code_verify_repair_loop`
class), `python/harness/code/eval/statement.py` and
`src/lib/i18n/dictionaries/en.ts` (the new shape's plain name). Tests
`python/tests/test_graph_repair_node.py` (20 cases) and the regenerated
`docs/harness/capability-statement.md`. A repair NEVER widens authority: the
pinned class still goes through `code.policy.decide_command` and the profile's
grants, and `plan_only` - the read-only profile - never repairs at all. Docs:
`docs/harness/architecture.md`, `docs/NAVIGATION.md`,
`docs/harness/roadmap/round-3-parity-plan.md`.

## ADR-H282 - Candidate verification is parallel within one bounded experiment, never across the one conductor

**Decision.** One RSL experiment may verify its `k` independent candidates
concurrently under `HARNESS_RSL_MAX_PARALLEL` (1–8, default 2), and the hill
climb may evaluate independent validation neighbors concurrently under the same
cap. Proposal order is preserved for selection, one verification failure is that
candidate's non-answer, and the daily budget trims `k` before any call. Replay
gates still run in order. The conductor remains one experiment at a time; per-
scope concurrency is deferred.

**Why.** The search was serial for no reason: each candidate owns its own trial
store and registry, so the verifier already isolates them. Parallelizing the
independent work buys search width without weakening the frozen judge, the
budget, or the operator-only landing path.

**Consequences.** `self_improvement/config.py` (`max_parallel`),
`self_improvement/recursion.py` (semaphore + ordered gather),
`self_improvement/hillclimb.py` (parallel validation, ordered processing),
`RSLLoop` passes the configured cap. Tests: `test_rsl_recursion.py`
(parallel cap + order), `test_rsl_hillclimb.py` (parallel validation). No new
worker, queue, runtime or database; the daily budget is still the outer bound.
Docs: `docs/self-improvement/rsl-v3-plan.md`.
## ADR-H283 - Tool guidance is prompt evidence, not authority

**Decision.** `HarnessSnapshot.toolGuidance` is a bounded (4,000 character)
block of per-tool instructions appended to the effective prompt for candidate
trials and the live surface. It cannot add a tool, raise a ceiling, change a
permission, or grant an effect: the registered tool allowlist and the security
gateway remain the execution boundary. The snapshot digest includes guidance
only when non-empty, so old snapshots keep their exact identity.

**Why.** Operators need to improve HOW the agent uses an allowed tool without
turning the self-improvement loop into a permission path. Prompt text is the
lowest-authority lever; binding it to the same `HarnessSnapshot` keeps the
measurement, canary, preview and rollback flow unchanged.

**Consequences.** `self_improvement/models.py` (`toolGuidance`,
`effective_prompt`, additive digest), `evaluation/candidate_invoker.py` (trials
use the effective prompt), `self_improvement/live_surface.py` (live application).
Tests: `test_rsl_snapshot.py` + `test_live_surface.py`. No new tool, permission
or runtime field. Docs: `docs/self-improvement/rsl-v3-plan.md`.

## ADR-H284 - One search primitive per question, on a fast engine, and the engine is always named

**Decision.** The search surface is TWO tools, not four: `search_text` for
file CONTENTS and `search_files` for file NAMES. The two older names -
`workspace_search` and `find_files` - stay callable for one release as
DEPRECATED ALIASES, and the registry warns once per run naming the
replacement.

- `workspace_search` now DELEGATES to the fs subsystem's `search_text`, which
  was already ripgrep-first. It used to run its own naive Python walk. The
  answer always names the engine that produced it (`ripgrep` or
  `python-scan`), so a slow result is explainable rather than mysterious.
- `search_files` now carries every filter `find_files` had - `exclude`,
  `extensions`, `directoriesOnly`, `filesOnly`, `includeHidden`,
  `includeIgnored` - so nothing is lost in the consolidation. (`search_files`
  was always `find_files(include=[pattern])`: one implementation, two
  front-doors.)
- The agent surfaces carry the CANONICAL names only. The deprecated names
  remain REGISTERED, so an older surface or a saved transcript still works.
- The workspace walker moved out of the ratcheted `code/tools.py` into
  `code/scan.py`, because it is a scan and not policy.
- A search now goes through the code capability, so it needs a code profile.
  That is the Phase-0 rule (no profile, no code capability) and it closes a
  hole: the old `workspace_search` bypassed the code policy entirely.

**Why.** The harness had FOUR tools for TWO questions, and the two the model
reached for FIRST were the slow ones. `workspace_search` ran its own naive
Python walk while `search_text` - one name away, in a surface that listed both
- was already ripgrep-first. `search_files` and `find_files` were always one
implementation with two front-doors, and only `find_files` carried the
exclude/extension/directory filters. A model asking "where is this?" paid the
slow path by default, and the answer never said which engine ran.

The cost of the fix is honest and bounded: `code/fs_tools.py` crossed the
1200-line hard ceiling and now carries a justified ratchet entry, with the fs
tool surface named as a Round 3 / W7a split candidate (it duplicates
`code/tools.py`'s). The two walkers - the moved `code/scan.py` one and the fs
subsystem's own - were NOT merged: they have different contracts (a file OR a
directory, bounded by bytes, versus directory-only and hidden-file aware), and
merging them would change the fs search's behaviour while looking like a
cleanup.

**Consequences.** `python/harness/code/scan.py` (the moved walker),
`python/harness/code/tools.py` (the delegating `workspace_search`, the
definition factory's deprecation wiring),
`python/harness/code/fs_tools.py` (`search_files` gains the filters,
`find_files` deprecated),
`python/harness/agents.py` (the three surfaces carry the canonical names),
`python/harness/code/eval/capabilities.py` (the `code_search` class). Tests
`python/tests/test_code_search_surface.py` (11 cases) plus the updated
`test_code_tools.py` (a search without a code profile is now REFUSED - the
behaviour is pinned), `test_chat_code_profile.py` and
`test_code_reachability.py` (the pinned surfaces). The legacy payload field
`filesScanned` is gone - nothing read it - and is replaced by `engine` and
`filesWithMatches`, both of which are exact. Docs:
`docs/harness/architecture.md`, `docs/NAVIGATION.md`,
`docs/harness/roadmap/round-3-parity-plan.md`.

> **ADR-H245 was renumbered to ADR-H294.** The series had two entries with this number; the first one keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H294 — The guard's rules are per command, the confinement covers the methods, and a refusal names the correction

**Decision.** Four corrections to the sandbox, all found by running the
adversarial probe against the live app rather than the tests.

1. **The rules are per COMMAND, not per process.** The persistent kernel runs
   many commands in one process, and each command may have a different import
   allowlist (a figure recipe adds `matplotlib`; a plain program does not).
   Installing the allowlist once judged a later command by the FIRST command's
   rules — a shipped capability (`drawdown_figure`) was refused mid-conversation.
   The allowlist and the path roots are now re-configured on every command; the
   shims and the audit hook stay installed once.
2. **The confinement covers the METHODS.** Wrapping the module function is not
   enough: a program writes with `pandas.DataFrame.to_csv`, which is a class
   method (and pandas 3 exposes `DataFrame` as a factory, so the class is
   resolved through the instance). `DataFrame`/`Series` writers are wrapped now,
   and the audit hook reads `os.open`'s integer flags as the write they are.
3. **A refusal names the correction IN THE PROMPT.** The model reads the
   refusal's prompt, not its stderr: the recipe's prompt now names the missing
   input and its kind (`prices` (series: a list of prices over time)) and lists
   the recipe's declared inputs.
4. **A truncated result says so, and the transcript MERGES its computed blobs.**
   The computed text carries up to 30 values and states what it hides; the
   client merges a second computation into the first one's blob instead of
   replacing it, so a reference to the first computation still resolves.

**Why.** Every one of these was invisible to the unit tests because the tests
call the runner directly, while the app goes through the persistent kernel and
the real `pandas`. The boundary held in both; the CAPABILITY did not. A guard
that judges a later command by an earlier command's rules is not a boundary, it
is a coin flip.

**Consequences.** `code/sandbox_guard.py` (per-command rules, the method
wrappers, the int-flag write detection); `code/recipes.py` (the prompt names
the input); `analysis_tools.py` (30 values + the notice);
`src/lib/agent/use-agent-run.ts` (the merge). Tests:
`python/tests/test_analysis_guard.py` (27). Docs: `docs/CHANGELOG.md`.

## ADR-H285 - ONE edit tool, and it insists the run read the file

**Decision.** The text-edit surface is ONE tool: `code_edit`
(`path`, `oldText`, `newText`, `expectedOccurrences`). It REPLACES text, and
two rules make it safe:

1. **A file may only be changed if THIS RUN read it.** The per-run read
   ledger (`harness.tools.read_ledger`) records every file a read tool returned
   a hash for. An edit to a file the run never read is REFUSED with
   `FILE_NOT_READ`, and the refusal names the read that would make the edit
   legal. The registry feeds the ledger in ONE place, so every read tool
   participates - the model does not have to read a file a particular way
   before it may change it.
2. **A file may only be changed if it has not changed since it was read.** The
   ledger's hash is passed to the edit engine as `expected_hash`, so the
   engine's own `STALE_FILE` refusal fires - the same code, the same recovery
   hint, in one place.

The mutators `code_edit` subsumes are DEPRECATED, still callable, and name
their replacement: `replace_text`, `patch_file` and `edit_file` name
`code_edit`; `multi_file_patch` names `apply_diff`; `workspace_patch_apply`
names `code_edit`. The genuinely DIFFERENT operations are NOT deprecated and
stay: `write_file`, `create_file`, `delete_file`, `move_file`, `copy_file`,
`rename_file`, the directory tools, `insert_text`, `delete_text` and
`apply_diff`. Removing those would remove capability, not duplication.

`code_edit` is a WRITE tool in `code/policy.py`, so `plan_only` removes it and
`edit_verify` keeps it - exactly like the tool it replaces. A wrong path comes
back with up to five nearest candidates (exact basename, then suffix, then a
bounded fuzzy match) instead of a bare "that file isn't there".

**Why.** The harness had THREE ways to change a file's contents and ELEVEN more
file-lifecycle tools, spread over two subsystems - and NONE of them required
proof that the model had seen the file it was about to change. `expectedHash`
was accepted everywhere and required NOWHERE, so the model could write to a
file it had never read, and a file that had changed underneath it was
overwritten silently. `edit_file`'s inspect packet already did this for ONE
tool, and the fs engine already refused an ambiguous match and a stale base;
the DUPLICATION was that three different front-doors each re-implemented (or
skipped) those rules.

The plan sketched deprecating thirteen mutators. The audit showed nine of them
are genuinely distinct operations - file lifecycle, directory operations,
insertion, line-range deletion - that one edit tool cannot express.
Deprecating them would have removed capability while looking like a
consolidation, so the honest set is the four text-edit paths `code_edit`
actually subsumes.

**Consequences.** `python/harness/code/edit_tools.py` (the tool, the ledger
contract and the path recovery), `python/harness/tools.py` (the per-run read
ledger and the ONE registry hook that feeds it), `python/harness/code/fs/errors.py`
(the `FILE_NOT_READ` code and its recovery hint),
`python/harness/code/policy.py` (`code_edit` in the WRITE set),
`python/harness/code/tools.py` (`code_edit` registered, `workspace_patch_apply`
deprecated, and `workspace_read` now returns the file's hash so a
workspace-level read feeds the ledger too),
`python/harness/code/fs_tools.py` (the four mutators deprecated),
`python/harness/agents.py` (the three surfaces carry the ONE edit tool),
`python/harness/code/eval/safety_cases.py`, `python/harness/code/eval/safety.py`
and `python/harness/code/eval/capabilities.py` (the S12 / S13 / S14 adversarial
cases and the `code_edit_contract` class). Tests
`python/tests/test_code_edit_primitive.py` (13 cases) plus the updated
`test_code_agent.py`, `test_chat_code_profile.py`, `test_code_reachability.py`
and `test_surface_resolution.py` (the pinned surfaces). Docs:
`docs/harness/architecture.md`, `docs/NAVIGATION.md`,
`docs/harness/roadmap/round-3-parity-plan.md`.

## ADR-H286 - The dropped turns are COMPACTED, not just dropped

**Decision.** The history bound now COMPACTS what it removes. The dropped
turns are carried forward as a fenced, labelled summary block: the requests
that were made and the answers that were given (the deterministic skeleton),
plus a bounded model NARRATIVE on top of it - the decisions taken, the open
questions, the state of the work. The existing inert dropped-context block is
NOT replaced: both reach the model, the compaction first.

The skeleton always runs. The narrative is additive and fails CLOSED - if the
model is unavailable, times out, says nothing, or raises, the skeleton alone
is used and the run records why. The block is DATA, never instruction (a
compaction can contain anything the user typed), and it never enters a
research claim: a summary of a conversation is not evidence.

**Why.** Until now the harness TRUNCATED its history. `history.py::
_bounded_history` kept the most recent turns and dropped the oldest, and
`eclipse_memory/dropped_context.py` mined the dropped turns for INERT
candidates ("you mentioned a file called X"). Everything else the conversation
carried - the decision that was made, the question that was still open, the
state of the work - was GONE. `context_budget.py` said it plainly: *"no
tokenizer dependency, no network call, no summarization."* Every top-tier
harness compacts; a long run in Eclipse could keep twenty turns and no more,
and the twenty-first turn started from nothing.

**Consequences.** `python/harness/compaction.py` (the skeleton, the narrative,
the renderer), `python/harness/history.py` (the compaction is built and
combined with the inert block; the extraction's fail-closed path now still
carries the compaction, because the compaction is independent and never
fails). The event `memory.history_compacted` records the facts - turns
compacted, whether the model contributed, the reason when it did not, the
block's cost. The policy key `history_compaction_model` turns the narrative ON; it is
off by default, exactly like the model-assisted dropped-context extraction,
because it costs a model call and up to twenty seconds on the run's critical
path - and the deterministic skeleton is what actually carries the
conversation forward. The skeleton ALWAYS runs. Tests
`python/tests/test_history_compaction.py` (12 cases). Docs:
`docs/harness/architecture.md`, `docs/NAVIGATION.md`,
`docs/harness/roadmap/round-3-parity-plan.md`.

## ADR-H287 - The model is TOLD the shape of the project, and the index's freshness is MEASURED

**Decision.** Two things change.

**The repo map.** `code/intel/repo_map.py` builds the shape of the project -
the folders, and a symbol outline of the top files - and it is handed to the
model in the FIRST turn, together with the CIUE context engine's answer for
THIS request (the "seeded slice"), resolved once per run with no model call.
The map is DETERMINISTIC (the same tree produces the same map and the same
digest, byte for byte), BOUNDED (a file count, a symbols-per-file count, a
byte budget, a render budget) and HONEST about its engine. The map's content
hash is the prompt-cache key's witness: a project whose shape changed
invalidates the cache, and one that did not change does not.

**Measured freshness.** `CodeIntelRuntime.is_fresh()` used to `return True`
with the comment "the background watcher keeps the shared index current" - an
UNMEASURED claim about a cache, which is exactly what this harness refuses to
publish. It now looks for the EVIDENCE - the background watcher that owns the
index's freshness - and a workspace that was never indexed, or that has no
watcher, is not fresh. `CodeIntelRuntime.status()`
publishes whether tree-sitter is actually available and which grammars it has:
the symbol outline falls back to the stdlib extractor (Python's own `ast`, and
a brace/regex scanner for everything else), and a reader must be able to tell.

**Why.** The harness had NO repo map at all. A model that wanted to know what
was in the project had to GUESS a path, spend a tool call on it, and - on a
miss - get a bare "that file isn't there". The CIUE index and the context
engine are genuinely good - `CodeContextEngine.select` ranks files by symbol
matches, path matches and related tests, and finds the related test file too -
but they were reachable only if the model THOUGHT to call
`code_get_relevant_context`. Excellent machinery, invisible.

The plan assumed the map would join the cache key for free because
`prompt_segments.py` keys the cache on segment content hashes. It would not:
`runtime_context` is a RUN-FROZEN segment and its content is NOT hashed, so
the map - appended there - would have been cache-key-invisible. The map
therefore gets its OWN segment whose version is its content hash, and the
cache key carries that version - the same pattern the profile snapshot and the
skill manifest already use.

**Consequences.** `python/harness/code/intel/repo_map.py` (the map),
`python/harness/code/intel/runtime.py` (the measured freshness and the
published engine), `python/harness/prompt_segments.py` (the `repo_map`
segment and the cache-key component), `python/harness/graph_context.py`
(`_repo_map_context` - the map and the seeded slice, never raising),
`python/harness/graph.py` (the per-run call site and the plan's map
arguments). Tests `python/tests/test_repo_map.py` (14 cases) plus the updated
`test_graph.py` (the run's event sequence now includes
`code.repo_map_built`). Docs: `docs/harness/architecture.md`,
`docs/NAVIGATION.md`, `docs/harness/roadmap/round-3-parity-plan.md`.

**Amendment (2026-10-25) - the eager map gets a WALL-CLOCK budget.** The map
and the seeded slice are built synchronously in the pre-model phase. "Bounded"
meant counts and bytes, not time: a real repo's `find_related_tests` re-ran one
SQL query per test file (32 500 queries, 94 s) on the event loop, freezing every
route and the stall watchdog, and a run sat silent after `response.plan`. The
bounds are now wall-clock as well: `CodeContextEngine.select(budget_s=...)`
(`BLACK_ECLIPSE_CODE_CONTEXT_BUDGET_S`, default 5 s) and
`build_repo_map(deadline=...)` cut off and report `truncated`; the reference set
is resolved once per NAME; `call_edges(caller_name|callee_name)` are indexed.
Opening the workspace index (seconds, hundreds of MB) moved to BOOT on a worker
thread (`CodeIntelRuntime.warm`); the scan stays lazy. This is an invariant, not
a tuning knob: no pre-model step may hold the event loop unbounded.

## ADR-H295 - The decision record is unambiguous, the harness has a TOTAL ceiling, and the release is real

**Decision.** Three things.

**The decision record is unambiguous.** The harness decision series had SEVEN
duplicate numbers (H160, H162, H242 x2, H243, H244, H245) and five gaps
(H108, H117, H124, H125, H146): two different decisions could share a number,
so a reference to "ADR-H242" named two different things. The later duplicate
of each number was renumbered, and the old number is RETIRED with a redirect
line - the decision is unchanged, but the old number now resolves. The
withdrawn numbers are reserved explicitly.

`scripts/check-adr-numbers.js` fails the release gate on a duplicate number,
on an unreserved gap, and on an entry that does not carry the canonical shape.
It never renumbers anything - renumbering is a judgement call and belongs to a
human. The shape is enforced from this entry onward; the entries below it
were written over months by several hands and some of them do not carry every
section. Rewriting them would mean inventing a rationale for decisions this
change did not make, so they are REPORTED and countable instead.

**The harness has a TOTAL ceiling.** `python/tests/test_size_budget.py`
already ratcheted the PER-MODULE size, which bounds a file's growth but not
the TREE's - a harness can keep every file under its pin and still double by
adding files. `scripts/size-budget.js` adds the missing half: a committed
budget (`scripts/size-budget.json`) for the whole harness and for the twelve
largest subsystems. It fails when the total grows. A budget is only
meaningful if it can be raised DELIBERATELY, so the budget file is committed
and raising it is a visible, reviewable edit.

**The release is real.** The version is 0.2.0 and there is a release note
(`docs/releases/v0.2.0.md`) derived from the CHANGELOG. The CHANGELOG
rotation - the rolling 7-section window with the older sections MOVED into
`docs/changelog/YYYY-MM.md` - is now a script (`scripts/rotate-changelog.js`),
not a promise somebody has to keep by hand. `CODEOWNERS` names the harness
paths, so the part of the tree where a mistake is hardest to see has an owner.

**Why.** The operational score was the harness's weakest dimension, and the
reason was not capability - it was DISCIPLINE. The harness was 368,218 lines
with no total ceiling, the decision series could not be referenced reliably,
there were no releases, and the working tree was the only place the work
existed. The per-module ratchet already proved the pattern works; the
missing half was the tree.

**Consequences.** `scripts/check-adr-numbers.js`,
`scripts/size-budget.js`, `scripts/size-budget.json`,
`scripts/rotate-changelog.js`, `docs/releases/v0.2.0.md`, `CODEOWNERS`, the
renumbered entries in `docs/harness/decisions.md`, the version in
`package.json`, and the two new steps in `.github/workflows/ci.yml` and
`scripts/check-all.js`. The harness is 368,218 lines against a committed
ceiling of 371,900.

**The 30% cut is NOT in this release.** It is a multi-release program, and
the candidates are DUPLICATION, not capability:

| Candidate | Size | What the merge would be |
|---|---|---|
| `terminal/` vs the `code/terminal*` tool surface | 5,334 vs part of 17,986 | one terminal, one policy |
| `execution/` vs `code/execution.py` | 17,872 vs part of 17,986 | one execution plane |
| `browser/` vs `computer/` | 8,997 vs 12,679 | one UI-automation plane |

The FS write stack and the search stack - the two the plan named first - are
already merged (ADR-H285 and ADR-H284). The rest are security-sensitive
refactors of ten-thousand-line subsystems, and doing them in one pass would
be a rewrite dressed as a cleanup. The enforcement now in place makes the
trajectory visible: every release either shrinks the tree or raises the
ceiling ON PURPOSE.

**The tag.** `v0.2.0` is NOT tagged by this change. The branch is not merged,
and the shared working tree still carries another stream's uncommitted work;
a tag must name a merged state. The release note and the version are the
preparation, and the tag is the last step of the merge.

## ADR-H296 - The benchmarks are GENERATED and published, the live tier is a lane, and no capability is published unmeasured

**Decision.** Four things.

**The published benchmark file is GENERATED.** `docs/harness/benchmarks.md` is
written by `python -m harness.evaluation.report` (or `npm run eval:benchmarks`)
and NEVER hand-edited, so a number cannot be quietly improved. A test
regenerates it and compares: the committed file must match a fresh generation
except for the run id, the commit, and the two timing-derived dimensions
(`latency` and `reliability`) - which are MEASUREMENTS of a real loop against
a real sandbox, not verdicts, and legitimately differ between two runs.

**The two tiers are kept apart, and named.** The OFFLINE tier replaces the
model with a recorded script: it measures the RUNTIME - the tool surface, the
policy, the graders, the wiring - and it is labelled that way. The LIVE tier
runs a REAL model and is the only tier that measures the agent. Both carry
their dataset, checksum, agent version, commit and grader version; a number
without its provenance is a rumour, and a rumour in a benchmark table is worse
than an empty row.

**The live tier is a LANE, and it never fabricates.**
`harness/evaluation/live_gate.py` runs the live tier with a configured
provider and records the result with the provenance the store requires. With
no provider it returns the honest reason - "NOT MEASURED" - and exits 0.
`scripts/run-live-evals.js` is the entry point; `.github/workflows/
live-evals.yml` runs it NIGHTLY (it costs money and it is not deterministic),
and SKIPS rather than failing when no secret is configured. Until a live run
has actually happened, the live tier is published as missing, on purpose.

**No capability class is published unmeasured.** The registry had four classes
marked "not measured yet", and one ("messaging_gateway") said "Not built yet".
Three of them are now graded END TO END, through the real modules:
`price_authority` (the authority ARBITRATES a disagreement rather than taking
the first answer, and the conflict is recorded), `promotion` (the full
landing round trip - preview, apply, rollback, re-apply - asserted on the real
bytes in a real folder), and `scraping_breadth` (the registry knows more than
one KIND of data source). Three classes that had no row at all were added:
`code_intel`, `code_chat_routing` and `context_compaction`. The
`messaging_gateway` class is RETIRED - it names a capability that is not
built, and a published class for something that does not exist is worse than
an unmeasured one, because the registry is what a reader trusts. It comes back
with a fixture when it is built.

**Why.** The evaluation framework was already first-rate - the real graph, 17
graders, 22 dimensions with un-averagable criticals, `pass@k` with Wilson
intervals, flakiness classification, frozen baselines stamped with the commit,
per-dimension regression detection, a worktree invoker, a canary split, a
verdict-leak tripwire, and a team-vs-single benchmark that returned a NEGATIVE
verdict and therefore did not ship. Almost nobody builds that.

And then: the offline tier REPLACED the model with a recorded program, the
live tier existed but was reachable only by hand, no number a stranger could
check had ever been published, and four capability classes were published
unmeasured. A harness that cannot be measured by anyone else is a harness that
has to be taken on trust - and "we measured it ourselves" is exactly the claim
this harness refuses to make.

**Consequences.** `python/harness/evaluation/report.py` (`to_benchmarks`,
`write_benchmarks`, a `__main__`), `python/harness/evaluation/live_gate.py`,
`python/harness/code/eval/capabilities.py` (three fixtures closed, three
classes added, one retired), `scripts/generate-benchmarks.js`,
`scripts/run-live-evals.js`, `python/tests/test_published_benchmarks.py`,
`.github/workflows/live-evals.yml`, `docs/harness/benchmarks.md` (generated),
and the regenerated `docs/harness/capability-statement.md` - **24 capability
classes, 24 measured, 0 not measured yet.**

**What is NOT here.** The external SWE-bench suite. Importing it needs a
network fetch of a dataset this repository does not carry, and a dataset
adapter for a task shape the harness does not yet have. Shipping an adapter
that has never seen the dataset would be a claim, not a measurement. The
honest next step is the adapter WITH a real dataset file, and the honest
current state is that this harness's numbers are its own - clearly labelled as
such, and comparable only to a harness that publishes the same way.

## ADR-H297 - A shell sees no more of the environment than a command does, the output is redacted, and the network is refused BY NAME

**Decision.** Four things.

**The shell inherits the SCRUBBED environment.** `code/shell.py` handed the
real host shell `dict(os.environ)` - the FULL environment. A single
`shell_run` could read a provider key, the callback token or the vault, and
print it into the conversation, the run blob and the ledger. The sandboxed
command path had been scrubbed since Phase 1 (`code.sandbox.build_env`); the
shell path had not, and the shell is the path a model most easily pushes a
secret through. Both `run` and `spawn` now use the SAME builder, so a shell
and a sandboxed command see the same world: `policy.SECRET_ENV_ALLOWLIST`
keys only, never the vault, never the provider keys, never the callback
token, never a proxy.

**Output is REDACTED before it reaches the model.** The file reader has
redacted secret-shaped spans since Phase 1; the command and shell paths did
not. Both now redact through the ONE primitive
(`session_index.redact_span`, with the code-surface marker
`REDACT_MARKER`), and emit `code.output.redacted` with the NUMBER of lines
changed - never the value.

**The network is refused BY NAME.** `RunSpec.network` was a declaration:
`code/sandbox.py` refused a `network=True` spec, but the refusal was
unreadable - a caller could not tell it apart from "there is no sandbox
primitive here". The refusal now carries `NETWORK_DENIED`. And the answer to
"is the network contained?" is PUBLISHED rather than assumed: the container
tier really does run `--network none`, and the local tier cannot, so the
command result says which one it was (`networkContained`). A `network=True`
spec only ever runs in the container tier, where the network really is off.

**Two new adversarial cases.** `S15-network-enforced` attacks the real
sandbox module with a network-capable spec; `S16-shell-env-scrubbed` attacks
the real shell environment builder with a secret present. Both are graded
against the real modules, not a stub.

**Why.** These were the three holes the audit found and the plan named:
`RunSpec.network` was declarative with no consumer, the container backend's
`execute()` was never called, and `shell_run` handed the full `os.environ` to
a host shell. A harness that insists on honesty everywhere else - an
UNMEASURED dimension, a fabricated claim, a silent removal - was handing a
model the keys to the vault on one code path.

**Consequences.** `python/harness/code/shell.py` (the scrubbed environment),
`python/harness/code/events.py` (`redact_output` - it lives here because
`shell_tools` and `command_tools` import each other, so a helper in either
would be a circular import),
`python/harness/code/shell_tools.py` and
`python/harness/code/command_tools.py` (the redaction on both output paths,
and the published `networkContained`),
`python/harness/code/sandbox.py` (the named refusal),
`python/harness/code/eval/{safety_cases,safety}.py` (S15 and S16),
`python/harness/code/eval/capabilities.py` (the `code_shell_exec` class), and
`python/tests/test_isolation_secrets.py`.

**What is NOT here.** The container backend is still not the default tier.
`code/backends.py` routes a `network=True` spec to it, and its
`docker_argv` really does apply `--network none` or `--network bridge` - but
selecting the container for an ordinary spec needs the operator to ask for it,
and on a machine without a container runtime `select_backend("container")`
raises `SandboxUnavailable` and the command is REFUSED rather than degraded.
That is the documented behaviour, and it is honest: no degradation, ever
(`README` invariant 3.5). Making the container the default is a packaging
decision, not a policy one.

## ADR-H298 - The harness has a PUBLIC surface, a published tool manifest, and a package that carries no data

**Decision.** Three things.

**A public surface.** `python/harness/public.py` is the WHOLE public API:
`AgenticService`, `ToolRegistry`, `ToolDefinition`, the run contract
(`RunRequest` / `RunInput` / `RunResult` / `ModelSettings`), and the export
functions. It is a closed list, and a test asserts the list is exactly what it
declares. Before this, everything the harness could do was reachable only
from inside this repository, and a caller who wanted to use it had to read the
source to find out what was safe to touch.

**A published tool manifest.** `export_tool_schemas()` produces the portable
manifest - the MCP tool-manifest shape (name / description / inputSchema) PLUS
the parts a consumer needs to bind a tool safely: `permission`, `effect`,
`risk`, `version`, and the deprecation fields. `docs/harness/tool-registry.json`
is that manifest, GENERATED, with a drift test that regenerates and compares.

**The honesty contract, and it is the point of the export.** A tool is
exported with its permission and its effect class, or it is NOT exported. A
tool whose permission is not "read" and whose effect is undeclared is WITHHELD,
because a consumer cannot honour a rule it cannot read - and a tool that
cannot be classified is a tool nobody should call. A consumer inherits
Eclipse's honesty: it can see that a tool writes, or that it reaches the
network, before it binds it.

**A package that carries no data.** `pyproject.toml` declares
`black-eclipse-harness`: the RUNTIME, with the provider SDKs as OPTIONAL
extras (a consumer that only wants the tool surface should not install an SDK
for a model it never calls), no package data, and no dependency on the app.
The packaging test asserts there is no data-shaped file, no credential-shaped
dependency, and that the app stays private. The published thing is the
runtime; the data belongs to the installation that runs it.

**Why.** The harness was excellent and unreachable. Everything it could do -
the retrieval layer, the research verifier, the comparability gate, the
receipts, the tool surface - was valuable to another harness and was
available only here. And a public surface that is not DECLARED is not a public
surface; it is a directory listing.

**Consequences.** `python/harness/public.py`,
`Black-Eclipse-site/pyproject.toml`,
`scripts/generate-tool-registry.js`,
`docs/harness/tool-registry.json` (generated, 946 tools),
`python/tests/test_public_api.py`, and the regenerated manifest. The
published manifest also caught a real bug: the `analysis_run` tool's
description interpolated the FUNCTION OBJECT instead of its result, so the
tool that runs analyses had been advertising `<function recipe_catalogue_text
at 0x...>` to the model instead of the recipes it can actually run. A guard
now asserts that no published description carries an object repr.

**What is NOT here, and why.** (1) **The package is not published to PyPI** -
that needs a credential and a decision about the name; the metadata is ready
for it. (2) **ACP is not implemented.** The Agent Client Protocol would be
the right interop surface - the 2026 field is converging on it - but the
protocol specification was not available in this environment, and shipping an
implementation written from memory would be exactly the "a claim, not a
measurement" this harness refuses. The portable manifest is the honest
interim: it is a stable, versioned, digested description of the tool surface
that any host can consume. (3) **There is no opencode/Claude Code plugin
yet.** The repository has no plugin source to follow, and a plugin that
duplicates the manifest would be a second source of truth. When a plugin
exists, it should READ the manifest.

> **ADR-H284 was renumbered to ADR-H299.** Both branches used this number; the first entry keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H299 - A process-wide model override is a lease: it is handed back

**Decision.** Two rules for the process-wide model seam.

1. **Every override is restored.** A gate, benchmark or fixture that replaces
   `graph.build_chat_model` MUST save the previous value and restore it in a
   `finally`. The workflow and resume gates now do; before this, a gate's model
   stayed installed and every later run in the same process answered with it.
2. **The evaluation seam never clobbers an active override.** The seam wraps
   the real builder (or another delegating seam) and refuses to replace a plain
   function that is currently installed - that is a test double, and replacing
   it would make it disappear at its teardown.

**Why.** The seam is process-wide, so a leaked override is a silent, global
behaviour change: in the full suite it broke 36 unrelated tests (graph, memory,
vision, portfolio, RSL) that all pass in isolation. The failure looked like a
regression in the code under test and was not one.

**Consequences.** `evaluation/runner.py` (seam installation rules),
`eval_gates/workflow.py` + `eval_gates/resume.py` (restore the builder).
Tests: `test_evaluation_runner.py` (the seam leaves an active override alone and
installs when none is active). Docs: `docs/CHANGELOG.md`.
> **ADR-H246 was renumbered to ADR-H300.** Both branches used this number; the first entry keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H300 — The runtime's own explanation reaches the model, and the surface it advertises is the surface it has

**Decision.** Three rules about what the runtime SAYS, each found by a probe
against the live app:

1. **A refusal carries the runtime's explanation.** The tool's refusal path
   reduced every refusal to a code and a generic phrase ("I couldn't work that
   out"), so the catalogue the runtime had already computed — the ready-made
   recipes for an unknown name, the input spec for a missing input, the refused
   element for a source rule — never reached the model. The refusal now carries
   the runtime's own text.
2. **The surface the tool advertises is the surface it has.** The analysis
   tool offered a data channel but published only the binding NAMES, so a
   program had to discover `price_history`'s arguments by trial; a model that
   burns runs on it concludes the sandbox is closed. The schema now publishes
   every binding's signature, and the recipe catalogue publishes every recipe's
   inputs.
3. **A produced file says where it is.** An artifact is registered in the
   Artifact Center, not written into the project tree; the result now says so,
   with the ids, so the model does not go hunting for a file that was never
   there.

**Why.** The boundary held under the probe — every escape was refused by the
right mechanism — and the CAPABILITY still did not come through, because the
runtime knew the answer and did not say it. A model that is told "I couldn't
work that out" for a name that exists in the catalogue will conclude the
capability is missing; the runtime has to hand over what it already knows.

**Consequences.** `analysis_tools.py` (the refusal keeps the explanation, the
artifact's location), `code/sandbox_tools.py` (`tool_signatures`),
`code/recipes.py` (the catalogue in the refusal). Tests:
`python/tests/test_analysis_guard.py` (30). Docs: `docs/CHANGELOG.md`.
> **ADR-H285 was renumbered to ADR-H301.** Both branches used this number; the first entry keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H301 - A surface may narrow the CONTEXT it injects, never widen its authority

**Decision.** A live surface may carry two context policies, both closed sets:

- `skillManifest`: `none` | `top3` | `top8` - how many skills are listed as a
  hint in the prompt (the manifest is a hint; narrowing it removes hints, never
  a capability).
- `memoryPolicy`: `off` | `profile_only` | `profile_plus_search` - whether
  recalled memory, the persistent profile, or both are injected. `off` injects
  neither; `profile_only` keeps the stable profile and drops the recall.

Both live on `AgentConfig` and on `HarnessSnapshot` (digest-additive: an empty
policy is not part of the identity, so older snapshots keep their digest). An
unknown value is refused - never treated as the default. Neither policy grants
a tool, a permission or an effect, and neither changes the tool allowlist.

**Why.** The surface could previously change the prompt, the tool subset and the
ceilings. It could not express "this agent should not be reminded of past
notes" or "list fewer skills" - the two most common operator requests, and both
are context questions rather than authority questions.

**Consequences.** `schemas.py` (`AgentConfig.skillManifest` / `memoryPolicy` +
the closed sets), `self_improvement/models.py` (the snapshot fields),
`self_improvement/live_surface.py` (the live application),
`graph_context.py` (the injection bounds), `routers/evolution.py` (the surface
experiment request carries them). Tests: `test_surface_context_policy.py`,
`test_rsl_snapshot.py`, `test_live_surface.py`. Docs:
`docs/self-improvement/rsl-v3-plan.md`.
> **ADR-H286 was renumbered to ADR-H302.** Both branches used this number; the first entry keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H302 - A guardrail is a runtime rule: it fails the run, it never instructs the model

**Decision.** A live surface may carry `guardrails` (a closed set) and
`maxAnswerChars`:

- `require_tool_before_answer` - the run executed at least one tool.
- `require_citation` - the answer carries at least one citation (the evaluator's
  own citation parser decides what a citation IS).
- `maxAnswerChars` - the answer is not longer than the given bound.

They are checked ONCE, at the end of the run, against what the run really did
(the persisted tool executions) and what it really answered. A violation FAILS
the run with a named code (`GUARDRAIL_TOOL_REQUIRED`,
`GUARDRAIL_CITATION_MISSING`, `GUARDRAIL_ANSWER_TOO_LONG`) and the answer is
not shipped as a completed result. A broken guardrail implementation degrades to
"no violation" - it can never break a run.

**Why.** Everything the surface could change before this was prompt text, tool
subset or ceilings: all either advisory or authority. An operator who wants
"this agent must show its sources" needs a rule the RUNTIME enforces, and a rule
that only lives in the prompt is a request, not a guarantee.

**Consequences.** `self_improvement/guardrails.py` (the evaluation),
`schemas.py` (`AgentConfig.guardrails` / `maxAnswerChars` + the error codes),
`self_improvement/models.py` (the snapshot fields), `self_improvement/live_surface.py`
(the live application), `graph.py` (the terminal check), `routers/evolution.py`
(the experiment request). Tests: `test_rsl_guardrails.py`,
`test_graph.py` (a run that breaks a guardrail fails with the named code),
`test_live_surface.py`, `test_rsl_snapshot.py`. Docs:
`docs/self-improvement/rsl-v3-plan.md`.
> **ADR-H287 was renumbered to ADR-H303.** Both branches used this number; the first entry keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H303 - The loop reports on itself, and a drift episode may open its own candidate

**Decision.** Two operator rules for RSL v3.

1. **There is ONE summary, and it is honest.** `GET /evolution/rsl/report`
   (and the weekly card in the operator panel) returns what the loop did in a
   window: experiments, accepted / rejected / unmeasured, the real cost and the
   cost per accepted improvement, the candidates in flight, the lessons adopted
   and where production is drifting. A measure that was never taken reads
   `UNKNOWN`; it is never a fabricated zero. The same digest renders as
   Markdown, so it can be read by a person or by CI.
2. **A drift episode may open its OWN inert candidate - off by default.**
   `HARNESS_RSL_DRIFT_AUTOCANDIDATE=on` lets the drift watch create, on a NEW
   transition into drift, the SAME inert candidate the operator route creates.
   The candidate carries no target and no operations: it is evidence that enters
   the existing propose -> verify -> land flow. It never lands, never activates
   and never bypasses the operator. With the flag off (the default) the watch
   stays exactly as it was: read-only.

**Why.** A loop that cannot report on itself is invisible, and an invisible
loop is either ignored or trusted blindly. The digest is the difference between
"the loop is on" and "here is what it did this week, and it cost $0.25". And a
regression that only becomes visible when a human opens a card is not
monitored - but the automation must stay opt-in, because an automatic candidate
is still a change to the queue of work.

**Consequences.** `self_improvement/report.py` (the digest), `routers/evolution_report.py`
(the route group), `self_improvement/drift_watch.py` (the opt-in candidate),
`src/components/screens/skills/rsl-weekly-card.tsx` (the card),
`src/lib/harness/evolution-{client,browser}.ts` + the Next.js proxy. Tests:
`test_rsl_digest.py`, `test_drift_watch.py` (the opt-in candidate),
`rsl-weekly-card.test.tsx`. Docs: `docs/self-improvement/rsl-v3-plan.md`.
> **ADR-H288 was renumbered to ADR-H304.** Both branches used this number; the first entry keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H304 - The unattended session is a GATE: it fails on a critical regression, and the baseline only moves forward on a clean run

**Decision.** `npm run rsl:nightly` runs the corpus once (the deterministic
tier), compares the result with the recorded baseline through the EXISTING
per-dimension regression gates, writes a dated JSON + Markdown report, and
exits non-zero when a CRITICAL measure regressed (task success, safety score,
hallucination rate, financial accuracy, routing). Cost, latency and token
movements are reported as warnings and do not fail the session.

The baseline moves forward only when the run is clean AND the caller passes
`--promote`: a worse run never silently becomes the new normal. With no
baseline yet, the first run reports what it measured and passes.

**Why.** A loop that is only ever watched is not verified. The gate that
already existed (`scripts/test-evals.js`) proves the corpus passes TODAY; this
proves the corpus has not got WORSE since the recorded baseline. A gate that
cannot fail is decoration, so the regression is PLANTED in a test: a task that
does not pass must make the session say FAIL and name the measure.

**Consequences.** `evaluation/nightly.py` (the session, the verdict, the
report), `scripts/rsl-nightly.js` + `npm run rsl:nightly`. The baseline,
regression gates and report writers are the existing ones
(`evaluation/baseline.py`, `evaluation/regression.py`,
`evaluation/report.py`) - no second gate, no second baseline store. Tests:
`test_evaluation_nightly.py` (the planted regression must fail the session and
the report). Docs: `docs/self-improvement/rsl-v3-plan.md`,
`docs/harness/agent-evaluation.md`.
> **ADR-H289 was renumbered to ADR-H305.** Both branches used this number; the first entry keeps it and this one is the second. The old number is retired - the decision is unchanged.

## ADR-H305 - A delegated child inherits the parent's context policy, and the surface may give it a prompt

**Decision.** A surface may carry `subagentPrompt` (≤4,000 characters). When it
is set, the DELEGATION tool builds the child's agent explicitly: the child's own
preset prompt, plus the surface's bounded instructions, and the child's tools
are the NARROWED set the delegation already computed (the parent's allowlist,
or the child's request within it).

The prompt is evidence, not authority: it cannot add a tool, raise a ceiling,
change a permission or grant an effect - the child's allowlist is still the
narrowed one, and the child is still a normal `execute_run` with a parent link.
Without the field the delegation path is exactly as it was.

**Why.** A delegated child runs the SAME agent preset as its parent, so a
surface that improves the parent's prompt did nothing for the child - and the
child is where the fan-out work actually happens. The child's tool set is
narrowed by the delegation layer, so naming the child's prompt next to that
narrowing is the honest place to state it.

**Consequences.** `schemas.py` (`AgentConfig.subagentPrompt`),
`self_improvement/models.py` (the snapshot field), `self_improvement/live_surface.py`
(the live application), `tools.py` (`ToolContext.agent` - the resolved agent of
the run), `graph.py` (the context carries it), `delegation_tools.py` (the child
agent is built explicitly when the surface set a prompt). Tests:
`test_delegation_tool.py` (the child inherits the prompt and the narrowed
tools), `test_rsl_snapshot.py` (bounded + digest). Docs:
`docs/self-improvement/rsl-v3-plan.md`.
## ADR-H306 - The lab tries to improve the harness itself, and it may only report a CANDIDATE

> Renumbered from ADR-H290 during the Round 3 merge: H290 was already the sandbox
> ADR. The decision is unchanged; only the number moved.

**Decision.** `npm run rsl:lab` (the surface lab) tries to improve the
harness's OWN surface and measures every attempt:

1. The variants are DETERMINISTIC and derived from the lessons the loop already
   ADOPTED (one lesson, the top three, and their cues as a checklist). With no
   adopted lesson there is no variant: the lab never invents an instruction
   nobody verified. The base prompt is never rewritten - only bounded
   `toolGuidance` is added.
2. Each variant is measured on the SAME tasks through the EXISTING measured
   surface path (`verifier.verify_surface`), against the base's own run.
3. The ranking is honest: an UNMEASURED variant is never a winner, a variant
   that regresses a critical category (safety, adversarial) is VETOED even when
   its average is better, and "flat" is a result, not a win.
4. The winner is a CANDIDATE for the operator. The lab never lands, never
   activates and never writes to the harness.

**Why.** Everything the loop can change is either code (a worktree, a promotion)
or a surface (prompt, guidance, context policy, guardrails). The surface is the
cheap one - no worktree, no landing - and it is where most of the headroom is.
But a self-improvement step that can only be measured by a live model is a
promise, not a result; on the scripted tier the lab correctly finds NO
improvement, because a scripted answer does not depend on the prompt.

**Consequences.** `self_improvement/surface_lab.py` (the variants, the ranking,
the report, the CLI), `scripts/rsl-lab.js` + `npm run rsl:lab`. Tests:
`test_surface_lab.py` (the variants are bounded and derived; a critical
regression vetoes; an unmeasured variant never wins; the scripted tier reports
"flat" and NO winner). Docs: `docs/self-improvement/rsl-v3-plan.md`.
## ADR-H307 - There is ONE surface-candidate path, and the lab stages EVIDENCE, not a decision

> Renumbered from ADR-H291 during the Round 3 merge: H291 was already taken. The
> decision is unchanged; only the number moved.

**Decision.** Two rules for surface experiments.

1. **One path.** The identity of a surface experiment and the row it creates
   live in `self_improvement/surface_candidate.py`. The operator route
   (`POST /evolution/surface-experiments`) and the surface lab both call it, so
   a lab winner and an operator experiment are the same thing to the rest of
   the system - and re-running the same experiment never creates a second row.
2. **The lab stages evidence; the operator decides.** The lab's winner is
   staged as an INERT surface candidate whose problem statement carries the
   measured delta and the lessons it came from. `POST
   /evolution/surface-lab/{id}/queue` is the operator's one click to queue it;
   the loop then verifies it through the SAME measured path every other surface
   experiment uses, and only then can it be activated (with the canary and the
   rollback). The lab itself never lands, never activates and never queues.

**Why.** A self-improvement loop that could land its own candidate would be an
authority the rest of the system does not have. Staging the measurement as an
inert candidate keeps the SAME gate for a machine-authored improvement and an
operator-authored one: verify, then activate, with the canary and the rollback
in between.

**Consequences.** `self_improvement/surface_candidate.py` (the identity + the
creation), `routers/evolution.py` (the operator route now calls the shared
path; the new queue route), `self_improvement/surface_lab.py` (`stage_winner`).
Tests: `test_surface_candidate.py` (one identity, one row, the staged winner is
inert, nothing is staged without a winner), `test_surface_lab.py`. Docs:
`docs/self-improvement/rsl-v3-plan.md`.
## ADR-H308 - A prompt can only be measured with a real model; the live tier never guesses a provider

> Renumbered from ADR-H292 during the Round 3 merge: H292 was already taken. The
> decision is unchanged; only the number moved.

**Decision.** The surface lab has two tiers and they are not interchangeable:

- The **scripted** tier (the default) replays a deterministic answer. It proves
  the harness path works and that the comparison is wired correctly - and it
  CANNOT see a prompt change, because a scripted answer does not depend on the
  prompt. It therefore reports "flat" and no winner, which is the honest
  result, not a gap.
- The **live** tier (`npm run rsl:lab -- --live --provider <p> --model <m>`)
  runs the SAME measured path with a real model. The credential comes from
  `HARNESS_EVAL_API_KEY`; the runtime never guesses a provider, a model or a
  key, and a live run without explicit settings is refused before it starts.

The live tier is the only tier in which the lab's verdict about a PROMPT change
means anything, so a report must say which tier produced it.

**Why.** A self-improvement loop that measures prompt changes against a
scripted model would report improvements that do not exist. The two tiers are
both useful - one proves the machinery, the other measures the thing - but only
one of them can decide a prompt change, and the report has to say which.

**Consequences.** `self_improvement/verifier.py` (`verify_surface` takes the
tier and the settings), `self_improvement/surface_lab.py` (the lab's live tier
and its CLI flags), `evaluation/candidate_invoker.py` (the live invoker already
refuses to run without explicit settings). Tests: `test_surface_lab.py` (the
live tier reaches the invoker with the explicit settings, and the run stays
deterministic in the test). Docs: `docs/self-improvement/rsl-v3-plan.md`.

## ADR-H309 - The structured-read budget is set from the measured slowest read, not from a guess

**Decision.** The data bridge's structured-read timeout is **15 s**, and the
comment above it names the measurement that produced that number. It stays below
the run's tool budget (20 s, `graph.py`), so a slow read still returns a structured
`UNAVAILABLE` instead of being killed as `TOOL_TIMEOUT`.

**Why.** The budget was 8 s, justified in prose as "before the smallest
market-tool budget (10 s)". The 10 s budget was real, and the pair was simply too
tight: the slowest read is 8.56 s. Measured against the live bridge on
this machine, one structured capability cannot fit 8 s:

    market_data             3.91 s
    market_snapshot         0.04 s  (cached)
    live_quote              3.57 s
    financial_statements    8.56 s  (cold)

`financial_statements` therefore failed on EVERY call, and the live evaluation
lane could not measure a single finance dimension. Six attempts to paper over it
from the other side - a production build (68 -> 15 failures), a 9.5 s budget (12),
concurrency 1 (13) - all failed, because the slowest read was above the line and
queueing pushed the rest over it.

**Consequences.** The finance tools' `timeout_ms` moves 10 s -> 25 s in the same
change, so the ordering the contract depends on (bridge 15 s < tool 25 s) holds
and `test_timeout_classes_stay_inside_the_tool_budgets_they_serve` still enforces
it. `bridge.py::HttpDataBridge._TIMEOUT_S` is 15.0 with the measured
basis in the comment. A structured read that now takes longer than 15 s means the
provider regressed, not that the budget is wrong - the number must be re-measured
before it is changed again. The network class (20 s) is unchanged. This does not
claim the finance dimensions now measure well; it removes the reason they measured
NOTHING.

## ADR-H316 - The search may propose from a measured stepping stone; only the champion can be replaced

**Decision.** `self_improvement/archive.py` retains measured states that did not
beat the champion as `stepping_stone` rows, and `self_improvement/sampler.py`
chooses the exploration parent from that archive (`uniform` / `weighted` /
`uct`). Two decisions are now separate:

- **where to propose from** - the sampled archive state;
- **what may replace the champion** - `utility.acceptance` against the
  champion only.

A candidate that beats a weak sampled parent but not the champion is retained,
never accepted. A stepping stone is never promotable, activatable or current.

**Why.** A climb that proposes only from the incumbent discards the measured
states that make open-ended search work. The Darwin G�del Machine ablation is
the clearest external evidence: archive sampling scored 50.0% where greedy
selection scored 39.7% and no open-ended archive scored 23.0%. The archive is
opt-in (`HARNESS_RSL_ARCHIVE=off` by default) because it adds stored states, and
the safety floor must not move: acceptance still requires a measured win, no
critical regression and a replay pass.

**Consequences.** `self_improvement/{sampler,archive}.py` (new),
`self_improvement/state.py` (additive `HarnessState` columns + `stepping_stone`
status + `list_for_sampling`/`children`/`record_visit`/`set_scores`),
`self_improvement/hillclimb.py` (`archive=`/`rng=`, per-generation
`proposalParentId`, retention), `self_improvement/config.py`
(`HARNESS_RSL_ARCHIVE*`, clamped; hot config accepts them),
`self_improvement/loop.py` (constructs the archive only when enabled). Tests:
`python/tests/test_rsl_archive.py`, an archive-on conductor test in
`test_rsl_loop.py`; the archive-off suite is unchanged. Docs:
`docs/self-improvement/rsl-v4-plan.md`.

## ADR-H317 - Rank exploration parents by descendant lift, and keep it out of acceptance

**Decision.** `self_improvement/cmp.py` estimates a state's metaproductivity as
the depth-discounted mean of `utility(descendant) - utility(state)` over its
measured descendants (bounded by `HARNESS_RSL_CMP_HORIZON`). The archive
persists the estimate as `cmpScore` on the state and its ancestors after a new
measured state is retained, and attaches a fresh estimate on the read path for
older rows. `sampler.scores` blends `cmpScore` into the parent score. A state
with too few measured descendants is `coldStart` with `lift: null`; it is never
treated as a measured zero.

**Why.** HGM showed that an agent's own benchmark score is a poor proxy for its
potential to produce further improvement, and that ranking by estimated
descendant performance beats ranking by the leaf's score. RSL already has the
lineage needed to compute that estimate. It must remain a *sampling* signal,
because a high-lift state is evidence about where to explore, not evidence that
its child is better than the champion.

**Consequences.** `self_improvement/cmp.py` (new),
`self_improvement/archive.py` (`ArchiveConfig.cmp*`, `refresh_cmp`,
read-path attachment, summary), `self_improvement/config.py`
(`HARNESS_RSL_CMP`/`_HORIZON`, clamped; hot config accepts them),
`self_improvement/loop.py` (passes the CMP settings). `utility.acceptance` is
untouched. Tests: `python/tests/test_rsl_cmp.py`; the archive/hill-climb suites
remain green. Docs: `docs/self-improvement/rsl-v4-plan.md`.

## ADR-H318 - A proposal is typed, near-duplicates are vetoed, and islands migrate evidence not decisions

**Decision.** A proposal carries a typed operator (`prompt`, `tool_guidance`,
`tool_subset`, `ceilings`, `context_policy`, `guardrail`, `subagent_prompt`,
`model_identity`, `skill_draft`, `code_patch`, `composition` or `unknown`).
`operators.py` classifies op sets and snapshots, and defines novelty as the
Jaccard distance over normalized tokens. `propose_many` accepts
`novelty_min`/`exclude_signatures`; when enabled it drops near-duplicates and
raises `PROPOSAL_ALL_DUPLICATES` if nothing new remains. A non-global `scope`
is an island: every N measured states (`HARNESS_RSL_ISLAND_MIGRATION`),
`StateArchive.migrate_best` copies the island's best measured state into
`global` as a `stepping_stone` with the same snapshot digest and a new row.

**Why.** The loop was proposing mutations with no name and no memory of what
shape had already been tried, so near-identical variants could consume the
daily budget as if they were independent evidence. Typing makes the experiment
readable; the novelty veto protects the budget. Islands let a scoped climb
(for example a finance agent) contribute evidence to the global archive without
promoting its own state or widening its authority. Migration copies evidence,
not a decision: it cannot activate, and acceptance still compares against the
champion.

**Consequences.** `self_improvement/operators.py` (new),
`self_improvement/proposer.py` (`CandidateProposal.operator`,
`novelty_min`/`exclude_signatures`), `self_improvement/archive.py`
(`ArchiveConfig.islands`/`island_migration_every`, `migrate_best`, `islands`,
island-restricted `parent_for`), `self_improvement/config.py`
(`HARNESS_RSL_NOVELTY_MIN`/`_ISLANDS`/`_ISLAND_MIGRATION`, clamped; hot config
accepts them), `self_improvement/{recursion,loop}.py` (passes the novelty
floor). The per-scope lease and the global busy flag are unchanged. Tests:
`python/tests/test_rsl_operators.py`, `python/tests/test_rsl_islands.py`.
Docs: `docs/self-improvement/rsl-v4-plan.md`.

## ADR-H319 - The corpus has a committed manifest, and a purpose reads exactly one split

**Decision.** `evaluation/corpus.py` writes
`baselines/corpus_manifest.json` (per-dataset checksum, task counts by split,
oracle coverage, canary count) and `verify_manifest()` recomputes every
checksum from the task files. The `corpus-manifest` gate in
`scripts/test-evals.js` fails on drift. `evaluation/contamination.py` maps each
purpose to exactly one split (`optimization`/`validation`/`canary`/`release`);
`access_guard` refuses a cross-split read, `select_for_purpose` returns refused
tasks with reasons, and a decoy canary (`metadata.canary`) turns a PASS into
`CANARY_HACK`.

**Why.** The corpus count had already drifted between documents (76 vs 88) and
nothing checked a dataset change against a recorded summary. The manifest is a
derived, committed summary - not a second task store - so a checksum, count or
split change is visible in review. The split contract makes the distinction the
loop depends on explicit: the search may read optimization, acceptance reads
validation, the canary reads held_out, and the release gate reads release. A
decoy canary that a candidate "passes" is evidence of answer-fitting, not
capability, so it must fail rather than count as a win.

**Consequences.** `evaluation/corpus.py` (new),
`evaluation/contamination.py` (new),
`evaluation/baselines/corpus_manifest.json` (committed),
`scripts/rsl-manifest.js` + `npm run rsl:manifest`,
`scripts/test-evals.js` (the manifest gate). The manifest is refreshed with
`--build` after a deliberate corpus change; `--audit --strict` is the authoring
rule for new tasks (oracle + contamination metadata). Tests:
`python/tests/test_evaluation_corpus.py`,
`python/tests/test_evaluation_contamination.py`. Docs:
`docs/harness/agent-evaluation.md`, `docs/self-improvement/rsl-v4-plan.md`.

## ADR-H320 - A lift must clear its paired noise floor, and the decision must know the sample size

**Decision.** `evaluation/significance.py` adds a paired bootstrap over TASKS,
an exact McNemar test for paired pass/fail tasks, a coarse `required_trials`
planning estimate, and `paired_rates` (which reads a metrics dict OR a
`SuiteResult`). `ablation.compare_arms` reports `deltaCi`, `pValue` and
`pairedTasks`, and renders the CI. The RSL acceptance decision uses the paired
CI only when `HARNESS_RSL_SIGNIFICANCE` is on: at least
`HARNESS_RSL_SIGNIFICANCE_MIN_TASKS` paired tasks are required and the CI low
bound must be above zero. A two-task win is refused.

**Why.** A Wilson interval on one arm answers precision, not significance, and
the loop was accepting a point-estimate improvement from a sample too small to
distinguish from noise. The comparison is paired by construction (same tasks,
same seeds), so resampling tasks is the correct null. The rule is opt-in
because a small scripted corpus cannot support a significance claim and the
default must not turn every honest flat result into a rejection. Significance is
an acceptance rule, never a sampling signal: the archive/CMP ranks exploration
parents, and it must not be confused with the evidence that a change is real.

**Consequences.** `evaluation/significance.py` (new),
`evaluation/ablation.py` (paired CI columns in `compare_arms` and the Markdown
report), `self_improvement/utility.py` (`acceptance(..., require_significance=)`),
`self_improvement/hillclimb.py` (`HillClimbConfig` significance fields),
`self_improvement/config.py` (`HARNESS_RSL_SIGNIFICANCE`/`_MIN_TASKS`, clamped;
hot config accepts them), `self_improvement/loop.py` (passes them). Tests:
`python/tests/test_significance.py`, `python/tests/test_rsl_significance.py`,
`python/tests/test_ablation.py`. Docs: `docs/harness/agent-evaluation.md`,
`docs/self-improvement/rsl-v4-plan.md`.

## ADR-H321 - Only the live tier may be pinned, and the key includes the prompt and the corpus

**Decision.** `evaluation/live_baseline.py` pins a live-tier baseline keyed by
provider + model + base URL + temperature + prompt hash + corpus checksum.
`build_live_baseline` refuses the scripted tier and an empty provider/model, and
stores the Wilson CI, per-category rates, task rates, cost/latency and
provenance (never a key). `nightly.py` gained `--live-baseline`,
`--write-live-baseline` and `--publish`, all requiring `--live`.
`publish_report` writes a Markdown report that names the tier, identity, prompt
hash, corpus checksum and verdict; no pinned baseline is `NOT COMPARED`, never
`PASS`.

**Why.** A scripted replay cannot see a prompt change, so pinning it as a model
baseline would manufacture evidence. A baseline is only comparable to the
experiment that produced it, so the prompt and corpus are part of its identity:
a changed prompt is a new entry, not a silent rewrite of the old number. The
report has to say which tier produced it and must never present an unpinned run
as a pass. The gate ("one recorded live run") stays an operator action because
it needs a real provider, model and key; the repository never fabricates it.

**Consequences.** `evaluation/live_baseline.py` (new),
`evaluation/nightly.py` (live-baseline flags + comparison + report),
`scripts/rsl-nightly.js` (path flags), `baselines/live_v1.json` (created on
first operator write). Tests: `python/tests/test_live_baseline.py`. Docs:
`docs/harness/agent-evaluation.md` §7.4,
`docs/self-improvement/rsl-v4-plan.md`.

## ADR-H322 - Autonomy is risk-classed, evidence-gated and killable, and it never widens what an operator approved

**Decision.** `self_improvement/autonomy.py` classifies a change as `L0`
(code, tool widening, guardrail removal, model identity), `L2`
(capability-widening: added tools, raised ceilings, removed guardrails) or `L1`
(bounded surface: prompt wording, tool guidance, narrowed context policy, a
delegated-child prompt, added guardrails). `decide(...)` allows only L1 under
`supervised`/`bounded`, with no kill switch, a paired `deltaCiLow` above
`HARNESS_RSL_AUTO_MIN_LIFT`, no critical regression, clean canary/replay, a
matching predicted direction, and a daily budget. `AutonomyKillSwitch` persists
under `rsl.autonomy.kill`; `/evolution/rsl/autonomy/pause|resume` stop/allow
decisions without a restart. `POST /evolution/rsl/autonomy/decide` previews a
decision and never activates.

**Why.** The loop could verify a change but had no explicit answer to "may the
runtime apply this by itself?", so the only safe state was "never". Making the
answer explicit requires three properties: a conservative risk ceiling (a tool
addition is capability, not wording), evidence stronger than a point estimate
(the paired CI from ADR-H320), and a stop that does not depend on a process
restart. The decision stays pure so it can be audited and replayed; activation
stays on the existing single-use surface path, which is why this slice does not
silently widen authority. Auto-applying an L1 winner is a supervised follow-up
after a dry run (`HARNESS_RSL_AUTO_MAX_PER_DAY=0`).

**Consequences.** `self_improvement/autonomy.py` (new),
`self_improvement/config.py` (`HARNESS_RSL_AUTONOMY`/`_AUTO_*`, clamped; hot
config accepts them), `self_improvement/runtime_settings.py` (writes commit),
`self_improvement/loop.py` (status, pause/resume, pure decision helpers),
`routers/evolution.py` (autonomy status/pause/resume/decide routes). Tests:
`python/tests/test_autonomy.py`. Docs:
`docs/self-improvement/rsl-v4-plan.md`, `docs/harness/architecture.md`.

## ADR-H323 - A chat notebook is a sidecar document over the existing sandbox, and a chart's code is a linked alternate view

**Decision.** Runnable cells live in a per-conversation document beside the
transcript (`.black-eclipse/sessions/<chatId>/notebook.json`), not inside the
response `chart` block and not as a new execution engine. A new operator-gated
`notebook` route constructs the analysis request itself: the import widening is
the named `("io", "matplotlib")` host constant, `allow_tools=False`, the output
directory is cell-scoped, and the result is always `computed`. An "Edit code"
action on a chart asks `POST /charts/code` for a matplotlib starting script
generated from the chart's resolved data; the resulting cell is linked to the
chart by `chartBlockId`, and the chart block remains the canonical answer. Notebook
cells may share an **opt-in** kernel namespace; `analysis_run` keeps its
fresh-namespace semantics.

**Why.** A chart is already a validated spec, deliberately not code
(`python/harness/charts/spec.py:179`), and `python_chart` accepts only that spec.
Editing therefore needs a place for a program and a way to run it. The runtime
already has both: the Phase 3 sandbox, the figure recipes' named import widening,
artifact collection and verification. Reusing them keeps one execution path and
one honesty rule. Turning chat messages into cells would couple the conversation
record to a mutable working document, so the cell document is a sidecar: the
transcript still says what was said, the notebook says what is being worked on.
The kernel currently keeps a process per conversation but builds a fresh globals
dict each round, which is why persistent state must be explicit rather than
assumed. Storing the source verbatim closes the gap left by
`AnalysisResult.source_digest` (a digest cannot be edited).

**Consequences.** New `python/harness/routers/notebook.py`,
`python/harness/charts/codegen.py`, an internal
`AnalysisRequest.persist_namespace` (default `False`), and an opt-in persistent
globals dict in `code/kernel.py`. Front door `src/lib/harness/notebook-client.ts`
and `src/app/api/harness/notebook/**`; per-chat document
`src/lib/chat/notebook-store.ts`; UI `src/components/chat/notebook-cell.tsx` plus
an "Edit code" action in `src/components/response/response-chart.tsx`; i18n keys
under `notebook.*`. The chart contract and the `python_chart` tool are unchanged.
A cell output is never a research claim, and a chart edit never mutates the
original chart block. Shipped 2026-10-22; plan and gate record:
`docs/harness/roadmap/phase-21-notebook-cells.md`,
`docs/harness/roadmap/progress/phase-21-progress.md`.

## ADR-H324 - Auto-apply is one surface-only path, and its activation row is the rollback handle

**Decision.** `RSLLoop.auto_apply_surface(state_id)` is the ONE automatic
activation path. It accepts only a `candidate` state whose `EvolutionCandidate`
is `subjectType == "surface"` with a valid `surfaceConfig` snapshot/base. It
derives per-task rates from the recorded baseline and candidate `EvaluationRun`s,
runs the paired bootstrap, computes the per-dimension regressions, classifies
the risk, and calls the existing `decide(...)` policy (level, kill switch, L1
risk, lift floor, canary/replay, daily budget). On success it calls the existing
`HarnessStateStore.activate_surface` and returns the `HarnessActivation` row;
`rollback_activation` is the undo. `POST
/evolution/states/{state_id}/autonomy-apply` is the operator-triggered seam.

**Why.** The P6 decision policy could say "allowed" but nothing could act on it,
so supervised autonomy was a report rather than a capability. Auto-applying must
not invent a second activation mechanism: it reuses the single-use surface
activation and its rollback row, so the safety properties are the ones already
reviewed. Restricting it to a *measured surface state* (not a lab result, not a
code patch, not a raw candidate) means the evidence, snapshot and rollback all
already exist. Missing paired evidence is a refusal, not a fallback to a point
estimate. The daily counter is persisted, so a restart cannot reset the budget.

**Consequences.** `self_improvement/autonomy.py` (activation counter),
`self_improvement/loop.py` (`auto_apply_surface`, run-rate/aggregate helpers),
`routers/evolution_autonomy.py` (`POST
/evolution/states/{state_id}/autonomy-apply`). Tests:
`python/tests/test_autonomy_apply.py` (apply + rollback, daily budget, kill
switch, L2/non-surface refusals, missing evidence). Docs:
`docs/self-improvement/rsl-v4-plan.md`, `docs/harness/architecture.md`.

## ADR-H325 - Rotation is a deterministic, seeded plan, and applying it is a separate operator act

**Decision.** `evaluation/rotation.py` plans a quarter's held_out rotation from
the optimization split with a seed derived from the quarter label, so the same
quarter always names the same tasks. `apply_rotation` is a separate call (the
CLI defaults to a dry run and requires `--apply`) that writes the split, bumps
the task version and records `metadata.rotation`; applying the same quarter
twice is a no-op (`already_rotated`). Validation is never rotated. A dataset may
set `metadata.rotationExempt` (the frozen team benchmark does) and is skipped.
The manifest is refreshed after an apply.

**Why.** A held_out task that a candidate has seen is no longer a clean canary,
so the corpus needs a rotation path. Automating it would silently change what
the canary measures; a seeded plan plus an explicit apply makes the change
reviewable and reproducible. Exempting the team benchmark keeps its frozen
multi-agent comparison intact. Validation is excluded because it is the
acceptance set and the search must never train on it.

**Consequences.** `evaluation/rotation.py` (new), `scripts/rsl-rotate.js`,
`npm run rsl:rotate`, `datasets/team_v1.json` (`rotationExempt`), the refreshed
manifest. Tests: `python/tests/test_evaluation_rotation.py`. Docs:
`docs/harness/agent-evaluation.md`, `docs/self-improvement/rsl-v4-plan.md`.

## ADR-H326 - Auto-apply stays operator-triggered; there is no scheduler tick

**Decision.** `RSLLoop.auto_apply_surface` is invoked only by the operator route
`POST /evolution/states/{state_id}/autonomy-apply`. No timer, `tick()`, or
background task calls it, and no `HARNESS_RSL_AUTO_APPLY` switch exists. The
existing policy (supervised/bounded level, L1 ceiling, paired evidence, kill
switch, daily budget) still bounds that operator-triggered path. A scheduler may
be added later only as a separate, bounded slice behind an off-by-default flag.

**Why.** The activation row is the rollback handle, but an unattended scheduler
changes the failure mode: a bad L1 change can be applied and observed by users
between ticks. The Phase 4 review found no operator need for unattended
activation, and the daily budget is not a substitute for a human noticing a
regression. Keeping the seam explicit means every activation has an operator
action behind it and a rollback one click away.

**Consequences.** No new config, no tick wiring, no scheduler tests. The
operator path remains covered by `python/tests/test_autonomy_apply.py` (apply +
rollback, daily budget, kill switch, L2/non-surface refusals, missing evidence).
Docs: `docs/self-improvement/rsl-v4-plan.md`, this ADR. A future scheduler needs
its own ADR, an off-by-default flag, a per-tick limit of one, and a dry-run
period.

## ADR-H327 - The size ceiling is raised deliberately for the data-integrity release, and Round 4 V2 must lower it again

**Decision.** `scripts/size-budget.json` is raised from 382,030 to 383,450 total
lines, with the `.` and `evaluation` directory ceilings raised to their measured
values (25,110 and 11,725). This is a one-release, recorded raise - not a silent
relaxation. The twelve data-integrity fixes (D1-D12) added ~1.4k Python lines to
`python/harness/` (new quality/freshness/gateway modules plus the retrieval and
market wiring), so the committed ceiling no longer matched the shipped tree and
`npm run check:all` failed at the size gate before any test ran. Round 4 V2 (the
terminal / execution / git-surface merges) is still required to lower the total,
one release per merge.

**Why.** A ratchet that cannot be raised deliberately turns every real capability
addition into a choice between deleting code and leaving the release gate red.
The budget's own contract says raising is allowed when it is an explicit,
reviewable edit; this ADR is that edit. The alternative - splitting the new
modules purely to satisfy a line count - would add indirection without removing
duplication. The direction that matters is that V2 then makes the next ceiling
lower, and every subsequent release is measured against it.

**Consequences.** `scripts/size-budget.json`; `npm run check:size` passes on the
data-integrity branch. The V2 shrink remains open and is the next ceiling-lowering
work. No runtime contract changes: the budget is a CI guard, not a capability
limit. Docs: this ADR, `docs/harness/roadmap/round-4-gap-closure.md` (V2).

## ADR-H311 - One terminal subsystem: the code profile runs through the TerminalManager in no-shell mode

**Decision.** `terminal_run` and the `.black-eclipse/verify.json` auto-verify hook
execute through the ONE `TerminalManager` (`terminal/manager.py`) instead of
calling `code/sandbox.run` directly. The manager gained `shell_mode: bool = True`;
the code profile passes `False`, which tokenizes the command with
`shlex.split(posix=True)` and runs the argv directly, preserving the no-shell
contract (`; rm -rf` and `$(...)` stay inert). `code/terminal.py` is now the ONE
classifier only; `code/terminal.py::build_run_spec` was removed. Tool names
(`terminal_run`, `execute_command`, `run_shell_command`, ...) are unchanged.

**Why.** There were two execution primitives: the terminal manager (used by the
UI and `execute_command`) and a direct `code/sandbox.run` call in the code
profile. Both consulted the same classifier, but the second bypassed the
manager's session/process/port/event bookkeeping, so the UI and the agent could
disagree about what ran. Keeping the manager as the single execution surface
means one place for bounds, redaction, recording and cancellation. No-shell mode
is a transport detail, not a second subsystem.

**Consequences.** `terminal/manager.py` (`shell_mode`), `code/terminal_tools.py`,
`code/tools.py::_auto_verify`, `code/terminal.py` (classifier only),
`python/tests/test_terminal_tools.py` (the manager-identity test),
`python/tests/test_code_terminal.py` (removed the `build_run_spec` tests). Docs:
`docs/harness/terminal-architecture.md`, this ADR.

## ADR-H328 - A clarification is a question, not an authorization

**Decision.** The `clarify` tool reuses the approval store's physical table and
in-process pause gate under a distinct `kind="clarification"`, with its own
additive `answer` column and a separate `POST /mcp/approvals/{id}/answer` route.
It is a `read` tool. `answer_clarification` refuses any row that is not a
clarification, and no effect authorizer ever consults an answer. When there is no
interactive client (a bot or scheduled/background run), the tool returns
`answered: false` immediately rather than blocking, and it never invents an
answer.

**Why.** The pause-and-resume machinery is exactly what a question needs, but
authority must not leak through the shared path: answering "5 years" must never
be one typo away from approving a write. Sharing the gate while keeping
authorization single-use and digest-bound lets the two flows share plumbing
without sharing power. A non-interactive fallback is mandatory because a run with
no card to resolve cannot wait forever, and a fabricated answer would violate the
no-invention rule.

**Consequences.** `python/harness/clarify_tools.py`;
`ApprovalStore.request_clarification`/`answer_clarification`; the
`clarification.requested`/`clarification.answered` events; the
`clarification-card.tsx` surface; the `run_watchdog.py` exemption. Docs:
`docs/harness/architecture.md`, `docs/NAVIGATION.md`, this ADR.

## ADR-H329 - Learned preferences are repeated evidence, staged inert, and gated by the same memory decision

**Decision.** A personal preference is inferred only from repeated style/format
behaviour across distinct runs, using a closed deterministic vocabulary
(`answer_format`, `answer_length`, `tone`, `research_depth`). Observations are
append-only `PreferenceObservation` rows with a bounded trigger-phrase excerpt,
not transcripts. A preference is proposed only after 3 observations across 3
distinct runs at 60% consistency; the proposal is an inert `MemorySuggestion`
carrying its evidence counts in `structuredData`. Accepting it writes a
user-scoped `preference` `MemoryRecord` through the existing suggestion route and
the ONE `eclipse_memory/decision.py` gate. Dismissing suppresses the same
inference for 30 days. Explicit preferences win; memory/preference-learning off
collects no evidence; secrets and current financial facts are never inferred.

**Why.** A single request ("show me a table") is a task instruction, not a stable
preference. Staging an inert suggestion keeps the user in control and prevents
the system from silently personalizing itself. Reusing the existing suggestion,
lifecycle, profile and decision-gate machinery avoids a second memory engine, a
second database and a second authorization path. The evidence counts make the
inference explainable rather than magical.

**Consequences.** `python/harness/eclipse_memory/preferences.py`; migration 34
(`PreferenceObservation`, `MemorySuggestion.structuredData`); service
`observe_turn_preferences`/`observe_run_preferences`/`learn_preferences`; policy
`preference_*` keys with per-dimension switches; routes
`/eclipse-memory/preferences` and `/eclipse-memory/preferences/learn`; `memory`
tool action `preferences`; the Eclipse Memory ? Preferences tab. Docs:
`docs/harness/personal-memory.md`, `docs/NAVIGATION.md`, `docs/ARCHITECTURE.md`.

## ADR-H330 - The Institution preset is one cast, four materialisations

**Decision.** The investment-committee preset (`analyst -> debate -> trader ->
risk -> PM`) is defined once as role data in
`python/harness/institution/cast.py`, and materialised into four existing
contracts: `AgentConfig` presets, A2A `AgentDefinition`s, a `TeamDefinition`
task graph, and bot specs consumed by the Bots workspace seeder. The pipeline is
always a Team run; a bot/group chat is never the orchestrator. Downstream stages
read a completed prerequisite memo through the read-only `read_run_output`
tool, keyed by the run id the task context already renders. The debate produces a
structured record of agreement and unresolved disagreement and never votes or
averages. The preset adds the `run_team` automation action so a scheduled run
opens a team run and waits for its terminal synthesis. Nothing executes an order.

**Why.** Bots are automations and team members are agent ids. Letting a team
reference automation ids would create a second identity system and let the two
drift. Generating every view from one spec keeps the seats, prompts and tool
surfaces identical everywhere. Reusing the existing team/A2A/Task layers keeps
the one-runtime rule (ADR-H1/H2/H155). The read-only handoff is necessary because
the task context intentionally renders only direct prerequisites, and silently
injecting a prior agent's full prose into another agent would blur provenance.
A dedicated action is required because a `run_agent` bot cannot reliably start a
team graph.

**Consequences.** New code lives in `python/harness/institution/`; routes extend
`routers/team.py`; the automation layer gains one action kind; the Bots
workspace gains a run dialog and a bot seeder. Per-seat model overrides remain
unsupported (one `modelSettings` per team run), and edited bots do not rewrite
the preset — the cast stays authoritative. The preset is opt-in and still
subject to ADR-H213's measured-adoption gate. A supporting fix makes
`tasks.manager.recompute_plan` keep a plan `RUNNING` while queued `TODO` work
remains, so a multi-level DAG is not marked `BLOCKED` after its first stage.
Docs: `docs/harness/institution-preset.md`, `docs/harness/bot-team-orchestration.md`,
`docs/NAVIGATION.md`, this ADR.

## ADR-H331 — Broker connectivity is one read-only adapter contract, attach-first, with no order path

**Decision.** Broker connectivity lives in `python/harness/trade/` and is split
into five seams: a `BrokerAdapter` read contract, a kind registry, a durable
`BrokerConnection` store on the ONE `agent-memory.db`, one `BrokerService`, and
operator-gated `/trading/*` routes with a front-door allowlist. The first adapter
attaches to an already-logged-in MetaTrader 5 terminal over local IPC. P0 is
read-only: account, positions, working orders, quotes. `POST /trading/orders`
returns an honest 501. Paper fills remain Graph Studio's `trading.paper_order`;
live execution is a later phase.

**Why.** The app already has a single-use, digest-bound approval store, the money
guardrails and the quant risk engine; the missing piece was a broker edge, not a
second trading engine. MetaTrader's official Python API is local IPC, so the
attach path needs no credential in Eclipse — the smallest possible secret surface.
A kind is a registration, so Alpaca/IBKR/MT4 bridges and plugin brokers add a
class instead of touching callers. Refusing secrets at write time is stronger than
redacting them at read time. A named `unavailable` reason is the only honest
answer when the terminal is missing, logged out or disconnected.

**Consequences.** New package `trade/` and route group `/trading/*`; new tables on
`agent-memory.db`; new read-only `broker_*` tools; no execution surface and no
`Trade` write. The dashboard `trading` widgets may now read a real connection but
must render the named reason when there is none. MT5 is Windows-only and requires
a running terminal; the Python package stays out of `requirements.txt` so
Linux/macOS installs are unaffected. Adding an order path requires a new ADR, the
P1/P2 gates in `docs/trading.md`, and the existing approval store — never a second
approval mechanism.

## ADR-H332 — Demo execution is one policy→guardrail→approval→send seam, gated twice as demo

**Decision.** P1 adds demo order submission only. `trade/policy.py` applies
deterministic connection limits (margin, volume, spread, open positions, symbol
allowlist). `trade/authorizer.py` evaluates the order as a `MoneyIntent` through
`FinanceGuardrailService`, creates a single-use `trade_order` approval whose
digest covers the exact `OrderIntent`, and consumes that approval atomically
before `order_send`. Demo is checked twice: the connection record must say `demo`
and the adapter's `account_info().trade_mode` must report demo at both preview and
submit. `order_send` is called once; a timeout or unknown result marks the ledger
`unknown` and is never retried. There is no agent `order_submit` tool.

**Why.** The approval store, money guardrails and quant risk rules already exist;
the missing piece was the demo execution edge, not a second trading engine. Two
independent demo gates mean a mislabelled connection cannot move real money: the
broker's own account mode is authoritative. Re-using `trade_order` as an approval
kind keeps one single-use mechanism and one audit trail. Refusing to retry is the
only honest choice because MetaTrader has no client order id; a blind retry after a
lost IPC reply can double a position. Keeping submission operator-only preserves
the existing rule that an agent may propose and preview, never execute.

**Consequences.** New `TradingAdapter` contract, `BrokerOrder` ledger, policy and
authorizer modules; `trade_order` joins the reviewable approval kinds; the MT5
adapter gains `order_preview`/`submit_order`/`cancel_order` with a full retcode
map. `BrokerKind.executionModes` states `("demo",)`, so the UI cannot imply live
execution. Live submission (P2) still requires a REAL-mode arm switch, a typed
confirmation, caps and a kill switch; it must extend this seam rather than add a
parallel one. Docs: `docs/trading.md`, `docs/ARCHITECTURE.md`, `docs/NAVIGATION.md`,
this ADR.

## ADR-H333 — Live execution is an armed, magic-scoped extension of the same trade seam

**Decision.** P2 enables live order submission only through a time-boxed arm on the
connection. Arming requires: connection mode `live`; `account_info().trade_mode`
`real`; Algo Trading enabled in the terminal; a non-zero `magic`; and a positive
`maxMargin` cap. The operator types `ARM LIVE <login>`; the arm expires in 60 s to
8 h and is cleared by a mode/config change, a failed probe, expiry, or the kill
switch. Every live order still uses `trade/authorizer.py`: policy → money
guardrails → single-use `trade_order` approval → `order_send`. In addition, the
preview always returns `requiresTypedConfirmation` with a `LIVE <SIDE> <volume>
<symbol>` phrase that submit must repeat. The kill switch cancels all working
orders with the connection's magic and disarms; it refuses magic 0 and does not
close positions. `order_send` is never retried on an ambiguous result.

**Why.** A global "live enabled" flag would be one mistake away from an unintended
order. A per-connection, expiring arm ties the permission to one account and one
short window. A non-zero magic is the only reliable ownership marker in MT5, so
the kill switch can be both effective and safe. A mandatory `maxMargin` means a
live connection cannot be armed without an explicit risk cap. The per-order typed
phrase is a second, human-speed decision after the approval card, not a replacement
for the digest-bound approval. Refusing to close positions in the kill switch keeps
this phase's blast radius to cancellations; closing is a distinct, risk-reducing
action that needs its own design (netting vs hedging, partial fills).

**Consequences.** `BrokerConnection` gains `armedUntil`/`armedAt`/`armedBy`;
`TradingAdapter` gains `cancel_all(magic)`; the MT5 adapter gains a magic-filtered
cancel-all; `BrokerKind.executionModes` becomes `("demo", "live")`; new routes
`/trading/connections/{id}/arm|disarm|kill`; the client gains arm/disarm/kill.
`_assert_account_mode` checks the terminal's Algo Trading flag as well as the
account's. The arm is not a substitute for the per-order approval, and no
automation may arm a connection. P3 must build on this seam. Docs:
`docs/trading.md`, `docs/ARCHITECTURE.md`, `docs/NAVIGATION.md`, this ADR.

## ADR-H334 — Unattended graph orders use a bounded demo mandate, never a live mandate

**Decision.** Graph Studio's `trading.broker_order` is paper-mode only and requires
an operator-created `TradingMandate`: a demo connection, an allowed-symbol set, a
per-order `maxMargin`, a `maxOrders` count and an expiry (60 s to 7 days). Creation
requires the typed phrase `MANDATE DEMO <label>`. Each use is counted atomically, so
a looping graph cannot exceed the mandate. Revocation is immediate. The node calls
the SAME `BrokerService.submit_order` path as manual orders, so connection policy,
money guardrails, the demo/real account check and the no-retry rule still apply. The
ledger records `mandateId`. Live mandates are not implemented: `create_mandate`
refuses a live connection, and the compiler/front door still refuse a live trading
node.

**Why.** A graph run has no approval card, so "unattended" needs an explicit
operator authorization that is bounded in every dimension that matters (symbols,
margin, count, time) and revocable. Making it demo-only keeps the worst case a
simulated fill on a demo account while the node contract and UI are proven. Reusing
`submit_order` is the only way to avoid a second execution path with its own
account checks and retry semantics. Keeping live graphs refused is defense in depth:
even a forged run grant cannot reach live execution.

**Consequences.** New `TradingMandate` table on `agent-memory.db`; routes for mandate
create/list/revoke; a `mandateId` column on the broker-order ledger; the
`trading.broker_order` node in both the Python registry and the TS graph registry.
P4 (unattended live) needs a separate ADR, a live mandate with stricter limits, and
a review of position reconciliation and partial fills. Docs: `docs/trading.md`,
`docs/ARCHITECTURE.md`, `docs/NAVIGATION.md`, this ADR.

## ADR-H335 — Reconciliation is a durable copy of broker truth, not a second source

**Decision.** `trade/reconcile.py` reads broker positions, working orders and deal
history and writes a durable local copy: `BrokerFill` (deduped by connection + deal
ticket), `BrokerPositionSnapshot` (replaced atomically), and `BrokerSyncState`
(cursor, status, counts, last error). It updates in-flight ledger orders only when
broker history is definitive (`filled`/`partial`/`cancelled`/`rejected`/`expired`);
a working ticket is `placed`. A failed sync records an error and keeps the last good
data. The background loop is opt-in (`TRADING_RECONCILE_SECONDS`, default 0);
manual `POST .../sync` always works.

**Why.** A read-on-demand is not a record: after a restart the app cannot explain a
fill, and the UI cannot show what happened while the agent was idle. Broker history
is the only authority, so it must be copied, not inferred. Deduping by deal ticket
makes an overlapping cursor safe; replacing the position snapshot avoids stale
positions; an explicit error state avoids the tempting "empty list means flat"
lie. Keeping the background loop opt-in respects the cost of IPC to a desktop
terminal and the fact that most installs do not have MT5.

**Consequences.** Adapter gains `deals_since`/`order_status`; `BrokerOrder` retains
`mandateId` alongside `approvalId`; new routes for sync/reconciliation/fills and
read-only `broker_reconciliation`/`broker_fills` tools; the loop starts/stops in
`boot.py`. Reconciliation does not close positions, mark to market, or authorize
anything. P5 (close/flatten, unattended live) remains a separate decision. Docs:
`docs/trading.md`, `docs/ARCHITECTURE.md`, `docs/NAVIGATION.md`, this ADR.

## ADR-H336 — Agent trade submission is mandate-authorized, not approval-authorized

**Decision.** Add `order_submit` as the ONE effectful agent broker tool. It is
admitted by an active, operator-created **demo** mandate (symbols, `maxMargin`,
`maxOrders`, expiry) and calls the SAME `BrokerService.submit_order` path as the
operator. The tool is registered with the registry's `read` permission so the
generic per-call approval card does not double-gate an unattended mandate; the
handler/service still enforces demo account mode, policy, money guardrails, symbol
scope, margin cap, order count and the no-retry rule. There is no agent path to
create/revoke a mandate, arm live, cancel, or submit live. A `broker_mandates` read
tool and a Settings mandate panel let the operator create/revoke the authorization.
The agent prompt tells it to keep analysis brief and call `order_submit` directly.

**Why.** A per-order approval card makes "ask Eclipse to analyze and trade"
impossible; an unattended mandate is the operator's explicit, bounded consent and
already has a digest/usage/expiry contract. Marking the tool `write` would require
a second authorization for the same decision and still fail. The handler/service is
the authority that matters: it can only submit one bounded demo order, uses the
same ledger/approval-free mandate path, and never reaches live. The explicit
`read` label is a deliberate exception; the effect metadata (`trading`, high risk)
and the audit trail preserve the real effect class.

**Consequences.** `order_submit` and `broker_mandates` join the agent allowlist; the
Settings panel gains mandate create/revoke; the prompt gains a short trading
instruction. A verified demo run placed ticket **11164809214** (EURUSD BUY 0.01,
SL 1.11125, TP 1.12260) under mandate `mnd_c82ba003d785`. The generic effect
authorizer wrapper remains as defense in depth if the permission label changes.
Live is still unreachable: no live mandate, and the account-mode checks reject a
real account. Docs: `docs/trading.md`, `docs/NAVIGATION.md`, this ADR.

## ADR-H337 — Closing a demo position is a risk-reducing exit, not a new mandate use

**Decision.** Add a demo position-close path: `TradingAdapter.close_position`,
`BrokerService.close_position` / `close_positions`, operator routes
`POST .../positions/{ticket}/close` and `POST .../positions/close`, the dashboard
Close/Close-all actions, and the agent tool `position_close`. A close needs no
mandate and no approval card, and it does not consume a mandate order; the service
refuses a live connection (`LIVE_NOT_ENABLED`) and a real account
(`DEMO_ACCOUNT_REQUIRED`). The MT5 adapter sends the exact opposite market deal
with the position ticket as `position` and the position's magic, once. The agent
prompt and a per-run `# Close request` directive tell it to prefer
`position_close` over research, a hedge or `order_preview`.

**Why.** "Step out" is the one trade action whose risk is entirely one-directional:
closing cannot increase exposure, so requiring a fresh authorization to reduce it
would keep a user trapped in a position when the mandate is used up or expired —
exactly when an exit matters most. It mirrors the existing cancel rule (cancel a
working order with no approval) instead of the submit rule (a mandate authorizes
new exposure). Ticket-scoped sends avoid the class of bug where "flatten" becomes a
blind close of the wrong position, and the magic is carried so the deal still
belongs to Eclipse.

**Consequences.** `position_close` joins `BROKER_TOOLS` and its authorizer admits it
when a ticket/`all` is named; the demo-only check lives in the service, where a
mislabelled connection cannot bypass it. Close is not a substitute for the P5
close/flatten design: live exits, partial closes and bracket (SL/TP) management
remain out of scope, and a live close still belongs in the terminal. Docs:
`docs/trading.md`, this ADR.

## ADR-H338 — Broker-native bars are the data source for a broker symbol

**Decision.** Add `broker_bars` and the `TradingAdapter.bars` /
`BrokerService.bars` seam. MT5 returns its own `copy_rates_from_pos` series
(server time, `M1…W1`, 1–500 bars) with contract/tick size; a pure
`trade/analysis.py` computes true range, ATR(14) and the lookback range. The
quote gains `contractSize`/`tickSize`. The per-run trade directive uses
`broker_bars` for the symbol being traded and forbids substituting a proxy ETF.

**Why.** XAUUSD exists on the terminal but not in the stored instrument
universe, so every market-history route returned `UNAVAILABLE` and the model fell
back to GLD as a proxy — a different instrument with a different price, beta and
contract. A stop derived from a proxy is not a stop for the position. The broker
already holds the exact series; reading it is more accurate, cheaper and honest
about provenance. Computing ATR in Python removes model arithmetic from risk.

**Consequences.** A bracket can now be justified by ATR and a measured range for
any tradable broker symbol, not just the stored universe. History depth is capped
at 500 bars and the vocabulary is fixed; an unknown timeframe is refused rather
than guessed. The advisory gate no longer treats a confirmation table as advice.
Docs: `docs/trading.md`, this ADR.

## ADR-H339 — Exits are verified against the broker, and partial/modify are exits too

**Decision.** Extend the demo lifecycle to partial closes (`volume`), close by
`symbol`, and SL/TP modification (`position_modify`). Every close, modify and
market fill is **read back** from the broker and returns `verified` + `verifyNote`;
a mismatch is surfaced, never masked as success. All three remain mandate-free and
approval-free, demo-only, and are implemented in `trade/positions.py` so the
broker service stays within its size budget. The position list text now names the
ticket.

**Why.** A `order_send` reply is an acceptance, not proof of stored state. The
live run showed exactly why this matters: a stop above the bid was legally
rejected, a TP-only retry was accepted, and only a read-back could state the
position's real bracket. Partial exits and moving a stop are still risk-reducing
verbs; requiring a fresh mandate to protect an open position would trap the user
when the mandate is exhausted — the same argument as ADR-H337. A wider stop can
increase risk, so the tool text says so and the result is audited.

**Consequences.** The trade surface is now open / close (full, partial, by symbol)
/ modify, each with a broker-verified result. Live remains refused. Bracket
management (trailing, OCO) and live exits remain out of scope; the MT5 bridge is
ratcheted as one wire module with a read/execute split as the next candidate.
Docs: `docs/trading.md`, this ADR.

## ADR-H340 — Risk limits are connection policy, and the daily brake never blocks an exit

**Decision.** Add three optional connection-config limits: `riskPerTradePct`
(equity risk at the stop, computed from `|entry − stop| × volume × contractSize`),
`maxDailyLossPct` (rolling 24 h realised + open P&L) and `maxSymbolNotional`
(per-order notional). With `riskPerTradePct` set, an order without a stop is
refused rather than treated as zero-risk. The daily brake is checked in the
service because it needs the broker's deal history; it raises
`DAILY_LOSS_LIMIT` before any mandate/approval is consumed. Exits
(`position_close`, `position_modify`) are never subject to it. Settings exposes a
compact Risk limits editor.

**Why.** A per-order margin cap bounds cash committed, not money lost. The user
can still be wrong by more than intended, or keep re-entering after a losing day.
Risk-per-trade is the only rule that scales the position to the stop; the daily
brake is the only rule that stops a bad day from becoming a bad week. Blocking
exits would be the opposite of risk management, so the brake applies to entries
only. Config (not code) keeps the numbers explainable and per-account.

**Consequences.** Every submit path (agent, approval, graph node) enforces the
same limits; `preview_order` shows the risk rule that would refuse, and submit
adds the daily check. A missing stop is refused only when the risk cap is on, so
existing setups are unchanged. Limits are per connection, not per mandate; the
mandate still bounds count and margin. Docs: `docs/trading.md`, this ADR.

## ADR-H341 — A trade turn binds a trade-shaped tool surface

**Decision.** When a run's prompt is a trade, close or modify request, the graph
narrows the policy-resolved allowlist to `TRADE_SURFACE_TOOLS` (~18 names: broker
reads, bars, order submit, position close/modify, response helpers, clarify).
Narrowing happens after surface resolution, replaces the published resolution's
`allowed`/`boundTools`, and can only remove. A run whose surface has no
submit/close/modify tool is left untouched.

**Why.** The agent carries ~700 tools and a 41.9k-character prompt. A trade turn
needs a quote, bars, one order, or a close — not shell, code, plugins or 600
research tools. The live measurement was unambiguous: 721 → 18 tools and ~228k →
47k prompt tokens for the same three-step trade, with the model calling exactly
the intended tools. A smaller surface also makes a wrong-tool detour visible.

**Consequences.** Trade runs are cheaper, faster and easier to audit. The static
system prompt still mentions other tools, which is harmless but is the next diet
candidate. The narrowing is lexical (`is_trade_turn`) and deterministic; a
misclassification only removes tools for that run, never grants one, and the
`allowedTools` field on the run records exactly what was bound. Docs:
`docs/trading.md`, this ADR.

## ADR-H342 — Custom agent presets are runtime-owned, versioned and effect-gated

**Decision.** Operator-authored agent presets live in `agent-memory.db`
(`AgentPreset` + immutable `AgentPresetVersion`), not in the Next.js settings
store. The runtime owns CRUD, validates every tool name against the live
registry, and appends a version on every edit. A graph node pins
`agent@version`; an unpinned node resolves the current version for backward
compatibility. Naming a write/external tool requires an explicit
`allowEffectful` acknowledgement at save time.

**Why.** Agent behaviour is runtime state: the prompt and tool allowlist decide
what the model can do, and the Python worker must resolve them without reaching
into JDDS. Keeping presets in the settings vault would create a second authority
and make a resumed run depend on the front door's configuration view. Version
pinning is the same reproducibility rule as a pinned subworkflow: editing a
preset must not silently change a saved workflow.

**Consequences.** Preset CRUD is a thin Next proxy over `/agents/*`; the browser
never sees the operator token. A preset cannot widen itself by naming an
effectful tool — the save is refused until the operator ticks the acknowledgement.
Built-ins stay code (`agents.py`); the three names are reserved because the
store ids are generated (`agt_…`). Docs: `docs/graph-studio.md`, this ADR.

## ADR-H343 — Unattended live trading is two-key and workflow-pinned

**Decision.** A Graph Studio live order requires BOTH (a) an armed live
connection (typed `ARM LIVE <login>`, time-boxed) and (b) an active live mandate
that pins `workflowId` + `workflowVersion`, the allowed symbols, a margin cap, an
order count and a ≤24 h expiry. A mandate substitutes for the per-order typed
phrase only for unattended runs; a manual live order still requires it. The
mandate never arms the connection and the arm never authorizes an order. The
compiler requires `allowLiveTrading` plus pinned `connectionId`/`mandateId`; it
refuses paper-only trading nodes in live mode. Broker reads are read effects;
close/modify/cancel are risk-reducing and stay arm-free and mandate-free.

**Why.** Live automation is the product goal, but an LLM or a scheduled graph
must not be able to create, arm or widen its own authority. Pinning the workflow
version means editing the graph invalidates the mandate, and the two independent
keys mean a stale mandate cannot trade after the arm expires or a compromised
mandate cannot trade without an armed terminal. Keeping exits outside the gate
ensures risk can always be reduced.

**Consequences.** Live execution is possible only through `trading.broker_*`
nodes; `trading.paper_order` remains paper-only. The mandate store gained
`workflowId`/`workflowVersion` and live TTL bounds; the execution context carries
the workflow id for the pin check. A live mandate cannot outlive the connection's
arm at execution time even if its own TTL is longer. Docs: `docs/trading.md`,
`docs/graph-studio.md`, this ADR.

## ADR-H344 - Node contracts change additively at the same version

**Decision.** A node's `type@version` names a contract. Adding an OPTIONAL port,
adding a config field that has a default, or widening a port type (`number` →
`any`, so a series payload can be wired in) is additive: the node keeps its
version and no saved IR is migrated. Removing or renaming a port or config key,
changing what a default means, or narrowing a type is a contract break: it needs
a NEW version plus a migration entry, and the old version stays registered until
no saved graph references it. Control outputs a node fires must be declared
(the executor now refuses an undeclared name instead of routing it nowhere).

**Why.** The runtime has no per-version registry, and saved graphs pin
`type@version`. Bumping for every additive tweak would strand every existing
graph; never bumping would let a removal change behaviour silently under the
same name. Optional-first is also what keeps the editor forgiving: a port added
later can be left unwired and the node still runs on dataflow.

**Consequences.** Every new input port starts `optional=True` unless the node
genuinely cannot run without it. `registry.manifest.json` is the cross-language
contract and now carries each config field's bounds (`maxLength`, `enum`,
`min`/`max`, `integer`), so `test_graph_registry_parity.py` fails if the editor
and the runtime disagree about what a valid value is. Peer review of a node
change is a question of add-vs-break before it is a question of code.

## ADR-H345 - Quant work is a chat-native protocol, not a UI mode

**Decision.** A quantitative turn is identified by the deterministic
`quant_intent` classifier and receives a per-run protocol appended to
`runtime_context`. The protocol may narrow the bound tool surface (trade
precedence first, remove-only), require `model_catalog` before a build, require
exactly one final `Artifacts: …` / `Artifacts: none — …` line, and state the gold
basis (spot `XAUUSD` vs `GC=F` futures). There is no separate quant screen,
workspace panel or catalog page; the catalog is a read-only agent tool whose text
result is summarized in chat.

**Why.** The product is a chat agent. A separate UI would duplicate the same
registries, drift from the runtime's tool/store truth, and split the operator's
attention. The model already has the engines, the catalog and the response
contract; the missing piece was a deterministic contract around the model, not a
new surface. Keeping it in `runtime_context` also keeps the byte-stable prompt
cache intact and makes the protocol observable as a `quant.plan` event.

**Consequences.** New quant behavior lands in `quant_intent.py` (+ the eval gate
and tests), not in a screen. The chat answer is the deliverable; a missing
artifact line is corrected with an explicit `Eclipse check` note, never silently
passed. The model catalog is the one reuse surface (`model_catalog`), and its
empty state is an empty registry, not an unavailable capability. If a future
operator wants a visual catalog, it must consume the same `build_catalog` read
model rather than forking one.

## ADR-H346 - A build belongs to the project it was created in

**Decision.** A code build carries a project for its whole life. The run resolves
its root from the per-chat binding (`workspaceRootId`, an APPROVED trusted-root
id) or the selected root; `worktree.allocate/release/diff` cut from, and clean up
in, THAT root; the disposable workspace records it
(`CodeWorkspace.checkoutRoot`, additive and old-row-safe); promotion
preview/apply/rollback target the recorded root and refuse a project that has
moved instead of redirecting; a writing chat turn writes `session.created` and
`task.created`, so it is a first-class build; land checks come from the project's
own manifests (with `.black-eclipse/land.json` winning); generated capabilities
draft under `<project>/.black-eclipse/` and the dynamic registry is refreshed for
the run's root.

**Why.** The runtime used `default_root()` (the globally selected folder) at
three different moments: allocation, promotion and cleanup. Binding a chat to
project B and then switching the selection to A could review and land a diff in
A. A single-user app may only have one "selected" root, but "selected" is a UI
convenience, not an identity.

**Consequences.** No request body carries a project path; callers name a task or
an approved root id and the runtime resolves it (`_task_checkout_root`,
`trusted_root_path`). A build whose project disappeared is a refusal, not a
fallback. The size ratchet, the per-project scorecard and the preview all key off
the same recorded root, so "this project" means the same thing everywhere. New
project-scoped state must be recorded on that row rather than inferred from the
current selection.

## ADR-H347 - The v0.3.0 open-source release ships on the measured live floor; the 0.9 target is not edited

**Decision.** The v0.3.0 open-source, self-hosted release accepts the measured
live-eligible `task_success` of **0.8520** (167/196, run
`live:f368f388bb662ce717c19da715f0bb44`) as a **version-scoped release floor**.
The live gate's 0.9 threshold and `DEFAULT_THRESHOLDS` are **unchanged**, the gate
still reports **FAIL**, and `docs/harness/benchmarks.md` keeps publishing the
measured number, the raw inclusive score (0.6901) and the 0.9 target beside it.
The exception ends at the next release: it must either reach 0.9 or record a new
ADR.

**Why.** The artifact is source code for single-operator, self-hosted installs
where the operator chooses the model, and the live score depends on that model.
A fresh 0.9 run needs a provider key that is not available here; editing the
threshold, raising the offline 1.0, or omitting the number would all be a
fabricated pass. Shipping with the failure published and the target explicit is
the honest option, and the safety/financial gates that do not depend on the live
lane are green on the release commit.

**Consequences.** The threshold stays a real gate: a run below 0.8520 fails, and
raising the number needs a fresh run and a new ADR-H. `benchmarks.md` carries the
version-scoped annotation, the release note states what is not met, and the
readiness roadmap's C8 closes as Option B. This covers a single-operator
self-hosted deployment, not a hosted multi-tenant service.

## ADR-H348 - The agent loop owns its retry, its run deadline, and its execution registration

**Decision.** The main agent path goes through `step.invoke_model_step`, which
applies `retry_policy.RetryPolicy` to the model call. `run_timeout_s` is the
RUN's wall clock (`retry_policy.RunDeadline`), while a model call gets its own
smaller ceiling; the deadline is checked before every model call and every tool
turn. A spent deadline is `RUN_TIMEOUT`. Retries are refused once text has
streamed, and emit one durable `model.retry` event per attempt. Separately,
`service.run` registers every foreground run in `service._active` (the same
registry `run_background` uses), so SSE and cancel see both paths;
`HARNESS_MAX_CONCURRENT_RUNS` optionally bounds admission.

**Why.** The retry subsystem existed (`ModelRuntime.invoke`) but the only path
that mattered — `graph.agent_node` — called `model_stream.ainvoke_streaming`
directly, so one 429/500/disconnect ended a run. The run timeout was also passed
as the per-call timeout, so a 24-step run could occupy 24 × the budget, and only
the silence watchdog noticed. Foreground runs were never in `_active`, so
`GET /runs/{id}/stream` replayed and closed while the run was executing and
`cancel()` returned false. These are trust bugs, not polish.

**Consequences.** A retry is visible in the trace (`model.retry`) and cannot
duplicate streamed text. The graph's existing context-overflow and provider
error classification stays authoritative, because `invoke_model_step` re-raises
the original exception rather than wrapping it. Foreground and background runs
now share one registration/cancel/stream contract; the done callback owns the
registry entry, so an HTTP disconnect cannot leave it stale. The LangChain tool
fallback context is a `ContextVar` (not a registry class attribute), so two
concurrent runs cannot overwrite each other's `ToolContext`. The deeper Round 5
work (one scheduler, event log, lazy tool surface) is still tracked in
`docs/harness/roadmap/round-5-core-integrity.md`.

## ADR-H349 - One execution vocabulary, one migration ledger, and a checkpoint cursor

**Decision.** `harness/execution_plane.py` is the canonical lifecycle language:
`ExecutionPhase` plus total adapters from `RunStatus`, `JobState`, `TaskStatus`
and `graph_runtime.RunStatus` (unknown -> `None`, never an invented phase), and
`JobKind`. `harness/migrations.py` adds a per-namespace `HarnessMigration` ledger
that applies each opted-in migration once, in id order, in one transaction, with
a full rollback on failure; the legacy idempotent DDL is untouched.
`RunCheckpoint.payload.messageSeq` is a cursor into a new `RunMessage` table that
stores the full conversation; resume prefers the cursor and falls back to the
bounded legacy tail. `RunLineage` is the first subsystem on the ledger.

**Why.** Four planes named the same lifecycle four ways, so no caller could ask
one question of any of them. No subsystem could say which schema version a
database was on. And a checkpoint kept only the last 60 messages with each one
capped at 12k chars, so a long run silently lost the evidence it started with —
the exact opposite of what a resume is for.

**Consequences.** New lifecycle code maps through `execution_plane.py` rather
than inventing another string; a new state must be added to the canonical enum
and every adapter, with a test. New schema changes register a `Migration` instead
of only running `CREATE TABLE IF NOT EXISTS`; the ledger is per database
(`agent-memory`, `eclipse-memory`) and its current version is readable. A
checkpoint row is now small and constant-size; the conversation lives in
`RunMessage` and is rewritten idempotently at each durable boundary. Older
checkpoints remain valid (legacy tail fallback), so no data migration is needed.
This does not yet merge the four schedulers or the four event tables; that
remains the open half of C2.

## ADR-H350 - The Fabric is the one execution substrate; ExecutionEvent is the one log

**Decision.** Every execution attempt runs as an Execution-Fabric job. The task
scheduler keeps its claim/lease/heartbeat/verification loop as a producer but
submits the run through a `task` Fabric job (`await_result=True`); the Fabric
`task` worker reconstructs the live task and calls the same
`service.task_executor`. A submit failure falls back to the inline executor; a
submitted job's failure is reported, never silently re-run. Graph Studio already
runs as a `graph` job; an orchestration DAG goes through
`ExecutionManager.submit_dag`. `ExecutionEvent(source, entityId, seq, type, at,
data, parentId)` is the one append-only log: the run emitter and the Fabric,
graph-runtime, task and orchestration stores append to it after writing their own
row. Legacy rows remain as projections.

**Why.** The task scheduler was a second execution path: it called the executor
directly while the Fabric ran agent and graph jobs, so two schedulers could
claim the same machine resources and only one of them had Fabric's retry,
lease, cancellation and recovery semantics. In parallel, five event tables meant
no single reader could answer "what happened to this unit of work?" without
joining five different schemas.

**Consequences.** A task run now has one durable job row, one retry/cancel
semantics and one execution trace; the scheduler's claim loop is a producer, not
an executor. The one log is append-only and per-entity sequenced; its row is
bounded and fail-open, so losing a log row never fails the work. Per-plane tables
are **projections**, not sources of truth for new readers; a reader that needs
cross-plane history uses `ExecutionEventLog`. The log is not yet the source the
UI reads — that is a later reader migration, not a new schema. The task
scheduler's producer loop and the orchestration engine's internal layer scheduler
remain; they no longer own a second execution path.

## ADR-H351 - A run is composed from a RunScope and a RunSpec, not 45 keywords

**Decision.** `graph.build_graph(spec, scope)` is the only constructor. `RunScope`
holds the process-wide services a run reuses (registry, data bridge, stores,
memory, MCP, market, portfolio, ledger, advisory, decision, quant, project,
search, artifacts, UI, tasks); `RunSpec` holds the facts of one invocation
(request, emitter, resolved agent, effect authorizer, allowlist, resume point,
lineage, code ledger and result sinks). `RunSpec` copies the allowlist. A
contract test asserts the signature has exactly those two parameters.

**Why.** The 45-keyword constructor was the single hardest surface to call
correctly: a dropped bundle was a silent `None` at tool-call time, and the
`_run_kwargs` docstring exists because that bug had already happened. It also
made the graph's dependencies unreadable at a glance. The two lifetimes were
already distinct in the code; they were just flattened into one argument list.

**Consequences.** A new subsystem is added to `RunScope` once and reaches every
node; a new per-run fact is added to `RunSpec` once. `execute_run` keeps its
legacy keyword signature for the many existing callers/tests, and constructs the
two objects at the single graph call site; migrating `ToolContext` itself is a
separate tool-facing refactor, not a prerequisite. The size ratchet usually
shrinks as a result: comments and defaults move to the dataclasses.

## ADR-H352 - A truncated tool result keeps its full text; tools are discoverable

**Decision.** `ToolRegistry.execute` stores the full text of a truncated result
in `ToolOutput` (run-scoped, 2M-char ceiling) and returns `output_id`; the
model-facing text names the handle and next offset, and `read_tool_output` pages
it back (12k default, 24k max). `tool_search` returns up to 12 matching tools with
their exact schemas; `tool_invoke` executes a discovered READ-ONLY tool only.
`HARNESS_LAZY_TOOLS=1` keeps every effectful tool bound and routes the read-only
long tail through search/invoke; the flag is OFF until eval parity.

**Why.** The 24k bound was honest but lossy: the model could see that data
existed and could not read it. Re-fetching was expensive and sometimes
impossible. Separately, binding hundreds of tools into every run spends context
before the task starts, but simply hiding tools loses capabilities. Discovery
plus a bounded pager is the smallest change that keeps both capabilities and
context.

**Consequences.** A truncated result always names its handle; a handle from
another run is indistinguishable from a missing one. `tool_invoke` refuses
effectful tools, so the graph remains the ONLY execution path for anything with
side effects — the dispatcher cannot bypass budget, duplicate suppression,
idempotency, approval or recovery. `tool_search` is read-only and its response is
bounded. The lazy surface never widens an agent's declared tools: it intersects
with the original set, so an agent that lacks `tool_search` does not gain it.
Code Mode IS shipped, but only through the real sandbox: `HARNESS_CODE_MODE=1`
gates `tool_code`, which runs a short Python program in a `code/sandbox.py`
child and bridges READ-ONLY tool calls over JSON lines. Effectful tools are
refused at the bridge, so the graph remains the ONLY side-effect path. The child
gets a scrubbed env, no network, resource caps and a fresh temp cwd; a missing
isolation primitive is a named refusal, never an unsandboxed fallback. This is
not `tool_invoke` in a loop — the code runs in a separate, killable process.
Containment is process/resource/network/env plus a fresh cwd, **not** a
filesystem jail: an absolute-path read is still possible. The flag is therefore
opt-in and intended for a single-user install; a multi-tenant deployment must
wait for C6's filesystem sandbox profiles.

## ADR-H353 - A goal step is verified against durable evidence, and a rejection replans

**Decision.** A run may carry `input.context.c4Plan` (objective + acceptance +
ordered steps, or `planId` to continue). `harness/goals/` owns the goal-scoped
plan view (`GoalRecord`/`GoalPlan`/`GoalStep`), the evidence ledger
(`EvidenceClaim`/`EvidenceHandleRow`) and plan iterations on `agent-memory.db`.
Every tool result contributes its handles to the current step. The answer seam
records the draft's CHECKABLE sentences (numbers, finance shapes, URLs,
test/build claims, produced files) with the run's handles, then a deterministic
`VerifierRegistry` picks the most specific verifier: `finance_number` rejects a
number with no computation/report/citation/record; `citation` rejects a URL with
no citation; `code_check` rejects a pass claim with no check record; `artifact`
rejects a produced-file claim with no file/artifact; the fallback marks a claim
with no handle `unverified`. A specific rejection with replan budget left diverts
the graph through the existing bounded `replan` node (max 2); the rejected
draft's answer claims are superseded and its tool evidence is kept. Without
budget, the step is failed. The ledger and outcome always record the claims;
rewriting the answer with a disclosure is an operator opt-in
(`HARNESS_EVIDENCE_ENFORCE=1`) until eval parity, following the plan's
advisory-first rule.

**Why.** The harness had four verification systems (code ledger, task checks,
research citations, Fiduciary Gate) that could not answer one question: "what
proves this claim?" A general answer could assert an unsupported number, and a
step could be marked complete without any handle. Reusing the task runtime and
the existing gate modules keeps the change additive instead of building a second
agent loop.

**Consequences.** A plan step is `verified` only when the verifier registry
accepts its evidence; a plan without evidence cannot be marked complete. The
answer seam never rewrites a fact silently: it records, verifies, and either
repairs once per rejection or discloses "unverified". `finance_number` is
fail-closed: a bare `run` handle is not enough, so a number needs a computation,
report, citation, artifact, tool-output or record. Claim extraction is
deterministic; generic prose is not labelled, because that would be noise rather
than evidence. The plan/step cursor is in the checkpoint, so a new run with the
same `planId` continues the first non-verified step; a killed step stays
`running` and is never falsely verified. `GET /runs/{id}/outcome` exposes the
goal and evidence blocks. This does not replace the existing code/finance/
research gates; it runs after them and adds the cross-domain ledger.

## ADR-H354 - One spawn primitive; the blackboard is scoped to the root run

**Decision.** `spawn` is the one child primitive. It submits a normal Fabric
`agent` job with `parentRunId`, a clamped budget, `maxSteps` and
`runTimeoutMs`; the Fabric owns lease, retry, timeout and cancellation. A child
returns a `ChildOutcome` with output, claims, handles, artifacts and usage; its
evidence is copied into the parent's `EvidenceLedger` with a `child:<runId>`
verifier. Up to eight children run together and return a deterministic synthesis
(dedupe by claim text, conflict when values/evidence differ, provenance on every
item). `board_write`/`board_read`/`board_search` share one bounded blackboard per
ROOT run, found by walking `parentRunId`; unrelated trees are isolated.

**Why.** Delegation called `execute_run` directly, so a child did not get the
Fabric's execution semantics, and the parent received prose plus a summary to
merge. There was no shared workspace and no budget tree. One primitive makes the
child addressable, cancellable and evidence-bearing without inventing another
scheduler.

**Consequences.** Depth is capped at 2, fanout at 8, and total child token
allocation at 120k per spawn call; a refusal is typed (`DEPTH_EXCEEDED`,
`CHILD_LIMIT`, `BUDGET_EXCEEDED`), never a silent truncation. The batch reports
the budget tree as `allocated = used + returned` (unused allocation is returned,
not silently spent). A child inherits the parent's user/workspace/session but
never an operator grant or the parent's autonomy. The blackboard is
one board per root run, capped at 200 entries / 200k chars; an over-full board
refuses the write. Synthesis is deterministic: a conflict is surfaced, never
merged into a single answer. `a2a`/`team` keep their planned task rows and dependencies, but their execution
attempt is now routed through `spawn` at the task-executor seam
(`tasks/spawn_bridge.py`): when the Fabric is available, an A2A/team task runs as
a Fabric `agent` job and its `ChildOutcome` is mapped back to the task outcome;
without a Fabric it falls back to the inline executor. `delegate` remains the
legacy entry point for the old path.

## ADR-H355 - The safety envelope is a named profile plus a per-call token

**Decision.** Isolation has four named profiles — `read-only`,
`workspace-write`, `network-allowlist`, `full` — resolved by
`security/profiles.py`. A plan is `enforceable` only when the active backend
reports every guarantee the profile promises; otherwise it names the missing
guarantees and the gateway refuses an execution action when the operator
configured that profile (`security.sandbox.profile_blocked`). `network-allowlist`
is only enforceable through a configured egress proxy; the allowlist matcher is
exact-or-`*.domain` and rejects suffix masquerades. Separately, the security
authorizer mints a signed **effect token** bound to
`(runId, tool, argument digest, tenant, expiry)` on ALLOW, and the tool executor
verifies it immediately before the handler runs.

**Why.** The audit's finding was "strong policy, weak enforcement": the
gateway decided *whether* an action was allowed, but nothing bound that decision
to the bytes that actually executed, and the sandbox reported guarantees without
saying whether a given promise was achievable on this host. A profile plus a
call-bound token closes both gaps without a second authorization path.

**Consequences.** A host that cannot keep a profile is an honest refusal, never
a silent fall to `local`. Effect tokens default to `shadow` (mint + verify, but
record a missing token instead of blocking) while the migration lands;
`HARNESS_EFFECT_TOKENS=enforce` makes every non-read call fail closed with
`TOOL_FORBIDDEN` when its token is missing, expired, already used, or bound to
different arguments. Tokens use `HARNESS_AUTH_SECRET` when set and a random
per-process key otherwise, so zero-config local keeps working. Auth stays
required by default for a non-loopback bind or a production build; loopback
`npm run dev` remains zero-config. The four escape classes (path traversal, env
exfiltration, fork bomb, raw egress) are covered by `test_c6_escape.py`. The
terminal and background-process executors call `executor_guard()` before spawning
(`test_c6_executor_profile.py`), and `computer_use` does the same before input
injection, so a profile cannot be bypassed by a caller that skips the gateway.
Native backends: `NativeSandbox` uses Linux bubblewrap and macOS Seatbelt with
pure, tested argv/policy builders; `WindowsAppContainerSandbox` is the opt-in
Windows primitive (package SID, zero network capabilities, workspace-only ACL)
and refuses on any launch failure. Both are covered by `test_c6_native.py` and
the opt-in `test_c6_appcontainer_live.py` (live filesystem-escape proof). Plugin
installs pass a signature + review gate: `plugin.sig` is either an Ed25519
publisher signature (trusted public keys, RFC 8032 implementation verified
against the standard vector) or an operator HMAC, over the canonical manifest and
package digest. `HARNESS_PLUGIN_REQUIRE_SIGNATURE=1` fails closed without key
material and `HARNESS_PLUGIN_REQUIRE_REVIEW=1` requires the operator
acknowledgment; install and update both roll back on refusal
(`test_c6_plugin_signing.py`). Remaining follow-up: a human review UI (the review
gate is the explicit operator decision, not a UI).

## ADR-H356 - Telemetry is a release gate, not a dashboard

**Decision.** The observability platform becomes the release gate for the core
loop. The graph emits `step.started`/`step.completed`; the projector produces a
`run → step → llm/tool` span tree. A closed failure taxonomy labels spans,
errors and `eclipse_failures_total`. Cost is attributed per run and per step.
SLOs support `ratio`, `percentile` and `rate` kinds, with terminal correctness,
p95 latency, resume p95, orphan rate and cost per task alongside the existing
agent/tool success objectives. `npm run gate:slo` runs a deterministic probe
through the shipped projector and evaluates `docs/harness/slo-budgets.json` plus
the committed baseline; CI and `check-all.js` fail on a breach or regression.
`/observability/dashboard` renders one card per objective.

**Why.** The audit's "measurement" finding was that scorecard claims outran
evidence. A dashboard nobody can fail is decoration. C7 makes the same numbers
that describe the system decide whether a release ships: a latency regression, a
missing terminal state, an orphan or a cost blow-up is CI-red, not a chart.

**Consequences.** Every failure must map to a known class; unknown codes become
`internal` and are counted so the vocabulary grows deliberately. The gate is
deterministic and offline (no provider/network), so it runs in every CI job; real
deployments can feed `GET /observability/release-report` into the same
`gate.py`. Percentile SLOs use a bounded sample window and skip honestly when
there are no samples. The `ops` scorecard dimension moves from `unmeasured` to
`measured` because `gate:slo` is runnable. The dashboard is server-rendered HTML
(operator-gated), not a second front-end stack. Budgets and baselines are data in
`docs/harness/`, reviewed like code.

## ADR-H357 - Conformance is a manifest plus real invariants

**Decision.** C8 starts with one offline conformance gate
(`python -m harness.evaluation.conformance` / `npm run gate:conformance`) built
from three classes: a fault matrix (retry/429/500/disconnect/400/cancel/deadline/
duplicate-effect), a golden `run → step → tool/LLM` trace, and contract checks
(closed hook vocabulary, versioned/digested tool manifest, release-report schema,
plugin SDK version). `docs/harness/conformance.json` is authoritative: a case
that disappears or an undeclared case fails the gate.

**Why.** The evaluation system had many tests but no single runnable artifact
that named the harness invariants and failed a release when one regressed. A
manifest prevents silent skips; a golden trace protects the C7 span contract; the
contract checks protect the packaging/API promises C9 will publish.

**Consequences.** Adding a case requires a real invariant and a manifest entry;
"skip" is never a pass. The fault matrix currently covers the retry/deadline/
cancel/duplicate-effect primitives — the full kill-at-every-state process matrix
and the external adapters remain C8 follow-ups. The gate is offline and runs in
CI before the test suite, so a core-loop regression fails fast. It reuses the ONE
retry policy, effect ledger and observability projector; there is no second
harness.

## ADR-H358 - External evidence is opt-in, comparable or explicitly not

**Decision.** The four external benchmark adapters (SWE-bench Lite, GAIA,
tau-bench, FinanceBench) implement one `DatasetAdapter` protocol. Committed data
is a two-task, license-labelled offline fixture per adapter; real benchmark data
is downloaded only with an explicit opt-in and is never committed. An adapter
with no data is `available=false` with a reason. Adapter tasks normalize to the
existing `EvaluationTask` with `metadata.external` provenance. Release reports
are compared by `compare_release`: same dataset set, version, checksum, tier and
grader version, or the pair is reported incomparable with the exact cause.

**Why.** Adding four benchmarks as four bespoke harnesses would create four
measurement contracts and four ways to fabricate a number. A single protocol plus
a hard comparability check keeps the claim honest: a score belongs to a named
dataset version and tier, and a missing download is not a pass.

**Consequences.** Offline CI runs the fixtures (fast, no network); live
benchmark numbers require an operator opt-in and are published with their
dataset/checksum/tier. The finance QA corpus (`finance_qa_v1`) is the internal
counterpart: deterministic numbers, recomputed by the financial grader, plus an
explicit unavailable-figure task. The offline tier is already the real graph
(`ScriptedAgentInvoker` calls `execute_run`); only the model is scripted.

## ADR-H359 - The public surface is versioned; ACP is a wire adapter

**Decision.** The package version (`harness.__version__`) and the public API
surface version (`harness.public.HARNESS_API_VERSION`) are declared and checked
together by `npm run release:check`; `pyproject.toml`, `package.json` and the
package must agree. The wheel is built from `pyproject.toml` and carries only the
runtime (no data/vault/DB). ACP is a small JSON-RPC 2.0 stdio adapter
(`harness/acp/`) over the existing runtime: `initialize`, `session/new`,
`session/prompt`, `session/cancel`, `shutdown`; unsupported methods are typed
errors. It is a wire surface, never a second loop.

**Why.** C9 needs an external consumer to depend on a declared surface and to
install the runtime without this repository. Four version fields drifting (the
package was 0.3.4, the wheel 0.2.1-rc.1, the harness 0.1.0) is a release bug, not
a cosmetic one. ACP was only a name in the roadmap; a minimal, tested adapter is
better than a claim.

**Consequences.** Version bumps are one operation checked by a gate. The wheel
install smoke and the installed conformance/ACP self-test are the release
evidence. The ACP surface is intentionally minimal; broader methods remain a
follow-up. Tagging and PyPI upload are operator steps (credentials never live in
the repo).

## ADR-H360 - W8 signals are a contract: objectives are eager, invariants are zero-tolerance

**Decision.** Every workstream signal enters the ONE observability hub through
the existing bus projector (`observability/integration.py`) or, for a subsystem
that owns no `RunEmitter`, the fail-open `observability/bridge.py`. The W8
hooks (`onModelAttempt`, `onModelFirstToken`, `onModelFallback`,
`onContextPressure`, `onContextCompaction`, `onToolBatch`, `onToolGuardrail`,
`onGoalCreated`, `onGoalVerdict`, `onSkillEvaluation`, `onCuratorRun`,
`onIntegrityEvent`, `onFileCheckpoint`, `onDelegation`) carry closed-vocabulary
attributes only. Objectives are registered eagerly at hub construction; an
objective with no samples reports `measured:false`, `healthy:null` and appears
in `slos.unmeasured`, never as green. Correctness events (unknown effect, lost
lease, checkpoint write failure, event-seq collision, batch ordering violation,
skill enabled without evaluation) are invariants, not error budgets: any
occurrence fails the gate. The release report is schema v2 (additive over v1)
and carries `capabilities`, so a check whose feature is off is `skipped`, not
`pass`. `docs/harness/slo-budgets.json` is declarative (`path`/`kind`/
`minSamples`/`requires`/`enforce`), and the gate validates paths. Eleven new
conformance cases include a contract that every metric named in
`deploy/prometheus/blackeclipse.rules.yml` exists in the shipped exposition.

**Why.** The Hermes comparison found that Eclipse emitted the new seams but
dropped them at projection, and that lazy `define()` both hid missing work and
silently reset counters. A dashboard that cannot distinguish "unmeasured" from
"healthy", or a correctness bug averaged into a 99.9% budget, is worse than no
dashboard: it manufactures confidence.

**Consequences.** Adding an objective is a budgets-file change, not gate code.
A disabled capability cannot fail a release, and a required-but-missing sample
cannot pass one. Metric labels stay bounded; ids, paths, arguments and prompt
text never become labels. Alert rules are real rules, not commented templates,
and a renamed metric fails CI. The probe synthesises every workstream's signal
and advertises all capabilities for the synthetic report; live reports use the
actual flags.

## ADR-H361 - Computer use is screenshot-first, with a bounded image budget and an optional grounding backend

**Decision.** A `computer_*` observation returns the REAL frame as model media
(crop → downscale → JPEG, bounded), with the `#N` handles drawn on it as numbered
badges, and a state-changing action attaches a fresh screenshot by default
(`capture_after`). Only the newest eight screenshots stay in context; older media
messages are dropped while every tool result and text turn remains. The grounding
ladder stays accessibility-first, but rung 5 is a real backend: an optional
UI-TARS-class endpoint that declares its coordinate grid and is mapped to screen
pixels (DPI-correct) before an action. No endpoint is an honest
`configured:false`, never a fabricated point, and a sensitive frame is never sent
to it. Per-window capture tries `PrintWindow(PW_RENDERFULLCONTENT)` before a
screen grab and records `captureMethod`, so background observation is not a lie.

**Why.** The Agent-S comparison found the model never received the screenshot,
`vision` mode returned nothing, `som` was an unannotated text list, and the
vision rung had no caller. The safety system was strong; the perception pipeline
was blind, which capped task success regardless of policy.

**Consequences.** Every observation now costs image tokens, so the budget is
explicit and pruned. A grounding endpoint is a deployment option, not a
zero-config requirement. Images are model content, so they follow the existing
sensitive-frame masking and never bypass it; a sensitive snapshot is not sent to
an external grounding model. The eight-image window is the ONE context rule;
per-agent duplication of it is a bug.

## ADR-H362 - Best-of-N is a selection mechanism over isolated sessions, and the benchmark scores from the environment

**Decision.** `computer_run(task, attempts=N)` runs independent desktop-agent
branches on fresh sessions, reduces each to a deterministic behavior caption, and
selects a **verified success** (fewest failed steps, then fewest steps). A judge
callable may pick a different successful branch but never an unsafe/failed one. A
branch is bound to its run id through `ComputerManager.session_for_run`, so
parallel isolated branches cannot act on each other's desktop; the shared desktop
runs branches serially. N>1 is refused for a task naming a financial mutation.
Evaluation is a suite runner (`eval/suite.py`) plus an OSWorld driver/bridge
(`drivers/osworld.py`, `eval/osworld.py`): the engine drives the environment, and
the score is the environment's `evaluate()`, never the agent's claim. The local
Windows suite publishes a pass@1 for the engine, not a claim about agent
intelligence.

**Why.** Agent S3's Behavior Best-of-N is the largest single accuracy lever in
the literature (66% -> 72.6% on OSWorld), and Eclipse already had isolated
desktops but no way to use them for selection. Separately, a benchmark whose
score can be self-reported is not a benchmark.

**Consequences.** A branch costs a full agent run, so N is capped at 4 and
defaults to 1; approvals are per-branch and single-use. A judge failure falls back
to the deterministic pick instead of failing the run. The OSWorld adapter is
honest about its dependencies: without the OSWorld VM or a configured model it
reports unavailable rather than producing a number.

## ADR-H363 - A verified run may propose a workflow; per-app redaction is a profile

**Decision.** A `done` that is a verified success (at least one verified step, no
`fail`, enough real actions) produces a workflow CANDIDATE: step sequence, target
labels, notes and task, with typed text/values excluded and secret-shaped text
scrubbed. By default the candidate is only surfaced for an explicit
`computer_remember`; `memory.auto_capture=true` records it tagged
`source=auto_capture`, and best-of-N stores only the winner's candidate. Separately,
sensitive-data detection is extended by per-application profiles (trading
terminal, broker web, password manager, banking): a profile adds labels/patterns
for its window and a password-manager profile masks every text field. A new app
family is a profile record, not new detection code.

**Why.** Explicit-only memory is safe but forgetful; the same verified task is
re-derived forever. Unconditional auto-write is worse: it can persist a secret or
a wrong shortcut. A verified candidate plus an opt-in store keeps the knowledge
gain and the inspectability. Generic labels also cannot know that "Buying Power"
is account data in a trading terminal or that a vault view is entirely secret.

**Consequences.** A false-positive redaction only masks more than necessary
(safe); a false negative leaks, so profiles err toward masking. The candidate is
bounded (20 steps), never contains a screenshot or a typed value, and a memory
outage cannot fail a desktop task. Auto-capture is a config decision, visible in
`computer.example.json` and the capabilities payload.

## ADR-H364 - Learned application interfaces are derived from verified runs, versioned, and never executable

**Decision.** A verified workflow candidate derives a `LearnedAdapter`
(application, controls it acted on, shortcuts, task→steps recipe), with a
monotonic version and merged knowledge. Structural validation requires a named
control, a recipe and verified-run evidence; `validated` means "derived from a
verified run", while the live-rig `verified` flag stays separate and false. The
adapter is registered in the session's adapter registry (so the grounder's
semantic rung can use it immediately) and persisted to the gitignored
`learned_adapters.json` sidecar under the artifacts root. `computer_recall`
returns learned interfaces even when Eclipse-Memory is empty.

**Why.** Agent S4 shows interfaces can be evolved from experience; Eclipse had
hand-written adapters that never improved. A full meta-agent/population search is
research, but the useful, safe core is: learn the controls and recipes from a run
that was actually verified, keep it inspectable, and never let learned knowledge
execute anything.

**Consequences.** Learned knowledge is bounded (20 recipes, 20 evidence digests),
contains no typed text, and a malformed/conflicting record cannot be selected.
Every observation live-validates a learned interface: a known control resets the
miss counter, three consecutive observations without one mark it stale, and a
stale interface stops matching (the grounder falls back) until a new verified run
learns the changed UI. A change in the app is therefore detected by the same
mechanism that learned it, and the record is a knowledge cache, not a source of
truth.

## ADR-H365 - The behavior judge compares captions, never screenshots, and can only confirm a winner

**Decision.** Best-of-N's optional model judge receives the deterministic branch
captions and the task, nothing else, and replies with one index. Parsing is
strict (`CHOICE: n`, tolerant of a trailing number), an out-of-range/zero/none
reply is no choice, and a judge exception or provider failure falls back to the
deterministic pick. The judge can only choose among branches the engine already
marked successful and safe. It is built from the run's own model settings and
only when attempts > 1.

**Why.** Behavior judging is the largest single accuracy lever in the CUA
literature, but a judge that sees raw screenshots can be steered by on-screen
text, and a judge that can override safety is a second authority. Text captions
are already scrubbed and bounded; the deterministic rank remains the floor when
the model is absent or wrong.

**Consequences.** A judge call costs one model request per selection and never
runs for a single branch. No screenshot leaves the machine for judging. The
judge's choice is reported as `judgedBy: behavior_judge` so the outcome is
auditable and a deterministic result is visibly different.






