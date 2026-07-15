from __future__ import annotations

import argparse
import gzip
import json
import os
import tarfile
import tempfile
from pathlib import Path

from release_common import (
    PACKAGE_NAME,
    ROOT,
    ReleaseError,
    file_sha256,
    git_head,
    git_is_clean,
    read_version_sources,
    run,
    validate_sha,
)
from release_verify_package import PackageVerificationError, inspect_archive


def _atomic_link_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if target.exists():
        raise FileExistsError(target)
    try:
        with temporary.open("w+b") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _deterministic_tarball(files: dict[str, bytes]) -> bytes:
    output = bytearray()
    with tempfile.TemporaryFile() as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0, filename="") as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative in sorted(files):
                    data = files[relative]
                    info = tarfile.TarInfo(f"package/{relative}")
                    info.size = len(data)
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, __import__("io").BytesIO(data))
        handle.seek(0)
        output.extend(handle.read())
    return bytes(output)


def build_package(
    root: Path,
    output: Path,
    *,
    git_sha: str | None = None,
    npm_pack_source: Path | None = None,
) -> str:
    sources = read_version_sources(root)
    version = sources["VERSION"]
    expected_name = f"{PACKAGE_NAME}-{version}.tgz"
    if output.name != expected_name:
        raise ReleaseError("output filename must be the exact versioned package name")
    if output.exists():
        raise FileExistsError(output)
    if git_sha is None:
        if not git_is_clean(root):
            raise ReleaseError("worktree must be clean before building the final artifact")
        git_sha = git_head(root)
    else:
        git_sha = validate_sha(git_sha)
    source = npm_pack_source or root
    with tempfile.TemporaryDirectory(prefix="tgl-build-package ") as directory:
        temporary = Path(directory)
        result = run(
            ["npm", "pack", "--pack-destination", str(temporary), "--json"],
            cwd=source,
            env=dict(os.environ),
            timeout=180,
        )
        if result.returncode != 0:
            raise ReleaseError("npm pack failed")
        packed = temporary / expected_name
        if not packed.is_file():
            raise ReleaseError("npm pack did not create the expected package")
        files = inspect_archive(packed)
    package_data = files.get("package.json")
    if package_data is None:
        raise PackageVerificationError("package.json is missing from npm pack output")
    package = json.loads(package_data)
    if not isinstance(package, dict) or package.get("name") != PACKAGE_NAME or package.get("version") != version:
        raise PackageVerificationError("npm pack package identity drifted")
    package["gitHead"] = git_sha
    files["package.json"] = (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    content = _deterministic_tarball(files)
    _atomic_link_write(output, content)
    return file_sha256(output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the one-time deterministic release artifact.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        build_package(ROOT, args.output)
    except (ReleaseError, OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        print(str(exc), file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
