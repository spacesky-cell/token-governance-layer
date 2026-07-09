# Token Governance Layer

[中文](#中文说明) | [English](#english)

Local-first token governance for coding agents. It automatically compresses long
tool outputs before they re-enter the model context, records a local receipt,
and lets the agent restore the original payload when needed.

## 中文说明

### 它解决什么问题

开发者在使用 Claude Code、MCP 工具或其他编码 Agent 时，长日志、搜索结果、
文件片段、工具 schema 很容易反复进入上下文，造成 token 浪费。Token Governance
Layer 的目标是把这类内容自动治理掉：能省就省，不能省就原样通过，同时保留可审计、
可恢复的本地凭证。

### 当前能力

- 本地优先的治理引擎，默认策略保守。
- SQLite receipt ledger，原文保存在本地。
- Claude Code `PostToolUse` 自动 hook。
- MCP 恢复和审计工具：
  - `retrieve_original`
  - `explain_receipt`
  - `show_savings`
  - `list_context_risks`
- MCP gateway：先暴露紧凑工具目录，需要时再加载完整 schema。
- npm 分发 wrapper：用户可通过 `npm install -g` 或 `npx` 使用。

### 真实基准数据

下面的数据来自 2026-07-09 在本仓库运行的真实样本，不是合成日志。样本包含
`npm pack --dry-run` 输出、`pytest -q` 输出、核心源码文件、MCP gateway 源码、
README 文档和源码/测试关键词搜索结果。token 数使用项目内置的确定性本地估算器
`token_governance.tokenizer.estimate_tokens`，用于稳定对比治理前后上下文大小。

| 样本 | 行数 | 动作 | 治理前 token | 治理后 token | 节省 token | 节省率 |
|---|---:|---|---:|---:|---:|---:|
| `npm pack --dry-run` 发布包清单 | 34 | summarize | 337 | 107 | 230 | 68.25% |
| `pytest -q` 测试输出 | 2 | passthrough | 48 | 48 | 0 | 0.00% |
| 核心治理引擎源码 | 107 | summarize | 591 | 70 | 521 | 88.16% |
| MCP gateway 源码 | 510 | summarize | 4,182 | 403 | 3,779 | 90.36% |
| README 文档上下文 | 407 | summarize | 2,070 | 114 | 1,956 | 94.49% |
| 源码和测试关键词搜索结果 | 3 | passthrough | 44 | 44 | 0 | 0.00% |

汇总结果：

- 总样本数：6
- 治理前：7,272 estimated tokens
- 治理后：786 estimated tokens
- 总节省：6,486 estimated tokens
- 总体节省率：89.19%
- 触发压缩：4 个样本；短输出原样通过：2 个样本
- 风险等级：6 个样本均为 `low`

这组数据反映了当前默认策略的核心行为：长源码、长文档、长命令输出会显著缩短；
很短的测试输出和搜索结果不会为了“看起来节省”而强行压缩。

### 快速使用：npm 安装

```powershell
npm install -g token-governance-layer
cd "<your-project>"
tgl claude-install --project .
claude
```

也可以不全局安装：

```powershell
cd "<your-project>"
npx token-governance-layer claude-install --project .
claude
```

npm 包会暴露这些命令：

```text
tgl
tgl-mcp
tgl-mcp-gateway
tgl-claude-hook
```

要求机器上有 Python 3.10+。Windows 下 wrapper 会依次尝试 `py -3`、`python`、
`python3`；macOS/Linux 下会尝试 `python3`、`python`。

### Claude Code 自动治理

在项目目录执行：

```powershell
tgl claude-install --project .
```

它只会写入项目级配置：

- `.claude/settings.json`：注册 `PostToolUse` hook。
- `.mcp.json`：注册 `token-governance-layer` MCP server，用于恢复和审计。
- `.tgl/claude-ledger.sqlite`：本地 receipt ledger。

之后正常启动 Claude Code：

```powershell
claude
```

用户不需要手动调用 `govern_context`。当 Claude 使用 `Bash`、`PowerShell`、`Read`、
`Grep`、`Glob`、`LS`、`Task`、`WebFetch`、`WebSearch` 等高输出工具时，如果治理后
更短，hook 会自动替换工具输出并附加 receipt ID。

需要原文时，在 Claude 中让它调用 MCP：

```text
Use retrieve_original for receipt_id tgl_<id>.
Use explain_receipt for receipt_id tgl_<id>.
Use show_savings to show token governance savings.
```

### CLI 使用

治理 stdin：

```bash
type long-log.txt | tgl govern --content-type log
```

指定 ledger：

```bash
type long-log.txt | tgl --db ./.tgl/ledger.sqlite govern --content-type log --source local-test
```

恢复原文：

```bash
tgl --db ./.tgl/ledger.sqlite retrieve tgl_<receipt_id>
```

查看 receipt：

```bash
tgl --db ./.tgl/ledger.sqlite inspect tgl_<receipt_id>
```

查看累计节省：

```bash
tgl --db ./.tgl/ledger.sqlite stats
```

### MCP Server

通过 stdio 启动：

```bash
tgl-mcp --db ./.tgl/ledger.sqlite
```

MCP client 配置示例：

```json
{
  "mcpServers": {
    "token-governance-layer": {
      "command": "tgl-mcp",
      "args": ["--db", "./.tgl/ledger.sqlite"]
    }
  }
}
```

### MCP Gateway

gateway 可以包装多个后端 MCP server，让客户端先看到紧凑工具目录，再按需读取
完整 schema。

```bash
tgl-mcp-gateway --config ./token-governance.config.json
```

配置示例：

```json
{
  "ledger": {
    "path": "./.tgl/ledger.sqlite"
  },
  "gateway": {
    "backends": [
      {
        "name": "code",
        "command": "python",
        "args": ["./path/to/code_mcp_server.py"]
      },
      {
        "name": "docs",
        "command": "python",
        "args": ["./path/to/docs_mcp_server.py"]
      }
    ]
  }
}
```

### 本地开发

```bash
python -m pip install -e .
python -m pytest -q
npm pack --dry-run
```

本地测试 npm tarball：

```powershell
npm pack
npm install -g .\token-governance-layer-0.1.0.tgz
tgl --help
```

发布 npm：

```powershell
npm login
npm publish --access public
```

## English

### Problem

Coding agents often feed long command outputs, logs, search results, file
snippets, and MCP tool schemas back into model context. That wastes tokens and
makes long tasks more expensive. Token Governance Layer automatically governs
those payloads, keeps a local receipt, and allows exact restoration when the
original output is needed.

### Features

- Conservative local governance engine.
- SQLite receipt ledger with local original-payload storage.
- Claude Code automatic `PostToolUse` hook.
- MCP restore and audit tools:
  - `retrieve_original`
  - `explain_receipt`
  - `show_savings`
  - `list_context_risks`
- MCP gateway that exposes a compact tool catalog first and full schemas on
  demand.
- npm wrapper distribution for simple installation.

### Real Benchmark Data

The data below was collected on 2026-07-09 from real payloads in this
repository, not synthetic logs. The benchmark used `npm pack --dry-run` output,
`pytest -q` output, source files, README content, and keyword search results
across source/tests. Token counts use the built-in deterministic local
estimator, `token_governance.tokenizer.estimate_tokens`, so the before/after
comparison is reproducible.

| Sample | Lines | Action | Before tokens | After tokens | Tokens saved | Save rate |
|---|---:|---|---:|---:|---:|---:|
| `npm pack --dry-run` package listing | 34 | summarize | 337 | 107 | 230 | 68.25% |
| `pytest -q` test output | 2 | passthrough | 48 | 48 | 0 | 0.00% |
| Core governance engine source | 107 | summarize | 591 | 70 | 521 | 88.16% |
| MCP gateway source | 510 | summarize | 4,182 | 403 | 3,779 | 90.36% |
| README documentation context | 407 | summarize | 2,070 | 114 | 1,956 | 94.49% |
| Keyword search results across source/tests | 3 | passthrough | 44 | 44 | 0 | 0.00% |

Summary:

- Total samples: 6
- Before governance: 7,272 estimated tokens
- After governance: 786 estimated tokens
- Total saved: 6,486 estimated tokens
- Overall save rate: 89.19%
- Summarized samples: 4; short passthrough samples: 2
- Risk classification: all 6 samples were `low`

The result shows the intended default behavior: long source files, long
documentation, and long command outputs shrink aggressively, while short outputs
pass through unchanged instead of being compressed for cosmetic savings.

### Quick Start With npm

```powershell
npm install -g token-governance-layer
cd "<your-project>"
tgl claude-install --project .
claude
```

Without a global install:

```powershell
cd "<your-project>"
npx token-governance-layer claude-install --project .
claude
```

The npm package exposes:

```text
tgl
tgl-mcp
tgl-mcp-gateway
tgl-claude-hook
```

Python 3.10+ must be available on `PATH`. On Windows the wrapper tries `py -3`,
`python`, then `python3`; on macOS/Linux it tries `python3`, then `python`.

### Claude Code Automatic Governance

Inside a project, run:

```powershell
tgl claude-install --project .
```

This writes only project-local configuration:

- `.claude/settings.json` registers the `PostToolUse` hook.
- `.mcp.json` registers the `token-governance-layer` MCP server for restore and
  audit.
- `.tgl/claude-ledger.sqlite` stores local receipts.

Then start Claude Code normally:

```powershell
claude
```

No manual `govern_context` call is needed. When Claude Code uses high-output
tools such as `Bash`, `PowerShell`, `Read`, `Grep`, `Glob`, `LS`, `Task`,
`WebFetch`, or `WebSearch`, the hook replaces the output only when the governed
output is shorter and appends a receipt ID.

To restore or inspect the original output, ask Claude to use the MCP tools:

```text
Use retrieve_original for receipt_id tgl_<id>.
Use explain_receipt for receipt_id tgl_<id>.
Use show_savings to show token governance savings.
```

### CLI

Govern stdin:

```bash
type long-log.txt | tgl govern --content-type log
```

Use an explicit ledger:

```bash
type long-log.txt | tgl --db ./.tgl/ledger.sqlite govern --content-type log --source local-test
```

Retrieve the original payload:

```bash
tgl --db ./.tgl/ledger.sqlite retrieve tgl_<receipt_id>
```

Inspect a receipt:

```bash
tgl --db ./.tgl/ledger.sqlite inspect tgl_<receipt_id>
```

Show aggregate savings:

```bash
tgl --db ./.tgl/ledger.sqlite stats
```

### MCP Server

Run over stdio:

```bash
tgl-mcp --db ./.tgl/ledger.sqlite
```

Example MCP client config:

```json
{
  "mcpServers": {
    "token-governance-layer": {
      "command": "tgl-mcp",
      "args": ["--db", "./.tgl/ledger.sqlite"]
    }
  }
}
```

### MCP Gateway

The gateway wraps one or more backend MCP servers. Clients first see a compact
tool surface; full backend schemas are loaded on demand.

```bash
tgl-mcp-gateway --config ./token-governance.config.json
```

Example config:

```json
{
  "ledger": {
    "path": "./.tgl/ledger.sqlite"
  },
  "gateway": {
    "backends": [
      {
        "name": "code",
        "command": "python",
        "args": ["./path/to/code_mcp_server.py"]
      },
      {
        "name": "docs",
        "command": "python",
        "args": ["./path/to/docs_mcp_server.py"]
      }
    ]
  }
}
```

### Development

```bash
python -m pip install -e .
python -m pytest -q
npm pack --dry-run
```

Test a local npm tarball:

```powershell
npm pack
npm install -g .\token-governance-layer-0.1.0.tgz
tgl --help
```

Publish to npm:

```powershell
npm login
npm publish --access public
```

### Safety Defaults

- Short payloads pass through unchanged.
- User instructions, secret-like content, and security-sensitive content are not
  semantically compressed.
- Protected lines for errors, assertions, tracebacks, file paths, and failures
  are preserved.
- Original content is stored locally and recoverable by receipt ID.
- Token counts use a deterministic local estimate until model-specific
  tokenizers are added.

