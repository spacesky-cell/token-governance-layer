from __future__ import annotations

import re
from typing import Sequence

from .contracts import (
    GovernanceRequest,
    ReasonCode,
    ShaperDiagnostic,
    ShaperDiagnosticCode,
    ShaperResult,
    Strategy,
    VerificationResult,
)
from .shapers import normalize_request_output


_STDOUT_HEADER = "[TGL structured output: stdout]\n"
_STDERR_HEADER = "[TGL structured output: stderr]\n"
_EXIT_HEADER = "[TGL structured output: exit status]\n"
_PROTECTED_FACT = re.compile(
    r"\b(?:error|exception|failed|failure|warning|warn|assert(?:ion)?|traceback|panic|fatal)\b"
    r"|(?:^|\s)(?:E\s+assert|FAILED:?)"
    r"|(?:[A-Za-z]:)?[^\s:]+\.(?:py|pyi|js|jsx|ts|tsx|rs|go|java|c|cc|cpp|h|hpp):\d+",
    re.IGNORECASE,
)
_PATH_FACT = re.compile(
    r"(?:^|\s)(?:[A-Za-z]:\\|/)[A-Za-z_.][^\s]*"
    r"|(?:^|\s)(?:\.{0,2}/)?(?:[A-Za-z_.][\w.-]*/)+[\w.-]+"
)
_TEST_SUMMARY = re.compile(
    r"(?:=+.*\b(?:passed|failed|errors?|skipped)\b.*=+|^Ran \d+ tests?|^(?:Test Suites|Tests):|^(?:OK|FAILED|ERROR)(?:\s|$))",
    re.IGNORECASE,
)
_BUILD_SUMMARY = re.compile(
    r"(?:build (?:succeeded|failed)|built in|ninja: build stopped|^FAILED:|npm ERR!)",
    re.IGNORECASE,
)
_TEST_PROGRESS_PATTERNS = (
    re.compile(r"^(?:collecting|collected) \.{3}(?:\s|$)", re.IGNORECASE),
    re.compile(r"^[.sFxX]+\s*(?:\[\s*\d+%\])?\s*$"),
)
_BUILD_PROGRESS_PATTERNS = (
    re.compile(r"^\[\d+/\d+\]\s+(?:compil(?:e|ing)|linking|building|generating)\b.*$", re.IGNORECASE),
    re.compile(r"^\s*\d+%\s+(?:compil(?:e|ing)|building|linking)\b.*$", re.IGNORECASE),
)
_DIAGNOSTIC_START = re.compile(
    r"\b(?:error|exception|failed|failure|warning|warn|assert(?:ion)?(?:error)?|traceback|panic|fatal)\b"
    r"|(?:^|\s)(?:E\s+assert|FAILED:?)",
    re.IGNORECASE,
)


def _units(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _sections_for(original: str | GovernanceRequest) -> tuple[str, list[str], list[str]]:
    if isinstance(original, str):
        units = _units(original)
        return original, units, ["raw"] * len(units)
    if not isinstance(original, GovernanceRequest):
        raise TypeError("original must be text or GovernanceRequest")
    normalized = normalize_request_output(original)
    units = _units(normalized)
    sections: list[str] = []
    section = "raw" if original.command_result is None else "metadata"
    previous = ""
    for unit in units:
        if unit in {_STDOUT_HEADER, _STDERR_HEADER, _EXIT_HEADER}:
            section = "metadata"
        elif previous == _STDOUT_HEADER:
            section = "stdout"
        elif previous == _STDERR_HEADER:
            section = "stderr"
        elif previous == _EXIT_HEADER:
            section = "exit"
        sections.append(section)
        previous = unit
    return normalized, units, sections


def _directly_protected(line: str, section: str, strategy: Strategy) -> bool:
    if section not in {"stdout", "raw"}:
        return True
    text = line.rstrip("\r\n")
    if (
        text.startswith("[TGL ")
        or _PROTECTED_FACT.search(text)
        or _PATH_FACT.search(text)
    ):
        return True
    if strategy is Strategy.TEST_OUTPUT and _TEST_SUMMARY.search(text):
        return True
    if strategy is Strategy.BUILD_OUTPUT and _BUILD_SUMMARY.search(text):
        return True
    return False


def _protected_flags(
    units: Sequence[str],
    sections: Sequence[str],
    strategy: Strategy,
) -> list[bool]:
    flags = [
        _directly_protected(unit, section, strategy)
        for unit, section in zip(units, sections)
    ]
    traceback_block = False
    failure_block = False
    diagnostic_block = False
    for index, (unit, section) in enumerate(zip(units, sections)):
        if section not in {"stdout", "raw"}:
            traceback_block = False
            failure_block = False
            diagnostic_block = False
            continue
        text = unit.rstrip("\r\n")
        if _DIAGNOSTIC_START.search(text):
            diagnostic_block = True
        if re.search(r"\bTraceback \(most recent call last\):", text):
            traceback_block = True
        if strategy is Strategy.TEST_OUTPUT and re.search(
            r"=+\s*(?:FAILURES|ERRORS)\s*=+", text, re.IGNORECASE
        ):
            failure_block = True
        if strategy is Strategy.BUILD_OUTPUT and re.match(
            r"^(?:FAILED:|ninja: build stopped|npm ERR!)", text, re.IGNORECASE
        ):
            failure_block = True
        if traceback_block or failure_block or diagnostic_block:
            flags[index] = True
        if traceback_block and not text:
            traceback_block = False
        if failure_block and (
            (strategy is Strategy.TEST_OUTPUT and _TEST_SUMMARY.search(text))
            or (strategy is Strategy.BUILD_OUTPUT and _BUILD_SUMMARY.search(text))
        ):
            failure_block = False
        if diagnostic_block and not text:
            diagnostic_block = False
    return flags


def _allowed_progress(strategy: Strategy, line: str) -> bool:
    text = line.rstrip("\r\n")
    if strategy is Strategy.TEST_OUTPUT:
        patterns = _TEST_PROGRESS_PATTERNS
    elif strategy is Strategy.BUILD_OUTPUT:
        patterns = _BUILD_PROGRESS_PATTERNS
    else:
        return False
    return any(pattern.fullmatch(text) for pattern in patterns)


def _marker(diagnostic: ShaperDiagnostic, span: str) -> str:
    kind = (
        "D"
        if diagnostic.code is ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES
        else "P"
    )
    newline = "\n" if span.endswith(("\n", "\r")) else ""
    return (
        f"[TGL {kind} {diagnostic.start_line}-{diagnostic.end_line} "
        f"x{diagnostic.occurrence_count}]{newline}"
    )


def _candidate_from_original(
    original: str,
    diagnostics: Sequence[ShaperDiagnostic],
) -> str:
    units = _units(original)
    by_start = {item.start_line: item for item in diagnostics}
    output: list[str] = []
    index = 0
    while index < len(units):
        diagnostic = by_start.get(index)
        if diagnostic is None:
            output.append(units[index])
            index += 1
            continue
        span = "".join(units[index : diagnostic.end_line + 1])
        output.append(_marker(diagnostic, span))
        index = diagnostic.end_line + 1
    return "".join(output)


def _restore_candidate(
    candidate: str,
    diagnostics: Sequence[ShaperDiagnostic],
) -> str:
    restored_units = _units(candidate)
    for diagnostic in diagnostics:
        original = (
            diagnostic.original_content * diagnostic.occurrence_count
            if diagnostic.code
            is ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES
            else diagnostic.original_content
        )
        marker = _marker(diagnostic, original)
        marker_index = diagnostic.start_line
        if marker_index >= len(restored_units) or restored_units[marker_index] != marker:
            raise ValueError("missing generated marker")
        restored_units[marker_index : marker_index + 1] = _units(original)
    return "".join(restored_units)


def _fact_units(units: Sequence[str]) -> tuple[str, ...]:
    if not units:
        return ("",)
    return tuple(dict.fromkeys(units))


def _accounted_fact_units(
    candidate: str,
    original_units: Sequence[str],
    diagnostics: Sequence[ShaperDiagnostic],
) -> set[str]:
    candidate_units = _units(candidate)
    accounted = set(candidate_units)
    removed_before = 0
    for diagnostic in diagnostics:
        candidate_index = diagnostic.start_line - removed_before
        span = original_units[diagnostic.start_line : diagnostic.end_line + 1]
        marker = _marker(diagnostic, "".join(span))
        if (
            0 <= candidate_index < len(candidate_units)
            and candidate_units[candidate_index] == marker
        ):
            accounted.update(span)
        removed_before += diagnostic.end_line - diagnostic.start_line
    return accounted


class PreservationVerifier:
    """Validate complete sequence preservation without trusting shaper claims."""

    def verify(
        self,
        strategy: Strategy,
        original: str | GovernanceRequest,
        result: ShaperResult,
    ) -> VerificationResult:
        try:
            normalized, units, sections = self._original_units(original)
            facts = _fact_units(units)
            fact_count = len(facts)
            if (
                strategy not in {
                    Strategy.REPETITIVE_LOG,
                    Strategy.TEST_OUTPUT,
                    Strategy.BUILD_OUTPUT,
                }
                or not isinstance(result, ShaperResult)
            ):
                return self._failed(fact_count, fact_count)
            diagnostics = result.diagnostics
            accounted = _accounted_fact_units(result.content, units, diagnostics)
            missing_count = sum(fact not in accounted for fact in facts)
            if not self._valid_diagnostics(
                strategy,
                units,
                sections,
                diagnostics,
            ):
                return self._failed(fact_count, missing_count)

            expected = _candidate_from_original(normalized, diagnostics)
            if result.content != expected:
                return self._failed(fact_count, missing_count)
            if _restore_candidate(result.content, diagnostics) != normalized:
                return self._failed(fact_count, missing_count)

            protected_flags = _protected_flags(units, sections, strategy)
            protected = tuple(
                unit
                for unit, is_protected in zip(units, protected_flags)
                if is_protected
            )
            candidate_units = set(_units(result.content))
            protected_missing = sum(fact not in candidate_units for fact in set(protected))
            if protected_missing:
                return self._failed(fact_count, max(missing_count, protected_missing))
        except Exception:
            return self._failed(1, 1)
        return VerificationResult(
            ok=True,
            protected_fact_count=fact_count,
            missing_fact_count=0,
            reason_code=ReasonCode.PRESERVATION_PASSED,
        )

    @staticmethod
    def _original_units(
        original: str | GovernanceRequest,
    ) -> tuple[str, list[str], list[str]]:
        return _sections_for(original)

    @staticmethod
    def _valid_diagnostics(
        strategy: Strategy,
        units: list[str],
        sections: list[str],
        diagnostics: Sequence[ShaperDiagnostic],
    ) -> bool:
        protected = _protected_flags(units, sections, strategy)
        previous_end = -1
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, ShaperDiagnostic):
                return False
            if diagnostic.start_line <= previous_end:
                return False
            if diagnostic.start_line < 0 or diagnostic.end_line >= len(units):
                return False
            span = units[diagnostic.start_line : diagnostic.end_line + 1]
            if diagnostic.occurrence_count != len(span) or len(span) < 3:
                return False
            span_protected = protected[
                diagnostic.start_line : diagnostic.end_line + 1
            ]
            if any(span_protected):
                return False

            if diagnostic.code is ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES:
                if any(unit != span[0] for unit in span):
                    return False
                if diagnostic.original_content != span[0]:
                    return False
            elif diagnostic.code is ShaperDiagnosticCode.COLLAPSED_PROGRESS:
                if strategy not in {Strategy.TEST_OUTPUT, Strategy.BUILD_OUTPUT}:
                    return False
                if diagnostic.original_content != "".join(span):
                    return False
                if not all(_allowed_progress(strategy, unit) for unit in span):
                    return False
            else:
                return False
            previous_end = diagnostic.end_line
        return True

    @staticmethod
    def _failed(fact_count: int, missing_count: int) -> VerificationResult:
        return VerificationResult(
            ok=False,
            protected_fact_count=fact_count,
            missing_fact_count=min(fact_count, max(1, missing_count)),
            reason_code=ReasonCode.PRESERVATION_FAILED,
        )
