"""AI2Thor worker 受限工具集 —— 导出为 ``AI2THOR_WORKER_TOOLS``。

形态对齐 SAR 版（``sar_orch/tools/worker/__init__.py``）：目录型注册表，
EnvPack 的 ``build_worker_tools`` 逐类构造并把运行时依赖
（``barrier`` / ``agent_idx`` / ``alias_registry``）注入——与环境回合屏障
共用同一 ``AliasRegistry`` 实例，保证工具侧 alias→raw 解析与观测脱敏一致。

8 件工具 = 受 visibility 约束的全部原子动作：
``move`` / ``rotate`` / ``look`` / ``navigate`` / ``pickup`` / ``put`` /
``open_close`` / ``done``。
其中 ``done`` 是任务完成工具（``ToolResult(task_complete=True)``）——worker
骨架以 ``require_explicit_completion=True`` 运行，缺它任务无法正常终结
（env_contract.md §2.1 第 2 类）；``navigate`` 是导航宏工具（一次调用 =
一次 Teleport 回合，对齐迁移前 ``NavigateTo(obj)`` 的导航能级，F-nav）。
"""

from ai2thor_orch.tools.worker.done import DoneTool
from ai2thor_orch.tools.worker.look import LookTool
from ai2thor_orch.tools.worker.move import MoveTool
from ai2thor_orch.tools.worker.navigate import NavigateTool
from ai2thor_orch.tools.worker.open_close import OpenCloseTool
from ai2thor_orch.tools.worker.pickup import PickupTool
from ai2thor_orch.tools.worker.put import PutTool
from ai2thor_orch.tools.worker.rotate import RotateTool

AI2THOR_WORKER_TOOLS = [
    MoveTool,
    RotateTool,
    LookTool,
    NavigateTool,
    PickupTool,
    PutTool,
    OpenCloseTool,
    DoneTool,
]

__all__ = [
    "AI2THOR_WORKER_TOOLS",
    "DoneTool",
    "LookTool",
    "MoveTool",
    "NavigateTool",
    "OpenCloseTool",
    "PickupTool",
    "PutTool",
    "RotateTool",
]
