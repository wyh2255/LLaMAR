# 代码事实探索 02：反思机制 agentic 化改造面（agentic 审查者实现面）

- 探索日期：2026-08-13
- 目标 commit：`32bfe57`（feat/memory-redesign，`docs(memory): G4 正式可用审批 + P6 文档收口`；工作区另有 4 个未提交文档改动，均为 plans/docs，不影响代码事实）
- 只读声明：本次探索只读，未修改任何源码/配置/数据文件；唯一产出为本报告。
- 范围：`src/a2a/coordinator/memory/reflection.py`（964 行）、`sar_orch/long_term_reflection.py`、`src/Agent/router_agent/agent.py`、`src/Agent/worker_agent/llm/llm_wrapper.py`、`src/Agent/worker_agent/llm/openai_client.py`、`sar_orch/tools/`、`src/a2a/coordinator/memory/long_term.py`、`sar_orch/experiment.py`、`sar_orch/coordinator.py`、`sar_orch/environment_state_provider.py`、`src/a2a/coordinator/memory/store.py`、`src/a2a/coordinator/memory/contracts.py`、`src/a2a/coordinator/router.py`、`src/Agent/router_agent/hooks.py` 等。

---

## 1. reflection.py 的 LLM 调用路径

### 1.1 import 来源（确认：worker_agent 的 llm_wrapper）
- `reflection.py:42`：`from Agent.worker_agent.llm.llm_wrapper import LLMClient`；`reflection.py:43`：`from Agent.worker_agent.schema import LLMProvider, Message`。仓库存在两个 `llm_wrapper.py`（`src/Agent/router_agent/llm/` 与 `src/Agent/worker_agent/llm/`），反射用的是 **worker_agent** 那份。

### 1.2 LLMClient 调用签名（同步/异步、temperature、tools）
- `LLMClient` 定义：`src/Agent/worker_agent/llm/llm_wrapper.py:18-131`；构造 `__init__(api_key, provider=LLMProvider.ANTHROPIC, api_base, model, retry_config)`（llm_wrapper.py:36-43）。
- **异步** `async def generate(messages: list[Message], tools: list | None = None) -> LLMResponse`（llm_wrapper.py:117-131）——转发给底层 `self._client.generate(messages, tools)`（llm_wrapper.py:131）。
- **不支持 temperature**：generate 全链路无 temperature 参数。`openai_client.py:298-302`（`async def generate(messages, tools=None)`）、`_prepare_request` 只返回 `{"api_messages", "tools"}`（openai_client.py:186-205）、`anthropic_client.py:271-274` 同构。router 侧 `RouterAgent.__init__` 虽有 `temperature: float = 0.7` 参数（router.py:264）并保存 `self._temperature`（router.py:281），但 `_build_agent` 构造 LLMClient 时**并未传入**（router.py:518-523）。→ agentic 审查者若需要 temperature 控制，需先扩展 LLM 栈。
- **tools 参数支持**：是，`tools: list | None`，透传 OpenAI/Anthropic tool-call 路径（llm_wrapper.py:120、openai_client.py:301）。

### 1.3 ReflectionModelPort（同步封装 + 事件循环兼容）
- 定义：`reflection.py:218-417`。构造参数 `provider/model/api_key/api_base/timeout_sec=300.0`（reflection.py:232-245）。
- **同步** `complete_with_function_call(*, system_prompt, user_prompt, tools: list[dict])`（reflection.py:250-256）：
  - 每次调用**新建** LLMClient（reflection.py:296-301，注释 280-286 说明 per-call 资源生命周期）；
  - messages = [system, user]（reflection.py:302-305）；`await client.generate(messages, tools)`（reflection.py:307-308）；
  - 无运行中 event loop → `asyncio.run(asyncio.wait_for(_call(), timeout=self.timeout_sec))`（reflection.py:310-312、314-319）；
  - 有运行中 event loop（生产 terminal 路径）→ `_invoke_off_thread`：daemon 线程 + queue 传结果 + `thread.join(timeout=timeout_sec+5.0)`（reflection.py:320-324、369-406，超时抛 TimeoutError 走 fail-closed）。
- **function-call 解析**（reflection.py:335-363）：
  - 遍历 `response.tool_calls`，只接受 name == tools[0] 的 function（336-346）；arguments 必须已是 dict（OpenAI client 已 `json.loads`，openai_client.py:235）；
  - 取 `arguments.get("memories", arguments)` 作为 `function_call` 载荷（reflection.py:355），返回 `{"function_call": payload, "finish_reason": "tool_calls", "usage": usage}`（356-360）；
  - 无匹配 tool call → 返回 `{"content": ..., "finish_reason": "stop", "usage": usage}`（362-363）→ validator 因缺 function_call 拒绝（fail closed）；
  - 任何异常/缺 key → `_fail_closed`（reflection.py:364-367、408-410）→ 返回无 function_call 的 dict。
- 只支持**单轮一次工具输出**（解析第一个匹配的 tool call），没有把 tool result 回喂模型的多轮路径。

### 1.4 现有单轮调用点
- **唯一模型调用点**：`reflection.py:860-864`（`run_reflection` 内）：
  ```python
  response = model_port.complete_with_function_call(
      system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt, tools=[_REFLECTION_TOOL],
  )
  ```
  - 上下文：窗口构建 810-817 → claim 827-843 → **model call 严格在 SQLite 事务外**（858-864）→ 校验 865 → trace 866-876 → rejected 分支 877-889 → per-candidate 发布 890-923 → completed 924-934。
  - `_SYSTEM_PROMPT`（reflection.py:672-679）、`_build_user_prompt`（682-689，payload = scope_id/memory_revision/window_truncated/events 的 canonical JSON）、工具 schema `_REFLECTION_TOOL = record_long_term_memories`（617-670，memories 数组，字段 memory_key/kind/statement/confidence/source_refs）。
- 另有一个滚动路径的二次封装：`sar_orch/long_term_reflection.py:226-235`（`_rolling_worker` 调 `_run_reflection`），最终仍落在 `reflection.py:860` 这一个调用点。

---

## 2. 现有 Agent 内核的 agentic 循环

### 2.1 循环结构（router_agent 版，`src/Agent/router_agent/agent.py`）
`async def run(...)`（agent.py:429-836）：
1. 取消/钩子检查：`while step < self.max_steps:`（481）；每步开头 `_check_cancelled()`（483-490）；`hooks.should_continue(self, step)` 可提前终止（493-509）。
2. LLM 调用：`response = await self.llm.generate(messages=messages_for_llm, tools=tool_list)`（536-538）；hooks 非空时消息由 `hooks.pre_llm` 重写（526-528）。
3. assistant 消息入历史（591-597）。
4. **无 tool_calls 即退出**（622-668）：hooks.should_continue 决定是否继续；`require_explicit_completion=True` 时注入 nudge（649-661、838-846，最多 3 次 nudge）。
5. 工具执行：`for tool_call in response.tool_calls:`（681）→ `tool.execute(**arguments)`（731-732）→ 失败转 ToolResult + 脱敏（733-755）→ tool 消息入历史（781-789）。
6. `step += 1`（828）；max_steps 耗尽 → 失败返回（830-836）。
- **控制变量**：`max_steps`（构造默认 50，agent.py:63、98）、`cancel_event`（101-102）、hooks（`AgentHooks` protocol，hooks.py:16-54）、`require_explicit_completion`（110）。
- worker 侧同构：`src/Agent/worker_agent/agent.py:444`（`async def run`）、`while step < self.max_steps:`（worker_agent/agent.py:499）。

### 2.2 A2A/coordinator 耦合点（判断可复用性）
- 循环本体是**通用**的；耦合来自 hooks：
  - `attach_context`（agent.py:156-167）注入 `ContextManager` + `CoordinatorSARHooks`；
  - `CoordinatorSARHooks`（hooks.py:57-113）：`pre_llm` 调 `ctx.prepare_runtime_state/refresh_runtime_state/prune_history/assemble`（hooks.py:70-84，即 SAR 状态注入）；`post_tool` 调 `ctx.observe`（96-109）；`should_continue` 返回 `not agent._task_complete`（111-113）。
  - **hooks=None 时**循环退化为纯工具循环 + 内置 token 摘要（agent.py:514-515）——这是可复用的形态。
- 生产 coordinator 路径：`RouterAgent._build_agent`（router.py:505-560，工具 = QueryWorkersTool + custom + extra_tools，**不注入 AssignTaskTool**，529-535）→ `route_dag`（router.py:391）→ `CoordinatorAgentExecutor`（`src/a2a/coordinator/agent_executor.py:98+`）→ A2A 协议任务驱动（`orchestration_timeout=1200`，coordinator.py:494）。
- **结论**：`Agent.run()` 的 LLM→tool→result→LLM 循环（agent.py:481-828）不绑死 A2A，诊断审查者可以复用「hooks=None 的裸 Agent + 只读查询工具 + 显式完成工具」形态；但注意三点：① 循环是 **async**，与反射现有同步 `ReflectionModelPort`（asyncio.run/off-thread，reflection.py:307-324）是两套执行模型，接入滚动 daemon 线程（long_term_reflection.py:274-278）需自建 event loop 或复用 off-thread 模式；② Agent.run 的退出条件是无 tool_calls/纯文本，审查者需要「最后一轮强制走 function-call 输出」的变体（现在靠 validator 拒绝兜底，reflection.py:134-146）；③ `ReflectionModelPort` 的「每轮新建 LLMClient」模式（reflection.py:280-286、296-301）在 N 轮循环里意味着 N 个 client/pool，可接受但无复用。

---

## 3. 只读查询工具集现状

### 3.1 现有 query 类工具清单
| 工具 | 文件 | 查什么 | 注册状态 |
|---|---|---|---|
| `QuerySemanticMapTool` | `sar_orch/tools/coordinator/query_semantic_map.py:7-21` | `SemanticMapStore.snapshot()`（语义图全量） | **未注册**（生产代码无实例化点；仅 docs/tests 出现） |
| `QueryTeamStatusTool` | `sar_orch/tools/coordinator/query_team_status.py:7-26` | SemanticMapStore.snapshot() 子集（workers/recent_observations/stale_entries/conflicts） | **未注册**（同上） |
| `QuerySARStateTool` | `sar_orch/tools/coordinator/query_sar_state.py:8-34` | `barrier.get_env_snapshot()`（**oracle 环境状态**） | 仅 `--mode oracle` 注册（coordinator.py:460-462） |
| `QuerySharedMemoryTool` | `sar_orch/tools/worker/query_shared_memory.py:12-26` | HTTP GET `{semantic_map_url}/semantic-map` | **DEPRECATED**（文件头 1-5 行声明，worker 侧退役） |

- 注册证据：生产 coordinator 的 `extra_tools` 只在 oracle 模式追加 QuerySARStateTool（`sar_orch/coordinator.py:460-462`）；semantic 模式下 `query_semantic_map`/`query_team_status` **未进入 extra_tools**（AGENTS.md:155「no longer registered as LLM-visible tools in semantic mode (debug/fallback)」、AGENTS.md:223 同述）；全仓 `QuerySemanticMapTool(` / `QueryTeamStatusTool(` 的实例化点仅存在于 `docs/`、`tests/test_semantic_tools.py:23,45`、`tests/test_coordinator_semantic_mode.py:94`。
- semantic 模式的替代注入：`SARCoordinatorStateProvider` 每轮 LLM 前把语义图/团队/任务状态投影进 Context（AGENTS.md:155、158；hooks.py:70-84 的 pre_llm 链）。

### 3.2 诊断审查者四类只读查询的现状（投影/temporal/supervision/control journal）
现有最接近「只读查询面」的是 `MemoryReadPort`（`sar_orch/environment_state_provider.py:76-196`，构造需 store + scope_id，79-93）与 `ControlPlaneReadPort`（199-240）：

| 类别 | 数据源 | 现成读取 API | 是否已有「LLM 可见工具」 |
|---|---|---|---|
| 投影（projection） | `projection_field` 表（store.py:203-222）+ `projection_outcome`（225） | `MemoryReadPort.spatial_snapshot()`（environment_state_provider.py:109-111）、`embodied_snapshot()`（113-115）、`_group_projection_fields`（117-137，读 `store.projection_fields`） | 无 |
| temporal 流水 | `temporal_event` 表（store.py:134-155） | `MemoryReadPort.temporal_events(after_sequence)`（environment_state_provider.py:139-144）→ `store.temporal_events`（store.py:834-841）；`latest_sequence`（105-107） | 无 |
| supervision | temporal_event 中 `supervision.%` 事件（ingestor.py:929-933，由 `SupervisionEventAdapter` 写入，857-899）；另有 `SupervisionStateStore`（`src/a2a/coordinator/supervision_state_store.py:110+`，NDJSON 持久化，非 sqlite） | 计数：`store.supervision_event_count(scope_id)`（store.py:952-961）；事件本体可经 temporal_events 过滤 | 无 |
| control journal | `control_transition_journal` 表（store.py:107-119，写入 `record_journal_entry` 496-516） | `store.control_journal_entries(context_id/runtime_epoch/dispatch_id)`（store.py:518-528）；另有内存态 `ControlPlaneReadPort.task_views()`（environment_state_provider.py:209-231，读 MissionRuntime dispatches） | 无 |

- **结论**：四类数据的**读取层全部已存在**（集中在 MemoryStore 读 API + MemoryReadPort 封装，且 MemoryReadPort 已有 ACL 化的 viewer 语义，environment_state_provider.py:243-249）；**缺的是「把这些读 API 包装成诊断审查者可见的工具」这一层**——现有一个 query 工具都没有注册给任何 agentic 循环（除 oracle 模式的 QuerySARStateTool，且它读的是 oracle 环境快照，对反射/诊断而言是 forbidden truth 来源，contracts.py:87-106）。注意 MemoryReadPort 位于 `sar_orch/`（依赖 SAR 组装），`a2a/coordinator/memory/` 内核层没有同等的只读封装。

---

## 4. 诊断产出的候选落点

### 4.1 temporal_event 之外可放诊断的表（现有）
- `MemoryStore`（`src/a2a/coordinator/memory/store.py`）全部表：`memory_scope`(80)、`memory_revision`(89)、`memory_relation`(93)、`control_transition_journal`(107)、`security_audit`(120)、`callback_nonce`(127)、`temporal_event`(134)、`idempotency_ledger`(158)、`control_receipt`(168)、`memory_outbox`(177)、`spatial_entity`(187)、`embodied_node`(195)、`projection_field`(203)、`projection_outcome`(225)、`memory_view_revision`(238)。
- `LongTermMemoryStore`（`src/a2a/coordinator/memory/long_term.py`）：`long_term_revision`(96-102)、`reflection_run`(103-119)、`long_term_memory`(122-138)、`long_term_support`(141-148)、**`long_term_audit`(149-155，字段 id/kind/reason/digest_prefix/at)** —— long_term_audit 是现成可放「诊断摘要行」的泛型表（`record_audit` 写入，long_term.py:848-861）；`reflection_run` 表（103-119，含 status CHECK 枚举 pending/completed/rejected/failed/timeout，112）可扩展 status 或加诊断 run 行。

### 4.2 独立诊断 sqlite 的模板：LongTermMemoryStore 建库/建表/migration 模式
（`src/a2a/coordinator/memory/long_term.py`，完全可作独立 diagnosis store 模板）
- 路径：`LongTermMemoryConfig` 派生 `<memory_root>/long_term/long_term.sqlite3`（contracts.py:720-728）；生产构造点 `LongTermMemoryStore(lt_config.long_term_db_path).open()`（coordinator.py:392-404）。
- `LongTermMemoryStore.__init__`（long_term.py:204-208）：db_path + conn + `threading.RLock`。
- `open()`（216-257）：绝对路径校验（227-231）→ `mkdir 0o700` + chmod（232-236）→ `sqlite3.connect(isolation_level=None, check_same_thread=False)` + row_factory（237-240）→ 文件 chmod 0o600（241-244）→ `PRAGMA journal_mode=WAL / synchronous=FULL / foreign_keys=ON / busy_timeout=50`（245-248，busy 常量 54-58）→ `_ensure_version_table` + `_run_migrations`（250-252）。
- 版本表：`schema_migrations(version PK, applied_at, migration_sha256)`（274-303）；「有业务表但无版本表」fail-closed 拒开（287-296）。
- Migration 模型：`_Migration` dataclass（78-92，digest = statements 拼接的 sha256，87-92）；`_MIGRATIONS: dict[int, _Migration]`（160-162）；v1 的 5 条建表语句在 `_MIGRATION_001_STATEMENTS`（95-156）。
- `_run_migrations()`（313-379）：先校验已应用版本（未知版本拒开 327-334、digest 不匹配拒开 335-343），再逐个 `BEGIN IMMEDIATE` 事务应用 + 写版本表（346-362）；失败回滚并抛 typed `LongTermStoreError`（363-377）。
- 事务助手：`_immediate()`（415-424+，BEGIN IMMEDIATE + 12 次有界重试，58 行）；写 API 全部短事务（claim_reflection_run 453、publish_memory 573 等）。
- **结论**：独立诊断 store 可直接照抄该模式（新文件如 `diagnosis.sqlite3` + 自己的 `_MIGRATIONS` v1），或更轻量地在现有 `long_term_audit` 表加诊断行；temporal_event 之外无现成「诊断」专用表。

---

## 5. 触发/调度面

### 5.1 long_term_reflection.py 的 trigger/drain/coalesce 与并发保护
（`sar_orch/long_term_reflection.py`）
- 模块级运行时：`_runtime` dict（73-81）、`_inflight_lock = threading.Lock()`（83）、`_inflight_thread`（84）、`_inflight`（85-88）。
- `configure_long_term_runtime`（91-108）：注入 store/snapshot_provider/project_id/policy_version/model_port/config，全部在锁内。
- `_rolling_worker`（183-246）：快照 → 窗口 → `_run_reflection`（226-235），结果写入 `_inflight`（236-243）；异常 → `failed`（244-246）。
- `maybe_trigger_rolling_reflection`（255-279）：**coalesce（防重入）** = `_inflight["active"]` 为真 → 返回 `coalesced_skip`（267-268）；**min-interval 节流** = `now - last_triggered_at < config.min_interval_sec` → `coalesced_skip`（269-270，默认 30s，contracts.py:770）；通过则起 daemon 线程 `long-term-rolling-reflection`（274-278）。**同一时刻至多一个滚动反射在飞**。
- `drain_inflight_reflection(timeout_sec=60)`（284-299）：`thread.join(timeout=timeout_sec)`（296）；仍存活 → `DrainResult(status="timeout")`（297-298），**不阻塞 run 退出**，由调用方记 typed audit（experiment.py:303-311）。

### 5.2 触发点
- 滚动触发：`sar_orch/experiment.py:810-832` —— task complete（818-821）/ every N env steps（822-827，默认 5，contracts.py:769）/ supervision 事件计数变化（828-832，`coordinator.long_term_supervision_count()` 定义在 coordinator.py:761-770，内部查 store.supervision_event_count）。
- terminal 收尾：`experiment.py:298-344` —— 先 drain（299-301）→ terminal snapshot（314-320）→ **同步** `reflection_run`（335-340，阻塞 run 退出）。
- `reflection_sec=60` 配置位：`LongTermRuntimeConfig.reflection_sec: int = 60`（contracts.py:772）；ini 映射 `("[timeout]", "reflection_sec")`（contracts.py:787）；配置文件 `long_term.config`（experiment.py:574-575 加载，contracts.py:791+ 解析）。

### 5.3 agentic 多轮审查会撞什么
1. **terminal drain 60s 必超时**：滚动审查若变多轮，单轮模型调用上限已 300s（reflection.py:239，`timeout_sec=300.0`），N 轮 >> 60s → `drain_inflight_reflection` 必返回 `timeout`（long_term_reflection.py:296-298），实验结束时不等待、审查结果丢弃（只记 audit，experiment.py:303-311）。`[timeout] reflection_sec=60` 需随审查轮数上调（contracts.py:787）。
2. **terminal 同步反射阻塞 run 退出**：experiment.py:335-340 的 `reflection_run` 在 run terminal 同步执行；多轮审查直接叠加到退出路径。现有单轮已靠 off-thread + `timeout_sec+5` join 保底（reflection.py:369-406），多轮需把「总预算」而非单轮 timeout 作为控制量。
3. **coalesce 粒度是「一个滚动反射」**：agentic 多轮期间若实验继续推进、触发点再次命中，`maybe_trigger_rolling_reflection` 因 `_inflight["active"]` 直接 `coalesced_skip`（267-268）——窗口事件会积压到下一轮，多轮审查的「审查期间新事件」语义需要明确（继续消费 or 跳过）。
4. **ReflectionModelPort 是同步 API**：滚动线程里无 event loop（asyncio.run 路径，reflection.py:310-312），若诊断审查者要复用 Agent.run()（async），需要在滚动线程里起事件循环，或把审查器整体做成 async 任务放进 coordinator 的 event loop（后者与现有 daemon 线程模型不同，需评估与实验 poll 循环的并发）。
5. **min_interval_sec=30**（contracts.py:770）与多轮审查时长无直接冲突（coalesce 已挡住并发），但 last_triggered_at 在启动时记录（271），审查超长时下一次触发会被 min-interval 之外的因素（active 标志）挡住，无额外问题。

---

## 6. 输出契约复用面（validate_reflection_response）

### 6.1 可直接复用的校验（`reflection.py:113-215`）
- response 形状 + function_call 存在性 + 单/批归一化：`reflection.py:134-146`（`invalid_response_shape`/`missing_function_call`/`invalid_function_call_shape`）。
- 批内 memory_key 去重：`reflection.py:162-163`（`duplicate_memory_key`）。
- kind 白名单：`reflection.py:164-166`（`kind not in LONG_TERM_MEMORY_KINDS` → `invalid_kind`）。
- statement 非空 + canonicalize + FORBIDDEN_TRUTH_TERMS 扫描：`reflection.py:167-174`（`empty_statement`/`forbidden_truth_term`；词表 contracts.py:87-106）。
- source_refs 形状 + 窗口可验证性（无窗口拒非空 refs；ref 不在窗口 → `forged_source_ref`）：`reflection.py:175-193`。
- confidence 类型 + DTO fail-closed 校验（含 [0,1] 越界）：`reflection.py:196-211`。
- 结果类型 `ReflectionValidationResult(status, long_term_memory_written=0, reason, candidates)`（reflection.py:64-76）。
- **这些是纯函数、无副作用**，诊断输出（若字段兼容）可直接复用整段。

### 6.2 诊断字段若要走同一条 validator 需要改哪些行
- **前提冲突：kind 枚举**。`LONG_TERM_MEMORY_KINDS = ("strategy","lesson","hazard","pattern","status")`（contracts.py:590-596）。诊断若是新 kind（如 `diagnosis`），走同一条 validator 需同步改 4 处，否则 validator 会拒：
  1. `contracts.py:590-596` —— 扩展 `LONG_TERM_MEMORY_KINDS`；
  2. `long_term.py:128-129` —— `long_term_memory.kind` 表的 `CHECK(kind IN (...))` 约束；
  3. `reflection.py:641-643` —— `_REFLECTION_TOOL` 参数 enum；
  4. `reflection.py:672-679` —— `_SYSTEM_PROMPT` 的 kind 列表。
  （若诊断独立成 `kind=diagnosis` 但**不落 long_term_memory 表**、而落独立 diagnosis 表/audit 表，则 2 可不改，但 1/3/4 仍会让「同一条 validator」接受诊断 kind。）
- **字段集不同则必须改**：`_CANDIDATE_FIELDS = ("memory_key","kind","statement","confidence","source_refs")`（reflection.py:61，校验点 154-158）；诊断若带 severity/recommendation/affected_scope 等新字段，validator 的缺失检查（157-158）会拒（多字段则被 DTO `validate()` 忽略或需新 DTO，201-211）。→ 更干净的做法是**独立 validator/DTO + 独立工具 schema**（模板即 reflection.py:617-670），复用 `reflection.py:134-146/162-163/167-174/175-193` 的纯校验函数。
- **工具输出包装**：`arguments.get("memories", arguments)`（reflection.py:355）——诊断若用新工具名（如 `record_diagnosis`），`complete_with_function_call` 的「匹配 tools[0] 名字 + 解包」逻辑（336-356）**无需改**（它是按 tools[0] 动态匹配的），只需换工具 schema。
- **消费方**：诊断若走 long_term_memory 表，现有 coordinator 注入链已就绪（`environment_state_provider.py:392-407` 的 `_long_term_section` + `environment_state.py:184-232` 渲染 + budget 阈值 environment_state_provider.py:64-70）；若走独立 diagnosis store，需新增 read port 方法 + 渲染段 + ACL（对照 `_is_system and mode=="read"` 门，environment_state_provider.py:374-381）。

---

## 7. 关键结论速览（给设计决策）
1. LLM 调用栈：同步 port（asyncio.run/off-thread）包 async client；无 temperature 支持；每轮新建 client；单轮单 tool-call，无多轮回喂。
2. Agent.run() 循环通用可复用（hooks=None 形态），但 async 执行模型与滚动 daemon 线程/同步 port 不匹配，且缺「强制 function-call 收尾」的退出变体。
3. 四类只读查询的数据读取层全部存在（MemoryStore + MemoryReadPort），缺工具封装与注册；现有 query 工具全部退役/未注册。
4. 诊断落点：可复用 LongTermMemoryStore 的独立建库/migration 模板（long_term.py:194-379），或轻量落 `long_term_audit`（long_term.py:149-155）。
5. 调度冲突核心：terminal drain 60s（contracts.py:772/787、experiment.py:299-301）vs 多轮审查分钟级耗时；coalesce 单飞（long_term_reflection.py:267-268）语义需重定义；terminal 同步反射阻塞退出（experiment.py:335-340）。
6. 输出契约：validator 纯校验段可整体复用（reflection.py:134-211），但 kind 枚举扩展需 4 处同步（contracts.py:590-596 / long_term.py:128-129 / reflection.py:641-643 / 672-679）；新字段建议独立 validator + 新工具 schema。

## 8. 未验证/待确认项
- `SARCoordinatorStateProvider` 内部如何构造/持有 `MemoryReadPort` 的精确行号（已确认 provider 构造时接收 store + mode，coordinator.py:406-420；MemoryReadPort 定义 environment_state_provider.py:76-196，构造点未逐行追）。
- `TaskWatchdog` 的 supervision 事件源头的文件行号（确认了写入端 `SupervisionEventAdapter`，ingestor.py:857-899；TaskWatchdog 本体位置未追）。
- 多轮审查若复用 `Agent.run()`，其内部 `_summarize_messages`（agent.py:263-355）与 hooks 摘要在只读工具循环下的行为未实测。
- `long_term.config` 仓库根配置文件当前实际内容（是否有 `[timeout] reflection_sec` 覆盖值）未读（只验证了解析面 contracts.py:776-788）。
