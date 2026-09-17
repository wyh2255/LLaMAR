---
日期: 2026-09-14
文档类型: 系统架构文档（契约 / 接入指南）
文档概述: 多场景框架的环境包契约与适配指南——三层结构与依赖不变量、环境包必供接口（9 类）、
  框架↔环境调用协议（7 条）、新环境适配步骤（EnvPack + AssemblyHooks + 一行 run_assembly）、
  分层验证清单（含 A100 真机实测）、契约-现状缺口 G1–G12
---

# 环境包契约与适配指南（多场景框架）

> **版本**：v0.2.0（2026-09-14；P5 收口 + A100 首跑证据入档。v0.1.1 为 P2 合并后收口版；v0.1 初版经独立复核后修订——复核报告 `.hermes/spec/env-contract/20260913-env-contract-review.md`）
> **来源**：2026-09-13 拍板「契约驱动」路线；内容基于独立校验卡 `t_2d32b10b` 实测证据（本地留档 `.hermes/spec/env-contract/`：决策记录 + 校验报告 + 勘查报告）、P4/P5 实现提交与 A100 首跑实测（`reports/a100_firstrun_20260914/A100_FIRSTRUN_REPORT.md`）。
> **引用基准**：AI2Thor 适配层 `feat/ai2thor-scene-adaptation@9b58372`（P5 收口 + 首跑后修复）；主仓 `main@f340a46`（P3 分支对齐时的 main 面）。两树同名文件行号可能不同，引用尽量标注树别；未标注的接口/协议条目为两树共有并已实测。本版更新了 §2.1 现状注记、§3、§4、§5 的引用；更早的重锚段（P2 触及的 `src/a2a/coordinator/*` 等）与未触碰段（e6b95fc 基准）不逐条重锚——P4/P5 改动过的文件，行号请以提交后代码现状为准。
> **TODO（main 重锚）**：本地 `main` 已自 `f340a46` 前进至 `1d45654`（`9249aa1` / `aabf5f9` / `1d45654`，eval 采集相关；未入 ai2thor 分支）——本版 main 侧引用仍锚 `f340a46`，下版重锚时逐条复核。
> **读者**：接入新仿真环境的实现者；维护内核与编排层的开发者。

## 0. 30 秒结论

- 框架三层：**内核**（`src/Agent` + `src/a2a`，场景无关）→ **编排层**（coordinator/worker 骨架 + 装配）→ **环境包**（每环境一包，`<env>_orch/`）。
- 目标不变量：**内核绝不 import 任何 `*_orch`**；环境包只依赖内核；编排层经 EnvPack 注入使用环境包。
- 硬判据：**接第 3 个环境 = 新增一个环境包 + 注册一行，内核与编排零改动**（内核侧 P2 已达标；编排/装配侧经 P4 收敛为 EnvPack + AssemblyHooks + 一行 `run_assembly`，并由 P5（AI2Thor 接入）完成首次按该路径的验证；未收敛项收窄至 §5-G11）。
- **现状（P5 收口后）**：适配一个新环境 = 实现环境包（含 `EnvPack` 工厂 + `AssemblyHooks` 钩子）+ **调用 `run_assembly` 一行**；内核与编排骨架零改动。**P2 已落地**（内核零 `*_orch` 引用成事实 + 依赖方向守卫测试），**P4 已落地**（实验装配通用化至 `src/orchestration/assembly.py`；SAR 侧 `sar_orch/experiment.py` 薄壳化 + `sar_orch/assembly_hooks.py` 钩子，CLI/产物零变化），**P5 已落地**（AI2Thor 按同一路径接入并完成真机三级验证——A100 2026-09-14：unity 冒烟 7/7 / 3.99 s、端到端 8 与 50 回合、`timeout_count=0`，见 §4-L4；证据 `reports/a100_firstrun_20260914/`）。§3 为过渡期操作手册，其 ⚙️ 手动接线点已被 P4 收敛（见 §3 注）。
- 新环境适配最小清单：**①barrier → ②worker 工具 → ③coordinator 工具（如有）→ ④state provider → ⑤Context 子类（如需）→ ⑥prompts → ⑦DTO/verifier/metrics → ⑧装配 → ⑨接线 → ⑩分层验证**。细则见 §3。

## 1. 三层结构与依赖方向

| 层 | 职责 | 场景无关? | 现状 | 目标 |
|---|---|---|---|---|
| 内核 `src/Agent` + `src/a2a` | ReAct / Context / StateProvider 协议、A2A 传输、supervision、memory | ✅ | 零 `*_orch` 引用 ✅（P2 落地；守卫测试） | 保持零引用 |
| 编排层（coordinator / worker / 装配） | 星形编排、回合屏障驱动、轮询、收尾 | ✅（P4 后） | 通用骨架 `src/orchestration/`（`env_pack.py` / `coordinator.py` / `worker.py` / `assembly.py`）+ 环境薄壳（SAR：`sar_orch/experiment.py` + `sar_orch/assembly_hooks.py`；AI2Thor：`ai2thor_orch/experiment/` + `ai2thor_orch/assembly_hooks.py`——P5） | 保持：新环境只加包 + 钩子 |
| 环境包 `<env>_orch/` | barrier、工具、state provider、渲染钩子、prompts、DTO、verifier、metrics | 按环境一份 | SAR 完整；AI2Thor 完整且已接编排（P5：`Ai2ThorEnvPack` + hooks + `run_assembly`；A100 真机验证） | 不变（每环境一包） |

目标依赖方向（不变量）：

```
环境包 <env>_orch/ ──依赖──▶ 内核 src/Agent + src/a2a ◀──依赖── 编排层（经 EnvPack 注入使用环境包）
```

## 2. 环境包契约

### 2.1 必供接口清单（9 类 + 1 可选）

> "必"＝框架按契约依赖；"可选"＝按需实现。参考实现均实测核对过。

| # | 组件 | 契约（接口 / 要点） | 必/选 | 参考实现与现状注记 |
|---|---|---|---|---|
| 1 | run control / barrier | `EnvironmentRunControl`：`request_stop(reason)` / `stop()` / `get_run_status() -> RunStatus`（`@runtime_checkable`）；barrier 本体承担回合语义（§2.2-②） | **必** | SARBarrier（main `sar_orch/barrier.py:83`）；AI2ThorBarrier（`ai2thor_orch/barrier/ai2thor_barrier.py`）。协议文件 `src/a2a/coordinator/run_control.py` **已合入 main（P2a `ddd0007`）**；`set_run_control` 已接线（P4-2 `e47a8d0` 起收敛进通用 coordinator——`src/orchestration/coordinator.py:930`；G8 已落地，§5） |
| 2 | worker 工具集 | `Tool` 子类（`execute()` → `ToolResult`，框架按 `.success` 判定）；有运行时依赖的（barrier / mailbox…）构造注入并走 `extra_tools` 显式注册；无依赖的声明类可走 `tools_dir` 目录约定；**必须含任务完成工具**（返回 `ToolResult(task_complete=True)`；`require_explicit_completion=True` 时缺它 worker 任务无法正常终结——参考 `sar_orch/tools/worker/finish_task.py`、`ai2thor_orch/tools/worker/done.py`） | **必** | SAR 注册表 `SAR_WORKER_TOOLS`（16 个 = 13 域 + 3 通用，`sar_orch/tools/worker/__init__.py:20`）；装配经通用骨架 `src/orchestration/worker.py`（P4-3 `0403e20`）。AI2Thor 8 工具注册表 `AI2THOR_WORKER_TOOLS`（8 件，`ai2thor_orch/tools/worker/__init__.py:26`，P5-2 `3c200c6` 起步、F-nav `navigate` 增补），已接编排（P5-3）。注册入口收敛见 G10（§5） |
| 3 | coordinator 工具集（如有） | 经 `create_server(extra_tools=[...])` 注入——需要 barrier 实例的工具**不能**走 `tools_dir` | 必（如有） | SAR 构建经 `SAREnvPack.build_coordinator_tools`（`sar_orch/env_pack.py:316`）；通用装配注入 `src/orchestration/coordinator.py`（P4-2 `e47a8d0`） |
| 4 | state provider（≥coordinator 侧） | `snapshot(context_id) -> RuntimeState`（`version` 单调，变化才刷新）；可选 `AsyncStatePreparer.prepare_for_llm(llm_client)` 做 LLM 前预处理 | **必** | 协议 `src/Agent/router_agent/state_provider.py`（RuntimeState:15 / StateProvider:42 / snapshot:45 / AsyncStatePreparer:56）；SAR 两侧 `sar_orch/coordinator_state_provider.py`、`worker_state_provider.py`；AI2Thor 两侧已实现并接线（P5-2/P5-3） |
| 5 | Context 子类（可选） | 覆写 `_render_environment_view()`（另有 `_render_current_state()` 等可选钩子）；基类 `src/Agent/router_agent/context.py`、`worker_agent/context.py` | 可选 | 参考 `ai2thor_orch/state/context.py:22/:77`（仅重写渲染）。挂载已可注入（`EnvPack.build_session_factory`，P4-2/P4-3；G7 已落地，§5） |
| 6 | prompts | 目录 `<env>/prompts/{coordinator,worker}/`（至少 `system.md`），经 `prompts_dir` 参数传入 | **必** | SAR：`sar_orch/prompts/…`（薄壳默认解析 + `SAREnvPack` 携带；装配参数 `coordinator_prompts_dir` / `worker_prompts_dir` 可注入——P4-1/P4-2）；AI2Thor 目录已存在（P5-2 起经 EnvPack 携带）。G6 已落地（§5） |
| 7 | 任务 / 场景 DTO | 场景/任务定义 + 注册入口（形态自由） | **必** | AI2Thor：`contracts/task.py:17`（`TaskContract`）+ `AI2Thor/Tasks/<task_id>/checker.py` 白名单；SAR：`scene:int` + `SAR/Scenes/scene_N.py` + `get_scene_initializer`。无统一 TaskSpec（G11） |
| 8 | verifier + domain metrics | 回合/终局校验 + metrics 载体（并投递到 `RunStatus` / `RoundResult`） | **必** | AI2Thor：`verifier/verifier.py`、`contracts/types.py:43/:98`；SAR：checker（`SAR/Scenes/base_checker.py`）+ `barrier.get_metrics()`。无共同载体接口 |
| 9 | 配置注入轴 | `config.yaml` 段（`prompts_dir / tools_dir / skills_dir / log_dir`）+ `load_path_config` 解析 | **必**（半成品） | `src/config/config.yaml:14-21` → `src/a2a/shared/path_config.py:31-68`（现行消费方 = 内核 CLI `src/a2a/{coordinator,worker}/cli.py`）；prompts/skills 目录面已随 EnvPack 携带（P4-2/P4-3），`config.yaml` 轴本身实验装配仍未消费（半成品） |
| + | 额外挂载（可选） | MCP / UI 等附加面 | 可选 | SAR map_agent：挂载/会话生命周期**经注入**（P2b-2 `9f5a9b7`）——内核 hook 消费点 `server.py:1060-1061`、`:2496-2497`、`:1180-1183`/`:1253-1254`；注入点 `src/orchestration/coordinator.py:848`（经 `EnvPack.map_mcp_mount_hook`，P4-2 `e47a8d0`；原 `sar_orch/coordinator.py` 锚点已随通用化迁移）。SAR UI（`ui_dir`）不变。G3 已落地 |

### 2.2 框架 ↔ 环境协议（调用契约）

**① 生命周期**：装配方创建 barrier → coordinator（先起服务）→ worker；收尾逆序 `barrier.stop()` → worker → coordinator（守卫式容错）。停止语义：置位并唤醒所有等待者（`threading.Event`）。锚点：`sar_orch/experiment.py:656 / 854 / 958 / 1281`。

**② 回合语义（核心）**：`submit_action(agent_idx, action, *, advance=True, source=None)`（SAR `barrier.py:160`）。三条路径——
- 全员已提交且有人 `advance=True` → 立即执行该回合；
- **全员 idle 占位 → 无限等待、不推进**（不烧步）；
- 部分提交 → `STEP_TIMEOUT = 60.0s`（:95）超时 → 给缺失者补 `NoOp`（**消耗步**，记入 `TimeoutAgents`）。

`idle_heartbeat`（worker 空闲期主动占位、不消耗步；`sar_orch/worker.py:577` 附近）与 `timeout_injected`（系统补偿）由 `NoOpSource` 区分。**AI2ThorBarrier 已完成契约对齐**（P5-1 `29fc7c7`）：`submit_action(agent_idx, action, *, advance=True, source=None)` 三条路径与 SAR 一致，补入 NoOp 带来源标记（`llm` / `idle_heartbeat` / `timeout_injected`，缺省按 `advance` 推导），经 `get_run_status().domain_metrics["noop_sources"]` 对外（`ai2thor_orch/barrier/ai2thor_barrier.py`）。

**③ 观测通道**：worker 经 `report_observation` → `[DATA]` JSON 块（内容上限 12000、脱敏）→ A2A push → coordinator 解析摄取（`src/a2a/worker/sink.py`；coordinator 摄取链 `server.py`，main :1087-1138）。环境侧义务：观测走此通道，勿直写协调器私有存储；semantic 模式建议提供观测上报工具（框架不强制，无观测不报错）。

**④ 状态注入**：每个 LLM 请求前 `pre_llm` 钩子刷新注入（router `src/Agent/router_agent/hooks.py:70-84`：prepare → refresh → prune → assemble；worker `worker_agent/hooks.py:70-86`：read_port 下 fetch → refresh → prune → assemble，无 prepare 步）；provider 侧 `version` 变更检测，同一 env step 未变则跳过冗余刷新。assemble 产出稳定前缀（缓存友好）。

**⑤ 终局判定**：环境侧每步在 `_execute_step` 刷新 finished（SAR `barrier.py:613` = checker 判定；`stop()` 时 :456 置位），对外经 `is_finished()`（:349）读取；实验主退出 = `a2a_task.done()`（`experiment.py:1016`），另有墙钟 3600s 与步数上限；mission 级终态：coordinator `FinishTaskTool` 经 `completion_validator`（`server.py:1042 / :1224`）→ `mark_finished`。`RunStatus` 字段：`step / max_steps / finished / stopped / stop_reason / timeout_agents / domain_metrics`（`ai2thor_orch/contracts/types.py:85`）。

**⑥ 并发与线程模型**：barrier 用 threading 原语（worker 各自线程 + 独立 event loop，asyncio 原语跨 loop 不安全——ADR-011）；框架以 `asyncio.to_thread` 承接阻塞调用（env.step、Event.wait）。环境侧义务：**`step` / `reset` 允许同步阻塞实现**；慢调用（如 AI2Thor controller）由环境包自行放入专用执行器（AI2ThorBarrier 置 `max_workers=1` 防与事件等待互相饿死）。

**⑦ 失败模式**：超时补 NoOp（见②）；worker 失联 → TaskWatchdog 事件（120s 阈值，不自动取消）；取消链：coordinator `CancelTaskTool` → A2A → worker 取消标记退出；中止（abort）由 `stop()` / `request_stop()` 承担（实现 RunControl 时）。

## 3. 适配新环境：过渡期操作步骤（现状路径）

> 目标：把新环境接进框架。**P4-4 后装配已通用化**：新环境 = 实现环境包（EnvPack 工厂 + AssemblyHooks 钩子）+ 调用 `run_assembly`；**不要再复制 `sar_orch/experiment.py` 骨架**（P5 起 `ai2thor_orch/experiment/` 即此形态，可作第二个参考）。
> 下文步骤 8/9 已按 P4-4 后路径改写；标 ⚙️ 处为过渡期（P4-4 前）的手动接线点，保留作对照（均已由 P4 收敛，见对应注）。

1. **建包**：新建 `<env>_orch/`，主题件对齐：`barrier/`、`tools/`（worker + 可选 coordinator）、`state/`（providers + 可选 context 子类）、`prompts/{coordinator,worker}/`、`contracts/`（DTO）、`verifier/`、场景/任务定义目录。
2. **barrier**：实现回合语义（§2.2-②）+ 线程模型（§2.2-⑥）+ `stop()`；若要对齐内核 cancel 协议，追加 `EnvironmentRunControl` 三方法（参考 `src/a2a/coordinator/run_control.py`（main 已含，P2a）及 SARBarrier 适配）。
3. **工具**：worker 工具按 `Tool` 基类实现（构造注入 barrier 等运行时依赖；**必须含任务完成工具**，返回 `task_complete=True`，详见 §2.1 第 2 类）；coordinator 工具（如状态查询）同样构造注入。
4. **state provider**：至少 coordinator 侧实现 `snapshot()`；需要 LLM 预处理的实现 `AsyncStatePreparer`。接入参数名：`create_server(state_provider=...)` / `create_worker_a2a_server(state_provider=...)`。
5. **Context 子类**（如需改渲染）：覆写 `_render_environment_view()`。⚙️ G7 已落地：经 `EnvPack.build_session_factory(role=...)` 替换内核缺省 factory（P4-2 `e47a8d0` / P4-3 `0403e20`；SAR 返回 `None` = 内核缺省），无需再改内核 build。
6. **prompts**：写 `<env>/prompts/{coordinator,worker}/system.md`（参考 AI2Thor 的 `system.md` 极简样式；semantic/oracle 模式的 prompt 选择逻辑参考 `experiment.py`）。
7. **DTO / verifier / metrics**：实现场景/任务定义 + 回合校验 + domain metrics 投递（参考 `ai2thor_orch/contracts/`、`verifier/`；SAR checker 亦可）。
8. **装配（P4-4 后）**：复用通用装配骨架 `src/orchestration/assembly.py` 的 `run_assembly`，环境侧只实现两件 + 一个入口壳——
   - **`EnvPack` 工厂**（ABC 见 `src/orchestration/env_pack.py`）：`build_barrier` 为唯一必需产物（第 1 类契约的第一消费点），worker/coordinator 侧工具、state provider、prompts/skills 目录、session factory 等为可选工厂；SAR 参照 `sar_orch/env_pack.py`；
   - **`AssemblyHooks` 钩子**（`src/orchestration/assembly.py`）：环境生命周期差异点（环境就绪产物快照、coordinator 启动接线如 long-term/diagnosis、poll 记账、run 终态评测如 memory/truth）；SAR 参照 `sar_orch/assembly_hooks.py`；
   - **入口壳**（参照 `sar_orch/experiment.py` 薄壳）：CLI 参数面 + 运行目录命名 + `build_run_metadata` + 构造 EnvPack/hooks；
   - `run_assembly(...)` 内已统一承担：coordinator/worker 服务启动、线程、poll 循环、wall-clock 兜底（3600s）、端口选择、日志接线、收尾。
   ⚙️ `sar_orch/experiment.py` 中 P4-4 前的装配锚点（barrier 创建 / coordinator 构造 / worker 构造 / poll 循环 / 收尾）已被 `run_assembly` 取代，仅保留薄壳转发（兼容 re-export）。
9. **接线点清点（P4-4 后）**：过渡期 5 处分散注册（①prompts 路径 ②工具注册 ③state provider 构造 ④context factory ⑤run control 注入）已收敛进 EnvPack 工厂 + AssemblyHooks：新环境 = 实现包 + 钩子 + 调用 `run_assembly`（G10 已落地：P4-2 `e47a8d0` ~ P4-4 `20587c0`）。
10. **验证**：按 §4 分层清单逐层通过再进上层。

## 4. 验证清单（分层，逐层通过再进上层）

| 层 | 内容 | 现状口径 / 命令 |
|---|---|---|
| L1 环境包单测 | fake 模式全量单测 | `env -u PYTHONPATH PYTHONPATH="src" .venv/bin/python -m pytest ai2thor_orch/tests -m "not unity" -q`（ai2thor 树；2026-09-14 实测 266 条全绿） |
| L2 fake E2E | 无 LLM 的端到端回合循环 | 通用装配回路：`tests/test_assembly_wiring.py`（P4-4 `20587c0`）；AI2Thor 薄壳：`ai2thor_orch/tests/test_experiment_shell.py`（P5-3 `a5c97e3`，由 `test_experiment_e2e` 重写） |
| L3 框架回归（护城河） | SAR 零回归——内核/编排任何改动后必跑 | `env -u PYTHONPATH .venv/bin/python -m pytest tests -q`（任一树根；2026-09-14 于 ai2thor 树实测 2175 passed / 8 skipped，tests/ 共 135 个测试文件；注意裸 `pytest` 因 `testpaths=sar_orch,ai2thor_orch` 收不到 tests/，须显式 `pytest tests`） |
| L4 真机 + 真 LLM 实跑 | unity 冒烟（G1/G5 门禁）+ 端到端实验（unity + 真 LLM，GPU 主机） | `scripts/ai2thor_runtime_smoke.py --mode unity` → `python -m ai2thor_orch.experiment --mode unity …`（ai2thor 树）；A100 实测（2026-09-14）：unity 冒烟 7/7 checks / 3.99 s、短跑 8 回合 / 28 s、完整跑 50 回合 / 132 s、`timeout_count=0`——证据 `reports/a100_firstrun_20260914/A100_FIRSTRUN_REPORT.md` |

> 编号对照：本表 L4 = A100 首跑 / runbook 三级流程的 L2（unity 冒烟）+ L3（端到端 = **真机 + 真 LLM 实跑**）两级；三级流程定义与命令见 `docs/system_docs/ai2thor_a100_runbook.md` §2。

## 5. 契约 vs 现状缺口（G1–G12）

> 详细证据（file:line 与复现命令）见本地勘查报告 `.hermes/spec/env-contract/20260913-env-interface-survey.md`。

| # | 缺口 | 归属 / 状态 |
|---|---|---|
| G1 | `RunStatus` 归属场景包（内核 `run_control.py` 与 SAR barrier 均反向 import ai2thor 的 DTO）→ 上移内核 | P2a ✅ 已落地（`ddd0007`） |
| G2 | 内核硬 import SAR `FinishTaskTool`（`src/a2a/coordinator/agent_executor.py:44`） | P2b-1 ✅ 已落地（`3d1c6ad`） |
| G3 | 内核硬编码 SAR `map_agent` MCP 挂载（内核 `server.py` 多处） | P2b-2 ✅ 已落地（`9f5a9b7`） |
| G4 | 内核硬 import SAR `environment_state_provider`（main `server.py:2226`） | P2b-1 ✅ 已落地（`3d1c6ad`） |
| G5 | 编排层 SAR 特化硬编码（state provider / 工具 / 语义图 / summarizer / llm client） | ✅ 已落地（P4）：`e47a8d0`（EnvPack 契约定型、通用 coordinator 抽取至 `src/orchestration/`，coordinator 侧五件套全部经 EnvPack 工厂注入）/ `0403e20`（通用 worker 骨架抽取，worker 侧（工具注册表 / state provider / 运行期接线 / 能力标签 / Action 格式化）全部经 EnvPack 注入，SAR 两侧装配薄壳化）/ `20587c0`（实验装配抽取至 `src/orchestration/assembly.py`——`run_assembly` + `AssemblyHooks`；SAR `experiment.py` 薄壳化、CLI/产物零变化） |
| G6 | prompts/tools 路径为 SAR 硬编码常量；config 配置轴存在但实验装配未消费 | ✅ 已落地（P4）：`bafef44`（prompts 根参数化可注入）/ `e47a8d0`（prompts/skills 目录随 EnvPack 携带（coordinator+worker），coordinator 工具面经 EnvPack 工厂注入）/ `0403e20`（worker 侧 prompts/skills 与工具注册表经 EnvPack 消费）/ `20587c0`（装配层经 EnvPack 消费 prompts——SAR 薄壳显式传 `coordinator_prompts_dir` / `worker_prompts_dir`）。`config.yaml` 轴本身仍仅内核 CLI 消费（见 §2.1 第 9 类） |
| G7 | Context 子类无注入点（build 默认 factory 硬编码基类；`session_factory` 参数无生产调用方） | ✅ 已落地（P4）：`bafef44`（装配处显式传入 `session_factory`、可注入且默认等价）/ `e47a8d0`（`EnvPack.build_session_factory` 落地（SAR 返回 `None` = 内核缺省），`create_server → CoordinatorServer → CoordinatorAgentExecutor` 透传链打通）/ `0403e20`（worker 侧 `create_worker_a2a_server → AgentAdapter / EnvelopeAwareAdapter` 透传链打通）；P5 起 AI2Thor 为其第二实现（`3c200c6`） |
| G8 | `set_run_control` 无生产接线（协议就绪但没人注入；仅 ai2thor 分支） | ✅ 已落地：`bafef44`（装配接线）/ `e47a8d0`（收敛进通用 coordinator——`src/orchestration/coordinator.py:930`，SAR 与 AI2Thor 同经此路径）；无遗留尾项 |
| G9 | AI2Thor 编排层缺失（unity 占位、fake 无 LLM、无 A2A） | ✅ 已落地（P5）：`29fc7c7`（barrier 契约对齐 / RunStatus 收口）→ `3c200c6`（Ai2ThorEnvPack + worker 工具注册表）→ `a5c97e3`（编排装配 + fake E2E）→ `fc591c4`（unity 运行时接线 + G1/G5 冒烟门禁）；真机三级验证全绿（A100 首跑 `e8d127e`，2026-09-14） |
| G10 | "注册一行"不存在——现状 5 处分散注册点（§3 步骤 9） | ✅ 已落地（P4）：`e47a8d0` / `0403e20`（coordinator/worker 骨架的工具、state provider、prompts/skills、session 工厂收敛为 EnvPack 单点）/ `20587c0`（装配层收敛为 `run_assembly` + `AssemblyHooks` 单点——新环境 = 实现 EnvPack 工厂 + 钩子 + 调用一行 `run_assembly`）；按此路径的首次真实接入 = P5（AI2Thor） |
| G11 | 任务 DTO 未定稿（`TaskContract` vs `scene:int`；`TaskSpec` 命名与字段） | ⏸ 仍开放（本版无推进）：现行形态 = AI2Thor `TaskContract`（`ai2thor_orch/contracts/task.py`）白名单 / SAR `scene:int` + 场景初始化器；统一 `TaskSpec` 命名与字段待定 |
| G12 | EnvPack 边界未定义（含哪些物：barrier/tools/prompts/state/context/verifier/DTO/run control；注册形态） | ✅ 已落地：`e47a8d0`（按 §2.1 代码定稿 `src/orchestration/env_pack.py`——ABC + 工厂签名）/ `3c200c6`（`Ai2ThorEnvPack` 第二实现） |

另注：内核 hooks 类名已中性化（`CoordinatorHooks` / `WorkerHooks`，P4-1 改名；见 `src/Agent/router_agent/agent.py:180`、`worker_agent/agent.py:189`；行为本就通用、无 `*_orch` import），不单独设缺口。

另注（2026-09-14，首跑收口）：A100 首跑（`e8d127e`）留证的产物一致性与可观测性问题已修复——`e5ce7f1`（计划节点路径产物一致性：`subtasks.csv` / 顶层 `assign_task`）、`9b58372`（ai2thor 失败归类扩展 / 未归类 `raw_request` 收编 / watchdog 阈值按环境可配 / `assign_task` 写点收敛为 accepted）。

## 6. 索引

- 本地留档（`.hermes/spec/env-contract/`，不进 git）：决策记录 `20260913-contract-driven-decision.md`；校验报告 `20260913-doc-verification.md`；勘查报告 `20260913-env-interface-survey.md`；独立复核报告 `20260913-env-contract-review.md`。
- 真机验证证据（入库）：`reports/a100_firstrun_20260914/`（A100 首跑报告 + L1/L2/L3 产物与留档）；运行流程与排障：`docs/system_docs/ai2thor_a100_runbook.md`。
- 参考实现坐标：通用骨架 = `src/orchestration/`（`env_pack` / `coordinator` / `worker` / `assembly`）；SAR = `sar_orch/`（barrier / 薄壳 experiment·coordinator·worker / env_pack / assembly_hooks / tools / prompts / state providers）；AI2Thor = `feat/ai2thor-scene-adaptation` 分支 `ai2thor_orch/`（env_pack / assembly_hooks / logger / barrier / executor / tools / state / verifier / contracts / prompts / tests）。
- 维护注记：本文档随 P2/P4/P5 进展更新；本版 v0.2.0（2026-09-14，P5 收口）行号基准 = ai2thor@9b58372 / main@f340a46（main 前进注记见 front-matter TODO）。
