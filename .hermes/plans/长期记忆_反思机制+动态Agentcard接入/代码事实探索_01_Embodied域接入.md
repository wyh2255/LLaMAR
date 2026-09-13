# Embodied 域接入探索报告（agent 具身状态随心跳/回调摄入）

> 只读探索报告 · 分支 `feat/memory-redesign` @ `5413705` · 2026-08-11
> 范围：worker 心跳/回调数据流 → coordinator 摄入路径 → canonical Memory embodied 域；含 `logs/h2_shadow_audit_run2` 实测核验。
> 行号均为当前 checkout 实测，计划文档中的旧行号（如 `server.py:746`）已重新核实并标注漂移。

## 1. Worker 心跳完整数据流（Q1）

- **构造点**：`src/a2a/worker/coordinator_client.py:90-98` `send_heartbeat()`，消息 `{"type": WS_HEARTBEAT, "payload": build_ws_heartbeat_payload(self._worker_id)}`。
- **payload 内容**：`src/a2a/shared/types.py:93-94` `build_ws_heartbeat_payload` 只返回 `{"worker_id": worker_id}` —— **心跳不携带 position / inventory / battery 任何状态**（计划文档"状态直接随心跳返回"的说法与代码不符）。
- **频率与传输**：`coordinator_client.py:111-123` `_heartbeat_loop` 每 30 秒发送一次；传输为 WebSocket，`connect()` 连 `ws://…/ws/worker/{worker_id}`（`coordinator_client.py:61-62`）。
- **WS 客户端接线方**：`sar_orch/worker.py:355` 导入 `CoordinatorWebSocketClient`（sar_orch 本身没有任何 heartbeat 构造代码，`rg heartbeat` 在 sar_orch/ 下 0 命中）。
- **Coordinator 接收点**：`src/a2a/coordinator/server.py:2236` `_handle_worker_message`，`WS_HEARTBEAT` 分支在 `server.py:2275-2279`：
  - `self._registry.update_heartbeat(worker_id)`（WorkerRegistry）
  - `self._agent_registry.update_heartbeat_from_worker(worker_id)`（AgentRegistry，`agent_registry.py:164-168`，同样只更新时间戳）
  - `self._task_watchdog.record_worker_contact(worker_id)`
- **心跳侧没有任何 Memory 写入**：WS_HEARTBEAT 分支不触碰 `_memory_ingestor`，不构造任何投影输入。

## 2. Worker 回调（A2A push）数据流（Q1）

- **worker 侧事件构造**：`src/a2a/worker/sink.py:53-116` `A2AWorkerSink.emit()` 把 `llm_response / tool_start / tool_result` 构造成 `TaskStatusUpdateEvent`，文本体附带 `[DATA]` JSON 块；`tool_result` 分支把工具返回的 `data` 放入 `payload["structured_data"]`（`sink.py:112-113`），经 `_enqueue_working` 推送。
- **发送端**：`src/a2a/worker/callback_sender.py:71-117` `SignedPushNotificationSender`，对序列化 HTTP body 签名 `X-A2A-Callback-Proof`（`callback_sender.py:35-36, 65-68`），重试带新鲜 nonce、body 不变（`callback_sender.py:84-98`）。
- **Coordinator 接收端**：`server.py:1315` `@app.post("/a2a/push-callback")`；`secure` 模式（shadow/read_port）在 `server.py:1357-1411` 先做 proof 校验、nonce 预留、redaction，再进入状态机处理（`server.py:1413-1650`）。
- **回调体里实际携带的具身状态**（关键事实）：
  - `sar_orch/barrier.py:404-408` `_build_structured_obs` 返回 `{"observations": [...], "position": (x,y,z), "inventory": [...]}` —— **agent 自己的 position/inventory 确实随每步结果返回**；
  - `sar_orch/tools/worker/_barrier_helpers.py:40-48` 把它们放进 `ToolResult.data = {"observations", "position", "inventory"}`；
  - `sink.py:112-113` 序列化为 `structured_data`。
  - **但 coordinator 提取时丢弃了它们**：`server.py:98-142` `_extract_observations_with_provenance` 只读 `structured_data["observations"]`（`server.py:116-125`）与 legacy `report_observation` JSON content（`server.py:128-140`）；`src/a2a/` 全目录 `grep structured_position|structured_inventory` 0 命中 —— agent 自身的 position/inventory 随回调到达 coordinator 后即被丢弃，从不进入投影。
- **battery 无任何生产者**：全仓库（src/ sar_orch/ Agent/）`battery` 仅出现在 `contracts.py:162`（字段策略定义）与 `coordinator_state_provider.py:630` 注释；SAR worker 工具（`get_agent_state`、barrier）均不返回 battery。计划文档"battery 随心跳/回调返回"的表述**未在代码中确认**（实际不存在）。

## 3. worker_registry.update_heartbeat 现状（Q2）

- `src/a2a/coordinator/worker_registry.py:105-110`：签名 `update_heartbeat(self, worker_id: str) -> None`，仅更新 `last_heartbeat = datetime.utcnow()`、`status = ONLINE`、`_last_contact_at` —— **只时间戳，无任何状态字段入参**。
- 注册表数据模型 `WorkerNode`（`worker_registry.py:10` 自 `a2a.shared.types` 导入）只承载连通性信息；注册路径同样只写 `a2a_endpoint`（`worker_registry.py:38-50`）。
- 结论：**registry 侧没有可接投影的状态载体**；若走"心跳摄入"，需要先扩展 payload、`update_heartbeat` 签名与 `WorkerNode` 存储。

## 4. 观测摄入路径与 embodied 映射（Q3）

- **domain 映射**：`server.py:745` `domain = "embodied" if obj_type == "agent" else "spatial"`（计划文档引用的 746 已漂移 1 行）；`server.py:746` `entity_type = "agent" if obj_type == "agent" else obj_type`。
- **`_normalize_observation_projection_inputs`（server.py:700-807）归一化内容**：
  - `server.py:733-736`：读 `object_type`/`name`，无 name 直接跳过；
  - `server.py:737-740`：`scan_forbidden_truth_fields` 命中（oracle/ground_truth 等）跳过，不进任何 sink；
  - `server.py:741-744`：`env_step` 取自观测自带 `step`；`evidence_id = "cb:{worker_task_id}:{body_sha256[:16]}:{obj_type}:{name}:{step}"`；
  - `server.py:747-748`：`confidence`、`actor_id = reporter or dispatch.worker_id`；
  - `server.py:782-790`：`position` claim；
  - `server.py:791-795`：`attributes` 逐 key claim（跳过 position/inventory，最多 8 个）；
  - `server.py:796-806`：**仅当 `obj_type == "agent"`** 才产生 `inventory` claim（`normalize_inventory`，结构化解析、永不 eval）。
- **embodied_node 何时被写**：由 `MemoryProjectionReducer.reduce` 对 domain=embodied 的 claim 触发 `_materialize`（`projections.py:201-245`）→ `store.upsert_projection_field` + `bump_entity_revision`（embodied_node 表定义在 `src/a2a/coordinator/memory/store.py:161-168`）。**没有任何 embodied claim 产生时，该表永远不会被写**。
- **触发前提**：投影输入只在 push-callback 的 `callback_result` 里提取到观测时构造（`server.py:1579-1603`）→ `_ingest_callback_to_memory(projection_inputs=...)`（`server.py:1616-1628`）。
- 读取侧：`sar_orch/environment_state_provider.py:93-95` `embodied_snapshot()` = `_group_projection_fields("embodied")`，`server.py` 的 `/environment-state`（`server.py:1895`）经此渲染 embodied 段（`environment_state_provider.py:268-297`）——读路径已就绪，只缺写侧。

## 5. projections.py 的 env_step 规则（Q4）

- `src/a2a/coordinator/memory/projections.py:104-110`（`reduce()` 内）：
  - 若当前投影字段已有 env_step（`cur_step is not None`），且新 claim `env_step is None` 或更旧（`in_step < cur_step`）→ `_ignored`（out-of-order，不覆盖）；
  - `projections.py:112-113`：`cur_step is None or in_step > cur_step` → materialize。
- **计划文档"缺失 env_step 只进 Temporal 审计"的表述需要修正**：代码里没有"只进 Temporal 审计"的分支；真实语义是——Temporal evidence 事件**总是**先写（`ingestor.py:737-753` 先 append event 再 reduce），env_step 缺失只影响**投影字段**的覆盖决策（有现存带 step 值时被 ignored；无现存值时照样 materialize）。对 embodied 遥测的意义：若 agent 字段已有 step=N 的值，无 step 的心跳遥测将无法覆盖它，必须给遥测设计 step 语义（见缺口 4）。

## 6. contracts.py FIELD_SOURCE_POLICY 与 worker_telemetry（Q5）

- `src/a2a/coordinator/memory/contracts.py:158-178` 字段族全貌：
  - `position` / `inventory` / `scene_object`：`(worker_sensor_tool, worker_observation, peer_report)`
  - `battery` / `localization_quality` / `node_telemetry`：**`worker_telemetry` 为最高权威**，其后 `worker_sensor_tool`, `worker_observation`
  - `availability` / `heartbeat`：`(control, supervision, worker_telemetry, worker_observation)`
  - `capability` / `sensor_type`：`(registry,)`
- `contracts.py:180-184` 默认策略（scene-object 类）；`contracts.py:187-199` `field_source_priority`（按字段查表，未知字段回退默认）。
- **`worker_telemetry` 已入白名单**：`contracts.py:54-64` `ONLINE_PROVENANCE_ALLOWLIST` 含 `worker_telemetry`（第 57 行），且 `validate()`（`contracts.py:245-260`）只认白名单 —— 来源类已就绪。
- **但全仓库除 contracts.py 外 `worker_telemetry` 零使用**：没有任何代码构造 `provenance="worker_telemetry"` 的 claim —— 权威来源声明存在、生产者不存在。
- 输入契约 `NormalizedProjectionInputV1`（`contracts.py:205-260`）：`domain ∈ {"spatial","embodied"}`、`source_priority` 属性（241-243）、`validate()` 强制字段（245-260）。

## 7. 实测：h2_shadow_audit_run2（Q6）

目录存在：`logs/h2_shadow_audit_run2/`（run_id `sar-scene1-agents2-seed42-ccece10f`，mode=shadow，2 agents，3 steps）。数据库 `coordinator/memory/memory.sqlite3` 只读统计：

| 表 | 行数 |
|---|---|
| `projection_field` | **38 行，domain 全部 = spatial**（fire 24 / reservoir 6 / deposit 4 / person 4） |
| `embodied_node` | **0 行** |
| `spatial_entity` | 9 行 |
| `projection_outcome` | 43 行 |

- `coordinator/memory_rollout_audit.ndjson`（20 条）：16 条 `.spatial_state.*`（read_port 均有值）；4 条 `.embodied_state.*`（Alice/Bob 的 inventory 空表）**read_port 全部为 null**，仅 legacy 侧有值 —— 即读侧差异审计明确记录"legacy 有 embodied、canonical 无"。
- `semantic_map.jsonl` 全部观测 `object_type` 分布：fire 16 / reservoir 8 / person 4 / deposit 4 —— **零条 `agent` 观测**，`server.py:745` 的 embodied 分支在本次 run 从未触发。
- 结论：**计划文档"38 行投影全为 spatial"的断言属实**；`embodied_node` 恒空的根因是 run 中根本没有 `obj_type=="agent"` 的观测（agent 自身状态只随 `structured_data.position/inventory` 到达但被提取层丢弃，见 §2）。

## 8. ingest_projection 入口与事务边界（Q7）

- **独立入口**：`src/a2a/coordinator/memory/ingestor.py:578-701` `ingest_projection(inputs: list[NormalizedProjectionInputV1])`，单个 `canonical_transaction()`（637）；门禁：scope/epoch 束检查（605-614）、白名单 + 真相字段扫描（619-629）、`validate()`（631-635）、scope 存在/未关闭（638-641）、epoch 匹配（643-649）、束幂等键 `projection_idempotency_key` + `claim_callback_idempotency`（651-668）；随后 `_reduce_evidence_in_tx`（670-675）+ revision/outbox（676-695）。
- **回调内同事务先例已存在**：`ingestor.py:335-438` `ingest_callback(..., projection_inputs=None)`，同一 `canonical_transaction()`（365）内：callback Temporal 事件（388-390）→ control receipts（391-392）→ `_reduce_evidence_in_tx`（393-399，Temporal 先、reducer 后）→ revision + outbox（414-432）。调用方 `server.py:1616-1628` 已把归一化投影输入传入。
- `_reduce_evidence_in_tx`（`ingestor.py:720-754`）：按 `inp.event_id` 分组，每组先 append 一个 `evidence.projection` Temporal 事件（739-745），再逐 claim `reducer.reduce`（747-753）。
- 结论：**"回调路径内同事务摄入"不是要新建的机制，而是既有实现**；embodied 接入只需把 agent 状态构造成 `NormalizedProjectionInputV1` 列表并走现有 `projection_inputs` 通道（或独立调用 `ingest_projection`），事务边界/幂等/门禁全部现成。

## 9. 现状缺口清单（Q8）

1. **心跳无状态可提取**：`types.py:93-94` 心跳 payload 只有 `worker_id`；`worker_registry.py:105-110` `update_heartbeat` 只更新时间戳。走"随心跳摄入"需扩展 payload 构造、WS 消息处理（`server.py:2275-2279`）与 registry 存储。
2. **回调携带的 agent 状态被提取层丢弃**：`barrier.py:404-408` 返回 position/inventory → `_barrier_helpers.py:44-48` 进 `ToolResult.data` → `sink.py:112-113` 进 `structured_data`；但 `server.py:116-125` 只读 `structured_data.observations`。最低成本接入点：扩展 `_extract_observations_with_provenance`（或新增并行提取）读 `structured_data.position/inventory`，构造 domain=embodied、entity_id=agent_name 的 claim。
3. **provenance 选择**：agent 自身状态可标 `worker_telemetry`（已白名单，`contracts.py:57`，且是 battery/localization_quality/node_telemetry 的最高权威，`contracts.py:162-168`），但当前无任何代码产出该来源的 claim —— 需在归一化层新增映射。
4. **env_step 语义**：心跳/遥测无 step；`projections.py:109` 下无 step 的 claim 无法覆盖已有带 step 的字段。需设计遥测的 step 语义（如沿用最近一次观测的 step、或用独立单调计数并调整栅栏规则）。
5. **battery 无生产者**：全代码库无 battery 数据源（SAR 环境、barrier、get_agent_state 均不返回），计划文档对此的表述与实际不符；接入时需先在 worker 侧定义 battery 的来源。
6. **`obj_type=="agent"` 观测路径本身在 SAR 中不产数据**：`barrier.py:33` 有 `AbsAgent→agent` 映射、`barrier.py:306-309` env snapshot 含 agents，但 `_build_structured_obs`（`barrier.py:377, 385-402`）只枚举可见物体（global_obs），agent 自身不进 observations 列表；实测 run2 零 agent 观测。仅靠修 server.py:745 映射无法激活 embodied。
7. **摄入入口二选一（均已具备）**：回调同事务（`ingestor.py:340, 393-399`，配 `server.py:1616-1628`）或独立 `ingest_projection`（`ingestor.py:578`）；幂等/门禁/事务边界现成，无需新建。
8. **读取侧已就绪**：`environment_state_provider.py:93-95` embodied_snapshot 与 `/environment-state` 渲染（268-297）等待写侧数据；run2 audit 的 4 条 embodied read_port=null 记录就是"读侧空、legacy 有"的既存证据。

## 10. 核心事实（带证据）

1. 心跳 payload 仅含 worker_id，无任何具身状态：`src/a2a/shared/types.py:93-94`。
2. 心跳每 30s 经 WebSocket 发送：`src/a2a/worker/coordinator_client.py:111-123`（`send_heartbeat` 90-98）。
3. Coordinator 接收心跳只更新两个 registry 的时间戳 + watchdog 联系时间，不触碰 Memory：`src/a2a/coordinator/server.py:2275-2279`。
4. `update_heartbeat` 签名无状态入参，只写 `last_heartbeat`/`status`/`_last_contact_at`：`src/a2a/coordinator/worker_registry.py:105-110`。
5. agent 自身 position/inventory 确实随每步工具结果返回（`structured_data`），但 coordinator 提取只读 `observations` 子列表，position/inventory 被丢弃：`sar_orch/barrier.py:404-408` → `sar_orch/tools/worker/_barrier_helpers.py:44-48` → `src/a2a/worker/sink.py:112-113`；提取端 `src/a2a/coordinator/server.py:116-125`。
6. embodied 域映射只认 `obj_type=="agent"`：`src/a2a/coordinator/server.py:745-746`（计划文档的 746 已漂移 1 行）。
7. `_normalize_observation_projection_inputs` 的 inventory claim 同样只在 `obj_type=="agent"` 时构造：`src/a2a/coordinator/server.py:796-806`。
8. embodied_node 仅在 reducer 收到 domain=embodied 的 claim 时经 `_materialize` 写入：`src/a2a/coordinator/memory/projections.py:201-245`；表定义 `store.py:161-168`。
9. env_step 栅栏：现存值带 step 时，无 step/更旧 step 的 claim 被 ignored（不覆盖），但 Temporal 事件照写：`projections.py:104-113` + `ingestor.py:737-753`。
10. FIELD_SOURCE_POLICY 中 battery/localization_quality/node_telemetry 以 `worker_telemetry` 为最高权威，availability/heartbeat 以 control 为首：`src/a2a/coordinator/memory/contracts.py:158-178`。
11. `worker_telemetry` 已在 ONLINE_PROVENANCE_ALLOWLIST 内（`contracts.py:57`），但全仓库无任何代码产出该来源的 claim。
12. 实测 run2：`projection_field` 38 行全 spatial（fire 24/reservoir 6/deposit 4/person 4），`embodied_node` 0 行，`spatial_entity` 9 行：`logs/h2_shadow_audit_run2/coordinator/memory/memory.sqlite3`（只读统计）。
13. run2 audit 20 条中 16 条 spatial 有 read_port 值、4 条 embodied 路径 read_port 全 null（仅 legacy）：`logs/h2_shadow_audit_run2/coordinator/memory_rollout_audit.ndjson`。
14. run2 观测 object_type 分布 fire 16/reservoir 8/person 4/deposit 4，零 agent：`logs/h2_shadow_audit_run2/semantic_map.jsonl`。
15. 回调内同事务投影摄入已实现（`ingest_callback` 的 `projection_inputs` 通道 + `_reduce_evidence_in_tx` Temporal 先、reducer 后），独立 `ingest_projection` 入口亦存在：`src/a2a/coordinator/memory/ingestor.py:335-438, 393-399, 578-701, 720-754`；调用方 `server.py:1616-1628`。
16. battery 全仓库无数据源（仅 `contracts.py:162` 定义与 `coordinator_state_provider.py:630` 注释）——计划文档"battery 随心跳/回调返回"未在代码中确认。
