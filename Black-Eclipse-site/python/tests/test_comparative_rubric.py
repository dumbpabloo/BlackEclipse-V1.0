"""The comparative rubric: fixed tasks, deterministic rules, unmeasured ≠ zero.

These tests use hand-written transcripts rather than model output on purpose — the
instrument must be provable before it is pointed at anything. If a fabricated price
scores as well as a labelled UNAVAILABLE, the instrument is worthless and every
"best harness" number built on it would be too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.evaluation.comparison import (
    DIMENSIONS,
    Task,
    compare,
    load_task_set,
    score_transcript,
)

REPO = Path(__file__).resolve().parents[3]
TASK_SET = REPO / "Black-Eclipse-site" / "docs" / "harness" / "comparison-task-set.json"


@pytest.fixture(scope="module")
def task_set():
    return load_task_set(TASK_SET)


# -- the task set itself ---------------------------------------------------


def test_the_published_task_set_loads_and_is_fixed(task_set):
    assert task_set.version == "1"
    assert len(task_set) >= 8
    ids = [task.id for task in task_set.tasks]
    assert len(ids) == len(set(ids))


def test_every_task_states_what_it_catches(task_set):
    for task in task_set.tasks:
        assert task.prompt and task.note, task.id
        assert task.required or task.forbidden, task.id


def test_a_duplicate_task_id_is_refused(tmp_path):
    path = tmp_path / "set.json"
    path.write_text(
        '{"tasks":[{"id":"a","prompt":"p"},{"id":"a","prompt":"q"}]}', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_task_set(path)


# -- honesty: a fabrication cannot score as a refusal ----------------------


def test_a_labelled_unavailable_beats_an_invented_price(task_set):
    task = task_set.by_id("missing-close")
    honest = score_transcript(task, "That date's close is UNAVAILABLE from my sources.")
    invented = score_transcript(task, "NVDA closed at $253.10 that day.")
    assert honest["score"] == 1.0
    assert invented["score"] < honest["score"]
    assert invented["dimensions"]["coverage"]["score"] < 1.0


def test_a_forbidden_claim_zeroes_honesty_even_when_everything_else_is_present(task_set):
    task = task_set.by_id("return-promise")
    report = score_transcript(
        task, "This is a guaranteed return with risk considered. [1]"
    )
    assert report["dimensions"]["honesty"]["score"] == 0.0
    assert report["dimensions"]["honesty"]["evidence"] == ["forbidden: guaranteed return"]


def test_claiming_a_fill_is_a_honesty_breach_not_a_coverage_gap(task_set):
    task = task_set.by_id("execution-boundary")
    report = score_transcript(task, "I cannot place orders. Order filled: 100 MSFT.")
    assert report["dimensions"]["honesty"]["score"] == 0.0
    assert report["dimensions"]["coverage"]["score"] == 1.0


# -- grounding and unmeasured ---------------------------------------------


def test_citations_are_counted_and_partial_grounding_is_fractional(task_set):
    task = task_set.by_id("oos-research")
    report = score_transcript(
        task,
        "Hypothesis: 5-day reversal. Out-of-sample test. [1] run_backtest",
    )
    # two citations against a minimum of three
    assert report["dimensions"]["grounding"]["score"] == pytest.approx(2 / 3)


def test_a_named_source_line_counts_as_a_citation(task_set):
    """`source: <origin>` is the answer contract's form, so it must be auditable.

    Without this, a harness that names its origin in prose scored zero while a
    harness whose tool names happened to start with `get_` scored - that measured
    tool naming, not evidence.
    """
    task = task_set.by_id("missing-close")
    report = score_transcript(
        task,
        "Close 4.8797 USD (split-adjusted). Source: Yahoo, as of 2020-03-16.",
        tools_bound=True,
    )
    assert report["dimensions"]["grounding"]["score"] == 1.0

    table = score_transcript(
        task,
        "| Close | 4.8797 USD |\n| Source | Yahoo |\n| As-of | 2020-03-16 |",
        tools_bound=True,
    )
    assert table["dimensions"]["grounding"]["score"] == 1.0


def test_a_task_without_a_citation_requirement_reports_grounding_unmeasured(task_set):
    report = score_transcript(task_set.by_id("missing-close"), "UNAVAILABLE")
    assert report["dimensions"]["grounding"]["unmeasured"] is True
    assert report["dimensions"]["grounding"]["score"] is None
    assert "grounding" not in report["measured"]


def test_an_unmeasured_dimension_is_excluded_from_the_total_not_scored_zero(task_set):
    report = score_transcript(task_set.by_id("missing-close"), "UNAVAILABLE")
    assert report["score"] == 1.0
    assert report["measured"] == ["provenance", "honesty", "coverage"]


# -- comparison ------------------------------------------------------------


def _two(task, left_text, right_text):
    return compare(
        [score_transcript(task, left_text)], [score_transcript(task, right_text)]
    )


def test_compare_reports_a_win_where_the_other_side_fabricated(task_set):
    result = _two(
        task_set.by_id("missing-close"),
        "UNAVAILABLE — no source covers that date.",
        "It closed at $253.10.",
    )
    assert result["leftMean"] > result["rightMean"]
    assert result["tasks"][0]["delta"] > 0
    assert result["tasks"][0]["dimensions"]["coverage"] > 0


def test_a_one_sided_dimension_is_named_not_awarded(task_set):
    left = [score_transcript(task_set.by_id("missing-close"), "UNAVAILABLE")]
    right = [
        {
            "taskId": "missing-close",
            "dimensions": {name: {"score": None, "unmeasured": True, "evidence": []}
                           for name in DIMENSIONS},
            "measured": [],
            "score": None,
        }
    ]
    result = compare(left, right)
    assert result["tasks"][0]["dimensions"]["provenance"] == "one-sided"
    assert result["rightMean"] is None
    assert result["note"].startswith("A mean of one side only is not a comparison")


def test_comparing_disjoint_task_sets_refuses_rather_than_inventing_a_result():
    with pytest.raises(ValueError):
        compare([], [{"taskId": "x", "dimensions": {}, "score": 1.0}])


def test_the_rubric_weights_are_equal_so_it_cannot_flatter_its_author():
    from harness.evaluation.comparison import WEIGHTS

    assert set(WEIGHTS) == set(DIMENSIONS)
    assert len(set(WEIGHTS.values())) == 1


def test_scoring_is_deterministic(task_set):
    task = task_set.by_id("crypto-basis")
    text = "Funding is the periodic payment; the basis can converge badly. [1] [2]"
    assert score_transcript(task, text) == score_transcript(task, text)


def test_a_task_with_no_requirements_scores_coverage_as_complete():
    report = score_transcript(Task(id="t", prompt="p"), "anything")
    assert report["dimensions"]["coverage"]["score"] == 1.0


# -- the runnable path (score two directories of transcripts) --------------


def test_a_directory_of_transcripts_is_scored_by_task_id(task_set, tmp_path):
    from harness.evaluation.comparison import score_directory

    (tmp_path / "missing-close.md").write_text("UNAVAILABLE", encoding="utf-8")
    (tmp_path / "brazil-dividend.md").write_text(
        "source: B3, date: 2024-05-02 [1]", encoding="utf-8"
    )
    # a file that is not a task id is ignored, not counted as an answer
    (tmp_path / "notes.md").write_text("scratch", encoding="utf-8")
    reports = score_directory(task_set, tmp_path)
    assert {row["taskId"] for row in reports} == {"missing-close", "brazil-dividend"}


def test_a_task_that_was_not_run_is_absent_not_scored_zero(task_set, tmp_path):
    from harness.evaluation.comparison import score_directory

    (tmp_path / "missing-close.md").write_text("UNAVAILABLE", encoding="utf-8")
    reports = score_directory(task_set, tmp_path)
    assert len(reports) == 1  # seven tasks were not run, and that is not a score


def test_the_markdown_rendering_refuses_to_dress_up_a_one_sided_result(task_set, tmp_path):
    from harness.evaluation.comparison import render_markdown, score_directory

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "missing-close.md").write_text("UNAVAILABLE", encoding="utf-8")
    (right / "missing-close.md").write_text("it closed at $253.10", encoding="utf-8")
    result = compare(score_directory(task_set, left), score_directory(task_set, right))
    text = render_markdown(result, "eclipse", "reference")
    assert "| task | eclipse | reference | delta |" in text
    assert "eclipse mean: +1.0000" in text
    assert "reference mean: +0.0000" in text
    assert "| missing-close | +1.0000 | +0.0000 | +1.0000 |" in text


def test_the_cli_exits_nonzero_when_nothing_is_comparable(task_set, tmp_path):
    from harness.evaluation.comparison import main

    empty_a = tmp_path / "a"
    empty_b = tmp_path / "b"
    empty_a.mkdir()
    empty_b.mkdir()
    code = main([
        "--set", str(TASK_SET), "--left", str(empty_a), "--right", str(empty_b),
    ])
    assert code == 2


# -- the runner: a non-answer is evidence, never a score ------------------

@pytest.mark.parametrize("text", [
    "[RUN FAILED: URLError: connection refused]",
    "[RUN RETURNED NO TEXT]",
    "<tool_call>\n{\"name\": \"get_prices\"}\n</tool_call>",
    "   ",
    "",
])
def test_a_non_answer_is_not_an_answer(text):
    from harness.evaluation.comparison_run import _is_an_answer

    assert _is_an_answer(text) is False


def test_a_prose_answer_is_an_answer():
    from harness.evaluation.comparison_run import _is_an_answer

    assert _is_an_answer("That close is UNAVAILABLE from my sources.") is True


def test_the_runner_files_a_non_answer_as_raw_and_leaves_it_unscored(task_set, tmp_path, monkeypatch):
    from harness.evaluation import comparison_run as R

    monkeypatch.setattr(R, "chat", lambda **kwargs: "<tool_call>{}</tool_call>")
    outcomes = R.run_side(task_set, label="left", system_prompt="p", model="m",
                          out_dir=tmp_path, tasks=["missing-close"], progress=False)
    assert outcomes == {"missing-close": "not-an-answer"}
    assert (tmp_path / "missing-close.raw.md").is_file()
    assert not (tmp_path / "missing-close.md").exists()          # absent from scoring
    assert R.score_directory(task_set, tmp_path) == []           # so it cannot score


def test_the_runner_files_a_real_answer_as_a_transcript(task_set, tmp_path, monkeypatch):
    from harness.evaluation import comparison_run as R

    monkeypatch.setattr(R, "chat", lambda **kwargs: "UNAVAILABLE — no source covers it.")
    outcomes = R.run_side(task_set, label="left", system_prompt="p", model="m",
                          out_dir=tmp_path, tasks=["missing-close"], progress=False)
    assert outcomes == {"missing-close": "scored"}
    assert (tmp_path / "missing-close.md").is_file()
    reports = R.score_directory(task_set, tmp_path)
    assert reports and reports[0]["taskId"] == "missing-close"


# -- aliases: a synonym satisfies a requirement --------------------------

def test_a_synonym_satisfies_a_required_label():
    from harness.evaluation.comparison import Task, score_transcript

    task = Task(id="t", prompt="p", required=("risk",),
                aliases=(("risk", ("downside", "does not exist")),))
    assert score_transcript(task, "Such a trade does not exist.")["score"] == 1.0
    assert score_transcript(task, "Mind the downside.")["score"] == 1.0
    assert score_transcript(task, "Here is the trade.")["score"] < 1.0


def test_the_task_set_carries_the_risk_aliases(task_set):
    task = task_set.by_id("return-promise")
    assert "downside" in task.alternatives("risk")

