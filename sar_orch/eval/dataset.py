import ast
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


_ACTION_RE = re.compile(r"^(\w+)\(([^)]*)\)$")


# 工具层动作名 → 环境层规范名。
#
# 为什么需要：`sar_orch/worker.py:_build_action()` 的 name_map 把工具名渲染成
# **工具层**名字写进 agent_interactions.csv 的 Action 列
# （`carry_person`→`CarryPerson`、`drop_off_person`→`DropOffPerson`），
# 而工具 `execute()` 提交给 barrier / 落进 trajectory.csv 的是**环境层**名字
# （`SAR/core.py` 的动作词表：`Carry` / `DropOff`，见 core.py:1889
# `CARRY_DROP_ACTIONS=['Carry','DropOff']`）。同一个动作两处两名。
#
# 下游 grader 的白名单（`dataset.ENV_ACTION_NAMES`）用的是环境层名字，
# 故未收录别名的动作会在 `action_name not in ENV_ACTION_NAMES → continue`
# 处被**静默丢弃**。
# 实测 115 个 run：Action 列共 90 行 `DropOffPerson`、0 行 `DropOff`；
# 对应 trajectory.csv 里 91 行 `DropOff`、0 行 `DropOffPerson`
# —— 即救援收尾这一步的约束检查此前完全没跑到过。
_ACTION_ALIASES = {
    "CarryPerson": "Carry",
    "DropOffPerson": "DropOff",
}


# 各 worker 工具的**声明**参数顺序（取自 sar_orch/tools/worker/*.py 的
# `parameters.properties` / `execute()` 签名）。只列多参数工具 —— 单参数
# 工具不存在顺序歧义。
#
# 为什么需要这张表：`sar_orch/worker.py:348` 的 `_build_action()` 用
# `", ".join(str(v) for v in args.values())` 拼 Action 字符串，即渲染顺序
# 取决于 **LLM 输出 JSON 的键序**，而非工具声明顺序。LLM 写
# `{"supply_type": "Water", "fire_id": "TownFire_Region_2"}` 时（键名完全
# 正确、运行时按 kwargs 正确执行），日志里却记成
# `UseSupply(Water, TownFire_Region_2)` —— 参数看起来是反的。
#
# 实测 20 个基线 run：11 行发生此错位（use_supply 8 / drop_off_person 3）。
# 后果是所有按下标取参的 grader 都会读错位置 —— constraint.py 的
# `_check_empty_supply` 因此把 7 起成功的 UseSupply 判成"用了不存在的物资"
# （拿火名去库存字典里查，必然得 0）。
#
# ToolArgs 列存的是原始 kwargs JSON，是**权威**来源；故在此按声明顺序
# 重排 action_args。原始 `action` 字符串保持 CSV 原样不动（证据保真）。
_TOOL_PARAM_ORDER = {
    "use_supply": ["fire_id", "supply_type"],
    "get_supply": ["source_id", "supply_type"],
    "drop_off_person": ["person_id", "deposit_id"],
    "finish_task": ["success", "summary", "task_description"],
}


def _canonical_action_args(
    tool_name: str, tool_args_raw: str, parsed_args: list[str]
) -> list[str]:
    """按工具声明的参数顺序重排 action_args。

    仅当 ToolArgs 能解析成 dict 且**覆盖了全部**声明参数时才重排；否则原样
    返回 —— 部分覆盖时重排会静默丢参，比顺序错位更糟。
    """
    order = _TOOL_PARAM_ORDER.get(tool_name)
    if not order or not tool_args_raw:
        return parsed_args
    try:
        args = json.loads(tool_args_raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return parsed_args
    if not isinstance(args, dict) or not all(k in args for k in order):
        return parsed_args
    return [str(args[k]) for k in order]


def parse_action(action_str: str) -> tuple[str, list[str]]:
    """Parse 'NavigateTo(Target)' into ('NavigateTo', ['Target']).

    Tool-level action names are normalized to env-level names via _ACTION_ALIASES
    (e.g. agent_interactions.csv logs CarryPerson(...) while trajectory.csv logs Carry(...)).
    """
    m = _ACTION_RE.match(action_str.strip())
    if not m:
        return action_str.strip(), []
    name = m.group(1)
    args_str = m.group(2)
    args = [a.strip() for a in args_str.split(",") if a.strip()]
    return _ACTION_ALIASES.get(name, name), args


#: 环境层动作词表 —— parse_action 归一化（含别名映射）后的**环境**动作名。
#:
#: 全仓唯一来源：constraint / error_taxonomy / outcome 都从这里导入，不再各自
#: 维护重复白名单（此前三份表靠人工同步，别名漏收会在白名单处静默丢弃动作）。
#: 不可变 frozenset：任何词表变更都必须回到这里改，下游只读。
ENV_ACTION_NAMES = frozenset(
    {
        "Explore",
        "NavigateTo",
        "Move",
        "GetSupply",
        "UseSupply",
        "Carry",
        "DropOff",
        "StoreSupply",
        "ClearInventory",
        "NoOp",
    }
)


@dataclass
class AgentInteraction:
    step: int
    agent: str
    tool_name: str
    tool_args: str
    action: str
    observation: str
    llm_input: str
    llm_output: str
    thinking: str
    error_type: str
    tool_latency_ms: str

    # inventory 是动作**之后**的快照（observation 由环境在 step 之后生成）。
    # 判定"动作发起时是否具备前提"必须用 inventory_before，否则会把
    # "成功用掉最后一单位"误判为违规（E-2：65/65 全误报）。
    inventory: Optional[dict] = None
    inventory_before: Optional[dict] = None
    position: Optional[tuple[int, int, int]] = None
    # visible_names 同样是动作**之后**的快照（与 inventory 同源，都出自本行的
    # Observation）。判定"agent 决定导航时看得见这个目标吗"必须用
    # visible_names_before —— 否则会把"成功走到目标、到达后周围可见集变了、
    # 目标掉出列表"的正常导航判成幻觉（E-2 同类时序缺陷，实测 18 起误报）；
    # 反向也会漏报（到达后才出现在列表里的目标逃脱指控，实测 3 起）。
    visible_names: list[str] = field(default_factory=list)
    # None = 无前序快照（该 agent 的首条交互，或此前没有任何一行解析出 Names）。
    # 与 inventory_before 一致用 Optional 而非空列表兜底：空列表是"当时确实什么
    # 都看不见"（可指控），None 是"无从判定"（不指控），两者不可混。
    visible_names_before: Optional[list[str]] = None
    action_name: str = ""
    action_args: list[str] = field(default_factory=list)
    csv_line: int = 0
    # None = 无法从 observation 判定（未知），不猜。
    succeeded: Optional[bool] = None

    # 本行的 observation 是 harness 的工具执行失败标记（见 `is_error_observation`）
    # ——即这个动作**抛异常了**，很可能从未到达环境。
    error_observation: bool = False
    # 同一 (step, agent) 下本行**之后**还有多少行。仅在被选为代表行时有意义：
    # >0 表示该 step 提交过多次动作，本行只是其中第一次。
    superseded_rows: int = 0
    # 代表行本身就是异常行、且后面还有重试行 —— 即 grader 拿到的这个"动作"
    # 极可能是**幽灵动作**（提交了但没进环境）。让下游能把它与正常单动作步区分开。
    phantom_first_row: bool = False


@dataclass
class Dispatch:
    step: int
    subtask: str
    assigned_to: str
    correlation_id: str
    worker_task_id: str
    event_type: str


@dataclass
class SubtaskRecord:
    step: int
    subtask_id: str
    status: str
    assigned_to: str
    subtask: str
    failure_class: str


@dataclass
class StepRecord:
    step: int
    actions: list[str]
    successes: list[bool]
    timeout_agents: list[int]
    coverage: float
    transport_rate: float
    finished: bool
    end_reason: str
    completed_subtasks_delta: list[str]
    interactions: list[AgentInteraction] = field(default_factory=list)
    dispatches: list[Dispatch] = field(default_factory=list)
    subtasks: list[SubtaskRecord] = field(default_factory=list)


@dataclass
class EpisodeDataset:
    run_dir: Path
    metadata: dict
    steps: dict[int, StepRecord]
    summary: dict[str, str | float | int]
    token_usage_rows: list[dict]
    semantic_map_log: list[dict]
    map_summaries: list[dict]
    agent_names: list[str]
    subtask_records: list[SubtaskRecord]
    dispatches: list[Dispatch]

    grader_skips: list[dict] = field(default_factory=list)

    _interaction_map: dict[tuple[int, str], AgentInteraction] = field(
        default_factory=dict
    )

    def get_step(self, step: int) -> Optional[StepRecord]:
        return self.steps.get(step)

    @property
    def last_step(self) -> Optional[StepRecord]:
        if not self.steps:
            return None
        return self.steps[max(self.steps.keys())]

    def get_interaction(self, step: int, agent_name: str) -> Optional[AgentInteraction]:
        return self._interaction_map.get((step, agent_name))

    def get_agent_success(self, step: int, agent_index: int) -> Optional[bool]:
        sr = self.steps.get(step)
        if sr is None or agent_index >= len(sr.successes):
            return None
        return sr.successes[agent_index]

    def get_agent_action(self, step: int, agent_index: int) -> Optional[str]:
        sr = self.steps.get(step)
        if sr is None or agent_index >= len(sr.actions):
            return None
        return sr.actions[agent_index]


_INVENTORY_RE = re.compile(r"I am holding (\{.*?\})")
_POSITION_RE = re.compile(r"co-ordinates:\s*(\([^)]+\))")
_NAMES_RE = re.compile(r"Names:\s*(\[[^\]]*\])")

# 当前动作的结果句。环境措辞（实测 20 个 run / 13085 行）：
#   "I tried to <动作> and was successful."       4749 次
#   "I tried to <动作> and was not successful."    293 次
# 必须整行锚定（^...$）而非子串匹配，原因有两个：
#   1) observation 里还有历史回顾句 "Previously, I have tried to ..., but was
#      unsuccessful"（1134 次）—— 它描述的是**过去**的失败。子串匹配
#      "unsuccessful" 会把 841 行实际成功的动作误判为失败。
#   2) observation 有时嵌入 prompt/skill 文本（如 "After successful drop-off:"），
#      子串匹配同样会命中。
_CURRENT_OUTCOME_RE = re.compile(
    r"^I tried to .*? and was (?P<neg>not )?successful\.?$"
)
# 历史回顾句，显式排除，避免将来措辞变动时被上面的模式意外吃进去。
_PRIOR_OUTCOME_RE = re.compile(r"^Previously, I have tried to ")

# 工具执行**抛异常**时 harness 写进 Observation 的结构化标记。
# 产出点唯一且确定：`src/Agent/worker_agent/agent.py:819`
#   content = result.content if result.success else f"Error: {result.error}"
# 即这个前缀**当且仅当** `result.success is False` 时出现 —— 它不是 LLM 写的
# 文本，也不是环境的结果句，而是 harness 的失败标记。
#
# 实测形态（87 个 run / 13085 行，366 行命中）：
#   "Error: Tool execution failed: AttributeError: 'NoneType' object has no
#    attribute 'get_radius'\n\nTraceback:\nTraceback (most recent call last):..."
#   "Error: Tool execution failed: AssertionError: Move action called direction
#    DownLeft is not valid, must be in ['Up','Down','Left','Right','Center']"
#   "Error: Tool execution failed: TypeError: UseSupplyTool.execute() missing 1
#    required positional argument: 'fire_id'"
#   "Error: Skill 'exploration' does not exist. Available skills: ..."
#   "Error: None"（result.error 本身为 None，但 success 仍是 False）
#
# 锚定选择：**只看首个非空行的行首前缀**（`^Error: ` + `match`），不用
# `re.search` / `re.MULTILINE` 全文搜。理由与 `_CURRENT_OUTCOME_RE` 选整行锚定
# 同源 —— 子串/全文匹配会误伤正常 observation 里嵌入的文本：
#   · `map_agent__query_natural` 成功返回的 JSON 里就带 `"error": "Error code:
#     429 - ..."` 字段（实测 2 行）。那是**工具结果载荷**，工具本身执行成功，
#     动作也不是环境动作，判定必须保持 None。
#   · get_skill 返回的 skill markdown、report_observation 的 JSON 都可能含
#     "Error" 字样。
# 全文搜会把这些一并判成失败；只认首行行首则天然免疫（实测 366 行命中全部
# 为真失败，0 误报；且带 `Error:` 行而非首行的样本数为 0 —— harness 的这个
# 标记只出现在内容开头）。整行锚定（`$`）在这里**不适用**：错误正文紧跟在
# 前缀之后，且常带多行 traceback。
_ERROR_OBSERVATION_RE = re.compile(r"^Error: ")


def is_error_observation(observation: str) -> bool:
    """observation 是否为 harness 的**工具执行失败**标记（见 `_ERROR_OBSERVATION_RE`）。

    单一判定入口：`_detect_success` 与 `AgentInteraction.error_observation`
    共用它，避免同一个概念出现两条派生路径（T4 的教训）。
    """
    if not observation:
        return False
    for line in observation.splitlines():
        line = line.strip()
        if not line:
            continue
        return bool(_ERROR_OBSERVATION_RE.match(line))
    return False


def _detect_success(observation: str, tool_name: str = "") -> Optional[bool]:
    """从 observation 判定**当前**动作是否成功。

    返回 None 表示无法判定（例如 report_observation 这类不产生结果句的工具），
    而不是猜一个值 —— 下游 grader 对 None 的处理是"不指控"。

    这是全仓唯一的成功判定入口：措辞散落在多处字符串匹配里时，环境改一次
    措辞就会静默失效（`tests/test_eval_success_detection.py` 钉住当前措辞）。
    """
    if not observation:
        return None
    # 工具执行抛异常 → **确定性失败**，不是"判不出来"。
    # 它本来就不是 "I tried to ..." 结果句，故旧实现让它落到 None，后果有两处：
    #   · `outcome.py:compute_tool_outcomes` 把它计进 `unknown` 桶 ——
    #     "引擎抛异常"被显示成"判不出来"，真实失败被藏起来；
    #   · `eval/agent/eval_agent.py:50` 的 judge 采样按 `succeeded is False`
    #     选步，于是这些**最病态**的步骤永远不会被 LLM judge 看到。
    # 注意这只是新增一个**确定可判**的形态，不改"解析不出就不猜"的总策略：
    # 查询类工具（无结果句的 JSON / skill 文本）仍然返回 None。
    if is_error_observation(observation):
        return False
    for line in observation.splitlines():
        line = line.strip()
        if not line or _PRIOR_OUTCOME_RE.match(line):
            continue
        m = _CURRENT_OUTCOME_RE.match(line)
        if m:
            return m.group("neg") is None
    return None


def _parse_observation(
    text: str,
) -> tuple[Optional[dict], Optional[tuple[int, int, int]], list[str]]:
    inventory = None
    position = None
    names = []

    m = _INVENTORY_RE.search(text)
    if m:
        try:
            inventory = json.loads(m.group(1).replace("'", '"'))
        except (json.JSONDecodeError, ValueError):
            pass

    m = _POSITION_RE.search(text)
    if m:
        try:
            pos = tuple(int(x.strip()) for x in m.group(1).strip("()").split(","))
            if len(pos) == 3:
                position = pos
        except (ValueError, TypeError):
            pass

    m = _NAMES_RE.search(text)
    if m:
        try:
            parsed = ast.literal_eval(m.group(1))
            if isinstance(parsed, list):
                names = [str(n) for n in parsed]
        except (ValueError, SyntaxError, TypeError):
            pass

    return inventory, position, names


def _load_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


class _RowParseError(ValueError):
    """某一行 CSV 无法当作数据行解析（字段类型不对）。"""


def _row_int(row: dict, key: str, default: int = 0) -> int:
    """取 row[key] 并转 int；不可转时抛 `_RowParseError`（供调用方记 skip）。

    实测 115 个 run 里有 16 个 trajectory.csv 的表头被重复写成了第一条数据行
    （`Step` 列的值是字符串 `'Step'`），旧实现在 `int()` 处直接抛
    `ValueError: invalid literal for int() with base 10: 'Step'`，整个
    `load_episode()` 崩掉 —— 一行脏数据废掉一个 run 的全部评估。
    """
    raw = row.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise _RowParseError(f"{key}={raw!r} is not an integer") from exc


def _row_float(row: dict, key: str, default: float = 0.0) -> float:
    """取 row[key] 并转 float；不可转时抛 `_RowParseError`。"""
    raw = row.get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise _RowParseError(f"{key}={raw!r} is not a number") from exc


def _skip_bad_row(
    grader_skips: list[dict], source: str, line_no: int, exc: _RowParseError
) -> None:
    """把"这一行不可用"记进 grader_skips。

    绝不静默丢行 —— 数据缺失必须与数据干净可区分（这是本仓反复出现过的
    失败模式：跳过的东西不留痕，下游看不出统计口径少了东西）。
    """
    grader_skips.append(
        {
            "grader": "dataset",
            "reason": f"{source}:L{line_no} unparseable data row ({exc})",
        }
    )


def _parse_csv_list(value: str):
    if not value or value == "[]":
        return []
    try:
        return json.loads(value.replace("'", '"'))
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        import ast

        parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, SyntaxError):
        return []


def load_episode(run_dir: str | Path) -> EpisodeDataset:
    run_dir = Path(run_dir)
    grader_skips = []

    metadata_path = run_dir / "metadata.json"
    metadata = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        grader_skips.append({"grader": "dataset", "reason": "metadata.json missing"})

    agent_names_raw = metadata.get("agent_names", [])
    if not agent_names_raw:
        # 与 experiment.py 的命名规范保持一致：按 agent_count 取规范名前 N 个。
        # 旧的兜底只给 4 个名字，5+ agent 的 run 会在 index() 时 KeyError。
        canonical = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"]
        try:
            count = int(metadata.get("agent_count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        agent_names_raw = canonical[:count] if 0 < count <= len(canonical) else canonical[:4]

    traj_rows = _load_csv(run_dir / "trajectory.csv")
    router_rows = _load_csv(run_dir / "router_interactions.csv")
    subtask_rows = _load_csv(run_dir / "subtasks.csv")
    agent_rows = _load_csv(run_dir / "agent_interactions.csv")

    summary_rows = _load_csv(run_dir / "summary.csv")
    summary = {}
    if summary_rows:
        summary = dict(summary_rows[0])

    token_usage_rows = _load_csv(run_dir / "token_usage.csv")

    semantic_map_log = _load_jsonl(run_dir / "semantic_map.jsonl")
    if not semantic_map_log:
        grader_skips.append(
            {"grader": "dataset", "reason": "semantic_map.jsonl missing or empty"}
        )

    map_summaries = _load_jsonl(run_dir / "map_summary.jsonl")
    if not map_summaries:
        grader_skips.append(
            {"grader": "dataset", "reason": "map_summary.jsonl missing or empty"}
        )

    supervision_dir = run_dir / "supervision"
    if not supervision_dir.exists() or not any(supervision_dir.iterdir()):
        grader_skips.append(
            {"grader": "dataset", "reason": "supervision/ missing or empty"}
        )

    subtask_records: list[SubtaskRecord] = []
    for line_idx, row in enumerate(subtask_rows):
        try:
            raw_step = _row_int(row, "Step")
        except _RowParseError as exc:
            _skip_bad_row(grader_skips, "subtasks.csv", line_idx + 2, exc)
            continue
        subtask_records.append(
            SubtaskRecord(
                step=raw_step + 1,
                subtask_id=row.get("SubtaskID", ""),
                status=row.get("Status", ""),
                assigned_to=row.get("AssignedTo", ""),
                subtask=row.get("Subtask", ""),
                failure_class=row.get("FailureClass", ""),
            )
        )

    dispatches: list[Dispatch] = []
    for line_idx, row in enumerate(router_rows):
        try:
            raw_step = _row_int(row, "Step")
        except _RowParseError as exc:
            _skip_bad_row(grader_skips, "router_interactions.csv", line_idx + 2, exc)
            continue
        dispatches.append(
            Dispatch(
                step=raw_step + 1,
                subtask=row.get("Subtask", ""),
                assigned_to=row.get("AssignedTo", ""),
                correlation_id=row.get("CorrelationID", ""),
                worker_task_id=row.get("WorkerTaskID", ""),
                event_type=row.get("EventType", ""),
            )
        )

    dispatches_by_step: dict[int, list[Dispatch]] = {}
    for d in dispatches:
        dispatches_by_step.setdefault(d.step, []).append(d)

    subtasks_by_step: dict[int, list[SubtaskRecord]] = {}
    for s in subtask_records:
        subtasks_by_step.setdefault(s.step, []).append(s)

    agent_interactions_by_step: dict[int, list[AgentInteraction]] = {}
    # 每个 agent 上一条**成功解析出**的 inventory 快照（= 本条动作发起前的库存）。
    # 按 CSV 行序推进：agent_interactions.csv 是按时间追加的，同一 step 内多个
    # agent 的行交错出现，故必须按 agent 分别维护，不能用全局 last。
    last_inventory_by_agent: dict[str, dict] = {}
    # 同上，但存的是可见对象名列表（= 本条动作发起前该 agent 看得见什么）。
    # 与 inventory 用同一条 CSV 行序推进、同样按 agent 分别维护。
    last_visible_names_by_agent: dict[str, list[str]] = {}
    for line_idx, row in enumerate(agent_rows):
        try:
            step = _row_int(row, "Step")
        except _RowParseError as exc:
            # 实测该文件目前无脏行，但表头重复写入是 logger 层的共性风险
            # （trajectory.csv 已出现 16 次），故同样加固而非假定它不会发生。
            _skip_bad_row(grader_skips, "agent_interactions.csv", line_idx + 2, exc)
            continue
        obs_text = row.get("Observation", "")
        inv, pos, names = _parse_observation(obs_text)
        act_str = row.get("Action", "")
        act_name, act_args = parse_action(act_str)
        tool_name = row.get("ToolName", "")
        # Action 字符串的参数顺序随 LLM 的 JSON 键序漂移；用权威的 ToolArgs 重排。
        act_args = _canonical_action_args(tool_name, row.get("ToolArgs", ""), act_args)
        agent_name = row.get("Agent", "")
        # 首条交互没有前序快照 → None（下游据此记 parse_miss 而非判违规）。
        inv_before = last_inventory_by_agent.get(agent_name)
        names_before = last_visible_names_by_agent.get(agent_name)
        ai = AgentInteraction(
            step=step,
            agent=agent_name,
            tool_name=tool_name,
            tool_args=row.get("ToolArgs", ""),
            action=act_str,
            observation=obs_text,
            llm_input=row.get("LLMInput", ""),
            llm_output=row.get("LLMOutput", ""),
            thinking=row.get("Thinking", ""),
            error_type=row.get("ErrorType", ""),
            tool_latency_ms=row.get("ToolLatencyMs", ""),
            inventory=inv,
            inventory_before=inv_before,
            position=pos,
            visible_names=names,
            visible_names_before=names_before,
            action_name=act_name,
            action_args=act_args,
            csv_line=line_idx + 2,
            succeeded=_detect_success(obs_text, tool_name),
            error_observation=is_error_observation(obs_text),
        )
        if inv is not None:
            last_inventory_by_agent[agent_name] = inv
        # `_parse_observation` 把"没有 Names 行"与"Names: []"都收敛成 []，故这里
        # 只能用真值判断。实测 87 run / 13085 行：`Names:` 行出现 5042 次，其中
        # 字面 `[]` **0 次**、正则命中但解析失败 **0 次** —— 即当前数据里"真值"
        # 与"有 Names 行"完全等价。若环境将来真的输出 `Names: []`，此处会保留更早
        # 的快照而非记下"当时什么都看不见"，方向上偏保守（少指控），与本仓
        # "漏报优于误报"的既定取向一致。
        if names:
            last_visible_names_by_agent[agent_name] = names
        agent_interactions_by_step.setdefault(step, []).append(ai)

    steps: dict[int, StepRecord] = {}
    for line_idx, row in enumerate(traj_rows):
        try:
            step = _row_int(row, "Step")
            coverage = _row_float(row, "Coverage")
            transport_rate = _row_float(row, "TransportRate")
        except _RowParseError as exc:
            _skip_bad_row(grader_skips, "trajectory.csv", line_idx + 2, exc)
            continue
        actions = _parse_csv_list(row.get("Actions", "[]"))
        successes = _parse_csv_list(row.get("Successes", "[]"))
        successes_bool = [bool(s) for s in successes]
        timeout_agents = _parse_csv_list(row.get("TimeoutAgents", "[]"))
        finished = row.get("Finished", "False").strip().lower() == "true"
        end_reason = row.get("EndReason", "")
        cst_delta = _parse_csv_list(row.get("CompletedSubtasksDelta", "[]"))

        sr = StepRecord(
            step=step,
            actions=actions,
            successes=successes_bool,
            timeout_agents=timeout_agents,
            coverage=coverage,
            transport_rate=transport_rate,
            finished=finished,
            end_reason=end_reason,
            completed_subtasks_delta=cst_delta,
            interactions=agent_interactions_by_step.get(step, []),
            dispatches=dispatches_by_step.get(step, []),
            subtasks=subtasks_by_step.get(step, []),
        )
        steps[step] = sr

    # 每个 (step, agent) 取**首行**作代表行给 grader。这在一般情形下是对的：
    # 同组后续行是同一 step 内的重试/查询，不是另一个动作。
    #
    # 但 barrier 层有个 bug（对角 Move / None 导航目标 / 缺参 会让
    # `submit_action` 抛异常而非正常完成，修复在 `sar_orch/barrier.py`，另一批次）
    # 触发时，首行自己就可能是那个**没进到环境**的提交 —— 它只是"提交顺序第一"，
    # 不是"第一个成功的"。此时 grader 拿着一个幽灵动作当成该 step 的真实动作在判。
    #
    # 为什么**不**改成"跳过开头的异常行、取第一个非异常行"：实测 87 个 run，
    # 首行为异常行的多行 (step,agent) 组共 14 个（环境动作类工具），这 14 组里
    # **每一行都是**异常行 —— 根本不存在"非异常的那一行"可换。放宽到全部工具后，
    # 仅 3 组是混合的，且其中的非异常行全是 map_agent__* / get_agent_state 这类
    # **查询**工具 —— 换成它们等于把"该 step 的动作"换成一次查询，更错。
    # 拿 trajectory.csv 的 Actions 列作真值交叉核对这 14 组：首行命中 3/14、
    # 末行命中 7/14、首个非异常行命中 0/14 —— 末行虽然相对高，但仍有一半不符，
    # 不足以支撑"改成取末行"这种会动到全部 1809 个多行组的口径变更。
    #
    # 故policy = 保留首行语义（口径不动，正常路径零影响），改为**标注**：
    # `phantom_first_row` 让下游能把"幽灵动作"与真正的单动作步区分开；
    # 配合 Defect 1，这些行的 `succeeded` 现在是 False（确定性失败）而非 unknown。
    # 不在 eval 层反推"环境到底执行了什么" —— 这里没有真值（正是 barrier 修复
    # 要从根上消除的歧义），能做的是不让幽灵动作**静默**冒充正常动作。
    interaction_map: dict[tuple[int, str], AgentInteraction] = {}
    rows_by_key: dict[tuple[int, str], list[AgentInteraction]] = {}
    for step, interactions in agent_interactions_by_step.items():
        for ai in interactions:
            rows_by_key.setdefault((step, ai.agent), []).append(ai)

    for key, rows in rows_by_key.items():
        rep = rows[0]
        rep.superseded_rows = len(rows) - 1
        rep.phantom_first_row = rep.error_observation and rep.superseded_rows > 0
        if rep.phantom_first_row:
            step, agent = key
            # 绝不静默：跳过/降级的东西必须留痕，否则下游看不出口径里掺了幽灵动作。
            grader_skips.append(
                {
                    "grader": "dataset",
                    "reason": (
                        f"agent_interactions.csv:L{rep.csv_line} step={step} "
                        f"agent={agent}: representative row is a tool-execution "
                        f"error (action likely never reached the environment); "
                        f"{rep.superseded_rows} later row(s) for the same step "
                        f"were not used as representative"
                    ),
                }
            )
        # P1.0 query-shadow 诊断：首行是非环境动作（query 等）、同组后续含环境
        # action 时，legacy representative 是查询行，真实环境动作被藏在后面
        # （实测 query-first 模式下 3,193 个多行 agent-step 里 214 个无
        # action+success 一致的候选 —— 不能自动换代表行）。
        # 绝不静默：记录首行 CSV line 与隐藏的环境 action CSV lines 供审计；
        # 本 phase 不改 `interaction_map[key] = rows[0]` 的兼容语义。
        hidden_env_rows = [ai for ai in rows[1:] if ai.action_name in ENV_ACTION_NAMES]
        if rep.action_name not in ENV_ACTION_NAMES and hidden_env_rows:
            step, agent = key
            hidden_lines = ", ".join(
                f"agent_interactions.csv:L{ai.csv_line}" for ai in hidden_env_rows
            )
            grader_skips.append(
                {
                    "grader": "dataset",
                    "reason": (
                        f"query_shadow step={step} agent={agent}: representative "
                        f"agent_interactions.csv:L{rep.csv_line} is a non-environment "
                        f"action shadowing {len(hidden_env_rows)} environment "
                        f"action(s) on hidden line(s) {hidden_lines}"
                    ),
                }
            )
        interaction_map[key] = rep

    return EpisodeDataset(
        run_dir=run_dir,
        metadata=metadata,
        steps=steps,
        summary=summary,
        token_usage_rows=token_usage_rows,
        semantic_map_log=semantic_map_log,
        map_summaries=map_summaries,
        agent_names=agent_names_raw,
        subtask_records=subtask_records,
        dispatches=dispatches,
        grader_skips=grader_skips,
        _interaction_map=interaction_map,
    )
