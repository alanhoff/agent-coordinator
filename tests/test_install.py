from __future__ import annotations

import functools
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import doctor  # noqa: E402
import install  # noqa: E402


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
    return result


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="coordinator-install-test-")
        self.base = pathlib.Path(self.temporary.name)
        self.temp_root = self.base / "tmp"
        self.temp_root.mkdir()
        self.env = mock.patch.dict(os.environ, {"COORDINATOR_TMP_ROOT": str(self.temp_root)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def make_repo(self, name: str = "repo") -> pathlib.Path:
        repo = self.base / name
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.name", "Coordinator Test")
        git(repo, "config", "user.email", "coordinator@example.test")
        (repo / "README.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "README.txt")
        git(repo, "commit", "-m", "base")
        return repo

    def install_local(self, repo: pathlib.Path):
        return install.ensure_project(
            repo,
            str(PACKAGE_ROOT),
            None,
            None,
            commit=True,
            require_activation=True,
        )

    def test_install_commit_preserves_unrelated_config_hooks_and_staging(self) -> None:
        repo = self.make_repo()
        codex = repo / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text(
            'approval_policy = "never"\n\n[mcp_servers.demo]\ncommand = "demo"\n',
            encoding="utf-8",
        )
        (codex / "hooks.json").write_text(
            json.dumps(
                {
                    "description": "existing",
                    "hooks": {
                        "Notification": [
                            {
                                "matcher": "*",
                                "hooks": [
                                    {"type": "command", "command": "echo existing", "timeout": 2}
                                ],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        (repo / "unrelated.txt").write_text("keep staged\n", encoding="utf-8")
        git(repo, "add", "unrelated.txt")

        result = self.install_local(repo)
        self.assertIn(result["action"], {"installed", "updated"})
        self.assertTrue(result["restart_required"])
        self.assertFalse(result["continue_allowed"])
        self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), result["commit"])
        self.assertIn(f"chore(coordinator): install/update v{install.LocalPackageSource(PACKAGE_ROOT).version}", git(repo, "log", "-1", "--pretty=%s").stdout)

        staged = git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
        self.assertEqual(staged, ["unrelated.txt"])
        committed = git(repo, "show", "--pretty=", "--name-only", "HEAD").stdout.splitlines()
        self.assertIn(".codex/config.toml", committed)
        self.assertIn(".codex/hooks.json", committed)
        self.assertNotIn("unrelated.txt", committed)

        config = (codex / "config.toml").read_text(encoding="utf-8")
        self.assertIn('approval_policy = "never"', config)
        self.assertIn("[mcp_servers.demo]", config)
        assignments = install.parse_simple_toml_assignments(config)
        self.assertEqual(assignments[("", "model")], "gpt-5.6-sol")
        self.assertEqual(assignments[("", "model_reasoning_effort")], "max")
        self.assertEqual(assignments[("agents", "max_concurrent_threads_per_session")], 8)

        hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("Notification", hooks["hooks"])
        self.assertTrue(install.hooks_are_current(codex / "hooks.json"))
        handler = hooks["hooks"]["SessionStart"][-1]["hooks"][0]
        self.assertIn("commandWindows", handler)
        self.assertIn("python3", handler["command"])
        self.assertIn("python", handler["commandWindows"])

        inspection = install.inspect_installation(repo, install.LocalPackageSource(PACKAGE_ROOT))
        self.assertTrue(inspection["current"], inspection["mismatches"])

    def test_restart_activation_update_and_doctor(self) -> None:
        repo = self.make_repo()
        first = self.install_local(repo)
        self.assertTrue(first["restart_required"])

        with self.assertRaises(install.InstallError) as blocked:
            install.ensure_project(
                repo, str(PACKAGE_ROOT), None, None, commit=True, require_activation=True
            )
        self.assertEqual(blocked.exception.code, "activation_required")

        activation = install.issue_activation(
            repo,
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": "session-one",
                "model": "gpt-5.6-sol",
            },
        )
        ready = install.ensure_project(
            repo,
            str(PACKAGE_ROOT),
            None,
            activation["token"],
            commit=True,
            require_activation=True,
        )
        self.assertEqual(ready["action"], "ready")
        self.assertTrue(ready["continue_allowed"])

        report = doctor.inspect_project(
            repo,
            repo / install.SKILL_REL,
            activation["token"],
            require_activation=True,
        )
        self.assertTrue(report["ok"], report["errors"])

        manual_current = install.ensure_project(
            repo, str(PACKAGE_ROOT), None, None, commit=True, require_activation=False
        )
        self.assertEqual(manual_current["action"], "current")
        self.assertFalse(manual_current["continue_allowed"])

        installed_skill = repo / install.SKILL_REL / "references" / "workflow-protocol.md"
        installed_skill.write_text(installed_skill.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")
        update = self.install_local(repo)
        self.assertEqual(update["action"], "updated")
        self.assertTrue(update["restart_required"])
        self.assertNotEqual(first["generation"], update["generation"])

        with self.assertRaises(install.InstallError):
            install.validate_activation(repo, activation["token"])
        activation_two = install.issue_activation(
            repo,
            {
                "hook_event_name": "SessionStart",
                "source": "resume",
                "session_id": "session-two",
                "model": "gpt-5.6-sol",
            },
        )
        self.assertEqual(install.validate_activation(repo, activation_two["token"])["session_id"], "session-two")

    def test_activation_rejects_wrong_parent_model(self) -> None:
        repo = self.make_repo()
        self.install_local(repo)
        activation = install.issue_activation(
            repo,
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": "session-wrong",
                "model": "gpt-5.6-terra",
            },
        )
        with self.assertRaises(install.InstallError) as blocked:
            install.validate_activation(repo, activation["token"])
        self.assertEqual(blocked.exception.code, "activation_required")
        self.assertIn("model", str(blocked.exception))

    def test_remote_source_url_normalization_and_invalid_suffixes(self) -> None:
        self.assertEqual(
            install.normalize_url("  https://example.test/skills/coordinator///  "),
            "https://example.test/skills/coordinator",
        )
        for url in (
            "https://example.test/skills/coordinator?version=1",
            "https://example.test/skills/coordinator#fragment",
            "file:///tmp/coordinator",
        ):
            with self.subTest(url=url):
                with self.assertRaises(install.InstallError) as blocked:
                    install.normalize_url(url)
                self.assertEqual(blocked.exception.code, "invalid_source")

    def test_remote_manifest_install_over_http(self) -> None:
        repo = self.make_repo("remote-repo")
        handler = functools.partial(QuietHandler, directory=str(PACKAGE_ROOT))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            result = install.ensure_project(
                repo,
                None,
                url,
                None,
                commit=True,
                require_activation=False,
            )
            wrapper_repo = self.make_repo("wrapper-repo")
            wrapper = subprocess.run(
                ["sh", str(PACKAGE_ROOT / "scripts" / "install.sh"), "--source", url + "/", "--project", str(wrapper_repo)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=dict(os.environ),
                check=False,
            )
            self.assertEqual(
                wrapper.returncode,
                install.EXIT_RESTART_REQUIRED,
                wrapper.stderr or wrapper.stdout,
            )
            wrapper_payload = json.loads(wrapper.stdout)
            self.assertTrue(wrapper_payload["restart_required"])
            self.assertFalse(wrapper_payload["continue_allowed"])
            self.assertTrue((wrapper_repo / install.SKILL_REL / "SKILL.md").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn(result["action"], {"installed", "updated"})
        self.assertEqual(result["source"], url)
        self.assertTrue((repo / install.SKILL_REL / "manifest.json").is_file())
        self.assertTrue(install.inspect_installation(repo, install.LocalPackageSource(repo / install.SKILL_REL))["current"])


    def test_non_git_project_is_initialized_and_bootstrap_commit_is_created(self) -> None:
        project = self.base / "new-project"
        project.mkdir()
        git_config = self.base / "global-gitconfig"
        subprocess.run(
            ["git", "config", "--file", str(git_config), "user.name", "Bootstrap Test"],
            check=True,
        )
        subprocess.run(
            ["git", "config", "--file", str(git_config), "user.email", "bootstrap@example.test"],
            check=True,
        )
        with mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": str(git_config), "GIT_CONFIG_NOSYSTEM": "1"},
            clear=False,
        ):
            result = install.ensure_project(
                project,
                str(PACKAGE_ROOT),
                None,
                None,
                commit=True,
                require_activation=False,
            )
        self.assertTrue(result["git_initialized"])
        self.assertTrue((project / ".git").exists())
        self.assertEqual(git(project, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(git(project, "rev-parse", "HEAD").stdout.strip(), result["commit"])

    def test_metadata_drift_forces_update_and_new_generation(self) -> None:
        repo = self.make_repo("metadata-drift")
        first = self.install_local(repo)
        metadata_path = repo / install.METADATA_REL
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("generation")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        updated = self.install_local(repo)
        self.assertEqual(updated["action"], "updated")
        self.assertNotEqual(first["commit"], updated["commit"])
        repaired = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertTrue(repaired["generation"])

    def test_automatic_source_selection_repairs_same_version_project_drift(self) -> None:
        repo = self.make_repo("auto-source")
        self.install_local(repo)
        home = self.base / "home"
        global_skill = home / ".agents" / "skills" / "coordinator"
        global_skill.parent.mkdir(parents=True)
        import shutil
        shutil.copytree(PACKAGE_ROOT, global_skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        project_skill = repo / install.SKILL_REL
        drifted = project_skill / "SKILL.md"
        drifted.write_text(
            drifted.read_text(encoding="utf-8") + "\nproject drift\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False), mock.patch.object(
            install, "PACKAGE_ROOT", project_skill
        ):
            source = install.select_source(repo, install.temp_root(), None, None)
        self.assertEqual(pathlib.Path(source.label), global_skill.resolve())
        self.assertEqual(source.version, (PACKAGE_ROOT / "VERSION").read_text().strip())

    def test_cleanup_removes_artifacts_older_than_five_days(self) -> None:
        stale = self.temp_root / "download-old"
        stale.mkdir()
        (stale / "x").write_text("x", encoding="utf-8")
        old = time.time() - 6 * 24 * 60 * 60
        os.utime(stale, (old, old))
        activation = self.temp_root / "activation"
        activation.mkdir()
        old_token = activation / "old.json"
        fresh_token = activation / "fresh.json"
        old_token.write_text("old", encoding="utf-8")
        fresh_token.write_text("fresh", encoding="utf-8")
        os.utime(old_token, (old, old))
        removed = install.cleanup_stale(self.temp_root)
        self.assertIn(str(stale), removed)
        self.assertIn(str(old_token), removed)
        self.assertFalse(stale.exists())
        self.assertFalse(old_token.exists())
        self.assertTrue(fresh_token.exists())

    def test_ambiguous_inline_config_is_blocked_without_changes(self) -> None:
        repo = self.make_repo("ambiguous")
        codex = repo / ".codex"
        codex.mkdir()
        path = codex / "config.toml"
        original = 'agents = { enabled = true }\n'
        path.write_text(original, encoding="utf-8")
        with self.assertRaises(install.InstallError) as blocked:
            self.install_local(repo)
        self.assertEqual(blocked.exception.code, "config_ambiguous")
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_legacy_v2_scalar_is_removed_and_unrelated_array_table_is_preserved(self) -> None:
        existing = (
            "[[mcp_servers.pool]]\n"
            'name = "one"\n\n'
            "[features]\n"
            "multi_agent_v2 = true\n"
            "other_feature = true\n"
        )
        merged = install.merge_config(existing)
        self.assertIn("[[mcp_servers.pool]]", merged)
        self.assertIn('name = "one"', merged)
        self.assertIn("other_feature = true", merged)
        self.assertNotIn("multi_agent_v2", merged)
        self.assertIn("[agents]", merged)
        self.assertIn("max_concurrent_threads_per_session = 8", merged)

    def test_target_array_dotted_quoted_and_multiline_config_forms_block(self) -> None:
        cases = {
            "target array": "[[agents]]\nname = 'x'\n",
            "dotted target": "agents.enabled = true\n",
            "quoted root key": '"model" = "gpt-5.6-terra"\n',
            "quoted target table": '["features"]\nhooks = false\n',
            "dotted managed key": "[agents]\nenabled.child = true\n",
            "multiline": 'instructions = """line one\nline two"""\n',
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(install.InstallError) as blocked:
                    install.merge_config(text)
                self.assertEqual(blocked.exception.code, "config_ambiguous")

    def test_child_target_tables_are_preserved(self) -> None:
        existing = "[agents.custom]\nflag = true\n[[features.rules]]\nname = 'kept'\n"
        merged = install.merge_config(existing)
        self.assertIn("[agents.custom]", merged)
        self.assertIn("[[features.rules]]", merged)
        self.assertIn("[agents]", merged)
        self.assertIn("[features]", merged)

    def test_malformed_session_start_hooks_block_instead_of_being_preserved(self) -> None:
        malformed = [
            {"hooks": {"SessionStart": ["not-an-object"]}},
            {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": "not-an-array"}]}},
            {"hooks": {"SessionStart": [{"matcher": 3, "hooks": []}]}},
            {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": ["bad-handler"]}]}},
        ]
        for document in malformed:
            with self.subTest(document=document):
                with self.assertRaises(install.InstallError) as blocked:
                    install.merge_hooks(document)
                self.assertEqual(blocked.exception.code, "hooks_ambiguous")

    def test_extra_or_misplaced_managed_hook_is_outdated_and_repaired(self) -> None:
        path = self.base / "hooks.json"
        document = install.merge_hooks(None)
        document["hooks"]["SessionStart"].append(
            {"matcher": "*", "hooks": [install.managed_hook_handler()]}
        )
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertFalse(install.hooks_are_current(path))
        repaired = install.merge_hooks(document)
        path.write_text(json.dumps(repaired), encoding="utf-8")
        self.assertTrue(install.hooks_are_current(path))


    def test_stale_managed_file_deletion_is_committed_and_worktree_is_clean(self) -> None:
        repo = self.make_repo("stale-managed")
        self.install_local(repo)

        stale_relative = ".agents/skills/coordinator/obsolete-managed.txt"
        stale_path = repo / pathlib.PurePosixPath(stale_relative)
        stale_path.write_text("old managed content\n", encoding="utf-8")
        metadata_path = repo / install.METADATA_REL
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["managed_paths"].append(stale_relative)
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        git(repo, "add", stale_relative, install.METADATA_REL.as_posix())
        git(repo, "commit", "-m", "simulate previous coordinator layout")

        result = self.install_local(repo)
        self.assertEqual(result["action"], "updated")
        self.assertFalse(stale_path.exists())
        name_status = git(repo, "show", "--format=", "--name-status", "HEAD").stdout.splitlines()
        self.assertIn("D\t" + stale_relative, name_status)
        self.assertEqual(git(repo, "status", "--porcelain").stdout, "")

    def test_manual_cli_returns_restart_exit_code_then_zero_when_current(self) -> None:
        repo = self.make_repo("repo with spaces")
        command = [
            sys.executable,
            str(SCRIPTS / "install.py"),
            "install",
            "--source",
            str(PACKAGE_ROOT),
            "--project",
            str(repo),
            "--json",
        ]
        first = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(first.returncode, install.EXIT_RESTART_REQUIRED, first.stderr or first.stdout)
        first_payload = json.loads(first.stdout)
        self.assertTrue(first_payload["restart_required"])
        self.assertFalse(first_payload["continue_allowed"])

        second = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        second_payload = json.loads(second.stdout)
        self.assertEqual(second_payload["action"], "current")
        self.assertFalse(second_payload["continue_allowed"])

    def test_newer_broken_local_source_blocks_automatic_downgrade(self) -> None:
        repo = self.make_repo("downgrade")
        project_source = repo / install.SKILL_REL
        project_source.mkdir(parents=True)
        manifest = json.loads((PACKAGE_ROOT / "manifest.json").read_text(encoding="utf-8"))
        manifest["version"] = "99.0.0"
        (project_source / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with self.assertRaises(install.InstallError) as blocked:
            install.select_source(repo, install.temp_root(), None, None)
        self.assertEqual(blocked.exception.code, "source_downgrade_blocked")


if __name__ == "__main__":
    unittest.main()
