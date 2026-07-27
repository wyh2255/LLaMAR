# 🔄 语义进化日报 — 2026-07-19

## 执行概要

基于今日 3AM 对齐结果（run_id: 20260719_030819）分析。

### 对齐概况
- **总检查项**: 194
- **已验证**: 188 (96.9%)
- **未找到**: 6 (全部为假阳性)
- **真实差异**: 0
- **配置版本**: v4 → v5

### 偏差最大的文档

| 文档 | 验证率 | 未找到 | 说明 |
|------|--------|--------|------|
| 框架.md | 96% (80/83) | 3 | AskCoordinator/ReadMailbox/SendMail 为工具实例名，非类名 |
| sandbox.md | 88% (22/25) | 3 | read_file/write_file/edit_file 为 ReadTool/WriteTool/EditTool 实例 |
| 其余 4 篇 | 100% | 0 | data_flow.md, logging_map.md, experiment_design.md, contextmanager.md |

### 系统性发现

**① 工具实例名 vs 类名误报（跨文档）**
- 框架.md 的 Worker 工具表格中正确列出了 AskCoordinator、ReadMailbox、SendMail 作为工具实例名（line 307-309）
- 同一文档也正确列出了 AskCoordinatorTool、ReadMailboxTool、A2ASendMailTool 作为类名（line 110-112）
- align_docs.py 将工具实例名（AskCoordinator）自动提取为类引用，导致「未找到」误报
- 与 sandbox.md 的 read_file/write_file/edit_file 问题同类

**② align_docs.py 文件路径解析缺陷**
- `Agent/worker_agent/agent.py` → 被映射到 `src/Agent/router_agent/agent.py`（应指向 worker_agent）
- `worker_agent/context.py` → 被映射到 `src/Agent/router_agent/context.py`
- 原因是两个并行副本目录（worker_agent/ 和 router_agent/）有相同文件名，搜索算法优先命中 router_agent

**③ 3AM 报告未生成 semantic_daily 文件**
- 当天的对齐运行产生了 latest.md 和 auto_fix_result.json，但未生成 semantic_daily_2026-07-19.md
- 使用了 latest.md 作为降级分析

### config.json 更新
- ✅ 添加 3 个 tool_references（AskCoordinator、ReadMailbox、SendMail）
- ✅ 版本从 v4 升至 v5
- 📝 无过时 key_claims 需移除 — 全部已验证

### 建议人工 Review
1. **align_docs.py 文件搜索算法**: 在 worker_agent/router_agent 并行目录场景下，相对路径应优先匹配原始文档中使用的目录（worker_agent → src/Agent/worker_agent）
2. **align_docs.py 工具实例处理**: 建议在自动提取类引用时，先检查 tool_references 列表；或以某种方式标记工具实例名，避免将其作为 class 类型验证
3. **semantic_daily 报告生成**: 确认 3AM 对齐脚本在生成 latest.md 的同时也应生成 semantic_daily_YYYY-MM-DD.md

### 运行信息
- 运行时间: 2026-07-19T04:00:00
- 运行环境: WSL (cron job)
- 项目根: /home/wyh/daily_work/LLaMAR
