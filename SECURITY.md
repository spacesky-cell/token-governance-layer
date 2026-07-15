# Security policy

## Supported versions

Security fixes are provided for the latest minor release line published on npm. Run `npm view token-governance-layer version`; the corresponding `major.minor.x` line is supported, and older minor lines are not. Repository changes that have not yet been published are development snapshots, not supported releases.

## Report privately

Use [GitHub private vulnerability reporting](https://github.com/spacesky-cell/token-governance-layer/security/advisories/new) for vulnerabilities, suspected secret persistence, unsafe file permissions, receipt integrity bypasses, Hook replacement failures, or MCP/Gateway protocol issues.

Include the affected version, operating system, minimal reproduction, security impact, and whether the report contains synthetic or real data. Redact credentials and private payloads; do not attach a real ledger.

If private reporting is unavailable, open a minimal [GitHub issue](https://github.com/spacesky-cell/token-governance-layer/issues) asking maintainers to enable a private channel. Do not include exploit details, credentials, private output, or personal paths in that issue.

The project does not publish a personal security email address. Please keep discussion in GitHub's private reporting workflow until coordinated disclosure is agreed.

For the product's plaintext storage, same-user threat boundary, detector limitations, and retention behavior, read [docs/security.md](docs/security.md).
