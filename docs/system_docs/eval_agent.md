---
日期: 2026-07-27
文档类型: 系统架构文档
文档概述: SAR 测评 Agent（eval_agent）设计详解 — legacy 兼容评测与 attempt-v2 LangGraph 控制面的边界、产物与门禁，面向新手的设计导读
---

# SAR 测评 Agent（eval_agent）设计

## 1. 这是什么

`sar_orch/eval/` 是一个**离线评测系统**：输入已完成的 SAR 实验结果目录（CSV/NDJSON/JSON），输出机器可读 JSON 和人读 Markdown 报告。它回答三类问题：

| 层级 | 问题 | 例子 |
|------|------|------|
| Action 级 | 单次动作是否合理？ | 没水为什么还灭火？导航目标是不是幻觉？ |
| Step 级 | Coordinator 派遣是否合理？ | 是否全员有任务？分工是否匹配？ |
| Episode 级 | 整局结果如何？ | 覆盖率、运输率、token 效率、违规分布 |

当前同时保留两条明确隔离的运行路径：

1. **legacy compatibility path**：未注入 workflow adapter 的直接 CLI。它保留根目录 `eval_report.{json,md}` 与 `eval_workspace/`，以兼容已有结果和工具。
2. **attempt-v2 workflow path**：由 `workflow.run_from_results_dir()` 与 benchmark eval/gate adapter 使用的 LangGraph 控制面。一次原始实验可以有多个 attempt，但只发布一个经过验证的 `selected_attempt.json`。

两条路径的报告不得混合统计或互相回退。

## 2. 核心设计原则

> **确定性代码拥有正式判定、artifact 和发布权；模型只能提出受限、可验证的草稿。**

| 层 | Owner | 可以做什么 | 不能做什么 |
|---|---|---|---|
| 确定性 grader | `graders/` | 产出 hard facts、指标、违规和证据 | 不调用模型 |
| workflow | `workflow.py` | 生命周期、lease、resume、cancel、final ledger、selected pointer | 不让模型直接写正式结果 |
| score role | `agent/roles.py` | 读取当前 ScoreJob 的 allowlist evidence，返回 `ScoreDraft` | 文件写入、shell、`task`、跨 job 读取、canonical 写入 |
| validator / merge | contracts、artifact、merge 层 | 校验草稿、写 ScoreResult、确定性合并 | 不伪造模型结果 |
| aggregate / gate | `aggregate.py` / `gate.py` | 同 family 聚合和回归比较 | 不把 Judge 诊断指标变成 release 判据 |

因此，即使模型失败或输出不合格，确定性证据仍是权威；模型失败会成为 typed failure、`PARTIAL` 或 `not_requested`，而不是被伪造成成功分数。

## 3. 两条运行路径

```text
直接 CLI（无 workflow adapter）
  → legacy 顺序路径
  → eval_workspace/ + 根目录 eval_report.{json,md}

benchmark / 注入 workflow adapter
  → AttemptLease + immutable input manifest
  → materialize evidence + deterministic graders
  → no-LLM 路径，或受限 ScoreRoleRunner fan-out
  → validator / deterministic merge / renderer / final ledger
  → 仅 SUCCEEDED 且 digest 链完整时发布 selected_attempt.json
```

`--no-llm-judge` 是显式成功模式：不构造 runner/模型、不创建 score job，`judge_execution_status=not_requested`；只要确定性链完成，workflow 可为 `SUCCEEDED`。

## 4. 关键目录与模块

```text
sar_orch/eval/
├── cli.py                    # legacy CLI；可显式注入 workflow adapter
├── dataset.py                # 实验目录 → EpisodeDataset
├── graders/                  # 五个确定性 grader，正式 hard facts 来源
├── contracts.py              # Series / Attempt / ArtifactRef / 状态与 typed contract
├── artifacts.py              # containment、digest、atomic write、lease、selected pointer
├── audit.py                  # append-only audit journal
├── rubric_registry.py        # 版本化 rubric 读取与校验
├── rubrics/                  # frozen dispatch-v1 / observation-v1 source
├── score_merge.py            # 确定性 ScoreJob / ScoreResult merge
├── workflow.py               # LangGraph lifecycle、resume、cancel、publish
├── agent/
│   ├── runner.py             # fake / real runner protocol
│   ├── roles.py              # job-scoped evidence reader + restricted ScoreRoleRunner
│   ├── eval_agent.py         # legacy DeepAgent compatibility path
│   ├── tools.py              # legacy compatibility tools
│   └── prompts/              # legacy prompts 与 versioned rubric prompt sources
├── report.py                 # legacy report merge + attempt-v2 report projection
├── aggregate.py              # legacy scan 与 selected-attempt aggregate
└── gate.py                   # same-family regression gate
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
| 可选文件缺失（如 semantic_map.jsonl 未生成） | 降级 + `grader_skips` 显式记录，不静默 |

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
| **OutcomeGrader** | episode | 终局指标、token 效率（含地图开销占比）、Balance（论文 §5：`min(s_i)/(max(s_i)+1e-4)`，覆盖全部 n 个环境 agent）、进度曲线 | 纯记录，`passed=None` |
| **StateGrader** | episode | 交叉验证：trajectory 重建子任务完成数 vs Finished；run_metrics.json vs summary.csv；指标回退检测 | 验证型 |
| **ConstraintGrader** | action | 6 条负例规则（见下表） | 违规列表，按严重度分级 |
| **ErrorTaxonomy** | action | 每个失败动作归因（优先级链） | 失败分布直方 |
| **TrajectoryGrader** | action/step | 流程序列约束（救人/灭火/协作搬运） | 4 项检查 pass/fail |

**OutcomeGrader 与论文 §5 指标的对照**（`sar_orch/eval/graders/outcome.py`）：

| 论文符号 | detail 字段 | 说明 |
|------|------|------|
| TR | `final_transport_rate` | checker 口径的运输率 |
| C | `final_coverage` | 覆盖率 |
| B | `balance` | `compute_balance()` 计算，公式 `min(s_i)/(max(s_i)+1e-4)`；n 取 `metadata.json` 的 `agent_count`（不是 `len(episode.agent_names)`，后者混入了 MapAgent/MapSummarizer 等非环境 agent），从未成功过的 agent 也会以 0 计入 min，因此某个 agent 全程 0 次成功会让 B=0。计入 s_i 需同时满足"该步 `Successes` 为真"且动作不在 `_BALANCE_SKIP_ACTIONS`（`NoOp()` / `NoOp` / `Idle` / `Done`）中——想从 `trajectory.csv` 手工复算 B 时须照此过滤 |
| L | `total_steps` | 本 episode 实际步数 |
| — | `checker_subtask_total` | 论文 TR 的分母，未被任何产物直接记录，由 `round(trajectory_completed / final_transport_rate)` 反推；TR=0 时为 `None`，不猜数 |
| — | `dispatch_count` / `dispatch_completed_count` | Coordinator 下发的自然语言任务条数（来自 `subtasks.csv`），**不是**环境 checker 子任务数，不参与 TR/效率计算 |
| — | `step_efficiency` | `trajectory_completed / total_steps`（trajectory.csv 的 `CompletedSubtasksDelta` 累加，与 TR 分子同源），不要与 `dispatch_count` 混用 |

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

### 5.3 两种 Judge 路径与角色边界

确定性 grader 负责“发生了什么”；Judge 只能给出受限、可审计的解释或评分草稿。

**legacy compatibility path** 仍保留 `eval_agent.py`、`tools.py` 与 `subagents.py`：它使用一个主 DeepAgent、两个 legacy judge 子代理和 `eval_workspace/`。`save_judge_verdict()` 的 schema 校验仍是这条兼容路径的最后写入防线，但它不是 attempt-v2 的权限模型。

**attempt-v2 path** 使用 `ScoreRoleRunner`：

- 每个 role invocation 有独立 `agent_id`、thread、backend 与空 history；同模型不等于共享上下文。
- 模型唯一可读工具是当前 ScoreJob 的 `read_job_evidence`；`JobEvidenceReader` 只通过 `ArtifactStore.read_verified()` 读取 allowlist evidence。
- `write_file`、`edit_file`、`execute`、`task`、通用 subagent、跨 job ref、绝对路径、traversal 与 legacy `eval_workspace/` 都被拒绝。
- role 只能返回 versioned `ScoreDraft`。validator 才能根据 evidence ref、digest、role 与维度一致性写 `ScoreResult`；非法 draft 只能成为 typed failure / `PARTIAL`。

这条边界保证“模型能提出意见，但不能直接改事实、报告、ledger 或 selected pointer”。

### 5.4 attempt 报告、终态与发布

attempt-v2 的 report 额外携带：

- `report_family="attempt-v2"`；
- `metadata.eval_semantics_version="attempt-stream-v1"`；
- `eval_attempt=<eval_run_id>/<attempt_id>`；
- `input_manifest_digest`；
- `judge_execution_status`。

`final_ledger.json` 是终态的唯一来源。只有 `SUCCEEDED` 且 input manifest、ledger、report 三段 digest 均一致的 attempt，才可通过 lease + expected-revision CAS 发布 `selected_attempt.json`。`PARTIAL` 可以有可读报告和 workflow exit 0，但不是正式 aggregate/gate 样本；`FAILED` 与 `CANCELLED` 同样不能发布。

`report.py` 继续保留 legacy `merge_results()` / `write_both_reports()`；attempt-v2 则由 workflow 的 renderer 生成绑定 manifest/ledger 的 report。两者均不允许把 LLM 输出变成确定性 hard verdict。

### 5.5 CLI、adapter 与退出码

`cli.py` 只在 host 传入 `workflow_adapter` 或通过 `set_workflow_adapter()` 注入后，才把解析后的参数交给 attempt workflow；否则保留 legacy 顺序路径。workflow-only 参数为：

```text
--resume-attempt <eval_run_id>/<attempt_id>
--eval-run-id <uuid>
--attempt-id <uuid>
--attempt-root <controlled path>
```

workflow path 的退出码是：

| Code | Meaning |
|---:|---|
| 0 | `SUCCEEDED`，或可诊断但未发布的 `PARTIAL` |
| 1 | `FAILED` 或发布前 integrity failure |
| 2 | config / locator error、lease busy、publish conflict |
| 130 | `CANCELLED`；写 cancellation ledger，不发布 pointer |

## 6. 关键设计防线

### 6.1 确定性判定不被模型推翻

五个 grader 直接产出 hard facts、违规和 evidence。Judge 的 score/narrative 是可审计诊断，不得改变 deterministic hard violation、workflow hard failure 或 release/gate 结论。

### 6.2 job-scoped evidence 与最小工具面

attempt-v2 role 不继承 legacy 模块级 episode/workspace 状态。每个 job 只可读 allowlist 内、digest 已验证的 evidence；越权访问返回 `evidence_not_authorized`，不退化为“尽量读取”。

### 6.3 immutable manifest、final ledger 与 selected pointer

输入在 `input_manifest.json` freeze 后不可重写；最终状态在 `final_ledger.json`；正式样本由 `selected_attempt.json` 指向。三者 digest 任一不匹配时 fail-closed，aggregate 记录 skipped/incompatible，而不是改用别的 attempt。

### 6.4 诊断指标不得绕入门禁

`dispatch_pass_rate_mean`、`hallucination_rate_mean` 与 `balance_mean` 仍写入报告，但 `load_config()` 和 `evaluate_gate()` 都拒绝它们出现在 absolute/regression 配置中。未来若要改变此规则，必须经过独立 calibration、fresh review 与用户批准。

### 6.5 真实模型与校准是独立授权

fake runner、`--no-llm-judge` 和 deterministic benchmark 不消耗模型成本，也不构成真实 LLM calibration 的授权。任何真实 calibration 必须另外定义样本、holdout、模型策略、成本上限和评估指标。

## 7. 输出文件

### legacy compatibility outputs

| 文件 | 内容 |
|------|------|
| `<run_dir>/eval_report.{json,md}` | legacy 机器/人读报告 |
| `<run_dir>/eval_workspace/grader_results/*.json` | legacy grader artifacts |
| `<run_dir>/eval_workspace/judge_results/*.json` | legacy judge verdicts |
| `<run_dir>/eval_workspace/conclusion.md` | legacy agent conclusion |

### attempt-v2 outputs

| 文件 | 内容 |
|------|------|
| `eval_attempts/<series>/series.json` | immutable series identity / source binding |
| `attempts/<attempt>/input_manifest.json` | frozen source、policy、role、rubric、command digest binding |
| `attempts/<attempt>/audit/events.jsonl` | append-only audit sequence |
| `attempts/<attempt>/audit/final_ledger.json` | terminal status 与 final artifact refs 的唯一来源 |
| `attempts/<attempt>/reports/eval_report.{json,md}` | manifest-bound attempt report |
| `eval_attempts/<series>/selected_attempt.json` | formal published pointer；不存在即该 series 不进入 attempt aggregate |

## 8. 多 episode 聚合：family 是硬边界

两种 aggregate 都按 metadata 的 `(scene, agents)` 分组，以 seed 为重复维度，并输出 pass@k/pass^k、coverage、transport、失败归因、违规和置信区间。区别在于输入选择：

```bash
# legacy：递归读取 root-level eval_report.json；剪枝 eval_workspace/
# 和 eval_attempts/，因此不会把新报告重复算入旧池。
uv run python -m sar_orch.eval.aggregate \
  --results-root sar_orch/results --family legacy

# attempt：只跟随 selected_attempt.json，并核验 pointer → manifest →
# final ledger → report 的 digest/identity/family/semantics chain。
uv run python -m sar_orch.eval.aggregate \
  --results-root sar_orch/results --family attempt
```

attempt aggregate 的 skip 是可观察结果：未 selected、pointer 损坏、非 `SUCCEEDED` ledger、report family/semantics version 不匹配、digest 不符都会带原因写入 `skipped_dirs`。它不会改用同 series 的另一个 attempt，也不会退回 legacy report。

历史 legacy 报告保持原样。需要 attempt-v2 baseline 时，应物理复制原始 source run 后以 attempt family 重跑；不得把两族报告直接拼到同一个 aggregate。

## 9. 回归门禁：只比较同 family 的确定性指标

```bash
uv run python -m sar_orch.eval.gate \
  --results-root sar_orch/results/benchmark \
  --baseline sar_orch/results/baseline_attempt_v2 \
  --family attempt
```

`--results-root` 与 `--baseline` 都可为目录或已有 `aggregate_report.json`。两边 family 不同、配置试图启用诊断/降级指标、或 report 不可读时，CLI 返回 2。

| 类型 | 语义 | 无基线时 |
|------|------|----------|
| **absolute** | 绝对下限/上限 | 照常运行 |
| **regression** | 同 group 的退化比较；改进不判 fail | 跳过 regression |

三条重要边界：

1. 组内 `n < min_runs`（默认 2）时，数值失败降级为 warn。
2. 缺失的可门禁指标为 `skip`，不伪造 0 分。
3. `dispatch_pass_rate_mean`、`hallucination_rate_mean`、`balance_mean` 是诊断量，配置出现即被拒绝，不能用 custom config 重新启用。

### benchmark 接入

```bash
# --eval 走 deterministic attempt workflow；--gate 隐含 --eval。
uv run python sar_orch/benchmark.py --concurrency 2 \
  --gate --gate-baseline sar_orch/results/baseline_attempt_v2
```

完成的 run 才会进入 eval；缺 `trajectory.csv` 的目录跳过而非制造空失败 episode。被 Ctrl-C 中断的 sweep 也跳过 eval/gate，避免把不完整基础设施产物解释为 agent 质量回归。

## 10. 未授权边界与相关文档

- fake runner、`--no-llm-judge`、deterministic benchmark 不等于真实 LLM calibration 授权。
- 真实 calibration 仍需单独批准：分层样本、dev/calibration 与 holdout、模型策略、成本上限、precision/recall/FP/FN/Unknown/role-model slice。
- 当前设计与 phase gate：`.hermes/plans/2026-08-06_004210-eval-judge-transparent-langgraph-design.md`
- 实验日志产物说明：`docs/system_docs/logging_map.md`
- SAR 实验框架：`docs/system_docs/框架.md`
