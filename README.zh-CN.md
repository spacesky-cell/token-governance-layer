# Token Governance Layer

[![状态：实验性](https://img.shields.io/badge/status-experimental-orange)](https://github.com/spacesky-cell/token-governance-layer)
[![CI](https://github.com/spacesky-cell/token-governance-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/spacesky-cell/token-governance-layer/actions/workflows/ci.yml)
[![Python 3.10-3.14](https://img.shields.io/badge/python-3.10--3.14-blue)](https://www.python.org/)
[![Node 22-24](https://img.shields.io/badge/node-22--24-339933)](https://nodejs.org/)
[![许可证：MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**实验性（Experimental）。** Token Governance Layer 是面向 Claude Code 的本地优先输出治理层：在工具结果重新进入模型上下文前，它只保守压缩已注册的重复日志、测试进度和构建进度，并为确实发生转换的输出保留可精确恢复的本地原文。

## 30 秒安装

支持 Windows、macOS、Linux，需要 Python 3.10-3.14 和 Node.js 22 或 24。

```bash
npm install -g token-governance-layer
cd /path/to/your-project
tgl init --project .
tgl claude-install --project .
tgl doctor --project . --integration
claude
```

安装器会使用全局 npm 安装中的稳定绝对命令路径。`npx`/`npm exec` 不能创建持久的 Claude 集成。

## Claude 会看到什么

对于已注册的重复日志命令，四个连续相同的行会变成一个可重建标记，唯一的最终状态保持原样：

```text
# 转换前
heartbeat service waiting for worker
heartbeat service waiting for worker
heartbeat service waiting for worker
heartbeat service waiting for worker
service ready

# 治理后
[TGL D 0-3 x4]
service ready
```

**安全边界：** 只有内置策略匹配、候选结果更小且独立保真校验通过时才会转换。未知输出、源码、diff、搜索结果、JSON、疑似 secret、畸形请求、超大载荷和不完整安装都会原样放行。原文以明文存入本地 SQLite ledger，并限制为当前操作系统用户访问；这不是加密，无法抵御同一账户、管理员/root 或已失陷的主机。

## 保守治理规则

Claude Code 自动转换只处理结构化的 `Bash` 和 `PowerShell` 结果，而且必须匹配以下已注册类别之一：

| 策略 | 可识别范围 | 允许折叠的内容 |
| --- | --- | --- |
| `repetitive_log` | `tail *.log`、`journalctl`、`docker logs`、`kubectl logs` 等已注册日志命令 | 三行及以上连续、完全相同、非保护行 |
| `test_output` | 已注册测试命令，且输出具有已识别的测试特征 | 连续重复行或已注册测试进度单元 |
| `build_output` | 已注册构建命令，且输出具有已识别的构建特征 | 连续重复行或已注册构建进度单元 |

stderr、退出状态、中断状态、warning、error、failure、traceback、assertion、最终测试/构建摘要和检测到的路径都会保留。源码特征、diff、搜索命令、JSON/JSONL、混合或歧义命令链以及未注册输出不会匹配自动策略。

Secret 检测器是尽力而为的保守护栏，不是数据防泄漏系统。检测到疑似 secret 时，TGL 会原样放行且不持久化该载荷。完整威胁模型与保留策略见 [安全与隐私](docs/security.md)。

## 安装生命周期

`tgl init --project .` 仅在文件不存在时创建 `token-governance.config.json`。随后，`tgl claude-install --project .` 将 TGL 自有条目合并到：

- `.claude/settings.json`：`PostToolUse` hook；
- `.mcp.json`：独立恢复/审计 MCP server；
- `.tgl/install-ownership.json` 与 `.tgl/install-state.json`：精确所有权和完整性检查。

安装状态完整之前，hook 始终原样放行。以下诊断命令只读：

```bash
tgl doctor --project .
tgl doctor --project . --integration
```

如果 doctor 报告 TGL 自有安装不完整，请从同一个可信全局安装显式修复：

```bash
tgl claude-install --project . --repair
tgl doctor --project . --integration
```

如果已记录的全局命令路径发生变化，应先卸载再安装。如果无法证明所有权，应先修复；卸载器会有意保留所有权不明的 Hook/MCP 条目。

精确卸载流程：

```bash
tgl claude-uninstall --project .
npm uninstall -g token-governance-layer
```

项目配置与 `.tgl/ledger.sqlite` 会保留，以便继续恢复 receipt。手工删除前请先检查或备份。

## CLI 与 receipt

手动治理从 stdin 读取内容，并使用同一个封闭策略集合：

```bash
tgl --db ./.tgl/ledger.sqlite govern --strategy repetitive_log < app.log
tgl --db ./.tgl/ledger.sqlite retrieve tgl_<receipt_id>
tgl --db ./.tgl/ledger.sqlite inspect tgl_<receipt_id>
tgl --db ./.tgl/ledger.sqlite stats
tgl --db ./.tgl/ledger.sqlite risks
```

v0.2 CLI 使用 `--strategy auto|repetitive_log|test_output|build_output`。旧的 `--content-type` 和 `--source` 参数已移除。

只有独立校验通过的转换才会创建 receipt。原文和候选结果在本地提交后，receipt 为 **prepared**；适配器成功写出转换结果后才标记为 **emitted**。统计值是确定性的**候选节省**（转换前估算 token 减转换后估算 token），顶层 `stats` 只统计 emitted 转换；它不是供应商计费数据。

## MCP 恢复与审计

全局安装的 Claude 集成会自动配置 `tgl-mcp`。独立 MCP 客户端也可直接启动：

```bash
tgl-mcp --config /absolute/path/to/token-governance.config.json
```

它提供 `govern_context`、`retrieve_original`、`explain_receipt`、`show_savings` 和 `list_context_risks`。MCP 恢复必须分页：`retrieve_original` 接收 `receipt_id`、`offset`（默认 `0`）和 `max_chars`（默认 `4096`，最大 `16384`），按 `next_offset` 继续，直到其为 `null`。需要一次性精确导出全文时使用 CLI 的 `retrieve`。

v0.2 MCP 迁移移除了 `govern_context` 的 `content_type`/`source`，也移除了无界的 `full` 恢复。破坏性变更见 [CHANGELOG.md](CHANGELOG.md)。

## 实验性 MCP Gateway

`tgl-mcp-gateway` 是可选的实验性 tools-only 代理。它提供精简的后端工具目录和限定名工具调用；其 `get_tool_schema` 当前会触发被 v2 阻止的 legacy receipt 路径并返回错误。它**不**代理 MCP resources 或 prompts，严格本地 fixture 也不能证明广泛的真实 server 互操作性。

Gateway 不属于主要 Claude 安装流程。启用前请阅读 [Gateway 配置、生命周期与限制](docs/gateway.md)。

## 确定性微基准

<!-- TGL-BENCHMARK:START -->
<!-- TGL-BENCHMARK:RESULT-SHA256 8146af8b74d92abc4356654dbab124956c0527243fc928876d97214adf0fe18b -->

已提交的 v0.2.0 确定性微基准包含 14 个固定样例：5 个转换、9 个原样放行，全部保护事实检查通过。本地估算器给出的总量为 348 token 降到 307 token，估算候选节省 41 token（11.7816%）。它只衡量候选体积与保真行为，不衡量供应商费用、计费 token、任务质量或模型端到端效果。

| 样例数 | 转换 | 原样放行 | 转换前估算 | 转换后估算 | 估算候选节省 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 14 | 5 | 9 | 348 | 307 | 41 (11.7816%) |

<!-- TGL-BENCHMARK:END -->

复现结果并检查 README 绑定：

```bash
python -m benchmarks.run --manifest benchmarks/fixtures/manifest.json --output .tgl/benchmark-v0.2.0.json
python -m benchmarks.check_readme --results benchmarks/results/v0.2.0.json --readme README.md
python -m benchmarks.check_readme --results benchmarks/results/v0.2.0.json --readme README.zh-CN.md
```

方法、fixture 身份、结果 schema 与限制见 [docs/benchmark.md](docs/benchmark.md)。

## 兼容性

| 组件 | 支持范围 |
| --- | --- |
| 操作系统 | Windows、macOS、Linux |
| Python | 3.10、3.11、3.12、3.13、3.14 |
| Node.js wrapper | 22、24 |
| 主要宿主 | Claude Code 项目级 `PostToolUse` 集成 |
| 存储 | 本地 SQLite；明文、当前用户权限 |
| MCP | stdio tools server；协议 `2025-06-18` |
| Gateway | 实验性，仅 tools |

## 开发

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio ruff
python -m pytest -q
python -m ruff check src tests benchmarks
python -m compileall -q src tests benchmarks
npm pack --dry-run
```

另见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 与 [英文 README](README.md)。

## 许可证

[MIT](LICENSE)
