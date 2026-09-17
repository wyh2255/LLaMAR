"""tool descriptions 运行时覆盖层（reef × sar_orch workflow §B3，2026-09-17）。

契约（与 prompt 外置化的 fail-closed **相反**，工具描述是 fail-soft 覆盖层）：

1. 覆盖层生效：``sar_orch/tools/descriptions.json`` 的 ``description`` /
   ``parameters`` 覆盖工具类同名类属性，并进入 ``to_openai_schema()``
   （LLM 真正看到的内容）；
2. 文件缺失 / 不可读 / JSON 非法 / 键缺失 → 完全回退代码内联值，默认行为逐字节
   不变（工具描述是演化目标，永远不该让一次 run 起不来）；
3. 部分键覆盖：只覆盖文件中出现的工具；条目只带一个字段时，另一字段保留内联值；
4. 键解析：名字在 worker/coordinator 两 scope 间唯一时用裸名，跨 scope 重名
   （``finish_task``）用 ``<scope>/<name>``；查找先 scope 限定键、后裸名；
5. 语料不变量：文件条目数 == ``sar_orch/tools/{worker,coordinator}/`` 下实际
   工具类数（本卡快照 22），每个工具类都有键。

为什么本文件不做「文件 == 内联值」的逐字节断言：全量阶段该文件本身就是 reef
变异目标（descriptor 第二 config target ``tools``），promote/pull 之后文件必然
与代码内联值不同——那是正常状态而非回归。等值核对由离线工具
``python sar_orch/tools/export_descriptions.py --verify`` 在外置化时点执行；
本文件改为断言「类属性 == 当前文件内容」，该不变量在变异前后都成立。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from a2a.utils import prompt_loader
from a2a.utils.prompt_loader import PromptLoadError
from Agent.router_agent.tools.base import Tool
from sar_orch.tools import descriptions_loader
from sar_orch.tools.coordinator import FinishTaskTool as CoordinatorFinishTaskTool
from sar_orch.tools.coordinator import (
    QueryControlJournalTool,
    QueryProjectionTool,
    QuerySARStateTool,
    QuerySemanticMapTool,
    QuerySupervisionTool,
    QueryTeamStatusTool,
    QueryTemporalFlowTool,
)
from sar_orch.tools.descriptions_loader import (
    DESCRIPTIONS_REL_PATH,
    apply_description_overlay,
    apply_description_overlays,
    load_descriptions_overlay,
    reset_descriptions_cache,
    tool_scope,
)
from sar_orch.tools.export_descriptions import build_overlay, iter_tool_specs
from sar_orch.tools.worker import SAR_WORKER_TOOLS, QuerySharedMemoryTool

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: 本卡快照的工具类总数（worker 14 + coordinator 8）；新增工具时必须同步更新
#: ``descriptions.json`` 与本常量。
EXPECTED_TOOL_CLASS_COUNT = 22

INLINE_DESCRIPTION = "inline description"
INLINE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"inline_arg": {"type": "string", "description": "inline arg"}},
    "required": ["inline_arg"],
}

#: 真实 SAR 工具类（worker 的 13 个注册工具 + 1 个弃用工具 + coordinator 8 个）。
REAL_SAR_TOOL_CLASSES: list[type] = [
    *[cls for cls in [*SAR_WORKER_TOOLS, QuerySharedMemoryTool]
      if cls.__module__.startswith("sar_orch.tools.")],
    CoordinatorFinishTaskTool,
    QueryControlJournalTool,
    QueryProjectionTool,
    QuerySARStateTool,
    QuerySemanticMapTool,
    QuerySupervisionTool,
    QueryTeamStatusTool,
    QueryTemporalFlowTool,
]


def make_tool(
    name: str,
    *,
    module: str = "sar_orch.tools.worker.synthetic",
    description: str = INLINE_DESCRIPTION,
    parameters: dict[str, Any] | None = None,
) -> type:
    """Synthetic class-attribute tool (same shape as the SAR tool classes)."""
    return type(
        "SyntheticTool",
        (Tool,),
        {
            "name": name,
            "description": description,
            "parameters": json.loads(json.dumps(parameters if parameters is not None else INLINE_PARAMETERS)),
            "__module__": module,
        },
    )


@pytest.fixture(autouse=True)
def _fresh_cache():
    """每个用例独立读一次覆盖层文件（loader 有进程内缓存）。"""
    reset_descriptions_cache()
    yield
    reset_descriptions_cache()


@pytest.fixture
def install_overlay(monkeypatch):
    """把 loader 的文件读取替换成给定文档（或异常）。"""

    def _install(document: str | Exception):
        calls: list[str] = []

        def fake_load(rel_path: str) -> str:
            calls.append(rel_path)
            if isinstance(document, Exception):
                raise document
            return document

        monkeypatch.setattr(descriptions_loader, "load_repo_prompt", fake_load)
        reset_descriptions_cache()
        return calls

    return _install


# ─────────────────────────────────────────────────────────────────────────────
# 1. 覆盖层生效（必需用例 ①）
# ─────────────────────────────────────────────────────────────────────────────


class TestOverlayTakesEffect:
    def test_description_and_parameters_are_overlaid(self, install_overlay):
        overlay_doc = {
            "carry_person": {
                "description": "OVERLAY description",
                "parameters": {
                    "type": "object",
                    "properties": {"person_id": {"type": "string", "description": "OVERLAY arg"}},
                    "required": ["person_id"],
                },
            }
        }
        calls = install_overlay(json.dumps(overlay_doc))
        tool = make_tool("carry_person")

        assert apply_description_overlay(tool) is True
        assert calls == [DESCRIPTIONS_REL_PATH]
        assert tool.description == "OVERLAY description"
        assert tool.parameters == overlay_doc["carry_person"]["parameters"]

        # LLM 实际看到的内容（schema）也随类属性变化
        schema = tool().to_openai_schema()
        assert schema["function"]["description"] == "OVERLAY description"
        assert schema["function"]["parameters"] == overlay_doc["carry_person"]["parameters"]

    def test_apply_is_case_by_case_and_counted(self, install_overlay):
        install_overlay(
            json.dumps({"carry_person": {"description": "A"}, "move": {"description": "B"}})
        )
        applied = apply_description_overlays([make_tool("carry_person"), make_tool("move"), make_tool("no_op")])
        assert applied == 2

    def test_parameters_dict_is_not_shared_between_classes(self, install_overlay):
        """同一裸名键命中多个类时，parameters 各自深拷贝，互不串改。"""
        install_overlay(json.dumps({"shared_name": {"parameters": {"type": "object", "properties": {}}}}))
        first = make_tool("shared_name", module="sar_orch.tools.worker.synthetic")
        second = make_tool("shared_name", module="sar_orch.tools.coordinator.synthetic")

        assert apply_description_overlay(first) is True
        assert apply_description_overlay(second) is True
        assert first.parameters == second.parameters
        assert first.parameters is not second.parameters
        first.parameters["properties"]["mutated"] = {"type": "string"}
        assert second.parameters["properties"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# 2. 文件缺失回退（必需用例 ②）
# ─────────────────────────────────────────────────────────────────────────────


class TestFallbackToInlineValues:
    def test_missing_file_keeps_inline_values(self, install_overlay):
        install_overlay(PromptLoadError("prompt file unreadable: ..."))
        tool = make_tool("carry_person")

        assert load_descriptions_overlay() == {}
        assert apply_description_overlay(tool) is False
        assert tool.description == INLINE_DESCRIPTION
        assert tool.parameters == INLINE_PARAMETERS

    def test_unknown_tool_key_keeps_inline_values(self, install_overlay):
        install_overlay(json.dumps({"some_other_tool": {"description": "X"}}))
        tool = make_tool("carry_person")

        assert apply_description_overlay(tool) is False
        assert tool.description == INLINE_DESCRIPTION
        assert tool.parameters == INLINE_PARAMETERS

    def test_missing_file_never_raises_for_real_classes(self, install_overlay):
        """fail-soft：异常只吞在 loader 内部，调用方（模块导入链）零异常。"""
        install_overlay(PromptLoadError("prompt file empty: sar_orch/tools/descriptions.json"))
        assert apply_description_overlays(REAL_SAR_TOOL_CLASSES) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. 部分键覆盖（必需用例 ③）
# ─────────────────────────────────────────────────────────────────────────────


class TestPartialOverlay:
    def test_tools_absent_from_file_keep_inline_values(self, install_overlay):
        install_overlay(json.dumps({"move": {"description": "OVERLAY description"}}))
        patched = make_tool("move")
        untouched = make_tool("carry_person")

        assert apply_description_overlay(patched) is True
        assert apply_description_overlay(untouched) is False
        assert patched.description == "OVERLAY description"
        assert patched.parameters == INLINE_PARAMETERS  # 未给 parameters → 内联值
        assert untouched.description == INLINE_DESCRIPTION
        assert untouched.parameters == INLINE_PARAMETERS

    def test_single_field_entry_keeps_the_other_inline_field(self, install_overlay):
        install_overlay(
            json.dumps({"move": {"parameters": {"type": "object", "properties": {"dir": {"type": "string"}}}}})
        )
        tool = make_tool("move")

        assert apply_description_overlay(tool) is True
        assert tool.description == INLINE_DESCRIPTION  # 未给 description → 内联值
        assert tool.parameters == {"type": "object", "properties": {"dir": {"type": "string"}}}

    def test_invalid_fields_are_rejected_field_by_field(self, install_overlay):
        install_overlay(
            json.dumps(
                {
                    "move": {"description": "   ", "parameters": {"type": "object", "properties": {"dir": {}}}},
                    "no_op": {"description": "OK", "parameters": ["not", "an", "object"]},
                }
            )
        )
        blank_description = make_tool("move")
        bad_parameters = make_tool("no_op")

        assert apply_description_overlay(blank_description) is True
        assert blank_description.description == INLINE_DESCRIPTION  # 空白描述被拒
        assert blank_description.parameters == {"type": "object", "properties": {"dir": {}}}

        assert apply_description_overlay(bad_parameters) is True
        assert bad_parameters.description == "OK"
        assert bad_parameters.parameters == INLINE_PARAMETERS  # 非对象参数被拒


# ─────────────────────────────────────────────────────────────────────────────
# 4. 键解析 / 文档健壮性
# ─────────────────────────────────────────────────────────────────────────────


class TestKeyResolution:
    def test_shared_name_resolves_by_scope(self, install_overlay):
        install_overlay(
            json.dumps(
                {
                    "worker/finish_task": {"description": "WORKER variant"},
                    "coordinator/finish_task": {"description": "COORDINATOR variant"},
                }
            )
        )
        worker_tool = make_tool("finish_task", module="sar_orch.tools.worker.finish_task")
        coordinator_tool = make_tool("finish_task", module="sar_orch.tools.coordinator.finish_task")

        assert apply_description_overlay(worker_tool) is True
        assert apply_description_overlay(coordinator_tool) is True
        assert worker_tool.description == "WORKER variant"
        assert coordinator_tool.description == "COORDINATOR variant"

    def test_bare_name_falls_back_for_both_scopes(self, install_overlay):
        install_overlay(json.dumps({"finish_task": {"description": "BARE"}}))
        for module in ("sar_orch.tools.worker.finish_task", "sar_orch.tools.coordinator.finish_task"):
            tool = make_tool("finish_task", module=module)
            assert apply_description_overlay(tool) is True
            assert tool.description == "BARE"

    def test_tool_scope_derivation(self):
        assert tool_scope(QueryProjectionTool) == "coordinator"
        assert tool_scope(SAR_WORKER_TOOLS[0]) == "worker"
        assert tool_scope(make_tool("x", module="somewhere.else.tool")) is None
        assert tool_scope(make_tool("x", module="sar_orch.other.worker.tool")) is None

    def test_non_object_root_degrades_to_inline(self, install_overlay):
        install_overlay(json.dumps(["not", "an", "object"]))
        assert load_descriptions_overlay() == {}
        assert apply_description_overlay(make_tool("carry_person")) is False

    def test_invalid_json_degrades_to_inline(self, install_overlay, caplog):
        install_overlay("{not json")
        assert load_descriptions_overlay() == {}
        assert apply_description_overlay(make_tool("carry_person")) is False

    def test_non_object_entry_is_dropped_but_siblings_apply(self, install_overlay):
        install_overlay(json.dumps({"move": ["nope"], "no_op": {"description": "OK"}}))
        overlay = load_descriptions_overlay()
        assert overlay == {"no_op": {"description": "OK"}}

        untouched = make_tool("move")
        patched = make_tool("no_op")
        assert apply_description_overlay(untouched) is False
        assert untouched.description == INLINE_DESCRIPTION
        assert apply_description_overlay(patched) is True
        assert patched.description == "OK"


# ─────────────────────────────────────────────────────────────────────────────
# 4.5 导入即接线（子进程探针：不依赖「文件恰好等于内联值」）
# ─────────────────────────────────────────────────────────────────────────────

_WIRING_PROBE_SCRIPT = """
import json
from sar_orch.tools import descriptions_loader

PROBE = {
    "carry_person": {"description": "PROBE carry_person"},
    "navigate_to": {
        "description": "PROBE navigate_to",
        "parameters": {"type": "object", "properties": {}},
    },
    "coordinator/query_projection": {"description": "PROBE query_projection"},
    "read_mailbox": {"description": "PROBE kernel tool"},
}
descriptions_loader.load_repo_prompt = lambda rel_path: json.dumps(PROBE)

import sar_orch  # noqa: E402
from sar_orch.tools.worker import SAR_WORKER_TOOLS  # noqa: E402
from sar_orch.tools.coordinator import QueryProjectionTool  # noqa: E402

by_name = {cls.name: cls for cls in SAR_WORKER_TOOLS}
print(json.dumps({
    "sar_orch_file": sar_orch.__file__,
    "carry_person": by_name["carry_person"].description,
    "navigate_to_params": by_name["navigate_to"].parameters,
    "query_projection": QueryProjectionTool.description,
    "move_inline": by_name["move"].description,
    "read_mailbox": by_name["read_mailbox"].description,
}))
"""

_MISSING_FILE_PROBE_SCRIPT = """
import json
from a2a.utils.prompt_loader import PromptLoadError
from sar_orch.tools import descriptions_loader


def _boom(rel_path):
    raise PromptLoadError("prompt file unreadable: " + rel_path)


descriptions_loader.load_repo_prompt = _boom

from sar_orch.tools.worker import SAR_WORKER_TOOLS  # noqa: E402
from sar_orch.tools.coordinator import QueryProjectionTool  # noqa: E402

by_name = {cls.name: cls for cls in SAR_WORKER_TOOLS}
print(json.dumps({
    "loaded_keys": sorted(descriptions_loader.load_descriptions_overlay()),
    "carry_person": by_name["carry_person"].description,
    "query_projection": QueryProjectionTool.description,
}))
"""


def _run_probe(script: str) -> dict:
    """Run a probe script in a fresh interpreter anchored on this worktree."""
    import os
    import subprocess
    import sys

    env = {
        k: v for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    env["PYTHONPATH"] = f"{_REPO_ROOT}:{_REPO_ROOT / 'src'}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestWiringAtImport:
    def test_importing_the_tool_packages_applies_the_overlay(self):
        """子进程内换掉 loader 的文件读取 → 工具包 import 后类属性必须是探针值。

        覆盖“接线本身”：本用例不依赖仓库文件内容，变异（reef tools target）前后
        都成立；同时断言 sar_orch 解析自本工作区，避免 .pth 把导入落到主树。
        """
        observed = _run_probe(_WIRING_PROBE_SCRIPT)

        assert observed["sar_orch_file"].startswith(str(_REPO_ROOT / "sar_orch"))
        assert observed["carry_person"] == "PROBE carry_person"
        assert observed["navigate_to_params"] == {"type": "object", "properties": {}}
        assert observed["query_projection"] == "PROBE query_projection"
        assert observed["move_inline"] == "Move one step in a cardinal direction."
        # 探针里故意放了一个内核工具名（read_mailbox）：SAR 覆盖层只作用于
        # sar_orch/tools/ 下的类，内核工具必须原样保留。
        assert observed["read_mailbox"] != "PROBE kernel tool"

    def test_missing_overlay_file_does_not_break_imports(self):
        """覆盖层文件不可读（PromptLoadError）时 import 工具包零异常、全量内联值。"""
        observed = _run_probe(_MISSING_FILE_PROBE_SCRIPT)

        assert observed["loaded_keys"] == []
        assert observed["carry_person"] == (
            "Pick up a trapped person. At least 2 agents must carry simultaneously to succeed."
        )
        assert observed["query_projection"] == (
            "Query the committed memory projection of the current scope "
            "(spatial or embodied), optionally narrowed to one entity. "
            "Read-only view of committed evidence; exposes no source-state "
            "selection parameters."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. 仓库语料不变量（数量核对 + 接线生效）
# ─────────────────────────────────────────────────────────────────────────────


class TestRepoCorpus:
    def test_overlay_is_read_from_this_tree(self):
        root = prompt_loader._find_repo_root(Path(prompt_loader.__file__).resolve().parent)
        assert root == _REPO_ROOT
        assert (_REPO_ROOT / DESCRIPTIONS_REL_PATH).is_file()

    def test_overlay_covers_every_tool_class(self):
        specs = iter_tool_specs()
        overlay = load_descriptions_overlay()

        assert len(specs) == EXPECTED_TOOL_CLASS_COUNT
        assert len(overlay) == EXPECTED_TOOL_CLASS_COUNT
        assert set(build_overlay(specs)) == set(overlay)
        for spec in specs:
            assert spec.name in overlay or f"{spec.scope}/{spec.name}" in overlay

    def test_real_tool_classes_carry_the_overlay_values(self):
        """模块导入时已接线：类属性 == 覆盖层文件内容（变异前后都成立）。"""
        overlay = load_descriptions_overlay()
        assert overlay

        assert len(REAL_SAR_TOOL_CLASSES) == EXPECTED_TOOL_CLASS_COUNT
        for cls in REAL_SAR_TOOL_CLASSES:
            scope = tool_scope(cls)
            assert scope in {"worker", "coordinator"}
            key = f"{scope}/{cls.name}" if f"{scope}/{cls.name}" in overlay else cls.name
            assert key in overlay, f"{cls.__name__} 缺少覆盖层条目"
            assert cls.description == overlay[key]["description"], key
            assert cls.parameters == overlay[key]["parameters"], key

    def test_kernel_tools_are_not_part_of_the_overlay(self):
        """SAR 覆盖层只管 sar_orch/tools/ 下的类；内核工具（property 形态）不在其中。"""
        overlay = load_descriptions_overlay()
        kernel_names = {
            cls.name for cls in SAR_WORKER_TOOLS if not cls.__module__.startswith("sar_orch.tools.")
        }
        assert kernel_names  # AskCoordinator / ReadMailbox / A2ASendMail
        assert kernel_names.isdisjoint(overlay)
