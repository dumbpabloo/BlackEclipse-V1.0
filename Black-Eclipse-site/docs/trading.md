---
title: Broker Connectivity
status: Current
last-verified: 2026-11-23
---

# Broker Connectivity

**Who this is for:** anyone connecting a broker to Eclipse, adding a broker
adapter, or wondering why a trading widget says `UNAVAILABLE`.

This is **P4/P9: demo + armed live execution with reconciliation, on every
declared broker kind**. Eclipse can attach to a broker, report its account,
positions, working orders and quotes, submit orders for a **demo** account or an
**armed live** account, and durably reconcile fills, positions and order state.
Nineteen kinds are registered; each executes through the same `TradingAdapter`
contract (MetaTrader 5 over local IPC, the MT4 bridge over its operator-run HTTP
contract, and 17 REST venues), with per-venue capabilities named rather than
assumed. Live is off by default: arming requires a typed phrase, a non-zero
magic, a positive maxMargin and a time box, and every manual live order needs its
own typed phrase. Chat surfaces that live approval as a **one-click card** above
the composer. Live exits (close/modify) are arm-free so risk reduction is never
blocked. Graph Studio can submit demo orders — and **live orders under a
workflow-pinned live mandate on an armed live connection**. The internal
simulator (`trading.paper_order`) remains available.

## The one rule

> The broker is the source of truth. Eclipse never invents a fill, a position, a
> balance or a connection.

A broker that is missing, logged out, disconnected or unsupported returns a named
reason (`PACKAGE_MISSING`, `TERMINAL_NOT_FOUND`, `NOT_LOGGED_IN`, `NO_IPC`, …),
never an empty account that looks real.

## Connection model

A **connection** is a durable row, not a credential bag:

```
BrokerConnection
  id, kind, label, mode (demo | live | unknown)
  config  { terminalPath?, magic?, symbolMap?, bridgeUrl?, baseUrl?, ... }
  status  (unconfigured | connected | unavailable) + statusReason
  account (last account snapshot)
  lastSeenAt, createdAt, updatedAt
```

- Config keys are allowlisted (`model.ALLOWED_CONFIG_KEYS`).
- Anything shaped like a password/token is **refused at write time**
  (`model.validate_config`); it is never stored and redacted later.
- `BrokerConnectionEvent` is an append-only audit of tests/reads/failures.

The preferred MetaTrader path is **attach**: the operator logs into
`terminal64.exe` once, and Eclipse calls `mt5.initialize(path=...)` with no
credentials. Nothing secret enters the app, the database or model context.

## Broker kinds and how each connects

| Kind | Transport | Auth | Eclipse path |
|---|---|---|---|
| **MetaTrader 5** (`mt5`) | Local IPC to `terminal64.exe` | attach to a logged-in terminal | `adapters/mt5.py` |
| MetaTrader 4 (`mt4_bridge`) | Local HTTP JSON bridge | bridge secret name | `adapters/mt4_bridge.py` |
| Alpaca (`alpaca`) | HTTPS REST | `credentialRef` NAME (`KEY:SECRET`) | `adapters/alpaca.py` |
| IBKR (`ibkr`) | Client Portal Gateway HTTPS | `credentialRef` NAME (gateway cookie) | `adapters/ibkr.py` |
| Trading 212, Zerodha, Tradier, OANDA | HTTPS REST | `credentialRef` NAME | `adapters/` |
| Binance, Kraken, Coinbase, Bitstamp, OKX, Bybit, Bitget, Gate.io, Deribit, Bitfinex, Coincheck | HTTPS REST | `credentialRef` NAME | `adapters/` |
| Any plugin broker | plugin host | per-plugin encrypted secrets | `plugins/` |

A new broker is a **registration**, not a refactor. It implements
`BrokerAdapter` (reads) — and `TradingAdapter` if it can place orders — and is
declared in `trade/registry.py`:

```python
class MyBrokerAdapter(TradingAdapter):
    kind = "mybroker"

    @classmethod
    def availability(cls) -> tuple[bool, str | None]:
        return (True, None) if sdk_installed() else (False, "the SDK is not installed")

    def probe(self) -> dict: ...        # honest status, never a fake success
    def account(self) -> dict: ...
    def positions(self) -> list[dict]: ...
    def orders(self) -> list[dict]: ...
    def quote(self, symbol: str) -> dict: ...
    def order_preview(self, intent) -> dict: ...
    def submit_order(self, intent) -> dict: ...
    def cancel_order(self, ticket) -> dict: ...
    def cancel_all(self, magic: int) -> dict: ...

register_kind(BrokerKind(id="mybroker", label="My broker", auth="credentials",
                         execution=True, execution_modes=("demo", "live")),
              MyBrokerAdapter)
```

Then the existing routes (`/trading/connections/{id}/...`), the agent tools and
the UI work unchanged.

## Execution across REST venues

Every declared kind that executes implements `TradingAdapter`, so it goes through
**the same** preview → approval/mandate → guardrails → single send path. The
venue-specific parts are named, never hidden:

- **Account mode is `demo` or `real`.** The service refuses a mismatch. A REST
  adapter derives it from the endpoint (`paper`, `sandbox`, `practice`, `demo`,
  `testnet`, `sim` → `demo`; anything else → `real`). This is the trade layer's
  vocabulary, not MetaTrader's `trade_mode`.
- **Tickets are `str | int`.** MetaTrader returns integers; REST venues return
  UUIDs, exchange order ids or a symbol. The service carries either form and
  never coerces an opaque id (a numeric string with a leading zero keeps its
  zero). See `trade/model.py::ticket_ref`.
- **Ownership is a client order id.** MetaTrader scopes Eclipse orders with its
  `magic`; a REST venue gets `eclipse-<magic>-<digest>` in its client-order field.
  `cancel_all` (and the kill switch) cancel only orders carrying that prefix. A
  venue with no client-order field returns `OWNERSHIP_UNAVAILABLE` instead of
  cancelling every order on the account.
- **Spot vs derivatives.** On spot, `positions()` reports non-zero balances and
  the ticket is the asset; `close_position` submits an opposite market order for
  the held quantity. An attached stop/target is refused with
  `STOP_UNSUPPORTED` when the venue cannot hold one on the entry, never silently
  dropped. Derivatives venues (OKX, Bybit, Bitget, Gate.io futures, Deribit)
  support attached SL/TP and close with a reduce-only opposite order.
- **Named refusals, not optimism.** `BROKER_READ_ONLY` (no execution adapter),
  `OWNERSHIP_UNAVAILABLE`, `STOP_UNSUPPORTED`, `BRACKET_UNSUPPORTED`,
  `POSITION_NOT_FOUND`, `IBKR_CONFIRMATION_REQUIRED`, `BRIDGE_EXECUTION_UNSUPPORTED`.
  An ambiguous send is recorded `unknown`; it is never retried.
- **The money gates are unchanged.** Live still requires an armed connection
  (typed phrase, non-zero magic, positive `maxMargin`, time box), a per-order
  `LIVE …` phrase or a pinned mandate, guardrails, the daily-loss brake and the
  proof/ramp ladder for unattended bots. `terminal.tradeAllowed` is read from
  the account when the venue has no separate "Algo Trading" switch.
- **A broker without a native adapter** can still be reached through the
  `mt4_bridge` JSON contract: the operator runs a small HTTP bridge (it may wrap
  any venue's API) and Eclipse treats it as a normal executing connection. The
  bridge owns credentials and the terminal/API session; Eclipse never stores a
  password.
- **Settings lists every registered kind**, read from `/trading/brokers` rather
  than a hardcoded four. The add form and the per-connection editor share the
  sidebar dialog's form, so both surfaces follow the selected kind's auth style.
  The form shows the credential name, an optional base URL / account id, and (for
  spot venues) the optional `quoteAsset` used when a balance is closed. Live
  arming shows the exact `ARM LIVE <armRef>` phrase the service verifies.



## MetaTrader 5 setup (demo first)

1. Install **MetaTrader 5** (Windows x64) and log the terminal into a **demo**
   account. Leave the terminal running: Eclipse **attaches** to it and never
   starts it on its own (see *Launch policy* below).
2. Enable **Algo Trading** in the terminal toolbar. Reads work without it; the
   probe warns when it is off because execution will need it.
3. Install the Python package in the Eclipse runtime:
   `pip install MetaTrader5` (Windows x64; now pinned in `python/requirements.txt`
   with a Windows-x64 marker, so `npm run setup:python` installs it; the terminal
   remains a separate manual install).
4. Open **Settings › Broker connection** and click **Add & test connection**
   (pick kind `mt5`, mode `demo`, terminal path prefilled). The panel shows the
   account, server, balance/equity, margin mode and warnings. The same panel
   lists every registered kind, so a REST venue is connected from the same form
   by changing **Broker type**. The API equivalent is
   `POST /trading/connections`:
   ```json
   { "kind": "mt5", "label": "MT5 demo", "mode": "demo",
     "config": { "terminalPath": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
                 "autoLaunchTerminal": false } }
   ```
   `terminalPath` is optional; common install paths and `MT5_TERMINAL_PATH` are
   probed. `portable: true` is supported.
5. The panel probes automatically (`POST /trading/connections/{id}/test`) and
   shows exactly what is wrong if it is not connected. **Test** and **Sync** remain
   available on the connection card.
6. Optionally map broker-specific symbol suffixes:
   `"symbolMap": { "EURUSD": "EURUSD.a" }`.

### Launch policy (attach-only by default)

`mt5.initialize(path=...)` does not merely connect: if `terminal64.exe` is closed,
the MetaTrader5 package **starts it**. Because the dashboard trading widgets and
the journal auto-sync read the broker in the background (every 15 s / 300 s), an
unguarded call reopened MT5 every time the operator closed it.

So the adapter checks first. If no `terminal64.exe` (or `terminal.exe`) process is
running and the connection has not opted in, it refuses **before** `initialize`
with `TERMINAL_NOT_RUNNING` and a message telling the operator to start and log
in. It never launches the terminal as a side effect of a read or a probe.

- `config.autoLaunchTerminal: true` restores launching for that connection
  (Settings › Broker connection › **Start MetaTrader 5 automatically when it is
  closed**, off by default).
- The one intentional launch left is the post-order reveal: focusing the running
  terminal, or starting `terminalPath` when an order was just filled. Disable it
  with `config.openTerminalOnTrade: false`.
- `BE_MT5_PATH` still overrides the default `C:\Program Files\MetaTrader 5\terminal64.exe`.
- If the process list cannot be read, the adapter lets `initialize` decide rather
  than falsely refusing a terminal that is actually running.

### What the MT5 adapter reads

`account_info()` (login, company/server, balance/equity/margin, leverage,
demo/contest/real, netting/hedging), `positions_get()`, `orders_get()`,
`symbol_info()` + `symbol_info_tick()` (bid/ask, spread in points, digits, volume
min/max/step, stops level). It never calls `order_send`, never selects symbols,
and never stores a login password.

A symbol that exists but has no live tick (for example `XAUUSD` before it is in
Market Watch) is subscribed with `symbol_select` and retried for up to ~2 s. That
is a local terminal subscription, not an order or account mutation. A tick that
stays all-zero is reported as `SYMBOL_NOT_FOUND`, never as a price.

### The MT4 bridge contract (reads + writes)

MT4 has no supported local Python IPC, so `mt4_bridge` attaches to a small HTTP
bridge the operator runs next to the terminal. The bridge owns the login and the
terminal; Eclipse speaks JSON:

```
GET  {bridgeUrl}/account
GET  {bridgeUrl}/positions
GET  {bridgeUrl}/orders
GET  {bridgeUrl}/orders/{ticket}
GET  {bridgeUrl}/quote?symbol=EURUSD
GET  {bridgeUrl}/symbol?symbol=EURUSD        (optional symbol spec)
GET  {bridgeUrl}/deals?from=<epoch_ms>&to=<epoch_ms>

POST {bridgeUrl}/orders
     {symbol, side, volume, type, price, stopLoss, takeProfit, magic,
      comment, deviation}
     -> {ok, ticket, price, retcode, retcodeName, reasons, message}
POST {bridgeUrl}/orders/{ticket}/cancel
POST {bridgeUrl}/orders/{ticket}/modify      {price?, stopLoss?, takeProfit?}
GET  {bridgeUrl}/orders/{ticket}/status
POST {bridgeUrl}/positions/{ticket}/close    {volume?}   ({} = full close)
POST {bridgeUrl}/positions/{ticket}/modify   {stopLoss?, takeProfit?}
```

Responses are JSON; lists may be a bare array or wrapped as `positions` /
`orders` / `deals`. `bridgeSecretRef` is a **name** (an environment variable, or
a `secret://…` vault reference), never a value; when set it is sent as
`Authorization: Bearer …`. A bridge that answers 404/405 on a write endpoint is
reported as `BRIDGE_EXECUTION_UNSUPPORTED` — the read path keeps working, and
nothing pretends the order was sent. Cancelling all orders matches the
connection's magic, so the kill switch never touches a manually opened order.
Tests: `python/tests/test_mt4_bridge_adapter.py`,
`python/tests/test_mt4_bridge_execution.py`.

### Alpaca and IBKR (execution, FIFO-normalized)

Neither API exposes an MT5-style `position_id` on its execution stream. Both
adapters therefore feed filled orders/executions through the shared
`adapters/fifo.py` normalizer, which produces round-trip legs with FIFO P&L,
pro-rata fees, partial fills and shorts. That P&L is **Eclipse-computed**, so
the journal records `pnlBasis="computed"` and the trade detail says so.

- **Alpaca**: `baseUrl` (default paper), `dataUrl`, optional `accountId`,
  `credentialRef` → `KEY:SECRET` or JSON `{"key","secret"}`. Market/limit/stop,
  bracket/OTO, cancel, replace and close-by-symbol. A paper host is the demo
  account.
- **IBKR**: `baseUrl` (default `https://localhost:5000/v1/api`), `accountId`,
  `credentialRef` → gateway cookie (`SESSION=…` or JSON `{"cookie":…}`). TLS
  verification is relaxed only for a loopback gateway. The gateway's order
  confirmation question is answered **once**; a second question refuses with
  `IBKR_CONFIRMATION_REQUIRED` rather than looping. Bracket legs and
  `cancel_all` are named refusals because CPAPI cannot link either to an order
  tag.

A `credentialRef` is a NAME (env var or `secret://…`), never a value.

### Smoke test

`python scripts/broker_smoke.py [--connection ID] [--days N]` calls
`POST /trading/connections/{id}/smoke`: probe → account → positions → orders →
deals window → one reconciliation sync, then prints PASS/FAIL per check and
exits non-zero on any failure. The same action is a **Smoke** button in the
broker dialog. It never places, cancels or modifies an order.

## Demo execution (P1)

```
intent ──▶ order_preview (normalize volume, stops, filling, order_check)
        ──▶ TradePolicy (margin / volume / spread / open positions / allowlist)
        ──▶ money guardrails (per-transaction, daily, velocity, crisis)
        ──▶ single-use approval (kind="trade_order", digest over the exact order)
        ──▶ order_send (once; never retried on an ambiguous result)
        ──▶ BrokerOrder ledger + reconciliation by the operator
```

**The connection mode and the account must agree.** A demo connection only acts on
a broker account whose `account_info().trade_mode` reports `demo`; a live
connection only acts on a `real` account. The account mode is checked at preview,
again at submit and again for a close/modify, so a mislabelled connection cannot
touch the wrong book (a named `*_ACCOUNT_REQUIRED` / `LIVE_ACCOUNT_REFUSED` refusal).

**The approval is the payload.** `OrderIntent.digest` covers connection, symbol,
side, type, volume, price, SL, TP, deviation, time-in-force, magic and comment.
Submitting a different order against the same approval fails with
`APPROVAL_ORDER_MISMATCH`. The approval is consumed atomically before the send,
so a second submit needs a fresh decision.

**Typed confirmation and cooling-off are enforced.** When the money guardrails
set `requiresTypedConfirmation` (for example a high-value order), the preview
returns the exact `confirmPhrase` and the submit body must repeat it; a missing
phrase returns `CONFIRMATION_REQUIRED` and does **not** consume the approval. If
the verdict carries a cooling-off, an early submit returns `COOLING_OFF` with the
remaining seconds. The operator can retry the same approval after both conditions
are satisfied, within its five-minute TTL.

Endpoints (operator-gated):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/trading/connections/{id}/orders/preview` | validate + `order_check` + request approval; sends nothing |
| `POST` | `/trading/connections/{id}/orders/submit` | consume the approval and `order_send` once |
| `POST` | `/trading/connections/{id}/orders/{ticket}/cancel` | cancel a working order (risk-reducing; no approval) |
| `POST` | `/trading/connections/{id}/orders/{ticket}/modify` | re-price / re-bracket a working order (risk-reducing; no approval) |
| `GET` | `/trading/connections/{id}/symbols/{symbol}` | static symbol facts (trade mode, contract, volume limits, sessions); no tick needed |
| `POST` | `/trading/connections/{id}/positions/{ticket}/close` | close a position, fully or with `volume` (risk-reducing; no approval, no arm) |
| `POST` | `/trading/connections/{id}/positions/{ticket}/modify` | move SL/TP (risk-reducing; no approval, no arm) |
| `POST` | `/trading/connections/{id}/positions/close` | close every open position (or `tickets`) |
| `GET` | `/trading/connections/{id}/orders/ledger` | local submitted-order ledger |
| `GET` | `/trading/orders/{orderId}` | one ledger row |
| `GET` | `/trading/approvals` | `trade_order` approvals |
| `POST` | `/trading/approvals/{id}/decide` | approve or deny (operator only) |

Agent tools add `order_preview` (creates an approval, never sends), `order_ledger`
and `order_submit`. There are two operator-gated execution paths and they do not
compete:

1. **Manual approval.** The agent calls `order_preview`; the operator clicks the
   card, which calls `/trading/connections/{id}/orders/submit` with the
   `approvalId`. That card **does fill the order** (demo without a phrase, live
   after the typed phrase) and needs **no mandate**. The agent never sends this
   order itself. The approval row records the run that requested it
   (`requestedByRunId`), so an unattended bot's delivery can report its own
   pending order and the operator approves it in that bot's conversation. The
   window is 300 seconds; a scheduled bot should run near the operator's open,
   not hours before it.
2. **Unattended demo mandate.** With an active operator-created demo mandate, the
   agent calls `order_submit` once with `intent`; the mandate is the authorization
   (see the mandate section). This is also the Graph Studio path.

**`order_submit` is demo-only, enforced by mode.** The agent tool resolves the
connection's `mode` and refuses anything that is not `demo` (`LIVE_ORDER_REFUSED`;
the authorizer refuses it too, and `TradingMandateStore.covers` now matches the
mandate's mode against the connection's). A live trade therefore has exactly two
homes: the approval card in path 1, or a workflow-pinned live mandate executed by
a Graph Studio `trading.*` node. The mandate store already refused an unpinned
live mandate (`MANDATE_WORKFLOW_REQUIRED`, workflow id **and** version), so
unattended live authority always names the workflow it authorizes; the agent path
now refuses live explicitly rather than relying on the workflow-pin check to fail
for the wrong reason. An unknown or missing connection mode is refused rather
than assumed to be demo.

Because both paths return through `order_submit` in the ledger, an earlier
`MANDATE_REQUIRED` refusal is not evidence that the order failed: after the
operator confirms a card, `order_ledger` shows the filled ticket. The agent's
guidance (system `<fiduciary_advice>` and the per-run trading directive) says so
explicitly and requires a ledger read before reporting a failure.

**Never retry an ambiguous send.** A timeout, a lost IPC reply or any exception
after `order_send` marks the ledger `unknown` and tells the operator to check the
terminal. Eclipse does not resend, because MetaTrader has no client order id and a
blind retry can double the position.

**A ledger row is traceable.** Beyond the digest, status and fills, `BrokerOrder`
records `runId` and `nodeId` when a Graph Studio node submitted the order (the
paper account's fill log already did). A bot-submitted order additionally records
`automationId` and `automationVersion` — the bot and the version of its objective
at submit time — so a version bump is visible on the orders it produced, and
`order_ledger` can be read for one bot. An operator can therefore go from a
position back to the workflow run, or to the bot run, that opened it; the
advisory decision behind it is a join on `runId`.

**What `order_check` proves.** Volume step/min/max, stops distance, symbol trade
mode, filling mode, pending-price side, and the margin impact. `order_check` is
not a promise the fill will happen; it is the broker's pre-trade verdict.

**A preview refusal is an outcome, not a server error.** When the account mode or
terminal switch blocks a preview, the API returns `allowed: false` with
`error.code` (`ALGO_TRADING_DISABLED`, `DEMO_ACCOUNT_REQUIRED`, `LIVE_NOT_ARMED`,
`LIVE_ARM_EXPIRED`) and `reasons`, not a 500. The operator/agent can read the exact
gate. MT5 IPC sessions are serialized in-process, so concurrent broker tool calls
cannot collide in `initialize`.

## Live execution (P2)

Arming is a separate, operator-only action:

```
POST /trading/connections/{id}/arm
{ "confirmation": "ARM LIVE 12345678", "ttlSeconds": 1800 }
```

Before the arm is stored, Eclipse reads the broker account and requires:

1. the connection mode is `live`;
2. `account_info().trade_mode` is **real**;
3. Algo Trading is enabled in the terminal;
4. a **non-zero magic** is set, so the kill switch only touches Eclipse orders;
5. a **positive `maxMargin`** is set, a mandatory live cap.

The arm is time-boxed (60 s to 8 h) and is cleared by a mode/config change, a failed
probe, expiry, or the kill switch. Disarm explicitly with
`POST /trading/connections/{id}/disarm`.

**Every live order is a second decision.** The preview returns
`confirmPhrase: "LIVE BUY 0.1 EURUSD"` and always sets
`requiresTypedConfirmation`. Submit must repeat that exact phrase; a missing or
wrong phrase returns `CONFIRMATION_REQUIRED` without consuming the approval. The
single-use approval still binds the exact order digest, and the money guardrails
still apply (caps, crisis mode, cooling-off).

**Kill switch.** `POST /trading/connections/{id}/kill` cancels every working order
whose magic matches the connection, then disarms. It refuses when the magic is 0.
It does **not** close open positions; use `positions/close` (or the Open Positions
widget) for that, which is arm-free because it only reduces exposure.

**Live exits are always available.** Closing or modifying a live position needs no
arm, no approval and no mandate: an expired arm must never trap the operator in a
live position. The only gate is mode agreement — a live connection can only act on
a real account, and a demo connection on a demo account. Every close/modify is read
back from the broker and returns `verified`.

### Unattended live execution: the bot ladder (P5)

A bot may replace the per-order card with its own authority, in tiers:

| Tier | May do |
|---|---|
| `l0` (default) | `order_preview` only; the operator approves each live order |
| `l1` | `order_submit` on a **demo** mandate |
| `l2` | `order_submit` on a **live** mandate pinned to this bot |

`l2` is not a switch. Raising the tier is a single-use operator approval, and `l2`
additionally requires ≥30 scored advisory calls for that bot. At submit time
`trade/authority.py` requires an **armed** live connection, a mandate whose
`botId` **and** `botVersion` match the bot's current definition (an objective edit
invalidates the grant), a stop loss when `requireStop` is on, an Eclipse-owned
position count under the cap, a cooldown since the bot's last order, and the
mandate margin at or below the bot's ceiling. The daily-loss brake, money
guardrails, cooling-off and the magic-scoped kill switch still run. Every refusal
carries a code (`BOT_TIER_INSUFFICIENT`, `BOT_VERSION_STALE`, `STOP_REQUIRED`,
`POSITION_CAP_REACHED`, `COOLDOWN_ACTIVE`, `POSITIONS_UNAVAILABLE`, …) so "why
didn't it trade?" is answerable.

A live mandate is created through the existing trading surface with `botId` +
`botVersion` (or a workflow pin, for Graph Studio). Exactly one pin is required.

### Standing consent: auto mode (P7)

A bot that trades on a schedule must not ask the operator for a phrase on every
mandate. `action.config.consent` is a **standing grant** the operator gives once:

```jsonc
{
  "version": 1, "kind": "standing",
  "modes": ["live"],
  "grantedAt": "…", "expiresAt": "…", "revokedAt": null,
  "autoRenew": true,
  "limits": { "symbols": ["EURUSD"], "maxOrdersPerDay": 4,
              "maxMarginPerOrder": 500, "maxOpenPositions": 1 },
  "phrase": "LIVE TRIAL ema_trend 1.2.0"
}
```

Rules:

- The phrase is typed **once** at prepare. The server stores the grant on the bot
  (not in a client token), so the runner verifies it without trusting the caller.
- `prepare` accepts an EMPTY phrase when a stored grant still **covers** the new
  request (mode, symbols, orders/day, margin, positions). That is the auto mode:
  re-preparing after an edit or a lesson does not nag.
- A grant that does not cover the request, has expired, or was revoked needs the
  phrase again. Widening any limit is a new consent, by construction.
- `autoRenew` only extends the grant; it never widens it. A revoked grant can
  never be renewed.
- Consent is **not** the arm. A live order still needs an armed connection and a
  bot-pinned mandate under 24 h, and the L2 fence still runs at submit.
- Pausing/archiving/deleting the bot revokes its mandates immediately. The
  consent stays on the definition for audit but covers nothing while paused.

Routes: `POST /automations/{id}/trading/prepare`, `GET /automations/{id}/trading`,
`POST /automations/{id}/trading/scan`, `POST /automations/{id}/trading/panic`
(front door: `/api/automations/{id}/trading…`). `prepare` is the only route that
creates market authority; none of them place an order.

### Visual teaching (P8)

Up to four bounded images (PNG/JPEG/WebP, ≤2 MB each) can be attached to a bot.
They are sent to the model as **real image parts** on a chat turn and on a
scheduled agent run (`RunInput.images` → `image_url` content blocks), together
with a text block that marks them as pattern guidance: never live market data,
never a price quote, and the broker/JDdS data wins on conflict. A text-only model
still receives the captions and the rule.

### Find trades

`POST /automations/{id}/trading/scan` evaluates the strategy across the bot's
symbols on the last closed bar and returns each symbol's signal plus the
actionable ones. It is the same evaluation the runner acts on, so a candidate is
never invented. The trading panel's **Find trades** button adopts the actionable
symbols as the universe and re-prepares under the standing consent.


### Strategy bots

`action.type = "run_strategy"` is a bot whose brain is a saved strategy version.
The runner (`python/harness/automation/trading_runner.py`) checks consent, the
session window and data freshness, scans the universe, then calls the SAME
`LiveStrategyService.run_once` path. Every run records a `DecisionRecord` —
including no-signal and every refusal — on `AutomationExecution.decision`.

Tier gates: L0 proposes, L1 fills demo, L2 fills live. `check_live_order` is
called INSIDE `submit_order` whenever `automation_id` is present, so no caller
(tool, graph node, strategy runner) can skip the fence. The broker seam is
covered directly: an L1 bot is refused before the adapter is touched, an L2 bot
with a bot-pinned mandate sends exactly once, and a stale bot version is refused
`BOT_VERSION_STALE`.


### Sizing, plans and pre-trade risk

A trade idea is **sized before it is previewed**. Two tools do that, and neither sends
anything:

- `broker_symbol_info` reads the broker's own facts for one instrument - contract size,
  volume minimum/step/maximum, tick size and **tick value in the account currency**,
  stops level, trade mode and sessions. A missing fact is `UNAVAILABLE`, never assumed.
- `trade_plan` turns `{symbol, side, stop, riskPct?, entry?, target?}` into a sized plan:
  the entry is the operator's price or the broker's ask/bid; the volume is the largest
  broker-legal size whose stop-out stays inside `riskPct` of equity (default **1%**) and
  is always rounded **down**.

A plan that cannot be sized refuses with the correction, not a guess:
`RISK_TOO_SMALL` names the smallest legal trade and what it would risk; `STOP_TOO_TIGHT`
names the broker's minimum entry-to-stop distance.

**Account fit is assessed in the same step** (`harness/trade/risk.py`): open position
count, per-symbol concentration, margin after the order vs equity, the broker's margin
level floor, and the spread's share of the planned risk. A blocking assessment refuses
the plan **before an approval card exists**. The thresholds live on the connection
(`config.risk`, edited under Settings -> Trading -> Risk limits -> Account fit):
`maxRiskPct`, `maxOpenPositions`, `maxSymbolPositions`, `maxMarginPct`,
`minMarginLevelPct`, `maxSpreadPctOfRisk`.

`order_preview` attaches the same class of summary to every preview (`riskSummary`:
max loss, % of equity, reward:risk, spread cost, warnings), computed from the numbers the
broker already returned - no second round-trip and no estimate. The chat approval card
renders it, so the operator approves a **sized risk**, not only a symbol and a volume. A
blocking summary refuses the preview itself.

### The portfolio supervisor (portfolio stops + bot stand-down)

`harness/trade/supervisor.py` is the account-level layer above the per-order checks.
It answers two questions and nothing else:

1. **May the book take new risk?** Total margin vs equity, total open positions, and the
   equity loss since the **day/week open**. The open equity is snapshotted once per period
   (`BrokerRiskSnapshot`, first write wins), so a restart cannot move the baseline to hide a
   loss. A breach is `block`: no new entries, and the numbers are the broker's own.
2. **Has a bot lost the right to keep trading?** A bot with at least `botMinTrades` (30)
   closed fills whose realized P&L after commission and swap is not positive, or with
   `botMaxConsecutiveLosses` (8) losses in a row, is **paused** through the automation
   service. Pausing stops future triggers; it never closes an open position.

Unreadable data is `unmeasured`, never "healthy". Limits live in the connection's
`config.supervisor` (`maxPortfolioMarginPct`, `maxPortfolioPositions`, `dailyLossPct`,
`weeklyLossPct`, `warnAtPct`, `botMinTrades`, `botMaxConsecutiveLosses`,
`botMinRealizedPnl`).

Surfaces: `risk_supervisor` (read-only report, safe for any turn) and
`risk_supervisor_apply` (write/high, pauses underperformers on request). The periodic
sweep is opt-in with `TRADING_SUPERVISOR_SECONDS` (minimum 30, default **0** = off) and
runs from `boot.py` beside the reconciler. It places no order.

### The live proof gate (evidence before exposure)

A tier is the **permission**; proof is the **evidence**. Creating a **live** mandate now
also requires `harness/trade/proof.py` to pass, with both halves present:

- **Out-of-sample** - the strategy was scored on data it was not fitted on, with costs
  included, at least `minOosTrades` (30) trades, positive expectancy and drawdown inside
  `maxDrawdownPct` (20%). The record is stored by the backtest flow as
  `config.proof.outOfSample`; this module never manufactures one.
- **Demo** - real fills from the broker's own deal history on **demo** connections only:
  at least `minDemoTrades` (30), spanning at least `minDemoDays` (14), with positive
  expectancy after commission and swap.

Missing or unreadable evidence is a refusal (`PROOF_INSUFFICIENT`, HTTP 409) that names
the first gaps and includes the full report; it is never a benefit of the doubt. A **demo**
mandate is not proof-gated - that is where the evidence comes from. Thresholds are
overridable per bot (`config.proof.requirements`), but malformed values fall back to the
defaults rather than widening the gate.

### The live ramp (small money before more)

Proof earns live access; `harness/trade/ramp.py` decides how **much**. A live mandate's
margin and order caps are clamped to the stage the bot's **real live fills** have earned:

| stage | earned by | margin cap |
|---|---|---|
| `probe` | no live history (or a losing one) | 25% of the bot's ceiling |
| `build` | >= 20 live fills, positive expectancy | 50% |
| `scale` | >= 50 live fills, positive expectancy | 100% |

A losing expectancy or a drawdown above the stage cap steps the bot **down one stage**;
nothing steps it up except live results. The ramp can only shrink the bot's own
`max_margin_per_order` / `max_orders` - it never raises them. A live mandate above the
current stage is refused (`RAMP_CAP_EXCEEDED`, 409) with the stage, the cap and what the
next stage needs.

### Chat: one-click order approval

`order_preview` creates the single-use approval; the chat then renders it above the
composer (`src/components/chat/trade-approval-banner.tsx`). The banner shows the
exact order, the risk and a **Confirm & send live** button. On a live connection the
operator must type the phrase the server computed (`LIVE BUY 0.1 EURUSD`) before the
button enables; the banner then approves the exact digest and calls the same
`/trading/connections/{id}/orders/submit` route as Quick Trade. Eclipse never types
the phrase on the operator's behalf, and the agent does not call `order_submit` for
a live order. A demo order follows the same card (the phrase appears only when the
money guardrails require one).

**The terminal comes forward after a fill.** When an order is `filled` or `placed`,
the harness focuses the running `terminal64.exe` (or launches the connection's
`terminalPath` if it is not running) so the operator sees the new position
immediately. This is Windows-only and best-effort: a failed window activation is
reported nowhere and never changes the order result, and on a container/remote
bridge it is a no-op. Disable it per connection with
`config.openTerminalOnTrade: false`, or set `BE_MT5_PATH` to override the default
`C:\Program Files\MetaTrader 5\terminal64.exe`.

## Unattended graph orders (P3 demo, P6 live)

A Graph Studio node cannot show an approval card. An operator-created **mandate**
is the explicit substitute for ONE connection. It is bounded by symbols, a margin
cap, an order count and an expiry. A **demo** mandate is enough for paper nodes.
A **live** mandate additionally requires an armed live connection, a
workflow-pinned version and a maximum 24 h TTL — the two-key model:

```
POST /trading/connections/{id}/mandates
{
  "confirmation": "MANDATE DEMO My demo",
  "symbols": ["EURUSD", "GBPUSD"],
  "maxOrders": 20,
  "maxMargin": 500,
  "ttlSeconds": 86400
}
```

A mandate names the allowed symbols, a margin cap per order, a maximum number of
orders and an expiry (60 s to 7 days for demo; **24 h maximum for live**). For
live it also pins `workflowId` + `workflowVersion`, so editing the graph cannot
reuse an old mandate. Every use is counted atomically, so a looping graph cannot
exceed `maxOrders`. Revocation is immediate
(`POST /trading/mandates/{id}/revoke`).

The Graph Studio node `trading.broker_order` takes an order intent plus a
`connectionId` and `mandateId`. In **paper** mode it uses a demo mandate. In
**live** mode it requires the operator to confirm live trading; the compiler then
requires `allowLiveTrading`, an armed live connection and a pinned live mandate.
The node calls the SAME `BrokerService.submit_order` path, so the connection
policy, money guardrails, account-mode check, daily-loss brake and ambiguity
rules all still apply. The mandate is consumed before the send and the ledger
records `mandateId` and the workflow pin.

**Two keys for live.** A live order can only be sent when BOTH are true:
the connection is armed (typed `ARM LIVE <login>`, time-boxed) and an active live
mandate pins the workflow. An expired arm or a revoked/expired mandate refuses
the order. The mandate never arms the account and the arm never authorizes an
order by itself; the operator creates both, and the agent can do neither.

**Reads and risk-reducing exits.** `trading.broker_account` reads the account
envelope, `trading.broker_positions` returns the open positions as a structured
list plus a `count`, and `broker_quote` / `broker_bars` read the broker's own
quotes and OHLCV (including symbols JDDS may not carry). Every read and every
exit returns `ok` plus a `reason`, so a graph branches on the outcome instead of
comparing status strings; `broker_bars` also returns the same deterministic
ATR(14), 20-bar range and contract size the chat tool computes, so a graph does
not recompute them. `trading.broker_close` closes one ticket, a whole symbol or
everything; `broker_modify` and `broker_cancel` move a bracket or cancel a
working order. All four are risk-reducing: they need no arm, approval or mandate,
because an expired arm must never trap a position. They still require a pinned
`connectionId`. `trading.broker_modify_order` changes a **working** (pending)
order's price and/or bracket; it carries the order's own type, time and
expiration across the modify, so a day order cannot silently become a resting GTC
one, and it reads the order back (`verified`) before reporting success.
`trading.broker_symbol` returns the static symbol facts — trade mode, contract
size, volume min/max/step, stops and freeze levels, filling mask — with **no tick
required**, so it answers while the market is closed. It also reads the broker's
**trade sessions** best-effort into `sessionDeals` (day 0 = Monday, `HH:MM`,
server time): MetaTrader's Python API does not reliably expose them (the
functions are missing or return nothing in several builds, reported to
MetaQuotes), so `sessionsAvailable` is explicit and an empty table is reported
with its reason rather than an invented calendar.

**Dry run before arming.** `trading.order_preview` runs the same preview the
manual path uses — broker `order_check`, connection policy, money guardrails and,
when a `mandateId` is set, the mandate's scope (verifying does not consume a use)
— and creates **no approval**. Its `ok` means the check ran and `allowed` means the
order would pass. With `dryRun = true`, `trading.broker_order` takes that path
instead of submitting, so a whole workflow can be validated before a single order
is sent.

**Building and reading the graph's own records.** `trading.order_intent` builds
the validated intent both execution adapters read (`quantity` and `volume`,
market/limit/stop, SL/TP, time-in-force); a limit or stop order without a price is
an error, never a market order in disguise. `trading.broker_positions`,
`trading.broker_orders` and `trading.broker_fills` expose the broker's positions,
working orders and reconciled deals as rows, and `trading.order_ledger` exposes
Eclipse's own submitted-order rows (including the `runId`/`nodeId`). The row types
match: `broker_positions` plugs straight into the risk gate's `positions` input,
while an account envelope does not and is refused at compile time.
`trading.paper_account` reads the durable PAPER book — cash, positions, average
cost and realized PnL — and emits its positions as rows too; `exists` is false
until the first fill.

**Trading-system primitives.** Four pure nodes turn an intent into a sized,
bracketed, guarded order and never call a broker: `trading.size_by_risk` (volume
from an equity risk budget and a stop distance, floored to the symbol's volume
step and capped at its maximum — contract size / step / min / max come from a
wired `broker_quote`), `trading.bracket` (direction-aware SL/TP from ATR ×
multiple or an explicit stop distance, with a reward:risk multiple),
`trading.spread_guard` (allowed only while the broker's spread is within the cap;
a quote with no spread is not a tight spread) and `trading.session_filter` (the
broker's own trade mode plus a calendar in this order: the broker's
`sessionDeals` evaluated in server time — the quote's server clock gives the
offset, or an explicit `serverOffsetMinutes` — then an explicit `sessionsJson`
calendar for terminals that report none, then the configured UTC window and
weekdays; `basis` says which was used). No holiday schedule is invented.

Three position-management nodes are risk-reducing and therefore need no arm,
approval or mandate, exactly like close/modify: `trading.break_even` measures the
position's own R from its entry and stop and moves the stop to entry after
`triggerR` of profit; `trading.trail_stop` keeps the stop a fixed distance behind
price and only ever tightens it (a worse candidate is refused, never applied);
`trading.partial_close` closes a fraction (or an explicit volume), flooring to
the symbol step — from a wired `broker_quote`, or from the broker's own
`symbol_info` when no quote is wired — and refusing a volume below the symbol
minimum. Each reports `moved`/`closed` plus `ok` and the broker's own refusal
reason.

**Lifecycle and reliability.** `trading.await_fill` polls Eclipse's own ledger
until an order is filled, rejected, cancelled, expired or unknown, or the
deadline passes; `timedOut` means it is still working (an ordinary outcome for a
limit order), and the node never sends or cancels anything. `trading.reconcile`
runs the same sync path as the route, copying the broker's deals, positions and
sync state into the mirror — it never changes broker state, and a failed sync
keeps the last good mirror. `trading.drift_check` compares the ledger with that
mirror and reports three things: in-flight ledger orders that match no position,
working order or deal; broker positions the ledger never recorded; and how old
the mirror is (`maxAgeSeconds`, 0 disables). `syncFirst` refreshes the mirror
through the normal sync path before comparing — that is the only repair offered,
and it keeps the broker authoritative. Remaining drift is reported, never
rewritten. **A test run never writes the mirror**: `trading.reconcile` and a
`syncFirst` drift check are refused when the run is a test run, because a test
run performs no writes.

**One-command demo verification.** `python/scripts/trade_demo_smoke.py` probes the
terminal, reads the account/positions/orders, checks the static symbol facts (and
reports whether the terminal exposes session windows), and — with
`--modify-order TICKET --price …` — re-prices one working demo order and asserts
the broker read back what was asked. Without that flag it is read-only, and it
refuses to run against a non-demo connection.

**An unattended graph cannot double-send.** `trading.broker_order` and
`trading.paper_order` take `dedupe: none | key | day`. `key` uses the node's
`idempotencyKey`; `day` uses the node id plus the UTC date. The claim is written
to `GraphTradingGuard` on `agent-memory.db` **before** the send, so a scheduled
run that fires twice sends once. When the service refuses **before** a send
(`REFUSED`, `CONFIRMATION_REQUIRED`, `COOLING_OFF`) the claim is **given back**,
so a policy refusal does not burn the day; `REJECTED` and `UNKNOWN` keep it,
because either may have reached the broker and a blind retry can double the
position. Guard rows older than 90 days are pruned when the store is wired.

**PAPER mark-to-market.** `trading.paper_account` reports cash and cost basis;
`trading.paper_valuation` marks the same book to prices the graph supplies (a
symbol→price map, or one symbol + price) and returns equity, market value,
unrealized and total PnL. It never fetches a price: a missing symbol is listed in
`unpriced` and valued at nothing rather than at zero.

**Agent-driven chat execution.** Settings → Broker connection can create and
revoke mandates. On a **live** connection, ask Eclipse to analyze a market and place
one order: the agent calls `order_preview` and the approval appears above the chat
composer. You type the live phrase and click **Confirm & send live**; the banner
approves the exact digest and submits through the same route as Quick Trade. The
agent never types the phrase and never calls `order_submit` for live. On a **demo**
connection with an active mandate the agent may still call `order_submit` once (the
unattended demo path); the result appears in the position book and ledger. The agent
cannot create the mandate, arm live, cancel, or exceed the mandate’s symbols / margin
cap / order count / expiry. A verified demo run placed ticket **11164809214**
(EURUSD BUY 0.01, SL 1.11125, TP 1.12260) under mandate `mnd_c82ba003d785`.

**A plain prompt is enough.** The user never has to name a mandate or a
connection. `order_submit` resolves the only saved connection, then the one
active mandate whose `symbols` cover the intent symbol (or the sole active
mandate regardless of symbol). Only when the symbol matches several mandates, or
none, does it return `MANDATE_REQUIRED` with the active ids. Ambiguity is refused,
never guessed. A `connectionId` that no longer exists (a remembered id from
another profile or database) is dropped and resolved normally; otherwise it would
make an account with a valid mandate report "no active mandate".

**The trade turn is not a research turn.** `graph.py` appends a per-run
`# Trading request` block when the query asks to place/buy/sell an order. It tells
the model to skip web/news/scrape work, read the quote (plus at most one bar source)
and call `order_preview` once with a small size, then tell the operator the approval
card is waiting for their phrase. A live turn never submits from the agent. A
refusal (“do not place a trade”) or a question (“should I buy…”) never gets the
block. The historical demo runs above were produced by the earlier
`order_submit`-first directive; the tool remains available for demo mandates.

**The trade turn gets the trade surface.** A run whose prompt is a trade, close
or modify request binds only the broker/trade tools plus the response helpers
(`TRADE_SURFACE_TOOLS`, ~18 names) instead of the agent’s ~700. The narrowing is
applied after policy resolution and can only remove: a run that cannot
submit/close/modify is left untouched. In the historical demo run on a refused gold
order the narrowing held: `allowedTools` fell from 721 to **18** and prompt tokens
for the 3-step run were **47k** instead of ~228k.

**The mandate cap sees this order's margin, not the account total.**
`order_check().margin` is the account margin *after* the order, so with other
positions open a 0.01 XAUUSD lot reported 113.74 against a 100 cap while adding
only 40.00. The adapter now charges `check.margin − account.margin` (floored at
zero), and exposes `accountMarginAfter` for diagnosis.

**Risk limits (connection config).** Beyond the broker's own checks, the
connection can carry `riskPerTradePct`, `maxDailyLossPct`, `maxSymbolNotional`,
`maxOpenPositions` and `maxSpreadPoints` (all optional; 0 = off). The first three
are the risk engine:

- **`riskPerTradePct`** — equity risk at the stop is `|entry − stop| × volume ×
  contractSize`; an order above the cap is refused, and with this cap set an order
  **without a stop is refused** (“undefined risk is not small”).
- **`maxDailyLossPct`** — a rolling 24 h brake over realised deals + open P&L.
  New entries are blocked; closing is always allowed.
- **`maxSymbolNotional`** — a per-order notional cap for one symbol.

Settings → Broker connection exposes these as a **Risk limits** editor. Verified
live: with `riskPerTradePct=0.02` a 0.01 XAUUSD order with a 100-point stop was
refused (`risk 121.76 > 20.00 cap`); at `0.5` and a 120-point spread cap the same
shape previews as allowed.
instrument universe, so `get_historical_prices` / `market_get_ohlcv` return
`UNAVAILABLE` for it. `broker_bars` reads the **broker's own** OHLCV series
(`copy_rates_from_pos`, server time) and returns a deterministic ATR(14) and
20-bar range computed from it, plus the contract size. Timeframes are
`M1/M5/M15/M30/H1/H4/D1/W1`, count clamped to 1–500. The agent uses it for gold
and is told never to substitute a proxy ETF for the instrument being traded.
Verified live: XAUUSD H1, 120 bars, contract 100, ATR(14) 14.82, 20-bar range
4005.20–4068.74 — the trade run placed ticket **11166047327** (BUY 0.01 @
4025.10, SL 4003.50 ≈ 1.46 ATR, TP 4055.00 ≈ 2.02 ATR).

**Stepping out (close, partial close, modify).** `position_close` closes one
position by `ticket`, every position in a `symbol`, or everything with `all: true`;
pass `volume` to close part of a ticket. `position_modify` moves `stopLoss` /
`takeProfit` (0 removes a level). Both only reduce or re-scope exposure, so — like
cancelling a working order — they need **no mandate, no approval and no live arm**,
and neither consumes a mandate order. They work on a demo or a live connection; a
mode mismatch is refused with `DEMO_ACCOUNT_REQUIRED` / `LIVE_ACCOUNT_REQUIRED`.
On a live connection, `all: true` closes only positions whose magic matches the
connection (name `tickets` to include others); this is the same ownership boundary
the kill switch uses. MT5 sends the exact
opposite market deal (or `TRADE_ACTION_SLTP`) against the named position ticket with
its magic, once, and an ambiguous reply is reported, never retried. Every close and
modify is **read back** and returns `verified` plus a `verifyNote`; a market fill
with SL/TP is verified the same way, so the tool never claims a bracket it did not
see stored. A wider stop can increase risk, so the tool text says so. The
dashboard's Open Positions widget exposes **SL/TP**, **Close** (with an optional
partial volume) and **Close all**; a **Recent Trades** widget lists the last eight
ledger orders with ticket, fill and status. Wide-stop, bracket management and
flatten-on-live beyond this path remain out of scope.

## Reconciliation (P4)

The broker is authoritative, so Eclipse keeps a durable local copy of what it
says happened:

```
POST /trading/connections/{id}/sync
GET  /trading/connections/{id}/reconciliation
GET  /trading/connections/{id}/fills
```

One sync reads positions, working orders and deal history, then writes:

| Table | Contents |
|---|---|
| `BrokerFill` | one row per deal, deduped by `(connectionId, dealTicket)` |
| `BrokerPositionSnapshot` | the current positions, replaced atomically each sync |
| `BrokerSyncState` | last sync time, advisory cursor, status, counts and the last error |

It also reconciles in-flight ledger orders: an order whose broker history is
`filled`, `partial`, `cancelled`, `rejected` or `expired` is moved to that state.
An order still present in the working set is marked `placed`. A sync never guesses:
an unknown or missing broker answer leaves the ledger row alone.

A failed sync records `error`/`unavailable` in `BrokerSyncState` and keeps the last
good fills and snapshot; it never deletes history or fabricates a fill.

**Background loop.** The reconciler is OFF by default. Set
`TRADING_RECONCILE_SECONDS` (minimum 15) to run it on a cadence; it only touches
connections whose status is `connected` or `unavailable`, and it never places an
order. Manual `POST .../sync` always works.

**History window.** Each adapter answers `deals_window(N)` from its own history
API. MetaTrader uses the last 7 days of deals with a 2-day future margin and a
3-day lower-bound margin: `history_deals_get` expects **server** time, and the
Python process clock can be hours behind or ahead (the current demo is +2h); a
tight local-time cursor silently drops the oldest or newest fill. REST venues page
their own fill/execution endpoints. Dedupe by deal ticket absorbs the overlap, so
the stored cursor is advisory, not authoritative.

**Journal projection.** The reconciled mirror is also the input to the
trading journal: `POST /api/journal/broker/sync` groups `BrokerFill` deals by
`positionTicket` and upserts one journal `Trade` per position, keeping the
broker authoritative for execution and the journal user-owned for notes,
reviews and self-assigned attribution. It never writes broker state. A first
import asks the reconciler for an explicit window with
`POST /trading/connections/{id}/sync {"days": N}` (1–3650); that calls the
adapter's `deals_window(N)` and still dedupes by deal ticket. See
`docs/journal-broker-sync.md` (ADR-059).

## Surface

| Layer | File |
|---|---|
| Contracts | `python/harness/trade/model.py` |
| Adapter protocol | `python/harness/trade/adapter.py` |
| Registry | `python/harness/trade/registry.py` |
| Store (`agent-memory.db`) | `python/harness/trade/store.py` |
| Service | `python/harness/trade/service.py` |
| Symbol facts | `python/harness/trade/symbols.py` |
| Order preview | `python/harness/trade/preview.py` |
| Pre-trade limits | `python/harness/trade/policy.py` |
| Demo mandates | `python/harness/trade/mandates.py` |
| Approval + guardrail seam | `python/harness/trade/authorizer.py` |
| MT5 adapter | `python/harness/trade/adapters/mt5.py` |
| MT5 wire tables | `python/harness/trade/adapters/mt5_contract.py` |
| Shared REST execution helpers | `python/harness/trade/adapters/orders.py` |
| REST adapters (17) | `python/harness/trade/adapters/{alpaca,ibkr,trading212,zerodha,tradier,oanda,binance,kraken,coinbase,bitstamp,okx,bybit,bitget,gateio,deribit,bitfinex,coincheck}.py` |
| MT4 bridge adapter | `python/harness/trade/adapters/mt4_bridge.py` |
| Reconciliation | `python/harness/trade/reconcile.py` |
| Graph node | `python/harness/graph_runtime/trading_nodes.py`, `trading_calc_nodes.py`, `trading_ops_nodes.py`, `trading_lifecycle_nodes.py`, `broker_nodes.py`, `src/lib/graph-studio/registry-trading.ts` |
| Tools | `python/harness/trade/tools.py` |
| Boot wiring | `python/harness/trade/bootstrap.py`, `service.py` |
| Routes | `python/harness/routers/trading.py` |
| Front door | `src/app/api/trading/[[...path]]/route.ts`, `src/lib/harness/trading-routes.ts` |
| Client | `src/lib/harness/clients/trading-client.ts` |
| Settings UI | `src/components/screens/trading-settings.tsx` |
| Sidebar connect | `src/components/layout/sidebar-broker-dialog.tsx`, `src/components/layout/sidebar-broker-panels.tsx` |
| Dashboard widgets | `src/lib/dashboard/trading-widgets.tsx`; shared read path `src/lib/dashboard/use-broker-snapshot.ts`; Home Trading section `src/components/screens/dashboard/home-trading-cards.tsx` + `src/lib/dashboard/use-trading-home-data.ts` + `src/lib/dashboard/trading-home-model.ts` |

Routes are operator-gated (loopback or runtime token); the front door injects
the token and never exposes it. The allowlist includes preview/submit/cancel,
arm/disarm/kill, sync/reconciliation/fills, mandate create/revoke and
approval-decide paths, but no bare order-send path: `POST /trading/orders`
returns an honest `501`.

Agent tools: `broker_connections`, `broker_account`, `broker_positions`,
`broker_orders`, `broker_quote`, `order_preview` (requests an approval, sends
nothing), `broker_mandates`, `order_submit` (the ONE effectful tool),
`order_ledger`, `broker_reconciliation` and `broker_fills`. `order_submit` requires
an active operator-created **demo** mandate; it cannot create/revoke a mandate,
arm live, cancel an order, or submit live. `position_close` (ticket, symbol, all,
volume) and `position_modify` (SL/TP) reduce or re-scope exposure and need no
mandate. `broker_bars` reads the broker's own OHLCV + ATR for symbols the
stored universe does not cover. `order_cancel`, arm/disarm and the kill
switch remain operator routes. `mandateId` may be omitted; the handler resolves the
one active mandate that covers the symbol, so a user never has to paste an id.

**Why `order_submit` is admitted by a mandate, not the generic approval card.**
An unattended mandate already carries the operator's consent, scope and caps. The
generic approval gate would double-gate it and make an "analyze then trade" run
impossible. The tool therefore runs through the same `BrokerService.submit_order`
path, and that handler/service enforces the demo account mode, policy, money
guardrails, symbol allowlist, max margin and the mandate order count. Live mandates
do not exist, so the agent cannot reach live.

The dashboard's six `trading` widgets (Quick Trade, Open Positions, Working
Orders, Margin Usage, Today P&L, Recent Trades) read the FIRST saved connection
and poll it every 15 s through a shared snapshot (`use-broker-snapshot.ts`, 10 s
dedupe). The Home dashboard's **Trading** section (`home-trading-cards.tsx`)
reads the same snapshot — read-only: broker-reported equity, today P&L
(deals since local midnight plus floating), a **realized P&L curve** built from
the broker's deals (last 30 days, cumulative, zero baseline), a **margin-usage
ring**, open positions with inline P&L bars, broker-reported margin risk with
per-symbol diverging bars, and the journal's edge stats plus an **equity
sparkline** (`analytics.equityCurrency`; all journal trades, not
broker-verified), a **drawdown (underwater) mini-chart** with max drawdown, and
**last-12-month P&L bars** from `analytics.monthlyReturns`. There is deliberately no broker equity curve: Eclipse stores
no balance history, and floating P&L is a single current number, so the curve is
realized-only rather than a fabricated balance line. Quick Trade runs preview → approve → submit against the same API and
shows the policy/guardrail reasons; Working Orders can cancel a ticket (risk-reducing,
no approval); a missing connection renders a Settings → Broker connection setup note instead
of a fake number. Today P&L is broker deals since local midnight plus the account's
floating P&L, labelled as broker-reported.

The shell's quick-launcher row (next to **Gateway** and **Graph**) also carries a
**broker icon** (handshake) with an **MT5** badge in its popup. In the collapsed
rail it stays in the footer. It opens a full **broker control center** with a
connection switcher and five tabs: **Overview** (live account tiles — balance,
equity, used/free margin, margin level, floating P/L, leverage, margin mode,
trade/algo flags — bridge health, connection timestamps and a quote lookup),
**Positions** (close one or all, risk-reducing), **Orders** (cancel a working
order), **Activity** (broker deals plus the Eclipse order ledger) and **Settings**
(edit label/mode/terminal/magic/max-margin, live arm/disarm/kill switch, add and
remove connections). Test, Sync and Refresh are available throughout. The dialog
uses the same `rounded-2xl border border-white/10 glass-card-strong` recipe as
the Gateway/Graph dialogs. Demo mandates and per-order approvals remain in
Settings → Broker connection; the dialog's live arm and kill switch reuse the
exact same operator routes.

## Roadmap

| Phase | Adds | Gate |
|---|---|---|
| **P0 (done)** | attach + read account/positions/orders/quotes | read-only |
| **P1 (done)** | demo order submission via `order_check` -> approval -> `order_send` | connection AND broker account must be `demo`; single-use approval |
| **P2 (done)** | armed live submission + kill switch | real account, Algo Trading, typed arm phrase, non-zero magic, positive `maxMargin`, time-boxed arm, per-order `LIVE ...` phrase |
| **P3 (done)** | Graph Studio `trading.broker_order` + bounded, revocable demo mandates | paper-mode graph + operator paper grant + demo connection + active mandate |
| **P4 (done)** | durable fills/positions/order-state reconciliation | broker history is the only source; a failed sync is an error, never a fake fill |
| **P4.5 (done)** | close (ticket, symbol or all), partial close, SL/TP modify, read-back verification | risk-reducing, no mandate/approval/arm; demo or live with a matching account; a live close-all is scoped to the connection magic |
| **P4.6 (done)** | per-trade risk, per-symbol notional, rolling 24 h loss brake, spread cap | config on the connection; brake blocks entries, never exits |
| **P4.7 (done)** | chat one-click live approval (`TradeApprovalBanner`) + Settings mode switch | preview -> approval -> typed LIVE phrase -> submit; no unattended live |
| **P5 (available, operator-gated)** | unattended live mandates | L2 + proof (out-of-sample + demo fills) + ramp stage + armed live connection + pinned live mandate <= 24 h + supervisor day/week stops |
| **P6 (done)** | live Graph Studio orders under an `allowLiveTrading` grant + workflow-pinned live mandate | compiler grant + armed connection + pinned mandate <= 24 h |
| **P9 (done)** | execution on every declared kind (19 brokers) through the same `TradingAdapter` contract | per-venue named capabilities; see *Execution across REST venues* |

**Autonomous live trading is enabled, and it is earned.** An operator who wants a bot
to trade live without a card must satisfy ALL of these, in order - there is no setting
that skips one:

1. **L2 authority** on the bot (operator-granted, versioned);
2. **proof**: out-of-sample results (costs included, positive expectancy) AND at least 30
   demo fills over 14 days with positive expectancy after commission/swap;
3. **ramp**: the live mandate's margin/order caps are clamped to `probe` (25%) until 20
   profitable live fills, then `build` (50%), then `scale` (100%);
4. **armed connection** (typed `ARM LIVE <login>`, time-boxed) for the connection;
5. **live mandate** pinned to the bot and its CURRENT objective version (<= 24 h TTL);
6. **supervisor**: day/week equity stops and the position/margin caps remain active.

Every order still passes the per-order stop requirement, position cap, cooldown, money
guardrails and the daily-loss brake. A demo mandate needs only L1 and no proof - that is
how the evidence in step 2 is earned.

## Honest boundaries

- Windows-only for MT5; other hosts report `PLATFORM_UNSUPPORTED`.
- The Python package attaches to a **running terminal**; it cannot conjure one.
- `initialize(path)` may start a terminal that is not running. The operator
  should log in first; Eclipse never supplies a password.
- Quotes are the broker's feed, not JDDS. They are broker-specific and may differ
  from other providers.
- Reads are synchronous IPC calls; every call opens a short session. A future
  streaming/reconciliation loop will own a long-lived session.
- Live submission requires an **armed** connection, an account the broker reports
  as `real`, a non-zero magic and a positive `maxMargin`; a demo account is refused
  on a live connection and a real account is refused on a demo connection. The
  arm reads the broker's own account mode; on MT5 it also requires Algo Trading.
- The kill switch cancels **working orders only** (by the connection's magic, or
  the `eclipse-<magic>-…` client id on REST venues). It does not close positions;
  `positions/close` (or the Open Positions widget) does that, arm-free because it
  only reduces exposure.
- Unattended mandates are not demo-only: a **bot** may hold a live mandate once L2,
  proof and the ramp stage allow it. A Graph Studio **node** may place a live
  order only under an `allowLiveTrading` grant plus a workflow-pinned live
  mandate; without both the compiler refuses it.
- Execution is per venue, not uniform: a spot exchange cannot hold an attached
  stop on the entry (`STOP_UNSUPPORTED`), and a venue with no client-order field
  cannot offer a scoped cancel-all (`OWNERSHIP_UNAVAILABLE`). Both are named
  refusals; neither is silently approximated.
- Graph Studio's ticket inputs are `ticket` ports: they accept an MT5 integer
  or a REST venue's opaque id (a UUID, an exchange id, `EUR_USD:LONG`), with a
  literal editor and compatibility with `number` and `string` producers. The
  canvas and the broker panel take the same id.
- REST venues are polled per read; there is no websocket stream. The background
  reconciler is off unless `TRADING_RECONCILE_SECONDS` is set.
- A demo fill is still a real broker round-trip (latency, requotes, spread), but
  it is not evidence that a live strategy is profitable.

## Declared broker kinds (generated)

<!-- BEGIN GENERATED broker-capability-matrix -->

Declared broker kinds, generated from `python/harness/trade/registry.py`.
Every row is a **declaration, not a runtime verification**: `available`
only reports whether the adapter's dependency imports on this host, and a
paper mode never grants live trading. Live execution still requires an
armed connection plus a mandate. In the Capabilities column `orders` means
**reading** working orders; only the Execution column permits placement, so
a kind without execution is read-only.

| Kind | Label | Auth | Modes | Asset classes | Capabilities | Execution | Available |
|---|---|---|---|---|---|---|---|
| `alpaca` | Alpaca | `credentials` | `demo`, `live` | `crypto`, `etf`, `stocks` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `binance` | Binance (spot) | `credentials` | `live` | `crypto` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `bitfinex` | Bitfinex v2 | `credentials` | `live` | `crypto`, `derivatives` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `bitget` | Bitget v2 | `credentials` | `live` | `crypto`, `derivatives` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `bitstamp` | Bitstamp v2 | `credentials` | `live` | `crypto` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `bybit` | Bybit (unified) | `credentials` | `live` | `crypto`, `derivatives` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `coinbase` | Coinbase Exchange | `credentials` | `live` | `crypto` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `coincheck` | Coincheck | `credentials` | `live` | `crypto` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `deribit` | Deribit (derivatives) | `credentials` | `live` | `crypto`, `derivatives`, `options` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `gateio` | Gate.io v4 | `credentials` | `live` | `crypto`, `derivatives` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `ibkr` | Interactive Brokers | `credentials` | `demo`, `live` | `etf`, `futures`, `fx`, `options`, `stocks` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `kraken` | Kraken | `credentials` | `live` | `crypto` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `mt4_bridge` | MetaTrader 4 (bridge) | `bridge` | `demo`, `live` | `cfd`, `futures`, `fx`, `metals`, `stocks` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `mt5` | MetaTrader 5 | `attach` | `demo`, `live` | `cfd`, `crypto`, `futures`, `fx`, `metals`, `stocks` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `oanda` | OANDA v20 (FX/CFD) | `credentials` | `demo`, `live` | `cfd`, `fx`, `metals` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `okx` | OKX | `credentials` | `live` | `crypto` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `tradier` | Tradier | `credentials` | `demo`, `live` | `etf`, `options`, `stocks` | `account`, `orders`, `positions`, `quote` | yes | yes |
| `trading212` | Trading212 | `credentials` | `demo`, `live` | `etf`, `stocks` | `account`, `orders`, `positions` | yes | yes |
| `zerodha` | Zerodha Kite | `credentials` | `live` | `etf`, `futures`, `options`, `stocks` | `account`, `orders`, `positions`, `quote` | yes | yes |

<!-- END GENERATED broker-capability-matrix -->
