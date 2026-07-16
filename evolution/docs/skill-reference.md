# agent-evolution Skill 参考

Hermes 的 `agent-evolution` skill 位于 `~/.hermes/skills/software-development/agent-evolution/SKILL.md`（见 skill 列表）。本文档将其核心内容备份至此，并标注了与实际代码的差异。

> **注意**：skill 文档中仍包含旧版 `evolve.mjs`（Node.js + OpenCode SDK）的描述。实际代码已完全迁移到 `evolver.py`（Python + Hermes native）。以下以实际代码为准。

## Skill 元数据

```
name:        agent-evolution
description: Use OpenCode SDK to evolve multi-agent prompts based on
             experiment trajectories and metrics (OBSOLETE — see below)
category:    software-development
```

## 实际架构

当前架构（纯 Python，无需 OpenCode）：

```
Python evolver.py
  │  reads experiment CSV/NDJSON paths
  │  builds self-contained analysis prompt
  │
  └─→ Hermes Agent (hermes chat -q)
       │  reads files itself using read_file tool
       │  analyzes failure patterns
       │  writes improved prompt in response
       │
       └─→ parse_response() extracts structured sections
            via regex → writes to prompts/gen_{N}/
```

**历史变更**：
- 最初用 `evolve.mjs`（Node.js + `@opencode-ai/sdk`），需启动 `opencode serve`
- 后改为 `evolver.py`（Python），直接调用 Hermes 自身能力
- `evolve.mjs` 和 `package.json` 已不存在于 evolution/ 目录中

## orchestrator.py 参数参考

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--generations` | int | 3 | 演化轮数 |
| `--scene` | int | 1 | SAR 场景编号（1-5） |
| `--agents` | int | 2 | 救援机器人数量（1-6） |
| `--seed` | int | 42 | 随机种子 |
| `--model` | str | deepseek-v4-flash | LLM 模型 |
| `--provider` | str | openai | LLM 提供商 |
| `--api-base` | str | https://api.deepseek.com | API 地址 |
| `--max-steps` | int | None | 最大环境步数 |
| `--results-dir` | str | None | 跳过实验，使用已有结果 |
| `--target` | choice | coordinator | 要演化的 prompt（coordinator/worker/all） |
| `--skip-experiment` | bool | False | 只分析不跑实验 |

## evolver.py 参数参考

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `--results` | str | ✓ | 实验结果目录路径 |
| `--prompt-dir` | str | ✓ | 当前 prompt 目录路径 |
| `--gen` | int | ✓ | 当前代数编号 |
| `--target` | choice | 默认 coordinator | coordinator/worker/all |

## 运行条件

1. **环境变量**（所有命令都需要）：
   ```bash
   export no_proxy="localhost,0.0.0.0,127.0.0.1"
   export PYTHONPATH="src:$PYTHONPATH"
   ```

2. **Hermes CLI**：`hermes chat -q` 必须可用（evolver.py 依赖它）
3. **项目根目录**：orchestrator.py 需从 `LLaMAR/` 根目录运行

## 关键实现细节

### build_prompt() — 第 36-117 行
构造分析 prompt 的函数。包含：
- 实验数据路径（由参数注入）
- 分析指令（固定文本）
- 输出格式要求（固定文本）
- Anti-cheating 规则（固定文本）

### parse_response() — 第 121-165 行
响应解析器。使用三个正则提取：
1. `Analysis Summary` — 分析摘要
2. `Improved Prompt for {target}` — 改进后的完整 prompt
3. `Changes Summary` — 变更说明

特性：
- 支持 `##` 和 `###` 两种 header 级别
- 自动剥离 markdown 代码 fences
- prompt 提取在遇到下一个 section header 时停止

### call_hermes() — 第 169-195 行
Hermes 调用桥接。通过 `subprocess.run(["hermes", "chat", "-q", prompt, "--quiet"])` 调用，过滤掉 `session_id:` 和 `Warning:` 前缀行。

## 输出目录规范

```
evolution/prompts/gen_{N}/
├── coordinator/
│   └── system.md           # 改进后的 Coordinator prompt
├── worker/
│   └── system.md           # 改进后的 Worker prompt（仅当 target 包含 worker）
├── generation_summary.json # 元数据 + 分析摘要 + 变更说明
└── raw_response.txt        # Hermes 原始回复（用于调试）
```

## 常见问题

### Hermes 输出格式变化导致解析失败
如果 Hermes 回复没有按 `Analysis Summary` / `Improved Prompt` / `Changes Summary` 三个 section 的格式组织，`parse_response()` 会返回空值。解决：
1. 检查 `raw_response.txt` 看实际输出格式
2. 调整 `parse_response()` 中的正则

### Anti-Cheating 规则不够严
如果生成的 prompt 仍然包含了实验特例（比如硬编码了实体名），增强 `build_prompt()` 中的 anti-cheating 段，或者增加具体例子说明什么样的 prompt 算"作弊"。

### 大上下文超时
随着 gen 数增加，prompt 文件越来越大。evolver.py 的 `timeout=300`（5分钟）可能不够。如遇超时，增大 `call_hermes()` 的 timeout 参数。
