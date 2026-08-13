# attribute_fidelity

## 1. 判定问题

**声称的属性对吗？**——每条 `report_observation` claim 的 `attributes`
（intensity、type、status、supply_type、requires_agents 等）与该对象在
环境真值中的属性描述是否一致。

## 2. 能力契约

- **测什么**：worker 对对象状态的读取精度——火的强度与化学类型、水库的
  物资类型、人的状态（Grounded / 被抬走）等。
- **常见弱行为**：把低强度火报成高强度；把 Non-chemical 火报成 Chemical
  （会导致错误的物资选择）；物资类型报反（Sand 报 Water）；状态误报
  （没灭报已灭 / 已灭还报在烧）。
- **捷径区分**：弱 worker 的捷径是"抄全局视野模板"——模板抄错或未更新
  （火已灭仍报原强度）就是本维度抓的错误。
- **覆盖范围**：位置信息不独立判（position_fidelity 已移除，真值缺失），
  但 status 类属性更新（extinguished / Grounded）仍在本维度覆盖内。

## 3. Oracle 标注

`fact`——真值可查：该 step 环境全局视野描述（"EnglandFire with average
intensity of Low of Chemical type"、"ReservoirOmaha containing Sand"）。
属性比对需归一（见下）。

## 4. 评分规则（比例分数）

分数 = 属性正确的 claim 数 / 可判定的 claim 数，`[0,1]`。

每条 claim 的判定：
- **supported**：claim 断言的全部属性，归一后与真值描述一致；
- **unsupported**：至少一个断言属性归一后与真值矛盾（如真值 Low 报
  Medium、真值 Sand 报 Water、真值 Non-chemical 报 Chemical）；
- **不计入分母/分子**：无属性断言的 claim（attributes 为空且 note 无
  属性内容）、`object_type: "status"` 状态更新、name=null、非 claim 结构。

归一规则：
- intensity：大小写归一（Low/low → low）；"None"/"none" 视为已熄灭信号
  （火 intensity=None 是环境合法状态，不是幻觉）；
- 火类型：Chemical / Non-chemical（含 "Nonchemical" 变体）；
- 物资类型：`SUPPLY_TYPE_MAP`（SAND/WATER/A/B → Sand/Water，见
  `graders/constraint.py`）；
- status：extinguished / Grounded / carrying 等词表归一；
- 真值侧属性从全局视野描述文本提取（"average intensity of X of Y type"
  与 "containing Z" 两种模式）。

真值侧注意：全局视野描述是 step 级的静态快照——"已熄灭"等动态状态更新
（step 8/11 的 extinguished 报告）与快照不一致时，需要 `query_semantic_map`
的溯源确认时序后再判，不得直接按快照判 unsupported。

## 5. 证据需求

- bundle 内：该样本的 claim 列表（attributes / note）；
- 真值侧：该 step 的 trajectory Observations 全局视野描述；
- 检索工具：`query_semantic_map`（动态状态溯源的时序证据）；
- 引用格式：`<file>:L<logical_line> sha256=<digest>`。

## 6. Abstain 条件

- 样本无带属性断言的 claim → **abstain**；
- 全局视野描述缺失且 semantic map 无该对象的属性数据；
- 属性真值存在时序歧义且无法用检索证据裁决（按第 4 段"真值侧注意"）。
