from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "skill" / "scripts" / "lib"
sys.path.insert(0, str(RUNTIME))

from coordinator.cli import state as state_cli  # noqa: E402
from coordinator.state import store as state_owner  # noqa: E402
from coordinator.state.store import (  # noqa: E402
    StateError,
    StateStore,
    graph_diagnostics,
    planning_diagnostics,
    ready_nodes,
    validate_state,
)

DIMENSIONS = {
    "breadth": 1,
    "change_surface": 0,
    "coupling": 1,
    "novelty": 1,
    "verification": 1,
}
AMBIGUITY = {
    "objective": 0,
    "inputs": 0,
    "boundaries": 0,
    "dependencies": 0,
    "acceptance": 0,
}


def manifest(
    node_id: str,
    *,
    dependencies: list[str] | None = None,
    scopes: list[str] | None = None,
    stage: str = "implementation",
    role: str = "implementer",
    dimensions: dict[str, int] | None = None,
) -> dict:
    scopes = list(scopes or [])
    scores = dict(dimensions or DIMENSIONS)
    scores["change_surface"] = 1 if scopes and scores["change_surface"] == 0 else scores["change_surface"]
    return {
        "id": node_id,
        "title": node_id,
        "stage": stage,
        "priority": 50,
        "dependencies": list(dependencies or []),
        "write_scopes": scopes,
        "role": role,
        "model": None,
        "effort": None,
        "acceptance": [f"{node_id} acceptance evidence"],
        "route_rationale": "runtime test manifest",
        "estimated_cost": None,
        "spec": {
            "objective": f"Complete {node_id}",
            "inputs": [],
            "outputs": [f"{node_id} output"],
            "constraints": [],
            "non_goals": [],
            "requirement_ids": [],
            "open_questions": [],
        },
        "assessment": {
            "dimensions": scores,
            "ambiguity_factors": dict(AMBIGUITY),
            "rationale": "bounded runtime test work",
        },
    }


def observation(
    *,
    progress: int,
    total: int = 4,
    ambiguity: int = 0,
    cost: float | None = None,
    confidence: int = 80,
) -> dict:
    dimensions = {name: 0 for name in (
        "breadth", "change_surface", "coupling", "novelty", "verification"
    )}
    remaining = total
    for name in ("breadth", "coupling", "novelty", "verification", "change_surface"):
        value = min(2, remaining)
        dimensions[name] = value
        remaining -= value
    factors = dict(AMBIGUITY)
    factors["objective"] = ambiguity
    return {
        "progress": progress,
        "dimensions": dimensions,
        "ambiguity_factors": factors,
        "estimated_remaining_cost": cost,
        "confidence": confidence,
        "signals": ["runtime telemetry"],
        "note": "bounded observation",
    }


class DynamicRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.session = self.base / "private" / "session.json"
        self.environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
            clear=False,
        )
        self.environment.start()
        self.counter = 0
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(self.session))
        created = self.cli(
            "init",
            "--repo",
            str(self.repo),
            "--task",
            "Exercise dynamic runtime graphs",
            "--session-file",
            str(self.session),
            "--mutation-id",
            "init-dynamic",
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

    def mutate(
        self,
        command: str,
        *arguments: str,
        mutation_id: str | None = None,
        expected_revision: int | None = None,
        session: pathlib.Path | None = None,
        expected: int = 0,
    ) -> dict:
        self.counter += 1
        revision = (
            StateStore().load(self.workflow_id)["revision"]
            if expected_revision is None
            else expected_revision
        )
        return self.cli(
            command,
            "--workflow-id",
            self.workflow_id,
            "--session-file",
            str(session or self.session),
            "--mutation-id",
            mutation_id or f"dynamic-{self.counter:04d}",
            "--expected-revision",
            str(revision),
            *arguments,
            expected=expected,
        )

    def apply(self, *nodes: dict) -> None:
        self.mutate(
            "plan-apply",
            "--plan-json",
            json.dumps({"requirements": [], "nodes": list(nodes)}),
        )

    def start(self, node_id: str) -> None:
        self.mutate(
            "node-route-auto",
            "--node-id",
            node_id,
            "--criticality",
            "3",
            "--determinism",
            "3",
        )
        claim = self.mutate("node-claim", "--node-id", node_id)
        self.mutate(
            "node-start",
            "--node-id",
            node_id,
            "--child-id",
            claim["data"]["suggested_child_id"],
        )

    def complete(self, node_id: str) -> None:
        self.mutate(
            "node-complete",
            "--node-id",
            node_id,
            "--outcome",
            "succeeded",
            "--result",
            f"{node_id} completed",
            "--evidence",
            f"{node_id} evidence",
        )

    def judge(self, judge_id: str, verdict: str) -> dict:
        self.start(judge_id)
        return self.mutate(
            "judge-complete",
            "--node-id",
            judge_id,
            "--verdict",
            verdict,
            "--result",
            f"{judge_id} reviewed",
            "--evidence",
            f"{judge_id} independent evidence",
        )

    def gate_plan(
        self,
        target_id: str,
        *,
        judge_count: int = 3,
        mode: str = "quorum",
        required: int = 2,
        loop: dict | None = None,
    ) -> dict:
        judges = [
            manifest(
                f"{target_id}.judge-{index}",
                stage="validation" if index % 2 else "review",
                role="validator" if index % 2 else "reviewer",
            )
            for index in range(1, judge_count + 1)
        ]
        return {"mode": mode, "required": required, "judges": judges, "loop": loop}

    def expand(
        self,
        parent_id: str,
        *,
        fragments: list[dict],
        join: dict | None,
        workload: str = "heterogeneous",
        shape: str = "auto",
        mutation_id: str | None = None,
        expected_revision: int | None = None,
        expected: int = 0,
    ) -> dict:
        plan = {
            "parent_id": parent_id,
            "reason": f"runtime evidence changed {parent_id}",
            "shape": shape,
            "workload": workload,
            "fragments": fragments,
            "join": join,
        }
        return self.mutate(
            "graph-expand-auto",
            "--plan-json",
            json.dumps(plan),
            mutation_id=mutation_id,
            expected_revision=expected_revision,
            expected=expected,
        )

    def test_schema_v6_loads_in_memory_and_persists_v7_on_next_mutation(self) -> None:
        store = StateStore()
        path = store._state_path(self.workflow_id)
        legacy = copy.deepcopy(store.load(self.workflow_id))
        legacy["schema_version"] = 6
        legacy.pop("runtime_graph")
        path.write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        loaded = store.load(self.workflow_id)
        self.assertEqual(loaded["schema_version"], 7)
        self.assertEqual(loaded["runtime_graph"]["generation"], 0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 6)

        self.mutate(
            "requirement-set",
            "--requirement-id",
            "migration-proof",
            "--text",
            "Persist schema seven",
            "--source",
            "test",
            "--status",
            "active",
        )
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 7)
        self.assertIn("runtime_graph", persisted)

    def test_observations_recalculate_complexity_and_are_monotonic_and_atomic(self) -> None:
        self.apply(manifest("adaptive"), manifest("ambiguous"))
        stable = self.mutate(
            "node-observe",
            "--node-id",
            "adaptive",
            "--observation-json",
            json.dumps(observation(progress=10, total=4)),
        )
        self.assertEqual(stable["data"]["projection"]["recommendation"], "stable")
        split = self.mutate(
            "node-observe",
            "--node-id",
            "adaptive",
            "--observation-json",
            json.dumps(observation(progress=25, total=7, cost=1023, confidence=30)),
        )
        self.assertEqual(split["data"]["projection"]["recommendation"], "split")
        refined = self.mutate(
            "node-observe",
            "--node-id",
            "ambiguous",
            "--observation-json",
            json.dumps(observation(progress=5, total=3, ambiguity=3)),
        )
        self.assertEqual(refined["data"]["projection"]["recommendation"], "refine")
        self.assertEqual(self.cli("next", "--workflow-id", self.workflow_id)["data"]["action"], "reconcile_runtime")

        baseline = StateStore().load(self.workflow_id)
        rejected = self.mutate(
            "node-observe",
            "--node-id",
            "adaptive",
            "--observation-json",
            json.dumps(observation(progress=24, total=7)),
            expected=2,
        )
        self.assertIn("cannot move backwards", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

    def test_runtime_load_reorders_the_critical_path_without_mutating_static_assessment(self) -> None:
        self.apply(manifest("cheap"), manifest("expensive"))
        before = StateStore().load(self.workflow_id)
        static_assessment = copy.deepcopy(before["nodes"]["expensive"]["assessment"])
        self.mutate(
            "node-observe",
            "--node-id",
            "cheap",
            "--observation-json",
            json.dumps(observation(progress=20, total=2, cost=0, confidence=100)),
        )
        self.mutate(
            "node-observe",
            "--node-id",
            "expensive",
            "--observation-json",
            json.dumps(observation(progress=20, total=2, cost=4095, confidence=20)),
        )
        state = StateStore().load(self.workflow_id)
        diagnostics = planning_diagnostics(state)
        self.assertGreater(
            diagnostics["critical_path_load"]["expensive"],
            diagnostics["critical_path_load"]["cheap"],
        )
        self.assertEqual(diagnostics["dispatch_order"][0], "expensive")
        self.assertEqual(state["nodes"]["expensive"]["assessment"], static_assessment)

    def test_reconciler_can_checkpoint_and_split_running_work_then_rewire_downstream(self) -> None:
        self.apply(manifest("running-parent"), manifest("downstream", dependencies=["running-parent"]))
        self.start("running-parent")
        self.mutate(
            "node-observe",
            "--node-id",
            "running-parent",
            "--observation-json",
            json.dumps(observation(progress=45, total=8, cost=100, confidence=35)),
        )
        reconciled = self.mutate("graph-reconcile")
        child_ids = reconciled["data"]["reconciliation"]["child_ids"]
        self.assertEqual(len(child_ids), 2)
        discovery, execution = child_ids
        state = StateStore().load(self.workflow_id)
        parent = state["nodes"]["running-parent"]
        self.assertEqual((parent["status"], parent["assessment"]["state"]), ("skipped", "decomposed"))
        self.assertEqual(parent["attempts"][-1]["outcome"], "adapted at runtime")
        self.assertEqual(parent["launch"]["state"], "terminal")
        self.assertEqual(state["nodes"][execution]["dependencies"], [discovery])
        self.assertEqual(state["nodes"]["downstream"]["dependencies"], [execution])
        self.assertEqual(state["runtime_graph"]["node_metadata"][execution]["graph_path"], ["running-parent", execution])
        self.assertEqual(graph_diagnostics(
            state["nodes"],
            case_sensitive=state["conventions"]["write_scope_case_sensitive"],
            platform=state["conventions"]["platform"],
        )["cycles"], [])

    def test_auto_shape_picker_supports_parallel_pipeline_map_reduce_diamond_fan_in_and_nested_graphs(self) -> None:
        parents = [manifest(name) for name in ("p-par", "p-pipe", "p-map", "p-dia", "p-fan")]
        self.apply(*parents)
        parallel = self.expand(
            "p-par",
            fragments=[manifest("par-a", scopes=["src/par-a"]), manifest("par-b", scopes=["src/par-b"])],
            join=None,
        )
        pipeline = self.expand(
            "p-pipe",
            fragments=[manifest("pipe-a", scopes=["src/shared"]), manifest("pipe-b", scopes=["src/shared"])],
            join=None,
        )
        mapped = self.expand(
            "p-map",
            fragments=[manifest("map-a", scopes=["src/map-a"]), manifest("map-b", scopes=["src/map-b"])],
            join=manifest("map-join", stage="documentation", role="documenter"),
            workload="homogeneous",
        )
        diamond = self.expand(
            "p-dia",
            fragments=[
                manifest("dia-build", scopes=["src/dia"]),
                manifest("dia-review", stage="review", role="reviewer"),
            ],
            join=manifest("dia-join", stage="documentation", role="documenter"),
        )
        fan = self.expand(
            "p-fan",
            fragments=[manifest("fan-a"), manifest("fan-b"), manifest("fan-c")],
            join=manifest("fan-join", stage="documentation", role="documenter"),
        )
        self.assertEqual(parallel["data"]["expansion"]["shape"], "parallel")
        self.assertEqual(pipeline["data"]["expansion"]["shape"], "pipeline")
        self.assertEqual(mapped["data"]["expansion"]["shape"], "map_reduce")
        self.assertEqual(diamond["data"]["expansion"]["shape"], "diamond")
        self.assertEqual(fan["data"]["expansion"]["shape"], "fanout_fanin")

        nested = self.expand(
            "par-a",
            fragments=[
                manifest("nested-a", scopes=["src/par-a/nested-a"]),
                manifest("nested-b", scopes=["src/par-a/nested-b"]),
            ],
            join=None,
        )
        self.assertEqual(nested["data"]["expansion"]["shape"], "parallel")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(
            state["runtime_graph"]["node_metadata"]["nested-a"]["graph_path"],
            ["p-par", "par-a", "nested-a"],
        )
        self.assertEqual(state["nodes"]["pipe-b"]["dependencies"], ["pipe-a"])
        self.assertEqual(set(state["nodes"]["map-join"]["dependencies"]), {"map-a", "map-b"})
        self.assertEqual(graph_diagnostics(
            state["nodes"],
            case_sensitive=state["conventions"]["write_scope_case_sensitive"],
            platform=state["conventions"]["platform"],
        )["cycles"], [])

    def test_expansion_is_atomic_and_idempotent_under_mutation_replay(self) -> None:
        self.apply(manifest("replay-parent"))
        fragments = [manifest("replay-a"), manifest("replay-b")]
        revision = StateStore().load(self.workflow_id)["revision"]
        first = self.expand(
            "replay-parent",
            fragments=fragments,
            join=None,
            mutation_id="expand-replay",
            expected_revision=revision,
        )
        self.assertEqual(first["code"], "runtime_graph_expanded")
        snapshot = StateStore().load(self.workflow_id)
        replay = self.expand(
            "replay-parent",
            fragments=fragments,
            join=None,
            mutation_id="expand-replay",
            expected_revision=revision,
        )
        self.assertEqual(replay["code"], "mutation_reconciled")
        self.assertEqual(StateStore().load(self.workflow_id), snapshot)

        rejected = self.expand(
            "replay-a",
            fragments=[manifest("replay-b"), manifest("new-child")],
            join=None,
            expected=2,
        )
        self.assertIn("unique and new", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), snapshot)

    def test_gate_targets_require_runtime_metadata_and_judges_cannot_own_gates(self) -> None:
        self.apply(manifest("gated"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "gated",
            "--gate-json",
            json.dumps(self.gate_plan("gated", judge_count=2, mode="all", required=2)),
        )
        baseline = StateStore().load(self.workflow_id)
        judge_id = baseline["runtime_graph"]["gates"]["gated"]["judge_ids"][0]
        rejected = self.mutate(
            "judge-gate-add",
            "--node-id",
            judge_id,
            "--gate-json",
            json.dumps(self.gate_plan(judge_id, judge_count=1, mode="all", required=1)),
            expected=2,
        )
        self.assertIn("judge nodes cannot own", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        missing_target_metadata = copy.deepcopy(baseline)
        missing_target_metadata["runtime_graph"]["node_metadata"].pop("gated")
        with self.assertRaisesRegex(StateError, "target metadata"):
            validate_state(missing_target_metadata)

        wrong_stage = copy.deepcopy(baseline)
        wrong_stage["nodes"][judge_id]["stage"] = "implementation"
        with self.assertRaisesRegex(StateError, "judge stage"):
            validate_state(wrong_stage)

    def test_gate_release_stales_authored_dependents_but_preserves_candidate_isolation(self) -> None:
        self.apply(manifest("candidate"), manifest("authored-dependent", dependencies=["candidate"]))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "candidate",
            "--gate-json",
            json.dumps(self.gate_plan("candidate", judge_count=1, mode="all", required=1)),
        )
        self.start("candidate")
        self.complete("candidate")
        judging = StateStore().load(self.workflow_id)
        self.assertEqual(judging["nodes"]["candidate"]["status"], "judging")
        snapshot = state_owner._dependency_snapshot("candidate", judging["nodes"]["candidate"])
        self.assertIsNone(snapshot["result"])
        self.assertIsNone(snapshot["evidence"])
        self.assertEqual(snapshot["disposition"], "nonterminal")

        judge_id = judging["runtime_graph"]["gates"]["candidate"]["judge_ids"][0]
        self.judge(judge_id, "pass")
        released = StateStore().load(self.workflow_id)
        self.assertEqual(released["nodes"]["candidate"]["status"], "done")
        self.assertEqual(released["nodes"]["authored-dependent"]["assessment"]["state"], "stale")
        self.assertEqual(self.cli("next", "--workflow-id", self.workflow_id)["data"]["action"], "reassess")

    def test_quorum_judge_gate_holds_completion_until_all_verdicts_resolve(self) -> None:
        self.apply(manifest("gated"))
        plan = self.gate_plan("gated", judge_count=3, mode="quorum", required=2)
        self.mutate("judge-gate-add", "--node-id", "gated", "--gate-json", json.dumps(plan))
        self.start("gated")
        self.complete("gated")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["gated"]["status"], "judging")
        self.assertEqual(state["runtime_graph"]["gates"]["gated"]["status"], "pending")
        self.assertEqual(set(ready_nodes(state)), set(plan_judge["id"] for plan_judge in plan["judges"]))

        self.judge("gated.judge-1", "pass")
        self.judge("gated.judge-2", "fail")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["gated"]["status"], "judging")
        self.judge("gated.judge-3", "pass")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["gated"]["status"], "done")
        self.assertEqual(state["runtime_graph"]["gates"]["gated"]["status"], "passed")
        self.assertEqual(graph_diagnostics(
            state["nodes"],
            case_sensitive=state["conventions"]["write_scope_case_sensitive"],
            platform=state["conventions"]["platform"],
        )["cycles"], [])

    def test_runtime_judges_require_judge_complete_to_resolve_their_gate(self) -> None:
        self.apply(manifest("protected-gate"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "protected-gate",
            "--gate-json",
            json.dumps(
                self.gate_plan(
                    "protected-gate",
                    judge_count=1,
                    mode="all",
                    required=1,
                )
            ),
        )
        self.start("protected-gate")
        self.complete("protected-gate")
        judge_id = "protected-gate.judge-1"
        self.start(judge_id)
        baseline = StateStore().load(self.workflow_id)

        rejected_completion = self.mutate(
            "node-complete",
            "--node-id",
            judge_id,
            "--outcome",
            "succeeded",
            "--result",
            "generic completion",
            "--evidence",
            "generic completion evidence",
            expected=2,
        )
        self.assertIn(
            "runtime judge nodes must use judge-complete",
            rejected_completion["data"]["message"],
        )
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        rejected_update = self.mutate(
            "node-update",
            "--node-id",
            judge_id,
            "--status",
            "done",
            "--result",
            "generic status update",
            "--evidence",
            "generic status update evidence",
            expected=2,
        )
        self.assertIn(
            "runtime judge nodes must use judge-complete",
            rejected_update["data"]["message"],
        )
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        completed = self.mutate(
            "judge-complete",
            "--node-id",
            judge_id,
            "--verdict",
            "pass",
            "--result",
            "judge reviewed the candidate",
            "--evidence",
            "independent judge evidence",
        )
        self.assertEqual(completed["data"]["judgment"]["gate_status"], "passed")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"][judge_id]["status"], "done")
        self.assertEqual(state["nodes"]["protected-gate"]["status"], "done")
        self.assertEqual(
            state["runtime_graph"]["gates"]["protected-gate"]["verdicts"],
            {judge_id: "pass"},
        )

    def test_failed_gate_can_materialize_a_bounded_logical_cycle_as_acyclic_iterations(self) -> None:
        self.apply(manifest("loop-target"), manifest("after-loop", dependencies=["loop-target"]))
        plan = self.gate_plan(
            "loop-target",
            judge_count=1,
            mode="all",
            required=1,
            loop={"id": "quality-loop", "max_iterations": 2},
        )
        self.mutate("judge-gate-add", "--node-id", "loop-target", "--gate-json", json.dumps(plan))
        self.start("loop-target")
        self.complete("loop-target")
        first_failure = self.judge("loop-target.judge-1", "fail")
        next_iteration = first_failure["data"]["judgment"]["next_iteration"]
        replacement = next_iteration["target_id"]
        replacement_judge = next_iteration["judge_ids"][0]
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["loop-target"]["status"], "skipped")
        self.assertEqual(state["nodes"]["loop-target"]["superseded_by"], replacement)
        self.assertEqual(state["nodes"]["after-loop"]["dependencies"], [replacement])
        self.assertEqual(state["runtime_graph"]["loops"]["quality-loop"]["iteration"], 2)
        self.assertEqual(graph_diagnostics(
            state["nodes"],
            case_sensitive=state["conventions"]["write_scope_case_sensitive"],
            platform=state["conventions"]["platform"],
        )["cycles"], [])

        self.start(replacement)
        self.complete(replacement)
        self.judge(replacement_judge, "fail")
        state = StateStore().load(self.workflow_id)
        loop = state["runtime_graph"]["loops"]["quality-loop"]
        self.assertEqual(loop["status"], "exhausted")
        self.assertEqual(len(loop["history"]), 2)
        self.assertEqual(state["nodes"][replacement]["status"], "failed")
        self.assertEqual(graph_diagnostics(
            state["nodes"],
            case_sensitive=state["conventions"]["write_scope_case_sensitive"],
            platform=state["conventions"]["platform"],
        )["cycles"], [])

    def test_second_iteration_can_pass_and_release_rewired_downstream_work(self) -> None:
        self.apply(manifest("retry-target"), manifest("retry-downstream", dependencies=["retry-target"]))
        plan = self.gate_plan(
            "retry-target",
            judge_count=1,
            mode="all",
            required=1,
            loop={"id": "retry-loop", "max_iterations": 3},
        )
        self.mutate("judge-gate-add", "--node-id", "retry-target", "--gate-json", json.dumps(plan))
        self.start("retry-target")
        self.complete("retry-target")
        failed = self.judge("retry-target.judge-1", "fail")
        iteration = failed["data"]["judgment"]["next_iteration"]
        self.start(iteration["target_id"])
        self.complete(iteration["target_id"])
        self.judge(iteration["judge_ids"][0], "pass")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["runtime_graph"]["loops"]["retry-loop"]["status"], "passed")
        self.assertEqual(state["nodes"][iteration["target_id"]]["status"], "done")
        self.assertIn("retry-downstream", ready_nodes(state))

    def test_abort_resolves_pending_gates_and_active_loops_without_corrupting_state(self) -> None:
        self.apply(manifest("abort-gate"))
        plan = self.gate_plan(
            "abort-gate",
            judge_count=1,
            mode="all",
            required=1,
            loop={"id": "abort-loop", "max_iterations": 2},
        )
        self.mutate("judge-gate-add", "--node-id", "abort-gate", "--gate-json", json.dumps(plan))
        self.start("abort-gate")
        self.complete("abort-gate")
        aborted = self.mutate("abort", "--reason", "operator accepted partial result")
        self.assertEqual(aborted["code"], "workflow_aborted")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(state["runtime_graph"]["gates"]["abort-gate"]["status"], "failed")
        self.assertEqual(state["runtime_graph"]["loops"]["abort-loop"]["status"], "exhausted")
        validate_state(state)

    def test_runtime_topology_limits_reject_oversized_expansion_gate_and_loop(self) -> None:
        self.apply(manifest("bounded-expansion"), manifest("bounded-gate"), manifest("bounded-loop"))
        baseline = StateStore().load(self.workflow_id)

        too_many_fragments = [manifest(f"fragment-{index:02d}") for index in range(17)]
        rejected_expansion = self.expand(
            "bounded-expansion",
            fragments=too_many_fragments,
            join=None,
            expected=2,
        )
        self.assertIn("2..16", rejected_expansion["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        too_many_judges = self.gate_plan(
            "bounded-gate", judge_count=9, mode="quorum", required=5
        )
        rejected_gate = self.mutate(
            "judge-gate-add",
            "--node-id",
            "bounded-gate",
            "--gate-json",
            json.dumps(too_many_judges),
            expected=2,
        )
        self.assertIn("1..8", rejected_gate["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        oversized_loop = self.gate_plan(
            "bounded-loop",
            judge_count=1,
            mode="all",
            required=1,
            loop={"id": "too-long", "max_iterations": 17},
        )
        rejected_loop = self.mutate(
            "judge-gate-add",
            "--node-id",
            "bounded-loop",
            "--gate-json",
            json.dumps(oversized_loop),
            expected=2,
        )
        self.assertIn("2..16", rejected_loop["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

    def test_runtime_validator_rejects_malformed_sections_as_state_errors(self) -> None:
        state = StateStore().load(self.workflow_id)
        malformed = copy.deepcopy(state)
        malformed["runtime_graph"]["gates"] = []
        with self.assertRaises(StateError):
            validate_state(malformed)

        malformed = copy.deepcopy(state)
        malformed["runtime_graph"]["node_metadata"] = {
            "ghost": {
                "kind": "judge",
                "graph_path": ["ghost"],
                "shape": None,
                "iteration": 1,
                "judge_for": "missing",
                "loop_id": None,
                "generated_by": None,
            }
        }
        with self.assertRaises(StateError):
            validate_state(malformed)

    def test_persisted_observation_history_rejects_backward_progress(self) -> None:
        self.apply(manifest("history"))
        self.mutate(
            "node-observe",
            "--node-id",
            "history",
            "--observation-json",
            json.dumps(observation(progress=30, total=3)),
        )
        self.mutate(
            "node-observe",
            "--node-id",
            "history",
            "--observation-json",
            json.dumps(observation(progress=60, total=2)),
        )
        state = StateStore().load(self.workflow_id)
        state["runtime_graph"]["observations"]["history"][1]["progress"] = 20
        with self.assertRaisesRegex(StateError, "progress cannot move backwards"):
            validate_state(state)

    def test_projection_is_derived_from_latest_observation_and_deep_json_is_bounded(self) -> None:
        self.apply(manifest("derived-projection"))
        self.mutate(
            "node-observe",
            "--node-id",
            "derived-projection",
            "--observation-json",
            json.dumps(observation(progress=20, total=8)),
        )
        tampered = copy.deepcopy(StateStore().load(self.workflow_id))
        tampered["runtime_graph"]["projections"]["derived-projection"].update(
            {
                "recommendation": "stable",
                "reason": "forged projection",
            }
        )
        with self.assertRaisesRegex(StateError, "policy-derived projection"):
            validate_state(tampered)

        with mock.patch.object(state_owner.json, "dumps", side_effect=RecursionError):
            with self.assertRaisesRegex(StateError, "canonical JSON"):
                state_owner._json_bytes({"nested": {}})

    def test_reconcile_uses_live_dimensions_and_retargets_a_configured_loop_gate(self) -> None:
        zero = {name: 0 for name in DIMENSIONS}
        self.apply(manifest("gated-adaptive", dimensions=zero))
        plan = self.gate_plan(
            "gated-adaptive",
            judge_count=1,
            mode="all",
            required=1,
            loop={"id": "adaptive-loop", "max_iterations": 2},
        )
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "gated-adaptive",
            "--gate-json",
            json.dumps(plan),
        )
        self.mutate(
            "node-observe",
            "--node-id",
            "gated-adaptive",
            "--observation-json",
            json.dumps(observation(progress=15, total=8, confidence=25)),
        )
        reconciled = self.mutate("graph-reconcile")
        discovery, execution = reconciled["data"]["reconciliation"]["child_ids"]
        state = StateStore().load(self.workflow_id)
        self.assertNotIn("gated-adaptive", state["runtime_graph"]["gates"])
        self.assertIn(execution, state["runtime_graph"]["gates"])
        self.assertEqual(
            state["runtime_graph"]["node_metadata"]["gated-adaptive.judge-1"]["judge_for"],
            execution,
        )
        self.assertEqual(
            state["nodes"]["gated-adaptive.judge-1"]["dependencies"],
            [execution],
        )
        loop = state["runtime_graph"]["loops"]["adaptive-loop"]
        self.assertEqual((loop["current_node_id"], loop["gate_targets"]), (execution, [execution]))
        self.assertGreater(state["nodes"][discovery]["assessment"]["total"], 0)
        self.assertGreater(state["nodes"][execution]["assessment"]["total"], 0)

        self.start(discovery)
        self.complete(discovery)
        self.start(execution)
        self.complete(execution)
        self.judge("gated-adaptive.judge-1", "pass")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["runtime_graph"]["loops"]["adaptive-loop"]["status"], "passed")

    def test_gated_multi_exit_expansion_requires_a_join_and_is_atomic(self) -> None:
        self.apply(manifest("gated-fanout"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "gated-fanout",
            "--gate-json",
            json.dumps(self.gate_plan("gated-fanout", judge_count=1, mode="all", required=1)),
        )
        baseline = StateStore().load(self.workflow_id)
        rejected = self.expand(
            "gated-fanout",
            fragments=[manifest("fanout-b"), manifest("fanout-a")],
            join=None,
            expected=2,
        )
        self.assertIn("requires one completion exit", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

    def test_auto_shape_uses_stable_fragment_order(self) -> None:
        self.apply(manifest("stable-order"))
        expanded = self.expand(
            "stable-order",
            fragments=[
                manifest("z-last", scopes=["src/shared"]),
                manifest("a-first", scopes=["src/shared"]),
            ],
            join=None,
        )
        self.assertEqual(expanded["data"]["expansion"]["shape"], "pipeline")
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["a-first"]["dependencies"], [])
        self.assertEqual(state["nodes"]["z-last"]["dependencies"], ["a-first"])

    def test_auto_diamond_is_binary_while_explicit_diamond_can_have_more_branches(self) -> None:
        self.apply(manifest("auto-diamond"), manifest("explicit-diamond"))
        auto = self.expand(
            "auto-diamond",
            fragments=[
                manifest("auto-review-a", stage="review", role="reviewer"),
                manifest("auto-review-b", stage="validation", role="validator"),
                manifest("auto-review-c", stage="review", role="reviewer"),
            ],
            join=manifest("auto-review-join", stage="integration", role="validator"),
        )
        explicit = self.expand(
            "explicit-diamond",
            fragments=[
                manifest("explicit-review-a", stage="review", role="reviewer"),
                manifest("explicit-review-b", stage="validation", role="validator"),
                manifest("explicit-review-c", stage="review", role="reviewer"),
            ],
            join=manifest("explicit-review-join", stage="integration", role="validator"),
            shape="diamond",
        )
        self.assertEqual(auto["data"]["expansion"]["shape"], "fanout_fanin")
        self.assertEqual(explicit["data"]["expansion"]["shape"], "diamond")


    def test_arbitrary_dag_cycle_is_rejected_before_materialization(self) -> None:
        self.apply(manifest("cyclic-parent"), manifest("cyclic-downstream", dependencies=["cyclic-parent"]))
        first = manifest("cycle-a", dependencies=["cycle-b"])
        second = manifest("cycle-b", dependencies=["cycle-a"])
        baseline = StateStore().load(self.workflow_id)
        rejected = self.expand(
            "cyclic-parent",
            fragments=[first, second],
            join=None,
            shape="dag",
            expected=2,
        )
        self.assertIn("internal dependency cycle", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

    def test_auto_shape_supports_an_arbitrary_acyclic_subgraph_and_derives_its_exits(self) -> None:
        self.apply(
            manifest("complex-parent"),
            manifest("complex-downstream", dependencies=["complex-parent"]),
        )
        expanded = self.expand(
            "complex-parent",
            fragments=[
                manifest("dag-d", dependencies=["dag-b", "dag-c"]),
                manifest("dag-b", dependencies=["dag-a"]),
                manifest("dag-a"),
                manifest("dag-c", dependencies=["dag-a"]),
            ],
            join=None,
        )
        self.assertEqual(expanded["data"]["expansion"]["shape"], "dag")
        self.assertEqual(expanded["data"]["expansion"]["exit_ids"], ["dag-d"])
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["complex-downstream"]["dependencies"], ["dag-d"])
        self.assertEqual(
            graph_diagnostics(
                state["nodes"],
                case_sensitive=state["conventions"]["write_scope_case_sensitive"],
                platform=state["conventions"]["platform"],
            )["cycles"],
            [],
        )

    def test_loop_history_targets_and_judges_are_cross_validated(self) -> None:
        self.apply(manifest("audit-loop"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "audit-loop",
            "--gate-json",
            json.dumps(
                self.gate_plan(
                    "audit-loop",
                    judge_count=1,
                    mode="all",
                    required=1,
                    loop={"id": "audit-loop-id", "max_iterations": 3},
                )
            ),
        )
        self.start("audit-loop")
        self.complete("audit-loop")
        first_judge = StateStore().load(self.workflow_id)["runtime_graph"]["gates"]["audit-loop"]["judge_ids"][0]
        self.judge(first_judge, "fail")
        state = StateStore().load(self.workflow_id)
        loop = state["runtime_graph"]["loops"]["audit-loop-id"]
        first_target, second_target = loop["gate_targets"]

        wrong_root = copy.deepcopy(state)
        root_id = loop["root_node_id"]
        wrong_root["runtime_graph"]["node_metadata"][root_id]["loop_id"] = None
        for root_judge in state["runtime_graph"]["gates"][first_target]["judge_ids"]:
            wrong_root["runtime_graph"]["node_metadata"][root_judge]["loop_id"] = None
        with self.assertRaisesRegex(StateError, "root_node_id metadata"):
            validate_state(wrong_root)

        wrong_iteration = copy.deepcopy(state)
        wrong_iteration["runtime_graph"]["node_metadata"][first_target]["iteration"] = 2
        first_iteration_judge = state["runtime_graph"]["gates"][first_target]["judge_ids"][0]
        wrong_iteration["runtime_graph"]["node_metadata"][first_iteration_judge]["iteration"] = 2
        with self.assertRaisesRegex(StateError, "gate_targets metadata"):
            validate_state(wrong_iteration)

        wrong_history = copy.deepcopy(state)
        wrong_history["runtime_graph"]["loops"]["audit-loop-id"]["history"][0]["gate_status"] = "passed"
        with self.assertRaisesRegex(StateError, "persisted gate outcome"):
            validate_state(wrong_history)

        second_judge = state["runtime_graph"]["gates"][second_target]["judge_ids"][0]
        wrong_judge = copy.deepcopy(state)
        wrong_judge["runtime_graph"]["node_metadata"][second_judge]["loop_id"] = None
        with self.assertRaisesRegex(StateError, "judge loop metadata"):
            validate_state(wrong_judge)

    def test_nested_loop_iterations_preserve_the_outer_graph_path(self) -> None:
        self.apply(manifest("outer"))
        self.expand(
            "outer",
            fragments=[manifest("nested-loop"), manifest("nested-sibling")],
            join=None,
        )
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "nested-loop",
            "--gate-json",
            json.dumps(
                self.gate_plan(
                    "nested-loop",
                    judge_count=1,
                    mode="all",
                    required=1,
                    loop={"id": "nested-quality", "max_iterations": 2},
                )
            ),
        )
        self.start("nested-loop")
        self.complete("nested-loop")
        failed = self.judge("nested-loop.judge-1", "fail")
        replacement = failed["data"]["judgment"]["next_iteration"]["target_id"]
        replacement_judge = failed["data"]["judgment"]["next_iteration"]["judge_ids"][0]
        state = StateStore().load(self.workflow_id)
        self.assertEqual(
            state["runtime_graph"]["node_metadata"][replacement]["graph_path"],
            ["outer", replacement],
        )
        self.assertEqual(
            state["runtime_graph"]["node_metadata"][replacement_judge]["graph_path"],
            ["outer", replacement, replacement_judge],
        )

    def test_judging_nodes_reject_generic_status_updates_without_internal_errors(self) -> None:
        self.apply(manifest("status-gated"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "status-gated",
            "--gate-json",
            json.dumps(self.gate_plan("status-gated", judge_count=1, mode="all", required=1)),
        )
        self.start("status-gated")
        self.complete("status-gated")
        rejected = self.mutate(
            "node-update",
            "--node-id",
            "status-gated",
            "--status",
            "failed",
            expected=2,
        )
        self.assertIn("invalid node transition judging -> failed", rejected["data"]["message"])

    def test_configured_gate_cannot_be_bypassed_or_nested_inside_an_owned_loop(self) -> None:
        self.apply(manifest("loop-root"), manifest("bypass-target"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "loop-root",
            "--gate-json",
            json.dumps(
                self.gate_plan(
                    "loop-root",
                    judge_count=1,
                    mode="all",
                    required=1,
                    loop={"id": "owned-loop", "max_iterations": 2},
                )
            ),
        )
        self.mutate(
            "node-observe",
            "--node-id",
            "loop-root",
            "--observation-json",
            json.dumps(observation(progress=20, total=8)),
        )
        reconciled = self.mutate("graph-reconcile")
        discovery = reconciled["data"]["reconciliation"]["child_ids"][0]
        baseline = StateStore().load(self.workflow_id)
        rejected = self.mutate(
            "judge-gate-add",
            "--node-id",
            discovery,
            "--gate-json",
            json.dumps(self.gate_plan(discovery, judge_count=1, mode="all", required=1)),
            expected=2,
        )
        self.assertIn("owned by a feedback loop", rejected["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)

        missing_loop = copy.deepcopy(baseline)
        missing_loop["runtime_graph"]["node_metadata"][discovery]["loop_id"] = "missing-loop"
        with self.assertRaisesRegex(StateError, "references an unknown loop"):
            validate_state(missing_loop)

        self.mutate(
            "judge-gate-add",
            "--node-id",
            "bypass-target",
            "--gate-json",
            json.dumps(self.gate_plan("bypass-target", judge_count=1, mode="all", required=1)),
        )
        self.start("bypass-target")
        self.complete("bypass-target")
        bypass = StateStore().load(self.workflow_id)
        bypass["runtime_graph"]["gates"]["bypass-target"]["status"] = "configured"
        with self.assertRaisesRegex(StateError, "configured gate requires unresolved"):
            validate_state(bypass)

    def test_runtime_expansion_cannot_bypass_an_active_parent_blocker(self) -> None:
        self.apply(manifest("blocked-adaptive"))
        blocked = self.mutate(
            "block",
            "--node-id",
            "blocked-adaptive",
            "--reason",
            "external prerequisite is unavailable",
            "--needed",
            "operator-provided prerequisite",
        )
        blocker_id = blocked["data"]["blocker"]["id"]
        self.mutate(
            "node-observe",
            "--node-id",
            "blocked-adaptive",
            "--observation-json",
            json.dumps(observation(progress=15, total=8, confidence=20)),
        )
        reconciled = self.mutate("graph-reconcile")
        child_ids = reconciled["data"]["reconciliation"]["child_ids"]
        state = StateStore().load(self.workflow_id)
        self.assertTrue(set(child_ids).issubset(state_owner._active_blocked_node_ids(state)))
        self.assertFalse(set(child_ids) & set(ready_nodes(state)))

        rejected = self.mutate(
            "node-route-auto",
            "--node-id",
            child_ids[0],
            "--criticality",
            "3",
            "--determinism",
            "3",
            expected=2,
        )
        self.assertIn("blocked", rejected["data"]["message"])
        self.assertEqual(self.cli("next", "--workflow-id", self.workflow_id)["data"]["action"], "resolve_blocker")

        self.mutate(
            "unblock",
            "--blocker-id",
            blocker_id,
            "--resolution",
            "prerequisite supplied",
        )
        state = StateStore().load(self.workflow_id)
        self.assertFalse(set(child_ids) & state_owner._active_blocked_node_ids(state))
        self.assertIn(child_ids[0], ready_nodes(state))


    def test_takeover_reconciles_a_running_judge_without_bypassing_the_gate(self) -> None:
        self.apply(manifest("recover-gate"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "recover-gate",
            "--gate-json",
            json.dumps(self.gate_plan("recover-gate", judge_count=1, mode="all", required=1)),
        )
        self.start("recover-gate")
        self.complete("recover-gate")
        judge_id = "recover-gate.judge-1"
        self.start(judge_id)
        child_id = StateStore().load(self.workflow_id)["nodes"][judge_id]["launch"]["child_id"]

        second_session = self.base / "private" / "takeover.json"
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(second_session))
        self.mutate("controller-takeover", session=second_session)
        selected = self.cli("next", "--workflow-id", self.workflow_id)["data"]
        self.assertEqual(selected["action"], "resume")
        self.mutate(
            "resume",
            "--message",
            "resume after controller takeover",
            session=second_session,
        )
        selected = self.cli("next", "--workflow-id", self.workflow_id)["data"]
        self.assertEqual((selected["action"], selected["node_ids"]), ("reconcile", [judge_id]))
        self.assertIn("child_id for bound or running", selected["required"])
        self.assertIn("status and attempt_outcome for terminal", selected["required"])

        self.mutate(
            "node-update",
            "--node-id",
            judge_id,
            "--launch-state",
            "running",
            "--child-id",
            child_id,
            "--reconciliation",
            "provider confirms the judge is still running",
            session=second_session,
        )
        self.mutate(
            "judge-complete",
            "--node-id",
            judge_id,
            "--verdict",
            "pass",
            "--result",
            "judge recovered and reviewed",
            "--evidence",
            "provider identity and independent review evidence verified",
            session=second_session,
        )
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["recover-gate"]["status"], "done")
        self.assertEqual(state["runtime_graph"]["gates"]["recover-gate"]["status"], "passed")
        self.assertEqual(state["controller"]["recovery_status"], "clean")


    def test_terminal_gate_validation_requires_complete_policy_consistent_verdicts(self) -> None:
        self.apply(manifest("tamper-gate"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "tamper-gate",
            "--gate-json",
            json.dumps(self.gate_plan("tamper-gate", judge_count=2, mode="quorum", required=1)),
        )
        self.start("tamper-gate")
        self.complete("tamper-gate")
        self.judge("tamper-gate.judge-1", "pass")
        malformed = copy.deepcopy(StateStore().load(self.workflow_id))
        gate = malformed["runtime_graph"]["gates"]["tamper-gate"]
        gate["status"] = "passed"
        gate["resolved_at"] = state_owner.now_iso()
        malformed["nodes"]["tamper-gate"]["status"] = "done"
        for judge_id in gate["judge_ids"]:
            state_owner._refresh_node_assessment(malformed, judge_id)
        with self.assertRaisesRegex(StateError, "requires every configured verdict"):
            validate_state(malformed)

    def test_runtime_control_plane_fences_judges_and_active_gates_from_generic_rewrites(self) -> None:
        self.apply(manifest("controlled"))
        self.mutate(
            "judge-gate-add",
            "--node-id",
            "controlled",
            "--gate-json",
            json.dumps(self.gate_plan("controlled", judge_count=1, mode="all", required=1)),
        )
        baseline = StateStore().load(self.workflow_id)
        observed = self.mutate(
            "node-observe",
            "--node-id",
            "controlled.judge-1",
            "--observation-json",
            json.dumps(observation(progress=1, total=8)),
            expected=2,
        )
        self.assertIn("cannot be structurally adapted", observed["data"]["message"])

        split = {
            "parent_id": "controlled",
            "reason": "generic split must not bypass its gate",
            "children": [],
            "coverage": {"requirements": {}, "outputs": {}, "acceptance": {}},
            "dependent_replacements": {},
        }
        rejected_split = self.mutate(
            "node-split",
            "--plan-json",
            json.dumps(split),
            expected=2,
        )
        self.assertIn("runtime-controlled work", rejected_split["data"]["message"])

        replan = {
            "reason": "generic replan must not detach a judge",
            "operations": [
                {
                    "op": "dependency_remove",
                    "node_id": "controlled.judge-1",
                    "dependency": "controlled",
                }
            ],
        }
        rejected_replan = self.mutate(
            "graph-replan",
            "--plan-json",
            json.dumps(replan),
            expected=2,
        )
        self.assertIn("runtime-controlled work", rejected_replan["data"]["message"])
        self.assertEqual(StateStore().load(self.workflow_id), baseline)


if __name__ == "__main__":
    unittest.main()
