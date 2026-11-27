---
title: Multi-session concurrency
status: Current
last-verified: 2026-11-26
owner: Agent runtime
scope: parallel chat conversations, per-run transport/model, workspace projection
---

# Multi-session concurrency

Eclipse supports **one live run per conversation, many conversations at once**.
Before this change the runtime could already execute concurrent runs, but the
chat client mounted a single `useAgentRun` and refused a second run while one
was active.

## Why it exists

- A demo (or real work) needs a second question answered while the first is
  still tool-looping.
- The runtime's run kernel is per-run (leases, event bus, `_active` registry),
  so concurrency was already safe at the run level.
- The client, not the runtime, was the bottleneck.

## Client ownership

| Concern | Owner | Invariant |
|---|---|---|
| Live streams | `src/lib/agent/run-manager.ts` | One `LiveRun` per `chatId`; `start()` for an already-active chat returns the existing turn |
| React selection | `src/lib/agent/use-agent-run.ts` | The hook selects one conversation; it does not own the stream and never aborts on unmount |
| Active-run recovery | `src/lib/agent/registry.ts` | `eclipse.activeRuns.v2` maps `chatId → { runId, at }` (v1 is migrated on read) |
| Queue ownership | `src/lib/chat/message-queue.ts` | Each `QueuedPrompt` carries its `chatId`; `use-chat-queue-drain.ts` drains the first prompt whose OWN chat is idle |
| Workspace projection | `src/lib/agent/run-focus.ts` | Only the focused conversation's run may bind/reset the single Agent Workspace |
| Sidebar badges | `runManager.subscribeLifecycle` | Start/finish notifications only — never one re-render per token |

The shell (`stock-chat.tsx`) passes a **per-run** `onTurn` sink. When a run
streams in a background conversation, its sink writes into that conversation's
transcript even if the user is looking at another one. The active chat's
`agentBusy` therefore does not block an idle conversation.

## Runtime ownership

The market gateway and MCP manager are process-wide singletons. Per-run values
used to be written onto them:

```python
market_gateway._bridge = bridge
mcp_manager.set_sampling_model_factory(factory)
```

Two concurrent runs overwrote each other. `python/harness/run_binding.py` puts
those values in `ContextVar`s instead:

- `bind_run(bridge=..., sampling_factory=...)` wraps the run body in both
  `service.run` and `service.run_background`.
- `market/gateway.py` and `market/streaming.py` read `current_bridge(fallback)`.
- `mcp/manager.py` reads `current_sampling_factory(fallback)` lazily per call.
- Child tasks inherit the context, so streams and tool workers keep the right
  transport/model.

`HARNESS_MAX_CONCURRENT_RUNS` remains the optional admission bound (default
`0` = unbounded). The client is the natural place for a per-user cap when one
is added.

## One runtime per port (and never kill a busy one)

A runtime executes runs in-process, so a second runtime on the same port/database
is not a scale-out — it is a race on SQLite plus a boot reconciliation that can
mistake the first runtime's live runs for orphans. Two guards now enforce this:

- **Port lock** — `python/harness/runtime_lock.py::enforce_instance_lock` takes
  an exclusive file lock keyed by the runtime port at `main.py` import time,
  **before** uvicorn runs the lifespan (enabled by the launcher's
  `BE_ENFORCE_SINGLE_RUNTIME=1`; a no-op in tests/embedded `TestClient` use). A
  rival process exits without touching the DB or registry.
- **Cooperative drain** — before clearing a sustained-silent port the front door
  calls `POST /runtime/drain` (`routers/runtime.py`): the runtime refuses new
  runs with 503 (`require_operator_accepting_runs`) and waits, bounded, for its
  active-run registry to empty. Only then is the process replaced, so a restart
  happens at an idle boundary instead of orphaning chats.
- **Supervisor restraint** — `src/lib/harness/runtime-supervision.ts`
  (`shouldClearSilentRuntime`) refuses to clear a listening runtime until it has
  missed `SILENT_MISSES_BEFORE_KILL` consecutive readiness probes across
  `SILENT_KILL_WINDOW_MS` (120 s), and `hasRunsToProtect` blocks replacement when
  `activeRuns` or `parkedRuns` (approval/clarification waits) is non-zero. A
  single slow tool or long model call can no longer cost every tab its run;
  observed 2026-11-26, two concurrent streams were killed mid-flight and later
  relabelled `RUN_STALLED` by the watchdog.

## Lease loss is terminal (never a silent orphan)

The lease heartbeat used to return `False` on ANY exception, so one >5 s SQLite
lock stopped the heartbeat thread permanently; the lease then expired and the
run was fenced mid-chat. Now:

- `RunLeaseManager.heartbeat_result()` distinguishes `"ok"`, `"fenced"` and
  `"error"`; only a fenced lease stops the thread, a transient error retries.
- `run_supervision.settle_lost_run()` writes a terminal `RUN_LEASE_LOST` row and
  a durable `run.failed` event when a worker discovers it was fenced — but only
  when no *other* live runtime owns the run, so a stale worker can never clobber
  the new owner.
- `run_supervision.guard_background_task()` wraps `service.run_background`: if
  the task raises or is cancelled outside `execute_run`, it writes an honest
  terminal state (`RUN_CRASHED` / `RUN_CANCELLED`), emits the terminal event and
  closes live streams.
- One `RunLeaseManager` is shared per store/process (schema DDL and the
  `RuntimeInstance` write are no longer paid on every chat start), and
  `RuntimeInstance.startedAt` is the process start, not the last run start.

## Silence is bounded (model and tools)

- `model_stream.ainvoke_streaming` bounds the gap BETWEEN streamed chunks
  (`HARNESS_MODEL_IDLE_S`, default 90 s). A provider that stops mid-answer now
  fails as `PROVIDER_TIMEOUT` and emits a durable `model.stalled` event instead
  of waiting for the watchdog.
- `tool_turn` gives every serial/non-parallel-safe tool call an outer
  `await_bounded` (batch and remaining-run budget), matching the parallel-safe
  path; a handler that ignores cancellation can no longer leave the run silent.
- The client treats 45 s without a frame as a dead stream, cancels the socket and
  recovers the durable run; the chat relay reconnects with `after=<last seq>`
  (Python's stream endpoint already supports resume).
- `run_activity` records EVERY event (including transient `model.delta`) in a
  process-local heartbeat map; the watchdog sweep adds fresh entries to its
  live set, so a long generation is not eligible for `RUN_STALLED` merely because
  deltas are not persisted. Terminal events forget the entry; stale entries are
  pruned hourly.
- Admission waits now reach the run trace: `ModelRuntime` reports
  `model.queued` / `model.rate_limited` through `model_runtime/run_events.py`
  (bound per run with a ContextVar), and the chat maps them to an explicit
  working-indicator note ("Waiting for provider capacity…"). A lost stream shows
  "Connection lost — reconnecting to the agent…" while recovery polls the run.

## Retention

The event store is append-only and grew to multi-GB. `python/harness/retention.py`
prunes the CHILD rows (`HarnessEvent`, `RunMessage`, `RunCheckpoint`,
`HarnessToolExecution`, `ToolExecutionIdempotency`, `RunLease`) of TERMINAL runs
older than a cutoff, then checkpoints the WAL. It never deletes `HarnessRun`
history and never touches `created`/`running` runs. It is a report by default:

```bash
npm run memory:prune                      # 30-day report (no writes)
npm run memory:prune -- --days 7 --apply  # prune + WAL checkpoint
npm run memory:prune -- --days 30 --apply --vacuum
```

## Provider quota admission

A provider's quota is real; Eclipse must not stampede it. The model path now
has one process-wide gate:

- **Shared credentials** — `model_runtime/credential_pool.get_credential_pool()`
  caches a pool per provider/key-set, so a 429 cooldown and key rotation are
  visible to every concurrent run. (`graph.py` and `gateway.py` no longer build a
  per-run pool.)
- **Async admission** — `RateLimitManager.acquire_slot()` uses a per-credential
  semaphore (FIFO) plus cooldown/RPM/TPM windows. `HARNESS_MODEL_CONCURRENCY`
  (default `2`; `0` = unbounded) is the per-provider/model/credential ceiling, so
  two configured keys get two independent slots.
- **Wait, don't fail** — `ModelRuntime.invoke` waits for the earliest credential
  reset / admission slot, emits `model.queued` (reason `credential_cooldown`,
  `rate_limit`, or `concurrency`), and honors a provider `Retry-After`.
  `HARNESS_MODEL_COOLDOWN_WAIT_S` (default `60`) bounds the wait; past it the run
  fails honestly with `RATE_LIMITED`.
- **Adaptive bound** — the default ceiling is conservative; operators can tune
  it per deployment. The gate lives in Python because automation, tasks, bots
  and delegation all reach the model through the same runtime.

## Cross-tab conversations

`src/lib/agent/tab-coordinator.ts` makes same-origin tabs coordinate:

- **One owner** — `withChatStartLock()` uses Web Locks
  (`eclipse-chat:<chatId>`, `ifAvailable`) to elect the tab that starts a run.
  A follower attaches to the same server run instead of starting a second one.
- **Shared record** — `eclipse.sharedRun.v3:<chatId>` in `localStorage` stores
  `{ runId, ownerTabId, at }`. One key per conversation (v1's shared map was a
  read-modify-write race: two tabs starting two chats could drop each other's
  record). The v1 map is still read for migration and cleared on removal.
- **Per-tab new chat** — an unsaved chat is keyed `<new>:<tabId>` in the run
  manager, so two tabs' fresh chats do not share a lock/record and attach to each
  other's run.
- **Per-tab rate limit** — the chat POST sends `x-be-client` (one id per tab) and
  `api-guard.clientKey()` combines it with the host, so one tab cannot exhaust
  another tab's chat budget.
- **Per-tab run indicator** — the desktop tab strip subscribes to
  `useRunningConversationIds()` and shows a pulsing dot on a Chat tab whose
  conversation still has a live run, so the single-projection Agent Workspace
  never makes background work look lost.
- **Live follower** — the owner broadcasts `run.started` / `run.snapshot`
  (throttled, plus a 5s heartbeat independent of rAF) / `run.done` on
  `BroadcastChannel('eclipse.chat.v1')`. A follower applies the same turn
  snapshot and polls `/api/harness/runs/{id}` as a fallback.
- **Follower actions** — cancel/approve/answer are plain HTTP calls by run id,
  so a follower can act without owning the stream. The per-chat queue treats a
  run owned by another tab as busy (`runManager.isBusy`).
- **Server truth** — `GET /sessions/{id}` now returns `activeRun` (newest
  non-terminal run for that `sessionId`), so a client that lost localStorage can
  still discover and attach to a live run.
- **Transcript merge** — `mergeConversations()` (client cache) and
  `writeTranscript()` (server copy) union messages by id instead of
  last-writer-wins. A terminal assistant turn beats a streaming placeholder.

## Durable prompt queue

`src/lib/chat/queue-store.ts` owns every conversation's waiting prompts:

- **Persistent** — `eclipse.promptQueues.v1` in `localStorage`; a queued prompt
  survives a reload or closing the tab. When storage is blocked, an in-memory
  book keeps the queue working for the session.
- **Cross-tab** — changes broadcast on `BroadcastChannel('eclipse.queue.v1')`
  and the `storage` event. `takeNextFor()` serializes dequeue with a per-chat
  Web Lock, so two tabs take different prompts instead of duplicating one.
- **Pause/resume** — each chat's queue has a `paused` flag. The queue panel's
  play/pause button flips it; a paused queue keeps accepting prompts but does
  not drain.
- **Boot resume** — the drain hook reads the persisted queue on mount and starts
  the first prompt whose conversation is idle and unpaused, so queued work
  resumes after a reload.
- **Per-chat visibility** — `useMessageQueue().items` spans conversations;
  the shell filters the panel to the active chat. Sending is explicit about the
  target chat, so a prompt can never drain into another conversation.

## Tests

- `python/tests/test_run_lease.py` — one live owner, fencing, transient heartbeat
  retries.
- `python/tests/test_run_supervision.py` — lease-loss/crash settlement, no
  overwrite of a live owner, one manager per store, single-owner port lock.
- `python/tests/test_run_activity.py` + `test_run_watchdog.py` — transient
  deltas keep a run out of RUN_STALLED.
- `python/tests/test_model_stream_idle.py` — a stream that stops after the first
  token fails on the chunk idle timeout and emits `model.stalled`.
- `python/tests/test_retention.py` — old terminal children pruned, live/recent
  runs and run history preserved.
- `python/tests/test_runtime_drain.py` — drain refuses admission and reports
  honestly; `test_model_run_events.py` — admission waits are durable per run.
- `src/lib/agent/run-manager.test.ts` / `events.test.ts` — waiting/reconnecting
  notices on the turn and their clearing on content/terminal frames.
- `src/lib/harness/runtime-supervision.test.ts` — a busy runtime is never killed
  on transient probe misses; parked approvals protect it.
- `src/lib/agent/tab-coordinator.test.ts` — per-conversation shared records
  survive concurrent tab writes.
- `src/lib/agent/run-focus.test.ts` — only the focused chat's run ingests.
- `src/lib/chat/message-queue.test.ts` — a prompt keeps its originating chat.
- `python/tests/test_run_binding.py` — two tasks keep their own bridge/model;
  MCP sampling refuses outside a run.
- `python/tests/test_execution_registry.py::test_concurrent_runs_keep_their_own_bridge`
  — two `service.run` calls keep their own callback transport.
- `python/tests/test_model_admission.py` — per-key concurrency is bounded, a
  `Retry-After` is honored, the wait is caller-bounded, and two runs share one
  credential cooldown.
- `src/lib/chat/chat-organization.test.ts` — cross-tab message union and the
  terminal-over-streaming rule.
- `src/lib/chat/queue-store.test.ts` — persistence, atomic dequeue, pause, and
  per-conversation clear.

## Known limits

- The gate bounds how Eclipse uses a provider quota; it cannot raise it. With
  every configured key cooling, a run waits up to the configured budget and then
  fails honestly with `RATE_LIMITED`.
- Web Locks / BroadcastChannel are per-origin. Multi-device coordination relies
  on the server-side `activeRun` lookup and would need the `ChatSession` claim
  (revision/owner) for a strict single-writer guarantee across devices.
- The Agent Workspace is a single projection by design. It follows the focused
  conversation; it does not merge two runs' tool activity.
