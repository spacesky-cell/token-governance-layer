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
    parser = argparse.ArgumentParser(prog="tgl-mcp")
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".token-governance" / "ledger.sqlite"),
        help="Path to the local SQLite ledger.",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    server = McpServer(ContextLedger(args.db))
    return server.run(sys.stdin, sys.stdout)


class McpServer:
    def __init__(self, ledger: ContextLedger):
        self.ledger = ledger
        self.engine = GovernanceEngine(ledger=ledger, policy=PolicyEngine())

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
        return 0

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
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
        if name == "govern_context":
            result = self.engine.govern_context(
                str(arguments.get("payload", "")),
                content_type=str(arguments.get("content_type", "text")),
                source=str(arguments.get("source", "mcp")),
            )
            return _text_result(json.dumps(result, ensure_ascii=False, indent=2))
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
                    "content_type": {"type": "string", "default": "text"},
                    "source": {"type": "string", "default": "mcp"},
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
