import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_cli(args, input_text=None, *, env_overrides=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "token_governance.cli", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def fake_global_install_env(tmp_path):
    prefix = tmp_path / "全局 npm with spaces"
    if os.name == "nt":
        package_root = prefix / "node_modules" / "token-governance-layer"
        shim_dir = prefix
    else:
        package_root = prefix / "lib" / "node_modules" / "token-governance-layer"
        shim_dir = prefix / "bin"
    shutil.copytree(ROOT / "src", package_root / "src")
    shutil.copy2(ROOT / "package.json", package_root / "package.json")
    shutil.copytree(ROOT / "bin", package_root / "bin")
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name, script in (
        ("tgl-claude-hook", "tgl-claude-hook.js"),
        ("tgl-mcp", "tgl-mcp.js"),
    ):
        if os.name == "nt":
            relative_target = (
                f"%~dp0\\node_modules\\token-governance-layer\\bin\\{script}"
            )
            (shim_dir / f"{name}.CMD").write_text(
                f'@echo off\nnode "{relative_target}" %*\n',
                encoding="utf-8",
            )
        else:
            shim = shim_dir / name
            shim.symlink_to(package_root / "bin" / script)
    return {
        "PYTHONPATH": str(package_root / "src"),
        "TGL_NPM_WRAPPER": "1",
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
    }


def test_cli_govern_and_retrieve_roundtrip(tmp_path):
    db_path = tmp_path / "tgl.sqlite"
    payload = "ordinary repeated cli line\n" * 120

    govern = run_cli(
        ["--db", str(db_path), "govern", "--strategy", "repetitive_log"],
        input_text=payload,
    )

    assert govern.returncode == 0, govern.stderr
    governed = json.loads(govern.stdout)
    assert governed["receipt_id"]
    assert governed["token_after"] < governed["token_before"]

    retrieve = run_cli(["--db", str(db_path), "retrieve", governed["receipt_id"]])

    assert retrieve.returncode == 0, retrieve.stderr
    assert retrieve.stdout == payload


def test_cli_auto_means_no_explicit_strategy_and_passthrough_has_no_receipt(tmp_path):
    db_path = tmp_path / "tgl.sqlite"

    result = run_cli(
        ["--db", str(db_path), "govern", "--strategy", "auto"],
        input_text="repeated auto line\n" * 120,
    )

    assert result.returncode == 0, result.stderr
    governed = json.loads(result.stdout)
    assert governed["action"] == "passthrough"
    assert governed["receipt_id"] is None


def test_cli_rejects_legacy_govern_arguments_with_fixed_guidance(tmp_path):
    result = run_cli(
        [
            "--db",
            str(tmp_path / "tgl.sqlite"),
            "govern",
            "--content-type",
            "log",
        ],
        input_text="payload",
    )

    assert result.returncode == 2
    assert (
        "content_type/source were removed; use --strategy "
        "auto|repetitive_log|test_output|build_output"
    ) in result.stderr


@pytest.mark.parametrize(
    ("strategy", "payload"),
    [
        (
            "test_output",
            "============================= test session starts =============================\n"
            "collecting ...\ncollecting ...\ncollecting ...\n"
            "============================== 3 passed in 0.10s ==============================\n",
        ),
        (
            "build_output",
            "[1/3] compiling a.cc\n[2/3] compiling b.cc\n[3/3] compiling c.cc\n"
            "build succeeded\n",
        ),
    ],
)
def test_cli_explicit_structured_strategies_use_v2_engine(tmp_path, strategy, payload):
    result = run_cli(
        [
            "--db",
            str(tmp_path / f"{strategy}.sqlite"),
            "govern",
            "--strategy",
            strategy,
        ],
        input_text=payload,
    )

    assert result.returncode == 0, result.stderr
    governed = json.loads(result.stdout)
    assert governed["action"] == "transform"
    assert governed["strategy"] == strategy
    assert governed["tokens_saved"] > 0


def test_cli_doctor_reports_database_path(tmp_path):
    db_path = tmp_path / "doctor.sqlite"

    result = run_cli(["--db", str(db_path), "doctor"])

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["database"].endswith("doctor.sqlite")


def test_cli_doctor_validates_good_config(tmp_path):
    db_path = tmp_path / "ledger.sqlite"
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": {"path": str(db_path)},
                "gateway": {
                    "tool_policy": {"allow": [], "deny": ["backend::read_symbol"]},
                    "backends": [
                        {
                            "name": "backend",
                            "command": sys.executable,
                            "args": ["-m", "token_governance.mcp_server"],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(["doctor", "--config", str(config_path)])

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    checks = {check["name"]: check["ok"] for check in report["checks"]}
    assert checks["config_load"] is True
    assert checks["ledger_writable"] is True
    assert checks["backends_valid"] is True
    assert checks["tool_policy_valid"] is True
    assert checks["backend_command:backend"] is True


def test_cli_doctor_reports_config_errors(tmp_path):
    config_path = tmp_path / "token-governance.config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": {"path": str(tmp_path / "ledger.sqlite")},
                "gateway": {
                    "tool_policy": {"deny": "backend::read_symbol"},
                    "backends": [
                        {"name": "dup", "command": "definitely-not-a-real-tgl-command"},
                        {"name": "dup", "command": sys.executable},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(["doctor", "--config", str(config_path)])

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    failed = {check["name"]: check["message"] for check in report["checks"] if not check["ok"]}
    assert "backends_valid" in failed
    assert "Backend names must be unique" in failed["backends_valid"]
    assert "backend_command:dup" in failed
    assert "not found" in failed["backend_command:dup"]
    assert "tool_policy_valid" in failed
    assert "deny" in failed["tool_policy_valid"]


def test_cli_generates_installed_gateway_mcp_config(tmp_path):
    config_path = tmp_path / "token-governance.config.json"

    result = run_cli(["mcp-config", "--config", str(config_path)])

    assert result.returncode == 0, result.stderr
    snippet = json.loads(result.stdout)
    server = snippet["mcpServers"]["token-governance-gateway"]
    assert server == {
        "command": "tgl-mcp-gateway",
        "args": ["--config", str(config_path)],
    }


def test_cli_generates_source_checkout_gateway_mcp_config(tmp_path):
    config_path = tmp_path / "token-governance.config.json"
    source_root = tmp_path / "checkout"

    result = run_cli(
        [
            "mcp-config",
            "--config",
            str(config_path),
            "--server-name",
            "tgl-dev",
            "--source-checkout",
            str(source_root),
        ]
    )

    assert result.returncode == 0, result.stderr
    snippet = json.loads(result.stdout)
    server = snippet["mcpServers"]["tgl-dev"]
    assert server["command"] == sys.executable
    assert server["args"] == [
        "-m",
        "token_governance.mcp_gateway",
        "--config",
        str(config_path),
    ]
    assert server["env"]["PYTHONPATH"] == str(source_root / "src")


def test_cli_init_creates_config_once_without_overwriting_user_values(tmp_path):
    project = tmp_path / "项目 with spaces"

    first = run_cli(["init", "--project", str(project)])
    config_path = project / "token-governance.config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["ledger"]["retention_days"] = 99
    config_path.write_text(json.dumps(config), encoding="utf-8")
    customized = config_path.read_bytes()
    second = run_cli(["init", "--project", str(project)])

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["created"] is True
    assert json.loads(second.stdout)["created"] is False
    assert config_path.read_bytes() == customized


def test_cli_claude_install_rejects_unproven_source_before_project_mutation(tmp_path):
    project = tmp_path / "project"

    result = run_cli(["claude-install", "--project", str(project)])

    assert result.returncode == 2
    assert "npm install -g token-governance-layer" in result.stderr
    assert not project.exists()


def test_cli_global_install_supports_unicode_paths_and_full_lifecycle(tmp_path):
    project = tmp_path / "用户 project with spaces"
    env = fake_global_install_env(tmp_path)

    installed = run_cli(
        ["claude-install", "--project", str(project)],
        env_overrides=env,
    )
    before_doctor = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    doctor = run_cli(
        ["doctor", "--project", str(project), "--integration"],
        env_overrides=env,
    )
    after_doctor = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    removed = run_cli(
        ["claude-uninstall", "--project", str(project)],
        env_overrides=env,
    )

    assert installed.returncode == 0, installed.stderr
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["ok"] is True
    assert after_doctor == before_doctor
    assert removed.returncode == 0, removed.stderr
    assert (project / "token-governance.config.json").exists()
    assert not (project / ".tgl" / "install-state.json").exists()


def test_cli_incomplete_install_requires_explicit_repair(tmp_path):
    project = tmp_path / "project"
    env = fake_global_install_env(tmp_path)
    installed = run_cli(
        ["claude-install", "--project", str(project)], env_overrides=env
    )
    assert installed.returncode == 0, installed.stderr
    state = project / ".tgl" / "install-state.json"
    state.write_text('{"schema_version":1,"status":"incomplete"}\n', encoding="utf-8")
    before = state.read_bytes()

    refused = run_cli(
        ["claude-install", "--project", str(project)], env_overrides=env
    )

    assert refused.returncode == 2
    assert "--repair" in refused.stderr
    assert state.read_bytes() == before

    repaired = run_cli(
        ["claude-install", "--project", str(project), "--repair"],
        env_overrides=env,
    )

    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "complete"


def test_cli_uninstall_without_ownership_fails_open_with_actionable_repair(tmp_path):
    project = tmp_path / "project"
    env = fake_global_install_env(tmp_path)
    installed = run_cli(
        ["claude-install", "--project", str(project)], env_overrides=env
    )
    assert installed.returncode == 0, installed.stderr
    (project / ".tgl" / "install-ownership.json").unlink()
    settings_before = (project / ".claude" / "settings.json").read_bytes()
    mcp_before = (project / ".mcp.json").read_bytes()

    removed = run_cli(
        ["claude-uninstall", "--project", str(project)], env_overrides=env
    )

    assert removed.returncode == 2
    assert "entries were preserved" in removed.stderr
    assert "claude-install --repair" in removed.stderr
    assert not (project / ".tgl" / "install-state.json").exists()
    assert (project / ".claude" / "settings.json").read_bytes() == settings_before
    assert (project / ".mcp.json").read_bytes() == mcp_before
