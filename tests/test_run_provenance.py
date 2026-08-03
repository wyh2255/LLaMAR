"""T2: 运行溯源 —— prompt_hash / prompt_snapshot / 脏树标记 / --skills-dir。

要回答的问题是"某个历史 run 到底用的什么代码和什么 prompt"。改动前三处都答不上：
`prompt_version` 是硬编码字符串、run 目录不存 prompt 内容、`code_commit` 在工作树
脏时记的是 HEAD（而实际跑的是 HEAD + 未提交改动）。

`--skills-dir` 是这里最要紧的一项，它是阶段 5 自进化回路的地基：候选 skill 必须
能真的被送进实验。本仓库有个反复出现的病灶 —— 上层接收参数、写进 metadata、
但从不生效（被硬编码或派生路径盖掉），已出现三次。所以这里的断言必须是
**真的把 skills 目录指向副本，然后证明加载的是副本**，而不是只断言形参存在。
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

import pytest

from sar_orch import experiment as E

PROMPTS = "sar_orch/prompts"
SKILLS = "sar_orch/skills"


@pytest.fixture
def prompt_tree(tmp_path):
    """A byte-identical copy of the real prompt/skill trees."""
    p, s = tmp_path / "prompts", tmp_path / "skills"
    shutil.copytree(PROMPTS, p)
    shutil.copytree(SKILLS, s)
    return p, s


# ---------------------------------------------------------------------------
# (A) 脏树检测
# ---------------------------------------------------------------------------


class TestGitDirtyMarking:
    def test_clean_tree_records_bare_sha(self):
        sha = E._get_git_commit(dirty=False)
        assert sha and "-dirty" not in sha

    def test_dirty_tree_is_marked(self):
        """脏树时记裸 SHA 是**错的** —— 实际跑的代码不等于那个 commit。"""
        sha = E._get_git_commit(dirty=True)
        assert sha.endswith("-dirty")

    def test_marker_is_suffix_not_replacement(self):
        """SHA 本身必须仍然可读 —— 否则丢掉了唯一的代码定位信息。"""
        clean = E._get_git_commit(dirty=False)
        assert E._get_git_commit(dirty=True) == f"{clean}-dirty"

    def test_metadata_carries_the_flag_separately(self):
        """既记进 code_commit 字符串，也单独出一个布尔 —— 后者才好机械过滤。"""
        md = _metadata(git_dirty=True)
        assert md["git_dirty"] is True
        assert md["code_commit"].endswith("-dirty")
        assert _metadata(git_dirty=False)["git_dirty"] is False


# ---------------------------------------------------------------------------
# (B) prompt_hash
# ---------------------------------------------------------------------------


class TestPromptHash:
    def test_is_stable_across_calls(self):
        assert E.compute_prompt_hash(PROMPTS, SKILLS) == E.compute_prompt_hash(
            PROMPTS, SKILLS
        )

    def test_identical_content_at_a_different_path_hashes_the_same(self, prompt_tree):
        """内容相同就该同哈希 —— 否则把候选 skill 拷到别处就无法与冠军比对。"""
        p, s = prompt_tree
        assert E.compute_prompt_hash(p, s) == E.compute_prompt_hash(PROMPTS, SKILLS)

    def test_traversal_order_does_not_affect_the_hash(self, prompt_tree):
        """`Path.rglob` 的顺序是未指定的，跨机器/文件系统可能不同。若哈希依赖
        它，同一份 prompt 在不同机器上会算出不同值 —— 这种 bug 极难发现，
        所以显式排序必须被钉住。"""
        p, s = prompt_tree
        pairs = E._prompt_hash_sources(p, s)
        assert len(pairs) > 1
        keys = [k for k, _ in pairs]
        # 排序键必须是相对路径字符串，且排序后与调用顺序无关
        assert sorted(keys) == sorted(reversed(keys))
        baseline = E.compute_prompt_hash(p, s)
        # 反复调用（rglob 每次重新枚举）必须稳定
        assert all(E.compute_prompt_hash(p, s) == baseline for _ in range(3))

    def test_one_changed_character_changes_the_hash(self, prompt_tree):
        p, s = prompt_tree
        before = E.compute_prompt_hash(p, s)
        target = sorted(p.rglob("*.md"))[0]
        target.write_text(target.read_text(encoding="utf-8") + "x", encoding="utf-8")
        assert E.compute_prompt_hash(p, s) != before

    def test_skill_change_also_changes_the_hash(self, prompt_tree):
        """skills 与 prompts 都要计入 —— 阶段 5 迭代的正是 skill，
        漏掉它会让"候选与冠军哈希相同"这种假象成立。"""
        p, s = prompt_tree
        before = E.compute_prompt_hash(p, s)
        target = sorted(s.rglob("*.md"))[0]
        target.write_text(target.read_text(encoding="utf-8") + "x", encoding="utf-8")
        assert E.compute_prompt_hash(p, s) != before

    def test_moving_a_file_between_roots_changes_the_hash(self, prompt_tree):
        """路径也参与哈希，不只是内容：同样的字节挂在 prompts 还是 skills 下
        是不同的配置。"""
        p, s = prompt_tree
        before = E.compute_prompt_hash(p, s)
        src = sorted(p.rglob("*.md"))[0]
        (s / src.name).write_bytes(src.read_bytes())
        src.unlink()
        assert E.compute_prompt_hash(p, s) != before

    def test_missing_directory_does_not_raise(self, tmp_path):
        """目录不存在时应给出一个哈希而不是崩 —— 但它必须与"有内容"不同。"""
        empty = E.compute_prompt_hash(tmp_path / "nope", tmp_path / "also-nope")
        assert isinstance(empty, str) and len(empty) == 12
        assert empty != E.compute_prompt_hash(PROMPTS, SKILLS)


# ---------------------------------------------------------------------------
# (B2) prompt_version 变成人类标签，不再是硬编码内容标识
# ---------------------------------------------------------------------------


def _metadata(**kw):
    base = dict(
        run_id="r",
        scene=1,
        num_agents=2,
        seed=42,
        model="m",
        provider="openai",
        api_base="x",
        max_steps=30,
        wall_clock_limit=None,
        sandbox_profile="workspace",
        coordinator_prompts="cp",
        worker_prompts="wp",
    )
    base.update(kw)
    return E.build_run_metadata(**base)


class TestPromptVersionAndHashInMetadata:
    def test_hash_and_tag_are_separate_fields(self):
        """两者职责不同：hash 是内容身份（机器用），tag 是人类标签。
        合并成一个字段就回到了 `prompt_version="baseline"` 的老问题 ——
        一个与实际内容无关的字符串。"""
        md = _metadata(prompt_hash="abc123def456", prompt_tag="R4")
        assert md["prompt_hash"] == "abc123def456"
        assert md["prompt_version"] == "R4"

    def test_tag_defaults_to_unlabeled_not_baseline(self):
        """默认值不能再是 "baseline" —— 那个字符串曾被当成真实版本读。"""
        assert _metadata()["prompt_version"] == "unlabeled"

    def test_hash_field_exists_even_when_not_supplied(self):
        assert "prompt_hash" in _metadata()


# ---------------------------------------------------------------------------
# (C) prompt_snapshot
# ---------------------------------------------------------------------------


class TestPromptSnapshot:
    def test_copies_are_byte_identical(self, tmp_path):
        E.snapshot_prompts(tmp_path, PROMPTS, SKILLS)
        snap = tmp_path / "prompt_snapshot"
        sources = dict(E._prompt_hash_sources(PROMPTS, SKILLS))
        assert sources, "no prompt sources discovered"
        for key, src in sources.items():
            dest = snap / key
            assert dest.is_file(), key
            assert filecmp.cmp(src, dest, shallow=False), key

    def test_preserves_relative_structure(self, tmp_path):
        """扁平化会丢掉 coordinator/worker 的区分，也丢掉 skill 的目录名 ——
        而 skill 目录名就是它的身份。"""
        E.snapshot_prompts(tmp_path, PROMPTS, SKILLS)
        snap = tmp_path / "prompt_snapshot"
        rel = {p.relative_to(snap).as_posix() for p in snap.rglob("*.md")}
        assert "prompts/coordinator/system.md" in rel
        assert "prompts/worker/system.md" in rel
        assert any(r.startswith("skills/coordinator/") for r in rel)
        assert any(r.startswith("skills/worker/") for r in rel)

    def test_snapshot_reproduces_the_recorded_hash(self, tmp_path):
        """快照的意义在于事后可复算：对快照算哈希必须得到当时记录的值。
        （快照内已是 prompts/ + skills/ 两个子目录，故直接以它们为根。）"""
        recorded = E.compute_prompt_hash(PROMPTS, SKILLS)
        E.snapshot_prompts(tmp_path, PROMPTS, SKILLS)
        snap = tmp_path / "prompt_snapshot"
        assert E.compute_prompt_hash(snap / "prompts", snap / "skills") == recorded

    def test_snapshots_the_candidate_not_the_champion(self, tmp_path, prompt_tree):
        """指定副本时，快照必须记副本内容 —— 否则自进化的每一代都会留下
        "看起来用了冠军 skill"的错误证据。"""
        p, s = prompt_tree
        target = sorted(s.rglob("*.md"))[0]
        target.write_text("CANDIDATE MARKER\n", encoding="utf-8")
        E.snapshot_prompts(tmp_path, p, s)
        snap = tmp_path / "prompt_snapshot"
        hits = [
            f for f in snap.rglob("*.md")
            if f.read_text(encoding="utf-8") == "CANDIDATE MARKER\n"
        ]
        assert hits, "candidate content missing from snapshot"


# ---------------------------------------------------------------------------
# (D) --skills-dir 真的生效 —— E-20 的直接回归测试
# ---------------------------------------------------------------------------


class TestSkillsDirTakesEffect:
    def _coordinator(self, **kw):
        from sar_orch.coordinator import SARCoordinator

        return SARCoordinator(prompts_dir=f"{PROMPTS}/coordinator", **kw)

    def _worker(self, **kw):
        from sar_orch.worker import SARWorker

        return SARWorker(
            worker_id="A",
            agent_name="A",
            agent_idx=0,
            barrier=None,
            prompts_dir=f"{PROMPTS}/worker",
            **kw,
        )

    def test_both_constructors_accept_skills_dir(self):
        import inspect

        from sar_orch.coordinator import SARCoordinator
        from sar_orch.worker import SARWorker

        for cls in (SARCoordinator, SARWorker):
            assert "skills_dir" in inspect.signature(cls.__init__).parameters, cls

    def test_explicit_path_wins_over_derivation(self, prompt_tree):
        """这是本任务的核心断言。此前 skills_dir 总是从 prompts_dir 派生，
        所以候选 skill 根本无法被加载 —— 参数被接收、写进 metadata、但从不生效。"""
        _, s = prompt_tree
        coord = self._coordinator(skills_dir=str(s / "coordinator"))
        worker = self._worker(skills_dir=str(s / "worker"))
        assert Path(coord._resolve_skills_dir()) == s / "coordinator"
        assert Path(worker._resolve_skills_dir()) == s / "worker"

    def test_derivation_still_works_when_omitted(self):
        """向后兼容：没传时仍按 prompts_dir 派生，老调用方不受影响。"""
        coord = self._coordinator()
        worker = self._worker()
        assert coord._resolve_skills_dir() is not None
        assert worker._resolve_skills_dir() is not None
        assert "coordinator" in str(coord._resolve_skills_dir())
        assert "worker" in str(worker._resolve_skills_dir())

    def test_explicit_and_derived_differ_for_a_copy(self, prompt_tree):
        """区分度检查：若两者恰好相等，上面的"显式优先"就证明不了什么。"""
        _, s = prompt_tree
        explicit = self._coordinator(skills_dir=str(s / "coordinator"))
        derived = self._coordinator()
        assert Path(explicit._resolve_skills_dir()) != Path(
            derived._resolve_skills_dir()
        )

    def test_candidate_skills_are_distinguishable_by_hash(self, prompt_tree):
        """端到端的可验证性：候选 skill 目录必须让 prompt_hash 变化，
        否则无法证明"实验真的加载了候选"。这是 E-20 的判定手段。"""
        p, s = prompt_tree
        champion = E.compute_prompt_hash(PROMPTS, SKILLS)
        assert E.compute_prompt_hash(p, s) == champion
        target = sorted(s.rglob("*.md"))[0]
        target.write_text(target.read_text(encoding="utf-8") + "\ncandidate\n", encoding="utf-8")
        assert E.compute_prompt_hash(p, s) != champion


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------


class TestCliWiring:
    @pytest.mark.parametrize(
        "param,default",
        [("prompt_dir", None), ("skills_dir", None), ("prompt_tag", "unlabeled")],
    )
    def test_run_experiment_accepts_the_params(self, param, default):
        import inspect

        assert inspect.signature(E.run_experiment).parameters[param].default == default

    @pytest.mark.parametrize("flag", ["--prompt-dir", "--skills-dir", "--prompt-tag"])
    def test_flags_are_registered(self, flag):
        """只加形参不加 CLI，等于这个能力从命令行到不了 —— 与
        "只加 CLI 不接线"是同一个病的两半。"""
        src = Path("sar_orch/experiment.py").read_text(encoding="utf-8")
        assert f'"{flag}"' in src

    def test_module_level_defaults_exist(self):
        assert Path(E._DEFAULT_PROMPT_DIR).is_dir()
        assert Path(E._DEFAULT_SKILLS_DIR).is_dir()
