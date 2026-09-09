# VERIFY（C1 独立校验，一轮）

- verdict: 可用
- 抽查统计:
  - file:line 断言: 39 条（命中 36 / 偏离 1 / 漂移 2）
  - 矩阵一致性: 4/4 行一致
  - 产物存在性: 全过（含 README 两处"编排放置"独立复核确认）
  - Gap 抽查: 3 条高（H1/H2/H3）→ H2/H3 证据成立；H1 结论成立但含 1 处数字断言错误（见缺陷 #1）

## 抽样明细（39 条 file:line 断言，每卡 ≥5）

### A1-coordinator.md（10 条）
| 断言 | 实测 | 判定 |
|---|---|---|
| agent.py:531-533 log_request 记录 pre_llm 后 messages_for_llm | :528 hooks.pre_llm → :531-533 log_request(messages_for_llm) | 命中 |
| coordinator.py:251-264 activate_plan_node 仅 events.ndjson、不进 CSV | :251-259 log_event("send_message") + :260-264 decision event，无 log_router_interaction | 命中 |
| controller.py:274 snapshot 守卫 task_id | :274 `if result.need_input and task_id:` → save_snapshot | 命中 |
| agent_executor.py:396-403 submit 不传 task_id | :397-403 submit(context_id, user_request, sink, extra_tools, system_prompt_override)，无 task_id | 命中 |
| logger.py:69 文件名模板 | :69 `f"{self._task_id}.ndjson"`；默认值实际在 logger.py:46 `task_id or "unnamed_task"` | 命中（默认来源行号偏移，链条成立） |
| context.py:580 compress_with_llm 仅定义 | :580 async def；全仓 grep 无调用点 | 命中 |
| coordinator.py:374-385 llm_response→token CSV | :374-385 log_token_usage(agent="Coordinator") | 命中 |
| coordinator.py:391-404 update_plan 仅 decision event | :391-404 _append_decision_event，无 CSV | 命中 |
| coordinator.py:815-820 加载 system.semantic.md | :815-820 实测吻合 | 命中 |
| logger.py:629-639 router_interactions 9 列 | :629-639 7+Success+ErrorType=9 列 | 命中 |
| 产物: unnamed_task.ndjson 267 行=71 req+71 resp+125 tool | 实测 267=71/71/125 | 命中 |
| 产物: events.ndjson 79 行、send_message 21=全 activate | 实测 79；send_message payload message_type 全为 activate_plan_node | 命中 |
| 产物: router_interactions 83 行=30/25/28 | 实测 83=assign 30/query_task_events 25/cancel 28 | 命中 |
| 产物: token CSV Coordinator 71 行+MapAgent 58 | 实测 71/58 | 命中 |
| 产物: subtasks 30 行全 assigned | 实测 30 行 Status=assigned | 命中 |

### A2-worker.md（9 条）
| 断言 | 实测 | 判定 |
|---|---|---|
| worker.py:287-292 LLMInput=6 条×200 字符 | :287-291 msgs[-6:] c[:200] | 命中 |
| worker.py:533-552 空闲心跳 NoOp advance=False | :533-538 submit_action("NoOp", advance=False) | 命中 |
| barrier.py:243-247 超时注入 advance=True | :247 `("NoOp", True)` + timeout_agents | 命中 |
| worker_agent/logger.py:86-121 log_request | :86 定义、:101-115 全字段 | 命中 |
| worker_agent/agent.py:546→549 记录裁剪后 messages | :546 pre_llm、:549-551 log_request(messages_for_llm) | 命中 |
| worker context.py:851 prune_history | worker_agent/context.py:851 定义 | 命中 |
| worker context.py:780-790 compression_summary | worker :780 _save_compression_summary；router :783 | 命中 |
| sink.py:59/83/105 截断 2000/1000/12000 | :59 content[:2000]、:83 arguments[:1000]、:105 content[:12000] | 命中 |
| hooks.py:70-86 pre_llm prune+assemble | :85 prune_history、:86 assemble | 命中 |
| 产物: agent_interactions 441 行全 tool_result | 实测 441（物理行 11577 因观察文本含换行，csv 解析后 441） | 命中 |
| 产物: trajectory 15 NoOp=8 LLM+7 系统 | 实测 NoOp 出现 15 次、no_op 工具行 8 | 命中 |
| episodic "content[:200] 摘要" | 实测 context.py:832 content[:500]、summary 为 "tool→status" 格式 | 偏离 |

### A3-environment.md（9 条）
| 断言 | 实测 | 判定 |
|---|---|---|
| env.py:282-361 generate_obs_text（:347 过滤、:354 Globally） | :282 定义、:347 filter、:354 Globally 行 | 命中 |
| env.py:377 get_agent_state 坐标文本 | :377 "I am at co-ordinates" | 命中 |
| barrier.py:327-373 get_env_snapshot（persons :360-363、reservoir :364-366 无水量） | :327 定义、:360-363 Person position/load/status、:364-366 仅 resource_type | 命中 |
| barrier.py:566-585 step log 缓冲 | :570-586 _pending_step_logs.append 全字段 | 命中 |
| logger.py:177-196 log_step 字段 | :177-196 row 字典 18 字段 | 命中 |
| truth_recorder.py:184-237 record_step/_claims_for_step | :184 record_step（1-based→0-based :190-192）、:209-237 六类遍历 | 命中 |
| truth_recorder.py:257-319 字段覆盖、:309-314 reservoir 无水量 | :257 _object_claims、:309-314 仅 resource_type | 命中 |
| experiment.py:476-497 truth 目录强制外置 | :477-484 拒绝 exp_dir 内路径 | 命中 |
| experiment.py:448-459 supervision 空目录 | :448-452 mkdir | 命中 |
| store.py:334-336 noteworthy 过滤 | :333-336 is_new or _is_observation_noteworthy | 命中 |
| 产物: trajectory 31 行、semantic_map 31 条、truth_trace 0、sqlite 0 | 实测全部吻合 | 命中 |

### A4-crosscutting.md（11 条）
| 断言 | 实测 | 判定 |
|---|---|---|
| diagnosis_loop.py:246 transcript 仅内存 | :246 定义、:368-392 _collect_evidence 写入内存列表 | 命中 |
| long_term_reflection.py:377-392 _inflight 覆盖 | :385-387/:389-392 覆盖 _inflight["result"] | 命中 |
| diagnosis.py:671-690 INSERT OR IGNORE | :685-688 INSERT OR IGNORE | 命中 |
| supervision_state_store.py:188-208 全状态行 | :188 _persist、:199-206 {ts, state: to_dict()} | 命中 |
| event_store.py:106-125 落盘字段（text[:500]、丢 observation） | :114-121 ts/task_id/context_id/event_type/state/text[:500]，无 observation | 命中 |
| server.py:1168 set_log_dir | :1168 supervision_store.set_log_dir(self._log_dir) | 命中 |
| task_watchdog.py:79-108 三路落盘 canonical | :79-108 _emit_to_memory → sink | 命中 |
| experiment.py:995-1003 run_metrics 先写 | :997-1003 | 命中 |
| 产物: events_dsp 28 文件 92 行=39/32/16/5 | 实测 92=supervision_event 39/status_update 32/task_created 16/artifact_update 5 | 命中 |
| 产物: TASK_STALE 带 progress_age_seconds、event_id {dispatch}::{counter} | 实测 text 内嵌 progress_age_seconds、event_id 'dsp_...::1' | 命中 |
| 产物: 20260719 supervision_1c364fe4 文件 1 行 | 实测 1 行（24 文件共 426 行，不矛盾） | 命中 |

## 矩阵一致性（清单 2）
- A1 行（有/部分/部分/B）↔ A1 Q1=有、Q2=部分、Q3=部分：一致
- A2 行（部分/部分/部分/B-）↔ A2 Q1=部分、Q2=部分、Q3=部分：一致
- A3 行（部分/部分/部分/C+）↔ A3 Q1=部分、Q2=部分、Q3=部分：一致
- A4 行（部分/部分/部分/C）↔ A4 六组件综合（1 有/有/有、2 有/有/有、3 部分、4 有、5 部分/无/部分、6 有/有/部分）：一致
- 编排放置 1（07-21 有 28 个 supervision_dsp_*.ndjson 共 812 行）：独立复核实测 28 文件 812 行 ✓
- 编排放置 2（supervision schema=SupervisionStateStore 全状态行 ≠ EventStore schema）：独立复核实测 supervision 头 {ts, state:{dispatch_id,...}} vs events_dsp 头 {ts, task_id, context_id, event_type, state, text} ✓

## 产物存在性（清单 3）
- a4 run 全套：router_interactions(83)/agent_interactions(441)/token(440)/subtasks(30)/events.ndjson(79)/trajectory(31)/metadata(code_commit=0119d39)/coordinator/unnamed_task.ndjson(267)/events_dsp×28/supervision_dsp×28/supervision/空目录 —— 全部实测存在
- 7/19 run：semantic_map.jsonl 31 条 observation_ingested、supervision_*.ndjson 24 文件、events_*.ndjson 24 文件 —— 实测存在
- benchmark scene_1/agents_2/seed_10/experiment_logs 4 件（summary/metadata/run_metrics/trajectory）—— 实测存在
- find 全仓 truth_trace：0；results 内 sqlite3：0 —— 实测吻合

## Gap 清单 sanity（清单 4，抽 3 条高）
- H2 环境真值底座空转：truth_recorder.py:184-237 ✓、experiment.py:476-497 ✓、env.py:282-361 ✓、barrier.py:360-363 ✓、find 0 truth_trace ✓ —— 证据成立
- H3 ContextManager 零轨迹：context.py:580 仅定义+全仓无调用 ✓、:477-529 Phase1 ✓、worker :851 prune_history ✓、被裁原文不另存 ✓ —— 证据成立（一处行号口径见缺陷 #3）
- H1 样本老化：核心成立（9 列 CSV 零样本、thinking 零样本、ENVIRONMENT STATE 零样本、sqlite 零样本，全部实测）—— 但含数字断言错误（缺陷 #1），"77 run 全 7 列/code_commit=0119d39" 与实测不符；H1 结论方向不受影响

## 零改动核验（清单 5）
- git status --short：无 tracked 改动；仅既有 untracked（.hermes/plans/、docs/daily_report/、docs/plans/trajectory-audit/ 等，全部为 B1 及更早批次产生）；本批唯一新增 = VERIFY.md
- 未 commit

## 缺陷单

| # | 级别 | 位置(文件+章节) | 问题 | 证据 |
|---|---|---|---|---|
| 1 | 重要 | README H1 证据列 / A1-G1 | "77 个 run 全部 7 列、code_commit=0119d39" 数字断言与实测不符：含 router CSV 的 77 run 中 65 个 7 列 + 12 个 3 列（sar_experiment_20260704/05 早期 run）；code_commit 分布 11 种（c1f408d×46、66cc5d3×12、ba907ce×5、ef2dcbf×4、debdda7×4、914b9cb×3、6c93f15×2、0119d39×2、c0054ce/537e5fa/e56d20a/a4b8893 各 1），0119d39 仅为 07-21 两个抽样 run | python 全量扫描 98 目录 metadata.json + router CSV header 列数统计 |
| 2 | 建议 | A2 Q3 / Gap#2（episodic 描述） | "episodic 仅 assistant content[:200] 摘要" 与实测不符：worker context.py:832 截断为 content[:500]，_Episode.summary 格式为 "tool_name → status: 截断文本" 而非纯 assistant 消息摘要；"只存内存不留盘"核心结论正确 | worker_agent/context.py:830-847 实测 |
| 3 | 建议 | A1 G2 证据 / README H3 | "context.py:851-906（被裁原文不另存）" 行号口径：router_agent/context.py:851-906 实为 assemble 区，prune_history 定义在 :836-848（:848 调 _compress_phase1）；:851 精确对应 worker_agent/context.py（A2 引用无误）。被裁原文确实不另存，语义正确 | router_agent/context.py:836-852 实测 |

## 结论
- 阻断缺陷 0，重要 1，建议 2。
- 核心结论链（Q1/Q2/Q3 三部分评级、矩阵、两大编排放置、H2/H3 证据、产物存在性）全部独立复核成立。
- 缺陷 #1 属统计数字错误，不改变 H1"全部产物为旧代码样本、新特性零真实样本"的结论方向；建议 B1/后续修订 README H1 证据列时改为实测分布。
- verdict: 可用（建议择机修订 #1-#3 数字/行号口径）。
