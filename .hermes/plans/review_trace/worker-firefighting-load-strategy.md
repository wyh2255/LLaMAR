# Worker 灭火装载策略探索：取 1 单位就跑（灭火效率主因之一）

> **状态**：探索完成（subagent 分析，未实施修复）
> **日期**：2026-08-12
> **关联**：`fix-validation-30steps.md`（30 步验证中 person 未救起，火蔓延 11 区域）
> **数据源**：30 步 run `20260812_225834_s2_s42_a2` + 20 步 run `memory_acceptance_ff6994c_20260809_032204/scene_2_agents_2`

## 1. 结论

**"worker 每次只取 1 单位就跑"在 30 步 run 中部分成立且显著存在；是灭火慢的主因之一，但蔓延失控是多因素叠加。** 20 步 run 的 worker 恰好选择更高装载，对照量化了差距。

## 2. 证据

**Prompt 语义缺陷**（`sar_orch/prompts/worker/system.md`，两 run 同一版本）：
- L10-11：`Reservoirs: Infinite supply, 1 unit per collect call`
- Firefighting Pattern 第 3 步：`Call get_supply() repeatedly to collect enough units` —— **从未要求装满 3 槽**
- 工具 description（`tools/worker/get_supply.py`）：`Collect firefighting supplies from a reservoir or deposit.` —— 无数量语义、无数量参数

**30 步 run 取水分布**：Alice 连续 get_supply `[1,1,3,2]`、Bob `[2,2,1,3]` —— 8 次取水中 **3 次只取 1 单位**，无一次"装满 3 后一次用光"。

| 往返 | 典型低效 | 代价 |
|---|---|---|
| Alice Trip1（S7-10） | 取 1 Sand → 灭火 1 次 | **4 步换 1 档**（EnglandFire_R1 Medium→Low） |
| Alice Trip2（S11-14） | 再取 1 Sand → 灭火 1 次 | 再 4 步换 1 档（本该一次取 2，6 步完成） |
| Alice Trip3（S18-25） | 取 3 → 去**已灭**的 TF_R1 | 2 次 use_supply 浪费 |
| Bob Trip1（S6-10） | 取 2 → 只用 1 | 5 步换 1 档 |

**LLM 意图文本实锤**：Bob S8 "I have 1 unit... let me collect more"（计划 3 实际只取 2 只用 1）；Alice S11 "inventory is now empty... return to ReservoirOmaha"（EnglandFire 只需 2 单位 Sand，分两次往返各取 1 = 8 步 vs 一次取 2 = 6 步）。

## 3. 蔓延失控 = 多因素叠加

1. **低载往返**（主因之一）：取 1-2 单位往返，4-5 步只降 1 档 intensity，火 medium→high 只要 3 步 + 蔓延；
2. **目标陈旧/去错 region**：Alice S22 去已灭的 TF_R1（语义记忆/状态陈旧，memory 系统排查线索）；
3. **coordinator 指令晚到**：开局只发 explore，首个灭火任务 S5-7 才到（浪费 4-5 步）；
4. 20 步 run 对照组：worker 恰好高装载 + 目标正确 → 20 步灭 6 区域成功。

## 4. 修复建议（未实施，按优先级）

1. **worker prompt 强化**：Firefighting Pattern 明确"每次往返装满 3 单位（连续 get_supply 3 次）后再前往火区；一次往返内把携带量用完再回 reservoir"；
2. **get_supply 工具支持数量参数**（如 `units=3`，缺省 1 保持兼容），减少 LLM 连续调用次数与出错面；
3. **coordinator 指令明确化**：assignment 模板加"装满 3 单位 X 后前往 Region_Y"；explore 与首个灭火任务并行下发（当前 S0 只发 explore）；蔓延 region 数 > worker 数时给出"全载灭火、禁止低载往返"优先级指令；
4. 关注点：worker 去错 region 的语义记忆陈旧问题（memory 系统排查线索）。

## 5. 证据文件

- `sar_orch/results/20260812_225834_s2_s42_a2/agent_interactions.csv`、`workers/*/*/*.ndjson`
- `sar_orch/results/memory_acceptance_ff6994c_20260809_032204/scene_2_agents_2/agent_interactions.csv`
- `sar_orch/prompts/worker/system.md`、`sar_orch/tools/worker/get_supply.py`
