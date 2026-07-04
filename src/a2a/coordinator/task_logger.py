"""TaskLogger - NDJSON 文件日志记录器，用于记录 DAG 任务事件。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc

logger = logging.getLogger(__name__)

# 正则：仅允许字母数字、下划线、短横线、点
_VALID_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# 各类字段截断上限
_MAX_LLM_RESPONSE_CONTENT = 2000
_MAX_TOOL_RESULT_CONTENT = 3000
_MAX_TOOL_ARGUMENTS_LENGTH = 1000

# 单文件最大 10MB
_MAX_LOG_FILE_SIZE = 10 * 1024 * 1024


def _truncate_string(value: str, max_len: int) -> str:
    """截断字符串到指定长度，超过时添加省略标记。"""
    if len(value) > max_len:
        return value[:max_len] + "...[truncated]"
    return value


class TaskLogger:
    """NDJSON 任务事件日志记录器。

    以 NDJSON 格式将任务事件写入文件，每个 task_id 对应独立的日志文件。
    日志目录按 task_id 首字符自动分片（.data/a/…, .data/b/…）。

    OSError 时静默降级，不抛异常影响主流程。
    """

    def __init__(self, base_dir: str = "logs") -> None:
        self._base_dir = Path(base_dir)
        self._name_map: dict[str, str] = {}  # safe_filename → task_id
        # 自动创建日志目录
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _raw_write(self, safe_id: str, data: dict) -> None:
        log_path = self._base_dir / f"{safe_id}.ndjson"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def init_task(self, task_id: str, friendly_name: str | None = None) -> None:
        if friendly_name:
            try:
                safe_name = self._sanitize_task_id(friendly_name)
            except ValueError:
                safe_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        else:
            safe_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._name_map[safe_name] = task_id
        self._raw_write(
            safe_name,
            {
                "event": "meta",
                "task_id": task_id,
                "friendly_name": friendly_name or safe_name,
            },
        )

    def _sanitize_task_id(self, task_id: str) -> str:
        """校验 task_id 合法性，拒绝路径遍历字符。

        Raises:
            ValueError: task_id 包含非法字符
        """
        if not task_id:
            raise ValueError("task_id cannot be empty")
        if task_id == "." or task_id == "..":
            raise ValueError(f"task_id contains path traversal characters: {task_id!r}")
        if "/" in task_id or "\\" in task_id:
            raise ValueError(f"task_id contains path traversal characters: {task_id!r}")
        if not _VALID_TASK_ID_RE.match(task_id):
            raise ValueError(f"task_id contains invalid characters: {task_id!r}")
        return task_id

    def get_log_path(self, task_id: str) -> str:
        """返回日志文件的完整路径。

        如果 task_id 对应一个已注册的友好名称，则返回那名称对应的路径。
        """
        for safe_name, known_task_id in self._name_map.items():
            if known_task_id == task_id:
                return str(self._base_dir / f"{safe_name}.ndjson")
        safe_id = self._sanitize_task_id(task_id)
        return str(self._base_dir / f"{safe_id}.ndjson")

    def log_exists(self, task_id: str) -> bool:
        """检查日志文件是否存在。"""
        return Path(self.get_log_path(task_id)).exists()

    def log_event(
        self,
        task_id: str,
        event_type: str,
        data: dict | None = None,
        source: str | None = None,
    ) -> None:
        """追加一行 NDJSON 日志。

        自动添加 timestamp 字段。对 llm_response、tool_result、tool_arguments
        等字段按阈值自动截断。文件超过 10MB 时静默跳过。

        Args:
            task_id: 任务 ID
            event_type: 事件类型（如 task_start、plan、layer_start 等）
            data: 事件数据字典（可选）
            source: 事件来源（executor/router/worker/verifier）
        """
        try:
            log_path_str = self.get_log_path(task_id)
        except ValueError:
            return

        log_path = Path(log_path_str)

        # 检查文件大小
        try:
            if log_path.exists() and log_path.stat().st_size > _MAX_LOG_FILE_SIZE:
                logger.warning("Log file %s exceeds 10MB, skipping write", log_path)
                return
        except OSError:
            return

        # 构造日志条目
        entry: dict = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "event": event_type,
        }
        if source:
            entry["source"] = source

        if data:
            # 深度复制 data，避免修改传入的字典
            safe_data = dict(data)

            # 按事件类型和字段名自动截断
            if event_type == "llm_response":
                if "content" in safe_data and isinstance(safe_data["content"], str):
                    safe_data["content"] = _truncate_string(
                        safe_data["content"], _MAX_LLM_RESPONSE_CONTENT
                    )

            if event_type == "tool_result":
                if "content" in safe_data and isinstance(safe_data["content"], str):
                    safe_data["content"] = _truncate_string(
                        safe_data["content"], _MAX_TOOL_RESULT_CONTENT
                    )

            if "tool_arguments" in safe_data:
                try:
                    args_str = json.dumps(
                        safe_data["tool_arguments"], ensure_ascii=False
                    )
                except TypeError:
                    args_str = str(safe_data["tool_arguments"])
                safe_data["tool_arguments"] = _truncate_string(
                    args_str, _MAX_TOOL_ARGUMENTS_LENGTH
                )

            entry["data"] = safe_data

        # 写入 NDJSON
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError):
            pass

    def cleanup_task(self, task_id: str) -> None:
        """删除指定 task_id 的日志文件。"""
        try:
            log_path = Path(self.get_log_path(task_id))
            if log_path.exists():
                log_path.unlink()
        except (ValueError, OSError):
            pass

    def list_task_ids(self) -> list[str]:
        """返回所有日志文件的 task_id 列表，按 mtime 倒序排列。

        Returns:
            按最后修改时间倒序排列的 task_id 列表
        """
        try:
            entries: list[tuple[str, float]] = []
            for f in self._base_dir.iterdir():
                if f.suffix == ".ndjson" and f.is_file():
                    task_id = f.stem
                    # 校验 task_id 格式
                    if _VALID_TASK_ID_RE.match(task_id):
                        mtime = f.stat().st_mtime
                        entries.append((task_id, mtime))
            # 按 mtime 倒序
            entries.sort(key=lambda x: x[1], reverse=True)
            return [task_id for task_id, _ in entries]
        except OSError:
            return []
