# Skill Benchmark 评测系统设计文档

## 1. 概述

### 1.1 目标

评测 AI agent 记忆系统的 skill 提取与复用效果。记忆系统通过学习 workflow 提取出 skill，本评测系统衡量：

- **质量分数**：skill 复用后任务完成质量是否提升
- **Token 消耗**：skill 复用后 token 用量是否减少
- **Skill 复用率**：提取的 skill 在后续任务中被注入使用的比例
- **任务完成率**：任务是否正确完成

### 1.2 评测方法

采用 **三阶段离线回放对比**：

- **Phase 0（纯净 baseline）**：关闭记忆系统，既不学习也不注入。作为无记忆系统的纯净 baseline。
- **Phase 1（冷启动）**：skill 库从空开始，开启学习和注入。随着任务执行，前期 task 学到的 skill 会实时注入给后续 task。体现记忆系统从零冷启动的增量收益。
- **Phase 2（热重跑）**：带着 Phase 1 积累的完整 skill 库，重新执行同样的 task，同样开启学习和注入。此阶段所有 task 从一开始就能享受完整 skill 库的加成。

三阶段对比维度：
- Phase 0 vs Phase 1：记忆系统冷启动的整体价值
- Phase 0 vs Phase 2：记忆系统在 skill 库成熟后的完整价值
- Phase 1 vs Phase 2：skill 库成熟度对效果的影响（冷启动 vs 热重跑）

### 1.3 数据源

使用 [OpenAI GDPval 数据集](https://huggingface.co/datasets/openai/gdpval)：

- 220 个真实职业任务，覆盖 44 个职业、9 个行业
- 每个任务包含：prompt、参考文件、交付物描述、结构化评分 rubric
- 用户可在前端自由选择评测子集

### 1.4 技术栈

- **语言**：Python
- **前端**：Gradio
- **存储**：SQLite
- **Agent 调度**：OpenClaw WebSocket Gateway
- **评分**：LLM Judge（模型可配置）
- **跨平台**：Windows / macOS / Linux

---

## 2. 系统架构

### 2.1 总体结构

```
skillbenchmark/
├── core/               # 评测引擎
│   ├── dataset.py          # GDPval 数据加载与管理
│   ├── runner.py           # OpenClaw 调度与交互
│   ├── judge.py            # LLM Judge 评分
│   └── metrics.py          # 指标计算与聚合
├── storage/            # 数据持久化
│   └── db.py               # SQLite 操作
├── plugin/             # OpenClaw 插件增强（TypeScript）
│   └── bench-hook.ts       # agent_end 时输出指标 JSON
├── ui/                 # Gradio 前端
│   └── app.py
├── config.yaml         # 全局配置
└── __main__.py         # 入口
```

### 2.2 数据流

```
┌─────────────────────────────────────────────────┐
│                 Gradio UI                        │
│  任务选择 │ 执行控制 │ 结果展示 │ 历史 │ Skill库  │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│              评测引擎 (core)                      │
│                                                   │
│  dataset.py ─→ runner.py ─→ judge.py ─→ metrics.py│
│  加载GDPval    调度OpenClaw   LLM评分    指标聚合  │
└───────┬──────────┬──────────┬───────────────────┘
        │          │          │
   HuggingFace  OpenClaw    LLM API
   datasets     Gateway     (可配置)
        │       (WebSocket)
        │          │
        │    ┌─────▼──────┐
        │    │ bench-hook  │  ← 插件增强，上报指标
        │    │ (TypeScript)│
        │    └─────┬──────┘
        │          │
        │          ▼
        │    指标 JSON 文件
        │
   ┌────▼─────────────────┐
   │   SQLite (storage)    │
   │  评测记录/历史/skill   │
   └──────────────────────┘
```

### 2.3 与 OpenClaw 的交互

评测系统通过 WebSocket 连接 OpenClaw Gateway（默认 `ws://127.0.0.1:18789`），实现双向交互。

#### 连接流程

1. 等待 `connect.challenge` 事件
2. 发送 `connect` 请求（携带 auth token、scopes 包含 `operator.admin`, `operator.read`, `operator.write`, `operator.approvals`）
3. 收到 `hello-ok` 响应，获取 session 信息

#### 任务执行交互

```
评测系统                      OpenClaw Gateway
   │  ── chat.send(task) ────► │
   │                            │ ── agent 执行中
   │  ◄── exec.approval ─────  │   需要权限审批？
   │  ── allow-once ─────────► │   自动批准
   │  ◄── chat delta ────────  │   流式输出
   │  ◄── chat final ────────  │   agent 回复完毕
   │                            │
   │  LLM判断: 是提问还是完成？  │
   │  如果是提问:                │
   │  ── chat.send(回复) ─────► │   评测系统 LLM 自动回复
   │  ...循环直到任务完成...      │
```

评测系统的 LLM 在此充当 **自动化用户** 角色：
- **工具权限审批**：收到 `exec.approval.requested` 事件时，自动回复 `allow-once`
- **Agent 提问**：收到 `chat` `final` 事件后，用自己的 LLM 分析 agent 回复内容。如果 agent 在提问或需要用户决策，生成合理回复通过 `chat.send` 发回
- **任务完成判断**：agent 回复 final 且不再提问 → 任务完成；或超时 → 强制结束
- **最大交互轮次**：默认 20 轮。超过后视为任务完成，防止 agent 和评测 LLM 无限对话

#### Session 重置

每个 task 执行前，通过 `sessions.reset` 方法重置 session，清除上下文，确保 task 之间互不影响。

#### Phase 切换与 Gateway 重启

各 Phase 通过 `autoLearn`/`autoInject` 配置和 skill 库初始状态区分。插件在启动时读取一次配置，无法热更新，因此切换 Phase 时需要重启 Gateway：

1. 调用 `config.patch` 修改 `autoLearn`/`autoInject`
2. Gateway 自动重启，WebSocket 连接断开
3. 评测系统轮询重连（每秒尝试一次 WebSocket connect）
4. 连接成功且收到 `hello-ok` → Gateway 就绪，继续发任务

| Phase   | autoLearn | autoInject | Skill 库初始状态 |
|---------|-----------|------------|-----------------|
| Phase 0 | false     | false      | 不使用           |
| Phase 1 | true      | true       | 空（从零积累）    |
| Phase 2 | true      | true       | Phase 1 积累的完整库 |

> 注意：Phase 1 和 Phase 2 配置相同，区别仅在于 skill 库的初始状态。Phase 切换时，Phase 0 → Phase 1 需要 `config.patch` 重启；Phase 1 → Phase 2 只需清理 session，skill 库保持不变即可，无需重启。

---

## 3. 数据模型

### 3.1 benchmark_runs — 评测运行记录

| 字段          | 类型     | 说明                              |
|--------------|----------|----------------------------------|
| id           | TEXT PK  | 唯一标识                          |
| name         | TEXT     | 如 "GDPval-50-Phase1"            |
| phase        | INT      | 0、1 或 2                        |
| status       | TEXT     | pending / running / completed / failed |
| config       | JSON     | LLM judge 模型、OpenClaw 配置等   |
| created_at   | DATETIME |                                  |
| completed_at | DATETIME |                                  |

### 3.2 task_results — 单个任务执行结果

| 字段             | 类型     | 说明                          |
|-----------------|----------|------------------------------|
| id              | TEXT PK  | 唯一标识                      |
| run_id          | TEXT FK  | 关联 benchmark_runs           |
| task_id         | TEXT     | GDPval task_id                |
| status          | TEXT     | pending / running / completed / failed |
| input_tokens    | INT      | 输入 token 数                 |
| output_tokens   | INT      | 输出 token 数                 |
| total_tokens    | INT      | 总 token 数                   |
| estimated_cost  | REAL     | 预估费用（USD）                |
| duration_ms     | INT      | 执行时长（毫秒）               |
| quality_score   | REAL     | rubric 实际得分                |
| max_score       | REAL     | rubric 满分                   |
| skills_injected | JSON     | 注入的 skill ID 列表           |
| deliverable_path| TEXT     | 交付物文件路径                  |
| interaction_log | JSON     | 与 OpenClaw 的完整对话记录      |
| created_at      | DATETIME |                              |

### 3.3 rubric_scores — 逐条评分详情

| 字段           | 类型     | 说明                      |
|---------------|----------|--------------------------|
| id            | TEXT PK  | 唯一标识                  |
| result_id     | TEXT FK  | 关联 task_results          |
| rubric_item_id| TEXT     | GDPval rubric 原始 ID      |
| criterion     | TEXT     | 评分标准描述               |
| max_score     | INT      | 该项满分                   |
| actual_score  | INT      | LLM judge 给的分           |
| judge_reason  | TEXT     | LLM judge 的判断理由       |

### 3.4 skill_snapshots — Skill 库快照

| 字段          | 类型     | 说明                      |
|--------------|----------|--------------------------|
| id           | TEXT PK  | 唯一标识                  |
| run_id       | TEXT FK  | 关联 benchmark_runs        |
| skill_id     | TEXT     | 原始 skill ID（sk_XXXX）  |
| name         | TEXT     | Skill 名称                |
| kind         | TEXT     | task-specific / generic   |
| content      | TEXT     | skill.md 完整内容          |
| inject_count | INT      | 被注入次数                 |
| success_count| INT      | 成功次数                   |
| created_at   | DATETIME |                          |

---

## 4. 核心指标

| 指标           | 计算方式                                              |
|---------------|------------------------------------------------------|
| **质量分数**    | `sum(actual_score) / sum(max_score)` per task         |
| **Token 节省率** | `1 - (Phase2 total_tokens / Phase1 total_tokens)`    |
| **Skill 复用率** | Phase 2 中有 skill 注入的 task 数 / 总 task 数        |
| **任务完成率**   | `status=completed` 的 task 数 / 总 task 数            |
| **质量变化**    | `Phase2 平均质量分数 - Phase1 平均质量分数`              |

---

## 5. 插件增强（bench-hook）

### 5.1 指标输出

在记忆插件的 `agent_end` hook 中，当检测到评测模式时，写出标准化指标文件：

```
~/.openclaw/benchmark/results/<run_id>/<task_id>.json
```

文件内容：

```json
{
  "task_id": "83d10b06-...",
  "session_key": "bench-task-001",
  "success": true,
  "duration_ms": 45200,
  "tokens": {
    "input": 12500,
    "output": 3200,
    "cache_read": 800,
    "cache_write": 1200
  },
  "skills": {
    "injected": ["sk_A1B2C3D4", "sk_E5F6G7H8"],
    "learned": ["sk_NEW12345"]
  },
  "deliverables": ["/path/to/output.xlsx"],
  "error": null
}
```

### 5.2 评测模式识别

通过 OpenClaw 配置字段控制（与 `autoLearn`/`autoInject` 一起在 `config.patch` 时写入，重启后生效）：

```json
{
  "memory": {
    "autoLearn": true,
    "autoInject": false,
    "benchmark": {
      "enabled": true,
      "runId": "run_xxx",
      "taskId": "83d10b06-..."
    }
  }
}
```

- `benchmark.enabled` 为 true 时，bench-hook 激活，写出指标文件
- `benchmark.runId` 和 `benchmark.taskId` 决定指标文件输出路径
- 每个 task 执行前，评测系统通过 `config.patch` 更新 `taskId`，触发 gateway 重启

> 注意：由于每个 task 前都需要更新 `taskId` 并重启，可以将 Phase 切换和 task 切换合并为同一次 `config.patch` 调用。

### 5.3 Token 数据采集

`agent_end` hook 不包含 token 数据。bench-hook 需要同时注册 `llm_output` hook，在每次 LLM 调用时累积 token 用量：

```typescript
// llm_output hook: 累积 token
let sessionTokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };

api.hooks.register("llm_output", (event) => {
  sessionTokens.input += event.usage.input;
  sessionTokens.output += event.usage.output;
  sessionTokens.cacheRead += event.usage.cacheRead;
  sessionTokens.cacheWrite += event.usage.cacheWrite;
});

// agent_end hook: 写出完整指标（包含累积的 token 数据）
api.hooks.register("agent_end", (event) => {
  writeMetrics({ ...event, tokens: sessionTokens });
  sessionTokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }; // reset
});
```

---

## 6. LLM Judge 评分

### 6.1 评分流程

对每个已完成的 task：

1. 读取 agent 交付物内容（Excel → 解析为文本/表格，PDF → 提取文本）
2. 读取 GDPval 的 `rubric_json`（结构化评分标准数组）
3. 逐条将 criterion + 交付物内容发给 LLM Judge
4. LLM 返回每条的 pass/fail + 理由
5. 累加得分，写入 `rubric_scores` 和 `task_results`

### 6.2 Judge Prompt 模板

```
你是一个评分助手。根据以下评分标准，判断交付物是否满足要求。

## 评分标准
{criterion}（满分 {max_score} 分）

## 交付物内容
{deliverable_content}

请回答：
1. 是否满足（yes/no）
2. 得分（0 到 {max_score}）
3. 判断理由（简要说明）

以 JSON 格式输出：
{"pass": true/false, "score": N, "reason": "..."}
```

### 6.3 模型配置

LLM Judge 模型通过 `config.yaml` 配置，支持切换：

```yaml
judge:
  provider: anthropic  # anthropic / openai
  model: claude-sonnet-4-6
  temperature: 0
  max_tokens: 500
```

---

## 7. Gradio 前端

### 7.1 Tab 布局

**Tab 1 — 任务管理**
- 从 GDPval 加载 220 个任务列表（表格：task_id, sector, occupation, prompt 预览）
- 按 sector / occupation 筛选
- 勾选要评测的任务子集
- 点击任务展开查看完整 prompt 和 rubric

**Tab 2 — 评测执行**
- 选择 Phase（0、1 或 2）
- 配置项：LLM judge 模型、并发数（1-3）、超时时间
- 启动 / 暂停 / 停止按钮
- 实时进度：当前执行第几个 task、当前 task 的对话流实时滚动
- 每个 task 完成后实时更新结果表格

**Tab 3 — 结果对比**
- Phase 1 vs Phase 2 汇总对比卡片（质量分数、token 消耗、skill 复用率、任务完成率）
- 逐 task 对比表格（可排序：质量提升最大的、token 节省最多的）
- 图表：各 sector 质量变化柱状图、token 消耗散点图

**Tab 4 — 历史记录**
- 所有评测 run 的列表（时间、phase、task 数、总分）
- 点击进入详情
- 选两次 run 做对比

**Tab 5 — Skill 库**
- 当前 skill 列表（从 skill_snapshots 读取）
- 每个 skill 详情：名称、触发条件、执行步骤、注入次数、成功率
- 按 kind 筛选（task-specific / generic）

---

## 8. 配置文件

```yaml
# config.yaml

openclaw:
  gateway_url: "ws://127.0.0.1:18789"
  auth_token: "your-token-here"
  timeout_seconds: 300
  max_concurrent: 1
  max_interaction_rounds: 20

judge:
  provider: anthropic
  model: claude-sonnet-4-6
  temperature: 0
  max_tokens: 500

dataset:
  source: "openai/gdpval"
  cache_dir: "./data/gdpval"

storage:
  db_path: "./data/benchmark.db"

benchmark:
  results_dir: "~/.openclaw/benchmark/results"
```

---

## 9. 跨平台注意事项

- 文件路径使用 `pathlib.Path` 处理，避免硬编码分隔符
- `~/.openclaw` 在 Windows 上为 `%USERPROFILE%/.openclaw`
- WebSocket 连接使用 `websockets` 库，跨平台兼容
- SQLite 为 Python 内置，无需额外安装
- 进程管理避免 Unix-only 信号，使用 `asyncio` 超时控制
