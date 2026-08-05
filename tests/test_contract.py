from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import re
import sys
import unittest

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import install  # noqa: E402


class ContractTests(unittest.TestCase):
    def test_layout_frontmatter_and_portability_contract(self) -> None:
        required = [
            "SKILL.md", "README.md", "REVIEW_LOG.md", "VERSION", "manifest.json",
            "agents/openai.yaml", "scripts/install.py", "scripts/install.sh",
            "scripts/install.cmd", "scripts/coordinator_state.py",
            "scripts/model_router.py", "scripts/doctor.py",
        ]
        for relative in required:
            self.assertTrue((PACKAGE_ROOT / relative).is_file(), relative)
        skill = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: coordinator\n"))
        for phrase in (
            "Bootstrap before any task work",
            "Remote `SKILL.md` invocation",
            "Mandatory adaptive control loop",
            "gpt-5.6-sol",
            "model_reasoning_effort = \"max\"",
            "Every spawn must explicitly provide",
            "at least two final adversarial review/fix rounds",
            "Windows CMD",
            "five-day cleanup",
        ):
            self.assertIn(phrase, skill)

    def test_readme_has_manual_remote_install_and_restart_gate(self) -> None:
        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("curl -fsSL https://<source>/skills/coordinator/scripts/install.sh", readme)
        self.assertIn("curl.exe -fsSL https://<source>/skills/coordinator/scripts/install.cmd", readme)
        self.assertIn("%USERPROFILE%\\.agents\\skills\\coordinator", readme)
        self.assertIn("~/.agents/skills/coordinator", readme)
        self.assertIn("restart_required: true", readme)
        self.assertIn("Direct remote-skill invocation", readme)
        self.assertIn("temporary Git index", readme)

    def test_manifest_hashes_and_complete_role_map(self) -> None:
        source = install.LocalPackageSource(PACKAGE_ROOT)
        manifest = source.manifest
        self.assertEqual(manifest["version"], (PACKAGE_ROOT / "VERSION").read_text().strip())
        self.assertEqual(set(manifest["project_agent_files"]), set(install.REQUIRED_AGENT_TYPES))
        self.assertIn("manifest.json", manifest["project_skill_files"])
        for relative, spec in manifest["files"].items():
            data = (PACKAGE_ROOT / pathlib.PurePosixPath(relative)).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), spec["sha256"], relative)
            self.assertEqual(len(data), spec["size"], relative)
        for relative in manifest["project_skill_files"]:
            self.assertTrue((PACKAGE_ROOT / pathlib.PurePosixPath(relative)).is_file(), relative)

    def test_python_3_8_syntax_and_standard_library_only(self) -> None:
        allowed = {
            "__future__", "argparse", "ast", "contextlib", "copy", "dataclasses",
            "datetime", "functools", "hashlib", "http", "json", "math", "os",
            "pathlib", "re", "secrets", "shutil", "subprocess", "sys", "tempfile",
            "threading", "time", "tomllib", "typing", "unittest", "urllib", "install",
        }
        for path in sorted(SCRIPTS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path), feature_version=(3, 8))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            self.assertFalse(imports - allowed, f"{path.name}: {sorted(imports - allowed)}")

    def test_custom_agents_are_runtime_routed(self) -> None:
        for role in install.REQUIRED_AGENT_TYPES:
            path = PACKAGE_ROOT / "assets" / "project" / ".codex" / "agents" / f"{role}.toml"
            text = path.read_text(encoding="utf-8")
            for field in ("name", "description", "developer_instructions"):
                self.assertRegex(text, rf"(?m)^\s*{field}\s*=")
            self.assertIsNone(re.search(r"(?m)^\s*(model|model_reasoning_effort)\s*=", text), role)

    def test_all_18_routing_rows_and_dynamic_graph_commands_are_documented(self) -> None:
        routing = (PACKAGE_ROOT / "references" / "model-routing.md").read_text(encoding="utf-8")
        for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            for effort in ("none", "low", "medium", "high", "xhigh", "max"):
                self.assertIn(f"| {model} | {effort} |", routing)
        state = (SCRIPTS / "coordinator_state.py").read_text(encoding="utf-8")
        for command in (
            "node-patch", "dependency-add", "dependency-remove", "node-supersede",
            "node-remove", "graph-validate", "graph-replan", "requirement-set",
        ):
            self.assertIn(f'add_parser("{command}"', state)
        self.assertIn("require_valid_graph(state.get(\"nodes\", {}))", state)
        self.assertIn('node_add.add_argument("--output-contract"', state)
        self.assertIn('node_add.add_argument("--validation-command"', state)
        self.assertIn('task_input.add_argument("--task-file"', state)
        self.assertIn('plan_input.add_argument("--plan-file"', state)
        self.assertIn("done nodes require concrete validation_evidence", state)

    def test_hooks_have_posix_and_windows_commands(self) -> None:
        handler = install.managed_hook_handler()
        self.assertEqual(handler["statusMessage"], "Coordinator activation gate")
        self.assertIn("python3", handler["command"])
        self.assertIn("python", handler["commandWindows"])
        self.assertIn("session-hook", handler["command"])

    def test_current_config_uses_documented_agents_table_without_legacy_v2(self) -> None:
        config = install.merge_config("")
        self.assertIn("[agents]", config)
        self.assertIn("max_concurrent_threads_per_session = 8", config)
        self.assertIn("[features]", config)
        self.assertNotIn("multi_agent_v2", config)

    def test_remote_wrappers_are_cross_platform_and_standard_library_backed(self) -> None:
        shell = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
        cmd = (SCRIPTS / "install.cmd").read_text(encoding="utf-8")
        self.assertIn("urllib.request", shell)
        self.assertIn("tempfile", shell)
        self.assertIn("urllib.request", cmd)
        self.assertNotIn("powershell", cmd.lower())

    def test_distribution_manifest_contains_no_generated_bytecode(self) -> None:
        manifest = json.loads((PACKAGE_ROOT / "manifest.json").read_text(encoding="utf-8"))
        bad = [
            relative for relative in manifest["project_skill_files"]
            if "__pycache__" in pathlib.PurePosixPath(relative).parts
            or pathlib.PurePosixPath(relative).suffix in {".pyc", ".pyo"}
        ]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
