"""SARWorker -- 每个 SAR 智能体的独立 Worker，不依赖 MARoS。
中文说明：
    SARWorker 是每个 SAR 智能体的核心运行单元。它：
    - 通过自写 ReAct agent + A2A server 与 Coordinator 通信
    - 使用 SimpleLLMClient 驱动 LLM ReAct 循环
    - 通过共享的 SARBarrier 同步提交动作
    - 不依赖 mini_agent / a2a_lib / transport.py / ROS 2
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from integration.sar_workers.tools import SAR_TOOLS
from integration.sar_workers.skills import SAR_SKILLS

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = """You are {agent_name}, a search and rescue robot.
Work with other robots to extinguish all fires and rescue all trapped persons.
"""


class SARWorker:
    """单个 SAR 智能体的独立 Worker。

    使用自写 ReAct agent + A2A server，不依赖 mini_agent / a2a_lib / transport.py。
    """

    def __init__(
        self,
        agent_name: str,
        agent_idx: int,
        barrier,
        port: int,
        coordinator_url: str = "http://localhost:8080",
        model: str = "deepseek-v4-flash",
    ):
        self.agent_name = agent_name
        self._agent_idx = agent_idx
        self._barrier = barrier
        self._port = port
        # 使用 urlparse 正确提取 host:port，兼容 ws:// 和 http:// 前缀
        normalized_url = coordinator_url.replace("ws://", "http://")
        parsed = urlparse(normalized_url)
        self._coordinator_host = parsed.hostname or "localhost"
        self._coordinator_port = parsed.port or 8080
        self._model = model

        self._current_subtask = "No subtask assigned yet."
        self._tools = [t.bind(self) for t in SAR_TOOLS]
        from integration.coordinator.llm_shim import SimpleLLMClient
        self._llm_client = SimpleLLMClient(model=self._model)

        self._react_agent = None
        self._a2a_server = None

    def _build_system_prompt(self) -> str:
        """构建系统提示词，注入当前环境观测和子任务信息。
        从 prompt.md 加载模板，替换 {agent_name} 占位符，从 Barrier 获取最新环境观测，
        并附加当前子任务描述。每次调用都会生成最新的提示词。"""
        base = _load_prompt_template()
        base = base.replace("{agent_name}", self.agent_name)
        try:
            obs = self._barrier.get_current_obs(self._agent_idx)
        except Exception:
            obs = "No observation available yet."
        subtask_info = f"\n\n## Current Subtask\n{self._current_subtask}"
        return base + "\n\n## Current Environment State\n" + obs + subtask_info

    def update_subtask(self, subtask: str):
        """更新当前子任务，由 Coordinator 通过 A2A 协议调用。
        设置子任务后会自动更新 ReAct agent 的系统提示词。"""
        self._current_subtask = subtask
        if self._react_agent:
            self._react_agent.update_system_prompt(self._build_system_prompt())

    def start(self):
        """启动 ReAct agent + A2A server（非阻塞，在后台线程中运行）。"""
        from integration.sar_workers.react_agent import WorkerReActAgent
        from integration.sar_workers.a2a_server import A2AWorkerServer

        self._react_agent = WorkerReActAgent(
            llm_client=self._llm_client,
            tools=self._tools,
            system_prompt=self._build_system_prompt(),
        )
        self._a2a_server = A2AWorkerServer(
            agent_name=self.agent_name,
            port=self._port,
            coordinator_host=self._coordinator_host,
            coordinator_port=self._coordinator_port,
            react_agent=self._react_agent,
            worker=self,
            skills=SAR_SKILLS,
            model=self._model,
        )
        self._a2a_server.start_in_thread()
        logger.info(f"[{self.agent_name}] Worker started on port {self._port}")

    def stop(self):
        """停止 Worker -- 清理 A2A server 资源。"""
        if self._a2a_server:
            self._a2a_server.stop()
        logger.info(f"[{self.agent_name}] Worker stopped")


# -- Prompt 模板 -------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"


def _load_prompt_template() -> str:
    """从 prompt.md 文件加载系统提示模板。"""
    try:
        return _PROMPT_PATH.read_text()
    except FileNotFoundError:
        logger.warning("prompt.md not found at %s, using default", _PROMPT_PATH)
        return _DEFAULT_PROMPT
