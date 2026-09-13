# 长期记忆实施方案补充：反思源快照原子性

> **性质：** 本文件是 [`长期记忆_反思机制+动态Agentcard接入_实施方案.md`](长期记忆_反思机制+动态Agentcard接入_实施方案.md) 的强制补充，优先于其 §3.4 与 Phase 4 中关于“committed snapshot”（滚动与 terminal 通用）的泛化表述。
>
> **绑定主方案 SHA-256：** `169f9f7de66b3f5b761140b7a6e1be8d4c9b7a6aa50dae68aaed3f9d171fa1ab`
> **状态：** 提案，未实施；随主方案一起进入 G0/G2 审查。

## 1. 必须修正的原子性约束

反思 source window **不得**按以下非原子顺序组装：

```text
revision_of(scope_id)
  → temporal_events(scope_id)
  → 交给反思器
```

因为两次 read 之间可以有已认证 callback 写入；这样得到的 `source_memory_revision` 和 event list 可能从未同时成立，导致 idempotency key、证据链与模型输入不一致。

## 2. 冻结替代契约

Phase 4 必须在 `src/a2a/coordinator/memory/store.py` 新增只读 API（名称可保持 `scope_event_snapshot`，但返回形状不可弱化）：

```text
ScopeEventSnapshotV1(
  scope_id: str,
  memory_revision: int,
  events: tuple[canonical temporal event, ...],  # sequence 升序
  snapshot_digest: sha256(canonical payload)
)
```

必需行为：

1. 在同一 `MemoryStore` 的锁与同一 SQLite read transaction/一致性临界区内读取 scope、revision 和 ordered events；不能调用已有两个独立 read helper 后再在 Python 层拼接。
2. snapshot 不修改 scope、revision、event、outbox、projection、security audit；不能使用 `BEGIN IMMEDIATE`，更不能在 read 临界区调用网络、LLM、export 或 file I/O；读事务必须在 `finally` 中 COMMIT/ROLLBACK，异常路径不得残留开放事务（`isolation_level=None` 下残留会毒化后续 callback 写）。
3. unknown scope 返回 typed `unknown_scope`；关闭 scope 仍允许只读 snapshot（V1 不负责 close scope）；读取异常返回 typed failure，长期 store 零内容写入。
4. `ReflectionSourceCollector` 只能接受该 snapshot，不得自己调用 `revision_of()` 或 `temporal_events()` 重建同一 source window。
5. `reflection_run` 幂等键至少绑定 `project_id + source_scope_id + snapshot.memory_revision + snapshot.snapshot_digest + policy_version`；同一 snapshot 重试零重复持久化。
6. 滚动与终结时均沿用现有 `materialize_compatibility_artifacts()` 的 scope 解析（`src/a2a/coordinator/server.py:551-642`；滚动时 scope 为 active runtime），随后调用 snapshot API；**不**在本次待办里引入生产 `close_scope()` 调用。

## 3. Phase 4 文件和测试增补

**Modify：**
- `src/a2a/coordinator/memory/store.py`
- `src/a2a/coordinator/memory/reflection.py`
- `sar_orch/long_term_reflection.py`
- `tests/test_long_term_reflection.py`
- `tests/test_long_term_memory_store.py`

**必须新增的 RED→GREEN 断言：**

1. 在 reader 取得 snapshot 前后交错一个 callback/projection writer；反思器收到的 revision、events、digest 必须来自一个真实的一致快照，而非混合状态。
2. 同一个 snapshot 反思 hook（滚动或终结）重试两次，只产生一条 `reflection_run` 和一次 candidate/support persistence。
3. snapshot 读取期间人为抛错，长期 store 的 `reflection_run` 可记录失败诊断但 `long_term_memory`/support 均为零。
4. close scope 后 snapshot 仍可读；unknown scope fail closed；两者均不改变 run-local `memory_revision`。
5. 使用 two-store/同 DB 并发 scenario 验证 long-term store lock retry 时，重复 writer 不会把同一个 snapshot 反思两次。

## 4. Gate 影响

- G2（长期写入）验收增加：`source_scope_id`、`source_memory_revision`、`snapshot_digest` 与 `reflection_run`/support evidence 必须可复算一致。
- 任何 snapshot 混合、revision/digest 不一致、late callback 未被明确定义归属，均为 Blocker；保持 `long_term_mode=off|shadow`，不得进入 `read`。
