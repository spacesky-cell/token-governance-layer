from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ledger import ContextLedger
from .policy import PolicyEngine
from .tokenizer import estimate_tokens


@dataclass
class GovernanceEngine:
    ledger: ContextLedger
    policy: PolicyEngine

    def govern_context(
        self,
        payload: str,
        *,
        content_type: str = "text",
        source: str = "unknown",
    ) -> dict[str, Any]:
        token_before = estimate_tokens(payload)
        decision = self.policy.decide(
            payload=payload,
            content_type=content_type,
            token_before=token_before,
        )

        if decision.action == "summarize":
            governed = self._summarize(payload, content_type=content_type)
        else:
            governed = payload

        token_after = estimate_tokens(governed)
        action = decision.action
        notes = list(decision.notes)
        if action == "summarize" and token_after >= token_before:
            governed = payload
            token_after = token_before
            action = "passthrough"
            notes = [
                "Summary was not shorter than the original payload.",
                "Original payload passed through unchanged and receipted.",
            ]

        receipt_id = self.ledger.record(
            source=source,
            content_type=content_type,
            action=action,
            risk=decision.risk,
            original_text=payload,
            governed_text=governed,
            token_before=token_before,
            token_after=token_after,
            policy=decision.policy,
            notes=notes,
        )

        return {
            "receipt_id": receipt_id,
            "content": governed,
            "content_type": content_type,
            "source": source,
            "action": action,
            "risk": decision.risk,
            "token_before": token_before,
            "token_after": token_after,
            "tokens_saved": token_before - token_after,
            "policy": decision.policy,
            "notes": notes,
        }

    def _summarize(self, payload: str, *, content_type: str) -> str:
        lines = payload.splitlines()
        protected = []
        for line in lines:
            if self.policy.is_protected_line(line):
                protected.append(line)

        protected = _dedupe_preserve_order(protected)[:20]
        head = lines[:5]
        tail = lines[-5:] if len(lines) > 10 else []
        sample = _dedupe_preserve_order(head + tail)

        sections = [
            f"[Token Governance Summary]",
            f"content_type: {content_type}",
            f"original_lines: {len(lines)}",
        ]
        if protected:
            sections.append("protected_lines:")
            sections.extend(f"- {line}" for line in protected)
        sections.append("representative_sample:")
        sections.extend(f"- {line}" for line in sample[:10])
        sections.append("restore: use retrieve_original(receipt_id) for full payload.")
        return "\n".join(sections)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
