from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from token_governance.config import (
    GatewayConfig,
    GatewayToolPolicyConfig,
    GovernanceConfig,
    LedgerConfig,
    PolicyConfig,
)
from token_governance.contracts import (
    Action,
    CommandResult,
    GovernanceMode,
    GovernanceRequest,
    PersistenceMode,
    ProtectedContentBehavior,
    ReasonCode,
    Risk,
    SourceKind,
    Strategy,
    VerificationResult,
)
from token_governance.core import GovernanceEngine
from token_governance.claude_hook import build_hook_response
from token_governance.ledger import ContextLedger
from token_governance.policy import PolicyEngine
from token_governance.shapers import normalize_request_output


def make_config(path: Path) -> GovernanceConfig:
    return GovernanceConfig(
        ledger=LedgerConfig(path=path, retention_days=30),
        policy=PolicyConfig(
            enabled_strategies=(
                Strategy.TEST_OUTPUT,
                Strategy.BUILD_OUTPUT,
                Strategy.REPETITIVE_LOG,
            ),
            protected_content_behavior=ProtectedContentBehavior.PASSTHROUGH,
            persistence_mode=PersistenceMode.TRANSFORMED_ONLY,
            max_payload_bytes=2 * 1024 * 1024,
            max_stored_original_bytes=1024 * 1024,
            hook_deadline_ms=2000,
            literal_secret_markers=(),
        ),
        gateway=GatewayConfig(
            request_timeout_seconds=10,
            backends=(),
            tool_policy=GatewayToolPolicyConfig(allow=(), deny=()),
        ),
        config_path=path.parent / "token-governance.config.json",
    )


def manual_request(
    payload: str,
    *,
    source_kind: SourceKind = SourceKind.CLI,
) -> GovernanceRequest:
    return GovernanceRequest(
        source_kind=source_kind,
        tool_name=None,
        tool_input={},
        command_result=None,
        raw_text=payload,
        payload_bytes=len(payload.encode("utf-8")),
        mode=GovernanceMode.MANUAL,
    )


def make_engine(ledger) -> GovernanceEngine:
    return GovernanceEngine(
        ledger=ledger,
        policy=PolicyEngine(),
        config=make_config(Path(getattr(ledger, "path", "unused.sqlite"))),
        events=ledger,
        clock=lambda: 0.0,
    )


def repetitive_payload() -> str:
    return "ordinary repeated log line\n" * 120


def test_engine_transforms_verifies_and_stores_only_prepared_receipt(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")

    result = make_engine(ledger).govern_request(
        manual_request(repetitive_payload()),
        explicit_strategy=Strategy.REPETITIVE_LOG,
    )

    assert result.action is Action.TRANSFORM
    assert result.reason_code is ReasonCode.TRANSFORMED
    assert result.risk is Risk.MEDIUM
    assert result.preservation_check is not None
    assert result.preservation_check.ok is True
    assert result.receipt_id
    assert result.tokens_saved > 0
    assert ledger.retrieve_original(result.receipt_id) == repetitive_payload()
    assert ledger.explain_receipt(result.receipt_id)["delivery_state"] == "prepared"
    assert ledger.savings()["receipt_count"] == 0
    assert ledger.savings()["prepared"]["receipt_count"] == 1


def test_engine_passthrough_and_secret_create_no_receipt(tmp_path):
    marker = "literal-private-marker-value"
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    config = replace(
        make_config(ledger.path),
        policy=replace(
            make_config(ledger.path).policy,
            literal_secret_markers=(marker,),
        ),
    )
    engine = GovernanceEngine(
        ledger=ledger,
        policy=PolicyEngine(),
        config=config,
        events=ledger,
        clock=lambda: 0.0,
    )

    small = engine.govern_request(manual_request("one\ntwo\n"))
    secret = engine.govern_request(manual_request(f"value={marker}"))

    assert small.action is Action.PASSTHROUGH
    assert small.receipt_id is None
    assert secret.reason_code is ReasonCode.SECRET_DETECTED
    assert secret.receipt_id is None
    assert ledger.stats()["prepared"]["receipt_count"] == 0


def test_engine_fails_open_when_independent_verification_rejects_candidate(tmp_path):
    class RejectingVerifier:
        def verify(self, strategy, original, result):
            return VerificationResult(
                ok=False,
                protected_fact_count=1,
                missing_fact_count=1,
                reason_code=ReasonCode.PRESERVATION_FAILED,
            )

    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    engine = make_engine(ledger)
    engine.verifier = RejectingVerifier()

    result = engine.govern_request(
        manual_request(repetitive_payload()),
        explicit_strategy=Strategy.REPETITIVE_LOG,
    )

    assert result.action is Action.PASSTHROUGH
    assert result.content == repetitive_payload()
    assert result.reason_code is ReasonCode.PRESERVATION_FAILED
    assert result.receipt_id is None
    assert ledger.stats()["prepared"]["receipt_count"] == 0


@pytest.mark.parametrize("failure", ["allocate", "store"])
def test_engine_ledger_failures_discard_candidate_and_claim_no_savings(failure):
    class FailingLedger:
        path = Path("unused.sqlite")

        def allocate_receipt_id(self):
            if failure == "allocate":
                raise OSError("private payload must not escape")
            return "tgl_test_receipt"

        def store_prepared(self, receipt_id, original_text, result):
            if failure == "store":
                raise OSError("private payload must not escape")

        def record_event(self, *args):
            pass

    result = make_engine(FailingLedger()).govern_request(
        manual_request(repetitive_payload()),
        explicit_strategy=Strategy.REPETITIVE_LOG,
    )

    assert result.action is Action.PASSTHROUGH
    assert result.reason_code is ReasonCode.LEDGER_FAILED
    assert result.content == repetitive_payload()
    assert result.receipt_id is None
    assert result.tokens_saved == 0
    assert "private payload" not in repr(result)


def test_manual_mcp_uses_owner_pipeline_and_records_real_source(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")

    result = make_engine(ledger).govern_request(
        manual_request(repetitive_payload(), source_kind=SourceKind.MCP),
        explicit_strategy=Strategy.REPETITIVE_LOG,
    )

    assert result.action is Action.TRANSFORM
    risks = ledger.risks()
    assert risks[0]["source_kind"] == SourceKind.MCP.value
    assert risks[0]["receipt_id"] == result.receipt_id


def test_legacy_generic_governance_api_is_removed():
    assert not hasattr(GovernanceEngine, "govern_context")
    assert not hasattr(GovernanceEngine, "_summarize")


def subprocess_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


@pytest.mark.parametrize(
    ("strategy", "command", "stdout"),
    [
        (
            "repetitive_log",
            "docker logs app",
            "adapter equivalent repeated line\n" * 120,
        ),
        (
            "test_output",
            "pytest -q",
            "============================= test session starts =============================\n"
            "collecting ...\ncollecting ...\ncollecting ...\n"
            "============================== 3 passed in 0.10s ==============================\n",
        ),
        (
            "build_output",
            "cmake --build build",
            "[1/3] compiling a.cc\n[2/3] compiling b.cc\n[3/3] compiling c.cc\n"
            "build succeeded\n",
        ),
    ],
)
def test_hook_cli_and_mcp_share_engine_owned_governance_truth(
    tmp_path,
    strategy,
    command,
    stdout,
):
    hook_db = tmp_path / "hook.sqlite"
    cli_db = tmp_path / "cli.sqlite"
    mcp_db = tmp_path / "mcp.sqlite"
    state_path = tmp_path / "install-state.json"
    state_path.write_text(
        json.dumps({"schema_version": 1, "status": "complete"}),
        encoding="utf-8",
    )
    hook_request = GovernanceRequest(
        source_kind=SourceKind.CLAUDE_HOOK,
        tool_name="Bash",
        tool_input={"command": command},
        command_result=CommandResult(stdout, "", 0, False),
        raw_text=stdout,
        payload_bytes=len(stdout.encode("utf-8")),
        mode=GovernanceMode.AUTO,
    )
    normalized = normalize_request_output(hook_request)
    hook_payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "exitCode": 0,
            "interrupted": False,
            "isImage": False,
        },
    }
    hook_ledger = ContextLedger(hook_db)

    hook_response = build_hook_response(
        hook_payload,
        ledger=hook_ledger,
        install_state_path=state_path,
    )
    hook_stdout = hook_response["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
    hook_content, _, receipt_block = hook_stdout.partition(
        "\n\n[Token Governance Receipt]\n"
    )
    hook_receipt = next(
        line.removeprefix("receipt_id: ")
        for line in receipt_block.splitlines()
        if line.startswith("receipt_id: ")
    )
    hook_truth = hook_ledger.explain_receipt(hook_receipt)

    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_governance.cli",
            "--db",
            str(cli_db),
            "govern",
            "--strategy",
            strategy,
        ],
        input=normalized,
        text=True,
        capture_output=True,
        check=False,
        env=subprocess_env(),
    )
    assert cli.returncode == 0, cli.stderr
    cli_truth = json.loads(cli.stdout)

    mcp_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "govern_context",
            "arguments": {"payload": normalized, "strategy": strategy},
        },
    }
    mcp = subprocess.run(
        [sys.executable, "-m", "token_governance.mcp_server", "--db", str(mcp_db)],
        input=json.dumps(mcp_request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=subprocess_env(),
    )
    assert mcp.returncode == 0, mcp.stderr
    mcp_envelope = json.loads(mcp.stdout)
    mcp_truth = json.loads(mcp_envelope["result"]["content"][0]["text"])

    assert hook_content == cli_truth["content"] == mcp_truth["content"]
    for field in (
        "action",
        "strategy",
        "reason_code",
        "confidence",
        "token_before",
        "token_after",
        "tokens_saved",
        "preservation_check",
    ):
        assert hook_truth[field] == cli_truth[field] == mcp_truth[field]
    assert hook_truth["risk"] == "low"
    assert cli_truth["risk"] == mcp_truth["risk"] == "medium"

    def event_source(db_path):
        with sqlite3.connect(db_path) as connection:
            return connection.execute(
                "SELECT source_kind FROM governance_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0]

    assert event_source(hook_db) == "claude_hook"
    assert event_source(cli_db) == "cli"
    assert event_source(mcp_db) == "mcp"
