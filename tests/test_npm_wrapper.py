import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def run_node_bin(script_name, args, *, input_text=None, env=None):
    if not NODE:
        pytest.skip("node is not installed")
    return subprocess.run(
        [NODE, str(ROOT / "bin" / script_name), *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize(
    ("major", "minor", "supported"),
    [(3, 9, False), (3, 10, True), (3, 14, True), (3, 15, False)],
)
def test_npm_wrapper_python_version_boundary(major, minor, supported):
    if not NODE:
        pytest.skip("node is not installed")
    runner_path = ROOT / "bin" / "tgl-python-runner.js"
    script = (
        f"const runner = require({json.dumps(str(runner_path))});"
        f"process.stdout.write(String(runner.isSupportedPythonVersion({major}, {minor})));"
    )

    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(supported).lower()


def test_npm_wrapper_reports_exact_supported_python_range_when_none_found(tmp_path):
    env = dict(os.environ)
    env["PATH"] = str(tmp_path)

    result = run_node_bin("tgl.js", ["--help"], env=env)

    assert result.returncode == 127
    assert "requires Python 3.10-3.14 on PATH" in result.stderr
    assert "Python 3.10+" not in result.stderr


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
    payload = "NPM wrapper repeated output\n" * 120

    govern = run_node_bin(
        "tgl.js",
        ["--db", str(db_path), "govern", "--strategy", "repetitive_log"],
        input_text=payload,
    )

    assert govern.returncode == 0, govern.stderr
    governed = json.loads(govern.stdout)
    assert governed["receipt_id"]
    assert governed["tokens_saved"] > 0

    retrieve = run_node_bin("tgl.js", ["--db", str(db_path), "retrieve", governed["receipt_id"]])

    assert retrieve.returncode == 0, retrieve.stderr
    assert retrieve.stdout == payload


def test_source_npm_wrapper_rejects_persistent_install_without_mutation(tmp_path):
    project_path = tmp_path / "project"

    result = run_node_bin("tgl.js", ["claude-install", "--project", str(project_path)])

    assert result.returncode == 2
    assert "npm install -g token-governance-layer" in result.stderr
    assert not project_path.exists()
