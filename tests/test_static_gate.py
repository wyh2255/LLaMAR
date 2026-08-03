"""候选 skill 的静态门禁：跑实验之前的机械检查。

这里的每项检查都是**免费**的（无 LLM 调用、无实验）。这很重要，因为替代方案是
每个 run 花约 87 万 token 之后才发现这个候选本来就不该被跑。

四项检查各自防的东西不同：

- **反作弊**：场景真值不得出现在文本里（见 `leak_dictionary`）
- **词数预算**：锚在**固定**基线上，绝不锚在当前冠军上
- **schema**：改完还得是个能用的 SKILL.md
- **单文件**：一个候选只改一个 skill

词数锚点为什么必须固定：`champion + 10%` 会复利。10 代 +10% 是 **2.59 倍**，
4 代是 1.46 倍 —— 而这恰好就是本仓库 4 轮**人工**编辑产生的 43-64% 增长。
滑动锚点不是在约束增长，是在给增长排时间表。

单文件为什么要机械检查：项目规则是"一次只改一个变量"。没有机械检查，这条规则
只在所有人都记得的时候有效 —— 而一个同时改三个 skill 的候选，其结果无法归因到
任何一个改动上。
"""

from __future__ import annotations

import pytest

from sar_orch.evolve import leak_dictionary as ld
from sar_orch.evolve import static_gate as sg


@pytest.fixture(scope="module")
def leaks():
    return ld.build("SAR/Scenes")


@pytest.fixture(scope="module")
def manifest():
    return sg.build_baseline_manifest("sar_orch/skills")


CLEAN = "# Navigation\n\nMove toward the nearest known target.\n" + "word " * 100
ONE_PATH = ["worker/navigation/SKILL.md"]


def _eval(leaks, **kw):
    base = dict(skill_paths=ONE_PATH, content=CLEAN, baseline_words=300, leaks=leaks)
    base.update(kw)
    return sg.evaluate(**base)


# ---------------------------------------------------------------------------
# 基线清单
# ---------------------------------------------------------------------------


class TestBaselineManifest:
    def test_covers_every_skill(self, manifest):
        assert len(manifest) == 9
        assert "worker/navigation/SKILL.md" in manifest
        assert "coordinator/exploration/SKILL.md" in manifest

    def test_counts_are_plausible(self, manifest):
        assert all(150 < v < 500 for v in manifest.values()), manifest

    def test_keys_are_relative(self, manifest):
        """相对路径 —— 树被拷到候选目录后清单仍然有效。"""
        assert all(not k.startswith("/") for k in manifest)
        assert all("sar_orch" not in k for k in manifest)

    def test_is_deterministic(self):
        a = sg.build_baseline_manifest("sar_orch/skills")
        b = sg.build_baseline_manifest("sar_orch/skills")
        assert a == b


# ---------------------------------------------------------------------------
# 单文件
# ---------------------------------------------------------------------------


class TestSingleFile:
    def test_one_file_passes(self, leaks):
        assert _eval(leaks).ok

    def test_two_files_rejected(self, leaks):
        v = _eval(leaks, skill_paths=["a/SKILL.md", "b/SKILL.md"])
        assert not v.ok
        assert any("more than one skill file" in r for r in v.rejections)

    def test_zero_files_rejected(self, leaks):
        v = _eval(leaks, skill_paths=[])
        assert not v.ok

    def test_duplicate_path_is_still_one_file(self, leaks):
        """同一路径列两次不算改了两个文件。"""
        assert _eval(leaks, skill_paths=ONE_PATH * 3).ok

    def test_reason_explains_attribution(self, leaks):
        """拒绝理由必须说明**为什么**，否则下一代会重犯。"""
        v = _eval(leaks, skill_paths=["a/SKILL.md", "b/SKILL.md"])
        assert any("attribut" in r for r in v.rejections)


# ---------------------------------------------------------------------------
# 词数预算
# ---------------------------------------------------------------------------


class TestWordBudget:
    def test_within_tolerance_passes(self, leaks):
        text = "# T\n" + "word " * 320
        assert _eval(leaks, content=text, baseline_words=300).ok

    def test_over_tolerance_rejected(self, leaks):
        text = "# T\n" + "word " * 400
        v = _eval(leaks, content=text, baseline_words=300)
        assert not v.ok
        assert any("exceeds baseline" in r for r in v.rejections)

    def test_absolute_ceiling_applies_even_with_a_large_baseline(self, leaks):
        """百分比容差不能被"基线本来就很大"利用。"""
        text = "# T\n" + "word " * 700
        v = _eval(leaks, content=text, baseline_words=5000)
        assert not v.ok
        assert any("absolute ceiling" in r for r in v.rejections)

    def test_missing_baseline_is_rejected_not_waived(self, leaks):
        """没有基线是**配置错误**，不是通过。当成通过就等于放行一个无界候选。"""
        v = _eval(leaks, baseline_words=None)
        assert not v.ok
        assert any("no baseline word count" in r for r in v.rejections)

    def test_rejection_states_the_compounding_rationale(self, leaks):
        """把"为什么锚点是固定的"写进拒绝理由 —— 否则下一个人会把它改成
        champion-relative，而那看起来更"自然"。"""
        v = _eval(leaks, content="# T\n" + "word " * 400, baseline_words=300)
        msg = " ".join(v.rejections)
        assert "compound" in msg
        assert "2.59" in msg  # 10 代的实际倍数

    def test_shrinking_is_always_allowed(self, leaks):
        """瘦身是目标之一，不该被预算检查拦住。"""
        assert _eval(leaks, content="# T\nBe brief.", baseline_words=300).ok


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_empty_rejected(self, leaks):
        assert not _eval(leaks, content="   \n  ").ok

    def test_missing_heading_rejected(self, leaks):
        v = _eval(leaks, content="just prose with no heading at all")
        assert any("SKILL.md" in r for r in v.rejections)

    def test_nul_byte_rejected(self, leaks):
        v = _eval(leaks, content="# T\nbody\x00more")
        assert any("NUL" in r for r in v.rejections)

    def test_heading_may_be_preceded_by_blank_lines(self, leaks):
        assert _eval(leaks, content="\n\n# Title\n\nbody").ok


# ---------------------------------------------------------------------------
# 反作弊
# ---------------------------------------------------------------------------


class TestAntiCheating:
    def test_scene_coordinate_rejected(self, leaks):
        v = _eval(leaks, content="# Nav\nHead to (15,5) immediately.")
        assert not v.ok
        assert v.leak_hits

    def test_entity_name_rejected(self, leaks):
        v = _eval(leaks, content="# Nav\nRescue LostPersonTimmy first.")
        assert not v.ok

    def test_engine_symbol_rejected(self, leaks):
        v = _eval(leaks, content="# Nav\nWeigh find_probability when choosing.")
        assert not v.ok

    def test_empty_leak_dictionary_refuses_rather_than_passes(self):
        """空字典意味着场景目录读不到 —— **什么都没检查**。
        此时放行等于静默关掉反作弊，比直接停下更糟。"""
        v = sg.evaluate(
            skill_paths=ONE_PATH,
            content=CLEAN,
            baseline_words=300,
            leaks=ld.LeakDictionary(),
        )
        assert not v.ok
        assert any("did not run" in r for r in v.rejections)

    def test_hits_are_exposed_for_the_record(self, leaks):
        """命中项要留在 verdict 上，供拒绝历史使用 —— 下一代要能看到具体命中
        了什么，否则只会换个说法再犯。"""
        v = _eval(leaks, content="# Nav\nGo to (15,5) for LostPersonTimmy.")
        assert len(v.leak_hits) >= 2


# ---------------------------------------------------------------------------
# 报告方式
# ---------------------------------------------------------------------------


class TestVerdictReporting:
    def test_all_problems_reported_together(self, leaks):
        """不短路：一次只给一个拒绝理由，生成器就需要一代修一个问题。"""
        v = sg.evaluate(
            skill_paths=["a/SKILL.md", "b/SKILL.md"],
            content="(15,5) " + "word " * 400,
            baseline_words=300,
            leaks=leaks,
        )
        assert len(v.rejections) >= 3

    def test_summary_is_readable_in_both_outcomes(self, leaks):
        assert "PASS" in _eval(leaks).summary()
        assert "REJECT" in _eval(leaks, content="").summary()

    def test_pass_has_no_rejections(self, leaks):
        v = _eval(leaks)
        assert v.ok and v.rejections == []
