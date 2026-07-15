from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from release_common import (
    PACKAGE_NAME,
    REPOSITORY,
    ROOT,
    ReleaseError,
    atomic_write_json,
    check,
    clean_network_env,
    evidence,
    file_sha256,
    github_get,
    git_head,
    git_is_clean,
    git_output,
    load_evidence,
    read_version_sources,
    release_documents_match,
    run,
    validate_sha256,
    validate_version,
)
from release_verify_package import PackageVerificationError, inspect_archive, scan_member_name, safe_member_path

PREFLIGHT_EVIDENCE_CHECKS = {
    "version_sources", "mcp_gateway_version", "release_documents", "tls", "clean_worktree", "local_tag_absent",
    "remote_tag_absent", "npm_ping", "npm_whoami", "npm_owner", "github_repository_access",
    "github_feature_push_dry_run", "github_tag_push_dry_run",
}
PACKAGE_EVIDENCE_CHECKS = {
    "git_head", "package_json_source", "package_identity", "source_bytes", "clean_worktree",
    "artifact_name", "secret_scan", "global_help", "init", "persistent_install",
    "doctor_integration", "doctor_read_only", "uninstall", "reinstall", "doctor_after_reinstall",
    "npx_help", "npx_persistent_rejected", "npx_rejection_read_only",
}


def _npm_auth_checks(env: dict[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ping = run(["npm", "ping"], env=env, timeout=45)
    checks.append(check("npm_ping", ping.returncode == 0, "npm registry reachable"))
    whoami = run(["npm", "whoami"], env=env, timeout=45)
    identity = whoami.stdout.strip() if whoami.returncode == 0 else ""
    checks.append(check("npm_whoami", bool(identity), "npm authentication available"))
    owner = run(["npm", "owner", "ls", PACKAGE_NAME, "--json"], env=env, timeout=45)
    owner_ok = False
    if owner.returncode == 0 and identity:
        try:
            owners = json.loads(owner.stdout)
            if isinstance(owners, dict):
                owner_ok = identity in owners
            elif isinstance(owners, list):
                owner_ok = any(
                    item == identity or (isinstance(item, dict) and item.get("name") == identity)
                    for item in owners
                )
        except json.JSONDecodeError:
            owner_ok = any(
                line.split(maxsplit=1)[0] == identity
                for line in owner.stdout.splitlines()
                if line.strip()
            )
    checks.append(check("npm_owner", owner_ok, "npm package owner authorization"))
    return checks


def _github_auth_checks(root: Path, branch: str, version: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        repository = github_get(f"/repos/{REPOSITORY}", root=root)
        permissions = repository.get("permissions", {}) if isinstance(repository, dict) else {}
        write_ok = isinstance(permissions, dict) and any(permissions.get(key) is True for key in ("push", "maintain", "admin"))
        checks.append(check("github_repository_access", write_ok, "GitHub Contents/Release write permission"))
    except ReleaseError:
        checks.append(check("github_repository_access", False, "GitHub authentication or repository access failed"))
    feature = run(["git", "push", "--dry-run", "origin", f"HEAD:refs/heads/{branch}"], cwd=root, timeout=60)
    checks.append(check("github_feature_push_dry_run", feature.returncode == 0, "feature branch push dry-run"))
    tag = run(["git", "push", "--dry-run", "origin", f"HEAD:refs/tags/v{version}"], cwd=root, timeout=60)
    checks.append(check("github_tag_push_dry_run", tag.returncode == 0, "release tag push dry-run"))
    return checks


def _tag_absent(version: str, root: Path) -> list[dict[str, Any]]:
    local = run(["git", "tag", "--list", f"v{version}"], cwd=root)
    remote = run(["git", "ls-remote", "--exit-code", "--refs", "origin", f"refs/tags/v{version}"], cwd=root, timeout=45)
    return [
        check("local_tag_absent", local.returncode == 0 and not local.stdout.strip(), "release tag is not already present locally"),
        check("remote_tag_absent", remote.returncode == 2, "release tag is not already present remotely"),
    ]


def validate_publish_evidence(root: Path, version: str, sha: str) -> str:
    evidence_dir = root / ".tgl" / "release" / f"v{version}"
    artifact = evidence_dir / f"{PACKAGE_NAME}-{version}.tgz"
    if not artifact.is_file():
        raise ReleaseError("canonical release artifact is unavailable")
    preflight = load_evidence(evidence_dir / "preflight.json")
    package = load_evidence(evidence_dir / "package.json")

    def strict(value: dict[str, Any], expected: set[str], label: str) -> None:
        if value.get("schema_version") != 1 or value.get("ok") is not True or value.get("version") != version or value.get("git_sha") != sha:
            raise ReleaseError(f"{label} evidence does not match the frozen release")
        checks = value.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ReleaseError(f"{label} evidence checks are malformed")
        names = [item.get("name") for item in checks if isinstance(item, dict)]
        if len(names) != len(checks) or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise ReleaseError(f"{label} evidence checks are malformed")
        if not expected.issubset(set(names)) or any(item.get("ok") is not True for item in checks):
            raise ReleaseError(f"{label} evidence checks are incomplete")

    strict(preflight, PREFLIGHT_EVIDENCE_CHECKS, "preflight")
    strict(package, PACKAGE_EVIDENCE_CHECKS, "package")
    manifest = package.get("manifest")
    if not isinstance(manifest, list) or not manifest:
        raise ReleaseError("package evidence manifest is malformed")
    records: dict[str, tuple[int, str]] = {}
    for item in manifest:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ReleaseError("package evidence manifest is malformed")
        path = item["path"]
        if not isinstance(path, str) or path in records or scan_member_name("package/" + path):
            raise ReleaseError("package evidence manifest contains an unsafe path")
        try:
            safe_member_path("package/" + path)
            digest = validate_sha256(item["sha256"])
        except (ValueError, PackageVerificationError):
            raise ReleaseError("package evidence manifest contains an unsafe record")
        if not isinstance(item["size"], int) or item["size"] < 0:
            raise ReleaseError("package evidence manifest contains an unsafe record")
        records[path] = (item["size"], digest)
    actual = inspect_archive(artifact)
    actual_records = {path: (len(data), hashlib.sha256(data).hexdigest()) for path, data in actual.items()}
    if records != actual_records:
        raise ReleaseError("package evidence manifest does not match the canonical artifact")
    artifact_hash = file_sha256(artifact)
    if package.get("artifact_sha256") != artifact_hash:
        raise ReleaseError("package evidence artifact hash does not match the canonical artifact")
    return artifact_hash


def _publish_evidence_checks(version: str, sha: str, root: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        artifact_hash = validate_publish_evidence(root, version, sha)
        checks = [
            check("preflight_evidence", True, "preflight evidence matches current commit"),
            check("package_evidence", True, "package evidence matches current commit"),
            check("artifact_hash", True, "canonical artifact SHA-256 matches package evidence"),
            check("artifact_manifest", True, "canonical artifact manifest matches package evidence"),
        ]
    except ReleaseError:
        artifact_hash = "0" * 64
        checks = [
            check("preflight_evidence", False, "required release evidence is invalid"),
            check("package_evidence", False, "required release evidence is invalid"),
            check("artifact_hash", False, "canonical artifact is unavailable or drifted"),
            check("artifact_manifest", False, "canonical artifact manifest is unavailable or drifted"),
        ]
    return checks, artifact_hash


def run_check(version: str, phase: str, output: Path, *, root: Path = ROOT) -> int:
    validate_version(version)
    sha = "0" * 40
    checks: list[dict[str, Any]] = []
    try:
        sha = git_head(root)
    except ReleaseError:
        checks.append(check("git_head", False, "current commit could not be resolved"))
    try:
        versions = read_version_sources(root)
        checks.append(check("version_sources", set(versions.values()) == {version}, "all public version sources agree"))
    except ReleaseError:
        checks.append(check("version_sources", False, "version sources are unavailable or ambiguous"))
    try:
        gateway_source = (root / "src" / "token_governance" / "mcp_gateway.py").read_text(encoding="utf-8")
        gateway_ok = gateway_source.count("from . import __version__") == 1 and gateway_source.count('"version": __version__') == 2
    except (OSError, UnicodeError):
        gateway_ok = False
    checks.append(check("mcp_gateway_version", gateway_ok, "MCP Gateway derives both versions from package runtime"))
    checks.append(check("release_documents", release_documents_match(version, root), "CHANGELOG and release notes match target"))
    try:
        network_env = clean_network_env()
        tls_ok = True
    except ReleaseError:
        network_env = None
        tls_ok = False
    checks.append(check("tls", tls_ok, "TLS verification enabled"))
    checks.append(check("clean_worktree", git_is_clean(root), "worktree is clean"))
    checks.extend(_tag_absent(version, root))
    if tls_ok:
        checks.extend(_npm_auth_checks(network_env or {}))
        checks.extend(_github_auth_checks(root, git_output("branch", "--show-current", root=root), version))
    else:
        checks.extend(
            [
                check("npm_ping", False, "network checks skipped because TLS verification is disabled"),
                check("npm_whoami", False, "network checks skipped because TLS verification is disabled"),
                check("npm_owner", False, "network checks skipped because TLS verification is disabled"),
                check("github_repository_access", False, "network checks skipped because TLS verification is disabled"),
                check("github_feature_push_dry_run", False, "network checks skipped because TLS verification is disabled"),
                check("github_tag_push_dry_run", False, "network checks skipped because TLS verification is disabled"),
            ]
        )
    if phase == "publish":
        evidence_checks, artifact_hash = _publish_evidence_checks(version, sha, root)
        checks.extend(evidence_checks)
    else:
        artifact_hash = None
    result = evidence(
        version,
        sha,
        checks,
        **({"artifact_sha256": artifact_hash} if phase == "publish" else {}),
    )
    atomic_write_json(output, result)
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run release preflight or publish readiness checks.")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--phase", choices=("preflight", "publish"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run_check(args.expected_version, args.phase, args.output)
    except (ReleaseError, ValueError, OSError) as exc:
        try:
            version = args.expected_version if isinstance(args.expected_version, str) else "unknown"
            atomic_write_json(args.output, evidence(version, "0" * 40, [check("release_check", False, "release check could not complete")]))
        except OSError:
            pass
        print(str(exc), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
