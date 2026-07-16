# LLaMAR Agent Evolution System

基于实验轨迹和指标自动改进多智能体 SAR（Search & Rescue）系统 prompt 的演化系统。

## 目录结构

```
evolution/
├── docs/                          # ⬅ 本文档
│   ├── README.md                  # 本文件 — 总览
│   ├── architecture.md            # 系统架构与数据流
│   ├── evolver-prompt.md          # Evolver 分析 prompt 详解
│   └── skill-reference.md         # agent-evolution skill 参考
│
├── orchestrator.py                # 全自动演化循环入口 （主入口）
├── evolver.py                     # Hermes-native 演化引擎
│
├── prompts/                       # 演化产出目录
│   ├── gen_0/                     # baseline（原始 prompt，来自 sar_orch/prompts/）
│   ├── gen_1/                     # 第1轮演化
│   │   ├── coordinator/system.md  # 改进后的 Coordinator prompt
│   │   ├── generation_summary.json # 分析摘要 + 变更说明
│   │   └── raw_response.md        # Hermes 原始回复
│   ├── gen_2/
│   ├── ...
│   └── gen_10/                    # 最新一轮
```

## 两个入口

| 入口 | 用途 | 依赖 |
|------|------|------|
| `orchestrator.py` | 全自动循环：跑实验 → 分析 → 生成 prompt → 再跑实验对比 | 无（调用 evolver.py） |
| `evolver.py` | 单轮分析：读实验数据 → 调用 Hermes → 提取改进 prompt | 无（调用 `hermes chat -q`） |

## 快速开始

### 全自动演化循环

```bash
cd /home/wyh/daily_work/LLaMAR-sematic_map

env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python evolution/orchestrator.py \
    --generations 3 \
    --scene 1 --agents 2 --seed 42 \
    --target coordinator
```

### 单轮分析（跳过实验，使用已有结果）

```bash
cd /home/wyh/daily_work/LLaMAR-sematic_map

env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python evolution/orchestrator.py \
    --skip-experiment \
    --results-dir sar_orch/results/sar_experiment_20260713_* \
    --target coordinator
```

### 直接调用 evolver

```bash
cd /home/wyh/daily_work/LLaMAR-sematic_map/evolution

uv run python evolver.py \
  --results ../sar_orch/results/sar_experiment_20260713_* \
  --prompt-dir ../sar_orch/prompts \
  --gen 5 \
  --target coordinator
```

## 演化结果

每轮演化输出到 `evolution/prompts/gen_{N}/`：

| 文件 | 内容 |
|------|------|
| `coordinator/system.md` | 改进后的 Coordinator prompt |
| `worker/system.md` | 改进后的 Worker prompt（仅 target=worker/all 时） |
| `generation_summary.json` | 元数据 + 分析摘要 + 变更说明 |
| `raw_response.txt` | Hermes 原始回复全文 |

## 核心理念

- **不依赖外部 AI SDK**：evolver.py 通过 `hermes chat -q` 直接调用当前 Hermes 会话来分析，无 OpenCode、无第三方依赖
- **Anti-Cheating**：禁止将特定场景的实体名、坐标、步数 hardcode 到 prompt 中，确保改进是通用策略而非"作弊"
- **Analysis → Generation 分离**：Hermes 先读实验数据做分析，再输出改进后的 prompt 全文
