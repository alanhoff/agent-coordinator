from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))

from coordinator.cli import state as state_cli  # noqa: E402
from coordinator.state import store as state_owner  # noqa: E402
from coordinator.state.store import (  # noqa: E402
    MAX_STATE_BYTES,
    StateError,
    StateStore,
    planning_diagnostics,
    ready_nodes,
    validate_state,
)


DIMENSIONS = {
    "breadth": 1,
    "change_surface": 1,
    "coupling": 1,
    "novelty": 1,
    "verification": 1,
}


def specification(
    node_id: str,
    *,
    outputs: list[str] | None = None,
    requirement_ids: list[str] | None = None,
) -> dict:
    return {
        "objective": f"Implement {node_id}",
        "inputs": [],
        "outputs": outputs or [f"{node_id} implementation"],
        "constraints": [],
        "non_goals": [],
        "requirement_ids": requirement_ids or [],
        "open_questions": [],
    }


def assessment_inputs(
    *,
    dimensions: dict[str, int] | None = None,
    ambiguity: int = 0,
    rationale: str = "small bounded leaf",
) -> dict:
    return {
        "dimensions": dict(dimensions or DIMENSIONS),
        "ambiguity": ambiguity,
        "rationale": rationale,
    }


def split_child(
    node_id: str,
    *,
    acceptance: list[str],
    outputs: list[str],
    requirement_ids: list[str],
    dependencies: list[str] | None = None,
    dimensions: dict[str, int] | None = None,
    priority: int = 50,
) -> dict:
    return {
        "id": node_id,
        "title": node_id,
        "stage": "implementation",
        "priority": priority,
        "dependencies": dependencies or [],
        "write_scopes": [f"src/{node_id}"],
        "role": "implementer",
        "model": None,
        "effort": None,
        "acceptance": acceptance,
        "route_rationale": "bounded split child",
        "estimated_cost": None,
        "spec": specification(node_id, outputs=outputs, requirement_ids=requirement_ids),
        "assessment": assessment_inputs(dimensions=dimensions),
    }


class DurableStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.session = self.base / "private" / "session.json"
        self.environment = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}, clear=False
        )
        self.environment.start()
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(self.session))
        created = self.cli(
            "init", "--repo", str(self.repo), "--task", "Coordinate durable work",
            "--session-file", str(self.session), "--mutation-id", "init-001",
        )
        self.workflow_id = created["data"]["workflow_id"]

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def cli(self, *arguments: str, expected: int = 0) -> dict:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = state_cli.main([*arguments, "--json"])
        self.assertEqual(code, expected, output.getvalue())
        return json.loads(output.getvalue())

    def mutation(self, command: str, revision: int, mutation: str, *arguments: str, session: pathlib.Path | None = None, expected: int = 0) -> dict:
        return self.cli(
            command,
            "--workflow-id", self.workflow_id,
            "--session-file", str(session or self.session),
            "--mutation-id", mutation,
            "--expected-revision", str(revision),
            *arguments,
            expected=expected,
        )

    def route(
        self,
        node_id: str,
        revision: int,
        mutation: str,
        *,
        role: str = "implementer",
        session: pathlib.Path | None = None,
        expected: int = 0,
    ) -> dict:
        return self.mutation(
            "node-route", revision, mutation, "--node-id", node_id,
            "--role", role, "--rationale", "fresh route for this launch attempt",
            session=session,
            expected=expected,
        )

    def add(
        self,
        node_id: str,
        revision: int,
        mutation: str,
        *extra: str,
        scope: str | None = None,
        expected: int = 0,
    ) -> dict:
        return self.mutation(
            "node-add", revision, mutation,
            "--node-id", node_id, "--title", node_id, "--stage", "implementation",
            "--write-scope", scope or f"src/{node_id}", "--role", "implementer",
            "--acceptance", "focused test passes", "--rationale", "bounded implementation",
            "--objective", f"Implement {node_id}", "--output", f"{node_id} implementation",
            "--breadth", "1", "--change-surface", "1", "--coupling", "1",
            "--novelty", "1", "--verification", "1", "--ambiguity", "0",
            "--complexity-rationale", "small bounded leaf",
            *extra,
            expected=expected,
        )

    def test_derived_assessment_scoring_policy_bounds_and_refinement_diagnostics(self) -> None:
        self.add("small", 1, "add-small")
        self.add(
            "oversized-total", 2, "add-oversized-total",
            "--breadth", "2", "--change-surface", "2", "--coupling", "2",
            "--novelty", "2", "--verification", "1",
        )
        self.add("oversized-dimension", 3, "add-oversized-dimension", "--breadth", "4")
        self.add("ambiguous", 4, "add-ambiguous", "--ambiguity", "2")
        self.add(
            "questions", 5, "add-questions", "--ambiguity", "1",
            "--open-question", "Which output contract applies?",
        )

        self.add("invalid-score", 6, "add-invalid-score", "--verification", "5", expected=2)
        self.add("invalid-ambiguity", 6, "add-invalid-ambiguity", "--ambiguity", "-1", expected=2)

        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["schema_version"], 4)
        self.assertEqual(
            {
                key: state["conventions"][key]
                for key in (
                    "max_node_complexity",
                    "max_dimension_complexity",
                    "max_node_ambiguity",
                    "max_refinement_depth",
                )
            },
            {
                "max_node_complexity": 8,
                "max_dimension_complexity": 3,
                "max_node_ambiguity": 1,
                "max_refinement_depth": 8,
            },
        )
        self.assertEqual(
            {
                node_id: (node["assessment"]["total"], node["assessment"]["state"])
                for node_id, node in state["nodes"].items()
            },
            {
                "small": (5, "executable"),
                "oversized-total": (9, "split_required"),
                "oversized-dimension": (8, "split_required"),
                "ambiguous": (5, "refinement_required"),
                "questions": (5, "refinement_required"),
            },
        )
        for node in state["nodes"].values():
            self.assertEqual(node["assessment"]["rubric_version"], 1)
            self.assertRegex(node["assessment"]["input_digest"], r"^[0-9a-f]{64}$")

        diagnostics = planning_diagnostics(state)
        self.assertEqual(diagnostics["over_budget_nodes"], ["oversized-dimension", "oversized-total"])
        self.assertEqual(diagnostics["ambiguous_nodes"], ["ambiguous", "questions"])
        self.assertEqual(diagnostics["refinement_required_nodes"], ["ambiguous", "questions"])
        self.assertEqual(diagnostics["dispatch_order"], [])

        for field, invalid in (
            ("max_node_complexity", 21),
            ("max_dimension_complexity", 5),
            ("max_node_ambiguity", 5),
            ("max_refinement_depth", 0),
        ):
            with self.subTest(policy=field):
                malformed = copy.deepcopy(state)
                malformed["conventions"][field] = invalid
                with self.assertRaisesRegex(StateError, re.escape(field)):
                    validate_state(malformed)

        forged_total = copy.deepcopy(state)
        forged_total["nodes"]["small"]["assessment"]["total"] = 6
        with self.assertRaisesRegex(StateError, r"assessment\.total"):
            validate_state(forged_total)
        forged_state = copy.deepcopy(state)
        forged_state["nodes"]["small"]["assessment"]["state"] = "split_required"
        with self.assertRaisesRegex(StateError, "not derived"):
            validate_state(forged_state)

    def test_refinement_replaces_exact_inputs_and_recovers_assessment_to_executable(self) -> None:
        self.add(
            "draft", 1, "add-draft", "--ambiguity", "3",
            "--open-question", "Which public result is required?",
        )
        original = StateStore().load(self.workflow_id)["nodes"]["draft"]
        refinement = {
            "spec": {
                "objective": "Produce the decided public result",
                "inputs": ["Approved decision"],
                "outputs": ["Stable public result"],
                "constraints": ["Preserve existing recovery behavior"],
                "non_goals": ["Redesign unrelated commands"],
                "requirement_ids": [],
                "open_questions": [],
            },
            "acceptance": ["Focused public-result test passes"],
            "write_scopes": ["src/draft-refined"],
            "assessment": assessment_inputs(
                dimensions={**DIMENSIONS, "breadth": 0},
                rationale="Decision removes breadth and ambiguity",
            ),
        }
        malformed_payloads = []
        extra_top_level = {**refinement, "total": 4}
        malformed_payloads.append(("top", extra_top_level))
        extra_nested_field = copy.deepcopy(refinement)
        extra_nested_field["assessment"]["total"] = 4
        malformed_payloads.append(("nested", extra_nested_field))
        for location, malformed in malformed_payloads:
            self.mutation(
                "node-refine", 2, f"refine-extra-{location}-field", "--node-id", "draft",
                "--refinement-json", json.dumps(malformed), expected=2,
            )
        self.assertEqual(StateStore().load(self.workflow_id)["revision"], 2)

        refinement_path = self.base / "refinement.json"
        refinement_path.write_text(json.dumps(refinement), encoding="utf-8")
        self.mutation(
            "node-refine", 2, "refine-draft", "--node-id", "draft",
            "--refinement-file", str(refinement_path),
        )
        node = StateStore().load(self.workflow_id)["nodes"]["draft"]
        self.assertEqual(node["spec"], refinement["spec"])
        self.assertEqual(node["acceptance"], refinement["acceptance"])
        self.assertEqual(node["write_scopes"], refinement["write_scopes"])
        self.assertEqual(node["assessment"]["dimensions"], refinement["assessment"]["dimensions"])
        self.assertEqual((node["assessment"]["total"], node["assessment"]["ambiguity"]), (4, 0))
        self.assertEqual(node["assessment"]["state"], "executable")
        self.assertNotEqual(node["assessment"]["input_digest"], original["assessment"]["input_digest"])
        self.assertEqual(node["lineage"], original["lineage"])

        self.mutation(
            "node-route", 3, "route-refined", "--node-id", "draft", "--role", "implementer",
            "--rationale", "refined packet is executable",
        )
        self.mutation("node-update", 4, "ready-draft", "--node-id", "draft", "--status", "ready")
        self.mutation(
            "node-update", 5, "claim-refined", "--node-id", "draft",
            "--launch-state", "claimed", "--request-id", "request-refined",
        )
        self.mutation(
            "node-refine", 6, "refine-active", "--node-id", "draft",
            "--refinement-json", json.dumps(refinement), expected=2,
        )
        active = StateStore().load(self.workflow_id)["nodes"]["draft"]
        self.assertEqual((active["launch"]["state"], active["assessment"]["state"]), ("claimed", "executable"))

        self.mutation(
            "node-update", 6, "bind-draft", "--node-id", "draft",
            "--launch-state", "bound", "--child-id", "child-draft",
        )
        self.mutation("node-update", 7, "run-draft", "--node-id", "draft", "--status", "running")
        self.mutation(
            "node-update", 8, "fail-draft", "--node-id", "draft", "--status", "failed",
            "--result", "partial draft result", "--evidence", "focused failure log",
            "--attempt-outcome", "focused test failed",
        )
        failed_attempts = copy.deepcopy(StateStore().load(self.workflow_id)["nodes"]["draft"]["attempts"])
        self.add("draft-dependent", 9, "add-draft-dependent", "--dependency", "draft")
        dependent_refinement = {
            "spec": specification("draft-dependent"),
            "acceptance": ["focused test passes"],
            "write_scopes": ["src/draft-dependent"],
            "assessment": assessment_inputs(rationale="Assessed against the recorded failure evidence"),
        }
        self.mutation(
            "node-refine", 10, "reassess-draft-dependent", "--node-id", "draft-dependent",
            "--refinement-json", json.dumps(dependent_refinement),
        )
        dependent_before_retry = StateStore().load(self.workflow_id)["nodes"]["draft-dependent"]["assessment"]
        self.assertEqual(dependent_before_retry["state"], "executable")
        retry_refinement = copy.deepcopy(refinement)
        retry_refinement["spec"]["inputs"].append("Failed attempt evidence")
        retry_refinement["assessment"] = assessment_inputs(
            dimensions={**DIMENSIONS, "breadth": 0, "novelty": 0},
            rationale="Failure evidence narrows the retry",
        )
        self.mutation(
            "node-refine", 11, "refine-failed-draft", "--node-id", "draft",
            "--refinement-json", json.dumps(retry_refinement),
        )
        reset_state = StateStore().load(self.workflow_id)
        failed = reset_state["nodes"]["draft"]
        self.assertEqual((failed["status"], failed["launch"]["state"]), ("pending", "unclaimed"))
        self.assertEqual(failed["attempts"], failed_attempts)
        self.assertEqual((failed["assessment"]["total"], failed["assessment"]["state"]), (3, "executable"))
        dependent_after_retry = reset_state["nodes"]["draft-dependent"]["assessment"]
        self.assertEqual(dependent_after_retry["state"], "stale")
        self.assertEqual(dependent_after_retry["input_digest"], dependent_before_retry["input_digest"])
        self.mutation(
            "block", 12, "defer-stale-draft-dependent", "--node-id", "draft-dependent",
            "--reason", "retry invalidated its evidence", "--needed", "reassess after retry",
        )
        retry_baseline = StateStore().load(self.workflow_id)
        rejected_claim = self.mutation(
            "node-update", 13, "claim-with-stale-route", "--node-id", "draft",
            "--launch-state", "claimed", "--request-id", "request-stale-route", expected=2,
        )
        self.assertIn("persist a fresh node-route", rejected_claim["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), retry_baseline)
        self.route("draft", 13, "route-failed-refinement", role="fixer")
        self.mutation(
            "node-update", 14, "claim-failed-refinement", "--node-id", "draft",
            "--launch-state", "claimed", "--request-id", "request-refined-retry",
        )
        retried = StateStore().load(self.workflow_id)["nodes"]["draft"]
        self.assertEqual([attempt["number"] for attempt in retried["attempts"]], [1, 2])
        self.assertEqual(retried["attempts"][0]["outcome"], "focused test failed")

    def test_requirement_and_dependency_evidence_make_only_affected_assessments_stale(self) -> None:
        self.mutation(
            "requirement-set", 1, "requirement-active", "--requirement-id", "req-public",
            "--text", "Expose the public result", "--source", "task", "--status", "active",
        )
        self.add("requirement-work", 2, "add-requirement-work", "--requirement-id", "req-public")
        self.add("unrelated", 3, "add-unrelated")
        self.add("dependency", 4, "add-dependency")
        self.add("direct-dependent", 5, "add-direct-dependent", "--dependency", "dependency")
        self.add("transitive-dependent", 6, "add-transitive", "--dependency", "direct-dependent")
        self.add("upstream", 7, "add-upstream", "--requirement-id", "req-public")
        self.add("output-dependent", 8, "add-output-dependent", "--dependency", "upstream")
        before = StateStore().load(self.workflow_id)
        requirement_digest = before["nodes"]["requirement-work"]["assessment"]["input_digest"]
        dependency_digest = before["nodes"]["direct-dependent"]["assessment"]["input_digest"]
        output_digest = before["nodes"]["output-dependent"]["assessment"]["input_digest"]

        upstream_refinement = {
            "spec": {**specification("upstream"), "outputs": ["upstream v2 artifact"]},
            "acceptance": ["upstream revised acceptance"],
            "write_scopes": ["src/upstream"],
            "assessment": assessment_inputs(rationale="Declared output is now precise"),
        }
        self.mutation(
            "node-refine", 9, "refine-upstream-output", "--node-id", "upstream",
            "--refinement-json", json.dumps(upstream_refinement),
        )
        output_changed = StateStore().load(self.workflow_id)
        self.assertEqual(output_changed["nodes"]["output-dependent"]["assessment"]["state"], "stale")
        self.assertEqual(output_changed["nodes"]["output-dependent"]["assessment"]["input_digest"], output_digest)
        dependent_refinement = {
            "spec": specification("output-dependent"),
            "acceptance": ["focused test passes"],
            "write_scopes": ["src/output-dependent"],
            "assessment": assessment_inputs(rationale="Reassessed against the renamed upstream output"),
        }
        self.mutation(
            "node-refine", 10, "reassess-output-dependent", "--node-id", "output-dependent",
            "--refinement-json", json.dumps(dependent_refinement),
        )
        reassessed = StateStore().load(self.workflow_id)["nodes"]["output-dependent"]["assessment"]
        self.assertEqual(reassessed["state"], "executable")
        self.assertNotEqual(reassessed["input_digest"], output_digest)
        self.assertEqual(
            (
                StateStore().load(self.workflow_id)["nodes"]["upstream"]["spec"]["requirement_ids"],
                StateStore().load(self.workflow_id)["nodes"]["upstream"]["spec"]["outputs"],
                StateStore().load(self.workflow_id)["nodes"]["upstream"]["acceptance"],
            ),
            ([], ["upstream v2 artifact"], ["upstream revised acceptance"]),
        )

        self.mutation("node-update", 11, "skip-upstream", "--node-id", "upstream", "--status", "skipped")
        skipped = StateStore().load(self.workflow_id)
        self.assertEqual(
            (skipped["nodes"]["upstream"]["result"], skipped["nodes"]["upstream"]["evidence"]),
            (None, None),
        )
        self.assertEqual(skipped["nodes"]["output-dependent"]["assessment"]["state"], "stale")
        self.mutation(
            "node-refine", 12, "reassess-after-upstream-disposition", "--node-id", "output-dependent",
            "--refinement-json", json.dumps(dependent_refinement),
        )
        disposition_reassessed = StateStore().load(self.workflow_id)
        self.assertEqual(
            disposition_reassessed["nodes"]["output-dependent"]["assessment"]["state"],
            "executable",
        )
        self.assertIn("output-dependent", ready_nodes(disposition_reassessed))

        self.mutation(
            "requirement-set", 13, "requirement-satisfied", "--requirement-id", "req-public",
            "--text", "Expose the public result", "--source", "task", "--status", "satisfied",
            "--evidence", "public contract approved",
        )
        self.mutation(
            "block", 14, "defer-stale-requirement-work", "--node-id", "requirement-work",
            "--reason", "requirement accounting changed", "--needed", "reassess declared work",
        )
        self.route("dependency", 15, "route-dependency")
        self.mutation("node-update", 16, "ready-dependency", "--node-id", "dependency", "--status", "ready")
        self.assertEqual(
            (
                StateStore().load(self.workflow_id)["nodes"]["direct-dependent"]["assessment"]["input_digest"],
                StateStore().load(self.workflow_id)["nodes"]["direct-dependent"]["assessment"]["state"],
            ),
            (dependency_digest, "executable"),
        )
        self.mutation(
            "node-update", 17, "claim-dependency", "--node-id", "dependency",
            "--launch-state", "claimed", "--request-id", "request-dependency",
        )
        self.mutation(
            "node-update", 18, "bind-dependency", "--node-id", "dependency",
            "--launch-state", "bound", "--child-id", "child-dependency",
        )
        self.mutation("node-update", 19, "run-dependency", "--node-id", "dependency", "--status", "running")
        self.assertEqual(
            (
                StateStore().load(self.workflow_id)["nodes"]["direct-dependent"]["assessment"]["input_digest"],
                StateStore().load(self.workflow_id)["nodes"]["direct-dependent"]["assessment"]["state"],
            ),
            (dependency_digest, "executable"),
        )
        self.mutation(
            "node-update", 20, "finish-dependency", "--node-id", "dependency", "--status", "done",
            "--result", "dependency implemented", "--evidence", "dependency tests passed",
        )

        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["requirement-work"]["assessment"]["state"], "stale")
        self.assertEqual(state["nodes"]["direct-dependent"]["assessment"]["state"], "stale")
        self.assertEqual(state["nodes"]["output-dependent"]["assessment"]["state"], "executable")
        self.assertEqual(state["nodes"]["transitive-dependent"]["assessment"]["state"], "executable")
        self.assertEqual(state["nodes"]["unrelated"]["assessment"]["state"], "executable")
        self.assertEqual(
            (state["nodes"]["dependency"]["result"], state["nodes"]["dependency"]["evidence"]),
            ("dependency implemented", "dependency tests passed"),
        )
        self.assertEqual(state["nodes"]["requirement-work"]["assessment"]["input_digest"], requirement_digest)
        self.assertEqual(state["nodes"]["direct-dependent"]["assessment"]["input_digest"], dependency_digest)
        self.assertEqual(
            planning_diagnostics(state)["stale_nodes"],
            ["direct-dependent", "requirement-work"],
        )
        self.assertEqual(ready_nodes(state), [])

    def test_critical_path_dispatch_order_and_capacity_planning_diagnostics(self) -> None:
        complex_args = (
            "--breadth", "2", "--change-surface", "2", "--coupling", "2",
            "--novelty", "1", "--verification", "1",
        )
        self.add("long-root", 1, "add-long-root", *complex_args, "--priority", "1")
        self.add("long-middle", 2, "add-long-middle", *complex_args, "--dependency", "long-root")
        self.add("long-tail", 3, "add-long-tail", *complex_args, "--dependency", "long-middle")
        self.add("high-priority", 4, "add-high-priority", "--priority", "100")
        self.add("alpha", 5, "add-alpha", "--priority", "50")
        self.add("beta", 6, "add-beta", "--priority", "50")

        response = self.cli("graph-validate", "--workflow-id", self.workflow_id)["data"]
        self.assertEqual(
            set(response),
            {
                "missing_dependencies",
                "cycles",
                "write_scope_collisions",
                "over_budget_nodes",
                "ambiguous_nodes",
                "refinement_required_nodes",
                "stale_nodes",
                "decomposed_nodes",
                "frontier_width",
                "usable_parallelism",
                "available_parallelism",
                "critical_path_load",
                "dispatch_order",
                "ready_nodes",
            },
        )
        self.assertEqual(
            response["critical_path_load"],
            {
                "alpha": 5,
                "beta": 5,
                "high-priority": 5,
                "long-middle": 16,
                "long-root": 24,
                "long-tail": 8,
            },
        )
        expected_order = ["long-root", "high-priority", "alpha", "beta"]
        self.assertEqual(response["dispatch_order"], expected_order)
        self.assertEqual(response["ready_nodes"], expected_order)
        self.assertEqual(
            (response["frontier_width"], response["usable_parallelism"], response["available_parallelism"]),
            (4, 3, 3),
        )

        self.route("long-root", 7, "route-long-root")
        self.mutation(
            "node-update", 8, "claim-long-root", "--node-id", "long-root",
            "--launch-state", "claimed", "--request-id", "request-long-root",
        )
        diagnostics = planning_diagnostics(StateStore().load(self.workflow_id))
        self.assertEqual(diagnostics["dispatch_order"], ["high-priority", "alpha", "beta"])
        self.assertEqual(
            (diagnostics["frontier_width"], diagnostics["usable_parallelism"], diagnostics["available_parallelism"]),
            (3, 3, 2),
        )

    def test_route_and_claim_reject_every_non_executable_assessment_state(self) -> None:
        self.mutation(
            "requirement-set", 1, "add-route-requirement", "--requirement-id", "req-route",
            "--text", "Route only current work", "--source", "task", "--status", "active",
        )
        self.add("stale", 2, "add-stale", "--requirement-id", "req-route")
        self.add("split-required", 3, "add-split-required", "--breadth", "4")
        self.add("refinement-required", 4, "add-refinement-required", "--ambiguity", "2")
        self.add("decomposed", 5, "add-decomposed", "--breadth", "4")
        self.mutation(
            "requirement-set", 6, "change-route-requirement", "--requirement-id", "req-route",
            "--text", "Route only current work", "--source", "task", "--status", "satisfied",
            "--evidence", "routing rule approved",
        )
        lower = {**DIMENSIONS, "breadth": 0}
        split = {
            "parent_id": "decomposed",
            "reason": "Separate artifact ownership from proof",
            "children": [
                split_child(
                    "decomposed-artifact",
                    acceptance=["artifact exists"],
                    outputs=["bounded artifact"],
                    requirement_ids=[],
                    dimensions=lower,
                ),
                split_child(
                    "decomposed-proof",
                    acceptance=["proof passes"],
                    outputs=["focused proof"],
                    requirement_ids=[],
                    dimensions=lower,
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"decomposed implementation": ["decomposed-artifact"]},
                "acceptance": {"focused test passes": ["decomposed-proof"]},
            },
            "dependent_replacements": {},
        }
        self.mutation("node-split", 7, "split-decomposed", "--plan-json", json.dumps(split))
        self.add("independent", 8, "add-independent")

        state = StateStore().load(self.workflow_id)
        expected_states = {
            "split-required": "split_required",
            "refinement-required": "refinement_required",
            "stale": "stale",
            "decomposed": "decomposed",
        }
        self.assertEqual(
            {node_id: state["nodes"][node_id]["assessment"]["state"] for node_id in expected_states},
            expected_states,
        )
        for index, arguments in enumerate((
            ("node-update", "--status", "ready"),
            (
                "node-route", "--role", "implementer", "--rationale",
                "global fixed point must reject this route",
            ),
            ("node-update", "--launch-state", "claimed", "--request-id", "request-global-fixed-point"),
        ), start=1):
            self.mutation(
                arguments[0], 9, f"reject-global-fixed-point-{index}",
                "--node-id", "independent", *arguments[1:], expected=2,
            )
        for index, node_id in enumerate(expected_states, start=1):
            if node_id != "decomposed":
                readied = self.mutation(
                    "node-update", 9, f"reject-ready-{index}", "--node-id", node_id,
                    "--status", "ready", expected=2,
                )
                self.assertIn("executable assessment", readied["data"]["message"])
            routed = self.mutation(
                "node-route", 9, f"reject-route-{index}", "--node-id", node_id,
                "--role", "implementer", "--rationale", "must remain rejected", expected=2,
            )
            self.assertIn("executable", routed["data"]["message"])
            claimed = self.mutation(
                "node-update", 9, f"reject-claim-{index}", "--node-id", node_id,
                "--launch-state", "claimed", "--request-id", f"request-rejected-{index}", expected=2,
            )
            self.assertIn("workflow planning fixed point", claimed["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), state)

        for revision, node_id in enumerate(("stale", "split-required", "refinement-required"), start=9):
            self.mutation(
                "block", revision, f"block-unresolved-{node_id}", "--node-id", node_id,
                "--reason", "deliberately deferred planning", "--needed", "explicit later repair",
            )
        blocked = StateStore().load(self.workflow_id)
        self.assertIn("independent", ready_nodes(blocked))
        self.assertIn("independent", planning_diagnostics(blocked)["dispatch_order"])

        bounded_split = {
            "parent_id": "split-required",
            "reason": "Resolve deferred decomposition into bounded leaves",
            "children": [
                split_child(
                    "split-bounded-a",
                    acceptance=["bounded A proof"],
                    outputs=["bounded A artifact"],
                    requirement_ids=[],
                    dimensions=lower,
                ),
                split_child(
                    "split-bounded-b",
                    acceptance=["bounded B proof"],
                    outputs=["bounded B artifact"],
                    requirement_ids=[],
                    dimensions=lower,
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"split-required implementation": ["split-bounded-a"]},
                "acceptance": {"focused test passes": ["split-bounded-b"]},
            },
            "dependent_replacements": {},
        }
        self.mutation("node-split", 12, "repair-split-required", "--plan-json", json.dumps(bounded_split))
        repaired = StateStore().load(self.workflow_id)
        self.assertEqual(
            {repaired["nodes"][node_id]["assessment"]["state"] for node_id in (
                "split-bounded-a", "split-bounded-b",
            )},
            {"executable"},
        )
        self.assertTrue(
            {"independent", "split-bounded-a", "split-bounded-b"}
            <= set(planning_diagnostics(repaired)["dispatch_order"])
        )

    def test_node_split_is_atomic_and_preserves_coverage_rewiring_lineage_and_failed_attempt(self) -> None:
        self.mutation(
            "requirement-set", 1, "add-req-a", "--requirement-id", "req-a",
            "--text", "Produce artifact A", "--source", "task", "--status", "active",
        )
        self.mutation(
            "requirement-set", 2, "add-req-b", "--requirement-id", "req-b",
            "--text", "Produce artifact B", "--source", "task", "--status", "active",
        )
        self.add(
            "parent", 3, "add-parent",
            "--requirement-id", "req-a", "--requirement-id", "req-b",
            "--output", "public API artifact", "--acceptance", "integration passes",
            "--breadth", "2", "--change-surface", "2", "--coupling", "2",
            "--novelty", "1", "--verification", "1",
        )
        self.add("dependent", 4, "add-dependent", "--dependency", "parent")
        self.mutation("node-update", 5, "ready-parent", "--node-id", "parent", "--status", "ready")
        self.route("parent", 6, "route-parent")
        self.mutation(
            "node-update", 7, "claim-parent", "--node-id", "parent",
            "--launch-state", "claimed", "--request-id", "request-parent",
        )
        self.mutation(
            "node-update", 8, "bind-parent", "--node-id", "parent",
            "--launch-state", "bound", "--child-id", "executor-parent",
        )
        self.mutation("node-update", 9, "run-parent", "--node-id", "parent", "--status", "running")
        self.mutation(
            "node-update", 10, "fail-parent", "--node-id", "parent", "--status", "failed",
            "--attempt-outcome", "tests failed",
        )
        failed_attempt = copy.deepcopy(StateStore().load(self.workflow_id)["nodes"]["parent"]["attempts"])
        lower = {**DIMENSIONS, "breadth": 0}
        recursive = {name: (4 if name == "breadth" else 0) for name in DIMENSIONS}
        split = {
            "parent_id": "parent",
            "reason": "Separate the two requirement and proof surfaces",
            "children": [
                split_child(
                    "child-a",
                    acceptance=["child A proof passes"],
                    outputs=["child A artifact"],
                    requirement_ids=["req-a"],
                    dimensions=recursive,
                    priority=70,
                ),
                split_child(
                    "child-b",
                    acceptance=["child B integration passes"],
                    outputs=["child B artifact"],
                    requirement_ids=["req-b"],
                    dimensions=lower,
                    priority=60,
                ),
            ],
            "coverage": {
                "requirements": {"req-a": ["child-a"], "req-b": ["child-b"]},
                "outputs": {
                    "parent implementation": ["child-a"],
                    "public API artifact": ["child-b"],
                },
                "acceptance": {
                    "focused test passes": ["child-a"],
                    "integration passes": ["child-b"],
                },
            },
            "dependent_replacements": {"dependent": ["child-a", "child-b"]},
        }
        split_path = self.base / "split.json"
        split_path.write_text(json.dumps(split), encoding="utf-8")
        self.mutation("node-split", 11, "split-failed-parent", "--plan-file", str(split_path))

        state = StateStore().load(self.workflow_id)
        replay = self.mutation("node-split", 11, "split-failed-parent", "--plan-file", str(split_path))
        self.assertEqual(replay["code"], "mutation_reconciled")
        self.assertEqual(StateStore().load(self.workflow_id), state)
        parent = state["nodes"]["parent"]
        self.assertEqual(parent["attempts"], failed_attempt)
        self.assertEqual(parent["attempts"][0]["outcome"], "tests failed")
        self.assertEqual((parent["status"], parent["launch"]["state"]), ("skipped", "terminal"))
        self.assertEqual(parent["assessment"]["state"], "decomposed")
        self.assertEqual(
            parent["lineage"],
            {
                "parent_id": None,
                "depth": 0,
                "child_ids": ["child-a", "child-b"],
                "split_reason": split["reason"],
                "obligations": {"requirements": [], "outputs": [], "acceptance": []},
            },
        )
        self.assertEqual(state["nodes"]["dependent"]["dependencies"], ["child-a", "child-b"])
        self.assertEqual(state["nodes"]["dependent"]["assessment"]["state"], "stale")
        child_obligations = {
            "child-a": {
                "requirements": ["req-a"],
                "outputs": ["parent implementation"],
                "acceptance": ["focused test passes"],
            },
            "child-b": {
                "requirements": ["req-b"],
                "outputs": ["public API artifact"],
                "acceptance": ["integration passes"],
            },
        }
        for node_id in ("child-a", "child-b"):
            child = state["nodes"][node_id]
            self.assertEqual(
                child["lineage"],
                {
                    "parent_id": "parent",
                    "depth": 1,
                    "child_ids": [],
                    "split_reason": None,
                    "obligations": child_obligations[node_id],
                },
            )
        self.assertEqual((state["nodes"]["child-a"]["assessment"]["total"], state["nodes"]["child-a"]["assessment"]["state"]), (4, "split_required"))
        self.assertEqual((state["nodes"]["child-b"]["assessment"]["total"], state["nodes"]["child-b"]["assessment"]["state"]), (4, "executable"))
        self.assertEqual(ready_nodes(state), [])

        recursive_lower = {**lower, "change_surface": 0}
        recursive_split = {
            "parent_id": "child-a",
            "reason": "Carry inherited requirement, output, and acceptance obligations to a bounded leaf",
            "children": [
                split_child(
                    "grandchild-a",
                    acceptance=["replacement native acceptance"],
                    outputs=["replacement native output"],
                    requirement_ids=[],
                    dimensions=recursive_lower,
                ),
                split_child(
                    "grandchild-proof",
                    acceptance=["recursive proof passes"],
                    outputs=["recursive proof artifact"],
                    requirement_ids=[],
                    dimensions=recursive_lower,
                ),
            ],
            "coverage": {
                "requirements": {"req-a": ["grandchild-a"]},
                "outputs": {
                    "child A artifact": ["grandchild-a"],
                    "parent implementation": ["grandchild-a"],
                },
                "acceptance": {
                    "child A proof passes": ["grandchild-proof"],
                    "focused test passes": ["grandchild-a"],
                },
            },
            "dependent_replacements": {"dependent": ["grandchild-a"]},
        }
        incomplete_recursive_split = copy.deepcopy(recursive_split)
        del incomplete_recursive_split["coverage"]["outputs"]["parent implementation"]
        recursive_baseline = StateStore().load(self.workflow_id)
        rejected_recursive = self.mutation(
            "node-split", 12, "reject-lost-inherited-output",
            "--plan-json", json.dumps(incomplete_recursive_split), expected=2,
        )
        self.assertIn("does not exactly cover", rejected_recursive["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), recursive_baseline)
        self.mutation("node-split", 12, "split-recursive-child", "--plan-json", json.dumps(recursive_split))
        state = StateStore().load(self.workflow_id)
        grandchild = state["nodes"]["grandchild-a"]
        self.assertEqual(grandchild["lineage"]["depth"], 2)
        self.assertEqual(
            grandchild["lineage"]["obligations"],
            {
                "requirements": ["req-a"],
                "outputs": ["child A artifact", "parent implementation"],
                "acceptance": ["focused test passes"],
            },
        )
        self.assertEqual(
            state["nodes"]["grandchild-proof"]["lineage"]["obligations"]["acceptance"],
            ["child A proof passes"],
        )
        self.assertEqual(state["nodes"]["dependent"]["dependencies"], ["grandchild-a", "child-b"])

        carried = copy.deepcopy(grandchild["lineage"]["obligations"])
        carried_digest = grandchild["assessment"]["input_digest"]
        descendant_refinement = {
            "spec": {
                **specification("grandchild-a"),
                "outputs": ["descendant refined output"],
                "requirement_ids": [],
            },
            "acceptance": ["descendant refined acceptance"],
            "write_scopes": ["src/grandchild-a-refined"],
            "assessment": assessment_inputs(
                dimensions=recursive_lower,
                rationale="Replace only native descendant fields",
            ),
        }
        self.mutation(
            "node-refine", 13, "refine-obligated-descendant", "--node-id", "grandchild-a",
            "--refinement-json", json.dumps(descendant_refinement),
        )
        refined_descendant = StateStore().load(self.workflow_id)["nodes"]["grandchild-a"]
        self.assertEqual(refined_descendant["lineage"]["obligations"], carried)
        self.assertEqual(refined_descendant["spec"]["outputs"], ["descendant refined output"])
        self.assertNotEqual(refined_descendant["assessment"]["input_digest"], carried_digest)
        refined_digest = refined_descendant["assessment"]["input_digest"]
        self.mutation(
            "requirement-set", 14, "satisfy-carried-requirement", "--requirement-id", "req-a",
            "--text", "Produce artifact A", "--source", "task", "--status", "satisfied",
            "--evidence", "carried requirement approved",
        )
        stale_descendant = StateStore().load(self.workflow_id)["nodes"]["grandchild-a"]
        self.assertEqual(stale_descendant["spec"]["requirement_ids"], [])
        self.assertEqual(stale_descendant["assessment"]["state"], "stale")
        self.assertEqual(stale_descendant["assessment"]["input_digest"], refined_digest)
        self.mutation(
            "node-refine", 15, "reassess-carried-requirement", "--node-id", "grandchild-a",
            "--refinement-json", json.dumps(descendant_refinement),
        )

        escape_baseline = StateStore().load(self.workflow_id)
        escaped = self.mutation(
            "node-update", 16, "reject-obligation-skip", "--node-id", "grandchild-proof",
            "--status", "skipped", expected=2,
        )
        self.assertIn("cannot discard carried obligations", escaped["data"]["message"])
        remove_plan = {
            "reason": "A carried acceptance obligation cannot disappear",
            "operations": [{"op": "remove", "node_id": "grandchild-proof"}],
        }
        removed = self.mutation(
            "graph-replan", 16, "reject-obligation-remove", "--plan-json", json.dumps(remove_plan),
            expected=2,
        )
        self.assertIn("cannot remove a node with carried obligations", removed["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), escape_baseline)

        nonrewritable_supersede = {
            "reason": "A decomposed parent cannot receive transferred obligations",
            "operations": [{"op": "supersede", "node_id": "grandchild-a", "replacement": "child-a"}],
        }
        rejected_replacement = self.mutation(
            "graph-replan", 16, "reject-nonrewritable-obligation-replacement",
            "--plan-json", json.dumps(nonrewritable_supersede), expected=2,
        )
        self.assertIn("supersede replacement requires", rejected_replacement["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), escape_baseline)

        self.add("obligation-replacement", 16, "add-obligation-replacement")
        supersede_plan = {
            "reason": "Transfer every carried obligation to rewritable replacement work",
            "operations": [{
                "op": "supersede",
                "node_id": "grandchild-a",
                "replacement": "obligation-replacement",
            }],
        }
        self.mutation(
            "graph-replan", 17, "transfer-carried-obligations",
            "--plan-json", json.dumps(supersede_plan),
        )
        transferred = StateStore().load(self.workflow_id)
        self.assertEqual(
            transferred["nodes"]["obligation-replacement"]["lineage"]["obligations"],
            carried,
        )
        self.assertEqual(
            transferred["nodes"]["obligation-replacement"]["assessment"]["state"],
            "stale",
        )
        self.assertEqual(
            transferred["nodes"]["dependent"]["dependencies"],
            ["obligation-replacement", "child-b"],
        )

        self.add(
            "bad-parent", 18, "add-bad-parent",
            "--breadth", "2", "--change-surface", "2", "--coupling", "2",
            "--novelty", "2", "--verification", "1",
        )
        self.add("bad-dependent", 19, "add-bad-dependent", "--dependency", "bad-parent")
        valid_bad_split = {
            "parent_id": "bad-parent",
            "reason": "Valid shape used to isolate rejected invariants",
            "children": [
                split_child(
                    "bad-child-a",
                    acceptance=["bad A proof"],
                    outputs=["bad-parent implementation"],
                    requirement_ids=[],
                ),
                split_child(
                    "bad-child-b",
                    acceptance=["focused test passes"],
                    outputs=["bad B artifact"],
                    requirement_ids=[],
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"bad-parent implementation": ["bad-child-a"]},
                "acceptance": {"focused test passes": ["bad-child-b"]},
            },
            "dependent_replacements": {"bad-dependent": ["bad-child-a", "bad-child-b"]},
        }
        malformed_plans = []
        missing_coverage = copy.deepcopy(valid_bad_split)
        missing_coverage["coverage"]["acceptance"] = {}
        malformed_plans.append(("missing-coverage", missing_coverage, "coverage"))
        missing_replacement = copy.deepcopy(valid_bad_split)
        missing_replacement["dependent_replacements"] = {}
        malformed_plans.append(("missing-dependent-replacement", missing_replacement, "dependent_replacements"))
        no_progress = copy.deepcopy(valid_bad_split)
        no_progress["children"][0]["assessment"]["dimensions"] = {
            "breadth": 2,
            "change_surface": 2,
            "coupling": 2,
            "novelty": 2,
            "verification": 1,
        }
        malformed_plans.append(("no-progress", no_progress, "lower total complexity"))
        legacy_child_key = copy.deepcopy(valid_bad_split)
        legacy_child_key["children"][0]["rationale"] = legacy_child_key["children"][0].pop("route_rationale")
        malformed_plans.append(("legacy-child-key", legacy_child_key, "route_rationale"))

        baseline = StateStore().load(self.workflow_id)
        for index, (name, plan, expected_message) in enumerate(malformed_plans, start=1):
            with self.subTest(plan=name):
                rejected = self.mutation(
                    "node-split", 20, f"reject-split-{index}",
                    "--plan-json", json.dumps(plan), expected=2,
                )
                self.assertIn(expected_message, rejected["data"]["message"])
                self.assertEqual(StateStore().load(self.workflow_id), baseline)

        self.mutation(
            "block", 20, "defer-stale-split-dependent", "--node-id", "dependent",
            "--reason", "recursive rewiring changed dependencies", "--needed", "reassess dependent",
        )
        self.mutation(
            "block", 21, "defer-bad-split-parent", "--node-id", "bad-parent",
            "--reason", "fixture remains intentionally oversized", "--needed", "valid future split plan",
        )
        self.mutation(
            "block", 22, "defer-stale-obligation-replacement", "--node-id", "obligation-replacement",
            "--reason", "supersede transferred new accounting", "--needed", "reassess replacement",
        )
        self.mutation(
            "requirement-set", 23, "add-stale-split-requirement", "--requirement-id", "req-stale-split",
            "--text", "Keep split accounting current", "--source", "task", "--status", "active",
        )
        self.add("stale-failed-parent", 24, "add-stale-failed-parent", "--requirement-id", "req-stale-split")
        self.route("stale-failed-parent", 25, "route-stale-failed-parent")
        self.mutation(
            "node-update", 26, "ready-stale-failed-parent", "--node-id", "stale-failed-parent",
            "--status", "ready",
        )
        self.mutation(
            "node-update", 27, "claim-stale-failed-parent", "--node-id", "stale-failed-parent",
            "--launch-state", "claimed", "--request-id", "request-stale-failed-parent",
        )
        self.mutation(
            "node-update", 28, "bind-stale-failed-parent", "--node-id", "stale-failed-parent",
            "--launch-state", "bound", "--child-id", "executor-stale-failed-parent",
        )
        self.mutation(
            "node-update", 29, "run-stale-failed-parent", "--node-id", "stale-failed-parent",
            "--status", "running",
        )
        self.mutation(
            "node-update", 30, "fail-stale-failed-parent", "--node-id", "stale-failed-parent",
            "--status", "failed", "--result", "partial split candidate",
            "--evidence", "failed candidate evidence", "--attempt-outcome", "candidate failed",
        )
        self.mutation(
            "requirement-set", 31, "change-stale-split-requirement", "--requirement-id", "req-stale-split",
            "--text", "Keep split accounting current", "--source", "task", "--status", "satisfied",
            "--evidence", "requirement changed after failure",
        )
        stale_failed_split = {
            "parent_id": "stale-failed-parent",
            "reason": "Must reject stale failure accounting",
            "children": [
                split_child(
                    "stale-failed-child-a",
                    acceptance=["stale child A proof"],
                    outputs=["stale child A artifact"],
                    requirement_ids=["req-stale-split"],
                    dimensions=lower,
                ),
                split_child(
                    "stale-failed-child-b",
                    acceptance=["stale child B proof"],
                    outputs=["stale child B artifact"],
                    requirement_ids=[],
                    dimensions=lower,
                ),
            ],
            "coverage": {
                "requirements": {"req-stale-split": ["stale-failed-child-a"]},
                "outputs": {"stale-failed-parent implementation": ["stale-failed-child-a"]},
                "acceptance": {"focused test passes": ["stale-failed-child-b"]},
            },
            "dependent_replacements": {},
        }
        stale_baseline = StateStore().load(self.workflow_id)
        stale_failed = stale_baseline["nodes"]["stale-failed-parent"]
        self.assertEqual(
            (stale_failed["status"], stale_failed["launch"]["state"], stale_failed["assessment"]["state"]),
            ("failed", "terminal", "stale"),
        )
        rejected_stale = self.mutation(
            "node-split", 32, "reject-stale-failed-split",
            "--plan-json", json.dumps(stale_failed_split), expected=2,
        )
        self.assertIn("current parent assessment", rejected_stale["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), stale_baseline)

        self.add("refinement-split-parent", 32, "add-refinement-split-parent", "--ambiguity", "2")
        refinement_split = {
            "parent_id": "refinement-split-parent",
            "reason": "Ambiguity must be resolved before decomposition",
            "children": [
                split_child(
                    "refinement-split-child-a",
                    acceptance=["refinement child A proof"],
                    outputs=["refinement child A artifact"],
                    requirement_ids=[],
                    dimensions=lower,
                ),
                split_child(
                    "refinement-split-child-b",
                    acceptance=["refinement child B proof"],
                    outputs=["refinement child B artifact"],
                    requirement_ids=[],
                    dimensions=lower,
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"refinement-split-parent implementation": ["refinement-split-child-a"]},
                "acceptance": {"focused test passes": ["refinement-split-child-b"]},
            },
            "dependent_replacements": {},
        }
        refinement_baseline = StateStore().load(self.workflow_id)
        self.assertEqual(
            refinement_baseline["nodes"]["refinement-split-parent"]["assessment"]["state"],
            "refinement_required",
        )
        rejected_refinement = self.mutation(
            "node-split", 33, "reject-refinement-required-split",
            "--plan-json", json.dumps(refinement_split), expected=2,
        )
        self.assertIn("split-required", rejected_refinement["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), refinement_baseline)

    def test_nested_split_rewires_children_and_clears_decomposed_dependencies(self) -> None:
        over_budget = {
            "breadth": 2,
            "change_surface": 2,
            "coupling": 2,
            "novelty": 2,
            "verification": 1,
        }
        add_over_budget = (
            "--breadth", "2", "--change-surface", "2", "--coupling", "2",
            "--novelty", "2", "--verification", "1",
        )
        self.add("upstream", 1, "add-nested-upstream", *add_over_budget)
        self.add("downstream", 2, "add-nested-downstream", *add_over_budget, "--dependency", "upstream")
        downstream_split = {
            "parent_id": "downstream",
            "reason": "Decompose downstream while retaining its upstream prerequisite",
            "children": [
                split_child(
                    "downstream-a",
                    acceptance=["downstream A proof"],
                    outputs=["downstream A artifact"],
                    requirement_ids=[],
                    dependencies=["upstream"],
                ),
                split_child(
                    "downstream-b",
                    acceptance=["downstream B proof"],
                    outputs=["downstream B artifact"],
                    requirement_ids=[],
                    dependencies=["upstream"],
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"downstream implementation": ["downstream-a"]},
                "acceptance": {"focused test passes": ["downstream-b"]},
            },
            "dependent_replacements": {},
        }
        self.mutation("node-split", 3, "split-nested-downstream", "--plan-json", json.dumps(downstream_split))
        after_downstream = StateStore().load(self.workflow_id)
        self.assertEqual(after_downstream["nodes"]["downstream"]["dependencies"], [])
        self.assertEqual(after_downstream["nodes"]["downstream-a"]["dependencies"], ["upstream"])
        self.assertEqual(after_downstream["nodes"]["downstream-b"]["dependencies"], ["upstream"])

        upstream_split = {
            "parent_id": "upstream",
            "reason": "Replace the upstream prerequisite for both downstream children",
            "children": [
                split_child(
                    "upstream-a",
                    acceptance=["upstream A proof"],
                    outputs=["upstream A artifact"],
                    requirement_ids=[],
                ),
                split_child(
                    "upstream-b",
                    acceptance=["upstream B proof"],
                    outputs=["upstream B artifact"],
                    requirement_ids=[],
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"upstream implementation": ["upstream-a"]},
                "acceptance": {"focused test passes": ["upstream-b"]},
            },
            "dependent_replacements": {
                "downstream-a": ["upstream-a"],
                "downstream-b": ["upstream-b"],
            },
        }
        self.mutation("node-split", 4, "split-nested-upstream", "--plan-json", json.dumps(upstream_split))
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["upstream"]["dependencies"], [])
        self.assertEqual(state["nodes"]["downstream"]["dependencies"], [])
        self.assertEqual(state["nodes"]["downstream-a"]["dependencies"], ["upstream-a"])
        self.assertEqual(state["nodes"]["downstream-b"]["dependencies"], ["upstream-b"])
        self.assertEqual(
            {state["nodes"][node_id]["assessment"]["state"] for node_id in ("downstream-a", "downstream-b")},
            {"stale"},
        )
        self.assertFalse(any(
            dependency in {"upstream", "downstream"}
            for node in state["nodes"].values()
            for dependency in node["dependencies"]
        ))
        self.assertEqual(over_budget, state["nodes"]["upstream"]["assessment"]["dimensions"])

    def test_node_split_prunes_terminal_success_dependents_without_replacements(self) -> None:
        self.add("P", 1, "add-cancelled-dependent-parent", "--breadth", "4")
        self.add("D", 2, "add-cancelled-dependent", "--dependency", "P")
        self.mutation(
            "node-update", 3, "skip-cancelled-dependent", "--node-id", "D", "--status", "skipped",
        )
        split = {
            "parent_id": "P",
            "reason": "Terminal-success dependent D no longer needs a live replacement edge",
            "children": [
                split_child(
                    "P-a",
                    acceptance=["bounded A proof"],
                    outputs=["bounded A artifact"],
                    requirement_ids=[],
                ),
                split_child(
                    "P-b",
                    acceptance=["bounded B proof"],
                    outputs=["bounded B artifact"],
                    requirement_ids=[],
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"P implementation": ["P-a"]},
                "acceptance": {"focused test passes": ["P-b"]},
            },
            "dependent_replacements": {},
        }
        self.mutation("node-split", 4, "split-after-dependent-cancellation", "--plan-json", json.dumps(split))
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["revision"], 5)
        self.assertEqual(state["nodes"]["D"]["dependencies"], [])
        self.assertEqual(
            (
                state["nodes"]["P"]["status"],
                state["nodes"]["P"]["assessment"]["state"],
                state["nodes"]["P"]["dependencies"],
            ),
            ("skipped", "decomposed", []),
        )
        self.assertEqual(
            {state["nodes"][node_id]["assessment"]["state"] for node_id in ("P-a", "P-b")},
            {"executable"},
        )
        self.assertEqual(set(ready_nodes(state)), {"P-a", "P-b"})

    def test_node_split_prerequisite_coverage_uses_final_rewired_graph_atomically(self) -> None:
        self.add("D", 1, "add-prerequisite")
        self.add(
            "P", 2, "add-split-parent", "--dependency", "D",
            "--breadth", "2", "--change-surface", "2", "--coupling", "2",
            "--novelty", "2", "--verification", "1",
        )
        self.add("R", 3, "add-current-dependent", "--dependency", "P")
        misleading_plan = {
            "parent_id": "P",
            "reason": "A pre-rewire path through R must not stand in for the final D prerequisite",
            "children": [
                split_child(
                    "P-via-R",
                    acceptance=["indirect child proof"],
                    outputs=["indirect child artifact"],
                    requirement_ids=[],
                    dependencies=["R"],
                ),
                split_child(
                    "P-replacement",
                    acceptance=["replacement child proof"],
                    outputs=["replacement child artifact"],
                    requirement_ids=[],
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"P implementation": ["P-via-R"]},
                "acceptance": {"focused test passes": ["P-replacement"]},
            },
            "dependent_replacements": {"R": ["P-replacement"]},
        }
        baseline = StateStore().load(self.workflow_id)
        rejected = self.mutation(
            "node-split", 4, "reject-false-prerequisite-coverage",
            "--plan-json", json.dumps(misleading_plan), expected=2,
        )
        self.assertIn("split silently drops parent prerequisite D", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        self.add("X", 4, "add-retained-node", "--dependency", "D")
        self.mutation("node-update", 5, "skip-retained-node", "--node-id", "X", "--status", "skipped")
        retained_plan = {
            "parent_id": "P",
            "reason": "A retained terminal node cannot witness the parent's prerequisite",
            "children": [
                split_child(
                    "P-via-X",
                    acceptance=["retained path proof"],
                    outputs=["retained path artifact"],
                    requirement_ids=[],
                    dependencies=["X"],
                ),
                split_child(
                    "P-retained-replacement",
                    acceptance=["retained replacement proof"],
                    outputs=["retained replacement artifact"],
                    requirement_ids=[],
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"P implementation": ["P-via-X"]},
                "acceptance": {"focused test passes": ["P-retained-replacement"]},
            },
            "dependent_replacements": {"R": ["P-retained-replacement"]},
        }
        retained_baseline = StateStore().load(self.workflow_id)
        retained_rejection = self.mutation(
            "node-split", 6, "reject-retained-prerequisite-bypass",
            "--plan-json", json.dumps(retained_plan), expected=2,
        )
        self.assertIn(
            "split silently drops parent prerequisite D",
            retained_rejection["data"]["message"],
        )
        self.assertEqual(StateStore().load(self.workflow_id), retained_baseline)
        self.mutation(
            "block", 6, "defer-unresolved-parent", "--node-id", "P",
            "--reason", "invalid prerequisite coverage", "--needed", "a valid child-only witness",
        )
        dispatch = planning_diagnostics(StateStore().load(self.workflow_id))["dispatch_order"]
        self.assertEqual(dispatch, ["D"])
        self.assertNotIn("P-via-X", StateStore().load(self.workflow_id)["nodes"])

    def test_terminal_bridge_severs_live_scope_and_critical_path_reachability(self) -> None:
        self.add("U", 1, "add-live-upstream", "--priority", "10", scope="src/shared")
        self.add("X", 2, "add-terminal-bridge", "--dependency", "U")
        self.mutation("node-update", 3, "skip-terminal-bridge", "--node-id", "X", "--status", "skipped")

        add_baseline = StateStore().load(self.workflow_id)
        rejected_add = self.add(
            "Y", 4, "reject-parallel-scope-through-terminal-bridge",
            "--dependency", "X", "--priority", "90", scope="src/shared", expected=2,
        )
        self.assertIn("write_scope_collisions", rejected_add["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), add_baseline)

        self.add("Y", 4, "add-nonoverlapping-downstream", "--dependency", "X", "--priority", "90")
        state = StateStore().load(self.workflow_id)
        diagnostics = planning_diagnostics(state)
        self.assertEqual(diagnostics["critical_path_load"], {"U": 5, "X": 0, "Y": 5})
        self.assertEqual(diagnostics["dispatch_order"], ["Y", "U"])
        self.assertEqual(ready_nodes(state), ["Y", "U"])
        self.assertEqual(
            [
                node_id
                for node_id in diagnostics["dispatch_order"]
                if "src/shared" in state["nodes"][node_id]["write_scopes"]
            ],
            ["U"],
        )

        y = state["nodes"]["Y"]
        colliding_refinement = {
            "spec": copy.deepcopy(y["spec"]),
            "acceptance": list(y["acceptance"]),
            "write_scopes": ["src/shared"],
            "assessment": assessment_inputs(rationale="Only the proposed scope changes"),
        }
        rejected_refinement = self.mutation(
            "node-refine", 5, "reject-refined-parallel-scope", "--node-id", "Y",
            "--refinement-json", json.dumps(colliding_refinement), expected=2,
        )
        self.assertIn("write_scope_collisions", rejected_refinement["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), state)

    def test_supersede_cycle_cannot_discharge_carried_work(self) -> None:
        self.add("cycle-parent", 1, "add-cycle-parent", "--breadth", "4")
        split = {
            "parent_id": "cycle-parent",
            "reason": "Create two eligible leaves while assigning the parent obligations to A",
            "children": [
                split_child(
                    "cycle-a",
                    acceptance=["cycle A proof"],
                    outputs=["cycle A artifact"],
                    requirement_ids=[],
                ),
                split_child(
                    "cycle-b",
                    acceptance=["cycle B proof"],
                    outputs=["cycle B artifact"],
                    requirement_ids=[],
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"cycle-parent implementation": ["cycle-a"]},
                "acceptance": {"focused test passes": ["cycle-a"]},
            },
            "dependent_replacements": {},
        }
        self.mutation("node-split", 2, "split-cycle-parent", "--plan-json", json.dumps(split))
        split_state = StateStore().load(self.workflow_id)
        self.assertEqual(set(ready_nodes(split_state)), {"cycle-a", "cycle-b"})
        obligations = copy.deepcopy(split_state["nodes"]["cycle-a"]["lineage"]["obligations"])
        self.assertTrue(obligations["outputs"] and obligations["acceptance"])

        forward = {
            "reason": "Transfer A's carried work to eligible leaf B",
            "operations": [{"op": "supersede", "node_id": "cycle-a", "replacement": "cycle-b"}],
        }
        self.mutation("graph-replan", 3, "supersede-cycle-a", "--plan-json", json.dumps(forward))
        transferred = StateStore().load(self.workflow_id)
        self.assertEqual(transferred["nodes"]["cycle-a"]["superseded_by"], "cycle-b")
        self.assertEqual(transferred["nodes"]["cycle-b"]["lineage"]["obligations"], obligations)
        self.assertEqual(transferred["nodes"]["cycle-b"]["assessment"]["state"], "stale")

        replacement = transferred["nodes"]["cycle-b"]
        refinement = {
            "spec": copy.deepcopy(replacement["spec"]),
            "acceptance": list(replacement["acceptance"]),
            "write_scopes": list(replacement["write_scopes"]),
            "assessment": assessment_inputs(rationale="Reassess B after accepting A's carried work"),
        }
        self.mutation(
            "node-refine", 4, "reassess-cycle-b", "--node-id", "cycle-b",
            "--refinement-json", json.dumps(refinement),
        )
        baseline = StateStore().load(self.workflow_id)
        reverse = {
            "reason": "A reverse transfer must not form a supersede cycle",
            "operations": [{"op": "supersede", "node_id": "cycle-b", "replacement": "cycle-a"}],
        }
        rejected = self.mutation(
            "graph-replan", 5, "reject-reverse-supersede", "--plan-json", json.dumps(reverse),
            expected=2,
        )
        self.assertIn("superseded_by cycle is not allowed", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)
        self.assertEqual(
            (baseline["nodes"]["cycle-b"]["status"], baseline["nodes"]["cycle-b"]["lineage"]["obligations"]),
            ("pending", obligations),
        )

        self.mutation(
            "finish", 5, "reject-unexecuted-carried-work", "--summary", "not executed",
            "--validation", "not run", "--commit", "a" * 40, expected=2,
        )
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        forged_cycle = copy.deepcopy(baseline)
        forged_cycle["nodes"]["cycle-b"].update({
            "status": "skipped",
            "result": "superseded",
            "evidence": "forged reverse transfer",
            "superseded_by": "cycle-a",
        })
        with self.assertRaisesRegex(StateError, "superseded_by cycle is not allowed"):
            validate_state(forged_cycle)
        unresolved = copy.deepcopy(forged_cycle)
        unresolved["nodes"]["cycle-b"]["superseded_by"] = None
        with self.assertRaisesRegex(
            StateError, "superseded_by chain must terminate in resolvable work"
        ):
            validate_state(unresolved)

    def test_supersede_cannot_cycle_carried_obligations_through_decomposition(self) -> None:
        self.add("G", 1, "add-provenance-root", "--output", "O", "--breadth", "4")
        split_required = {name: (4 if name == "breadth" else 0) for name in DIMENSIONS}
        bounded = {"breadth": 0, "change_surface": 0, "coupling": 1, "novelty": 1, "verification": 1}
        root_split = {
            "parent_id": "G",
            "reason": "Carry every root obligation into P while leaving Q obligation-free",
            "children": [
                split_child(
                    "P",
                    acceptance=["focused test passes"],
                    outputs=["G implementation", "O"],
                    requirement_ids=[],
                    dimensions=split_required,
                ),
                split_child(
                    "Q",
                    acceptance=["Q native acceptance"],
                    outputs=["Q native output"],
                    requirement_ids=[],
                    dimensions=bounded,
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"G implementation": ["P"], "O": ["P"]},
                "acceptance": {"focused test passes": ["P"]},
            },
            "dependent_replacements": {},
        }
        self.mutation("node-split", 2, "split-provenance-root", "--plan-json", json.dumps(root_split))
        first_split = StateStore().load(self.workflow_id)
        self.assertIn("O", first_split["nodes"]["P"]["spec"]["outputs"])
        self.assertIn("O", first_split["nodes"]["P"]["lineage"]["obligations"]["outputs"])
        self.assertEqual(
            first_split["nodes"]["Q"]["lineage"]["obligations"],
            {"requirements": [], "outputs": [], "acceptance": []},
        )
        self.mutation("node-update", 3, "cancel-zero-obligation-Q", "--node-id", "Q", "--status", "cancelled")

        child_split = {
            "parent_id": "P",
            "reason": "Carry P's effective obligations into A while leaving B obligation-free",
            "children": [
                split_child(
                    "A",
                    acceptance=["A native acceptance"],
                    outputs=["A native output"],
                    requirement_ids=[],
                    dimensions=bounded,
                ),
                split_child(
                    "B",
                    acceptance=["B native acceptance"],
                    outputs=["B native output"],
                    requirement_ids=[],
                    dimensions=bounded,
                ),
            ],
            "coverage": {
                "requirements": {},
                "outputs": {"O": ["A"], "G implementation": ["A"]},
                "acceptance": {"focused test passes": ["A"]},
            },
            "dependent_replacements": {},
        }
        self.mutation("node-split", 4, "split-obligated-P", "--plan-json", json.dumps(child_split))
        second_split = StateStore().load(self.workflow_id)
        self.assertIn("O", second_split["nodes"]["A"]["lineage"]["obligations"]["outputs"])
        self.assertEqual(
            second_split["nodes"]["B"]["lineage"]["obligations"],
            {"requirements": [], "outputs": [], "acceptance": []},
        )
        self.mutation("node-update", 5, "cancel-zero-obligation-B", "--node-id", "B", "--status", "cancelled")

        baseline = StateStore().load(self.workflow_id)
        self.assertIsNone(baseline["nodes"]["A"]["superseded_by"])
        self.assertIsNone(baseline["nodes"]["P"]["superseded_by"])
        provenance_cycle = {
            "reason": "A pointer-acyclic transfer must not loop through P's decomposition coverage",
            "operations": [{"op": "supersede", "node_id": "A", "replacement": "P"}],
        }
        rejected = self.mutation(
            "graph-replan", 6, "reject-provenance-cycle",
            "--plan-json", json.dumps(provenance_cycle), expected=2,
        )
        self.assertIn("obligation", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)
        self.assertEqual(
            (baseline["nodes"]["A"]["status"], baseline["nodes"]["A"]["attempts"]),
            ("pending", []),
        )
        self.mutation(
            "finish", 6, "reject-unexecuted-provenance-cycle", "--summary", "not executed",
            "--validation", "not run", "--commit", "b" * 40, expected=2,
        )
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

    def test_node_split_capacity_reserves_liveness_and_requires_bounded_final_children(self) -> None:
        over_budget = (
            "--breadth", "2", "--change-surface", "2", "--coupling", "2",
            "--novelty", "2", "--verification", "1",
        )
        with mock.patch.object(state_owner, "MAX_NODES", 3):
            original_workflow = self.workflow_id
            raw_created = self.cli(
                "init", "--repo", str(self.repo), "--task", "Reserve raw split capacity",
                "--session-file", str(self.session), "--mutation-id", "init-raw-capacity",
            )
            self.workflow_id = raw_created["data"]["workflow_id"]
            try:
                self.mutation(
                    "requirement-set", 1, "add-raw-capacity-requirement",
                    "--requirement-id", "req-raw-capacity", "--text", "Keep accounting current",
                    "--source", "task", "--status", "active",
                )
                self.add(
                    "raw-capacity", 2, "add-ambiguous-raw-capacity",
                    "--requirement-id", "req-raw-capacity", "--breadth", "4",
                    "--ambiguity", "2", "--open-question", "Which bounded split applies?",
                )
                refinement_required = StateStore().load(self.workflow_id)
                raw_node = refinement_required["nodes"]["raw-capacity"]
                self.assertEqual((raw_node["assessment"]["dimensions"]["breadth"], raw_node["assessment"]["state"]), (4, "refinement_required"))
                rejected_ambiguous_filler = self.add(
                    "raw-filler", 3, "reject-ambiguous-raw-filler", expected=2,
                )
                self.assertIn(
                    "workflow capacity must reserve two node records",
                    rejected_ambiguous_filler["data"]["message"],
                )
                self.assertEqual(StateStore().load(self.workflow_id), refinement_required)

                self.mutation(
                    "requirement-set", 3, "stale-raw-capacity", "--requirement-id", "req-raw-capacity",
                    "--text", "Keep accounting current", "--source", "task", "--status", "satisfied",
                    "--evidence", "accounting input changed",
                )
                stale = StateStore().load(self.workflow_id)
                raw_node = stale["nodes"]["raw-capacity"]
                self.assertEqual((raw_node["assessment"]["dimensions"]["breadth"], raw_node["assessment"]["state"]), (4, "stale"))
                rejected_stale_filler = self.add(
                    "raw-filler", 4, "reject-stale-raw-filler", expected=2,
                )
                self.assertIn(
                    "workflow capacity must reserve two node records",
                    rejected_stale_filler["data"]["message"],
                )
                self.assertEqual(StateStore().load(self.workflow_id), stale)
            finally:
                self.workflow_id = original_workflow

            self.add("capacity-parent", 1, "add-capacity-parent", *over_budget)
            baseline = StateStore().load(self.workflow_id)
            rejected_filler = self.add("capacity-filler", 2, "reject-capacity-filler", expected=2)
            self.assertIn(
                "workflow capacity must reserve two node records per split-required leaf",
                rejected_filler["data"]["message"],
            )
            self.assertEqual(StateStore().load(self.workflow_id), baseline)

            unresolved = {
                "breadth": 4,
                "change_surface": 1,
                "coupling": 1,
                "novelty": 1,
                "verification": 1,
            }
            capacity_split = {
                "parent_id": "capacity-parent",
                "reason": "Fill the final capacity with two children",
                "children": [
                    split_child(
                        "capacity-child-a",
                        acceptance=["capacity A proof"],
                        outputs=["capacity A artifact"],
                        requirement_ids=[],
                        dimensions=unresolved,
                    ),
                    split_child(
                        "capacity-child-b",
                        acceptance=["capacity B proof"],
                        outputs=["capacity B artifact"],
                        requirement_ids=[],
                    ),
                ],
                "coverage": {
                    "requirements": {},
                    "outputs": {"capacity-parent implementation": ["capacity-child-a"]},
                    "acceptance": {"focused test passes": ["capacity-child-b"]},
                },
                "dependent_replacements": {},
            }
            rejected = self.mutation(
                "node-split", 2, "reject-unresolved-capacity-split",
                "--plan-json", json.dumps(capacity_split), expected=2,
            )
            self.assertIn(
                "workflow capacity must reserve two node records per split-required leaf",
                rejected["data"]["message"],
            )
            self.assertEqual(StateStore().load(self.workflow_id), baseline)

            bounded_split = copy.deepcopy(capacity_split)
            bounded_split["reason"] = "Fill the final capacity with bounded children"
            bounded_split["children"][0]["assessment"]["dimensions"] = DIMENSIONS
            self.mutation(
                "node-split", 2, "accept-bounded-capacity-split",
                "--plan-json", json.dumps(bounded_split),
            )
            state = StateStore().load(self.workflow_id)
            self.assertEqual(len(state["nodes"]), 3)
            self.assertEqual(
                {state["nodes"][node_id]["assessment"]["state"] for node_id in (
                    "capacity-child-a", "capacity-child-b",
                )},
                {"executable"},
            )
            self.assertEqual(state["nodes"]["capacity-child-a"]["route"]["attempt"], 0)
            unrouted = self.mutation(
                "node-update", 3, "claim-unrouted-split-child", "--node-id", "capacity-child-a",
                "--launch-state", "claimed", "--request-id", "request-capacity-child", expected=2,
            )
            self.assertIn("persist a fresh node-route", unrouted["data"]["message"])
            self.assertEqual(StateStore().load(self.workflow_id), state)
            self.route("capacity-child-a", 3, "route-capacity-child")
            self.mutation(
                "node-update", 4, "claim-capacity-child", "--node-id", "capacity-child-a",
                "--launch-state", "claimed", "--request-id", "request-capacity-child",
            )

    def test_node_split_and_refine_enforce_max_depth_liveness_atomically(self) -> None:
        profile_path = self.base / "depth-profile.json"
        profile_path.write_text(json.dumps({"max_refinement_depth": 1}), encoding="utf-8")
        created = self.cli(
            "init", "--repo", str(self.repo), "--task", "Prove split depth",
            "--profile-file", str(profile_path), "--session-file", str(self.session),
            "--mutation-id", "init-depth",
        )
        original_workflow = self.workflow_id
        self.workflow_id = created["data"]["workflow_id"]
        try:
            self.add("root", 1, "add-depth-root", "--breadth", "4")
            lower = {**DIMENSIONS, "breadth": 0}
            over_budget_at_limit = {
                "parent_id": "root",
                "reason": "This would strand over-budget work at the depth limit",
                "children": [
                    split_child(
                        "depth-one-a",
                        acceptance=["depth A proof"],
                        outputs=["depth A artifact"],
                        requirement_ids=[],
                        dimensions={name: (4 if name == "breadth" else 0) for name in DIMENSIONS},
                    ),
                    split_child(
                        "depth-one-b",
                        acceptance=["depth B proof"],
                        outputs=["depth B artifact"],
                        requirement_ids=[],
                        dimensions=lower,
                    ),
                ],
                "coverage": {
                    "requirements": {},
                    "outputs": {"root implementation": ["depth-one-a"]},
                    "acceptance": {"focused test passes": ["depth-one-b"]},
                },
                "dependent_replacements": {},
            }
            baseline = StateStore().load(self.workflow_id)
            rejected_split = self.mutation(
                "node-split", 2, "reject-over-budget-at-limit",
                "--plan-json", json.dumps(over_budget_at_limit), expected=2,
            )
            self.assertIn(
                "max_refinement_depth requires bounded final children",
                rejected_split["data"]["message"],
            )
            self.assertEqual(StateStore().load(self.workflow_id), baseline)

            ambiguous_over_budget_at_limit = copy.deepcopy(over_budget_at_limit)
            ambiguous_over_budget_at_limit["reason"] = (
                "Ambiguity must not hide raw over-budget work at the depth limit"
            )
            ambiguous_over_budget_at_limit["children"][0]["assessment"]["ambiguity"] = 2
            ambiguous_over_budget_at_limit["children"][0]["spec"]["open_questions"] = [
                "Which final bounded leaf owns this work?"
            ]
            rejected_ambiguous_split = self.mutation(
                "node-split", 2, "reject-ambiguous-over-budget-at-limit",
                "--plan-json", json.dumps(ambiguous_over_budget_at_limit), expected=2,
            )
            self.assertIn(
                "max_refinement_depth requires bounded final children",
                rejected_ambiguous_split["data"]["message"],
            )
            self.assertEqual(StateStore().load(self.workflow_id), baseline)

            bounded_at_limit = copy.deepcopy(over_budget_at_limit)
            bounded_at_limit["reason"] = "Create bounded final leaves at the depth limit"
            bounded_at_limit["children"][0]["assessment"]["dimensions"] = lower
            self.mutation(
                "node-split", 2, "split-bounded-at-limit",
                "--plan-json", json.dumps(bounded_at_limit),
            )
            bounded = StateStore().load(self.workflow_id)
            for node_id in ("depth-one-a", "depth-one-b"):
                self.assertEqual(
                    (
                        bounded["nodes"][node_id]["lineage"]["depth"],
                        bounded["nodes"][node_id]["assessment"]["state"],
                    ),
                    (1, "executable"),
                )

            child = bounded["nodes"]["depth-one-a"]
            over_budget_refinement = {
                "spec": child["spec"],
                "acceptance": child["acceptance"],
                "write_scopes": child["write_scopes"],
                "assessment": assessment_inputs(
                    dimensions={name: (4 if name == "breadth" else 0) for name in DIMENSIONS},
                    rationale="This refinement would require another split",
                ),
            }
            rejected_refinement = self.mutation(
                "node-refine", 3, "reject-over-budget-refinement-at-limit", "--node-id", "depth-one-a",
                "--refinement-json", json.dumps(over_budget_refinement), expected=2,
            )
            self.assertIn(
                "max_refinement_depth must produce a bounded final leaf",
                rejected_refinement["data"]["message"],
            )
            self.assertEqual(StateStore().load(self.workflow_id), bounded)
        finally:
            self.workflow_id = original_workflow

    def test_revision_receipts_and_invalid_graph_changes_are_atomic(self) -> None:
        replayed_init = self.cli(
            "init", "--repo", str(self.repo), "--task", "Coordinate durable work",
            "--session-file", str(self.session), "--mutation-id", "init-001",
        )
        self.assertEqual(replayed_init["data"]["workflow_id"], self.workflow_id)
        with mock.patch.object(state_owner, "now_iso", return_value="2099-01-01T00:00:00Z"):
            first = self.add("a", 1, "add-a")
        self.assertEqual(first["data"]["revision"], 2)
        with mock.patch.object(state_owner, "now_iso", return_value="2099-01-01T00:00:01Z"):
            replay = self.add("a", 1, "add-a")
        self.assertEqual(replay["code"], "mutation_reconciled")
        self.assertEqual(
            StateStore().load(self.workflow_id)["nodes"]["a"]["route"]["routed_at"],
            "2099-01-01T00:00:00Z",
        )
        inherited = StateStore().load(self.workflow_id)["nodes"]["a"]
        self.assertEqual((inherited["model"], inherited["effort"]), (None, None))
        collision = self.add("different", 1, "add-a", expected=20)
        self.assertEqual(collision["code"], "mutation_conflict")
        self.add("other", 1, "stale-add", expected=20)
        self.add("b", 2, "add-b", "--dependency", "a")
        malformed = json.dumps({"reason": "invalid node id", "operations": [{"op": "remove", "node_id": {}}]})
        self.mutation("graph-replan", 3, "invalid-node-id", "--plan-json", malformed, expected=2)
        malformed = json.dumps({"reason": "invalid dependency", "operations": [{"op": "dependency_add", "node_id": "a", "dependency": {}}]})
        self.assertEqual(
            self.mutation("graph-replan", 3, "invalid-dependency", "--plan-json", malformed, expected=2)["code"],
            "invalid_state",
        )
        malformed = json.dumps({"reason": "invalid replacement", "operations": [{"op": "supersede", "node_id": "a", "replacement": {}}]})
        self.assertEqual(
            self.mutation("graph-replan", 3, "invalid-replacement", "--plan-json", malformed, expected=2)["code"],
            "invalid_state",
        )
        plan = json.dumps({"reason": "try cycle", "operations": [{"op": "dependency_add", "node_id": "a", "dependency": "b"}]})
        self.mutation("graph-replan", 3, "cycle", "--plan-json", plan, expected=2)
        self.add("collision", 3, "scope-collision", scope="src/a/file.py", expected=2)
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["revision"], 3)
        self.assertEqual(set(state["nodes"]), {"a", "b"})

    def test_launch_claim_requires_dependency_and_workflow_readiness(self) -> None:
        self.add("dependency", 1, "add-dependency")
        self.add("work", 2, "add-work", "--dependency", "dependency")
        baseline = StateStore().load(self.workflow_id)
        unrouted = self.mutation(
            "node-update", 3, "claim-fresh-node", "--node-id", "dependency",
            "--launch-state", "claimed", "--request-id", "request-fresh", expected=2,
        )
        self.assertIn("persist a fresh node-route", unrouted["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)
        self.route("dependency", 3, "route-dependency")
        self.route("work", 4, "route-work")
        rejected = self.mutation(
            "node-update", 5, "claim-dependent", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-work", expected=2,
        )
        self.assertEqual(rejected["code"], "invalid_state")
        self.assertEqual(self.cli("graph-validate", "--workflow-id", self.workflow_id)["data"]["ready_nodes"], ["dependency"])
        self.mutation("block", 5, "block-workflow", "--reason", "pause", "--needed", "approval")
        self.assertEqual(self.cli("graph-validate", "--workflow-id", self.workflow_id)["data"]["ready_nodes"], [])
        self.mutation(
            "node-update", 6, "claim-blocked", "--node-id", "dependency",
            "--launch-state", "claimed", "--request-id", "request-dependency", expected=2,
        )
        state = StateStore().load(self.workflow_id)
        self.assertEqual((state["revision"], state["nodes"]["dependency"]["launch"]["state"]), (6, "unclaimed"))
        state["status"] = "planning"
        self.assertEqual(ready_nodes(state), [])
        state["blockers"][0].update({"status": "resolved", "resolution": "approved"})
        state["status"] = "blocked"
        self.assertEqual(ready_nodes(state), [])

    def test_inline_fallback_binds_a_maximum_length_request_identifier(self) -> None:
        self.add("work", 1, "add-work")
        self.mutation("node-update", 2, "ready-work", "--node-id", "work", "--status", "ready")
        self.route("work", 3, "route-work")
        request_id = "r" * 128
        self.mutation(
            "node-update", 4, "claim-work", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", request_id,
        )
        inline_id = "inline-" + hashlib.sha256(request_id.encode()).hexdigest()
        self.mutation(
            "node-update", 5, "bind-inline", "--node-id", "work",
            "--launch-state", "bound", "--child-id", inline_id,
        )
        self.mutation("node-update", 6, "run-inline", "--node-id", "work", "--status", "running")
        self.mutation(
            "node-update", 7, "finish-inline", "--node-id", "work", "--status", "done",
            "--result", "implemented inline", "--evidence", "focused test passed",
        )
        node = StateStore().load(self.workflow_id)["nodes"]["work"]
        self.assertEqual((node["status"], node["launch"]["state"]), ("done", "terminal"))
        self.assertEqual(node["launch"]["child_id"], inline_id)

    def test_takeover_fences_old_controller_and_launch_reconciliation_is_explicit(self) -> None:
        self.mutation(
            "requirement-set", 1, "add-recovery-requirement", "--requirement-id", "req-recovery",
            "--text", "Use the current recovery input", "--source", "task", "--status", "active",
        )
        self.add("work", 2, "add-work", "--requirement-id", "req-recovery")
        second_session = self.base / "private" / "second.json"
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(second_session))
        takeover = self.mutation("controller-takeover", 3, "takeover-1", session=second_session)
        self.assertTrue(takeover["data"]["resume_required"])
        self.mutation("event", 4, "old-controller", "--kind", "test", "--message", "old", expected=20)
        self.mutation("event", 4, "before-resume", "--kind", "test", "--message", "new", session=second_session, expected=20)
        resumed = self.mutation("resume", 4, "resume-1", "--message", "reconciled takeover", session=second_session)
        self.assertEqual(resumed["data"]["controller_epoch"], 2)
        self.route("work", 5, "route-after-takeover", session=second_session)
        self.mutation(
            "node-update", 6, "ready-work", "--node-id", "work", "--status", "ready",
            session=second_session,
        )
        self.mutation("node-update", 7, "claim-1", "--node-id", "work", "--launch-state", "claimed", "--request-id", "request-1", session=second_session)
        self.mutation("node-update", 8, "uncertain-1", "--node-id", "work", "--launch-state", "reconcile_required", "--reconciliation", "provider timed out", session=second_session)
        self.mutation("node-update", 9, "unsafe-retry", "--node-id", "work", "--launch-state", "unclaimed", session=second_session, expected=2)
        assessment_before_change = copy.deepcopy(
            StateStore().load(self.workflow_id)["nodes"]["work"]["assessment"]
        )
        self.mutation(
            "requirement-set", 9, "change-recovery-requirement", "--requirement-id", "req-recovery",
            "--text", "Use the current recovery input", "--source", "task", "--status", "satisfied",
            "--evidence", "recovery input changed", session=second_session,
        )
        self.mutation(
            "node-update", 10, "safe-retry", "--node-id", "work", "--launch-state", "unclaimed",
            "--reconciliation", "provider confirms no child", session=second_session,
        )
        state = StateStore().load(self.workflow_id)
        node = state["nodes"]["work"]
        self.assertEqual((state["revision"], node["status"], node["launch"]["state"]), (11, "pending", "unclaimed"))
        self.assertEqual(node["launch"]["reconciliation"], "provider confirms no child")
        self.assertEqual(node["assessment"]["state"], "stale")
        self.assertEqual(node["assessment"]["input_digest"], assessment_before_change["input_digest"])
        self.assertEqual(node["attempts"][0]["outcome"], "provider confirmed not launched")
        self.assertIsNotNone(node["attempts"][0]["finished_at"])

        recovery_baseline = copy.deepcopy(state)
        self.route("work", 11, "reject-route-before-reassessment", session=second_session, expected=2)
        self.mutation(
            "node-update", 11, "reject-claim-before-reassessment", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-stale", session=second_session,
            expected=2,
        )
        self.assertEqual(StateStore().load(self.workflow_id), recovery_baseline)

        refinement = {
            "spec": specification("work", requirement_ids=["req-recovery"]),
            "acceptance": ["focused test passes"],
            "write_scopes": ["src/work"],
            "assessment": assessment_inputs(rationale="Reassessed against the current recovery input"),
        }
        self.mutation(
            "node-refine", 11, "reassess-recovered-work", "--node-id", "work",
            "--refinement-json", json.dumps(refinement), session=second_session,
        )
        reassessed = StateStore().load(self.workflow_id)
        self.assertEqual(reassessed["nodes"]["work"]["assessment"]["state"], "executable")
        unrouted = self.mutation(
            "node-update", 12, "reject-claim-before-reroute", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-unrouted", session=second_session,
            expected=2,
        )
        self.assertIn("persist a fresh node-route", unrouted["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), reassessed)
        self.route("work", 12, "route-reassessed-recovery", session=second_session)
        self.mutation(
            "node-update", 13, "claim-reassessed-recovery", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-2", session=second_session,
        )
        relaunched = StateStore().load(self.workflow_id)["nodes"]["work"]
        self.assertEqual([attempt["number"] for attempt in relaunched["attempts"]], [1, 2])
        self.assertEqual(relaunched["attempts"][0]["outcome"], "provider confirmed not launched")

    def test_failed_attempt_can_be_routed_and_relaunched_without_losing_history(self) -> None:
        self.add("work", 1, "add-work")
        self.mutation("node-update", 2, "ready-work", "--node-id", "work", "--status", "ready")
        self.route("work", 3, "route-work")
        self.mutation(
            "node-update", 4, "claim-work", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-1",
        )
        self.mutation(
            "node-update", 5, "bind-work", "--node-id", "work",
            "--launch-state", "bound", "--child-id", "child-1",
        )
        self.mutation("node-update", 6, "run-work", "--node-id", "work", "--status", "running")
        self.mutation(
            "node-update", 7, "fail-work", "--node-id", "work",
            "--status", "failed", "--attempt-outcome", "tests failed",
        )
        route = (
            "--node-id", "work", "--role", "fixer", "--model", "vendor/future-model@next",
            "--effort", "adaptive-depth", "--rationale", "fix the failed attempt",
        )
        self.mutation("node-route", 8, "reroute-work", *route)
        replay = self.mutation("node-route", 8, "reroute-work", *route)
        self.assertEqual(replay["code"], "mutation_reconciled")
        node = StateStore().load(self.workflow_id)["nodes"]["work"]
        self.assertEqual((node["status"], node["launch"]["state"], node["route"]["attempt"]), ("pending", "unclaimed", 2))
        self.assertEqual((node["model"], node["effort"]), ("vendor/future-model@next", "adaptive-depth"))
        self.assertEqual((node["attempts"][0]["number"], node["attempts"][0]["outcome"]), (1, "tests failed"))
        self.assertIsNotNone(node["attempts"][0]["finished_at"])

        self.mutation("node-update", 9, "ready-retry", "--node-id", "work", "--status", "ready")
        self.mutation(
            "node-update", 10, "claim-retry", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-2",
        )
        attempts = StateStore().load(self.workflow_id)["nodes"]["work"]["attempts"]
        self.assertEqual([attempt["number"] for attempt in attempts], [1, 2])
        self.assertEqual(attempts[0]["outcome"], "tests failed")

    def test_aborted_workflow_allows_only_preexisting_launch_reconciliation(self) -> None:
        self.add("absent", 1, "add-absent")
        self.add("found", 2, "add-found")
        self.add("running", 3, "add-running")
        self.mutation("node-update", 4, "ready-running", "--node-id", "running", "--status", "ready")
        self.route("absent", 5, "route-absent")
        self.route("found", 6, "route-found")
        self.route("running", 7, "route-running")
        self.mutation(
            "node-update", 8, "claim-absent", "--node-id", "absent",
            "--launch-state", "claimed", "--request-id", "request-absent",
        )
        self.mutation(
            "node-update", 9, "uncertain-absent", "--node-id", "absent",
            "--launch-state", "reconcile_required", "--reconciliation", "provider timeout",
        )
        self.mutation(
            "node-update", 10, "claim-found", "--node-id", "found",
            "--launch-state", "claimed", "--request-id", "request-found",
        )
        self.mutation(
            "node-update", 11, "uncertain-found", "--node-id", "found",
            "--launch-state", "reconcile_required", "--reconciliation", "provider timeout",
        )
        self.mutation(
            "node-update", 12, "claim-running", "--node-id", "running",
            "--launch-state", "claimed", "--request-id", "request-running",
        )
        self.mutation(
            "node-update", 13, "bind-running", "--node-id", "running",
            "--launch-state", "bound", "--child-id", "child-running",
        )
        self.mutation("node-update", 14, "run-running", "--node-id", "running", "--status", "running")
        self.mutation("abort", 15, "abort-workflow", "--reason", "operator stopped")
        self.mutation(
            "node-update", 16, "reject-different-known-child", "--node-id", "running",
            "--launch-state", "bound", "--child-id", "child-different",
            "--reconciliation", "provider reports a different child", expected=2,
        )
        unchanged = StateStore().load(self.workflow_id)
        self.assertEqual(unchanged["revision"], 16)
        self.assertEqual(
            (unchanged["nodes"]["running"]["launch"]["state"], unchanged["nodes"]["running"]["launch"]["child_id"]),
            ("reconcile_required", "child-running"),
        )
        self.mutation(
            "node-update", 16, "reject-known-child-absence", "--node-id", "running",
            "--launch-state", "unclaimed", "--reconciliation", "provider confirms no child", expected=2,
        )
        absent = (
            "--node-id", "absent", "--launch-state", "unclaimed",
            "--reconciliation", "provider confirms no child",
        )
        self.mutation("node-update", 16, "resolve-absent", *absent)
        self.mutation(
            "node-update", 17, "resolve-found", "--node-id", "found",
            "--launch-state", "bound", "--child-id", "child-found",
            "--reconciliation", "provider confirms existing child",
        )
        self.mutation(
            "node-update", 18, "resolve-known-child", "--node-id", "running",
            "--launch-state", "bound", "--child-id", "child-running",
            "--reconciliation", "provider confirms the known child",
        )
        replay = self.mutation("node-update", 16, "resolve-absent", *absent)
        self.assertEqual(replay["code"], "mutation_reconciled")
        self.mutation("event", 19, "terminal-event", "--kind", "test", "--message", "blocked", expected=2)

        state = StateStore().load(self.workflow_id)
        self.assertEqual((state["status"], state["phase"], state["controller"]["recovery_status"]), ("aborted", "aborted", "reconcile_required"))
        self.assertEqual(state["nodes"]["absent"]["launch"]["state"], "unclaimed")
        self.assertEqual(state["nodes"]["absent"]["attempts"][0]["outcome"], "provider confirmed not launched")
        self.assertEqual(state["nodes"]["found"]["launch"]["state"], "bound")
        self.assertEqual(state["nodes"]["found"]["launch"]["child_id"], "child-found")
        running = state["nodes"]["running"]
        self.assertEqual((running["status"], running["launch"]["state"]), ("cancelled", "bound"))
        self.assertEqual(running["launch"]["child_id"], "child-running")
        self.assertIsNone(running["attempts"][0]["finished_at"])
        self.assertIsNone(running["attempts"][0]["outcome"])

    def test_post_abort_recovery_can_transfer_and_complete_a_discovered_child(self) -> None:
        self.add("work", 1, "add-work")
        self.route("work", 2, "route-work")
        self.mutation(
            "node-update", 3, "claim-work", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-1",
        )
        self.mutation(
            "node-update", 4, "uncertain-work", "--node-id", "work",
            "--launch-state", "reconcile_required", "--reconciliation", "provider timeout",
        )
        self.mutation("abort", 5, "abort-workflow", "--reason", "controller stopped")

        second_session = self.base / "private" / "second.json"
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(second_session))
        takeover = self.mutation("controller-takeover", 6, "takeover-aborted", session=second_session)
        replayed_takeover = self.mutation("controller-takeover", 6, "takeover-aborted", session=second_session)
        self.assertEqual((takeover["data"]["revision"], replayed_takeover["data"]["revision"]), (7, 7))
        self.mutation("resume", 7, "resume-recovery", "--message", "resume provider recovery", session=second_session)
        replayed_resume = self.mutation(
            "resume", 7, "resume-recovery", "--message", "resume provider recovery", session=second_session
        )
        self.assertEqual(replayed_resume["code"], "mutation_reconciled")
        self.mutation(
            "node-update", 8, "bind-work", "--node-id", "work", "--launch-state", "bound",
            "--child-id", "child-1", "--reconciliation", "provider found existing child", session=second_session,
        )
        self.assertEqual(StateStore().load(self.workflow_id)["controller"]["recovery_status"], "reconcile_required")
        self.mutation(
            "node-update", 9, "terminal-without-evidence", "--node-id", "work",
            "--launch-state", "terminal", "--attempt-outcome", "stopped after abort",
            session=second_session, expected=2,
        )
        complete = (
            "--node-id", "work", "--launch-state", "terminal",
            "--reconciliation", "provider confirms child stopped", "--attempt-outcome", "stopped after abort",
        )
        self.mutation("node-update", 9, "complete-child", *complete, session=second_session)
        replayed = self.mutation("node-update", 9, "complete-child", *complete, session=second_session)
        self.assertEqual(replayed["code"], "mutation_reconciled")

        state = StateStore().load(self.workflow_id)
        attempt = state["nodes"]["work"]["attempts"][0]
        self.assertEqual((state["status"], state["controller"]["recovery_status"]), ("aborted", "clean"))
        self.assertEqual((state["nodes"]["work"]["launch"]["state"], attempt["outcome"]), ("terminal", "stopped after abort"))
        self.assertIsNotNone(attempt["finished_at"])
        third_session = self.base / "private" / "third.json"
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(third_session))
        self.mutation("controller-takeover", 10, "takeover-clean-abort", session=third_session, expected=2)

    def test_unknown_stored_fields_are_reported_without_repair(self) -> None:
        store = StateStore()
        path = store._state_path(self.workflow_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["unexpected"] = True
        before = json.dumps(raw, sort_keys=True)
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(StateError):
            store.load(self.workflow_id)
        self.assertEqual(json.dumps(json.loads(path.read_text(encoding="utf-8")), sort_keys=True), before)
        self.cli(
            "session-open", "--repo", str(self.repo), "--session-file", str(self.repo / "bearer.json"), expected=20
        )
        self.assertFalse((self.repo / "bearer.json").exists())

    def test_intermediate_alias_cannot_redirect_control_or_bearer_writes(self) -> None:
        outside_home = self.base / "outside" / "home"
        (outside_home / "private").mkdir(parents=True)
        alias = self.base / "intermediate-alias"
        alias.symlink_to(outside_home.parent, target_is_directory=True)
        repository = state_owner.canonical_repository(self.repo)
        control_store = StateStore(alias / "home" / ".agent-coordinator")
        safe_bearer = self.base / "safe-bearer" / "session.json"
        with self.assertRaisesRegex(StateError, "unsafe directory"):
            control_store.open_session(repository, safe_bearer)
        self.assertFalse((outside_home / ".agent-coordinator").exists())
        self.assertFalse(safe_bearer.exists())

        bearer_store = StateStore(self.base / "safe-control")
        outside_bearer = alias / "home" / "private" / "session.json"
        with self.assertRaisesRegex(StateError, "unsafe directory"):
            bearer_store.open_session(repository, outside_bearer)
        self.assertFalse((outside_home / "private" / "session.json").exists())
        self.assertFalse((self.base / "safe-control").exists())

    def test_state_boundary_rejects_duplicate_keys_bool_integers_oversize_and_filename_mismatch(self) -> None:
        store = StateStore()
        path = store._state_path(self.workflow_id)
        original = path.read_bytes()
        duplicate = original.decode("utf-8").replace(
            '"task": "Coordinate durable work"',
            '"task": "first", "task": "second"',
        )
        path.write_text(duplicate, encoding="utf-8")
        with self.assertRaisesRegex(StateError, "duplicate key"):
            store.load(self.workflow_id)
        path.write_bytes(original)

        surrogate = original.decode("utf-8").replace(
            '"task": "Coordinate durable work"',
            '"task": "\\ud800"',
        )
        path.write_text(surrogate, encoding="utf-8")
        with self.assertRaisesRegex(StateError, "Unicode scalar"):
            store.load(self.workflow_id)
        path.write_bytes(original)

        state = json.loads(original)
        state["conventions"].update({"max_parallel": True, "reserve": False})
        with self.assertRaisesRegex(StateError, "max_parallel"):
            validate_state(state)

        wrong = store.workflows / "wrong-name.json"
        wrong.write_bytes(original)
        os.chmod(wrong, 0o600)
        mismatch = next(error for candidate, _state, error in store.iter_records() if candidate == wrong)
        self.assertEqual(mismatch.code, "corrupt_state")

        before = path.read_bytes()
        with self.assertRaisesRegex(StateError, "exceeds"):
            store._atomic_json(path, {"payload": "x" * MAX_STATE_BYTES})
        self.assertEqual(path.read_bytes(), before)

    def test_state_boundary_rejects_mismatched_attempt_completion_fields(self) -> None:
        self.add("work", 1, "add-work")
        self.mutation("node-update", 2, "ready-work", "--node-id", "work", "--status", "ready")
        self.route("work", 3, "route-work")
        self.mutation(
            "node-update", 4, "claim-work", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-1",
        )
        self.mutation(
            "node-update", 5, "bind-work", "--node-id", "work",
            "--launch-state", "bound", "--child-id", "child-1",
        )
        self.mutation("node-update", 6, "run-work", "--node-id", "work", "--status", "running")

        active = StateStore().load(self.workflow_id)
        active["nodes"]["work"]["attempts"][0]["outcome"] = "premature outcome"
        with self.assertRaisesRegex(StateError, "completion fields"):
            validate_state(active)

        self.mutation(
            "node-update", 7, "fail-work", "--node-id", "work",
            "--status", "failed", "--attempt-outcome", "tests failed",
        )
        terminal = StateStore().load(self.workflow_id)
        terminal["nodes"]["work"]["attempts"][0]["outcome"] = None
        with self.assertRaisesRegex(StateError, "completion fields"):
            validate_state(terminal)

    def test_launch_id_uniqueness_and_recovery_status_wait_for_every_uncertain_launch(self) -> None:
        self.add("a", 1, "add-a")
        self.add("b", 2, "add-b")
        self.add("c", 3, "add-c")
        self.route("a", 4, "route-a")
        self.route("b", 5, "route-b")
        self.route("c", 6, "route-c")
        self.mutation("node-update", 7, "claim-a", "--node-id", "a", "--launch-state", "claimed", "--request-id", "request-shared")
        self.mutation(
            "node-update", 8, "claim-b-duplicate", "--node-id", "b", "--launch-state", "claimed", "--request-id", "request-shared", expected=2
        )
        self.mutation("node-update", 8, "claim-b", "--node-id", "b", "--launch-state", "claimed", "--request-id", "request-b")
        self.mutation("node-update", 9, "uncertain-a", "--node-id", "a", "--launch-state", "reconcile_required")
        self.mutation("node-update", 10, "uncertain-b", "--node-id", "b", "--launch-state", "reconcile_required")
        self.mutation(
            "node-update", 11, "clear-a", "--node-id", "a", "--launch-state", "unclaimed", "--reconciliation", "provider confirms no child"
        )
        self.assertEqual(StateStore().load(self.workflow_id)["controller"]["recovery_status"], "reconcile_required")
        self.mutation(
            "node-update", 12, "bind-b", "--node-id", "b", "--launch-state", "bound", "--child-id", "child-shared", "--reconciliation", "provider confirms child"
        )
        self.assertEqual(StateStore().load(self.workflow_id)["controller"]["recovery_status"], "clean")
        self.mutation("node-update", 13, "claim-c", "--node-id", "c", "--launch-state", "claimed", "--request-id", "request-c")
        self.mutation(
            "node-update", 14, "bind-c-duplicate", "--node-id", "c", "--launch-state", "bound", "--child-id", "child-shared", expected=2
        )
        self.assertEqual(StateStore().load(self.workflow_id)["revision"], 14)

    def test_finish_requires_commit_checkpoint_and_terminal_workflow_cannot_reopen(self) -> None:
        self.add("work", 1, "add-work")
        self.mutation(
            "finish", 2, "finish-short", "--summary", "done", "--validation", "passed", "--commit", "abc", expected=2
        )
        commit = "a" * 40
        self.mutation("node-update", 2, "skip-work", "--node-id", "work", "--status", "skipped")
        self.mutation(
            "node-update", 3, "claim-skipped", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-1", expected=2,
        )
        malformed = StateStore().load(self.workflow_id)
        node = malformed["nodes"]["work"]
        node["launch"].update({
            "state": "claimed", "request_id": "request-1", "claimed_at": malformed["updated_at"],
        })
        node["route"]["attempt"] = 1
        node["attempts"].append({
            "number": 1, "started_at": malformed["updated_at"], "finished_at": None, "outcome": None,
        })
        with self.assertRaisesRegex(StateError, "terminal status cannot retain an active launch"):
            validate_state(malformed)
        self.mutation(
            "finish", 3, "finish-valid", "--summary", "done", "--validation", "passed", "--commit", commit
        )
        self.mutation("resume", 4, "reopen", "--message", "reopen", expected=2)
        state = StateStore().load(self.workflow_id)
        self.assertEqual((state["status"], state["phase"], state["git"]["checkpoint"]), ("completed", "completed", commit))
        state["status"] = "running"
        with self.assertRaisesRegex(StateError, "completed workflow phase"):
            validate_state(state)

    def test_state_lock_removes_only_proven_stale_evidence(self) -> None:
        store = StateStore()
        store._ensure_private_directory(store.locks)
        lock = store.locks / "probe.lock"
        stale = json.dumps({"pid": 999999, "nonce": "a" * 32, "created_at": state_owner.now_iso()}).encode()
        lock.write_bytes(stale)
        os.chmod(lock, 0o600)
        with mock.patch.object(state_owner, "_process_is_proven_dead", return_value=True):
            with store._lock("probe"):
                self.assertNotEqual(lock.read_bytes(), stale)
        self.assertFalse(lock.exists())

        live = json.dumps({"pid": os.getpid(), "nonce": "b" * 32, "created_at": state_owner.now_iso()}).encode()
        lock.write_bytes(live)
        os.chmod(lock, 0o600)
        with self.assertRaisesRegex(StateError, "locked"):
            with store._lock("probe"):
                pass
        self.assertEqual(lock.read_bytes(), live)

        lock.write_bytes(b"{")
        before = lock.read_bytes()
        with mock.patch.object(state_owner, "_process_is_proven_dead", return_value=True):
            with self.assertRaisesRegex(StateError, "locked"):
                with store._lock("probe"):
                    pass
        self.assertEqual(lock.read_bytes(), before)

        with (
            mock.patch.object(state_owner.os, "name", "nt"),
            mock.patch.object(state_owner, "_windows_process_is_proven_dead", return_value=True) as query,
            mock.patch.object(state_owner.os, "kill", side_effect=AssertionError("Windows must not call os.kill")),
        ):
            self.assertTrue(state_owner._process_is_proven_dead(123))
        query.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
