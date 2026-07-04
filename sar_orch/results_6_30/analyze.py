#!/usr/bin/env python3
"""Comprehensive analysis of SAR benchmark results."""

import csv
import json
from collections import defaultdict
from pathlib import Path

BASE = Path("/home/user/.WYH/LLaMAR/sar_orch/results_6_30/benchmark")


def load_all_results():
    """Load all result.json and summary.csv files, handling pass variants."""
    runs = []
    for scene_dir in sorted(BASE.iterdir()):
        if not scene_dir.is_dir() or not scene_dir.name.startswith("scene_"):
            continue
        scene = int(scene_dir.name.split("_")[1])
        for agents_dir in sorted(scene_dir.iterdir()):
            if not agents_dir.is_dir() or not agents_dir.name.startswith("agents_"):
                continue
            agents = int(agents_dir.name.split("_")[1])
            for seed_dir in sorted(agents_dir.iterdir()):
                if not seed_dir.is_dir():
                    continue
                seed_name = seed_dir.name
                # Parse seed number: "seed_0", "seed_0_pass_0", "seed_0_pass_1"
                parts = seed_name.split("_")
                seed = int(parts[1])
                is_pass = seed_name.endswith("_pass_0") or seed_name.endswith("_pass_1")

                result_file = seed_dir / "result.json"
                summary_file = seed_dir / "summary.csv"
                exp_summary = seed_dir / "experiment_logs" / "summary.csv"

                run = {
                    "scene": scene,
                    "agents": agents,
                    "seed": seed,
                    "pass": 0
                    if not is_pass
                    else (0 if seed_name.endswith("_pass_0") else 1),
                    "is_pass": is_pass,
                    "dir": str(seed_dir),
                    "has_data": False,
                    "has_error": False,
                }

                # Load result.json
                if result_file.exists():
                    try:
                        with open(result_file) as f:
                            data = json.load(f)
                        run.update(
                            {
                                "coverage": data.get("coverage", 0),
                                "transport_rate": data.get("transport_rate", 0),
                                "steps": data.get("steps", 0),
                                "finished": data.get("finished", False),
                                "elapsed_seconds": data.get("elapsed_seconds", 0),
                                "status": data.get("status", "unknown"),
                            }
                        )
                        run["has_data"] = True
                    except Exception as exc:
                        run["has_error"] = True
                        run["error"] = str(exc)

                # Load summary.csv for token data
                summary_target = summary_file if summary_file.exists() else exp_summary
                if summary_target.exists():
                    try:
                        with open(summary_target) as f:
                            reader = csv.DictReader(f)
                            for row in reader:
                                agent_tokens = {}
                                total_prompt = 0
                                total_completion = 0
                                total = 0
                                for col, val in row.items():
                                    if col.endswith("PromptTokens"):
                                        agent = col.replace("PromptTokens", "")
                                        if agent not in agent_tokens:
                                            agent_tokens[agent] = {
                                                "prompt": 0,
                                                "completion": 0,
                                                "total": 0,
                                            }
                                        agent_tokens[agent]["prompt"] = (
                                            int(val) if val else 0
                                        )
                                        total_prompt += int(val) if val else 0
                                    elif col.endswith("CompletionTokens"):
                                        agent = col.replace("CompletionTokens", "")
                                        if agent not in agent_tokens:
                                            agent_tokens[agent] = {
                                                "prompt": 0,
                                                "completion": 0,
                                                "total": 0,
                                            }
                                        agent_tokens[agent]["completion"] = (
                                            int(val) if val else 0
                                        )
                                        total_completion += int(val) if val else 0
                                    elif col.endswith("TotalTokens"):
                                        agent = col.replace("TotalTokens", "")
                                        if agent not in agent_tokens:
                                            agent_tokens[agent] = {
                                                "prompt": 0,
                                                "completion": 0,
                                                "total": 0,
                                            }
                                        agent_tokens[agent]["total"] = (
                                            int(val) if val else 0
                                        )
                                        total += int(val) if val else 0
                                run["agent_tokens"] = agent_tokens
                                run["total_prompt_tokens"] = total_prompt
                                run["total_completion_tokens"] = total_completion
                                run["total_tokens"] = total
                                run["total_agent_interactions"] = int(
                                    row.get("TotalAgentInteractions", 0)
                                )
                                run["total_router_interactions"] = int(
                                    row.get("TotalRouterInteractions", 0)
                                )
                    except Exception:
                        pass

                # Load error logs
                error_file = seed_dir / "error.log"
                if error_file.exists():
                    error_text = error_file.read_text()
                    if (
                        "Traceback" in error_text
                        or "Error" in error_text
                        or "Exception" in error_text
                    ):
                        run["has_error"] = True
                        run["error_log_lines"] = len(error_text.strip().split("\n"))

                runs.append(run)

    return runs


def filter_best_runs(runs):
    """For runs with pass variants, keep only the primary (non-pass) run."""
    # Group by (scene, agents, seed)
    primary = {}
    passes = defaultdict(list)
    for r in runs:
        key = (r["scene"], r["agents"], r["seed"])
        if r["is_pass"]:
            passes[key].append(r)
        else:
            primary[key] = r
    return list(primary.values()), passes


def analyze():
    runs_raw = load_all_results()
    runs, passes = filter_best_runs(runs_raw)

    print(f"Total raw runs: {len(runs_raw)}")
    print(f"Primary runs: {len(runs)}")
    print(f"Pass runs: {sum(len(v) for v in passes.values())}")

    # Count by scene
    scene_counts = defaultdict(int)
    for r in runs:
        scene_counts[r["scene"]] += 1
    print(f"\nRuns by scene: {dict(sorted(scene_counts.items()))}")

    # ============= SECTION 1: OVERALL SUMMARY =============
    print("\n" + "=" * 80)
    print("SECTION 1: OVERALL SUMMARY")
    print("=" * 80)

    valid = [r for r in runs if r.get("has_data")]
    failed = [r for r in runs if r.get("has_error") or not r.get("has_data")]
    print(f"Valid runs: {len(valid)}")
    print(f"Failed/empty runs: {len(failed)}")

    if valid:
        avg_cov = sum(r["coverage"] for r in valid) / len(valid)
        avg_tr = sum(r["transport_rate"] for r in valid) / len(valid)
        avg_steps = sum(r["steps"] for r in valid) / len(valid)
        finished_count = sum(1 for r in valid if r.get("finished"))
        print(f"Average coverage: {avg_cov:.4f}")
        print(f"Average transport rate: {avg_tr:.4f}")
        print(f"Average steps: {avg_steps:.1f}")
        print(
            f"Finished: {finished_count}/{len(valid)} ({finished_count / len(valid) * 100:.1f}%)"
        )

        if any("total_tokens" in r for r in valid):
            avg_tokens = sum(
                r["total_tokens"] for r in valid if "total_tokens" in r
            ) / sum(1 for r in valid if "total_tokens" in r)
            total_tokens_all = sum(
                r["total_tokens"] for r in valid if "total_tokens" in r
            )
            print(f"Average tokens per run: {avg_tokens:,.0f}")
            print(f"Total tokens across all runs: {total_tokens_all:,}")

    # ============= SECTION 2: BY SCENE =============
    print("\n" + "=" * 80)
    print("SECTION 2: PERFORMANCE BY SCENE")
    print("=" * 80)

    scenes = sorted(set(r["scene"] for r in valid))
    for scene in scenes:
        sv = [r for r in valid if r["scene"] == scene]
        if not sv:
            continue
        avg_cov = sum(r["coverage"] for r in sv) / len(sv)
        avg_tr = sum(r["transport_rate"] for r in sv) / len(sv)
        avg_steps = sum(r["steps"] for r in sv) / len(sv)
        finished = sum(1 for r in sv if r.get("finished"))
        print(f"\nScene {scene} ({len(sv)} runs):")
        print(
            f"  Coverage: {avg_cov:.4f} | Transport: {avg_tr:.4f} | Steps: {avg_steps:.1f} | Finished: {finished}/{len(sv)} ({finished / len(sv) * 100:.1f}%)"
        )

        # By agent count within scene
        agent_counts = sorted(set(r["agents"] for r in sv))
        for a in agent_counts:
            av = [r for r in sv if r["agents"] == a]
            if not av:
                continue
            avg_cov_a = sum(r["coverage"] for r in av) / len(av)
            avg_tr_a = sum(r["transport_rate"] for r in av) / len(av)
            avg_steps_a = sum(r["steps"] for r in av) / len(av)
            finished_a = sum(1 for r in av if r.get("finished"))
            tokens_a = sum(r.get("total_tokens", 0) for r in av)
            print(
                f"    {a} agents: cov={avg_cov_a:.4f} tr={avg_tr_a:.4f} steps={avg_steps_a:.1f} finished={finished_a}/{len(av)} tokens={tokens_a:,}"
            )

    # ============= SECTION 3: BY AGENT COUNT =============
    print("\n" + "=" * 80)
    print("SECTION 3: PERFORMANCE BY AGENT COUNT (across all scenes)")
    print("=" * 80)

    agent_counts = sorted(set(r["agents"] for r in valid))
    for a in agent_counts:
        av = [r for r in valid if r["agents"] == a]
        if not av:
            continue
        avg_cov = sum(r["coverage"] for r in av) / len(av)
        avg_tr = sum(r["transport_rate"] for r in av) / len(av)
        avg_steps = sum(r["steps"] for r in av) / len(av)
        finished = sum(1 for r in av if r.get("finished"))
        avg_tokens = sum(r.get("total_tokens", 0) for r in av) / len(av)
        avg_elapsed = sum(r.get("elapsed_seconds", 0) for r in av) / len(av)
        print(f"\n{a} agents ({len(av)} runs):")
        print(
            f"  Coverage: {avg_cov:.4f} | Transport: {avg_tr:.4f} | Steps: {avg_steps:.1f}"
        )
        print(
            f"  Finished: {finished}/{len(av)} ({finished / len(av) * 100:.1f}%) | Avg tokens: {avg_tokens:,.0f} | Avg time: {avg_elapsed:.0f}s"
        )

    # ============= SECTION 4: SUCCESS/FINISHED RATE DETAIL =============
    print("\n" + "=" * 80)
    print("SECTION 4: SUCCESS/FINISHED RATE BY SCENE × AGENT COUNT")
    print("=" * 80)

    header = "Scene" + "".join(f" | N={a:>8}" for a in agent_counts)
    print(header)
    print("-" * len(header))
    for scene in scenes:
        parts = [f"S{scene:>2}  "]
        for a in agent_counts:
            av = [r for r in valid if r["scene"] == scene and r["agents"] == a]
            if av:
                fin = sum(1 for r in av if r.get("finished"))
                parts.append(f" | {fin}/{len(av):>6}")
            else:
                parts.append(" |    -   ")
        print("".join(parts))

    # ============= SECTION 5: TOKEN USAGE ANALYSIS =============
    print("\n" + "=" * 80)
    print("SECTION 5: TOKEN USAGE ANALYSIS")
    print("=" * 80)

    # Aggregate coordinator vs agent tokens
    coord_tokens = defaultdict(int)
    agent_tokens_agg = defaultdict(int)
    coord_count = defaultdict(int)
    agent_count = defaultdict(int)

    for r in valid:
        tokens = r.get("agent_tokens", {})
        for agent, tdata in tokens.items():
            if agent == "Coordinator":
                coord_tokens[r["scene"]] += tdata.get("total", 0)
                coord_count[r["scene"]] += 1
            else:
                agent_tokens_agg[r["scene"]] += tdata.get("total", 0)
                agent_count[r["scene"]] += 1

    print("\nPer-scene token distribution:")
    for scene in scenes:
        ct = coord_tokens.get(scene, 0)
        at = agent_tokens_agg.get(scene, 0)
        total = ct + at
        if total > 0:
            print(
                f"  Scene {scene}: Coordinator {ct:>10,} ({ct / total * 100:.1f}%) | Agents {at:>10,} ({at / total * 100:.1f}%) | Total {total:>10,}"
            )

    # Efficiency: tokens per step
    print("\nToken efficiency (tokens per step):")
    for a in agent_counts:
        av = [
            r
            for r in valid
            if r["agents"] == a and "total_tokens" in r and r["steps"] > 0
        ]
        if av:
            avg_tps = sum(r["total_tokens"] / r["steps"] for r in av) / len(av)
            print(f"  {a} agents: {avg_tps:,.0f} tokens/step")

    # ============= SECTION 6: PASS/RETRY ANALYSIS =============
    print("\n" + "=" * 80)
    print("SECTION 6: PASS/RETRY ANALYSIS")
    print("=" * 80)

    passes_by_key = defaultdict(list)
    for key, plist in passes.items():
        passes_by_key[key] = plist

    print(f"Runs with pass/retry variants: {len(passes_by_key)}")
    for key in sorted(passes_by_key.keys()):
        plist = passes_by_key[key]
        primary = runs_dict.get(key)
        if primary:
            print(f"  Scene {key[0]}, {key[1]} agents, seed {key[2]}:")
            print(
                f"    Primary: cov={primary['coverage']:.4f} tr={primary['transport_rate']:.4f} steps={primary['steps']} finished={primary.get('finished')}"
            )
            for p in plist:
                print(
                    f"    Pass {p['pass']}: cov={p['coverage']:.4f} tr={p['transport_rate']:.4f} steps={p['steps']} finished={p.get('finished')}"
                )

    # ============= SECTION 7: STUCK/FAILED RUN ANALYSIS =============
    print("\n" + "=" * 80)
    print("SECTION 7: FAILED/ERROR RUNS")
    print("=" * 80)

    errors = [r for r in runs if r.get("has_error")]
    print(f"Total error runs: {len(errors)}")
    if errors:
        for e in sorted(errors, key=lambda x: (x["scene"], x["agents"], x["seed"])):
            err_msg = e.get("error", "")
            err_lines = e.get("error_log_lines", 0)
            print(
                f"  Scene {e['scene']} | {e['agents']} agents | seed {e['seed']} | pass={e['pass']} | err_lines={err_lines} | err={err_msg[:100]}"
            )

    # ============= SECTION 8: VARIANCE ANALYSIS =============
    print("\n" + "=" * 80)
    print("SECTION 8: VARIANCE ANALYSIS (by seed)")
    print("=" * 80)

    for scene in scenes[:3]:  # First 3 scenes
        for a in agent_counts:
            av = [r for r in valid if r["scene"] == scene and r["agents"] == a]
            if len(av) < 2:
                continue
            covs = [r["coverage"] for r in av]
            trs = [r["transport_rate"] for r in av]
            import statistics

            cov_std = statistics.stdev(covs) if len(covs) > 1 else 0
            tr_std = statistics.stdev(trs) if len(trs) > 1 else 0
            print(
                f"  S{a}S: {a}ag | cov: {min(covs):.3f}-{max(covs):.3f} (σ={cov_std:.3f}) | tr: {min(trs):.3f}-{max(trs):.3f} (σ={tr_std:.3f})"
            )

    return valid, runs, passes


if __name__ == "__main__":
    valid, runs, passes = analyze()
    runs_dict = {(r["scene"], r["agents"], r["seed"]): r for r in runs}
