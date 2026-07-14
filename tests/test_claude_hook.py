from __future__ import annotations

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from token_governance.claude_hook import (
    build_hook_response,
    emit_prepared_response,
    prepare_hook_response,
)
from token_governance.ledger import ContextLedger


def python_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


def complete_install_state(tmp_path: Path) -> Path:
    path = tmp_path / "install-state.json"
    path.write_text(
        json.dumps({"schema_version": 1, "status": "complete"}),
        encoding="utf-8",
    )
    return path


def command_payload(*, tool_name: str = "Bash", stdout: str | None = None):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": "docker logs app"},
        "tool_response": {
            "stdout": stdout or ("ordinary repeated service line\n" * 120),
            "stderr": "warning from the command\n",
            "exitCode": 0,
            "interrupted": False,
            "isImage": False,
            "durationMs": 12,
        },
    }


def test_post_tool_use_preserves_structured_command_semantics(tmp_path):
    ledger = ContextLedger(tmp_path / "hook.sqlite")
    payload = command_payload()

    response = build_hook_response(
        payload,
        ledger=ledger,
        install_state_path=complete_install_state(tmp_path),
    )

    updated = response["hookSpecificOutput"]["updatedToolOutput"]
    assert "[TGL structured output: stdout]" in updated["stdout"]
    assert "[Token Governance Receipt]" in updated["stdout"]
    assert "receipt_id:" in updated["stdout"]
    assert updated["stderr"] == payload["tool_response"]["stderr"]
    assert updated["exitCode"] == 0
    assert updated["interrupted"] is False
    assert updated["isImage"] is False
    assert updated["durationMs"] == 12
    assert "docker logs app" not in json.dumps(response)
    assert ledger.savings()["receipt_count"] == 0
    assert ledger.savings()["prepared"]["receipt_count"] == 1


def test_powershell_uses_the_same_structured_pipeline(tmp_path):
    ledger = ContextLedger(tmp_path / "hook.sqlite")

    response = build_hook_response(
        command_payload(tool_name="PowerShell"),
        ledger=ledger,
        install_state_path=complete_install_state(tmp_path),
    )

    assert "updatedToolOutput" in response["hookSpecificOutput"]


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"schema_version": True, "status": "complete"},
        {"schema_version": 1.0, "status": "complete"},
        {"schema_version": "1", "status": "complete"},
        {"schema_version": 1},
        {"schema_version": 1, "status": True},
        {"schema_version": 1, "status": 1},
        {"schema_version": 1, "status": "installing"},
        {"schema_version": 2, "status": "complete"},
        {"schema_version": 1, "status": "complete", "extra": "field"},
        "malformed-json",
    ],
)
def test_missing_incomplete_or_malformed_install_state_passes_through(tmp_path, state):
    state_path = tmp_path / "install-state.json"
    if isinstance(state, dict):
        state_path.write_text(json.dumps(state), encoding="utf-8")
    elif state is not None:
        state_path.write_text(state, encoding="utf-8")
    ledger = ContextLedger(tmp_path / "hook.sqlite")

    response = build_hook_response(
        command_payload(),
        ledger=ledger,
        install_state_path=state_path,
    )

    assert response == {"continue": True}
    assert ledger.stats()["prepared"]["receipt_count"] == 0


def test_string_or_stderr_only_command_output_passes_through(tmp_path):
    state = complete_install_state(tmp_path)
    ledger = ContextLedger(tmp_path / "hook.sqlite")
    string_payload = command_payload()
    string_payload["tool_response"] = "ordinary repeated service line\n" * 120
    stderr_payload = command_payload(stdout="")
    stderr_payload["tool_response"]["stdout"] = ""

    assert build_hook_response(
        string_payload,
        ledger=ledger,
        install_state_path=state,
    ) == {"continue": True}
    assert build_hook_response(
        stderr_payload,
        ledger=ledger,
        install_state_path=state,
    ) == {"continue": True}
    assert ledger.stats()["prepared"]["receipt_count"] == 0


def test_serialization_failure_discards_candidate_without_receipt(tmp_path):
    ledger = ContextLedger(tmp_path / "hook.sqlite")

    prepared = prepare_hook_response(
        command_payload(),
        ledger=ledger,
        install_state_path=complete_install_state(tmp_path),
        serializer=lambda _value: (_ for _ in ()).throw(TypeError("private")),
    )

    assert prepared.response == {"continue": True}
    assert json.loads(prepared.serialized) == {"continue": True}
    assert prepared.receipt_id is None
    assert ledger.stats()["prepared"]["receipt_count"] == 0


def test_write_failure_leaves_receipt_prepared(tmp_path):
    class FailingStream:
        def write(self, _value):
            raise OSError("closed")

        def flush(self):
            raise AssertionError("flush must not run after write failure")

    ledger = ContextLedger(tmp_path / "hook.sqlite")
    prepared = prepare_hook_response(
        command_payload(),
        ledger=ledger,
        install_state_path=complete_install_state(tmp_path),
    )

    assert emit_prepared_response(prepared, stdout=FailingStream(), ledger=ledger) == 0
    assert ledger.savings()["receipt_count"] == 0
    assert ledger.savings()["prepared"]["receipt_count"] == 1


def test_mark_failure_keeps_emitted_json_valid_and_receipt_prepared(tmp_path, monkeypatch):
    ledger = ContextLedger(tmp_path / "hook.sqlite")
    prepared = prepare_hook_response(
        command_payload(),
        ledger=ledger,
        install_state_path=complete_install_state(tmp_path),
    )
    stdout = StringIO()

    def fail_mark(_receipt_id):
        raise OSError("closed")

    monkeypatch.setattr(ledger, "mark_emitted", fail_mark)

    assert emit_prepared_response(prepared, stdout=stdout, ledger=ledger) == 0
    assert json.loads(stdout.getvalue()) == prepared.response
    assert ledger.savings()["receipt_count"] == 0
    assert ledger.savings()["prepared"]["receipt_count"] == 1


def test_claude_hook_subprocess_emits_then_marks_receipt(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    state_path = complete_install_state(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_governance.claude_hook",
            "--db",
            str(db_path),
            "--install-state",
            str(state_path),
        ],
        input=json.dumps(command_payload()),
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert "updatedToolOutput" in response["hookSpecificOutput"]
    assert proc.stderr == ""
    assert ContextLedger(db_path).savings()["receipt_count"] == 1
    assert ContextLedger(db_path).savings()["prepared"]["receipt_count"] == 0


def test_secret_hook_payload_is_never_persisted_or_logged(tmp_path):
    db_path = tmp_path / "hook.sqlite"
    state_path = complete_install_state(tmp_path)
    secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
    payload = command_payload(stdout=f"Authorization: Bearer {secret}\n")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_governance.claude_hook",
            "--db",
            str(db_path),
            "--install-state",
            str(state_path),
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=python_env(),
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"continue": True}
    assert secret not in proc.stdout
    assert secret not in proc.stderr
    assert ContextLedger(db_path).stats()["prepared"]["receipt_count"] == 0
    for path in tmp_path.iterdir():
        assert secret.encode("utf-8") not in path.read_bytes()


FIXED_SECRET_CORPUS = (
    "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890",
    "github_pat_" + "11AA0_exampleExampleExample123456789",
    "sk-proj-" + "abcdefghijklmnopqrstuvwxyz1234567890",
    "glpat-" + "abcdefghijklmnopqrstuvwxyz123456",
    "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx",
    "sk_live_" + "abcdefghijklmnopqrstuvwxyz1234567890",
    "AIza" + "SyABCDEFGHIJKLMNOPQRSTUVWXYZ123456789",
    "hf_" + "abcdefghijklmnopqrstuvwxyz1234567890",
    "OPENAI_API_KEY=" + "sk-" + "abcdefghijklmnopqrstuvwxyz1234567890",
    "AWS_ACCESS_KEY_ID=" + "AKIA" + "ABCDEFGHIJKLMNOP",
    "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.payload.signature",
    "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
    "password = correct-horse-battery-staple",
)


@pytest.mark.parametrize("position", ["first", "middle", "end"])
@pytest.mark.parametrize("secret", FIXED_SECRET_CORPUS)
def test_fixed_secret_corpus_never_enters_schema_v2_storage_or_hook_output(
    tmp_path,
    capsys,
    secret,
    position,
):
    case_dir = tmp_path / f"case-{FIXED_SECRET_CORPUS.index(secret)}-{position}"
    case_dir.mkdir()
    ledger = ContextLedger(case_dir / "hook.sqlite")
    parts = {
        "first": [secret, "ordinary output", "done"],
        "middle": ["ordinary output", secret, "done"],
        "end": ["ordinary output", "done", secret],
    }[position]
    payload = command_payload(stdout="\n".join(parts))

    prepared = prepare_hook_response(
        payload,
        ledger=ledger,
        install_state_path=complete_install_state(case_dir),
    )
    captured = capsys.readouterr()

    assert prepared.response == {"continue": True}
    assert json.loads(prepared.serialized) == {"continue": True}
    assert prepared.receipt_id is None
    assert ledger.stats()["prepared"]["receipt_count"] == 0
    assert secret not in prepared.serialized
    assert secret not in json.dumps(prepared.response, ensure_ascii=False)
    assert secret not in captured.err
    assert secret not in repr(ledger.risks())
    for suffix in ("", "-journal", "-wal", "-shm"):
        storage = Path(f"{ledger.path}{suffix}")
        if storage.exists():
            assert secret.encode("utf-8") not in storage.read_bytes()


@pytest.mark.parametrize(
    ("field", "value", "matched_value"),
    [
        (
            "stdout",
            "npm token = npm_" + "abcdefghijklmnopqrstuvwxyz123456",
            "npm_" + "abcdefghijklmnopqrstuvwxyz123456",
        ),
        (
            "stderr",
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "dXNlcjpwYXNzd29yZA==",
        ),
        (
            "tool_input",
            {"env": {"ANTHROPIC_API_KEY": "structured-sensitive-value"}},
            "structured-sensitive-value",
        ),
        (
            "tool_input",
            {"nested": ["ok", {"password": "nested-sensitive-value"}]},
            "nested-sensitive-value",
        ),
    ],
)
def test_structured_secret_formats_never_enter_schema_v2_storage(
    tmp_path,
    capsys,
    field,
    value,
    matched_value,
):
    case_dir = tmp_path / field
    case_dir.mkdir(exist_ok=True)
    ledger = ContextLedger(case_dir / "hook.sqlite")
    payload = command_payload()
    if field == "tool_input":
        payload["tool_input"] = value
    else:
        payload["tool_response"][field] = value

    prepared = prepare_hook_response(
        payload,
        ledger=ledger,
        install_state_path=complete_install_state(case_dir),
    )
    captured = capsys.readouterr()

    assert prepared.response == {"continue": True}
    assert prepared.receipt_id is None
    assert ledger.stats()["prepared"]["receipt_count"] == 0
    assert matched_value not in prepared.serialized
    assert matched_value not in captured.err
    assert matched_value not in repr(ledger.risks())
    for suffix in ("", "-journal", "-wal", "-shm"):
        storage = Path(f"{ledger.path}{suffix}")
        if storage.exists():
            assert matched_value.encode("utf-8") not in storage.read_bytes()
