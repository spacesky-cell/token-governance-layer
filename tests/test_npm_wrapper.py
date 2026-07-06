import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def run_node_bin(script_name, args, *, input_text=None):
    if not NODE:
        pytest.skip("node is not installed")
    return subprocess.run(
        [NODE, str(ROOT / "bin" / script_name), *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def test_package_json_exposes_cli_bins():
    package_json = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert package_json["name"] == "token-governance-layer"
    assert package_json["bin"] == {
        "token-governance-layer": "bin/tgl.js",
        "tgl": "bin/tgl.js",
        "tgl-mcp": "bin/tgl-mcp.js",
        "tgl-mcp-gateway": "bin/tgl-mcp-gateway.js",
        "tgl-claude-hook": "bin/tgl-claude-hook.js",
    }
    assert "src/token_governance/*.py" in package_json["files"]


def test_npm_tgl_wrapper_runs_python_cli_help():
    result = run_node_bin("tgl.js", ["--help"])

    assert result.returncode == 0, result.stderr
    assert "claude-install" in result.stdout


def test_npm_tgl_wrapper_govern_retrieve_roundtrip(tmp_path):
    db_path = tmp_path / "ledger.sqlite"
    payload = "\n".join(f"NPM wrapper repeated output {i % 3}" for i in range(90))

    govern = run_node_bin(
        "tgl.js",
        ["--db", str(db_path), "govern", "--content-type", "log"],
        input_text=payload,
    )

    assert govern.returncode == 0, govern.stderr
    governed = json.loads(govern.stdout)
    assert governed["receipt_id"]
    assert governed["tokens_saved"] > 0

    retrieve = run_node_bin("tgl.js", ["--db", str(db_path), "retrieve", governed["receipt_id"]])

    assert retrieve.returncode == 0, retrieve.stderr
    assert retrieve.stdout == payload


def test_npm_tgl_wrapper_claude_install_uses_npm_bin_commands(tmp_path):
    project_path = tmp_path / "project"

    result = run_node_bin("tgl.js", ["claude-install", "--project", str(project_path)])

    assert result.returncode == 0, result.stderr
    settings = json.loads((project_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hook_command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert hook_command.startswith('"tgl-claude-hook" --db ')

    mcp_config = json.loads((project_path / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp_config["mcpServers"]["token-governance-layer"]
    assert server["command"] == "tgl-mcp"
    assert server["args"][0] == "--db"
