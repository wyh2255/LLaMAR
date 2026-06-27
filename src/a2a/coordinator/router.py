"""Router Agent - LLM 驱动的智能路由（Mini-Agent 框架）。"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
from dataclasses import dataclass

import httpx
from a2a.client import create_client, ClientConfig, Client
from a2a.utils.constants import TransportProtocol
from a2a.client.errors import A2AClientError
from a2a.types.a2a_pb2 import (
    SendMessageRequest,
    Message,
    Part,
    StreamResponse,
    Role,
)

from a2a.coordinator.agent_registry import AgentRegistry
from a2a.builtin_tools.query_workers import QueryWorkersTool

from Agent.router_agent.agent import Agent
from Agent.router_agent.llm import LLMClient
from Agent.router_agent.schema import LLMProvider
from Agent.router_agent.tools.skill_loader import SkillLoader
from Agent.router_agent.tools.skill_tool import GetSkillTool

logger = logging.getLogger(__name__)


# 内置默认 prompt（DAG 分层规划格式）
# 注意：RouterAgent 只做规划不执行。它输出 DAG JSON 计划，由 CoordinatorAgentExecutor 负责执行和验证。
_DEFAULT_PROMPT = """You are a task routing agent for a multi-agent coordination system.
Your ONLY job is to output a layered DAG plan as JSON. Never answer the user directly.

Available tool:
- query_workers: Query the list of currently online workers and their capabilities

Workflow:
1. Use query_workers to understand what workers are available and their capabilities
2. Analyze the user's request — what needs to be done, in what order, what dependencies
3. Output a DAG plan: tasks grouped into layers, where tasks within a layer run in parallel,
   and each layer depends on results from prior layers

CRITICAL: You MUST output ONLY a JSON object. No natural language, no markdown, no explanation.
Even for simple questions, you MUST produce a DAG plan JSON.
Never answer the user directly — always delegate to workers via the plan.
{
  "layers": [
    {
      "layer_index": 0,
      "description": "First, independently gather data from multiple sources",
      "tasks": [
        {
          "task_id": "fetch-stock",
          "agent_id": "worker-id",
          "prompt": "Complete self-contained instruction for this worker",
          "depends_on": [],
          "description": "Fetch stock price data"
        }
      ]
    },
    {
      "layer_index": 1,
      "description": "Then synthesize results from layer 0",
      "tasks": [
        {
          "task_id": "synthesize",
          "agent_id": "worker-id",
          "prompt": "Using results from [fetch-stock], produce a summary report",
          "depends_on": ["fetch-stock"],
          "description": "Synthesize final report"
        }
      ]
    }
  ],
  "reasoning": "Explanation of the DAG structure and task assignments"
}

Rules:
- task_id must be unique within the plan, use descriptive kebab-case names
- Tasks in the same layer run in parallel (no dependencies between tasks in the same layer)
- A task can only depend on tasks from strictly earlier layers
- Reference dependency results using [task_id] notation so the executor can inject their outputs
- Each subtask prompt must be a complete, self-contained instruction
- Consider worker capabilities when choosing the target worker
- For simple single-worker tasks: output a single layer with one task
- If no workers are online, output {"layers": [], "reasoning": "No workers available"}
- Only output JSON, no other text
"""

_AGENTIC_PROMPT = """You dispatch tasks to workers. That's your only job.

When user says "让worker依次说hello" or "tell each worker to X":
Step 1: query_workers
Step 2: dispatch_task("say hello") to each worker
Step 3: collect_results
Step 4: return the results to user

Stop: do NOT introduce yourself, ask questions, explore workspace, or query old results.
Just: query → dispatch → collect → return.
"""


@dataclass
class SubTask:
    """LLM 拆解出的子任务."""

    agent_id: str
    prompt: str
    task_id: str = ""


@dataclass
class RouteResult:
    """路由决策结果（扁平格式，保持向后兼容）."""

    subtasks: list[SubTask]
    execution_order: str  # "sequential" | "parallel" | "single"
    reasoning: str


# ============================================================
# DAG (Directed Acyclic Graph) 数据模型
# ============================================================


@dataclass
class DAGTask:
    """DAG 中的一个任务节点。"""

    task_id: str  # plan 内唯一标识，如 "fetch-stock"
    agent_id: str  # 目标 Worker
    prompt: str  # 自包含指令，可含 [dep_task_id] 引用
    depends_on: list[str]  # 依赖的 task_id 列表（来自前层）
    description: str = ""  # 人类可读描述，用于调试/推理


@dataclass
class DAGLayer:
    """DAG 中的一层（层内任务并行，层间串行）。"""

    layer_index: int
    tasks: list[DAGTask]
    description: str = ""


@dataclass
class DAGPlan:
    """分层 DAG 执行计划。"""

    layers: list[DAGLayer]
    reasoning: str

    @property
    def total_tasks(self) -> int:
        return sum(len(layer.tasks) for layer in self.layers)

    @property
    def is_empty(self) -> bool:
        return len(self.layers) == 0

    @classmethod
    def from_route_result(cls, rr: RouteResult) -> "DAGPlan":
        """将扁平 RouteResult 转换为 DAGPlan（向后兼容）."""
        if not rr.subtasks:
            return cls(layers=[], reasoning=rr.reasoning)

        if rr.execution_order in ("single", "parallel"):
            # 所有子任务在一层内并行
            tasks = [
                DAGTask(
                    task_id=f"task-{i}",
                    agent_id=s.agent_id,
                    prompt=s.prompt,
                    depends_on=[],
                )
                for i, s in enumerate(rr.subtasks)
            ]
            layers = [
                DAGLayer(
                    layer_index=0,
                    tasks=tasks,
                    description="Flat plan (single/parallel)",
                )
            ]
        elif rr.execution_order == "sequential":
            # 每个子任务独占一层，隐式依赖前一层
            layers = []
            for i, s in enumerate(rr.subtasks):
                deps = [f"task-{i - 1}"] if i > 0 else []
                task = DAGTask(
                    task_id=f"task-{i}",
                    agent_id=s.agent_id,
                    prompt=s.prompt,
                    depends_on=deps,
                )
                layers.append(
                    DAGLayer(layer_index=i, tasks=[task], description=f"Step {i + 1}")
                )
        else:
            raise ValueError(f"Unknown execution_order: {rr.execution_order}")

        return cls(layers=layers, reasoning=rr.reasoning)

    def to_route_result(self) -> RouteResult:
        """将 DAGPlan 转换为扁平 RouteResult（仅单层无依赖时无损）。"""
        if not self.layers:
            return RouteResult(
                subtasks=[], execution_order="single", reasoning=self.reasoning
            )

        if len(self.layers) == 1:
            layer = self.layers[0]
            subtasks = [
                SubTask(agent_id=t.agent_id, prompt=t.prompt, task_id=t.task_id)
                for t in layer.tasks
            ]
            execution_order = "parallel" if len(subtasks) > 1 else "single"
        else:
            # 多层 DAG：摊平所有任务
            subtasks = []
            for layer in self.layers:
                for t in layer.tasks:
                    subtasks.append(
                        SubTask(agent_id=t.agent_id, prompt=t.prompt, task_id=t.task_id)
                    )
            execution_order = "sequential"

        return RouteResult(
            subtasks=subtasks, execution_order=execution_order, reasoning=self.reasoning
        )


class RouterAgent:
    """
    Coordinator 的路由 Agent —— 基于 Mini-Agent 框架，通过 Tool Calling 进行智能路由。

    每次 route() 调用创建独立 Agent 实例，确保并发安全。
    所有依赖通过构造函数显式注入。
    """

    def __init__(
        self,
        registry: AgentRegistry,
        prompts_dir: Path | None = None,
        custom_tools_dir: Path | None = None,
        extra_tools: list | None = None,
        skills_dir: Path | None = None,
        model: str = "claude-opus-4-5",
        max_steps: int = 15,
        temperature: float = 0.7,
        provider: str = "anthropic",
        api_base: str = "https://api.anthropic.com",
        api_key_env: str = "ANTHROPIC_API_KEY",
        log_dir: Path | None = None,
    ) -> None:
        self._registry = registry
        self._prompts_dir = prompts_dir
        self._custom_tools_dir = custom_tools_dir
        self._extra_tools = extra_tools or []
        self._skills_dir = skills_dir
        self._model = model
        self._max_steps = max_steps
        self._temperature = temperature
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._log_dir = log_dir

        # 加载 prompt 和自定义 tools
        self._system_prompt = self._load_prompt()
        self._custom_tools = self._load_custom_tools()

        # SDK Client 缓存（在 RouterAgent 生命周期内共享）
        self._httpx_client: httpx.AsyncClient | None = None
        self._sdk_clients: dict[str, Client] = {}

        # workspace dir for Agent
        self._workspace_dir = Path("./workspace/coordinator")
        self._workspace_dir.mkdir(parents=True, exist_ok=True)

    def _load_prompt(self) -> str:
        """加载外部 system.md，未指定则使用内置默认。"""
        if self._prompts_dir is None:
            return _DEFAULT_PROMPT
        prompt_file = self._prompts_dir / "system.md"
        if not prompt_file.exists():
            raise FileNotFoundError(
                f"Prompt file not found: {prompt_file}. "
                f"Either create the file or omit --prompts-dir to use the built-in default."
            )
        return prompt_file.read_text(encoding="utf-8")

    @property
    def agentic_prompt(self) -> str:
        """Agentic 模式的 system prompt。
        当 --prompts-dir 指定时使用外部 system.md，否则使用内置默认。
        """
        if self._prompts_dir is not None:
            return self._system_prompt
        return _AGENTIC_PROMPT

    def _discover_skills_dir(self) -> Path | None:
        """尝试从约定路径发现 skills 目录。"""
        for candidate in [
            Path.cwd() / "skills" / "coordinator",
            Path(__file__).parent.parent / "skills" / "coordinator",
        ]:
            if candidate.is_dir():
                return candidate
        return None

    def _load_custom_tools(self) -> list:
        """从 custom_tools_dir 加载自定义 Tool 模块。加载失败即报错。"""
        if self._custom_tools_dir is None or not self._custom_tools_dir.is_dir():
            return []
        tools = []
        for py_file in sorted(self._custom_tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"_a2a_coordinator_tool_{py_file.stem}", str(py_file)
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "tool"):
                    tools.append(module.tool)
                    logger.info("Loaded custom coordinator tool: %s", py_file.name)
                else:
                    raise ImportError(
                        f"Custom tool module '{py_file.name}' does not expose a 'tool' attribute"
                    )
            except Exception as e:
                raise ImportError(
                    f"Failed to load custom tool '{py_file.name}': {e}"
                ) from e
        return tools

    # ============================================================
    # 路由方法
    # ============================================================

    async def route(self, user_request: str) -> RouteResult:
        """
        分析用户请求并输出扁平路由决策（向后兼容）。

        内部委托给 route_dag()，然后将 DAGPlan 转换为 RouteResult。
        当 DAGPlan 只有一层时转换无损；多层时摊平为 sequential。
        """
        dag_plan = await self.route_dag(user_request)
        return dag_plan.to_route_result()

    async def route_dag(self, user_request: str) -> DAGPlan:
        """
        分析用户请求并输出分层 DAG 执行计划。

        每次调用创建独立 Agent 实例，通过 Mini-Agent 的 Tool Calling 进行：
        1. query_workers → 了解可用 Worker
        2. Agent 推理输出 DAG JSON 计划
        """
        agent = self._build_agent()
        agent.add_user_message(user_request)
        final_text = await agent.run()
        return self._parse_dag_result(final_text)

    async def plan_next(
        self,
        original_request: str,
        completed_results: dict[str, str],
        verification_reports: list["VerificationReport"] | None = None,
        remaining_layers: list["DAGLayer"] | None = None,
    ) -> DAGPlan:
        """
        基于已完成工作的结果进行重规划（闭环反馈）。

        每层 DAG 执行完毕后调用此方法，让 Router 根据实际结果
        决定：继续执行下一层、修改剩余计划、或停止。

        Args:
            original_request: 原始用户请求
            completed_results: task_id → result_text 的映射
            verification_reports: 可选的验证报告列表
            remaining_layers: 原始计划中尚未执行的层

        Returns:
            DAGPlan — 返回空 layers 表示"任务完成，无需继续"
        """
        from a2a.builtin_tools.query_task_results import QueryTaskResultsTool

        # 构建已完成结果的摘要文本
        results_summary = self._format_completed_results(
            completed_results, verification_reports
        )

        replan_prompt = f"""## Re-planning Round

Below are the results of previously executed DAG tasks. Review them and decide:
- If all tasks succeeded and the original request is fulfilled: return an empty plan.
- If some tasks failed or need re-doing: update the remaining plan accordingly.
- If the original plan is no longer valid: produce an entirely new plan.

### Original User Request
{original_request}

### Completed Task Results
{results_summary}

### Remaining Layers (from original plan)
{self._format_remaining_layers(remaining_layers)}

Output a DAG plan (same JSON format as before). If no more work is needed, output:
{{"layers": [], "reasoning": "All tasks completed successfully."}}"""

        agent = self._build_agent(extra_tools=[QueryTaskResultsTool(completed_results)])
        agent.add_user_message(replan_prompt)
        final_text = await agent.run()
        return self._parse_dag_result(final_text)

    def _format_completed_results(
        self,
        results: dict[str, str],
        reports: list | None,
    ) -> str:
        """将已完成结果和可选的验证报告格式化为文本。"""
        lines = []
        for task_id, text in results.items():
            truncated = text[:1500] + ("...[truncated]" if len(text) > 1500 else "")
            lines.append(f"### {task_id}\n{truncated}")
        if reports:
            lines.append("\n### Verification Reports")
            for r in reports:
                lines.append(
                    f"- {r.task_id}: {'PASS' if r.passed else 'FAIL'} — {r.summary}"
                )
        return "\n\n".join(lines) if lines else "(no completed results)"

    @staticmethod
    def _format_remaining_layers(layers: list | None) -> str:
        """将剩余层格式化为文本。"""
        if not layers:
            return "(no remaining layers — this is the initial plan)"
        lines = []
        for layer in layers:
            task_ids = [t.task_id for t in layer.tasks]
            lines.append(f"- Layer {layer.layer_index}: {task_ids}")
        return "\n".join(lines)

    # ============================================================
    # Agent 构建
    # ============================================================

    def _build_agent(
        self,
        extra_tools: list | None = None,
        system_prompt_override: str | None = None,
    ) -> Agent:
        """构建新的 Agent 实例。每次调用创建新实例（不复用）。"""
        provider_map = {
            "anthropic": LLMProvider.ANTHROPIC,
            "openai": LLMProvider.OPENAI,
        }
        provider = provider_map.get(self._provider, LLMProvider.ANTHROPIC)

        api_key = os.environ.get(self._api_key_env, "")
        llm_client = LLMClient(
            api_key=api_key,
            provider=provider,
            api_base=self._api_base,
            model=self._model,
        )

        # 内置 tools（仅查询类）+ 自定义 tools + 额外 tools
        # 注意：RouterAgent 不注入 AssignTaskTool！
        # route() 只负责规划（查询 Worker → 输出 JSON 计划），
        # 实际任务派发由 CoordinatorAgentExecutor.execute() 调用 push_task() 完成。
        tools: list = [
            QueryWorkersTool(self._registry),
        ]
        tools.extend(self._custom_tools)
        tools.extend(self._extra_tools)
        if extra_tools:
            tools.extend(extra_tools)

        # 构建 system prompt（可能附加 skill metadata）
        system_prompt = system_prompt_override or self._system_prompt
        skills_dir = self._skills_dir or self._discover_skills_dir()
        if skills_dir:
            try:
                skill_loader = SkillLoader(skills_dir=str(skills_dir))
                skill_loader.discover_skills()
                metadata_prompt = skill_loader.get_skills_metadata_prompt()
                if metadata_prompt:
                    system_prompt = system_prompt + "\n\n" + metadata_prompt
                tools.append(GetSkillTool(skill_loader))
            except Exception as e:
                logger.warning("Failed to load skills from %s: %s", skills_dir, e)

        return Agent(
            llm_client=llm_client,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=self._max_steps,
            workspace_dir=str(self._workspace_dir),
            log_dir=self._log_dir,
        )

    # ============================================================
    # 解析方法
    # ============================================================

    def _parse_dag_result(self, response: str) -> DAGPlan:
        """解析 Agent 的 DAG JSON 输出。

        优先尝试解析 DAG 格式 (layers)，失败时回退到扁平格式 (subtasks)。
        如果 LLM 返回了非 JSON 文本（如直接回答问题），则将其包装为
        一个不含实际子任务的空 DAGPlan（由上游 _execute_dag_loop 处理）。
        """
        cleaned = response.strip()
        cleaned = self._strip_markdown_fence(cleaned)
        try:
            data = json.loads(cleaned)

            # 优先解析 DAG 格式
            if "layers" in data:
                layers = []
                for layer_data in data["layers"]:
                    tasks = []
                    for t in layer_data.get("tasks", []):
                        tasks.append(
                            DAGTask(
                                task_id=t.get("task_id", f"task-{len(tasks)}"),
                                agent_id=t.get("agent_id", ""),
                                prompt=t.get("prompt", ""),
                                depends_on=t.get("depends_on", []),
                                description=t.get("description", ""),
                            )
                        )
                    layers.append(
                        DAGLayer(
                            layer_index=layer_data.get("layer_index", len(layers)),
                            tasks=tasks,
                            description=layer_data.get("description", ""),
                        )
                    )
                return DAGPlan(layers=layers, reasoning=data.get("reasoning", ""))

            # 回退到扁平格式（向后兼容旧 prompt）
            return DAGPlan.from_route_result(self._parse_route_result(response))
        except json.JSONDecodeError:
            # LLM 返回了非 JSON 文本（如直接回答问题）
            # 此时将 LLM 的回答原样返回作为最终结果，由上游 _finish() 处理
            logger.warning(
                "LLM returned non-JSON response (model may not follow JSON instruction). "
                "Response preview: %s...",
                response[:200],
            )
            # 返回空 DAGPlan — 上游 _execute_dag_loop 会将此视为
            # 「无可执行任务，直接将 LLM 回答作为结果返回」
            return DAGPlan(layers=[], reasoning=response[:2000])

    def _parse_route_result(self, response: str) -> RouteResult:
        """解析 Agent 的扁平 RouteResult（向后兼容）。

        解析旧格式的 JSON 输出 (subtasks + execution_order)。
        新代码应使用 route_dag() + _parse_dag_result()。
        """
        cleaned = response.strip()
        cleaned = self._strip_markdown_fence(cleaned)
        try:
            data = json.loads(cleaned)
            subtasks_data = data.get("subtasks", [])
            subtasks = [
                SubTask(
                    agent_id=s["agent_id"],
                    prompt=s["prompt"],
                    task_id=s.get("task_id", ""),
                )
                for s in subtasks_data
            ]
            if not subtasks:
                for agent_id in data.get("target_agents", []):
                    subtasks.append(SubTask(agent_id=agent_id, prompt="", task_id=""))
            return RouteResult(
                subtasks=subtasks,
                execution_order=data.get("execution_order", "single"),
                reasoning=data.get("reasoning", ""),
            )
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM response as JSON: {response[:200]}...")
            raise ValueError(
                f"Invalid RouterAgent response (expected JSON): {response[:200]}..."
            )

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        """去除 markdown 代码围栏 (```json ... ```)."""
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            return "\n".join(lines)
        return text

    async def _get_httpx_client(self) -> httpx.AsyncClient:
        """获取或创建共享的 httpx 客户端。"""
        if self._httpx_client is None or self._httpx_client.is_closed:
            self._httpx_client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=30.0)
            )
        return self._httpx_client

    async def _get_sdk_client(self, agent_id: str, endpoint: str) -> Client:
        """获取或创建 Worker 的 SDK Client（缓存复用）。"""
        if agent_id not in self._sdk_clients:
            httpx_client = await self._get_httpx_client()
            config = ClientConfig(
                streaming=True,
                httpx_client=httpx_client,
                supported_protocol_bindings=[
                    TransportProtocol.JSONRPC,
                    TransportProtocol.HTTP_JSON,
                ],
            )
            self._sdk_clients[agent_id] = await create_client(endpoint, config)
        return self._sdk_clients[agent_id]

    async def push_task(self, agent_id: str, prompt: str) -> list[StreamResponse]:
        """推送子任务到 Worker A2A 端点，收集流式 StreamResponse。"""
        agent_info = self._registry.get(agent_id)

        request = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                parts=[Part(text=prompt)],
            ),
        )

        events: list[StreamResponse] = []
        client = await self._get_sdk_client(agent_id, agent_info.endpoint)
        try:
            async for stream_response in client.send_message(request):
                events.append(stream_response)
        except A2AClientError as e:
            logger.error(f"Failed to push task to {agent_id}: {e}")
            raise

        return events

    async def close(self) -> None:
        """关闭所有 SDK 客户端和共享的 httpx 客户端。"""
        for client in self._sdk_clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._sdk_clients.clear()
        if self._httpx_client is not None and not self._httpx_client.is_closed:
            await self._httpx_client.aclose()
        self._httpx_client = None
