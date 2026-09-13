"""通用编排层（env-contract P4）。

内容：

- ``orchestration.env_pack``：环境包契约（``EnvPack`` ABC + 工厂签名 + 依赖上下文）。
- ``orchestration.coordinator``：通用 Coordinator 骨架（环境经 EnvPack 注入）。
- ``orchestration.user_command_queue``：线程安全用户指令队列（console 注入链）。

依赖方向（硬不变量）：本包只依赖内核 ``src/Agent`` + ``src/a2a`` 与标准库/第三方库；
禁止 import ``sar_orch`` / ``ai2thor_orch``（守卫测试
``tests/test_orchestration_dependency_direction.py``）。
"""
