# rescue-jeremy 激活失败（team_setup_failed）：enable_peer_mail 配置不对称

> **状态**：已定位根因，未修复（待拍板）
> **发现**：2026-08-09（run `memory_acceptance_ff6994c_20260809_032204` 复盘时）
> **类别**：配置不对称 / 能力降级缺失
> **关联**：`sar_orch/coordinator.py`、`sar_orch/worker.py`、`src/a2a/coordinator/team_partition_service.py`、
> `src/a2a/coordinator/production_adapters.py`、`src/a2a/coordinator/sender_service.py`、`src/a2a/worker/agent_adapter.py`

## 1. 现象

scene_2 run 中，`LostPersonJeremy` 救援节点 `rescue-jeremy`（participants=[Alice, Bob]）在 step 19
激活时返回 `Error: team_setup_failed`，节点被 `mark_canceled`，两个 worker 随后收到 team revoke，
救援从未开始（step 20 两人仅 navigate 到 person 位置，run 即因 `max_steps_reached` 结束）。

## 2. 时间线（run 内实测）

| 时间 | 事件 |
|---|---|
| 03:45:48.28 | coordinator `activate_plan_node(rescue-jeremy)` → `prepare_activation`（team_id=`team:rescue-jeremy:r3`）→ 向 Alice/Bob 发 `team_update`（签名信封） |
| 03:45:49 | Alice 收到 team_update **但进入 LLM 循环**（`workers/Alice/Alice/84f256ba-….ndjson` 出现 llm_request）——正常应走本地控制路径秒回 ACK |
| 03:46:18 | `send_control` 30s 超时（`DEFAULT_TIMEOUT=30.0`，`sender_service.py:33`）→ ACK 全部 timeout |
| 03:46:46 | 补偿开始，Bob 收到 `team_revoke` |
| 03:46:55 | activate 返回 `team_setup_failed`（COMPENSATED）→ 节点 canceled |

## 3. 根因：worker 未启用 envelope ingress，team_update 被当普通任务喂 LLM

1. **worker 侧**（`sar_orch/worker.py:403`）：`if self._enable_peer_mail and coord_secret is not None:` 才创建
   `EnvelopeIngress` / `WorkerTeamState` / `EnvelopeAwareAdapter`。本 run `metadata.json` 中
   `enable_peer_mail=false` → `ingress=None` → worker 收到 team_update 信封时按**普通任务**送进 LLM 循环
   （正常路径 `agent_adapter.py:437-438` 应走 `_handle_team_update` 本地安装 + 秒回 "Team installed"）。
2. **coordinator 侧**（`sar_orch/coordinator.py:551`）：`enable_peer_mail` 只控制 ConfigureTeamTool 等
   **工具注册**；而 mission_graph 多参与者节点的 team saga（`mission_runtime.py:890-924` +
   `team_partition_service.activate_node_team`）**不依赖该开关**——delivery adapter 由
   `wire_production_adapters`（`production_adapters.py:188-229`）无条件注入。
3. 结果：worker 无快速 ACK 能力，但 coordinator 仍走 team saga → 30s 超时 → COMPENSATED →
   `team_setup_failed`（`mission_runtime.py:937-983`）→ `_rollback_claim` cancel 节点（`mission_runtime.py:935`）。

LLM 循环耗时实测：Alice 03:45:49 收到 → 03:46:38 仍在思考（工具调用中），远超 30s ACK 窗口。

## 4. 影响评估

- 本 run：救援完全未开始（仅 step 20 两 agent navigate 到 person）。
- 任何 `enable_peer_mail=false` 的 run，只要 mission_graph 出现**多参与者节点**（需要 2+ agent 协作的 rescue），
  激活必然失败——这是确定性故障，不是概率问题。
- 与步数问题叠加：即使 max_steps 放宽，本配置下 rescue 也会失败。

## 5. 修复建议（按优先级）

1. **能力前置检查**：worker 注册/心跳时上报是否支持 team 协议（`enable_peer_mail`）；coordinator 激活多参与者
   节点前检查所有参与者能力，不支持则降级为普通双 dispatch（无 team fence）或明确报错，而不是发出去等超时；
2. **fail-fast**：`activate_node_team` 前检查 delivery adapter 是否可用且各 worker 支持 envelope 协议，
   不支持直接返回明确错误（比 30s 超时 + 补偿更可诊断）；
3. **配置一致性**：实验默认 `enable_peer_mail=true`（与 mission_graph team 功能默认启用对齐），
   或 mission_graph 在 `enable_peer_mail=false` 时禁用多参与者 team 路径；
4. 修复后验证：`enable_peer_mail=false` 下多参与者节点应快速明确失败或正常降级；`=true` 下
   team_update 走本地 ACK（worker ndjson 不再出现 team_update 的 llm_request）。

## 6. 证据文件

- run 目录：`sar_orch/results/memory_acceptance_ff6994c_20260809_032204/scene_2_agents_2/`
  - `workers/Alice/Alice/84f256ba-….ndjson`（team_update 进 LLM 循环，03:45:49 起）
  - `workers/Bob/Bob/00080429-….ndjson`（同，03:46:17 起；03:46:46 收到 revoke）
  - `events.ndjson`（最后一条 `activate_plan_node(related_task_id=rescue-jeremy)` step 19）
  - `metadata.json`（`enable_peer_mail: false`）
- 代码：`sar_orch/worker.py:139-182, 403-407, 460-496`、`sar_orch/coordinator.py:551-598`、
  `src/a2a/coordinator/team_partition_service.py:286-362, 825-894`、
  `src/a2a/coordinator/production_adapters.py:59-185, 188-229`、
  `src/a2a/coordinator/sender_service.py:33, 80-116, 192-221`、
  `src/a2a/worker/agent_adapter.py:392-441, 493-546`
