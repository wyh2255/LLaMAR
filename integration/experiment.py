#!/usr/bin/env python3
"""MARoS x LLaMAR SAR 端到端实验运行器（End-to-end MARoS x LLaMAR SAR experiment runner）。
中文说明：
    此脚本是集成的实验入口，按顺序启动以下组件：
      1. SARBarrier（LLaMAR SAREnv 环境屏障）
      2. N 个 SARWorker（每个智能体一个，运行各自的 LLM ReAct 循环）
      3. SARCoordinator（MARoS RouterAgent 任务分解器）
    协调器（Coordinator）接收任务描述（如"扑灭所有火灾并救助所有被困人员"），
    将其分解为子任务并推送给 Worker。Worker 通过 SARBarrier 同步执行动作。

Usage:
    python3 integration/experiment.py --scene=1 --agents=3 --seed=42

Launches:
    1. SARBarrier (LLaMAR SAREnv)
    2. N x SARWorker (one per agent, per-agent LLM ReAct loop)
    3. SARCoordinator (MARoS RouterAgent task decomposition)

The Coordinator receives a task description (e.g., "Extinguish all fires and
rescue all persons"), decomposes it, and pushes subtasks to Workers.
Workers execute actions through the SARBarrier synchronously.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from integration.sar_barrier import SARBarrier
from integration.experiment_logger import IntegrationLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_experiment")


# 为最多 6 个智能体分配端口号，每个 Worker 对应一个唯一的 WebSocket 端口
# Port assignments for up to 6 agents
AGENT_PORTS = {
    "Alice": 8191,
    "Bob": 8192,
    "Charlie": 8193,
    "David": 8194,
    "Emma": 8195,
    "Finn": 8196,
}

COORDINATOR_PORT = 8080


async def _monitor_coordinator(
    coord_task: asyncio.Task,
    barrier: SARBarrier,
) -> None:
    """后台监控协调器运行状态，若崩溃则记录错误日志。
    If coordinator crashes, log the error."""
    try:
        await coord_task
    except Exception as e:
        logger.error("Coordinator crashed: %s", e)


async def run_experiment(
    scene: int = 1,
    num_agents: int = 2,
    seed: int = 42,
    model: str = "deepseek-v4-flash",
    coordinator_model: str = "claude-opus-4-5",
) -> dict:
    """运行一次完整的 SAR 实验。
    Run one full SAR experiment.

    参数（Parameters）:
        scene: SAR 场景编号（1-5），决定地图布局、火情和人员位置
        num_agents: 智能体数量（1-6）
        seed: 随机种子，用于结果可复现
        model: Worker 智能体使用的 LLM 模型名
        coordinator_model: 协调器使用的 LLM 模型名

    返回（Returns）:
        dict: 包含以下键的指标字典：
            - finished: bool，任务是否完成
            - steps: int，执行的总步数
            - coverage: float，地图探索覆盖率
            - transport_rate: float，资源运输成功率
            - elapsed_seconds: float，实验耗时（秒）
    """
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]

    logger.info("=" * 60)
    logger.info("SAR Experiment: scene=%d, agents=%d, seed=%d", scene, num_agents, seed)
    logger.info("Agent model: %s, Coordinator model: %s", model, coordinator_model)
    logger.info("=" * 60)

    # 1. 创建 SARBarrier（环境屏障）：初始化 SAR 仿真环境（包括网格地图、火焰、人员、资源等）
    # Create barrier
    barrier = SARBarrier(num_agents=num_agents, scene=scene, seed=seed)
    logger.info("SARBarrier initialized -- env.task_timeout=%d", barrier.env.task_timeout)

    # 创建实验日志记录器，注入到 barrier
    exp_logger = IntegrationLogger(
        experiment_name="integration_sar",
        num_agents=num_agents,
        scene=scene,
        seed=seed,
    )
    barrier.set_logger(exp_logger)
    logger.info("IntegrationLogger initialized -- log dir: %s", exp_logger.get_log_dir())

    workers: dict = {}
    coordinator = None

    try:
        # 2. 创建 Worker 智能体：为每个智能体创建一个 SARWorker，启动其 A2A HTTP 服务器
        #    注意此循环放在 try 块内，确保 finally 能正确清理资源
        # Create workers (inside try so finally always cleans up)
        for i, name in enumerate(agent_names):
            port = AGENT_PORTS[name]
            worker = _create_worker(
                agent_name=name,
                agent_idx=i,
                barrier=barrier,
                port=port,
                coordinator_url=f"ws://localhost:{COORDINATOR_PORT}",
                model=model,
            )
            workers[name] = worker
            worker.start()  # 启动 Worker 的 A2A HTTP 服务器（非阻塞，运行在后台线程）
            logger.info("Worker %s started on port %d", name, port)

        # 等待所有 Worker 完成 HTTP 服务器启动（确保 Coordinator 连接时端口已就绪）
        # Give workers a moment to start their HTTP servers
        await asyncio.sleep(1.0)

        # 3. 创建协调器（Coordinator）：任务分解和分配，通过 A2A 协议与 Workers 通信
        # Import and create coordinator (inside try so finally always cleans up)
        from integration.coordinator.sar_coordinator import SARCoordinator

        coordinator = SARCoordinator(
            barrier=barrier,
            agent_names=agent_names,
            worker_ports={name: AGENT_PORTS[name] for name in agent_names},
            port=COORDINATOR_PORT,
            model=coordinator_model,
        )
        logger.info("SARCoordinator starting on port %d", COORDINATOR_PORT)

        # 注入实验日志到协调器
        coordinator.set_experiment_logger(exp_logger)

        start_time = time.time()
        coord_task = asyncio.create_task(coordinator.start())

        # 后台监控任务：在 Coordinator 崩溃时及时捕获异常并记录日志
        # Background monitor for coordinator crashes
        monitor_task = asyncio.create_task(
            _monitor_coordinator(coord_task, barrier)
        )

        # 等待 Coordinator 启动完成（uvicorn 绑定端口、RouterAgent 构建完毕）
        # Wait for coordinator to finish starting
        await asyncio.sleep(2.0)

        # ── 提交初始任务：触发 RouterAgent 进行任务分解和分配 ──
        # Submit initial task to trigger RouterAgent decomposition
        task_description = "Extinguish all fires and rescue all persons"
        logger.info("Submitting initial task: %s", task_description)

        # 在后台运行 RouterAgent，避免阻塞主轮询循环
        # Run RouterAgent in background to avoid blocking the main poll loop
        router_task = asyncio.create_task(
            coordinator.submit_task(task_description)
        )

        # 主轮询循环：等待任务完成或超时
        # 每 2 秒检查一次任务状态、指标和 Coordinator 健康状态
        # Wait for task completion or timeout
        task_timeout = barrier.env.task_timeout
        poll_interval = 2.0
        elapsed = 0.0
        last_logged_step = -1  # 用于去重：避免同一步被重复记录到 trajectory.csv

        while not barrier.is_finished() and elapsed < task_timeout:
            await asyncio.sleep(poll_interval)
            elapsed = time.time() - start_time

            # 检查 Coordinator 是否已结束（正常完成或抛出异常）
            # Check if coordinator failed
            if coord_task.done():
                exc = coord_task.exception()
                if exc:
                    logger.error("Coordinator failed with: %s", exc)
                    break

            # 检查 RouterAgent 任务分解是否完成
            # Check if router task decomposition completed
            if router_task.done():
                try:
                    router_result = router_task.result()
                    logger.info("RouterAgent finished: %s", router_result[:300] if router_result else "(empty)")
                except Exception as e:
                    logger.error("RouterAgent failed: %s", e)

            # 从 Barrier 获取当前实验指标并记录日志
            metrics = barrier.get_metrics()
            logger.info(
                "Step %d | Coverage: %.2f | Transport: %.2f | Finished: %s",
                metrics["steps"],
                metrics["coverage"],
                metrics["transport_rate"],
                metrics["finished"],
            )

            # 记录轨迹日志到 IntegrationLogger（仅当步数变化时记录，避免重复）
            # Log trajectory only when step number changes to avoid duplicates
            current_step = metrics["steps"]
            if current_step != last_logged_step:
                step_log = barrier.get_last_step_log()
                if step_log["actions"]:
                    exp_logger.log_step(
                        step_num=current_step,
                        actions=step_log["actions"],
                        successes=step_log["successes"],
                        coverage=metrics["coverage"],
                        transport_rate=metrics["transport_rate"],
                        finished=metrics["finished"],
                    )
                    last_logged_step = current_step

        elapsed_total = time.time() - start_time
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

        # 如果 RouterAgent 还在运行，等待其完成或取消
        # Wait for router task if still running
        if not router_task.done():
            logger.info("Waiting for RouterAgent to finish...")
            try:
                router_result = await asyncio.wait_for(router_task, timeout=10.0)
                logger.info("RouterAgent finished: %s", router_result[:300] if router_result else "(empty)")
            except asyncio.TimeoutError:
                logger.warning("RouterAgent did not finish in time, cancelling...")
                router_task.cancel()
            except Exception as e:
                logger.error("RouterAgent error: %s", e)

        if barrier.is_finished():
            logger.info(
                "TASK COMPLETED in %.1f seconds, %d steps",
                elapsed_total,
                final_metrics["steps"],
            )
        else:
            logger.warning(
                "TASK TIMEOUT after %.1f seconds, %d steps",
                elapsed_total,
                final_metrics["steps"],
            )

        return final_metrics

    finally:
        # 清理阶段 — 无论实验成功、超时还是异常，finally 保证资源释放
        # 关闭顺序：Worker -> Coordinator -> Barrier -> Logger
        # Cleanup — guaranteed even if coordinator import fails above
        logger.info("Shutting down workers...")
        for name, worker in workers.items():
            worker.stop()
        if coordinator is not None:
            logger.info("Shutting down coordinator...")
            await coordinator.stop()
        logger.info("Shutting down barrier...")
        barrier.stop()

        # 关闭日志记录器，写入汇总统计
        exp_logger.close()
        log_dir = exp_logger.get_log_dir()
        final_metrics["log_dir"] = log_dir
        logger.info("Experiment logs saved to: %s", log_dir)
        logger.info("Cleanup complete")


def _create_worker(
    agent_name: str,
    agent_idx: int,
    barrier: SARBarrier,
    port: int,
    coordinator_url: str,
    model: str,
):
    """SARWorker 工厂函数：延迟导入并创建 SARWorker 实例。
    采用延迟导入（lazy import）的原因是避免在模块加载时产生循环依赖或
    不必要的依赖检查，只有实际创建 Worker 时才加载 SARWorker 类。
    Factory to lazily import and create a SARWorker."""
    from integration.sar_workers.sar_worker import SARWorker

    return SARWorker(
        agent_name=agent_name,
        agent_idx=agent_idx,
        barrier=barrier,
        port=port,
        coordinator_url=coordinator_url,
        model=model,
    )


def main():
    """CLI 入口函数：解析命令行参数，启动并运行 SAR 实验。
    支持的参数包括场景编号（--scene）、智能体数量（--agents）、
    随机种子（--seed）以及 Worker / Coordinator 的模型选择。
    """
    parser = argparse.ArgumentParser(description="MARoS x LLaMAR SAR Experiment")
    parser.add_argument(
        "--scene", type=int, default=1, help="SAR scene number (1-5)"
    )
    parser.add_argument(
        "--agents", type=int, default=2, help="Number of agents (1-6)"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--model",
        type=str,
        default="deepseek-v4-flash",
        help="LLM model for workers",
    )
    parser.add_argument(
        "--coordinator-model",
        type=str,
        default="claude-opus-4-5",
        help="LLM model for coordinator",
    )
    args = parser.parse_args()

    metrics = asyncio.run(
        run_experiment(
            scene=args.scene,
            num_agents=args.agents,
            seed=args.seed,
            model=args.model,
            coordinator_model=args.coordinator_model,
        )
    )

    print("\n" + "=" * 60)
    print("EXPERIMENT RESULTS")
    print("=" * 60)
    print(f"  Finished:       {metrics['finished']}")
    print(f"  Steps:          {metrics['steps']}")
    print(f"  Coverage:       {metrics['coverage']:.2f}")
    print(f"  Transport Rate: {metrics['transport_rate']:.2f}")
    print(f"  Elapsed:        {metrics['elapsed_seconds']:.1f}s")
    if "log_dir" in metrics:
        print(f"  Log Dir:        {metrics['log_dir']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
