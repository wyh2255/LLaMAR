"""环境变量文件 (.env) 加载器。

支持两种格式：
- `key: value`   （当前项目的 .env 格式）
- `KEY=VALUE`    （标准 dotenv 格式）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


def load_env_file(path: str | Path = ".env") -> Dict[str, str]:
    """加载 .env 文件，返回 {key: value} dict。

    同时支持两种格式（按行自动检测）：
        key: value      # YAML 风格
        KEY=VALUE       # 标准 dotenv 风格

    Args:
        path: .env 文件路径，默认 "./.env"

    Returns:
        配置字典。文件不存在、无法读取或格式错误时返回空 dict。
        不会抛异常。
    """
    env_path = Path(path)
    if not env_path.exists():
        logger.debug(".env file not found at %s, skipping", env_path.resolve())
        return {}
    if not env_path.is_file():
        logger.warning(".env path %s is not a file, skipping", env_path.resolve())
        return {}

    config: Dict[str, str] = {}

    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read .env file %s: %s", env_path.resolve(), e)
        return {}

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        # 跳过空行和注释
        if not line or line.startswith("#"):
            continue

        # 尝试 KEY=VALUE 格式（标准 dotenv）— 优先检查，避免 URL 中的 : 干扰
        if "=" in line:
            parts = line.split("=", 1)
            key = parts[0].strip()
            value = parts[1].strip()
            if key:
                config[key] = value
                continue

        # 尝试 key: value 格式
        if ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip()
            value = parts[1].strip()
            if key:
                config[key] = value
                continue

        logger.debug("Skipping unparseable .env line %d: %s", line_no, raw_line)

    return config
