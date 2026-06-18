"""SARWorker -- self-contained A2A Worker per SAR agent. No ROS 2 dependency."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional, Callable

# -- Import path setup -------------------------------------------------------
_llamar_root = Path(__file__).resolve().parent.parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

_maros_a2a_lib = Path("/home/wyh/daily_work/MARoS/maros_ws/a2a_lib")
if str(_maros_a2a_lib) not in sys.path:
    sys.path.insert(0, str(_maros_a2a_lib))

from integration.sar_workers.tools import SAR_TOOLS
from integration.sar_workers.skills import SAR_SKILLS

logger = logging.getLogger(__name__)


class _MockNode:
    """Lightweight mock of ROS 2 Node for transport.py compatibility.

    start_a2a_transport() calls:
      - node.get_logger().warning(...)  -> redirect to logging
      - node._get_system_prompt_with_history(prompt) -> return dynamically built prompt

    No rclpy needed.
    """

    class _MockLogger:
        def warning(self, msg, *args, **kwargs):
            logger.warning(msg, *args, **kwargs)
        def info(self, msg, *args, **kwargs):
            logger.info(msg, *args, **kwargs)
        def error(self, msg, *args, **kwargs):
            logger.error(msg, *args, **kwargs)

    def __init__(self, name: str = "sar_worker", prompt_builder: Optional[Callable[[], str]] = None):
        self._name = name
        self._logger = self._MockLogger()
        self._prompt_builder = prompt_builder

    def get_logger(self):
        return self._logger

    def _get_system_prompt_with_history(self, base_prompt: str) -> str:
        """Return dynamically built prompt with current observations.

        If a prompt_builder callable was provided, it takes precedence over
        the static base_prompt. This allows SARWorker to inject fresh
        observations on every A2A task execution.
        """
        if self._prompt_builder is not None:
            return self._prompt_builder()
        return base_prompt


class SARWorker:
    """Self-contained A2A Worker for one SAR agent.

    Each SARWorker:
      - Has a reference to the shared SARBarrier for action submission
      - Runs an A2A HTTP server (via MARoS transport.py) for Coordinator communication
      - Uses mini-agent for LLM ReAct loop with SAR tools
      - Dynamically rebuilds system prompt with latest observations

    No ROS 2 dependency -- mocks the Node interface for transport.py.
    """

    def __init__(
        self,
        agent_name: str,
        agent_idx: int,
        barrier,               # SARBarrier
        port: int,
        coordinator_url: str = "ws://localhost:8080",
        model: str = "deepseek-v4-flash",
    ):
        self.agent_name = agent_name
        self._agent_idx = agent_idx
        self._barrier = barrier
        self._port = port
        self._coordinator_url = coordinator_url
        self._model = model

        # Current subtask (set by Coordinator via A2A) -- must be set before
        # _build_system_prompt() since it's injected into the prompt.
        self._current_subtask = "No subtask assigned yet."

        # Bind tools to self so @tool functions receive this worker as 'node'
        # (node._barrier and node._agent_idx work in tool bodies)
        self._tools = [t.bind(self) for t in SAR_TOOLS]

        # Build initial system prompt
        self._system_prompt = self._build_system_prompt()

        # Create mock node for transport.py, wired to rebuild prompts dynamically
        self._mock_node = _MockNode(
            name=f"sar_{agent_name.lower()}",
            prompt_builder=self._build_system_prompt,
        )

        # Transport server handles (set by start())
        self._server = None
        self._a2a_thread = None
        self._ws_thread = None

    def _build_system_prompt(self) -> str:
        """Build system prompt with current observation injected."""
        base = _load_prompt_template()
        # Replace {agent_name} placeholder
        base = base.replace("{agent_name}", self.agent_name)
        obs = self._barrier.get_current_obs(self._agent_idx)
        subtask_info = f"\n\n## Current Subtask\n{self._current_subtask}"
        return base + "\n\n## Current Environment State\n" + obs + subtask_info

    def update_subtask(self, subtask: str):
        """Called by Coordinator via A2A to set the current subtask."""
        self._current_subtask = subtask
        # Rebuild system prompt to reflect updated subtask
        self._system_prompt = self._build_system_prompt()

    def start(self):
        """Start the A2A HTTP server (non-blocking, runs in background threads).

        The transport may fail gracefully if a2a.server is not installed.
        """
        from a2a_lib.transport import start_a2a_transport

        self._server, self._a2a_thread, self._ws_thread = start_a2a_transport(
            node=self._mock_node,
            worker_id=self.agent_name,
            port=self._port,
            coordinator_url=self._coordinator_url,
            skills=SAR_SKILLS,
            backend="mini_agent",
            model=self._model,
            tools=self._tools,
            system_prompt=self._system_prompt,
            map_client_factory=None,  # No map_server in SAR
            task_logger=None,          # Optional: could wire TaskLogger later
        )

        if self._server is None:
            logger.warning(
                "A2A server not started (a2a.server unavailable). "
                "Worker %s running in degraded mode.",
                self.agent_name,
            )

    def stop(self):
        """Graceful shutdown."""
        if self._server is not None:
            self._server.should_exit = True


# -- Prompt template ---------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"
_DEFAULT_PROMPT = """You are a search and rescue robot in a grid environment.
Your job is to help extinguish fires and rescue trapped persons.
Use your tools to navigate, collect supplies, fight fires, and carry people to safety."""


def _load_prompt_template() -> str:
    """Load the system prompt template from prompt.md."""
    try:
        return _PROMPT_PATH.read_text()
    except FileNotFoundError:
        logger.warning("prompt.md not found at %s, using default", _PROMPT_PATH)
        return _DEFAULT_PROMPT
