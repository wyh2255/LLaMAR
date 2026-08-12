# TaskWatchdog TASK_STALE 误报（任务创建于环境中期）

> **状态**：已定位根因，未修复（待拍板）
> **发现**：2026-08-09（run `memory_acceptance_ff6994c_20260809_032204` 复盘时）
> **类别**：实现缺陷（watchdog 状态初始化）
> **关联**：`src/a2a/coordinator/task_watchdog.py`、`src/a2a/coordinator/supervision_state_store.py`、`src/a2a/coordinator/server.py`

## 1. 现象

`scene_2_agents_2` run 中，coordinator 轮 11（03:43:07）`query_task_events` 返回 Bob 任务
`dsp_d859e35d` 状态 RUNNING 但 `events = ["supervision_event", "supervision_event"]`。
对应两条 supervision 事件：

| 事件 | 时间 | 内容 |
|---|---|---|
| `TASK_STALE` | 任务创建后 9s（03:42:14） | `progress_age=10s`、`steps_since_progress=4` |
| `TASK_RECOVERED` | +5s（03:42:19） | Bob 在 step 5 提交首个动作后自动恢复 |

**Worker 本身无任何异常**：Bob 从任务启动到完成全程正常（查询 → 取水 → 灭火 → 03:45:31 正常 COMPLETED，见
`workers/Bob/Bob/b653eefc-…ndjson`）。

## 2. 根因

`SupervisionState.last_progress_step` 初始值为 0，任务创建于**环境中期**（本 run 为 step 4 / step 10）时，
step 维度从创建瞬间起就是"欠账"状态：

```
stale_by_steps = env_step - last_progress_step >= no_progress_step_threshold(3)
                = 4 - 0 = 4 ≥ 3   →  首次检查即触发
```

链路（均带行号证据）：

1. `supervision_state_store.py:154` — `get_or_create` 构造 `SupervisionState`，`last_progress_step` 取 dataclass 默认值 0；
2. `task_watchdog.py:283-294` — `_refresh_progress_by_domain_delta`：`last = state.last_metrics; if last:` — **首次 tick 时 `last_metrics` 为空 → 跳过初始化**，`last_progress_step` 保持 0；且后续 tick 环境指标未变（step 4→4）时 `changed=False`，始终不更新；
3. `task_watchdog.py:351-354` — `_check_stale`：`stale_by_time`（>120s）或 `stale_by_steps`（≥3 步无进展）任一成立即告警；
4. `task_watchdog.py:242` — `in_grace`（10s）只延迟检查，**不参与 last_progress_step 初始化**；首次 tick 在创建时立即执行，grace 一过（10s）即误报。

恢复路径：worker 首个动作提交 → 域指标变化（coverage/transport_rate）→ `changed=True` →
`last_progress_step = env_step` → 下个 tick（5s 内）发 `TASK_RECOVERED`。

## 3. 系统性验证（3/3 全部中招）

| dispatch | worker | 创建于 step | STALE 时间 | RECOVERED 时间 |
|---|---|---|---|---|
| `dsp_03b5011d` | Alice | 4 | 创建后 9s | 首动作后（step 5） |
| `dsp_d859e35d` | Bob | 4 | 创建后 9s | 首动作后（step 5） |
| `dsp_78e980e7` | Alice | 10 | 创建后 9s | 首动作后（step 12） |

证据：`coordinator/supervision_dsp_*.ndjson`（每 5s 一条状态快照，首条即 `last_progress_step=0` + `env_steps=4/10`）。

## 4. 影响评估

- **不致命**：watchdog 只发事件不自动取消/重派；本 run coordinator 仅观察到 "stale alerts recovered"，未误取消任务。
- **有风险**：`TASK_STALE` 写入 EventStore 与 Memory canonical 记录（`task_watchdog.py:370-377` 有
  `supervision_task_stale` memory-producer 注释），污染监督/记忆数据；且 coordinator prompt 明确指导
  "看到 TASK_STALE 考虑 cancel 重派"（`prompts/coordinator/system.semantic.md`），高步数/长任务场景可能诱导
  误取消健康任务。
- 顺带发现：`server.py:1702` artifact_update 路径调 `record_progress` 未传 `step`（默认 0，
  `if step > 0` 不成立），该路径只会刷新 `last_progress_at`、`last_progress_step` 持续落后
  （observation_report 路径 `server.py:1113` 正确传了 `step`）。

## 5. 修复建议（按优先级）

1. **首次基线化**：`_refresh_progress_by_domain_delta` 中 `last` 为空时也设置
   `last_progress_step = env_step`（把创建时刻的环境步数作为基线），而不是跳过；
2. 或 **新任务守卫**：`_check_stale` 对从未有进展的任务（`last_progress_step == 0` 且未 RECOVERED 过）
   只判时间维度，不判 step 维度；
3. **补 step 参数**：`server.py:1702` artifact_update 路径的 `record_progress` 补
   `step=self._barrier._step_counter if self._barrier else 0`。

修复后验证：5 scenes × 2 agent counts = 10 组交叉验证（max_steps=20, seed=42），
检查 `supervision_*.ndjson` 中创建于中期（step>0）的任务不再产生 TASK_STALE；ruff + pytest + smoke。

## 6. 证据文件

- run 目录：`sar_orch/results/memory_acceptance_ff6994c_20260809_032204/scene_2_agents_2/`
- 复盘文档：`sar_orch/results/memory_acceptance_ff6994c_20260809_032204/scene_2_agents_2/run_review.md`（轮 11）
- 代码：`src/a2a/coordinator/task_watchdog.py:283-294, 343-389, 493-527`、
  `src/a2a/coordinator/supervision_state_store.py:148-176`、
  `src/a2a/coordinator/server.py:1107-1114, 1698-1708`
