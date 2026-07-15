from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from token_governance.contracts import CommandResult, GovernanceMode, GovernanceRequest, SourceKind
from token_governance.core import create_governance_engine, default_governance_config
from token_governance.ledger import ContextLedger

SEED = 20260715
SCHEMA_VERSION = "result-v1"
LABEL = "estimated_candidate_microbenchmark"


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def manifest_digest(manifest: dict[str, Any]) -> str:
    copy = dict(manifest)
    copy.pop("integrity_sha256", None)
    return hashlib.sha256(_canonical_json(copy)).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest could not be read: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("manifest version is unsupported")
    expected = manifest.get("integrity_sha256")
    if not isinstance(expected, str) or expected != manifest_digest(manifest):
        raise ValueError("manifest integrity check failed")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases or any(not isinstance(case, dict) for case in cases):
        raise ValueError("manifest cases are invalid")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(item, str) for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("manifest case ids must be unique strings")
    return manifest


def _request(case: dict[str, Any]) -> GovernanceRequest:
    source = SourceKind(case["source_kind"])
    mode = GovernanceMode(case["mode"])
    stdout = case.get("stdout", "")
    stderr = case.get("stderr", "")
    has_command_result = case.get("command_result", True)
    command_result = None
    if has_command_result:
        command_result = CommandResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=case.get("exit_code", 0),
            interrupted=bool(case.get("interrupted", False)),
        )
    raw_text = case.get("raw_text", stdout)
    payload_bytes = case.get("payload_bytes", len(raw_text.encode("utf-8")))
    tool_name = case.get("tool_name")
    command = case.get("command")
    tool_input = {} if command is None else {"command": command}
    return GovernanceRequest(
        source_kind=source,
        tool_name=tool_name,
        tool_input=tool_input,
        command_result=command_result,
        raw_text=raw_text,
        payload_bytes=payload_bytes,
        mode=mode,
    )


def _evidence(case: dict[str, Any]) -> list[dict[str, str]]:
    values = case.get("protected_facts", [])
    if not isinstance(values, list):
        raise ValueError(f"protected_facts for {case.get('id')} must be a list")
    return [
        {"text": value, "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
        for value in values
        if isinstance(value, str)
    ]


def run_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="tgl-benchmark-") as temp_dir:
        ledger = ContextLedger(Path(temp_dir) / "ledger.sqlite")
        engine = create_governance_engine(ledger, config=default_governance_config(ledger.path))
        for case in manifest["cases"]:
            request = _request(case)
            result = engine.govern_request(request)
            if result.receipt_id:
                ledger.mark_emitted(result.receipt_id)
            before = result.token_before
            after = result.token_after
            rows.append(
                {
                    "id": case["id"],
                    "action": result.action.value,
                    "risk": result.risk.value,
                    "strategy": result.strategy.value,
                    "reason_code": result.reason_code.value,
                    "preservation": bool(result.preservation_check and result.preservation_check.ok),
                    "estimated_tokens_before": before,
                    "estimated_tokens_after": after,
                    "estimated_tokens_saved": before - after,
                    "protected_fact_evidence": _evidence(case),
                }
            )
    total_before = sum(item["estimated_tokens_before"] for item in rows)
    total_after = sum(item["estimated_tokens_after"] for item in rows)
    return {
        "schema": SCHEMA_VERSION,
        "version": "0.2.0",
        "label": LABEL,
        "seed": SEED,
        "manifest_sha256": manifest_digest(manifest),
        "methodology": "Local GovernanceEngine microbenchmark with estimated tokenizer counts; no provider billing or task-quality claim.",
        "environment": {"contract": "portable", "runtime": "stdlib", "observed": {}},
        "cases": rows,
        "aggregate": {
            "cases": len(rows),
            "estimated_tokens_before": total_before,
            "estimated_tokens_after": total_after,
            "estimated_tokens_saved": total_before - total_after,
            "reduction_ratio": round((total_before - total_after) / total_before, 6) if total_before else 0.0,
        },
    }


def validate_result(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["result must be an object"]
    for key in ("schema", "version", "label", "seed", "manifest_sha256", "methodology", "environment", "cases", "aggregate"):
        if key not in data:
            errors.append(f"missing {key}")
    if data.get("schema") != SCHEMA_VERSION:
        errors.append("unsupported schema")
    if data.get("label") != LABEL:
        errors.append("invalid label")
    if not isinstance(data.get("cases"), list):
        errors.append("cases must be a list")
    else:
        required = {"id", "action", "risk", "strategy", "reason_code", "preservation", "estimated_tokens_before", "estimated_tokens_after", "estimated_tokens_saved", "protected_fact_evidence"}
        ids: list[str] = []
        before_total = after_total = saved_total = 0
        for index, case in enumerate(data["cases"]):
            if not isinstance(case, dict):
                errors.append(f"case {index} must be an object")
            else:
                for key in required:
                    if key not in case:
                        errors.append(f"case {index} missing {key}")
                case_id = case.get("id")
                if not isinstance(case_id, str):
                    errors.append(f"case {index} id must be a string")
                else:
                    ids.append(case_id)
                numeric = [case.get(name) for name in ("estimated_tokens_before", "estimated_tokens_after", "estimated_tokens_saved")]
                if not all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in numeric[:2]):
                    errors.append(f"case {index} token counts are invalid")
                elif numeric[2] != numeric[0] - numeric[1]:
                    errors.append(f"case {index} token savings are inconsistent")
                else:
                    before_total += numeric[0]
                    after_total += numeric[1]
                    saved_total += numeric[2]
                if case.get("action") not in {"passthrough", "transform"}:
                    errors.append(f"case {index} action is invalid")
                if case.get("risk") not in {"low", "medium", "high", "unavailable"}:
                    errors.append(f"case {index} risk is invalid")
                if case.get("strategy") not in {"passthrough", "repetitive_log", "test_output", "build_output"}:
                    errors.append(f"case {index} strategy is invalid")
                if not isinstance(case.get("preservation"), bool):
                    errors.append(f"case {index} preservation is invalid")
                evidence = case.get("protected_fact_evidence")
                if not isinstance(evidence, list):
                    errors.append(f"case {index} protected evidence is invalid")
                else:
                    for item in evidence:
                        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or item.get("sha256") != hashlib.sha256(item["text"].encode("utf-8")).hexdigest():
                            errors.append(f"case {index} protected evidence is invalid")
        if len(ids) != len(set(ids)):
            errors.append("case ids must be unique")
        aggregate = data.get("aggregate")
        if isinstance(aggregate, dict):
            if aggregate.get("cases") != len(data["cases"]):
                errors.append("aggregate case count is inconsistent")
            if aggregate.get("estimated_tokens_before") != before_total or aggregate.get("estimated_tokens_after") != after_total or aggregate.get("estimated_tokens_saved") != saved_total:
                errors.append("aggregate token counts are inconsistent")
            expected_ratio = round(saved_total / before_total, 6) if before_total else 0.0
            if aggregate.get("reduction_ratio") != expected_ratio:
                errors.append("aggregate reduction ratio is inconsistent")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_manifest(load_manifest(args.manifest))
        errors = validate_result(result)
        if errors:
            raise ValueError("result validation failed: " + "; ".join(errors))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(_canonical_json(result))
    except Exception as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
