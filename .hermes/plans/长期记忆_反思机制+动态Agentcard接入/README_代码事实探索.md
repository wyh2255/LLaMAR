# 代码事实探索总览（长期记忆三待办）

> 日期：2026-08-11 · 分支 `feat/memory-redesign` @ `5413705`
> 方式：3 个只读 subagent 并行探索 → 主 agent 独立核验（83+ 断言批量比对）→ 合并落地
> 关联：[待办计划](长期记忆_反思机制+动态Agentcard接入.md) · [探索 01 Embodied](代码事实探索_01_Embodied域接入.md) · [探索 02 AgentCard](代码事实探索_02_AgentCard能力接入.md) · [探索 03 反思/长期记忆](代码事实探索_03_反思机制与长期记忆.md)

---

## 一句话现状

三个待办的**代码事实基础已全部探明**：待办 1/2 的字段族、来源策略、门禁、幂等、读侧均已就绪，缺的只是**生产者与摄入时机**；待办 3 的第四类记忆完全无代码，但全部输入素材（control.* / supervision / callback / read_port / 真值门 / 只读评估器）已就绪。

## 各待办核心结论

| 待办 | 现状一句话 | 关键证据 | 最大缺口 |
|---|---|---|---|
| 1. Embodied 域接入 | agent 自身 position/inventory **随每步回调到达 coordinator 但被提取层丢弃**；心跳只带 worker_id | `barrier.py:404-408` → `_barrier_helpers.py:44-48` → `sink.py:112-113` → 丢弃于 `server.py:116-125`；实测 run2 `embodied_node` 0 行（`logs/h2_shadow_audit_run2`） | 扩展 `_extract_observations_with_provenance` 读 `structured_data.position/inventory` 构造 embodied claim；battery 全库无数据源 |
| 2. AgentCard 能力接入 | "worker 声明 → AgentCard → registry 提取"链路已存在且被调度/prompt/REST 消费，但 **registry 来源零生产者**（全 src 仅 `contracts.py:60,176,177` 字面量） | `a2a_server.py:164-199`、`agent_registry.py:129-142`、`contracts.py:176-177` | 无 `provenance="registry"` 摄入器；无增量同步（WS 仅 6 种消息）；sensor_type 无声明方式（AgentCard 无 sensor 字段） |
| 3. 反思机制/长期记忆 | **无第四类记忆代码**（rg 0 命中、16 张表无长期记忆表）；但反思素材全就绪：control.* 事件带 `control_revision` 时间线（`mission_runtime.py:1198-1233`）、supervision 5 类事件（`task_watchdog.py:307-474`）、真值隔离门（`contracts.py:54-64` + `ingestor.py:619-629`） | `ingestor.py:555`（`control.{source}.{state}`）、`environment_state_provider.py:68-147`（MemoryReadPort 9 方法） | 触发钩子、时间窗/跨 scope 查询、存储形态（`store.py:171` domain CHECK 需扩展）、LLM 决策原文（只留 `result_digest`，`ingestor.py:565-570`） |

## 计划文档表述修正（探索发现，以代码为准）

| 计划文档表述 | 实际代码事实 | 证据 |
|---|---|---|
| "agent position/inventory/battery 状态直接随心跳/回调返回" | 心跳 payload **只有 worker_id**（不携带任何状态）；position/inventory 随**回调**（structured_data）到达但被丢弃；**battery 全仓库无数据源** | `shared/types.py:93-94`、`server.py:116-125`、`contracts.py:162`（仅定义） |
| "缺失 env_step 只进 Temporal 审计" | 无此分支。真实语义：Temporal 事件**总是先写**；env_step 缺失只影响投影字段覆盖决策（有现存带 step 值时被 ignored，无现存值照样 materialize） | `projections.py:104-113`、`ingestor.py:737-753` |
| 行号引用（如 `server.py:746`、`contracts.py:158-184`） | 全部重新核实，漂移 ±1-7 行（如 embodied 映射实际在 `server.py:745`） | 各探索报告内标注 |
| "capability/sensor_type 链路已存在但未喂 Memory" | **属实**，且更精确：capabilities 已被调度（`worker_registry.py:94-97`）、prompt（`query_workers.py:36`）、REST（`routes/workers.py:30,52`）消费，唯独不进 Memory | 探索 02 §3 |

## 核验记录

- 派发：3 个 leaf subagent 并行（embodied / agentcard / reflection），全程只读，仓库零改动
- 独立核验：主 agent 对三份草稿抽取 **83+ 条 load-bearing 断言**（类定义、行号、枚举、常量、表结构）批量比对，**全部通过**；唯一疑似错误（`router_agent/context.py` 路径）查实为 `src/Agent/router_agent/context.py`（真实存在，行号准确）
- 行号漂移容差 ±1-7 行（docstring/装饰器占行属正常）；实质性修正 0 处
- 实测证据（只读）：`logs/h2_shadow_audit_run2/coordinator/memory/memory.sqlite3`（mode=ro 统计 projection_field 38 行全 spatial、embodied_node 0 行）、`memory_rollout_audit.ndjson`（20 条：16 条 spatial 有 read_port 值、4 条 embodied 全 null）、`semantic_map.jsonl`（零 agent 观测）
- 遗留未确认：`acknowledge_event`（`supervision_state_store.py:218-230`）在 src 内未见调用者（已在探索 03 如实标注）

## 建议下一步（仅事实提示，无实施信号）

1. 待办 1 接入点已收敛为"扩展 `server.py:116-125` 提取层 + 走既有 `ingest_callback` 的 `projection_inputs` 通道"（同事务/幂等/门禁全部现成，`ingestor.py:335-438, 578-701`）
2. 待办 2 需要先定 sensor_type 声明方式与摄入时机（注册路径 `server.py:2240-2270` 目前不触碰 memory）
3. 待办 3 设计时注意：LLM 决策原文目前不进 canonical（只有 digest），"基于 Coordinator 决策反思"需要先建在线侧决策文本通道并过隔离门
