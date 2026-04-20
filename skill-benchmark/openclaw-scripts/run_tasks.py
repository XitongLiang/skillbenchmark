#!/usr/bin/env python3
"""
Batch task execution for SkillsBench via OpenClaw + Harbor verification.

Usage:
    python run_tasks.py                       # run all tasks per config.yaml
    python run_tasks.py --tasks dialogue-parser jax-computing-basics
    python run_tasks.py --difficulty easy
    python run_tasks.py --phase execute       # Phase 1 only (OpenClaw)
    python run_tasks.py --phase verify        # Phase 2 only (Harbor)
    python run_tasks.py --phase both          # default: execute + verify
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TestFailure:
    test_name: str
    expected: str
    actual: str
    error_type: str


@dataclass
class TaskResult:
    task_id: str
    phase: str  # "execute" | "verify"
    reward: Optional[float] = None
    response_length: int = 0
    failures: list[TestFailure] = field(default_factory=list)
    pytest_output: str = ""
    error: str = ""
    timestamp: str = ""
    duration_sec: float = 0


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------

def discover_tasks(cfg: dict) -> list[str]:
    """Find task IDs based on config filters."""
    sb_root = Path(cfg["skillsbench"]["root"])
    task_set = cfg["skillsbench"]["task_set"]
    tasks_dir = sb_root / task_set

    if not tasks_dir.exists():
        print(f"ERROR: tasks directory not found: {tasks_dir}", file=sys.stderr)
        sys.exit(1)

    all_tasks = sorted(
        d.name for d in tasks_dir.iterdir() if d.is_dir() and (d / "task.toml").exists()
    )

    filt = cfg.get("tasks", {}).get("filter", {})

    # Include filter
    include = filt.get("include", [])
    if include:
        all_tasks = [t for t in all_tasks if t in include]

    # Exclude filter
    exclude = set(filt.get("exclude", []))
    all_tasks = [t for t in all_tasks if t not in exclude]

    # Difficulty filter
    diff_filter = filt.get("difficulty")
    if diff_filter:
        filtered = []
        for task_id in all_tasks:
            toml_path = tasks_dir / task_id / "task.toml"
            content = toml_path.read_text()
            if f'difficulty = "{diff_filter}"' in content:
                filtered.append(task_id)
        all_tasks = filtered

    return all_tasks


# ---------------------------------------------------------------------------
# Phase 1: OpenClaw execution
# ---------------------------------------------------------------------------

CONTAINER_ROOTS = ["/root/", "/app/", "/workspace/", "/home/github/build/failed/", "/home/github/build/"]


def rewrite_paths(instruction: str, workspace: str) -> str:
    """Replace container paths with local workspace paths."""
    for root in CONTAINER_ROOTS:
        instruction = instruction.replace(root, f"{workspace}/")
    # Also handle without trailing slash
    for root in [r.rstrip("/") for r in CONTAINER_ROOTS]:
        instruction = instruction.replace(root, workspace)
    return instruction


def extract_docker_contents(dockerfile_path: str, workspace: str) -> bool:
    """Extract code/data from Docker image into local workspace.

    Parses the Dockerfile to find the base image, creates a temporary
    container, and copies relevant directories out.
    """
    try:
        content = Path(dockerfile_path).read_text()
    except Exception:
        return False

    # Find base image from FROM line
    image = None
    for line in content.splitlines():
        line = line.strip()
        if line.upper().startswith("FROM "):
            image = line.split()[1]
            break
    if not image:
        return False

    # Find WORKDIR or common data paths to extract
    extract_paths = []
    for line in content.splitlines():
        line = line.strip()
        if line.upper().startswith("WORKDIR "):
            extract_paths.append(line.split()[1])

    # Also check for common BugSwarm / SkillsBench paths
    for p in ["/home/github/build/failed", "/app", "/root"]:
        if p not in extract_paths:
            extract_paths.append(p)

    # Create temp container, copy files out, remove container
    container_name = f"skillbench-extract-{os.getpid()}"
    try:
        # Pull image if needed + create container
        result = subprocess.run(
            ["docker", "create", "--name", container_name, image],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return False

        extracted = False
        for src_path in extract_paths:
            dst = os.path.join(workspace, src_path.lstrip("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            cp_result = subprocess.run(
                ["docker", "cp", f"{container_name}:{src_path}", dst],
                capture_output=True, text=True, timeout=120,
            )
            if cp_result.returncode == 0:
                extracted = True

        return extracted
    except (subprocess.TimeoutExpired, Exception):
        return False
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, timeout=30,
        )


def prepare_workspace(task_id: str, cfg: dict) -> str:
    """Set up local workspace with task environment data."""
    workspace = str(Path(cfg["execution"]["workspace_base"]).resolve() / task_id)

    # Clean previous run
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(workspace, exist_ok=True)

    # Copy environment data (exclude Dockerfile, skills/, tests/)
    sb_root = Path(cfg["skillsbench"]["root"])
    task_dir = sb_root / cfg["skillsbench"]["task_set"] / task_id
    env_dir = task_dir / "environment"
    has_local_data = False
    if env_dir.exists():
        for item in env_dir.iterdir():
            if item.name in ("Dockerfile", "skills", "docker-compose.yaml"):
                continue
            dst = os.path.join(workspace, item.name)
            if item.is_dir():
                shutil.copytree(item, dst)
                has_local_data = True
            else:
                shutil.copy2(item, dst)
                has_local_data = True

    # If environment/ had no data files, try extracting from Docker image
    if not has_local_data:
        dockerfile = env_dir / "Dockerfile" if env_dir.exists() else None
        if dockerfile and dockerfile.exists():
            print(f"    extracting code from Docker image...")
            extract_docker_contents(str(dockerfile), workspace)

    return workspace


def _snapshot_skill_names(skills_dir: str) -> set[str]:
    """Return set of skill directory names currently in the skills dir."""
    p = Path(skills_dir)
    if not p.exists():
        return set()
    return {d.name for d in p.iterdir() if d.is_dir() and (d / "SKILL.md").exists()}


def _record_task_skills(task_id: str, new_skills: set[str], log_dir: str):
    """Append task→skills mapping to task_skill_log.jsonl."""
    os.makedirs(log_dir, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "task_id": task_id,
        "new_skills": sorted(new_skills),
    }
    log_path = os.path.join(log_dir, "task_skill_log.jsonl")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def execute_task(task_id: str, cfg: dict) -> TaskResult:
    """Phase 1: Send instruction to OpenClaw and collect response."""
    from openclaw_client import OpenClawClient

    start = time.time()
    timestamp = datetime.now().isoformat()

    # Snapshot skills before execution to detect new ones
    skills_dir = os.path.expanduser("~/.openclaw/memory/skills")
    skills_before = _snapshot_skill_names(skills_dir)

    # Prepare workspace
    workspace = prepare_workspace(task_id, cfg)

    # Read instruction
    sb_root = Path(cfg["skillsbench"]["root"])
    instruction_path = sb_root / cfg["skillsbench"]["task_set"] / task_id / "instruction.md"
    instruction = instruction_path.read_text()
    instruction = rewrite_paths(instruction, workspace)

    # Add workspace context
    instruction = (
        f"你是一个自主代码实现 Agent，正在完成一项基准测试任务。\n"
        f"规则：\n"
        f"1. 严格按照任务描述实现，不得向用户提问或要求澄清\n"
        f"2. 所有输出文件保存到工作目录：{workspace}\n"
        f"3. 完成后输出「任务完成」，列出已创建的文件\n\n"
        f"任务描述如下：\n\n"
        f"{instruction}"
    )

    # Send to OpenClaw
    oc_cfg = cfg["openclaw"]
    client = OpenClawClient(
        timeout=oc_cfg.get("recv_timeout", 600),
        agent=oc_cfg.get("agent", "main"),
    )

    session_key = f"benchmark-{task_id}-{int(time.time())}"
    try:
        response = client.chat(session_key, instruction)
        error = ""
    except Exception as e:
        response = ""
        error = str(e)

    # Detect and log newly created skills
    skills_after = _snapshot_skill_names(skills_dir)
    new_skills = skills_after - skills_before
    if new_skills:
        results_dir = str(Path(__file__).parent / "results")
        _record_task_skills(task_id, new_skills, results_dir)

    duration = time.time() - start
    return TaskResult(
        task_id=task_id,
        phase="execute",
        response_length=len(response),
        error=error,
        timestamp=timestamp,
        duration_sec=round(duration, 1),
    )


# ---------------------------------------------------------------------------
# Phase 2: Harbor verification
# ---------------------------------------------------------------------------

def verify_task(task_id: str, cfg: dict, skill_dir: Optional[str] = None) -> TaskResult:
    """Phase 2: Run harbor verification and parse results."""
    start = time.time()
    timestamp = datetime.now().isoformat()

    sb_root = Path(cfg["skillsbench"]["root"]).resolve()
    vcfg = cfg["verification"]

    # Choose task path (with or without skills)
    if skill_dir:
        task_path = str(sb_root / "tasks" / task_id)
        # Inject skills
        skills_dst = os.path.join(task_path, "environment", "skills")
        if os.path.exists(skills_dst):
            shutil.rmtree(skills_dst)
        shutil.copytree(skill_dir, skills_dst)
    else:
        task_path = str(sb_root / cfg["skillsbench"]["task_set"] / task_id)

    # Build harbor env vars
    harbor_env_pairs = " ".join(
        f'export {k}="{v}";'
        for k, v in vcfg.get("harbor_env", {}).items()
    )

    # On Windows, route through WSL to avoid asyncio/Docker compatibility issues
    if sys.platform == "win32":
        wsl_task_path = task_path.replace("\\", "/").replace("C:", "/mnt/c").replace("c:", "/mnt/c")
        wsl_sb_root = str(sb_root).replace("\\", "/").replace("C:", "/mnt/c").replace("c:", "/mnt/c")
        bash_cmd = (
            f"source ~/.local/bin/env 2>/dev/null; "
            f"export PATH=$HOME/.local/bin:$PATH; "
            f"export PYTHONIOENCODING=utf-8; "
            f"{harbor_env_pairs} "
            f"cd '{wsl_sb_root}' && "
            f"harbor run -p '{wsl_task_path}' -a terminus-2 -m '{vcfg['model']}'"
        )
        cmd = ["wsl", "-d", "Ubuntu-20.04", "--", "bash", "-lc", bash_cmd]
        env = None
        cwd = None
    else:
        cmd = [
            "harbor", "run",
            "-p", task_path,
            "-a", "terminus-2",
            "-m", vcfg["model"],
        ]
        env = os.environ.copy()
        for k, v in vcfg.get("harbor_env", {}).items():
            env[k] = v
        cwd = str(sb_root)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=cfg["execution"]["timeout_sec"] + 120,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return TaskResult(
            task_id=task_id,
            phase="verify",
            error="harbor run timed out",
            timestamp=timestamp,
            duration_sec=round(time.time() - start, 1),
        )

    # Find latest job directory for this task
    reward = 0.0
    pytest_output = ""
    failures = []

    jobs_dir = sb_root / "jobs"
    if jobs_dir.exists():
        job_dirs = sorted(jobs_dir.iterdir(), reverse=True)
        for job_dir in job_dirs:
            trial_dirs = [
                d for d in job_dir.iterdir()
                if d.is_dir() and d.name.startswith(task_id)
            ]
            if trial_dirs:
                trial_dir = trial_dirs[0]

                # Read reward
                reward_file = trial_dir / "verifier" / "reward.txt"
                if reward_file.exists():
                    try:
                        reward = float(reward_file.read_text().strip())
                    except ValueError:
                        pass

                # Read pytest output
                test_stdout = trial_dir / "verifier" / "test-stdout.txt"
                if test_stdout.exists():
                    pytest_output = test_stdout.read_text()
                    failures = parse_pytest_failures(pytest_output)

                # Also check result.json
                result_json = trial_dir / "result.json"
                if result_json.exists():
                    try:
                        rdata = json.loads(result_json.read_text())
                        vr = rdata.get("verifier_result", {}).get("rewards", {})
                        if "reward" in vr:
                            reward = vr["reward"]
                    except (json.JSONDecodeError, KeyError):
                        pass

                break

    duration = time.time() - start
    return TaskResult(
        task_id=task_id,
        phase="verify",
        reward=reward,
        failures=failures,
        pytest_output=pytest_output[:2000],  # truncate for log
        timestamp=timestamp,
        duration_sec=round(duration, 1),
    )


def parse_pytest_failures(output: str) -> list[TestFailure]:
    """Extract structured failure info from pytest output."""
    failures = []
    # Match patterns like: FAILED tests/test_outputs.py::test_name - AssertionError: ...
    pattern = re.compile(
        r"FAILED\s+\S+::(\w+)\s*[-–]\s*(\w+Error):\s*(.*?)(?:\n|$)"
    )
    for m in pattern.finditer(output):
        test_name = m.group(1)
        error_type = m.group(2)
        detail = m.group(3).strip()

        expected = ""
        actual = ""
        # Try to parse "assert X == Y" patterns
        assert_match = re.search(r"assert\s+(.+?)\s*==\s*(.+?)$", detail)
        if assert_match:
            actual = assert_match.group(1).strip()
            expected = assert_match.group(2).strip()

        failures.append(TestFailure(
            test_name=test_name,
            expected=expected,
            actual=actual,
            error_type=error_type,
        ))

    return failures


def _parse_pytest_counts(output: str) -> tuple[int, int, int]:
    """Parse pytest summary line. Returns (passed, failed, errors)."""
    passed = failed = errors = 0
    m = re.search(r"(\d+) passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) error", output)
    if m:
        errors = int(m.group(1))
    return passed, failed, errors


def verify_task_locally(task_id: str, cfg: dict) -> "TaskResult":
    """Phase 2 (local): Run pytest against workspace output without Docker."""
    start = time.time()
    timestamp = datetime.now().isoformat()

    sb_root = Path(cfg["skillsbench"]["root"])
    task_dir = sb_root / cfg["skillsbench"]["task_set"] / task_id
    test_src = task_dir / "tests" / "test_outputs.py"

    if not test_src.exists():
        return TaskResult(
            task_id=task_id,
            phase="verify-local",
            error=f"tests/test_outputs.py not found: {test_src}",
            timestamp=timestamp,
            duration_sec=round(time.time() - start, 1),
        )

    workspace = os.path.join(cfg["execution"]["workspace_base"], task_id)
    if not os.path.exists(workspace):
        return TaskResult(
            task_id=task_id,
            phase="verify-local",
            error=f"workspace missing ({workspace}) — run execute phase first",
            timestamp=timestamp,
            duration_sec=round(time.time() - start, 1),
        )

    # Patch container paths → local workspace paths, write temp test file
    test_code = test_src.read_text(encoding="utf-8", errors="replace")
    patched = rewrite_paths(test_code, workspace)
    local_test = Path(workspace) / "_test_local.py"
    local_test.write_text(patched, encoding="utf-8")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(local_test), "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=cfg["execution"].get("timeout_sec", 120),
            cwd=workspace,
        )
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return TaskResult(
            task_id=task_id,
            phase="verify-local",
            error="pytest timed out",
            timestamp=timestamp,
            duration_sec=round(time.time() - start, 1),
        )
    finally:
        if local_test.exists():
            local_test.unlink()

    passed, failed, errors = _parse_pytest_counts(output)
    total = passed + failed + errors
    reward = (passed / total) if total > 0 else 0.0

    return TaskResult(
        task_id=task_id,
        phase="verify-local",
        reward=reward,
        failures=parse_pytest_failures(output),
        pytest_output=output[:2000],
        timestamp=timestamp,
        duration_sec=round(time.time() - start, 1),
    )


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def append_jsonl(path: str, result: TaskResult):
    """Append a result to JSONL log file."""
    with open(path, "a") as f:
        f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


def run_batch(
    task_ids: list[str],
    cfg: dict,
    phase: str = "both",
    log_path: str = "results/execution_log.jsonl",
):
    """Run a batch of tasks."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    # Load already-completed tasks for resume
    completed = set()
    if os.path.exists(log_path):
        with open(log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    completed.add((entry["task_id"], entry["phase"]))
                except (json.JSONDecodeError, KeyError):
                    pass

    total = len(task_ids)
    pass_count = 0
    fail_count = 0

    for i, task_id in enumerate(task_ids):
        print(f"\n[{i+1}/{total}] {task_id}")

        # Phase 1: Execute
        if phase in ("execute", "both"):
            if (task_id, "execute") in completed:
                print(f"  execute: skipped (already done)")
            else:
                print(f"  execute: sending to OpenClaw...")
                result = execute_task(task_id, cfg)
                append_jsonl(log_path, result)
                if result.error:
                    print(f"  execute: ERROR - {result.error}")
                else:
                    print(f"  execute: done ({result.duration_sec}s, {result.response_length} chars)")

        # Phase 2: Verify
        if phase in ("verify", "both"):
            if (task_id, "verify") in completed:
                print(f"  verify: skipped (already done)")
            else:
                print(f"  verify: running harbor...")
                result = verify_task(task_id, cfg)
                append_jsonl(log_path, result)
                if result.reward is not None and result.reward > 0:
                    pass_count += 1
                    print(f"  verify: PASS (reward={result.reward}, {result.duration_sec}s)")
                else:
                    fail_count += 1
                    print(f"  verify: FAIL (reward={result.reward}, {result.duration_sec}s)")
                    if result.failures:
                        for f in result.failures[:3]:
                            print(f"    - {f.test_name}: expected={f.expected}, actual={f.actual}")

        # Interval
        if i < total - 1:
            time.sleep(cfg["execution"].get("interval_sec", 5))

    # Summary
    print(f"\n{'='*50}")
    print(f"Batch complete: {total} tasks")
    if phase in ("verify", "both"):
        print(f"  Pass: {pass_count}/{total} ({100*pass_count/total:.1f}%)")
        print(f"  Fail: {fail_count}/{total}")
    print(f"Log: {log_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SkillsBench batch runner via OpenClaw")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--tasks", nargs="+", help="Specific task IDs to run")
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard"], help="Filter by difficulty")
    parser.add_argument("--phase", choices=["execute", "verify", "both"], default="both")
    parser.add_argument("--log", default="results/execution_log.jsonl", help="Output log path")
    parser.add_argument("--resume", action="store_true", help="Skip already-completed tasks")
    args = parser.parse_args()

    # Load config
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    cfg = load_config(args.config)

    # Determine tasks
    if args.tasks:
        task_ids = args.tasks
    else:
        if args.difficulty:
            cfg.setdefault("tasks", {}).setdefault("filter", {})["difficulty"] = args.difficulty
        task_ids = discover_tasks(cfg)

    print(f"Tasks: {len(task_ids)}")
    print(f"Phase: {args.phase}")
    print(f"Config: {args.config}")
    print()

    run_batch(task_ids, cfg, phase=args.phase, log_path=args.log)


if __name__ == "__main__":
    main()
