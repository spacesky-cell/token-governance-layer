import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


PROTOCOL_VERSION = "2025-06-18"
ROOT = Path(__file__).resolve().parents[1]


def python_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def initialize_request(request_id=1, protocol_version=PROTOCOL_VERSION):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "tgl-test", "version": "1.0.0"},
        },
    }


def initialized_notification():
    return {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }


def run_session(tmp_path, messages, *, db_path=None, args=None, raw_input=None):
    path = db_path or tmp_path / "mcp.sqlite"
    command = [
        sys.executable,
        "-m",
        "token_governance.mcp_server",
        "--db",
        str(path),
        *(args or []),
    ]
    input_text = raw_input
    if input_text is None:
        input_text = "".join(json.dumps(message) + "\n" for message in messages)
    proc = subprocess.run(
        command,
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env=python_env(),
    )
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    return proc, responses


def active_messages(*requests):
    return [initialize_request(), initialized_notification(), *requests]


def start_server(tmp_path, *, db_path=None):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_server",
            "--db",
            str(db_path or tmp_path / "mcp.sqlite"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=python_env(),
    )


def send_request(proc, request):
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def send_notification(proc, notification):
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(notification) + "\n")
    proc.stdin.flush()


def activate(proc):
    response = send_request(proc, initialize_request())
    assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
    send_notification(proc, initialized_notification())


def stop_server(proc):
    assert proc.stdin is not None
    proc.stdin.close()
    stderr = proc.stderr.read() if proc.stderr else ""
    assert proc.wait(timeout=5) == 0, stderr


def govern_payload(tmp_path, db_path, payload):
    proc = start_server(tmp_path, db_path=db_path)
    try:
        activate(proc)
        response = send_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "govern_context",
                    "arguments": {
                        "payload": payload,
                        "strategy": "repetitive_log",
                    },
                },
            },
        )
    finally:
        stop_server(proc)
    result = json.loads(response["result"]["content"][0]["text"])
    assert result["receipt_id"]
    return result["receipt_id"]


def test_requests_before_initialize_fail_structurally(tmp_path):
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}

    proc, responses = run_session(tmp_path, [request])

    assert proc.returncode == 0, proc.stderr
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {"code": -32002, "message": "Server not initialized"},
        }
    ]


def test_initialize_negotiates_supported_version_and_requires_notification(tmp_path):
    messages = [
        initialize_request(1),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        initialized_notification(),
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    ]

    proc, responses = run_session(tmp_path, messages)

    assert proc.returncode == 0, proc.stderr
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[0]["result"] == {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {"name": "token-governance-layer", "version": "0.1.0"},
        "capabilities": {"tools": {}},
    }
    assert responses[1]["error"] == {
        "code": -32002,
        "message": "Server not initialized",
    }
    assert responses[2]["result"]["tools"]


def test_initialize_rejects_an_incompatible_protocol_version(tmp_path):
    proc, responses = run_session(
        tmp_path,
        [initialize_request(protocol_version="2024-11-05")],
    )

    assert proc.returncode == 0, proc.stderr
    assert responses[0]["error"] == {
        "code": -32602,
        "message": f"Unsupported protocol version; supported: {PROTOCOL_VERSION}",
    }


def test_repeated_initialize_is_an_invalid_request(tmp_path):
    proc, responses = run_session(
        tmp_path,
        [initialize_request(1), initialize_request(2)],
    )

    assert proc.returncode == 0, proc.stderr
    assert responses[1]["error"] == {
        "code": -32600,
        "message": "Server is already initialized",
    }


def test_all_notifications_are_silent_and_only_initialized_activates(tmp_path):
    messages = [
        initialize_request(),
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 99, "reason": "no longer needed"},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/unsupported",
            "params": {"private": "must not echo"},
        },
        initialized_notification(),
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
    ]

    proc, responses = run_session(tmp_path, messages)

    assert proc.returncode == 0, proc.stderr
    assert [response["id"] for response in responses] == [1, 4]
    assert responses[1]["result"]["tools"]


def test_malformed_initialized_notification_is_silent_but_does_not_activate(tmp_path):
    messages = [
        initialize_request(),
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {"unexpected": True},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        initialized_notification(),
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
    ]

    proc, responses = run_session(tmp_path, messages)

    assert proc.returncode == 0, proc.stderr
    assert [response["id"] for response in responses] == [1, 4, 5]
    assert responses[1]["error"] == {
        "code": -32002,
        "message": "Server not initialized",
    }
    assert responses[2]["result"]["tools"]


def test_malformed_json_and_invalid_envelopes_return_protocol_errors(tmp_path):
    raw_input = "not-json\n[]\n" + json.dumps(
        {"jsonrpc": "1.0", "id": 8, "method": "initialize", "params": {}}
    ) + "\n" + json.dumps(
        {
            "jsonrpc": "2.0",
            "id": {"invalid": True},
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "invalid-id", "version": "1.0.0"},
            },
        }
    ) + "\n"

    proc, responses = run_session(tmp_path, [], raw_input=raw_input)

    assert proc.returncode == 0, proc.stderr
    assert [response["error"]["code"] for response in responses] == [
        -32700,
        -32600,
        -32600,
        -32600,
    ]
    assert [response["id"] for response in responses] == [None, None, 8, None]
    assert "not-json" not in proc.stdout


def test_invalid_utf8_is_a_single_safe_parse_error(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_server",
            "--db",
            str(tmp_path / "mcp.sqlite"),
        ],
        input=b"\xff\n",
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0
    assert proc.stdout.splitlines() == [
        b'{"jsonrpc": "2.0", "id": null, "error": '
        b'{"code": -32700, "message": "Parse error"}}'
    ]
    assert b"Traceback" not in proc.stderr
    assert b"\\xff" not in proc.stderr


def test_mcp_server_accepts_utf8_bom_prefixed_json_lines(tmp_path):
    messages = active_messages(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    raw_input = "\ufeff" + "".join(json.dumps(message) + "\n" for message in messages)

    proc, responses = run_session(tmp_path, [], raw_input=raw_input)

    assert proc.returncode == 0, proc.stderr
    assert responses[-1]["result"]["tools"]


def test_tool_schemas_are_closed_and_retrieval_is_bounded(tmp_path):
    request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    proc, responses = run_session(tmp_path, active_messages(request))

    assert proc.returncode == 0, proc.stderr
    tools = responses[-1]["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "govern_context",
        "retrieve_original",
        "explain_receipt",
        "show_savings",
        "list_context_risks",
    }
    for tool in tools:
        assert tool["inputSchema"]["additionalProperties"] is False
    retrieve = next(tool for tool in tools if tool["name"] == "retrieve_original")
    assert retrieve["inputSchema"]["properties"] == {
        "receipt_id": {"type": "string", "minLength": 1},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "max_chars": {
            "type": "integer",
            "minimum": 1,
            "maximum": 16384,
            "default": 4096,
        },
    }


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
def test_mcp_govern_context_and_paginated_retrieve(tmp_path, strategy, payload):
    db_path = tmp_path / "mcp.sqlite"
    proc = start_server(tmp_path, db_path=db_path)
    try:
        activate(proc)
        govern_response = send_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "govern_context",
                    "arguments": {"payload": payload, "strategy": strategy},
                },
            },
        )
        receipt_id = json.loads(
            govern_response["result"]["content"][0]["text"]
        )["receipt_id"]
        retrieve_response = send_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "retrieve_original",
                    "arguments": {
                        "receipt_id": receipt_id,
                        "offset": 3,
                        "max_chars": 17,
                    },
                },
            },
        )
    finally:
        stop_server(proc)

    page = json.loads(retrieve_response["result"]["content"][0]["text"])
    assert page == {
        "receipt_id": receipt_id,
        "text": payload[3:20],
        "offset": 3,
        "max_chars": 17,
        "returned_chars": 17,
        "total_chars": len(payload),
        "next_offset": 20,
        "remaining_chars": len(payload) - 20,
        "truncated": True,
    }


def test_retrieve_defaults_to_a_bounded_page_and_finishes_with_null_offset(tmp_path):
    db_path = tmp_path / "mcp.sqlite"
    payload = "x\n" * 2050 + "END"
    receipt_id = govern_payload(tmp_path, db_path, payload)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "retrieve_original",
                "arguments": {"receipt_id": receipt_id},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "retrieve_original",
                "arguments": {"receipt_id": receipt_id, "offset": 4096},
            },
        },
    ]

    proc, responses = run_session(
        tmp_path,
        active_messages(*requests),
        db_path=db_path,
    )

    assert proc.returncode == 0, proc.stderr
    first = json.loads(responses[-2]["result"]["content"][0]["text"])
    final = json.loads(responses[-1]["result"]["content"][0]["text"])
    assert len(first["text"]) == 4096
    assert first["next_offset"] == 4096
    assert first["truncated"] is True
    assert final["text"] == payload[4096:]
    assert final["next_offset"] is None
    assert final["remaining_chars"] == 0
    assert final["truncated"] is False


def test_unbounded_restore_argument_returns_migration_guidance_as_tool_error(tmp_path):
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "retrieve_original",
            "arguments": {"receipt_id": "tgl_value", "full": True},
        },
    }

    proc, responses = run_session(tmp_path, active_messages(request))

    assert proc.returncode == 0, proc.stderr
    result = responses[-1]["result"]
    assert result["isError"] is True
    error = json.loads(result["content"][0]["text"])
    assert error == {
        "code": "invalid_arguments",
        "message": (
            "Unbounded MCP retrieval was removed; use offset/max_chars pagination "
            "or the CLI receipt export for an exact full original"
        ),
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"receipt_id": ""},
        {"receipt_id": "tgl_value", "offset": -1},
        {"receipt_id": "tgl_value", "offset": True},
        {"receipt_id": "tgl_value", "max_chars": 0},
        {"receipt_id": "tgl_value", "max_chars": 16385},
        {"receipt_id": "tgl_value", "max_chars": "10"},
    ],
)
def test_retrieve_rejects_malformed_arguments_as_tool_errors(tmp_path, arguments):
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "retrieve_original", "arguments": arguments},
    }

    proc, responses = run_session(tmp_path, active_messages(request))

    assert proc.returncode == 0, proc.stderr
    result = responses[-1]["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["code"] == "invalid_arguments"


def test_hash_mismatch_is_a_safe_structured_tool_error(tmp_path):
    db_path = tmp_path / "mcp.sqlite"
    receipt_id = govern_payload(tmp_path, db_path, "private original\n" * 120)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE receipts SET original_text = ? WHERE receipt_id = ?",
            ("tampered secret payload", receipt_id),
        )
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "retrieve_original",
            "arguments": {"receipt_id": receipt_id},
        },
    }

    proc, responses = run_session(
        tmp_path,
        active_messages(request),
        db_path=db_path,
    )

    assert proc.returncode == 0, proc.stderr
    result = responses[-1]["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"]) == {
        "code": "ledger_integrity_failed",
        "message": "Receipt integrity check failed",
    }
    assert "private" not in proc.stdout
    assert "tampered" not in proc.stdout


def test_tool_execution_errors_do_not_become_protocol_errors_or_leak_exceptions(tmp_path):
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "retrieve_original",
            "arguments": {"receipt_id": "tgl_missing"},
        },
    }

    proc, responses = run_session(tmp_path, active_messages(request))

    assert proc.returncode == 0, proc.stderr
    assert "error" not in responses[-1]
    result = responses[-1]["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"]) == {
        "code": "receipt_not_found",
        "message": "Receipt not found",
    }
    assert "tgl_missing" not in proc.stdout


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"name": "retrieve_original", "arguments": []},
        {"name": "unknown", "arguments": {}},
        {"name": "show_savings", "arguments": {}, "extra": True},
    ],
)
def test_malformed_tool_calls_return_invalid_params_protocol_errors(tmp_path, params):
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": params,
    }

    proc, responses = run_session(tmp_path, active_messages(request))

    assert proc.returncode == 0, proc.stderr
    assert responses[-1]["error"]["code"] == -32602
    assert "Traceback" not in proc.stdout + proc.stderr


def test_govern_schema_exposes_only_closed_strategy_enum(tmp_path):
    request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    _, responses = run_session(tmp_path, active_messages(request))

    govern = next(
        tool
        for tool in responses[-1]["result"]["tools"]
        if tool["name"] == "govern_context"
    )
    properties = govern["inputSchema"]["properties"]
    assert set(properties) == {"payload", "strategy"}
    assert govern["inputSchema"]["additionalProperties"] is False
    assert properties["strategy"]["enum"] == [
        "auto",
        "repetitive_log",
        "test_output",
        "build_output",
    ]


def test_legacy_govern_argument_returns_migration_guidance_as_tool_error(tmp_path):
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "govern_context",
            "arguments": {"payload": "value", "content_type": "log"},
        },
    }

    _, responses = run_session(tmp_path, active_messages(request))

    result = responses[-1]["result"]
    assert result["isError"] is True
    assert "content_type/source were removed; use strategy" in json.loads(
        result["content"][0]["text"]
    )["message"]


def test_mcp_config_ledger_path_is_not_overridden_by_an_implicit_default(tmp_path):
    db_path = tmp_path / "configured.sqlite"
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps({"ledger": {"path": str(db_path)}}),
        encoding="utf-8",
    )
    messages = active_messages(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    input_text = "".join(json.dumps(message) + "\n" for message in messages)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_governance.mcp_server",
            "--config",
            str(config_path),
        ],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0, proc.stderr
    assert db_path.exists()
