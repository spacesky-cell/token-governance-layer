# Token Governance Layer

[![Status: Experimental](https://img.shields.io/badge/status-experimental-orange)](https://github.com/spacesky-cell/token-governance-layer)
[![CI](https://github.com/spacesky-cell/token-governance-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/spacesky-cell/token-governance-layer/actions/workflows/ci.yml)
[![Python 3.10-3.14](https://img.shields.io/badge/python-3.10--3.14-blue)](https://www.python.org/)
[![Node 22-24](https://img.shields.io/badge/node-22--24-339933)](https://nodejs.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Experimental.** Token Governance Layer is a local-first output governance layer for Claude Code: it conservatively compacts registered repetitive logs, test progress, and build progress before they re-enter model context, while keeping exact local originals for transformed output.

## 30-second setup

Requires Python 3.10-3.14 and Node.js 22 or 24 on Windows, macOS, or Linux.

```bash
npm install -g token-governance-layer
cd /path/to/your-project
tgl init --project .
tgl claude-install --project .
tgl doctor --project . --integration
claude
```

The installer uses stable absolute commands from the global npm installation. `npx`/`npm exec` cannot create a persistent Claude integration.

## What Claude sees

Given a registered repetitive-log command, four identical lines can become one reconstructable marker while the unique final state remains verbatim:

```text
# Before
heartbeat service waiting for worker
heartbeat service waiting for worker
heartbeat service waiting for worker
heartbeat service waiting for worker
service ready

# Governed output
[TGL D 0-3 x4]
service ready
```

**Safety boundary:** output is transformed only when a built-in strategy matches, the candidate is smaller, and an independent preservation check passes. Unknown output, source, diffs, search results, JSON, secret-like content, malformed requests, oversized payloads, and incomplete installations pass through unchanged. Stored originals are plaintext in a local SQLite ledger protected for the current OS user; this is not encryption and does not defend against the same account, administrators/root, or a compromised machine.

## Conservative behavior

Automatic Claude Code transformation is limited to structured `Bash` and `PowerShell` results that match one registered family:

| Strategy | Recognized surface | What may be folded |
| --- | --- | --- |
| `repetitive_log` | Registered log commands such as `tail *.log`, `journalctl`, `docker logs`, and `kubectl logs` | Three or more consecutive, identical, non-protected lines |
| `test_output` | Registered test commands with a recognized test-output signature | Consecutive duplicates or registered test progress units |
| `build_output` | Registered build commands with a recognized build-output signature | Consecutive duplicates or registered build progress units |

Stderr, exit status, interruption state, warnings, errors, failures, tracebacks, assertions, final test/build summaries, and detected paths are protected. Source-like content, diffs, search commands, JSON/JSONL records, mixed or ambiguous command families, and unregistered output do not match an automatic strategy.

The secret detector is a conservative best-effort guard, not a data-loss-prevention system. When it detects a secret-like marker, TGL passes the payload through and does not persist it. See [Security and privacy](docs/security.md) for the complete threat model and retention guidance.

## Installation lifecycle

`tgl init --project .` creates `token-governance.config.json` only when it is absent. `tgl claude-install --project .` then merges TGL-owned entries into:

- `.claude/settings.json` for the `PostToolUse` hook;
- `.mcp.json` for the standalone restore/audit MCP server;
- `.tgl/install-ownership.json` and `.tgl/install-state.json` for exact ownership and completeness checks.

The hook remains passthrough until the install state is complete. Diagnose without writes:

```bash
tgl doctor --project .
tgl doctor --project . --integration
```

If doctor reports an incomplete TGL-owned installation, repair it explicitly from the same proven global installation:

```bash
tgl claude-install --project . --repair
tgl doctor --project . --integration
```

If recorded global command paths changed, uninstall before reinstalling. If ownership cannot be proven, repair first; uninstall deliberately preserves unproven Hook/MCP entries.

Exact uninstall flow:

```bash
tgl claude-uninstall --project .
npm uninstall -g token-governance-layer
```

Project config and `.tgl/ledger.sqlite` are preserved so receipts remain recoverable. Review or back them up before deleting them manually.

## CLI and receipts

Manual governance accepts stdin and the same closed strategy set:

```bash
tgl --db ./.tgl/ledger.sqlite govern --strategy repetitive_log < app.log
tgl --db ./.tgl/ledger.sqlite retrieve tgl_<receipt_id>
tgl --db ./.tgl/ledger.sqlite inspect tgl_<receipt_id>
tgl --db ./.tgl/ledger.sqlite stats
tgl --db ./.tgl/ledger.sqlite risks
```

The v0.2 CLI contract uses `--strategy auto|repetitive_log|test_output|build_output`. The old `--content-type` and `--source` arguments were removed.

Receipts are created only for verified transformations. A receipt is **prepared** after the original and candidate are committed locally; adapters mark it **emitted** only after transformed output is written successfully. Reported savings are deterministic **candidate savings** (`estimated tokens before - estimated tokens after`), and top-level `stats` counts emitted transformations only. These values are not provider billing measurements.

## MCP restore and audit

The globally installed Claude integration configures `tgl-mcp` automatically. A standalone client can run it directly:

```bash
tgl-mcp --config /absolute/path/to/token-governance.config.json
```

It exposes `govern_context`, `retrieve_original`, `explain_receipt`, `show_savings`, and `list_context_risks`. MCP retrieval is paginated: call `retrieve_original` with `receipt_id`, `offset` (default `0`), and `max_chars` (default `4096`, maximum `16384`), then continue with `next_offset` until it is `null`. Use the CLI `retrieve` command for one exact full export.

The v0.2 MCP migration removes `content_type`/`source` from `govern_context` and removes unbounded `full` retrieval. See [CHANGELOG.md](CHANGELOG.md) for breaking changes.

## Experimental MCP Gateway

`tgl-mcp-gateway` is an optional, experimental tools-only proxy. It exposes a compact backend tool catalog and invokes qualified backend tools. Its `get_tool_schema` surface currently hits a v2-blocked legacy receipt path and returns an error. It does **not** proxy MCP resources or prompts, and strict local fixtures do not prove broad real-server interoperability.

The Gateway is outside the primary Claude setup. Read [Gateway configuration, lifecycle, and limits](docs/gateway.md) before enabling it.

## Deterministic microbenchmark

<!-- TGL-BENCHMARK:START -->
<!-- TGL-BENCHMARK:RESULT-SHA256 8146af8b74d92abc4356654dbab124956c0527243fc928876d97214adf0fe18b -->

The checked-in v0.2.0 deterministic microbenchmark contains 14 fixed cases. Five transform and nine pass through; all protected-fact checks pass. The local estimator reports 348 tokens before and 307 after, for 41 estimated candidate tokens saved (11.7816%). This measures candidate size and preservation behavior only. It does not measure provider cost, billed tokens, task quality, or end-to-end model performance.

| Cases | Transformed | Passthrough | Estimated before | Estimated after | Estimated candidate saved |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 14 | 5 | 9 | 348 | 307 | 41 (11.7816%) |

<!-- TGL-BENCHMARK:END -->

Reproduce the result and its README binding:

```bash
python -m benchmarks.run --manifest benchmarks/fixtures/manifest.json --output .tgl/benchmark-v0.2.0.json
python -m benchmarks.check_readme --results benchmarks/results/v0.2.0.json --readme README.md
python -m benchmarks.check_readme --results benchmarks/results/v0.2.0.json --readme README.zh-CN.md
```

Methodology, fixture identities, result schema, and limitations are documented in [docs/benchmark.md](docs/benchmark.md).

## Compatibility

| Component | Supported release line |
| --- | --- |
| Operating systems | Windows, macOS, Linux |
| Python | 3.10, 3.11, 3.12, 3.13, 3.14 |
| Node.js wrapper | 22, 24 |
| Primary host | Claude Code project-level `PostToolUse` integration |
| Storage | Local SQLite; plaintext, current-user permissions |
| MCP | stdio tools server; protocol `2025-06-18` |
| Gateway | Experimental, tools only |

## Development

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio ruff
python -m pytest -q
python -m ruff check src tests benchmarks
python -m compileall -q src tests benchmarks
npm pack --dry-run
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the [Chinese README](README.zh-CN.md).

## License

[MIT](LICENSE)
