from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import GovernanceConfig


PACKAGE_NAME = "token-governance-layer"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "token-governance.config.json"
SETTINGS_PATH = Path(".claude/settings.json")
MCP_PATH = Path(".mcp.json")
STATE_PATH = Path(".tgl/install-state.json")
OWNERSHIP_PATH = Path(".tgl/install-ownership.json")
SERVER_NAME = "token-governance-layer"
DEFAULT_MATCHER = "Bash|PowerShell|Read|Grep|Glob|LS|Task|WebFetch|WebSearch"
GLOBAL_INSTALL_GUIDANCE = (
    "Persistent Claude installation requires `npm install -g "
    "token-governance-layer`; npm exec/npx temporary installs are not supported"
)
COMPLETE_STATE = {"schema_version": 1, "status": "complete"}
DEFAULT_PROJECT_CONFIG = {
    "ledger": {"path": ".tgl/ledger.sqlite", "retention_days": 30},
    "policy": {
        "enabled_strategies": [
            "test_output",
            "build_output",
            "repetitive_log",
        ],
        "protected_content_behavior": "passthrough",
        "persistence_mode": "transformed_only",
        "max_payload_bytes": 2 * 1024 * 1024,
        "max_stored_original_bytes": 1024 * 1024,
        "hook_deadline_ms": 2000,
        "literal_secret_markers": [],
    },
    "gateway": {
        "request_timeout_seconds": 10,
        "tool_policy": {"allow": [], "deny": []},
        "backends": [],
    },
}


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class StableCommands:
    hook: Path
    mcp: Path

    def __post_init__(self) -> None:
        for name, value in (("hook", self.hook), ("mcp", self.mcp)):
            if not isinstance(value, Path) or not value.is_absolute():
                raise InstallError(f"{name} command must be an absolute path")
            if not value.is_file():
                raise InstallError(f"{name} command does not exist")


@dataclass(frozen=True)
class _OwnershipRecord:
    hook: Path
    mcp: Path


Checkpoint = Callable[[str, Path], None]


class AtomicFileStore:
    def __init__(self, checkpoint: Checkpoint | None = None) -> None:
        self._checkpoint = checkpoint or (lambda _point, _path: None)

    def write_json(self, path: Path, value: dict[str, Any], *, stage: str) -> bool:
        return self.write_bytes(path, _json_bytes(value), stage=stage)

    def write_bytes(self, path: Path, content: bytes, *, stage: str) -> bool:
        if path.exists() and path.read_bytes() == content:
            return False
        _ensure_private_directory(path.parent)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            self._checkpoint(f"{stage}:write", path)
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _make_private_file(temporary)
            self._checkpoint(f"{stage}:replace", path)
            os.replace(temporary, path)
            _make_private_file(path)
            return True
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def remove(self, path: Path, *, stage: str) -> bool:
        if not path.exists():
            return False
        self._checkpoint(f"{stage}:remove", path)
        path.unlink()
        return True


def initialize_project(
    project_path: str | Path,
    *,
    store: AtomicFileStore | None = None,
) -> dict[str, Any]:
    project = _project_path(project_path)
    config_path = project / CONFIG_NAME
    created = False
    if not config_path.exists():
        created = (store or AtomicFileStore()).write_json(
            config_path,
            DEFAULT_PROJECT_CONFIG,
            stage="config",
        )
    return {
        "ok": True,
        "project": str(project),
        "config": str(config_path),
        "created": created,
    }


def discover_stable_commands(
    env: Mapping[str, str] | None = None,
) -> StableCommands:
    environment = dict(os.environ if env is None else env)
    if environment.get("TGL_NPM_WRAPPER") != "1":
        raise InstallError(GLOBAL_INSTALL_GUIDANCE)
    if environment.get("npm_command", "").lower() == "exec":
        raise InstallError(GLOBAL_INSTALL_GUIDANCE)
    manifest_path = PACKAGE_ROOT / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(GLOBAL_INSTALL_GUIDANCE) from exc
    if not isinstance(manifest, dict) or manifest.get("name") != PACKAGE_NAME:
        raise InstallError(GLOBAL_INSTALL_GUIDANCE)
    bins = manifest.get("bin")
    if not isinstance(bins, dict) or (
        bins.get("tgl-claude-hook") != "bin/tgl-claude-hook.js"
        or bins.get("tgl-mcp") != "bin/tgl-mcp.js"
    ):
        raise InstallError(GLOBAL_INSTALL_GUIDANCE)

    commands: dict[str, Path] = {}
    for key, command_name, script_name in (
        ("hook", "tgl-claude-hook", "tgl-claude-hook.js"),
        ("mcp", "tgl-mcp", "tgl-mcp.js"),
    ):
        command_value = shutil.which(command_name, path=environment.get("PATH"))
        if command_value is None:
            raise InstallError(GLOBAL_INSTALL_GUIDANCE)
        command = Path(command_value).absolute()
        lowered_parts = {part.lower() for part in command.parts}
        if "_npx" in lowered_parts or ".bin" in lowered_parts:
            raise InstallError(GLOBAL_INSTALL_GUIDANCE)
        if os.name == "nt":
            expected_root = command.parent / "node_modules" / PACKAGE_NAME
            expected_script = PACKAGE_ROOT / "bin" / script_name
            if (
                _same_path(expected_root, PACKAGE_ROOT) is False
                or not expected_script.is_file()
                or not _windows_shim_targets_script(command, expected_script)
            ):
                raise InstallError(GLOBAL_INSTALL_GUIDANCE)
        else:
            if (
                PACKAGE_ROOT.name != PACKAGE_NAME
                or PACKAGE_ROOT.parent.name != "node_modules"
                or PACKAGE_ROOT.parent.parent.name != "lib"
                or _same_path(command.parent, PACKAGE_ROOT.parents[2] / "bin")
                is False
            ):
                raise InstallError(GLOBAL_INSTALL_GUIDANCE)
            expected_script = PACKAGE_ROOT / "bin" / script_name
            if _same_path(command.resolve(), expected_script.resolve()) is False:
                raise InstallError(GLOBAL_INSTALL_GUIDANCE)
        commands[key] = command
    return StableCommands(hook=commands["hook"], mcp=commands["mcp"])


def _windows_shim_targets_script(shim: Path, expected_script: Path) -> bool:
    try:
        lines = shim.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return False
    for line in lines:
        for segment in _windows_shim_segments(line):
            tokens = _windows_shim_tokens(segment)
            if len(tokens) < 3 or tokens[-1].lower() != "%*":
                continue
            executable = tokens[0].lower()
            if not _is_node_shim_executable(executable):
                continue
            target = _expand_windows_shim_path(tokens[1], shim)
            if target is not None and _same_path(target, expected_script):
                return True
    return False


def _windows_shim_segments(line: str) -> list[str]:
    """Split a cmd line at control separators while preserving quoted paths."""
    segments: list[str] = []
    start = 0
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "&()":
            if line[start:index].strip():
                segments.append(line[start:index].strip())
            start = index + 1
    if line[start:].strip():
        segments.append(line[start:].strip())
    return segments


def _windows_shim_tokens(segment: str) -> list[str]:
    tokens: list[str] = []
    for match in re.finditer(r'"([^"]*)"|([^\s]+)', segment):
        token = match.group(1) if match.group(1) is not None else match.group(2)
        if token:
            tokens.append(token)
    return tokens


def _is_node_shim_executable(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"node", "node.exe", "%_prog%", '"%_prog%"'}:
        return True
    if lowered.startswith("%") and lowered.endswith("%"):
        return lowered in {"%node%", "%node_exe%", "%node.exe%"}
    return lowered.replace("/", "\\").endswith("\\node.exe")


def _expand_windows_shim_path(value: str, shim: Path) -> Path | None:
    expanded = re.sub(
        r"%(?:~dp0|dp0%)\\?",
        lambda _match: str(shim.parent) + os.sep,
        value,
        flags=re.IGNORECASE,
    )
    if "%" in expanded:
        return None
    return Path(expanded)


def install_project(
    project_path: str | Path,
    *,
    commands: StableCommands,
    repair: bool = False,
    store: AtomicFileStore | None = None,
) -> dict[str, Any]:
    project = _project_path(project_path)
    paths = _paths(project)
    file_store = store or AtomicFileStore()
    settings = _read_json_object(paths["settings"])
    mcp_config = _read_json_object(paths["mcp"])
    ownership = _read_optional_json(paths["ownership"])
    recorded_commands = _parse_ownership(ownership)
    if repair:
        try:
            state = _read_optional_json(paths["state"])
        except InstallError:
            state = None
    else:
        state = _read_optional_json(paths["state"])
    config_exists = paths["config"].exists()
    if config_exists:
        _validate_config(paths["config"])

    server = _mcp_server(mcp_config)
    managed_hook_executables = _managed_hook_executables(
        settings,
        paths["config"],
        paths["state"],
    )
    if repair and recorded_commands is None and (
        server is not None or managed_hook_executables
    ):
        mcp_matches = (
            server is not None
            and _is_owned_mcp(server, paths["config"], commands.mcp)
        )
        hook_matches = (
            len(managed_hook_executables) == 1
            and _same_path(managed_hook_executables[0], commands.hook)
        )
        if not mcp_matches or not hook_matches:
            raise InstallError(
                "Existing managed-argument entries are not owned by the proven commands"
            )
        recorded_commands = _OwnershipRecord(hook=commands.hook, mcp=commands.mcp)
    if server is not None and (
        recorded_commands is None
        or not _is_owned_mcp(
            server,
            paths["config"],
            recorded_commands.mcp,
        )
    ):
        raise InstallError(
            f"MCP server name {SERVER_NAME!r} exists but is not owned by TGL"
        )

    target_mcp = _target_mcp(commands.mcp, paths["config"])
    target_hook = _target_hook(commands.hook, paths["config"], paths["state"])
    target_ownership = _target_ownership(commands)
    if recorded_commands is not None and (
        not _same_path(recorded_commands.hook, commands.hook)
        or not _same_path(recorded_commands.mcp, commands.mcp)
    ):
        raise InstallError(
            "Recorded TGL command paths changed; uninstall before reinstalling"
        )
    complete = state == COMPLETE_STATE
    exact = (
        config_exists
        and ownership == target_ownership
        and server == target_mcp
        and _has_exact_hook(settings, target_hook)
        and complete
    )
    if exact:
        return _install_report(project, paths, changed=False)

    has_owned = recorded_commands is not None and (
        server is not None
        or _has_owned_hook(
            settings,
            paths["config"],
            paths["state"],
            recorded_commands.hook,
        )
    )
    incomplete = paths["state"].exists() or ownership is not None or has_owned
    if incomplete and not repair:
        raise InstallError(
            "Incomplete TGL installation detected; run claude-install --repair"
        )
    changed = False
    if repair:
        changed |= file_store.remove(paths["state"], stage="state")

    init_report = initialize_project(project, store=file_store)
    changed |= bool(init_report["created"])
    _validate_config(paths["config"])
    changed |= file_store.write_json(
        paths["ownership"],
        target_ownership,
        stage="ownership",
    )

    updated_mcp = _copy_json_object(mcp_config)
    _set_mcp_server(updated_mcp, target_mcp)
    changed |= file_store.write_json(paths["mcp"], updated_mcp, stage="mcp")

    updated_settings = _copy_json_object(settings)
    _set_hook(
        updated_settings,
        command=target_hook,
        config_path=paths["config"],
        state_path=paths["state"],
        executable=commands.hook,
    )
    changed |= file_store.write_json(
        paths["settings"], updated_settings, stage="hook"
    )
    changed |= file_store.write_json(paths["state"], COMPLETE_STATE, stage="state")
    return _install_report(project, paths, changed=changed)


def uninstall_project(
    project_path: str | Path,
    *,
    store: AtomicFileStore | None = None,
) -> dict[str, Any]:
    project = _project_path(project_path)
    paths = _paths(project)
    file_store = store or AtomicFileStore()
    settings = _read_json_object(paths["settings"])
    mcp_config = _read_json_object(paths["mcp"])
    ownership = _read_optional_json(paths["ownership"])
    recorded_commands = _parse_ownership(ownership)
    changed = False

    if recorded_commands is None:
        server = _mcp_server(mcp_config)
        has_unproven_entries = (
            isinstance(server, dict)
            and _is_managed_mcp(server, paths["config"])
        ) or bool(
            _managed_hook_executables(
                settings,
                paths["config"],
                paths["state"],
            )
        )
        if paths["state"].exists() or has_unproven_entries:
            file_store.remove(paths["state"], stage="state")
            raise InstallError(
                "TGL ownership registry is missing; Hook/MCP entries were preserved "
                "because ownership cannot be proven. Run claude-install --repair "
                "from the proven global install"
            )

    updated_settings, hook_removed = (
        _without_owned_hooks(
            settings,
            config_path=paths["config"],
            state_path=paths["state"],
            executable=recorded_commands.hook,
        )
        if recorded_commands is not None
        else (_copy_json_object(settings), False)
    )
    if hook_removed:
        changed |= file_store.write_json(
            paths["settings"], updated_settings, stage="hook"
        )

    updated_mcp = _copy_json_object(mcp_config)
    servers = updated_mcp.get("mcpServers")
    if isinstance(servers, dict):
        server = servers.get(SERVER_NAME)
        if (
            isinstance(server, dict)
            and recorded_commands is not None
            and _is_owned_mcp(
                server,
                paths["config"],
                recorded_commands.mcp,
            )
        ):
            del servers[SERVER_NAME]
            changed |= file_store.write_json(
                paths["mcp"], updated_mcp, stage="mcp"
            )

    if recorded_commands is not None:
        changed |= file_store.remove(paths["state"], stage="state")
        changed |= file_store.remove(paths["ownership"], stage="ownership")
    return {
        "ok": True,
        "project": str(project),
        "changed": changed,
        "config_preserved": str(paths["config"]),
    }


def doctor_project(
    project_path: str | Path,
    *,
    integration: bool,
) -> dict[str, Any]:
    project = _project_path(project_path)
    paths = _paths(project)
    checks: list[dict[str, Any]] = []
    config_ok = _doctor_config(paths["config"])
    checks.append(_check("config", config_ok, "Config is valid" if config_ok else "Config is unavailable or invalid"))
    python_ok = sys.version_info >= (3, 10)
    checks.append(_check("python", python_ok, f"Python {sys.version_info.major}.{sys.version_info.minor}"))

    settings = _doctor_read_object(paths["settings"])
    mcp_config = _doctor_read_object(paths["mcp"])
    state = _doctor_read_value(paths["state"])
    ownership = _doctor_read_value(paths["ownership"])
    recorded_commands = _doctor_ownership(ownership)
    hook_command = (
        _owned_hook_command(
            settings,
            paths["config"],
            paths["state"],
            recorded_commands.hook,
        )
        if recorded_commands is not None
        else None
    )
    server = _mcp_server(mcp_config)
    ownership_ok = (
        hook_command is not None
        and server is not None
        and recorded_commands is not None
        and _is_owned_mcp(
            server,
            paths["config"],
            recorded_commands.mcp,
        )
    )
    command_paths = (
        [recorded_commands.hook, recorded_commands.mcp]
        if recorded_commands is not None
        else []
    )
    commands_ok = len(command_paths) == 2 and all(
        path.is_absolute() and path.is_file() for path in command_paths
    )
    checks.append(_check("commands", commands_ok, "Installed commands are available" if commands_ok else "Installed commands are unavailable"))
    checks.append(_check("install_state", state == COMPLETE_STATE, "Install state is complete" if state == COMPLETE_STATE else "Install state is incomplete or unavailable"))
    checks.append(_check("ownership", ownership_ok, "Owned entries are consistent" if ownership_ok else "Owned entries are incomplete or unavailable"))
    permission_paths = [project]
    permission_paths.extend(
        path for path in paths.values() if path.exists()
    )
    permissions_ok = project.exists() and all(
        os.access(path, os.R_OK | os.W_OK) for path in permission_paths
    )
    checks.append(_check("permissions", permissions_ok, "Project paths are readable and writable" if permissions_ok else "Project paths are not readable and writable"))

    if integration:
        hook_ok = (
            _hook_smoke(recorded_commands.hook)
            if recorded_commands is not None
            else False
        )
        mcp_ok = (
            _mcp_smoke(recorded_commands.mcp)
            if recorded_commands is not None
            else False
        )
        checks.append(_check("hook_smoke", hook_ok, "Hook smoke passed" if hook_ok else "Hook smoke failed"))
        checks.append(_check("mcp_smoke", mcp_ok, "MCP initialize/list smoke passed" if mcp_ok else "MCP initialize/list smoke failed"))
    return {
        "ok": all(bool(check["ok"]) for check in checks),
        "project": str(project),
        "checks": checks,
    }


def _project_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _paths(project: Path) -> dict[str, Path]:
    return {
        "config": project / CONFIG_NAME,
        "settings": project / SETTINGS_PATH,
        "mcp": project / MCP_PATH,
        "state": project / STATE_PATH,
        "ownership": project / OWNERSHIP_PATH,
    }


def _validate_config(path: Path) -> None:
    try:
        GovernanceConfig.load(path)
    except Exception as exc:
        raise InstallError("Project config is invalid") from exc


def _target_ownership(commands: StableCommands) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "hook_command": _normalized_path(commands.hook),
        "mcp_command": _normalized_path(commands.mcp),
    }


def _parse_ownership(value: Any) -> _OwnershipRecord | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "hook_command",
        "mcp_command",
    }:
        raise InstallError("TGL ownership registry is invalid")
    if value.get("schema_version") != 1:
        raise InstallError("TGL ownership registry is invalid")
    hook = value.get("hook_command")
    mcp = value.get("mcp_command")
    if not isinstance(hook, str) or not isinstance(mcp, str):
        raise InstallError("TGL ownership registry is invalid")
    hook_path = Path(hook)
    mcp_path = Path(mcp)
    if not hook_path.is_absolute() or not mcp_path.is_absolute():
        raise InstallError("TGL ownership registry is invalid")
    return _OwnershipRecord(hook=hook_path, mcp=mcp_path)


def _doctor_ownership(value: Any) -> _OwnershipRecord | None:
    try:
        return _parse_ownership(value)
    except InstallError:
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = _read_json(path)
    if not isinstance(value, dict):
        raise InstallError(f"Expected a JSON object in {path}")
    return value


def _read_optional_json(path: Path) -> Any:
    if not path.exists():
        return None
    return _read_json(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"Unable to read {path}") from exc


def _doctor_read_object(path: Path) -> dict[str, Any]:
    try:
        return _read_json_object(path)
    except InstallError:
        return {}


def _doctor_read_value(path: Path) -> Any:
    try:
        return _read_optional_json(path)
    except InstallError:
        return None


def _doctor_config(path: Path) -> bool:
    try:
        _validate_config(path)
    except InstallError:
        return False
    return True


def _copy_json_object(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _target_mcp(command: Path, config_path: Path) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": _normalized_path(command),
        "args": ["--config", _normalized_path(config_path)],
        "env": {},
    }


def _target_hook(command: Path, config_path: Path, state_path: Path) -> str:
    return " ".join(
        (
            _shell_quote(_normalized_path(command)),
            "--config",
            _shell_quote(_normalized_path(config_path)),
            "--install-state",
            _shell_quote(_normalized_path(state_path)),
        )
    )


def _mcp_server(value: dict[str, Any]) -> dict[str, Any] | None:
    servers = value.get("mcpServers")
    if servers is None:
        return None
    if not isinstance(servers, dict):
        raise InstallError("Expected .mcp.json mcpServers to be an object")
    server = servers.get(SERVER_NAME)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise InstallError(f"MCP server {SERVER_NAME!r} is not an object")
    return server


def _set_mcp_server(value: dict[str, Any], server: dict[str, Any]) -> None:
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError("Expected .mcp.json mcpServers to be an object")
    servers[SERVER_NAME] = server


def _is_owned_mcp(
    server: dict[str, Any], config_path: Path, executable: Path
) -> bool:
    return (
        _is_managed_mcp(server, config_path)
        and isinstance(server.get("command"), str)
        and _same_path(Path(server["command"]), executable)
    )


def _is_managed_mcp(server: dict[str, Any], config_path: Path) -> bool:
    return server.get("type") == "stdio" and server.get("args") == [
        "--config",
        _normalized_path(config_path),
    ]


def _set_hook(
    settings: dict[str, Any],
    *,
    command: str,
    config_path: Path,
    state_path: Path,
    executable: Path,
) -> None:
    cleaned, _removed = _without_owned_hooks(
        settings,
        config_path=config_path,
        state_path=state_path,
        executable=executable,
    )
    settings.clear()
    settings.update(cleaned)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("Expected settings hooks to be an object")
    post_tool_use = hooks.setdefault("PostToolUse", [])
    if not isinstance(post_tool_use, list):
        raise InstallError("Expected hooks.PostToolUse to be a list")
    post_tool_use.append(
        {
            "matcher": DEFAULT_MATCHER,
            "hooks": [{"type": "command", "command": command}],
        }
    )


def _without_owned_hooks(
    settings: dict[str, Any],
    *,
    config_path: Path,
    state_path: Path,
    executable: Path,
) -> tuple[dict[str, Any], bool]:
    result = _copy_json_object(settings)
    hooks = result.get("hooks")
    if hooks is None:
        return result, False
    if not isinstance(hooks, dict):
        raise InstallError("Expected settings hooks to be an object")
    post_tool_use = hooks.get("PostToolUse")
    if post_tool_use is None:
        return result, False
    if not isinstance(post_tool_use, list):
        raise InstallError("Expected hooks.PostToolUse to be a list")
    kept_entries = []
    removed = False
    for entry in post_tool_use:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept_entries.append(entry)
            continue
        kept_hooks = []
        for hook in entry["hooks"]:
            command = hook.get("command") if isinstance(hook, dict) else None
            if _is_owned_hook_command(
                command,
                config_path,
                state_path,
                executable,
            ):
                removed = True
            else:
                kept_hooks.append(hook)
        if kept_hooks:
            copied = dict(entry)
            copied["hooks"] = kept_hooks
            kept_entries.append(copied)
    hooks["PostToolUse"] = kept_entries
    return result, removed


def _is_owned_hook_command(
    command: Any,
    config_path: Path,
    state_path: Path,
    executable: Path,
) -> bool:
    managed_executable = _managed_hook_executable(
        command,
        config_path,
        state_path,
    )
    return managed_executable is not None and _same_path(
        managed_executable,
        executable,
    )


def _managed_hook_executable(
    command: Any,
    config_path: Path,
    state_path: Path,
) -> Path | None:
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    tokens = [token.replace("\\$", "$").replace("\\`", "`") for token in tokens]
    if len(tokens) != 5:
        return None
    command_executable, config_flag, config_value, state_flag, state_value = tokens
    if (
        not Path(command_executable).is_absolute()
        or config_flag != "--config"
        or not _same_path(Path(config_value), config_path)
        or state_flag != "--install-state"
        or not _same_path(Path(state_value), state_path)
    ):
        return None
    return Path(command_executable)


def _managed_hook_executables(
    settings: dict[str, Any],
    config_path: Path,
    state_path: Path,
) -> list[Path]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get("PostToolUse")
    if not isinstance(entries, list):
        return []
    executables = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for hook in entry["hooks"]:
            command = hook.get("command") if isinstance(hook, dict) else None
            executable = _managed_hook_executable(command, config_path, state_path)
            if executable is not None:
                executables.append(executable)
    return executables


def _has_owned_hook(
    settings: dict[str, Any],
    config_path: Path,
    state_path: Path,
    executable: Path,
) -> bool:
    return (
        _owned_hook_command(settings, config_path, state_path, executable)
        is not None
    )


def _owned_hook_command(
    settings: dict[str, Any],
    config_path: Path,
    state_path: Path,
    executable: Path,
) -> str | None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return None
    entries = hooks.get("PostToolUse")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for hook in entry["hooks"]:
            command = hook.get("command") if isinstance(hook, dict) else None
            if _is_owned_hook_command(
                command,
                config_path,
                state_path,
                executable,
            ):
                return command
    return None


def _has_exact_hook(settings: dict[str, Any], command: str) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get("PostToolUse")
    if not isinstance(entries, list):
        return False
    matches = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for hook in entry["hooks"]:
            if isinstance(hook, dict) and hook.get("command") == command:
                matches += 1
    return matches == 1


def _install_report(
    project: Path, paths: dict[str, Path], *, changed: bool
) -> dict[str, Any]:
    return {
        "ok": True,
        "project": str(project),
        "changed": changed,
        "config": str(paths["config"]),
        "settings": str(paths["settings"]),
        "mcp": str(paths["mcp"]),
        "state": str(paths["state"]),
    }


def _hook_smoke(command: Path) -> bool:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = _subprocess_env()
        try:
            result = subprocess.run(
                _installed_invocation(
                    command,
                    [
                    "--db",
                    str(root / "ledger.sqlite"),
                    "--install-state",
                    str(root / "missing-state.json"),
                    ],
                ),
                input="{}\n",
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=10,
                env=env,
            )
            return result.returncode == 0 and json.loads(result.stdout) == {
                "continue": True
            }
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return False


def _mcp_smoke(command: Path) -> bool:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "tgl-doctor", "version": "1.0.0"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        try:
            result = subprocess.run(
                _installed_invocation(
                    command,
                    ["--db", str(root / "ledger.sqlite")],
                ),
                input="".join(json.dumps(message) + "\n" for message in messages),
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=10,
                env=_subprocess_env(),
            )
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            return (
                result.returncode == 0
                and [response.get("id") for response in responses] == [1, 2]
                and responses[0]["result"]["protocolVersion"] == "2025-06-18"
                and bool(responses[1]["result"]["tools"])
            )
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError):
            return False


def _installed_invocation(command: Path, args: list[str]) -> list[str]:
    if os.name == "nt" and command.suffix.lower() in {".cmd", ".bat"}:
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            "call",
            str(command),
            *args,
        ]
    return [str(command), *args]


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = (
        src_path + os.pathsep + env["PYTHONPATH"]
        if env.get("PYTHONPATH")
        else src_path
    )
    return env


def _check(name: str, ok: bool, message: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "message": message}


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _make_private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _shell_quote(value: str) -> str:
    escaped = value.replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def _normalized_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(
            str(right.resolve())
        )
    except OSError:
        return False
