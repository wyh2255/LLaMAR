---
title: H1 Projection Semantics Lock — Human Approval Record
status: APPROVED — USER HUMAN GATE CLOSED
approved_at: 2026-08-06T14:51:11+08:00
approval_source: user explicit instruction “解冻H1，并添加必要的注释，我直接开新会话按着进度文档继续”
implementation_target: /home/wyh/daily_work/LLaMAR-memory-redesign
branch: feat/memory-redesign
target_head: e1a5a01392c814ddcbc33f5e17aa5cad5d9e8fe6
source_scope_at_approval: clean for src/ + sar_orch/ + pyproject.toml; pre-existing AGENTS.md modification excluded
review_basis:
  design: .hermes/plans/memory-system-redesign-design.md
  design_sha256: cc81485efa188c70285f0ff8df060a191f95731cd8d4fdbe49e69cc45fc93072
  h1_card: .hermes/plans/memory-system-redesign-h1-projection-contract-card.md
  h1_card_sha256: 9c326b9a32c908d81c44c82cd723f9e366671c2b095ae52327659c8222090335
---

# H1 Human Approval Record

## 1. 判定

用户已对上述冻结的主设计与 H1 contract card 给出明确 human `APPROVE`。H1 `Projection Semantics Lock` 已解冻，允许开始 **Phase 3 — Spatial / Embodied projection 与脱敏**。

这份记录是外部、hash-bound 的审批证据；它不会改写已审核的 design/card bytes。progress 中的 H1 状态以本记录为准。

## 2. 已批准的语义范围

本次批准仅覆盖 H1 card 的 C1–C7 和对应的设计条款，包括：

1. 在线 Memory 只消费 Worker evidence、静态 AgentRegistry metadata 和 control/supervision facts；不直接读取 Barrier/simulator/oracle truth。
2. `AgentRegistry` 负责稳定 capability metadata；`WorkerRegistry` 负责带 provenance 的动态节点视图；`MissionRuntime.PhysicalDispatch` 仍是唯一任务控制真相。
3. evidence 按 `scope/epoch → env_step → field policy/source priority → confidence → sequence/event_id` 归约；sequence 不裁决同级事实真相。
4. 迟到 evidence、同级 conflict、跨 epoch fence、field-level partial materialization、三层 revision 与 redaction 都按 C1–C6 实现。
5. terminal-only evaluator 在 Phase 5 只读对比 frozen Memory 和 evaluator-private truth，不回写当前 run。

## 3. 解冻后的允许与禁止

### 允许

- 新会话在重新确认 target HEAD 与 source scope 后，按主设计的 **Phase 3 ledger** test-first 实施；先写/运行 RED tests，再实现最小 reducer/projection 代码。
- 仅触及 Phase 3 `Create`/`Modify` allowlist；重点从：
  - `tests/test_memory_projections.py`
  - `tests/test_memory_online_truth_boundary.py`
  - `src/a2a/coordinator/memory/{contracts.py,store.py,ingestor.py,projections.py}`
  开始。
- Phase 3 的预期源码/测试改动是本批准授权的实施工作，不因这些 allowlisted 改动本身重新冻结 H1。

### 仍然禁止

- 不进入 Phase 4、Phase 5，不切 `read_port`，不申请/假定 H2 或 H3 已批准。
- 不让 `MemoryIngestor`、reducer、Context 或 semantic Coordinator 读取 `SARBarrier.get_env_snapshot()`、checker truth、oracle tool 或 evaluator truth trace。
- 不扩展到 H1 card/主设计之外的语义、来源优先级或 rollout 规则；若需要改变这些语义、当前 target HEAD，或产生 allowlist 外的源码改动，必须停下并重新取得 H1 决策/审查。
- 不把 post-run truth 或 evaluator 结果回灌为本次 run 的 Temporal/Spatial/Embodied 事实。

## 4. 新会话入口检查与 Phase 3 handoff

新会话开始时先执行：

```bash
repo=/home/wyh/daily_work/LLaMAR-memory-redesign
git -C "$repo" rev-parse HEAD
git -C "$repo" diff --quiet -- src sar_orch pyproject.toml
git -C "$repo" status --short
```

预期：HEAD 仍为 `e1a5a01392c814ddcbc33f5e17aa5cad5d9e8fe6`，source scope clean；`AGENTS.md` 的预存修改不得覆盖。若不满足，先报告 drift，不能静默套用本审批。

然后：

1. 重读主设计的 Phase 3 ledger 和 H1 card 的 C1–C6；
2. 首先新增 C1–C6 的 RED tests，尤其验证 `online_truth_forbidden` 的零领域写入；
3. 仅实现满足这些测试的 `NormalizedProjectionInputV1`、field policy、Temporal→projection reducer 和 revision/relation 行为；
4. 运行 Phase 3 focused pytest 与 lint；
5. 完成后在 progress record 写明真实测试证据，但 H2/H3 继续 `PENDING`。

## 5. 失效条件

以下任一情况使本 H1 approval 需要重新确认：

- 主设计或 H1 card 的语义内容变更；
- target HEAD 在 Phase 3 entry 前变化；
- entry 时 `src/`、`sar_orch/`、`pyproject.toml` 已有非本 Phase 3 的 drift；
- 需要使用新的 external truth/oracle 来源，改变 source-priority policy，或触及 Phase 3 allowlist 外的源码。

Phase 3 内的 allowlisted 实现和测试变更不自动撤销 H1；它们必须在 Phase 3 exit 时以 C1–C6 测试和父侧独立验收证明符合本审批。
