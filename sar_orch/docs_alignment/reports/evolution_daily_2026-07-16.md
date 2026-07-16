# 语义进化日报 — 2026-07-16

## 执行概要

基于 2026-07-15 3AM 语义对齐报告进行分析，发现 4 类系统性偏差并更新 config.json 至 v3。

## 偏差最大的文档

| 文档 | 验证率 | 问题 |
|------|--------|------|
| logging_map.md | 76% (29/38) | 9 个输出文件不在源码索引中 — 假阳性（生成文件） |
| sandbox.md | 86% (18/21) | 3 个内置工具名不在函数索引中 — 假阳性（内置工具） |
| data_flow.md | 100% (25/25) | 验证率掩盖了文档仍包含已删除引用的问题 |
| 框架.md | 99% (82/83) | DestroyedTaskError 已在 SKIP_CLASSES — 可移除 |

## 系统性发现

### a) 文档-源码偏差最大

- **data_flow.md** 在 v2 移除了 5 个过期 key_claims，但文档仍包含 `DefaultRequestHandler.on_message_send()`（line 20, 102）
- **框架.md** 引用了 `SupervisionStateStore`、`TaskWatchdog`、`WorkerReportPublisher` 但 config 无对应 key_claims
- **contextmanager.md** 引用了 `CoordinatorPinnedState`、`CoordinatorContextManager` 但 config 无对应 key_claims

### b) 跨文档系统性误差

1. **配置过期 claim 移除 → 文档未更新 → 无声腐烂**
   - v2 从 data_flow.md 移除了 5 个 claims（DefaultRequestHandler, SubmitActionTool 等），但文档实际内容未改变
   - 未来即使文档完全腐烂，验证率仍显示 100%

2. **生成文件 vs 源码混淆**
   - logging_map.md 中 9/13 "缺失" 项是实际的输出文件（trajectory.csv 等），不是源码实体
   - 验证系统无法区分生成文件与源码实体

3. **内置工具 vs 项目函数混淆**
   - sandbox.md 中 read_file/write_file/edit_file 是 Hermes Agent 框架内置工具
   - 验证系统在项目源码索引中找不到它们

### c) 文档-源码映射完整性

**框架.md 缺少的 key_claims：**
- `SupervisionStateStore` → `src/a2a/coordinator/supervision_state_store.py`
- `TaskWatchdog` → `src/a2a/coordinator/task_watchdog.py`
- `WorkerReportPublisher` → `sar_orch/observation_publisher.py`

**contextmanager.md 缺少的 key_claims：**
- `CoordinatorContextManager` → `src/Agent/router_agent/context.py`
- `CoordinatorPinnedState` → `src/Agent/router_agent/context.py`

**experiment_design.md 缺少的 key_claims：**
- `classify_end_reason` → `sar_orch/experiment.py`

### d) 对齐流程本身的问题

- **Subagent 结果未整合**：3AM 报告启动了 6 个 subagent 但未等待其返回结果
- **移除过期 claims 不当**：应修改文档而非仅从验证列表移除
- **假阳性未过滤**：生成文件 / 内置工具反复标记为缺失

## config.json 更新内容

| 操作 | 项 | 说明 |
|------|-----|------|
| ✅ 移除 | DestroyedTaskError | 已在 SKIP_CLASSES，源码中已删除 |
| ✅ 添加 | SupervisionStateStore | 框架.md — 可操作事件持久化存储 |
| ✅ 添加 | TaskWatchdog | 框架.md — 任务监督检测器 |
| ✅ 添加 | WorkerReportPublisher | 框架.md — 观测去重推送 |
| ✅ 添加 | CoordinatorContextManager | contextmanager.md — 协调器端实现 |
| ✅ 添加 | CoordinatorPinnedState | contextmanager.md — 协调器类型化 pinned state |
| ✅ 添加 | classify_end_reason | experiment_design.md — 结束原因分类器 |
| ✅ 扩充 | skip_patterns | 覆盖 snapshot_*.json, logs/agent/ 等生成文件 |
| ✅ 扩充 | tool_references | 覆盖 bash/bash_output/bash_kill/get_skill |

**版本:** v2 → v3

## 待办项

- [ ] data_flow.md 手动更新：移除 line 20, 102 的 DefaultRequestHandler 引用
- [ ] 考虑修改验证流程：生成文件 / 内置工具不应计入缺失
- [ ] 考虑修改移除策略：移除过期 claim 前应先更新文档
- [ ] 3AM subagent 结果应整合到最终输出

## 配置

- 配置版本: 3
- 上次进化: 2026-07-16T04:00:00
- 运行环境: WSL
- 项目根: /home/wyh/daily_work/LLaMAR-sematic_map
