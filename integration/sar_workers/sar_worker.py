"""SARWorker -- 每个 SAR 智能体的独立 A2A Worker 实现，不依赖 ROS 2。
中文说明：
    SARWorker 是每个 SAR 智能体的核心运行单元。它：
    - 通过 A2A（Agent-to-Agent）协议与 Coordinator 通信
    - 使用 mini-agent 后端驱动 LLM ReAct 循环
    - 通过共享的 SARBarrier 同步提交动作
    - 没有 ROS 2 依赖 —— 使用 _MockNode 模拟 ROS 2 Node 接口
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Callable

# -- Import path setup -------------------------------------------------------
_llamar_root = Path(__file__).resolve().parent.parent.parent
if str(_llamar_root) not in sys.path:
    sys.path.insert(0, str(_llamar_root))

_maros_a2a_lib = Path(os.environ.get("MAROS_A2A_LIB", "/home/wyh/daily_work/MARoS/maros_ws/a2a_lib"))
if str(_maros_a2a_lib) not in sys.path:
    sys.path.insert(0, str(_maros_a2a_lib))

from integration.sar_workers.tools import SAR_TOOLS
from integration.sar_workers.skills import SAR_SKILLS

logger = logging.getLogger(__name__)


class _MockNode:
    """ROS 2 Node 的轻量模拟（mock），用于保持与 MARoS transport.py 的接口兼容性。
    此模拟类避免了引入 rclpy（ROS 2 Python 客户端库）的依赖，使得 Worker
    可以在没有 ROS 2 环境的机器上运行。

    被 start_a2a_transport() 调用的接口:
      - node.get_logger().warning(...)  -> 重定向到标准 logging
      - node._get_system_prompt_with_history(prompt) -> 返回动态构建的 prompt

    Lightweight mock of ROS 2 Node for transport.py compatibility.

    start_a2a_transport() calls:
      - node.get_logger().warning(...)  -> redirect to logging
      - node._get_system_prompt_with_history(prompt) -> return dynamically built prompt

    No rclpy needed.
    """

    class _MockLogger:
        # 模拟 ROS 2 Node 的 Logger 接口，将日志重定向到 Python 标准 logging 模块
        # Mock logger that redirects all ROS 2 logger calls to Python's standard logging
        def warning(self, msg, *args, **kwargs):
            logger.warning(msg, *args, **kwargs)
        def info(self, msg, *args, **kwargs):
            logger.info(msg, *args, **kwargs)
        def error(self, msg, *args, **kwargs):
            logger.error(msg, *args, **kwargs)
        def __getattr__(self, name):
            return lambda *args, **kwargs: getattr(logger, name, logger.debug)(*args, **kwargs)

    def __init__(self, name: str = "sar_worker", prompt_builder: Optional[Callable[[], str]] = None):
        # 节点名称和日志模拟器
        self._name = name
        self._logger = self._MockLogger()
        # prompt_builder 是一个可调用对象，用于动态构建带有最新观测信息的系统提示
        self._prompt_builder = prompt_builder

    def get_logger(self):
        # 返回模拟的日志记录器，供 transport.py 使用
        return self._logger

    def _get_system_prompt_with_history(self, base_prompt: str) -> str:
        """返回动态构建的 prompt，注入当前环境观测信息。
        如果提供了 prompt_builder 可调用对象，则优先使用它而非静态 base_prompt。
        这使得 SARWorker 能在每次 A2A 任务执行时注入最新的观测数据。

        Return dynamically built prompt with current observations.

        If a prompt_builder callable was provided, it takes precedence over
        the static base_prompt. This allows SARWorker to inject fresh
        observations on every A2A task execution.
        """
        if self._prompt_builder is not None:
            return self._prompt_builder()
        return base_prompt


class SARWorker:
    """单个 SAR 智能体的独立 A2A Worker。

    每个 SARWorker 负责：
      - 持有共享的 SARBarrier 引用，通过其提交智能体动作（动作由 Barrier 同步执行）
      - 运行 A2A HTTP 服务器（基于 MARoS transport.py），与 Coordinator 通信
      - 使用 mini-agent 后端驱动 LLM ReAct 循环，配合 SAR 工具集
      - 每次执行 A2A 任务时动态重建 system prompt，注入最新环境观测和子任务信息

    不依赖 ROS 2 —— 使用 _MockNode 模拟 transport.py 所需的 Node 接口。

    Self-contained A2A Worker for one SAR agent.

    Each SARWorker:
      - Has a reference to the shared SARBarrier for action submission
      - Runs an A2A HTTP server (via MARoS transport.py) for Coordinator communication
      - Uses mini-agent for LLM ReAct loop with SAR tools
      - Dynamically rebuilds system prompt with latest observations

    No ROS 2 dependency -- mocks the Node interface for transport.py.
    """

    def __init__(
        self,
        agent_name: str,       # 智能体名称（如 "Alice"、"Bob"），用于标识和日志
        agent_idx: int,        # 智能体在环境中的索引（0-based），用于从 Barrier 获取对应观测
        barrier,               # 共享的 SARBarrier 实例，用于提交动作和执行步进
        port: int,             # Worker A2A HTTP 服务器的端口号
        coordinator_url: str = "ws://localhost:8080",  # Coordinator 的 WebSocket 地址
        model: str = "deepseek-v4-flash",              # Worker 使用的 LLM 模型名
    ):
        self.agent_name = agent_name
        self._agent_idx = agent_idx
        self._barrier = barrier
        self._port = port
        self._coordinator_url = coordinator_url
        self._model = model

        # 当前子任务（由 Coordinator 通过 A2A 协议设置）—— 必须在 _build_system_prompt()
        # 之前设置，因为子任务信息会被注入到系统提示词中
        # Current subtask (set by Coordinator via A2A) -- must be set before
        # _build_system_prompt() since it's injected into the prompt.
        self._current_subtask = "No subtask assigned yet."

        # 将 SAR 工具绑定到当前 Worker 实例，使得 @tool 装饰的函数能通过 'node' 参数
        # 访问到 self._barrier 和 self._agent_idx（用于提交动作）
        # Bind tools to self so @tool functions receive this worker as 'node'
        # (node._barrier and node._agent_idx work in tool bodies)
        self._tools = [t.bind(self) for t in SAR_TOOLS]

        # 构建初始系统提示词，包含默认观测和子任务信息
        # Build initial system prompt
        self._system_prompt = self._build_system_prompt()

        # 创建模拟的 ROS 2 Node 供 transport.py 使用，
        # 传入 prompt_builder 以便在每次 A2A 任务执行时动态重建 prompt
        # Create mock node for transport.py, wired to rebuild prompts dynamically
        self._mock_node = _MockNode(
            name=f"sar_{agent_name.lower()}",
            prompt_builder=self._build_system_prompt,
        )

        # 传输层服务器的句柄（由 start() 方法设置）
        self._server = None    # uvicorn Server 实例
        self._a2a_thread = None  # A2A HTTP 服务器后台线程
        self._ws_thread = None   # A2A WebSocket 客户端后台线程

    def _build_system_prompt(self) -> str:
        """构建系统提示词，注入当前环境观测和子任务信息。
        从 prompt.md 加载模板，替换 {agent_name} 占位符，从 Barrier 获取最新环境观测，
        并附加当前子任务描述。每次调用都会生成最新的提示词。
        Build system prompt with current observation injected."""
        base = _load_prompt_template()
        # Replace {agent_name} placeholder
        base = base.replace("{agent_name}", self.agent_name)
        try:
            obs = self._barrier.get_current_obs(self._agent_idx)
        except Exception:
            obs = "No observation available yet (environment not stepped)."
        subtask_info = f"\n\n## Current Subtask\n{self._current_subtask}"
        return base + "\n\n## Current Environment State\n" + obs + subtask_info

    def update_subtask(self, subtask: str):
        """更新当前子任务，由 Coordinator 通过 A2A 协议调用。
        设置子任务后会自动重建系统提示词，使 LLM 能感知到新的任务目标。
        Called by Coordinator via A2A to set the current subtask."""
        self._current_subtask = subtask
        # Rebuild system prompt to reflect updated subtask
        self._system_prompt = self._build_system_prompt()

    def start(self):
        """启动 A2A HTTP 服务器（非阻塞，在后台线程中运行）。

        启动流程：
        1. 调用 MARoS 的 start_a2a_transport()，传入模拟的 Node、Worker 标识、端口等信息
        2. 启动 A2A HTTP 服务器线程（接收 Coordinator 发来的子任务和指令）
        3. 启动 WebSocket 客户端线程（连接到 Coordinator，用于实时通信）
        4. 如果 a2a.server 不可用（未安装），则优雅降级 —— Worker 进入退化模式运行

        Start the A2A HTTP server (non-blocking, runs in background threads).

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
        """优雅关闭 Worker。
        先通知 HTTP 服务器停止接受新请求（设置 should_exit = True），
        然后等待 A2A 和 WebSocket 两个后台线程结束（最多等 5 秒）。
        Graceful shutdown."""
        if self._server is not None:
            self._server.should_exit = True
        for thread in (self._a2a_thread, self._ws_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)


# -- Prompt 模板 -------------------------------------------------------------
# Prompt template section: 系统提示词模板，用于构建 LLM 的初始指令

# prompt.md 文件的路径（与当前文件同目录），包含完整的系统提示模板
_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"

# 默认的系统提示模板（在 prompt.md 文件缺失时使用）
# {agent_name} 占位符会在运行时被替换为实际的智能体名称（如 "Alice"）
_DEFAULT_PROMPT = """You are {agent_name}, a search and rescue robot in a grid environment.
Your job is to help extinguish fires and rescue trapped persons.
Use your tools to navigate, collect supplies, fight fires, and carry people to safety."""


def _load_prompt_template() -> str:
    """从 prompt.md 文件加载系统提示模板。
    如果文件不存在，则回退使用 _DEFAULT_PROMPT 默认模板。
    Load the system prompt template from prompt.md."""
    try:
        return _PROMPT_PATH.read_text()
    except FileNotFoundError:
        logger.warning("prompt.md not found at %s, using default", _PROMPT_PATH)
        return _DEFAULT_PROMPT
