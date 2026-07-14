import json
import os
import stat

import pytest

from token_governance.installer import (
    AtomicFileStore,
    InstallError,
    StableCommands,
    discover_stable_commands,
    doctor_project,
    initialize_project,
    install_project,
    uninstall_project,
)
from token_governance import installer as installer_module


def stable_commands(tmp_path):
    bin_dir = tmp_path / "全局 bin with spaces"
    bin_dir.mkdir()
    hook = bin_dir / "tgl-claude-hook"
    mcp = bin_dir / "tgl-mcp"
    hook.write_text("hook", encoding="utf-8")
    mcp.write_text("mcp", encoding="utf-8")
    return StableCommands(hook=hook.resolve(), mcp=mcp.resolve())


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def project_snapshot(project):
    if not project.exists():
        return {}
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }


def owned_hook_commands(project):
    settings = read_json(project / ".claude" / "settings.json")
    return [
        hook["command"]
        for entry in settings["hooks"]["PostToolUse"]
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict) and "command" in hook
    ]


def test_init_creates_private_typed_config_once_without_overwriting(tmp_path):
    project = tmp_path / "项目 with spaces"

    first = initialize_project(project)
    config_path = project / "token-governance.config.json"
    original = config_path.read_bytes()
    config = read_json(config_path)
    config["policy"]["max_payload_bytes"] = 12345
    config_path.write_text(json.dumps(config), encoding="utf-8")
    customized = config_path.read_bytes()
    second = initialize_project(project)

    assert first["created"] is True
    assert original.endswith(b"\n")
    assert read_json(config_path)["ledger"]["path"] == ".tgl/ledger.sqlite"
    assert second["created"] is False
    assert config_path.read_bytes() == customized
    if os.name != "nt":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_global_command_discovery_proves_package_owned_absolute_shims(
    tmp_path, monkeypatch
):
    prefix = tmp_path / "stable global"
    if os.name == "nt":
        package_root = prefix / "node_modules" / "token-governance-layer"
        hook = prefix / "tgl-claude-hook.CMD"
        mcp = prefix / "tgl-mcp.CMD"
    else:
        package_root = prefix / "lib" / "node_modules" / "token-governance-layer"
        hook = prefix / "bin" / "tgl-claude-hook"
        mcp = prefix / "bin" / "tgl-mcp"
    (package_root / "bin").mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "token-governance-layer",
                "bin": {
                    "tgl-claude-hook": "bin/tgl-claude-hook.js",
                    "tgl-mcp": "bin/tgl-mcp.js",
                },
            }
        ),
        encoding="utf-8",
    )
    targets = {
        "tgl-claude-hook": package_root / "bin" / "tgl-claude-hook.js",
        "tgl-mcp": package_root / "bin" / "tgl-mcp.js",
    }
    for target in targets.values():
        target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    hook.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        hook.write_text("@echo off\n", encoding="utf-8")
        mcp.write_text("@echo off\n", encoding="utf-8")
    else:
        hook.symlink_to(targets["tgl-claude-hook"])
        mcp.symlink_to(targets["tgl-mcp"])
    monkeypatch.setattr(installer_module, "PACKAGE_ROOT", package_root)
    monkeypatch.setattr(
        installer_module.shutil,
        "which",
        lambda name, path=None: str(
            hook if name == "tgl-claude-hook" else mcp
        ),
    )

    commands = discover_stable_commands(
        {"TGL_NPM_WRAPPER": "1", "PATH": str(prefix)}
    )

    assert commands == StableCommands(hook=hook.absolute(), mcp=mcp.absolute())


def test_global_command_discovery_rejects_manifest_with_unowned_bin_mapping(
    tmp_path, monkeypatch
):
    prefix = tmp_path / "global"
    if os.name == "nt":
        package_root = prefix / "node_modules" / "token-governance-layer"
        hook = prefix / "tgl-claude-hook.CMD"
        mcp = prefix / "tgl-mcp.CMD"
    else:
        package_root = prefix / "lib" / "node_modules" / "token-governance-layer"
        hook = prefix / "bin" / "tgl-claude-hook"
        mcp = prefix / "bin" / "tgl-mcp"
    (package_root / "bin").mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "token-governance-layer",
                "bin": {
                    "tgl-claude-hook": "../other-hook.js",
                    "tgl-mcp": "../other-mcp.js",
                },
            }
        ),
        encoding="utf-8",
    )
    targets = [
        package_root / "bin" / "tgl-claude-hook.js",
        package_root / "bin" / "tgl-mcp.js",
    ]
    for target in targets:
        target.write_text("script", encoding="utf-8")
    hook.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        hook.write_text("shim", encoding="utf-8")
        mcp.write_text("shim", encoding="utf-8")
    else:
        hook.symlink_to(targets[0])
        mcp.symlink_to(targets[1])
    monkeypatch.setattr(installer_module, "PACKAGE_ROOT", package_root)
    monkeypatch.setattr(
        installer_module.shutil,
        "which",
        lambda name, path=None: str(
            hook if name == "tgl-claude-hook" else mcp
        ),
    )

    with pytest.raises(InstallError, match="npm install -g"):
        discover_stable_commands({"TGL_NPM_WRAPPER": "1", "PATH": str(prefix)})


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TGL_NPM_WRAPPER": "1", "npm_command": "exec", "PATH": "ignored"},
    ],
)
def test_global_command_discovery_rejects_unproven_or_npx_invocation(env):
    with pytest.raises(InstallError, match="npm install -g"):
        discover_stable_commands(env)


def test_install_order_preserves_unrelated_entries_and_writes_exact_complete_state(
    tmp_path,
):
    project = tmp_path / "项目 with spaces"
    settings_path = project / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "theme": "keep",
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {"type": "command", "command": "user-audit-hook"}
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"user-server": {"command": "keep"}}}),
        encoding="utf-8",
    )
    commands = stable_commands(tmp_path)
    checkpoints = []
    store = AtomicFileStore(checkpoint=lambda point, _path: checkpoints.append(point))

    report = install_project(project, commands=commands, store=store)

    assert report["ok"] is True
    assert checkpoints == [
        "config:write",
        "config:replace",
        "mcp:write",
        "mcp:replace",
        "hook:write",
        "hook:replace",
        "state:write",
        "state:replace",
    ]
    assert read_json(project / ".tgl" / "install-state.json") == {
        "schema_version": 1,
        "status": "complete",
    }
    settings = read_json(settings_path)
    assert settings["theme"] == "keep"
    assert "user-audit-hook" in owned_hook_commands(project)
    owned_command = next(
        command
        for command in owned_hook_commands(project)
        if "--install-state" in command
    )
    assert str(commands.hook).replace("\\", "/") in owned_command
    assert str(project.resolve()).replace("\\", "/") in owned_command
    mcp = read_json(project / ".mcp.json")["mcpServers"]
    assert mcp["user-server"] == {"command": "keep"}
    assert mcp["token-governance-layer"] == {
        "type": "stdio",
        "command": str(commands.mcp).replace("\\", "/"),
        "args": [
            "--config",
            str((project / "token-governance.config.json").resolve()).replace(
                "\\", "/"
            ),
        ],
        "env": {},
    }


def test_complete_install_is_byte_idempotent(tmp_path):
    project = tmp_path / "project"
    commands = stable_commands(tmp_path)
    install_project(project, commands=commands)
    before = project_snapshot(project)

    report = install_project(project, commands=commands)

    assert report["changed"] is False
    assert project_snapshot(project) == before


@pytest.mark.parametrize(
    "failure_point",
    [
        "config:write",
        "config:replace",
        "mcp:write",
        "mcp:replace",
        "hook:write",
        "hook:replace",
        "state:write",
        "state:replace",
    ],
)
def test_install_failure_at_every_atomic_boundary_is_fail_open_and_repairable(
    tmp_path,
    failure_point,
):
    project = tmp_path / "project"
    commands = stable_commands(tmp_path)

    def fail_at(point, _path):
        if point == failure_point:
            raise OSError(f"injected {point}")

    with pytest.raises(OSError, match="injected"):
        install_project(
            project,
            commands=commands,
            store=AtomicFileStore(checkpoint=fail_at),
        )

    assert not (project / ".tgl" / "install-state.json").exists()
    has_owned_mcp = (
        project / ".mcp.json"
    ).exists() and "token-governance-layer" in read_json(
        project / ".mcp.json"
    ).get(
        "mcpServers", {}
    )
    has_owned_hook = (project / ".claude" / "settings.json").exists() and any(
        "--install-state" in command for command in owned_hook_commands(project)
    )
    if has_owned_mcp or has_owned_hook:
        with pytest.raises(InstallError, match="--repair"):
            install_project(project, commands=commands)
        install_project(project, commands=commands, repair=True)
    else:
        install_project(project, commands=commands)
    assert read_json(project / ".tgl" / "install-state.json")["status"] == "complete"


def test_incomplete_owned_state_requires_explicit_repair(tmp_path):
    project = tmp_path / "project"
    commands = stable_commands(tmp_path)
    install_project(project, commands=commands)
    (project / ".tgl" / "install-state.json").write_text(
        '{"schema_version":1,"status":"incomplete"}\n', encoding="utf-8"
    )
    before = project_snapshot(project)

    with pytest.raises(InstallError, match="--repair"):
        install_project(project, commands=commands)

    assert project_snapshot(project) == before
    install_project(project, commands=commands, repair=True)
    assert read_json(project / ".tgl" / "install-state.json")["status"] == "complete"


def test_install_refuses_unowned_server_name_conflict_without_mutation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    mcp_path = project / ".mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "token-governance-layer": {"command": "user-owned-command"}
                }
            }
        ),
        encoding="utf-8",
    )
    before = project_snapshot(project)

    with pytest.raises(InstallError, match="not owned"):
        install_project(project, commands=stable_commands(tmp_path))

    assert project_snapshot(project) == before


def test_uninstall_removes_only_exact_owned_entries_and_preserves_config_ledger(tmp_path):
    project = tmp_path / "project"
    commands = stable_commands(tmp_path)
    install_project(project, commands=commands)
    config_path = project / "token-governance.config.json"
    ledger_path = project / ".tgl" / "ledger.sqlite"
    ledger_path.write_bytes(b"ledger stays")
    settings = read_json(project / ".claude" / "settings.json")
    settings["hooks"]["PostToolUse"].append(
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": "echo tgl-claude-hook --install-state not-owned",
                }
            ],
        }
    )
    (project / ".claude" / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )

    report = uninstall_project(project)

    assert report["ok"] is True
    assert config_path.exists()
    assert ledger_path.read_bytes() == b"ledger stays"
    assert not (project / ".tgl" / "install-state.json").exists()
    assert owned_hook_commands(project) == [
        "echo tgl-claude-hook --install-state not-owned"
    ]
    assert "token-governance-layer" not in read_json(project / ".mcp.json")[
        "mcpServers"
    ]


@pytest.mark.parametrize(
    "failure_point",
    [
        "hook:write",
        "hook:replace",
        "mcp:write",
        "mcp:replace",
        "state:remove",
    ],
)
def test_uninstall_failure_at_every_boundary_is_recoverable(tmp_path, failure_point):
    project = tmp_path / "project"
    install_project(project, commands=stable_commands(tmp_path))
    config_before = (project / "token-governance.config.json").read_bytes()
    ledger = project / ".tgl" / "ledger.sqlite"
    ledger.write_bytes(b"preserve")

    def fail_at(point, _path):
        if point == failure_point:
            raise OSError(f"injected {point}")

    with pytest.raises(OSError, match="injected"):
        uninstall_project(
            project,
            store=AtomicFileStore(checkpoint=fail_at),
        )

    uninstall_project(project)

    assert (project / "token-governance.config.json").read_bytes() == config_before
    assert ledger.read_bytes() == b"preserve"
    assert not (project / ".tgl" / "install-state.json").exists()
    assert not any("--install-state" in command for command in owned_hook_commands(project))
    assert "token-governance-layer" not in read_json(project / ".mcp.json")[
        "mcpServers"
    ]


def test_uninstall_is_byte_idempotent_when_no_owned_entries_exist(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text(
        '{"mcpServers":{"token-governance-layer":{"command":"user"}}}\n',
        encoding="utf-8",
    )
    before = project_snapshot(project)

    report = uninstall_project(project)

    assert report["changed"] is False
    assert project_snapshot(project) == before


def test_shell_metacharacters_preserve_exact_hook_ownership_roundtrip(tmp_path):
    project = tmp_path / "项目 $ with spaces"
    install_project(project, commands=stable_commands(tmp_path))

    diagnosed = doctor_project(project, integration=False)
    removed = uninstall_project(project)

    assert diagnosed["ok"] is True
    assert removed["changed"] is True
    assert not (project / ".tgl" / "install-state.json").exists()
    assert not any("--install-state" in command for command in owned_hook_commands(project))


def test_doctor_integration_is_byte_for_byte_read_only(tmp_path):
    project = tmp_path / "project"
    install_project(project, commands=stable_commands(tmp_path))
    before = project_snapshot(project)

    report = doctor_project(project, integration=True)

    assert report["ok"] is True
    assert project_snapshot(project) == before
    checks = {check["name"]: check["ok"] for check in report["checks"]}
    assert checks == {
        "config": True,
        "python": True,
        "commands": True,
        "install_state": True,
        "ownership": True,
        "permissions": True,
        "hook_smoke": True,
        "mcp_smoke": True,
    }


def test_doctor_reports_incomplete_state_without_repairing(tmp_path):
    project = tmp_path / "project"
    commands = stable_commands(tmp_path)
    install_project(project, commands=commands)
    (project / ".tgl" / "install-state.json").write_text(
        '{"schema_version":1,"status":"incomplete"}\n', encoding="utf-8"
    )
    before = project_snapshot(project)

    report = doctor_project(project, integration=False)

    assert report["ok"] is False
    assert project_snapshot(project) == before
    failed = {check["name"] for check in report["checks"] if not check["ok"]}
    assert "install_state" in failed
