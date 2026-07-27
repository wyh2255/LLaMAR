---
日期: 2026-07-26
文档类型: 设计文档 / 论文写作方案
文档概述: 基于 docs/system_docs 撰写期刊/学位论文章节的设计方案。确立 C0/C1/C2 三配置证据结构与不变式审计方法，明确四项与路线无关的材料缺口，并将首批交付范围收敛为四个纯文档驱动章节（中文正文、术语保留英文）。
---

# 基于 system_docs 撰写论文 — 设计方案

## Context

用户希望基于 `docs/system_docs/`（10 份文档、4032 行）撰写一篇期刊 / 学位论文章节，最初的问题是"还缺什么材料"。

调研后结论：文档在**系统机制描述**上非常扎实且刚与代码对齐过（commit `707b36c`），但在**学术论文要件**上几乎为零 —— 无问题形式化、无引用、无基线对比、无定量结果、无消融、无统计分析、无出版级插图。

更要紧的是一处**证据与主张错配**：已选定的核心主张是"协调职责从 prompt 下沉到框架状态机"，而磁盘上现有的 100 格 benchmark sweep 提交于 2026-07-04（`af3d2ce`），MissionGraph / MissionRuntime（Phase 0–6）却是 07-21 至 07-26 才落地的 —— **现有 sweep 没有跑过该主张所依赖的代码路径**。

本方案确立整体证据结构与写作口径，并将**首批交付**收敛为四个不依赖任何新实验的文档驱动章节，使写作可以立即启动，与后续实验工作并行。

预期结果：四个章节完成后，论文的"系统"半部完整；实验半部按轨道 B/C/D 补齐后合并成稿。

---

## 一、已确立的事实（调研结论，勿重复核实）

### 文档侧
- 10 份文档 4032 行。最像"方法"的两块：`contextmanager.md`（三层记忆压缩，**唯一含真伪代码**的文档）、`route_strategy.md`（514 行，三层状态机 + Team ACK saga 补偿 + 12 条不变式）。
- `route_strategy.md:470-510` = §10 错误信号表 + I1–I12 不变式表，真实存在且规范。
- **`route_strategy.md` §10.1 明确写明：失败信号"未必是逐字的独立错误码"**，呈现方式分三类（工具错误字符串 / 状态枚举 / 通用 `reject`+`reason`）。因此不可用单一 grep 错误串做审计。
- 可直接复用的局限性小节：`route_strategy.md` §3.2/§5.6、`peer_mail.md:209-268`。
- 插图素材：`docs/system_docs/images/` 下 `architecture.svg`、`dataflow.svg`；`llamar-sar-flow.svg` 为未被任何文档引用的孤儿文件（且未纳入 git）。

### 代码侧
- `src/`（30k 行）、`sar_orch/`（9.2k 行）为当前实现。**`integration/`（4k 行）是冻结的旧实现，一律不引用。**
- `orchestration_mode ∈ {agentic, dag}` 贯通 `agent_executor.py:268`、`server.py:176`、`cli.py:67`，但 **`sar_orch/experiment.py:278` 硬编码为 `"agentic"`**，runner 无法切换。
- **`_execute_agentic`（`agent_executor.py:305`–451）使用 MissionRuntime**（admit `:322`、abort `:450`）。
- **`_execute_dag_loop`（`:452`–721）完全不触碰 MissionGraph / TeamPartition**。已核实其后唯一的 mission 引用落在 `cancel()`（`:885`–901）内的 `:897`–898（`active_runtime` 取用 + `abort()`），不在 dag 循环内。
- 采集通道现成：`src/Agent/router_agent/logger.py:158` `log_tool_result` 对每次 Router 工具调用记录 `tool_name`/`arguments`/`success`/`error`，与编排模式无关。
- **结构化事件通道已存在，无需新增埋点（已实测）**：每个 run 的 `coordinator/<timestamp>.ndjson` 内含 `tool_result` 事件，payload 形如 `{"tool_name": "query_workers", "success": true}`，另有 `source` 与 `timestamp` 字段。样本格 `20260721_201943_s4_s42_a5` 的事件分布：`tool_start` 137、`tool_result` 136、`llm_response` 91、`task_status` 87、`task_complete` 5、`agentic_start` 1。此外每个 dispatch 另有 `coordinator/events_dsp_<uuid>.ndjson`。**先前"仅用普通 logging、不发结构化事件"的判断作废。**
- 测试：`tests/` 下 71 文件、1156 个 `test_*` 函数。

### 数据侧
- 新系统 sweep：`sar_orch/results_6_30/benchmark/`，数据由 `b2de044`（2026-07-01，"记录6_30日第一次全量场景测试"）加入且此后未变，**早于 Phase 0–6（07-19~07-26）**。报 78/92（84.8%）完成、coverage 0.997、transport 0.957、均步数 29.2、51.5M tokens。**不可作主结果。**（注：`af3d2ce` 是 07-04 的一个 `collect_results` 修复提交，并非加入该 sweep 数据的提交，勿引作数据出处。）
- 原版 LLaMAR SAR 基线：`SAR/baselines/results/llamar/actions/{2..5}_agents/seed_{0,10,20,30,40}/scene_{1..5}` = 恰好 100 格，**与新 sweep 格网完全一致**，指标可对齐（Coverage / TransportRate / Finished / Steps）。
- **公平性问题**：基线硬编码 `gpt-4-turbo`（`llamar_utils_multiagent.py:497`）打真 OpenAI（`:577`）。注意 `llamar.py:99` 的 `config.model` 是死代码，payload 自行硬编码模型，改它无效。
- **07-04 sweep 的模型无从数据确认**：该 sweep 每格仅有 `meta.json`（内容只有 `{"scene":N,"agents":N,"seed":N}`）、`result.json`、`summary.csv`，**全树无任何 model / provider 字段**（已核实 `results_6_30/` 下无 `metadata.json`，且 grep `deepseek|packyapi|gpt-4` 零命中）。`deepseek-v4-flash` + `packyapi.com` 的证据只存在于 `sar_orch/results/2026072*`（Phase 0–6 之后的另一批 ad-hoc run）与代码默认值中。因此**不得声称该 sweep 的模型已由数据确认** —— 只能说其模型未被记录。这也是该 sweep 不可作主结果的又一条理由（不可复现归因）。重跑时必须把 model / provider / max_steps 写入每格 metadata。
- 单格耗时约 170–590 秒（均值 5–6 分钟），并发 2 时 100 格约 5–6 小时。

#### 最新数据实测：`/home/wyh/daily_work/LLaMAR/sar_orch/results/`

**路径注意**：位于 `LLaMAR/` 主仓。两仓库是**同一 repo 的 worktree**（`LLaMAR-refack/.git` 是指向 `LLaMAR/.git/worktrees/LLaMAR-refack` 的文件），共享对象库。该目录被 `.gitignore:155` 忽略，`git ls-files` 为 0 —— **数据完全未纳入版本控制**，作为论文证据前必须先归档落盘。

98 个条目 = 80 个 `YYYYMMDD_HHMMSS_s<scene>_s<seed>_a<agents>` 扁平 run + 17 个 `sar_experiment_202607{04,05}_*` + 一个 `benchmark/`。

**可用之处**：
- 格网覆盖比预期好：scene 1–5 全有（39/11/12/9/9），agents 2–5 全有（41/23/15/1）
- `metadata.json` 91/91 **全部含 `code_commit`**，且含 model / api_base / max_steps / state_mode / enable_peer_mail / prompt_version
- `agent_interactions.csv` 71 个 run 有；**balance 实测可算**（详见第 4 章）
- `ToolLatencyMs` **是活的**（实测 0.13–60055 ms）→ 工具级延迟可报

**不可作主结果的实测理由**：
- **seed 只有 42 一个值**（80/80）。主矩阵的 0/10/20/30/40 全缺 → 100 格覆盖为 0
- **仅 5 个 run 成功完成**。`finished`：67 False / 7 True，其中可归属 scene/agents 的 5 个。按 (scene,agents) 拆：(1,2)=1/19、(1,4)=2/3、(4,5)=1/1、(5,4)=1/2，**其余全 0**
- `end_reason`：`max_steps_reached` 38、`coordinator_finished_early` 16、`framework_error` 8、`success` 5
- **`max_steps` 分布混乱**：20×47、30×29、50×9、3×3、5×1、10×1、1200×1。而 `max_steps_reached` 是首要失败原因 → 大量 run 被上限掐死，非能力不足
- **无任何 `dag` 模式 run**：`orchestration_mode` 在 91 个 metadata 中**零出现**（`experiment.py:278` 硬编码），`events.ndjson` 亦无 mission/DAG 事件类型 → **C1 目前零数据**
- **仅 6 个 run 的 `code_commit` 在触碰 mission 文件的提交之后**（`debdda7`×4、`0119d39`×2，均 07-21）；其余 85 个早于或不涉及 MissionGraph
- `LLMLatencyMs` 仍恒为 0.0（抽查 8 文件，min=max=0.0）
- **`benchmark/` 从未跑完第一格**：20 格计划仅试 3 格（2 timeout、1 failed），17 格 `pending`，`progress.json` 明写 `success:0`；且仅有 scene_1/agents_2 目录
- 17 个 `sar_experiment_*` 中仅 3 个有 `metadata.json`，2 个完全空 → 视为已被取代

### 已知阻塞项
- **`sar_orch/aggregate.py:95` 的 balance 是假指标**：`1.0 if finished else transport_rate` —— 与负载均衡无关，是 success_rate/transport_rate 的复制品。且全 `sar_orch/` 内**没有任何真实 balance / action_share 计算**。原版 CI 脚本却把 balance 当四大指标之一（`confidence_intervals.py:44-51`）。
- **`LLMLatencyMs` 有列但实测恒为 0.0** → `experiment_design.md` §6.2 的所有 latency 指标均不可报。
- `sar_orch/benchmark.py` 的 `run_dir()` 仅按 scene/agents/seed 建目录 → C1/C2 两轮 sweep 会互相覆盖。
- `results/benchmark_aggregated.tsv` 被 `logging_map.md` 引用但不存在。

---

## 二、整体证据结构（已批准）

`agentic` 与 `dag` 不是同一条轴上的深浅，而是把"协调"拆成两个**互相独立**的关注点：

- **轴 A — 计划结构由谁保证**（依赖序、分层扇出）
- **轴 B — 派发安全与通信授权由谁保证**（mission 准入、单写者状态机、team 独占分区、epoch 防重放）

| | 轴 A | 轴 B | 代码路径 |
|---|---|---|---|
| **C0** 原版 LLaMAR | prompt | prompt | `SAR/baselines/llamar.py` |
| **C1** dag | **框架** | prompt | `_execute_dag_loop` |
| **C2** agentic | LLM | **框架** | `_execute_agentic` |
| ~~C3~~ 两者兼有 | 框架 | 框架 | **不存在，不做** |

价值：把"协调下沉"从定性主张变成**两个机制各自贡献多少**的可分解命题。C1 应改善计划合理性/重复劳动，C2 应消除死锁/越权/重放类协调故障。C3 需把 MissionRuntime 接进 dag 循环，属新增功能而非补实验，归 future work。

### 不变式审计口径（已批准）
按可测性分三类，**不可混为一谈**：

- **零改造可测（现有 Router tool_result ndjson）**：I1、I2、I4、I5 —— 按 `success=False` 分桶，但需按 §10.1 的三类呈现方式分别解析，不可单一 grep。
- **数据侧通道已具备，先解析再决定是否补埋点**：`coordinator/*.ndjson` 已有 `tool_result{tool_name, success}`（实测，见第一节）。I6（`apply_physical_status` 单写者 + 终态 exact-once）、I9（abort finalizer：`agent_executor.py:450`、`:897`–898、`mission_runtime.py:1339`）、I10（epoch 单调，TEAM_UPDATE 处）**须先用现有 ndjson 验证可测性，仅对确实无信号的性质补埋点**，不得预设"需新增 5 处"。
- **归为测试验证，不得声称运行时审计**：I3、I8、I11、I12 是缺失性/结构性性质，跑 benchmark 测不出 —— 无违规 ≠ 已验证。引用 `tests/test_team_partition.py` 等既有断言。I11 可用一次性静态扫描脚本。

审计只报**违规计数 + 观测覆盖度**，不声称正确性证明；未被触发的性质如实标注未覆盖。C0/C1/C2 的覆盖梯度本身即主张的证据。

---

## 三、本次交付范围

**四个纯文档驱动章节，中文正文，术语保留英文**（MissionGraph / PhysicalDispatch / TeamPartition / invariant / saga / epoch 等首次出现时加中文解释）。与 `docs/system_docs` 现有写法一致，迁移成本最低。

这四章不依赖任何新实验，可与轨道 B/C/D 完全并行。

输出到 `docs/paper/` 下，每章一文件：
`01-系统架构.md`、`02-形式模型与不变式.md`、`03-实现细节.md`、`04-实验设置.md`

### 章节 1 — 系统架构
**源**：`框架.md`、`系统架构概览.md`、`route_strategy.md` §1–6

内容：Coordinator（1 LLM）+ N Workers 经 A2A 协议协作、`SARBarrier` 步同步的整体结构；双通道通信（WS 控制/心跳 + A2A HTTP 载荷）；三层协调模型 MissionGraph / PhysicalDispatch / TeamPartition 的职责边界。

可直接吸收：`框架.md:456-466` 的 9 条关键设计决策及其 rationale —— 这是现成的"为什么这样设计"论据来源。

注意：
- 引用代码位置一律指向 `src/` 与 `sar_orch/`，**不得引用 `integration/`**。
- 需明确写出 C1/C2 两条路径的差异（轴 A / 轴 B），这是后续结果章节的铺垫。

### 章节 2 — 形式模型与不变式
**源**：原论文 §3 Background（POMDP 七元组，直接继承）、`route_strategy.md` §10（`:470-510`）、状态机定义 `:434-457`

原始论文：`/home/wyh/daily_work/paper/02_多机器人任务规划与分解/LLaMAR_Long-Horizon_Planning_Multi-Agent_Robots_2407.10031.md`

#### 2.1 底层：继承原论文的 POMDP（不重新发明）

原论文 §3 已给出完整形式化，**直接继承**：

```
⟨ N, I, S, {O_i}, {A_i}, P, G, T ⟩
```

其中 `A = A_NAV ∪ A_INT ∪ A_EXP`；`G = {g_1..g_k}` 为子任务集；`T` 为规划视界。

与本仓库 SAR 实现的对应关系（须在文中给出，建立记号可信度）：
- `A` ← `Controller.ALL_ACTIONS + ['Explore']`（`SAR/env.py:108`）
- `G` ← `Checker.initialize()` 自场景参数确定性生成（`SAR/Scenes/checker.py:27`）；生成规则：每火灾 `NavigateTo + UseSupply + EndFire`；每人员 `NavigateTo + Carry + DropOff + NavigateTo(Deposit) + Spot`；每必要水库 `GetSupply + NavigateTo`
- `O_i` ← `generate_obs_text(agent_idx)`（`SAR/env.py:282`）逐 agent 局部观测

**写作口径**：形式化底座不是本文贡献，是共享基座；贡献叠加于其上。

#### 2.2 上层：新增协调层（本文贡献所在）

原论文的 POMDP 只刻画"任务是什么"，**不含"协调职责归谁"的形式表达** —— 因为原版协调全在 prompt 内，无可形式化之物。这是本文的形式化空间。

在 `G` 之上定义协调层 `⟨ G_m, D, π ⟩`：
- `G_m=(V,E)` — MissionGraph，`V` 为 logical node，状态取值 `{BLOCKED, READY, ACTIVATING, ACTIVE, COMPLETED, FAILED, CANCELED}`（7 值，权威来源为代码 `mission_graph.py:20-28` 的 `LogicalState`，`mark_canceled()` 在 `:599`）。**注意** `route_strategy.md:436-456` 的状态机图只画了 6 个状态，CANCELED 未作为独立转移目标出现（图中取消被折叠进 FAILED）—— 形式化以代码为准，并在文中说明图与代码的这一差异，勿声称图已含 7 态。
- `D` — PhysicalDispatch 的 opaque ID 空间，`V ∩ D = ∅`（此即 I2）
- `π: W → Teams` — TeamPartition，I3 即"`π` 是良定义划分"（每 online Worker 恰属一个 team）
- I1–I12 表述为该模型上的**安全性质**（不变式）

**核心论述**：原版把 `G → 执行` 的映射交给 prompt；C1 把 `G` 上的偏序交给 `G_m`；C2 把执行安全交给 `D` 与 `π` 上的不变式。三配置**在同一形式框架下是"哪些约束由系统保证"的差异**，而非三个不同系统。这把第二节的 2×2 表从经验观察升级为模型层面的区分：C1 保证调度序正确性，C2 保证状态安全不变式 —— 两者在形式模型中是**不同类型的性质**，故不在同一轴上。

#### 2.3 依赖链：原论文留下的空白（C1 的定位依据）

原论文附录 D.2 明确论述 SAR 具有依赖链（"火源识别 → 类型识别 → 资源获取 → 使用"），但：
- 该依赖链在原论文中**仅为环境的叙述性质，从未被形式化，也从未被任何指标测量**
- 本仓库 `SAR/Scenes/base_checker.py:31` 明写"所有子任务都是无条件触发的（不依赖于其他任务完成）"，checker 仅检查子任务字符串是否出现于完成列表
- coverage / transport_rate / finished **三个指标均不对子任务完成的先后顺序设门控**（checker 不校验前置条件）

**一处需精确表述的簿记细节**：`base_checker.py:91` `give_credit_for_navigate()` 设计意图是"非导航动作成功时追溯为对应 `NavigateTo` 子任务计分"，即一种顺序相关的簿记旁路。但 `:103` 实际写作 `f"NavigateTo({object})"` —— `inner` 在 `:101` 解包后被丢弃，`{object}` 插值的是 Python 内建类型，渲染为 `NavigateTo(<class 'object'>)`，**永不匹配任何真实子任务**。故该旁路在当前代码下不生效，顺序无关性的结论成立。写作时按"设计存在但因实现缺陷未生效"如实表述，不要简单断言"完全顺序无关"（那会掩盖一个真实存在的机制），也不要当作生效机制描述。

因此 C1 的正确定位是：**首次使 SAR 的依赖链成为系统可保证的属性**，而非"改善既有指标"。这不是本实现的缺陷，是原论文的空白。

**必须诚实标注**：现有指标测不出 C1 的收益。需在第 4 章新增序相关指标（前置未满足导致的动作失败数、重复动作数、步数），并明确说明成功率类指标对 C1 不敏感 —— 乱序但最终蒙对与严格按序执行，在既有指标上无差别。

#### 2.4 口径声明（两条，须显式写出）

1. **这是形式化规范，不是形式化验证。** I1–I12 由测试与运行时审计支撑，**无机器检查的证明**。用 "we formalize" 而非 "we prove" —— 后者会招致 proof 或 model checker 产物的要求。
2. **I1–I12 均为安全性质（不变式），非活性性质。** 本文无活性证明，不得暗示有。

三个可信度风险必须在此章或局限性中精确表述（**不是隐瞒，而是精确**）：
- `ActiveMissionRegistry`（`route_strategy.md:122`）**无实现** → I1 的证据引真实路径 `MissionRuntimeManager.admit`，registry 写入 future work，不得当作已有子系统描述
- 节点级 team 释放：`release_node_team()`（`team_partition_service.py:896`）仅 `MissionRuntime.abort` 一个调用者，`TransitionStatus.RELEASE_PREPARING`（`:44`）从未被赋值 → 写成"team 隔离在 Mission 生命周期内保持，仅 abort 时释放"，归 future work（文档 §5.6 自己已标注）
- `VERIFIED` 状态：精确区分 —— `verifier.py` 产出的 `VerificationReport` 是 DAG replan 循环内部的 PASS/FAIL 门控，**不是**协议级 A2A TaskState；明确写出该区别，不得暗示存在 verified 生命周期阶段

### 章节 3 — 实现细节
**源**：`contextmanager.md`（有真伪代码）、`data_flow.md`、`peer_mail.md`

- **ContextManager**：三层记忆（Pinned / Episodic / Recent）+ token 阈值驱动的降级式压缩。关键参数：`summary_trigger_ratio=0.8`、`episodic_max_items=20`、`recent_messages=12`；4 种策略 `none/summary/hybrid/raw`。伪代码 `observe()`/`prune_history()`/`assemble()` 可直接改写为论文算法块。**注意** `contextmanager.md:432` 自述 `_extract_pinned()` 的 `dispatch_task`/`collect_results` 分支在当前工具集下永不触发 —— 属死分支，不要当作生效机制描述。
- **数据流与事件体系**：`data_flow.md` 的 9 层事件分类 + 3 种关系（containment / derivation / lifecycle）、INPUT_REQUIRED 快照暂停恢复、跨三家 provider 归一化的 token 记账（含 cache 命中/未命中）。
- **PeerMail**：Worker↔Worker 直连、envelope kind/auth 矩阵、HMAC 与 epoch 防重放。**必须注明**：仅 `experiment.py` 支持 `--enable-peer-mail`，`benchmark.py` 未接线（`peer_mail.md:270-271`）—— 因此不得声称该路径已被大规模评测。

### 章节 4 — 实验设置
**源**：`experiment_design.md` §3–5

内容：自变量（scene 1–5 × agents 2–5 × 5 固定 seed = 100 格主矩阵、model、prompt 版本、timeout）；因变量按 §6 指标分类法组织；失败归因方法（框架 / prompt / 模型 / 环境 / 预算五类）。

本章须**如实声明**四件事：
1. 三配置 C0/C1/C2 跑同一格网 → 每格为**配对样本**，统计上用 per-cell 差值的 Wilcoxon 符号秩检验，而非独立区间重叠判断
2. **延迟类指标全部排除**并说明原因（`LLMLatencyMs` 恒为 0.0）；可报的时间维度仅 `elapsed_seconds` 与 token 成本
3. C0 结构上不存在框架计数类指标（dispatch / barrier timeout / a2a error）→ 标 N/A，**不填 0**
4. **成功率类指标对 C1 不敏感**（见 §2.3）→ C1 的评估重心为序相关指标

#### 指标定义须与原论文对齐（balance 假指标的修复依据）

原论文 §5 Metrics 给出 balance 的精确定义，据此修 `aggregate.py:95`：

```
B := min{s_1..s_n} / (max{s_1..s_n} + ε),   ε = 1e-4
```

其中 `s_i` 是 agent `i` 完成的**关键子任务相关**成功高层动作数 —— 注意是**子集**（原文："only check for a subset of high-level actions that must be executed to accomplish critical subtasks"），非全部动作。B=0 表示至少一个 agent 无成功动作，B=1 表示完全均衡。

**数据源已实测可用，但成功判定需选路**（`agent_interactions.csv`，71 个 run 有）：
- 该文件的 `ErrorType` 列**全 9782 行皆空**，`EventType` 只有 `tool_result`/空/`query_sar_state` → **两者都不能作成功标志**
- 无独立 `Success` 列；`subtasks.csv` 的 `Status` 实测 100% 为 `"assigned"`、`FailureClass` 全空 → 亦不可用
- 可行路径一：解析 `Observation` 文本中的 `"was successful"` / `"was not success"` 短语。**实测可行** —— 样本格 `20260721_201943_s4_s42_a5` 得 per-agent 成功数 David 18 / Emma 19 / Bob 18 / Charlie 20 / Alice 21，B = 18/(21+1e-4) ≈ **0.857**。局限：该短语仅存在于动作类工具（explore / navigate_to / use_supply / get_supply / carry_person / drop_off_person / clear_inventory / no_op / get_agent_state），**report_observation / finish_task / map_agent__* / 邮箱类工具无 pass/fail 信号**
- 可行路径二：`trajectory.csv`（85 个 run 有）的 `Successes` 列，与该步 `Actions` 列按位对齐（全量 3130 True / 156 False）。局限：step 级、多 agent 联合，非直接 per-agent 标注

**决策**：路径一即"动作类工具成功数"，天然贴近原论文"关键子任务相关动作"的限定，建议以其为主、用路径二交叉校验。实现时须在论文中写明成功判定的具体来源与所含工具集合。

#### 新增序相关指标（C1 的评估依据，§2.3）

既有指标（coverage / transport_rate / finished）与顺序无关，测不出 C1。须新增：
- 前置未满足导致的动作失败数（依赖链违序的直接体现）
- 重复动作数
- 步数（`steps`）

#### 三项对比混淆项（均须在重跑时对齐，或如实声明）

| 混淆项 | C0 基线现状 | 新系统现状 | 原论文 |
|---|---|---|---|
| 模型 | `gpt-4-turbo` 硬编码（`llamar_utils_multiagent.py:497`） | 代码默认 `deepseek-v4-flash`；**07-04 sweep 未记录，无从确认** | GPT-4V |
| 步数上限 | `env.task_timeout`（`SAR/baselines/llamar.py:95`） | `benchmark.py:223` 默认 50；**实测 run 中为 20×47 / 30×29 / 50×9 / 其他×6** | `L=30` |
| balance 口径 | 原论文公式 | 假指标（`aggregate.py:95`） | 原论文公式 |

balance 口径现在可修；模型与步数上限**必须在重跑时统一**，否则三者混杂无法归因。注意现有 sweep 均步数 29.2 已贴近论文 `L=30` 上限，步数上限的选择会实质影响成功率。

#### 统计工具

复用而非重写：`meta/result_analysis/confidence_intervals.py` 的 `get_CP_interval`（`:20`, Clopper-Pearson）与 `get_mean_std_interval`（`:27`, t 分布）是通用 pandas 函数，无 AI2Thor 依赖。该文件即原论文产物，故口径天然与原论文一致（原文：均值 + 95% CI，SR 为二项指标用 Clopper-Pearson）。唯一不匹配是列名（`get_ci_metrics` `:44` 要 `"success rate"` 带空格，`aggregate.py:116` 输出 `success_rate` 下划线）→ 写十行改名适配器即可。

---

## 四、明确不在本次范围

- 引言 / 相关工作 / 结果 / 讨论 / 结论 章节
- 相关工作四方向（LLM 多智能体编排、具身多智能体规划基线、分布式协调 saga/epoch、agentic 运行时不变式监控）。**仅 AutoGen（Wu et al., 2023）已核实可引**；其余需在写作阶段逐条查证，不得沿用未核实的文献名
- 轨道 B（代码改动）：接 `--orchestration-mode` 到 `experiment.py`/`benchmark.py`、修 `run_dir()` 目录冲突、**修 balance 假指标**、C0 公平性补丁（`llamar_utils_multiagent.py:497`/`:577`）、I6/I9/I10 三处埋点
- 轨道 C（约 15–18 小时墙钟，可无人值守）：三轮 100 格 sweep，先 C2 验证埋点，再 C0/C1
- 轨道 D：聚合、CI 适配、审计解析、配对检验、成图
- 出版级插图绘制（`route_strategy.md:399-421` 数据流、`:434-457` 生命周期状态机提升为正式图；新画 C0/C1/C2 代码路径对比图、不变式违规计数柱状图）

**硬顺序约束**：balance 假指标与 `run_dir()` 目录冲突**必须在跑 sweep 之前修完**，否则三轮跑完需重来。

---

## 五、验证方式

四个章节属文稿，验证以事实核对为主：

1. **代码引用核对** — 章节中每处 `file:line` 引用逐一 `Read` 校验行号仍指向所述内容（近期 commit 频繁，行号易漂）。重点：`agent_executor.py:305/322/450/452`、`experiment.py:278`、`route_strategy.md:470-510`、`aggregate.py:95`。
2. **`integration/` 零引用检查** — `grep -rn "integration/" docs/paper/` 应无实质引用。
3. **未实现机制检查** — 全文搜 `ActiveMissionRegistry`、`RELEASE_PREPARING`、`VERIFIED`，确认三处均以 future work / 精确区分的措辞出现，无一处被描述为生效机制。
4. **死分支检查** — 确认 `contextmanager.md:432` 所述死分支未被当作生效机制写入。
5. **PeerMail 评测范围检查** — 确认已注明 `benchmark.py` 未接线。
6. **术语一致性** — 英文术语首次出现处均有中文解释，后文用法统一。
7. **参考文献** — 仅保留已核实条目；AutoGen 之外的每一条须给出可验证来源。

实验相关验证（sweep 可复现性、CI 数值、审计计数）随轨道 C/D 交付，不在本次范围。
