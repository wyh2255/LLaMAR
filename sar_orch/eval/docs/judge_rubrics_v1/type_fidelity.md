# type_fidelity

## 1. 判定问题

**声称的对象类型对吗？**——每条 `report_observation` claim 的
`object_type` 与该对象在环境中的真实类别是否一致。

## 2. 能力契约

- **测什么**：worker 对所见对象的分类是否正确——把火报成水库、把人报成
  物资、把 agent 报成 deposit 都属于类型失真的幻觉。
- **常见弱行为**：对象类别张冠李戴；用含糊/非标准类别（如 `status`）替代
  真实类别报告。
- **捷径区分**：弱 worker 的捷径是"只报告名字不核对类别"或"套用模板类别
  "——本维度逐 claim 核对类别。
- **与相邻维度划界**：类别不存在（对象是编造的）→ existence_grounding；
  类别存在但类别词错误 → 本维度；类别正确但属性值错误 → attribute_fidelity。

## 3. Oracle 标注

`fact`——真值可查：环境对象类别由 Names 列表条目形态与全局视野描述共同
确定。已实证真阳性样本：claim `("deposit", "Charlie")`——Charlie 是 agent
不是 deposit。

类别词表（归一目标）：
- `fire` / `fire_region`（`*_Region_*` 后缀）；
- `person`（全局视野 "LostPerson..." / `requiring N agents to carry`）；
- `reservoir`（"containing Sand/Water"）；
- `deposit`（"DepositFacility"）；
- `agent`（metadata agent_names：Alice/Bob/Charlie/David/...）。

## 4. 评分规则（比例分数）

分数 = 类别正确的 claim 数 / 可判定的 claim 数，`[0,1]`。

每条 claim 的判定：
- **supported**：`object_type` 归一后与真值类别一致（fire 报 fire、
  `EnglandFire_Region_1` 报 fire_region 类、agent 报 agent）；
- **unsupported**：归一后可证不一致（`("deposit", "Charlie")` 类）；
- **不计入分母/分子**：`object_type: "status"` 的状态更新、name=null 的
  claim、非 claim 结构——没有可核对的类别断言。

归一规则：类别词表映射 + 大小写归一；对象名后缀 `_Region_N` 归为
fire_region（报告 `fire` 对 fire_region 也算 supported——父对象语义下
类别不细分）。

## 5. 证据需求

- bundle 内：该样本的 claim 列表（object_type / name）；
- 真值侧：该 step 的 trajectory Observations（Names 列表对象形态 +
  全局视野描述）、metadata agent_names；
- 检索工具：`query_semantic_map`（对象类别 provenance）；
- 引用格式：`<file>:L<logical_line> sha256=<digest>`。

## 6. Abstain 条件

- 样本无可判类别的 claim → **abstain**；
- 对象类别真值无法确定（Names 与全局视野均缺失且 semantic map 无数据）；
- 环境侧类别本身含糊（无法从证据区分 fire 与 fire_region 之外的歧义）。
