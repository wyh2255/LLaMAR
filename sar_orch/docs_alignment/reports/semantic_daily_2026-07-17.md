# 语义对齐日报 — 2026-07-17

## 执行概要

### 步骤 1 (align_docs.py --auto-fix)
- Auto-fix 移除 `logging_map.md` 中 4 个过期 benchmark 文件引用（results/benchmark/.../meta.json 等）
- 更新 logging_map.md 日期为 2026-07-17

### 步骤 2 (自动扫描结果)

| 文档 | 验证率 | 操作 |
|------|--------|------|
| ✅ 框架.md | 100% (73/73) | 日期 2026-07-04 → 2026-07-17 |
| ✅ data_flow.md | 100% (25/25) | 日期已为 2026-07-17，无需修改 |
| ✅ experiment_design.md | 100% (14/14) | 日期 2026-07-05 → 2026-07-17 |
| ✅ contextmanager.md | 100% (16/16) | 日期 2026-07-04 → 2026-07-17 |
| ⚠️ logging_map.md | 76% (29/38) | Auto-fix 移除 4 个过期引用，日期已更新 |
| ✅ sandbox.md | 86% (19/22) | 日期 2026-07-04 → 2026-07-17 |

### 假阳性分析

12 项标记为"缺失"的全部为**假阳性**：

1. **logging_map.md 中 9 个运行时生成的输出文件：**
   - trajectory.csv, agent_interactions.csv, router_interactions.csv, token_usage.csv,
     summary.csv, events.ndjson, subtasks.csv, metadata.json, semantic_map.jsonl
   - 这些文件在运行时由 ExperimentLogger 在 `<log_dir>/` 下创建，不在源码目录中
   - 文档中的引用正确（写作 `<log_dir>/xxx.csv`），align_docs.py 无法通过文件系统验证

2. **sandbox.md 中 3 个工具实例名：**
   - read_file, write_file, edit_file
   - 这些是 ReadTool/WriteTool/EditTool 类的实例名称，不是独立函数
   - 文档描述准确，无需修改

### 总计
- **总检查项**: 188
- **已验证**: 176 (93.6%)
- **假阳性**: 12 (6.4%)
- **真实差异**: 0

### 文档修正记录

| 文档 | 修改内容 |
|------|---------|
| logging_map.md | 移除 results/benchmark/.../meta.json 等 4 个过期文件引用的反引号 |
| logging_map.md | 更新日期为 2026-07-17 |
| 框架.md | 更新日期为 2026-07-17 |
| experiment_design.md | 更新日期为 2026-07-17 |
| contextmanager.md | 更新日期为 2026-07-17 |
| sandbox.md | 更新日期为 2026-07-17 |

### 注意
- Subagent（x6）已启动做深度语义对齐，但尚未完成返回结果
- 如果 subagent 后续返回了额外修改，将在下次日报中补充
- Webhook 推送失败（系统限制），报告已存档至此文件

## 配置
- 配置版本: 3
- 上次进化: 2026-07-16T04:00:00
- 运行环境: WSL (cron job)
- 项目根: /home/wyh/daily_work/LLaMAR-sematic_map
