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
    def test_stage_selects_role_and_profile_limits_route(self) -> None:
        result = choose(task(stage="review"), {"allowed_models": ["gpt-5.6-terra"], "allowed_efforts": ["high"], "budget": "quality"})
        self.assertEqual(result["route"], {"role": "reviewer", "model": "gpt-5.6-terra", "effort": "high"})
        self.assertEqual(len(result["task_digest"]), 64)
        fallback = choose(
            task(stage="architecture", complexity=5, ambiguity=5, criticality=5, coupling=5, novelty=5, determinism=1),
            {"allowed_models": ["gpt-5.6-luna"], "allowed_efforts": ["none"], "budget": "value"},
        )
        self.assertFalse(fallback["alternatives"][0]["viable"])
        self.assertIn("fallback because no allowed route met required capacity", fallback["rationale"])

    def test_unknown_and_out_of_range_input_is_rejected(self) -> None:
        with self.assertRaises(RoutingError):
            choose(task(unknown=True))
        with self.assertRaises(RoutingError):
            choose(task(complexity=6))

    def test_malformed_structured_inputs_return_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            task_path = base / "task.json"
            profile_path = base / "profile.json"
            cases = (
                (task(), {"allowed_models": [{}], "allowed_efforts": ["high"], "budget": "balanced"}),
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
