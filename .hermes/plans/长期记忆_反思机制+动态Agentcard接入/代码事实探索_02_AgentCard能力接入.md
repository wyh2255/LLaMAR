## 探索报告：AgentCard 动态能力接入（capability / sensor_type 来源链路）

> 分支：`feat/memory-redesign`（HEAD `5413705`）｜模式：严格只读
> 范围：worker 侧 AgentSkill 生成 → coordinator 注册/提取 → Memory 投影字段链路
> 行号以当前 checkout 实测为准（2026-08-11）

### 0. 链路总览（一句话）

worker 通过 `create_worker_a2a_server(capabilities=[...])` 在**构造期**把每个 cap 生成为一个 `AgentSkill` 放进 AgentCard（`a2a_server.py:164-199`）；coordinator 在 worker WS 注册时**主动 HTTP 拉取** AgentCard（`server.py:2246-2250`），由 `AgentRegistry.register_from_agent_card` 提取 skills 为 `AgentInfo.capabilities`（`agent_registry.py:123-142`）；但该提取结果**只流向调度/prompt/REST 展示**（`worker_registry.py:94`、`query_workers.py:36`、`routes/workers.py:30,52`），**从未进入 Memory**——`FIELD_SOURCE_POLICY` 中 `capability`/`sensor_type` 仅允许 `registry` 来源（`contracts.py:176-177`），而全库没有任何代码以 `registry` 作为 provenance 构造投影输入（grep 证实 0 生产者）。

---

### 1. worker 侧：capabilities → AgentSkill 生成与 skills.append 机制（核实 164-176 / 185-199）

- `create_worker_a2a_server(..., capabilities: list[str] | None = None, ...)` 签名在 `src/a2a/worker/a2a_server.py:121-151`。
- 每个 cap 生成一个 `AgentSkill`：列表推导 `AgentSkill(id=cap, name=cap, description=f"Capability: {cap}", tags=[cap])`（`a2a_server.py:164-167`），随后两次 `skills.append(...)`：
  - `id="backend"`，`tags=["metadata","backend","mini_agent"]`（`a2a_server.py:168-175`）；
  - `id="model"`，`tags=["metadata","model",<model>]`（`a2a_server.py:176-183`）。
- AgentCard 在**同一函数内一次性构造**：`skills=skills`（`a2a_server.py:185-199`），`capabilities=AgentCapabilities(streaming=True, push_notifications=callback_signer is not None)`（`a2a_server.py:189-191`）——注意 `AgentCapabilities` 只承载 streaming/push_notifications，**能力清单本身在 `skills` 里**（与 `docs/system_docs/memory.md:502` 的描述一致）。
- **"动态追加"的准确含义**：`skills` 是函数内局部 list，`skills.append(...)` 只在 AgentCard 构造前可用；`server.agent_card` 与路由在构造后固定（`a2a_server.py:297,303`），**全文件无任何运行期追加/修改 skill 的 API**。SDK 侧 `create_agent_card_routes(agent_card)` 持同一对象引用（`.venv/.../a2a/server/routes/agent_card_routes.py:31-35`），但项目代码中无运行期写入者。因此"动态"仅指**每次启动时按参数动态生成**，非运行期热更新。
- 实际调用方的 cap 来源：
  - `sar_orch/worker.py:450` 硬编码 `cap_list = ["sar", "navigation", "rescue", "firefighting"]`，`sar_orch/worker.py:460-464` 传入 `create_worker_a2a_server(capabilities=cap_list, ...)`；
  - `src/a2a/worker/cli.py:124` 从 CLI `--capabilities` 逗号串解析，`cli.py:133-137` 传入。
- coordinator 自身也以同样模式声明 AgentCard skills（`src/a2a/coordinator/a2a_server.py:67,78-82`）。

### 2. coordinator 侧：AgentCard 如何到达 agent_registry + 提取过滤逻辑（核实 135-146）

**到达路径**（`src/a2a/coordinator/server.py:2236-2270`，`_handle_worker_message`）：
1. worker WS 连接后发 `WS_REGISTER`，payload 仅含 `{worker_id, a2a_endpoint}`（`src/a2a/shared/types.py:79-90`，注释明言"仅连通性信息，能力信息通过 A2A AgentCard 获取"；worker 侧发送处 `src/a2a/worker/coordinator_client.py:65-74`）；
2. coordinator 收到后先 `WorkerRegistry.register_from_ws`（`server.py:2244`）；
3. **coordinator 反向 HTTP 拉取** `{a2a_endpoint}/.well-known/agent-card.json`，3 次重试、间隔 1s（`server.py:2247-2250`；实现 `server.py:2368-2395`，timeout 10s）；
4. 拉取成功 → `_agent_registry.register_from_agent_card(worker_id, endpoint, card_data)`（`server.py:2254-2258`）；失败 → 最小化注册兜底（`server.py:2259-2270`）。

**skills → capabilities 过滤逻辑**（`src/a2a/coordinator/agent_registry.py:123-142`，逐 skill 遍历）：
- `if "backend" in tags` → `backend` = tags 中第一个不属于 `("metadata","backend","model")` 的标签（133-136）；
- `elif "model" in tags` → 同理提取 `model`（137-140）；
- `elif skill_id not in ("backend","model")` → **`capabilities.append(skill_id)`**（141-142）。
- 精确语义：被排除的是带 `backend`/`model` 标签的 skill 以及 id 恰为 `backend`/`model` 的 skill；`metadata` 标签本身不构成排除条件（只有 backend/model 两个 skill 携带它）。注意：**无去重**，同一 skill_id 重复出现会重复入列。
- 同时提取 `description`（144）与 `push_notifications`（145-150），整体构造 `AgentInfo` 并**整体覆盖** `self._agents[agent_id]`（151-162）。
- 注册/心跳的其余消费点：`register`（74-78）、`update_heartbeat_from_worker`（164-168）、`unregister_worker`（170-173）。

### 3. 提取出的 capabilities 流向哪里？是否写入 Memory？（核实"无实际生产者"）

**当前消费者（全部为非 Memory 用途）**：
- 调度过滤：`worker_registry.py:79-94` `select_worker(capability=..., agent_registry=...)` 按 `agent.capabilities` 匹配 worker（94-97）；
- LLM prompt 文本：`src/a2a/builtin_tools/query_workers.py:36` 调 `get_all_agents_prompt_text()`（`agent_registry.py:175-181`，能力拼进 prompt）；
- REST 展示：`src/a2a/coordinator/routes/workers.py:30,52` 把 `agent.capabilities` 放进 `/workers` 响应；
- context 摘要：`sar_orch/coordinator_state_provider.py:641` 拼进 summary 文本。

**Memory 侧无生产者的 grep 证据**：
- 全 `src/` 搜索 `"registry"`（provenance 字面量）：仅 3 处命中——`contracts.py:60`（`ONLINE_PROVENANCE_ALLOWLIST` 成员）、`contracts.py:176`、`contracts.py:177`（FIELD_SOURCE_POLICY）。**没有任何代码构造 `NormalizedProjectionInputV1(provenance="registry", ...)`**；
- `src/a2a/coordinator/memory/` 内搜 `capabil`：仅 `contracts.py:157`（注释）与 `contracts.py:176`（策略）；`projections.py`、`store.py` 中搜 `capability`/`sensor_type`：**0 命中**（这两个字段连投影列/表结构都不存在，仅存在于字段级策略字典）；
- `FIELD_SOURCE_POLICY` / `field_source_priority` 的消费点：`contracts.py:187-199`（实现）、`contracts.py:241-243`（`NormalizedProjectionInputV1.source_priority` 属性）、`projections.py:115-117,220`（reducer 仲裁时经 `inp.source_priority` 使用）、`exporter.py:227-248`（只读导出重建 field claim）。即：**仲裁机制已就绪，但 registry 来源的 claim 永远不会有**。
- 佐证文档：`docs/system_docs/memory.md:496,502-503` 明确记录"capability/sensor_type 无实际生产者，后续应接入 AgentCard 作为摄入源"。

### 4. AgentCard 动态更新：有无增量同步机制？（核实：无）

- WS 消息类型全集只有 6 种：`register` / `heartbeat` / `task_progress` / `cancel_task` / `shutdown` / `relay_a2a`（`src/a2a/shared/types.py:70-76`）。**不存在** agent_card_update / skills_update / card 变更类消息。
- worker 侧唯一发送 `WS_REGISTER` 的时机是 `connect()`（`coordinator_client.py:59-77`）；断线重连会重新走 `connect()`（`_try_reconnect`，`coordinator_client.py:124-139`），从而触发 coordinator 重新拉取 AgentCard 并整体覆盖注册（`server.py:2254-2258` + `agent_registry.py:161`）。
- 结论：**worker 新增 skill 后没有任何通知机制**；唯一能传播新 skill 的路径是"断开重连 → 重新注册 → coordinator 重新拉卡"，且无任何代码在 skill 变化时主动触发重连。心跳（30s 一次，`coordinator_client.py:111-121`）与 A2A 回调均不携带 AgentCard。

### 5. sensor_type 现状（核实：契约字段，无来源声明机制）

- `src/` + `sar_orch/` 全量搜 `sensor`（排除 `worker_sensor_tool`/`sensor_tool` 名称）：仅 `contracts.py:157`（注释）、`contracts.py:177`（策略）、`sar_orch/coordinator_state_provider.py:28`（注释）。**无任何运行期 sensor 数据路径**。
- A2A SDK（`.venv/lib/python3.13/site-packages/a2a/`）AgentCard/AgentSkill **无 sensor 概念**：
  - `AgentCard` 字段：name/description/supported_interfaces/provider/version/documentation_url/capabilities/security_schemes/security_requirements/default_input_modes/default_output_modes/skills/signatures/icon_url（`a2a/types/a2a_pb2.pyi:188-218`）；
  - `AgentSkill` 字段：id/name/description/tags/examples/input_modes/output_modes/security_requirements（`a2a/types/a2a_pb2.pyi:259-271`）；
  - SDK types 内搜 `sensor`：0 命中。
- `contracts.py:176-177`：`"capability": ("registry",)`、`"sensor_type": ("registry",)`——sensor_type 在策略中**只允许 registry 来源**，但 registry 无生产者（见 §3），AgentCard 也无处声明 sensor，**sensor_type 目前是纯死契约**。

### 6. ingestor 幂等键模式（核实 73-95），作为摄入时机设计的复用基础

- `projection_idempotency_key(scope_id, inputs)`（`src/a2a/coordinator/memory/ingestor.py:73-95`）：对 `["projection", scope_id, 排序后的证据元组]` 做 canonical JSON + sha256；每个证据元组 = `(event_id, env_step, domain, entity_id, entity_type, field_name, digest_payload(value))`——**同一证据束必然得到同一 key**。
- 消费点 `ingest_projection`（`ingestor.py:578` 起，事务内 637-680+）：
  1. 前置门禁：provenance allowlist（619-624）、truth scan（625-629）、`inp.validate()`（631-635）、scope 存在/未关闭（638-641）、runtime_epoch 匹配（644-649）；
  2. `bundle_key = projection_idempotency_key(...)`（651）→ `tx.claim_callback_idempotency(scope_id, bundle_key, bundle_digest)`（655-657）；
  3. `conflict` → `IdempotencyConflictError`（同 key 不同 digest，658-661）；`duplicate` → 返回 `ProjectionIngestResult("duplicate", event_ids=..., committed_revision=...)`，**零新写入**（662-668）；首次 → `_reduce_evidence_in_tx` + bump revision + `insert_idempotency_ledger`（670-679）。
- 同族其他键：callback（43-62）、control（65-66）、supervision（69-70）——全部是"canonical 内容 sha256"式确定性键。
- 对"registry 能力摄入"的启示：capabilities 是**跨心跳稳定的集合态**而非事件流，可直接以 `projection_idempotency_key` 语义（内容决定 key）复用——同一能力集合重复摄入自动去重，能力集合变化则生成新 key 触发 revision。

### 7. worker→coordinator 消息结构：AgentCard 随什么传输？

- **AgentCard 不随 WS 消息传输**：WS_REGISTER payload 只有 `{worker_id, a2a_endpoint}`（`shared/types.py:79-90`）；WS_HEARTBEAT payload 只有 `{worker_id}`（`shared/types.py:93-94`）。
- AgentCard 由 **coordinator 在注册时通过 HTTP 主动拉取**（`server.py:2246-2250`，`/.well-known/agent-card.json`，重试 3 次）。
- 心跳处理只更新时间戳与 watchdog：`server.py:2275-2279`（`_registry.update_heartbeat` + `_agent_registry.update_heartbeat_from_worker` + `record_worker_contact`）。**心跳/回调均不携带 AgentCard，也不触发重新拉卡**。
- 结论：AgentCard 是"**注册时一次性快照**"语义；其新鲜度仅由重连（重新注册）保证。

---

### 8. 现状缺口清单（capabilities → embodied 投影接起来缺什么）

1. **无 registry 来源的摄入生产者**：`FIELD_SOURCE_POLICY["capability"/"sensor_type"] = ("registry",)`（`contracts.py:176-177`）已声明仲裁优先级，但全库无任何 `provenance="registry"` 的 `NormalizedProjectionInputV1` 构造点（§3 grep 证据）——缺一个"把 `AgentInfo.capabilities` 转成投影 claim 的摄入器/调用点"。
2. **摄入时机未定**：候选时机（worker 注册 `server.py:2254-2258` 之后 / 首次回调 / 心跳）在计划文档中列为待设计点；目前注册路径完全不触碰 memory 模块（`server.py:2240-2270` 无任何 memory 调用）。
3. **增量同步机制缺失**：worker 新增 skill 无通知消息类型（`shared/types.py:70-76` 仅 6 种）、无卡片 diff、无主动重连触发；只有断线重连才会重拉 AgentCard（`coordinator_client.py:124-139`）——动态能力变化无法及时进入任何下游（包括未来要接入的 Memory）。
4. **sensor_type 无声明方式**：A2A AgentCard/AgentSkill 均无 sensor 字段（`a2a_pb2.pyi:188-218,259-271`），项目代码零 sensor 运行期逻辑（§5 grep）——需要定义"如何声明 sensor_type"（AgentSkill tags 约定？新字段？registry 侧配置？），否则 `contracts.py:177` 永远无输入。
5. **extraction 语义细节待定**：`agent_registry.py:141-142` 的提取无去重、`metadata` 标签本身不参与过滤（过滤实际由 backend/model 标签承担）；若 sensor_type 也走 AgentCard，需在提取层扩展并明确去重/规范化规则。
6. **能力变更与 scope/runtime_epoch 的关系未设计**：capability 属 registry 静态元数据，不随 env_step 变化；摄入时需对齐 `ingestor.py:638-649` 的 scope 存在性/closed/runtime_epoch 门禁，并决定能力变更是否 bump projection revision（现有 `projections.py` 无 capability 列，需先建列/建表）。
7. **消费侧未定义**：即便摄入成功，`capability`/`sensor_type` 目前不存在于 `projections.py`/`store.py` 的任何表结构（grep 0 命中），read_port/投影读取侧也无可读字段——需要"写入路径 + 读取路径"一起补齐。

---

### 9. 核心事实速览（每条带证据）

1. `create_worker_a2a_server` 的 `capabilities` 参数在**构造期**为每个 cap 生成 `AgentSkill(id=cap, name=cap, description=f"Capability: {cap}", tags=[cap])`：`src/a2a/worker/a2a_server.py:164-167`。
2. `skills.append(...)` 是 AgentCard 构造前的局部 list 追加（backend:168-175、model:176-183），运行期无任何追加 API，AgentCard 一次性构造于 `a2a_server.py:185-199`；"动态"= 每次启动按参数生成。
3. `AgentCapabilities` 仅含 streaming/push_notifications（`a2a_server.py:189-191`），能力清单实际存放在 `AgentCard.skills`。
4. 实验 worker 的 cap 硬编码为 `["sar","navigation","rescue","firefighting"]`：`sar_orch/worker.py:450,464`；CLI 版来自 `--capabilities` 逗号串：`src/a2a/worker/cli.py:124,137`。
5. WS_REGISTER payload 仅 `{worker_id, a2a_endpoint}`，能力信息由 coordinator 反向 HTTP 拉取 AgentCard：`src/a2a/shared/types.py:79-90`、`src/a2a/coordinator/server.py:2246-2250`（重试 3 次/1s，实现 `server.py:2368-2395`）。
6. 注册链路：WS_REGISTER → `register_from_ws`（`server.py:2244`）→ 拉卡 → `register_from_agent_card`（`server.py:2254-2258`），失败走最小注册兜底（`server.py:2259-2270`）。
7. skills→capabilities 过滤：`backend`/`model` 标签的 skill 提取为 backend/model 字段，其余 `skill_id` 入 `capabilities`（`src/a2a/coordinator/agent_registry.py:129-142`），注册为整体覆盖（`agent_registry.py:161`）。
8. 提取出的 capabilities 只被调度（`worker_registry.py:94-97`）、prompt 文本（`query_workers.py:36` + `agent_registry.py:175-181`）、REST 展示（`routes/workers.py:30,52`）消费，**从不进 Memory**。
9. Memory 侧 `registry` provenance 生产者=0：全 src 仅 `contracts.py:60,176,177` 三处字面量；`memory/` 目录内 `capabil` 仅 `contracts.py:157,176`；`projections.py`/`store.py` 对 `capability`/`sensor_type` 0 命中。
10. `FIELD_SOURCE_POLICY`：`"capability": ("registry",)`、`"sensor_type": ("registry",)`（`contracts.py:176-177`）；`registry` 在 `ONLINE_PROVENANCE_ALLOWLIST` 中（`contracts.py:60`）；仲裁经 `source_priority` 属性生效（`contracts.py:241-243`、`projections.py:115-117,220`），即机制就绪、无输入。
11. 无 AgentCard 增量同步：WS 消息类型全集 6 种（`shared/types.py:70-76`），无 card 变更消息；skill 变化只能靠断线重连触发重新注册（`coordinator_client.py:59-77,124-139`），心跳（30s，`coordinator_client.py:111-121`）不携带卡片。
12. sensor_type 是纯死契约：全 src/sar_orch 无运行期 sensor 逻辑（仅 `contracts.py:157,177` 与 `coordinator_state_provider.py:28` 注释）；SDK AgentCard/AgentSkill 无 sensor 字段（`.venv/.../a2a/types/a2a_pb2.pyi:188-218,259-271`）。
13. 幂等复用基础：`projection_idempotency_key` = sha256(["projection", scope_id, 排序证据元组])（`ingestor.py:73-95`），在 `ingest_projection`（`ingestor.py:578`）中经 `claim_callback_idempotency` 实现 conflict/duplicate 判定（`ingestor.py:651-668`），duplicate 零写入。
14. 摄入门禁：provenance allowlist + truth scan + scope 存在/未关闭 + runtime_epoch 匹配（`ingestor.py:619-649`）——registry 来源的能力 claim 天然通过 allowlist，但需满足其余门禁。
15. 文档佐证：`docs/system_docs/memory.md:496,502-503` 已记录"capability 链路已存在、无实际生产者、后续接入 AgentCard 作为摄入源"。
