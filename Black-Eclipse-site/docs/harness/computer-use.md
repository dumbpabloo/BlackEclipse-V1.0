---
title: Computer Use Engine
status: Current
last-verified: 2026-11-22
---

# Computer Use Engine

**Who this is for:** anyone extending Eclipse's desktop-control capability, or
debugging a run that drove a native application.

> Deciding whether a task belongs here, in the web-research engine, or in the
> browser? See [`research-surfaces.md`](./research-surfaces.md) — the one "which
> surface answers which question" page.

The **Computer Use Engine** (`python/harness/computer/`) lets Eclipse observe and
operate a real desktop: native applications (Excel, VS Code, a terminal
*emulator*, PDF readers), OS chrome (file pickers, dialogs, menus) and financial
terminals (TradingView, MetaTrader 5, Interactive Brokers, Bloomberg-style). It is
a first-class capability registered on the **same** `ToolRegistry`, gated by the
**same** permission/approval architecture, persisted on the **same**
`agent-memory.db`, and observable on the **same** event bus. It is not a parallel
stack.

**The model sees the screen.** An observation returns a real screenshot (crop →
downscale → JPEG, bounded) attached to the tool result as model media, with the
`#N` handles drawn on the image as numbered badges. The tool result also carries
the text list and the working notes. A state-changing action attaches the
post-action screenshot by default (`capture_after`), so one action is one model
call. Only the newest 8 screenshots are kept in context; older ones are replaced
by their text digest.

## The fundamental loop

```
OBSERVE → UNDERSTAND → PLAN → GROUND → AUTHORIZE → ACT → OBSERVE → VERIFY
        → (SUCCESS | RECOVER → REPLAN)
```

Every important action carries state awareness and verification. A result hands
back a **verdict** (`success`, `recapture`, `retry`, `reground`,
`foreground_required`, `approval_required`, `blocked`, `ask_user`, `abort`) so the
agent follows a directive instead of guessing.

## Computer Use vs the browser subsystem

They are **different systems that coexist**:

| | Browser (`harness/browser/`) | Computer Use (`harness/computer/`) |
|---|---|---|
| Owns | DOM, HTML, page nav, forms, browser content | native apps, OS UI, dialogs, file pickers, app chrome, browser *chrome* |
| Observation | accessibility snapshot (`@eN`) | SOM (`#N`) from the AX tree, OCR, vision |
| Use it for | web pages | anything outside a page's DOM |

The browser is **not** replaced by Computer Use, and vice versa. Browser *chrome*
(address bar, permission dialogs, extension popups) belongs to Computer Use.

## Layer map

| Module | Owns |
|---|---|
| `core.py` | the platform-independent orchestrator (`ComputerCore`): the ONE loop; delivery-mode escalation; finance routing |
| `manager.py` | the process-wide `ComputerManager` (sessions, driver, engines, leases, store) |
| `session.py` | one `ComputerSession`: state, snapshot cache, stale-handle validation, focus restore |
| `drivers/` | `ComputerDriver` interface + `windows.py` (ctypes Win32), `linux.py` (ctypes X11/XTest), `macos.py` (ctypes Core Graphics/AX), `wayland.py` (grim/ydotool), `window.py` (attach to a contained remote/isolated desktop) + `null.py`; the factory picks the platform driver |
| `accessibility/` | AX providers: `windows.py` (UIA when installed, Win32 child-window baseline), `linux.py` (AT-SPI), `macos.py` (AX API), `null.py` |
| `observation.py` | builds a `Snapshot` in mode `som`/`ax`/`ocr`/`vision`; mints snapshot ids + numbered handles |
| `screenshots/annotate.py` | draws the numbered SOM badges on the frame (red=AX, blue=OCR, green=vision) |
| `grounding/` | `Grounder` (the fixed hierarchy) + pure matchers |
| `grounding/backends.py` | the optional UI-TARS-class grounding endpoint (coordinate space normalised here) |
| `actions/` | `mouse.py`, `keyboard.py`, `value.py` (`set_value`), `keymap.py` (semantic shortcuts) |
| `windows/` · `applications/` | window management; application management + the `ApplicationAdapter` framework, shipped adapters and `learned.py` (versioned adapters from verified workflows) |
| `screenshots/` | capture + region intelligence + screen diff |
| `ocr/` | first-class OCR (`windows.py` OS-native via `winsdk`; Tesseract; honest null otherwise) |
| `vision/` | provider-agnostic vision via the harness model runtime; local/cloud/hybrid routing |
| `verification/` | `ActionVerifier` (verified / failed / uncertain) |
| `recovery.py` | the recovery ladder + `RecoveryEngine` |
| `policy.py` · `security.py` | permission engine + risk bands; sensitive-region detection/redaction; challenge + injection detection |
| `finance/` | field vocabulary, screen extraction (with provenance), the **Finance Action Firewall** |
| `takeover.py` · `broker.py` | the per-session control lease; the machine-wide session broker |
| `desktop.py` | `DesktopProvisioner` (local shared; real Xvfb, RDP, Windows Sandbox; Wayland attach) — create/attach/teardown of a session's desktop |
| `recording.py` · `replay.py` | trajectories + deterministic replay |
| `telemetry.py` · `diagnostics.py` | metrics; the doctor |
| `store.py` · `memory.py` | `agent-memory.db` tables; Eclipse-Memory knowledge (not JDdS) |
| `schemas.py` · `tools.py` | `computer_*` model-facing toolset |
| `best_of.py` · `runner.py` · `judge.py` | behavior-captioned best-of-N, the desktop-agent branch runner, and the optional model judge |
| `eval/` | the suite runner (pass@1) + the OSWorld driver/bridge |
| `routers/computer.py` | the operator HTTP surface |

## Stable element handles + stale protection

Every `capture` mints a `snapshot_id` and a generation. An element handle
(`#N`) is only valid against the snapshot it came from. An action that passes an
`element` (and its `snapshot_id`) is validated for snapshot, window, application
and identity; a mismatch returns `COMPUTER_STALE_ELEMENT` (verdict `recapture`)
and the model must re-capture. An old element number is **never** silently mapped
to a new UI element.

## Grounding hierarchy

```
1 accessibility element → 2 semantic → 3 structured UI → 4 OCR → 5 vision
→ 6 relative coordinate → 7 raw coordinate
```

Rung 5 is **live**: when `grounding.url`/`grounding.model` are configured, the
session asks the backend for the described control, scales its declared grid to
the captured frame and then to screen coordinates, and only then hands the point
to the ladder. With no endpoint the rung is honestly skipped (AX/adapter/OCR
only) — never a fabricated point. Coordinates are the fallback; high-risk targets
require a fresh semantic target, a fresh observation and policy approval.

**Progress guard.** The core fingerprints `(action, target, text, UI state)` and
refuses the third identical attempt against the same state
(`COMPUTER_LOOP_SUSPECTED`, verdict `replan`). Any changed UI state resets it, so
clicking "next" five times is not a loop.

**Recovery runs.** A failed expectation gets one bounded pass of
`reobserve → retry` through `RecoveryEngine.execute`; the attempts are reported in
the result's `recovery` block. `uncertain` is still not success: the model
re-captures.

**Working notes.** `computer_use(action="note", text=…)` appends to a bounded
per-session buffer that is re-injected with every observation, alongside the last
eight actions. This is in-task memory, distinct from the cross-run
`computer_remember` store.

## Background-first delivery

Default delivery is **background**: input is posted directly to the target
window and the user's cursor, focus and virtual desktop are not disturbed. A
`foreground` escalation (SendInput; raises/focuses) is explicit, counted in
telemetry, and the previous focus is restored afterwards. A background attempt
that produces no change escalates **once** — never silently, never in a loop.

Two guarantees make this safe on Windows:

- A background click is posted to the **deepest child control under the point**
  (`RealChildWindowFromPoint`, z-order independent) with **client** coordinates;
  a message sent to the top-level window alone would never activate a child
  control. Background keyboard input targets the owning thread's focused control
  (`GetGUIThreadInfo`).
- A **foreground** absolute-coordinate click is refused when the point is owned
  by a different top-level window (`COMPUTER_TARGET_OBSCURED`, verdict
  `foreground_required`) — Eclipse never clicks blind into a covering window, and
  never sends foreground keystrokes to a window that is not focused.

Verification reads a **fresh** accessibility digest + window title (never the
pre-action snapshot), settle-polls for a bounded window, and treats only
*content* change as `verified`; incidental pixel churn is `uncertain`, and an
`uncertain` result never becomes a `success` verdict.

Background-first delivery covers clicks, typing, keys, **pointer moves, scrolls,
drags and button press/release**: each posts the corresponding `WM_*` message to
the control under the point in the target's own child hierarchy, so none of them
need steal the user's focus. `mouse_move`/`scroll`/`drag` follow the configured
`mode` and may still be forced `foreground`.

**Observation can see a background window.** A window capture tries
`PrintWindow(PW_RENDERFULLCONTENT)` first (a DWM window that is behind another
still renders), falls back to a screen-region grab only when that yields a blank
image, and records `captureMethod` (`print_window` / `screen_grab`) on the
snapshot. A `screen_grab` of a covered window is the occluder — the method tells
the caller which one it got.

Observation also scans for **sensitive** controls/text: a sensitive snapshot is
flagged, its persisted image is masked, and a cloud vision model is refused
unless policy explicitly allows it. An unrelated dialog that appears after an
action is reported as `UNEXPECTED_DIALOG` (verdict `ask_user`) instead of
receiving blind input. A **CAPTCHA / MFA / biometric / security challenge**
(detected from the foreground window or screen text via
`security.detect_challenge`) is a hard stop: the core refuses *before* delivering
any input and returns `COMPUTER_HUMAN_REQUIRED` (verdict `ask_user`) so a person
takes over — Eclipse never tries to solve or bypass it.

**Limits are enforced, not just advertised.** Every action runs under a
per-action timeout (`action_timeout_seconds`; breach ⇒ `COMPUTER_TIMEOUT`,
verdict `retry`) and a hard session budget (`max_actions`; breach ⇒
`BudgetExceeded`/`COMPUTER_BUDGET_EXCEEDED`, verdict `abort`). A dry run still
counts against the budget, matching what `capabilities().limits` reports.

## The agent-facing surface

One workhorse `computer_use(action=…)` plus small read/support tools
(`schemas.py`). The action set is complete enough to drive an app without
workarounds:

| Group | Actions |
|---|---|
| Observe | `capture` (window / `region` / `display_id`; mode `som`/`ax`/`ocr`/`vision`), `read_screen` |
| Pointer | `click`, `double_click`, `right_click`, `middle_click` (+ `modifiers`), `mouse_move`, `mouse_position`, `scroll`, `scroll_into_view`, `drag` (coordinates **or** `from_element`/`to_element`), `press_and_hold` |
| Keyboard | `type`, `key`, `hotkey` |
| Semantics | `set_value` (UIA ValuePattern first, keyboard fallback), `invoke` (Invoke/Toggle/Select pattern first, click fallback), `focus` (UIA SetFocus, no click), `wait_for` (text or window, bounded) |
| Apps | `open`, `close`, `focus_app`, `list_apps`, `list_windows`, `list_displays` |
| Clipboard | `clipboard_get`, `clipboard_set` |
| Control | `note`, `done`, `fail`, session/takeover tools |
| Knowledge | `computer_remember`, `computer_recall` (incl. learned interfaces), `computer_forget_interface` |
| Scale | `computer_run` — best-of-N over fresh desktop sessions |

`expect` lets the caller state the effect (`stateChanged`, `windowChanged`,
`dialogAppeared`, `axContains`), and `axContains` is checked against the live
accessibility text (not a digest). `allow_coordinates` + `confirm_coordinates`
are the documented override for a critical coordinate target, and both are now
reachable from the schema.

## Best-of-N (a brittle task, several rollouts)

Single-rollout desktop use compounds early mistakes. `computer_run(task, attempts=N)`
runs N independent **branches** and keeps one:

- a branch is a fresh `ComputerSession` with its own run id; the desktop-agent
  branch runner drains that run (the tools resolve their session through
  `ComputerManager.session_for_run`, so parallel isolated branches cannot act on
  each other's desktop);
- each branch is reduced to a **behavior caption**: outcome, verified success,
  step counts, failed steps, the action trail, notes and final window. Captions
  are deterministic — no extra model call is needed to describe a branch;
- selection is deterministic by default: a **verified success** beats an
  unverified claim, then fewer failed steps, then fewer steps. When the run's
  model is available, a **behavior judge** (`judge.py`) compares the captions
  (text only, never screenshots) with a strict prompt and may pick another
  **successful** branch; it can never promote an unsafe or failed one. A judge
  failure or an unusable reply falls back to the deterministic pick;
- isolated desktops run concurrently; the shared desktop runs branches serially
  so input never interleaves;
- a branch does **not** write shared memory or learn adapters — `done` inside a
  branch is suppressed, and only the winner's workflow/adapter is recorded;
- a task that names a financial mutation is refused at N>1
  (`COMPUTER_MULTI_ATTEMPT_FINANCE`) — one approved branch, never a race;
- without the branch runner the tool is an honest
  `COMPUTER_BRANCH_RUNNER_UNAVAILABLE`.

## Evaluation

`computer/eval/` is the measurement layer, not a second runtime:

- `suite.py` — tasks with a deterministic checker, and a `run_suite` report with
  pass@1, per-task time and failures. A raising task is a failure; there is no
  partial credit.
- `osworld.py` + `drivers/osworld.py` — run the SAME engine against an OSWorld
  `DesktopEnv`: the driver turns observation/input into the environment's
  screenshot and pyautogui code, and the score comes from the environment's own
  `evaluate()`, never from the agent's claim. An episode is `success` only when
  the agent run completed cleanly **and** the environment score is positive; a
  crashed agent is reported as `agentCrashed:true` with `success:false`, even if
  the final state happens to score — the score stays visible, the pass does not.
- The live Windows suite (`tests/test_computer_eval.py`) and
  `python scripts/cua_benchmark.py` run **12 real tasks** against an owned Win32
  window with the real driver and report pass@1 (locate/read, click, UIA invoke,
  set-value, window listing, wait-for, clipboard, drag, press-and-hold,
  scroll-into-view, region capture, learned interface). It is a capability suite
  (scripted actions), not an intelligence benchmark; an OSWorld pass@1
  additionally needs the OSWorld VM and a configured model.

## Learned interfaces (from verified workflows)

A verified workflow candidate also teaches Eclipse the application's interface.
`applications/learned.py` derives a versioned `LearnedAdapter`:

- controls it actually acted on (`button Save`, `textbox Ticker`), shortcuts it
  used, and the task→steps recipe;
- every derivation increments the version and merges into the prior record;
- structural validation requires ≥1 named control, a recipe and verified-run
  evidence; `validated` means "derived from a verified run", NOT "live-verified
  against this build" (the live rig's `verified` flag stays separate);
- the learned adapter is registered in the session's adapter registry, so the
  grounder's semantic rung can use it immediately, and it is persisted to the
  gitignored `learned_adapters.json` sidecar under the artifacts root;
- **live validation:** every observation that still shows a known control resets
  the miss counter (`lastValidatedAt`); `STALE_AFTER_MISSES` (3) consecutive
  observations without one mark the interface **stale**, it stops matching in the
  adapter registry, and only a new verified run (a new version) revives it;
- `computer_recall` returns learned interfaces (application, version, controls,
  shortcuts, known tasks, stale state) even when Eclipse-Memory is empty;
  `computer_forget_interface` removes one (a wrong/stale record), leaving shipped
  adapters untouched.

## Isolated desktops

`desktop.py` decides WHERE a session runs. `isolation.mode=off` (default) uses the
shared local desktop; `auto`/`on` provisions an isolated one, falling back to local
with a reason if the backend is absent. `headless_desktop` is true only for a
genuinely provisioned isolated desktop.

| Provider | Backend | Isolated | Status |
|---|---|---|---|
| `local` | the operator's desktop | no | default; sessions serialize via `broker.py` |
| `x11` | private `Xvfb :N` per session | yes | **real** — create/attach/teardown, tested |
| `rdp` | `mstsc` to `rdp.host`/`rdp.file` | yes | **real** — attaches a `WindowDriver` to the client window |
| `windows_sandbox` | `WindowsSandbox.exe` guest window | yes | **real** — attaches to the sandbox client window |
| `wayland` | nested headless compositor (`wlheadless-run`/`sway`/`weston`) | yes | **real** — private socket; `grim` capture + `ydotool` input |

- **`WindowDriver`** (`drivers/window.py`) is the attach mechanism for remote/isolated
  desktops: it scopes capture/input to the contained desktop window and translates
  window-local coordinates to absolute foreground events. Input is **background-first
  when the base driver supports it**, escalating to foreground (with verification)
  otherwise. Because the guest's native accessibility tree is not host-visible, the
  driver derives a real element tree from **OCR of the contained window**
  (`source=ocr`); with no OCR backend it reports `accessibility:false` honestly.
- An **isolated session does not take the machine-wide broker**, so N agents run
  concurrently on N desktops; shared sessions still serialize.
- **`WaylandDriver`** (`drivers/wayland.py`) reports capture and input separately:
  a missing `grim` makes capture False, a missing `ydotool` makes input False —
  each with a reason. `WaylandHeadlessProvisioner` gives a session its own headless
  compositor socket when a launcher/compositor is installed.

## Security, permissions, finance

- The **policy engine** (`policy.py`) is the ONE permission authority:
  `READ`/`LOW`/`MEDIUM`/`HIGH`/`CRITICAL` risk bands, an application
  allow/deny/prohibit policy, and fail-closed defaults (observe allowed; click/
  type ASK; `finance_mutate` DENY). An ASK routes through the same single-use
  approval store the rest of the harness uses.
- The **security layer** flags sensitive UI (password/API key/seed phrase/2FA/
  bank credentials) and masks regions before a frame leaves the machine or enters
  an exportable trajectory. A **per-app profile** (`security.DEFAULT_APP_PROFILES`)
  widens the vocabulary for the window it matches: a trading terminal's
  "Buying Power", "Net Liq", positions and P&L are account data even though the
  generic label list does not name them; a password manager (1Password,
  Bitwarden, KeePass, LastPass) masks EVERY text field; broker/banking titles add
  balance/account/routing labels and account-number patterns. A custom profile is
  a record (`AppProfile`), not new detection code. Masking converts screen
  coordinates to the captured frame's local coordinates (origin + scale), and a
  sensitive snapshot also redacts its text and element values before they reach
  the model or a trajectory. Clipboard reads are redacted and labeled UNTRUSTED,
  like screen text. A sensitive frame is never sent to a cloud vision model unless
  policy explicitly allows it.
- The **Finance Action Firewall** (`finance/firewall.py`) classifies and gates
  every financial intent. Modes: `read_only`, `assisted`, `autonomous`,
  `simulation`. `transfer`/`withdraw`/`deposit`/`payment`/`account_change` are
  always blocked; a live mutation needs approval; `assisted` may prepare but not
  submit; notional/account/instrument/app allowlists are enforced **before** the
  UI is touched. The firewall is now followed by the money guardrails
  (`finance_guardrails/`): a deny refuses, and a confirm forces an approval even
  when the firewall's mode would allow autonomously. A click/type that names a
  money verb passes the same guardrails before delivery, so the prompt's "never
  click Submit to bypass the firewall" is enforced, not merely requested. See
  `money-guardrails.md`.
- Screen-derived finance carries explicit provenance
  (`source_type=desktop_screen`) and is never authoritative market data.

## Integration

- **Tools:** `register_computer_tools` registers the `computer_*` set on the one
  registry (in `service.py`, beside the browser/terminal/git tools). The elevated
  half (`COMPUTER_EFFECTFUL_TOOLS` in `code/policy.py`) is removed at `plan_only`;
  `computer_use` stays (like `browser_click`) and is gated per-call.
- **Authorizer:** `computer/authorizers.py` is composed into the ONE chain in
  `code/authorizers.py`.
- **Agents:** `COMPUTER_TOOLS` is declared once in `agents.py` and offered to the
  finance agent, the code agent and the chat code profile. The dedicated
  `desktop-agent` preset gives the same capability a compact toolset and the
  step protocol (`prompts.DESKTOP_AGENT_SYSTEM_PROMPT`: previous action → screen
  → one action → verify), for desktop-only work.
- **HTTP:** `routers/computer.py` (`/computer/*`, operator-gated) is included by
  `main.py`; lifespan calls `service.start_computer()` / `stop_computer()`.
- **CLI:** `eclipse computer doctor|status|capabilities|windows|apps|displays|sessions|replay`.
- **Memory:** `computer/memory.py` stores learned layouts/workflows/recovery paths
  in `eclipse-memory` (never JDdS, never screenshots). The base write path is the
  explicit `computer_remember` tool; automatic capture is opt-in (below).
  `computer/procedural.py` builds a **workflow candidate** after a `done` that is
  a verified success: step sequence, target labels, notes and task — no typed
  text, no field values, no frames, secret-shaped text scrubbed. The candidate is
  surfaced for an explicit `computer_remember`; `memory.auto_capture=true` also
  records it (tagged `source=auto_capture`), and best-of-N stores only the
  WINNER's candidate.
- **Human hand-off:** `computer_takeover` (and `computer_request_human` for a
  challenge) hand the control lease to a person; the agent stops, state is
  preserved, and `computer_resume`/`computer_release` return control.

## Configuration

`computer/config.py` resolves defaults ← `.black-eclipse/computer.json` ← env
(`HARNESS_COMPUTER_*`). A reference file is committed at `computer.example.json`.
Notable keys now wired end-to-end: `vision.model` (overrides the resolved vision
model id), `ocr.enabled` (disables OCR entirely) and `ocr.backend`
(`auto|winocr|tesseract|none`), the finance allowlists
`finance.allowed_apps|allowed_accounts|allowed_instruments`
(`HARNESS_COMPUTER_FINANCE_ALLOWED_*`), desktop isolation
`isolation.mode` (`off|auto|on`), `isolation.provider`
(`auto|local|x11|wayland|windows_sandbox|rdp`), `isolation.display_base`,
`isolation.screen`, `isolation.rdp_host`, `isolation.rdp_file`
(`HARNESS_COMPUTER_ISOLATION`, `HARNESS_COMPUTER_ISOLATION_PROVIDER`,
`HARNESS_COMPUTER_DISPLAY_BASE`, `HARNESS_COMPUTER_RDP_HOST`, `HARNESS_COMPUTER_RDP_FILE`),
and the GUI grounding endpoint `grounding.url`, `grounding.model`,
`grounding.api_key`, `grounding.width`, `grounding.height`,
`grounding.timeout_seconds` (`HARNESS_COMPUTER_GROUNDING_URL`,
`HARNESS_COMPUTER_GROUNDING_MODEL`, `HARNESS_COMPUTER_GROUNDING_API_KEY`,
`HARNESS_COMPUTER_GROUNDING_WIDTH`, `HARNESS_COMPUTER_GROUNDING_HEIGHT`,
`HARNESS_COMPUTER_GROUNDING_TIMEOUT`), and automatic procedural memory
`memory.auto_capture` / `memory.min_actions`
(`HARNESS_COMPUTER_MEMORY_AUTO_CAPTURE`, `HARNESS_COMPUTER_MEMORY_MIN_ACTIONS`).
The endpoint is optional; an empty URL is reported as `grounding.configured=false`,
never a fake backend. Auto-capture is off by default.

## Honesty (nothing is fabricated)

A missing driver, accessibility backend, OCR engine or vision model is an honest
`UNAVAILABLE` with a reason — never a fake desktop. On Windows a window record
carries the process stem as `app`, and `get_window` builds a record directly from
the handle for an untitled/custom window, so application profiles and learned
adapters always have an identity to match. `eclipse computer doctor`
reports exactly what is present. On Windows the driver uses `ctypes` Win32 (no
third-party package required). `auto` OCR prefers the OS-native engine
(`Windows.Media.Ocr` via `winsdk`), then Tesseract; the rich accessibility tree
(UIA via `uiautomation`) and the native OCR are optional and their absence is
reported, not hidden. `Pillow`/`uiautomation`/`winsdk` are declared in
`requirements.txt` and installed/repaired by `setup-python.js`/`doctor.js`.

## Platform support and roadmap

| Platform | Driver | Input | Accessibility | Status |
|---|---|---|---|---|
| Windows | `windows.py` (ctypes Win32) | foreground (SendInput) + background (PostMessage) | UIA (installed) / Win32 baseline | **Ready** |
| Linux/X11 | `linux.py` (ctypes X11/XTest) | foreground (XTest) | AT-SPI (if `python3-pyatspi`) | Implemented; live-verification pending |
| macOS | `macos.py` (ctypes CGEvent/AX) | foreground (CGEvent) | AX API (Accessibility TCC) | Implemented; TCC permissions required |
| Linux/Wayland | — | — | — | Honest `NullDriver` (no global-input API) |

The driver factory returns a real driver per platform and a genuine `NullDriver`
where no backend exists (Wayland-only sessions without `grim`/`ydotool`, unknown
OS). Isolated desktops are covered above. Remaining honest gaps:

- **Live app verification** (`tests/test_computer_apps_live.py`, opt-in via
  `BLACK_ECLIPSE_CUA_APPS=1`) drives real apps; adapters carry a `verified` flag
  that defaults False (knowledge record) until a live run marks it with a build note.
- **Xvfb** has an opt-in live test (`tests/test_computer_xvfb_live.py`,
  `BLACK_ECLIPSE_CUA_XVFB=1` on Linux) — the injected-probe lifecycle runs everywhere,
  the real capture runs on a Linux host.
- **Sandbox/RDP input** is best-effort: background-first where the base driver
  supports it, else foreground with verification; a client may still not forward
  every event. The guest element tree is OCR-derived, not native AX.

## Tests

`python/tests/test_computer_unit.py` (pure logic), `test_computer_safety.py`
(finance firewall, approval boundary, stale protection, takeover, dry-run),
`test_computer_tools.py` (the toolset over a deterministic fake driver),
`test_computer_adversarial.py` (moved/duplicate/absent targets, DPI, finance
adversarial matrix), `test_computer_hardening.py` (the defects found by the
production stress test — background child-control delivery, occlusion refusal,
semantic verification, sensitive wiring, action allowlist, recovery plan,
bounded `set_value`, cross-session snapshot ids, memory source) and
`test_computer_state_of_art.py` (annotated SOM, media attachment, grounding
coordinate scaling, the drag schema regression, loop guard, notes, expectation
verification, recovery execution, clipboard honesty, wait_for, bounded image
history).

The **live stress rig** (`python/tests/computer_stress_rig.py`,
`computer_stress_target.py`, `computer_stress_focus.py`) drives the REAL Windows
driver against a controlled Win32 window our test target owns, so it exercises
the actual input path without disturbing the live desktop. It covers observation,
stale handles, UI change, grounding, verification, input, recovery, freeze/race,
the finance firewall matrix, takeover, sensitive data, audit/replay/memory,
latency, session isolation, failure injection, prompt injection and focus-safety.

The **live demo suite** (`test_computer_demos.py`) runs five end-to-end
workflows against windows the test owns (locate/read, find-and-click,
set-value-and-verify, read financial state, prepare-never-submit) with the real
driver. It is part of the default Windows suite and passes on this host.

Additional production harnesses:

- **`test_computer_demos.py`** — the demo workflows (locate/read, find-and-click,
  set-value-and-verify, read financial state, prepare-never-submit) run against
  owned Win32 windows with the real driver.
- **`test_computer_benchmark.py`** — the Eclipse CUA Benchmark: grounding
  accuracy, verification accuracy, recovery success, task completion, and the
  finance priority (`false_action_rate_read_only`, `dangerous_action_rate`,
  `noop_false_success_rate`), each asserted at its production bar.
- **`computer_finance_e2e.py`** + **`computer_finance_terminal.py`** — a full
  finance scenario on a mock trading terminal: screen-derived state with
  provenance, a deterministic rebalance calc, and proof (from the terminal's own
  click log) that read_only blocks, assisted holds for approval, and no mutation
  control is ever clicked.

## High-risk target gate

A CRITICAL action (a financial mutation, or a Submit/Buy/Sell/Transfer control an
adapter flags risky) is held to a stricter standard than a normal click:
`ComputerCore` refuses a **coordinate-only** target or a target whose confidence
is below the floor, and requires the target to be grounded against the CURRENT
snapshot. A caller can override only by passing BOTH `allow_coordinates=true` and
`confirm_coordinates=true`. Coordinates are never the primary method for a
critical action.
