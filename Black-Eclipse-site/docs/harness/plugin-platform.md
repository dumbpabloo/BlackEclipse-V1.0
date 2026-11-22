---
title: Plugin Platform
status: Current
last-verified: 2026-11-11
---

# Plugin Platform

**Who this is for:** anyone installing, writing, or operating a BlackEclipse
plugin.

A plugin is a validated package that extends Eclipse with tools, skills, events,
hooks, commands, UI panels, workers, webhooks, config, secrets, storage and
provider descriptors. Plugins compose into the **existing** systems — the Tool
Registry, the Event Bus, the hook points, the Evidence engine and observability —
they do not fork them. This document explains the model, the security boundary,
and how a developer ships a plugin. Code reference:
`python/harness/plugins/`.

## Where it lives

| Concern | File |
|---|---|
| Manifest + validation | `python/harness/plugins/manifest.py`, `semver.py` |
| Permission vocabulary + enforcement | `python/harness/plugins/permissions.py` |
| Persistence (agent-memory.db) | `python/harness/plugins/store.py` |
| Lifecycle state machine | `python/harness/plugins/model.py` |
| Lifecycle + composition authority | `python/harness/plugins/manager.py` |
| Developer SDK | `python/harness/plugins/sdk.py` |
| Runtime context (permission-checked) | `python/harness/plugins/runtime.py` |
| Config / secrets / storage / HTTP | `config.py`, `secrets.py`, `storage.py`, `http.py` |
| Events / hooks | `events.py`, `hooks.py` |
| Contribution wiring | `registry.py` |
| Health / doctor / marketplace | `health.py`, `doctor.py`, `marketplace.py` |
| Agent tools / boot / routes / CLI | `tools.py`, `bootstrap.py`, `routers/plugins.py`, `harness/cli.py` (`plugin` group) |
| Reference plugins | `python/harness/plugins/bundled/` |

Installed plugins live under `.black-eclipse/plugins/<id>/`; their private data
under `.black-eclipse/plugins/data/<id>/`. The secret key is
`.black-eclipse/plugins/secret.key` (0600). None of this touches `jdds.db` or
`eclipse-memory.db`.

## The manifest contract

`plugin.yaml` (or `.yml`/`.json`) is validated in full before anything loads.
`extra="forbid"` means an unknown key is a rejection, not a silent ignore.
Validation covers: identifier shape, SemVer, SDK-range compatibility, the closed
permission vocabulary, contribution naming and duplication, dependency
declarations, config schemas, and host compatibility. Nothing is best-effort —
an unknown permission, an unparseable version or a duplicate contribution is a
hard failure (`MANIFEST_INVALID`).

`contributes` declares what the plugin provides, and the runtime **must agree**:
registration cross-checks the manifest against the object returned by the
entrypoint, and a declared-but-missing or provided-but-undeclared contribution is
`PLUGIN_CONTRIBUTION_MISMATCH`. A manifest cannot lie about what a plugin does.

## Lifecycle

```
DISCOVERED → VALIDATING → INSTALLED → ENABLED → LOADING → RUNNING
                      ↘ DISABLED   ↘ UPDATING → ROLLING_BACK
                      ↘ QUARANTINED / FAILED → INSTALLED (repair)
                      ↘ UNINSTALLED
```

Transitions are validated against the closed table in `model.py`; an illegal
transition is a `PLUGIN_ILLEGAL_TRANSITION`. Operations: `install` (atomic:
write to a temp path then swap), `enable`/`disable`, `update` (snapshot → swap →
validate → enable; **automatic rollback** on failure), `rollback`, `repair`,
`uninstall`. State is persisted in `agent-memory.db`, so an Eclipse restart
restores the same active plugin set (`bootstrap.start_plugins` → `start_enabled`);
a plugin that fails to reload becomes `FAILED`, never silently dropped.

## Permissions and the security model

A plugin declares permissions; the host grants exactly that set and **enforces it
at every call site** (`PluginContext`/`GrantedPermissions.require`). Declaration
alone never authorises. The vocabulary is closed and includes `network`,
`filesystem.read/write`, `process.spawn`, `terminal`, `browser`, `database`,
`secrets`, `events.subscribe/publish`, `task.create`, `workflow.execute`,
`agent.invoke`, `agents.contribute`, `skills.contribute`, `prompts.contribute`,
`models.contribute`, `memory.read/write`, `research.execute`, `ui`, `notifications`,
`webhooks`, `workers`.

- `ELEVATED_PERMISSIONS` (`process.spawn`, `terminal`, `browser`, `database`,
  `filesystem.write`) require an explicit **operator** enable; a non-operator
  enable is refused with `PLUGIN_PERMISSION_DENIED`.
- Secrets are encrypted at rest (Fernet key file, 0600), scoped per plugin, never
  listed by value, never logged and never placed in config/model context.
- A contributed tool with a non-read permission is routed through Eclipse's
  **existing effect authorizer** — the same approval flow as MCP and code actions.
- Package integrity: a SHA-256 checksum is recorded at install and re-verified on
  load; a mismatch quarantines the load (`PLUGIN_INTEGRITY_FAILED`) rather than
  running changed code. Package file paths are validated, so a package cannot
  write outside its own directory (`PLUGIN_PACKAGE_PATH_ESCAPE`).
- The HTTP client is the only network path: timeouts, retries/backoff, per-host
  rate limiting, a per-host circuit breaker (the shared `mcp.circuit`), request
  IDs, telemetry, and an always-on metadata-IP SSRF guard. An optional
  `allowedHosts` config restricts a plugin further.

## Developer SDK

```python
from harness.plugins.sdk import Plugin, tool, skill, hook, command, event_handler, worker, ui_panel

class Hello(Plugin):
    @tool(name="greet", description="Say hello",
          schema={"type": "object", "properties": {"name": {"type": "string"}}})
    async def greet(self, args, ctx):
        return f"{ctx.config.get('greeting', 'Hello')}, {args.get('name', 'world')}!"

plugin = Hello
```

The `ctx` object is the only surface: `ctx.config`, `ctx.secrets`, `ctx.storage`,
`ctx.http`, `ctx.events`, `ctx.logger`, `ctx.create_task`, `ctx.invoke_agent`,
`ctx.remember`/`ctx.recall`, `ctx.add_evidence`. Every method checks the matching
permission first. `@tool`, `@skill`, `@hook`, `@command`, `@event_handler`,
`@worker`, `@webhook`, `@ui_panel`, `@data_provider`, `@research_provider`,
`@agent_contribution`, `@workflow_contribution` declare contributions.

Scaffold and run:

```bash
eclipse plugin create my-plugin
eclipse plugin dev ./my-plugin        # validate + install + enable
eclipse plugin validate ./my-plugin
eclipse plugin package ./my-plugin
```

## Executable contributions (Phase 4, kernel on)

When `ECLIPSE_KERNEL=on`, these contribution kinds are mounted through the
kernel services instead of remaining descriptors:

- **agents** (`@agent_contribution`) - the handler returns an `AgentConfig` or a
  mapping; it registers in the kernel `AgentCatalog` and resolves by id.
- **skills** (`@skill`) - the handler returns the procedure text; it registers
  in the kernel `SkillService` and, when a `SkillRegistry` is attached, the live
  skill catalog.
- **prompt sections** (`@prompt_section`) - the handler returns text; it
  registers in the kernel `PromptService` under its declared `scope`.
- **research/data providers** (`@research_provider`/`@data_provider`) - the
  handler returns a provider object/descriptor; it registers in the kernel
  contribution catalog (`research.execute`).
- **workflows** (`@workflow_contribution`) - the handler returns a workflow
  object; it registers in the kernel contribution catalog (`workflow.execute`).
- **models** (`@model_provider`) - the handler returns a model/provider
  descriptor; it registers in the kernel contribution catalog
  (`models.contribute`).
- **UI panels** (`@ui_panel`) - the handler and descriptor register in the
  kernel contribution catalog (`ui`); the existing plugin panel renderer remains
  the UI surface.

Each contribution is permissioned, cross-checked against the manifest, and
returns an exact undo that runs on disable/uninstall. Executable contributions
require a **trusted in-process** plugin; a sandboxed (`community`/`untrusted`)
plugin may still contribute tools, commands, hooks, events, workers and UI.

At boot, the kernel contribution catalog is bound to the real consumers:
research providers to the search registry, data providers to the retrieval
provider registry, workflows to the workflow store (validated before save),
models to the model provider registry, and UI handlers to the UI service's
dynamic-panel surface. Disabling the plugin removes the same entries from those
consumers.

A manifest may also declare:

```yaml
provides:
  - key: demo.agent
    version: 1
requires:
  - key: tools
  - key: optional.service
    optional: true
```

`provides` must equal what the plugin's `provided_services()` returns;
`requires` is checked against the live kernel services before load. The
read-only `harness_inspect` tool exposes the live services, agents, tool/prompt
scopes, skills and provided keys to the agent.

## Integration points

- **Tools** register on the ONE `ToolRegistry` as `plug_<id>_<name>`; the general
  agent discovers them through `plugin_capabilities`/`plugin_invoke` and, when
  `allowPluginTools` is set, the `plug_`/`plugin_` allowlist expansion. Execution
  uses the shared registry (validation, timeout, effect approval).
- **Events** publish/subscribe through `PluginEventBridge`, which rides the ONE
  run bus (`harness.events.EventBus`) — the same bus the canonical Event System
  ingests, so plugin events are durable and observable without a second
  transport. A failing subscriber never fails the originating operation.
- **Hooks** run at `before_tool_call`/`after_tool_call` (wired in `graph.py`) and
  the full canonical hook set is available to the host; ordering, per-handler
  timeout, `continue`/`abort` policy and tracing are enforced by `HookRegistry`.
- **Config, secrets and storage** are namespaced per plugin and persisted.
- **Health/observability**: tool/event/hook/http failure counters, uptime,
  crashes/restarts, latency, plus `plugin.tool.started/completed/failed` events on
  the run trace.

## Operator surfaces

- Agent tools: `plugin_list`, `plugin_inspect`, `plugin_capabilities`,
  `plugin_health`, `plugin_invoke`, `plugin_run_command`.
- HTTP (operator-gated): `/plugins*` (list, available, search, capabilities,
  install, enable/disable, update/rollback/repair, uninstall, config, secrets,
  command, webhook, health, doctor, logs).
- CLI: `eclipse plugin list|available|search|capabilities|install|uninstall|
  enable|disable|update|rollback|repair|inspect|health|doctor|logs|config|
  validate|create|package|dev`.
- `/health` reports `plugins` (installed/enabled/tool counts).

## Sandboxing untrusted plugins

`community`/`untrusted` Python plugins run in a **separate OS process**
(`plugins/sandbox.py` host + `plugins/sandbox_runner.py` child) over a
newline-delimited JSON-RPC channel. `trusted`/`verified` plugins run in-process;
`HARNESS_PLUGIN_SANDBOX` forces/forbids sandboxing. The child proxies its context
operations (config, secrets, storage, HTTP, auth, logging, events, webhooks) back
to the host, so every permission check, the SSRF guard and the secret boundary
still apply. A plugin crash cannot crash Eclipse Core; per-call timeouts and
message-size limits bound a misbehaving child, and an optional
`HARNESS_PLUGIN_MAX_MEMORY_MB` sets a POSIX address-space limit. Lifecycle, tools,
commands, hooks, workers and UI all cross the boundary.

## Install signature and review (C6)

An install is a supply-chain decision. `plugins/signing.py` checks a `plugin.sig`
sidecar (or an explicit signature) covering the canonical manifest
(id/version/publisher/permissions) plus the package content digest; the sidecar
is excluded from that digest.

- **Ed25519 publisher signature** — `ed25519:<hex>`, verified against
  `HARNESS_PLUGIN_TRUSTED_KEYS` (comma-separated 32-byte hex public keys). The
  implementation is dependency-free RFC 8032 (`plugins/ed25519.py`).
- **Operator HMAC** — `hmac-sha256:<hex>` with `HARNESS_PLUGIN_SIGNING_KEY`.
- `HARNESS_PLUGIN_REQUIRE_SIGNATURE=1` refuses a missing/invalid signature; with
  no key material it fails closed as `PLUGIN_SIGNING_UNAVAILABLE`.
- `HARNESS_PLUGIN_REQUIRE_REVIEW=1` requires `install(..., operator=True)`.
- Bundled/trusted packages are exempt (`verification="official"`); install and
  update both enforce the gate and roll back on refusal.

## OAuth 2.0

The host owns the token lifecycle (`plugins/oauth.py`); a plugin only declares
its flow and reads a valid token via `await ctx.auth.token()`. Grants:
`client_credentials`, `device_code` (RFC 8628), and `authorization_code` with
PKCE (S256). Refresh and revoke are supported; tokens live in the encrypted
secret store. Operator surfaces: `POST /plugins/{id}/oauth/start|poll|complete|refresh`,
`DELETE /plugins/{id}/oauth`, `GET /plugins/{id}/auth`, and the CLI
`eclipse plugin auth|auth-status|auth-complete|auth-refresh|logout`.

## Outbound webhooks and streaming

A plugin may declare an `outbound` webhook and deliver through
`await ctx.webhooks.deliver(id, payload)` (permission `webhooks`): the host signs
the body (HMAC-SHA256 `X-Signature-256`), retries with backoff and dead-letters a
final failure (`GET /plugins/{id}/webhooks/deliveries`). Outbound targets are set
with `POST /plugins/{id}/webhooks/target` or `eclipse plugin webhook-target`.
Inbound webhooks were already HMAC-verified and replay-protected. The controlled
HTTP client also exposes a reconnecting **WebSocket** (`ctx.http.connect_ws`) and
SSE (`ctx.http.sse`).

## Hosted registry

`plugins/registry_client.py` loads a JSON index from an HTTP(S) URL, a local
directory, or an index file, searches it, downloads a package, verifies its
SHA-256, and extracts it with a zip-slip/symlink guard before installing.
Configure `HARNESS_PLUGIN_REGISTRY_URL` or pass a source. Operator surfaces:
`GET /plugins/registry`, `GET /plugins/registry/search`,
`POST /plugins/registry/install`, and CLI `registry-search` / `registry-install`.

## Plugin UI

A plugin contributes a UI panel whose handler returns a **declarative view spec**
(`markdown`, `keyvalue`, `table`, `list`, `status`) — never HTML or DOM. The
runtime exposes it at `GET /plugins/{id}/ui` and `GET /plugins/{id}/ui/{panel}`;
the `Plugins` screen (`src/components/screens/plugins/plugins-screen.tsx`) lists
installed plugins, shows health/contributions, and renders the view spec through
a fixed, safe renderer.

## Reference plugins

`bundled/hello-world` exercises every contribution surface. `bundled/{github,
slack,jira,postgres,binance,sec}` are real SDK plugins used as architectural
validation (network calls go through the controlled HTTP client; they install,
validate and enable in tests without network). `bundled/kernel-demo` exercises
the Phase-4 executable surfaces (agent + skill + prompt section + research/data
provider + workflow + model + UI panel, provides/requires) and is the regression
fixture for mount/unwind.

## Composition (planned kernel)

The plugin platform decides *what a plugin may contribute*. It does not yet
decide *how the harness itself is composed*: profiles, ordered bundles, patch
layers, per-session presets, service replacement and reversible hot reload are
specified in `docs/harness/plugin-kernel-plan.md` and have tested first slices
in `python/harness/kernel/`. The tools/prompt/agents services are wired behind
`ECLIPSE_KERNEL` (default off); composition rows and plugin contributions are
not live yet. Read that plan before adding another extension point; the target
is one composition model, not a second plugin mechanism.

## Testing

`python/tests/test_plugins_{manifest,lifecycle,sdk,security,reference,cli_api}.py`
cover manifest validation, the lifecycle (including restart persistence, update
and automatic rollback), all contribution surfaces, permission denial, integrity,
path traversal, encryption, reference-plugin enablement, the CLI and the routes.
`test_plugins_kernel_{manifest,contributions}.py` cover the Phase-4 executable
mount/unwind, permission gate, provides/requires and `harness_inspect`.
