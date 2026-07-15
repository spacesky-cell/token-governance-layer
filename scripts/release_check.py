from __future__ import annotations

import argparse
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


def _publish_evidence_checks(version: str, sha: str, root: Path) -> list[dict[str, Any]]:
    evidence_dir = root / ".tgl" / "release" / f"v{version}"
    checks: list[dict[str, Any]] = []
    try:
        preflight = load_evidence(evidence_dir / "preflight.json")
        package = load_evidence(evidence_dir / "package.json")
        checks.append(check("preflight_evidence", preflight.get("ok") is True and preflight.get("version") == version and preflight.get("git_sha") == sha, "preflight evidence matches current commit"))
        artifact_hash = package.get("artifact_sha256")
        checks.append(check("package_evidence", package.get("ok") is True and package.get("version") == version and package.get("git_sha") == sha and isinstance(artifact_hash, str), "package evidence matches current commit"))
        try:
            hash_ok = isinstance(artifact_hash, str) and validate_sha256(artifact_hash) == artifact_hash
        except ValueError:
            hash_ok = False
        checks.append(check("artifact_hash", hash_ok, "recorded artifact SHA-256 is valid"))
    except ReleaseError:
        checks.append(check("preflight_evidence", False, "required preflight/package evidence is unavailable"))
        checks.append(check("package_evidence", False, "required preflight/package evidence is unavailable"))
    return checks


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
    checks.append(check("release_documents", release_documents_match(version, root), "CHANGELOG and release notes match target"))
    tls_ok = os.environ.get("NODE_TLS_REJECT_UNAUTHORIZED") != "0"
    checks.append(check("tls", tls_ok, "TLS verification enabled"))
    checks.append(check("clean_worktree", git_is_clean(root), "worktree is clean"))
    checks.extend(_tag_absent(version, root))
    if tls_ok:
        env = clean_network_env()
        checks.extend(_npm_auth_checks(env))
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
        checks.extend(_publish_evidence_checks(version, sha, root))
    result = evidence(version, sha, checks)
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
