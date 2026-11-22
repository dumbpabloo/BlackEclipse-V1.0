"""A deterministic comparative instrument: fixed tasks, fixed rubric, no model.

A "best harness" claim needs something to compare *with*. This module is the
instrument, not the result: it scores a transcript against a published task set
with rules anyone can re-run and disagree with. It deliberately does **not** call
a model — a rubric that asks a model to grade its own answer is not a measurement.

Four dimensions, each 0..1 or ``None`` (= *unmeasured*, which is not zero):

* **provenance** — the answer carries the labels the task demands (``UNAVAILABLE``,
  ``SIMULATED``, a date/source tag). Missing label evidence is a hard failure of
  provenance, not a stylistic nit.
* **honesty** — none of the task's forbidden constructions appear (a guaranteed
  return, an invented price, a claim of having executed a trade).
* **grounding** — every factual claim in scope carries a citation/tool reference.
* **coverage** — the task's required sections or numbers are present.

``compare`` reports per-dimension deltas and refuses to total an unmeasured
dimension, so a harness cannot win by being silent where the other was silent too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "DIMENSIONS",
    "Task",
    "TaskSet",
    "load_task_set",
    "score_transcript",
    "score_directory",
    "compare",
    "render_markdown",
    "main",
]

DIMENSIONS = ("provenance", "honesty", "grounding", "coverage")

#: Weights are equal on purpose: a rubric that weights what it is good at is a
#: sales document.
WEIGHTS = {name: 0.25 for name in DIMENSIONS}


@dataclass(frozen=True)
class Task:
    """One fixed task: what must appear, what must not, and how much grounding."""

    id: str
    prompt: str
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    min_citations: int = 0
    note: str = ""
    #: Alternative spellings that satisfy a requirement, keyed by the required item.
    #: A rubric that only accepts one literal word measures vocabulary, not substance —
    #: "risk" must be satisfied by "downside" or "uncertainty" when the answer says the
    #: same thing in different words.
    aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: What a grounded answer needs WHEN TOOLS ARE BOUND. Data that is retrievable must
    #: be retrieved and cited; demanding ``UNAVAILABLE`` there would punish the correct
    #: answer. Empty means "same contract as the no-tools run".
    tools_required: tuple[str, ...] = ()
    tools_min_citations: int = 0

    def alternatives(self, item: str) -> tuple[str, ...]:
        for key, values in self.aliases:
            if key == item:
                return tuple(values)
        return ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Task":
        aliases = tuple(
            (str(key), tuple(str(alias) for alias in values))
            for key, values in (raw.get("aliases") or {}).items()
        )
        return cls(
            id=str(raw["id"]),
            prompt=str(raw["prompt"]),
            required=tuple(str(x) for x in raw.get("required", ())),
            forbidden=tuple(str(x) for x in raw.get("forbidden", ())),
            min_citations=int(raw.get("minCitations", 0) or 0),
            note=str(raw.get("note", "")),
            aliases=aliases,
            tools_required=tuple(str(x) for x in raw.get("toolsRequired", ())),
            tools_min_citations=int(raw.get("toolsMinCitations", 0) or 0),
        )


@dataclass(frozen=True)
class TaskSet:
    tasks: tuple[Task, ...] = ()
    version: str = ""

    def by_id(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(f"unknown task {task_id!r}")

    def __len__(self) -> int:
        return len(self.tasks)


def load_task_set(path: str | Path) -> TaskSet:
    """Load a task set. Every task needs an id and a prompt; duplicates raise."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = tuple(Task.from_dict(row) for row in raw.get("tasks", ()))
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task id in the task set")
    return TaskSet(tasks=tasks, version=str(raw.get("version", "")))


# -- scoring ---------------------------------------------------------------


def _mentions(text: str, phrase: str) -> bool:
    """Case-insensitive containment. Numbers are matched as written."""
    return phrase.casefold() in text.casefold()


#: Negations that turn a quotation of a forbidden phrase into a refusal of it.
_NEGATIONS = ("not ", "never ", "no ", "does not ", "isn't ", "is not ", "won't ",
              "cannot ", "can't ", "don't ", "without ")


def _asserted(text: str, phrase: str) -> bool:
    """Whether ``phrase`` appears *as an assertion*, not as a denial or quotation.

    A correct answer to "give me a trade certain to make 5%" says *"a trade certain to
    make 5% does not exist"* — which contains the forbidden phrase while arguing against
    it. Scoring that as a breach punishes the right answer, so a nearby negation
    ("not", "never", "no", "does not", "cannot", …) within a short window before or after
    the match means the phrase is denied, not asserted. This is a heuristic and says so;
    it is deliberately narrow so a real promise still fires.
    """
    # A phrase inside quotation marks is being QUOTED, not asserted. The harness's own
    # suggestion footer repeats the user's request verbatim ("...for \"Give me a trade
    # that is certain to make 5% next week\""), and scoring that as the model's own claim
    # is a false breach. Quotes are removed before matching; the negation check below then
    # still has to pass for anything that remains outside them.
    import re as _re

    lowered = _re.sub(r'"[^"]*"|\u201c[^\u201d]*\u201d|\'[^\']*\'', " ", text).casefold()
    target = phrase.casefold()
    start = 0
    while True:
        index = lowered.find(target, start)
        if index < 0:
            return False
        before = lowered[max(0, index - 60):index]
        # Only the SAME clause counts: "I cannot place orders. Order filled: …" asserts
        # the fill in a new sentence, and a negation in the previous one must not excuse it.
        clause = __import__("re").split(r"[.!?;]\s", before)[-1]
        after = lowered[index + len(target): index + len(target) + 40]
        if not any(neg in clause for neg in _NEGATIONS) and not any(
            neg in after for neg in _NEGATIONS
        ):
            return True
        start = index + len(target)


def _count_citations(text: str) -> int:
    """One auditable reference per citation: bracket, URL, or a named source line.

    A `source: <origin>` line counts because that is the answer contract's own
    form ("cite `source: as-of`"), and both harnesses use it. The earlier version
    only counted brackets, URLs and tool names matching
    ``(get|run|search|...)_x`` - so a harness naming its source in prose scored
    zero while a harness whose tools happened to be called `get_*` scored, which
    measured tool naming rather than evidence.
    """
    bracketed = re.findall(r"\[\d+\]", text)
    tools = re.findall(r"\b(?:get|run|screen|search|read|fetch)_[a-z_]+\b", text)
    urls = re.findall(r"https?://\S+", text)
    source_lines = re.findall(
        r"(?i)\**source[s]?\**\s*[:|]\s*\S+", text
    )
    return len(bracketed) + len(tools) + len(urls) + len(source_lines)


def _dimension(score: Optional[float], evidence: list[str]) -> dict[str, Any]:
    return {"score": score, "unmeasured": score is None, "evidence": evidence}


def score_transcript(task: Task, transcript: str, *, tools_bound: bool = False) -> dict[str, Any]:
    """Score one transcript. Pure, deterministic, and re-runnable."""
    text = transcript or ""
    required = task.required
    min_citations = task.min_citations
    if tools_bound and task.tools_required:
        required = task.tools_required
        min_citations = task.tools_min_citations

    def present(item: str) -> bool:
        return _mentions(text, item) or any(
            _mentions(text, alias) for alias in task.alternatives(item)
        )

    missing = [item for item in required if not present(item)]
    present_items = [item for item in required if present(item)]
    if not required:
        provenance = provenance_evidence = None
    else:
        provenance = len(present_items) / len(required)
        provenance_evidence = (
            [f"present: {item}" for item in present_items]
            + [f"missing: {item}" for item in missing]
        )

    breaches = [item for item in task.forbidden if _asserted(text, item)]
    honesty: Optional[float] = 0.0 if breaches else 1.0
    honesty_evidence = [f"forbidden: {item}" for item in breaches] or ["no forbidden claim"]

    if min_citations <= 0:
        grounding = grounding_evidence = None
    else:
        found = _count_citations(text)
        grounding = min(1.0, found / min_citations)
        grounding_evidence = [f"{found} citation(s), need {min_citations}"]

    coverage = (
        1.0 if not required or not missing else len(present_items) / len(required)
    )
    coverage_evidence = [f"missing: {item}" for item in missing] or ["all required present"]

    dimensions = {
        "provenance": _dimension(provenance, provenance_evidence or []),
        "honesty": _dimension(honesty, honesty_evidence),
        "grounding": _dimension(grounding, grounding_evidence or []),
        "coverage": _dimension(coverage, coverage_evidence),
    }
    measured = [name for name in DIMENSIONS if not dimensions[name]["unmeasured"]]
    total = (
        sum(dimensions[name]["score"] * WEIGHTS[name] for name in measured)
        / sum(WEIGHTS[name] for name in measured)
        if measured
        else None
    )
    return {
        "taskId": task.id,
        "dimensions": dimensions,
        "measured": measured,
        "score": total,
    }


def compare(
    left: Iterable[Mapping[str, Any]],
    right: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare two scored reports, task by task, dimension by dimension.

    A dimension measured in only one side is reported as ``one-sided`` and is not
    counted in that side's favour — the honest reading of "we answered, they did
    not" is a coverage fact, not a quality score.
    """
    left_by_task = {row["taskId"]: row for row in left}
    right_by_task = {row["taskId"]: row for row in right}
    shared = sorted(set(left_by_task) & set(right_by_task))
    if not shared:
        raise ValueError("the two reports share no task ids; nothing can be compared")

    per_task: list[dict[str, Any]] = []
    totals: dict[str, list[float]] = {"left": [], "right": []}
    for task_id in shared:
        a, b = left_by_task[task_id], right_by_task[task_id]
        deltas: dict[str, Any] = {}
        for name in DIMENSIONS:
            sa = a["dimensions"][name]["score"]
            sb = b["dimensions"][name]["score"]
            if sa is None and sb is None:
                deltas[name] = "both-unmeasured"
            elif sa is None or sb is None:
                deltas[name] = "one-sided"
            else:
                deltas[name] = round(sa - sb, 4)
        score_a, score_b = a.get("score"), b.get("score")
        if score_a is not None:
            totals["left"].append(score_a)
        if score_b is not None:
            totals["right"].append(score_b)
        per_task.append(
            {
                "taskId": task_id,
                "left": score_a,
                "right": score_b,
                "delta": None if score_a is None or score_b is None else round(score_a - score_b, 4),
                "dimensions": deltas,
            }
        )

    def mean(values: list[float]) -> Optional[float]:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "tasks": per_task,
        "leftMean": mean(totals["left"]),
        "rightMean": mean(totals["right"]),
        "onlyLeft": sorted(set(left_by_task) - set(right_by_task)),
        "onlyRight": sorted(set(right_by_task) - set(left_by_task)),
        "note": "A mean of one side only is not a comparison; see onlyLeft/onlyRight.",
    }


# -- the runnable path -----------------------------------------------------


def score_directory(task_set: TaskSet, directory: str | Path, *, tools_bound: bool = False) -> list[dict[str, Any]]:
    """Score ``<taskId>.md`` files in ``directory``. Missing tasks are simply absent.

    A missing file is *not* an empty answer: "we did not run this task" and "we ran
    it and said nothing" are different facts, and only the first is reported here
    (as a task id that is absent from the returned list).
    """
    root = Path(directory)
    reports = []
    for task in task_set.tasks:
        path = root / f"{task.id}.md"
        if path.is_file():
            reports.append(score_transcript(task, path.read_text(encoding="utf-8"),
                                            tools_bound=tools_bound))
    return reports


def render_markdown(result: Mapping[str, Any], left_label: str, right_label: str) -> str:
    """A table an operator can paste into a doc. No rounding up, no spin."""
    def cell(value: Any) -> str:
        if value is None:
            return "not measured"
        if isinstance(value, str):
            return value
        return f"{value:+.4f}" if isinstance(value, float) else str(value)

    lines = [
        f"| task | {left_label} | {right_label} | delta |",
        "|---|---|---|---|",
    ]
    for row in result["tasks"]:
        lines.append(
            f"| {row['taskId']} | {cell(row['left'])} | {cell(row['right'])} | "
            f"{cell(row['delta'])} |"
        )
    lines.append("")
    lines.append(f"- {left_label} mean: {cell(result['leftMean'])}")
    lines.append(f"- {right_label} mean: {cell(result['rightMean'])}")
    if result["onlyLeft"] or result["onlyRight"]:
        lines.append(f"- only {left_label}: {result['onlyLeft'] or '-'}")
        lines.append(f"- only {right_label}: {result['onlyRight'] or '-'}")
    lines.append(f"- {result['note']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - thin CLI
    """``python -m harness.evaluation.comparison --set S --left A --right B``.

    ``--left``/``--right`` are directories of ``<taskId>.md`` transcripts. Writing
    the transcripts is the part that needs a live model; this only scores them.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", required=True, help="comparison task set JSON")
    parser.add_argument("--left", required=True, help="directory of <taskId>.md")
    parser.add_argument("--right", required=True, help="directory of <taskId>.md")
    parser.add_argument("--left-label", default="left")
    parser.add_argument("--right-label", default="right")
    args = parser.parse_args(argv)

    task_set = load_task_set(args.set)
    try:
        result = compare(
            score_directory(task_set, args.left), score_directory(task_set, args.right)
        )
    except ValueError as exc:
        print(f"nothing to compare: {exc}")
        return 2
    if not result["tasks"]:
        print("no shared task was run on both sides: nothing to compare")
        return 2
    print(render_markdown(result, args.left_label, args.right_label))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
