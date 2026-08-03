"""T5: balance/judge 出门禁、TR 正名、idle_ratio 新增、效率类入门禁。

这批改动里有两处属 DESIGN P5 定义的「改判据」（移除门禁项），已获显式批准。
判据放松必须可复检，所以这里既钉住"被移除的确实不再判定"，也钉住"移除的
理由被记录下来"—— 后者防的是几轮之后有人不明所以地把它加回去。

同时钉住配套的**收紧**项：效率类指标接入门禁。净效果必须是门禁更严而非更松，
否则"移除两个方向错误的指标"就变成了单纯放水。
"""

from __future__ import annotations

import pytest

from sar_orch.eval import gate
from sar_orch.eval.graders.outcome import compute_idle_ratio


# ---------------------------------------------------------------------------
# 门禁构成
# ---------------------------------------------------------------------------


class TestDemotions:
    @pytest.mark.parametrize(
        "metric", ["balance_mean", "dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_demoted_metrics_are_out_of_regression(self, metric):
        assert metric not in gate.DEFAULT_CONFIG["regression"]

    @pytest.mark.parametrize(
        "metric", ["balance_mean", "dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_demoted_metrics_remain_reportable(self, metric):
        """降级 != 删除。原值必须继续出现在报告里（论文可比性 + 诊断价值）。"""
        assert metric in gate.METRICS_BY_KEY

    @pytest.mark.parametrize(
        "metric", ["balance_mean", "dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_every_demotion_records_its_reason(self, metric):
        """依据必须留在代码里。"为什么不看这个指标"比"看哪些指标"更容易
        在几轮之后被遗忘，然后被误加回去。"""
        assert metric in gate.DEMOTED_METRICS
        assert len(gate.DEMOTED_METRICS[metric]) > 40

    def test_balance_regression_no_longer_blocks(self):
        """降级依据：min/max 结构性惩罚角色分工（低值可能恰是好协作）。
        注意**不是**"与成功反相关" —— 那个说法据「失败 0.841 > 成功 0.805」
        而来，但补做 Mann-Whitney U 后 p=0.485、效应量 +0.021，未达显著。
        大幅"退化"不应再触发 block。"""
        result = gate.evaluate_gate(
            current={
                "groups": [
                    {
                        "key": {"scene": 1, "agents": 2},
                        "n": 3,
                        "finished_count": 2,
                        "pass_at_k": {"1": 0.667},
                        "episode_stats": {
                            "coverage": {"mean": 0.9},
                            "transport_rate": {"mean": 0.8},
                            "balance": {"mean": 0.10},  # 从 0.9 崩到 0.10
                        },
                        "constraint_violations": {"per_run_violations": 1.0},
                    }
                ]
            },
            baseline={
                "groups": [
                    {
                        "key": {"scene": 1, "agents": 2},
                        "n": 3,
                        "finished_count": 2,
                        "pass_at_k": {"1": 0.667},
                        "episode_stats": {
                            "coverage": {"mean": 0.9},
                            "transport_rate": {"mean": 0.8},
                            "balance": {"mean": 0.90},
                        },
                        "constraint_violations": {"per_run_violations": 1.0},
                    }
                ]
            },
            config=gate.load_config(None),
        )
        assert result.passed
        assert not [c for c in result.checks if c.metric == "balance_mean"]


class TestEfficiencyTightening:
    def test_token_regression_is_now_gated(self):
        assert "total_tokens_mean" in gate.DEFAULT_CONFIG["regression"]

    @pytest.mark.parametrize(
        "metric", ["total_tokens_mean", "step_efficiency_mean", "idle_ratio_mean"]
    )
    def test_efficiency_metrics_are_extractable(self, metric):
        """在 METRICS 里声明但 extract_metrics 不产出 = 永远 skip，等于没加。"""
        got = gate.extract_metrics(
            {
                "key": {"scene": 1, "agents": 2},
                "n": 2,
                "finished_count": 1,
                "episode_stats": {
                    "total_tokens": {"mean": 900000.0},
                    "step_efficiency": {"mean": 0.45},
                    "idle_ratio": {"mean": 0.12},
                },
            }
        )
        assert got[metric] is not None

    def _groups(self, tokens):
        return {
            "groups": [
                {
                    "key": {"scene": 1, "agents": 2},
                    "n": 3,
                    "finished_count": 2,
                    "pass_at_k": {"1": 0.667},
                    "episode_stats": {
                        "coverage": {"mean": 0.9},
                        "transport_rate": {"mean": 0.8},
                        "total_tokens": {"mean": tokens},
                    },
                    "constraint_violations": {"per_run_violations": 1.0},
                }
            ]
        }

    def test_token_blowup_now_blocks(self):
        """这正是先前完全不被察觉的情形：完成率/覆盖/违规全部不变，
        但 token 大涨 —— 自进化回路会有滑向"更慢但一样能过"的动机。"""
        result = gate.evaluate_gate(
            current=self._groups(1_500_000.0),
            baseline=self._groups(1_000_000.0),
            config=gate.load_config(None),
        )
        assert not result.passed
        fails = [c for c in result.failures if c.metric == "total_tokens_mean"]
        assert fails, [c.metric for c in result.failures]

    def test_token_within_relative_tolerance_passes(self):
        result = gate.evaluate_gate(
            current=self._groups(1_150_000.0),  # +15%, 阈值 20%
            baseline=self._groups(1_000_000.0),
            config=gate.load_config(None),
        )
        assert result.passed

    def test_token_reduction_never_blocks(self):
        result = gate.evaluate_gate(
            current=self._groups(400_000.0),
            baseline=self._groups(1_000_000.0),
            config=gate.load_config(None),
        )
        assert result.passed


class TestRelativeTolerance:
    def test_relative_scales_with_baseline(self):
        """相对容差的意义：token 量级跨场景差一个数量级，绝对阈值在小场景上
        永不触发、在大场景上永远触发。"""
        small, _ = gate._resolve_tolerance({"relative": 0.2}, 1000.0)
        large, _ = gate._resolve_tolerance({"relative": 0.2}, 1_000_000.0)
        assert small == pytest.approx(200.0)
        assert large == pytest.approx(200_000.0)

    def test_absolute_form_still_works(self):
        tol, kind = gate._resolve_tolerance(0.15, 0.8)
        assert tol == pytest.approx(0.15)
        assert kind == "absolute"

    @pytest.mark.parametrize("baseline", [0.0, -5.0, None])
    def test_nonpositive_baseline_skips_instead_of_failing(self, baseline):
        """基线为 0 时 20% 还是 0 —— 任何增长都会判 fail，那是假 fail 不是退化。"""
        tol, reason = gate._resolve_tolerance({"relative": 0.2}, baseline)
        assert tol is None
        assert "baseline" in reason

    def test_relative_without_key_is_rejected_loudly(self):
        with pytest.raises(ValueError):
            gate._resolve_tolerance({"absolute": 0.2}, 100.0)


# ---------------------------------------------------------------------------
# idle_ratio
# ---------------------------------------------------------------------------


class _FakeStep:
    def __init__(self, actions, successes):
        self.actions = actions
        self.successes = successes


class _FakeEpisode:
    def __init__(self, steps):
        self.steps = dict(enumerate(steps))
        self.metadata = {"agent_count": 2}
        self.agent_names = ["Alice", "Bob"]


class TestIdleRatio:
    def test_all_successful_non_noop_is_zero(self):
        ep = _FakeEpisode([_FakeStep(["NavigateTo(x)", "GetSupply(y)"], [True, True])])
        assert compute_idle_ratio(ep) == 0.0

    def test_noop_counts_as_waste(self):
        ep = _FakeEpisode([_FakeStep(["NoOp()", "GetSupply(y)"], [True, True])])
        assert compute_idle_ratio(ep) == pytest.approx(0.5)

    def test_failed_action_counts_as_waste(self):
        ep = _FakeEpisode([_FakeStep(["NavigateTo(x)", "GetSupply(y)"], [False, True])])
        assert compute_idle_ratio(ep) == pytest.approx(0.5)

    def test_does_not_penalise_division_of_labour(self):
        """与 balance 的关键差别：两个 agent 各干各的、产出悬殊，但每一步都有效
        —— balance 会给出接近 0 的差评，idle_ratio 正确地给 0。"""
        steps = [_FakeStep(["NavigateTo(fire)", "NoOp()"], [True, True])] * 1
        busy = _FakeEpisode(
            [_FakeStep(["UseSupply(a, Water)", "Carry(p)"], [True, True])] * 5
        )
        assert compute_idle_ratio(busy) == 0.0
        # 对照：有空动作时才算浪费
        assert compute_idle_ratio(_FakeEpisode(steps)) == pytest.approx(0.5)

    def test_no_actions_returns_none_not_zero(self):
        """"没有浪费"与"没有数据"是两种状态；合并会让空 run 看起来完美。"""
        assert compute_idle_ratio(_FakeEpisode([])) is None
        assert compute_idle_ratio(_FakeEpisode([_FakeStep([], [])])) is None

    def test_length_mismatch_does_not_invent_failures(self):
        """actions/successes 长度不一致是数据损坏。宁可少算也不要用 None 冒充
        失败 —— 那会把解析问题伪装成行为问题。"""
        ep = _FakeEpisode([_FakeStep(["A", "B", "C"], [True])])
        assert compute_idle_ratio(ep) == 0.0


# ---------------------------------------------------------------------------
# TR 正名
# ---------------------------------------------------------------------------


class TestTransportRateRenaming:
    def test_new_name_is_emitted_alongside_old(self):
        """两个字段必须同值：旧名保论文可比与向后兼容，新名传达正确语义。

        直接调 grade_outcome 而不是去读磁盘上的报告 —— 早先那版扫
        `sar_orch/results/*/eval_report_verify.json`，那是离线复算的临时产物，
        清理掉之后测试就静默 skip 了。覆盖率会随一个 scratch 文件消失的断言
        不算断言。
        """
        from sar_orch.eval.graders.outcome import grade_outcome

        class _Step:
            def __init__(self):
                self.actions = ["NavigateTo(x)", "GetSupply(y)"]
                self.successes = [True, True]
                self.coverage = 1.0
                self.transport_rate = 0.75
                self.finished = True
                self.end_reason = "task_complete"
                self.completed_subtasks_delta = ["a", "b", "c"]
                self.interactions = []
                self.dispatches = []
                self.subtasks = []
                self.step = 1
                self.timeout_agents = []

        class _Episode:
            def __init__(self):
                self.steps = {1: _Step()}
                self.metadata = {"agent_count": 2}
                self.agent_names = ["Alice", "Bob"]
                self.summary = {}
                self.token_usage_rows = []
                self.dispatches = []
                self.subtask_records = []

            @property
            def last_step(self):
                return self.steps[1]

        detail = next(
            r.detail for r in grade_outcome(_Episode()) if r.grader == "OutcomeGrader"
        )
        assert detail["subtask_completion_rate"] == detail["final_transport_rate"]
        assert detail["subtask_completion_rate"] == pytest.approx(0.75)
        assert "idle_ratio" in detail

    def test_report_annotates_the_misleading_name(self):
        """名字本身没法改（论文可比 + 向后兼容），所以报告里必须写清它其实
        是什么，否则读者会按字面理解成"运输率"。"""
        from pathlib import Path

        src = Path("sar_orch/eval/report.py").read_text(encoding="utf-8")
        assert "checker 子任务完成率" in src
        assert "去重" in src

    def test_report_marks_balance_as_diagnostic_only(self):
        from pathlib import Path

        src = Path("sar_orch/eval/report.py").read_text(encoding="utf-8")
        assert "诊断量" in src
        assert "不作优化目标" in src
