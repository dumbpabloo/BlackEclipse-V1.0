"""UI Service — the ONE authority for the UI Engineering runtime.

Frontend-agnostic: the routes, the agent tools and producers all call this
object. It composes three collaborators — the component catalog (what can be
composed), the engine (validation + atomic mutation) and the store (durable
state) — and adds the cross-cutting concerns every operation needs: optimistic
concurrency, an operation audit record, structured errors and best-effort
events. It owns no second connection and no second layout engine.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..persistence import new_id, now_iso
from .catalog import (
    CatalogError,
    PORTFOLIO_DATA_SOURCE,
    PORTFOLIO_VIEWS,
    RENDERABLE_COMPONENTS,
    UiCatalog,
    validate_component_config,
    validate_source,
)
from .engine import apply_ops as engine_apply_ops, diff_blueprints, validate_blueprint, widget_count
from .engineering import UiEngineering
from .mode import classify_request as _classify_request
from .model import (
    AddWidgetInput,
    AppSurface,
    BlueprintOwner,
    BlueprintScope,
    CatalogQuery,
    CreatePageInput,
    ListBlueprintsQuery,
    MAX_CONFIG_BYTES,
    MoveWidgetInput,
    ReorderWidgetInput,
    RestoreRevisionInput,
    ResizeWidgetInput,
    SaveLayoutInput,
    SectionRefInput,
    SetColumnCountInput,
    SetColumnWeightInput,
    Severity,
    UiBlueprint,
    UiComponentSpec,
    UiEventType,
    UiOperation,
    UpdateConfigInput,
    UpdatePageInput,
    WidgetRefInput,
    WorkspaceColumn,
    WorkspaceLayout,
    WorkspaceSection,
)


class UiError(Exception):
    def __init__(self, code: str, message: str, *, issues: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []


def _publish(event_type: str, payload: dict[str, Any]) -> None:
    try:
        from ..event_system.emit import publish_event  # noqa: PLC0415

        publish_event(event_type, payload, source="ui")
    except Exception:  # noqa: BLE001 - telemetry must never break a mutation
        pass


def _normalize_path(value: Optional[str]) -> str:
    """Normalise a page path for matching: trim, leading slash, no trailing
    slash, case-insensitive. ``None``/blank normalises to ``""`` so an unset
    path is never matched against a real request."""
    p = (value or "").strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1:
        p = p.rstrip("/")
    return p.lower()


def _is_dashboard_mirror(path: Optional[str]) -> bool:
    """Command Center mirrors are agent-readable board snapshots, not user pages."""
    return (path or "").strip().lower().startswith("/dashboard/")


def _empty_layout() -> WorkspaceLayout:
    return WorkspaceLayout(
        version=2,
        sections=[WorkspaceSection(id=new_id("sec"), zone="main", columns=[WorkspaceColumn(id=new_id("col"), weight=3, widgets=[])])],
    )


class UiService:
    def __init__(self, store: Any, catalog: Optional[UiCatalog] = None, tasks: Any = None, locks: Any = None) -> None:
        self._store = store
        self._catalog = catalog or UiCatalog()
        # The SDK lock registry is attached at boot (or via the module singleton
        # in tests). Keeping it on the instance means enforcement still works if
        # the singleton is unavailable for any reason.
        self._locks = locks
        # Phase 7: the Engineer-mode orchestration rides the EXISTING Task/Plan
        # runtime (never a second loop). `tasks` is the shared TaskManager; None
        # simply means engineering is unavailable (the tools report it honestly).
        self.engineering = UiEngineering(self, tasks)
        self._dynamic_panels: dict[str, Any] = {}
        self._dynamic_meta: dict[str, dict[str, Any]] = {}
        self._hydrate_registered_components()

    def _hydrate_registered_components(self) -> None:
        for spec in self._store.list_components():
            try:
                self._catalog.register(spec, replace=True)
            except CatalogError:
                continue

    @property
    def catalog(self) -> UiCatalog:
        return self._catalog

    # -- kernel/plugin dynamic panels ----------------------------------------

    def register_dynamic_panel(
        self, panel_id: str, handler: Any, *, meta: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not panel_id or not callable(handler):
            raise UiError("UI_DYNAMIC_PANEL_INVALID", "a dynamic panel needs an id and a handler")
        previous = self._dynamic_panels.get(panel_id)
        previous_meta = self._dynamic_meta.get(panel_id)
        self._dynamic_panels[panel_id] = handler
        self._dynamic_meta[panel_id] = dict(meta or {})

        def dispose() -> None:
            if self._dynamic_panels.get(panel_id) is handler:
                if previous is None:
                    self._dynamic_panels.pop(panel_id, None)
                    self._dynamic_meta.pop(panel_id, None)
                else:
                    self._dynamic_panels[panel_id] = previous
                    self._dynamic_meta[panel_id] = previous_meta or {}

        return dispose

    def dynamic_panels(self) -> list[dict[str, Any]]:
        return [
            {"id": panel_id, **self._dynamic_meta.get(panel_id, {})}
            for panel_id in self._dynamic_panels
        ]

    def render_dynamic_panel(self, panel_id: str, ctx: Any = None) -> dict[str, Any]:
        handler = self._dynamic_panels.get(panel_id)
        if handler is None:
            raise UiError("UI_DYNAMIC_PANEL_NOT_FOUND", f"unknown dynamic panel {panel_id!r}")
        value = handler(ctx) if ctx is not None else handler()
        return value if isinstance(value, dict) else {"type": "markdown", "text": str(value)}

    # ── catalog ───────────────────────────────────────────────────────────────

    def list_components(self, query: Optional[CatalogQuery] = None) -> list[UiComponentSpec]:
        return self._catalog.search(query or CatalogQuery())

    def inspect_component(self, component_id: str) -> UiComponentSpec:
        spec = self._catalog.get(component_id)
        if spec is None:
            raise UiError("UI_COMPONENT_NOT_FOUND", f'unknown component "{component_id}"')
        return spec

    def validate_config(self, component_id: str, config: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        spec = self._catalog.get(component_id)
        if spec is None:
            raise UiError("UI_COMPONENT_NOT_FOUND", f'unknown component "{component_id}"')
        return [i.model_dump(by_alias=True) for i in validate_component_config(spec, config)]

    def register_component(self, spec: UiComponentSpec, *, actor: Optional[str] = None) -> UiComponentSpec:
        try:
            self._catalog.register(spec, replace=True)
        except CatalogError as exc:
            raise UiError(exc.code, exc.message) from exc
        self._store.upsert_component(spec)
        self._record("register_component", None, "completed", actor, detail=f"registered {spec.id}", result={"componentId": spec.id})
        _publish(UiEventType.COMPONENT_REGISTERED.value, {"componentId": spec.id, "owner": spec.owner.value})
        return spec

    def get_capabilities(self) -> dict[str, Any]:
        components = self._catalog.list()
        return {
            "componentCount": len(components),
            "categories": self._catalog.categories(),
            "dataSources": self._catalog.data_sources(),
            "dataSourceRegistry": self.list_data_sources(),
            "components": [c.id for c in components],
        }

    def register_surfaces(self, surfaces: list[AppSurface], *, actor: Optional[str] = None) -> dict[str, Any]:
        """Replace the app's fixed screen inventory (synced from the host).

        The host derives this from its own tab registry; the runtime persists it
        so the agent can answer "where does X live?" after a restart. Saved
        dashboards remain a separate list — an empty batch never wipes it.
        """
        registered = self._store.replace_surfaces(surfaces)
        if registered:
            self._record(
                "register_surfaces", None, "completed", actor,
                detail=f"registered {registered} app screen(s)", result={"screens": registered},
            )
            _publish("ui.surfaces.registered", {"screens": registered})
        return {"total": len(surfaces), "registered": registered}

    def list_data_sources(self) -> dict[str, Any]:
        """The registry of valid data-source strings the runtime understands.

        The component schema permits any source string, so without this the
        agent has no way to know which strings actually resolve. Today the one
        structured family is `portfolio`:

            portfolio:<portfolio_id>:<view>
        """
        return {
            "portfolio": {
                "family": PORTFOLIO_DATA_SOURCE,
                "pattern": f"{PORTFOLIO_DATA_SOURCE}:<portfolio_id>:<view>",
                "metricPattern": f"{PORTFOLIO_DATA_SOURCE}:<portfolio_id>:<view>.<field>",
                "example": f"{PORTFOLIO_DATA_SOURCE}:pf_7586246984d44b11bfb8:positions",
                "metricExample": f"{PORTFOLIO_DATA_SOURCE}:pf_7586246984d44b11bfb8:summary.totalValue",
                "views": list(PORTFOLIO_VIEWS),
            },
            "market": {
                "family": "market",
                "patterns": ["market:quote:<SYMBOLS>", "market:indices:<MARKET>", "market:price:<SYMBOL>"],
                "examples": ["market:quote:AAPL,MSFT", "market:indices:US", "market:price:AAPL"],
                "note": "quote/indices render as tables (or .<field> as a metric); price renders as a chart series.",
            },
            "news": {
                "family": "news",
                "patterns": ["news:latest"],
                "examples": ["news:latest"],
            },
            "agent": {
                "family": "agent",
                "patterns": ["agent:activity"],
                "examples": ["agent:activity"],
            },
            "tasks": {
                "family": "tasks",
                "patterns": ["tasks:list", "tasks:plans"],
                "examples": ["tasks:list", "tasks:plans"],
            },
            "artifacts": {
                "family": "artifacts",
                "patterns": ["artifacts:list"],
                "examples": ["artifacts:list"],
            },
            "research": {
                "family": "research",
                "patterns": ["research:list"],
                "examples": ["research:list"],
            },
            "jdds": {
                "family": "jdds",
                "patterns": ["jdds:search:<QUERY>"],
                "examples": ["jdds:search:apple"],
                "note": "JDDS instrument-universe search; renders rows (or .<field> as a metric).",
            },
            # The components that render these sources live in the host registry
            # (`src/lib/workspace/generic-*.tsx`).
            "components": sorted(RENDERABLE_COMPONENTS),
        }

    def classify_request(self, text: str, *, blueprint_id: Optional[str] = None, owner_id: Optional[str] = None) -> dict[str, Any]:
        """Choose Compose vs Engineer for a natural-language UI request (Phase 6)."""
        blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id) if blueprint_id else None
        decision = _classify_request(text, catalog=self._catalog)
        payload = decision.to_dict()
        if blueprint is not None:
            payload["blueprintId"] = blueprint.id
            payload["revision"] = blueprint.revision
        return payload

    # ── introspection ─────────────────────────────────────────────────────────

    def get_application_map(self, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        """A structured map of the UI the agent can reason about.

        Three DISTINCT things, never conflated:
          * ``screens`` — the app's fixed screens and sub-tabs with deep links
            (synced from the host's tab registry; e.g. Memory → Reviews);
          * ``pages`` — the user's saved dashboards/workspaces;
          * ``capabilities`` — the composable component catalog.
        Live rendered-DOM state is provided by the front door, which augments
        this with the current page's rendered instances.
        """
        listed = self.list_blueprints(ListBlueprintsQuery(limit=200), owner_id=None)
        return {
            "screens": [s.model_dump(by_alias=True) for s in self._store.list_surfaces()],
            "pages": listed["blueprints"],
            "pageCount": listed["total"],
            "capabilities": self.get_capabilities(),
        }

    def get_current_page(
        self,
        *,
        blueprint_id: Optional[str] = None,
        path: Optional[str] = None,
        title: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resolve the page the user is on, unambiguously.

        Resolution order: an explicit ``blueprint_id`` wins; else ``path``; else
        ``title``; else — when nothing is given — the owner's most recently
        updated page (``list_blueprints`` is ordered ``updatedAt DESC``). The
        response always states *how* the page was chosen (``resolution``) and,
        where a choice was made among several candidates, lists the others so a
        caller can disambiguate instead of silently acting on row 1.
        """
        candidates: list[dict[str, Any]] = []
        if blueprint_id:
            blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id)
            resolution = "explicit_id"
        elif path:
            blueprint, resolution, candidates = self._resolve_current_by_path(path, owner_id)
        elif title:
            blueprint, resolution, candidates = self._resolve_current_by_title(title, owner_id)
        else:
            blueprint, resolution, candidates = self._resolve_current_default(owner_id)
        return {
            "blueprint": blueprint.model_dump(by_alias=True),
            "revision": blueprint.revision,
            "instances": self._instances(blueprint),
            "resolution": resolution,
            "candidates": candidates,
        }

    def _candidate_ref(self, entry_or_blueprint: Any) -> dict[str, Any]:
        return {
            "id": entry_or_blueprint.id,
            "title": entry_or_blueprint.title,
            "path": getattr(entry_or_blueprint, "path", None),
            "updatedAt": entry_or_blueprint.updated_at,
        }

    def _resolve_current_by_path(
        self, path: str, owner_id: Optional[str]
    ) -> tuple[UiBlueprint, str, list[dict[str, Any]]]:
        target = _normalize_path(path)
        summaries, _total = self._store.list_blueprints(ListBlueprintsQuery(limit=200), owner_id=owner_id)
        unassigned = 0
        for entry in summaries:
            candidate = self._store.get_blueprint(entry.id, owner_id=owner_id)
            if candidate is None:
                continue
            if not (candidate.path or "").strip():
                unassigned += 1
                continue
            if _normalize_path(candidate.path or "") == target:
                return candidate, "path", []
        # Distinguish "no page has this path" from "pages exist but none has a
        # path assigned" — the latter is actionable (resolve by id/title).
        if unassigned:
            raise UiError(
                "UI_BLUEPRINT_PATH_UNASSIGNED",
                f'no page has path "{path}"; {unassigned} page(s) exist without a path assigned — '
                "resolve by blueprintId or title instead",
            )
        raise UiError("UI_BLUEPRINT_NOT_FOUND", f'no page has path "{path}"')

    def _resolve_current_by_title(
        self, title: str, owner_id: Optional[str]
    ) -> tuple[UiBlueprint, str, list[dict[str, Any]]]:
        want = title.strip().lower()
        summaries, _total = self._store.list_blueprints(ListBlueprintsQuery(limit=200), owner_id=owner_id)
        matches: list[UiBlueprint] = []
        for entry in summaries:
            if entry.title.strip().lower() != want:
                continue
            candidate = self._store.get_blueprint(entry.id, owner_id=owner_id)
            if candidate is not None:
                matches.append(candidate)
        if not matches:
            raise UiError("UI_BLUEPRINT_NOT_FOUND", f'no page is titled "{title}"')
        # Most recently updated wins (id ASC tiebreak — same order as list).
        matches.sort(key=lambda b: b.id)
        matches.sort(key=lambda b: b.updated_at, reverse=True)
        chosen = matches[0]
        candidates = [self._candidate_ref(m) for m in matches[1:]]
        return chosen, ("title" if len(matches) == 1 else "title_ambiguous"), candidates

    def _resolve_current_default(
        self, owner_id: Optional[str]
    ) -> tuple[UiBlueprint, str, list[dict[str, Any]]]:
        summaries, _total = self._store.list_blueprints(ListBlueprintsQuery(limit=200), owner_id=owner_id)
        if not summaries:
            raise UiError("UI_BLUEPRINT_NOT_FOUND", "there is no page to fall back to")
        chosen = self._store.get_blueprint(summaries[0].id, owner_id=owner_id)
        if chosen is None:
            raise UiError("UI_BLUEPRINT_NOT_FOUND", "there is no page to fall back to")
        candidates = [self._candidate_ref(s) for s in summaries[1:]]
        return chosen, "default_most_recent", candidates

    def _instances(self, blueprint: UiBlueprint) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for section in blueprint.layout.sections:
            for column in section.columns:
                for widget in column.widgets:
                    spec = self._catalog.get(widget.widget_id)
                    out.append({
                        "instanceId": widget.id,
                        "componentId": widget.widget_id,
                        "category": spec.category if spec else None,
                        "owner": spec.owner.value if spec else None,
                        "availability": spec.availability.value if spec else "unavailable",
                        "config": widget.config or {},
                        "hidden": bool(widget.hidden),
                        "sectionId": section.id,
                        "columnId": column.id,
                        "height": widget.height,
                    })
        return out

    def verify_page(self, blueprint_id: str, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        """Read a page back and report whether each widget can actually render.

        The honest check a composer needs before claiming success: is each
        widget's component id present in the catalog (renderable), and — where a
        `portfolio:<id>:<view>` source is declared — is it a well-formed source
        string? It reports what it can verify and names what it cannot; it never
        asserts the live data was fetched (that is the front door's job).
        """
        blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id)
        instances: list[dict[str, Any]] = []
        all_available = True
        for instance in self._instances(blueprint):
            spec = self._catalog.get(instance["componentId"])
            config = instance.get("config") or {}
            raw_source = config.get("source") or config.get("metric")
            source = str(raw_source) if isinstance(raw_source, str) and raw_source.strip() else None
            source_valid: Optional[bool] = None
            source_reason: Optional[str] = None
            if config.get("portfolioId"):
                # `portfolioId` (+ an optional bare field in `metric`) implies a
                # portfolio source; validate that shape so a typo in the field is
                # still surfaced.
                pid = str(config["portfolioId"]).strip()
                field = f".{source}" if source else ""
                implied = f"{PORTFOLIO_DATA_SOURCE}:{pid}:summary{field}"
                source_valid, source_reason = validate_source(implied)
            elif source:
                # ANY declared source is checked against the closed grammar —
                # including a bare token such as "rates" or "us10y". Previously a
                # value without ":" skipped validation, so `ui_verify_page` said
                # a page was renderable while its binding was invalid.
                source_valid, source_reason = validate_source(source)
            available = spec is not None
            all_available = all_available and available and source_valid is not False
            instances.append({
                "instanceId": instance["instanceId"],
                "componentId": instance["componentId"],
                "componentAvailable": available,
                "source": source,
                "sourceValid": source_valid,
                "sourceReason": source_reason,
            })
        return {
            "blueprintId": blueprint.id,
            "title": blueprint.title,
            "revision": blueprint.revision,
            "renderable": all_available,
            "instances": instances,
        }

    def get_operation_status(self, operation_id: str) -> UiOperation:
        operation = self._store.get_operation(operation_id)
        if operation is None:
            raise UiError("UI_OPERATION_NOT_FOUND", f'unknown operation "{operation_id}"')
        return operation

    # ── blueprints: read ──────────────────────────────────────────────────────

    def get_blueprint(self, blueprint_id: str, *, owner_id: Optional[str] = None) -> UiBlueprint:
        # Fetch unscoped, then apply the owner rule HERE so a scope mismatch is a
        # distinct, actionable error — never collapsed into "unknown blueprint".
        # This gives get_current_page / get_layout / preview the same policy.
        blueprint = self._store.get_blueprint_unscoped(blueprint_id)
        if blueprint is None:
            raise UiError("UI_BLUEPRINT_NOT_FOUND", f'unknown blueprint "{blueprint_id}"')
        if owner_id is not None and blueprint.owner.id != owner_id:
            raise UiError(
                "UI_BLUEPRINT_OWNER_MISMATCH",
                f'blueprint "{blueprint_id}" is owned by "{blueprint.owner.id}", not "{owner_id}"',
            )
        return blueprint

    def list_blueprints(self, query: Optional[ListBlueprintsQuery] = None, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        query = query or ListBlueprintsQuery()
        summaries, _stored_total = self._store.list_blueprints(query, owner_id=owner_id)
        visible = [
            s for s in summaries
            if query.include_dashboard_mirrors or not _is_dashboard_mirror(s.path)
        ]
        return {
            "blueprints": [s.model_dump(by_alias=True) for s in visible],
            "total": len(visible), "limit": query.limit, "offset": query.offset,
        }

    def get_layout(self, blueprint_id: str, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id)
        return {"blueprintId": blueprint.id, "revision": blueprint.revision, "layout": blueprint.layout.model_dump(by_alias=True)}

    def history(self, blueprint_id: str) -> dict[str, Any]:
        self.get_blueprint(blueprint_id)
        return {"blueprintId": blueprint_id, "revisions": self._store.history(blueprint_id)}

    def export_blueprint(self, blueprint_id: str, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        """A portable, self-describing snapshot (no secrets; blueprint data only)."""
        blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id)
        return {
            "kind": "eclipse.ui.blueprint",
            "schemaVersion": blueprint.schema_version,
            "exportedAt": now_iso(),
            "blueprint": blueprint.model_dump(by_alias=True),
        }

    def import_blueprint(self, payload: dict[str, Any], *, owner_id: str = "operator", actor: Optional[str] = None) -> UiBlueprint:
        """Validate an exported snapshot and store it as a NEW blueprint (fresh id)."""
        raw = payload.get("blueprint") if isinstance(payload, dict) and isinstance(payload.get("blueprint"), dict) else payload
        validation = validate_blueprint(raw, self._catalog)
        if not validation.ok or validation.blueprint is None:
            raise UiError(
                "UI_BLUEPRINT_INVALID", "the imported blueprint failed validation",
                issues=[i.model_dump(by_alias=True) for i in validation.issues],
            )
        imported = validation.blueprint.model_copy(deep=True)
        imported.id = new_id("uibp")
        imported.revision = 1
        imported.created_at = now_iso()
        imported.updated_at = imported.created_at
        imported.owner = BlueprintOwner(kind="user", id=owner_id)
        saved = self._store.save_blueprint(imported, summary="imported")
        self._record("import_blueprint", saved.id, "completed", actor, detail=f"imported {saved.title}", result={"blueprintId": saved.id})
        _publish(UiEventType.PAGE_CREATED.value, {"blueprintId": saved.id, "title": saved.title, "imported": True})
        return saved

    # ── blueprints: write ─────────────────────────────────────────────────────

    def _unique_title(self, title: str, owner_id: str) -> str:
        """A title unique among this owner's blueprints, so a create never adds a
        second page sharing a title. A duplicate is suffixed ("Main" -> "Main 2").

        Duplicates can still arise from a title EDIT (`update_page`) or an import,
        so the `title_ambiguous` resolution path remains reachable and tested; only
        CREATE guarantees uniqueness.
        """
        base = (title or "").strip() or "Page"
        try:
            existing, _ = self._store.list_blueprints(ListBlueprintsQuery(limit=200), owner_id=owner_id)
        except Exception:  # noqa: BLE001 - uniquing must never block a create
            return base
        taken = {b.title.strip().casefold() for b in existing}
        if base.casefold() not in taken:
            return base
        n = 2
        while f"{base} {n}".casefold() in taken:
            n += 1
        return f"{base} {n}"

    def create_page(self, input_model: CreatePageInput, *, owner_id: str = "operator", actor: Optional[str] = None) -> UiBlueprint:
        if input_model.idempotency_key:
            existing = self._store.lookup_idempotency(input_model.idempotency_key)
            if existing:
                found = self._store.get_blueprint(existing)
                if found is not None:
                    return found
        blueprint = UiBlueprint(
            id=new_id("uibp"), title=self._unique_title(input_model.title, owner_id),
            description=input_model.description,
            kind=input_model.kind, scope=input_model.scope, path=input_model.path,
            icon=input_model.icon, tags=input_model.tags or None, layout=_empty_layout(),
            owner=BlueprintOwner(kind="user", id=owner_id),
            revision=1, created_at=now_iso(), updated_at=now_iso(),
        )
        validation = validate_blueprint(blueprint.model_dump(by_alias=True), self._catalog)
        if not validation.ok:
            raise UiError("UI_BLUEPRINT_INVALID", "the new page failed validation", issues=[i.model_dump(by_alias=True) for i in validation.issues])
        saved = self._store.save_blueprint(validation.blueprint or blueprint, summary="created")
        if input_model.idempotency_key:
            self._store.record_idempotency(input_model.idempotency_key, saved.id)
        self._record("create_page", saved.id, "completed", actor, detail=f"created {saved.title}", result={"blueprintId": saved.id})
        _publish(UiEventType.PAGE_CREATED.value, {"blueprintId": saved.id, "title": saved.title})
        return saved

    def _assert_unlocked(
        self,
        blueprint_id: str,
        action: str,
        ops: Optional[list[dict[str, Any]]] = None,
        instance_ids: Optional[list[str]] = None,
    ) -> None:
        """Refuse a mutation on a locked page (and its locked widget instances).

        Locks are enforced HERE, not only at the SDK route, so the Pages UI, the
        `/api/ui` routes and the `ui_*` agent tools all obey them. The unlock
        token arrives through the SDK middleware's contextvar; an agent tool run
        carries no request header and is therefore blocked until the user
        unlocks through the SDK/UI.
        """
        # Only an explicitly attached registry can own locks. Do NOT fall back
        # to a process singleton: a directly constructed service in a test or
        # evaluation must never consult another runtime's (possibly closed) DB.
        locks = self._locks
        if locks is None:
            return
        try:
            from ..sdk.locks import LockError  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - SDK absence means no locks exist
            return
        try:
            locks.assert_allowed("page", blueprint_id, action)
            ids = list(instance_ids or [])
            for op in ops or []:
                instance_id = op.get("instanceId")
                if instance_id:
                    ids.append(instance_id)
            for instance_id in dict.fromkeys(ids):
                if instance_id:
                    locks.assert_allowed("widget", f"{blueprint_id}:{instance_id}", action)
        except LockError as exc:
            raise UiError("UI_LOCKED", exc.message) from exc

    def update_page(self, blueprint_id: str, input_model: UpdatePageInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None, expected_revision: Optional[int] = None) -> UiBlueprint:
        meta = {"op": "update_meta"}
        if input_model.title is not None:
            meta["title"] = input_model.title
        if input_model.description is not None:
            meta["description"] = input_model.description
        if input_model.path is not None:
            meta["path"] = input_model.path
        if input_model.icon is not None:
            meta["icon"] = input_model.icon
        if input_model.tags is not None:
            meta["tags"] = input_model.tags
        return self._mutate(blueprint_id, [meta], "update_page", owner_id=owner_id, actor=actor, expected_revision=expected_revision, event=UiEventType.PAGE_UPDATED)

    def duplicate_page(self, blueprint_id: str, *, owner_id: Optional[str] = None, actor: Optional[str] = None, title: Optional[str] = None) -> UiBlueprint:
        source = self.get_blueprint(blueprint_id, owner_id=owner_id)
        clone = source.model_copy(deep=True)
        clone.id = new_id("uibp")
        clone.title = title or f"{source.title} (copy)"
        clone.revision = 1
        clone.created_at = now_iso()
        clone.updated_at = clone.created_at
        saved = self._store.save_blueprint(clone, summary="duplicated")
        self._record("duplicate_page", saved.id, "completed", actor, detail=f"duplicated from {source.id}", result={"blueprintId": saved.id, "sourceId": source.id})
        return saved

    def delete_page(self, blueprint_id: str, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> bool:
        """Delete a page, refusing when it (or a widget on it) is locked."""
        blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id)
        instance_ids = [
            w.id for s in blueprint.layout.sections for c in s.columns for w in c.widgets
        ]
        self._assert_unlocked(blueprint.id, "delete", instance_ids=instance_ids)
        removed = self._store.delete_blueprint(blueprint.id)
        self._record("delete_page", blueprint.id, "completed", actor, detail=f"deleted {blueprint.title}", result={"blueprintId": blueprint.id})
        _publish(UiEventType.PAGE_UPDATED.value, {"blueprintId": blueprint.id, "deleted": True})
        return removed

    def save_layout(self, input_model: SaveLayoutInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        blueprint = self.get_blueprint(input_model.blueprint_id, owner_id=owner_id)
        instance_ids = [
            w.id for s in blueprint.layout.sections for c in s.columns for w in c.widgets
        ] + [
            w.id for s in input_model.layout.sections for c in s.columns for w in c.widgets
        ]
        self._assert_unlocked(blueprint.id, "write", instance_ids=instance_ids)
        candidate = blueprint.model_copy(deep=True)
        candidate.layout = input_model.layout
        candidate.revision = blueprint.revision + 1
        candidate.updated_at = now_iso()
        validation = validate_blueprint(candidate.model_dump(by_alias=True), self._catalog)
        if not validation.ok:
            raise UiError("UI_LAYOUT_INVALID", "the layout failed validation", issues=[i.model_dump(by_alias=True) for i in validation.issues])
        return self._commit(validation.blueprint or candidate, input_model.expected_revision, blueprint, "save_layout", actor, "saved layout", UiEventType.LAYOUT_CHANGED)

    def restore_revision(self, input_model: RestoreRevisionInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        blueprint = self.get_blueprint(input_model.blueprint_id, owner_id=owner_id)
        instance_ids = [
            w.id for s in blueprint.layout.sections for c in s.columns for w in c.widgets
        ]
        self._assert_unlocked(blueprint.id, "write", instance_ids=instance_ids)
        previous = self._store.get_revision(blueprint.id, input_model.revision)
        if previous is None:
            raise UiError("UI_REVISION_NOT_FOUND", f"revision {input_model.revision} is not retained")
        restored = previous.model_copy(deep=True)
        restored.revision = blueprint.revision + 1
        restored.updated_at = now_iso()
        saved = self._store.save_blueprint(restored, summary=f"restored revision {input_model.revision}")
        self._record("restore_layout", saved.id, "completed", actor, detail=f"restored r{input_model.revision}", result={"blueprintId": saved.id, "revision": saved.revision})
        _publish(UiEventType.BLUEPRINT_RESTORED.value, {"blueprintId": saved.id, "fromRevision": input_model.revision, "toRevision": saved.revision})
        return saved

    # ── widget / layout operations ────────────────────────────────────────────

    def _mutate(self, blueprint_id: str, ops: list[dict[str, Any]], operation: str, *, owner_id: Optional[str], actor: Optional[str], expected_revision: Optional[int], event: Optional[UiEventType] = None, summary: Optional[str] = None) -> UiBlueprint:
        blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id)
        self._assert_unlocked(blueprint_id, "write", ops)
        result = engine_apply_ops(blueprint, ops, catalog=self._catalog)
        if not result.ok:
            self._record(operation, blueprint_id, "rejected", actor, detail="validation failed", error=result.issues[0].message if result.issues else "invalid", result={"issues": [i.model_dump(by_alias=True) for i in result.issues]})
            raise UiError("UI_OPERATION_INVALID", result.issues[0].message if result.issues else "the operation was invalid", issues=[i.model_dump(by_alias=True) for i in result.issues])
        changes = diff_blueprints(blueprint, result.blueprint)
        return self._commit(result.blueprint, expected_revision, blueprint, operation, actor, summary or operation, event, changes=changes)

    def _commit(self, candidate: UiBlueprint, expected_revision: Optional[int], current: UiBlueprint, operation: str, actor: Optional[str], detail: str, event: Optional[UiEventType], *, changes: Optional[list[Any]] = None) -> UiBlueprint:
        if expected_revision is not None and expected_revision != current.revision:
            self._record(operation, current.id, "conflict", actor, detail=f"expected r{expected_revision}, found r{current.revision}", error="UI_CONFLICT")
            _publish(UiEventType.OPERATION_FAILED.value, {"blueprintId": current.id, "operation": operation, "reason": "conflict"})
            raise UiError("UI_CONFLICT", f"blueprint moved from revision {expected_revision} to {current.revision}; reload and re-apply")
        saved = self._store.save_blueprint(candidate, summary=detail)
        if changes is not None:
            self._store.append_revision(saved, summary=detail, changes=[c.model_dump(by_alias=True) for c in changes])
        self._record(operation, saved.id, "completed", actor, detail=detail, result={"blueprintId": saved.id, "revision": saved.revision})
        if event is not None:
            _publish(event.value, {"blueprintId": saved.id, "revision": saved.revision})
        return saved

    def add_widget(self, input_model: AddWidgetInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        if json.dumps(input_model.config or {}) and len(json.dumps(input_model.config)) > MAX_CONFIG_BYTES:
            raise UiError("UI_CONFIG_TOO_LARGE", "widget configuration exceeds the size limit")
        op: dict[str, Any] = {"op": "add_widget", "componentId": input_model.component_id}
        if input_model.instance_id:
            op["instanceId"] = input_model.instance_id
        if input_model.target is not None:
            op["target"] = input_model.target.model_dump(by_alias=True)
        if input_model.zone is not None:
            op["zone"] = input_model.zone
        if input_model.height is not None:
            op["height"] = input_model.height
        if input_model.config:
            op["config"] = input_model.config
        blueprint = self._mutate(input_model.blueprint_id, [op], "add_widget", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.WIDGET_ADDED, summary=f"added {input_model.component_id}")
        return blueprint

    def remove_widget(self, input_model: WidgetRefInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "remove_widget", "instanceId": input_model.instance_id}], "remove_widget", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.WIDGET_REMOVED)

    def move_widget(self, input_model: MoveWidgetInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "move_widget", "instanceId": input_model.instance_id, "target": input_model.target.model_dump(by_alias=True)}], "move_widget", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.WIDGET_UPDATED)

    def reorder_widget(self, input_model: ReorderWidgetInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "reorder_widget", "instanceId": input_model.instance_id, "toIndex": input_model.to_index}], "reorder_widget", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.WIDGET_UPDATED)

    def resize_widget(self, input_model: ResizeWidgetInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "resize_widget", "instanceId": input_model.instance_id, "height": input_model.height}], "resize_widget", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.WIDGET_UPDATED)

    def update_widget_config(self, input_model: UpdateConfigInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "update_config", "instanceId": input_model.instance_id, "patch": input_model.patch}], "update_config", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.WIDGET_UPDATED)

    def set_column_count(self, input_model: SetColumnCountInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "set_column_count", "sectionId": input_model.section_id, "count": input_model.count}], "set_column_count", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.LAYOUT_CHANGED)

    def set_column_weight(self, input_model: SetColumnWeightInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "set_column_weight", "columnId": input_model.column_id, "weight": input_model.weight}], "set_column_weight", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.LAYOUT_CHANGED)

    def remove_section(self, input_model: SectionRefInput, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> UiBlueprint:
        return self._mutate(input_model.blueprint_id, [{"op": "remove_section", "sectionId": input_model.section_id}], "remove_section", owner_id=owner_id, actor=actor, expected_revision=input_model.expected_revision, event=UiEventType.LAYOUT_CHANGED)

    def apply_ops(self, blueprint_id: str, ops: list[dict[str, Any]], *, owner_id: Optional[str] = None, actor: Optional[str] = None, expected_revision: Optional[int] = None) -> UiBlueprint:
        return self._mutate(blueprint_id, ops, "apply_ops", owner_id=owner_id, actor=actor, expected_revision=expected_revision)

    def preview(self, blueprint_id: str, ops: list[dict[str, Any]], *, owner_id: Optional[str] = None) -> dict[str, Any]:
        """Compute the would-be result of a batch WITHOUT persisting it."""
        blueprint = self.get_blueprint(blueprint_id, owner_id=owner_id)
        result = engine_apply_ops(blueprint, ops, catalog=self._catalog)
        if not result.ok:
            raise UiError("UI_OPERATION_INVALID", "the preview failed validation", issues=[i.model_dump(by_alias=True) for i in result.issues])
        changes = diff_blueprints(blueprint, result.blueprint)
        return {
            "blueprint": result.blueprint.model_dump(by_alias=True),
            "changes": [c.model_dump(by_alias=True) for c in changes],
            "currentRevision": blueprint.revision,
            "nextRevision": result.blueprint.revision,
        }

    # ── audit ─────────────────────────────────────────────────────────────────

    def _record(self, operation: str, blueprint_id: Optional[str], status: str, actor: Optional[str], *, detail: str = "", result: Optional[dict[str, Any]] = None, error: Optional[str] = None) -> UiOperation:
        record = UiOperation(
            id=new_id("uiop"), operation=operation, blueprint_id=blueprint_id, status=status,
            actor=actor, detail=detail, result=result or {}, error=error, created_at=now_iso(),
        )
        return self._store.record_operation(record)


__all__ = ["UiError", "UiService"]
