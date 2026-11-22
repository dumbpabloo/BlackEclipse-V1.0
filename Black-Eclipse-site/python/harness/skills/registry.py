"""Skill registry — the single source of truth for installed skills.

The registry is in-memory over the library + the `SkillStore` (lifecycle state).
It answers every question the runtime asks:

  * what skills exist / are enabled / are available to an actor?
  * which version should be used?
  * are a skill's tools, permissions and dependencies satisfied?
  * are there duplicate ids, invalid versions, missing/disabled deps, cycles?

It deliberately distinguishes INSTALLED (on disk) from VALIDATED (manifest well-
formed + deps resolvable) from ENABLED (approved) from AVAILABLE (enabled and
within an actor's permissions). A skill can be installed and valid yet not
enabled — that is the normal state.

There is exactly one registry. Nothing else may load or index skills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .loader import LoadedSkill, load_catalog
from .manifest import (
    EXECUTABLE_STATUSES,
    SkillManifest,
    parse_version,
    version_satisfies,
)
from .permissions import Actor, PermissionDecision, check_permissions
from .store import SkillStore


def _source_base_version(store: Any, manifest: SkillManifest) -> Optional[str]:
    """The declared base for a NEW catalog/source version.

    A source bump (e.g. `1.1.0` → `1.1.1`) is still a lineage edge. Without this,
    the new row carried `baseVersion = NULL`, so an evaluation could only compare
    it against itself (or fall back to "any prior version") instead of a declared
    base. Returns the highest stored version LOWER than the source version; a
    first version, an equal version or an unparseable one has no base.
    """
    try:
        current = parse_version(manifest.version)
        candidates: list[tuple[tuple[int, int, int, str], str]] = []
        for row in store.list_versions(manifest.id):
            version = str(row.get("version") or "")
            if not version or version == manifest.version:
                continue
            try:
                parsed = parse_version(version)
            except ValueError:
                continue
            if parsed < current:
                candidates.append((parsed, version))
    except Exception:  # noqa: BLE001 - lineage is best-effort, never a boot failure
        return None
    if not candidates:
        return None
    return max(candidates)[1]


def _safe_instructions(loaded: LoadedSkill) -> str:
    """Best-effort instruction text (file or inline) for version identity."""
    try:
        return loaded.load_instructions()
    except OSError:
        return ""


@dataclass
class DependencyResolution:
    """The result of resolving a skill's dependency graph.

    `order` is a topological execution order (dependencies first, the requested
    skill last). `errors` non-empty ⇒ the skill may not execute.
    """

    ok: bool
    order: list[tuple[str, str]] = field(default_factory=list)  # (skillId, version)
    errors: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    cycles: list[str] = field(default_factory=list)
    version_mismatches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "order": [{"skillId": s, "version": v} for s, v in self.order],
            "errors": self.errors,
            "missing": self.missing,
            "disabled": self.disabled,
            "cycles": self.cycles,
            "versionMismatches": self.version_mismatches,
        }


class SkillRegistry:
    def __init__(self, store: Optional[SkillStore] = None) -> None:
        self.store = store or SkillStore()
        self._skills: dict[str, LoadedSkill] = {}
        self._duplicates: list[str] = []
        # Skill ids a higher tier legally shadowed (project/profile over bundled).
        self._shadowed: list[str] = []
        # Untrusted-project notices from the last load (names only). Surfaced
        # to the UI/diagnostics; never a loaded skill.
        self._project_notices: list[str] = []
        # True when a trusted project root's content changed (quarantined).
        self._project_drift: bool = False
        # Per-skill project drift detail (Phase 2 final hardening): the changed
        # skills held back and the unchanged skills still loaded.
        self._project_changed_ids: list[str] = []
        self._project_quarantined_ids: list[str] = []
        self._project_unchanged_ids: list[str] = []
        # Advisory content-scan findings by skill id, and skills held back for a
        # quarantine-severity finding. Both are informational; the permission
        # gate remains authoritative.
        self._content_findings: dict[str, dict[str, Any]] = {}
        self._quarantined: list[str] = []
        # Installed (GitHub) tier: notices + per-skill drift/quarantine, kept
        # separate from the project fields so the UI can tell the two roots apart.
        self._installed_notices: list[str] = []
        self._installed_changed_ids: list[str] = []
        self._installed_quarantined_ids: list[str] = []
        # The kwargs of the last `load_all`, so `reload()` (after an install /
        # trust decision) re-reads every tier — installs, project, profile,
        # bundled — without a caller having to restate the configuration.
        self._load_kwargs: dict[str, Any] = {}

    # ── Registration / loading ──────────────────────────────────────────────

    def register(self, loaded: LoadedSkill) -> None:
        """Register one skill. Duplicate ids are refused — first wins, and the
        duplicate is recorded and surfaced (never silently dropped).

        An advisory content scan runs here. A *bundled* skill that trips a
        quarantine-severity rule is registered but warned (it is human-reviewed
        source). A **project, profile, or generated** skill that trips one is
        QUARANTINED — not registered — because untrusted tiers must not reach the
        model with instruction-override prose. The permission/execution gate
        remains authoritative regardless.
        """
        skill_id = loaded.manifest.id
        if skill_id in self._skills:
            self._duplicates.append(skill_id)
            return
        try:
            from .content_scan import scan_loaded_skill  # noqa: PLC0415

            scan = scan_loaded_skill(loaded)
        except Exception:  # noqa: BLE001 - scanning must never break loading
            scan = None
        if scan is not None and scan.findings:
            self._content_findings[skill_id] = scan.to_dict()
            tier = getattr(loaded, "provenance", "bundled")
            if scan.should_quarantine and tier in ("project", "profile", "generated", "external"):
                self._quarantined.append(skill_id)
                try:
                    self.store.append_event(
                        "skill.content_quarantined",
                        skill_id=skill_id,
                        data={"provenance": tier, "categories": scan.categories},
                    )
                except Exception:  # noqa: BLE001 - audit is best-effort
                    pass
                return
        self._skills[skill_id] = loaded

    def load_library(self, root: Optional[str] = None) -> list[str]:
        """Load the shipped library into the registry. Returns load errors
        (malformed manifests) without aborting the whole load."""
        errors: list[str] = []
        for loaded in load_catalog(root):
            self.register(loaded)
        return errors

    def load_all(
        self,
        root: Optional[str] = None,
        *,
        project_root: Optional[str] = None,
        project_discovery: bool = True,
        installed_home: Optional[str] = None,
    ) -> list[str]:
        """Load the library and persist each skill/version to the store.

        A version already present is skipped (immutability). This is the boot
        path; it is idempotent.

        Precedence (deterministic, first-wins): **project > profile (user) >
        installed (external) > bundled**. Project skills are registered ONLY for
        a trusted root; an untrusted root contributes a notice, never a skill.
        Installed (GitHub) skills load only from TRUSTED install roots (the
        install approval records that trust) and carry the `external` tier so a
        quarantine-severity content finding holds them back. A skill still runs
        through the same permission/execution gate regardless of tier.
        """
        self._load_kwargs = {
            "root": root,
            "project_root": project_root,
            "project_discovery": project_discovery,
            "installed_home": installed_home,
        }
        self._skills = {}
        self._duplicates = []
        self._shadowed = []
        self._project_drift = False
        self._project_changed_ids = []
        self._project_quarantined_ids = []
        self._project_unchanged_ids = []
        self._content_findings = {}
        self._quarantined = []
        self._installed_notices = []
        self._installed_changed_ids = []
        self._installed_quarantined_ids = []
        errors: list[str] = []

        # 1. Project skills (highest precedence) — trusted roots only.
        if project_root and project_discovery:
            from .project_skills import discover_project_skills  # noqa: PLC0415

            discovery = discover_project_skills(self.store, project_root)
            for loaded in discovery.trusted_skills:
                loaded.provenance = "project"
                self.register(loaded)
            self._project_notices = list(discovery.notices)
            # A trusted root whose content changed: only the changed skills are
            # quarantined (per-skill identity); unchanged skills remain loaded.
            self._project_changed_ids = list(discovery.changed_skill_ids)
            self._project_quarantined_ids = list(discovery.quarantined_skill_ids)
            self._project_unchanged_ids = list(discovery.unchanged_skill_ids)
            if discovery.drift:
                self._project_drift = True
                try:
                    self.store.append_event(
                        "skill.project_drift",
                        data={
                            "path": discovery.root,
                            "changedSkillIds": discovery.changed_skill_ids,
                            "quarantinedSkillIds": discovery.quarantined_skill_ids,
                        },
                    )
                except Exception:  # noqa: BLE001 - audit is best-effort
                    pass
        else:
            self._project_notices = []
            self._project_drift = False
            self._project_changed_ids = []
            self._project_quarantined_ids = []
            self._project_unchanged_ids = []

        # 2. User-authored skills (profile tier).
        for user_skill in self.store.list_user_skills():
            try:
                manifest = SkillManifest(**user_skill["manifest"])
            except Exception as exc:  # noqa: BLE001 - a bad row must not kill boot
                errors.append(f"user skill {user_skill['id']!r} has an invalid manifest: {exc}")
                continue
            self.register(LoadedSkill(
                manifest=manifest,
                folder="",
                instructions_path=None,
                instructions_text=user_skill["instructions"],
                authored=True,
                provenance="profile",
            ))

        # 3. Installed (GitHub) skills — trusted install roots only, `external`
        # tier. They sit BELOW project/profile (an operator's own or authored
        # skill always wins) and ABOVE bundled (installing a skill may
        # deliberately shadow a shipped one).
        if installed_home:
            from .install import discover_installed_skills  # noqa: PLC0415

            installed, notices, changed, quarantined = discover_installed_skills(
                self.store, installed_home,
            )
            for loaded in installed:
                loaded.provenance = "external"
                existing = self._skills.get(loaded.manifest.id)
                if existing is not None and existing.provenance in ("project", "profile"):
                    self._shadowed.append(loaded.manifest.id)
                    continue
                self.register(loaded)
            self._installed_notices = list(notices)
            self._installed_changed_ids = list(changed)
            self._installed_quarantined_ids = list(quarantined)

        # 4. Bundled library (lowest precedence). A higher tier
        # (project/profile/installed) legitimately shadows a bundled skill of the
        # same id — that is expected precedence, not a duplicate-error. Track it
        # separately.
        for loaded in load_catalog(root):
            existing = self._skills.get(loaded.manifest.id)
            if existing is not None:
                # Shadowing by a higher tier is expected precedence; a second
                # bundled definition of the same id is a true duplicate error.
                if existing.provenance in ("project", "profile", "external"):
                    self._shadowed.append(loaded.manifest.id)
                else:
                    self._duplicates.append(loaded.manifest.id)
                continue
            loaded.provenance = "bundled"
            self.register(loaded)

        if self._duplicates:
            for dup in self._duplicates:
                errors.append(f"duplicate skill id {dup!r} — later definition ignored")

        # Persist Skill + version rows for BOTH built-in and user skills. A user
        # skill's stored lifecycle status (set via enable/disable) is preserved:
        # we never reset it back to the manifest default on reload.
        for loaded in self._skills.values():
            manifest = loaded.manifest
            existing = self.store.get_skill(manifest.id)
            # authored + project + installed + BUNDLED skills are lifecycle-managed
            # by the store; a reload must not reset an operator's enable/disable
            # decision. Before this, a bundled kit could be enabled and was silently
            # walked back to `draft` on the next reload, so the drafting kits could
            # never actually be promoted.
            managed = loaded.authored or loaded.provenance in ("project", "external", "bundled")
            status = existing["status"] if existing and managed else manifest.status
            # Reconcile the in-memory manifest with the persisted status so a
            # reload reflects an operator's enable/disable. Without this the store
            # said `enabled` while the live manifest still said `draft`, so
            # discovery/executability stayed wrong within the session.
            if managed and status != manifest.status:
                try:
                    loaded.manifest.status = status
                except Exception:  # noqa: BLE001 - a frozen manifest must not break the load
                    pass
            # A version created by a structured patch may be NEWER than the
            # catalog/UserSkill source manifest. The store's own SkillVersion rows
            # are authoritative for the active version, so a reload never walks
            # the skill back to an older folder/row version.
            active_manifest, active_version = self._active_manifest(manifest, existing)
            self.store.upsert_skill(
                skill_id=manifest.id,
                name=active_manifest.name,
                description=active_manifest.description,
                category=active_manifest.category,
                latest_version=active_version,
                status=status,
                tags=active_manifest.tags,
            )
            # Register the active (possibly patched) manifest in memory so
            # discovery/evaluation see the authoritative version.
            if active_version != manifest.version and active_manifest is not manifest:
                loaded = LoadedSkill(
                    manifest=active_manifest,
                    folder=loaded.folder,
                    instructions_path=loaded.instructions_path,
                    instructions_text=loaded.instructions_text,
                    authored=loaded.authored,
                    provenance=loaded.provenance,
                )
                self._skills[manifest.id] = loaded
                manifest = active_manifest
            try:
                self.store.save_version(
                    manifest.model_dump(), manifest.version_identity(_safe_instructions(loaded)),
                    instructions=loaded.instructions_text,
                    base_version=_source_base_version(self.store, manifest),
                )
            except ValueError:
                # Immutable version already stored. A version's persisted identity
                # is `version_identity(manifest, instructions)`, so it covers the
                # behavior-affecting inputs. Drift is reported only when BOTH the
                # current identity and the legacy manifest-only hash differ from
                # the stored hash — a legacy hash (pre-dating instruction-aware
                # identity) still identifies behavior via the stored `instructions`
                # column, so it is not treated as drift.
                try:
                    existing_version = self.store.get_version(manifest.id, manifest.version)
                    if existing_version:
                        stored = existing_version.get("contentHash")
                        # Lifecycle fields are stored in their own columns and are
                        # changed by activation/rollback/evaluation — NOT by editing
                        # the skill. Fold their STORED values back in before hashing,
                        # or an activation would look like a content change on every
                        # later reload.
                        stored_manifest = existing_version.get("manifest") or {}
                        pinned = {
                            field: stored_manifest[field]
                            for field in ("status", "evaluationStatus")
                            if field in stored_manifest
                        }
                        identity = manifest.model_copy(update=pinned).version_identity(
                            _safe_instructions(loaded)
                        )
                        if stored not in (identity, manifest.content_hash()):
                            drift = (
                                f"skill {manifest.id!r} version {manifest.version!r} content changed "
                                "without a version bump — bump the version so the behavior change is traceable"
                            )
                            errors.append(drift)
                            self.store.append_event(
                                "skill.version_drift", skill_id=manifest.id, version=manifest.version,
                                data={"storedHash": stored, "currentHash": identity},
                            )
                except Exception:  # noqa: BLE001 - drift detection must not break boot
                    pass
            # Persist the skill's deterministic cases (idempotent: replace the
            # existing set for this version so a case edit takes effect).
            cases = loaded.load_cases()
            if cases:
                self.store.replace_evaluation_cases(manifest.id, manifest.version, cases)
        return errors

    # ── User authoring ──────────────────────────────────────────────────────

    def _active_manifest(
        self, source_manifest: "SkillManifest", existing: Optional[dict[str, Any]],
    ) -> tuple["SkillManifest", str]:
        """Resolve the authoritative active manifest for a skill.

        A structured patch (Milestone 2) creates a newer `SkillVersion` row while
        the catalog/UserSkill source still names the older version. The store's
        `latestVersion` (advanced by activation/rollback) is authoritative, so a
        reload must NOT walk the skill back to the source version. Falls back to
        the source manifest when the stored version cannot be loaded.
        """
        if not existing:
            return source_manifest, source_manifest.version
        latest = existing.get("latestVersion")
        if not latest or latest == source_manifest.version:
            return source_manifest, source_manifest.version
        stored = None
        try:
            stored = self.store.get_version(source_manifest.id, latest)
        except Exception:  # noqa: BLE001
            stored = None
        if not stored:
            return source_manifest, source_manifest.version
        try:
            patched = SkillManifest(**stored["manifest"])
        except Exception:  # noqa: BLE001 - an invalid stored patch falls back safely
            return source_manifest, source_manifest.version
        return patched, latest
    def create_user_skill(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
        category: str = "research",
        tags: Optional[list[str]] = None,
        required_tools: Optional[list[str]] = None,
        permissions: Optional[list[str]] = None,
        risk_level: str = "low",
        skill_id: Optional[str] = None,
        author: str = "user",
    ) -> dict[str, Any]:
        """Validate and persist a new user skill, then register it live.

        The skill is created `enabled` (a user writing their own procedure should
        be able to use it), but only after passing the same validation a built-in
        must pass: safe permissions, real tools, a well-formed manifest.
        """
        from .authoring import SkillAuthoringError, build_user_manifest, suggest_skill_id, validate_against_tools

        existing_ids = set(self._skills.keys())
        resolved_id = skill_id or suggest_skill_id(name, existing_ids=existing_ids)
        if resolved_id in existing_ids:
            raise SkillAuthoringError(f"skill id {resolved_id!r} already exists")

        manifest = build_user_manifest(
            skill_id=resolved_id, name=name, description=description,
            instructions=instructions, category=category, tags=tags,
            required_tools=required_tools, permissions=permissions,
            risk_level=risk_level, status="enabled", author=author,
        )
        validate_against_tools(manifest, self._tool_names())

        self.store.save_user_skill({
            "id": manifest.id, "name": manifest.name, "description": manifest.description,
            "category": manifest.category, "tags": manifest.tags, "instructions": instructions,
            "manifest": manifest.model_dump(), "version": manifest.version,
            "status": "enabled", "author": author,
        })
        self._materialise_manifest(manifest, instructions)
        self._register_user_manifest(manifest, instructions)
        return {"skillId": manifest.id, "version": manifest.version, "status": "enabled"}

    def update_user_skill(
        self, skill_id: str, **patch: Any
    ) -> dict[str, Any]:
        """Edit a user skill. Always produces a NEW version — an enabled version
        is never mutated in place. Built-in skills cannot be edited."""
        from .authoring import SkillAuthoringError, build_user_manifest, next_version, validate_against_tools

        record = self.store.get_user_skill(skill_id)
        if record is None:
            raise SkillAuthoringError(f"{skill_id!r} is not a user skill (built-ins cannot be edited)")

        new_version = next_version(record["version"])
        merged = {
            "name": patch.get("name", record["name"]),
            "description": patch.get("description", record["description"]),
            "instructions": patch.get("instructions", record["instructions"]),
            "category": patch.get("category", record["category"]),
            "tags": patch.get("tags", record["tags"]),
            "required_tools": patch.get("requiredTools", record["manifest"].get("requiredTools")),
            "permissions": patch.get("permissions", record["manifest"].get("permissions")),
            "risk_level": patch.get("riskLevel", record["manifest"].get("riskLevel", "low")),
        }
        current_status = self.store.get_skill(skill_id)
        status = current_status["status"] if current_status else record["status"]

        manifest = build_user_manifest(
            skill_id=skill_id, version=new_version, status=status, author=record.get("author") or "user",
            **merged,
        )
        validate_against_tools(manifest, self._tool_names())

        self.store.save_user_skill({
            "id": skill_id, "name": manifest.name, "description": manifest.description,
            "category": manifest.category, "tags": manifest.tags, "instructions": merged["instructions"],
            "manifest": manifest.model_dump(), "version": new_version,
            "status": status, "author": record.get("author"),
        })
        self._materialise_manifest(manifest, merged["instructions"])
        self.store.set_latest_version(skill_id, new_version)
        self.store.append_event("skill.user_updated", skill_id=skill_id, version=new_version)
        self._register_user_manifest(manifest, merged["instructions"])
        return {"skillId": skill_id, "version": new_version, "status": status}

    def delete_user_skill(self, skill_id: str) -> dict[str, Any]:
        """Delete a user skill. Built-ins cannot be deleted. A user skill that
        has been referenced by a run is retained for audit instead (marked
        deprecated) — history is never destroyed silently."""
        from .authoring import SkillAuthoringError

        if self.store.get_user_skill(skill_id) is None:
            raise SkillAuthoringError(f"{skill_id!r} is not a user skill (built-ins cannot be deleted)")
        referenced = any(e["skillId"] == skill_id for e in self.store.list_executions(skill_id, limit=1))
        if referenced:
            self.store.set_version_status(skill_id, self.resolve_version(skill_id) or "1.0.0", "deprecated")
            return {"skillId": skill_id, "status": "deprecated", "deleted": False,
                    "reason": "skill has execution history; deprecated instead of deleted"}
        self.store.delete_user_skill(skill_id)
        self.store.delete_skill_records(skill_id)
        self._skills.pop(skill_id, None)
        self.store.append_event("skill.user_deleted", skill_id=skill_id)
        return {"skillId": skill_id, "deleted": True}

    def _materialise_manifest(self, manifest: SkillManifest, instructions: Optional[str]) -> None:
        """Write a user skill's derived Skill/SkillVersion rows immediately, so
        it is fully usable in the same session (not only after a reload)."""
        self.store.upsert_skill(
            skill_id=manifest.id, name=manifest.name, description=manifest.description,
            category=manifest.category, latest_version=manifest.version,
            status=manifest.status, tags=manifest.tags,
        )
        try:
            self.store.save_version(
                manifest.model_dump(), manifest.version_identity(instructions), instructions=instructions,
            )
        except ValueError:
            pass  # version already materialised

    def _register_user_manifest(self, manifest: SkillManifest, instructions: str) -> None:
        self._skills[manifest.id] = LoadedSkill(
            manifest=manifest, folder="", instructions_path=None,
            instructions_text=instructions, authored=True,
        )

    # ── Lookups ─────────────────────────────────────────────────────────────

    def get(self, skill_id: str) -> Optional[LoadedSkill]:
        return self._skills.get(skill_id)

    def unregister(self, skill_id: str) -> bool:
        """Remove a live skill. Used by plugin unload; stored/user skills stay."""
        return self._skills.pop(skill_id, None) is not None

    def load_stored(self, skill_id: str) -> Optional[LoadedSkill]:
        """Load a store-only skill (a learning draft) into the in-memory catalog.

        Drafts have no installable instruction file, so `load_all` cannot see
        them. This materializes the stored manifest/instructions on demand so
        discovery, view and evaluation can operate on the same skill.
        """
        loaded = self._skills.get(skill_id)
        if loaded is not None:
            return loaded
        record = self.store.get_skill(skill_id)
        if not record:
            return None
        row = self.store.get_version(skill_id, str(record.get("latestVersion") or ""))
        if not row:
            return None
        try:
            manifest = SkillManifest.model_validate(row.get("manifest") or {})
        except Exception:  # noqa: BLE001 - an unreadable draft is not loaded
            return None
        self._register_user_manifest(manifest, str(row.get("instructions") or ""))
        return self._skills.get(skill_id)

    def manifest(self, skill_id: str) -> Optional[SkillManifest]:
        loaded = self._skills.get(skill_id)
        return loaded.manifest if loaded else None

    def all_manifests(self) -> list[SkillManifest]:
        return [entry.manifest for entry in self._skills.values()]

    def duplicate_ids(self) -> list[str]:
        return list(self._duplicates)

    def shadowed_ids(self) -> list[str]:
        """Bundled skill ids legally shadowed by a project/profile skill."""
        return list(getattr(self, "_shadowed", []))

    def project_notices(self) -> list[str]:
        """Untrusted-project notices from the last load (names only, no prose)."""
        return list(getattr(self, "_project_notices", []))

    def project_drift(self) -> bool:
        """True when a trusted project root is quarantined for content drift."""
        return bool(getattr(self, "_project_drift", False))

    def project_changed_ids(self) -> list[str]:
        """Project skills whose content changed since approval (quarantined)."""
        return list(getattr(self, "_project_changed_ids", []))

    def project_quarantined_ids(self) -> list[str]:
        """Project skills held back pending re-approval (subset of changed)."""
        return list(getattr(self, "_project_quarantined_ids", []))

    def project_unchanged_ids(self) -> list[str]:
        """Project skills whose content still matches the approved hash."""
        return list(getattr(self, "_project_unchanged_ids", []))

    def installed_notices(self) -> list[str]:
        """Installed-tier notices from the last load (untrusted/drift/errors)."""
        return list(getattr(self, "_installed_notices", []))

    def installed_changed_ids(self) -> list[str]:
        """Installed skills whose content changed since install (quarantined)."""
        return list(getattr(self, "_installed_changed_ids", []))

    def installed_quarantined_ids(self) -> list[str]:
        """Installed skills held back pending re-approval (subset of changed)."""
        return list(getattr(self, "_installed_quarantined_ids", []))

    def reload(self, **overrides: Any) -> list[str]:
        """Re-run the last `load_all` configuration (plus any overrides).

        The ONE reload seam after an install/uninstall/trust decision, so every
        tier — installs, project, profile, bundled — is re-read together and a
        caller never has to restate the boot configuration.
        """
        kwargs = {**self._load_kwargs, **overrides}
        return self.load_all(**kwargs)

    def content_findings(self) -> dict[str, dict[str, Any]]:
        """Advisory content-scan findings by skill id (never proof of safety)."""
        return dict(getattr(self, "_content_findings", {}))

    def quarantined_ids(self) -> list[str]:
        """Skill ids held back for a quarantine-severity content finding."""
        return list(getattr(self, "_quarantined", []))

    def provenance_of(self, skill_id: str) -> Optional[str]:
        entry = self._skills.get(skill_id)
        return entry.provenance if entry else None

    def resolve_version(self, skill_id: str, version: Optional[str] = None) -> Optional[str]:
        """Resolve the concrete version to use. An explicit version must exist;
        otherwise the skill's latest stored version is used."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return None
        if version:
            stored = self.store.get_version(skill_id, version)
            return version if stored else None
        record = self.store.get_skill(skill_id)
        if record and record.get("latestVersion"):
            return str(record["latestVersion"])
        return skill.manifest.version

    # ── Discovery / search ──────────────────────────────────────────────────

    def discover(
        self,
        *,
        query: str = "",
        category: Optional[str] = None,
        tags: Optional[list[str]] = None,
        status: Optional[str] = None,
        risk_level: Optional[str] = None,
        actor: Optional[Actor] = None,
        include_non_executable: bool = False,
    ) -> list[dict[str, Any]]:
        """Deterministic discovery over DISCOVERY METADATA only (never prose).

        Filters by category/status/risk/tags and a keyword match over
        id/name/description/tags. Permission filtering is applied when an actor
        is supplied. Ordering is stable: exact id match first, then name, then
        id — so discovery is reproducible.
        """
        terms = [t for t in query.lower().split() if t]
        results: list[dict[str, Any]] = []
        for entry in self._skills.values():
            manifest = entry.manifest
            if not include_non_executable and manifest.status in {"rejected", "deprecated", "archived"}:
                continue
            if category and manifest.category != category:
                continue
            if status and manifest.status != status:
                continue
            if risk_level and manifest.riskLevel != risk_level:
                continue
            if tags:
                wanted = {t.lower() for t in tags}
                if not wanted.issubset({t.lower() for t in manifest.tags}):
                    continue
            if terms:
                haystack = " ".join(
                    [manifest.id, manifest.name, manifest.description, *manifest.tags]
                ).lower()
                if not all(term in haystack for term in terms):
                    continue
            if actor is not None:
                decision = check_permissions(manifest.permissions, actor)
                if not decision.allowed:
                    continue
            metadata = manifest.discovery_metadata()
            metadata["executable"] = manifest.status in EXECUTABLE_STATUSES
            results.append(metadata)

        def sort_key(item: dict[str, Any]) -> tuple[int, str]:
            if query and item["id"].lower() == query.lower():
                return (0, item["id"])
            if query and query.lower() in item["name"].lower():
                return (1, item["id"])
            return (2, item["id"])

        results.sort(key=sort_key)
        return results

    def list_summaries(self) -> list[dict[str, Any]]:
        """Every installed skill with its stored lifecycle status and latest
        version, for the library UI. Stored status wins over the file's, since
        enable/disable/deprecate mutate the store."""
        out: list[dict[str, Any]] = []
        for entry in self._skills.values():
            manifest = entry.manifest
            record = self.store.get_skill(manifest.id)
            summary = manifest.discovery_metadata()
            if record:
                summary["status"] = record["status"]
                summary["latestVersion"] = record["latestVersion"]
                # Drives the Library's "Recently updated" sort. Older rows /
                # store-less manifests read None and sort last.
                summary["updatedAt"] = record.get("updatedAt")
            summary["versions"] = [v["version"] for v in self.store.list_versions(manifest.id)]
            summary["origin"] = manifest.origin
            summary["provenance"] = entry.provenance
            out.append(summary)
        out.sort(key=lambda item: item["id"])
        return out

    # ── Validation ──────────────────────────────────────────────────────────

    def validate(self, skill_id: str, version: Optional[str] = None) -> dict[str, Any]:
        """Full manifest/dependency/tool/permission validation for a skill.

        Returns a structured report; `ok` is True only when every mandatory
        check passes. A skill that fails validation cannot be enabled.
        """
        manifest = self.manifest(skill_id)
        if manifest is None:
            return {"ok": False, "skillId": skill_id, "checks": [], "errors": ["skill not installed"]}

        checks: list[dict[str, Any]] = []
        errors: list[str] = []

        checks.append({"check": "manifest", "ok": True, "detail": "pydantic-valid"})

        forbidden = manifest.forbidden_permissions
        checks.append({
            "check": "permissions",
            "ok": not forbidden,
            "detail": f"blocked: {', '.join(forbidden)}" if forbidden else "within allowed vocabulary",
        })
        if forbidden:
            errors.append(f"declares blocked permissions: {', '.join(forbidden)}")

        dep_result = self.resolve_dependencies(skill_id, version)
        checks.append({
            "check": "dependencies",
            "ok": dep_result.ok,
            "detail": dep_result.errors or "resolved",
        })
        errors.extend(dep_result.errors)

        tools_ok, tool_detail = self.check_tools(skill_id, version)
        checks.append({"check": "tools", "ok": tools_ok, "detail": tool_detail})
        if not tools_ok:
            errors.append(tool_detail)

        schema_ok, schema_detail = self._check_schemas(manifest)
        checks.append({"check": "schemas", "ok": schema_ok, "detail": schema_detail})
        if not schema_ok:
            errors.append(schema_detail)

        return {
            "ok": not errors,
            "skillId": skill_id,
            "version": version or manifest.version,
            "checks": checks,
            "errors": errors,
        }

    @staticmethod
    def _check_schemas(manifest: SkillManifest) -> tuple[bool, str]:
        if manifest.inputSchema.get("type") != "object":
            return False, "inputSchema must be an object schema"
        if manifest.outputSchema is not None and not isinstance(manifest.outputSchema, dict):
            return False, "outputSchema must be an object"
        from .schema_validation import check_schema  # noqa: PLC0415

        input_ok, input_errors = check_schema(manifest.inputSchema)
        if not input_ok:
            return False, f"inputSchema is not a valid JSON Schema: {'; '.join(input_errors)}"
        if manifest.outputSchema is not None:
            output_ok, output_errors = check_schema(manifest.outputSchema)
            if not output_ok:
                return False, f"outputSchema is not a valid JSON Schema: {'; '.join(output_errors)}"
        return True, "input/output schemas are valid JSON Schema"

    def check_tools(self, skill_id: str, version: Optional[str] = None) -> tuple[bool, str]:
        """Every required tool must be registered in the runtime's tool registry.
        The registry is injected lazily to avoid an import cycle."""
        manifest = self.manifest(skill_id)
        if manifest is None:
            return False, "skill not installed"
        available = set(self._tool_names())
        missing = [t for t in manifest.requiredTools if t not in available]
        if missing:
            return False, f"required tools not registered: {', '.join(missing)}"
        return True, "all required tools are registered"

    # The runtime tool registry is attached by the service after construction.
    _tool_registry: Any = None

    def attach_tool_registry(self, registry: Any) -> None:
        self._tool_registry = registry

    def _tool_names(self) -> list[str]:
        if self._tool_registry is not None:
            return list(self._tool_registry.names())
        return []

    # ── Dependency resolution ───────────────────────────────────────────────

    def resolve_dependencies(
        self, skill_id: str, version: Optional[str] = None
    ) -> DependencyResolution:
        """Resolve the dependency graph. Deterministic order: dependencies
        before dependents. Detects missing, disabled, version-mismatched and
        circular dependencies. Bounded (each skill visited once)."""
        result = DependencyResolution(ok=True)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(current_id: str, constraint: Optional[str], chain: list[str], *, root: bool = False) -> bool:
            if current_id in visiting:
                cycle = " -> ".join([*chain, current_id])
                result.cycles.append(cycle)
                return False
            entry = self._skills.get(current_id)
            if entry is None:
                result.missing.append(current_id)
                return False
            resolved_version = self.resolve_version(current_id, None)
            if resolved_version and constraint and not version_satisfies(resolved_version, constraint):
                result.version_mismatches.append(
                    f"{current_id} {resolved_version} does not satisfy {constraint}"
                )
                return False
            record = self.store.get_skill(current_id)
            stored_status = record["status"] if record else entry.manifest.status
            # A skill's OWN current status is not a dependency of itself: the root
            # being validated for (re-)enablement may legitimately be disabled,
            # deprecated or archived. Only its DEPENDENCIES must be executable.
            if not root and stored_status not in EXECUTABLE_STATUSES:
                result.disabled.append(f"{current_id} is {stored_status}")
                return False
            if current_id in visited:
                return True
            visiting.add(current_id)
            for dep in entry.manifest.requiredSkills:
                if dep.optional:
                    continue
                if not visit(dep.skillId, dep.version, [*chain, current_id]):
                    if current_id in visiting:
                        visiting.discard(current_id)
                    return False
            visiting.discard(current_id)
            visited.add(current_id)
            result.order.append((current_id, resolved_version or entry.manifest.version))
            return True

        ok = visit(skill_id, None, [], root=True)
        result.ok = ok and not (
            result.missing or result.disabled or result.cycles or result.version_mismatches
        )
        if result.cycles:
            result.errors.append("circular dependency detected: " + "; ".join(result.cycles))
        for missing in result.missing:
            result.errors.append(f"missing dependency: {missing}")
        for disabled in result.disabled:
            result.errors.append(f"dependency not executable: {disabled}")
        for mismatch in result.version_mismatches:
            result.errors.append(f"version mismatch: {mismatch}")
        return result

    def dependency_graph(self, skill_id: str, version: Optional[str] = None) -> dict[str, Any]:
        """A structured, version-rich dependency view for the UI/preflight.

        Every node carries its id, resolved version, and a per-dependency status
        (ok/missing/disabled/version-mismatch/optional). Deterministic and
        bounded. This is a READ model; it never enables a dependency.
        """
        manifest = self.manifest(skill_id)
        if manifest is None:
            return {"skillId": skill_id, "ok": False, "nodes": [], "edges": [], "errors": ["skill not installed"]}
        nodes: dict[str, dict[str, Any]] = {}

        def node_for(current_id: str) -> dict[str, Any]:
            entry = self._skills.get(current_id)
            record = self.store.get_skill(current_id)
            stored_status = record["status"] if record else (entry.manifest.status if entry else "missing")
            resolved = self.resolve_version(current_id, None)
            node = {
                "id": current_id,
                "version": resolved or (entry.manifest.version if entry else None),
                "status": stored_status if entry else "missing",
                "executable": stored_status in EXECUTABLE_STATUSES if entry else False,
                "missing": entry is None,
                "requiredTools": list(entry.manifest.requiredTools) if entry else [],
                "permissions": list(entry.manifest.permissions) if entry else [],
            }
            nodes[current_id] = node
            return node

        edges: list[dict[str, Any]] = []
        visiting: set[str] = set()

        def walk(current_id: str) -> None:
            if current_id in visiting:
                return  # a cycle is reported below via resolve_dependencies()
            visiting.add(current_id)
            node_for(current_id)
            entry = self._skills.get(current_id)
            if entry is None:
                visiting.discard(current_id)
                return
            for dep in entry.manifest.requiredSkills:
                dep_node = node_for(dep.skillId)
                resolved = dep_node.get("version")
                compatible = True
                if dep.version and resolved and not version_satisfies(resolved, dep.version):
                    compatible = False
                if dep.optional:
                    status = "optional"
                elif dep_node.get("missing"):
                    status = "missing"
                elif not dep_node.get("executable"):
                    status = "disabled"
                elif not compatible:
                    status = "incompatible"
                else:
                    status = "ok"
                edges.append({
                    "from": current_id, "to": dep.skillId,
                    "constraint": dep.version, "optional": bool(dep.optional),
                    "resolvedVersion": resolved, "status": status,
                })
                if not dep.optional:
                    walk(dep.skillId)
            visiting.discard(current_id)

        walk(skill_id)
        resolution = self.resolve_dependencies(skill_id, version)
        return {
            "skillId": skill_id,
            "ok": resolution.ok,
            "nodes": list(nodes.values()),
            "edges": edges,
            "order": [{"skillId": s, "version": v} for s, v in resolution.order],
            "errors": resolution.errors,
            "cycles": resolution.cycles,
            "missing": resolution.missing,
            "disabled": resolution.disabled,
            "versionMismatches": resolution.version_mismatches,
        }

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def set_status(self, skill_id: str, status: str, actor: Actor, version: Optional[str] = None) -> dict[str, Any]:
        """Transition a skill's lifecycle status. Enable/disable/deprecate are
        the operator-facing operations; all require the matching permission."""
        from .permissions import can_enable

        manifest = self.manifest(skill_id)
        if manifest is None:
            return {"ok": False, "error": "skill not installed"}

        if status == "enabled":
            decision: PermissionDecision = can_enable(manifest.permissions, actor)
            if not decision.allowed:
                return {"ok": False, "error": decision.reason, "permissions": decision.to_dict()}
            report = self.validate(skill_id, version)
            if not report["ok"]:
                return {"ok": False, "error": "skill failed validation", "validation": report}

        required_permission = {
            "enabled": "skill.enable",
            "disabled": "skill.disable",
            "deprecated": "skill.enable",
            "archived": "skill.enable",
        }.get(status)
        if required_permission and required_permission not in actor.granted():
            return {"ok": False, "error": f"{required_permission} requires an authorized operator"}

        target_version = version or self.resolve_version(skill_id) or manifest.version
        evaluation_status = "passed" if status == "enabled" else None
        self.store.set_version_status(skill_id, target_version, status, evaluation_status)
        # Reflect the new status on the in-memory manifest so discovery and
        # evaluation in THIS session see it without a full reload.
        loaded = self._skills.get(skill_id)
        if loaded is not None:
            try:
                loaded.manifest.status = status
            except Exception:  # noqa: BLE001 - a frozen manifest must not break the transition
                pass
        self.store.append_event(f"skill.{status}" if status in {"enabled", "disabled", "deprecated", "archived"} else "skill.status_changed",
                                skill_id=skill_id, version=target_version,
                                data={"status": status, "actor": actor.id})
        return {"ok": True, "skillId": skill_id, "version": target_version, "status": status}

    def deprecate(self, skill_id: str, actor: Actor, reason: str, superseded_by: Optional[str] = None) -> dict[str, Any]:
        manifest = self.manifest(skill_id)
        if manifest is None:
            return {"ok": False, "error": "skill not installed"}
        if "skill.enable" not in actor.granted():
            return {"ok": False, "error": "skill.enable requires an authorized operator"}
        target_version = self.resolve_version(skill_id) or manifest.version
        self.store.set_version_status(skill_id, target_version, "deprecated")
        self.store.save_review(
            skill_id=skill_id, version=target_version, actor=actor.id,
            decision="deprecated", notes=reason,
        )
        self.store.append_event(
            "skill.disabled", skill_id=skill_id, version=target_version,
            data={"reason": reason, "supersededBy": superseded_by},
        )
        return {"ok": True, "skillId": skill_id, "version": target_version, "status": "deprecated"}

    # ── History ─────────────────────────────────────────────────────────────

    def version_history(self, skill_id: str) -> list[dict[str, Any]]:
        return [
            {
                "version": v["version"],
                "status": v["status"],
                "evaluationStatus": v["evaluationStatus"],
                "contentHash": v["contentHash"],
                "createdAt": v["createdAt"],
                "origin": v["origin"],
                "author": v["author"],
                "pinned": bool(v.get("pinned")),
            }
            for v in self.store.list_versions(skill_id)
        ]

    def evaluation_history(self, skill_id: str) -> list[dict[str, Any]]:
        return self.store.list_evaluations(skill_id)

    def execution_history(self, skill_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_executions(skill_id, limit)


__all__ = ["DependencyResolution", "SkillRegistry"]
