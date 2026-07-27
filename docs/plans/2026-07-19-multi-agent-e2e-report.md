# 多 Agent 端到端验证报告

**日期**: 2026-07-19
**实验配置**: Scene 1-2 / Agents 3-4 / Seed 42 / Max Steps 30

## 实验结果汇总

| 实验 | Agents | Steps | Coverage | Transport | Finished | MCP 调用 | 多人救援 |
|------|--------|-------|----------|-----------|----------|----------|----------|
| scene 1 / a3 | 3 | 28 | 0.33 | 0.20 | False | 18 | 0 |
| scene 1 / a4 | 4 | 29 | **1.00** | **1.00** | **True** | 4 | **9** |
| scene 2 / a3 | 3 | 30 | 0.67 | 0.67 | False | 35 | 0 |
| scene 2 / a4 | 4 | 30 | **1.00** | **0.93** | False | 14 | **32** |

## 关键发现

### 1. 4 Agents 显著优于 3 Agents

- **Scene 1**: 3 agents 完成率 33%，4 agents 完成率 100%
- **Scene 2**: 3 agents 完成率 67%，4 agents 完成率 93%

4 agents 能同时处理多个任务（灭火 + 救援），3 agents 在 max_steps=30 内无法完成所有任务。

### 2. 多人救援成功

- **Scene 1 / a4**: Charlie + David 同时 `carry_person` → 同时 `drop_off_person`
- **Scene 2 / a4**: 32 次 carry/drop_off 操作

### 3. MCP 工具使用

所有实验中 `map_agent__*` 工具均被调用，Map Agent MCP 工作正常。

| 实验 | MapAgent Total Tokens |
|------|----------------------|
| scene 1 / a3 | 6,668 |
| scene 1 / a4 | 4,238 |
| scene 2 / a3 | 6,393 |
| scene 2 / a4 | 3,488 |

### 4. 系统稳定性

- 所有实验正常启动和关闭
- MCP lifespan 管理正常
- 无 `worker_not_found` 错误（端口清理后）

## 结论

Map Agent MCP 化 + 团队协作功能在多 agent 场景下工作正常。4 agents 配置能成功完成多人救援任务，3 agents 在 30 步限制内任务过重。

## 建议

1. **增加 max_steps**: 对于 3 agents 场景，建议 max_steps=50 或更高
2. **优化任务分配**: Coordinator 应优先分配灭火任务，再分配救援任务
3. **Token 优化**: MapAgent token 消耗较低（3-7K），但 worker token 消耗较高（200-400K）
