"""Computer Use evaluation: the suite runner and the OSWorld bridge."""

from .osworld import build_osworld_manager, run_osworld_episode
from .suite import EvalTask, SuiteReport, TaskResult, run_suite
from .windows_suite import LaunchedTarget, LocalWindowsSuite, launch_target, permissive_policy

__all__ = [
    "EvalTask", "SuiteReport", "TaskResult", "run_suite",
    "build_osworld_manager", "run_osworld_episode",
    "LaunchedTarget", "LocalWindowsSuite", "launch_target", "permissive_policy",
]
