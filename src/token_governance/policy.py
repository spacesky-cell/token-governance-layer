from __future__ import annotations

from dataclasses import dataclass


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
    def __init__(self, *, min_tokens_for_compression: int = 80):
        self.min_tokens_for_compression = min_tokens_for_compression

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
