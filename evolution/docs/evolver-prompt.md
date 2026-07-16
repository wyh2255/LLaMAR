# Evolver 分析 Prompt 详解

evolver.py 的核心是 `build_prompt()` 函数，它构造一个**自包含的分析 prompt** 发给 Hermes。Hermes 收到后自己读取实验数据文件，进行分析，然后输出改进后的 prompt。

## 完整 Prompt 模板

以下是根据 `evolver.py` 第 36-117 行提取的完整 prompt：

```
You are an AI agent evolution system. Your task is to analyze SAR (Search & Rescue)
multi-agent experiment results and generate improved system prompts.

## Data

The experiment results are at: {results_dir}
The current system prompts are at: {prompt_dir}

### Files to read:
- {results_dir}/trajectory.csv — per-step metrics
- {results_dir}/events.ndjson — full event log
- {results_dir}/summary.tsv (or summary.csv) — aggregate metrics
- {results_dir}/metadata.json — experiment config
- {results_dir}/subtasks.csv — subtask completion timeline
- {results_dir}/agent_interactions.csv — per-agent tool calls
- {results_dir}/router_interactions.csv — coordinator dispatch history
- {results_dir}/token_usage.csv — token usage per LLM call
- {prompt_dir}/coordinator/system.md — CURRENT coordinator prompt
- {prompt_dir}/worker/system.md — CURRENT worker prompt

## What to analyze

1. **Failure patterns**: What went wrong? Look at coverage/transport rates,
   timeout agents, event errors, tool call failures, end_reason.
2. **Prompt weaknesses**: Where did the current prompts fail? Look for:
   - Repeated actions → missing state memory guidance
   - Invalid tool calls → weak tool schema instructions
   - Premature NoOp/Finish → missing completion conditions
   - Idle agents → poor dispatch strategy guidance
   - Step budget mismanagement → missing time awareness
3. **Improvement opportunities**: What specific prompt changes would address
   these patterns?

## Output format — YOU MUST FOLLOW THIS EXACTLY

First, read ALL the data files listed above. Then respond with:

Analysis Summary
- Top failure pattern: ...
- Root cause: ...
- Specific metrics: ...

Improved Prompt for {Coordinator|Worker|Coordinator and Worker}

[Write the COMPLETE improved system prompt here — full replacement,
not amendments. Output it in your response text, NOT to a file.]

Changes Summary
- Change 1: [what changed and why]
- Change 2: [what changed and why]

## Evolution Guidelines

- Preserve the existing prompt structure and tone
- Only add constraints that address observed failures
- Keep prompts concise — don't add fluff
- Add specific, actionable rules (not vague suggestions)
- Reference specific experiment observations to justify each change

## Anti-Cheating Rules — READ CAREFULLY

You are generating a **general-purpose** system prompt that must work across
ALL scenes (1-5), agent counts (1-6), seeds, and environment configurations
— not just the experiment you analyzed.

**STRICTLY FORBIDDEN:**
- Never hardcode entity names like 'ReservoirUtah', 'CaldorFire', 'GreatFire'
  — use generic placeholders like '{{ReservoirName}}'
- Never hardcode agent names ('Alice should do X')
- Never hardcode grid positions, coordinates, or scene-specific numbers
- Never embed experiment-specific step counts, coverage values, or metric
  thresholds as constants
- Never reference a specific scene number ('In Scene 1, ...')

**ALLOWED:**
- General reasoning patterns ('Before using a tool, verify your location')
- General anti-patterns ('Do not dispatch scouting-only tasks')
- General strategy rules ('Always dispatch the longest useful chain')
- Structural improvements (adding missing sections, clarifying rules)

**Rule of thumb**: If the improvement works on a completely different scene
with different entity names — it's good. If it relies on specifics from the
one trajectory you observed — it's cheating.
```

## Prompt 各部分说明

### 1. Data 段
告诉 Hermes **输入文件在哪**，列出所有需要读取的实验数据和当前 prompt 文件。evolver.py 不自己读这些文件——Hermes 用 `read_file` 工具自己读。

### 2. What to analyze
定义分析维度：
- **Failure patterns**：从顶层指标（coverage, transport_rate, end_reason）找失败模式
- **Prompt weaknesses**：从具体行为（重复动作、无效 tool call、过早终止）反推 prompt 缺陷
- **Improvement opportunities**：把观察到的问题映射到具体的 prompt 修改

### 3. Output format
输出解析的三个 section：
| Section | 正则匹配 | 写入文件 |
|---------|----------|---------|
| `Analysis Summary` | `re.search(r'(?:#{0,3}\s*)?Analysis Summary\s*\n(...)` | `generation_summary.json` |
| `Improved Prompt for {Coordinator/Worker}` | `re.search(r'...#{0,3}\s*)?Improved Prompt for...')` | `coordinator/system.md` 或 `worker/system.md` |
| `Changes Summary` | `re.search(r'(?:#{0,3}\s*)?Changes Summary\s*\n(...)$')` | `generation_summary.json` |

解析器特性：
- 支持 `##` 和 `###` 两种 header 格式
- 自动剥离 markdown 代码 fences（```）
- prompt 提取在遇到下一个 section header 时停止

### 4. Anti-Cheating Rules
这是**最关键的设计**——防止模型"作弊"：把实验中的特例 hardcode 成 prompt 规则。
- **为什么重要**：如果 prompt 写了"先救 ReservoirUtah"，它在别的场景里就没用
- **如何实现**：把 anti-cheating 规则直接嵌在分析 prompt 里，让模型自己约束自己
- **校验标准**：改进后的 prompt 能在完全不同的场景（不同实体名、不同坐标）下同样有效

## Hermes 调用方式

```python
def call_hermes(prompt: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["hermes", "chat", "-q", prompt, "--quiet"],
        capture_output=True, text=True, timeout=timeout,
    )
    # 过滤掉 session_id: 和 Warning: 行
    lines = result.stdout.split("\n")
    content_lines = [
        line for line in lines
        if line.strip()
        and not line.startswith("session_id:")
        and not line.startswith("Warning:")
        and not line.startswith("Query:")
    ]
    return "\n".join(content_lines)
```

关键点：
- `-q` 表示 query 模式，节省 token 开销
- `--quiet` 减少输出噪音
- 输出过滤：去掉 `session_id:`、`Warning:`、`Query:` 前缀行，只保留回复正文

## 响应解析器

`parse_response()` 使用三个正则提取结构化信息：

```python
# Analysis Summary
re.search(
    r"(?:#{0,3}\s*)?Analysis Summary\s*\n([\s\S]*?)"
    r"(?=\n(?:#{0,3}\s*)?(?:Improved Prompt|Changes Summary))",
    response,
)

# Improved Prompt for {target}
re.search(
    r"(?:#{0,3}\s*)?Improved Prompt for " + header + r"\s*\n+"
    r"([\s\S]*?)(?=\n(?:#{0,3}\s*)?(?:Changes Summary|Improved Prompt for))",
    response,
)

# Changes Summary
re.search(
    r"(?:#{0,3}\s*)?Changes Summary\s*\n([\s\S]*?)$",
    response,
)
```

## 如何修改分析 Prompt

如果想调整分析 prompt 的内容，编辑 `evolver.py` 中的 `build_prompt()` 函数（第 36-117 行）。典型的修改场景：

1. **增加新的分析维度**：在 `What to analyze` 段新增 bullet
2. **调整 Anti-Cheating 规则**：增加禁止（Forbidden）或允许（Allowed）的项目
3. **修改输出格式**：调整 section header 名称后，需要同步更新 `parse_response()` 的正则
4. **增加新的输入文件**：在 `Files to read` 列表里新增一行
