"""冠军状态与原子晋升。

旧回路**无条件轮转**世代：候选无论好坏都成为下一代的基底，于是退化被继承而不是
被丢弃。这里只在门禁明确通过时晋升，且写入是原子的。

原子性为什么不是过度设计：晋升要把候选的 skill 树覆盖到活的 `sar_orch/skills/`。
中途崩溃会留下**混合**状态 —— 一部分文件来自冠军、一部分来自候选 —— 那是一个
从未被评估过的配置，且不属于任何一代。更糟的是它**静默**：下一个 run 会加载它，
并报出一个归属于"从未整体存在过的树"的数字。

**失败的候选必须让 `sar_orch/skills/` 逐字节不变。** 这一条是断言出来的，
不是假定的。
"""

from __future__ import annotations

import json

import pytest

from sar_orch.evolve import promotion as pr

TS = "2026-08-02T18:00:00+00:00"


def _tree(root, **files):
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def live(tmp_path):
    return _tree(
        tmp_path / "skills",
        **{
            "worker/navigation/SKILL.md": "# Nav\nchampion content\n",
            "coordinator/exploration/SKILL.md": "# Explore\nchampion content\n",
        },
    )


@pytest.fixture
def candidate(tmp_path):
    return _tree(
        tmp_path / "cand",
        **{
            "worker/navigation/SKILL.md": "# Nav\nCANDIDATE content\n",
            "coordinator/exploration/SKILL.md": "# Explore\nchampion content\n",
        },
    )


# ---------------------------------------------------------------------------
# tree_hash
# ---------------------------------------------------------------------------


class TestTreeHash:
    def test_same_content_same_hash(self, tmp_path, live):
        copy = _tree(
            tmp_path / "copy",
            **{
                "worker/navigation/SKILL.md": "# Nav\nchampion content\n",
                "coordinator/exploration/SKILL.md": "# Explore\nchampion content\n",
            },
        )
        assert pr.tree_hash(copy) == pr.tree_hash(live)

    def test_content_change_changes_hash(self, live, candidate):
        assert pr.tree_hash(candidate) != pr.tree_hash(live)

    def test_path_participates_in_the_hash(self, tmp_path):
        """同样的字节挂在不同路径下是不同的配置。"""
        a = _tree(tmp_path / "a", **{"x/SKILL.md": "same\n"})
        b = _tree(tmp_path / "b", **{"y/SKILL.md": "same\n"})
        assert pr.tree_hash(a) != pr.tree_hash(b)

    def test_is_stable_across_calls(self, live):
        """显式排序而非依赖 `rglob` 顺序 —— 后者未指定，跨文件系统不同，
        会让同一棵树在不同机器上算出不同哈希。"""
        assert len({pr.tree_hash(live) for _ in range(5)}) == 1


# ---------------------------------------------------------------------------
# champion state
# ---------------------------------------------------------------------------


class TestChampionState:
    def test_absent_state_reads_as_none(self, tmp_path):
        assert pr.load_champion(tmp_path) is None

    def test_roundtrip(self, tmp_path):
        rec = pr.ChampionRecord(
            generation=3, skills_hash="abc123", promoted_at=TS,
            metrics={"pass_at_1": 0.4}, changed_path="worker/navigation/SKILL.md",
        )
        pr.save_champion(tmp_path, rec)
        got = pr.load_champion(tmp_path)
        assert got.generation == 3
        assert got.skills_hash == "abc123"
        assert got.metrics == {"pass_at_1": 0.4}

    def test_corrupt_state_raises_rather_than_reading_as_absent(self, tmp_path):
        """损坏的状态文件若被当成"还没有冠军"，会静默从基线重启并丢掉全部历史。"""
        (tmp_path / pr.CHAMPION_STATE_FILE).write_text("{broken", encoding="utf-8")
        with pytest.raises(RuntimeError, match="refusing"):
            pr.load_champion(tmp_path)

    def test_save_is_atomic_no_temp_left_behind(self, tmp_path):
        pr.save_champion(tmp_path, pr.ChampionRecord(generation=1, skills_hash="x"))
        assert [p.name for p in tmp_path.iterdir()] == [pr.CHAMPION_STATE_FILE]

    def test_metrics_are_recorded_for_later_attribution(self, tmp_path):
        """记录晋升时的指标，否则几代之后无法回答"是哪一代开始退化的"。"""
        pr.save_champion(
            tmp_path,
            pr.ChampionRecord(generation=2, skills_hash="h", metrics={"coverage_mean": 0.9}),
        )
        data = json.loads((tmp_path / pr.CHAMPION_STATE_FILE).read_text(encoding="utf-8"))
        assert data["metrics"]["coverage_mean"] == 0.9


# ---------------------------------------------------------------------------
# promotion
# ---------------------------------------------------------------------------


class TestPromotion:
    def test_live_tree_becomes_the_candidate(self, tmp_path, live, candidate):
        pr.promote(
            candidate_dir=candidate, live_dir=live, state_dir=tmp_path / "state",
            generation=1, timestamp=TS,
        )
        got = (live / "worker/navigation/SKILL.md").read_text(encoding="utf-8")
        assert "CANDIDATE content" in got

    def test_untouched_files_survive(self, tmp_path, live, candidate):
        pr.promote(
            candidate_dir=candidate, live_dir=live, state_dir=tmp_path / "state",
            generation=1, timestamp=TS,
        )
        other = (live / "coordinator/exploration/SKILL.md").read_text(encoding="utf-8")
        assert "champion content" in other

    def test_state_records_the_new_hash(self, tmp_path, live, candidate):
        rec = pr.promote(
            candidate_dir=candidate, live_dir=live, state_dir=tmp_path / "state",
            generation=4, timestamp=TS, changed_path="worker/navigation/SKILL.md",
        )
        assert rec.skills_hash == pr.tree_hash(live)
        assert rec.generation == 4
        assert pr.load_champion(tmp_path / "state").skills_hash == rec.skills_hash

    def test_no_staging_dirs_left_behind(self, tmp_path, live, candidate):
        pr.promote(
            candidate_dir=candidate, live_dir=live, state_dir=tmp_path / "state",
            generation=1, timestamp=TS,
        )
        leftovers = [p.name for p in live.parent.iterdir() if p.name.startswith(".promote")]
        assert leftovers == []

    def test_outgoing_champion_can_be_archived(self, tmp_path, live, candidate):
        arch = tmp_path / "archive"
        pr.promote(
            candidate_dir=candidate, live_dir=live, state_dir=tmp_path / "state",
            generation=2, timestamp=TS, archive_dir=arch,
        )
        saved = (arch / "gen001" / "worker/navigation/SKILL.md").read_text(encoding="utf-8")
        assert "champion content" in saved

    def test_missing_candidate_leaves_live_untouched(self, tmp_path, live):
        """这是回滚正确性的核心断言：晋升失败时活树必须逐字节不变。"""
        before = pr.tree_hash(live)
        with pytest.raises(FileNotFoundError):
            pr.promote(
                candidate_dir=tmp_path / "nope", live_dir=live,
                state_dir=tmp_path / "state", generation=1, timestamp=TS,
            )
        assert pr.tree_hash(live) == before

    def test_failed_promotion_writes_no_champion_state(self, tmp_path, live):
        """失败不得留下"已晋升"的记录 —— 否则状态与磁盘内容不一致。"""
        state = tmp_path / "state"
        with pytest.raises(FileNotFoundError):
            pr.promote(
                candidate_dir=tmp_path / "nope", live_dir=live,
                state_dir=state, generation=1, timestamp=TS,
            )
        assert pr.load_champion(state) is None

    def test_timestamp_is_injected_not_read_from_the_clock(self, tmp_path, live, candidate):
        """时间戳由调用方给出，操作才可复现。"""
        rec = pr.promote(
            candidate_dir=candidate, live_dir=live, state_dir=tmp_path / "state",
            generation=1, timestamp=TS,
        )
        assert rec.promoted_at == TS


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


class TestGitCommit:
    def test_disabled_by_default(self):
        """自动提交默认关闭：一个会自己提交的进化回路会积累无人审阅的历史。"""
        assert pr.git_commit_promotion(paths=["x"], message="m") is None

    def test_never_stages_the_whole_tree(self):
        """`git add -A` / `git add .` 会把工作树里任何无关改动卷进自动提交。
        只允许 add 明确的晋升路径。"""
        import inspect

        src = inspect.getsource(pr.git_commit_promotion)
        # 只查 git 命令构造本身。先前这条断言写成 `'"."' not in src`，
        # 结果命中了 `repo_root: Path | str = "."` 这个**默认形参** ——
        # 那与 pathspec 无关。断言必须针对真正危险的东西。
        assert '"add", "-A"' not in src
        assert '"add", "."' not in src
        assert '"commit", "-a"' not in src
        # 路径以 `--` 分隔后显式传入，不依赖通配
        assert '["git", "add", "--"] + paths' in src

    def test_enabled_defaults_to_false_in_the_signature(self):
        import inspect

        default = inspect.signature(pr.git_commit_promotion).parameters["enabled"].default
        assert default is False
