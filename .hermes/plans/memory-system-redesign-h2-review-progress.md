---
document_type: human-gate-review-progress
schema_version: 1
project: LLaMAR-memory-redesign
gate_id: H2
review_title: Read-Port Cutover
review_status: APPROVED
approval_status: AUTHORIZED
approval_record: .hermes/plans/memory-system-redesign-h2-approval-record.md
reviewed_commit: 04147016e396429b03c8bb3d19b7165f631f8be2
branch: feat/memory-redesign
recorded_at: 2026-08-07T12:14:03+08:00
---

# H2 人工必要审查进度 v0.1

> 用途：这是 H2 的可恢复审查账本，不是实现计划、coder 工作日志或最终批准记录。
>
> 写入规则：后续只在“审查日志”末尾追加；已记录的 source freeze、事实、结论和决定不静默改写。若源码、packet 或审批范围变化，创建新的 review round，并把旧 round 标为 historical。

## 1. 当前门禁状态

| 项目 | 当前值 |
|---|---|
| 人工决策 | 是否授权在已审查目标上启用 `memory_read_mode=read_port` |
| 当前状态 | `APPROVED`（2026-08-08 用户批准 packet v2）；正式记录见 `memory-system-redesign-h2-approval-record.md`。`read_port` 启用已授权，legacy 仍为默认 |
| 实际候选源码 | `feat/memory-redesign@04147016e396429b03c8bb3d19b7165f631f8be2` |
| 审查范围 | Phase 4 read-port 进入 LLM Context 的正确性、安全边界、ACL、freshness、shadow 与 rollback |
| 明确不在范围 | Phase 5 exporter/recovery、legacy retirement、H3 决策；`AGENTS.md` 的仓库路径文档修改 |
| 当前唯一下一跳 | 核对 worker read-port 中 Barrier-derived runtime snapshot 是否会在同一 LLM 请求泄漏进 legacy/pinned Context |

## 2. Source freeze 与审查输入

### 2.1 当前源码边界

- 仓库：`/home/wyh/daily_work/LLaMAR-memory-redesign`
- 分支：`feat/memory-redesign`
- 审查 commit：`ac10cc42a626e37610a64cb41cfc5b737f8ac1ca`
- 工作区例外：仅 `AGENTS.md` 仍修改；它只把文档中的绝对仓库路径改为 `git rev-parse --show-toplevel`，不属于 H2 代码候选，未纳入本次 commit 或本次审查。
- 审查时点：`2026-08-07T12:14:03+08:00`

### 2.2 冻结工件

| 工件 | SHA-256 | 审查用途 |
|---|---|---|
| `memory-system-redesign-design.md` | `cc81485efa188c70285f0ff8df060a191f95731cd8d4fdbe49e69cc45fc93072` | H2 合同、禁区和准入条件 |
| `memory-system-redesign-h1-projection-contract-card.md` | `9c326b9a32c908d81c44c82cd723f9e366671c2b095ae52327659c8222090335` | H1 在线 truth boundary 前提 |
| `memory-system-redesign-h2-read-port-review-packet.md` | `8c7e575be3b582476e5a8e115ea27e81f3e10452378dd8d0450015a999a8d089` | coder 提供的 H2 候选验收包 |

### 2.3 必须先解决的审批元数据边界

- **源码事实**：现有 H2 packet 的 `target_head` 是 `e1a5a013…`，而实际待审实现已提交为 `ac10cc4…`。
- **影响**：旧 packet 可以作为历史/候选证据，但不能直接绑定对 `ac10cc4…` 的人工批准。
- **准入门**：正式作出 H2 决策前，coder/计划维护者必须生成新的 immutable packet revision，或明确补充一个绑定 `ac10cc4…`、本 review 文档 hash 与精确 rollout allowlist 的审批输入。
- **状态**：`BLOCKED_FOR_APPROVAL_METADATA`；不阻止继续静态源码审查。

## 3. 术语与所有权地图

```text
Context
  = 每轮 LLM 消息装配、raw history、cursor、skill refs、临时 runtime projection

canonical Memory
  = coordinator-owned SQLite 中按 scope 保存的 Temporal / Spatial / Embodied 事实

Bridge
  ContextManager --generic EnvironmentStateProvider protocol-->
  MemoryReadPort + ControlPlaneReadPort --> canonical Memory + MissionRuntime
```

- `Context` 不是 canonical Memory 的持久化 owner。
- `Memory` 不是 LLM 的 raw conversation/history owner。
- `EnvironmentStateProvider` 是只读 bridge；它把已授权的 Memory/Control-plane view 转成 Context 可以渲染的 DTO。

## 4. 审查日志（append-only）

### R0 — 审查对象与可恢复边界

- 状态：`COMPLETE_WITH_METADATA_BLOCK`
- 源码事实：当前代码候选是 `ac10cc4…`；工作区唯一差异为非 H2 的 `AGENTS.md`。
- 结论：可以针对 commit 继续审查；不能在旧 H2 packet 的 `e1a5a013…` 绑定下批准。
- 准入门：见 §2.3。

### R1 — Context 与 canonical Memory 是否职责分离

- 状态：`PARTIAL_PASS — static source`
- 已读锚点：
  - `src/Agent/router_agent/context.py:81-90, 152-167, 317-354, 917-921`
  - `src/Agent/environment_state.py:28-44, 73-112`
  - `src/a2a/coordinator/memory/store.py:261-282`
  - `src/a2a/coordinator/memory/ingestor.py:244-250, 335-348`
  - `sar_orch/environment_state_provider.py:243-317`
- 源码事实：
  1. Context 的持久 snapshot 只写 messages、skill refs 与 `(scope_id, viewer_id, cursor)`；不序列化 pinned、RuntimeState 或领域 projection。
  2. canonical Memory 由 coordinator-owned SQLite `MemoryStore` 和 `MemoryIngestor` 的 canonical transaction 所有。
  3. Context 通过通用 `EnvironmentStateProvider` 协议查询，而不是直接导入 `sar_orch` / Memory backend。
  4. provider 只读 `MemoryReadPort` 和 `ControlPlaneReadPort`，将内容构造为 `EnvironmentStateView`。
- 边界/未完成项：Context 仍保留 legacy pinned state，且 provider 仍提供 `RuntimeState` compatibility facade。因此 H2 已建立依赖、所有权与持久化分离，但尚未完成 legacy path retirement；这属于 H3 前预期的兼容状态。
- 运行证据：未运行。

### R2 — Coordinator 的 read-port 如何进入 LLM Context 与回退

- 状态：`COMPLETE — static source`
- 已读锚点：
  - `sar_orch/coordinator_state_provider.py:149-234`
  - `src/Agent/router_agent/context.py:917-921, 925-976, 1030-1045`
- 源码事实：
  1. `SARCoordinatorStateProvider._attach_read_port_provider()` 将 `MemoryReadPort` 与 `ControlPlaneReadPort` 组成 concrete provider。
  2. Context 在装配时把 Environment State 追加为 `role="user"` 消息，而不是 system prompt。
  3. 只有 `memory_read_mode == "read_port"` 且 provider 返回 `FRESH` view 时，read-port 文本替代 legacy Environment State。
  4. provider exception、`STALE` 或 `UNAVAILABLE` 会触发 rollback latch 并返回空文本，随后回退 legacy；同一视图不拼接两类领域 truth。
- 结论：Coordinator 侧存在明确的 fail-closed 渲染分支；尚未独立验证真实运行的 audit 与一次性 latch。

### R3 — Worker 身份、缓存 view 与 Context 可见性

- 状态：`IN_PROGRESS`
- 已读锚点：
  - `src/Agent/worker_agent/hooks.py:70-86`
  - `sar_orch/worker_state_provider.py:75-203, 270-359`
  - `src/a2a/coordinator/server.py:410-463, 1753-1862`
- 源码事实：
  1. worker `pre_llm` 在 `read_port` mode 先调用 `fetch_environment_state_async()`，再刷新 runtime state、prune history、assemble Context。
  2. HTTP 请求携带 server-issued opaque `worker_task_id`；server 通过 `worker_task_id → active dispatch → worker_id` 推导 principal，未知/终态/未注册 worker fail-closed。
  3. server 使用该派生 principal 构造 worker-scoped `EnvironmentStateProvider`，再执行 query。
  4. `SARWorkerStateProvider.snapshot()` 仍读取 Barrier 形成 legacy runtime payload；仅从已读源码不能证明这些 fields 在 `read_port` 的同一 LLM request 中永远不会经 pinned/其他渲染面泄漏。
- 当前风险分类：`CONTRACT_CANDIDATE`，不是已确认漏洞。
- 准入问题：证明 worker fresh read-port block 会排他地成为该轮的 Environment State，且 Barrier-derived legacy payload 不会在同一 LLM 输入中作为领域 truth 附加。

### R4 — Shadow compare 与可见差异

- 状态：`NOT_STARTED`
- 所需证据：同一 scope/principal/env step 的 legacy/new 并列 view、allowlist 定义、`memory_rollout_audit.ndjson` 的脱敏差异记录、真实 `non_allowlist_diff_count=0` 产物。
- 当前证据等级：H2 packet 声称 `clean_runs=1` 与 `non_allowlist_diff_count=0`；未由本审查重新运行或读取对应运行产物，标记为 `AUTHOR_REPORTED`。

### R5 — Freshness、rollback 和 canonical 不变性

- 状态：`NOT_STARTED`
- 所需证据：`FRESH/STALE/UNAVAILABLE` 的实际渲染样例；ACL/provider failure 后一次性 rollback audit；rollback 前后 canonical event count、revision、outbox 的对照；成功 response 不 rollback 的反例。
- 当前证据等级：H2 packet 声称相关 tests 149 passed；本审查尚未 fresh rerun，标记为 `AUTHOR_REPORTED`。

### R6 — 当前工作树 H2 修复与 worker read-port 排他性

- 状态：`COMPLETE — offline test evidence; candidate freeze pending`
- 源码事实：在 `ac10cc4...` 之上的未提交工作树中，worker read-port 对 missing/non-FRESH、HTTP/ACL 和 injected-client failure 均一次锁存 legacy；同轮渲染测试断言 canonical block 与 legacy pinned block 不混合。HTTP payload 使用非零 budget；experiment 向 worker 传递 `memory_read_mode`。
- 测试证据：新增 worker rollback/secret-redaction/correlation/budget/exclusivity tests 与 route test 通过；当前工作树的完整测试最近一次为 `1408 passed, 3 skipped`。这是实现测试，尚非绑定候选 commit 的 rollout 证据。
- 结论：R3 的 fresh-success 排他性与 failure fallback 的 latch/audit 均已有离线测试覆盖；尚无 live `read_port` LLM-request artifact。

### R7 — 真实 shadow runtime evidence

- 状态：`COMPLETE — runtime evidence, candidate freeze pending`
- 运行：`sar_orch/results/shadow_verify_20260807_204914`；scene 1、2 agents、seed 42、12 steps、semantic + explicit `shadow`，direct run 无 runtime workaround，`max_steps_reached`，coverage `0.6667`，transport `0.6`。
- 证据：canonical DB 在 coordinator server thread 正常读写；pristine DB SHA-256 为 `c473bd74caea6b7f0424f4ac3d47b39b827a6b1f8db0d8405880fac9b84e379b`。无 `coordinator/memory_rollout_audit.ndjson`，表示未出现 non-allowlist diff。对持久 `semantic_map.jsonl` 与 Memory projection 的 deterministic replay 得到 `runs=1`、`clean_runs=1`、`non_allowlist_diff_count=0`、`last_clean=true`。
- 限制：该 run 实际使用未提交 H2 修复，而 `metadata.json` 的 `code_commit=ac10cc4...` 只是运行时 HEAD label；不得把本 evidence 归因给 committed baseline。live `read_port` rollback drill 仍未执行。

### R8 — 决策元数据状态

- 状态：`BLOCKED_FOR_APPROVAL_METADATA`
- 事实：现有 packet 仍绑定 `e1a5a013...`；clean evidence 依赖未提交 patch，不能以 `ac10cc4...` 或旧 packet 申请批准。
- 下一步：用户将当前 H2 patch 冻结为新 candidate commit 后，重新执行 packet verification 与 direct shadow run，生成 packet v2（绑定新 commit、冻结 design/H1 hash、packet/progress hash 和 precise rollout allowlist）。之后仍需提供 live read-port rollback drill，方可进入最小人工决策。
- 独立复验：当前工作树的 packet 11-file command 于本轮重跑为 `166 passed, 46 warnings`；这提升测试证据，不解除 candidate-freeze 或 live rollback-drill 的门禁。

### R9 — H2 候选冻结与决策入口

- 状态：`READY_FOR_HUMAN_DECISION — NOT_AUTHORIZED`
- 候选：`feat/memory-redesign@04147016e396429b03c8bb3d19b7165f631f8be2`；源码范围为提交内容，工作区仅有排除的 `AGENTS.md` 修改。
- packet v2：`.hermes/plans/memory-system-redesign-h2-read-port-review-packet-v2.md`，SHA-256 `d9f84e35b8f581c98737e72d0d68582348e5300b30f772180e0a4115c14d1268`。design/H1 hashes 与冻结输入一致；旧 packet 已由 v2 supersede。
- R3：worker read-port fresh-success 排他性、failure fallback 的一次 latch、redaction、correlation 和 nonzero budget 均有 candidate tests；无 live read-port LLM artifact。
- R4：direct run `shadow_genuine_0414701_20260807_225809` 的 `metadata.code_commit` 与候选一致，live audit 无 non-allowlist diff；offline replay `runs=1, clean_runs=1, non_allowlist_diff_count=0`。
- R5：freshness/ACL/rollback/canonical immutability/thread safety/supervision seam 由 candidate tests 覆盖；production read-port rollback drill 未执行，属于 H2 批准前主动保持的安全边界，已在 packet v2 明示。
- 决策输入：人工需仅针对 packet v2 的精确 rollout allowlist 作 `APPROVE` 或 `REJECT`。最终决定必须新建独立 H2 approval record，绑定 candidate HEAD、packet v2 hash、本 review-progress freeze hash、design/H1 hashes、用户结论和时间。

### R10 — 用户批准与门禁关闭

- 状态：`APPROVED — USER HUMAN GATE CLOSED`（2026-08-08T00:33+08:00）
- 批准记录：`memory-system-redesign-h2-approval-record.md`，绑定 `0414701`、packet v2 hash `d9f84e35...`、design/H1 hashes、review-progress freeze hash 与 rollout allowlist。
- 边界：仅授权在 `0414701` 上启用 `read_port` 进行 scene 1 / 2 agents / seed 42 / max_steps 30 的真实实验；legacy 仍为默认；H3 仍为 `PENDING`。

### R11 — read_port 真实 rollout（candidate `51e9e39`）

- 状态：`PASS — rollout verified`
- 运行：`sar_orch/results/read_port_semantic_s1_a2_s42_51e9e39_20260808_113934`，scene 1 / 2 agents / seed 42 / max_steps 30，semantic + `read_port`。steps 30/30，coverage 1.000，transport 0.9333，`max_steps_reached`。
- 证据：Alice/Bob 全部 99 个 Environment State block 均带 `scope_id`/`view_revision`/`freshness: FRESH`/canonical spatial/embodied；coordinator 28/28 canonical block；零 rollback、零 rollout audit；canonical DB 正常写入（312 temporal / 52 projection_field / 147 outcome / 144 relation / 299 outbox）。
- 安全：19 条 `callback_auth_rejected/unknown_worker_task` 是启动期绑定竞态的正常 fail-closed，后续 push 与 read-port 均正常；无 secret 泄漏。
- 测试：packet 11-file suite `179 passed, 50 warnings`（既有 datetime deprecation）。
- 结论：H2 `read_port` 在获批参数与候选提交上通过真实 rollout 验证；门禁已关闭。
- 决策：用户明确批准 H2 packet v2（原话"批准"）。正式记录见 `.hermes/plans/memory-system-redesign-h2-approval-record.md`，绑定 candidate `0414701`、packet v2 SHA-256 `d9f84e35b8f581c98737e72d0d68582348e5300b30f772180e0a4115c14d1268`、design `cc81485e…`、H1 card `9c326b9a…`、review-progress freeze hash `784d6ebf…`、批准时点账本 hash `940bfd81…` 与 rollout allowlist。
- 允许：在 `0414701` 上启用 `memory_read_mode=read_port`（经既有 `EnvironmentStateProvider` bridge 与 authenticated worker route）；legacy 保持默认、shadow 保留；`SHADOW_COMPARE_ALLOWLIST`（`sar_orch/environment_state_provider.py:780`）零 non-allowlist diff 为 gate，不新增字段/实体例外；worker 视图保持 `worker_task_id -> active PhysicalDispatch -> worker_id` principal；rollback 限一次性 `read_port -> legacy` latch，canonical Memory/outbox 不变。
- 未授权：H3（exporter/recovery、legacy retirement）仍 `PENDING`，需单独决策；不得改变 H1 truth boundary；不得扩展 allowlist 例外。
- 下一步：live `read_port` LLM run 与 production rollback drill 是本批准授权的 rollout 验证动作，执行产物作为 H3 讨论的输入证据。

## 5. 证据账本

| 证据 ID | 类型 | 当前状态 | 内容 / 位置 |
|---|---|---|---|
| E-01 | current source | VERIFIED | `ac10cc4…` 的 Context → provider → coordinator Context 分支（R1/R2） |
| E-02 | current source | PARTIAL | worker task-bound principal / provider 路径（R3）；仍需完成可见性排他性核对 |
| E-03 | test claim | AUTHOR_REPORTED | H2 packet `149 passed`、42 个既有 warning；未 fresh rerun |
| E-04 | runtime artifact | NOT_INSPECTED | `<log_dir>/memory_rollout_audit.ndjson` 尚未取得/核验 |
| E-05 | static integrity | VERIFIED | design、H1 card、H2 packet 的 SHA-256 见 §2.2 |

## 6. 尚未到达的人工决策

当前不能批准或拒绝 H2；尚未达到可供你最小注意力决策的证据条件。

到达决策卡前，至少需要：

1. 绑定 `ac10cc4…` 的 H2 审批输入与明确 rollout allowlist；
2. R3 关闭或升级为明确的 Major admission gate；
3. R4 的真实 shadow artifact 与 allowlist diff 复核；
4. R5 的 freshness / rollback / canonical immutability 证据；
5. 审查结论明确区分静态源码、重新执行的测试和真实运行证据。

## 7. 恢复协议

下次继续 H2 审查前：

1. 重新检查仓库根目录、branch、HEAD、worktree exception；
2. 重新计算本文件、H2 packet、设计和 H1 card 的 hash；
3. 若 commit 或任一输入 hash 变化，保留本 v0.1 为 historical，并创建 v0.2 review round；
4. 若输入一致，从 R3 的唯一下一跳继续，不重复 R1/R2：

```text
SARWorkerStateProvider.snapshot() 的 Barrier-derived payload
  → WorkerContextManager 的 read_port / legacy render 分支
  → 最终 provider messages 中是否仍存在同轮 legacy truth
```

## 8. 可复制的审查条目模板（example）

```markdown
### R<n> — <一个可验证的问题>

- 状态：`NOT_STARTED | IN_PROGRESS | COMPLETE | BLOCKED`
- 审查对象：`path:symbol:line`
- 要证明的合同：<一句话>
- 源码事实：<只写当前代码直接可见的内容>
- 调用关系推断：<如有，明确标为推断>
- 测试 / 运行证据：<命令、产物路径、hash；没有则写未运行>
- Finding / 风险：<事实、影响、严重度；Major 必须转成准入门>
- 下一步：<唯一 source hop 或唯一待取证工件>
```

## 9. 最终决定记录规则

- 最终 `APPROVE` / `REJECT` 不写在本进度文档中替代正式记录。
- 决定到来时，新建/更新单独的 H2 approval record，绑定：gate ID、`ac10cc4…` 或新的候选 commit、冻结 packet revision/hash、本进度文档 hash、rollout allowlist、用户结论、日期。
- 无该批准记录即保持 fail-closed：不得启用 `read_port`。
