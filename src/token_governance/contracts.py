from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


class _ClosedEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class SourceKind(_ClosedEnum):
    CLAUDE_HOOK = "claude_hook"
    CLI = "cli"
    MCP = "mcp"
    MCP_GATEWAY = "mcp_gateway"


class GovernanceMode(_ClosedEnum):
    AUTO = "auto"
    MANUAL = "manual"


class CommandFamily(_ClosedEnum):
    UNKNOWN = "unknown"
    GENERIC_SHELL = "generic_shell"
    TEST = "test"
    BUILD = "build"


class Action(_ClosedEnum):
    PASSTHROUGH = "passthrough"
    TRANSFORM = "transform"


class Strategy(_ClosedEnum):
    PASSTHROUGH = "passthrough"
    REPETITIVE_LOG = "repetitive_log"
    TEST_OUTPUT = "test_output"
    BUILD_OUTPUT = "build_output"


STRATEGY_RECOGNITION_ORDER = (
    Strategy.TEST_OUTPUT,
    Strategy.BUILD_OUTPUT,
    Strategy.REPETITIVE_LOG,
)

MAX_TOOL_INPUT_DEPTH = 32


class Risk(_ClosedEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNAVAILABLE = "unavailable"


class Confidence(_ClosedEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNAVAILABLE = "unavailable"


class ProtectedContentBehavior(_ClosedEnum):
    PASSTHROUGH = "passthrough"


class PersistenceMode(_ClosedEnum):
    TRANSFORMED_ONLY = "transformed_only"


class ReasonCode(_ClosedEnum):
    TRANSFORMED = "transformed"
    STRATEGY_MATCHED = "strategy_matched"
    NO_MATCHING_STRATEGY = "no_matching_strategy"
    AMBIGUOUS_STRATEGY = "ambiguous_strategy"
    STRATEGY_DISABLED = "strategy_disabled"
    PROTECTED_CONTENT = "protected_content"
    SECRET_DETECTED = "secret_detected"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    ORIGINAL_TOO_LARGE = "original_too_large"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    MALFORMED_REQUEST = "malformed_request"
    CONFIG_INVALID = "config_invalid"
    SHAPER_FAILED = "shaper_failed"
    PRESERVATION_PASSED = "preservation_passed"
    PRESERVATION_FAILED = "preservation_failed"
    NOT_SMALLER = "not_smaller"
    LEDGER_FAILED = "ledger_failed"
    SERIALIZATION_FAILED = "serialization_failed"
    CLASSIFICATION_UNAVAILABLE = "classification_unavailable"


class ShaperDiagnosticCode(_ClosedEnum):
    COLLAPSED_CONTIGUOUS_DUPLICATES = "collapsed_contiguous_duplicates"
    COLLAPSED_PROGRESS = "collapsed_progress"


def _require_enum(name: str, value: object, enum_type: type[Enum]) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be a {enum_type.__name__}")


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{name} must be a non-negative integer")


def _freeze_json(
    value: Any,
    *,
    active: frozenset[int] = frozenset(),
    depth: int = 0,
) -> Any:
    if depth > MAX_TOOL_INPUT_DEPTH:
        raise TypeError("tool_input exceeds maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("tool_input numbers must be finite")
        return value
    if isinstance(value, Mapping):
        object_id = id(value)
        if object_id in active:
            raise TypeError("tool_input must not contain cycles")
        next_active = active | {object_id}
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("tool_input object keys must be strings")
            copied[key] = _freeze_json(
                item,
                active=next_active,
                depth=depth + 1,
            )
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        object_id = id(value)
        if object_id in active:
            raise TypeError("tool_input must not contain cycles")
        next_active = active | {object_id}
        return tuple(
            _freeze_json(item, active=next_active, depth=depth + 1)
            for item in value
        )
    raise TypeError("tool_input must contain only JSON-compatible values")


def _copy_string_tuple(name: str, value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    copied = tuple(value)
    if not all(isinstance(item, str) for item in copied):
        raise TypeError(f"{name} must be a sequence of strings")
    return copied


def _copy_diagnostic_tuple(
    value: Sequence["ShaperDiagnostic"],
) -> tuple["ShaperDiagnostic", ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("diagnostics must be a sequence of ShaperDiagnostic values")
    copied = tuple(value)
    if not all(isinstance(item, ShaperDiagnostic) for item in copied):
        raise TypeError("diagnostics must be a sequence of ShaperDiagnostic values")
    return copied


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int | None
    interrupted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("stdout and stderr must be strings")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None")
        if not isinstance(self.interrupted, bool):
            raise TypeError("interrupted must be a bool")


@dataclass(frozen=True)
class GovernanceRequest:
    source_kind: SourceKind
    tool_name: str | None
    tool_input: Mapping[str, Any]
    command_result: CommandResult | None
    raw_text: str
    payload_bytes: int
    mode: GovernanceMode

    def __post_init__(self) -> None:
        _require_enum("source_kind", self.source_kind, SourceKind)
        _require_enum("mode", self.mode, GovernanceMode)
        if self.tool_name is not None and not isinstance(self.tool_name, str):
            raise TypeError("tool_name must be a string or None")
        if self.command_result is not None and not isinstance(self.command_result, CommandResult):
            raise TypeError("command_result must be a CommandResult or None")
        if not isinstance(self.raw_text, str):
            raise TypeError("raw_text must be a string")
        _require_non_negative_int("payload_bytes", self.payload_bytes)
        if not isinstance(self.tool_input, Mapping):
            raise TypeError("tool_input must be an object")
        object.__setattr__(self, "tool_input", _freeze_json(self.tool_input))


@dataclass(frozen=True)
class PolicyDecision:
    action: Action
    strategy: Strategy
    reason_code: ReasonCode
    confidence: Confidence

    def __post_init__(self) -> None:
        _require_enum("action", self.action, Action)
        _require_enum("strategy", self.strategy, Strategy)
        _require_enum("reason_code", self.reason_code, ReasonCode)
        _require_enum("confidence", self.confidence, Confidence)
        if self.action is Action.TRANSFORM:
            if self.strategy not in STRATEGY_RECOGNITION_ORDER:
                raise ValueError("transform decisions require a registered strategy")
            if self.reason_code is not ReasonCode.STRATEGY_MATCHED:
                raise ValueError("transform decisions require strategy_matched reason")
            if self.confidence is Confidence.UNAVAILABLE:
                raise ValueError("transform decisions require available confidence")
        elif self.strategy is not Strategy.PASSTHROUGH:
            raise ValueError("passthrough decisions require the passthrough strategy")


@dataclass(frozen=True)
class ShaperDiagnostic:
    code: ShaperDiagnosticCode
    start_line: int
    end_line: int
    occurrence_count: int
    original_content: str

    def __post_init__(self) -> None:
        _require_enum("code", self.code, ShaperDiagnosticCode)
        _require_non_negative_int("start_line", self.start_line)
        _require_non_negative_int("end_line", self.end_line)
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        if (
            isinstance(self.occurrence_count, bool)
            or not isinstance(self.occurrence_count, int)
            or self.occurrence_count < 1
        ):
            raise TypeError("occurrence_count must be a positive integer")
        if not isinstance(self.original_content, str):
            raise TypeError("original_content must be a string")


@dataclass(frozen=True)
class ShaperResult:
    content: str
    candidate_protected_facts: tuple[str, ...]
    diagnostics: tuple[ShaperDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        object.__setattr__(
            self,
            "candidate_protected_facts",
            _copy_string_tuple("candidate_protected_facts", self.candidate_protected_facts),
        )
        object.__setattr__(
            self,
            "diagnostics",
            _copy_diagnostic_tuple(self.diagnostics),
        )


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    protected_fact_count: int
    missing_fact_count: int
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise TypeError("ok must be a bool")
        _require_non_negative_int("protected_fact_count", self.protected_fact_count)
        _require_non_negative_int("missing_fact_count", self.missing_fact_count)
        _require_enum("reason_code", self.reason_code, ReasonCode)
        if self.missing_fact_count > self.protected_fact_count:
            raise ValueError("missing_fact_count must not exceed protected_fact_count")
        if self.ok is not (self.missing_fact_count == 0):
            raise ValueError("ok must equal whether missing_fact_count is zero")
        expected_reason = (
            ReasonCode.PRESERVATION_PASSED
            if self.ok
            else ReasonCode.PRESERVATION_FAILED
        )
        if self.reason_code is not expected_reason:
            raise ValueError("reason_code must agree with preservation outcome")


@dataclass(frozen=True)
class GovernanceResult:
    action: Action
    content: str
    risk: Risk
    reason_code: ReasonCode
    strategy: Strategy
    confidence: Confidence
    preservation_check: VerificationResult | None
    token_before: int
    token_after: int
    tokens_saved: int
    receipt_id: str | None

    def __post_init__(self) -> None:
        _require_enum("action", self.action, Action)
        _require_enum("risk", self.risk, Risk)
        _require_enum("reason_code", self.reason_code, ReasonCode)
        _require_enum("strategy", self.strategy, Strategy)
        _require_enum("confidence", self.confidence, Confidence)
        if self.preservation_check is not None and not isinstance(
            self.preservation_check, VerificationResult
        ):
            raise TypeError("preservation_check must be a VerificationResult or None")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        _require_non_negative_int("token_before", self.token_before)
        _require_non_negative_int("token_after", self.token_after)
        _require_non_negative_int("tokens_saved", self.tokens_saved)
        if self.token_after > self.token_before:
            raise ValueError("token_after must not exceed token_before")
        if self.tokens_saved != self.token_before - self.token_after:
            raise ValueError("tokens_saved must equal token_before minus token_after")
        if self.receipt_id is not None and not isinstance(self.receipt_id, str):
            raise TypeError("receipt_id must be a string or None")
        if self.action is Action.TRANSFORM:
            if self.strategy not in STRATEGY_RECOGNITION_ORDER:
                raise ValueError("transformed results require a registered strategy")
            if self.reason_code is not ReasonCode.TRANSFORMED:
                raise ValueError("transformed results require the transformed reason")
            if self.risk not in (Risk.LOW, Risk.MEDIUM):
                raise ValueError("transformed results require low or medium risk")
            if self.confidence is Confidence.UNAVAILABLE:
                raise ValueError("transformed results require available confidence")
            if self.risk is Risk.LOW and self.confidence is not Confidence.HIGH:
                raise ValueError(
                    "low-risk transformed results require high confidence"
                )
            if self.token_after >= self.token_before or self.tokens_saved <= 0:
                raise ValueError("transformed results require positive token savings")
            if self.preservation_check is None or not self.preservation_check.ok:
                raise ValueError("transformed results require a passing preservation check")
            if not self.receipt_id:
                raise ValueError("transformed results require a non-empty receipt_id")
        else:
            if self.strategy is not Strategy.PASSTHROUGH:
                raise ValueError("passthrough results require the passthrough strategy")
            if self.receipt_id is not None:
                raise ValueError("passthrough results must not have a receipt_id")
            if self.token_after != self.token_before or self.tokens_saved != 0:
                raise ValueError("passthrough results must not report token savings")


@runtime_checkable
class ShaperMatcher(Protocol):
    strategy: Strategy

    def matches(self, request: GovernanceRequest) -> bool: ...


@runtime_checkable
class CommandFamilyClassifier(Protocol):
    def classify(self, request: GovernanceRequest) -> CommandFamily: ...


@runtime_checkable
class ShaperRegistry(Protocol):
    def matcher_for(self, strategy: Strategy) -> ShaperMatcher: ...

    def shape(self, strategy: Strategy, request: GovernanceRequest) -> ShaperResult: ...


@runtime_checkable
class LedgerPort(Protocol):
    def allocate_receipt_id(self) -> str: ...

    def store_prepared(
        self,
        receipt_id: str,
        original_text: str,
        result: GovernanceResult,
    ) -> None: ...

    def mark_emitted(self, receipt_id: str) -> None: ...


@runtime_checkable
class EventPort(Protocol):
    def record_event(
        self,
        source_kind: SourceKind,
        action: Action,
        risk: Risk,
        reason_code: ReasonCode,
        token_count: int,
        receipt_id: str | None,
    ) -> None: ...
