---
title: General Ledger (double-entry book of record)
status: Current
last-verified: 2026-11-26
---

# General Ledger

**Who this is for:** anyone posting an entry, reading a statement, adding a
ledger tool, or reasoning about why a number in the books is what it is.

The General Ledger keeps a **company's books**: what it earned, owed, owned and
spent, provable from the entries. It is deliberately separate from the
[Portfolio Accounting Engine](portfolio-accounting.md), which keeps a
*securities* book (positions, cost basis, valuation, performance). Two questions,
two books; neither is derived from the other.

```
Chart of accounts ──▶ Journal entries ──▶ Postings ──▶ Trial balance
                                            ├────────▶ Balance sheet
                                            ├────────▶ Income statement
                                            └────────▶ Cash flow
```

## Where it lives

- **Runtime:** `python/harness/ledger/` — money, contracts, posting rules,
  periods, statements, store, service, Phase 2's `importers.py` (CSV/OFX/QIF
  parsers), `rules.py` (categorisation), `imports.py` (the import pipeline) and
  `reconcile.py`, Phase 3's `schedules.py` (depreciation/amortisation/
  accruals), `revaluation.py` (period-end FX) and `close.py` (the close
  checklist), and Phase 4's `parties.py`, `tax.py`, `documents.py` (AR/AP
  invoices, bills, payments, aging), `subledger_store.py` and
  `document_tools.py`, plus Phase 5's `group_store.py`, `consolidation.py`,
  `exports.py` and `group_tools.py`.
- **Agent preset:** the closed `bookkeeper` preset in `python/harness/agents.py`.
  **There is no dedicated tab or browser client: the ledger is used in chat.**
- **Persistence:** the ONE `agent-memory.db` connection (`ledger/store.py`).
  Books are private user state; they are never market data and never committed.
- **Agent tools:** `python/harness/ledger/tools.py` — the flat `ledger_*` family
  on the shared `ToolRegistry`.
- **HTTP:** `python/harness/routers/ledger.py` (`/ledger/*`, operator-gated).
- **Boot:** `python/harness/ledger/bootstrap.py`, called from
  `AgenticService.__init__`, exposed to each run as `LedgerBundle`.
- **Tests:** `python/tests/test_ledger_{core,statements,imports,reconcile,tools,authorizer,api}.py`,
  plus the cross-book hardcore suite `python/tests/test_accounting_hardcore.py`
  (prompts: `docs/accounting-hardcore-tests.md`).

## The five invariants

Everything else follows from these. If a change weakens one, it is a bug.

1. **A posting balances exactly.** `Σ base debit == Σ base credit`, compared as
   `Decimal`. A one-cent difference is a refusal (`UNBALANCED_ENTRY`) carrying
   the exact difference — never rounded away, never auto-plugged.
2. **A posted entry is never mutated.** There is no update path. A correction is
   a *reversing entry* that links to the original
   (`reversesEntryId` ↔ `reversedByEntryId`). Both stay in the book, so the
   audit trail is the arithmetic.
3. **Statements are derived on every read.** No cached total, no "last known"
   balance. `trial_balance` / `balance_sheet` / `income_statement` / `cash_flow`
   recompute from postings and return a `checks` list.
4. **A period is a lock.** `OPEN` accepts postings; `CLOSED` refuses them until
   an operator reopens; `LOCKED` refuses forever. Nothing writes into a closed
   month by accident.
5. **No invented numbers.** A cash movement the engine cannot classify lands in
   an explicit `unclassified` bucket and sets `complete: false`. A missing FX
   rate is `MISSING_FX`. A non-zero suspense balance blocks a close
   (`PERIOD_NOT_READY`).

### Tamper-evident audit chain

Append-only is enforced by the store; the hash chain proves it. Every
`LedgerAuditEvent` is sealed with SHA-256 over the previous event's hash plus
the event's immutable fields (`python/harness/audit_chain.py`).

`ledger_audit_verify` (tool) and `GET /ledger/{id}/audit/verify` (route)
recompute the chain in append order and return `status`:

| status | meaning |
|---|---|
| `OK` | every sealed event hashes correctly and links to its predecessor |
| `UNSEALED` | historical rows predate the chain; the sealed portion is not claimed as whole |
| `BROKEN` | the first edited, reordered or deleted event, with `brokenAt` and `HASH_MISMATCH` / `PREV_HASH_MISMATCH` |

Rows written before the chain existed are sealed on store open (metadata only;
event content is never changed). A deleted event makes the next event's
`prevHash` disagree, so deletion is detected as a broken link even when the
surviving hashes are internally valid.

### Source-document attachments

A posted entry can carry the receipt, invoice or bank statement that supports
it. The bytes are **not** copied into the ledger: the attachment stores the
artifact id plus the artifact's content hash (`checksum`) frozen at attach time
(`LedgerAttachment`). The artifact lives in the ONE artifact store
(`python/harness/artifacts/`).

- **Attach** refuses an artifact that cannot be found or that has no checksum
  (`ARTIFACT_NOT_FOUND` / `ARTIFACT_UNHASHED`): an unverifiable reference is not
  evidence. Without the artifact service the answer is
  `ARTIFACT_STORE_UNAVAILABLE`.
- **`get_entry`** returns `attachments` with the entry, so an auditor reading one
  posting sees its evidence without a second lookup.
- **Verify** compares the frozen hash with the artifact's current hash:
  `MATCHED`, `CHANGED` (the artifact was replaced) or `MISSING` (deleted). It
  never rewrites the ledger.
- **Detach** removes the reference only; the artifact itself is unchanged.
- Attach/detach are audited (`DOCUMENT_ATTACHED` / `DOCUMENT_DETACHED`) and
  therefore sealed by the audit chain.
- The table is `entryId` + `artifactId` unique, so the same document cannot be
  attached twice to the same entry (`ATTACHMENT_EXISTS`).

## Chart of accounts

Charts ship as **data** in `ledger/accounts.py` (`standard`, `sole_trader`,
`minimal`). A new chart is a new entry in `CHARTS`, not a code branch.

- Account **type** (`ASSET`, `LIABILITY`, `EQUITY`, `REVENUE`, `EXPENSE`) decides
  the normal balance; that rule exists once (`models.normal_balance`).
- **`is_cash`** marks the accounts the cash-flow statement treats as cash.
  Nothing is inferred from an account's name.
- **`cash_flow_category`** (OPERATING / INVESTING / FINANCING / UNCLASSIFIED) is
  what a cash movement is attributed to, via the *counter* line's account.
  Revenue/expense accounts are always OPERATING by construction.
- **`currency`** is a restriction, not a default: a blank account accepts a line
  in any currency (translated to the base through the line's `fx_rate`); an
  account created with `currency='EUR'` accepts only EUR lines. The templates
  leave it blank on purpose.
- **`9999 Suspense`** is the one place an importer may park a line it could not
  classify. That keeps the trial balance balanced while making the problem
  visible: a non-zero suspense balance is a close blocker.

## Posting contract

`ledger_post` refuses, with a named code, when:

| Condition | Code |
|---|---|
| fewer than two lines | `INVALID_ENTRY` |
| a line names an unknown account | `ACCOUNT_NOT_FOUND` |
| a line names an inactive account | `ACCOUNT_INACTIVE` |
| a line has neither side, or both sides | `EMPTY_LINE` / `DUPLICATE_LINE_SIDE` |
| a negative amount | `NEGATIVE_AMOUNT` (post the other side) |
| debits ≠ credits | `UNBALANCED_ENTRY` (+ the exact difference) |
| a foreign-currency line without a rate | `MISSING_FX` |
| a rate on a base-currency line | `MISSING_FX` |
| an account restricted to another currency | `CURRENCY_MISMATCH` |
| the date falls in a CLOSED / LOCKED period | `PERIOD_CLOSED` / `PERIOD_LOCKED` |

Amounts are quantized to money places at the input boundary and never
mid-computation. `currency` + `fx_rate` translate a line to the base currency
(`base = q_money(amount × rate)`); the balance check is on base amounts, and both
transaction and base amounts are stored.

**Idempotency:** `(ledgerId, source, externalId)` is unique where `externalId` is
not null, so a retried import returns the FIRST posting (`duplicate: true`)
instead of posting twice. An entry without an external id is deliberately not
deduplicated.

## Periods

Monthly, with a configurable fiscal-year start month (`1..12`). A period is
materialised the first time a posting lands in it; `ledger_get` shows the current
period without creating it (a read has no side effects). Quarterly/annual
reporting is a date range over monthly periods, not a second calendar.

`close_period` runs the close check first: it refuses with `PERIOD_NOT_READY`
when a blocker exists (a suspense balance, a broken trial balance) and lists the
findings. `reopen_period` works on `CLOSED`; `lock_period` makes it permanent.

## Statements

| Statement | Reads | Check |
|---|---|---|
| trial balance | opening + movement + closing per account | closing debits = credits, movement balanced, Σ nets = 0 |
| balance sheet | assets / liabilities / equity as of a date, current earnings folded into equity | assets = liabilities + equity |
| income statement | revenue, cost of sales, gross profit, operating expenses, depreciation, net income | net income = gross profit − opex − depreciation |
| cash flow (direct) | operating / investing / financing / unclassified | opening + net change = closing; every cash movement classified |
| cash flow (indirect) | net income + depreciation/amortisation + operating working-capital movement; investing/financing from the direct statement | indirect operating = direct operating (any difference is reported) |

Every statement returns `checks` (`[{name, ok, detail}]`) and a `balanced` flag.
`currentEarnings` on the balance sheet is the same number as `netIncome` for the
same range — a test pins that.

`ledger_cash_flow` / `GET /ledger/{id}/cash-flow` accepts `method=indirect`.
The indirect statement selects working capital by the account's declared
`cash_flow_category` (ASSET/LIABILITY, non-cash, OPERATING): receivables,
inventory, prepaid, payables, tax, accruals, payroll, intercompany and suspense.
Depreciation/amortisation expense is added back by subtype. A debit movement in
an operating asset is cash used; a credit movement in an operating liability is
cash provided (`amount = −movement_net`).

The direct and indirect operating figures are reconciled, not forced. A non-cash
investing/financing entry (for example equipment bought on credit) is outside a
simple working-capital adjustment, so the difference is returned with the
amount and `reconciled: false` rather than plugged.

## Statement import

`POST /ledger/{id}/import/preview` and `ledger_import_preview` parse a bank export,
categorise every row and show exactly what *would* be posted. `…/import/commit`
posts it — through the same `LedgerService.post` every manual entry uses.

**Formats are declared, never guessed.** `format` is `csv`, `ofx` or `qif`, and a
CSV/QIF import carries a `mapping`. The mapping names the date column and its
`date_format`, the amount column (signed) or the debit/credit columns, the
description columns, and the decimal/thousands separators. `05/01/2026` means
5 January with `dd/mm/YYYY` and 1 May with `mm/dd/yyyy`; the caller says which.
An unparseable date is `IMPORT_ROW_INVALID` naming the row — it is never guessed.

What a row becomes, from the statement's point of view:

```
amount < 0 (money left the account)   Dr <categorised account> / Cr <bank>
amount > 0 (money arrived)            Dr <bank> / Cr <categorised account>
```

The sign picks the *bank* side, so a liability target (a loan repayment) needs no
special case.

**Idempotency.** Each row has a stable identity: the bank's `FITID` for OFX, else a
hash over date + amount + description + occurrence. The posted entry carries
`source='import'`, `external_id='stmt:<account>:<row-id>'`, so re-importing the
same file reports every row as `DUPLICATE` and posts nothing.

**Foreign currency.** A row whose currency is not the ledger's base currency is
`REQUIRES_FX` and is **not posted** until an `fx_rate` is supplied; with a rate it
posts at `q_money(amount × rate)`.

**Suspense.** A row no rule matches goes to account `9999 Suspense`. It is posted
(so the trial balance stays balanced and the money is on the books) and it blocks
a period close until it is categorised. It is never assigned to a guessed account.

A batch is `COMPLETE`, `PARTIAL` (some rows posted, some refused) or `FAILED`, and
`LedgerImportBatch` records the counts plus per-row errors.

## Categorisation rules

A rule answers one question: which account should the counter-entry hit?

- Conditions inside a rule are **ANDed**; the needles inside `payee_contains` are
  **ORed**. `amount_min`/`amount_max` compare the **magnitude**, so "over 100"
  works for both directions; `direction` (`IN`/`OUT`/`ANY`) selects the sign.
- Evaluation order is `(priority, name, version)` and **first match wins** — the
  same rows and rules always produce the same account.
- A rule whose target account is missing or inactive is **skipped and reported**
  (`skippedRules`), not silently ignored.
- Rules are **append-only versions**: creating a rule with an existing name writes
  the next version and deactivates the previous one, so a past import can still be
  explained by the version that was live.

## Reconciliation

`POST /ledger/{id}/reconcile` (`ledger_reconcile_statement`) compares a statement
to the ledger for one **cash** account. It never mutates the journal; a run is
persisted (`LedgerReconciliation` + items) and can be re-read.

| status | meaning |
|---|---|
| `MATCHED` | the row is in the ledger — by `EXTERNAL_ID`, or by `AMOUNT_DATE` when exactly one entry is within the tolerance and date window |
| `MISMATCH` | a candidate exists in the window but the amount disagrees; the difference is reported |
| `MISSING` | nothing in the ledger resembles the row |
| `UNKNOWN` | more than one candidate matches — never "pick the first" |

Plus a **balance check**: with `statement_balance` the run compares the declared
closing balance to the ledger's closing balance (cumulative through the statement
end date) and reports `BALANCED`/`UNBALANCED` and the difference. Without it the
run is `INCOMPLETE` — rows alone never earn the word "reconciled". Ledger entries
inside the period that no statement row covers are listed as `unpresented`.

## Depreciation, amortisation and accruals

`schedules.py` turns terms into every period's entry **up front**, so the whole
life of an asset can be inspected before the first posting.

- `kind` is `DEPRECIATION` (Dr depreciation expense / Cr accumulated depreciation)
  or `AMORTISATION` (Dr expense / Cr prepaid). Monthly periods; the entry date is
  that month's end.
- `STRAIGHT_LINE` divides `cost − salvage` and gives the **last period the
  remainder**, so `Σ entries == cost − salvage` exactly.
- `DECLINING_BALANCE` applies `factor / periods` to the opening book value each
  month and never takes the book below salvage; the period that would overshoot
  takes the remaining depreciable amount and the schedule ends.
- A period that rounds to zero posts nothing, but it is still consumed so it is
  never "due" forever.

`ledger_schedule_post` posts every period due on/before `through` (default today),
stops at the first entry that cannot post (a closed period), and does not repeat a
posted period. `sched:<scheduleId>:<sequence>` is the idempotency key.

**Accruals** are a one-off, not a schedule: `ledger_accrue` posts
`Dr debit / Cr credit` and, when `reversal_date` is given, the automatic reversal
on that date. `reference` makes the pair idempotent across a retried run.

## FX revaluation

`ledger_revalue_preview` / `ledger_revalue_post` true up the **base carrying
amount** of foreign-currency accounts to a supplied period-end rate.

- Only accounts **restricted to a foreign currency** are candidates. A base
  currency account has nothing to revalue, and an account with no currency
  restriction has no single foreign balance to translate.
- A rate is **supplied**. A missing rate is `RATE_REQUIRED` for that account and
  it is skipped — never converted at an invented or last-period rate.
- `delta = target_base − current_base` on the account's own (debit-positive) side,
  so a positive delta is a gain and a negative delta a loss. The counter-entry goes
  to the gain or loss account accordingly — no per-account-type special case.
- The adjustment is a **base-only line** (`base_only` on the journal line): it
  changes the carrying amount without touching the account's transaction-currency
  balance, which is why the foreign balance does not drift on a re-run.
- Idempotent per **account + date + rate**: re-running at the same rate is a
  no-op (the book already equals the target), and a rate correction produces one
  new entry that trues up the difference.

## The period close

`ledger_close_preview` runs the checklist and records the run; `ledger_close_period`
refuses to close while a **blocker** stands. Every finding names the account or
entry it is about.

| check | severity when it fails |
|---|---|
| `SUSPENSE_BALANCE` | blocker |
| `TRIAL_BALANCE_BROKEN` | blocker |
| `NO_ENTRIES` | warning |
| `IMPORT_INCOMPLETE` (a PARTIAL/FAILED batch) | warning |
| `NEGATIVE_CASH` (a credit balance on a cash account) | warning |
| `RECONCILIATION_NOT_CURRENT` (cash moved, no RECONCILED run covering the period end) | warning |
| `SCHEDULE_DUE` (an active schedule has unposted periods) | warning |
| `FX_REVALUATION_MISSING` (a foreign balance never revalued this period) | warning |
| `POSSIBLE_DUPLICATE` (same day, total and non-empty memo) | warning |

Warnings do not stop a close; they are the operator's to accept. The run is
persisted (`LedgerCloseRun`) with its findings and checks, so "we closed March"
carries the evidence that was true at the time.

## Year-end close

`ledger_year_end_preview` / `ledger_close_fiscal_year` (routes
`GET /ledger/{id}/year-end/preview` and `POST /ledger/{id}/year-end/close`) roll a
fiscal year's P&L into retained earnings.

- **Preview writes nothing.** It returns the net income, the retained-earnings
  target, every closing line and the closing date.
- **The closing entry is dated the FIRST day of the next fiscal year**, so the
  closed year's income statement is never rewritten. It carries
  `source='year_end_close'` and `externalId='ye:<yearEnd>'`, so a retry returns
  `ALREADY_CLOSED` with the existing entry instead of double-posting.
- **Lines:** every revenue/expense account is zeroed (debit the credit balance of
  a revenue account, credit the debit balance of an expense account), then the
  balancing amount goes to the first active `EQUITY`/`RETAINED` account. A net
  profit credits retained earnings; a net loss debits it.
- **P&L readers omit closing entries.** `income_statement` and the indirect
  cash-flow statement filter `source='year_end_close'`; the trial balance and
  balance sheet include them. After a close, `currentEarnings` is zero and
  retained earnings carries the result.
- A chart with no active retained-earnings account is refused
  (`RETAINED_EARNINGS_MISSING`); a year with no revenue/expense movement is
  `YEAR_END_NOT_READY`.
- A correction is a **reversal** entry, never an edit, like every other posting.

## Dimensions, budgets and runway

**Dimensions are already first-class on every journal line** (`dimensions`, a
small string→string map validated at post time). `ledger_dimension_summary` /
`GET /ledger/{id}/dimensions/{dimension}` group movement by one dimension value
(project, cost centre, department) over a date range. Cash lines are excluded
from the grouping; a line without the dimension is reported as `(unassigned)`,
never dropped.

**A budget is planning data, not a posting.** `LedgerBudget` + `LedgerBudgetLine`
store a named set of account + optional dimension targets over a period.
`ledger_budget_create` / `ledger_budget_list` create and read them;
`ledger_budget_variance` / `GET /ledger/{id}/budgets/{id}/variance` derive
actuals from the journal:

- A posting is assigned to the **most specific matching budget line** (most
  dimension keys), so a broad account line and a project line do not
  double-count.
- Actuals are signed in each account's natural direction (revenue is
  credit-positive, expense debit-positive). `favorable` compares against that
  direction; `variancePct` is `null` when the budget is zero.
- Actuals that match no line appear in `unbudgeted`, with the amount, rather
  than disappearing.

**Cash runway** (`ledger_cash_runway` / `GET /ledger/{id}/runway`) uses the
trailing direct operating cash flow to derive a monthly burn, adds open AR/AP
from the aging report, and returns `runwayMonths`, an AR/AP-adjusted runway and
a horizon projection. A surplus (negative burn) reports runway `UNKNOWN`, never
an infinite number. It is a labelled planning model, not a collection forecast.

## Receivables, payables and tax

`documents.py` is the AR/AP subledger. A **document** is an invoice (`INVOICE`, a
customer owes you) or a bill (`BILL`, you owe a vendor). It is a validated batch
of lines handed to the one posting path — not a second journal.

```
INVOICE   Dr <AR control> total      Cr <revenue> net      Cr <tax account> tax
BILL      Dr <expense> net           Dr <tax account> tax  Cr <AP control> total
```

- **Lines** carry a description, an `account_code` (revenue or expense), a
  quantity, a unit price and an optional `tax_code`. Totals are the sum of the
  quantized line net + tax, so the header can never disagree with the lines.
- **Tax is configured data.** A tax code names its jurisdiction, its rate as a
  fraction (`0.20`), the GL account the tax posts to, and whether prices are
  inclusive. There is **no built-in rate table** — a rate is a fact about a
  jurisdiction on a date, and shipping one would be a fabricated fact that goes
  stale. A code that is not configured is `TAX_CODE_NOT_FOUND`; a pack for an
  unconfigured jurisdiction is `UNSUPPORTED_TAX_JURISDICTION`.
- **Control accounts are discovered, not configured.** The first active account
  with subtype `RECEIVABLE` (invoices) or `PAYABLE` (bills) is the control
  account; a caller may override it per document. None found is
  `CONTROL_ACCOUNT_MISSING`, never an invented account.
- **Payments allocate.** `ledger_document_pay` posts `Dr bank / Cr AR` (or
  `Dr AP / Cr bank`) and records an allocation. Over-payment is refused
  (`DOCUMENT_OVERPAID`); a repeated `reference` posts once.
- **Voiding reverses.** An unpaid document is voided by reversing its journal
  entry. A document with payments is refused (`DOCUMENT_HAS_PAYMENTS`) — reverse
  the payment first.
- **Aging is derived.** Current / 1-30 / 31-60 / 61-90 / 90+ is computed from open
  documents and their allocations **at the report date** (an invoice paid in
  February is still open on 31 January), and the total is checked against the
  control account's closing balance. A difference is reported, not hidden.

Parties are thin: a name, `CUSTOMER`/`VENDOR`, optional contact/tax identifiers
and a payment term. No balance is stored on a party.

## Entity groups and consolidation

A **group** is a consolidation scope: member ledgers, an ownership percentage
each, and a reporting currency. A group owns no journal and never posts to a
member's books — `ledger_consolidate` is a report.

- **Method: proportionate.** A member held at 80% contributes 80% of every line.
  There is no non-controlling-interest line; full consolidation with NCI is not
  modelled. This is stated in the report as `ownershipMethod: "PROPORTIONATE"`.
- **Accounts are matched by code AND type.** Two members may both have `1900`, but
  if one calls it an asset and the other a liability that is
  `ACCOUNT_TYPE_CONFLICT` (a blocker), not a number to average. Charts that do not
  share codes are not silently mapped for you.
- **A foreign member needs a supplied rate.** `rates={"EUR": "1.20"}` translates a
  EUR member into the group currency; a missing rate is `RATE_REQUIRED` and the
  member is excluded — never converted at an invented rate. One rate per currency
  is used for every account (no average-rate P&L vs closing-rate balance-sheet
  split).
- **Intercompany is eliminated by subtype**, not by name: `INTERCOMPANY_RECEIVABLE`
  ↔ `INTERCOMPANY_PAYABLE` on the balance sheet and `INTERCOMPANY_EXPENSE` ↔
  `INTERCOMPANY_REVENUE` in the P&L. Only the **matched** amount is eliminated; a
  remainder stays in the consolidated books and is reported as
  `INTERCOMPANY_OUT_OF_BALANCE` (a warning). Nothing is force-balanced.
- The consolidated figures are handed to the **same statement engine** as a single
  ledger, so the accounting equation is checked once, in one place.

## Exports

`ledger_export` (and `GET /ledger/{id}/export`) returns
`{format, filename, contentType, content}` — text out; persisting it is the
caller's job. Four formats:

| format | what it is |
|---|---|
| `journal_csv` | one row per journal line: date, entry, reference, description, account code/name, debit, credit, currency, source |
| `trial_balance_csv` | opening / movement / closing per account with a totals row |
| `json` | the complete bundle: ledger, accounts, trial balance, statements, entries |
| `xbrl` | a well-formed XBRL 2.1 **instance** document |

Two deliberate refusals:

- **XBRL is not a filing.** The instance declares a context, a unit and the
  primary-statement facts under a local namespace; there is no IFRS/GAAP taxonomy
  linkbase. The file says so in its own comment. Filing needs a jurisdiction's
  taxonomy mapping.
- **SAF-T is refused** (`UNSUPPORTED_EXPORT`). It is a jurisdiction-specific audit
  file with a mandated schema version and validation rules; a look-alike would be
  indistinguishable from a compliant one. Use `json`/`journal_csv` and map them
  with the filing tool.

A statement export with no date range anchors its start at the **first posting**,
not at the current month. Exports stop at the entry limit and say `truncated: true`
rather than silently dropping rows.

## Surfaces (chat only)

**The ledger is a chat capability by design.** There is no dedicated tab, page or
browser client: the owner's decision is that accounting happens in conversation
with the `bookkeeper` agent (or any agent holding the `ledger_*` tools). Nothing
computes a second set of numbers, because there is nothing else to compute them.
The Python `/ledger/*` routes remain an operator/API surface like every other
subsystem's; they are not a UI.

**The `bookkeeper` preset** is a closed agent surface: its allowlist is exactly the
`ledger_*` family (no market data, no code, no browser, no MCP), and its
instructions are the discipline — never invent a number, a posting balances or it
does not happen, corrections are reversals, an unmatched row goes to suspense, a
tax rate is configured data, and a period is not "reconciled" unless the tool says
so. Mutations still require the `ledger_write` approval: the allowlist is the
surface, the authorizer is the gate.

**Monitors.** Three ledger shapes are in the closed monitor catalogue
(`advisory/monitors.py`), all created switched off: `ledger_suspense_watch`
(`ledger.{id}.suspense > {threshold}`), `ledger_close_blocked`
(`ledger.{id}.blockers > 0`) and `ledger_receivables_watch`
(`ledger.{id}.open_ar > {threshold}`). Their facts come from `LedgerFactProvider`
in `automation/facts.py`, which resolves `ledger.<id>.<field>` through the SAME
service the tools read (`suspense`, `blockers`, `warnings`, `ready`, `open_ar`,
`open_ap`, `entries`). A missing ledger, a failed read or an unknown field yields
NO fact, so the condition evaluates UNAVAILABLE rather than firing on a
fabricated number.

## Agent tools and safety

Reads run freely. Every mutation declares effect `ledger.write` and is admitted
only by `make_ledger_authorizer` (`ledger/authorizers.py`): a standing grant, a
single-use digest-bound operator approval of kind `ledger_write`, or a
request-then-decision pause. The digest binds ledger + tool + arguments, so a
changed payload needs a new approval. With no approval store the answer is
refusal.

A posting is a write to the **book of record**, not a payment: nothing in this
package moves money, so it needs one approval and no money guardrail. Any future
payment initiation belongs behind the existing finance guardrails instead.

### Optional segregation of duties

Above that single-use approval, an operator may configure a **per-effect SoD
policy** (`harness/sod.py`). It is **off by default** — with no policy,
single-user local mode is byte-for-byte unchanged. A policy sets a threshold and
`requiredApprovals`: an effect whose amount is at/above the highest applicable
threshold needs N **distinct** approvers, and by default the requester cannot be
one of them (`SOD_SELF_APPROVAL`).

- Policy/sign-off routes are operator-only: `GET|POST /sod/policies`,
  `DELETE /sod/policies/{id}`, `GET /sod/status`, `POST|GET /sod/signoffs`.
  There is deliberately **no agent tool to sign off**, so the agent cannot
  satisfy a rule that exists to constrain it.
- Sign-offs are keyed by the exact call digest (the same digest as the single-use
  approval), so a changed amount or payload needs fresh sign-offs. They are
  consumed when the call is authorized.
- Both gates apply: SoD sign-offs **and** the single-use `ledger_write`
  approval. If SoD is unsatisfied the authorizer requests the normal approval
  card, emits `sod.pending`, and refuses without pausing; the operator records
  the missing sign-offs and the caller retries.
- A broken SoD store fails closed once a policy is configured.
- The amount is the balanced debit total of a posting, an explicit
  `amount`/`total`, or `quantity × price`; a zero threshold applies to every
  call regardless of amount.

The registered handlers are wrapped so no ledger failure escapes as an exception:
the tool loop always receives `status: FAILED` plus a named code.

| Tools | Class |
|---|---|
| `ledger_list`, `ledger_get`, `ledger_list_accounts`, `ledger_list_entries`, `ledger_get_entry` | read |
| `ledger_list_attachments`, `ledger_verify_attachment` | read |
| `ledger_trial_balance`, `ledger_balance_sheet`, `ledger_income_statement`, `ledger_cash_flow` | read |
| `ledger_list_periods`, `ledger_audit_log`, `ledger_audit_verify` | read |
| `ledger_year_end_preview` | read |
| `ledger_budget_list`, `ledger_budget_variance`, `ledger_dimension_summary`, `ledger_cash_runway` | read |
| `ledger_import_preview`, `ledger_list_imports`, `ledger_rule_list` | read |
| `ledger_reconcile_statement`, `ledger_list_reconciliations`, `ledger_get_reconciliation` | read (persist a run; never touch the journal) |
| `ledger_schedule_list`, `ledger_revalue_preview`, `ledger_close_preview` | read |
| `ledger_list_close_runs`, `ledger_get_close_run` | read |
| `ledger_party_list`, `ledger_tax_code_list`, `ledger_document_list`, `ledger_document_get`, `ledger_aging` | read |
| `ledger_group_list`, `ledger_consolidate`, `ledger_export` | read |
| `ledger_create`, `ledger_create_account`, `ledger_set_account_active` | write (`ledger.write`) |
| `ledger_post`, `ledger_reverse` | write |
| `ledger_attach_document`, `ledger_detach_document` | write |
| `ledger_close_fiscal_year` | write |
| `ledger_budget_create` | write |
| `ledger_import_commit` | write |
| `ledger_rule_create`, `ledger_rule_deactivate` | write |
| `ledger_schedule_create`, `ledger_schedule_post`, `ledger_accrue`, `ledger_revalue_post` | write |
| `ledger_party_create`, `ledger_tax_code_create`, `ledger_document_create`, `ledger_document_void`, `ledger_document_pay` | write |
| `ledger_group_create`, `ledger_group_add_member` | write |
| `ledger_close_period`, `ledger_reopen_period`, `ledger_lock_period` | write |

`ledger_reconcile_statement` is read-class on purpose: it writes a report row, not
a posting. A run that finds problems cannot change the book.

## HTTP routes

`GET /ledger/list`, `POST /ledger/create`, `DELETE /ledger/{id}`,
`GET /ledger/{id}`, `GET|POST /ledger/{id}/accounts`,
`POST /ledger/{id}/accounts/{code}/active`, `POST /ledger/{id}/entries`,
`GET /ledger/{id}/entries`, `GET /ledger/{id}/entries/{entryId}`,
`POST /ledger/{id}/entries/{entryId}/reverse`, `GET /ledger/{id}/trial-balance`,
`…/balance-sheet`, `…/income-statement`, `…/cash-flow`, `GET …/periods`,
`POST …/periods/{periodId}/{close,reopen,lock}`, `GET …/year-end/preview`,
`POST …/year-end/close`, `GET …/audit`, `GET …/audit/verify`,
`GET|POST …/attachments`, `DELETE …/attachments/{attachmentId}`,
`GET …/attachments/{attachmentId}/verify`, `GET|POST …/budgets`,
`GET …/budgets/{budgetId}/variance`, `GET …/dimensions/{dimension}`,
`GET …/runway`,
`POST …/import/preview`, `POST …/import/commit`, `GET …/imports`,
`GET|POST …/rules`, `POST …/rules/{ruleId}/deactivate`, `POST …/reconcile`,
`GET …/reconciliations`, `GET …/reconciliations/{runId}`,
`GET|POST …/schedules`, `POST …/schedules/{scheduleId}/post`, `POST …/accrue`,
`POST …/revalue/preview`, `POST …/revalue`, `POST …/close/preview`,
`GET …/close-runs`, `GET …/close-runs/{runId}`,
`GET|POST …/parties`, `GET|POST …/tax-codes`, `GET|POST …/documents`,
`GET …/documents/{documentId}`, `POST …/documents/{documentId}/void`,
`POST …/documents/{documentId}/pay`, `GET …/aging`,
`GET|POST …/groups`, `POST …/groups/{groupId}/members`,
`POST …/groups/{groupId}/consolidate`, `GET …/export`.

All operator-gated. Tenant scope via `X-Harness-User` (403 on mismatch); with no
actor asserted the app is in its documented single-user local mode.

## Known limits (honest)

- **Monthly periods only.** No weekly/4-4-5 calendar.
- **Indirect cash flow models depreciation/amortisation plus operating working
  capital.** Other non-cash items (e.g. non-cash investing/financing) are not
  reconstructed; they show up as a reported direct-vs-indirect difference.
- **Budgets are one currency per budget and have no version/approval workflow.**
  Actuals always come from the journal; editing a budget is a new budget.
- **Runway is an extrapolation, not a forecast.** It uses trailing operating cash
  flow and open AR/AP at face value; it does not model seasonality, collection
  probability or financing events.
- **Single functional currency per book**, with per-line FX translation. No
  multi-currency revaluation of monetary balances and no translation reserve.
- **No subledger beyond AR/AP:** invoices, bills, payments and aging exist;
  credit notes, dunning letters, recurring invoices, purchase orders and
  per-party credit limits do not.
- **Documents are in the ledger's base currency.** A foreign-currency invoice
  needs document-level FX plus a revaluation of the open item; neither is built,
  so a non-base document is refused rather than silently translated.
- **No tax engine:** a sales-tax control account exists; jurisdiction rates,
  returns and filings are not modelled.
- **Inventory costing and payroll are not modelled.** Depreciation and
  amortisation schedules exist; there is no inventory valuation engine and no
  payroll calculation/withholding.
- **Import is per-account and single-currency-per-file.** A statement is parsed
  against one declared mapping and posted against one bank account; multi-account
  files (a combined export) must be split first. PDF statements are not parsed —
  the document-intelligence extractor can read them, but nothing feeds its output
  into the ledger automatically.
- **No bank feed / live API pull.** Statements are supplied as text.
- **No automatic splitting, transfers or matching across accounts**: a transfer
  between two bank accounts is two rows that each need a category; an
  inter-account transfer pair is not collapsed.
- **Reconciliation does not repair.** It reports MATCHED/MISMATCH/MISSING/UNKNOWN
  and a balance difference; posting a correcting entry is the operator's decision.
- **FX revaluation covers foreign-currency-RESTRICTED accounts only.** An account
  that accepts any currency has no single foreign balance to translate, so it is
  not revalued; there is no per-transaction open-item revaluation (that needs the
  receivable/payable subledgers).
- **Accruals are one-off entries**, not a recurring schedule (an accrual pattern
  that repeats monthly is the operator's to re-post or a future schedule kind).
- **Year-end close is one closing entry, not a per-account close checklist.**
  It does not require every month of the year to be `CLOSED` first. The entry is
  dated in the next fiscal year, so that period must be `OPEN`; monthly `LOCKED`
  remains the control that stops later postings into a closed month.
- **Consolidation is proportionate and by account code.** No
  non-controlling-interest line, no full-consolidation/NCI treatment, no chart
  mapping between members that use different codes, and one translation rate per
  currency for all accounts.
- **Exports are text, not filings.** XBRL is a well-formed instance with no
  taxonomy linkbase; SAF-T is refused; there is no PDF/Excel statement pack
  (the existing `document_create` builders can render a payload).
- **No multi-entity intercompany transaction matching.** Elimination pairs are
  found by account subtype and balanced by amount; it does not match individual
  intercompany invoices against each other.
- **No UI, deliberately.** The ledger is used in chat; there is no tab, page or
  browser client to keep in sync with the engine.
- **No financial-statement PDF/XLSX export** — the existing `document_create`
  builders can render a statement payload; a dedicated exporter is not built.
- **No scheduled year-end automation**: the closing entry is an explicit tool /
  route; it is not yet a graph node or a scheduler job.
- **SoD policies are enforced only by the finance authorizers** (`ledger.write`
  and the portfolio write/proposal effects). Other effect classes are not wired;
  the policy surface is routes/API, not a dedicated settings page.

## Next phases (the plan this core unblocks)

Phase 1 is the core, Phase 2 is import + categorisation + reconciliation, Phase 3
is schedules, accruals, FX revaluation and the close checklist, Phase 4 is the
AR/AP subledger with configured tax codes, Phase 5 is entity groups, consolidation
and exports, and Phase 6 is the chat surface (`bookkeeper` preset + monitors).
Still open:

7. Ledger graph nodes (`ledger.post`, `ledger.close`) so a workflow can close a
   period on a schedule without an agent turn.
