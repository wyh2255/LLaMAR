"""compare_reports.py 的单测（DESIGN 2.0）。

这个脚本是阶段 2 全部验收的**唯一依据** —— 若它漏检，整个阶段 2 的
"离线复算数值不变"就失去意义。故必须覆盖：嵌套 dict 字段变化、list 元素
增删、类型变化（0 / null / "0" / False 互不相同）、新增与删除键。

比较用**精确相等**，不设浮点容差：阶段 2 的改动全是整数计数或新增字段，
没有浮点漂移来源，容差只会掩盖真实差异。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "workspace"
    / ".plan"
    / "compare_reports.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("compare_reports", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load_module()


def _paths(base, curr, **kw):
    return [p for p, _, _ in cr.diff_json(base, curr, **kw)]


def _triples(base, curr, **kw):
    return list(cr.diff_json(base, curr, **kw))


# --------------------------------------------------------------------------
# 相同 → 零差异
# --------------------------------------------------------------------------


def test_identical_reports_have_no_diff():
    doc = {"episode": {"coverage": 1.0, "finished": True}, "violations": [{"rule": "a"}]}
    assert _paths(doc, json.loads(json.dumps(doc))) == []


def test_dict_key_order_is_irrelevant():
    assert _paths({"a": 1, "b": 2}, {"b": 2, "a": 1}) == []


# --------------------------------------------------------------------------
# 嵌套 dict
# --------------------------------------------------------------------------


def test_nested_dict_value_change_is_detected():
    base = {"episode": {"metrics": {"coverage": 0.5}}}
    curr = {"episode": {"metrics": {"coverage": 0.6}}}
    assert _paths(base, curr) == ["episode/metrics/coverage"]


def test_deeply_nested_change_reports_full_path():
    base = {"a": {"b": {"c": {"d": 1}}}}
    curr = {"a": {"b": {"c": {"d": 2}}}}
    (path, b, c), = _triples(base, curr)
    assert (path, b, c) == ("a/b/c/d", 1, 2)


# --------------------------------------------------------------------------
# 新增 / 删除键
# --------------------------------------------------------------------------


def test_added_key_is_detected_as_absent_baseline():
    (path, b, c), = _triples({"a": 1}, {"a": 1, "b": 2})
    assert path == "b"
    assert b is cr._MISSING
    assert c == 2


def test_removed_key_is_detected_as_absent_current():
    (path, b, c), = _triples({"a": 1, "b": 2}, {"a": 1})
    assert path == "b"
    assert b == 2
    assert c is cr._MISSING


def test_added_nested_key_is_detected():
    base = {"detail": {"parse_miss_counts": {"names": 0}}}
    curr = {"detail": {"parse_miss_counts": {"names": 0, "inventory_before": 0}}}
    assert _paths(base, curr) == ["detail/parse_miss_counts/inventory_before"]


# --------------------------------------------------------------------------
# 类型变化 —— 0 / null / "0" / False 必须互相区分
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_val,curr_val",
    [
        (0, None),
        (0, "0"),
        (0, False),
        (None, "0"),
        (None, False),
        ("0", False),
        (0, 0.0),
        (1, 1.0),
        ([], {}),
        ("", None),
    ],
)
def test_type_changes_are_never_folded(base_val, curr_val):
    assert _paths({"k": base_val}, {"k": curr_val}) == ["k"]


def test_type_change_at_root_is_reported():
    assert _paths({"a": 1}, [1]) == ["<root>"]


def test_formatter_distinguishes_same_looking_values():
    """报告文本必须让 0 / null / "0" / False 看起来不同。"""
    rendered = {cr._fmt(v) for v in (0, None, "0", False, 0.0)}
    assert len(rendered) == 5


def test_formatter_marks_absent():
    assert cr._fmt(cr._MISSING) == "<absent>"


# --------------------------------------------------------------------------
# list 增删与逐元素
# --------------------------------------------------------------------------


def test_list_element_change_is_detected():
    base = {"violations": [{"rule": "empty_supply_use"}]}
    curr = {"violations": [{"rule": "full_inventory_get"}]}
    assert _paths(base, curr) == ["violations/0/rule"]


def test_list_append_reports_length_and_new_element():
    base = {"v": [1]}
    curr = {"v": [1, 2]}
    assert _paths(base, curr) == ["v/<len>", "v/1"]


def test_list_removal_reports_length_and_missing_element():
    base = {"v": [1, 2]}
    curr = {"v": [1]}
    paths = _triples(base, curr)
    assert [p for p, _, _ in paths] == ["v/<len>", "v/1"]
    assert paths[1][2] is cr._MISSING


def test_list_reorder_is_a_difference():
    """顺序变化是真实差异 —— grader 输出顺序应当稳定。"""
    assert _paths({"v": [1, 2]}, {"v": [2, 1]}) == ["v/0", "v/1"]


def test_empty_list_to_populated():
    assert _paths({"v": []}, {"v": [1]}) == ["v/<len>", "v/0"]


def test_multiple_independent_diffs_all_reported():
    base = {"a": 1, "b": {"c": 2}, "d": [1, 2]}
    curr = {"a": 9, "b": {"c": 2}, "d": [1, 3]}
    assert _paths(base, curr) == ["a", "d/1"]


# --------------------------------------------------------------------------
# ignore 前缀
# --------------------------------------------------------------------------


def test_ignore_suppresses_exact_path():
    assert _paths({"generated_at": "x"}, {"generated_at": "y"}, ignore=("generated_at",)) == []


def test_ignore_suppresses_subtree():
    base = {"meta": {"generated_at": "x", "keep": 1}}
    curr = {"meta": {"generated_at": "y", "keep": 2}}
    assert _paths(base, curr, ignore=("meta/generated_at",)) == ["meta/keep"]


def test_ignore_does_not_suppress_prefix_lookalike():
    """`generated_at` 不应连带忽略 `generated_at_extra`。"""
    base = {"generated_at_extra": 1}
    curr = {"generated_at_extra": 2}
    assert _paths(base, curr, ignore=("generated_at",)) == ["generated_at_extra"]


def test_ignore_suppresses_added_key():
    assert _paths({}, {"generated_at": "y"}, ignore=("generated_at",)) == []


# --------------------------------------------------------------------------
# CLI 行为与退出码
# --------------------------------------------------------------------------


def _write(path: Path, doc) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_cli_returns_zero_when_identical(tmp_path, capsys):
    doc = {"episode": {"coverage": 1.0}}
    b = _write(tmp_path / "b.json", doc)
    c = _write(tmp_path / "c.json", doc)
    rc = cr.main(["somerun", "--baseline", str(b), "--current", str(c)])
    assert rc == 0
    assert "NO DIFFERENCES" in capsys.readouterr().out


def test_cli_returns_one_when_different(tmp_path, capsys):
    b = _write(tmp_path / "b.json", {"episode": {"coverage": 1.0}})
    c = _write(tmp_path / "c.json", {"episode": {"coverage": 0.5}})
    rc = cr.main(["somerun", "--baseline", str(b), "--current", str(c)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 DIFFERENCE(S)" in out
    assert "episode/coverage" in out


def test_cli_ignores_timestamps_by_default(tmp_path, capsys):
    b = _write(tmp_path / "b.json", {"generated_at": "2026-08-01", "x": 1})
    c = _write(tmp_path / "c.json", {"generated_at": "2026-08-02", "x": 1})
    assert cr.main(["r", "--baseline", str(b), "--current", str(c)]) == 0
    capsys.readouterr()


def test_cli_no_ignore_flag_surfaces_timestamps(tmp_path, capsys):
    b = _write(tmp_path / "b.json", {"generated_at": "2026-08-01", "x": 1})
    c = _write(tmp_path / "c.json", {"generated_at": "2026-08-02", "x": 1})
    rc = cr.main(["r", "--baseline", str(b), "--current", str(c), "--no-ignore"])
    assert rc == 1
    assert "generated_at" in capsys.readouterr().out


# --------------------------------------------------------------------------
# --ignore：声明比较协议差异（如基线含 judge 输出、复算 --no-llm-judge）
# --------------------------------------------------------------------------


def _judge_pair(tmp_path):
    b = _write(tmp_path / "b.json", {"llm_judge": {"verdict": "ok"}, "x": 1})
    c = _write(tmp_path / "c.json", {"llm_judge": {"verdict": "skipped"}, "x": 1})
    return b, c


def test_extra_ignore_suppresses_declared_subtree(tmp_path, capsys):
    b, c = _judge_pair(tmp_path)
    rc = cr.main(["r", "--baseline", str(b), "--current", str(c), "--ignore", "llm_judge"])
    assert rc == 0
    assert "extra ignore: llm_judge" in capsys.readouterr().out


def test_extra_ignore_accepts_comma_separated_and_repeats(tmp_path, capsys):
    b = _write(tmp_path / "b.json", {"llm_judge": 1, "conclusion": "a", "x": 1})
    c = _write(tmp_path / "c.json", {"llm_judge": 2, "conclusion": "b", "x": 1})
    assert cr.main(
        ["r", "--baseline", str(b), "--current", str(c), "--ignore", "llm_judge,conclusion"]
    ) == 0
    capsys.readouterr()
    assert cr.main(
        [
            "r", "--baseline", str(b), "--current", str(c),
            "--ignore", "llm_judge", "--ignore", "conclusion",
        ]
    ) == 0
    capsys.readouterr()


def test_extra_ignore_does_not_hide_other_diffs(tmp_path, capsys):
    """声明忽略 judge 不得连带压掉真实指标差异。"""
    b = _write(tmp_path / "b.json", {"llm_judge": 1, "violation_count": 65})
    c = _write(tmp_path / "c.json", {"llm_judge": 2, "violation_count": 0})
    rc = cr.main(["r", "--baseline", str(b), "--current", str(c), "--ignore", "llm_judge"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "violation_count" in out
    assert "llm_judge" not in out.split("DIFFERENCE(S)")[1]


def test_judge_is_not_ignored_by_default(tmp_path, capsys):
    """DEFAULT_IGNORE 不含 judge —— 否则工具会对真实 judge 变化永久失明。"""
    assert not any("judge" in p for p in cr.DEFAULT_IGNORE)
    b, c = _judge_pair(tmp_path)
    assert cr.main(["r", "--baseline", str(b), "--current", str(c)]) == 1
    capsys.readouterr()


def test_extra_ignore_still_applies_under_no_ignore(tmp_path, capsys):
    """--no-ignore 关掉默认集，但显式 --ignore 仍生效。"""
    b = _write(tmp_path / "b.json", {"generated_at": "x", "llm_judge": 1})
    c = _write(tmp_path / "c.json", {"generated_at": "y", "llm_judge": 2})
    rc = cr.main(
        ["r", "--baseline", str(b), "--current", str(c), "--no-ignore", "--ignore", "llm_judge"]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "generated_at" in out
    assert "llm_judge" not in out.split("DIFFERENCE(S)")[1]


def test_cli_exits_two_on_missing_file(tmp_path):
    c = _write(tmp_path / "c.json", {})
    with pytest.raises(SystemExit) as exc:
        cr.main(["r", "--baseline", str(tmp_path / "nope.json"), "--current", str(c)])
    assert exc.value.code == 2


def test_cli_exits_two_on_invalid_json(tmp_path):
    b = tmp_path / "b.json"
    b.write_text("{not json", encoding="utf-8")
    c = _write(tmp_path / "c.json", {})
    with pytest.raises(SystemExit) as exc:
        cr.main(["r", "--baseline", str(b), "--current", str(c)])
    assert exc.value.code == 2
