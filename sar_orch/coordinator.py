"""SAR Coordinator —— SAR 环境包装配薄壳（env-contract P4-2）。

P4-2 起，通用装配/运维逻辑在 ``orchestration.coordinator.OrchestratorCoordinator``；
本模块只保留 SAR 特化面：

- ``SARCoordinator``：组装 ``SAREnvPack`` 后转调通用骨架（公开签名逐参不变）；
- 内核注入口直通工厂的兼容 re-export：``build_finish_task_tool`` /
  ``build_environment_state_provider`` / ``build_map_agent_session_lifecycle``
  （实现迁至 ``sar_orch.env_pack``，既有 ``from sar_orch.coordinator import ...``
  面零变化，identity 保持不变）；
- ``UserCommandQueue`` re-export（类迁至 ``orchestration.user_command_queue``）；
- ``_validate_long_term_mode_combo`` re-export（迁至 ``orchestration.coordinator``）；
- ``_semantic_map`` 只读别名：SAR 侧读者（``experiment.py`` /
  ``launch_dashboard.py``）在 P4-2 前直接访问该私有属性，现指向通用骨架的
  ``_observation_source``。
"""

from __future__ import annotations

from pathlib import Path

from orchestration.coordinator import (
    OrchestratorCoordinator,
    _validate_long_term_mode_combo,
)
from orchestration.user_command_queue import UserCommandQueue
from sar_orch.env_pack import (
    SAREnvPack,
    build_environment_state_provider,
    build_finish_task_tool,
    build_map_agent_session_lifecycle,
)

__all__ = [
    "SARCoordinator",
    "SAREnvPack",
    "UserCommandQueue",
    "_validate_long_term_mode_combo",
    "build_environment_state_provider",
    "build_finish_task_tool",
    "build_map_agent_session_lifecycle",
]


class SARCoordinator(OrchestratorCoordinator):
    """SAR Coordinator — ``SAREnvPack`` 装配的通用编排骨架实例。

    与 P4-2 前兼容的构造面：``prompts_dir`` / ``map_summary_path`` 进
    ``SAREnvPack``（``coordinator_prompts_dir`` / ``map_summary_path``），其余
    参数直通 ``OrchestratorCoordinator``；构造期校验（模式组合 fail-closed、
    secret 门槛）与运行期行为由通用骨架提供，逐字沿用迁移前实现。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        barrier=None,  # SARBarrier instance
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        prompts_dir: str | None = None,
        log_dir: str | None = None,
        supervision_dir: str | None = None,
        orchestration_mode: str = "agentic",
        exp_logger=None,
        sandbox_policy=None,
        state_mode: str = "semantic",
        enable_peer_mail: bool = False,
        coordinator_secret: bytes | None = None,
        max_steps: int = 50,
        map_summary_path: str | Path | None = None,
        # Phase 2: authenticated Temporal shadow write; default read_port
        # since H3 retirement approval (2026-08-10).
        memory_read_mode: str = "read_port",
        # P1 cache optimization: history pruning policy
        # (``count_window`` | ``prefix_stable``) for the coordinator context.
        prune_policy: str = "count_window",
        run_id: str | None = None,
        # Phase 4: run-local long-term memory mode (off|shadow|read).
        long_term_mode: str = "off",
        # Phase 4 (P4): optional ``[diagnosis]`` tunables from
        # ``long_term.config`` (DiagnosisRuntimeConfig).  ``None`` keeps the
        # frozen DiagnosisConfig defaults.
        diagnosis_tunables=None,
    ):
        super().__init__(
            env_pack=SAREnvPack(
                coordinator_prompts_dir=prompts_dir,
                map_summary_path=map_summary_path,
            ),
            host=host,
            port=port,
            a2a_port=a2a_port,
            barrier=barrier,
            model=model,
            provider=provider,
            api_base=api_base,
            api_key_env=api_key_env,
            log_dir=log_dir,
            supervision_dir=supervision_dir,
            orchestration_mode=orchestration_mode,
            exp_logger=exp_logger,
            sandbox_policy=sandbox_policy,
            state_mode=state_mode,
            enable_peer_mail=enable_peer_mail,
            coordinator_secret=coordinator_secret,
            max_steps=max_steps,
            memory_read_mode=memory_read_mode,
            prune_policy=prune_policy,
            run_id=run_id,
            long_term_mode=long_term_mode,
            diagnosis_tunables=diagnosis_tunables,
        )

    @property
    def _semantic_map(self):
        """SAR 兼容别名：观测源（``SemanticMapStore``），``start()`` 前为 None。

        SAR 侧读者在 P4-2 前直接访问本属性（``experiment.py`` 的
        ``set_jsonl_path`` / ``map_recall`` / ``freshness`` /
        ``update_step_budget``，``launch_dashboard.py`` 的
        ``update_step_budget``）；P4-2 起其指向通用骨架的 ``_observation_source``。
        """
        return self._observation_source
