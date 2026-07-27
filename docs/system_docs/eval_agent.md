---
日期: 2026-07-23
文档类型: 系统架构文档
文档概述: SAR 测评 Agent（eval_agent）设计详解 — 离线评测已完成实验的确定性 grader + LLM judge 组合架构，面向新手的设计导读
---

# SAR 测评 Agent（eval_agent）设计

## 1. 这是什么

`sar_orch/eval/` 是一个**离线评测工具**：输入一个已跑完的 SAR 实验结果目录（CSV/NDJSON/JSON 产物），输出一份结构化评测报告（`eval_report.json` + `eval_report.md`）。

它回答三类问题：

| 层级 | 问题 | 例子 |
|------|------|------|
| Action 级 | 单次动作是否合理？ | 没水为什么还灭火？导航目标是不是幻觉？动作失败归因是什么？ |
| Step 级 | Coordinator 派遣质量如何？ | 是否全员都有任务？分工和位置/库存匹配吗？ |
| Episode 级 | 整局结果如何？ | 覆盖率、运输率、token 效率、负载均衡 |

一句话理解：**一半是传统数据分析脚本（确定性 grader），一半是 LLM 评审团（judge 子代理），用 DeepAgent 把它们编排起来。**

## 2. 核心设计原则（最重要的不变量）

> **判定权永远留在确定性代码里，LLM 只负责编排、深挖和主观评审。**

| 角色 | 承担者 | 能做什么 | 不能做什么 |
|------|--------|----------|------------|
| 判定 | 5 个确定性 grader（纯函数） | 产出 pass/fail、指标、违规列表 | — |
| 编排 | DeepAgent 主代理 | 决定深挖哪些异常 step、派发 judge | **不得修改 grader 判定结果** |
| 主观评审 | 2 个 judge 子代理 | 评派遣质量、检幻觉 | 输出必须通过 schema 校验才能落盘 |
| 报告 | report.py | 机械合并所有结果 | 不发明数据 |

为什么这样设计？防止 LLM "商量"出好看的评测结果——就算 agent 完全失控，磁盘上的 grader 结果依然是权威真相。

## 3. 架构总览

```
python -m sar_orch.eval.cli --results-dir <实验目录>
│
├─ 阶段 1：确定性分析（无 LLM，<1 秒）
│    load_episode()                    # dataset.py：8+ 个文件 → EpisodeDataset
│    5 个 grader 依次运行               # outcome/state/constraint/error_taxonomy/trajectory
│    结果落盘 eval_workspace/grader_results/*.json   ← 权威判定
│
├─ 阶段 2：DeepAgent 分析（--no-llm-judge 时跳过，约 5 分钟）
│    主代理（6 个 Phase 的工作流）：
│      1. 确认 grader 结果  2. 定位失败点  3. 深挖异常 step
│      4. 派发 dispatch_judge ──→ 评 Coordinator 派遣质量（4 维 rubric）
│      5. 派发 observation_judge → 检 worker 观测幻觉（逐条 claim）
│      6. 撰写 conclusion.md
│    judge 输出经 schema 硬校验后落盘 eval_workspace/judge_results/
│
└─ 阶段 3：合并输出（无 LLM）
     merge_results()      # GradeResult（权威）+ judge 结果 + 结论文本
     write_both_reports() # eval_report.json（机器读）+ eval_report.md（人读）
```

## 4. 目录结构

```
sar_orch/eval/
├── cli.py                  # 入口：参数解析 + 三阶段编排
├── dataset.py              # EpisodeDataset：实验目录 → 标准化视图
├── aggregate.py            # 跨 run 聚合：pass@k/pass^k（§8）
├── gate.py                 # 回归门禁：aggregate → CI pass/fail（§9）
├── graders/                # 确定性 grader（纯函数，不依赖 LLM）
│   ├── __init__.py         #   ALL_GRADERS 注册表 + run_all_graders()（单一来源）
│   ├── base.py             #   GradeResult 数据类 + Grader 协议
│   ├── outcome.py          #   episode 级：终局指标/token/均衡度
│   ├── state.py            #   episode 级：状态一致性交叉验证
│   ├── constraint.py       #   action 级：6 条负例规则
│   ├── error_taxonomy.py   #   action 级：失败归因（优先级链）
│   └── trajectory.py       #   action/step 级：流程序列约束
├── agent/                  # DeepAgent 装配层
│   ├── eval_agent.py       #   create_deep_agent 装配 + judge 结果收集
│   ├── tools.py            #   把 dataset/graders 包装成 agent 工具
│   ├── subagents.py        #   dispatch_judge / observation_judge 定义
│   └── prompts/            #   system.md + 两个 judge 的 rubric
└── report.py               # 合并 → eval_report.json/md
```

## 5. 模块详解

### 5.1 dataset.py — 数据加载层

把实验目录里的原始文件标准化为 `EpisodeDataset`，下游 grader 只认这个视图。

**必须知道的 4 个数据陷阱**（不处理就会算错）：

| 陷阱 | 处理 |
|------|------|
| Step 编号偏移：`trajectory.csv` 从 1 起，`router/subtasks.csv` 从 0 起 | 加载时统一 `raw_step + 1`，下游只见 1-based |
| 动作名不一致：agent 日志是工具级名 `CarryPerson(...)`，trajectory 是环境级名 `Carry(...)` | `parse_action()` 别名归一 |
| 观测文本约 1/3 行被 CSV 截断 | 解析器容错，截断行返回 None 而非抛异常 |
| 可选文件缺失（oracle 模式无 semantic_map.jsonl） | 降级 + `grader_skips` 显式记录，不静默 |

**数据模型**（三层）：

```
EpisodeDataset
├── steps: dict[step_num → StepRecord]     # trajectory 每行 + 对齐挂载
│     ├── actions/successes/timeout_agents  # env 真实结果（唯一可信的成败来源）
│     ├── interactions: [AgentInteraction]  # agent_interactions.csv 按 step 对齐
│     └── dispatches: [Dispatch]            # router_interactions.csv 按 step 对齐
├── summary / token_usage_rows              # summary.csv / token_usage.csv
└── map_summaries / semantic_map_log        # judge 的地图输入
```

**成败判定的铁律**：动作是否成功只看 `trajectory.csv` 的 `Successes` 列（来自 `env.step()` 的真实结果）。`ToolResult.success` 恒为 True，不可信。

### 5.2 graders/ — 确定性判定层

统一接口（`base.py`，仅 23 行）：

```python
@dataclass
class GradeResult:
    grader: str            # grader 名
    level: str             # action | step | episode
    passed: bool | None    # None = 纯记录型，不做二元判定
    score: float | None    # 0~1 部分分数
    detail: dict           # 证据明细
    evidence_ref: str      # 溯源，如 "agent_interactions.csv:L66"
```

5 个 grader 一览：

| Grader | 层级 | 干什么 | 输出特点 |
|--------|------|--------|----------|
| **OutcomeGrader** | episode | 终局指标、token 效率（含地图开销占比）、Balance（min/max 成功动作数）、进度曲线 | 纯记录，`passed=None` |
| **StateGrader** | episode | 交叉验证：trajectory 重建子任务完成数 vs Finished；run_metrics.json vs summary.csv；指标回退检测 | 验证型 |
| **ConstraintGrader** | action | 6 条负例规则（见下表） | 违规列表，按严重度分级 |
| **ErrorTaxonomy** | action | 每个失败动作归因（优先级链） | 失败分布直方 |
| **TrajectoryGrader** | action/step | 流程序列约束（救人/灭火/协作搬运） | 4 项检查 pass/fail |

**ConstraintGrader 的 6 条规则**：

| 规则 | 判定 | 严重度 |
|------|------|--------|
| `empty_supply_use` | 库存为 0 还 `UseSupply` | high |
| `hallucinated_nav_target` | `NavigateTo` 目标不在可见 Names 列表 | high |
| `restricted_action_violation` | 搬人状态执行非移动/搬运类动作 | medium |
| `repeat_failure_loop` | 同一 agent 连续 ≥3 步相同失败动作 | medium |
| `full_inventory_get` | 库存满 3 格还 `GetSupply` | low |
| `excessive_noop` | 主动 NoOp 占比 >30%（剔除超时填充） | low |

**ErrorTaxonomy 的归因优先级链**（判定顺序即优先级，命中即停）：

```
infrastructure（超时自动填充，非 LLM 决策）
  → not_visible（目标不在观测中 —— 幻觉）
  → restricted_action（搬人状态违规）
  → obstacle_blocked（Move/Explore 撞障碍/边界）
  → not_interactable（可见但超交互距离，启发式）
  → unknown（兜底，重点人工抽查对象）
```

两个防漏设计：
- `TimeoutAgents`（barrier 超时自动填的 NoOp）在所有分析中先剔除——否则会把系统行为误算成 LLM 的消极倾向
- 无法归因的失败计入 `unmapped_failures` 显式列出，**绝不静默丢弃**

### 5.3 agent/ — DeepAgent 装配层

#### 为什么需要 LLM agent？

确定性 grader 只能回答"发生了什么"，回答不了"为什么"和"这样决策好不好"。例如：
- Coordinator 连续 10 步发同样的指令是不是故障？（需要理解上下文）
- Worker 上报的观测是不是编造的？（需要逐条语义比对）

#### 三个组件

**tools.py** — 把数据包装成 agent 工具：

| 工具 | 作用 |
|------|------|
| `run_grader(name)` / `run_all_graders()` | 触发确定性 grader，结果落盘 |
| `get_step_evidence(step)` | 某 step 的完整证据（轨迹+交互+成败） |
| `get_agent_trace(agent)` | 某 agent 全剧动作序列 |
| `get_dispatch_context(step)` | dispatch_judge 的输入（派遣+团队状态+地图摘要） |
| `get_observation_claims(agent, step)` | observation_judge 的输入（上报 vs 环境真实观测） |
| `save_judge_verdict(name, json)` | judge 结果落盘 —— **带 schema 硬校验** |

**subagents.py** — 两个 judge 子代理（context quarantine：各自评审几十条记录，主代理只看结论）：

- **dispatch_judge**：评 Coordinator 派遣质量，4 维 rubric（全员覆盖/角色匹配/地图感知/步数预算），每维 pass/fail/Unknown
- **observation_judge**：检 worker 观测幻觉，逐条 claim 判定 `supported: true/false` + 证据

**eval_agent.py** — 装配入口：`create_deep_agent(model=ChatOpenAI实例, tools=[...], subagents=[...], system_prompt=...)`，以及 `collect_judge_results()` 把落盘的 judge 结果收集进报告。

#### 主代理工作流（prompts/system.md 规定的 6 个 Phase）

```
1. 调 run_all_graders() 确认判定结果
2. 读 grader 结果，定位失败 step 和违规
3. 对异常 step 调 get_step_evidence 深挖根因
4. task 工具派发 dispatch_judge（等距抽样 ≤20 步 + 失败 step 必审）
5. task 工具派发 observation_judge（抽样 agent×step，step 1 必查）
6. 综合所有发现写 conclusion.md
```

### 5.4 report.py — 报告合并层

`merge_results()` 把三部分合成一个 dict（结构见 `eval_report.json`）：
- 确定性 grader 结果（权威判定，原样嵌入）
- `llm_judge` 字段（dispatch pass_rate、observation hallucination_rate、judge_model、`same_model_warning`）
- agent 撰写的 conclusion 全文

`write_report_md()` 生成人读版，"结论先行"：指标总表 → 失败归因分布 → 严重违规 Top-N（带 evidence_ref 可回查原始 CSV）→ judge 结果 → agent 结论 → **建议改进点**（规则化模板：哪类失败占比最高自动给对应修复建议）。

### 5.5 cli.py — 入口

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.eval.cli \
  --results-dir sar_orch/results/<实验目录> \
  [--agent-model X]        # 主代理模型（默认 .env model）
  [--judge-model X]        # judge 模型（默认同主代理，可不同）
  [--judge-sample-steps N] # judge 抽样上限（默认 20）
  [--no-llm-judge]         # 跳过 LLM，秒出纯确定性报告
  [--output path]          # 输出路径（.json/.md 自动推导）
```

## 6. 关键设计防线（实战中踩过的坑）

这些防线都是在真实审计中暴露问题后加固的，理解它们就理解了这个系统的一半：

### 6.1 LLM 输出不可信 → 工具侧硬校验

**问题**：judge 子代理每次输出的 JSON 结构都可能不同（list/dict/各种键名），靠 prompt 约定 + 解析层兼容追不上格式漂移，曾导致整份报告的 dispatch 表格解析失败。

**防线**：`save_judge_verdict` 工具内做 schema 校验——输出不合法直接拒绝保存并返回错误信息，迫使 LLM 重试成合格式。合并层只读 canonical 文件（`dispatch_full.json`/`observation_full.json`）。

> 经验：**LLM 的结构化输出必须在工具侧硬校验，不能只靠 prompt 约定。**

### 6.2 judge 判定漂移 → rubric 客观锚点

**问题**：同一个"0 派遣 step"，不同运行中 judge 一次判 pass（"工作还在推进"）一次判 fail，pass_rate 在 0.5~0.95 间大幅波动。

**防线**：`prompts/dispatch_judge.md` 增加客观锚点——"0 派遣且任务未完 → full_coverage/map_awareness 必 fail；notes 必须写明派遣数"。客观规则优先于整体判断。

> 经验：**judge rubric 的可判定项尽量客观化，主观判断只留给真正主观的维度。**

### 6.3 数据静默丢失 → 显式计数

**问题**：动作名不一致（CarryPerson vs Carry）曾导致失败动作被静默跳过，6 个失败只归类了 5 个。

**防线**：所有"无法处理"的情况计入 `unmapped_failures`/`parse_miss_counts` 显式列出，禁止 `continue` 静默丢弃。

### 6.4 judge 与被测模型同源 → 偏差警告

judge 模型 == 实验中被测 agent 模型时，报告标注 `same_model_warning: true`——"用自己评自己"存在系统性偏差风险。可通过 `--judge-model` 指定不同模型隔离。

### 6.5 agent 失控 → 确定性结果先行落盘

5 个 grader 由 CLI **直接**运行（不经 agent），结果先落盘。即使后续 DeepAgent 死循环或崩溃，`grader_results/` 依然完整，报告标 `missing` 而非静默缺失。

## 7. 输出文件

| 文件 | 内容 |
|------|------|
| `<run_dir>/eval_report.json` | 机器可读主产物：指标、失败归因、违规、judge 结果 |
| `<run_dir>/eval_report.md` | 人读摘要：结论先行 + 改进建议 |
| `<run_dir>/eval_workspace/grader_results/*.json` | 5 个 grader 的权威判定（LLM 不可修改） |
| `<run_dir>/eval_workspace/judge_results/*.json` | judge 子代理的结构化 verdict |
| `<run_dir>/eval_workspace/conclusion.md` | 主代理撰写的分析结论 |

## 8. 多 episode 聚合（二期已实现）

`sar_orch/eval/aggregate.py` — 跨 run 聚合器，消费各实验目录的 `eval_report.json`（不重新评测）：

```bash
uv run python -m sar_orch.eval.aggregate [--results-root sar_orch/results] [--output <path>]
```

- **分组**：按 metadata 的 `(scene, agents)` 分组，seed 为重复维度
- **pass@k**：k=1..n 无偏估计 `1 - C(n-c,k)/C(n,k)`（n=组内 run 数，c=finished=true 数）
- **pass^k**：`(c/n)^k`——全成功可靠性指标，比 pass@k 严格；pass@k=0.8 但 pass^k=0.2 说明系统不可靠
- 每组还聚合：数值指标 mean/std/min/max、end_reason 分布、failure_taxonomy 汇总、违规 Top、trajectory_checks pass 率、llm_judge 跨 seed 均值
- 输出 `<results-root>/aggregate_report.{json,md}`；缺 eval_report.json 的**叶子**目录列入 skipped 清单

**扫描是递归的**：同时支持扁平布局（`results/<run>/`）与 benchmark 嵌套布局（`results/benchmark/scene_X/agents_Y/seed_Z/`）。三级判定：

1. 含 `eval_report.json` → 收录，停止下探
2. 否则含 run 标记文件（`trajectory.csv` / `summary.csv` / `metadata.json` / `result.json`）→ 这是**未评测的 run**，记入 skipped 并停止下探。少了这一条，`workers/`、`coordinator/`、`supervision/` 等内部子目录会各自被误报成一个"漏评目录"
3. 否则视为中间层目录，继续递归

两类目录直接剪枝：`eval_workspace/`（内部产物，不是 run），以及 `seed_<N>_pass_<M>`（`benchmark.py` 重试时把上一轮结果改名成这个格式）——**这些是被取代的历史尝试，计入会重复计数同一 (scene, agents, seed)，直接污染 pass@k 的 n**

## 9. 回归门禁（二期已实现）

`sar_orch/eval/gate.py` — 把 aggregate 产物变成 CI 可用的 pass/fail。纯函数比较，不调 LLM，**判定权同样留在代码**。

```bash
uv run python -m sar_orch.eval.gate \
  --results-root sar_orch/results/benchmark \
  [--baseline sar_orch/results/baseline_v1] \
  [--config gate.json] [--output <path>] [--warn-only]
```

`--results-root` 既可传目录（无 `aggregate_report.json` 时现场聚合），也可直接传一个 `aggregate_report.json`；`--baseline` 同理。

### 两类检查

| 类型 | 语义 | 无基线时 |
|------|------|----------|
| **absolute** | 绝对下限/上限，防"从来就很差" | 照常运行 |
| **regression** | 与基线同组对比，**只看退化**，改进永不判 fail | 整段跳过 |

可门禁指标：`pass_at_1`、`finished_rate`、`coverage_mean`、`transport_rate_mean`、`balance_mean`、`violations_per_run`、`dispatch_pass_rate_mean`、`hallucination_rate_mean`。每个指标带方向（`higher_is_better`），退化幅度按方向计算，因此"幻觉率上升"和"覆盖率下降"都是退化。

### 三条防误报设计

1. **样本不足降级**：组内 `n < min_runs`（默认 2）时该组所有 fail 降级为 warn 并记 `meta/min_runs`。单个 seed 的波动不构成回归证据（对应设计文档 §9.5 的样本局限风险）。
2. **指标缺失 skip 而非 0 分**：judge 未跑时 `dispatch_pass_rate_mean` 是 `None`，判 `skip`；若当 0 分处理会把"没评"误报成"评得很差"。
3. **组变动只告警**：当前新增的组（基线没有）记 `baseline_group` warn，基线有而当前缺的组记 `missing_group` warn——扫描范围变化不该直接阻塞。

阈值配置按段浅合并到 `DEFAULT_CONFIG`（`min_runs` / `absolute` / `regression`），未知指标名在加载时直接报错而非静默忽略。退出码：`0` 通过 · `1` 阻塞 · `2` 配置非法/报告不可读；`--warn-only` 恒返回 0。

### 接入 benchmark.py

```bash
uv run python sar_orch/benchmark.py --concurrency 2 \
  --eval                                        # 只评不门禁（零 LLM 成本）
  --gate                                        # 门禁（隐含 --eval）
  [--gate-baseline sar_orch/results/baseline_v1]  # 基线，缺省仅跑 absolute 检查
  [--gate-config gate.json]                       # 阈值覆盖，同 gate.py --config
  [--gate-warn-only]                              # 门禁失败也返回 0
```

sweep 结束后：对每个完成的 run 目录跑**确定性 grader**（`--no-llm-judge` 等价路径，零 LLM 成本）→ 递归聚合 → 门禁。`--eval` 只评不门禁；`--gate` 隐含 `--eval`。

`main()` 返回码：`0` 通过（或未启用 `--gate`）· `1` 门禁阻塞 · `2` 门禁本身跑挂了（配置非法/报告不可读，`run_gate` 抛异常时兜底捕获）。`--gate-warn-only` 让门禁失败也返回 `0`，但门禁跑挂仍返回 `2`。

**sweep 被中断（Ctrl-C）时跳过 eval/gate**：`_finalize` 检查 `_shutdown_event`，命中则直接返回 0 并跳过评测——中断留下的是部分数据，对它下门禁判定会误导（不完整 run 可能被误判成失败 episode）。

**关键取舍**：缺 `trajectory.csv` 的目录**跳过而非评测**。`load_episode` 对缺失文件是降级而非抛错，所以评一个崩溃的 run 会产出空 `episode` 块，聚合器随后把它当作"未完成的 episode"计入 `pass@k` 分母——基础设施噪声伪装成 agent 质量回归。这正是方法论来源里 LangChain"先排除基础设施噪声再归因 agent"的落点。

### 二期遗留

- judge 校准集管理（20 步人工标注，目标一致率 ≥80%）
- 基线目录的版本管理约定（当前靠 `--gate-baseline` 手工指路）

## 10. 相关文档

- 设计方案：`docs/plans/2026-07-19-sar-eval-agent-design.md`（含方法论来源、数据陷阱清单、实施审计记录）
- 实验日志产物说明：`docs/system_docs/logging_map.md`
- SAR 实验框架：`docs/system_docs/框架.md`
