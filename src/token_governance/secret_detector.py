from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import GovernanceRequest, ReasonCode


_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,255}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b",
        r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,255}\b",
        r"\bglpat-[A-Za-z0-9_-]{20,255}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b",
        r"\bsk_(?:live|test)_[A-Za-z0-9]{20,255}\b",
        r"\bAIza[A-Za-z0-9_-]{30,64}\b",
        r"\bhf_[A-Za-z0-9]{20,255}\b",
        r"\bnpm_[A-Za-z0-9]{20,255}\b",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\bauthorization[\"']?[ \t]{0,32}[:=][ \t]{0,32}[\"']?"
        r"(?:bearer|basic)[ \t]{1,32}[^\s,;\"']{1,512}",
        r"-----BEGIN (?:[A-Z0-9 ]{1,64} )?PRIVATE KEY-----",
        r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
        r"[\"']?[ \t]{0,32}[:=][ \t]{0,32}[\"']?"
        r"[^\s,;\"']{1,512}",
        r"\b(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_ACCESS_KEY_ID|"
        r"AWS_SECRET_ACCESS_KEY|GITHUB_TOKEN|NPM_TOKEN)[\"']?"
        r"[ \t]{0,32}[:=][ \t]{0,32}[\"']?[^\s,;\"']{1,512}",
    )
)

_SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "openai_api_key",
        "anthropic_api_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "github_token",
        "npm_token",
        "authorization",
    }
)


@dataclass(frozen=True)
class SecretDetectionResult:
    detected: bool
    reason_code: ReasonCode


class SecretDetector:
    def detect(
        self,
        request: GovernanceRequest,
        *,
        literal_secret_markers: tuple[str, ...],
    ) -> SecretDetectionResult:
        if not isinstance(request, GovernanceRequest):
            raise TypeError("request must be a GovernanceRequest")
        if not isinstance(literal_secret_markers, tuple) or not all(
            isinstance(marker, str) for marker in literal_secret_markers
        ):
            raise TypeError("literal_secret_markers must be a tuple of strings")

        texts = [request.raw_text]
        if request.command_result is not None:
            texts.extend(
                (request.command_result.stdout, request.command_result.stderr)
            )

        detected = any(
            self._text_contains_secret(text, literal_secret_markers)
            for text in texts
        ) or self._value_contains_secret(
            request.tool_input,
            literal_secret_markers,
        )
        return SecretDetectionResult(
            detected=detected,
            reason_code=(
                ReasonCode.SECRET_DETECTED
                if detected
                else ReasonCode.NO_MATCHING_STRATEGY
            ),
        )

    @staticmethod
    def _text_contains_secret(text: str, literal_markers: tuple[str, ...]) -> bool:
        if any(marker and marker in text for marker in literal_markers):
            return True
        return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)

    def _value_contains_secret(
        self,
        value: Any,
        literal_markers: tuple[str, ...],
    ) -> bool:
        if isinstance(value, str):
            return self._text_contains_secret(value, literal_markers)
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized_key = key.casefold().replace("-", "_")
                if normalized_key in _SECRET_FIELD_NAMES:
                    return True
                if self._value_contains_secret(item, literal_markers):
                    return True
            return False
        if isinstance(value, tuple):
            return any(
                self._value_contains_secret(item, literal_markers) for item in value
            )
        return False
