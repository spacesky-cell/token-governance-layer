from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from .contracts import (
    Confidence,
    GovernanceMode,
    GovernanceRequest,
    ShaperDiagnostic,
    ShaperDiagnosticCode,
    ShaperResult,
    SourceKind,
    Strategy,
)


_STRUCTURED_STDOUT = "[TGL structured output: stdout]\n"
_STRUCTURED_STDERR = "[TGL structured output: stderr]\n"
_STRUCTURED_EXIT = "[TGL structured output: exit status]\n"
_GENERATED_MARKER_PREFIX = "[TGL "

_TEST_INVOCATION = re.compile(
    r"^\s*(?:(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?\s+-m\s+)?"
    r"(?:\S*[\\/])?(?:pytest|py\.test|unittest|jest|vitest|mocha)(?:\.exe)?(?:\s|$)"
    r"|^\s*(?:cargo|go|dotnet)\s+test(?:\s|$)"
    r"|^\s*(?:npm|pnpm|yarn)(?:\.cmd|\.exe)?\s+(?:run\s+)?test(?:\s|$)",
    re.IGNORECASE,
)
_BUILD_INVOCATION = re.compile(
    r"^\s*(?:npm|pnpm|yarn)(?:\.cmd|\.exe)?\s+(?:run\s+)?build(?:\s|$)"
    r"|^\s*(?:cargo|dotnet)\s+build(?:\s|$)"
    r"|^\s*(?:cmake\s+--build|make|ninja|msbuild|webpack|vite\s+build|tsc)(?:\s|$)",
    re.IGNORECASE,
)
_LOG_INVOCATION = re.compile(
    r"^\s*(?:tail\b.*\.(?:log|out|txt)\b|journalctl\b|docker\s+logs\b|"
    r"kubectl\s+logs\b|get-content\b.*\s-wait(?:\s|$))",
    re.IGNORECASE,
)
_SEARCH_OR_SOURCE_INVOCATION = re.compile(
    r"^\s*(?:cat|type|more|less|head|tail)\b"
    r"|^\s*git\s+(?:diff|show)\b"
    r"|^\s*(?:rg|grep|findstr|select-string)\b",
    re.IGNORECASE,
)
_SOURCE_SIGNATURE = re.compile(
    r"^\s*(?:diff --git |@@\s|[+\-]{3}\s|<<<<<<<\s|=======\s*$|>>>>>>>\s|"
    r"(?:async\s+)?def\s+\w+\s*\(|class\s+\w+.*:|"
    r"(?:export\s+)?(?:const|let|var|function|interface|type)\s+\w+|#include\s*[<\"]|"
    r"(?:print|require|include)\s*\(|(?:from|import|use|package|namespace)\s+\S+)",
    re.MULTILINE,
)
_TEST_OUTPUT_SIGNATURES = (
    re.compile(r"=+\s+test session starts\s+=+", re.IGNORECASE),
    re.compile(
        r"^=*\s*\d+\s+(?:passed|failed|errors?|skipped)"
        r"(?:,\s*\d+\s+(?:passed|failed|errors?|skipped))*"
        r"(?:\s+in\s+\S+)?\s*=*\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"^Ran \d+ tests?", re.MULTILINE),
    re.compile(r"^(?:FAILED|ERROR|OK)(?:\s|$)", re.MULTILINE),
    re.compile(r"^(?:Test Suites|Tests):", re.MULTILINE),
)
_BUILD_OUTPUT_SIGNATURES = (
    re.compile(r"^\[\d+/\d+\]\s+", re.MULTILINE),
    re.compile(r"^\s*(?:built in\s+\S+|build (?:succeeded|failed))\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(?:compil(?:e|ed|ing)|linking)\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^(?:FAILED:|ninja: build stopped|npm ERR!)", re.MULTILINE),
    re.compile(r"\berror TS\d+\b"),
)
_DIAGNOSTIC_START = re.compile(
    r"\b(?:error|exception|failed|failure|warning|warn|assert(?:ion)?(?:error)?|traceback|panic|fatal)\b"
    r"|(?:^|\s)(?:E\s+assert|FAILED:?)",
    re.IGNORECASE,
)
_PROTECTED = re.compile(
    r"\b(?:error|exception|failed|failure|warning|warn|assert(?:ion)?|traceback|panic|fatal)\b"
    r"|(?:^|\s)(?:E\s+assert|FAILED:?)"
    r"|(?:[A-Za-z]:)?[^\s:]+\.(?:py|pyi|js|jsx|ts|tsx|rs|go|java|c|cc|cpp|h|hpp):\d+",
    re.IGNORECASE,
)
_PATH = re.compile(
    r"(?:^|\s)(?:[A-Za-z]:\\|/)[A-Za-z_.][^\s]*"
    r"|(?:^|\s)(?:\.{0,2}/)?(?:[A-Za-z_.][\w.-]*/)+[\w.-]+"
)
_FINAL_TEST_SUMMARY = re.compile(
    r"(?:=+.*\b(?:passed|failed|errors?|skipped)\b.*=+|^Ran \d+ tests?|^(?:Test Suites|Tests):|^(?:OK|FAILED|ERROR)(?:\s|$))",
    re.IGNORECASE,
)
_FINAL_BUILD_SUMMARY = re.compile(
    r"(?:build (?:succeeded|failed)|built in|ninja: build stopped|^FAILED:|npm ERR!)",
    re.IGNORECASE,
)
_TEST_PROGRESS = (
    re.compile(r"^(?:collecting|collected) \.{3}(?:\s|$)", re.IGNORECASE),
    re.compile(r"^[.sFxX]+\s*(?:\[\s*\d+%\])?\s*$"),
)
_BUILD_PROGRESS = (
    re.compile(r"^\[\d+/\d+\]\s+(?:compil(?:e|ing)|linking|building|generating)\b.*$", re.IGNORECASE),
    re.compile(r"^\s*\d+%\s+(?:compil(?:e|ing)|building|linking)\b.*$", re.IGNORECASE),
)


def _command(request: GovernanceRequest) -> str:
    value = request.tool_input.get("command", "")
    return value if isinstance(value, str) else ""


def _output_for_matching(request: GovernanceRequest) -> str:
    result = request.command_result
    if result is None:
        return request.raw_text
    return result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except (TypeError, ValueError):
        return False
    return True


def _looks_like_json_lines(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        if line[0] not in "[{":
            return False
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            return False
        if not isinstance(value, (dict, list)):
            return False
    return True


def _contains_json_record(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "[{":
            continue
        try:
            value = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(value, (dict, list)):
            return True
    return False


def _has_conflicting_output(text: str) -> bool:
    return (
        _looks_like_json(text)
        or _looks_like_json_lines(text)
        or _contains_json_record(text)
        or bool(_SOURCE_SIGNATURE.search(text))
    )


def _command_segments(command: str) -> tuple[str, ...] | None:
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        character = command[index]
        if quote:
            if quote != "'" and (
                character == "`"
                or (character == "$" and index + 1 < len(command) and command[index + 1] == "(")
            ):
                return None
            current.append(character)
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            current.append(character)
            index += 1
            continue
        if character == "`" or (
            character == "$"
            and index + 1 < len(command)
            and command[index + 1] == "("
        ):
            return None
        if character in {";", "|", "&", "\r", "\n"}:
            segment = "".join(current).strip()
            if not segment:
                return None
            segments.append(segment)
            current = []
            if (
                character == "\r"
                and index + 1 < len(command)
                and command[index + 1] == "\n"
            ):
                index += 1
            elif index + 1 < len(command) and command[index + 1] == character:
                index += 1
            index += 1
            continue
        current.append(character)
        index += 1

    if quote:
        return None
    segment = "".join(current).strip()
    if not segment:
        return None
    segments.append(segment)
    return tuple(segments)


def _invocation_family(segment: str) -> str | None:
    if _TEST_INVOCATION.search(segment):
        return "test"
    if _BUILD_INVOCATION.search(segment):
        return "build"
    if _LOG_INVOCATION.search(segment):
        return "log"
    if _SEARCH_OR_SOURCE_INVOCATION.search(segment):
        return "source_or_search"
    return None


def _registered_command_family(command: str) -> str | None:
    segments = _command_segments(command)
    if not segments:
        return None
    families = {_invocation_family(segment) for segment in segments}
    if None in families or len(families) != 1:
        return None
    family = families.pop()
    return family if family in {"test", "build", "log"} else None


def _eligible_surface(request: GovernanceRequest) -> bool:
    if request.source_kind in {SourceKind.CLI, SourceKind.MCP}:
        return (
            request.mode is GovernanceMode.MANUAL
            and request.tool_name is None
        )
    return (
        request.source_kind is SourceKind.CLAUDE_HOOK
        and request.mode is GovernanceMode.AUTO
        and (request.tool_name or "").casefold() in {"bash", "powershell"}
    )


def _commandless_manual_surface(request: GovernanceRequest) -> bool:
    return (
        request.source_kind in {SourceKind.CLI, SourceKind.MCP}
        and request.mode is GovernanceMode.MANUAL
        and request.tool_name is None
        and not _command(request)
        and request.command_result is None
    )


def _has_required_status(request: GovernanceRequest) -> bool:
    return request.command_result is not None and request.command_result.exit_code is not None


def _line_units(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def normalize_request_output(request: GovernanceRequest) -> str:
    """Return the deterministic text representation shaped by this module."""
    if not isinstance(request, GovernanceRequest):
        raise TypeError("request must be a GovernanceRequest")
    result = request.command_result
    if result is None:
        return request.raw_text

    sections = [_STRUCTURED_STDOUT, result.stdout]
    if result.stdout and not result.stdout.endswith(("\n", "\r")):
        sections.append("\n")
    sections.extend((_STRUCTURED_STDERR, result.stderr))
    if result.stderr and not result.stderr.endswith(("\n", "\r")):
        sections.append("\n")
    sections.extend(
        (
            _STRUCTURED_EXIT,
            f"exit_code={'unknown' if result.exit_code is None else result.exit_code}\n",
            f"interrupted={str(result.interrupted).lower()}\n",
        )
    )
    return "".join(sections)


def _protected_line(line: str, *, section: str, strategy: Strategy) -> bool:
    text = line.rstrip("\r\n")
    if section not in {"stdout", "raw"}:
        return True
    if text.startswith(_GENERATED_MARKER_PREFIX):
        return True
    if _PROTECTED.search(text) or _PATH.search(text):
        return True
    if strategy is Strategy.TEST_OUTPUT and _FINAL_TEST_SUMMARY.search(text):
        return True
    if strategy is Strategy.BUILD_OUTPUT and _FINAL_BUILD_SUMMARY.search(text):
        return True
    return False


def _protected_flags(
    units: Sequence[str],
    sections: Sequence[str],
    strategy: Strategy,
) -> list[bool]:
    flags = [
        _protected_line(unit, section=section, strategy=strategy)
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
            r"=+\s*(?:FAILURES|ERRORS)\s*=+",
            text,
            re.IGNORECASE,
        ):
            failure_block = True
        if strategy is Strategy.BUILD_OUTPUT and re.match(
            r"^(?:FAILED:|ninja: build stopped|npm ERR!)",
            text,
            re.IGNORECASE,
        ):
            failure_block = True
        if traceback_block or failure_block or diagnostic_block:
            flags[index] = True
        if traceback_block and not text:
            traceback_block = False
        if failure_block and (
            (strategy is Strategy.TEST_OUTPUT and _FINAL_TEST_SUMMARY.search(text))
            or (strategy is Strategy.BUILD_OUTPUT and _FINAL_BUILD_SUMMARY.search(text))
        ):
            failure_block = False
        if diagnostic_block and not text:
            diagnostic_block = False
    return flags


def _normalized_sections(request: GovernanceRequest) -> tuple[str, list[str], list[str]]:
    normalized = normalize_request_output(request)
    units = _line_units(normalized)
    sections: list[str] = []
    section = "raw" if request.command_result is None else "metadata"
    for unit in units:
        if unit == _STRUCTURED_STDOUT:
            section = "metadata"
        elif unit == _STRUCTURED_STDERR:
            section = "metadata"
        elif unit == _STRUCTURED_EXIT:
            section = "metadata"
        else:
            if sections and units[len(sections) - 1] == _STRUCTURED_STDOUT:
                section = "stdout"
            elif sections and units[len(sections) - 1] == _STRUCTURED_STDERR:
                section = "stderr"
            elif sections and units[len(sections) - 1] == _STRUCTURED_EXIT:
                section = "exit"
        sections.append(section)
    return normalized, units, sections


def _progress_line(strategy: Strategy, line: str) -> bool:
    text = line.rstrip("\r\n")
    patterns = _TEST_PROGRESS if strategy is Strategy.TEST_OUTPUT else _BUILD_PROGRESS
    return any(pattern.fullmatch(text) for pattern in patterns)


def _marker(diagnostic: ShaperDiagnostic, *, newline: bool) -> str:
    kind = (
        "D"
        if diagnostic.code is ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES
        else "P"
    )
    suffix = "\n" if newline else ""
    return (
        f"[TGL {kind} {diagnostic.start_line}-{diagnostic.end_line} "
        f"x{diagnostic.occurrence_count}]{suffix}"
    )


def _marker_is_smaller(
    diagnostic: ShaperDiagnostic,
    span: Sequence[str],
) -> bool:
    original = "".join(span)
    marker = _marker(diagnostic, newline=original.endswith(("\n", "\r")))
    return len(marker.encode("utf-8")) < len(original.encode("utf-8"))


def _shape(strategy: Strategy, request: GovernanceRequest) -> ShaperResult:
    normalized, units, sections = _normalized_sections(request)
    protected = _protected_flags(units, sections, strategy)
    diagnostics: list[ShaperDiagnostic] = []
    index = 0
    while index < len(units):
        if protected[index]:
            index += 1
            continue

        duplicate_end = index + 1
        while (
            duplicate_end < len(units)
            and sections[duplicate_end] == sections[index]
            and units[duplicate_end] == units[index]
            and not protected[duplicate_end]
        ):
            duplicate_end += 1
        if duplicate_end - index >= 3:
            diagnostic = ShaperDiagnostic(
                code=ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES,
                start_line=index,
                end_line=duplicate_end - 1,
                occurrence_count=duplicate_end - index,
                original_content=units[index],
            )
            if _marker_is_smaller(diagnostic, units[index:duplicate_end]):
                diagnostics.append(diagnostic)
            index = duplicate_end
            continue

        if strategy in {Strategy.TEST_OUTPUT, Strategy.BUILD_OUTPUT} and _progress_line(
            strategy, units[index]
        ):
            progress_end = index + 1
            while (
                progress_end < len(units)
                and sections[progress_end] == sections[index]
                and not protected[progress_end]
                and _progress_line(strategy, units[progress_end])
            ):
                progress_end += 1
            if progress_end - index >= 3:
                diagnostic = ShaperDiagnostic(
                    code=ShaperDiagnosticCode.COLLAPSED_PROGRESS,
                    start_line=index,
                    end_line=progress_end - 1,
                    occurrence_count=progress_end - index,
                    original_content="".join(units[index:progress_end]),
                )
                if _marker_is_smaller(diagnostic, units[index:progress_end]):
                    diagnostics.append(diagnostic)
                index = progress_end
                continue
        index += 1

    by_start = {item.start_line: item for item in diagnostics}
    candidate: list[str] = []
    index = 0
    while index < len(units):
        diagnostic = by_start.get(index)
        if diagnostic is None:
            candidate.append(units[index])
            index += 1
            continue
        span = units[diagnostic.start_line : diagnostic.end_line + 1]
        candidate.append(_marker(diagnostic, newline="".join(span).endswith(("\n", "\r"))))
        index = diagnostic.end_line + 1

    facts = tuple(
        unit
        for unit, is_protected in zip(units, protected)
        if is_protected
    )
    content = "".join(candidate)
    if len(content.encode("utf-8")) >= len(normalized.encode("utf-8")):
        return ShaperResult(normalized, facts, ())
    return ShaperResult(content, facts, tuple(diagnostics))


@dataclass(frozen=True)
class _Matcher:
    strategy: Strategy
    predicate: Callable[[GovernanceRequest], bool]
    confidence: Confidence = Confidence.HIGH

    def matches(self, request: GovernanceRequest) -> bool:
        return isinstance(request, GovernanceRequest) and self.predicate(request)


def _matches_test(request: GovernanceRequest) -> bool:
    if not _eligible_surface(request):
        return False
    command = _command(request)
    output = _output_for_matching(request)
    manual = _commandless_manual_surface(request)
    if not manual and (
        not _has_required_status(request)
        or _registered_command_family(command) != "test"
    ):
        return False
    if _has_conflicting_output(output):
        return False
    return any(
        signature.search(output) for signature in _TEST_OUTPUT_SIGNATURES
    )


def _matches_build(request: GovernanceRequest) -> bool:
    if not _eligible_surface(request):
        return False
    command = _command(request)
    output = _output_for_matching(request)
    manual = _commandless_manual_surface(request)
    if not manual and (
        not _has_required_status(request)
        or _registered_command_family(command) != "build"
    ):
        return False
    if _has_conflicting_output(output):
        return False
    return any(
        signature.search(output) for signature in _BUILD_OUTPUT_SIGNATURES
    )


def _matches_repetitive(request: GovernanceRequest) -> bool:
    if not _eligible_surface(request):
        return False
    command = _command(request)
    output = _output_for_matching(request)
    if not output:
        return False
    command_family = _registered_command_family(command) if command else None
    if _commandless_manual_surface(request):
        pass
    elif command_family != "log":
        return False
    if _has_conflicting_output(output):
        return False
    units = _line_units(output)
    if len(units) < 3:
        return False
    repeated = 0
    index = 0
    while index < len(units):
        end = index + 1
        while end < len(units) and units[end] == units[index]:
            end += 1
        count = end - index
        if count >= 3 and not (
            _PROTECTED.search(units[index]) or _PATH.search(units[index])
        ):
            repeated += count
        index = end
    return repeated >= 3 and repeated / len(units) >= 0.5


class BuiltinShaperRegistry:
    def __init__(self) -> None:
        self._matchers = {
            Strategy.TEST_OUTPUT: _Matcher(Strategy.TEST_OUTPUT, _matches_test),
            Strategy.BUILD_OUTPUT: _Matcher(Strategy.BUILD_OUTPUT, _matches_build),
            Strategy.REPETITIVE_LOG: _Matcher(
                Strategy.REPETITIVE_LOG,
                _matches_repetitive,
            ),
        }

    def matcher_for(self, strategy: Strategy) -> _Matcher:
        if not isinstance(strategy, Strategy) or strategy not in self._matchers:
            raise ValueError("strategy is not registered")
        return self._matchers[strategy]

    def shape(self, strategy: Strategy, request: GovernanceRequest) -> ShaperResult:
        matcher = self.matcher_for(strategy)
        if not matcher.matches(request):
            raise ValueError(f"strategy {strategy} is not applicable")
        return _shape(strategy, request)


def reconstruct_original(
    candidate: str,
    diagnostics: Sequence[ShaperDiagnostic],
) -> str:
    """Restore the normalized original from a candidate and validated diagnostics."""
    if not isinstance(candidate, str):
        raise TypeError("candidate must be a string")
    restored_units = _line_units(candidate)
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, ShaperDiagnostic):
            raise TypeError("diagnostics must contain ShaperDiagnostic values")
        original = (
            diagnostic.original_content * diagnostic.occurrence_count
            if diagnostic.code
            is ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES
            else diagnostic.original_content
        )
        marker = _marker(diagnostic, newline=original.endswith(("\n", "\r")))
        marker_index = diagnostic.start_line
        if marker_index >= len(restored_units) or restored_units[marker_index] != marker:
            raise ValueError("candidate does not contain the diagnostic marker")
        restored_units[marker_index : marker_index + 1] = _line_units(original)
    return "".join(restored_units)


def _expected_candidate(original: str, diagnostics: Sequence[ShaperDiagnostic]) -> str:
    units = _line_units(original)
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
        output.append(_marker(diagnostic, newline=span.endswith(("\n", "\r"))))
        index = diagnostic.end_line + 1
    return "".join(output)


def _is_allowed_progress(strategy: Strategy, line: str) -> bool:
    return strategy in {Strategy.TEST_OUTPUT, Strategy.BUILD_OUTPUT} and _progress_line(
        strategy, line
    )
