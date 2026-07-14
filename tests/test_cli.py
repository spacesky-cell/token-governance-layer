import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(args, input_text=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "token_governance.cli", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


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


def test_cli_claude_install_writes_project_hook_and_mcp_config(tmp_path):
    project_path = tmp_path / "project"
    db_path = project_path / ".tgl" / "claude-ledger.sqlite"
    hook_command = tmp_path / "bin" / "tgl-claude-hook.exe"
    mcp_command = tmp_path / "bin" / "tgl-mcp.exe"

    result = run_cli(
        [
            "--db",
            str(db_path),
            "claude-install",
            "--project",
            str(project_path),
            "--hook-command",
            str(hook_command),
            "--mcp-command",
            str(mcp_command),
        ]
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True

    settings = json.loads((project_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    post_tool_use = settings["hooks"]["PostToolUse"]
    assert len(post_tool_use) == 1
    hook = post_tool_use[0]
    assert hook["matcher"] == "Bash|PowerShell|Read|Grep|Glob|LS|Task|WebFetch|WebSearch"
    command = hook["hooks"][0]["command"]
    assert "tgl-claude-hook.exe" in command
    assert "--db" in command
    assert "\\" not in command
    assert str(db_path).replace("\\", "/") in command

    mcp_config = json.loads((project_path / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp_config["mcpServers"]["token-governance-layer"]
    assert server["type"] == "stdio"
    assert server["command"] == str(mcp_command).replace("\\", "/")
    assert server["args"] == ["--db", str(db_path).replace("\\", "/")]
    assert server["env"] == {}


def test_cli_claude_install_preserves_existing_hooks_and_is_idempotent(tmp_path):
    project_path = tmp_path / "project"
    settings_path = project_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "existing-audit-hook",
                                }
                            ],
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "old-tgl-claude-hook --db old.sqlite",
                                }
                            ],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (project_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"existing-server": {"command": "keep-me"}}}),
        encoding="utf-8",
    )

    args = [
        "--db",
        str(project_path / ".tgl" / "claude-ledger.sqlite"),
        "claude-install",
        "--project",
        str(project_path),
        "--hook-command",
        str(tmp_path / "bin" / "tgl-claude-hook.exe"),
        "--mcp-command",
        str(tmp_path / "bin" / "tgl-mcp.exe"),
    ]

    first = run_cli(args)
    second = run_cli(args)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entry in settings["hooks"]["PostToolUse"]
        for hook in entry["hooks"]
    ]
    assert commands.count("existing-audit-hook") == 1
    assert sum("tgl-claude-hook" in command for command in commands) == 1

    mcp_config = json.loads((project_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "existing-server" in mcp_config["mcpServers"]
    assert "token-governance-layer" in mcp_config["mcpServers"]
