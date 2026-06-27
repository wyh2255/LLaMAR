---
日期: 2026-06-21
文档类型: 环境配置指南
文档概述: LLaMAR integration/ 目录的 Python 环境配置步骤，包含 uv 安装依赖和 PYTHONPATH 配置
---

# 集成环境配置指南

## 快速开始（pyproject.toml 方式）

```bash
# 1. 创建虚拟环境（使用 uv）
uv venv .venv-integration
source .venv-integration/bin/activate

# 2. 安装依赖（pyproject.toml 自动处理 mini-agent git 依赖）
uv sync

# 3. 设置 PYTHONPATH（本地 MARoS 包）
export PYTHONPATH="${PWD}:${PWD}/SAR:/home/wyh/daily_work/MARoS/my_a2a/src:/home/wyh/daily_work/MARoS/maros_ws/a2a_lib:${PYTHONPATH}"

# 4. 运行测试
python -m pytest integration/test_sar_coordinator.py -v
```

## 备选：requirements.txt 方式

```bash
uv pip install -r requirements-integration.txt
uv pip install "mini-agent @ git+https://github.com/wyh2255/Mini-Agent.git@feature/task-context"
```

## 所需路径

| 路径 | 用途 |
|------|------|
| `LLaMAR/` | 项目根目录，PYTHONPATH 需要 |
| `LLaMAR/SAR/` | SAR 环境模块（`from env import SAREnv`） |
| `MARoS/my_a2a/src/` | `openharness-a2a` 包（CoordinatorServer, RouterAgent） |
| `MARoS/maros_ws/a2a_lib/` | `a2a_lib` 包（A2A 传输层, Skill, @tool） |

## 测试分层

| 层级 | 命令 | 依赖 | 耗时 |
|------|------|------|------|
| Layer 1（静态） | `pytest integration/test_sar_coordinator.py -v -k "not start_stop"` | `minimal` | ~2s |
| Layer 2（集成） | `pytest integration/test_sar_coordinator.py -v -k "start_stop"` | 需要 uvicorn + MARoS | ~5s |
| 回归 | `pytest integration/test_sar_barrier.py integration/test_integration.py -v` | 需要 opencv + numpy | ~2s |

## Python 版本要求

- Python >= 3.10（MARoS 和 uv 都要求 3.10+）
