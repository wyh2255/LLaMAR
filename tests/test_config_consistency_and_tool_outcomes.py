"""T6: 配置一致性检查 + 逐工具行为计数（E-24 双份解析的根治）。

两件事在这里绑到一起，因为它们服务同一个判断：**这批数据能不能池化成一个数字**。

- **配置一致性**：LLM 配置已定为恒定量（用户决策 2026-08-02），故批内配置漂移
  是**污染**而非变量。两个不同 model 的 run 池化进同一个 CI，测出的"方差"里
  混着配置差异，而报告上完全看不出来。这条判 **fail** 而非 warn —— 只 warn
  的话，混批数据仍会以"通过"收场。
- **逐工具计数**：`compare_cells.py` 先前自己解析 CSV、用字符串匹配数成功失败，
  与 `eval/dataset.py` 各有一份"成功"判定实现（E-24）。环境改一次措辞就会两边
  静默不一致。现在统一由 `OutcomeGrader` 产出。

这些计数不是可选的诊断：DESIGN 3.1b 已论证 pass@1 在任何可负担样本量下只能
分辨 ≥33 pp，故**改进证据一律靠它们**。
"""

from __future__ import annotations

import pytest

from sar_orch.eval import gate
from sar_orch.eval.aggregate import (
    CONSISTENCY_FIELDS,
    aggregate_group,
    check_config_consistency,
)
from sar_orch.eval.graders.outcome import (
    compute_timeout_steps,
    compute_tool_outcomes,
)

BASE_META = dict(
    scene=1,
    agents=2,
    seed=42,
    model="deepseek-v4-flash",
    provider="openai",
    api_base="https://gw/v1",
    temperature=0.7,
    llm_seed_supported=False,
    prompt_hash="abc123",
    state_mode="semantic",
    max_steps=30,
)


def _report(run="r", **meta):
    m = dict(BASE_META)
    m.update(meta)
    return {
        "run_dir": run,
        "metadata": m,
        "episode": {
            "finished": True,
            "coverage": 0.9,
            "transport_rate": 0.8,
            "total_tokens": 900000,
            "steps": 30,
        },
    }


# ---------------------------------------------------------------------------
# 配置一致性
# ---------------------------------------------------------------------------


class TestConfigConsistency:
    def test_identical_config_is_poolable(self):
        assert check_config_consistency([_report("a"), _report("b")]) == []

    def test_scene_seed_may_vary_by_design(self):
        """`group_by_key` 只按 (scene, agents) 分组，seed 被池化 —— 这是有意的。
        若把 seed 也算进一致性字段，每一组都会被判不可池化。"""
        assert "seed" not in CONSISTENCY_FIELDS
        assert check_config_consistency([_report("a", seed=42), _report("b", seed=43)]) == []

    @pytest.mark.parametrize(
        "field,other",
        [
            ("model", "gpt-4o"),
            ("provider", "anthropic"),
            ("api_base", "https://other/v1"),
            ("temperature", 0.2),
            ("prompt_hash", "def456"),
            ("state_mode", "something_else"),
            ("max_steps", 60),
            ("llm_seed_supported", True),
        ],
    )
    def test_each_tracked_field_is_detected(self, field, other):
        issues = check_config_consistency([_report("a"), _report("b", **{field: other})])
        assert [i["field"] for i in issues] == [field]
        assert issues[0]["partial_record_only"] is False

    def test_missing_field_on_some_runs_is_flagged_as_partial(self):
        """旧 run 没记这个字段，与"两个不同取值"是不同的问题：前者是数据缺失
        （warn），后者是真的配置漂移（fail）。合并会让旧数据无法参与任何比较。"""
        issues = check_config_consistency([_report("a"), _report("b", model=None)])
        assert issues[0]["partial_record_only"] is True

    def test_field_absent_from_every_run_is_not_an_inconsistency(self):
        """整组都没这个字段 → 不是不一致，是这批数据早于该字段存在。"""
        reports = [_report("a", prompt_hash=None), _report("b", prompt_hash=None)]
        assert [i["field"] for i in check_config_consistency(reports)] == []

    def test_offending_runs_are_named(self):
        """报告必须说出是哪些 run —— 否则人得自己去翻 20 个 metadata。"""
        issues = check_config_consistency(
            [_report("run_a"), _report("run_b", model="gpt-4o")]
        )
        vals = issues[0]["values"]
        assert vals["deepseek-v4-flash"] == ["run_a"]
        assert vals["gpt-4o"] == ["run_b"]


class TestAggregateExposesPoolability:
    def test_consistent_group_is_marked_poolable(self):
        g = aggregate_group([_report("a"), _report("b", seed=43)])
        assert g["poolable"] is True
        assert g["config_issues"] == []

    def test_drifting_group_is_marked_not_poolable(self):
        g = aggregate_group([_report("a"), _report("b", model="gpt-4o")])
        assert g["poolable"] is False

    def test_config_is_recorded_in_the_report(self):
        """让"这批用的什么配置"成为报告里可读的事实，而不是要去翻某个 run。"""
        g = aggregate_group([_report("a")])
        assert g["config"]["model"] == "deepseek-v4-flash"
        assert set(g["config"]) == set(CONSISTENCY_FIELDS)


class TestGateEnforcesPoolability:
    def _verdict(self, reports):
        g = aggregate_group(reports)
        r = gate.evaluate_gate(
            current={"groups": [g]}, baseline=None, config=gate.load_config(None)
        )
        cfg = [c for c in r.checks if c.metric.startswith("config:")]
        return r, cfg

    def test_drift_fails_the_gate(self):
        """判 fail 而非 warn 是关键：只 warn 的话混批数据仍会以"通过"收场，
        而它的 CI 已经不表示任何单一配置下的性能。"""
        r, cfg = self._verdict([_report("a"), _report("b", model="gpt-4o")])
        assert r.passed is False
        assert [c.status for c in cfg] == ["fail"]

    def test_partial_record_only_warns(self):
        r, cfg = self._verdict([_report("a"), _report("b", temperature=None)])
        assert r.passed is True
        assert [c.status for c in cfg] == ["warn"]

    def test_consistent_group_adds_no_config_checks(self):
        r, cfg = self._verdict([_report("a"), _report("b", seed=43)])
        assert cfg == []

    def test_underpowered_group_does_not_downgrade_config_failure(self):
        """n < min_runs(=2) 会把 absolute/regression 的 fail 降级为 warn。
        配置漂移**不能**跟着降级 —— 混了两个 model 的数据不可池化，与样本量无关。

        用单 run 组构造 underpowered，同时让它带上"部分缺记录之外"的真实漂移：
        单 run 内不可能有漂移，故手工塞一个 config_issues 来模拟聚合结果。
        """
        g = aggregate_group([_report("a")])
        assert g["n"] == 1  # 触发 underpowered 路径
        g["config_issues"] = [
            {"field": "model", "values": {"x": ["a"], "y": ["b"]}, "partial_record_only": False}
        ]
        r = gate.evaluate_gate(
            current={"groups": [g]}, baseline=None, config=gate.load_config(None)
        )
        assert any(c.check == "meta" and c.metric == "min_runs" for c in r.checks)
        cfg = [c for c in r.checks if c.metric.startswith("config:")]
        assert [c.status for c in cfg] == ["fail"]
        assert r.passed is False


# ---------------------------------------------------------------------------
# 逐工具计数
# ---------------------------------------------------------------------------


class _AI:
    def __init__(self, tool, succeeded):
        self.tool_name = tool
        self.succeeded = succeeded


class _Step:
    def __init__(self, interactions=(), timeout_agents=()):
        self.interactions = list(interactions)
        self.timeout_agents = list(timeout_agents)


class _Ep:
    def __init__(self, steps):
        self.steps = dict(enumerate(steps))


class TestToolOutcomes:
    def test_counts_split_three_ways(self):
        ep = _Ep([_Step([
            _AI("use_supply", True), _AI("use_supply", False), _AI("use_supply", None),
        ])])
        assert compute_tool_outcomes(ep)["use_supply"] == {
            "attempts": 3, "succeeded": 1, "failed": 1, "unknown": 1
        }

    def test_unknown_is_not_folded_into_failed(self):
        """"判不出来"与"判定为失败"是两种状态。合并会让解析退化伪装成行为退化
        —— 那正是 E-2 造成 65 起误报的形状。"""
        ep = _Ep([_Step([_AI("navigate_to", None), _AI("navigate_to", None)])])
        rec = compute_tool_outcomes(ep)["navigate_to"]
        assert rec["failed"] == 0
        assert rec["unknown"] == 2

    def test_attempts_always_equals_the_sum(self):
        ep = _Ep([_Step([_AI("a", True), _AI("a", False), _AI("b", None)])])
        for rec in compute_tool_outcomes(ep).values():
            assert rec["attempts"] == rec["succeeded"] + rec["failed"] + rec["unknown"]

    def test_absent_tool_is_absent_not_zero(self):
        """先前的实现对"从未调用"和"调用过但零成功"都报 0，两者无法区分 ——
        而它们对行为分析的含义完全不同。"""
        ep = _Ep([_Step([_AI("use_supply", True)])])
        assert "drop_off_person" not in compute_tool_outcomes(ep)

    def test_empty_episode_yields_empty_mapping(self):
        assert compute_tool_outcomes(_Ep([])) == {}

    def test_missing_tool_name_is_bucketed_not_dropped(self):
        ep = _Ep([_Step([_AI("", True)])])
        assert compute_tool_outcomes(ep)["unknown"]["attempts"] == 1

    def test_tools_are_sorted_for_stable_diffing(self):
        ep = _Ep([_Step([_AI("zeta", True), _AI("alpha", True)])])
        assert list(compute_tool_outcomes(ep)) == ["alpha", "zeta"]


class TestTimeoutSteps:
    def test_counts_steps_with_any_timeout(self):
        ep = _Ep([_Step(timeout_agents=[0]), _Step(), _Step(timeout_agents=[0, 1])])
        assert compute_timeout_steps(ep) == 2

    def test_zero_when_no_timeouts(self):
        assert compute_timeout_steps(_Ep([_Step(), _Step()])) == 0


class TestDoubleParseIsGone:
    def test_compare_cells_does_not_parse_csv(self):
        """E-24：同一个"成功"判定曾在 eval/dataset.py 与 compare_cells.py 各有
        一份实现。环境改一次措辞就会两边静默不一致。"""
        from pathlib import Path

        src = Path(".agents/workspace/compare_cells.py").read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        # 去掉模块 docstring（其中会提到旧做法）
        if code.count('"""') >= 2:
            first = code.index('"""')
            second = code.index('"""', first + 3)
            code = code[:first] + code[second + 3:]
        for banned in ("csv.DictReader", "import csv", "was successful", "was not success"):
            assert banned not in code, banned

    def test_validation_script_runs_aggregate_and_gate(self):
        """先前的链子停在逐 run eval，聚合与门禁靠人手跑或干脆不跑 ——
        于是每轮调优都在比没有误差棒的 pass@1 计数。"""
        from pathlib import Path

        src = Path("scripts/run_validation.sh").read_text(encoding="utf-8")
        assert "sar_orch.eval.aggregate" in src
        assert "sar_orch.eval.gate" in src

    def test_csv_cells_are_parsed_safely(self):
        """`eval(value)` 会执行 CSV 单元格里的任意 Python。这些文件虽由本工具
        产出，但读回时是不可信输入（跨机器拷贝、共享结果目录、LLM 写的
        observation 串）。"""
        from pathlib import Path

        src = Path("sar_orch/eval/graders/outcome.py").read_text(encoding="utf-8")
        assert "ast.literal_eval" in src
        assert "return eval(" not in src


class TestBatchCompleteness:
    """`aggregate` 早就记录了无法使用的 run 目录（`skipped_dirs`），但门禁从不读它
    —— 于是"1/3 的 run 没产出报告"的批次照样通过，指标全部由幸存者算出。
    而产不出报告的 run **不成比例地**是跑坏的那些（崩溃/超时/网关错误），
    它们的缺席会让每个指标偏高。对此保持沉默比门禁略微吵闹更糟。
    """

    _GROUP = {
        "key": {"scene": 1, "agents": 2},
        "n": 3,
        "finished_count": 2,
        "pass_at_k": {"1": 0.667},
        "episode_stats": {"coverage": {"mean": 0.9}, "transport_rate": {"mean": 0.8}},
        "constraint_violations": {"per_run_violations": 1.0},
    }

    def _run(self, **extra):
        return gate.evaluate_gate(
            current={"groups": [self._GROUP], **extra},
            baseline=None,
            config=gate.load_config(None),
        )

    def test_complete_batch_is_silent(self):
        r = self._run(valid_run_dirs=3, total_run_dirs_found=3, skipped_dirs=[])
        assert [c for c in r.checks if c.metric == "batch_complete"] == []

    def test_missing_runs_are_surfaced(self):
        r = self._run(
            valid_run_dirs=10, total_run_dirs_found=15,
            skipped_dirs=[f"r{i}" for i in range(5)],
        )
        checks = [c for c in r.checks if c.metric == "batch_complete"]
        assert len(checks) == 1
        assert checks[0].status == "warn"

    def test_reason_states_the_ratio_and_the_bias_direction(self):
        """光说"有 5 个缺失"不够 —— 读者需要知道缺多少比例、以及指标偏向哪一边。"""
        r = self._run(
            valid_run_dirs=10, total_run_dirs_found=15,
            skipped_dirs=[f"r{i}" for i in range(5)],
        )
        reason = [c for c in r.checks if c.metric == "batch_complete"][0].reason
        assert "5/15" in reason
        assert "33%" in reason
        assert "biased upward" in reason

    def test_offending_dirs_are_named_but_bounded(self):
        """要能定位到具体目录，但 100 个缺失时不该把门禁报告刷爆。"""
        r = self._run(
            valid_run_dirs=1, total_run_dirs_found=101,
            skipped_dirs=[f"run_{i:03d}" for i in range(100)],
        )
        reason = [c for c in r.checks if c.metric == "batch_complete"][0].reason
        assert "run_000" in reason
        assert reason.endswith("...")

    def test_warn_does_not_block_on_its_own(self):
        """未评测目录有时是无害的（run 还在跑、目录是杂物）。必须可见，
        但不该单方面拦下批次 —— 拦的应当是真实的指标退化。"""
        r = self._run(valid_run_dirs=3, total_run_dirs_found=4, skipped_dirs=["r4"])
        assert r.passed is True

    def test_absent_keys_are_tolerated(self):
        """老的 aggregate 报告没有这些键，不能因此崩掉。"""
        r = self._run()
        assert [c for c in r.checks if c.metric == "batch_complete"] == []
