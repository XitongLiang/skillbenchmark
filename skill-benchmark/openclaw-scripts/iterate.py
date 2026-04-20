#!/usr/bin/env python3
"""
Iterative skill evolution via feedback loop.

Flow per round:
  1. Export current skills → inject into tasks
  2. Harbor verify all tasks → collect pytest failures
  3. For failed tasks: construct feedback → send to OpenClaw → inteSkill Evolve
  4. Snapshot skills → log results → next round

Usage:
    python iterate.py --rounds 3
    python iterate.py --rounds 3 --tasks dialogue-parser jax-computing-basics
"""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import yaml

from export_skills import find_skills, snapshot_skills, export_to_task
from openclaw_client import OpenClawClient
from run_tasks import (
    TaskResult,
    discover_tasks,
    load_config,
    verify_task,
    append_jsonl,
    parse_pytest_failures,
)


def construct_feedback(task_id: str, result: TaskResult) -> str:
    """Build a feedback message from pytest failures for inteSkill Evolve."""
    lines = [
        f"任务 `{task_id}` 执行后验证失败。",
        f"通过的测试数: {len([f for f in result.failures if False])} (无法判断)",
        f"失败的测试数: {len(result.failures)}",
        "",
        "以下是期望与实际的差异：",
    ]

    for f in result.failures:
        line = f"- **{f.test_name}** ({f.error_type})"
        if f.expected and f.actual:
            line += f": 期望 `{f.expected}`, 实际 `{f.actual}`"
        lines.append(line)

    lines.extend([
        "",
        "请根据这些反馈改进相关 skill，使下次执行能通过这些测试。",
        "重点关注：",
        "1. 输出格式是否符合要求",
        "2. 数值精度是否满足容差",
        "3. 是否遗漏了某些必需的步骤",
    ])

    return "\n".join(lines)


def send_feedback(task_id: str, feedback: str, cfg: dict) -> bool:
    """Send feedback to OpenClaw to trigger inteSkill Evolve."""
    oc_cfg = cfg["openclaw"]
    client = OpenClawClient(
        timeout=oc_cfg.get("recv_timeout", 600),
    )

    session_key = f"benchmark-evolve-{task_id}-{int(time.time())}"
    try:
        client.chat(session_key, feedback)
        return True
    except Exception as e:
        print(f"    feedback error: {e}")
        return False


def run_iteration(
    round_num: int,
    task_ids: list[str],
    cfg: dict,
    results_dir: str,
):
    """Run one iteration: verify → feedback → evolve."""
    print(f"\n{'='*60}")
    print(f"  Round {round_num}")
    print(f"{'='*60}")

    sb_root = Path(cfg["skillsbench"]["root"]).resolve()
    skills_dir = os.path.expanduser("~/.openclaw/memory/skills")

    # 1. Export current skills
    skills = find_skills(skills_dir)
    print(f"\n[1/4] Exporting {len(skills)} skills...")
    for task_id in task_ids:
        export_to_task(task_id, skills, sb_root)

    # 2. Snapshot
    snapshot_dir = os.path.join(results_dir, "skill_snapshots", f"round_{round_num}")
    snapshot_skills(skills, snapshot_dir)

    # 3. Verify all tasks
    print(f"\n[2/4] Verifying {len(task_ids)} tasks...")
    round_results = []
    pass_count = 0

    for i, task_id in enumerate(task_ids):
        print(f"  [{i+1}/{len(task_ids)}] {task_id}...", end=" ", flush=True)
        result = verify_task(task_id, cfg)
        round_results.append(result)

        if result.reward and result.reward > 0:
            pass_count += 1
            print(f"PASS ({result.duration_sec}s)")
        else:
            print(f"FAIL ({len(result.failures)} failures, {result.duration_sec}s)")

        # Log
        log_path = os.path.join(results_dir, f"results_iter_{round_num}.jsonl")
        append_jsonl(log_path, result)

    pass_rate = pass_count / len(task_ids) if task_ids else 0
    print(f"\n  Round {round_num} pass rate: {pass_count}/{len(task_ids)} ({100*pass_rate:.1f}%)")

    # 4. Send feedback for failed tasks
    failed = [r for r in round_results if not r.reward or r.reward == 0]
    if failed:
        print(f"\n[3/4] Sending feedback for {len(failed)} failed tasks...")
        evolve_log_path = os.path.join(results_dir, "evolve_log.jsonl")
        for r in failed:
            if not r.failures:
                continue
            print(f"  {r.task_id}: {len(r.failures)} failures → evolve")
            feedback = construct_feedback(r.task_id, r)
            ok = send_feedback(r.task_id, feedback, cfg)

            # Log evolve action
            evolve_entry = {
                "round": round_num,
                "task_id": r.task_id,
                "feedback_sent": ok,
                "n_failures": len(r.failures),
                "failures": [
                    {"test": f.test_name, "expected": f.expected, "actual": f.actual}
                    for f in r.failures
                ],
                "timestamp": datetime.now().isoformat(),
            }
            with open(evolve_log_path, "a") as f:
                f.write(json.dumps(evolve_entry, ensure_ascii=False) + "\n")
    else:
        print(f"\n[3/4] All tasks passed! No feedback needed.")

    # 5. Record round summary
    summary = {
        "round": round_num,
        "total_tasks": len(task_ids),
        "passed": pass_count,
        "pass_rate": round(pass_rate, 4),
        "n_skills": len(skills),
        "feedback_sent": len([r for r in failed if r.failures]),
        "timestamp": datetime.now().isoformat(),
    }
    curve_path = os.path.join(results_dir, "iteration_curve.json")
    curve = []
    if os.path.exists(curve_path):
        with open(curve_path) as f:
            curve = json.load(f)
    curve.append(summary)
    with open(curve_path, "w") as f:
        json.dump(curve, f, indent=2, ensure_ascii=False)

    print(f"\n[4/4] Round {round_num} complete.")
    return pass_rate


def main():
    parser = argparse.ArgumentParser(description="Iterative skill evolution benchmark")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--tasks", nargs="+", help="Specific task IDs")
    parser.add_argument("--results-dir", default="results")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    cfg = load_config(args.config)

    # Determine tasks
    task_ids = args.tasks or discover_tasks(cfg)

    print(f"Iterative evolution benchmark")
    print(f"  Tasks: {len(task_ids)}")
    print(f"  Rounds: {args.rounds}")
    print(f"  Results: {args.results_dir}")

    os.makedirs(args.results_dir, exist_ok=True)

    rates = []
    for round_num in range(1, args.rounds + 1):
        rate = run_iteration(round_num, task_ids, cfg, args.results_dir)
        rates.append(rate)

        # Early stop if all pass
        if rate == 1.0:
            print(f"\nAll tasks passed at round {round_num}. Stopping early.")
            break

    # Final summary
    print(f"\n{'='*60}")
    print(f"  Iteration Summary")
    print(f"{'='*60}")
    for i, rate in enumerate(rates, 1):
        bar = "#" * int(rate * 40)
        print(f"  Round {i}: {100*rate:5.1f}% |{bar:<40}|")


if __name__ == "__main__":
    main()
