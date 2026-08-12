# 长期记忆实施方案补充：长期库存储与 Schema 演进（run-local）

> **性质：** 本文件是 [`长期记忆_反思机制+动态Agentcard接入_实施方案.md`](长期记忆_反思机制+动态Agentcard接入_实施方案.md) 的强制补充，优先约束其 Phase 1/4 的 run-local long-term store。
>
> **范围说明（2026-08-11 用户拍板）：** 长期记忆已收缩为**单 run 语义**——反思、写入与注入都在当前 run 内；跨 run 复用走 skill/文档沉淀。本补充因此聚焦 run-local 长期库的存储拓扑与 schema 演进契约；"跨 run 聚合/稳定 root"动机不再成立，迁移 runner 契约保留用于本库自身的版本演进与未来合并路径。
>
> **绑定主方案 SHA-256：** `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`
> **状态：** 提案，未实施；随主方案一起进入 G0/G2 审查。

## 1. 独立核验的事实

1. 当前 run-local `MemoryConfig.db_path` 固定为 `<memory_root>/memory/memory.sqlite3`（`src/a2a/coordinator/memory/contracts.py:510-541`），而 SAR experiment 的 `coord_dir` 来自本次 `exp_dir/coordinator`（`sar_orch/experiment.py:312-335`），SARCoordinator 把这个路径传给 `MemoryStore`（`sar_orch/coordinator.py:357-385`）。
2. 真实 `logs/h2_shadow_audit_run2/coordinator/memory/memory.sqlite3` 只读实查：一条 scope，`experiment_id=sar-scene1-agents2-seed42-ccece10f`，`closed_at=NULL`，`PRAGMA user_version=3`，16 张表（15 张业务表 + `sqlite_sequence` 系统表；`_SCHEMA` 实为 15 张业务表，2026-08-12 复核）。因此它是本 run 私有 canonical source 的实证，不是 project-level 跨 run 库。
3. 当前 `MemoryStore` 打开时直接 `PRAGMA user_version=3` 并执行 `_SCHEMA`（`src/a2a/coordinator/memory/store.py:264-290`）；没有按版本分发、未知版本拒绝或逐迁移 runner。
4. `MemoryRecovery.close_old_scope()` 只封装 `ingestor.close_scope()`（`src/a2a/coordinator/memory/recovery.py:163-169`）；当前 run terminal 路径调用 compatibility materialization/evaluator（`sar_orch/experiment.py:173-255,735-760`），不能据此假设 production 已关闭 scope。
5. run2 根目录没有 `export_manifest.json`。这只能说明该历史 artifact 未能证明 terminal materialization 是否执行，**不能**反推当前源码没有该调用；实现与验收以当前源码/新鲜 run 为准。

## 2. 强制存储拓扑

```text
run-local source DB
  <memory_root>/memory/memory.sqlite3
  - Temporal / Spatial / Embodied 的唯一事实源
  - 每次 run 新建；不承载长期记忆表

run-local long-term DB
  <memory_root>/long_term/long_term.sqlite3
  - 只保存 reflection run、validated candidate/published memory、support refs、audit
  - 每一行携带 project_id（默认 "llamar"，与 MemoryConfig.project_id 同源）+ scope_id；所有查询必带两者
  - 单 run 语义：不提供跨 run 导入/扫描/聚合 API；跨 run 复用走 skill/文档沉淀
```

不得把长期表加到 run-local `MemoryStore._SCHEMA`，不得扩展 `projection_field.domain`，不得通过符号链接或当前工作目录隐式猜测路径；long-term root 一律由 `MemoryConfig.memory_root` 派生（`<memory_root>/long_term/long_term.sqlite3`）。

## 3. Schema migration 必须从 V1 就存在

`LongTermMemoryStore` 是独立于 canonical 的 durable state（run-local 文件，未来存在合并/复用演进可能）；即便首版只有 initial schema，也必须自带可演进、fail-closed 的 migration runner，不能复制当前 run-store 的“写死 user_version + CREATE IF NOT EXISTS”模式。单 run 新建库时仅运行 `001_initial`；升级路径预留给本库自身演进与未来跨 run 合并场景。

### 契约

1. 用单一、明确的 schema version authority（推荐 `schema_migrations(version, applied_at, migration_sha256)`；可同步更新 `PRAGMA user_version`，但不得只写死常量）。
2. migration 文件/函数按数值版本顺序执行；新 DB 先建立 version table，再运行 `001_initial`。
3. 每个 migration 在显式 `BEGIN IMMEDIATE → statement-by-statement → version row → COMMIT` 中完成；失败必须 `ROLLBACK`，不能使用 `executescript()` 充当事务边界。
4. 已应用版本必须是真 no-op；未知未来版本、缺失版本、migration digest 不匹配、半完成 migration 均 fail closed，禁止打开 store 后继续读写。
5. long-term store 的 schema/version 错误不得修改 run-local source DB；滚动/terminal reflection 写 typed failure status（落点 `run_metrics`/terminal memory summary；store 拒绝打开时无法写 `reflection_run`，failure 只落 run_metrics），`long_term_memory`/support 零部分写。
6. `LongTermMemoryStore` 的跨进程 busy retry 只包短事务；LLM/network/export 永远在 migration/transaction 外。

### Phase 1 文件与测试增补

**Modify：**
- `src/a2a/coordinator/memory/long_term.py`
- `src/a2a/coordinator/memory/contracts.py`
- `tests/test_long_term_memory_store.py`

**新增 RED→GREEN coverage：**

- fresh root：运行 `001_initial`，版本/表/约束精确成立；二次打开无新 migration；
- reopen：两个独立 store instance 可读取同一 scope 数据，scope filter 仍生效；
- simulated old version：仅应用旧 migration 的临时 DB 升级后，旧数据/支持 refs 保留，新增默认/状态语义正确；
- negative：未知 future version、版本记录与 schema 不一致、migration 中途抛错，均拒绝并证明 transaction 回滚；
- isolation：失败/升级期间 run-local `memory.sqlite3` 的文件清单、行数、mtime 不变；
- concurrency：两个 long-term store 写入不同/相同 reflection id，重试后恰有一条 canonical run/support，绝无 duplicate/partial row。

## 4. Gate 增补

G2（`off → shadow`）除了主方案的 source/evidence/function-call 条件外，必须验证：

- long-term DB schema version、applied migration list 和 migration digest 可导出审计；
- 新 root/重复 open/旧版 upgrade/未知 future version/中途失败五类测试全绿；
- 真实 run 的长期库随 run-local 目录新建，不接触用户已有库；
- 任何 schema migration 或 lock 问题保持 `long_term_mode=off|shadow`，禁止进入 `read`。
