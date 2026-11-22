"""The ONE authority for the code capability's policy.

Nothing here executes anything. This module answers, deterministically and
without side effects: may this run read/write/execute this thing, in this
workspace, under this autonomy profile? Every later phase calls it; none of
them re-implement it.

The decisions are DATA plus small pure functions:

  * ``PROTECTED_PATHS``   — never touchable, even by an operator.
  * ``IGNORED_DIRS`` / ``IGNORED_GLOBS`` — generated or bulky paths a write
    never targets.
  * ``COMMAND_CLASSES``   — the closed command vocabulary (Phase 3 owns argv).
  * ``AUTONOMY_PROFILES`` — what each profile may do (the actor consults this).
  * ``PLAIN_LANGUAGE_DENYLIST`` — mechanism words that must never reach a
    user-facing string; the later phases' plain-language gates read it here.

Path safety is enforced by ``resolve_within``: it compares REAL paths and
refuses an escape (traversal, an absolute path outside the root, or a symlink
that leaves the root). The raw string is never trusted.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Optional, Sequence

# ── Autonomy profiles ───────────────────────────────────────────────────────

AUTONOMY_PROFILES: tuple[str, ...] = ("plan_only", "edit_verify", "approve_external")

# The per-profile grant table. Unknown autonomy grants nothing (fail-closed),
# exactly like the MCP trust tier.
_PROFILE_GRANTS: dict[str, FrozenSet[str]] = {
    "plan_only": frozenset({"code.read", "git.read"}),
    "edit_verify": frozenset(
        {"code.read", "code.write", "git.read", "code.execute", "command.execute"}
    ),
    "approve_external": frozenset(
        {
            "code.read",
            "code.write",
            "git.read",
            "code.execute",
            "command.execute",
            "git.write",
            "workspace.admin",
        }
    ),
}

# ── Protected paths — absolute, even inside a workspace, even for an operator ─

PROTECTED_PATHS: tuple[str, ...] = (
    "jdds.db",                     # the market-data vault
    "agent-memory.db",
    "eclipse-memory.db",
    ".env", "*.env",
    "mcp-servers.json",            # holds discovered env values
    "prisma/migrations/**",
    ".git/**",
    "python/harness/code/policy.py",        # FROZEN (Phase 10)
    "python/harness/skills/permissions.py", # FROZEN (Phase 10)
    "python/harness/mcp/approvals.py",      # FROZEN (Phase 10)
    "scripts/check-all.js",                 # FROZEN (Phase 10)
    "**/*.pem", "**/*.key", "**/id_rsa*",
)

IGNORED_DIRS: tuple[str, ...] = (
    ".git", "node_modules", "src/generated", "__pycache__",
    # `.next` is the production build; `.next-dev` is the `next dev` build
    # (see next.config.ts distDir). Both are generated bundles and must never be
    # indexed/searched — a dev build here is hundreds of multi-MB files, and
    # parsing them wedged the runtime.
    ".next", ".next-dev", "_backups", ".black-eclipse", "scripts/vendor",
    # Generated / build output that is never source (the CIUE §25 ignore set). A
    # build directory is excluded from indexing, searching and the walk.
    "dist", "build", "target", "coverage", "out", "vendor", "generated", ".cache",
    ".turbo", ".parcel-cache", "bin", "obj", ".venv", "venv",
)

IGNORED_GLOBS: tuple[str, ...] = ("*.db", "*.sqlite", "*.png", "*.jpg", "*.woff2", "*.zip")

# ── Command vocabulary ──────────────────────────────────────────────────────
#
# The command classes Phase 3 implements. Declared here because the POLICY owns
# the vocabulary; commands.py owns the argv templates. Every class maps to the
# set of programs it may invoke; a program outside its class's set is refused.
COMMAND_CLASSES: tuple[str, ...] = (
    "typecheck", "lint", "unit_test", "python_test",
    "build", "format_check", "git_read", "dependency_install",
)

_COMMAND_PROGRAMS: dict[str, frozenset[str]] = {
    "typecheck": frozenset({"npm", "npx", "pnpm", "yarn", "bun", "node", "tsc"}),
    "lint": frozenset({"npm", "npx", "pnpm", "yarn", "eslint", "ruff"}),
    "unit_test": frozenset({"npm", "npx", "pnpm", "yarn", "vitest", "jest", "pytest"}),
    "python_test": frozenset({"python", "python3", "pytest", "uv"}),
    "build": frozenset({"npm", "npx", "pnpm", "yarn", "node", "next", "go", "cargo", "dotnet", "javac", "mvn", "gradle", "make", "cmake", "gcc", "g++", "clang", "cl"}),
    "format_check": frozenset({"npx", "prettier", "ruff"}),
    "git_read": frozenset({"git"}),
    "dependency_install": frozenset({"npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go", "dotnet", "composer", "bundler"}),
}

# Programs that are never allowed, whatever the class. Belt and braces beside
# the per-class allowlist: a shell, a downloader, or a destructive tool.
DENIED_PROGRAMS: frozenset[str] = frozenset(
    {"rm", "rmdir", "del", "erase", "format", "mkfs", "shutdown", "curl",
     "wget", "invoke-webrequest", "powershell", "pwsh", "cmd", "sh", "bash",
     "zsh", "reg", "diskpart"}
)

NETWORK_DEFAULT: bool = False          # denied unless a class explicitly allows it

SECRET_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR",
    "LANG", "PYTHONIOENCODING", "NODE_OPTIONS",
    # Windows: the executable-extension resolution list. Without it a shell
    # cannot resolve `python.exe`/`npm.cmd` by name even when PATH contains the
    # directory (measured: a scrubbed PowerShell silently produced no output).
    # Not a secret; it is the Windows analogue of PATH.
    "PATHEXT",
)   # everything else is dropped from the child environment — never os.environ passthrough

# Words that must never appear in a user-facing string or an i18n key. The surface
# names the finance JOB, never the mechanism. Every phase that ships UI asserts
# against this list — it lives here so no phase re-types it.
PLAIN_LANGUAGE_DENYLIST: tuple[str, ...] = (
    "sandbox", "worktree", "workspace", "allowlist", "autonomy", "digest",
    "argv", "cwd", "stderr", "stdout", "patch", "diff", "manifest",
    "operator", "policy engine", "tool registry", "exit code",
    "verification ledger", "repo", "repository", "commit", "branch", "head",
)


# ── Contracts ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkspaceBinding:
    id: str
    root: Path              # absolute; must be inside the allowed workspace root
    base_revision: str      # a git rev or a snapshot digest
    strategy: str           # "worktree" | "snapshot"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str          # "OK" | "PROTECTED_PATH" | "OUTSIDE_WORKSPACE" | "PROFILE_DENIED"
                       # | "SANDBOX_UNAVAILABLE" | "COMMAND_NOT_ALLOWLISTED" | "UNKNOWN_AUTONOMY"
    reason: str        # ONE PLAIN SENTENCE safe to show a human — no secrets, no paths
                       # outside the workspace, no mechanism term from the denylist above


class PolicyDenial(Exception):
    """Raised by ``resolve_within`` — carries a machine code and a plain reason.

    ``code`` is the stable, structured refusal code (`PATH_TRAVERSAL`,
    `PATH_OUTSIDE_WORKSPACE`, `SYMLINK_ESCAPE`, `PROTECTED_PATH`, `IGNORED_PATH`,
    `NOT_FOUND`). ``reason`` is the operator/human-facing plain sentence.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


# Plain, single-sentence reasons. Safe to show a human: no secret, no outside
# path, no mechanism term. This is the only place a refusal sentence is written.
_REASON_PROTECTED = "That file is part of Eclipse's own data and can't be changed by a task."
_REASON_OUTSIDE = "That location is outside the task's own folder, so Eclipse can't use it there."
_REASON_TRAVERSAL = "That location points above the task's own folder, so Eclipse can't use it there."
_REASON_IGNORED = "That file is generated automatically, so Eclipse leaves it out."
_REASON_NOT_FOUND = "That file isn't there, so there's nothing for Eclipse to read."
_REASON_NO_BINDING = "This kind of work isn't available on this computer right now."
_REASON_PROFILE = "This task isn't set up to make that kind of change."
_REASON_UNKNOWN = "This task's permissions aren't recognized, so Eclipse won't continue."
_REASON_COMMAND = "That command isn't one Eclipse is allowed to run."
_REASON_ALLOWED = "This change is allowed inside the task's own folder."
_REASON_COMMAND_OK = "That command is allowed for this task."


# ── Path helpers ────────────────────────────────────────────────────────────


def _normalize(rel_path: str) -> str:
    """A posix-style relative path with no leading `./`, for pattern matching."""
    text = str(rel_path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _matches(rel_path: str, pattern: str) -> bool:
    """Case-insensitive glob match over a normalized relative path.

    ``fnmatch`` treats ``*`` as crossing ``/`` so a nested ``**`` pattern also
    matches its descendants; the explicit ``/**`` forms and a bare directory
    pattern are both handled here.
    """
    rel = _normalize(rel_path).casefold()
    pat = pattern.replace("\\", "/").casefold()
    if fnmatch.fnmatchcase(rel, pat):
        return True
    # A directory pattern also covers everything beneath it.
    if not pat.endswith("/**") and fnmatch.fnmatchcase(rel, pat + "/**"):
        return True
    return False


def is_protected(rel_path: str) -> bool:
    """Whether a workspace-relative path is protected (never writable)."""
    return any(_matches(rel_path, pattern) for pattern in PROTECTED_PATHS)


def is_ignored(rel_path: str) -> bool:
    """Whether a workspace-relative path is generated/bulky and never written."""
    rel = _normalize(rel_path)
    parts = rel.split("/")
    for directory in IGNORED_DIRS:
        if directory in parts:
            return True
        if _matches(rel, directory + "/**"):
            return True
    return any(_matches(rel, glob) for glob in IGNORED_GLOBS)


def _is_within(root_real: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root_real)
        return True
    except ValueError:
        return False


def _is_junction(path: Path) -> bool:
    """Whether ``path`` is a Windows junction/directory reparse point.

    A junction is NOT reported by ``Path.is_symlink()`` on Windows, so a
    junction that points outside the root would otherwise escape the symlink
    check. ``os.path.isjunction`` exists on 3.12; treat its absence as False.
    """
    checker = getattr(os.path, "isjunction", None)
    if checker is None:
        return False
    try:
        return bool(checker(path))
    except OSError:
        return False


def _symlink_escape(root_real: Path, absolute: Path) -> bool:
    """Whether a component of ``absolute`` is a link whose target leaves ``root``.

    ``absolute`` is the lexical (unresolved) candidate path. Only components
    lexically under the root are examined; a path that is already outside the
    root is reported as ``PATH_OUTSIDE_WORKSPACE`` instead, so the two escape
    vectors keep DISTINCT codes.
    """
    try:
        relative = absolute.relative_to(root_real)
    except ValueError:
        return False
    current = root_real
    for part in relative.parts:
        current = current / part
        try:
            is_link = current.is_symlink() or _is_junction(current)
        except OSError:
            is_link = False
        if is_link:
            target = Path(os.path.realpath(current))
            if not _is_within(root_real, target):
                return True
    return False


def resolve_within(root: Path, candidate: str, *, allow_ignored: bool = False) -> Path:
    """Resolve ``candidate`` inside ``root`` or raise ``PolicyDenial``.

    The comparison is always on REAL, resolved paths — never the raw string.
    Distinct codes are produced per escape vector so a caller (and a test) can
    tell them apart:

      * ``PATH_TRAVERSAL``         — a ``..`` component in the request;
      * ``SYMLINK_ESCAPE``         — a link/junction component leaving the root;
      * ``PATH_OUTSIDE_WORKSPACE`` — the resolved path is not the root or under
        it (covers an absolute path outside the root);
      * ``PROTECTED_PATH``         — ``is_protected(rel)``;
      * ``IGNORED_PATH``           — ``is_ignored(rel)``.

    ``allow_ignored`` relaxes ONLY the ``IGNORED_PATH`` rule, for a caller that
    names a real file explicitly rather than walking the tree (the SQL runtime
    opening an operator-chosen ``.db``/``.sqlite`` database, which the index
    ignore-globs exist to skip). Traversal, symlink, outside-root and PROTECTED
    checks are unchanged — so ``.git``/``.env``/keys stay unreachable.
    """
    root_real = Path(os.path.realpath(root))
    raw = Path(str(candidate).replace("\\", os.sep))
    # A `..` component is refused by the raw shape, before any resolution.
    if ".." in str(candidate).replace("\\", "/").split("/"):
        raise PolicyDenial("PATH_TRAVERSAL", _REASON_TRAVERSAL)
    absolute = raw if raw.is_absolute() else root_real / raw
    if _symlink_escape(root_real, absolute):
        raise PolicyDenial("SYMLINK_ESCAPE", _REASON_OUTSIDE)
    resolved = Path(os.path.realpath(absolute))
    if not _is_within(root_real, resolved):
        raise PolicyDenial("PATH_OUTSIDE_WORKSPACE", _REASON_OUTSIDE)
    rel = resolved.relative_to(root_real).as_posix() if resolved != root_real else "."
    if is_protected(rel):
        raise PolicyDenial("PROTECTED_PATH", _REASON_PROTECTED)
    if not allow_ignored and is_ignored(rel):
        raise PolicyDenial("IGNORED_PATH", _REASON_IGNORED)
    return resolved


# ── Grants ──────────────────────────────────────────────────────────────────


def code_grants(autonomy: str) -> frozenset[str]:
    """The code permissions a profile may hold. Unknown profile → nothing."""
    return _PROFILE_GRANTS.get(autonomy, frozenset())


# ── Tool-allowlist narrowing (Phase 5) ──────────────────────────────────────
#
# The autonomy profile is enforced by the TOOL ALLOWLIST, not by a prompt: a
# disallowed tool is REMOVED before the model sees its schema, so it cannot call
# it — not "shouldn't", cannot. This is the ONE place a write/external tool is
# removed; `graph.effective_allowlist` calls it before building the model tools.

# The execution layer's own write/execute family. It is part of WRITE_TOOLS, but
# named separately because the runtime authorizer's admission branch covers
# exactly this family plus APP_RUNTIME_WRITE_TOOLS (R3-02).
EXECUTION_WRITE_TOOLS: tuple[str, ...] = (
  # Round 3 / W3 (ADR-H282): the ONE text-edit tool. It is a WRITE tool, so
  # `plan_only` removes it and `edit_verify` keeps it - exactly like the tool
  # it replaces. A new tool that is not in this set is a tool `plan_only`
  # cannot restrain, which is the bug this line prevents.
  "code_edit",
    "execute_code", "execute_python", "execute_javascript", "execute_typescript",
    "execute_sql", "execute_shell", "build_project", "test_project",
    "run_verification", "start_service", "stop_service", "restart_service",
    "cancel_execution",
)

# Effectful code-write / execute tools (the REAL names Phases 1–9 registered).
WRITE_TOOLS: tuple[str, ...] = (
    "workspace_patch_apply", "workspace_revert",   # code.write
    "command_run",                                  # code.execute / command.execute
    "terminal_run",                                 # command.execute (generic, gated)
    "shell_run", "shell_spawn", "process_kill",     # command.execute (real shell)
    "scaffold_kit",                                 # code.write (Phase 9)
    "python_env_install", "python_env_remove",       # analysis package env (approval-gated)
    # The Code Execution & Verification Layer (Phase EX); named as its own tuple
    # above so the runtime authorizer can admit exactly this family.
    *EXECUTION_WRITE_TOOLS,
)

# The execution layer's read-only tools: always available to an allowlisted run.
EXECUTION_READ_TOOLS: tuple[str, ...] = (
    "discover_tests", "execution_get", "execution_logs", "execution_result",
    "execution_artifacts", "service_status", "service_logs", "list_services",
)

# The Application Runtime layer (DevOps). These RUN applications, install
# dependencies, and drive Docker/Podman/Compose, so they are code-write tools
# removed at `plan_only`; the read-only surface stays. Mirrors
# `execution/application/tools.py` (APP_RUNTIME_WRITE_TOOLS / _READ_TOOLS).
APP_RUNTIME_WRITE_TOOLS: tuple[str, ...] = (
    "runtime_start", "runtime_stop", "runtime_restart", "runtime_cleanup",
    "runtime_recover",
    "workspace_start", "workspace_stop",
    "package_install", "package_add", "package_remove", "package_update",
    "package_run", "package_build", "package_test",
    "container_build", "container_run", "container_start", "container_stop",
    "container_restart", "container_exec", "container_remove", "container_pull",
    "container_rmi", "container_network", "container_volume",
    "compose_build", "compose_up", "compose_down", "compose_start",
    "compose_stop", "compose_restart", "compose_exec", "compose_pull", "compose_rm",
    "environment_set", "environment_unset",
)

APP_RUNTIME_READ_TOOLS: tuple[str, ...] = (
    "runtime_detect", "runtime_capabilities", "runtime_inspect", "runtime_status",
    "runtime_health", "runtime_logs", "runtime_reconcile", "runtime_diagnose",
    "runtime_metrics", "runtime_metrics_history",
    "workspace_graph",
    "package_detect", "package_version", "package_list", "package_audit",
    "container_logs", "container_inspect", "container_stats", "container_list",
    "container_images", "container_health",
    "compose_validate", "compose_config", "compose_ps", "compose_logs",
    "environment_inspect", "environment_validate", "environment_load",
    "environment_compare",
    "port_check", "port_find_free", "port_list", "port_identify_process",
    "health_check", "health_status",
    # Orchestration / infrastructure / cloud read-only surface.
    "k8s_status", "k8s_get", "k8s_get_pods", "k8s_describe", "k8s_logs",
    "k8s_rollout", "helm_list", "helm_status", "kustomize_build",
    "terraform_init", "terraform_validate", "terraform_fmt", "terraform_plan",
    "terraform_output", "terraform_state",
    "cloud_providers", "deploy_status", "deploy_logs",
)

# Orchestration / infrastructure / cloud mutations (removed at `plan_only`, and
# each is an `external_action` requiring a single-use approval at call time).
# A production deploy and every delete/destroy additionally require authorization.
APP_RUNTIME_K8S_WRITE_TOOLS: tuple[str, ...] = (
    "k8s_apply", "k8s_delete", "k8s_scale", "k8s_exec",
    "helm_install", "helm_uninstall",
    "terraform_apply", "terraform_destroy",
    "deploy_run", "deploy_rollback", "deploy_delete", "deploy_promote",
    "k8s_port_forward", "k8s_logs_follow",
)

# The capability-acquire tools (GitHub skill install/uninstall, MCP integration
# install). They mutate what the agent can then use, so they are removed at
# `plan_only` and in the unknown-profile fallback exactly like the other write
# families; at `edit_verify` they stay (each call still requires its own
# single-use operator approval in the effect authorizer chain).
ACQUISITION_WRITE_TOOLS: tuple[str, ...] = (
    "skill_install", "skill_uninstall", "mcp_install",
    # Agent-authored extension skills (the extension layer, `skills/manage_tools.py`).
    # Declarative only (a validated manifest + bounded instructions, never code);
    # each call still requires a single-use operator approval in the effect chain.
    "skill_create", "skill_update", "skill_delete",
    # Skill lifecycle writes (`skills/lifecycle_tools.py`): evaluation, activation
    # and the learning switch. Also approval-gated in the effect chain.
    "skill_version_evaluate", "skill_version_activate", "skill_learning_set",
)

# ── Bulk data acquisition (Phase 16, first slice) ─────────────────────────────
#
# `data.collect` is a NEW noun granted ONLY by an operator-set acquisition
# profile — never by the default actor, never by `code.write`. `acquire` is the
# one tool family it grants; the surface is removed entirely when the operator's
# switch (`acquisition_enabled`) is off, and the handler re-checks the grant
# fail-closed.
ACQUISITION_PROFILES: tuple[str, ...] = ("none", "declared_sources")

_ACQUISITION_GRANTS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "declared_sources": frozenset({"data.collect"}),
}


def acquisition_grants(profile: str) -> frozenset[str]:
    """Unknown profile → nothing (fail-closed, like `code_grants`)."""
    return _ACQUISITION_GRANTS.get(profile, frozenset())


# The tools the `data.collect` noun admits. Admitted by the effect authorizer
# (`code/authorizers.py`); the handler enforces the switch and the budget.
DATA_COLLECT_TOOLS: tuple[str, ...] = ("acquire",)

# The scraper lane's tools. `scraper_run` writes a generated program, fetches
# pages through the host's safe transport and stores what it collected. It is
# NOT part of the `data.collect` family: the operator's consent for this lane is
# per DOMAIN (the `scraper_domain` approval kind), not the bulk-acquisition
# switch, because a scraper collects a dataset the user asked for on the spot.
SCRAPER_TOOLS: tuple[str, ...] = ("scraper_run",)

# The file-system subsystem's mutating tools (Phase FS). They are code.write
# tools: removed at `plan_only`, admitted at `edit_verify`, and each path is
# re-checked by the handler → `permissions.require` → `decide_write`.
FS_WRITE_TOOLS: tuple[str, ...] = (
    "write_file", "create_file", "delete_file", "move_file", "copy_file",
    "rename_file", "create_directory", "delete_directory",
    "patch_file", "replace_text", "insert_text", "delete_text",
    "apply_diff", "multi_file_patch", "edit_file",
)

# Read-only file-system tools: always available to an allowlisted run.
FS_READ_TOOLS: tuple[str, ...] = (
    "list_directory", "read_file", "search_text", "search_files", "find_files",
    "search_symbols", "find_references", "file_metadata", "file_hash",
    "file_permissions", "index_workspace", "watch_file", "watch_directory",
    "verify_file", "validate_changes", "git_status",
)

# The Code Intelligence & Understanding Engine (CIUE): semantic rename is the only
# write tool. Every other CIUE tool is read-only structural/semantic navigation. A
# rename is a code.write, removed at `plan_only`, admitted by the effect authorizer
# and re-checked per path by `editing.apply_file_edits` → `resolve_within`.
INTEL_WRITE_TOOLS: tuple[str, ...] = ("code_rename_symbol",)

# Read-only CIUE tools: always available to an allowlisted run regardless of profile.
INTEL_READ_TOOLS: tuple[str, ...] = (
    "code_get_definition", "code_find_references", "code_find_symbol",
    "code_search_symbols", "code_get_hover", "code_get_type",
    "code_get_diagnostics", "code_get_document_symbols", "code_get_callers",
    "code_get_callees", "code_get_call_hierarchy", "code_get_implementations",
    "code_get_imports", "code_get_exports", "code_get_dependencies",
    "code_get_dependents", "code_get_project_structure", "code_get_dependency_graph",
    "code_parse_file", "code_analyze_file", "code_organize_imports",
    "code_get_relevant_context", "code_index_workspace", "code_intel_status",
    "code_find_related_tests",
)


def is_code_write(name: str) -> bool:
    """Whether a tool name is a code-file write the authorizer may admit.

    `workspace_*` writes keep their existing per-call approval flow; the fs
    subsystem's own write tools are admitted here and enforce the binding +
    autonomy profile + per-path policy in the handler. Neither terminal/shell nor
    external-action tools are covered by this predicate.
    """
    return name.startswith("workspace_") or name in FS_WRITE_TOOLS or name in INTEL_WRITE_TOOLS


def is_git_write(name: str) -> bool:
    """Whether a tool name is a LOCAL Git mutation (effect `write`).

    The Git effect authorizer admits these under the run's autonomy profile; a
    `dangerous`/`destructive`/`critical` classified operation additionally needs a
    single-use operator approval. Remote tools are NOT covered here.
    """
    return name in GIT_WRITE_TOOLS


def is_git_external(name: str) -> bool:
    """Whether a tool name is a REMOTE Git/VCS action (effect `external_action`).

    These require the `approve_external` profile AND a single-use operator
    approval bound to the exact operation.
    """
    return name in GIT_EXTERNAL_TOOLS


# External actions — commit / push / open a PR / deploy. approve_external only,
# and each is STILL an effectful external_action requiring a single-use approval.
EXTERNAL_ACTION_TOOLS: tuple[str, ...] = (
    "code_commit", "code_push", "code_open_pr", "code_deploy",
)

# The Vision/Multimodal external action: image generation is a paid provider
# call, so it is removed at `plan_only`, kept (but per-call approval-gated) at
# `edit_verify`/`approve_external`, and absent from the read-only fail-closed
# profile — exactly like every other external tool.
VISION_EXTERNAL_TOOLS: tuple[str, ...] = ("image_generate",)

# ── Browser subsystem ────────────────────────────────────────────────────────
# The browser tools are registered by `register_browser_tools` when the
# capability is enabled. Their READ half (observe/navigate) is always available;
# the effectful half (run script / CDP / sandbox program / upload / vault /
# network interception) is a durable or external action. The BROWSER POLICY
# engine in the handler is the real gate; these names only let the autonomy
# profile remove the effectful tools BEFORE the model sees a schema, exactly like
# every other capability.
BROWSER_EFFECTFUL_TOOLS: tuple[str, ...] = (
    "browser_upload", "browser_evaluate", "browser_cdp", "browser_exec",
    "browser_intercept", "browser_vault_fill", "browser_vault_enter_code",
    "browser_vault_save_login",
)


def is_browser_effect(name: str) -> bool:
    """Whether a tool name is an effectful browser tool the authorizer admits."""
    return name in BROWSER_EFFECTFUL_TOOLS


# ── Computer Use Engine ──────────────────────────────────────────────────────
# The `computer_*` tools are registered by `register_computer_tools`. The READ
# half (capture / list / doctor / finance_read) is always available; the ACTION
# half changes the desktop (or prepares a financial action) and is removed at
# `plan_only` before the model sees a schema. The handler's COMPUTER POLICY engine
# and the FINANCE FIREWALL are the real per-call gates — these names only make the
# autonomy boundary structural, exactly like the browser set.
# Mirrors the browser set's semantics: the ELEVATED actions are removed at
# `plan_only`, while `computer_use` (like `browser_click`) stays but is gated
# per-call by the computer policy engine, so a planning run can still OBSERVE.
COMPUTER_EFFECTFUL_TOOLS: tuple[str, ...] = (
    "computer_finance_prepare",
    "computer_takeover",
    "computer_request_human",
    # Writes a JSON export file, so it is effectful (R3-07): admitted by the
    # computer authorizer, removed at `plan_only`, never advertised as `read`.
    "computer_record_export",
)


def is_computer_effect(name: str) -> bool:
    """Whether a tool name is an effectful Computer Use tool the authorizer admits."""
    return name in COMPUTER_EFFECTFUL_TOOLS

# ── Automation / bots (the durable scheduled agents) ─────────────────────────
#
# The four tools that can CREATE OR KEEP autonomous work running are effectful:
# `bot_create`/`bot_update` when the schedule is autonomous and `bot_enable`/
# `bot_resume` because enabling a stored schedule starts firing it. The bot
# authorizer admits them; the HANDLER requests a single-use operator approval
# (kind `bot_schedule`) before the schedule is created or started, so an agent
# cannot quietly put itself on a cadence (R3-07). Lifecycle-safe tools (pause,
# disable, delete, run, reads) stay `read`: they stop future work or act once,
# visibly in this conversation.
AUTOMATION_EFFECT_TOOLS: tuple[str, ...] = (
    "bot_create", "bot_update", "bot_enable", "bot_resume",
)


def is_automation_effect(name: str) -> bool:
    """Whether a tool name is an autonomous-schedule tool the authorizer admits."""
    return name in AUTOMATION_EFFECT_TOOLS


# Graph Studio authoring: `create_workflow` writes a NEW workflow document into
# the app's catalog. It is a local write (it never runs the graph), so `plan_only`
# and unknown autonomy remove it like any other write. The effect authorizer
# admits it unconditionally; the front door's planner + compiler validate the
# document before a row is written.
GRAPH_WRITE_TOOLS: tuple[str, ...] = ("create_workflow",)


# The scheduling tools (Phase 17, repo-mapped onto the Automation engine). Only
# `job_create` is effectful: it writes a DISABLED finance job from the fixed
# catalogue — it can never start work on its own (enabling stays an operator
# action in the UI), so it is a local write, not an autonomous schedule. The
# bot authorizer admits it; the POLICY SWITCH (`agent_scheduling_enabled`) plus
# the always-disabled save are the controls.
SCHEDULING_EFFECT_TOOLS: tuple[str, ...] = ("job_create",)


def is_scheduling_effect(name: str) -> bool:
    """Whether a tool name is an admitted scheduling-effect tool."""
    return name in SCHEDULING_EFFECT_TOOLS

# ── Git / Version-Control subsystem (the Git engine) ─────────────────────────
#
# The Git tools are classified by EFFECT exactly like every other tool, so the
# autonomy boundary stays structural. Three sets:
#
#   GIT_READ_TOOLS      — read-only git/vcs inspection; available at every profile.
#   GIT_WRITE_TOOLS     — LOCAL mutations (local history/index/worktree); removed
#                         at `plan_only`, present at `edit_verify`.
#   GIT_EXTERNAL_TOOLS  — REMOTE mutations (fetch/pull/push/PR/CI); removed below
#                         `approve_external`, and each still needs a single-use
#                         operator approval via the Git effect authorizer.
#
# `git/operations.py` owns the per-operation RISK; these tuples own the GRANT.
GIT_READ_TOOLS: tuple[str, ...] = (
    "git_repository_info", "git_list_repositories", "git_status", "git_diff",
    "git_diff_cached", "git_diff_range", "git_log", "git_show", "git_commit_info",
    "git_blame", "git_reflog", "git_branch_list", "git_conflicts",
    "git_operation_state", "git_stash_list", "git_stash_show", "git_tag_list",
    "git_remote_list", "git_worktree_list", "git_submodule_list", "git_config_get",
    "git_config_list", "git_hooks", "git_lfs_status", "git_clean_preview",
    "git_clean_preview", "git_ignore_check", "git_generate_commit_message",
    "git_recovery_options", "git_list_hunks", "git_tag_show",
    "vcs_status", "vcs_list_repositories", "vcs_get_repository", "vcs_list_branches",
    "vcs_list_commits", "vcs_get_pull_request", "vcs_list_pull_requests",
    "vcs_list_reviews", "vcs_get_review_comments", "vcs_ci_status",
    "vcs_list_workflow_runs", "vcs_get_job_logs", "vcs_generate_pull_request",
)

GIT_WRITE_TOOLS: tuple[str, ...] = (
    "git_stage", "git_unstage", "git_stage_hunks", "git_unstage_hunks", "git_apply_patch",
    "git_commit", "git_generate_and_commit",
    "git_create_branch", "git_checkout", "git_delete_branch", "git_rename_branch",
    "git_set_upstream", "git_merge", "git_merge_abort", "git_merge_continue",
    "git_rebase", "git_rebase_plan", "git_rebase_abort", "git_rebase_continue", "git_rebase_skip",
    "git_cherry_pick", "git_cherry_pick_abort", "git_cherry_pick_continue",
    "git_revert", "git_revert_abort", "git_revert_continue", "git_reset",
    "git_resolve_conflict", "git_abort_operation", "git_continue_operation",
    "git_stash_push", "git_stash_apply", "git_stash_pop", "git_stash_drop",
    "git_tag_create", "git_tag_delete", "git_worktree_add", "git_worktree_remove",
    "git_submodule_update",     "git_bisect_start", "git_bisect_mark", "git_bisect_reset",
    "git_clean", "git_config_set",
    # Long-tail: path-level ops, stash/tag/worktree/submodule extras.
    "git_move_path", "git_remove_paths", "git_restore", "git_unset_upstream",
    "git_stash_branch", "git_stash_clear", "git_worktree_prune", "git_worktree_move",
    "git_submodule_init", "git_submodule_sync",
)

GIT_EXTERNAL_TOOLS: tuple[str, ...] = (
    "git_fetch", "git_pull", "git_push", "git_remote_add", "git_remote_remove",
    "git_remote_set_url", "git_tag_push",
    # LFS fetch/pull download from a REMOTE (they were mis-filed as local writes;
    # registration already declared them `external_action`). R3-07.
    "git_lfs_fetch", "git_lfs_pull",
    "vcs_create_branch", "vcs_create_pull_request", "vcs_update_pull_request",
    "vcs_close_pull_request", "vcs_merge_pull_request", "vcs_request_review",
    "vcs_reply_review_comment", "vcs_rerun_workflow", "vcs_cancel_workflow",
)

GIT_TOOLS: tuple[str, ...] = (*GIT_READ_TOOLS, *GIT_WRITE_TOOLS, *GIT_EXTERNAL_TOOLS)

# The terminal subsystem's effectful tools (execution, sessions, process control).
# Like the shell tools, they are removed at `plan_only` so the autonomy boundary is
# structural; at `edit_verify`/`approve_external` they are present and each
# command is still classified (allow / ask / deny) by the effect authorizer.
TERMINAL_WRITE_TOOLS: tuple[str, ...] = (
    "execute_command", "open_shell", "run_shell_command",
    "close_shell", "resize_shell", "write_shell_input", "send_shell_control",
    "start_process", "stop_process", "restart_process", "kill_process",
    "write_process_input", "send_signal",
)

# Terminal read-only tools: always available, regardless of profile.
TERMINAL_READ_TOOLS: tuple[str, ...] = (
    "list_terminal_sessions", "terminal_session_status", "read_shell_output",
    "list_processes", "process_status", "read_process_output", "process_tree",
    "process_health", "list_ports", "terminal_capabilities", "system_info",
    "resource_report",
)

# Tools that are always available regardless of profile (read-only).
READ_ONLY_TOOLS: tuple[str, ...] = (
    "workspace_status", "workspace_list", "workspace_search",
    "workspace_read", "workspace_docs", "workspace_diff",
    *TERMINAL_READ_TOOLS,
    *EXECUTION_READ_TOOLS,
    *APP_RUNTIME_READ_TOOLS,
)


# ── The coding tool-surface preset (2026-11-21) ─────────────────────────────
#
# A coding run needs ~25 tools, not the full runtime/container/cloud/browser
# catalogue. `CODING_SURFACE` is the default coding profile; non-coding effectful
# tools are removed BEFORE the model sees their schemas. They remain discoverable
# (tool_search names them and says they are outside the surface) and can be
# enabled by the operator (`BLACK_ECLIPSE_CODING_EXTRA_TOOLS`) or disabled as a
# whole (`BLACK_ECLIPSE_CODING_SURFACE=0`).
CODING_SURFACE: tuple[str, ...] = (
    # project + repository
    "workspace_status", "workspace_docs", "project_rules", "git_status",
    # navigate
    "read_file", "list_directory", "search_text", "search_files",
    "search_symbols",
    # code intelligence
    "code_get_diagnostics",
    # change
    "code_edit", "write_file", "create_file", "delete_file", "move_file",
    "apply_diff", "review_change", "code_todo",
    # undo
    "checkpoint_list", "checkpoint_diff", "checkpoint_restore", "checkpoint_undo_turn",
    # local git history (stage / commit / branch / restore). The long tail of the
    # git engine stays OUTSIDE the preset: a coding run needs a commit path, not
    # every worktree/submodule/bisect operation. These are LOCAL mutations, so
    # `allowed_tools` keeps them at `edit_verify` and drops them at `plan_only`;
    # remote actions (push/fetch/PR) stay in the `approve_external` profile.
    "git_stage", "git_unstage", "git_commit", "git_generate_and_commit",
    "git_restore", "git_reset", "git_checkout", "git_create_branch",
    "git_stash_push", "git_stash_pop",
    # run + inspect processes
    "command_run", "terminal_run", "execute_command", "start_process",
    "stop_process", "shell_spawn", "process_output",
    # verify a change with the project's OWN build/test commands and record the
    # evidence. Without these a coding run could edit and shell out, but never
    # produce a verification record - the loop's proof that the change works.
    "build_project", "test_project", "run_verification", "execution_get",
    # ask
    "clarify",
    # ship (still approval-gated; only reachable at approve_external)
    "code_commit", "code_push", "code_open_pr", "code_deploy",
    # discovery / output pager
    "tool_search", "tool_invoke", "load_tool_schema", "read_tool_output",
)

#: Removed tools the operator may enable by name (comma-separated).
CODING_EXTRA_ENV = "BLACK_ECLIPSE_CODING_EXTRA_TOOLS"
#: `0`/`off` turns the preset off entirely (the declared surface is unchanged).
CODING_SURFACE_ENV = "BLACK_ECLIPSE_CODING_SURFACE"


def coding_surface_enabled() -> bool:
    value = str(os.environ.get(CODING_SURFACE_ENV, "1")).strip().lower()
    return value not in {"0", "off", "false", "no", "disable", "disabled"}


def coding_extra_tools() -> tuple[str, ...]:
    raw = str(os.environ.get(CODING_EXTRA_ENV, "") or "")
    return tuple(sorted({name.strip() for name in raw.split(",") if name.strip()}))


def coding_surface(autonomy: Optional[str] = None) -> frozenset[str]:
    """The coding preset for this autonomy, including operator extras.

    Extras still pass through the autonomy profile, so adding a write tool to a
    `plan_only` run does not grant it.
    """
    base = set(CODING_SURFACE) | set(coding_extra_tools())
    if autonomy:
        base = set(allowed_tools(autonomy, tuple(sorted(base))))
    return frozenset(base)


def allowed_tools(autonomy: str, declared: tuple[str, ...]) -> tuple[str, ...]:
    """Narrow a declared allowlist to what the autonomy profile permits.


    plan_only        → write + external tools REMOVED (the model cannot call them)
    edit_verify      → write tools present; external tools REMOVED
    approve_external → everything, but external tools are still effectful and
                       require a per-action single-use approval at execution
    unknown          → the read-only subset (fail-closed, same as code_grants)
    """
    if autonomy == "plan_only":
        banned = set(WRITE_TOOLS) | set(FS_WRITE_TOOLS) | set(EXTERNAL_ACTION_TOOLS) | set(TERMINAL_WRITE_TOOLS) | set(INTEL_WRITE_TOOLS) | set(GIT_WRITE_TOOLS) | set(GIT_EXTERNAL_TOOLS) | set(APP_RUNTIME_WRITE_TOOLS) | set(APP_RUNTIME_K8S_WRITE_TOOLS) | set(BROWSER_EFFECTFUL_TOOLS) | set(COMPUTER_EFFECTFUL_TOOLS) | set(VISION_EXTERNAL_TOOLS) | set(AUTOMATION_EFFECT_TOOLS) | set(ACQUISITION_WRITE_TOOLS) | set(GRAPH_WRITE_TOOLS)
    elif autonomy == "edit_verify":
        # Local container/process mutations stay; CLUSTER/INFRA/CLOUD mutations
        # (kubectl apply/delete/scale/exec, helm install/uninstall, terraform
        # apply/destroy, deploy/rollback/delete) are removed — they touch live
        # infrastructure beyond the task's own folder and need `approve_external`.
        # An interactive browser session is a local/dev capability, so its
        # effectful tools stay at `edit_verify`; the browser policy engine still
        # gates each call.
        banned = set(EXTERNAL_ACTION_TOOLS) | set(GIT_EXTERNAL_TOOLS) | set(APP_RUNTIME_K8S_WRITE_TOOLS)
    elif autonomy == "approve_external":
        banned = set()
    else:
        banned = set(WRITE_TOOLS) | set(FS_WRITE_TOOLS) | set(EXTERNAL_ACTION_TOOLS) | set(TERMINAL_WRITE_TOOLS) | set(INTEL_WRITE_TOOLS) | set(GIT_WRITE_TOOLS) | set(GIT_EXTERNAL_TOOLS) | set(APP_RUNTIME_WRITE_TOOLS) | set(APP_RUNTIME_K8S_WRITE_TOOLS) | set(BROWSER_EFFECTFUL_TOOLS) | set(COMPUTER_EFFECTFUL_TOOLS) | set(VISION_EXTERNAL_TOOLS) | set(AUTOMATION_EFFECT_TOOLS) | set(ACQUISITION_WRITE_TOOLS) | set(GRAPH_WRITE_TOOLS)
    return tuple(t for t in declared if t not in banned)


# ── Decisions ───────────────────────────────────────────────────────────────


def _gate(actor: Any, binding: Optional[WorkspaceBinding], verb: str) -> Optional[PolicyDecision]:
    """The shared binding/profile gate. ``None`` means the verb passed the gate."""
    if binding is None:
        return PolicyDecision(False, "SANDBOX_UNAVAILABLE", _REASON_NO_BINDING)
    workspace_id = getattr(actor, "workspace_id", None)
    autonomy = getattr(actor, "autonomy", None)
    if not workspace_id or not autonomy or workspace_id != binding.id:
        return PolicyDecision(False, "PROFILE_DENIED", _REASON_PROFILE)
    if autonomy not in AUTONOMY_PROFILES:
        return PolicyDecision(False, "UNKNOWN_AUTONOMY", _REASON_UNKNOWN)
    if verb not in code_grants(autonomy):
        return PolicyDecision(False, "PROFILE_DENIED", _REASON_PROFILE)
    return None


def decide_write(actor: Any, binding: Optional[WorkspaceBinding], rel_path: str) -> PolicyDecision:
    """May ``actor`` write ``rel_path`` inside ``binding``?"""
    gate = _gate(actor, binding, "code.write")
    if gate is not None:
        return gate
    assert binding is not None  # narrowed by the gate
    # Protected is stricter than ignored: a protected path reports PROTECTED_PATH
    # even when it also matches an ignored pattern (e.g. `policy.py` under a
    # vendor-like dir, or `jdds.db` matching `*.db`).
    if is_protected(rel_path):
        return PolicyDecision(False, "PROTECTED_PATH", _REASON_PROTECTED)
    if is_ignored(rel_path):
        return PolicyDecision(False, "PROFILE_DENIED", _REASON_IGNORED)
    try:
        resolve_within(binding.root, rel_path)
    except PolicyDenial as denial:
        return PolicyDecision(False, denial.code, denial.reason)
    return PolicyDecision(True, "OK", _REASON_ALLOWED)


def _program_of(argv: Sequence[str]) -> str:
    first = argv[0] if argv else ""
    name = os.path.basename(str(first).replace("\\", "/")).casefold()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".py"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def decide_command(
    actor: Any,
    binding: Optional[WorkspaceBinding],
    command_class: str,
    argv: Any,
) -> PolicyDecision:
    """May ``actor`` run ``argv`` as ``command_class`` inside ``binding``?"""
    verb = "git.read" if command_class == "git_read" else "command.execute"
    gate = _gate(actor, binding, verb)
    if gate is not None:
        return gate
    if command_class not in COMMAND_CLASSES:
        return PolicyDecision(False, "COMMAND_NOT_ALLOWLISTED", _REASON_COMMAND)
    tokens = argv.split() if isinstance(argv, str) else list(argv or [])
    if not tokens:
        return PolicyDecision(False, "COMMAND_NOT_ALLOWLISTED", _REASON_COMMAND)
    program = _program_of(tokens)
    if not program or program in DENIED_PROGRAMS:
        return PolicyDecision(False, "COMMAND_NOT_ALLOWLISTED", _REASON_COMMAND)
    if program not in _COMMAND_PROGRAMS[command_class]:
        return PolicyDecision(False, "COMMAND_NOT_ALLOWLISTED", _REASON_COMMAND)
    return PolicyDecision(True, "OK", _REASON_COMMAND_OK)


__all__ = [
    "ACQUISITION_WRITE_TOOLS",
    "AUTONOMY_PROFILES",
    "COMMAND_CLASSES",
    "CODING_EXTRA_ENV",
    "CODING_SURFACE",
    "CODING_SURFACE_ENV",
    "DENIED_PROGRAMS",
    "EXECUTION_READ_TOOLS",
    "EXTERNAL_ACTION_TOOLS",
    "VISION_EXTERNAL_TOOLS",
    "FS_READ_TOOLS",
    "FS_WRITE_TOOLS",
    "IGNORED_DIRS",
    "IGNORED_GLOBS",
    "INTEL_READ_TOOLS",
    "INTEL_WRITE_TOOLS",
    "NETWORK_DEFAULT",
    "AUTOMATION_EFFECT_TOOLS",
    "GRAPH_WRITE_TOOLS",
    "SCHEDULING_EFFECT_TOOLS",
    "DATA_COLLECT_TOOLS",
    "ACQUISITION_PROFILES",
    "PLAIN_LANGUAGE_DENYLIST",
    "PROTECTED_PATHS",
    "READ_ONLY_TOOLS",
    "SECRET_ENV_ALLOWLIST",
    "WRITE_TOOLS",
    "PolicyDecision",
    "PolicyDenial",
    "WorkspaceBinding",
    "allowed_tools",
    "acquisition_grants",
    "code_grants",
    "coding_extra_tools",
    "coding_surface",
    "coding_surface_enabled",
    "decide_command",
    "decide_write",
    "is_automation_effect",
    "is_code_write",
    "is_ignored",
    "is_protected",
    "is_scheduling_effect",
    "resolve_within",
]
