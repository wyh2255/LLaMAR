# P3 Eval Workflow：节点角色地图

> **用途**：帮助理解 P3 LangGraph 控制面的职责分工。
>
> **当前状态**：P3 正在修复审查发现；本文件描述的是目标职责与边界，不代表当前工作树已通过验收。具体修复门见 `.hermes/plans/2026-08-06-eval-judge-langgraph-progress.md`。

## 30 秒理解

P3 把“评测”拆成一条确定性流水线：

```text
准入与冻结 → 证据与规则评分 → 是否需要 LLM
→ Job 调度与执行 → 合并 → 报告 → 验证与封存
```

LLM 不拥有最终裁决权：它只能提交 `ScoreDraft`；确定性 validator、merge 和 finalizer 才能写入正式结果。

## 工作流总览

```text
START
  → admit_or_resume
  → freeze_input_manifest
  → materialize_evidence
  → run_deterministic_graders
  → route_judge_mode
       ├─ not_requested → render_reports
       └─ requested
            → build_score_jobs
            → run_score_judge (fan-out: 每个 job 一条分支)
            → join_score_jobs
            → deterministic_score_merge
            → render_reports
  → verify_and_finalize
  → END
```

`checkpoint_probe` 是 P3 测试用的受控中断点，用来验证 checkpoint/recovery，不承担业务评测职责。

## 各节点的作用

| 节点 | 角色 | 它负责什么 | 它不负责什么 |
|---|---|---|---|
| `admit_or_resume` | 门卫 / 恢复调度员 | 判断新 run、短路完成 run、或从缺失阶段恢复 | 不评分、不调用模型 |
| `freeze_input_manifest` | 封条员 | 冻结 source、policy、rubric、role 等输入条件 | 不允许后续 live 配置覆盖冻结事实 |
| `materialize_evidence` | 证据打包员 | 将允许评测使用的 episode 信息物化为 evidence artifact | 不让模型直接任意读取原始结果目录 |
| `run_deterministic_graders` | 机械裁判 | 运行可复算的规则 grader，产出 hard facts / violations | 不请求 LLM，也不被 LLM 分数推翻 |
| `route_judge_mode` | 分流开关 | 依据 frozen policy 决定走 no-LLM 还是 Judge 路径 | 不创建 job、不做合并 |
| `build_score_jobs` | 派工员 | 把 sample × rubric × role 变成独立 `ScoreJob` | 不调用模型、不直接给分 |
| `run_score_judge` | 单工位执行员 | 对一个 job 做 claim、run、retry/timeout/cancel、Draft 校验和结果持久化 | 不决定全局成功/失败，不修改其他 job |
| `join_score_jobs` | 班组长 | 汇总所有 job 的完成情况，得出 requested / partial / budget 状态 | 不重新评分，不写最终 report |
| `deterministic_score_merge` | 会计 | 按冻结 policy 合并各 rubric/job 的正式结果 | 不再调用模型，不绕过 deterministic veto |
| `render_reports` | 出单员 | 将结构化状态和 artifact refs 投影为报告 | 不重新改变评分或终态 |
| `verify_and_finalize` | 档案管理员 | 复核 artifact 完整性，写入唯一权威的 final ledger | 不重新运行 job 或篡改冻结输入 |

## 三条最重要的边界

### 1. 模型只能提出意见

```text
LLM / runner → ScoreDraft
validator    → ScoreResult
merge        → merged score
finalizer    → terminal ledger
```

这让模型输出即使异常，也只能成为 typed failure / partial，而不能直接把一次评测宣布为成功。

### 2. State 只传引用，不搬运大内容

Graph State 应保存 ID、status 和 `ArtifactRef`；完整证据、模型输出、报告内容放在 attempt artifact 中。

好处：checkpoint 更小、resume 可验证、不同 job/role 不会天然共享全部上下文。

### 3. P3 是控制骨架，不是完整 Agent 产品

P3 的重点是确定性 spine、fake-runner fan-out、retry/cancel、artifact 和恢复边界。

以下属于后续阶段：

- **P4**：真实 DeepAgent role、工具白名单、job-scoped evidence reader、Draft 角色隔离；
- **P5**：默认 CLI、JSON/Markdown report、aggregate/gate/benchmark 兼容；
- **P6**：离线矩阵和单独授权的真实 LLM calibration。

## 阅读顺序

想读实现时，建议按这个顺序：

1. `admit_or_resume`：先理解一次 run 如何开始或恢复；
2. `route_judge_mode`：理解 no-LLM 为什么不创建 job；
3. `build_score_jobs` → `run_score_judge`：理解并发工作的边界；
4. `join_score_jobs` → `deterministic_score_merge`：理解局部结果如何成为全局结论；
5. `verify_and_finalize`：理解为什么 final ledger 才是最终事实。
