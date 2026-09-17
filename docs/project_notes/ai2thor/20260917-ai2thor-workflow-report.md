# AI2Thor 适配 workflow 报告（2026-09-17）

> 范围：09-17 单次会话内完成的「诊断 → 立项 → 实施 → 复核 → 真机验证 → 小 sweep」完整闭环。
> 仓库状态：`feat/ai2thor-scene-adaptation` @ `d72fd44`（local == origin，专属 worktree `/home/wyh/daily_work/LLaMAR-ai2thor`）。
> 详细决策留痕：`docs/project_notes/ai2thor/20260917-ai2thor-baseline-comparison-metrics.md`（本报告姊妹篇，同分支入库）。

## 一、一句话结论

**基础设施层达到真机可用状态（6/6 组零框架故障）；首次拿到 22/22 全成功 run（RP4）；但 S1 小 sweep 揭示行为层方差极大（success 0/6、transport 0.045–0.818），与论文 baseline 的正式对照需要 N≥10/条件 + 更大预算。**

## 二、起点（09-17 下午进场时的状态）

- G0–G5 实现完成、fake 模式全绿；09-14/15 已完成 RP1b–RP3 三轮真机验证，修复感知面（P0）、搜索协议（P1）、put 签名（FA）、视角串台（FB）。
- 遗留：09-15 凌晨 watcher 自动重发的 120 步跑（rp3_long120b）结果未分析；待拍板项 FC（LLM 悬挂）/P2。

## 三、本会话时间线

| 时刻 | 事件 |
|---|---|
| ~15:00 | 编排侧分析 watcher 产物：**RP3 120 步 = transport 0.59，但仍未成功**；归因出 Bug C（verifier parentReceptacles 裸名匹配真机 objectId 恒 False）+ 行为残差（coordinator 清单幻觉把非任务物品 Egg 写进冲刺指令） |
| ~16:00 | 与原版 `AI2Thor/` 代码逐文件对照，确认对比口径：baseline 成功=纯动作证据 22/22（`checker.check_success()`），我们额外加了更严的状态级 verifier；发现 NavigateTo 宏动作缺失是结构性差距 |
| 16:30 | 用户拍板「均立项」：F-done（终止信号挂论文口径）+ F-verifier（Bug C）+ F-nav（navigate 工具） |
| 17:58–19:07 | 三卡并行/串行实施完成（F-nav 撞满 200 轮预算但交付已落，超时事件为记账噪音） |
| 19:08–19:19 | R7 六闸复核 + 提交 `ac74470`（22 文件 +1506/-145），顺手修 exit code 残留；发现文档锚点漂移另开两卡（t_8f547f9a/t_ab3249f5，均完成推送 `9896335`/`bafff13`） |
| 19:20–19:45 | **RP4 真机双跑**：Run2（120 步预算）**93 步达成 22/22、end_reason=success、verified_completion=True**（首次全成功）；暴露 FD（horizon 越界）+ FC 新证据（断连即死） |
| 19:44–20:23 | 用户拍板 FC 立项；FD（t_d03829a1）与 FC（t_a8b5239a→R8 t_572c8e8a）相继修复推送（`18c7d85`/`b9d98e5`/`d72fd44`）；R8 顺手修掉 env 变量 nan/inf 静默透传 |
| 20:40–21:15 | **S1 小 sweep**（3 seeds × {50,120} 步 = 6 组）全干净跑完 |
| 21:30 | 分支收口：专属 worktree 建立 + 本报告 |

## 四、技术变更清单（本会话推送到 `feat/ai2thor-scene-adaptation` 的全部提交）

| commit | 内容 | 真机归因 |
|---|---|---|
| `ac74470` | F-done：finished=tracker 22/22（论文口径），verifier 降级审计；F-verifier：Bug C objectId 前缀匹配 + fakes 去剥皮；F-nav：navigate 工具（Teleport 到目标旁可达点，≤3 候选 fallback） | RP3 |
| `9896335`/`bafff13` | env_contract 锚点重推；architecture §2 补录 | R7 复核发现 |
| `18c7d85`+`b9d98e5` | FD：Teleport 缺省 horizon 注入夹取值 [-29.9°,59.9°] + camera_horizon_out_of_range 域错误类 + fake 复现真机 horizon 语义 | RP4 attempt1 |
| `d72fd44` | FC：AsyncOpenAI 两构造点显式 timeout=240s/max_retries=2（env 可覆盖）；R8 顺手修 nan/inf 静默透传 | RP3 悬挂 + RP4 断连 |

## 五、实验结果

### RP4（09-17 19:20，A100 unity @ac74470）——里程碑

| 指标 | Run1（50 步） | **Run2（成功）** |
|---|---|---|
| end_reason | max_steps_reached | **success** |
| transport_rate | 0.227 | **1.0（22/22）** |
| verified_completion | False | **True** |
| coverage（审计） | 0.2 ≠ 0 | 1.0 |
| 动作成功率 / 超时 | 0.957 / 0 | 0.975 / 0 |
| navigate | 19 调 18 成 | 25 调 25 成 |
| 步数 / 耗时 | 50 / 88s | **93 / 160s** |

### S1 小 sweep（09-17 20:40，@d72fd44，6/6 组）

| seed | 50 步 transport | 120 步 transport | coverage@120 | 备注 |
|---|---|---|---|---|
| 42 | 0.409 | 0.455 | 0.4 | 预算增长几乎不收益 |
| 43 | 0.227 | 0.045 | 0.0 | 50 步组 coordinator 提前收官；120 步组卡死（12×navigation_blocked） |
| 44 | 0.227 | **0.818** | 0.8 | 正常 scaling |

- 组均值：50 步 0.288 / 120 步 0.439；success 0/6。
- 基础设施：6/6 无框架失败、无超时、无断连、路由错误 0；FC/FD 修复真机实证（3 次 SDK 重试自愈；horizon 异常正常归类 21 次）。
- **核心发现：RP4 的 1.0 是分布上尾的幸运样本；行为层方差极大（120 步 0.045–0.818），预算非单调。**

## 六、缺陷台账（累计 8 个真机/框架缺陷，全部归因修复）

| 编号 | 缺陷 | 状态 |
|---|---|---|
| P0 | 感知面撒谎（77 对象冒充可见） | ✅ 09-14 |
| P1 | worker 无搜索协议 | ✅ 09-14 |
| FA | PutObject 参数签名错误（receptacleObjectId 不存在于真机 build） | ✅ 09-15 |
| FB | 跨 worker 状态外溢（单槽 provider） | ✅ 09-15 |
| Bug C | verifier parentReceptacles 裸名匹配 | ✅ 09-17（RP4 实证 coverage 读数恢复） |
| FD | horizon 浮点残差击穿 teleportFull 校验 | ✅ 09-17 |
| FC | LLM 客户端无超时（悬挂 17min）/断连即死 | ✅ 09-17（timeout 收敛；**run 级降级/续跑未做，记录为已知边界**） |
| — | coordinator 提前收官（coordinator_finished_early，1/6） | ⏳ 待拍板 |

**共性教训**：8 个里 5 个（P0/FA/Bug C/FD 同类 + RP1b 的 object_not_visible）是「离线 fake 数据偏离真机 build」家族——fake 形状已逐项对齐真机（visible=False 样本、参数白名单、objectId 全形状、horizon 残差语义）。

## 七、与论文 baseline 对比的现状

- 口径已对齐：主指标 = transport/success（动作证据，同 `checker.py`）；verified/coverage 为状态级审计列（我们更严的附加口径）。
- 动作空间已对齐：navigate 宏 ≈ baseline NavigateTo（粒度差异：我们 1 步=1 决策，baseline 宏内微动作也计步——报告时需脚注说明）。
- **差距**：当前 success 0/6、transport 均值 0.288/0.439，撑不起对照结论；需要 N≥10/条件 + 150-200 步预算的稳定分布。

## 八、待拍板项

1. 下一轮采样规模：建议 8-10 seeds × 150 步（约 30-40 分钟/轮）。
2. coordinator 提前收官（1/6）是否立项修。
3. P2（观测接入 MissionGraph / look 零成本化）——S1 数据后升级为「可能是方差来源之一」，是否立项。
4. VLM 方向（用户已问）：轻量版（关键帧导出，小卡）vs VLM agent（需模型选型拍板 + 观测/消息通道改造）。

## 九、证据索引

- RP4：`.hermes/kanban/attachments/t_d33e48d8/`（sha256：`9559704e…` 成功 run / `777d871d…` / `cdf51e90…` attempt1）
- S1：`.hermes/kanban/attachments/t_fd87e45b/`（sha256：`1edb8ac7…`，6 组 run + watch/stdout 日志 + README）
- RP3 及更早：`/home/wyh/daily_work/LLaMAR/.hermes/spec/a100-remote-dev/evidence/`
- A100 落盘：`/home/user/.WYH/`（rp4_* / s1_*）
