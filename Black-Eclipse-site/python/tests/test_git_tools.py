"""Git agent tools — registration, the autonomy boundary, the effect authorizer,
and end-to-end tool calls against an isolated repository.

Every test uses a temporary repository and never the Eclipse source repository.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from harness.agents import CODE_AGENT
from harness.code import policy
from harness.code.tools import make_code_authorizer, register_code_tools
from harness.code.policy import WorkspaceBinding
from harness.code.workspace import ResolvedWorkspace
from harness.git.approvals import GIT_APPROVAL_KIND, make_git_authorizer, needs_approval
from harness.git.register import register_git_subsystem
from harness.git.tools import GIT_TOOL_NAMES, register_git_tools
from harness.git.vcs_tools import VCS_TOOL_NAMES
from harness.graph import effective_allowlist
from harness.persistence import AgentMemoryStore
from harness.tools import ToolContext, ToolRegistry

from git_helpers import commit_all, git, make_repo, write


class _Recorder:
    def __init__(self):
        self.events = []

    async def emit(self, event_type, data=None):
        self.events.append((event_type, data or {}))
        return {"type": event_type, "data": data or {}}

    def emit_sync(self, event_type, data=None):
        self.events.append((event_type, data or {}))


def _ws(root: Path) -> ResolvedWorkspace:
    real = Path(os.path.realpath(root))
    binding = WorkspaceBinding(id="ws_git", root=real, base_revision="HEAD", strategy="worktree")
    return ResolvedWorkspace(id="ws_git", root=real, base_revision="HEAD", strategy="worktree", binding=binding)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_code_tools(reg)
    register_git_subsystem(reg)
    return reg


def _call(registry, name, args, ctx, *, allowed=None):
    return asyncio.run(registry.execute(name, "c1", args, ctx, allowed=allowed or {name}))


def _authorizer(approvals=None):
    """The production composition: the Git authorizer OVER the code authorizer."""
    return make_git_authorizer(approvals, "r", "ws_git", inner=make_code_authorizer())


# ── Registration / metadata ──────────────────────────────────────────────────────────

def test_every_git_tool_registers():
    reg = _registry()
    names = set(reg.names())
    for name in GIT_TOOL_NAMES + VCS_TOOL_NAMES:
        assert name in names, name


def test_git_read_tool_is_read_and_mutating_tool_is_write():
    reg = _registry()
    assert reg.get("git_status").permission == "read"
    assert reg.get("git_commit").permission == "write"
    assert reg.get("git_push").effect == "external_action"
    assert reg.get("git_commit").effect == "write"


def test_git_tools_have_machine_metadata():
    reg = _registry()
    meta = reg.get("git_rebase").effect_metadata
    assert meta and meta.get("mutating") is True
    assert meta.get("risk") == "dangerous"


# ── Autonomy boundary ──────────────────────────────────────────────────────────────────

def test_plan_only_removes_all_git_writes_and_external():
    allowed = effective_allowlist(CODE_AGENT, _registry(), "plan_only")
    assert not (set(allowed) & set(policy.GIT_WRITE_TOOLS))
    assert not (set(allowed) & set(policy.GIT_EXTERNAL_TOOLS))
    assert "git_status" in allowed  # reads survive


def test_edit_verify_has_git_writes_but_not_external():
    # Two layers, asserted separately. The autonomy PROFILE classifies effects:
    # every local git mutation survives `edit_verify`, every remote action does not.
    profile = set(policy.allowed_tools("edit_verify", policy.GIT_TOOLS))
    assert set(policy.GIT_WRITE_TOOLS) <= profile
    assert not (set(policy.GIT_EXTERNAL_TOOLS) & profile)

    # The BOUND surface additionally applies the coding preset. It keeps the
    # commit path a coding run needs; the long tail stays outside the advertised
    # surface (discoverable via `tool_search`, refused until an operator enables
    # it by name).
    allowed = effective_allowlist(CODE_AGENT, _registry(), "edit_verify")
    assert {"git_stage", "git_commit", "git_checkout"} <= allowed
    assert not (set(allowed) & set(policy.GIT_EXTERNAL_TOOLS))
    assert "git_push" not in allowed


def test_approve_external_has_git_external():
    # The profile permits every remote action at `approve_external` (each is still
    # gated by a single-use operator approval at execution).
    profile = set(policy.allowed_tools("approve_external", policy.GIT_TOOLS))
    assert set(policy.GIT_EXTERNAL_TOOLS) <= profile

    # The coding preset advertises only the ship tools; the git-engine remote
    # long tail is outside the preset (discoverable via `tool_search`, refused
    # until an operator enables it by name).
    allowed = effective_allowlist(CODE_AGENT, _registry(), "approve_external")
    assert "tool_search" in allowed
    assert not (set(allowed) & set(policy.GIT_EXTERNAL_TOOLS))
    assert {"code_push", "code_open_pr"} <= allowed


def test_code_agent_has_git_tools_declared():
    declared = set(CODE_AGENT.tools)
    assert "git_status" in declared and "git_commit" in declared
    # External git tools ARE declared (like code_commit/code_push) but removed
    # below `approve_external` by `code.policy.allowed_tools`.
    assert set(policy.GIT_EXTERNAL_TOOLS) <= declared
    edit_verify = effective_allowlist(CODE_AGENT, _registry(), "edit_verify")
    assert not (set(policy.GIT_EXTERNAL_TOOLS) & set(edit_verify))


# ── Authorizer ────────────────────────────────────────────────────────────────────────────

def test_needs_approval_classification():
    assert needs_approval("git_commit", {}, "medium", "write") is False
    assert needs_approval("git_merge", {}, "high", "write") is False  # caution in catalog
    assert needs_approval("git_reset", {"mode": "hard"}, "high", "write") is True
    assert needs_approval("git_clean", {}, "high", "write") is True
    # The catalog classifies `merge`/`rebase` as caution; only the explicitly
    # DANGEROUS operations and every remote action need approval.
    assert needs_approval("git_rebase", {}, "high", "write") is True
    assert needs_approval("git_push", {}, "high", "external_action") is True
    assert needs_approval("git_push", {"forceWithLease": True}, "high", "external_action") is True
    assert needs_approval("vcs_merge_pull_request", {}, "high", "external_action") is True


def test_commit_is_admitted_without_approvals(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    ctx = ToolContext(
        run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify",
        authorize_effect=_authorizer(),
    )
    result = _call(reg, "git_status", {}, ctx)
    assert result.ok and result.data["success"] is True


def test_hard_reset_is_refused_without_approval(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    ctx = ToolContext(
        run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify",
        authorize_effect=_authorizer(),
    )
    result = _call(reg, "git_reset", {"mode": "hard"}, ctx)
    assert result.ok is False
    assert result.error.code == "TOOL_FORBIDDEN"


def test_push_refused_without_approval_store(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    ctx = ToolContext(
        run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="approve_external",
        authorize_effect=_authorizer(),
    )
    result = _call(reg, "git_push", {"remote": "origin"}, ctx)
    assert result.ok is False
    assert result.error.code == "TOOL_FORBIDDEN"


def test_commit_admitted_and_recorded_when_approved(tmp_path):
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    try:
        reg = _registry()
        repo = make_repo(tmp_path / "repo")
        write(repo.root, "f.txt", "x\n")
        ctx = ToolContext(
            run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify",
            authorize_effect=_authorizer(),
        )
        _call(reg, "git_stage", {"paths": ["f.txt"]}, ctx)
        result = _call(reg, "git_commit", {"message": "add f"}, ctx)
        assert result.ok, result.text
        assert result.data["success"] is True
        assert result.data["result"]["commit"]
    finally:
        store.close()


def test_dangerous_op_approved_with_single_use(tmp_path):
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    try:
        from harness.mcp.approvals import ApprovalStore
        from harness.git.approvals import _digest

        approvals = ApprovalStore(store)
        repo = make_repo(tmp_path / "repo")
        reg = _registry()
        ctx = ToolContext(
            run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify",
            approvals=approvals, authorize_effect=make_git_authorizer(approvals, "r", "ws_git"),
        )
        # No approval yet: refused.
        refused = _call(reg, "git_clean", {"confirm": True}, ctx)
        assert refused.ok is False
        # Operator approves the exact call.
        digest = _digest("ws_git", "git_clean", {"directories": None, "confirm": True})
        pending = approvals.request_kind(
            kind=GIT_APPROVAL_KIND, request_key="git:git_clean", digest=digest,
            effect="external_action", workspace_id="ws_git",
        )
        approvals.decide(pending["id"], approve=True, decided_by="operator")
        write(repo.root, "junk.txt", "junk\n")
        ctx2 = ToolContext(
            run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify",
            approvals=approvals, authorize_effect=make_git_authorizer(approvals, "r", "ws_git"),
        )
        allowed = _call(reg, "git_clean", {"confirm": True}, ctx2)
        assert allowed.ok, allowed.text
    finally:
        store.close()


# ── Tool results ─────────────────────────────────────────────────────────────────────────────

def test_git_status_tool_returns_structured_data(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    write(repo.root, "f.txt", "x\n")
    ctx = ToolContext(run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify")
    result = _call(reg, "git_status", {}, ctx)
    assert result.ok
    assert result.data["operation"] == "status"
    assert result.data["branch"] == "main"
    assert result.data["gitOperation"]["operation"] is None


def test_git_diff_tool(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    write(repo.root, "f.txt", "hello\n")
    commit_all(repo.root, "add")
    write(repo.root, "f.txt", "hello\nworld\n")
    ctx = ToolContext(run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify")
    result = _call(reg, "git_diff", {}, ctx)
    assert result.ok
    assert "+world" in result.data["patch"]


def test_git_create_branch_tool(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    ctx = ToolContext(
        run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify",
        authorize_effect=_authorizer(),
    )
    result = _call(reg, "git_create_branch", {"name": "feature/x"}, ctx)
    assert result.ok and result.data["success"]


def test_git_operation_state_reports_none_when_idle(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    ctx = ToolContext(run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify")
    result = _call(reg, "git_operation_state", {}, ctx)
    assert result.ok
    assert result.data["operation"] is None


def test_git_repository_info_unavailable_outside_checkout(tmp_path):
    reg = _registry()
    plain = tmp_path / "plain"
    plain.mkdir()
    ctx = ToolContext(run_id="r", log=lambda *_: None, workspace=_ws(plain), autonomy="edit_verify")
    result = _call(reg, "git_repository_info", {}, ctx)
    assert result.ok
    assert result.data["success"] is False
    assert result.data["error"]["code"] == "NOT_A_REPOSITORY"


def test_git_conflicts_tool_after_conflict(tmp_path):
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    write(repo.root, "c.txt", "base\n")
    commit_all(repo.root, "base")
    git(repo.root, "checkout", "-q", "-b", "other")
    write(repo.root, "c.txt", "other\n")
    commit_all(repo.root, "other")
    git(repo.root, "checkout", "-q", "main")
    write(repo.root, "c.txt", "main\n")
    commit_all(repo.root, "main")
    git(repo.root, "merge", "other")
    ctx = ToolContext(run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify")
    result = _call(reg, "git_conflicts", {}, ctx)
    assert result.ok
    assert result.data["count"] == 1


# ── VCS status (no credentials) ─────────────────────────────────────────────────────────────

def test_vcs_status_without_credentials_reports_honestly(tmp_path, monkeypatch):
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    git(repo.root, "remote", "add", "origin", "https://github.com/example/repo.git")
    ctx = ToolContext(run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify")
    result = _call(reg, "vcs_status", {}, ctx)
    assert result.ok
    # The provider is resolved (github) but reports not-authenticated.
    assert result.data["service"]["provider"] == "github"
    assert result.data["service"]["authenticated"] is False
    assert result.data["defaultRepository"] == "example/repo"


def test_vcs_ci_status_never_claims_success_without_credentials(tmp_path, monkeypatch):
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    reg = _registry()
    repo = make_repo(tmp_path / "repo")
    git(repo.root, "remote", "add", "origin", "https://github.com/example/repo.git")
    ctx = ToolContext(run_id="r", log=lambda *_: None, workspace=_ws(repo.root), autonomy="edit_verify")
    result = _call(reg, "vcs_ci_status", {"fullName": "example/repo", "ref": "main"}, ctx)
    assert result.ok
    assert result.data["success"] is False
    assert result.data["error"]["code"] == "AUTHENTICATION_FAILED"


