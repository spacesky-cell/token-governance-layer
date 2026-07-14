from __future__ import annotations

from dataclasses import replace

import pytest

from token_governance.contracts import (
    CommandResult,
    GovernanceMode,
    GovernanceRequest,
    ReasonCode,
    ShaperDiagnostic,
    ShaperDiagnosticCode,
    ShaperResult,
    SourceKind,
    Strategy,
)
from token_governance.preservation import PreservationVerifier
from token_governance.shapers import BuiltinShaperRegistry


def request(stdout: str, *, stderr: str = "", exit_code: int | None = 0):
    return GovernanceRequest(
        source_kind=SourceKind.CLAUDE_HOOK,
        tool_name="Bash",
        tool_input={"command": "tail app.log"},
        command_result=CommandResult(stdout, stderr, exit_code, False),
        raw_text=stdout,
        payload_bytes=len(stdout.encode()),
        mode=GovernanceMode.AUTO,
    )


def verify(case, shaped, strategy=Strategy.REPETITIVE_LOG):
    return PreservationVerifier().verify(strategy, case, shaped)


def test_independent_verifier_accepts_exact_reconstructable_result():
    case = request("same\nsame\nsame\nsame\nunique middle\n")
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    result = verify(case, shaped)

    assert result.ok is True
    assert result.reason_code is ReasonCode.PRESERVATION_PASSED
    assert result.missing_fact_count == 0
    assert result.protected_fact_count >= 3


def test_verifier_ignores_claimed_facts_and_rejects_deleted_unique_middle_fact():
    case = request("same\nsame\nsame\ncritical unique fact\ntail\n")
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)
    malicious = replace(
        shaped,
        content=shaped.content.replace("critical unique fact\n", ""),
        candidate_protected_facts=("critical unique fact\n",),
    )

    result = verify(case, malicious)

    assert result.ok is False
    assert result.reason_code is ReasonCode.PRESERVATION_FAILED
    assert result.missing_fact_count > 0


@pytest.mark.parametrize("mutation", ["range", "overlap", "count", "content", "code"])
def test_verifier_rejects_forged_diagnostic_fields(mutation):
    case = request("duplicate\nduplicate\nduplicate\nduplicate\nend\n")
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)
    diagnostic = shaped.diagnostics[0]
    if mutation == "range":
        forged = replace(diagnostic, end_line=99)
        diagnostics = (forged,)
    elif mutation == "overlap":
        forged = replace(diagnostic, start_line=1)
        diagnostics = (diagnostic, forged)
    elif mutation == "count":
        diagnostics = (replace(diagnostic, occurrence_count=3),)
    elif mutation == "content":
        diagnostics = (replace(diagnostic, original_content="other\n"),)
    else:
        diagnostics = (replace(diagnostic, code=ShaperDiagnosticCode.COLLAPSED_PROGRESS),)

    result = verify(case, replace(shaped, diagnostics=diagnostics))

    assert result.ok is False
    assert result.reason_code is ReasonCode.PRESERVATION_FAILED


def test_verifier_rejects_modified_error_block_even_when_diagnostic_claims_it():
    case = request(
        "work\nwork\nwork\nERROR src/app.py:9 exploded\nTraceback (most recent call last):\n"
    )
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)
    malicious = replace(
        shaped,
        content=shaped.content.replace("ERROR src/app.py:9 exploded", "ERROR handled"),
        candidate_protected_facts=("ERROR src/app.py:9 exploded\n",),
    )

    result = verify(case, malicious)

    assert result.ok is False
    assert result.missing_fact_count > 0


def test_verifier_rejects_diagnostic_marker_or_sequence_tampering():
    case = request("duplicate\nduplicate\nduplicate\nduplicate\nend\n")
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    marker_changed = replace(shaped, content=shaped.content.replace("x4]", "x3]"))
    reordered = replace(shaped, content=shaped.content.replace("end\n", "") + "end\n")

    assert verify(case, marker_changed).ok is False
    assert verify(case, reordered).ok is False


def test_verifier_accepts_string_original_and_rejects_wrong_strategy_progress():
    original = "[1/3] build\n[2/3] build\n[3/3] build\nsummary\n"
    diagnostic = ShaperDiagnostic(
        code=ShaperDiagnosticCode.COLLAPSED_PROGRESS,
        start_line=0,
        end_line=2,
        occurrence_count=3,
        original_content="[1/3] build\n[2/3] build\n[3/3] build\n",
    )
    shaped = ShaperResult(
        content="[[TGL:collapsed_progress lines=0-2 count=3]]\nsummary\n",
        candidate_protected_facts=(),
        diagnostics=(diagnostic,),
    )

    assert PreservationVerifier().verify(Strategy.REPETITIVE_LOG, original, shaped).ok is False


def test_verifier_rejects_passthrough_strategy_and_non_contract_inputs():
    shaped = ShaperResult("x", (), ())

    assert PreservationVerifier().verify(Strategy.PASSTHROUGH, "x", shaped).ok is False
    assert PreservationVerifier().verify(Strategy.REPETITIVE_LOG, object(), shaped).ok is False


def test_verifier_does_not_trust_shaper_progress_predicate(monkeypatch):
    original = "unique one\nunique two\nunique three\nsummary\n"
    diagnostic = ShaperDiagnostic(
        code=ShaperDiagnosticCode.COLLAPSED_PROGRESS,
        start_line=0,
        end_line=2,
        occurrence_count=3,
        original_content="unique one\nunique two\nunique three\n",
    )
    shaped = ShaperResult(
        content="[TGL P 0-2 x3]\nsummary\n",
        candidate_protected_facts=(),
        diagnostics=(diagnostic,),
    )
    monkeypatch.setattr(
        "token_governance.shapers._progress_line",
        lambda strategy, line: True,
    )

    assert PreservationVerifier().verify(Strategy.TEST_OUTPUT, original, shaped).ok is False


def test_fact_count_is_the_exact_number_of_distinct_original_units():
    case = request("same\nsame\nsame\nsame\nunique middle\n")
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    result = verify(case, shaped)

    assert result.ok is True
    assert result.protected_fact_count == 7
    assert result.missing_fact_count == 0


def test_missing_fact_count_reports_each_distinct_deleted_unit():
    case = request(
        "duplicate\nduplicate\nduplicate\nduplicate\nunique one\nunique two\nend\n"
    )
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)
    malicious = replace(
        shaped,
        content=shaped.content.replace("unique one\n", "").replace("unique two\n", ""),
    )

    result = verify(case, malicious)

    assert result.ok is False
    assert result.protected_fact_count == 9
    assert result.missing_fact_count == 2


def test_missing_fact_count_is_not_a_constant_for_wholesale_tampering():
    case = request("duplicate\nduplicate\nduplicate\nunique\n")
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    result = verify(case, replace(shaped, content=""))

    assert result.ok is False
    assert result.protected_fact_count == 7
    assert result.missing_fact_count == 7


def test_verifier_independently_protects_ordinary_multiline_diagnostic_block():
    case = request(
        "WARNING: retrying request\n"
        "  contextual detail\n"
        "  contextual detail\n"
        "  contextual detail\n"
        "\nhealthy\n"
    )
    diagnostic = ShaperDiagnostic(
        code=ShaperDiagnosticCode.COLLAPSED_CONTIGUOUS_DUPLICATES,
        start_line=2,
        end_line=4,
        occurrence_count=3,
        original_content="  contextual detail\n",
    )
    malicious = ShaperResult(
        content=(
            "[TGL structured output: stdout]\n"
            "WARNING: retrying request\n"
            "[TGL D 2-4 x3]\n"
            "\nhealthy\n"
            "[TGL structured output: stderr]\n"
            "[TGL structured output: exit status]\n"
            "exit_code=0\ninterrupted=false\n"
        ),
        candidate_protected_facts=(),
        diagnostics=(diagnostic,),
    )

    result = verify(case, malicious)

    assert result.ok is False
    assert result.missing_fact_count >= 1


def test_verifier_accepts_shaper_output_when_original_contains_marker_text():
    case = request("[TGL D 2-4 x3]\nrepeat\nrepeat\nrepeat\nend\n")
    shaped = BuiltinShaperRegistry().shape(Strategy.REPETITIVE_LOG, case)

    result = verify(case, shaped)

    assert result.ok is True
    assert result.missing_fact_count == 0
