---
日期: 2026-07-30
文档类型: 学习路径 / 知识地图
文档概述: 面向 LLaMAR 项目的 LLM 训练知识补齐清单。含分层知识地图、按阶段学习路径、8 个最小可行实验、误区清单与不必学的内容。已按「单张 RTX 4090 24GB」的算力约束和 860 个 episode 的实测 token 分布修订。
---

# LLM 训练知识补齐清单

配套文档：[小参数模型训练路线图](2026-07-30-small-model-training-roadmap.md)

## 核心判断

短板不是「不懂 RL 理论」，而是**没有跨过「文本 ↔ token_id ↔ 梯度」这层膜**。

已有的工程能力（A2A、MCP、barrier、语义地图、日志体系）在训练侧几乎不用重学，能直接迁移成数据管线和环境适配层。真正要补的集中在四个窄口：**tokenizer/模板层、显存/序列长度层、训练环境的版本地雷、RL 算法的工程语义**（不是数学推导）。

已可复用的资产：

| 资产 | 位置 | 训练侧用途 | 注意 |
|---|---|---|---|
| `Message` / `ToolCall` schema | `src/Agent/worker_agent/schema/schema.py` | OpenAI 风格多轮 tool_call 结构，SFT 轨迹导出的现成素材 | `arguments` 是 `dict`（`schema.py:19`），恰好符合 transformers 的期望，见误区 1 |
| `AgentLogger` NDJSON | `src/Agent/worker_agent/logger.py` | 完整 messages + usage + tool 结果 | **`tools` 字段只存工具名**（`logger.py:111`），不是完整 schema，见下 |
| `OpenAIClient` | `src/Agent/router_agent/llm/openai_client.py` | 走标准 OpenAI 协议，换本地 vLLM 只改 `api_base` | `finish_reason` 被硬编码为 `"stop"`（`openai_client.py:292`），无法用它识别截断样本 |
| `run_metrics.json` | 各 run 目录 | `coverage`/`transport_rate`/`finished` 已是 episode 级标量，可直接做 reward 基础项 | |

依赖现状：`pyproject.toml` 里**没有** torch / transformers / vllm / sglang / trl / peft 任何一个；`.venv`（Python 3.11.15，237 个包）里 ML 相关只有 `tiktoken`。注意 `requirements.txt:135,139` 有 `torch==2.2.0` + `transformers==4.38.0`，是旧 MAP-THOR 遗留、venv 里并未安装，且这个老 pin 与 Qwen3 所需版本冲突 —— **它是负担不是资产，训练环境必须另建独立 venv**。

**两个必须先补的缺口**：

1. **指标坎**。`evolution/` 跑到 gen 10，但评分是 LLM 读日志后写的**定性分析**，没有自动化的标量 fitness。RL 必须有这个数字。**从 prompt 进化到 RL，要跨的第一道坎是指标坎，不是算法坎。**
2. **日志里没有工具 schema**。`logger.py:111` 是 `[tool.name for tool in tools]`，而工具描述文本是运行时由 `src/Agent/worker_agent/build.py` 拼接的。**历史轨迹的工具 schema 无法从日志精确复原，只能按当前代码近似重建。** 这限制了实验 1「往返一致性」能做到的严格程度，也意味着一旦工具集变更，旧轨迹的 system prompt 就整体过时。

---

## 零、实测数据规模（后面所有显存/时长估算的基准）

全量普查 860 个 `sar_orch/results/*/workers/<A>/<A>/*.ndjson`（不是抽样）。token 数由 5,817 组 `(字符数, usage.prompt_tokens)` 做 OLS + 同 episode 相邻请求差分交叉验证标定得出 **3.21 字符/token + 约 1,100 tokens 工具 schema 常量**。

| 指标 | min | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| 整条 episode 样本 | 5,159 | **8,121** | **11,568** | 18,517 | 32,137 |
| 其中进 loss 的 assistant 部分 | 43 | **1,720** | 3,880 | 8,623 | 15,193 |
| assistant turn 数 | 1 | 5 | 13 | 26 | 46 |
| tool_result 数 | 0 | 9 | 20 | 43 | 75 |
| 单请求 prompt_tokens（实测 usage） | 3,940 | 6,969 | 8,186 | 9,410 | **10,592** |
| 单请求 completion_tokens（实测 usage） | 31 | 225 | 674 | 1,225 | 2,758 |

**监督比例中位 21.1%**（p90 35.7%）—— 中位 episode 8,121 token 里只有 1,720 token 进 loss，system prompt 单独占 3,977 token。856 条 episode **总监督 token 仅 181 万**。

`max_seq_len` 覆盖率：4,096 → 0% / **8,192 → 51.4%** / **12,288 → 93.7%** / 16,384 → 98.0% / 32,768 → 100%。

**两条直接推论**：

- 数据量本身就排除了大规模蒸馏路线（181 万监督 token 约等于一个 8B 模型几分钟的训练吞吐），与配套路线图阶段 0 的判断一致。
- 上表基于 DeepSeek tokenizer 的实测 usage 标定。**换 Qwen3 tokenizer（词表 151,936）后需重测，可能有 ±15% 偏差**，`max_seq_len=12288` 的建议留了余量。

**一个文档此前漏掉的数据污染**：`src/Agent/worker_agent/context.py:38` 的 `recent_messages=12` 滑动窗口剪枝（`prune_history`，`context.py:627-680`）**始终在生效**（配套路线图 1.4 节只验证了 Phase 3 LLM 摘要从未触发，那条成立）。实测 **44.6% 的 episode** 在日志里已丢失早期轮次的原始 message，`messages` 数量硬顶在 15 而 assistant turn 最多 46。所以「一个 episode 导出一条多轮样本」**不能从最后一条请求还原**，必须跨 `llm_request` / `tool_result` 事件重建并集。

---

## 一、知识地图

### 必须掌握（不懂就会做错或调不出来）

#### 0. 训练环境的版本地雷

这条排在最前，因为它是**唯一一个会让你在写第一行训练代码之前就卡住一到两天**的东西，且与 ML 理论无关。

- **4090 是 Ada 架构**：bf16 可用；**FlashAttention-2 官方支持**（README 列了 "Ampere, Ada, or Hopper … RTX 4090"），但 **FlashAttention-3 是 Hopper only**，`kernels-community/vllm-flash-attn3` 在 4090 上装不了
- **不要手工编译 flash-attn**。TRL 官方原话：手工编译 "is complex and time-consuming. It's never recommended unless absolutely necessary"，走 Kernels Hub（`model_init_kwargs={"attn_implementation": "kernels-community/flash-attn2"}`）
- **TRL 对 vLLM 有版本区间约束**：官方明确「TRL currently only supports vLLM versions from `0.17.0` to `0.25.1`」。装错版本阶段 2 直接起不来
- **训练 venv 与业务 venv 必须分离**：业务侧 `numpy>=2.2.6`，训练生态对 numpy 版本敏感
- **bitsandbytes NF4** 需求很宽（Pascal 或更新），4090 无问题

#### 1. Tokenizer / chat template / 多轮 token 边界

**坑在哪**：你的 `Message` 有 `role`（system/user/assistant/tool）、`tool_calls`、`tool_call_id`。Qwen3 的 chat template 会把它们渲染成特定文本（`<|im_start|>assistant\n<tool_call>...</tool_call><|im_end|>`），而**不同模型家族的 tool_call 渲染格式完全不同** —— Qwen 用 `<tool_call>` XML 风格包 JSON，Llama 3 用 Python 函数调用风格字符串，有些模型把 tool_result 塞进 user turn 而不是独立 tool role。

**真正的坑是 dict vs JSON 字符串，不是 key 顺序。** transformers 官方明确警告：「the OpenAI API uses a JSON string as its `tool_calls` format. This may cause errors or strange model behavior if used in Transformers, which expects a dict」。你的 `schema.py:19` 是 `arguments: dict[str, Any]`，**恰好是对的，这一关天然通过**。而 key 顺序/空格/引号这类差异由模板里的 `| tojson` 统一处理，不是主要风险 —— 所以核对渲染结果时要做**语义级比较**（`json.loads(渲染串) == 原始 dict`）而不是字符级比较。

**为什么这个项目必须懂**：你要把 A2A 消息流 + MCP tool_call/result 导出成带 mask 的多轮数据。不先亲手跑一遍 `apply_chat_template` 肉眼检查渲染字符串，很可能因格式差异产生「看起来一样但 token 不一样」的样本。

#### 2. Loss masking + retokenization drift + 截断

只有 assistant 生成的 token 算 loss，system/user/tool 全部 mask（label=-100）。TRL 的参数名是 `assistant_only_loss=True`。

**这个参数有一条硬前置条件**：官方警告「This functionality requires the chat template to include `{% generation %}` and `{% endgeneration %}` keywords」。Qwen3 在 TRL 的 bundled 支持列表里、会自动打补丁。但 TRL 文档对 `qwen3_training.jinja` 只写「wrap assistant message output」，对 `qwen3_vl_training.jinja` 才明确写「both `content` and `tool_calls`」—— **带 tool_call 的 assistant turn 是否被计入 loss，措辞不一致，必须在实验 1 实测**。

**retokenization drift**。两种构造样本的方式：

- **(a) 应用层**：把完整 `Message` 列表重新走 `apply_chat_template` 生成字符串再 tokenize
- **(b) 网关层**：直接用推理时刻产生的 token_id（vLLM 返回的序列）

**两者不保证一致** —— BPE 的合并边界会跨 turn 边界变化。你的 `AgentLogger` 记录的是**文本层** message，不是 token_id，所以天然走 (a)。后果是：**必须用训练时的同一个 tokenizer + 同一个 chat template 版本重建历史轨迹**。Qwen3 若更新 `chat_template.jinja`，旧轨迹重新渲染出的 token 序列会变。

**做法**：导出 SFT 数据时把 tokenizer 版本号 + chat_template 的 hash 一起存进元数据。

**比 drift 更容易害到你的是截断**。TRL `SFTConfig.max_length` **默认 1024**，`truncation_mode` 默认 `"keep_start"`（且官方说这是唯一支持值）。你的样本中位 8,121 token、system prompt 单独占 3,977 —— 按默认跑，每条样本只保留开头 1024 个 token（system 的前 1/4），**末尾的 assistant 目标整段被丢掉，labels 全 -100，该样本零梯度**。它不报错：loss 可能显示 nan，或显示一个很低的数，你会以为收敛得挺好。

**两条硬动作，写进 exporter**：

1. 先跑序列长度直方图（用 Qwen3 tokenizer 对 100 条 episode 统计 `len(apply_chat_template(msgs, tools=tools, tokenize=True))` 的 p50/p95/max），再显式设 `max_length`
2. 断言任何样本 `assistant_mask.sum() > 0`，为 0 直接报错退出

#### 3. Qwen3 thinking 模式必须二选一并全链路锁死

Qwen3-8B 模型卡给了两套采样参数：thinking（temp 0.6 / top_p 0.95 / top_k 20）、non-thinking（temp 0.7 / top_p 0.8 / top_k 20），对应两种渲染模式。

**你的数据里 `reasoning_content` 出现 0 次**（860 个 ndjson 全量核查；teacher 是 deepseek-v4-flash，没有 reasoning 字段落盘）。而 TRL 的 `qwen3_training.jinja` 补丁说明写「Always include the thinking block regardless of message position」—— 对无 reasoning 的消息会渲染出空的 `<think>\n\n</think>`。于是训练数据全是「空思考」，而推理时默认 `enable_thinking=True` 会真的生成思考内容。两边 token 分布不一致，你会看到「SFT 之后格式反而更乱」却找不到原因。

**做法**：阶段 0 就对同一条 message 列表分别用 `enable_thinking=True/False` 渲染、diff 两个字符串、决定训哪一种，然后在整条链路上锁死 —— 训练侧 `chat_template_kwargs`，推理侧 vLLM 的 `chat_template_kwargs={"enable_thinking": false}`（注意 Qwen 官方提醒这个参数**不是 OpenAI API 兼容的**，走 `OpenAIClient` 时要确认能否透传）。

#### 4. LoRA vs 全参数

显存差异见下条。但更重要的是**任务特性**：你的 cold start 目标是「稳定输出 tool_call 格式 + 遵循精简后的 prompt 风格」，是局部行为调整而非知识注入，LoRA 够用。且 LoRA 对「小数据集上防止灾难性遗忘」更友好 —— 你只有百条量级 cold start 数据，全参数在小数据集上容易把基本对话能力也训退化。

- **target_modules**：decoder-only 标配 `q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj`（所有线性层，不含 embedding/lm_head，除非要改输出词表分布）
- **rank**：教格式的任务 rank 8–16 够。起点 `rank=16, alpha=32, dropout=0.05`，loss 降不下去或格式学不会再加大
- **学习率**：LoRA 的合适 lr 比全参数**高一到两个数量级**，凭全参数直觉调会完全学不动。TRL 官方口径是 adapter 训练用 ≈`1e-4`，而 `SFTConfig.learning_rate` 默认 2e-5，**对 LoRA 偏低**。起点：`learning_rate=1e-4, warmup_ratio=0.03, lr_scheduler_type="cosine"`
- **有效 batch = per_device_train_batch_size × gradient_accumulation_steps × 卡数**，记住这个恒等式，因为显存不够时你会一直在前两项之间搬运
- **注意 LoRA 反而拿不到一项省显存红利**：TRL 默认 `loss_type="chunked_nll"` 能把 logits 显存压到 `chunk_size × vocab`，但官方明确「Not compatible with `use_liger_kernel=True`, PEFT, or VLM」—— 用 LoRA 就享受不到（官方未说明是报错还是静默回退，需实测确认）

#### 5. 显存估算：真正的瓶颈是 logits，不是权重

Qwen3-8B：8.19B 参数、36 层、hidden 4096、**词表 151,936**。以下权重/优化器/logits 为闭式计算，激活为估算。

**seq_len = 12,288（覆盖 93.7% 样本）、batch = 1 时的分项：**

| 分项 | 计算 | 显存 |
|---|---|---|
| 基座权重 bf16 | 8.19e9 × 2B | **16.4 GB** |
| 基座权重 NF4 4bit | 8.19e9 × 0.5B + scale | **~5.0 GB** |
| LoRA r=16 参数 + 梯度 + AdamW 状态 | ~40M × (2+2+8)B | 0.5 GB |
| **朴素 logits + 交叉熵** | 12,288 × 151,936 × (2+4+4)B | **18.7 GB** ← 杀手 |
| 融合 CE（Liger-Kernel / cut-cross-entropy，分块） | 只存 chunk | **~0.5 GB** |
| 激活，开 gradient checkpointing（估算） | 36 层 × 重算 | ~3–5 GB |
| 激活，不开 checkpointing（估算） | — | **>40 GB，必爆** |

**三档结论：**

| 配置 | 合计 | 判断 |
|---|---|---|
| 8B bf16 + 朴素 CE + ckpt | 16.4 + 18.7 + ~4 ≈ **39 GB** | **放不下** |
| 8B bf16 + 融合 CE + ckpt | 16.4 + 0.5 + 0.5 + ~4 ≈ **21–22 GB** | 勉强，余量极小（碎片化和长尾样本会 OOM） |
| **8B QLoRA 4bit + 融合 CE + ckpt** | 5.0 + 0.5 + 0.5 + ~4 ≈ **10–11 GB** | **舒适，可上 seq=16k 或 bs=2** |

**要记住的一句话：24GB 上的瓶颈不是权重，是词表 151,936 × 序列长度的 logits。** 单这一项 18.7GB，比整个基座还大。而它与参数量无关、只与 seq_len 和词表有关 —— 所以「LoRA 更省显存所以不用管序列长度」是错的。

全参数 SFT 作为对照：bf16 权重 16.4 + bf16 梯度 16.4 + fp32 master 32.8 + Adam m 32.8 + v 32.8 ≈ **131 GB**（此前文档写 96GB，漏了 fp32 master weights）。结论不变：单卡不可行。

#### 6. 单卡 24GB 的必备配置清单

按重要性排序，前三条决定「能不能跑」：

1. **融合交叉熵，不可省** —— Liger-Kernel（`apply_liger_kernel_to_qwen3`）或 cut-cross-entropy。省 ~18GB，是分水岭
2. **`gradient_checkpointing=True`** —— TRL 官方说「enabled by default in all trainers」，但要确认没被关掉；不开激活就 >40GB
3. **QLoRA 4bit 强烈建议** —— `BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)`。这四个参数名属于必需的操作知识
4. **flash-attn 2 或 SDPA** —— 否则 attention 的 O(seq²) 在 12k 上再吃数 GB
5. **`max_length=12288`**（覆盖 93.7%），长尾 6.3% 截断或丢弃。降到 8192 只覆盖 51.4%，会砍掉一半样本的后半段轨迹，对多轮连贯性有害
6. **`per_device_train_batch_size=1` + 梯度累积**，`bf16=True`，**packing 关闭**（多轮 mask 正确性优先，见下）
7. 显存仍不够时的额外选项：`activation_offloading=True`（换时间）

**OOM 七步排查顺序**（会是你遇到的第一个错误，按顺序试，每步代价不同）：

| 步骤 | 动作 | 代价 |
|---|---|---|
| 1 | `per_device_train_batch_size` 降到 1 | 无（配合下一步数学等价） |
| 2 | 提 `gradient_accumulation_steps` 保持有效 batch | 换时间 |
| 3 | 降 `max_length` | **丢数据** |
| 4 | 确认 `gradient_checkpointing=True` | 换时间 |
| 5 | `activation_offloading=True` | 换时间 |
| 6 | 换 QLoRA 4bit | 换精度 |
| 7 | 换更小模型（8B → 4B → 1.7B） | 换能力 |

#### 7. RL 工程语义（不用手推公式）

- **on-policy**：GRPO/DAPO 的 rollout 应当用**训练中的最新权重**。严格说 PPO 系用 importance ratio + clip 正是为了**支持轻度 off-policy 的样本复用**（多个 epoch），所以「都是 on-policy」这个说法过粗 —— 理解这一点是理解 GSPO 的前提。工程坑：异步 rollout 时若用几步之前的权重生成样本去更新当前策略，staleness 太大会不稳定
- **importance ratio + clip**：算「新策略概率/旧策略概率」并 clip 防止单步过猛。**GSPO 把这个比值从 token-level 换成 sequence-level**。但 TRL 官方对 `importance_sampling_level="sequence"` 注明：「this method only has an effect when training goes slightly off-policy—for example, when `steps_per_generation > gradient_accumulation_steps` or `num_iterations > 1`. Otherwise, it is effectively equivalent to no modification」。**你在单卡小规模上默认是纯 on-policy，切 GSPO 等于什么都没改** —— 先跑 `loss_type="dapo"`，等你真的开始走轻度 off-policy 再上 GSPO
- **advantage 归一化的零梯度陷阱**：GRPO 组内减均值除标准差。**如果同一 prompt 的一组 rollout reward 全同（都成功或都失败），`r - mean = 0`，advantage 全为 0，这组样本对梯度零贡献。** 注意归因：直接原因是**分子为零**，不是 std=0 除零 —— 所以关掉 `scale_rewards` 并不能解决，要去调 reward 的区分度。你的场景真实风险：场景/seed 固定、任务同质会导致大量组零梯度。**对策：监控「零梯度组比例」作为诊断指标**，且 **DAPO 的 Dynamic Sampling 在 TRL 里官方标注不支持，过滤逻辑要自己写**
- **KL 惩罚**：**TRL `beta` 默认 0.0，此时 reference 模型根本不加载**（官方：「If `0.0` (default), the reference model is not loaded, reducing memory usage」），**没有 KL 曲线可看**。而 DAPO 和 GSPO 的官方复现配置都是 `beta=0.0`，靠 clip 和 overlong filtering 而非 KL 约束漂移。如果你确实担心格式崩（8B 比大模型更容易漂移到退化解），需要**显式设 `beta>0`**，代价是多一份 reference 模型显存 + 多一次前向 —— 在 24GB 上这个代价不小，要算进预算

#### 8. 训练稳定性诊断：看什么，以及怎么看

必须会看这几条曲线的**组合**，不是任何单一条。注意第二列 —— 混淆「现成的」和「要自己写的」会让你对着不存在的曲线发愁：

| 指标 | 来源 | 正常 | 异常含义 | 第一步查什么 |
|---|---|---|---|---|
| loss | TRL 自动 | 平稳下降 | 异常低但生成差 | **dump 一条样本的 `(input_ids, labels)`，把 `labels != -100` 的位置解码出来看是不是 assistant 文本** —— 这一条同时覆盖 mask 错误和截断两种病因 |
| entropy | TRL 自动 | 缓慢下降 | 快速降到近 0 → 过早收敛；完全不降 → 没学到 | 抽样看生成内容是不是塌成同一个 tool_call |
| gradient norm | TRL 自动 | 平稳 | 尖峰 → 某个异常样本主导；长期偏大 → 调 lr 或 clip | 定位那个 step 的 batch，dump 样本看是不是工具报错导致极端 reward |
| mean_token_accuracy | TRL 自动 | 上升 | — | — |
| reward | GRPO 自动 | 震荡上升 | 长期不涨 | **先看零方差组比例**（若已实现），而不是先调超参 |
| KL（vs reference） | **需显式 `beta>0`** | 收敛 | 持续单向增大 → 将训崩 | 降 lr 或提 `beta` |
| **零梯度组比例** | **要自己写** | 低 | 高 → 大量组无梯度 | 查 reward 区分度、任务难度分布 |
| **个体 success vs team 相关性** | **要自己写** | 正相关 | 个体涨 team 不涨 | team-level 修正项设计有问题（个体在「表演」局部成功，比如反复无意义探索却不去救人） |

**怎么打开**（远程租卡场景下这一步没人说就会卡住）：

```bash
pip install wandb && wandb login
# SFTConfig(report_to="wandb", run_name="...", logging_steps=10)
#   注意 TRL 把 logging_steps 默认从 500 改成了 10，对短跑很友好
# 或者本地看：
tensorboard --logdir <output_dir> --port 6006
# 远程卡需要 SSH 端口转发：ssh -L 6006:localhost:6006 <host>
```

#### 9. Checkpoint 与 adapter 生命周期

这一步正好卡在阶段 1 和阶段 2 之间：**训练产出的是 adapter，不是完整模型**。

- `save_steps` / `save_total_limit` / `resume_from_checkpoint=True`
- `seed`（`SFTConfig` 默认 42，与你数据的 seed 42 是巧合，别混淆）、`data_seed`、`full_determinism`
- 要让 vLLM 用上训练结果：先 `merge_and_unload()` 存成完整权重，或走 vLLM 的 LoRA 加载路径

#### 10. 评估：pass^k vs pass@k

- **pass@k**：k 次里至少 1 次成功（衡量「有没有能力」，宽松，容易被采样次数刷高）
- **pass^k**：k 次**全部**成功（衡量稳定性）

多轮 agentic 必须看 pass^k：长 episode 多步决策，一次成功可能靠运气，团队协作尤其容易「这次配合对了下次乱了」。**只看 pass@k 会高估实际可用性。**

**评估时必须锁采样参数**。同一 checkpoint 在 temp=0.7 和 temp=0.1 下的格式合法率能差十几个点，Qwen3 模型卡还明确警告 thinking 模式下不要用 greedy decoding（「can lead to performance degradation and endless repetitions」）。不锁参数，不同实验的数字不可比。

评估集划分见配套路线图阶段 -1（已给出具体设计：留未见 seed、agents=5/6、scene 4/5），**不要重复设计一遍**。

#### 11. 数据去重与泄漏

- 同一条 run 会被多个 worker 记录（860 个 worker episode 文件 vs 98 个 run 目录）
- 同一段 system prompt 在 6,189 条 `llm_request` 里几乎逐字重复
- 做法：按 `(run_id, agent, task_id)` 去重 + 按 assistant 输出做精确哈希/MinHash 去重，把去重前后条数都记进元数据

### 需要理解（知道机制和坑，不必深入）

**packing / padding**：packing 把多个短样本拼进固定长度 batch 减少 padding 浪费，单轮任务常用。但多轮 agentic 数据要小心 attention mask 是否正确隔离不同样本、loss mask 拼接后是否仍只算 assistant token。**依据**：packing 的 `bfd` 策略会自动启用 padding-free，而 padding-free 官方要求 FlashAttention 2/3，否则有 batch contamination —— 4090 上 FA3 不可用（Hopper only），只能用 FA2。**建议全程关掉 packing**：你的样本本来就长（中位 8k），packing 的收益小、风险大。

**vLLM/SGLang 机制**：continuous batching、PagedAttention 管 KV cache、prefix caching。你运行时的 system 消息 3,977 tokens、coordinator prompt 峰值 58,071 tokens，每次请求重算浪费巨大。注意 **vLLM 的 `enable_prefix_caching` 现在已是默认开启**（`vllm/config/cache.py` 里 `enable_prefix_caching: bool = True`），SGLang 的 RadixAttention 也是默认开（对应项是 `--disable-radix-cache`）—— 所以「SGLang 默认开、vLLM 需手工开」这个对比依据已失效。两者对你场景（多 worker 共享超长 system prompt）的命中率差异需要实测才能下结论。

**KV cache 预算**（阶段 2 必算）：按 Qwen3-8B config（36 层 / 8 个 KV head / head_dim 128），每 token KV = 36 × 8 × 128 × 2 × 2B = **144 KiB**。24GB 卡 `gpu_memory_utilization=0.9` ≈ 21.6GB，减去 16.4GB 权重，KV 只剩约 5GB ≈ **35,000 token ≈ 3 个并发 10.6k 请求**。而 coordinator prompt 峰值 58,071 tokens，**单个请求就超过整张卡的 KV 容量**。结论：**coordinator 继续走远程 API**（本来也不训它），worker 用 4B 或 8B-4bit 降权重占用换 KV 空间，并显式设 `--max-model-len`。

**rollout 与推理服务的耦合**：RL 时需要「拿最新权重 → 推理引擎更新权重 → 生成 rollout → 训练框架算梯度」这个循环。TRL 的关键约束：**默认 `vllm_mode="colocate"`（vLLM 跑在 trainer 进程内共享同卡），`vllm_mode="server"` 官方要求 vLLM 与 trainer 在不同 CUDA 设备上，同卡会直接 `RuntimeError`**。所以单卡必须用 colocate，「实验 6 需要两张卡」的真实原因是这个硬约束，不是显存加总。

`rollout_func` 的返回契约（TRL 标注 experimental）：返回 `{"prompt_ids", "completion_ids", "logprobs"}` 的 dict，可选 `logprob_token_ids`，其余字段透传给 reward function。**签名迭代快，请查你安装版本的源码。**

### 可以延后

- 分布式训练的**拓扑与 NCCL 调优** —— 但**单卡到双卡的第一步要学**：`CUDA_VISIBLE_DEVICES` 怎么分、`accelerate launch` 怎么起、以及消费级 4090 无 NVLink / P2P 被禁对 TRL server 模式权重同步吞吐的影响（会退化到走主机内存中转，需实测）。到实验 6 时建议直接租**单张 80GB** 而不是多租 4090
- 混合精度/量化的**数值理论**（fp8 实现、量化误差分析）—— 但 QLoRA 那四个参数名属必需操作知识，见上
- 自定义 CUDA kernel、FlashAttention 实现细节
- PPO 完整数学推导（GAE、value function 偏差方差分析）—— GRPO 系用组内相对 reward 绕开了 value function

---

## 二、学习路径

**与配套路线图的依赖关系（先读这段，否则会做白工）**：配套路线图阶段 -1（修 `evolution/orchestrator.py` 的 metric key bug + 建 held-out 评估集）和阶段 1（工具窄化）是本文档阶段 1 的**前置**。原因见路线图 L309：工具集一改，旧轨迹的 system prompt 就整体过时，而工具 schema 又无法从日志复原（本文档核心判断第 2 条）。**先做工具窄化，再导 SFT 数据**，否则你会花两周在一批马上作废的数据上。

时间估算按「学生、非全职、单卡 4090、零训练经验」给。

### 阶段 -0.5：环境与自检（0.5–2 天）

**目标**：一个干净的训练 venv + 一个能确认「我的卡和我的库都对得上」的自检脚本。

- 新建独立 venv（**不要复用业务 venv**），装 torch / transformers / trl / peft / accelerate / datasets / bitsandbytes / liger-kernel；vLLM 留到阶段 2，装的时候注意 TRL 的版本区间 0.17.0–0.25.1
- flash-attn 走 Kernels Hub，不要手工编译
- 写 5 行自检脚本，打印：`torch.cuda.is_bf16_supported()`、`torch.cuda.get_device_capability()`（4090 应为 (8,9)）、各库版本、`import flash_attn` 是否成功

**判据**：自检脚本全绿，且能在 4090 上把 Qwen3-1.7B 载进显存跑一次 forward。

### 阶段 0：token 视角热身（2–3 天）

**目标**：能解释「一条 A2A+MCP 轨迹渲染成 token 后，哪些是 assistant 生成的、边界在哪」。

- **必读**：HuggingFace Chat Templates 基础 `https://huggingface.co/docs/transformers/main/en/chat_templating`；**tool calling 在另一页** `https://huggingface.co/docs/transformers/main/en/chat_extras`（"Tool use"）
- **必做**：拿 Qwen3-1.7B 的 tokenizer（**这一步不需要 8B，也不需要 GPU**），写 20 行脚本，把 `sar_orch/results/*/workers/*/*/*.ndjson` 里一小段真实轨迹转成 Qwen3 期望格式，跑 `apply_chat_template(..., tools=...)`，肉眼检查渲染字符串；再 `tokenizer(text, return_offsets_mapping=True)` 看每个 token 对应哪段文本
- **必做**：thinking 模式 diff（见知识地图 §3），决定训哪一种并记录下来
- **必做**：用 Qwen3 tokenizer 重测第零节那张长度表（此前是按 DeepSeek usage 标定换算的）
- **注意 glob**：`workers/<A>/` 层级下还有 `mailbox.ndjson` 等无 `event` 字段的文件，必须按 `workers/<A>/<A>/` 双层过滤

### 阶段 1a：Exporter + 长度统计（3–5 天，不训练）

**目标**：一个能把 ndjson 变成带 mask 样本的函数，以及一张真实的长度直方图。

- 跨 `llm_request` / `tool_result` 事件**重建并集**（`system + 首个 user + 全部 assistant + 全部 tool_result`），不能只读最后一条请求 —— 44.6% 的 episode 有剪枝
- 工具 schema 从当前代码重建（日志里只有名字）
- 断言 `assistant_mask.sum() > 0`、`prompt_tokens < 64000`
- 记录 tokenizer 版本 + chat_template hash 到元数据
- 输出长度分位数，据此定 `max_length`

**判据**：全部 860 个文件跑通，报出「有多少条样本被截断/mask 为空」的具体数字。

### 阶段 1b：LoRA cold start（1–2 周）

**目标**：在 4090 上跑通一次 LoRA SFT，并且**你能证明 loss 是从 assistant token 上来的**。

- **必读**：TRL SFT Trainer 文档 `https://huggingface.co/docs/trl/main/en/sft_trainer`，重点数据格式和 `assistant_only_loss`（**API 变化快，查你安装版本，不要凭记忆**）
- **必读**：PEFT LoRA 文档 `https://huggingface.co/docs/peft/main/en/conceptual_guides/lora`
- **参考**：LoRA 论文 arXiv:2106.09685（Hu et al. 2021）；显存紧张再看 QLoRA
- **边做边学**：跟 `huggingface/trl` 的 `examples/scripts/sft.py`，只改数据加载部分
- **模型顺序**：先 Qwen3-1.7B 把管线跑通，再 4B，最后 8B QLoRA 确认。不要一上来抱着 8B 调
- 末尾必做：checkpoint 保存/恢复一次 + `merge_and_unload()` 一次（见知识地图 §9）

### 阶段 2：本地推理服务（1–2 周，**不能与阶段 1 并行**）

一张卡不能同时跑 SFT 和 vLLM。**建议把这个阶段提前到阶段 1b 之前**：先确认本地 8B 在这个任务上能不能正常调工具，再决定值不值得为它做 SFT。

**目标**：`OpenAIClient` 指向本地 vLLM，跑通一次 SAR 实验（不训练）。

- **必读**：vLLM OpenAI-Compatible Server 文档 `https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html`
- **必做**：先算 KV cache 预算（见「需要理解」一节），再启服务：

```bash
vllm serve Qwen/Qwen3-8B \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --max-model-len <按 KV 预算算出的值>
# --enable-prefix-caching 已是默认，不用加
```

**`--enable-auto-tool-choice --tool-call-parser hermes` 不加就会卡在第一分钟** —— 不加这两个，vLLM 不会把 `<tool_call>` 解析成 OpenAI 风格的 `tool_calls` 字段，而你的 `OpenAIClient` 正靠这个字段工作。

- 把 `sar_orch` 配置指向本地端点跑一次 scene_1，对比远程 API 的行为差异（tool 调用格式解析、延迟、吞吐）
- coordinator 保持走远程 API（58k prompt 单请求超过整卡 KV 容量）

**工程量被低估的风险**：从「云 API 直连」切到「本地 vLLM + tool calling」涉及 chat template 对齐、function calling parser 兼容性一堆隐性坑，代码库里没有任何踩坑记录。

### 阶段 3：RL 算法与框架落地（3–5 周）

**必读论文（按顺序，都只读关键节）**：

1. **DeepSeekMath**（提出 GRPO，arXiv:2402.03300）—— 只看 GRPO 那节，跳过数学推理章节
2. **DAPO**（arXiv:2503.14476，ByteDance Seed / 清华 AIR）—— TRL 官方列为 5 个组件：Overlong Filtering、Clip-Higher、Soft Overlong Punishment、Token-level Loss、**Dynamic Sampling（⚠️ TRL 不支持，要自己实现）**。最后一项恰好是零梯度组的官方对策
3. **GSPO**（arXiv:2507.18071，Qwen3 团队）—— 理解 sequence-level importance ratio 解决了什么。**但先别用**：纯 on-policy 下 TRL 官方说等价于不做修改（见知识地图 §7）
4. **（参考）SkyRL-Agent**、**MUA-RL** —— 多轮工具调用 RL 的工程实践
5. **（参考）SUPO** —— 对应你现有上下文压缩机制在 RL 场景下的延伸

**必读文档**：TRL GRPO Trainer `https://huggingface.co/docs/trl/main/en/grpo_trainer`，重点 `rollout_func`。**API 迭代快，打开文档现取最新签名，不确定时直接读 `trl/trainer/grpo_trainer.py` 源码。**

**单卡必须注意**：用默认的 `vllm_mode="colocate"`。若看到 `RuntimeError: Attempting to use the same CUDA device for multiple distinct roles`，说明你误开了 `server` 模式。

**边做边学**：读完 GRPO 那节 + DAPO 技巧列表后，直接跟 TRL 官方 `examples/scripts/grpo.py` 改一个玩具环境跑通。**不要线性通读所有论文再动手。**

### 阶段 4：接 SAR 环境 + reward 设计（4–8 周）

不看新资料，基于阶段 3 跑通的代码替换环境接口为 `SAREnv`/Controller，reward 从 `run_metrics.json` 的 `coverage`/`transport_rate`/`finished` 起步，先用最简单的线性加权跑通再迭代。

**估时向配套路线图对齐**（路线图 L217 对同一件事给的是 6–10 周，理由是「代码库里目前没有任何 RL 训练循环雏形」）。此前本文档写的 1–2 周是两份文档里最乐观的数字。

**吞吐是这一阶段的第一个拦路虎，不是显存**：episode 实测中位 301 秒，GRPO 默认 `num_generations=8` → **一个训练 step 就是 40 分钟**。必须先算清这笔账再决定 `num_generations` 和并发度。

多个同质 worker 共享权重（parameter sharing across homogeneous agents）是 MARL 成熟做法，不需要专门找论文支撑，按现有设计做。

---

## 三、最小可行实验序列

按代码复用度递增。每个实验标注了**该用多大的模型** —— 学习阶段的模型规模只由「跑得动、迭代快」决定，与最终交付模型规模无关（xLAM-2 的 τ-bench 曲线衡量的是任务能力，不是管线正确性）。

| # | 实验 | 模型 | 算力 | 时长 |
|---|---|---|---|---|
| 1 | Chat template 往返一致性 | 1.7B tokenizer | **不用 GPU** | 半天 |
| 2a | 零样本基线 | 1.7B → 8B | 1×4090 | 半小时 |
| 2b | 单轮 LoRA SFT | 1.7B → 4B | 1×4090 | 2–4h |
| 3 | 多轮 LoRA SFT | 4B 调通 → 8B QLoRA | 1×4090 | 1–2 天 |
| 4 | TRL 玩具 GRPO | 0.6B / 1.7B | 1×4090 | 几小时 |
| 5 | 自定义单轮工具 GRPO | 1.7B | 1×4090 | 半天 |
| 5.5 | 单个 SAR episode rollout（不训练） | 4B / 8B | 1×4090 | 1–2 天 |
| 6 | 完整 SAR + team reward | 8B | **≥2 卡或 1×80GB** | 数周 |

### 实验 1：Chat template 往返一致性（不训练，半天）

用 Qwen3 tokenizer 把真实轨迹渲染成 chat template 文本再 tokenize，核对 assistant token 边界。

**成功判据**：
- 写出一个函数，输入 `Message` 列表，输出 `(token_ids, loss_mask)`
- **语义级**核对 tool_call：`json.loads(渲染出的 arguments 串) == 原始 dict`。**不要做字符级比较** —— 模板里 `| tojson` 与日志原始 dict 的 repr 天然不同，字符级必然失败且失败无意义
- 对**全部 860 个文件**（不是抽 5 条）跑 `assistant_mask.sum() > 0` 断言，报出失败样本数
- 用 TRL 的 `return_assistant_tokens_mask=True` 路径而非自己写 mask，**并顺手验证带 tool_call 的 assistant turn 是否落在 `{% generation %}` 块内**（TRL 文档对 `qwen3_training.jinja` 和 `qwen3_vl_training.jinja` 的措辞不一致，只能实测）
- 完成 thinking 模式 diff 并记录选择

**这个实验不通过，后面所有训练都有隐性数据质量问题。**

### 实验 2a：零样本基线（半小时，不训练）

在 held-out 集上测基座模型输出合法可解析 tool_call 的比例。**这一步必须先做** —— 后面「提升了多少」的分母在这里。

**成功判据**：拿到一个带 95% 置信区间的数字，采样参数写死（`temperature=0.7, top_p=0.8, top_k=20`，non-thinking），n≥200。

### 实验 2b：单轮 LoRA SFT（1×4090，2–4h）

导出「给定观测+工具列表 → 输出一个合法 tool_call」的单轮样本（先不含多轮历史），LoRA 微调。

**为什么这个实验在 4090 上很宽松**：单轮样本短，`max_length=2048` 够用，logits 项只有 0.6GB。

**成功判据**：held-out 上合法 tool_call 比例从实验 2a 的基线 X% 提升到 **≥X+20pt 且绝对值 ≥95%**，tool 名/参数命中已知 schema 的比例明显提升。**采样参数与实验 2a 完全一致**，否则数字不可比。

**这个实验验证的是 loss mask + chat template 管线正确性，不涉及 RL。** 先用 1.7B 跑通再换 4B。

### 实验 3：多轮 LoRA SFT（1×4090，1–2 天）

扩展到多轮（含前几步 tool_result 作为历史）。**这是实验 2 到 3 之间最大的隐藏跨度**：单轮 2k token → 多轮 8–12k token，logits 从 0.6GB 跳到 18.7GB（朴素 CE），且 `chunked_nll` 因 PEFT 失效。

**必备配置**：`Qwen3-8B` + **QLoRA 4bit** + 融合 CE + `gradient_checkpointing` + `max_length=12288`（按实验 1 实测重定）+ `per_device_train_batch_size=1` + `gradient_accumulation_steps=8`。先用 4B 调通再换 8B。

**成功判据**：
- loss 曲线正常下降。**注意反例：若 mask 错误把 tool_result token 也算进 loss，曲线会异常低但生成质量差** —— 遇到就按诊断表第一行 dump `labels != -100` 的解码结果
- 用规则检查生成的 tool_call 序列是否违反前置条件（同一 person 重复 pickup、对已灭火点用资源），**报违规率**。用 `SAR/env.py` 的现成语义写个几十行状态机即可 —— 判据必须是可判定的数字，不是「感觉连贯」

**不要在这个实验里验证 packing**（与「全程关掉 packing」的建议矛盾，且 packing 对多轮 mask 有额外风险）。

### 实验 4：TRL 玩具环境 GRPO 单轮（1×4090，几小时）

不接 SAR，用 TRL 官方 example 的简单环境跑通训练循环。官方例子多是 0.6B–1.7B 量级，单卡宽松。

**成功判据**：
- reward 曲线在几十到几百 step 内明显上升
- 你能在 wandb/TensorBoard 里指出 entropy 从多少降到多少
- **你能说出 `num_generations`（默认 8）、`beta`（默认 0.0，因此没有 reference 模型也没有 KL 曲线）、`loss_type`（默认 `"dapo"`）三个值分别是什么，以及它们对应日志里哪条曲线**

**目的是熟悉操作界面，不是解决真实任务。** 若看到 `RuntimeError: Attempting to use the same CUDA device for multiple distinct roles`，说明误开了 `vllm_mode="server"`，单卡请用默认 colocate。

### 实验 5：自定义单轮工具调用环境 + rollout_func（1×4090，半天）

换成简化版工具调用场景（从 SAR 抽最简子任务，如「给定单个火源，选择正确灭火资源类型」），用 `rollout_func` 接自己写的最简环境，reward 用二元信号。用 1.7B —— 8B 即使 4bit，加 group 内多条样本的 KV cache，24GB 极紧。

**成功判据**：
- GRPO 后准确率明显超过 SFT-only 或零样本基线
- **加一个 callback 记录每 step 的「零方差组数 / 总组数」并画到 wandb 里，这条曲线你能读懂且至少有一个非零点**（TRL 默认不记录这个量，「你能观察到零梯度现象」必须先把它做出来才可观察）
- 顺手把 Dynamic Sampling 的过滤逻辑写进 `rollout_func`（TRL 官方不支持这个组件），这是本实验最该产出的代码

### 实验 5.5：单个 SAR episode rollout，不训练（1×4090，1–2 天）

**这一步是实验 5 到 6 之间必须插入的桥。** 用 `rollout_func` 真的调 `SAREnv`/Controller 跑完一个 episode，但不做梯度更新。

**成功判据**：拿到一条完整轨迹的 `(prompt_ids, completion_ids, logprobs)`，且 token 数与 vLLM 返回一致。

**这一步的真正价值是暴露吞吐问题**：301s/episode × `num_generations=8` = 40 分钟一个 step。越早看到这个数字，越早能决定是降 `num_generations`、并行 episode，还是重新设计 rollout 调度。

### 实验 6：完整 SAR 环境 + team-level reward（≥2 卡或 1×80GB，数周）

`rollout_func` 跑完整 episode，reward 用 `coverage`/`transport_rate`/`finished` 组合，多 worker 共享正在训练的权重（coordinator 仍免训练）。

**算力**：4090 上不可行。需要两张卡（TRL server 模式硬要求 trainer 与 vLLM 分处不同 CUDA 设备）或单张 80GB。**建议后者** —— 消费级 4090 无 NVLink、P2P 被禁，权重同步会退化到走主机内存中转，两张 4090 不一定比一张 A100 划算。降级路径：worker 用 Qwen3-4B、`num_generations` 降到 4、只训一个 worker 角色。

**成功判据**：worker 权重在 held-out 评估集（未出现过的 scene+seed 组合）上，team-level `finished` 比例或 `coverage` 相对 SFT-only 基线有统计显著提升，**且用 pass^k 而非单次运行确认这个提升不是噪声**。

**估时**：数周，与配套路线图阶段 5（6–10 周）对齐。

---

## 四、误区清单

1. **以为 `tool_calls.function.arguments` 该是 JSON 字符串** → transformers 官方期望的是 **dict**，与 OpenAI 线上 API 相反。你的 `schema.py:19` 已经是 dict，这关天然通过；反倒别为了「像 OpenAI」而手动 `json.dumps`
2. **loss mask 只 mask 了 user/system，忘了 role=tool 的内容** → 模型被训练去「预测环境返回值」，完全错误的监督信号
3. **不看序列长度分布就开训** → `max_length` 默认 1024 + `keep_start` 截断会把 8k 样本的 assistant 目标整段丢弃，labels 全 -100，**不报错**，曲线看似正常
4. **以为 LoRA 一定省显存所以不用管序列长度** → 大词表下 logits 项与参数量无关、只与 seq_len 有关（12k 序列 18.7GB，比基座还大），且 TRL 的 `chunked_nll` 优化与 PEFT 不兼容，LoRA 反而拿不到这项红利
5. **训练数据用一种 thinking 模式、推理用另一种** → 你的日志里 `reasoning_content` 一条都没有，若推理时默认开思考，格式分布与训练数据不符
6. **LoRA 沿用全参数的学习率** → LoRA 合适 lr 高一到两个数量级，`SFTConfig` 默认 2e-5 对 LoRA 偏低，起点应为 1e-4
7. **用旧 checkpoint 的 rollout 更新新策略且不做 importance sampling 修正** → 违反 on-policy 前提，轻则收敛慢重则发散
8. **GRPO 只看平均 reward，不监控组内方差** → 平均不涨可能是大量组零方差根本没梯度。且要认对归因：零梯度来自 `r - mean = 0`，关掉 `scale_rewards` 解决不了
9. **以为 DAPO 的 Dynamic Sampling 是个开关** → TRL 官方标注不支持，过滤逻辑要自己写
10. **纯 on-policy 下切 GSPO 期待方差改善** → TRL 官方说此时等价于不做修改，要先真的走轻度 off-policy
11. **以为有 KL 曲线可看** → `beta` 默认 0.0 时 reference 模型根本不加载，没有这条曲线；要有就得显式设 `beta>0` 并为多一份模型腾显存
12. **单卡上开 `vllm_mode="server"`** → TRL 要求 vLLM 与 trainer 分处不同 CUDA 设备，同卡直接 `RuntimeError`；单卡必须 colocate
13. **reward 只用 episode 是否成功这一个稀疏信号** → 浪费你最大的工程优势（已有 `subtasks.csv` 细粒度信号），且纯稀疏 reward 在长 episode 上效率极低
14. **多 worker 共享权重时把各自轨迹当独立同分布样本** → 一个 worker 的失败可能是另一个错误决策的连锁反应，当成「这个 worker 训练不好」来惩罚会引入错误信用分配
15. **prompt 精简/工具窄化和 RL 训练同时进行** → 出问题时分不清是 prompt 变了还是训练坏了。应先固定 prompt/工具集，SFT 验证格式稳定后再进 RL
16. **本地 vLLM 部署后不做 A/B 就切换** → 8B 的指令遵循能力天然弱于顶级闭源模型，直接切换可能让整体成功率骤降，误判为「训练没起作用」；且忘加 `--tool-call-parser hermes` 会让 tool_calls 字段整体缺失
17. **评估集和训练数据用同样的 scene+seed** → SAR 只有 5 个 scene 且现有数据 seed 100% 是 42，极易做出过拟合假象
18. **评估时不锁采样参数** → 同一 checkpoint 在不同 temperature 下格式合法率差十几个点，不同实验的数字不可比
19. **把 pass@k 高当成任务已解决** → pass@k 高但 pass^k 低意味着靠运气凑出偶尔成功
20. **一次性上 packing + 多卡 + 各种优化再开始第一次训练** → 出 bug 无法定位。先在最简配置下跑通正确性，再逐个加

---

## 五、不需要学的东西

| 内容 | 为什么不学 | 例外 |
|---|---|---|
| 从零实现 Transformer（手写 attention/FFN 前反向） | 用现成 Qwen3 权重 + HF/PEFT/TRL 生态 | — |
| 预训练相关的一切（数据清洗、tokenizer 训练、大规模预训练分布式） | 与「微调现成模型做特定任务」完全无关 | — |
| RLHF 的人类偏好标注流程（标注员管理、Bradley-Terry） | 你的 reward 来自环境反馈，不是从人类偏好学 reward model | — |
| PPO 完整数学推导（GAE 偏差方差分析、value function 参数化） | GRPO/DAPO/GSPO 用组内相对 reward 绕开了显式 value function | — |
| 多机分布式通信优化（NCCL 调优、拓扑设计） | 8B + 当前资源规模，单机够用 | **单卡到双卡的第一步要学**：`CUDA_VISIBLE_DEVICES`、`accelerate launch`、4090 无 P2P 对权重同步的影响 |
| 量化训练的数值细节（fp8 实现、量化误差理论） | 用现成 bf16/QLoRA 配置 | **QLoRA 那四个参数名是必需操作知识**（`load_in_4bit` / `bnb_4bit_quant_type` / `bnb_4bit_use_double_quant` / `bnb_4bit_compute_dtype`） |
| 从头设计 RL 环境框架（Gym 接口规范理论） | 已有成熟 SAR 环境和 Controller，只需学怎么接进 rollout 接口 | — |

---

## 六、信源说明与待核实项

**已核实**（HuggingFace / TRL / vLLM / SGLang / Qwen 官方文档，以及本仓库实测）：TRL 的 `max_length` 默认值与 `keep_start`、`assistant_only_loss` 及其 `{% generation %}` 前置条件、`chunked_nll` 与 PEFT 不兼容、`beta` 默认 0.0 不加载 reference、`importance_sampling_level="sequence"` 的生效条件、DAPO 五组件及 Dynamic Sampling 不支持、`vllm_mode` 的分卡硬约束、vLLM 与 SGLang 的 prefix caching 均默认开启、FA3 Hopper only、TRL 支持的 vLLM 版本区间、Qwen3-8B 的 `vocab_size=151936` 与 36 层 / 8 KV head / head_dim 128、LoRA=arXiv:2106.09685、DeepSeekMath=arXiv:2402.03300、GSPO=arXiv:2507.18071、DAPO=arXiv:2503.14476。

**本仓库实测**（全量普查，非抽样）：第零节全部数字；`logger.py:111` 只存工具名；`openai_client.py:292` 硬编码 `finish_reason`；`context.py:38` 的 `recent_messages=12` 剪枝影响 44.6% episode；`.venv` 无任何 ML 框架；`requirements.txt:135,139` 的旧 torch/transformers pin。

**待核实 / 必须实测**：

- **`qwen3_training.jinja` 是否把 `tool_calls` 包在 `{% generation %}` 块内** —— TRL 文档对它和 `qwen3_vl_training.jinja` 措辞不一致，直接决定带 tool_call 的 turn 是否进 loss。**实验 1 必测**
- **`chunked_nll` 与 PEFT 不兼容后的实际行为** —— 官方只写 "Not compatible"，未说是报错还是静默回退。用小 batch 对比两种 `loss_type` 的 `torch.cuda.max_memory_allocated()` 即可确认
- **logits 显存的精确倍数** —— `seq_len × vocab × 2B` 可闭式计算，但「CE 内部 fp32 上采样」和反向梯度的确切驻留时长依赖实现版本，18.7GB 是估算上界。用 `max_memory_allocated()` 实测
- **Qwen3 tokenizer 下的真实长度分布** —— 第零节是按 DeepSeek usage 标定的 3.21 字符/token 换算，可能 ±15% 偏差。阶段 0 重测
- **4090 无 P2P 对 TRL server 模式权重同步吞吐的影响** —— 无官方数据也无公开实测，实验 6 时自测
- **DAPO 的会议归属** —— arXiv:2503.14476 页面 Comments 只有 "Project Page"，无 venue 字段。此前文档写的「NeurIPS 2025」查不到出处，已删除
- **配套路线图 L111 的 τ-bench 数字** —— 路线图写「pass^1 >60% → pass^8 <25%」，但 arXiv:2406.12045 摘要给的是「gpt-4o 成功率 <50%」，`>60%` 对不上，可能是分域数字或记错。pass^k 的论证方向不受影响，但引用前应读全文
