# A100 首跑任务书 — AI2Thor Unity 端到端验证

> **给执行方**（A100 / 任意 GPU 主机上的 agent 或操作者）：按本清单从上到下执行。
> 本任务书 = 执行口径与验收标准；详细背景与排障细节见同仓 `docs/system_docs/ai2thor_a100_runbook.md`。
> **总原则**：上一级不过不跑下一级；一切判定看产物证据，不看自报；**发现问题不就地改代码**（见 §6 红线）。

**目标**：在真实 GPU + Unity 上完成三级递进验证（L1 fake → L2 unity 冒烟 → L3 端到端），
产出结构化报告（§7），逐条回答"本地无 GPU 无法验证的 8 个首跑问题"（§8）。

## 0. 速览

| 步 | 内容 | 通过判据 | 预计耗时 |
|----|------|----------|----------|
| 准备 | 前置检查 + `uv sync --extra ai2thor-unity` + 环境变量（+ L3 用 `.env`） | 无报错 | 10–20 min（含下载） |
| L1 | fake 冒烟（无 GPU 依赖） | `exit=0` | 秒级 |
| L2 | unity 冒烟（G1/G5 门禁 7 项，不接 LLM） | `exit=0` 且 checks 全 true | 首跑 5–15 min（含 Unity build 下载） |
| L3 | 端到端实验（真 LLM）：先 8 步短跑 → 再 50 步完整 | 产物完整 + 指标合理（§5） | 短跑 ~5 min；完整 20–60 min |
| 报告 | 汇总到 `reports/a100_firstrun_<日期>/` | 按 §7 模板 | — |

## 1. 前置检查（逐条确认）

```bash
cd <repo>
git fetch origin && git checkout feat/ai2thor-scene-adaptation
git log --oneline -3
#   顶部 = 本任务书提交；往下应可见 a796b80（文档收口）与 fc591c4（P5-4 unity 接线）
ls A100_TASK.md docs/system_docs/ai2thor_a100_runbook.md
#   两个文件都在 = 拉到的版本正确
```

- `nvidia-smi` → 能看到 GPU 与驱动（看不到 → 记录现象并停止，属环境问题）
- `uv --version` → 缺失则 `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `apt-get install -y libvulkan1`（无 sudo 权限则记录并求援）；`df -h ~` 空闲 ≥ 6 GB
- 出网确认：PyPI + GitHub（uv 拉依赖）+ ai2thor 官方 build 服务器（首次下载 Unity Player，缓存到 `~/.ai2thor/releases`）

## 2. 环境装配

```bash
cd <repo>
export no_proxy="localhost,0.0.0.0,127.0.0.1"
unset PYTHONPATH
export PYTHONPATH="src"        # 必须：本仓 src/a2a 要压过 site-packages 里的 a2a-sdk

uv sync --extra ai2thor-unity  # = ai2thor + CUDA torch + sentence-transformers

export LLAMAR_AI2THOR_MODE=unity
export LLAMAR_AI2THOR_HEADLESS=1      # 无显示主机（缺省即 1，显式无害）
export LLAMAR_AI2THOR_PLATFORM=cloud  # CloudRendering：无显示 GPU 渲染
# export LLAMAR_AI2THOR_GPU_DEVICE=0  # 多卡时按需指定
```

> ⚠️ `unset PYTHONPATH` + `export PYTHONPATH="src"` 必须在**每个新 shell 会话**执行，后文所有命令都假设已完成本节。不设置时，多个命令会因 `a2a-sdk` 遮蔽 `src/a2a` 而报错。
> ⚠️ `LLAMAR_AI2THOR_MODE` **只影响探针脚本的默认模式**；实验 CLI 不读取它——L3 必须显式传 `--mode unity`。
> L3 需要仓库根 `.env`：四项**小写**字段 `provider` / `api_key` / `api_base` / `model`，凭据由用户提供；**绝不要提交 `.env` 进 git**。L1/L2 不需要它（不接 LLM）。
> 本仓自带 `src/a2a` 与全部依赖（`pyproject.toml` 无本地路径依赖），**不需要**开发机的 MARoS editable 包。

## 3. L1：fake 冒烟（先证明工具链没坏）

```bash
mkdir -p reports/a100_firstrun_$(date +%Y%m%d)
uv run python scripts/ai2thor_runtime_smoke.py --mode fake \
  --report reports/a100_firstrun_$(date +%Y%m%d)/smoke_fake.json
echo "exit=$?"
```

**通过 = `exit=0` 且报告 `status=="ok"`。** 失败 → 把报告 JSON 与 stdout 原文留档（大概率是 PYTHONPATH/依赖问题）。

## 4. L2：unity 冒烟（G1/G5 门禁；不接 LLM）

```bash
uv run python scripts/ai2thor_runtime_smoke.py \
  --scene FloorPlan1 --agents 2 --timeout 900 \
  --report reports/a100_firstrun_$(date +%Y%m%d)/unity_smoke.json
echo "exit=$?"
```

> 首跑含 Unity build 下载，`--timeout` 直接给 900；下载缓存好后重跑 300 足够。

退出码：**0** 成功｜**1** 运行时异常或 gating 断言失败（看报告 `checks` 哪条 false 与 `error.traceback`）｜**2** ai2thor 包未装（重跑 `uv sync --extra ai2thor-unity`）｜**3** 超时（看 `~/.ai2thor/releases` 是否仍在下载 → 加大 `--timeout` 重试）。

7 项 gating 断言（报告 `checks` 逐条可查）：`import_ok` / `controller_started` / `multi_agent_events` / `per_agent_metadata` / `executor_round_ok` / `adapter_surface_ok` / `stop_clean`。

**通过 = `exit=0` 且 7 项全 true。** 同时留存报告 `info` 字段（ai2thor 版本、`MoveAhead` 位移记录、`GetReachablePositions` 数量、原始 `Done` 动作接受性——它们是 §8 的证据来源）。

排障：runbook §5 排障表；Unity 侧问题先看 Player.log（`~/.config/unity3d/Allen Institute for Artificial Intelligence/AI2-THOR/Player.log` 最后 50 行）。

## 5. L3：端到端实验（unity + 真 LLM，按回合计费）

### 5.1 先短跑 8 回合

```bash
uv run python -m ai2thor_orch.experiment \
  --task 3_transport_groceries --scene FloorPlan1 --agents 2 --seed 42 \
  --mode unity --max-steps 8 --wall-clock-limit 3600 \
  --coordinator-port 18080 --agent-base-port 18191 \
  --log-dir reports/a100_firstrun_$(date +%Y%m%d)/l3_short
echo "exit=$?"
```

- 退出码：**0** = `finished` / `verified_completion`；**1 = 不必然失败**——8 步通常跑不完任务，`end_reason=max_steps_reached` + `exit=1` 属**预期**。判定看 `summary.json` 指标（下方清单），不要以退出码否定短跑。
- 断言标准只看产物：下列 5 条逐条给证据。

### 5.2 短跑产物验收（逐条核）

1. `summary.json`：`metric_schema_version=2`；记录 `end_reason` / `verified_completion` / `goal_coverage` / `transport_rate` / `action_success_rate` / `balance` / `timeout_count`
2. **`timeout_count == 0`**：非 0 = 真机动作延迟超过 `step_timeout`（默认 60s/回合），记录具体超时回合与耗时
3. `trajectory.csv` 行数 = 回合数 = verifier trace 行数；每行 Actions / Successes 含 2 条（= `--agents`）agent 动作结果
4. `summary.csv` 单行聚合（每步覆盖写）；`events.ndjson` 为 coordinator 语义事件流（assign_task / reply_to_help / cancel_task / send_message）
5. 无 `worker_busy` / `task_not_routable_yet` / `unknown_task_id` 一类路由错误

### 5.3 短跑产物正常 → 完整跑（50 步预算）

```bash
uv run python -m ai2thor_orch.experiment \
  --task 3_transport_groceries --scene FloorPlan1 --agents 2 --seed 42 \
  --mode unity --max-steps 50 --wall-clock-limit 3600 \
  --coordinator-port 18080 --agent-base-port 18191 \
  --log-dir reports/a100_firstrun_$(date +%Y%m%d)/l3_full
echo "exit=$?"
```

- 建议 `tmux` / `nohup` 跑，防断连。
- 若 50 步仍 `max_steps_reached`：记录 `transport_rate` / `action_success_rate`；可选做同参 `--mode fake` 对照跑（按 runbook §2.3：fake 正常而 unity 异常 → 问题在仿真侧而非编排侧）。
- 可选对照（成本很低）：同参 `--mode fake --max-steps 8` 跑一遍，两条 `summary.json` 供对比。

## 6. 红线（执行方必须遵守）

1. **不改代码**：本任务只运行与观测。发现代码/功能问题 → 收集证据（原始 traceback、报告 JSON、Player.log 关键段、复现命令）写进报告，**回开发侧修复**（不在 A100 就地改代码、不提交）。
2. **环境层修复允许**（装 `libvulkan1`、修 `~/.ai2thor` 属主/权限、加大 `--timeout`、删半成品 build 重下、`pkill -f AI2-THOR` 清残留）——每条都要写进报告"处理记录"。
3. 不提交 `.env` / 密钥 / 凭据进 git；不改 git 历史；不 force push。
4. L3 是真 LLM 计费：先短跑后完整；**不要**跑 benchmark / sweep。
5. **跨机协作纪律**（开工先 `git pull --ff-only`、小步推、不留孤本）：详见 runbook「双机协作纪律」小节。

## 7. 报告产出（强制交付物）

目录 `reports/a100_firstrun_<YYYYMMDD>/`，包含：

- `smoke_fake.json`（L1 报告）、`unity_smoke.json`（L2 报告）
- `l3_short/`、`l3_full/`（L3 run 目录，至少保留 `summary.json` / `summary.csv` / `trajectory.csv` / `events.ndjson` / `metadata.json` / `token_usage.csv`）
- **`A100_FIRSTRUN_REPORT.md`** 主报告，模板：

```markdown
# A100 首跑报告（<日期>）

## 1. 环境
- 主机 / GPU / 驱动（nvidia-smi 摘要）/ OS / python / uv
- 分支与 commit（git log -1 原文）；ai2thor 版本（L2 报告 info）
- 实际生效的环境变量清单

## 2. 三级结果
- L1：exit=? / report status=?（路径）
- L2：exit=? / 7 项 checks 逐条 ✓✗ / info 关键字段（per-agent metadata 是否带 objects、
        MoveAhead 位移、GetReachablePositions 数量、Done 接受性）
- L3 短跑：end_reason / verified_completion / goal_coverage / transport_rate /
           action_success_rate / balance / timeout_count / token 用量
- L3 完整跑：同上（若执行）

## 3. 首跑 8 问逐条回答（见 §8；每条 = 结论 + 证据来源）

## 4. 问题与处理记录
- 现象 / 原始报错 / 关键日志段 / 复现命令 / 已做的环境层处理

## 5. 结论与建议
```

## 8. 首跑 8 问清单（本地无 GPU 无法验证，必须真机逐条确认）

| # | 待确认 | 怎么确认 |
|---|--------|----------|
| 1 | 目标机下载到的 build 与本机 `ai2thor==5.0.0` 的事件字段一致（尤其 per-agent metadata 是否带 `objects`） | L2 报告 `checks.adapter_surface_ok`；适配器已做 `objects` 顶层 → 兄弟事件回退 |
| 2 | `agentCount` 参数被该 build 接受 | L2 `checks.multi_agent_events`（若报未知参数 → build/包版本不一致） |
| 3 | `CloudRendering` 在 A100 上可用 | L2 `checks.controller_started`；失败退 `PLATFORM=linux` + `xvfb-run` |
| 4 | `gpu_device` 仍是有效 kwarg | 若 `TypeError` → 去掉 `LLAMAR_AI2THOR_GPU_DEVICE` |
| 5 | `PutObject` 是否需要额外参数（`forceAction` / 朝向） | L3 `action_success_rate` 与 verifier trace；首版只在目标为 Fridge 时补 `forceAction=True` |
| 6 | `GetReachablePositions` 在初始 pose 可用 | L2 `info` 里的数量；供导航工具参考 |
| 7 | `MoveAhead` 位移量与 `grid_size=0.25` 的坐标约定一致 | L2 `info` 的 `MoveAhead` 位移记录 + L3 里导航是否收敛 |
| 8 | 真机单回合延迟 vs `step_timeout=60s` | L3 `timeout_count`（非 0 就调参） |

> 诚实边界：以上 8 条在 mock 注入下均有覆盖测试，但 **mock 只能证明消费面契约成立，不能证明真机行为**——这 8 条是本次 A100 首跑的真正任务。

## 9. 结果回流

把 `reports/a100_firstrun_<日期>/`（至少主报告 + L1/L2 报告 + L3 关键产物）发回用户 / 开发侧；
任何失败与复现证据一并附上，开发侧据其开修复卡处理。
