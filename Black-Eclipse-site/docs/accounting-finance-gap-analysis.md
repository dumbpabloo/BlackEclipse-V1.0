---
title: Accounting & Finance Gap Analysis
status: Analysis
last-verified: 2026-11-26
---

# Accounting & Finance Gap Analysis

**Who this is for:** product/engineering planning for Eclipse's money surfaces.
This is an *analysis and proposal*, not a description of how the system works.
The canonical behaviour lives in
[ledger.md](./ledger.md),
[portfolio-accounting.md](./portfolio-accounting.md),
[journal-broker-sync.md](./journal-broker-sync.md) and
[harness/financial-analysis-engine.md](./harness/financial-analysis-engine.md).

## Method

Read against source on 2026-11-26 (not against the docs alone):

- `python/harness/ledger/` (30 modules: journal, statements, imports, reconcile,
  schedules, documents, tax, consolidation, exports)
- `python/harness/portfolio/` (28 modules: accounting, lots, valuation, risk,
  construction, attribution, reconciliation)
- `src/components/screens/portfolio/` + `src/lib/portfolio/` + `src/hooks/use-portfolio-engine.ts`
- `src/lib/journal/broker/`, `python/harness/trade/`
- `python/harness/finance_guardrails/`, `python/harness/graph_runtime/`,
  `python/harness/automation/`
- `docs/ledger.md`, `docs/portfolio-accounting.md`,
  `docs/accounting-hardcore-tests.md`, `docs/journal-broker-sync.md`

Severity below means **impact on trust or usefulness**, not effort:

- **P0** — a number the user sees can disagree with the book of record, or audit
  evidence is missing.
- **P1** — a finance capability a normal user expects is absent.
- **P2** — workflow, discoverability, scale.

## What already exists (do not rebuild)

The foundations are unusually strong and should be preserved:

- A real double-entry GL with balanced postings, reversals-only corrections,
  periods (OPEN/CLOSED/LOCKED), derived statements with checks, suspense,
  deterministic import/categorisation, reconciliation, schedules, FX
  revaluation, close checklist, AR/AP with aging, configured tax codes,
  proportionate consolidation, and text exports.
- A real securities book with FIFO/LIFO/AVERAGE_COST/HIFO/SPECIFIC_ID lots,
  corporate actions, crypto lifecycle, Decimal money, data-quality states,
  performance/risk/attribution/construction, proposals-only rebalancing.
- A broker↔journal projection with idempotent fills, ownership split, backfill
  and honest `UNAVAILABLE` states.
- Money guardrails and a single-use, digest-bound approval layer above every
  mutation.

The gaps are therefore mostly **integration, reporting, and the last mile** —
not missing accounting math.

---

## P0 — Correctness and trust

### P0-1. The Funds UI recomputes accounting instead of reading the engine

**Evidence**

- `src/components/screens/portfolio/use-fund-data.ts:4` — "Wires the pure
  engines: usePortfolioEngine (FIFO/LIFO)".
- `src/hooks/use-portfolio-engine.ts:98` — a second lot engine in TypeScript.
- `src/components/screens/portfolio/fund-manager.tsx:118` —
  `computePortfolioStats(f.transactions, live, 'FIFO')`.
- `docs/portfolio-accounting.md` — the Python engine is the authoritative book.

**Impact.** Positions, cost basis, realized P&L, cash and dividends rendered in
the UI are produced by a JS FIFO/LIFO approximation, while the agent tools,
HTTP routes and hardcore tests use the Python engine. The two can disagree on
exactly the cases the engine was hardened for: HIFO, specific-lot designation,
splits, spin-offs, mergers, return of capital, crypto swaps, staking rewards,
slashing, funding, fees, taxes. The user sees one number; the agent sees another.

**Recommendation.** Make the UI a *reader* of the engine's derived outputs:

1. Add a single `/api/portfolio/{id}/query` read into the fund data hook
   (positions, cash, realized/unrealized P&L, lots, transactions).
2. Keep the client engine only as an explicit "preview before save" path, and
   label it as such.
3. Delete or demote `computePortfolioStats` from the rendering path; add a
   parity test that compares client output against a fixed engine fixture.

This is the single highest-value change in the analysis.

### P0-2. The UI adapter silently drops transaction types

**Evidence.** `src/lib/portfolio/engine-funds.ts:60-84`

```ts
const TX_ALIASES = { TRANSFER_IN: 'BUY', TRANSFER_OUT: 'SELL' };
if (!['BUY','SELL','DEPOSIT','WITHDRAWAL','DIVIDEND'].includes(type)) return null;
```

**Impact.** The following engine rows never reach the UI: `SPLIT`,
`REVERSE_SPLIT`, `FEE`, `TAX`, `INTEREST`, `SPIN_OFF`, `MERGER`,
`SYMBOL_CHANGE`, `COST_BASIS_ADJUSTMENT`, `RETURN_OF_CAPITAL`, `RIGHTS_ISSUE`,
`WARRANT_EXERCISE`, `REALIZED_ADJUSTMENT`, `SWAP`, `STAKING_REWARD`, `AIRDROP`,
`BRIDGE`, `GAS_FEE`, `STAKE`, `UNSTAKE`, `SLASHING`, `FUNDING`. A split is
invisible, so displayed quantity and cost per unit are wrong after it. Fees and
taxes (the two fields most likely to change realized P&L) disappear.

**Recommendation.** Either stop mapping transactions into the legacy `Fund`
shape and render engine rows directly, or carry every type through and teach the
UI how to display each. At minimum, never silently drop a row: show an
"unrepresented transaction" warning with the type and id.

### P0-3. Cost-basis method is not honoured or persisted in the UI

**Evidence**

- `src/components/screens/portfolio/fund-workspace.tsx:31` initialises
  `taxMethod` to FIFO/LIFO only.
- Line 68 toggles local state; it does not call the engine's `PATCH`/policy.
- `src/lib/portfolio/types.ts:30` only allows `FIFO | LIFO | AVERAGE_COST`.
- The engine supports `HIFO` and `SPECIFIC_ID` with `lot_selection`.

**Impact.** A fund configured as HIFO or SPECIFIC_ID in the engine is displayed
as FIFO/LIFO. The toggle looks like it changes accounting but does not change
the book, and lot selection is impossible from the UI.

**Recommendation.** Read the method from the portfolio record, make the control
a real persisted setting (engine `cost_basis_method`), expose HIFO and
specific-lot, and show the selected lot(s) for a sale. Reuse the engine's
`portfolio_calculate_tax_lots` result rather than recomputing.

### P0-4. No realized-gains / tax-lot report a user can hand to an accountant — **implemented 2026-11-26**

**Evidence.** The engine exposes `portfolio_calculate_tax_lots` and lot matches;
the UI has no tax-lots widget (`FUND_WIDGETS`, `widgets.tsx:916-929`) and there
is no realized-gains-by-year export. `harvest.py` is proposal-only and wash-sale
rules are explicitly not modelled (`docs/portfolio-accounting.md`).

**Impact.** The hardest accounting problem (what did I realize, in which year,
against which lot, with what holding period) is computed but not reportable.
Users still need a spreadsheet for tax time.

**Recommendation.** Add a realized-gains report (per disposal: symbol, lot id,
acquired date, disposed date, holding period, proceeds, basis, gain, short/long)
with CSV export, grouped by tax year and by method. Add an optional,
caller-configured wash-sale adjustment (jurisdiction-neutral red flag, never a
silent rewrite). Surface it in the UI as a Tax Lots / Realized Gains widget.

### P0-5. Financial history is append-only but not tamper-evident — **implemented 2026-11-26**

**Evidence.** `python/harness/ledger/store.py` is append-only; corrections are
reversals. But there is no hash chain or periodic digest. A hash-chained ledger
already exists elsewhere (`python/harness/governance/ledger.py`,
`ChainedLedger.verify()`), just not for money records.

**Impact.** "History was never rewritten" is true at the API layer, not provable
against a database edit. For accounting that is the difference between a book
and a claim.

**Recommendation.** Reuse `ChainedLedger` (or the same pattern) for
`LedgerAuditEvent` and `PortfolioAuditEvent`: each event hashes
`seq + prev + canonical(payload)`. Add a verify endpoint/tool that names the
first broken record. No new dependency.

### P0-6. No source-document attachments on postings — **implemented 2026-11-26**

**Evidence.** No attachment/evidence field in the ledger package; `documents.py`
is AR/AP invoices, not scanned receipts. The close checklist can warn "missing
receipt" (`close.py:147`) but cannot link one.

**Impact.** Audit trail stops at the arithmetic. A user cannot attach the bank
statement, invoice PDF or receipt to the entry it supports.

**Recommendation.** Add an artifact reference on journal entries/lines
(artifact id + content hash), mounted through the existing artifact store, and
expose it in the entry read. Do not build a second file store.

### P0-7. No segregation of duties — **implemented 2026-11-26**

**Evidence.** Every mutation is admitted by one digest-bound approval
(`ledger/authorizers.py`, `portfolio/authorizers.py`). There is no poster vs
approver role, no four-eyes rule, no amount-tiered authority.

**Impact.** Fine for a single-user local app; a blocker for any team or company
use. The same actor can prepare and approve a posting/payment.

**Implemented.** `harness/sod.py` adds per-effect policies (threshold +
required distinct approvers + requester exclusion), stored on `agent-memory.db`.
The ledger and portfolio authorizers consult it before the existing single-use
approval, so both gates apply; with no policy configured the path is unchanged.
Operator-only `/sod/*` routes configure policies and record sign-offs (there is
no agent tool to sign off), keyed to the exact call digest and consumed on
authorization. Tests: `test_sod.py` (unit + both authorizers + routes).

---

## P1 — Missing finance capabilities

### P1-1. No year-end close / retained-earnings roll-forward — **implemented 2026-11-26**

Current earnings are a derived equity line; there is no closing entry that
transfers them to retained earnings (`docs/ledger.md` known limits). A period
close also does not stop later postings into it (that is what `LOCKED` is for).

**Implemented.** `ledger_year_end_preview` / `ledger_close_fiscal_year`
(`GET /ledger/{id}/year-end/preview`, `POST …/year-end/close`) post a real
closing entry dated the first day of the next fiscal year, zero revenue/expense
and roll the net into the active `EQUITY/RETAINED` account. It is idempotent
(`source=year_end_close`, `externalId=ye:<yearEnd>`; a retry is
`ALREADY_CLOSED`), P&L readers omit closing entries, and the balance sheet /
trial balance include them. A correction is a reversal. Tests:
`test_ledger_year_end.py` + route coverage.

### P1-2. No budgets, forecasts, variance or dimensions — **implemented 2026-11-26**

No `budget`, `forecast`, `variance`, `cost center`, `department`, `project` or
`class` concept exists in `python/harness/ledger/`.

**Impact.** The GL can prove history but cannot answer "are we over budget",
"what is this project's P&L", or "what is our runway". It is bookkeeping, not
management accounting.

**Implemented.** Dimensions were already stored on every journal line;
`ledger_dimension_summary` / `GET /ledger/{id}/dimensions/{dimension}` now
groups movement by them (cash lines excluded, unassigned explicit). `LedgerBudget`
+ `LedgerBudgetLine` and `ledger_budget_create/list/variance` add budgets by
account + dimension, with most-specific-match actuals, favourable direction and
an explicit `unbudgeted` bucket. `ledger_cash_runway` / `GET /ledger/{id}/runway`
adds trailing operating burn + open AR/AP and reports an `UNKNOWN` runway for a
surplus rather than infinity. Tests: `test_ledger_budgeting.py` + route coverage.

### P1-3. Cash flow is direct-method only — **implemented 2026-11-26**

`statements.py:285-365` classifies cash movements into operating/investing/
financing via `cash_flow_category`. There is no indirect-method reconciliation
(net income → operating cash flow adjustments).

**Implemented.** `statements.cash_flow_indirect` derives net income,
depreciation/amortisation add-backs and operating working-capital movements
(category-driven), then reconciles to the direct operating bucket.
`ledger_cash_flow` / `GET /ledger/{id}/cash-flow` accept `method=indirect`; a
difference (e.g. non-cash investing on credit) is returned with `reconciled:
false`, never plugged. Tests: `test_ledger_cash_flow_indirect.py` + the route
test in `test_ledger_api.py`.

### P1-4. "Fund" has no fund administration — **closed by product decision 2026-11-26: keep the portfolio-book surface**

The product calls portfolios **funds**, but there is no NAV per unit,
subscription/redemption, management fee, performance fee/hurdle/waterfall,
capital account, investor registry or equalisation.

**Impact.** The word "fund" promises fund accounting. A real fund user cannot
answer "what is my unit price", "what fee did I accrue", or "what does each
investor own".

**Implemented (per-account custody).** `PortfolioAccount` is now wired:
account CRUD, `account_id`-aware cash, per-account open positions/lots, an
explicit `Unassigned` bucket, account-scoped tax lots, and account-scoped
oversell protection. `read` returns `accounts` and reconciles
`accountCashTotal` to aggregate cash. Routes/tools: `…/accounts`, four
`portfolio_*_account` tools; tests: `test_portfolio_accounts.py` + route
coverage.

**Product decision (2026-11-26): keep the portfolio-book surface.** Real fund
administration (NAV per unit, subscriptions/redemptions, management/performance
fees, high-water mark, capital accounts, investor registry) is deliberately
**not** built. The docs state that "fund" means a portfolio book with custody
accounts, so the surface does not imply a regulated fund administrator. A real
unit ledger would be a separate, explicit layer if the product direction changes.

### P1-5. No derivatives, shorts or margin

Shorts are refused (`docs/portfolio-accounting.md`); options/futures are carried
at cost unless a gateway price exists (`_mark`), with no Greeks, expiry, roll,
margin or contract multiplier. Crypto `FUNDING` is the only derivative-like
event.

**Recommendation.** Scope explicitly. If derivatives are in scope, add a
contract model (multiplier, expiry, strike, right), mark-to-market, margin
balance, and expiry/assignment corporate actions. If not, say so in the UI so a
user does not import an option and get a cost-carried number silently.

### P1-6. No fixed-income accounting

`DIVIDEND`/`INTEREST` are cash events. There is no accrued-interest accrual,
premium/discount amortization, yield-to-maturity, duration or clean/dirty price.

**Recommendation.** For bonds held as positions, add accrual and amortization as
ledger adjustments (`COST_BASIS_ADJUSTMENT` already exists) and report YTM/
duration in analytics. Until then, label bond positions as "price-only".

### P1-7. Dividend and withholding-tax treatment is cash-only

Dividends are recorded as cash (`service.py:475-570`); there is no ex-date/record
date accrual, no withholding tax, no foreign-tax credit, no dividend forecast.

**Recommendation.** Add optional `withholding_tax` on dividend events (a
separate cash outflow and a tax-lot/report line), and a receivable accrual
between ex-date and pay-date if the user wants accrual accounting.

### P1-8. Multi-currency is single-functional-currency

- Ledger: one functional currency per book; documents must be base currency;
  consolidation uses one rate per currency for all accounts; no translation
  reserve (`docs/ledger.md` known limits).
- Portfolio: base currency, per-line FX, but cash is effectively one bucket; the
  `PortfolioAccount` table exists but is barely used (no account listing tool,
  no per-account cash).

**Impact.** A company with EUR revenue and USD reporting cannot produce a proper
translation reserve or revalue monetary balances; a user with accounts at two
brokers cannot separate cash/lots by account.

**Recommendation.** Prioritise (a) per-account cash/lots in the portfolio
(existing table), then (b) ledger translation reserve and monetary/non-monetary
revaluation. Do not fake a multi-currency P&L with one blended rate.

### P1-9. No live bank feeds and no statement-document ingestion

Imports are declared CSV/OFX/QIF only, one account per file
(`docs/ledger.md`). PDF statements are not parsed into the ledger even though a
document-intelligence extractor exists.

**Recommendation.** Add a PDF/CSV "statement intake" that produces a *preview*
(never auto-posts), reusing the existing preview/commit pipeline and the
suspense/categorisation rules. Live bank APIs are a larger, jurisdiction-heavy
project; keep them optional adapters behind the same import contract.

### P1-10. Recurring documents and AR/AP depth

Missing: credit notes, dunning, recurring invoices, purchase orders, per-party
credit limits, prepayments/deposits, bad-debt write-off workflow. Aging exists.

**Recommendation.** Add recurring invoice/bill templates (a schedule kind) and
credit notes as reversing documents; both fit the existing "document = validated
batch of lines" contract. Credit limits are a report/blocker, not a new ledger.

### P1-11. No inventory, payroll or fixed-asset register

Depreciation/amortisation schedules exist, but no asset register (acquisition,
disposal, gain/loss, net book value), no inventory costing, no payroll.

**Recommendation.** A fixed-asset register is the cheapest high-value add: link
acquisitions to accounts, run the existing schedule, handle disposal as a real
posting with gain/loss. Payroll and inventory are large; keep out of scope until
demand is proven.

### P1-12. No tax engine

Configured tax codes only; no returns, filings, VAT/GST return, deferred tax,
withholding or jurisdiction tables. This is deliberate and correct (no invented
rates), but it stops short of filing.

**Recommendation.** Add a tax *return report* (outputs from postings grouped by
tax code/jurisdiction/period) and an importable jurisdiction pack, still
user-supplied. Never bundle a rate that goes stale silently.

### P1-13. Portfolio has no statement import; broker sync stops at the journal

`reconciliation.py` accepts a caller-built statement. There is no CSV/OFX broker
import into the portfolio, and `journal-broker-sync` projects fills into
`jdds.db` for the journal only. Ledger/portfolio never receive broker activity
automatically.

**Recommendation.** Add a broker-statement adapter into the portfolio's
transaction ledger (idempotent on `(source, externalId)`, already supported),
then offer "reconcile → propose corrections" rather than auto-posting. A single
broker feed should be able to populate journal, portfolio and (optionally) GL.

### P1-14. The three books are not connected

GL (company), portfolio (securities) and trade journal are intentionally
separate, which is correct. But there is no consolidated view:
no net worth across personal + company books, no "fund distribution → GL",
no single FX source (ledger needs caller rates; portfolio uses the gateway).

**Recommendation.** Add a read-only **Finance Overview** that composes:
cash + AR/AP + close status from the GL; value/P&L from the portfolio; realized
P&L/edge from the journal. State clearly which book each number comes from and
never net company and personal money without a label.

### P1-15. No indirect transfer/split automation in import

A transfer between two bank accounts is two rows that each need a category; no
inter-account matching or automatic split rules (`docs/ledger.md`).

**Recommendation.** Add a deterministic "transfer pair" rule (same amount/date
within tolerance, opposite sign, two cash accounts) that proposes a single
journal entry in preview. Reuse reconciliation matching; never auto-commit.

---

## P2 — Workflow, surfaces and scale

### P2-1. The General Ledger has no UI at all

Chat-only is a deliberate decision (`docs/ledger.md`). It keeps one engine, but
it hides the capability: there is no nav entry, no statement viewer, no invoice
form, no close checklist, no aging table. Non-technical finance users will not
discover it.

**Recommendation.** Keep the engine authoritative, then add a thin **read-only**
GL surface (trial balance, statements, close status, AR/AP aging) plus an
entry/invoice composer that calls the same tools. If chat-only is a hard product
rule, at minimum add a discoverable "Bookkeeper" entry that seeds the preset and
a statement artifact export.

### P2-2. The portfolio UI omits the engine's most valuable outputs

Missing widgets: tax lots, realized gains, dividends/income, cash accounts,
reconciliation status, audit/transactions detail (the "ledger" widget is a
transaction list, not the GL). `FUND_WIDGETS` has 14 widgets, none of them tax
or reconciliation.

**Recommendation.** Add Tax Lots, Realized Gains, Income and Reconciliation
widgets driven by engine reads (after P0-1). These are high value and low risk.

### P2-3. No finance home / management dashboard

No surface answers "cash today, what is overdue, what is unclosed, what did I
make, what is my runway" in one place.

**Recommendation.** A Finance Overview built from existing reads (GL statements/
aging/close, portfolio value/P&L, journal edge). No new engine.

### P2-4. No ledger graph nodes or scheduled close

`docs/ledger.md` lists `ledger.post` / `ledger.close` graph nodes as still open.
Only three ledger monitors exist (`suspense`, `blockers`, `open_ar`), created
switched off.

**Recommendation.** Add `ledger.post` and `ledger.close` nodes to
`graph_runtime`, and expand the monitor catalogue with `ar_overdue`,
`cash_runway_below`, `budget_variance_over`, `reconciliation_stale` and
`fx_unrevalued`. These reuse `LedgerFactProvider`.

### P2-5. No reporting/statement pack tied to the books

`ledger_export` returns text (journal/trial-balance CSV, JSON, XBRL without a
taxonomy, SAF-T refused). There is no PDF/XLSX statement pack. Artifact skills
can render documents but are not wired to ledger payloads.

**Recommendation.** Add a "statement pack" report builder that hands ledger
payloads to the existing artifact/report skill (PDF/XLSX). Keep XBRL honest:
still not a filing.

### P2-6. Onboarding and data entry are weak

No chart-of-accounts wizard beyond templates, no opening-balance importer, no
mobile expense capture, no receipt OCR → draft entry, no bulk review queue for
approvals.

**Recommendation.** Add an opening-balances import (a journal preview), a
receipt → draft entry flow through the document extractor, and a single approval
inbox. These are the adoption blockers for a real bookkeeping user.

### P2-7. Client-side FX fallback is a hardcoded table

`use-portfolio-engine.ts:12-39` carries approximate USD rates (PKR 1/278, etc.)
used when live FX is missing. It is flagged `fxMissing`, but an approximate
number still renders as a value.

**Recommendation.** After P0-1 this disappears for engine-backed reads. If the
preview path stays, render `UNAVAILABLE` instead of a fallback rate, or label it
"estimated" inline on every affected figure.

### P2-8. Scale and operations are single-instance by design

SQLite on one connection, statements recomputed on every read, in-process rate
limits. Fine for a desktop app; a multi-user or high-volume book will need
snapshots/indexes and an explicit concurrency story.

**Recommendation.** Measure before changing: add a ledger/portfolio benchmark
(entries vs statement latency) and only then add materialised statement caches
with a content hash. Do not cache totals without an invalidation contract.

---

## Implementation status

### 2026-11-26 — Phase 0 core + first Phase 1 report

Done in source (see `docs/portfolio-accounting.md` for the contract):

- **P0-1 (UI reads the engine).** `src/lib/portfolio/engine-stats.ts` maps the
  engine read into the widget stats; `use-fund-data.ts` uses it for the
  workspace and `use-engine-fund-stats.ts` for the fund list. The client lot
  engine remains only as a labelled `PREVIEW` fallback.
- **P0-2 (transaction fidelity).** `src/lib/portfolio/engine-transactions.ts`
  preserves all engine types; the old five-type filter and the
  `TRANSFER_IN → BUY` / `TRANSFER_OUT → SELL` relabelling are gone.
  Tests: `engine-transactions.test.ts`.
- **P0-3 (cost basis is real).** The workspace control persists
  `cost_basis_method` through `PATCH /portfolio/{id}` and re-reads the engine;
  FIFO/LIFO/AVERAGE_COST/HIFO/SPECIFIC_ID are selectable.
- **P0-4 (complete).** A **Tax Lots & Realized Gains** widget reads
  `GET /portfolio/{id}/tax-lots` and exports realized matches as CSV.
  `portfolio/tax_report.py` adds a tax-year/currency/short-long report
  (`portfolio_realized_gains`, `GET /portfolio/{id}/realized-gains`) with
  wash-sale **candidates** over a caller-supplied window; it never disallows a
  loss or adjusts basis. Jurisdiction filings remain out of scope by design.
- **Parity guard.** `engine-stats.test.ts` pins that engine realized P&L is
  shown verbatim even when a trade replay would disagree, and that allocations
  use invested value with cash excluded.
- **P0-6 (source-document attachments).** `LedgerAttachment` stores an artifact
  id plus the content hash frozen at attach time; bytes stay in the ONE artifact
  store. `ledger_attach_document` / `ledger_list_attachments` /
  `ledger_detach_document` / `ledger_verify_attachment` plus
  `GET|POST …/attachments` expose it, `get_entry` returns `attachments`, and
  verify reports `MATCHED` / `CHANGED` / `MISSING`. `test_ledger_attachments.py`
  covers attach, hash verification, changed/deleted artifacts, duplicate and
  cross-ledger refusal.
- **P0-5 (tamper-evident history).** `python/harness/audit_chain.py` seals every
  `LedgerAuditEvent` and `PortfolioAuditEvent` with SHA-256 over the previous
  hash + the event's immutable fields. Legacy rows are sealed on store open
  (metadata only). New read surfaces: `ledger_audit_verify`,
  `portfolio_audit_log`, `portfolio_audit_verify` and
  `GET …/audit/verify`. `test_audit_chain.py` proves clean verification, edited
  row detection and deletion detection for both books.

Still open from Phase 0: a fully automated client↔Python parity fixture in CI
(the current tests pin the adapter, not a live engine response).

## Prioritised roadmap

**Phase 0 — make the UI tell the truth (highest ROI)**

1. Fund screen reads engine outputs; client engine becomes preview-only (P0-1). ✅
2. Full transaction-type fidelity or explicit "unrepresented" warnings (P0-2). ✅
3. Persist and honour cost-basis method, including HIFO/SPECIFIC_ID (P0-3). ✅
4. Parity test: fixed fixture → client vs Python must match (or client path gone). ◐

**Phase 1 — reportable accounting**

5. Realized gains + tax lots report and CSV export (P0-4). ✅ (widget + CSV + tax-year/currency/term report + wash-sale candidates)
6. Statement pack (PDF/XLSX) from ledger payloads (P2-5).
7. Hash-chained audit events + verify (P0-5).
8. Entry attachments/evidence (P0-6).

**Phase 2 — close, controls, management**

9. Year-end closing entries / retained earnings (P1-1). ✅
10. Dimensions + budget vs actual + runway forecast (P1-2). ✅
11. Indirect cash flow (P1-3). ✅
12. Approval policy / segregation of duties (P0-7). ✅
13. Ledger graph nodes + expanded monitors (P2-4).

**Phase 3 — product expansion (decision-gated)**

14. Fund administration (NAV/unit, fees, capital accounts) **or** rename the
    surface honestly (P1-4). Per-account cash/lots ✅; NAV/unit is decision-gated.
15. Per-account portfolio cash/lots (P1-8).
16. Fixed assets register (P1-11), then recurring invoices/credit notes (P1-10).
17. Statement intake (PDF/CSV) and broker import into portfolio (P1-9, P1-13).

**Explicit non-goals until a product decision:** short positions, derivatives
pricing/margin, payroll, inventory costing, bank API integrations, bundled tax
jurisdiction tables.

## Design rules to keep

These decisions are good and should not be eroded while closing gaps:

- Ledger and portfolio stay separate books; a "Finance Overview" reads both,
  never merges their arithmetic.
- History is append-only; corrections are reversals/adjustments.
- Missing price/FX/rate is `UNAVAILABLE`/`MISSING_FX`, never zero or 1.0.
- Suspense is visible and blocks a close; no account is guessed.
- Tax rates are configured data; no bundled rate table.
- Rebalancing/construction is proposal-only; nothing places a trade.
- One approval mechanism; new controls compose with it rather than bypassing it.

## Open questions for the product owner

1. Is "fund" a personal portfolio or a real investment fund? This changes
   Phase 3 entirely.
2. Is chat-only GL a hard rule, or may a read-only statement surface exist?
3. Are derivatives/shorts in scope, or should the UI explicitly reject them?
4. Is multi-user (roles, segregation of duties) required, or is this a
   single-operator desktop product?
5. Which tax jurisdictions are real targets (for a *user-supplied* pack), if any?
