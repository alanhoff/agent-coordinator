from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coordinator.state.store import new_state  # noqa: E402

EXPECTED_ROLES = {
    "architect",
    "designer",
    "documenter",
    "fixer",
    "implementer",
    "researcher",
    "reviewer",
    "validator",
}


class PublicContractTests(unittest.TestCase):
    def test_schema_v4_refinement_and_node_split_public_contract(self) -> None:
        protocol = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
        complexity_path = ROOT / "skill" / "references" / "complexity-accounting.md"
        complexity_reference = complexity_path.read_text(encoding="utf-8")

        for required in (
            "node-refine",
            "node-split",
            "fixed point",
            "current `executable` assessment",
            "references/complexity-accounting.md",
        ):
            self.assertIn(required, protocol)

        examples = [
            json.loads(block)
            for block in re.findall(r"```json\s*\n(.*?)\n```", complexity_reference, flags=re.DOTALL)
        ]
        self.assertEqual(len(examples), 2, "complexity-accounting.md must publish refinement and split JSON")
        refinement, split = examples
        self.assertEqual(set(refinement), {"spec", "acceptance", "write_scopes", "assessment"})
        self.assertEqual(
            set(refinement["spec"]),
            {"objective", "inputs", "outputs", "constraints", "non_goals", "requirement_ids", "open_questions"},
        )
        self.assertEqual(set(refinement["assessment"]), {"dimensions", "ambiguity", "rationale"})
        self.assertEqual(set(refinement["assessment"]["dimensions"]), {
            "breadth", "change_surface", "coupling", "novelty", "verification",
        })

        self.assertEqual(
            set(split),
            {"parent_id", "reason", "children", "coverage", "dependent_replacements"},
        )
        self.assertGreaterEqual(len(split["children"]), 2)
        child_keys = {
            "id", "title", "stage", "priority", "dependencies", "write_scopes", "role", "model", "effort",
            "acceptance", "route_rationale", "estimated_cost", "spec", "assessment",
        }
        for child in split["children"]:
            self.assertEqual(set(child), child_keys)
            self.assertEqual(
                set(child["spec"]),
                {
                    "objective", "inputs", "outputs", "constraints", "non_goals", "requirement_ids",
                    "open_questions",
                },
            )
            self.assertEqual(set(child["assessment"]), {"dimensions", "ambiguity", "rationale"})
        self.assertEqual(set(split["coverage"]), {"requirements", "outputs", "acceptance"})
        for mapping in (*split["coverage"].values(), split["dependent_replacements"]):
            self.assertIsInstance(mapping, dict)
            for replacements in mapping.values():
                self.assertIsInstance(replacements, list)
                self.assertTrue(replacements)

    def test_surviving_source_and_metadata_are_complete(self) -> None:
        for relative in (
            "README.md",
            "INSTALL.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "LICENSE",
            "pyproject.toml",
            "skill/SKILL.md",
            "skill/README.md",
            "skill/agents/openai.yaml",
            "skill/scripts/coordinator_state.py",
            "skill/scripts/model_router.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

        owners = {
            path.name
            for path in (ROOT / "src" / "coordinator").iterdir()
            if path.is_dir() and path.name != "__pycache__"
        }
        self.assertEqual(owners, {"cli", "routing", "state"})
        configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("project", configuration)
        self.assertEqual(configuration["tool"]["ruff"]["target-version"], "py311")
        self.assertNotIn("VER" + "SION", (ROOT / "src" / "coordinator" / "__init__.py").read_text(encoding="utf-8"))
        state = new_state({"path": "/repository", "identity": "0" * 64}, "task", "session")
        self.assertNotIn("version", state)

    def test_inline_role_profiles(self) -> None:
        roles = ROOT / "skill" / "agents" / "roles"
        self.assertEqual({path.stem for path in roles.glob("*.toml")}, EXPECTED_ROLES)
        for path in roles.glob("*.toml"):
            text = path.read_text(encoding="utf-8")
            role = tomllib.loads(text)
            self.assertEqual(set(role), {"name", "description", "developer_instructions"})
            self.assertEqual(role["name"], path.stem)
            self.assertIsNone(re.search(r"(?m)^\s*(model|model_reasoning_effort)\s*=", text))

        scripts = ROOT / "skill" / "scripts"
        self.assertEqual({path.name for path in scripts.iterdir() if path.is_file()}, {
            "coordinator_state.py",
            "model_router.py",
        })
        for path in scripts.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(source.splitlines()), 10, path.name)
            self.assertIn("from coordinator", source)

        protocol = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
        for required in (
            "agents/roles/ROLE.toml",
            "`description`",
            "`developer_instructions`",
            "tool's task/message argument",
        ):
            self.assertIn(required, protocol)

    def test_runtime_imports_only_standard_library_and_coordinator(self) -> None:
        standard = set(sys.stdlib_module_names)
        for path in (ROOT / "src" / "coordinator").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    self.assertTrue(name == "coordinator" or name in standard, f"{path}: {name}")

    def test_all_python_adapters_emit_json_for_invalid_invocations(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("coordinator_state.py", "model_router.py"):
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
            self.assertEqual(set(payload), {"command", "status", "code", "data", "warnings"}, name)
            self.assertEqual((payload["status"], payload["code"]), ("error", "invalid_invocation"), name)

    def test_self_contained_install_surface(self) -> None:
        prompt = "Install https://github.com/alanhoff/agent-coordinator by following INSTALL.md"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        instructions = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        section = readme.split("## Install\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"> `{prompt}`", section)
        self.assertEqual(readme.count(prompt), 1)
        for required in (
            "~/.agents/skills/coordinator",
            "skill/",
            "src/coordinator/",
            "scripts/lib/coordinator/",
            "Keep role profiles inside `agents/roles/`",
        ):
            self.assertIn(required, instructions)
        self.assertEqual(
            set(re.findall(r"`(~/[^`]+)`", instructions)),
            {"~/.agents/skills/coordinator"},
        )
        combined = readme + instructions
        self.assertNotIn(".co" + "dex", combined)
        self.assertNotIn("config" + ".toml", combined)
        self.assertNotIn("coordinator-" + "architect.toml", combined)
        self.assertNotIn("installed " + "adapter", combined.casefold())

    def test_adaptive_execution_contract(self) -> None:
        protocol = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
        execution = protocol.split("## Execute adaptively\n", 1)[1].split("\n## ", 1)[0]
        execution = " ".join(execution.split())
        for required in (
            "delegation as enabled only when a subagent creation or delegation tool is callable",
            "lowercase SHA-256 digest of the request ID",
            "execute the same packet in the parent",
            "persist `reconcile_required`",
            "only after proving no child exists",
        ):
            self.assertIn(required, execution)

    def test_ci_uses_latest_platforms_with_only_minimum_python(self) -> None:
        workflows = {
            path.name: path.read_text(encoding="utf-8")
            for path in (ROOT / ".github" / "workflows").glob("*.yml")
        }
        self.assertEqual(set(workflows), {"ci.yml"})
        ci = workflows["ci.yml"]
        self.assertIn("os: [ubuntu-latest, macos-latest, windows-latest]", ci)
        self.assertNotIn("matrix.python", ci)
        versions = re.findall(r"python-version:\s*['\"]?([^'\"\s]+)", ci)
        self.assertEqual(set(versions), {"3.11"})
        self.assertIn("ruff check --no-cache src skill/scripts tests", ci)
        self.assertIn("python -m compileall -q src skill/scripts tests", ci)


if __name__ == "__main__":
    unittest.main()
