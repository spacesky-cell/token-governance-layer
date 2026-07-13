from __future__ import annotations

import pytest

from token_governance.contracts import (
    CommandResult,
    GovernanceMode,
    GovernanceRequest,
    ReasonCode,
    SourceKind,
)
from token_governance.secret_detector import SecretDetector


def make_request(
    raw_text: str = "ordinary output",
    *,
    stdout: str = "",
    stderr: str = "",
    tool_input: dict | None = None,
) -> GovernanceRequest:
    return GovernanceRequest(
        source_kind=SourceKind.CLAUDE_HOOK,
        tool_name="Bash",
        tool_input=tool_input or {"command": "pytest -q"},
        command_result=CommandResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=0,
            interrupted=False,
        ),
        raw_text=raw_text,
        payload_bytes=len(raw_text.encode("utf-8")),
        mode=GovernanceMode.AUTO,
    )


@pytest.mark.parametrize(
    "secret_text",
    [
        "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890",
        "github_pat_" + "11AA0_exampleExampleExample123456789",
        "sk-proj-" + "abcdefghijklmnopqrstuvwxyz1234567890",
        "glpat-" + "abcdefghijklmnopqrstuvwxyz123456",
        "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx",
        "sk_live_" + "abcdefghijklmnopqrstuvwxyz1234567890",
        "AIza" + "SyABCDEFGHIJKLMNOPQRSTUVWXYZ123456789",
        "hf_" + "abcdefghijklmnopqrstuvwxyz1234567890",
        "OPENAI_API_KEY=" + "sk-" + "abcdefghijklmnopqrstuvwxyz1234567890",
        "AWS_ACCESS_KEY_ID=" + "AKIA" + "ABCDEFGHIJKLMNOP",
        "Author" + "ization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "-----BEGIN "
        + "PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
        "password" + " = " + "correct-horse-battery-staple",
    ],
)
@pytest.mark.parametrize("position", ["first", "middle", "end"])
def test_detects_fixed_secret_corpus_without_returning_payload(secret_text, position):
    parts = {
        "first": [secret_text, "普通输出", "done"],
        "middle": ["普通输出", secret_text, "done"],
        "end": ["普通输出", "done", secret_text],
    }[position]
    request = make_request("\n".join(parts))

    result = SecretDetector().detect(request, literal_secret_markers=())

    assert result.detected is True
    assert result.reason_code is ReasonCode.SECRET_DETECTED
    assert tuple(vars(result)) == ("detected", "reason_code")
    assert secret_text not in repr(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stdout", "npm token = npm_" + "abcdefghijklmnopqrstuvwxyz123456"),
        ("stderr", "Author" + "ization: Basic " + "dXNlcjpwYXNzd29yZA=="),
        ("tool_input", {"env": {"ANTHROPIC_API_KEY": "sensitive-value"}}),
        ("tool_input", {"nested": ["ok", {"password": "sensitive-value"}]}),
    ],
)
def test_scans_structured_command_result_and_json_tool_input(field, value):
    kwargs = {field: value}
    request = make_request(**kwargs)

    result = SecretDetector().detect(request, literal_secret_markers=())

    assert result.detected is True
    assert result.reason_code is ReasonCode.SECRET_DETECTED
    assert "sensitive-value" not in repr(result)


@pytest.mark.parametrize("field", ["stdout", "stderr", "tool_input"])
@pytest.mark.parametrize("position", ["first", "middle", "end"])
def test_structured_secret_corpus_covers_positions_and_unicode(field, position):
    marker = "秘密-marker-" + field + "-" + position
    parts = {
        "first": [marker, "普通输出", "done"],
        "middle": ["普通输出", marker, "done"],
        "end": ["普通输出", "done", marker],
    }[position]
    value = "\n".join(parts)
    kwargs = (
        {"tool_input": {"nested": ["开始", {"value": value}]}}
        if field == "tool_input"
        else {field: value}
    )

    result = SecretDetector().detect(
        make_request(**kwargs),
        literal_secret_markers=(marker,),
    )

    assert result.detected is True
    assert result.reason_code is ReasonCode.SECRET_DETECTED
    assert marker not in repr(result)


@pytest.mark.parametrize("field", ["raw_text", "stdout", "stderr"])
@pytest.mark.parametrize("position", ["first", "middle", "end"])
@pytest.mark.parametrize("secret_kind", ["authorization", "password", "api-key"])
def test_detects_json_quoted_secrets_in_text_fields(
    field, position, secret_kind
):
    sensitive_value = "quoted-sensitive-" + secret_kind + "-value"
    json_fragment = {
        "authorization": (
            '{"Author' + 'ization":"Bearer ' + sensitive_value + '"}'
        ),
        "password": '{"pass' + 'word":"' + sensitive_value + '"}',
        "api-key": '{"api-' + 'key":"' + sensitive_value + '"}',
    }[secret_kind]
    parts = {
        "first": [json_fragment, "普通输出", "done"],
        "middle": ["普通输出", json_fragment, "done"],
        "end": ["普通输出", "done", json_fragment],
    }[position]
    value = "\n".join(parts)
    kwargs = {field: value}

    result = SecretDetector().detect(
        make_request(**kwargs),
        literal_secret_markers=(),
    )

    assert result.detected is True
    assert result.reason_code is ReasonCode.SECRET_DETECTED
    assert sensitive_value not in repr(result)


@pytest.mark.parametrize("key", ["api-key", "apiKey"])
def test_detects_common_api_key_spellings_in_tool_input(key):
    sensitive_value = "structured-sensitive-value"

    result = SecretDetector().detect(
        make_request(tool_input={"nested": {key: sensitive_value}}),
        literal_secret_markers=(),
    )

    assert result.detected is True
    assert sensitive_value not in repr(result)


@pytest.mark.parametrize("secret_kind", ["authorization", "password"])
def test_quoted_secret_patterns_match_a_bounded_prefix_of_long_values(secret_kind):
    long_value = "x" * 2048
    text = (
        '{"Author' + 'ization":"Bearer ' + long_value + '"}'
        if secret_kind == "authorization"
        else '{"pass' + 'word":"' + long_value + '"}'
    )

    result = SecretDetector().detect(
        make_request(raw_text=text),
        literal_secret_markers=(),
    )

    assert result.detected is True
    assert long_value not in repr(result)


def test_literal_markers_are_matched_as_literal_text_not_regex():
    marker = "tenant.key[0]+literal"
    detector = SecretDetector()

    detected = detector.detect(
        make_request(f"prefix {marker} suffix"),
        literal_secret_markers=(marker,),
    )
    not_detected = detector.detect(
        make_request("tenantXkey000literal"),
        literal_secret_markers=(marker,),
    )

    assert detected.detected is True
    assert not_detected.detected is False
    assert not_detected.reason_code is ReasonCode.NO_MATCHING_STRATEGY


def test_ordinary_unicode_output_is_not_secret_like():
    request = make_request("构建成功\n42 tests passed\n访问令牌统计已更新")

    result = SecretDetector().detect(request, literal_secret_markers=())

    assert result.detected is False
    assert result.reason_code is ReasonCode.NO_MATCHING_STRATEGY
