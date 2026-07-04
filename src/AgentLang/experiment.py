#!/usr/bin/env python3
"""SAR 端到端实验运行器（LangGraph 内核版）。

镜像 sar_orch/experiment.py，仅把 coordinator/worker 的工厂函数换成
AgentLang 的 LangGraph 版（create_server_lang / create_worker_a2a_server_lang）。
SARBarrier / ExperimentLogger / SAR 工具 / prompts 全部原样复用，
保证 run_metrics.json 与 5 个 CSV schema 与原版完全一致，benchmark/aggregate 零改动。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time

from sar_orch.barrier import SARBarrier
from sar_orch.logger import ExperimentLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_experiment_lang")

COORDINATOR_PORT = 8080

# 本文件位于 src/AgentLang/experiment.py，比原版 sar_orch/experiment.py 深一层，
# 故 _PROJECT_ROOT 需多上溯一级 dirname。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_COORDINATOR_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "coordinator")
_WORKER_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "worker")
_COORDINATOR_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs", "agent", "sar_coordinator_lang")
_WORKER_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs", "agent", "sar_worker_lang")


class SARCoordinatorLang:
    """SAR Coordinator（LangGraph 内核）— 包装 LangCoordinatorServer。"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        barrier=None,
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        prompts_dir: str | None = None,
        log_dir: str | None = None,
        orchestration_mode: str = "agentic",
        exp_logger=None,
    ):
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._barrier = barrier
        self._model = model
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._prompts_dir = prompts_dir
        self._log_dir = log_dir
        self._orchestration_mode = orchestration_mode
        self._exp_logger = exp_logger
        self._server = None

    async def start(self):
        """启动协调器服务（后台线程）。"""
        from AgentLang.coordinator.server import create_server_lang
        from sar_orch.tools.coordinator.query_sar_state import QuerySARStateTool
        from a2a.shared.env_loader import load_env_file
        from pathlib import Path
        import threading

        # 显式加载 .env 设置 API key（原版靠 worker side-effect，此处显式化更健壮）
        env_path = Path(__file__).parent.parent.parent / ".env"
        env = load_env_file(str(env_path))
        if "api_key" in env:
            os.environ[self._api_key_env] = env["api_key"]

        sar_tool = QuerySARStateTool(barrier=self._barrier)

        def _router_cb(event_type: str, **kw):
            if event_type == "llm_response" and self._exp_logger is not None:
                usage = kw.get("usage")
                if usage is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent="Coordinator",
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif (
                event_type == "tool_start"
                and kw.get("tool_name") == "dispatch_task"
                and self._exp_logger is not None
            ):
                args = kw.get("arguments", {})
                self._exp_logger.log_router_interaction(
                    step=getattr(self._barrier, "_step_counter", 0),
                    subtask=args.get("prompt", ""),
                    assigned_to=args.get("agent_id", ""),
                )

        self._server = create_server_lang(
            host=self._host,
            port=self._port,
            a2a_port=self._a2a_port,
            router_model=self._model,
            router_provider=self._provider,
            router_api_base=self._api_base,
            router_api_key_env=self._api_key_env,
            router_max_steps=20,
            router_temperature=0.7,
            prompts_dir=self._prompts_dir,
            extra_tools=[sar_tool],
            log_dir=self._log_dir,
            verifier_enabled=False,
            orchestration_mode=self._orchestration_mode,
            max_tasks_per_run=50,
            orchestration_timeout=1200,
            router_step_callback=_router_cb,
        )
        self._server.set_barrier(self._barrier)
        logger.info(
            "SAR Coordinator (LangGraph) starting on port %d (A2A port %d)",
            self._port,
            self._a2a_port,
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        await asyncio.sleep(2.0)

    async def submit_task(self, task_description: str) -> str:
        """经 A2A JSON-RPC 提交初始任务。"""
        import httpx

        a2a_url = f"http://{self._host}:{self._a2a_port}/"
        await asyncio.sleep(1.0)
        async with httpx.AsyncClient() as client:
            try:
                await client.get(f"{a2a_url}.well-known/agent-card.json", timeout=10.0)
            except Exception as e:
                logger.warning("Could not fetch agent card: %s", e)
            payload = {
                "jsonrpc": "2.0",
                "id": "task-1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "role": 1,
                        "parts": [{"text": task_description}],
                    }
                },
            }
            headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
            try:
                resp = await client.post(
                    f"{a2a_url}api/v1/jsonrpc/", json=payload, headers=headers, timeout=600.0
                )
                return json.dumps(resp.json(), indent=2)
            except Exception as e:
                logger.error("Task submission failed: %s", e)
                return f"Error: {e}"

    async def stop(self):
        if self._server is not None and hasattr(self._server, "shutdown"):
            await self._server.shutdown()


class SARWorkerLang:
    """SAR Worker（LangGraph 内核）— 包装 LangAgentAdapter A2A server。"""

    def __init__(
        self,
        worker_id: str,
        agent_name: str,
        agent_idx: int,
        barrier,
        a2a_host: str = "0.0.0.0",
        a2a_port: int = 8191,
        coordinator_url: str = "ws://localhost:8080",
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        prompts_dir: str | None = None,
        log_dir: str | None = None,
        exp_logger=None,
    ):
        self.worker_id = worker_id
        self.agent_name = agent_name
        self.agent_idx = agent_idx
        self._barrier = barrier
        self._a2a_host = a2a_host
        self._a2a_port = a2a_port
        self._coordinator_url = coordinator_url
        self._model = model
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._prompts_dir = prompts_dir
        self._log_dir = log_dir
        self._exp_logger = exp_logger
        self._server = None
        self._client = None
        self._server_task = None
        import threading

        self._stop_event = threading.Event()
        self._call_seq = 0
        self._pending_tool = None
        self._last_llm_output = ""

    def start(self):
        from sar_orch.tools.worker import SAR_WORKER_TOOLS
        from AgentLang.worker_adapter import create_worker_a2a_server_lang
        from a2a.worker.coordinator_client import CoordinatorWebSocketClient
        from a2a.shared.env_loader import load_env_file
        from pathlib import Path

        env_path = Path(__file__).parent.parent / ".env"
        env = load_env_file(str(env_path))
        if "api_key" in env:
            os.environ[self._api_key_env] = env["api_key"]

        tools = [
            tool_cls(barrier=self._barrier, agent_idx=self.agent_idx)
            for tool_cls in SAR_WORKER_TOOLS
        ]
        cap_list = ["sar", "navigation", "rescue", "firefighting"]

        def _build_action(tool_name: str, args: dict) -> str:
            name_map = {
                "navigate_to": "NavigateTo", "move": "Move", "explore": "Explore",
                "carry_person": "CarryPerson", "drop_off_person": "DropOffPerson",
                "get_supply": "GetSupply", "store_supply": "StoreSupply",
                "use_supply": "UseSupply", "clear_inventory": "ClearInventory",
                "no_op": "NoOp",
            }
            sar_name = name_map.get(tool_name, tool_name)
            if not args:
                return f"{sar_name}()"
            arg_parts = ", ".join(str(v) for v in args.values())
            return f"{sar_name}({arg_parts})"

        def _step_callback(type_: str, **data):
            if type_ == "llm_response":
                self._last_llm_output = data.get("content", "")
                usage = data.get("usage")
                if usage is not None and self._exp_logger is not None:
                    self._exp_logger.log_token_usage(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
            elif type_ == "tool_start":
                self._pending_tool = {
                    "tool_name": data.get("tool_name", ""),
                    "arguments": data.get("arguments", {}),
                }
            elif type_ == "tool_result" and self._pending_tool is not None:
                tool_name = self._pending_tool["tool_name"]
                args = self._pending_tool["arguments"]
                exp = self._exp_logger
                if exp is not None:
                    exp.log_agent_interaction(
                        step=getattr(self._barrier, "_step_counter", 0),
                        agent=self.agent_name,
                        tool_name=tool_name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        action=_build_action(tool_name, args),
                        observation=data.get("content", ""),
                        llm_output=self._last_llm_output,
                    )
                self._pending_tool = None

        self._server = create_worker_a2a_server_lang(
            worker_id=self.worker_id,
            host=self._a2a_host,
            port=self._a2a_port,
            capabilities=cap_list,
            model=self._model,
            provider=self._provider,
            api_base=self._api_base,
            api_key_env=self._api_key_env,
            extra_tools=tools,
            prompts_dir=Path(self._prompts_dir) if self._prompts_dir else None,
            log_dir=Path(self._log_dir) if self._log_dir else None,
            max_steps=50,
            temperature=0.7,
            step_callback=_step_callback,
            include_base_tools=False,
        )

        a2a_endpoint = f"http://{self._a2a_host}:{self._a2a_port}/"
        self._client = CoordinatorWebSocketClient(
            coordinator_url=self._coordinator_url,
            worker_id=self.worker_id,
            a2a_endpoint=a2a_endpoint,
        )

        async def run():
            self._server_task = asyncio.create_task(self._server.serve())
            await self._client.connect()
            try:
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.5)
            finally:
                await self._client.disconnect()
                self._server_task.cancel()

        import threading

        self._thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        self._stop_event.set()
        self._thread.join(timeout=10)


async def run_experiment(
    scene: int = 1,
    num_agents: int = 2,
    seed: int = 42,
    model: str = "deepseek-v4-flash",
    provider: str = "openai",
    api_base: str = "https://api.deepseek.com",
    api_key_env: str = "OPENAI_API_KEY",
    max_steps: int | None = None,
    coordinator_port: int = 8080,
    agent_base_port: int = 8191,
    log_dir: str | None = None,
) -> dict:
    """跑一次完整 SAR 实验（LangGraph 内核）。与 sar_orch.run_experiment 同参同返回。"""
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]

    logger.info("=" * 60)
    logger.info(
        "SAR Experiment (LangGraph): scene=%d, agents=%d, seed=%d", scene, num_agents, seed
    )
    logger.info("Model: %s (provider=%s)", model, provider)
    logger.info("=" * 60)

    barrier = SARBarrier(num_agents=num_agents, scene=scene, seed=seed)
    max_steps = max_steps or getattr(barrier.env, "task_timeout", 300)
    logger.info("SARBarrier initialized -- max_steps=%d", max_steps)

    exp_logger = ExperimentLogger(experiment_name="sar_experiment_lang", log_dir=log_dir)
    logger.info("ExperimentLogger initialized -- log dir: %s", exp_logger.get_log_dir())

    workers: dict[str, SARWorkerLang] = {}
    coordinator: SARCoordinatorLang | None = None
    final_metrics: dict = {
        "finished": False,
        "steps": 0,
        "coverage": 0.0,
        "transport_rate": 0.0,
        "elapsed_seconds": 0.0,
    }

    try:
        coordinator = SARCoordinatorLang(
            host="0.0.0.0",
            port=coordinator_port,
            a2a_port=coordinator_port + 1,
            barrier=barrier,
            model=model,
            provider=provider,
            api_base=api_base,
            api_key_env=api_key_env,
            prompts_dir=_COORDINATOR_PROMPTS,
            log_dir=_COORDINATOR_LOG_DIR,
            orchestration_mode="agentic",
            exp_logger=exp_logger,
        )

        logger.info("SARCoordinatorLang starting on port %d", coordinator_port)
        coord_task = asyncio.create_task(coordinator.start())
        await asyncio.sleep(3.0)

        for i, name in enumerate(agent_names):
            port = agent_base_port + i
            worker = SARWorkerLang(
                worker_id=name,
                agent_name=name,
                agent_idx=i,
                barrier=barrier,
                a2a_port=port,
                coordinator_url=f"ws://localhost:{coordinator_port}",
                model=model,
                provider=provider,
                api_base=api_base,
                api_key_env=api_key_env,
                prompts_dir=_WORKER_PROMPTS,
                log_dir=_WORKER_LOG_DIR,
                exp_logger=exp_logger,
            )
            workers[name] = worker
            worker.start()
            logger.info("Worker %s started on port %d", name, port)

        await asyncio.sleep(2.0)

        task_description = "Extinguish all fires and rescue all persons"
        logger.info("Submitting initial task: %s", task_description)
        a2a_task = asyncio.create_task(coordinator.submit_task(task_description))
        await asyncio.sleep(0.5)

        start_time = time.time()
        poll_interval = 2.0
        wall_clock_limit = 600.0
        _last_step_logged = -1

        while not barrier.is_finished() and barrier.get_metrics()["steps"] < max_steps:
            await asyncio.sleep(poll_interval)

            if coord_task.done():
                exc = coord_task.exception()
                if exc:
                    logger.error("Coordinator failed: %s", exc)
                    break
            if a2a_task.done() and not a2a_task.cancelled():
                exc = a2a_task.exception()
                if exc:
                    logger.error("A2A orchestration failed: %s", exc)
                    break
                else:
                    logger.info("A2A orchestration completed; exiting poll loop")
                    break

            elapsed = time.time() - start_time
            if elapsed > wall_clock_limit:
                logger.warning(
                    "Wall-clock limit %.0fs reached, stopping (step %d/%d)",
                    wall_clock_limit,
                    barrier.get_metrics()["steps"],
                    max_steps,
                )
                break

            metrics = barrier.get_metrics()
            logger.info(
                "Step %d/%d | Coverage: %.2f | Transport: %.2f | Finished: %s | %.0fs",
                metrics["steps"],
                max_steps,
                metrics["coverage"],
                metrics["transport_rate"],
                metrics["finished"],
                elapsed,
            )

            step_log = (
                barrier.get_last_step_log() if hasattr(barrier, "get_last_step_log") else None
            )
            if step_log and step_log.get("actions") and metrics["steps"] > _last_step_logged:
                exp_logger.log_step(
                    step_num=metrics["steps"],
                    actions=step_log.get("actions", []),
                    successes=step_log.get("successes", []),
                    observations=step_log.get("observations", []),
                    coverage=metrics["coverage"],
                    transport_rate=metrics["transport_rate"],
                    finished=metrics["finished"],
                    timeout_agents=step_log.get("timeout_agents", []),
                )
                exp_logger.flush_summary()
                _last_step_logged = metrics["steps"]

        elapsed_total = time.time() - start_time
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

        if barrier.is_finished():
            logger.info(
                "TASK COMPLETED in %.1f seconds, %d/%d steps",
                elapsed_total,
                final_metrics["steps"],
                max_steps,
            )
        else:
            logger.warning(
                "TASK TIMEOUT after %d steps (limit %d), %.1f seconds",
                final_metrics["steps"],
                max_steps,
                elapsed_total,
            )

        if not a2a_task.done():
            a2a_task.cancel()
            try:
                await a2a_task
            except asyncio.CancelledError:
                pass

        return final_metrics

    finally:
        logger.info("Shutting down workers...")
        for name, worker in workers.items():
            worker.stop()
        if coordinator is not None:
            logger.info("Shutting down coordinator...")
            await coordinator.stop()
        logger.info("Shutting down barrier...")
        barrier.stop()
        exp_logger.close()
        final_metrics["log_dir"] = exp_logger.get_log_dir()
        logger.info("Experiment logs saved to: %s", exp_logger.get_log_dir())
        logger.info("Cleanup complete")


def main():
    parser = argparse.ArgumentParser(description="SAR Experiment (LangGraph kernel)")
    parser.add_argument("--scene", type=int, default=1)
    parser.add_argument("--agents", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model", type=str, default="deepseek-v4-flash")
    parser.add_argument("--provider", type=str, default="openai")
    parser.add_argument("--api-base", type=str, default="https://api.deepseek.com")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--coordinator-port", type=int, default=8080)
    parser.add_argument("--agent-base-port", type=int, default=8191)
    parser.add_argument("--log-dir", type=str, default=None)
    args = parser.parse_args()

    metrics = asyncio.run(
        run_experiment(
            scene=args.scene,
            num_agents=args.agents,
            seed=args.seed,
            model=args.model,
            provider=args.provider,
            api_base=args.api_base,
            max_steps=args.max_steps,
            coordinator_port=args.coordinator_port,
            agent_base_port=args.agent_base_port,
            log_dir=args.log_dir,
        )
    )

    log_dir = metrics.get("log_dir", "")
    if log_dir:
        metrics_file = os.path.join(log_dir, "run_metrics.json")
        with open(metrics_file, "w") as f:
            json.dump(metrics, f, indent=2, default=str)
    print(json.dumps(metrics, default=str))


if __name__ == "__main__":
    main()
