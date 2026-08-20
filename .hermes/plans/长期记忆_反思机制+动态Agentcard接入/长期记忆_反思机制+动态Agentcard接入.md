# 长期记忆 · 反思机制 + 动态 AgentCard 接入（已实施）

> 记录日期：2026-08-11（待办记录）；2026-08-12 经 G0–G4 审批链全部 APPROVE，正式实施完成
> 状态：**✅ 已实施（2026-08-12，G4/R4 APPROVE）**——Embodied telemetry 接入（P2）、AgentCard registry projection（P3）、反思机制与长期记忆（P4/P5）、coordinator-only read 注入正式可用；实施细节见 [`长期记忆_反思机制+动态Agentcard接入_实施方案.md`](长期记忆_反思机制+动态Agentcard接入_实施方案.md) 与实施进度文档
> 来源：`docs/system_docs/memory.md` §7（已更新为实施状态）
> 关联文档：[`../../../docs/system_docs/memory.md`](../../../docs/system_docs/memory.md) · [`../memory-system-redesign-design.md`](../memory-system-redesign-design.md)

---

## 背景

canonical Memory 目前只有三类记忆（Temporal 事件流水 / Spatial 场景投影 / Embodied agent 投影）。三类问题/方向在审查中暴露，需后续处理：

1. **Embodied 域生产零数据**——agent 具身状态（position/inventory/battery）实际随心跳/回调返回，但未接入投影；
2. **capability/sensor_type 无实际生产者**——能力信息在 A2A AgentCard（含动态添加接口），但 `FIELD_SOURCE_POLICY` 仅标注 `registry` 来源且无人写入；
3. **无长期记忆**——所有记忆都是短期事实/流水，缺少"基于近期内容 + Coordinator 决策反思生成长期记忆"的机制。

---

## 待办 1：Embodied 域接入（agent 具身状态随心跳/回调摄入）

**现状证据**：
- 观测路径只把 `obj_type == "agent"` 的观测映射为 embodied 域（`src/a2a/coordinator/server.py:745`），实际 run 中 worker 观测对象均为环境物体 → `embodied_node` 表与 `projection_field(domain='embodied')` 恒为空（实测 `logs/h2_shadow_audit_run2`：38 行投影全为 spatial）
- agent 的 position / inventory 随**回调**（`structured_data`）到达 coordinator 但被提取层丢弃；**心跳 payload 仅含 worker_id**（`src/a2a/shared/types.py:93-94`），battery 全库无数据源（见 [`代码事实探索_01`](代码事实探索_01_Embodied域接入.md) §2/§10）；`worker_registry.update_heartbeat` 目前只更新时间戳（`src/a2a/coordinator/worker_registry.py:105-110`），状态未进入 canonical Memory
- embodied 字段族定义已就绪（`FIELD_SOURCE_POLICY`，`src/a2a/coordinator/memory/contracts.py:158-184`）：`position` / `inventory` / `battery` / `localization_quality` / `node_telemetry` / `availability` / `heartbeat` / `capability` / `sensor_type`；`worker_telemetry` 是 battery/遥测类字段的最高权威来源

**目标**：从 worker 心跳/回调状态中提取具身状态，以 `worker_telemetry` 来源摄入 embodied 域投影。

**待设计点**：
- 心跳/回调状态文本的解析与字段映射（与 `_normalize_observation_projection_inputs` 对齐，`server.py:700-807`）
- 摄入入口（回调路径内同事务 / 独立 ingest_projection）
- 与 env_step 栅栏的衔接（telemetry 无 step 语义时的处理：`projections.py:104-113` 中无 step/更旧 step 的 claim 不覆盖已有带 step 值，Temporal 事件照写）

---

## 待办 2：AgentCard 动态能力接入（capability / sensor_type）

**现状证据**：
- 能力标签对应 **A2A AgentCard**：worker 侧 `create_worker_a2a_server(capabilities=[...])` 参数为每个 cap 动态生成 `AgentSkill`，`skills.append(...)` 可继续追加（`src/a2a/worker/a2a_server.py:164-176, 185-199`）
- coordinator 侧 `agent_registry.py:135-146` 注册时已从 AgentCard 提取 skill 为 `capabilities` 列表（过滤 `metadata`/`backend`/`model` 标签）——"worker 声明 → AgentCard → registry 提取"链路**已存在**
- `FIELD_SOURCE_POLICY` 中 `capability` / `sensor_type` 仅允许 `registry` 来源（`contracts.py:176-177`），但该链路结果**未喂给 Memory 投影**，无实际生产者

**目标**：将 registry 从 AgentCard 提取的 capabilities 作为 `capability`（及 `sensor_type`）字段的权威来源摄入 embodied 投影。

**待设计点**：
- 摄入时机：**新 worker 首次注册成功时**（registry 中此前不存在）bootstrap 能力快照；重复注册（断线重连）不摄入（D2 拍板 2026-08-12）；幂等复用 `projection_idempotency_key` 模式（`ingestor.py:73-95`）
- AgentCard 动态更新（新增 skill）的增量同步**不在 V1**；`agent_card_changed` 消息与 in-process mutation 待真实 producer 出现后再设计
- `sensor_type` 的字段来源约定（AgentCard 目前无 sensor 字段，需定义声明方式；V1 只做机制，SAR 无 sensor 恒空）

---

## 待办 3：反思机制与长期记忆（第四类记忆）

**现状**：Memory 只有短期记忆（Temporal/Spatial/Embodied），无长期记忆类别。

**目标（用户拍板方向，2026-08-11）**：
1. **反思机制**：基于**近期返回的内容**（worker callback / 观测证据 / supervision 事件）+ **Coordinator 决策**（`control.*` 生命周期事件），进行反思（reflection），**生成长期记忆**
2. **长期记忆（Long-term Memory）**：独立于 temporal/spatial/embodied 的第四类记忆——**单次 run 内**对短期事实与决策模式的**滚动反思总结**（LLM 总结已发生事情）；跨 run 复用通过 skill/文档沉淀，不属本待办（用户拍板 2026-08-11）

**待设计点**：
- 反思的触发时机与输入窗口（run 内滚动触发 + terminal 收尾；**增量窗口** = 上次反思之后的 sequence 区间；`control.*` 事件天然带 `control_revision` 与时间线，可作为输入锚点）
- 长期记忆的存储形态（run-local 独立文件 `<memory_root>/long_term/long_term.sqlite3`、scope 隔离、自带 migration runner）与读取侧接入（read_port 读路径 → 注入 run 内后续轮次 Context，`MemoryReadPort` 扩展）
- 与现有契约的兼容：scope 语义（单 run 单 scope）、幂等键（system-derived `memory_key`）、revision 体系（canonical/entity/view）
- **在线真相隔离边界**（H1-INV-1）：反思输入只能来自在线来源（`ONLINE_PROVENANCE_ALLOWLIST`），长期记忆生成不得引入 Barrier/真值；与 evaluator 的 post-run 使用边界保持一致
- 评估：长期记忆质量指标（可参考 `memory_projection_quality.py` 的只读评估模式）

---

## 验收方向（各待办通用）

- 遵循既有验证链：单元契约测试（tests/test_memory_*.py 风格）→ ruff → 全量 pytest（当前基线 1636 passed, 4 skipped）
- 真实 run 证据：shadow/read_port 模式跑实验，检查 canonical DB 中 embodied 投影/长期记忆产物实际落库
- 涉及框架层改动时按项目惯例做 5 scenes × 2 agent counts 交叉验证（max_steps=20, seed=42），零框架错误码
