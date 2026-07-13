from dataclasses import FrozenInstanceError, fields

import pytest

import token_governance.contracts as contracts
from token_governance.contracts import (
    Action,
    CommandResult,
    Confidence,
    EventPort,
    GovernanceMode,
    GovernanceRequest,
    GovernanceResult,
    LedgerPort,
    PolicyDecision,
    ReasonCode,
    Risk,
    ShaperDiagnostic,
    ShaperDiagnosticCode,
    ShaperMatcher,
    ShaperRegistry,
    ShaperResult,
    SourceKind,
    Strategy,
    VerificationResult,
)


DEFAULT_VERIFICATION = object()
DEFAULT_REASON_CODE = object()


def make_request(tool_input=None):
    return GovernanceRequest(
        source_kind=SourceKind.CLAUDE_HOOK,
        tool_name="Bash",
        tool_input={} if tool_input is None else tool_input,
        command_result=CommandResult(
            stdout="ok\n",
            stderr="",
            exit_code=0,
            interrupted=False,
        ),
        raw_text="ok\n",
        payload_bytes=3,
        mode=GovernanceMode.AUTO,
    )


def make_verification(
    *,
    ok=True,
    protected_fact_count=1,
    missing_fact_count=0,
    reason_code=ReasonCode.PRESERVATION_PASSED,
):
    return VerificationResult(
        ok=ok,
        protected_fact_count=protected_fact_count,
        missing_fact_count=missing_fact_count,
        reason_code=reason_code,
    )


def make_result(
    *,
    action=Action.TRANSFORM,
    strategy=Strategy.REPETITIVE_LOG,
    risk=Risk.LOW,
    reason_code=DEFAULT_REASON_CODE,
    confidence=Confidence.HIGH,
    preservation_check=DEFAULT_VERIFICATION,
    token_before=10,
    token_after=2,
    tokens_saved=8,
    receipt_id="receipt-1",
):
    if preservation_check is DEFAULT_VERIFICATION and action is Action.TRANSFORM:
        preservation_check = make_verification()
    if reason_code is DEFAULT_REASON_CODE:
        reason_code = (
            ReasonCode.TRANSFORMED
            if action is Action.TRANSFORM
            else ReasonCode.NO_MATCHING_STRATEGY
        )
    return GovernanceResult(
        action=action,
        content="governed",
        risk=risk,
        reason_code=reason_code,
        strategy=strategy,
        confidence=confidence,
        preservation_check=preservation_check,
        token_before=token_before,
        token_after=token_after,
        tokens_saved=tokens_saved,
        receipt_id=receipt_id,
    )


def test_contracts_are_frozen_and_use_closed_enums():
    request = make_request()
    decision = PolicyDecision(
        action=Action.TRANSFORM,
        strategy=Strategy.REPETITIVE_LOG,
        reason_code=ReasonCode.STRATEGY_MATCHED,
        confidence=Confidence.HIGH,
    )
    verification = VerificationResult(
        ok=True,
        protected_fact_count=2,
        missing_fact_count=0,
        reason_code=ReasonCode.PRESERVATION_PASSED,
    )
    result = GovernanceResult(
        action=Action.TRANSFORM,
        content="ok\n",
        risk=Risk.LOW,
        reason_code=ReasonCode.TRANSFORMED,
        strategy=Strategy.REPETITIVE_LOG,
        confidence=Confidence.HIGH,
        preservation_check=verification,
        token_before=10,
        token_after=2,
        tokens_saved=8,
        receipt_id="receipt-1",
    )

    assert decision.strategy is Strategy.REPETITIVE_LOG
    assert verification.ok
    assert result.risk is Risk.LOW
    assert result.preservation_check is verification
    with pytest.raises(FrozenInstanceError):
        request.raw_text = "changed"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: GovernanceRequest(
            source_kind="claude_hook",
            tool_name="Bash",
            tool_input={},
            command_result=None,
            raw_text="",
            payload_bytes=0,
            mode=GovernanceMode.AUTO,
        ),
        lambda: PolicyDecision(
            action=Action.PASSTHROUGH,
            strategy="passthrough",
            reason_code=ReasonCode.NO_MATCHING_STRATEGY,
            confidence=Confidence.UNAVAILABLE,
        ),
        lambda: GovernanceResult(
            action=Action.PASSTHROUGH,
            content="",
            risk="low",
            reason_code=ReasonCode.NO_MATCHING_STRATEGY,
            strategy=Strategy.PASSTHROUGH,
            confidence=Confidence.UNAVAILABLE,
            preservation_check=None,
            token_before=0,
            token_after=0,
            tokens_saved=0,
            receipt_id=None,
        ),
        lambda: VerificationResult(
            ok=False,
            protected_fact_count=0,
            missing_fact_count=0,
            reason_code="preservation_failed",
        ),
    ],
)
def test_contracts_reject_free_form_enum_values(factory):
    with pytest.raises(TypeError):
        factory()


def test_tool_input_is_deeply_copied_and_immutable():
    original = {
        "command": "pytest",
        "options": {"paths": ["tests/unit", "tests/api"]},
    }

    request = make_request(original)
    original["command"] = "changed"
    original["options"]["paths"].append("tests/other")

    assert request.tool_input["command"] == "pytest"
    assert request.tool_input["options"]["paths"] == ("tests/unit", "tests/api")
    with pytest.raises(TypeError):
        request.tool_input["command"] = "changed"
    with pytest.raises(TypeError):
        request.tool_input["options"]["new"] = True


def test_tool_input_rejects_excessive_nesting_with_fixed_error():
    nested = {}
    for _ in range(33):
        nested = {"nested": nested}

    with pytest.raises(
        TypeError,
        match="^tool_input exceeds maximum nesting depth$",
    ):
        make_request(nested)


def test_shaper_result_copies_collections_to_immutable_tuples():
    facts = ["ERROR exact line"]
    diagnostic = ShaperDiagnostic(
        code=ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES,
        start_line=2,
        end_line=4,
        occurrence_count=3,
        original_content="progress",
    )
    diagnostics = [diagnostic]

    result = ShaperResult(
        content="governed",
        candidate_protected_facts=facts,
        diagnostics=diagnostics,
    )
    facts.append("late mutation")
    diagnostics.append(
        ShaperDiagnostic(
            code=ShaperDiagnosticCode.COLLAPSED_PROGRESS,
            start_line=5,
            end_line=5,
            occurrence_count=1,
            original_content="50%",
        )
    )

    assert result.candidate_protected_facts == ("ERROR exact line",)
    assert result.diagnostics == (diagnostic,)


def test_shaper_result_rejects_free_form_diagnostics():
    with pytest.raises(TypeError):
        ShaperResult("governed", (), ("free-form reason",))


def test_protocols_accept_structural_injected_implementations():
    class Matcher:
        strategy = Strategy.REPETITIVE_LOG

        def matches(self, request):
            return True

    class Registry:
        def matcher_for(self, strategy):
            return Matcher()

        def shape(self, strategy, request):
            return ShaperResult("governed", (), ())

    class Ledger:
        def allocate_receipt_id(self):
            return "receipt-1"

        def store_prepared(self, receipt_id, original_text, result):
            return None

        def mark_emitted(self, receipt_id):
            return None

    class LegacyLedger:
        def store_transformation(self, original_text, result):
            return "receipt-1"

    class Events:
        def record_event(self, source_kind, action, risk, reason_code, token_count, receipt_id):
            return None

    assert isinstance(Matcher(), ShaperMatcher)
    assert isinstance(Registry(), ShaperRegistry)
    assert isinstance(Ledger(), LedgerPort)
    assert not isinstance(LegacyLedger(), LedgerPort)
    assert isinstance(Events(), EventPort)


@pytest.mark.parametrize(
    "values",
    [
        {
            "ok": True,
            "protected_fact_count": 1,
            "missing_fact_count": 2,
            "reason_code": ReasonCode.PRESERVATION_PASSED,
        },
        {
            "ok": True,
            "protected_fact_count": 2,
            "missing_fact_count": 1,
            "reason_code": ReasonCode.PRESERVATION_PASSED,
        },
        {
            "ok": False,
            "protected_fact_count": 1,
            "missing_fact_count": 0,
            "reason_code": ReasonCode.PRESERVATION_FAILED,
        },
        {
            "ok": True,
            "protected_fact_count": 1,
            "missing_fact_count": 0,
            "reason_code": ReasonCode.PRESERVATION_FAILED,
        },
        {
            "ok": False,
            "protected_fact_count": 1,
            "missing_fact_count": 1,
            "reason_code": ReasonCode.PRESERVATION_PASSED,
        },
        {
            "ok": True,
            "protected_fact_count": 1,
            "missing_fact_count": 0,
            "reason_code": ReasonCode.TRANSFORMED,
        },
        {
            "ok": False,
            "protected_fact_count": 1,
            "missing_fact_count": 1,
            "reason_code": ReasonCode.TRANSFORMED,
        },
    ],
)
def test_verification_result_rejects_contradictory_states(values):
    with pytest.raises(ValueError):
        VerificationResult(**values)


@pytest.mark.parametrize(
    ("action", "strategy"),
    [
        (Action.TRANSFORM, Strategy.PASSTHROUGH),
        (Action.PASSTHROUGH, Strategy.TEST_OUTPUT),
    ],
)
def test_policy_decision_action_and_strategy_must_agree(action, strategy):
    with pytest.raises(ValueError):
        PolicyDecision(
            action=action,
            strategy=strategy,
            reason_code=ReasonCode.NO_MATCHING_STRATEGY,
            confidence=Confidence.UNAVAILABLE,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason_code": ReasonCode.TRANSFORMED},
        {"confidence": Confidence.UNAVAILABLE},
    ],
)
def test_transform_policy_decision_requires_matched_reason_and_available_confidence(
    overrides,
):
    values = {
        "action": Action.TRANSFORM,
        "strategy": Strategy.REPETITIVE_LOG,
        "reason_code": ReasonCode.STRATEGY_MATCHED,
        "confidence": Confidence.HIGH,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        PolicyDecision(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy": Strategy.PASSTHROUGH},
        {"preservation_check": None},
        {
            "preservation_check": make_verification(
                ok=False,
                protected_fact_count=1,
                missing_fact_count=1,
                reason_code=ReasonCode.PRESERVATION_FAILED,
            )
        },
        {"receipt_id": None},
        {"receipt_id": ""},
    ],
)
def test_transformed_result_requires_registered_verified_receipted_state(overrides):
    values = {
        "action": Action.TRANSFORM,
        "strategy": Strategy.REPETITIVE_LOG,
        "preservation_check": make_verification(),
        "receipt_id": "receipt-1",
    }
    values.update(overrides)

    with pytest.raises((TypeError, ValueError)):
        make_result(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason_code": ReasonCode.STRATEGY_MATCHED},
        {"risk": Risk.HIGH},
        {"risk": Risk.UNAVAILABLE},
        {"confidence": Confidence.UNAVAILABLE},
    ],
)
def test_transformed_result_requires_success_reason_bounded_risk_and_confidence(
    overrides,
):
    with pytest.raises(ValueError):
        make_result(**overrides)


@pytest.mark.parametrize("risk", [Risk.LOW, Risk.MEDIUM])
def test_transformed_result_accepts_verified_low_or_medium_risk(risk):
    result = make_result(risk=risk)

    assert result.risk is risk


def test_transformed_result_requires_a_strict_token_reduction():
    with pytest.raises(
        ValueError,
        match="^transformed results require positive token savings$",
    ):
        make_result(token_before=10, token_after=10, tokens_saved=0)


@pytest.mark.parametrize("confidence", [Confidence.LOW, Confidence.MEDIUM])
def test_low_risk_transformed_result_requires_high_confidence(confidence):
    with pytest.raises(
        ValueError,
        match="^low-risk transformed results require high confidence$",
    ):
        make_result(risk=Risk.LOW, confidence=confidence)


@pytest.mark.parametrize("confidence", [Confidence.LOW, Confidence.MEDIUM])
def test_medium_risk_transformed_result_accepts_available_confidence(confidence):
    result = make_result(risk=Risk.MEDIUM, confidence=confidence)

    assert result.confidence is confidence


def test_transformed_result_accepts_positive_token_savings():
    result = make_result(token_before=10, token_after=2, tokens_saved=8)

    assert result.tokens_saved == 8


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy": Strategy.TEST_OUTPUT},
        {"receipt_id": "receipt-1"},
        {"token_after": 9, "tokens_saved": 1},
    ],
)
def test_passthrough_result_requires_passthrough_strategy_no_receipt_and_no_savings(
    overrides,
):
    values = {
        "action": Action.PASSTHROUGH,
        "strategy": Strategy.PASSTHROUGH,
        "preservation_check": None,
        "token_before": 10,
        "token_after": 10,
        "tokens_saved": 0,
        "receipt_id": None,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        make_result(**values)


def test_low_risk_passthrough_is_a_valid_contract_state():
    result = make_result(
        action=Action.PASSTHROUGH,
        strategy=Strategy.PASSTHROUGH,
        preservation_check=None,
        token_before=10,
        token_after=10,
        tokens_saved=0,
        receipt_id=None,
    )

    assert result.risk is Risk.LOW


def test_command_family_is_engine_owned_and_classifier_is_injectable():
    class Classifier:
        def classify(self, request):
            return contracts.CommandFamily.TEST

    request = make_request()

    assert tuple(contracts.CommandFamily) == (
        contracts.CommandFamily.UNKNOWN,
        contracts.CommandFamily.GENERIC_SHELL,
        contracts.CommandFamily.TEST,
        contracts.CommandFamily.BUILD,
    )
    assert "command_family" not in {field.name for field in fields(GovernanceRequest)}
    assert isinstance(Classifier(), contracts.CommandFamilyClassifier)
    assert Classifier().classify(request) is contracts.CommandFamily.TEST
    with pytest.raises(ValueError):
        contracts.CommandFamily("pytest")
