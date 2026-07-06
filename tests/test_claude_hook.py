import json
import os
import subprocess
import sys
from pathlib import Path

from token_governance.claude_hook import build_hook_response
from token_governance.ledger import ContextLedger


def python_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


def test_post_tool_use_replaces_long_tool_output_with_governed_output(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_output": "\n".join(f"INFO repeated build line {i % 4}" for i in range(120)),
    }

    response = build_hook_response(payload, ledger=ContextLedger(db_path))

    assert response["continue"] is True
    hook_output = response["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PostToolUse"
    assert "updatedToolOutput" in hook_output
    assert "Token Governance Summary" in hook_output["updatedToolOutput"]
    assert "receipt_id:" in hook_output["updatedToolOutput"]


def test_post_tool_use_passthrough_when_governance_does_not_save_tokens(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    original = "small exact output"
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_output": original,
    }

    response = build_hook_response(payload, ledger=ContextLedger(db_path))

    assert response == {"continue": True}


def test_post_tool_use_accepts_tool_response_alias(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Grep",
        "tool_response": "\n".join(f"match line {i % 3}" for i in range(120)),
    }

    response = build_hook_response(payload, ledger=ContextLedger(db_path))

    assert response["hookSpecificOutput"]["updatedToolOutput"]


def test_post_tool_use_extracts_stdout_from_tool_response_object(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "PowerShell",
        "tool_response": {
            "stdout": "\n".join("HOOK_AUTO_LINE repeated token governance smoke" for _ in range(180)),
            "stderr": "",
            "interrupted": False,
        },
    }

    response = build_hook_response(payload, ledger=ContextLedger(db_path))

    updated = response["hookSpecificOutput"]["updatedToolOutput"]
    assert "Token Governance Summary" in updated
    assert "receipt_id:" in updated


def test_non_post_tool_use_event_is_ignored(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
    }

    response = build_hook_response(payload, ledger=ContextLedger(db_path))

    assert response == {"continue": True}


def test_claude_hook_cli_reads_json_from_stdin(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_output": "\n".join(f"INFO repeated test line {i % 2}" for i in range(100)),
    }

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_governance.claude_hook",
            "--db",
            str(db_path),
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["continue"] is True
    assert "updatedToolOutput" in response["hookSpecificOutput"]
