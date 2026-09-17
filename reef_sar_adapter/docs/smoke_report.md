# Smoke 报告 — 基线批实测 + TOKEN_CAP 冻结（A5，2026-09-17）

> 卡片：`t_390b487c`（[Reef-A5] 基线批 + 冻结 TOKEN_CAP + smoke 报告）
> 上游：A4 `t_8b66bb3a`（首个 step 闭环，方案 A）；campaign 记录
> `docs/campaign-20260917-sar-smoke.md`；spec
> `/home/wyh/daily_work/LLaMAR/.hermes/spec/20260917-reef-sar-evolution.md` §6/§7。
> 证据：`docs/evidence/a5-baseline.json`（逐场数字）、`docs/evidence/a5-tree-audit.json`（审计）。

## 0. 结论卡（30 秒）

| 项 | 结论 |
|---|---|
| 基线批 | **4/4 场 exit 0，无 infra 失败**；全部 35/35 步、`max_steps_reached`；ref 逐场 = 冻结值 `b243b2f0…` |
| 成本实测 | 单场 **0.78M–1.21M billed tokens / 4.4–7.1 min**；4 场合计 **3,994,468 tokens / 21.5 min**（串行） |
| TOKEN_CAP | **冻结 = 1 200 000**（p95：nearest-rank 1 210 561 / linear 1 182 395；依据见 §4），已写入 `method.py` |
| 公式适用性 | **transport_rate 未饱和**（4 场 4 个不同值，0.5–0.778，无一取 1.0）→ **不启用 §6 fallback**；且数据反向否定了两个 fallback 候选（`finished` 4/4 全 False、coverage 3/4 同分） |
| 附带发现 | 同任务同树重跑方差大：4/0 任务两次 current 跑出 transport **1.0 vs 0.5**（score 差 0.69）——R=1 的噪声量级，与 §9-2 一致 |
| 服务态 | 8900 服务未重启（pids 2417837/2417867，step 4 不变）；运行实例仍持有 freeze 前的 `TOKEN_CAP=1_000_000`，下一个要按冻结方法评分的 step 前需重启（§7） |

## 1. 范围与口径

本卡做三件事：

1. **基线批**：用**手工构造的 `REEF_SAR_TREE`**（overlay = seed release 的当前 tree 内容，逐字节 sha 校验）
   直接调 `reef-sar` runner 跑 smoke 套件 4 个任务的 **current 侧**各一场；
   `LLAMAR_REF` 用 A4 冻结值 `b243b2f0f563c14fded6cf4b01b5b2a99d236262`。
   不经 reef 服务（不占 gate、不产生 step），因此本批**不触发 propose/gate/verdict**。
2. **冻结 TOKEN_CAP**：按 §6 冻结程序，用基线批 `effective_billed_tokens` 分布取 p95，写死 `method.py`。
3. **公式 fallback 核查 + smoke 报告**（本文件），含 §9 风险清单逐条复核。

口径对齐（与 gate 两侧完全一致）：scene3、`--mode semantic`、`max_steps=35`（task JSON 权威）、
`--provider openai`、binding `https://cf.api.fan/v1` + `deepseek-flash`（A4 冻结 key，指纹
`0980fb1a8368`）、`--truth-output-dir {tree}/workspace/truth`、产物收集 `run_metrics.json` /
`eval_metrics.json` / `summary.csv`。overlay 的四个文件与 seed release `941a5cb7` 内容一致：

| overlay 文件 | sha256 | 对照 |
|---|---|---|
| `prompts/coordinator/system.semantic.md` | `ba01a8ca36d8b62b…` | == seed release / == ref 原文 |
| `prompts/worker/system.md` | `e0592d3e07ae5806…` | == seed release / == ref 原文 |
| `rules.md` | 基线占位符（追加空） | == seed release |
| `sar_config.json` | `{"llm": {…}}`（reef 评估期渲染的 binding 形态） | 与 step-4 逐场一致 |

先跑 2 步 preflight（s3-a2-s0，21 648 tokens / 54.5 s，exit 0）验证链路：物化 4927 文件、
overlay 写 2 个 prompt、端口分配、experiment → eval 收集、truth 隔离——通过后才开基线批。

## 2. 基线批实况（4 场，串行）

| 任务 (agents/seed) | steps | end_reason | finished | transport_rate | coverage | load_balance_b | billed tokens | 墙钟 | score@1.20M |
|---|---|---|---|---|---|---|---|---|---|
| 2 / 0 | 35 | max_steps_reached | False | 0.6667 (12/18) | 0.7143 (5/7) | 0.8750 | 775 422 | 262 s | 0.7999 |
| 2 / 10 | 35 | max_steps_reached | False | 0.7222 (13/18) | 0.7143 (5/7) | 0.8710 | 985 700 | 331 s | 0.8192 |
| 4 / 0 | 35 | max_steps_reached | False | 0.5000 (9/18) | 0.5714 (4/7) | 0.1515 | 1 210 561 | 273 s | 0.3455 |
| 4 / 10 | 35 | max_steps_reached | False | 0.7778 (14/18) | 0.7143 (5/7) | 0.5789 | 1 022 785 | 426 s | 0.7810 |

- 4/4 `exit_code=0`、`eval_collector_exit_code=0`、collected 集合齐全（missing 空）。
- 4/4 `framework_error_counts` 全零（`worker_busy` / `task_not_routable_yet` / `unknown_task_id`）、
  `missing_error_code_rows=0`；失败工具行 4/1/9/11 属 agent 级动作失败（行为数据，非框架错误）。
- `transport_rate` = 已完成子任务 / 总子任务数（`base_checker.py:162-170`），scene3 分母恒为 18
  = 3 个火灾 ×3（NavigateTo/UseSupply/EndFire）+ 1 名人员 ×5（NavigateTo/Carry/DropOff/
  NavigateTo(deposit)/Spot）+ 2 个必要水库 ×2（`checker.py:88-117`；与 agent 数无关）；括号内为
  已完成子任务数。`coverage` = 已覆盖对象 / 总需覆盖对象（分母 7 = 3 火 + 1 人 + 1 仓库 + 2 水库）。
- 审计（`a5-tree-audit.json`，**AUDIT PASS**）：每场 `run_meta.llamar_ref == b243b2f0…` 且
  `llamar_ref_source == LLAMAR_REF`；truth 只在 `workspace/truth/`（`truth_trace.jsonl` +
  `truth_manifest.json`），`sar/out` 无任何 truth 类文件；config 快照已脱敏；tree 顶层残留仅
  `workspace/ sar/ overlay/`。

**闭环验证结论**：

- 闭环本体（A4 已证）：`/reef/train` → propose → admission → gate 8 场 → verdict → 不改变——
  走通并以 `reject` 收口，promote/pull 因无 pending release 未触发（设计内）。
- 本卡补证：**current 侧在隔离环境可复现地跑干净**（4/4 全链路无 infra 失败），说明 A4 step-4
  的 3/4 受污染 pair 来自"并发 gate + worker 竞态（defect 4）"而非 current 侧本身。
- 仍未验证：**promotion-grade 的 verdict 与 promote/pull 两腿**（需先合 defect 4、换新 ref）。

## 3. 成本实测（§7 成本模型校准）

单场口径（本批 4 场）：

| 指标 | 值 |
|---|---|
| billed tokens | min 775 422 / mean 998 617 / max 1 210 561 |
| 墙钟（runner 全程） | min 262 s / mean 323 s / max 426 s |

推算（按本批均值；candidate 侧成本≈current 侧）：

| 规模 | episode 数 | billed tokens | 墙钟（2 workers，A4 配置） |
|---|---|---|---|
| smoke step | 8（2×4） | ≈ 8.0 M | ≈ 21.5 min（A4 实测 26 min，含 proposer/启动开销） |
| 正式 step | 40（2×20） | ≈ 40 M | ≈ 1.8 h（serial ≈ 3.6 h） |
| proposer | 1 次 call | 千级 | 1.5 s（A4 实测） |

本卡自身消耗：基线批 3 994 468 + preflight 21 648 ≈ **4.02 M billed tokens**；
runner 墙钟合计 1292 s + 54.5 s ≈ **22.4 min**（批运行窗口 19:36–20:02）。
0 步失败的成本参考（A4 step-4）：两次 0 步失败分别烧 ≈ 280 k / 288 k tokens（纯启动/注册开销）；
另一场 2 s 端口竞态死亡的烧 0（没有任何 LLM 轮）。

## 4. TOKEN_CAP 冻结

按 §6 冻结程序（"实测 billed_tokens 分布，取 p95 定 TOKEN_CAP"）：

```
样本 = 本基线批 4 场（current tree，全部 35 步干净跑完）
values = [775 422.4, 985 700.0, 1 022 784.8, 1 210 561.0]
p95 (nearest-rank) = 1 210 561.0        # n=4 → 第 4 位
p95 (linear)       = 1 182 394.6
TOKEN_CAP = 1 200 000                   # 两种口径都在 1.18–1.21 M 区间，取整到 1.2 M
```

- **冻结值已写入 `reef_sar_adapter/method.py`**（`TOKEN_CAP = 1_200_000`，注释含冻结日期、样本、
  数值依据、重测规则）。
- 取整不改变任何 verdict：cap 只决定惩罚项在哪里饱和（`min(1, billed/CAP)`），1.18 M 与 1.21 M
  的差异 < 1 %，对分数影响 < 0.003（见 evidence 里三档 cap 的逐场分数对照）。
- 灵敏度（供拍板参考）：本批 4 场在新 cap 下的惩罚项为 0.129 / 0.164 / 0.200 / 0.171
  （权重 0.2 满额对应 1.21 M 那场，已 clamp）。历史样本里出现过 1.33 M 的干净 current 跑
  （A4 step-4 current-2，success/29 步）——本 cap 比 "A4 + 本批 current 侧联合" 的 p95
  （nearest-rank 1 331 765 / linear 1 302 226）低 ≈ 10 %，属**有意**偏离：本卡按 spec 以
  "本基线批" 为样本；联合口径已记录在 evidence，若后续重测可再取。
- 规则：**此后改这个字面量（或权重）即方法变更**，须重跑基线（写死进方法模块的注释）。

## 5. 公式适用性结论（fallback 核查）

判据（对照 §6 fallback："若基线显示 transport_rate 在 35 步内普遍饱和（多场同分）"）：

| 检查 | 数据 | 结论 |
|---|---|---|
| 本批 transport 区分度 | 4 场 → **4 个不同值**（0.5000 / 0.6667 / 0.7222 / 0.7778），无一为 1.0 | 未饱和 |
| 叠加 A4 step-4 current 侧 | + {0.5556, 1.0000, 0.8889, 0.0(0 步失败)} → 8 场 8 个不同值；去掉 0 步失败 7 场仍 7 个不同值 | 未饱和 |
| fallback 候选①：加 `finished` 项 | 本批 4/4 `finished == False`（全是 max_steps 收尾）；A4 current 侧也只有 1 场 success | **反向证据**：用 finished 会让 4 场全部同分，比 transport 差 |
| fallback 候选②：加 coverage 项 | 本批 coverage 3/4 同为 0.7143（5/7），A4 侧亦多重复 | **反向证据**：coverage 比 transport 更饱和，做 SR 项会加噪 |

**结论：transport_rate 在 scene3 × 35 步内未饱和，§6 fallback 不启用**，SR 保持
`1.0 × transport_rate`；公式与权重不动（本卡按要求不自改公式）。

附带的量化观察（供后续拍板，不改）：

- transport 粒度 1/18 ≈ **0.0556 / 子任务**；token 项权重上限 0.2 ≈ 3.6 个子任务等价。新 cap 下
  "candidate 相对 current 多用 50 % token" 只换来 ≈ 0.03 分差——token 压力在 0.6–1.2 M
  成本区间内偏弱；若要让成本约束更硬，需调 `TOKEN_WEIGHT`（方法变更，重测基线）。
- **同任务同树重跑方差**（R=1 噪声，§9-2 直接证据；任务对同名 current 跑）：
  2/0：0.5556 → 0.6667（A4 那场 framework@21 收尾）；4/0：**1.0000（success/29 步）
  → 0.5000（max_steps/35 步）**，分数 1.0368 → 0.3455；4/10：0.8889 → 0.7778。
  即：同一棵树同一任务，单跑之间 transport 可差 0.5（= 9 个子任务，18 的一半）。正式期用
  R=1 + margin=1 时，这个量级的系统方差大概率吞掉真实提升——建议正式套件前用本数据重估
  R 与 margin（未决项）。

## 6. §9 风险清单逐条复核

| # | 风险 | 复核结论 | 证据 |
|---|---|---|---|
| 1 | 成本：每 step 8–40 场；N=3/日 提案上限 | 量化：smoke step ≈ 8.0 M tokens / ≈ 22–43 min；正式 step ≈ 40 M / ≈ 1.8 h（2 workers）。提案速率上限在分析 agent 侧（本卡未触达，0 提案）。 | §3；A4 记录 |
| 2 | R=1 + margin=0 发布会含噪声 | 证实且量级可观（同任务重跑 Δtransport 达 0.5，Δscore 0.69）。smoke 只证闭环不证提升——本结论与 spec 一致；margin/R 建议见 §5 末。 | §5 方差段 |
| 3 | 过拟合 gate 套件；分析 agent 不见 truth | 未触达（本卡无 holdout、无分析 agent）。通道侧：指令只走 `/reef/train` 文本，adapter/runner 无读 truth 的路径；truth 外置 + `sar/out` 无 truth（审计）。 | `a5-tree-audit.json` |
| 4 | 真值隔离 | **PASS**：4+1 场全审计通过（truth 仅 `workspace/truth/`，收集集无 truth 文件）。 | `a5-tree-audit.json` |
| 5 | residue：产物收拢 workspace/+sar/，whitelist 覆盖 | 手跑 tree 顶层 = `workspace/ sar/ overlay/`（无其它残留）；物化检出无 `.git`、无外部写入。reef 侧 `forbid_residue` smoke 期关闭（与 A4 相同）。 | `a5-tree-audit.json` |
| 6 | prompt 变体一致：coordinator 三条全进 tree；gate 固定 semantic | gate 固定 semantic 成立（runner 常量 `MODE="semantic"`，config 侧 `mode` 为 pinned key）。**注**：tree 实际只含 `system.semantic` 一条 coordinator entry——这是 §2.2 第四轮修正案（B7）的收紧，§9-6 的"三条全进"措辞已被其取代；worker 一条。 | `runner.py:165`、`serve.yaml` seed、audit overlay sha |
| 7 | local executor 无沙箱 → 变异面限文本/config | 成立：descriptor 未声明 `code_extension`（`OPTIONAL_NODE_KINDS` 之一，未声明即在 admission/render 拒），`method.ANCHORS` 白名单 4 entry，config 仅 `CONFIG_RUN_KEYS` 两个键可过、其余 pinned/忽略并告警。A4 step-4 的 mutation 实际落在 `worker-system`(skill)。**残余**：runner 用 `uv run --extra sar` 在物化检出里执行实验代码——代码来自冻结 ref（非变异面），可接受但值得在正式期记录。 | `descriptor.yaml`、`method.py` ANCHORS、reef `descriptor.py:9-10,61-80` |
| 8 | 基线漂移：LLAMAR_REF 每 campaign 冻结；adapter 包版本锁定 | ref 冻结成立（descriptor 字面量；本批 4/4 run_meta 核验）。**偏离**：adapter 是 editable 安装（`direct_url` 指向 worktree），不是 pip 锁定版本——代码改动会静默进入下一次服务启动的评分路径（本卡 freeze 即依赖此机制生效）。建议正式期打版本/记 commit 哈希。 | `a5-tree-audit.json`；reef venv `direct_url.json` |

## 7. 交接与未决项

1. **运行中的 reef 服务持有 freeze 前的 `method` 模块**（boot 时 `importlib` 解析，
   `strategies.py:252-275`；A4 已用同一机制证明模块不会热重载）。即：下一个要按冻结方法评分的
   gate step 之前，需要**重启服务**（按 A4 的"保留 frozen key"程序：从 `/proc` 取 key → 直接以
   该 key 启动，勿走会读取新 `.env` 的 launcher），否则该 step 会用旧 `TOKEN_CAP=1_000_000` 评分。
   服务当前健康（healthz ok、scenario `sar-smoke` step 4、head `941a5cb7` 未变）；本卡全程未触碰服务。
2. **defect 4（worker MCP 竞态）未合并**：修复在 `.worktrees/t-7c303cb1`（未提交）。本批串行
   4/4 未复现（与 A4"单场时窗口通常能赢"一致）；gate 并发 2 场时会复发。
3. **未决（需人拍板，未实施）**：
   - 正式套件前是否合并 defect-4 修复 → 若合并则 ref 变更 → **基线需重测**（本报告与 TOKEN_CAP
     的样本口径将随之更新）；
   - R 与 margin 取值（§5 方差数据支持重新评估）；
   - adapter 包的版本锁定方式（§9-8 偏离项）。
4. 本卡**未 commit**（按卡片要求）；新增未跟踪产物：`docs/smoke_report.md`、
   `docs/evidence/a5-baseline.json`、`docs/evidence/a5-tree-audit.json`，以及 `method.py` 的
   TOKEN_CAP 冻结改动与 `tests/test_method.py` 的冻结值 pin 测试（worktree 内，未提交）。
   适配器测试：**154 passed**（153 + 1 pin），ruff 全过。

## 附：复现命令（基线批）

```
# tree 构造：overlay 四文件 = seed release 内容（sha 见 §1），key 取 A4 冻结指纹源
# 单场：
cd /home/wyh/daily_work/LLaMAR/.worktrees/reef-a
REEF_SAR_TREE=/home/wyh/.cache/reef-sar-baseline/trees/s3-a2-s0 \
REEF_SAR_OVERLAY=/home/wyh/.cache/reef-sar-baseline/trees/s3-a2-s0/overlay \
LLAMAR_REPO=/home/wyh/daily_work/LLaMAR \
LLAMAR_REF=b243b2f0f563c14fded6cf4b01b5b2a99d236262 \
UV_CACHE_DIR=/home/wyh/.cache/uv \
/home/wyh/daily_work/reef/.venv/bin/reef-sar '{"scene":3,"agents":2,"seed":0,"max_steps":35}'
```

原始数据：`/home/wyh/.cache/reef-sar-baseline/`（`metrics.jsonl`、`logs/<slug>.{stdout,stderr}`、
各 tree 的 `workspace/run/`、`sar/out/`、审计脚本输出）。
