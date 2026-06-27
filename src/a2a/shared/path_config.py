"""外部资源路径配置加载器。

从 src/config/config.yaml 加载 prompts_dir / tools_dir / skills_dir 默认值，
相对路径按项目根目录解析为绝对路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class PathConfig:
    prompts_dir: Path | None
    tools_dir: Path | None
    skills_dir: Path | None
    log_dir: Path | None


def _find_project_root(config_path: Path) -> Path:
    """从 config.yaml 所在位置向上查找项目根目录（含 pyproject.toml）。"""
    for parent in config_path.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return config_path.parent


def load_path_config(config_path: Path, section: str) -> PathConfig:
    """加载 config.yaml 中指定 section 的路径配置。

    相对路径按项目根目录（含 pyproject.toml 的目录）解析。
    配置文件中不存在或值为 null 的键返回 None。
    config.yaml 文件本身不存在时，所有值均为 None。

    Args:
        config_path: config.yaml 的文件路径（如 src/config/config.yaml）
        section: 配置 section 名（"coordinator" 或 "worker"）

    Returns:
        PathConfig 实例，路径已解析为绝对路径
    """
    defaults: dict[str, str | None] = {
        "prompts_dir": None,
        "tools_dir": None,
        "skills_dir": None,
        "log_dir": None,
    }

    if not config_path.is_file():
        return PathConfig(**defaults)

    project_root = _find_project_root(config_path)

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return PathConfig(**defaults)

    section_data = data.get(section, {}) or {}
    for key in ("prompts_dir", "tools_dir", "skills_dir", "log_dir"):
        rel_path = section_data.get(key)
        if rel_path:
            defaults[key] = (project_root / rel_path).resolve()

    return PathConfig(**defaults)
