"""Phase 1 — active-manifest rename gate for the legacy ``Context Memory`` term.

The legacy phrase ``Context Memory`` (including the ``## Context Memory``
heading) must reach zero occurrences ONLY inside the §2.3 active migration
manifest.  Historical / inert surfaces are explicitly excluded and must NOT be
batch-changed by this gate.

Also asserts the renderer heading is exactly ``## Environment State``, the
dynamic ``Output Format`` contract moved into the stable system prompt (never
the trailing role=user state block), ``memory_read_mode`` defaults to
``legacy``, and the ``SARCoordinatorStateProvider`` exposes an Environment State
adapter view with the required projection metadata.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# §2.3 active migration manifest — the ONLY scanned input.
ACTIVE_MANIFEST = [
    "src/Agent/router_agent/context.py",
    "src/Agent/router_agent/hooks.py",
    "src/Agent/worker_agent/context.py",
    "src/Agent/worker_agent/hooks.py",
    "src/a2a/builtin_tools/query_task_events.py",
    "src/a2a/coordinator/team_status_auth.py",
    "src/a2a/coordinator/team_partition_service.py",
    "sar_orch/coordinator_state_provider.py",
    "sar_orch/prompts/coordinator/system.md",
    "sar_orch/prompts/coordinator/system.semantic.md",
    "sar_orch/prompts/coordinator/system.oracle.md",
    "sar_orch/prompts/worker/system.md",
    "sar_orch/skills/coordinator/fire-suppression/SKILL.md",
    "sar_orch/skills/coordinator/person-rescue/SKILL.md",
    "sar_orch/skills/worker/firefighting/SKILL.md",
    "sar_orch/skills/worker/inventory-management/SKILL.md",
    "sar_orch/skills/worker/navigation/SKILL.md",
    "sar_orch/skills/worker/person-rescue/SKILL.md",
    "tests/test_coordinator_semantic_mode.py",
    "tests/test_phase3_read_mailbox.py",
    "tests/test_phase5_team_status_auth.py",
    "AGENTS.md",
    "docs/system_docs/contextmanager.md",
    "docs/system_docs/route_strategy.md",
    "docs/system_docs/semantic_map.md",
    "tool_gap_analysis.md",
]

# Historical / inert surfaces — explicitly excluded from the gate.  These are
# evidence, not active prompts: they are allowed to keep the legacy phrase and
# must never be bulk-rewritten by a rename sweep.
EXCLUDED_DIRS = [
    ".hermes",
    ".agents/handovers",
    "docs/plans",
    "docs/paper",
    "sar_orch/results",
    "logs",
    "docs/system_docs_html",
    "docs/superpowers",
]
EXCLUDED_GLOBS = ("**/*.bak", "**/*.svg")

LEGACY_PHRASE = "Context Memory"
RENDERER_HEADING = "## Environment State"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_manifest_paths_exist():
    missing = [p for p in ACTIVE_MANIFEST if not (ROOT / p).is_file()]
    assert not missing, f"manifest entries missing: {missing}"


def test_active_manifest_has_zero_legacy_phrase_occurrences():
    offenders: list[str] = []
    for rel in ACTIVE_MANIFEST:
        path = ROOT / rel
        text = _read(path)
        if LEGACY_PHRASE in text:
            offenders.append(f"{rel}: {text.count(LEGACY_PHRASE)}x")
    assert not offenders, (
        f"legacy phrase {LEGACY_PHRASE!r} must be zero in the active manifest:\n"
        + "\n".join(offenders)
    )


def test_excluded_surfaces_are_not_part_of_the_gate():
    """The gate must not be a repo-wide grep.

    At least one excluded/inert surface still legitimately contains the legacy
    phrase — proving the scanner only reads the §2.3 manifest.
    """
    baits = [
        ROOT / "sar_orch/prompts/worker/system.md.bak",
        ROOT / "sar_orch/prompts/coordinator/system.md.bak",
    ]
    present = [p for p in baits if p.is_file() and LEGACY_PHRASE in _read(p)]
    assert present, (
        "expected at least one inert *.bak surface to still contain the legacy "
        f"phrase so the gate is verifiably not a full-repo grep (found: {present})"
    )
    for rel in EXCLUDED_DIRS:
        assert (ROOT / rel).is_dir(), f"excluded dir missing: {rel}"
    # The excluded dirs are NOT scanned: assert the scanner scope is exactly the manifest.
    scanned = {str((ROOT / p).resolve()) for p in ACTIVE_MANIFEST}
    for rel in EXCLUDED_DIRS:
        for p in (ROOT / rel).rglob("*"):
            if p.is_file():
                assert str(p.resolve()) not in scanned, (
                    f"{p} is in an excluded dir but present in the scanned set"
                )


def test_renderer_heading_is_environment_state():
    from Agent.router_agent.context import CoordinatorContextManager
    from Agent.worker_agent.context import WorkerContextManager

    for ctx in (CoordinatorContextManager(), WorkerContextManager()):
        block = ctx._render_memory_block()
        assert LEGACY_PHRASE not in block
        assert RENDERER_HEADING in block
        assert "## Context Memory" not in block


def test_output_contract_moved_to_system_prompt_not_user_block():
    from Agent.router_agent.context import (
        ContextConfig,
        CoordinatorContextManager,
    )
    from Agent.router_agent.schema import Message

    schema = "Respond with EXACTLY one tool call per turn."
    ctx = CoordinatorContextManager(config=ContextConfig(output_schema=schema))
    messages = [Message(role="system", content="base system")]
    assembled = ctx.assemble("base system", messages)

    system_msg = assembled[0]
    user_block = assembled[-1]

    assert "## Output / Response Contract" in system_msg.content
    assert schema in system_msg.content
    assert "### Output Format" not in user_block.content
    assert schema not in user_block.content
    assert LEGACY_PHRASE not in user_block.content


def test_worker_user_block_has_no_output_contract():
    from Agent.worker_agent.context import ContextConfig, WorkerContextManager
    from Agent.worker_agent.schema import Message

    schema = "Return structured JSON only."
    ctx = WorkerContextManager(config=ContextConfig(output_schema=schema))
    assembled = ctx.assemble(
        "base system", [Message(role="system", content="base system")]
    )

    system_msg = assembled[0]
    user_block = assembled[-1]
    assert "## Output / Response Contract" in system_msg.content
    assert schema in system_msg.content
    assert "### Output Format" not in user_block.content
    assert schema not in user_block.content


def test_memory_read_mode_defaults_to_legacy():
    from Agent.router_agent.context import ContextConfig as RouterConfig
    from Agent.worker_agent.context import ContextConfig as WorkerConfig

    assert RouterConfig().memory_read_mode == "legacy"
    assert WorkerConfig().memory_read_mode == "legacy"


def test_coordinator_state_provider_environment_state_view():
    """SARCoordinatorStateProvider exposes an Environment State adapter view.

    Environment State is a rebuildable view, not a persistence truth source: the
    view must carry scope_id / as_of_sequence / memory_revision / freshness /
    conflicts / evidence_refs per the Environment State contract.
    """
    from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider

    class FakeMap:
        def snapshot(self):
            return {
                "agents": [],
                "recent_observations": [{"reporter": "A", "note": "x"}],
                "stale_entries": [],
                "conflicts": ["conflict-A"],
            }

        def get_step_budget(self):
            return {"current_step": 2, "max_steps": 50, "remaining": 48}

    provider = SARCoordinatorStateProvider(
        semantic_map=FakeMap(),
        state_mode="semantic",
    )
    view = provider.build_environment_state_view(context_id="ctx-1")

    assert set(view) >= {
        "scope_id",
        "as_of_sequence",
        "memory_revision",
        "freshness",
        "conflicts",
        "evidence_refs",
    }
    assert view["scope_id"] == "ctx-1"
    assert view["freshness"] in {"FRESH", "STALE", "UNAVAILABLE"}
    assert isinstance(view["evidence_refs"], list)
    assert isinstance(view["conflicts"], list)
    assert isinstance(view["payload"], dict)
