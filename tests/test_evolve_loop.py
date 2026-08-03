"""世代编排：生成 → 静态门禁 → 实验 → 适应度 → 晋升/回滚。

LLM 调用与实验运行都是**注入**的，所以整条控制流（去重、终止、晋升、回滚）
可以零 token 测试。这很重要，因为值得测的失败模式恰恰是昂贵的那些：
晋升了一个退化候选、重跑了一个已知结果、在卡住的生成器上无限循环。

本回路相对旧版的四点差异：

**读判定而非退出码。** `gate` 在"确实通过"与"欠功效批次的 fail 全被降级为 warn"
两种情况下都 exit 0 —— 这是两个相反的结果、同一个退出状态。故晋升走
`fitness.evaluate`。

**按内容去重。** 重复提出同一份 skill 否则要花一整批（每 run 约 87 万 token）
去重新发现一个已知结果。

**停滞即终止。** 连续拒绝与重复候选都计入 —— 一个反复产出同样被拒文本的生成器
并没有进展，尽管"没有失败"。

**拒绝历史回喂。** 不回喂的话，因泄漏坐标被拒的候选会换个说法把同一个坐标再交
一次，回路在重新学习同一条规则上烧掉世代。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sar_orch.eval import gate
from sar_orch.evolve import loop, promotion, static_gate
from sar_orch.evolve.leak_dictionary import build as build_leaks

PATH = "worker/navigation/SKILL.md"
TS = "2026-08-02T18:00:00+00:00"

CLEAN = "# Navigation\n\nHead to the nearest known target promptly.\n"
LEAKY = "# Navigation\n\nGo to (15,5) first.\n"
FAT = "# Navigation\n" + "word " * 400


@pytest.fixture(scope="module")
def leaks():
    return build_leaks("SAR/Scenes")


@pytest.fixture
def champion(tmp_path):
    d = tmp_path / "skills"
    shutil.copytree("sar_orch/skills", d)
    return d


@pytest.fixture
def manifest(champion):
    return static_gate.build_baseline_manifest(champion)


@pytest.fixture
def cfg():
    return gate.load_config(None)


def _agg(cov=0.9):
    return {
        "groups": [
            {
                "key": {"scene": 1, "agents": 2},
                "n": 3,
                "finished_count": 2,
                "pass_at_k": {"1": 0.667},
                "episode_stats": {
                    "coverage": {"mean": cov},
                    "transport_rate": {"mean": 0.8},
                },
                "constraint_violations": {"per_run_violations": 1.0},
            }
        ]
    }


def _agg_collapsed():
    """全面崩塌，但 3 个 run 只活下来 1 个（另 2 个没写 eval_report）。

    `repeats` 仍是诚实的 3 —— 这是"声明的重复次数"与"观测到的 n"分叉的那一格。
    n=1 让每条本该 fail 的检查降级为 warn，于是 `gate.passed` 读作 True。
    """
    return {
        "groups": [
            {
                "key": {"scene": 1, "agents": 2},
                "n": 1,
                "finished_count": 0,
                "pass_at_k": {"1": 0.0},
                "episode_stats": {
                    "coverage": {"mean": 0.2},
                    "transport_rate": {"mean": 0.05},
                },
                "constraint_violations": {"per_run_violations": 30.0},
            }
        ]
    }


def _runner(cov=0.9, per_run=None):
    def _r(skills_dir, generation):
        return _agg(cov), (per_run or []), f"batch_gen{generation}"

    return _r


def _run(
    champion, manifest, cfg, leaks, tmp_path, candidates, *, cov=0.9,
    state=None, max_gen=6, repeats=3, holdouts=None, per_run=None,
):
    it = iter(candidates)
    return loop.run_loop(
        champion_dir=champion,
        state_dir=state or (tmp_path / "state"),
        work_dir=tmp_path / "work",
        leaks=leaks,
        baseline_manifest=manifest,
        baseline_aggregate=_agg(0.9),
        baseline_reports=[],
        gate_config=cfg,
        generate=lambda g, h: next(it, None),
        run_batch=_runner(cov, per_run),
        config=loop.LoopConfig(
            max_generations=max_gen, repeats=repeats,
            holdout_labels=holdouts or set(),
        ),
        timestamp_fn=lambda: TS,
    )


C = loop.Candidate


# ---------------------------------------------------------------------------
# 静态门禁在花钱之前拦下候选
# ---------------------------------------------------------------------------


class TestStaticRejectionIsFree:
    def test_leaked_coordinate_never_reaches_the_experiment(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        ran = []

        def spy(skills_dir, generation):
            ran.append(generation)
            return _agg(), [], "b"

        it = iter([C(PATH, LEAKY)])
        loop.run_loop(
            champion_dir=champion, state_dir=tmp_path / "s", work_dir=tmp_path / "w",
            leaks=leaks, baseline_manifest=manifest, baseline_aggregate=_agg(),
            baseline_reports=[], gate_config=cfg,
            generate=lambda g, h: next(it, None), run_batch=spy,
            config=loop.LoopConfig(max_generations=2, repeats=3),
            timestamp_fn=lambda: TS,
        )
        assert ran == [], "a rejected candidate must not cost an experiment"

    def test_over_budget_is_rejected_with_the_numbers(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, FAT)])
        rej = [o for o in outs if o.outcome == "static_reject"]
        assert rej
        assert any("exceeds baseline" in r for r in rej[0].rejections)


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_identical_content_is_not_re_run(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        runs = []

        def spy(skills_dir, generation):
            runs.append(generation)
            return _agg(0.4), [], "b"

        it = iter([C(PATH, CLEAN), C(PATH, CLEAN)])
        outs = loop.run_loop(
            champion_dir=champion, state_dir=tmp_path / "s", work_dir=tmp_path / "w",
            leaks=leaks, baseline_manifest=manifest, baseline_aggregate=_agg(0.9),
            baseline_reports=[], gate_config=cfg,
            generate=lambda g, h: next(it, None), run_batch=spy,
            config=loop.LoopConfig(max_generations=4, repeats=3),
            timestamp_fn=lambda: TS,
        )
        assert len(runs) == 1, "the duplicate should not have cost a second batch"
        assert [o.outcome for o in outs][:2] == ["fitness_reject", "duplicate"]

    def test_duplicate_counts_toward_stagnation(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """反复产出同样内容的生成器没有进展，尽管"没有失败"。"""
        outs = _run(
            champion, manifest, cfg, leaks, tmp_path,
            [C(PATH, LEAKY)] + [C(PATH, CLEAN)] * 5, cov=0.4, max_gen=10,
        )
        assert sum(1 for o in outs if o.outcome == "duplicate") >= 1
        assert not any(o.outcome == "promoted" for o in outs)

    def test_history_persists_dedup_across_invocations(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """第二次调用必须记得上次评估过什么，否则重启回路就等于重花一遍钱。"""
        state = tmp_path / "state"
        _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)],
             cov=0.4, state=state)
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)],
                    cov=0.4, state=state)
        assert outs[0].outcome == "duplicate"


# ---------------------------------------------------------------------------
# 晋升与回滚
# ---------------------------------------------------------------------------


class TestPromotionAndRollback:
    def test_clean_candidate_is_promoted(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        before = promotion.tree_hash(champion)
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)], cov=0.95)
        assert outs[0].outcome == "promoted"
        assert promotion.tree_hash(champion) != before
        assert CLEAN in (champion / PATH).read_text(encoding="utf-8")

    def test_champion_state_records_the_generation(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        state = tmp_path / "state"
        _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)],
             cov=0.95, state=state)
        rec = promotion.load_champion(state)
        assert rec.generation == 1
        assert rec.changed_path == PATH
        assert rec.promoted_at == TS

    @pytest.mark.parametrize("content,cov", [(LEAKY, 0.9), (FAT, 0.9), (CLEAN, 0.3)])
    def test_rejected_candidate_leaves_champion_byte_identical(
        self, champion, manifest, cfg, leaks, tmp_path, content, cov
    ):
        """**回滚正确性的核心断言**：三条拒绝路径（静态/预算/适应度）
        都必须让 skill 树逐字节不变。"""
        before = promotion.tree_hash(champion)
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, content)], cov=cov)
        assert not any(o.outcome == "promoted" for o in outs)
        assert promotion.tree_hash(champion) == before

    def test_no_champion_state_written_on_rejection(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        state = tmp_path / "state"
        _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, LEAKY)], state=state)
        assert promotion.load_champion(state) is None

    def test_promoted_candidate_becomes_the_next_baseline(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """否则同一个改进会在每一代被重复计功。"""
        seen_baselines = []
        gen_iter = iter([C(PATH, CLEAN), C(PATH, CLEAN + "\nSecond.\n")])

        def spy(skills_dir, generation):
            return _agg(0.95), [], "b"

        real = loop.fitness_mod.evaluate

        def capture(**kw):
            seen_baselines.append(
                (kw["baseline"] or {}).get("groups", [{}])[0]
                .get("episode_stats", {}).get("coverage", {}).get("mean")
            )
            return real(**kw)

        loop.fitness_mod.evaluate = capture
        try:
            loop.run_loop(
                champion_dir=champion, state_dir=tmp_path / "s",
                work_dir=tmp_path / "w", leaks=leaks, baseline_manifest=manifest,
                baseline_aggregate=_agg(0.9), baseline_reports=[], gate_config=cfg,
                generate=lambda g, h: next(gen_iter, None), run_batch=spy,
                config=loop.LoopConfig(max_generations=3, repeats=3),
                timestamp_fn=lambda: TS,
            )
        finally:
            loop.fitness_mod.evaluate = real
        assert seen_baselines[0] == pytest.approx(0.9)
        assert seen_baselines[1] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# 终止条件
# ---------------------------------------------------------------------------


class TestTermination:
    def test_stops_after_consecutive_failures(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """没有终止条件的回路会无限烧钱。"""
        cands = [C(PATH, f"# Nav\nGo to (15,5). v{i}\n") for i in range(10)]
        outs = _run(champion, manifest, cfg, leaks, tmp_path, cands, max_gen=10)
        assert len(outs) == 3  # max_consecutive_failures 默认 3

    def test_generator_exhaustion_ends_the_loop(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [])
        assert [o.outcome for o in outs] == ["generator_exhausted"]

    def test_promotion_resets_the_failure_counter(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """一次成功之后应当重新获得完整的失败预算。"""
        cands = [C(PATH, LEAKY), C(PATH, CLEAN), C(PATH, "# Nav\nGo to (2,2).\n")]
        outs = _run(champion, manifest, cfg, leaks, tmp_path, cands, cov=0.95, max_gen=6)
        kinds = [o.outcome for o in outs]
        assert "promoted" in kinds
        assert kinds.index("promoted") == 1

    def test_repeats_below_min_runs_refuses_before_generating(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """必须在**任何**世代之前拒绝 —— 而不是花掉一批之后才发现门禁不可能失败。"""
        called = []
        with pytest.raises(ValueError, match="min_runs"):
            loop.run_loop(
                champion_dir=champion, state_dir=tmp_path / "s",
                work_dir=tmp_path / "w", leaks=leaks, baseline_manifest=manifest,
                baseline_aggregate=_agg(), baseline_reports=[], gate_config=cfg,
                generate=lambda g, h: called.append(g) or C(PATH, CLEAN),
                run_batch=_runner(), config=loop.LoopConfig(repeats=1),
                timestamp_fn=lambda: TS,
            )
        assert called == []


# ---------------------------------------------------------------------------
# 候选落盘与拒绝回喂
# ---------------------------------------------------------------------------


class TestStaging:
    def test_candidate_tree_is_complete_not_just_the_changed_file(
        self, champion, tmp_path
    ):
        """部分树会让本次 run 静默丢掉其他所有 skill。"""
        dest = loop.stage_candidate(
            champion_dir=champion, candidate=C(PATH, CLEAN), dest=tmp_path / "cand"
        )
        assert len(list(dest.rglob("*.md"))) == 9
        assert CLEAN in (dest / PATH).read_text(encoding="utf-8")

    def test_unknown_path_is_rejected(self, champion, tmp_path):
        """候选只能改**已存在**的 skill，不能凭空发明位置。"""
        with pytest.raises(ValueError, match="does not exist"):
            loop.stage_candidate(
                champion_dir=champion,
                candidate=C("made/up/SKILL.md", CLEAN),
                dest=tmp_path / "c",
            )

    def test_staging_is_idempotent(self, champion, tmp_path):
        for _ in range(2):
            loop.stage_candidate(
                champion_dir=champion, candidate=C(PATH, CLEAN), dest=tmp_path / "c"
            )
        assert (tmp_path / "c" / PATH).read_text(encoding="utf-8") == CLEAN


class TestRejectionFeedback:
    def test_prior_rejections_are_rendered(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        outs = _run(champion, manifest, cfg, leaks, tmp_path,
                    [C(PATH, LEAKY), C(PATH, FAT)])
        fb = loop.format_rejection_feedback(outs)
        assert "do not repeat" in fb
        assert "coordinate" in fb
        assert "exceeds baseline" in fb

    def test_empty_history_is_stated_plainly(self):
        assert "No prior rejections" in loop.format_rejection_feedback([])

    def test_promoted_generations_are_not_listed_as_rejections(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)], cov=0.95)
        assert "No prior rejections" in loop.format_rejection_feedback(outs)

    def test_feedback_is_built_deterministically(self):
        """从记录里机械拼装，不引入二次 LLM 摘要 —— 那会多花 token 且可能悄悄
        丢掉"违反了哪条规则"，而那是唯一有用的部分。

        断言"同样输入两次得到同样输出"，而不是去源码里 grep "llm"：
        我最初那版正是后者，结果命中了**解释为什么不用 LLM 的那句注释** ——
        一个会因为代码被正确注释而失败的断言，测的不是行为。
        """
        hist = [
            loop.GenerationOutcome(
                generation=1, outcome="static_reject", path=PATH,
                rejections=["leaked coordinate (15,5)"],
            ),
            loop.GenerationOutcome(
                generation=2, outcome="fitness_reject", path=PATH,
                rejections=["coverage dropped"],
            ),
        ]
        first = loop.format_rejection_feedback(hist)
        assert all(loop.format_rejection_feedback(hist) == first for _ in range(3))
        # 具体违反了什么必须在场 —— 那是回喂唯一的作用
        assert "(15,5)" in first
        assert "coverage dropped" in first

    def test_generator_exhaustion_is_not_presented_as_a_rejection(self):
        """`generator_exhausted` 是"没有候选了"，不是对候选内容的判定。
        把它当成"不要重复这个"回喂，等于把一个空的非尝试当成指导。"""
        hist = [loop.GenerationOutcome(generation=1, outcome="generator_exhausted")]
        assert "No prior rejections" in loop.format_rejection_feedback(hist)


class TestInconclusiveBatch:
    """欠功效批次不得当成干净通过晋升，也不得当成对候选内容的拒绝。"""

    def _run_collapsed(self, champion, manifest, cfg, leaks, tmp_path, cands,
                       state=None, max_gen=6):
        it = iter(cands)
        return loop.run_loop(
            champion_dir=champion, state_dir=state or (tmp_path / "s"),
            work_dir=tmp_path / "w", leaks=leaks, baseline_manifest=manifest,
            baseline_aggregate=_agg(0.9), baseline_reports=[], gate_config=cfg,
            generate=lambda g, h: next(it, None),
            run_batch=lambda sd, g: (_agg_collapsed(), [], f"batch_gen{g}"),
            config=loop.LoopConfig(max_generations=max_gen, repeats=3),
            timestamp_fn=lambda: TS,
        )

    def test_collapsed_batch_is_not_promoted(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """**核心断言**：覆盖 0.9→0.2、违规 1→30，但只有 1 个 run 活下来。
        修复前 gate 报 passed、fitness 报 promote，这个候选会成为新冠军。"""
        before = promotion.tree_hash(champion)
        outs = self._run_collapsed(
            champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)]
        )
        assert not any(o.outcome == "promoted" for o in outs)
        assert promotion.tree_hash(champion) == before, "skill 树必须逐字节不变"

    def test_outcome_is_inconclusive_not_fitness_reject(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """必须与真退化可区分 —— 真退化是内容相关反馈，这个不是。"""
        outs = self._run_collapsed(
            champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)]
        )
        assert outs[0].outcome == "inconclusive"

    def test_reasons_go_to_unjudged_not_rejections(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """`rejections` 会被回喂给生成器。样本量不足写进去等于凭空给候选记缺陷。"""
        outs = self._run_collapsed(
            champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)]
        )
        assert outs[0].rejections == []
        assert outs[0].unjudged_reasons
        assert any("min_runs" in r for r in outs[0].unjudged_reasons)

    def test_not_fed_back_as_a_thing_to_avoid(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """与 `generator_exhausted` 同类：不是对候选内容的判定，
        列进"不要重复这些"等于让生成器回避一段从未被指出问题的文本。"""
        outs = self._run_collapsed(
            champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)]
        )
        fb = loop.format_rejection_feedback(outs)
        assert "No prior rejections" in fb
        assert "do not repeat" not in fb
        assert PATH not in fb

    def test_champion_state_is_not_written(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        state = tmp_path / "state"
        self._run_collapsed(
            champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)], state=state
        )
        assert promotion.load_champion(state) is None

    def test_loop_stops_rather_than_spending_another_batch(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """匹配 `generator_exhausted` 的 break 语义：起因是 run 死在写报告之前，
        下一代原样继承，再跑一批只会得到另一个不可判定的结果（每 run 约 87 万
        token）。既不计入停滞、也不重试。"""
        ran = []

        it = iter([C(PATH, CLEAN), C(PATH, CLEAN + "\nSecond.\n")])
        outs = loop.run_loop(
            champion_dir=champion, state_dir=tmp_path / "s", work_dir=tmp_path / "w",
            leaks=leaks, baseline_manifest=manifest, baseline_aggregate=_agg(0.9),
            baseline_reports=[], gate_config=cfg,
            generate=lambda g, h: next(it, None),
            run_batch=lambda sd, g: (
                ran.append(g), (_agg_collapsed(), [], f"b{g}")
            )[1],
            config=loop.LoopConfig(max_generations=5, repeats=3),
            timestamp_fn=lambda: TS,
        )
        assert ran == [1], "第二代不该再花一批"
        assert [o.outcome for o in outs] == ["inconclusive"]

    def test_summary_says_what_to_do_about_it(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """从外面看，不可判定的停止长得像干净的停止，而该采取的行动完全不同。"""
        outs = self._run_collapsed(
            champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)]
        )
        s = loop.summarise(outs)
        assert "repeats" in s
        assert "not read this as a rejection" in s

    def test_clean_batch_still_promotes(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """happy path 回归护栏：新状态不得泄漏到样本量充足的通过路径。"""
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)], cov=0.95)
        assert outs[0].outcome == "promoted"
        assert outs[0].unjudged_reasons == []

    def test_genuine_regression_still_rejects_with_content_feedback(
        self, champion, manifest, cfg, leaks, tmp_path
    ):
        """真退化路径回归护栏：仍判 fitness_reject，理由仍进 `rejections`
        并被回喂 —— 那是**内容相关**的反馈，与不可判定必须分开处理。"""
        outs = _run(champion, manifest, cfg, leaks, tmp_path, [C(PATH, CLEAN)], cov=0.3)
        assert outs[0].outcome == "fitness_reject"
        assert outs[0].rejections
        assert outs[0].unjudged_reasons == []
        assert "do not repeat" in loop.format_rejection_feedback(outs)


class TestHistory:
    def test_one_corrupt_line_does_not_discard_the_rest(self, tmp_path):
        state = tmp_path / "s"
        state.mkdir()
        (state / loop.HISTORY_FILE).write_text(
            '{"generation": 1, "outcome": "promoted", "content_hash": "aaa"}\n'
            "{not json\n"
            '{"generation": 2, "outcome": "duplicate", "content_hash": "bbb"}\n',
            encoding="utf-8",
        )
        hist = loop.load_history(state)
        assert [h.generation for h in hist] == [1, 2]

    def test_summary_states_when_nothing_was_promoted(self):
        outs = [loop.GenerationOutcome(generation=1, outcome="static_reject")]
        assert "champion is unchanged" in loop.summarise(outs)
