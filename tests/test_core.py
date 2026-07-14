from token_governance.contracts import (
    Action,
    GovernanceMode,
    GovernanceRequest,
    ReasonCode,
    SourceKind,
    Strategy,
)
from token_governance.core import GovernanceEngine, create_governance_engine
from token_governance.ledger import ContextLedger


def request(payload: str) -> GovernanceRequest:
    return GovernanceRequest(
        source_kind=SourceKind.CLI,
        tool_name=None,
        tool_input={},
        command_result=None,
        raw_text=payload,
        payload_bytes=len(payload.encode("utf-8")),
        mode=GovernanceMode.MANUAL,
    )


def test_v2_engine_creates_only_transformed_receipts(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    payload = "INFO repeated startup line\n" * 120

    result = create_governance_engine(ledger).govern_request(
        request(payload),
        explicit_strategy=Strategy.REPETITIVE_LOG,
    )

    assert result.action is Action.TRANSFORM
    assert result.receipt_id
    assert ledger.retrieve_original(result.receipt_id) == payload


def test_test_output_keeps_failure_and_summary_lines(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    payload = (
        "============================= test session starts =============================\n"
        "collecting ...\ncollecting ...\ncollecting ...\n"
        "FAILED tests/test_payments.py::test_refund_total\n"
        "AssertionError: expected 42, got 41\n"
        "============================== 1 failed in 0.10s ==============================\n"
    )

    result = create_governance_engine(ledger).govern_request(
        request(payload),
        explicit_strategy=Strategy.TEST_OUTPUT,
    )

    assert result.action is Action.TRANSFORM
    assert "FAILED tests/test_payments.py::test_refund_total" in result.content
    assert "AssertionError: expected 42, got 41" in result.content


def test_small_unique_payload_passes_through_without_receipt(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")

    result = create_governance_engine(ledger).govern_request(
        request("small exact context"),
        explicit_strategy=Strategy.REPETITIVE_LOG,
    )

    assert result.action is Action.PASSTHROUGH
    assert result.receipt_id is None
    assert result.reason_code is ReasonCode.NO_MATCHING_STRATEGY
    assert ledger.stats()["prepared"]["receipt_count"] == 0


def test_legacy_generic_head_tail_path_is_absent():
    assert not hasattr(GovernanceEngine, "govern_context")
    assert not hasattr(GovernanceEngine, "_summarize")
