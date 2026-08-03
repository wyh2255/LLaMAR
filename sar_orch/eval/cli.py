from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

from sar_orch.eval.dataset import load_episode, EpisodeDataset
from sar_orch.eval.graders import ALL_GRADERS, run_all_graders
from sar_orch.eval.graders.outcome import episode_agent_count
from sar_orch.eval.report import merge_results, write_both_reports
from sar_orch.eval.agent.tools import materialize_workspace
from sar_orch.eval.agent.eval_agent import (
    create_eval_agent,
    collect_judge_results,
    select_judge_steps,
    read_conclusion,
    get_agent_messages,
)


def _load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env_vars[key.strip()] = val.strip()
    return env_vars


def _run_deterministic_graders(episode: EpisodeDataset) -> list:
    return run_all_graders(episode)


def _save_grader_results_to_workspace(results: list, workspace_dir: Path) -> None:
    grader_dir = workspace_dir / "grader_results"
    grader_dir.mkdir(parents=True, exist_ok=True)
    by_name: dict[str, list] = {}
    for r in results:
        by_name.setdefault(r.grader, []).append(r.__dict__)
    for name, data in by_name.items():
        (grader_dir / f"{name}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )


def main():
    parser = argparse.ArgumentParser(
        description="SAR Experiment Eval Agent — M3 (LLM Judge + DeepAgent)"
    )
    parser.add_argument(
        "--results-dir",
        type=str,
        required=True,
        help="Path to experiment results directory",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path for eval_report.json (default: <results_dir>/eval_report.json)",
    )
    parser.add_argument(
        "--agent-model",
        type=str,
        default=None,
        help="Main agent model (default: .env model)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help="Judge subagent model (default: same as --agent-model)",
    )
    parser.add_argument(
        "--judge-sample-steps",
        type=int,
        default=20,
        help="Max steps to sample for LLM judge (default: 20, hard cap)",
    )
    parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help="Skip DeepAgent, only run deterministic graders + mechanical report",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"Error: results directory not found: {results_dir}", file=sys.stderr)
        sys.exit(1)

    env_vars = _load_env()
    agent_model = args.agent_model or env_vars.get("model", "deepseek-v4-flash")
    judge_model = args.judge_model or agent_model
    api_base = env_vars.get("api_base", "https://www.packyapi.com/v1")
    api_key = env_vars.get("api_key", "")

    print(f"Loading episode from {results_dir}...")
    episode = load_episode(results_dir)
    # 环境智能体数取自 metadata；episode.agent_names 还包含 MapAgent /
    # MapSummarizer 等非环境角色，用它会虚报智能体数量。
    print(
        f"  Scene {episode.metadata.get('scene')}, "
        f"{episode_agent_count(episode)} agents, "
        f"{max(episode.steps.keys()) if episode.steps else 0} steps"
    )

    eval_workspace = results_dir / "eval_workspace"
    materialize_workspace(episode, eval_workspace)

    print("\nRunning deterministic graders...")
    all_results = _run_deterministic_graders(episode)
    _save_grader_results_to_workspace(all_results, eval_workspace)
    print(f"  {len(all_results)} GradeResult(s) from {len(ALL_GRADERS)} graders")

    llm_judge = {}
    conclusion_text = None
    agent_trace = None

    if not args.no_llm_judge:
        import shutil

        judge_dir = eval_workspace / "judge_results"
        if judge_dir.exists():
            shutil.rmtree(judge_dir)
        judge_dir.mkdir(parents=True, exist_ok=True)
        (eval_workspace / "conclusion.md").write_text(
            "# Conclusion\n\n*(to be written by Eval Agent)*\n", encoding="utf-8"
        )

        print("\n--- LLM Judge Mode ---")
        print(f"  Agent model: {agent_model}")
        print(f"  Judge model: {judge_model}")
        print(f"  Judge sample steps: {args.judge_sample_steps}")
        print("  Creating DeepAgent...")

        subject_model = episode.metadata.get("model", "")

        agent = create_eval_agent(
            episode=episode,
            workspace_dir=eval_workspace,
            agent_model=agent_model,
            judge_model=judge_model if judge_model != agent_model else None,
            agent_api_base=api_base,
            agent_api_key=api_key,
            judge_api_base=api_base,
            judge_api_key=api_key,
            judge_sample_steps=args.judge_sample_steps,
        )

        print("  Running Eval Agent...")
        result = agent.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=(
                            f"Analyze the SAR experiment at {results_dir}. "
                            f"Grader results are already computed and saved in eval_workspace/grader_results/. "
                            f"Review them, do deep dives on failures, dispatch dispatch_judge and "
                            f"observation_judge subagents for evaluation, and write the conclusion to "
                            f"eval_workspace/conclusion.md. Save judge subagent outputs to "
                            f"eval_workspace/judge_results/ as individual JSON files."
                        )
                    )
                ]
            }
        )

        agent_trace = get_agent_messages(result)
        tool_calls = [m for m in agent_trace if m["role"] == "tool"]
        print(f"\n  Agent completed. Tool calls made: {len(tool_calls)}")
        for tc in tool_calls:
            print(f"    - {tc['name']}: {tc['content_preview'][:100]}...")

        # Pass the deterministic step count so a short return is flagged
        # `judge_partial` rather than silently yielding a rate over fewer
        # observations than were requested.
        llm_judge = collect_judge_results(
            eval_workspace,
            judge_model_name=judge_model,
            subject_model_name=subject_model,
            expected_steps=len(select_judge_steps(episode, args.judge_sample_steps)),
        )
        conclusion_text = read_conclusion(eval_workspace)
        print(
            f"  LLM judge results: dispatch={llm_judge.get('dispatch', {}).get('sampled_steps', 0)} steps, "
            f"observation={llm_judge.get('observation', {}).get('sampled_claims', 0)} claims"
        )
        if llm_judge.get("same_model_warning"):
            print(f"  ⚠ WARNING: judge_model == subject_model ({judge_model})")
    else:
        print("\n  --no-llm-judge: skipping DeepAgent")

    print("\nMerging report...")
    report = merge_results(
        episode,
        all_results,
        llm_judge=llm_judge,
        conclusion=conclusion_text,
    )

    json_path, md_path = write_both_reports(report, args.output, results_dir)
    print("eval report written to:")
    print(f"  JSON: {json_path}")
    print(f"  MD:   {md_path}")


if __name__ == "__main__":
    main()
