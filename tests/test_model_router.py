from __future__ import annotations

import pathlib
import sys
import unittest

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import model_router  # noqa: E402


class ModelRouterTests(unittest.TestCase):
    def test_every_model_effort_pair_exists(self) -> None:
        table = model_router._markdown_table(25_000, 2_000, 0.0)
        self.assertEqual(len(model_router.MODEL_ORDER) * len(model_router.EFFORT_ORDER), 18)
        for model in model_router.MODEL_ORDER:
            for effort in model_router.EFFORT_ORDER:
                self.assertIn(f"| {model} | {effort} |", table)

    def test_official_rate_baseline_and_effort_heuristic_are_separate(self) -> None:
        self.assertEqual(model_router.MODELS["gpt-5.6-sol"]["input"], 5.00)
        self.assertEqual(model_router.MODELS["gpt-5.6-terra"]["output"], 12.00)
        self.assertEqual(model_router.MODELS["gpt-5.6-luna"]["cache_write"], 0.25)
        cost, output_tokens, long_context = model_router.estimate_cost(
            "gpt-5.6-sol", "max", 25_000, 2_000, 0.0, "no"
        )
        self.assertEqual(output_tokens, 5_200)
        self.assertFalse(long_context)
        self.assertAlmostEqual(cost, 0.281, places=9)

    def test_long_context_switch_and_cache_write_cost(self) -> None:
        short, _, short_flag = model_router.estimate_cost(
            "gpt-5.6-terra", "medium", 272_000, 2_000, 0.0, "auto"
        )
        long, _, long_flag = model_router.estimate_cost(
            "gpt-5.6-terra", "medium", 272_001, 2_000, 0.0, "auto"
        )
        self.assertFalse(short_flag)
        self.assertTrue(long_flag)
        self.assertGreater(long, short)
        cache_cost, _, _ = model_router.estimate_cost(
            "gpt-5.6-sol", "medium", 1_000, 0,
            cached_fraction=0.25, cache_write_fraction=0.25, force_long="no"
        )
        self.assertAlmostEqual(cache_cost, 0.0041875, places=10)

    def test_hard_quality_architecture_chooses_sol_max(self) -> None:
        _, candidates = model_router.rank_candidates(
            stage="architecture",
            complexity=5, ambiguity=5, criticality=5,
            coupling=5, novelty=5, determinism=1,
            input_tokens=60_000, base_output_tokens=5_000,
            cached_fraction=0.0, budget_mode="quality", force_long="auto",
        )
        self.assertEqual((candidates[0].model, candidates[0].reasoning_effort), ("gpt-5.6-sol", "max"))
        self.assertTrue(candidates[0].viable)

    def test_simple_deterministic_work_prefers_luna(self) -> None:
        _, candidates = model_router.rank_candidates(
            stage="documentation",
            complexity=1, ambiguity=1, criticality=1,
            coupling=1, novelty=1, determinism=5,
            input_tokens=5_000, base_output_tokens=500,
            cached_fraction=0.0, budget_mode="value", force_long="auto",
        )
        self.assertEqual(candidates[0].model, "gpt-5.6-luna")

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            model_router.estimate_cost(
                "gpt-5.6-luna", "low", 1_000, 100,
                cached_fraction=0.7, cache_write_fraction=0.4,
            )
        with self.assertRaises(ValueError):
            model_router.rank_candidates(
                stage="review", complexity=6, ambiguity=3, criticality=3,
                coupling=3, novelty=3, determinism=3,
                input_tokens=1_000, base_output_tokens=100,
                cached_fraction=0.0, budget_mode="balanced", force_long="auto",
            )


if __name__ == "__main__":
    unittest.main()
