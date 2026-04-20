#!/usr/bin/env python3
"""
Gradio GUI for SkillsBench × inteSkill benchmark control.

Launch:
    python gui.py
    # Opens at http://localhost:7860
"""

import json
import os
import platform
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

import gradio as gr
import yaml

# ---------------------------------------------------------------------------
# Ensure we run from the script directory
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
os.chdir(SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Config & task discovery (reuse from run_tasks.py)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def discover_all_tasks() -> list[dict]:
    """Return list of {id, difficulty, category, tags} for all available tasks."""
    cfg = load_config()
    sb_root = Path(cfg["skillsbench"]["root"])
    task_set = cfg["skillsbench"]["task_set"]
    tasks_dir = sb_root / task_set

    if not tasks_dir.exists():
        return []

    import tomllib
    tasks = []
    exclude = set(cfg.get("tasks", {}).get("filter", {}).get("exclude", []))

    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        toml_path = task_dir / "task.toml"
        if not toml_path.exists():
            continue

        task_id = task_dir.name
        difficulty = "unknown"
        category = "unknown"
        tags = []

        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            meta = data.get("metadata", {})
            difficulty = meta.get("difficulty", "unknown")
            category = meta.get("category", "unknown")
            tags = meta.get("tags", [])
        except Exception:
            pass

        tasks.append({
            "id": task_id,
            "difficulty": difficulty,
            "category": category,
            "tags": ", ".join(tags),
            "excluded": task_id in exclude,
        })

    return tasks


# Cache tasks on load
ALL_TASKS = discover_all_tasks()

# ---------------------------------------------------------------------------
# Background runner state
# ---------------------------------------------------------------------------

class RunState:
    """Shared state between the background runner and the Gradio UI."""

    def __init__(self):
        self.running = False
        self.should_stop = False
        self.log_lines: list[str] = []
        self.results: list[dict] = []
        self.current_task = ""
        self.progress = 0.0  # 0.0 to 1.0
        self.total_tasks = 0
        self.completed_tasks = 0
        self.pass_count = 0
        self.fail_count = 0
        self.lock = threading.Lock()

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.log_lines.append(f"[{ts}] {msg}")

    def get_log(self) -> str:
        with self.lock:
            return "\n".join(self.log_lines[-200:])

    def get_results_table(self) -> list[list]:
        with self.lock:
            return [
                [r["task_id"], r["phase"], r.get("reward", "-"),
                 r.get("duration_sec", "-"), r.get("status", "-")]
                for r in self.results
            ]

    def reset(self):
        with self.lock:
            self.running = False
            self.should_stop = False
            self.log_lines = []
            self.results = []
            self.current_task = ""
            self.progress = 0.0
            self.total_tasks = 0
            self.completed_tasks = 0
            self.pass_count = 0
            self.fail_count = 0


STATE = RunState()


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------

def run_benchmark_thread(
    task_ids: list[str],
    phase: str,
    agent: str,
    rounds: int,
):
    """Run benchmark in background thread."""
    from run_tasks import (
        load_config as load_cfg,
        execute_task,
        verify_task,
        verify_task_locally,
        append_jsonl,
    )

    cfg = load_cfg()

    # Override agent in config if needed
    if agent == "openclaw":
        # execute phase uses OpenClaw
        pass
    # For harbor, use the configured model

    STATE.total_tasks = len(task_ids) * (rounds if phase == "iterate" else 1)
    STATE.completed_tasks = 0

    log_path = str(SCRIPT_DIR / "results" / "gui_execution_log.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    if phase == "iterate":
        _run_iterate(task_ids, cfg, rounds, log_path)
    else:
        _run_single_pass(task_ids, cfg, phase, agent, log_path)

    STATE.log(f"--- Done: {STATE.pass_count} pass, {STATE.fail_count} fail ---")
    with STATE.lock:
        STATE.running = False


def _run_single_pass(task_ids, cfg, phase, agent, log_path):
    from run_tasks import execute_task, verify_task, verify_task_locally, append_jsonl
    from dataclasses import asdict

    for i, task_id in enumerate(task_ids):
        if STATE.should_stop:
            STATE.log("Stopped by user.")
            break

        STATE.current_task = task_id
        STATE.log(f"[{i+1}/{len(task_ids)}] {task_id}")

        # Execute phase
        if phase in ("execute", "both"):
            STATE.log(f"  execute: sending to OpenClaw...")
            try:
                result = execute_task(task_id, cfg)
                if result.error:
                    STATE.log(f"  execute: ERROR - {result.error}")
                else:
                    STATE.log(f"  execute: done ({result.duration_sec}s)")
                append_jsonl(log_path, result)
                with STATE.lock:
                    STATE.results.append({
                        "task_id": task_id,
                        "phase": "execute",
                        "reward": "-",
                        "duration_sec": result.duration_sec,
                        "status": "ERROR" if result.error else "OK",
                    })
            except Exception as e:
                STATE.log(f"  execute: EXCEPTION - {e}")

        # Verify phase — local pytest for OpenClaw, Harbor for terminus-2
        if phase in ("verify", "both"):
            if agent == "openclaw":
                STATE.log(f"  verify-local: running pytest locally...")
                try:
                    result = verify_task_locally(task_id, cfg)
                except Exception as e:
                    STATE.log(f"  verify-local: EXCEPTION - {e}")
                    STATE.fail_count += 1
                    continue
            else:
                STATE.log(f"  verify: running harbor ({cfg['verification']['model']})...")
                try:
                    result = verify_task(task_id, cfg)
                except Exception as e:
                    STATE.log(f"  verify: EXCEPTION - {e}")
                    STATE.fail_count += 1
                    continue

            append_jsonl(log_path, result)
            passed = result.reward is not None and result.reward > 0
            if passed:
                STATE.pass_count += 1
                STATE.log(f"  {result.phase}: PASS (reward={result.reward}, {result.duration_sec}s)")
            else:
                STATE.fail_count += 1
                STATE.log(f"  {result.phase}: FAIL (reward={result.reward}, {result.duration_sec}s)")
                for f in (result.failures or [])[:3]:
                    STATE.log(f"    - {f.test_name}: {f.error_type}")
            with STATE.lock:
                STATE.results.append({
                    "task_id": task_id,
                    "phase": result.phase,
                    "reward": result.reward,
                    "duration_sec": result.duration_sec,
                    "status": "PASS" if passed else "FAIL",
                })

        STATE.completed_tasks += 1
        STATE.progress = STATE.completed_tasks / max(STATE.total_tasks, 1)

        # Interval
        if i < len(task_ids) - 1 and not STATE.should_stop:
            time.sleep(cfg["execution"].get("interval_sec", 5))


def _run_iterate(task_ids, cfg, rounds, log_path):
    from export_skills import find_skills, snapshot_skills, export_to_task
    from run_tasks import verify_task, append_jsonl

    sb_root = Path(cfg["skillsbench"]["root"]).resolve()
    skills_dir = os.path.expanduser("~/.openclaw/workspace/skills")
    results_dir = str(SCRIPT_DIR / "results")

    for round_num in range(1, rounds + 1):
        if STATE.should_stop:
            STATE.log("Stopped by user.")
            break

        STATE.log(f"=== Round {round_num}/{rounds} ===")

        # Export skills
        skills = find_skills(skills_dir)
        STATE.log(f"  Exporting {len(skills)} skills...")
        for task_id in task_ids:
            export_to_task(task_id, skills, sb_root)

        # Snapshot
        snapshot_dir = os.path.join(results_dir, "skill_snapshots", f"round_{round_num}")
        snapshot_skills(skills, snapshot_dir)

        # Verify
        round_pass = 0
        for i, task_id in enumerate(task_ids):
            if STATE.should_stop:
                break
            STATE.current_task = task_id
            STATE.log(f"  [{i+1}/{len(task_ids)}] {task_id}...")

            result = verify_task(task_id, cfg)
            append_jsonl(log_path, result)

            passed = result.reward is not None and result.reward > 0
            if passed:
                round_pass += 1
                STATE.pass_count += 1
                STATE.log(f"    PASS ({result.duration_sec}s)")
            else:
                STATE.fail_count += 1
                STATE.log(f"    FAIL ({len(result.failures)} failures)")

            with STATE.lock:
                STATE.results.append({
                    "task_id": task_id,
                    "phase": f"verify-r{round_num}",
                    "reward": result.reward,
                    "duration_sec": result.duration_sec,
                    "status": "PASS" if passed else "FAIL",
                })

            STATE.completed_tasks += 1
            STATE.progress = STATE.completed_tasks / max(STATE.total_tasks, 1)

        rate = round_pass / len(task_ids) if task_ids else 0
        STATE.log(f"  Round {round_num}: {round_pass}/{len(task_ids)} ({100*rate:.1f}%)")

        # Send feedback for failed tasks via OpenClaw
        if round_num < rounds:
            from iterate import construct_feedback, send_feedback
            from run_tasks import TaskResult
            failed_results = [
                r for r in STATE.results
                if r.get("phase") == f"verify-r{round_num}" and r.get("status") == "FAIL"
            ]
            if failed_results:
                STATE.log(f"  Sending feedback for {len(failed_results)} failures...")
                # Note: actual feedback sending requires full TaskResult objects
                # This is simplified - the iterate.py script handles the full flow

        if rate == 1.0:
            STATE.log(f"  All tasks passed! Stopping early.")
            break


# ---------------------------------------------------------------------------
# Gradio event handlers
# ---------------------------------------------------------------------------

def filter_tasks(difficulty: str, category: str, search: str) -> list[list]:
    """Filter task table based on selections."""
    rows = []
    for t in ALL_TASKS:
        if difficulty and difficulty != "all" and t["difficulty"] != difficulty:
            continue
        if category and category != "all" and t["category"] != category:
            continue
        if search and search.lower() not in t["id"].lower() and search.lower() not in t["tags"].lower():
            continue
        rows.append([
            t["id"],
            t["difficulty"],
            t["category"],
            t["tags"],
            "excluded" if t["excluded"] else "available",
        ])
    return rows


def get_task_sets() -> list[str]:
    """Discover task set directories inside the skillsbench root."""
    try:
        cfg = load_config()
        sb_root = Path(cfg["skillsbench"]["root"])
        sets = [
            d.name for d in sorted(sb_root.iterdir())
            if d.is_dir() and d.name.startswith("tasks") and any(d.rglob("task.toml"))
        ]
        return sets if sets else ["tasks", "tasks-no-skills"]
    except Exception:
        return ["tasks", "tasks-no-skills"]


def set_task_set(task_set: str):
    """Persist task_set to config.yaml and reload ALL_TASKS."""
    global ALL_TASKS
    import re
    cfg_path = SCRIPT_DIR / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    text = re.sub(r'(task_set:\s*)"[^"]+"', f'\\1"{task_set}"', text)
    cfg_path.write_text(text, encoding="utf-8")
    ALL_TASKS = discover_all_tasks()
    return filter_tasks_run("all", "all", ""), "", f"Loaded {len(ALL_TASKS)} tasks from '{task_set}'"


def filter_tasks_run(difficulty: str, category: str, search: str) -> list[list]:
    """Filter task table for Run tab, with a Select checkbox as the first column."""
    rows = []
    for t in ALL_TASKS:
        if difficulty and difficulty != "all" and t["difficulty"] != difficulty:
            continue
        if category and category != "all" and t["category"] != category:
            continue
        if search and search.lower() not in t["id"].lower() and search.lower() not in t["tags"].lower():
            continue
        rows.append([
            False,
            t["id"],
            t["difficulty"],
            t["category"],
            t["tags"],
            "excluded" if t["excluded"] else "available",
        ])
    return rows


def update_selected_from_table(table_data) -> str:
    """Derive selected task IDs from checkbox column of the run table."""
    if table_data is None:
        return ""
    # Gradio passes a pandas DataFrame; convert to plain list of lists first
    try:
        import pandas as pd
        if isinstance(table_data, pd.DataFrame):
            rows = table_data.values.tolist()
        else:
            rows = table_data
    except ImportError:
        rows = table_data

    selected = []
    for row in rows:
        if len(row) > 0 and row[0]:
            selected.append(str(row[1]))
    return "\n".join(selected)


def filter_and_clear_run(difficulty: str, category: str, search: str):
    """Re-filter run table and reset selected tasks."""
    return filter_tasks_run(difficulty, category, search), ""


def get_categories() -> list[str]:
    cats = sorted(set(t["category"] for t in ALL_TASKS))
    return ["all"] + cats


def get_difficulties() -> list[str]:
    return ["all", "easy", "medium", "hard"]


def get_task_detail(task_id: str) -> dict:
    """Load full detail for a single task."""
    cfg = load_config()
    sb_root = Path(cfg["skillsbench"]["root"])
    task_set = cfg["skillsbench"]["task_set"]
    task_dir = sb_root / task_set / task_id

    detail = {"id": task_id}

    if not task_dir.exists():
        detail["error"] = f"Task directory not found: {task_dir}"
        return detail

    # task.toml
    toml_path = task_dir / "task.toml"
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            detail["toml"] = tomllib.load(f)

    # instruction.md
    instruction_path = task_dir / "instruction.md"
    if instruction_path.exists():
        detail["instruction"] = instruction_path.read_text(encoding="utf-8", errors="replace")

    # solution/solve.sh
    solve_path = task_dir / "solution" / "solve.sh"
    if solve_path.exists():
        detail["solve_sh"] = solve_path.read_text(encoding="utf-8", errors="replace")

    # solution files (list all, read .py/.sh/.js)
    solution_dir = task_dir / "solution"
    if solution_dir.exists():
        sol_files = {}
        for f in sorted(solution_dir.iterdir()):
            if f.is_file() and f.suffix in (".py", ".sh", ".js", ".ts", ".txt", ".md"):
                try:
                    sol_files[f.name] = f.read_text(encoding="utf-8", errors="replace")[:5000]
                except Exception:
                    sol_files[f.name] = "(binary or unreadable)"
            elif f.is_file():
                sol_files[f.name] = f"({f.suffix} file, {f.stat().st_size} bytes)"
        detail["solution_files"] = sol_files

    # tests/test_outputs.py
    test_path = task_dir / "tests" / "test_outputs.py"
    if test_path.exists():
        detail["test_code"] = test_path.read_text(encoding="utf-8", errors="replace")

    # environment files
    env_dir = task_dir / "environment"
    if env_dir.exists():
        env_files = []
        for f in sorted(env_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(env_dir)
                env_files.append(str(rel))
        detail["env_files"] = env_files

        # Dockerfile
        dockerfile = env_dir / "Dockerfile"
        if dockerfile.exists():
            detail["dockerfile"] = dockerfile.read_text(encoding="utf-8", errors="replace")

        # skills
        skills_dir = env_dir / "skills"
        if skills_dir.exists() and any(skills_dir.iterdir()):
            skill_names = [d.name for d in skills_dir.iterdir() if d.is_dir()]
            detail["skills"] = skill_names

    return detail


def format_task_detail(task_id: str) -> tuple[str, str, str, str, str]:
    """Format task detail into display strings for the UI components.
    Returns: (header_md, instruction_md, solution_md, tests_md, env_md)
    """
    if not task_id or not task_id.strip():
        empty = "*Select a task to view details*"
        return empty, "", "", "", ""

    task_id = task_id.strip()
    detail = get_task_detail(task_id)

    if "error" in detail:
        return f"**Error:** {detail['error']}", "", "", "", ""

    # Header
    toml = detail.get("toml", {})
    meta = toml.get("metadata", {})
    header_parts = [
        f"## {task_id}",
        "",
        f"**Difficulty:** {meta.get('difficulty', '?')} | "
        f"**Category:** {meta.get('category', '?')} | "
        f"**Tags:** {', '.join(meta.get('tags', []))}",
        "",
        f"**Author:** {meta.get('author_name', '?')} ({meta.get('author_email', '')})",
    ]
    env_cfg = toml.get("environment", {})
    if env_cfg:
        header_parts.append(
            f"**Docker:** {env_cfg.get('image', '?')} | "
            f"CPU: {env_cfg.get('cpus', '?')} | "
            f"RAM: {env_cfg.get('memory_mb', '?')}MB | "
            f"Disk: {env_cfg.get('storage_mb', '?')}MB"
        )
    agent_cfg = toml.get("agent", {})
    verifier_cfg = toml.get("verifier", {})
    header_parts.append(
        f"**Timeout:** agent {agent_cfg.get('timeout_sec', '?')}s, "
        f"verifier {verifier_cfg.get('timeout_sec', '?')}s"
    )
    if detail.get("skills"):
        header_parts.append(f"**Human Skills:** {', '.join(detail['skills'])}")
    header_md = "\n".join(header_parts)

    # Instruction
    instruction_md = detail.get("instruction", "*No instruction.md found*")

    # Solution
    sol_parts = []
    if detail.get("solve_sh"):
        sol_parts.append("### solve.sh\n```bash\n" + detail["solve_sh"] + "\n```")
    for fname, content in detail.get("solution_files", {}).items():
        if fname == "solve.sh":
            continue
        ext = Path(fname).suffix.lstrip(".")
        sol_parts.append(f"### {fname}\n```{ext}\n{content}\n```")
    solution_md = "\n\n".join(sol_parts) if sol_parts else "*No solution files*"

    # Tests
    if detail.get("test_code"):
        tests_md = f"```python\n{detail['test_code']}\n```"
    else:
        tests_md = "*No test_outputs.py found*"

    # Environment
    env_parts = []
    if detail.get("dockerfile"):
        env_parts.append("### Dockerfile\n```dockerfile\n" + detail["dockerfile"] + "\n```")
    if detail.get("env_files"):
        env_parts.append("### Files\n" + "\n".join(f"- `{f}`" for f in detail["env_files"]))
    env_md = "\n\n".join(env_parts) if env_parts else "*No environment info*"

    return header_md, instruction_md, solution_md, tests_md, env_md


def start_run(
    selected_tasks_str: str,
    difficulty: str,
    runner_val: str,
    oc_phase_val: str,
    oc_rounds_val: int,
):
    """Start the benchmark run."""
    if STATE.running:
        return "Already running. Stop first."

    # Derive phase/agent from runner selection
    if runner_val == "SkillsBench (Harbor)":
        phase = "verify"
        agent = "harbor (terminus-2)"
        rounds = 1
    else:
        _phase_map = {"execute": "execute", "execute + verify": "both", "iterate": "iterate"}
        phase = _phase_map.get(oc_phase_val, "execute")
        agent = "openclaw"
        rounds = int(oc_rounds_val)

    # Parse task selection
    task_ids = []
    if selected_tasks_str.strip():
        task_ids = [t.strip() for t in selected_tasks_str.strip().split("\n") if t.strip()]
    else:
        # Use difficulty filter
        for t in ALL_TASKS:
            if t["excluded"]:
                continue
            if difficulty and difficulty != "all" and t["difficulty"] != difficulty:
                continue
            task_ids.append(t["id"])

    if not task_ids:
        return "No tasks selected."

    STATE.reset()
    STATE.running = True
    STATE.log(f"Starting: {len(task_ids)} tasks, phase={phase}, agent={agent}, rounds={rounds}")
    STATE.log(f"Tasks: {', '.join(task_ids[:10])}{'...' if len(task_ids) > 10 else ''}")

    thread = threading.Thread(
        target=run_benchmark_thread,
        args=(task_ids, phase, agent, rounds),
        daemon=True,
    )
    thread.start()
    return f"Started: {len(task_ids)} tasks"


def stop_run():
    if STATE.running:
        STATE.should_stop = True
        STATE.log("Stop requested...")
        return "Stopping after current task..."
    return "Not running."


def poll_status():
    """Called by a timer to update the UI."""
    pct = int(STATE.progress * 100)
    status = "Idle"
    if STATE.running:
        status = f"Running: {STATE.current_task} ({STATE.completed_tasks}/{STATE.total_tasks})"
    elif STATE.completed_tasks > 0:
        status = "Complete"

    summary = f"{status} | Pass: {STATE.pass_count} | Fail: {STATE.fail_count} | Progress: {pct}%"
    return (
        STATE.get_log(),
        STATE.get_results_table(),
        summary,
        STATE.progress,
    )


# ---------------------------------------------------------------------------
# Load existing results for display
# ---------------------------------------------------------------------------

def load_task_skill_log() -> str:
    """Load task→skill mapping log."""
    log_path = SCRIPT_DIR / "results" / "task_skill_log.jsonl"
    if not log_path.exists():
        return "暂无记录"
    lines = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            skills = ", ".join(entry.get("new_skills", [])) or "（无新 skill）"
            lines.append(f"{entry['task_id']}  →  {skills}")
        except Exception:
            pass
    return "\n".join(lines) if lines else "暂无记录"


def load_existing_results():
    """Load results from all JSONL files in results/."""
    results_dir = SCRIPT_DIR / "results"
    if not results_dir.exists():
        return "No results directory found."

    from analyze import load_results, analyze_group

    text_parts = []
    for fname in sorted(results_dir.glob("*.jsonl")):
        results = load_results(str(fname))
        if not results:
            continue
        stats = analyze_group(results, fname.name)
        if stats["total"] > 0:
            text_parts.append(
                f"{fname.name}: {stats['passed']}/{stats['total']} "
                f"({100*stats['pass_rate']:.1f}%) avg {stats.get('avg_duration', 0):.0f}s"
            )

    return "\n".join(text_parts) if text_parts else "No results found."


# ---------------------------------------------------------------------------
# Build Gradio UI
# ---------------------------------------------------------------------------

def build_ui() -> gr.Blocks:
    # Pre-build task ID list for the detail dropdown
    task_id_choices = [t["id"] for t in ALL_TASKS]

    with gr.Blocks(
        title="SkillsBench x inteSkill Benchmark",
    ) as app:
        gr.Markdown("# SkillsBench x inteSkill Benchmark Dashboard")

        with gr.Tabs():
            # ==================================================================
            # Tab 1: Run Benchmark
            # ==================================================================
            with gr.Tab("Run Benchmark"):
                gr.Markdown("选择任务、配置运行参数、查看实时进度和结果。")

                with gr.Row():
                    # ---- Left: Task selection ----
                    with gr.Column(scale=2):
                        gr.Markdown("### Task Selection")

                        with gr.Row():
                            diff_filter = gr.Dropdown(
                                choices=get_difficulties(),
                                value="all",
                                label="Difficulty",
                            )
                            cat_filter = gr.Dropdown(
                                choices=get_categories(),
                                value="all",
                                label="Category",
                            )
                            search_box = gr.Textbox(label="Search", placeholder="task name or tag...")

                        task_table = gr.Dataframe(
                            headers=["Select", "Task ID", "Difficulty", "Category", "Tags", "Status"],
                            datatype=["bool", "str", "str", "str", "str", "str"],
                            col_count=(6, "fixed"),
                            value=filter_tasks_run("all", "all", ""),
                            interactive=True,
                        )

                        selected_tasks = gr.Textbox(
                            label="Selected Tasks (勾选上方任务后自动填入，留空则跑全部筛选结果)",
                            placeholder="暂无选中任务",
                            lines=3,
                            interactive=True,
                        )

                        # Filter events — reset table and clear selection
                        diff_filter.change(
                            filter_and_clear_run, [diff_filter, cat_filter, search_box], [task_table, selected_tasks]
                        )
                        cat_filter.change(
                            filter_and_clear_run, [diff_filter, cat_filter, search_box], [task_table, selected_tasks]
                        )
                        search_box.change(
                            filter_and_clear_run, [diff_filter, cat_filter, search_box], [task_table, selected_tasks]
                        )

                        # Checkbox changes → update selected tasks
                        task_table.change(
                            update_selected_from_table, inputs=[task_table], outputs=[selected_tasks]
                        )

                    # ---- Right: Controls ----
                    with gr.Column(scale=1):
                        runner = gr.Radio(
                            choices=["SkillsBench (Harbor)", "OpenClaw"],
                            value="SkillsBench (Harbor)",
                            label="Runner",
                            info="选择执行任务的 Agent",
                        )

                        # ---- SkillsBench (Harbor) 配置 ----
                        with gr.Group() as harbor_group:
                            _vcfg = load_config()["verification"]
                            _model_short = _vcfg['model'].split("/")[-1]
                            task_set_dd = gr.Dropdown(
                                choices=get_task_sets(),
                                value=load_config()["skillsbench"]["task_set"],
                                label="Task Set",
                                info=f"tasks=内置skill  |  tasks-no-skills=无skill  |  验证模型: {_model_short}",
                            )

                        # ---- OpenClaw 配置 ----
                        with gr.Group(visible=False) as openclaw_group:
                            oc_phase = gr.Radio(
                                choices=["execute", "execute + verify", "iterate"],
                                value="execute + verify",
                                label="Phase",
                                info="execute=仅解题  |  execute + verify=解题+本地验证  |  iterate=多轮进化",
                            )
                            oc_rounds = gr.Slider(
                                minimum=1, maximum=10, step=1, value=3,
                                label="Rounds（iterate 时有效）",
                            )

                        with gr.Row():
                            start_btn = gr.Button("Start", variant="primary", size="lg")
                            stop_btn = gr.Button("Stop", variant="stop", size="lg")

                        status_text = gr.Textbox(
                            label="Status",
                            value="Idle",
                            interactive=False,
                        )
                        progress_bar = gr.Slider(
                            minimum=0, maximum=1, value=0,
                            label="Progress",
                            interactive=False,
                        )

                        existing_results = gr.Textbox(
                            value=load_existing_results(),
                            label="历史结果",
                            interactive=False,
                            lines=5,
                        )

                        skill_log_box = gr.Textbox(
                            value=load_task_skill_log(),
                            label="Task → Skill 对应",
                            interactive=False,
                            lines=5,
                        )
                        refresh_btn = gr.Button("Refresh")
                        refresh_btn.click(
                            lambda: (load_existing_results(), load_task_skill_log()),
                            outputs=[existing_results, skill_log_box],
                        )

                # ---- Bottom: Log & Results ----
                gr.Markdown("---")
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### Live Log")
                        log_output = gr.Textbox(
                            value="",
                            lines=18,
                            max_lines=18,
                            interactive=False,
                        )

                    with gr.Column(scale=1):
                        gr.Markdown("### Results")
                        results_table = gr.Dataframe(
                            headers=["Task ID", "Phase", "Reward", "Duration(s)", "Status"],
                            value=[],
                            interactive=False,
                        )

                # ---- Event bindings ----
                def on_runner_change(runner_val):
                    is_harbor = runner_val == "SkillsBench (Harbor)"
                    return gr.update(visible=is_harbor), gr.update(visible=not is_harbor)

                runner.change(
                    on_runner_change,
                    inputs=[runner],
                    outputs=[harbor_group, openclaw_group],
                )
                task_set_dd.change(
                    set_task_set,
                    inputs=[task_set_dd],
                    outputs=[task_table, selected_tasks, status_text],
                )
                start_btn.click(
                    start_run,
                    inputs=[selected_tasks, diff_filter, runner, oc_phase, oc_rounds],
                    outputs=[status_text],
                )
                stop_btn.click(stop_run, outputs=[status_text])

                # Auto-refresh timer (every 2 seconds)
                timer = gr.Timer(2)
                timer.tick(
                    poll_status,
                    outputs=[log_output, results_table, status_text, progress_bar],
                )

            # ==================================================================
            # Tab 2: Task Detail
            # ==================================================================
            with gr.Tab("Task Detail"):
                gr.Markdown("按难度/类别浏览任务，点击查看完整描述、答案、测试和环境配置。")

                with gr.Row():
                    # ---- Left: browse & filter ----
                    with gr.Column(scale=1):
                        gr.Markdown("### Browse Tasks")
                        with gr.Row():
                            detail_diff = gr.Dropdown(
                                choices=get_difficulties(),
                                value="all",
                                label="Difficulty",
                            )
                            detail_cat = gr.Dropdown(
                                choices=get_categories(),
                                value="all",
                                label="Category",
                            )
                        detail_search = gr.Textbox(label="Search", placeholder="task name or tag...")

                        detail_task_list = gr.Dataframe(
                            headers=["Task ID", "Difficulty", "Category", "Tags", "Status"],
                            value=filter_tasks("all", "all", ""),
                            interactive=False,
                        )

                        # Filter events for detail tab
                        detail_diff.change(
                            filter_tasks, [detail_diff, detail_cat, detail_search], [detail_task_list]
                        )
                        detail_cat.change(
                            filter_tasks, [detail_diff, detail_cat, detail_search], [detail_task_list]
                        )
                        detail_search.change(
                            filter_tasks, [detail_diff, detail_cat, detail_search], [detail_task_list]
                        )

                    # ---- Right: detail view ----
                    with gr.Column(scale=2):
                        with gr.Row():
                            detail_task_picker = gr.Dropdown(
                                choices=task_id_choices,
                                label="Select Task",
                                allow_custom_value=True,
                            )
                            detail_load_btn = gr.Button("Load", variant="primary")

                        detail_header = gr.Markdown("*Select a task to view details*")

                        with gr.Tabs():
                            with gr.Tab("Instruction (问题描述)"):
                                detail_instruction = gr.Markdown("")

                            with gr.Tab("Solution (参考答案)"):
                                detail_solution = gr.Markdown("")

                            with gr.Tab("Tests (验证代码)"):
                                detail_tests = gr.Markdown("")

                            with gr.Tab("Environment (环境配置)"):
                                detail_env = gr.Markdown("")

                # When user clicks a row in the task list, update the picker and load detail
                def on_task_row_select(difficulty, category, search, evt: gr.SelectData):
                    row = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
                    rows = filter_tasks(difficulty, category, search)
                    if 0 <= row < len(rows):
                        task_id = rows[row][0]
                        return (task_id, *format_task_detail(task_id))
                    return ("", *format_task_detail(""))

                detail_task_list.select(
                    on_task_row_select,
                    inputs=[detail_diff, detail_cat, detail_search],
                    outputs=[detail_task_picker, detail_header, detail_instruction, detail_solution, detail_tests, detail_env],
                )

                # Load detail on button click or dropdown change
                detail_load_btn.click(
                    format_task_detail,
                    inputs=[detail_task_picker],
                    outputs=[detail_header, detail_instruction, detail_solution, detail_tests, detail_env],
                )
                detail_task_picker.change(
                    format_task_detail,
                    inputs=[detail_task_picker],
                    outputs=[detail_header, detail_instruction, detail_solution, detail_tests, detail_env],
                )

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = build_ui()
    server_name = "localhost" if platform.system() == "Windows" else "0.0.0.0"
    app.launch(
        server_name=server_name,
        server_port=7860,
        share=False,
        theme=gr.themes.Soft(),
    )
