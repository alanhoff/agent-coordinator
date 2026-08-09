from __future__ import annotations

import json
import hashlib
import io
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from tools import build_release, verify_release  # noqa: E402


def committed_source(target: pathlib.Path) -> tuple[pathlib.Path, str]:
    target.mkdir()
    for name in ("skill", "src"):
        shutil.copytree(ROOT / name, target / name)
    for name in ("LICENSE", "VERSION"):
        shutil.copy2(ROOT / name, target / name)
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "Coordinator Tests"),
        ("git", "config", "user.email", "tests@example.invalid"),
        ("git", "add", "skill", "src/coordinator", "LICENSE", "VERSION"),
        ("git", "commit", "-qm", "fixture"),
    ):
        subprocess.run(command, cwd=target, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=target, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()
    return target, commit


def remove_inventoried_path(release: pathlib.Path, relative: str) -> None:
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"].pop(relative)
    manifest_bytes = build_release._json(manifest)
    versioned = release / f"coordinator-{manifest['version']}.zip"
    source = zipfile.ZipFile(io.BytesIO(versioned.read_bytes()))
    rebuilt = io.BytesIO()
    with source, zipfile.ZipFile(rebuilt, "w") as target:
        for item in source.infolist():
            if item.filename == "coordinator/" + relative:
                continue
            data = manifest_bytes if item.filename == "coordinator/manifest.json" else source.read(item)
            target.writestr(item, data)
    archive = rebuilt.getvalue()
    versioned.write_bytes(archive)
    (release / "coordinator-latest.zip").write_bytes(archive)
    (release / "manifest.json").write_bytes(manifest_bytes)
    payloads = {path.name: path.read_bytes() for path in release.iterdir() if path.name != "SHA256SUMS"}
    sums = "".join(f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n" for name in sorted(payloads))
    (release / "SHA256SUMS").write_text(sums, encoding="ascii", newline="\n")


class PackageTests(unittest.TestCase):
    def test_two_builds_are_identical_and_independently_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            first = base / "first"
            second = base / "second"
            build_release.build(source, first, commit)
            build_release.build(source, second, commit)
            self.assertEqual({path.name for path in first.iterdir()}, {
                "coordinator-3.0.0.zip", "coordinator-latest.zip", "install.py", "manifest.json", "SHA256SUMS"
            })
            for path in first.iterdir():
                self.assertEqual(path.read_bytes(), (second / path.name).read_bytes(), path.name)
            result = verify_release.verify(first, commit)
            self.assertEqual(result["version"], "3.0.0")
            manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("manifest.json", manifest["files"])
            self.assertEqual(set(manifest["roles"]), set(build_release.ROLES))

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
            self.assertEqual(verify_release.verify(outputs[0], commit)["source_commit"], commit)
            self.assertEqual(verify_release.verify(outputs[1], commit)["source_commit"], commit)

    def test_independent_verifier_rejects_asset_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            output = base / "release"
            build_release.build(source, output, commit)
            with (output / "install.py").open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaises(verify_release.VerificationError):
                verify_release.verify(output, commit)

    def test_independent_verifier_rejects_directory_shaped_zip_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            output = base / "release"
            build_release.build(source, output, commit)
            versioned = output / "coordinator-3.0.0.zip"
            archive = io.BytesIO(versioned.read_bytes())
            with zipfile.ZipFile(archive, "a") as target:
                target.writestr("coordinator/unlisted-payload/", b"hidden bytes")
            tampered = archive.getvalue()
            versioned.write_bytes(tampered)
            (output / "coordinator-latest.zip").write_bytes(tampered)
            payloads = {path.name: path.read_bytes() for path in output.iterdir() if path.name != "SHA256SUMS"}
            checksums = "".join(f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n" for name in sorted(payloads))
            (output / "SHA256SUMS").write_text(checksums, encoding="ascii", newline="\n")
            with self.assertRaisesRegex(verify_release.VerificationError, "unlisted directory"):
                verify_release.verify(output, commit)

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

    def test_verifier_rejects_self_consistent_incomplete_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            source, commit = committed_source(base / "source")
            output = base / "release"
            build_release.build(source, output, commit)
            remove_inventoried_path(output, "scripts/lib/coordinator/cli/outcome.py")
            with self.assertRaisesRegex(verify_release.VerificationError, "canonical"):
                verify_release.verify(output, commit)


if __name__ == "__main__":
    unittest.main()
