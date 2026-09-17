"""The descriptor: what reef's engines will render, run and read back."""

from __future__ import annotations

import json

import pytest
import yaml

descriptor_module = pytest.importorskip("reef.harness.adapters.descriptor")

from reef.harness.adapters.descriptor import load_descriptor
from reef.harness.episodes.model_binding import ModelBinding
from reef.harness.episodes.trajectory import reader_for
from reef.harness.tree.render import render_composition
from reef.train.cordis_backend.backend import admit_mutations
from reef_sar_adapter import DISTRIBUTION, descriptor, descriptor_path
from reef_sar_adapter.gen_serve_yaml import (
    PROMPT_FILES,
    SERVE_FILE,
    repo_root,
)
from reef_sar_adapter.method import parse_mutations

EXPECTED_RENDER = {
    "overlay/sar_config.json",
    "overlay/rules.md",
    "overlay/prompts/coordinator/system.semantic.md",
    "overlay/prompts/worker/system.md",
}


def seed_entries() -> list[dict]:
    """The seeded tree exactly as serve.yaml declares it."""
    config = yaml.safe_load(SERVE_FILE.read_text(encoding="utf-8"))
    return [dict(entry) for entry in config["evolution"]["seed"]]


def seed_nodes() -> tuple[tuple[str, dict], ...]:
    return tuple((str(entry["name"]), dict(entry["config"])) for entry in seed_entries())


def test_descriptor_loads_with_the_contract_reef_engines_use():
    adapter = descriptor()
    assert adapter.name == "sar"
    assert adapter.binary == "reef-sar"
    assert adapter.argv == ("{prompt}",)
    assert adapter.trajectory_format == "reef_sar_adapter.trajectory:read_sar_run"
    assert adapter.trajectory_path == "sar/out"
    assert adapter.cleanup_whitelist == ("sar/**",)
    assert adapter.writable_paths == ("sar",)
    assert adapter.self_isolating is False


def test_descriptor_path_ships_beside_the_package():
    assert descriptor_path().name == "descriptor.yaml"
    assert descriptor_path().parent.name == "reef_sar_adapter"
    assert load_descriptor(descriptor_path()).name == "sar"


def test_env_relocates_the_tree_and_pins_the_uv_cache():
    env = descriptor().env
    assert env["REEF_SAR_TREE"] == "{root}"
    assert env["REEF_SAR_OVERLAY"] == "{root}/overlay"
    assert env["LLAMAR_REPO"] == "/home/wyh/daily_work/LLaMAR"
    # episode/run.py sets HOME to the episode root; without this the episode
    # rebuilds uv's cache from scratch every time (§A.2-2).
    assert env["UV_CACHE_DIR"] == "/home/wyh/.cache/uv"
    # The campaign's frozen code baseline: LocalExecutor forwards no host
    # environment, so only a descriptor literal can pin it (R1 F1).
    assert env["LLAMAR_REF"] == "b243b2f0f563c14fded6cf4b01b5b2a99d236262"


def test_compose_relocation_resolves_for_client_wrappers():
    assert descriptor().compose_relocation() == ("REEF_SAR_OVERLAY", "overlay")


def test_trajectory_reference_resolves_to_the_package_reader(tmp_path):
    reader = reader_for(descriptor().trajectory_format)
    assert reader(tmp_path / "empty-run") == ()


def test_model_binding_renders_the_llm_block_into_the_config_target():
    binding = ModelBinding(base_url="http://127.0.0.1:1234", model="test-model", api_key="test-key")
    binding_nodes = binding.compose_nodes(descriptor())
    assert [kind for kind, _ in binding_nodes] == ["config"]
    rendered = render_composition(seed_nodes() + binding_nodes, descriptor())
    config = json.loads(rendered["overlay/sar_config.json"])
    assert config["llm"] == {"base_url": "http://127.0.0.1:1234", "api_key": "test-key", "model": "test-model"}


def test_the_seed_renders_the_paths_the_runner_and_harness_expect():
    files = render_composition(seed_nodes(), descriptor())
    assert set(files) == EXPECTED_RENDER
    root = repo_root()
    rendered_prompt_paths = {
        "system.semantic": "overlay/prompts/coordinator/system.semantic.md",
        "worker-system": "overlay/prompts/worker/system.md",
    }
    for entry_id, path in rendered_prompt_paths.items():
        # Byte-for-byte the prompt the harness loads today (the seed is a
        # snapshot, not a paraphrase).
        assert files[path] == (root / PROMPT_FILES[entry_id]).read_text(encoding="utf-8")
    # No provider rides in the served tree: the binding is injected per episode.
    assert json.loads(files["overlay/sar_config.json"]) == {}


def test_a_proposed_text_mutation_is_admitted_and_renders():
    """The whole loop the gate runs: parse -> admit -> render."""
    entries = seed_entries()
    specs = parse_mutations(
        json.dumps(
            [
                {
                    "id": "system.semantic",
                    "name": "agent_command",
                    "config": {"name": "system.semantic", "text": "REWRITTEN COORDINATOR PROMPT\n"},
                }
            ]
        )
    )
    admitted, refusal = admit_mutations(entries, [spec.mutation() for spec in specs], descriptor())
    assert refusal is None
    rendered = render_composition(
        tuple((str(entry["name"]), dict(entry["config"])) for entry in admitted), descriptor()
    )
    assert rendered["overlay/prompts/coordinator/system.semantic.md"] == "REWRITTEN COORDINATOR PROMPT\n"


def test_a_proposed_config_mutation_is_admitted():
    entries = seed_entries()
    specs = parse_mutations(
        json.dumps(
            [{"id": "sar_config", "name": "config", "config": {"target": "primary", "data": {"max_steps": 20}}}]
        ),
    )
    admitted, refusal = admit_mutations(entries, [spec.mutation() for spec in specs], descriptor())
    assert refusal is None
    rendered = render_composition(
        tuple((str(entry["name"]), dict(entry["config"])) for entry in admitted), descriptor()
    )
    assert json.loads(rendered["overlay/sar_config.json"]) == {"max_steps": 20}


def test_the_seed_passes_reef_admission():
    """Seed entries enter the loader directly, but a bad one would still fail to render."""
    admitted, refusal = admit_mutations(seed_entries(), [], descriptor())
    assert refusal is None
    assert len(admitted) == len(seed_entries())


def test_entry_point_registers_the_adapter():
    from importlib.metadata import PackageNotFoundError, entry_points, version

    try:
        version(DISTRIBUTION)
    except PackageNotFoundError:
        pytest.skip(f"{DISTRIBUTION} is not installed (pip install -e)")
    points = {point.name: point for point in entry_points(group="reef.harness_adapters")}
    assert "sar" in points
    loaded = points["sar"].load()
    adapter = loaded() if not isinstance(loaded, descriptor_module.AdapterDescriptor) else loaded
    assert adapter.name == "sar"
    assert "sar" in descriptor_module.external_descriptors()
