"""Pytest mirror of `check_trading_policy.py`.

The runtime test suite is skipped on a machine without the agent stack; these
tests only import `harness.automation.trading_policy`, which is stdlib-only, so
they run everywhere the suite runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harness.automation import trading_policy as p

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)


def test_symbols_normalise_and_match_broker_suffixes():
    assert p.normalize_symbols(["eurusd", " EURUSD ", "GBPUSD", "EURUSD"]) == [
        "EURUSD",
        "GBPUSD",
    ]
    assert p.normalize_symbols([""]) == []
    assert p.is_symbol_tradable("EURUSD", ["EURUSD.m"])
    assert p.is_symbol_tradable("BTCUSD", ["BTCUSD-ECN"])
    assert not p.is_symbol_tradable("EURUSD", ["EURUSDT"])
    assert p.missing_symbols(["EURUSD", "USDJPY"], ["EURUSD"]) == ["USDJPY"]


def test_the_universe_size_is_configurable_and_hard_capped():
    many = [f"SYM{i}" for i in range(40)]
    assert len(p.normalize_symbols(many)) == p.MAX_SYMBOLS
    assert len(p.normalize_symbols(many, limit=12)) == 12
    # A malformed/oversized setting is clamped, never accepted as-is.
    assert len(p.normalize_symbols(many, limit=999)) == p.MAX_SYMBOLS_HARD
    # 0/negative/malformed means "use the default", never a surprise 1-symbol bot.
    assert len(p.normalize_symbols(many, limit=0)) == p.MAX_SYMBOLS
    assert len(p.normalize_symbols(many, limit=-3)) == p.MAX_SYMBOLS
    assert len(p.normalize_symbols(many, limit="nonsense")) == p.MAX_SYMBOLS


def test_consent_covers_a_universe_wider_than_the_default_form_cap():
    symbols = [f"SYM{i}" for i in range(12)]
    consent = {
        "modes": ["live"],
        "expiresAt": "2099-01-01T00:00:00Z",
        "revokedAt": None,
        "limits": {
            "symbols": symbols,
            "maxOrdersPerDay": 4,
            "maxMarginPerOrder": 100,
            "maxOpenPositions": 1,
        },
    }
    request = {
        "mode": "live",
        # Order must not matter: the grant is a set, not the first eight.
        "symbols": list(reversed(symbols)),
        "maxOrdersPerDay": 4,
        "maxMarginPerOrder": 100,
        "maxOpenPositions": 1,
    }
    assert p.consent_covers(consent, request, NOW)["covered"] is True


def test_session_window():
    session = {
        "timezone": "UTC",
        "weekdays": [1, 2, 3, 4, 5],
        "start": "08:00",
        "end": "20:00",
    }
    assert p.session_state(NOW, session)["open"] is True
    assert p.session_state(NOW.replace(hour=22), session)["open"] is False
    assert p.session_state(NOW.replace(hour=3), session)["open"] is False
    assert p.session_state(NOW + timedelta(days=5), session)["open"] is False

    overnight = {"timezone": "UTC", "weekdays": [], "start": "22:00", "end": "02:00"}
    assert p.session_state(NOW.replace(hour=23), overnight)["open"] is True
    assert p.session_state(NOW.replace(hour=1), overnight)["open"] is True
    assert p.session_state(NOW.replace(hour=12), overnight)["open"] is False

    unknown = p.session_state(
        NOW,
        {"timezone": "Mars/Olympus", "weekdays": [], "start": "00:00", "end": "23:59"},
    )
    assert unknown["open"] is False
    assert "timezone" in (unknown["reason"] or "")

    assert p.session_state(NOW, None)["open"] is True


def test_freshness():
    fresh = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    stale = (NOW - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    assert p.is_fresh(fresh, NOW, 3600)
    assert not p.is_fresh(stale, NOW, 3600)
    assert not p.is_fresh(None, NOW, 3600)
    assert p.timeframe_seconds("h1") == 3600
    # 1-minute bars: 60s per bar, a 2.5× window.
    assert p.timeframe_seconds("M1") == 60
    assert p.is_fresh((NOW - timedelta(seconds=90)).isoformat(), NOW, 60)
    assert not p.is_fresh((NOW - timedelta(seconds=300)).isoformat(), NOW, 60)


def _consent(**patch):
    base = {
        "version": 1,
        "kind": "standing",
        "modes": ["demo"],
        "grantedAt": NOW.isoformat(),
        "expiresAt": (NOW + timedelta(days=30)).isoformat(),
        "revokedAt": None,
        "limits": {
            "symbols": ["EURUSD", "GBPUSD"],
            "maxOrdersPerDay": 4,
            "maxMarginPerOrder": 500,
            "maxOpenPositions": 2,
        },
        "phrase": "MANDATE DEMO MT5 demo",
    }
    base.update(patch)
    return base


def _request(**patch):
    base = {
        "mode": "demo",
        "symbols": ["EURUSD"],
        "maxOrdersPerDay": 2,
        "maxMarginPerOrder": 200,
        "maxOpenPositions": 1,
    }
    base.update(patch)
    return base


def test_consent_covers_inside_the_grant():
    assert p.consent_status(None, NOW) == "missing"
    assert p.consent_status(_consent(), NOW) == "active"
    assert p.consent_covers(_consent(), _request(), NOW)["covered"] is True


def test_consent_refuses_each_dimension():
    mode = p.consent_covers(_consent(), _request(mode="live"), NOW)
    assert mode["covered"] is False and mode["field"] == "consent"

    symbols = p.consent_covers(_consent(), _request(symbols=["USDJPY"]), NOW)
    assert symbols["covered"] is False and symbols["field"] == "symbols"

    orders = p.consent_covers(_consent(), _request(maxOrdersPerDay=9), NOW)
    assert orders["covered"] is False

    margin = p.consent_covers(_consent(), _request(maxMarginPerOrder=900), NOW)
    assert margin["covered"] is False

    positions = p.consent_covers(_consent(), _request(maxOpenPositions=3), NOW)
    assert positions["covered"] is False


def test_consent_wildcard_and_lifecycle():
    wildcard = _consent(limits={**_consent()["limits"], "symbols": ["*"]})
    assert p.consent_covers(wildcard, _request(symbols=["XAUUSD"]), NOW)["covered"]

    expired = _consent(expiresAt=(NOW - timedelta(seconds=1)).isoformat())
    assert p.consent_status(expired, NOW) == "expired"
    assert not p.consent_covers(expired, _request(), NOW)["covered"]

    revoked = _consent(revokedAt=NOW.isoformat())
    assert p.consent_status(revoked, NOW) == "revoked"

    assert p.needs_phrase(None, _request(mode="shadow"), NOW) is False
    assert p.needs_phrase(None, _request(), NOW) is True
    assert p.needs_phrase(_consent(), _request(), NOW) is False


def test_decision_shape_and_line():
    line = p.format_decision_line({
        "symbol": "EURUSD",
        "action": "buy",
        "outcome": "filled",
        "risk": {"quantity": 0.1},
        "code": None,
    })
    assert line == "EURUSD · BUY 0.1 · filled"

    record = p.decision(
        bot_id="aut_1",
        bot_version=3,
        strategy_id="ema",
        strategy_version="1.0.0",
        mode="demo",
        tier="l1",
        symbol="EURUSD",
        action="buy",
        outcome="filled",
        order_id="ord_1",
        risk={"allowed": True, "quantity": 0.1},
    )
    assert record["botId"] == "aut_1"
    assert record["tier"] == "l1"
    assert record["warnings"] == []


def test_visual_teaching_prompt_is_guidance_not_data():
    prompt = p.visual_teaching_prompt([
        {"caption": "enter after the retest", "mime": "image/png"},
        {"caption": "", "mime": "image/png"},
    ])
    assert "enter after the retest" in prompt
    assert "No caption." in prompt
    assert "not live market data" in prompt
    assert "data wins" in prompt
    assert p.visual_teaching_prompt([]) == ""
    assert p.visual_teaching_prompt(None) == ""

def test_rank_evaluations_prefers_exits_and_skips_held_symbols():
    picks = p.rank_evaluations([
        {"symbol": "EURUSD", "signal": "long_entry", "score": 0.9},
        {"symbol": "GBPUSD", "signal": "long_exit", "score": 0.1},
        {"symbol": "AUDUSD", "signal": "long_entry", "score": 0.95},
        {"symbol": "NZDUSD", "signal": "short_entry", "score": 0.8},
        {"symbol": "USDJPY", "signal": "none", "score": 0},
    ], max_entries=1, open_symbols=["AUDUSD"])
    assert [row["symbol"] for row in picks] == ["GBPUSD", "EURUSD"]
