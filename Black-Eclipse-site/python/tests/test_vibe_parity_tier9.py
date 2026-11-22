"""Numeric parity, ninth tier: swarm DAG layering.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/swarm/task_store.py::topological_layers` + `validate_dag`). Its contract is
three sentences, all checkable:

* layer 0 is every task with no unmet prerequisite, and each later layer depends only
  on earlier ones (Kahn);
* a cycle makes layering impossible and the error **names the cycle path**;
* a dependency on an unknown task is a validation error, not a silent skip.

Eclipse's `team/graph.py` already layered correctly (and its `validate_graph` already
reported unknown dependencies and cycle paths). The two findings were:

* **the cycle error did not name the cycle** — it said only "the task graph contains a
  dependency cycle", leaving an operator to find it by hand while the reference prints
  the path. Fixed.
* the layer *partition* matched the reference exactly on every shared fixture, which is
  what this file proves rather than asserts.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

import pytest

from harness.team import graph as G


# -- a minimal subtask stand-in (the graph math only reads id + depends_on) --


@dataclass
class _Sub:
    id: str
    depends_on: list[str]


def tasks(*pairs):
    return [_Sub(task_id, list(deps)) for task_id, deps in pairs]


# -- the reference algorithm, restated from source -------------------------

def reference_layers(pairs) -> list[list[str]]:
    """Kahn layering exactly as `topological_layers` implements it."""
    in_degree = {task_id: len(deps) for task_id, deps in pairs}
    dependents = defaultdict(list)
    for task_id, deps in pairs:
        for dep in deps:
            dependents[dep].append(task_id)
    queue = deque(task_id for task_id, degree in in_degree.items() if degree == 0)
    layers = []
    processed = 0
    while queue:
        layer = list(queue)
        queue.clear()
        layers.append(layer)
        processed += len(layer)
        for task_id in layer:
            for downstream in dependents[task_id]:
                in_degree[downstream] -= 1
                if in_degree[downstream] == 0:
                    queue.append(downstream)
    if processed != len(pairs):
        raise ValueError("cycle")
    return layers


DAGS = {
    "single": [("a", [])],
    "linear": [("a", []), ("b", ["a"]), ("c", ["b"])],
    "diamond": [("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])],
    "two_roots": [("a", []), ("b", []), ("c", ["a", "b"]), ("d", ["c"])],
    "disconnected": [("a", []), ("b", []), ("c", ["a"])],
    "deep": [("a", []), ("b", ["a"]), ("c", ["b"]), ("d", ["c"]), ("e", ["d"])],
    "wide": [("root", []), ("x1", ["root"]), ("x2", ["root"]), ("x3", ["root"])],
}


@pytest.mark.parametrize("name", sorted(DAGS))
def test_the_layer_partition_matches_the_reference(name):
    pairs = DAGS[name]
    ours = G.topological_layers(tasks(*pairs))
    theirs = reference_layers(pairs)
    assert [sorted(layer) for layer in ours] == [sorted(layer) for layer in theirs]
    # and the property that makes layering useful: each prerequisite is earlier
    position = {task_id: index for index, layer in enumerate(ours) for task_id in layer}
    for task_id, deps in pairs:
        for dep in deps:
            assert position[dep] < position[task_id], (task_id, dep)


def test_layer_zero_is_every_root():
    pairs = DAGS["two_roots"]
    assert sorted(G.topological_layers(tasks(*pairs))[0]) == ["a", "b"]


def test_the_parallel_layer_really_is_parallel():
    """The diamond's two middle tasks must land in the SAME layer."""
    layers = G.topological_layers(tasks(*DAGS["diamond"]))
    assert sorted(layers[1]) == ["b", "c"]


def test_a_cycle_raises_and_names_the_path():
    pairs = [("a", ["c"]), ("b", ["a"]), ("c", ["b"])]
    with pytest.raises(ValueError) as exc:
        G.topological_layers(tasks(*pairs))
    message = str(exc.value)
    assert "cycle" in message
    # the reference prints the path; so must we
    assert "->" in message
    for task_id in ("a", "b", "c"):
        assert task_id in message


def test_a_self_loop_is_a_cycle():
    with pytest.raises(ValueError):
        G.topological_layers(tasks(("a", ["a"])))


def test_an_unknown_dependency_is_a_validation_error_not_a_silent_skip():
    """The reference raises `depends on unknown task`; ours reports it too."""
    problems = G.validate_graph(tasks(("a", ["ghost"])))
    assert any("unknown" in p and "ghost" in p for p in problems)


def test_validation_reports_duplicates_which_the_reference_never_sees():
    problems = G.validate_graph(tasks(("a", []), ("a", [])))
    assert any("duplicate" in p for p in problems)


def test_a_valid_graph_validates_clean():
    assert G.validate_graph(tasks(*DAGS["diamond"])) == []


def test_readiness_agrees_with_the_layering():
    """A task may run exactly when its prerequisites are placed, not before."""
    subtasks = tasks(*DAGS["diamond"])
    statuses = {}
    from harness.team.model import TeamSubtaskStatus

    assert sorted(G.ready_ids(subtasks, statuses)) == ["a"]
    statuses = {"a": TeamSubtaskStatus.COMPLETED}
    assert sorted(G.ready_ids(subtasks, statuses)) == ["b", "c"]
    statuses = {"a": TeamSubtaskStatus.COMPLETED, "b": TeamSubtaskStatus.COMPLETED}
    assert G.ready_ids(subtasks, statuses) == ["c"]
