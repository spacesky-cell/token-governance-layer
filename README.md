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

### 快速使用：npm 安装

```powershell
npm install -g token-governance-layer
cd "C:\path\to\your-project"
tgl claude-install --project .
claude
```

也可以不全局安装：

```powershell
cd "C:\path\to\your-project"
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

### Quick Start With npm

```powershell
npm install -g token-governance-layer
cd "C:\path\to\your-project"
tgl claude-install --project .
claude
```

Without a global install:

```powershell
cd "C:\path\to\your-project"
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

