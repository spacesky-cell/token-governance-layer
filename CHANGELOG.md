# Changelog

All notable changes are documented here. This project follows semantic versioning for public package releases.

## [Unreleased]

The changes below target v0.2.0 and are not published until the release workflow completes.

### Added

- Typed, strict governance configuration and immutable request/result contracts.
- Built-in `repetitive_log`, `test_output`, and `build_output` shapers with independent reconstruction and protected-fact verification.
- Project-local Claude Code installer, read-only doctor, explicit repair, ownership-aware uninstall, and isolated Hook/MCP integration checks.
- Ledger v2 with transformed-only receipts, prepared/emitted delivery state, payload-free events, integrity validation, v0.1 migration, pruning, and explicit legacy purge API.
- Hardened standalone MCP lifecycle, bounded paginated retrieval, structured errors, and cancellation handling.
- Experimental tools-only MCP Gateway with strict lifecycle/fault fixtures, bounded transport, partial availability, tool policy, and deterministic shutdown.
- Fixed-fixture deterministic microbenchmark with machine-readable schema/results and README drift checks.
- Windows/macOS/Linux CI across Python 3.10-3.14 and Node.js 22/24 package contracts.

### Changed

- Product positioning is Claude Code-specific; generic support for all coding agents is not claimed.
- Automatic governance is conservative: only registered log/test/build output shapes can transform. Source, diffs, searches, JSON, secrets, ambiguous content, and failure paths pass through.
- Savings are labeled estimated candidate savings. Top-level totals include emitted v2 transformations only; prepared and legacy receipts are separate.
- Persistent Claude installation requires `npm install -g token-governance-layer`; `npx`/`npm exec` remains suitable only for non-persistent one-shot commands.

### Breaking

- Removed the generic head/tail summarizer and the old `GovernanceEngine.govern_context` compatibility behavior.
- CLI `tgl govern` removed `--content-type` and `--source`; use `--strategy auto|repetitive_log|test_output|build_output`.
- MCP `govern_context` removed `content_type` and `source`; it now accepts only `payload` and optional `strategy`.
- MCP `retrieve_original` removed unbounded `full` retrieval; use `offset`/`max_chars` pagination or CLI `retrieve` for an exact full export.
- Legacy ledger rows migrate as explicitly legacy, are excluded from v2 savings, and cannot be created through the v2 write path.

### Security

- Secret-like, malformed, oversized, unavailable, and failed-verification content passes through without raw receipt persistence.
- Ledger files and SQLite sidecars receive current-user protections, with a documented plaintext same-user threat boundary.
- Gateway stderr is bounded and secret-redacted; protocol output remains separate.

## [0.1.0] - 2026-07-09

- Initial npm package with local governance, SQLite receipts, Claude Hook, standalone MCP server, and prototype Gateway.

[Unreleased]: https://github.com/spacesky-cell/token-governance-layer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/spacesky-cell/token-governance-layer/releases/tag/v0.1.0
