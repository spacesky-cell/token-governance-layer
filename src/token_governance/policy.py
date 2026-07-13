from __future__ import annotations

from dataclasses import dataclass

from .config import GovernanceConfig
from .contracts import (
    STRATEGY_RECOGNITION_ORDER,
    Action,
    CommandFamily,
    CommandFamilyClassifier,
    Confidence,
    GovernanceRequest,
    GovernanceMode,
    PolicyDecision as GovernancePolicyDecision,
    ReasonCode,
    ShaperRegistry,
    SourceKind,
    Strategy,
)


PROTECTED_MARKERS = (
    "error",
    "exception",
    "failed",
    "failure",
    "assert",
    "traceback",
    " at ",
    ".py:",
    ".ts:",
    ".tsx:",
    ".js:",
    ".jsx:",
    ".rs:",
    ".go:",
)


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    risk: str
    policy: str
    notes: list[str]


class PolicyEngine:
    def __init__(
        self,
        *,
        min_tokens_for_compression: int = 80,
        classifier: CommandFamilyClassifier | None = None,
        registry: ShaperRegistry | None = None,
    ):
        self.min_tokens_for_compression = min_tokens_for_compression
        self.classifier = classifier
        self.registry = registry

    def decide(self, *, payload: str, content_type: str, token_before: int) -> PolicyDecision:
        if token_before < self.min_tokens_for_compression:
            return PolicyDecision(
                action="passthrough",
                risk="low",
                policy="default-conservative",
                notes=["Payload is below compression threshold."],
            )

        if content_type in {"user_instruction", "secret", "security"}:
            return PolicyDecision(
                action="passthrough",
                risk="medium",
                policy="default-conservative",
                notes=[f"{content_type} is protected from semantic compression."],
            )

        return PolicyDecision(
            action="summarize",
            risk="low",
            policy="default-conservative",
            notes=[
                "Large payload summarized with protected lines preserved.",
                "Original payload stored in local ledger and can be restored by receipt_id.",
            ],
        )

    def is_protected_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(marker in lowered for marker in PROTECTED_MARKERS)

    def decide_request(
        self,
        request: GovernanceRequest,
        config: GovernanceConfig,
        *,
        explicit_strategy: Strategy | None = None,
    ) -> GovernancePolicyDecision:
        if not isinstance(request, GovernanceRequest) or not isinstance(
            config, GovernanceConfig
        ):
            return self._passthrough(ReasonCode.CLASSIFICATION_UNAVAILABLE)
        if self.classifier is None or self.registry is None:
            return self._passthrough(ReasonCode.CLASSIFICATION_UNAVAILABLE)
        if explicit_strategy is not None:
            if (
                not isinstance(explicit_strategy, Strategy)
                or explicit_strategy not in STRATEGY_RECOGNITION_ORDER
            ):
                return self._passthrough(ReasonCode.NO_MATCHING_STRATEGY)
            if explicit_strategy not in config.policy.enabled_strategies:
                return self._passthrough(ReasonCode.STRATEGY_DISABLED)

        try:
            family = self.classifier.classify(request)
            if not isinstance(family, CommandFamily):
                return self._passthrough(ReasonCode.CLASSIFICATION_UNAVAILABLE)
            if not self._surface_allows(request, family):
                return self._passthrough(ReasonCode.NO_MATCHING_STRATEGY)

            candidates = (
                (explicit_strategy,)
                if explicit_strategy is not None
                else STRATEGY_RECOGNITION_ORDER
            )
            matches: list[tuple[Strategy, Confidence]] = []
            for strategy in candidates:
                if strategy not in config.policy.enabled_strategies:
                    continue
                matcher = self.registry.matcher_for(strategy)
                if matcher.strategy is not strategy:
                    return self._passthrough(
                        ReasonCode.CLASSIFICATION_UNAVAILABLE
                    )
                matched = matcher.matches(request)
                if not isinstance(matched, bool):
                    return self._passthrough(
                        ReasonCode.CLASSIFICATION_UNAVAILABLE
                    )
                if not matched or not self._family_allows(
                    strategy,
                    family,
                    request,
                ):
                    continue
                confidence = getattr(matcher, "confidence", Confidence.HIGH)
                if not isinstance(confidence, Confidence):
                    return self._passthrough(
                        ReasonCode.CLASSIFICATION_UNAVAILABLE
                    )
                matches.append((strategy, confidence))
        except Exception:
            return self._passthrough(ReasonCode.CLASSIFICATION_UNAVAILABLE)

        if not matches:
            return self._passthrough(ReasonCode.NO_MATCHING_STRATEGY)
        if len(matches) > 1:
            return self._passthrough(ReasonCode.AMBIGUOUS_STRATEGY)
        strategy, confidence = matches[0]
        if confidence is not Confidence.HIGH:
            return self._passthrough(ReasonCode.NO_MATCHING_STRATEGY)
        return GovernancePolicyDecision(
            action=Action.TRANSFORM,
            strategy=strategy,
            reason_code=ReasonCode.STRATEGY_MATCHED,
            confidence=confidence,
        )

    @staticmethod
    def _surface_allows(
        request: GovernanceRequest,
        family: CommandFamily,
    ) -> bool:
        if request.source_kind is SourceKind.CLAUDE_HOOK:
            return (
                request.mode is GovernanceMode.AUTO
                and (request.tool_name or "").casefold() in {"bash", "powershell"}
                and family is not CommandFamily.UNKNOWN
            )
        if request.source_kind is SourceKind.CLI:
            return request.tool_name is None
        return False

    @staticmethod
    def _family_allows(
        strategy: Strategy,
        family: CommandFamily,
        request: GovernanceRequest,
    ) -> bool:
        if strategy is Strategy.TEST_OUTPUT:
            return family is CommandFamily.TEST
        if strategy is Strategy.BUILD_OUTPUT:
            return family is CommandFamily.BUILD
        if strategy is not Strategy.REPETITIVE_LOG:
            return False
        if family is not CommandFamily.UNKNOWN:
            return True
        return (
            request.source_kind is SourceKind.CLI
            and request.tool_name is None
        )

    @staticmethod
    def _passthrough(reason_code: ReasonCode) -> GovernancePolicyDecision:
        return GovernancePolicyDecision(
            action=Action.PASSTHROUGH,
            strategy=Strategy.PASSTHROUGH,
            reason_code=reason_code,
            confidence=Confidence.UNAVAILABLE,
        )
