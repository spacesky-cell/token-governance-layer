from __future__ import annotations

from dataclasses import replace

import pytest

from token_governance.contracts import (
    CommandResult,
    GovernanceMode,
    GovernanceRequest,
    ShaperDiagnosticCode,
    SourceKind,
    Strategy,
)
from token_governance.preservation import PreservationVerifier
from token_governance.shapers import (
    BuiltinShaperRegistry,
    normalize_request_output,
    reconstruct_original,
)


def request(
    stdout: str,
    *,
    command: str = "tail -f app.log",
    stderr: str = "",
    exit_code: int | None = 0,
    interrupted: bool = False,
) -> GovernanceRequest:
    return GovernanceRequest(
        source_kind=SourceKind.CLAUDE_HOOK,
        tool_name="Bash",
        tool_input={"command": command},
        command_result=CommandResult(stdout, stderr, exit_code, interrupted),
        raw_text=stdout,
        payload_bytes=len(stdout.encode("utf-8")),
        mode=GovernanceMode.AUTO,
    )


def test_repetitive_log_matcher_and_shape_keep_middle_fact_and_exact_order():
    original = "tick\ntick\ntick\ntick\nmiddle fact: 你好\ntock\ntock\ntock\n"
    registry = BuiltinShaperRegistry()
    case = request(original)

    assert registry.matcher_for(Strategy.REPETITIVE_LOG).matches(case) is True
    shaped = registry.shape(Strategy.REPETITIVE_LOG, case)

    assert "middle fact: 你好\n" in shaped.content
    assert len(shaped.content.encode("utf-8")) < len(
        normalize_request_output(case).encode("utf-8")
    )
    assert [item.code for item in shaped.diagnostics] == [
        ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES,
    ]
    assert [(item.start_line, item.end_line, item.occurrence_count) for item in shaped.diagnostics] == [
        (1, 4, 4),
    ]
    assert reconstruct_original(shaped.content, shaped.diagnostics) == normalize_request_output(case)


@pytest.mark.parametrize(
    ("command", "content"),
    [
        ("cat service.py", "def run():\n    return 1\n" * 3),
        ("git diff", "diff --git a/a.py b/a.py\n+print('x')\n" * 3),
        ("cat data.json", '{"items": [1, 2, 3]}\n' * 4),
        ("rg needle src", "src/a.py:1:needle\n" * 4),
        ("custom-command", "alpha\nbeta\ngamma\n"),
    ],
)
def test_repetitive_matcher_rejects_source_diff_json_search_and_unknown(command, content):
    registry = BuiltinShaperRegistry()
    case = request(content, command=command)

    assert registry.matcher_for(Strategy.REPETITIVE_LOG).matches(case) is False
    with pytest.raises(ValueError, match="not applicable"):
        registry.shape(Strategy.REPETITIVE_LOG, case)


def test_pytest_failure_preserves_failure_block_stderr_and_status_exactly():
    stdout = (
        "============================= test session starts =============================\n"
        "collecting ...\ncollecting ...\ncollecting ...\n"
        "FAILED tests/test_api.py::test_value - AssertionError: expected 2\n"
        "E       assert 1 == 2\n"
        "=========================== 1 failed, 2 passed in 0.12s =========================\n"
    )
    stderr = "WARNING tests/test_api.py:18: resource not closed\n"
    case = request(stdout, command="python -m pytest -q", stderr=stderr, exit_code=1)
    registry = BuiltinShaperRegistry()

    assert registry.matcher_for(Strategy.TEST_OUTPUT).matches(case) is True
    shaped = registry.shape(Strategy.TEST_OUTPUT, case)

    for exact in (
        "FAILED tests/test_api.py::test_value - AssertionError: expected 2\n",
        "E       assert 1 == 2\n",
        "=========================== 1 failed, 2 passed in 0.12s =========================\n",
        stderr,
        "exit_code=1\n",
        "interrupted=false\n",
    ):
        assert exact in shaped.content
    assert reconstruct_original(shaped.content, shaped.diagnostics) == normalize_request_output(case)


def test_build_progress_collapses_but_warning_failed_target_and_summary_survive():
    stdout = (
        "[1/5] compiling a.cc\n[2/5] compiling b.cc\n[3/5] compiling c.cc\n"
        "src/main.cc:9: warning: unused variable 'x'\n"
        "FAILED: app\n"
        "ninja: build stopped: subcommand failed.\n"
    )
    case = request(stdout, command="cmake --build build", exit_code=1)
    registry = BuiltinShaperRegistry()

    assert registry.matcher_for(Strategy.BUILD_OUTPUT).matches(case) is True
    shaped = registry.shape(Strategy.BUILD_OUTPUT, case)

    assert shaped.diagnostics[0].code is ShaperDiagnosticCode.COLLAPSED_PROGRESS
    assert shaped.diagnostics[0].occurrence_count == 3
    for exact in (
        "src/main.cc:9: warning: unused variable 'x'\n",
        "FAILED: app\n",
        "ninja: build stopped: subcommand failed.\n",
        "exit_code=1\n",
    ):
        assert exact in shaped.content
    assert reconstruct_original(shaped.content, shaped.diagnostics) == normalize_request_output(case)


def test_empty_unicode_long_line_and_terminal_newline_are_reconstructable():
    registry = BuiltinShaperRegistry()
    assert normalize_request_output(request("")) .startswith("[TGL structured output: stdout]\n")

    long_line = "界" * 10_000
    case = request(f"{long_line}\n{long_line}\n{long_line}\ntail-without-newline")
    shaped = registry.shape(Strategy.REPETITIVE_LOG, case)

    assert reconstruct_original(shaped.content, shaped.diagnostics) == normalize_request_output(case)
    normalized = reconstruct_original(shaped.content, shaped.diagnostics)
    assert f"{long_line}\ntail-without-newline\n[TGL structured output: stderr]" in normalized


def test_matchers_are_deterministic_and_do_not_assign_risk():
    registry = BuiltinShaperRegistry()
    case = request("pulse\npulse\npulse\npulse\n")
    matcher = registry.matcher_for(Strategy.REPETITIVE_LOG)

    assert matcher.matches(case) is matcher.matches(replace(case)) is True
    assert not hasattr(matcher, "risk")


def test_cli_raw_repetitive_text_shapes_and_restores_terminal_newline_exactly():
    text = "raw-status\r\nraw-status\r\nraw-status\r\nunique-without-newline"
    case = GovernanceRequest(
        source_kind=SourceKind.CLI,
        tool_name=None,
        tool_input={},
        command_result=None,
        raw_text=text,
        payload_bytes=len(text.encode()),
        mode=GovernanceMode.MANUAL,
    )
    registry = BuiltinShaperRegistry()

    assert registry.matcher_for(Strategy.REPETITIVE_LOG).matches(case) is True
    shaped = registry.shape(Strategy.REPETITIVE_LOG, case)

    assert shaped.diagnostics
    assert reconstruct_original(shaped.content, shaped.diagnostics) == text
    assert not shaped.content.endswith(("\n", "\r"))


@pytest.mark.parametrize("source_kind", [SourceKind.CLI, SourceKind.MCP])
@pytest.mark.parametrize(
    ("strategy", "text"),
    [
        (Strategy.REPETITIVE_LOG, "pulse\npulse\npulse\npulse\nunique\n"),
        (
            Strategy.TEST_OUTPUT,
            "============================= test session starts =============================\n"
            "collecting ...\ncollecting ...\ncollecting ...\n"
            "============================== 3 passed in 0.10s ==============================\n",
        ),
        (
            Strategy.BUILD_OUTPUT,
            "[1/3] compiling a.cc\n[2/3] compiling b.cc\n[3/3] compiling c.cc\n"
            "build succeeded\n",
        ),
    ],
)
def test_commandless_manual_surfaces_shape_only_structurally_matched_content(
    source_kind,
    strategy,
    text,
):
    case = GovernanceRequest(
        source_kind=source_kind,
        tool_name=None,
        tool_input={},
        command_result=None,
        raw_text=text,
        payload_bytes=len(text.encode("utf-8")),
        mode=GovernanceMode.MANUAL,
    )
    registry = BuiltinShaperRegistry()

    assert registry.matcher_for(strategy).matches(case) is True
    shaped = registry.shape(strategy, case)

    assert shaped.diagnostics
    assert reconstruct_original(shaped.content, shaped.diagnostics) == text


def test_automatic_mcp_surface_is_never_applicable():
    text = "pulse\npulse\npulse\npulse\n"
    case = GovernanceRequest(
        source_kind=SourceKind.MCP,
        tool_name=None,
        tool_input={},
        command_result=None,
        raw_text=text,
        payload_bytes=len(text),
        mode=GovernanceMode.AUTO,
    )
    registry = BuiltinShaperRegistry()

    assert registry.matcher_for(Strategy.REPETITIVE_LOG).matches(case) is False


def test_complete_pytest_failure_block_is_never_collapsed():
    stdout = (
        "=============================== FAILURES ===============================\n"
        "____________________________ test_value ____________________________\n"
        "context detail\ncontext detail\ncontext detail\n"
        "E   AssertionError: expected 2\n"
        "=========================== 1 failed in 0.1s ===========================\n"
    )
    case = request(stdout, command="pytest -q", exit_code=1)

    shaped = BuiltinShaperRegistry().shape(Strategy.TEST_OUTPUT, case)

    assert shaped.content.count("context detail\n") == 3
    assert shaped.diagnostics == ()


@pytest.mark.parametrize(
    "text",
    [
        '{"event": "same"}\n{"event": "same"}\n{"event": "same"}\n',
        'print("same")\nprint("same")\nprint("same")\n',
    ],
)
def test_cli_json_lines_and_obvious_source_are_not_repetitive_logs(text):
    case = GovernanceRequest(
        source_kind=SourceKind.CLI,
        tool_name=None,
        tool_input={},
        command_result=None,
        raw_text=text,
        payload_bytes=len(text.encode()),
        mode=GovernanceMode.MANUAL,
    )

    assert (
        BuiltinShaperRegistry()
        .matcher_for(Strategy.REPETITIVE_LOG)
        .matches(case)
        is False
    )


def test_repeated_paths_are_protected_and_not_collapsed():
    text = (
        "heartbeat\nheartbeat\nheartbeat\n"
        "/srv/app/config.yaml\n/srv/app/config.yaml\n/srv/app/config.yaml\n"
    )
    case = request(text)
    registry = BuiltinShaperRegistry()

    assert registry.matcher_for(Strategy.REPETITIVE_LOG).matches(case) is True
    shaped = registry.shape(Strategy.REPETITIVE_LOG, case)

    assert shaped.content.count("/srv/app/config.yaml\n") == 3
    assert [item.original_content for item in shaped.diagnostics] == ["heartbeat\n"]


@pytest.mark.parametrize(
    ("command", "content", "strategy"),
    [
        (
            "opaque-tool --watch",
            "tick\ntick\ntick\ntick\n",
            Strategy.REPETITIVE_LOG,
        ),
        (
            "tail -f events.log",
            'service ready\n{"event":"pulse"}\n{"event":"pulse"}\n'
            '{"event":"pulse"}\n',
            Strategy.REPETITIVE_LOG,
        ),
        (
            "pytest -q --json-report",
            '{"summary":"3 passed"}\n',
            Strategy.TEST_OUTPUT,
        ),
        (
            "npm run build",
            'const message = "built in 1s";\n'
            'const message = "built in 1s";\n'
            'const message = "built in 1s";\n',
            Strategy.BUILD_OUTPUT,
        ),
    ],
)
def test_matchers_reject_unknown_or_conflicting_output(command, content, strategy):
    registry = BuiltinShaperRegistry()
    case = request(content, command=command)

    assert registry.matcher_for(strategy).matches(case) is False
    with pytest.raises(ValueError, match="not applicable"):
        registry.shape(strategy, case)


@pytest.mark.parametrize(
    ("command", "content", "strategy"),
    [
        (
            "pytest -q",
            "documentation example: 3 passed is not a test result\n",
            Strategy.TEST_OUTPUT,
        ),
        (
            "npm run build",
            "the phrase built in 1s appears in documentation\n",
            Strategy.BUILD_OUTPUT,
        ),
    ],
)
def test_matchers_do_not_use_keyword_only_output_proof(command, content, strategy):
    case = request(content, command=command)

    assert BuiltinShaperRegistry().matcher_for(strategy).matches(case) is False


@pytest.mark.parametrize(
    ("command", "strategy", "header"),
    [
        ("tail -f app.log", Strategy.REPETITIVE_LOG, "WARNING: retrying request\n"),
        ("pytest -q", Strategy.TEST_OUTPUT, "AssertionError: expected 2\n"),
        ("npm run build", Strategy.BUILD_OUTPUT, "warning: bundle fallback\n"),
    ],
)
def test_ordinary_multiline_diagnostic_blocks_are_preserved_exactly(
    command, strategy, header
):
    context = "  contextual detail\n" * 3
    trailer = (
        "\n1 failed in 0.1s\n"
        if strategy is Strategy.TEST_OUTPUT
        else "\nbuilt in 1s\n"
        if strategy is Strategy.BUILD_OUTPUT
        else "\nhealthy\n"
    )
    case = request(header + context + trailer, command=command, exit_code=1)

    shaped = BuiltinShaperRegistry().shape(strategy, case)

    assert header + context in shaped.content
    assert not any(item.original_content == "  contextual detail\n" for item in shaped.diagnostics)
    assert reconstruct_original(shaped.content, shaped.diagnostics) == normalize_request_output(case)


def test_reconstruction_uses_marker_position_when_original_contains_marker_text():
    original = "[TGL D 2-4 x3]\nrepeat\nrepeat\nrepeat\nend\n"
    case = request(original)

    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    assert shaped.content.count("[TGL D 2-4 x3]\n") == 2
    assert reconstruct_original(shaped.content, shaped.diagnostics) == normalize_request_output(case)


def test_interrupted_status_is_preserved_and_reconstructable():
    case = request("pulse\npulse\npulse\n", interrupted=True)

    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    assert "interrupted=true\n" in shaped.content
    assert reconstruct_original(shaped.content, shaped.diagnostics) == normalize_request_output(case)


@pytest.mark.parametrize(
    ("command", "content", "strategies"),
    [
        (
            "pytest -q | rg passed",
            "3 passed in 0.10s\n",
            (Strategy.TEST_OUTPUT,),
        ),
        (
            "npm run build | grep built",
            "built in 1s\n",
            (Strategy.BUILD_OUTPUT,),
        ),
        (
            "pytest -q; cat src/value.py",
            "3 passed in 0.10s\nVALUE = 1\n",
            (Strategy.TEST_OUTPUT,),
        ),
        (
            "pytest -q && npm run build",
            "3 passed in 0.10s\nbuilt in 1s\n",
            (Strategy.TEST_OUTPUT, Strategy.BUILD_OUTPUT),
        ),
    ],
)
def test_test_and_build_matchers_reject_conflicting_command_families(
    command, content, strategies
):
    registry = BuiltinShaperRegistry()
    case = request(content, command=command)

    for strategy in strategies:
        assert registry.matcher_for(strategy).matches(case) is False
        with pytest.raises(ValueError, match="not applicable"):
            registry.shape(strategy, case)


@pytest.mark.parametrize(
    ("command", "content", "strategy"),
    [
        ("echo pytest", "3 passed in 0.10s\n", Strategy.TEST_OUTPUT),
        ("echo npm run build", "built in 1s\n", Strategy.BUILD_OUTPUT),
        ("pytest -q | opaque-filter", "3 passed in 0.10s\n", Strategy.TEST_OUTPUT),
        ("npm run build | opaque-filter", "built in 1s\n", Strategy.BUILD_OUTPUT),
        (
            "tail -f app.log | opaque-filter",
            "pulse\npulse\npulse\n",
            Strategy.REPETITIVE_LOG,
        ),
    ],
)
def test_matchers_require_complete_registered_command_invocations(
    command, content, strategy
):
    registry = BuiltinShaperRegistry()
    case = request(content, command=command)

    assert registry.matcher_for(strategy).matches(case) is False
    with pytest.raises(ValueError, match="not applicable"):
        registry.shape(strategy, case)


@pytest.mark.parametrize(
    ("command", "content", "strategy"),
    [
        ("pytest -q\nopaque-filter", "3 passed in 0.10s\n", Strategy.TEST_OUTPUT),
        (
            "tail -f app.log\nopaque-filter",
            "pulse\npulse\npulse\n",
            Strategy.REPETITIVE_LOG,
        ),
        ("pytest -q $(opaque-filter)", "3 passed in 0.10s\n", Strategy.TEST_OUTPUT),
        ("npm run build `opaque-filter`", "built in 1s\n", Strategy.BUILD_OUTPUT),
    ],
)
def test_matchers_reject_multiline_and_shell_substitution_commands(
    command, content, strategy
):
    registry = BuiltinShaperRegistry()
    case = request(content, command=command)

    assert registry.matcher_for(strategy).matches(case) is False
    with pytest.raises(ValueError, match="not applicable"):
        registry.shape(strategy, case)


def test_single_quoted_shell_substitution_text_remains_a_literal():
    case = request(
        "3 passed in 0.10s\n",
        command="pytest -q -k 'value $(opaque-filter) `literal`'",
    )

    assert BuiltinShaperRegistry().matcher_for(Strategy.TEST_OUTPUT).matches(case) is True


@pytest.mark.parametrize(
    ("command", "content", "strategy"),
    [
        (
            "pytest -q 'x\\' | opaque-filter #'",
            "3 passed in 0.10s\n",
            Strategy.TEST_OUTPUT,
        ),
        (
            "tail -f app.log 'x\\' | opaque-filter #'",
            "heartbeat\nheartbeat\nheartbeat\n",
            Strategy.REPETITIVE_LOG,
        ),
    ],
)
def test_backslash_inside_single_quotes_does_not_hide_command_separator(
    command, content, strategy
):
    case = request(content, command=command)

    assert BuiltinShaperRegistry().matcher_for(strategy).matches(case) is False


@pytest.mark.parametrize(
    "command",
    [
        'pytest -q "x\\" | opaque-filter #"',
        "pytest -q value\\| opaque-filter",
    ],
)
def test_powershell_backslash_cannot_escape_shell_syntax(command):
    case = replace(
        request("3 passed in 0.10s\n", command=command),
        tool_name="PowerShell",
    )

    assert BuiltinShaperRegistry().matcher_for(Strategy.TEST_OUTPUT).matches(case) is False


def test_nine_byte_duplicate_span_is_not_replaced_by_a_larger_marker():
    case = request("ab\nab\nab\n")
    registry = BuiltinShaperRegistry()

    assert registry.matcher_for(Strategy.REPETITIVE_LOG).matches(case) is True
    shaped = registry.shape(Strategy.REPETITIVE_LOG, case)

    assert shaped.content == normalize_request_output(case)
    assert shaped.diagnostics == ()
    assert PreservationVerifier().verify(Strategy.REPETITIVE_LOG, case, shaped).ok is True


def test_short_registered_progress_span_is_not_replaced_by_a_larger_marker():
    case = request(".\nss\nFx\n3 passed in 0.10s\n", command="pytest -q")

    shaped = BuiltinShaperRegistry().shape(Strategy.TEST_OUTPUT, case)

    assert shaped.content == normalize_request_output(case)
    assert shaped.diagnostics == ()
    assert PreservationVerifier().verify(Strategy.TEST_OUTPUT, case, shaped).ok is True


def test_many_short_duplicate_groups_leave_the_whole_candidate_unchanged():
    original = "".join(f"{index:02}\n" * 3 + f"gap-{index}\n" for index in range(12))
    case = request(original)

    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    assert shaped.content == normalize_request_output(case)
    assert shaped.diagnostics == ()
    assert PreservationVerifier().verify(Strategy.REPETITIVE_LOG, case, shaped).ok is True


def test_mixed_spans_collapse_only_the_span_that_makes_output_smaller():
    short = "ab\n" * 3
    long_unit = "very long repetitive status line\n"
    case = request(short + "middle\n" + long_unit * 3)

    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    assert len(shaped.content.encode()) < len(normalize_request_output(case).encode())
    assert [item.original_content for item in shaped.diagnostics] == [long_unit]
    assert shaped.content.count("ab\n") == 3
    assert PreservationVerifier().verify(Strategy.REPETITIVE_LOG, case, shaped).ok is True
