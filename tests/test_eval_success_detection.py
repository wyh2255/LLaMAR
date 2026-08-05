"""成功检测集中化 + empty_supply_use 误报修复的回归测试（T4 / DESIGN 2.1、2.5）。

这个文件钉住三件事，任一被破坏都应让测试失败而非静默降级：

1. **环境措辞**：`_detect_success` 只认整行的
   `I tried to ... and was (not )?successful.`。环境改措辞时这里先红。
2. **inventory_before 时序**：E-2 的根因是用动作**后**的库存判动作**前**的
   前提，把"成功用掉最后一单位"判成违规（65/65 全误报）。
3. **参数顺序规范化**：`worker.py:_build_action()` 按 LLM 的 JSON 键序拼
   Action 字符串，故日志里可能出现 `UseSupply(Water, Fire_Region_1)`。
   eval 层必须用权威的 ToolArgs 重排，否则按下标取参的 grader 全部读错位。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.eval.agent.eval_agent import select_judge_steps
from sar_orch.eval.dataset import (
    ENV_ACTION_NAMES,
    _detect_success,
    is_error_observation,
    load_episode,
    parse_action,
)
from sar_orch.eval.graders.constraint import grade_constraint
from sar_orch.eval.graders.outcome import compute_tool_outcomes

AGENT_COLUMNS = [
    "Step",
    "Agent",
    "ToolName",
    "ToolArgs",
    "Action",
    "Observation",
    "LLMInput",
    "LLMOutput",
    "Thinking",
    "RunID",
    "CorrelationID",
    "EventType",
    "ToolLatencyMs",
    "ErrorType",
]

TRAJ_COLUMNS = [
    "Step",
    "Actions",
    "Successes",
    "TimeoutAgents",
    "Coverage",
    "TransportRate",
    "Finished",
    "EndReason",
    "CompletedSubtasksDelta",
]


_DEFAULT_NAMES = ("GreatFire_Region_1", "Reservoir_1")


def _obs(
    inventory: dict,
    *,
    action_desc: str,
    success: bool = True,
    names: tuple[str, ...] = _DEFAULT_NAMES,
) -> str:
    """拼一条形似真实环境输出的 observation。

    `names` 即该行 Observation 里的 `Names:` 列表 —— 环境在动作**之后**生成，
    故它是"动作后可见集"。判定导航目标是否幻觉要用**前一行**的这份列表
    （见 `visible_names_before` 相关用例）。
    """
    outcome = "successful" if success else "not successful"
    return (
        f"I am at co-ordinates: (0, 0, 0)\n"
        f"I am holding {json.dumps(inventory)}\n"
        f"Names: {list(names)}\n"
        f"I tried to {action_desc} and was {outcome}."
    )


def _write_run(
    tmp_path: Path,
    rows: list[dict],
    *,
    agent_names: tuple[str, ...] = ("Alice",),
    steps: int | None = None,
) -> Path:
    """落盘一个最小 run 目录（metadata + agent_interactions + trajectory）。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"agent_names": list(agent_names), "agent_count": len(agent_names)}),
        encoding="utf-8",
    )

    with (run_dir / "agent_interactions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=AGENT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in AGENT_COLUMNS})

    n_steps = steps if steps is not None else max((r["Step"] for r in rows), default=1)
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAJ_COLUMNS)
        w.writeheader()
        for s in range(1, n_steps + 1):
            w.writerow(
                {
                    "Step": s,
                    "Actions": json.dumps(["NoOp"] * len(agent_names)),
                    "Successes": json.dumps([True] * len(agent_names)),
                    "TimeoutAgents": "[]",
                    "Coverage": 0.0,
                    "TransportRate": 0.0,
                    "Finished": "False",
                    "EndReason": "",
                    "CompletedSubtasksDelta": "[]",
                }
            )
    return run_dir


def _violations(run_dir: Path) -> list[dict]:
    results = grade_constraint(load_episode(run_dir))
    return [v for r in results for v in r.detail["violations"]]


def _parse_miss(run_dir: Path) -> dict:
    results = grade_constraint(load_episode(run_dir))
    return results[0].detail["parse_miss_counts"]


# --------------------------------------------------------------------------
# 1. _detect_success — 钉住环境措辞
# --------------------------------------------------------------------------


def test_detect_success_current_outcome_positive():
    assert _detect_success("I tried to use supply and was successful.") is True


def test_detect_success_current_outcome_negative():
    assert _detect_success("I tried to navigate and was not successful.") is False


def test_detect_success_ignores_prior_failure_recap():
    """历史回顾句描述的是**过去**的失败，不能影响当前动作判定。

    实测 20 run：`was unsuccessful` 出现 1134 次，全在这种回顾句里；
    子串匹配会把其中 841 行**实际成功**的动作判成失败。
    """
    obs = (
        "Previously, I have tried to navigate to GreatFire_Region_1, "
        "but was unsuccessful\n"
        "I tried to use supply and was successful."
    )
    assert _detect_success(obs) is True


def test_detect_success_ignores_embedded_prompt_text():
    """observation 里嵌入的 prompt/skill 文本不是结果句。"""
    obs = (
        "After successful drop-off: call finish_task(success=True)\n"
        "I tried to move Up and was not successful."
    )
    assert _detect_success(obs) is False


def test_detect_success_unknown_returns_none():
    """无结果句 → None（未知），不猜。下游对 None 的处理是"不指控"。"""
    assert _detect_success("I am holding {'Sand': 1}") is None
    assert _detect_success("") is None


def test_detect_success_requires_whole_line_anchor():
    """结果句必须整行成立；被包在更长句子里的不算。"""
    assert _detect_success("Note that I tried to X and was successful. Also foo") is None


# --------------------------------------------------------------------------
# 1b. 工具执行异常 → **确定性失败**（不是 unknown）
#
# 与紧邻的"仍然 None"用例成对存在：这里新增的只是一个**确定可判**的形态
# （harness 在 `result.success is False` 时写下的 `Error: ` 标记），
# "解析不出就不猜"的总策略未变。谁想把 None 改成"一律猜失败"，
# 下面 test_detect_success_*_still_unknown 会先红。
# --------------------------------------------------------------------------

# 实测形态，取自 sar_orch/results/*/agent_interactions.csv（87 run / 13085 行，
# 命中 366 行）。产出点唯一：src/Agent/worker_agent/agent.py:819。
_REAL_ERROR_OBS_TRACEBACK = (
    "Error: Tool execution failed: AttributeError: 'NoneType' object has no "
    "attribute 'get_radius'\n"
    "\n"
    "Traceback:\n"
    "Traceback (most recent call last):\n"
    '  File "/home/wyh/daily_work/LLaMAR/src/Agent/worker_agent/agent.py", line 728, in run\n'
    "    result = await tool.execute(**arguments)\n"
    '  File "/home/wyh/daily_work/LLaMAR/sar_orch/barrier.py", line 168, in submit_action\n'
    "    await asyncio.to_thread(self._execute_step,\n"
)
_REAL_ERROR_OBS_ASSERTION = (
    "Error: Tool execution failed: AssertionError: Move action called direction "
    "DownLeft is not valid, must be in ['Up', 'Down', 'Left', 'Right', 'Center']"
)
_REAL_ERROR_OBS_MISSING_ARG = (
    "Error: Tool execution failed: TypeError: UseSupplyTool.execute() missing 1 "
    "required positional argument: 'fire_id'"
)


def test_detect_success_error_observation_is_determinate_failure():
    """工具抛异常 → False（确定失败），**不是** None。

    未修复前这些行落进 `compute_tool_outcomes` 的 `unknown` 桶（"判不出来"），
    且 `eval_agent.py:50` 的 judge 采样按 `succeeded is False` 选步，
    于是一个 episode 里**最病态**的步骤永远不会被 judge 看到。
    """
    for obs in (
        _REAL_ERROR_OBS_TRACEBACK,
        _REAL_ERROR_OBS_ASSERTION,
        _REAL_ERROR_OBS_MISSING_ARG,
        "Error: Skill 'exploration' does not exist. Available skills: navigation",
        "Error: None",  # result.error 为 None，但 success 仍是 False
    ):
        assert is_error_observation(obs) is True, obs[:60]
        assert _detect_success(obs) is False, obs[:60]


def test_detect_success_query_tool_payload_still_unknown():
    """与上一条成对：**查询类**工具的返回载荷仍然是 None。

    这条钉住 Defect 1 的修复没有退化成"含 Error 字样就判失败"。第一个样本是
    实测数据：`map_agent__query_natural` **执行成功**、返回的 JSON 里带
    `"error": "Error code: 429 ..."` 字段 —— 那是结果载荷，动作也不是环境动作，
    判定必须保持 None。全文 `re.search` 会把它误判成失败。
    """
    payload_429 = (
        "{\n"
        '  "answer": "",\n'
        '  "error": "Error code: 429 - concurrent_request_limit_exceeded"\n'
        "}"
    )
    assert is_error_observation(payload_429) is False
    assert _detect_success(payload_429) is None

    # skill markdown / 普通查询返回同样不得被猜成失败
    for obs in (
        "# Skill: navigation\n\nAvoid errors: never pass None as a target.",
        '{"reporter": "Alice", "step": 1, "object_type": "fire"}',
        "No unread messages.",
    ):
        assert is_error_observation(obs) is False, obs[:40]
        assert _detect_success(obs) is None, obs[:40]


def test_error_marker_must_be_at_start_of_observation():
    """`Error: ` 只在**首个非空行的行首**才算 harness 标记。

    实测 366 个命中里，带 `Error:` 却不在开头的样本数为 0 —— 这个标记就是
    `f"Error: {result.error}"` 拼在内容最前面。放宽成子串/全文搜会误伤
    observation 里嵌入的文本（本函数历史上正是因子串匹配踩过误报）。
    """
    assert is_error_observation("I tried to move Up and was successful.\nError: x") is False
    assert is_error_observation("Note: Error: something") is False
    # 前导空行不影响判定（取首个**非空**行）
    assert is_error_observation("\n\nError: Tool execution failed: X") is True


def test_error_observation_lands_in_failed_not_unknown(tmp_path):
    """端到端：异常行经完整管线后计进 `failed` 桶，而不是 `unknown`。

    Defect 1 的下游后果就在这里 —— `unknown` 读作"判不出来"，会把真实的
    引擎异常藏起来，让行为退化伪装成解析问题。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "move",
                "ToolArgs": json.dumps({"direction": "DownLeft"}),
                "Action": "Move(DownLeft)",
                "Observation": _REAL_ERROR_OBS_ASSERTION,
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "move",
                "ToolArgs": json.dumps({"direction": "Up"}),
                "Action": "Move(Up)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0}, action_desc="move Up"
                ),
            },
        ],
    )
    outcomes = compute_tool_outcomes(load_episode(run))
    assert outcomes["move"] == {
        "attempts": 2,
        "succeeded": 1,
        "failed": 1,
        "unknown": 0,
    }


def test_error_step_is_selected_for_judge_review(tmp_path):
    """异常步必须进入 judge 采样。

    `eval_agent.py:select_judge_steps` 按 `succeeded is False` 选步；异常步
    此前是 None，于是被永久跳过 —— 而引擎抛异常至少与普通动作失败同等值得看。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": None}),
                "Action": "NavigateTo(None)",
                "Observation": _REAL_ERROR_OBS_TRACEBACK,
            },
        ],
        steps=3,
    )
    ep = load_episode(run)
    # target=1 时只有"含失败交互的步"能挤进来，故这是个强断言
    assert 2 in select_judge_steps(ep, 1)


# --------------------------------------------------------------------------
# 2. inventory_before 时序 — E-2
# --------------------------------------------------------------------------


def test_get_then_use_is_not_a_violation(tmp_path):
    """get→use 序列：用掉最后一单位后库存归零，**不得**判违规（E-2 的 65/65）。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps({"source_id": "Reservoir_1", "supply_type": "Water"}),
                "Action": "GetSupply(Reservoir_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 1, "Person": 0},
                    action_desc="get supply from Reservoir_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                # 动作后库存归零 —— 旧实现正是在这里误判
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                ),
            },
        ],
    )
    assert [v["rule"] for v in _violations(run)] == []


def test_use_without_get_is_a_violation(tmp_path):
    """纯 use（前序库存确为 0）→ 必须判违规。修误报不等于放弃该规则。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                    success=False,
                ),
            },
        ],
    )
    rules = [v["rule"] for v in _violations(run)]
    assert rules.count("empty_supply_use") == 1


def test_first_interaction_without_prior_snapshot_counts_parse_miss(tmp_path):
    """无前序快照时不指控，记 parse_miss（无证据不构成指控）。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                ),
            },
        ],
    )
    assert [v["rule"] for v in _violations(run)] == []
    assert _parse_miss(run)["inventory_before"] == 1


def test_inventory_before_is_tracked_per_agent(tmp_path):
    """同 step 内多 agent 的行交错出现，前序快照必须按 agent 分别维护。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps({"source_id": "Reservoir_1", "supply_type": "Water"}),
                "Action": "GetSupply(Reservoir_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 1, "Person": 0},
                    action_desc="get supply from Reservoir_1",
                ),
            },
            {
                "Step": 1,
                "Agent": "Bob",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                # Bob 库存空 —— 若快照用全局 last，会错用 Alice 的 Water:1
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Bob",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                    success=False,
                ),
            },
        ],
        agent_names=("Alice", "Bob"),
    )
    violations = _violations(run)
    assert [(v["rule"], v["agent"]) for v in violations] == [
        ("empty_supply_use", "Bob")
    ]


# --------------------------------------------------------------------------
# 3. 参数顺序规范化 — worker.py:_build_action 的键序漂移
# --------------------------------------------------------------------------


def _reversed_use_supply_run(tmp_path: Path, tool_args: str) -> Path:
    return _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps({"source_id": "Reservoir_1", "supply_type": "Water"}),
                "Action": "GetSupply(Reservoir_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 2, "Person": 0},
                    action_desc="get supply from Reservoir_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": tool_args,
                # LLM 的 JSON 键序把 Action 字符串拼反了（运行时执行是正确的）
                "Action": "UseSupply(Water, GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 1, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                ),
            },
        ],
    )


def test_reversed_action_string_is_normalized(tmp_path):
    """Action 字符串参数反序时，用 ToolArgs 重排 → 不得判违规。

    未修复前：取 action_args[1] 得到 'GreatFire_Region_1'，拿火名去库存字典里
    查必然得 0 → 误报。实测这是 20 run 中 7 起残留误报的全部原因。
    """
    run = _reversed_use_supply_run(
        tmp_path,
        json.dumps({"supply_type": "Water", "fire_id": "GreatFire_Region_1"}),
    )
    assert [v["rule"] for v in _violations(run)] == []
    assert _parse_miss(run)["supply_type"] == 0


def test_action_args_canonical_order_from_tool_args(tmp_path):
    """规范化后 action_args 就是声明顺序 [fire_id, supply_type]。"""
    run = _reversed_use_supply_run(
        tmp_path,
        json.dumps({"supply_type": "Water", "fire_id": "GreatFire_Region_1"}),
    )
    ep = load_episode(run)
    ai = ep.get_interaction(2, "Alice")
    assert ai.action_args == ["GreatFire_Region_1", "Water"]
    # 原始 action 字符串保持 CSV 原样（证据保真）
    assert ai.action == "UseSupply(Water, GreatFire_Region_1)"


def test_unparseable_tool_args_falls_back_to_parse_miss(tmp_path):
    """ToolArgs 不可解析时无法重排；此时不指控，记 parse_miss。"""
    run = _reversed_use_supply_run(tmp_path, "not-json{{{")
    assert [v["rule"] for v in _violations(run)] == []
    assert _parse_miss(run)["supply_type"] == 1


def test_partial_tool_args_does_not_drop_arguments(tmp_path):
    """ToolArgs 只覆盖部分声明参数时不重排 —— 重排会静默丢参。"""
    run = _reversed_use_supply_run(tmp_path, json.dumps({"supply_type": "Water"}))
    ep = load_episode(run)
    ai = ep.get_interaction(2, "Alice")
    assert ai.action_args == ["Water", "GreatFire_Region_1"]


# --------------------------------------------------------------------------
# 4. 工具层→环境层动作名别名 — DropOffPerson 此前对所有约束检查不可见
# --------------------------------------------------------------------------


def test_drop_off_person_resolves_to_env_action_name():
    """`worker.py:_build_action()` 把 drop_off_person 记成 `DropOffPerson`，
    而工具 `execute()` 提交给环境/落进 trajectory.csv 的是 `DropOff`。
    别名必须把前者归一到后者，否则整个救援收尾动作在 grader 白名单处被丢掉。
    """
    name, args = parse_action("DropOffPerson(LostPersonTimmy, DepositFacility)")
    assert name == "DropOff"
    assert args == ["LostPersonTimmy", "DepositFacility"]


def test_carry_and_drop_action_names_are_all_whitelisted():
    """两个工具层名字都必须落在 `dataset.ENV_ACTION_NAMES` 内。

    未收录别名时 `constraint.py` 的
    `action_name not in ENV_ACTION_NAMES → continue` 会静默跳过 —— 实测 115 个
    run 的 90 行 DropOffPerson 因此从未被约束检查看到过。
    """
    for raw in ("CarryPerson()", "DropOffPerson(LostPersonTimmy, DepositFacility)"):
        name, _ = parse_action(raw)
        assert name in ENV_ACTION_NAMES, f"{raw} -> {name} not whitelisted"


def test_drop_off_person_is_graded_not_skipped(tmp_path):
    """端到端：携人状态下的 DropOffPerson 必须进入约束检查且**不**被判违规。

    `DropOff` 在 `ALLOWED_WHEN_CARRYING` 内，所以正确解析的结果是"无违规"；
    而如果别名缺失、动作名停留在 `DropOffPerson`，它会在白名单处被跳过
    —— 两种情况都表现为"无违规"，故本用例额外断言 action_name 已归一，
    以区分"检查通过"与"根本没检查"。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "carry_person",
                "ToolArgs": json.dumps({"person_id": "LostPersonTimmy"}),
                "Action": "CarryPerson(LostPersonTimmy)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 1},
                    action_desc="carry LostPersonTimmy",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "drop_off_person",
                "ToolArgs": json.dumps(
                    {"person_id": "LostPersonTimmy", "deposit_id": "DepositFacility"}
                ),
                "Action": "DropOffPerson(LostPersonTimmy, DepositFacility)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="drop off LostPersonTimmy at DepositFacility",
                ),
            },
        ],
    )
    ep = load_episode(run)
    ai = ep.get_interaction(2, "Alice")
    # 归一化生效 → 该行确实走进了约束检查（而非被白名单静默跳过）
    assert ai.action_name == "DropOff"
    assert ai.action_name in ENV_ACTION_NAMES
    # 原始 action 字符串保持 CSV 原样（证据保真）
    assert ai.action == "DropOffPerson(LostPersonTimmy, DepositFacility)"
    # DropOff 在 ALLOWED_WHEN_CARRYING 内 → 携人时执行它不构成 restricted 违规
    assert [v["rule"] for v in _violations(run)] == []


def test_restricted_action_while_carrying_still_flagged(tmp_path):
    """反向用例：携人时执行**不在**白名单内的动作仍必须判违规。

    保证上一个用例的"无违规"不是因为规则整体失效。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "carry_person",
                "ToolArgs": json.dumps({"person_id": "LostPersonTimmy"}),
                "Action": "CarryPerson(LostPersonTimmy)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 1},
                    action_desc="carry LostPersonTimmy",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps(
                    {"source_id": "Reservoir_1", "supply_type": "Water"}
                ),
                "Action": "GetSupply(Reservoir_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 1, "Person": 1},
                    action_desc="get supply from Reservoir_1",
                ),
            },
        ],
    )
    assert "restricted_action_violation" in [v["rule"] for v in _violations(run)]


# --------------------------------------------------------------------------
# 5. 脏 CSV 行 — 表头被重复写成数据行时不得崩掉整个 episode
# --------------------------------------------------------------------------


def _bad_rows(run_dir: Path) -> list[dict]:
    return [
        s
        for s in load_episode(run_dir).grader_skips
        if "unparseable data row" in s["reason"]
    ]


def _append_duplicate_header(path: Path) -> None:
    """把表头再追加一次作为数据行 —— 复刻实测的 16 个 run 的脏数据形态。"""
    with path.open(encoding="utf-8") as f:
        header = next(csv.reader(f))
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(header)


def _minimal_run(tmp_path: Path) -> Path:
    return _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
        ],
        steps=2,
    )


def test_clean_csv_loads_with_no_row_skips(tmp_path):
    """happy path 基线：干净 CSV 一行不丢、一条 skip 不多。

    与下面的脏行用例配对 —— 没有这条基线，"跳过"很容易退化成默默跳过一切
    （本仓有过回归测试静默降级成 skip 的历史）。
    """
    run = _minimal_run(tmp_path)
    ep = load_episode(run)
    assert _bad_rows(run) == []
    assert sorted(ep.steps.keys()) == [1, 2]
    assert ep.get_interaction(1, "Alice") is not None
    assert ep.steps[1].coverage == 0.0


def test_duplicated_header_row_in_trajectory_does_not_crash(tmp_path):
    """trajectory.csv 的表头被重复写成数据行时：不崩、脏行记进 grader_skips、
    其余行照常加载。

    未修复前 `int(row.get("Step", 0))` 直接抛
    `ValueError: invalid literal for int() with base 10: 'Step'`，
    一行脏数据废掉整个 run 的评估（实测 115 个 run 中 16 个如此）。
    """
    run = _minimal_run(tmp_path)
    _append_duplicate_header(run / "trajectory.csv")

    ep = load_episode(run)  # 未修复时此处抛 ValueError

    # 干净的两行照常加载
    assert sorted(ep.steps.keys()) == [1, 2]
    # 脏行**可见**：不是崩溃，也不是无痕消失
    bad = _bad_rows(run)
    assert len(bad) == 1
    assert "trajectory.csv:L4" in bad[0]["reason"]
    assert "Step='Step'" in bad[0]["reason"]
    assert bad[0]["grader"] == "dataset"


def test_duplicated_header_row_in_agent_interactions_does_not_crash(tmp_path):
    """agent_interactions.csv 同类脏行同样被加固（表头重复是 logger 层共性风险）。"""
    run = _minimal_run(tmp_path)
    _append_duplicate_header(run / "agent_interactions.csv")

    ep = load_episode(run)

    assert ep.get_interaction(1, "Alice") is not None
    bad = _bad_rows(run)
    assert len(bad) == 1
    assert "agent_interactions.csv:L3" in bad[0]["reason"]


def test_non_numeric_coverage_row_is_skipped_not_crashed(tmp_path):
    """Coverage/TransportRate 也走同一条 float 解析路径，同样不得让整体崩掉。"""
    run = _minimal_run(tmp_path)
    with (run / "trajectory.csv").open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRAJ_COLUMNS).writerow(
            {
                "Step": 3,
                "Actions": "[]",
                "Successes": "[]",
                "TimeoutAgents": "[]",
                "Coverage": "Coverage",  # 非数值
                "TransportRate": 0.0,
                "Finished": "False",
                "EndReason": "",
                "CompletedSubtasksDelta": "[]",
            }
        )
    ep = load_episode(run)
    assert sorted(ep.steps.keys()) == [1, 2]  # 脏的第 3 步被跳过
    bad = _bad_rows(run)
    assert len(bad) == 1
    assert "Coverage='Coverage'" in bad[0]["reason"]


def test_row_parse_guard_does_not_swallow_unrelated_errors(tmp_path):
    """守卫必须只吃字段类型错误，不能变成宽 except 把真 bug 一起吞掉。

    Actions/Successes 是坏 JSON 时，既有的 `_parse_csv_list` 语义是回退成空列表
    （而非记 skip）—— 这条钉住新加的 int/float 守卫没有把这类行也顺手跳过，
    否则解析层的真实缺陷会以"skip 数上升"的形式被掩盖。
    """
    run = _minimal_run(tmp_path)
    with (run / "trajectory.csv").open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRAJ_COLUMNS).writerow(
            {
                "Step": 3,
                "Actions": "not-a-list{{{",
                "Successes": "[]",
                "TimeoutAgents": "[]",
                "Coverage": 0.5,
                "TransportRate": 0.0,
                "Finished": "False",
                "EndReason": "",
                "CompletedSubtasksDelta": "[]",
            }
        )
    ep = load_episode(run)
    assert _bad_rows(run) == []  # 数值字段合法 → 不算脏行
    assert sorted(ep.steps.keys()) == [1, 2, 3]
    assert ep.steps[3].actions == []
    assert ep.steps[3].coverage == 0.5


# --------------------------------------------------------------------------
# 6. 幽灵动作 — 多行 (step,agent) 组的代表行自己就是异常行
#
# barrier 的 bug（对角 Move / None 导航目标 / 缺参 → `submit_action` 抛异常，
# 修复在 sar_orch/barrier.py，另一批次）触发时，"每组取首行"取到的可能是那次
# **没进到环境**的提交 —— 它只是提交顺序第一，不是第一个成功的。
# 本节钉住：这种行可被识别（`phantom_first_row`）、留痕（grader_skips）、
# 且已被判为确定失败；同时正常单行组的行为**完全不变**。
# --------------------------------------------------------------------------


def _phantom_skips(run_dir: Path) -> list[dict]:
    return [
        s
        for s in load_episode(run_dir).grader_skips
        if "never reached the environment" in s["reason"]
    ]


def test_phantom_first_row_is_flagged_and_counted_as_failure(tmp_path):
    """首行为异常行 + 同组后面还有行 → 标记 phantom、判 False、记 skip。

    实测 87 个 run 共 3 个这样的代表行。选择的 policy 是**标注**而非换行：
    首行为异常行的 14 个多行**环境动作**组里，每一行都是异常行，根本不存在
    "非异常的那一行"可换；拿 trajectory.csv 作真值核对，首行命中 3/14、
    末行 7/14、首个非异常行 0/14 —— 没有任何选择规则可靠到值得改动全部
    1809 个多行组的口径。故保留首行语义，改为让幽灵动作**可见**。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": None}),
                "Action": "NavigateTo(None)",
                "Observation": _REAL_ERROR_OBS_TRACEBACK,
            },
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "explore",
                "ToolArgs": "{}",
                "Action": "Explore()",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0}, action_desc="explore"
                ),
            },
        ],
    )
    ep = load_episode(run)
    ai = ep.get_interaction(1, "Alice")

    # 代表行语义不变（仍是首行）—— 口径没动
    assert ai.action == "NavigateTo(None)"
    # 但它现在可与"正常单动作步"区分开
    assert ai.error_observation is True
    assert ai.phantom_first_row is True
    assert ai.superseded_rows == 1
    # Defect 1：确定性失败，不是 unknown
    assert ai.succeeded is False
    # 绝不静默：降级留痕
    skips = _phantom_skips(run)
    assert len(skips) == 1
    assert "step=1" in skips[0]["reason"] and "agent=Alice" in skips[0]["reason"]


def test_single_row_group_is_unchanged(tmp_path):
    """回归护栏：正常单行组的行为**一个字节都不许变**。

    幽灵标记只针对"首行异常 + 同组还有后续行"。单行组即便本身是异常行，
    也不是幽灵（没有后续重试可言，它就是该 step 唯一的提交）。
    """
    run = _minimal_run(tmp_path)
    ai = load_episode(run).get_interaction(1, "Alice")
    assert ai.action == "NavigateTo(GreatFire_Region_1)"
    assert ai.succeeded is True
    assert ai.error_observation is False
    assert ai.phantom_first_row is False
    assert ai.superseded_rows == 0
    assert _phantom_skips(run) == []


def test_lone_error_row_is_failure_but_not_phantom(tmp_path):
    """单行组 + 该行是异常行 → 判 False，但**不**标 phantom、**不**记 skip。

    区分"这一步唯一的动作抛异常了"（确定失败，证据完整）与
    "代表行可能是幽灵"（证据有歧义）—— 两者不可混为一谈。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "move",
                "ToolArgs": json.dumps({"direction": "UpLeft"}),
                "Action": "Move(UpLeft)",
                "Observation": _REAL_ERROR_OBS_ASSERTION,
            },
        ],
    )
    ai = load_episode(run).get_interaction(1, "Alice")
    assert ai.succeeded is False
    assert ai.error_observation is True
    assert ai.phantom_first_row is False
    assert _phantom_skips(run) == []


def test_multirow_group_with_normal_first_row_is_not_flagged(tmp_path):
    """多行组但首行正常 → 不标 phantom（只记 superseded_rows）。

    与上面成对：phantom 的判定条件是"首行异常"**且**"有后续行"，
    两个条件缺一不可。多行本身不是问题（同 step 内查询/重试是常态）。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "explore",
                "ToolArgs": "{}",
                "Action": "Explore()",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0}, action_desc="explore"
                ),
            },
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "move",
                "ToolArgs": json.dumps({"direction": "UpLeft"}),
                "Action": "Move(UpLeft)",
                "Observation": _REAL_ERROR_OBS_ASSERTION,
            },
        ],
    )
    ai = load_episode(run).get_interaction(1, "Alice")
    assert ai.action == "Explore()"
    assert ai.succeeded is True
    assert ai.phantom_first_row is False
    assert ai.superseded_rows == 1
    assert _phantom_skips(run) == []


# --------------------------------------------------------------------------
# 7. visible_names_before 时序 — hallucinated_nav_target 的双向时序缺陷
#
# 与 inventory_before（第 2 节）同类同源：`visible_names` 出自**本行**的
# Observation，而 Observation 是环境在动作**执行之后**生成的。拿它判
# "agent 决定导航时看得见目标吗"两个方向都会错，实测 87 个 run：
#   · 误报 18 起 —— 成功走到目标，到达改变了周围可见集、目标自己掉出列表。
#   · 漏报 3 起  —— 目标动作前谁都看不见，到达/探索后才出现在列表里。
# 修法与 inventory_before 完全一致：按 agent 逐行 carry-forward 前序快照。
# --------------------------------------------------------------------------


def _explore_row(step: int, agent: str, names: tuple[str, ...]) -> dict:
    """一条只用来建立"动作前可见集"快照的 Explore 行。"""
    return {
        "Step": step,
        "Agent": agent,
        "ToolName": "explore",
        "ToolArgs": "{}",
        "Action": "Explore()",
        "Observation": _obs(
            {"Sand": 0, "Water": 0, "Person": 0},
            action_desc="explore",
            names=names,
        ),
    }


def _nav_row(
    step: int,
    agent: str,
    target: str,
    names_after: tuple[str, ...],
    *,
    success: bool = True,
) -> dict:
    return {
        "Step": step,
        "Agent": agent,
        "ToolName": "navigate_to",
        "ToolArgs": json.dumps({"target_id": target}),
        "Action": f"NavigateTo({target})",
        "Observation": _obs(
            {"Sand": 0, "Water": 0, "Person": 0},
            action_desc=f"navigate to {target}",
            success=success,
            names=names_after,
        ),
    }


def test_nav_to_target_visible_before_but_not_after_is_not_flagged(tmp_path):
    """目标动作**前**可见、到达后掉出列表 → 不得判违规（18 起误报的形态）。

    实测样本：run=20260719_124838_s1_s42_a2 L55 step=13 Alice
    NavigateTo(CaldorFire_Region_2)，succeeded=True，
    before 含 CaldorFire_Region_2、after 不含（到达火点改变了可见集）。
    """
    run = _write_run(
        tmp_path,
        [
            _explore_row(1, "Alice", ("CaldorFire_Region_2", "ReservoirUtah")),
            _nav_row(2, "Alice", "CaldorFire_Region_2", ("ReservoirUtah", "Bob")),
        ],
    )
    assert [v["rule"] for v in _violations(run)] == []


def test_nav_to_target_only_visible_after_is_flagged(tmp_path):
    """目标动作**前**不可见、只在动作后出现 → 必须判违规（3 起漏报的形态）。

    实测样本：run=20260719_190605_s2_s42_a2 L39/L40 step=10 Alice+Bob 同时
    NavigateTo(TownFire_Region_1)，两个 agent 的 before 都不含该目标、
    after 都含 —— 旧实现下两者一起逃脱指控。这里用同样的双 agent 结构，
    顺带钉住快照是**按 agent** carry-forward 的。
    """
    run = _write_run(
        tmp_path,
        [
            _explore_row(1, "Alice", ("TownFire_Region_2", "ReservoirOmaha")),
            _explore_row(1, "Bob", ("TownFire_Region_2", "ReservoirOmaha")),
            _nav_row(
                2,
                "Alice",
                "TownFire_Region_1",
                ("TownFire_Region_1", "TownFire_Region_2", "ReservoirOmaha"),
                success=False,
            ),
            _nav_row(
                2,
                "Bob",
                "TownFire_Region_1",
                ("TownFire_Region_1", "TownFire_Region_2", "ReservoirOmaha"),
                success=False,
            ),
        ],
        agent_names=("Alice", "Bob"),
    )
    flagged = [
        (v["rule"], v["agent"])
        for v in _violations(run)
        if v["rule"] == "hallucinated_nav_target"
    ]
    assert sorted(flagged) == [
        ("hallucinated_nav_target", "Alice"),
        ("hallucinated_nav_target", "Bob"),
    ]


def test_nav_target_never_visible_is_still_flagged(tmp_path):
    """回归护栏：目标动作前后**都**不可见 —— 规则原本就判对的情形不得被削弱。

    修时序缺陷不等于放宽规则。指控文本也一并钉住指向的是动作**前**的列表。
    """
    run = _write_run(
        tmp_path,
        [
            _explore_row(1, "Alice", ("TownFire_Region_2", "ReservoirOmaha")),
            _nav_row(
                2,
                "Alice",
                "GhostFire_Region_99",
                ("TownFire_Region_2", "ReservoirOmaha"),
                success=False,
            ),
        ],
    )
    hits = [v for v in _violations(run) if v["rule"] == "hallucinated_nav_target"]
    assert len(hits) == 1
    assert hits[0]["step"] == 2
    assert "GhostFire_Region_99" in hits[0]["detail"]
    assert "before the action" in hits[0]["detail"]


def test_nav_as_first_interaction_counts_parse_miss(tmp_path):
    """episode 首条交互无前序快照 → 不指控、记 parse_miss（不崩、不猜）。

    与 `test_first_interaction_without_prior_snapshot_counts_parse_miss`
    （inventory_before 版）同一套处理：无证据不构成指控。
    """
    run = _write_run(
        tmp_path,
        [
            _nav_row(1, "Alice", "CaldorFire_Region_2", ("CaldorFire_Region_2",)),
        ],
    )
    assert [v["rule"] for v in _violations(run)] == []
    assert _parse_miss(run)["names_before"] == 1


def test_visible_names_before_is_carried_from_preceding_row(tmp_path):
    """结构性断言：`visible_names_before` 就是**前一行**的 Names，
    `visible_names` 仍是本行的 —— 两者必须可区分。

    没有这条，上面的行为断言可能在"两个字段恰好相等"的巧合下通过。
    """
    run = _write_run(
        tmp_path,
        [
            _explore_row(1, "Alice", ("CaldorFire_Region_2", "ReservoirUtah")),
            _nav_row(2, "Alice", "CaldorFire_Region_2", ("ReservoirUtah", "Bob")),
        ],
    )
    ep = load_episode(run)
    first = ep.get_interaction(1, "Alice")
    nav = ep.get_interaction(2, "Alice")
    assert first.visible_names_before is None  # 首条交互无前序快照
    assert first.visible_names == ["CaldorFire_Region_2", "ReservoirUtah"]
    assert nav.visible_names_before == ["CaldorFire_Region_2", "ReservoirUtah"]
    assert nav.visible_names == ["ReservoirUtah", "Bob"]


def test_empty_prior_view_is_chargeable_not_a_parse_miss():
    """`None`（无从判定）与 `[]`（当时确实什么都看不见）不可混为一谈。

    bail 条件必须是 `is None` 而非真值判断 —— 否则"前序视野为空"这种
    **可指控**的证据会被静默 bail 掉，等于悄悄削弱规则。当前 CSV 解析路径
    不会产出 `[]` 快照（`_parse_observation` 把"无 Names 行"也收敛成 `[]`，
    故 carry-forward 用真值过滤），这里直接构造对象来钉住 grader 侧的语义。
    """
    from sar_orch.eval.dataset import AgentInteraction
    from sar_orch.eval.graders.constraint import _check_hallucinated_nav

    def check(before):
        ai = AgentInteraction(
            step=1,
            agent="Alice",
            tool_name="navigate_to",
            tool_args=json.dumps({"target_id": "CaldorFire_Region_2"}),
            action="NavigateTo(CaldorFire_Region_2)",
            observation="",
            llm_input="",
            llm_output="",
            thinking="",
            error_type="",
            tool_latency_ms="",
            action_name="NavigateTo",
            action_args=["CaldorFire_Region_2"],
            visible_names_before=before,
        )
        violations, miss = [], {}
        _check_hallucinated_nav(ai, violations, miss)
        return violations, miss

    v_none, miss_none = check(None)
    assert v_none == []
    assert miss_none["names_before"] == 1

    v_empty, miss_empty = check([])
    assert [x["rule"] for x in v_empty] == ["hallucinated_nav_target"]
    assert miss_empty == {}


# --------------------------------------------------------------------------
# 8. query-first 约束合同 — P1 Phase 0（P0.2）
#
# 同 (step, agent) 组内首行是查询、后续行才是环境 action 时，约束检查必须看
# 后续的环境 action attempt（P1.2.1：遍历 `sr.interactions` 的全部 SAR attempt），
# 不能因 `get_interaction()` 代表行是 query 而把空库存 UseSupply 漏掉。
# 同时 query 本身不是环境 attempt（P1.2.6），error-observation 的 UseSupply
# 不触发任何 constraint rule（P1.2.3），同 step 重复 attempt 先去重 step、
# 不得伪造跨 step repeat loop（P1.2.4）。
# --------------------------------------------------------------------------

_QUERY_ACTION = "map_agent__query_natural(Where is Reservoir_1?)"


def _overwrite_trajectory(run_dir: Path, traj_rows: list[dict]) -> None:
    """按测试需要重写 trajectory.csv（Successes/TimeoutAgents 默认值不够用时）。"""
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAJ_COLUMNS)
        w.writeheader()
        for r in traj_rows:
            w.writerow({c: r.get(c, "") for c in TRAJ_COLUMNS})


def _traj_row(step: int, actions: list[str], successes: list[bool]) -> dict:
    return {
        "Step": step,
        "Actions": json.dumps(actions),
        "Successes": json.dumps(list(successes)),
        "TimeoutAgents": "[]",
        "Coverage": 0.0,
        "TransportRate": 0.0,
        "Finished": "False",
        "EndReason": "",
        "CompletedSubtasksDelta": "[]",
    }


def _query_first_empty_supply_run(tmp_path: Path) -> Path:
    """step 1 建立空库存快照；step 2 首行是 query、后续是空库存 UseSupply。"""
    return _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "map_agent__query_natural",
                "ToolArgs": json.dumps({"question": "Where is Reservoir_1?"}),
                "Action": _QUERY_ACTION,
                "Observation": '{"answer": "Reservoir_1 is to the north"}',
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                    success=False,
                ),
            },
        ],
    )


def test_query_first_empty_supply_use_is_still_flagged(tmp_path):
    """query 代表行不得掩盖后续空库存 UseSupply：新合同恰有一个
    `empty_supply_use`，evidence 指向后续 UseSupply 行（L4）。

    当前行为（RED）：`get_interaction()` 代表行是 query，在白名单处被跳过，
    空库存 UseSupply 从未被检查 —— 0 条违规。修复后应恰有 1 条。
    """
    run = _query_first_empty_supply_run(tmp_path)
    violations = _violations(run)

    assert [v["rule"] for v in violations].count("empty_supply_use") == 1  # RED：当前 0
    hit = next(v for v in violations if v["rule"] == "empty_supply_use")
    assert hit["step"] == 2
    assert hit["agent"] == "Alice"
    assert hit["evidence_ref"] == "agent_interactions.csv:L4"
    # query 行本身零违规、零环境 attempt：没有任何违规指向 query action
    assert all(v["action"] != _QUERY_ACTION for v in violations)


def test_query_shadow_diagnostic_records_hidden_env_action_lines(tmp_path):
    """query-first 的 legacy 代表行必须暴露 query-shadow 诊断及隐藏环境 action
    CSV lines（P1.0.2/3），且该诊断不得改变 `get_interaction()` 的兼容返回值。

    当前行为（RED）：dataset 层没有任何 query_shadow 诊断。
    """
    run = _query_first_empty_supply_run(tmp_path)
    ep = load_episode(run)

    shadows = [s for s in ep.grader_skips if "query_shadow" in s["reason"]]
    assert len(shadows) == 1  # RED：当前 0
    assert "step=2" in shadows[0]["reason"]
    assert "agent=Alice" in shadows[0]["reason"]
    assert "agent_interactions.csv:L3" in shadows[0]["reason"]  # 首行 query line
    assert "agent_interactions.csv:L4" in shadows[0]["reason"]  # 隐藏环境 action line

    # 兼容代表行语义不变：仍是首行 query，且知道后面还有被藏起来的行
    ai = ep.get_interaction(2, "Alice")
    assert ai.action == _QUERY_ACTION
    assert ai.superseded_rows == 1


def test_error_observation_use_supply_triggers_no_constraint_rule(tmp_path):
    """`error_observation=True` 的 UseSupply 即使携带可解析的错误参数/前序空库存，
    也不得触发任何 constraint violation / repeat-failure / NoOp 分子分母
    （P1.2.3）：它的错误归因只属于 ErrorTaxonomy 的 tool-execution 桶。

    当前行为（RED）：error-observation 行仍走 `_check_empty_supply`，
    前序空库存把它误判成 `empty_supply_use`。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _REAL_ERROR_OBS_MISSING_ARG,
            },
        ],
    )
    assert _violations(run) == []  # RED：当前 1 条 empty_supply_use


def test_same_step_retries_do_not_fabricate_repeat_failure_loop(tmp_path):
    """同一 step 内多个失败 attempt 必须先去重 step（P1.2.4）：只有不同连续
    step 上发生的同一失败动作才可触发 `repeat_failure_loop`。

    这里 step 2 有两次失败 UseSupply、step 3 有一次 —— 去重后是 [2, 3]，
    不足 3 个连续 step，不得伪造跨 step loop。
    """
    fail_use = {
        "Step": 2,
        "Agent": "Alice",
        "ToolName": "use_supply",
        "ToolArgs": json.dumps({"fire_id": "GreatFire_Region_1", "supply_type": "Water"}),
        "Action": "UseSupply(GreatFire_Region_1, Water)",
        "Observation": _obs(
            {"Sand": 0, "Water": 0, "Person": 0},
            action_desc="use supply on GreatFire_Region_1",
            success=False,
        ),
    }
    run = _write_run(tmp_path, [fail_use, dict(fail_use), {**fail_use, "Step": 3}])
    _overwrite_trajectory(
        run,
        [
            _traj_row(1, ["NoOp"], [True]),
            _traj_row(2, ["UseSupply(GreatFire_Region_1, Water)"], [False]),
            _traj_row(3, ["UseSupply(GreatFire_Region_1, Water)"], [False]),
        ],
    )
    rules = [v["rule"] for v in _violations(run)]
    assert "repeat_failure_loop" not in rules


def test_constraint_detail_marks_environment_action_attempt_unit(tmp_path):
    """NoOp 比例等 action-level 统计的分子/分母基于环境 action attempt
    （P1.2.5），输出 detail 必须标明 `evaluation_unit: "environment_action_attempt"`。
    """
    run = _minimal_run(tmp_path)
    detail = grade_constraint(load_episode(run))[0].detail
    assert "evaluation_unit" in detail  # RED：当前无此键
    assert detail["evaluation_unit"] == "environment_action_attempt"
