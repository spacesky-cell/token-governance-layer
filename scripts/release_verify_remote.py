from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from urllib.parse import urlparse
import urllib.request
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
    run,
    validate_sha,
    validate_sha256,
    validate_version,
)


REQUIRED_TOPICS = {
    "claude-code",
    "claude-code-hook",
    "mcp",
    "token",
    "context-governance",
    "local-first",
}
OFFICIAL_REGISTRY = "https://registry.npmjs.org/"


def _append_failures_once(checks: list[dict[str, Any]], names: tuple[str, ...], detail: str) -> None:
    existing = {item.get("name") for item in checks}
    for name in names:
        if name not in existing:
            checks.append(check(name, False, detail))
            existing.add(name)


def _download(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org":
        raise ReleaseError("registry tarball URL is not the official HTTPS registry")
    request = urllib.request.Request(url, headers={"User-Agent": "token-governance-layer-release-verifier"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    too_large = int(length) > 50 * 1024 * 1024
                except ValueError as exc:
                    raise ReleaseError("registry artifact has an invalid size") from exc
                if too_large:
                    raise ReleaseError("registry artifact exceeds the download size limit")
            data = response.read(50 * 1024 * 1024 + 1)
            if len(data) > 50 * 1024 * 1024:
                raise ReleaseError("registry artifact exceeds the download size limit")
            return data
    except OSError as exc:
        raise ReleaseError("registry artifact download failed") from exc


def _isolated_npm_env(directory: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    env = clean_network_env(base or os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.startswith("NPM_CONFIG_") or upper in {"NPM_TOKEN", "NODE_AUTH_TOKEN"}:
            env.pop(key, None)
    userconfig = directory / "user.npmrc"
    globalconfig = directory / "global.npmrc"
    userconfig.write_text("", encoding="utf-8")
    globalconfig.write_text("", encoding="utf-8")
    env.update(
        {
            "NPM_CONFIG_USERCONFIG": str(userconfig),
            "NPM_CONFIG_GLOBALCONFIG": str(globalconfig),
            "NPM_CONFIG_CACHE": str(directory / "cache"),
            "NPM_CONFIG_STRICT_SSL": "true",
            "NPM_CONFIG_REGISTRY": OFFICIAL_REGISTRY,
        }
    )
    return env


def _registry_install(version: str, root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="tgl-registry-verify-") as directory:
        base = Path(directory)
        prefix = base / "prefix"
        env = _isolated_npm_env(base)
        for key in ("PYTHONPATH", "PYTHONHOME", "NODE_PATH"):
            env.pop(key, None)
        env.update(
            {
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
            }
        )
        install = run(
            ["npm", "install", "--global", "--prefix", str(prefix), f"{PACKAGE_NAME}@{version}", "--registry", OFFICIAL_REGISTRY, "--no-audit", "--no-fund"],
            cwd=root,
            env=env,
            timeout=180,
        )
        if install.returncode != 0:
            return False
        executable = prefix / "node_modules" / PACKAGE_NAME / "bin" / "tgl.js"
        command = ["node", str(executable), "--help"]
        result = run(command, cwd=root, env=env, timeout=90)
        return result.returncode == 0 and "claude-install" in result.stdout


def verify_remote(version: str, expected_sha: str, tarball_sha256: str, output: Path, *, root: Path = ROOT) -> int:
    validate_version(version)
    expected_sha = validate_sha(expected_sha)
    tarball_sha256 = validate_sha256(tarball_sha256)
    checks: list[dict[str, Any]] = []
    current = git_head(root)
    checks.append(check("local_head", current == expected_sha, "local commit matches frozen release SHA"))
    checks.append(check("clean_worktree", git_is_clean(root), "local worktree is clean"))
    try:
        clean_network_env()
        tls_ok = True
    except ReleaseError:
        tls_ok = False
    checks.append(check("tls", tls_ok, "TLS verification enabled"))
    if not tls_ok:
        checks.extend(
            check(name, False, "remote checks skipped because TLS verification is disabled")
            for name in (
                "github_topics",
                "github_main_ref",
                "github_tag_ref",
                "github_release",
                "npm_version",
                "npm_git_head",
                "npm_integrity",
                "npm_tarball_hash",
                "npm_latest",
                "npm_registry_install",
            )
        )
        result = evidence(version, expected_sha, checks, artifact_sha256=tarball_sha256)
        atomic_write_json(output, result)
        return 1
    try:
        repository = github_get(f"/repos/{REPOSITORY}", root=root)
        topics = set(repository.get("topics", [])) if isinstance(repository, dict) else set()
        checks.append(check("github_topics", REQUIRED_TOPICS.issubset(topics), "required repository topics are present"))
        main_ref = github_get(f"/repos/{REPOSITORY}/git/ref/heads/main", root=root)
        tag_ref = github_get(f"/repos/{REPOSITORY}/git/ref/tags/v{version}", root=root)
        main_sha = ((main_ref.get("object") or {}).get("sha") if isinstance(main_ref, dict) else None)
        tag_sha = ((tag_ref.get("object") or {}).get("sha") if isinstance(tag_ref, dict) else None)
        checks.append(check("github_main_ref", main_sha == expected_sha, "GitHub main ref matches frozen SHA"))
        checks.append(check("github_tag_ref", tag_sha == expected_sha, "GitHub release tag matches frozen SHA"))
        release = github_get(f"/repos/{REPOSITORY}/releases/tags/v{version}", root=root)
        checks.append(check("github_release", isinstance(release, dict) and release.get("tag_name") == f"v{version}" and release.get("draft") is False and release.get("prerelease") is False, "published non-draft GitHub Release exists"))
    except ReleaseError:
        _append_failures_once(
            checks,
            ("github_topics", "github_main_ref", "github_tag_ref", "github_release"),
            "GitHub remote verification failed",
        )
    npm_meta: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="tgl-registry-metadata-") as directory:
            npm_env = _isolated_npm_env(Path(directory))
            npm_result = run(["npm", "view", f"{PACKAGE_NAME}@{version}", "--json", "--registry", OFFICIAL_REGISTRY], env=npm_env, timeout=60)
        if npm_result.returncode != 0:
            raise ReleaseError("npm metadata unavailable")
        npm_meta = json.loads(npm_result.stdout)
        dist = npm_meta.get("dist", {}) if isinstance(npm_meta, dict) else {}
        checks.append(check("npm_version", npm_meta.get("version") == version, "npm package version matches"))
        checks.append(check("npm_git_head", npm_meta.get("gitHead") == expected_sha, "npm gitHead matches frozen SHA"))
        integrity = dist.get("integrity") if isinstance(dist, dict) else None
        tarball_url = dist.get("tarball") if isinstance(dist, dict) else None
        if not isinstance(tarball_url, str):
            raise ReleaseError("npm tarball metadata unavailable")
        downloaded = _download(tarball_url)
        actual_integrity = "sha512-" + base64.b64encode(hashlib.sha512(downloaded).digest()).decode("ascii")
        checks.append(check("npm_integrity", integrity == actual_integrity, "npm integrity matches registry tarball"))
        downloaded_hash = hashlib.sha256(downloaded).hexdigest()
        checks.append(check("npm_tarball_hash", downloaded_hash == tarball_sha256, "registry tarball SHA-256 matches recorded artifact"))
        latest = run(["npm", "view", PACKAGE_NAME, "dist-tags", "--json", "--registry", OFFICIAL_REGISTRY], env=npm_env, timeout=60)
        latest_value = json.loads(latest.stdout) if latest.returncode == 0 else {}
        checks.append(check("npm_latest", isinstance(latest_value, dict) and latest_value.get("latest") == version, "npm latest dist-tag matches release"))
        checks.append(check("npm_registry_install", _registry_install(version, root), "clean registry install and help smoke"))
    except (ReleaseError, json.JSONDecodeError):
        _append_failures_once(
            checks,
            ("npm_version", "npm_git_head", "npm_integrity", "npm_tarball_hash", "npm_latest", "npm_registry_install"),
            "npm remote verification failed",
        )
    result = evidence(version, expected_sha, checks, artifact_sha256=tarball_sha256)
    atomic_write_json(output, result)
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify published GitHub and npm release metadata.")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--tarball-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return verify_remote(args.expected_version, args.expected_sha, args.tarball_sha256, args.output)
    except (ReleaseError, ValueError, OSError) as exc:
        try:
            atomic_write_json(args.output, evidence(args.expected_version, "0" * 40, [check("remote_verification", False, "remote verification could not complete")], artifact_sha256=args.tarball_sha256 if len(args.tarball_sha256) == 64 else "0" * 64))
        except OSError:
            pass
        print(str(exc), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
