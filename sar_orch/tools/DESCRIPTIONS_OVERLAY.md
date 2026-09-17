# SAR tool descriptions 运行时覆盖层（reef 第二 config target `tools` 预留）

> 状态：**本阶段只落地 LLaMAR 侧机制**（文件 + loader + 接线 + 单测）。
> reef 侧接入（descriptor 第二 config target、tree entry、runner 安装动作）**尚未实施**——
> `reef_sar_adapter/` 在另一个 worktree（reef-a），按 workflow §B4 属"全量阶段"，
> smoke tree 不含 tool descriptions。本文记录接入方式，供 A 侧与 R2 使用。
>
> 出处：`20260917-reef-sar-evolution.md` §5.2 末行（tool descriptions 目标）、
> `20260917-reef-sar-implementation-workflow.md` §B1-B4。

## 1. 文件与键规则

`sar_orch/tools/descriptions.json`，形态：

```json
{
  "<key>": {
    "description": "<LLM 看到的工具描述>",
    "parameters": { "<JSON Schema，含每个参数自己的 description>" }
  }
}
```

- **键** = 工具 `name`（`sar_orch/tools/{worker,coordinator}/*.py` 里的类属性）；
  同一个 name 在 worker 与 coordinator 两个 scope 都存在时（当前只有
  `finish_task`），键升级为 `<scope>/<name>`：`worker/finish_task`、
  `coordinator/finish_task`（两者的 description/parameters 本来就不同，裸名无法表达）。
- **查找顺序**：`<scope>/<name>` → 裸 `<name>`。裸名键会同时命中两个 scope 的同名类
  （有意如此：写裸名 = 显式对所有同名工具生效）。
- **条目可以只带一个字段**：只写 `description` → `parameters` 保留代码内联值，反之亦然。
- 快照规模（本卡）：**22 个工具类**（worker 14 + coordinator 8），键数同为 22
  （21 个裸名 + 2 个 scope 限定键，`finish_task` 占两个键）。

## 2. 运行时契约（fail-soft，与 prompt 外置化相反）

| 情形 | 行为 |
|---|---|
| 文件存在、条目齐全 | 覆盖对应类属性；`to_openai_schema()` 立即反映（LLM 看到新描述） |
| 文件缺失 / 不可读 / 非 utf-8 / 空 | 静默回退：覆盖层视为空，全部用代码内联值（默认行为逐字节不变） |
| JSON 语法错误 / 根不是对象 | 同上回退（记 warning 日志，不抛异常） |
| 条目不是对象 | 丢弃该条目（记 warning），其余条目照常生效 |
| 缺键（工具不在文件里） | 该工具保留内联值 |
| 字段非法（描述空白 / parameters 不是对象） | 逐字段拒绝，该字段保留内联值，另一字段照常覆盖 |

理由：工具描述是演化目标而非正确性依赖，任何形态的问题都不该让一次 run 起不来；
prompt 外置化（`src/a2a/utils/prompt_loader.py`）是 fail-closed，两者口径不同是刻意的。

**接线点**（导入即生效，任何导入路径都会先执行包 `__init__`）：

- `sar_orch/tools/worker/__init__.py` → `apply_description_overlays([*SAR_WORKER_TOOLS, QuerySharedMemoryTool])`
- `sar_orch/tools/coordinator/__init__.py` → 覆盖 4 个基础工具 + 4 个诊断只读工具
  （`query_projection` / `query_temporal_flow` / `query_supervision` /
  `query_control_journal`，由 `sar_orch/diagnosis_loop.py` 直接实例化）

**不在覆盖范围**：`src/a2a` 的内核工具（`send_message` / `ask_coordinator` /
`read_mailbox` / `a2a_send_mail` 等）——它们不在 `sar_orch/tools/` 下，且部分以
property 形态提供 `description`（不是类属性），本次既不在文件范围也不在变异面。

## 3. 离线工具与数量不变量

```bash
python sar_orch/tools/export_descriptions.py --check    # 扫描工具类、打印键映射与总数
python sar_orch/tools/export_descriptions.py --write    # 由源码内联值生成 descriptions.json
python sar_orch/tools/export_descriptions.py --verify   # 核对文件 == 源码内联值（逐字段）
```

- 取值走 AST 读源码，**不 import 工具类**，因此与运行时覆盖层状态无关。
- 新增/删除工具类后必须重跑 `--write`（`tests/test_descriptions_overlay.py`
  的 `EXPECTED_TOOL_CLASS_COUNT` 会同时报警）。
- `--verify` 是**外置化时点**的等值核对；变异 promote/pull 之后文件与内联值不同属正常，
  **不要**把 `--verify` 接成常驻 CI 硬门。
- 覆盖层生效的判据（单测覆盖）：仓库文件覆盖每个工具类；子进程探针证明
  "import 工具包 → 类属性变为覆盖值"。

## 4. 接入 reef（未来工作，本卡只预留）

### 4.1 descriptor 增量

```yaml
files:
  config:
    primary:
      path: "overlay/sar_config.json"
      defaults: {}
    # reef §5.2 tool descriptions：第二 config target（kind 仍是 config，按 target 名区分）
    tools:
      path: "overlay/tools/descriptions.json"
      defaults: {}
```

reef 的渲染行为（`reef/harness/tree/render.py:122-186`）：所有 target 的文档都会落盘，
`config` 节点按 `{target, data}` **深合并**进对应 target，最后 `json.dumps(sort_keys=True)`。
因此 tree 里的 entry 形态为：

```yaml
- id: tool-descriptions            # mutation 按 id 锚定
  name: config                     # kind
  config: {target: tools, data: {...}}   # data 即 descriptions.json 的内容子集
```

### 4.2 A 侧 runner 需要补的动作（一处）

渲染产物在 `overlay/tools/descriptions.json`，runner 需把它安装到物化仓库的
`sar_orch/tools/descriptions.json`（与 `overlay/prompts/coordinator/<name>.md` →
`sar_orch/prompts/coordinator/<name>.md` 同一安装模式）。LLaMAR 侧无需再改：
loader 已按 repo 根解析该路径，文件缺失即回退内联值。

### 4.3 变异面边界与建议

- kind 限 `config`；可变异对象 = 22 个 SAR 工具类的 `description` / `parameters`；
  不改代码、不改键集合本身（键集合固定，值可变）。
- **建议 admission/提案 prompt 约束键白名单**：loader 对未知键是静默忽略的（fail-soft），
  拼错键不会报错也不会生效——提案侧应拿 `--check` 的键清单做校验。
- 覆盖评估的口径与 prompt 变异一致：工具描述只影响 LLM 决策，指标看 gate 三要素
  （SR / token / balance）。
- 变更生效会在 `eval`/gate 中体现；人工 promote 后由 pull 脚本同步该文件回 LLaMAR 树，
  再走人审 + commit。

### 4.4 未决点（交给 A 侧 / R2）

- runner 安装动作与 `cleanup_whitelist`（`sar/**`）的关系：安装发生在物化后的 repo 内，
  属 overlay 覆写阶段，不应产生 residue——实现时按 A2 runner 现有模式核对。
- 是否把 `tools` target 纳入全量阶段 tree：按 §B4，全量阶段再入；smoke tree 保持 4 entries。
