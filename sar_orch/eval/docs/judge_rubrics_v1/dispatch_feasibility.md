# dispatch_feasibility

## 1. 判定问题

**派的活执行者能做吗？**——每个派发的任务，与指定 agent 在派发时刻的
位置 / 库存 / 能力是否匹配。只判执行前提，不判识别对错（归 completeness/
novelty）、不判优先级（归 efficiency）。

## 2. 能力契约

- **测什么**：coordinator 对 agent 当前状态（库存、位置、能力）的把握——
  派发任务时是否考虑了"这个 agent 现在具备执行条件"。
- **常见弱行为**：派背包里没有对应物资的 agent 去灭火（库存错配）；派
  一个人去救需要 2 人协作抬的人（能力错配）；派 agent 去它到不了的目标。
- **捷径区分**：弱 coordinator 的捷径是"只按目标派活、不看 agent 状态"
  ——本维度专抓这类错配。defensible 规则：以派发时刻的证据为准，事后才
  变得不可行的不算错（如物资是在派发之后才耗尽）。
- **与确定性层的镜像**：动作级同类判定已存在于
  `ConstraintGrader.empty_supply_use`（背包没水硬灭火）与
  `TrajectoryGrader.coop_carry`（抬人需 ≥2）。本维度是派发级判定——抓的是
  决策时刻的错配，不是执行时刻的失败。

## 3. Oracle 标注

`fact`（需消歧）——真值可查：库存快照（agent_interactions.csv Observation
列解析的 `inventory_before`）、任务文本中的目标与物资要求、map summary 中
的目标可达性。消歧点：任务文本是自然语言，从中提取"需要的物资类型/人数"
需要归一（`SUPPLY_TYPE_MAP` 的 SAND/WATER 规则已存在，见
`graders/constraint.py`）。

## 4. 评分规则（比例分数）

分数 = 得到支持的派发数 / 可判定的派发数，`[0,1]`。

每条派发的判定：
- **supported**：派发时刻 agent 的库存/位置/能力证据支持该任务（如灭火
  任务且 agent 持有对应物资、抬人任务且安排了 ≥2 人）；
- **unsupported**：证据确凿的错配（如 `UseSupply(Water)` 类任务但 agent
  库存 Water=0、单人去救 requires_agents=2 的人、目标不可达）；
- **不可判定**：库存/位置证据缺失（bundle 与检索均不可得）→ 该条不计入
  分母，也不计入分子。

defensible 判定：以派发时刻状态为准；任务文本本身含糊（没说需要什么物资）
不判 unsupported，记不可判定。

## 5. 证据需求

- bundle 内：router_interactions 派发任务文本、team status（位置/库存/
  进行中任务）；
- 检索工具：`read_worker_state`（派发时刻的库存/位置轨迹）、
  `read_tool_trace`（`GetSupply`/`UseSupply` 的实际结果）、
  `query_semantic_map`（目标位置与 interactability）；
- 引用格式：`<file>:L<logical_line> sha256=<digest>`。

## 6. Abstain 条件

- 本步无派发可判；
- 全部派发的库存/位置证据均不可得（`read_worker_state` 后仍无法确定）；
- 不判战术偏好（"换我会派别人"）——只判客观错配。
