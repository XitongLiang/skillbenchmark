#!/usr/bin/env python3
"""
Analyze and compare benchmark results across experiment groups.

Usage:
    python analyze.py                                    # analyze all results in results/
    python analyze.py --groups A C D                      # compare specific groups
    python analyze.py --iteration-curve results/iteration_curve.json
"""

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_results(path: str) -> list[dict]:
    """Load JSONL results file."""
    results = []
    if not os.path.exists(path):
        return results
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return results


def load_task_metadata(cfg: dict) -> dict[str, dict]:
    """Load task.toml metadata for category/difficulty grouping."""
    import tomllib

    sb_root = Path(cfg["skillsbench"]["root"])
    task_set = cfg["skillsbench"]["task_set"]
    tasks_dir = sb_root / task_set
    metadata = {}

    if not tasks_dir.exists():
        return metadata

    for task_dir in tasks_dir.iterdir():
        if not task_dir.is_dir():
            continue
        toml_path = task_dir / "task.toml"
        if toml_path.exists():
            try:
                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
                meta = data.get("metadata", {})
                metadata[task_dir.name] = {
                    "difficulty": meta.get("difficulty", "unknown"),
                    "category": meta.get("category", "unknown"),
                    "tags": meta.get("tags", []),
                }
            except Exception:
                pass

    return metadata


def analyze_group(results: list[dict], label: str) -> dict:
    """Compute stats for a single experiment group."""
    verify_results = [r for r in results if r.get("phase") == "verify"]
    if not verify_results:
        return {"label": label, "total": 0}

    total = len(verify_results)
    passed = sum(1 for r in verify_results if (r.get("reward") or 0) > 0)

    return {
        "label": label,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total > 0 else 0,
        "avg_duration": round(
            sum(r.get("duration_sec", 0) for r in verify_results) / total, 1
        ),
    }


def analyze_by_dimension(
    results: list[dict], metadata: dict[str, dict], dimension: str
) -> dict[str, dict]:
    """Break down results by difficulty or category."""
    groups = defaultdict(list)
    for r in results:
        if r.get("phase") != "verify":
            continue
        task_id = r.get("task_id", "")
        meta = metadata.get(task_id, {})
        key = meta.get(dimension, "unknown")
        groups[key].append(r)

    breakdown = {}
    for key, group_results in sorted(groups.items()):
        total = len(group_results)
        passed = sum(1 for r in group_results if (r.get("reward") or 0) > 0)
        breakdown[key] = {
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / total, 4) if total > 0 else 0,
        }

    return breakdown


def print_comparison_table(groups: list[dict]):
    """Print a formatted comparison table."""
    if not groups:
        print("No results to compare.")
        return

    print(f"\n{'Group':<30} {'Total':>6} {'Pass':>6} {'Rate':>8} {'Avg Time':>10}")
    print("-" * 65)
    for g in groups:
        if g["total"] == 0:
            continue
        print(
            f"{g['label']:<30} {g['total']:>6} {g['passed']:>6} "
            f"{100*g['pass_rate']:>7.1f}% {g.get('avg_duration', 0):>9.1f}s"
        )


def print_breakdown_table(breakdown: dict[str, dict], dimension: str):
    """Print breakdown by difficulty or category."""
    print(f"\n  By {dimension}:")
    print(f"  {'Value':<25} {'Total':>6} {'Pass':>6} {'Rate':>8}")
    print(f"  {'-'*50}")
    for key, stats in breakdown.items():
        print(
            f"  {key:<25} {stats['total']:>6} {stats['passed']:>6} "
            f"{100*stats['pass_rate']:>7.1f}%"
        )


def print_iteration_curve(curve_path: str):
    """Print iteration convergence curve."""
    if not os.path.exists(curve_path):
        print(f"No iteration curve found at {curve_path}")
        return

    with open(curve_path) as f:
        curve = json.load(f)

    print(f"\nIteration Convergence Curve:")
    print(f"{'Round':>6} {'Pass Rate':>10} {'Skills':>8} {'Feedback':>10} {'Bar':>42}")
    print("-" * 80)
    for entry in curve:
        rate = entry["pass_rate"]
        bar = "#" * int(rate * 40)
        print(
            f"{entry['round']:>6} {100*rate:>9.1f}% {entry['n_skills']:>8} "
            f"{entry.get('feedback_sent', 0):>10} |{bar:<40}|"
        )


def main():
    parser = argparse.ArgumentParser(description="Analyze benchmark results")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--groups", nargs="+", help="Groups to compare (A, B, C, D)")
    parser.add_argument("--iteration-curve", help="Path to iteration_curve.json")
    parser.add_argument("--by-difficulty", action="store_true")
    parser.add_argument("--by-category", action="store_true")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    cfg = load_config(args.config)

    results_dir = args.results_dir

    # Map group labels to expected file patterns
    group_files = {
        "A": "results_no_skill.jsonl",
        "B": "results_human_skill.jsonl",
        "C": "results_inteskill_v1.jsonl",
        "D": "results_inteskill_iter_final.jsonl",
    }

    # Also check for generic execution log
    if os.path.exists(os.path.join(results_dir, "execution_log.jsonl")):
        group_files["current"] = "execution_log.jsonl"

    # Determine which groups to analyze
    target_groups = args.groups or list(group_files.keys())

    # Load and analyze
    analyzed = []
    for group_label in target_groups:
        filename = group_files.get(group_label)
        if not filename:
            continue
        filepath = os.path.join(results_dir, filename)
        results = load_results(filepath)
        if results:
            stats = analyze_group(results, f"Group {group_label} ({filename})")
            analyzed.append((stats, results))

    if not analyzed:
        print("No results found. Run some experiments first.")
        print(f"Expected files in {results_dir}/:")
        for label, fname in group_files.items():
            print(f"  Group {label}: {fname}")
        return

    # Comparison table
    print("=" * 65)
    print("  SkillsBench Results Comparison")
    print("=" * 65)
    print_comparison_table([a[0] for a in analyzed])

    # Breakdown by difficulty/category
    metadata = load_task_metadata(cfg)
    if metadata and (args.by_difficulty or args.by_category or len(args.groups or []) == 0):
        for stats, results in analyzed:
            print(f"\n{stats['label']}:")
            if args.by_difficulty or not args.groups:
                breakdown = analyze_by_dimension(results, metadata, "difficulty")
                print_breakdown_table(breakdown, "difficulty")
            if args.by_category:
                breakdown = analyze_by_dimension(results, metadata, "category")
                print_breakdown_table(breakdown, "category")

    # Iteration curve
    curve_path = args.iteration_curve or os.path.join(results_dir, "iteration_curve.json")
    if os.path.exists(curve_path):
        print_iteration_curve(curve_path)


if __name__ == "__main__":
    main()
