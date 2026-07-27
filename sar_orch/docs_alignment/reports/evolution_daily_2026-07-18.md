# 语义进化日报 — 2026-07-18

## 执行概要

基于 2026-07-17 3AM 对齐结果分析。

## 偏差最大的文档

| 文档 | 验证率 | 假阳性 | 真实差异 |
|------|--------|--------|---------|
| logging_map.md | 76% (29/38) | 9 | 0 |
| sandbox.md | 86% (19/22) | 3 | 0 |
| 框架.md | 100% (73/73) | 0 | 0 |
| data_flow.md | 100% (25/25) | 0 | 0 |
| experiment_design.md | 100% (14/14) | 0 | 0 |
| contextmanager.md | 100% (16/16) | 0 | 0 |
| **总计** | **93.6% (176/188)** | **12** | **0** |

## 系统性发现

### a) 文档-代码偏差分析

- **logging_map.md** 验证率最低（76%），但 9/9 缺失项均为运行时生成的输出文件（trajectory.csv 等），是已知假阳性
- **sandbox.md** 3 个缺失项为 read_file/write_file/edit_file 工具实例名（非独立函数），也是已知假阳性
- 其他 4 篇文档 100% 对齐

### b) 跨文档系统性误差

1. **运行时生成文件引用（跨文档）**：logging_map.md 的 9 个输出文件引用（带 `<log_dir>/` 前缀）与 experiment_design.md 的类似引用模式相同。experiment_design.md 已经配置了 `generated_files`，但 logging_map.md 没有 → **本次已修复**

2. **DefaultRequestHandler 引用的歧义**：v2 从 data_flow.md 移除了 DefaultRequestHandler.on_message_send 的 key_claims（认为代码中已不存在），但文档仍在 line 20/105 引用该类。经分析，DefaultRequestHandler 是外部 a2a-sdk 包的类，被 src/a2a/coordinator/a2a_server.py 和 src/a2a/worker/a2a_server.py 导入和使用。文档中的引用正确，无需修改。

### c) 文档-源码映射完整性

- **文件路径歧义问题**：`align_docs.py` 的自动提取器在匹配 src/Agent/worker_agent/build.py 时错误匹配到 src/Agent/router_agent/build.py（两个文件同名），src/a2a/worker/cli.py 同理匹配到 src/a2a/coordinator/cli.py。这是 `align_docs.py` 的路径匹配算法 bug，非配置文件问题。
- 其他文档-源码映射完整（全部 176 项已验证）

### d) 对齐流程问题

- **Subagent 超时**：2026-07-17 报告提到 6 个 subagent 已启动但未返回结果。这是 background delegation 的同步问题 — subagent 返回时已超出报告生成窗口
- **假阳性处理不完整**：logging_map.md 的运行时文件一直被误标为"缺失"，因为 `generated_files` 配置未覆盖这些文件 → **本次已修复**

## config.json 更新（v3 → v4）

### 新增
| 文档 | 修改内容 |
|------|---------|
| ✅ logging_map.md | 添加 10 个 `generated_files`：trajectory.csv, agent_interactions.csv, router_interactions.csv, token_usage.csv, summary.csv, events.ndjson, subtasks.csv, metadata.json, semantic_map.jsonl, snapshot_*.json |
| ✅ sandbox.md | 添加 3 个类名 key_claims：ReadTool, WriteTool, EditTool |

### 更新
| 条目 | 修改内容 |
|------|---------|
| ✅ sandbox.md read_file | 描述更新为 "Read file tool (ReadTool class instance)" |
| ✅ sandbox.md write_file | 描述更新为 "Write file tool (WriteTool class instance)" |
| ✅ sandbox.md edit_file | 描述更新为 "Edit file tool (EditTool class instance)" |

### 版本
- v3 → **v4**

## config.json 未修改内容（维持不变）

| 项 | 结论 |
|----|------|
| DefaultRequestHandler 的 key_claims | 不添加 — 是外部 SDK 类，不在项目源码中，v2 移除正确 |
| 文件路径匹配 bug (build.py/cli.py) | 是 align_docs.py 的代码 bug，非 config 问题 |
| 其他 key_claims | 全部正确，无需增删 |

## 配置

- 当前版本: **v4**
- 上次进化: 2026-07-18T04:00:00
- 运行环境: WSL (cron job)
- 项目根: /home/wyh/daily_work/LLaMAR
