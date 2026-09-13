# LLaMAR Agent 轨迹落地审计与框架优化探索（2026-09-09）

> 综合报告（B1）。事实基础 = 四份组件盘点（A1 Coordinator / A2 Worker / A3 环境 / A4 横切），存 `docs/plans/trajectory-audit/components/`。
> 基线声明（编排放置：以代码为准）：工作区实测 HEAD = **45299ce**（2026-09-03，`git rev-parse --short HEAD` 实测）；A2/A4 报告头部写 `main@3ad6980`，经 `git merge-base --is-ancestor 3ad6980 HEAD` 核验该 commit 不在 LLaMAR 历史中，属笔误，不影响任何行号级证据（全部 file:line 均为工作区实测）。
> 样例 run 主锚点：`sar_orch/results/20260721_201943_s3_s42_a4/`（scene3/4 agents/seed42/30 步，code_commit=0119d39，产物时代=2026-07）。

## 0. 一页结论（TL;DR）

**三问总回答：Q1 完整轨迹 = 部分；Q2 交互记录 = 部分；Q3 逐步状态 = 部分。** 最厚的一层是 LLM 交互（coordinator 71 轮完整 prompt 全文、worker 323 request / 311 response / 441 tool_result 全量落盘，均有真实产物验证）；最薄的一层是环境全量状态（唯一逐步落盘器 truth_recorder 从未启用，persons 坐标/reservoir 水量连字段都没有）与"裁剪前原文、诊断输入"类中间态（零轨迹）。

最关键的 5 个发现：

1. **全部现有产物都是旧代码样本**：含 router CSV 的 77 个 run 全部早于 Phase 5 与新版 Context（code_commit 共 11 种，0119d39 仅为 07-21 两个抽样 run；CSV 实测 65 个 7 列 + 12 个 3 列、9 列零样本），9 列 router_interactions、thinking、ENVIRONMENT STATE 注入块、read_port、long-term、diagnosis、memory SQLite 系**全部零真实样本**（A1-G1；A4-G8）。任何数据驱动优化都必须先跑新基线，否则分析对象是旧框架。
2. **环境真值底座是空的**：truth_recorder 设计上是唯一"每步全量环境状态"落盘器（truth_recorder.py:184-237），但 opt-in 且全仓零产物；且即便启用，persons 坐标（barrier.py:360-363 快照有、无落盘路径）与 reservoir 水量（快照/truth 均无字段）依然缺失。救援任务核心进度"人救没救、在哪"目前无法离线回答（A3 离线重建实测）。
3. **coordinator 决策黄金数据是真实存在的**：llm_request 完整 messages（含注入块全文）与 token_usage.csv 71 轮一一对应（A1 §2 实测），决策上下文可逐轮还原——这是四张卡里最可用的资产。
4. **ContextManager 裁剪/压缩链路零轨迹**：Phase 3 `compress_with_llm` 全仓无调用点（死代码，context.py:580 仅定义）；Phase 1 裁剪静默执行无日志（:477-529）；被裁原文不另存（:836-848）。"上下文策略如何影响决策"类分析当前完全不可做。
5. **记忆/诊断系统"代码完备、零样例、不可审计"三合一**：memory/long_term/diagnosis 三个 SQLite 系全 results 零真实样例（A4-G8）；诊断每轮 LLM 输入 transcript 只存内存（diagnosis_loop.py:246）、rolling 中间态被内存覆盖（long_term_reflection.py:377-392）——记忆效果评估需先开模式跑样 + 补审计点。

## 1. 总览矩阵

| 组件域 | Q1 完整轨迹 | Q2 交互记录 | Q3 状态/环境逐步保存 | 综合评级 | 关键证据 |
|---|---|---|---|---|---|
| A1 Coordinator 侧 | **有** | 部分 | 部分 | **B** | 每轮 LLM 完整 prompt 全文落盘（AgentLogger `llm_request`，agent.py:531-533；产物 unnamed_task.ndjson 71 条 llm_request 与 token_usage.csv Coordinator 71 行一一对应，A1 §2）；但 activate_plan_node/update_plan 不进 router_interactions.csv（coordinator.py:251-264、:391-404）、snapshot 永不触发（controller.py:274） |
| A2 Worker 侧 | 部分 | 部分 | 部分 | **B-** | 三通道交叉可还原每步 LLM 输入输出与工具调用（ndjson 323/311/441 + csv 441 + trajectory 30 步，A2 §2 实测）；缺 tool_start 事件（logger.py 仅 3 类 log_*）、llm_request 记录的是裁剪后 messages（agent.py:546→549）、被裁原文不另存（context.py:851-906） |
| A3 环境侧 | 部分 | 部分 | 部分 | **C+** | 行为轨迹逐步全量（trajectory.csv 30 步 Actions/Observations 全文，logger.py:177-196）；离线重建实测：agent 位置/库存 100% 可提取、fire 熄灭可定位下界；**persons 坐标、reservoir 水量、全量网格永不落盘**（SAR/env.py:282-361、barrier.py:360-366）；truth_recorder 零产物（A3 §Q6） |
| A4 横切组件 | 部分 | 部分 | 部分 | **C** | 事件面有真实数据（events.ndjson 79 行、EventStore 92 条、supervision 状态行 812 行——见下方编排放置）；但 memory/long_term/diagnosis SQLite 系全 results 零真实样例（仅代码证据）；诊断输入只内存（diagnosis_loop.py:246）、ContextManager 裁剪/压缩零轨迹（context.py:477-529/:580） |

**编排放置 1（以产物为准）**：A4 报告称"20260721 样例 run 无 supervision_*.ndjson"——实测该 run `coordinator/` 下存在 28 个 `supervision_dsp_*.ndjson` 共 812 行（A1 的"×28"正确，A4 的 Gap#6 该子项不成立）。A4 Gap#6 另一子项（落盘位置在 `coordinator/` 而非文档声明的 `<run>/supervision/` 子目录，后者仅建空目录 experiment.py:448-459）成立。
**编排放置 2（以产物为准）**：supervision_*.ndjson 的 schema 为 SupervisionStateStore 全状态行 `{ts, state:{dispatch_id, supervision_state, active_alerts, acknowledged_event_ids, terminal, …}}`（supervision_state_store.py:188-208，产物头 600 字节实测），**不是** A1 所说的"同上 EventStore schema"（EventStore 行为 `{ts, task_id, context_id, event_type, state, text[:500]}`，event_store.py:106-125 实测）。两个文件系是两套 schema，勿混。

评级说明（A-F）：A1 核心链路（LLM 全文 + token + dispatch 参数）全部有真实产物验证，评级最高；A4 六组件中三个 SQLite 系只有代码证据、ContextManager 链路零轨迹，评级最低。四者无一达到"有/有/有"，均无 A 级。

## 2. Gap 总清单（合并四卡去重，按影响分级）

### 高（7 条）

| # | Gap | 来源卡 | 证据 |
|---|---|---|---|
| H1 | **样本老化**：全部现有产物（77 run，code_commit 共 11 种，0119d39 仅为 07-21 两个抽样 run）早于 Phase 5/新版 Context；9 列 router_interactions、thinking、ENVIRONMENT STATE 块、read_port、long-term、diagnosis、memory SQLite 均零真实样本；worker prompt 无内容指纹（prompt_version 恒 baseline）加剧归因困难 | A1-G1/G8、A2-G6、A4-G8 | 77 run CSV 实测 65 个 7 列 + 12 个 3 列（sar_experiment_20260704/05 早期 run，9 列零样本，A1 §3）；7/21 注入块为旧「Context Memory」格式 vs 现代码 context.py:1051；results 全扫无 sqlite（A4-G8） |
| H2 | **环境真值底座空转**：truth_recorder（唯一逐步全量状态落盘器）opt-in 且全仓零产物；persons 位置/状态无任何落盘路径（观察文本无坐标、快照有但无写盘） | A3-G4、A3-G1 | truth_recorder.py:184-237；experiment.py:476-497；SAR/env.py:282-361；barrier.py:360-363；find 全仓 0 个 truth_trace |
| H3 | **ContextManager 裁剪/压缩零轨迹**：Phase 3 compress_with_llm 死代码（全仓无调用点）；Phase 1 裁剪静默无日志；被裁原文不另存（episodic 仅内存摘要）；compression_summary.json 实际不生成 | A2-G2、A4-G3/G4/G5 | context.py:580 仅定义；:477-529；:836-848（prune_history，:848 调 _compress_phase1）；:843-847；grep 全仓无调用 |
| H4 | **诊断通道不可审计**：每轮 LLM 输入 transcript 仅内存不留盘；rolling 中间态（rejected/timeout/rounds_exhausted）被 _inflight 内存覆盖，仅 terminal drain 末轮进 run_metrics.json；同 key 新结论 INSERT OR IGNORE 吞掉 | A4-G1/G2/G10 | diagnosis_loop.py:246、368-392；long_term_reflection.py:377-392；diagnosis.py:671-690 |
| H5 | **dispatch 事件面断链**：activate_plan_node/update_plan 不进 router_interactions.csv（update_plan 的 decision event 还依赖默认关闭的 memory ingestor）；events.ndjson 类型粒度不足（activate 与 fallback 共用 send_message） | A1-G2、A4-G9 | coordinator.py:251-264、:391-404、:640-673；产物 activate 21 次仅 events.ndjson |
| H6 | **观测→语义地图链不完整**：EventStore 落盘丢弃 observation 结构化字段（仅 text[:500]）；semantic_map.jsonl 非每 run 必产物（7/21 无、7/19 有）且仅写 noteworthy 事件、init_priors 不落 | A1-G3/G9、A3-G5 | event_store.py:106-125；store.py:334-336、187-210；产物对比 7/19 vs 7/21 |
| H7 | **coordinator 侧 snapshot 永不落盘**（暂停/恢复现场不可重建）+ AgentLogger 文件名恒 `unnamed_task.ndjson` | A1-G4/G6 | controller.py:274 守卫 task_id；agent_executor.py:396-403 不传 task_id；logger.py:69 |

### 中（8 条）

| # | Gap | 来源卡 | 证据 |
|---|---|---|---|
| M1 | fire/flammable 强度仅枚举（Low/Medium/High/None）且扑灭后从可见列表消失——无法区分"已扑灭"与"未观测"，蔓延过程不可重建 | A3-G6 | SAR/env.py:347、354；实测 step11 后 EmberFire_Region_1 消失 |
| M2 | scene config（初始网格/对象布局）无快照，跨 run 场景可比性缺基准 | A3-G7 | experiment_design.md:438 已自标；metadata.json 无 scene 内容 |
| M3 | reservoir 剩余水量无任何字段（文本观察/快照/truth 均无） | A3-G2 | barrier.py:364-366；truth_recorder.py:309-314 |
| M4 | auto-noop 无显式来源标记（空闲心跳 vs 超时注入 vs LLM 主动需三处差值推导） | A2-G3 | worker.py:533-552；barrier.py:243-247；实测 15 NoOp=8 LLM+7 系统侧 |
| M5 | csv LLMInput 仅最近 6 条×200 字符摘要，按 csv 快速统计会低估输入上下文 | A2-G1 | worker.py:287-292 |
| M6 | supervision 落盘位置与文档不符（实际 `coordinator/`，文档声明 `<run>/supervision/` 子目录为空）；文件名 schema 跨版本变化（7/19 `supervision_<id>` vs 7/21 `supervision_dsp_<id>`） | A4-G6（"07-21 未落盘"子项经核验不成立，见编排放置 1） | server.py:1168；supervision_state_store.py:188-208；experiment.py:448-459 |
| M7 | watchdog 事件"注入 Context/runtime state"环节无独立审计记录，只能从 llm_request 完整 messages 反推 | A4-G7 | task_watchdog.py:79-108；CoordinatorPinnedState.supervision（contextmanager.md §5.1） |
| M8 | subtasks.csv 只写 assigned 快照，无 completed/failed 终态更新（log_subtask 全仓唯一调用点） | A1-G5 | coordinator.py:181；产物 30 行全 assigned |

### 低（4 条）

| # | Gap | 来源卡 | 证据 |
|---|---|---|---|
| L1 | AgentLogger 无 tool_start 事件：ndjson 无法独立重建工具时序，仅 csv ToolLatencyMs | A2-G4 | worker_agent/logger.py 仅 3 类 log_*；worker.py:319-321 |
| L2 | token 只覆盖成功收到 usage 的响应（323 request vs 311 usage），LLM 异常/NeedInput 路径无 token 记录 | A2-G5 | agent.py:557-572、752-790 直接 return |
| L3 | oracle 模式（query_sar_state/记录点 2b/finish_task 路径）零产物样本，从未跑过 | A1-G7 | 全部 run oracle_mode=false |
| L4 | A2A [DATA] 块截断（2000/1000/12000）且 worker 侧无推送镜像文件——原文已在 ndjson/csv，不影响本侧还原 | A2-G7 | sink.py:59、83、105 |

合计 **19 条**（高 7 / 中 8 / 低 4）。四卡原始 gap 数 9+7+7+10=33，去重合并后 19。

## 3. 轨迹体系自身优化建议（补短板，按性价比排序）

每条 = 改什么模块 / 大约改什么 / 解决哪个 Gap。性价比判断：现成能力接线 + 几行改动 > 新落盘点 > 补审计。

1. **TruthRecorder 默认接线 + 补字段**（experiment.py:476-497 给默认输出目录或 run 配置显式开启；truth_recorder.py:257-319 补 persons 坐标落盘、reservoir 水量字段；barrier.py:360-366 快照字段同步）→ H2、M3。代码全部现成，只差开关与两个字段；一次改动为环境回放、记忆投影评测、语义地图对照提供真值底座，收益最大。
2. **EventStore 补 observation 字段**（event_store.py:106-125 的写入 dict 增加结构化 observation 键或完整 text）→ H6。几行改动恢复观测证据在事件流的可追溯性。
3. **router_interactions.csv 补 activate/update_plan 行**（coordinator.py:251-264、:391-404 增加 log_router_interaction 调用）→ H5。dispatch 四类全进 CSV 层。
4. **AgentLogger 增加 log_tool_start**（worker_agent/logger.py 加方法与写入；agent.py 工具执行前调用）→ L1。ndjson 侧独立重建工具时序。
5. **诊断 transcript 落盘 + rolling 中间态逐轮 append**（diagnosis_loop.py:246 把四件工具证据视图写 diagnosis/ 目录 ndjson；long_term_reflection.py:377-392 中间态改追加式记录）→ H4。诊断"看到了什么→得出什么"可审计。
6. **ContextManager 补 instrumentation**（context.py:477-529 Phase 1 裁剪写日志/计数；:836-848 被裁消息写 discard 文件；Phase 3 若启用则补触发 token 元数据）→ H3。上下文策略效果可测。
7. **coordinator executor 传 task_id**（agent_executor.py:396-403 submit 传 dispatch/任务 id）→ H7。一举修复 snapshot 永不落盘 + unnamed_task.ndjson 文件名问题。
8. **auto-noop 来源标记**（worker.py:533-552 与 barrier.py:243-247 的 NoOp 提交带来源字段/独立事件）→ M4。
9. **supervision 落盘位置统一**（server.py:1168 改为 `<run>/supervision/` 子目录并删除空目录创建，或修订文档口径）→ M6。
10. **subtasks.csv 终态更新**（coordinator 侧 finish/cancel 分支补 log_subtask 调用点）→ M8。
11. **worker prompt 内容指纹**（experiment.py 写 metadata 时对 prompts/worker/system.md 加 sha256）→ H1 归因部分。
12. **token 异常路径补记**（agent.py:557-572、752-790 补 usage=None/异常标记行）→ L2。
13. **scene config 快照**（env 初始化时 dump 初始网格/对象布局到 run 目录）→ M2。
14. **diagnosis 表 revision 演进**（diagnosis.py:671-690 的 INSERT OR IGNORE 改 upsert + revision 列，旧行保留）→ H4 演进丢失部分。

建议实施顺序：1-4 为"第一批"（改动小、解除最多下游分析的阻塞），5-6 为"第二批"（中等改动、高 gap），7-14 随近期功能改动顺手做。

## 4. 基于轨迹的框架优化探索路径（核心章节）

逐方向小卡：现有是否具备的结论绑定四卡证据；「缺口」即第 2 节对应条目。

### 4.1 coordinator 决策质量分析（dispatch 合理性 / 步数预算意识）

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| coordinator 决策质量 | 每轮完整 prompt（含注入 Context）+ dispatch 参数与结果 + 环境逐步进度 | **具备（主数据源最强）**：llm_request 全文 71 轮与 token 一一对应（A1 §2）；assign/cancel/reply 参数在 CSV+events.ndjson（coordinator.py:169-250） | activate/update_plan 不进 CSV（H5）；新注入格式无样本（H1）；环境全量状态缺（H2） | LLM-as-judge 盲评每轮 dispatch 决策（独立 evaluator，用 llm_request 快照还原决策现场）；dispatch 频率/取消率/重复指派/工单震荡分析；注入块体积与决策质量相关性；env step 进度 vs dispatch 步数的预算意识度量 |

### 4.2 worker 行为与 prompt 优化

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| worker 行为与 prompt | 每步 LLM 输入输出全文 + 工具调用结果/延迟 + no-op 来源 + prompt 指纹 | **基本具备**：ndjson 323 request/311 response/441 tool_result 全量（A2 §2）；csv ToolLatencyMs/Success/ErrorType；trajectory Actions 全量 | no-op 来源需差值推导（M4）；裁剪前原文缺（H3）；prompt 无指纹（H1） | 工具成功率/延迟/错误类型矩阵；重复动作与"来回踱步"检测（位置序列 + action 序列）；no-op 分层占比（主动等待 vs 系统补位）；prompt 改版 A/B 归因（需先补指纹） |

### 4.3 token 效率与缓存命中分析

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| token 效率与缓存 | 每轮 token 分项（prompt/completion/cache hit/miss）+ llm_request 消息数与体积 | **具备（数据质量最高）**：token_usage.csv 与 llm_request 一一对应（A1 §2，Coordinator 71 行 + worker 440 行）；CacheHitTokens/CacheMissTokens 全列 | 异常路径 token 缺失约 4%（L2）；压缩事件当前不存在（H3） | 每轮 token 曲线与上下文增长曲线；缓存命中率时序与注入块结构的关系；prompt 体积 vs 决策质量的成本-收益；摘要触发点分析（需先启用 Phase 3 或改用 Phase 1 裁剪日志） |

### 4.4 语义地图质量与观测注入链路

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| 语义地图质量与观测链路 | observation_ingested 事件流 + 上报端 tool 参数 + 真值对照 + init_priors | **部分具备**：7/19 run 31 条 observation_ingested（A3 §Q5）；report_observation ToolArgs/Observation 完整 JSON（A2 §5.6） | 非 noteworthy 被吞、init_priors 不落、7/21 无文件（H6）；真值对照需 truth（H2） | 上报内容 vs 地图合并结果一致性；map recall/freshness（已有指标）与注入块相关性；启用 truth recorder 后跑 memory_projection_quality 评测器（experiment.py:173-254 已接线，`--truth-manifest`）量化记忆投影质量 |

### 4.5 记忆系统（长期记忆/诊断注入）效果评估

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| 记忆系统效果评估 | reflection 输入输出 + 诊断 transcript + 注入内容与决策关联 + 模式开关 metadata | **不具备（零真实样例）**：long_term/diagnosis SQLite 全 results 无产物，仅代码证据（A4 §2 组件 2/3） | 诊断输入不留盘、rolling 中间态覆盖（H4）；注入无独立审计（M7）；零样例（H1） | 先跑 read_port + long-term=read + 诊断注入样例 run（补齐零样例）；reflection_trace.ndjson 做反思质量评估；注入 on/off 决策差异对比；诊断结论与后续 dispatch 行为关联（需先做 §3.5 补点） |

### 4.6 watchdog 阈值调优

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| watchdog 阈值调优 | 监督状态时间序列 + 告警/恢复事件 + 环境进度对照 | **具备（真实数据）**：supervision_dsp_*.ndjson 28 文件 812 行全状态行（编排放置 1）；EventStore 39 条 supervision_event（TASK_STALE 带 progress_age_seconds、TASK_RECOVERED 带 recovered_from） | 注入 Context 环节无独立审计（M7）；旧产物阈值参数（stale 120s 等）与当前代码参数可能不一致（H1） | 告警-恢复配对分析；误报率评估（告警后 agent 是否实际停滞 vs 正常执行）；离线重放不同 stale_threshold 扫描（用状态行序列模拟，无需重跑）；阈值与 end_reason/timeout 的相关性 |

### 4.7 环境动态 ↔ 决策关联回放

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| 环境动态↔决策关联 | 逐步环境全量状态 + agent 行动 + coordinator 决策时序 | **部分具备**：agent 位置/库存每步可提取（A3 离线重建实测 100%）；deposit 库存演化可追溯；fire 熄灭下界可定位 | persons 坐标/reservoir 水量/全量网格/蔓延过程缺失（H2、M1、M3）；truth recorder 未启用 | coverage 曲线与 dispatch 事件对齐；火势阶段（熄灭时刻）与运输调度的因果配对；先接 truth recorder（§3.1）后做真值级回放：验证"coordinator 决策时的环境状态"与"真实状态"偏差 |

### 4.8 benchmark 跨 seed/scene 对比的轨迹支撑度

| 优化方向 | 需要的轨迹数据 | 现有是否具备 | 缺口 | 可做的分析方法 |
|---|---|---|---|---|
| benchmark 跨 run 对比 | 多 run 轨迹 + 完整元数据（scene/seed/agent 数/model/prompt 指纹/代码版本） | **部分具备**：benchmark 产物结构在（meta.json/result.json + experiment_logs，A3 §1）；77 run 历史样本 | 全部旧代码样本（H1）；scene config 无快照（M2）；部分 benchmark run 是 abort run（trajectory 仅 1 行） | coverage_auc/transport_rate 分布与方差分析；失败/abort run 的轨迹诊断（为何早期崩溃）；新基线后跨 seed 稳定性 + 新旧版本纵向对比（需 code_commit 字段已有，可直接分组） |

## 5. 建议的下一步（给用户拍板的选项清单，不实施）

| # | 选项 | 前置依赖 | 预期收益 |
|---|---|---|---|
| 1 | **新基线 run**（1-2 scene × 2-3 seed，30 步），一次带 `--truth-output-dir`、`--long-term-mode=read`、memory read_port | 无（代码现成） | 一次补齐 H1/H2/H4 的零样例：验证 9 列 CSV、thinking、ENVIRONMENT STATE 块、long-term/diagnosis/memory 产物真实形状，是所有下游分析的总前置 |
| 2 | 轨迹补短板第一批 patch（§3 建议 1-4：truth 接线 + EventStore observation + activate CSV 行 + tool_start） | 可与 #1 并行 | 解除 H2/H6/H5/L1 四个阻塞，为所有"状态回放/观测链路/dispatch 审计"类分析建立数据底座 |
| 3 | coordinator 决策质量分析试点（§4.1） | 只需 #1（旧样本也可先行探索方法） | 第一个可交付的框架优化洞察：dispatch 合理性盲评报告 |
| 4 | 语义地图 + truth 对照评测（§4.4） | #2 的 truth 接线 | 记忆投影质量量化（memory_projection_quality 评测器已接线未启用） |
| 5 | 诊断/记忆开模式样例 run + 审计补点（§3.5/3.6） | #1 | 记忆系统（长期记忆/诊断注入）效果评估从"不可做"变"可做" |
| 6 | watchdog 阈值离线调优（§4.6） | 无（07-21 产物 812 行状态行可直接用） | 阈值参数化依据 + 误报率量化，成本最低（纯离线分析） |

**综合建议**：以 #1 + #2 为启动组合（一个跑数据、一个补管道），随后 #3/#6 可并行推进；#4/#5 依赖前两者的产物。

## 6. 附录：§3 全部落地后的 run 目录目标形态

以 §3 的 14 条建议全部实施为准，单个 run 的轨迹目录目标形态（标记：`+`=新增文件，`~`=现有文件内容/schema 变化，括号为 §3 建议编号）：

```text
sar_orch/results/2026MMDD_HHMMSS_s3_s42_a4/
├── metadata.json                ~ +worker prompt sha256 指纹、truth_dir 指针（#11）
├── scene_config.json            + 初始网格/对象布局快照（#13）
├── trajectory.csv               ~ NoOp 带来源标记：LLM主动/空闲心跳/超时注入（#8）
├── agent_interactions.csv       不变
├── router_interactions.csv      ~ dispatch 四类齐全：补 activate_plan_node/update_plan 行（#3）
├── token_usage.csv              ~ 补异常/失败路径标记行（消除 323 vs 311 的 4% 缺口）（#12）
├── subtasks.csv                 ~ Status 出现终态 completed/failed/canceled（#10）
├── summary.csv / events.ndjson / run_metrics.json   不变
│
├── coordinator/
│   ├── <task_id>.ndjson         ~ 文件名不再恒 unnamed_task（#7）；事件流 +tool_start（#4）
│   ├── events_<task>.ndjson     ~ 行内恢复结构化 observation 字段（#2）
│   ├── semantic_map.jsonl       不变（noteworthy 事件流）
│   ├── snapshot_<task_id>.json  ~ INPUT_REQUIRED 时真正会出现（#7 修复守卫链）
│   ├── context/                 + ContextManager 审计（#6）
│   │   ├── prune_events.ndjson      Phase 1 裁剪触发点+计数
│   │   └── discards/                被裁消息原文（"裁剪如何影响决策"由此可分析）
│   ├── long_term/long_term.sqlite3  已有（--long-term-mode != off 时）
│   └── diagnosis/
│       ├── diagnosis.sqlite3    ~ INSERT OR IGNORE → upsert+revision 列（#14）
│       └── transcripts.ndjson   + 每轮诊断：四件工具证据视图+结论+rolling 中间态（#5）
│
├── workers/<AgentName>/
│   └── <task>.ndjson            ~ +tool_start 事件，工具时序可独立重建（#4）
│
└── supervision/
    └── supervision_dsp_*.ndjson ~ 从 coordinator/ 归位（此目录不再是空壳）（#9）

── run 目录外（保持真值隔离，evaluator-private）──
<truth_output_dir>/
├── truth_trace.jsonl            + 默认开启：每步全量环境 claims，
│                                  补 persons 坐标 + reservoir 水量两个字段（#1）
└── truth_manifest.json          + run 的 metadata.json 写指针指向这里
```

设计注记：

1. **truth 不放进 run 目录是有意的**：truth_trace 是 oracle 真值，现有代码强制外置（experiment.py:476-497）就是为了真值隔离（防 agent 侧读取、防评测污染）。目标形态是「run 内 metadata 写指针、truth 落外置目录」，而非塞进 run 内。
2. **只落地第一批（#1-#4）时的最小可见差异**：truth_trace 出现、events 行内多 observation 字段、router CSV 四类 dispatch 齐全、ndjson 多 tool_start 事件——四个文件级变化即解除 H2/H6/H5/L1 四个主要阻塞。
3. **体积代价**：最大头是 truth_trace（每步全量对象 claims）与 context/discards（被裁原文），逐 run 线性增长；其余均为行级/字段级增量，可忽略。
