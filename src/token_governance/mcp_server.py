from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import GovernanceConfig
from .contracts import GovernanceMode, GovernanceRequest, SourceKind, Strategy
from .core import create_governance_engine, default_governance_config
from .ledger import ContextLedger


GOVERN_MIGRATION_GUIDANCE = (
    "content_type/source were removed; use strategy "
    "auto|repetitive_log|test_output|build_output"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tgl-mcp")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the local SQLite ledger.",
    )
    parser.add_argument("--config", help="Path to token-governance.config.json.")
    args = parser.parse_args(argv)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.config:
        config = GovernanceConfig.load(
            args.config,
            cli_overrides={"ledger": {"path": args.db}} if args.db else None,
        )
    else:
        config = default_governance_config(
            args.db or str(Path.home() / ".token-governance" / "ledger.sqlite")
        )
    server = McpServer(ContextLedger(config.ledger.path), config=config)
    return server.run(sys.stdin, sys.stdout)


class McpServer:
    def __init__(
        self,
        ledger: ContextLedger,
        *,
        config: GovernanceConfig | None = None,
    ):
        self.ledger = ledger
        self.engine = create_governance_engine(ledger, config=config)
        self._pending_receipt_id: str | None = None

    def run(self, stdin: Any, stdout: Any) -> int:
        for line in stdin:
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            request = json.loads(line)
            response = self.handle(request)
            if response is None:
                continue
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
            if self._pending_receipt_id is not None:
                try:
                    self.ledger.mark_emitted(self._pending_receipt_id)
                except Exception:
                    pass
                finally:
                    self._pending_receipt_id = None
        return 0

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        self._pending_receipt_id = None
        if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return _result(
                    request_id,
                    {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {
                            "name": "token-governance-layer",
                            "version": "0.1.0",
                        },
                        "capabilities": {"tools": {}},
                    },
                )
            if method == "tools/list":
                return _result(request_id, {"tools": tool_definitions()})
            if method == "tools/call":
                params = request.get("params", {})
                return _result(request_id, self._call_tool(params))
            return _error(request_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            return _error(request_id, -32000, str(exc))

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        if name == "govern_context":
            if "content_type" in arguments or "source" in arguments:
                raise ValueError(GOVERN_MIGRATION_GUIDANCE)
            payload = arguments.get("payload", "")
            strategy_value = arguments.get("strategy", "auto")
            if not isinstance(payload, str):
                raise TypeError("payload must be a string")
            if strategy_value not in {
                "auto",
                "repetitive_log",
                "test_output",
                "build_output",
            }:
                raise ValueError(
                    "strategy must be auto|repetitive_log|test_output|build_output"
                )
            request = GovernanceRequest(
                source_kind=SourceKind.MCP,
                tool_name=None,
                tool_input={},
                command_result=None,
                raw_text=payload,
                payload_bytes=len(payload.encode("utf-8")),
                mode=GovernanceMode.MANUAL,
            )
            explicit = (
                None if strategy_value == "auto" else Strategy(strategy_value)
            )
            result = self.engine.govern_request(
                request,
                explicit_strategy=explicit,
            )
            self._pending_receipt_id = result.receipt_id
            return _text_result(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        if name == "retrieve_original":
            return _text_result(self.ledger.retrieve_original(str(arguments["receipt_id"])))
        if name == "explain_receipt":
            return _text_result(
                json.dumps(
                    self.ledger.explain_receipt(str(arguments["receipt_id"])),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        if name == "show_savings":
            return _text_result(json.dumps(self.ledger.savings(), ensure_ascii=False, indent=2))
        if name == "list_context_risks":
            return _text_result(
                json.dumps({"risks": self.ledger.risks()}, ensure_ascii=False, indent=2)
            )
        raise KeyError(f"Unknown tool: {name}")


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "govern_context",
            "description": "Apply conservative token governance to a payload and return a receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payload": {"type": "string"},
                    "strategy": {
                        "type": "string",
                        "enum": [
                            "auto",
                            "repetitive_log",
                            "test_output",
                            "build_output",
                        ],
                        "default": "auto",
                    },
                },
                "required": ["payload"],
            },
        },
        {
            "name": "retrieve_original",
            "description": "Retrieve the original payload for a receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {"receipt_id": {"type": "string"}},
                "required": ["receipt_id"],
            },
        },
        {
            "name": "explain_receipt",
            "description": "Explain a token governance receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {"receipt_id": {"type": "string"}},
                "required": ["receipt_id"],
            },
        },
        {
            "name": "show_savings",
            "description": "Show aggregate token savings recorded in the local ledger.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_context_risks",
            "description": "List receipts that were not classified as low risk.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


if __name__ == "__main__":
    raise SystemExit(main())
