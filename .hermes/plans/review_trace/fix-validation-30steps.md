# 修复批次验证记录：30 步 run（2026-08-12）

> **状态**：三处修复验证通过（框架层面）；person 未救起待探索（worker 取水策略）
> **关联**：review_trace 三条问题记录（watchdog 误报 / team ACK 配置不对称 / 任务间隙 60s 超时）
> **验证 run**：`sar_orch/results/20260812_225834_s2_s42_a2`（scene_2 × 2 agents × seed 42 × max_steps 30 × enable_peer_mail）

## 1. 修复内容（本批次）

| # | 修复 | 文件 | 验证 |
|---|---|---|---|
| 1 | watchdog 首次基线化（last_progress_step = 创建时 env_step，不再停 0）；artifact_update 路径补 step 参数 | `src/a2a/coordinator/task_watchdog.py`、`server.py` | tests/test_task_watchdog.py 16 passed（含新回归测试） |
| 2 | team 能力上报（WS 注册带 supports_team_protocol）+ 多参与者节点激活前 fail-fast（不支持则快速失败，不走 30s ACK 补偿） | `worker_registry.py`、`mission_runtime.py`、`shared/types.py`、`coordinator_client.py`、`sar_orch/worker.py` | 相关套件 116+103 passed |
| 3 | 任务间隙空闲心跳（adapter task_lifecycle_cb + worker idle NoOp 循环）→ v2 修正：barrier `advance=False` non-advancing 占位语义（全空闲不推进 step、不触发 60s 超时；有真实动作才步进） | `sar_orch/barrier.py`、`worker.py`、`agent_adapter.py`、`a2a_server.py` | tests/test_barrier_advance_semantics.py 等 72 passed |

过程中发现并修复：idle 心跳 v1 直接调 submit_action 会推进 step（两 worker 空闲心跳 4 秒烧 494 步 → 假完成），v2 用 advance=False 占位语义根治。

## 2. 30 步验证结果

| 指标 | 值 | 结论 |
|---|---|---|
| steps | 30（真实跑满） | ✅ 494 步假完成修复 |
| coverage / transport_rate | 0.667 / 0.667 | 与 20 步 run 同量级 |
| memory acceptance gate | pass（零框架错误） | ✅ worker_busy/task_not_routable_yet/unknown_task_id 全 0 |
| watchdog 创建时误报 | 0（首次 HEALTHY progress_step=5/16） | ✅ 基线化生效 |
| 运行期 STALE | 8 次（5s 内 RECOVERED） | ⚠️ 真实捕捉：LLM 思考/工具慢（对应 60s+ 步长），非误报 |
| DEADLINE_WARNING | 2 次（长任务 5min+） | 正常功能 |
| person 救援 | 未开始（rescue-jeremy 全程 blocked） | ❌ 火蔓延 11 区域，30 步灭不完 |

## 3. 剩余现象（非框架 bug）

1. **运行期 STALE / 60s+ 步长**（step 22=62s、step 27=72s，TimeoutAgents 均为空）：LLM 调用/思考慢导致，barrier 无超时注入。非 bug，但与"灭火效率"相关（慢一步，火多蔓延一分）。
2. **火蔓延失控**：本轮 TownFire 蔓延到 11 区域（上轮 20 步 run 仅 6 区域）。同 seed 42，LLM 决策差异导致灭火更慢。`rescue-jeremy` 依赖（alice-help-townfire active + bob-help-townfire ready）到 run 结束未满足 → 从未激活（events.ndjson 无 activate rescue-jeremy）。
3. **person 未救起根因**：不是步数线性不足，而是灭火效率 vs 蔓延速度赛跑失败。

## 4. 待探索（2026-08-12 开 subagent）

worker 灭火策略效率：**是否每次只取 1 单位水/沙就去灭火**（inventory cap 3，GetSupply 每次 1 单位），导致频繁往返 reservoir、单次灭火循环只灭 1 次。若成立，这是灭火慢/火蔓延的 worker 侧主因。探索结果将决定是否需要 prompt 或工具层调整。

## 5. 证据文件

- run 目录：`sar_orch/results/20260812_225834_s2_s42_a2/`（trajectory.csv、coordinator/supervision_*.ndjson、coordinator/mission_graph.jsonl、events.ndjson、run_metrics.json）
- 假完成 run（已清理）：`20260812_224822_s2_s42_a2`（steps=494, coverage=0）
- 代码：`sar_orch/barrier.py`（advance 语义）、`sar_orch/worker.py`（心跳）
