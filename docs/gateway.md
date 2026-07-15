# Experimental MCP Gateway

The v0.2 Gateway is an optional **experimental, tools-only** stdio proxy. It is not part of the primary Claude Code installation and is not a general MCP compatibility claim.

## State A release status

T08 reached terminal State A: the public `tgl-mcp-gateway` bin remains packaged because strict local lifecycle, framing, cancellation, timeout, stderr-redaction, backpressure, partial-failure, and shutdown fixtures pass. State A does not promote the Gateway to stable. Real reference-server interoperability remains unproven, and the v2 ledger intentionally blocks the Gateway's legacy schema-receipt write path.

In practical terms:

- `list_backend_tools` and qualified `invoke_tool` are the supported experimental path;
- `get_tool_schema` is exposed, but currently attempts a disabled legacy ledger write and returns an error rather than creating a non-v2 receipt;
- restore/audit tools are present for existing valid receipts;
- MCP resources and prompts are not proxied;
- use the standalone `tgl-mcp` server for supported TGL restore/audit behavior.

## Configuration

Create a strict config with one or more stdio backend commands:

```json
{
  "ledger": {
    "path": ".tgl/ledger.sqlite",
    "retention_days": 30
  },
  "gateway": {
    "request_timeout_seconds": 10,
    "tool_policy": {
      "allow": ["code::search_code"],
      "deny": []
    },
    "backends": [
      {
        "name": "code",
        "command": "python",
        "args": ["./path/to/code_mcp_server.py"]
      }
    ]
  }
}
```

Relative backend command paths resolve from the config file directory. Bare commands resolve through `PATH`. Backend names must be unique. Policy names use `backend::tool`; deny entries take precedence, an empty allow list allows all tools not denied, and a non-empty allow list hides everything else from catalog, schema lookup, and invocation.

Start the Gateway:

```bash
tgl-mcp-gateway --config /absolute/path/to/token-governance.config.json
```

Or generate an MCP client snippet:

```bash
tgl mcp-config --config /absolute/path/to/token-governance.config.json
```

Check config, ledger access, backend command discovery, and policy shape without starting a persistent Gateway:

```bash
tgl doctor --config /absolute/path/to/token-governance.config.json
```

## Tools-only surface

The upstream client sees Gateway tools rather than every backend schema:

- `list_backend_tools` returns backend/name/qualified-name/description records and accepts an optional keyword query;
- `get_tool_schema` is intended to retrieve one full backend schema, subject to the v0.2 limitation above;
- `invoke_tool` forwards arguments to a selected qualified backend tool;
- `retrieve_original`, `explain_receipt`, `show_savings`, and `list_context_risks` expose local ledger operations.

Backend tool name collisions require `backend::tool`. Backend MCP `isError` tool results remain tool results; transport/protocol failures remain errors.

## Lifecycle contract

```text
Upstream client: CREATED -> INITIALIZED -> ACTIVE -> CLOSING -> CLOSED
Backend:         STARTING -> INITIALIZED -> ACTIVE -> FAILED | CLOSING -> CLOSED
```

The Gateway acts as its own MCP client and never impersonates the upstream client. On upstream `initialize`, it starts and initializes each backend with Gateway `clientInfo`, validates protocol `2025-06-18`, records capabilities, sends `notifications/initialized`, and only then marks that backend ACTIVE. The upstream becomes ACTIVE only after its own initialized notification.

Requests before ACTIVE are rejected. Responses are routed by JSON-RPC request ID; interleaved backend notifications cannot satisfy a request. `notifications/tools/list_changed` invalidates only that backend's cached catalog. Cancellation is forwarded once a backend request ID is bound, including cancellation races during cold catalog loading.

Each backend uses a bounded write queue and frame size, a configurable request timeout of 1-120 seconds (default 10), and a secret-redacted 64 KiB stderr ring. One backend may fail while other ACTIVE backends remain available; catalog responses include structured unavailable-backend entries. Shutdown closes stdin, waits, terminates, and finally kills a process if required.

## Strict fixtures and evidence

The dependency-free transport is verified against `tests/fixtures/strict_mcp_backend.py` and failure fixtures covering initialization order, client identity, notifications, out-of-order/interleaved responses, cancellation, timeout, malformed/oversized frames, stderr secrets/flooding, cache invalidation, partial availability, concurrent output serialization, queue backpressure, and deterministic close.

Machine-readable T08 evidence is stored in `docs/evidence/2026-07-14-t08-mcp-gateway.json`. These fixtures are repository-controlled and do not substitute for pinned, credential-free tests against diverse real MCP servers.

## Experimental limitations

- Tools only: no `resources/*`, `prompts/*`, subscriptions, or sampling proxy.
- No broad compatibility guarantee for real backend servers or SDK variants.
- No backend authentication, sandboxing, permission broker, or process isolation.
- Backend commands inherit the Gateway process environment and current-user privileges.
- Tool allow/deny policy is exact-name filtering, not semantic authorization.
- The Gateway does not transform arbitrary backend tool results.
- `get_tool_schema` has the v0.2 legacy receipt limitation described above.

Do not expose the Gateway to untrusted clients or configure untrusted backend commands. Keep it local and experimental.
