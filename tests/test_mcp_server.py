import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def python_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


def test_mcp_server_lists_governance_tools(tmp_path):
    db_path = tmp_path / "mcp.sqlite"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }

    proc = subprocess.run(
        [sys.executable, "-m", "token_governance.mcp_server", "--db", str(db_path)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    tool_names = {tool["name"] for tool in response["result"]["tools"]}

    assert {
        "govern_context",
        "retrieve_original",
        "explain_receipt",
        "show_savings",
        "list_context_risks",
    }.issubset(tool_names)


def test_mcp_server_accepts_utf8_bom_prefixed_json_lines(tmp_path):
    db_path = tmp_path / "mcp.sqlite"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }

    proc = subprocess.run(
        [sys.executable, "-m", "token_governance.mcp_server", "--db", str(db_path)],
        input="\ufeff" + json.dumps(request) + "\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["result"]["tools"]


def test_mcp_server_does_not_respond_to_notifications(tmp_path):
    db_path = tmp_path / "mcp.sqlite"
    request = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }

    proc = subprocess.run(
        [sys.executable, "-m", "token_governance.mcp_server", "--db", str(db_path)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize(
    ("strategy", "payload"),
    [
        ("repetitive_log", "ordinary repeated mcp line\n" * 120),
        (
            "test_output",
            "============================= test session starts =============================\n"
            "collecting ...\ncollecting ...\ncollecting ...\n"
            "============================== 3 passed in 0.10s ==============================\n",
        ),
        (
            "build_output",
            "[1/3] compiling a.cc\n[2/3] compiling b.cc\n[3/3] compiling c.cc\n"
            "build succeeded\n",
        ),
    ],
)
def test_mcp_govern_context_and_retrieve_original(tmp_path, strategy, payload):
    db_path = tmp_path / "mcp.sqlite"
    govern_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "govern_context",
            "arguments": {
                "payload": payload,
                "strategy": strategy,
            },
        },
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "token_governance.mcp_server", "--db", str(db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=python_env(),
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(govern_request) + "\n")
    proc.stdin.flush()
    govern_response = json.loads(proc.stdout.readline())
    receipt_id = json.loads(govern_response["result"]["content"][0]["text"])["receipt_id"]

    retrieve_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "retrieve_original",
            "arguments": {"receipt_id": receipt_id},
        },
    }
    proc.stdin.write(json.dumps(retrieve_request) + "\n")
    proc.stdin.close()
    retrieve_response = json.loads(proc.stdout.readline())
    stderr = proc.stderr.read() if proc.stderr else ""
    exit_code = proc.wait(timeout=5)

    assert exit_code == 0, stderr
    assert retrieve_response["result"]["content"][0]["text"] == payload


def test_mcp_govern_schema_exposes_only_closed_strategy_enum(tmp_path):
    db_path = tmp_path / "mcp.sqlite"
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    proc = subprocess.run(
        [sys.executable, "-m", "token_governance.mcp_server", "--db", str(db_path)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    tools = proc.stdout and json.loads(proc.stdout)["result"]["tools"]
    govern = next(tool for tool in tools if tool["name"] == "govern_context")
    properties = govern["inputSchema"]["properties"]
    assert set(properties) == {"payload", "strategy"}
    assert properties["strategy"]["enum"] == [
        "auto",
        "repetitive_log",
        "test_output",
        "build_output",
    ]


def test_mcp_rejects_legacy_content_type_with_migration_guidance(tmp_path):
    db_path = tmp_path / "mcp.sqlite"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "govern_context",
            "arguments": {"payload": "value", "content_type": "log"},
        },
    }

    proc = subprocess.run(
        [sys.executable, "-m", "token_governance.mcp_server", "--db", str(db_path)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    response = json.loads(proc.stdout)
    assert "content_type/source were removed; use strategy" in response["error"]["message"]


def test_mcp_config_ledger_path_is_not_overridden_by_an_implicit_default(tmp_path):
    db_path = tmp_path / "configured.sqlite"
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps({"ledger": {"path": str(db_path)}}),
        encoding="utf-8",
    )
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    proc = subprocess.run(
        [sys.executable, "-m", "token_governance.mcp_server", "--config", str(config_path)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0, proc.stderr
    assert db_path.exists()
