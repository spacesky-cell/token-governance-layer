from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .core import GovernanceEngine
from .ledger import ContextLedger
from .mcp_gateway import (
    load_config,
    parse_backend_specs,
    parse_tool_policy,
    resolve_db_path,
)
from .policy import PolicyEngine

DEFAULT_DB_PATH = str(Path.home() / ".token-governance" / "ledger.sqlite")
DEFAULT_CLAUDE_LEDGER = ".tgl/claude-ledger.sqlite"
DEFAULT_CLAUDE_HOOK_MATCHER = "Bash|PowerShell|Read|Grep|Glob|LS|Task|WebFetch|WebSearch"
TGL_HOOK_COMMAND_MARKERS = (
    "tgl-claude-hook",
    "token_governance.claude_hook",
    "claude-tgl-hook",
)


@dataclass(frozen=True)
class BackendCheckSpec:
    name: str
    command: list[str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgl")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the local SQLite ledger.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    govern = subparsers.add_parser("govern", help="Govern stdin and emit JSON result.")
    govern.add_argument("--content-type", default="text")
    govern.add_argument("--source", default="cli")

    retrieve = subparsers.add_parser("retrieve", help="Retrieve original payload by receipt ID.")
    retrieve.add_argument("receipt_id")

    explain = subparsers.add_parser("inspect", help="Explain a receipt.")
    explain.add_argument("receipt_id")

    subparsers.add_parser("stats", help="Show token savings summary.")
    subparsers.add_parser("risks", help="Show non-low-risk receipts.")
    doctor = subparsers.add_parser("doctor", help="Check local configuration.")
    doctor.add_argument("--config", help="Path to token-governance.config.json.")

    mcp_config = subparsers.add_parser("mcp-config", help="Generate an MCP config snippet.")
    mcp_config.add_argument("--config", required=True, help="Path to token-governance.config.json.")
    mcp_config.add_argument("--server-name", default="token-governance-gateway")
    mcp_config.add_argument(
        "--source-checkout",
        help="Path to a source checkout. Emits python -m config with PYTHONPATH instead of installed script.",
    )

    claude_install = subparsers.add_parser(
        "claude-install",
        help="Install project-level Claude Code automatic token governance.",
    )
    claude_install.add_argument(
        "--project",
        default=".",
        help="Project directory where .claude/settings.json and .mcp.json will be updated.",
    )
    claude_install.add_argument(
        "--hook-command",
        help="Path or command for tgl-claude-hook. Defaults to the installed console script.",
    )
    claude_install.add_argument(
        "--mcp-command",
        help="Path or command for tgl-mcp. Defaults to the installed console script.",
    )
    claude_install.add_argument("--server-name", default="token-governance-layer")
    claude_install.add_argument("--matcher", default=DEFAULT_CLAUDE_HOOK_MATCHER)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = args.db or DEFAULT_DB_PATH

    if args.command == "claude-install":
        try:
            _print_json(
                install_claude_project(
                    project_path=args.project,
                    db_path=args.db,
                    hook_command=args.hook_command,
                    mcp_command=args.mcp_command,
                    server_name=args.server_name,
                    matcher=args.matcher,
                )
            )
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    ledger = ContextLedger(db_path)
    engine = GovernanceEngine(ledger=ledger, policy=PolicyEngine())

    try:
        if args.command == "govern":
            payload = sys.stdin.read()
            result = engine.govern_context(
                payload,
                content_type=args.content_type,
                source=args.source,
            )
            _print_json(result)
            return 0

        if args.command == "retrieve":
            sys.stdout.write(ledger.retrieve_original(args.receipt_id))
            return 0

        if args.command == "inspect":
            _print_json(ledger.explain_receipt(args.receipt_id))
            return 0

        if args.command == "stats":
            _print_json(ledger.savings())
            return 0

        if args.command == "risks":
            _print_json({"risks": ledger.risks()})
            return 0

        if args.command == "doctor":
            report = build_doctor_report(db_path=db_path, config_path=args.config)
            _print_json(report)
            return 0 if report["ok"] else 1

        if args.command == "mcp-config":
            _print_json(
                build_mcp_config_snippet(
                    config_path=args.config,
                    server_name=args.server_name,
                    source_checkout=args.source_checkout,
                )
            )
            return 0
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def install_claude_project(
    *,
    project_path: str,
    db_path: str | None,
    hook_command: str | None,
    mcp_command: str | None,
    server_name: str,
    matcher: str,
) -> dict[str, object]:
    project = Path(project_path).expanduser().resolve()
    ledger_path = _resolve_project_path(project, db_path or DEFAULT_CLAUDE_LEDGER)
    hook_executable = _resolve_command(hook_command, "tgl-claude-hook")
    mcp_executable = _resolve_command(mcp_command, "tgl-mcp")

    settings_path = project / ".claude" / "settings.json"
    mcp_path = project / ".mcp.json"

    settings = _read_json_object(settings_path)
    mcp_config = _read_json_object(mcp_path)

    hook_command_line = (
        f"{_quote_for_claude_shell(_config_path(hook_executable))} "
        f"--db {_quote_for_claude_shell(_config_path(ledger_path))}"
    )
    _install_claude_hook(settings, matcher=matcher, command=hook_command_line)
    _install_mcp_server(
        mcp_config,
        server_name=server_name,
        command=_config_path(mcp_executable),
        db_path=_config_path(ledger_path),
    )

    _write_json(settings_path, settings)
    _write_json(mcp_path, mcp_config)

    return {
        "ok": True,
        "project": str(project),
        "settings": str(settings_path),
        "mcp": str(mcp_path),
        "ledger": _config_path(ledger_path),
        "hook_command": hook_command_line,
        "mcp_server": server_name,
    }


def _resolve_project_path(project: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _resolve_command(value: str | None, command_name: str) -> str:
    if value is None and os.environ.get("TGL_NPM_WRAPPER") == "1":
        return command_name
    command = value or shutil.which(command_name) or command_name
    if any(separator in command for separator in ("\\", "/")):
        return str(Path(command).expanduser().resolve())
    return command


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _install_claude_hook(settings: dict[str, object], *, matcher: str, command: str) -> None:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Expected .claude/settings.json hooks to be an object")

    post_tool_use = hooks.get("PostToolUse", [])
    if not isinstance(post_tool_use, list):
        raise ValueError("Expected hooks.PostToolUse to be a list")

    kept_entries = []
    for entry in post_tool_use:
        if not isinstance(entry, dict):
            kept_entries.append(entry)
            continue
        entry_hooks = entry.get("hooks", [])
        if not isinstance(entry_hooks, list):
            kept_entries.append(entry)
            continue
        kept_hooks = [
            hook
            for hook in entry_hooks
            if not (isinstance(hook, dict) and _is_tgl_hook_command(hook.get("command")))
        ]
        if kept_hooks:
            copied = dict(entry)
            copied["hooks"] = kept_hooks
            kept_entries.append(copied)

    kept_entries.append(
        {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ],
        }
    )
    hooks["PostToolUse"] = kept_entries


def _is_tgl_hook_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    lowered = command.lower()
    return any(marker in lowered for marker in TGL_HOOK_COMMAND_MARKERS)


def _install_mcp_server(
    mcp_config: dict[str, object], *, server_name: str, command: str, db_path: str
) -> None:
    servers = mcp_config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Expected .mcp.json mcpServers to be an object")
    servers[server_name] = {
        "type": "stdio",
        "command": command,
        "args": ["--db", db_path],
        "env": {},
    }


def _config_path(value: str | Path) -> str:
    return str(value).replace("\\", "/")


def _quote_for_claude_shell(value: str) -> str:
    escaped = value.replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def build_mcp_config_snippet(
    *,
    config_path: str,
    server_name: str,
    source_checkout: str | None = None,
) -> dict[str, object]:
    if source_checkout:
        source_root = Path(source_checkout)
        server = {
            "command": sys.executable,
            "args": [
                "-m",
                "token_governance.mcp_gateway",
                "--config",
                config_path,
            ],
            "env": {
                "PYTHONPATH": str(source_root / "src"),
            },
        }
    else:
        server = {
            "command": "tgl-mcp-gateway",
            "args": ["--config", config_path],
        }
    return {"mcpServers": {server_name: server}}


def build_doctor_report(*, db_path: str, config_path: str | None = None) -> dict[str, object]:
    checks = []
    config = {}
    effective_db_path = db_path

    if config_path:
        try:
            config = load_config(config_path)
            checks.append(_check("config_load", True, f"Loaded {Path(config_path).resolve()}"))
            effective_db_path = resolve_db_path(None, config)
        except Exception as exc:
            checks.append(_check("config_load", False, str(exc)))
            config = {}

    checks.append(_check_ledger_writable(effective_db_path))

    if config_path and config:
        checks.extend(_check_backends(config))
        checks.append(_check_tool_policy(config))

    return {
        "ok": all(bool(check["ok"]) for check in checks),
        "database": str(Path(effective_db_path).resolve()),
        "database_exists": Path(effective_db_path).exists(),
        "config": str(Path(config_path).resolve()) if config_path else None,
        "checks": checks,
    }


def _check(name: str, ok: bool, message: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "message": message}


def _check_ledger_writable(db_path: str) -> dict[str, object]:
    try:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        ContextLedger(path)
        return _check("ledger_writable", True, f"Ledger is writable: {path.resolve()}")
    except Exception as exc:
        return _check("ledger_writable", False, str(exc))


def _check_backends(config: dict[str, object]) -> list[dict[str, object]]:
    checks = []
    try:
        specs = parse_backend_specs([], None, [], config)
        checks.append(_check("backends_valid", True, f"{len(specs)} backend(s) configured."))
    except Exception as exc:
        checks.append(_check("backends_valid", False, str(exc)))
        specs = _best_effort_backend_specs(config)

    for spec in specs:
        command = spec.command[0] if spec.command else ""
        if _command_exists(command):
            checks.append(_check(f"backend_command:{spec.name}", True, f"Found {command}"))
        else:
            checks.append(_check(f"backend_command:{spec.name}", False, f"Command not found: {command}"))
    return checks


def _check_tool_policy(config: dict[str, object]) -> dict[str, object]:
    try:
        parse_tool_policy(config)
        return _check("tool_policy_valid", True, "Tool policy is valid.")
    except Exception as exc:
        return _check("tool_policy_valid", False, str(exc))


def _best_effort_backend_specs(config: dict[str, object]):
    gateway = config.get("gateway", {})
    if not isinstance(gateway, dict):
        return []
    backends = gateway.get("backends", [])
    if not isinstance(backends, list):
        return []
    specs = []
    for item in backends:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        command = item.get("command")
        args = item.get("args", [])
        if isinstance(name, str) and isinstance(command, str):
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                args = []
            specs.append(BackendCheckSpec(name=name, command=[command, *args]))
    return specs


def _command_exists(command: str) -> bool:
    if not command:
        return False
    path = Path(command)
    if path.is_absolute() or any(separator in command for separator in ("\\", "/")):
        return path.exists()
    return shutil.which(command) is not None


if __name__ == "__main__":
    raise SystemExit(main())
