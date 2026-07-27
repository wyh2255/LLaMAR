---
日期: 2026-07-24
文档类型: 文档影响策略
文档概述: 判断何时更新长期文档及其受控写入范围。
---

# Documentation Impact Policy

Every run records `doc-impact` as `update` or `none`. Update documentation for
module boundaries, public contracts, state ownership, operational recovery, or
an architectural decision. Record local helpers and phase logs as `none`.

Canonical documents store current truth, decisions, and operating instructions;
they never store test pass counts or phase diaries. A Doc Writer may change only
manifest targets. Do not update `docs/system_docs`, project notes, reports, or
any dirty-baseline file as a side effect of this workflow.
