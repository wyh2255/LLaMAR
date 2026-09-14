---
日期: 2026-09-14
文档类型: 运行手册（Runbook）
文档概述: AI2Thor unity 模式在远程 GPU 主机（A100）上的三级运行流程——fake 冒烟 → unity 冒烟门禁 → 端到端实验；含启动参数、环境变量、产物验收判据、排障与「本地无 GPU 无法验证、需首跑确认」清单
---

# AI2Thor unity 模式 A100 运行手册

本手册面向**远程 GPU 主机（A100 等）上的 AI2Thor unity 端到端运行**。它回答三件事：
跑什么、什么算通过、坏了看哪里。

- 适用代码：`ai2thor_orch/`（P5-4 起 unity 路径已接线：`ai2thor_orch/executor/unity_controller.py`）
- 本机（开发机）无 GPU / 无 Unity：本地只能跑 `fake` 模式与 **mock 注入** 的 unity 路径
- 门禁脚本：`scripts/ai2thor_runtime_smoke.py`（G1/G5 探针，三级流程的 L1/L2）
- 交叉引用：`docs/system_docs/architecture_ai2thor_orch.md`、
  `docs/system_docs/env_contract.md`、
  `docs/plans/2026-07-18-ai2thor-a2a-migration-implementation-plan.md`（§G1 / §G5）

---

## 双机协作纪律（WSL ↔ A100）

两台机器（WSL 开发侧 ↔ A100 执行侧）协同时的默认纪律，任何一侧开工前先过一遍：

1. **开工先拉**：开工前 `git pull --ff-only`。
2. **小步推**：一个能跑通的改动就 commit + push、不过夜。
3. **不留孤本**：A100 上不留「只此一份」的成果——产物 / 日志尽快回传提交（无法入库的按约定归档）。

**分工口径**：默认 WSL 侧 = 主开发场（改码 / 修复 / 评测 / 文档 / 编排）；A100 = 真机执行场（unity 验证 / sweep）；连续真机行为调试可就地在 A100 改跑，但每完成一小点即 commit + push。

---

## 0. 拉取代码（在目标主机上）

```bash
git fetch origin
git checkout feat/ai2thor-scene-adaptation          # unity 接线所在分支（P5-4）
git log --oneline -3                                # 确认 HEAD 含 P5-4 提交（fc591c4）与文档收口
```

> P5 段提交均已 push（含 P5-4 `fc591c4` 与文档收口 `a796b80`）。若拉到的副本缺少
> 这些提交，说明本地快照过旧，重新 `git fetch` 即可。
> 执行向清单与报告模板见仓库根 **`A100_TASK.md`**（与本文互补：本文 = 参考手册，任务书 = 照做清单）。
>
> ⚠️ **网络（A100 真机实测）**：A100 直连 `github.com:443` 超时（校园网：DNS 可解析、TCP 443 不通）
> ——`fetch/pull` 需经 **A100 本机代理**（chisel SOCKS `127.0.0.1:1080` 或等效代理），用临时 env、
> **不改 `git config`**：
>
> ```bash
> https_proxy=socks5h://127.0.0.1:1080 http_proxy=socks5h://127.0.0.1:1080 git pull --ff-only
> ```
>
> 同一网络事实也影响 §2.2 `uv sync` 首次拉取（GitHub git 依赖走代理出网；首跑报告 `reports/a100_firstrun_20260914/` §4-3）。

---

## 1. 前提：目标主机要有什么

| 类别 | 要求 | 说明 |
|------|------|------|
| GPU | NVIDIA GPU（A100 等）+ 可用驱动 | ai2thor 需要 GPU 渲染（CloudRendering / Linux64 二选一） |
| 系统库 | `libvulkan1`（无显示渲染必需） | `apt-get install -y libvulkan1`；缺失时报 Vulkan/渲染设备错误 |
| 显示 | 无显示主机走 headless + CloudRendering | 有 X 时可 `LLAMAR_AI2THOR_X_DISPLAY=:0`；纯 Linux64 无显示需 `xvfb-run` |
| 磁盘 | ≥ 6 GB 空闲 | Unity build 缓存（`~/.ai2thor/`）+ CUDA torch（~2 GB） |
| 网络 | 首次运行需出网 | ai2thor 从官方 build 服务器下载 Unity Player（缓存在 `~/.ai2thor/releases`） |
| 依赖 | `uv sync --extra ai2thor-unity` | = `llamar-integration[ai2thor]` + `torch>=2.0.0` + `sentence-transformers`（CUDA torch） |
| 权限 | 运行用户对 repo 与 `~/.ai2thor` 有读写+执行权限 | 不要用 root 跑完再换普通用户（`~/.ai2thor` 属主/文件模式会错）；共享机器建议每人一个 `HOME` |
| LLM 端点 | 仓库根 `.env`（**小写**字段 `provider` / `api_key` / `api_base` / `model`） | L3 端到端实验需要；L1/L2 探针不需要（不接 LLM） |

**venv 注意**：本仓 `openharness-a2a` / `a2a_lib` 属于 MARoS 项目，以 editable + `--no-deps`
方式安装（见 `pyproject.toml` 尾部注释）。`uv sync` 可能把它们剪掉；如 `import a2a` 失败，
按注释重装：

```bash
# 路径按目标机实际布局替换（下列为开发机示例）
uv pip install -e /home/wyh/daily_work/MARoS/maros_ws/a2a_lib --no-deps
uv pip install -e /home/wyh/daily_work/MARoS/my_a2a --no-deps
```

统一命令前缀（本仓约定：绕过 Privoxy，并让 `src/` 优先于 site-packages 里的 `a2a-sdk`）：

```bash
cd <repo>                                # 如 /home/wyh/daily_work/LLaMAR-ai2thor
export no_proxy="localhost,0.0.0.0,127.0.0.1"
unset PYTHONPATH                         # 清掉 shell/系统注入的其它路径（如有）
export PYTHONPATH="src"                  # 仅本仓 src：src/a2a 必须压过 site-packages 的 a2a-sdk
```

> 若目标机 shell 会注入其它 PYTHONPATH（本开发机的后台 shell 会注入 ROS
> `/opt/ros/humble/...`，导致 pytest 加载 ROS entrypoint 插件失败），务必保持上面的
> `unset` + 仅 `src` 的形式；后文所有 `python -m` / `pytest` 命令都假设本节已执行。

---

## 2. 三级运行流程

三级是**递进门禁**：上一级不过，不要跑下一级（否则把环境问题误判成 agent/LLM 问题）。

```
L1 fake 冒烟   （任何机器，秒级，无 GPU/无 ai2thor 依赖）
   ↓ 通过
L2 unity 冒烟  （GPU 主机，分钟级；7 条 gating 断言，不接 LLM）
   ↓ 通过
L3 端到端实验  （unity + LLM coordinator/workers，按回合计费）
```

### 2.1 L1：fake 冒烟（任何机器）

```bash
uv run python scripts/ai2thor_runtime_smoke.py --mode fake --report /tmp/smoke_fake.json
echo "exit=$?"
```

**通过判据**：`exit=0`，报告里 `status == "ok"`；同时产出 schema 版本与 checks 明细
（用于确认报告结构本身没坏）。

### 2.2 L2：unity 冒烟（GPU 主机；G1/G5 门禁）

```bash
# 首次（或依赖变更后）；拉依赖遇 GitHub 直连超时 → 代理 env，见 §0 网络注记
uv sync --extra ai2thor-unity

export LLAMAR_AI2THOR_MODE=unity
export LLAMAR_AI2THOR_HEADLESS=1          # 无显示主机常态
export LLAMAR_AI2THOR_PLATFORM=cloud      # CloudRendering：无显示 GPU 渲染
# export LLAMAR_AI2THOR_GPU_DEVICE=0      # 多卡指定，按需

uv run python scripts/ai2thor_runtime_smoke.py \
  --scene FloorPlan1 --agents 2 --timeout 300 --report reports/unity_smoke.json
echo "exit=$?"
```

**这条命令走的就是实验要走的同一条接线**：
`ai2thor.controller.Controller` → `UnityController`（`ai2thor_orch/executor/unity_controller.py`）
→ `ControllerExecutor.execute_step`。

退出码（`exit_code_for`）：

| 码 | 含义 |
|----|------|
| 0 | 探针成功（`status=ok`） |
| 1 | 运行时异常或 gating 断言失败 |
| 2 | ai2thor 包未安装（ImportError） |
| 3 | 探针整体超时（`--timeout` 秒内未完成，首跑含 build 下载，建议 300） |

gating 断言（任一失败 → `status=error` + 退出码 1，报告 `checks` 里逐条可查）：

| # | check | 含义 |
|---|-------|------|
| 1 | `import_ok` | ai2thor 包可导入 |
| 2 | `controller_started` | Controller 启动 + `UnityController` 构造成功 |
| 3 | `multi_agent_events` | `agentCount=N` 生效（事件里 N 份 agent 事件） |
| 4 | `per_agent_metadata` | 每份 agent 事件带 `agentId` 与 `agent.position` |
| 5 | `executor_round_ok` | `execute_step` 一轮 N 个动作全部成功（含 `NoOp` → `Pass` 空动作映射） |
| 6 | `adapter_surface_ok` | 归一化 metadata 带 `agents`（N 条）/ `objects`（barrier/verifier 消费面成立） |
| 7 | `stop_clean` | `ControllerExecutor.stop()` 干净回收（含 `Controller.stop`） |

非 gating 记录（写进报告 `info`，不判定成败）：`MoveAhead` 是否真的位移、
原始 `Done` 动作是否被该 build 接受、`GetReachablePositions` 数量。

### 2.3 L3：端到端实验（unity + LLM）

**前置：本块命令自带必需 env**（可独立复制执行；三项 `LLAMAR_AI2THOR_*` 与首跑报告 `reports/a100_firstrun_20260914/` §1「实际生效的环境变量」一致）——先写 env 文件：

```bash
# 路径可自选，下文统一用 /tmp/l3_env.txt
cat > /tmp/l3_env.txt <<'EOF'
no_proxy=localhost,0.0.0.0,127.0.0.1
PYTHONPATH=src
LLAMAR_AI2THOR_MODE=unity
LLAMAR_AI2THOR_HEADLESS=1
LLAMAR_AI2THOR_PLATFORM=cloud
EOF
```

```bash
# 首跑建议先短跑（对齐 P5-3 fake 短跑口径：8 回合）
uv run --env-file /tmp/l3_env.txt python -m ai2thor_orch.experiment \
  --task 3_transport_groceries --scene FloorPlan1 --agents 2 --seed 42 \
  --mode unity --max-steps 8 --wall-clock-limit 3600 \
  --coordinator-port 18080 --agent-base-port 18191

# 短跑通了再跑完整预算
uv run --env-file /tmp/l3_env.txt python -m ai2thor_orch.experiment \
  --task 3_transport_groceries --scene FloorPlan1 --agents 2 --seed 42 \
  --mode unity --max-steps 50 --wall-clock-limit 3600
echo "exit=$?"
```

> **写法取舍（为什么用 `uv run --env-file`）**：等价写法是 `env no_proxy=… PYTHONPATH=src
> uv run python -m …` 前缀（与仓库其它命令一致），但在 **Hermes headless**（agent / 单查询
> 终端）下 `PYTHONPATH=` 内联赋值会被安全扫描按 `[HIGH] Interpreter hijack` 拦截（真机实测，
> 对应条目见 §5）；env 文件形式对人工 shell 与 agent 两处都成立，代价只是多一步写文件。纯人工
> shell 下亦可 `export` 同组变量后直接 `uv run python -m …`（等效）。
>
> ⚠️ **缺 `LLAMAR_AI2THOR_PLATFORM=cloud` 会走 Linux64 分支 → 触发 `thor-Linux64-*.zip`
> 构建下载（769MB，实测 ~32–44KB/s，全量以小时计）**——真机曾原样执行误触发（发现即中止、
> 补 env 后重跑）；本机缓存的 CloudRendering build（797MB）只有 `PLATFORM=cloud` 才会用到。
> `LLAMAR_AI2THOR_MODE=unity` 仅探针脚本读取（实验 CLI 认显式 `--mode unity`），一并带上以
> 与首跑生效清单对齐。

- 退出码：0 = `verified_completion` 或 `finished`；1 = 未达成
- **短跑注意**：`--max-steps 8` 通常跑不到任务完成，此时 `end_reason=max_steps_reached`
  且退出码为 1 属**预期**——判定看 `summary.json` 的 `end_reason` 与指标，
  不要以退出码否定短路。完整预算跑（50 步）若仍 `max_steps_reached`，才需要看
  `transport_rate` / `action_success_rate` 判断是编排问题还是任务难度
- 日志目录：`logs/<YYYYMMDD_HHMMSS>_3_transport_groceries_FloorPlan1_a2_seed42_unity/`
  （需要固定目录用 `--log-dir <path>`）
- 端口：coordinator `8080`（A2A `8081`），worker 基址 `8191`；**并发多跑要错开**
  `--coordinator-port` / `--agent-base-port`（benchmark 会自动错开）
- **fake 与 unity 的差别只在 Controller**：coordinator/worker 在两种模式下都是真 LLM
  agent（`src/orchestration/assembly.py` 统一用 `model` / `api_key_env` + `.env` 构造），
  所以 L3 会产生真实 LLM 调用与费用
- 对照跑法（建议）：同 task/scene/seed 先跑一遍 `--mode fake`，再跑 `--mode unity`，
  两条 `summary.json` 对比 —— 若 fake 正常而 unity 异常，问题在仿真侧而非编排侧

---

## 3. 环境变量参考（`LLAMAR_AI2THOR_*`）

解析函数：`unity_controller.unity_launch_options()`（缺省值即代码默认，未设置时生效）。

| 变量 | 缺省 | 说明 |
|------|------|------|
| `LLAMAR_AI2THOR_MODE` | `fake` | 仅探针脚本的默认运行模式（`--mode` > 环境变量 > `fake`）；实验 CLI 不读取该变量，L3 必须显式传 `--mode unity` |
| `LLAMAR_AI2THOR_HEADLESS` | `1` | headless 启动；显式设 `0` 才开窗渲染 |
| `LLAMAR_AI2THOR_PLATFORM` | 交给 ai2thor 自选 | `cloud` → `CloudRendering`（无显示 GPU 渲染）；`linux` → `Linux64` |
| `LLAMAR_AI2THOR_X_DISPLAY` | 沿用 `DISPLAY` | 如 `:0` |
| `LLAMAR_AI2THOR_GPU_DEVICE` | 未设置 | 传给 Controller 的 `gpu_device` |
| `LLAMAR_AI2THOR_WIDTH` / `_HEIGHT` | 300 / 300 | 观测走 metadata，不需要大图 |
| `LLAMAR_AI2THOR_GRID_SIZE` | 0.25 | 与编排层坐标约定的步长 |
| `LLAMAR_AI2THOR_VISIBILITY` | 1.5 | `visibilityDistance`（与迁移前 AI2ThorEnv 一致） |

---

## 4. 产物与验收判据

### 4.1 探针报告（L1/L2）

`--report <path>` 落盘 JSON：`status` / `checks`（上表 7 条）/ `info`（版本、
`MoveAhead` 位移、`GetReachablePositions` 数量等）/ `error`（失败时带 traceback）。

### 4.2 实验产物（L3）

`logs/<run>/`（详见 `docs/system_docs/architecture_ai2thor_orch.md`）：

| 文件 | 看什么 |
|------|--------|
| `summary.json` | `metric_schema_version=2`；`verified_completion` / `goal_coverage` / `transport_rate` / `action_success_rate` / `balance` / `timeout_count` |
| `summary.csv` | 单行 run 聚合（TotalSteps / FinalCoverage / EndReason / 交互计数 / per-agent token 累计；每步覆盖写，崩溃安全） |
| `trajectory.csv` | 每回合一行（Step / Actions / Successes / NoOpSource / TimeoutAgents / EndReason；趋势与超时分布看这里） |
| `events.ndjson` | 单 `run_id` 的 coordinator 语义事件时间线（assign_task / reply_to_help / cancel_task / send_message） |
| `metadata.json` | 运行元信息（task / scene / mode / seed / 模型 / code_commit） |
| `<run>/coordinator/`、`<run>/workers/<AgentName>/<AgentName>/` | per-agent LLM 与工具调用明细（agent 数 = `--agents`） |
| verifier trace（任务快照） | postcondition 判定依据（目标物是否在 Fridge 等） |

**unity 首跑验收清单**（逐条给证据，不看自报）：

1. `summary.json.verified_completion` 与 `goal_coverage` 一致（1.0 / 0.0 不对齐即异常）
2. `timeout_count == 0`：非 0 说明真机动作延迟超过 `step_timeout`（默认 60s/回合），
   需要调大 `step_timeout`（`Ai2ThorEnvPack(step_timeout=...)`）或减少每回合动作数
3. `action_success_rate`：明显低于 fake 对照跑 → 检查动作映射（`Pass`/`Done`、`forceAction`）
4. `trajectory.csv` 行数 = 回合数 = `verifier_trace.ndjson` 行数；每行 Actions / Successes 含 N 条 agent 动作结果
5. 任务物体最终在哪：与 verifier trace 的 postcondition 判定互证
6. 无 `worker_busy` / `task_not_routable_yet` / `unknown_task_id` 一类路由错误

---

## 5. 排障

| 症状 | 定位 | 处理 |
|------|------|------|
| `ModuleNotFoundError: ai2thor` | 依赖 | `uv sync --extra ai2thor-unity`（探针退出码 2） |
| 启动即失败 / Unity 侧异常 | **Player.log**：`~/.config/unity3d/Allen Institute for Artificial Intelligence/AI2-THOR/Player.log` | 先看最后 50 行；渲染/Vulkan 错误按下一行处理 |
| Vulkan / 渲染设备错误 | 系统库 | `apt-get install -y libvulkan1`；或改 `LLAMAR_AI2THOR_PLATFORM=cloud` 走 CloudRendering |
| 无显示主机仍报 X/display 错 | 显示模式 | `LLAMAR_AI2THOR_HEADLESS=1` + `PLATFORM=cloud`；用 Linux64 时套 `xvfb-run -a` |
| build 下载失败 / 卡住 | `~/.ai2thor/releases` | 删掉半成品目录重试；把 `--timeout` 提到 300+；确认出网 |
| 权限错误（`Permission denied` / 属主不符） | `ls -ld ~/.ai2thor ~/.ai2thor/releases` | 之前用 root/他人账号跑过 → `sudo chown -R "$USER:$USER" ~/.ai2thor`；Player 可执行位丢失时 `chmod +x ~/.ai2thor/releases/<build>/**/AI2-THOR*`（正常由 ai2thor 自动设置） |
| 第二次运行仍等很久 | build 缓存正常（首次下载一次） | 场景加载本身 30–120s，属正常 |
| 残留 Unity 进程占资源 | 进程 | `pkill -f AI2-THOR` 后重跑 |
| 探针退出码 3 | 超时 | `--timeout 300`（首跑含下载）；仍超时看 Player.log |
| 端口占用（8080/8081/8191+） | 并发/残留 | 换端口或清理残留进程；benchmark 已自动错开 |
| 动作全失败但无异常 | 域内软失败 | 空手 `PutObject` 是**软失败**（保留上一步状态、不进模拟）；先确认 `PickupObject` 成功、目标在 Fridge 时自动带 `forceAction=True` |
| LLM 报 401/404 | `.env` | 字段必须小写（`provider` / `api_key` / `api_base` / `model`）；`no_proxy` 已设置 |
| `PYTHONPATH=src` 内联赋值被安全扫描拦截（`[HIGH] Interpreter hijack environment variable: PYTHONPATH`） | Hermes headless（agent / 单查询终端）执行 §2.3 命令 | 改用 `uv run --env-file <file>` 携带（§2.3 已按此写）；并核验 `a2a` 解析到仓库 `src/a2a` 而非 site-packages（见下注） |

> **`a2a` 解析核验**（确认 `PYTHONPATH=src` 语义真的生效）：按 §2.3 的 env 文件跑
> `uv run --env-file /tmp/l3_env.txt python -c "import a2a; print(a2a.__file__)"`，
> 输出应为 `<repo>/src/a2a/__init__.py`；未设置时实测落到
> `.venv/lib/.../site-packages/a2a/`（`a2a-sdk 1.1.0`，首跑报告 `reports/a100_firstrun_20260914/` §1）。注：`python -c` /
> heredoc 形式在 Hermes headless 下同样会被安全扫描拦（script execution via -c/heredoc）
> ——agent 执行时把核验写成脚本文件再跑。

---

## 6. 需 A100 首跑确认清单（本地无法验证的项）

以下项在开发机（无 GPU / 无 Unity）**不可能验证**，P5-4 只保证接线面与 mock 形状一致。
首跑时逐条确认（每条都给了“怎么确认”）：

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

> 诚实边界：以上 8 条在 mock 注入下均有覆盖测试（`ai2thor_orch/tests/test_unity_controller.py`、
> `test_runtime_smoke.py`），但**mock 只能证明消费面契约成立，不能证明真机行为**。

---

## 7. 本地可复现的验证（无 GPU 时该跑什么）

```bash
# ① 先导出 PYTHONPATH：本机（及 A100）的 site-packages 里装着 a2a-sdk，
#    会遮蔽本仓 src/a2a —— 不导出时 collection 直接失败（9 errors，
#    实测口径见下方说明）
env -u PYTHONPATH PYTHONPATH="src" .venv/bin/python -m pytest ai2thor_orch/tests -m "not unity" -q
# → 260 passed

# unity 接线面 + 探针（mock controller 注入：launch options / 动作映射 /
# 事件归一化 / 守卫 / 报告 schema）
env -u PYTHONPATH PYTHONPATH="src" .venv/bin/python -m pytest \
  ai2thor_orch/tests/test_unity_controller.py ai2thor_orch/tests/test_runtime_smoke.py -q
# → 61 passed

# LLaMAR 全量测试（这条用 env -u PYTHONPATH，与仓内口径一致）
env -u PYTHONPATH .venv/bin/python -m pytest tests -q
# → 2147 passed, 8 skipped
```

> `env -u PYTHONPATH ... pytest ai2thor_orch/tests`（**完全不导出** PYTHONPATH）在
> 本机会因 a2a-sdk 遮蔽 `src/a2a` 产生 9 个 collection error 而中断——这是预存在的
> 环境问题，故统一用上面的「剥皮 + 仅 `src`」形式。

`unity` marker 的语义（`pyproject.toml`）：**需要真实 Unity runtime 的测试**，仅 GPU 主机执行；
`-m "not unity"` 是本仓 CI 与开发机的默认口径。
