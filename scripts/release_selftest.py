from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from release_common import atomic_write_json, read_version_sources, validate_version
from release_set_version import VersionUpdateError, set_version
from release_verify_package import PackageVerificationError, scan_secret, safe_member_path


class ReleaseCommonTests(unittest.TestCase):
    def test_semver_is_strict(self) -> None:
        self.assertEqual(validate_version("0.2.0"), "0.2.0")
        for value in ("v0.2.0", "01.2.0", "0.2", "0.2.0-rc.1", " 0.2.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_version(value)

    def test_atomic_json_is_stable_and_creates_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "evidence.json"
            atomic_write_json(target, {"z": 1, "a": True})
            self.assertEqual(target.read_bytes(), b'{\n  "a": true,\n  "z": 1\n}\n')


class VersionUpdateTests(unittest.TestCase):
    def _project(self, root: Path, *, ambiguous: bool = False) -> None:
        (root / "src" / "token_governance").mkdir(parents=True)
        (root / "VERSION").write_bytes(b"0.1.0\r\n")
        (root / "package.json").write_bytes(
            b'{\r\n  "name": "token-governance-layer",\r\n  "version": "0.1.0"\r\n}\r\n'
        )
        pyproject = b'[project]\r\nname = "token-governance-layer"\r\nversion = "0.1.0"\r\n'
        if ambiguous:
            pyproject += b'version = "9.9.9"\r\n'
        (root / "pyproject.toml").write_bytes(pyproject)
        (root / "src" / "token_governance" / "__init__.py").write_bytes(
            b'__version__ = "0.1.0"\r\n'
        )

    def test_set_version_is_idempotent_and_preserves_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            set_version(root, "0.2.0")
            first = {
                path: (root / path).read_bytes()
                for path in (
                    "VERSION",
                    "package.json",
                    "pyproject.toml",
                    "src/token_governance/__init__.py",
                )
            }
            set_version(root, "0.2.0")
            second = {path: (root / path).read_bytes() for path in first}
            self.assertEqual(first, second)
            self.assertTrue(all(b"\r\n" in data for data in first.values()))
            self.assertEqual(set(read_version_sources(root).values()), {"0.2.0"})

    def test_set_version_rejects_ambiguous_anchor_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root, ambiguous=True)
            before = (root / "pyproject.toml").read_bytes()
            with self.assertRaises(VersionUpdateError):
                set_version(root, "0.2.0")
            self.assertEqual((root / "pyproject.toml").read_bytes(), before)
            self.assertEqual((root / "VERSION").read_text().strip(), "0.1.0")


class PackageSafetyTests(unittest.TestCase):
    def test_tar_paths_reject_traversal_and_wrong_root(self) -> None:
        self.assertEqual(safe_member_path("package/src/token_governance/cli.py"), Path("src/token_governance/cli.py"))
        for name in ("../secret", "package/../../secret", "/absolute", "other/file"):
            with self.subTest(name=name), self.assertRaises(PackageVerificationError):
                safe_member_path(name)

    def test_secret_scan_detects_values_without_returning_them(self) -> None:
        secret = "ghp_" + "a" * 36
        finding = scan_secret(("prefix " + secret).encode())
        self.assertEqual(finding, "github_token")
        self.assertNotIn(secret, finding)
        self.assertIsNone(scan_secret(b"https://github.com/spacesky-cell/token-governance-layer"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
