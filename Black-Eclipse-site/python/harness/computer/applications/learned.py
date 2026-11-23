"""Learned application interfaces (the Agent S4 direction, bounded and honest).

A shipped adapter is hand-written knowledge. This module learns a small adapter
from a **verified workflow candidate** (see `computer/procedural.py`): the
controls it acted on, the shortcuts it used, and the task→steps recipe. It never
invents a control, never stores typed text, and cannot execute anything — it is
knowledge the grounder and the prompt can consult.

Validation is structural and evidence-bound:

  * at least two real steps and one named control;
  * the source candidate must be a verified success (the procedural module only
    builds those);
  * the record is versioned on every successful derivation, adding to the prior
    controls/shortcuts/recipes;
  * `validated` means "derived from a verified run", NOT "live-verified against
    every build" — the live rig's `verified` flag stays separate and false.

The book is a JSON sidecar under the gitignored artifacts root (a cache, not the
authority), and it is also exposed through `computer_recall` so the knowledge is
inspectable.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .base import ApplicationAdapter

_SLUG = re.compile(r"[^a-z0-9]+")
_STEP_CONTROL_ACTIONS = frozenset({"click", "double_click", "right_click", "middle_click",
                                   "invoke", "set_value", "press_and_hold"})
# How many consecutive observations without a known control before the learned
# interface is considered stale (and stops being selected for grounding).
STALE_AFTER_MISSES = 3


def _slug(text: str) -> str:
    value = _SLUG.sub("-", (text or "application").lower()).strip("-")
    return value[:48] or "application"


def _parse_control(target: str) -> Optional[dict[str, str]]:
    """'button Save' / 'textbox Ticker:' → {role, name}. Never guesses."""
    text = " ".join(str(target or "").split())
    if not text:
        return None
    head, _, rest = text.partition(" ")
    role = head.lower()
    name = rest.strip()
    if role not in ("button", "textbox", "edit", "combobox", "checkbox", "radio",
                    "menuitem", "tab", "link", "listitem", "gridcell", "text",
                    "slider", "spinbutton", "treeitem"):
        return None
    if not name:
        return None
    return {"name": name[:120], "role": role}


@dataclass
class LearnedAdapter:
    id: str
    application: str
    version: int = 1
    controls: dict[str, dict[str, str]] = field(default_factory=dict)
    shortcuts: dict[str, str] = field(default_factory=dict)
    recipes: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    validated: bool = False
    validation: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Live-observation validation: controls seen in a real snapshot reset the
    # miss counter; repeated misses mark the interface stale so the grounder
    # stops trusting a UI that changed under it.
    last_validated_at: float = 0.0
    misses: int = 0
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "application": self.application, "version": self.version,
            "controls": self.controls, "shortcuts": self.shortcuts,
            "recipes": self.recipes, "evidence": self.evidence,
            "validated": self.validated, "validation": self.validation,
            "createdAt": self.created_at, "updatedAt": self.updated_at,
            "lastValidatedAt": self.last_validated_at, "misses": self.misses,
            "stale": self.stale,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LearnedAdapter":
        return cls(
            id=str(payload.get("id") or "application"),
            application=str(payload.get("application") or ""),
            version=int(payload.get("version") or 1),
            controls=dict(payload.get("controls") or {}),
            shortcuts=dict(payload.get("shortcuts") or {}),
            recipes=list(payload.get("recipes") or []),
            evidence=list(payload.get("evidence") or []),
            validated=bool(payload.get("validated")),
            validation=str(payload.get("validation") or ""),
            created_at=float(payload.get("createdAt") or time.time()),
            updated_at=float(payload.get("updatedAt") or time.time()),
            last_validated_at=float(payload.get("lastValidatedAt") or 0.0),
            misses=int(payload.get("misses") or 0),
            stale=bool(payload.get("stale")),
        )

    def to_application_adapter(self) -> ApplicationAdapter:
        """A read-only knowledge adapter the registry can select."""
        return LearnedApplicationAdapter(self)

    def recall_text(self) -> str:
        controls = ", ".join(
            f"{control.get('role', 'control')} {control.get('name', '')}".strip()
            for control in self.controls.values()
        ) or "none yet"
        shortcuts = ", ".join(self.shortcuts.values()) or "none yet"
        recipes = "; ".join(str(r.get("task") or "") for r in self.recipes[-3:]) or "none yet"
        state = "stale" if self.stale else ("validated" if self.validated else "unvalidated")
        seen = (
            f", last confirmed {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.last_validated_at))}"
            if self.last_validated_at else ""
        )
        return (
            f"Learned interface for {self.application} (v{self.version}, {state}{seen}): "
            f"controls: {controls}. Shortcuts: {shortcuts}. Known tasks: {recipes}."
        )


class LearnedApplicationAdapter(ApplicationAdapter):
    """The registry-facing view of a learned record.

    It shares the `LearnedAdapter` object, so when live observation marks the
    record stale this adapter immediately stops matching — the grounder cannot
    keep using an interface the current UI no longer supports.
    """

    def __init__(self, record: "LearnedAdapter") -> None:
        self.record = record
        self.id = f"learned-{record.id}"
        self.name = f"{record.application} (learned v{record.version})"
        self.executables = (_slug(record.application).replace("-", ""),)
        self.title_patterns = (record.application.lower(),) if record.application else ()
        self.controls = dict(record.controls)
        self.shortcuts = dict(record.shortcuts)
        self.safe_actions = ("observe", "navigate", "select", "copy")
        self.risky_actions = ()
        self.has_native_api = False
        self.verified = False
        self.verified_note = (
            f"learned from {len(record.evidence)} verified workflow(s); not live-verified"
        )

    def matches(self, *, app_name: str = "", executable: str = "", title: str = "") -> bool:
        if self.record.stale:
            return False
        return super().matches(app_name=app_name, executable=executable, title=title)


def derive_adapter(candidate: dict[str, Any], *, existing: Optional[LearnedAdapter] = None) -> Optional[LearnedAdapter]:
    """Derive (or extend) an adapter from a verified workflow candidate."""
    application = " ".join(str(candidate.get("application") or "").split())[:80]
    if not application:
        return None
    controls: dict[str, dict[str, str]] = dict(existing.controls) if existing else {}
    shortcuts: dict[str, str] = dict(existing.shortcuts) if existing else {}
    steps = list(candidate.get("steps") or [])
    for step in steps:
        action = str(step.get("action") or "")
        target = str(step.get("target") or "")
        if action in _STEP_CONTROL_ACTIONS:
            control = _parse_control(target)
            if control:
                controls.setdefault(control["name"].lower(), control)
        elif action in ("key", "hotkey") and target:
            shortcut = target.strip()
            if shortcut:
                shortcuts[f"learned_{len(shortcuts) + 1}"] = shortcut[:60]
    recipes = list(existing.recipes) if existing else []
    task = str(candidate.get("task") or "").strip()
    if task:
        recipe = {
            "task": task[:300],
            "steps": [
                {"action": str(s.get("action")), "target": str(s.get("target") or "")}
                for s in steps[:20]
            ],
            "verified": True,
            "source": "verified_success",
        }
        recipes = [r for r in recipes if r.get("task") != recipe["task"]]
        recipes.append(recipe)
        recipes = recipes[-20:]
    digest = hashlib.sha256(
        json.dumps({"app": application, "task": task, "steps": steps}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    evidence = list(existing.evidence) if existing else []
    if digest not in evidence:
        evidence.append(digest)
    adapter = LearnedAdapter(
        id=_slug(application),
        application=application,
        version=(existing.version + 1) if existing else 1,
        controls=controls,
        shortcuts=shortcuts,
        recipes=recipes,
        evidence=evidence[-20:],
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
        last_validated_at=(existing.last_validated_at if existing else 0.0),
        misses=0,
        stale=False,
    )
    ok, reason = validate_adapter(adapter)
    adapter.validated = ok
    adapter.validation = reason
    return adapter if ok else None


def validate_adapter(adapter: LearnedAdapter) -> tuple[bool, str]:
    if not adapter.application:
        return False, "no application name"
    if not adapter.controls:
        return False, "no named control was observed"
    if not adapter.recipes:
        return False, "no task recipe was captured"
    if not adapter.evidence:
        return False, "no verified-run evidence"
    if adapter.version < 1:
        return False, "invalid version"
    return True, (
        f"derived from {len(adapter.evidence)} verified workflow(s); "
        f"{len(adapter.controls)} controls, {len(adapter.recipes)} recipe(s)"
    )


class LearnedAdapterBook:
    """A versioned, inspectable sidecar of learned adapters."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else None
        self._adapters: dict[str, LearnedAdapter] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = payload.get("adapters") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                adapter = LearnedAdapter.from_dict(item)
                self._adapters[adapter.id] = adapter

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({
                "version": 1,
                "adapters": [a.to_dict() for a in self._adapters.values()],
            }, indent=2), encoding="utf-8")
        except OSError:
            pass

    def record(self, candidate: dict[str, Any]) -> Optional[LearnedAdapter]:
        application = " ".join(str(candidate.get("application") or "").split())
        if not application:
            return None
        existing = self._adapters.get(_slug(application))
        adapter = derive_adapter(candidate, existing=existing)
        if adapter is None:
            return None
        self._adapters[adapter.id] = adapter
        self._save()
        return adapter

    def get(self, application: str) -> Optional[LearnedAdapter]:
        return self._adapters.get(_slug(application))

    def select(self, *, app_name: str = "", title: str = "") -> Optional[LearnedAdapter]:
        haystack = f"{app_name or ''} {title or ''}".lower()
        for adapter in self._adapters.values():
            if adapter.stale:
                continue
            if adapter.application and adapter.application.lower() in haystack:
                return adapter
        return None

    def observe(self, application: str, control_names: Any) -> Optional[LearnedAdapter]:
        """Validate a learned interface against a LIVE observation.

        Any known control present resets the miss counter; repeated observations
        with none mark the interface stale, so the grounder stops using it until a
        new verified run learns the changed UI.
        """
        adapter = self.get(application)
        if adapter is None:
            return None
        names = {str(name).strip().lower() for name in (control_names or []) if str(name).strip()}
        known = {
            str(control.get("name") or "").strip().lower()
            for control in adapter.controls.values()
            if control.get("name")
        }
        if known & names:
            adapter.last_validated_at = time.time()
            adapter.misses = 0
            adapter.stale = False
        else:
            adapter.misses += 1
            if adapter.misses >= STALE_AFTER_MISSES:
                adapter.stale = True
        adapter.updated_at = time.time()
        self._save()
        return adapter

    def forget(self, application: str) -> bool:
        adapter = self.get(application)
        if adapter is None:
            return False
        # Live sessions hold a registry view of THIS record; marking it stale
        # stops selection immediately, not after a restart.
        adapter.stale = True
        adapter.updated_at = time.time()
        self._adapters.pop(adapter.id, None)
        self._save()
        return True

    def all(self) -> list[LearnedAdapter]:
        return sorted(self._adapters.values(), key=lambda a: a.application.lower())

    def recall(self, query: str = "", *, application: str = "") -> list[LearnedAdapter]:
        needle = (query or "").lower()
        if application:
            adapter = self.get(application)
            return [adapter] if adapter else []
        return [
            a for a in self.all()
            if not needle or needle in a.application.lower() or needle in a.recall_text().lower()
        ]

    def register_into(self, registry: Any) -> None:
        for adapter in self.all():
            try:
                registry.register(adapter.to_application_adapter())
            except Exception:  # noqa: BLE001 - a knowledge record must not break selection
                continue

    def describe(self) -> dict[str, Any]:
        return {
            "count": len(self._adapters),
            "stale": sum(1 for a in self._adapters.values() if a.stale),
            "adapters": [
                {"id": a.id, "application": a.application, "version": a.version,
                 "validated": a.validated, "stale": a.stale,
                 "lastValidatedAt": a.last_validated_at,
                 "controls": len(a.controls), "recipes": len(a.recipes)}
                for a in self.all()
            ],
        }


__all__ = [
    "LearnedAdapter", "LearnedAdapterBook", "LearnedApplicationAdapter",
    "STALE_AFTER_MISSES", "derive_adapter", "validate_adapter",
]
