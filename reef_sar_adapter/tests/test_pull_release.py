"""pull_release: the tree-to-checkout mapping, and the dry-run default.

The pull is the inverse of an episode's overlay, so the mapping and the
"nothing writes unless --apply" contract are what these tests pin: a release
that carries no change must leave the checkout alone, and a release that
carries one must touch exactly the mapped files.
"""

from __future__ import annotations

import subprocess

from reef_sar_adapter import pull_release
from reef_sar_adapter.runner import RULES_PLACEHOLDER

COORDINATOR = "overlay/prompts/coordinator/system.semantic.md"
WORKER = "overlay/prompts/worker/system.md"
RULES = "overlay/rules.md"
CONFIG = "overlay/sar_config.json"

COORDINATOR_FILE = "sar_orch/prompts/coordinator/system.semantic.md"
WORKER_FILE = "sar_orch/prompts/worker/system.md"
RULES_FILE = "sar_orch/prompts/coordinator/rules.md"


def manifest(**files: str) -> dict:
    return {"release_id": "r1", "parent_release_id": "r0", "gate": None, "files": files}


def checkout(tmp_path):
    """A stand-in LLaMAR checkout carrying the two prompt files and a .bak."""
    repo = tmp_path / "LLaMAR"
    (repo / "sar_orch" / "prompts" / "coordinator").mkdir(parents=True)
    (repo / "sar_orch" / "prompts" / "worker").mkdir(parents=True)
    (repo / "sar_orch" / "prompts" / "coordinator" / "system.semantic.md").write_text("coordinator v1\n")
    (repo / "sar_orch" / "prompts" / "worker" / "system.md").write_text("worker v1\n")
    (repo / "sar_orch" / "prompts" / "coordinator" / "system.semantic.md.bak").write_text("old backup\n")
    return repo


def seeded_manifest() -> dict:
    return manifest(
        **{
            COORDINATOR: "coordinator v1\n",
            WORKER: "worker v1\n",
            RULES: f"{RULES_PLACEHOLDER}\n",
            CONFIG: "{}\n",
        }
    )


def test_the_seed_release_plans_no_write(tmp_path):
    repo = checkout(tmp_path)
    plan = pull_release.build_plan(seeded_manifest(), repo)
    assert [(item.action, item.path) for item in plan] == [
        ("unchanged", COORDINATOR),
        ("unchanged", WORKER),
        ("skip", RULES),
        ("report-only", CONFIG),
    ]
    assert pull_release.apply_plan(plan) == []
    assert not (repo / RULES_FILE).exists()


def test_a_changed_prompt_is_the_only_file_written(tmp_path):
    repo = checkout(tmp_path)
    plan = pull_release.build_plan(
        manifest(
            **{
                COORDINATOR: "coordinator v2\n",
                WORKER: "worker v1\n",
                RULES: f"{RULES_PLACEHOLDER}\n",
                CONFIG: '{"max_steps": 20}\n',
            }
        ),
        repo,
    )
    assert [item.action for item in plan] == ["write", "unchanged", "skip", "report-only"]
    written = pull_release.apply_plan(plan)
    assert written == [str(repo / COORDINATOR_FILE)]
    assert (repo / COORDINATOR_FILE).read_text("utf-8") == "coordinator v2\n"
    assert (repo / WORKER_FILE).read_text("utf-8") == "worker v1\n"
    # `.bak` files are never a target: the pull writes mapped files only.
    assert (repo / f"{COORDINATOR_FILE}.bak").read_text("utf-8") == "old backup\n"


def test_real_rules_land_as_the_new_coordinator_rules_file(tmp_path):
    repo = checkout(tmp_path)
    plan = pull_release.build_plan(
        manifest(
            **{
                COORDINATOR: "coordinator v1\n",
                WORKER: "worker v1\n",
                RULES: "Reserve one robot for medical transport.\n",
                CONFIG: "{}\n",
            }
        ),
        repo,
    )
    assert [(item.action, str(item.target) if item.target else None) for item in plan][2] == (
        "write",
        str(repo / RULES_FILE),
    )
    assert pull_release.apply_plan(plan) == [str(repo / RULES_FILE)]
    assert (repo / RULES_FILE).read_text("utf-8") == "Reserve one robot for medical transport.\n"


def test_a_tree_file_without_a_target_is_only_reported(tmp_path):
    repo = checkout(tmp_path)
    plan = pull_release.build_plan(
        manifest(
            **{
                COORDINATOR: "coordinator v1\n",
                WORKER: "worker v1\n",
                RULES: f"{RULES_PLACEHOLDER}\n",
                CONFIG: "{}\n",
                "overlay/unknown.md": "no target\n",
            }
        ),
        repo,
    )
    assert ("skip", "overlay/unknown.md") in [(item.action, item.path) for item in plan]


def test_render_plan_shows_the_text_diff_and_the_config_body(tmp_path):
    repo = checkout(tmp_path)
    plan = pull_release.build_plan(
        manifest(
            **{COORDINATOR: "coordinator v2\n", WORKER: "worker v1\n", RULES: f"{RULES_PLACEHOLDER}\n", CONFIG: "{}"}
        ),
        repo,
    )
    report = pull_release.render_plan(plan, seeded_manifest(), repo)
    assert "WRITE" in report and "coordinator v2" in report
    assert "not written back" in report


def test_git_diff_shows_the_tracked_change_and_a_new_file(tmp_path):
    repo = checkout(tmp_path)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "seed"],
        check=True,
    )
    plan = pull_release.build_plan(
        manifest(
            **{
                COORDINATOR: "coordinator v1\n",
                WORKER: "worker v1\n",
                RULES: "Reserve one robot.\n",
                CONFIG: "{}\n",
            }
        ),
        repo,
    )
    written = pull_release.apply_plan(plan)
    assert len(written) == 1
    diff = pull_release.git_diff(repo, [repo / RULES_FILE])
    assert RULES_FILE in diff and "+Reserve one robot." in diff
