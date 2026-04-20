# OpenClaw × SkillsBench 自动化执行规格说明

## 1. 目标

让 OpenClaw 作为 agent 执行 SkillsBench 的 87 个任务，同时 inteSkill 插件自动提取 skill。
最终对比四组实验结果：无 skill / 人工 skill / inteSkill 单次提取 / inteSkill 迭代 N 次。

## 2. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                      自动化脚本 (run_tasks.py)                │
│                                                              │
│  1. 读取 task 列表                                           │
│  2. 准备 workspace                                           │
│  3. 通过 WebSocket 发指令给 OpenClaw                          │
│  4. 收集执行结果                                             │
│  5. 调用 Harbor 验证                                         │
└──────┬────────────────────┬──────────────────────┬───────────┘
       │                    │                      │
       ▼                    ▼                      ▼
┌──────────────┐  ┌──────────────────┐  ┌────────────────────┐
│   OpenClaw   │  │  inteSkill 插件   │  │  Harbor + Docker   │
│   Gateway    │  │  (自动触发)        │  │  (验证环境)         │
│              │  │                    │  │                    │
│  ws://       │  │  before_prompt:    │  │  docker compose    │
│  localhost:  │  │    inject skills   │  │  up → test.sh →   │
│  18789       │  │  agent_end:        │  │  pytest → reward   │
│              │  │    extract skills  │  │                    │
└──────────────┘  └──────────────────┘  └────────────────────┘
```

## 3. 核心问题与解决方案

### 3.1 环境隔离问题

**问题**：SkillsBench 任务在 Docker 容器内执行（特定依赖、数据文件），OpenClaw 在宿主机运行。

**方案**：两阶段分离 —— OpenClaw 做任务，Docker 做验证。

```
Phase 1 — 执行 + 提取（OpenClaw 本地）
  · 将 task 的 environment/ 数据复制到 OpenClaw workspace
  · 通过 WebSocket 发 instruction.md 内容
  · OpenClaw agent 在本地 workspace 工作
  · inteSkill 自动触发 skill 提取（agent_end → skill_ingest）

Phase 2 — 验证（Harbor + Docker 标准流程）
  · 导出 skill bank 中的 SKILL.md 到 tasks/ 的 skills 目录
  · harbor run -a terminus-2 跑带 skill 的任务
  · test.sh + pytest 自动验证 → reward（0/1）
  · 收集 pytest 报错信息（用于 Phase 3 反馈）

Phase 3 — 迭代演进（仅 Group D）
  · 解析 pytest 报错：哪些 test case 失败、期望值 vs 实际值
  · 将报错作为 ground_truth_diff 反馈喂给 inteSkill Evolve
  · skill 更新 → 回到 Phase 2 重新验证
  · 重复 N 轮，记录每轮通过率
```

### 3.2 路径映射

SkillsBench instruction.md 中的路径（如 `/root/sc100-blank.pdf`、`/app/problem.json`）
需要映射到 OpenClaw workspace 的本地路径。

**方案**：在发送 instruction 前，自动替换路径前缀。

```python
# 路径映射规则
CONTAINER_ROOTS = ["/root", "/app", "/workspace"]
LOCAL_WORKSPACE = "/Users/xitongliang/.openclaw/workspace/benchmark/{task_id}"

def rewrite_paths(instruction: str, task_id: str) -> str:
    for root in CONTAINER_ROOTS:
        instruction = instruction.replace(root, f"{LOCAL_WORKSPACE}")
    return instruction
```

### 3.3 依赖问题

部分任务需要容器内的特殊依赖（如 JAX、PyTorch、ffmpeg）。

**方案**：
- **代码生成类任务**（多数）：OpenClaw 生成代码/文件 → 在 Docker 容器内执行和验证
- **需要交互的任务**：跳过，或在 Docker 内安装 claude CLI 让 OpenClaw 远程操控

**优先跑**不依赖特殊环境的任务（Excel、PDF、文本处理、代码编写类）。

### 3.4 inteSkill 触发

OpenClaw 的 inteSkill 插件已配置：
- `before_prompt_build` → `skill_search`（注入已有 skill）
- `agent_end` → `skill_ingest`（从对话中提取 skill）

**无需额外代码**，只要通过 WebSocket 发消息，两个 hook 自动触发。

## 4. WebSocket 通信协议

基于 `chatting_agent_memory/chat.py` 中的 `OpenClawClient`。

### 4.1 连接参数

```python
WS_URL = "ws://localhost:18789"
TOKEN = "67ec60c41f14a24abf3dfead93b2bc94fbb3ee256eb9d246"  # from ~/.openclaw/openclaw.json
```

### 4.2 消息流

```
Client                          OpenClaw Gateway
  │                                    │
  ├── connect (auth + scopes) ──────►  │
  │  ◄── res {ok: true} ──────────────┤
  │                                    │
  ├── chat.send (message) ──────────►  │
  │  ◄── res {ok: true} ──────────────┤
  │                                    │
  │  ◄── event: agent {delta} ────────┤  (streaming text)
  │  ◄── event: agent {delta} ────────┤
  │  ◄── ...                           │
  │  ◄── event: chat {state: final} ──┤  (turn complete)
  │                                    │
  ├── disconnect ──────────────────►   │
```

### 4.3 Session Key

每个任务用独立 session，避免上下文串扰：

```python
session_key = f"benchmark:{task_id}:{run_id}"
```

## 5. 执行流程详细设计

### 5.1 单任务执行流程

```python
async def run_single_task(task_id: str, config: Config) -> TaskResult:
    # 1. 准备 workspace
    workspace = prepare_workspace(task_id)
    #    - 创建目录
    #    - 复制 environment/ 下的数据文件到 workspace
    #    - 不复制 Dockerfile、tests/、solution/

    # 2. 读取并改写 instruction
    instruction = read_instruction(task_id)
    instruction = rewrite_paths(instruction, task_id)
    #    - 替换容器路径为本地路径
    #    - 可选：追加 "请将所有输出文件保存到 {workspace}/" 提示

    # 3. 发送给 OpenClaw
    client = OpenClawClient(ws_url=WS_URL, token=TOKEN)
    full_response = ""
    for chunk in client.chat_stream(
        user_id=f"benchmark-{task_id}",
        message=instruction
    ):
        full_response += chunk

    # 4. 记录执行信息
    log = {
        "task_id": task_id,
        "instruction_length": len(instruction),
        "response_length": len(full_response),
        "timestamp": datetime.now().isoformat(),
    }

    # 5. 验证（Docker）
    reward = run_verification(task_id, workspace)

    return TaskResult(task_id=task_id, reward=reward, log=log)
```

### 5.2 验证流程

验证走 Harbor 标准流程，不需要自己管 Docker。

```python
def run_verification(task_id: str, skill_dir: str | None = None) -> VerifyResult:
    """
    用 harbor run 验证，skill 通过文件系统注入。

    skill_dir: 导出的 skill 目录，None 则跑无 skill 版本
    """
    # 1. 选择 task 路径
    if skill_dir:
        # 将 skill 复制到 tasks/{task_id}/environment/skills/
        task_path = f"skillsbench/tasks/{task_id}"
        inject_skills(skill_dir, f"{task_path}/environment/skills/")
    else:
        task_path = f"skillsbench/tasks-no-skills/{task_id}"

    # 2. harbor run 标准验证
    result = subprocess.run([
        "harbor", "run",
        "-p", task_path,
        "-a", "terminus-2",
        "-m", model_name,
    ], capture_output=True, text=True, env=env)

    # 3. 解析结果
    job_dir = find_latest_job(task_id)
    reward = read_reward(job_dir)

    # 4. 收集 pytest 报错（用于迭代反馈）
    pytest_output = read_file(f"{job_dir}/verifier/test-stdout.txt")
    failures = parse_pytest_failures(pytest_output)

    return VerifyResult(
        task_id=task_id,
        reward=reward,
        failures=failures,        # 失败的 test case 列表
        pytest_output=pytest_output,  # 原始 pytest 输出
    )
```

### 5.3 迭代演进流程

```python
def run_iteration(task_id: str, failures: list[TestFailure]) -> None:
    """
    将 pytest 报错作为反馈喂给 inteSkill，触发 skill Evolve。

    pytest 报错天然就是 ground truth diff：
      FAILED test_amount - assert 1500 == 1200
      FAILED test_date_format - "2026-01-19" != "January 19, 2026"
    """
    # 1. 构造反馈消息
    feedback = construct_feedback(task_id, failures)
    # 格式：
    # "任务 {task_id} 执行后验证失败。以下是期望与实际的差异：
    #  - test_amount: 期望 1500, 实际 1200
    #  - test_date_format: 期望 '2026-01-19', 实际 'January 19, 2026'
    #  请根据这些反馈改进相关 skill。"

    # 2. 发送给 OpenClaw（触发 inteSkill Evaluate → Evolve）
    client = OpenClawClient(ws_url=WS_URL, token=TOKEN)
    for chunk in client.chat_stream(
        user_id=f"benchmark-evolve-{task_id}",
        message=feedback
    ):
        pass  # 只需触发，不需要回复内容

    # 3. 或直接调用 MCP：skill_ingest with ground_truth_diff
    # client.call_tool("skill_ingest", {
    #     "messagesJson": ...,
    #     "taskDescription": ...,
    #     "success": False,
    #     "ground_truth_diff": feedback,
    # })
```

### 5.3 批量执行流程

```python
async def run_batch(task_ids: list[str], config: Config):
    results = []
    for task_id in task_ids:
        print(f"[{len(results)+1}/{len(task_ids)}] {task_id}")

        # 执行
        result = await run_single_task(task_id, config)
        results.append(result)

        # 写入增量日志（断点续跑）
        append_jsonl("execution_log.jsonl", result)

        # 间隔（避免 OpenClaw 过载）
        await asyncio.sleep(config.interval_sec)

    return results
```

## 6. 四组实验设计

### Group A: 无 skill（baseline）

```yaml
setup:
  - 清空 skill bank（或用空白 skill 目录）
  - 禁用 inteSkill 插件
execute:
  - 对每个任务：发 instruction → OpenClaw 执行 → 验证
output:
  - results_no_skill.jsonl
```

### Group B: 人工 skill

```yaml
setup:
  - 将 tasks/（有 skill 版本）的 skills/ 导入 inteSkill skill bank
  - 或直接用 harbor run -p tasks/{task_id} 跑带 skill 的版本
execute:
  - 对每个任务：skill 自动注入 → OpenClaw 执行 → 验证
output:
  - results_human_skill.jsonl
```

### Group C: inteSkill 单次提取

```yaml
setup:
  - 清空 skill bank
  - 启用 inteSkill 插件
execute:
  - Phase 1: 对每个任务执行一次（inteSkill 自动提取 skill）
  - Phase 2: 清空执行结果，用提取到的 skill 重新跑一遍 → 验证
output:
  - results_inteskill_v1.jsonl
  - extracted_skills/（导出的 skill 快照）
```

### Group D: inteSkill 迭代 N 次

```yaml
setup:
  - 从 Group C 的 skill bank 开始

execute:
  for round in 1..N:
    # 1. 验证：用当前 skill 跑全部任务
    - 导出 skill bank → SKILL.md 文件
    - harbor run -a terminus-2（带 skill）→ 收集每个任务的 reward + pytest 报错

    # 2. 反馈：将 pytest 报错喂给 inteSkill
    - for each failed_task:
        - 解析 pytest 输出，提取 test case 级别的 diff：
          · 哪个 test 失败
          · 期望值（expected）
          · 实际值（actual）
          · 错误类型（AssertionError / FileNotFoundError / ...）
        - 构造 ground_truth_diff 反馈消息
        - 发送给 OpenClaw → inteSkill Evaluate → Evolve skill

    # 3. 记录
    - 保存本轮成功率、skill bank 快照、演进 diff

output:
  - results_inteskill_iter_{n}.jsonl（每轮逐任务结果）
  - iteration_curve.json（收敛曲线：round → pass_rate）
  - skill_snapshots/round_{n}/（每轮 skill bank 快照）
  - evolve_log.jsonl（每次 Evolve 的输入反馈 + 产出 diff）
```

#### 迭代闭环图

```
              ┌─────────────────────────────────────────┐
              │                                         │
              ▼                                         │
  ┌─────────────────┐                                   │
  │  导出 skill bank │                                   │
  │  → SKILL.md 文件 │                                   │
  └────────┬────────┘                                   │
           │                                            │
           ▼                                            │
  ┌─────────────────┐     ┌────────────────┐            │
  │  harbor run      │────►│  pytest 验证    │            │
  │  (带 skill)      │     │  reward + 报错  │            │
  └─────────────────┘     └───────┬────────┘            │
                                  │                     │
                    ┌─────────────┼─────────────┐       │
                    │ pass        │ fail         │       │
                    ▼             ▼              │       │
               记录成功     解析 pytest 报错      │       │
                           │                    │       │
                           ▼                    │       │
                    ┌──────────────┐             │       │
                    │ 构造反馈      │             │       │
                    │ expected vs  │             │       │
                    │ actual diff  │             │       │
                    └──────┬───────┘             │       │
                           │                    │       │
                           ▼                    │       │
                    ┌──────────────┐             │       │
                    │ inteSkill    │             │       │
                    │ Evaluate →   │─────────────┘       │
                    │ Evolve skill │                     │
                    └──────┬───────┘                     │
                           │ skill 更新                   │
                           └─────────────────────────────┘
                                  下一轮
```

#### pytest 报错解析示例

```
# pytest 原始输出
FAILED tests/test_outputs.py::test_plaintiff_name
  AssertionError: assert "Joyce He" == "JOYCE HE"

FAILED tests/test_outputs.py::test_claim_amount
  AssertionError: assert 1500.0 == 1200.0

PASSED tests/test_outputs.py::test_file_exists
PASSED tests/test_outputs.py::test_date_format

# 解析为结构化反馈
{
  "task_id": "court-form-filling",
  "round": 2,
  "total_tests": 4,
  "passed": 2,
  "failed": 2,
  "failures": [
    {
      "test": "test_plaintiff_name",
      "expected": "JOYCE HE",
      "actual": "Joyce He",
      "error_type": "AssertionError",
      "hint": "名字需要全大写"
    },
    {
      "test": "test_claim_amount",
      "expected": 1500.0,
      "actual": 1200.0,
      "error_type": "AssertionError",
      "hint": "金额取自 security deposit, 不是 rent"
    }
  ]
}
```

## 7. 配置文件设计

```yaml
# config.yaml

openclaw:
  ws_url: "ws://localhost:18789"
  token: ""  # 从 ~/.openclaw/openclaw.json 自动读取

skillsbench:
  root: "../skillsbench"
  task_set: "tasks-no-skills"  # tasks / tasks-no-skills

execution:
  workspace_base: "/tmp/skill-benchmark-workspace"
  interval_sec: 5         # 任务间隔
  timeout_sec: 600        # 单任务超时
  max_retries: 1          # 失败重试次数

verification:
  method: "docker"         # docker / harbor
  docker_path: "/Applications/Docker.app/Contents/Resources/bin/docker"

tasks:
  filter:
    difficulty: null       # easy / medium / hard / null=all
    category: null         # 按类别过滤
    include: []            # 指定任务 ID 列表
    exclude:               # 排除的任务
      - video-tutorial-indexer    # 需要 20GB 存储
      - mhc-layer-impl           # 需要 GPU
      - speaker-diarization-subtitles  # 音频模型

experiment:
  group: "C"               # A / B / C / D
  iterations: 3            # Group D 的迭代次数
```

## 8. 输出文件结构

```
openclaw-scripts/
├── docs/
│   └── spec.md                  # 本文档
├── tasks/
│   └── task-catalog.md          # 任务目录
├── run_tasks.py                 # 批量执行入口
├── openclaw_client.py           # WebSocket 客户端（从 chat.py 抽取）
├── verification.py              # Docker 验证逻辑
├── export_skills.py             # Skill bank 导出
├── analyze.py                   # 结果分析与对比
├── config.yaml                  # 配置
└── results/                     # 运行结果（gitignored）
    ├── execution_log.jsonl      # 逐任务执行日志
    ├── results_no_skill.jsonl
    ├── results_human_skill.jsonl
    ├── results_inteskill_v1.jsonl
    ├── results_inteskill_iter_N.jsonl
    └── skill_snapshots/         # 每轮 skill bank 快照
```

## 9. 依赖

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | >= 3.12 | 脚本运行 |
| websocket-client | latest | WebSocket 通信 |
| Docker + Compose | v29+ / v5+ | 验证环境 |
| Harbor | latest | 可选，简化验证流程 |
| OpenClaw | running | 本地 agent 执行 |
| gspd_mcp_server | compiled | inteSkill 后端 |

## 10. 分步实施计划

### Step 1: 最小可行脚本

- 抽取 `OpenClawClient` 为独立模块
- 写 `run_single_task()`：读 instruction → 发 OpenClaw → 收集结果
- 手动验证 1 个 easy 任务（dialogue-parser）
- **验收标准**：OpenClaw 能完成任务 + inteSkill 能提取 skill

### Step 2: Docker 验证对接

- 写 `run_verification()`：构建容器 → 注入产出 → test.sh → reward
- 端到端跑通 1 个任务：OpenClaw 执行 → Docker 验证 → 得到 reward
- **验收标准**：reward 与 harbor run 结果一致

### Step 3: 批量执行

- 写 `run_batch()`：循环 + 断点续跑 + JSONL 日志
- config.yaml 支持过滤任务
- 先跑 7 个 easy 任务
- **验收标准**：7 个 easy 全部跑完，日志完整

### Step 4: 四组实验

- Group A：禁用 inteSkill，跑 baseline
- Group C：启用 inteSkill，跑单次提取
- Group D：构造反馈 + 迭代
- Group B：导入人工 skill
- **验收标准**：四组结果可对比

### Step 5: 分析与可视化

- 四组对比表
- 按领域/难度分项
- 迭代收敛曲线

## 11. 风险与局限

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 路径映射不完整 | 部分任务找不到文件 | 逐任务测试，维护映射规则表 |
| 本地缺少依赖 | 代码能生成但无法本地执行 | 只验证最终产出，执行在 Docker |
| OpenClaw agent 超时 | 任务中断 | 配置 timeout，跳过超时任务 |
| Skill 提取质量波动 | 实验结果不稳定 | 多次运行取平均 |
| 任务需要联网 | 本地无法完成 | 排除需要外部 API 的任务 |
| OpenClaw session 上下文泄漏 | 任务间互相影响 | 每个任务用独立 session key |
