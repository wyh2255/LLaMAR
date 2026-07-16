---
日期: 2026-07-15
文档类型: 项目总结
文档概述: LLaMAR A2A-SAR 项目全局进度摘要 — 架构、里程碑、当前状态、待办
---

# 项目总结

## 项目概述

基于 A2A (Agent-to-Agent) 协议的多智能体搜救 (SAR) 编排系统。Coordinator 调度多个 Worker 机器人在 SAR 网格环境中协作灭火、救人。

- **代码**: 框架层 `src/a2a/` + `src/Agent/`，编排层 `sar_orch/`，环境层 `SAR/`
- **模型**: `deepseek-v4-flash` (DeepSeek API)
- **模式**: `semantic` (部分可观测) / `oracle` (上帝视角)
- **评测**: 5 场景 × 4 agent 数 × 5 种子 = 100 轮

## 架构

```
experiment.py
  ├─ SARBarrier → SAREnv (grid world, threading同步)
  ├─ SARCoordinator → FastAPI:8080 + A2A:8081
  │   └─ RouterAgent (LLM) + query_semantic_map/cancel_task/dispatch_task 等
  └─ SARWorker × N → A2A Server (8191+)
      └─ WorkerAgent (LLM) + 12 SAR 工具 (含邮箱工具)
```

观测管线: `Worker report_observation → A2A push [DATA] → Coordinator ingest_observation → SemanticMapStore`

## 里程碑

| 时间 | 里程碑 | 状态 |
|------|--------|------|
| 06-27 | 框架迁移 + SAR 编排层 + 地图可视化 + 日志链路 + 全流程验证 | ✅ |
| 06-29 | Benchmark Phase 3 批量运行框架 (子进程/超时/重试) | ✅ |
| 07-01 | 结构性修复: threading同步/TimeoutAgents/step可见性/prompt优化 | ✅ |
| 07-02 | Agent 上下文管理 (ContextManager 三层记忆策略) | ⚠️ 5 个待修复 |
| 07-05 | 语义地图 (SemanticMapStore) + semantic/oracle 双模式 | ✅ |
| 07-05 | CancelTaskTool — Coordinator 抢占 Worker 任务 | ✅ |
| 07-05 | SAR 报告渲染器重构为独立 skill (render-sar-report) | ✅ |
| 07-09 | max_steps 固定 50 步 + step_budget 实时更新 | ✅ |
| **07-15** | **Worker 邮箱 + 小队管理 + 对等通信 (Phase 1-5)** | **✅ 最新** |

### Benchmark 结果 (06-30, oracle 模式)

| 指标 | 值 |
|------|----|
| 全局成功率 | 78% (78/100) |
| 最优 agent 数 | agents=3 (84%) |
| 失败模式 | 协同搬运死锁 (transport_rate 不足) |
| 总 Token | ~74.6M |

## 当前状态 (2026-07-15)

### 已完成的子系统

- [x] **A2A 框架**: Coordinator/Worker 服务器、A2A 协议、Agent 执行循环、TaskWatchdog
- [x] **SAR 编排**: Barrier 同步、step 轮询、CSV 日志 (5 文件)、token 统计
- [x] **地图可视化**: HTML Table + SSE 实时刷新
- [x] **语义地图**: 观测聚合/冲突检测/过期标记/JSONL 持久化
- [x] **任务抢占**: CancelTaskTool (A2A TASK_CANCEL)
- [x] **认证邮箱**: HMAC-SHA256 信封、Worker 邮箱持久化、Coordinator 小队注册表、Worker 对等直连

### 剩余未修复问题

| 问题 | 严重度 | 条目 |
|------|--------|------|
| shutdown SIGABRT 崩溃 | 中 | bugs.md: 07-02 |
| `agent_adapter.py` ruff E402 (10 处) | 低 | bugs.md: 07-02 |
| `ContextManager.assemble()` 隐式假设 | 低 | bugs.md: 07-02 |
| `finish_task` 在 Worker prompt 不可见 | 中 | bugs.md: 07-05 |
| CancelTask 未认证 (A2A SDK 限制) | 中 | decisions.md: ADR-017 |
| 小队密钥明文落盘 (0600) | 低 | decisions.md: ADR-017 |
| benchmark 不支持 `--enable-peer-mail` | 低 | key_facts.md |

### 近期待办

1. 修复 shutdown SIGABRT (EventQueue 清理顺序)
2. Worker prompt 补全 `finish_task` 工具说明
3. 跑 semantic 模式 benchmark 对比实验
4. 修复 ruff E402 违规
5. benchmark 添加邮箱支持

## 文件索引

| 文件 | 内容 |
|------|------|
| `issues.md` | 工作日志 (仅 July+，June 见 `docs/archives/issues_202606.md`) |
| `bugs.md` | Bug 记录 (仅 July+，June 见 `docs/archives/bugs_202606.md`) |
| `decisions.md` | ADR (ADR-011~017，June 见 `docs/archives/decisions_202606.md`) |
| `key_facts.md` | 配置/端口/CLI/架构速查 |
| `docs/archives/` | 2026年6月及之前所有历史记忆 |
