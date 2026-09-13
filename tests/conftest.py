import sys
from pathlib import Path

# 确保 src/ 在 Python 路径中（且必须排在 site-packages 之前）。
# 某些 venv 的 .pth 会把项目根与 src 追加到 sys.path 末尾，导致 site-packages
# 里的 a2a-sdk 包遮蔽项目自身的 src/a2a（coordinator/worker/shared 子包会
# 因此不可导入）。无条件把 SRC_DIR 移到最前，保证项目 a2a 优先，SDK 子包
# （server/types 等）由 src/a2a/__init__.py 的 __path__ 扩展合并。
SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR in sys.path:
    sys.path.remove(SRC_DIR)
sys.path.insert(0, SRC_DIR)


# ToolResult 在 worker_agent 和 router_agent 中各有一份完全相同的拷贝
# 为了测试简单，我们直接构造 ToolResult(success=..., content=...)
# 而不用导入具体的 Tool 基类（它依赖外部 LLM 客户端）
def make_tool_result(success: bool, content: str = "") -> object:
    from Agent.worker_agent.tools.base import ToolResult

    return ToolResult(success=success, content=content)
