---
日期: 2026-07-19
文档类型: 技术设计文档
文档概述: SAR 多智能体实验的测评 Agent（Eval Agent）设计方案——对指定实验结果目录做离线分析，组合确定性 grader 与 LLM judge，输出结构化评测报告
---

# SAR 测评 Agent 设计方案

## 1. 背景与目标

### 1.1 定位

对**指定实验结果目录**（如 `sar_orch/results/20260719_141217_s2_s42_a4`）做**离线分析**的测评工具。
不改变现有实验流程；输入是实验产物（CSV/NDJSON/JSON），输出是结构化评测报告。

一期范围：**确定性 grader + LLM judge 一起交付**。judge 模型为可配置项。

**实现方式**：测评 Agent 本体用 LangChain **DeepAgent**（`deepagents` 包的 `create_deep_agent`）实现——利用其内置的任务规划（todo）、文件系统上下文卸载、子代理派发（context quarantine）能力编排整个分析流程；确定性 grader 保持为纯函数、以工具形式挂载，判定权不交给 LLM。

### 1.2 方法论来源

| 来源 | 借鉴点 |
|------|--------|
| Anthropic《Demystifying evals for AI agents》(2026-01) | 三类 grader 组合；评结果不评路径；部分分数；读 transcript；pass@k/pass^k |
| LangChain agentevals / Readiness Checklist | run/trace/thread 三级评测；trajectory match（strict/unordered/subset/superset）；二元 pass/fail 优先；先排除基础设施噪声再归因 agent |
| LLaMAR 原论文 checker（`SAR/Scenes/base_checker.py`） | 子任务匹配即确定性 grader 雏形；Coverage/TransportRate 定义直接复用 |

### 1.3 评测层级（映射 run/trace/thread）

| 层级 | SAR 对应 | 数据来源 | 评测问题 |
|------|----------|----------|----------|
| **Action 级** | 单次工具调用 | `agent_interactions.csv` | 没水是否还灭火？target_id 是否幻觉？动作失败原因是什么？ |
| **Step 级** | 每个 barrier step 全队决策 | `trajectory.csv` + `router_interactions.csv` + `subtasks.csv` | Coordinator dispatch 是否覆盖所有 agent？分工与已知信息是否匹配？ |
| **Episode 级** | 一整局 | `summary.csv` + `metadata.json` | 终局 Coverage/TransportRate/Finished、token 效率、步数效率 |

## 2. 已知数据陷阱（grader 设计前提）

以下陷阱在实现时必须显式处理，否则会产出错误结论：

1. **`ToolResult.success` ≠ 动作成功**：`barrier.submit_action()` 返回的 `success` 恒为 True（`sar_orch/barrier.py:215`）；`navigate_to.py:36` 无条件回 "You have arrived"。**动作成败只能信 `trajectory.csv` 的 `Successes` 列**（来自 `env.step()` 的真实 `act_successes`）。
2. **`ErrorTypes` 列在 CSV 产物中几乎不可用**：barrier 把最后一个 agent 的 `error_type` 贴给所有失败 agent（`sar_orch/barrier.py:377-384`），且实测 `agent_interactions.csv` 的 `ErrorType` 列 178 行**全为空**，`trajectory.csv` 的 `ErrorTypes` 列仅个别 step 有值。不是"归属不准"而是"基本没有"——错误分类必须按 §4.4 的离线归因规则完全重建，不能依赖日志字段。
3. **DropOff 成功是事后同步改写的**（`SAR/env.py:629-643`）：以 `Successes` 列终值为准，不要用中间 event。
4. **TimeoutAgents ≠ LLM 决策**：`trajectory.csv` 的 `TimeoutAgents` 列是 barrier 超时自动填充的 NoOp，必须在 action 级分析中剔除，否则会高估 LLM 的 NoOp 倾向。
5. **观测文本即 ground truth 子集**：`agent_interactions.csv` 的 `Observation` 来自 `controller.get_observation()`，是环境真实状态投影，可作为幻觉检测的比对基准。注意部分行因 CSV 写出截断会丢失 `I am holding`/`co-ordinates` 段（实测 178 行中约 118 行完整），解析器必须容错。
6. **Step 编号偏移**：`router_interactions.csv`/`subtasks.csv` 的 Step 从 0 起（初始 dispatch 为 step 0），`trajectory.csv` 从 1 起（实测 router 0–26 vs traj 1–30）。dataset.py 交叉索引时必须显式对齐，禁止隐式 join。
7. **`EndReason` 区分终止方式**：`trajectory.csv` 末行 `EndReason`（如 `max_steps_reached` / 完成 / wall-clock）是 episode 级归因的关键字段，必须纳入报告。

## 3. 架构总览（DeepAgent 实现）

### 3.1 职责划分原则

| 角色 | 承担者 | 说明 |
|------|--------|------|
| **判定权** | 确定性 grader（纯函数） | pass/fail、指标、违规列表只能由代码产出；LLM 输出**不得修改** `GradeResult.passed`，防止 agent "商量"出好看结果 |
| **编排与深挖** | DeepAgent 主代理 | 制定分析计划（内置 todo）、调 grader 工具、对异常 step 调证据工具细看、派发 judge 子代理 |
| **主观维度评审** | judge 子代理 | dispatch/observation 两个 subagent，context quarantine——主代理只看结论，不被几十条 trace 撑爆上下文 |
| **报告** | report.py + 主代理 | GradeResult 机械合并进 JSON；主代理撰写 md 结论文本 |

### 3.2 目录结构

```
sar_orch/eval/
├── __init__.py
├── cli.py                 # 入口：python -m sar_orch.eval.cli --results-dir <dir> [--judge-model X]
├── dataset.py             # EpisodeDataset：实验目录 → 标准化视图（含 Step 偏移对齐）
├── graders/               # 确定性 grader（纯函数，不依赖 LLM）
│   ├── base.py            # Grader 抽象基类 + GradeResult
│   ├── outcome.py
│   ├── state.py
│   ├── constraint.py
│   ├── trajectory.py
│   └── error_taxonomy.py
├── agent/                 # DeepAgent 装配层
│   ├── eval_agent.py      # create_deep_agent(...) 装配入口
│   ├── tools.py           # 将 dataset/graders 包装为 agent 工具
│   ├── subagents.py       # dispatch_judge / observation_judge 子代理定义
│   └── prompts/
│       ├── system.md          # 主代理 system prompt（分析流程与纪律）
│       ├── dispatch_judge.md
│       └── observation_judge.md
├── analysis/efficiency.py # token/步数效率指标
└── report.py              # GradeResult + agent 结论 → eval_report.json/md
```

### 3.3 运行数据流

```
CLI --results-dir <run_dir>
  → dataset.py 加载 EpisodeDataset，物化摘要到 <run_dir>/eval_workspace/（agent 文件系统可读）
  → create_deep_agent(
       model=init_chat_model(model, base_url=...),   # 可配置，与被测模型可隔离
       tools=[run_grader(name), get_step_evidence(step),
              get_agent_trace(agent), list_constraint_rules(), ...],
       subagents=[dispatch_judge, observation_judge],
       system_prompt=agent/prompts/system.md,
    )
  → 主代理自主执行：
       1. todo 制定分析计划
       2. 调 run_grader 跑全部确定性 grader（GradeResult 落盘 workspace）
       3. 对失败集中/异常 step 调 get_step_evidence 深挖（读 transcript）
       4. task 工具派发 dispatch_judge / observation_judge 子代理
       5. 综合所有结果撰写结论文本（写入 workspace/conclusion.md）
  → report.py 合并 GradeResult（权威判定）+ conclusion.md → eval_report.json / eval_report.md
```

### 3.4 为什么是 DeepAgent 而不是脚本串联

- 评测分析本身是**探索性**的：失败模式事先未知，固定流水线无法"对异常 step 多看一眼"；agent 可按 grader 结果自主决定深挖方向
- DeepAgent 内置 **filesystem 上下文卸载**：大 trace 落盘 workspace，agent 按需读取，契合 SAR 单步观测动辄数千 token 的数据规模
- **subagent context quarantine**：judge 子代理各自评审几十条 dispatch/observation，主代理上下文只进结论
- 模型可插拔：`init_chat_model` 统一抽象，judge 模型做成 CLI 参数即可（天然满足"judge 与被测模型隔离"要求）

## 4. Grader 详细设计

统一接口（`graders/base.py`）：

```python
@dataclass
class GradeResult:
    grader: str            # grader 名
    level: str             # action | step | episode
    passed: bool | None    # 二元判定；None = 仅记录指标
    score: float | None    # 部分分数（0~1）
    detail: dict           # 证据：step、agent、原始片段引用
    evidence_ref: str      # 溯源，如 "agent_interactions.csv:L123"

class Grader(Protocol):
    name: str
    def grade(self, episode: EpisodeDataset) -> list[GradeResult]: ...
```

### 4.1 OutcomeGrader（episode 级，确定性）

读 `summary.csv` + `trajectory.csv` 末行：

- 终局 `FinalCoverage` / `FinalTransportRate` / `Finished`（直接引用，不重算）
- 衍生指标：
  - **步数效率** = 完成任务所需最小步数下界估算 / 实际步数（下界 = 子任务数，粗估即可）
  - **token 效率** = 总 token / 完成子任务数（`token_usage.csv` 聚合）。注意 `summary.csv` 的总 token **包含 MapAgent/MapSummarizer**（语义地图管线）的消耗，报告中需单列"地图开销占比"
  - **Balance**（对齐原论文 `SAR/baselines/sar_logging.py:306` `_get_balance_metric`，需自行实现）：min/max(各 agent 成功动作数)
  - 利用 `trajectory.csv` 的 `CompletedSubtasksDelta` 列做逐步进度曲线（该列已记录每步新完成子任务，免重算）
- 输出 `passed=None, score=各指标`，属纯记录型

### 4.2 StateGrader（episode 级，确定性）

"别信 agent 说 done，查状态"（LangChain checklist 原则）：

- 从 `trajectory.csv` 重建 `checker.subtasks_completed` 终值，与 `Finished` 交叉验证
- 校验 `run_metrics.json` 与 `summary.csv` 一致性
- 检测**指标回退**：coverage/transport_rate 应单调不减，若出现下降则标记异常 step（可能指示环境 bug 或日志错位）

### 4.3 ConstraintGrader（action 级，确定性，负例检查）

逐行扫 `agent_interactions.csv`，结合当步 inventory/position（从 Observation 文本解析 `I am holding {...}` / `co-ordinates`）：

| 规则 | 判定 | 严重度 |
|------|------|--------|
| `UseSupply` 时 inventory 中该资源为 0 | 空资源灭火 | high |
| `NavigateTo(target_id)` 的 target 不在当步可见 Names 列表 | 幻觉导航目标 | high |
| 搬人状态执行非 Navigate/DropOff 动作 | restricted_action 违反（日志已有 error_type 时直接引用） | medium |
| `GetSupply` 时 inventory 已满（3 格） | 无效取用 | low |
| 同一 agent 连续 ≥3 步完全相同的失败动作 | 死循环倾向 | medium |
| LLM 主动 NoOp（剔除 TimeoutAgents 后）占比 > 阈值 | 消极行为 | low |

### 4.4 ErrorTaxonomy（action 级，确定性，替代不可用的 ErrorTypes 列）

对每个 `Successes=False` 的动作做**逻辑推断**（而非文本关键词匹配——实测 Observation 文本中仅 "wasn't visible" 有信号，"wasn't close enough"/restricted 提示文本在 CSV 中不出现）：

- `infrastructure`：该 step `TimeoutAgents` 非空且该 agent 在列 → 优先归类，停止进一步归因
- `not_visible`：动作目标不在当步观测 Names 列表
- `restricted_action`：该 agent 搬人状态（inventory 含 Person）且动作类型非 Navigate/Move/Carry/DropOff
- `not_interactable`：目标在 Names 列表，且由 position 计算距离 > 交互半径（距离阈值从 `SAR/core.py` 的 `sees()`/交互检查逻辑提取为常量）
- `unknown`：其余（重点人工抽查对象；占比高说明归因规则有缺口）

判定顺序即上表顺序（infra → visibility → restricted → distance）。输出失败原因分布直方，这是 Anthropic/LangChain 共同强调的"60-80% 精力在错误分析"的落点。

### 4.5 TrajectoryGrader（action/step 级，确定性）

借鉴 agentevals 的 match 模式，对关键流程做**序列约束**检查：

| 检查 | 模式 | 规则 |
|------|------|------|
| 救人流程 | superset | `Carry(person)` 之后必须出现 ≥2 个 agent 的 `NavigateTo(deposit)` 且最终 `DropOff` 成功 |
| 灭火流程 | superset | `UseSupply(fire, X)` 成功前必须存在对应类型的 `GetSupply` 成功记录（资源来源合法） |
| 协作搬运 | strict | `Carry(person)` 成功 ⇔ 同步step ≥2 agent 同时 Carry 同一 person |
| 探索效率 | 指标型 | 重复 NavigateTo 同一已覆盖目标的比例 |

### 4.6 LLM Judge（step 级，模型可配置，DeepAgent 子代理实现）

两个 judge 实现为 `subagents.py` 中的自定义子代理（字典式 SubAgent：独立 system prompt + 限定工具集），由主代理通过 `task` 工具派发；每维度单独 judge（符合 Anthropic 建议），二元 pass/fail + "Unknown" 出口防幻觉。子代理模型可与主代理不同（`--judge-model` 独立配置）。

**DispatchJudge — Coordinator 派遣质量**
- 输入：当步语义地图状态 + team 状态 + 当步 `router_interactions.csv` 的 dispatch 内容
- ⚠️ 语义地图状态获取方式（实测修正）：`semantic_map.jsonl` 只有 `observation_ingested` 增量事件、**无快照**，重建需重放（复杂度高的降级方案）；**优先用 `map_summary.jsonl`**——含 `env_step`/`summary`/`trigger_reasons`，取 ≤ 目标 step 的最近一条中文摘要作为地图状态输入。两文件都缺失时该 judge 降级跳过并标注
- rubric 维度（每维二元）：
  1. 是否给所有 agent 都派了任务（无 idle 导致 barrier 空转）
  2. 分工是否与 agent 位置/库存匹配（派无水 agent 去灭火 = fail）
  3. 是否利用语义地图中的已知信息（已知火位置仍派人盲目 explore = fail）
  4. 步数预算意识（后期仍有大量低价值派遣 = fail）
- 抽样策略：全量 step 太多时，等距抽 ≤20 步 + 所有失败 step 必审（抽样由主代理按确定性 grader 结果决定，写入派发指令）

**ObservationJudge — Worker 观测报告质量（幻觉检测）**
- 输入：worker 的 `report_observation` 输出 vs 同行 `Observation`（环境真实投影）；辅以 `agent_interactions.csv` 的 `LLMOutput` 列交叉检查（claim 在 LLMOutput 出现但环境观测无支撑 → 幻觉候选）
- rubric：报告中每个对象声明（类型/位置/属性）是否有真实观测支撑；无支撑 = 幻觉，逐条列出
- 这是 semantic 模式的核心质量指标：语义地图被污染的程度

**配置**：`--judge-model`（默认 deepseek-v4-flash），`--judge-base-url`、`--judge-api-key` 走 `.env` 同名字段兜底。judge 与被测模型同型号时，报告中必须标注 `judge_model == subject_model` 的偏差警告。

## 5. 数据模型（dataset.py）

```python
@dataclass
class StepRecord:
    step: int
    actions: list[str]
    successes: list[bool]
    timeout_agents: list[int]
    coverage: float
    transport_rate: float
    interactions: list[AgentInteraction]   # 来自 agent_interactions.csv，按 step 对齐
    dispatches: list[Dispatch]             # 来自 router_interactions.csv

@dataclass
class EpisodeDataset:
    run_dir: Path
    metadata: dict                          # metadata.json
    steps: list[StepRecord]
    summary: dict                           # summary.csv
    token_usage: pd.DataFrame
    semantic_map_log: list[dict]            # semantic_map.jsonl（可选存在，增量事件）
    map_summaries: list[dict]               # map_summary.jsonl（可选存在，DispatchJudge 首选地图输入）
    agent_names: list[str]
```

加载容错：`semantic_map.jsonl`、`map_summary.jsonl`、`supervision/` 缺失**或为空目录/空文件**时降级（对应 judge/grader 跳过并标注 `skipped_reason`）。router/subtasks 与 trajectory 的 Step 偏移（0 起 vs 1 起）在本层完成对齐，下游 grader 只看到统一视图。

## 6. 输出报告

### 6.1 `eval_report.json`（机器可读，主产物）

```json
{
  "run_dir": "...",
  "metadata": {"scene": 2, "agents": 4, "seed": 42, "model": "..."},
  "episode": {"coverage": 1.0, "transport_rate": 0.933, "finished": false,
              "steps": 30, "total_tokens": 1463922, "balance": 0.71},
  "failure_taxonomy": {"not_visible": 12, "not_interactable": 5, "...": "..."},
  "constraint_violations": [{"rule": "empty_supply_use", "step": 7, "agent": "Bob", "evidence_ref": "..."}],
  "trajectory_checks": [{"check": "rescue_flow", "passed": true, "detail": "..."}],
  "llm_judge": {"dispatch": {"pass_rate": 0.85, "sampled_steps": 20, "failures": [...]},
                "observation": {"hallucination_rate": 0.03, "claims": [...]},
                "judge_model": "deepseek-v4-flash", "same_model_warning": true},
  "grader_skips": [{"grader": "ObservationJudge", "reason": "semantic_map.jsonl missing"}]
}
```

### 6.2 `eval_report.md`（人读摘要）

按"结论先行"组织：指标总表 → 失败归因分布 → 严重违规 Top-N（带 evidence_ref 可跳查）→ judge 结果 → 建议改进点（规则化模板：哪类失败占比最高就给对应修复建议）。

### 6.3 与现有体系的关系

- 不替代 `render-sar-report` skill（那个面向"看过程"，本工具面向"下判定"）；`eval_report.json` 可作为其额外数据源后续接入
- CLI 支持 `--output` 指定路径，默认写到 `<run_dir>/eval_report.*`

## 7. CLI 设计

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.eval.cli \
  --results-dir sar_orch/results/20260719_141217_s2_s42_a4 \
  [--agent-model deepseek-v4-flash] \   # 主代理模型
  [--judge-model deepseek-v4-flash] \   # judge 子代理模型（可与主代理不同）
  [--judge-sample-steps 20] \
  [--no-llm-judge]            # 跳过 DeepAgent，只跑确定性 grader + 机械报告
  [--output <path>]
```

依赖：在 `pyproject.toml` 增加 `deepagents`（经 uv 管理）；模型通过 `langchain.chat_models.init_chat_model` 初始化，复用 `.env` 的 `api_key`/`api_base`（openai 兼容端点）。`--no-llm-judge` 模式不实例化 DeepAgent，保证零 LLM 成本的最小可用路径始终存在。

## 8. 里程碑

| 阶段 | 内容 | 验收 |
|------|------|------|
| M0 | 引入 `deepagents` 依赖；`init_chat_model` 连通 deepseek 端点并验证工具调用 | 最小 agent 跑通一次工具调用往返 |
| M1 | dataset.py + outcome/state grader + JSON 报告骨架 | 对 `20260719_141217_s2_s42_a4` 跑通，指标与 summary.csv 一致 |
| M2 | constraint + error_taxonomy + trajectory grader | 失败归因分布合理，抽查 10 条证据链无误 |
| M3 | DeepAgent 装配（tools.py + system.md）+ 两个 judge 子代理 + 抽样策略 | 人工标注 20 步与 judge 一致率 ≥80%（校准）；`--no-llm-judge` 路径不受影响 |
| M4 | report.py 合并 + eval_report.md + CLI 完善 + 文档 | 一键跑完出双报告 |

**二期（不在本方案范围）**：多 episode 聚合（pass@k/pass^k 跨 seed）✅、回归门禁接入 benchmark.py ✅、judge 校准集管理（未完成）。

## 9. 风险与开放问题

1. **观测文本解析脆弱性**：ConstraintGrader 依赖从 Observation 自由文本解析 inventory/position，若格式变更会失效 → 解析器集中放在 dataset.py，配单测；实测约 1/3 行有截断，需容错。
2. **judge 校准成本**：M3 需要人工标注 ~20 步作为校准集，是一次性人工成本，需预留。
3. **semantic_map.jsonl 缺失场景**：oracle 模式实验无此文件，ObservationJudge 自动降级为仅比对 `report_observation` vs 环境观测，仍能出幻觉率。
4. **步数效率下界估算粗糙**：一期只做粗估（子任务数），不做路径规划级精确下界。
5. **样本局限**：基准实验目录（`20260719_141217_s2_s42_a4`）中 `TimeoutAgents` 全空、`map_summary.jsonl` 仅 8 条，相关逻辑在该样本上无法充分验证，M2/M3 验收需另选含 timeout 与频繁地图更新的 run 补测。
6. **ErrorTaxonomy 的 `not_interactable` 依赖距离阈值常量**：需从 `SAR/core.py` 交互检查逻辑中核实阈值（`sees()`/GPS.near 半径），阈值取错会系统性误分类，M2 时先用 `core_unittest.py` 的用例校准。
7. **DeepAgent 引入的新风险**：
   - `deepagents` 包演进快、API 可能变动 → 在 `pyproject.toml` 中 pin 版本，升级单独评审；
   - deepseek 模型的 function-calling 稳定性是主代理可靠性的前提 → M0 专门验证；
   - 主代理可能跳过计划中的 grader 调用 → report.py 以**确定性 grader 的落盘结果为必备项**，agent 未跑某 grader 时报告中标 `missing` 而非静默缺失；
   - judge 子代理 token 成本随抽样步数线性增长 → `--judge-sample-steps` 硬上限。

## 10. 评审记录

2026-07-19 经独立 subagent 对照真实数据（`results/20260719_141217_s2_s42_a4` + 源码）审查，已修正：

- ErrorTypes 列实测全空（原描述"归属不准"过轻）→ §2(2) 改为"不可用，完全重建"
- ErrorTaxonomy 从文本关键词匹配改为逻辑推断（`not_interactable`/`restricted_action` 文本信号在 CSV 中不存在）→ §4.4
- DispatchJudge 地图输入：`semantic_map.jsonl` 无快照需重放 → 改用 `map_summary.jsonl` 优先 → §4.6
- 补充 Step 编号偏移（router 0 起 / trajectory 1 起）、`EndReason`、CSV 截断容错 → §2(5)(6)(7)
- token 效率需单列 MapAgent/MapSummarizer 地图开销 → §4.1
- Balance 实现路径确认为 `SAR/baselines/sar_logging.py:306`（非 `SAR/sar_logging.py`）→ §4.1
- ObservationJudge 补充 `LLMOutput` 列交叉检查；supervision 空目录降级；样本局限风险 → §4.6/§5/§9

2026-07-19 二次修订（用户决策）：实现方式从"脚本流水线 + llm_judge.py 模块"改为 **LangChain DeepAgent（`create_deep_agent`）编排架构** → §1.1/§3 整体重写，§4.6 judge 改为子代理实现，§7 CLI 增加 `--agent-model`/`--no-llm-judge`，§8 增加 M0，§9 增加风险 7。核心不变量：**判定权留在确定性 grader，DeepAgent 只做编排、深挖、主观评审与报告撰写**。

2026-07-19 实施记录（M0–M2 审计偏差，均已验收）：

- **ErrorTaxonomy 新增 `obstacle_blocked` 类别**：§4.4 原 taxonomy 未覆盖 Move/Explore 失败（目标是方向而非对象，not_visible 不适用），实测占失败 50%，全落 unknown 会淹没真实信号。实现按"移动失败 → obstacle_blocked（likely obstacle/boundary）"启发式归类，detail 中明示启发式属性；后续可解析 Observation 方向格内容升级为证据型判定
- **`not_interactable` 改为启发式**：CSV 产物不含目标对象坐标，无法做 §4.4 原设计的精确距离计算。已实现为"目标在 Names 列表 + 非 timeout/restricted → not_interactable"，交互半径常量（3√2≈4.24，SAR/core.py:1832-1863）作为参考值写入注释
- **动作名别名归一**：`agent_interactions.csv` 的 Action 列存在工具级命名（`CarryPerson(...)`），与 trajectory 的环境级命名（`Carry(...)`）不一致 → dataset.py `parse_action` 统一归一，ErrorTaxonomy 对无法映射的失败计入 `unmapped_failures` 明示而非静默丢弃
- **evidence_ref 行号约定**：`L<N>` 为 CSV 逻辑记录号（含表头，从 1 起），因 Observation 含内嵌换行，与物理行号不一致；用 pandas/csv 模块按记录号回查

2026-07-19 实施记录（M3 审计）：

- **模型**：主代理/judge 均用 .env 配置的 deepseek-v4-flash（packyapi 代理实测 tool calling 正常，M0 时该端点不支持此模型的结论已过时）。本例 judge_model == subject_model → same_model_warning=true 按 §4.6 正常标注
- **ObservationJudge 输出契约收紧（审计返工）**：初版 judge 输出聚合格式（total_hallucinations + summary），无 per-claim 明细且漏检实证（Alice@1 把 agent Charlie 报为容器对象）。修复：prompt 强制固定 schema（claims 数组）、`save_judge_verdict` 工具侧 schema 校验（非法输出拒绝保存迫使重试）、hallucination_rate 改由 claims 计算。**经验：LLM 子代理的结构化输出必须工具侧硬校验，不能只靠 prompt 约定**（§9.7 风险的实证）
- **§9.7 风险实测**：deepseek-v4-flash 主代理功能完整（84 次工具调用、两 judge 均派发、conclusion 落盘），但存在冗余文件探索（约半数 tool call 是 read_file/ls 而非专用 eval 工具）与 5-7 分钟延时；子代理 JSON 输出格式漂移需 flatten 层多格式兼容 + 工具校验双防线

2026-07-19 实施记录（M4 审计）：

- **DispatchJudge 增加客观锚点（审计返工）**：schema 校验修复格式漂移后，暴露判定漂移——同一"0 派遣 step"不同运行判 pass/fail 不一（pass_rate 0.5~0.95 大幅波动）。修复：`prompts/dispatch_judge.md` 增加 Anchor A/B/C（0 派遣且任务未完 → full_coverage/map_awareness 必 fail；notes 必须写明派遣数），rubric 客观规则优先于整体判断。修复后 0 派遣 step 判定一致（step 8/13 fail，notes 明确 "0 dispatches"）。**经验：judge rubric 的可判定项要尽量客观化，整体判断只留给真正主观的维度**
- **judge 保存侧 schema 校验对称化**：dispatch verdict 与 observation 同样走 `save_judge_verdict` 硬校验（verdicts list + 4 维度 pass/fail/Unknown），canonical 文件名固定 `dispatch_full.json`/`observation_full.json`；merge 层只读 canonical，缺失才降级 flatten 并在报告标 `format_fallback: true`
- **judge 运行间数值波动属预期**（抽样步不同 + 主观维度判断差异），正式校准（20 步人工标注，一致率 ≥80%）待用户执行

2026-07-23 实施记录（二期回归门禁）：

- **`scan_results` 由深度 1 改为递归**：原实现只扫 `root_dir` 直接子目录，对 benchmark 的 `benchmark/scene_X/agents_Y/seed_Z/` 嵌套布局**一个 run 都找不到**（门禁接入的前置阻塞）。改为递归下探：含 `eval_report.json` 即收录停止；否则含 run 标记文件（`trajectory.csv` 等）则记为"未评测 run"并停止——少这一条，`workers/`、`coordinator/`、`supervision/` 会各自被误报成一个漏评目录
- **重试备份目录必须剪枝**：`benchmark.py` 重试时把上一轮结果改名为 `seed_<N>_pass_<M>`（benchmark.py:275-278）。递归扫描会把备份和当前结果都算进同一 `(scene, agents)` 组，**重复计数同一 seed 直接污染 pass@k 的 n**。已按名字模式剪枝，同时剪 `eval_workspace/`
- **门禁三条防误报设计**：(1) 组内 `n < min_runs`（默认 2）时 fail 降级 warn——单 seed 波动不构成回归证据（§9.5 样本局限的直接对策）；(2) 指标缺失判 `skip` 而非当 0 分，否则"judge 没跑"会误报成"judge 评分极低"；(3) 组增减只 warn，扫描范围变化不该阻塞
- **`--eval` 跳过缺 `trajectory.csv` 的目录**：`load_episode` 对缺失文件是**降级而非抛错**，评一个崩溃的 run 会产出空 `episode` 块，聚合器随后把它当"未完成 episode"计入 pass@k 分母——基础设施噪声伪装成 agent 质量回归。这是 §1.2 中 LangChain"先排除基础设施噪声"原则的落点
- **grader 注册表去重**：`ALL_GRADERS` 原在 `cli.py`（模块级 import langchain），benchmark 复用会拖入 LLM 依赖链。已下沉到 `graders/__init__.py` 作为单一来源，cli/benchmark 共用
- **验收**：42 条单测（门禁 30 + benchmark 接线 12）全绿（现状复核于 2026-07-27）；对参考 run 构造退化副本实测——n=1 时降级 warn 退出 0、n=2 时 absolute+regression 共 4 项 fail 退出 1
