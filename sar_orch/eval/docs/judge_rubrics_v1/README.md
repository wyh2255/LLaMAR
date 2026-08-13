# LLM Judge 评分准则 v1（设计基线）

状态：**设计基线第一版**（2026-08-13）。准则本体与执行形态解耦——本文档只定义
**判什么、怎么判、判到什么刻度**，不绑定任何执行形态（家族 DeepAgent / 单
judge / 未来确定性实现均可消费同一本体）。

## 定位

- judge 评分是**诊断信号，不是 gate**。gate 由确定性 grader 负责（
  `sar_orch/eval/graders/`）。
- 目标：把 judge 的评分准则做成可审计、可校准、可分流（事实类→确定性）的
  声明式文本，而不是散落在各 prompt 文件里的模糊指令。

## 维度结构（2 家族 × 7 维度）

| 家族 | 维度 | 判定问题 | oracle 类型 |
|---|---|---|---|
| dispatch | dispatch_completeness | 未处理目标的识别完整吗（漏报方向） | judgment |
| dispatch | dispatch_feasibility | 派的活执行者能做吗 | fact（需消歧） |
| dispatch | dispatch_novelty | 派的是新活吗（误报方向） | fact |
| dispatch | dispatch_efficiency | 已识别目标间的优先级权衡值吗 | judgment |
| observation | existence_grounding | 声称的对象存在吗 | fact |
| observation | type_fidelity | 声称的对象类型对吗 | fact |
| observation | attribute_fidelity | 声称的属性对吗 | fact |

变更记录：v2 rubric 的 `position_fidelity` 从 8 维度中移除（2026-08-13 实证：
claim 仅 4/34 带非 null position，且环境日志无对象坐标真值，见
`existence_grounding.md` 的验证附录）。位置信息不再独立判分。

## 判定对象划界（防重叠）

dispatch 家族按"识别行为的两个方向 + 执行前提 + 排序"划分：

- **completeness = recall**：该识别为待处理的目标，识别了没。漏报方向。
- **novelty = precision**：已处理/处理中的目标，有没有被重复派。误报方向。
- **feasibility**：识别对了，执行者能不能做（位置/库存/能力匹配）。与识别
  对错无关。
- **efficiency**：识别对了且都能做，先做哪个值（预算/优先级）。只判已识别
  目标之间的排序，不判识别对错。

observation 家族按 claim 的字段划分：对象存在性 / 对象类型 / 对象属性。
属性（intensity、type、status、supply_type、requires_agents）归一后与
环境全局视野描述比对；位置不再独立成维度。

## 评分刻度约定

- **fact 类维度**：比例分数 `[0,1]`，分母 = 该样本可判定的 claim/派发数，
  分子 = 得到支持的 claim/派发数。档位问题留给未来确定性实现。
- **judgment 类维度**：三档 `1.0 / 0.5 / 0.0`，每档绑可观测条件（见各维度
  文档第 4 段）。三档语义：无问题 / 部分正确（有实质正确内容但有遗漏或瑕疵）
  / 证据确凿的失败且无合理理由。

## 事实类真值来源（已实证，2026-08-13）

以 run `20260719_141217_s2_s42_a4` 的 34 条 report_observation claim 为样本：

| 真值 | 位置 | 实证结论 |
|---|---|---|
| env Names 列表 | trajectory.csv Observations 列（每 step 每 agent 一段） | 仅含 region 级子对象；父对象 claim 需归一匹配 |
| 全局视野描述 | 同上，`Globally, I can see:` 段落 | 含父对象名与属性（"EnglandFire with average intensity of Low of Chemical type"）；属性比对的主要真值源 |
| agent 坐标 | 同上，`I am at co-ordinates:` | 只有 agent 自身坐标，**无对象坐标真值**（position 维度移除依据） |
| 库存快照 | agent_interactions.csv Observation 列解析 | 动作前后快照（`inventory_before`），feasibility 的库存匹配真值源 |

claim 形态：绝大多数 ToolArgs 是结构化 JSON（`object_type` / `name` /
`attributes` / `note`），name 为 canonical 名，无需自然语言指称消歧。三类
离群形态（`object_type: "status"`、`name: null`、非 claim 结构）在各 fact
维度文档中显式规定了处理方式。

## 与现有代码的关系

- 本目录是准则**设计基线**；`rubrics/*.yaml` 与
  `agent/prompts/dimensions/*.md` 的同步属于实施阶段工作，不在本版范围。
- 同步时注意既有坑位（见 llm-judge-role-runners skill）：维度清单硬编码在
  家族 prompt；dimension prompt 字节不进 rubric digest 锚点（I1 盲区）；
  anti-halo 守卫对"分数措辞"的误拒。
- 事实类维度未来分流确定性 grader 时，判定规则以本目录文档第 4 段的
  判定规则为准绳（含归一规则与离群处理）。
