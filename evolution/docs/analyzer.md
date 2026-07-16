# analyzer.py — SAR 实验分析工作流

自动分析最新 SAR 实验结果，调用 Hermes 诊断卡点，输出结构化报告。

## 位置

```
/home/wyh/daily_work/LLaMAR/evolution/analyzer.py
```

## 使用

```bash
cd /home/wyh/daily_work/LLaMAR/evolution

# 自动找到最新实验并分析
uv run python analyzer.py

# 分析后与我对比上轮实验
uv run python analyzer.py --compare

# 输出到文件
uv run python analyzer.py --output ~/analysis.md

# 指定实验结果目录
uv run python analyzer.py --results-dir /path/to/sar_experiment_20260714_215625

# 调试模式（打印提取的数据摘要）
uv run python analyzer.py --verbose
```

## 工作流

```
analyzer.py
  │
  ├─ 1. 自动找到最新 sar_experiment_* 目录
  │
  ├─ 2. 读取结构化数据（Python 直接解析 CSV/JSON）
  │    ├─ metadata.json → 实验配置
  │    ├─ run_metrics.json → 顶层指标
  │    ├─ trajectory.csv → 每步 actions
  │    ├─ router_interactions.csv → coordinator 调度历史
  │    ├─ agent_interactions.csv → agent 工具调用
  │    ├─ token_usage.csv → token 消耗
  │    └─ subtasks.csv → 子任务状态
  │
  ├─ 3. 数据分析（纯 Python，不依赖 LLM）
  │    ├─ Coordinator 行为分析 → dispatch/query 模式
  │    ├─ Agent 行为分析 → 工具调用分布
  │    └─ 结构化数据摘要生成 → 紧凑的 Markdown
  │
  ├─ 4. hermes chat -q → 调用 Hermes 进行诊断
  │    ├─ 读取数据摘要
  │    ├─ 分析卡点（coordinator 空转、agent 行为、step budget）
  │    └─ 输出结构化的分析/根因/建议
  │
  └─ 5. 输出报告
       ├─ stdout / 文件
       ├─ evolution/prompts/analyses/{exp_name}.md  → 分析报告
       └─ evolution/prompts/analyses/{exp_name}.json → 结构化数据快照
```

## 持续演进

`analyzer.py` 设计为可轻松扩展：

| 扩展点 | 位置 | 说明 |
|--------|------|------|
| 新增分析维度 | `analyze_router()`、`analyze_agent_actions()` 等 | 新增函数，在 main() 中注册 |
| 修改分析 prompt | `BUILTIN_PROMPT` 常量 | 调整分析指令、格式要求 |
| 新增输出格式 | `parse_response()` + 报告构建 | 增加 section 解析和渲染 |
| 新增数据源 | `load_*()` 函数族 | 读取新的 CSV/JSON/NDJSON |

## 与 evolver.py 的关系

两者是互补关系：

```
analyzer.py              evolver.py
  │                        │
  ├─ 诊断卡点              ├─ 生成改进 prompt
  ├─ 输出分析报告          ├─ 输出改进后的 system.md
  └─ 给人类看              └─ 给下一轮实验用

orchestrator.py
  │
  ├─ 全自动循环
  ├─ 跑实验 → evolve → 跑实验 → 对比
  └─ 可集成 analyzer.py 的诊断结果作为演化输入
```
