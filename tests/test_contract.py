from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coordinator import VERSION  # noqa: E402
from coordinator.install import standalone  # noqa: E402


class PublicContractTests(unittest.TestCase):
    def test_v3_source_and_public_metadata_are_complete(self) -> None:
        self.assertEqual(VERSION, "3.0.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), VERSION)
        for path in (
            "README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", "LICENSE", "pyproject.toml",
            "skill/SKILL.md", "skill/README.md", "skill/agents/openai.yaml",
            "tools/build_release.py", "tools/verify_release.py",
        ):
            self.assertTrue((ROOT / path).is_file(), path)
        for owner in ("install", "state", "routing", "dashboard", "cli"):
            self.assertTrue((ROOT / "src" / "coordinator" / owner).is_dir(), owner)

    def test_exact_roles_and_thin_entry_scripts(self) -> None:
        roles = ROOT / "skill" / "agents" / "roles"
        self.assertEqual({path.stem for path in roles.glob("*.toml")}, set(standalone.ROLES))
        for path in roles.glob("*.toml"):
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r"(?m)^name = ")
            self.assertRegex(text, r"(?m)^description = ")
            self.assertRegex(text, r"(?m)^developer_instructions = ")
            self.assertIsNone(re.search(r"(?m)^\s*(model|model_reasoning_effort)\s*=", text))
        scripts = ROOT / "skill" / "scripts"
        for name in ("install.py", "doctor.py", "coordinator_state.py", "model_router.py", "dashboard.py"):
            source = (scripts / name).read_text(encoding="utf-8")
            self.assertLessEqual(len(source.splitlines()), 10, name)
            self.assertIn("from coordinator", source)
        self.assertTrue((scripts / "install.sh").is_file())
        self.assertTrue((scripts / "install.cmd").is_file())

    def test_runtime_imports_only_standard_library_and_coordinator(self) -> None:
        standard = set(sys.stdlib_module_names)
        for path in (ROOT / "src" / "coordinator").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level:
                    names = []
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    self.assertTrue(name == "coordinator" or name in standard, f"{path}: {name}")

    def test_all_python_adapters_emit_json_for_invalid_invocations(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("install.py", "doctor.py", "coordinator_state.py", "model_router.py", "dashboard.py"):
            result = subprocess.run(
                [sys.executable, str(ROOT / "skill" / "scripts" / name), "invalid", "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
            self.assertEqual(result.returncode, 2, name)
            self.assertEqual(result.stderr, "", name)
            payload = json.loads(result.stdout)
            self.assertEqual(
                set(payload),
                {"command", "status", "code", "data", "warnings", "new_session_required"},
                name,
            )
            self.assertEqual((payload["status"], payload["code"]), ("error", "invalid_invocation"), name)

if __name__ == "__main__":
    unittest.main()
