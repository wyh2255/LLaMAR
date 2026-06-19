---
日期: 2026-06-19
文档类型: 阅读指南
文档概述: MARoS × LLaMAR SAR 集成代码阅读指南，按数据流顺序梳理入口与核心文件
---

# MARoS × LLaMAR SAR 集成 — 代码阅读指南

## 入口

```
experiment.py:main()
    └── argparse 解析 CLI 参数
        └── asyncio.run(run_experiment())
```

```bash
# 启动一次完整实验
python3 integration/experiment.py --scene=1 --agents=2 --seed=42
```

## 阅读顺序（按数据流，不是按 Task 编号）

```
1. experiment.py       ~170 行  入口：启动 → 轮询 → 收尾
     │
     ├── 创建 barrier ──────────────────────────────────────┐
     │                                                      ▼
2.   │              sar_barrier.py   ~200 行  核心：同步屏障 + env.step()
     │                ├── 内部创建 SAREnv
     │                ├── submit_action() 收集所有 agent 的 action
     │                └── _execute_step() 统一执行 + 广播 obs
     │
     ├── 创建 N 个 worker ──────────────────────────────────┐
     │                                                      ▼
3.   │              sar_workers/sar_worker.py  ~170 行  Agent：A2A HTTP + LLM ReAct
     │                ├── 绑定 10 个 SAR tool
     │                ├── 启动 A2A HTTP server（复用 MARoS transport.py）
     │                └── 动态 system prompt（注入最新观测）
     │
     │   每个 tool 定义在 ──────────────────────────────────┐
     │                                                      ▼
4.   │              sar_workers/tools.py  ~100 行  动作空间：10 个 @tool
     │                └── 每个 tool: 格式化 LLaMAR action string
     │                   → barrier.submit_action() → 返回 obs 文本
     │
     │   每个 tool 的能力声明在 ──────────────────────────────┐
     │                                                      ▼
     │              sar_workers/skills.py  ~60 行  能力组合
     │                └── 4 个 Skill: firefighting / rescue / supply_chain / exploration
     │
     ├── 创建 coordinator ──────────────────────────────────┐
     │                                                      ▼
5.   │              coordinator/sar_coordinator.py  ~300 行  协调者：RouterAgent
     │                └── SAR 专用 system prompt
     │                   → 分解任务 → push_task 到各 worker
     │                   → 监控完成 → 重新分配
     │
     │   coordinator 的查询工具 ─────────────────────────────┐
     │                                                      ▼
6.   │              coordinator/sar_router_tools.py  ~75 行
     │                └── query_sar_state: 直接读 barrier.get_env_snapshot()
     │
     └── 轮询 barrier.is_finished() / barrier.get_metrics()
```

## 核心阅读优先级

| 优先级 | 文件 | 为什么先读 | 关键方法 |
|--------|------|-----------|---------|
| **P0** | `integration/sar_barrier.py` | 整个集成的核心——理解同步屏障就理解了一切 | `submit_action()`, `_execute_step()` |
| **P0** | `integration/sar_workers/tools.py` | 每个 tool 只有 3-5 行，但定义了 agent 能做什么 | 任意一个 `@tool` 函数 |
| **P1** | `integration/experiment.py` | 看启动顺序和轮询逻辑，串联全局 | `run_experiment()`, `main()` |
| **P1** | `integration/sar_workers/sar_worker.py` | 理解 agent 如何启动 A2A server + 绑定 tool | `__init__()`, `start()` |
| **P2** | `integration/sar_workers/skills.py` | 4 个 Skill 定义，如何组合 tool | `FIREFIGHTING_SKILL` 等 |
| **P2** | `integration/coordinator/sar_coordinator.py` | Coordinator 在任务层面工作，不影响 step 循环 | `SARRouterAgent`, `SARCoordinator.start()` |
| **P2** | `integration/coordinator/sar_router_tools.py` | Coordinator 如何查询环境状态 | `QuerySARStateTool.execute()` |
| **P2** | `integration/sar_workers/prompt.md` | SAR agent 的 system prompt，LLM 看到的领域知识 | 全文 |

## 一个 Step 的数据流（核心）

```
Step N:
  ┌─────────────────────────────────────────────────────────┐
  │ 1. 每个 Worker 的 LLM 独立推理                            │
  │    Alice:   "我看到前方有火源，我需要取水"                  │
  │    Bob:     "我在水源旁，我应该取水然后送给 Alice"           │
  │    Charlie: "我在受困者旁边，等待另一个 agent 来帮忙抬人"    │
  │                                                         │
  │ 2. 每个 Worker 调用 tool → barrier.submit_action()       │
  │    Alice:   NavigateTo(WaterSource_1)                    │
  │    Bob:     GetSupply(WaterSource_1, Water)              │
  │    Charlie: NoOp                                        │
  │                                                         │
  │ 3. Barrier 收集齐所有 action → env.step([...])            │
  │    - Alice 移动到 WaterSource_1 ✓                        │
  │    - Bob 从 WaterSource_1 取水 ✓                         │
  │    - Charlie 等待 ✓                                      │
  │                                                         │
  │ 4. Barrier 解析 obs，广播给各 Worker                       │
  │    Alice:   "我在 WaterSource_1，周围有..."               │
  │    Bob:     "我在 WaterSource_1，背包: [Water×1]"         │
  │    Charlie: "我在 LostTimmy 旁，状态不变"                  │
  └─────────────────────────────────────────────────────────┘

Step N+1:
  ┌─────────────────────────────────────────────────────────┐
  │ 1. LLM 根据新 obs 决定下一个 action                        │
  │    Alice:   GetSupply(WaterSource_1, Water)              │
  │    Bob:     NavigateTo(GreatFire_Region_1)               │
  │    Charlie: (Coordinator 通知 Bob 已灭火, 改去抬人)        │
  │                                                         │
  │ 2. 提交 → Barrier → env.step() → 广播 obs                │
  └─────────────────────────────────────────────────────────┘

... 循环直到 checker.check_success() == True 或超时
```

## LLaMAR 端（参考即可，无需逐行阅读）

| 文件 | 我们用的部分 |
|------|-------------|
| `SAR/env.py` | `SAREnv` — 被 SARBarrier 直接实例化，`step()` 方法执行所有 agent 的 action |
| `SAR/core.py` | `Controller`、`Field`、`GridEngine`、所有对象类（`Fire`、`Person`、`Reservoir` 等） |
| `SAR/base_env.py` | `SARBaseEnv` — `parse_action()` 解析 action string 为 Controller 可执行的格式 |
| `SAR/Scenes/` | 场景定义（scene_1 到 scene_5）和 `checker.py` 任务完成度检查 |

## MARoS 端（参考即可，无需逐行阅读）

| 文件 | 我们用的部分 |
|------|-------------|
| `maros_ws/a2a_lib/a2a_lib/transport.py` | `C2FixedMiniAgentAdapter` + `start_a2a_transport()` — A2A HTTP server |
| `maros_ws/a2a_lib/a2a_lib/tool_decorator.py` | `@tool` 装饰器 — 把 Python 函数变成 LLM-callable tool |
| `my_a2a/src/openharness_a2a/coordinator/` | `RouterAgent`、`CoordinatorServer`、`CoordinatorAgentExecutor` |

## 阅读建议

**从这两个方法开始读：**

1. `integration/sar_barrier.py` 的 `submit_action()` — 看一个 agent 如何提交 action 并等待
2. `integration/sar_barrier.py` 的 `_execute_step()` — 看所有 action 如何被统一执行

这两个方法就把整个数据流说清楚了。然后看任意一个 `@tool` 函数（比如 `tools.py` 里的 `navigate_to`）理解 Worker 端的行为模式，最后看 `experiment.py` 的 `run_experiment()` 理解启动顺序。
