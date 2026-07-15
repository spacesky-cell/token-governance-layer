import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

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
    for case in data["cases"]:
        assert case["preservation"] is True
        assert case["receipt_created"] is (case["action"] == "transform")
    for case_id in ("secret_first", "secret_middle", "secret_end"):
        case = next(item for item in data["cases"] if item["id"] == case_id)
        assert case["action"] == "passthrough"
        assert case["reason_code"] == "secret_detected"
        assert case["receipt_created"] is False


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


@pytest.mark.parametrize("seed", [1, True])
def test_manifest_seed_is_fixed_and_not_boolean(tmp_path, seed):
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["seed"] = seed
    data.pop("integrity_sha256", None)
    canonical = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    data["integrity_sha256"] = hashlib.sha256(canonical).hexdigest()
    altered = tmp_path / "manifest.json"
    altered.write_text(json.dumps(data), encoding="utf-8")
    completed = run_benchmark(manifest=altered, output=tmp_path / "result.json")
    assert completed.returncode != 0
    assert "seed" in (completed.stderr + completed.stdout).lower()


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


@pytest.mark.parametrize("field,value", [("version", "9.9.9"), ("seed", 1), ("manifest_sha256", "0" * 64), ("methodology", ""), ("environment", {}), ("reason_code", "bogus"), ("preservation", False), ("receipt_created", True)])
def test_schema_and_manifest_contract_reject_mutated_fields(field, value):
    from benchmarks.run import validate_result

    data = json.loads(RESULT.read_text(encoding="utf-8"))
    if field in {"version", "seed", "manifest_sha256", "methodology", "environment"}:
        data[field] = value
    else:
        target = 0 if field != "receipt_created" else next(i for i, item in enumerate(data["cases"]) if item["id"] == "secret_first")
        data["cases"][target][field] = value
    assert validate_result(data)


def test_mutated_protected_evidence_is_rejected():
    from benchmarks.run import validate_result

    data = json.loads(RESULT.read_text(encoding="utf-8"))
    data["cases"][0]["protected_fact_evidence"] = []
    assert validate_result(data)


@pytest.mark.parametrize("mutation", ["remove", "duplicate", "extra"])
def test_result_case_ids_must_exactly_match_manifest(mutation):
    from benchmarks.run import validate_result

    data = json.loads(RESULT.read_text(encoding="utf-8"))
    if mutation == "remove":
        data["cases"].pop()
    elif mutation == "duplicate":
        data["cases"][-1]["id"] = data["cases"][0]["id"]
    else:
        extra = dict(data["cases"][0])
        extra["id"] = "unexpected"
        data["cases"].append(extra)
    data["aggregate"]["cases"] = len(data["cases"])
    data["aggregate"]["estimated_tokens_before"] = sum(item["estimated_tokens_before"] for item in data["cases"])
    data["aggregate"]["estimated_tokens_after"] = sum(item["estimated_tokens_after"] for item in data["cases"])
    data["aggregate"]["estimated_tokens_saved"] = sum(item["estimated_tokens_saved"] for item in data["cases"])
    before = data["aggregate"]["estimated_tokens_before"]
    data["aggregate"]["reduction_ratio"] = round(data["aggregate"]["estimated_tokens_saved"] / before, 6) if before else 0.0
    assert validate_result(data)


def test_actual_output_missing_fact_fails_evidence_extraction():
    from benchmarks.run import _evidence

    case = {"id": "x", "protected_facts": ["critical\n"]}
    with pytest.raises(ValueError, match="protected fact missing"):
        _evidence(case, "rewritten\n")
