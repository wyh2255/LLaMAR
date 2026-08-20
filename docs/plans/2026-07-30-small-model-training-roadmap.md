---
日期: 2026-07-30
文档类型: 技术方案 / 路线图
文档概述: 把 LLaMAR 多智能体 SAR 轨迹用于训练小参数模型的分阶段路线图。含代码核实结论、门槛判据、风险排序与文献依据。经 subagent 评审后修订，并按第二轮全量代码核查更正了 3 处实测断言。
---

# 小参数模型训练路线图（SAR 多智能体）

## 0. 这份文档解决什么问题

目标：用多智能体 SAR 系统产生的轨迹，训练一个小参数模型（1.5B–8B）替代当前的托管 API。

结论先行：

1. **数据管道够用**，全保真日志已存在，但当前这批数据不足以训练。
2. **teacher 选错了** —— 82 个 run 全是 `deepseek-v4-flash`，成功率 7/72。不能用来蒸馏。
3. **RL vs SFT 是伪二分法** —— 需要的不是「SFT 蒸馏能力」，而是「minimal SFT 稳格式 + RL 提能力」，或用 ICRL 完全绕开参数级 SFT。
4. **最高性价比的一步跟训练无关**：把工具数从 20–22 砍到 6–10。
5. **整条路线最大的隐患是评估** —— 现有 83 个 run 的 seed 100% 是 42，任何「成功率提升」都可能是过拟合到单点的假象。

---

## 1. 代码核实结论（写方案前实测，非纸面推断）

### 1.1 数据源

| 事实 | 核实结果 |
|---|---|
| `sar_orch/results/*/workers/<Agent>/<Agent>/*.ndjson` 全保真 | **部分成立**。`llm_request` 的 `messages` 在单条请求内未截断（system 消息实测 12765–14273 字符），`llm_response` 含 `content`/`tool_calls`/`finish_reason`/`usage`。但有三处保真度缺口，见 1.1a |
| `agent_interactions.csv` 的 `LLMInput` 被截断 | **成立**。`sar_orch/worker.py:357-361` 只取 `msgs[-6:]`、每条 `c[:200]`。**不可用于训练** |
| 数据规模 | 98 个 run 目录，860 个 worker episode 文件，6189 条 `llm_request`，365MB |
| `finished=true` 的 run | **7 / 72**（有 `run_metrics.json` 的） |
| `transport_rate ≥ 0.8` 但未完成 | 10 个 |

### 1.1a 保真度的三处缺口（第二轮核查新增，直接影响 exporter 设计）

**(1) `tools` 字段只有工具名，没有 schema。** `src/Agent/worker_agent/logger.py:111` 是 `entry["tools"] = [tool.name for tool in tools]`，实测 ndjson 里就是 `["navigate_to","move",...]`。而工具描述文本又是运行时由 `src/Agent/worker_agent/build.py` 拼接的（见 1.2）。**结论：历史轨迹的工具 schema 无法从日志精确复原，只能按当前代码近似重建。** 这也意味着工具集一变更，旧轨迹的 system prompt 就整体过时 —— 强化了「先砍工具再导数据」的顺序要求（见第 7 节）。

**(2) 上下文窗口剪枝始终在生效。** 1.4 节论证 Phase 3 LLM 摘要从未触发，这条成立。但漏掉了 `src/Agent/worker_agent/context.py:38` 的 `recent_messages=12` 滑动窗口剪枝（`prune_history`，`context.py:627-680`）—— 它**一直在工作**。实测 `messages` 数量硬顶在 15，而单 episode 最多有 46 个 assistant turn，**44.6% 的 episode** 在日志里已丢失早期轮次的原始 message。所以阶段 0 的「一个 episode 输出一条多轮样本」**不能从最后一条请求还原前缀**，必须跨 `llm_request` / `tool_result` 事件重建并集（`system + 首个 user + 全部 assistant + 全部 tool_result`）。

**(3) `finish_reason` 恒为 `"stop"`。** `src/Agent/router_agent/llm/openai_client.py:292` 硬编码。该字段虽然存在但无信息量，**无法用它识别被模型截断的样本**。

### 1.1b 实测 token 长度分布（第二轮核查新增）

全量普查 860 个 worker episode 文件（不是抽样）。token 数由 5,817 组 `(字符数, usage.prompt_tokens)` 做 OLS + 同 episode 相邻请求差分交叉验证标定得出 **3.21 字符/token + 约 1,100 tokens 工具 schema 常量**（差分法消掉截距后中位 3.225，两法一致；注意 cl100k_base 直接算是 4.3 字符/token，偏离实测，因为 DeepSeek tokenizer 不同）。

| 指标 | p50 | p90 | p99 | max |
|---|---|---|---|---|
| 整条 episode 样本 | **8,121** | **11,568** | 18,517 | 32,137 |
| 其中进 loss 的 assistant 部分 | **1,720** | 3,880 | 8,623 | 15,193 |
| assistant turn 数 | 5 | 13 | 26 | 46 |
| tool_result 数 | 9 | 20 | 43 | 75 |

监督比例中位 **21.1%**，system prompt 单独占 3,977 tokens。856 条 episode **总监督 token 仅 181 万** —— 这个量级独立印证了阶段 0「几百条样本、排除蒸馏路线」的判断。

`max_seq_len` 覆盖率：8,192 → 51.4% / **12,288 → 93.7%** / 16,384 → 98.0% / 32,768 → 100%。

**注意**：以上基于 DeepSeek tokenizer 的 usage 标定，换 Qwen3 tokenizer（词表 151,936）后需重测，可能 ±15% 偏差。显存与配置推导见配套文档《LLM 训练知识补齐清单》第零节与知识地图 §5。

### 1.2 我先前说错的三处

**工具数不是固定 22。** 实测分布（第二轮全量普查更正）：**20 工具 × 4,860 次请求，22 工具 × 1,329 次请求**（此前写的 732 / 128 疑似只统计了单个 run，比例结论成立、绝对数字不成立）。差异来自 `sar_orch/worker.py:186-195` —— `enable_peer_mail=False` 时 `ReadMailboxTool`/`A2ASendMailTool` 不装配。**exporter 和白名单逻辑必须按请求动态判断工具集，不能硬编码 22；但注意工具 schema 要从代码重建，日志里只有名字（见 1.1a）。**

**system prompt 的 14.3k 不在文件里。** 实测：

| 位置 | 字符数 |
|---|---|
| `sar_orch/prompts/worker/system.md`（文件） | 8,497 |
| 运行时实际 system 消息 | 中位 12,765 / 最大 14,273 |
| `sar_orch/prompts/coordinator/system.md` | 13,607 |
| `sar_orch/prompts/coordinator/system.semantic.md` | 16,288 |

差值约 4,300–5,800 字符来自 `src/Agent/worker_agent/build.py` 运行时拼接的 `_tool_descriptions_text()` + `skill_loader.get_skills_metadata_prompt()`。

**这个发现改变了阶段 1 的设计**：prompt 里最大的可压缩块是自动生成的工具描述文本，它随工具数线性增长。所以「工具窄化」和「prompt 精简」不是两件并列的事 —— 砍工具会自动把 prompt 压下来。顺序必须是先砍工具、再测量剩余可压缩空间。

**单 episode 不是 570 秒。** 实测 72 个 `run_metrics.json`：中位 **301s**、均值 336s、最大 1155s。570 是我从单个 run 里取的，不具代表性。方差很大（3.8 倍），做墙钟估算要用区间而非点值。

### 1.3 数据分布（比预期更严重）

83 份 `metadata.json` 统计：

| 维度 | 分布 |
|---|---|
| seed | **42 × 83（100%，无例外）** |
| scene | 1:42, 2:11, 3:12, 4:9, 5:9 |
| agent_count | 2:44, 3:23, 4:15, 5:1, **6:0** |
| model | deepseek-v4-flash × 82, deepseek-chat × 1 |

scene 1 占一半，5-agent 只跑过 1 次，6-agent 从未跑过。7 次成功里大部分是 scene 1。**这意味着 7/72 这个成功率本身是在最有利的单点条件下测出来的，跨场景真实成功率可能更低。**

### 1.4 上下文压缩没有污染数据（评审此处判断有误）

评审担心 `src/Agent/worker_agent/context.py` 的 Phase 3 LLM 摘要压缩会让训练数据混入「摘要重写过的轮次」。实测：

- 阈值：`token_limit=80000` × `summary_trigger_ratio=0.8` = **64,000 tokens**
- worker 实际峰值 `prompt_tokens`：**10,592**
- coordinator 实际峰值：**58,071**（全局最大，仍低于阈值）

**Phase 3 从未触发过**（实测 `prompt_tokens ≥ 64000` 的请求 0 / 5,817）。现有数据不存在这个污染。但 exporter 里应该加一条断言检查（未来 episode 变长时会触发），而不是当作已解决。

**但这一节漏了另一种压缩，它一直在生效** —— `context.py:38` 的 `recent_messages=12` 滑动窗口剪枝影响 44.6% 的 episode。详见 1.1a(2)。这是本节此前的实质遗漏：只查了 LLM 摘要压缩，没查 count-based 剪枝。

### 1.5 evolution/ 管线的既有 bug

`evolution/orchestrator.py:load_metrics()` 把 `summary.csv` 的表头原样读进 dict，实际表头是 `FinalCoverage` / `FinalTransportRate` / `Finished`（驼峰）。但 `pretty_metrics()`（第 88 行）和 `compare_generations()`（第 199 行）查的 key 是 `success_rate` / `finished` / `transport_rate` / `coverage`（小写，且 `success_rate` 这个字段根本不存在）。

`metrics.get(key)` 返回 None → 被 `if p is not None` 跳过 → **不报错，静默打印空结果**。这条链路很可能从第 1 代起就没生效过。

**第二轮核查已实测复现且确认至今未修**：`pretty_metrics` 全部 key 未命中、回落到 `str(metrics)`；`compare_generations` 匹配到的 key 列表为空 `[]`。`run_metrics.json` 的实际 key 是 `coverage`/`transport_rate`/`steps`/`finished`/`elapsed_seconds`/`end_reason`/`run_id`/`max_steps`/`log_dir` —— 确实没有 `success_rate`。

**顺带发现一个同文件的第二个 bug**：`load_metrics()` 的 `for row in reader` 循环让后一行覆盖前一行，最终只保留 `summary.csv` **最后一行**的指标。多 episode 的 summary 会静默丢数据 —— 修 key 映射时要一起改成聚合而非覆盖。

另一个问题：10 代进化全部以同一个 `sar_experiment_20260705_171652`（scene1 / seed42 / 2agent）为分析源，`--target` 默认 `coordinator`。**这是在单点上做了 10 轮过拟合，且没有任何数字反馈能告诉你有没有变好。**

结论：「复用 evolution/ 管线」这个说法过于乐观。它没有工具选择的输出通道（`evolver.py` 只解析 `Improved Prompt for Coordinator/Worker` 文本段），实际是「参照其 orchestrator 结构重写」。**且在复用前必须先修 metric key bug，否则所有阶段的判据都读不到数。**

### 1.6 并行可行性（评审纠正了我的关注点）

`SAR/core.py:219` 的 `GPS` 类用类属性做 tracker，是进程内单例。但 `sar_orch/benchmark.py:325` 是 `asyncio.create_subprocess_exec` **每 episode 起独立子进程**，配 `asyncio.Semaphore(concurrency)` 和动态端口块扫描（`_find_free_blocks`）。**GPS 单例在进程级并行下不构成阻塞。**

真正未验证的是：
- 端口分配机制只验证到 `concurrency=2`
- `sar_orch/results/benchmark/` 下 20 条记录里 2 条 timeout、1 条 failed、17 条 pending —— **从未跑完过一次完整 benchmark**
- 已完成的失败记录原因是 `LLM 调用在 4 次重试后失败: Request timed out.` —— **API 侧限流/超时，与并行机制无关**

这条最后一点很关键：**并发瓶颈可能根本不在你的代码里，而在 API 配额。** concurrency=2 就已经出现超时。

---

## 2. 路线图

五个阶段，每阶段有准入门槛和放弃条件。

### 阶段 -1 — 修 bug + 建评估集（1 天）★ 前置，不可跳过

这一步是评审指出的最大遗漏，插在所有事情之前。

1. **修 `evolution/orchestrator.py` 的 metric key 映射**（`FinalCoverage`→`coverage` 等）。不修的话后面所有「成功率提升了多少」都读不出数。
2. **划 held-out 评估集**：从 scene 1–5 × agents 2–6 × seed 池里显式留出一批组合，**这批组合此后禁止出现在任何 prompt 进化 / SFT / RL 训练数据里**。建议至少覆盖：一个未见过的 seed、agents=5 和 6（现在几乎没数据）、scene 4/5（现在各 9 次）。
3. **确立 baseline**：在评估集上跑一遍当前配置，记下成功率 / coverage / transport_rate。这是此后一切对比的锚点。
4. **评估指标用 pass^k 而非 pass@k**：同一组合跑 k 次（k=5 起），看**全部成功的比例**。多轮协作任务单次成功可能靠运气；τ-bench 原论文报告 gpt-4o 在 retail 域的 pass^8 掉到 <25%。（注：此前本行写「从 pass^1 的 >60%」，但 arXiv:2406.12045 摘要给的是「成功率 <50%」，`>60%` 对不上出处，已删除该数字；结论方向不受影响。）
5. **评估时锁死采样参数**并记录进 baseline 元数据。同一 checkpoint 在不同 temperature 下的 tool_call 合法率能差十几个点，不锁参数后续所有对比都不可比。

**门槛**：能在 held-out 集上产出一个可复现的 baseline 数字。

### 阶段 0 — Exporter（3–5 天，此前估 1–2 天偏低）

唯一的基础设施投入，SFT / RL 共用。写 `scripts/export_trajectories.py`：

- 输入 `sar_orch/results/*/workers/<Agent>/<Agent>/*.ndjson`（唯一全保真源）。**注意按 `workers/<A>/<A>/` 双层过滤** —— 同名目录的上一层还有 `mailbox.ndjson` 等无 `event` 字段的文件，会混进 14 行无效记录
- **不要碰** `agent_interactions.csv`（见 1.1）
- 一个 episode 输出**一条**多轮样本，对每个 assistant turn 打 loss mask，tool_result 不算 loss。不要按 turn 切成 N 条重复编码前缀
- **必须跨 `llm_request` / `tool_result` 事件重建并集**（`system + 首个 user + 全部 assistant + 全部 tool_result`），不能只读最后一条请求 —— 44.6% 的 episode 已被 `recent_messages=12` 剪枝（见 1.1a(2)）
- 工具集按请求动态判断（20 或 22，见 1.2），但**工具 schema 必须从当前代码重建** —— 日志的 `tools` 字段只有名字（见 1.1a(1)）
- 断言 `prompt_tokens < 64000`，防止未来的 Phase 3 压缩污染（见 1.4）
- **断言 `assistant_mask.sum() > 0`**，为 0 直接报错退出。配合先跑序列长度直方图再定 `max_length` —— TRL `SFTConfig.max_length` 默认 1024，而样本中位 8,121 tokens，按默认跑会把 assistant 目标整段截掉且不报错（见配套文档知识地图 §2）
- join 时注意 key 不一致：`trajectory.csv` / `subtasks.csv` 用 `RunID`（驼峰），`run_metrics.json` 用 `run_id`（下划线）；且 `subtasks.csv` 是**事件驱动稀疏记录**（只在派发/完成时写行），不是每 step 一行，需要显式处理粒度不匹配
- 记录 tokenizer 版本 + chat_template hash 到输出元数据

**关于 `Successes` 字段的语义澄清（评审指出我的内部不一致）**：链路是 `sar_orch/experiment.py:408` → `barrier.py:445` → `SAR/env.py:step()` 的 `event['success']`。它表示「动作是否被环境合法执行」（导航是否到达、拾取是否成功），**不是对团队目标的贡献**。我在阶段 0 说要拿它当「步级质量标签」、又在阶段 5 说「action success ≠ team contribution」，这是矛盾的。正确做法：阶段 0 只把它当**动作合法性标签**（用于过滤格式/参数错误的样本），团队贡献度必须靠 `subtasks.csv` + 规则修正项另算。

**去重要求**：同一条 run 会被多个 worker 记录（860 个 worker episode 文件 vs 98 个 run 目录），且同一段 system prompt 在 6,189 条请求里几乎逐字重复。按 `(run_id, agent, task_id)` 去重 + 按 assistant 输出做哈希/MinHash 去重，去重前后条数都记进元数据。

**门槛**：拿到「去重后有效样本数」。预估几百条 —— **1.1b 的实测已确认这一点**：856 条 episode 的总监督 token 只有 181 万，约等于一个 8B 模型几分钟的训练吞吐。**SFT 蒸馏路线当前排除。**

### 阶段 1 — 工具窄化（3–5 天）★ 最高优先级

零训练成本，且结果决定后面一切。**注意顺序：先砍工具，prompt 长度会跟着自动降。**

1. 按 agent 角色 / 子任务阶段做工具白名单：导航阶段不暴露灭火/救人工具，灭火子任务不暴露 mailbox。20–22 → 6–10。
   改动点集中在 `sar_orch/worker.py:168` 的 `_assemble_tools_async()`：`SAR_WORKER_TOOLS` 遍历处按类名过滤，MCP 工具（`map_agent__*` 5 个）在 `load_mcp_tools_async()` 之后按 `t.name` 过滤即可。**工程量小，不需要碰上层调用链。**
2. 测量 prompt 自动收缩了多少，再决定 `system.md` 正文还要不要手工精简。
3. 叠加 Tool RAG：按当前观测 + 子任务描述检索 3–5 个工具。
4. 如要用 prompt 进化：先修 1.5 的 bug，且必须**多场景轮换**分析源，不能再用单个 scene1/seed42 run。

**依据**：TRAJECT-Bench (arXiv:2510.04550) 测的全是前沿模型（Claude-4 / Gemini-2.5 / DeepSeek-V3.1 / o4-mini / Kimi-k2），结论 "the steepest drop occurring between three and five tools"，o4-mini 和 Kimi-k2 超过 7 个工具就崩。三类失败模式（相似工具混淆、参数盲选、冗余调用）正是 20+ 工具场景的高发项。收益侧：ToolScope 实测工具选择准确率提升 8.38%–38.6%；TinyAgent-1.1B 在 16 工具场景靠「蒸馏 + Tool RAG」从 12.71% 做到 80.06%，超过 GPT-4-Turbo 的 79.08%；TRAJECT-Bench 的检索消融显示越弱的模型收益越大（Claude-4 EM 0.445→0.473，Claude-3.7 0.135→0.186）。

**门槛（关键决策点，评审认为偏乐观，故调整为分档）**：在 held-out 评估集上（不是 scene1/seed42）：

| 结果 | 判断 |
|---|---|
| ≥40% | 瓶颈在 prompt/架构，继续阶段 2 |
| 20–30% | 提升不足 2 倍，先考虑**提前执行阶段 3 换 teacher**，判断是否已触到 deepseek-flash 的能力天花板 |
| 无变化 | 瓶颈在别处（barrier、checker 定义、任务难度），去 debug 而非训模型 |

评审的质疑成立：靠工具窄化 + prompt 精简把成功率拉 6 倍缺乏历史证据支撑（10 代进化没有可信数字显示推高过成功率）。所以「40%」是理想目标而非可保证的门槛，20–30% 档位要有明确的分支动作。

**为什么这是硬门槛**：GRPO 的 advantage = (r-mean)/std，组内 reward 全同时 std=0、advantage 全为 0，该 prompt 对梯度贡献为零。这不是猜测，是算法定义的数学后果 —— DAPO (arXiv:2503.14476) 的 Dynamic Sampling 组件正是为过滤 accuracy=0 或 1 的 group 而设计。10% 成功率意味着大部分 group 是 all-zero，带着它上 RL 等于烧算力。

### 阶段 2 — 并发压测（2–3 天，拆成两半）

评审指出原设计有依赖问题：用 deepseek-flash 测出的并发结论，换成 Claude/GPT 或本地 vLLM 后大概率不成立。故拆分：

**2a（现在做，与 teacher 无关）** —— 只测框架层：
- 端口分配在 50 / 100 / 200 并发下是否正常（目前只验证到 2）
- barrier 同步和消息队列的排队拐点
- 先把 `sar_orch/results/benchmark/` 下从未跑完过的 benchmark 真正跑完一次

**2b（阶段 3 确定 teacher 后再做）** —— 测 API 侧：
- 目标 teacher 的 RPM/TPM 限流边界（concurrency=2 就已出现超时，这是当前最被低估的瓶颈）
- 换本地 vLLM 后的延迟结构变化

墙钟估算（用实测中位 301s，非 570s）：

| 规模 | 总 episode | 串行 | 并行 50 | 并行 100 |
|---|---|---|---|---|
| pilot | 3,200 | 11 天 | 5.4h | 2.7h |
| 中等 | 19,200 | 67 天 | 1.3 天 | 16h |

串行不可行是确定的，异步 rollout 是必需项而非可选项。但**数据生成阶段 GPU 不是瓶颈** —— 301 秒主要是远程 API 往返 + barrier 协调。

**换本地 vLLM 后延迟结构会完全改变，但并发上限会被 KV cache 卡住而不是被 API 限流卡住**：单张 24GB 上 Qwen3-8B 只剩约 5GB KV ≈ 35,000 token ≈ **3 个并发 10.6k 请求**（推导见阶段 5 的 KV 预算小节）。上表「并行 50 / 100」在单卡本地推理下不可达 —— 那两列只对「远程 API + 足够配额」的情形有意义。

**这张表也不能直接搬到 RL 阶段**：RL 的 rollout 需要 `num_generations` 份同 prompt 采样（TRL 默认 8），单个训练 step 的墙钟是 301s × 8 ÷ 实际并发度。见阶段 5 的吞吐小节。

**门槛**：2a 通过（并发 50 端口/barrier 不退化）+ 2b 的限流边界已知。

### 阶段 3 — 用强模型重产数据（1 周）

阶段 1 的窄化配置定型后再做，否则数据跟着 prompt 一起过时。

- teacher 换 Claude / GPT 档。现有 82 个 run 的 deepseek-v4-flash + 10% 成功率**不能用于蒸馏** —— 学生的上界是模仿 teacher 的失败模式，它 `max_steps_reached` 时的绕圈行为会被一并学走
- seed 从单一 42 扩到 50–200；补齐 agents=5/6 和 scene 2–5（现在严重倾斜）
- rejection sampling：同一初始状态采 k 次，只留 `finished=true` 或 `transport_rate` 高的
- 失败轨迹**不删**，用 Negative-Aware Training（样本前加成功/失败标记一起 SFT）。成本极低，有正面文献支持，比上 DPO 更适合当前数据量

**目标量**：去重后成功轨迹 2,000–5,000 条，对标 APIGen-MT 的 5K 高质量拒绝采样（复杂度最接近本场景）。若强 teacher 成功率能到 40–60%，总尝试量需 4,000–12,000 episode。

**隐含依赖（评审指出）**：这个目标量依赖阶段 1 把成功率拉起来。如果阶段 1 没达标，即便换了强 teacher，凑够样本的采样成本也会指数上升。**所以阶段 1 的分档结果直接决定阶段 3 的预算，两者不是独立的。**

### 阶段 4 — Minimal cold start（1.5–3 周，此前估 3–5 天严重偏低）

**目的是稳格式，不是传能力。不要做成大规模蒸馏。**

估时上修的理由：3–5 天只覆盖了「跑 trainer」这一步，漏掉了三个各自天级的子任务 —— 序列长度统计 + `max_length` 决策、单卡 24GB 的显存配置试错（融合 CE / QLoRA / 降级路径）、LoRA merge 与 checkpoint 恢复。加上第一次 OOM 排查。详见配套文档阶段 -0.5 / 1a / 1b。

两条路选一：

- **(a) 参数级**：几百条量级 LoRA SFT，只求稳定输出合法 tool_call 结构。基座 Qwen3-8B（BFCL v3 60.2 非思考 / 68.1 思考，128K，Apache 2.0）
- **(b) ICRL**：system prompt 里放 few-shot 工具调用示范，随训练进程逐步减少示例数退火到 zero-shot，不做参数 SFT。工程上比维护 checkpoint 简单

**依据**：《Why Multi-Step Tool-Use RL Collapses》(2026) 指出多轮工具 RL 崩溃的根因是**特定控制 token 概率突刺破坏结构化格式**，底层能力未丢，解法是 interleaved SFT+RL。《SFT Memorizes, RL Generalizes》(ICML 2025) 支持 RL 泛化更好，但同一篇写明 SFT 的价值在 "stabilizes the model's output format"。注意该结论有争议：《Debunk the Myth of SFT Generalization》(2025) 指出 SFT 的失败很多源于 frozen-prompt artifacts，引入 prompt diversity 后 SFT 也能追平 RL。

**最终交付模型的规模不要往下试**（这句话只约束交付模型，**不约束学习阶段** —— 验证 loss mask、chat template 渲染、rollout 循环这些管线正确性与模型大小无关，学习阶段应该用 1.7B/4B 快速迭代，见配套文档第三节的实验-模型对照表）。xLAM-2 同家族同方法的完整曲线（arXiv:2504.03601）：

| 模型 | BFCL v3（单轮 FC） | τ-bench avg（多轮 agentic） |
|---|---|---|
| xLAM-2-1b-fc-r | 58.90 | **21.8%** |
| xLAM-2-3b-fc-r | 65.11 | **38.2%** |
| xLAM-2-8b-fc-r | 72.83 | **46.7%** |
| xLAM-2-70b-fc-r | 78.19 | 56.2% |
| GPT-4o (2024-11) | 72.08 | 52.9% |

BFCL 曲线平缓、τ-bench 曲线极陡 —— **BFCL 会系统性高估小模型在真实多轮场景的能力**。SAR 对标 τ-bench 而非 BFCL。3B 还有额外风险：ACEBench 显示 Hammer2.1-3B 只有 11.3 分，比通用的 Llama-3.2-3B-Instruct (19.6) 更差，专用微调在小尺寸上会丢泛化。

注：xLAM-2 是 cc-by-nc-4.0 非商用，只能作对照不能直接用。

**Qwen3-8B 在单张 4090（24GB）上的可行性**（闭式计算，词表 151,936 / 8.19B 参数）：真正的瓶颈不是权重（bf16 16.4GB）而是 **logits + 交叉熵** —— seq=12,288 时朴素 CE 单独吃 **18.7GB，比整个基座还大**。三档结论：

| 配置 | 合计 | 判断 |
|---|---|---|
| bf16 + 朴素 CE + ckpt | ≈39 GB | 放不下 |
| bf16 + 融合 CE + ckpt | ≈21–22 GB | 勉强，余量极小 |
| **QLoRA 4bit + 融合 CE + ckpt** | ≈10–11 GB | **舒适** |

所以阶段 4(a) 用 Qwen3-8B 在 4090 上**可行但有前提**：融合交叉熵（Liger-Kernel / cut-cross-entropy）不可省、`gradient_checkpointing` 必开、强烈建议 QLoRA 4bit。完整分项、必备配置清单和 OOM 七步排查见配套文档知识地图 §5/§6。

### 阶段 5 — RL（评审认为 2–4 周严重低估，改为 6–10 周）

代码库里目前没有任何 RL 训练循环雏形，`rollout_func` / `HarnessRolloutWorker` 都是构想。评审的判断是对的：这类 agentic RL harness 集成通常以季度计。

**框架**：TRL 的 `rollout_func` / `HarnessRolloutWorker(harness_adapter=None)`。A2A + MCP + barrier 已跑通，需要的是「收 token 算 loss」的后端，不是规定交互形态的框架。`max_inflight_tasks` 对应并发需求。备选 rLLM 的 `@rllm.rollout`（对交互形态零假设，backend 可在 tinker / verl 间切）。若后续想只训 worker 不动 coordinator，Agent Lightning 是唯一把「多智能体里选择性优化部分 agent」当原生设计目标的。

**注意**：TRL 的 `rollout_func` / `HarnessRolloutWorker` 标注 experimental，API 迭代快，需锁版本；具体签名请查安装版本的文档或源码，不要凭二手描述写代码。已知返回契约是 `{"prompt_ids", "completion_ids", "logprobs"}` 的 dict（可选 `logprob_token_ids`，其余字段透传给 reward function），但仍应以你安装版本的源码为准。

**算力约束（本阶段唯一的硬门槛）**：TRL 的 `vllm_mode="server"` 官方要求 vLLM 与 trainer 分处**不同** CUDA 设备，同卡直接 `RuntimeError`；单卡只能用默认的 `colocate`。所以本阶段起需要 **≥2 张卡或单张 80GB**，且建议后者 —— 消费级 4090 无 NVLink、P2P 被禁，权重同步会退化到走主机内存中转，两张 4090 不一定比一张 A100 划算。

**吞吐比显存更早成为拦路虎**：episode 实测中位 301 秒，GRPO 默认 `num_generations=8` → **一个训练 step 就是 40 分钟**。进本阶段前先做配套文档的实验 5.5（用 `rollout_func` 跑通单个 SAR episode 但不训练），把这个数字量出来再决定 `num_generations` 和并发度。

**算法**：DAPO（`loss_type="dapo"`）起步，trajectory-level。两处必须知道的实现现实：

- **Dynamic Sampling 在 TRL 里官方标注不支持** —— 而它恰好是「组内 reward 全同 → 零梯度」的官方对策。过滤逻辑要自己写进 `rollout_func`。顺带修正下文对零梯度的归因：直接原因是 `r - mean = 0`（分子为零），不是 `std=0` 除零，所以关掉 `scale_rewards` 解决不了，要去调 reward 区分度
- **GSPO (arXiv:2507.18071，Qwen3 团队) 先别用**。它把 importance ratio 从 token-level 改为 sequence-level，对长轨迹方差更稳 —— 但 TRL 官方对 `importance_sampling_level="sequence"` 注明「only has an effect when training goes slightly off-policy—for example, when `steps_per_generation > gradient_accumulation_steps` or `num_iterations > 1`. Otherwise, it is effectively equivalent to no modification」。纯 on-policy 下切过去等于什么都没改，等真的走轻度 off-policy 时再上

**KL**：TRL `beta` 默认 0.0，此时**reference 模型根本不加载、也没有 KL 曲线**，DAPO/GSPO 的官方复现配置都是 `beta=0`。若确实担心 8B 漂移到退化解而要开 `beta>0`，代价是多一份 reference 模型显存 + 多一次前向，在 24GB 上要算进预算。

**Reward**：用已有的 per-agent per-step `Successes` + per-subtask checker。

**这是相对文献的真实优势**：GiGPO (arXiv:2505.10978) 的 anchor state 分组本质是在没有步级 reward 时**近似**出你已经**精确拥有**的东西。而且 **GiGPO 搬过来大概率失效** —— ECPO (2026) 指出它在有限 rollout 下统计不可靠，"rare but lucky actions may receive overly large advantages, producing divergent anchor bias and late-stage training oscillation"。你 300+ 秒一个 episode，组内样本数天然受限，正撞在失效条件上。**跳过整套 anchor 机制。**

**必须补的一层**：action success ≠ team contribution。Agent 成功把水搬到错误火点，`success=True` 但贡献为零；两个 worker 各自「成功」救同一个人，动作层面都对但分工低效。用**规则**（而非 LLM 打分）做轻量 team-level 修正：subtask 完成时按「该 agent 是否在该 subtask 窗口内执行了关键动作」分配 reward。规则版精确无噪声，代价是无法覆盖没预想到的失败模式（资源竞争、互相干扰）。

**训练分配**：优先给 worker 更多更新预算，coordinator 先 prompt 工程免训练。Traj-Evolve (2026) 观察到联合训练时 manager 的 loss 收敛很快、边际收益迅速见顶，worker 的提升空间更持久。这条只有单篇 2026 论文的观察支撑，成熟度未核实，建议自己做小规模消融。

**多 worker 策略**：Alice/Bob/Charlie/David 工具集相同，**共享一套权重**起步（参考 EALLMs / OSPO 的同质 agent + 组内相对优势思路）。风险是行为趋同（都涌向同一火点）—— Kaleidoscope (NeurIPS 2024) 和 Contrastive Trajectory Learning 都指出全参数共享导致行为同质化、限制探索。若观察到，再考虑部分参数共享（SHPPO 的共享主干 + 异构层）。

**非平稳性**：每隔 K 个 episode 只更新 coordinator 或只更新 worker，别每次联合更新。这是经典 MARL 的成熟技巧，在现有 barrier 机制上容易实现。注意：LLM agent 场景下的非平稳性只被**间接观察到**（AT-GRPO 的分组失效、SPIRAL 需专门稳定机制），没有系统性研究，此条属工程保险而非文献结论。

**上下文增长**：SUPO (arXiv 2025) 专门处理多轮 RL 里 context 撑爆的问题。现有 token 化上下文压缩（commit `81d5efe`）阈值 64000、实测峰值 58071，训练时若 episode 变长会触发，需要提前决定是否把摘要策略也纳入优化。**另外 `recent_messages=12` 的 count-based 剪枝一直在生效**（见 1.1a(2)）—— RL 时它会决定 policy 实际看到的上下文，等于是一个未被优化的超参，要显式决定训练期是否保持这个值。

**推理侧 KV cache 预算**（rollout 用本地 vLLM 时必算）：Qwen3-8B 每 token KV = 36 层 × 8 KV head × 128 head_dim × 2 × 2B = **144 KiB**。24GB 卡 `gpu_memory_utilization=0.9` 减去 16.4GB 权重后 KV 只剩约 5GB ≈ **35,000 token ≈ 3 个并发 10.6k 请求**。而 coordinator prompt 峰值 58,071 tokens，**单个请求就超过整卡 KV 容量** —— 这独立佐证了「coordinator 免训练、继续走远程 API」的设计。

---

## 3. 决策点汇总

| 阶段 | 门槛 | 通过 | 未通过 |
|---|---|---|---|
| -1 | held-out 集 + baseline 数字可复现（含锁定的采样参数） | 继续 | 先修 metric bug（含 `load_metrics` 的覆盖 bug） |
| 0 | 去重后样本数 | >2000 → 蒸馏可选 | 几百条 → 排除蒸馏（**1.1b 已确认属此档**） |
| **1** | **held-out 成功率** | **≥40% → 阶段 2** | **20–30% → 提前换 teacher；无变化 → 去 debug** |
| 2a | 并发 50 端口/barrier 不退化 | 继续 | 改编排系统 |
| 2b | teacher API 限流边界已知 | 继续 | 申请配额 / 上退避策略 |
| 4 前置 | 全部样本 `assistant_mask.sum() > 0`，`max_length` 按实测分布设定 | 可以开训 | 先修 exporter —— 截断/mask 为空不报错，会毁掉整轮训练 |
| 4 | 8B 稳定输出合法 tool_call（采样参数与 baseline 一致） | 上 RL | 查控制 token 概率 |
| 5 前置 | 单个 SAR episode rollout 能拿到 `(prompt_ids, completion_ids, logprobs)` 且 token 数与 vLLM 一致 | 进 RL 训练 | infra 未通，先补，别急着上训练循环 |

---

## 4. 风险排序

| # | 风险 | 早期检测信号 |
|---|---|---|
| 1 | **数据分布单一导致伪进步**（seed 100% 是 42、scene 1 占一半、agents=6 零覆盖） | 阶段 3 扩 seed 后成功率大幅低于阶段 1 测出的水平（如 scene1/seed42 下 40%，换 scene 4/5 掉到 10% 以下） |
| 2 | **API 并发限流**让阶段 2/3 的墙钟估算全部失真 | 10–20 并发试跑时频繁 429/超时（concurrency=2 已出现过一次） |
| 3 | **40% 门槛不可达**，阶段 1 长期卡住阻塞后续 | 窄化后跑 20–30 个组合，提升不足 2 倍（7/72→15/72 而非 30/72）→ 立刻转向换 teacher |
| 4 | **evolution/ 的 metric bug** 产生虚假「无变化」结论误导判断 | 跑一次 `orchestrator.py`，看 `pretty_metrics()` 是否打印空字符串 |
| 5 | **阶段 5 无 infra 基础**，时间预算与工作量不匹配 | 到阶段 5 开始时若还没有能跑通单 episode rollout 的最小验证，说明 infra 会吃掉大半预算 |
| 6 | **训练数据静默损坏**（`max_length` 默认 1024 截断 + mask 为空不报错） | 训练 loss 异常低但生成质量差 → 立刻 dump 一条样本的 `labels != -100` 位置解码看是不是 assistant 文本 |
| 7 | **rollout 吞吐而非显存成为瓶颈**（301s × `num_generations=8` = 40 分钟/step） | 实验 5.5 一跑就能看到；若不提前量，会在阶段 5 中期才发现训练根本推不动 |
| 8 | **训练环境版本地雷吃掉数天**（flash-attn 编译、TRL 只支持 vLLM 0.17.0–0.25.1、旧 `requirements.txt` 的 torch 2.2.0 冲突） | 第一天装环境就受阻 → 走 Kernels Hub 而非手工编译，训练 venv 与业务 venv 分离 |

---

## 5. 可以砍掉的部分

- **阶段 3 可推迟**：若走 ICRL 路线（few-shot 退火，不做参数 SFT），不需要几千条蒸馏数据，阶段 3 缩成「扩 seed 让 RL 有多样任务分布」即可
- **阶段 4 可整个跳过**：走 ICRL 就没有独立 SFT 阶段
- **阶段 5 不是必须**：若阶段 1 把成功率做到 60%+，而目标是降推理成本而非提升上限，那「窄化工具 + 8B + 少量 cold start」可能已够用。CacheRL (2026) 的消融：移除知识迁移导致性能下降 41%，而 **RL 提升训练稳定性但相对强 SFT 基线的绝对收益有限** —— 数据质量和 reward 设计比优化算法更重要

---

## 6. 文献依据索引

| 主题 | 出处 | 关键数字 |
|---|---|---|
| 工具数衰减曲线 | TRAJECT-Bench, arXiv:2510.04550 | 3–5 工具处最陡下降；o4-mini/Kimi-k2 超 7 工具崩 |
| 工具窄化收益 | ToolScope | 工具选择准确率 +8.38%–38.6% |
| 极端窄化案例 | TinyAgent-1.1B | 16 工具场景 12.71% → 80.06% |
| 模型规模曲线 | APIGen-MT / xLAM-2, arXiv:2504.03601 | τ-bench: 1B 21.8% / 3B 38.2% / 8B 46.7% |
| 小尺寸专用微调丢泛化 | ACEBench (EMNLP Findings 2025) | Hammer2.1-3B 11.3 < Llama-3.2-3B 19.6 |
| 多轮一致性衰减 | τ-bench (Yao et al. 2024, arXiv:2406.12045) | retail 域 pass^8 <25%（摘要另给「成功率 <50%」；此前本表写的 pass^1 >60% 对不上出处，已删） |
| GRPO 零梯度组 | DAPO, arXiv:2503.14476 | Dynamic Sampling 过滤 accuracy=0/1 的 group（⚠️ **TRL 官方标注不支持此组件**，需自行实现） |
| 长轨迹方差 | GSPO, arXiv:2507.18071 | sequence-level importance ratio（⚠️ TRL 官方：纯 on-policy 下等价于不做修改） |
| 步级 credit assignment | GiGPO, arXiv:2505.10978 | ALFWorld +12% / WebShop +9% |
| GiGPO 在低 rollout 下失效 | ECPO (2026) | anchor bias + 后期震荡；比 GiGPO 再 +5–7pt |
| 格式崩溃根因 | Why Multi-Step Tool-Use RL Collapses (2026) | 控制 token 概率突刺；interleaved SFT+RL |
| SFT 稳格式的价值 | SFT Memorizes, RL Generalizes (ICML 2025) | "SFT stabilizes output format" |
| 对上条的反驳 | Debunk the Myth of SFT Generalization (2025) | frozen-prompt artifacts 是主因 |
| 绕开 SFT 的路径 | ICRL (2026) | few-shot 退火到 zero-shot |
| 蒸馏数据量对标 | APIGen-MT | 5K 拒绝采样轨迹；Phase1 成功率 28%→70%，Phase2 67% |
| 低数据量门槛 | FireAct | 500 条时能力开始 emerge（但任务是单工具单轮） |
| 失败轨迹利用 | Learning From Failure (NAT) | 加成功/失败标记的 SFT |
| 上下文管理 | SUPO, arXiv 2025 | 多轮 RL 的 context 压缩 |
| 角色化 MAS 的 GRPO | AT-GRPO, arXiv:2510.11062 | 标准 GRPO 分组假设在 MAS 下失效 |
| coordinator vs worker 收敛差异 | Traj-Evolve (2026) | manager loss 快速见顶，worker 提升更持久 |
| 参数共享的代价 | Kaleidoscope (NeurIPS 2024) / SHPPO | 全共享导致行为同质化 |
| RL 相对 SFT 的边际收益 | CacheRL (2026) | 移除知识迁移 -41%；RL 绝对收益有限 |

**信源说明**：调研时 tavily 配额耗尽，以上依据来自 Semantic Scholar 论文原文/摘要 + GitHub README。标注 2026 年、引用数个位数的论文（ECPO / Traj-Evolve / C3 / LangMARL / CacheRL）只读到摘要，同行评审状态与实验严谨性未核实，引用前建议下载全文确认。部分 arXiv 编号来自模型回忆，建议按标题搜索核实。

**第二轮已核实的编号**：LoRA = arXiv:2106.09685，DeepSeekMath（GRPO 原始出处）= arXiv:2402.03300，GSPO = 2507.18071，DAPO = 2503.14476。DAPO 的 arXiv 页面 Comments 只有 "Project Page"、无 venue 字段，此前配套文档写的「NeurIPS 2025」查不到出处，已删除。

**框架侧行为已按官方文档核实**（TRL / vLLM / SGLang / transformers / Qwen 官方）：`SFTConfig.max_length` 默认 1024 与 `keep_start` 截断、`assistant_only_loss` 及其 `{% generation %}` 前置条件、`chunked_nll` 与 PEFT 不兼容、`beta` 默认 0.0 不加载 reference、`importance_sampling_level="sequence"` 的生效条件、DAPO 五组件与 Dynamic Sampling 不支持、`vllm_mode` 的分卡硬约束、vLLM 与 SGLang 的 prefix caching 均已默认开启、FlashAttention-3 为 Hopper only、TRL 支持的 vLLM 版本区间 0.17.0–0.25.1。详见配套文档第六节。

---

## 7. 下一步

阶段 -1 和阶段 0 可并行。建议先做阶段 -1 的第 1 项（修 metric key bug + `load_metrics` 的覆盖 bug），因为它是所有后续判据的前提；再做阶段 1 的工具白名单，它的结论决定阶段 0 的数据有没有价值（prompt 一改旧轨迹的 system prompt 就过时，而工具 schema 又无法从日志复原，见 1.1a(1)）。

**训练侧的第一步不是训练**：先做配套文档的阶段 -0.5（建独立训练 venv + 自检脚本）和实验 1（chat template 往返一致性，不用 GPU）。这两步花不到三天，却能挡掉后面最难查的两类问题 —— 环境版本地雷和静默的数据损坏。
