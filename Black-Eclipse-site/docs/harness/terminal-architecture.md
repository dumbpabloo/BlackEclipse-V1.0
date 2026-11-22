---
title: Terminal, Shell & Process Subsystem
status: Current
last-verified: 2026-11-21
---

# Terminal, Shell & Process Subsystem

**Who this is for:** anyone changing how Eclipse executes commands, keeps a shell
open, manages background processes, detects ports, or exposes any of that to the
agent or the UI.

This is the **execution backbone**. It is not a terminal widget: the agent tools
and the terminal UI are two thin adapters over one shared runtime, so they can
never disagree about whether a process is running.

## The one sentence

> A command is classified before it runs; a process's state is owned by the
> runtime, never the UI; and an unimplemented capability refuses honestly rather
> than pretending.

## Layering

```
python/harness/terminal/
  manager.py         TerminalManager  — the ONE facade (tools + router call it)
  session.py         persistent shells (cwd/env/stdin/dimensions/Ctrl+C)
  process_manager.py background processes, trees, signals, crash detection, restart
  spawn.py           per-process isolation (Windows Job Object / POSIX group)
  proctree.py        OS parent map (tasklist equivalent) for real trees + port ownership
  buffer.py          RingBuffer (sequenced, bounded) + head/tail model truncation
  port_manager.py    listening ports + conflicts (OS table, never a hard-coded list)
  shell_detect.py    shells / WSL / capabilities
  runtimes.py        Local, WSL, Docker, SSH (all real; each probes + refuses honestly)
  resources.py       CPU/memory telemetry + runaway limits (no psutil dependency)
  redaction.py       secrets never reach the model/log
  store.py           TerminalSession / ProcessRecord / TerminalCommand rows
  recovery.py        reconcile persisted state on boot
  runtime.py         the process-wide singleton + EventBus bridge
  tools.py           the agent tool surface + effect authorizer
python/harness/code/terminal.py         the ONE classifier (allow/ask/deny); no execution
python/harness/code/terminal_tools.py   terminal_run — the manager in no-shell mode
python/harness/routers/terminal.py    the operator-gated HTTP surface
src/components/terminal/xterm-terminal.tsx   the real xterm.js terminal (UI)
```

The agent reaches it through the ONE `ToolRegistry`; the UI through
`/api/terminal/**` → `routers/terminal.py` → the same `TerminalManager`. There is no
second execution path.

## Two primitives, deliberately

* **`execute_command`** — one-shot, bounded, cancellable. Runs through
  `code/sandbox.py` (unchanged), so the wall-clock/output/process-tree bounds and
  the scrubbed environment apply. It blocks only its own worker, never the agent
  loop (handlers call it via `asyncio.to_thread`).
* **`start_process`** — a long-running process the runtime keeps alive and
  monitors. It has NO ordinary execution timeout; only an explicit stop/kill, a
  crash, or a `timeoutMs` the caller chose ends it.

`open_shell` + `run_shell_command` are the third shape: a **persistent shell** whose
`cd` and `export` survive across commands. A real PTY (`pywinpty` on Windows,
`ptyprocess` elsewhere) is installed by default (platform markers in
`python/requirements.txt`) so `capabilities.pty` is `true` out of the box; if a PTY
library is genuinely absent the session falls back to a piped persistent shell and
reports `pty: false` — the fallback is never called a PTY. A `cd`/`export` is sent
to the REAL shell (not just tracked) and the resulting prompt is read back via the
sentinel, then confined to the workspace; a command that moved the shell outside
the root is refused and the shell is returned to the root. The agent-facing text
has ANSI stripped; the UI reads the raw buffer so a real terminal can render.

The code profile's `terminal_run` and the `.black-eclipse/verify.json` auto-verify
hook are **not** a fourth primitive. Both call
`TerminalManager.execute_command(..., shell_mode=False)`, which tokenizes the
command with `shlex.split(posix=True)` and executes the argv directly (no shell),
so the historical `code/sandbox.run` bypass and `code/terminal.py::build_run_spec`
are gone. `code/terminal.py` is now the ONE classifier; the manager is the ONE
executor.

## Process lifecycle

`CREATED → STARTING → RUNNING → {STOPPING →} {STOPPED|EXITED|CRASHED|KILLED|TIMEOUT|FAILED}`.
State is applied once, under a lock, by the monitor thread that reaps the real OS
exit — never inferred from "does a PID exist". A non-zero exit becomes `CRASHED`
with a `CommandError` (`category: PROCESS_CRASH`, or `COMMAND_NOT_FOUND` for
`127`/`9009`) and the last output tail.

Output is pumped per stream into **separate** `RingBuffer`s, so stdout and stderr
are never silently merged. Every line carries a monotonic `sequence`; an agent
polls `read_process_output(cursor=…)` and never sees a line twice.

## Ports

`port_manager` reads the OS listening table (`netstat -ano` / `ss -ltnp`). A
process's ports are collected over its **whole OS tree**, because a shell wrapper
does not itself listen — the dev server it launched does. A conflict reports the
owner pid/process; the runtime never kills an unrelated process.

## Security

Enforced at three layers, matching the existing code capability:

1. **Allowlist** — `code/policy.py` removes every terminal *write* tool at
   `plan_only`, so the autonomy boundary is structural, not a prompt (`allowed_tools`
   now includes `WRITE_TOOLS` + `EXTERNAL_ACTION_TOOLS` + `TERMINAL_WRITE_TOOLS`).
2. **Effect authorizer** — `terminal/tools.py::make_terminal_tool_authorizer` is
   composed into the graph's chain. An `allow` command runs; an `ask` command
   requires a single-use, workspace-bound approval; a `deny` is refused. A pending
   `ask` approval is created with kind `terminal_command` (or `shell_command`) and
   is listed by `GET /code/approvals` alongside `code_action` — without that, the
   request would be invisible in the UI and the agent would report a dead end.
   `mkdir`/`md` is `allow` (runs with no approval); the long `New-Item` form is
   `ask`. The code-agent prompt tells it to prefer the auto-allowed short form.
3. **Handler** — the executing handlers re-classify (defense in depth) and confine
   `cwd` to the workspace root.

Secrets are redacted on every model-facing and logged path (`redaction.redact`),
reusing `session_index.redact_span`. The environment is the scrubbed
`sandbox.build_env` (never `os.environ` passthrough).

## Persistence & recovery

`store.py` owns three additive tables on the ONE `agent-memory.db`
connection: `TerminalSession`, `ProcessRecord`, `TerminalCommand`. No process
output is stored relationally; only the metadata and cursors needed to recover.

On boot `service.start_terminal()` runs `RecoveryManager.reconcile()`: a persisted
process whose pid is gone is marked `EXITED`; one whose pid still exists is marked
`UNKNOWN` (owned but no longer managed) — never `RUNNING`. Persisted sessions are
closed. Nothing is fabricated and nothing is silently adopted.

## Events

`TerminalManager` publishes onto the shared `EventBus` as `terminal.*`
(`terminal.session.created`, `terminal.process.started`, `terminal.process.output`,
`terminal.process.crashed`, `terminal.port.*`, …), carrying `runId` when known so a
run's SSE consumer sees them.

## Runtimes

| Runtime | Commands | Background processes | Interactive sessions |
|---|---|---|---|
| `local` | yes | yes | yes |
| `wsl` | yes | yes | yes |
| `docker` | yes (`docker exec -i`) | yes | no — refuses `NOT_IMPLEMENTED` |
| `ssh` | yes (`ssh <host> <cmd>`) | yes | no — refuses `NOT_IMPLEMENTED` |

Docker and SSH are configured by the operator, never guessed:
`HARNESS_TERMINAL_DOCKER_CONTAINER` (+ `_WORKDIR`, default `/workspace`), and
`HARNESS_TERMINAL_SSH_HOST`. SSH uses the host's own OpenSSH config and agent
(`BatchMode=yes`); the subsystem never reads, stores or forwards a key or
password. Each runtime probes availability and refuses with `RUNTIME_UNAVAILABLE`
when unconfigured — `select_runtime` never degrades to local.

## Resource monitoring and runaway protection

`resources.system_resources()` reads CPU count, memory and load directly from the
OS (`GlobalMemoryStatusEx` / `sysconf` / `/proc/meminfo`, load average where the
platform exposes it) — **no `psutil` dependency**. `ResourceLimits` (env
`HARNESS_TERMINAL_MAX_PROCESSES` / `_RUNAWAY_SECONDS` / `_MAX_OUTPUT_BYTES`) bounds
the subsystem: a start past `maxProcesses` is refused `PROCESS_LIMIT`, and a
process whose output buffer or lifetime exceeds its bound is FLAGGED `runaway`
(never auto-killed) with a `terminal.process.runaway` event. `resource_report`
and `system_info` surface the snapshot; the UI's Processes view shows the flag.

## Terminal UI

The terminal opens from a button at the **header's right edge** (next to the search
bar) and from **Ctrl/Cmd + `**. Both drive one shared `useTerminalStore`
(`src/lib/terminal/store.ts`), so the button and the shortcut are the same control
and cannot disagree. The shortcut listener (`src/components/layout/terminal-keybind.tsx`)
lives in the root layout, listens in the **capture phase on `window`**, and matches
`event.code === 'Backquote'` (layout-independent; `event.key` varies by keyboard),
calling `preventDefault`/`stopPropagation` so neither the browser nor a focused
xterm can swallow it. The open state is persisted; the panel has an expand/restore
control and a "New" shell action.

The terminal is a **real docked region the app layout reserves space for**, not a
floating overlay — so it never covers the app (or the ChatBot). `TerminalDock`
(`src/components/terminal/terminal-dock.tsx`) is rendered by the shell
(`stock-chat.tsx`) in three slots:

* **bottom** — a strip inside the shell, below the main content (the main column
  shrinks to `h-80` above it);
* **right** — a full-height column beside the main panel, as a sibling in one flex
  row (`sidebar | main | terminal`), so the main panel shrinks by the dock's width;
* **full page** — the only floating slot, because nothing else is visible behind it.

Only the slot matching the chosen placement renders; the choice is persisted and
hydrated on mount (`useTerminalShell`). The terminal refits and the PTY resizes on
every placement change, and the shell is preserved across switches (bottom → right
keeps its history). In focus mode the terminal is tucked away.

**Narrow screens.** The right column collapses to a bottom strip below `md`
(`md:w-[24rem] lg:w-[26rem]`), so a side dock can never overlap the main panel in
sidebar/mobile mode; the header wraps rather than crowding.

**Detach & new window.** A detach control turns the dock into a draggable floating
pop-out that SHARES the running session; a new-window control opens
`/terminal?session=<id>` — a standalone page (`terminal-window.tsx` +
`src/app/terminal/page.tsx`) that attaches to the same shell, or opens its own when
no id is given. `terminalOwnerId()` keeps the dock, pop-out and window on one owner.

**Instant open.** The shell is ready before the user asks for it: the service
pre-opens one idle session at boot (`TerminalManager.warm_shell`, owner
`WARM_OWNER`), the root layout warms the runtime + terminal chunks
(`terminal-keybind.tsx`), and the dock starts the session on the server in
parallel with the xterm download. The first open adopts the warm shell, so the
prompt appears in ~1s instead of waiting on a cold Python spawn + PowerShell start.

**Frosted glass.** The dock and the detached pop-out use `glass-card-strong`; the
xterm host box is transparent so the frost shows through, and the pop-out is
portaled to `document.body` so its `backdrop-filter` samples the real page rather
than an empty backdrop root.

**Resize.** The docked bottom (height) and right (width) panels have a pointer-drag
handle on their inner edge; a change refits xterm and posts the new rows/cols to
`/terminal/sessions/{id}/resize` so the shell wraps correctly. The pop-out is
dragged by its header and resized by its corner handle (pointer-driven; the native
CSS `resize` handle is unreliable inside a portal).

**Device-Attributes noise.** The shell asks the terminal "what are you?" (Device
Attributes, `ESC[c`); xterm auto-replies `ESC[?1;2c`. Because every keystroke is
forwarded to the PTY, that reply reached PowerShell, which cannot interpret it and
echoed the literal `?1;2c` at the prompt. The reply is now dropped from the INPUT
stream (`filterTerminalInput` in `xterm-terminal.tsx`) before it reaches the shell —
the shell never needs it — with an output-side strip (`stripTerminalNoise`) as a
fallback for a session that echoed it before this fix.

Input is sent as an **ordered queue**: the client keeps one request in flight and
coalesces keystrokes typed during a round-trip, and the runtime serializes PTY
writes with a per-session lock — together this keeps byte order exactly the typing
order (without it, "terminal" arrived as "terimnal").

The Terminal tab itself is a **real** interactive terminal: **xterm.js**
(`src/components/terminal/xterm-terminal.tsx`) rendering a live PTY-backed shell
in the runtime.

* **Input** — every keystroke goes straight to the PTY
  (`POST /terminal/sessions/{id}/raw`), so interactive programs (a REPL, a prompt,
  `vim`, Ctrl+C, arrow keys, TUI apps) all work.
* **Output** — a long-poll stream (`GET /terminal/sessions/{id}/stream`) delivers
  raw bytes with ANSI preserved (~0.06 s latency, not a timer), so xterm renders
  colours and layout. The backend strips its hidden cwd sentinel from the display.
* **Resize** — the window is fit to the panel and the PTY is resized to match.
* **Durability** — the session lives in the Python runtime, so closing the panel
  or reloading the page does NOT kill the shell. The panel passes a durable
  `owner` id and reattaches to the same shell; "New shell" rotates the id to open a
  fresh one. The Processes and Ports tabs read the same backend the agent tools use.

## Adding a runtime

Implement the `ExecutionRuntime` protocol in `runtimes.py` (`available`,
`describe`, `shell_for`, `build_argv`, `map_path`, `extra_env`) and add a branch to
`select_runtime`. An unavailable runtime MUST raise `RuntimeUnavailable`;
`select_runtime` never degrades. `extra_env()` names only the non-secret host
variables the client needs (e.g. `DOCKER_HOST`, `SSH_AUTH_SOCK`), merged over the
scrubbed base environment.

## Why the shell tool still exists

`terminal_run`/`shell_run`/`shell_spawn` predate this subsystem and remain for
backward compatibility. The new `terminal/*` tools are the general surface; both
share the same classifier (`code/shell_tools.py::_verdict`) and approval model.

`shell_spawn` is now **watched**: `harness/background_jobs.watch_process` waits
on the process in a daemon thread, and the graph drains the queued notice at its
one step boundary (`inject_into_state`), appending a `<background-result
kind="process">` message with the exit code and a redacted, 4 000-char output
tail. The notice is delivered once and says not to poll; `process_output`
remains available for a live log. Notices are per-run and in memory, so a run
that has already ended is not resumed by one.

## Tests

`python/tests/test_terminal_*.py` — buffer, shell/runtimes/ports, process
(lifecycle, trees, signals, restart, crash, health), sessions (incl. real PTY),
store/recovery, resources/runaway limits, tools-through-the-registry, HTTP router,
and `test_terminal_e2e.py` (create →
serve → detect port → health-check → modify → restart → verify → stop → confirm
the port is released). Docker/SSH are unit-tested with `shutil.which`/`_docker_status`
stubbed, so they are covered without requiring a daemon or a remote host.
