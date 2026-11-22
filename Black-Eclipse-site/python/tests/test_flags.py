"""W0: typed flag registry parsing, defaults, snapshots and strict mode."""

from __future__ import annotations

import pytest

from harness.config import flags


def test_unknown_flags_are_not_invented():
    assert flags.get_flag("HARNESS_NOT_A_REAL_FLAG", environ={}) == ""
    assert "HARNESS_NOT_A_REAL_FLAG" not in flags.snapshot(environ={})


def test_defaults_preserve_today():
    snap = flags.snapshot(environ={})
    # W1 promoted the live gateway to the default; `off` and `shadow` remain.
    assert snap["HARNESS_MODEL_GATEWAY"] == "on"
    assert snap["HARNESS_PARALLEL_TOOLS"] == "on"
    assert snap["HARNESS_AUTONOMY"] == "off"
    assert snap["HARNESS_EFFECT_TOKENS"] == "shadow"
    assert snap["HARNESS_SKILL_EXEC_EVAL"] == "on"


def test_bool_parsing_accepts_common_spellings():
    for raw in ("1", "true", "YES", "on", "enable", "enabled"):
        assert flags.get_bool(
            "HARNESS_AUTONOMY", environ={"HARNESS_AUTONOMY": raw}
        )
    for raw in ("0", "false", "NO", "off", "disable", "disabled"):
        assert not flags.get_bool(
            "HARNESS_AUTONOMY", environ={"HARNESS_AUTONOMY": raw}
        )


def test_bool_invalid_value_falls_back_and_is_reported():
    env = {"HARNESS_AUTONOMY": "maybe"}
    assert not flags.get_bool("HARNESS_AUTONOMY", environ=env)
    problems = flags.validate(environ=env)
    assert any("HARNESS_AUTONOMY" in p for p in problems)


def test_int_clamps_and_invalid_falls_back():
    assert flags.get_int(
        "HARNESS_TOOL_CONCURRENCY", environ={"HARNESS_TOOL_CONCURRENCY": "99"}
    ) == 12
    assert flags.get_int(
        "HARNESS_TOOL_CONCURRENCY", environ={"HARNESS_TOOL_CONCURRENCY": "0"}
    ) == 1
    assert flags.get_int(
        "HARNESS_TOOL_CONCURRENCY", environ={"HARNESS_TOOL_CONCURRENCY": "nope"}
    ) == 6


def test_parallel_tools_is_a_shadowable_enum():
    assert flags.get_flag("HARNESS_PARALLEL_TOOLS", environ={}) == "on"
    assert flags.get_flag(
        "HARNESS_PARALLEL_TOOLS", environ={"HARNESS_PARALLEL_TOOLS": "shadow"}
    ) == "shadow"
    assert flags.get_flag(
        "HARNESS_PARALLEL_TOOLS", environ={"HARNESS_PARALLEL_TOOLS": "yes"}
    ) == "on"
    assert flags.get_int(
        "HARNESS_TOOL_BATCH_TIMEOUT_S",
        environ={"HARNESS_TOOL_BATCH_TIMEOUT_S": "9999"},
    ) == 600


def test_enum_invalid_value_falls_back():
    env = {"HARNESS_MODEL_GATEWAY": "shdaow"}
    assert flags.get_flag("HARNESS_MODEL_GATEWAY", environ=env) == "on"
    assert any("HARNESS_MODEL_GATEWAY" in p for p in flags.validate(environ=env))


def test_snapshot_is_sorted_and_stable():
    snap = flags.snapshot(environ={})
    assert list(snap) == sorted(snap)
    again = flags.snapshot(environ={})
    assert snap == again
    assert flags.snapshot_hash(snap) == flags.snapshot_hash(again)


def test_snapshot_hash_changes_with_a_flag_value():
    base = flags.snapshot(environ={})
    changed = flags.snapshot(environ={"HARNESS_PARALLEL_TOOLS": "off"})
    assert flags.snapshot_hash(base) != flags.snapshot_hash(changed)


def test_strict_mode_raises_only_on_invalid_known_values():
    env = {"HARNESS_FLAG_STRICT": "on", "HARNESS_MODEL_GATEWAY": "on"}
    flags.assert_valid(environ=env)  # valid, no raise

    bad = {"HARNESS_FLAG_STRICT": "on", "HARNESS_MODEL_GATEWAY": "nonsense"}
    with pytest.raises(ValueError, match="HARNESS_MODEL_GATEWAY"):
        flags.assert_valid(environ=bad)

    # Strict off: invalid values are still reported by validate but do not raise.
    strict_off = {"HARNESS_FLAG_STRICT": "off", "HARNESS_MODEL_GATEWAY": "nonsense"}
    flags.assert_valid(environ=strict_off)
    assert flags.validate(environ=strict_off)


def test_snapshot_contains_no_secret_like_names():
    snap = flags.snapshot(environ={})
    for name in snap:
        assert not name.endswith("_KEY")
        assert "_SECRET" not in name
        assert not name.endswith("_TOKEN")
        assert len(snap[name]) <= 64
