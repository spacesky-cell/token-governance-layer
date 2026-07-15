from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

from release_common import ROOT, read_version_sources, validate_version


class VersionUpdateError(RuntimeError):
    pass


def _replace_one(data: bytes, pattern: bytes, replacement: bytes, label: str) -> bytes:
    updated, count = re.subn(pattern, replacement, data, count=2, flags=re.MULTILINE)
    if count != 1:
        raise VersionUpdateError(f"{label} version anchor is missing or ambiguous")
    return updated


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_version(root: Path, version: str) -> None:
    try:
        validate_version(version)
    except ValueError as exc:
        raise VersionUpdateError(str(exc)) from exc
    paths = {
        "VERSION": root / "VERSION",
        "package.json": root / "package.json",
        "pyproject.toml": root / "pyproject.toml",
        "runtime": root / "src" / "token_governance" / "__init__.py",
    }
    try:
        original = {label: path.read_bytes() for label, path in paths.items()}
    except OSError as exc:
        raise VersionUpdateError("a required version source is unavailable") from exc

    version_newline = b"\r\n" if b"\r\n" in original["VERSION"] else b"\n"
    if not re.fullmatch(rb"[^\r\n]+(?:\r\n|\n)", original["VERSION"]):
        raise VersionUpdateError("VERSION must contain exactly one version line")
    updated = {
        "VERSION": version.encode("ascii") + version_newline,
        "package.json": _replace_one(
            original["package.json"],
            rb'(^\s*"version"\s*:\s*")[^"]+("\s*,?\s*$)',
            rb"\g<1>" + version.encode("ascii") + rb"\g<2>",
            "package.json",
        ),
        "pyproject.toml": _replace_one(
            original["pyproject.toml"],
            rb'(^version\s*=\s*")[^"]+("\s*$)',
            rb"\g<1>" + version.encode("ascii") + rb"\g<2>",
            "pyproject.toml",
        ),
        "runtime": _replace_one(
            original["runtime"],
            rb'(^__version__\s*=\s*")[^"]+("\s*$)',
            rb"\g<1>" + version.encode("ascii") + rb"\g<2>",
            "runtime",
        ),
    }
    for label, path in paths.items():
        if updated[label] != original[label]:
            _atomic_write(path, updated[label])
    values = read_version_sources(root)
    if set(values.values()) != {version}:
        raise VersionUpdateError("version sources did not converge")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set every public package version.")
    parser.add_argument("version")
    args = parser.parse_args()
    try:
        set_version(ROOT, args.version)
    except (VersionUpdateError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
