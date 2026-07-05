"""Coordinator AgentExecutor - 通过 A2A 协议接收外部客户端任务并加入队列。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from a2a.coordinator.verifier import VerificationReport

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState, TaskStatus
from a2a.types import TaskStatusUpdateEvent
from a2a.types.a2a_pb2 import StreamResponse
from a2a.helpers import new_text_message
from a2a.client.errors import A2AClientError

from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError
from a2a.coordinator.router import RouterAgent, DAGPlan, DAGTask
from a2a.coordinator.task_logger import TaskLogger
from a2a.coordinator.task_queue import TaskQueue
from a2a.coordinator.task_store import TaskStore
from a2a.shared.types import DistributedTask
from Agent.router_agent.context import ContextConfig
from Agent.controller import CallbackSink, SessionAPI, TeeSink
from Agent.router_agent.build import (
    RouterBuildOptions,
    RouterControllerBuildOptions,
    build_router_controller,
)
from a2a.coordinator.sink import A2ACoordinatorSink
from a2a.builtin_tools.dispatch_task import DispatchTaskTool
from a2a.builtin_tools.query_task_events import QueryTaskEventsTool
from a2a.builtin_tools.verify_result import VerifyResultTool
from a2a.builtin_tools.query_task_results import QueryTaskResultsTool
from a2a.builtin_tools.cancel_task import CancelTaskTool
from a2a.builtin_tools.query_workers import QueryWorkersTool
from a2a.builtin_tools.update_plan import UpdatePlanTool
from a2a.builtin_tools.respond_worker import RespondWorkerTool
from sar_orch.tools.coordinator.finish_task import FinishTaskTool as SARFinishTaskTool

logger = logging.getLogger(__name__)


def _extract_event_text(event: StreamResponse) -> str:
    """从 StreamResponse protobuf 中提取文本。"""
    if event.HasField("status_update"):
        su = event.status_update
        if su.status and su.status.message:
            parts = su.status.message.parts
            texts = [p.text for p in parts if p.text]
            return " ".join(texts) or str(su.status.message)[:200]
    if event.HasField("artifact_update"):
        au = event.artifact_update
        if au.artifact and au.artifact.parts:
            texts = [p.text for p in au.artifact.parts if p.text]
            return " ".join(texts)
    if event.HasField("task"):
        return f"Task:{event.task.id} state={event.task.status.state}"
    if event.HasField("message"):
        return " ".join(p.text for p in event.message.parts if p.text)
    return ""


def _extract_all_text(events: list[StreamResponse]) -> str:
    """从 StreamResponse 列表中提取最终有效文本。

    优先取 artifact_update（Agent 的 final_text），
    这是 Mini-Agent 的纯净回答，不含 A2A 协议包装。
    没有 artifact 时回退到最后一条非空状态文本。
    """
    artifact_text = None
    last_status_text = None

    for event in events:
        text = _extract_event_text(event)
        if not text:
            continue
        if event.HasField("artifact_update"):
            artifact_text = text
        elif not text.startswith("Task:"):
            last_status_text = text

    result = artifact_text or last_status_text or "(empty result)"
    return result


def _strip_data_blocks(text: str) -> str:
    """移除 [DATA] 块，返回人类可读文本。"""
    return re.sub(r"\n\[DATA\]\n.*", "", text)


class CoordinatorAgentExecutor(AgentExecutor):
    """
    Coordinator 的 A2A AgentExecutor。

    接收外部客户端的 A2A 消息任务，使用 RouterAgent 智能路由到合适的 Worker Agent。

    工作流程（闭环 DAG）：
    1. Router 输出分层 DAG 执行计划
    2. 逐层并行分发任务到 Worker
    3. 收集 Worker 回复，用 [task_id] 占位符注入上游结果
    4. 可选：Verifier 审查每层输出
    5. Router 重规划（继续 / 调整 / 停止）
    6. 返回最终结果给客户端
    """

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        router: RouterAgent | None = None,
        task_queue: TaskQueue | None = None,
        verifier: Any | None = None,  # VerifierAgent (lazy import)
        task_logger: TaskLogger | None = None,
        max_dag_iterations: int = 10,  # 防止无限循环的安全上限
        orchestration_mode: str = "agentic",
        max_tasks_per_run: int = 20,
        orchestration_timeout: int = 600,
        router_step_callback=None,
        context_config: ContextConfig | None = None,
        token_limit: int = 80000,
        require_explicit_completion: bool = False,
        coordinator_host: str = "localhost",
        coordinator_port: int = 8080,
        sandbox_policy=None,
    ) -> None:
        self._coordinator_host = coordinator_host
        self._coordinator_port = coordinator_port
        self._registry = registry or AgentRegistry()
        self._router = router or RouterAgent(registry=self._registry)
        self._task_queue = task_queue or TaskQueue()
        self._verifier = verifier
        self._task_logger = task_logger
        self._max_dag_iterations = max_dag_iterations
        self._orchestration_mode = orchestration_mode
        self._max_tasks_per_run = max_tasks_per_run
        self._orchestration_timeout = orchestration_timeout
        self._router_step_callback = router_step_callback
        self._context_config = context_config
        self._token_limit = token_limit
        self._require_explicit_completion = require_explicit_completion
        self._sandbox_policy = sandbox_policy

        # 统一控制器：通过 build_router_controller 组装。
        # agent_factory 自动合并运行时 extra_tools / system_prompt_override。
        self._controller: SessionAPI = build_router_controller(
            RouterControllerBuildOptions(
                agent=RouterBuildOptions(
                    model=self._router._model,
                    provider=self._router._provider,
                    api_base=self._router._api_base,
                    api_key=os.environ.get(self._router._api_key_env, ""),
                    system_prompt=self._router._system_prompt,
                    builtin_tools=[QueryWorkersTool(self._registry)],
                    custom_tools=self._router._custom_tools,
                    extra_tools=self._router._extra_tools,
                    max_steps=self._router._max_steps,
                    workspace_dir=str(self._router._workspace_dir),
                    log_dir=self._router._log_dir,
                    skills_dir=self._router._skills_dir,
                    sandbox_policy=self._sandbox_policy,
                ),
                context_config=self._context_config,
                token_limit=self._token_limit,
                require_explicit_completion=self._require_explicit_completion,
            ),
        )

    def clear_sessions(self) -> None:
        """清空所有协调器会话存储。"""
        self._controller.clear_sessions()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """处理 A2A 任务：使用 RouterAgent DAG 计划 + 闭环执行。"""
        if self._task_logger is not None:
            try:
                raw_query = context.get_user_input() or ""
                msg = context.message
                request_info = {
                    "query_preview": raw_query[:500],
                    "has_metadata": bool(msg and msg.HasField("metadata")),
                    "task_id": context.task_id,
                    "context_id": context.context_id,
                }
                self._task_logger.log_event(
                    context.current_task.id if context.current_task else "unknown",
                    "raw_request",
                    request_info,
                    source="executor",
                )
            except Exception as exc:
                logger.debug("Failed to log raw request: %s", exc)
        task = context.current_task
        if task is None:
            from a2a.helpers import new_task_from_user_message

            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        task_id = task.id
        context_id = task.context_id

        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.start_work(message=None)

        query = context.get_user_input()
        if not query:
            query = ""

        # 从消息 metadata 中提取友好任务名称
        friendly_name = None
        try:
            msg = context.message
            if msg and msg.HasField("metadata"):
                md = msg.metadata
                if "task_name" in md:
                    friendly_name = md["task_name"]
        except Exception:
            pass

        if self._task_logger is not None:
            self._task_logger.init_task(task_id, friendly_name)

        # 将任务加入 TaskQueue 进行生命周期追踪
        total_task = DistributedTask(task_id=task_id, task_type="agent", prompt=query)
        self._task_queue.enqueue(total_task)
        self._task_queue.start(task_id)

        if self._task_logger is not None:
            self._task_logger.log_event(
                task_id, "task_start", {"query": query}, source="executor"
            )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message(
                        "Task submitted to coordinator, planning with AI agent"
                    ),
                ),
            )
        )

        try:
            # 按编排模式分流
            if self._orchestration_mode == "dag":
                await self._execute_dag_loop(query, task_id, context_id, event_queue)
            else:
                await self._execute_agentic(query, task_id, context_id, event_queue)
        except AgentNotFoundError as e:
            self._task_queue.fail(task_id, str(e))
            if self._task_logger is not None:
                self._task_logger.log_event(
                    task_id, "task_error", {"error": str(e)}, source="executor"
                )
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                        message=new_text_message(f"No available agent: {e}"),
                    ),
                )
            )
        except Exception as e:
            self._task_queue.fail(task_id, str(e))
            if self._task_logger is not None:
                self._task_logger.log_event(
                    task_id, "task_error", {"error": str(e)}, source="executor"
                )
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                        message=new_text_message(f"Execution error: {e}"),
                    ),
                )
            )

    async def _execute_agentic(
        self,
        user_request: str,
        task_id: str,
        context_id: str,
        event_queue: EventQueue,
    ) -> None:
        """Agentic 编排模式：RouterAgent 作为自主编排者全程驱动。

        通过 dispatch_task/collect_results/verify_result 等工具
        在 Agent.run() loop 内自主完成编排。
        """
        from a2a.coordinator.event_store import event_store

        event_store.clear()

        store = TaskStore(
            original_request=user_request,
            router=self._router,
            verifier=self._verifier,
            max_tasks=self._max_tasks_per_run,
            context_id=context_id,
        )

        tools = [
            UpdatePlanTool(store),
            DispatchTaskTool(
                store,
                coordinator_host=self._coordinator_host,
                coordinator_port=self._coordinator_port,
            ),
            QueryTaskEventsTool(store),
            VerifyResultTool(store),
            QueryTaskResultsTool(store.results),
            RespondWorkerTool(store, self._registry),
            SARFinishTaskTool(store),
            CancelTaskTool(store, self._registry),
        ]

        # 输出通道：coordinator 传输 sink（+ 可选外部 router_step_callback）
        sink = A2ACoordinatorSink(
            event_queue, task_id, context_id, self._task_logger, store
        )
        if self._router_step_callback is not None:
            sink = TeeSink([sink, CallbackSink(self._router_step_callback)])

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message("Agentic orchestration started"),
                ),
            )
        )

        if self._task_logger is not None:
            self._task_logger.log_event(
                task_id, "agentic_start", {"mode": "agentic"}, source="executor"
            )

        try:
            result = await asyncio.wait_for(
                self._controller.submit(
                    context_id,
                    user_request,
                    sink,
                    extra_tools=tools,
                    system_prompt_override=self._router.agentic_prompt,
                ),
                timeout=self._orchestration_timeout,
            )
            final_text = result.content
        except asyncio.TimeoutError:
            final_text = (
                f"Orchestration timed out after {self._orchestration_timeout}s. "
                f"Completed {store.dispatched_count} tasks."
            )
            logger.warning("Agentic orchestration timed out for task %s", task_id)
            for fut in store._futures.values():
                if not fut.done():
                    fut.cancel()
        except Exception as e:
            final_text = f"Orchestration error: {e}"
            logger.exception("Agentic orchestration failed for task %s", task_id)
            for fut in store._futures.values():
                if not fut.done():
                    fut.cancel()

        await self._finish(event_queue, task_id, context_id, final_text)

        if self._task_logger is not None:
            self._task_logger.log_event(
                task_id,
                "task_final",
                {"result": final_text[:500] if final_text else None},
                source="executor",
            )
            self._task_logger.log_event(
                task_id,
                "done",
                {"mode": "agentic", "tasks_dispatched": store.dispatched_count},
                source="executor",
            )

    async def _execute_dag_loop(
        self,
        user_request: str,
        task_id: str,
        context_id: str,
        event_queue: EventQueue,
    ) -> None:
        """
        闭环 DAG 执行主循环。

        流程:
        1. Router 初始规划 → DAGPlan
        2. 逐层执行: dispatch → collect → verify → re-plan
        3. 当 Router 返回空 layers 时结束
        """
        completed_results: dict[str, str] = {}  # task_id → result_text
        verification_reports: list[
            "VerificationReport"
        ] = []  # list of VerificationReport

        # Step 1: 初始规划
        dag_plan = await self._router.route_dag(
            user_request, task_id=task_id, context_id=context_id
        )
        await self._notify_plan(event_queue, task_id, context_id, dag_plan)

        if dag_plan.is_empty:
            # 如果规划器在 reasoning 中留下了回答文本（LLM 直接作答），
            # 则将其作为最终结果返回；否则使用默认消息
            fallback_msg = (
                dag_plan.reasoning[:2000]
                if dag_plan.reasoning
                else "No workers available or no tasks needed."
            )
            await self._finish(event_queue, task_id, context_id, fallback_msg)
            if self._task_logger is not None:
                self._task_logger.log_event(
                    task_id,
                    "done",
                    {"total_layers": 0, "total_tasks": 0},
                    source="executor",
                )
            return

        # Step 2: 逐层执行
        current_layer_idx = 0
        iteration_count = 0
        while current_layer_idx < len(dag_plan.layers):
            iteration_count += 1
            if iteration_count > self._max_dag_iterations:
                logger.warning(
                    "DAG loop exceeded max iterations (%d), forcing completion. "
                    "Completed %d tasks across %d layers.",
                    self._max_dag_iterations,
                    len(completed_results),
                    current_layer_idx,
                )
                break

            layer = dag_plan.layers[current_layer_idx]

            # 注入上游结果到 prompt
            enriched_tasks = self._enrich_prompts(layer.tasks, completed_results)

            # 通知层开始
            task_ids = [t.task_id for t in enriched_tasks]
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_WORKING,
                        message=new_text_message(
                            f"[Layer {layer.layer_index}] Dispatching: {task_ids}"
                        ),
                    ),
                )
            )

            if self._task_logger is not None:
                self._task_logger.log_event(
                    task_id,
                    "layer_start",
                    {"layer_index": layer.layer_index, "task_ids": task_ids},
                    source="executor",
                )

            # 并行分发层内所有任务
            layer_results = await self._dispatch_layer(enriched_tasks, context_id)

            # 收集结果
            for task, events in zip(enriched_tasks, layer_results):
                raw_text = _extract_all_text(events)
                result_text = _strip_data_blocks(raw_text)
                completed_results[task.task_id] = result_text

                # 日志记录：解析 Worker 事件中的 [DATA] 块
                self._log_worker_events(task_id, task.task_id, task.agent_id, events)

                if self._task_logger is not None:
                    self._task_logger.log_event(
                        task_id,
                        "task_complete",
                        {
                            "subtask_id": task.task_id,
                            "result": result_text[:500],
                            "worker_id": task.agent_id,
                        },
                        source="worker",
                    )

                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        status=TaskStatus(
                            state=TaskState.TASK_STATE_WORKING,
                            message=new_text_message(
                                f"[{task.task_id}] Completed: {result_text[:100]}..."
                            ),
                        ),
                    )
                )

            # 验证（如果配置了 Verifier）
            layer_verifications = []
            if self._verifier is not None:
                for task in enriched_tasks:
                    try:
                        report = await self._verifier.verify(
                            original_request=user_request,
                            subtask_prompt=task.prompt,
                            worker_output=completed_results[task.task_id],
                            task_id=task.task_id,
                        )
                        verification_reports.append(report)
                        layer_verifications.append(report)
                        await event_queue.enqueue_event(
                            TaskStatusUpdateEvent(
                                task_id=task_id,
                                context_id=context_id,
                                status=TaskStatus(
                                    state=TaskState.TASK_STATE_WORKING,
                                    message=new_text_message(
                                        f"[{task.task_id}] Verification: {'PASS' if report.passed else 'FAIL'} — {report.summary}"
                                    ),
                                ),
                            )
                        )
                        if self._task_logger is not None:
                            self._task_logger.log_event(
                                task_id,
                                "verify",
                                {
                                    "subtask_id": task.task_id,
                                    "status": "PASS" if report.passed else "FAIL",
                                    "reason": report.summary,
                                },
                                source="verifier",
                            )
                    except Exception as e:
                        logger.warning(
                            "Verification failed for %s: %s", task.task_id, e
                        )

            # 重规划：让 Router 决定下一步
            remaining = (
                dag_plan.layers[current_layer_idx + 1 :]
                if current_layer_idx + 1 < len(dag_plan.layers)
                else []
            )
            next_plan = await self._router.plan_next(
                original_request=user_request,
                completed_results=completed_results,
                verification_reports=verification_reports,
                remaining_layers=remaining,
                task_id=task_id,
                context_id=context_id,
            )

            if next_plan.is_empty:
                # Router 信号：任务完成
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        status=TaskStatus(
                            state=TaskState.TASK_STATE_WORKING,
                            message=new_text_message(
                                "Router signals completion — all tasks done."
                            ),
                        ),
                    )
                )
                break

            # 如果 Router 返回了新计划，替换剩余层
            if next_plan.layers:
                dag_plan = DAGPlan(
                    layers=list(dag_plan.layers[: current_layer_idx + 1])
                    + list(next_plan.layers),
                    reasoning=next_plan.reasoning,
                )
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        status=TaskStatus(
                            state=TaskState.TASK_STATE_WORKING,
                            message=new_text_message(
                                f"Router re-planned: {next_plan.reasoning[:100]}..."
                            ),
                        ),
                    )
                )

                if self._task_logger is not None:
                    new_layers_list = [
                        [t.task_id for t in layer.tasks] for layer in next_plan.layers
                    ]
                    self._task_logger.log_event(
                        task_id,
                        "replan",
                        {
                            "reason": next_plan.reasoning[:2000],
                            "new_layers": new_layers_list,
                        },
                        source="router",
                    )

            current_layer_idx += 1

        # 完成
        self._task_queue.complete(
            task_id,
            {
                "results": completed_results,
                "verification": [
                    {"task_id": r.task_id, "passed": r.passed, "summary": r.summary}
                    for r in verification_reports
                ],
            },
        )

        final_text = self._format_final_results(completed_results)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED,
                    message=new_text_message(final_text),
                ),
            )
        )

        if self._task_logger is not None:
            self._task_logger.log_event(
                task_id,
                "task_final",
                {"result": final_text[:500] if final_text else None},
                source="executor",
            )
            total_tasks = sum(len(layer.tasks) for layer in dag_plan.layers)
            self._task_logger.log_event(
                task_id,
                "done",
                {"total_layers": len(dag_plan.layers), "total_tasks": total_tasks},
                source="executor",
            )

    # ============================================================
    # DAG 执行辅助方法
    # ============================================================

    async def _dispatch_layer(
        self,
        tasks: list[DAGTask],
        context_id: str | None = None,
    ) -> list[list[StreamResponse]]:
        """并行执行一层中的所有任务。

        使用 asyncio.gather 并发推送。单个任务失败不影响其他任务。
        """

        async def dispatch_one(task: DAGTask) -> list[StreamResponse]:
            try:
                return await self._router.push_task(
                    task.agent_id, task.prompt, context_id=context_id
                )
            except (A2AClientError, AgentNotFoundError) as e:
                logger.error(
                    "Failed to dispatch task %s to %s: %s",
                    task.task_id,
                    task.agent_id,
                    e,
                )
                # 返回空列表作为错误占位，让其他任务继续
                return []

        return await asyncio.gather(*[dispatch_one(t) for t in tasks])

    def _log_worker_events(
        self, task_id: str, subtask_id: str, worker_id: str, events
    ) -> None:
        """从 Worker StreamResponse 事件中解析 [DATA] 块并写入日志。"""
        if self._task_logger is None:
            return

        for event in events:
            try:
                text = _extract_event_text(event)

                if "\n[DATA]\n" not in text:
                    continue

                # 提取最后一段 [DATA] 块
                parts = text.split("\n[DATA]\n")
                json_str = parts[-1].split("\n")[0]

                parsed = json.loads(json_str)
                event_type = parsed.pop("ev", "unknown")
                parsed.pop("ts", None)  # 移除 Worker 时间戳，TaskLogger 会自动添加

                self._task_logger.log_event(
                    task_id,
                    event_type,
                    {
                        "subtask_id": subtask_id,
                        "worker_id": worker_id,
                        **parsed,
                    },
                    source="worker",
                )
            except (json.JSONDecodeError, KeyError, ValueError):
                pass  # 解析失败，静默跳过

    def _enrich_prompts(
        self, tasks: list[DAGTask], results: dict[str, str]
    ) -> list[DAGTask]:
        """将 prompt 中的 [task_id] 占位符替换为上游实际结果。

        只替换已有结果的上游引用；尚未完成的任务引用会保留原样（由后续层替换）。
        """
        enriched = []
        for task in tasks:
            new_prompt = task.prompt
            for dep_id in task.depends_on:
                if dep_id in results:
                    placeholder = f"[{dep_id}]"
                    summary = results[dep_id]
                    # 截断过长的上游结果，避免下游 prompt 爆炸
                    if len(summary) > 2000:
                        summary = summary[:2000] + "...[truncated]"
                    new_prompt = new_prompt.replace(
                        placeholder,
                        f"\n--- Result from {dep_id} ---\n{summary}\n--- End of {dep_id} ---",
                    )
            enriched.append(
                DAGTask(
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    prompt=new_prompt,
                    depends_on=task.depends_on,
                    description=task.description,
                )
            )
        return enriched

    # ============================================================
    # 通知和格式化辅助方法
    # ============================================================

    async def _notify_plan(
        self, event_queue: EventQueue, task_id: str, context_id: str, dag_plan: DAGPlan
    ) -> None:
        """通知客户端 DAG 计划已生成。"""
        layer_summaries = []
        for layer in dag_plan.layers:
            task_ids = [t.task_id for t in layer.tasks]
            layer_summaries.append(f"Layer {layer.layer_index}: {task_ids}")

        plan_text = (
            f"Plan: {dag_plan.total_tasks} tasks in {len(dag_plan.layers)} layers\n"
            + "\n".join(layer_summaries)
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=new_text_message(plan_text),
                ),
            )
        )

        if self._task_logger is not None:
            layer_ids = [[t.task_id for t in layer.tasks] for layer in dag_plan.layers]
            self._task_logger.log_event(
                task_id,
                "plan",
                {"layers": layer_ids, "task_count": dag_plan.total_tasks},
                source="router",
            )

    async def _finish(
        self, event_queue: EventQueue, task_id: str, context_id: str, message: str
    ) -> None:
        """以完成状态结束任务。"""
        self._task_queue.complete(task_id, {"message": message})
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED,
                    message=new_text_message(message),
                ),
            )
        )

    @staticmethod
    def _format_final_results(results: dict[str, str]) -> str:
        """格式化最终结果文本。"""
        if not results:
            return "(no results)"
        parts = []
        for task_id, text in results.items():
            truncated = text[:500] + ("..." if len(text) > 500 else "")
            parts.append(f"[{task_id}]\n{truncated}")
        return "\n\n".join(parts)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消任务。"""
        task_id = context.task_id or ""
        context_id = context.context_id or ""

        try:
            self._task_queue.cancel(task_id)
        except Exception:
            pass

        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.cancel()
