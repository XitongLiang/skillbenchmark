#!/usr/bin/env python3
"""
Export inteSkill skill bank to SkillsBench format.

Reads SKILL.md files from the inteSkill skills directory and copies them
into the SkillsBench tasks/*/environment/skills/ directories.

Usage:
    python export_skills.py                          # export to all tasks
    python export_skills.py --tasks dialogue-parser   # export to specific tasks
    python export_skills.py --snapshot snapshots/v1   # save a snapshot
"""

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def find_skills(skills_dir: str) -> list[dict]:
    """Find all SKILL.md files in the inteSkill skills directory."""
    skills = []
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        return skills

    for skill_dir in skills_path.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            skills.append({
                "name": skill_dir.name,
                "path": str(skill_dir),
                "skill_md": str(skill_md),
                "has_scripts": any(
                    f.suffix in (".sh", ".py", ".js")
                    for f in skill_dir.iterdir()
                    if f.is_file()
                ),
            })

    return skills


def export_to_task(task_id: str, skills: list[dict], sb_root: Path) -> bool:
    """Copy skills into a task's environment/skills/ directory."""
    task_skills_dir = sb_root / "tasks" / task_id / "environment" / "skills"

    # Clean existing injected skills (keep original ones)
    if task_skills_dir.exists():
        for item in task_skills_dir.iterdir():
            if item.name.startswith("inteskill-"):
                shutil.rmtree(item) if item.is_dir() else item.unlink()

    os.makedirs(task_skills_dir, exist_ok=True)

    for skill in skills:
        # Prefix with inteskill- to distinguish from human skills
        dst = task_skills_dir / f"inteskill-{skill['name']}"
        shutil.copytree(skill["path"], dst, dirs_exist_ok=True)

    return True


def snapshot_skills(skills: list[dict], snapshot_dir: str):
    """Save a snapshot of the current skill bank."""
    os.makedirs(snapshot_dir, exist_ok=True)

    for skill in skills:
        dst = os.path.join(snapshot_dir, skill["name"])
        shutil.copytree(skill["path"], dst, dirs_exist_ok=True)

    # Write metadata
    meta = {
        "timestamp": datetime.now().isoformat(),
        "count": len(skills),
        "skills": [
            {"name": s["name"], "has_scripts": s["has_scripts"]}
            for s in skills
        ],
    }
    with open(os.path.join(snapshot_dir, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"Snapshot saved: {snapshot_dir} ({len(skills)} skills)")


def main():
    parser = argparse.ArgumentParser(description="Export inteSkill skills to SkillsBench format")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--skills-dir", default=None, help="Override skills directory")
    parser.add_argument("--tasks", nargs="+", help="Specific task IDs")
    parser.add_argument("--snapshot", help="Save skill snapshot to this directory")
    parser.add_argument("--list", action="store_true", help="Just list available skills")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    cfg = load_config(args.config)

    # Default skills directory
    skills_dir = args.skills_dir or os.path.expanduser("~/.openclaw/memory/skills")
    skills = find_skills(skills_dir)

    if args.list:
        print(f"Skills directory: {skills_dir}")
        print(f"Found {len(skills)} skills:\n")
        for s in skills:
            scripts = " [has scripts]" if s["has_scripts"] else ""
            print(f"  {s['name']}{scripts}")
        return

    if not skills:
        print(f"No skills found in {skills_dir}")
        return

    print(f"Found {len(skills)} skills in {skills_dir}")

    # Snapshot
    if args.snapshot:
        snapshot_skills(skills, args.snapshot)

    # Export to tasks
    sb_root = Path(cfg["skillsbench"]["root"]).resolve()

    if args.tasks:
        task_ids = args.tasks
    else:
        tasks_dir = sb_root / "tasks"
        task_ids = sorted(
            d.name for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "task.toml").exists()
        )

    exported = 0
    for task_id in task_ids:
        if export_to_task(task_id, skills, sb_root):
            exported += 1

    print(f"Exported {len(skills)} skills to {exported} tasks")


if __name__ == "__main__":
    main()
