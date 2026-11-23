---
title: Eclipse Computer Use vs Agent-S — gap analysis and plan to state of the art
status: Current
last-verified: 2026-11-22
---

# Eclipse Computer Use vs Agent-S

**Who this is for:** whoever owns the next iteration of Eclipse's desktop
capability. This is a comparison and a plan, not a feature announcement.

**Method.** Eclipse's side was read from source and *executed* on this Windows 11
host (`python/harness/computer/`, the tool registry, the agent graph, and the
computer tests). Agent-S's side was read from the upstream repository at
`main` (`simular-ai/Agent-S`: `gui_agents/s3/agents/{agent_s,worker,grounding}.py`,
`memory/procedural_memory.py`, `bbon/`, README) plus the Agent S3 and Agent S4
papers. Claims that could not be verified are labelled as such.

**Verdict in one paragraph.** Eclipse has the better *system* around desktop
control — safety, approvals, finance firewalls, background input without stealing
focus, multi-monitor/DPI, isolated desktops, verification, audit. Agent-S has the
better *desktop-control agent* — it gives the model pixels, a trained grounding
model, an explicit per-step protocol, a reflection critic, a working-memory text
buffer, a code-agent escape hatch, and a multi-rollout selection method that took
OSWorld from 66% to 72.6%. Eclipse's perception and grounding pipelines are
currently wired in a way that makes the two headline features — *screen of marks*
and *vision observation* — do nothing for the model. Until that is fixed, no
amount of policy work will produce state-of-the-art task success. The plan below
closes the agent gap **without** giving up the safety system, which is the part
Agent-S does not have.

---

## 1. What each system actually is

### 1.1 Eclipse Computer Use (as built today)

- One process-wide `ComputerManager` → `ComputerSession` → `ComputerCore` loop:
  `OBSERVE → UNDERSTAND → PLAN → GROUND → AUTHORIZE → ACT → OBSERVE → VERIFY`
  (`python/harness/computer/core.py:1-16`).
- 21 model-facing tools (`python/harness/agents.py:138-147`): one action
  workhorse `computer_use(action=…)` plus session/takeover/recording/finance/
  memory helpers (`python/harness/computer/schemas.py:35-133`).
- Observation modes `som | ax | ocr | vision` (`observation.py:85-181`), driven
  by the Windows UI Automation tree (`uiautomation`), Win32 child windows as a
  fallback, Windows Media OCR (`winocr`), Tesseract, or a model-runtime vision
  backend.
- Grounding ladder `accessibility → semantic → structured → OCR → vision →
  relative coordinate → coordinate` (`grounding/grounder.py:22-127`).
- Delivery is background-first via `PostMessage` to the deepest child control,
  foreground via `SendInput` only on escalation (`docs/harness/computer-use.md:97-126`).
- Policy/approval engine, finance firewall, money guardrails, sensitive-region
  masking, prompt-injection wrapping, takeover/human hand-off, recording/replay,
  telemetry (`policy.py`, `security.py`, `finance/`, `takeover.py`).
- It is exposed to a **general-purpose agent that carries ~366 tools**
  (`agents.py` `ECLIPSE_AGENT`), sharing a prompt with finance, coding, browser,
  research and document tools. The CUA-specific prompt is four bullet lines
  (`prompts.py:98-102`).

### 1.2 Agent-S (S3 / S4)

- A **purpose-built single-task CUA loop**. Per step: screenshot → Worker LLM
  produces `(Previous action verification)` → `(Screenshot Analysis)` →
  `(Next Action)` → `(Grounded Action)` as one line of `agent.*` Python
  (`memory/procedural_memory.py`), executed with pyautogui.
- **Vision-only perception**: the model sees the screenshot every step. No
  accessibility tree, no DOM.
- **A trained grounding model is mandatory** (UI-TARS-1.5-7B recommended;
  `README`), plus an OCR word-id table for text targets
  (`grounding.py:generate_coords`, `generate_text_coords`).
- **Reflection agent** on the trajectory, explicitly looking for action cycles
  (`worker._generate_reflection`; `REFLECTION_ON_TRAJECTORY`).
- **Working-memory text buffer**: `agent.save_to_knowledge([...])` appends notes
  that are re-injected into every subsequent step
  (`worker.generate_next_action` injects `Current Text Buffer`).
- **Code-agent escape hatch**: `agent.call_code_agent(task)` runs Python/Bash
  for bulk data work, with a 20-step budget and a structured report; the GUI
  agent must verify the result visually.
- **Behavior Best-of-N (BJudge)**: run k rollouts, caption each rollout as a
  behavior narrative, then a comparative judge picks the best. Sets SOTA 72.6%
  on OSWorld (human ≈72.36%), 66% for S3 alone at 100 steps; WindowsAgentArena
  50.2% → 56.6%; AndroidWorld 68.1% → 71.6% (paper 2510.02250, README).
- **Agent S4** evolves the *interfaces themselves* from unlabeled trajectories,
  with population-based selection on held-out validation tasks: 21.3% binary on
  OSWorld V2 (open SOTA) vs 13.0% GPT-5.5 and 20.6% Opus 4.8 at a fraction of
  the cost (paper 2610.28601, README).
- **Safety: effectively none.** It executes arbitrary Python/Bash locally, the
  prompt carries a hard-coded sudo password for the benchmark VM, and there is
  no policy engine, approval store, finance guardrail, or human hand-off.

---

## 2. Measured evidence from this machine

Run from `Black-Eclipse-site/python` with `PYTHONPATH` set, Windows 11
(10.0.26200), Python 3.12.10, `uiautomation` + `winsdk` + Pillow installed:

```
driver: windows   background_input=true foreground_input=true
accessibility=true screen_capture=true multi_monitor=true ocr=true vision=false
DOCTOR status: READY   (only failing check: "vision unavailable")

Run A (Eclipse app window in front):
mode=som    elements=11  chars=726   image=True   453 ms
mode=ocr    elements=304 chars=11177 image=True   375 ms

Run B (another Chromium window in front):
mode=ax     elements=8   chars=474   image=False
mode=vision elements=0   chars=64    image=True
  text: "[observation mode: vision]\n(no interactable elements were found)"
screenshot PNG: ~187-242 KB at 1920x1020
```

Three facts fall out of that, and they matter more than any feature list:

1. **The model never receives the screenshot.** `computer_use` returns rendered
   text only (`tools.py:279-289`, `_render_result` at 292-299); the image path
   lives in the tool's structured payload and tool results are
   `ToolMessage(content=<text>)` only (`tool_turn.py:395-511`). The frame is
   persisted to disk (`observation.py:151-156`) and never enters the model turn.
2. **`mode="vision"` is a no-op.** `include_vision_description` defaults to
   `False` (`observation.py:92`) and is never passed by `_act_capture`
   (`core.py:519-526`), so "vision" observation returns 64 characters of nothing.
   `vision=false` in the doctor is therefore not the only problem; even with a
   vision model configured the mode would still return nothing.
3. **`som` is text-only and unannotated.** It is described to the model as "a
   screen-of-marks with numbered elements" (`schemas.py:60-61`), but nothing is
   drawn on the image — the only `ImageDraw` use in the whole package is the
   privacy mask (`security.py:147-154`). On a Chromium-hosted app it produced
   **11 elements for a full 1920×1020 UI**; OCR produced 304 elements / 11,177
   chars for the same screen. That is the AX-first failure mode in one number.

Test evidence (this host, this commit):

```
python -m pytest tests/test_computer_benchmark.py tests/test_computer_unit.py \
  tests/test_computer_safety.py tests/test_computer_adversarial.py -q
67 passed in 3.19s

[Eclipse CUA Benchmark]
  dangerous_action_rate: 0.000
  false_action_rate_read_only: 0.000
  grounding_accuracy: 1.000
  noop_false_success_rate: 0.000
  recovery_success: 1.000
  task_completion_rate: 1.000
  verification_accuracy: 1.000
```

The 100% numbers are real *for the fixture* — and that is the point: the
"benchmark" is a five-needle synthetic fixture over a fake driver
(`test_computer_benchmark.py`), plus a single hand-built click. It measures
plumbing, not desktop task success. **Eclipse has no external CUA benchmark
result.** It cannot currently claim, or even estimate, an OSWorld or
WindowsAgentArena number. That is the single largest evidence gap in this
comparison, and it is why the plan starts with measurement.

---

## 3. Capability comparison

Legend: ✅ present and wired · ◐ present but partial/unwired · ❌ absent.

| Dimension | Eclipse | Agent-S | Reading |
|---|---|---|---|
| Sees the screen (pixels in model context) | ❌ | ✅ every step | Decisive. |
| Accessibility-tree perception | ✅ UIA + Win32 | ❌ | Eclipse wins where AX works. |
| OCR fallback | ✅ OS-native + Tesseract | ✅ Tesseract + OCR word-id grounding | Comparable, different use. |
| Visual grounding model | ❌ unreachable from `computer_use` (vision rung never wired); standalone `visual_ground` is a general VLM JSON-bbox prompt with self-reported confidence | ✅ dedicated UI-TARS-style model, calibrated coordinate space | Eclipse's is a fallback, not a grounding engine. |
| SOM annotation on image | ❌ (text only, despite the name) | ✅ marks + zoom-in crops for its narrator | Eclipse's naming is misleading. |
| Per-step protocol / dedicated CUA prompt | ◐ 4 bullets inside a 366-tool prompt | ✅ purpose-built 4-section protocol | Big behavioural difference. |
| Reflection / cycle detection | ❌ | ✅ per-step reflection agent | Less wasted steps. |
| Working memory within a task | ❌ (cross-run `computer_remember` only) | ✅ injected text buffer | Better long-horizon coherence. |
| Code-agent escape hatch | ◐ exists as separate tools, not offered by the CUA prompt as an action | ✅ first-class `call_code_agent` with budget/report | Eclipse has the engine, not the hand-off. |
| Verification of action effect | ✅ semantic digest + settle poll | ◐ model self-reports "previous action verification" | Eclipse wins. |
| Multi-rollout selection (best-of-N) | ❌ | ✅ BJudge 66%→72.6% | Largest single accuracy lever in the paper. |
| Interface evolution from experience | ❌ static adapters (`verified:false`) | ✅ S4 (OSWorld V2 21.3% vs 13.0% GPT-5.5) | Research frontier; Eclipse has the isolation to do it. |
| Background input without stealing focus | ✅ PostMessage to child control | ❌ pyautogui (foreground, moves cursor) | Eclipse wins, and it is a product-level win. |
| Multi-monitor / per-monitor DPI | ✅ | ❌ single monitor by design | Eclipse wins. |
| UIA pattern activation (Invoke/Select/Value) | ❌ clicks coordinates only | n/a | Robustness gap even within AX-first. |
| Capture of occluded/minimized windows | ❌ screen-region grab | ❌ screenshot of the screen | Both fail; fixable in Eclipse. |
| Autonomic recovery execution | ❌ ladder is only returned as advice | ◐ model retries, reflection discourages loops | Eclipse's ladder is better designed, unused. |
| App launch / close | ❌ (policy knows `launch_app`, no action) | ✅ `agent.open()` | Missing verbs. |
| Clipboard read/write | ❌ (policy knows it, no action) | ◐ via pyautogui hotkeys | Missing verbs. |
| Drag by element description | ❌ broken through the tool schema | ✅ `drag_and_drop(desc, desc)` | See §4.5. |
| Explicit wait-for-condition | ◐ `wait(ms)` only | ❌ fixed sleeps | Eclipse is close; add predicates. |
| Terminal `done`/`fail` | ❌ (outer agent decides) | ✅ | Ambiguous termination is a real failure source. |
| Policy / approvals / finance firewall | ✅ | ❌ | Eclipse wins decisively. |
| Sensitive-region masking before egress | ✅ | ❌ | Eclipse wins. |
| Prompt-injection fencing of screen text | ✅ | ❌ | Eclipse wins. |
| Human takeover / hand-back | ✅ | ❌ | Eclipse wins. |
| Isolated desktops | ✅ Xvfb/RDP/Sandbox/Wayland | ◐ VM/cloud only | Eclipse can enable parallel best-of-N. |
| Audit / replay / telemetry | ✅ | ◐ logs | Eclipse wins. |
| Cross-platform | ◐ Windows verified; macOS/Linux implemented, not live-verified | ✅ macOS/Windows/Linux | Honest gap in Eclipse. |
| Public benchmark result | ❌ none | ✅ OSWorld 72.6%, WAA 56.6% | Eclipse cannot yet be compared. |

---

## 4. Gaps, ranked by how much they cost

### 4.1 P0 — The model cannot see the screen (perception)

**Evidence.** §2 items 1–3; `tools.py:279-289`; `tool_turn.py:395-511`;
`observation.py:151-181`.

**Why it costs the most.** Every SOTA CUA result in 2025–26 is built on a model
looking at the screen. Accessibility trees do not exist for canvas apps, games,
charts, Electron/Chromium content that has not enabled a11y, custom-drawn
financial terminals, PDF canvases, or most installers. On this machine the AX
tree gave 11 elements for a full app; OCR gave 304 words but no icons, no
grouping, no state. The agent is effectively blind and is asked to reason about
`#7 text "" at (-2,1018,2x2)`.

The counter-argument — "text is cheaper and more precise" — is only true when a
rich AX tree exists. When it does not, OCR text costs *more* tokens (11,177 chars
≈ 2.8k tokens) than a downscaled screenshot (≈1–1.5k tokens) and carries strictly
less information.

**Fix.** Make observation multimodal end-to-end:
- `Snapshot` already carries `image_path` and `png_bytes`. Return the frame as an
  image content block on the **tool result** for `capture`/`read_screen` (and
  optionally on `capture_after`), downscaled to the provider's image budget,
  with the text digest kept alongside it.
- Draw the SOM marks on a copy of the image: numbered badges at element centres,
  colour-coded by source (AX / OCR). Then the text numbering and the picture
  agree, and `som` means what it says.
- When the image is sensitive, use the already-masked frame (`security.py`) or
  omit the image and say so in the tool result — never silently send the
  unmasked frame.
- Add a `detail`/`region` option so a 4K multi-monitor desktop is not shipped at
  full size; crop to the target window by default.
- Budget: one image per step by default, keep the last N (Agent-S uses 8) and
  summarise older steps as text.

### 4.2 P0 — Grounding is text matching, not visual grounding

**Evidence.** `grounding/grounder.py` ranks element names/roles with string
scoring (`rank_elements`, `matching.py`); the model rung is a general VLM asked
for a JSON bbox with self-reported confidence (`vision/grounding.py:47-56`);
`grounding_min_confidence` defaults to 0.6 (`computer.example.json`). A
self-reported 0.6 from a general VLM is not a calibrated probability.

Agent-S requires a *trained* grounding model and normalises its output
coordinate space (`resize_coordinates`) before acting. That is the difference
between "the model thinks the button is around here" and pixel-accurate targets.

Worse, Eclipse's vision rung is not merely weak — it is **unreachable**.
`Grounder.ground` consults `vision_lookup` only when a caller supplies it
(`grounder.py:85-101`), and no caller in `computer/` ever does (grep finds only
the parameter and its default). So `computer_use(target=...)` can only match AX,
an app adapter, or OCR. The only visual grounding that exists is the separate
`visual_ground` tool, which the agent has to call by hand and then convert into
coordinates itself.

**Fix.**
- Add a `GroundingBackend` interface beside `vision/backends.py` with two real
  implementations: (a) a UI-TARS-class model behind an OpenAI-compatible
  endpoint (local `llama.cpp`/vLLM or hosted), (b) the current VLM JSON prompt as
  an honest fallback. Report which one was used, exactly like the browser's
  provider routing.
- Normalise coordinates: the backend declares its output space (e.g.
  1000×1000 or 1920×1080), the adapter scales to screen pixels, and a click on a
  scaled screenshot of a HiDPI window applies the inverse transform. This is a
  classic source of systematic mis-clicks and it must be tested at 100 %, 125 %,
  150 %, 200 %.
- Fuse rather than replace: prefer an AX element whose bounds contain the
  grounded point; otherwise use the grounded point and record `method=vision`.
- Calibrate confidence against a labelled fixture: a 0.6 threshold is only
  meaningful if 0.6 has been measured.

### 4.3 P0 — No CUA agent; 366 tools and four bullets of prompt

**Evidence.** `agents.py` `ECLIPSE_AGENT` ≈366 tools (AST count), of which 21
are `computer_*`; `prompts.py:98-102`. Agent-S's entire prompt is a role, a
tool contract, and a forced four-section response.

**Why it costs.** Tool selection over 366 candidates is itself an error source;
the model must also decide *whether* to use the desktop at all. There is no
enforced "verify the previous action, analyse the screen, choose one action"
cadence, so a model can fire three actions into a stale UI. And every action
costs two model round-trips (capture, then act) because a handle is only valid
against the latest snapshot (`session.validate_handle`).

**Fix.** A dedicated **computer-use subagent** with a compact toolset
(`computer_use`, `capture/read`, `wait_for`, `notes`, `ask_human`, `done/fail`,
plus `delegate` to the code agent), a purpose-built prompt, and a
`capture_after=true` fast path that returns the post-action snapshot in the same
tool result (one round-trip per action). Eclipse already has the delegation
runtime (`delegate`, `TASK_TOOLS`) and the code profile — reuse them; do not
build a second agent framework.

### 4.4 P1 — No in-task memory and no reflection

**Evidence.** `computer_remember`/`computer_recall` are explicit, cross-run, and
never automatic (`memory.py`); the core returns a recovery *plan* but nothing
consumes it (`recovery.py` `execute()` has no caller in `computer/`); there is no
critic in the loop.

**Why it costs.** Long desktop tasks fail by losing the thread: which dialog was
already dismissed, which row was already edited, that the third attempt at the
same menu is a loop. Agent-S's text buffer + reflection directly target this, and
the S3 ablation attributes real gains to reflection.

**Fix.**
- A per-session `notes` buffer the agent can append to (`computer_use(action=
  "note")` or a `computer_note` tool) that is re-injected in the CUA subagent's
  prompt each step. Bounded (e.g. 20 bullets), summarised when full.
- A cheap **progress guard** in the core: hash `(action, target identity,
  window, ax_digest)` per step; on the third identical hash return
  `COMPUTER_LOOP_SUSPECTED` (verdict `replan`) instead of executing again. This
  is deterministic, needs no model call, and is testable.
- Wire `RecoveryEngine.execute` into `_deliver` for the safe rungs
  (`reobserve`, `reground`, `refresh_ax`) with a small budget, and keep
  `escalate_foreground`/`replan` as model decisions. Today the ladder is a
  suggestion; make the cheap half automatic.

### 4.5 P1 — The action surface is narrower than the policy implies

**Evidence.**
- `_ACTIONS` (`schemas.py:14-19`) has no `open`/`close`/`clipboard`/
  `wait_for`/`done`.
- `policy.py` defines `launch_app`, `close_app`, `clipboard_read`,
  `clipboard_write` — no action consumes them.
- **`drag` is unusable through the registry.** The core requires
  `from_coordinates`/`to_coordinates` (`core.py:688-697`) but the schema has no
  such properties (`schemas.py:37-67`), and the registry builds a pydantic model
  that silently drops unknown keys (`tools.py:474-507`). Verified on this host:
  ```
  model_dump: {'action': 'drag', ...}   # from_coordinates / to_coordinates gone
  has from_coordinates: False
  ```
  So `drag` always raises "drag needs from_coordinates and to_coordinates".
- Also unreachable: `allow_coordinates`, `confirm_coordinates`, `relative`,
  which the high-risk gate reads (`core.py:255-263`), so the documented
  coordinate override can never be exercised by the model.
- `set_value` can only target by bounds; there is no UIA `ValuePattern.SetValue`
  or `InvokePattern.Invoke` path.

**Fix.** Extend the schema and handlers: `open(path|app)`, `close(app)`,
`clipboard_get/set`, `wait_for(text|window|idle, timeout)`, `press_and_hold`,
`drag(from_element,to_element)` / `drag(from_coordinates,to_coordinates)`,
`scroll_into_view(element)`, `done(summary)` / `fail(reason)`. Add a schema
conformance test that asserts every `_act_*` handler's required kwargs exist in
the tool schema — that single test would have caught `drag`.

Then add **pattern-first activation**: if the target element came from UIA and
exposes `Invoke`, `Toggle`, `SelectionItem`, or `Value`, call the pattern; fall
back to mouse/keyboard only if the pattern is absent or throws. This is more
reliable than coordinates, works when the control is scrolled out of view, and
does not require the window to be visible.

### 4.6 P1 — Screenshot capture cannot see a background window

**Evidence.** `drivers/windows.py:506-516` grabs the screen region at the
window's bounds (`ImageGrab.grab(bbox=…)`, GDI `BitBlt` from the screen DC). If
another window covers it, the returned image is the cover. Minimised windows
yield the desktop.

**Why it contradicts the design.** Eclipse's headline promise is background
delivery that does not disturb the user. Background *input* works; background
*observation* does not. The agent acts on a window it cannot see, then
verification compares a screenshot of the wrong window.

**Fix.** Per-window capture in this order: `PrintWindow(hwnd,
PW_RENDERFULLCONTENT)` (works for most Chromium/UWP/DWM-composed windows), then
`Windows.Graphics.Capture` (Win10 1903+, GPU content), then screen-region grab.
Report the method in the snapshot (`captureMethod`) so verification can weight
it. If none works for a covered window, either say so honestly
(`background_observation=false`) or require an isolated desktop for background
tasks.

### 4.7 P1 — No benchmark, therefore no claim

**Evidence.** `test_computer_benchmark.py` is a 5-case fixture; grep shows no
OSWorld/WindowsAgentArena adapter anywhere in the repo. Agent-S publishes
72.6%/56.6% and an S4 number on OSWorld V2.

**Why it matters beyond marketing.** Without a stable, external task suite every
improvement above is unfalsifiable. The team cannot tell whether adding pixels
helped or hurt, and regressions in grounding/verification are invisible. This is
also the only way to answer "are we state of the art?" honestly.

**Fix.** Three lanes, in order of cost:
1. **Internal Windows suite (2 weeks).** 25–40 real tasks on this machine
   (Excel edit + save, VS Code refactor, PDF form, file dialog, settings toggle,
   trading-terminal read-only flow), each with a deterministic checker (file
   hash/cell value/DB row) and a fixed seed. Score pass@1, steps, wall time,
   tokens, approvals requested, unsafe actions.
2. **OSWorld adapter (3–4 weeks).** Eclipse's Xvfb provisioner is the right
   substrate; add a thin environment adapter that maps OSWorld's
   `setup/execute/get_obs` to `ComputerManager` and run the standard 369-task
   split at 100 steps. Publish the number even if it is low.
3. **WindowsAgentArena** for the Windows-native claim, once lane 2 is stable.

Track the same harness in CI on a self-hosted Windows runner for a 10-task smoke
subset.

### 4.8 P2 — No multi-rollout selection

**Evidence.** Nothing in `computer/` runs more than one trajectory.

**Why it matters.** BJudge is the largest single lever in the S3 paper (66% →
72.6%) and it is *orthogonal* to model quality. Eclipse is unusually well
positioned for it: isolated desktops already exist (`desktop.py` Xvfb/RDP/
Sandbox), so N rollouts can run concurrently without fighting over the broker.

**Fix.** `computer_run(task, attempts=N)` on the isolated-desktop path: spawn N
sessions, run the CUA subagent, caption each final state (the screen diff +
verification + step notes), then a judge model compares initial/final frames and
captions. Gate by cost: N=1 by default, N=3 for high-value tasks, never for
finance mutations. This must remain a *selection* mechanism, not a licence for
one branch to perform a mutation.

### 4.9 P2 — Adapters are static knowledge, not learned interfaces

**Evidence.** `applications/adapters.py` — 11 adapters, `verified: false` until a
live run marks them; `applications/base.py` is a knowledge record.

**Opportunity.** Agent S4 shows interfaces can be evolved from unlabeled
trajectories (21.3% vs 13.0% for the same model). Eclipse already records
trajectories (`recording.py`), has an explicit memory (`memory.py`), and has a
capability-build system (`capability_build`). The natural version: after N
successful runs on an app, propose an adapter/recipe (selectors, shortcuts,
known dialogs, wait conditions), validate it on isolated desktops against
recorded tasks, and only then mark `verified: true`. This is a differentiator
Agent-S cannot copy cheaply because it has no approval/validation substrate.

### 4.10 P2 — Cross-platform claims are unverified

macOS and Linux drivers exist (`drivers/macos.py`, `drivers/linux.py`,
`drivers/wayland.py`) but the docs say live verification is pending
(`docs/harness/computer-use.md:244-265`). Agent-S runs on all three. Either
verify them or scope the public claim to Windows.

---

## 5. What Eclipse does better (do not trade this away)

These are not consolation prizes; they are the reason Eclipse's CUA can be
shipped to a real user's desktop while Agent-S is a benchmark/research tool.

1. **Non-disruptive operation.** Background `PostMessage` to the deepest child
   control, thread-focused keyboard input, occlusion refusal
   (`docs/harness/computer-use.md:97-126`). pyautogui moves the real cursor and
   steals focus on every action.
2. **Verification.** A semantic AX/OCR digest plus a settle poll, and `uncertain`
   is never promoted to `success` (`verification/verifier.py:95-163`). Agent-S
   trusts the model's own claim that the last action worked.
3. **Effect safety.** Policy bands, single-use approvals, the finance firewall
   (transfer/withdraw/deposit/payment always blocked), money guardrails, and a
   high-risk target gate that refuses coordinate-only clicks on Buy/Sell
   (`core.py:245-279`). Agent-S will happily type a wire transfer.
4. **Data safety.** Sensitive-region detection and masking before any frame is
   persisted or sent to a cloud model (`observation.py:140-156`), injection
   scanning and untrusted fencing of screen text (`tools.py:50-68`).
5. **Human hand-off.** Takeover lease, `request_human` for CAPTCHA/MFA, resume
   (`takeover.py`, `tools.py:418-444`). Agent-S has no CAPTCHA/MFA handling:
   nothing forces a stop at a security challenge.
6. **Isolation and concurrency.** Real Xvfb/RDP/Windows Sandbox/Wayland guests
   (`desktop.py`), which also make best-of-N feasible.
7. **Multi-monitor and DPI.** `multi_monitor=true` on this host; Agent-S
   explicitly supports one monitor.
8. **Auditability.** Recording, replay, telemetry, per-session store.

The correct strategy is **Agent-S's agent quality on top of Eclipse's
substrate**, not a port of Agent-S.

---

## 6. Where Agent-S is weak (avoid copying these)

- **No safety model at all.** Arbitrary code execution, a hard-coded sudo
  password in the prompt, no approvals, no finance gating, no redaction.
- **Foreground-only.** Every action disturbs the user and races anything else on
  the machine.
- **Single monitor.**
- **Needs a second model server.** Performance depends on a UI-TARS endpoint;
  without it, accuracy drops and the setup is not zero-config.
- **Prompt/benchmark coupling.** The procedural memory contains benchmark-specific
  instructions (`password is 'osworld-public-evaluation'`, LibreOffice/Chrome
  specifics) and `set_cell_values` is Linux-only UNO. That is fine for a paper,
  wrong for a product.
- **Fixed sleeps and no structured verification.** `wait(1.333)` on a parse
  failure is the canonical example.
- **No provenance.** Screen-derived numbers are just text; Eclipse's
  `source_type=desktop_screen` rule is the right one.

---

## 7. What "state of the art" should mean for Eclipse

Do not adopt Agent-S's OSWorld number as the target; adopt a target *profile*:

| Metric | Today (Eclipse) | Target | Why |
|---|---|---|---|
| OSWorld 100-step pass@1 | unmeasured | ≥ 45% first published, ≥ 60% within two cycles | Honest first number, then a real SOTA track. Agent-S S3 = 66%, S3+bBoN = 72.6%. |
| Internal Windows suite pass@1 | unmeasured (fixture only) | ≥ 85% | Product quality on the apps Eclipse actually claims. |
| Steps per completed task | unmeasured | ≤ 1.35× human baseline | Long-horizon efficiency. |
| Model calls per action | 2 (capture + act) | 1 | `capture_after` in the act result. |
| Wall time per action | ~0.9–1.5 s overhead (capture + verify + 400 ms settle) | ≤ 0.6 s p50 | Responsiveness. |
| Grounding accuracy @150% DPI | unmeasured | ≥ 95% | The classic silent failure. |
| False-success rate (verification says success, task not done) | benchmark 0 | ≤ 0.5% on the internal suite | Trust. |
| Unsafe-action rate (finance/destructive without approval) | 0 by construction | 0 | Non-negotiable. |
| External benchmark reproducibility | none | one command, pinned image | Credibility. |

---

## 8. The plan

Sequenced so that each phase is independently shippable and measurable. Effort
is engineer-days for one experienced engineer, including tests and docs.

### Phase 0 — Measurement first (5–7 d)

**Why first.** Every later claim depends on it; it also protects against
regressions while changing perception.

- `python/harness/computer/eval/`: a task-suite runner (YAML task + setup +
  checker), a result record (pass, steps, calls, tokens, ms, approvals, unsafe
  actions), and a CLI `eclipse computer eval --suite windows-core`.
- 25 tasks, each with a deterministic checker, run against a *disposable*
  profile: temp files, a scratch Excel workbook, a scratch VS Code workspace.
- Wire the 10-task smoke set into CI on the Windows runner; fail on >2 pp
  regression.
- Replace the misleading 100% fixture "benchmark" with a clearly named
  `plumbing_fixture` test, and make the real suite the benchmark.

**Acceptance:** `eclipse computer eval --suite windows-core` prints a stable
score three runs in a row (variance ≤ 4 pp) and the number is recorded in
`docs/harness/computer-use.md`.

### Phase 1 — Perception: pixels to the model (4–6 d)

Files: `observation.py`, `models.py` (`Snapshot`), `tools.py`,
`tool_turn.py` (image content parts), `screenshots/capture.py`.

- Annotated SOM copy: numbered badges + optional role glyphs; keep the raw frame
  for vision.
- `computer_use(capture)` returns `[image, text]`; `capture_after=true` returns
  the post-action image in the same result.
- Image pipeline: crop to window → downscale to a configurable max edge →
  encode → token estimate; keep the last N images and replace older ones with
  their text digest (Agent-S's `max_trajectory_length` idea, done in the harness
  rather than the agent).
- Sensitive frames: masked image or no image + explicit `image_omitted` reason.
- `mode="vision"` actually calls the vision router and returns a description, or
  honestly says vision is unavailable.

**Acceptance:** on this host, `capture` returns a non-empty image block and
`mode="vision"` returns a description or an explicit `VISION_UNAVAILABLE`; a
test asserts the tool result carries an image and that a sensitive snapshot never
does.

### Phase 2 — Grounding: a real grounding backend (6–9 d)

Files: new `computer/grounding/backends.py`, `vision/grounding.py`,
`grounder.py`, `config.py`, `drivers/*` coordinate transform.

- `GroundingBackend` protocol: `locate(image, target) -> [GroundedElement]`,
  `coordinate_space`, `available`, `describe`.
- Backends: `ui_tars` (OpenAI-compatible endpoint; config
  `HARNESS_COMPUTER_GROUNDING_URL/MODEL`), `vlm_json` (current prompt, honest
  fallback), `ocr` (deterministic).
- Coordinate normalisation + HiDPI inverse transform, unit-tested at 1.0/1.25/
  1.5/2.0 scale and on a secondary monitor with a negative origin.
- Grounder order becomes `AX element → app adapter → OCR → grounding model →
  coordinates`, with the chosen method and a *measured* confidence in the result.
- Calibration fixture: 40 labelled controls across 5 apps; report precision at
  the 0.6 threshold; adjust the default from data, not intuition.

**Acceptance:** grounding accuracy on the fixture ≥ 95% at 100% and 150% DPI;
`method` is reported on every target; no silent coordinate fallback.

### Phase 3 — The agent: a dedicated CUA subagent (6–8 d)

Files: `agents.py` (new preset), `prompts.py` (CUA protocol), `graph.py` /
delegation wiring, new `computer/notes.py`.

- A `computer` subagent: 8–10 tools, forced four-section response mirrored from
  the proven pattern (previous-action verification → screen analysis → next
  action → grounded action), one action per turn, explicit `done`/`fail`.
- Working notes buffer (bounded, summarised) re-injected each step.
- Fast path: `capture_after` so one action = one model call.
- Deterministic loop guard in the core (`COMPUTER_LOOP_SUSPECTED`).
- Code-agent hand-off: when the task is bulk data/file work (spreadsheet range,
  rename many files, parse a PDF), the prompt requires `delegate` to the code
  agent and then visual verification — the Agent-S `call_code_agent` pattern with
  Eclipse's existing, safer runtime.

**Acceptance:** internal suite steps drop ≥ 20% vs Phase 0 at equal pass rate;
no task exceeds the action budget without a `fail`/`ask_human`.

### Phase 4 — Execution robustness (7–10 d)

- Per-window capture: `PrintWindow` → `Windows.Graphics.Capture` → screen grab;
  `captureMethod` in the snapshot; a covered-window test.
- UIA pattern activation before mouse input; record `activation=invoke|click`.
- Schema/action conformance test (this is what catches `drag`).
- New verbs: `open`, `close`, `clipboard_*`, `wait_for`, `press_and_hold`,
  element-to-element drag, `scroll_into_view`, `done`/`fail`.
- Wire the safe recovery rungs into `_deliver`.
- Latency: reuse the pre-action frame for verification when the window did not
  move; make settle adaptive (stop at first semantic change; 150 ms default,
  400 ms only after a foreground escalation).

**Acceptance:** `drag` works through the registry (test);
covered-window capture returns the target window's content or an honest
`background_observation=false`; p50 action overhead ≤ 0.6 s on the internal
suite.

### Phase 5 — Scaling: best-of-N on isolated desktops (8–12 d)

- `computer_run(task, attempts=N, selector=behavior_judge)`.
- Behavior caption: verification result + diff summary + notes + final frame.
- Judge compares captions and initial/final frames; picks one; the losing
  sessions are destroyed, never merged.
- Safety: N>1 forbidden for any session whose plan contains a finance mutation
  or a CRITICAL action; approvals are per-branch and single-use, so a rejected
  branch cannot authorize another.
- Cost guard: budget per task, N capped by configured `max_concurrent`.

**Acceptance:** on the internal suite, N=3 improves pass@1 by ≥ 5 pp at ≤ 2.8×
cost; unsafe-action rate stays 0.

### Phase 6 — Learned interfaces (10–15 d, research)

- Mine successful trajectories per app → propose adapter deltas (selectors,
  shortcuts, known dialogs, wait predicates).
- Validate candidates on isolated desktops against replayable tasks; only then
  set `verified: true` with a build note.
- Keep the existing rule: never auto-write screenshots or secrets into memory.

**Acceptance:** at least one adapter (Excel or a trading terminal) improves the
internal suite by ≥ 5 pp and its `verified` flag is backed by a recorded run id.

---

## 9. Priority table

| # | Change | Impact | Effort | Risk |
|---|---|---|---|---|
| 1 | Internal task suite + `computer eval` | Makes everything else measurable | 5–7 d | Low |
| 2 | Pixels + annotated SOM to the model | Largest accuracy gain | 4–6 d | Medium (tokens, privacy) |
| 3 | Grounding backend + DPI transform | Removes silent mis-clicks | 6–9 d | Medium (new dependency) |
| 4 | Dedicated CUA subagent + prompt + notes | Fewer steps, fewer loops | 6–8 d | Low |
| 5 | `capture_after` fast path | 2× fewer model calls | 1–2 d | Low |
| 6 | Schema conformance + `drag`/verbs | Fixes a broken action | 3–4 d | Low |
| 7 | Per-window capture | Background promise becomes true | 3–5 d | Medium (Win32) |
| 8 | UIA pattern activation | Robustness | 3–4 d | Low |
| 9 | Recovery execution + loop guard | Fewer wasted turns | 3–4 d | Low |
| 10 | Best-of-N | +5–6 pp (paper) | 8–12 d | Medium (cost) |
| 11 | OSWorld adapter | External credibility | 3–4 w | Medium |
| 12 | Learned adapters | Differentiator | 2–3 w | High (research) |

**Do 1–5 first.** They are ~3–4 weeks and they change the class of the system.
6–9 are hardening. 10–12 are the SOTA track.

---

## 10. Risks, and what not to do

- **Do not replace the AX-first ladder with vision.** Hybrid is strictly better:
  AX is exact when available, vision covers the rest, OCR covers text. The
  grounder should report which rung won.
- **Do not let images leak secrets.** The masking path already exists; route the
  model image through it and add a test that a password field cannot reach a
  cloud provider.
- **Do not let best-of-N multiply authority.** A rollout is a *candidate*, not a
  second approval. Finance mutations remain single-branch and approval-bound.
- **Do not copy Agent-S's prompt verbatim.** It contains benchmark credentials
  and Linux-only app assumptions. Take the *structure*, not the content.
- **Do not publish an OSWorld number without a pinned image, seeds and a
  reproduction command.** A number nobody can rerun is worse than no number.
- **Do not break zero-config.** A grounding model must be optional: absent it,
  the system falls back to the current ladder and says so (`UNAVAILABLE` with a
  reason), exactly like the rest of the harness.
- **Do not regress background operation** to chase screenshot fidelity. If a
  window cannot be captured in the background, say so; do not silently foreground
  every action.

---

## 11. Implementation status (updated 2026-11-22)

The P0/P1 gaps below were closed in this cycle; the P2 research items are not.

| Gap | Status | Where |
|---|---|---|
| Pixels to the model | **fixed** — a bounded screenshot is attached to capture/read/act results | `computer/tools.py`, `tool_turn.py` |
| `vision` mode no-op | **fixed** — returns the image and an honest description | `computer/observation.py` |
| Unannotated SOM | **fixed** — numbered badges drawn on the frame | `screenshots/annotate.py` |
| Vision rung unreachable | **fixed** — `vision_lookup` wired to the grounding backend | `computer/core.py`, `session.vision_ground` |
| No trained grounding model / DPI mapping | **fixed** — UI-TARS-class backend with declared coordinate space | `grounding/backends.py`, `computer/config.py` |
| `drag` unreachable | **fixed** — coordinates and elements both supported; schema regression test | `schemas.py`, `core.py`, `test_computer_state_of_art.py` |
| Missing verbs (`open`/`close`/clipboard/`wait_for`/`press_and_hold`/`invoke`/`scroll_into_view`/`note`/`done`/`fail`) | **fixed** | `core.py`, `schemas.py` |
| UIA pattern activation | **fixed** — Invoke/Toggle/Select/Value with click fallback | `accessibility/windows.py` |
| Occluded-window capture | **fixed** — PrintWindow first, method reported | `drivers/windows.py` |
| Recovery executes | **fixed** — bounded reobserve → retry on a failed expectation | `core.py`, `recovery.py` |
| Expectation checks dead | **fixed** — `expect` is reachable and `axContains` reads real text | `core.py`, `verifier.py` |
| No loop guard | **fixed** — deterministic `COMPUTER_LOOP_SUSPECTED` | `core.py` |
| No in-task memory | **fixed** — bounded notes + last-eight-actions trajectory | `session.py`, `tools.py` |
| No automatic procedural memory | **fixed** — a verified `done` yields a scrubbed workflow candidate; opt-in auto-store; best-of stores only the winner | `computer/procedural.py` |
| No per-app redaction | **fixed** — trading / broker / password-manager / banking profiles, mask-all for vaults, custom `AppProfile` | `computer/security.py` |
| No dedicated CUA agent | **fixed** — `desktop-agent` preset + protocol prompt | `agents.py`, `prompts.py` |
| Two model calls per action | **fixed** — `capture_after` defaults true, screenshot attached | `core.py`, `tools.py` |
| Image context growth | **fixed** — newest 8 screenshots kept, older media dropped | `tool_turn.py` |
| Cross-platform live verification | **open** — macOS/Linux still not live-verified here | — |
| External benchmark (OSWorld/WAA) | **adapter added, OSWorld run open** — `drivers/osworld.py` + `eval/osworld.py` drive the standard environment and score from `env.evaluate()`; the run needs the OSWorld VM and a configured model. The local Windows suite publishes **pass@1 = 12/12 (1.000)** on twelve real owned-window tasks (`scripts/cua_benchmark.py`) | `computer/eval/` |
| Best-of-N selection | **fixed** — `computer_run` runs N branches, captions behavior, deterministic rank + optional model **behavior judge over captions only** (fallback on failure), refuses N>1 for finance, serializes on a shared desktop | `computer/best_of.py`, `runner.py`, `judge.py` |
| Learned interfaces (S4) | **bounded implementation** — verified workflows derive versioned, validated per-app adapters (controls, shortcuts, recipes); live observations confirm or stale them (3 misses retires the adapter), stale records stop matching, and recall exposes them. Full interface *evolution* (meta-agent, population search) remains research | `applications/learned.py` |



## 12. Bottom line

Eclipse's Computer Use is a **strong desktop-control runtime with a weak
desktop-control agent**. It has solved the hard production problems — safety,
approvals, non-disruptive input, isolation, verification, audit — that Agent-S
does not attempt. But the agent never sees the screen, its "vision" mode returns
nothing, its SOM is a text list, its grounding is string matching plus a general
VLM guess, it has no reflection or working memory, its recovery ladder is advice,
and it has never been measured on a real task suite. Those are the gaps that
separate "a careful automation framework" from "a state-of-the-art computer-use
agent".

The fastest credible path is: measure first (Phase 0), then make the loop
multimodal and grounded (Phases 1–2), then give it a real CUA agent protocol and
memory (Phase 3), harden execution (Phase 4), and only then scale with
best-of-N and learned interfaces (Phases 5–6). Done in that order, Eclipse can
plausibly reach the 60 %+ OSWorld band while keeping a safety record Agent-S
cannot match — which is a more defensible definition of state of the art for a
product that types into a user's real trading terminal.
