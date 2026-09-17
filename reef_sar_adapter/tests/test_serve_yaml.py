"""serve.yaml: in sync with the prompts, shaped as the cards decide, loadable by reef."""

from __future__ import annotations

import pytest
import yaml
from reef_sar_adapter import runner
from reef_sar_adapter.gen_serve_yaml import (
    PROMPT_FILES,
    SERVE_FILE,
    SMOKE_TASKS,
    build_serve_yaml,
    expected_config,
    repo_root,
)

REPO_ROOT = repo_root()


def loaded() -> dict:
    return yaml.safe_load(SERVE_FILE.read_text(encoding="utf-8"))


def seed() -> list[dict]:
    return loaded()["evolution"]["seed"]


def test_the_file_is_in_sync_with_the_repository_prompts():
    """A prompt edit must be re-seeded by re-running the generator, not by hand."""
    assert build_serve_yaml(REPO_ROOT) == SERVE_FILE.read_text(encoding="utf-8")


def test_the_render_parses_back_to_the_built_config():
    assert yaml.safe_load(build_serve_yaml(REPO_ROOT)) == expected_config(REPO_ROOT)


def test_the_seed_is_the_four_entry_variation_surface():
    """§B7-1: coordinator system.semantic, worker system, rules, config - nothing else."""
    entries = seed()
    assert [entry["id"] for entry in entries] == ["system.semantic", "worker-system", "base-rules", "sar_config"]
    assert [(entry["name"], entry["config"].get("name")) for entry in entries] == [
        ("agent_command", "system.semantic"),
        ("skill", "system"),
        ("rules", None),
        ("config", None),
    ]


def test_the_seeded_prompts_are_the_prompts_the_harness_loads_today():
    for entry in seed():
        relative = PROMPT_FILES.get(entry["id"])
        if relative is None:
            continue
        assert entry["config"]["text"] == (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_the_rules_seed_carries_the_runner_placeholder():
    """reef refuses an empty rules text (the scenario cannot be created), so the
    seed carries the placeholder whose overlay append the runner skips."""
    entry = next(entry for entry in seed() if entry["id"] == "base-rules")
    assert entry["config"]["text"] == runner.RULES_PLACEHOLDER


def test_the_evolution_knobs_are_the_decided_ones():
    text = loaded()
    evolution = text["evolution"]
    assert evolution["adapter"] == "sar"
    assert evolution["propose"] == "reef_sar_adapter.method:propose"
    assert evolution["evaluate"] == "reef_sar_adapter.method:evaluate"
    assert evolution["tasks"] == list(SMOKE_TASKS)
    assert len(evolution["tasks"]) == 4
    assert evolution["episode_timeout_s"] == 1800
    assert evolution["episode_repeats"] == 1
    assert evolution["min_win_margin"] == 0
    assert evolution["publish"] == "review"
    assert evolution["executor"] == "local"
    assert evolution["step_record_dir"] == "work/step-records"
    assert text["data"] == {"training_mode": "manual", "batch_size": 1}
    assert text["execution"]["evolution"]["workers"] == 2


def _strings(value):
    """Every string in a parsed config, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_preset_values_are_literal():
    """A preset is read as-is: `${VAR}` would arrive verbatim (only a deployment config interpolates)."""
    assert all("${" not in text for text in _strings(loaded()))


def test_reef_loads_the_preset_into_a_recipe():
    pytest.importorskip("reef.recipe.registry")
    from reef.recipe.config import load_recipe_config
    from reef.recipe.registry import build_recipe

    config = load_recipe_config(SERVE_FILE)
    recipe = build_recipe(config["implementation"], environ={}, config=config)
    assert recipe.adapter == "sar"
    assert recipe.tasks == SMOKE_TASKS
    assert recipe.episode_timeout_s == 1800
    assert recipe.min_win_margin == 0
    assert recipe.publish == "review"
    assert recipe.step_record_dir == "work/step-records"
    assert len(recipe.seed) == 4
    assert recipe.propose.reads_requests  # manual mode only hands the instruction to a proposer that names it
