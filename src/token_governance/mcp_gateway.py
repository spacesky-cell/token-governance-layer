from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .ledger import ContextLedger
from .tokenizer import estimate_tokens


class GatewayState(str, Enum):
    CREATED = "CREATED"
    INITIALIZED = "INITIALIZED"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class BackendState(str, Enum):
    STARTING = "STARTING"
    INITIALIZED = "INITIALIZED"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


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
    request_timeout_seconds = parse_request_timeout(config)
    gateway = McpGateway(
        ContextLedger(db_path),
        backends,
        tool_policy=parse_tool_policy(config),
        request_timeout_seconds=request_timeout_seconds,
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


def parse_request_timeout(config: dict[str, Any]) -> int:
    gateway = config.get("gateway", {})
    if not isinstance(gateway, dict):
        raise ValueError("Config gateway must be an object.")
    value = gateway.get("request_timeout_seconds", 10)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 120:
        raise ValueError("Config gateway.request_timeout_seconds must be an integer from 1 to 120.")
    return value


def _parse_policy_list(raw_policy: dict[str, Any], key: str) -> list[str]:
    value = raw_policy.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Config gateway.tool_policy.{key} must be an array of strings.")
    return value


class StdioMcpBackend:
    """A small MCP client with independent protocol and stderr readers."""

    PROTOCOL_VERSION = "2025-06-18"
    STDERR_RING_BYTES = 64 * 1024

    def __init__(self, command: list[str], *, name: str = "backend", timeout_seconds: int = 10,
                 on_notification: Any | None = None):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self.command = command
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.on_notification = on_notification
        self.next_id = 1
        self.proc: subprocess.Popen[bytes] | None = None
        self.state = BackendState.STARTING
        self.capabilities: dict[str, Any] = {}
        self.failure_reason: str | None = None
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._writer_start_lock = threading.Lock()
        self._write_queue: queue.Queue[tuple[bytes, threading.Event, dict[str, Any]] | None] = queue.Queue()
        self._writer: threading.Thread | None = None
        self._stderr = deque()  # type: deque[bytes]
        self._stderr_size = 0
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    @property
    def stderr_ring(self) -> str:
        with self._lock:
            return b"".join(self._stderr).decode("utf-8", errors="replace")

    def start(self) -> None:
        if self.proc is not None:
            return
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        self._reader = threading.Thread(target=self._read_stdout, name=f"tgl-{self.name}-stdout", daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, name=f"tgl-{self.name}-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def initialize(self) -> None:
        if self.state is BackendState.ACTIVE:
            raise RuntimeError(f"Backend {self.name} is already initialized")
        if self.state is not BackendState.STARTING:
            raise RuntimeError(f"Backend {self.name} is unavailable ({self.state.value.lower()})")
        try:
            self.start()
            result = self.request("initialize", {
                "protocolVersion": self.PROTOCOL_VERSION,
                "clientInfo": {"name": "token-governance-mcp-gateway", "version": "0.2.0"},
                "capabilities": {},
            })
            if result.get("protocolVersion") != self.PROTOCOL_VERSION:
                raise RuntimeError("Backend MCP protocol version is incompatible")
            caps = result.get("capabilities", {})
            if not isinstance(caps, dict):
                raise RuntimeError("Backend MCP capabilities are invalid")
            self.capabilities = dict(caps)
            self.state = BackendState.INITIALIZED
            self.notify("notifications/initialized", {})
            self.state = BackendState.ACTIVE
        except Exception as exc:
            self.failure_reason = _redact(str(exc))
            self.state = BackendState.FAILED
            self.close()
            raise

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any] | None = None,
                *, request_id_observer: Any | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        if method == "initialize" and self.state is not BackendState.STARTING:
            raise RuntimeError(f"Backend {self.name} is unavailable ({self.state.value.lower()})")
        if method != "initialize" and self.state is not BackendState.ACTIVE:
            raise RuntimeError(f"Backend {self.name} is unavailable ({self.state.value.lower()})")
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Backend MCP process has no stdio pipes.")
        with self._lock:
            request_id = self.next_id
            self.next_id += 1
            event = threading.Event()
            slot: dict[str, Any] = {}
            self._pending[request_id] = (event, slot)
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        try:
            self._write(request, deadline=deadline)
            if request_id_observer is not None:
                request_id_observer(request_id)
            if not event.wait(max(0.0, deadline - time.monotonic())):
                self.cancel(request_id)
                raise TimeoutError(f"Backend {self.name} request timed out")
            if "exception" in slot:
                raise RuntimeError(str(slot["exception"]))
            response = slot.get("response")
            if not isinstance(response, dict):
                raise RuntimeError("Backend MCP response is invalid")
            if "error" in response:
                error = response.get("error")
                message = error.get("message", "Backend MCP error") if isinstance(error, dict) else "Backend MCP error"
                raise RuntimeError(_redact(message))
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Backend MCP result is invalid")
            return result
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def cancel(self, request_id: int, reason: str = "gateway timeout or cancellation") -> None:
        with self._lock:
            if request_id not in self._pending:
                return
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": _redact(reason)})
        except Exception:
            pass

    def _write(self, payload: dict[str, Any], *, deadline: float | None = None) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError(f"Backend {self.name} has no stdin")
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._ensure_writer()
        event = threading.Event()
        slot: dict[str, Any] = {}
        self._write_queue.put((raw, event, slot))
        write_deadline = deadline if deadline is not None else time.monotonic() + self.timeout_seconds
        if not event.wait(max(0.0, write_deadline - time.monotonic())):
            raise TimeoutError(f"Backend {self.name} write timed out")
        if "exception" in slot:
            raise RuntimeError(f"Backend {self.name} transport failed") from slot["exception"]

    def _ensure_writer(self) -> None:
        with self._writer_start_lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(
                target=self._write_frames,
                name=f"tgl-{self.name}-stdin",
                daemon=True,
            )
            self._writer.start()

    def _write_frames(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is None:
                return
            raw, event, slot = item
            proc = self.proc
            try:
                if proc is None or proc.stdin is None:
                    raise OSError("backend stdin unavailable")
                proc.stdin.write(raw)
                proc.stdin.flush()
            except BaseException as exc:
                slot["exception"] = exc
            finally:
                event.set()

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw_line in iter(proc.stdout.readline, b""):
                if not raw_line:
                    break
                try:
                    message = json.loads(raw_line.decode("utf-8"))
                except Exception:
                    self._fail_pending("Backend MCP protocol error")
                    continue
                if not isinstance(message, dict):
                    self._fail_pending("Backend MCP protocol error")
                    continue
                if "id" not in message:
                    if self.on_notification is not None:
                        try:
                            self.on_notification(self.name, message)
                        except Exception:
                            pass
                    continue
                request_id = message.get("id")
                with self._lock:
                    pending = self._pending.get(request_id)
                if pending is None:
                    continue
                event, slot = pending
                slot["response"] = message
                event.set()
        finally:
            self._fail_pending("Backend MCP process exited without response")

    def _read_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        carry = b""
        private_key_open = False
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            carry += chunk
            while b"\n" in carry:
                line, carry = carry.split(b"\n", 1)
                line += b"\n"
                if private_key_open:
                    if re.search(br"-----END (?:[A-Z0-9 ]{1,64} )?PRIVATE KEY-----", line, re.IGNORECASE):
                        private_key_open = False
                        self._append_stderr(b"[REDACTED]\n")
                    continue
                begin = re.search(br"-----BEGIN (?:[A-Z0-9 ]{1,64} )?PRIVATE KEY-----", line, re.IGNORECASE)
                if begin is not None:
                    private_key_open = True
                    if begin.start() > 0:
                        self._append_stderr(line[: begin.start()])
                    if re.search(br"-----END (?:[A-Z0-9 ]{1,64} )?PRIVATE KEY-----", line[begin.end() :], re.IGNORECASE):
                        private_key_open = False
                        self._append_stderr(b"[REDACTED]\n")
                    continue
                self._append_stderr(line)
            if len(carry) > 4096 and not private_key_open:
                self._append_stderr(carry[:-1024])
                carry = carry[-1024:]
        if private_key_open:
            self._append_stderr(b"[REDACTED]")
        elif carry:
            self._append_stderr(carry)

    def _append_stderr(self, value: bytes) -> None:
        redacted = _redact_bytes(value)
        with self._lock:
            self._stderr.append(redacted)
            self._stderr_size += len(redacted)
            while self._stderr_size > self.STDERR_RING_BYTES and self._stderr:
                self._stderr_size -= len(self._stderr.popleft())

    def _fail_pending(self, message: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
        for event, slot in pending:
            slot["exception"] = message
            event.set()

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            self.state = BackendState.CLOSED
            return
        self.state = BackendState.CLOSING
        stdin_closed = threading.Event()

        def close_stdin() -> None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            stdin_closed.set()

        stdin_closer = threading.Thread(
            target=close_stdin,
            name=f"tgl-{self.name}-stdin-close",
            daemon=True,
        )
        stdin_closer.start()
        stdin_closed.wait(0.05)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self._write_queue.put(None)
        if self._writer is not None:
            self._writer.join(timeout=2)
        stdin_closer.join(timeout=0.1)
        for thread in (self._reader, self._stderr_reader):
            if thread is not None:
                thread.join(timeout=2)
        self.state = BackendState.CLOSED


class McpGateway:
    def __init__(
        self,
        ledger: ContextLedger,
        backends: dict[str, StdioMcpBackend],
        tool_policy: GatewayToolPolicy | None = None,
        request_timeout_seconds: int = 10,
    ):
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, int)
            or not 1 <= request_timeout_seconds <= 120
        ):
            raise ValueError("request_timeout_seconds must be between 1 and 120")
        self.ledger = ledger
        self.backends = backends
        self.tool_policy = tool_policy or GatewayToolPolicy(allow=frozenset(), deny=frozenset())
        self.request_timeout_seconds = request_timeout_seconds
        for name, backend in self.backends.items():
            backend.name = name
            backend.timeout_seconds = request_timeout_seconds
            backend.on_notification = self._on_backend_notification
        self._tools: dict[str, list[dict[str, Any]]] = {}
        self._state = GatewayState.CREATED
        self._upstream_pending: dict[Any, tuple[str, int]] = {}
        self._current_upstream = threading.local()
        self._output_lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._pending_lock = threading.Lock()
        self._cancelled_pending: dict[Any, str] = {}

    def run(self, stdin: Any, stdout: Any) -> int:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except (TypeError, ValueError):
                self._write_upstream(stdout, _error(None, -32700, "Parse error"))
                continue
            if not isinstance(request, dict):
                self._write_upstream(stdout, _error(None, -32600, "Invalid Request"))
                continue
            if request.get("method") == "tools/call" and "id" in request and self._state is GatewayState.ACTIVE:
                self._register_upstream(request.get("id"))
                worker = threading.Thread(target=self._handle_async, args=(request, stdout), daemon=True)
                self._workers.add(worker)
                worker.start()
            else:
                response = self.handle(request)
                if response is not None:
                    self._write_upstream(stdout, response)
        self.close()
        for worker in tuple(self._workers):
            worker.join(timeout=2)
        return 0

    def _handle_async(self, request: dict[str, Any], stdout: Any) -> None:
        try:
            response = self.handle(request)
            if response is not None:
                self._write_upstream(stdout, response)
        finally:
            self._workers.discard(threading.current_thread())

    def _write_upstream(self, stdout: Any, response: dict[str, Any]) -> None:
        with self._output_lock:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        is_notification = "id" not in request
        if not isinstance(method, str):
            return None if is_notification else _error(request_id, -32600, "Invalid Request")
        if is_notification:
            if method == "notifications/initialized" and self._state is GatewayState.INITIALIZED:
                self._state = GatewayState.ACTIVE
            elif method == "notifications/cancelled":
                self._cancel_upstream(request.get("params", {}))
            return None
        if method == "initialize":
            return self._initialize_upstream(request_id, request.get("params", {}))
        if self._state is GatewayState.CREATED:
            return _error(request_id, -32002, "Client not initialized")
        try:
            if self._state is not GatewayState.ACTIVE:
                return _error(request_id, -32002, "Client not initialized")
            if method == "tools/list":
                return _result(request_id, {"tools": gateway_tool_definitions()})
            if method == "tools/call":
                self._register_upstream(request_id)
                self._current_upstream.request_id = request_id
                if self._is_upstream_cancelled(request_id):
                    raise RuntimeError("Request cancelled")
                return _result(request_id, self._call_tool(request.get("params", {})))
            return _error(request_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            return _error(request_id, -32000, _redact(str(exc)))
        finally:
            with self._pending_lock:
                self._upstream_pending.pop(request_id, None)
                self._cancelled_pending.pop(request_id, None)
            self._current_upstream.request_id = None

    def _initialize_upstream(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if self._state is not GatewayState.CREATED:
            return _error(request_id, -32600, "Client is already initialized")
        if not isinstance(params, dict):
            return _error(request_id, -32602, "initialize params must be an object")
        failures: dict[str, str] = {}
        for name, backend in self.backends.items():
            try:
                backend.initialize()
            except Exception as exc:
                failures[name] = _redact(str(exc))
        self._state = GatewayState.INITIALIZED
        result: dict[str, Any] = {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "token-governance-mcp-gateway", "version": "0.2.0"},
            "capabilities": {"tools": {}},
        }
        if failures:
            result["backendStatus"] = {name: self.backends[name].state.value.lower() for name in self.backends}
            result["unavailableBackends"] = [
                {
                    "backend": name,
                    "state": self.backends[name].state.value.lower(),
                    "reason": reason,
                }
                for name, reason in failures.items()
            ]
        return _result(request_id, result)

    def _cancel_upstream(self, params: dict[str, Any]) -> None:
        request_id = params.get("requestId") if isinstance(params, dict) else None
        reason = params.get("reason", "upstream cancellation") if isinstance(params, dict) else "upstream cancellation"
        with self._pending_lock:
            pending = self._upstream_pending.get(request_id)
            if not pending:
                return
            if not pending[1]:
                self._cancelled_pending[request_id] = str(reason)
                return
        if pending and pending[1]:
            self.backends[pending[0]].cancel(pending[1], str(reason))

    def _register_upstream(self, request_id: Any) -> None:
        with self._pending_lock:
            self._upstream_pending.setdefault(request_id, ("", 0))

    def _is_upstream_cancelled(self, request_id: Any) -> bool:
        with self._pending_lock:
            return request_id in self._cancelled_pending

    def _bind_backend_request(self, upstream_id: Any, backend_name: str, backend_id: int) -> None:
        with self._pending_lock:
            pending = self._upstream_pending.get(upstream_id)
            if pending is None:
                return
            self._upstream_pending[upstream_id] = (backend_name, backend_id)
            reason = self._cancelled_pending.pop(upstream_id, None)
        if reason is not None:
            self.backends[backend_name].cancel(backend_id, reason)

    def _on_backend_notification(self, backend_name: str, message: dict[str, Any]) -> None:
        if message.get("method") == "notifications/tools/list_changed":
            self._tools.pop(backend_name, None)

    def close(self) -> None:
        if self._state in {GatewayState.CLOSING, GatewayState.CLOSED}:
            return
        self._state = GatewayState.CLOSING
        for backend in self.backends.values():
            backend.close()
        self._state = GatewayState.CLOSED

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "list_backend_tools":
            query = arguments.get("query")
            if query is not None and not isinstance(query, str):
                raise TypeError("list_backend_tools.query must be a string.")
            payload: dict[str, Any] = {"tools": self._tool_catalog(query=query)}
            unavailable = self._unavailable_backends()
            if unavailable:
                payload["unavailable_backends"] = unavailable
            return _text_result(
                json.dumps(
                    payload,
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
            upstream_id = getattr(self._current_upstream, "request_id", None)
            if self._is_upstream_cancelled(upstream_id):
                raise RuntimeError("Request cancelled")
            return self.backends[backend_name].request(
                "tools/call",
                {"name": str(schema["name"]), "arguments": tool_args},
                request_id_observer=lambda backend_id: self._bind_backend_request(
                    upstream_id, backend_name, backend_id
                ),
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
        backend = self.backends[backend_name]
        if backend.state is not BackendState.ACTIVE:
            return []
        if backend_name not in self._tools:
            result = backend.request("tools/list")
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

    def _unavailable_backends(self) -> list[dict[str, str]]:
        return [
            {
                "backend": name,
                "state": backend.state.value.lower(),
                "reason": backend.failure_reason or "backend unavailable",
            }
            for name, backend in self.backends.items()
            if backend.state is not BackendState.ACTIVE
        ]

    def _find_tool(self, tool_name: str) -> tuple[str, dict[str, Any]]:
        if "::" in tool_name:
            backend_name, raw_name = tool_name.split("::", 1)
            if backend_name not in self.backends:
                raise KeyError(f"Backend not found: {backend_name}")
            if self.backends[backend_name].state is not BackendState.ACTIVE:
                raise RuntimeError(f"Backend unavailable: {backend_name}")
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


_REDACTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:gh[opurs]|github_pat|sk|glpat|xox[baprs]|hf|npm)_[A-Za-z0-9_-]{8,255}\b",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization)"
        r"[\"']?[ \t]{0,32}[:=][ \t]{0,32}[\"']?(?:bearer|basic)?[ \t]*[^\s,;\"']{1,512}",
        r"(?s)-----BEGIN (?:[A-Z0-9 ]{1,64} )?PRIVATE KEY-----.{0,65536}?-----END (?:[A-Z0-9 ]{1,64} )?PRIVATE KEY-----",
    )
)


def _redact(value: str) -> str:
    if not isinstance(value, str):
        return "[REDACTED]"
    result = value
    for pattern in _REDACTION_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _redact_bytes(value: bytes) -> bytes:
    return _redact(value.decode("utf-8", errors="replace")).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
