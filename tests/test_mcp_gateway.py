import json
import os
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

import pytest

from token_governance import __version__

from token_governance.mcp_gateway import (
    BackendState,
    McpGateway,
    StdioMcpBackend,
    _redact,
)
from token_governance.ledger import ContextLedger


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "tests" / "fixtures" / "fake_mcp_backend.py"
STRICT_BACKEND = ROOT / "tests" / "fixtures" / "strict_mcp_backend.py"


def python_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def start_gateway(tmp_path):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--db",
            str(tmp_path / "gateway.sqlite"),
            "--backend",
            f"backend={sys.executable},{BACKEND}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )


def send_raw(proc, request):
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def send(proc, request):
    if not getattr(proc, "_tgl_active", False) and request.get("method") != "initialize":
        initialized = send_raw(
            proc,
            {
                "jsonrpc": "2.0",
                "id": "test-initialize",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "clientInfo": {"name": "pytest", "version": "1"},
                    "capabilities": {},
                },
            },
        )
        assert initialized["result"]["protocolVersion"] == "2025-06-18"
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()
        proc._tgl_active = True
    return send_raw(proc, request)


def test_gateway_requires_initialize_and_keeps_notifications_silent(tmp_path):
    proc = start_gateway(tmp_path)
    try:
        response = send_raw(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response["error"] == {"code": -32002, "message": "Client not initialized"}
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled"}) + "\n"
        )
        proc.stdin.flush()
        initialized = send(
            proc,
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    assert initialized["result"]["capabilities"] == {"tools": {}}


def test_gateway_exposes_compact_tool_surface(tmp_path):
    proc = start_gateway(tmp_path)
    try:
        response = send(proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    tool_names = {tool["name"] for tool in response["result"]["tools"]}
    assert tool_names == {
        "list_backend_tools",
        "get_tool_schema",
        "invoke_tool",
        "retrieve_original",
        "explain_receipt",
        "show_savings",
        "list_context_risks",
    }
    serialized = json.dumps(response["result"]["tools"])
    assert "Search code using a deliberately verbose description" not in serialized


def test_gateway_lists_catalog_but_rejects_legacy_schema_receipt_path(tmp_path):
    proc = start_gateway(tmp_path)
    try:
        catalog_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_backend_tools", "arguments": {}},
            },
        )
        catalog = json.loads(catalog_response["result"]["content"][0]["text"])
        schema_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_tool_schema",
                    "arguments": {"tool_name": "search_code"},
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    assert catalog["tools"] == [
        {
            "backend": "backend",
            "name": "search_code",
            "qualified_name": "backend::search_code",
            "description": "Search code using a deliberately verbose description that would be expensive when repeated.",
        },
        {
            "backend": "backend",
            "name": "read_symbol",
            "qualified_name": "backend::read_symbol",
            "description": "Read a symbol implementation by fully qualified name.",
        },
    ]
    assert "Legacy ledger writes are disabled" in schema_response["error"]["message"]


def test_gateway_invokes_backend_tool(tmp_path):
    proc = start_gateway(tmp_path)
    try:
        response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "invoke_tool",
                    "arguments": {
                        "tool_name": "search_code",
                        "arguments": {"query": "receipt ledger", "limit": 3},
                    },
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    assert response["result"]["content"][0]["text"] == 'backend called search_code with {"limit": 3, "query": "receipt ledger"}'


def test_gateway_namespaces_multiple_backends_and_invokes_selected_backend(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--db",
            str(tmp_path / "multi-gateway.sqlite"),
            "--backend",
            f"code={sys.executable},{BACKEND},--label,code",
            "--backend",
            f"docs={sys.executable},{BACKEND},--label,docs",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        catalog_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_backend_tools", "arguments": {}},
            },
        )
        invoke_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "invoke_tool",
                    "arguments": {
                        "tool_name": "docs::search_code",
                        "arguments": {"query": "install docs"},
                    },
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    catalog = json.loads(catalog_response["result"]["content"][0]["text"])
    qualified_names = {tool["qualified_name"] for tool in catalog["tools"]}
    assert {"code::search_code", "docs::search_code", "code::read_symbol", "docs::read_symbol"} == qualified_names
    assert invoke_response["result"]["content"][0]["text"] == 'docs called search_code with {"query": "install docs"}'


def test_gateway_loads_backends_from_config_file(tmp_path):
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": {"path": str(tmp_path / "configured.sqlite")},
                "gateway": {
                    "backends": [
                        {
                            "name": "code",
                            "command": sys.executable,
                            "args": [str(BACKEND), "--label", "code"],
                        },
                        {
                            "name": "docs",
                            "command": sys.executable,
                            "args": [str(BACKEND), "--label", "docs"],
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        catalog_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_backend_tools", "arguments": {}},
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    catalog = json.loads(catalog_response["result"]["content"][0]["text"])
    qualified_names = {tool["qualified_name"] for tool in catalog["tools"]}
    assert "code::search_code" in qualified_names
    assert "docs::search_code" in qualified_names


def test_gateway_db_argument_overrides_config_ledger_path(tmp_path):
    configured_db = tmp_path / "configured.sqlite"
    override_db = tmp_path / "override.sqlite"
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": {"path": str(configured_db)},
                "gateway": {
                    "backends": [
                        {
                            "name": "code",
                            "command": sys.executable,
                            "args": [str(BACKEND), "--label", "code"],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--config",
            str(config_path),
            "--db",
            str(override_db),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_tool_schema",
                    "arguments": {"tool_name": "code::search_code"},
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    assert override_db.exists()
    assert not configured_db.exists()


def test_gateway_accepts_utf8_bom_config_files(tmp_path):
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": {"path": str(tmp_path / "bom.sqlite")},
                "gateway": {
                    "backends": [
                        {
                            "name": "code",
                            "command": sys.executable,
                            "args": [str(BACKEND), "--label", "code"],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8-sig",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        catalog_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_backend_tools", "arguments": {}},
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    catalog = json.loads(catalog_response["result"]["content"][0]["text"])
    assert catalog["tools"][0]["qualified_name"] == "code::search_code"


def test_gateway_legacy_schema_path_does_not_claim_savings(tmp_path):
    proc = start_gateway(tmp_path)
    try:
        schema_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_tool_schema",
                    "arguments": {"tool_name": "backend::search_code"},
                },
            },
        )
        savings_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "show_savings", "arguments": {}},
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    savings = json.loads(savings_response["result"]["content"][0]["text"])
    assert "Legacy ledger writes are disabled" in schema_response["error"]["message"]
    assert savings["receipt_count"] == 0
    assert savings["tokens_saved"] == 0


def test_gateway_legacy_schema_path_creates_no_retrievable_receipt(tmp_path):
    proc = start_gateway(tmp_path)
    try:
        schema_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_tool_schema",
                    "arguments": {"tool_name": "backend::search_code"},
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    assert "Legacy ledger writes are disabled" in schema_response["error"]["message"]


def test_gateway_policy_denies_tools_in_catalog_schema_and_invocation(tmp_path):
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": {"path": str(tmp_path / "policy.sqlite")},
                "gateway": {
                    "tool_policy": {
                        "deny": ["backend::read_symbol"],
                    },
                    "backends": [
                        {
                            "name": "backend",
                            "command": sys.executable,
                            "args": [str(BACKEND)],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        catalog_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_backend_tools", "arguments": {}},
            },
        )
        schema_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_tool_schema",
                    "arguments": {"tool_name": "backend::read_symbol"},
                },
            },
        )
        invoke_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "invoke_tool",
                    "arguments": {
                        "tool_name": "backend::read_symbol",
                        "arguments": {"symbol": "TokenLedger"},
                    },
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    catalog = json.loads(catalog_response["result"]["content"][0]["text"])
    qualified_names = {tool["qualified_name"] for tool in catalog["tools"]}
    assert "backend::search_code" in qualified_names
    assert "backend::read_symbol" not in qualified_names
    assert schema_response["error"]["code"] == -32000
    assert "denied by gateway policy" in schema_response["error"]["message"]
    assert invoke_response["error"]["code"] == -32000
    assert "denied by gateway policy" in invoke_response["error"]["message"]


def test_gateway_policy_allow_list_limits_visible_tools(tmp_path):
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": {"path": str(tmp_path / "allow.sqlite")},
                "gateway": {
                    "tool_policy": {
                        "allow": ["backend::read_symbol"],
                    },
                    "backends": [
                        {
                            "name": "backend",
                            "command": sys.executable,
                            "args": [str(BACKEND)],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        catalog_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_backend_tools", "arguments": {}},
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    catalog = json.loads(catalog_response["result"]["content"][0]["text"])
    assert [tool["qualified_name"] for tool in catalog["tools"]] == ["backend::read_symbol"]


def test_gateway_list_backend_tools_query_filters_catalog(tmp_path):
    proc = start_gateway(tmp_path)
    try:
        response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "list_backend_tools",
                    "arguments": {"query": "semantic"},
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    catalog = json.loads(response["result"]["content"][0]["text"])
    assert [tool["qualified_name"] for tool in catalog["tools"]] == ["backend::search_code"]


def test_backend_uses_own_identity_and_interleaved_notification_is_not_response(tmp_path):
    events = tmp_path / "events.jsonl"
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--mode", "interleaved", "--events", str(events)],
        timeout_seconds=2,
    )
    try:
        backend.initialize()
        result = backend.request("tools/list")
    finally:
        backend.close()

    recorded = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert recorded[0]["method"] == "initialize"
    assert recorded[0]["params"]["clientInfo"] == {
        "name": "token-governance-mcp-gateway",
        "version": __version__,
    }
    assert recorded[0]["params"]["capabilities"] == {}
    assert recorded[1]["method"] == "notifications/initialized"
    assert recorded[2]["method"] == "tools/list"
    assert result["tools"][0]["name"] == "echo"
    assert backend.capabilities == {"tools": {"listChanged": True}}
    assert backend.state is BackendState.CLOSED


def test_incompatible_backend_is_closed_without_blocking_healthy_backend(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--db",
            str(tmp_path / "partial.sqlite"),
            "--backend",
            f"healthy={sys.executable},{STRICT_BACKEND}",
            "--backend",
            f"old={sys.executable},{STRICT_BACKEND},--mode,incompatible",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        initialized = send_raw(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()
        catalog_response = send_raw(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_backend_tools", "arguments": {}},
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=7)

    assert initialized["result"]["backendStatus"]["healthy"] == "active"
    assert initialized["result"]["backendStatus"]["old"] == "closed"
    catalog = json.loads(catalog_response["result"]["content"][0]["text"])
    assert [item["qualified_name"] for item in catalog["tools"]] == ["healthy::echo"]
    assert catalog["unavailable_backends"][0]["backend"] == "old"


def test_timeout_forwards_cancellation_with_backend_request_id(tmp_path):
    events = tmp_path / "timeout-events.jsonl"
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--mode", "timeout", "--events", str(events)],
        timeout_seconds=1,
    )
    try:
        backend.initialize()
        with pytest.raises(TimeoutError, match="timed out"):
            backend.request("tools/call", {"name": "echo", "arguments": {}})
    finally:
        backend.close()

    recorded = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    call = next(item for item in recorded if item.get("method") == "tools/call")
    cancelled = next(item for item in recorded if item.get("method") == "notifications/cancelled")
    assert cancelled["params"]["requestId"] == call["id"]


def test_stderr_flood_is_drained_bounded_and_secret_redacted():
    secret = "ghp_" + "S" * 40
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--mode", "stderr-flood"],
        timeout_seconds=5,
    )
    try:
        backend.initialize()
        result = backend.request("tools/call", {"name": "echo", "arguments": {}})
    finally:
        backend.close()

    assert result["content"][0]["text"] == "backend result"
    assert len(backend.stderr_ring.encode("utf-8")) <= 64 * 1024
    assert secret not in backend.stderr_ring
    assert "-----BEGIN PRIVATE KEY-----" not in backend.stderr_ring
    assert "private-material" not in backend.stderr_ring
    assert "[REDACTED]" in backend.stderr_ring


def test_backend_tool_error_result_is_forwarded_unchanged():
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--mode", "tool-error"],
        timeout_seconds=2,
    )
    try:
        backend.initialize()
        result = backend.request("tools/call", {"name": "echo", "arguments": {}})
    finally:
        backend.close()

    assert result == {
        "content": [{"type": "text", "text": "backend result"}],
        "isError": True,
    }


def test_backend_protocol_error_is_secret_redacted():
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--mode", "error-secret"],
        timeout_seconds=2,
    )
    try:
        backend.initialize()
        with pytest.raises(RuntimeError) as caught:
            backend.request("tools/call", {"name": "echo", "arguments": {}})
    finally:
        backend.close()

    message = str(caught.value)
    assert "ghp_" not in message
    assert "error-private-material" not in message
    assert "[REDACTED]" in message


def test_chunked_multiline_private_key_is_redacted_from_stderr_ring():
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--mode", "pem-chunked"],
        timeout_seconds=2,
    )
    try:
        backend.initialize()
        backend.request("tools/call", {"name": "echo", "arguments": {}})
    finally:
        backend.close()

    assert "chunked-private-material" not in backend.stderr_ring
    assert "-----BEGIN PRIVATE KEY-----" not in backend.stderr_ring


def test_tools_list_changed_invalidates_only_that_backend_catalog(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--db",
            str(tmp_path / "list-change.sqlite"),
            "--backend",
            f"changing={sys.executable},{STRICT_BACKEND},--mode,list-change",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "list_backend_tools", "arguments": {}},
    }
    try:
        first = send(proc, {**request, "id": 1})
        deadline = time.monotonic() + 2
        request_id = 2
        while True:
            second = send(proc, {**request, "id": request_id})
            second_catalog = json.loads(second["result"]["content"][0]["text"])
            if second_catalog["tools"][0]["description"].endswith("generation=1"):
                break
            if time.monotonic() >= deadline:
                pytest.fail("tools/list_changed did not invalidate the cached catalog")
            request_id += 1
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    first_catalog = json.loads(first["result"]["content"][0]["text"])
    assert first_catalog["tools"][0]["description"].endswith("generation=0")
    assert second_catalog["tools"][0]["description"].endswith("generation=1")


def test_shutdown_escalates_from_wait_to_terminate_then_kill():
    calls = []

    class Stdin:
        def close(self):
            calls.append("close-stdin")

    class Process:
        stdin = Stdin()

        def wait(self, timeout):
            calls.append(("wait", timeout))
            if calls.count(("wait", 2)) < 3:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

    backend = StdioMcpBackend(["unused"])
    backend.proc = Process()
    backend.close()

    assert calls == [
        "close-stdin",
        ("wait", 2),
        "terminate",
        ("wait", 2),
        "kill",
        ("wait", 2),
    ]
    assert backend.state is BackendState.CLOSED


def test_multiline_private_key_is_redacted_from_all_diagnostic_text():
    private_key = "-----BEGIN PRIVATE KEY-----\n" + ("very-secret-material\n" * 4) + "-----END PRIVATE KEY-----"
    diagnostic = f"prefix token=ghp_{'X' * 40}\n{private_key}\nsuffix"
    redacted = _redact(diagnostic)
    assert private_key not in redacted
    assert "very-secret-material" not in redacted
    assert "[REDACTED]" in redacted


def test_cancellation_before_backend_id_binding_is_forwarded_after_binding(tmp_path):
    events = tmp_path / "cancel-before-bind.jsonl"
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--events", str(events)],
        timeout_seconds=2,
    )
    gateway = McpGateway(ContextLedger(tmp_path / "cancel.sqlite"), {"backend": backend})
    gateway._upstream_pending[91] = ("", 0)
    entered = __import__("threading").Event()
    release = __import__("threading").Event()
    original_request = backend.request

    def delayed_request(method, params=None, *, request_id_observer=None):
        def delayed_observer(backend_id):
            entered.set()
            release.wait(timeout=2)
            request_id_observer(backend_id)

        return original_request(method, params, request_id_observer=delayed_observer)

    backend.initialize()
    try:
        import threading

        worker = threading.Thread(
            target=lambda: delayed_request(
                "tools/call",
                {"name": "echo", "arguments": {}},
                request_id_observer=lambda backend_id: gateway._bind_backend_request(
                    91, "backend", backend_id
                ),
            )
        )
        worker.start()
        assert entered.wait(timeout=2)
        gateway._cancel_upstream({"requestId": 91, "reason": "user cancelled"})
        release.set()
        worker.join(timeout=3)
    finally:
        backend.close()

    recorded = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    cancelled = next(item for item in recorded if item.get("method") == "notifications/cancelled")
    call = next(item for item in recorded if item.get("method") == "tools/call")
    assert cancelled["params"]["requestId"] == call["id"]


def test_backend_write_serializes_complete_json_rpc_frames():
    import threading
    import time

    class Stdin:
        def __init__(self):
            self.data = bytearray()

        def write(self, value):
            half = max(1, len(value) // 2)
            self.data.extend(value[:half])
            time.sleep(0.001)
            self.data.extend(value[half:])

        def flush(self):
            return None

    class Process:
        stdin = Stdin()

    backend = StdioMcpBackend(["unused"])
    backend.proc = Process()
    threads = [
        threading.Thread(target=backend._write, args=({"jsonrpc": "2.0", "id": index, "method": "x"},))
        for index in range(10)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    frames = [json.loads(frame) for frame in bytes(backend.proc.stdin.data).splitlines()]
    assert {frame["id"] for frame in frames} == set(range(10))


def test_backend_request_before_initialize_is_unavailable_and_repeated_initialize_rejected(tmp_path):
    backend = StdioMcpBackend([sys.executable, str(STRICT_BACKEND)], timeout_seconds=2)
    backend.start()
    with pytest.raises(RuntimeError, match="starting"):
        backend.request("tools/list")
    backend.initialize()
    with pytest.raises(RuntimeError, match="already initialized|unavailable"):
        backend.request("initialize")
    backend.close()


def test_backend_start_failure_is_closed_with_structured_reason(tmp_path):
    backend = StdioMcpBackend([str(tmp_path / "does-not-exist")])
    with pytest.raises((FileNotFoundError, RuntimeError)):
        backend.initialize()
    assert backend.state is BackendState.CLOSED
    assert backend.failure_reason


def test_gateway_start_failure_is_structured_unavailable(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_gateway",
            "--db",
            str(tmp_path / "start-failure.sqlite"),
            "--backend",
            f"healthy={sys.executable},{STRICT_BACKEND}",
            "--backend",
            f"missing={tmp_path / 'not-a-command'}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    try:
        initialized = send_raw(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    assert initialized["result"]["backendStatus"]["missing"] == "closed"
    unavailable = next(
        item for item in initialized["result"]["unavailableBackends"] if item["backend"] == "missing"
    )
    assert unavailable["state"] == "closed"
    assert unavailable["reason"]


def test_run_registers_immediate_cancellation_before_worker_handle(tmp_path):
    import threading

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingGateway(McpGateway):
        called = False

        def handle(self, request):
            if request.get("method") == "tools/call":
                entered.set()
                release.wait(timeout=2)
            result = super().handle(request)
            if request.get("method") == "tools/call":
                finished.set()
            return result

        def _call_tool(self, params):
            self.called = True
            return {"content": []}

    class Input:
        def __iter__(self):
            yield json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "tools/call",
                    "params": {"name": "invoke_tool", "arguments": {}},
                }
            )
            assert entered.wait(timeout=2)
            yield json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 77, "reason": "immediate"},
                }
            )
            release.set()
            assert finished.wait(timeout=2)

    gateway = BlockingGateway(ContextLedger(tmp_path / "immediate.sqlite"), {})
    gateway._state = gateway._state.ACTIVE
    stdout = StringIO()
    gateway.run(Input(), stdout)

    response = json.loads(stdout.getvalue())
    assert response["error"]["message"] == "Request cancelled"
    assert gateway.called is False


def test_cold_catalog_cancellation_prevents_backend_tool_call(tmp_path):
    events = tmp_path / "cold-cancel.jsonl"
    release = tmp_path / "release"
    backend = StdioMcpBackend(
        [
            sys.executable,
            str(STRICT_BACKEND),
            "--mode",
            "slow-list",
            "--events",
            str(events),
            "--release",
            str(release),
        ],
        timeout_seconds=2,
    )
    gateway = McpGateway(ContextLedger(tmp_path / "cold.sqlite"), {"backend": backend})
    gateway._state = gateway._state.ACTIVE
    backend.initialize()
    response = {}

    import threading

    worker = threading.Thread(
        target=lambda: response.update(
            gateway.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 88,
                    "method": "tools/call",
                    "params": {
                        "name": "invoke_tool",
                        "arguments": {"tool_name": "backend::echo", "arguments": {}},
                    },
                }
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if events.exists() and '"method": "tools/list"' in events.read_text(encoding="utf-8"):
            break
    gateway._cancel_upstream({"requestId": 88, "reason": "cold lookup cancelled"})
    release.touch()
    worker.join(timeout=3)
    backend.close()

    recorded = events.read_text(encoding="utf-8")
    assert '"method": "tools/call"' not in recorded
    messages = [json.loads(line) for line in recorded.splitlines()]
    catalog_request = next(item for item in messages if item.get("method") == "tools/list")
    cancelled = next(item for item in messages if item.get("method") == "notifications/cancelled")
    assert cancelled["params"]["requestId"] == catalog_request["id"]
    assert response["error"]["message"] == "Request cancelled"


def test_large_request_timeout_and_close_are_bounded_when_backend_never_reads():
    backend = StdioMcpBackend(
        [sys.executable, str(STRICT_BACKEND), "--mode", "never-read"],
        timeout_seconds=1,
    )
    backend.initialize()
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, ValueError), match="timed out|too large"):
            backend.request(
                "tools/call",
                {"name": "echo", "arguments": {"value": "x" * (8 * 1024 * 1024)}},
            )
    finally:
        backend.close()
    assert time.monotonic() - started < 6
    assert backend.state is BackendState.CLOSED


def test_reinitialize_active_backend_preserves_healthy_process():
    backend = StdioMcpBackend([sys.executable, str(STRICT_BACKEND)], timeout_seconds=2)
    backend.initialize()
    process = backend.proc
    with pytest.raises(RuntimeError, match="already initialized"):
        backend.initialize()
    assert backend.state is BackendState.ACTIVE
    assert backend.proc is process
    assert process is not None and process.poll() is None
    assert backend.request("tools/list")["tools"][0]["name"] == "echo"
    backend.close()


def test_writer_has_finite_queue_and_rejects_oversized_frame():
    backend = StdioMcpBackend([sys.executable, str(STRICT_BACKEND)], timeout_seconds=1)
    assert 0 < backend._write_queue.maxsize <= 8
    backend.MAX_FRAME_BYTES = 1024
    backend.proc = type("Process", (), {"stdin": type("Stdin", (), {})()})()
    with pytest.raises(ValueError, match="frame too large"):
        backend._write({"payload": "x" * 2048})


def test_worker_snapshot_is_safe_when_workers_finish_during_close(tmp_path):
    import threading

    total = 24
    started = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    count = 0

    class StressGateway(McpGateway):
        def handle(self, request):
            nonlocal count
            if request.get("method") == "tools/call":
                with counter_lock:
                    count += 1
                    if count == total:
                        started.set()
                release.wait(timeout=2)
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}

        def close(self):
            release.set()
            return super().close()

    class Input:
        def __iter__(self):
            for index in range(total):
                yield json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {}})
            assert started.wait(timeout=3)

    gateway = StressGateway(ContextLedger(tmp_path / "worker-stress.sqlite"), {})
    gateway._state = gateway._state.ACTIVE
    gateway.run(Input(), StringIO())
    assert not gateway._workers


def test_cancel_uses_reserved_control_capacity_and_preempts_queued_writes():
    import threading

    entered = threading.Event()
    release = threading.Event()

    class Stdin:
        def __init__(self):
            self.frames = []
            self.first = True

        def write(self, value):
            if self.first:
                self.first = False
                entered.set()
                release.wait(timeout=2)
            self.frames.append(value)

        def flush(self):
            return None

    class Process:
        stdin = Stdin()

    backend = StdioMcpBackend(["unused"], timeout_seconds=1)
    backend.proc = Process()
    backend.state = BackendState.ACTIVE
    backend._pending[7] = (threading.Event(), {})
    first = threading.Thread(
        target=backend._write,
        args=({"jsonrpc": "2.0", "id": 1, "method": "ordinary"},),
    )
    first.start()
    assert entered.wait(timeout=1)
    for index in range(backend._write_queue.maxsize):
        backend._write_queue.put_nowait(
            (
                json.dumps({"jsonrpc": "2.0", "id": index + 10, "method": "queued"}).encode()
                + b"\n",
                threading.Event(),
                {},
            )
        )

    started = time.monotonic()
    backend.cancel(7, "urgent")
    assert time.monotonic() - started < 0.5
    assert backend._control_queue.qsize() == 1
    release.set()
    first.join(timeout=2)
    deadline = time.monotonic() + 2
    while len(backend.proc.stdin.frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    backend._writer_stop.set()
    backend._writer.join(timeout=2)

    methods = [json.loads(frame)["method"] for frame in backend.proc.stdin.frames[:2]]
    assert methods == ["ordinary", "notifications/cancelled"]
