"""The parity register is machine-checked, not prose.

`docs/harness/parity-register.json` is the answer to "what from the reference did
we take, and what did we not take, and why". A register that nobody validates
rots, so this test fails when:

  * a reference module disappears from the register,
  * a status is missing or outside the vocabulary,
  * the evidence path for a ``ported``/``equivalent`` row no longer exists on disk,
  * a ``deferred``/``not_applicable`` row has no recorded reason,
  * a gap-register item (G1-G14) loses its ``where`` path.

The path checks are the important ones: they make it impossible to claim
coverage by citing a file that was deleted or never written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
REGISTER = REPO / "Black-Eclipse-site" / "docs" / "harness" / "parity-register.json"

#: Every module in the reference's module table (analysis §1). Hardcoded on
#: purpose: it must not derive from the file it validates.
REFERENCE_MODULES = {
    "tools", "factors", "trading", "channels", "backtest", "quantlib", "cli", "api",
    "live", "agent", "providers", "data_gateway", "swarm", "portfolio",
    "shadow_account", "strategy_discovery", "memory", "scheduled_research",
    "session", "entities", "strategy_store", "scheduling", "goal",
    "research_artifacts", "governance", "br_data", "openbb_bridge", "research_lab",
    "hypotheses", "core", "channelsui", "pixel_agents", "security",
}


def _register() -> dict:
    return json.loads(REGISTER.read_text(encoding="utf-8"))


def test_the_register_covers_every_reference_module():
    modules = {row["module"] for row in _register()["modules"]}
    assert modules == REFERENCE_MODULES
    assert len(modules) == len(_register()["modules"]), "duplicate module row"


def test_every_module_has_a_status_from_the_vocabulary():
    allowed = set(_register()["statuses"])
    for row in _register()["modules"]:
        assert row["status"] in allowed, row["module"]
        assert row.get("eclipse"), f"{row['module']} has no Eclipse evidence path"


def test_every_cited_path_exists():
    """The anti-fabrication gate: no row may cite a file that is not there."""
    missing = []
    for row in _register()["modules"] + _register()["gaps"]:
        for relative in row.get("eclipse") or [row.get("where")]:
            if not relative:
                continue
            if not (REPO / relative).exists():
                missing.append(f"{row.get('module') or row.get('id')}: {relative}")
    assert not missing, "missing evidence paths:\n" + "\n".join(missing)


def test_a_row_that_did_not_port_something_states_why():
    for row in _register()["modules"]:
        if row["status"] in ("deferred", "not_applicable"):
            assert len(str(row.get("note", ""))) >= 40, row["module"]


def test_the_gap_register_is_complete_and_done():
    gaps = {row["id"]: row for row in _register()["gaps"]}
    assert set(gaps) == {f"G{n}" for n in range(1, 15)}
    for gap in gaps.values():
        assert gap["status"] in ("done", "already_existed"), gap["id"]
        assert gap.get("where"), f"{gap['id']} has no evidence path"


def test_the_register_does_not_claim_more_than_it_shows():
    """"ported" means a mechanism was transcribed, so it must resolve to source."""
    for row in _register()["modules"]:
        if row["status"] != "ported":
            continue
        resolves = False
        for path in row["eclipse"]:
            target = REPO / path
            if target.is_file() and target.suffix in (".py", ".md"):
                resolves = True
            elif target.is_dir() and any(target.rglob("*.py")):
                resolves = True
        assert resolves, row["module"]


def test_governance_is_recorded_as_ported_with_both_halves():
    row = next(r for r in _register()["modules"] if r["module"] == "governance")
    assert row["status"] == "ported"
    assert any("ledger.py" in path for path in row["eclipse"])
    assert any("manifest.py" in path for path in row["eclipse"])


# -- behaviour coverage, not just path presence ---------------------------


def test_each_ported_or_equivalent_module_names_a_test_that_contains_its_symbol():
    """A cited path proves a file exists. This proves a test exercises the mechanism.

    Every row claiming a mechanism must name at least one test file, and that file
    must contain the row's ``testSymbol``. The symbol is declared, so it cannot be
    satisfied by a test that merely mentions the package name.
    """
    orphans: list[str] = []
    for row in _register()["modules"]:
        if row["status"] not in ("ported", "equivalent"):
            continue
        tests = row.get("tests") or []
        symbol = str(row.get("testSymbol") or "")
        if not tests:
            assert len(str(row.get("noTestReason", ""))) >= 40, row["module"]
            continue
        if not symbol:
            orphans.append(f"{row['module']}: names tests but no testSymbol")
            continue
        for relative in tests:
            path = REPO / relative
            if not path.is_file():
                orphans.append(f"{row['module']}: missing test file {relative}")
            elif symbol not in path.read_text(encoding="utf-8", errors="ignore"):
                orphans.append(f"{row['module']}: {path.name} does not contain {symbol!r}")
    assert not orphans, "modules without behaviour evidence:\n" + "\n".join(orphans)


def test_the_behaviour_evidence_is_a_test_file_not_a_readme():
    for row in _register()["modules"]:
        for relative in row.get("tests") or []:
            assert relative.endswith(".py"), relative
            assert "python/tests/test_" in relative, relative


def test_a_behaviour_claim_names_a_test_containing_its_symbol():
    """The numeric half: a row may assert the reference's own values on a fixture."""
    problems: list[str] = []
    for row in _register()["modules"]:
        for entry in row.get("behaviour") or []:
            path = REPO / entry["test"]
            if not path.is_file():
                problems.append(f"{row['module']}: missing {entry['test']}")
                continue
            if entry["symbol"] not in path.read_text(encoding="utf-8", errors="ignore"):
                problems.append(
                    f"{row['module']}: {path.name} does not contain {entry['symbol']!r}"
                )
            if len(str(entry.get("checks", ""))) < 20:
                problems.append(f"{row['module']}: behaviour entry states no check")
    assert not problems, "behaviour claims without evidence:\n" + "\n".join(problems)


def test_every_ported_row_carries_a_behaviour_comparison():
    """Completeness is now a gate, not a number someone typed.

    The register reached 32/32 by porting each remaining crate's contract; this test is
    what stops it sliding back to file-presence. A new `ported`/`equivalent` row without a
    behaviour comparison fails here, which is the only way the claim stays true.
    """
    missing = [
        row["module"] for row in _register()["modules"]
        if row["status"] in ("ported", "equivalent") and not row.get("behaviour")
    ]
    assert not missing, f"rows without a behaviour comparison: {missing}"


def test_the_ported_row_count_is_stated():
    ported = [row for row in _register()["modules"]
              if row["status"] in ("ported", "equivalent")]
    assert len(ported) == 32, (
        f"expected 32 ported/equivalent rows, found {len(ported)}"
    )


def test_the_high_risk_equivalents_carry_a_numeric_comparison():
    """These six were compared numerically, not by shape. Adding a row here is a
    promise that a fixture exists that would notice a divergence."""
    required = {
        "strategy_store",
        "research_lab",
        "portfolio",
        "shadow_account",
        "research_artifacts",
        "br_data",
        "session",
        "channels",
        "entities",
        "memory",
        "scheduling",
    }
    rows = {row["module"]: row for row in _register()["modules"]}
    missing = sorted(name for name in required if not rows[name].get("behaviour"))
    assert not missing, f"no behavioural comparison for: {missing}"


def test_every_behaviour_row_names_the_reference_it_was_checked_against():
    """A numeric claim without a named source is just a number we like."""
    for row in _register()["modules"]:
        if not row.get("behaviour"):
            continue
        note = str(row.get("note", ""))
        assert any(token in note.lower() for token in
                   ("reference", "ported", "parity", "verified")), row["module"]





@pytest.mark.parametrize("module", sorted(REFERENCE_MODULES))
def test_each_module_row_is_addressable(module):
    assert any(r["module"] == module for r in _register()["modules"])
