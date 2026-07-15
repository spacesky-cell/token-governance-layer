from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from release_common import (
    PACKAGE_NAME,
    ROOT,
    ReleaseError,
    atomic_write_json,
    check,
    clean_network_env,
    evidence,
    file_sha256,
    git_head,
    git_is_clean,
    read_version_sources,
)


class PackageVerificationError(ReleaseError):
    pass


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private_key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("authorization_header", re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("npm_token", re.compile(rb"\bnpm_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("password_assignment", re.compile(rb"(?i)\b(?:password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}")),
    ("personal_windows_path", re.compile(rb"(?i)\b[A-Z]:[\\/]Users[\\/]")),
    ("personal_unix_path", re.compile(rb"(?i)/(?:Users|home)/[A-Za-z0-9._~-]+/")),
    ("email_address", re.compile(rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)

SENSITIVE_FILENAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "private.key",
    "private.pem",
    "id_rsa",
}


def scan_secret(data: bytes) -> str | None:
    data = data[:1024 * 1024]
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(data):
            return name
    return None


def scan_member_name(name: str) -> str | None:
    lowered = PurePosixPath(name).name.lower()
    if lowered in SENSITIVE_FILENAMES or lowered.startswith(".env"):
        return "sensitive_filename"
    return None


def safe_member_path(name: str) -> Path:
    if "\\" in name:
        raise PackageVerificationError("package member uses an unsafe separator")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or path.parts[0] != "package":
        raise PackageVerificationError("package member is outside package root")
    relative = path.relative_to("package")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PackageVerificationError("package member contains traversal")
    return Path(*relative.parts)


def inspect_archive(tarball: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(tarball, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > 1000:
                raise PackageVerificationError("package contains too many members")
            seen: set[str] = set()
            total_size = 0
            for member in members:
                if member.name == "package":
                    continue
                relative = safe_member_path(member.name)
                key = relative.as_posix()
                if key in seen:
                    raise PackageVerificationError("package contains duplicate members")
                seen.add(key)
                finding = scan_member_name(member.name)
                if finding:
                    raise PackageVerificationError("package contains sensitive filenames")
                if member.isdir():
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise PackageVerificationError("package contains a non-regular member")
                if member.size > 10 * 1024 * 1024:
                    raise PackageVerificationError("package member exceeds the size limit")
                total_size += member.size
                if total_size > 50 * 1024 * 1024:
                    raise PackageVerificationError("package contents exceed the size limit")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise PackageVerificationError("package member could not be read")
                data = extracted.read()
                finding = scan_secret(data)
                if finding:
                    raise PackageVerificationError("package contains secret-like content")
                files[key] = data
    except (OSError, tarfile.TarError) as exc:
        raise PackageVerificationError("tarball could not be safely inspected") from exc
    return files


def validate_expected_files(actual: set[str], expected: set[str]) -> None:
    if actual != expected:
        raise PackageVerificationError("package file set differs from expected allowlist")


def _package_files(manifest: dict[str, Any], root: Path) -> set[str]:
    files: set[str] = {"package.json"}
    configured = manifest.get("files", [])
    if not isinstance(configured, list) or not all(isinstance(item, str) for item in configured):
        raise PackageVerificationError("package files allowlist is invalid")
    for pattern in configured:
        if pattern.endswith("/*.py"):
            files.update(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
        elif pattern.endswith("/**"):
            files.update(path.relative_to(root).as_posix() for path in root.glob(pattern[:-3] + "**/*") if path.is_file())
        elif (root / pattern).is_dir():
            files.update(path.relative_to(root).as_posix() for path in (root / pattern).rglob("*") if path.is_file())
        elif (root / pattern).is_file():
            files.add(pattern.replace("\\", "/"))
    # npm always includes these metadata files when present.
    for name in ("README", "README.md", "README.txt", "LICENSE", "LICENSE.md", "LICENCE", "CHANGELOG.md"):
        if (root / name).is_file():
            files.add(name)
    return files


def _run_npm(command: list[str], *, env: dict[str, str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    normalized = list(command)
    if normalized and normalized[0] == "npm":
        normalized[0] = shutil.which("npm") or shutil.which("npm.cmd") or normalized[0]
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            normalized,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **process_options,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageVerificationError("npm lifecycle command was unavailable or timed out") from exc


def _run_tgl(executable: Path, args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = [str(executable), *args]
    if executable.suffix.lower() == ".js":
        command = [shutil.which("node") or "node", str(executable), *args]
    elif os.name == "nt" and executable.suffix.lower() in {".cmd", ".bat"}:
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "call " + subprocess.list2cmdline(command)]
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
            **process_options,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageVerificationError("installed CLI command was unavailable or timed out") from exc


def _project_snapshot(path: Path) -> dict[str, bytes]:
    if not path.exists():
        return {}
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def _run_install_matrix(tarball: Path, root: Path, version: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="tgl npm prefix 验证 ") as directory:
        base = Path(directory)
        prefix = base / "prefix with spaces"
        cache = base / "cache"
        userconfig = base / "empty-npmrc"
        userconfig.write_text("", encoding="utf-8")
        env = clean_network_env()
        for key in ("PYTHONPATH", "PYTHONHOME", "NODE_PATH"):
            env.pop(key, None)
        env.update(
            {
                "NPM_CONFIG_USERCONFIG": str(userconfig),
                "NPM_CONFIG_CACHE": str(cache),
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
            }
        )
        installed = _run_npm(
            ["npm", "install", "--global", "--prefix", str(prefix), str(tarball), "--ignore-scripts", "--no-audit", "--no-fund"],
            env=env,
        )
        if installed.returncode != 0:
            raise PackageVerificationError("global installation failed")
        bin_dir = prefix if os.name == "nt" else prefix / "bin"
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        tgl = prefix / "node_modules" / PACKAGE_NAME / "bin" / "tgl.js"
        if not tgl.exists():
            raise PackageVerificationError("global install did not expose tgl")
        checks: list[dict[str, Any]] = []
        help_result = _run_tgl(tgl, ["--help"], env=env)
        checks.append(check("global_help", help_result.returncode == 0 and "claude-install" in help_result.stdout, "global tgl help"))
        project = base / "项目 with spaces"
        init = _run_tgl(tgl, ["init", "--project", str(project)], env=env)
        checks.append(check("init", init.returncode == 0, "project init"))
        install = _run_tgl(tgl, ["claude-install", "--project", str(project)], env=env)
        checks.append(check("persistent_install", install.returncode == 0, "persistent Claude install"))
        before_doctor = _project_snapshot(project)
        doctor = _run_tgl(tgl, ["doctor", "--project", str(project), "--integration"], env=env)
        doctor_json: dict[str, Any] = {}
        try:
            doctor_json = json.loads(doctor.stdout)
        except json.JSONDecodeError:
            pass
        checks.append(check("doctor_integration", doctor.returncode == 0 and doctor_json.get("ok") is True, "read-only Hook/MCP doctor"))
        checks.append(check("doctor_read_only", before_doctor == _project_snapshot(project), "doctor did not mutate project"))
        uninstall = _run_tgl(tgl, ["claude-uninstall", "--project", str(project)], env=env)
        checks.append(check("uninstall", uninstall.returncode == 0, "owned installation uninstall"))
        reinstall = _run_tgl(tgl, ["claude-install", "--project", str(project)], env=env)
        checks.append(check("reinstall", reinstall.returncode == 0, "persistent reinstall"))
        doctor_again = _run_tgl(tgl, ["doctor", "--project", str(project), "--integration"], env=env)
        checks.append(check("doctor_after_reinstall", doctor_again.returncode == 0, "doctor after reinstall"))
        npx_project = base / "npx target"
        npx_project_before = _project_snapshot(npx_project)
        npx_help = _run_npm(
            ["npm", "exec", "--yes", "--package", str(tarball), "--", "tgl", "--help"],
            env=env,
        )
        checks.append(check("npx_help", npx_help.returncode == 0 and "claude-install" in npx_help.stdout, "npx one-shot help"))
        npx_install = _run_npm(
            ["npm", "exec", "--yes", "--package", str(tarball), "--", "tgl", "claude-install", "--project", str(npx_project)],
            env=env,
        )
        checks.append(check("npx_persistent_rejected", npx_install.returncode != 0, "npx persistent install rejected"))
        checks.append(check("npx_rejection_read_only", npx_project_before == _project_snapshot(npx_project), "npx rejection did not create project"))
        return {"checks": checks, "prefix": {"installed": True}}


def verify_package(tarball: Path, output: Path, *, root: Path = ROOT) -> int:
    version_sources = read_version_sources(root)
    version = version_sources["VERSION"]
    expected_name = f"{PACKAGE_NAME}-{version}.tgz"
    checks: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    tarball_hash = file_sha256(tarball)
    if tarball.stat().st_size > 50 * 1024 * 1024:
        raise PackageVerificationError("tarball exceeds the verification size limit")
    files = inspect_archive(tarball)
    manifest = [
        {"path": key, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for key, data in files.items()
    ]
    package_data = files.get("package.json")
    if package_data is None:
        raise PackageVerificationError("package.json is missing")
    try:
        package_json = json.loads(package_data)
    except json.JSONDecodeError as exc:
        raise PackageVerificationError("package.json is invalid") from exc
    if not isinstance(package_json, dict) or package_json.get("name") != PACKAGE_NAME or package_json.get("version") != version:
        raise PackageVerificationError("package identity or version drifted")
    frozen_sha = git_head(root)
    git_head_ok = package_json.get("gitHead") == frozen_sha
    checks.append(check("git_head", git_head_ok, "package gitHead matches frozen commit"))
    try:
        source_package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageVerificationError("source package.json is unavailable") from exc
    package_without_head = dict(package_json)
    package_without_head.pop("gitHead", None)
    checks.append(check("package_json_source", package_without_head == source_package, "package.json matches source apart from gitHead"))
    expected = _package_files(package_json, root)
    actual = set(files)
    try:
        validate_expected_files(actual, expected)
        file_set_ok = True
    except PackageVerificationError:
        file_set_ok = False
    checks.append(check("package_identity", file_set_ok, "package identity and exact allowlist"))
    for name, data in files.items():
        source = root / name
        if name == "package.json":
            continue
        if source.is_file() and source.read_bytes() != data:
            raise PackageVerificationError("packed file differs from the frozen source")
    checks.append(check("source_bytes", True, "packed files match source bytes"))
    checks.append(check("clean_worktree", git_is_clean(root), "artifact is verified from a clean worktree"))
    checks.append(check("artifact_name", tarball.name == expected_name, "versioned tarball name"))
    checks.append(check("secret_scan", True, "secret-like content scan"))
    try:
        lifecycle = _run_install_matrix(tarball.resolve(), root, version)
        checks.extend(lifecycle["checks"])
    except (PackageVerificationError, ReleaseError):
        checks.append(check("install_matrix", False, "packed artifact install matrix failed"))
    result = evidence(
        version,
        frozen_sha,
        checks,
        artifact_sha256=tarball_hash,
        manifest=sorted(manifest, key=lambda item: item["path"]),
    )
    atomic_write_json(output, result)
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a packed npm artifact safely.")
    parser.add_argument("--tarball", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return verify_package(args.tarball, args.output)
    except (PackageVerificationError, ReleaseError, OSError) as exc:
        try:
            sha = file_sha256(args.tarball) if args.tarball.is_file() else "0" * 64
            version = args.tarball.stem.rsplit("-", 1)[-1]
            atomic_write_json(args.output, evidence(version, "0" * 40, [check("verification", False, "package verification failed")], artifact_sha256=sha))
        except OSError:
            pass
        print(str(exc), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
