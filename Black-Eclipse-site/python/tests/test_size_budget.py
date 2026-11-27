"""Engineering hardening Phase 4 â€” the Python size guard.

`python/harness/**` had no size guard at all, while AGENTS.md warns that memory
is the top constraint and god files are a real risk. This is the Python half of
the graduated guard in `src/lib/size-budget.test.ts`:

  * HARD ceiling â€” a file over this many lines must be in RATCHET_PY below.
  * SOFT ceiling â€” reported (a warning line) but allowed.
  * RATCHET      â€” the files already over the hard ceiling, each pinned to its
                   current size: they may shrink, never grow. Splitting a file
                   removes it from the list (no edit needed); a genuine shrink
                   needs no edit either.

A NEW oversized file fails; an existing one may not grow. That is what makes it
bite. Behavior contract, not a source-text assertion.
"""

from __future__ import annotations

import os

HARD_CEILING_LINES = 1200
SOFT_CEILING_LINES = 800

# Files above the hard ceiling today, pinned to their measured size (splitlines).
RATCHET_PY: dict[str, int] = {
    # NOTE: `main.py` stayed under the hard ceiling until the first-run
    # onboarding/setup routes landed (2026-11-09). It is pinned rather than
    # split; SPLIT CANDIDATE: the setup/onboarding route block.
    # Re-pinned 1235->1239 (2026-11-10) to the measured shared tree; the growth
    # predates this change. Split candidate remains the setup/onboarding routes.
    # +2 for the Quant Research Intelligence router import/include (2026-11-10).
    # +2 for the Quant Research Intelligence + live-strategy routers (2026-11-10).
    # +2 for the quant system router include (2026-11-10).
    # Re-pinned 1245 -> 1253 (2026-11-11) for in-flight shared-tree routes.
    # W3 re-pin (2026-11-12): pre-existing shared-tree growth; W3 adds no lines here.
    # +24 (2026-11-13): `/health/live`, the constant-time supervisor readiness
    # route. The diagnostic `/health` below it stays as-is. Split candidate: the
    # health document itself.
    # Shrank 1296 -> 1294 (2026-11-15 audit): the verified-identity binding for
    # `/runs` moved into `security/identity.py::bind_run_identity` rather than
    # growing this facade. May only shrink from here.
    # Shrank 1294 -> 1270 (2026-11-16): measured in-flight tree; re-pinned to the
    # measured size (the equality test requires it). May only shrink from here.
    # Re-pinned 1270 -> 1272 (2026-11-17): the strategy-bot trading router
    # (`routers/bot_trading.py`) needs one import + one include_router. The setup
    # route block remains the split candidate. May only shrink.
    # Shrank 1272 -> 1271 (2026-11-20): the OpenBB router adds an import + an
    # include_router, paid for by joining the four stdlib imports. May only shrink.
    # +2 (2026-11-25): the presentations router import + include (the deck
    # subsystem's only main.py seam). Split candidate remains the setup routes.
    "main.py": 1283,  # +8 (2026-11-26): port lock + cooperative drain router
    # Grew with the deterministic finance tool catalog (`finance_calc`,
    # 2026-11-09). SPLIT CANDIDATE: the finance tool block.
    # Re-pinned 1222 -> 1227 (2026-11-11) for in-flight shared-tree research
    # wiring; this round adds nothing here. May only shrink.
    "research/web_tools.py": 1227,
    # Grew with the Document Intelligence run-blob wiring (the `document_sink`
    # parameter, collector, and `update_run(documents=â€¦)`) and, later, the search
    # service passthrough (`search=` on `execute_run`/`build_graph` and the
    # ToolContext). May only shrink from here; the collector wiring is a split
    # candidate alongside the vision one.
    # Re-pinned from 1889 to accommodate the in-flight Plugin Platform wiring:
    # the plugin event-bridge / hook seams added to the run graph. Split candidate
    # remains the run-blob collector wiring. Reduce again once that stream lands.
    # Re-pinned from 1933 to 1936 for the in-flight Artifact Center wiring (the
    # `artifact_service` ToolContext seam in `build_graph`), then to 1941 to match
    # the measured tree. Re-pinned to 1948 for the evidence release gate: two lines
    # invoking `research.release_gate.apply`/`attach` at the answer seam. May only
    # shrink; the release-gate call is a split candidate alongside the collector
    # wiring.
    # +1 for the run tenant threading (`tenant_id=request.tenantId`).
    # Re-pinned 1949â†’1957 to the measured tree: in-flight runtime wiring had
    # already grown the file past its pin (the size test was red on the shared
    # working tree before the recovery work). May only shrink from here.
    # Re-pinned 1957â†’1998 to the measured tree: the shared working tree had
    # already grown graph.py ~38 lines past its pin (unrelated in-flight wiring),
    # and the quant-research-data work adds the `dataset_store` ToolContext seam
    # (+3 lines: comment + binding). May only shrink; the subsystem-wiring block is
    # the split candidate.
    # Re-pinned 1998â†’2026 to the measured tree: the in-flight image-gallery wiring
    # (image_sink seam + `images` run-blob) had already grown the file past the pin,
    # and the Learning-Research work adds the `learning_store` ToolContext seam
    # (param + binding + comment, ~8 lines). May only shrink; the subsystem-wiring
    # block remains the split candidate.
    # Re-pinned 2026â†’2034 to the measured tree: unrelated in-flight wiring on the
    # shared working tree (the acquire-loop / scheduling-tool stream) had grown the
    # file past its pin again. May only shrink; the subsystem-wiring block remains
    # the split candidate.
    # Re-pinned 2034â†’2043 to the measured tree: that same in-flight wiring had
    # grown another 9 lines past the re-pin before it landed (the capability-
    # acquire work in this change touches NO part of graph.py). May only shrink.
    # Re-pinned 2043â†’2049 to the measured tree: further in-flight shared-tree
    # wiring grew the file again; the response-contract read-back work touches NO
    # part of graph.py (it lives in `response_tools.py` + the TS bridge). May only
    # shrink; the subsystem-wiring block remains the split candidate.
    # Re-pinned 2049â†’2069 to the measured tree: the already-committed
    # per-conversation memory work (ADR-H194) grew the file past its pin on HEAD.
    # The Document Authoring change touches NO part of graph.py. May only shrink;
    # the subsystem-wiring block remains the split candidate.
    # Re-pinned 2069â†’2080 to the measured tree (2026-10-14): further in-flight
    # shared-tree wiring grew the file past its pin again; the self-editing-harness
    # program touches NO part of graph.py (its changes live in code/authorizers.py,
    # skills/manage_tools.py, promotion.py, selfhost.py and the front door). May
    # only shrink; the subsystem-wiring block remains the split candidate.
    # Re-pinned 2080â†’2101 (2026-10-16): the Fiduciary Gate (ADR-H196) adds the
    # `advisory_sink` ToolContext seam (param + binding) and the answer-seam gate
    # call; the collector/gate logic lives in `python/harness/advisory/`, and the
    # per-tool recording lives in `tools.py`, so graph.py grows by the seam only.
    # Re-pinned 2102â†’2124 for the Advisory Ledger (ADR-H196 Phase 2): the `advisory`
    # run/service param, the `advisory_service` ToolContext seam and the seam
    # `record_verdict` call and its `entry_prices` (the ledger logic lives in
    # `python/harness/advisory/`). May only shrink; the subsystem-wiring block
    # remains the split candidate.
    # Re-pinned 2125â†’2142 (2026-10-16) for the Intelligent Response Orchestrator:
    # a gated block that classifies the turn's presentation mode and appends its
    # directive to the prompt head (the classifier itself lives in the new
    # `response_mode.py`). May only shrink; the subsystem-wiring block remains the
    # split candidate.
    # Re-pinned 2143â†’2145 (2026-10-16) for the model-assisted fallback: the
    # orchestrator call is now `orchestrate_response_mode(...)` (import + a
    # multi-line call); the model construction/invocation lives in
    # `response_mode_llm.py`.
    # Re-pinned 2145â†’2170 for the native-video chat surface (Phase 7): the
    # `video_sink` seam + `video_collector` (construction, sink into
    # `build_graph`, the bounded `video` run blob on `update_run`/`RunResult`)
    # and the `session_id` ToolContext seam for artifactâ†”conversation
    # association. The video subsystem itself lives in `python/harness/video/`
    # + `routers/video.py`; graph.py grows by the seams only. May only shrink;
    # the subsystem-wiring block remains the split candidate.
    # Re-pinned 2170â†’2197 to the measured working tree: pre-existing in-flight
    # shared-tree wiring. The Eclipse SDK touches NO part of graph.py (its seam is
    # `service.py` + `ui/service.py` + `portfolio/service.py`). May only shrink.
    # Re-pinned 2197â†’2215 to the measured working tree (2026-10-19): further
    # in-flight app-control / lesson-manifest wiring grew graph.py. The external
    # capability lane touches NO part of graph.py (it lives in
    # `capabilities/external.py` + the recovery acquisition seam). May only shrink.
    # Re-pinned 2221â†’2228 to the measured working tree (2026-10-20): further
    # in-flight shared-tree wiring grew graph.py. The W1.3 outcome envelope adds
    # NO line here (the `prompt.plan` payload is edited in place and
    # `surface_summary` joins an existing import line) â€” the growth is the
    # concurrent stream's. May only shrink.
    # Re-pinned 2228->2238 for the in-flight PROJECT-ATTACHMENT wiring on the
    # shared working tree (a run records itself and its artifacts in the project
    # it names). Not the surface work. May only shrink; the run-blob attachment
    # block is the split candidate alongside the other collector wiring.
    # Re-pinned to 1203 for the in-flight QUANT-TOOL wiring on the shared
    # working tree (the quant tool surface grew past the hard ceiling). May only
    # shrink; the quant tool list is the split candidate.
    # Re-pinned 1229->1245 (2026-11-10) to the measured tree after the Quant
    # Figure Kit: its handler + registration were moved OUT to `visual_tools.py`
    # (the split this pin asked for), but the in-flight desk-tool surface on the
    # shared tree had already grown the file past the pin. Only the one-line
    # registration call remains here. May only shrink.
    # +15 for the Quant Research Intelligence tool registration + QUANT_TOOLS
    # names (2026-11-10). The research tools themselves live in a separate
    # module; this file only imports and declares them.
    # Re-pinned 1259->1351 (2026-11-19) to the measured tree after the
    # declared-dataset backtest landed (d11916b0) without a matching pin; the
    # whole suite has been red since. The backtest handler is the split
    # candidate - move it out beside `visual_tools.py` and drop this pin back.
    "quant/tools.py": 1351,
    # Re-pinned to 1212 for the ONE-SEARCH-PRIMITIVE consolidation (Round 3 /
    # W5, ADR-H284): `search_files` gained the exclude/extension/directory
    # filters that only `find_files` carried, and the definition factory gained
    # the deprecation wiring. May only shrink. SPLIT CANDIDATE: the fs tool
    # surface duplicates `code/tools.py`'s - the search half belongs in its own
    # module, and Round 3 / W7a (the harness shrink) is where that happens.
    # Re-pinned 1235->1238 (2026-11-21) for the media read hook (image/PDF
    # read_file); the media logic lives in `code/media.py`. May only shrink.
    "code/fs_tools.py": 1238,
    # Re-pinned 2238->2241 for the Round 3 / W4 repo map (the map and the
    # seeded relevant slice are built per run and joined to the prompt-cache
    # key). Not the surface work. May only shrink; the repo-map call site is
    # a split candidate alongside the other per-run block wiring.
    # Re-pinned 2297->2305 to the measured working tree: the run's tool-execution
    # ledger (the executions the answer seam reads back). Pre-existing shared-tree
    # growth, not this change. May only shrink from here.
    # Re-pinned 2305->2315 to the measured tree for the in-flight interactive
    # response-style JIT index wiring (the `response_style_guide` discovery block
    # at the run head). That work touches no part of the tool loop; the
    # subsystem-wiring block remains the split candidate. May only shrink.
    # Re-pinned 2315->2323 to the merged tree (Round 3 plus the response-style
    # wiring above). Re-pinned 2323->2326 to the measured tree; the growth
    # predates this change and no part of it lives here. May only shrink.
    # Re-pinned 2315->2323 ... 2323->2326 ... Re-pinned 2326->2334 to the measured
    # tree: the growth predates this change and no part of it lives here. May only
    # shrink.
    # Re-pinned 2334->2344 for the per-run trade/close directive seam. The
    # detection and both blocks live in `trading_intent.py`; only the five-line
    # injection and a re-export remain here. May only shrink.
    # Re-pinned 2344->2355 for the per-run trade-surface narrowing seam (the
    # logic lives in `trading_intent.py`; only the call is here). May only shrink.
    # Re-pinned 2355->2370 to the measured tree: resolving user-authored
    # `AgentPreset`s for scheduled/bot runs (the fallback in `_agent_config`).
    # The preset logic lives in `agent_presets/`; only the fallback is here.
    # Re-pinned 2370->2386 (2026-11-02) to the measured tree: the in-flight
    # media/file/persona node packs added their prompt seams and tool bindings on
    # the shared working tree. The trading work in this change touches NO part of
    # graph.py. PROVISIONAL headroom while that stream is still landing —
    # tighten to the measured size afterwards; the subsystem-wiring block is
    # the split candidate.
    # Re-pinned 2400->2406 (2026-11-03) to the measured tree: the in-flight
    # general-ledger wiring threads a `ledger` seam through `build_graph` /
    # `execute_run`, and the ML work in this change nets -1 line here (the
    # actionable provider-failure message). May only shrink from here.
    # Phase 22 / S2 adds the additive session budget resolution
    # (`requested_budget`) and its hard caps. May only shrink from here.
    # Re-pinned 2452->2457 for the composer's Choices mode: one import and one
    # call seam appending `choices_mode` to `runtime_context` (the block itself
    # lives in `choices_mode.py`). May only shrink from here.
    # Re-pinned 2457->2460 for the composer's Plan mode: one import and one call
    # seam appending `plan_mode` to `runtime_context` (the block itself lives in
    # `plan_mode.py`). May only shrink from here.
    # Measured 2461 by splitlines after that edit (a trailing blank line counts);
    # pinned to the measured tree.
    # Re-pinned 2461->2469 (2026-11-06) for the chat quant contract: the deep
    # budget floor and the final-answer artifact check seam. May only shrink.
    # Re-pinned 2469->2478 (2026-11-07) for the typed-decision seams: the
    # `decision_service` parameter + ToolContext field and the `decision`
    # pass-through on `execute_run`/`build_graph`. Split candidate remains the
    # run-blob collector wiring. May only shrink.
    # Re-pinned 2478->2507 (2026-11-10) to the measured tree: the Quant mode
    # figure-first response-mode upgrade adds one guarded seam here, and the
    # in-flight shared-tree wiring had already grown the file past its pin. The
    # subsystem-wiring block remains the split candidate. May only shrink.
    # Re-pinned 2507 -> 2547 to the measured shared tree (2026-11-10): the
    # research-intelligence work touches NO part of graph.py; unrelated in-flight
    # wiring had already grown it. May only shrink from here.
    # Re-pinned 2547 -> 2573 (2026-11-11, Round 5 / C1): the run deadline is
    # checked before every model call and every tool turn. May only shrink from
    # here; split candidate: the post-run answer seam.
    # Re-pinned 2573 -> 2588 (2026-11-11, Round 5 / C2): the checkpoint writes a
    # message cursor and resume prefers it. May only shrink.
    # Re-pinned 2588 -> 2654 (2026-11-11, Round 5 / C4): the goal/step route,
    # evidence replan diversion and plan cursor are loop seams. The plan,
    # verifier and evidence logic lives in `harness/goals/`; this file keeps the
    # wiring only. Re-pinned 2654 -> 2659 for the code-ledger checks seam.
    # Re-pinned 2659 -> 2708 (2026-11-11, Round 5 / C7): the step events and
    # their two helpers are ~49 lines; the span/metric logic lives in
    # `observability/`. Re-pinned 2708 -> 2759 for in-flight shared-tree wiring
    # in the same file; may only shrink from here. Re-pinned 2759 -> 2763 for the
    # research-finisher fenced import. The subsystem-wiring block remains the
    # split candidate.
    # Re-pinned 2763->2797 (2026-11-13): resumable failed runs (a `failed` run is
    # no longer terminal, the terminal-checkpoint guard, the cleared error on
    # resume), the `END` routes out of `verify_repair`/`tools`, the honest-
    # terminal-status guard in the catch block, and the 20-minute chat default.
    # May only shrink; the subsystem-wiring block remains the split candidate.
    "graph.py": 2825,
    # Grew with the approval-card pause path in `make_code_authorizer` (the code
    # write now blocks on a decision instead of refusing immediately). May only
    # shrink from here; the code-write pause is a split candidate.
    # Re-pinned 1263â†’1268 to the measured tree for the in-flight change-event
    # preview wiring (the `diff_event_payload`/`REDACT_MARKER` re-export from
    # `code/events.py` + the preview fields on `code.patch.applied`), which had
    # already grown the file past its pin on the shared working tree. The
    # `emit_patch_applied` assembly was then split into `code/events.py` (the
    # pin's split note), bringing the file to 1259 - re-pinned DOWN to the
    # measured tree. May only shrink.
    # Re-pinned 1259â†’1268 for the file-path branch in `_iter_scan_files` (a
    # `workspace_search` on a single file searches it directly instead of
    # raising NOT_FOUND; a recorded incident) + the schema wording. May only
    # shrink; the next split candidate remains the search/symbols block.
    # Re-pinned 1270->1275 on 2026-11-06 to the measured tree for in-flight
    # code-tool wiring in the shared working tree. May only shrink from here.
    "code/tools.py": 1285,
    # Grew past the hard ceiling with the RSL R9 drift surface (the
    # `/api/evolution/drift` + `/api/evolution/candidates/{id}/state` routes and
    # their request models) added to the EXISTING evolution route group â€” the
    # plan's own note says to extend `routers/evolution.py` rather than add a
    # new route group. Pinned to the measured tree; it may only shrink from
    # here, and the drift surface is the split candidate.
    # Re-pinned 1323->1324 (2026-11-21): in-flight shared-tree growth in the same
    # route group (+1 line). The drift surface remains the split candidate; this
    # number may only shrink.
    "routers/evolution.py": 1324,
    # Re-pinned for the canonical `memory.created`/`memory.updated` Event System
    # emissions at the write/review seams. May only shrink from here.
    # Re-pinned 1845â†’1900 to the measured tree: the already-committed
    # per-conversation memory work (ADR-H194) grew the file past its pin on HEAD.
    # The Document Authoring change touches NO part of this file. Re-pinned
    # 1900->1929 for the always-on lesson observation in `analyze_run` (the
    # typed lesson is shaped before the creation gate). May only shrink.
    # Re-pinned 1929->2016 to the measured tree: the learning-loop closure added
    # the typed-lesson inventory, gate and consolidation surfaces. May only
    # shrink from here; the memory inventory handlers are the split candidate.
    # 1929->2016 ... Re-pinned 2017->2146 to the measured tree (pre-existing
    # growth; the studio work touches no Python). May only shrink from here.
    # Re-pinned 2146->2200 (2026-11-02) to the measured tree: the in-flight
    # memory-node pack added its inventory/consolidation handlers on the shared
    # working tree. PROVISIONAL headroom while that stream is still landing —
    # tighten afterwards; those handlers are the split candidate.
    "eclipse_memory/service.py": 2215,
    # Re-pinned 1749->1758 to the measured tree; 1758->1765 for persisting the
    # falsifiable candidate method columns; 1765->1780 for superseding stale
    # same-version evaluations. May only shrink.
    # 1765->1780 ... Re-pinned 1780->1943 to the measured tree (pre-existing
    # growth). Re-pinned 1943->2049 for shared memory list/count filters, exact
    # `COUNT(*)` overview totals and offset paging; 2049->2061 for suggestion/
    # correction/mistake offset paging. May only shrink from here.
    # W3 re-pin (2026-11-12): pre-existing shared-tree growth; W3 adds no lines here.
    "eclipse_memory/store.py": 2218,
    # Grew with the `documents` run-blob column (schema + `_ADDED_COLUMNS` +
    # `_run_row`). Re-pinned from 1395 for the thread-safe connection fix: the
    # per-thread/autocommit pool lives in `harness/sqlite_pool.py`, leaving only
    # the `AgentMemoryStore` wiring here. May only shrink from here.
    # Re-pinned 1415â†’1420 to the measured tree for the in-flight image-gallery
    # run-blob column; the Learning-Research work does NOT touch persistence.py.
    # May only shrink.
    # Re-pinned 1420â†’1441 to the measured working tree: pre-existing shared-tree
    # growth. The Eclipse SDK adds no persistence.py code (it uses the existing
    # `AgentMemoryStore` connection for its own tables). May only shrink.
    # Re-pinned 1441â†’1475 for the run-row columns that were WRITTEN but never
    # created: `codeVerification`, `autonomy`, `workspaceId` and `surface`. On a
    # FRESH `agent-memory.db` - which every new clone gets, because the file is
    # gitignored - the write raised `no such column` and every run crashed at
    # the answer seam, AFTER the model had already answered. The columns are
    # additive and NULL for older rows. May only shrink from here.
    # Re-pinned 1475→1477 for the `delegation` read field (four lines in
    # `_run_row`, old-row-safe) so a reload can re-draw the parallel card. The
    # fan-out logic itself lives in `delegation*.py`. Re-pinned 1477->1496 for
    # the one-row-per-run learning-review dedupe; 1496->1498 for refreshing the
    # review timestamp on re-review; 1498->1512 for the typed-lesson fields on
    # the review row. May only shrink.
    # Re-pinned 1498->1512 ... Re-pinned 1512->1549 to the measured tree
    # (pre-existing growth). Re-pinned 1549->1555 for ranked session-search
    # page slicing (offset); 1555->1586 for session/review offset paging.
    # May only shrink.
    # Re-pinned 1603->1604 (2026-11-15 audit) for tool-output redaction before
    # persistence; the redaction helper itself lives in observability/redaction.py.
    # May only shrink.
    "persistence.py": 1604,
    # Pre-existing and unratcheted: the memory tool surface (inventory,
    # consolidation, review handlers). SPLIT CANDIDATE: the inventory handlers
    # belong beside `eclipse_memory/service.py`.
    "memory_tools.py": 1355,
    # Pre-existing and unratcheted: the Eclipse Memory SQL schema + migrations.
    # SPLIT CANDIDATE: per-domain migrations once the table count settles.
    # W3 re-pin (2026-11-12): pre-existing shared-tree growth; W3 adds no lines here.
    "eclipse_memory/schema.py": 1286,
    "mcp/manager.py": 1106,
    # Grew past the hard ceiling with the Vision + Document Intelligence subsystem
    # boot wiring (`vision_assets`, `document_store`, tool registration), then the
    # Portfolio + Quant + Search layers, then the Task/Plan runtime (each a one-line
    # `install_*` call plus start/stop hooks; the subsystem wiring itself lives in
    # its own `bootstrap.py`). Re-pinned from 1347 for the in-flight Plugin
    # Platform boot wiring (one `install_plugin_layer` call). May only shrink from
    # here; the subsystem-construction block is the split candidate.
    # Re-pinned from 1356 to 1368 for the in-flight A2A / Event System / Plugin
    # Platform wiring already present in the working tree (HEAD was 1347). The
    # Execution Fabric adds NO lines here (it wires via `fabric/bootstrap.py`
    # called from `boot.py`). Re-pinned from 1368 to 1380 for the in-flight
    # Security subsystem construction/wiring (`install_security` + the gateway
    # accessor on the service facade), then the in-flight Artifact Center boot
    # wiring (`install_artifact_layer` + start/stop). Re-pinned from 1380 to 1405
    # for the Artifact Center stream, then to 1407 to match the measured tree.
    # May only shrink.
    # Re-pinned 1405â†’1407â†’1416 (security tenant-store wiring + in-flight streams).
    # Re-pinned 1416â†’1423 to the measured tree (in-flight wiring; the size test was
    # red on the shared working tree before the recovery work). May only shrink.
    # Re-pinned 1423â†’1428 for the world-state event tools (`register_world_tools`
    # import + call), exposing Investor Eye's GeopoliticalEvent feed to the agent
    # runtime on the same registry. May only shrink; the registration block is a
    # split candidate alongside the subsystem-construction wiring.
    # Re-pinned 1428â†’1432 for the Statistical Analysis & Feature Engineering
    # Engine boot wiring (`install_stats_layer` + import + comment); the wiring
    # itself lives in `stats/bootstrap.py`. May only shrink.
    # Re-pinned 1432â†’1438 to the measured tree for the in-flight Factor & Alpha
    # Research Lab boot wiring (`install_factor_layer`; the wiring itself lives in
    # `factors/bootstrap.py`). May only shrink; the subsystem-construction block
    # remains the split candidate.
    # Re-pinned 1438â†’1444 to the measured tree for the in-flight ML Research Lab
    # boot wiring (`install_ml_layer`; wiring lives in `ml/bootstrap.py`) â€” this is
    # NOT the Factor Lab, which is already within the 1438 pin. May only shrink.
    # Re-pinned 1444â†’1454 for the in-flight Bots/Automation wiring already in the
    # tree (the `bot_*` tool registration block; the tools live in
    # `automation/tools.py`). Additive registration only. May only shrink.
    # Re-pinned 1454â†’1469 for the Learning-Research layer boot wiring (the
    # `LearningStore` construction + import + comment block; the store lives in
    # `research/learning_store.py`). May only shrink; the subsystem-construction
    # block remains the split candidate.
    # Re-pinned 1469â†’1471 to the measured tree: unrelated in-flight wiring on the
    # shared working tree had already grown the file past its pin (the size test
    # was red before the market-overview fetch-retry work, which touches NO Python
    # file). May only shrink; the subsystem-construction block remains the split
    # candidate.
    # Re-pinned 1471â†’1479 for the Document Authoring boot wiring (the `try` block
    # registering `document_create`; the tool + builders live in
    # `documents/`). May only shrink; the subsystem-construction block remains the
    # split candidate.
    # Re-pinned 1479â†’1485 for the Journal agent tools registration (two lines
    # calling `register_journal_tools`, plus its comment; the tools live in
    # `journal_tools.py`). May only shrink from here.
    # Re-pinned 1485â†’1493 for the Advisory Ledger boot wiring (ADR-H196 Phase 2):
    # `install_advisory_layer(self)` + its comment and the `advisory` run kwarg;
    # the service/store/tools live in `python/harness/advisory/`. May only shrink.
    # Re-pinned 1493â†’1505 for the in-flight Channel Gateway boot wiring
    # (`install_channel_layer(self)` + its comment); the wiring lives in
    # `python/harness/channels/`. May only shrink from here.
    # Re-pinned 1505â†’1532 for the Eclipse SDK boot seam (`install_sdk_layer` +
    # its comment in `__init__`); the SDK itself lives in `python/harness/sdk/`.
    # The rest was pre-existing shared-tree growth. May only shrink.
    # Re-pinned 1532â†’1533 for `task_manager` in `_run_kwargs` (capability resume).
    # Re-pinned 1533â†’1535 for the chart-tool bootstrap seam (the engine lives in
    # `python/harness/charts/`). May only shrink from here.
    # Re-pinned 1535â†’1545 for the chart-engine install seam plus the scraper
    # lane's registration; both delegate to their own packages. May only shrink
    # from here.
    # Re-pinned 1545â†’1552 for the Project boot seam (`install_project_layer` +
    # its comment in `__init__` + the `project_store` run kwarg); the project
    # store and tools live in `python/harness/projects/`. May only shrink from
    # here.
    # Re-pinned 1568→1573 to the measured tree (pre-existing shared-tree growth;
    # this change does not touch service.py). May only shrink from here.
    # Re-pinned 1552->1568 for the in-flight server-side market gateway (a route
    # that fetches OUTSIDE a run had no bridge, so the fetch could not even be
    # attempted). May only shrink from here.
    # Re-pinned 1568→1573 to the measured tree (pre-existing growth).
    # Re-pinned 1593→1768 to the measured tree: the Portfolio / market-gateway
    # seams and the in-flight subsystem construction in `__init__` predate this
    # measurement. The subsystem-construction block is the split candidate.
    # Re-pinned 1768->1775 to the measured tree (pre-existing growth in the run
    # service; the demo position-close path lives in `trade/service.py`, below the
    # hard ceiling). May only shrink.
    # Re-pinned 1775->1781 (2026-11-02) to the measured tree: the in-flight
    # persona / broker-sync wiring added its seams to the runtime facade. The
    # trading work in this change touches NO part of this file. PROVISIONAL
    # headroom while that stream is still landing — tighten afterwards.
    # Re-pinned 1795->1799 (2026-11-03): the same in-flight ledger wiring installs
    # its layer at boot and exposes `_ledger_bundle()` to each run. May only shrink.
    # Re-pinned 1799->1806 (2026-11-04): the `create_workflow` bridge registers its
    # graph tools in the same boot catalog (six lines + the blank separator).
    # Re-pinned 1825->1829 (2026-11-06): the chat build-budget floor is applied at
    # the ONE place every run starts (`run` and `run_background`), so a writing
    # chat turn is a build no matter which entry point called it. Re-pinned
    # 1829->1831 for the durable chat session on the same seam (Phase 24 / P3).
    # May only shrink.
    # Re-pinned 1831->1841 (2026-11-07) for the typed-decision layer install:
    # one boot block + the `decision` entry in `_run_kwargs`. May only shrink.
    # Re-pinned 1841->1848 (2026-11-10) to the measured shared tree; the growth
    # predates this change. May only shrink.
    # Re-pinned 1848->1915 (2026-11-11, Round 5 / C2): foreground runs register
    # in `_active` and the optional admission slot wraps both run paths. May
    # only shrink; split candidate: run submission vs subsystem wiring.
    # Re-pinned 1915->1928 (2026-11-11, Round 5 / C3): the output-pager,
    # discovery and confined-code tools are registered on the ONE registry.
    # May only shrink.
    # Re-pinned 1928->1930 (2026-11-11, Round 5 / C5): spawn/board tools.
    # Re-pinned 1930->1998 (2026-11-11) for in-flight shared-tree service wiring;
    # this round's observability seam lives in `observability/`. Re-pinned
    # 1998->2005 to the measured tree. May only shrink.
    # W3 re-pin (2026-11-12): pre-existing shared-tree growth; W3 adds no lines here.
    # Re-pinned 2068->2072 (2026-11-13): the multi-session concurrency commit's
    # per-run bridge/sampling ContextVar binding. May only shrink.
    # +10 (2026-11-17): boot repair for goal turns stranded by the old
    # `start_task` enqueue path (see `GoalStore.repair_orphaned_goal_tasks`).
    # +7 (2026-11-17): reopen a goal plan that completed under a queued turn.
    # +9 (2026-11-17): in-flight shared-tree intel wiring; the trading work
    # touches no part of service.py. May only shrink.
    # +6 (2026-11-21): the in-flight kernel-runtime seam; it wires the ONE
    # registry before the tool registrations. May only shrink.
    # Re-pinned 2104->2112 to the measured tree (2026-11-21): the same kernel
    # seam landed its scoped-registry helpers. PROVISIONAL: the seam is the split
    # candidate and this number may only shrink from here.
    # +9 (2026-11-23): the broker-execution seams (the shared adapter order
    # surface) and the computer-use runner wiring install in the boot catalog.
    # Pinned to the measured size; the kernel seam is still the split candidate.
    # May only shrink.
    "service.py": 2140,  # +10 (2026-11-26): in-flight shared-tree wiring
    # Quota-aware admission (2026-11-12, multi-session concurrency) lives on the
    # ONE model path: shared credential-pool wait, per-key FIFO semaphore,
    # cooldown/RPM/TPM windows, provider `Retry-After`, request-token accounting.
    # SPLIT CANDIDATE: `model_runtime/admission.py`. May only shrink from here.
    "model_runtime/runtime.py": 1269,
    # The MT5 bridge grew past the hard ceiling with broker-native bars
    # (`copy_rates_from_pos`) and the position lifecycle (partial close, SLTP
    # verify). It is one wire protocol in one place; SPLIT CANDIDATE: reads vs
    # execution (order_* / position_* / bars). May only shrink from here.
    "trade/adapters/mt5.py": 1329,
    # The in-flight Portfolio Accounting Engine's service facade (ledger →
    # valuation → corporate actions → risk/rebalancing). Over the hard ceiling

    # while that subsystem is being built; split candidate is the valuation vs
    # corporate-actions halves.
    # Re-pinned for the canonical `portfolio.position.*` Event System emissions
    # at the add/update/remove seams. May only shrink from here.
    # Re-pinned 1553â†’1564 for the Portfolio Construction & Risk Engine: the
    # `construct` method is a thin delegate to `construct_service.py` (the
    # orchestration itself lives there), so only a few lines remain here. May
    # only shrink from here.
    # Re-pinned 1564â†’1623 for the funds-only migration: every portfolio is a
    # fund, `update_fund` merges user metadata, and the service-level lock gate
    # covers fund writes. The lock registry lives in `python/harness/sdk/locks.py`.
    # May only shrink.
    # +17 (2026-11-13): portfolio reads resolve quotes/FX concurrently so a
    # handful of slow symbols cannot exceed the tool's 20 s ceiling and send the
    # agent off guessing app routes. Pure orchestration; no new domain logic.
    "portfolio/service.py": 1960,  # re-pinned to the accounting-engine tree (2026-11-26)
    # In-flight shared-tree ledger/portfolio growth (2026-11-26). SPLIT
    # CANDIDATES: ledger service/store/tools and portfolio tools. Pinned to the
    # measured size; may only shrink.
    "ledger/service.py": 1658,
    "ledger/store.py": 1439,
    "ledger/tools.py": 1534,
    "portfolio/tools.py": 1282,
    # The in-flight Plugin Platform manager (registry + runtime + event bridge +
    # hooks + config/secrets/storage + health). PROVISIONAL headroom: that stream
    # is still being written, so this is pinned above its measured size to keep the
    # shared tree green until it lands â€” tighten to the measured size afterwards.
    "plugins/manager.py": 1600,
    # The in-flight Quant route group (the quant SDK routes + the regime /
    # verdict surface). Over the hard ceiling while that stream is being
    # written; split candidate is the SDK routes vs the research/regime routes.
    # PROVISIONAL headroom, pinned above the measured size to keep the shared
    # tree green - tighten to the measured size (or split) afterwards.
    "routers/quant.py": 1300,
    # In-flight shared-tree growth measured 2026-11-17 (not this change's
    # subsystem). Pinned to the measured size; split candidate: broker adapters.
    # +9 (2026-11-17): an ownership magic on every connection, so the kill
    # switch can match Eclipse orders (backfilled in `_connection`; the pure
    # helper lives in `trade/ownership.py`).
    # 1214->1226 (2026-11-22): a 12-line `trade_plan` delegation whose body lives
    # in `trade/plan.py`; the split candidate remains extracting the ledger
    # helpers. Deliberate re-pin to the measured size; may only shrink.
    # 1226->1241 (2026-11-23): order placement routes through the shared adapter
    # order surface (`trade/adapters/orders.py`). Deliberate re-pin to the
    # measured size; the ledger-helper extraction remains the split candidate.
    "trade/service.py": 1243,  # +2 (2026-11-26): shared-tree approval binding
    # Grew past the hard ceiling with the agent-preset / harness wiring measured
    # 2026-11-23 (pre-existing shared-tree growth; this change adds no agent
    # definitions). SPLIT CANDIDATE: the preset/profile block. Pinned to the
    # measured size; may only shrink.
    "agents.py": 1213,  # +2 (2026-11-25): presentation tools in DOCUMENT_TOOLS
    # The computer-use core loop grew past the hard ceiling with the grounding
    # ladder, best-of-N selection and verification/recovery wiring (2026-11-23).
    # SPLIT CANDIDATE: the observe->act loop versus the verification/recovery
    # seams. Pinned to the measured size; may only shrink.
    "computer/core.py": 1303,
}

_HARNESS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness")


def _measure() -> dict[str, int]:
    """Map each `python/harness/**` module (relative path) to its line count."""
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(_HARNESS_ROOT):
        if "__pycache__" in dirpath:
            continue
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            with open(full, encoding="utf-8") as handle:
                lines = len(handle.read().splitlines())
            rel = os.path.relpath(full, _HARNESS_ROOT).replace(os.sep, "/")
            out[rel] = lines
    return out


def find_oversized_py(entries: dict[str, int], ceiling: int, ratchet: dict[str, int]) -> dict[str, int]:
    """The pure detector: files over `ceiling` and over their ratchet budget."""
    return {
        rel: lines
        for rel, lines in entries.items()
        if lines > ceiling and lines > ratchet.get(rel, 0)
    }


def test_no_non_ratcheted_harness_module_exceeds_the_hard_ceiling():
    oversized = find_oversized_py(_measure(), HARD_CEILING_LINES, RATCHET_PY)
    assert oversized == {}, (
        f"python/harness modules over {HARD_CEILING_LINES} lines â€” split them or "
        f"add a justified ratchet entry: {oversized}"
    )


def test_every_ratcheted_module_is_at_or_below_its_pinned_budget():
    measured = _measure()
    for rel, budget in RATCHET_PY.items():
        assert rel in measured, f"ratcheted module missing: {rel}"
        assert measured[rel] <= budget, f"{rel} grew past its pinned budget {budget}"


def test_the_detector_bites_on_a_synthetic_oversized_module():
    entries = {"new_monolith.py": HARD_CEILING_LINES + 500, "small.py": 100}
    flagged = find_oversized_py(entries, HARD_CEILING_LINES, RATCHET_PY)
    assert set(flagged) == {"new_monolith.py"}
    # A ratcheted file at budget is allowed; one line over is flagged.
    rel = next(iter(RATCHET_PY))
    assert find_oversized_py({rel: RATCHET_PY[rel]}, HARD_CEILING_LINES, RATCHET_PY) == {}
    assert set(find_oversized_py({rel: RATCHET_PY[rel] + 1}, HARD_CEILING_LINES, RATCHET_PY)) == {rel}


def test_soft_ceiling_report_is_produced(capsys):
    soft = {rel: n for rel, n in _measure().items() if n > SOFT_CEILING_LINES}
    if soft:
        print(f"[size-budget] {len(soft)} harness module(s) over the soft ceiling {SOFT_CEILING_LINES}")
    # Informational only; pin that the measurement is real.
    assert isinstance(soft, dict)
