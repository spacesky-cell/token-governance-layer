from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


START = "<!-- TGL-BENCHMARK:START -->"
END = "<!-- TGL-BENCHMARK:END -->"
SHA = "<!-- TGL-BENCHMARK:RESULT-SHA256"


def check_readme(result_path: Path, readme_path: Path) -> list[str]:
    errors: list[str] = []
    try:
        result = result_path.read_bytes()
        readme = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [str(exc)]
    if START not in readme or END not in readme or SHA not in readme:
        errors.append("README benchmark markers are missing")
    if readme.count(START) != 1 or readme.count(END) != 1 or readme.count(SHA) != 1:
        errors.append("README benchmark markers must occur exactly once")
    if errors:
        return errors
    digest = hashlib.sha256(result).hexdigest()
    block = readme[readme.index(START) : readme.index(END) + len(END)]
    if digest not in block:
        errors.append("README benchmark result SHA-256 is stale")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--readme", required=True, type=Path)
    args = parser.parse_args(argv)
    errors = check_readme(args.results, args.readme)
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
