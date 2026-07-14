from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any

from .config import GovernanceConfig
from .contracts import GovernanceMode, GovernanceRequest, SourceKind, Strategy
from .core import create_governance_engine, default_governance_config
from .ledger import ContextLedger, LedgerIntegrityError


SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18",)
DEFAULT_RETRIEVE_MAX_CHARS = 4096
MAX_RETRIEVE_MAX_CHARS = 16384
GOVERN_MIGRATION_GUIDANCE = (
    "content_type/source were removed; use strategy "
    "auto|repetitive_log|test_output|build_output"
)
RETRIEVE_MIGRATION_GUIDANCE = (
    "Unbounded MCP retrieval was removed; use offset/max_chars pagination "
    "or the CLI receipt export for an exact full original"
)


class _ServerState(Enum):
    CREATED = "created"
    INITIALIZED = "initialized"
    ACTIVE = "active"


class _ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tgl-mcp")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the local SQLite ledger.",
    )
    parser.add_argument("--config", help="Path to token-governance.config.json.")
    args = parser.parse_args(argv)
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
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    return server.run(stdin, sys.stdout)


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
        self._state = _ServerState.CREATED
        self._cancelled_request_ids: set[str | int] = set()

    def run(self, stdin: Any, stdout: Any) -> int:
        for raw_line in stdin:
            try:
                line = (
                    raw_line.decode("utf-8")
                    if isinstance(raw_line, bytes)
                    else raw_line
                )
            except UnicodeDecodeError:
                response = _error(None, -32700, "Parse error")
            else:
                line = line.strip().lstrip("\ufeff")
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    response = _error(None, -32700, "Parse error")
                else:
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

    def handle(self, request: Any) -> dict[str, Any] | None:
        self._pending_receipt_id = None
        if not isinstance(request, dict):
            return _error(None, -32600, "Invalid Request")

        is_notification = "id" not in request
        request_id = request.get("id")
        method = request.get("method")
        if not is_notification and not (
            request_id is None
            or isinstance(request_id, str)
            or (isinstance(request_id, int) and not isinstance(request_id, bool))
        ):
            return _error(None, -32600, "Invalid Request")
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if is_notification:
                return None
            return _error(request_id, -32600, "Invalid Request")
        if is_notification:
            self._dispatch_notification(method, request.get("params", {}))
            return None

        try:
            if method == "initialize":
                return _result(request_id, self._initialize(request.get("params")))
            if self._state is not _ServerState.ACTIVE:
                raise _ProtocolError(-32002, "Server not initialized")
            if method == "tools/list":
                _require_empty_params(request.get("params", {}))
                return _result(request_id, {"tools": tool_definitions()})
            if method == "tools/call":
                params = _validate_tool_call_params(request.get("params"))
                return _result(request_id, self._call_tool(params))
            return _error(request_id, -32601, f"Method not found: {method}")
        except _ProtocolError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception:
            return _error(request_id, -32603, "Internal error")

    def _initialize(self, params: Any) -> dict[str, Any]:
        if self._state is not _ServerState.CREATED:
            raise _ProtocolError(-32600, "Server is already initialized")
        if not isinstance(params, dict):
            raise _ProtocolError(-32602, "Invalid initialize params")
        protocol_version = params.get("protocolVersion")
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            supported = ", ".join(SUPPORTED_PROTOCOL_VERSIONS)
            raise _ProtocolError(
                -32602,
                f"Unsupported protocol version; supported: {supported}",
            )
        if not isinstance(params.get("capabilities"), dict):
            raise _ProtocolError(-32602, "Invalid initialize params")
        client_info = params.get("clientInfo")
        if (
            not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not isinstance(client_info.get("version"), str)
        ):
            raise _ProtocolError(-32602, "Invalid initialize params")
        self._state = _ServerState.INITIALIZED
        return {
            "protocolVersion": protocol_version,
            "serverInfo": {
                "name": "token-governance-layer",
                "version": "0.1.0",
            },
            "capabilities": {"tools": {}},
        }

    def _dispatch_notification(self, method: str, params: Any) -> None:
        if method == "notifications/initialized":
            if self._state is _ServerState.INITIALIZED and params == {}:
                self._state = _ServerState.ACTIVE
            return
        if (
            method == "notifications/cancelled"
            and self._state is _ServerState.ACTIVE
            and isinstance(params, dict)
        ):
            request_id = params.get("requestId")
            if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
                if len(self._cancelled_request_ids) >= 1024:
                    self._cancelled_request_ids.clear()
                self._cancelled_request_ids.add(request_id)

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params["name"]
        arguments = params["arguments"]
        validation_error = _validate_tool_arguments(name, arguments)
        if validation_error is not None:
            return _tool_error("invalid_arguments", validation_error)
        try:
            if name == "govern_context":
                payload = arguments["payload"]
                strategy_value = arguments.get("strategy", "auto")
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
                return _text_result(
                    json.dumps(asdict(result), ensure_ascii=False, indent=2)
                )
            if name == "retrieve_original":
                original = self.ledger.retrieve_original(arguments["receipt_id"])
                offset = arguments.get("offset", 0)
                max_chars = arguments.get(
                    "max_chars", DEFAULT_RETRIEVE_MAX_CHARS
                )
                end = min(len(original), offset + max_chars)
                remaining = max(0, len(original) - end)
                page = {
                    "receipt_id": arguments["receipt_id"],
                    "text": original[offset:end],
                    "offset": offset,
                    "max_chars": max_chars,
                    "returned_chars": len(original[offset:end]),
                    "total_chars": len(original),
                    "next_offset": end if remaining else None,
                    "remaining_chars": remaining,
                    "truncated": bool(remaining),
                }
                return _text_result(
                    json.dumps(page, ensure_ascii=False, separators=(",", ":"))
                )
            if name == "explain_receipt":
                return _text_result(
                    json.dumps(
                        self.ledger.explain_receipt(arguments["receipt_id"]),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            if name == "show_savings":
                return _text_result(
                    json.dumps(self.ledger.savings(), ensure_ascii=False, indent=2)
                )
            return _text_result(
                json.dumps(
                    {"risks": self.ledger.risks()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except LedgerIntegrityError:
            return _tool_error(
                "ledger_integrity_failed", "Receipt integrity check failed"
            )
        except KeyError:
            return _tool_error("receipt_not_found", "Receipt not found")
        except Exception:
            return _tool_error("tool_execution_failed", "Tool execution failed")


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
                "additionalProperties": False,
            },
        },
        {
            "name": "retrieve_original",
            "description": "Retrieve the original payload for a receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "receipt_id": {"type": "string", "minLength": 1},
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_RETRIEVE_MAX_CHARS,
                        "default": DEFAULT_RETRIEVE_MAX_CHARS,
                    },
                },
                "required": ["receipt_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "explain_receipt",
            "description": "Explain a token governance receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "receipt_id": {"type": "string", "minLength": 1}
                },
                "required": ["receipt_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "show_savings",
            "description": "Show aggregate token savings recorded in the local ledger.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "list_context_risks",
            "description": "List receipts that were not classified as low risk.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def _require_empty_params(params: Any) -> None:
    if not isinstance(params, dict) or params:
        raise _ProtocolError(-32602, "Invalid params")


def _validate_tool_call_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict) or set(params) - {"name", "arguments"}:
        raise _ProtocolError(-32602, "Invalid tools/call params")
    name = params.get("name")
    arguments = params.get("arguments", {})
    supported = {tool["name"] for tool in tool_definitions()}
    if not isinstance(name, str) or name not in supported:
        raise _ProtocolError(-32602, "Unknown tool")
    if not isinstance(arguments, dict):
        raise _ProtocolError(-32602, "Tool arguments must be an object")
    return {"name": name, "arguments": arguments}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_tool_arguments(name: str, arguments: dict[str, Any]) -> str | None:
    if name == "govern_context":
        if "content_type" in arguments or "source" in arguments:
            return GOVERN_MIGRATION_GUIDANCE
        if set(arguments) - {"payload", "strategy"}:
            return "govern_context accepts only payload and strategy"
        if not isinstance(arguments.get("payload"), str):
            return "payload must be a string"
        if arguments.get("strategy", "auto") not in {
            "auto",
            "repetitive_log",
            "test_output",
            "build_output",
        }:
            return "strategy must be auto|repetitive_log|test_output|build_output"
        return None
    if name == "retrieve_original":
        if "full" in arguments:
            return RETRIEVE_MIGRATION_GUIDANCE
        if set(arguments) - {"receipt_id", "offset", "max_chars"}:
            return "retrieve_original accepts only receipt_id, offset, and max_chars"
        receipt_id = arguments.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            return "receipt_id must be a non-empty string"
        offset = arguments.get("offset", 0)
        if not _is_int(offset) or offset < 0:
            return "offset must be a non-negative integer"
        max_chars = arguments.get("max_chars", DEFAULT_RETRIEVE_MAX_CHARS)
        if (
            not _is_int(max_chars)
            or max_chars < 1
            or max_chars > MAX_RETRIEVE_MAX_CHARS
        ):
            return f"max_chars must be an integer from 1 to {MAX_RETRIEVE_MAX_CHARS}"
        return None
    if name == "explain_receipt":
        if set(arguments) != {"receipt_id"}:
            return "explain_receipt requires only receipt_id"
        receipt_id = arguments.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            return "receipt_id must be a non-empty string"
        return None
    if arguments:
        return f"{name} does not accept arguments"
    return None


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(code: str, message: str) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"code": code, "message": message},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ],
        "isError": True,
    }


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
