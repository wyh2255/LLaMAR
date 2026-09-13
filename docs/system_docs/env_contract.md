---
日期: 2026-09-13
文档类型: 系统架构文档（契约 / 接入指南）
文档概述: 多场景框架的环境包契约与适配指南——三层结构与依赖不变量、环境包必供接口（9 类）、
  框架↔环境调用协议（7 条）、新环境过渡期适配步骤与目标形态（EnvPack + 注册一行）、
  分层验证清单、契约-现状缺口 G1–G12
---

# 环境包契约与适配指南（多场景框架）

> **版本**：v0.1.1（2026-09-13；P2 合并后收口。v0.1 初版同日经独立复核后修订——复核报告 `.hermes/spec/env-contract/20260913-env-contract-review.md`）
> **来源**：2026-09-13 拍板「契约驱动」路线；内容基于独立校验卡 `t_2d32b10b` 实测证据（本地留档 `.hermes/spec/env-contract/`：决策记录 + 校验报告 + 勘查报告）。
> **引用基准**：主仓 `main@9f5a9b7`（P2 后）；AI2Thor 适配层 `feat/ai2thor-scene-adaptation@3c4f4c2`。两树同名文件行号可能不同，引用尽量标注树别；未标注的接口/协议条目为两树共有并已实测。本版对 P2 触及文件（`src/a2a/coordinator/{server,agent_executor,run_control}.py`、`sar_orch/coordinator.py`）的行号做了重锚；P2 未触及文件（experiment.py / barrier.py 等）的行号仍按 e6b95fc 基准可读。
> **读者**：接入新仿真环境的实现者；维护内核与编排层的开发者。

## 0. 30 秒结论

- 框架三层：**内核**（`src/Agent` + `src/a2a`，场景无关）→ **编排层**（coordinator/worker 骨架 + 装配）→ **环境包**（每环境一包，`<env>_orch/`）。
- 目标不变量：**内核绝不 import 任何 `*_orch`**；环境包只依赖内核；编排层经 EnvPack 注入使用环境包。
- 硬判据：**接第 3 个环境 = 新增一个环境包 + 注册一行，内核与编排零改动**（内核侧 P2 落地后已达标；未达成项收窄至 §5 G5–G12）。
- **现状（v0.1.1 时点）**：适配一个新环境 = 实现环境包 + **复制一份装配**（以 `sar_orch/experiment.py` 为模板）+ **5 处手动接线**（§3 步骤 9）。§3 即这条过渡路径的操作手册；**P2 已落地**（内核零 `*_orch` 引用成事实 + 依赖方向守卫测试），剩余收敛 = **P4**（装配参数化）→ **P5**（AI2Thor 接线）。
- 新环境适配最小清单：**①barrier → ②worker 工具 → ③coordinator 工具（如有）→ ④state provider → ⑤Context 子类（如需）→ ⑥prompts → ⑦DTO/verifier/metrics → ⑧装配 → ⑨接线 → ⑩分层验证**。细则见 §3。

## 1. 三层结构与依赖方向

| 层 | 职责 | 场景无关? | 现状 | 目标 |
|---|---|---|---|---|
| 内核 `src/Agent` + `src/a2a` | ReAct / Context / StateProvider 协议、A2A 传输、supervision、memory | ✅ | 零 `*_orch` 引用 ✅（P2 落地；守卫测试） | 保持零引用 |
| 编排层（coordinator / worker / 装配） | 星形编排、回合屏障驱动、轮询、收尾 | ❌ 现状 SAR 特化 | `sar_orch/coordinator.py`、`worker.py`、`experiment.py` | 通用 Orchestrator + EnvPack 注入（P4） |
| 环境包 `<env>_orch/` | barrier、工具、state provider、渲染钩子、prompts、DTO、verifier、metrics | 按环境一份 | SAR 完整；AI2Thor 适配层完整但未接编排（P5） | 不变（每环境一包） |

目标依赖方向（不变量）：

```
环境包 <env>_orch/ ──依赖──▶ 内核 src/Agent + src/a2a ◀──依赖── 编排层（经 EnvPack 注入使用环境包）
```

## 2. 环境包契约

### 2.1 必供接口清单（9 类 + 1 可选）

> "必"＝框架按契约依赖；"可选"＝按需实现。参考实现均实测核对过。

| # | 组件 | 契约（接口 / 要点） | 必/选 | 参考实现与现状注记 |
|---|---|---|---|---|
| 1 | run control / barrier | `EnvironmentRunControl`：`request_stop(reason)` / `stop()` / `get_run_status() -> RunStatus`（`@runtime_checkable`）；barrier 本体承担回合语义（§2.2-②） | **必** | SARBarrier（main `sar_orch/barrier.py:83`）；AI2ThorBarrier（`ai2thor_orch/barrier/ai2thor_barrier.py`）。协议文件 `src/a2a/coordinator/run_control.py` **已合入 main（P2a `ddd0007`）**；`set_run_control` 仍无生产接线（G8） |
| 2 | worker 工具集 | `Tool` 子类（`execute()` → `ToolResult`，框架按 `.success` 判定）；有运行时依赖的（barrier / mailbox…）构造注入并走 `extra_tools` 显式注册；无依赖的声明类可走 `tools_dir` 目录约定；**必须含任务完成工具**（返回 `ToolResult(task_complete=True)`；`require_explicit_completion=True` 时缺它 worker 任务无法正常终结——参考 `sar_orch/tools/worker/finish_task.py`、`ai2thor_orch/tools/worker/done.py`） | **必** | SAR 注册表 `SAR_WORKER_TOOLS`（16 个 = 13 域 + 3 通用，`sar_orch/tools/worker/__init__.py:20`），装配 `sar_orch/worker.py:203 起`；AI2Thor 7 工具（无注册表、未接编排）。无统一注册入口（G10） |
| 3 | coordinator 工具集（如有） | 经 `create_server(extra_tools=[...])` 注入——需要 barrier 实例的工具**不能**走 `tools_dir` | 必（如有） | `sar_orch/coordinator.py:791-793`（构建）/ `:821`（传入 create_server） |
| 4 | state provider（≥coordinator 侧） | `snapshot(context_id) -> RuntimeState`（`version` 单调，变化才刷新）；可选 `AsyncStatePreparer.prepare_for_llm(llm_client)` 做 LLM 前预处理 | **必** | 协议 `src/Agent/router_agent/state_provider.py`（RuntimeState:15 / StateProvider:42 / snapshot:45 / AsyncStatePreparer:56）；SAR 两侧 `sar_orch/coordinator_state_provider.py`、`worker_state_provider.py`；AI2Thor 两侧已实现未接线 |
| 5 | Context 子类（可选） | 覆写 `_render_environment_view()`（另有 `_render_current_state()` 等可选钩子）；基类 `src/Agent/router_agent/context.py`、`worker_agent/context.py` | 可选 | 参考 `ai2thor_orch/state/context.py:22/:77`（仅重写渲染）。现状挂载需改内核 build 默认 factory（G7） |
| 6 | prompts | 目录 `<env>/prompts/{coordinator,worker}/`（至少 `system.md`），经 `prompts_dir` 参数传入 | **必** | SAR：`sar_orch/prompts/…`，实验层以模块常量传（`experiment.py:45-46`，消费 :744-745/:863/:969）；AI2Thor 目录已存在。G6 |
| 7 | 任务 / 场景 DTO | 场景/任务定义 + 注册入口（形态自由） | **必** | AI2Thor：`contracts/task.py:17`（`TaskContract`）+ `AI2Thor/Tasks/<task_id>/checker.py` 白名单；SAR：`scene:int` + `SAR/Scenes/scene_N.py` + `get_scene_initializer`。无统一 TaskSpec（G11） |
| 8 | verifier + domain metrics | 回合/终局校验 + metrics 载体（并投递到 `RunStatus` / `RoundResult`） | **必** | AI2Thor：`verifier/verifier.py`、`contracts/types.py:43/:98`；SAR：checker（`SAR/Scenes/base_checker.py`）+ `barrier.get_metrics()`。无共同载体接口 |
| 9 | 配置注入轴 | `config.yaml` 段（`prompts_dir / tools_dir / skills_dir / log_dir`）+ `load_path_config` 解析 | **必**（半成品） | `src/config/config.yaml:14-21` → `src/a2a/shared/path_config.py:31-68`；现状 SAR 实验装配未消费该轴（G6）。目标：随 EnvPack 携带 |
| + | 额外挂载（可选） | MCP / UI 等附加面 | 可选 | SAR map_agent：挂载/会话生命周期**经注入**（P2b-2 `9f5a9b7`）——内核 hook 消费点 `server.py:1060-1061`、`:2496-2497`、`:1180-1183`/`:1253-1254`；SAR 注入点 `sar_orch/coordinator.py:846-849`。SAR UI（`ui_dir`）不变。G3 已落地 |

### 2.2 框架 ↔ 环境协议（调用契约）

**① 生命周期**：装配方创建 barrier → coordinator（先起服务）→ worker；收尾逆序 `barrier.stop()` → worker → coordinator（守卫式容错）。停止语义：置位并唤醒所有等待者（`threading.Event`）。锚点：`sar_orch/experiment.py:656 / 854 / 958 / 1281`。

**② 回合语义（核心）**：`submit_action(agent_idx, action, *, advance=True, source=None)`（SAR `barrier.py:160`）。三条路径——
- 全员已提交且有人 `advance=True` → 立即执行该回合；
- **全员 idle 占位 → 无限等待、不推进**（不烧步）；
- 部分提交 → `STEP_TIMEOUT = 60.0s`（:95）超时 → 给缺失者补 `NoOp`（**消耗步**，记入 `TimeoutAgents`）。

`idle_heartbeat`（worker 空闲期主动占位、不消耗步；`sar_orch/worker.py:577` 附近）与 `timeout_injected`（系统补偿）由 `NoOpSource` 区分。**AI2ThorBarrier 现状边界**（2026-09-13 复核）：全员提交执行 + 超时补 NoOp 两条路径可用（async `submit_action(agent_idx, action)`），但补入的 NoOp 无来源标记、且 `advance=False` 全 idle 占位（不烧步）路径尚未移植——照它复用前须核对；随 P3/P5 对齐。

**③ 观测通道**：worker 经 `report_observation` → `[DATA]` JSON 块（内容上限 12000、脱敏）→ A2A push → coordinator 解析摄取（`src/a2a/worker/sink.py`；coordinator 摄取链 `server.py`，main :1087-1138）。环境侧义务：观测走此通道，勿直写协调器私有存储；semantic 模式建议提供观测上报工具（框架不强制，无观测不报错）。

**④ 状态注入**：每个 LLM 请求前 `pre_llm` 钩子刷新注入（router `src/Agent/router_agent/hooks.py:70-84`：prepare → refresh → prune → assemble；worker `worker_agent/hooks.py:70-86`：read_port 下 fetch → refresh → prune → assemble，无 prepare 步）；provider 侧 `version` 变更检测，同一 env step 未变则跳过冗余刷新。assemble 产出稳定前缀（缓存友好）。

**⑤ 终局判定**：环境侧每步在 `_execute_step` 刷新 finished（SAR `barrier.py:613` = checker 判定；`stop()` 时 :456 置位），对外经 `is_finished()`（:349）读取；实验主退出 = `a2a_task.done()`（`experiment.py:1016`），另有墙钟 3600s 与步数上限；mission 级终态：coordinator `FinishTaskTool` 经 `completion_validator`（`server.py:1042 / :1224`）→ `mark_finished`。`RunStatus` 字段：`step / max_steps / finished / stopped / stop_reason / timeout_agents / domain_metrics`（`ai2thor_orch/contracts/types.py:85`）。

**⑥ 并发与线程模型**：barrier 用 threading 原语（worker 各自线程 + 独立 event loop，asyncio 原语跨 loop 不安全——ADR-011）；框架以 `asyncio.to_thread` 承接阻塞调用（env.step、Event.wait）。环境侧义务：**`step` / `reset` 允许同步阻塞实现**；慢调用（如 AI2Thor controller）由环境包自行放入专用执行器（AI2ThorBarrier 置 `max_workers=1` 防与事件等待互相饿死）。

**⑦ 失败模式**：超时补 NoOp（见②）；worker 失联 → TaskWatchdog 事件（120s 阈值，不自动取消）；取消链：coordinator `CancelTaskTool` → A2A → worker 取消标记退出；中止（abort）由 `stop()` / `request_stop()` 承担（实现 RunControl 时）。

## 3. 适配新环境：过渡期操作步骤（现状路径）

> 目标：在当前代码（P2/P4 未做）上把新环境接进框架。**装配模板 = `sar_orch/experiment.py`（复制骨架，逐段替换）**。
> 标 ⚙️ 的为"现状必须手动触碰"的接线点——P4 后应全部消失（届时只剩"实现包 + 注册一行"）。

1. **建包**：新建 `<env>_orch/`，主题件对齐：`barrier/`、`tools/`（worker + 可选 coordinator）、`state/`（providers + 可选 context 子类）、`prompts/{coordinator,worker}/`、`contracts/`（DTO）、`verifier/`、场景/任务定义目录。
2. **barrier**：实现回合语义（§2.2-②）+ 线程模型（§2.2-⑥）+ `stop()`；若要对齐内核 cancel 协议，追加 `EnvironmentRunControl` 三方法（参考 `src/a2a/coordinator/run_control.py`（main 已含，P2a）及 SARBarrier 适配）。
3. **工具**：worker 工具按 `Tool` 基类实现（构造注入 barrier 等运行时依赖；**必须含任务完成工具**，返回 `task_complete=True`，详见 §2.1 第 2 类）；coordinator 工具（如状态查询）同样构造注入。
4. **state provider**：至少 coordinator 侧实现 `snapshot()`；需要 LLM 预处理的实现 `AsyncStatePreparer`。接入参数名：`create_server(state_provider=...)` / `create_worker_a2a_server(state_provider=...)`。
5. **Context 子类**（如需改渲染）：覆写 `_render_environment_view()`。⚙️ 现状挂载需改内核 build 默认 factory（main 树：`src/Agent/router_agent/build.py:256-263`、`worker_agent/build.py:242`；ai2thor 树为 `build.py:244-247`、`worker build.py:228`）——G7。
6. **prompts**：写 `<env>/prompts/{coordinator,worker}/system.md`（参考 AI2Thor 的 `system.md` 极简样式；semantic/oracle 模式的 prompt 选择逻辑参考 `experiment.py`）。
7. **DTO / verifier / metrics**：实现场景/任务定义 + 回合校验 + domain metrics 投递（参考 `ai2thor_orch/contracts/`、`verifier/`；SAR checker 亦可）。
8. **装配**：复制 `sar_orch/experiment.py` 骨架，按下列锚点逐段替换——
   - prompts 常量 `:45-46` → 你的 `<env>/prompts/…`；
   - barrier 创建 `:656` → `<Env>Barrier(...)`；
   - coordinator 构造 `:854`（构造参数仅 `prompts_dir`；`extra_tools` / state provider 在类内装配——`coordinator.py:717-751`、`:791-793`；P2 注入 kwarg `finish_task_tool_factory` / `environment_state_provider_factory` / `map_mcp_mount_hook` / `mcp_session_lifecycle_provider` 位于 `:846-849`）；
   - worker 构造 `:958`（同上；类内装配点 `worker.py:203`、`:467`）；
   - poll 主循环 `:1016`（`a2a_task.done()` 退出条件）与墙钟 `:696`；
   - 收尾 `:1281`（`barrier.stop()` → worker → coordinator，守卫式）。
   ⚙️ 这是现状最大工作量——"第 3 个环境 = 第 3 份装配"的根源；P4 收敛为通用 Orchestrator。
9. **接线点清点（现状 5 处分散注册）**：①prompts 路径（模块常量）②工具注册（`extra_tools` 或 `tools_dir`）③state provider 构造 ④context factory ⑤run control 注入。⚙️ 逐处手动；P1 契约 + P4 后收敛为"注册一行"（G10）。
10. **验证**：按 §4 分层清单逐层通过再进上层。

## 4. 验证清单（分层，逐层通过再进上层）

| 层 | 内容 | 现状口径 / 命令 |
|---|---|---|
| L1 环境包单测 | fake 模式全量单测 | `cd LLaMAR-ai2thor && PYTHONPATH="src:$PYTHONPATH" .venv/bin/python -m pytest ai2thor_orch/tests -m "not unity" -q`（现有 152 条全绿） |
| L2 fake E2E | 无 LLM 的端到端回合循环 | `ai2thor_orch/tests/test_experiment_e2e.py` |
| L3 框架回归（护城河） | SAR 零回归——内核/编排任何改动后必跑 | `cd LLaMAR && .venv/bin/python -m pytest tests -q`（tests/ 共 2023 条 / 119 文件；注意裸 `pytest` 因 `testpaths=sar_orch` 收 0，须显式 `pytest tests`） |
| L4 真机 smoke | unity / 真实仿真（GPU 主机） | `scripts/ai2thor_runtime_smoke.py --mode unity`（ai2thor 树）→ 真实实验 runner |

## 5. 契约 vs 现状缺口（G1–G12）

> 详细证据（file:line 与复现命令）见本地勘查报告 `.hermes/spec/env-contract/20260913-env-interface-survey.md`。

| # | 缺口 | 归属 / 状态 |
|---|---|---|
| G1 | `RunStatus` 归属场景包（内核 `run_control.py` 与 SAR barrier 均反向 import ai2thor 的 DTO）→ 上移内核 | P2a ✅ 已落地（`ddd0007`） |
| G2 | 内核硬 import SAR `FinishTaskTool`（`src/a2a/coordinator/agent_executor.py:44`） | P2b-1 ✅ 已落地（`3d1c6ad`） |
| G3 | 内核硬编码 SAR `map_agent` MCP 挂载（内核 `server.py` 多处） | P2b-2 ✅ 已落地（`9f5a9b7`） |
| G4 | 内核硬 import SAR `environment_state_provider`（main `server.py:2226`） | P2b-1 ✅ 已落地（`3d1c6ad`） |
| G5 | 编排层 SAR 特化硬编码（state provider / 工具 / 语义图 / summarizer / llm client） | P4（P4-2：EnvPack 契约定型、通用 coordinator 骨架抽取至 `src/orchestration/`；coordinator 侧五件套已全部经 EnvPack 工厂注入；worker 侧随 P4-3） |
| G6 | prompts/tools 路径为 SAR 硬编码常量；config 配置轴存在但实验装配未消费 | P4（P4-1：prompts 根已参数化可注入；P4-2：prompts/skills 目录随 EnvPack 携带（coordinator+worker），coordinator 工具面经 EnvPack 工厂注入；worker 工具面随 P4-3） |
| G7 | Context 子类无注入点（build 默认 factory 硬编码基类；`session_factory` 参数无生产调用方） | P4（P4-1：装配处已显式传入 `session_factory`、可注入且默认等价；P4-2：`EnvPack.build_session_factory` 落地（SAR 返回 `None` = 内核缺省），并打通 `create_server → CoordinatorServer → CoordinatorAgentExecutor` 透传链；worker 侧随 P4-3） |
| G8 | `set_run_control` 无生产接线（协议就绪但没人注入；仅 ai2thor 分支） | P4（P4-1：SAR 装配已接线 `set_run_control(barrier)`）/ P5 |
| G9 | AI2Thor 编排层缺失（unity 占位、fake 无 LLM、无 A2A） | P5 |
| G10 | "注册一行"不存在——现状 5 处分散注册点（§3 步骤 9） | P1 / P4 |
| G11 | 任务 DTO 未定稿（`TaskContract` vs `scene:int`；`TaskSpec` 命名与字段） | P1 |
| G12 | EnvPack 边界未定义（含哪些物：barrier/tools/prompts/state/context/verifier/DTO/run control；注册形态） | P1（文档侧：§2.1 九类）；P4-2：按 §2.1 代码定稿 `src/orchestration/env_pack.py`（ABC + 工厂签名） |

另注：内核 hooks 类名已中性化（`CoordinatorHooks` / `WorkerHooks`，P4-1 改名；见 `src/Agent/router_agent/agent.py:180`、`worker_agent/agent.py:189`；行为本就通用、无 `*_orch` import），不单独设缺口。

## 6. 索引

- 本地留档（`.hermes/spec/env-contract/`，不进 git）：决策记录 `20260913-contract-driven-decision.md`；校验报告 `20260913-doc-verification.md`；勘查报告 `20260913-env-interface-survey.md`；独立复核报告 `20260913-env-contract-review.md`。
- 参考实现坐标：SAR = 主仓 `sar_orch/`（barrier / worker / coordinator / experiment / tools / prompts / state providers）；AI2Thor 适配层 = `feat/ai2thor-scene-adaptation` 分支 `ai2thor_orch/`（barrier / executor / tools / state / verifier / contracts / prompts / tests）。
- 维护注记：本文档随 P2/P4/P5 进展更新；本版 v0.1.1（2026-09-13，P2 合并后收口）行号基准 = main@9f5a9b7（P2 后）/ ai2thor@3c4f4c2。
