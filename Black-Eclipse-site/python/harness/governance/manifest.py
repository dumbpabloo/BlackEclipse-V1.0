"""A methodology fingerprint: what produced this run, independent of *when*.

Two runs of the same prompt, the same skills and the same tools must hash
identically on different days, so ``timestamp`` and ``run_id`` are recorded but
deliberately excluded from the hash. That is the whole point: the digest names
the *methodology*, not the event.

This is the port of Vibe's ``governance/manifest.py``. It answers a question the
existing provenance records do not: "was this conclusion produced by the same
system that produced that one?", where the system is prompt + skills + tool
surface + curated package versions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = ["build_manifest", "manifest_hash", "manifest_diff", "verify_hash", "HASHED_FIELDS"]

#: The fields the digest covers. Anything not here (timestamp, runId, hash
#: itself) is metadata about the run, not about the methodology.
HASHED_FIELDS = (
    "systemPromptHash",
    "skills",
    "tools",
    "packages",
    "extra",
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def build_manifest(
    *,
    system_prompt: str,
    skills: Iterable[Sequence[str]] = (),
    tools: Iterable[str] = (),
    packages: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, str]] = None,
    timestamp: Optional[str] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the manifest. ``skills`` is ``(name, content)`` pairs.

    Skills are hashed by their *content*, so a renamed skill with the same text is
    a different skill but an edited one is always visible. Tools are sorted and
    hashed as a set, because ordering is an implementation detail and a reshuffle
    must not look like a methodology change.
    """
    skill_rows = sorted(
        ({"name": str(name), "contentHash": _digest(str(content))}
         for name, content in skills),
        key=lambda row: row["name"],
    )
    tool_names = sorted({str(name) for name in tools})
    package_rows = {str(k): str(v) for k, v in sorted((packages or {}).items())}
    extra_rows = {str(k): str(v) for k, v in sorted((extra or {}).items())}
    manifest: dict[str, Any] = {
        "systemPromptHash": _digest(str(system_prompt)),
        "skills": skill_rows,
        "tools": {"names": tool_names, "hash": _digest("\n".join(tool_names))},
        "packages": package_rows,
        # Readable, un-hashed-in-value composition dimensions (provider, model):
        # small and non-sensitive, so they are stored as text for a diff to show.
        "extra": extra_rows,
        "timestamp": timestamp,
        "runId": run_id,
    }
    manifest["manifestHash"] = manifest_hash(manifest)
    return manifest


def verify_hash(manifest: Mapping[str, Any]) -> bool:
    """Recompute the digest from the recorded fields and compare.

    A persisted manifest trusts its stored hash at load time; this is the check
    that actually re-derives it, so a hand-edited skills list is caught.
    """
    return manifest_hash(manifest) == manifest.get("manifestHash")


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Hash only :data:`HASHED_FIELDS`, so the digest is day-independent."""
    payload = {field: manifest.get(field) for field in HASHED_FIELDS}
    return _digest(_canonical(payload))


def manifest_diff(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """What changed between two manifests, field by field. Empty = identical."""
    changed: dict[str, Any] = {}
    if before.get("systemPromptHash") != after.get("systemPromptHash"):
        changed["systemPrompt"] = True
    if _skill_map(before) != _skill_map(after):
        changed["skills"] = {
            "added": sorted(set(_skill_map(after)) - set(_skill_map(before))),
            "removed": sorted(set(_skill_map(before)) - set(_skill_map(after))),
            "edited": sorted(
                name
                for name in set(_skill_map(before)) & set(_skill_map(after))
                if _skill_map(before)[name] != _skill_map(after)[name]
            ),
        }
    before_tools = set(((before.get("tools") or {}).get("names")) or ())
    after_tools = set(((after.get("tools") or {}).get("names")) or ())
    if before_tools != after_tools:
        changed["tools"] = {
            "added": sorted(after_tools - before_tools),
            "removed": sorted(before_tools - after_tools),
        }
    if dict(before.get("packages") or {}) != dict(after.get("packages") or {}):
        changed["packages"] = True
    return changed


def _skill_map(manifest: Mapping[str, Any]) -> dict[str, str]:
    rows = manifest.get("skills") or []
    return {
        row["name"]: row["contentHash"]
        for row in rows
        if isinstance(row, dict) and "name" in row and "contentHash" in row
    }
