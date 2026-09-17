"""The runner: task validation, the plan, the chain steps, the dry run."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from reef_sar_adapter import runner

TASK = '{"scene":3,"agents":2,"seed":0,"max_steps":35}'

BINDING = {"base_url": "https://example.invalid/v1", "api_key": "not-a-real-key", "model": "test-model"}


# -- fixtures -----------------------------------------------------------------


def _git(repo: Path, *argument: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    subprocess.run(["git", "-C", str(repo), *argument], check=True, capture_output=True, env=env)


def checkout(tmp_path: Path, name: str = "llamar") -> tuple[Path, str]:
    """A throwaway LLaMAR checkout the runner can materialize, and its commit."""
    repo = tmp_path / name
    (repo / "sar_orch" / "prompts" / "coordinator").mkdir(parents=True)
    (repo / "sar_orch" / "prompts" / "worker").mkdir(parents=True)
    (repo / "sar_orch" / "prompts" / "coordinator" / "system.semantic.md").write_text(
        "checkout coordinator\n", "utf-8"
    )
    (repo / "sar_orch" / "prompts" / "worker" / "system.md").write_text("checkout worker\n", "utf-8")
    (repo / "README.md").write_text("checkout\n", "utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def render_overlay(
    tree: Path,
    *,
    config: dict | None = None,
    rules: str = "keep the coordinator terse\n",
) -> None:
    """The composition reef renders: the two prompt directories, rules, config."""
    for domain in ("coordinator", "worker"):
        (tree / "overlay" / "prompts" / domain).mkdir(parents=True, exist_ok=True)
    (tree / "overlay" / "sar_config.json").write_text(
        json.dumps(config if config is not None else {"llm": BINDING}), "utf-8"
    )
    (tree / "overlay" / "rules.md").write_text(rules, "utf-8")
    (tree / "overlay" / "prompts" / "coordinator" / "system.semantic.md").write_text("overlay coordinator\n", "utf-8")
    (tree / "overlay" / "prompts" / "worker" / "system.md").write_text("overlay worker\n", "utf-8")


def environ_for(tmp_path: Path, repo: Path, *, ref: str | None = None) -> dict[str, str]:
    tree = tmp_path / "episode"
    tree.mkdir(exist_ok=True)
    environ = {"REEF_SAR_TREE": str(tree), "LLAMAR_REPO": str(repo), "UV_CACHE_DIR": "/cache"}
    if ref is not None:
        environ["LLAMAR_REF"] = ref
    return environ


# -- task ---------------------------------------------------------------------


def test_parse_task_reads_the_smoke_task():
    assert runner.parse_task(TASK) == {"scene": 3, "agents": 2, "seed": 0, "max_steps": 35}


def test_parse_task_keeps_extra_fields():
    assert runner.parse_task('{"scene":3,"agents":2,"seed":0,"note":"x"}')["note"] == "x"


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "[]",
        '"a string"',
        '{"scene":3,"agents":2}',
        '{"scene":"3","agents":2,"seed":0}',
        '{"scene":true,"agents":2,"seed":0}',
        '{"scene":-1,"agents":2,"seed":0}',
        '{"scene":3,"agents":2,"seed":0,"max_steps":0}',
    ],
)
def test_bad_tasks_are_refused(text):
    with pytest.raises(ValueError):
        runner.parse_task(text)


# -- config translation -------------------------------------------------------


def test_the_whitelist_translates_run_keys_and_ignores_the_rest():
    task = runner.parse_task('{"scene":3,"agents":2,"seed":0}')
    translation = runner.translate_config(
        {
            "llm": BINDING,
            "prune_policy": "prefix_stable",
            "max_steps": 12,
            "scene": 5,
            "agents": 4,
            "seed": 99,
            "mode": "oracle",
            "unknown_key": True,
        },
        task,
    )
    assert translation.flags == {"prune_policy": "prefix_stable", "max_steps": 12}
    assert translation.binding == BINDING
    joined = "\n".join(translation.warnings)
    for key in ("scene", "agents", "seed", "mode", "unknown_key"):
        assert f"'{key}'" in joined
    # The task decides which task is graded: the config's copies never survive.
    assert "flags" in repr(translation) and "scene" not in translation.flags


def test_the_task_owns_the_step_budget():
    task = runner.parse_task(TASK)
    translation = runner.translate_config({"max_steps": 5}, task)
    assert "max_steps" not in translation.flags
    assert "the task JSON sets max_steps=35" in translation.warnings[0]


def test_a_config_without_a_binding_is_reported():
    translation = runner.translate_config({}, runner.parse_task(TASK))
    assert translation.binding == {}
    assert "no usable llm binding" in translation.warnings[0]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://cf.api.fan", "https://cf.api.fan/v1"),
        ("https://cf.api.fan/", "https://cf.api.fan/v1"),
        ("https://opencode.ai/zen/go/v1", "https://opencode.ai/zen/go/v1"),
        ("http://127.0.0.1:1234/v1/", "http://127.0.0.1:1234/v1"),
        ("", ""),
    ],
)
def test_harness_base_url_restores_the_v1_prefix_once(given, expected):
    """reef's dialect drops /v1 (model_binding.py:61); the SDK-style base needs it."""
    assert runner.harness_base_url(given) == expected


def test_the_experiment_gets_the_sdk_style_base_from_a_bare_binding(tmp_path):
    repo, head = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref=head)
    render_overlay(tmp_path / "episode", config={"llm": {**BINDING, "base_url": "https://cf.api.fan"}})
    episode = runner.build_plan(runner.parse_task(TASK), environ)
    command = episode.experiment_command()
    assert command[command.index("--api-base") + 1] == "https://cf.api.fan/v1"
    assert episode.plan()["config"]["binding"]["experiment_api_base"] == "https://cf.api.fan/v1"


@pytest.mark.parametrize("value", [0, -3, "ten", True])
def test_a_bad_max_steps_is_refused(value):
    translation = runner.translate_config({"max_steps": value}, runner.parse_task('{"scene":3,"agents":2,"seed":0}'))
    assert "max_steps" not in translation.flags


def test_an_unknown_prune_policy_is_refused():
    translation = runner.translate_config({"prune_policy": "yolo"}, runner.parse_task(TASK))
    assert "prune_policy" not in translation.flags
    assert "count_window" in translation.warnings[0]


# -- overlay ------------------------------------------------------------------


def test_the_overlay_overwrites_the_prompts_and_appends_the_rules(tmp_path):
    repo, _ = checkout(tmp_path)
    tree = tmp_path / "episode"
    render_overlay(tree, rules="keep it terse\n")
    written, notes = runner.apply_overlay(tree / "overlay", repo)
    assert set(written) == {
        "sar_orch/prompts/coordinator/system.semantic.md",
        "sar_orch/prompts/worker/system.md",
        runner.RULES_TARGET,
    }
    assert any("appended" in note for note in notes)
    coordinator = (repo / "sar_orch" / "prompts" / "coordinator" / "system.semantic.md").read_text("utf-8")
    assert coordinator == f"overlay coordinator\n{runner.RULES_HEADER}keep it terse\n"
    assert (repo / "sar_orch" / "prompts" / "worker" / "system.md").read_text("utf-8") == "overlay worker\n"


def test_an_empty_rules_file_appends_nothing(tmp_path):
    repo, _ = checkout(tmp_path)
    tree = tmp_path / "episode"
    render_overlay(tree, rules="\n")
    written, notes = runner.apply_overlay(tree / "overlay", repo)
    assert set(written) == {"sar_orch/prompts/coordinator/system.semantic.md", "sar_orch/prompts/worker/system.md"}
    assert any("empty" in note for note in notes)
    assert (repo / "sar_orch" / "prompts" / "coordinator" / "system.semantic.md").read_text("utf-8") == (
        "overlay coordinator\n"
    )


def test_the_seeds_rules_placeholder_appends_nothing(tmp_path):
    # reef refuses an empty rules text, so the seed carries the placeholder
    # instead; it must reach the checkout exactly like an empty file: not at all.
    repo, _ = checkout(tmp_path)
    tree = tmp_path / "episode"
    render_overlay(tree, rules=f"{runner.RULES_PLACEHOLDER}\n")
    written, notes = runner.apply_overlay(tree / "overlay", repo)
    assert set(written) == {"sar_orch/prompts/coordinator/system.semantic.md", "sar_orch/prompts/worker/system.md"}
    assert any("placeholder" in note for note in notes)
    assert (repo / "sar_orch" / "prompts" / "coordinator" / "system.semantic.md").read_text("utf-8") == (
        "overlay coordinator\n"
    )


def test_the_config_is_the_runners_input_not_checkout_content(tmp_path):
    repo, _ = checkout(tmp_path)
    tree = tmp_path / "episode"
    render_overlay(tree)
    runner.apply_overlay(tree / "overlay", repo)
    assert not (repo / "sar_config.json").exists()


def test_an_overlay_entry_the_harness_cannot_take_is_a_note(tmp_path):
    repo, _ = checkout(tmp_path)
    tree = tmp_path / "episode"
    render_overlay(tree)
    (tree / "overlay" / "prompts" / "stray.md").write_text("x\n", "utf-8")
    _written, notes = runner.apply_overlay(tree / "overlay", repo)
    assert "stray.md" in notes[0]
    assert not (repo / "prompts").exists()


# -- materialize and ports ----------------------------------------------------


def test_preflight_reports_uv_and_its_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path))
    tooling = runner.preflight()
    assert tooling["uv"] and tooling["git"]
    assert tooling["uv_cache_warm"] is True
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "absent"))
    assert runner.preflight()["uv_cache_warm"] is False


def test_preflight_refuses_a_missing_uv(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None if name == "uv" else "/usr/bin/git")
    with pytest.raises(runner.ChainError) as caught:
        runner.preflight()
    assert caught.value.code == runner.USAGE_EXIT
    assert "uv is not on PATH" in str(caught.value)


def test_materialize_extracts_the_ref(tmp_path):
    repo, head = checkout(tmp_path)
    destination = tmp_path / "workspace" / "repo"
    count = runner.materialize(str(repo), head, destination)
    assert count == 3  # README + the two prompt files
    assert (destination / "README.md").read_text("utf-8") == "checkout\n"
    assert (destination / "sar_orch" / "prompts" / "worker" / "system.md").is_file()


def test_materialize_refuses_a_bad_ref(tmp_path):
    repo, _ = checkout(tmp_path)
    destination = tmp_path / "workspace" / "repo"
    with pytest.raises(runner.ChainError) as caught:
        runner.materialize(str(repo), "deadbeefdeadbeef", destination)
    assert "git archive" in str(caught.value)
    assert not destination.exists()  # no half-extracted checkout is left behind


def test_allocate_ports_returns_a_free_block():
    coordinator, base = runner.allocate_ports(2)
    assert base == coordinator + 2
    assert all(runner._port_free(port) for port in (coordinator, coordinator + 1, base, base + 1))


def test_allocate_ports_spreads_concurrent_episodes_apart():
    """Two episodes probing at the same moment must not pick the same block.

    The probe reserves nothing and `uv run` takes seconds before the experiment
    binds, so a shared scan start let a pair launched together both take 50000
    (one then died on the bind: steps=0, framework_error, ~2 s).
    """
    assert runner._scan_slots("")[0] == runner.PORT_SCAN_START
    first_slots = runner._scan_slots("/tmp/reef-episode-sar-aaaaaaaa")
    second_slots = runner._scan_slots("/tmp/reef-episode-sar-bbbbbbbb")
    assert first_slots[0] != second_slots[0]
    assert sorted(first_slots) == sorted(second_slots)
    coordinator, base = runner.allocate_ports(2, spread_seed="/tmp/reef-episode-sar-aaaaaaaa")
    assert base == coordinator + 2
    assert all(runner._port_free(port) for port in (coordinator, coordinator + 1, base, base + 1))


def test_the_framework_error_diagnostics_echo_the_logs_and_redact_the_key(tmp_path, capsys):
    repo, head = checkout(tmp_path)
    render_overlay(tmp_path / "episode", config={"llm": BINDING})
    episode = runner.build_plan(runner.parse_task(TASK), environ_for(tmp_path, repo, ref=head))
    episode.logs_dir.mkdir(parents=True, exist_ok=True)
    (episode.logs_dir / "experiment.err.log").write_text(
        f"boom {BINDING['api_key']}\ntraceback line\n", encoding="utf-8"
    )
    runner._diagnose_framework_error(episode)
    err = capsys.readouterr().err
    assert "boom <redacted>" in err
    assert BINDING["api_key"] not in err
    assert "traceback line" in err


# -- plan ---------------------------------------------------------------------


def test_build_plan_keeps_every_path_inside_the_tree(tmp_path):
    repo, head = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref=head)
    render_overlay(tmp_path / "episode")
    episode = runner.build_plan(runner.parse_task(TASK), environ)
    tree = Path(environ["REEF_SAR_TREE"])
    assert episode.repo_dir == tree / "workspace" / "repo"
    assert episode.run_dir == tree / "workspace" / "run"
    assert episode.truth_dir == tree / "workspace" / "truth"
    assert episode.out_dir == tree / "sar" / "out"
    assert episode.ref == head
    assert episode.ref_source == "LLAMAR_REF"
    assert episode.overlay_missing == ()
    assert episode.max_steps == 35 and episode.max_steps_source == "task JSON"
    plan = episode.plan()
    assert plan["artifacts"]["truth_dir"] == str(tree / "workspace" / "truth")
    assert plan["commands"]["experiment"][:3] == ["uv", "run", "--extra"]
    assert "--truth-output-dir" in plan["commands"]["experiment"]


def test_the_config_step_budget_applies_only_without_a_task_budget(tmp_path):
    repo, head = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref=head)
    render_overlay(tmp_path / "episode", config={"llm": BINDING, "max_steps": 12})
    episode = runner.build_plan(runner.parse_task('{"scene":3,"agents":2,"seed":0}'), environ)
    assert (episode.max_steps, episode.max_steps_source) == (12, "config")
    assert "--max-steps" in episode.experiment_command()


def test_a_missing_environment_is_a_usage_error(tmp_path):
    with pytest.raises(runner.ChainError) as caught:
        runner.build_plan(runner.parse_task(TASK), {"LLAMAR_REPO": "/tmp"})
    assert caught.value.code == runner.USAGE_EXIT
    assert "missing required environment" in str(caught.value)


def test_an_unresolvable_ref_is_an_environment_error(tmp_path):
    repo, _ = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref="deadbeefdeadbeef")
    with pytest.raises(runner.ChainError) as caught:
        runner.build_plan(runner.parse_task(TASK), environ)
    assert caught.value.code == runner.USAGE_EXIT
    assert "does not resolve" in str(caught.value)


def test_the_head_fallback_warns_on_stderr(tmp_path, capsys):
    repo, head = checkout(tmp_path)
    ref, source = runner.resolve_ref(str(repo), {})
    assert ref == head
    assert "HEAD" in source
    assert "LLAMAR_REF is unset" in capsys.readouterr().err


# -- collect ------------------------------------------------------------------


def _episode_with_run(tmp_path: Path, *, files: dict[str, str] | None = None) -> runner.Episode:
    repo, head = checkout(tmp_path)
    tree = tmp_path / "episode"
    render_overlay(tree, config={"llm": BINDING, "prune_policy": "prefix_stable"})
    episode = runner.build_plan(runner.parse_task(TASK), environ_for(tmp_path, repo, ref=head))
    run_dir = episode.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_metrics.json": json.dumps({"steps": 5, "transport_rate": 0.5, "end_reason": "max_steps_reached"}),
        "eval_metrics.json": json.dumps(
            {"l2_planning": {"load_balance_b": 0.25}, "l4_cost": {"effective_billed_tokens": 1234.5}}
        ),
        "summary.csv": "step,coverage\n0,0.0\n",
    }
    payload.update(files or {})
    for name, text in payload.items():
        (run_dir / name).write_text(text, "utf-8")
    return episode


def test_collect_lands_the_gradeable_artifacts(tmp_path):
    episode = _episode_with_run(tmp_path)
    copied, missing = runner.collect_artifacts(
        episode, task_text=TASK, run_meta=runner.run_meta(episode, task_text=TASK, exit_code=0)
    )
    assert copied == list(runner.COLLECT_FILES)
    assert missing == []
    out = episode.out_dir
    assert sorted(path.name for path in out.iterdir()) == sorted(
        [*runner.COLLECT_FILES, runner.RUN_META_FILE, runner.TASK_FILE, runner.CONFIG_SNAPSHOT]
    )
    meta = json.loads((out / runner.RUN_META_FILE).read_text("utf-8"))
    assert meta["scene"] == 3 and meta["agents"] == 2 and meta["seed"] == 0
    assert meta["files"]["collected"] == list(runner.COLLECT_FILES)
    assert (out / runner.TASK_FILE).read_text("utf-8") == TASK + "\n"


def test_collect_reports_missing_artifacts_without_faking_them(tmp_path):
    episode = _episode_with_run(tmp_path, files={"eval_metrics.json": ""})
    (episode.run_dir / "eval_metrics.json").unlink()
    _copied, missing = runner.collect_artifacts(
        episode, task_text=TASK, run_meta=runner.run_meta(episode, task_text=TASK, exit_code=1)
    )
    assert "eval_metrics.json" in missing
    assert not (episode.out_dir / "eval_metrics.json").exists()


def test_the_config_snapshot_redacts_the_key(tmp_path):
    episode = _episode_with_run(tmp_path)
    runner.collect_artifacts(episode, task_text=TASK, run_meta=runner.run_meta(episode, task_text=TASK, exit_code=0))
    snapshot = json.loads((episode.out_dir / runner.CONFIG_SNAPSHOT).read_text("utf-8"))
    assert snapshot["llm"]["api_key"] == "<redacted>"
    assert snapshot["llm"]["model"] == "test-model"
    assert snapshot["prune_policy"] == "prefix_stable"


def test_the_truth_guard_refuses_a_leak(tmp_path):
    episode = _episode_with_run(tmp_path)
    episode.out_dir.mkdir(parents=True, exist_ok=True)
    (episode.out_dir / "truth_trace.jsonl").write_text("{}\n", "utf-8")
    with pytest.raises(runner.ChainError) as caught:
        runner.collect_artifacts(episode, task_text=TASK, run_meta={})
    assert "truth leaked" in str(caught.value)


def test_truth_files_in_the_run_dir_are_never_copied(tmp_path):
    episode = _episode_with_run(tmp_path)
    (episode.run_dir / "truth_trace.jsonl").write_text("{}\n", "utf-8")
    runner.collect_artifacts(episode, task_text=TASK, run_meta=runner.run_meta(episode, task_text=TASK, exit_code=0))
    assert not (episode.out_dir / "truth_trace.jsonl").exists()
    assert all("truth" not in path.name for path in episode.out_dir.iterdir())


# -- the chain ----------------------------------------------------------------


def test_a_chain_failure_still_leaves_the_episode_identity(tmp_path):
    repo, head = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref=head)
    render_overlay(Path(environ["REEF_SAR_TREE"]), config={})
    episode = runner.build_plan(runner.parse_task(TASK), environ)
    with pytest.raises(runner.ChainError) as caught:
        runner.run_episode(episode, task_text=TASK)
    assert "no model binding" in str(caught.value)
    meta = json.loads((episode.out_dir / runner.RUN_META_FILE).read_text("utf-8"))
    assert (meta["scene"], meta["agents"], meta["seed"]) == (3, 2, 0)
    assert meta["llamar_ref"] == head
    assert meta["task"] == TASK
    # Nothing ran: no run dir, no metrics, and the trajectory reader reads it as
    # "an episode that could not run".
    assert not episode.run_dir.exists()
    assert not (episode.out_dir / "run_metrics.json").exists()


# -- environment and metrics --------------------------------------------------


def test_the_run_environment_carries_the_binding_and_drops_the_harness_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("REEF_SAR_TREE", "/tmp/whatever")
    episode = _episode_with_run(tmp_path)
    env = runner.experiment_env(episode)
    assert env["OPENAI_API_KEY"] == BINDING["api_key"]
    assert env["PYTHONPATH"] == "src"
    assert env["no_proxy"] == "localhost,0.0.0.0,127.0.0.1"
    assert not [key for key in env if key.startswith("REEF_SAR_")]
    assert env["MPLCONFIGDIR"].startswith(str(episode.tree / "workspace"))


def test_metrics_line_reads_the_collected_numbers(tmp_path):
    episode = _episode_with_run(tmp_path)
    line = runner.metrics_line(
        episode,
        run_metrics={"steps": 5, "transport_rate": 0.5, "end_reason": "max_steps_reached"},
        eval_metrics={"l2_planning": {"load_balance_b": 0.25}, "l4_cost": {"effective_billed_tokens": 1234.5}},
        exit_code=0,
    )
    assert line == {
        "scene": 3,
        "agents": 2,
        "seed": 0,
        "transport_rate": 0.5,
        "load_balance_b": 0.25,
        "effective_billed_tokens": 1234.5,
        "steps": 5,
        "end_reason": "max_steps_reached",
        "exit_code": 0,
    }
    empty = runner.metrics_line(episode, run_metrics={}, eval_metrics={}, exit_code=3)
    assert empty["transport_rate"] is None and empty["exit_code"] == 3


# -- main ---------------------------------------------------------------------


def test_dry_run_renders_the_tree_and_runs_nothing(tmp_path, monkeypatch, capsys):
    repo, head = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref=head)
    tree = Path(environ["REEF_SAR_TREE"])
    render_overlay(tree, config={"llm": BINDING, "prune_policy": "prefix_stable", "scene": 5})
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    assert runner.main([TASK, "--dry-run"]) == 0
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert plan["task"]["scene"] == 3
    assert plan["config"]["flags"] == {"prune_policy": "prefix_stable"}
    assert plan["config"]["binding"]["api_key_present"] is True
    assert BINDING["api_key"] not in captured.out and BINDING["api_key"] not in captured.err
    assert plan["dry_run"]["overlay_written"] == [
        "sar_orch/prompts/coordinator/system.semantic.md",
        "sar_orch/prompts/worker/system.md",
    ]
    assert any("appended" in note for note in plan["dry_run"]["notes"])
    assert plan["tooling"]["uv"] and plan["tooling"]["git"]
    # The tree is really rendered: the overlay sits where the harness reads it.
    materialized = tree / "workspace" / "repo" / "sar_orch" / "prompts" / "coordinator" / "system.semantic.md"
    assert materialized.read_text("utf-8").startswith("overlay coordinator\n")
    assert runner.RULES_HEADER in materialized.read_text("utf-8")
    # Nothing was executed: no run dir, no logs, no trajectory artifacts.
    assert not (tree / "workspace" / "run").exists()
    assert not (tree / "sar").exists()
    # The config's scene mutation was refused, loudly, on stderr.
    assert "scene" in captured.err


def test_dry_run_reports_an_incomplete_tree(tmp_path, monkeypatch, capsys):
    repo, head = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref=head)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    assert runner.main([TASK, "--dry-run"]) == runner.INCOMPLETE_TREE_EXIT
    plan = json.loads(capsys.readouterr().out)
    assert plan["overlay_files"]["missing"] == ["config", "coordinator_prompts", "rules", "worker_prompts"]
    assert not (Path(environ["REEF_SAR_TREE"]) / "workspace" / "repo").exists()


def test_missing_environment_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("REEF_SAR_TREE", raising=False)
    monkeypatch.delenv("LLAMAR_REPO", raising=False)
    assert runner.main([TASK, "--dry-run"]) == runner.USAGE_EXIT
    assert "missing required environment" in capsys.readouterr().err


def test_a_bad_task_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("REEF_SAR_TREE", "/tmp")
    monkeypatch.setenv("LLAMAR_REPO", "/tmp")
    assert runner.main(["{ nope", "--dry-run"]) == runner.USAGE_EXIT
    assert "not valid JSON" in capsys.readouterr().err


def test_main_prints_the_metrics_line_last_and_forwards_the_exit_code(tmp_path, monkeypatch, capsys):
    repo, head = checkout(tmp_path)
    environ = environ_for(tmp_path, repo, ref=head)
    render_overlay(Path(environ["REEF_SAR_TREE"]))
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    line = {"scene": 3, "agents": 2, "seed": 0, "transport_rate": 1.0, "exit_code": 7}
    monkeypatch.setattr(runner, "run_episode", lambda episode, *, task_text: (7, line))
    assert runner.main([TASK]) == 7
    stdout = capsys.readouterr().out.strip().splitlines()
    assert json.loads(stdout[-1]) == line
