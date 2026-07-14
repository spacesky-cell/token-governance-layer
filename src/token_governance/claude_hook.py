from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from .config import GovernanceConfig
from .contracts import (
    Action,
    CommandResult,
    GovernanceMode,
    GovernanceRequest,
    GovernanceResult,
    SourceKind,
)
from .core import create_governance_engine, default_governance_config
from .ledger import ContextLedger


INSTALL_STATE_SCHEMA_VERSION = 1
_PASSTHROUGH_RESPONSE = {"continue": True}


@dataclass(frozen=True)
class PreparedHookResponse:
    response: dict[str, Any]
    serialized: str
    receipt_id: str | None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tgl-claude-hook")
    parser.add_argument("--db", default=None, help="Path to the local SQLite ledger.")
    parser.add_argument("--config", help="Path to token-governance.config.json.")
    parser.add_argument("--install-state", help="Path to .tgl/install-state.json.")
    args = parser.parse_args(argv)

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    fallback = _fallback_prepared()
    try:
        payload_text = sys.stdin.read()
        payload = json.loads(payload_text) if payload_text.strip() else {}
        if not isinstance(payload, dict):
            return emit_prepared_response(fallback, stdout=sys.stdout, ledger=None)
        config = _load_config(args.config, args.db)
        ledger = ContextLedger(config.ledger.path)
        state_path = Path(args.install_state) if args.install_state else ledger.path.parent / "install-state.json"
        prepared = prepare_hook_response(
            payload,
            ledger=ledger,
            config=config,
            install_state_path=state_path,
        )
        return emit_prepared_response(prepared, stdout=sys.stdout, ledger=ledger)
    except Exception:
        return emit_prepared_response(fallback, stdout=sys.stdout, ledger=None)


def prepare_hook_response(
    payload: dict[str, Any],
    *,
    ledger: ContextLedger,
    install_state_path: str | Path,
    config: GovernanceConfig | None = None,
    serializer: Callable[[object], str] | None = None,
) -> PreparedHookResponse:
    serializer = serializer or _serialize
    if not _complete_install_state(Path(install_state_path)):
        return _fallback_prepared()
    request_and_output = _normalize_post_tool_use(payload)
    if request_and_output is None:
        return _fallback_prepared()
    request, original_output = request_and_output
    engine = create_governance_engine(
        ledger,
        config=config or default_governance_config(ledger.path),
    )
    captured: dict[str, Any] = {}

    def prepare(result: GovernanceResult) -> None:
        response = _transformed_response(original_output, result)
        serialized = serializer(response)
        if not isinstance(serialized, str):
            raise TypeError("serializer must return text")
        captured["response"] = response
        captured["serialized"] = serialized

    result = engine.govern_request(request, prepare_result=prepare)
    if result.action is not Action.TRANSFORM or result.receipt_id is None:
        return _fallback_prepared()
    response = captured.get("response")
    serialized = captured.get("serialized")
    if not isinstance(response, dict) or not isinstance(serialized, str):
        return _fallback_prepared()
    return PreparedHookResponse(response, serialized, result.receipt_id)


def build_hook_response(
    payload: dict[str, Any],
    *,
    ledger: ContextLedger,
    install_state_path: str | Path | None = None,
    config: GovernanceConfig | None = None,
) -> dict[str, Any]:
    state_path = (
        Path(install_state_path)
        if install_state_path is not None
        else ledger.path.parent / "install-state.json"
    )
    return prepare_hook_response(
        payload,
        ledger=ledger,
        config=config,
        install_state_path=state_path,
    ).response


def emit_prepared_response(
    prepared: PreparedHookResponse,
    *,
    stdout: TextIO,
    ledger: ContextLedger | None,
) -> int:
    try:
        stdout.write(prepared.serialized + "\n")
        stdout.flush()
    except Exception:
        return 0
    if prepared.receipt_id is not None and ledger is not None:
        try:
            ledger.mark_emitted(prepared.receipt_id)
        except Exception:
            pass
    return 0


def _normalize_post_tool_use(
    payload: Mapping[str, Any],
) -> tuple[GovernanceRequest, dict[str, Any]] | None:
    if payload.get("hook_event_name") != "PostToolUse":
        return None
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or tool_name.casefold() not in {"bash", "powershell"}:
        return None
    output = payload.get("tool_response", payload.get("tool_output"))
    if not isinstance(output, dict):
        return None
    stdout = output.get("stdout")
    stderr = output.get("stderr", "")
    interrupted = output.get("interrupted", False)
    if (
        not isinstance(stdout, str)
        or not stdout
        or not isinstance(stderr, str)
        or not isinstance(interrupted, bool)
    ):
        return None
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, Mapping):
        return None
    exit_code = _extract_exit_code(output)
    try:
        request = GovernanceRequest(
            source_kind=SourceKind.CLAUDE_HOOK,
            tool_name=tool_name,
            tool_input=tool_input,
            command_result=CommandResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                interrupted=interrupted,
            ),
            raw_text=stdout,
            payload_bytes=len(stdout.encode("utf-8")),
            mode=GovernanceMode.AUTO,
        )
    except (TypeError, ValueError, UnicodeError):
        return None
    return request, dict(output)


def _extract_exit_code(output: Mapping[str, Any]) -> int | None:
    for key in ("exitCode", "exit_code", "status"):
        value = output.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _transformed_response(
    original_output: dict[str, Any],
    result: GovernanceResult,
) -> dict[str, Any]:
    updated = dict(original_output)
    updated["stdout"] = "\n".join(
        [
            result.content,
            "",
            "[Token Governance Receipt]",
            f"receipt_id: {result.receipt_id}",
            f"tokens_saved: {result.tokens_saved}",
            f"risk: {result.risk.value}",
            "restore: call retrieve_original with this receipt_id if full output is needed.",
        ]
    )
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": updated,
        },
    }


def _complete_install_state(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and set(value) == {"schema_version", "status"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == INSTALL_STATE_SCHEMA_VERSION
        and type(value["status"]) is str
        and value["status"] == "complete"
    )


def _load_config(config_path: str | None, db_path: str | None) -> GovernanceConfig:
    if config_path is None:
        path = db_path or str(Path.home() / ".token-governance" / "claude-ledger.sqlite")
        return default_governance_config(path)
    overrides = {"ledger": {"path": db_path}} if db_path is not None else None
    return GovernanceConfig.load(config_path, cli_overrides=overrides)


def _serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fallback_prepared() -> PreparedHookResponse:
    return PreparedHookResponse(
        response=dict(_PASSTHROUGH_RESPONSE),
        serialized=_serialize(_PASSTHROUGH_RESPONSE),
        receipt_id=None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
