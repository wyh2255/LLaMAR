# Phase 5 Smoke 验证报告

**日期**: 2026-07-19
**实验目录**: `sar_orch/results/20260719_124838_s1_s42_a2`

## 实验配置

- **Scene**: 1
- **Agents**: 2 (Alice, Bob)
- **Seed**: 42
- **Max Steps**: 30
- **实际完成**: 26 steps, Finished=True

## 实验结果

| 指标 | 值 |
|------|-----|
| Total Steps | 26 |
| Final Coverage | 1.0 |
| Final Transport Rate | 1.0 |
| Finished | True |

## 6 个检查点验证

### 1. 工具发现 ✅

**证据**: `workers/Alice/Alice/*.ndjson` 中 `Available Tools` 列表包含：
- `map_agent__get_fire_info`
- `map_agent__get_person_info`
- `map_agent__get_reservoir_info`
- `map_agent__get_task_context`
- `map_agent__query_natural`

Worker 通过 MCP 成功发现 Map Agent 工具。

### 2. MCP 调用 ✅

**证据**: `agent_interactions.csv` 中大量 `map_agent__*` 调用：
- `map_agent__get_fire_info(CaldorFire)` — Alice
- `map_agent__get_fire_info(GreatFire)` — Alice
- `map_agent__get_person_info(LostTimmy)` — Alice
- `map_agent__get_person_info(TrappedTina)` — Alice
- `map_agent__get_reservoir_info(Water)` — Alice
- `map_agent__get_task_context()` — Alice
- `map_agent__get_fire_info(CaldorFire)` — Bob
- `map_agent__get_reservoir_info(Sand)` — Bob

### 3. Context 注入 ✅

**证据**: `workers/Alice/Alice/*.ndjson` 中 `Context Memory` 块包含：
```
### Team Coordination
  - Bob: at {'r': 4.242640687119286, 'coords': {'x': 7, 'y': 4, 'z': 0}} | task= (UNKNOWN) | empty
```

Team coordination 块成功注入 worker context。格式为 dict 而非简单 tuple，但不影响功能。

### 4. 多人救援 ✅

**证据**: `agent_interactions.csv` 显示：
- Step 15: Bob `carry_person(LostPersonTimmy)`
- Step 17: Alice `carry_person(LostPersonTimmy)`
- Step 23: Bob `drop_off_person(LostPersonTimmy, DepositFacility)`
- Step 26: Alice `drop_off_person(LostPersonTimmy, DepositFacility)`

多人救援成功执行，且 worker prompt 中包含 `[CARRYING PERSON]` 标记说明。

### 5. Token 对比 ⚠️ N/A

无历史 baseline 数据（同 scene/seed 使用旧 `query_shared_memory` 的实验）。MapAgent token 使用已记录：

| Agent | Prompt Tokens | Completion Tokens | Total Tokens |
|-------|--------------|-------------------|--------------|
| Alice | 280,016 | 11,839 | 291,855 |
| Bob | 266,620 | 9,116 | 275,736 |
| Coordinator | 393,402 | 14,541 | 407,943 |
| **MapAgent** | 6,394 | 994 | 7,388 |
| MapSummarizer | 4,468 | 1,579 | 6,047 |

MapAgent 新增 token 消耗为 7,388（prompt 6,394 + completion 994）。

### 6. 指标无回归 ✅

**证据**: `summary.csv` 中 `FinalCoverage=1.0`, `FinalTransportRate=1.0`。实验成功完成，无指标回归。

## 发现的问题

1. **Team Coordination 格式**: Bob 的位置显示为 `{'r': 4.242640687119286, 'coords': {'x': 7, 'y': 4, 'z': 0}}` 而非简单的 `(x,y,z)`。这是 Phase 4 `_render_team_coordination` 的格式问题，不影响功能但可读性差。

2. **worker_not_found 根因**: 端口 8080/8191/8192 被之前实验的残留进程占用，导致新实验的 worker 无法绑定端口，A2A server 启动失败，进而 `_fetch_agent_card` 失败，coordinator 使用 minimal registration 但后续 `send_message` 因某种原因找不到 worker。清理端口后实验成功。

## 建议

1. **修复 Team Coordination 格式**: 将 dict 格式改为简单的 `(x,y,z)` 字符串，提高可读性。
2. **实验前清理端口**: 在 `experiment.py` 启动前检查并清理残留进程，或添加端口冲突检测。

## 结论

Phase 5 smoke 验证通过。Map Agent MCP 化 + 团队协作功能在真实场景中工作正常。
