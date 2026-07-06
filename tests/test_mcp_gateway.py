import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "tests" / "fixtures" / "fake_mcp_backend.py"


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


def send(proc, request):
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


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


def test_gateway_lists_backend_catalog_and_gets_full_schema(tmp_path):
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
    full_schema = json.loads(schema_response["result"]["content"][0]["text"])
    assert full_schema["name"] == "search_code"
    assert full_schema["inputSchema"]["properties"]["query"]["description"].startswith("The exact semantic")
    assert full_schema["receipt_id"]
    assert full_schema["tokens_saved"] > 0


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


def test_gateway_exposes_savings_and_receipt_inspection(tmp_path):
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
        schema_payload = json.loads(schema_response["result"]["content"][0]["text"])
        receipt_id = schema_payload["receipt_id"]
        savings_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "show_savings", "arguments": {}},
            },
        )
        explain_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "explain_receipt",
                    "arguments": {"receipt_id": receipt_id},
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    savings = json.loads(savings_response["result"]["content"][0]["text"])
    explanation = json.loads(explain_response["result"]["content"][0]["text"])
    assert savings["receipt_count"] == 1
    assert savings["tokens_saved"] == schema_payload["tokens_saved"]
    assert explanation["receipt_id"] == receipt_id
    assert explanation["content_type"] == "mcp_tool_schema"


def test_gateway_retrieves_original_schema_from_receipt(tmp_path):
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
        receipt_id = json.loads(schema_response["result"]["content"][0]["text"])["receipt_id"]
        retrieve_response = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "retrieve_original",
                    "arguments": {"receipt_id": receipt_id},
                },
            },
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    original = json.loads(retrieve_response["result"]["content"][0]["text"])
    assert original["name"] == "search_code"
    assert "inputSchema" in original


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
