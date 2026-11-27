"""Agent tool registration, schemas, read execution and effect authorization."""

from __future__ import annotations

from portfolio_helpers import make_portfolio, make_service

from harness.portfolio.tools import (
    PORTFOLIO_CONSEQUENTIAL_TOOLS,
    PORTFOLIO_READ_TOOLS,
    PORTFOLIO_WRITE_TOOLS,
    register_portfolio_tools,
)
from harness.tools import ToolContext, ToolRegistry


def _registry():
    reg = ToolRegistry()
    register_portfolio_tools(reg)
    return reg


def test_all_tools_register():
    reg = _registry()
    names = set(reg.names())
    assert set(PORTFOLIO_READ_TOOLS) | set(PORTFOLIO_WRITE_TOOLS) | set(PORTFOLIO_CONSEQUENTIAL_TOOLS) <= names
    assert {
        "portfolio_audit_log", "portfolio_audit_verify", "portfolio_realized_gains",
        "portfolio_create_account", "portfolio_list_accounts",
        "portfolio_update_account", "portfolio_delete_account",
    } <= names
    assert len(names) == 47


def test_write_tools_declare_an_effect():
    reg = _registry()
    for name in PORTFOLIO_WRITE_TOOLS + PORTFOLIO_CONSEQUENTIAL_TOOLS:
        tool = reg.get(name)
        assert tool.permission == "write"
        assert tool.effect
    for name in PORTFOLIO_READ_TOOLS:
        assert reg.get(name).permission == "read"


async def test_read_tool_executes(tmp_path):
    svc = make_service(tmp_path)
    pid = make_portfolio(svc)
    reg = _registry()
    ctx = ToolContext(run_id="r", log=lambda *_: None, portfolio_service=svc)
    result = await reg.execute("portfolio_get", "c1", {"portfolio_id": pid}, ctx)
    assert result.ok
    assert result.data["portfolio"]["id"] == pid


async def test_write_tool_refused_without_authorizer(tmp_path):
    svc = make_service(tmp_path)
    pid = make_portfolio(svc)
    reg = _registry()
    ctx = ToolContext(run_id="r", log=lambda *_: None, portfolio_service=svc)
    result = await reg.execute("portfolio_add_position", "c1",
                               {"portfolio_id": pid, "symbol": "AAPL", "quantity": "1", "price": "10"}, ctx)
    assert not result.ok
    assert result.error.code == "TOOL_FORBIDDEN"


async def test_write_tool_allowed_with_authorizer(tmp_path):
    svc = make_service(tmp_path)
    pid = make_portfolio(svc)
    reg = _registry()

    async def authorize(tool, args, ctx=None):
        return tool.name in ("portfolio_add_position",)

    ctx = ToolContext(run_id="r", log=lambda *_: None, portfolio_service=svc,
                      authorize_effect=authorize)
    result = await reg.execute("portfolio_add_position", "c1",
                               {"portfolio_id": pid, "symbol": "AAPL", "quantity": "1", "price": "10"}, ctx)
    assert result.ok
    assert result.data["status"] == "SUCCESS"


async def test_rebalance_is_consequential(tmp_path):
    svc = make_service(tmp_path)
    pid = make_portfolio(svc)
    reg = _registry()
    ctx = ToolContext(run_id="r", log=lambda *_: None, portfolio_service=svc)
    result = await reg.execute("portfolio_rebalance", "c1",
                               {"portfolio_id": pid, "target": {"AAPL": 1.0}}, ctx)
    assert not result.ok  # no authorizer ⇒ refused (consequential)
    assert result.error.code == "TOOL_FORBIDDEN"


async def test_tool_validation_rejects_bad_input(tmp_path):
    svc = make_service(tmp_path)
    pid = make_portfolio(svc)
    reg = _registry()
    ctx = ToolContext(run_id="r", log=lambda *_: None, portfolio_service=svc)
    result = await reg.execute("portfolio_get", "c1", {"portfolio_id": 123}, ctx)
    assert not result.ok
    assert result.error.code == "TOOL_VALIDATION_FAILED"
