---
title: G2/R2 Shadow 模式放行审批记录（long_term_mode: off → shadow）
schema_version: 1
gate_id: G2
review_point: R2
conclusion: APPROVE
approved_at: 2026-08-12
reviewed_commit: 8869328
branch: feat/memory-redesign
review_packet: .hermes/plans/长期记忆_反思机制+动态Agentcard接入/长期记忆_反思机制+动态Agentcard接入_G2-R2-review-package.md
review_packet_sha256: d7d2fbf089f918dcf6d39aa26fa2c398d850a2b4679018029a4799e5ce081afc
design_sha256: 169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab
review_points_doc_sha256: 218d7990833015c94a87bd930d4a8f3912222d1256997a132ff4ca2d062ab96d
---

# G2/R2 审批记录（`long_term_mode: off → shadow` 放行）

## 人工决议

用户在本次会话（2026-08-12）批准 G2，原话：

> 通过G2 APPROVE

即 **APPROVE**：放行 `long_term_mode=shadow`（真实 SAR run 中启用 run-local 长期记忆反思写入，滚动 + 终末，published 只落库不注入 Context），并授权 G2-5（P5 实现：coordinator-only read-port 注入的编码 + 离线测试，真实验证与 G3 绑定）。

## 基线变更说明（相对审查包）

- 审查包绑定基线为 HEAD `5413705`（P0–P4 未提交状态）。审批前用户指示提交，P0–P4 已落地为 commit `8869328`（48 文件 +11095/-10，含审查包同版代码；results 数据产物未入库）。
- 本记录 `reviewed_commit` 以实际审批时 HEAD `8869328` 为准；审查包代码内容与提交内容一致，审查结论不受影响。
- 审查包 SHA-256 于写入本记录前重算：`d7d2fbf0…81afc`，与文件绑定一致。

## 授权范围（allowlist）

- `long_term_mode=shadow`：真实 SAR run 中滚动 + 终末反思写入 run-local 长期库（`<memory_root>/long_term/long_term.sqlite3`），published 产物只落库不注入 Context；
- shadow run 触发真实模型反思调用（deepseek-v4-flash，经 opencode.ai 网关）——成本属预期（2026-08-12 确认）；
- **P5 实施授权**：coordinator-only read-port 注入（environment_state / environment_state_provider / coordinator_state_provider 等）的编码 + 离线测试可实施；P5 的真实验证（read 10-run）依赖 G3 审批；
- P5 实现约束：shadow 模式下长期段不得渲染进 Context（12 项 P5 预期 RED 转 GREEN 为准：`test_long_term_environment_state.py` 6 + `test_environment_state_acl.py` 1 + `test_coordinator_state_provider.py` 1 + `test_environment_state_provider.py` 4，实测 2026-08-12）。

## 决策项结论（G2-1 ~ G2-5）

| # | 决策项 | 结论 |
|---|---|---|
| G2-1 | 放行 `long_term_mode=shadow` | **APPROVE**：真实 run 启用 run-local 长期记忆写入（滚动+终末），published 只落库不注入 |
| G2-2 | P0 冻结修正 3 处追认 | **追认**（审查员已独立证实 #1 causation_id 正确必要，#2/#3 断言逐字不变） |
| G2-3 | support digest 列缺口（source_revision/event_digest NULL） | **接受**为已知缺口；用户 2026-08-12 拍板：**P5 实施窗口内一并处理**（补填 source_revision/event_digest，或补填不可行则明确定义为可选列） |
| G2-4 | 他报 capability 观察点（Charlie） | **接受**为观察点，G3 决定是否在观测路径过滤 capability 字段 |
| G2-5 | P5 实施授权 | **授权**：read-port 注入实现 + 离线测试；真实验证与 G3 绑定 |

## 前提核验

- 审批时点 HEAD `8869328`（`feat/memory-redesign`）；P0–P4 已提交，工作区含进度文档后续记录（观察点/变更日志，未提交）。
- shadow 10-run 矩阵（`sar_orch/results/long_term_memory_20260812_130327/`）10/10 全绿：终末 7 completed + 3 skipped（空窗口契约正确）、框架错误码全 0、quality 全优（traceability 1.0 / truth violation 0 / conflict 0 / supersede 0）、reflection_run 全 completed、memories 全 published、support refs 可追溯。
- 真实模型 function-call smoke 5/5 × 3 次独立运行（`long_term_smoke_20260812_*.json`），fail-closed sanity 3/3。
- 测试：pytest 1819 passed（12 项 P5 预期 RED 未实施，跨 4 文件：`test_long_term_environment_state.py` 6 + `test_environment_state_acl.py` 1 + `test_coordinator_state_provider.py` 1 + `test_environment_state_provider.py` 4，P5 完成后转 GREEN）；ruff 本次改动零新增错误。

## 明确不授权（G2 放行边界）

- `long_term_mode=read`（Context 注入）——属 G3，需 read 10-run 矩阵 + ACL 证据 + 独立审批；
- canonical schema migration、Context cutover；
- 反思输入范围扩展（仍只读 callback.*/evidence.projection/supervision.*/control.* 四类）；
- 任何对 H1 reducer、真相隔离、认证边界的改动。

## 授权补充（2026-08-12 用户确认）

- **trace 日志（模型输入/输出记录）已授权实施**：`<results>/coordinator/reflection_trace.ndjson`，每次模型调用一行（system_prompt/user_prompt/tool_schema/raw_response 四件套，truth terms 脱敏），记录点覆盖 completed/rejected/异常路径，写入失败静默跳过，线程安全。用户 2026-08-12 带读 reflection 代码时拍板设计（正常记录、放 coordinator 目录），本记录初始写入时列为"另行决策"；用户 2026-08-12 明确追认已授权，随 P5 一并提交。

## 残留风险（用户接受）

| 风险 | 缓解 | 状态 |
|---|---|---|
| 反思模型输出幻觉/伪造 ref | fail-closed validator（缺 function-call/伪造 ref/truth term/重复 key 全拒）+ shadow 不注入 Context | 已实现 + smoke sanity 3/3 + 10-run 零 rejected |
| 模型调用阻塞实验 | 异步线程 + coalesce + drain join 超时（60s）不阻塞退出 | 10-run 实测无阻塞 |
| 终末反思生产上下文故障 | M1 修复（loop 探测 + offload 线程） | 矩阵重启后 7 completed + 3 skipped 验证 |
| 长期库写入污染 canonical | 独立文件独立 schema；无 LLM 在事务内；锁耗尽 typed retryable | 10-run 无异常 |
| shadow 模式不注入 → 无 Context 风险 | shadow 下不渲染长期段（P5 实现约束） | 设计冻结 |
| 反思成本 | 每 run 4-10 次反思、每次 ~1500 tokens（实测） | 2026-08-12 已确认属预期 |

---

*本记录由父 agent 生成（2026-08-12），绑定用户原话「通过G2 APPROVE」。审批结论已回写进度文档（§1/§2/§3/§4/§5）。*
