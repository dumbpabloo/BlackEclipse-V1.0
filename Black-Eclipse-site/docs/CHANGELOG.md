# Change Log

Older entries are **moved, never deleted** into [`docs/changelog/`](./changelog/)
— one file per month. This file keeps the newest seven dated sections;
`npm run rotate:changelog -- --write` performs the move.

## 2026-11-27

- **docs(updates): record the published v0.4.31 manifest.** `desktop-release.yml`
  built the Windows installer (`195,555,083` bytes, sha256 `622d4216…`) and the
  prebuilt archive (`80,522,912` bytes, sha256 `60c3f211…`, buildId
  `O858p8KbVNOQMn_Mme_xm`, lockHash `78c740b8…`) into a draft; `stable.json` was
  signed locally with key id `47e545f302249385`, uploaded to the draft, and the
  release published. `node scripts/update.js --check` reports
  `latest signed release: v0.4.31`. docs updated: `docs/UPDATES.md` (release
  state), `docs/CHANGELOG.md`.

- **release: v0.4.31.** Version bumped in `package.json`, `package-lock.json`,
  `pyproject.toml`, `python/harness/__init__.py` and the README's current-version
  line; SBOM and published benchmarks regenerated. Ships the Self-Improvement
  Memory ledger (opt-in `HARNESS_RSL_MEMORY`), the two-action bots onboarding,
  side threads/queue/composer work and the group-room orchestration already on
  `main`. The `v0.4.31` tag drives `desktop-release.yml`; the offline-signed
  `stable.json` is uploaded to the draft before publishing.
  docs updated: `docs/CHANGELOG.md`, `docs/UPDATES.md` (release state).

- **feat(rsl): Self-Improvement Memory (SIM) — the harness optimizer's own
  problem-keyed ledger.** RSL kept per-candidate attempt rows
  (`EvolutionStore.save_attempt`) but not *why* each edit existed, what it
  predicted, or what it observed in another setting — so a model or benchmark
  change forced the proposer to rediscover the same root causes. New
  `self_improvement/improvement_memory.py` implements the mechanism from
  arXiv:2611.18856: append-only problems keyed by normalised root cause text
  (merged with aliases), attempts carrying rationale, edit, **predictions
  recorded before evaluation**, observed fixed/missed/unresolved, pass counts,
  utility delta and provenance (domain/model/round/digest); `unmeasured` and
  unresolved predictions are never coerced to zero. Bounded deterministic
  `render()` states what it omitted; `carry_from()` carries the ledger into a
  new setting while keeping source provenance. Wired opt-in
  (`HARNESS_RSL_MEMORY=off` by default): the model proposer receives the block,
  every evaluated hill-climb neighbor is appended after the climb, and
  `GET /evolution/rsl/memory` + `memory` in `GET /evolution/rsl` expose it.
  No second database, no model call, no authority to promote or land.
  docs updated: `docs/self-improvement/sim-continual-memory-plan.md` (new),
  `docs/NAVIGATION.md` (RSL row).

- **fix(sign-in): the dead browser-lane launchers no longer open at logon.** The
  2026-11-11 cleanup renamed `BlackEclipse Front Door.vbs` and
  `BlackEclipse.cmd` to `*.disabled-legacy` but left them in the user's Startup
  folder. Explorer still ran every Startup item, so the unknown
  `.disabled-legacy` extension was handed to Notepad (Windows had recorded it in
  `FileExts\.disabled-legacy\OpenWithList`) and the dead
  `local-proxy.js` comment text appeared at every sign-in. Both files are now
  moved out of Startup to `%LOCALAPPDATA%\BlackEclipse\legacy-startup\` and the
  stray `OpenWithList` entry is removed; Startup holds only `Hermes_Gateway.vbs`.
  Nothing referenced the moved files (the proxy lane is deleted, and no
  scheduled task / `Run` key ever pointed at them). no doc change — the lane's
  removal is already documented in `docs/local-hostname.md` and
  `docs/environments.md`; this only finishes a machine-level leftover.

## 2026-11-26

- **chore(repo): GitHub Pages is enabled and the public metadata now matches the
  page.** Pages had never been turned on, so `site/index.html` was committed but
  unreachable; it is now enabled with **Source: GitHub Actions**, deployed from
  `main`, and live at `https://dumbpabloo.github.io/BlackEclipse-V1.0/`. The
  repository description now carries the positioning sentence, the homepage points
  at the Pages site, and twelve finance/AI topics replace an empty topic list.
  `pages.yml` and `public-readme.md` no longer tell the reader to enable Pages —
  they record that it is enabled and that a README-only push does not redeploy it.
  docs updated: `docs/public-readme.md` (§8 publish path); `.github/workflows/pages.yml`
  comment.

- **docs(readme): the hero banner drops its logo lockup.** The first version repeated
  the eclipse mark and wordmark inside the banner, directly under the repository name
  GitHub already renders. The banner is now purely typographic — eyebrow, the
  positioning sentence, and the scope/version strip — so the headline carries the
  page and the artwork no longer competes with the page chrome. Regenerated
  `.github/assets/readme-hero.png` from the SVG.
  docs updated: `docs/public-readme.md` (§3 banner rule).

- **docs(readme): the public front door is rebuilt around one positioning
  sentence, with Features / Use / Needs.** The README led with "the open-source
  financial agentic harness that grows with you", buried automation as capability
  #9, and spread requirements through a 40-line install block. The hero now states
  *"Black Eclipse is an open-source agentic harness designed to help you automate
  finance"*; six feature groups replace the nine-capability and seven-area tables;
  six concrete recipes replace the workflow and persona tables; and one Needs
  table absorbs the install requirements. The page now opens with an authored
  **hero banner** (`.github/assets/readme-hero.svg` / `.png`), centred download
  and badges, a three-shot "See it" gallery, and `<details>` blocks for
  troubleshooting, methodology, sources and licensing. The benchmark block is
  condensed, the legal data-source table now points at the generated doc, and the
  page is 366 lines instead of 444. The GitHub Pages landing page
  (`site/index.html`) mirrors the same hero and sections, and every
  `dumbpabloo/black-eclipse` link (which 404s) is corrected to
  `BlackEclipse-V1.0`. Social card regenerated with the new tagline and `v0.4.30`.
  docs updated: `docs/public-readme.md` (§1 order, §2 claim evidence, §3 figures,
  §4 rule 6, §6 L2, new §8 landing page), `docs/OVERVIEW.md` (product definition),
  `docs/NAVIGATION.md` (public surface row), `docs/CHANGELOG.md`.

- **fix(desktop): the taskbar badge shows the unread count, not a bare dot.**
  Windows painted one red disc from `SetOverlayIcon`, so the operator could not
  tell one waiting reply from twelve. `overlay_unread_image` now rasterizes the
  count with a 3×5 bitmap font (3×/2× glyph scale for two digits and `99+`) on
  the same red disc, sharing the supersampled circle and rim. The settings copy
  and the emitted `count` are unchanged, and the count still survives hide/show
  via `AppState`. Tests: `taskbar_icon_tests`.
  docs updated: `desktop/README.md`, `docs/desktop-app-plan.md`; no change
  needed in the canonical system docs (shell-internal rendering, same
  `be-taskbar` contract).

- **fix(quant): synthesized futures specs validate, and strategy builds get the
  budget to finish.** `infer_asset_class()` returned `"futures"` while
  `SUPPORTED_ASSET_CLASSES` spells the class `"future"`, so every method
  synthesized on `GC=F`/`CL`/`=F` returned `valid=False
  (UNSUPPORTED_ASSET_CLASS)` and the build stopped before a backtest. Separately,
  a deep quant turn now floors at 32 steps (reported in `run.started`,
  previously it under-reported 24) and raises the tool-call ceiling from 48 to
  64; a strategy build had hit the 48-call limit at step 22 with steps still
  left, so `validate_strategy`/`create_strategy`/`run_backtest` never ran.
  Tests: `test_quant_discovery.py`, `test_chat_step_budget.py`.
  docs updated: `docs/quant-engine.md`, `docs/harness/architecture.md`.

- **test(harness): isolate each parallel pytest worker's SQLite files.** The
  sharded suite gave every worker the same `AGENT_MEMORY_DB_PATH` /
  `ECLIPSE_MEMORY_DB_PATH`, so concurrent startup migrations raced and a worker
  died during COLLECTION with `duplicate column name: recoveredCall` (exit 2)
  before running a test. `pytest-parallel.js` now accepts a per-worker env
  factory and `test-python.js` gives each worker its own DB pair.
  no doc change — test-runner infrastructure.

- **feat(harness): cooperative drain, activity heartbeat, retention and honest
  wait/reconnect state.** Completes the parallel-chat reliability work: the
  supervisor now calls `POST /runtime/drain` (new runtime router) before clearing
  a silent port, so a restart waits for live runs; `run_activity` heartbeats every
  event (transient deltas included) and the watchdog treats fresh activity as
  live; `model.queued`/`model.rate_limited` are persisted per run via
  `model_runtime/run_events.py` and the chat shows "Waiting for provider
  capacity…" / "Connection lost — reconnecting…" instead of a bare spinner; the
  desktop tab strip marks a tab whose conversation still has a live run.
  `python/harness/retention.py` + `npm run memory:prune` prune terminal-run child
  rows (dry run by default) and checkpoint the WAL — a live dry run against the
  multi-GB store reported 800 terminal runs / 36,583 events eligible at 30 days
  (no deletion performed). Tests: `test_run_activity.py`, `test_retention.py`,
  `test_runtime_drain.py`, `test_model_run_events.py`, the watchdog/parallel-tool
  additions, and the TS notice/run-manager cases.
  docs updated: `docs/harness/multi-session-concurrency.md`.

- **fix(harness): stop orphaned runs when two chats stream in parallel.**
  Root cause was a runtime replacement/kill while runs were in flight: the
  supervisor cleared a listening-but-silent port after a 30 s grace with no
  `activeRuns` check, the lease heartbeat died permanently on one transient
  SQLite lock, and a lost/crashed run had no terminal write (the watchdog only
  noticed ~6 min later and wrote `RUN_STALLED`). Adds a single-owner port lock
  before uvicorn's lifespan, a conservative silence/kill policy that protects
  active and parked runs, per-chat shared-run keys (v3) and per-tab new-chat
  keys, a per-tab rate-limit key, chunk-idle + serial-tool deadlines, terminal
  settlement for lost/crashed runs, and a client stream idle watchdog with SSE
  resume. Verified against the live DB: two runs on `stable:14000` stopped
  heartbeating at 21:17:38/41 and were only failed at 21:24:09; new runs were
  already being served by `stable:12044`.
  docs updated: `docs/harness/multi-session-concurrency.md`.

- **test(size): re-pin the shared-tree Python module budgets.** The accounting /
  portfolio work already in the tree pushed `main.py`, `service.py`,
  `ledger/{service,store,tools}.py`, `portfolio/{service,tools}.py`,
  `trade/service.py` and `graph.py` past their pinned budgets. Re-pinned to the
  measured sizes with split candidates named in
  `python/tests/test_size_budget.py`; no runtime code was changed by this
  re-pin. `test_phase_s_stabilization.py` updated for the new `main.py`
  measurement. Also made the port-ownership child test inject the package root
  into `sys.path` (the vendored interpreter ignores `PYTHONPATH`).
  no doc change — test-only.

- **test(runner): make the port-ownership child see `harness` under the vendored
  interpreter.** The full suite runs from the repo root with the vendored
  Python, whose `_pth` ignores `PYTHONPATH`; the test now injects the package
  root into the child's `sys.path` (same launcher pattern as the test runner).
  no doc change — test-only.

- **test(registry): regenerate the published tool manifest.** The webhook
  trigger and external-agent action changed the tool schema, so
  `docs/harness/tool-registry.json` was regenerated with `npm run eval:registry`
  (1133 tools, digest `cb8ca155b15b0c01`). No runtime behavior changed by the
  regeneration.
  docs updated: `docs/harness/tool-registry.json`.

- **feat(bots): quiet runs, continuity handoff, roundtable rooms, approvals
  inbox, archive, and stale-write protection.** Scheduled bot runs may answer
  exactly `[[no-update]]` (or `[SILENT]`); the answer stays in run history but
  is kept out of the thread and the completion toast/sound, and a pending order
  always breaks quiet. Each scheduled run gets a bounded digest of the bot's
  last completed reports, labelled unverified, to survive compaction/reset.
  Group rooms gain a **Roundtable** mode (members answer in order and later
  members see earlier replies in the same round) plus an optional chair that
  writes one visible synthesis, alongside independent takes.
  Bots mode now mounts the effectful-tool approvals panel in the scheduler dock,
  group rooms mount the trade approval banner, the row menu can archive a bot,
  and the UI-authored condition trigger defaults to the backend's edge semantics
  with an explicit toggle. The BotState PUT refuses an older `updatedAt` with
  409 + the current document, so a stale tab cannot roll the transcript back.
  Bot/group messages now also have a per-message durable log
  (`BotChatMessage`, `BotGroupMessage` + `/api/bots/messages`) that survives a
  lost localStorage snapshot; clear/regenerate replaces the log atomically.
  Team `maxCost`/`maxRuntimeMs` now meter from a task result's optional
  cost/runtime and stop NEW work at the next task boundary (in-flight provider
  calls still complete; the run reports the exhausted budget honestly).
  Adds a **webhook trigger**: the runtime generates a per-bot bearer token at
  create time and exposes `POST /api/automations/webhook/<id>` (Python
  `/automations/webhook/{id}`) outside operator auth; the token is compared in
  constant time, the bot must be active, and an edit preserves the token. The
  Bots schedule editor and detail page expose the type, URL and token.
  `messaging` delivery is now real: `<kind>:<chat>` targets call the one
  `ChannelService.send_outbound` with the rendered run report instead of
  publishing a dead event; failures are recorded per target, never raised.
  Trade approvals carry the proposing bot (`automationId`) and a bot's chat
  filters the banner to its own orders (old unattributed rows stay visible).
  Scheduled bots get an isolated workspace under `.black-eclipse/bots/<id>`
  when they have no explicit root; a run may now narrow (never widen) the
  server-resolved workspace to a real subdirectory. The Bots sidebar has a
  search field over bot name/description/objective/tags and room names, and a
  running desk can be cancelled from its room. A new **Teammates** panel in the
  scheduler dock lists A2A agents and recent durable messages and can post a
  STATUS message through a guarded `/api/a2a/*` proxy.
  Room identity/membership/mode/chair/photo now has a durable `BotRoom` row and
  `/api/bots/rooms`, so a cleared snapshot no longer orphans transcripts.
  Adds a real minimal **ACP client** (`python/harness/acp_client.py`): a
  `run_external_agent` action launches an external agent over stdio JSON-RPC
  (`initialize → session/new → session/prompt`), streams text, denies
  permission requests unless explicitly allowed, and exposes command/args on the
  bot detail page (operator-only; not exposed to `bot_create`/`bot_update`).
  The full ACP SDK surface is intentionally not implemented.
  Bots export/import as secret-free, versioned JSON: export strips the webhook
  token and credential reference, and import always creates a draft. Adds
  `npm run bots:headless` (`scripts/bots-headless.js`): the Python runtime and
  scheduler run without Electron/Next (loopback by default; the existing
  non-loopback secret guard is unchanged), giving scheduled bots an always-on
  host path.
  Group rooms now have real orchestration: optional lead routing for
  unaddressed requests, per-request task ownership/status with reassignment,
  threaded member replies (quoted parent), and a bounded shared context packet.
  Adds per-bot backup-model failover for provider-class failures and a
  privacy-safe Bots diagnostics JSON (`/api/bots/diagnostics`, sidebar Download).
  Bots gain side threads and branch-from-message (each with its own conversation
  id), a durable queued-turn panel with reorder/cancel/steer, and general emoji
  reactions on bot and room messages.
  The Bots home is reduced to two actions (Create a bot / Start a group chat);
  desks, finance packs, the institution committee and the EUR/USD starter move
  into Create Bot as template cards, sidebar utilities fold into one menu, the
  conversation header collapses to Run now + status pill + scheduler + one menu,
  and the bot/group composer becomes one capsule with an inline paperclip,
  growing textarea and a one-line queue strip.
  Extends the ACP client with `session/load`, MCP server descriptors at session
  setup, and opt-in path-confined fs/terminal capability handling. Adds a bot
  audit trail (`AutomationAudit`, `/automations/audit`, Bots Audit section) and
  gives each bot chat the same server-owned `bot:<id>` workspace as its scheduled
  runs. Updates `docs/research/hermes-vs-eclipse/FEATURE-GAP-MATRIX.md` with the
  closed rows.
  Verified: `npx tsc --noEmit` clean; full Vitest 5,356 passed / 1 skipped;
  Python 14,094 passed / 70 skipped (1,225 files); eval gates ALL PASS.
  New tests: `test_acp_client.py`, `test_automation_failover.py`,
  `group-orchestration.test.ts`, `queue-store.test.ts`, `room-log.test.ts`,
  `transfer.test.ts`, plus side-thread/branch coverage in `chat-store.test.ts`.
  Tests: `quiet.test.ts`, `groups-store.test.ts`, `bots-run-diff.test.ts`,
  `message-log.test.ts`, `test_automation_quiet_runs.py`,
  `test_task_budget_usage.py`; `npx tsc --noEmit` clean.
  docs updated: `docs/bot-mode.md`, `docs/harness/bot-team-orchestration.md`,
  `docs/harness/automation-engine.md`, `docs/harness/agent-to-agent.md`,
  `docs/research/hermes-vs-eclipse/FEATURE-GAP-MATRIX.md`.

- **docs(analysis): accounting & finance gap analysis.** Adds a source-verified
  gap register across the General Ledger, Portfolio Accounting Engine, broker
  journal and finance guardrails, with the highest-risk finding being that the
  Funds UI recomputes cost basis/P&L in TypeScript (`use-portfolio-engine.ts`)
  and silently drops split/corporate-action/fee/tax transaction types
  (`engine-funds.ts`) instead of reading the authoritative Python engine. Also
  records missing realized-gains/tax-lot reporting, tamper-evident audit,
  attachments, year-end close, dimensions/budgets, fund administration and
  workflow surfaces, plus a phased roadmap.
  docs updated: `docs/accounting-finance-gap-analysis.md` (new, `status: Analysis`).

- **feat(portfolio): the Funds UI reads the authoritative engine, preserves
  every transaction type, persists cost basis, and reports tax lots.** Adds a
  pure `engine-stats.ts` mapper (`POST /portfolio/{id}/query` → widget stats) and
  `use-engine-fund-stats.ts` for the fund list, so workspace and cards show the
  engine's positions/cost basis/realized P&L instead of a second TypeScript lot
  engine; the client FIFO/LIFO path is now a labelled `PREVIEW` fallback. Adds
  `engine-transactions.ts`, which keeps splits, corporate actions, fees, taxes,
  crypto and transfer rows (the old adapter dropped all but five types and
  relabelled transfers). The cost-basis control persists
  `cost_basis_method` via `PATCH /portfolio/{id}` and offers FIFO/LIFO/
  AVERAGE_COST/HIFO/SPECIFIC_ID. Adds a **Tax Lots & Realized Gains** widget
  (open lots, realized matches, holding days) with CSV export, plus engine
  data-quality warnings. Tests: `engine-stats.test.ts`,
  `engine-transactions.test.ts` (22 passing with the existing engine suite),
  plus `python/tests/test_portfolio_api.py` pinning the tax-lot fields the
  widget reads and the persisted `cost_basis_method`; `npx tsc --noEmit` clean,
  full vitest 5,305 passing.
  docs updated: `docs/portfolio-accounting.md`,
  `docs/accounting-finance-gap-analysis.md`.

- **feat(accounting): tamper-evident hash-chained audit trails for both books.**
  Adds `python/harness/audit_chain.py` (SHA-256 over the previous hash + the
  event's immutable fields) and seals `LedgerAuditEvent` /
  `PortfolioAuditEvent`. Existing rows are sealed on store open (metadata only;
  content unchanged). New read surfaces: `ledger_audit_verify`,
  `portfolio_audit_log`, `portfolio_audit_verify` tools plus
  `GET /ledger/{id}/audit/verify`, `GET /portfolio/{id}/audit` and
  `GET /portfolio/{id}/audit/verify`. Verification returns `OK`, `UNSEALED`
  (legacy rows) or `BROKEN` with the first bad event id and
  `HASH_MISMATCH`/`PREV_HASH_MISMATCH`. Tests: new
  `python/tests/test_audit_chain.py` (clean, edited-row and deleted-row cases
  for both books) and the tool-count contract updated to 42.
  docs updated: `docs/ledger.md`, `docs/portfolio-accounting.md`,
  `docs/accounting-finance-gap-analysis.md`.

- **feat(ledger): source-document attachments with a frozen content hash.**
  Adds `LedgerAttachment` (artifact id + checksum + note, no copied bytes) and
  `ledger_attach_document` / `ledger_list_attachments` /
  `ledger_detach_document` / `ledger_verify_attachment`, plus
  `GET|POST /ledger/{id}/attachments`, `DELETE …/attachments/{id}` and
  `GET …/attachments/{id}/verify`. `get_entry` now returns `attachments`.
  Attach refuses a missing/unhashed artifact (`ARTIFACT_NOT_FOUND` /
  `ARTIFACT_UNHASHED`); verify reports `MATCHED`, `CHANGED` or `MISSING`.
  Attach/detach are audited and therefore sealed by the hash chain. Tests:
  new `python/tests/test_ledger_attachments.py` (6) + route coverage in
  `test_ledger_api.py`.
  docs updated: `docs/ledger.md`, `docs/accounting-finance-gap-analysis.md`.

- **feat(portfolio): realized-gains report by tax year and term, with
  wash-sale candidates.** Adds `portfolio/tax_report.py` (pure report over the
  engine's lot matches) and `PortfolioService.realized_gains`, exposed as the
  `portfolio_realized_gains` tool and `GET /portfolio/{id}/realized-gains`.
  Groups gains by tax year, currency and SHORT/LONG term (caller-supplied
  `long_term_days`, default 365), with per-currency totals and per-disposal
  proceeds/basis/gain/holding days. For a realized loss it reports
  same-symbol acquisitions within `wash_sale_window_days` as **candidates**
  (`washSaleSuspected`, `candidateDisallowedLoss`, replacement transactions)
  and explicitly does **not** apply a jurisdiction rule, disallow the loss or
  adjust replacement basis (`washSale.applied: false`). Currencies are never
  summed together. Tests: `test_portfolio_tax_report.py` (6) + route coverage
  in `test_portfolio_api.py`; portfolio tool count 43.
  docs updated: `docs/portfolio-accounting.md`,
  `docs/accounting-finance-gap-analysis.md`.

- **feat(ledger): indirect-method cash flow reconciled to the direct statement.**
  Adds `statements.cash_flow_indirect` (net income + depreciation/amortisation
  add-back + operating working-capital movement, with investing/financing from
  the direct statement). Working capital is selected by the account's declared
  `cash_flow_category` (ASSET/LIABILITY, non-cash, OPERATING), and the indirect
  operating figure is checked against the direct operating bucket. A difference
  (e.g. equipment bought on credit) is returned with the amount and
  `reconciled: false`, never plugged. `ledger_cash_flow` and
  `GET /ledger/{id}/cash-flow` accept `method=direct|indirect`; an unknown
  method is `INVALID_STATEMENT_METHOD` (422). Tests: new
  `test_ledger_cash_flow_indirect.py` (trading month reconciles, depreciation
  add-back, non-cash investing on credit reports the difference) + route test in
  `test_ledger_api.py`.
  docs updated: `docs/ledger.md`, `docs/accounting-finance-gap-analysis.md`.

- **feat(ledger): year-end closing entries and retained-earnings roll-forward.**
  Adds `ledger_year_end_preview` / `ledger_close_fiscal_year` and
  `GET /ledger/{id}/year-end/preview` + `POST …/year-end/close`. The closing
  entry is dated the first day of the next fiscal year, zeroes every revenue/
  expense account, and posts the balancing net income to the active
  `EQUITY/RETAINED` account (a net loss debits it). It is idempotent
  (`source=year_end_close`, `externalId=ye:<yearEnd>`; retry → `ALREADY_CLOSED`),
  and P&L readers (`income_statement`, indirect cash flow) omit closing rows
  while the trial balance / balance sheet include them. A year with no activity
  is `YEAR_END_NOT_READY`; a chart without a retained account is
  `RETAINED_EARNINGS_MISSING`. Tests: new `test_ledger_year_end.py` (preview,
  post/roll-forward, idempotency, reversal + fresh re-close, net loss,
  non-January fiscal year, refusals) + route coverage in `test_ledger_api.py`.
  docs updated: `docs/ledger.md`, `docs/accounting-finance-gap-analysis.md`.

- **feat(ledger): dimensions reporting, budgets vs actual, and cash runway.**
  Journal-line `dimensions` are now selectable in `store.postings` and reported
  by `ledger_dimension_summary` / `GET /ledger/{id}/dimensions/{dimension}`
  (cash lines excluded, unassigned explicit). Adds `LedgerBudget` +
  `LedgerBudgetLine`, `ledger_budget_create/list/variance` and
  `GET|POST /ledger/{id}/budgets` + `…/variance`: actuals are journal movements,
  assigned to the most specific account+dimension budget line, signed in the
  account's natural direction, with `favorable` and an explicit `unbudgeted`
  bucket. Adds `ledger_cash_runway` / `GET /ledger/{id}/runway`: trailing
  operating burn, open AR/AP from aging, runway months and a horizon projection;
  a surplus reports `UNKNOWN`, never infinity. Tests: new
  `test_ledger_budgeting.py` (6) + route coverage in `test_ledger_api.py`.
  docs updated: `docs/ledger.md`, `docs/accounting-finance-gap-analysis.md`.

- **feat(security): optional segregation-of-duties policies above the single-use
  approval layer.** Adds `harness/sod.py` (per-effect policy: amount threshold,
  required distinct approvers, requester exclusion) and operator-only `/sod/*`
  routes for policies, status and sign-offs. The ledger and portfolio authorizers
  read the collected sign-offs before consuming the normal single-use approval,
  emit `sod.pending` when blocked, and consume both gates on authorization.
  Default is off: with no policy the single-user flow is unchanged. There is no
  agent tool to sign off, so the agent cannot satisfy a rule that constrains it;
  a self-approval is `SOD_SELF_APPROVAL`. Tests: new `test_sod.py` (off-by-default,
  distinct approvers, duplicate/self approval, threshold tiers, deactivation,
  portfolio authorizer, routes).
  docs updated: `docs/ledger.md`, `docs/portfolio-accounting.md`,
  `docs/accounting-finance-gap-analysis.md`.

- **feat(portfolio): per-account cash and lot separation on PortfolioAccount.**
  Wires the existing table: `portfolio_create_account/list/update/delete` tools
  and `GET|POST /portfolio/{id}/accounts` + `PATCH|DELETE …/{accountId}`;
  `account_id` on transactions, tax lots and add/remove/update position. `read`
  returns `accounts[]` with cash/net deposits/dividends/open positions/open lots
  plus an explicit synthetic `Unassigned` bucket, and reconciles
  `accountCashTotal` to aggregate cash (`accountsReconcile`). An account-scoped
  SELL/oversell consumes only that account's lots; deleting an in-use account is
  `ACCOUNT_IN_USE`; duplicate external ids are `ACCOUNT_EXISTS`. Tests: new
  `test_portfolio_accounts.py` (4) + route coverage in `test_portfolio_api.py`;
  portfolio tool count 47. Real fund administration (NAV per unit, fees, capital
  accounts) remains an explicit product decision and is documented as such.
  docs updated: `docs/portfolio-accounting.md`,
  `docs/accounting-finance-gap-analysis.md`.

- **docs(scope): "fund" is a portfolio book; real fund administration is
  explicitly out of scope.** Product decision 2026-11-26: do not build NAV per
  unit, subscriptions/redemptions, management/performance fees, capital accounts
  or an investor registry unless the product direction changes. Per-account cash
  and lot separation remain in place as custody accounting.
  docs updated: `docs/portfolio-accounting.md`,
  `docs/accounting-finance-gap-analysis.md`.

## 2026-11-25

- **fix(desktop): the boot splash keeps its orbit mark, and the update panel
  names the running version.** Restores the intentional boot orb (a lit conic
  ring around a dark core) in `desktop/ui/index.html`; the offline screen keeps
  the static brand mark instead, so the two states stay distinct. The title-bar
  update icon stays icon-only, and the panel it opens now shows a
  **Current version** row (`v<current>`) above the actions, so the version in use
  is visible where updates are handled.
  no doc change — presentation-only; the shell and update contracts are
  unchanged.

- **fix(desktop): a failed navigation shows the Black Eclipse offline screen,
  never WebView2's Microsoft/Edge error page.** WebView2 substitutes its own
  branded `chrome-error://` document when a navigation fails, and the app
  document is already gone, so `show_failure_overlay` could not style it. On
  Windows the shell now subscribes to `NavigationCompleted` (`webview2-com`,
  same version Tauri already resolves) and navigates back to the bundled splash
  with `#offline=local|offline|timeout|other`; the splash (`desktop/ui/index.html`)
  renders the real inlined brand mark, a reason-specific message, and a
  `desktop_reopen` retry that probes the loopback port before restarting the
  supervisor. `on_navigation` now cancels every non-app URL and opens http(s)
  links in the OS browser, and `on_new_window` denies the window and forwards
  externally, so the desktop window can never render a remote (or failed) page.
  The Live Intelligence widget no longer mounts a remote player while
  `navigator.onLine` is false, which removes the same error page from inside the
  card, and a root-mounted `OfflineBanner`
  (`src/components/layout/offline-banner.tsx`) shows one calm app-wide
  "You're offline" notice while local features keep working. A 15 s
  `/api/updates` watchdog in the shell replaces the page after three unanswered
  probes, so a wedged Next server (which never exits the child) cannot leave a
  dead document on screen. Tests: `navigation_tests` (origin/bundled-asset/
  reason mapping, splash contract, HTTP probe incl. a silent socket) in
  `desktop/src-tauri/src/main.rs`. Also fixes the release payload: Next's
  standalone tracing copied local build/release trees from the checkout root
  (`releases/` 1.9 GB, `.runtime/` 948 MB, `.next-desktop-dev/` 175 MB), which
  inflated the payload to 3.2 GB and made the 32-bit NSIS compressor fail with
  `Internal compiler error #12345`. `scripts/desktop-payload.js` now prunes them
  (payload 3211 MB → 210 MB); `--prune-only` re-prunes an assembled payload.
  Docs updated: `desktop/README.md`, `docs/desktop-app-plan.md`,
  `docs/desktop-cross-platform-plan.md`, `docs/data-and-privacy.md`,
  `docs/NAVIGATION.md`, `docs/CHANGELOG.md`.

- **docs(updates): record the published v0.4.30 manifest.** Signed offline against
  the CI build (`vg7kNYrOIlBSOn-VzBotG`, lockHash `c26e37a1…`) and the win32-x64
  installer (`195,428,381` bytes, sha256 `086f42f8…`), uploaded `stable.json` to
  the draft, published, and verified: `node scripts/update.js --check` reports
  `latest signed release: v0.4.30 / already up to date`. docs updated:
  `docs/UPDATES.md` (release state), `docs/CHANGELOG.md`.

- **release: v0.4.30.** Version bumped in `package.json`, `package-lock.json`,
  `pyproject.toml`, `python/harness/__init__.py` and the README's current-version
  line; SBOM and published benchmarks regenerated. Ships the presentations
  subsystem (typed `DeckOp` copilot, PDF/PNG export, thumbnails, `.pptx` import),
  the video player/type-system pass, serial transcript writes and the
  cold-runtime boot fix. The `v0.4.30` tag drives `desktop-release.yml`; the
  offline-signed `stable.json` is uploaded to the draft before publishing.
  docs updated: `docs/UPDATES.md` (release state), `docs/CHANGELOG.md`.

- **feat(presentations): deck copilot, PDF/PNG export, thumbnails and import.**
  Completes the presentation subsystem: (1) the Canvas AI panel now edits decks
  through typed `DeckOp` proposals (`canvas/edits.py` branches on
  `CanvasKind.PRESENTATION`, returns `deckOperations`, and the canvas save path
  validates and applies them by kind; text ops are refused); (2) `format: pdf`
  renders one PDF page per slide and `format: png` returns a ZIP of per-slide
  PNGs, both via the vendored Matplotlib backend, with the Export menu offering
  all three; (3) every deck write refreshes a rendered first-slide thumbnail
  (`ArtifactService.set_preview_thumbnail`); (4) `presentation_import` turns a
  `.pptx` artifact into an editable outline (titles/text/notes, honest dropped-
  design warnings); (5) `document_create(format="pptx")` now delegates to the
  same deck exporter. Adds tests for AI deck proposals, PDF/PNG, thumbnails and
  import; the JSON prompt char ratchet is re-pinned for the deck guidance. Docs
  updated: `docs/harness/presentations.md`, `docs/presentations-plan.md`,
  `docs/canvas.md`, `docs/harness/document-intelligence.md`,
  `docs/harness/artifact-center.md`, `docs/NAVIGATION.md`, `docs/CHANGELOG.md`,
  `docs/harness/tool-registry.json`.

- **feat(presentations): a dedicated deck subsystem — editable artifact, live
  preview, present mode and real `.pptx` export.** A presentation is now a
  versioned `DeckSpec` Canvas artifact (`kind=presentation`,
  `application/vnd.eclipse.deck+json`) rendered by one neutral layout engine;
  the PowerPoint file is a derivative. Ships `python/harness/presentations/`
  (schema + limits, four theme presets, deterministic fit, fractional 13-layout
  engine, neutral render model with honest chart/image resolution,
  `python-pptx` export with speaker notes/tables/native charts/images, closed
  deck ops, `PresentationService`, and the `presentation_create` / `_edit` /
  `_export` / `_outline` tools), the Canvas slide editor (rail, stage,
  inspector, fullscreen present) and an Export PowerPoint action that registers
  a new artifact and writes the file to Documents/Eclipse, the `/deck`
  agent-turn command (with prompt guidance so a deck request routes to
  `presentation_create`, not the one-shot writer), the created file handed to
  the chat like any authored document, a readable deck outline preview in the
  Artifact Center, and
  the `/presentations` theme+export routes with their front-door proxy. Data
  honesty is structural: a named market source is resolved once and stored, and
  an unresolved chart/image is an explicit UNAVAILABLE slide; text that cannot
  fit is reported as `overflowSlides`, never clipped. (The follow-up bullet above
  adds the copilot, PDF/PNG, thumbnails and import.) Docs updated:
  `docs/harness/presentations.md` (new), `docs/presentations-plan.md`
  (implemented status), `docs/NAVIGATION.md`, `docs/canvas.md`,
  `docs/harness/document-intelligence.md`, `docs/harness/artifact-center.md`,
  `docs/harness/slash-commands.md`, `docs/GLOSSARY.md`, `docs/DECISIONS.md`,
  `skills/ARCHITECT.md` (ADR-085), `docs/harness/tool-registry.json`,
  `docs/CHANGELOG.md`.

- **fix(chat): transcript writes are serialized instead of flooding the origin.**
  A first run migrates every chat held only in localStorage; those upserts fired
  in parallel. Live inspection of the app showed up to 74 queued
  `/api/chat/transcript` POSTs competing with the boot's own fetches for the
  browser's ~6 connections per origin. All transcript writes/deletes now share
  one serial lane (`src/lib/chat/serial-lane.ts`): one request at a time, order
  preserved, a failure releases the lane. Tests:
  `src/lib/chat/serial-lane.test.ts`. no doc change — internal transport fix,
  contracts unchanged.

## 2026-11-24

- **fix(chat): a cold runtime boot can no longer look like “Eclipse is broken”.**
  Three separate failures made the first message after a restart produce nothing:
  (1) the window was served by a server that restarted underneath it → a bare
  `Failed to fetch`; (2) the 3 s request budget expired while the Python runtime
  was still importing → a hard `RUNTIME_UNAVAILABLE` telling the user to install
  a runtime that was merely starting; (3) the pre-model repo map ran
  synchronously on the event loop and took **137 s** against its 5 s budget,
  freezing every route (health included) until the user cancelled. Now the front
  door answers `503 RUNTIME_STARTING` while a start is still in flight — a new
  code, distinct from `RUNTIME_UNAVAILABLE` — and the chat POST retries that,
  plus a transport `Failed to fetch`, with backoff for up to 120 s under a
  visible “Starting Eclipse…” status (the run id is client-owned, so the retry
  cannot start a second run). The repo map/seed build now runs in a worker
  thread under a hard `budget + 2 s` timeout, `build_repo_map` checks its
  deadline per file, and a single file over 1 MB is skipped rather than parsed.
  docs updated: `docs/ARCHITECTURE.md`, `docs/harness/code-intelligence.md`,
  `docs/CHANGELOG.md`.

- **docs(presentations): add the dedicated deck subsystem plan.** New
  `docs/presentations-plan.md` designs presentations as a first-class artifact:
  a versioned `DeckSpec` JSON stored through the existing Canvas/Artifact Center
  (new `CanvasKind.PRESENTATION`), one neutral normalized layout model rendered
  by both React (preview/present) and python-pptx (export), a deterministic
  fit ladder with honest overflow reporting, native charts where 1:1 and the
  existing Python chart engine otherwise, and UNAVAILABLE instead of invented
  data. Includes the option analysis (deck JSON vs OOXML vs response envelope),
  file-by-file change map, five phases with acceptance criteria, tests, risks,
  non-goals and the decisions still open. Implementation status: proposed only;
  nothing in the tree changed but this plan and its NAVIGATION entry. Docs
  updated: `docs/presentations-plan.md`, `docs/NAVIGATION.md`.

- **feat(video): one real video player + playback hardening.** Replaced the bare
  `<video controls>` elements in the chat job card, the Artifact Center, inline
  run artifacts and response artifact cards with one shared `VideoPlayer` (poster, explicit loading/error
  states, retry, download, open-in-browser fallback, keyboard, mute/volume,
  speed, fullscreen, pause-offscreen, one-player-at-a-time). Playback transport
  now always knows its length: the artifact route sets `Content-Length` on the
  plain 200 path, honours `If-Range`, and the Next proxy forwards it and retries
  a dropped media GET once. Final renders are remuxed `-movflags +faststart`
  (best-effort, recorded in the render result) so the moov atom leads the file.
  The worker's shared background gained an accent glow, grid and vignette, and
  the header rail now grows in; the video poster now samples a content frame
  just over a third into the clip (duration-aware, with the old fixed fallbacks)
  instead of always the opening. The worker now links Inter + JetBrains Mono
  (SIL OFL) and registers them with fframes, so layout is deterministic across
  hosts instead of resolving Segoe UI/Consolas from the OS. Docs updated:
  `docs/video-pipeline.md` (Playback section), `docs/CHANGELOG.md`.

- **readme: drop the Pine engine licence badge.** Removed the `pine engine / AGPL-3.0-only`
  shields.io badge from the root `README.md` badge row (badges now: core license, Next.js,
  Python). The AGPL boundary itself is unchanged and still stated in the licence section and
  `pine-engine/NOTICE.md`. no doc change — presentation-only edit, no contract or setup change.

## 2026-11-23

- **docs(updates): record the published v0.4.29 manifest.** Signed offline against the CI
  build (`kHVPAsdQeCTARLWbi2uCa`, lockHash `589fd452…`) and the win32-x64 installer
  (`195,129,411` bytes, sha256 `8b224c74…`), uploaded `stable.json` to the draft, published,
  and verified end to end: a 0.4.28 window reports `available: true`, stages 0.4.29, and
  *Restart* (`--restart-only`) reopens on 0.4.29 with `available: false`. docs updated:
  `docs/UPDATES.md` (release state), `docs/CHANGELOG.md`.

- **release: v0.4.29.** Version bumped in `package.json`, `package-lock.json`, `pyproject.toml`,
  `python/harness/__init__.py` and the README's current-version line; SBOM and published
  benchmarks regenerated. Ships the Restart fix below: the shell now reopens the promoted
  release instead of re-promoting the checkout's `.next`, and `desktop-refresh.js --no-build`
  without `--from` refuses to downgrade. docs updated: `docs/UPDATES.md`, `docs/CHANGELOG.md`.

- **fix(desktop): Restart Eclipse reopens the promoted build, not the checkout's stale one.**
  After an in-app update, *Restart Eclipse* spawned `desktop-refresh.js --no-build`, which
  promotes `channel.buildDir` (`.next`). That is the user's working tree, not the release the
  updater had just verified and promoted — so it moved `current.json` back to the older build
  (0.4.27 after staging 0.4.28), reopened on it, and the update notice never cleared. The shell
  now spawns `desktop-promote.js --restart-only`, which relaunches onto whatever `current.json`
  already names and promotes nothing. `desktop-refresh.js --no-build` without `--from` delegates
  to the same reopen, so a shell that still invokes it (0.4.28 or older) can no longer downgrade
  the pointer. `desktop-dev-bridge.js` also writes a `desktop-promote.js` shim so bridged
  installs can run it. docs updated: `docs/UPDATES.md` (restart contract, file map),
  `docs/CHANGELOG.md`.

- **release: v0.4.28.** Version bumped in `package.json`, `package-lock.json`, `pyproject.toml`,
  `python/harness/__init__.py` and the README's current-version line, with the SBOM regenerated.
  Ships today's four fixes/features: charts that render through the running runtime's health
  (label axes, booting-runtime retry), real absolute file paths plus the `file_locate` tool and
  shell-owned desktop downloads, and the trading pass (portfolio heat, deterministic stop
  management, unattended renewal, and the `BOT_VERSION_STALE` renewal refusal). The `v0.4.28`
  tag drives `desktop-release.yml`; the offline-signed `stable.json` is uploaded to the draft
  before publishing. docs updated: `docs/UPDATES.md` (release state), `docs/CHANGELOG.md`.

- **docs(updates): record the published v0.4.28 manifest.** Signed the manifest with the
  offline key (`47e545f302249385`) against the CI-built `BlackEclipse-Build-0.4.28.zip`
  (buildId `X2Jp5YF-vG-pqKGM_60ZV`, lockHash `d8c5a3a8…`) and the Windows installer
  (`195,136,365` bytes, sha256 `742c815e…`), uploaded `stable.json` to the draft, and
  published the release. `node scripts/update.js --check` reports `v0.4.28`, and the
  local `/api/updates` flips to `available: true` for a 0.4.27 window. docs updated:
  `docs/UPDATES.md` (release state), `docs/CHANGELOG.md`.

- **fix(files): a produced file now has a real path, and the agent can find it.**
  Two complaints, one root cause: nothing ever told the model where a file
  landed. `eclipse_report`/`document_create` wrote only into the content-addressed
  Artifact Center (`.black-eclipse/artifacts/…`), so "where is my PDF?" could
  only be answered with a vague folder; and the chat fs tools are confined to the
  bound project, so a file in Downloads/Documents could not be located at all.
  Now `documents/outputs.py::publish` writes a readable copy into
  `Documents/Eclipse` (shell-resolved known folder, `ECLIPSE_OUTPUT_DIR`
  override; `saveTo` = documents/downloads/desktop/absolute folder), and both
  tools return `{"path": "<absolute>"}` plus "Saved at <absolute path>" in the
  tool text. New `file_locate` tool (`documents/locate_tools.py`) searches the
  export folder, Downloads, Documents, Desktop and the bound workspace by name
  fragment/glob, extension and age, newest first, bounded (depth 4, 60k entries,
  skip list) and honest on a miss (it names the folders searched). Code-tool
  writes (`write_file`, `create_file`, edits/patches) now also return
  `absolutePath` and append "Absolute path: …" (Hermes does the same). Chat
  prompt says to name the absolute path, never "it's in your documents".
  Tests: `python/tests/test_file_outputs.py`, `test_reports.py`,
  `test_change_event_previews.py`, plus the machine-checkable
  `scripts/verify_file_outputs.py` (writes a real PDF through the tool handler,
  prints the absolute path, re-finds it with `file_locate`). Docs updated:
  `docs/harness/reports.md`, `docs/harness/document-intelligence.md`,
  `docs/NAVIGATION.md`, `docs/CHANGELOG.md`.
- **fix(desktop): download buttons actually download, and top-bar links open.**
  WebView2 in the Tauri shell has no download shelf, so `<a download>` and
  `blob:` downloads (response JSON/MD/PDF, Artifact Center, chart PNG, 3D exports,
  interactive HTML) could silently do nothing; `target="_blank"` anchors (the
  title-bar update panel's installer link and "What's new") are new-window
  requests the single-window shell does not host. The shell now owns both:
  `be-save-download` (name + base64 → a real file in the OS Downloads folder,
  collision-safe, revealed in Explorer, absolute path echoed on
  `be-download-saved`) and `be-open-external` (http/https only → the real
  browser). Client helpers `src/lib/desktop/download.ts` and
  `external.ts` are wired into every download surface and the update UI; a
  browser without the bridge keeps the normal anchor. Rust unit tests cover
  base64 decoding, name escaping and collision naming. Docs updated:
  `desktop/README.md`, `docs/CHANGELOG.md`.

- **fix(charts): charts render again — the front door asks the RUNNING runtime
  whether it can draw, instead of a separately resolved interpreter.** Every
  chart 503'd with "the chart engine cannot draw — ... is missing matplotlib,
  numpy, plotly, pydantic" while `GET /charts/health` on the live runtime
  reported all four. The blocking build resolved the POSIX vendored path
  `scripts/vendor/python/bin/python3` (a build from an older revision), so the
  capability probe failed even though the runtime could draw. `probeChartEngine()`
  now calls the runtime's own `/charts/health` first (the drawing process is the
  authority; `chartEngineReadinessFromHealth()` maps it) and falls back to a
  local interpreter probe only when the runtime is silent. `findPython()` only
  prefers the vendored interpreter when the file exists. Verified end to end:
  `POST /api/harness/charts` returns 200 with a figure, a live Plotly chart and
  a downloadable PNG artifact, and the desktop pinned build was refreshed so the
  window serves it. Tests: `src/lib/harness/runtime-config.test.ts` (health
  mapping), the charts route suite. Docs updated: `docs/chart-engine.md`,
  `docs/CHANGELOG.md`.

- **fix(charts): a scene mark's `x` may be labels.** A scene is the
  compositional form the chat uses for "any chart", and writing a bar chart as
  `x: ["Q1","Q2","Q3"]` is how a person writes one — but the matplotlib scene
  renderer coerced every `x` to `float`, so the PNG fallback failed the whole
  render with `could not convert string to float: 'Q1'` (a 500, no chart). Labels
  now map to their index on a panel-wide shared category axis (ticks show the
  labels), a non-numeric `y` is a gap instead of a crash, and a regression is
  never fitted over labels. Verified live: the scene bar+line over `Q1…Q3`
  returns 200 with both traces. Tests: `python/tests/test_chart_scene.py`.
  Docs updated: `docs/chart-engine.md`, `docs/CHANGELOG.md`.

- **fix(charts): a booting Python runtime is waited out — "run
  `npm run setup:python`" can no longer appear while a chart is rendering.**
  After a launch or a source-change restart, the chart request landed inside the
  runtime's cold start; one page request waits only 3 s by design (long waits
  starve the browser's sockets), so the route answered `RUNTIME_UNAVAILABLE`
  with the install command for a runtime that was already installed and
  importing. `POST /api/harness/charts` now distinguishes an installed-but-
  booting runtime (`RUNTIME_STARTING`, `Retry-After: 2`) from a missing
  interpreter (the only case that keeps the repair instruction), and the chart
  surface shows "Starting the chart engine…" and retries with backoff for
  several minutes instead of rendering an error box
  (`isRetryableChartError`); a "Try again" control covers the manual case.
  Tests: `route.test.ts` (starting vs missing), `charts.test.ts` (retry
  classification). Docs updated: `docs/chart-engine.md`, `docs/CHANGELOG.md`.

- **feat(trading): portfolio heat, concentration clusters and a drawdown
  governor.** Added `harness/trade/heat.py`: open risk at stops (heat) plus the
  planned order, deterministic FX/crypto clusters (`FX:USD`, `CRYPTO:USDT`,
  metals), a drawdown risk multiplier floored at `minRiskScale`, volatility-
  targeted stop distance and a caller-supplied event blackout. `trade_plan` now
  returns `heat`, `riskScale`, `riskPctEffective` and `drawdownPct`, applies the
  governor before sizing, and refuses on a blocking heat/cluster/event verdict.
  Extended `config.risk` with `maxHeatPct`, `maxClusterRiskPct`,
  `maxClusterPositions`, `maxDrawdownPct`, `minRiskScale` and `targetStopVolPct`
  (validated + exposed in Settings › Trading). Tests:
  `python/tests/test_trade_heat.py` plus the trade suites. Docs updated:
  `docs/trading.md`, `docs/ARCHITECTURE.md`, `docs/CHANGELOG.md`.

- **fix(desktop): "Restart Eclipse" can no longer leave a black, dead window.**
  The restart promoted the build with `--no-restart`, then on the explicit
  restart killed the shell and relaunched it immediately. Tauri's
  single-instance plugin handed that launch to the still-exiting old process, so
  no new window ran its bootstrap and the app sat on a black splash with no
  server — the reported "upgrade blacks out my desktop instance". `restart()` now
  waits for the old shell to exit (`waitForShellExit`), confirms the relaunched
  process survived, and retries once. Also fixed bridged installs: the dev
  bridge wrote only the `desktop-server.js` shim, so the shell's
  `update-apply.js` / `desktop-refresh.js` spawns targeted files that did not
  exist and *Apply update* / *Restart* silently did nothing; all three shims are
  written now (applied to this machine's install). Removed stale
  login-autostart leftovers from the deleted browser lane
  (`BlackEclipse.cmd` → a deleted `release-v0.3.2` tree, `BlackEclipse Front
  Door.vbs` → deleted `scripts/local-proxy.js`); both renamed to
  `.disabled-legacy` in the user's Startup folder. Docs updated:
  `docs/UPDATES.md`, `docs/environments.md`, `docs/CHANGELOG.md`.

- **feat(trading): automatic stop management + persistable risk config.** Added
  `harness/trade/management.py`: a deterministic policy engine (`evaluate_management`)
  for break-even (+buffer), one-way ATR / percent trailing, a giveback profit
  floor, time stops and partial takes, plus `PositionManager`, which applies only
  risk-reducing actions through the existing `modify_position` / `close_position`
  paths. `harness/trade/management_api.py` adds an opt-in sweep/loop
  (`TRADING_MANAGEMENT_SECONDS`, default 0) wired in `boot.py`. Settings › Trading
  now has a Stop management editor.
  Also fixed a real bug: `validate_config` rejected the `config.risk` and
  `config.supervisor` blocks that `trade/risk.py` and `trade/supervisor.py` read,
  so the account-fit and portfolio-supervisor limits always ran on their defaults;
  both blocks are now allowlisted and sanitised (finite, non-negative, unknown
  keys dropped). Tests: `python/tests/test_trade_management.py` (17) and the
  trade/broker suites (120 passed). Docs updated: `docs/trading.md`,
  `docs/ARCHITECTURE.md`, `docs/CHANGELOG.md`.

- **fix(runtime): a half-installed Python can no longer be selected as the agent
  runtime.** Chat failed with `No module named 'langchain_openai'`: the running
  app had picked a system Python that could import `langgraph` but had none of
  the provider packages, so every model call died on import. Installed the
  missing provider/chart packages into that interpreter and restarted the
  harness (healthy again). Root cause fixed: `PYTHON_RUNTIME_DEPS` now probes
  `langgraph, langchain_openai`, and `scripts/setup-python.js` +
  `scripts/doctor.js` use the same marker, so a partial install is repaired
  instead of being accepted as "already installed". Added a regression test for
  the marker. Docs updated: `docs/chart-engine.md`, `docs/CHANGELOG.md`.

- **fix(trading): autonomous bots now renew their own arm and mandate.** Auto
  mode promised renewal, but nothing renewed the 8 h live arm or the 24 h
  mandate, so an unattended bot silently stopped until a human re-armed it.
  `python/harness/trade/renewal.py` extends the arm only while it is still valid
  (an expired arm still needs the operator's typed phrase) and replaces a
  due/expired/exhausted mandate while keeping the same bot pin, clamped down to
  the live ramp stage. The old grant is revoked only after the replacement exists,
  and submit reads the refreshed trial. `GET /automations/{id}/trading` now
  reports the `authority` countdowns and `POST /automations/{id}/trading/renew`
  forces a renewal; the bot panel shows both. Also fixed the duplicate
  `strategyVersion` field in `PrepareBody` and a stale-trial read after renewal.
  Tests: `python/tests/test_trade_renewal.py` (8) and the bot e2e suite.
  Docs updated: `docs/trading.md` (unattended renewal), `docs/CHANGELOG.md`.

- **fix(trading): the bot's `maxSymbols` setting is now real.** The form offered a
  "scan up to N symbols" field, but the backend ignored it and hard-capped every
  bot at 8. `normalize_symbols(limit=...)` now honors `config.maxSymbols` up to a
  32-symbol hard ceiling in the runner, the scan endpoint, validation and the
  standing-consent comparison (order-independent). A zero, negative or malformed
  value falls back to the default instead of silently shrinking the universe.
  Tests: `python/tests/test_trading_policy.py` (limit/clamp + 12-symbol consent).
  Docs updated: `docs/trading.md` (universe), `docs/CHANGELOG.md`.

- **fix(prompts): repaired a literal Windows path that blocked the Python suite.**
  An in-flight prompt edit embedded `C:\\Users\\...` in a normal triple-quoted
  string, so `\U` was an invalid escape and `python/harness/prompts.py` failed to
  import, aborting collection for 20 test modules. Escaped the backslashes;
  `ast.parse` and `import harness.prompts` now succeed. This is a typo repair in
  an unrelated in-flight file, not a trading change.
  no doc change — syntax repair only (CHANGELOG entry).

- **chore: cleared the four unrelated full-suite failures left by the in-flight stream.**
  The computer-use test still stubbed `_session_of` with the old one-argument
  signature; both prompt-ceiling ratchets were stale (47371/47885 vs the measured
  48205); and the generated `docs/harness/tool-registry.json` / `benchmarks.md`
  had drifted. Updated the stub, re-pinned both ratchets to the measured 48205,
  and regenerated both artifacts with their documented commands
  (`npm run eval:registry`, `npm run eval:benchmarks`). Not a trading change.
  no doc change — generated artifacts + test ratchets only (CHANGELOG entry).

- **fix(trading): auto-renew only runs for a bot-driven order.** The renewal hook
  fired for every `run_once`, so a human-prepared trial (no automation id) could
  be blocked by the renewal call and a broker double had to answer it.
  `run_once` now calls `ensure_authority` only when `automation_id` is present;
  manual trials and graph-run orders keep the old exact path.
  Tests: `test_quant_live` (the regression), `test_trading_bot_e2e`,
  `test_trade_renewal` (9). Docs updated: `docs/trading.md` (the manual-trial
  boundary), `docs/CHANGELOG.md`.

- **fix(evaluation): the RSL prompt cap admits the production prompt again.**
  `HarnessSnapshot.MAX_PROMPT_CHARS` was 48,000 while the measured production
  prompt is 48,205, so `test_snapshot_admits_the_production_agent_prompt` failed.
  Raised the cap to 50,000 (still a hard ceiling) and recorded the measured
  baseline in the comment. no doc change — guard constant (CHANGELOG entry).

- **chore: normalized a mixed line ending in `src/lib/harness/charts.test.ts`.**
  An in-flight edit left one CRLF line in an otherwise-LF tracked file, which the
  `gitignore-guard` line-ending test rejects. Rewrote the file with LF only.
  no doc change — formatting repair in an unrelated test file (CHANGELOG entry).

- **fix(trading): renewal refuses after an edit instead of silently re-arming.**
  Verifying the live UI on a real demo bot showed `Renew now` minting a mandate
  pinned to the bot's NEW version while the trial was still pinned to the old
  one — the documented rule is that an edit invalidates the grant.
  `ensure_authority` now raises `BOT_VERSION_STALE` before creating anything when
  the trial's pinned version differs from the bot's current version, matching the
  run path. Tests: `test_trade_renewal` (10).
  Docs updated: `docs/trading.md` (renewal boundary), `docs/CHANGELOG.md`.

- **release: v0.4.27 published — the signed update manifest is live.** `Desktop release` built the
  Windows installer (`BlackEclipse-Setup-0.4.27.exe`, 195,084,048 bytes, sha256 `9c4acc15…`), the
  `-full` installer, the prebuilt `BlackEclipse-Build-0.4.27.zip` (80,021,739 bytes, buildId
  `qNUlhzNwPDc-CUqdH8B2l`) and the packs into a draft; `stable.json` was then signed offline with key id
  `47e545f302249385` against tag commit `1140efa9`, uploaded to the release, and the release promoted to
  latest. `node scripts/update.js --check` now reports *latest signed release: v0.4.27*, so installs on
  0.4.26 see the update. Uses the documented order: sign -> upload -> publish.
  docs updated: `docs/UPDATES.md` (release state), `docs/CHANGELOG.md`.

- **chore(docs): rotate the oldest changelog section into `docs/changelog/2026-11.md`.**
  Adding today's section put the file at 8 dated sections, over the 7-section window that
  `src/lib/changelog-guard.test.ts` enforces (it failed CI's Vitest step). `npm run rotate:changelog --
  --write` moved the oldest section; nothing was deleted.
  docs updated: `docs/CHANGELOG.md`, `docs/changelog/2026-11.md`.

- **release: v0.4.27.** Version bumped in `package.json`, `package-lock.json`, `pyproject.toml` and
  `python/harness/__init__.py`. Ships the broker order-execution surface for all 19 adapters, the
  computer-use state-of-the-art pass, the graph-studio port/broker parity, and the CI repair above
  (lint config, size budget, SBOM). The `v0.4.27` tag drives `desktop-release.yml`; the offline-signed
  `stable.json` is signed and uploaded to the draft before publishing. The tag was re-pointed to the
  follow-up commit that regenerates `sbom.cdx.json` / `THIRD-PARTY-NOTICES.md` for the new version (the
  public-release preflight failed on the bump commit) and updates the README's current-version line.
  docs updated: `docs/UPDATES.md` (release state), `docs/CHANGELOG.md`, `README.md`.

- **fix(sdk): cleared the test-page backlog out of the main sidebar.**
  The live `agent-memory.db` held 66 agent-generated test blueprints that rendered as
  saved-page entries in the main nav (Locked SDK page 1–20, API SDK page 1–5, and the
  Order/Preview/Reject/Iconic/Portable/Engineered/Portfolio E2E/Main/API Desk families).
  Removed those 66 `UiBlueprint` rows, their 135 revisions and all 20 `SdkLock` rows
  (no passphrase needed: this is owner-side runtime data cleanup, not an SDK unlock),
  leaving the 33 `/dashboard/dash-*` mirror blueprints that the nav deliberately hides.
  Rows were exported first to `.black-eclipse/backups/sdk-pages-removed-2026-11-23.json`.
  No source change; the saved-pages nav now renders nothing until a real page is authored.
  no doc change — runtime data cleanup, contracts unchanged.

- **fix(ci): lint loads again, the harness size budget is re-pinned, and the SBOM matches the lockfile.**
  CI had been failing at the Lint step: the global `minimatch@11` override also applied to
  `@eslint/eslintrc`, which imports minimatch's CJS default and crashed with
  `does not provide an export named 'default'`. The override now keeps `minimatch@11.0.9` everywhere except
  `@eslint/eslintrc`, which is pinned by a nested override to `^3.1.5` (the version it declares), so
  `npx eslint .` runs. With the config loading again, two real errors surfaced and are fixed: an unescaped
  apostrophe in `trading-settings.tsx` (`react/no-unescaped-entities`) and a `require("os")` in
  `runtime-config.test.ts` (`@typescript-eslint/no-require-imports`, now a top-level `os` import).
  Regenerated `sbom.cdx.json` and `THIRD-PARTY-NOTICES.md` for the lockfile change
  (`node scripts/generate-sbom.js`), and re-pinned `scripts/size-budget.json` to the measured tree
  (563274 total; `.` 41816, quant 28371, code 23796, routers 24899, eclipse_memory 13314, skills 11586,
  evaluation 13631) because the Vibe-Trading, coding-parity, plugin-kernel, signed-updates and
  broker-execution/computer-use work had landed without a pin. The per-module ratchet in
  `python/tests/test_size_budget.py` was brought back to the measured sizes: `service.py` 2112 -> 2121,
  `trade/service.py` 1226 -> 1241, and `agents.py` (1211) / `computer/core.py` (1303) added as pinned
  split candidates.
  Verified: `node scripts/size-budget.js` passes; `test_size_budget.py` 4 passed; `npx eslint .` clean;
  `scan-secrets` clean; `tsc --noEmit` clean; SLO, conformance and external gates pass.
  `eslint.config.mjs` also ignores `.runtime/**` now: a single in-app update stages a whole dependency tree
  there (~22k JS files), so a local `npx eslint .` walked it for over an hour and reported warnings inside
  vendored packages. CI is unaffected (a fresh checkout has no `.runtime`), but the local gate is usable again.
  docs updated: `docs/CHANGELOG.md`.

## 2026-11-22

- **fix(computer-use): a crashed OSWorld agent cannot pass, and a failed capture attaches no media.**
  `run_osworld_episode` now exposes `agentCrashed` and reports `success` only when the agent run completed
  cleanly AND the environment score is positive; the environment score is still returned separately, so a
  lucky final state is visible but never counted as the agent's pass. Strengthened the test and added a
  clean-pass test and a clean-zero-score test. Added the missing regression test that a failed capture
  (permission denied after an earlier successful capture) carries no `media` in its tool result. Also added a
  `focus` action (UIA SetFocus, no click) for keyboard input when a control is obscured, and made the local
  benchmark deterministic (12/12 across three consecutive runs; the flaky synthetic typing task was replaced
  with a window-listing task, with typing covered by unit tests). Verified: 17 audit tests, eval + tools
  suites, full related suite green; `npx tsc --noEmit` clean.
  docs updated: `docs/harness/computer-use.md`, `docs/CHANGELOG.md`.

- **fix(computer-use): post-implementation audit defects (memory writes, masking offset, dead vision rung, branch leakage, dry-run, staleness).**
  Fixed the defects found by the audit: `store_workflow` now uses the real `structured=` keyword (auto memory and
  learned adapters were silently dead); sensitive-region masking subtracts the frame origin and scale (it was
  blacking the wrong pixels and leaking the secret); vision-grounding results carry `name` so the grounder can
  score them (the rung was dead); best-of-N branch sessions suppress shared memory/adapter writes and only the
  winner is learned; security profiles apply to window-less vision/OCR captures (focused window resolved); the
  masked image is what the describer sees, and sensitive text/element values are redacted; clipboard reads are
  redacted and marked untrusted; `drag`/`wait`/`note`/`done`/`fail`/`clipboard_get` honour a per-call dry run;
  `trajectory_summary` reports a loop from two identical signatures; element drags validate stale handles;
  `set_value` dry runs no longer echo the value; `uncertain` verification is no longer recorded as success; a
  failed capture no longer attaches the previous frame; learned/app identity strips display suffixes; best-of-N
  cleans up sessions on partial creation/observer failure; `forget` stops live selection; the base `activate`
  accepts `value`; `close` reports a failed close as retry. Also fixed invoke verification order (capture
  before invoke) and the typing-settle path in the benchmark. Evidence: 15 new audit-regression tests pass;
  **209 passed** across the related suite; `[windows-core] pass@1 = 12/12 (1.000)`; `npx tsc --noEmit` clean.
  docs updated: `docs/harness/computer-use.md`, `docs/CHANGELOG.md`.

- **fix(broker): the Settings › Broker connection panel now offers every registered broker, not just MT5.**
  The add form and the per-connection editor reuse the sidebar dialog's `ConnectionForm`, so the
  broker-type dropdown lists the registry's kinds (with per-host availability and the unavailable
  reason) and the credential / endpoint / quote-asset fields follow the selected kind. Both surfaces
  now default to the first *available* kind instead of a disabled `mt5`, a blank label follows the
  picked broker, and the sidebar's add fallback no longer borrows the selected connection's name.
  `npx tsc --noEmit` clean; vitest 5235 pass; desktop build refreshed. The Python suite still has
  four pre-existing failures in unrelated in-flight areas (computer-use lambda signature, prompt
  ceiling, file-size ratchets in `agents.py`/`service.py`/`computer/core.py`/`trade/service.py`).
  docs updated: `docs/trading.md` (setup step + registry bullet), `docs/CHANGELOG.md`.

- **feat(computer-use): 12-task local benchmark CLI + Windows application identity for every window.**
  `python scripts/cua_benchmark.py` runs the shared `LocalWindowsSuite` (real Windows driver, owned Win32
  window) and prints pass@1; the suite grew from 6 to 12 tasks (click, UIA invoke, set-value, typing,
  wait-for, clipboard, drag, press-and-hold, scroll-into-view, region capture, learned interface). The pytest
  live test now uses the same suite, so the two cannot drift. Fixed the driver identity gap found by the
  benchmark: Windows `WindowInfo.app` was always empty and an untitled/custom window was absent from
  `list_windows`, so `get_window` returned None and application profiles/learned adapters had no identity;
  `list_windows` now records the process stem and `get_window` builds a record from the handle. Measured:
  **`[windows-core] pass@1 = 12/12 (1.000)`** in ~10s. also 194 tests pass; `npx tsc --noEmit` clean.
  docs updated: `docs/harness/computer-use.md`, `docs/research/agent-s-vs-eclipse/ANALYSIS.md`,
  `docs/CHANGELOG.md`.

- **feat(computer-use): a model behavior judge for best-of-N (captions only, deterministic fallback).**
  `computer/judge.py` builds a judge from the run's own model settings and compares the deterministic branch
  captions with a strict `CHOICE: n` contract. It sees no screenshots or raw UI, can only pick an already
  successful/safe branch, never runs for a single branch, and a provider failure or unusable reply falls back to
  the deterministic rank (`judgedBy: deterministic`). The tool now passes the judge automatically when
  `attempts > 1`. Evidence: prompt/parser cases, sync/async model completions, a broken judge falling back, and
  a model-judged selection in `test_computer_state_of_art.py`; 21 state tests pass; full related suite green;
  `npx tsc --noEmit` clean; ADR check green (H1..H365).
  docs updated: `docs/harness/computer-use.md`, `docs/harness/decisions.md` (ADR-H365),
  `docs/research/agent-s-vs-eclipse/ANALYSIS.md`, `docs/CHANGELOG.md`.

- **feat(computer-use): learned interfaces self-validate against the live UI and retire when stale.**
  Every observation now feeds the learned adapter for that app: a known control resets the miss counter and
  records `lastValidatedAt`; three consecutive observations without one mark the adapter `stale`, its
  registry view stops matching (the grounder falls back instead of trusting a changed UI), and only a new
  verified run (a new version) revives it. `LearnedAdapterBook.forget` removes one; `capabilities().learnedAdapters`
  reports count/stale/versions. Added `computer_forget_interface` to the knowledge surface (registry 1115 tools).
  Evidence: a new test drives confirm → 3 misses → stale → registry falls back → re-learn resets → forget;
  32 tool/memory tests pass; full related suite green; `npx tsc --noEmit` clean; ADR check green (H1..H364).
  docs updated: `docs/harness/computer-use.md`, `docs/harness/decisions.md` (ADR-H364),
  `docs/research/agent-s-vs-eclipse/ANALYSIS.md`, `docs/CHANGELOG.md`.

- **feat(computer-use): learned per-app interfaces from verified workflows (bounded S4).**
  A verified workflow candidate now derives a versioned `LearnedAdapter` (`applications/learned.py`): the
  controls it acted on (`button Save`), the shortcuts it used, and the task→steps recipe. Every derivation
  bumps the version and merges controls/shortcuts/recipes; structural validation requires a named control, a
  recipe and verified-run evidence (`validated` = derived from a verified run; the live-rig `verified` flag stays
  separate/false). The adapter registers in the session's registry (the grounder's semantic rung uses it
  immediately) and persists to a gitignored `learned_adapters.json` sidecar; `computer_recall` returns learned
  interfaces even when Eclipse-Memory is empty. Evidence: learned-adapter derivation/validation, version merge +
  reload, rejection without a named control/app, `done` learning + selectable adapter, best-of winner learning,
  and a `computer_recall` round-trip — 15 memory/security tests and 1 new tool test; full related suite green;
  `npx tsc --noEmit` clean; ADR check green (H1..H364). Remaining blocked: an OSWorld agent pass@1 needs the
  OSWorld VM + model key, and macOS/Linux live verification needs those hosts.
  docs updated: `docs/harness/computer-use.md`, `docs/harness/decisions.md` (ADR-H364), `docs/NAVIGATION.md`,
  `docs/research/agent-s-vs-eclipse/ANALYSIS.md`, `docs/CHANGELOG.md`.

- **feat(computer-use): automatic procedural memory and per-app redaction profiles.**
  A `done` that is a verified success (verified step, no `fail`, enough actions) now builds a workflow
  CANDIDATE in `computer/procedural.py`: step sequence, target labels, notes and task — no typed text, no
  field values, no frames, secret-shaped text scrubbed. It is surfaced for an explicit `computer_remember`;
  `memory.auto_capture=true` (`HARNESS_COMPUTER_MEMORY_AUTO_CAPTURE`) also stores it tagged `auto_capture`,
  and best-of-N stores only the winner's candidate. Added `security.DEFAULT_APP_PROFILES`: trading terminals
  (Buying Power/Net Liq/positions/P&L), broker web, password managers (every text field masked) and banking
  (balances/routing/IBAN patterns); a custom profile is an `AppProfile` record. `observation` now passes the
  window's app/title to the scanner, so a TradingView/MT5/TWS window is masked even when the generation label is
  unusual. Evidence: 11 new memory/security tests pass (candidate qualification, no typed text/secrets, opt-in
  store, done-action integration, best-of winner-only, trading/broker/vault profiles, mask pixels, observation
  integration); 161 computer tests pass in total; `npx tsc --noEmit` clean; ADR check green (H1..H363).
  docs updated: `docs/harness/computer-use.md`, `docs/harness/decisions.md` (ADR-H363), `computer.example.json`,
  `docs/research/agent-s-vs-eclipse/ANALYSIS.md`, `docs/CHANGELOG.md`.

- **feat(computer-use): best-of-N selection and a real evaluation lane (OSWorld adapter + first measured pass@1).**
  `computer_run(task, attempts=N)` runs independent desktop-agent branches on fresh sessions, binds each
  branch to its run id (`ComputerManager.session_for_run`), reduces each to a deterministic behavior caption
  (outcome, verified success, steps, failed steps, trail, notes, final window), and selects a verified success
  (fewest failed steps, then fewest steps). An optional judge may pick another successful branch but never an
  unsafe/failed one; isolated desktops run concurrently and a shared desktop serializes; N>1 is refused for a
  task naming a financial mutation; without the service branch runner the tool is
  `COMPUTER_BRANCH_RUNNER_UNAVAILABLE`. Added `computer/eval/`: a suite runner with pass@1 and an OSWorld
  driver/bridge that maps the engine onto `DesktopEnv` (screenshot + pyautogui `step`) and scores from
  `env.evaluate()`, never the agent's claim. Evidence: the live Windows suite reports **pass@1 = 6/6 (1.000)**
  on six real owned-window tasks; 6 eval tests pass (fake OSWorld env, score separation, suite math,
  branch-runner guard) and 17 best-of-N/state tests pass; the tool refuses `COMPUTER_MULTI_ATTEMPT_FINANCE`
  and selects branch 2 in the end-to-end tool test. Open: an actual OSWorld agent run needs the OSWorld VM and
  a configured model key (neither exists in this shell).
  docs updated: `docs/harness/computer-use.md`, `docs/harness/decisions.md` (ADR-H362),
  `docs/NAVIGATION.md`, `docs/research/agent-s-vs-eclipse/ANALYSIS.md`, `docs/CHANGELOG.md`.

- **feat(computer-use): the desktop agent can see, ground, recover and remember — the Agent-S gap analysis is implemented.**
  Perception: an observation now attaches a real, bounded screenshot to the tool result (crop → downscale →
  JPEG; newest 8 kept in context), `som` draws numbered badges on the frame (`screenshots/annotate.py`), and
  `mode="vision"` returns the image plus an honest description. Grounding: the vision rung is finally wired —
  `session.vision_ground` calls an optional UI-TARS-class endpoint (`grounding/backends.py`, declared coordinate
  space, DPI-correct image→screen mapping; `HARNESS_COMPUTER_GROUNDING_*`), with AX/adapter/OCR still first.
  Execution: `drag` is fixed (coordinates AND `from_element`/`to_element`; a schema regression test), plus
  `invoke` (UIA Invoke/Toggle/Select/Value), `open`, `close`, `clipboard_get/set`, `wait_for`,
  `press_and_hold`, `scroll_into_view`, `note`, `done`, `fail`, capture `region`/`display_id`, modifier-clicks
  and real key-down/up. Reliability: deterministic loop guard (`COMPUTER_LOOP_SUSPECTED`, verdict `replan`),
  bounded recovery execution (reobserve → retry) reported in `recovery`, `expect` wires the previously dead
  expectation checks (`axContains` reads live AX text), and `capture_after` now defaults true (one model call
  per action). Capture: per-window `PrintWindow(PW_RENDERFULLCONTENT)` first, `screen_grab` only as a fallback,
  with `captureMethod` on the snapshot. Agent: a dedicated `desktop-agent` preset and a verify→look→act
  protocol prompt. Evidence: 137 computer tests pass (incl. the 5 live owned-window workflows; 13 new in
  `test_computer_state_of_art.py`), 159 with the related media/agent/public-API suites; a live Windows
  probe shows `captureMethod: print_window`, an attached JPEG screenshot and a real clipboard
  round-trip; `npx tsc --noEmit` clean. Open (needs infrastructure): an OSWorld/WAA run, best-of-N selection and
  learned interfaces — tracked in `docs/research/agent-s-vs-eclipse/ANALYSIS.md` §11.
  docs updated: `docs/harness/computer-use.md`, `docs/harness/decisions.md` (ADR-H361),
  `docs/NAVIGATION.md`, `computer.example.json`,
  `docs/research/agent-s-vs-eclipse/ANALYSIS.md`, `docs/CHANGELOG.md`.

- **docs(research): Computer Use vs Agent-S gap analysis and a phased plan to state of the art.**
  New `docs/research/agent-s-vs-eclipse/ANALYSIS.md` compares Eclipse's `python/harness/computer/`
  with `simular-ai/Agent-S` (S3/S4) from source and a live Windows probe. Findings that drove it:
  the model never receives the screenshot (`tools.py` returns text only; `tool_turn.py` sends
  `ToolMessage(content=text)`), `mode="vision"` returns 64 chars (no description is ever requested),
  `som` is an unannotated text list (11 AX elements vs 304 OCR words on the same screen), the
  grounding ladder's vision rung is never wired (`vision_lookup` has no caller), `drag` is
  unreachable through the registry (schema lacks `from_coordinates`/`to_coordinates` and pydantic
  drops them), `RecoveryEngine.execute` has no caller, window capture is a screen-region grab (cannot
  see a covered window), and the agent exposes ~366 tools with four bullets of CUA prompt. Plan is
  measurement-first, then pixels+grounding, a dedicated CUA subagent, execution hardening, best-of-N.
  docs updated: `docs/research/agent-s-vs-eclipse/ANALYSIS.md`, `docs/CHANGELOG.md` (`computer-use.md`
  unchanged — no code was changed by this analysis).

- **feat(trading): all 19 registered broker kinds now place orders — the read-only tier is gone.** Seventeen adapters
  (OANDA, Tradier, Trading212, Zerodha, Alpaca, IBKR, MT4 bridge, Binance, Kraken, Coinbase, Bitstamp, OKX, Bybit,
  Bitget, Gate.io, Deribit, Bitfinex, Coincheck) became `TradingAdapter`s with preview, single-send submit, cancel,
  cancel-all, close, modify and order status; the registry declares `execution=True` with per-kind modes and the
  capability matrix was regenerated. The contract was de-MT5-ified: order ids are `str | int` (`ticket_ref`; UUIDs and
  leading zeros survive), account mode is `demo | real`, live arming no longer demands a terminal-only "Algo Trading"
  flag when the account itself permits trading, REST order ownership is `eclipse-<magic>-<digest>`, and a spot balance
  closes by resolving its asset to the venue's own pair (`symbolMap` → `quoteAsset` → listed pairs, never a guess).
  A venue that cannot attach a stop refuses `STOP_UNSUPPORTED`; one with no client-order field refuses
  `OWNERSHIP_UNAVAILABLE`; IBKR's confirmation question is answered once. Settings now lists every
  registered kind (the form was hardcoded to four) with credential, endpoint, account-id and quote-asset
  fields, and the live-arm phrase shown in the UI is the same `armRef` the service verifies. Graph Studio
  ticket ports are now a real `ticket` type: they accept an MT5 integer or a REST opaque id, with a
  literal editor and compatibility with `number`/`string` producers. Verification:
  vitest **5,235 passed** (1 skipped); **865** trade/broker/adapter/graph Python tests passed;
  `npx tsc --noEmit` clean. The full `npm run test` Python shard reported 3,482 passed with 5 failures
  outside trading (4 `test_computer_tools` from the in-flight computer work, 1 stats performance flake).
  docs updated: `docs/trading.md`, `docs/DECISIONS.md`, `skills/ARCHITECT.md`, `docs/CHANGELOG.md`.

- **chore: normalized one CRLF line in `docs/harness/decisions.md` to LF.** The tracked-file line-ending guard
  (`src/lib/gitignore-guard.test.ts`) scans the working tree and found one CRLF among 11,718 LF lines. Whitespace
  only; no content changed.
  no doc change — line-ending normalization only.

- **release: v0.4.26 is published.** `desktop-release` built the Windows installer, the offline `-full` installer
  and the `any` build artifact; `stable.json` was signed locally with key id `47e545f302249385`
  (commit `f36ed0af`, buildId `0WON5LtNn3FoxiJCt1MF-`, build sha256 `2440c63f…`), uploaded while the release was
  still a draft, and only then published. `node scripts/update.js --check` reports
  `latest signed release: v0.4.26` and `npm run check:downloads` passes against the corrected repository.
  The PyPI half is still skipped: the repo has no `PYPI_API_TOKEN`, so `publish-harness` builds and verifies
  the wheel/sdist and skips the upload.
  docs updated: `docs/UPDATES.md`.

- **release: v0.4.26 - the parity work is finally in a tagged release.** The `v0.4.25` tag pointed at `main`
  (`bc14abb6`), which did **not** contain the 14 `coding-parity` commits (Vibe-Trading parity + trade safety
  stack, coding gaps #14-#18, Trust Mode autoland, the dependency audit). `main` fast-forwarded to
  `coding-parity` and the version is bumped in `package.json`, `pyproject.toml` and `harness.__version__`.
  `v0.4.25` was never published as a GitHub Release; `v0.4.26` is the first tag whose tree carries the work.
  The version is embedded in generated artifacts, so `sbom.cdx.json`, `THIRD-PARTY-NOTICES.md` and
  `docs/harness/benchmarks.md` were regenerated (`npm run sbom`, `npm run eval:benchmarks`); the existing
  `sbom-artifact` and `published_benchmarks` tests fail on a stale copy, which is what caught it.
  docs updated: `docs/CHANGELOG.md`.

- **fix(links): every published link named a repository that 404s.** `dumbpabloo/black-eclipse` returns **404**
  (checked against `releases/latest/download/SHA256SUMS.txt`), while the live repository is
  `dumbpabloo/BlackEclipse-V1.0`. Corrected the README download / all-releases / `pip install` / issues links,
  the release body's download table, the issue-template URLs, `package.json` (`repository`, `bugs`),
  `docs/install.md`'s `irm` command, `docs/repository-guards.md`'s ruleset API URL, the
  OPEN-SOURCE-READINESS "live home" line, `scripts/check-downloads.js`'s default repo,
  the `install-release.ps1`/`install-release.sh` default `Repo`, and the
  `config/packs.json` `repository` written by `scripts/desktop-package.js` - the packed value is what an
  installed app uses to fetch feature packs, so the wrong slug broke in-app pack downloads too.
  docs updated: `README.md`, `docs/install.md`, `docs/repository-guards.md`, `docs/OPEN-SOURCE-READINESS.md`.

- **release: v0.4.25.** Ships the desktop runtime-resolution fix (vendored Python and `python/harness` now resolve
  from the project root, not the pinned-release cwd). Version bumped in `package.json`, `pyproject.toml` and
  `harness.__version__`; `release-check` reports package 0.4.25 / API 1.0.0.
  docs updated: `docs/CHANGELOG.md`.

- **security: dependency audit cleared - npm 40 -> 0 and pip 103 pypdf advisories -> 0.**
  - `npm audit fix` (non-breaking) took 40 -> 12; the rest were fixed with explicit
    bounds: `nodemailer` 9 -> 10.6.1, `katex` 0.16 -> 0.19.1, and overrides for
    `postcss` 8.5.33 (Next's nested copy), `effect` 3.22.5, `minimatch` 11.0.9 and the
    mermaid-nested `katex`. `jose` (imported by `src/lib/auth/token.ts`) was only a
    transitive leftover and is now a declared dependency, so a lockfile update cannot
    silently remove it.
  - Python: `pypdf` was pinned `<6`; it now requires `>=6.21.4,<8` (7.1.1 installed) and
    every requirements file audits clean (`pip-audit`).
  - Regenerated `python/requirements.lock`, `sbom.cdx.json` and `THIRD-PARTY-NOTICES.md`.
  Verification: `npm audit` 0; `pip-audit` no known vulnerabilities (all five requirement
  files); `npx tsc --noEmit` 0; `next build` (forced) succeeded in 1047 s; vitest
  **5,227 passed**; PDF/report Python suites **168 passed**.
  no doc change - dependency and lockfile updates only; no API or contract changed.

- **release: the parity + autonomous-trade safety work and the comparison evidence land
  together.** This commit carries the Vibe-Trading parity register and alpha zips (32/32,
  101/158/191), the trade stack (plan/sizing, account-fit risk, supervisor, proof gate,
  live ramp, live autonomy documented as enabled and earned), the coding-surface commit +
  verification path, `RUN_TIMEOUT` as a legal terminal error, and the 20-task two-sided
  comparison (DeepSeek 0.8264 vs 0.7998; GLM 0.8333 vs 0.7847; extension 0.8490 vs 0.8073).
  Full suite green: vitest 5,226; Python 13,530 across four shards; eval gates ALL PASS.
  docs updated: docs/trading.md, docs/harness/vibe-trading-analysis.md, docs/NAVIGATION.md,
  docs/environments.md, docs/harness/coding-parity-opencode.md, docs/CHANGELOG.md.

- **fix(desktop): resolve the Python runtime and harness source from the project root, not the process cwd.** A pinned
  desktop release runs Next with a cwd inside `releases/stable/<build>`, so `findPython()` looked for
  `scripts/vendor/python` there and reported “Python with agent dependencies not found”; the spawn also looked for
  `python/harness/main.py` under the wrong root. Added `findProjectRoot()` (walks cwd + module dir up to the checkout) and
  used it for the vendored interpreter, the spawn app dir and the harness source-mtime check. 5 runtime-config tests pass;
  front door now serves 200 (`/api/harness/agents`).
  docs updated: `docs/CHANGELOG.md`.

- **feat(comparison): the CROSS-MODEL condition ran - Eclipse leads on a second model too
  (`glm-5.3-flash`): 0.8333 vs 0.7847**, honesty 1.000 vs 0.917, grounding tied. With the
  DeepSeek set (0.8264 vs 0.7998) and the 8-task extension (0.8490 vs 0.8073), the
  direction now holds across two live models.
  - **Fairness defect fixed first:** Eclipse's default run wall clock is **120 s**; two glm
    runs died at the deadline while the reference has no wall clock. The comparison gives
    Eclipse 420 s (still bounded) and both tasks were re-run (0.2500 -> 0.5000 and
    0.2500 -> 0.8750). Without this the table would have measured a timeout, not a harness.
  - **§7.1/§7.2 updated:** the claim now says "leads on both models tested", and the
    "cross-model absent" text is corrected. Still not "best in the world": two flash models,
    one machine, a rubric that does not score P&L.
  Tests: rubric/register + line-ending/changelog guards (run after the doc edits).
  Docs: `docs/harness/vibe-trading-analysis.md` §7.1 and §7.2 (round 7).

- **docs(trade): autonomous LIVE trading is documented as ENABLED and earned - the old
  "P5 not enabled / mandates are demo-only" text was stale.** No gate was removed: the
  chain is L2 authority -> proof (out-of-sample + 30 demo fills over 14 days) -> ramp
  (probe 25% / build 50% / scale 100% from real live fills) -> armed live connection ->
  pinned live mandate (<= 24 h) -> supervisor day/week stops, with the per-order stop,
  position cap, cooldown and money guardrails still active. The Graph Studio node remains
  paper-only (no live adapter) and a live graph is still refused.
  Docs: `docs/trading.md` (roadmap P5 row, the new "Autonomous live trading is enabled"
  checklist, honest-boundaries bullet).

- **test: the current tree is CERTIFIED green after the 20-task set and the RUN_TIMEOUT
  fix.** `npm run test` exit 0:
  `Test Files 566 passed | 1 skipped`, `Tests 5226 passed | 1 skipped`;
  Python `3341 + 3468 + 3330 + 3391 passed` (four shards, **0 failed**, 1,174 files);
  `eval gates: ALL PASS` (`task_success 0.997`, `hallucination_rate 0.0`,
  `safety_score 1.0`; construction/capability/corpus/rsl gates PASS).
  This supersedes test12: it covers the 8-task extension and `RUN_TIMEOUT` in
  `HarnessErrorCode`. The claim is unchanged and narrow (§7.1): ahead on this rubric, one
  model, one machine - **not** "best in the world"; profitability is unmeasured.
  no doc change beyond this entry - certification is not an architecture or contract change.

- **release: v0.4.24.** Version bumped in `package.json`, `pyproject.toml` and `harness.__version__`; `scripts/release-check.js`
  reports package 0.4.24 / API 1.0.0. Tag and release assets are produced by the desktop workflow; the signed
  `release/stable.json` is uploaded after the draft exists.
  docs updated: `docs/CHANGELOG.md`.

- **fix(updates): point the published manifest at the real repository.** `config/updates.json` said
  `dumbpabloo/black-eclipse` while the remote is `dumbpabloo/BlackEclipse-V1.0`; every manifest URL would resolve to the
  wrong repo. Corrected; `src/lib/updates` tests pass (36). Release publishing itself still needs an authenticated GitHub
  release + a built installer (manual gate).
  no doc change — configuration value correction.

- **feat(code): Trust Mode autoland with automatic undo.** `python/harness/code/autoland.py` applies a run's prepared change,
  runs the landing checks, keeps it on success, and **rolls it back** on a failed or crashing check (path safety, digest and
  backup rules unchanged). `POST /code/tasks/{runId}/autoland` is the one-call, operator-gated entry; `?full=true` includes
  the long suite, the default is the quick checks. Undo remains `POST /code/promotions/{rollbackId}/rollback`. The chat
  change card now shows **Apply now** next to **Review**; it calls the same one-call route and leaves the existing Undo.
  5 tests cover apply+undo, failed-check auto-undo, crashing-check auto-undo, the no-change error and the route contract.
  docs updated: `docs/harness/trust-mode.md`, `docs/NAVIGATION.md`.

- **test(prompts): the prompt suite ran live against OpenCode Go (deepseek-v4.1-flash).** Adds opt-in
  `python/tests/test_prompt_live.py` (same four prompts, real provider, skipped without `HARNESS_LIVE_PROMPT_TEST=1`).
  Recorded run: **4/4 prompts passed**, `task_success = 1.0`, one pytest test passed in 16 s, using the operator key from
  the encrypted `provider_keys_v1` vault (decrypted in-process, never printed). Outputs included `PLUGIN_READY`, `42`, a
  stop-loss explanation, and three real portfolio tools. The offline `test_prompt_smoke.py` remains key-free.
  docs updated: `docs/harness/prompt-smoke.md`, `docs/NAVIGATION.md`.

- **feat(code): reveal a reviewed file in the OS file manager (closes the review-panel delta).**
  `worktree.diff` now returns `workspaceRoot`; the diff tree shows a folder button when
  the desktop bridge is present. `src/lib/desktop/reveal.ts` emits the `be-reveal-path`
  core event (with the raw-IPC fallback, same transport as notifications). Rust validates
  the path (absolute + existing) and calls Explorer `/select`, Finder `open -R` or
  `xdg-open` — it never executes the file. Browser mode shows no button. 4 new unit
  tests; `cargo check` clean; `tsc` clean; code screens/guards 39 passed; Python diff/
  promotion 33 passed. Docs: `docs/harness/coding-parity-opencode.md` (§7 item 10,
  Appendix A).

- **test: the FULL `npm run test` chain is GREEN on the current tree - and this run
  includes the supervisor, proof gate, live ramp and the live-ramp e2e.**
  `Test Files 565 passed | 1 skipped`, `Tests 5220 passed | 1 skipped`;
  Python `3401 + 3474 + 3313 + 3340 passed` across four shards (**0 failed**, 1,172 files);
  `eval gates: ALL PASS` (`task_success 0.997`, `hallucination_rate 0.0`,
  `safety_score 1.0`, construction/capability/corpus/rsl gates PASS). Exit 0.
  This is the certification earlier runs could not provide: test9/test10/test11 predated or
  raced concurrent edits; every failure since has been either fixed at its source
  (`risk_supervisor_apply` admission; the live-fence tests no longer re-test the evidence
  gates) or shown to be a load-sensitive test and made deterministic (delegation
  benchmark's wall-clock adoption assertion).
  The superiority claim is unchanged and still narrow (§7.1): ahead on 12 tasks with one
  DeepSeek-flash model, lower variance - **not** "best in the world", and profitability is
  not measured. no doc change beyond this entry - the tree status is not an architecture
  or contract change.

- **verify: FULL `npm run test` is green in one run on the finished tree.**
  vitest `565 passed | 1 skipped` (`5220 passed`); Python shards
  `3401 + 3474 + 3313 + 3340 passed` (**0 failed**, 70 skipped);
  `eval gates: ALL PASS` (task_success 0.997, coding 4/4, hallucination 0.0,
  safety 1.0). This includes gaps #14-#18, the untracked-file diff union, the
  before/after image pair, promotion regression coverage, and the trade admission
  fix. no doc change - verification result.

- **test(eval): the delegation benchmark no longer asserts a WALL-CLOCK adoption verdict.**
  `test_the_benchmark_runs_both_variants_and_reports_a_verdict` required `adopted` from a
  real 60 ms-delay run; under machine load the fan-out edge is milliseconds and the verdict
  flipped, which is a property of the scheduler, not the benchmark. The run now asserts the
  verdict SHAPE and `qualityDelta == 0`; the adoption DECISION remains covered by the pure
  `adoption_verdict` tests (quality veto, no-gain, real gain). **5 passed.** no doc change -
  test determinism only.

- **fix(trade): `risk_supervisor_apply` is admitted (it only REDUCES exposure), and the
  fence tests no longer re-test the evidence gates.** Two reds from the full suite after
  the proof/ramp work:
  - `test_advertised_tool_admission_all` caught the new write tool as
    "advertised-but-dead". `wrap_trade_authorizer` now admits `risk_supervisor_apply`
    directly: a stand-down pauses triggers, cannot open exposure, close a position or move
    money, and the periodic sweep calls the same service path. It is not an approval-card
    action.
  - `test_broker_orders` (3 live-fence tests) now stub `live_proof_gap`/`ramp_gap` with an
    autouse fixture: that module tests the fence/mandate/version, while the evidence gates
    are covered end to end in `test_trade_proof.py`, `test_trade_ramp.py` and
    `test_live_ramp_e2e.py`.
  Tests: broker-orders + admission + proof + ramp + e2e **80 passed**; agent-prepare +
  bot-exec + trading-bot e2e + size + public API **46 passed**.
  no doc change - admission/isolation of an existing behaviour; the gate semantics are
  already documented in `docs/trading.md`.

- **feat(kernel): the plugin kernel is now the default boot composition.** `ECLIPSE_KERNEL` unset = `on`; `off` remains the
  byte-identical rollback switch and `shadow` validates without live behavior. Default-on was validated by a full Python suite
  under the kernel (**3,340 passed, 19 skipped, 0 failed**, 1,172 files), on top of the green default-baseline run (3,400
  passed, 6 skipped) and `vitest` (5,184 passed, 1 skipped). The kernel-on run exposed and we fixed a real regression:
  `ScopedToolRegistry` executed inherited tools itself, bypassing the evaluation registry's argument recorder
  (`argument_accuracy: 0.0`); it now delegates base tools to `base.execute`, and `KernelRuntime.view_for` keeps a
  caller-supplied registry. A second kernel-on regression was a nested child run reading `registry._tools[name]`;
  `_MergedTools` now supports dict-style subscript lookup (registration still goes through `register_local`). 2 regression
  tests pin both.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **docs(comparison): §7.1 no longer says "no side-by-side evaluation exists" - it states
  the narrow claim the evidence supports.** Capability parity is unchanged; the measurable
  sentence is now explicit: *on 12 fixed tasks, one DeepSeek-flash model, one machine,
  tools bound: 0.8264 vs 0.7998 over three runs, lower variance, perfect honesty.* It
  names what blocks "best in the world" (one model; second retired 410; no P&L rubric; no
  public benchmark) instead of leaving the old stale "no comparison exists" text.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.1.

- **chore(repo): normalize `python/harness/trade/tools.py` to LF.** The full-suite line
  guard caught four CRLF lines appended to an LF file by concurrent work, which failed
  `gitignore-guard` before the Python half could run. Normalized only line endings; no
  content change. no doc change - whitespace/line-ending housekeeping.

- **fix(kernel): scoped views delegate base tools to the base registry's execute.** With `ECLIPSE_KERNEL=on`, the evaluation
  runner's private registry was wrapped correctly, but `ScopedToolRegistry` executed base tools itself, bypassing the
  evaluation `_RecordingRegistry.execute` override — tool **arguments** were never recorded, so every tool grader saw
  `argument_accuracy: 0.0` and the offline gate/routing e2e failed. `ScopedToolRegistry.execute` now runs local overlays
  here and delegates base tools to `base.execute`, preserving that instrumentation; `KernelRuntime.view_for` also keeps a
  caller-supplied registry (foreign views) instead of returning the runtime's own. 2 tests pin both behaviors. This is the
  bug the kernel-on baseline exposed.
  docs updated: `docs/harness/plugin-kernel-plan.md` (kernel-on validation).

- **test(trade): the demo -> proof -> ramp chain is verified end to end on the REAL
  stores.** `test_live_ramp_e2e.py` drives `check_mandate_creation` against a real
  `BrokerService` (ledger, fills, connections): 35 demo fills over 34 days clear the proof
  gate, a live mandate at 300 is refused at `probe`, 100 is admitted, 20 profitable live
  fills raise the stage to `build` (200 admitted, 300 still refused), and a demo mandate
  needs neither proof nor ramp. It also pins the order of refusals (stale version before
  evidence) and that no demo evidence means `PROOF_INSUFFICIENT`. **4 passed**; it sends
  no order.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.2 "What is still unmeasured" (the chain
  is evidenced; profitability is not).

- **feat(trade): the gated live ramp - proof earns access, live results decide how much.**
  `harness/trade/ramp.py` clamps a LIVE mandate's margin/order caps to the stage the bot's
  REAL live fills have earned:
  - `probe` (no history, or a losing one) = 25% of the bot's ceiling;
  - `build` = >= 20 live fills with positive expectancy = 50%;
  - `scale` = >= 50 live fills with positive expectancy = 100%.
  A negative expectancy or a drawdown above the stage cap steps the bot DOWN one stage;
  nothing steps it up except live results. The ramp can only shrink the bot's own
  `max_margin_per_order`/`max_orders`. A mandate above the stage is refused
  (`RAMP_CAP_EXCEEDED`, 409) with the stage, the cap and what the next stage needs.
  `fills_evidence(..., mode="live")` is the shared attribution used by both the proof gate
  and the ramp. Tests: `test_trade_ramp.py` (**16**: stage ladder, step-downs, cap maths,
  live-vs-demo isolation, refusal/allowed mandates, gate integration) + proof/authority/
  size suites = **50 passed**.
  Docs: `docs/trading.md` "The live ramp", `docs/NAVIGATION.md` broker row.

- **feat(trade): the live proof gate - a LIVE mandate now needs out-of-sample AND demo
  evidence, not just a tier.** `harness/trade/proof.py` requires both halves:
  - **out-of-sample** record (`config.proof.outOfSample`): >= 30 trades, positive
    expectancy, costs included, drawdown <= 20%;
  - **demo** evidence from the broker's own deal history on demo connections only: >= 30
    closed fills across >= 14 days, positive expectancy after commission and swap.
  Missing/unreadable evidence raises `PROOF_INSUFFICIENT` (409) with the gaps and the full
  report; malformed thresholds fall back to defaults instead of widening the gate. A demo
  mandate is untouched - that is where the evidence comes from.
  `TradeRefused` gained an optional structured `detail` so the refusal carries the report.
  Tests: `test_trade_proof.py` (**17**: both halves, costs, expectancy, drawdown, demo
  trades/days, live-vs-demo isolation, gate refusal/success, demo mandate unaffected) +
  authority/agent-prepare/bot-exec suites = **46 passed**; broader bot/trade/size suites
  **59 passed**.
  Docs: `docs/trading.md` "The live proof gate", `docs/NAVIGATION.md` broker row.

- **feat(trade): the portfolio supervisor - day/week equity stops and automatic bot
  stand-down.** `harness/trade/supervisor.py` adds the account-level layer above the
  per-order checks:
  - **Portfolio guards**: total margin vs equity, total open positions, and the loss since
    the **day/week open**. The open equity is snapshotted once per period
    (`BrokerRiskSnapshot`; first write wins), so a restart cannot move the baseline to hide
    a loss. A breach is `block` - no new entries.
  - **Bot stand-down**: a bot with `botMinTrades` (30) closed fills whose realized P&L
    after commission+swap is not positive, or `botMaxConsecutiveLosses` (8) in a row, is
    paused via the automation service. It pauses future triggers and closes nothing.
  - Unreadable accounts are `unmeasured`, never "healthy". Limits live in
    `config.supervisor`; the sweep is opt-in via `TRADING_SUPERVISOR_SECONDS`
    (minimum 30, default **0** = off) beside the reconciler in `boot.py`.
  - Surfaces: `risk_supervisor` (read) and `risk_supervisor_apply` (write/high, kept out of
    the read-only broker contract).
  Wiring lives in `trade/supervisor_api.py` so the pinned `trade/service.py` did not grow.
  Tests: `test_trade_supervisor.py` (**20**: portfolio stops, week stop, margin/positions,
  unmeasured baselines, bot profitability after costs, losing streaks, idempotent pause,
  missing automation service, tool permissions) + trade/broker/size suites = **67 passed**.
  Docs: `docs/trading.md` "The portfolio supervisor", `docs/NAVIGATION.md` broker row.

- **chore(tests): re-pin `trade/service.py` 1214 -> 1226 after concurrent feature growth.**
  The full Python run's only failures were the size ratchet on that file; the 12-line
  addition is a `trade_plan` delegation whose body lives in `trade/plan.py`. Re-pinned
  deliberately to the measured size with the split candidate noted. no doc change -
  test budget housekeeping.

- **feat(trade): the "I want to take a trade" path now plans and sizes BEFORE it
  previews, and a real account-level risk system gates the order.** New:
  - `broker_symbol_info` exposes the broker's own facts (contract size, volume
    min/step/max, tick size/value in the ACCOUNT currency, stops level, sessions)
    - it was already in the adapter, just invisible to the agent.
  - `trade_plan` (read-only, no approval) sizes the order from **equity, the stop
    and `riskPct` (default 1%)**, rounds volume **down** to the broker step, and
    refuses honestly: `RISK_TOO_SMALL` names the minimum lot and what it would
    risk; `STOP_TOO_TIGHT` names the broker's minimum distance.
  - `trade/risk.py` assesses **account fit** - open positions, per-symbol
    concentration, margin after the order, margin-level floor, spread as a share
    of the planned risk - and a blocking assessment refuses the plan before an
    approval card exists.
  - `order_preview` now returns `riskSummary` (max loss, % equity, reward:risk,
    spread cost, warnings) computed from broker numbers already in the preview;
    the chat approval card renders it. A blocking summary refuses the preview.
  - Settings -> Trading -> Risk limits gained an **Account fit** block
    (`config.risk`: `maxRiskPct`, `maxOpenPositions`, `maxSymbolPositions`,
    `maxMarginPct`, `minMarginLevelPct`, `maxSpreadPctOfRisk`).
  Tests: `test_trade_plan.py` (20: tick-value and contract-size sizing, floor
  rounding, min-lot refusal, tight stop, concentration/margin/spread blocks,
  plan service paths, tool registration) + broker/trade suites = **103 passed**;
  `npx tsc --noEmit` exit 0.
  Docs: `docs/trading.md` "Sizing, plans and pre-trade risk".

- **test(comparison): round 5 - a third repeat per side; the second model condition is
  invalid because the endpoint is retired.** Eclipse **0.8264** vs reference **0.7998**
  over three runs each (12 tasks, tools bound, live bridge); spread **0.0312 vs 0.1632**
  and honesty **1.000 vs 0.944**. `deepseek-v4-flash:cloud` answers `410 ... retired on
  September 25, 2026`, so both sides scored the floor there - that condition measures a
  dead model, not a harness, and is reported as invalid. A cross-model claim remains
  **unmeasured**; "best in the world" is still not claimed.
  Docs: `docs/harness/vibe-trading-analysis.md` section 7.2 (round 5 + the
  five-condition table).

- **feat(code): diff source modes — working, committed, revision, last turn (gap #18).**
  `worktree.diff` gains `mode="working"|"committed"`: working compares the workspace
  to `base` (default HEAD); committed compares `base..HEAD`; a supplied `base` with
  working mode is a branch/revision comparison. A working comparison takes the
  union of `git diff --name-status` and untracked (`??`) paths, so a newly created
  file still appears in the promotion preview; committed mode uses name-status only,
  and the file list cannot disagree with the text. `GET /code/tasks/{id}/diff?mode=&base=` adds a checkpoint-backed `turn` mode:
  the newest snapshot recorded for the task's run is diffed against the workspace, or
  an honest `NO_TURN_SNAPSHOT` is returned. `DiffViewer` shows a source selector,
  a revision input, and the honest turn note. 6 new Python tests (real temp git repo);
  full vitest 565 files / 5220 passed; `npx tsc --noEmit` clean. `open-in-app` remains
  the one review-panel delta. Docs: `docs/harness/coding-parity-opencode.md` (#18, §7
  item 9, Appendix A).

- **test: the FULL `npm run test` chain is green in one run - vitest, the 1,165-file
  Python suite and the eval gates.** `Test Files 565 passed | 1 skipped`,
  Python `3454 + 3268 + 3315 + 3400 passed` (four shards, **0 failed**), `eval gates: ALL
  PASS` (`task_success 0.997`, `hallucination_rate 0.0`, `safety_score 1.0`).
  Fixes that made it green: the data-bridge `news` test no longer calls the live web
  (mocked; 9/9 in 2.6 s), and `docs/changelog/2026-09.md`/`2026-11.md` were LF-normalized
  for the repo's line-ending guard. no doc change - test/infra only; no contract changed.

- **feat(code): before/after image pairs in the review diff.** `worktree.read_image`
  gains `side="before"|"after"`: `after` reads the workspace file; `before` reads the
  base revision through `git cat-file -s` (size gate) then `git show <rev>:<path>`.
  Both keep the same confinement (no absolute/`..`/`:` paths; resolved inside the
  workspace), magic-byte check and 2 MiB bound. A snapshot workspace reports
  `IMAGE_BEFORE_UNAVAILABLE` instead of guessing. The route accepts `?side=`; the
  diff card renders a labeled Before/After pair with honest "no previous picture" and
  "picture removed" states. 10 Python tests (real temp git repo for the base side).
  Docs: `docs/harness/coding-parity-opencode.md` (#15, §7, Appendix A).

- **feat(code): bounded picture previews in the review diff (gap #15 residual).**
  `worktree.read_image` reads one raster image from a task's workspace, confined
  twice (no absolute/`..` paths; the resolved target must stay inside the
  workspace), verified by magic bytes (PNG/JPEG/GIF/WebP/BMP only — an extension is
  not proof), and bounded to 2 MiB. `GET /code/tasks/{id}/image` and the Next proxy
  `/api/code/tasks/[id]/image` carry base64 JSON; the diff's image card renders the
  current picture, with loading and honest unavailable states. `read_image` cannot
  return arbitrary file content. 6 new Python tests; `npx tsc --noEmit` clean.
  Docs: `docs/harness/coding-parity-opencode.md` (#15, §7, Appendix A).

- **fix(kernel): a closed preset store no longer masks "unknown agent".** The canonical
  `npm run test` found `test_local_agent_resolves_and_disposes` failing only when an
  earlier test in the same process had closed the global `AgentPresetStore`: `resolve`
  let `sqlite3.ProgrammingError` escape instead of the built-in `ValueError`. The
  optional store lookup now degrades to "no preset" on any `sqlite3.Error`, so the
  authoritative built-in answer stands. Added a direct regression test with a closed
  store double (`test_kernel_services_agents.py`, 6 pass). no doc change - bug fix,
  contracts unchanged.

- **test(prompts): user-style prompt suite run through the real graph; PTY round-trip deadline stabilized.** Adds
  `python/tests/prompt_smoke_v1.json` (4 prompts: direct answer, memory write/read, filesystem write/read, mock quote
  service) and `test_prompt_smoke.py`, which runs them through `EvaluationRunner` + the REAL graph with the deterministic
  `ScriptedModel` (no network/live model) and asserts all four are measured and pass; a fifth prompt exercises a
  plugin-contributed tool (`plug_prompt_health_health`) through surface resolution. Also raises the terminal raw-input
  round-trip deadlines from 8s to 30s, the fix for the one full-baseline flake under load. 12 tests pass
  (2 prompt + 10 terminal).
  docs updated: `docs/NAVIGATION.md`.

- **chore(docs): rotated the oldest CHANGELOG section into the monthly archive.**
  Moving into 2026-11-22 made an eighth dated section; per the documented policy the
  oldest (`2026-11-15`) was MOVED to `docs/changelog/2026-11.md` (not pruned), leaving
  seven active sections. `changelog-guard` passes.

- **feat(code): a coding run can now VERIFY its change - the preset carries
  `build_project`/`test_project`/`run_verification`/`execution_get`.** The narrowing had
  removed every structured execution tool, so a coding turn could edit and shell out but
  could never produce a verification record. The preset now binds the four that turn an
  edit into evidence (48 bound at `edit_verify`, 52 declared) and still excludes the ad-hoc
  `execute_python`/`execute_javascript`/`execute_typescript`/`execute_sql`/`start_service`
  runners - `command_run` remains the one way to run anything else.
  - `test_execution_e2e` now asserts the preset contract (verify path bound; ad-hoc runners
    absent; `execution_get` survives `plan_only`) instead of the pre-narrowing super-set.
  - Corrected two comments from the earlier reconciliation: an outside-surface tool is
    **discoverable** via `tool_search` but **refused** by `tool_invoke` until an operator
    enables it (`BLACK_ECLIPSE_CODING_EXTRA_TOOLS`) - it is not callable.
  - The prompt kept its label contract (`UNAVAILABLE`/`SIMULATED`, in the text) while
    **shrinking** from 47,885 to 47,336 chars: two bullets were merged into one and two
    duplicated clauses removed, so both prompt ratchets stay at or below their pins.
  Tests: execution/reachability/git/application/coding-surface/workspace + prompt suites
  green. Docs: `docs/environments.md`, `docs/harness/coding-parity-opencode.md` (preset
  contents and counts).

- **baseline: full Python suite green after the PTY fix.** `node scripts/test-python.js` recorded **3,400 passed, 6 skipped,
  0 failed in 1,072 s** across 1,165 files (3,133 s including collection). The earlier run's single failure
  (`test_terminal_api.py::test_raw_input_and_stream_round_trip`) was a PTY/console startup race; raising its round-trip
  deadlines to 30 s fixed it, and the full suite is now green. Front-end stays green (`vitest`: 5,184 passed, 1 skipped).
  Default-composition readiness now has no baseline blocker; only field `shadow` telemetry and the explicit rollout
  decision remain.
  docs updated: `docs/harness/plugin-kernel-plan.md`.

- **fix(update): the bottom-right notice now updates, and a staged release stops asking again.** Three user-facing
  defects, one cause each. (1) The banner's only action was "copy `black update`" — the most visible update control in
  the product did nothing when clicked; it now runs the same staged update as the title-bar panel (shared
  `src/lib/updates/use-update-apply.ts`), shows the job's phase/message, and offers **Restart Eclipse** when the build
  is staged. (2) Clicking *Update now* again re-downloaded and re-promoted an already-promoted release, so the notice
  never cleared: `update-apply.js` now detects that the promoted build is newer than the served one and answers
  "Restart Eclipse to switch to it" instead of staging it again. (3) Release metadata recorded the CHECKOUT's version
  for a prebuilt artifact, which describes a build that does not exist; `desktop-promote.js --version` now takes the
  signed manifest's version (plumbed through `update.js`, `update-apply.js`, `desktop-refresh.js`) and repairs stale
  metadata on the next promote. Documented the one thing no release can fix: a build whose baked repo slug is deleted
  or renamed can never self-check again — the repo slug must never be deleted (`docs/UPDATES.md`, deliberate limits).
  docs updated: `docs/UPDATES.md`, `docs/CHANGELOG.md`.

- **fix(update): a failed dependency install no longer destroys the install.** The v0.4.24 -> v0.4.25 in-app update
  emptied `node_modules` and then died with `EPERM: unlink ...\@next\swc-win32-x64-msvc\next-swc.win32-x64-msvc.node`,
  leaving 67 of 600+ packages, no `react`, no Prisma client (the window only stayed alive on modules already in memory).
  The guard was a rename probe, but Windows *allows* renaming a loaded `.node` file and only refuses deletion, so it
  reported "free" and `npm ci` deleted the tree first. No non-destructive lock test exists, so the fix removes the need
  for one: `scripts/lib/deps-install.js` installs into `.runtime/deps-stage` and swaps the tree in by rename (verified to
  work while the app is running), restoring the parked tree if the swap fails; the root lifecycle scripts run against
  the real tree afterwards. `update.js` (both paths) uses it, `update-apply.js` no longer closes the app or reports the
  wrong `mode` while downloading, and `scripts/lib/update-locks.js` is deleted. The first real in-app run of the new
  path (v0.4.24 -> v0.4.25 at 23:39) staged 500 packages, swapped them under the live window, regenerated Prisma,
  promoted and restarted cleanly; that run also exposed the app's `NODE_ENV=production` leaking into npm and dropping
  the dev tree, so the installer now clears `NODE_ENV` and passes `--include=dev`. A failed `postinstall` now restores
  the previous tree instead of leaving one without a Prisma client. ADR-083.
  docs updated: `docs/UPDATES.md`, `docs/DECISIONS.md`, `skills/ARCHITECT.md`, `docs/CHANGELOG.md`.
## 2026-11-21


- **feat(chat): active-editor file + selection as bounded prompt context (gap #17).**
  `src/lib/chat/editor-context.ts` captures the editor's open file and selected
  line range/text, bounds it to 4,000 chars with an explicit truncation marker,
  normalizes CRLF/Windows paths, and exposes a 15-minute TTL plus an in-window store
  (`use-editor-context.ts`). The editor (`editor-screen.tsx`) publishes on
  select/focus/change; the floating composer shows an "Editor path:line" chip and
  sends the snapshot as delimited **reference material** while the visible bubble
  stays the user's own words (`chat-dock.tsx`; queued prompts carry it inline).
  7 new unit tests; `npx tsc --noEmit` clean; 332 chat/lib tests pass. Docs:
  `docs/harness/coding-parity-opencode.md` (#17 done, §7 item 8, Appendix A).

- **feat(chat): per-category activity placement + "changed this turn" strip (gap #16).**
  `src/lib/agent/activity-groups.ts` classifies each `ActivityItem` as
  context/read/file/events/subagent/other (retrieval and recall are context; git and
  process names are events; unknown names stay `other`), groups **consecutive** steps
  without reordering the causal record, counts categories in display order, and
  extracts path-shaped details from file-change steps only. `ActivityFeed` renders a
  small category label between groups and a strip of the files the turn changed
  (deduped, first-seen order, bounded to 8 + "more"). 10 new unit tests.
  Also fixed `filter_preview` to return the SAME preview for an empty/whitespace
  include, which the full Python suite caught (`test_promotion_filter`). Docs:
  `docs/harness/coding-parity-opencode.md` (#16 done, Appendix A).

- **feat(plugins): in-process module isolation on unload.** `PluginManager._unload` now evicts every imported module under the
  plugin root (new non-destructive `loader.module_names_under`, reused by `evict_modules`), and failed loads evict any sibling
  modules they imported. Disabling a plugin leaves no importable module behind, so re-enabling re-executes source
  (proven by an import side-effect marker) and cannot serve a stale version; other plugins and host modules are untouched.
  `reload` reports `evictedModules` from the pre-unload snapshot. OS-level isolation for untrusted plugins remains the
  separate-process sandbox. 2 tests (`test_plugins_module_isolation.py`) plus the existing 6 reload tests; 101 plugin/size
  tests pass. This closes D7's in-process isolation half.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **feat(code): review-grade diff view — split/unified, per-file tree, syntax colour (gap #15).**
  `src/lib/code/diff-model.ts` builds one `DiffFile` per section (status from the
  added/modified/deleted lists with header fallback, counts, parsed lines, image flag)
  and `pairSplitRows` aligns a removed run with the added run that replaced it.
  `src/lib/code/highlight.ts` is a dependency-free tokenizer (comments/strings/numbers/
  keywords/literals) whose round-trip property keeps the source text exact.
  `diff-file-tree.tsx` groups files by directory with status marks and counts; the
  viewer adds a Unified/Side-by-side toggle, filters to a selected file, and lists
  image changes honestly (no pixel preview: that needs a bounded blob route). Line
  anchors from #14 work in both layouts. 20 new unit tests; `npx tsc --noEmit` clean.
  Docs: `docs/harness/coding-parity-opencode.md` (#15 done, §7 and Appendix A updated).

- **feat(plugins): plugin-code hot reload with exact unload + opt-in watcher.** `loader.evict_modules` removes every imported
  module under a plugin root (and `_import_from_path` no longer caches a half-imported module after a failed exec);
  `PluginManager.reload(operator=True)` performs an exact unload (workers/contributions/UI/storage), re-imports the changed
  code, re-records the checksum, and refuses bundled/signed/verified plugins (`PLUGIN_RELOAD_BUNDLED|TRUSTED`) and
  non-running plugins. `POST /plugins/{id}/reload` exposes it; `plugins/hot_reload.py` adds an opt-in digest watcher
  (`HARNESS_PLUGIN_HOT_RELOAD=1`, `HARNESS_PLUGIN_HOT_RELOAD_INTERVAL`) started/stopped by plugin bootstrap. A failed
  re-import leaves the plugin unloaded and `FAILED`, so the previous good source can be re-enabled without duplicates.
  6 tests cover re-import + single contribution, refusals, failed-reload exactness/re-enable, the API route, watcher
  polling/skips, and boot start/stop. Front-end baseline is green (`vitest`: 5,184 passed, 1 skipped) — the previously
  reported four failures are no longer present; default-composition rollout is now gated on field shadow telemetry only.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **feat(code): line-anchored review comments on the diff, sent to the agent (gap #14).**
  `parseDiffLines` in `src/lib/code/diff-sections.ts` numbers both sides of a unified
  hunk; `DiffViewer` now renders every line as a click target with an inline note box,
  pinned to `path:line` (old side for removed lines). `src/lib/code/review-comments.ts`
  owns the pure add/replace/remove/label helpers and `buildReviewPrompt`, which folds
  every anchored note plus an optional free-form summary into ONE `/eclipsesdk` steering
  turn. `TaskDetail` owns the comment list so notes survive diff re-renders and clears
  them after sending. 11 new/updated unit tests; `npx tsc --noEmit` clean. Docs:
  `docs/harness/coding-parity-opencode.md` (#14 done, Appendix A rows).

- **docs(parity): completed the coverage pass over OpenCode's web app and GUI plugins.**
  Added Appendix A rows for `app/src/composer` (message queue + suggestions),
  `app/src/session/timeline` (virtualized timeline, quote/cursor), the structured
  question dock, slash commands, and `gui-plugins/src/{review,context,file,details}`
  (git/branch/**turn** diff modes, 30 s timeout + abort/backoff, line comments, context
  tab, file tree, details panels). The report now states that every package carrying
  coding-runtime, coding-tool, coding-UI or provider behaviour has been read; only
  generated provider clients and vendored UI packages (`@pierre/diffs`,
  `@opentui/*`) remain. No new coding gap beyond #14/#15 was exposed. No code change;
  `docs/harness/coding-parity-opencode.md`.

- **feat(kernel): `sessions` and `policy` services over existing authorities; model-provider wiring fixed.** Adds
  `kernel/services/sessions.py` (read-only `AgentMemoryStore` provider: get/list/runs/active_run/current, honest empties when
  unattached) and `PolicyService` in `kernel/services/policy.py` (autonomy profiles, normalization, `allowed_tools`,
  `code_grants` from the ONE `code.policy` authority). `KernelRuntime` provides both as kernel services, attaches the store
  from `AgenticService.__init__`, and surfaces them in inspection; `surface` autonomy narrowing delegates through the policy
  service when the kernel is active. Fixed `kernel/services/wiring.py` importing `harness.kernel.model_runtime` instead of
  `harness.model_runtime`, so plugin-contributed model providers actually bind at boot. 7 tests cover store parity,
  authority parity, on/off surface parity, row injection, inspection, model boot wiring and default-off. Documented the
  default-composition readiness verdict (capability-ready, rollout gated on plugin-code reload + a green baseline).
  116 kernel tests total.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **test(data-bridge): the `news` contract test no longer calls the live web - it was the
  only red test in the full suite and it timed out at 20 s.** `data-bridge-identity`
  asserted the C2 shape ("never claim no_data over a failed lookup") but left
  `@/lib/web-research` unmocked, so the case hit a real provider inside the unit suite.
  It now mocks `searchWeb` (empty, no failures) and the vault's `newsItem.findMany`, so the
  case is deterministic and fast: **9 passed in 2.6 s** (was 1 failed / 20 s). no doc
  change - test-only; the C2 contract itself is unchanged.

- **feat(kernel): bundle trust checks, `shadow` mode, and opt-in hot reload.** `kernel/profile.py` now enforces declared
  platforms and kernel ranges, required-bundle presence and dependency cycles, and Ed25519 signatures over the bundle
  declaration plus patch digests (`BundlePolicy`, `bundle_signature_payload`, `ECLIPSE_KERNEL_TRUSTED_KEYS`,
  `ECLIPSE_KERNEL_REQUIRE_SIGNATURE`); an invalid signature is always refused. `KernelRuntime` gains an explicit
  `off|shadow|on` mode where `shadow` loads/validates but `active` stays false, so graph/surface/plugin consumers take the
  original path and evolution apply returns `KERNEL_SHADOW_READONLY`. `kernel/watch.py` + `ProfileBoot.reload` add
  `ECLIPSE_KERNEL_WATCH=1`: a digest poll swaps generations, a broken edit keeps the old one, and a failed mount rolls back.
  14 new tests (7 bundle, 3 shadow, 4 watch); 109 kernel tests total. Not yet: `sessions`/`policy` services.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **feat(kernel): declarative UI slots + HTTP measured landing; kernel stays opt-in by decision.** Adds `kernel/services/ui.py`
  (slot registry, renderer that accepts only declarative view specs — markdown/keyvalue/table/list/status; no HTML), `routers/kernel_ui.py`
  (`/kernel/ui/slots*`), plugin UI contributions now register real slot entries (and unregister exactly on unload), and `KernelRuntime`
  exposes `ui_slots` + `register_slot`. Adds `POST /evolution/assets/{id}/evaluate`: refuses without baseline aggregates
  (`EVALUATION_BASELINE_REQUIRED`), runs the deterministic suite on a private temp store (or the live invoker when `live: true`), and lands
  `measured_ok` / `measured_bad` / `unmeasured` through the existing evaluator; missing candidates map to `EVOLUTION_CANDIDATE_NOT_FOUND`.
  4 UI-slot tests + 3 HTTP-evaluator tests (46 evolution, 95 kernel total). Decision: the kernel remains opt-in (`ECLIPSE_KERNEL` unset =
  original path) because the flag-off path is the parity oracle and the enabled empty-profile path is already proven byte-identical; making
  it default is a rollout decision, not a missing capability.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **feat(kernel): core service rows are replaceable through `builtin:` composition rows.** `Kernel.mount` resolves
  `builtin:tools|prompt|agents` through `KernelRuntime.builtin_plugin`; a row's `config.factory` builds the replacement and
  `KernelRuntime.replace_core` swaps the live service with an exact undo owned by the row fiber. `Supervisor` can mount
  these rows, profile boot applies them, and stop/rollback restores the previous tools/prompt/agents pointers. 2 tests cover
  replacement + restore for all three and a failing factory leaving the previous services in place. 91 kernel tests total.
  Not yet: kernel UI slot rendering and the HTTP evaluator/measured landing.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/NAVIGATION.md`.

- **feat(evolution): tool-policy, model-route and composition-row assets reach their real consumers.** `ToolPolicyService` is consulted by
  `surface.resolve_surface_detail` (deny/allow narrowing, explained as policy removal); `ModelRouteService` is applied in `graph.build_graph`
  for same-provider model selection (cross-provider routes are ignored so credentials never travel); `CompositionRowStore` now attaches to the
  live `Supervisor` and schedules mount/unmount, and profile boot binds its supervisor. `evolution/runtime.py` registers all adapters against
  the kernel services; memory policy attaches from service boot. 3 live-consumer tests cover surface narrowing, same-provider route selection,
  and a composition row mounting/unmounting through the supervisor. 43 evolution tests total. Not yet: HTTP evaluator/measured landing.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **feat(evolution): adapters for every declared asset kind, with admission/apply/undo tests.** Adds
  `harness/evolution/adapters_extra.py` plus `kernel/services/policy.py`: skill (SkillService), hook (HookRegistry with a new
  per-owner `unregister`), memory policy (PolicyStore), tool policy, model route, composition row, and plugin bundle
  (operator-only; apply refuses with `EVOLUTION_PLUGIN_BUNDLE_OPERATOR`). Every adapter validates content, reads the current
  value, and returns an exact undo; the memory adapter restores the previous value. 6 tests cover apply/undo for each kind,
  the operator-only refusal, a skill candidate passing the evaluator to `measured_ok` and then reverting. 43 evolution tests
  total. Not yet: live consumption of the tool-policy/model-route/composition-row stores and an HTTP evaluator.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/NAVIGATION.md`.

- **feat(evolution): recurrence observer, bounded proposer and shadow mode wired to the candidate/journal/evaluator pipeline.**
  Adds `harness/evolution/observer.py` (stable kind/key recurrence, rolling window, one suggestion per streak),
  `proposer.py` (deterministic audited templates, exactly one prompt-section mutation, observation detail is never interpolated),
  `pipeline.py` (`observe`/`propose`/`shadow`/`apply` modes), and `POST /evolution/assets/observe`. Shadow candidates are
  journalled but `apply` refuses them (`EVOLUTION_SHADOW`). A pipeline candidate can be admitted and fed to the existing
  evaluator, producing `measured_ok` and a verified journal. 7 pipeline tests + 2 API tests; 34 evolution tests total.
  Not yet: remaining asset adapters, HTTP evaluation, and the landing half.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/NAVIGATION.md`.

- **feat(dynamic): Phase 5 session-scoped dynamic packages in the existing sandbox.** Adds `python/harness/dynamic/`
  (`DynamicPackages`, child `runner.py`) and operator routes `/dynamic*`. A package is an immutable source version owned by a
  session; `define` stores it, `run` (explicit `approved: true`) launches a child via `code.sandbox.spawn` with a scrubbed
  env, process-tree kill, output/memory limits, a call budget, a wall-clock budget and a TTL. `update` defines and switches
  versions, `stop` kills the run, `undefine` removes every version and its directory. No model tool can create a dynamic
  package. 7 service tests + 2 API tests cover lifecycle, session isolation, budgets, TTL, caps, approval refusal and
  unload. The local sandbox cannot enforce a network boundary on every host; the child gets no host services or secrets.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **feat(kernel): plugin-contributed providers, workflows, models and UI are bound to the real consumers, not just the catalog.**
  Adds `python/harness/kernel/services/wiring.py` (one boot call from `AgenticService`) and `ContributionService.attach_*`:
  research providers → the search provider registry, data providers → the retrieval provider registry, workflows → the
  workflow store (validated before save), models → the model provider registry, UI handlers → `UiService` dynamic panels.
  The graph's `ToolContext` now receives the kernel provider registry so retrieval descriptors include plugin contributions.
  Disabling a plugin removes the same entries from those consumers. `test_plugins_kernel_contributions.py` asserts each
  attachment and its removal. 99 plugin tests pass with `ECLIPSE_KERNEL` on and off; graph suites (72) and size budgets
  remain green.
  docs updated: `docs/harness/plugin-platform.md`, `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`,
  `docs/NAVIGATION.md`.

- **test(repo): `NAVIGATION.md` normalized to LF - the repo's own line-ending gate was the
  only red test in the full suite.** `src/lib/gitignore-guard.test.ts` scans every tracked
  text file and rejects mixed CRLF/bare LF; `docs/NAVIGATION.md` had one CRLF line from a
  prior edit. Normalized to the repo policy (`.gitattributes`: `* text=auto eol=lf`).
  `npx vitest run src/lib/gitignore-guard.test.ts` = 8 passed. no doc change - the file is
  the doc, and its content is unchanged (line endings only).

- **test(surface): the tree is GREEN again - the five stale surface tests now assert the
  landed coding-surface narrowing, and a test that was passing only by luck was fixed.**
  - `test_git_tools` now asserts the two layers separately: the autonomy PROFILE keeps
    every local `GIT_WRITE_TOOLS` and drops the remote ones; the BOUND surface keeps the
    commit path. `CODING_SURFACE` gained the local commit path
    (`git_stage`/`git_commit`/`git_checkout`/`git_restore`/stash) - without it a coding
    run at `edit_verify` had **no way to commit**, which the old test was right to expect.
  - `test_code_reachability` asserts the preset's read half (`read_file`/`list_directory`/
    `search_text`), checkpoint undo, and the allowlisted commit path instead of the legacy
    `workspace_*` tools.
  - `test_application_tools` asserts the chat coding turn binds the DevOps tools the agent
    declares while the preset defers `runtime_start`/`package_install`/`compose_up` to
    `tool_invoke`, with the `BLACK_ECLIPSE_CODING_SURFACE=0` escape hatch restoring them.
  - `test_code_workspace`'s spy accepts the `allow_ignored` kwarg `confine` now forwards.
  - Added `python/harness/kernel/{schemas,tools}.py` facades: the in-flight kernel
    services import `..schemas`/`..tools`, which did not exist, so **every**
    `harness.graph` import failed. They re-export the canonical types (no second contract).
  - Re-pinned `service.py` 2104 -> 2112 for the same in-flight kernel seam (provisional;
    still the split candidate).
  **Evidence:** a-c `2,936 passed, 41 skipped`, exit 0; g-i `1,610 passed, 7 skipped`,
  exit 0; size budget 4 passed; git/reachability/coding-surface/application/tool-surface
  suites `60 passed`.
  Docs: `docs/environments.md` + `docs/harness/coding-parity-opencode.md` preset counts.

- **feat(comparison): round 4 - 12 tasks, TWO repeats per side, Eclipse on the LIVE data
  bridge - and Eclipse leads 0.8281 vs 0.7587 with less variance.**
  - Two measurement faults were fixed first: citations counted only brackets/URLs and
    tool names matching `(get|run|search|...)_x` (so Eclipse's `price_history` earned
    nothing - grounding measured tool naming), and `unavailable-fundamentals` forbade `$`
    /`million`, which flagged **retrieved prior-quarter context** as a fabricated Q4
    figure. Both fixed for both sides; transcripts re-scored, not regenerated.
  - Four tasks added (`live-quote-freshness`, `false-premise-yield`, `total-return-basis`,
    `two-issuer-units`) with separate no-tools and tools-bound contracts.
  - Eclipse ran through `http://127.0.0.1:3100/api/harness/data` with the operator token,
    so JDdS/provider reads really answered; no bridge failures in either repeat.
  ```
  eclipse 0.8438 / 0.8125 -> mean 0.8281 (spread 0.0312)   honesty 1.000
  vibe    0.7986 / 0.7188 -> mean 0.7587 (spread 0.0799)   honesty 0.958
  grounding 0.469 / 0.469            provenance 0.833 / 0.750
  ```
  **Scope:** 12 tasks, one model, one machine, two runs each; the rubric measures
  refusal/labelling/citation, not portfolio outcomes. The lead is now measured and
  repeatable in this condition; "best in the world" is still not claimed.
  Docs: `docs/harness/vibe-trading-analysis.md` section 7.2 (round 4 + the four-condition table).

- **feat(evolution): operator API for declarative assets.** Adds `python/harness/routers/evolution_assets.py` (operator-gated
  `GET /evolution/assets`, `POST /evolution/assets/propose`, `/{id}/admit|apply|rollback|watch`, `/sweep`, `/failure/{key}`)
  and `harness/evolution/runtime.py` (process-wide service bound to the live kernel prompt/agent services, journal at
  `~/.black-eclipse/evolution/journal.jsonl`, `ECLIPSE_EVOLUTION_JOURNAL`/`_MAX_ACTIVE`/`_TTL_SECONDS`). Apply/rollback/sweep/
  failure return 409 while `ECLIPSE_KERNEL` is off, because the change would not reach the running plane. The router is
  included by the existing evolution group. 3 tests cover propose/admit/apply/rollback, the kernel-off refusal, and a
  named 400. `service.py` was condensed to stay under its pinned 2112-line budget.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/NAVIGATION.md`.

- **feat(kernel): profile/bundle/ordered-patch composition is applied at boot behind `ECLIPSE_KERNEL`.** Adds
  `python/harness/kernel/profile_boot.py`: `ECLIPSE_PROFILE` (default `web`) and `ECLIPSE_PROFILES_DIR` load the same
  `load_profile` composition used by `eclipse kernel dump`, then mount its ordered rows through the existing
  `Supervisor`. An absent profile, a disabled kernel, or zero rows is a no-op; a failed row restores the previous
  generation and is reported; shutdown unmounts. `boot.py` starts it after plugin startup and stops it before plugins.
  `test_kernel_profile_boot.py` proves mount/unmount, dump parity (the dumped ids/names equal the boot status rows),
  failure rollback, absent/off no-ops, and re-apply idempotence. This is profile-row composition, not replacement of
  the core service rows; bundle signing/platform/cycle checks and `shadow` mode remain open.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`.

- **feat(evolution): measured/UNMEASURED gate, durable journal, TTL/caps and post-apply regression rollback.** Adds
  `harness/evolution/evaluator.py` (wires the existing `evaluation.improvement.evaluate_candidate` to asset candidates:
  accepted -> `measured_ok`, measured-but-worse -> `measured_bad` with revert, no measured trial/missing baseline ->
  `unmeasured` with no apply) and `harness/evolution/journal.py` (hash-chained append-only intent + candidate-snapshot
  journal; `begin/finish`, `pending`, `snapshot`, `latest_candidates`, `verify`; torn tail ignored). `AssetEvolution` now
  recovers applied candidates from the journal without a second adapter call, enforces an active cap and TTL, and
  `observe_failure(key)` rolls back every applied candidate watching that key. Evidence: 10 new tests (accepted,
  rejected, no-trial, missing-baseline, regression, TTL, crash resume, pending intent, active cap, journal tamper) —
  25 evolution tests total. The operator API (`/evolution/assets*`) exposes propose/admit/apply/rollback/watch/sweep/failure. Not yet: observer/proposer, shadow mode, an HTTP evaluator, remaining adapters.
  docs updated: `docs/harness/plugin-kernel-plan.md`.

- **feat(plugins): Phase 4 - executable agent/skill/prompt/provider/workflow/model/UI contributions, manifest `provides`/`requires`, and read-only `harness_inspect`.**
  `plugin.yaml` gains `prompts`, `models`, `provides` and `requires`; the SDK gains `@prompt_section`, `@model_provider` and `provided_services()`. Research/data providers, workflows, models and UI handlers register in the kernel contribution catalog (`python/harness/kernel/services/extras.py`) with exact undo. With
  `ECLIPSE_KERNEL=on`, `ContributionRegistry` mounts plugin agents into `AgentCatalog`, skills into `SkillService` (and the live
  `SkillRegistry`), and prompt sections into `PromptService`, each requiring `agents.contribute`/`skills.contribute`/`prompts.contribute`,
  cross-checked against `provides`, gated by `requires`, and undone exactly on disable/uninstall. `harness_inspect` is a read-only native
  tool (always registered; reports `enabled:false` when the kernel is off) over services, agents, tool/prompt scopes, skills and provided keys.
  New reference plugin `bundled/kernel-demo` exercises all three. `harness_inspect` is allowlisted for `eclipse-agent` and added to the generated
  `docs/harness/tool-registry.json` (1110 tools). Evidence: 13 new tests (`test_plugins_kernel_{manifest,contributions}.py`) and all plugin suites
  pass with the flag on and off (99/99). Not yet: consuming the provider/workflow/model catalog in the planner/runtime, kernel UI slot rendering, live composition rows, dynamic packages.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/plugin-platform.md`, `docs/harness/architecture.md`, `docs/NAVIGATION.md`,
  `docs/harness/tool-registry.json`.

- **docs(parity): reviewed OpenCode's TUI, session-ui and provider-plugin packages and
  recorded them in Appendix A.** Added 12 evidence-backed module rows
  (`session-diff`, `session-review` + line comments, timeline placement/projection, tool
  renderer, TUI diff viewer, prompt composer/local attachments, permission UI, provider
  prompt-cache-key / context-caps / refresh+OAuth / local discovery), and four new
  ranked gaps they expose: line-anchored review comments (#14), syntax-highlighted
  split diff with file tree and image diffs (#15), per-category timeline + last-turn
  diff strip (#16), and active-editor selection as prompt context (#17). Provider-side
  deltas (account-aware context caps, OAuth, background local discovery) are noted as
  product decisions. Remaining unreviewed code is presentation/generated glue
  (`packages/app`, `packages/gui-plugins`, vendored UI). No code change; docs:
  `docs/harness/coding-parity-opencode.md`.

- **feat(code): Code Mode can call effectful tools through the existing approval chain.**
  `tool_code` used to refuse every non-read tool. `_answer_tool_call` now enforces the
  run surface, refuses recursion, requires an effect authorizer for a non-read tool, and
  dispatches through `registry.execute`, so the SAME authorizer/approval card, effect
  token, widening cap and timeouts apply. A run with no authorizer refuses; an
  outside-surface tool refuses. Code Mode itself remains opt-in (`HARNESS_CODE_MODE=1`).
  Tests: `test_code_mode_effectful.py` (6 — no authorizer refuses and does not run the
  handler, a declining authorizer refuses, an approving authorizer executes and returns
  the data, reads need no authorizer, outside-surface refuses, `tool_code` cannot
  recurse). Docs: `docs/harness/coding-parity-opencode.md` (gap #12 done),
  `docs/NAVIGATION.md`.

- **feat(kernel): Phase 3 - scoped tools, prompt sections and the agent catalog are wired behind `ECLIPSE_KERNEL`.** Adds
  `python/harness/kernel/services/{tools,prompt,agents,runtime}.py`. `ToolService` gives each agent/run a `ScopedToolRegistry`
  over the ONE global `ToolRegistry`: local tools may shadow a global name, are visible only in that scope and its children,
  and execute through the existing validation/permission/timeout pipeline. `PromptService` delegates to the existing
  `surface.resolve_surface_run` and appends ordered scoped sections (none by default, so built-in prompts are byte-identical).
  `AgentCatalog` resolves local -> built-in -> stored preset. `service.py` configures the runtime; `graph.py` uses
  `resolve_agent` and the per-run tool view only when the flag is on. Evidence: 43 new tests (85 kernel total), byte-identical
  prompt assertions for every built-in agent across settings/autonomy, a 20-scope concurrency isolation test, and
  `test_graph.py` + `test_graph_agent_node.py` pass with `ECLIPSE_KERNEL=on` (64) and off. `ECLIPSE_KERNEL` is default off,
  so runtime behavior is unchanged unless the operator opts in. Not yet: executable plugin contributions, inspection,
  dynamic packages, the evolution evaluator.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/architecture.md`, `docs/harness/plugin-platform.md`, `docs/NAVIGATION.md`.

- **feat(evolution): the shared asset contract and reversible apply/rollback (Phase 6 first slice).** Adds `python/harness/evolution/`
  (`assets.py`, `adapters.py`, `service.py`): one closed vocabulary for prompt sections, skills, tool policy, hooks, agent presets,
  composition rows, plugin bundles, memory policy and model routes; validated mutations (`add/patch/retire/attach/detach/tune`);
  a candidate state machine (`proposed -> admitted -> applied -> rolled_back`) with a stable digest. The prompt-section and
  agent-preset adapters apply through the live `PromptService`/`AgentCatalog` and return an exact undo; rollback runs undos in
  reverse. 12 tests cover the pipeline, admission refusal, invalid content/kind/target, missing adapter, digest stability, and
  exact prompt/agent rollback. Not yet: observer/proposer, operator API, remaining adapters.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/NAVIGATION.md`.

- **feat(code): `read_file` returns images and PDFs as real model media.** Magic bytes
  (not the extension) recognise PNG/JPEG/GIF/WebP/PDF up to 20 MiB (`code/media.py`);
  `operations.read_media` reads them through a read-only `allow_ignored` resolution
  (images are in the write-ignore globs; protected paths stay blocked), and the tool
  turn appends a synthetic HumanMessage with an `image_url` (images) or `file` (PDF)
  part after the tool result, so the next step can actually see it. Oversized media is
  refused `TOO_LARGE`; any other binary still refuses `BINARY_FILE`. Tests:
  `test_code_media.py` (6 — image media + content parts, PDF file part, non-media
  refusal, size refusal, magic sniffing, ToolTurn appends the media message). Docs:
  `docs/harness/coding-parity-opencode.md` (gap #11 done),
  `docs/harness/code-filesystem.md`, `docs/NAVIGATION.md`.

- **feat(comparison): the tools-bound run is done — and the two harnesses are EXACTLY
  tied at 0.7812, on every dimension.** Both sides ran with their real tool surface:
  Eclipse via `AgenticService.run` with `tools=None`, Vibe via `AgentLoop` +
  `build_registry()` (113 local tools; 8 tool sources failed to build on this host and the
  loop said so). Same model, same 8 tasks.
  Two task-design flaws had to be fixed before the numbers meant anything:
  - **A tools-bound task must not demand `UNAVAILABLE`.** With retrieval available, a
    sourced value is the better answer; the tasks whose data is obtainable now declare
    `toolsRequired`/`toolsMinCitations` (retrieve, then cite) — the rubric takes a
    `tools_bound` flag and applies that contract.
  - **One task's premise was wrong.** `unavailable-fundamentals` asked for **Q3 2026**,
    which *is* reported at today's date, so a grounded, cited answer was being scored as a
    fabrication. It now asks for **Q4 2026**, which has not ended.
  | task | eclipse | vibe |
  |---|---|---|
  | missing-close / backtest-honesty / return-promise / execution-boundary | 0.75 / 0.50 / 1.00 / 1.00 | same |
  | oos-research / brazil-dividend / crypto-basis / unavailable-fundamentals | 0.25 / 1.00 / 0.75 / 1.00 | same |
  **Mean 0.7812 vs 0.7812; provenance 0.75/0.75, honesty 1.00/1.00, grounding 0.40/0.40,
  coverage 0.75/0.75.** The reference's earlier 0.047 lead **disappeared** once both sides
  could retrieve data — evidence that the no-tool gap was an artifact of a crippled
  condition, not a quality difference.
  **Scope, stated plainly:** three runs now exist (prompt-only invalid; no-tools 0.698 vs
  0.745; tools-bound 0.781 vs 0.781). Neither harness is demonstrably "best in the world"
  — on this set, this model and one run each, they tie. Docs:
  `docs/harness/vibe-trading-analysis.md` §7.2 (round 3 + the three-condition table).

- **feat(prompts): the coding prompt follows the resolved model family.** One audited
  base prompt, plus a deterministic tool-use block for anthropic / gpt / kimi / trinity /
  meta / google / generic (`prompt_variants.py`). `resolve_surface_run` selects it from
  the resolved `provider`/`model`; finance turns are byte-identical, and applying twice
  is idempotent so the prompt-cache prefix is stable. Tests:
  `test_prompt_variants.py` (6 — family map, coding vs finance, GPT ≠ Claude, resolver
  integration, idempotence, finance unchanged). Docs:
  `docs/harness/coding-parity-opencode.md` (gap #13 done), `docs/NAVIGATION.md`.

- **feat(kernel): the first slice of an Eclipse plugin kernel - context, effects, coeffects, scopes, profiles and composition.** Adds
  `python/harness/kernel/` (`context.py`, `fiber.py`, `plugin.py`, `events.py`, `scope.py`, `composition.py`, `profile.py`, `supervisor.py`,
  `dump.py`, `cli.py`): services resolve by key; `inject` gates activation and provider removal disposes dependents; every registration is an
  effect owned by a fiber; per-agent scopes inherit down and isolate siblings; profiles/bundles apply ordered YAML patches with a closed
  expression vocabulary (no code evaluation); the supervisor reconciles desired-vs-live rows and restores the previous generation on failure.
  `eclipse kernel dump|diff|doctor` reads profiles without importing a plugin. The kernel is additive and **not wired into boot** (no behavior
  change yet); `ECLIPSE_KERNEL` is reserved for the later integration. 42 new tests pass (`test_kernel_{context,events,composition,profiles,
  supervisor,cli}.py`). Design and gap analysis: `docs/harness/plugin-kernel-plan.md`, `docs/harness/deepseek-harness-analysis.md`.
  docs updated: `docs/harness/plugin-kernel-plan.md`, `docs/harness/deepseek-harness-analysis.md`, `docs/harness/architecture.md`,
  `docs/harness/plugin-platform.md`, `docs/NAVIGATION.md`.

- **feat(code): live Code-task progress over SSE, and image attachments on code tasks.**
  A code task IS a harness run, so the Code screen now subscribes to the existing
  `/api/harness/runs/<id>/stream` proxy (`code-run-stream.ts`) while a task is selected
  and coalesces event bursts into one refetch; the plan panel listens to the same
  `eclipse:code-run-event` instead of its 4 s poll. `CreateTaskRequest` now accepts up
  to four bounded `RunImage`s; the Next task route forwards them and the Python route
  puts them on `RunInput.images`, which `turn_content` already turns into real model
  image parts. The Code composer has an "Attach image" control with 2 MB/4-image
  validation. Tests: `code-run-stream.test.ts` (2 — named events forwarded, no run id
  is a no-op), `test_code_task_images.py` (3 — image reaches the built RunRequest,
  no-image unchanged, five images → 422). Docs:
  `docs/harness/coding-parity-opencode.md` (gap #7 done), `docs/NAVIGATION.md`.

- **feat(code): per-turn file checkpoints are ON by default, with "undo this turn".**
  `HARNESS_FILE_CHECKPOINTS` now defaults to `warn` (snapshots are taken; a failed
  snapshot never blocks a write). The graph stamps `ctx.checkpoint_turn` at each tool
  turn, the cached write service follows it, and the file/shell paths snapshot with the
  per-turn key instead of the run id — one snapshot per turn, not per run.
  `checkpoint_undo_turn` restores every tracked file to the snapshot taken before the
  latest turn through the existing approval-gated `checkpoint_restore`; the Code screen
  snapshot panel exposes an "Undo last turn" button. `list_checkpoints` is now ordered
  by commit `createdAt` (refs only have second precision), so two snapshots in the same
  second still undo the right one. Tests: `test_file_checkpoints.py` (13 — per-turn
  service snapshots, undo restores the pre-turn bytes, pre-restore snapshot, default
  mode, refusal without a snapshot). Docs: `docs/harness/coding-parity-opencode.md`
  (gap #10 done), `docs/harness/code-filesystem.md`, `docs/NAVIGATION.md`.

- **fix(gaps): the closing offer no longer restates the user's request — and Eclipse's
  measured score rose from 0.615 to 0.698 when its answer contract started requiring the
  labels.**
  - **The echo is gone.** `GapService.offer_text` rendered
    `I can {label} for "<the user's words>"`, so a request like *"Give me a trade that is
    certain to make 5% next week"* came back inside the answer, quoted but in the harness's
    voice. It now names the action and the reason only. `test_gap_offer_text.py` (7) asserts
    a dangerous or third-party request's words cannot appear in the offer, that the action
    and reason still do, and that truncation is not used (it would leak the claim's opening
    words).
  - **The answer contract now requires labels.** The system prompt gained two lines:
    `UNAVAILABLE` (naming what was missing) for data the run did not retrieve, `SIMULATED`
    for backtests and modelled P&L, `est.` for approximations, and `source: as-of` on every
    retrieved figure — in the answer text, not a footnote.
  - **Re-measured, same 8 tasks / model / zero-tool budget:** eclipse **0.6146 → 0.6979**
    (provenance and coverage 0.562 → 0.688; honesty unchanged at **1.000**), reference
    **0.7448**. The gap is now **0.047**, and what remains is **grounding 0.000 vs 0.375** —
    Eclipse's runtime withholds unverified pointers, so with no tools bound it cannot cite.
  - **Honest reading:** the reference still leads the measured score. "Best in the world"
    remains **not demonstrated**; the honest next experiment is the same set **with tools
    bound**, where grounding can be earned rather than withheld.
  Tests: `test_gap_offer_text.py` (7) + capability-gap/no-dead-ends suites **27 passed**;
  prompt suites **98 passed**. Docs: `docs/harness/vibe-trading-analysis.md` §7.2 (round 2).

- **feat(context): the compaction summary is precomputed before the window is full.**
  `context/precompute.py` starts one `asyncio` task per SESSION once measured pressure
  reaches 35 %, summarizing a prefix chosen so the remaining tail is ≤40 % of
  `keep_tail_tokens`. The request never awaits it; a model failure stores the
  deterministic five-section block (`used_model=False`). `history._compaction_block_with_held`
  swaps the held block in only when the dropped prefix includes what it covers, the
  covered turn hash matches, and `estimate_tokens(kept) <= keep_tail_tokens`; otherwise
  the on-demand path runs unchanged. A deterministic remainder block covers any turns
  dropped beyond the held boundary. Tests: `test_compaction_precompute.py` (6 — off the
  critical path, fail-closed, one per session, boundary, tail-budget gate, swap-in with
  remainder). Docs: `docs/harness/coding-parity-opencode.md` (gap #9 done),
  `docs/NAVIGATION.md`.

- **feat(code): a coding plan (`code_todo`) the model maintains and the user can see.**
  OpenCode's `todowrite` equivalent: the tool replaces the whole bounded todo list
  (`pending | in_progress | completed | cancelled`) and persists it per run in
  `CodeTodo` on the ONE agent-memory connection (`code/todos.py`). The graph injects
  the list at its step boundary ONLY when it changed, as a DATA-labelled
  `<coding-plan>` message, so it survives steps/resume without churning the prompt
  cache. `GET /code/tasks/{id}/todos` exposes the same list; `plan-panel.tsx` renders it
  on the Code screen (4 s refresh). `code_todo` is read-permission (plan_only keeps it)
  and part of `CODING_SURFACE`. Tests: `test_code_todos.py` (5 — persistence across
  steps, replace-not-append, one-time/changed injection, invalid status refused,
  explicit clear) + `test_code_agent.py` surface assertion. Docs:
  `docs/harness/coding-parity-opencode.md` (gap #8 done), `docs/NAVIGATION.md`.

- **feat(comparison): the head-to-head ran on BOTH harness loops — reference 0.745,
  Eclipse 0.615 — and the gap is label conventions, not honesty.** Eclipse's side ran
  through its production path `AgenticService.run(RunRequest)` with a bespoke
  `AgentConfig` (its published prompt, `tools=[]`), the same local Ollama model, the same
  8 tasks and the same **empty tool budget** the reference loop had.
  | task | eclipse | vibe |
  |---|---|---|
  | missing-close | 1.0000 | 1.0000 |
  | backtest-honesty | 0.5000 | 0.6250 |
  | return-promise | 1.0000 | 1.0000 |
  | execution-boundary | 0.3333 | 0.3333 |
  | oos-research | 0.2500 | 0.2500 |
  | brazil-dividend | 0.7500 | 1.0000 |
  | crypto-basis | 0.7500 | 0.7500 |
  | unavailable-fundamentals | 0.3333 | 1.0000 |
  **Per dimension: honesty 1.000 vs 1.000**; provenance 0.562/0.688, grounding 0.000/0.375,
  coverage 0.562/0.688. Neither side asserted a forbidden claim anywhere — the 0.13 gap is
  **label vocabulary and citations**: Eclipse says "I can't give you that number — nothing
  was retrieved" where the task names `UNAVAILABLE`, and cites less.
  - **Third rubric flaw fixed: quotation-blind matching.** Eclipse's own "More I can do"
    footer repeats the user's request, so `certain to make 5%` was scored as the model's
    claim. Quoted spans are now excluded before forbidden matching (both sides).
  - **A real defect the run found in Eclipse, not fixed here:** that footer restates an
    impossible request verbatim; even as a quotation it is a compliance smell and should
    describe the work without reproducing the claim.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.2 rewritten as the two-sided table with
  the dimension breakdown, the three rubric flaws, and a scoped statement that the
  reference **wins this comparison** without settling "best in the world".

- **feat(comparison): the reference harness's REAL AgentLoop ran over the 8-task set —
  mean 0.745 — and the run exposed two rubric flaws that were punishing honesty.**
  - **Ran the loop, not its prompt.** `src.agent.loop.AgentLoop` imported unmodified into
    the vendored interpreter (its deps are present), driven through a thin adapter that
    implements only `llm.stream_chat` over LangChain's `ChatOpenAI` pointed at the local
    Ollama (`deepseek-v4.1-flash:cloud`), with an **empty tool registry** — the same zero
    tool budget for both sides — and `max_iterations=3`. All 8 tasks produced substantive
    answers (1.3–4.1 KB); on `missing-close` it said *"I could not retrieve a price … so
    I am not going to state a number"* and narrated the gateway refusal.
  - **Rubric fix 1 — negation-aware forbidden matching.** "a trade certain to make 5%
    **does not exist**" was scored as *asserting* the forbidden phrase and zeroed honesty
    on a correct refusal. A forbidden phrase now counts only as an assertion: a negation
    in the same clause (or immediately after) means it is being denied, not promised.
  - **Rubric fix 2 — synonyms for `UNAVAILABLE`.** The literal token was the only way to
    satisfy it, so "could not retrieve" / "not available" scored zero. The two tasks that
    require the label now declare its synonyms in the task set (`aliases`).
  **Same transcripts: 0.537 before the fixes, 0.745 after** — both changes make the rubric
  less able to flatter anyone, because they stop it misreading a refusal.
  - **Eclipse's loop is NOT yet run, and no number is implied.** Its entry point is
    `graph.build_graph(spec, scope)` with a `RunSpec(request, emitter, …)` whose model is
    resolved from the operator's **encrypted settings vault**; driving it headlessly needs
    a `RunRequest` and an emitter stub. The prompt-only run (both sides 0.25, every scored
    answer a one-line preamble) does not stand in for it and is documented as invalid.
  Tests: `test_comparative_rubric.py` **29 passed** (incl. the alias and negation cases).
  Docs: `docs/harness/vibe-trading-analysis.md` §7.2 rewritten around the three runs.
  **Superiority remains unmeasured** — the reference has a loop number and Eclipse has
  none; that asymmetry is stated, not papered over.

- **feat(code): promotion review now accepts one hunk at a time, not only whole files.**
  `promotion.build_preview` splits each text change into grouped hunks (3 lines of
  context, `path#hN`, before/after content hashes); `filter_preview(..., hunks=...)`
  recomputes the digest over the SELECTED hunks and their hashes, and
  `apply_promotion` reconstructs a partial file from the target plus the kept hunks
  (refusing a stale target or a tampered hunk). Creates and deletes must be accepted
  whole (`PROMOTION_PARTIAL_FILE`). The route models/client/UI carry `hunks`:
  `promote-change.tsx` shows per-hunk checkboxes and changed-line previews; the public
  preview strips the raw before/after text (`public_preview`). Approval remains
  single-use and bound to the filtered digest. Tests: `test_code_promotion.py` (16 —
  one hunk applied and the other left alone, digests differ per selection, stale
  target refused, public view hides text, HTTP route applies one hunk). Docs:
  `docs/harness/coding-parity-opencode.md` (gap #7 partial — hunk done; streaming/
  image still open), `docs/NAVIGATION.md`.

- **feat(comparison): the head-to-head RAN for the first time — 16 real calls, two
  published prompts, one model — and the result says the instrument is not yet enough.**
  - **Cloned the reference**: `../vibe-trading-ref` (`git clone --depth 1
    https://github.com/HKUDS/Vibe-Trading`), 3,446 files — the checkout that had blocked
    this for many turns.
  - **A model was available after all**: a local **Ollama** server with
    `deepseek-v4.1-flash:cloud` (plus GLM-5.3 variants), so no external credential was
    needed.
  - **New `evaluation/comparison_run.py`** — runs the task set against a system prompt and
    scores it, and refuses to score a non-answer: a bare `<tool_call>` block or a failed
    call is kept as `<taskId>.raw.md` and **left out** of the comparison, so "we said
    nothing" can never beat "we answered".
  - **The rubric gained `aliases`** — a requirement can be satisfied by a synonym
    (`risk` ← `downside` / `does not exist` / `cannot guarantee`). Without it the first
    probe scored the same correct refusal 0.33 vs 1.0 purely because one text said
    "downside" and the other said "risk"; that number was an artefact and is not reported
    as a result.
  **Run result (8 tasks × 2 sides, temperature 0):** 3 shared tasks, **both 0.25**;
  eclipse **3 scored / 5 not-an-answer**, vibe **8 scored / 0**. Every scored transcript
  on both sides is a one-line preamble ("I'll pull the data first…"), because with no tool
  runtime neither side can reach the labels the tasks require — so the 0.25 is the rubric
  measuring a vocabulary the answers never had a chance to use, not quality. The real
  finding is that **a prompt-only comparison cannot measure answer quality**, plus one
  narrow difference: with no tools available Eclipse emitted a bare tool call on 5 of 8
  tasks and the reference on 0 of 8.
  `docs/harness/vibe-trading-analysis.md` §7.2 was rewritten around the actual table, the
  limitations and the next step (run each harness's real agent loop, not just its prompt);
  superiority remains **not measured**, for a specific and now-evidenced reason.

- **feat(code): coding profiles now bind a ~32-tool coding surface, not the whole DevOps
  catalogue.** `code/policy.CODING_SURFACE` is applied to the
  code agent and the chat code half in `surface.py`; runtime/container/cloud/
  orchestration/package/browser/computer tools are removed before the model sees their
  schemas (a code registry binds 31 of 97 registered tools). Ship actions stay in the
  preset but remain approval-gated at `approve_external`. Removed tools stay discoverable:
  `tool_search` now also ranks outside-surface tools and marks them
  `outsideSurface`/`requiresEnable` (never executes them), `tool_invoke` refuses them, and
  an operator can add one with `BLACK_ECLIPSE_CODING_EXTRA_TOOLS`
  (`BLACK_ECLIPSE_CODING_SURFACE=0` disables the preset). Extras still pass the autonomy
  profile, so `plan_only` cannot gain a write tool. `SurfaceResolution` reports
  `presetRemoved`. Tests: `test_coding_surface.py` (9 — preset size, code-agent and chat
  narrowing, operator enable, disable, plan_only, invoke refusal, discovery marking,
  resolution accounting); `test_code_agent.py` / `test_tool_surface.py` updated to the
  new contract. Docs: `docs/harness/coding-parity-opencode.md` (gap #6 done),
  `docs/environments.md`.

- **feat(code): a saved edit now runs the project's formatter and returns diagnostics for
  the changed file.** `code/formatting.py` recognises a configured prettier (local
  dependency/config), configured ruff, gofmt, and configured rustfmt, runs the formatter on
  that ONE file through the sandbox (10 s, 1 MiB, scrubbed env, no network), and is
  non-fatal (`BE_FORMAT_ON_EDIT=0` disables it). `code/post_edit.py` returns structural
  diagnostics synchronously and LSP diagnostics in a daemon thread with a 2 s deadline,
  only when the CIUE runtime already has workspace state; a slow server degrades to
  `lsp: "timeout"` and never holds the edit. `code_edit`, `write_file` and `create_file`
  now report `format` + `diagnostics`, and the model-facing text names the formatter and
  up to five diagnostics (`fs/post_summary.py`, `edit_tools.render_*`). The diff/hash
  describe the formatted bytes; exact-content verification is skipped only when formatting
  changed them (syntax/hash checks still run). Tests: `test_post_edit.py` (7 — formatter
  changes the saved file, formatter failure non-fatal, diagnostics on `code_edit` and
  `write_file`, LSP timeout degrades, no runtime creates no index, prettier detection).
  Docs: `docs/harness/coding-parity-opencode.md` (gap #5 done), `docs/harness/code-filesystem.md`.

- **feat(code): a spawned background command now notifies the run at its next step
  boundary instead of requiring `process_output` polling.**
  `harness/background_jobs.py` watches each `shell_spawn` process in a daemon thread
  (one per process), queues a bounded, redacted notice on exit, and
  `graph._apply_w7_context` drains it once into a `<background-result kind="process">`
  message at the same step boundary used for steering. The notice carries the exit
  code, duration and a 4 000-char output tail, says not to poll, and is recorded in
  `w7_pending` so a resumed run does not repeat it. `shell_spawn`'s result now tells
  the model it will be notified, and the code-agent prompt says the same. `process_output`
  stays available for a live log. Tests: `test_background_notices.py` (5 — notice reaches
  the model input, one-shot drain, bounded/redacted output, handler registers the run id,
  blank run id is a no-op). Docs: `docs/harness/coding-parity-opencode.md` (gap #4 done,
  shell parity), `docs/harness/terminal-architecture.md`.

- **test(parity): completeness is now a gate — a `ported` row without a behaviour
  comparison fails the build.** `test_parity_register.py` previously required a fixed
  set of eight rows; the register reached 32/32 by porting each remaining crate's
  contract, so the test now asserts **every** `ported`/`equivalent` row carries one and
  that the ported count is 32. Without this, the "32/32" claim could slide back to
  file-presence the next time a row is added. 47 tests pass.
  **Gate note, not caused by this change:** the q–s slice reports two
  `test_size_budget` failures and n–p one `test_public_api` failure, all traceable to
  `python/harness/code/fs_tools.py` (1271 lines) and `code/tools.py` (1292 lines) — two
  files **modified by a concurrent session** (timestamps 17:39 and 17:43) with 40 and 9
  changed lines, including an encoding regression that mojibakes every em dash and a
  reference to an undefined `PARALLEL_READ_TOOLS` that makes `harness.public.
  tool_manifest()` raise. They are left untouched deliberately: repairing another
  session's in-flight edit is not a parity change and would guess at its intent.

- **chore(repo): discoverability pass — the repository moves to
  `dumbpabloo/black-eclipse` and gains a real landing page.** `BlackEclipse-V1.0`
  tokenised as `blackeclipse` + `v1` + `0`, so neither "black eclipse" nor
  "eclipse" matched it; the new name keeps two clean tokens and drops the stale
  version. Every hard-coded reference moves with it: `README.md`,
  `package.json` (repository/homepage/bugs), `config/updates.json` (the live
  updater contract), `scripts/check-downloads.js`,
  `scripts/install-release.{sh,ps1}`, `scripts/desktop-package.js`,
  `.github/workflows/desktop-release.yml`, `.github/ISSUE_TEMPLATE/config.yml`.
  New `site/index.html` + `.github/workflows/pages.yml` publish a static
  landing page (no build, no server) so the `homepage` field and the canonical
  URL point at real content. The MIT scope note moved below the standard MIT
  block so GitHub detects the licence instead of `NOASSERTION`. The commit
  identity (`scripts/install-git-hooks.js`, `.githooks/pre-commit`) is now
  `dumbpabloo <121116805+dumbpabloo@users.noreply.github.com>`; history is
  intentionally not rewritten. Docs: `docs/UPDATES.md`,
  `docs/repository-guards.md`, `docs/install.md`, `docs/DEPLOYMENT.md`,
  `docs/OPEN-SOURCE-READINESS.md`, `docs/NAVIGATION.md`.

- **feat(context): history compaction is now a token-budgeted verbatim tail plus a
  five-section structured summary.** The old bound was 20 turns / 40 000 chars, then a
  free-form 12-line narrative over 400-char-clipped items — a real coding session forgot
  its files and decisions. `context_budget.KEEP_TAIL_TOKENS` (15 000 tokens / 60 000
  chars, minus a 2 000-token summary reserve) now drives `_bounded_history` on a KNOWN
  window; it walks the history backwards, keeps a contiguous newest suffix, and keeps
  the newest turn even when that turn alone exceeds the tail. Unknown windows keep the
  conservative 20/40 000 fallback byte-for-byte. `compact_dropped` always builds a
  deterministic `OBJECTIVE / DECISIONS / WORK STATE / NEXT MOVE / FILES` summary
  (`build_structured_summary`, deterministic file extraction) and the model prompt now
  asks for those exact labels over a 12 000-char transcript instead of 400-char clips;
  the block remains fenced and labelled DATA. `memory.history_bounded` reports
  `keepTailTokens` and the budget policy. Tests: `test_context_budget.py` (token tail,
  newest-turn survival), `test_history_compaction.py` (five sections, DATA label, file
  extraction, structured prompt), `test_history_bounded.py` updated. Docs:
  `docs/harness/coding-parity-opencode.md`, `docs/NAVIGATION.md`.

- **feat(code): `read_file` now answers a wrong path with candidates.** A mistyped
  path used to return a bare not-found while `code_edit` already offered the nearest
  paths. `fs/path_hints.with_path_suggestions` now adds the same bounded
  `nearest_paths` result (exact basename → suffix → fuzzy, never file contents) to the
  refusal text ("Did you mean one of these?") and to `details.candidates`. The helper
  lives in its own module so `fs_tools.py` stays at its size ratchet (re-pinned by the
  three-line hook). Test:
  `test_code_edit_primitive.py::test_read_file_suggests_candidates_for_a_wrong_path`.
  Docs: `docs/harness/coding-parity-opencode.md` (tool parity row),
  `docs/harness/code-filesystem.md` (edit lifecycle).

- **feat(code): coding runs get room to finish — 48 steps / 20 minutes, and read-only
  tools now run in parallel by default.** Two measured loop gaps against OpenCode: a
  coding turn was capped at 24 steps / 600 s, and `HARNESS_PARALLEL_TOOLS` defaulted
  `off` with only three response tools marked `parallel_safe`. `graph.py` raises the
  coding autonomy to 48 steps and 1 200 s (the hard caps and duplicate/loop guards stay
  in force). `config/flags.py` defaults the parallel flag to `on`; the contiguous-batch
  engine is unchanged (`tool_turn.py`), and the fs read/search/metadata tools, the CIUE
  reads (except `code_index_workspace`, which warms shared state) and `workspace_status/
  list/search/read/docs/diff` now opt in. Tests: `test_flags.py` updated; the parallel
  engine's own tests already covered batching. Docs:
  `docs/harness/coding-parity-opencode.md` (#2/#3).

- **feat(code): the edit tool now matches like the best agents — tolerant tiers,
  real line-ending preservation, bulk replace, and miss hints.** `code_edit` was
  exact-only; a curly quote, an NBSP, a trailing space or a CRLF checkout could
  refuse an edit the model had correctly read, and a replacement written with
  `\n` into a CRLF file silently mixed endings. `code/fs/editing.py` now tries
  exact → case-insensitive → typographic (quotes/dashes/spaces) → per-line
  trailing-whitespace tiers on an LF view, maps spans back to the source, and
  gives every replacement the file's dominant ending. A miss returns the closest
  numbered lines (`_miss_hint`, 12 lines × 200 chars) in `details.hint`, and
  `code_edit` forwards it. `replaceAll` was added to `code_edit` (count=0) and
  `replaced` now reports the real number. Tests: `test_fs_editing.py` (+6),
  `test_code_edit_primitive.py` unchanged-pass. Docs:
  `docs/harness/coding-parity-opencode.md`, `docs/NAVIGATION.md`.
- **fix(disclosure): a coding turn keeps its read/search/inspect tools.** Tiered
  disclosure hid every read-only tool outside a finance-focused `FOCUS_READ_TOOLS`
  set, so a prompt like "make the failing test pass" could lose `read_file` /
  `search_text` / LSP diagnostics behind a `tool_search` round-trip while every
  effectful tool stayed bound. `tool_disclosure.py` now has
  `CODE_FOCUS_READ_TOOLS` (project, filesystem, CIUE, checkpoint reads) unioned
  into the kept set; only names already in the allowlist are kept, so finance
  runs are unchanged. Test: `test_tool_disclosure_bm25.py`
  `test_coding_read_tools_stay_bound_on_a_coding_surface`.
- **docs(code): `docs/harness/coding-parity-opencode.md` — the OpenCode 2.3.0
  (`6531f17`) comparison.** Evidence-backed per area (loop, context, tools,
  subagents, permissions, code intelligence, front door), with the ranked gap
  list and an explicit "where Eclipse is already better" section (verification
  ledger, policy/sandbox, LSP, read-before-edit). `docs/NAVIGATION.md` gains the
  routing row.

- **feat(parity): the last two rows — run state and slash matching — bring the register to
  **32 of 32**. Nothing in `docs/harness/parity-register.json` now rests on file-presence
  alone.** References fetched (MIT): `agent/src/core/state.py` and
  `agent/cli/commands/slash_router.py`.
  - **`core/run_state.py`** — the reference's subprocess runner is a stated non-goal
    (Eclipse executes declarative strategies in-process), but the **state contract** was
    missing: three terminal statuses with **`cancelled` distinct from `failed`** (a user
    stop is not an outage — conflating them made the reference's Reports view contradict
    its own chat history), `failed` requiring a reason, `success` forbidding one, and a
    flush + `fsync` + `rename` write so a crash cannot leave a truncated state file that
    reads as *still running*.
  - **`slash_commands.py`** — the REPL was retired for the desktop window, so the
    **matching contract** existed only in TypeScript. Ported the reference's ladder
    (`prefix 100+len`, `substring 50+len`, `subsequence 10+matched`, `0`, bare `/` → `1`),
    alias resolution before scoring (`/q` → `quit`), registry-order tie-breaking, and
    `/`-only matching, as one surface-neutral module a palette, a job surface or a future
    CLI can share.
  Tests: `test_vibe_parity_tier23.py` (21) — the terminal vocabulary and the
  cancelled≠failed distinction, the reason rules, an unknown status refused with the
  vocabulary, the durable/atomic write and its replacement, then the scoring ladder
  (including prefix > substring > subsequence), token parsing, alias resolution,
  registry-order ties, the fresh-list guarantee and exact lookup.
  Register: **32/32** — parity complete; `portfolio` remains the single genuinely
  equivalent row, the other 31 each needed a fix.
  Slices re-run after the change, all `pytestExit=0`: t-z 1,923 (+21 tier-23),
  n-p 1,068. Combined with the five already green on this tree (a-c 2,879, d-f
  1,293, g-i 1,599, j-m 1,780, q-s 2,606): **13,209 passed, 0 failed**.

- **fix(security): a credential in a query string reached the access log verbatim.**
  Reference: `HKUDS/Vibe-Trading/agent/src/api/security.py` (fetched, MIT). Eclipse
  already redacted config values *by key name* (`channels/config.py`) and trace spans
  (`session_index.redact_span`), but nothing handled a **URL**: `GET /v1/events?api_key=
  secret` lands in Uvicorn's access log, and from there in the proxy log, the browser
  history and any `Referer` header.
  - **`security/log_redaction.py`** — the reference's narrow rule: replace the VALUE of
    `api_key=` / `ticket=` (case-insensitive, quote-terminated values included), keep the
    parameter NAME so an operator can see which credential to rotate, and return ordinary
    text unchanged. Idempotent: the placeholder matches nothing on a second pass.
  - **`RedactionFilter`** applies it to both the message template and its string
    arguments, never drops a record (redaction must not become a way to lose an audit
    line), and `install_log_redaction()` is **idempotent** — an in-process restart cannot
    stack filters per boot.
  Tests: `test_vibe_parity_tier22.py` (15) — multiple secrets per line, case variants, a
  quoted value, ordinary text untouched, idempotent substitution, the filter's template
  and tuple paths, non-string arguments, and the install-twice check.
  Register: **30 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: q-s 2,606, t-z 1,902 (+15
  tier-22), n-p 1,068. Combined with the four already green on this tree (a-c
  2,879, d-f 1,293, g-i 1,599, j-m 1,780): **13,188 passed, 0 failed**.

- **feat(spawn): the manual research-task board's rules — a finished task cannot be
  reassigned and a retry cannot rewrite its result.** Reference:
  `HKUDS/Vibe-Trading/agent/src/pixel_agents/office.py` (fetched, MIT). Eclipse's board
  (`spawn/board.py`) is a generic key/value board (write/read/search) with no notion of a
  task that is assigned, completed, and whose load is derived. Ported the three
  invariants that a naive board gets wrong, as pure rules
  (`spawn/office_rules.py`):
  - **a completed task cannot be assigned** — its owner is history, not a slot;
  - **a re-completion preserves the first recorded result** — a retry or duplicate tool
    call must not replace the answer that was actually produced;
  - **load is derived** from assigned-and-unfinished tasks, so the busy/idle counts
    cannot drift from the tasks they describe.
  Plus the reference's vocabularies (`low`/`normal`/`high`, `Operator`/`Senior`/`Lead`),
  its field limits (100/200/2000), auto-assignment by `role == workstream` with an
  actionable refusal when nothing matches, and a board that labels itself
  `manual_planning` — assignment is organizational metadata, never trading authority.
  The SQLite persistence layer stays out of scope and is **not** claimed.
  Tests: `test_vibe_parity_tier21.py` (21) — the vocabularies and limits, explicit/
  auto/unknown assignment, the completed-task refusal, input immutability, both
  completion rules, the over-long/empty result refusals, and the derived dashboard's
  consistency and non-mutation. Register: **29 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: j-m 1,780 (spawn/ledger),
  t-z 1,887 (+21 tier-21), n-p 1,068. Combined with the four already green on this
  tree (a-c 2,879, d-f 1,293, g-i 1,599, q-s 2,606): **13,173 passed, 0 failed**.

- **fix(openbb): the bridge accepted a request whose widgets it then ignored — so a model
  could answer about a chart it had never seen.** Reference:
  `HKUDS/Vibe-Trading/agent/src/openbb_bridge/context_injector.py` (fetched, MIT).
  Eclipse injected the request's attached context items only; `QueryRequest.widgets` was
  parsed and dropped.
  - **The widget block now says the thing that matters**: *"Only names and parameters are
    available — their values are NOT attached, so use your own data tools to fetch
    anything you need instead of guessing."* OpenBB does not put widget values in a
    `/v1/query` request (that needs a `get_widget_data` round trip this bridge does not
    implement), so a model shown "widget: AAPL price" would otherwise answer from memory.
  - **The reference's numeric limits**, pinned as constants: **10** widgets (with
    `...and N more`), **200**-char values, **8** parameters per widget, and an unnamed
    widget labelled rather than dropped.
  - Rendered as a fenced `<workspace-widgets>` block after the context block; a request
    with no widgets produces no block and the message is byte-identical.
  Tests: `test_vibe_parity_tier20.py` (11) — the honesty line, the fencing and parameter
  rendering, all three caps, the more-line, unnamed/junk tolerance, coexistence with
  context, and the user-turn guard. Bridge suites re-run: **27 passed**.
  Register: **28 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: n-p 1,068, t-z 1,866 (+11
  tier-20). Combined with the five already green on this tree (a-c 2,879, d-f
  1,293, g-i 1,599, j-m 1,780, q-s 2,606): **13,152 passed, 0 failed**.

- **feat(discovery): evidence is now dated and gated by arithmetic, not by a stored
  verdict.** References: `HKUDS/Vibe-Trading/agent/src/strategy_discovery/{facade,models}.py`
  (fetched, MIT). The discovery surface could list strategies but had no arithmetic for
  whether the evidence behind one is still any good. Ported the pure helpers with the
  reference's constants into `quant/discovery/evidence.py`:
  - **coverage is anchored to month-END** — a range ending `2024-06` covers through
    30 June, so the span runs from the first day of the earliest start to the last day
    of the latest end; malformed ranges contribute nothing and an undatable span is `0`,
    not a guess.
  - **the quality ladder** — below 10 trades in a regime is `insufficient`, below 730
    days of coverage `marginal`, else `adequate`.
  - **decay is read-time** — `stale` at ≥180 days, `aging` at ≥90, and an exact
    threshold belongs to the **stricter** side; a window that cannot be dated is
    **stale fail-closed** (evidence nobody can date cannot vouch for anything). The
    gating age is the **evidence window's**, never `last_verified` — a scheduled
    refresh of unchanged artifacts must not keep a dead row fresh; `staleness_days` is
    reported beside the verdict and never used to gate.
  - **breakeven fee** — `ln(1+r)/(2·trades·size)·10_000`, returning `None` (never
    infinity) for no trades, a ≤−100% return, non-finite input or non-positive sizing.
  - **warnings carry stable prefixes** (`insufficient-trades:`, `short-coverage:`,
    `cost-sensitive:`, `stale-evidence:`, `aged-evidence:`) so a tool can match them
    without parsing prose, plus the buffered **borderline** bands.
  Tests: `test_vibe_parity_tier19.py` (18) — the month-end anchoring (761 days for
  2020-01→2022-01), the ladder boundaries, both decay thresholds including the exact
  threshold landing on the stricter side, fail-closed dating, the log identity and its
  `None` cases, borderline bands and warning prefixes. Register: **27 rows** with
  behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: q-s 2,606 (discovery/quant),
  t-z 1,855 (+18 tier-19), n-p 1,068. Combined with the four already green on this
  tree (a-c 2,879, d-f 1,293, g-i 1,599, j-m 1,780): **13,141 passed, 0 failed**.

- **feat(hypotheses): the registry contract — a derived id, a closed status vocabulary,
  and a search whose `limit=0` means zero.** Reference:
  `HKUDS/Vibe-Trading/agent/src/hypotheses/registry.py` (fetched, MIT). Eclipse
  pre-registered a hypothesis with its acceptance rule before a trial (the
  methodological core, in `quant/store.py`), but had none of the registry semantics:
  - **`hyp_` + 12 hex of `sha256("title|created_at")`** — derived, so the same hypothesis
    recorded at the same instant gets the same id and a re-import is idempotent rather
    than a duplicate row; `_2`/`_3` only on a real collision.
  - **status is closed** — `exploring`/`testing`/`validated`/`rejected`/`monitoring`,
    lower-cased, and an unknown value is refused with every allowed status named.
  - **search ranks by token overlap then recency**, over the whole record including
    links, with `limit` clamped to `0..100` and **`0` returning nothing** — an explicit
    zero from a caller must not become "at least one".
  Ported as a pure module (`harness/hypotheses/registry.py`); storage stays with the
  existing store rather than being duplicated.
  Tests: `test_vibe_parity_tier18.py` (23) — id derivation and collisions, the status
  vocabulary, ranking with a recency tie-break, status filtering, links in the haystack,
  the limit semantics including negatives, and CJK title search.
  Register: **26 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: g-i 1,599, t-z 1,837 (+23
  tier-18), n-p 1,068. Combined with the four already green on this tree (a-c
  2,879, d-f 1,293, j-m 1,780, q-s 2,606): **13,123 passed, 0 failed**.

- **fix(code): a shell command could kill the harness itself — `pkill python` had no
  guard.** Reference: `HKUDS/Vibe-Trading/agent/src/tools/_shell_safety.py` (fetched,
  MIT). The harness runs in Python, so `pkill python`, `killall python`,
  `taskkill /IM python.exe` or `powershell Stop-Process -Name python` terminates the
  process executing the command: the turn looks successful and then the agent is dead,
  which is indistinguishable from a crash. Eclipse had **no such guard** — the shell went
  straight to the sandbox.
  - **`code/shell_safety.py`** — the reference's four patterns, checked **per shell
    segment** (`;`, a bare `&`, CR/LF) so `echo hi && pkill python` is caught, quotes
    flattened so quoting cannot smuggle the flag, case-insensitive, and an actionable
    refusal naming the alternative (cancel the specific task by id).
  - **Wired into `code/shell.py` before the sandbox spawns**, so the refusal costs
    nothing (exit 126, `duration_ms=0`) and no process is started.
  - **One shared limit is stated, not papered over:** a bare `get-process python |
    stop-process` *without* a `powershell` prefix is not matched — the reference's
    PowerShell guard is scoped to a PowerShell command line either.
  Tests: `test_vibe_parity_tier17.py` (34) — fifteen refused spellings and twelve
  allowed commands (targeted pids, `pkill -f uvicorn`, a quoted mention, `taskkill
  /IM node.exe`), plus the wiring: refusal without spawning, and an ordinary command
  still executing. Shell/sandbox/policy suites re-run: **156 passed**.
  Register: **25 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: d-f 1,293 (code/shell),
  t-z 1,814 (+34 tier-17), n-p 1,068. Combined with the four already green on this
  tree (a-c 2,879, g-i 1,599, j-m 1,780, q-s 2,606): **13,100 passed, 0 failed**.

- **feat(governance): the ledger now reports *why* it broke, refuses to extend a broken
  chain, and exports offline.** References: `HKUDS/Vibe-Trading/agent/src/governance/
  {ledger,manifest}.py` (fetched, MIT). The chain and manifest were ported earlier;
  comparing behaviour found three capabilities missing:
  - **`verify_report()`** returns the reference's structured break —
    `{ok, recordCount, firstBreak: {index, seq, reason, detail}}` with five stable reason
    codes (`malformed_json`, `missing_fields`, `seq_gap`, `prev_hash_mismatch`,
    `hash_mismatch`) — instead of raising a sentence, so an API or UI can act on it.
  - **`append_refusing_a_broken_chain()`** verifies the WHOLE existing chain before
    appending. Previously only a torn tail was refused, so a valid-looking suffix could
    be built on top of an edited earlier record — exactly the "self-healing chain"
    failure the reference calls out. `verify()` still raises for existing callers.
  - **`export()`** embeds every record, the verification and a hash over the envelope,
    so an edited exported record is caught even when the embedded records still chain.
  - **Manifest**: readable `extra` dimensions (provider/model) now enter the digest, and
    `verify_hash()` re-derives it (a persisted hash is trusted at load).
  Two **format divergences are stated, not changed**: our `seq` is 0-based and our
  hashes carry no `sha256:` prefix, where the reference uses 1-based `seq` and prefixed
  digests — rewriting them would invalidate every record already written.
  Tests: `test_vibe_parity_tier16.py` (44) — every reason code with the record it broke
  on, never raising; the refusal to extend; a sound chain still appending; the export
  and its envelope hash; `extra` in the digest; `verify_hash` catching a hand edit; and
  JSON round-tripping. Register: **24 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: g-i 1,599 (governance),
  t-z 1,780 (+44 tier-16), n-p 1,068. `docs/harness/benchmarks.md` went stale on the
  count change and was regenerated with `npm run eval:benchmarks` (the
  `test_published_benchmarks` gate caught it, which is the gate working); its own
  test then passed 3/3. Combined with the four already green on this tree
  (a-c 2,879, d-f 1,293, j-m 1,780, q-s 2,606): **13,032 passed, 0 failed**.

- **feat(factors): an input is now described by WHEN it can be known, not only whether
  it is present.** Reference: `HKUDS/Vibe-Trading/agent/src/factors/data_availability.py`
  (fetched, MIT). Eclipse refused an absent declared input correctly
  (`MISSING_INPUT`), but had no status vocabulary and no point-in-time notion, so
  "present" and "knowable at the time" were the same statement.
  - **`factors/contracts.py`** gained `availability_mode` (bar close / filing date /
    session date / effective date / unknown, by field kind) and `field_availability`,
    which applies the reference's decision order exactly: an explicit status wins; an
    absent **required** input is `failed` while an absent optional one is `unknown`; a
    **fundamental stays `unknown`** even when present and sourced, because a filing date
    is not a publication timestamp (a one-bar shift is protection, not proof); universe
    membership is `verified` only with a point-in-time flag; a sourced field is
    `assumed`; anything else `unknown`.
  - **`pitSafe` is true only for `verified`** — the rest is exactly the data that looks
    available and is not safe to treat as known-at-the-time.
  Tests: `test_vibe_parity_tier15.py` (25) — the vocabulary, twelve field-kind modes,
  every branch of the decision order, an out-of-vocabulary explicit status ignored, and
  the existing refusal agreeing with the status view on the same fixture. Factor and zoo
  suites re-run: **218 passed**.
  Register: **23 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: d-f 1,293 (factor suites),
  t-z 1,760 (+25 tier-15). Combined with the five already green on this tree
  (a-c 2,879, g-i 1,599, j-m 1,780, n-p 1,068, q-s 2,606): **12,985 passed,
  0 failed**.

- **fix(security): the model-endpoint guard allowed `100.100.100.200` — Alibaba
  Cloud's metadata service — and refused the range every fake-IP proxy answers from.**
  Reference: `HKUDS/Vibe-Trading/agent/src/providers/endpoint_guard.py` (fetched, MIT).
  - **Allowed a credential target.** The old predicate was
    `is_private or is_loopback or is_link_local`, and `100.100.100.200` is **none of
    those** — it lives in the shared `100.64.0.0/10` space. A user-supplied
    OpenAI-compatible endpoint could therefore reach cloud metadata. Replaced with the
    reference's rule: `is_global and not is_multicast`, with an IPv4-mapped IPv6
    address judged by the IPv4 address it carries.
  - **Refused a whole class of working hosts.** `198.18.0.0/15` is what Clash, Surge
    and sing-box answer *every* lookup from in TUN mode; refusing it in all forms broke
    every endpoint on such a machine. It is now allowed **only when the address came
    from a DNS lookup**, never written literally into the URL.
  Tests: `test_vibe_parity_tier14.py` (34) — 20 blocked addresses (incl. the shared
  space and IPv4-mapped), 4 public ones, a metadata literal and a hostname resolving to
  it, one-bad-among-good, the fake-IP carve-out both ways, and `allow_private` for
  local models. Endpoint/model-runtime suites re-run: **126 passed**.
  Register: **22 rows** with behaviour comparisons.

- **fix(onboarding): short windows clipped the Continue/Skip footer, and the
  stepper rail was not a second navigation path.** The left rail's full stepper
  forced a min-content height on the grid row, so `overflow-hidden` cut the footer
  off below the card; the rail and step body are now independent `min-h-0` scroll
  areas, the footer/header are `shrink-0`, the card has a `100vh` fallback with a
  `100dvh` refinement, and the header description hides under 620px of height.
  Each stepper row (disc included) is now one button: after the risk notice is
  acknowledged every step is clickable, the current step is disabled and marked
  `aria-current="step"`. Also `fix(onboarding AI)`: the connect panel no longer
  collapses on success (its model picker was never seen), and the connected card
  gained a real **Test connection** probe and a saved-provider model picker;
  "Set up later" is only offered when nothing is connected so it cannot wipe a
  working key.
  Docs: `docs/onboarding-plan.md` (frame/footer + rail + AI step).

- **fix(chat): the composer kept showing Ollama's models after connecting an
  API.** `useChatModel` fetched once on mount, but Settings is a tab in the same
  shell, so a connection made there never reached the already-mounted chat or the
  floating dock; a saved keyless Ollama choice also outlived the new provider.
  Added `lib/agent/llm-config-events.ts` (`eclipse:llm-changed`): connect,
  activate, disconnect and key removal now re-fetch every composer, connect
  events clear the stale per-chat choice, and a keyless local pick yields to a
  different saved usable provider. `/api/harness/chat/models` now resolves the
  active provider's key through `llmKeyEnvVar` (edited base URLs and named custom
  connections report available, with their endpoint and active model), and
  `connectProvider` records the probe's models into the durable discovery
  snapshot so the API's real catalog appears immediately and after reload.
  Docs: `docs/model-connections.md` (composer follows the saved connection).

- **feat(jobs): ported the two-phase proposal state machine — a job change had no
  confirmation step at all.** Reference: `HKUDS/Vibe-Trading/agent/src/scheduled_research/proposals.py`
  (fetched, MIT). Without it, an expired or superseded confirmation had nowhere to be
  refused, and there was no notion of a job the scheduler owns being off-limits.
  `harness/jobs/proposals.py` is the pure state machine, clock injected:
  - ids are **`srp_` + 32 hex**, validated *before* any path is built;
  - **15-minute TTL**; committing exactly at the deadline works, one millisecond past
    it refuses **and marks the record `expired`** so a stale confirmation cannot land;
  - `pending → committed | discarded | expired` is one-way, and committing an
    already-committed proposal is **idempotent** (a retried confirmation must not
    create the job twice);
  - **system jobs** (`system:` prefix or `source_type == "system"`) are untouchable at
    propose **and** re-checked at commit, because a proposal record can be hand-edited
    between the two.
  Tests: `test_vibe_parity_tier13.py` (30) — TTL boundaries, idempotence, the executor
  guard, a vanished cancel target, the three system-job refusals, the tampered-record
  re-check, and the bad-input cases.
  **Wired, not library-only:** `automation/tools.py::_delete` now issues a 15-minute
  proposal instead of deleting, and only a committed, **single-use**, bot-matched
  confirmation removes the bot. A system job is refused outright. Two existing tests
  that deleted in one call were updated to the two-step flow rather than exempted
  from it (`test_automation_tools.py`), and six wiring tests were added to
  `test_vibe_parity_tier13.py` (now 36): nothing deleted before confirmation, delete
  exactly once, no reuse, mismatch refused, system job refused, unknown id offers a
  way forward. Automation suites re-run: **204 passed**.
  Register: **21 rows** with behaviour comparisons.
  Slices re-run after the wiring, all `pytestExit=0`: a-c 2,879, j-m 1,780,
  t-z 1,701, n-p 1,068. Combined with the three already green on this tree:
  **12,926 passed, 0 failed**. One `test_terminal_process` failure appeared on the
  first t-z pass and passed 16/16 in isolation; the slice was re-run rather than the
  failure explained away, and the re-run is clean.

- **feat(security): ported the prompt-injection scanner and control-token defanging —
  external text could previously forge a role boundary.** Reference:
  `HKUDS/Vibe-Trading/agent/src/security/scanner.py` (fetched, MIT). Eclipse had
  injection *phrases* in three unrelated places (browser policy, computer security,
  secret redaction) and **nothing that defanged chat-template tokens**.
  - **`harness/security/injection.py`** — the reference's five rules with severities
    (instruction override, system-prompt exfiltration, role/channel impersonation,
    secret exfiltration, tool abuse), at most one finding per rule in a stable order,
    and **no rewrite because of a finding**: a source is annotated, never dropped.
  - **`neutralize_special_tokens`** — a zero-width space after the opening delimiter of
    every control token: ChatML (`<|im_start|>`), **DeepSeek's fullwidth-bar family**
    (`<｜User｜>`, the shipped default model), Llama `[INST]`/`<<SYS>>`/`<s>`, and
    Gemma's turn tokens. Idempotent; clean text is returned as the same object; the
    inner run is capped at 64 chars so a 10 KB `<|aaaa…` cannot backtrack.
  - **Wired** into `browser/policy.py::wrap_untrusted` — the single choke point every
    browser observation passes through — so a page can no longer impersonate a system
    or user message.
  Tests: `test_vibe_parity_tier12.py` (70) — 10 positive and 6 negative rule cases,
  rule-order stability, excerpt bounding, **all twenty token families** defanged and
  visually identical, idempotence, same-object for clean text, the adversarial
  10 KB pseudo-token, payload selectors with dotted paths, and the `wrap_untrusted`
  integration. Browser suites re-run: **183 passed, 33 skipped** (Playwright absent).
  Register: **20 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: a-c 2,879, t-z 1,665 (+70
  tier-12), n-p 1,068. Combined with the four already green on this tree:
  **12,890 passed, 0 failed**.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (20-row table: nineteen needed a
  fix, `portfolio` the only genuinely equivalent one).

- **fix(context): the zero-cost fold was too eager — it stripped the middle of a ~2 KB
  tool result the next turn then had to re-fetch.** Reference:
  `HKUDS/Vibe-Trading/agent/src/agent/context_compact.py` (fetched, MIT), whose layer-2
  constants are **min 2 400 chars, head 900, tail 500**. Eclipse folded at 1 200 with
  400/400.
  - **Defaults adopted**: `COLLAPSE_MIN_CHARS = 2400`, `COLLAPSE_HEAD_CHARS = 900`,
    `COLLAPSE_TAIL_CHARS = 500`. Six compaction/history suites re-run: **63 passed**.
  - **Two places where Eclipse already beat the reference** are now pinned: the marker
    names the exact omitted count **and** a sha256 of the full original (so a reader can
    verify the fold), and folding is **idempotent** — the reference would fold its own
    marker again on a second pass, compounding the loss.
  Tests: `test_vibe_parity_tier11.py` (13) — the three constants, a text exactly at the
  threshold untouched and one char past it folded, head/tail preserved byte-for-byte,
  the exact omitted count, the digest verifying the original, idempotence, the
  parameterised path reproducing the reference's numbers, and turn-level
  non-mutation.
  Register: **19 rows** with behaviour comparisons.
  **Full sweep on this tree, every slice `pytestExit=0`:** a-c 2,879, d-f 1,293,
  g-i 1,599, j-m 1,780, n-p 1,068, q-s 2,606, t-z 1,595 -> **12,820 passed,
  70 skipped, 0 failed**.
  One honest note on the way there: the first t-z run was left in the background for
  **4 h 08 m** (machine contention) and reported 10 failures in the terminal and
  trading-bot files. Both files pass in isolation (18 passed / 22 passed) and the
  re-run above is clean — those were timeouts under load, not regressions, which is why
  the slice was re-run rather than explained away.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (19-row table: eighteen needed a
  fix, `portfolio` the only genuinely equivalent one).

- **fix(market): the provider breaker used a FLAT retry interval — a dead source was
  hammered at the same cadence as one that failed once.** Reference:
  `HKUDS/Vibe-Trading/agent/src/data_gateway/routing.py::NetworkRouter` (fetched, MIT),
  whose schedule is `retry_at = now + min(lifetime, cooldown * 2**(failures-1))`.
  - **`mcp/circuit.py`** gained `backoff_cooldown_s(failures, base_s, lifetime_s)` —
    doubling per consecutive failure, capped at a lifetime, with the exponent bounded so
    10,000 failures cannot overflow a float.
  - **`CircuitBreaker`** gained an optional `lifetime_s`; when set, `cooldown_s` is the
    BASE of the exponential schedule. Default `None` means every existing caller
    (MCP servers) keeps the flat cooldown byte-for-byte.
  - **`market/router.py`** now constructs provider breakers with the schedule
    (`HARNESS_MARKET_BREAKER_LIFETIME_S`, default 300s), and `market/config.py` carries
    the new cap. `ProbeBudget` is unchanged.
  Also pinned: both routers only **reorder the candidates they are given** — neither
  invents a source, which would bypass the capability/asset-class filters.
  Tests: `test_vibe_parity_tier10.py` (33) — the formula on three base/lifetime pairs ×
  eight failure counts, the doubling-then-cap sequence, 10,000-failure overflow,
  breaker open → half-open at the *effective* cooldown, re-arm, reset on success, the
  flat path when no lifetime is set, and candidate-only reordering. Circuit/router
  suites re-run: **204 passed, 2 skipped**.
  Register: **18 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: a-c 2,879, j-m 1,780, q-s 2,606,
  t-z 1,582 (+33 tier-10), n-p 1,068. Combined with the two already green on this tree:
  **12,807 passed, 0 failed**.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (18-row table: seventeen needed a
  fix, `portfolio` the only genuinely equivalent one).

- **feat(swarm): the DAG layering matched the reference, but a cycle error named no
  cycle.** Reference: `HKUDS/Vibe-Trading/agent/src/swarm/task_store.py`
  (`topological_layers` + `validate_dag`, fetched, MIT). Eclipse's `team/graph.py`
  already produced the same **layer partition** on every shared shape (linear,
  diamond, two roots, disconnected, deep, wide) and `validate_graph` already reported
  unknown dependencies, duplicates and cycle paths — two things the reference's
  `validate_dag` does not do. The one real difference:
  `topological_layers` raised `"the task graph contains a dependency cycle"` with **no
  path**, where the reference prints it; it now uses `detect_cycle` and names the
  cycle, so an operator does not have to find it by hand.
  Tests: `test_vibe_parity_tier9.py` (15) — the layer partition compared to a
  reimplementation of the reference on eight DAG shapes, the every-prerequisite-is-
  earlier property, the diamond's parallel middle layer, cycle naming, self-loop,
  unknown dependency, duplicates, and readiness agreeing with the layering.
  Team suites re-run: **131 passed, 1 skipped**.
  Register: **17 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: t-z 1,549 (+15 tier-9), n-p 1,068
  (register). Combined with the five already green on this tree: **12,774 passed,
  0 failed**.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (17-row table: sixteen needed a
  fix, `portfolio` the only genuinely equivalent one).

- **fix(trade, goals): two fail-open gaps the reference does not have — a missing
  broker field walked past a configured cap, and an execution order was an accepted
  research goal.** References fetched from `HKUDS/Vibe-Trading` (MIT):
  `live/enforcement.py::check_mandate` and `goal/policy.py`.
  - **`trade/policy.py` was fail-OPEN on a missing field.** Every cap was guarded by
    `value is not None`, so a broker preview that omitted `margin`, `volume`,
    `notional` or `openPositions` **skipped the cap entirely** — the opposite of the
    reference's contract ("any unparseable input … denies"). Each capped dimension now
    denies with a named `policy.<cap>-unverified` rule when its field cannot be read;
    `0` still means off, and spread stays best-effort because some connectors do not
    publish it (stated in the docstring, not silently).
  - **`harness/goals/policy.py` did not exist.** "Buy 100 shares of NVDA now" was an
    accepted research goal. Ported the reference's seven patterns and wired them into
    `GoalRuntime.create`, so the refusal is a `GoalError("EXECUTION_OBJECTIVE")` at the
    creation seam before any task is dispatched. The two deliberate non-matches are
    preserved and tested: a bare asset noun is not a cue, and `long-term`/`short-term`
    are horizons (a word boundary alone misfires, so the patterns carry a negative
    lookahead).
  Two structural differences are stated rather than hidden: Eclipse returns **every**
  breach in one response where the reference returns the first with a DENY/PAUSE
  `kind`; and its rule set has no asset-class/leverage/funding chain.
  Tests: `test_vibe_parity_tier8.py` (35) — the accept/reject table (13 refused, 9
  accepted, incl. the horizon false-positives), the creation-seam guard, fail-closed on
  four caps, `0`-means-off, and every-breach reporting. Existing trade/goal suites
  re-run green: `test_trade_policy` + `test_broker_orders` + `test_trade_mandate_mode`
  + tier-8 **93 passed**, goal suites **43 passed**.
  Register: **16 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: a-c 2,879, g-i 1,599,
  t-z 1,534 (+35 tier-8), n-p 1,068. Combined with the three already green on this
  tree: **12,759 passed, 0 failed**.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (16-row table: fifteen needed a
  fix, `portfolio` the only genuinely equivalent one).

- **feat(quant): multiple-testing control completed — a standalone probabilistic
  Sharpe and Benjamini–Hochberg FDR, the two pieces the reference has and Eclipse
  did not.** Reference: `HKUDS/Vibe-Trading/agent/src/quantlib/multipletesting.py`
  (fetched, MIT), reimplemented in the test rather than imported.
  - **`probabilistic_sharpe_ratio`** — Eclipse computed PSR privately inside the
    deflated Sharpe with no benchmark argument and no guards. Now a public function
    with the reference's semantics: `Phi[(SR−SR*)·sqrt(T−1)/sqrt(1−g3·SR+(g4−1)/4·SR²)]`,
    a **30-observation floor** (the moment estimates are meaningless below it), a
    **kurtosis ≥ 1** check that catches an excess-kurtosis caller before it
    *overstates* confidence, and a refusal when the variance term is non-positive
    (mutually inconsistent moments) instead of returning a plausible number.
  - **`benjamini_hochberg`** — absent entirely. Step-up rejection, **monotone**
    adjusted p-values (running minimum from the largest rank down, clipped at 1),
    the largest rejected raw p-value as `threshold`, results in the caller's order.
    This is the right tool when the question is "which of these 462 factors are
    real" rather than "is the best one real" — deflating one maximum answers only
    the second.
  Tests: `test_vibe_parity_tier7.py` (127) — PSR against the reference closed form on
  **96 combinations** of Sharpe × observations × benchmark × (skew, kurtosis) to
  1e-12, the fat-tails-lower-confidence property, all four refusals; BH against the
  reference on **6 p-value sets × 3 FDR levels** plus monotonicity, the empty set,
  `[1.0, 0.0, 0.5]` and the input refusals; and the whole DSR chain
  (expected-max-of-N bar → PSR against it) end to end.
  Register: **13 rows** with behaviour comparisons.
  Slices re-run after the change, all `pytestExit=0`: q-s 2,606 (quant),
  t-z 1,499 (+127 tier-7), n-p 1,068 (register). Combined with the four slices
  already green on this tree: **12,724 passed, 0 failed**.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (13-row table: **twelve** needed
  a fix, `portfolio` the only genuinely equivalent one).

- **fix(quant): two real backtest-metric defects, found by comparing numbers with the
  reference — a first-bar loss reported 0% drawdown, and the first bar's return was
  dropped entirely.** Reference: `HKUDS/Vibe-Trading/agent/backtest/metrics.py`
  (fetched, MIT). `calc_metrics` seeds the high-water mark with `initial_cash`
  (`equity.cummax().clip(lower=initial_cash)`) and `period_returns` measures the first
  bar from cash; Eclipse did neither:
  - **drawdown** — `_drawdown_series` started the peak at `equity[0]`, so an account
    that held 100 and closed the first bar at 95 showed **0%** drawdown instead of
    −5%. Now takes `initial_capital` (the engine passes `inputs.initial_capital`) and
    seeds the peak with it; the one-argument form keeps the old behaviour for callers
    that have no capital.
  - **returns** — `_returns` began at bar 1, so the first bar's P&L was missing from
    every mean, volatility and ratio. Now seeded with `equity[0] / initial_capital − 1`
    when capital is known, which is what makes `prod(1+r) − 1` reproduce
    `final/capital − 1`.
  - **Sharpe** agrees with the reference to 1e-7 on a shared fixture; the only gap is
    the reference's `1e-10` denominator epsilon (Eclipse reports `None` for zero
    volatility rather than a huge ratio).
  - **Sortino** is a **deliberate divergence**, now quantified instead of glossed: the
    sample downside deviation (`n−1`, pinned by `test_quant_audit_fixes.py`) versus the
    reference's population RMS (`n`), so the values differ by exactly
    `sqrt((n−1)/n)`. The test asserts that ratio.
  - **trade statistics**: `profit_factor` is `None` when nothing lost (both sides), and
    the losing streak matches the reference walk.
  Tests: `test_vibe_parity_tier6.py` (19) — drawdown on four fixtures incl. the
  first-bar loss, Sharpe formula equality, the exact Sortino ratio, the `None`
  profit factor, the streak, and annualisation (252 daily / 52 weekly / 12 monthly /
  252×390 one-minute / 252×6.5 hourly). The eight quant-metric suites that could
  regress were re-run: **98 passed**.
  Register: **12 rows** with behaviour comparisons.
  **Full sweep on this tree, every slice `pytestExit=0`:** a-c 2,879, d-f 1,293,
  g-i 1,599, j-m 1,780, n-p 1,068, q-s 2,606, t-z 1,372 -> **12,597 passed,
  77 skipped, 0 failed** (g-i and j-m were re-run after the metric change because
  `MetricsEngine` feeds the graph metrics nodes and the portfolio reports).
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (12-row table: **eleven** needed
  a fix, `portfolio` the only genuinely equivalent one).

- **feat(memory, scheduling): two more numeric/branch comparisons — the missing
  reinforcement arithmetic and the missing DST policy.** References fetched from
  `HKUDS/Vibe-Trading` (MIT), restated in tests rather than imported:
  - **`memory`** — importance was a **static column**: every memory carried a
    `quality_score`/`importance` but nothing ever moved it on feedback. Ported the
    reference's arithmetic into `eclipse_memory/quality.py` as pure functions:
    event deltas (`task_success` +0.10, `task_failure` −0.15, `user_confirm` +0.20,
    `user_reject` −0.30, `passive_decay` −0.05), a **0.7× discount for system-sourced**
    events, a **0.5 absolute per-session cap** that **refuses** (returning `accepted:
    false` and leaving the accumulated delta untouched, so a retry cannot walk past
    it), a `[0,1]` clamp, and the GC thresholds (archive < 0.15, delete < 0.05 **and
    only** with `enable_delete`, never under 7 days).
  - **`scheduling`** — the cron matcher was already parity-grade and stronger than
    the reference in one respect (it accepts `a-b/N` steps and refuses name aliases
    with named codes), but it had **no DST policy**: `next_fire` did wall-clock
    arithmetic with no zone awareness, so a daily 02:30 job could land inside the
    spring-forward gap and a fall-back hour could fire twice. Added
    `local_wall_time_to_epoch_ms` (gap → `None`/skipped, fold → first occurrence) and
    `next_fire_epoch_ms` (days walked on the zone's own calendar, reference's
    `6*366+1`-day horizon, `None` for an impossible date).
  Tests: `test_vibe_parity_tier5.py` (47) — all 30 event/source/quality combinations
  compared to a reimplementation of the reference, the cap sequence walked to
  rejection, the GC boundaries at 6.9/7.0 days and 0.05/0.15/0.20, plus DST on
  `America/New_York` (2024-03-10 gap skipped; 2024-11-03 fold resolved to 05:30Z) and
  an EST/EDT pair proving the same wall time is a different instant.
  Register: **11 rows** with behaviour comparisons, required set widened and asserted.
  Slices re-run after these changes, all `pytestExit=0`: j-m 1,780 (jobs/cron/memory),
  n-p 1,068 (register), t-z 1,353 (+47 tier-5). The other four slices are unaffected
  code: `jobs.cron` is imported only by `automation/{validation,timeutil}.py` and the
  jobs scheduler, all of whose tests live in j-m, and `eclipse_memory/quality.py` is
  new and imported only by the parity test.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (11-row table: **ten** needed a
  fix, `portfolio` the only genuinely equivalent one).

- **feat(entities): the non-bar cash-flow spine was three kinds short, unchecked on
  non-canonical spellings, and had no FX translation.** Reference fetched from
  `HKUDS/Vibe-Trading` (`agent/src/entities/cashflow.py`, MIT) and restated in the
  test rather than trusted on shape:
  - **The sign table matched 9 of the reference's 15 kinds.** Added
    `contribution`, `transfer_in`, `expense` (out) and `interest`, `principal`,
    `transfer_out` (in). A kind missing from the table is a flow whose sign nobody
    checks, so completeness is the contract.
  - **Kind labels were not normalized**, so `"Capital Call"` / `"capital-call"` were
    not sign-checked. `normalize_kind()` now runs at construction — the check bites on
    a broker export's spelling instead of the caller's.
  - **Valuation status came only from the caller.** A `nav` marked
    `is_valuation=False` was counted as income. It is now **derived from the kind**:
    a mark can never be summed into distributions.
  - **`translate_flows()`** closes the FX gap with the reference's rule: each flow at
    **its own settlement date**, a missing rate **raises** (`MissingExchangeRateError`)
    instead of forward-filling, stale reuse is opt-in and **flagged**
    (`fxRateIsStale`), optionally bounded by `max_staleness_days`, and every
    translated flow keeps its original amount/currency/rate provenance.
  Tests: `test_vibe_parity_tier4.py` (27) — the table compared key-for-key, every kind
  checked in both directions, seven spellings normalized, valuation kinds excluded from
  `total()`, TVPI `None` without paid-in capital, own-date rates, gap refusal, flagged
  staleness, bounded staleness, and non-positive/non-finite rates refused.
  The register's behaviour column now covers **9 rows** and the required set is
  asserted.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (9-row table: **eight** rows
  needed a fix, `portfolio` was the only genuinely equivalent one).
  Slices re-run after the entity change, all `pytestExit=0`: d-f 1,293, n-p 1,068,
  t-z 1,306 (the other four slices were already green on this code and no module they
  exercise calls `harness.entities` — a grep confirms the package has no in-tree
  importers).

- **feat(parity): third tier — reconnect replay and the channel status union, both
  missing, plus a wrong comment the comparison exposed.** References fetched from
  `HKUDS/Vibe-Trading` (MIT) and reimplemented in the test:
  - **`session`/`execution_log`**: there was **no replay-by-id** at all — a
    reconnecting client could only re-read everything. Ported the reference's
    `_replay_locked` as `replay_after()` + `ExecutionEventLog.replay()`: *no id* →
    nothing unless `replay_all`; *found id* → everything strictly after it; *evicted
    id* → **nothing**, never a silent restart from the beginning (that is how a client
    ends up rendering duplicates). Buffer bounded at 500, like the reference, so a
    long-dead client gets an honest empty list.
  - **`channels`**: `describe(config_names=...)` now unions the built-in providers with
    the names in the operator's config and flags a configured-but-unimplemented one
    `known: false`, so a channel cannot appear absent while being configured. Reading
    the reference also caught a **wrong comment** in `registry.py`: `PUSH_KINDS` /
    `POLL_KINDS` were documented as a three-way partition, but a `both` provider
    (lark) is legitimately in both — the code was right, the claim was not, and a test
    now asserts the invariant that actually holds.
  Tests: `test_vibe_parity_tier3.py` (17) — replay compared to a reimplementation of
  the reference over 7 branch fixtures, plus the channel surface (19 families,
  unknown-configured visibility, push/poll invariant). The register's behaviour column
  now covers **8 rows**, and the required set is asserted so it cannot silently shrink.
  **Full sweep on this tree, every slice `pytestExit=0`:** a-c 2,879, d-f 1,293,
  g-i 1,599, j-m 1,780, n-p 1,068, q-s 2,606, t-z 1,279 -> **12,504 passed,
  70 skipped, 0 failed**.
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (8-row table: six needed a fix,
  two were genuinely equivalent).

- **feat(parity): second tier of numeric comparisons — artifact evidence diffs and B3
  symbol mapping — and both found a real gap.** References fetched from
  `HKUDS/Vibe-Trading` (MIT), reimplemented in the test rather than imported:
  - **`research_artifacts`**: Eclipse's diff was checksum-only with metadata and
    provenance, and had **no evidence comparison at all** — the one thing a research
    reader re-checks. Ported the reference's **multiset** evidence diff into
    `artifacts/compare.py::compare_evidence`: duplicates are counted (citing a source
    twice then once is a *removal*), a pure reordering is `orderChanged` rather than
    "no change", additions/removals keep source order, and the result now rides the
    report and the Markdown. Content is still compared by checksum, not line diff
    (stated in the module docstring).
  - **`br_data`**: added `cotahist.map_symbol` — the reference's convention for
    reaching the right market: strip `.SA`, then `TPMERC` 20 (odd lot) if the ticker
    ends in `F` after a **fraction digit**, else 10 (cash). Verified on twelve inputs
    including `"F"` and `"GF"`, which must stay cash because there is no fraction
    digit. The parametrised fixtures also cover `.SA`, lowercase, surrounding
    whitespace and `SANB11F`.
  Tests: `test_vibe_parity_tier2.py` (26) — the evidence function is compared to a
  reimplementation of the reference on **7 shared fixtures**, and `map_symbol` on 12.
  The register's behaviour column now covers **6 rows** (was 4), and the register test
  fails if any of them loses its numeric comparison or stops naming its reference.
  **Full suite on the current tree, every slice with `pytestExit=0`:** a-c 2,879,
  d-f 1,293, g-i 1,599, j-m 1,780, n-p 1,068, q-s 2,606, t-z 1,262 ->
  **12,487 passed, 70 skipped, 0 failed** (11 slices re-run in this and the previous
  turn; the earlier `Exit code: 1` on a-c was PowerShell's stderr passthrough, and the
  explicit `pytestExit` now settles it).
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (findings table now 6 rows:
  four needed a fix, two were genuinely equivalent).

- **fix(quant): behavioural parity with the reference found three real gaps that
  "equivalent" had been hiding — the decay state machine, the walk-forward holdout,
  and two RSI divergences.** Every number below was checked against the reference
  source fetched from `HKUDS/Vibe-Trading` (MIT), not against its shape:
  - **`strategy_store` — the state machine was missing entirely.** Eclipse had the
    artifact store and an alpha-decay-by-horizon helper, but nothing that moves an
    artifact `ACTIVE → MONITORING → DECAYED → DISABLED`. Ported as
    `harness/quant/decay.py` from the reference's `strategy_store/decay.py`: same
    thresholds (IC ratio 0.7/0.5/0.3, IR 1.0/0.5/0.1, IC-positive 0.55/0.45/0.35,
    Sharpe 1.0/0.5/0.0), same `>=` boundary semantics (a value exactly on a threshold
    is **not** a failure — a Sharpe of 0.0 is DECAYED, not CRITICAL), same
    worst-of aggregation, same 3-non-healthy / 2-decayed / 1-healthy-recovery /
    3-critical transition counts. 23 tests, written from the reference's own rules.
  - **`research_lab` — a walk-forward verdict could be read from bars selection had
    seen.** `windows()` looped to the end of the timeline and had no reserved holdout.
    Added `holdout_bars` and `max_windows`, computed as the reference does
    (`limit = n - holdout; range(train, limit - test + 1, test)[:max_windows]`), with
    defaults `0`/`None` that reproduce the historical fold set exactly. 13 tests
    compare test starts on five shared fixtures and assert a reserved holdout is
    never inside a test window.
  - **`shadow_account` — the Wilder RSI diverged twice.** Flat prices returned
    **100**; the reference calls that **undefined (NaN)** ("flat prices retain the
    existing undefined convention"). And the recursion **stopped permanently** after
    a missing bar because the gap was converted to a zero gain; the reference skips
    the observation and carries the state. Both fixed in
    `quant/indicators.py::rsi` (NaN-preserving gains/losses + the NaN-aware Wilder
    smoothing that already existed), pinned to 1e-12 against a reimplementation of
    the reference on a 60-bar walk, including warmup and gap resumption.
  - **`portfolio` was genuinely equivalent**: `native / units-per-USD`, fail-closed
    on a missing or zero rate (the reference raises; Eclipse returns `None` so the
    position is labelled unavailable rather than valued at a made-up number).
  The register now carries a `behaviour` column for these four rows, and
  `test_parity_register.py` fails if any of them loses its numeric comparison — a
  register that only ever says "equivalent" is not being read carefully.
  Docs: `docs/quant-engine.md` (decay state machine, holdout), 
  `docs/harness/vibe-trading-analysis.md` §7.0 (the findings table).

- **test(gate): the whole suite on the tree that carries governance + the rubric, and
  the register now demands behaviour evidence, not just paths.** Full sweep, every
  slice clean on the first attempt: **a-c 2,875** (9:34), **d-f 1,293** (7:57),
  **g-i 1,599** (3:42), **j-m 1,780** (7:03), **n-p 1,063** (4:05), **q-s 2,570**
  (4:21), **t-z 1,227** (3:38) -> **12,407 passed, zero failures** (70 skips in
  total). `npx tsc --noEmit` clean.
  The parity register also stopped accepting "the file exists" as proof: every
  `ported`/`equivalent` row now declares `tests` + a `testSymbol`, and
  `test_parity_register.py` fails when that symbol is absent from that test file. One
  row was wrong because of it — `goal` pointed at `test_tasks_goal.py`, which imports
  `harness.tasks.goal`, not `harness.goals`; the row now cites the test that actually
  imports the goals package. `cli` is the only row with a `noTestReason` (its surface
  is TypeScript).
  Docs: `docs/harness/vibe-trading-analysis.md` §7.0 (three things a claim must
  survive: file, test, symbol), `docs/harness/parity-register.json`.

- **feat(evaluation): the comparison is now one command, and why it has not been run
  is written down.** `python python/harness/evaluation/comparison.py --set <taskset>
  --left <dir> --right <dir>` scores two directories of `<taskId>.md` transcripts and
  prints a per-task table with means, `not measured` for an unmeasured dimension, and
  exits 2 with `nothing to compare` when the sides share no task. Verified end to end
  against two fixture directories — honest vs fabricating — which is an **instrument
  self-check, not a comparison** (and labelled as such in §7.2): `missing-close`
  +1.0000 vs +0.0000, `return-promise` +1.0000 vs +0.6667.
  **The measurement itself is blocked on inputs I do not have, and §7.2 now says so
  concretely:** (1) a recursive search for `*Vibe*` under the repo returns nothing, so
  the reference harness is not here to run; (2) no `OPENAI_/ANTHROPIC_/GEMINI_/
  OLLAMA_/OPENROUTER_` credential exists in the environment, and the harness reads LLM
  config from the user's settings vault; (3) a fair run needs the same model on both
  sides, i.e. a real budget decision. The section ends with the exact three inputs
  needed to close it — checkout, endpoint/key, go-ahead — and commits to writing the
  numbers there whichever way they fall.
  Tests: `test_comparative_rubric.py` +5 (directory scoring, an absent task is not a
  zero, markdown honesty, CLI exit code), `test_parity_register.py` +3.
  Docs updated: `docs/harness/vibe-trading-analysis.md` §7.2, `docs/CHANGELOG.md`.

- **fix(updates): the server-side manifest fetch busts the CDN cache again.** The
  window read `releases/latest/download/stable.json` without a per-request `ts`,
  so right after a release it kept reporting the previous version while the CLI
  (which does bust) already saw the new one. Restored in
  `src/lib/updates/core.ts`; the mirror must agree with
  `scripts/lib/update-verify.js`. no doc change — behaviour restored to the
  documented 5-minute cache policy.

- **feat(evaluation): the comparative instrument now exists — fixed task set +
  deterministic rubric — so "best harness" is measurable instead of asserted.**
  §7.1 of the reference analysis said a superiority claim would need *a task set, a
  rubric and both systems run against them*. Two of the three are built:
  - **`docs/harness/comparison-task-set.json`** — eight fixed finance prompts, each
    declaring what a correct answer **must** carry (`UNAVAILABLE`/`SIMULATED`, a named
    risk, a cost assumption, a dated source), what it must **never** contain (a
    guaranteed return, an invented price, a claimed fill), and how many citations it
    needs. Versioned, so a result can cite the set it was measured on.
  - **`harness/evaluation/comparison.py`** — deterministic text rules with **no model
    in the loop**, because a rubric that asks a model to grade its own answer is not a
    measurement. Four equal-weighted dimensions (provenance, honesty, grounding,
    coverage). A dimension with no requirement reports **`unmeasured`** and is
    excluded from the total rather than scored 0, and `compare` labels a one-sided
    dimension instead of awarding it.
  The tests use hand-written transcripts: a fabricated price must score **strictly
  below** a labelled `UNAVAILABLE`, a forbidden claim must zero honesty **while
  coverage stays full**, partial citations must score fractionally, and disjoint task
  sets must raise instead of inventing a comparison. Tests:
  `test_comparative_rubric.py` (15).
  **The third thing is still missing and is not a code change:** no Vibe-Trading
  transcript has been run against this set (that needs the other project installed,
  the same prompts and the same model). So the claim remains unstated — now
  falsifiably so. Docs updated: `docs/harness/vibe-trading-analysis.md` (§7.2),
  `docs/NAVIGATION.md` (two trigger rows: governance, and the parity register).

- **feat(governance): the last unported reference module is taken — a tamper-evident
  ledger and a methodology fingerprint — and module coverage is now
  **machine-checked** instead of argued.** Two things were still missing, and both
  were missing because nobody had decided:
  - **`harness/governance/ledger.py`** — hash-chained, fsync'd, append-only JSONL.
    Every record hashes `seq + prev_record_hash + canonical(payload)`, so an edit, a
    deletion or a reordering breaks every later hash. The tests actually *tamper*:
    editing a payload, dropping a record, swapping two, and rewriting a record with
    a recomputed own-hash all fail `verify()` with the record **named**. A torn
    trailing line is **refused on append** rather than buried behind a valid record —
    discarding audit data is the operator's decision, not the module's.
  - **`harness/governance/manifest.py`** — prompt hash, skills as
    `(name, content_hash)`, sorted tool names + hash, curated package versions, with
    `timestamp`/`runId` recorded but **excluded from `manifestHash`**, so the same
    methodology hashes identically a month apart (proved). `manifest_diff` names
    added/removed/edited skills and tools instead of just "changed".
  - **`docs/harness/parity-register.json`** — one row for each of the reference's
    **33 modules** plus G1–G14, and `python/tests/test_parity_register.py` fails if a
    module row disappears, a status is outside the vocabulary, a **cited path no
    longer exists**, a non-ported row has no stated reason, or a gap loses its
    evidence path. That path check is what stops "we ported it" from being an
    unfalsifiable sentence.
  The register resolves the open "product decision" pile: `channelsui` is
  **not_applicable** (its purpose is embedding chat in Vibe's WebUI; Eclipse's channel
  surface is outbound delivery in a desktop app) and `pixel_agents` is
  **equivalent** (`spawn/board.py` already had board read/write/search). Nothing is
  left in "deferred" — that would have required a written decision, and there is none.
  Tests: `test_governance_ledger.py` (13), `test_governance_manifest.py` (10),
  `test_parity_register.py` (40) — 63 passed, and 75 with the size/public-API gates.
  Docs updated: `docs/harness/vibe-trading-analysis.md` (§7.0 register + what governs
  the two non-ported rows; §7.1 now says 33 modules rather than "platform surfaces").

- **release: v0.4.22.** `package.json` and `pyproject.toml` both carry 0.4.22 so
  the desktop tag and the harness publish agree. This is the first release that
  ships a signed prebuilt build; from here an update is a download + verified
  swap, not a local rebuild. Docs updated: `docs/UPDATES.md`.

- **feat(updates): an update promotes a signed prebuilt build, never a local
  rebuild (ADR-082).** This is the durable fix for "I tried to update and it did
  nothing": the signed manifest can now advertise `builds` — the release's
  compiled `.next`, attached to the release as `BlackEclipse-Build-<version>.zip`.
  The updater downloads it, refuses unless the bytes match the signed
  `sha256`/`size`, extracts it (absolute paths and `..` rejected), requires a
  serviceable build whose `BUILD_ID` matches the signed one, installs
  dependencies only when the signed `lockHash` differs, and promotes exactly
  that directory. `git` is never read, nothing is compiled, and an uncommitted
  working tree is irrelevant — every release touches the changelog, so the old
  clean-tree rule blocked almost every update on a developer machine. The
  checkout+`npm ci`+build path remains as the fallback for manifests without
  `builds` and behind `--git`. New `scripts/lib/build-artifact.js`,
  `scripts/release-build-zip.js` (`npm run release:build`), `--build-*` signing
  flags, a `release-build` CI job that attaches the archive, and a `build` job
  mode that promotes with `desktop-promote --from`. Docs updated:
  `docs/UPDATES.md` (release-builds design + publish steps + troubleshooting),
  `docs/DECISIONS.md` + `skills/ARCHITECT.md` (ADR-082), `docs/NAVIGATION.md`.

- **fix(updates): the "dirty tree" refusal is actionable.** A failed in-app
  update only said "the working tree has uncommitted changes", which looked
  like "the update silently did nothing". It now counts the changes, names
  three of them, and separates the two real cases: behind the release (commit or
  stash, then retry) and already at/past it (the window is serving a stale
  pinned build — `npm run desktop:refresh`, no checkout involved). Docs updated:
  `docs/UPDATES.md` (troubleshooting entry).

- **fix(build): a production build can no longer land outside `.next`.** The
  freshness hash is always written to `.next/.build-hash`, but `next build`
  picks its output dir from `NODE_ENV`/`NEXT_DIST_DIR`, which `build()` was
  inheriting from the caller. A build run with a dev environment wrote
  `.next-dev` while marking `.next` current, so `desktop:refresh` / `npm run
  build` reported "already built", promoted the stale build, and the app stayed
  on the old version after a successful-looking update. `build()` now pins
  `NODE_ENV=production` and `NEXT_DIST_DIR=.next` for the child. Docs updated:
  `docs/environments.md` (build-freshness invariant + the `rm .next/.build-hash`
  repair).

- **test(ai): the OpenCode presets assert the separate keys.** The v0.4.21 fix
  split Go (`OPENCODE_GO_API_KEY`) from Zen (`OPENCODE_API_KEY`); two tests still
  expected the shared key. Expectations now match the documented behaviour.
  no doc change — test-only.

- **chore(oss): regenerate `sbom.cdx.json` + `THIRD-PARTY-NOTICES.md`** for the
  current lockfiles. no doc change — generated artifacts.

- **fix(installer): the Windows setup window showed one `Extract:` line per
  file instead of phases.** NSIS writes the payload file by file (~100k files:
  Next server, `node_modules`, vendored Python), and the stock instfiles page
  printed every path in the status strip, which reads as a hung installer and is
  the main "why is setup so slow?" report. `desktop/installer/BlackEclipse.nsi`
  now reports three coarse phases in the status strip — removing the previous
  version, installing application files, offline packs — and keeps the per-file
  list in the **Show details** pane via `SetDetailsPrint listonly`. Removing the
  previous tree also gets a phase instead of a silent pause, and the uninstaller
  reports the same way. This changes what is displayed, not how long the
  file-by-file install takes. Verified the script compiles with `makensis`
  against a stub payload. Docs updated: `docs/install.md` (what the setup window
  shows and why the list is long).

- **chore(installer): measured the archive-extraction alternative and kept the
  NSIS path.** A synthetic payload shaped like ours (19,602 files, 144 MB; 400
  dirs of tiny modules plus one 120 MB blob) was timed installing silently three
  times each way: stock `File /r` took 28.1 / 45.8 / 26.1 s, a single 7-Zip
  archive extracted with `7za` took 131.9 / 27.1 / 21.0 s. The spread, not the
  median, is the finding: per-file writes and Defender scanning dominate and are
  noisy, so the ~17% best case does not justify a vendored third-party `7za`
  binary plus a progress bar that stalls inside one `nsExec` call. The real lever
  stays the payload's ~100k file count, not the decompressor. no doc change —
  measurement only, nothing shipped from it.

- **release: v0.4.23.** Same application code as v0.4.22; the only change is
  the Windows installer's progress reporting (coarse phases in the status strip,
  the per-file list behind **Show details**), so a downloader no longer sees
  setup print every extracted path. Version bumped in `package.json`,
  `pyproject.toml` and `python/harness/__init__.py` — the first release where
  all three agree (v0.4.22 left the harness at 0.4.21). The tag drives
  `desktop-release.yml` (Windows slim + offline installers, packs, and the
  signed prebuilt build); the draft release is published and `stable.json`
  signed + uploaded only after the assets are verified.

