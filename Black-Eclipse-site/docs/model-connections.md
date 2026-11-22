---
title: Model connections — how an LLM provider is connected
status: Current
last-verified: 2026-11-21
---

# Model connections

**Who this is for:** anyone changing the AI-provider UI, the provider vault, the
model runtime, or the onboarding AI step.

## Why this exists

Before this module, connecting a provider had three hidden ordering rules:

1. choose a provider,
2. save its key in the vault,
3. choose the provider **again** so the selection was persisted.

Step 1 was rejected by `setLLMConfig` whenever the key was not stored yet, so the
UI changed but the server did not. Step 3 only happened if the operator thought
to press **Use** after saving the key. The result was a screen that said
"Connected" while chat ran on the previous provider. On top of that, every test
used `Authorization: Bearer`, so a valid Anthropic or Gemini key was reported as
rejected.

The connection module removes the ordering problem and uses each provider's real
auth style.

## The one registry

`src/lib/providers/registry.ts` is the single source of truth for the providers
the UI can connect. Each descriptor declares:

| Field | Meaning |
|---|---|
| `id` | Matches `python/harness/models.py` `PROVIDER_CATALOG` and `LLMConfig.provider`. |
| `protocol` | `openai` (chat-completions), `anthropic` (`/v1/messages`), `google` (`generateContent`). |
| `authStyle` | `bearer`, `x-api-key`, `x-goog-api-key`, or `none`. Test/probe code must use it. |
| `baseUrl` / `model` | Safe defaults. Kept stable so an existing saved config still matches after an upgrade. |
| `envVar` | Vault entry for the key. |
| `keyless` / `local` | No credential needed / runs on this machine. |
| `keyPrefixes` | Unambiguous prefixes for paste-detection. A shared `sk-` never guesses. |

`src/lib/agent/presets.ts` derives `LLM_PRESETS` from the registry; it is no
longer a second table. `src/lib/providers/registry.test.ts` fails if a UI
provider has no row in the Python catalog.

**Adding a provider:** add one descriptor, make sure the id exists in
`PROVIDER_CATALOG` (and, when it needs a vault entry, in `PROVIDER_KEYS`), then
add it to the connect form automatically. No component change is required.

## Connection lifecycle

```
paste key ──▶ detect provider ──▶ probe (correct auth) ──▶ discover models
                                                              │
                                       ┌──────────────────────┘
                                       ▼
                          save key in vault + activate config
```

Server pieces:

- `src/lib/agent/connection.ts`
  - `probeModels` — lists models using the provider's protocol and auth.
  - `probeChat` — one minimal real request (retries once without probe-only
    parameters such as `max_tokens`/`temperature` when a reasoning model rejects
    them).
  - `testConnection` — models + chat; never persists. When the operator gave no
    model, the probe prefers the provider's declared default if the live list
    contains it, then the first chat-like id (never an embedding/audio/image
    model), then the declared default.
  - `connectProvider` — **atomic**: a failed probe writes nothing. On success it
    stores the key under the same env var `getLLMConfig` reads, then persists the
    active provider/base URL/model, then records the models the probe found into
    the durable snapshot (`lib/agent/discovered-models`) so the composer and the
    Settings picker can show the provider's real catalog immediately. A `custom`
    endpoint is marked `keyless` because a key is optional, but a key the
    operator pasted is still stored (under `CUSTOM_LLM_API_KEY`).
  - `setActiveModel` — switch models for a connected provider.
- Routes:
  - `POST /api/llm/connect` — `{ providerId, connectionId?, name?, baseUrl?, model?, apiKey?, authStyle?, apiKeyHeader?, apiKeyPrefix? }`
  - `DELETE /api/llm/connect?providerId=…` — remove a fixed provider key + deactivate when active
  - `POST /api/llm/test` — probe a draft (nothing persisted)
  - `POST /api/llm/active` — `{ providerId, model, baseUrl?, connectionId? }`
  - `GET /api/llm/connections` — named custom connections (never their keys)
  - `POST /api/llm/connections` — create/edit a named connection (`id` edits in place)
  - `DELETE /api/llm/connections?id=…` — remove the record and its vault key
  - `POST /api/agent/test` — compatibility wrapper used by Settings and setup check

## Named custom connections

A custom gateway is a first-class connection, not a single shared slot. Each
record in `SystemConfig` key `llm_custom_connections_v1` has an id, display name,
base URL, model, and auth style. Its key lives in the **encrypted vault** under
`CUSTOM_LLM_KEY_<ID>` — never in the record. The active `llm_config` stores only
`connectionId`, so:

- several gateways can coexist (company LiteLLM, local vLLM, an Azure-shaped
  proxy) without overwriting each other;
- switching connections does not re-enter a key;
- editing a connection keeps its id, so the key is not orphaned;
- deleting a connection removes its key and deactivates chat if it was active.

### Auth styles

| Style | Wire format |
|---|---|
| `bearer` | `Authorization: Bearer <key>` |
| `x-api-key` | `x-api-key: <key>` |
| `api-key` | `api-key: <key>` (Azure-style gateways) |
| `header` | `<apiKeyHeader>: <apiKeyPrefix><key>` |

The selected style is used by the **probe** (so Test/Connect verifies the real
handshake) and threaded to the Python runtime as `apiKeyHeader`/`apiKeyPrefix`
on `ModelSettings`. `python/harness/models.py` maps it to
`ChatOpenAI(default_headers=...)` with a placeholder `api_key`, so the real key
is not also sent as a Bearer token to a gateway that rejects it. An
`authorization` header is always treated as standard Bearer; a non-Bearer
Authorization prefix is not supported by ChatOpenAI's public API.

### OpenCode Go and Zen are separate connections

They are different gateways with different catalogs and limits. Go uses
`OPENCODE_GO_API_KEY`; Zen uses `OPENCODE_API_KEY`. Connecting one never marks
the other connected. A stored per-chat composer choice is dropped when its
provider has no key **or when it is a stale keyless local pick (Ollama/LM
Studio) and a different provider is now saved** — so the composer follows the
provider you actually connected instead of showing the other gateway's or the
local server's models.

### The composer follows the saved connection

`GET /api/harness/chat/models` is the composer's option source. It resolves the
ACTIVE provider's key through `llmKeyEnvVar` — the same helper `getLLMConfig`
uses — so an edited base URL or a named custom connection (whose key lives under
its own vault entry) is reported as available instead of "needs a key". It also
adds the active model and any models recorded by the connect probe/discovery.

The chat screen and the floating dock stay mounted while Settings is open, so a
mount-only fetch kept showing the boot-time catalog (often only keyless Ollama).
`lib/agent/llm-config-events.ts` broadcasts `eclipse:llm-changed`; every mounted
composer re-fetches. Connect/activate/disconnect events carry `resetChoice`, so
a stale per-chat pick is cleared and the newly connected provider becomes the
default. The hook also re-fetches when the window becomes visible again.

### Provider-required session headers

Some gateways require more than a key. **OpenCode Go** rejects requests without
a stable `x-opencode-session` id (HTTP 400 `MissingSessionID`) and asks clients to
identify themselves with a real User-Agent. A descriptor can declare
`sessionHeader`; the probe sends a fresh opaque id for the probe, and the runtime
sends the run's `sessionId` (falling back to a process-stable id). OpenCode also
publishes its model catalog one level above the chat base, so `modelsUrl` is
declared explicitly. Adding another gateway with the same requirement is one
descriptor field, not a code path.

Verified against a live OpenCode Go key: without the header the gateway returns
400; with it, `/models` returns the live catalog and `/chat/completions`
returns 200.

## Invariants

1. **A configuration may exist before its credential.** `setLLMConfig` persists
   provider/base URL/model even when no key resolves. Chat then refuses with
   `PROVIDER_NOT_CONFIGURED` and a clear message; it never fakes a reply.
2. **A failed probe never replaces a working connection.** The key and config are
   written only after success.
3. **A named provider keeps its own key.** Editing the base URL to a regional
   endpoint or proxy still resolves `OPENAI_API_KEY`, not the custom slot. A
   `custom` provider always uses `CUSTOM_LLM_API_KEY`.
4. **Test = the real protocol.** Anthropic gets `x-api-key` + `anthropic-version`;
   Gemini gets `x-goog-api-key`; OpenAI-compatible gets Bearer.
5. **Status is evidence-based.** "Connected" requires a configured key (or a
   keyless/local provider). A local server is not marked connected merely because
   a preset exists.
6. **A public model list is not proof of a key.** OpenRouter serves `/models`
   without authentication. A keyed provider is therefore rejected with
   `MISSING_KEY` **before any request** when the key is empty — otherwise the
   public list passes, the chat probe fails with `Missing Authentication header`,
   and the UI wrongly says "key rejected". `ok: true` always means the chat probe
   succeeded, never just that the model list was reachable.
7. **Secrets stay server-side.** The browser receives `hasApiKey` and a masked
   hint only. The probe uses the key in memory and never logs it.

## UI

`src/components/providers/provider-connect.tsx` is the one connect experience,
used by:

- Onboarding › AI & Chat (`src/components/setup/steps/ai-step.tsx`)
- Settings › AI (`src/components/screens/ai-provider-settings.tsx`)

It is paste-first: a key is detected from its prefix, the provider card is
selected, Connect tests and saves in one click, and the model list is discovered
and selectable. Errors are plain language with a "get a key" link. Advanced
fields (base URL, model) are collapsed and only shown on request.

Choosing **Custom / other** reveals the named-connection fields: connection
name, required base URL, auth style (Bearer / x-api-key / api-key / custom
header+prefix), optional model. Settings lists every named connection with
**Use / Edit / Delete**; editing keeps the id (and therefore the key) and
deleting removes both the record and its vault entry.

## Verification

- `src/lib/providers/registry.test.ts` — key detection, URL normalisation, auth
  headers, Python-catalog parity.
- `src/lib/agent/connection.test.ts` — Anthropic/Gemini auth, honest failures,
  atomic connect (no write on failure).
- `src/lib/settings.test.ts` — persist-before-key, named-provider key resolution.
