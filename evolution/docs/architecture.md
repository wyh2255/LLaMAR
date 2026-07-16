# 系统架构

## 概览

```
┌─────────────────────────────────────────────────────────────┐
│                   orchestrator.py                           │
│  全自动演化循环：跑实验 → 分析 → 生成 → 对比 → 循环         │
└──────────┬──────────────────────────────┬───────────────────┘
           │                              │
           ▼                              ▼
┌─────────────────────┐    ┌──────────────────────────────┐
│  sar_orch/           │    │       evolver.py              │
│  experiment.py       │    │  Hermes-native 演化引擎       │
│  跑 SAR 场景         │    │                              │
│  生成结果CSV/NDJSON  │    │  1. 构建分析 prompt          │
└──────────┬──────────┘    │  2. 调用 hermes chat -q       │
           │               │  3. 解析返回的改进 prompt     │
           ▼               │  4. 写入 prompts/gen_{N}/     │
┌─────────────────────┐    └──────────┬───────────────────┘
│  sar_orch/results/   │               │
│  sar_experiment_*/   │               │ hermes chat -q
│  ├─ trajectory.csv   │               ▼
│  ├─ events.ndjson    │    ┌──────────────────────┐
│  ├─ summary.tsv      │    │    Hermes Agent       │
│  ├─ metadata.json    │    │  （当前会话）          │
│  └─ ...              │    │  读 CSV/NDJSON 数据    │
└─────────────────────┘    │  分析失败模式           │
                           │  输出改进 prompt        │
                           └──────────────────────┘
```

## orchestrator.py 工作流

```
main()
  │
  ├─ 第0轮：跑 baseline 实验
  │    run_experiment(gen=0, prompt_dir=sar_orch/prompts/)
  │    → 得到 baseline metrics
  │
  └─ 第1..N 轮：演化循环
       │
       ├─ evolve_with_hermes(results, prompt_dir, gen, target)
       │    → 调用 evolver.py
       │    → 得到改进后的 prompt 文件
       │
       ├─ run_experiment(gen, new_prompt_dir)
       │    → 用新 prompt 跑实验
       │    → 得到新 metrics
       │
       └─ compare_generations(prev_metrics, curr_metrics)
            → 对比 success_rate, coverage, transport_rate 等
            → 输出 comparison + 箭头（↑↓→）
            → 写入 evolution_summary.json
```

## evolver.py 工作流

```
main()
  │
  ├─ build_prompt(results_dir, prompt_dir, target)
  │   → 构建分析 prompt（详见 evolver-prompt.md）
  │   → 告诉 Hermes 读哪些文件、分析什么、按什么格式输出
  │
  ├─ call_hermes(prompt)
  │   → subprocess.run(["hermes", "chat", "-q", prompt])
  │   → 得到原始回复文本
  │   → 写入 raw_response.txt
  │
  └─ parse_response(raw, target)
      → 正则提取三个 section：
        1. Analysis Summary        — 失败模式分析
        2. Improved Prompt for...  — 改进后的完整 prompt
        3. Changes Summary         — 变更说明
      → 写入 coordinator/system.md 和/或 worker/system.md
      → 写入 generation_summary.json
```

## 数据流

### 输入（evolver 需要读取的文件）

| 文件 | 来源 | 用途 |
|------|------|------|
| `trajectory.csv` | experiment.py 输出 | 每步的 coverage, transport_rate, action |
| `events.ndjson` | experiment.py 输出 | 完整事件日志（dispatch, tool_call, barrier 等） |
| `summary.tsv` / `summary.csv` | experiment.py 输出 | 聚合指标 |
| `metadata.json` | experiment.py 输出 | scene, agents, seed, model, prompt_version |
| `agent_interactions.csv` | experiment.py 输出 | 每个 agent 的 tool call 参数和 LLM 输出 |
| `router_interactions.csv` | experiment.py 输出 | coordinator 的 dispatch 历史 |
| `token_usage.csv` | experiment.py 输出 | prompt/completion/cache tokens |
| `coordinator/system.md` | sar_orch/prompts/ | 当前的 Coordinator prompt |
| `worker/system.md` | sar_orch/prompts/ | 当前的 Worker prompt |

### 输出

| 文件 | 去向 | 用途 |
|------|------|------|
| `prompts/gen_{N}/coordinator/system.md` | evolution/prompts/ | 下一轮实验用 |
| `prompts/gen_{N}/worker/system.md` | evolution/prompts/ | 下一轮实验用 |
| `prompts/gen_{N}/generation_summary.json` | evolution/prompts/ | 分析记录 |
| `prompts/gen_{N}/raw_response.txt` | evolution/prompts/ | 调试用 |

## 关键设计决策

1. **Hermes-native**：evolver.py 用 `hermes chat -q` 而非 OpenCode SDK，无需启动外部 server，无第三方依赖
2. **Self-contained prompt**：分析 prompt 中列出所有需读取的文件路径，Hermes 自己用工具读取，evolver.py 不主动读取任何数据
3. **正则解析输出**：evolver.py 通过正则提取 Hermes 回复中的三个 section，不依赖 JSON 结构
4. **Anti-Cheating**：在分析 prompt 中内嵌 anti-cheating 规则，防止模型把实验特例 hardcode 到 prompt 里
5. **No OpenCode 分支已移除**：旧版 evolver.mjs（Node.js + OpenCode SDK）已被 evolver.py（纯 Python）替代
