from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coordinator.cli import routing as routing_cli  # noqa: E402
from coordinator.routing.selector import RoutingError, choose  # noqa: E402


def task(**overrides):
    value = {
        "summary": "Implement a bounded change",
        "stage": "implementation",
        "complexity": 3,
        "ambiguity": 3,
        "criticality": 3,
        "coupling": 3,
        "novelty": 3,
        "determinism": 3,
    }
    value.update(overrides)
    return value


class RoutingTests(unittest.TestCase):
    def test_stage_selects_role_and_ranks_arbitrary_runtime_candidates(self) -> None:
        profile = {
            "candidates": [
                {"model": "vendor/economy@next", "effort": None, "capacity": 3.4, "relative_cost": 1},
                {"model": "vendor/reasoner@next", "effort": "custom-depth", "capacity": 4, "relative_cost": 2},
            ],
            "budget": "quality",
        }
        result = choose(task(stage="review"), profile)
        self.assertEqual(
            result["route"],
            {"role": "reviewer", "model": "vendor/reasoner@next", "effort": "custom-depth"},
        )
        self.assertEqual(len(result["task_digest"]), 64)
        fallback = choose(
            task(stage="architecture", complexity=5, ambiguity=5, criticality=5, coupling=5, novelty=5, determinism=1),
            {
                "candidates": [
                    {"model": "small-runtime-model", "effort": "quick", "capacity": 1, "relative_cost": 0.1}
                ],
                "budget": "value",
            },
        )
        self.assertFalse(fallback["alternatives"][0]["viable"])
        self.assertIn("fallback because no candidate met required capacity", fallback["rationale"])

    def test_missing_runtime_catalog_inherits_parent_route(self) -> None:
        result = choose(task(stage="validation"))
        self.assertEqual(result["route"], {"role": "validator", "model": None, "effort": None})
        self.assertEqual(result["alternatives"], [])
        self.assertIn("inherit the parent model and effort", result["rationale"])

    def test_unknown_and_out_of_range_input_is_rejected(self) -> None:
        with self.assertRaises(RoutingError):
            choose(task(unknown=True))
        with self.assertRaises(RoutingError):
            choose(task(complexity=6))

    def test_router_rejects_duplicate_constants_and_oversized_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            task_path = base / "task.json"
            invalid_documents = (
                '{"summary":"one","summary":"two"}',
                '{"summary":NaN}',
                ' ' * (routing_cli.MAX_JSON_BYTES + 1),
            )
            for document in invalid_documents:
                with self.subTest(prefix=document[:20]):
                    task_path.write_text(document, encoding="utf-8")
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = routing_cli.main(
                            ["choose", "--task-file", str(task_path), "--json"]
                        )
                    payload = json.loads(output.getvalue())
                    self.assertEqual((code, payload["code"]), (2, "invalid_routing_input"))

    def test_malformed_structured_inputs_return_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            task_path = base / "task.json"
            profile_path = base / "profile.json"
            cases = (
                (task(), {"candidates": [{}], "budget": "balanced"}),
                (task(summary="\ud800"), None),
                (task(stage={}), None),
            )
            for task_value, profile_value in cases:
                with self.subTest(profile=profile_value):
                    task_path.write_text(json.dumps(task_value), encoding="utf-8")
                    arguments = ["choose", "--task-file", str(task_path)]
                    if profile_value is not None:
                        profile_path.write_text(json.dumps(profile_value), encoding="utf-8")
                        arguments.extend(("--profile-file", str(profile_path)))
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = routing_cli.main([*arguments, "--json"])
                    self.assertEqual((code, json.loads(output.getvalue())["code"]), (2, "invalid_routing_input"))


if __name__ == "__main__":
    unittest.main()
