"""TaskContract and load_task — load task definitions from AI2Thor/Tasks/.

Supports dynamic loading of checker.py for task-specific subtask lists
and coverage object requirements, plus the scene-side SceneInitializer
(``<scene>.py``) that defines the task's object layout (D5: the initial
layout must come from the task's preinit, otherwise results carry no
alignment value against the paper's MAP-THOR runs).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import sys
import types
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


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
        scene_initializer: The task's ``SceneInitializer`` instance (from
            ``AI2Thor/Tasks/<task_id>/<scene>.py``), consumed by the
            controller layer to run ``preinit`` before the first round.
            ``None`` when the task/scene has no layout file.  Not part of
            equality/hash/repr — it is a loaded runtime helper, not data.
    """

    task_id: str
    scene: str = "FloorPlan1"
    num_agents: int = 2
    subtasks: list[str] = field(default_factory=list)
    coverage_objects: list[str] = field(default_factory=list)
    initial_inventory: dict[int, list[str]] = field(default_factory=dict)
    scene_initializer: Any = field(default=None, compare=False, repr=False)


# ── Known task base path ───────────────────────────────────────────────────

_TASKS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "AI2Thor",
    "Tasks",
)


_BASELINES_LOGGING_MODULE = "AI2Thor.baselines.utils.logging"


def _ensure_baselines_logger_stub() -> None:
    """Break the ``AI2Thor.baselines.utils`` package-__init__ import chain.

    20 of the 24 task checkers subclass ``BaseChecker`` via
    ``from AI2Thor.baselines.utils.checker import BaseChecker``. That package's
    ``__init__`` first imports ``Logger`` from ``.logging``, whose module body
    pulls pandas/imageio — heavy baseline-only deps this runtime does not
    install (and the A100 execution tree deliberately reuses its venv instead
    of re-syncing). Checkers never use ``Logger``, so a bare stub module with
    an importable ``Logger`` name is registered under the canonical dotted
    name: pure code, no new dependencies, nothing under AI2Thor/ touched.

    Idempotent; an already-imported (real or stub) module wins.
    """
    if _BASELINES_LOGGING_MODULE in sys.modules:
        return

    stub = types.ModuleType(_BASELINES_LOGGING_MODULE)

    class _LoggerStub:
        """Importability placeholder — checkers never instantiate Logger."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    stub.Logger = _LoggerStub  # type: ignore[attr-defined]
    sys.modules[_BASELINES_LOGGING_MODULE] = stub
    logger.debug(
        "Registered stub for %s (pandas/imageio chain cut)", _BASELINES_LOGGING_MODULE
    )


def _import_checker(task_id: str) -> Any:
    """Dynamically import the Checker class from AI2Thor/Tasks/<task_id>/checker.py.

    Returns:
        The Checker module object.

    Raises:
        NotImplementedError: If the task directory or checker.py does not exist
            (a task loads whenever ``<task_id>/checker.py`` exists).
    """
    task_dir = os.path.join(_TASKS_ROOT, task_id)
    checker_path = os.path.join(task_dir, "checker.py")

    if not os.path.isdir(task_dir):
        raise NotImplementedError(
            f"Task '{task_id}' not found at {task_dir}. "
            f"Requires AI2Thor/Tasks/<task_id>/ with a checker.py."
        )
    if not os.path.isfile(checker_path):
        raise NotImplementedError(
            f"Task '{task_id}' has no checker.py at {checker_path}. "
            f"Requires AI2Thor/Tasks/<task_id>/ with a checker.py."
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
        # ``from AI2Thor.baselines.utils.checker import BaseChecker`` (20/24
        # checkers) goes through a package __init__ that imports Logger →
        # pandas/imageio; stub the logging module first so the chain loads
        # without the heavy baseline-only deps.
        _ensure_baselines_logger_stub()
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path

    return module


def _checker_for_task(task_id: str) -> Any:
    """Return an instantiated Checker for *task_id*.

    Any task whose directory contains a checker.py is loadable (the checker
    must define a ``Checker`` class exposing ``subtasks`` / ``coverage``).

    Raises:
        NotImplementedError: If the task directory / checker.py is missing or
            the checker defines no ``Checker`` class.
    """
    module = _import_checker(task_id)
    if not hasattr(module, "Checker"):
        raise NotImplementedError(
            f"checker.py for '{task_id}' does not define a Checker class."
        )
    return module.Checker()


def _load_scene_initializer(task_id: str, scene: str) -> Any:
    """Load the task's ``SceneInitializer`` from ``<task_id>/<scene>.py``.

    - File missing → ``None`` (no task-layout override for this scene, the
      same "not applicable" branch as the original ``get_scene_initializer``).
    - File present but fails to import → raise (a silently skipped task
      layout means ``preinit`` never runs and the run is not aligned).
    - File present without a ``SceneInitializer`` class → warning + ``None``.
    """
    scene_file = os.path.join(_TASKS_ROOT, task_id, f"{scene}.py")
    if not os.path.isfile(scene_file):
        return None

    spec = importlib.util.spec_from_file_location(
        f"_task_scene_{task_id}_{scene}", scene_file
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load scene file {scene_file}")

    scene_mod = importlib.util.module_from_spec(spec)
    old_path = list(sys.path)
    try:
        task_dir = os.path.join(_TASKS_ROOT, task_id)
        if task_dir not in sys.path:
            sys.path.insert(0, task_dir)
        spec.loader.exec_module(scene_mod)
    finally:
        sys.path[:] = old_path

    if not hasattr(scene_mod, "SceneInitializer"):
        logger.warning(
            "Scene file %s defines no SceneInitializer; no task layout override",
            scene_file,
        )
        return None
    return scene_mod.SceneInitializer()


def load_task(task_id: str, scene: str = "FloorPlan1") -> TaskContract:
    """Load a TaskContract from AI2Thor/Tasks/<task_id>/.

    The task's checker.py provides subtasks and coverage_objects,
    while FloorPlan<scene>.py (if it exists) provides the SceneInitializer
    that defines the task's initial object layout (D5).

    Args:
        task_id: Task identifier (e.g. ``"3_transport_groceries"``).
        scene: Scene name (default ``"FloorPlan1"``).

    Returns:
        A populated :class:`TaskContract`.

    Raises:
        NotImplementedError: If *task_id* is not supported (no directory /
            checker.py / ``Checker`` class).
    """
    checker = _checker_for_task(task_id)

    # Extract subtasks and coverage from the Checker instance
    subtasks: list[str] = list(getattr(checker, "subtasks", []))
    coverage_objects: list[str] = list(getattr(checker, "coverage", []))

    scene_initializer = _load_scene_initializer(task_id, scene)

    num_agents = 2  # Default for transport_groceries; could be overridden

    return TaskContract(
        task_id=task_id,
        scene=scene,
        num_agents=num_agents,
        subtasks=subtasks,
        coverage_objects=coverage_objects,
        initial_inventory={},
        scene_initializer=scene_initializer,
    )


# ── SceneInitializer.preinit invocation (D5 task-layout wiring) ─────────────


def _resolve_preinit(scene_initializer: Any) -> tuple[Any, bool]:
    """Resolve how ``scene_initializer.preinit`` must be invoked.

    Returns ``(raw_function, False)`` when ``preinit`` is written without
    ``self`` (the historical ``def preinit(event, controller)`` form used by
    9 FloorPlan files — the original repo's own bug), or ``(None, True)``
    for the standard bound-method form ``def preinit(self, event, controller)``.
    """
    raw = inspect.getattr_static(scene_initializer, "preinit", None)
    if raw is None:
        raise RuntimeError(
            f"{type(scene_initializer).__name__} defines no preinit method"
        )
    if isinstance(raw, staticmethod):
        # ``@staticmethod def preinit(event, controller)``: same call shape
        # as the missing-self form.
        return raw.__func__, False
    if inspect.isfunction(raw):
        try:
            params = list(inspect.signature(raw).parameters.values())
        except (TypeError, ValueError):
            return None, True
        positional = [
            p
            for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        if len(positional) == 2:
            # Missing ``self``: calling through the instance would pass the
            # instance as an extra first argument and raise TypeError.
            return raw, False
    return None, True


def invoke_scene_preinit(scene_initializer: Any, event: Any, controller: Any) -> Any:
    """Invoke the task's ``SceneInitializer.preinit`` (D5 layout wiring).

    Mirrors the original ``AI2Thor/env_new.py`` reset() call
    (``scene_initializer.SceneInitializer().preinit(event, controller)``)
    with dual-signature compatibility: the 9 FloorPlan files whose ``preinit``
    is missing ``self`` are called as bare functions, everything else through
    the instance.

    Fail-fast: any failure is logged and re-raised as ``RuntimeError`` — a
    failed layout initialisation means the run no longer matches the task
    definition, so it must never be skipped silently.

    Args:
        scene_initializer: Instance from ``load_task``; ``None`` (no layout
            file for this task/scene) returns *event* unchanged.
        event: Current event (the original ``env.event``; may be ``None`` for
            the fake path).
        controller: Underlying ai2thor controller — preinit places objects via
            ``controller.step(action=..., **params)``.

    Returns:
        ``preinit``'s return value (by convention the latest event), or *event*
        unchanged when there is no initializer.
    """
    if scene_initializer is None:
        return event

    func, bound = _resolve_preinit(scene_initializer)
    try:
        if bound:
            return scene_initializer.preinit(event, controller)
        return func(event, controller)
    except Exception as exc:
        logger.exception(
            "SceneInitializer.preinit failed (task layout is wrong; fail-fast)"
        )
        raise RuntimeError(
            "SceneInitializer.preinit failed for "
            f"{type(scene_initializer).__module__}."
            f"{type(scene_initializer).__qualname__}: {exc!r}"
        ) from exc


__all__ = [
    "TaskContract",
    "invoke_scene_preinit",
    "load_task",
]
