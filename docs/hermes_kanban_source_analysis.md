# Hermes Kanban 源码分析报告

> 目的：给后续 Agent 和 LLaMAR_UI 集成工作提供一份“先看哪里、数据怎么流、状态如何变化、UI 需要拿什么数据”的源码地图。
>
> 事实范围：本报告以本地 Hermes 源码快照为准，不把设计推测写成代码事实。涉及未来 UI 的部分统一标记为“集成建议”。

## 0. 30 秒摘要

Hermes Kanban 不是一个独立的远程队列服务，而是一个以 SQLite 为共享协调总线的多 profile 任务系统：

1. 所有 profile 共享同一个 Hermes 根目录下的 Kanban 数据库；默认路径是 `<Hermes 根>/kanban.db`。
2. 任务、依赖边、评论、事件、运行尝试、附件、通知订阅都落在同一套 `kanban_db` 数据模型中。
3. Gateway 默认内嵌 Dispatcher。Dispatcher 周期性执行：回收过期/崩溃/超时任务 → 重新计算依赖就绪状态 → CAS 认领 `ready` 任务 → 启动对应 profile 的 worker 子进程。
4. Dispatcher worker 通过 `HERMES_KANBAN_TASK` 等环境变量绑定到一个运行任务；`complete/block/heartbeat/attach` 等生命周期写操作有当前任务 ownership guard，但评论、创建、链接和部分读取工具仍有更宽的跨任务语义。
5. 并行不是由某个“并行 API”完成，而是由 DAG 的 `task_links` 表表达：无未完成 parent 的多个任务同时处于 `ready`，Dispatcher 在并发上限允许时分别 claim、spawn。
6. 集成建议：Dashboard Plugin、CLI、Gateway `/kanban`、Worker 工具都复用 `hermes_cli.kanban_db`；LLaMAR_UI 不要另造一套状态机。
7. Dashboard 的实时刷新有两条不同链路：浏览器直接通过 Plugin `/events` WebSocket tail `task_events`；消息平台通知则由 Gateway notifier 读取 `kanban_notify_subs` 后推送到原始聊天。

核心调用链：

```text
CLI / Gateway / Dashboard / Worker tool
                │
                ▼
      hermes_cli.kanban_db.connect()
                │
                ▼
     SQLite WAL + BEGIN IMMEDIATE + CAS
                │
     ┌──────────┴──────────┐
     │                     │
 task/link/event/run   Dispatcher tick
                           │
                           ▼
                     claim ready task
                           │
                           ▼
                  _default_spawn()
                           │
                           ▼
              hermes -p <profile> chat -q
                           │
                           ▼
                 Worker kanban_* tools
                           │
                           ▼
            complete/block/heartbeat/event
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
     next dependent task             UI/消息通知
       recompute_ready()        WebSocket / notifier
```

## 1. 源码快照与阅读边界

### 1.1 本次核查的源码

| 项目 | 值 |
|---|---|
| Hermes 源码根目录 | `/home/wyh/.hermes/hermes-agent` |
| 分支 | `main` |
| Git revision | `78f38e79cfb501c5c4db8cf2cc7593f01e01fb48` |
| 目标报告 | `/home/wyh/daily_work/LLaMAR_UI/docs/hermes_kanban_source_analysis.md` |
| 主要项目 | `/home/wyh/daily_work/LLaMAR_UI`，分支 `feat/ui` |

源码工作树当前有 `hermes_cli/runtime_provider.py`、`hermes_cli/setup.py` 和 `.install_method` 的未提交变化；本报告引用的 Kanban 核心文件不在这三个变化之内。行号均对应上述 revision 的当前工作树，源码变化后应重新定位。

### 1.2 最小定位清单

| 想了解的问题 | 首先打开的文件/位置 |
|---|---|
| console script 从哪里进入 Hermes | `pyproject.toml:307-311`，`hermes = "hermes_cli.main:main"` |
| CLI 从哪里进入 Kanban | `hermes_cli/main.py:4354-4358`、`:13489-13494` |
| CLI 有哪些子命令 | `hermes_cli/kanban.py:193-884` |
| CLI 如何执行子命令、slash 如何复用 | `hermes_cli/kanban.py:891-1007`、`:2873-2948` |
| 数据库路径、board、schema | `hermes_cli/kanban_db.py:1-69`、`:332-710`、`:1096-1277` |
| SQLite 初始化和事务并发 | `hermes_cli/kanban_db.py:1682-2050`、`:2275-2344` |
| 任务创建和依赖 | `hermes_cli/kanban_db.py:2387-2708`、`:2814-2901` |
| claim、ready、heartbeat | `hermes_cli/kanban_db.py:3393-3710` |
| stale/crash/timeout/retry | `hermes_cli/kanban_db.py:3712-3855`、`:6369-7021` |
| complete/block/unblock | `hermes_cli/kanban_db.py:4094-4301`、`:4876-5225` |
| Dispatcher 具体如何 spawn | `hermes_cli/kanban_db.py:7439-7904`、`:8169-8355` |
| Worker 能看到哪些工具 | `tools/kanban_tools.py:65-179`、`:1919-2025`、`toolsets.py:70-78`、`:261-279` |
| Worker prompt/context 如何注入 | `agent/prompt_builder.py:198-296`、`agent/system_prompt.py:221-240`、`agent/agent_init.py:1235-1244` |
| Gateway 内嵌 Dispatcher/notifier | `gateway/kanban_watchers.py:115-371`、`:744-1286`；启动挂载在 `gateway/run.py:7760-7769` |
| DAG fan-out/decompose | `hermes_cli/kanban_decompose.py:271-468`、`hermes_cli/kanban_db.py:5319-5539` |
| 固定的并行 worker→verifier→synthesizer | `hermes_cli/kanban_swarm.py:77-222` |
| Dashboard 后端 API/实时事件 | `plugins/kanban/dashboard/plugin_api.py:378-2489` |
| Dashboard 插件如何挂载 | `plugins/kanban/dashboard/manifest.json:1-14`、`hermes_cli/web_server.py:17752-17867` |
| 测试入口 | 下文“测试与证据” |

## 2. 分层架构与职责边界

### 2.1 入口层

1. `hermes_cli/main.py:4354-4358` 定义薄包装 `cmd_kanban(args)`，再调用 `hermes_cli.kanban.kanban_command`。
2. `hermes_cli/main.py:13489-13494` 把 `build_parser()` 挂到顶层 `hermes kanban`。
3. `hermes_cli/kanban.py:193-884` 只负责 argparse 树、参数和命令清单；真正的读写委托给 `kanban_db`。
4. `hermes_cli/kanban.py:891-1007` 根据 `kanban_action` 选择 handler，并在任务命令前初始化 DB；`--board` 通过 `scoped_current_board()` 作用于本次调用。
5. `hermes_cli/kanban.py:2873-2948` 的 `run_slash(rest)` 把单个 `/kanban ...` 字符串重新送入同一套 argparse/handler，因此 CLI 内部 slash 和 Gateway slash 不应各自实现一份 Kanban 逻辑。

### 2.2 核心存储层

`hermes_cli/kanban_db.py` 是事实上的 Kanban kernel：

- 路径/board 解析、初始化、迁移、SQLite 连接。
- Task/Run/Comment/Event/Attachment 的 dataclass 视图。
- 任务创建、链接、状态转换、claim/reclaim、依赖 promotion。
- worker workspace、log、attachment 路径。
- dispatcher tick 和 worker spawn。
- worker context 和消息订阅游标。

CLI、Worker tools、Dashboard API 都复用了这层的数据模型/连接/事务；但 Dashboard 的部分编辑、drag/drop 和批量更新 helper 会在自己的事务中直接执行受控 SQL 并写入事件，不应概括为所有写操作都经过某个语义 kernel 函数。

### 2.3 调度层

`gateway/kanban_watchers.py` 把两条长期后台循环从 Gateway 中集中管理：

- `_kanban_dispatcher_watcher()`：负责周期性 `dispatch_once()`。
- `_kanban_notifier_watcher()`：负责从 `kanban_notify_subs` 读取 terminal/status 事件并发消息。

`gateway/run.py:7760-7769` 在 Gateway 启动时把两个 watcher 交给 `_spawn_supervised()`。默认不需要另起 daemon。

### 2.4 Agent/Worker 层

- `tools/kanban_tools.py`：给模型的 task lifecycle 工具。
- `toolsets.py`：声明 `kanban` toolset 和 core tool 名称。
- `agent/agent_init.py:1235-1244`：根据有效工具集缓存 Kanban 生命周期指导。
- `agent/system_prompt.py:229-240`：把 Kanban 指导放进稳定的 system prompt。
- `agent/conversation_loop.py:5608-5653`：如果 worker 只输出文字而没有 `kanban_complete`/`kanban_block`，注入有限次数的 terminal-tool nudge。
- `run_agent.py:3215-3235` 与 `tools/kanban_tools.py:231-287`：正常 API activity 也会 best-effort 地桥接为 Kanban heartbeat，减少“进程活着但 claim 被回收”。

## 3. Board、路径和 SQLite 数据模型

### 3.1 Board 和路径解析

`kanban_db.py:332-460` 定义共享 Kanban home、board slug 和 current-board pointer；`kanban_db.py:516-559` 定义 DB/workspace 路径。

实际实现中要区分两种“显式路径”:

1. `connect(db_path=...)` 传入的显式 `db_path` 直接使用。
2. 否则 `kanban_db_path(board=...)` 的实现先检查 `HERMES_KANBAN_DB`，再解析 board；因此环境变量 `HERMES_KANBAN_DB` 是一个强制路径覆盖。没有该路径覆盖时，board slug 的 `get_current_board()` 顺序是 `scoped_current_board()` 的 ContextVar 覆盖 → `HERMES_KANBAN_BOARD` → `kanban/current` → `default`；CLI `--board` 正是通过 scoped override 作用于当前调用。

默认布局：

```text
<shared Hermes root>/
├── kanban.db                         # default board，历史兼容路径
└── kanban/
    ├── current                       # 当前 board slug
    ├── workspaces/                   # default board scratch workspace
    ├── logs/                          # default board worker logs
    ├── attachments/                  # default board attachments
    └── boards/
        ├── default/
        │   └── board.json                 # default board metadata; DB stays above
        └── <slug>/
            ├── board.json
            ├── kanban.db
            ├── workspaces/
            ├── logs/
            └── attachments/
```

`default` 的 DB 仍然保持历史兼容路径 `<shared Hermes root>/kanban.db`；`board.json` 属于 metadata，因此位于 `kanban/boards/default/`。`HERMES_KANBAN_HOME` 可改变根；`HERMES_KANBAN_DB` 和 `HERMES_KANBAN_WORKSPACES_ROOT` 可直接 pin 路径。Dispatcher 启动 worker 时会把 DB、workspace root、board slug 全部写入 worker 环境，避免 profile 激活后重新解析到另一块 DB（`kanban_db.py:8259-8271`）。

### 3.2 SQLite 并发策略

`kanban_db.py:61-65`、`:2275-2344` 是并发设计的关键：

- 默认/优先使用 SQLite WAL；NFS/SMB/FUSE 等网络文件系统不支持 WAL 时回退到 `DELETE` journal mode；连接设置 `busy_timeout`。
- 所有多语句写入使用 `BEGIN IMMEDIATE`。
- 事务边界的 `BEGIN`/`COMMIT` 有有限 jitter retry，但不会重放事务体。
- claim 使用 `UPDATE ... WHERE status='ready' AND claim_lock IS NULL` 的 CAS；竞争失败者得到 0 affected rows，而不是靠分布式锁重试。
- `dispatch_once()` 之外还有 board-scoped `.dispatch.lock`；Gateway 外层还有 machine-global `.dispatcher.lock`。正常取得 advisory lock 时，竞争者会跳过本次 dispatch；但两类锁都是非阻塞且 fail-open 的 best-effort 防护，锁文件不可用/平台不支持时仍可能继续运行。

### 3.3 表和关键字段

完整 schema 位于 `kanban_db.py:1096-1277`。集成建议：UI 建模时至少要理解以下表：

| 表 | 作用 | UI/数据流意义 |
|---|---|---|
| `tasks` | 当前任务卡和当前状态 | 卡片主体、状态列、assignee、priority、workspace、失败计数、当前 run 指针 |
| `task_links` | `parent_id -> child_id` DAG 边 | 依赖线、子任务、fan-in/fan-out、父任务进度 |
| `task_comments` | 人/Agent 的持久评论线程 | handoff、人工输入、swarm blackboard 的承载位置 |
| `task_events` | append-mostly 事件流 | Dashboard WS、CLI tail、诊断、通知游标的共同事实源；终态任务的旧事件可由 `gc_events()` 清理 |
| `task_runs` | 每次 claim/attempt 的历史 | running attempt、PID、heartbeat、outcome、summary、metadata |
| `task_attachments` | 文件附件元数据；blob 在磁盘 | 详情抽屉下载、worker context 的绝对路径、完成产物持久化 |
| `kanban_notify_subs` | `(task, platform, chat, thread)` 订阅及 `last_event_id` | 消息平台 terminal notification 的投递游标 |

`tasks` 的重要字段：

- 身份/内容：`id`, `title`, `body`, `created_by`, `created_at`, `tenant`, `session_id`。
- 路由：`assignee`, `priority`, `workflow_template_id`, `current_step_key`。
- 状态：`status`, `result`, `completed_at`, `block_kind`, `block_recurrences`。
- claim：`claim_lock`, `claim_expires`, `worker_pid`, `last_heartbeat_at`, `current_run_id`。
- 执行：`workspace_kind`, `workspace_path`, `branch_name`, `project_id`, `skills`, `model_override`, `max_runtime_seconds`, `max_retries`, `goal_mode`, `goal_max_turns`。
- 可靠性：`consecutive_failures`, `last_failure_error`。

`task_runs` 是“一个任务的一次尝试”，不是任务本身。每次 claim 都新建 run；完成、阻塞、崩溃、超时、spawn failure、reclaim 都会关闭当前 run。`_end_run()` 在 `kanban_db.py:3236-3290` 清理 run 的 claim/PID 并把 `tasks.current_run_id` 置空。

## 4. 状态机和依赖状态流

### 4.1 状态全集

`kanban_db.py:102-135` 定义：

```text
triage → todo → ready → running → done
                       │       ├── blocked
                       │       ├── scheduled
                       │       └── review → running   # review agent path
```

实际允许的状态集合是：

```text
triage, todo, scheduled, ready, running, blocked, review, done, archived
```

`VALID_INITIAL_STATUSES` 目前只接受 `running` 和 `blocked` 作为 create 参数；普通 create 传默认 `running` 时，kernel 仍会根据 `triage`/parents 计算最终状态。

### 4.2 状态转换表

| 触发点 | 状态变化 | 代码事实 |
|---|---|---|
| 普通 create，无 parent | `→ ready` | `create_task()`，`:2412-2418`, `:2581-2606` |
| create，有 parent | `→ todo` 或 `ready` | create/link/unblock 的即时判断只把 parent `done` 视为满足；`archived` parent 也可能令 child 初始停在 `todo` |
| create `triage=True` | `→ triage` | 交给 specify/decompose |
| `recompute_ready()` | `todo → ready` | 所有 parent 已 `done/archived`；会尊重 sticky block 和失败上限（`:3393-3477`） |
| `claim_task()` | `ready → running` | `BEGIN IMMEDIATE` 内 CAS；同时插入 `task_runs`、写 `claimed` event（`:3484-3603`） |
| `claim_review_task()` | `review → running` | review attempt 单独建 run（`:3606-3678`） |
| `heartbeat_claim()` | 保持 `running` | 延长 claim TTL，同时更新 run expiry（`:3681-3710`） |
| `heartbeat_worker()` | 保持 `running` | 更新 task/run heartbeat，写 `heartbeat` event（`:6369-6417`） |
| worker `complete` | `running/ready/blocked → done` | close run、写 summary/metadata、清失败计数、促成 children ready（`:4094-4301`） |
| `block(kind=dependency)` | `running/ready → todo` | 不进人工 blocked 列，等待 parent DAG（`:4876-4976`） |
| `block(kind=needs_input/capability/transient/None)` | `running/ready → blocked` | 人工介入/暂时失败路径；相同原因反复 block 达阈值会去 `triage`（`:4978-5088`） |
| `unblock_task()` | `blocked/scheduled → ready` 或 `todo` | 即时 parent 判断使用 `parent.status != 'done'`，所以 archived parent 也可能得到 todo；保留 block recurrence 计数（`:5162-5225`） |
| `schedule_task()` | `todo/ready/running/blocked → scheduled` | 时间等待，不等价于人工 blocked（`:5923-5965`） |
| stale claim | `running → ready` | TTL 到期且无法证明活跃；活 worker 会先延长 claim，避免重复 spawn（`:3712-3855`） |
| timeout | `running → ready` | 超过 `max_runtime_seconds` 后 SIGTERM/SIGKILL，写 `timed_out` 并累加失败计数（`:6420-6531`） |
| crash | `running → ready` | host-local PID 不活跃时写 `crashed`；失败达到阈值则 `blocked` + `gave_up`（`:6752-7021`） |
| archive | `* → archived` | 解除 parent blocking；可随后 GC/hard delete（`:5542-5614`） |

### 4.3 两个容易混淆的“blocked”

1. `kind="dependency"` 是“依赖等待”，代码把它放进 `todo`，让 `recompute_ready()` 自动处理。
2. `needs_input`、`capability`、一般 `None` 是真正需要人工/外部决策的 blocked；worker 主动 block 还会被 `_has_sticky_block()` 识别，不会被普通 `recompute_ready()` 偷偷拉回 ready。
3. 同一种真正阻塞 `block_kind` 重复经历 unblock→re-block 达 `BLOCK_RECURRENCE_LIMIT=2` 后，转到 `triage`；具体 reason 文本不会参与 recurrence 判断，打破无限 cron loop。

集成建议：UI 不要只根据 `status == blocked` 猜原因，应同时读取 `block_kind`、最近 `task_events` 和最新 run outcome。

## 5. Dispatcher：从 ready 卡到 worker 进程

### 5.1 Gateway 内嵌循环

`gateway/kanban_watchers.py:744-1286` 的 `_kanban_dispatcher_watcher()`：

1. 读取 `kanban.dispatch_in_gateway`，默认 `True`；环境变量 `HERMES_KANBAN_DISPATCH_IN_GATEWAY` 可禁用。
2. 尝试获取 `<kanban home>/kanban/.dispatcher.lock`；其他 Gateway 已持有时本实例不 dispatch，锁不可用时记录 warning 并退回 config-only 控制。
3. 读取 `dispatch_interval_seconds`（默认 60 秒）、`failure_limit`（默认 2）、`dispatch_stale_timeout_seconds`（默认 14400 秒）、`default_assignee`、`max_in_progress`、`max_in_progress_per_profile` 等配置。
4. 每 tick 先 reap zombie，再按当前磁盘上的 board 列表逐 board 执行。
5. 如果启用 `auto_decompose`，在 dispatch 前对 `triage` 卡调用辅助 LLM decompose；每 tick 默认最多 3 张。
6. 调用 `kanban_db.dispatch_once()`，把结果汇总到日志和 health telemetry。
7. 首次进入循环前有约 5 秒 wiring delay，之后以 1 秒小片段 sleep；Gateway stop 时不用等待完整 interval。

配置默认值位于 `hermes_cli/config.py:2820-2879`：

```yaml
kanban:
  dispatch_in_gateway: true
  dispatch_interval_seconds: 60
  failure_limit: 2
  orchestrator_profile: ""
  default_assignee: ""
  max_in_progress_per_profile: null
  auto_decompose: true
  auto_decompose_per_tick: 3
  dispatch_stale_timeout_seconds: 14400
```

`max_spawn` 是 `dispatch_once` 的 live concurrency cap：它统计当前 `running` 加上本 tick 已 spawn 的数量，而不是简单的“每 tick 新增 N”。`max_in_progress` 是另一层全局上限；`max_in_progress_per_profile` 限制同一个 profile 的 in-flight 数量。

默认 liveness 常量在 `kanban_db.py:166-224`：claim TTL 为 15 分钟；若 host-local worker PID 仍活着且没有超过 1 小时的 stale heartbeat，过期 claim 会先延长（从未发过 heartbeat 也不会因这个 backstop 直接判 stale）；刚 spawn 后有 30 秒 crash-detection grace。任务自己的 `max_runtime_seconds` 是另一条硬 runtime cap。

### 5.2 单次 tick 的顺序

`kanban_db.py:7439-7904` 的实际顺序：

```text
reap_worker_zombies()
    ↓
release_stale_claims()       # claim TTL / PID 活性判断
    ↓
detect_stale_running()      # 配置启用时 heartbeat stale
    ↓
detect_crashed_workers()   # host-local PID
    ↓
enforce_max_runtime()      # per-task runtime cap
    ↓
recompute_ready()            # todo/依赖满足 → ready
    ↓
扫描 ready rows（priority DESC, created_at ASC）
    ↓
过滤：unassigned / non-spawnable profile / profile cap / respawn guard
    ↓
claim_task()                 # CAS ready → running
    ↓
resolve_workspace()
    ↓
_default_spawn()
    ↓
回写 worker_pid；失败则 _record_spawn_failure()
    ↓
另行扫描 review rows，启动 review agent
```

默认目录布局下各 board 的 DB 是分开的；但 `HERMES_KANBAN_DB` 可以让多个 board slug 复用同一路径。`dispatch_once()` 还用 board DB 同目录的 `.dispatch.lock` 做跨进程单 tick 互斥（`:7439-7502`）；锁竞争时返回 `skipped_locked=True`，无法打开/使用锁时会降级为继续 dispatch。

在 ready 扫描和 claim 前，未分配任务会先尝试应用有效的 `default_assignee`；仍未分配的 task 才进入 `skipped_unassigned`，不能被 spawn。

### 5.3 失败、重试和自动阻塞

`_record_task_failure()` 位于 `kanban_db.py:7022-7202`：

- 统一统计 `spawn_failed`、`crashed`、`timed_out`。
- 优先使用任务的 `max_retries`，否则使用 Dispatcher 传入的 `failure_limit`，再退回默认 2。
- 未达到阈值：回到 `ready`，保留 `consecutive_failures` 和 `last_failure_error`。
- 达到阈值：转 `blocked`，写 `gave_up` event。
- rate-limit sentinel（exit code 75）不计入失败，回到 ready 并由 respawn guard 等待配额窗口。
- clean-exit 但没有调用 terminal Kanban tool 的 protocol violation 有独立的 violation-only streak，默认上限为 3（可被 task `max_retries` 覆盖）；低于该上限时不累加统一的 `consecutive_failures`。stale reclaim 也不是普通失败计数。

集成建议：UI 的“失败次数”不应只统计 event 行数；优先显示 `tasks.consecutive_failures`，再用 `task_runs`/`task_events` 解释原因。

### 5.4 `_default_spawn()` 的 worker 合同

`kanban_db.py:8169-8355` 启动：

```text
<hermes executable> -p <normalized assignee> --cli --accept-hooks
    [--skills skill ...]
    [-m model_override]
    [--toolsets profile_toolsets]
    chat -q "work kanban task <task_id>"
    [-Q if goal_mode]
```

同时注入：

```text
HERMES_HOME                     profile-scoped home
HERMES_PROFILE                 assignee
HERMES_KANBAN_TASK             当前任务 id
HERMES_KANBAN_WORKSPACE        resolved workspace
HERMES_KANBAN_RUN_ID           当前 task_runs.id
HERMES_KANBAN_CLAIM_LOCK       当前 claim lock
HERMES_KANBAN_DB               Dispatcher 实际使用的 DB
HERMES_KANBAN_WORKSPACES_ROOT  Dispatcher 实际使用的 workspace root
HERMES_KANBAN_BOARD             board slug
TERMINAL_CWD                   workspace（路径有效时）
HERMES_KANBAN_BRANCH           worktree branch（有时）
HERMES_KANBAN_GOAL_MODE        goal mode 开关（有时）
```

上面是核心合同而不是穷尽清单：task tenant 还会注入 `HERMES_TENANT`；goal task 可能注入 `HERMES_KANBAN_GOAL_MAX_TURNS`；有 `max_runtime_seconds` 时会按剩余预算注入 `TERMINAL_TIMEOUT` 和 `TERMINAL_MAX_FOREGROUND_TIMEOUT`。为避免 worker 误入 TUI，`_default_spawn()` 会移除继承的 `HERMES_TUI`。

stdout/stderr 追加写入 `<board logs>/<task_id>.log`，默认 2 MiB 轮转、保留一个 `.log.1`（`:7915-7986`、`:8321-8355`）。

`hermes_cli/kanban.py:2326-2464` 的 `hermes kanban daemon` 默认只打印迁移提示并返回非零；旧式 standalone daemon 只有显式 `--force` 才继续。不要让 Gateway 和 standalone daemon 同时写同一 board。

## 6. Worker 侧工具、权限和上下文

### 6.1 Tool visibility

`toolsets.py:70-78` 把 Kanban 工具加入 core 工具名单；`toolsets.py:261-279` 定义 `kanban` toolset。实际工具注册在 `tools/kanban_tools.py:1919-2025`。

可用工具包括：

```text
kanban_show
kanban_list
kanban_complete
kanban_block
kanban_heartbeat
kanban_comment
kanban_create
kanban_link
kanban_unblock
kanban_attach
kanban_attach_url
kanban_attachments
```

`tools/kanban_tools.py:65-93` 有两级 gate：

- `HERMES_KANBAN_TASK` 存在时，进程是 Dispatcher worker，获得自己的 lifecycle 工具。
- profile 显式配置 `kanban` toolset、但没有 task env 时，是 orchestrator surface。
- `kanban_list`、`kanban_unblock` 对 dispatcher worker 隐藏，只给 orchestrator；运行时 `_require_orchestrator_tool()` 还会再次拒绝 stale registration。

`tools/kanban_tools.py:65-79` 只把存在 `HERMES_KANBAN_TASK` 的进程视为 worker surface；这只是环境/工具 gate，不是强认证或进程沙箱。`tools/kanban_tools.py:135-164` 对 `complete/block/heartbeat/attach/attach_url` 等生命周期写操作做 ownership guard，不能显式传 sibling task id；但这不是全面的任务隔离——`kanban_comment` 明确允许跨任务评论作为 handoff，`kanban_create` 可指定任意 parents，`kanban_link` 可操作显式 parent/child，`kanban_show`/`kanban_attachments` 也按传入 id 读取。

### 6.2 Worker context 的来源

`kanban_show()` 在 `tools/kanban_tools.py:367-432` 返回 task、parents、children、comments、events、runs 和 `worker_context`。

`kanban_db.build_worker_context()`（`:8421-8671`）按以下顺序组织 context：

1. 当前任务标题、assignee、状态、租户、workspace、runtime cap/terminal timeout（如有）、branch。
2. task body，最多 8 KB。
3. 当前任务附件的绝对路径。
4. 当前任务最近 10 次已结束 run；较旧尝试折叠。
5. 已完成 parent 的 summary/metadata；带相对时间，并明确说明是 point-in-time handoff，不是 live state。
6. 当前 assignee 最近完成的其他 5 个任务，提供 role history。
7. 最近 30 条评论；更旧评论折叠。

这说明“parent handoff”不是新消息队列：它是从 `task_runs`/`tasks.result`/`task_comments` 在 worker 启动或 `kanban_show` 时重新渲染的稳定文本。

### 6.3 Worker 终止协议

`agent/prompt_builder.py:198-296` 明确要求 worker：

1. 先 `kanban_show()`。
2. 在 `$HERMES_KANBAN_WORKSPACE` 工作。
3. 长任务周期性 `kanban_heartbeat()`。
4. 真正需要人工决策时 `kanban_block()`。
5. 完成时用 `kanban_complete(summary, metadata, artifacts)`。
6. 后续工作用 `kanban_create()` 交给其他 profile，不要自己 scope creep。

`agent/conversation_loop.py:5608-5653` 是框架层保护：模型如果只输出“我接下来会……”就停，Hermes 会合成一次/有限次数的提示，要求立即调用 `kanban_complete` 或 `kanban_block`。如果 worker 最终仍然 clean-exit 且没有 terminal board tool，Dispatcher 会把它记录为 protocol violation。

### 6.4 关键工具语义

| 工具 | 代码位置 | 主要行为 |
|---|---|---|
| `kanban_show` | `tools/kanban_tools.py:367-432` | 读完整任务上下文；无参时从 `HERMES_KANBAN_TASK` 取 id |
| `kanban_list` | `:443-501` | orchestrator-only；有限制的列表并先 `recompute_ready()` |
| `kanban_complete` | `:504-672` | 敏感文本 redaction；可提交 summary/metadata/created_cards/artifacts；源码意图是在 auxiliary judge 可用时检查 goal，但当前 `judge_goal()` 返回四元组而调用方按三元组解包（`tools/kanban_tools.py:601-617` vs `hermes_cli/goals.py:836-857,928-957`），触发异常后被捕获并 fail-open，因此当前 judge gate 实际不生效；最终调用 `complete_task()` |
| `kanban_block` | `:675-751` | 需要 reason；支持 typed block；goal mode 限制可用 kind |
| `kanban_heartbeat` | `:753-801` | 同时延长 claim TTL 和写 heartbeat event |
| `kanban_comment` | `:804-839` | 评论 author 取 worker runtime identity，不接受任意 author 伪造 |
| `kanban_create` | `:1060-1177` | 创建 child task，必须有 assignee；worker 默认继承自身 workspace/project |
| `kanban_unblock` | `:1278-1305` | orchestrator-only；blocked/scheduled → ready/todo |
| `kanban_link` | `:1307-1326` | 添加 parent→child 边并阻止 cycle |
| attachment 工具 | `:841-1057` | 与 Dashboard/CLI 共享 25 MB 限制、路径安全和 metadata 记录 |

## 7. 多 Agent 并行：两种真实实现

### 7.1 普通 DAG：`kanban_create` + `task_links`

并行的最小模式：

```text
orchestrator
    ├─ create child A, parents=[]  → ready → worker A
    ├─ create child B, parents=[]  → ready → worker B
    └─ create synthesizer, parents=[A, B] → todo
                                             │
                          A done + B done ──┘
                                             ▼
                                           ready
```

代码事实：

- `create_task()` 会在同一写事务中插入 task、parent links 和 `created` event（`:2387-2704`）。
- create/link/unblock 的即时 parent 判断只把 `done` 视为满足，因此 archived parent 仍可能让新 child 初始为 `todo`；这是与后续 promotion 不同的边界。
- `recompute_ready()` 要求所有 parent 为 `done` 或 `archived`，并同时处理 `todo` 与非 sticky `blocked` → `ready`。
- `claim_task()` 还有第二道 parent CAS invariant，防止错误 writer 把未满足依赖的卡直接 claim（`:3500-3524`）。
- `link_tasks()` 拒绝 self-link 和 cycle，并在 parent 未 done 时把已 ready child 降回 todo（`:2814-2841`）。
- 多个无依赖 child 会同时进入 Dispatcher 的 ready scan；受 global/per-profile concurrency cap 限制后分别 spawn。

这条链路是“持久任务并行”，不等同于当前会话里的 `delegate_task`。Kanban card 可以跨 API loop、跨 Gateway restart、跨 worker retry 存活。

### 7.2 Triage 自动分解：`kanban_decompose`

`hermes_cli/kanban_decompose.py:271-468`：

1. 只接受 `triage` task。
2. 读取 profile roster + description 和 default assignee。
3. 调用 `auxiliary.kanban_decomposer`，要求 JSON：`fanout`、`tasks`、`assignee`；`tasks` 数组内的 `parents` 使用同一数组的 0-based indices。源码随后才把该 JSON 字段规范化到内部变量 `children`。
4. 清理非法 assignee，全部 fallback 到有效 profile；清理越界/自依赖 parent index。
5. 调用 `kanban_db.decompose_triage_task()`。

`kanban_db.py:5319-5539` 把整个 fan-out 做成一个 `write_txn`：

- 先验证 children 形状和 sibling DAG 无 cycle。
- 所有 child 先创建为 `todo`。
- child 之间按 index 建 `task_links`。
- 为了让 root 等待整个图完成，实际写入的是 `child → root` 依赖边：root 是这些 child 的下游 dependent，而不是它们的 parent。源码注释/函数 docstring 有“root becomes parent”的旧表述，但 `INSERT INTO task_links (parent_id, child_id)` 的实际参数在 `kanban_db.py:5487-5496` 是 `(child_id, root_id)`；集成提示：读取该 DAG 时应以实际写入方向为准。
- root `triage → todo`，assignee 改成 orchestrator profile。
- 写 root comment + `decomposed` event。
- `auto_promote=True` 时立即 `recompute_ready()`，无 parent 的起始 child/source nodes 进入 ready；按 `parent_id → child_id` 的存储方向，不应称为图的 leaves。

Gateway watcher 每 tick 最多处理 `auto_decompose_per_tick` 张 triage 卡；手动入口是 `hermes kanban decompose` 或 Dashboard Decompose 按钮。

### 7.3 固定 Swarm 图：`kanban_swarm`

`hermes_cli/kanban_swarm.py:1-15` 说明它不另建 scheduler，而是写入既有 Kanban kernel：

```text
planning root (done immediately; shared blackboard)
    ├─ worker A (ready)
    ├─ worker B (ready)
    └─ ...
         ↓ all workers done
       verifier (todo → ready)
         ↓ verifier completes（Prompt 要求 gate pass；kernel 不验证 metadata.gate）
       synthesizer (todo → ready)
```

`kanban_swarm.py:77-222`：

- root 立即 done，但保留 topology metadata 和 blackboard anchor。
- 每个 worker 以 root 为 parent。
- verifier 以所有 worker 为 parents；其 Prompt 要求只有证据充分时才用 `metadata.gate=pass` 完成，但当前 kernel 没有检查该 metadata，任意成功完成 verifier 都可以触发下游 promotion。
- synthesizer 以 verifier 为 parent。
- blackboard 使用 root task 的 `[swarm:blackboard] <JSON>` comments；`latest_blackboard()` 以后写覆盖先前同 key 值，并记录 `_authors`。

这是一个可复用的 fan-out/fan-in 模板。集成建议：后续 UI 可以把它渲染成 DAG，而不是只显示线性列。

## 8. 通知和实时 UI 数据流

### 8.1 消息平台 terminal notification

订阅表和游标实现位于 `kanban_db.py:8759-8967`：

- `add_notify_sub()` 按 `(task_id, platform, chat_id, thread_id)` 幂等插入。
- `last_event_id` 是每个订阅自己的 cursor。
- `claim_unseen_events_for_sub()` 在 `BEGIN IMMEDIATE` 内先 CAS 推进游标，避免多个 notifier 重复发送。
- 发送失败可 `rewind_notify_cursor()`，并有 CAS 防止覆盖后来 watcher 的进度。

订阅来源：

1. Gateway `/kanban create`：`gateway/slash_commands.py:431-527` 调 `run_slash()` 后解析 task id，自动以原始 platform/chat/thread 建订阅。
2. Worker/Orchestrator `kanban_create`：`tools/kanban_tools.py:1179-1276` 的 `_maybe_auto_subscribe()` 从 Gateway session env 或 TUI session key 建订阅。
3. Dashboard home subscribe：`plugin_api.py:1713-1869` 按平台 home channel 创建/删除订阅。

Gateway notifier `gateway/kanban_watchers.py:115-371`：

- Notifier 与 Dispatcher 共享 `HERMES_KANBAN_DISPATCH_IN_GATEWAY` / `kanban.dispatch_in_gateway` 启停门禁；每 tick 遍历 board，读取订阅并 claim terminal/status/unblocked event。
- 通过授权 adapter 发到正确的 profile/platform/chat/thread。
- `completed` 优先展示 run summary；`blocked` 展示 reason。
- 任务真正 `done/archived` 后才移除 subscription；crashed/gave_up/timed_out/blocked 之后仍保留，以便 retry loop 后继续通知。
- 单次 `dispatch_once()` 的 global/per-profile cap 是按当前 board 统计，不是跨所有 board 的 fleet-wide 上限；Notifier 连续 3 次发送失败会删除该订阅。

### 8.2 Dashboard Plugin

manifest：`plugins/kanban/dashboard/manifest.json:1-14`：

- tab `/kanban`。
- frontend `dist/index.js`，CSS `dist/style.css`。
- backend `plugin_api.py`。

Dashboard backend 在 `plugin_api.py` 中复用 `kanban_db`，挂载路径由 `web_server.py:17752-17867` 统一变成 `/api/plugins/kanban/...`。HTTP plugin 路由受 dashboard auth middleware 保护；WebSocket `/events` 通过共享 `_ws_auth_ok()` 校验。

主要 API：

| API | 代码位置 | 返回/作用 |
|---|---|---|
| `GET /board` | `plugin_api.py:378-510` | grouped columns、tenants、assignees、latest event id、card summary、diagnostics、parent progress |
| `GET /tasks/{id}` | `:517-589` | 完整 task、comments、events、attachments、links、child results、runs |
| `POST /tasks` | `:596-656` | 创建 task；ready+assigned 且无 Dispatcher 时返回 warning |
| `PATCH /tasks/{id}` | `:805-936` | status/assignee/priority/title/body；禁止直接把状态设成 running |
| `POST /tasks/bulk` | `:1148-1254` | 多选批量更新，逐 id 返回结果 |
| `GET /diagnostics` | `:1263-1337` | fleet distress signals |
| `GET /workers/active` | `:1351-1409` | 当前 active workers |
| `GET /runs/{id}` | `:1412-1431` | run detail |
| `GET /runs/{id}/inspect` | `:1434-1500` | psutil PID/CPU/memory 等进程检查 |
| `POST /runs/{id}/terminate` | `:1506-1551` | 通过 `reclaim_task()` 终止并回收 worker |
| `POST /tasks/{id}/reclaim` | `:1562-1589` | 不等待 TTL 的人工回收 |
| `POST /tasks/{id}/reassign` | `:1650-1682` | 可先 reclaim 再换 profile |
| attachment APIs | `:675-798` | 上传/下载/删除 |
| home subscription | `:1812-1869` | 绑定消息平台 home channel |
| `GET /stats`/`assignees` | `:1876-1905` | HUD 和 profile picker |
| `GET /tasks/{id}/log` | `:1913-1946` | worker log tail |
| `POST /dispatch` | `:1953-1971` | UI quick-path 手动触发一次 dispatch |
| board CRUD/switch | `:2027-2138` | 多 board 管理 |
| `WS /events` | `:2427-2489` | 从 `task_events.id > since` 每 0.3 秒读取，批量发 `{events, cursor}` |

浏览器 frontend `plugins/kanban/dashboard/dist/index.js` 的关键定位：

- `:195` 定义 `/api/plugins/kanban` base URL。
- `:586-626` 加载 board 列表和 board 数据。
- `:628-704` 建立 `/events` WebSocket，并用 cursor 增量更新。
- `:740-820` 处理 task patch/create。
- `:778-798` 处理 bulk action。
- `:1249-1384` 对诊断项执行 reclaim/reassign/unblock 等恢复操作。

实时 UI 的权威增量源是 `task_events`，不是“某个内存中的 task list”。浏览器可以用 `GET /board` 做初始快照，再以 WS cursor 增量刷新；当前 frontend 对普通 close 按退避策略重连，但 close code `1008` 鉴权失败和 `new WebSocket()` 构造异常会直接停止该次连接流程；这些路径都不会自动重新拉 `GET /board`（收到事件时才 schedule board reload）。断线后按需重新拉 snapshot 是集成时应补充的健壮性策略，而不是当前实现事实。

## 9. 给 LLaMAR_UI 的融合建议（非 Hermes 代码事实）

这一节是设计建议，不代表 Hermes 当前已经实现了 LLaMAR_UI 适配。

### 9.1 推荐的最小集成边界

建议做一个 `KanbanRepository`/adapter，而不是让 UI 组件直接拼 SQL：

```text
LLaMAR_UI components
        │
        ▼
KanbanRepository
  ├─ snapshot(board, filters)      → GET /board 等价数据
  ├─ task_detail(task_id)          → GET /tasks/{id}
  ├─ transition(task_id, action)   → complete/block/reclaim/unblock/reassign
  ├─ graph(task_id)                → links + child progress
  ├─ run_detail(run_id)            → run + inspect/log
  └─ subscribe_events(cursor)      → task_events WebSocket/SSE adapter
```

如果 LLaMAR_UI 与 Hermes 在同一 Python 进程/同一环境，adapter 可以调用 `hermes_cli.kanban_db`；如果隔离部署，优先接 Dashboard Plugin HTTP/WS API，而不要跨进程直接读 DB 文件。

### 9.2 UI 数据模型应分成四层

1. **Card projection**：`id/title/status/assignee/priority/tenant/latest_summary/age/warnings`。
2. **Graph projection**：`parents/children/progress`，用于 DAG、依赖阻塞提示、fan-in progress。
3. **Attempt projection**：`task_runs` 的 profile/status/outcome/PID/heartbeat/summary/metadata。
4. **Event projection**：`task_events` 的 id/kind/payload/run_id/created_at，用于增量刷新和审计。

不要把 `tasks.result` 当成唯一 handoff 字段；Worker 的标准交付在 `task_runs.summary` + `task_runs.metadata`，Dashboard 已通过 `latest_summary` 兼容两者。

### 9.3 列和状态呈现

建议 UI 的默认列与插件保持一致：

```text
triage | todo | scheduled | ready | running | blocked | review | done
```

`archived` 做过滤器，不作为常驻列。卡片上至少显示：

- status + `block_kind`。
- assignee + 当前 run id。
- `consecutive_failures`、last failure error。
- `last_heartbeat_at`、claim expires、worker PID。
- parent/child 数量和 `done/total` progress。
- latest summary preview。

状态操作要走语义 API：

- 不要 UI 直接把 `running` 写入 DB；源码明确要求 running 只能通过 dispatcher/claim path。
- `done` 应调用 complete 语义并传 summary/metadata。
- `blocked` 应要求 reason/kind。
- `ready` 需给出 parent 尚未 done 的冲突信息。
- reclaim/reassign 是恢复动作，应该在 UI 中与普通拖拽分开显示。

### 9.4 实时同步策略

推荐：

1. 页面打开：拉一次 board snapshot，并保存 `latest_event_id`。
2. 连接 WS `/events?since=<latest_event_id>&board=<slug>`。
3. 收到 event 后：
   - 小事件（comment/status/heartbeat）可局部更新。
   - `completed/blocked/reclaimed/decomposed/archived` 直接触发 task detail/board refresh，避免前端复制状态机。
4. WS 关闭、鉴权失败或事件 cursor 失配：退避重连；必要时重新拉 snapshot。
5. 切换 board 时关闭旧 WS，重新以新 board 的 snapshot cursor 建立连接。

### 9.5 与 LLaMAR 现有任务/实验 UI 的映射

如果 LLaMAR UI 已有 `task`、`run`、`event` 概念，建议映射如下：

| LLaMAR UI | Hermes Kanban |
|---|---|
| task/card | `tasks` row |
| parent/child workflow | `task_links` |
| execution attempt | `task_runs` |
| timeline/log | `task_events` + worker log |
| worker profile | `tasks.assignee` / `task_runs.profile` |
| human input | `blocked` + `block_kind=needs_input` + comment |
| artifact | `task_attachments` / completion `artifacts` |
| live update cursor | `task_events.id` / `kanban_notify_subs.last_event_id` |

### 9.6 安全与一致性边界

- 不要把 `HERMES_KANBAN_DB`、session token 或任何 credential 放进前端 bundle、报告或事件 payload。
- Dashboard API 需要复用现有 auth；`/events` 不能裸连。
- Worker 的部分生命周期操作由 tool gate 和 environment scope 约束，但 `HERMES_KANBAN_TASK` 不是强认证或通用进程沙箱；UI 的“以某 profile 运行”应只选择已存在 profile。
- `created_cards` 有 kernel verification；UI/Agent 不应允许用户随意把不存在的卡 id 当 handoff 事实写入。
- scratch workspace 完成时会清理；UI 要提示用户将持久产物作为 `artifacts`/attachment 保存，不能只显示 scratch 路径。
- 多 board 场景必须把 board slug 作为所有查询、事件、log、attachment 请求的显式上下文。

## 10. 后续 Agent 的推荐阅读顺序

### 场景 A：只想知道“卡为什么没有运行”

```text
config.py:2820-2879
→ gateway/kanban_watchers.py:744-1286
→ kanban_db.py:7439-7904
→ kanban_db.py:8169-8355
→ task_events / task_runs / worker log
```

重点检查：是否有 Gateway、dispatch_in_gateway 是否关闭、assignee 是否真实 profile、是否被 max_in_progress/per-profile cap、是否被 respawn guard、是否达到 failure_limit。

### 场景 B：只想知道“为什么 child 还在 todo”

```text
kanban_db.py:2814-2901
→ kanban_db.py:3393-3477
→ kanban_db.py:3500-3524
→ task_links + parent task status
```

重点检查：所有 parent 是否 `done/archived`；是否 sticky blocked；是否 link cycle/错误 writer 造成 `claim_rejected`。

### 场景 C：只想知道“worker 做完后下游看到了什么”

```text
tools/kanban_tools.py:504-672
→ kanban_db.py:4094-4301
→ kanban_db.py:3236-3290
→ kanban_db.py:8421-8671
```

重点检查：complete 是否成功、summary/metadata 是否在 closing run、parent context 是否被重新渲染、scratch artifact 是否被保存成 attachment。

### 场景 D：要接 UI 实时看板

```text
plugins/kanban/dashboard/manifest.json
→ web_server.py:17752-17867
→ plugin_api.py:378-589, 2427-2489
→ dist/index.js:586-704
→ kanban_db.py:8759-8967（消息通知另一路）
```

## 11. 测试与证据

源码中已有专门的 Kanban 测试，按层覆盖：

- Kernel/DB：
  - `tests/hermes_cli/test_kanban_db.py`
  - `tests/hermes_cli/test_kanban_core_functionality.py`
  - `tests/hermes_cli/test_kanban_db_init.py`
  - `tests/hermes_cli/test_kanban_write_txn_busy_retry.py`
  - `tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py`
- CLI/board/dependency：
  - `tests/hermes_cli/test_kanban_cli.py`
  - `tests/hermes_cli/test_kanban_boards.py`
  - `tests/hermes_cli/test_kanban_promote.py`
  - `tests/hermes_cli/test_kanban_block_kinds.py`
  - `tests/hermes_cli/test_kanban_blocked_sticky.py`
- 并行/DAG：
  - `tests/hermes_cli/test_kanban_swarm.py`
  - `tests/hermes_cli/test_kanban_decompose.py`
  - `tests/hermes_cli/test_kanban_decompose_db.py`
- Worker/工具/协议：
  - `tests/tools/test_kanban_tools.py`
  - `tests/hermes_cli/test_kanban_worker_spawn_toolsets.py`
  - `tests/hermes_cli/test_kanban_goal_mode.py`
  - `tests/agent/test_kanban_stop.py`
- Gateway：
  - `tests/gateway/test_kanban_watchers_mixin.py`
  - `tests/gateway/test_kanban_notifier.py`
  - `tests/gateway/test_kanban_auto_decompose_live.py`
  - `tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py`
- Dashboard/附件：
  - `tests/plugins/test_kanban_dashboard_plugin.py`
  - `tests/plugins/test_kanban_attachments.py`
  - `tests/plugins/test_kanban_worker_runs.py`

这份报告完成后应由独立 reviewer 只读源码复核，重点核对：路径/行号、状态转换、Dispatcher 默认行为、worker env 合同、DAG fan-out、Dashboard API 和实时事件 cursor。Reviewer 的结论应记录在本文件末尾的“审查记录”中，而不是仅保留在聊天里。

已执行的 ad-hoc 证据（不是完整 pytest suite）：

```text
uv run pytest -q \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_swarm.py \
  tests/tools/test_kanban_tools.py \
  tests/gateway/test_kanban_watchers_mixin.py \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/agent/test_kanban_stop.py
→ 417 passed, 2 warnings, 193.29s (0:03:13)

uv run pytest -q \
  tests/hermes_cli/test_kanban_decompose.py \
  tests/hermes_cli/test_kanban_decompose_db.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_boards.py
→ 123 passed, 31.65s

uv run pytest -q \
  tests/gateway/test_kanban_notifier.py \
  tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py \
  tests/gateway/test_kanban_auto_decompose_live.py
→ 17 passed, 2.90s

uv run pytest -q tests/hermes_cli/test_kanban_db.py \
  -k 'not test_write_txn_check_reads_correct_header_fields'
→ 229 passed, 1 deselected, 78.63s
```

另一次包含 `tests/hermes_cli/test_kanban_db.py` 的较宽目标集返回 `646 passed, 1 failed, 2 warnings`。唯一失败是 `test_write_txn_check_reads_correct_header_fields`：测试预期 `_check_file_length_invariant()` 返回 `torn-extend/page count mismatch`，但当前 Python/SQLite 在 `PRAGMA database_list` 处先抛出 `database disk image is malformed`。排除该单个失败用例后，Kanban DB 文件其余 `229` 个用例通过。这不是本报告新增代码造成的修改；因此不能把这次较宽目标集称为全绿。

## 12. 审查记录

- 初稿：已基于 Hermes revision `78f38e79cfb501c5c4db8cf2cc7593f01e01fb48` 写入。
- 第一轮独立 subagent 审查：`deleg_4b2d085a`，model `gpt-5.6-luna`，只读核查报告与同一 revision 源码，结论为 `APPROVE_WITH_FIXES`；其 2 个 Major、4 个 Minor 已修订。
- 第二轮更广范围独立 subagent 审查：`deleg_3572d6e7`，model `gpt-5.6-luna`，结论为 `REJECT`；其 3 个 Major、7 个 Minor 已逐项核对并修订。
- 第三轮 fresh 独立 subagent 审查：`deleg_1858496a`，model `gpt-5.6-luna`，结论为 `REJECT`；其 1 个 Major、4 个 Minor 已逐项核对并修订，重点揭示 goal judge 当前因三元组/四元组解包不一致而实际 fail-open。
- 第四轮 fresh 独立 subagent 审查：`deleg_ade534c4`，model `gpt-5.6-luna`，结论为 `APPROVE_WITH_FIXES`；仅指出 2 个 Minor：worker context 首段内部字段顺序、以及 schema 表格前未显式标注“集成建议”，现已修订。
- 父 Agent 已逐项读取各轮 reviewer 指出的源码并完成修订；源码事实基准仍为 `78f38e79cfb501c5c4db8cf2cc7593f01e01fb48`。
- 第五轮最终 fresh 独立 subagent 审查：`deleg_a8c29f89`，model `gpt-5.6-luna`，重新读取完整报告与指定 revision 源码，结论为 `APPROVE`，未发现事实性问题。
- 最终状态：报告已通过独立源码事实验收，可作为后续 Agent 定位 Hermes Kanban 和设计 LLaMAR_UI 融合方案的依据。
