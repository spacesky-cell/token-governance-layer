# Contributing

Contributions should preserve the project's conservative Claude Code boundary: shared semantics belong in the engine/contracts/ledger, while Hook, CLI, MCP, and Gateway remain adapters.

## Development setup

Use a supported Python (3.10-3.14) and Node.js (22 or 24):

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio ruff
```

Run the complete local checks:

```bash
python -m pytest -q
python -m ruff check src tests benchmarks
python -m compileall -q src tests benchmarks
npm pack --dry-run
```

Focused suites are named by owner layer, for example:

```bash
python -m pytest -q tests/test_governance_v2.py tests/test_shapers.py tests/test_preservation.py
python -m pytest -q tests/test_installer.py tests/test_claude_hook.py
python -m pytest -q tests/test_mcp_server.py tests/test_mcp_gateway.py
```

## Benchmark and documentation checks

Do not hand-edit benchmark numbers. Reproduce the canonical result into ignored scratch space and compare exact bytes:

```bash
python -m benchmarks.run --manifest benchmarks/fixtures/manifest.json --output .tgl/benchmark-v0.2.0.json
python -c "from pathlib import Path; a=Path('.tgl/benchmark-v0.2.0.json').read_bytes(); b=Path('benchmarks/results/v0.2.0.json').read_bytes(); raise SystemExit(0 if a == b else 1)"
python -m benchmarks.check_readme --results benchmarks/results/v0.2.0.json --readme README.md
python -m benchmarks.check_readme --results benchmarks/results/v0.2.0.json --readme README.zh-CN.md
python .github/scripts/check_public_docs.py
```

When public behavior changes, update the English and Chinese READMEs together, the relevant focused doc, and the changelog. Keep claims limited to checked-in evidence.

## Pull requests

- Start from a focused branch and keep unrelated changes out.
- Add or update tests for runtime behavior; observe the relevant failure before implementation when fixing a bug or adding a feature.
- Run the strongest relevant focused checks and the full suite before requesting review.
- Do not commit credentials, real private payloads, personal filesystem paths, ledgers, transcripts, package tarballs, or npm configuration.
- Describe user-visible and breaking behavior explicitly, including passthrough and recovery consequences.

Security reports do not belong in a normal pull request or public issue. Follow [SECURITY.md](SECURITY.md).
