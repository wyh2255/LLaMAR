"""反作弊：候选 skill 文本与场景真值的交叉扫描。

威胁不是恶意的生成器，是**称职**的生成器：一个被要求改进 skill 的 agent 自然会
想理解问题，而读 `SAR/Scenes/scene_*.py` 是最直接的方式。读过之后，它能写出一条
对这些场景确实有用、但作为通用策略毫无价值的规则 —— "优先处理地图左下的蓄水池"
就是 `position=(15,5)` 换了个说法。

**预防优先**：首选不给候选生成器活的文件系统访问（inline 喂文本），
本模块是漏网之后的**检测**层。三层信号强度不同：

1. **engine 内部参数名**（`extra_load`/`find_probability`/`enclosing_grid`/
   `amt_light`）—— 这些是 `Arg()` 构造关键字，agent 经 observation
   **不可能**合法获知，出现即是"读过场景文件"的实锤，比人名/坐标信号更强
2. **实体名** —— agent 运行时确实能看到，故不必然是泄漏，但同样不该写进通用策略
3. **坐标** —— 带容差匹配，因为"写成相邻格子"是最显然的规避手段

扫描范围**必须覆盖** fenced code block 与 inline code：为降低误报而先 strip
代码围栏是很自然的实现选择，但那恰好把代码块变成唯一可靠的藏匿处。
"""

from __future__ import annotations

import pytest

from sar_orch.evolve import leak_dictionary as ld


@pytest.fixture(scope="module")
def leaks():
    return ld.build("SAR/Scenes")


# ---------------------------------------------------------------------------
# 提取
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_finds_entity_names(self, leaks):
        assert "LostPersonTimmy" in leaks.names
        assert "ReservoirUtah" in leaks.names
        assert "CaldorFire" in leaks.names

    def test_finds_coordinates(self, leaks):
        assert (15, 5) in leaks.coordinates
        assert (2, 2) in leaks.coordinates

    def test_covers_every_scene(self, leaks):
        for n in range(1, 6):
            assert f"scene_{n}.py" in leaks.scene_files

    def test_does_not_execute_or_import_the_scenes(self):
        """用 `ast` 解析而不 import：场景模块会 `from core import Controller`，
        拉起整个引擎，而这个门禁必须能在模拟器不可导入时运行。
        顺带也意味着读文件不执行代码。"""
        import inspect

        src = inspect.getsource(ld.build)
        assert "ast.parse" in src
        assert "import_module" not in src

    def test_unparseable_scene_yields_no_entries_not_a_crash(self, tmp_path):
        (tmp_path / "scene_x.py").write_text("def broken(:\n", encoding="utf-8")
        got = ld.build(tmp_path)
        assert got.names == frozenset()

    def test_empty_dictionary_is_detectable(self, tmp_path):
        """空字典必须可判别 —— 调用方要能区分"没东西可查"与"查过没发现"，
        否则场景目录路径写错会静默变成"全部通过"。"""
        assert ld.build(tmp_path).is_empty()
        assert not ld.build("SAR/Scenes").is_empty()


# ---------------------------------------------------------------------------
# 检测：真实的规避手法
# ---------------------------------------------------------------------------


class TestDetection:
    def test_clean_generalisation_passes(self, leaks):
        """通用策略必须不被误伤，否则门禁会拦下所有候选。"""
        text = (
            "When a fire is discovered, send two agents: one to fetch the matching "
            "supply type, another to contain the spread before it reaches MEDIUM."
        )
        assert ld.scan_text(text, leaks) == []

    def test_exact_entity_name_is_blocked(self, leaks):
        hits = ld.scan_text("Rescue LostPersonTimmy first.", leaks)
        assert any(h.kind == "entity_name" for h in hits)

    def test_misspelled_name_is_blocked(self, leaks):
        """扎住"改一两个字的人名"。"""
        hits = ld.scan_text("Rescue LostPersonTimy first.", leaks)
        assert any("near match" in h.detail for h in hits)

    def test_exact_coordinate_is_blocked(self, leaks):
        hits = ld.scan_text("The reservoir sits at (15,5).", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_off_by_one_coordinate_is_blocked(self, leaks):
        """精确匹配可被"写成相邻格子"轻易绕过，故用 ±1 容差。"""
        hits = ld.scan_text("The reservoir sits at (16,5).", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_coordinate_inside_a_fenced_block_is_blocked(self, leaks):
        """先 strip 代码围栏是很自然的实现选择，但那会让代码块成为盲区 ——
        而"把坐标藏进 Example: 代码块"正是评审给出的绕过手法之一。"""
        text = "Example:\n```python\nnavigate_to(position=(2,2))\n```\n"
        assert any(h.kind == "coordinate" for h in ld.scan_text(text, leaks))

    def test_coordinate_in_inline_code_is_blocked(self, leaks):
        assert any(
            h.kind == "coordinate"
            for h in ld.scan_text("Go to `(15,5)` immediately.", leaks)
        )

    @pytest.mark.parametrize(
        "sym", ["extra_load", "find_probability", "enclosing_grid", "amt_light"]
    )
    def test_engine_symbols_are_blocked(self, sym, leaks):
        """这些是 `Arg()` 关键字，agent 经 observation 不可能合法获知。"""
        hits = ld.scan_text(f"Consider {sym} when planning.", leaks)
        assert any(h.kind == "engine_symbol" for h in hits)

    def test_engine_symbol_outranks_weaker_signals(self, leaks):
        """报告顺序按信号强度：engine 符号是实锤，应当排在最前。"""
        text = "Use find_probability near (15,5) for LostPersonTimmy."
        hits = ld.scan_text(text, leaks)
        assert hits[0].kind == "engine_symbol"

    def test_unrelated_coordinates_are_not_flagged(self, leaks):
        """容差不能宽到把任意数字对都算成泄漏。"""
        hits = ld.scan_text("A 3x3 grid spans (0,0) to (2,2)... ", leaks)
        coords = [h for h in hits if h.kind == "coordinate"]
        # (2,2) 确实是场景字面量，应命中；(0,0) 不该凭空产生额外命中。
        assert len(coords) <= 1

    def test_short_names_do_not_trigger_fuzzy_noise(self, leaks):
        """短名的近似匹配巧合太多，反而制造误报。"""
        assert ld._near_token("the cat sat", "bob", 1) is None


# ---------------------------------------------------------------------------
# 检测：审查实测确认的两个绕过缺口 + 后续探测发现的规避手法
# ---------------------------------------------------------------------------


class TestConfirmedGapsAndNewEvasions:
    """审查实测确认 `_COORD_RE` 与 `_near_token` 各有一个可复现的绕过缺口：

    1. 坐标写成算术式/十六进制（`(3*5,5)`、`(0xF,5)`），`_COORD_RE` 只认字面
       十进制数字，零命中。
    2. 实体名按空格/连字符/下划线拆写（`Lost Person Timmy` 等），`_near_token`
       对整段 token 做编辑距离，拆分后每个 token 都离原名太远，零命中。

    本类同时覆盖后续探测发现的规避手法：方括号坐标、`x=,y=` 坐标、
    大小写变形、逐字母插入符号、零宽字符、全角字符。
    """

    # -- 缺口 1：算术式 / 十六进制坐标 ------------------------------------

    def test_arithmetic_coordinate_is_blocked(self, leaks):
        """`(3*5,5)` 等价于 `(15,5)`，修补前零命中。"""
        hits = ld.scan_text("The reservoir sits at (3*5,5).", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_hex_coordinate_is_blocked(self, leaks):
        """`(0xF,5)` 等价于 `(15,5)`，修补前零命中。"""
        hits = ld.scan_text("The reservoir sits at (0xF,5).", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_arithmetic_coordinate_with_subtraction_is_blocked(self, leaks):
        hits = ld.scan_text("Navigate to (20-5,5) for supplies.", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_unrelated_arithmetic_prose_is_not_flagged(self, leaks):
        """算术检测的宽度必须卡住，不能把步数说明这类普通数学表达当成坐标。
        评估器只认"一个运算符连接两个数字"的 term，且必须在括号里成对出现 ——
        单独的 `3-10 steps` 或 `1-2 agents` 没有括号包裹，不触发。"""
        text = (
            "Small-team rule (2-3 agents): the standard opening is one agent "
            "per fire, budget 3-10 steps for the round trip."
        )
        assert ld.scan_text(text, leaks) == []

    # -- 缺口 2：实体名拆写 ------------------------------------------------

    def test_space_split_name_is_blocked(self, leaks):
        """`Lost Person Timmy`，修补前零命中。"""
        hits = ld.scan_text("Rescue Lost Person Timmy first.", leaks)
        assert any(h.kind == "entity_name" for h in hits)

    def test_hyphen_split_name_is_blocked(self, leaks):
        """`Lost-Person-Timmy`，修补前零命中。"""
        hits = ld.scan_text("Rescue Lost-Person-Timmy first.", leaks)
        assert any(h.kind == "entity_name" for h in hits)

    def test_underscore_split_name_is_blocked(self, leaks):
        hits = ld.scan_text("Rescue Lost_Person_Timmy first.", leaks)
        assert any(h.kind == "entity_name" for h in hits)

    def test_reordered_words_are_not_flagged(self, leaks):
        """拆写检测要求词序与原名一致、连续出现 —— 词袋式匹配会把任何同时
        提到"lost"和"person"的无关句子都当成泄漏，那本身就是新的误报源。"""
        assert ld.scan_text("Timmy Person Lost", leaks) == []

    def test_unrelated_words_in_prose_are_not_flagged(self, leaks):
        """通用文本里"lost"和"person"分别出现在不相邻的位置，不该触发。"""
        text = "If a person is lost, report it immediately to the team."
        assert ld.scan_text(text, leaks) == []

    # -- 新发现的规避手法 ---------------------------------------------------

    def test_bracket_coordinate_is_blocked(self, leaks):
        """`[15,5]` 用方括号代替圆括号。"""
        hits = ld.scan_text("The reservoir sits at [15,5].", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_xy_keyword_coordinate_is_blocked(self, leaks):
        """`x=15, y=5` 写法。"""
        hits = ld.scan_text("Navigate to x=15, y=5 for the reservoir.", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_bracket_coordinate_near_miss_is_not_flagged(self, leaks):
        """方括号/`x=,y=` 坐标只做精确匹配，不带容差 —— 这是权衡后的选择：
        通用 skill 文本里方括号三元组很常见（如 JSON 示例里的
        `"position": [5, 4, 0]`），对这类新增格式套用和圆括号一样的 ±1
        容差会把普通示例数据也算成"接近某个场景坐标"，实测已经命中过一次。
        `[5,4]` 离场景真实坐标 (5,5) 只差 1，若套用容差会被误伤；精确匹配下
        不应命中。"""
        hits = ld.scan_text("Example position: [5,4,0]", leaks)
        assert not any(h.kind == "coordinate" for h in hits)

    def test_fullwidth_coordinate_is_blocked(self, leaks):
        """全角括号/数字/逗号：（１５，５）。NFKC 归一化后等价于 (15,5)。"""
        hits = ld.scan_text("水库在（１５，５）附近。", leaks)
        assert any(h.kind == "coordinate" for h in hits)

    def test_uppercase_name_is_blocked(self, leaks):
        """全大写变形：LOSTPERSONTIMMY。原本大小写不敏感匹配已经覆盖，
        这里确认没有被新逻辑意外破坏。"""
        hits = ld.scan_text("RESCUE LOSTPERSONTIMMY FIRST.", leaks)
        assert any(h.kind == "entity_name" for h in hits)

    def test_dotted_letter_obfuscated_name_is_blocked(self, leaks):
        """逐字母插入符号：L.o.s.t.P.e.r.s.o.n.T.i.m.m.y。"""
        hits = ld.scan_text(
            "Rescue L.o.s.t.P.e.r.s.o.n.T.i.m.m.y first.", leaks
        )
        assert any(h.kind == "entity_name" for h in hits)

    def test_zero_width_char_in_name_is_blocked(self, leaks):
        """零宽字符（U+200B）插在名字中间；NFKC 之外单独 strip Cf 类字符。"""
        text = "Rescue Lost​Person​Timmy first."
        hits = ld.scan_text(text, leaks)
        assert any(h.kind == "entity_name" for h in hits)

    def test_letter_obfuscated_engine_symbol_is_blocked(self, leaks):
        """engine 符号也可能被逐字母拆写：e.x.t.r.a._.l.o.a.d。"""
        hits = ld.scan_text(
            "Consider e.x.t.r.a._.l.o.a.d when planning.", leaks
        )
        assert any(h.kind == "engine_symbol" for h in hits)

    def test_html_comment_hidden_coordinate_is_blocked(self, leaks):
        """坐标藏进 HTML 注释——原有实现已扫描全文（不 strip 任何标记），
        这条确认新坐标格式在这种藏匿方式下依然生效。"""
        text = "Strategy text.\n<!-- position=(15,5) -->\nMore text.\n"
        hits = ld.scan_text(text, leaks)
        assert any(h.kind == "coordinate" for h in hits)


# ---------------------------------------------------------------------------
# 误报回归：通用策略文本必须仍然干净
# ---------------------------------------------------------------------------


class TestFalsePositiveRegression:
    """修补检测规则最常见的失败模式是把误报率推高到门禁不可用。这里用真实
    仓库素材做回归：不能因为新增的拆写/算术式/方括号检测而误伤正常内容。"""

    def test_clean_generalisation_still_passes(self, leaks):
        text = (
            "When a fire is discovered, send two agents: one to fetch the "
            "matching supply type, another to contain the spread before it "
            "reaches MEDIUM."
        )
        assert ld.scan_text(text, leaks) == []

    def test_step_budget_prose_with_ranges_is_not_flagged(self, leaks):
        """`sar_orch/skills/coordinator/step-budget-management/SKILL.md` 风格
        的步数区间说明（`50-35`、`1-2`、`35-20`、`20-10`）不该被算术式坐标
        检测误伤——它们没有被括号包裹，也不是逗号分隔的坐标对。"""
        text = (
            "Budget 50-35 steps for early exploration, 1-2 agents per fire, "
            "35-20 steps for mid mission, 20-10 steps for late mission."
        )
        assert ld.scan_text(text, leaks) == []

    def test_json_example_with_bracket_list_is_not_flagged(self, leaks):
        """`sar_orch/skills/worker/observation-reporting/SKILL.md` 风格的
        report_observation() JSON 示例，方括号坐标 `[5, 4, 0]` 精确匹配下
        不命中（场景里没有这个坐标；(5,5) 是相邻但不相等）。"""
        text = (
            'Call report_observation() with structured JSON fields:\n'
            '```json\n'
            '{\n'
            '  "object_type": "fire",\n'
            '  "name": "SomeFire_Region_3",\n'
            '  "position": [5, 4, 0],\n'
            '  "attributes": {"intensity": "medium", "type": "chemical"}\n'
            '}\n'
            '```\n'
        )
        assert ld.scan_text(text, leaks) == []

    def test_generic_fire_suppression_prose_is_not_flagged(self, leaks):
        """真实 fire-suppression skill 里"load per trip"这类通用措辞，
        不能被 engine 符号 `extra_load` 的拆写检测误伤 —— "load"单独出现，
        前面不是"extra"。"""
        text = (
            "Full load per trip: tell agents to fill all 3 inventory slots; "
            "each burning cell costs about 1 unit per intensity notch."
        )
        assert ld.scan_text(text, leaks) == []

    def test_generic_position_language_is_not_flagged(self, leaks):
        """"x,y" 占位符写法（提示词里教工具调用格式用的）不该被 x=,y= 坐标
        检测误伤——没有具体数值可评估。"""
        text = 'Tell Bob to rescue the person at position (x,y).'
        assert ld.scan_text(text, leaks) == []

    def test_generic_ratio_language_is_not_flagged(self, leaks):
        """"one agent, one fire"这类通用措辞不该被实体名拆写检测误伤。"""
        text = "One agent, one mission: assign each agent a single clear task."
        assert ld.scan_text(text, leaks) == []


class TestKnownLimitation:
    def test_paraphrased_position_is_not_detectable(self, leaks):
        """**这条测试记录的是能力边界，不是缺陷。**

        "地图左下的蓄水池"在文本上与任何字面量都不匹配，事后正则对语义改写
        基本无效。故本模块**不是**主要防线：
          - 首选预防 —— 不给候选生成器读 `SAR/` 的能力
          - 兜底靠留出组 —— 过拟合到已见场景的候选会在未见组上暴露
        把这条写成测试，是为了防止有人以为扫描器覆盖了这种情况。
        """
        text = "Head to the reservoir in the lower-left quadrant early."
        assert ld.scan_text(text, leaks) == []

    def test_slash_coordinate_is_not_detectable(self, leaks):
        """坐标写成分数形式 `15/5`：故意不覆盖。斜杠在这个语料里普遍用来写
        比例和步数区间（`3/10`、`1/2`），把它当成坐标分隔符会把这类通用
        表达也算成潜在泄漏，误报面比收益大。"""
        text = "The reservoir sits at 15/5."
        assert ld.scan_text(text, leaks) == []

    def test_english_number_words_are_not_detectable(self, leaks):
        """坐标拼成英文数字单词 `fifteen, five`：故意不覆盖。场景坐标范围
        到 30，完整覆盖需要一套英文数字词法（含 "twenty-five" 这类复合词），
        规则复杂且用词与日常英语重叠度高，误报风险不成比例；LLM 把坐标写成
        单词的概率本身也低。"""
        text = "The reservoir sits at fifteen, five."
        assert ld.scan_text(text, leaks) == []

    def test_coordinate_split_across_sentences_is_not_detectable(self, leaks):
        """坐标拆到相邻两句话里：`x 是 15。y 是 5。` 故意不覆盖 —— 要抓住
        这个手法需要在任意两个数字之间做跨句关联，没有一个明确的"这是坐标对"
        锚点，覆盖面会失控地宽。"""
        text = "The x coordinate is 15. The y coordinate is 5."
        assert ld.scan_text(text, leaks) == []


class TestEditDistance:
    @pytest.mark.parametrize(
        "a,b,limit,expected",
        [
            ("abc", "abc", 0, True),
            ("abc", "abd", 1, True),
            ("abc", "xyz", 1, False),
            ("timmy", "timy", 1, True),
            ("reservoirutah", "reservoirutah", 0, True),
        ],
    )
    def test_within(self, a, b, limit, expected):
        assert ld._edit_distance_within(a, b, limit) is expected

    def test_length_gap_short_circuits(self):
        assert ld._edit_distance_within("a", "aaaaaaa", 1) is False
