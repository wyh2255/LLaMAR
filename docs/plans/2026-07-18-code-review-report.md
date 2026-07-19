# 计划审查报告：2026-07-18-map-agent-mcp-and-team-coordination.md

**审查日期**: 2026-07-19
**审查范围**: 逐条核对计划中的技术声明与 `/home/wyh/daily_work/LLaMAR` 项目真实代码的一致性
**审查人**: Hermes Agent（严格代码审查模式）

---

## 1. 第 2.1 节「代码事实」逐条核对

### 事实 1: `query_shared_memory` 工具路径
**声明**: `sar_orch/tools/worker/query_shared_memory.py`，通过 HTTP GET `/semantic-map`
**结果**: ✅ 与代码一致
**证据**: 
- 文件存在：`sar_orch/tools/worker/query_shared_memory.py:1-20`
- `QuerySharedMemoryTool.execute()` 调用 `client.get(f"{self._semantic_map_url}/semantic-map")` (第18行)

### 事实 2: Worker 工具注册机制
**声明**: `sar_orch/tools/worker/__init__.py` 导出 `SAR_WORKER_TOOLS`；`sar_orch/worker.py` 按类名特判构造
**结果**: ✅ 与代码一致
**证据**:
- `SAR_WORKER_TOOLS` 在 `sar_orch/tools/worker/__init__.py:21-39` 定义为类列表（而非实例列表）
- `sar_orch/worker.py:250-274` 对每个类名特判构造：
  - `QuerySharedMemoryTool` → `tool_cls(semantic_map_url=http_url)`
  - `ReportObservationTool` → 传入 `agent_name`, `task_id`, `get_step`
  - `ReadMailboxTool` / `A2ASendMailTool` → 条件创建

### 事实 3: MCP 客户端已实现
**声明**: `src/Agent/worker_agent/tools/mcp_loader.py`，支持 stdio/SSE/streamable_http
**结果**: ✅ 与代码一致
**证据**:
- 文件存在：`src/Agent/worker_agent/tools/mcp_loader.py` (451行)
- 三个连接方法：`_connect_stdio` (行250), `_connect_sse` (行257), `_connect_streamable_http` (行271)
- `ConnectionType = Literal["stdio", "sse", "http", "streamable_http"]` (行18)

### 事实 4: MCP 包当前状态
**声明**: `uv.lock` 有 `mcp 1.28.0`（来自 anthropic），但 `pyproject.toml` 未声明；`uv run` 环境下 `import mcp` 失败；` .venv/lib/python3.10/site-packages/` 可能有

**结果**: ⚠️ 部分正确，但有 2 个错误

**证据**:
- ✅ `uv.lock` 确实包含 `mcp 1.28.0` — 已确认
- ❌ **"来自 anthropic" — 错误**。`uv.lock` 显示 `mcp` 是 `mini-agent` 的依赖（来自 Git `feature/task-context` 分支），不是 `anthropic` 的依赖。
  ```
  source = { git = "https://github.com/wyh2255/Mini-Agent.git?rev=feature%2Ftask-context..." }
  dependencies = [{ name = "mcp" }, ...]
  ```
- ✅ `pyproject.toml` 未声明 `mcp` — 正确
- ✅ `uv run python -c "import mcp"` 失败 — 已确认
- ❌ **"`.venv/lib/python3.10/site-packages/` 可能有" — 错误**。实际检查：`.venv` 的 site-packages 中**没有** `mcp` 包。`uv sync --all-groups` 后仍然没有（因为 `mini-agent` 也未安装成功）。
- ⚠️ 补充：系统全局 Python 通过 `pip install mcp` 安装了 `mcp 1.27.2`（通过 `pip list | grep mcp` 确认），但项目 `.venv` 无此包。

### 事实 5: MCP server 通过配置文件加载
**声明**: `mcp_loader.load_mcp_tools(config_path)` 读 `mcp.json`，格式 `{"mcpServers": {...}}`
**结果**: ❌ 函数名错误

**证据**:
- `mcp_loader.py` 中定义的是 **`load_mcp_tools_async`** (行348)，而不是 `load_mcp_tools`
- ✅ 配置文件格式确实是 `{"mcpServers": {...}}` (行389)
- ✅ 配置文件解析支持自动降级到 `mcp-example.json`

### 事实 6: Coordinator FastAPI 端点
**声明**: `src/a2a/coordinator/server.py`，已有 `/semantic-map`、`/map/state` 等端点
**结果**: ✅ 与代码一致
**证据**:
- `/semantic-map` → `server.py:828-835`
- `/map/state` → `server.py:837-867`（SSE 端点，轮询 `barrier.get_env_snapshot()`）
- 还有 `/tasks`, `/logs`, `/ui` 等端点

### 事实 7: `SARCoordinator.submit_task` 实现方式
**声明**: 用 A2A SDK `send_message`，worker 通过 `finish_task` 回传
**结果**: ✅ 与代码一致
**证据**:
- `sar_orch/coordinator.py:450-485` — 使用 `a2a.client.create_client` 发送 `SendMessageRequest`
- 使用 `google.protobuf` 构建 `Message`/`Part`
- 通过 A2A 协议（不是 HTTP REST）

### 事实 8: `CoordinatorTeamRegistry` / `WorkerTeamState` 关系
**声明**: `CoordinatorTeamRegistry` 维护 team_id/members/endpoints/shared_secret；`WorkerTeamState` 是只读副本
**结果**: ✅ 与代码一致
**证据**:
- `src/a2a/coordinator/team_registry.py:67` — `CoordinatorTeamRegistry`
  - 持有 `_team_id`, `_members`, `_team_secret`
  - `configure_for_delivery()` 返回 `DeliveryPlan(dto, team_secret)`
- `tests/test_worker_team_state.py` — `WorkerTeamState` 测试存在
- `a2a.worker.team_state.WorkerTeamState` — 通过 `ts.current()` 获取只读 DTO

### 事实 9: Peer mail HMAC 签名
**声明**: `WorkerPeerSenderService.send_mail` + `WorkerMailboxStore`，HMAC 签名
**结果**: ✅ 与代码一致
**证据**:
- `src/a2a/shared/message_envelope.py:221-235` — `sign_envelope()` 使用 `hmac.new(secret, canonical, hashlib.sha256).hexdigest()`
- `src/a2a/coordinator/sender_service.py` — `CoordinatorSenderService.send_mail` 调用 `sign_envelope`

### 事实 10: `SARWorkerStateProvider.semantic_map_url` 使用状态
**声明**: 已传入但**当前未使用**（`_extract_fires_from_obs` 只解析本地观测文本）
**结果**: ✅ 与代码一致
**证据**:
- `sar_orch/worker_state_provider.py:46-48` — `self._semantic_map_url` 被保存
- 但 `snapshot()` 方法 (行55-154) 中**完全未使用** `self._semantic_map_url`
- `_extract_fires_from_obs` (行156-164) 只解析观测文本字符串

### 事实 11: Worker context 已有 pinned state 渲染
**声明**: `WorkerContextManager._render_environment_view` / `_render_current_state`
**结果**: ✅ 与代码一致
**证据**:
- `src/Agent/worker_agent/context.py:831-846` — `WorkerContextManager._render_environment_view()`
- `src/Agent/worker_agent/context.py:848-866` — `WorkerContextManager._render_current_state()`
- `src/Agent/worker_agent/context.py:868-900` — `_render_mailbox_reminder()`
- ❌ 补充：`_render_team_coordination` **尚不存在**（这符合计划预期）

---

## 2. 第 4 节「接口契约」可行性核查

### 2.1 FastMCP `@mcp.tool()` 装饰器是否存在
**声明**: `from mcp.server.fastmcp import FastMCP`，然后用 `@mcp.tool()` 装饰
**结果**: ✅ 可行
**证据**:
- `python3 -c "from mcp.server.fastmcp import FastMCP; m=FastMCP('test'); print(dir(m))"` 成功执行
- `mcp.server.fastmcp.FastMCP` 的 `tool()` 装饰器是 FastMCP 的标准 API

### 2.2 `mcp.streamable_http_app()` 能否 mount 到现有 FastAPI
**声明**: `app.mount("/mcp/map", mcp.streamable_http_app())`
**结果**: ✅ 可行
**证据**:
- `FastMCP` 实例确实有 `streamable_http_app` 属性（从 `dir(m)` 确认）
- `starlette.applications.Starlette.mount()` 是 FastAPI 的标配功能
- `python3 -c "from fastapi import FastAPI; app=FastAPI(); app.mount('/mcp/map', 'test')"` 成功

### 2.3 4 个工具的返回字段能否从 `SemanticMapStore.snapshot()` 派生
**声明**: 所有字段从 `snapshot()` 裁剪/计算
**结果**: ✅ 可行
**证据**:
- `SemanticMapStore._build_snapshot_locked()` (行324-349) 返回:
  - `known_dynamic_objects.fires` → `SemanticObject.to_dict()` (含 `sources`, `confidence`, `last_seen_ts`, `last_seen_step`, `attributes` 含 `observed_cells`)
  - `known_dynamic_objects.persons` → 同上
  - `known_priors.reservoirs` / `deposits`
  - `agents` → `AgentSemanticState.to_dict()` (含 `current_task_id`, `task_state`, `last_position`, `inventory`)
- 裁剪 `sources`/`observed_cells`/`confidence`/`last_seen_ts` 的规则在计划中正确描述
- 计算 `nearest_reservoir_with_sand` / `nearest_deposit` / `carriers` 需要额外逻辑（计划已提到）— 这些**不是** snapshot 直接提供的派生字段

### 2.4 `/team-status` 端点所需的 `barrier.get_env_snapshot()` 字段
**声明**: 包含 position/inventory
**结果**: ✅ 可行
**证据**:
- `sar_orch/barrier.py:238-284` — `get_env_snapshot()` 返回 `agents` 列表
- 每个 agent 包含 `name`, `position`, `inventory` (行261-264)
- 此外还有 `fires` (含 `fire_type`, `average_intensity`), `persons` (含 `status`, `load`)

---

## 3. 第 7 节「目录与文件改动」核查

### 3.1 新增文件路径
| 路径 | 是否存在 | 备注 |
|------|---------|------|
| `sar_orch/map_agent/__init__.py` | ❌ 不存在 | 新目录，合理 |
| `sar_orch/map_agent/server.py` | ❌ 不存在 | 新目录，合理 |
| `sar_orch/map_agent/tools.py` | ❌ 不存在 | 新目录，合理 |
| `sar_orch/map_agent/llm_query.py` | ❌ 不存在 | 新目录，合理 |
| `docs/plans/2026-07-18-...md` | ✅ 存在 | 本文档 |

### 3.2 修改文件路径
| 路径 | 是否存在 | 备注 |
|------|---------|------|
| `pyproject.toml` | ✅ 存在 | 合理 |
| `src/a2a/coordinator/server.py` | ✅ 存在 | 合理 |
| `sar_orch/coordinator.py` | ✅ 存在 | 合理 |
| `sar_orch/worker.py` | ✅ 存在 | 合理 |
| `sar_orch/worker_state_provider.py` | ✅ 存在 | 合理 |
| `src/Agent/worker_agent/context.py` | ✅ 存在 | 合理 |
| `sar_orch/tools/worker/__init__.py` | ✅ 存在 | 合理 |
| `sar_orch/tools/worker/query_shared_memory.py` | ✅ 存在 | 合理 |
| 多个 docs 文件 | ✅ 存在 | 合理 |

**结论**: 所有文件路径核查通过（新文件不存在是正常的，因为此计划尚未实施）

---

## 4. Phase 1-5 实施步骤代码核查

### 4.1 Phase 1: `from mcp.server.fastmcp import FastMCP`
**声明**: `from mcp.server.fastmcp import FastMCP`
**结果**: ✅ 可导入（系统 Python 全局安装有 mcp 1.27.2）
**风险**: ⚠️ 项目 `.venv` 没有安装 `mcp` 包，实施时需要先加 `pyproject.toml` 依赖并 `uv sync`

### 4.2 Phase 2: `load_mcp_tools` 签名
**声明**: `mcp_tools = await load_mcp_tools(str(mcp_config_path))`
**结果**: ❌ 函数名错误
**证据**:
- `mcp_loader.py:348` 定义的是 `async def load_mcp_tools_async(config_path: str = "mcp.json")`
- 没有叫 `load_mcp_tools` 的函数
- 应改为：`mcp_tools = await load_mcp_tools_async(str(mcp_config_path))`

### 4.3 Phase 4: `SARWorkerStateProvider.snapshot()` 同步 vs 异步
**声明**: Plan 代码 (行724) 写的是 `async def snapshot(self, context_id=None)`，内部用 `async with httpx.AsyncClient` 和 `await client.get`
**结果**: ❌ 与现有代码严重冲突
**证据**:
- 现有实现 `sar_orch/worker_state_provider.py:55` 是 **`def snapshot(self, context_id=None)`**（同步方法）
- 调用方 `ContextManager.refresh_runtime_state()` (context.py:201) 调用 `self._state_provider.snapshot(context_id)` — 也是**同步调用**
- 如果改为 `async def snapshot`，调用方（以及所有其他调用方）必须改为 `await` 调用

**影响**: ⚠️ 这是**大问题**。计划需要在 Phase 4 中明确说明：
   1. 需要同时修改调用方 `ContextManager.refresh_runtime_state()` 以支持 `await`
   2. 或者保持 `snapshot()` 同步（改用 `httpx.Client` 而非 `AsyncClient`）
   3. 这会波及到：`src/Agent/worker_agent/context.py:201`, `src/Agent/router_agent/state_provider.py` 基类

### 4.4 MCP 自动前缀
**声明**: `map_agent__<tool>` 命名遵循 `<server>__<tool>` 约定（MCP client 自动加前缀）
**结果**: ⚠️ 部分正确，有遗漏风险
**证据**:
- `mcp_loader.py` 中的 `MCPTool` 类 (行60) 不自动加前缀 — 工具名直接从 MCP server 的 `list_tools()` 获取
- `MCPServerConnection.connect()` (行203) 调用 `session.list_tools()`，工具名就是 server 注册的名字
- 所以「自动加前缀」**不成立** — 前缀需要在 FastMCP server 端的工具注册时显式命名，或在 worker 工具合并逻辑中映射
- **修正建议**: 在 `sar_orch/map_agent/server.py` 注册工具时使用带前缀的函数名，如 `@mcp.tool(name="map_agent__get_fire_info")`；或者保持工具名简洁，让 worker 端的工具列表用配置映射

---

## 5. 特别检查

### 5.1 `AgentSemanticState.to_dict()` 字段
**字段**: `agent_id`, `last_position`, `inventory`, `current_task_id`, `task_state`, `last_seen_step`, `last_message`
**确认**: ✅ `/team-status` 端点需要的 `current_task_id` 和 `task_state` 确实存在

### 5.2 `SemanticObject.to_dict()` 字段
**字段**: `sources`, `confidence`, `last_seen_ts`, `last_seen_step`, `attributes` (含 `observed_cells`)
**确认**: ✅ 计划中的裁剪规则是正确的 — 这些字段都存在且应该被剔除

### 5.3 `observed_cells` 的位置
**确认**: ⚠️ `observed_cells` 不是 `SemanticObject` 的顶层字段，而是存储在 `attributes["observed_cells"]` 中（行386）。计划说「展开为 regions」是正确的路径，但实际实现要注意从 `attributes` 字典读取。

### 5.4 MCP config `type` 字段合法值
**确认**: ✅ `"streamable_http"` 是合法值
**证据**:
- `mcp_loader.py:18`: `ConnectionType = Literal["stdio", "sse", "http", "streamable_http"]`
- `mcp_loader.py:306-314`: `_determine_connection_type` 接受这些值
- 如果 URL 存在且未指定 type，默认推断为 `"streamable_http"`

### 5.5 `mcp.json` 文件存在性
**确认**: 项目根目录下没有任何 `mcp.json` 或 `mcp-example.json` 文件。这是合理的 — MCP 配置目前不存在，因为 Map Agent 尚未实施。

### 5.6 `sar_orch/map/__init__.py` 存在性
**确认**: ✅ 存在，并导出 `SemanticMapStore` 等
**路径**: `sar_orch/map/__init__.py` (不是 `sar_orch/map/store.py` 里的 `__all__` — 库的实际导出由 `__init__.py` 控制)

---

## 6. 总体评估

### 可执行性比例: 75%

计划整体方向正确，大部分代码事实准确，数据流设计合理。

### 必须先解决的 Top 3 问题

1. **🔴 严重: `SARWorkerStateProvider.snapshot()` 同步 vs 异步冲突**
   - Plan Phase 4 代码写的是 `async def snapshot`，但现有实现是同步方法，且调用方 `ContextManager.refresh_runtime_state()` 也是同步调用
   - **必须**：要么用同步 httpx，要么同时改造 `StateProvider` 基类和所有调用方以支持 async
   - **涉及文件**: `sar_orch/worker_state_provider.py:55`, `src/Agent/worker_agent/context.py:201`, `src/Agent/router_agent/state_provider.py` (如果有基类)
   - **建议**: 使用同步 httpx (加 `asyncio.to_thread` 或用 `httpx.Client`) 保持 `snapshot()` 同步，改动最小

2. **🔴 严重: `load_mcp_tools` 函数名错误**
   - 实际函数名是 `load_mcp_tools_async` 而非 `load_mcp_tools`
   - **建议**: 计划代码使用 `await load_mcp_tools_async(str(mcp_config_path))`
   - 虽然是命名小问题，但不修正会导致 Phase 2 无法编译

3. **🟡 中等: MCP 包未安装 + 依赖来源错误**
   - `mcp` 包不在 `.venv` 中，需要先加入 `pyproject.toml`（主依赖或 `[tool.uv.sources]` 下）
   - 计划说「来自 anthropic」— 实际来自 `mini-agent`（间接依赖）
   - `uv.lock` 中有 `mcp 1.28.0` 但未实际安装 — 需 `uv sync --all-groups` 或明确添加
   - **建议**: 在 `pyproject.toml` 的 `dependencies` 中加入 `"mcp>=1.28.0"`，然后 `uv sync`；同时修正文档中关于"来自 anthropic"的不准确描述

### 其他值得注意的问题

4. **🟡 中等: MCP 工具前缀机制**
   - 计划说明书说「MCP client 自动加前缀」— 这不符合现有 `mcp_loader.py` 实现
   - **建议**: 在 `sar_orch/map_agent/server.py` 注册时指定 `name` 参数：`@mcp.tool(name="map_agent__get_fire_info")`
   - 或者在 `sar_orch/worker.py` 的工具合并逻辑中添加前缀映射

5. **🟢 轻微: `mcp.json` 文件名**
   - 计划 Phase 2 写 `mcp_config_path = Path(log_dir) / f"mcp_{agent_name}.json"`
   - `mcp_loader.py` 的 `_resolve_mcp_config_path` 如果找不到指定文件，会尝试降级到同目录下的 `mcp-example.json`
   - 使用 `mcp_{agent_name}.json` 命名不会触发此降级逻辑，需确保文件确实写入

6. **🟢 轻微: barrier.get_env_snapshot() 返回的 agent 字段**
   - `get_env_snapshot` 返回的 agent 有 `inventory` (dict 类型，行263: `getattr(obj, 'inventory', {})`)
   - 但计划 Phase 4 的 `/team-status` 代码 (行707) 写的是 `inv = live_data.get("inventory", [])` — 实际返回的是 dict 不是 list，需要确认 barrier 返回格式是否兼容
   - **建议**: 核查 `obj.inventory` 的实际类型（可能是 dict 表示库存物品的计数）

### 综合修正建议

| 阶段 | 需修正项 | 严重度 |
|------|---------|--------|
| Phase 0 | `pyproject.toml` 加 `mcp>=1.28.0` | 高 |
| Phase 1 | `server.py` 中 `@mcp.tool(name="map_agent__get_fire_info")` 显式命名 | 中 |
| Phase 2 | `load_mcp_tools` → `load_mcp_tools_async` | 高 |
| Phase 4 | `snapshot()` 保持同步或用同步 httpx 调用 | 最高 |
| 文档 | "来自 anthropic" → "来自 mini-agent" | 低 |
| 文档 | "但 `.venv/lib/...可能有`" → 改为"当前未安装，需添加依赖" | 低 |
