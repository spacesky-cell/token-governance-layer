from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping

from .config import (
    DEFAULT_GATEWAY_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_HOOK_DEADLINE_MS,
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MAX_STORED_ORIGINAL_BYTES,
    DEFAULT_RETENTION_DAYS,
    MAX_HOOK_DEADLINE_MS,
    MAX_LITERAL_SECRET_MARKER_LENGTH,
    MAX_LITERAL_SECRET_MARKERS,
    MAX_PAYLOAD_BYTES,
    GatewayConfig,
    GatewayToolPolicyConfig,
    GovernanceConfig,
    LedgerConfig,
    PolicyConfig,
)
from .contracts import (
    Action,
    CommandFamily,
    Confidence,
    EventPort,
    GovernanceRequest,
    GovernanceResult,
    PersistenceMode,
    PolicyDecision as GovernancePolicyDecision,
    ProtectedContentBehavior,
    ReasonCode,
    Risk,
    ShaperRegistry,
    SourceKind,
    Strategy,
    VerificationResult,
)
from .ledger import ContextLedger
from .policy import PolicyEngine
from .preservation import PreservationVerifier
from .secret_detector import SecretDetectionResult, SecretDetector
from .shapers import BuiltinShaperRegistry, normalize_request_output
from .tokenizer import estimate_tokens


@dataclass
class GovernanceEngine:
    ledger: ContextLedger
    policy: PolicyEngine
    config: GovernanceConfig | None = None
    secret_detector: SecretDetector | None = None
    events: EventPort | None = None
    clock: Callable[[], float] = monotonic
    registry: ShaperRegistry | None = None
    verifier: PreservationVerifier | None = None

    def __post_init__(self) -> None:
        policy_registry = getattr(self.policy, "registry", None)
        if self.registry is None:
            self.registry = policy_registry or BuiltinShaperRegistry()
        if isinstance(self.policy, PolicyEngine):
            self.policy.registry = self.registry
            if self.policy.classifier is None:
                self.policy.classifier = _BuiltinCommandFamilyClassifier()
        if self.verifier is None:
            self.verifier = PreservationVerifier()

    def govern_request(
        self,
        request: GovernanceRequest,
        *,
        explicit_strategy: Strategy | None = None,
        prepare_result: Callable[[GovernanceResult], None] | None = None,
    ) -> GovernanceResult:
        if not isinstance(request, GovernanceRequest):
            return self._passthrough_result(
                content="",
                source_kind=None,
                reason_code=ReasonCode.MALFORMED_REQUEST,
                risk=Risk.UNAVAILABLE,
            )

        content = request.raw_text
        if not self._config_is_usable():
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.CONFIG_INVALID,
                risk=Risk.UNAVAILABLE,
                token_count=request.payload_bytes,
            )
        assert self.config is not None

        try:
            deadline = self.clock() + self.config.policy.hook_deadline_ms / 1000
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.CLASSIFICATION_UNAVAILABLE,
                risk=Risk.UNAVAILABLE,
                token_count=request.payload_bytes,
            )

        inspection_reason, actual_payload_bytes = self._measure_inspection_bytes(
            request,
            self.config.policy.max_payload_bytes,
        )
        if inspection_reason is not None:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=inspection_reason,
                risk=self._risk_for_reason(inspection_reason),
                token_count=request.payload_bytes,
            )
        assert actual_payload_bytes is not None
        if request.payload_bytes != actual_payload_bytes:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.MALFORMED_REQUEST,
                risk=Risk.UNAVAILABLE,
            )
        if request.payload_bytes > self.config.policy.max_stored_original_bytes:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.ORIGINAL_TOO_LARGE,
                risk=Risk.MEDIUM,
                token_count=request.payload_bytes,
            )
        if self._deadline_exceeded(deadline):
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.DEADLINE_EXCEEDED,
                risk=Risk.UNAVAILABLE,
                token_count=request.payload_bytes,
            )

        detector = self.secret_detector or SecretDetector()
        try:
            detection = detector.detect(
                request,
                literal_secret_markers=self.config.policy.literal_secret_markers,
            )
            if not isinstance(detection, SecretDetectionResult):
                raise TypeError("detector returned an invalid result")
            detected = detection.detected
            detection_reason = detection.reason_code
            if not isinstance(detected, bool) or not isinstance(
                detection_reason,
                ReasonCode,
            ):
                raise TypeError("detector returned invalid result fields")
            expected_reason = (
                ReasonCode.SECRET_DETECTED
                if detected
                else ReasonCode.NO_MATCHING_STRATEGY
            )
            if detection_reason is not expected_reason:
                raise ValueError("detector returned inconsistent result fields")
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.CLASSIFICATION_UNAVAILABLE,
                risk=Risk.UNAVAILABLE,
            )
        if self._deadline_exceeded(deadline):
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.DEADLINE_EXCEEDED,
                risk=Risk.UNAVAILABLE,
                token_count=request.payload_bytes,
            )
        if detected:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.SECRET_DETECTED,
                risk=Risk.HIGH,
            )
        if self._is_protected_request(request):
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.PROTECTED_CONTENT,
                risk=Risk.HIGH,
            )

        try:
            decision = self.policy.decide_request(
                request,
                self.config,
                explicit_strategy=explicit_strategy,
            )
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.CLASSIFICATION_UNAVAILABLE,
                risk=Risk.UNAVAILABLE,
            )
        if not isinstance(decision, GovernancePolicyDecision):
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.CLASSIFICATION_UNAVAILABLE,
                risk=Risk.UNAVAILABLE,
            )
        if self._deadline_exceeded(deadline):
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.DEADLINE_EXCEEDED,
                risk=Risk.UNAVAILABLE,
                token_count=request.payload_bytes,
            )
        if decision.action is not Action.TRANSFORM:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=decision.reason_code,
                risk=self._risk_for_reason(decision.reason_code),
                confidence=decision.confidence,
            )

        assert self.registry is not None
        assert self.verifier is not None
        try:
            shaped = self.registry.shape(decision.strategy, request)
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.SHAPER_FAILED,
                risk=Risk.UNAVAILABLE,
                confidence=decision.confidence,
            )
        if self._deadline_exceeded(deadline):
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.DEADLINE_EXCEEDED,
                risk=Risk.UNAVAILABLE,
                token_count=request.payload_bytes,
            )

        try:
            verification = self.verifier.verify(
                decision.strategy,
                request,
                shaped,
            )
            if not isinstance(verification, VerificationResult):
                raise TypeError("verifier returned an invalid result")
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.PRESERVATION_FAILED,
                risk=Risk.HIGH,
                confidence=decision.confidence,
            )
        if not verification.ok:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.PRESERVATION_FAILED,
                risk=Risk.HIGH,
                confidence=decision.confidence,
            )

        try:
            original_for_storage = normalize_request_output(request)
            original_size = len(original_for_storage.encode("utf-8"))
            candidate_size = len(shaped.content.encode("utf-8"))
            if original_size > self.config.policy.max_stored_original_bytes:
                return self._passthrough_result(
                    content=content,
                    source_kind=request.source_kind,
                    reason_code=ReasonCode.ORIGINAL_TOO_LARGE,
                    risk=Risk.MEDIUM,
                    token_count=request.payload_bytes,
                )
            token_before = estimate_tokens(original_for_storage)
            token_after = estimate_tokens(shaped.content)
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.SHAPER_FAILED,
                risk=Risk.UNAVAILABLE,
                confidence=decision.confidence,
            )
        if candidate_size >= original_size or token_after >= token_before:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.NOT_SMALLER,
                risk=Risk.MEDIUM,
                confidence=decision.confidence,
            )
        if self._deadline_exceeded(deadline):
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.DEADLINE_EXCEEDED,
                risk=Risk.UNAVAILABLE,
                token_count=request.payload_bytes,
            )

        try:
            receipt_id = self.ledger.allocate_receipt_id()
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.LEDGER_FAILED,
                risk=Risk.UNAVAILABLE,
                confidence=decision.confidence,
            )
        risk = (
            Risk.LOW
            if request.source_kind is SourceKind.CLAUDE_HOOK
            else Risk.MEDIUM
        )
        result = GovernanceResult(
            action=Action.TRANSFORM,
            content=shaped.content,
            risk=risk,
            reason_code=ReasonCode.TRANSFORMED,
            strategy=decision.strategy,
            confidence=decision.confidence,
            preservation_check=verification,
            token_before=token_before,
            token_after=token_after,
            tokens_saved=token_before - token_after,
            receipt_id=receipt_id,
        )
        if prepare_result is not None:
            try:
                prepare_result(result)
            except Exception:
                return self._passthrough_result(
                    content=content,
                    source_kind=request.source_kind,
                    reason_code=ReasonCode.SERIALIZATION_FAILED,
                    risk=Risk.UNAVAILABLE,
                    confidence=decision.confidence,
                )

        try:
            self.ledger.store_prepared(receipt_id, original_for_storage, result)
        except Exception:
            return self._passthrough_result(
                content=content,
                source_kind=request.source_kind,
                reason_code=ReasonCode.LEDGER_FAILED,
                risk=Risk.UNAVAILABLE,
                confidence=decision.confidence,
            )
        self._record_event(request.source_kind, result)
        return result

    def _record_event(
        self,
        source_kind: SourceKind,
        result: GovernanceResult,
    ) -> None:
        if self.events is None:
            return
        try:
            self.events.record_event(
                source_kind,
                result.action,
                result.risk,
                result.reason_code,
                result.token_before,
                result.receipt_id,
            )
        except Exception:
            pass

    def _config_is_usable(self) -> bool:
        if not isinstance(self.config, GovernanceConfig):
            return False
        try:
            policy = self.config.policy
            numeric_values = (
                policy.max_payload_bytes,
                policy.max_stored_original_bytes,
                policy.hook_deadline_ms,
            )
            markers = policy.literal_secret_markers
            valid_numbers = all(
                not isinstance(value, bool)
                and isinstance(value, int)
                and value > 0
                for value in numeric_values
            )
            valid_markers = (
                isinstance(markers, tuple)
                and len(markers) <= MAX_LITERAL_SECRET_MARKERS
                and len(markers) == len(set(markers))
                and all(
                    isinstance(marker, str)
                    and 0 < len(marker) <= MAX_LITERAL_SECRET_MARKER_LENGTH
                    for marker in markers
                )
            )
            return (
                valid_numbers
                and valid_markers
                and policy.max_payload_bytes <= MAX_PAYLOAD_BYTES
                and policy.max_stored_original_bytes <= policy.max_payload_bytes
                and policy.hook_deadline_ms <= MAX_HOOK_DEADLINE_MS
            )
        except Exception:
            return False

    @classmethod
    def _measure_inspection_bytes(
        cls,
        request: GovernanceRequest,
        max_bytes: int,
    ) -> tuple[ReasonCode | None, int | None]:
        total = 0

        def add_text(value: str) -> tuple[bool, int | None]:
            nonlocal total
            remaining = max_bytes - total
            length = cls._bounded_utf8_length(value, remaining)
            if length is None:
                return False, None
            total += length
            return total <= max_bytes, length

        within_limit, raw_bytes = add_text(request.raw_text)
        if raw_bytes is None:
            return ReasonCode.MALFORMED_REQUEST, None
        if not within_limit:
            return ReasonCode.PAYLOAD_TOO_LARGE, None

        def visit(value: Any) -> ReasonCode | None:
            if isinstance(value, str):
                within, length = add_text(value)
                if length is None:
                    return ReasonCode.MALFORMED_REQUEST
                if not within:
                    return ReasonCode.PAYLOAD_TOO_LARGE
                return None
            if isinstance(value, Mapping):
                for key, item in value.items():
                    reason = visit(key)
                    if reason is not None:
                        return reason
                    reason = visit(item)
                    if reason is not None:
                        return reason
                return None
            if isinstance(value, tuple):
                for item in value:
                    reason = visit(item)
                    if reason is not None:
                        return reason
            return None

        if request.command_result is not None:
            for value in (
                request.command_result.stdout,
                request.command_result.stderr,
            ):
                reason = visit(value)
                if reason is not None:
                    return reason, None
        reason = visit(request.tool_input)
        if reason is not None:
            return reason, None
        return None, raw_bytes

    @staticmethod
    def _bounded_utf8_length(value: str, limit: int) -> int | None:
        length = 0
        for character in value:
            code_point = ord(character)
            if code_point <= 0x7F:
                width = 1
            elif code_point <= 0x7FF:
                width = 2
            elif 0xD800 <= code_point <= 0xDFFF:
                return None
            elif code_point <= 0xFFFF:
                width = 3
            else:
                width = 4
            length += width
            if length > limit:
                return limit + 1
        return length

    def _deadline_exceeded(self, deadline: float) -> bool:
        try:
            return self.clock() >= deadline
        except Exception:
            return True

    @staticmethod
    def _is_protected_request(request: GovernanceRequest) -> bool:
        tool_name = (request.tool_name or "").casefold()
        if tool_name in {
            "read",
            "write",
            "edit",
            "multiedit",
            "notebookedit",
            "grep",
            "glob",
            "search",
            "webfetch",
            "websearch",
        }:
            return True
        command = request.tool_input.get("command")
        if not isinstance(command, str):
            return False
        normalized = " ".join(command.casefold().split())
        return normalized.startswith(
            (
                "git diff",
                "git show",
                "cat ",
                "type ",
                "get-content ",
                "grep ",
                "rg ",
            )
        )

    @staticmethod
    def _risk_for_reason(reason_code: ReasonCode) -> Risk:
        if reason_code in (
            ReasonCode.SECRET_DETECTED,
            ReasonCode.PROTECTED_CONTENT,
        ):
            return Risk.HIGH
        if reason_code in (
            ReasonCode.PAYLOAD_TOO_LARGE,
            ReasonCode.ORIGINAL_TOO_LARGE,
            ReasonCode.STRATEGY_MATCHED,
        ):
            return Risk.MEDIUM
        return Risk.UNAVAILABLE

    def _passthrough_result(
        self,
        *,
        content: str,
        source_kind: SourceKind | None,
        reason_code: ReasonCode,
        risk: Risk,
        confidence: Confidence = Confidence.UNAVAILABLE,
        token_count: int | None = None,
    ) -> GovernanceResult:
        if token_count is None:
            token_count = estimate_tokens(content)
        result = GovernanceResult(
            action=Action.PASSTHROUGH,
            content=content,
            risk=risk,
            reason_code=reason_code,
            strategy=Strategy.PASSTHROUGH,
            confidence=confidence,
            preservation_check=None,
            token_before=token_count,
            token_after=token_count,
            tokens_saved=0,
            receipt_id=None,
        )
        if self.events is not None and source_kind is not None:
            try:
                self.events.record_event(
                    source_kind,
                    result.action,
                    result.risk,
                    result.reason_code,
                    result.token_before,
                    None,
                )
            except Exception:
                pass
        return result


class _BuiltinCommandFamilyClassifier:
    _TEST = re.compile(
        r"^(?:(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?\s+-m\s+)?"
        r"(?:\S*[\\/])?(?:pytest|py\.test|unittest|jest|vitest|mocha)(?:\.exe)?(?:\s|$)"
        r"|^(?:cargo|go|dotnet)\s+test(?:\s|$)"
        r"|^(?:npm|pnpm|yarn)(?:\.cmd|\.exe)?\s+(?:run\s+)?test(?:\s|$)",
        re.IGNORECASE,
    )
    _BUILD = re.compile(
        r"^(?:npm|pnpm|yarn)(?:\.cmd|\.exe)?\s+(?:run\s+)?build(?:\s|$)"
        r"|^(?:cargo|dotnet)\s+build(?:\s|$)"
        r"|^(?:cmake\s+--build|make|ninja|msbuild|webpack|vite\s+build|tsc)(?:\s|$)",
        re.IGNORECASE,
    )
    _LOG = re.compile(
        r"^(?:tail\b.*\.(?:log|out|txt)\b|journalctl\b|docker\s+logs\b|"
        r"kubectl\s+logs\b|get-content\b.*\s-wait(?:\s|$))",
        re.IGNORECASE,
    )

    def classify(self, request: GovernanceRequest) -> CommandFamily:
        command = request.tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return CommandFamily.UNKNOWN
        normalized = command.strip()
        if self._TEST.search(normalized):
            return CommandFamily.TEST
        if self._BUILD.search(normalized):
            return CommandFamily.BUILD
        if self._LOG.search(normalized):
            return CommandFamily.GENERIC_SHELL
        return CommandFamily.UNKNOWN


def default_governance_config(ledger_path: str | Path) -> GovernanceConfig:
    path = Path(ledger_path).expanduser().resolve()
    return GovernanceConfig(
        ledger=LedgerConfig(path=path, retention_days=DEFAULT_RETENTION_DAYS),
        policy=PolicyConfig(
            enabled_strategies=(
                Strategy.TEST_OUTPUT,
                Strategy.BUILD_OUTPUT,
                Strategy.REPETITIVE_LOG,
            ),
            protected_content_behavior=ProtectedContentBehavior.PASSTHROUGH,
            persistence_mode=PersistenceMode.TRANSFORMED_ONLY,
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            max_stored_original_bytes=DEFAULT_MAX_STORED_ORIGINAL_BYTES,
            hook_deadline_ms=DEFAULT_HOOK_DEADLINE_MS,
            literal_secret_markers=(),
        ),
        gateway=GatewayConfig(
            request_timeout_seconds=DEFAULT_GATEWAY_REQUEST_TIMEOUT_SECONDS,
            backends=(),
            tool_policy=GatewayToolPolicyConfig(allow=(), deny=()),
        ),
        config_path=path.parent / "token-governance.config.json",
    )


def create_governance_engine(
    ledger: ContextLedger,
    *,
    config: GovernanceConfig | None = None,
) -> GovernanceEngine:
    registry = BuiltinShaperRegistry()
    return GovernanceEngine(
        ledger=ledger,
        policy=PolicyEngine(
            classifier=_BuiltinCommandFamilyClassifier(),
            registry=registry,
        ),
        config=config or default_governance_config(ledger.path),
        events=ledger,
        registry=registry,
        verifier=PreservationVerifier(),
    )
