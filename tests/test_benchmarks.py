import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "benchmarks" / "fixtures" / "manifest.json"
RESULT = ROOT / "benchmarks" / "results" / "v0.2.0.json"


def run_benchmark(manifest=MANIFEST, output=None):
    output = output or ROOT / ".tgl" / "benchmark-test.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "benchmarks.run", "--manifest", str(manifest), "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_manifest_contains_required_case_matrix():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["version"] == 1
    ids = {case["id"] for case in data["cases"]}
    for required in {
        "repetitive_log",
        "test_output",
        "build_output",
        "protected_middle",
        "secret_first",
        "secret_middle",
        "secret_end",
        "source_passthrough",
        "json_passthrough",
        "unicode",
        "empty",
        "malformed",
        "large",
    }:
        assert required in ids


def test_canonical_result_validates_and_has_protected_evidence():
    from benchmarks.run import validate_result

    data = json.loads(RESULT.read_text(encoding="utf-8"))
    assert validate_result(data) == []
    assert data["label"] == "estimated_candidate_microbenchmark"
    assert data["aggregate"]["cases"] == len(data["cases"])
    assert all("protected_fact_evidence" in case for case in data["cases"])


def test_canonical_command_is_byte_stable(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert run_benchmark(output=first).returncode == 0
    assert run_benchmark(output=second).returncode == 0
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    assert first.read_bytes() == RESULT.read_bytes()


def test_altered_manifest_is_rejected(tmp_path):
    altered = tmp_path / "manifest.json"
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["cases"][0]["stdout"] = "tampered\n"
    altered.write_text(json.dumps(data), encoding="utf-8")
    completed = run_benchmark(manifest=altered, output=tmp_path / "result.json")
    assert completed.returncode != 0
    assert "manifest" in (completed.stderr + completed.stdout).lower()


def test_checker_extracts_markers_and_rejects_drift(tmp_path):
    from benchmarks.check_readme import check_readme
    digest = hashlib.sha256(RESULT.read_bytes()).hexdigest()

    readme = tmp_path / "README.md"
    readme.write_text(
        "<!-- TGL-BENCHMARK:START -->\n"
        f"<!-- TGL-BENCHMARK:RESULT-SHA256 {digest} -->\n"
        "<!-- TGL-BENCHMARK:END -->\n",
        encoding="utf-8",
    )
    assert check_readme(RESULT, readme) == []
    readme.write_text(readme.read_text(encoding="utf-8").replace("RESULT-SHA256", "WRONG"), encoding="utf-8")
    assert check_readme(RESULT, readme)


def test_schema_rejects_missing_case_field():
    from benchmarks.run import validate_result

    data = json.loads(RESULT.read_text(encoding="utf-8"))
    del data["cases"][0]["action"]
    assert validate_result(data)
