"""The methodology fingerprint: same method, same hash — on any day.

The property that matters is *day independence*: a digest that changes every time
you look at it cannot identify a methodology, so ``timestamp`` and ``runId`` are
excluded and the tests prove it by hashing the same method at two times.
"""

from __future__ import annotations

from harness.governance import build_manifest, manifest_diff, manifest_hash

SKILLS = (("equity-research", "read the filing, then the transcript"),)
TOOLS = ("get_quote", "run_backtest", "screen")


def _manifest(**overrides):
    kwargs = dict(
        system_prompt="You are a careful analyst.",
        skills=SKILLS,
        tools=TOOLS,
        packages={"numpy": "2.1.0", "pandas": "2.2.3"},
        timestamp="2026-01-01T00:00:00Z",
        run_id="run-a",
    )
    kwargs.update(overrides)
    return build_manifest(**kwargs)


# -- day independence ------------------------------------------------------


def test_two_runs_of_the_same_method_hash_identically_a_month_apart():
    first = _manifest(timestamp="2026-01-01T00:00:00Z", run_id="run-a")
    second = _manifest(timestamp="2026-02-01T00:00:00Z", run_id="run-b")
    assert first["manifestHash"] == second["manifestHash"]
    # the metadata is still recorded, just not hashed
    assert first["timestamp"] != second["timestamp"]
    assert first["runId"] != second["runId"]


def test_the_hash_is_recomputable_from_the_manifest_alone():
    manifest = _manifest()
    assert manifest_hash(manifest) == manifest["manifestHash"]


def test_tool_order_does_not_change_the_hash():
    a = _manifest(tools=("a", "b", "c"))
    b = _manifest(tools=("c", "a", "b"))
    assert a["manifestHash"] == b["manifestHash"]


def test_duplicate_tool_names_collapse():
    a = _manifest(tools=("a", "b"))
    b = _manifest(tools=("a", "b", "b"))
    assert a["manifestHash"] == b["manifestHash"]


# -- every hashed dimension is actually hashed ----------------------------


def test_editing_the_system_prompt_changes_the_hash():
    assert _manifest()["manifestHash"] != _manifest(system_prompt="Be terse.")[
        "manifestHash"
    ]


def test_editing_a_skill_body_changes_the_hash():
    edited = (("equity-research", "read the transcript first"),)
    assert _manifest()["manifestHash"] != _manifest(skills=edited)["manifestHash"]


def test_adding_a_tool_changes_the_hash():
    assert _manifest()["manifestHash"] != _manifest(tools=TOOLS + ("new_tool",))[
        "manifestHash"
    ]


def test_a_package_version_bump_changes_the_hash():
    bumped = {"numpy": "2.1.1", "pandas": "2.2.3"}
    assert _manifest()["manifestHash"] != _manifest(packages=bumped)["manifestHash"]


def test_the_prompt_is_recorded_as_a_hash_not_the_text():
    manifest = _manifest(system_prompt="a secret prompt")
    assert "a secret prompt" not in str(manifest)
    assert len(manifest["systemPromptHash"]) == 64


# -- a diff an operator can act on ---------------------------------------


def test_an_identical_methodology_diffs_to_empty():
    assert manifest_diff(_manifest(), _manifest(run_id="other")) == {}


def test_the_diff_names_the_added_removed_and_edited_skills():
    after = _manifest(
        skills=(
            ("equity-research", "different text"),
            ("new-skill", "x"),
        )
    )
    diff = manifest_diff(_manifest(), after)
    assert diff["skills"] == {
        "added": ["new-skill"],
        "removed": [],
        "edited": ["equity-research"],
    }


def test_the_diff_names_tool_changes():
    after = _manifest(tools=("get_quote", "run_backtest", "fresh_tool"))
    assert manifest_diff(_manifest(), after)["tools"] == {
        "added": ["fresh_tool"],
        "removed": ["screen"],
    }
