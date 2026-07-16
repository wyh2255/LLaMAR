---
日期: 2026-06-27
文档类型: 实验方案
文档概述: A2A-SAR 框架全场景基准测试方案，对标原 LLaMAR 论文实验设计
---

# A2A-SAR 基准测试方案

## 1. 目标

用我们的 A2A 编排框架，在 SAR 全部 5 个场景上跑完原 LLaMAR 论文的实验配置，输出可对比的指标。

## 2. 实验矩阵

| 维度 | 值 | 数量 |
|------|----|------|
| 场景 | Scene 1, 2, 3, 4, 5 | 5 |
| Agent 数 | 2, 3, 4, 5 | 4 |
| 随机种子 | 0, 10, 20, 30, 40 | 5 |
| **总计** | | **100 轮** |

与原文完全一致。

## 3. 每轮配置

| 参数 | 值 | 说明 |
|------|----|------|
| Model | `deepseek-v4-flash` | 与之前实验一致 |
| Provider | `openai` (DeepSeek API) | |
| 并发 | **2 轮同时** | 避免 API 限速 + 机器性能限制 |
| STEP_TIMEOUT | 15s | 单步 agent 等待上限 |
| max_steps | Scene 1=1200, Scene 2-5=35 | 与原文 `task_timeout` 语义对齐 |
| orchestration_timeout | 1200 | Coordinator LLM 超时，与之前一致 |
| 失败重试 | 1 次 | 如果某轮因 API 错误失败，自动重试 |

## 4. 代码实现（已完成 ✅）

### 4.1 `sar_orch/experiment.py` — 已修改

改动点：
- poll loop 从按秒超时改为按步数超时：`while not barrier.is_finished() and barrier.get_metrics()["steps"] < max_steps`
- 新增 `max_steps` 参数（可选，默认取场景 `task_timeout`）
- `run_experiment()` 函数签名新增 `max_steps: int | None = None`
- `final_metrics` 在 `try` 前初始化默认 dict，避免 `finally` 块中 `NameError`
- CLI 新增 `--max-steps` 参数

### 4.2 `sar_orch/benchmark.py` — 已新建（含后续增强）

批量运行器，职责：
```
遍历所有组合 (scene, agents, seed):
    创建结果目录: sar_orch/results/benchmark/{scene}/{agents}/{seed}/
    调 experiment.run_experiment()
    写 result.json + 复制 summary.csv
    捕获异常，失败重试 N 次
    Semaphore(N) 控制并发数
完成后输出 index.json（全量索引）
```

参数：
```bash
uv run python sar_orch/benchmark.py --concurrency 2 --retry 1 --resume
```

（矩阵硬编码在脚本内：5 场景 × 4 agent 数 × 5 种子）

关键实现细节：
- 每轮写 `meta.json` + `result.json` + `error.log`（失败时）
- `result.json` 包含 `finished/steps/coverage/transport_rate/elapsed/status`
- `--resume` 跳过已成功的轮次，重试已失败的轮次
- **增强（2026-06-30）**：
  - `progress.json`：每轮状态变化时原子写入，外部可实时读取进度
  - stderr 进度条：`[████░░] 45/100 (45%)  ✓30 ✗8 ⏱3 ⊘4  ▶3  120s elapsed · ETA 180s`
  - `SIGUSR1` 信号支持：`kill -USR1 <pid>` 打印当前快照
  - 重试计数修正：重试时回退 failed 计数，避免重复统计
  - 子进程 env：添加 `LD_LIBRARY_PATH`（conda lib）和 `PYTHONPATH`（项目根），子进程通过 `uv run` 启动时自动使用 `.venv/` 的依赖

### 4.3 `sar_orch/aggregate.py` — 已新建

结果聚合器，职责：
```
遍历 results/benchmark/ 下 scene_{N}/agents_{N}/seed_{N}/result.json:
    提取 scene, agents, seed, steps, coverage, transport_rate, finished
    balance = 1.0（finished）或 transport_rate（未完成）
输出: benchmark_aggregated.tsv（与论文格式对齐）
```

输出 TSV 列：

```
scene    agents    seed    steps    balance    coverage    success_rate    transport_rate
1        2         0       18       1.0        1.0         1.0             1.0
```

## 5. 分阶段计划

### Phase 1: 代码修改 ✅ 已完成

| 文件 | 改动 | 状态 |
|------|------|------|
| `sar_orch/experiment.py` | poll loop 步数超时 + `max_steps` 可选参数 + `final_metrics` 兜底 | ✅ |
| `sar_orch/benchmark.py` | 批量运行器（并发/resume/重试/index.json） | ✅ |
| `sar_orch/aggregate.py` | TSV 聚合器 | ✅ |

### Phase 2: 单场景验证（5 轮，~30 分钟）

各场景用 2 agent、seed 42 跑一次，确认：
- A2A 编排能完成或至少正常推进
- 步数上限正确截断
- token 记录完整

### Phase 3: 批量执行（100 轮，2 并发，预估 3-5 小时）

后台挂起 `nohup uv run python sar_orch/benchmark.py ... &`

### Phase 4: 聚合分析（~10 分钟）

跑 `aggregate.py` 生成 TSV。

## 6. 预期输出

```
sar_orch/results/
├── benchmark/
│   ├── scene_1/
│   │   ├── agents_2/
│   │   │   ├── seed_0/
│   │   │   │   ├── meta.json        # 运行元数据
│   │   │   │   ├── result.json      # 结果指标
│   │   │   │   ├── summary.csv      # 从实验中复制
│   │   │   │   └── error.log        # （失败时）
│   │   │   ├── seed_10/
│   │   │   └── ...
│   │   ├── agents_3/
│   │   └── ...
│   ├── scene_2/
│   └── ...
│   └── index.json                  # 全量运行索引
├── benchmark_aggregated.tsv        # 论文格式结果
```

## 7. 与原文差异说明

| 维度 | 原 LLaMAR | 我们的 A2A | 影响 |
|------|-----------|-----------|------|
| LLM | `gpt-4-turbo` | `deepseek-v4-flash` | 能力/成本不同，不可直接比绝对值 |
| 每步 LLM 调用 | 3 次（串联） | N+1 次（部分并联） | 每步耗时不同，但步数语义一致 |
| 架构 | 集中式（一个 LLM 控制所有 agent） | 分布式（Coordinator 派活，Worker 独立执行） | 方法论不同，可对比效果 |
| 步数上限 | 35-1200 | 35-1200（对齐后） | 对齐，可对比 |
| 结果格式 | TSV: steps/balance/coverage/success/transport | 同上 | 对齐，可对比 |

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| API 限速/超时 | 2 并发 + 自动重试 1 次 |
| 机器内存不足 | 每轮结束后 asyncio 会自动 GC；2 并发内存压力很小 |
| 某场景完全不可完成 | 记录 finished=False，仍输出部分指标 |
| 实验结果与原文差异大 | 架构/模型不同，差异在意料之中。关注趋势而非绝对值 |
