from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
PUBLIC_FILES = (
    *READMES,
    ROOT / "CHANGELOG.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "docs/security.md",
    ROOT / "docs/benchmark.md",
    ROOT / "docs/gateway.md",
)
REQUIRED_README_TEXT = (
    "Experimental",
    "npm install -g token-governance-layer",
    "tgl init --project .",
    "tgl claude-install --project .",
    "tgl doctor --project . --integration",
    "tgl claude-install --project . --repair",
    "tgl claude-uninstall --project .",
    "<!-- TGL-BENCHMARK:START -->",
    "<!-- TGL-BENCHMARK:END -->",
)
FORBIDDEN_README_TEXT = (
    "89.19%",
    "npx token-governance-layer claude-install",
)
PRIVACY_PATTERNS = {
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "npm token": re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    "npm auth config": re.compile(r"registry\.npmjs\.org/\s*:\s*_authToken", re.I),
    "Windows personal path": re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+[\\/]"),
    "POSIX personal path": re.compile(r"/(?:Users|home)/[^/\s]+/"),
    "personal email": re.compile(
        r"\b[A-Z0-9._%+-]+@(?!users\.noreply\.github\.com\b)"
        r"[A-Z0-9.-]+\.[A-Z]{2,}\b",
        re.I,
    ),
}
LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _tracked_text() -> list[tuple[Path, str]]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    values: list[tuple[Path, str]] = []
    for raw_name in completed.stdout.split(b"\0"):
        if not raw_name:
            continue
        path = ROOT / raw_name.decode("utf-8")
        try:
            values.append((path, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError):
            continue
    return values


def _check_readmes(errors: list[str]) -> None:
    for path in READMES:
        text = path.read_text(encoding="utf-8")
        for expected in REQUIRED_README_TEXT:
            if expected not in text:
                errors.append(f"{path.name}: missing required text: {expected}")
        for forbidden in FORBIDDEN_README_TEXT:
            if forbidden in text:
                errors.append(f"{path.name}: forbidden legacy claim/command: {forbidden}")


def _check_links(errors: list[str]) -> None:
    for path in PUBLIC_FILES:
        text = path.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith(("#", "https://", "http://", "mailto:")):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            if not (path.parent / relative).resolve().exists():
                errors.append(f"{path.relative_to(ROOT)}: missing relative link target: {target}")


def _check_privacy(errors: list[str]) -> None:
    for path, text in _tracked_text():
        for label, pattern in PRIVACY_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{path.relative_to(ROOT)}: possible {label}")


def _check_workflows(errors: list[str]) -> None:
    try:
        import yaml
    except ImportError:
        errors.append("PyYAML is required to validate workflow syntax")
        return
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: invalid YAML: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{path.relative_to(ROOT)}: workflow root must be a mapping")


def _check_metadata(errors: list[str]) -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    expected_root = "https://github.com/spacesky-cell/token-governance-layer"
    expected_package_docs = {
        "README.md",
        "README.zh-CN.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/security.md",
        "docs/benchmark.md",
        "docs/gateway.md",
    }
    package_files = package.get("files", [])
    if not isinstance(package_files, list) or len(package_files) != len(set(package_files)):
        errors.append("package.json: files must be a unique array")
        package_files = []
    if not expected_package_docs.issubset(set(package_files)):
        errors.append("package.json: public documentation is missing from files")
    if package.get("engines", {}).get("node") != ">=22 <25":
        errors.append("package.json: engines.node must be >=22 <25")
    if package.get("homepage") != expected_root + "#readme":
        errors.append("package.json: homepage is not the public repository")
    if package.get("bugs", {}).get("url") != expected_root + "/issues":
        errors.append("package.json: bugs URL is not the public issue tracker")
    if package.get("repository", {}).get("url") != "git+" + expected_root + ".git":
        errors.append("package.json: repository URL is not the public repository")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'requires-python = ">=3.10,<3.15"' not in pyproject:
        errors.append("pyproject.toml: requires-python must match Python 3.10-3.14")
    for version in range(10, 15):
        if f'"Programming Language :: Python :: 3.{version}"' not in pyproject:
            errors.append(f"pyproject.toml: missing Python 3.{version} classifier")
    if expected_root not in pyproject:
        errors.append("pyproject.toml: public repository URLs are missing")


def main() -> int:
    errors: list[str] = []
    _check_readmes(errors)
    _check_links(errors)
    _check_privacy(errors)
    _check_workflows(errors)
    _check_metadata(errors)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("Public documentation, links, privacy, workflows, and metadata are consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
