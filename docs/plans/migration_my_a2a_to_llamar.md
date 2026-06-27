---
日期: 2026-06-27
文档类型: 技术方案 / 迁移计划
文档概述: 将 my_a2a 通用的 Coordinator-Worker 多智能体编排框架完整复制到 LLaMAR 项目中，并基于其 SAR（Search and Rescue）环境构建全新的多智能体搜救任务编排系统
---

# my_a2a → LLaMAR SAR 环境迁移方案

## 1. 背景与目标

### 1.1 现状

| 项目 | 路径 | 定位 |
|---|---|---|
| **my_a2a** | `/home/wyh/daily_work/my_a2a/` | 通用多智能体编排框架（47,711 行 Python） |
| **LLaMAR** | `/home/wyh/daily_work/LLaMAR/` | SAR 搜救模拟环境 + MARoS 集成（git repo） |

LLaMAR 当前通过 `PYTHONPATH` 引用外部 `MARoS/my_a2a/src` 来获得编排能力，存在外部依赖耦合。已有的 `integration/` 目录是一个参考实现。

### 1.2 目标

1. **去外部依赖** — 将 my_a2a 源码完整复制到 LLaMAR 内部，不再依赖 `MARoS/my_a2a`
2. **全新 SAR 集成** — 基于复制后的框架，在 LLaMAR 内重新构建 SAR 编排层
3. **Git 版本管理** — LLaMAR 独立维护 git 历史，my_a2a 只作模板参考
4. **模型** — 统一使用 `deepseek-v4-flash`（openai provider，`api_base=https://api.deepseek.com`）

---

## 2. Git 管理策略

```bash
# main 分支保持不变（原有 LLaMAR + 旧 integration）
git checkout -b feat/copy-my-a2a-framework
```

- `main` → 保留原 LLaMAR（不做任何改动）
- `feat/copy-my-a2a-framework` → 新迁移开发分支

---

## 3. 目录结构规划

```
LLaMAR/
├── SAR/                                 # [保留] 原有 SAR 环境引擎
├── integration/                         # [保留] 原有集成（参考用）
│
├── src/                                 # ★ 从 my_a2a 复制
│   ├── a2a/
│   │   ├── __init__.py                  #   命名空间合并
│   │   ├── coordinator/                 #   CoordinatorServer, RouterAgent, agent_executor
│   │   ├── worker/                      #   create_worker_a2a_server, AgentAdapter
│   │   ├── builtin_tools/               #   内置工具（query_workers, dispatch_task 等）
│   │   └── shared/                      #   env_loader, path_config
│   │
│   └── Agent/
│       ├── router_agent/                #   Mini-Agent 框架（Coordinator 使用）
│       └── worker_agent/                #   Mini-Agent 框架（Worker 使用，结构对称）
│
├── sar_orch/                            # ★ 新建：SAR 编排层
│   ├── barrier.py                       #   SARBarrier — 多智能体同步屏障
│   ├── coordinator.py                   #   SARCoordinator
│   ├── worker.py                        #   SARWorker
│   ├── tools/
│   │   ├── coordinator/query_sar_state.py
│   │   └── worker/                      #   10 个 SAR 动作工具
│   ├── prompts/
│   │   ├── coordinator/system.md
│   │   └── worker/system.md
│   ├── experiment.py                    #   实验运行器
│   └── logger.py                        #   实验日志
│
├── scripts/
│   └── run_sar.sh                       #   一键启动脚本
│
├── pyproject.toml                       # [需更新]
├── .gitignore                           # [需更新]
└── .env                                 # [新建] LLM 配置
```

---

## 4. 架构设计

### 4.1 整体数据流

```
experiment.py
    │
    ├── 1. 创建 SARBarrier(场景, 智能体数量, 种子)
    │      └─ 内部创建 SAREnv → env.reset() → 初始观测
    │
    ├── 2. 创建 N 个 SARWorker
    │      ├─ 包装 create_worker_a2a_server()
    │      ├─ --tools-dir → 10 个 SAR 工具
    │      ├─ --prompts-dir → SAR system.md
    │      └─ 启动 A2A HTTP 服务器 (端口 8191~8196)
    │
    ├── 3. 创建 SARCoordinator
    │      ├─ 包装 CoordinatorServer + RouterAgent
    │      ├─ --tools-dir → QuerySARState tool
    │      ├─ --orchestration-mode agentic
    │      └─ 启动 FastAPI 服务 (端口 8080 + 8081)
    │
    └── 4. 提交总任务 → 轮询完成 → 记录结果
```

### 4.2 同步屏障设计（SARBarrier）

- 包装 LLaMAR 的 `SAREnv`
- Worker 通过 `submit_action(agent_idx, action)` 提交动作 → 阻塞等待
- 所有 N 个 Worker 到齐 → `env.step(actions)` → 生成观测 → 唤醒所有等待者
- 30s 超时 → 缺失智能体自动填充 `NoOp`

### 4.3 Worker Tool 设计（10 个）

| 工具 | 对应 LLaMAR 动作 |
|---|---|
| `navigate_to` | `NavigateTo(target)` |
| `move` | `Move(direction)` |
| `explore` | `Explore()` |
| `carry_person` | `Carry(person)` |
| `drop_off_person` | `DropOff(deposit, person)` |
| `get_supply` | `GetSupply(source)` |
| `store_supply` | `StoreSupply(deposit)` |
| `use_supply` | `UseSupply(fire, type)` |
| `clear_inventory` | `ClearInventory()` |
| `no_op` | `NoOp()` |

### 4.4 Coordinator Tool

- `query_sar_state` → 查询 SARBarrier 全局快照（火势、人员、智能体位置）

---

## 5. 需要修改/适配的文件

| 文件 | 改动 |
|---|---|
| `src/a2a/__init__.py` | 命名空间合并逻辑适配新路径 |
| `src/a2a/shared/path_config.py` | 默认路径指向 LLaMAR 目录结构 |
| `src/a2a/coordinator/server.py` | `create_server()` 默认路径 |
| `src/a2a/worker/a2a_server.py` | `create_worker_a2a_server()` 默认路径 |
| `pyproject.toml` | 添加 a2a-sdk 依赖 + sar_orch 包声明 |
| `.env` | 新建 — provider/model/api_base 配置 |

---

## 6. 实施步骤

| # | 内容 |
|---|---|
| 1 | Git 准备 — 创建 feature 分支，更新 .gitignore |
| 2 | 复制 my_a2a 核心源码到 LLaMAR/src/ |
| 3 | 适配导入路径 |
| 4 | 验证基础框架可运行 |
| 5 | 创建 SARBarrier |
| 6 | 创建 10 个 Worker SAR 工具 |
| 7 | 创建 Coordinator QuerySARState 工具 |
| 8 | 编写 SAR 专用 Prompts |
| 9 | 创建 SARWorker + SARCoordinator + experiment.py |
| 10 | 更新 pyproject.toml + 创建启动脚本 |
| 11 | 全流程验证 |

---

## 7. 关键风险

| 风险 | 缓解 |
|---|---|
| a2a-sdk 命名空间合并 | 确认 a2a-sdk 已在 venv 中 |
| Mini-Agent 双副本同步 | 复制后统一验证两边 |
| protobuf monkey-patch | 确认 patch 代码保留 |
| 系统代理拦截 | 始终附带 `no_proxy="localhost,127.0.0.1"` |
| .env 字段大小写 | 严格小写 |
| SAREnv step() 同步阻塞 | 使用 `asyncio.to_thread()` 在线程中执行 |
