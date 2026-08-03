"""晋升判定：`eval/gate.py` 的薄封装 + gate 自身做不到的三项检查。

适应度**不是**新的评分函数，就是既有的回归门禁 —— 候选由守护本项目其他一切
比较的同一份代码判定。本模块补的是"门禁通过"可能是空心的三种方式：

**1. 欠功效批次默认通过。** 组内 run 数低于 `min_runs` 时 gate 把所有 fail 降级为
`warn`，而 `GateResult.passed` 只看 fail。于是 `repeats=1` 的批次**不可能失败** ——
报 PASSED 加一堆 warning。预算压力下 `--repeats 1` 恰恰是最容易被选的，
而它会静默关掉回滚。故用硬断言而非警告。

**2. 池化掩盖单种子崩溃。** `group_by_key` 按 `(scene, agents)` 分组、seed 被池化。
候选可以抬高组均值同时毁掉某个 seed，而 gate 只看得到均值。

**3. 留出组是结构性最弱的一环。** 配对组因多 seed 共享 `(scene,agents)` 键被自然
池化到 n=3N，天然免疫欠功效降级；留出组按定义是**单例组**，n 恒等于重复次数 ——
是这套分组机制里唯一的单例。预算一压，专门用来抓过拟合的机制第一个失效。
故留出组有独立下限，且其上任何 warn 都阻止自动晋升。
"""

from __future__ import annotations

import pytest

from sar_orch.eval import gate
from sar_orch.evolve import fitness as ft


def _group(scene=1, agents=2, n=3, fin=2, cov=0.9, tr=0.8, viol=1.0):
    return {
        "key": {"scene": scene, "agents": agents},
        "n": n,
        "finished_count": fin,
        "pass_at_k": {"1": fin / n if n else 0.0},
        "episode_stats": {
            "coverage": {"mean": cov},
            "transport_rate": {"mean": tr},
        },
        "constraint_violations": {"per_run_violations": viol},
    }


def _rep(scene, seed, cov, agents=2):
    return {
        "metadata": {"scene": scene, "agents": agents, "seed": seed},
        "episode": {"coverage": cov},
    }


@pytest.fixture
def cfg():
    return gate.load_config(None)


# ---------------------------------------------------------------------------
# 1. 欠功效批次
# ---------------------------------------------------------------------------


class TestRepeatsFloor:
    def test_repeats_below_min_runs_raises(self, cfg):
        """必须 raise 而不是返回 reject —— 这是**配置错误**不是候选的问题，
        报成 reject 会让人以为候选不合格。"""
        with pytest.raises(ValueError, match="min_runs"):
            ft.evaluate(
                current={"groups": [_group()]}, baseline=None, config=cfg, repeats=1
            )

    def test_message_explains_the_silent_failure(self, cfg):
        """理由必须写清"门禁会报 PASSED"，否则下一个人会觉得这个断言多余。"""
        with pytest.raises(ValueError) as exc:
            ft.assert_adequate_repeats(1, cfg)
        msg = str(exc.value)
        assert "PASSED" in msg
        assert "unconditional" in msg

    def test_repeats_at_min_runs_is_allowed(self, cfg):
        ft.assert_adequate_repeats(2, cfg)  # 不抛

    def test_custom_min_runs_is_respected(self):
        with pytest.raises(ValueError):
            ft.assert_adequate_repeats(3, {"min_runs": 5})


# ---------------------------------------------------------------------------
# 2. 逐 seed 回归
# ---------------------------------------------------------------------------


class TestPerSeedRegression:
    def test_identical_group_mean_still_catches_a_seed_collapse(self):
        """这是本模块存在的核心理由：两侧组均值**完全相同**（0.700），
        但 seed 44 从 1.0 崩到 0.1。gate 只看均值，什么都发现不了。"""
        base = [_rep(1, 42, 0.5), _rep(1, 43, 0.6), _rep(1, 44, 1.0)]
        cur = [_rep(1, 42, 0.9), _rep(1, 43, 1.1), _rep(1, 44, 0.1)]
        assert sum(r["episode"]["coverage"] for r in base) == pytest.approx(
            sum(r["episode"]["coverage"] for r in cur)
        )
        problems = ft.check_per_seed_regression(cur, base)
        assert len(problems) == 1
        assert "seed=44" in problems[0]

    def test_uniform_improvement_is_clean(self):
        base = [_rep(1, 42, 0.5), _rep(1, 43, 0.5)]
        cur = [_rep(1, 42, 0.9), _rep(1, 43, 0.9)]
        assert ft.check_per_seed_regression(cur, base) == []

    def test_small_drop_is_tolerated(self):
        """噪声不该被当成退化 —— 阈值以下不报。"""
        base = [_rep(1, 42, 0.9)]
        cur = [_rep(1, 42, 0.8)]
        assert ft.check_per_seed_regression(cur, base) == []

    def test_threshold_is_configurable(self):
        base = [_rep(1, 42, 0.9)]
        cur = [_rep(1, 42, 0.8)]
        assert ft.check_per_seed_regression(cur, base, max_drop=0.05)

    def test_missing_seed_is_skipped_not_flagged(self):
        """某 seed 只在一侧存在 = 批次组成问题，由 gate 的完整性检查报告，
        不该在这里伪装成"退化"。"""
        base = [_rep(1, 42, 0.9), _rep(1, 99, 0.9)]
        cur = [_rep(1, 42, 0.9)]
        assert ft.check_per_seed_regression(cur, base) == []

    def test_scene_and_agents_are_part_of_the_key(self):
        """同 seed 不同 scene 是不同的格，不能混。"""
        base = [_rep(1, 42, 1.0), _rep(2, 42, 0.2)]
        cur = [_rep(1, 42, 1.0), _rep(2, 42, 0.2)]
        assert ft.check_per_seed_regression(cur, base) == []

    def test_absent_metric_does_not_crash(self):
        base = [{"metadata": {"scene": 1, "agents": 2, "seed": 42}, "episode": {}}]
        cur = base
        assert ft.check_per_seed_regression(cur, base) == []


# ---------------------------------------------------------------------------
# 3. 留出组
# ---------------------------------------------------------------------------


class TestHoldoutHealth:
    def _result(self, groups, cfg):
        return gate.evaluate_gate(
            current={"groups": groups}, baseline={"groups": groups}, config=cfg
        )

    def test_absent_holdout_blocks(self, cfg):
        """留出组不在报告里 = 根本没跑，过拟合无从检测。"""
        r = self._result([_group()], cfg)
        problems = ft.check_holdout_health(r, {"S2xA5"})
        assert any("absent" in p for p in problems)

    def test_underpowered_holdout_blocks(self, cfg):
        """n=1 的留出组：所有检查被降级为 warn，于是它产出不了任何判定 ——
        而只读 gate 的 exit code 分辨不出这与真通过的区别。"""
        r = self._result([_group(), _group(scene=2, agents=5, n=1, fin=0)], cfg)
        problems = ft.check_holdout_health(r, {"S2xA5"})
        assert problems
        assert any("floor" in p or "real verdict" in p for p in problems)

    def test_healthy_holdout_passes(self, cfg):
        r = self._result([_group(), _group(scene=2, agents=5, n=3, fin=2)], cfg)
        assert ft.check_holdout_health(r, {"S2xA5"}) == []

    def test_failing_holdout_blocks(self, cfg):
        cur = [_group(scene=2, agents=5, n=3, fin=2, cov=0.2)]
        r = gate.evaluate_gate(
            current={"groups": cur},
            baseline={"groups": [_group(scene=2, agents=5, n=3, fin=2, cov=0.9)]},
            config=cfg,
        )
        assert any("failed" in p for p in ft.check_holdout_health(r, {"S2xA5"}))

    def test_holdout_floor_is_independent_of_min_runs(self, cfg):
        """留出组的重复次数不得随预算单方面压低 —— 它有独立下限。"""
        r = self._result([_group(scene=2, agents=5, n=1, fin=0)], cfg)
        assert ft.check_holdout_health(r, {"S2xA5"}, min_holdout_runs=3)


# ---------------------------------------------------------------------------
# 整体判定
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_clean_candidate_promotes(self, cfg):
        g = [_group()]
        v = ft.evaluate(
            current={"groups": g}, baseline={"groups": g}, config=cfg, repeats=3
        )
        assert v.promote and v.blockers == []

    def test_gate_failure_blocks(self, cfg):
        v = ft.evaluate(
            current={"groups": [_group(cov=0.5)]},
            baseline={"groups": [_group(cov=0.9)]},
            config=cfg,
            repeats=3,
        )
        assert not v.promote
        assert any("gate fail" in b for b in v.blockers)

    def test_config_drift_blocks(self, cfg):
        """批内混了两个 model 的数据不可池化，其 CI 不表示任何单一配置的性能。"""
        g = _group()
        g["config_issues"] = [
            {"field": "model", "values": {"a": ["r1"], "b": ["r2"]},
             "partial_record_only": False}
        ]
        v = ft.evaluate(
            current={"groups": [g]}, baseline=None, config=cfg, repeats=3
        )
        assert not v.promote
        assert any("config drift" in b or "config:" in b for b in v.blockers)

    def test_metrics_are_carried_for_attribution(self, cfg):
        g = [_group()]
        v = ft.evaluate(
            current={"groups": g}, baseline={"groups": g}, config=cfg, repeats=3
        )
        assert "S1xA2" in v.metrics
        assert v.metrics["S1xA2"]["coverage_mean"] == pytest.approx(0.9)

    def test_skipped_per_seed_check_is_noted_not_silent(self, cfg):
        """没提供逐 run 报告时，池化掩盖的风险必须被记下来而不是无声跳过。"""
        g = [_group()]
        v = ft.evaluate(
            current={"groups": g}, baseline={"groups": g}, config=cfg, repeats=3
        )
        assert any("per-seed" in n for n in v.notes)

    def test_warnings_are_recorded_as_notes(self, cfg):
        v = ft.evaluate(
            current={"groups": [_group()], "valid_run_dirs": 2,
                     "total_run_dirs_found": 3, "skipped_dirs": ["r3"]},
            baseline=None, config=cfg, repeats=3,
        )
        assert any("batch_complete" in n for n in v.notes)

    def test_summary_is_readable(self, cfg):
        g = [_group()]
        assert "PROMOTE" in ft.evaluate(
            current={"groups": g}, baseline={"groups": g}, config=cfg, repeats=3
        ).summary()
        assert "REJECT" in ft.evaluate(
            current={"groups": [_group(cov=0.4)]},
            baseline={"groups": [_group(cov=0.9)]},
            config=cfg, repeats=3,
        ).summary()


# ---------------------------------------------------------------------------
# 4. 无法判定（gate 已给出信号，本层必须消费）
# ---------------------------------------------------------------------------


def _collapsed():
    """全面崩塌，但只活下来 1 个 run —— 另 2 个崩了/超时，没写 eval_report。

    注意 `repeats` 仍然诚实地是 3：`assert_adequate_repeats` 读的是**声明值**，
    降级读的是**观测到的 n**，两者会分叉。这个格就是分叉点。
    """
    return {"groups": [_group(n=1, fin=0, cov=0.2, tr=0.05, viol=30.0)]}


class TestInconclusiveVerdict:
    def test_collapse_at_n1_is_not_promotable(self, cfg):
        """**本类存在的核心理由**：覆盖 0.9→0.2、违规 1→30，每一条都本该判 fail，
        只因 n=1 全被降级为 warn，于是 `gate.passed` 读作 True。修复前
        `promote` 也跟着是 True —— 一次全面崩塌可以当冠军。"""
        v = ft.evaluate(
            current=_collapsed(), baseline={"groups": [_group()]},
            config=cfg, repeats=3,
        )
        assert v.gate_passed is True, "gate 仍报 passed（含义不变）"
        assert v.inconclusive is True
        assert v.promote is False

    def test_reason_names_the_sample_size_not_a_generic_rejection(self, cfg):
        """理由必须能追回"样本量不足"，而不是一句泛泛的拒绝 —— 否则人会去
        改候选内容，而该改的是 --repeats。"""
        v = ft.evaluate(
            current=_collapsed(), baseline={"groups": [_group()]},
            config=cfg, repeats=3,
        )
        blob = " ".join(v.inconclusive_reasons)
        assert "min_runs" in blob
        assert "repeats" in blob
        assert "no verdict" in blob
        # 被掩盖的具体检查必须留证，否则"无法判定"无从复核
        assert any("coverage_mean" in r for r in v.inconclusive_reasons)

    def test_inconclusive_does_not_produce_blockers(self, cfg):
        """blockers 会作为"不要重复这个"回喂给下一代生成器。样本量不足与候选
        内容无关，写进 blockers 等于凭空给候选记一条不存在的缺陷。"""
        v = ft.evaluate(
            current=_collapsed(), baseline={"groups": [_group()]},
            config=cfg, repeats=3,
        )
        assert v.blockers == []

    def test_inconclusive_is_distinguishable_from_a_hard_failure(self, cfg):
        """真退化是**内容相关**的反馈、要回喂；无法判定不是。两者必须可区分。"""
        inc = ft.evaluate(
            current=_collapsed(), baseline={"groups": [_group()]},
            config=cfg, repeats=3,
        )
        hard = ft.evaluate(
            current={"groups": [_group(cov=0.5)]},
            baseline={"groups": [_group(cov=0.9)]}, config=cfg, repeats=3,
        )
        assert (inc.promote, inc.inconclusive, bool(inc.blockers)) == (False, True, False)
        assert (hard.promote, hard.inconclusive, bool(hard.blockers)) == (False, False, True)
        assert hard.inconclusive_reasons == []

    def test_clean_pass_is_not_marked_inconclusive(self, cfg):
        """样本量充足时行为必须逐字不变 —— 不能把新状态泄漏到 happy path。"""
        g = [_group()]
        v = ft.evaluate(
            current={"groups": g}, baseline={"groups": g}, config=cfg, repeats=3
        )
        assert v.promote is True
        assert v.inconclusive is False
        assert v.inconclusive_reasons == []

    def test_summary_headline_says_inconclusive_not_reject(self, cfg):
        v = ft.evaluate(
            current=_collapsed(), baseline={"groups": [_group()]},
            config=cfg, repeats=3,
        )
        head = v.summary().splitlines()[0]
        assert "INCONCLUSIVE" in head
        assert "REJECT" not in head

    def test_real_blockers_outrank_the_inconclusive_headline(self, cfg):
        """两者同时成立时，可行动的那个不该被"批次太小"挡在后面。"""
        v = ft.FitnessVerdict(
            promote=False, blockers=["gate fail S1xA2/coverage_mean: ..."],
            inconclusive=True, inconclusive_reasons=["..."],
        )
        assert "REJECT" in v.summary().splitlines()[0]

    def test_assert_adequate_repeats_stays_silent_on_this_case(self, cfg):
        """两道防线**互补**、互不覆盖：声明的 repeats=3 完全诚实，
        断言无话可说；只有 gate 的降级证据能抓到实际 n=1。"""
        ft.assert_adequate_repeats(3, cfg)  # 不抛
        v = ft.evaluate(
            current=_collapsed(), baseline={"groups": [_group()]},
            config=cfg, repeats=3,
        )
        assert v.inconclusive is True

    def test_underpowered_but_in_tolerance_is_not_inconclusive(self, cfg):
        """反向缺口：`--repeats 1` 的 smoke batch 若各项都在容差内，没有任何
        检查被降级，`inconclusive` 为 False —— 此时**只有** assert 拦得住它。
        这就是为什么不能用 inconclusive 替掉那道断言。"""
        g = [_group(n=1, fin=1)]
        r = gate.evaluate_gate(
            current={"groups": g}, baseline={"groups": g}, config=cfg
        )
        assert r.downgraded == []
        assert r.inconclusive is False
        with pytest.raises(ValueError, match="min_runs"):
            ft.evaluate(
                current={"groups": g}, baseline={"groups": g}, config=cfg, repeats=1
            )
