from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ledger import ContextLedger
from .tokenizer import estimate_tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tgl-mcp-gateway")
    parser.add_argument(
        "--config",
        help="Path to token-governance.config.json. CLI backend/db options override config values.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the local SQLite ledger.",
    )
    parser.add_argument(
        "--backend",
        action="append",
        default=[],
        help="Named backend in the form name=command,arg1,arg2. Repeat for multiple backends.",
    )
    parser.add_argument("--backend-command", help="Command used to start one legacy backend MCP server.")
    parser.add_argument(
        "--backend-arg",
        action="append",
        default=[],
        help="Argument passed to the backend MCP server. Repeat for multiple args.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config) if args.config else {}
    db_path = resolve_db_path(args.db, config)
    backend_specs = parse_backend_specs(
        args.backend,
        args.backend_command,
        args.backend_arg,
        config,
    )
    backends = {spec.name: StdioMcpBackend(spec.command) for spec in backend_specs}
    gateway = McpGateway(
        ContextLedger(db_path),
        backends,
        tool_policy=parse_tool_policy(config),
    )
    try:
        return gateway.run(sys.stdin, sys.stdout)
    finally:
        for backend in backends.values():
            backend.close()


@dataclass(frozen=True)
class BackendSpec:
    name: str
    command: list[str]


@dataclass(frozen=True)
class GatewayToolPolicy:
    allow: frozenset[str]
    deny: frozenset[str]

    def is_allowed(self, qualified_name: str) -> bool:
        if qualified_name in self.deny:
            return False
        if self.allow and qualified_name not in self.allow:
            return False
        return True

    def assert_allowed(self, qualified_name: str) -> None:
        if not self.is_allowed(qualified_name):
            raise PermissionError(f"Tool denied by gateway policy: {qualified_name}")


def parse_backend_specs(
    backend_values: list[str],
    legacy_command: str | None,
    legacy_args: list[str],
    config: dict[str, Any] | None = None,
) -> list[BackendSpec]:
    specs = []
    for value in backend_values:
        name, separator, command_text = value.partition("=")
        if not separator or not name.strip() or not command_text.strip():
            raise ValueError("--backend must use the form name=command,arg1,arg2")
        command = [part for part in command_text.split(",") if part]
        if not command:
            raise ValueError("--backend command cannot be empty")
        specs.append(BackendSpec(name.strip(), command))
    if legacy_command:
        specs.append(BackendSpec("backend", [legacy_command, *legacy_args]))
    if not specs and config:
        specs.extend(parse_config_backend_specs(config))
    if not specs:
        raise ValueError("At least one --backend or --backend-command is required.")
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError("Backend names must be unique.")
    return specs


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("Config file root must be a JSON object.")
    return value


def resolve_db_path(cli_db: str | None, config: dict[str, Any]) -> str:
    if cli_db:
        return cli_db
    ledger = config.get("ledger", {})
    if isinstance(ledger, dict) and ledger.get("path"):
        return str(ledger["path"])
    return str(Path.home() / ".token-governance" / "ledger.sqlite")


def parse_config_backend_specs(config: dict[str, Any]) -> list[BackendSpec]:
    gateway = config.get("gateway", {})
    if not isinstance(gateway, dict):
        raise ValueError("Config gateway must be an object.")
    backends = gateway.get("backends", [])
    if not isinstance(backends, list):
        raise ValueError("Config gateway.backends must be an array.")

    specs = []
    for item in backends:
        if not isinstance(item, dict):
            raise ValueError("Each backend config must be an object.")
        name = item.get("name")
        command = item.get("command")
        args = item.get("args", [])
        if not isinstance(name, str) or not name:
            raise ValueError("Each backend config requires a non-empty name.")
        if not isinstance(command, str) or not command:
            raise ValueError(f"Backend {name} requires a command.")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"Backend {name} args must be an array of strings.")
        specs.append(BackendSpec(name, [command, *args]))
    return specs


def parse_tool_policy(config: dict[str, Any]) -> GatewayToolPolicy:
    gateway = config.get("gateway", {})
    if not isinstance(gateway, dict):
        return GatewayToolPolicy(allow=frozenset(), deny=frozenset())
    raw_policy = gateway.get("tool_policy", {})
    if not isinstance(raw_policy, dict):
        raise ValueError("Config gateway.tool_policy must be an object.")

    allow = _parse_policy_list(raw_policy, "allow")
    deny = _parse_policy_list(raw_policy, "deny")
    return GatewayToolPolicy(allow=frozenset(allow), deny=frozenset(deny))


def _parse_policy_list(raw_policy: dict[str, Any], key: str) -> list[str]:
    value = raw_policy.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Config gateway.tool_policy.{key} must be an array of strings.")
    return value


class StdioMcpBackend:
    def __init__(self, command: list[str]):
        self.command = command
        self.next_id = 1
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("Backend MCP process has no stdio pipes.")
        request_id = self.next_id
        self.next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"Backend MCP process exited without response. {stderr}".strip())
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(response["error"].get("message", "Backend MCP error"))
        return response["result"]

    def close(self) -> None:
        if self.proc.poll() is not None:
            return
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=3)


class McpGateway:
    def __init__(
        self,
        ledger: ContextLedger,
        backends: dict[str, StdioMcpBackend],
        tool_policy: GatewayToolPolicy | None = None,
    ):
        self.ledger = ledger
        self.backends = backends
        self.tool_policy = tool_policy or GatewayToolPolicy(allow=frozenset(), deny=frozenset())
        self._tools: dict[str, list[dict[str, Any]]] = {}

    def run(self, stdin: Any, stdout: Any) -> int:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            response = self.handle(request)
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
        return 0

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        try:
            if method == "initialize":
                return _result(
                    request_id,
                    {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {
                            "name": "token-governance-mcp-gateway",
                            "version": "0.1.0",
                        },
                        "capabilities": {"tools": {}},
                    },
                )
            if method == "tools/list":
                return _result(request_id, {"tools": gateway_tool_definitions()})
            if method == "tools/call":
                return _result(request_id, self._call_tool(request.get("params", {})))
            return _error(request_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            return _error(request_id, -32000, str(exc))

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "list_backend_tools":
            query = arguments.get("query")
            if query is not None and not isinstance(query, str):
                raise TypeError("list_backend_tools.query must be a string.")
            return _text_result(
                json.dumps(
                    {"tools": self._tool_catalog(query=query)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        if name == "get_tool_schema":
            tool_name = str(arguments["tool_name"])
            backend_name, schema = self._find_tool(tool_name)
            receipt_id, tokens_saved = self._record_schema_receipt(schema)
            payload = dict(schema)
            payload["backend"] = backend_name
            payload["qualified_name"] = self._qualified_name(backend_name, str(schema.get("name", "")))
            payload["receipt_id"] = receipt_id
            payload["tokens_saved"] = tokens_saved
            return _text_result(json.dumps(payload, ensure_ascii=False, indent=2))
        if name == "invoke_tool":
            tool_name = str(arguments["tool_name"])
            tool_args = arguments.get("arguments", {})
            if not isinstance(tool_args, dict):
                raise TypeError("invoke_tool.arguments must be an object.")
            backend_name, schema = self._find_tool(tool_name)
            return self.backends[backend_name].request(
                "tools/call",
                {"name": str(schema["name"]), "arguments": tool_args},
            )
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
        raise KeyError(f"Unknown gateway tool: {name}")

    def _backend_tools(self, backend_name: str) -> list[dict[str, Any]]:
        if backend_name not in self._tools:
            result = self.backends[backend_name].request("tools/list")
            tools = result.get("tools", [])
            if not isinstance(tools, list):
                raise TypeError("Backend tools/list result must contain a tools array.")
            self._tools[backend_name] = tools
        return self._tools[backend_name]

    def _tool_catalog(self, query: str | None = None) -> list[dict[str, str]]:
        catalog = []
        for backend_name in self.backends:
            for tool in self._backend_tools(backend_name):
                raw_name = str(tool.get("name", ""))
                qualified_name = self._qualified_name(backend_name, raw_name)
                if not self.tool_policy.is_allowed(qualified_name):
                    continue
                catalog_item = {
                    "backend": backend_name,
                    "name": raw_name,
                    "qualified_name": qualified_name,
                    "description": str(tool.get("description", "")),
                }
                if query and not _matches_query(catalog_item, query, raw_tool=tool):
                    continue
                catalog.append(catalog_item)
        return catalog

    def _find_tool(self, tool_name: str) -> tuple[str, dict[str, Any]]:
        if "::" in tool_name:
            backend_name, raw_name = tool_name.split("::", 1)
            if backend_name not in self.backends:
                raise KeyError(f"Backend not found: {backend_name}")
            for tool in self._backend_tools(backend_name):
                if tool.get("name") == raw_name:
                    self.tool_policy.assert_allowed(self._qualified_name(backend_name, raw_name))
                    return backend_name, tool
            raise KeyError(f"Backend tool not found: {tool_name}")

        matches = []
        for backend_name in self.backends:
            for tool in self._backend_tools(backend_name):
                if tool.get("name") == tool_name:
                    qualified_name = self._qualified_name(backend_name, tool_name)
                    if self.tool_policy.is_allowed(qualified_name):
                        matches.append((backend_name, tool))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise KeyError(f"Ambiguous backend tool name, use backend::tool: {tool_name}")
        raise KeyError(f"Backend tool not found: {tool_name}")

    def _qualified_name(self, backend_name: str, tool_name: str) -> str:
        return f"{backend_name}::{tool_name}"

    def _record_schema_receipt(self, schema: dict[str, Any]) -> tuple[str, int]:
        original = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        governed = json.dumps(
            {
                "name": schema.get("name"),
                "description": schema.get("description"),
                "schema": "loaded on demand by get_tool_schema",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        token_before = estimate_tokens(original)
        token_after = estimate_tokens(governed)
        receipt_id = self.ledger.record(
            source="mcp-gateway",
            content_type="mcp_tool_schema",
            action="lazy_schema",
            risk="low",
            original_text=original,
            governed_text=governed,
            token_before=token_before,
            token_after=token_after,
            policy="mcp-gateway-lazy-schema",
            notes=[
                "Full backend tool schema withheld from tools/list.",
                "Schema returned on demand through get_tool_schema.",
            ],
        )
        return receipt_id, token_before - token_after


def gateway_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_backend_tools",
            "description": "List backend tools without exposing full input schemas.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional keyword filter matched against backend, name, description, and hidden schema metadata.",
                    }
                },
            },
        },
        {
            "name": "get_tool_schema",
            "description": "Retrieve the full schema for one backend tool.",
            "inputSchema": {
                "type": "object",
                "properties": {"tool_name": {"type": "string"}},
                "required": ["tool_name"],
            },
        },
        {
            "name": "invoke_tool",
            "description": "Invoke a backend tool after selecting it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["tool_name", "arguments"],
            },
        },
        {
            "name": "retrieve_original",
            "description": "Retrieve the original payload for a token governance receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {"receipt_id": {"type": "string"}},
                "required": ["receipt_id"],
            },
        },
        {
            "name": "explain_receipt",
            "description": "Explain a token governance receipt generated by the gateway.",
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
            "description": "List non-low-risk receipts recorded in the local ledger.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _matches_query(
    tool: dict[str, str],
    query: str,
    raw_tool: dict[str, Any] | None = None,
) -> bool:
    normalized = query.casefold().strip()
    if not normalized:
        return True
    raw_tool_text = json.dumps(raw_tool or {}, ensure_ascii=False, sort_keys=True)
    haystack = " ".join(
        [
            tool.get("backend", ""),
            tool.get("name", ""),
            tool.get("qualified_name", ""),
            tool.get("description", ""),
            raw_tool_text,
        ]
    ).casefold()
    return all(part in haystack for part in normalized.split())


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
