import sys
from pathlib import Path

# 确保 src/ 在 Python 路径中
SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


# ToolResult 在 worker_agent 和 router_agent 中各有一份完全相同的拷贝
# 为了测试简单，我们直接构造 ToolResult(success=..., content=...)
# 而不用导入具体的 Tool 基类（它依赖外部 LLM 客户端）
def make_tool_result(success: bool, content: str = "") -> object:
    from Agent.worker_agent.tools.base import ToolResult

    return ToolResult(success=success, content=content)
