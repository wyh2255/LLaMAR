"""SAR Worker —— SAR 环境包装配薄壳（env-contract P4-3）。

P4-3 起，通用装配/运维逻辑在 ``orchestration.worker.OrchestratorWorker``；本模块
只保留 SAR 特化面：

- ``SARWorker``：组装 ``SAREnvPack`` 后转调通用骨架（公开构造签名逐参不变）；
- ``ConfigurationError`` / ``MIN_COORDINATOR_SECRET_LENGTH`` re-export（既有
  ``from sar_orch.worker import ...`` 面零变化）；
- 环境文件定位：仓库根 ``.env``（经 ``env_file`` 显式传入通用骨架——装配层提供
  凭据发现面，通用骨架不猜测路径）。

worker 侧 SAR 特化件（工具注册表 / state provider / 观测发布器 / 能力标签 /
Action 格式化）在 ``sar_orch.env_pack.SAREnvPack``（P4-3 自本模块迁入）。
"""

from __future__ import annotations

from pathlib import Path

from orchestration.worker import (
    MIN_COORDINATOR_SECRET_LENGTH,
    ConfigurationError,
    OrchestratorWorker,
)
from sar_orch.env_pack import SAREnvPack

__all__ = [
    "MIN_COORDINATOR_SECRET_LENGTH",
    "ConfigurationError",
    "SARWorker",
]


class SARWorker(OrchestratorWorker):
    """SAR Worker — ``SAREnvPack`` 装配的通用编排骨架 worker 实例。

    与 P4-3 前兼容的构造面：``prompts_dir`` 进 ``SAREnvPack``
    （``worker_prompts_dir``），其余参数直通 ``OrchestratorWorker``；构造期校验
    （secret 门槛 / peer-mail 配置）与运行期行为由通用骨架提供，逐字沿用迁移前实现。
    """

    def __init__(
        self,
        worker_id: str,
        agent_name: str,
        agent_idx: int,
        barrier,  # SARBarrier instance
        a2a_host: str = "0.0.0.0",
        a2a_port: int = 8191,
        coordinator_url: str = "ws://localhost:8080",
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        prompts_dir: str | None = None,
        log_dir: str | None = None,
        exp_logger=None,  # ExperimentLogger for agent_interactions.csv
        sandbox_policy=None,  # SandboxPolicy for workspace sandboxing
        # Phase 2/3: peer mail
        enable_peer_mail: bool = False,
        coordinator_secret: bytes | None = None,
        # Phase 2: secure callback signing mode; default read_port since H3
        # retirement approval (2026-08-10).
        memory_read_mode: str = "read_port",
        # P1 cache optimization: history pruning policy
        # (``count_window`` | ``prefix_stable``).  The default keeps the
        # legacy count-based sliding window byte-for-byte unchanged;
        # ``prefix_stable`` opts into the append-only discipline
        # (.agents/context-prefix-stability.md).
        prune_policy: str = "count_window",
    ):
        super().__init__(
            env_pack=SAREnvPack(worker_prompts_dir=prompts_dir),
            worker_id=worker_id,
            agent_name=agent_name,
            agent_idx=agent_idx,
            barrier=barrier,
            a2a_host=a2a_host,
            a2a_port=a2a_port,
            coordinator_url=coordinator_url,
            model=model,
            provider=provider,
            api_base=api_base,
            api_key_env=api_key_env,
            log_dir=log_dir,
            exp_logger=exp_logger,
            sandbox_policy=sandbox_policy,
            enable_peer_mail=enable_peer_mail,
            coordinator_secret=coordinator_secret,
            memory_read_mode=memory_read_mode,
            prune_policy=prune_policy,
            # 现状 .env 位置：仓库根（本文件上两级）——迁移前 start() 内就地推导，
            # P4-3 起由薄壳显式提供给通用骨架。
            env_file=Path(__file__).parent.parent / ".env",
        )
