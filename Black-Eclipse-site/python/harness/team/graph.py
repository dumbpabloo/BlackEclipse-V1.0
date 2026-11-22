"""Pure task-graph math for a team run — validation, layering and readiness.

This is deliberately a thin, dependency-free layer over the SAME DAG primitives
the Task runtime uses (`tasks.dag`), so a team graph and a plan graph cannot
disagree about what a cycle or a satisfied dependency is. It never touches the
store or the runtime.
"""

from __future__ import annotations

from .model import TeamSubtask, TeamSubtaskStatus

from ..tasks.dag import detect_cycle as _detect_cycle
from ..tasks.dag import topological_layers as _topological_layers
from ..tasks.model import TaskStatus

# A prerequisite satisfies a dependent only when it has genuinely completed.
SATISFIED: frozenset[TeamSubtaskStatus] = frozenset({TeamSubtaskStatus.COMPLETED})
FAILED_PREREQ: frozenset[TeamSubtaskStatus] = frozenset(
    {TeamSubtaskStatus.FAILED, TeamSubtaskStatus.CANCELLED, TeamSubtaskStatus.SKIPPED}
)

_TASK_TO_SUBTASK: dict[TaskStatus, TeamSubtaskStatus] = {
    TaskStatus.TODO: TeamSubtaskStatus.PENDING,
    TaskStatus.IN_PROGRESS: TeamSubtaskStatus.RUNNING,
    TaskStatus.VERIFYING: TeamSubtaskStatus.RUNNING,
    TaskStatus.ASKING_USER: TeamSubtaskStatus.RUNNING,
    TaskStatus.WAITING: TeamSubtaskStatus.RUNNING,
    TaskStatus.BLOCKED: TeamSubtaskStatus.BLOCKED,
    TaskStatus.FAILED: TeamSubtaskStatus.FAILED,
    TaskStatus.COMPLETED: TeamSubtaskStatus.COMPLETED,
    TaskStatus.CANCELLED: TeamSubtaskStatus.CANCELLED,
}


def map_task_status(status: TaskStatus | str) -> TeamSubtaskStatus:
    """Project a durable Task status onto the team subtask vocabulary."""
    if isinstance(status, str):
        try:
            status = TaskStatus(status)
        except ValueError:
            return TeamSubtaskStatus.PENDING
    return _TASK_TO_SUBTASK.get(status, TeamSubtaskStatus.PENDING)


def edges(subtasks: list[TeamSubtask]) -> list[tuple[str, str]]:
    """The dependency edges as ``(dependent, prerequisite)`` pairs."""
    out: list[tuple[str, str]] = []
    for subtask in subtasks:
        for dep in subtask.depends_on:
            out.append((subtask.id, dep))
    return out


def validate_graph(subtasks: list[TeamSubtask]) -> list[str]:
    """Return every structural problem with the graph (empty = valid).

    Detects duplicate ids, unknown dependencies and cycles. Never raises: the
    caller decides how to refuse.
    """
    errors: list[str] = []
    ids = [s.id for s in subtasks]
    seen: set[str] = set()
    for subtask_id in ids:
        if subtask_id in seen:
            errors.append(f"duplicate subtask id {subtask_id!r}")
        seen.add(subtask_id)
    for subtask in subtasks:
        for dep in subtask.depends_on:
            if dep not in seen:
                errors.append(f"subtask {subtask.id!r} depends on unknown subtask {dep!r}")
    if not errors:
        cycle = _detect_cycle(edges(subtasks))
        if cycle:
            errors.append("dependency cycle: " + " -> ".join(cycle))
    return errors


def topological_layers(subtasks: list[TeamSubtask]) -> list[list[str]]:
    """Kahn layers of the graph; raises ``ValueError`` naming the cycle.

    The reference's ``validate_dag`` reports the cycle path, and a bare "there is a
    cycle" leaves an operator to find it by hand — so the message carries the path.
    """
    edges_ = edges(subtasks)
    layers = _topological_layers([s.id for s in subtasks], edges_)
    if layers is None:
        cycle = _detect_cycle(edges_)
        detail = " -> ".join(cycle) if cycle else "unresolved"
        raise ValueError(f"the task graph contains a dependency cycle: {detail}")
    return layers


def unmet_dependencies(subtask: TeamSubtask, statuses: dict[str, TeamSubtaskStatus]) -> list[str]:
    """Prerequisites of ``subtask`` that are not yet satisfied."""
    return [dep for dep in subtask.depends_on if statuses.get(dep) not in SATISFIED]


def failed_dependencies(subtask: TeamSubtask, statuses: dict[str, TeamSubtaskStatus]) -> list[str]:
    """Prerequisites of ``subtask`` that are terminal-but-unsatisfied."""
    return [dep for dep in subtask.depends_on if statuses.get(dep) in FAILED_PREREQ]


def is_runnable(subtask: TeamSubtask, statuses: dict[str, TeamSubtaskStatus]) -> bool:
    """A PENDING subtask with every prerequisite COMPLETED may run."""
    if statuses.get(subtask.id) not in (None, TeamSubtaskStatus.PENDING):
        return False
    return not unmet_dependencies(subtask, statuses)


def ready_ids(
    subtasks: list[TeamSubtask], statuses: dict[str, TeamSubtaskStatus]
) -> list[str]:
    """Every subtask that can run right now, in stable declaration order."""
    return [s.id for s in subtasks if is_runnable(s, statuses)]


__all__ = [
    "FAILED_PREREQ",
    "SATISFIED",
    "edges",
    "failed_dependencies",
    "is_runnable",
    "map_task_status",
    "ready_ids",
    "topological_layers",
    "unmet_dependencies",
    "validate_graph",
]
