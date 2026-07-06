from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import GovernanceEngine
from .ledger import ContextLedger
from .policy import PolicyEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tgl-claude-hook")
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".token-governance" / "claude-ledger.sqlite"),
        help="Path to the local SQLite ledger.",
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    payload_text = sys.stdin.read()
    payload = json.loads(payload_text) if payload_text.strip() else {}
    response = build_hook_response(payload, ledger=ContextLedger(args.db))
    print(json.dumps(response, ensure_ascii=False))
    return 0


def build_hook_response(payload: dict[str, Any], *, ledger: ContextLedger) -> dict[str, Any]:
    if payload.get("hook_event_name") != "PostToolUse":
        return {"continue": True}

    output = _extract_tool_output(payload)
    if not output:
        return {"continue": True}

    tool_name = str(payload.get("tool_name", "unknown"))
    engine = GovernanceEngine(ledger=ledger, policy=PolicyEngine())
    result = engine.govern_context(
        output,
        content_type=_content_type_for_tool(tool_name),
        source=f"claude-hook:{tool_name}",
    )

    if result["tokens_saved"] <= 0 or result["content"] == output:
        return {"continue": True}

    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": _format_updated_output(result),
        },
    }


def _extract_tool_output(payload: dict[str, Any]) -> str:
    for key in ("tool_output", "tool_response"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for field in ("stdout", "content", "text", "output"):
                field_value = value.get(field)
                if isinstance(field_value, str) and field_value:
                    return field_value
            stderr = value.get("stderr")
            if isinstance(stderr, str) and stderr:
                return stderr
        if value is not None:
            return json.dumps(value, ensure_ascii=False, indent=2)
    return ""


def _content_type_for_tool(tool_name: str) -> str:
    name = tool_name.lower()
    if name in {"bash", "powershell"}:
        return "command_output"
    if name in {"read", "grep", "glob", "ls"}:
        return "file_context"
    if name.startswith("mcp__"):
        return "mcp_tool_output"
    return "tool_output"


def _format_updated_output(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(result["content"]),
            "",
            "[Token Governance Receipt]",
            f"receipt_id: {result['receipt_id']}",
            f"tokens_saved: {result['tokens_saved']}",
            f"risk: {result['risk']}",
            "restore: call retrieve_original with this receipt_id if full output is needed.",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
