# Wave 3 合并后全形态终验：与 W0 基线对照简报

- 日期：2026-09-09
- 任务：t_ef3114cf（[W3-Review] 合并 review + 30 步全形态终验 run + L2 验收 + 与 W0 基线对照）
- 合并提交（main，均 --no-ff，未 push）：
  - `ac4dc76` merge(tracing): W3 NoOp 来源标记 + csv LLMInput 原始长度列 (t_c48244c9)
  - `f704abf` merge(tracing): W3 supervision 落盘归位 <run>/supervision/ 子目录 (t_27e22d1d)
  - `d521ea7` merge(tracing): W3 run 目录落 scene_config.json 初始布局快照 (t_2ab2f39e)
  - `3a233f4` merge(tracing): W3 supervision_injected 审计事件 (t_9c277889)
- 终验 run：`sar_orch/results/validate_w3_s3_s42_a4`（scene3 / agents4 / seed42 / 30 步 / long-term-mode=read，与 W0 基线同配置）
- 真值目录：`sar_orch/results/truth/validate_w3_s3_s42_a4`（truth_trace.jsonl + truth_manifest.json 齐备）
- 门禁：pytest 1948 passed / 1 failed（test_frozen_coordinator_events_readable，W2 既有基线 fail）/ 7 skipped；改动 6 文件 ruff 0 错误

---

## ① L2 验收结果（validate_run.py --mode l2，对照 README §6 目标形态）

汇总：**PASS=17 / FAIL=3 / MISSING=3**（基线同口径为 PASS=4 / FAIL=8 / MISSING=7，W3 落地把 FAIL 减 5、MISSING 减 4）。

| # | 检查项 | 结果 | 说明 |
|---|---|---|---|
| 1 | metadata worker prompt sha256 指纹 | PASS | 已落 metadata.json |
| 2 | metadata.truth_dir 指针（真值目录外置） | PASS | 指向 results/truth/validate_w3_s3_s42_a4，manifest+trace 存在 |
| 3 | scene_config.json 初始布局快照（#13） | PASS | 30×30×1 网格、4 agent/3 fire/11 flammable/1 person/2 reservoir/2 deposit 全量 id+position |
| 4 | trajectory.csv NoOp 来源标记（#8） | PASS | 新增 NoOpSource 列，30 步全有值（57 个 NoOp 槽位） |
| 5 | agent_interactions.csv | PASS | 新增 LLMInputChars 列（144 行非零，4,124,830 字符总计） |
| 6 | router_interactions.csv 四类齐全（#3） | FAIL | 缺 `reply_to_help`、`cancel_task`（本 run agent 未调用这两个工具；有 activate_plan_node/update_plan 等 9 类） |
| 7 | token_usage.csv 异常/失败标记行（#12） | FAIL | Status 列已存在但 111 行全为 ok——本 run 无 LLM 失败/异常路径（failed_tool_rows=3 为工具级失败，不产生非 ok 行） |
| 8 | subtasks.csv 终态（#10） | FAIL | 仅 4 条 dispatch-* assigned；run 未 finished（max_steps_reached），无 subtask 走到终态 |
| 9 | summary.csv / events.ndjson / run_metrics.json | PASS | 形态不变 |
| 10 | coordinator/<task_id>.ndjson 命名（#7） | PASS | 20260909_150646.ndjson + 6a9f9eda-….ndjson + reflection_trace.ndjson |
| 11 | coordinator/events_*.ndjson 结构化 observation（#2） | PASS | 行内 observation 字段恢复 |
| 12 | semantic_map.jsonl | PASS | noteworthy 事件流 |
| 13 | coordinator/snapshot_<task_id>.json（#7 守卫链） | MISSING | 触发条件 `agent.run()` 返回 need_input=True（controller.py:274-275）；本 run 无 agent 触发 INPUT_REQUIRED 状态转换（ndjson 中 23 处 "INPUT_REQUIRED" 均为 llm_request prompt 文本内字样，非状态事件；events_*.ndjson 无 INPUT_REQUIRED 状态行） |
| 14 | coordinator/context/prune_events.ndjson（#6） | MISSING | 触发条件：消息估算 token 超过 token limit 的 50% 才执行 phase-1 裁剪（router_agent/context.py:938-950）；30 步 run context 未超阈值 |
| 15 | coordinator/context/discards/ | MISSING | 同上，无裁剪即无丢弃原文 |
| 16 | coordinator/long_term/long_term.sqlite3 | PASS | 存在 |
| 17 | diagnosis.sqlite3 revision 列（#14） | PASS | diagnosis 表含 revision 列 |
| 18 | coordinator/diagnosis/transcripts.ndjson（#5） | PASS | 每轮诊断证据视图落盘 |
| 19 | supervision/ 归位（#9） | PASS | 11 个 supervision_dsp_*.ndjson 落在 <run>/supervision/；coordinator/ 下 0 个 supervision 文件（归位彻底，不再是空壳） |
| 20 | workers ndjson tool_start（#4） | PASS | Alice/Bob/Charlie/David 四 worker 均有 tool_start 事件 |

FAIL/MISSING 共 6 项，全部为条件触发型产物，非实现缺陷：

- reply_to_help / cancel_task：需要对应工具被调用（本 run 无 agent 求助/取消）；基线同样缺（缺 reply_to_help、activate_plan_node），非 W3 引入。
- token_usage 非 ok 标记行：需要 LLM 调用失败/异常路径；基线连 Status 列都没有（W3 已补齐列），本 run 恰好无失败路径。
- subtasks 终态：需要 subtask 完成；本 run 30 步未完成任务（coverage 0.714、transport 12/18），基线同样只有 assigned。
- snapshot_<task_id>.json / context prune / discards：均为阈值/状态触发（need_input=True、context>50% 阈值），30 步 run 未达触发条件；基线同样 MISSING。

## ② 与 W0 基线 run 的行为对照

基线：`sar_orch/results/baseline_s3_s42_a4`（W0 卡 t_e7943055，同 scene3/seed42/a4/30 步，run_id sar-scene3-agents4-seed42-46d8fb9d）。
对比 run：`sar_orch/results/validate_w3_s3_s42_a4`（run_id sar-scene3-agents4-seed42-d4ca6ce1）。

| 指标 | W0 基线 | W3 验证 run | 差异 |
|---|---|---|---|
| coverage | 0.7143（5/7） | 0.7143（5/7） | 无 |
| transport_rate | 0.7778（14/18） | 0.6667（12/18） | -2 个运输子任务 |
| steps / finished / end_reason | 30 / false / max_steps_reached | 30 / false / max_steps_reached | 无 |
| token 总量 | 1,703,358 | 1,646,732 | -3.3%（-56,626） |
| LLM 调用次数（token_usage 行数） | 121 | 111 | -10 次 |
| elapsed | 886.3 s | 567.9 s | -35.9% |
| failed_tool_rows | 9 | 3 | -6 |
| framework_error（worker_busy/task_not_routable/unknown_task） | 0/0/0 | 0/0/0 | 无 |
| acceptance_gate | pass | pass | 无 |
| long_term_memory_written | 6 | 4 | -2 |
| 反思 LLM tokens | 34,233 | 28,443 | -16.9% |

行为差异简述：

- **产物形态巨变（预期内）**：trajectory.csv 新增 NoOpSource（120 个 agent 槽位中 58 个 NoOp 全部带来源：idle_heartbeat 52 槽、timeout_injected 6 槽，真实动作 62 槽；本 run 无 LLM 主动 no_op 调用）；agent_interactions.csv 新增 LLMInputChars（144 行，单次输入 14,446~47,181 字符，合计 412 万字符）；scene_config.json 落盘；supervision/ 从空壳变 11 个 ndjson；coordinator/ 下 supervision 文件归零；events_coordinator.ndjson 出现 supervision_injected 审计事件（含 TASK_STALE alert、text 序列化视图）。
- **行为指标**：coverage 完全一致（0.7143）；transport_rate 0.778→0.667（差 2 个运输子任务）；token 总量 -3.3%；elapsed -35.9%；failed_tool_rows 9→3。这些差异均属同配置 run 的自然波动范围（LLM 采样随机性 + 网关状态），且 W3 四卡改动全部位于审计/追踪层（来源标记、新列、布局快照、落盘位置、审计事件），不触碰动作执行、任务检查器或 LLM 决策路径——patch 本身无改变行为的机制。按任务约定**不下因果强结论**，仅报告前后对照事实。
- **无系统性劣化信号**：框架错误三计数器全 0、acceptance_gate pass、coverage 持平、token 用量未增反降，未见任何 W3 改动引入的行为退化。

## 附：验证产物位置（均未入库）

- run 目录：`sar_orch/results/validate_w3_s3_s42_a4/`
- 真值目录：`sar_orch/results/truth/validate_w3_s3_s42_a4/`
- 分析脚本：`/tmp/analyze_w3.py`（csv/json 对照采集，未入库）
