from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "token-governance-layer"
REPOSITORY = "spacesky-cell/token-governance-layer"
SEMVER_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseError(RuntimeError):
    pass


def validate_version(value: str) -> str:
    if not SEMVER_RE.fullmatch(value):
        raise ValueError("version must be strict SemVer X.Y.Z")
    return value


def validate_sha(value: str) -> str:
    lowered = value.lower()
    if not SHA_RE.fullmatch(lowered):
        raise ValueError("expected a full 40-character Git SHA")
    return lowered


def validate_sha256(value: str) -> str:
    lowered = value.lower()
    if not SHA256_RE.fullmatch(lowered):
        raise ValueError("expected a 64-character SHA-256")
    return lowered


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    normalized = list(command)
    if normalized and normalized[0] in {"npm", "npx"}:
        normalized[0] = shutil.which(normalized[0]) or shutil.which(f"{normalized[0]}.cmd") or normalized[0]
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            normalized,
            cwd=cwd,
            env=None if env is None else dict(env),
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
            **process_options,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("required command was unavailable or timed out") from exc


def git_output(*args: str, root: Path = ROOT) -> str:
    result = run(["git", *args], cwd=root)
    if result.returncode != 0:
        raise ReleaseError("Git command failed")
    return result.stdout.strip()


def git_head(root: Path = ROOT) -> str:
    return validate_sha(git_output("rev-parse", "HEAD", root=root))


def git_is_clean(root: Path = ROOT) -> bool:
    result = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root)
    return result.returncode == 0 and not result.stdout.strip()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _single_match(pattern: bytes, data: bytes, label: str) -> str:
    matches = list(re.finditer(pattern, data, flags=re.MULTILINE))
    if len(matches) != 1:
        raise ReleaseError(f"{label} version anchor is missing or ambiguous")
    return matches[0].group(1).decode("ascii")


def read_version_sources(root: Path = ROOT) -> dict[str, str]:
    try:
        version_bytes = (root / "VERSION").read_bytes()
        package_bytes = (root / "package.json").read_bytes()
        pyproject_bytes = (root / "pyproject.toml").read_bytes()
        runtime_bytes = (root / "src" / "token_governance" / "__init__.py").read_bytes()
    except OSError as exc:
        raise ReleaseError("a required version source is unavailable") from exc

    version_text = version_bytes.decode("ascii").strip()
    if version_bytes.decode("ascii").replace("\r\n", "\n") != version_text + "\n":
        raise ReleaseError("VERSION must contain exactly one version line")
    values = {
        "VERSION": validate_version(version_text),
        "package_json": _single_match(rb'^\s*"version"\s*:\s*"([^"]+)"\s*,?\s*$', package_bytes, "package.json"),
        "pyproject": _single_match(rb'^version\s*=\s*"([^"]+)"\s*$', pyproject_bytes, "pyproject.toml"),
        "runtime": _single_match(rb'^__version__\s*=\s*"([^"]+)"\s*$', runtime_bytes, "runtime"),
    }
    for label, value in values.items():
        try:
            validate_version(value)
        except ValueError as exc:
            raise ReleaseError(f"{label} contains an invalid version") from exc
    return values


def release_documents_match(version: str, root: Path = ROOT) -> bool:
    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        notes = (root / "docs" / "releases" / f"v{version}.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    heading = f"## [{version}] - 2026-07-15"
    return (
        "## [Unreleased]" in changelog
        and heading in changelog
        and f"[{version}]: https://github.com/{REPOSITORY}/compare/v0.1.0...v{version}" in changelog
        and f"# Token Governance Layer v{version}" in notes
        and "DRAFT" in notes
    )


def tls_enabled(env: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if env is None else env
    return environment.get("NODE_TLS_REJECT_UNAUTHORIZED") != "0"


def check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"detail": detail, "name": name, "ok": bool(ok)}


def evidence(version: str, sha: str, checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "checks": checks,
        "git_sha": sha,
        "ok": all(item.get("ok") is True for item in checks),
        "schema_version": 1,
        "version": version,
        **extra,
    }


def load_evidence(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("required release evidence is unavailable or invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ReleaseError("required release evidence has the wrong schema")
    return value


def clean_network_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    if not tls_enabled(env):
        raise ReleaseError("TLS verification is disabled")
    env.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
    return env


def github_credentials(root: Path = ROOT) -> tuple[str, str]:
    result = run(
        ["git", "credential", "fill"],
        cwd=root,
        input_text="protocol=https\nhost=github.com\n\n",
        timeout=15,
    )
    if result.returncode != 0:
        raise ReleaseError("GitHub credentials are unavailable")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    username, password = fields.get("username"), fields.get("password")
    if not username or not password:
        raise ReleaseError("GitHub credentials are unavailable")
    return username, password


def github_get(path: str, *, root: Path = ROOT, accept: str = "application/vnd.github+json") -> Any:
    username, password = github_credentials(root)
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": accept,
            "Authorization": f"Basic {encoded}",
            "User-Agent": "token-governance-layer-release-verifier",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise ReleaseError("authenticated GitHub API request failed") from exc


def npm_json(args: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: int = 60) -> Any:
    result = run(["npm", *args, "--json"], env=env, timeout=timeout)
    if result.returncode != 0:
        raise ReleaseError("npm command failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError("npm returned invalid JSON") from exc
