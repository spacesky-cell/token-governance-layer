from __future__ import annotations

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

class PolicyEngine:
    def __init__(
        self,
        *,
        classifier: CommandFamilyClassifier | None = None,
        registry: ShaperRegistry | None = None,
    ):
        self.classifier = classifier
        self.registry = registry

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
                    explicit_strategy=explicit_strategy,
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
            return (
                request.mode is GovernanceMode.MANUAL
                and request.tool_name is None
            )
        if request.source_kind is SourceKind.MCP:
            return (
                request.mode is GovernanceMode.MANUAL
                and request.tool_name is None
            )
        return False

    @staticmethod
    def _family_allows(
        strategy: Strategy,
        family: CommandFamily,
        request: GovernanceRequest,
        *,
        explicit_strategy: Strategy | None,
    ) -> bool:
        if (
            explicit_strategy is strategy
            and family is CommandFamily.UNKNOWN
            and request.source_kind in {SourceKind.CLI, SourceKind.MCP}
            and request.mode is GovernanceMode.MANUAL
            and request.tool_name is None
        ):
            return True
        if strategy is Strategy.TEST_OUTPUT:
            return family is CommandFamily.TEST
        if strategy is Strategy.BUILD_OUTPUT:
            return family is CommandFamily.BUILD
        if strategy is not Strategy.REPETITIVE_LOG:
            return False
        if family is not CommandFamily.UNKNOWN:
            return True
        return False

    @staticmethod
    def _passthrough(reason_code: ReasonCode) -> GovernancePolicyDecision:
        return GovernancePolicyDecision(
            action=Action.PASSTHROUGH,
            strategy=Strategy.PASSTHROUGH,
            reason_code=reason_code,
            confidence=Confidence.UNAVAILABLE,
        )
