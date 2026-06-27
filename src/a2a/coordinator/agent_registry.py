"""混合注册表 - 支持静态配置 + 动态注册。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import yaml




class AgentStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


@dataclass
class AgentInfo:
    """Agent 注册信息。"""
    agent_id: str
    description: str
    endpoint: str  # A2A HTTP endpoint
    capabilities: list[str] = field(default_factory=list)
    backend: str = "openharness"       # 新增: "openharness" | "mini_agent"
    model: str = "claude-opus-4-5"     # 新增: 可选的模型覆写
    status: AgentStatus = AgentStatus.OFFLINE
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)

    @property
    def id(self) -> str:
        """兼容性别名，与 WorkerNode.worker_id 对齐。"""
        return self.agent_id


class AgentNotFoundError(Exception):
    pass


class AgentRegistry:
    """
    混合注册表：静态配置 + 动态注册。

    静态配置在启动时加载，动态注册允许运行时增删 Agent。
    同名 ID 的动态注册覆盖静态配置。
    """

    def __init__(self, static_config_path: Path | None = None) -> None:
        self._agents: dict[str, AgentInfo] = {}
        if static_config_path:
            self._load_static_config(static_config_path)

    def _load_static_config(self, config_path: Path) -> None:
        """从 YAML 文件加载静态配置。"""
        if not config_path.exists():
            return
        with open(config_path) as f:
            data = yaml.safe_load(f)
        for agent_data in data.get("agents", []):
            agent = AgentInfo(
                agent_id=agent_data["id"],
                description=agent_data.get("description", ""),
                endpoint=agent_data["endpoint"],
                capabilities=agent_data.get("capabilities", []),
                backend=agent_data.get("backend", "openharness"),
                model=agent_data.get("model", "claude-opus-4-5"),
            )
            self._agents[agent.agent_id] = agent

    def register(self, agent: AgentInfo) -> None:
        """动态注册或更新 Agent（覆盖静态配置）。"""
        agent.status = AgentStatus.ONLINE
        agent.last_heartbeat = datetime.utcnow()
        self._agents[agent.agent_id] = agent

    def unregister(self, agent_id: str) -> None:
        """注销 Agent。"""
        self._agents.pop(agent_id, None)

    def get(self, agent_id: str) -> AgentInfo:
        """根据 ID 获取 Agent。"""
        if agent_id not in self._agents:
            raise AgentNotFoundError(agent_id)
        return self._agents[agent_id]

    def list_online(self) -> list[AgentInfo]:
        """列出所有在线 Agent。"""
        return [a for a in self._agents.values() if a.status == AgentStatus.ONLINE]

    def update_heartbeat(self, agent_id: str) -> None:
        """更新心跳时间。"""
        if agent_id in self._agents:
            self._agents[agent_id].last_heartbeat = datetime.utcnow()
            self._agents[agent_id].status = AgentStatus.ONLINE

    def mark_busy(self, agent_id: str) -> None:
        """标记为忙碌。"""
        if agent_id in self._agents:
            self._agents[agent_id].status = AgentStatus.BUSY

    def mark_idle(self, agent_id: str) -> None:
        """标记为空闲。"""
        if agent_id in self._agents:
            self._agents[agent_id].status = AgentStatus.ONLINE

    def register_from_agent_card(
        self, worker_id: str, endpoint: str, agent_card: dict
    ) -> AgentInfo:
        """从 Worker 的 AgentCard JSON 解析并注册 Agent。

        Args:
            worker_id: WS 注册用的 worker_id
            endpoint: Worker A2A endpoint
            agent_card: AgentCard JSON (来自 /.well-known/agent-card.json)

        Returns:
            注册的 AgentInfo
        """
        skills = agent_card.get("skills", [])

        capabilities: list[str] = []
        backend = "openharness"
        model = "claude-opus-4-5"

        for skill in skills:
            tags = skill.get("tags", [])
            skill_id = skill.get("id", "")

            if "backend" in tags:
                for tag in tags:
                    if tag not in ("metadata", "backend", "model"):
                        backend = tag
            elif "model" in tags:
                for tag in tags:
                    if tag not in ("metadata", "backend", "model"):
                        model = tag
            elif skill_id not in ("backend", "model"):
                capabilities.append(skill_id)

        description = agent_card.get("description", "")
        agent = AgentInfo(
            agent_id=worker_id,
            description=description,
            endpoint=endpoint,
            capabilities=capabilities,
            backend=backend,
            model=model,
            status=AgentStatus.ONLINE,
        )
        self._agents[agent.agent_id] = agent
        return agent

    def update_heartbeat_from_worker(self, worker_id: str) -> None:
        """从 WebSocket 心跳更新 Agent 状态。"""
        if worker_id in self._agents:
            self._agents[worker_id].last_heartbeat = datetime.utcnow()
            self._agents[worker_id].status = AgentStatus.ONLINE

    def unregister_worker(self, worker_id: str) -> None:
        """Worker 断开时标记为离线（不删除，保留静态配置的 Agent）。"""
        if worker_id in self._agents:
            self._agents[worker_id].status = AgentStatus.OFFLINE

    def get_all_agents_prompt_text(self) -> str:
        """生成所有 Agent 的描述文本，用于 LLM Prompt。"""
        lines = []
        for agent in self._agents.values():
            caps = ", ".join(agent.capabilities) if agent.capabilities else "无"
            lines.append(f"- {agent.agent_id}: {agent.description} (能力: {caps})")
        return "\n".join(lines) if lines else "无可用 Agent"
