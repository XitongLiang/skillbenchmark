# OpenClaw × SkillsBench Benchmark Scripts

通过 OpenClaw 执行 SkillsBench 任务，验证 inteSkill 自演进 skill 系统的效果。

## 前置条件

- Python >= 3.12
- Docker Desktop 已启动
- `pip3 install pyyaml websockets`
- `pip3 install "harbor @ git+https://github.com/laude-institute/harbor.git"`
- OpenClaw 运行中（仅 Phase 1 execute 需要）

## 快速开始

```bash
cd skill-benchmark/openclaw-scripts

# 1. 查看有哪些任务
python3 run_tasks.py --difficulty easy --phase verify --tasks NONE 2>&1 | head -5
# 或直接看 tasks/task-catalog.md

# 2. 跑一个任务试试（只验证，不需要 OpenClaw）
python3 run_tasks.py --tasks dialogue-parser --phase verify

# 3. 跑所有 easy 任务
python3 run_tasks.py --difficulty easy --phase verify
```

## 脚本说明

### run_tasks.py — 批量执行 + 验证

```bash
# 只验证（Harbor agent 做任务 + pytest 验证）
python3 run_tasks.py --phase verify

# 只执行（OpenClaw 做任务 + inteSkill 提取 skill）
python3 run_tasks.py --phase execute

# 两个都做
python3 run_tasks.py --phase both

# 指定任务
python3 run_tasks.py --tasks dialogue-parser jax-computing-basics

# 按难度过滤
python3 run_tasks.py --difficulty easy
python3 run_tasks.py --difficulty medium

# 断点续跑（跳过已完成的任务）
python3 run_tasks.py --resume

# 自定义日志路径
python3 run_tasks.py --log results/results_no_skill.jsonl
```

### export_skills.py — 导出 inteSkill 提取的 skill

```bash
# 查看当前有哪些 skill
python3 export_skills.py --list

# 指定 inteSkill 的 skill 目录
python3 export_skills.py --skills-dir ~/.openclaw/memory/skills --list

# 导出到所有任务
python3 export_skills.py

# 导出到特定任务
python3 export_skills.py --tasks dialogue-parser jax-computing-basics

# 保存 skill 快照（用于对比不同版本）
python3 export_skills.py --snapshot results/skill_snapshots/v1
```

### iterate.py — 迭代演进

```bash
# 跑 3 轮迭代（验证 → 反馈 → evolve → 再验证）
python3 iterate.py --rounds 3

# 只对 easy 任务迭代
python3 iterate.py --rounds 3 --tasks dialogue-parser offer-letter-generator

# 指定结果目录
python3 iterate.py --rounds 5 --results-dir results/experiment_d
```

### analyze.py — 结果分析

```bash
# 分析所有已有结果
python3 analyze.py

# 对比指定组
python3 analyze.py --groups A C D

# 按难度细分
python3 analyze.py --by-difficulty

# 按领域细分
python3 analyze.py --by-category

# 查看迭代收敛曲线
python3 analyze.py --iteration-curve results/iteration_curve.json
```

## 四组实验操作手册

### Group A: 无 skill（baseline）

```bash
# 禁用 inteSkill，用 harbor 跑无 skill 版本
python3 run_tasks.py --phase verify --log results/results_no_skill.jsonl
```

### Group B: 人工 skill

编辑 config.yaml，将 `task_set` 改为 `tasks`（带 skill 的版本）：

```yaml
skillsbench:
  task_set: "tasks"   # 改这里
```

```bash
python3 run_tasks.py --phase verify --log results/results_human_skill.jsonl
```

### Group C: inteSkill 单次提取

```bash
# Step 1: OpenClaw 执行任务（inteSkill 自动提取 skill）
python3 run_tasks.py --phase execute

# Step 2: 导出提取到的 skill + 快照
python3 export_skills.py --snapshot results/skill_snapshots/v1

# Step 3: 用提取的 skill 验证
python3 run_tasks.py --phase verify --log results/results_inteskill_v1.jsonl
```

### Group D: inteSkill 迭代 N 次

```bash
# 从 Group C 的 skill 开始，迭代 3 轮
python3 iterate.py --rounds 3 --results-dir results
```

### 对比分析

```bash
python3 analyze.py --groups A B C D --by-difficulty
```

## 配置文件 config.yaml

```yaml
openclaw:
  ws_url: "ws://localhost:18789"   # OpenClaw Gateway 地址
  recv_timeout: 600                # 单任务超时（秒）

skillsbench:
  root: "../skillsbench"           # SkillsBench 代码路径
  task_set: "tasks-no-skills"      # tasks（有skill）/ tasks-no-skills（无skill）

verification:
  model: "fireworks_ai/accounts/fireworks/models/glm-5"  # Harbor agent 用的 LLM
  harbor_env:
    FIREWORKS_API_KEY: "your-key"
    PATH: "..."                    # 需要包含 docker 和 harbor 的路径

tasks:
  filter:
    difficulty: null               # easy / medium / hard / null=all
    include: []                    # 指定任务 ID
    exclude:                       # 排除的任务
      - video-tutorial-indexer     # 需要 20GB
      - mhc-layer-impl            # 需要 GPU
```

## 输出文件

```
results/
├── execution_log.jsonl          # 逐任务执行日志
├── results_no_skill.jsonl       # Group A 结果
├── results_human_skill.jsonl    # Group B 结果
├── results_inteskill_v1.jsonl   # Group C 结果
├── results_inteskill_iter_N.jsonl  # Group D 每轮结果
├── iteration_curve.json         # 迭代收敛数据
├── evolve_log.jsonl             # Evolve 反馈日志
└── skill_snapshots/             # Skill bank 快照
    ├── v1/
    ├── round_1/
    ├── round_2/
    └── round_3/
```

## 环境变量

| 变量 | 用途 |
|---|---|
| `FIREWORKS_API_KEY` | Fireworks API key（Harbor agent 用） |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw 网关 token（默认自动读取） |
| `ANTHROPIC_API_KEY` | 如果 Harbor agent 用 Claude |
| `OPENAI_API_KEY` | 如果 Harbor agent 用 OpenAI |
