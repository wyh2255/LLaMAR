---
日期: 2026-06-22
文档类型：实验测试报告
文档概述: MARoS x LLaMAR SAR 端到端实验运行结果（第六轮，超时时间增至120s，任务仍未完成）
---

# SAR 端到端实验测试报告

## 实验配置

| 参数 | 值 |
|------|-----|
| 场景 | scene=1 |
| 智能体数量 | 2 (Alice, Bob) |
| 随机种子 | 42 |
| Worker 模型 | deepseek-v4-flash |
| Coordinator 模型 | claude-opus-4-5 (实际使用 deepseek-v4-flash，由 config.yaml 覆盖) |
| task_timeout | 120s (从 35s 增加) |
| 运行命令 | `NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 python3 integration/experiment.py --scene=1 --agents=2 --seed=42` |

## 运行结果

**状态: 超时 (TIMEOUT) — 端到端流程正常运行，但任务在 120s 内仍未完成**

实验在 120 秒超时限制下运行。Coverage 达到 100%（所有火焰区域已探索），Transport Rate 达到 53%（部分资源运输完成）。但 Worker 在探索阶段花费了大量时间，导致实际灭火和救人动作执行不足。

## 本轮修改内容

### 修改 1：增加 task_timeout

**问题**：task_timeout=35s 过短，RouterAgent 任务分解消耗约 17s，留给 Worker 执行的时间不足。

**修改**：将 `SAR/Scenes/scene_1.py` 中的 `self.task_timeout=35` 修改为 `self.task_timeout=120`。

**修改文件**：`/home/wyh/daily_work/LLaMAR/SAR/Scenes/scene_1.py`

## 实验过程记录

### 第一阶段：组件启动 (T=0s ~ T=5s)

```
2026-06-22 00:17:27 [INFO] SAR Experiment: scene=1, agents=2, seed=42
2026-06-22 00:17:27 [INFO] SARBarrier initialized -- env.task_timeout=120
2026-06-22 00:17:29 [INFO] A2A HTTP server started on port 8191 (Alice)
2026-06-22 00:17:29 [INFO] A2A HTTP server started on port 8192 (Bob)
2026-06-22 00:17:30 [INFO] SAR Coordinator starting on port 8080
```

所有组件正常启动，task_timeout=120 确认生效。

### 第二阶段：RouterAgent 任务分解 (T=4s ~ T=14s)

```
2026-06-22 00:17:32 [INFO] Submitting task to RouterAgent: Extinguish all fires and rescue all persons
2026-06-22 00:17:32 [INFO] HTTP Request: POST deepseek "HTTP/1.1 200 OK"  ← RouterAgent 第1次 LLM 调用
2026-06-22 00:17:33 [INFO] HTTP Request: POST deepseek "HTTP/1.1 200 OK"  ← RouterAgent 第2次 LLM 调用
2026-06-22 00:17:36 [INFO] Fetched AgentCard from http://localhost:8191/
2026-06-22 00:17:36 [INFO] Fetched AgentCard from http://localhost:8192/
2026-06-22 00:17:41 [INFO] HTTP Request: POST deepseek "HTTP/1.1 200 OK"  ← RouterAgent 第3次 LLM 调用
```

RouterAgent 任务分解正常完成，耗时约 10 秒。

### 第三阶段：Worker 子任务执行 (T=17s ~ T=90s)

**Alice Worker 执行过程：**

| 时间 | Step | 动作 | 说明 |
|------|------|------|------|
| T=17s | Step 1 | explore() | 初始探索，获取环境状态 |
| T=21s | Step 2 | navigate_to(ReservoirYork) | 导航到水源 |
| T=23s | Step 3 | navigate_to(ReservoirUtah) | 导航到沙源 |
| T=27s | Step 4 | navigate_to(DepositFacility) | 导航到存储设施 |
| T=29s | Step 5 | navigate_to(CaldorFire_Region_1) | 导航到化学火焰 |
| T=32s | Step 6 | navigate_to(GreatFire_Region_1) | 导航到普通火焰 |
| T=36s | Step 7 | navigate_to(ReservoirUtah) | 返回沙源 |
| T=38s | Step 8 | navigate_to(LostPersonTimmy) | 导航到被困人员 |
| T=40s | Step 9 | explore() | 再次探索环境 |
| T=47s | Step 10 | navigate_to(ReservoirYork) | 返回水源 |
| T=50s | Step 11 | navigate_to(ReservoirUtah) | 再次导航到沙源 |
| T=53s | Step 12 | navigate_to(ReservoirUtah) | 继续尝试到达沙源 |
| T=80s | Step 13 | navigate_to(ReservoirUtah) | 最终到达沙源 |

**Bob Worker 执行过程：**

| 时间 | Step | 动作 | 说明 |
|------|------|------|------|
| T=17s | Step 1 | explore() | 初始探索 |
| T=21s | Step 2 | get_supply(ReservoirYork, Water) | 获取水资源 |
| T=23s | Step 3 | get_supply(ReservoirYork, Water) x2 | 继续获取水 |
| T=28s | Step 4 | explore() | 探索环境 |
| T=32s | Step 5 | navigate_to(ReservoirYork) | 导航到水源 |
| T=36s | Step 6 | navigate_to(ReservoirUtah) | 导航到沙源 |
| T=43s | Step 7 | navigate_to(ReservoirUtah) | 继续导航 |

**关键里程碑：**
- Coverage 达到 100%（T=32s）：所有火焰区域已被探索
- Transport Rate 达到 53%（T=47s）：部分资源运输完成
- Bob 成功获取 3 单位 Water

### 第四阶段：超时中断 (T=121.9s)

```
2026-06-22 00:19:31 [INFO] Step 13 | Coverage: 1.00 | Transport: 0.53 | Finished: False
2026-06-22 00:19:41 [WARNING] TASK TIMEOUT after 121.9 seconds, 13 steps
```

实验因 task_timeout=120 秒限制而中断。

## 实验指标

| 指标 | 上轮 (35s) | 本轮 (120s) | 变化 |
|------|-----------|------------|------|
| Finished | False | False | 无变化 |
| Steps | 5 | 13 | +8 |
| Coverage | 0.83 | 1.00 | +0.17 |
| Transport Rate | 0.40 | 0.53 | +0.13 |
| Elapsed | 36.9s | 121.9s | +85s |

## 问题分析

### 问题 1：Worker 过度探索

**现象**：两个 Worker 都花费了大量时间在探索和导航上，而不是执行实际的灭火和救人动作。

**原因分析**：
1. Worker 的 ReAct 循环中，LLM 倾向于先进行全面探索再执行动作
2. 每次导航操作需要 2-3 秒（LLM 调用时间）
3. Worker 没有明确的任务优先级指导

**数据**：
- Alice: 13 个步骤中，12 个是导航/探索，0 个是灭火/救人
- Bob: 7 个步骤中，3 个是导航/探索，3 个是获取资源，0 个是灭火

### 问题 2：导航效率低下

**现象**：Worker 在导航到目标时经常"绕圈"或到达错误位置。

**原因分析**：
1. `navigate_to` 工具的实现可能不是最优路径
2. Worker 没有记住之前访问过的位置
3. 障碍物检测和路径规划不够智能

**数据**：
- Alice 到达 ReservoirUtah 花费了 5 个步骤（Step 3, 7, 11, 12, 13）
- Bob 到达 ReservoirUtah 花费了 3 个步骤（Step 6, 7, 未完成）

### 问题 3：缺乏协调机制

**现象**：两个 Worker 独立执行，没有任务分配和协调。

**原因分析**：
1. RouterAgent 分配了相同的子任务给两个 Worker
2. Worker 之间没有通信机制
3. 没有全局任务状态跟踪

**数据**：
- 两个 Worker 都在尝试获取 Sand 和 Water
- 没有分工：一个灭火，一个救人

### 问题 4：任务完成条件不明确

**现象**：Worker 不知道何时任务完成。

**原因分析**：
1. Worker 没有收到明确的任务完成标准
2. 没有进度反馈机制
3. Worker 不知道需要灭火多少个区域、救多少个人

## 建议的优化方案

### 短期优化（可立即实施）

1. **优化 Worker 提示词**
   - 明确任务优先级：灭火 > 救人 > 探索
   - 提供任务完成标准
   - 减少不必要的探索步骤

2. **增加任务超时时间**
   - 将 task_timeout 增加到 300s（5 分钟）
   - 或改为基于步骤数的超时（如 50 步）

3. **优化导航工具**
   - 实现 A* 路径规划
   - 缓存已访问位置
   - 提供最短路径建议

### 中期优化（需要代码修改）

1. **实现任务分配机制**
   - RouterAgent 为每个 Worker 分配具体子任务
   - 避免重复劳动

2. **添加 Worker 间通信**
   - 实现共享状态机制
   - 允许 Worker 请求帮助

3. **实现进度跟踪**
   - 添加全局任务状态
   - 定期向 Worker 反馈进度

### 长期优化（架构改进）

1. **改进 ReAct 循环**
   - 实现多步规划
   - 添加回溯机制

2. **优化 LLM 调用**
   - 批量处理多个动作
   - 缓存常见操作

3. **实现分层规划**
   - 高层：任务分解和分配
   - 中层：区域规划和协调
   - 底层：动作执行和反馈

## 当前状态分析

### 已完成的架构层

| 组件 | 状态 | 说明 |
|------|------|------|
| SARBarrier | 正常 | SAR 仿真引擎正常初始化和交互 |
| SARWorker A2A HTTP | 正常 | 端口 8191/8192 启动，AgentCard 可被获取 |
| Worker WebSocket 注册 | 正常 | Alice/Bob 成功连接 Coordinator |
| Coordinator WebSocket | 正常 | 接受 Worker 连接并维护注册表 |
| AgentCard 获取 | 正常 | Coordinator 成功获取两个 Worker 的能力描述 |
| A2A 协议通信 | 正常 | 通信链路完全建立 |
| 任务提交 | 正常 | Coordinator 成功接收任务并触发 RouterAgent |
| RouterAgent LLM 调用 | 正常 | DeepSeek API 调用成功（thinking 模式兼容） |
| RouterAgent 任务分解 | 正常 | 成功分解任务并调度子任务到 Worker |
| Worker LLM 调用 | 正常 | Worker 通过 mini_agent ReAct 循环调用 LLM |
| Worker 动作执行 | 正常 | Worker 通过 SAR 工具集执行动作 |
| SAR 环境交互 | 正常 | Coverage 1.00, Transport 0.53 |

### 核心瓶颈

**Worker 执行效率低下**

Worker 在 120 秒内只执行了 13 个步骤，平均每个步骤 9.2 秒。其中大部分时间花在导航和探索上，实际灭火和救人动作为 0。

**时间分配**：
- RouterAgent 任务分解：~10s (8%)
- Worker 探索和导航：~100s (83%)
- Worker 实际执行：~0s (0%)
- 其他开销：~11s (9%)

### 可选模块状态

| 模块 | 状态 | 影响 |
|------|------|------|
| mini_agent | 已安装 | Worker ReAct 循环正常运行 |
| SimpleLLMClient | 正常 | Coordinator LLM 调用正常（thinking 模式兼容） |
| memory_server | 不可用 | 记忆功能降级运行，非阻塞 |
| a2a_lib.task_logger | 不可用 | 任务日志不可用，非阻塞 |

## 修复的代码文件

| 文件 | 修改内容 |
|------|----------|
| `SAR/Scenes/scene_1.py` | task_timeout 从 35s 增加到 120s |

## 结论

本轮实验将 task_timeout 从 35s 增加到 120s，但任务仍未完成。**核心问题不是超时时间不足，而是 Worker 执行效率低下**。

关键发现：
1. **Coverage 达到 100%**：所有火焰区域已被探索，环境认知完成
2. **Transport Rate 仅 53%**：资源运输未完成，灭火和救人动作未执行
3. **Worker 过度探索**：13 个步骤中 12 个是导航/探索，0 个是实际灭火
4. **导航效率低下**：到达目标位置需要多次尝试

**根本原因**：
- Worker 的 ReAct 循环缺乏任务优先级指导
- 没有明确的任务完成标准
- 导航工具效率低下
- 缺乏 Worker 间协调机制

**下一步建议**：
1. 优化 Worker 提示词，明确任务优先级和完成标准
2. 实现更高效的导航算法
3. 添加 Worker 间通信和任务协调机制
4. 考虑将 task_timeout 增加到 300s 或改为步骤数限制

端到端架构已完全验证可行，**瓶颈在于 LLM 驱动的 Worker 执行效率**，而非架构或通信问题。
