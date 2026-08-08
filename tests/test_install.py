from __future__ import annotations

import contextlib
import io
import os
import base64
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from coordinator.install import standalone  # noqa: E402
from tools import build_release  # noqa: E402


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


def recovery_journal(owned: standalone.Paths, phase: str, transaction: str) -> dict:
    metadata = json.loads(owned.metadata.read_text(encoding="utf-8"))
    package = standalone.verify_directory(owned.package)
    roles_digest = standalone._roles_digest(owned.roles)
    config_bytes = owned.config.read_bytes()
    metadata_bytes = owned.metadata.read_bytes()
    return {
        "schema_version": standalone.INSTALL_SCHEMA,
        "transaction": transaction,
        "package_digest": metadata["package_digest"],
        "package_content_digest": package.content_digest,
        "roles_content_digest": roles_digest,
        "config_digest": standalone._sha256(config_bytes),
        "metadata_digest": standalone._sha256(metadata_bytes),
        "phase": phase,
        "package_backup": str(owned.package.parent / f".coordinator.backup.{transaction}"),
        "roles_backup": str(owned.roles.parent / f".coordinator.backup.{transaction}"),
        "package_existed": True,
        "package_backup_digest": package.content_digest,
        "roles_existed": True,
        "roles_backup_digest": roles_digest,
        "config_existed": True,
        "config_backup": base64.b64encode(config_bytes).decode("ascii"),
        "config_backup_digest": standalone._sha256(config_bytes),
        "metadata_existed": True,
        "metadata_backup": base64.b64encode(metadata_bytes).decode("ascii"),
        "metadata_backup_digest": standalone._sha256(metadata_bytes),
    }


class GlobalInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.source, self.commit = committed_source(self.base / "source")
        self.release = self.base / "release"
        build_release.build(self.source, self.release, self.commit)
        self.environment = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_install_is_global_idempotent_and_preserves_repository_and_config(self) -> None:
        repo = self.base / "repo"
        repo.mkdir()
        sentinel = repo / "user.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('# user comment\nmodel = "gpt-user"\n', encoding="utf-8")
        package = standalone.acquire_source(str(self.release / "coordinator-3.0.0.zip"), None, None)
        result = standalone.ensure_global(package, between_sessions=True)
        self.assertTrue(result["changed"])
        self.assertTrue(result["new_session_required"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
        self.assertEqual(list(repo.iterdir()), [sentinel])
        owned = standalone.paths()
        self.assertTrue((owned.package / "SKILL.md").is_file())
        self.assertEqual({path.stem for path in owned.roles.glob("*.toml")}, set(standalone.ROLES))
        merged = config.read_text(encoding="utf-8")
        self.assertIn("# user comment", merged)
        self.assertIn('model = "gpt-user"', merged)
        self.assertTrue(standalone.inspect()["current"])
        second = standalone.ensure_global(package, between_sessions=True)
        self.assertFalse(second["changed"])

        alias = self.base / "config-alias.toml"
        os.link(config, alias)
        info = config.lstat()
        before = (info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns, config.read_bytes())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = standalone.main(["status", "--json"])
        status = json.loads(output.getvalue())
        self.assertEqual((code, status["code"], status["data"]["mismatches"]), (1, "install_drift", ["paths:unsafe_path"]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = standalone.main([
                "ensure-global", "--source", str(self.release / "coordinator-3.0.0.zip"),
                "--between-sessions", "--json",
            ])
        self.assertEqual((code, json.loads(output.getvalue())["code"]), (20, "unsafe_path"))
        info = config.lstat()
        after = (info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns, config.read_bytes())
        self.assertEqual((after, alias.read_bytes()), (before, before[-1]))

    def test_tampered_source_fails_before_host_mutation(self) -> None:
        source = self.release / "coordinator-3.0.0.zip"
        tampered = source.read_bytes() + b"trailing tamper"
        path = self.base / "tampered.zip"
        path.write_bytes(tampered)
        with self.assertRaises(standalone.InstallError):
            standalone.acquire_source(str(path), None, None)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_install_rejects_dangling_owned_leaf_before_transaction(self) -> None:
        owned = standalone.paths()
        owned.package.parent.mkdir(parents=True)
        missing = self.base / "missing-package"
        owned.package.symlink_to(missing, target_is_directory=True)
        command = [
            "ensure-global", "--source", str(self.release / "coordinator-3.0.0.zip"),
            "--between-sessions", "--json",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = standalone.main(command)
        self.assertEqual((code, json.loads(output.getvalue())["code"]), (20, "unsafe_path"))
        self.assertEqual(owned.package.readlink(), missing)
        self.assertFalse(owned.journal.exists() or owned.journal.is_symlink())
        self.assertFalse(any(owned.package.parent.glob(".coordinator.new.*")))
        self.assertFalse(owned.roles.parent.exists() and any(owned.roles.parent.glob(".coordinator.new.*")))

        owned.package.unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = standalone.main(command)
        self.assertEqual((code, json.loads(output.getvalue())["code"]), (0, "install_updated"))

    def test_installer_rejects_unlisted_and_unsupported_zip_entries(self) -> None:
        archive = io.BytesIO((self.release / "coordinator-3.0.0.zip").read_bytes())
        with zipfile.ZipFile(archive, "a") as target:
            target.writestr("coordinator/unlisted-payload/", b"hidden bytes")
        with self.assertRaisesRegex(standalone.InstallError, "unlisted directory"):
            standalone.verify_zip(archive.getvalue(), label="adversarial")

        original = (self.release / "coordinator-3.0.0.zip").read_bytes()
        with zipfile.ZipFile(io.BytesIO(original)) as target:
            item = target.getinfo("coordinator/VERSION")
        central_name = original.rfind(item.filename.encode("utf-8"))
        central = original.rfind(b"PK\x01\x02", 0, central_name)
        self.assertGreaterEqual(central, 0)
        for kind in ("encrypted", "unsupported"):
            data = bytearray(original)
            if kind == "encrypted":
                data[item.header_offset + 6 : item.header_offset + 8] = (
                    int.from_bytes(data[item.header_offset + 6 : item.header_offset + 8], "little") | 1
                ).to_bytes(2, "little")
                data[central + 8 : central + 10] = (
                    int.from_bytes(data[central + 8 : central + 10], "little") | 1
                ).to_bytes(2, "little")
            else:
                data[item.header_offset + 8 : item.header_offset + 10] = (99).to_bytes(2, "little")
                data[central + 10 : central + 12] = (99).to_bytes(2, "little")
            source = self.base / f"{kind}.zip"
            source.write_bytes(data)
            output = io.StringIO()
            with self.subTest(kind=kind), contextlib.redirect_stdout(output):
                code = standalone.main([
                    "ensure-global", "--source", str(source), "--between-sessions", "--json",
                ])
                self.assertEqual((code, json.loads(output.getvalue())["code"]), (20, "invalid_package"))
                self.assertEqual(list(self.home.iterdir()), [])

    def test_status_reports_symlinked_home_without_writing(self) -> None:
        package = standalone.acquire_source(str(self.release / "coordinator-3.0.0.zip"), None, None)
        standalone.ensure_global(package, between_sessions=True)
        before = {path.relative_to(self.home): path.stat().st_mtime_ns for path in self.home.rglob("*")}
        home_link = self.base / "home-link"
        home_link.symlink_to(self.home, target_is_directory=True)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home_link)}, clear=False), contextlib.redirect_stdout(output):
            code = standalone.main(["status", "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["code"], result["data"]["mismatches"]), (1, "install_drift", ["paths:unsafe_path"]))
        self.assertEqual({path.relative_to(self.home): path.stat().st_mtime_ns for path in self.home.rglob("*")}, before)

    def test_ambiguous_owned_config_is_left_unchanged(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = "[agents]\nenabled = true\n"
        config.write_text(original, encoding="utf-8")
        package = standalone.acquire_source(str(self.release / "coordinator-3.0.0.zip"), None, None)
        with self.assertRaisesRegex(standalone.InstallError, "not Coordinator-owned"):
            standalone.ensure_global(package, between_sessions=True)
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        self.assertFalse(standalone.paths().package.exists())

    def test_config_current_requires_one_ordered_marker_pair_per_table(self) -> None:
        config = self.base / "config.toml"
        merged = standalone._config_merge(b"")
        config.write_bytes(merged)
        self.assertTrue(standalone._config_current(config))
        text = config.read_text(encoding="utf-8")
        config.write_text(text.replace("# <<< agent-coordinator:agents\n", ""), encoding="utf-8")
        self.assertFalse(standalone._config_current(config))
        config.write_text(text + "# <<< agent-coordinator:features\n", encoding="utf-8")
        self.assertFalse(standalone._config_current(config))

    def test_install_snapshots_user_config_after_lock_acquisition(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "before"\n', encoding="utf-8")
        package = standalone.acquire_source(str(self.release / "coordinator-3.0.0.zip"), None, None)
        real_lock = standalone._install_lock

        @contextlib.contextmanager
        def concurrent_edit(owned: standalone.Paths):
            config.write_text('model = "concurrent-user-edit"\n', encoding="utf-8")
            with real_lock(owned):
                yield

        with mock.patch.object(standalone, "_install_lock", concurrent_edit):
            standalone.ensure_global(package, between_sessions=True)
        merged = config.read_text(encoding="utf-8")
        self.assertIn('model = "concurrent-user-edit"', merged)
        self.assertNotIn('model = "before"', merged)

    def test_cleanup_rejects_symlinked_control_ancestor(self) -> None:
        outside = self.base / "outside" / "control"
        cache = outside / "cache"
        cache.mkdir(parents=True)
        (cache / ".coordinator-cache").write_text("coordinator-v3\n", encoding="utf-8")
        victim = cache / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        (self.home / ".agent-coordinator").symlink_to(outside, target_is_directory=True)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = standalone.main(["cleanup", "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["code"]), (20, "unsafe_path"))
        self.assertTrue(victim.is_file())
        self.assertTrue(cache.is_dir())

    def test_cleanup_rejects_symlinked_home(self) -> None:
        real_home = self.base / "outside-home"
        cache = real_home / ".agent-coordinator" / "cache"
        cache.mkdir(parents=True)
        (cache / ".coordinator-cache").write_text("coordinator-v3\n", encoding="utf-8")
        victim = cache / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        home_link = self.base / "home-link"
        home_link.symlink_to(real_home, target_is_directory=True)

        output = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home_link)}, clear=False), contextlib.redirect_stdout(output):
            code = standalone.main(["cleanup", "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["code"]), (20, "unsafe_path"))
        self.assertTrue(victim.is_file())
        self.assertTrue(cache.is_dir())

    def test_interrupted_update_rolls_back_and_committed_recovery_finalizes(self) -> None:
        first = standalone.acquire_source(str(self.release / "coordinator-3.0.0.zip"), None, None)
        standalone.ensure_global(first, between_sessions=True)
        second_release = self.base / "second-release"
        subprocess.run(
            ("git", "commit", "--allow-empty", "-qm", "second fixture"),
            cwd=self.source,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second_commit = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.source, check=True, text=True, stdout=subprocess.PIPE
        ).stdout.strip()
        build_release.build(self.source, second_release, second_commit)
        second = standalone.acquire_source(str(second_release / "coordinator-3.0.0.zip"), None, None)
        owned = standalone.paths()
        real_replace = os.replace

        def interrupt(source, target):
            if pathlib.Path(source) == owned.roles and pathlib.Path(target).name.startswith(".coordinator.backup."):
                raise OSError("simulated interruption")
            return real_replace(source, target)

        with mock.patch.object(standalone.os, "replace", side_effect=interrupt):
            with self.assertRaisesRegex(standalone.InstallError, "filesystem boundary"):
                standalone.ensure_global(second, between_sessions=True)
        self.assertEqual(standalone.inspect()["source_commit"], self.commit)
        self.assertFalse(owned.journal.exists())
        self.assertFalse(any(path.name.startswith(".coordinator.new.") for path in owned.package.parent.iterdir()))

        metadata = json.loads(owned.metadata.read_text(encoding="utf-8"))
        transaction = metadata["transaction"]
        journal = recovery_journal(owned, "committed", transaction)
        standalone._safe_directory(owned.journal.parent, owned.home, private=True)
        standalone._atomic(owned.journal, standalone._json_bytes(journal))
        code, status = standalone.recovery_status()
        self.assertEqual(code, 1)
        self.assertEqual(status["action"], "finalize")
        result = standalone.recover(action="finalize", transaction=transaction, digest=metadata["package_digest"], between_sessions=True)
        self.assertTrue(result["recovered"])
        self.assertTrue(standalone.inspect()["current"])

    def test_recovery_rejects_missing_and_invalid_required_backups(self) -> None:
        package = standalone.acquire_source(str(self.release / "coordinator-3.0.0.zip"), None, None)
        standalone.ensure_global(package, between_sessions=True)
        owned = standalone.paths()
        transaction = "1" * 24
        journal = recovery_journal(owned, "surface_replaced", transaction)
        standalone._atomic(owned.journal, standalone._json_bytes(journal))
        with self.assertRaisesRegex(standalone.InstallError, "surface-replaced"):
            standalone.recover(
                action="rollback", transaction=transaction, digest=journal["package_digest"], between_sessions=True
            )
        self.assertTrue(owned.journal.exists())

        owned.journal.unlink()
        package_backup = pathlib.Path(journal["package_backup"])
        shutil.copytree(owned.package, package_backup)
        (package_backup / "VERSION").write_text("tampered\n", encoding="utf-8")
        standalone._atomic(owned.journal, standalone._json_bytes(journal))
        with self.assertRaises(standalone.InstallError):
            standalone.recover(
                action="rollback", transaction=transaction, digest=journal["package_digest"], between_sessions=True
            )
        self.assertTrue(owned.journal.exists())

    def test_recovery_revalidates_ancestors_after_lock_acquisition(self) -> None:
        package = standalone.acquire_source(str(self.release / "coordinator-3.0.0.zip"), None, None)
        standalone.ensure_global(package, between_sessions=True)
        owned = standalone.paths()
        transaction = "2" * 24
        journal = recovery_journal(owned, "committed", transaction)
        standalone._atomic(owned.journal, standalone._json_bytes(journal))
        real_lock = standalone._install_lock

        @contextlib.contextmanager
        def swap_parent(current: standalone.Paths):
            with real_lock(current):
                original = self.home / ".agents"
                saved = self.home / ".agents.saved"
                outside = self.base / "outside"
                outside.mkdir()
                os.replace(original, saved)
                original.symlink_to(outside, target_is_directory=True)
                try:
                    yield
                finally:
                    original.unlink()
                    os.replace(saved, original)

        with mock.patch.object(standalone, "_install_lock", swap_parent):
            with self.assertRaisesRegex(standalone.InstallError, "unsafe ancestor"):
                standalone.recover(
                    action="finalize", transaction=transaction, digest=journal["package_digest"], between_sessions=True
                )
        self.assertTrue(owned.journal.exists())

    def test_install_lock_reclaims_proven_stale_but_preserves_live_lock(self) -> None:
        owned = standalone.paths()
        standalone._safe_directory(owned.lock.parent, owned.home, private=True)
        stale = json.dumps({"pid": 999999, "nonce": "a" * 32, "created_at": standalone._now()}).encode()
        owned.lock.write_bytes(stale)
        os.chmod(owned.lock, 0o600)
        with mock.patch.object(standalone, "_process_is_proven_dead", return_value=True):
            with standalone._install_lock(owned):
                self.assertNotEqual(owned.lock.read_bytes(), stale)
        self.assertFalse(owned.lock.exists())

        live = json.dumps({"pid": os.getpid(), "nonce": "b" * 32, "created_at": standalone._now()}).encode()
        owned.lock.write_bytes(live)
        os.chmod(owned.lock, 0o600)
        with self.assertRaisesRegex(standalone.InstallError, "holds"):
            with standalone._install_lock(owned):
                pass
        self.assertEqual(owned.lock.read_bytes(), live)

        with (
            mock.patch.object(standalone.os, "name", "nt"),
            mock.patch.object(standalone, "_windows_process_is_proven_dead", return_value=True) as query,
            mock.patch.object(standalone.os, "kill", side_effect=AssertionError("Windows must not call os.kill")),
        ):
            self.assertTrue(standalone._process_is_proven_dead(123))
        query.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
