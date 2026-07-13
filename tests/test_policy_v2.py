from __future__ import annotations

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
    CommandFamily,
    CommandResult,
    Confidence,
    GovernanceMode,
    GovernanceRequest,
    PersistenceMode,
    ProtectedContentBehavior,
    ReasonCode,
    Risk,
    SourceKind,
    Strategy,
)
from token_governance.core import GovernanceEngine
from token_governance.policy import PolicyEngine
from token_governance.secret_detector import SecretDetectionResult, SecretDetector


def make_config(
    *,
    enabled: tuple[Strategy, ...] = (
        Strategy.TEST_OUTPUT,
        Strategy.BUILD_OUTPUT,
        Strategy.REPETITIVE_LOG,
    ),
    max_payload_bytes: int = 4096,
    max_stored_original_bytes: int = 2048,
    hook_deadline_ms: int = 100,
    markers: tuple[str, ...] = (),
) -> GovernanceConfig:
    return GovernanceConfig(
        ledger=LedgerConfig(path=Path("unused.sqlite"), retention_days=30),
        policy=PolicyConfig(
            enabled_strategies=enabled,
            protected_content_behavior=ProtectedContentBehavior.PASSTHROUGH,
            persistence_mode=PersistenceMode.TRANSFORMED_ONLY,
            max_payload_bytes=max_payload_bytes,
            max_stored_original_bytes=max_stored_original_bytes,
            hook_deadline_ms=hook_deadline_ms,
            literal_secret_markers=markers,
        ),
        gateway=GatewayConfig(
            request_timeout_seconds=10,
            backends=(),
            tool_policy=GatewayToolPolicyConfig(allow=(), deny=()),
        ),
        config_path=Path("token-governance.config.json"),
    )


def make_request(
    text: str = "ordinary output",
    *,
    tool_name: str | None = "Bash",
    tool_input: dict | None = None,
    payload_bytes: int | None = None,
    source_kind: SourceKind = SourceKind.CLAUDE_HOOK,
    mode: GovernanceMode = GovernanceMode.AUTO,
    stdout: str | None = None,
    stderr: str = "",
) -> GovernanceRequest:
    return GovernanceRequest(
        source_kind=source_kind,
        tool_name=tool_name,
        tool_input=(
            {"command": "pytest -q"} if tool_input is None else tool_input
        ),
        command_result=CommandResult(
            stdout=text if stdout is None else stdout,
            stderr=stderr,
            exit_code=0,
            interrupted=False,
        ),
        raw_text=text,
        payload_bytes=(
            len(text.encode("utf-8")) if payload_bytes is None else payload_bytes
        ),
        mode=mode,
    )


class Classifier:
    def __init__(self, family=CommandFamily.TEST, error: Exception | None = None):
        self.family = family
        self.error = error
        self.requests = []

    def classify(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.family


class Matcher:
    def __init__(self, strategy, matched, *, confidence=Confidence.HIGH, error=None):
        self.strategy = strategy
        self.matched = matched
        self.confidence = confidence
        self.error = error
        self.requests = []

    def matches(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.matched


class Registry:
    def __init__(self, matches=None):
        matches = matches or {}
        self.matchers = {
            strategy: Matcher(strategy, matches.get(strategy, False))
            for strategy in (
                Strategy.TEST_OUTPUT,
                Strategy.BUILD_OUTPUT,
                Strategy.REPETITIVE_LOG,
            )
        }
        self.requested = []

    def matcher_for(self, strategy):
        self.requested.append(strategy)
        return self.matchers[strategy]

    def shape(self, strategy, request):
        raise AssertionError("T02 must not shape content")


class SpyLedger:
    def __getattr__(self, name):
        if name in {"allocate_receipt_id", "store_prepared", "mark_emitted", "record"}:
            raise AssertionError(f"T02 must not call ledger.{name}")
        raise AttributeError(name)


class SpyEvents:
    def __init__(self):
        self.calls = []

    def record_event(self, *args):
        self.calls.append(args)


def test_policy_uses_registered_order_and_derives_family_from_request():
    classifier = Classifier(CommandFamily.TEST)
    registry = Registry({Strategy.TEST_OUTPUT: True})
    policy = PolicyEngine(classifier=classifier, registry=registry)
    request = make_request(tool_input={"command": "pytest -q"})

    decision = policy.decide_request(request, make_config())

    assert decision.action is Action.TRANSFORM
    assert decision.strategy is Strategy.TEST_OUTPUT
    assert decision.reason_code is ReasonCode.STRATEGY_MATCHED
    assert decision.confidence is Confidence.HIGH
    assert classifier.requests == [request]
    assert registry.requested == [
        Strategy.TEST_OUTPUT,
        Strategy.BUILD_OUTPUT,
        Strategy.REPETITIVE_LOG,
    ]


@pytest.mark.parametrize(
    ("matches", "reason"),
    [
        ({}, ReasonCode.NO_MATCHING_STRATEGY),
        (
            {Strategy.TEST_OUTPUT: True, Strategy.REPETITIVE_LOG: True},
            ReasonCode.AMBIGUOUS_STRATEGY,
        ),
    ],
)
def test_policy_fails_open_for_zero_or_multiple_matchers(matches, reason):
    decision = PolicyEngine(
        classifier=Classifier(CommandFamily.TEST),
        registry=Registry(matches),
    ).decide_request(make_request(), make_config())

    assert decision.action is Action.PASSTHROUGH
    assert decision.strategy is Strategy.PASSTHROUGH
    assert decision.reason_code is reason
    assert decision.confidence is Confidence.UNAVAILABLE


def test_disabled_and_low_confidence_strategies_pass_through():
    registry = Registry({Strategy.TEST_OUTPUT: True})
    disabled = PolicyEngine(
        classifier=Classifier(CommandFamily.TEST), registry=registry
    ).decide_request(
        make_request(),
        make_config(enabled=(Strategy.BUILD_OUTPUT,)),
        explicit_strategy=Strategy.TEST_OUTPUT,
    )
    registry.matchers[Strategy.TEST_OUTPUT].confidence = Confidence.LOW
    low_confidence = PolicyEngine(
        classifier=Classifier(CommandFamily.TEST), registry=registry
    ).decide_request(make_request(), make_config())

    assert disabled.reason_code is ReasonCode.STRATEGY_DISABLED
    assert disabled.action is Action.PASSTHROUGH
    assert low_confidence.reason_code is ReasonCode.NO_MATCHING_STRATEGY
    assert low_confidence.action is Action.PASSTHROUGH


def test_low_confidence_match_still_counts_toward_ambiguity():
    registry = Registry(
        {Strategy.TEST_OUTPUT: True, Strategy.REPETITIVE_LOG: True}
    )
    registry.matchers[Strategy.REPETITIVE_LOG].confidence = Confidence.LOW

    decision = PolicyEngine(
        classifier=Classifier(CommandFamily.TEST), registry=registry
    ).decide_request(make_request(), make_config())

    assert decision.action is Action.PASSTHROUGH
    assert decision.reason_code is ReasonCode.AMBIGUOUS_STRATEGY


def test_explicit_strategy_still_requires_its_matcher_and_family():
    registry = Registry({Strategy.TEST_OUTPUT: False})
    decision = PolicyEngine(
        classifier=Classifier(CommandFamily.TEST), registry=registry
    ).decide_request(
        make_request(), make_config(), explicit_strategy=Strategy.TEST_OUTPUT
    )

    assert decision.action is Action.PASSTHROUGH
    assert decision.reason_code is ReasonCode.NO_MATCHING_STRATEGY


@pytest.mark.parametrize(
    ("case_request", "family"),
    [
        (
            make_request(tool_name="Bash"),
            CommandFamily.UNKNOWN,
        ),
        (
            make_request(tool_name="Read", tool_input={"path": "source.py"}),
            CommandFamily.GENERIC_SHELL,
        ),
        (
            make_request(tool_name="Custom", tool_input={"command": "tail app.log"}),
            CommandFamily.GENERIC_SHELL,
        ),
        (
            make_request(
                tool_name="Bash",
                source_kind=SourceKind.MCP,
                tool_input={"command": "tail app.log"},
            ),
            CommandFamily.GENERIC_SHELL,
        ),
    ],
)
def test_automatic_policy_rejects_unknown_hook_family_and_unregistered_surfaces(
    case_request, family
):
    registry = Registry({Strategy.REPETITIVE_LOG: True})
    decision = PolicyEngine(
        classifier=Classifier(family), registry=registry
    ).decide_request(case_request, make_config())

    assert decision.action is Action.PASSTHROUGH
    assert decision.reason_code is ReasonCode.NO_MATCHING_STRATEGY
    assert registry.requested == []


def test_cli_unknown_family_can_use_content_proven_repetitive_log_strategy():
    request = make_request(
        tool_name=None,
        tool_input={},
        source_kind=SourceKind.CLI,
        mode=GovernanceMode.MANUAL,
    )
    registry = Registry({Strategy.REPETITIVE_LOG: True})

    decision = PolicyEngine(
        classifier=Classifier(CommandFamily.UNKNOWN), registry=registry
    ).decide_request(request, make_config())

    assert decision.action is Action.TRANSFORM
    assert decision.strategy is Strategy.REPETITIVE_LOG
    assert decision.reason_code is ReasonCode.STRATEGY_MATCHED


@pytest.mark.parametrize(
    "policy",
    [
        lambda: PolicyEngine(
            classifier=Classifier(error=RuntimeError("contains-sensitive-data")),
            registry=Registry(),
        ),
        lambda: PolicyEngine(
            classifier=Classifier(),
            registry=Registry(),
        ),
    ],
)
def test_classifier_or_matcher_errors_are_payload_free(policy):
    engine_policy = policy()
    if engine_policy.registry is not None:
        engine_policy.registry.matchers[Strategy.TEST_OUTPUT].error = RuntimeError(
            "contains-sensitive-data"
        )
    decision = engine_policy.decide_request(make_request(), make_config())

    assert decision.action is Action.PASSTHROUGH
    assert decision.reason_code is ReasonCode.CLASSIFICATION_UNAVAILABLE
    assert decision.confidence is Confidence.UNAVAILABLE
    assert "contains-sensitive-data" not in repr(decision)


@pytest.mark.parametrize(
    ("case_request", "config", "reason", "risk"),
    [
        (
            make_request("x" * 20),
            make_config(max_payload_bytes=10, max_stored_original_bytes=10),
            ReasonCode.PAYLOAD_TOO_LARGE,
            Risk.MEDIUM,
        ),
            (
                make_request("x" * 20),
                make_config(max_payload_bytes=100, max_stored_original_bytes=10),
                ReasonCode.ORIGINAL_TOO_LARGE,
                Risk.MEDIUM,
            ),
        (
            make_request("payload", payload_bytes=1),
            make_config(),
            ReasonCode.MALFORMED_REQUEST,
            Risk.UNAVAILABLE,
        ),
    ],
)
def test_engine_bounds_and_malformed_requests_fail_open(
    case_request, config, reason, risk
):
    events = SpyEvents()
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=config,
        secret_detector=SecretDetector(),
        events=events,
        clock=lambda: 0.0,
    )

    result = engine.govern_request(case_request)

    assert result.action is Action.PASSTHROUGH
    assert result.content == case_request.raw_text
    assert result.receipt_id is None
    assert result.reason_code is reason
    assert result.risk is risk
    assert result.token_after == result.token_before
    assert result.tokens_saved == 0
    assert len(events.calls) == 1


def test_lone_surrogate_fails_open_as_malformed_without_ledger_access():
    text = "prefix-\ud800-suffix"
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(),
        secret_detector=SecretDetector(),
        events=SpyEvents(),
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request(text, payload_bytes=1))

    assert result.action is Action.PASSTHROUGH
    assert result.content == text
    assert result.reason_code is ReasonCode.MALFORMED_REQUEST
    assert result.risk is Risk.UNAVAILABLE
    assert result.receipt_id is None


@pytest.mark.parametrize(
    "oversized_field",
    ["stdout", "tool_input"],
)
def test_structured_oversize_is_rejected_before_secret_detection(oversized_field):
    class DetectorSpy:
        def __init__(self):
            self.called = False

        def detect(self, request, *, literal_secret_markers):
            self.called = True
            raise AssertionError("oversized structured data reached detector")

    detector = DetectorSpy()
    oversized = "x" * 5000
    request_kwargs = (
        {"stdout": oversized}
        if oversized_field == "stdout"
        else {"tool_input": {"command": "pytest", "data": oversized}}
    )
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(max_payload_bytes=4096, max_stored_original_bytes=2048),
        secret_detector=detector,
        events=SpyEvents(),
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request("ok", **request_kwargs))

    assert result.action is Action.PASSTHROUGH
    assert result.content == "ok"
    assert result.reason_code is ReasonCode.PAYLOAD_TOO_LARGE
    assert result.risk is Risk.MEDIUM
    assert detector.called is False


@pytest.mark.parametrize(
    "fail_open_case",
    [
        "config_invalid",
        "clock_error",
        "payload_too_large",
        "original_too_large",
        "deadline_exceeded",
    ],
)
def test_bounded_fail_open_paths_do_not_estimate_tokens(
    monkeypatch, fail_open_case
):
    def forbidden_estimate(_content):
        raise AssertionError("bounded fail-open path scanned content")

    monkeypatch.setattr("token_governance.core.estimate_tokens", forbidden_estimate)
    if fail_open_case == "config_invalid":
        request = make_request("ordinary output")
        config = replace(
            make_config(),
            policy=replace(make_config().policy, max_payload_bytes=-1),
        )
        clock = lambda: 0.0
        expected_reason = ReasonCode.CONFIG_INVALID
    elif fail_open_case == "clock_error":
        request = make_request("ordinary output")
        config = make_config()

        def clock():
            raise RuntimeError("clock unavailable")

        expected_reason = ReasonCode.CLASSIFICATION_UNAVAILABLE
    elif fail_open_case == "payload_too_large":
        request = make_request("x" * 20)
        config = make_config(max_payload_bytes=10, max_stored_original_bytes=10)
        clock = lambda: 0.0
        expected_reason = ReasonCode.PAYLOAD_TOO_LARGE
    elif fail_open_case == "original_too_large":
        request = make_request("x" * 20)
        config = make_config(max_payload_bytes=100, max_stored_original_bytes=10)
        clock = lambda: 0.0
        expected_reason = ReasonCode.ORIGINAL_TOO_LARGE
    else:
        ticks = iter((0.0, 1.0))
        request = make_request("ordinary output")
        config = make_config(hook_deadline_ms=100)
        clock = lambda: next(ticks)
        expected_reason = ReasonCode.DEADLINE_EXCEEDED

    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=config,
        secret_detector=SecretDetector(),
        events=SpyEvents(),
        clock=clock,
    )

    result = engine.govern_request(request)

    assert result.reason_code is expected_reason
    assert result.token_before == result.token_after
    assert result.tokens_saved == 0


def test_secret_detector_error_and_deadline_are_unavailable_and_payload_free():
    secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"

    class FailingDetector:
        def detect(self, request, *, literal_secret_markers):
            raise RuntimeError(secret)

    ticks = iter((0.0, 0.0, 0.2))
    detector_error_engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(),
        secret_detector=FailingDetector(),
        events=SpyEvents(),
        clock=lambda: 0.0,
    )
    timeout_events = SpyEvents()
    timeout_engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(hook_deadline_ms=100),
        secret_detector=SecretDetector(),
        events=timeout_events,
        clock=lambda: next(ticks),
    )

    unavailable = detector_error_engine.govern_request(make_request(secret))
    timed_out = timeout_engine.govern_request(make_request("ordinary output"))

    assert unavailable.reason_code is ReasonCode.CLASSIFICATION_UNAVAILABLE
    assert unavailable.risk is Risk.UNAVAILABLE
    assert unavailable.content == secret
    assert timed_out.reason_code is ReasonCode.DEADLINE_EXCEEDED
    assert timed_out.risk is Risk.UNAVAILABLE
    assert secret not in repr(detector_error_engine.events.calls)


@pytest.mark.parametrize(
    "detection_result",
    [
        SecretDetectionResult(
            detected="yes",
            reason_code=ReasonCode.SECRET_DETECTED,
        ),
        SecretDetectionResult(
            detected=True,
            reason_code="secret_detected",
        ),
        SecretDetectionResult(
            detected=True,
            reason_code=ReasonCode.NO_MATCHING_STRATEGY,
        ),
        SecretDetectionResult(
            detected=False,
            reason_code=ReasonCode.SECRET_DETECTED,
        ),
    ],
)
def test_detector_field_type_or_consistency_failures_are_unavailable(
    detection_result
):
    class Detector:
        def detect(self, request, *, literal_secret_markers):
            return detection_result

    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(),
        secret_detector=Detector(),
        events=SpyEvents(),
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request())

    assert result.action is Action.PASSTHROUGH
    assert result.reason_code is ReasonCode.CLASSIFICATION_UNAVAILABLE
    assert result.risk is Risk.UNAVAILABLE


@pytest.mark.parametrize("exploding_field", ["detected", "reason_code"])
def test_detector_duck_object_properties_are_never_trusted(exploding_field):
    class ExplodingDuck:
        def __getattribute__(self, name):
            if name == exploding_field:
                raise RuntimeError("sensitive-property-error")
            if name == "detected":
                return False
            if name == "reason_code":
                return ReasonCode.NO_MATCHING_STRATEGY
            return object.__getattribute__(self, name)

    class Detector:
        def detect(self, request, *, literal_secret_markers):
            return ExplodingDuck()

    events = SpyEvents()
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(),
        secret_detector=Detector(),
        events=events,
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request())

    assert result.reason_code is ReasonCode.CLASSIFICATION_UNAVAILABLE
    assert "sensitive-property-error" not in repr(events.calls)


def test_secret_passthrough_has_high_risk_and_closed_event_metadata_only():
    secret = "Author" + "ization: Bearer " + "very-sensitive-token-value"
    events = SpyEvents()
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(markers=("very-sensitive-token-value",)),
        secret_detector=SecretDetector(),
        events=events,
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request(secret))

    assert result.action is Action.PASSTHROUGH
    assert result.content == secret
    assert result.risk is Risk.HIGH
    assert result.reason_code is ReasonCode.SECRET_DETECTED
    assert result.receipt_id is None
    assert len(events.calls) == 1
    assert all(
        isinstance(item, (SourceKind, Action, Risk, ReasonCode, int)) or item is None
        for item in events.calls[0]
    )
    assert secret not in repr(events.calls)


@pytest.mark.parametrize("field", ["stdout", "stderr", "tool_input"])
@pytest.mark.parametrize("position", ["first", "middle", "end"])
def test_structured_secret_corpus_never_enters_result_or_events(field, position):
    marker = "秘密-event-marker-" + field + "-" + position
    parts = {
        "first": [marker, "普通输出", "done"],
        "middle": ["普通输出", marker, "done"],
        "end": ["普通输出", "done", marker],
    }[position]
    structured_value = "\n".join(parts)
    request_kwargs = (
        {"tool_input": {"nested": ["开始", {"value": structured_value}]}}
        if field == "tool_input"
        else {field: structured_value}
    )
    events = SpyEvents()
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=Classifier(), registry=Registry()),
        config=make_config(markers=(marker,)),
        secret_detector=SecretDetector(),
        events=events,
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request("ordinary output", **request_kwargs))

    assert result.reason_code is ReasonCode.SECRET_DETECTED
    assert result.risk is Risk.HIGH
    assert marker not in repr(result)
    assert marker not in repr(events.calls)


@pytest.mark.parametrize(
    ("tool_name", "reason", "risk"),
    [
        ("Read", ReasonCode.PROTECTED_CONTENT, Risk.HIGH),
        ("Bash", ReasonCode.NO_MATCHING_STRATEGY, Risk.UNAVAILABLE),
    ],
)
def test_protected_and_unknown_content_use_conservative_risk(
    tool_name, reason, risk
):
    classifier = Classifier(CommandFamily.UNKNOWN)
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=classifier, registry=Registry()),
        config=make_config(),
        secret_detector=SecretDetector(),
        events=SpyEvents(),
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request(tool_name=tool_name))

    assert result.action is Action.PASSTHROUGH
    assert result.reason_code is reason
    assert result.risk is risk
    if tool_name == "Read":
        assert classifier.requests == []


def test_invalid_typed_config_fails_open_without_calling_dependencies():
    invalid_policy = replace(make_config().policy, max_payload_bytes=-1)
    invalid_config = replace(make_config(), policy=invalid_policy)
    classifier = Classifier()
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=classifier, registry=Registry()),
        config=invalid_config,
        secret_detector=SecretDetector(),
        events=SpyEvents(),
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request())

    assert result.reason_code is ReasonCode.CONFIG_INVALID
    assert result.risk is Risk.UNAVAILABLE
    assert classifier.requests == []


def test_invalid_literal_marker_config_fails_open_before_detection():
    invalid_policy = replace(make_config().policy, literal_secret_markers=("",))
    invalid_config = replace(make_config(), policy=invalid_policy)
    classifier = Classifier()
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=PolicyEngine(classifier=classifier, registry=Registry()),
        config=invalid_config,
        secret_detector=SecretDetector(),
        events=SpyEvents(),
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request())

    assert result.reason_code is ReasonCode.CONFIG_INVALID
    assert classifier.requests == []


def test_engine_contains_policy_failures_without_leaking_exception_text():
    secret = "policy-exception-sensitive-value"

    class FailingPolicy:
        def decide_request(self, request, config, *, explicit_strategy=None):
            raise RuntimeError(secret)

    events = SpyEvents()
    engine = GovernanceEngine(
        ledger=SpyLedger(),
        policy=FailingPolicy(),
        config=make_config(),
        secret_detector=SecretDetector(),
        events=events,
        clock=lambda: 0.0,
    )

    result = engine.govern_request(make_request())

    assert result.reason_code is ReasonCode.CLASSIFICATION_UNAVAILABLE
    assert result.risk is Risk.UNAVAILABLE
    assert secret not in repr(events.calls)


def test_policy_module_does_not_import_concrete_shapers():
    source = Path(PolicyEngine.__module__.replace(".", "/") + ".py")
    policy_text = Path("src") / source

    assert "import shaper" not in policy_text.read_text(encoding="utf-8").lower()
    assert "from .shaper" not in policy_text.read_text(encoding="utf-8").lower()
