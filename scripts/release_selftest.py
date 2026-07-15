from __future__ import annotations

import tempfile
import tarfile
import json
import hashlib
import subprocess
import unittest
from pathlib import Path

from release_common import (
    atomic_write_json,
    read_version_sources,
    tls_disabled_reasons,
    validate_version,
)
from release_build_package import build_package
from release_check import validate_publish_evidence
from release_set_version import VersionUpdateError, set_version
from release_verify_package import (
    PackageVerificationError,
    scan_member_name,
    scan_secret,
    safe_member_path,
    validate_expected_files,
)


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

    def test_privacy_scans_paths_emails_and_sensitive_names_without_values(self) -> None:
        values = (
            b"C:" + b"\\Users\\alice\\private.txt",
            b"/" + b"home/alice/.config",
            b"owner" + b"@example.com",
        )
        for value in values:
            finding = scan_secret(value)
            self.assertIsNotNone(finding)
            self.assertNotIn(value.decode(), finding or "")
        self.assertEqual(scan_member_name("package/.env"), "sensitive_filename")
        self.assertEqual(scan_member_name("package/.npmrc"), "sensitive_filename")

    def test_tls_disable_paths_are_all_rejected(self) -> None:
        cases = (
            ({"NODE_TLS_REJECT_UNAUTHORIZED": "0"}, "node_tls_disabled"),
            ({"GIT_SSL_NO_VERIFY": "true"}, "git_ssl_disabled"),
            ({"NPM_CONFIG_STRICT_SSL": "false"}, "npm_strict_ssl_disabled"),
        )
        for environment, expected in cases:
            self.assertIn(expected, tls_disabled_reasons(environment))
        self.assertIn("npm_strict_ssl_disabled", tls_disabled_reasons({}, npm_config="strict-ssl=false"))
        self.assertIn("git_ssl_disabled", tls_disabled_reasons({}, git_config="http.sslVerify=false"))

    def test_missing_expected_package_member_is_rejected(self) -> None:
        with self.assertRaises(PackageVerificationError):
            validate_expected_files({"package.json"}, {"package.json", "README.md"})


class ReleaseEvidenceTests(unittest.TestCase):
    def _publish_fixture(self, root: Path, *, artifact_bytes: bytes = b"{}\n", evidence_hash: str | None = None) -> Path:
        release = root / ".tgl" / "release" / "v0.2.0"
        release.mkdir(parents=True)
        artifact = release / "token-governance-layer-0.2.0.tgz"
        with tarfile.open(artifact, "w:gz") as archive:
            info = tarfile.TarInfo("package/package.json")
            info.size = len(artifact_bytes)
            archive.addfile(info, __import__("io").BytesIO(artifact_bytes))
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        manifest_digest = hashlib.sha256(artifact_bytes).hexdigest()
        checks_pre = [{"name": name, "ok": True, "detail": "ok"} for name in __import__("release_check").PREFLIGHT_EVIDENCE_CHECKS]
        checks_pkg = [{"name": name, "ok": True, "detail": "ok"} for name in __import__("release_check").PACKAGE_EVIDENCE_CHECKS]
        preflight = {"schema_version": 1, "ok": True, "version": "0.2.0", "git_sha": "a" * 40, "checks": checks_pre}
        package = {"schema_version": 1, "ok": True, "version": "0.2.0", "git_sha": "a" * 40, "artifact_sha256": evidence_hash or digest, "checks": checks_pkg, "manifest": [{"path": "package.json", "size": len(artifact_bytes), "sha256": manifest_digest}]}
        (release / "preflight.json").write_text(json.dumps(preflight), encoding="utf-8")
        (release / "package.json").write_text(json.dumps(package), encoding="utf-8")
        return artifact

    def test_publish_evidence_requires_canonical_artifact_and_strict_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / ".tgl" / "release" / "v0.2.0"
            release.mkdir(parents=True)
            preflight = {"schema_version": 1, "ok": True, "version": "0.2.0", "git_sha": "a" * 40, "checks": [{"name": "tls", "ok": True, "detail": "ok"}]}
            package = {"schema_version": 1, "ok": True, "version": "0.2.0", "git_sha": "a" * 40, "artifact_sha256": "b" * 64, "checks": [{"name": "git_head", "ok": True, "detail": "ok"}], "manifest": [{"path": "package.json", "size": 2, "sha256": "c" * 64}]}
            (release / "preflight.json").write_text(json.dumps(preflight), encoding="utf-8")
            (release / "package.json").write_text(json.dumps(package), encoding="utf-8")
            with self.assertRaises(Exception):
                validate_publish_evidence(root, "0.2.0", "a" * 40)

    def test_publish_evidence_rejects_substituted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = self._publish_fixture(root)
            artifact.write_bytes(artifact.read_bytes() + b"tampered")
            with self.assertRaises(Exception):
                validate_publish_evidence(root, "0.2.0", "a" * 40)

    def test_publish_evidence_rejects_tampered_hash_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._publish_fixture(root, evidence_hash="b" * 64)
            with self.assertRaises(Exception):
                validate_publish_evidence(root, "0.2.0", "a" * 40)

    def test_builder_injects_git_head_deterministically_and_rejects_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(json.dumps({"name": "token-governance-layer", "version": "0.2.0", "files": []}, indent=2) + "\n", encoding="utf-8")
            (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
            (root / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n', encoding="utf-8")
            (root / "src" / "token_governance").mkdir(parents=True)
            (root / "src" / "token_governance" / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "release-test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "release-test" + "@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
            with tempfile.TemporaryDirectory(prefix="tgl builder one ") as first_dir, tempfile.TemporaryDirectory(prefix="tgl builder two ") as second_dir:
                first = Path(first_dir) / "token-governance-layer-0.2.0.tgz"
                second = Path(second_dir) / "token-governance-layer-0.2.0.tgz"
                build_package(root, first)
                build_package(root, second)
                self.assertEqual(first.read_bytes(), second.read_bytes())
                with tarfile.open(first, "r:gz") as archive:
                    package = json.loads(archive.extractfile("package/package.json").read())
                expected_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
                self.assertEqual(package["gitHead"], expected_sha)
                with self.assertRaises(FileExistsError):
                    build_package(root, first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
