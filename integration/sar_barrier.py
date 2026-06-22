"""SARBarrier — synchronous action collector wrapping LLaMAR SAREnv.
SARBarrier —— 封装 LLaMAR SAREnv 的同步动作收集器。

该模块实现了多智能体同步屏障：收集所有智能体的动作后统一执行 env.step()，
再将观测结果广播回各智能体。这是 MARoS 与 LLaMAR 集成的核心同步机制。
"""
import asyncio
from pathlib import Path

# 需要将 SAR/ 加入 sys.path，因为 SAR 目录使用扁平导入
# （如 "from env import SAREnv", "import core"），不是正规 Python 包
# SAR/ must be on sys.path because it uses flat imports (not a proper package)
_sar_dir = Path(__file__).resolve().parent.parent / "SAR"
import sys
if str(_sar_dir) not in sys.path:
    sys.path.insert(0, str(_sar_dir))

from env import SAREnv


class SARBarrier:
    """Collect per-agent actions, execute env.step() synchronously, broadcast observations.
    收集各智能体的动作，同步执行 env.step()，然后广播观测结果。

    Wraps LLaMAR's SAREnv with an async barrier:
    封装 LLaMAR 的 SAREnv，实现异步屏障：
      1. Workers call submit_action(agent_idx, action) -> await
         工作节点调用 submit_action(agent_idx, action) 并等待
      2. When all N agents have submitted -> _execute_step()
         当所有 N 个智能体都已提交时 -> 执行 _execute_step()
      3. Observations distributed -> awaiting Workers resume
         分发观测结果 -> 等待的工作节点恢复执行

    Timeout: if an agent hasn't submitted within 30s of the first submission,
    NoOp is auto-filled and the step proceeds.
    超时：如果某个智能体在首次提交后 30 秒内未提交，
    则自动填充 NoOp 并继续执行步骤。
    """

    STEP_TIMEOUT = 30.0  # seconds to wait for all agents before auto-NoOp
                       # 等待所有智能体的超时时间（秒），超时后自动填入 NoOp

    def __init__(self, num_agents: int, scene: int = 1, seed: int = 42):
        """Initialize the SAR barrier environment.
        初始化 SAR 屏障环境。

        Args:
            num_agents: Number of agents in the simulation (1-6)
                       模拟中的智能体数量（1-6）
            scene: Scene number to load (default 1)
                   要加载的场景编号（默认为 1）
            seed: Random seed for reproducibility (default 42)
                  随机种子，用于结果可复现（默认为 42）
        """
        if not (1 <= num_agents <= 6):
            raise ValueError(f"num_agents must be 1-6, got {num_agents}")

        self.num_agents = num_agents
        self.scene = scene
        self.seed = seed

        # Create and reset the SAR environment
        # 创建并重置 SAR 环境
        self.env = SAREnv(num_agents=num_agents, scene=scene, seed=seed)
        self.env.reset()

        # Per-step state
        # 每步状态
        self._step_counter: int = 0          # 当前步数计数
        self._action_queue: dict[int, str] = {}  # 当前步骤的动作队列
        self._current_obs: dict[int, str] = {}   # 当前观测结果
        self._finished: bool = False             # 任务是否完成

        # Logging state -- populated by worker tools, flushed each step
        # 日志状态 -- 由 Worker 工具函数填充，每步执行后清空并记录
        self._logger_ref = None                        # IntegrationLogger 实例引用
        self._pending_agent_logs: list[dict] = []      # 当前步积累的 agent 交互日志
        self._last_step_log: dict = {                  # 最近一步的日志快照
            "actions": [],
            "successes": [],
            "observations": [],
            "subtasks": [],
        }

        # Async synchronization -- re-created each step
        # 异步同步机制 —— 每一步都会重新创建
        self._obs_events: list[asyncio.Event] = [
            asyncio.Event() for _ in range(num_agents)
        ]
        self._step_lock = asyncio.Lock()  # 保护动作队列的异步锁

    # -- Public API ---------------------------------------------------------
    # -- 公开 API -----------------------------------------------------------

    async def submit_action(self, agent_idx: int, action: str) -> dict:
        """Submit this agent's action and wait for all agents to submit.
        提交当前智能体的动作并等待所有智能体提交。

        Returns a dict with the agent's observation and step metadata.
        返回包含智能体观测结果和步骤元数据的字典。

        Args:
            agent_idx: Index of the agent submitting (0-based)
                       提交动作的智能体索引（从 0 开始）
            action: Action string (e.g. "NavigateTo(target_id)")
                    动作字符串（如 "NavigateTo(target_id)"）

        Returns:
            dict with keys: observation, agent_name, step, finished, success
            包含 observation, agent_name, step, finished, success 键的字典
        """
        if not (0 <= agent_idx < self.num_agents):
            raise ValueError(
                f"agent_idx {agent_idx} out of range [0, {self.num_agents})"
            )

        # Register this agent's action
        # 注册当前智能体的动作
        async with self._step_lock:
            self._action_queue[agent_idx] = action
            all_submitted = len(self._action_queue) == self.num_agents
            # 检查是否所有智能体都已提交动作

        # If we're the last agent, execute the step (sets all events)
        # 如果是最后一个提交的智能体，则执行步骤（会设置所有事件）
        if all_submitted:
            await self._execute_step()
        else:
            # Wait for our specific event (set by _execute_step when all agents submit)
            # 等待当前智能体对应的事件（当所有智能体提交后由 _execute_step 设置）
            # With timeout: if other agents never submit, auto-fill with NoOp
            # 带有超时机制：如果其他智能体未提交，自动填充 NoOp
            try:
                await asyncio.wait_for(
                    self._obs_events[agent_idx].wait(),
                    timeout=self.STEP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # Timeout: fill missing agents with NoOp and proceed
                # 超时：为未提交的智能体填充 NoOp，然后继续执行
                async with self._step_lock:
                    for i in range(self.num_agents):
                        if i not in self._action_queue:
                            self._action_queue[i] = "NoOp"
                await self._execute_step()

        # Return this agent's observation
        # 返回当前智能体的观测结果
        obs_text = self._current_obs.get(agent_idx, "")
        return {
            "observation": obs_text,
            "agent_name": self.env.agent_names[agent_idx],
            "step": self._step_counter,
            "finished": self._finished,
            "success": True,
        }

    def get_current_obs(self, agent_idx: int) -> str:
        """Return the latest formatted observation for prompt injection.
        返回最新的格式化观测结果，用于提示词注入。

        Args:
            agent_idx: Index of the agent
                       智能体索引

        Returns:
            Formatted observation string
            格式化后的观测字符串
        """
        return self._current_obs.get(agent_idx, "No observation yet.")

    def is_finished(self) -> bool:
        """Check if the task is complete.
        检查任务是否完成。

        Returns:
            True if the task is finished, False otherwise
            如果任务完成则返回 True，否则返回 False
        """
        return self._finished

    def get_metrics(self) -> dict:
        """Return current task metrics.
        返回当前任务指标。

        Returns:
            dict with coverage, transport_rate, steps, finished
            包含 coverage, transport_rate, steps, finished 的字典
        """
        return {
            "coverage": self.env.checker.get_coverage(),
            "transport_rate": self.env.checker.get_transport_rate(),
            "steps": self._step_counter,
            "finished": self._finished,
        }

    def get_env_snapshot(self) -> dict:
        """Return current environment state for Coordinator's query_sar_state tool.
        返回当前环境状态，供 Coordinator 的 query_sar_state 工具使用。

        Includes all visible objects with positions, intensities, types, and agent states.
        包含所有可见物体的位置、强度、类型以及智能体状态。

        Returns:
            dict categorized by object type (agents, fires, persons, etc.)
            按物体类型分类的字典（agents, fires, persons 等）
        """
        # 获取所有物体（展开装饰器包装，不使用记忆）
        all_objects = self.env.controller.field.all_objects(
            expand=True, with_memory=False
        )
        snapshot = {
            "agents": [],      # 智能体列表
            "fires": [],       # 火灾列表
            "persons": [],     # 待救援人员列表
            "reservoirs": [],  # 资源源列表
            "deposits": [],    # 存放点列表
            "flammables": [],  # 可燃物列表
        }
        for obj in all_objects:
            # Use class_name() to resolve decorator wrappers (IdWrapper, CollidableWrapper, etc.)
            # 使用 class_name() 解析装饰器包装（IdWrapper, CollidableWrapper 等）
            type_name = obj.class_name() if hasattr(obj, "class_name") else type(obj).__name__
            obj_dict = {
                "name": getattr(obj, "name", str(obj)),
                "position": getattr(obj, "position", None),         # 位置坐标
                "object_id": getattr(obj, "object_id", str(obj)),   # 物体唯一 ID
                "type": type_name,                                  # 物体类型
            }
            if type_name == "AbsAgent":
                # Access inventory directly from the agent object
                # 直接从智能体对象访问库存
                inv = getattr(obj, "inventory", {})
                obj_dict["inventory"] = inv
                snapshot["agents"].append(obj_dict)
            elif type_name == "Fire":
                # 火灾：记录平均强度和火灾类型
                obj_dict["average_intensity"] = str(
                    getattr(obj, "average_intensity", "?")
                )
                obj_dict["fire_type"] = str(getattr(obj, "fire_type", "?"))
                snapshot["fires"].append(obj_dict)
            elif type_name == "Person":
                # 待救援人员：记录搬运所需人数和状态
                obj_dict["load"] = getattr(obj, "load", 2)
                obj_dict["status"] = str(getattr(obj, "status", "?"))
                snapshot["persons"].append(obj_dict)
            elif type_name == "Reservoir":
                # 资源源：记录资源类型
                obj_dict["resource_type"] = str(
                    getattr(obj, "resource_type", "?")
                )
                snapshot["reservoirs"].append(obj_dict)
            elif type_name == "Deposit":
                # 存放点：记录库存
                obj_dict["inventory"] = str(getattr(obj, "inventory", {}))
                snapshot["deposits"].append(obj_dict)
            elif type_name == "Flammable":
                # 可燃物：记录燃烧强度
                obj_dict["intensity"] = str(getattr(obj, "intensity", "?"))
                snapshot["flammables"].append(obj_dict)
        return snapshot

    def set_logger(self, logger):
        """Set the IntegrationLogger for experiment logging.
        设置 IntegrationLogger 用于实验日志记录。

        Args:
            logger: IntegrationLogger 实例
        """
        self._logger_ref = logger

    def get_last_step_log(self) -> dict:
        """Return the log data from the most recently executed step.
        返回最近一次执行步骤的日志数据。

        Returns:
            dict with keys: actions, successes, observations, subtasks
            包含 actions, successes, observations, subtasks 键的字典
        """
        return self._last_step_log

    def stop(self):
        """Clean up the environment.
        清理环境资源。

        调用 env.stop() 释放 SAR 引擎资源。
        """
        if hasattr(self, "env"):
            self.env.stop()

    # -- Internal -----------------------------------------------------------
    # -- 内部方法 -----------------------------------------------------------

    async def _execute_step(self):
        """Execute one env.step() with all collected actions, then broadcast obs.
        使用所有收集到的动作执行一次 env.step()，然后广播观测结果。

        该方法由最后一个提交动作的智能体触发，或超时后自动触发。
        """
        # Collect actions in agent order, normalizing "NoOp" to "NoOp()" for parse_action
        # 按智能体顺序收集动作，将 "NoOp" 标准化为 "NoOp()"（env.parse_action 要求带括号）
        actions = []
        for i in range(self.num_agents):
            raw_action = self._action_queue.get(i, "NoOp")
            # Ensure actions have parentheses (required by env.parse_action)
            # 确保动作字符串包含括号（env.parse_action 的必需格式）
            if "(" not in raw_action:
                raw_action = raw_action + "()"
            actions.append(raw_action)

        # Execute synchronously (env.step is blocking, run in thread)
        # 同步执行（env.step 是阻塞操作，通过线程运行避免阻塞事件循环）
        obs_text, act_successes = await asyncio.to_thread(self.env.step, actions)

        # Parse per-agent observations from env state
        # 从环境状态中解析每个智能体的观测结果
        observations = []
        for i in range(self.num_agents):
            obs, _ = self.env.generate_obs_text(i)   # 生成观测文本
            state = self.env.get_agent_state(i)       # 获取智能体状态
            full_obs = f"{obs}\n{state}"              # 拼接完整观测
            self._current_obs[i] = full_obs           # 保存当前观测
            observations.append(full_obs)
            self._obs_events[i].set()                 # 通知等待的智能体

        self._step_counter += 1  # 步骤计数器加一

        # Check task completion
        # 检查任务是否完成
        self._finished = self.env.checker.check_success()

        # Build per-step log snapshot (actions, successes, observations, subtasks)
        # 构建每步日志快照
        self._last_step_log = {
            "actions": list(actions),
            "successes": list(act_successes) if act_successes else [],
            "observations": observations,
            "subtasks": [
                log.get("subtask", "") for log in self._pending_agent_logs
            ] if self._pending_agent_logs else [],
        }

        # Flush pending agent interaction logs to IntegrationLogger
        # 将积累的 agent 交互日志写入 IntegrationLogger
        if self._logger_ref is not None and self._pending_agent_logs:
            for log_entry in self._pending_agent_logs:
                self._logger_ref.log_agent_interaction(
                    step=self._step_counter,
                    agent=log_entry.get("agent", ""),
                    subtask=log_entry.get("subtask", ""),
                    tool_calls=[{
                        "name": log_entry.get("tool_name", ""),
                        "args": log_entry.get("tool_args", {}),
                    }],
                    action=log_entry.get("action", ""),
                    observation=log_entry.get("observation", ""),
                )
        self._pending_agent_logs.clear()

        # Reset for next step
        # 重置动作队列和事件，为下一步做准备
        self._action_queue.clear()
        self._obs_events = [asyncio.Event() for _ in range(self.num_agents)]
