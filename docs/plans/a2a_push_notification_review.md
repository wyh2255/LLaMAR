---
日期: 2026-07-03
文档类型: 评审记录
文档概述: A2A PushNotification 实现方案评审问题记录
---

# A2A PushNotification 评审问题记录

## 文档层面的问题

| # | 问题 | 位置 | 状态 |
|---|------|------|------|
| 1 | `DefaultRequestHandlerV2` 应为 `DefaultRequestHandler`（代码库无 V2） | 数据流注释第 400 行 | 已修复 |
| 2 | `InMemoryPushNotificationConfigStore()` 有可选参数 `owner_resolver`，文档显示无参调用虽可工作但不够精确 | §6 | 已更新 |

## 实现层面的问题

### 已修复

| # | 问题 | 严重性 | 文件 | 修复内容 |
|---|------|--------|------|----------|
| 3 | `send_task_async` 返回空字符串时 Future 永不 resolve，`CollectResultsTool` 永久挂起 | **重要** | `dispatch_task.py` | 加返回值检查，空返回时抛 `RuntimeError` |
| 4 | dispatch 失败时用 `set_result(f"[dispatch failed: {e}]")`，下游 `success: True` 掩盖错误 | **重要** | `dispatch_task.py` | 改为 `set_exception(e)`，`CollectResultsTool` 自然返回 `success: False` |
| 5 | callback URL 传 `"0.0.0.0"`（绑定地址）给 Worker，不可路由 | **重要** | `server.py` | 改为 `"localhost"` |

### 设计中已标注（待解决）

| # | 问题 | 严重性 | 说明 |
|---|------|--------|------|
| 6 | Worker 中间状态（非 terminal 的 `status_update`、`artifact_update`）在 callback handler 被丢弃 | **一般** | 当前只在 terminal 时 resolve，中间状态虽然 worker 发了但未处理。如需进度展示，需新增 `QueryWorkerStateTool` + 全量状态缓存 |
| 7 | callback 端点无身份认证 | **重要** | 协议要求 `PushNotificationConfig.authentication` 的 Bearer 令牌等校验。目前完全开放 |
| 8 | callback 端点无来源验证 | **重要** | 知道 URL 即可伪造通知 |
| 9 | 无重试机制 | **一般** | 协议要求指数退避重试。callback 不可达时通知丢失 |
| 10 | `send_task_async` 中 `agent_info` 未做 None 检查 | **一般** | 匹配已有 `push_task` 的模式，可以统一改进 |
| 11 | Worker `httpx.AsyncClient()` 未在关闭时清理 | **一般** | 触发 `Unclosed transport` 警告 |
| 12 | `_dispatch_with_error_handling` 后台任务未追踪，无法在关闭时取消 | **一般** | 匹配旧代码的生命周期管理方式 |

### 范围外

| # | 问题 | 说明 |
|---|------|------|
| 13 | `QueryWorkersTool` 随 Task 7+8 被额外引入 `builtin_tools` | 用户确认保留 |

## 协议合规项缺失

对照 A2A PushNotification 协议标准：

| 要求 | 状态 | 备注 |
|------|------|------|
| 负载解析 `StreamResponse`（task/message/statusUpdate/artifactUpdate） | ✅ | 已实现 |
| HTTP 2xx 确认 | ✅ | 返回 `{"status": "ok"}` |
| 幂等处理 | ✅ | `pop` + `done()` 守卫 |
| 验证 task_id | ❌ | 未校验分发过的 task_id 集合 |
| 身份认证（Bearer/基本认证） | ❌ | callback 端点无保护 |
| 来源验证 | ❌ | 无签名/IP 校验 |
| 重试机制（指数退避） | ❌ | 单次发送，失败即丢 |
| 合理超时（10-30s） | ✅ | 默认 5s，可以显式设置 |
