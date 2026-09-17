# 2026-09-15 AI2Thor 感知面缺陷诊断与 P0/P1 修复立案

> 背景：用户问「ai2thor_orch 到底出了什么问题、怎么优化」→ 编排侧基于 RP1b 真机回传产物 + 源码完成诊断；用户夜间授权（09-14 深夜）：「晚上就交给你了，worker 随便使用 deepseek-flash，明天如果可以的话可以看到正常成功完成一次实验」→ 按 09-13 委托闸门代行，同标准裁决 + 留档 + 晨起摘要。

## 一、背景

RP1b（`t_51bc5102`，A100 真机 @`9b58372`）三项验收全过后，剩余 13 条失败里 **11 条 `object_not_visible`**——行为层瓶颈被真机数据坐实。编排侧对回传 tar（`.hermes/spec/a100-remote-dev/evidence/a100_rerun_c4c2.tgz`，sha256 `67efb905…`）+ worktree 源码做了根因分析。

## 二、讨论要点（诊断证据链）

1. **感知面撒谎（根因）**：`ai2thor_orch/barrier/ai2thor_barrier.py` `snapshot_public()`（≈583-589）把 Unity metadata 的**全部对象**逐个别名化，未按 `obj["visible"]` 过滤 → `state/context.py`（65-72）渲染成 `Visible objects:`，worker prompt 里出现 **77 个**对象（含 `Floor_1`/`Window_1`/9 个 Cabinet/9 个 Drawer）= 全屋清单冒充视野。`snapshot_coordinator()`（≈634/644）同病。
2. **失败模式吻合**：RP1b 17 次工具调用 13 失败 = 11×`object_not_visible` + 1×`navigation_blocked` + 1×unclassified；失败目标全部取自假清单，且 agent 在清单上挨个换名重试（Bread→Apple→Egg→Tomato）死循环。
3. **动作后零感知增量**：观测只有 `Action RotateRight succeeded.`，探索动作无信息收益。注：Environment State 块每轮由 `worker_state_provider` 重渲染——**修好 visible 过滤后该块天然成为感知增量**，无需另造通道。
4. **探索工具形同虚设**：`look` 仅 up/down 俯仰且占一步，8 步 0 次调用；prompt 说"看不到就 explore"但状态块宣称全可见，策略无从收敛。
5. **离线测试抓不到**：`tests/fakes.py` 假对象全 `visible: True`——滤与不滤在测试里等价，真机才露馅。
6. **编排层欠账（P2，暂缓）**：任务分解无 explore 阶段；coordinator 拿不到"谁在哪看到什么"（AI2Thor 域未接 semantic map 观测通道）。

## 三、决策（编排代行，09-14 夜）

- **P0 修感知面（一卡）**：`snapshot_public`/`snapshot_coordinator` 按 `visible` 过滤；失败文案可行动化（object_not_visible → 提示先搜索）；顺手修 Scene 名空渲染/step 口径；fakes 补 visible=False 样本 + 新增过滤断言测试。
- **P1 策略层（一卡，纯 prompt）**：`prompts/worker/system.md` 写死搜索协议（目标不在 Visible objects → rotate 扫 → move 邻格 → 再查；只在可见时 pickup；禁止同名重试）。`look` 零成本化改造**不在本轮**（涉 barrier 步数语义，留 P2）。
- **R5 复核+提交**：独立复核 P0+P1（diff 实读 + 全量 ai2thor 测试自跑 + fakes 抽查），精确路径 commit + push `wt/p4p5-ai2thor` → `feat/ai2thor-scene-adaptation`。
- **RP2 真机闭环（a100）**：pull 至含 R5 提交 → ① 8 步短跑对照 RP1b（判据：object_not_visible 11→≤2，出现搜索行为）→ ② 完整跑（50 步）冲任务成功（exit=0）；失败则归类分析，可再跑一次；产物 tar 回传 + sha256。
- **P2（编排层观测接入 + look 零成本化）**：暂缓，看 RP2 数据再定。
- 模型：coder 卡一律 pin `deepseek-flash`（`custom:deepseek_packapi`），用户授权「随便用」。

## 四、卡图

```
P0 (coder, 感知面过滤+失败文案)  ─┐
                                  ├─→ R5 (coder, 复核+commit+push) ─→ RP2 (a100, 短跑对照+完整跑冲成功)
P1 (coder, worker prompt 搜索协议) ─┘
```

| 卡 | id | assignee | 内容 | 状态 |
|---|---|---|---|---|
| P0 | `t_e8c9a34c` | coder（deepseek-flash pin） | `snapshot_public/coordinator` visible 过滤 + context.py 文案 + 失败文案行动指引 + Scene 空渲染修 + fakes 补 visible=False 样本与断言 | ✅ done |
| P1 | `t_65c83078` | coder（同 pin） | `prompts/worker/system.md` 搜索协议（不见→rotate/move→重读→仅可见 pickup；禁同名重试；预算意识） | ✅ done |
| R5 | `t_8912bfb7` | coder（同 pin） | 六闸独立复核（改动面/语义/测试自跑/回归抽查/prompt 实读/SAR 零接触）+ 精确路径 commit + push | ✅ done — verdict=PASS，commit **`d2e3e4d`**（10 文件 +371/−27），push ff，三源核验一致（编排侧亲核）；测试 278 passed |
| RP2 | `t_ccde1523` | a100 | pull 至 R5 提交 → ① 8 步短跑对照（判据：object_not_visible 11→≤2、搜索行为出现、Visible 块 ≪77、timeout=0）② 50 步完整跑**冲任务成功**（失败可换 seed 43 再跑一次）→ 双 tar 回传 + sha256 | ✅ done — 短跑四项全过（object_not_visible **11→0**、Visible 块 **77→0–9**、timeout+STALE=0、搜索行为 87.5%）；完整跑 2 次均 max_steps（transport 0.136/0.182 vs 基线 0.045）→ 抓到 Bug A/B 原始报文，如实报告（见第五节） |

## 五、RP2 真机结果与第二轮归因（09-15 凌晨）

RP2（`t_ccde1523`）：短跑四项判据**全过**（object_not_visible 11→0、Visible 块 77→0–9、timeout+STALE=0、搜索行为成立）；但**完整跑两次（seed42/43）均 max_steps_reached**（transport 0.136/0.182，基线 0.045——方向性改善但未成功）。worker 如实报告并抓到原始报文，编排侧独立核数（tar sha256 三件全对）+ 代码定位出**两个新根因**：

- **Bug A（put 100% 失败，致命）**：真机 build 的 `PutObject` 合法签名 = `objectId`（目标容器）+forceAction/placeStationary/randomSeed，**无 `receptacleObjectId` 参数**；`unity_controller._map_put_object` 拼的旧形状必被 Unity 拒（6 次复现，报文见 `a100-remote-dev/evidence/rp2_softfail_evidence.txt`）→ transport 任务在此形状下**永远不可能成功**。离线 fake 不校验参数签名故未暴露。
- **Bug B（跨 worker 状态外溢）**：`ai2thor_orch/env_pack.py:360-369` 单槽缓存 worker provider——env_pack 实例全员共享（`assembly.py:441-465`），Bob（idx=1）后建覆盖 Alice，且两 worker 的 `build_session_factory` 都从同一槽读 → **两个 worker 的 Context 渲染都是 Bob 视角**（状态块逐字相同、同标 Agent1；Alice 自述持 Bob 的 Bread → put 按空手软失败）。工具装配逐调用传参未串（动作执行正确，只有感知渲染串）。SAR 无此路径（session factory 返回 None）。

**第二轮卡图**：

```
FA (coder, put 官方签名映射 + fake 签名校验)  ─┐
                                              ├─→ R6 (coder, 复核+commit+push) ─→ RP3 (a100, 真机冲任务成功)
FB (coder, env_pack 单槽→thread-local 隔离)  ─┘
```

| 卡 | id | 内容 | 状态 |
|---|---|---|---|
| FA | `t_42c8179b` | `_map_put_object` 改官方签名（objectId=容器，forceAction 保留）；fakes 补 PutObject 参数白名单校验（旧形状离线必红）；look/done unclassified 顺手静态分析（不强求修） | ✅ done |
| FB | `t_4a0d2d57` | 单槽 → agent_idx 注册表 + 线程局部 ctx 锚点（worker 自证装配窗口跨线程，裸 TLS 不够）；复现测试 6 个；SAR 无影响论证 | ✅ done |
| R6 | `t_41dd8b76` | 六闸复核（含对真机报文核 FA 映射形状 + 旧形状必红实证 + FB 线程模型实读）+ commit + push | ✅ done — PASS，commit **`6c97da7`**（5 文件 +423/−31），push ff 三源一致（编排侧亲核）；测试 288 passed |
| RP3 | `t_5c467f44` | pull → 完整跑冲成功（判据：end_reason 非 max_steps / verified_completion；最多 3 次 seed 42/43/44）+ 专项核验（put 成功率>0、状态外溢消失、timeout/STALE）+ tar 回传 | 🏃 running（run 902；mux 已预热） |

## 六、RP3 结果与代报（09-15 凌晨 02:30-03:00）

**RP3 卡通道故障与代报**：run 902 实际完成了全部工作（seed42/43 两跑 + 取证），但随后 packapi（cf.api.fan）**余额低→3 并发上限 429**，四次 spawn 连环 rc=0 无终态调用（protocol violation）→ 卡面 gave up。编排侧按通知指引：ssh 直查 A100 + 拉 tar 独立核数后，以 `kanban_complete`（run 908）**代报结果**。seed44 跑发现 coordinator LLM 调用悬挂（step_index=4 的 llm_request 后 17+ 分钟无响应，Step 0/50 卡死；实验用 api.deepseek.com 官方端点，与 429 的 packapi 无关）→ 编排侧 kill 并归档证据（**新缺陷候选 FC：LLM 客户端无超时/悬挂检测**）。

**FA/FB 修复真机双实证（编排侧独立核数）**：

| 指标 | RP2 基线（seed42/43） | RP3（seed42/43） |
|---|---|---|
| put 成功率 | **0/6 全败**（receptacleObjectId 被拒） | **1/1、2/2 全成**（Bob s48 put→Fridge_1 ✓） |
| 状态外溢 | 双方块逐字相同、全标 Agent1 | **消失**——Alice 49/49 标 Agent0、Bob 50/50 标 Agent1，位置集不同 |
| transport_rate | 0.136 / 0.182 | **0.364 / 0.409** |
| action_success | 0.656 / 0.853 | **0.883 / 0.926** |
| 失败条数 | 35 / 14 | 11 / 7（navigation_blocked 6/4 + unclassified 5/3） |
| end_reason | max_steps | max_steps（**verified_completion 仍 False**） |

**结论**：三个致命缺陷（感知面撒谎 / put 签名 / 眼睛串台）全部修复且真机实证；任务未完成的剩余瓶颈 = **步数预算**（22 子任务 × 每件约 10 次成功动作 ≈ 110-120 步 vs --max-steps 50）+ 残留 unclassified（look(down)×4 / pickup×2 / rotate(left)×3，无原始报文待复现）。

**编排侧夜跑（绕开故障卡通道）**：A100 上 nohup 直启 **120 步长跑**（seed42、端口 18092/18203、`logs/rp3_long120_seed42`、wall-clock 1800s、stdout `/tmp/rp3_long120_stdout.log`）——预算充足后冲 verified_completion=True。证据：`.hermes/spec/a100-remote-dev/evidence/rp3_{seed42,seed43,seed44_stuck}.tgz` + `rp3_seed44_stdout.log`（sha256 前缀 ab87acc1/ea20aaaa/7c2e4da4/ab91f5b1）。

**03:00-03:16 追加（上游 API 故障 + watcher 接管）**：120 步长跑同样卡死 Step 0（601s+，与 seed44 悬挂同症状）。编排侧在 A100 直接探测 `api.deepseek.com/chat/completions`（真 key、25s 上限）：**probe1 = read timeout 25.3s、probe2 = HTTP 503 Service Temporarily Unavailable**——根因是 **DeepSeek 官方 API 上游服务降级**（非代码回归；02:24-02:31 的 seed42/43 两跑还是好的，02:38 起 packapi 429 与官方 503 同期出现，疑同一波服务波动）。处置：kill 卡死长跑（GPU 已释放）；部署 **`/tmp/rp3_watcher.sh`**（A100 上 setsid nohup，pid 70621）：每 5 分钟双探测，**连续两次 200 OK 才自动重发 120 步跑**（`logs/rp3_long120b_seed42`、stdout `/tmp/rp3_long120b_stdout.log`），跑完自动 summary 摘录 + `tar czf /home/user/.WYH/rp3_long120b.tgz` + sha256 落 `/tmp/rp3_watcher.log`；**deadline 07:00**（到点未恢复则放弃并记录，不无限重试）。晨起核验点：`/tmp/rp3_watcher.log` 尾部 + `rp3_long120b.tgz` 是否在。
- 附带缺陷候选 **FC（LLM 客户端悬挂面）**：coordinator llm_request 悬挂 17+ 分钟无日志无中止——`AsyncOpenAI` 缺省 timeout（600s×retry）在无流式时观感即"卡死"；上游 503/timeout 时 experiment 无快速失败路径。修复方向：client 显式 `timeout` + `max_retries` 收敛（`src/Agent/{worker,router}_agent/llm/openai_client.py:71` 两处构造点），**留待用户拍板后立项**（涉 src/ 共用层，须 SAR 影响评估）。

## 七、后续动作

- [ ] P0/P1 实施 → R5 复核入库 → RP2 真机跑通（晨起验收点：**一次成功完成的实验**或失败归类 + 对照数据）
- [ ] RP2 数据出来后裁决 P2（观测接入 MissionGraph / look 零成本化）是否立项
- [ ] 晨起摘要（板上直接可见结果）
