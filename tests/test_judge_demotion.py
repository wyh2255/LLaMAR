"""T7: judge 降级为诊断 —— 锚点删除 / 确定性采样 / 错误可见。

三个缺陷，各自的形状不同：

**E-5 锚点判错对象。** `dispatch_judge.md` 原有 Anchor A：零派发步一律
`full_coverage = fail` + `map_awareness = fail`，理由写的是"闲置 agent 浪费步数"。
但 coordinator 的设计规则是 `NEVER re-dispatch to an agent with an active task`
—— 全员正在执行任务时保持安静**正是设计要求**。锚点把设计要求判成违规，
于是 dispatch pass_rate 与任务成功脱钩，用它把门禁会奖励"迎合评分规则"。

**采样量漂移。** system prompt 原本写"evenly sample up to `judge_sample_steps`
steps"，把选择交给 LLM，实测采样量在 **0-38** 间漂移。3 步算出的 pass_rate 与
38 步算出的不是同一个测量，而报告里两者长得一样。

**E-7 静默吞错。** `_load_canonical_*` 的 `return None` 与 fallback 里的
`continue` 让"文件损坏"与"judge 没跑"产生完全相同的结果 —— 报告照样给出一个
看起来合理的 pass_rate，只是分母悄悄变小了。

判定权归代码（DESIGN P3）：judge 产出降级为诊断，不进门禁。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sar_orch.eval import gate
from sar_orch.eval.agent.eval_agent import collect_judge_results, select_judge_steps

PROMPT_DIR = Path("sar_orch/eval/agent/prompts")


# ---------------------------------------------------------------------------
# 锚点删除（E-5）
# ---------------------------------------------------------------------------


class TestAnchorsRemoved:
    @pytest.fixture
    def prompt(self):
        return (PROMPT_DIR / "dispatch_judge.md").read_text(encoding="utf-8")

    def test_named_anchors_are_gone(self, prompt):
        for anchor in ("Anchor A", "Anchor B", "Anchor C"):
            assert anchor not in prompt, anchor

    def test_zero_dispatch_no_longer_forced_to_fail(self, prompt):
        """原文要求零派发 → `full_coverage = fail`。这是在判 coordinator 的
        设计要求违规。"""
        assert "ZERO dispatches" not in prompt
        assert "full_coverage = fail" not in prompt

    def test_zero_dispatch_maps_to_unknown(self, prompt):
        """零派发时无法从单步证据区分"正确保持安静"与"什么都没做"，
        故应记 Unknown 而非猜一个 fail。"""
        assert "zero dispatches" in prompt.lower()
        assert "Unknown" in prompt
        assert "not automatically a failure" in prompt

    def test_the_design_rule_is_stated(self, prompt):
        """把"为什么零派发不是错"写进 prompt，否则下一个人会把锚点加回来。"""
        assert "active task" in prompt

    def test_tactical_doctrine_items_are_gone(self, prompt):
        """原文含"Carry tasks should go to agents near the person"这类战术教条，
        与 coordinator prompt 里的策略文本互相复述 —— judge 于是在给"是否遵守
        我们自己的教条"打分，形成调优循环性。"""
        assert "Carry tasks should go to agents near the person" not in prompt
        # 断言语义而非逐字：prompt 会换行，硬编码整句易在无害的重排后误报。
        collapsed = " ".join(prompt.split())
        assert "not score the Coordinator against a preferred tactical" in collapsed

    def test_diagnostic_status_is_declared(self, prompt):
        assert "not a gate" in prompt

    def test_unknown_is_preferred_over_guessing(self, prompt):
        assert "Prefer `Unknown` over guessing" in prompt


class TestJudgeMetricsOutOfGate:
    @pytest.mark.parametrize(
        "metric", ["dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_not_in_regression(self, metric):
        assert metric not in gate.DEFAULT_CONFIG["regression"]

    @pytest.mark.parametrize(
        "metric", ["dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_reason_is_recorded(self, metric):
        assert metric in gate.DEMOTED_METRICS
        assert len(gate.DEMOTED_METRICS[metric]) > 40

    @pytest.mark.parametrize(
        "metric", ["dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_still_reported(self, metric):
        """降级 != 删除：原值继续进报告，只是不判 pass/fail。"""
        assert metric in gate.METRICS_BY_KEY


# ─────────────────────────────────────────────────────────────────────────────
# P5（§7.3）：JUDGE_DIAGNOSTIC_METRICS 是冻结契约 —— 进 absolute/regression
# 即拒绝，不能靠直接 config 绕过。
# ─────────────────────────────────────────────────────────────────────────────


class TestJudgeDiagnosticMetricContracts:
    def test_diagnostic_metrics_set_is_frozen(self):
        assert gate.JUDGE_DIAGNOSTIC_METRICS == frozenset(
            {"dispatch_pass_rate_mean", "hallucination_rate_mean"}
        )

    def test_diagnostic_metrics_cannot_reenter_default_config(self):
        for metric in gate.JUDGE_DIAGNOSTIC_METRICS:
            assert metric not in gate.DEFAULT_CONFIG["absolute"]
            assert metric not in gate.DEFAULT_CONFIG["regression"]

    def test_forbidden_metrics_equal_demoted_metrics(self):
        """所有 DEMOTED_METRICS（含 balance_mean）当前都不可 re-enable；
        未来若把某一项移出门禁必须同步更新两处并新增批准/测试。"""
        assert gate.GATE_FORBIDDEN_METRICS == frozenset(gate.DEMOTED_METRICS)

    @pytest.mark.parametrize(
        "metric", ["dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_load_config_rejects_diagnostic_metric(self, tmp_path, metric):
        p = tmp_path / "gate.json"
        p.write_text(json.dumps({"absolute": {metric: {"min": 0.5}}}), encoding="utf-8")
        with pytest.raises(ValueError, match="cannot be enabled"):
            gate.load_config(p)

    @pytest.mark.parametrize(
        "metric", ["dispatch_pass_rate_mean", "hallucination_rate_mean"]
    )
    def test_evaluate_gate_rejects_diagnostic_metric_direct_config(self, metric):
        """绕过 load_config、直接把诊断量塞进 evaluate_gate 的 config 同样被拒。"""
        with pytest.raises(ValueError, match="cannot be enabled"):
            gate.evaluate_gate(
                current={"root_dir": "x", "groups": []},
                baseline=None,
                config={
                    "min_runs": 1,
                    "absolute": {metric: {"min": 0.5}},
                    "regression": {},
                },
            )


# ---------------------------------------------------------------------------
# 确定性采样
# ---------------------------------------------------------------------------


class _AI:
    def __init__(self, ok=True):
        self.succeeded = ok


class _Step:
    def __init__(self, interactions=None):
        self.interactions = list(interactions or [_AI(True)])


class _Ep:
    def __init__(self, n, fail_at=()):
        self.steps = {
            i: _Step([_AI(False)] if i in fail_at else [_AI(True)])
            for i in range(1, n + 1)
        }


class TestDeterministicSampling:
    def test_same_input_yields_same_steps(self):
        """同一 episode 两次调用必须返回**相同**步集合 —— 这是 DESIGN 3.3 的
        明确验收项。否则重跑 judge 得到的 pass_rate 不可复现。"""
        ep = _Ep(30, fail_at={7, 13, 22})
        first = select_judge_steps(ep, 20)
        assert all(select_judge_steps(ep, 20) == first for _ in range(4))

    def test_every_failing_step_is_included(self):
        """失败步是信号所在，不能被均匀采样挤掉。"""
        sel = set(select_judge_steps(_Ep(30, fail_at={7, 13, 22}), 20))
        assert {7, 13, 22} <= sel

    def test_failures_survive_a_small_target(self):
        """25 个失败步、target=5：宁可超出 target 也不丢失败步。
        丢掉它们会让"这个 run 到底错在哪"无法回答。"""
        sel = select_judge_steps(_Ep(30, fail_at=set(range(1, 26))), 5)
        assert len(sel) >= 25

    @pytest.mark.parametrize("n", [1, 2, 5, 30, 100])
    def test_endpoints_always_sampled(self, n):
        """首步是幻觉高发处，末步是预算耗尽行为出现处。"""
        sel = select_judge_steps(_Ep(n), 20)
        assert 1 in sel and n in sel

    @pytest.mark.parametrize("target", [1, 3, 5, 10, 20])
    def test_target_is_respected_without_failures(self, target):
        sel = select_judge_steps(_Ep(30), target)
        assert len(sel) <= max(target, 2)  # 首末两步是下限

    def test_target_above_episode_length_returns_all(self):
        assert select_judge_steps(_Ep(10), 50) == list(range(1, 11))

    def test_coverage_is_spread_not_bunched(self):
        """曾经的 bug：`stride = len(remaining)//room` 在 room 接近
        len(remaining) 时退化为 1，采样全挤在开头（target=20/30 步时取 1..18
        然后直接跳到 30），末段完全没覆盖 —— 而末段正是预算耗尽行为所在。"""
        sel = select_judge_steps(_Ep(30), 10)
        gaps = [b - a for a, b in zip(sel, sel[1:])]
        assert max(gaps) <= 6, sel
        assert any(s > 20 for s in sel), sel

    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_nonpositive_target_yields_nothing(self, bad):
        assert select_judge_steps(_Ep(10), bad) == []

    def test_empty_episode_yields_nothing(self):
        assert select_judge_steps(_Ep(0), 20) == []

    def test_steps_are_sorted(self):
        sel = select_judge_steps(_Ep(30, fail_at={22, 7, 13}), 15)
        assert sel == sorted(sel)


# ---------------------------------------------------------------------------
# 错误可见（E-7）
# ---------------------------------------------------------------------------


def _judge_dir(tmp_path, **files):
    d = tmp_path / "judge_results"
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (d / name).write_text(content, encoding="utf-8")
    return tmp_path


VALID_DISPATCH = json.dumps(
    {
        "verdicts": [
            {
                "step": 1,
                "full_coverage": "pass",
                "role_match": "pass",
                "map_awareness": "pass",
                "step_budget_awareness": "pass",
                "notes": "1 dispatch",
            }
        ],
        "summary": "ok",
    }
)


class TestJudgeErrorsAreRecorded:
    def test_malformed_json_is_reported_not_swallowed(self, tmp_path):
        """损坏的文件先前与"judge 没跑"产生相同结果：都返回 None，
        调用方回退到 glob 扫描，报告照样给出一个 pass_rate。"""
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": "{not json"})
        res = collect_judge_results(ws)
        assert res["judge_errors"], res
        assert res["judge_errors"][0]["stage"] == "parse"
        assert "dispatch_full.json" == res["judge_errors"][0]["file"]

    def test_wrong_schema_is_reported(self, tmp_path):
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": json.dumps({"nope": 1})})
        res = collect_judge_results(ws)
        assert any(e["stage"] == "schema" for e in res["judge_errors"])

    def test_errors_key_present_even_when_clean(self, tmp_path):
        """字段缺席读起来像"没检查过"，而这正是它要消除的歧义。"""
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": VALID_DISPATCH})
        res = collect_judge_results(ws)
        assert res["judge_errors"] == []

    def test_missing_file_is_not_an_error(self, tmp_path):
        """文件不存在 = judge 没跑这一项，不是错误。与"存在但用不了"区分。"""
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": VALID_DISPATCH})
        res = collect_judge_results(ws)
        assert not any("observation" in e["file"] for e in res["judge_errors"])

    def test_partial_flag_when_fewer_steps_than_requested(self, tmp_path):
        """1 步的 pass_rate 与 20 步的不是同一个测量。"""
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": VALID_DISPATCH})
        res = collect_judge_results(ws, expected_steps=20)
        assert res["judge_partial"] is True
        assert res["expected_steps"] == 20
        assert res["dispatch"]["sampled_steps"] == 1

    def test_not_partial_when_count_matches(self, tmp_path):
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": VALID_DISPATCH})
        assert collect_judge_results(ws, expected_steps=1)["judge_partial"] is False

    def test_errors_imply_partial_even_without_expected_count(self, tmp_path):
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": "{broken"})
        assert collect_judge_results(ws).get("judge_partial") is True

    def test_gating_status_is_declared_in_the_report(self, tmp_path):
        """报告读者不应把低 pass_rate 误当门禁条件。"""
        ws = _judge_dir(tmp_path, **{"dispatch_full.json": VALID_DISPATCH})
        assert collect_judge_results(ws)["gating"] == "diagnostic-only"

    def test_absent_judge_dir_returns_empty(self, tmp_path):
        assert collect_judge_results(tmp_path) == {}


class TestCliPassesExpectedSteps:
    def test_cli_wires_the_deterministic_count(self):
        """只加确定性采样而不把它的计数传给 collect，`judge_partial` 就永远
        算不出来 —— 又一个"参数被接收但从不生效"的形状。"""
        src = Path("sar_orch/eval/cli.py").read_text(encoding="utf-8")
        assert "select_judge_steps" in src
        assert "expected_steps=" in src

    def test_prompt_declares_the_step_list_authoritative(self):
        src = Path("sar_orch/eval/agent/eval_agent.py").read_text(encoding="utf-8")
        assert "judge_steps" in src
        assert "authoritative" in src
