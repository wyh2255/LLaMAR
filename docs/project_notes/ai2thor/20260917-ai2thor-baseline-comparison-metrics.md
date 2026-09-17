# 2026-09-17 AI2Thor 与论文 baseline 对比口径对齐 + 三项立项

> 背景：用户提出核心质疑——"要和 baseline 算法对比，如果我们（判定）更严，对比就没意义"。编排侧实读原版 `AI2Thor/` 代码与我们 `ai2thor_orch/` 后给出口径对照与差距分析，用户拍板：**三项均立项**。

## 一、背景

- 09-15 RP3 + watcher 重跑（120 步，seed42）：transport_rate 0.59（13/22）、动作成功率 88%、零超时，但 `verified_completion=False`、`coverage` 恒 0.0。
- 编排侧分析产物发现两个新问题：
  1. **Bug C（verifier）**：`_objects_in_fridge` 用裸字符串 `"Fridge"` 做 `parentReceptacles` 列表成员匹配；真机存的是完整 objectId（`Fridge|-02.10|+00.00|+01.07`）→ 永假。fake 剥皮（`fakes.py:354` split("|")[0]）致离线全绿真机必红——与 P0/FA 同类的第三次"假数据偏离真 build"。
  2. **行为残差**：13/22 = OpenObject + Apple/Bread/Tomato 全链；缺的 9 条 = Lettuce×4 + Potato×4 + CloseObject。coordinator step 105 冲刺指令清单幻觉（写了非任务物品 Egg_1、丢了 Lettuce/Potato）。
- 用户目标明确：实验是要和论文 baseline（llamar/coela/smartllm/reAct/CoT/act/llamar_text）对比的。

## 二、讨论要点（口径对照，代码实证）

| 维度 | 原版（AI2Thor/） | 我们（ai2thor_orch） |
|---|---|---|
| 成功判定 | 纯动作证据：`checker.check_success()` = 22/22 子任务记满（`baselines/llamar/llamar.py:173`、`coela.py:147` 直接以其为 finished），**从不查物体终态** | 双层：Tracker 复刻同口径（transport_rate 可比）+ 状态级 verifier（更严，且有 Bug C 恒 False） |
| 终止信号 | check_success() 记满即停 | `ai2thor_barrier.py:832-834` finished 只信 verifier → **22/22 记满也不停不报成功**（Bug C 的真正危害） |
| 导航 | `NavigateTo(obj)` 宏：thortils A*，一次决策跨房间（`env_new.py:508`） | **无 NavigateTo**，只有 move 单格原语——120 步只搬 3/5 件的结构性原因之一 |
| step 口径 | step_num 在导航宏内每个微动作也 +1（`env_new.py:534`） | 1 工具调用 = 1 barrier 回合 |
| 感知 | instance_detections2D ∩ 可交互（`base_env.py:259`） | metadata visible 过滤（P0 后） |

关键判断：**公平性风险是双向的**——判定口径我们更严（对用户不利的一面在对比里吃亏的是我们），动作空间对方有宏动作（我们吃亏）。对比表要成立，主指标必须用论文口径，且导航能级要补齐。

## 三、决策（用户拍板：均立项）

1. **F-done**：run 终止信号挂论文口径——`finished` = tracker 记满 22/22；verifier 降级为审计字段（verified_completion/coverage 保留可读，不进成功判定）。
2. **F-verifier（Bug C）**：verifier 按 objectId 前缀（`split("|",1)[0]`）匹配；fakes 不再剥皮、parentReceptacles 写真机形状；test fixtures 同步 + 回归断言。修复后服务审计口径（"transport 22/22 但 verified <1" = 物理执行损耗，可作为执行质量的补充故事，baseline 亦可补测拉平）。
3. **F-nav**：新增 `navigate` 工具（别名→objectId→坐标→GetReachablePositions→Teleport 面向目标，≤3 候选 fallback，fail-closed；1 调用 = 1 回合）。对齐 baseline 的导航能级。
4. **报告口径**：headline = transport_rate / success（论文口径）；steps 单列并注明粒度差异；verified_completion 作为审计列并列。

## 四、卡图

```
F-done (t_fdb4f0ff) ──────────────┐
F-verifier (t_bea5d578) ─┬────────┤
                         └─→ F-nav (t_ffe8db2e) ─→ R7 (t_5fcfbfa9, 复核+commit+push) ─→ RP4 (t_d33e48d8, a100)
```

| 卡 | id | assignee | 内容 | 状态 |
|---|---|---|---|---|
| F-done | `t_fdb4f0ff` | coder（deepseek-flash pin） | finished=tracker 22/22，verifier 降级审计；end_reason 联动；测试 | ready |
| F-verifier | `t_bea5d578` | coder（同 pin） | Bug C：objectId 前缀匹配 + fakes 去剥皮 + 回归断言 | ready |
| F-nav | `t_ffe8db2e` | coder（同 pin） | navigate 工具 + fake Teleport/GetReachablePositions + 双 prompt | todo（等 B） |
| R7 | `t_5fcfbfa9` | coder（同 pin） | 六闸复核 + 精确路径 commit + push | todo |
| RP4 | `t_d33e48d8` | a100 | 50 步（论文预算）+ 120 步（冲 22/22）双跑 + tar 回传 | todo |

模型沿用 ai2thor 线既定授权（coder 卡 pin `deepseek-flash`/`custom:deepseek_packapi`）。

## 五、后续动作

- [x] R7 通过后 RP4 双跑（09-17 晚，A100 @ac74470）——**193 步内首次全成功**，见下节。
- [ ] RP4 数据回来后：裁决是否补测 baseline 的状态级口径（verified_completion 对 baseline 拉平）；裁决 P2（观测接入 MissionGraph / look 零成本化）是否仍需要。
- [ ] coordinator 清单幻觉（把 Egg 当任务物品、丢 Lettuce/Potato）暂记录不立项——RP4 若复发再立项（届时在 coordinator prompt 钉死任务物品清单）。
- [ ] FC（LLM 客户端无超时悬挂，`src/Agent/{worker,router}_agent/llm/openai_client.py`）仍待用户拍板（涉 src/ 共用层，需 SAR 影响评估）。

## 五之二、RP4 结果（09-17 19:20-19:45，A100 unity @ac74470，编排侧独立核数）

**里程碑：首次 22/22 全成功。** Run2（120 步预算、seed42）：

| 指标 | Run1（50 步预算） | **Run2（成功）** | 历史对照（120 步，09-15 RP3） |
|---|---|---|---|
| end_reason | max_steps_reached | **success** | max_steps_reached |
| transport_rate | 0.227 (5/22) | **1.0 (22/22)** | 0.59 (13/22) |
| verified_completion | False | **True** | False（Bug C 恒 False） |
| coverage（审计） | 0.2 ≠ 0 ✅ | 1.0 | 恒 0（Bug C） |
| action_success_rate | 0.957 | 0.975 | 0.88-0.93 |
| timeout_count | 0 | 0 | 0 |
| steps/elapsed | 50 / 88s | **93 步 / 160s** | 120 / 186s |
| navigate 用量 | 19 调 18 成 | **25 调 25 成（100%）** | 无此工具 |

- 三卡全部真机生效：F-done（success 终止信号）、F-verifier（coverage 0.2→1.0 阶梯，不再恒 0）、F-nav（navigate 25/25）。
- 编排侧独立核数：3 tar sha256 对账 3/3 一致；成功 run summary.json 亲读；attempt1 horizon 原文亲读（44 处报错实证）。
- 证据：`.hermes/kanban/attachments/t_d33e48d8/`（rp4_short50_seed42.tgz `777d871d…` / rp4_long120_seed42.tgz `9559704e…` / attempt1 `cdf51e90…`）+ A100 `/home/user/.WYH/` 落盘。

## 六、S1 小 sweep 结果（09-17 21:15，A100 @d72fd44，6/6 组，编排侧独立核数）

| dir | seed | 预算 | end_reason | transport | completed | succ率 | coverage | navigate |
|---|---|---|---|---|---|---|---|---|
| s1_seed42_s50 | 42 | 50 | max_steps | 0.409 | 9/22 | 0.938 | 0.4 | 15/13 |
| s1_seed42_s120 | 42 | 120 | max_steps | 0.455 | 10/22 | 0.896 | 0.4 | 39/37 |
| s1_seed43_s50 | 43 | 50 | **coordinator_finished_early** | 0.227 | 5/22 | 0.909 | 0.2 | 13/13 |
| s1_seed43_s120 | 43 | 120 | max_steps | 0.045 | 1/22 | 0.880 | 0.0 | 34/32 |
| s1_seed44_s50 | 44 | 50 | max_steps | 0.227 | 5/22 | 0.966 | 0.2 | 8/8 |
| s1_seed44_s120 | 44 | 120 | max_steps | **0.818** | 18/22 | 0.919 | 0.8 | 52/50 |

**组均值**：50 步 transport 0.288（success 0/3）；120 步 0.439（success 0/3）。

**三条结论**：
1. **基础设施层达标**：6/6 无框架失败、无超时、无断连、路由错误 0；FC 修复实证（3 次 SDK 重试自愈、0 Connection error）、FD 修复实证（camera_horizon_out_of_range 正常归类、navigate 161 调 153 成 95%）。
2. **行为层方差极大**：120 步三组 0.455/0.045/0.818；RP4 单跑 1.0（同 seed42）属幸运样本，不可作对照。预算不单调：seed43 加预算反而更差（1/22，12 次 navigation_blocked 卡死）。→ **对比表需 N≥10/条件 + 更大预算（150-200 步）**，单样本对照无统计意义。
3. **新缺陷候选（1/6 出现）**：`coordinator_finished_early`——seed43/50 第 49 回合 coordinator 在 tracker 5/22 时把 dispatch 标 COMPLETED 提前收工（现场保留）。属 coordinator 行为缺陷（与「清单幻觉」同族），待拍板是否立项。

**与基线对比的现实**：当前 50 步 transport 均值 0.288 / 120 步 0.439、success 0/6——对比表还撑不起"追平基线"的结论；先解决方差采样（补种子/补预算）再谈对照。


