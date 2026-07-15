# Security and privacy model

Token Governance Layer is local-first, but local-first does not mean encrypted or isolated from the current operating-system account. This document defines the v0.2 boundary.

## Stored data

TGL may create or open an empty local SQLite ledger during startup, before any payload is classified. Ledger initialization does not itself persist tool output. Only output that was transformed and passed independent preservation verification creates a v2 receipt and stores its normalized original. A receipt also contains the governed candidate, integrity hash, strategy, closed risk/reason fields, estimated token counts, preservation result, delivery state, and timestamps. Passthrough output does not create a receipt; passthrough governance events use closed metadata and do not store raw payloads or free-form tool input.

The project installer normally places the ledger at `.tgl/ledger.sqlite`. SQLite sidecar files such as `-wal` and `-shm` may exist while the database is open.

## Plaintext and same-user threat boundary

Receipt contents are plaintext. TGL creates private files and directories and applies current-user-only permissions where the platform supports them:

- POSIX ledger files are regular, non-symlink files with mode `0600`; SQLite derives active sidecar modes from the protected main database, and unsafe parent symlinks are rejected.
- Windows ledger and sidecar files receive a DACL for the current user.

These controls reduce accidental cross-account access. They do not protect against:

- another process running as the same user;
- administrators, root, backup agents, endpoint/security software, or filesystem snapshots;
- malware or a compromised user session;
- a user who copies the ledger to a less protected location;
- disclosure from the original command, Claude transcript, shell history, terminal, or upstream MCP server.

Use full-disk encryption and normal host hardening when local plaintext is unacceptable. Do not use TGL as a secret vault.

## Secret detector limits

The detector checks bounded built-in patterns for common API keys, authorization headers, private-key markers, passwords, environment-style names, and configured literal markers. It runs before transformation and persistence. A match causes passthrough with `secret_detected`, no receipt, and payload-free event metadata.

Detection is best effort. Encoded, fragmented, novel, short, context-dependent, or deliberately obfuscated secrets can be missed; ordinary text can also resemble a secret and produce a false positive. Passthrough prevents TGL persistence but still leaves the original payload in its existing tool/Claude flow. Secret scanning at source, least-privilege credentials, and rotation remain necessary.

`policy.literal_secret_markers` can add exact local markers. Do not commit real secrets as markers: the config itself is plaintext.

## Fail-open behavior

Unknown or ambiguous output, unsupported source surfaces, source/diff/search/JSON content, malformed requests, oversized payloads, detector/classifier/verifier errors, deadline expiry, ledger failure, and incomplete project installation pass through. This preserves tool behavior but means TGL is not an enforcement boundary that can guarantee content removal.

## Retention, pruning, and deletion

`ledger.retention_days` defaults to 30 in generated config, but v0.2 does not run a scheduler and does not expose a `tgl prune` CLI command. Retention is therefore not automatic. Applications embedding the Python package can explicitly prune:

```python
from token_governance.ledger import ContextLedger

ledger = ContextLedger(".tgl/ledger.sqlite")
report = ledger.prune(retention_days=30)
```

`prune` removes expired receipts and linked events transactionally. Stop Claude/TGL processes before an operator-level backup or manual deletion so SQLite sidecars are not active. `tgl claude-uninstall` intentionally preserves the config and ledger; removal of local data is a separate, explicit operator action.

## Migrated legacy receipts

Opening a v0.1 ledger migrates its rows as `is_legacy=1`. Their historical semantics did not satisfy the v2 transformed-only and delivery-state contract, so they are excluded from v2 top-level savings and produce a `legacy_receipts_present` warning. Legacy rows may contain originals created under the broader v0.1 policy.

After backup and review, an embedding application can purge only legacy rows with explicit confirmation:

```python
from token_governance.ledger import ContextLedger

ledger = ContextLedger(".tgl/ledger.sqlite")
report = ledger.purge_legacy(confirm=True)
```

There is no v0.2 CLI wrapper for this destructive operation. Deleting the whole ledger also removes current receipts and recovery capability.

## Receipt and savings terminology

- **Prepared:** a verified transformation and exact original were committed before transformed output was returned.
- **Emitted:** an adapter successfully wrote the transformed result and then marked the receipt emitted.
- **Candidate savings:** deterministic local estimated tokens before minus after. This is not provider usage or billing.

Top-level savings include emitted v2 transformations only. Prepared and legacy counts are reported separately to avoid claiming savings for output that may not have reached the consumer.

## Reporting a vulnerability

Follow the private reporting process in [SECURITY.md](../SECURITY.md). Never put credentials, private payloads, ledger files, or exploit details in a public issue.
