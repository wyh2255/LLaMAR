"""rescue-level 完成口径：修 `drop_off_person` 原始成功率的统计缺陷。

背景（详见 `.agents/workspace/.plan/PROGRESS.md` T8a 复测记录）：
`drop_off_person` 是 cumulative 协议 —— 一个 person 要等全部 carrier 都调用
过才算救出，先到的 carrier 必须持续重试直到最后一个到位
（`sar_orch/skills/worker/person-rescue/SKILL.md`）。原口径把"逐次 tool call
是否成功"当作改进信号，于是协议内的合理重试被计成"失败"：重试轮数越多，
原始成功率越低，但那恰恰对应"协议被正确遵守"。实证：一批 `transport_rate`
全为 1.0（人全部救出）的数据，原始 `drop_off_person` 成功率只有 31.0%，
方向与 transport_rate 相反。

新口径改成"这次 rescue 最终是否完成"（该 person 的调用里只要有一次成功即算
完成），并把重试轮数单独产出。这里用真实的 `AgentInteraction` dataclass 和
一个最小 fake episode/step 容器（不依赖磁盘产物，不 grep 源码文本 —— 全部
断言调用真实函数、检查真实返回值）。
"""

from __future__ import annotations

from sar_orch.eval.dataset import AgentInteraction
from sar_orch.eval.graders.outcome import (
    check_rescue_transport_consistency,
    compute_rescue_outcomes,
    compute_rescue_summary,
)


def _drop_off(
    step: int,
    person_id: str,
    succeeded: bool | None,
    deposit_id: str = "DepositFacility",
    tool_args: str | None = None,
) -> AgentInteraction:
    """构造一次真实的 `drop_off_person` AgentInteraction。

    `tool_args` 默认按权威格式（原始 kwargs JSON）编码 person_id/deposit_id，
    与 `agent_interactions.csv` 里 ToolArgs 列的真实内容一致。
    """
    if tool_args is None:
        tool_args = f'{{"person_id": "{person_id}", "deposit_id": "{deposit_id}"}}'
    return AgentInteraction(
        step=step,
        agent="Alice",
        tool_name="drop_off_person",
        tool_args=tool_args,
        action=f"DropOffPerson({person_id}, {deposit_id})",
        observation="",
        llm_input="",
        llm_output="",
        thinking="",
        error_type="",
        tool_latency_ms="",
        action_name="DropOffPerson",
        action_args=[person_id, deposit_id],
        succeeded=succeeded,
    )


class _Step:
    def __init__(self, interactions):
        self.interactions = list(interactions)


class _Episode:
    """最小 fake episode：只提供 `compute_rescue_outcomes` 依赖的 `.steps`
    和 `check_rescue_transport_consistency` 依赖的 `.last_step`。"""

    def __init__(self, steps_by_num: dict[int, list[AgentInteraction]], transport_rate=None):
        self.steps = {n: _Step(ints) for n, ints in steps_by_num.items()}
        self._transport_rate = transport_rate

    @property
    def last_step(self):
        class _Last:
            transport_rate = self._transport_rate

        return _Last() if self.steps or self._transport_rate is not None else None


# ---------------------------------------------------------------------------
# cumulative 协议的正常情形
# ---------------------------------------------------------------------------


class TestCumulativeProtocolNormalCase:
    def test_multiple_calls_last_one_succeeds_counts_as_completed(self):
        """先到的 carrier 反复调用直到最后一个到位——协议内的合理重试，
        最后一次 succeeded=True 时该 person 应判为完成。"""
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", False)],
                2: [_drop_off(2, "Timmy", False)],
                3: [_drop_off(3, "Timmy", True)],
            }
        )
        outcomes = compute_rescue_outcomes(ep)
        assert outcomes["Timmy"]["completed"] is True
        assert outcomes["Timmy"]["attempts"] == 3

    def test_any_single_success_is_sufficient_even_amid_many_failures(self):
        """真实 run 里观测到 16 次调用只 1 次成功（早到的 carrier 反复重试）——
        只要出现过一次成功，该次 rescue 就算完成，不受调用总数影响。"""
        calls = [_drop_off(i, "Timmy", i == 12) for i in range(1, 17)]
        ep = _Episode({0: calls})
        outcomes = compute_rescue_outcomes(ep)
        assert outcomes["Timmy"]["completed"] is True
        assert outcomes["Timmy"]["attempts"] == 16
        assert outcomes["Timmy"]["succeeded_attempts"] == 1
        assert outcomes["Timmy"]["failed_attempts"] == 15


# ---------------------------------------------------------------------------
# 真实失败情形 —— 防止"修口径"滑成"什么都算成功"
# ---------------------------------------------------------------------------


class TestGenuineFailureIsNotMaskedAsSuccess:
    def test_all_failed_calls_count_as_not_completed(self):
        """全部调用都 succeeded=False → 必须判未完成。这是防止新口径矫枉过正、
        把"没有一次成功过"也吞掉的关键断言。"""
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", False)],
                2: [_drop_off(2, "Timmy", False)],
                3: [_drop_off(3, "Timmy", False)],
            }
        )
        outcomes = compute_rescue_outcomes(ep)
        assert outcomes["Timmy"]["completed"] is False
        assert outcomes["Timmy"]["attempts"] == 3
        assert outcomes["Timmy"]["succeeded_attempts"] == 0

    def test_failed_rescue_is_excluded_from_completion_rate_numerator(self):
        summary = compute_rescue_summary(
            _Episode({1: [_drop_off(1, "Timmy", False)]})
        )
        assert summary["persons_completed"] == 0
        assert summary["persons_failed"] == 1
        assert summary["completion_rate"] == 0.0

    def test_mixed_persons_one_completed_one_failed(self):
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", True), _drop_off(1, "Jeremy", False)],
                2: [_drop_off(2, "Jeremy", False)],
            }
        )
        outcomes = compute_rescue_outcomes(ep)
        assert outcomes["Timmy"]["completed"] is True
        assert outcomes["Jeremy"]["completed"] is False
        summary = compute_rescue_summary(ep)
        assert summary["persons_total"] == 2
        assert summary["persons_completed"] == 1
        assert summary["persons_failed"] == 1
        assert summary["completion_rate"] == 0.5


# ---------------------------------------------------------------------------
# succeeded=None 的三态处理
# ---------------------------------------------------------------------------


class TestThreeStateHandling:
    def test_all_unknown_calls_yield_none_not_false(self):
        """全部调用都无法判定（succeeded=None）时，该 person 应判 None
        （判不出来），不能当作 False（失败）—— 与 compute_tool_outcomes 的
        unknown 单独计数是同一原则。"""
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", None)],
                2: [_drop_off(2, "Timmy", None)],
            }
        )
        outcomes = compute_rescue_outcomes(ep)
        assert outcomes["Timmy"]["completed"] is None
        assert outcomes["Timmy"]["unknown_attempts"] == 2

    def test_one_success_amid_unknowns_still_completes(self):
        """已确认的成功不应被后续/先前无法判定的调用推翻。"""
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", None)],
                2: [_drop_off(2, "Timmy", True)],
                3: [_drop_off(3, "Timmy", None)],
            }
        )
        outcomes = compute_rescue_outcomes(ep)
        assert outcomes["Timmy"]["completed"] is True

    def test_unknown_persons_are_excluded_from_completion_rate_denominator(self):
        """全 unknown 的 person 既不计入分子也不计入分母 —— 不能拉高也不能
        拉低 completion_rate。"""
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", True)],
                2: [_drop_off(2, "Jeremy", None)],
            }
        )
        summary = compute_rescue_summary(ep)
        assert summary["persons_total"] == 2
        assert summary["persons_unknown"] == 1
        # 分母只有 Timmy 一个可判定的 person，Jeremy 不计入。
        assert summary["completion_rate"] == 1.0

    def test_no_determined_persons_yields_none_completion_rate(self):
        """一个可判定的 person 都没有时，completion_rate 必须是 None
        （无法给出比率），不能是 0.0（那会被误读成"全部失败"）。"""
        ep = _Episode({1: [_drop_off(1, "Timmy", None)]})
        summary = compute_rescue_summary(ep)
        assert summary["completion_rate"] is None


# ---------------------------------------------------------------------------
# 重试轮数计算
# ---------------------------------------------------------------------------


class TestRetryRoundsCounting:
    def test_retry_rounds_equals_call_count_per_person(self):
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", False), _drop_off(1, "Jeremy", True)],
                2: [_drop_off(2, "Timmy", False)],
                3: [_drop_off(3, "Timmy", True)],
            }
        )
        outcomes = compute_rescue_outcomes(ep)
        assert outcomes["Timmy"]["attempts"] == 3
        assert outcomes["Jeremy"]["attempts"] == 1

    def test_retry_rounds_are_reported_per_person_in_summary(self):
        """这是被原口径混淆掉的变量：必须在 summary 里独立可见，
        不能只剩一个压缩后的完成率。"""
        ep = _Episode(
            {
                1: [_drop_off(1, "Timmy", False)],
                2: [_drop_off(2, "Timmy", False)],
                3: [_drop_off(3, "Timmy", True)],
            }
        )
        summary = compute_rescue_summary(ep)
        assert summary["retry_rounds_per_person"] == [3]
        assert summary["retry_rounds_mean"] == 3.0

    def test_high_retry_low_raw_rate_still_completes(self):
        """核心场景：16 次调用只 1 次成功（原始成功率 6.25%），但依然是
        完整的一次 rescue —— 证明新口径与调用轮数解耦，不会被重试拖累。"""
        calls = [_drop_off(i, "Timmy", i == 16) for i in range(1, 17)]
        ep = _Episode({0: calls})
        summary = compute_rescue_summary(ep)
        assert summary["completion_rate"] == 1.0
        assert summary["retry_rounds_mean"] == 16.0


# ---------------------------------------------------------------------------
# 无 rescue 事件时字段缺席而非记 0
# ---------------------------------------------------------------------------


class TestAbsentRescueEventIsAbsentNotZero:
    def test_no_drop_off_calls_yields_empty_outcomes_mapping(self):
        ep = _Episode({1: []})
        assert compute_rescue_outcomes(ep) == {}

    def test_no_drop_off_calls_yields_none_summary_not_zero(self):
        """该 run 应被上游排除出跨 run 统计，而非记 0（"没有尝试"与
        "尝试了但零成功"是两种状态）。"""
        ep = _Episode({1: []})
        assert compute_rescue_summary(ep) is None

    def test_empty_episode_yields_empty_mapping(self):
        ep = _Episode({})
        assert compute_rescue_outcomes(ep) == {}
        assert compute_rescue_summary(ep) is None

    def test_consistency_check_reports_unchecked_when_no_rescue_event(self):
        ep = _Episode({1: []}, transport_rate=0.73)
        result = check_rescue_transport_consistency(ep)
        assert result["checked"] is False
        assert result["consistent"] is None
        assert result["rescue_completion_rate"] is None
        assert result["transport_rate"] == 0.73


# ---------------------------------------------------------------------------
# rescue-level 完成数与 transport_rate 的交叉核对
# ---------------------------------------------------------------------------


class TestRescueTransportCrossCheck:
    def test_full_transport_and_full_rescue_is_consistent(self):
        ep = _Episode({1: [_drop_off(1, "Timmy", True)]}, transport_rate=1.0)
        result = check_rescue_transport_consistency(ep)
        assert result["checked"] is True
        assert result["consistent"] is True

    def test_full_transport_but_incomplete_rescue_is_flagged_mismatch(self):
        """transport_rate=1.0 但 rescue completion_rate<1.0 是方向冲突，
        必须被标记出来供人工核查，而不是被 check_rescue_transport_consistency
        悄悄放过。"""
        ep = _Episode({1: [_drop_off(1, "Timmy", False)]}, transport_rate=1.0)
        result = check_rescue_transport_consistency(ep)
        assert result["checked"] is True
        assert result["consistent"] is False

    def test_partial_transport_does_not_force_strict_check(self):
        """transport_rate 未满值时两个指标分母不保证同源，不应苛求相等——
        此时应放行（consistent=True）而不是误报冲突。"""
        ep = _Episode({1: [_drop_off(1, "Timmy", False)]}, transport_rate=0.5)
        result = check_rescue_transport_consistency(ep)
        assert result["checked"] is True
        assert result["consistent"] is True

    def test_unresolvable_rescue_rate_is_not_checked(self):
        """completion_rate 为 None（全 unknown）时无法核对，应报
        checked=False 而不是强行判定一致/不一致。"""
        ep = _Episode({1: [_drop_off(1, "Timmy", None)]}, transport_rate=1.0)
        result = check_rescue_transport_consistency(ep)
        assert result["checked"] is False
        assert result["consistent"] is None


# ---------------------------------------------------------------------------
# person_id 提取：优先 tool_args，action_args 仅兜底
# ---------------------------------------------------------------------------


class TestPersonIdExtractionPrefersToolArgs:
    def test_extracts_person_id_from_tool_args_even_when_action_args_reordered(self):
        """T4 的教训：action_args 可能被 `_canonical_action_args` 按声明顺序
        重排过，tool_args 才是原始 kwargs JSON、权威来源。这里构造一个
        action_args 顺序与 tool_args 键序不同的场景，确认取的是 tool_args
        里的 person_id 而不是误取 action_args 的某个位置。"""
        ai = _drop_off(
            1,
            "Timmy",
            True,
            tool_args='{"deposit_id": "DepositFacility", "person_id": "Timmy"}',
        )
        # 故意让 action_args 顺序与 person_id 不对应（模拟未重排的原始渲染）。
        ai.action_args = ["DepositFacility", "Timmy"]
        ep = _Episode({1: [ai]})
        outcomes = compute_rescue_outcomes(ep)
        assert "Timmy" in outcomes
        assert "DepositFacility" not in outcomes

    def test_falls_back_to_action_args_when_tool_args_unparseable(self):
        ai = _drop_off(1, "Timmy", True, tool_args="not valid json")
        ai.action_args = ["Timmy", "DepositFacility"]
        ep = _Episode({1: [ai]})
        outcomes = compute_rescue_outcomes(ep)
        assert "Timmy" in outcomes

    def test_missing_person_id_is_bucketed_not_dropped(self):
        """既不能计入某个 person，也不能悄悄丢弃——归入占位桶让缺口可见。"""
        ai = _drop_off(1, "Timmy", True, tool_args="{}")
        ai.action_args = []
        ep = _Episode({1: [ai]})
        outcomes = compute_rescue_outcomes(ep)
        assert "unknown_person" in outcomes
        assert outcomes["unknown_person"]["attempts"] == 1


# ---------------------------------------------------------------------------
# 与其它工具调用共存：不应误纳入非 drop_off_person 交互
# ---------------------------------------------------------------------------


class TestIgnoresNonDropOffInteractions:
    def test_other_tool_calls_do_not_pollute_rescue_outcomes(self):
        other = AgentInteraction(
            step=1,
            agent="Bob",
            tool_name="navigate_to",
            tool_args="{}",
            action="NavigateTo(DepositFacility)",
            observation="",
            llm_input="",
            llm_output="",
            thinking="",
            error_type="",
            tool_latency_ms="",
            succeeded=True,
        )
        ep = _Episode({1: [other, _drop_off(1, "Timmy", True)]})
        outcomes = compute_rescue_outcomes(ep)
        assert list(outcomes) == ["Timmy"]
