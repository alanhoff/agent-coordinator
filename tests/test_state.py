from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import coordinator_state  # noqa: E402


class CoordinatorStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="coordinator-state-test-")
        self.base = pathlib.Path(self.temporary.name)
        self.state_root = self.base / "state"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = mock.patch.dict(
            os.environ, {"COORDINATOR_TMP_ROOT": str(self.state_root)}, clear=False
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def run_main(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = coordinator_state.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def init(self, task: str = "Build adaptive feature") -> str:
        code, output, error = self.run_main(
            ["init", "--repo", str(self.repo), "--task", task, "--json"]
        )
        self.assertEqual(code, 0, error)
        return json.loads(output)["workflow_id"]

    def add_node(self, workflow_id: str, node_id: str, dependencies=(), priority=50):
        args = [
            "node-add", "--workflow-id", workflow_id,
            "--node-id", node_id,
            "--stage", "implementation",
            "--title", node_id,
            "--agent-type", "implementer",
            "--model", "gpt-5.6-terra",
            "--reasoning-effort", "high",
            "--routing-rationale", "Current bounded route",
            "--priority", str(priority),
            "--write-scope", f"src/{node_id}.py",
            "--acceptance-criterion", "Focused validation passes",
            "--output-contract", f"Produce the validated {node_id} result",
            "--validation-command", "python -m unittest",
        ]
        for dependency in dependencies:
            args.extend(["--dependency", dependency])
        args.append("--json")
        code, output, error = self.run_main(args)
        self.assertEqual(code, 0, error)
        return json.loads(output)

    def state(self, workflow_id: str):
        return coordinator_state.load_state(coordinator_state.temp_root(), workflow_id)

    def test_exact_task_resumes_and_abort_resume_is_recoverable(self) -> None:
        workflow_id = self.init("Same task")
        second = self.init("Same task")
        self.assertEqual(workflow_id, second)
        self.add_node(workflow_id, "work")
        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--status", "running", "--increment-attempt", "--task-name", "work_1", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, error = self.run_main([
            "abort", "--workflow-id", workflow_id, "--reason", "operator interruption", "--json",
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.state(workflow_id)["nodes"]["work"]["status"], "interrupted")
        code, _, error = self.run_main([
            "resume", "--workflow-id", workflow_id, "--message", "continue", "--json",
        ])
        self.assertEqual(code, 0, error)
        self.assertIn(self.state(workflow_id)["status"], {"planning", "running"})

    def test_task_and_replan_files_support_shell_sensitive_cross_platform_input(self) -> None:
        task_file = self.base / "task input with spaces.txt"
        task_text = "Implement A & B with quoted value \"x\"\nand preserve multiline context"
        task_file.write_text(task_text, encoding="utf-8")
        code, output, error = self.run_main([
            "init", "--repo", str(self.repo), "--task-file", str(task_file), "--json",
        ])
        self.assertEqual(code, 0, error)
        workflow_id = json.loads(output)["workflow_id"]
        self.assertEqual(self.state(workflow_id)["task"], task_text)
        self.add_node(workflow_id, "a")
        self.add_node(workflow_id, "b", ["a"])

        plan_file = self.base / "graph plan with spaces.json"
        plan_file.write_text(json.dumps({
            "reason": "Validation changed priority",
            "operations": [
                {"op": "patch", "node_id": "b", "changes": {"priority": 97}},
            ],
        }), encoding="utf-8")
        code, _, error = self.run_main([
            "graph-replan", "--workflow-id", workflow_id,
            "--plan-file", str(plan_file), "--json",
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.state(workflow_id)["nodes"]["b"]["priority"], 97)

    def test_windows_write_scopes_are_case_insensitive(self) -> None:
        with mock.patch.object(coordinator_state.os, "name", "nt"):
            self.assertTrue(coordinator_state._scopes_overlap("Src/Chat.py", "src/chat.py"))
            self.assertTrue(coordinator_state._scopes_overlap("SRC/**", "src/sub/file.py"))
            self.assertFalse(coordinator_state._scopes_overlap("src/a.py", "src/b.py"))

    def test_cycle_rejection_is_atomic(self) -> None:
        workflow_id = self.init()
        self.add_node(workflow_id, "a")
        self.add_node(workflow_id, "b", ["a"])
        self.add_node(workflow_id, "c", ["b"])
        before = self.state(workflow_id)
        code, _, _ = self.run_main([
            "dependency-add", "--workflow-id", workflow_id,
            "--node-id", "a", "--dependency", "c",
            "--reason", "attempt a cycle", "--json",
        ])
        self.assertEqual(code, 2)
        after = self.state(workflow_id)
        self.assertEqual(before["nodes"], after["nodes"])
        self.assertEqual(before["graph_revision"], after["graph_revision"])

    def test_supersede_rewires_dependents_and_records_revision(self) -> None:
        workflow_id = self.init()
        self.add_node(workflow_id, "research")
        self.add_node(workflow_id, "old_impl", ["research"])
        self.add_node(workflow_id, "docs", ["old_impl"])
        self.add_node(workflow_id, "new_impl", ["research"], priority=90)
        before_revision = self.state(workflow_id)["graph_revision"]
        code, output, error = self.run_main([
            "node-supersede", "--workflow-id", workflow_id,
            "--node-id", "old_impl", "--replacement", "new_impl",
            "--reason", "repository evidence changed the boundary", "--json",
        ])
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        state = self.state(workflow_id)
        self.assertGreater(result["graph_revision"], before_revision)
        self.assertEqual(state["nodes"]["old_impl"]["status"], "skipped")
        self.assertEqual(state["nodes"]["old_impl"]["superseded_by"], "new_impl")
        self.assertIn("old_impl", state["nodes"]["new_impl"]["supersedes"])
        self.assertEqual(state["nodes"]["docs"]["dependencies"], ["new_impl"])

    def test_atomic_batch_replan_rolls_back_invalid_plan(self) -> None:
        workflow_id = self.init()
        self.add_node(workflow_id, "a")
        self.add_node(workflow_id, "b", ["a"])
        before = self.state(workflow_id)
        plan = {
            "reason": "invalid coupled edit",
            "operations": [
                {"op": "patch", "node_id": "a", "changes": {"priority": 99}},
                {"op": "remove", "node_id": "a"},
            ],
        }
        code, _, _ = self.run_main([
            "graph-replan", "--workflow-id", workflow_id,
            "--plan-json", json.dumps(plan), "--json",
        ])
        self.assertEqual(code, 2)
        after = self.state(workflow_id)
        self.assertEqual(before["nodes"], after["nodes"])
        self.assertEqual(before["graph_revision"], after["graph_revision"])

    def test_patch_priority_controls_ready_order_and_running_work_is_immutable(self) -> None:
        workflow_id = self.init()
        self.add_node(workflow_id, "low", priority=10)
        self.add_node(workflow_id, "high", priority=90)
        diagnostics_code, diagnostics_output, diagnostics_error = self.run_main([
            "graph-validate", "--workflow-id", workflow_id, "--json"
        ])
        self.assertEqual(diagnostics_code, 0, diagnostics_error)
        self.assertEqual(json.loads(diagnostics_output)["ready_nodes"], ["high", "low"])

        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "high",
            "--status", "running", "--increment-attempt", "--agent-id", "agent-123", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, _ = self.run_main([
            "node-patch", "--workflow-id", workflow_id, "--node-id", "high",
            "--priority", "1", "--reason", "stale plan", "--json",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(self.state(workflow_id)["nodes"]["high"]["priority"], 90)

    def test_node_scoped_blocker_leaves_independent_branch_ready(self) -> None:
        workflow_id = self.init()
        self.add_node(workflow_id, "blocked_branch", priority=90)
        self.add_node(workflow_id, "independent", priority=80)
        code, output, error = self.run_main([
            "block", "--workflow-id", workflow_id,
            "--node-id", "blocked_branch", "--confidence", "medium",
            "--reason", "Two incompatible public contracts",
            "--needed", "Choose the stable contract",
            "--recommendation", "Keep the current public field",
            "--example", "Option A preserves clients",
            "--impact", "Option B breaks clients",
            "--json",
        ])
        self.assertEqual(code, 0, error)
        blocker_id = json.loads(output)["blocker_id"]
        status_code, status_output, status_error = self.run_main([
            "graph-validate", "--workflow-id", workflow_id, "--json"
        ])
        self.assertEqual(status_code, 0, status_error)
        self.assertEqual(json.loads(status_output)["ready_nodes"], ["independent"])
        code, _, error = self.run_main([
            "unblock", "--workflow-id", workflow_id,
            "--blocker-id", blocker_id, "--resolution", "Selected stable contract", "--json",
        ])
        self.assertEqual(code, 0, error)

    def test_finish_requires_valid_terminal_graph_and_new_commit(self) -> None:
        workflow_id = self.init("Finish task")
        self.add_node(workflow_id, "done")
        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "done",
            "--status", "running", "--increment-attempt", "--agent-name", "done_agent", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "done",
            "--status", "done", "--summary", "implemented and validated",
            "--validation-evidence", "unit test output: 12 passed", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, error = self.run_main([
            "requirement-set", "--workflow-id", workflow_id,
            "--requirement-id", "req-1", "--text", "Implement the requested feature",
            "--source", "user", "--status", "satisfied", "--confidence", "high",
            "--evidence", "done node produced and validated the feature", "--json",
        ])
        self.assertEqual(code, 0, error)
        snapshot = {
            "available": True,
            "head": "new-head",
            "branch": "main",
            "dirty_entries": [],
            "captured_at": coordinator_state.iso_now(),
        }
        with mock.patch.object(coordinator_state, "git_snapshot", return_value=snapshot):
            code, output, error = self.run_main([
                "finish", "--workflow-id", workflow_id,
                "--commit", "new-head", "--summary", "complete",
                "--validation", "unit tests passed", "--json",
            ])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "completed")


    def test_start_is_blocked_until_dependencies_are_terminal(self) -> None:
        workflow_id = self.init("Dependency gate")
        self.add_node(workflow_id, "research")
        self.add_node(workflow_id, "implementation", ["research"])
        code, _, _ = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "implementation",
            "--status", "running", "--increment-attempt", "--agent-id", "agent-impl", "--json",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(self.state(workflow_id)["nodes"]["implementation"]["status"], "pending")

    def test_independent_overlapping_scopes_are_rejected_but_serialized_scopes_are_valid(self) -> None:
        workflow_id = self.init("Scope gate")
        self.add_node(workflow_id, "broad")
        # Replace the first node scope with a directory boundary.
        code, _, error = self.run_main([
            "node-patch", "--workflow-id", workflow_id, "--node-id", "broad",
            "--write-scope", "src", "--reason", "owns the source tree", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, _ = self.run_main([
            "node-add", "--workflow-id", workflow_id,
            "--node-id", "conflict", "--stage", "implementation", "--title", "conflict",
            "--agent-type", "implementer", "--model", "gpt-5.6-luna",
            "--reasoning-effort", "medium", "--routing-rationale", "bounded",
            "--write-scope", "src/conflict.py", "--acceptance-criterion", "passes",
            "--output-contract", "produce conflict implementation",
            "--validation-command", "python -m unittest",
            "--json",
        ])
        self.assertEqual(code, 2)
        self.assertNotIn("conflict", self.state(workflow_id)["nodes"])

        serialized = self.add_node(workflow_id, "serialized", ["broad"])
        code, _, error = self.run_main([
            "node-patch", "--workflow-id", workflow_id, "--node-id", "serialized",
            "--write-scope", "src/serialized.py", "--reason", "serialized follow-up", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, output, error = self.run_main([
            "graph-validate", "--workflow-id", workflow_id, "--json"
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["write_scope_collisions"], [])

    def test_stale_route_and_malformed_batch_are_rejected_atomically(self) -> None:
        workflow_id = self.init("Fresh route")
        self.add_node(workflow_id, "work")
        root = coordinator_state.temp_root()
        state = self.state(workflow_id)
        old = coordinator_state.utcnow() - dt.timedelta(hours=1)
        state["nodes"]["work"]["route_history"][-1]["at"] = old.isoformat().replace("+00:00", "Z")
        coordinator_state.write_state(root, state)
        code, _, _ = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--status", "running", "--increment-attempt", "--agent-id", "agent-work", "--json",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(self.state(workflow_id)["nodes"]["work"]["status"], "pending")

        before = self.state(workflow_id)
        plan = {
            "reason": "malformed scope",
            "operations": [
                {"op": "patch", "node_id": "work", "changes": {"write_scope": "src"}}
            ],
        }
        code, _, _ = self.run_main([
            "graph-replan", "--workflow-id", workflow_id,
            "--plan-json", json.dumps(plan), "--json",
        ])
        self.assertEqual(code, 2)
        after = self.state(workflow_id)
        self.assertEqual(before["nodes"], after["nodes"])
        self.assertEqual(before["graph_revision"], after["graph_revision"])

    def test_cleanup_removes_state_older_than_five_days(self) -> None:
        workflow_id = self.init("Old task")
        path = coordinator_state.state_path(coordinator_state.temp_root(), workflow_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        old = coordinator_state.utcnow() - dt.timedelta(days=6)
        raw["updated_at"] = old.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(raw), encoding="utf-8")
        os.utime(path.parent, (old.timestamp(), old.timestamp()))
        removed = coordinator_state.cleanup_stale(coordinator_state.temp_root())
        self.assertEqual(removed, [workflow_id])

    def test_cleanup_removes_nested_stale_artifacts_from_recent_workflow(self) -> None:
        workflow_id = self.init("Recent state with stale nested artifact")
        workflow_root = coordinator_state.state_path(
            coordinator_state.temp_root(), workflow_id
        ).parent
        nested = workflow_root / "downloads" / "old" / "artifact.tmp"
        nested.parent.mkdir(parents=True)
        nested.write_text("obsolete", encoding="utf-8")
        old = coordinator_state.utcnow() - dt.timedelta(days=6)
        os.utime(nested, (old.timestamp(), old.timestamp()))
        os.utime(nested.parent, (old.timestamp(), old.timestamp()))
        removed = coordinator_state.cleanup_stale(coordinator_state.temp_root())
        self.assertFalse(nested.exists())
        self.assertTrue(any("artifact.tmp" in item for item in removed))

    def test_done_requires_validation_evidence_and_finish_requires_resolved_ledger(self) -> None:
        workflow_id = self.init("Evidence gates")
        self.add_node(workflow_id, "work")
        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--status", "running", "--increment-attempt", "--agent-id", "agent-work", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, _ = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--status", "done", "--summary", "implemented", "--json",
        ])
        self.assertEqual(code, 2)
        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--status", "done", "--summary", "implemented",
            "--validation-evidence", "focused test passed", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, _ = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--artifact", "late mutation", "--json",
        ])
        self.assertEqual(code, 2)

        snapshot = {
            "available": True,
            "head": "new-head",
            "branch": "main",
            "dirty_entries": [],
            "captured_at": coordinator_state.iso_now(),
        }
        with mock.patch.object(coordinator_state, "git_snapshot", return_value=snapshot):
            code, _, _ = self.run_main([
                "finish", "--workflow-id", workflow_id, "--commit", "new-head",
                "--summary", "complete", "--validation", "focused test passed", "--json",
            ])
        self.assertEqual(code, 2)

        code, _, error = self.run_main([
            "requirement-set", "--workflow-id", workflow_id,
            "--requirement-id", "req", "--text", "complete work", "--source", "user",
            "--status", "active", "--confidence", "high", "--json",
        ])
        self.assertEqual(code, 0, error)
        with mock.patch.object(coordinator_state, "git_snapshot", return_value=snapshot):
            code, _, _ = self.run_main([
                "finish", "--workflow-id", workflow_id, "--commit", "new-head",
                "--summary", "complete", "--validation", "focused test passed", "--json",
            ])
        self.assertEqual(code, 2)

        code, _, error = self.run_main([
            "requirement-set", "--workflow-id", workflow_id,
            "--requirement-id", "req", "--text", "complete work", "--source", "user",
            "--status", "satisfied", "--confidence", "high",
            "--evidence", "work node validated", "--json",
        ])
        self.assertEqual(code, 0, error)
        with mock.patch.object(coordinator_state, "git_snapshot", return_value=snapshot):
            code, _, error = self.run_main([
                "finish", "--workflow-id", workflow_id, "--commit", "new-head",
                "--summary", "complete", "--validation", "focused test passed", "--json",
            ])
        self.assertEqual(code, 0, error)

    def test_resolved_requirement_requires_evidence(self) -> None:
        workflow_id = self.init("Requirement evidence")
        code, _, _ = self.run_main([
            "requirement-set", "--workflow-id", workflow_id,
            "--requirement-id", "req", "--text", "must hold", "--source", "user",
            "--status", "satisfied", "--confidence", "high", "--json",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(self.state(workflow_id)["requirements"], [])

    def test_patch_can_change_output_and_validation_contracts(self) -> None:
        workflow_id = self.init("Patch contracts")
        self.add_node(workflow_id, "work")
        code, output, error = self.run_main([
            "node-patch", "--workflow-id", workflow_id, "--node-id", "work",
            "--output-contract", "produce a migration report",
            "--validation-command", "python scripts/check.py",
            "--reason", "new repository evidence changed the deliverable", "--json",
        ])
        self.assertEqual(code, 0, error)
        node = json.loads(output)
        self.assertEqual(node["output_contract"], ["produce a migration report"])
        self.assertEqual(node["validation_commands"], ["python scripts/check.py"])

    def test_same_scope_replacement_can_be_added_then_supersede_atomically(self) -> None:
        workflow_id = self.init("Same scope replacement")
        self.add_node(workflow_id, "old")
        self.add_node(workflow_id, "consumer", ["old"])
        # Serialize the replacement behind the old node just long enough to
        # avoid a concurrent scope collision. Supersession removes that edge.
        self.add_node(workflow_id, "replacement", ["old"])
        code, _, error = self.run_main([
            "node-supersede", "--workflow-id", workflow_id,
            "--node-id", "old", "--replacement", "replacement",
            "--reason", "replace the future implementation plan", "--json",
        ])
        self.assertEqual(code, 0, error)
        state = self.state(workflow_id)
        self.assertEqual(state["nodes"]["replacement"]["dependencies"], [])
        self.assertEqual(state["nodes"]["consumer"]["dependencies"], ["replacement"])
        self.assertEqual(state["nodes"]["old"]["status"], "skipped")

    def test_batch_supersession_defers_intermediate_validation_until_final_graph(self) -> None:
        workflow_id = self.init("Atomic supersession reversal")
        self.add_node(workflow_id, "old")
        self.add_node(workflow_id, "consumer", ["old"])
        self.add_node(workflow_id, "replacement", ["consumer"])
        plan = {
            "reason": "Reverse the stale chain atomically",
            "operations": [
                {"op": "supersede", "node_id": "old", "replacement": "replacement"},
                {"op": "dependency_remove", "node_id": "replacement", "dependency": "consumer"},
            ],
        }
        code, _, error = self.run_main([
            "graph-replan", "--workflow-id", workflow_id,
            "--plan-json", json.dumps(plan), "--json",
        ])
        self.assertEqual(code, 0, error)
        state = self.state(workflow_id)
        self.assertEqual(state["nodes"]["old"]["status"], "skipped")
        self.assertEqual(state["nodes"]["consumer"]["dependencies"], ["replacement"])
        self.assertEqual(state["nodes"]["replacement"]["dependencies"], [])

    def test_finish_detects_changes_hidden_behind_same_porcelain_status(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "init"], check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "State Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "state@example.test"], check=True)
        tracked = self.repo / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "base"], check=True, stdout=subprocess.PIPE)
        tracked.write_text("pre-existing dirty\n", encoding="utf-8")

        workflow_id = self.init("Preserve initial dirty state")
        initial = self.state(workflow_id)["git"]["initial"]
        self.assertTrue(initial["available"])
        self.assertTrue(initial["dirty_fingerprint"])
        self.add_node(workflow_id, "work")
        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--status", "running", "--increment-attempt", "--agent-id", "agent", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, error = self.run_main([
            "node-update", "--workflow-id", workflow_id, "--node-id", "work",
            "--status", "done", "--summary", "task committed",
            "--validation-evidence", "focused validation passed", "--json",
        ])
        self.assertEqual(code, 0, error)
        code, _, error = self.run_main([
            "requirement-set", "--workflow-id", workflow_id,
            "--requirement-id", "req", "--text", "preserve dirty state", "--source", "user",
            "--status", "satisfied", "--confidence", "high",
            "--evidence", "task commit created", "--json",
        ])
        self.assertEqual(code, 0, error)

        task_file = self.repo / "task.txt"
        task_file.write_text("task\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "task.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "task"], check=True, stdout=subprocess.PIPE)
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.strip()

        tracked.write_text("different dirty content\n", encoding="utf-8")
        changed = coordinator_state.git_snapshot(str(self.repo))
        self.assertEqual(changed["dirty_entries"], initial["dirty_entries"])
        self.assertNotEqual(changed["dirty_fingerprint"], initial["dirty_fingerprint"])
        code, _, _ = self.run_main([
            "finish", "--workflow-id", workflow_id, "--commit", head,
            "--summary", "complete", "--validation", "focused validation passed", "--json",
        ])
        self.assertEqual(code, 2)

        tracked.write_text("pre-existing dirty\n", encoding="utf-8")
        code, _, error = self.run_main([
            "finish", "--workflow-id", workflow_id, "--commit", head,
            "--summary", "complete", "--validation", "focused validation passed", "--json",
        ])
        self.assertEqual(code, 0, error)


if __name__ == "__main__":
    unittest.main()
