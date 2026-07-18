"""TaskContract and load_task — load task definitions from AI2Thor/Tasks/.

Supports dynamic loading of checker.py for task-specific subtask lists
and coverage object requirements.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskContract:
    """Contract for an AI2Thor task.

    Attributes:
        task_id: Identifier matching AI2Thor/Tasks/<task_id>/.
        scene: Unity scene name (e.g. "FloorPlan1").
        num_agents: Expected number of cooperating agents.
        subtasks: Ordered list of subtask descriptions.
        coverage_objects: Object types that must be in correct locations.
        initial_inventory: Per-agent initial inventory (agent_idx -> list of item types).
    """

    task_id: str
    scene: str = "FloorPlan1"
    num_agents: int = 2
    subtasks: list[str] = field(default_factory=list)
    coverage_objects: list[str] = field(default_factory=list)
    initial_inventory: dict[int, list[str]] = field(default_factory=dict)


# ── Known task base path ───────────────────────────────────────────────────

_TASKS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "AI2Thor",
    "Tasks",
)


def _import_checker(task_id: str) -> Any:
    """Dynamically import the Checker class from AI2Thor/Tasks/<task_id>/checker.py.

    Returns:
        The Checker module object.

    Raises:
        NotImplementedError: If the task directory or checker.py does not exist.
    """
    task_dir = os.path.join(_TASKS_ROOT, task_id)
    checker_path = os.path.join(task_dir, "checker.py")

    if not os.path.isdir(task_dir):
        raise NotImplementedError(
            f"Task '{task_id}' not found at {task_dir}. "
            f"Only '3_transport_groceries' is supported in this version."
        )
    if not os.path.isfile(checker_path):
        raise NotImplementedError(
            f"Task '{task_id}' has no checker.py at {checker_path}. "
            f"Only '3_transport_groceries' is supported in this version."
        )

    # Import using importlib.util to avoid polluting sys.modules with a short name
    spec = importlib.util.spec_from_file_location(
        f"_task_checker_{task_id}", checker_path
    )
    if spec is None or spec.loader is None:
        raise NotImplementedError(f"Could not load checker for task '{task_id}'")

    module = importlib.util.module_from_spec(spec)
    # Temporarily add task_dir to sys.path so internal imports in checker.py work
    old_path = list(sys.path)
    try:
        if task_dir not in sys.path:
            sys.path.insert(0, task_dir)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path

    return module


def _checker_for_task(task_id: str) -> Any:
    """Return an instantiated Checker for *task_id*.

    Raises NotImplementedError for unsupported tasks.
    """
    if task_id != "3_transport_groceries":
        raise NotImplementedError(
            f"Task '{task_id}' is not supported yet. "
            f"Only '3_transport_groceries' is available in this version."
        )

    module = _import_checker(task_id)
    if not hasattr(module, "Checker"):
        raise NotImplementedError(
            f"checker.py for '{task_id}' does not define a Checker class."
        )
    return module.Checker()


def load_task(task_id: str, scene: str = "FloorPlan1") -> TaskContract:
    """Load a TaskContract from AI2Thor/Tasks/<task_id>/.

    The task's checker.py provides subtasks and coverage_objects,
    while FloorPlan<scene>.py (if it exists) may provide scene-specific
    initialisation data.

    Args:
        task_id: Task identifier (e.g. ``"3_transport_groceries"``).
        scene: Scene name (default ``"FloorPlan1"``).

    Returns:
        A populated :class:`TaskContract`.

    Raises:
        NotImplementedError: If *task_id* is not supported.
    """
    checker = _checker_for_task(task_id)

    # Extract subtasks and coverage from the Checker instance
    subtasks: list[str] = list(getattr(checker, "subtasks", []))
    coverage_objects: list[str] = list(getattr(checker, "coverage", []))

    # Try to load FloorPlan<scene>.py for scene-specific data (optional)
    scene_initializer = None
    scene_file = os.path.join(_TASKS_ROOT, task_id, f"{scene}.py")
    if os.path.isfile(scene_file):
        try:
            spec = importlib.util.spec_from_file_location(
                f"_task_scene_{task_id}_{scene}", scene_file
            )
            if spec and spec.loader:
                scene_mod = importlib.util.module_from_spec(spec)
                old_path = list(sys.path)
                try:
                    task_dir = os.path.join(_TASKS_ROOT, task_id)
                    if task_dir not in sys.path:
                        sys.path.insert(0, task_dir)
                    spec.loader.exec_module(scene_mod)
                finally:
                    sys.path[:] = old_path
                if hasattr(scene_mod, "SceneInitializer"):
                    scene_initializer = scene_mod.SceneInitializer()
        except Exception:
            # FloorPlan loading is best-effort; non-critical
            pass

    num_agents = 2  # Default for transport_groceries; could be overridden

    return TaskContract(
        task_id=task_id,
        scene=scene,
        num_agents=num_agents,
        subtasks=subtasks,
        coverage_objects=coverage_objects,
        initial_inventory={},
    )
