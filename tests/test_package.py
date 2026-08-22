from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from coordinator.install import standalone  # noqa: E402
from tests.support import committed_source  # noqa: E402
from tools import build_release  # noqa: E402


class PackageTests(unittest.TestCase):
    def test_build_is_deterministic_and_has_a_complete_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            first = base / "first"
            second = base / "second"
            result = build_release.build(source, first, commit)
            build_release.build(source, second, commit)

            expected_assets = {"coordinator-3.0.0.zip", "coordinator-latest.zip", "install.py"}
            self.assertEqual({path.name for path in first.iterdir()}, expected_assets)
            self.assertEqual(set(result["assets"]), expected_assets)
            self.assertEqual((result["version"], result["source_commit"]), ("3.0.0", commit))
            for path in first.iterdir():
                self.assertEqual(path.read_bytes(), (second / path.name).read_bytes(), path.name)
            self.assertEqual(
                (first / "coordinator-3.0.0.zip").read_bytes(),
                (first / "coordinator-latest.zip").read_bytes(),
            )

            archive_path = first / "coordinator-3.0.0.zip"
            package = standalone.verify_zip(archive_path.read_bytes(), label="test release")
            with zipfile.ZipFile(archive_path) as archive:
                names = {item.filename for item in archive.infolist()}
                self.assertEqual(names, {"coordinator/" + path for path in package.files})
                marker = json.loads(archive.read("coordinator/.coordinator-package.json"))
                self.assertEqual(
                    marker,
                    {"schema_version": 1, "name": "coordinator", "version": "3.0.0", "source_commit": commit},
                )
                self.assertEqual(
                    archive.read("coordinator/scripts/lib/coordinator/install/standalone.py"),
                    (first / "install.py").read_bytes(),
                )
            self.assertEqual((package.version, package.source_commit), ("3.0.0", commit))

    def test_same_commit_build_ignores_checkout_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            checkouts = []
            for name, autocrlf in (("lf", "false"), ("crlf", "true")):
                checkout = base / name
                subprocess.run(
                    ("git", "-c", f"core.autocrlf={autocrlf}", "clone", "-q", str(source), str(checkout)),
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                subprocess.run(("git", "config", "core.autocrlf", autocrlf), cwd=checkout, check=True)
                status = subprocess.run(
                    ("git", "status", "--porcelain=v1"), cwd=checkout, check=True, text=True, stdout=subprocess.PIPE
                ).stdout
                self.assertEqual(status, "")
                checkouts.append(checkout)

            self.assertNotEqual(
                (checkouts[0] / "skill/README.md").read_bytes(),
                (checkouts[1] / "skill/README.md").read_bytes(),
            )
            outputs = []
            for index, checkout in enumerate(checkouts):
                output = base / f"release-{index}"
                build_release.build(checkout, output, commit)
                outputs.append(output)
            for first in outputs[0].iterdir():
                self.assertEqual(first.read_bytes(), (outputs[1] / first.name).read_bytes(), first.name)

    def test_build_rejects_fake_commit_and_relevant_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            with self.assertRaises(RuntimeError):
                build_release.build(source, base / "fake", "f" * 40)
            with self.assertRaisesRegex(ValueError, "lowercase"):
                build_release.build(source, base / "uppercase", commit.upper())
            (source / "skill" / "README.md").write_text("drift\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "differ"):
                build_release.build(source, base / "drift", commit)

    def test_new_tracked_file_is_packaged_without_an_inventory_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, _commit = committed_source(base / "source")
            added = source / "skill" / "references" / "future.md"
            added.write_text("future release file\n", encoding="utf-8")
            subprocess.run(("git", "add", str(added.relative_to(source))), cwd=source, check=True)
            subprocess.run(("git", "commit", "-qm", "add future file"), cwd=source, check=True)
            commit = subprocess.run(
                ("git", "rev-parse", "HEAD"), cwd=source, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            output = base / "release"
            build_release.build(source, output, commit)
            package = standalone.verify_zip((output / "coordinator-3.0.0.zip").read_bytes(), label="extended")
            self.assertEqual(package.files["references/future.md"], b"future release file\n")

    def test_package_validation_rejects_an_incomplete_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            output = base / "release"
            build_release.build(source, output, commit)
            archive_path = output / "coordinator-3.0.0.zip"
            with zipfile.ZipFile(archive_path) as source_archive:
                kept = [
                    item
                    for item in source_archive.infolist()
                    if item.filename != "coordinator/scripts/lib/coordinator/cli/outcome.py"
                ]
                payloads = {item.filename: source_archive.read(item) for item in kept}
            rebuilt = base / "incomplete.zip"
            with zipfile.ZipFile(rebuilt, "w") as target:
                for item in kept:
                    target.writestr(item, payloads[item.filename])
            with self.assertRaisesRegex(standalone.InstallError, "missing required paths"):
                standalone.verify_zip(rebuilt.read_bytes(), label="incomplete")


if __name__ == "__main__":
    unittest.main()
