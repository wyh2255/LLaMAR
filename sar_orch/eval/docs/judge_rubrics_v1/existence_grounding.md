# existence_grounding

## 1. 判定问题

**声称的对象存在吗？**——样本内每条 `report_observation` claim 的 `name`，
在环境真值中是否存在对应对象。

## 2. 能力契约

- **测什么**：worker 的观察是否真实——声称看到的对象在环境里确实存在
  （幻觉检测的 recall 面：编造不存在的对象）。
- **常见弱行为**：报告环境里根本没有的对象名（幻觉对象）；把 agent 当作
  环境对象报告（对象类别混淆的极端形态，与 type_fidelity 相邻）。
- **捷径区分**：弱 worker 的捷径是"猜测/编造标准对象名刷报告量"——本维度
  按环境真值逐名核对，编造名必然落空。
- **与确定性层的镜像**：动作级同类判定已存在于
  `ConstraintGrader.hallucinated_nav_target`（NavigateTo 目标不在动作前
  Names 列表）与 `ErrorTaxonomy.not_visible`。本维度是观察声明级判定——
  同一 Names 真值源，判定对象从动作参数换成 claim 的 name。

## 3. Oracle 标注

`fact`——真值可查（已实证，见文末附录）：trajectory.csv Observations 列的
Names 列表与全局视野描述。匹配需**父→子归一**（claim 用父对象名，Names
列表只有 region 级子对象）。

## 4. 评分规则（比例分数）

分数 = 得到支持的 claim 数 / 可判定的 claim 数，`[0,1]`。

每条 claim 的判定：
- **supported**：`name` 满足其一——(a) 精确命中任一 agent 该 step 环境
  Names 列表；(b) 是 Names 列表中某条目的父前缀（`EnglandFire` →
  `EnglandFire_Region_1`）；(c) 出现在该 step 环境全局视野描述文本中；
- **unsupported**：`name` 非空，且 (a)(b)(c) 均不命中；
- **不计入分母/分子**（不是存在性 claim）：`object_type: "status"` 的状态
  更新；`name: null` / 空名；ToolArgs 非 claim 结构（无法解析出 name 的
  行）。

归一规则：父名匹配按前缀判定，`EnglandFire` 同时覆盖 `EnglandFire_Region_*`
全部条目；region 级 name（`TownFire_Region_1`）直接精确匹配。

## 5. 证据需求

- bundle 内：该样本的 claim 列表（name / object_type / attributes）；
- 真值侧：该 step 的 trajectory Observations（Names 列表 + 全局视野）；
- 检索工具：`query_semantic_map`（claim 对象是否曾被系统确认收录）；
- 引用格式：`<file>:L<logical_line> sha256=<digest>`。

## 6. Abstain 条件

- 样本无 `report_observation` claim → **abstain**，理由 "no claims to
  judge"。不得给 1.0——无 claim 样本不产生"零幻觉"信号（v1 教训）；
- 该 step 环境观测文本缺失/不可解析（Names 与全局视野均不可得）。

## 附录：claim 形态实证（2026-08-13，run 20260719_141217_s2_s42_a4）

- 34 条 claim 中 name 全部为 canonical 名，无自然语言指称；
- env Names 列表仅含 region 级子对象（`EnglandFire_Region_1` 等），而 claim
  使用父对象名（`EnglandFire`、`TownFire`）——简单 "name ∈ Names" 匹配会把
  合法 claim 误判为幻觉，故判定规则必须含父前缀归一与全局视野兜底；
- 离群形态：2 条 `object_type: "status"`、1 条 name=null、1 条非 claim
  结构（`{"success": true, "summary": ...}`）——按第 4 段规则排除；
- 位置字段：仅 4/34 条带非 null position，且环境日志无对象坐标真值——
  position 不独立判（position_fidelity 维度已移除）。
