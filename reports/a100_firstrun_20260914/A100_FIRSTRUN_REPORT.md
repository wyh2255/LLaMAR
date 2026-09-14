# A100 首跑报告（2026-09-14）

> 执行方：A100 主机上运行 Hermes agent（不修改仓库代码；所有环境层处理见 §4）。
> 仓库：`/home/user/.WYH/LLaMAR`　分支：`feat/ai2thor-scene-adaptation`　commit：`849275a`
> 任务书：`A100_TASK.md`（849275a）；排障参考：`docs/system_docs/ai2thor_a100_runbook.md`

## 1. 环境

- **主机 / GPU / 驱动**：`a100`；`NVIDIA A100-SXM4-40GB`（PCI `3b:00.0`，40960 MiB，SXM4）
  `NVIDIA-SMI 580.173.02 | Driver Version: 580.173.02 | CUDA Version: 13.0`
  内核模块版本 `NVRM 580.173.02`（与用户态一致，见 §4-1）
- **OS / 内核 / 硬件**：Ubuntu 24.04（glibc 2.39）、`6.8.0-134-generic`、x86_64、80 vCPU、125 GiB RAM、`/` 159 GiB 可用
- **python / uv**：venv Python `3.14.3`；`uv 0.10.9`；`torch 2.12.1+cu130`（`torch.cuda.is_available()=True`，识别到 `NVIDIA A100-SXM4-40GB`）
- **ai2thor**：`5.0.0`，Unity build commit `f0825767cd50d69f666c7f282e54abfe58f1e917`
  （CloudRendering 包 797 MB，缓存于 `~/.ai2thor/releases/thor-CloudRendering-f0825767…`）
- **实际生效的环境变量**：
  `no_proxy=localhost,0.0.0.0,127.0.0.1,::1,pypi.tuna.tsinghua.edu.cn`、
  `http_proxy/https_proxy/all_proxy=http://127.0.0.1:8118`（本机代理）、
  `PYTHONPATH=src`（未设 PYTHONPATH 时 `import a2a` 会落到 site-packages 的 `a2a-sdk 1.1.0`，已实测）、
  `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`、
  `LLAMAR_AI2THOR_MODE=unity`、`LLAMAR_AI2THOR_HEADLESS=1`、`LLAMAR_AI2THOR_PLATFORM=cloud`、
  `LLAMAR_AI2THOR_GPU_DEVICE`（**未设置**，单卡）、
  `LD_LIBRARY_PATH=/tmp/glvnd-libs/usr/lib/x86_64-linux-gnu`（**临时 workaround**，见 §4-2）
- **LLM 端点**（L3，来自仓库根 `.env`）：`provider=openai`、`model=deepseek-v4-flash`、`api_base=https://api.deepseek.com`、`api_key`（不落档）

## 2. 三级结果

### L1 — fake 冒烟（无 GPU 依赖）
- 命令：`uv run python scripts/ai2thor_runtime_smoke.py --mode fake --report reports/a100_firstrun_20260914/smoke_fake.json`
- **`exit=0`，`status="ok"`**，7 项 checks 全 `true`（fake 注入）。产物：`smoke_fake.json`

### L2 — unity 冒烟（G1/G5 门禁，2 agent，FloorPlan1）
- 首次尝试（补 glvnd 前）：**`exit=3` 超时**，`error.type=TimeoutError`，Unity 进程段错误变僵尸 → 证据留档 `unity_smoke.attempt1_timeout.json` / `.log`
- 修复后重跑：**`exit=0`，`status="ok"`，7 项 checks 全 `true`，耗时 3.99 s**
  ```
  import_ok ✓  controller_started ✓  multi_agent_events ✓  per_agent_metadata ✓
  executor_round_ok ✓  adapter_surface_ok ✓  stop_clean ✓
  ```
- `info` 关键字段（§8 证据来源）：
  - `launch_options`: `{"scene":"FloorPlan1","width":"300","height":"300","headless":"True","agentCount":"2","gridSize":"0.25","visibilityDistance":"1.5","platform":"CloudRendering"}`
  - `agent_positions_init`: `[{-1.0, 0.901, 1.0}, {-1.0, 0.901, 0.0}]`
  - `move_caused_displacement = true`　`done_action_supported = true`　`reachable_positions_count = 162`
  - `metadata_schema`: `has_objects=true`、`has_agent=true`、`has_reachable_positions=true`、`object_count=77`，
    `sample_object_keys` 含 `name/position/rotation/visible/isInteractable/receptacle/toggleable/isToggled/breakable/…`
- 产物：`unity_smoke.json`

### L3 短跑 — 8 回合（unity + 真 LLM）
- 命令：`--task 3_transport_groceries --scene FloorPlan1 --agents 2 --seed 42 --mode unity --max-steps 8 --wall-clock-limit 3600 --coordinator-port 18080 --agent-base-port 18191`
- **`exit=1`（预期：8 步跑不完任务）**，`end_reason=max_steps_reached`，耗时 28.04 s

| 指标 | 值 |
|---|---|
| `metric_schema_version` | 2 |
| `verified_completion` / `finished` | false / false |
| `goal_coverage` / `coverage` / `transport_rate` | 0.0 / 0.0 / 0.0 |
| `action_success_rate` | 0.4（成功 6 / 尝试 15，失败 9） |
| `balance` | 0.5 |
| `timeout_count` / `timeout_rounds` | 0 / 0 |
| `interaction_coverage` | 0.333 |
| token 用量 | 21 次调用：prompt 60874 + completion 4440 = **65314**（`deepseek-v4-flash`，全部 `Status=ok`；Alice 8 / Bob 8 / Coordinator 5） |

§5.2 五条验收：**全部通过**
1. ✓ `summary.json` 字段齐全（上表）
2. ✓ `timeout_count == 0`
3. ✓ `trajectory.csv` 8 行 = `rounds_completed` 8 = `verifier_trace.ndjson` 8 行；每行 Actions / Successes 各含 **2** 条 agent 动作结果
4. ✓ `summary.csv` 单行聚合（含 per-agent token 列）；`events.ndjson` 4 条 coordinator 语义事件：`assign_task`×2、`send_message`(`activate_plan_node`)×2
5. ✓ 无 `worker_busy` / `task_not_routable_yet` / `unknown_task_id`（0 次）

留证异常（详见 §4-4）：
- `✗ Error: unclassified_tool_error` 高频出现；失败动作集中在 `PickupObject(Apple|…)`（7 次）、`PickupObject(Bread|…)`（1 次）、`MoveAhead`（1 次）
- `task_watchdog` 报 `TASK_STALE`×4 → `TASK_RECOVERED`×2（`progress_age=10.0s steps_since=6`）
- `coordinator/unknown.ndjson` 记录 1 条来自 executor 的未归类 `raw_request`

### L3 完整跑 — 50 步预算
- 同参数、仅 `--max-steps 50`、`--log-dir …/l3_full`；**`exit=1`**，`end_reason=max_steps_reached`，耗时 **132.22 s**（≈2.6 s/回合）

| 指标 | 完整跑（50 步） | 短跑（8 步） |
|---|---|---|
| `metric_schema_version` | 2 | 2 |
| `verified_completion` / `finished` | false / false | false / false |
| `goal_coverage` / `coverage` | 0.0 / 0.0 | 0.0 / 0.0 |
| `transport_rate` | **0.0455**（1/22 子任务） | 0.0 |
| `action_success_rate` | 0.3699（成功 27 / 尝试 73，失败 46） | 0.4（6/15） |
| `balance` | 0.6875 | 0.5 |
| `interaction_coverage` | 0.6667 | 0.3333 |
| `completed_subtask_count` | 1 | 0 |
| `timeout_count` / `timeout_rounds` | **0 / 0** | 0 / 0 |
| `per_agent_successful_actions` | `{0: 11, 1: 16}` | `{0: 2, 1: 4}` |
| token 用量 | **89 次调用：prompt 267139 + completion 23690 = 290829** | 21 次：65314 |

§5.2 五条验收：**全部通过**
1. ✓ `summary.json` 字段齐全（上表）
2. ✓ `timeout_count == 0`（50 回合无一超时，`step_timeout=60 s` 有 ~22× 余量）
3. ✓ `trajectory.csv` 50 行 = `rounds_completed` 50 = `verifier_trace.ndjson` 50 行；每行 2 条 agent 动作结果
4. △ `summary.csv` 单行 ✓；**`events.ndjson` 仅 5 条 `send_message`(activate_plan_node，step 0/23)，无 `assign_task`**（见 §4-5）
5. ✓ 无 `worker_busy` / `task_not_routable_yet` / `unknown_task_id`（0 次）

**动作级统计（真机行为画像，开发侧重点看）**

| 动作 | 次数 | 失败 |
|---|---|---|
| `PickupObject` | 30 | **30（100% 失败）** |
| `NoOp` | 23 | 0 |
| `MoveAhead` | 16 | 7 |
| `OpenObject` | 8 | 6 |
| `RotateLeft/Right` | 11 | 0 |
| `Done` | 4 | 0 |
| `MoveLeft/Right/Back` | 4 | 3 |
| `LookDown/Up` | 4 | 0 |

- **零参数类错误**：全 run 目录内 `InvalidArguments` / `MissingArguments` / `AmbiguousAction` / `InvalidAction` 均为 **0** → `PickupObject` / `OpenObject` 的**参数形态被该 build 接受**，失败不在参数面
- 失败统一落入 `unclassified_tool_error`（`agent_interactions.csv` 中 **226** 次），轨迹侧只剩归一化文本 `Action PickupObject(Apple_1) failed.`
- 两个 agent 反复对同一坐标发同一条 `PickupObject(Apple|-00.47|+01.15|+00.48)`（30 次全败），模型自身推理称"items may be out of reach / let me move closer"，但未见有效的接近策略（`MoveAhead` 仍有 7 次失败）

### 关键产物差异（短跑 vs 完整跑）
| 产物 | 短跑（8 步） | 完整跑（50 步） |
|---|---|---|
| `subtasks.csv` | 有（2 行 `dispatch-1/2`） | **无（0 行，文件未生成）** |
| 顶层 `events.ndjson` | `assign_task`×2 + `send_message`×2 | 仅 `send_message`×5（无 `assign_task`） |
| `assign_task` 出现位置 | 顶层 + `coordinator/*.ndjson` | 仅在 `coordinator/*.ndjson`（84 处） |
| 协调路径 | 直接派发（`dispatch-1/2`，计划节点 `explore-deliver-*`） | 计划节点驱动（`recon` → `deliver-alice/bob`，dispatch id `dsp_…`） |

## 3. 首跑 8 问逐条回答

| # | 结论 | 证据 |
|---|------|------|
| 1 | **成立**：目标机 build 与 `ai2thor==5.0.0` 事件字段一致，per-agent metadata **带 `objects`** | L2 `checks.adapter_surface_ok=true`；`info.metadata_schema.has_objects=true`、`object_count=77`；build commit `f0825767…` |
| 2 | **成立**：`agentCount` 被该 build 接受 | L2 `info.launch_options.agentCount="2"` + `checks.multi_agent_events=true`（无未知参数报错） |
| 3 | **成立**：`CloudRendering` 在 A100 上可用，**无需退 `PLATFORM=linux` + `xvfb-run`** | L2 `controller_started=true`、`platform=CloudRendering`、3.99 s 内启动完成。**前提**：系统 glvnd 分发器库必须存在（§4-2），否则 Vulkan 枚举不到 GPU |
| 4 | **成立（静态）**：`gpu_device` 仍是有效 kwarg | `ai2thor.controller.Controller.__init__` 签名含 `gpu_device`；适配器 `ai2thor_orch/executor/unity_controller.py:183` 在 `LLAMAR_AI2THOR_GPU_DEVICE` 非空时透传。本次单卡**未设置**该变量，故未触发 `TypeError`。注意：设置它会让 ai2thor 调 `vulkaninfo`（本机未装 `vulkan-tools`） |
| 5 | **有结论（数据面）**：`PutObject` 是否需要额外参数 —— **失败不在参数面** | 完整跑 50 回合内 `PickupObject` 30 次**全部失败**、`OpenObject` 8 次失败 6 次，`transport_rate=0.0455`；但全 run 内 `InvalidArguments`/`MissingArguments`/`AmbiguousAction`/`InvalidAction` 均为 **0** → 动作参数形态被 build 接受。失败统一归一化为 `unclassified_tool_error`（226 次），模型自述"物体可能太远"，未见有效接近。**结论：真机瓶颈是"接近/朝向不足 + 失败信息未归类"，不是缺 `forceAction` 之类参数**；首版只在目标为 Fridge 时补 `forceAction=True` 的做法在本次数据下无证据支持（也未被触发：`PutObject` 0 次） |
| 6 | **成立**：`GetReachablePositions` 在初始 pose 可用 | L2 `info.reachable_positions_count=162`；`metadata_schema.has_reachable_positions=true`（供导航工具参考的坐标集可用） |
| 7 | **成立**：`MoveAhead` 位移与 `grid_size=0.25` 约定一致 | L2 `info.move_caused_displacement=true`；初始位姿 z 间距 1.0 = 4×0.25。真机导航**未收敛**：完整跑 `MoveAhead` 16 次失败 7 次、`MoveLeft/Right/Back` 4 次失败 3 次，与 `PickupObject` 30/30 失败互为因果（够不到物体） |
| 8 | **成立**：单回合延迟远小于 `step_timeout=60s` | 完整跑 50 回合 132.22 s ≈ **2.64 s/回合**，`timeout_count=0`；短跑 3.5 s/回合，同样 0 超时。`step_timeout=60 s` 有 ~22× 余量，**无需调参** |

> 诚实边界：以上结论均来自真机产物（报告 JSON / 运行日志 / Unity `unity.log`），非 mock。

## 4. 问题与处理记录

### 4-1 GPU 不可用：NVML driver/library 版本错配（已处理）
- **现象**：`nvidia-smi` → `Failed to initialize NVML: Driver/library version mismatch`（NVML library 580.173）
- **取证**：内存模块 `/proc/driver/nvidia/version` = `580.159.03`，磁盘 DKMS 模块与用户态 = `580.173.02`；`journalctl -k` 留下 `NVRM: has the version 580.173.02, but this kernel module has the version 580.159.03`
- **处理**：卸载并重载内核模块（`journalctl -k` 记录 11:23:08 `Unloading driver` → 11:23:10 `loading NVIDIA UNIX x86_64 Kernel Module 580.173.02`）
- **备注（诚实记录）**：实际发生的是**模块重载，而非整机重启** —— `uptime` 仍为 `up 6 weeks, 6 days`，`/var/run/reboot-required` 依然存在，登录会话未中断。本机仍有待生效的系统更新（含内核 `6.8.0-139`），建议择机正式重启一次

### 4-2 【本次最关键】glvnd 分发器库文件丢失 → NVIDIA Vulkan ICD 失效 → Unity 段错误（已用 workaround 绕过，**根治需 root**）
- **现象**：L2 首次运行 `exit=3` 超时；`~/.ai2thor/log/unity.log`：
  ```
  [Vulkan init] Physical Device … [0]: "llvmpipe (LLVM 20.1.2, 256 bits)" deviceType=4
  [Vulkan init] Selected physical device (nil)
  Caught fatal signal - signo:11 code:1 errno:0 addr:0x10      ← 段错误 → 进程变 zombie
  ```
- **定位链**：
  1. loader 报 `loader_scanned_icd_add: Could not get 'vkCreateInstance' via 'vk_icdGetInstanceProcAddr' for ICD libGLX_nvidia.so.0`
  2. 强制 `VK_DRIVER_FILES=nvidia_icd.json` → `vkCreateInstance = -9 (VK_ERROR_INCOMPATIBLE_DRIVER)`
  3. 直接调 ICD：`vk_icdNegotiateLoaderICDInterfaceVersion` 返回 **-3**，`vk_icdGetInstanceProcAddr` 全部返回 NULL，且**从未访问 `/dev/nvidia*`**（strace）
  4. 全量 file trace 显示 ICD 反复尝试 `dlopen("libEGL.so.1")` 全部 `ENOENT`
  5. `dpkg -V` → **`libEGL.so.1`、`libGLX.so.0`、`libGLdispatch.so.0`、`libGL.so.1` 文件被删但包仍登记为已安装**（全系统 89 个 `missing`，另含 41 个 xrdp 文件）
- **处理（环境层，允许）**：`apt-get download` 同名同版本 deb（免 root）→ `dpkg-deb -x` 解到 `/tmp/glvnd-libs` → `LD_LIBRARY_PATH` 前置。验证：Vulkan 设备数 1 → **2**，`[0] name='NVIDIA A100-SXM4-40GB' vendorID=0x10de deviceID=0x20b0 type=DISCRETE_GPU`；L2 随即 `exit=0`
- **根治（需 sudo，执行方无密码无法代做）**：
  ```bash
  sudo apt-get install --reinstall -y libglvnd0 libegl1 libglx0 libgl1
  ```
  建议一并修复其余缺失：`libglu1-mesa libglew2.2 libgles1 libgles2`（dev 包按需）与 `xrdp`

### 4-3 依赖同步：uv 不读 pip.conf；PyPI 与 GitHub 需不同出口（已处理）
- `~/.config/pip/pip.conf` 已配清华源，但 **uv 忽略 pip.conf**，默认走 `http_proxy=127.0.0.1:8118` 直连 PyPI（慢）
- 首次 unset 全部代理后又卡住：`pyproject.toml:119` 有 git 依赖 `mini-agent = { git = ".../Mini-Agent.git", rev = "feature/task-context" }`，而**直连 GitHub 不通**（`curl --noproxy` 15 s 超时）
- 最终配置：`UV_DEFAULT_INDEX=清华源` + `no_proxy` 含 `pypi.tuna.tsinghua.edu.cn`（直连，约 3× 于走代理）+ 其余域名保留代理。`uv sync --extra ai2thor-unity` 成功（torch 2.12.1+cu130 等），venv 5.2 GiB

### 4-4 L3 短跑观察到的行为异常（**未改代码**，交开发侧）
- `unclassified_tool_error`：失败动作未归类，出现在 agent 工具结果里（`PickupObject` 7 次、`MoveAhead` 1 次）
- `TASK_STALE` → `TASK_RECOVERED`：`task_watchdog` 在 `progress_age=10.0s`、`steps_since=6` 时判定两个 dispatch 陈旧并恢复（8 回合内 4 次 STALE / 2 次 RECOVERED），需确认阈值是否适配真机 `step_timeout=60s`
- `coordinator/unknown.ndjson`：executor 发出的一条 `raw_request` 未被归类（`has_metadata=false`）
- 两 agent 在相邻回合对同一物体重复发出**完全相同**的失败动作（`PickupObject(Apple|-00.47|+01.15|+00.48)`），未见基于失败的策略调整

### 4-5 协调路径不同 → 产物不一致（**新增，完整跑发现**）
- 同样参数、仅步数不同，两次 run 走了**不同协调路径**：
  - 短跑：直接派发，子任务 id `dispatch-1/2`，计划节点 `explore-deliver-alice/bob`
  - 完整跑：计划节点驱动，`recon` → `deliver-alice/bob`（step 23 才激活），dispatch id `dsp_…`
- 后果：
  - **`subtasks.csv` 在完整跑中完全没有生成**（短跑有 2 行）；该文件由 `ai2thor_orch/logger.py:448` 的 subtask 生命周期写入，完整跑此路径未触发
  - 顶层 `events.ndjson` 在完整跑中**不含 `assign_task`**（仅 5 条 `send_message`）；`assign_task` 只出现在 `coordinator/*.ndjson`（84 处）
  - `coordinator/` 下 dispatch 级文件命名也随之不同（`events_dsp_*.ndjson`）
- 影响：§5.2-4 的"`events.ndjson` = coordinator 语义事件流（assign_task / reply_to_help / cancel_task / send_message）"**只在某些协调路径成立**；判据本身需要按路径放宽或在导出侧统一

### 4-6 其他
- L2 首跑前清理了 `~/.ai2thor/tmp/*.lock` 残留（探针超时退出后会留下 0 字节锁文件）
- 探针超时把 Unity 子进程留成 zombie（`Z [thor-CloudRende]`），父进程退出后自动回收
- `pkill -f <pattern>` 模式会匹配到 Hermes 自己的命令行导致自杀，改用字符类 `smok[e]` 规避
- 完整跑 132 s / 短跑 28 s，**远快于任务书预估的 20–60 min**（本机单步约 2.6–3.5 s，含 LLM 往返）
- **`uv.lock` 被 `uv sync` 改写后已恢复原状**：因使用清华源，uv 把 lock 里所有 `pypi.org` / `files.pythonhosted.org` URL 重写为 `pypi.tuna.tsinghua.edu.cn`（2616 行）—— **版本、包集合、sha256、size 全部零变化**（`version` 行差异 0，`name` 行差异 0）。已 `git checkout -- uv.lock` 还原以守住红线 1，并验证恢复后 `uv run --frozen` 环境照常可用（`ai2thor 5.0.0` / `torch 2.12.1+cu130` / `cuda True`）。后续如在 A100 上续跑，建议仍用环境变量 `UV_DEFAULT_INDEX` 而不是让 lock 被改写
- 最终仓库状态：`git status` 仅 `?? reports/`（本报告目录，未提交）；`.env` 由 `.gitignore:123` 忽略，未被跟踪

## 5. 结论与建议

1. **编排侧与适配器侧在真机成立**：L1/L2 全绿，L3 短跑与完整跑的五条产物验收全过，真 LLM（`deepseek-v4-flash`）调用与计费记录正常，`timeout_count=0`（两 run 均 0）。**§8 八问全部有结论**：1/2/3/6/7 由真机产物确认，4 由签名+适配器代码确认（未触发路径），5/8 由完整跑数据确认。
2. **真机瓶颈在"仿真交互"而非"编排"**：完整跑 50 回合 `PickupObject` **30/30 失败**、`OpenObject` 6/8 失败、导航动作 16 次中 7 次失败，`transport_rate=0.0455`、`goal_coverage=0`。失败**零参数类错误**（无 `InvalidArguments` 等），统一落 `unclassified_tool_error`。25 万 prompt token 换来 1/22 子任务 —— 建议下一步优先修"接近/朝向"与"工具失败归类"，而非调 `step_timeout` 或补动作参数。
3. **本机环境有两个必须先修的前置缺陷**（都与本次任务无关，但会阻塞/污染后续真机验证）：
   - glvnd 分发器库文件丢失（§4-2）→ 只在本次用 `LD_LIBRARY_PATH` 绕过，**建议立刻 `sudo apt-get install --reinstall -y libglvnd0 libegl1 libglx0 libgl1`**（推荐方案见 §4-2）
   - 系统仍处 `reboot-required`（内核 `6.8.0-139` 待生效），建议择机正式重启
4. **建议开发侧优先看**：§4-4 的 `unclassified_tool_error` 归类缺失、`TASK_STALE` 阈值、agent 重复无效动作；§4-5 的产物不一致（协调路径 → `subtasks.csv` / `assign_task` 事件缺失）——后者会让任何"按产物判据验收"的流程出现假阴性。
5. 若需对照，可补一条同参 `--mode fake --max-steps 8`（`--mode fake` 仍走真 LLM，会产生小额计费），用于分离"仿真侧 vs 编排侧"。本次未执行。

**计费实况**：L3 短跑 21 次调用 / 65,314 token；L3 完整跑 89 次调用 / 290,829 token；两 run 合计 **110 次调用 / 356,143 token**（`Status` 全 `ok`）。

### 产物清单
```
reports/a100_firstrun_20260914/
├── smoke_fake.json                      # L1 报告（exit=0 / status=ok）
├── unity_smoke.json                     # L2 报告（exit=0 / 7 项全 true）
├── unity_smoke.attempt1_timeout.json    # L2 首次失败留档（exit=3 / TimeoutError）
├── unity_smoke.attempt1_timeout.log     # 同上，stdout 原文
├── l3_short/                            # L3 短跑（8 步）产物
│   ├── summary.json  summary.csv  run_metrics.json  metadata.json  task_config.json
│   ├── trajectory.csv  verifier_trace.ndjson  token_usage.csv
│   ├── events.ndjson  subtasks.csv  agent_interactions.csv  router_interactions.csv
│   ├── coordinator/  workers/  supervision/
├── l3_full/                             # L3 完整跑（50 步，完成）产物
│   ├── summary.json  summary.csv  run_metrics.json  metadata.json  task_config.json
│   ├── trajectory.csv  verifier_trace.ndjson  token_usage.csv
│   ├── events.ndjson  agent_interactions.csv  router_interactions.csv
│   └── coordinator/  workers/  supervision/          # 注意：无 subtasks.csv（见 §4-5）
└── A100_FIRSTRUN_REPORT.md              # 本报告
```
