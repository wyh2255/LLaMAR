# G1/R1 审查包 — H1 Projection Contract Card：`worker_telemetry` authority 扩展

> **性质：** 本文件是条件 Gate G1（人工必要审查点 R1）的审查材料，由父 agent 基于真实代码取证生成，提交用户审批。批准后放行 P2（Embodied callback telemetry producer）。
> **对应决策：** 主方案 D1（2026-08-12 用户拍板接受）：`worker_telemetry` 升为 position/inventory 第一权威，必须带 `env_step`。
> **绑定基线：** HEAD `54137050a8d5332e7f81e24b9a1f45857bd8455f`；主方案 SHA-256 `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`。
> **结论占位：** 待审批（`APPROVE` / `REJECT` / `REVISE`，审批结论将落入 `<feature>-g1-approval-record.md`）。

---

## 1. 变更范围（批准的字段集合 — 精确到字段，禁止泛化）

| 字段 | 当前 source policy（contracts.py:158-178） | D1 后（本 card 批准值） | 变更 |
|---|---|---|---|
| `position` | `(worker_sensor_tool, worker_observation, peer_report)` | `(worker_telemetry, worker_sensor_tool, worker_observation, peer_report)` | **worker_telemetry 升为第一权威（priority 0）** |
| `inventory` | `(worker_sensor_tool, worker_observation, peer_report)` | `(worker_telemetry, worker_sensor_tool, worker_observation, peer_report)` | **worker_telemetry 升为第一权威（priority 0）** |
| `battery` | `(worker_telemetry, worker_sensor_tool, worker_observation)` | 不变 | 无变更（已有 worker_telemetry 第一，但无真实生产者 → 不写） |
| `localization_quality` | `(worker_telemetry, worker_sensor_tool, worker_observation)` | 不变 | 无变更 |
| `node_telemetry` | `(worker_telemetry, worker_sensor_tool, worker_observation)` | 不变 | 无变更 |
| `scene_object` / 其他 | 不变 | 不变 | 无变更 |

**本 card 只批准 `position` 和 `inventory` 两个字段的 authority 提升。** 不涉及 battery 合成、不涉及 sensor_type/capability（registry 来源，P3）、不涉及任何其他 telemetry 字段。

## 2. 变更语义（H1 reducer 影响）

reducer 仲裁顺序（`src/a2a/coordinator/memory/projections.py:95-134`，本项目不改）：

```text
env_step fence → same-step source priority → confidence → value equality → conflict
```

D1 批准后，`worker_telemetry` 对 position/inventory 的含义：

1. **env_step 强制：** 无 `env_step` 或非整数的 telemetry claim **不产生字段写入**（主方案 §3.1：step 缺失/坏 step → 对应 field 不产生 claim；callback 自身 Temporal 审计保持现有行为）。
2. **不回退：** `env_step` 小于当前字段已持有的 step → `ignored_out_of_order`，绝不覆盖新事实（现有 fence 逻辑，未改）。
3. **同 step 冲突：** 同 `env_step` 下 `worker_telemetry` 胜过 `worker_observation`/`worker_sensor_tool`/`peer_report`（自报是 agent 自身位置的最高权威）。同 priority 同 confidence 不同值 → 现有 C3 CONFLICTED 语义，sequence 不选赢家。
4. **身份绑定：** entity_id/actor_id 一律来自已认证 `auth_dispatch.worker_id`，**不信任 payload 中的 reporter/name**（`server.py` callback 认证路径）。telemetry 与观测路径的 entity_id 一致性依赖项目约定 worker_id == agent name（SAR 成立，2026-08-12 确认）。
5. **battery 无生产者：** 全仓库无 battery 数据源，本 Phase 不产生任何 battery claim，禁止写 0/None/推断值。
6. **允许来源：** `worker_telemetry` 已在 `ONLINE_PROVENANCE_ALLOWLIST`（contracts.py:54-64），H1-INV-1 真相隔离不受影响；truth 词表扫描（`FORBIDDEN_TRUTH_TERMS`）继续适用。

## 3. P0 RED/guard 测试记录（contracts 已冻结，P2 实现后转 GREEN）

| 测试（tests/test_memory_embodied_telemetry.py） | 类型 | 当前状态 | 断言内容 |
|---|---|---|---|
| `test_worker_telemetry_first_in_position_field_source_policy` | RED（AssertionError） | `field_source_priority("position","worker_telemetry") == 0`，当前返回 3（不在 policy） | D1 契约值 |
| `test_worker_telemetry_first_in_inventory_field_source_policy` | RED（AssertionError） | `field_source_priority("inventory","worker_telemetry") == 0`，当前返回 3 | D1 契约值 |
| `test_same_step_telemetry_outranks_observation_for_position` | RED（AssertionError） | 同 step 8：ingest observation [3,4,0] 后 ingest telemetry [5,6,0] → 投影 provenance 必须是 `worker_telemetry`；当前 telemetry priority 3 > observation 1 → 被 superseded | D1 场景级契约 |
| `test_newer_step_telemetry_not_regressed_by_older_step` | GREEN 守护 | newer-step telemetry 不被旧/无 step 回退 | 时序栅栏不变 |
| `test_no_step_telemetry_cannot_overwrite_existing_step` | GREEN 守护 | 无 step telemetry 不能覆盖已有 step 字段 | 无 step 不写 |
| `test_telemetry_missing_step_produces_no_claim` / `test_telemetry_bad_step_produces_no_claim` | RED（ImportError） | `_normalize_telemetry_projection_inputs` 未来函数：缺/坏 step → `[]` | §3.1 fail-closed |
| `test_telemetry_never_produces_battery_claim` | RED（ImportError） | 提取永不产生 battery claim | §3.1 |
| `test_telemetry_identity_from_authenticated_dispatch_not_payload` | RED（ImportError） | identity 绑定 auth dispatch，payload 的 reporter/name 不得作为身份 | 身份契约 |
| `test_telemetry_evidence_id_distinct_from_observation_evidence` | RED（ImportError） | telemetry evidence id 为 callback/body/worker/step 确定性摘要，不与 observation evidence 冲突 | 证据 id 契约 |
| `test_worker_telemetry_provenance_allowlisted` | GREEN 守护 | `worker_telemetry` ∈ ONLINE_PROVENANCE_ALLOWLIST | H1-INV-1 前置 |

truth denial 守护（tests/test_memory_online_truth_boundary.py:101-176）：telemetry 值含 `FORBIDDEN_TRUTH_TERMS` → 整 bundle 拒绝、零 domain 写入（不变）。

## 4. `worker_telemetry` payload 生产/传输/提取链路图（真实代码）

```text
┌─ 生产点：SARBarrier._build_structured_obs()（sar_orch/barrier.py:369-408）
│   返回 {observations: [...], position: (x,y,z)|None, inventory: [...]}
│   ⚠ 当前顶层无 step（observations 每条有 "step": self._step_counter）
│   ── P2 改：顶层补当前采样 step（与同次 observation 同一 step）
│
├─ 传输点：tool_result_from_barrier()（sar_orch/tools/worker/_barrier_helpers.py:21-77）
│   data = {observations, position, inventory}（+ error_detail/overrides）
│   ⚠ 当前不保留顶层 step → P2 改：保留 step 到 ToolResult.data
│
├─ Sink 封装：A2AWorkerSink（src/a2a/worker/sink.py:90-116）
│   [DATA] block: structured_data = _REDACTOR.redact_data(data)
│   （脱敏在 worker 侧已执行；content_limit 12000 对 observation 路径）
│
├─ 提取点：_extract_observations_with_provenance()（src/a2a/coordinator/server.py:98-142）
│   ⚠ 当前只取 observations（object_type/name/step），丢弃顶层 position/inventory
│   ── P2 改：新增独立 telemetry 提取器 _extract_worker_telemetry_with_provenance()
│      （与观测提取并列；只提取白名单字段 position/inventory，忽略其他）
│
├─ 归一化：_normalize_telemetry_projection_inputs()（P2 新增）
│   仅当 step 合法 + position 三维数值 + inventory 可 normalize 时构造
│   NormalizedProjectionInputV1(domain=embodied, provenance=worker_telemetry,
│   actor_id=auth_dispatch.worker_id)
│
└─ 落库：server.py:1576-1628 callback path
    projection_inputs 并入 _ingest_callback_to_memory(... projection_inputs=...)
    同 canonical 事务：callback Temporal + evidence Temporal + embodied 投影原子提交
    （不新开旁路 transaction；重复 callback 由现有 idempotency 去重）
```

关键事实核验（本审查包取证）：
- 顶层 self state 真实生产点确实存在且被丢弃：`barrier.py:369-408` 返回 position/inventory，`server.py:98-142` 只消费 observations。
- 心跳（`WS_HEARTBEAT`，server.py:2275-2279）payload 只有 worker_id，**P2 不扩充心跳**（主方案 §1 前提校正，保持）。
- 身份不信任 payload：观测路径 entity_id 取 `name`（server.py:771），telemetry 与观测一致依赖 worker_id == agent name 约定。
- `normalize_inventory()` 已存在（P0 测试引用），P2 复用。

## 5. 需用户拍板的决策项（现状 → 建议 → 需确认）

| # | 决策项 | 现状 | 建议 | 需确认 |
|---|---|---|---|---|
| C1 | **批准字段集合** | D1 已拍板 position/inventory 升为第一权威 | 本 card 精确锁定 `position` + `inventory` 两个字段；battery/localization_quality/node_telemetry 不写（无生产者）；scene_object 不动 | 是否同意精确锁定范围 |
| C2 | **同 step 冲突语义** | 当前同 step 下 observation(1) 胜过 telemetry(3) | D1 后 telemetry(0) 胜过 observation；同 priority 同 confidence 不同值 → C3 CONFLICTED（现有逻辑不变） | 是否接受"自报优先于他报"的同 step 语义 |
| C3 | **entity_id 依赖 worker_id == agent name 约定** | 观测路径 entity_id 取 name | telemetry 沿用同一约定；若未来分离出现双实体，另开设计（方案 §3.1 已声明） | 是否接受该约定作为 V1 前提 |
| C4 | **无 step 的 telemetry 零写入** | 无（feature 未实现） | 无 step/坏 step 不产生 claim，callback Temporal 审计保持 | 是否确认 fail-closed 行为 |
| C5 | **battery 保持零写入** | 无生产者 | 禁止写 0/None/推断值；有真实生产者后另开 Phase | 是否确认 |

## 6. 风险评估与残留

| 风险 | 缓解 |
|---|---|
| telemetry 乱序覆盖新事实 | mandatory step + env_step fence + older/no-step 测试（P0 已冻结） |
| 伪造身份 | identity 来自 auth_dispatch.worker_id，不信任 payload；签名 callback 是唯一写入路径 |
| worker 自报 position 与 observation 冲突误导 | C3 CONFLICTED 保留双方候选，sequence 不选赢家；shadow/read 阶段真实 run 观察 |
| battery 被误写 | 提取器白名单不含 battery 字段 + P0 负例测试 |
| 双实体（worker_id ≠ name） | V1 声明接受约定；出现后另设计（不在本 Phase） |

**明确不授权（G1 放行边界）：** 本 card 不授权扩充心跳、不授权 battery 合成、不授权 telemetry 覆盖现有 env_step fence、不授权 worker 直接写 canonical DB、不授权 P2 之外的任何生产改动。G1 批准只放行 P2（callback telemetry producer + source policy 调整）。

## 7. 证据清单

- `src/a2a/coordinator/memory/contracts.py:153-199` — FIELD_SOURCE_POLICY 现状（本 card §1 依据）
- `src/a2a/coordinator/memory/projections.py:95-134` — reducer 仲裁顺序（本 card §2 依据）
- `src/a2a/coordinator/server.py:98-142` — telemetry 丢弃点（链路图）
- `src/a2a/coordinator/server.py:1576-1628` — callback 同事务投影入口（链路图）
- `sar_orch/barrier.py:369-408`、`sar_orch/tools/worker/_barrier_helpers.py:21-77`、`src/a2a/worker/sink.py:90-116` — 生产/传输点
- `tests/test_memory_embodied_telemetry.py`（P0 冻结，§3 记录）
- `tests/test_memory_online_truth_boundary.py:101-176` — truth denial 守护
- P0 验收：106 RED + 179 GREEN（2026-08-12）；P1 验收：contracts+store 46/46、reflection 9/22、回归 25/25

---

*本审查包由父 agent 生成（2026-08-12），提交用户对 G1/R1 审批。审批结论（APPROVE/REJECT/REVISE + 授权范围 + 用户原话）将写入 `<feature>-g1-approval-record.md` 并回写进度文档。*
