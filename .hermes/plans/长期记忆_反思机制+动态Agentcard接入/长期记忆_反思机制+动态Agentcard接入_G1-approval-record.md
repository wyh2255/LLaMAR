---
title: G1/R1 Projection Authority Extension Approval Record（worker_telemetry）
schema_version: 1
gate_id: G1
review_point: R1
conclusion: APPROVE
approved_at: 2026-08-12
reviewed_commit: 54137050a8d5332e7f81e24b9a1f45857bd8455f
branch: feat/memory-redesign
review_packet: .hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_G1-R1-H1-contract-card-worker-telemetry.md
review_packet_sha256: 1faeff92de86ebc44e4fef2cb42732491f416f3edfc9dc9b71465646e898b9a4
design_sha256: 169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab
review_points_doc_sha256: 218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d
---

# G1/R1 投影权威扩展审批记录（`worker_telemetry`）

## 人工决议

用户在本次会话（2026-08-12）逐项确认审查包 §5 全部五个决策项后批准 G1，原话：

> 这轮5项决策都同意（C1–C5）
> 好的，批准G1通过，可以准备一下写进进度文档然后，开放写下来的内容了。但是不要开始实施计划

即 **APPROVE**：批准 H1 projection contract card 的字段集合，放行 P2（callback telemetry producer + source policy 调整）。`APPROVE` 不自动启动实施——P2 的编码与验证仍需用户另行明确"开始实施"指令（人工必要审查点 §1）。

## 授权范围（allowlist）

- `position`、`inventory` 两个字段的 source policy 升为 `worker_telemetry` 第一权威（priority 0），仅此两个字段，禁止泛化到其他 telemetry 字段。
- P2 实施内容：`sar_orch/barrier.py:369-408` 顶层补采样 step；`sar_orch/tools/worker/_barrier_helpers.py:21-77` 保留 step 至 ToolResult.data；`src/a2a/coordinator/server.py` 新增 `_extract_worker_telemetry_with_provenance()` 与 `_normalize_telemetry_projection_inputs()`；callback 同 canonical 事务落库（server.py:1576-1628）；P0 冻结的 10 个 telemetry 测试 RED 转 GREEN。

## 决策项结论（C1–C5 全部确认）

| # | 决策项 | 结论 |
|---|---|---|
| C1 | 批准字段集合 | 同意精确锁定 `position` + `inventory`；battery/localization_quality/node_telemetry 不写（无生产者）；scene_object 不动 |
| C2 | 同 step 冲突语义 | 接受"自报优先于他报"：同 step 下 telemetry(0) 胜过 observation/sensor_tool/peer_report；同 priority 同 confidence 不同值仍走 C3 CONFLICTED |
| C3 | entity_id 约定 | 接受 `worker_id == agent name` 约定作为 V1 前提；未来双实体另开设计 |
| C4 | 无 step 零写入 | 确认 fail-closed：无 step/坏 step 不产生 claim，callback Temporal 审计保持现有行为 |
| C5 | battery 零写入 | 确认保持零写入，禁止写 0/None/推断值；有真实生产者后另开 Phase |

## 前提核验

- 审查时点 HEAD `54137050a8d5332e7f81e24b9a1f45857bd8455f`（`feat/memory-redesign`）与审查包绑定基线一致。
- 审查包 SHA-256 于写入本记录前重新计算：`1faeff92de86ebc44e4fef2cb42732491f416f3edfc9dc9b71465646e898b9a4`，与文件绑定一致。
- 主方案 SHA-256 `169f9f7d…fa1ab`、人工必要审查点补充 SHA-256 `218d7990…2ab96d` 为冻结值。
- 工作区含 P0/P1 未提交产物（`long_term.py`、`reflection.py`、`contracts.py` 等，属既有 P0/P1 记录范围）；P2 尚未有任何生产改动。

## 明确不授权（G1 放行边界，同审查包 §6）

- 不授权扩充心跳（`WS_HEARTBEAT`，server.py:2275-2279）。
- 不授权 battery 合成或任何推断写入。
- 不授权 telemetry 覆盖现有 env_step fence 与不回退约束。
- 不授权 worker 直接写 canonical DB。
- 不授权 P2 之外的任何生产改动；P3–P6 仍依赖后续 Gate（G2/G3/G4）与用户明确授权。
- 未批准前不得提升 telemetry authority，也不得以 `worker_sensor_tool` 等其他 provenance 偷换 D1 语义。

## 残留风险（用户接受）

| 风险 | 缓解 |
|---|---|
| telemetry 乱序覆盖新事实 | mandatory step + env_step fence + P0 older/no-step 测试冻结 |
| 伪造身份 | identity 来自 auth_dispatch.worker_id；签名 callback 为唯一写入路径 |
| 自报 position 与 observation 冲突误导 | C3 CONFLICTED 保留双方候选，sequence 不选赢家；shadow/read 阶段真实 run 观察 |
| battery 被误写 | 提取器白名单不含 battery + P0 负例测试 |
| 双实体（worker_id ≠ name） | V1 声明接受约定；出现后另设计，不在本 Phase |
