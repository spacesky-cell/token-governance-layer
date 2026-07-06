from token_governance.core import GovernanceEngine
from token_governance.ledger import ContextLedger
from token_governance.policy import PolicyEngine


def test_govern_context_creates_receipt_and_restores_original(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    engine = GovernanceEngine(ledger=ledger, policy=PolicyEngine())

    payload = "\n".join(f"INFO repeated startup line {i % 3}" for i in range(80))
    result = engine.govern_context(payload, content_type="log", source="pytest")

    assert result["receipt_id"]
    assert result["token_after"] < result["token_before"]
    assert result["risk"] == "low"
    assert ledger.retrieve_original(result["receipt_id"]) == payload


def test_error_output_preserves_failure_lines(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    engine = GovernanceEngine(ledger=ledger, policy=PolicyEngine())
    payload = "\n".join(
        [
            "INFO setup ok",
            "FAILED tests/test_payments.py::test_refund_total",
            "AssertionError: expected 42, got 41",
            "at app/payments.py:88",
        ]
        + [f"INFO noisy retry {i}" for i in range(100)]
    )

    result = engine.govern_context(payload, content_type="test_output", source="pytest")
    governed = result["content"]

    assert "FAILED tests/test_payments.py::test_refund_total" in governed
    assert "AssertionError: expected 42, got 41" in governed
    assert "app/payments.py:88" in governed
    assert ledger.retrieve_original(result["receipt_id"]) == payload


def test_short_payload_is_not_compressed_but_still_receipted(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    engine = GovernanceEngine(ledger=ledger, policy=PolicyEngine())

    result = engine.govern_context("small exact context", content_type="text", source="unit")

    assert result["content"] == "small exact context"
    assert result["action"] == "passthrough"
    assert result["token_after"] == result["token_before"]
    assert ledger.explain_receipt(result["receipt_id"])["action"] == "passthrough"


def test_summary_falls_back_to_passthrough_when_it_would_increase_tokens(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    engine = GovernanceEngine(ledger=ledger, policy=PolicyEngine())
    payload = "\n".join(
        [
            "INFO booting dev server",
            "ERROR tests/test_core.py:42 AssertionError: expected receipt_id to be present",
            "Traceback (most recent call last):",
            '  File "tests/test_core.py", line 42, in test_receipt',
            '    assert result["receipt_id"]',
        ]
        + ["Large repeated context follows." for _ in range(10)]
    )

    result = engine.govern_context(payload, content_type="log", source="pytest")

    assert result["content"] == payload
    assert result["action"] == "passthrough"
    assert result["token_after"] == result["token_before"]
    assert result["tokens_saved"] == 0
    assert "Summary was not shorter than the original payload." in result["notes"]
    assert ledger.retrieve_original(result["receipt_id"]) == payload
