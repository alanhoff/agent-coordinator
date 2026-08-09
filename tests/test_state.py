from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))

from coordinator.cli import state as state_cli  # noqa: E402
from coordinator.state import store as state_owner  # noqa: E402
from coordinator.state.store import MAX_STATE_BYTES, StateError, StateStore, ready_nodes, validate_state  # noqa: E402


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
            "--model", "gpt-5.6-terra", "--effort", "high",
            "--acceptance", "focused test passes", "--rationale", "bounded implementation",
            *extra,
            expected=expected,
        )

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
        rejected = self.mutation(
            "node-update", 3, "claim-dependent", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-work", expected=2,
        )
        self.assertEqual(rejected["code"], "invalid_state")
        self.assertEqual(self.cli("graph-validate", "--workflow-id", self.workflow_id)["data"]["ready_nodes"], ["dependency"])
        self.mutation("block", 3, "block-workflow", "--reason", "pause", "--needed", "approval")
        self.assertEqual(self.cli("graph-validate", "--workflow-id", self.workflow_id)["data"]["ready_nodes"], [])
        self.mutation(
            "node-update", 4, "claim-blocked", "--node-id", "dependency",
            "--launch-state", "claimed", "--request-id", "request-dependency", expected=2,
        )
        state = StateStore().load(self.workflow_id)
        self.assertEqual((state["revision"], state["nodes"]["dependency"]["launch"]["state"]), (4, "unclaimed"))
        state["status"] = "planning"
        self.assertEqual(ready_nodes(state), [])
        state["blockers"][0].update({"status": "resolved", "resolution": "approved"})
        state["status"] = "blocked"
        self.assertEqual(ready_nodes(state), [])

    def test_takeover_fences_old_controller_and_launch_reconciliation_is_explicit(self) -> None:
        self.add("work", 1, "add-work")
        second_session = self.base / "private" / "second.json"
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(second_session))
        takeover = self.mutation("controller-takeover", 2, "takeover-1", session=second_session)
        self.assertTrue(takeover["data"]["resume_required"])
        self.mutation("event", 3, "old-controller", "--kind", "test", "--message", "old", expected=20)
        self.mutation("event", 3, "before-resume", "--kind", "test", "--message", "new", session=second_session, expected=20)
        resumed = self.mutation("resume", 3, "resume-1", "--message", "reconciled takeover", session=second_session)
        self.assertEqual(resumed["data"]["controller_epoch"], 2)
        self.mutation("node-update", 4, "claim-1", "--node-id", "work", "--launch-state", "claimed", "--request-id", "request-1", session=second_session)
        self.mutation("node-update", 5, "uncertain-1", "--node-id", "work", "--launch-state", "reconcile_required", "--reconciliation", "provider timed out", session=second_session)
        self.mutation("node-update", 6, "unsafe-retry", "--node-id", "work", "--launch-state", "unclaimed", session=second_session, expected=2)
        self.mutation("node-update", 6, "safe-retry", "--node-id", "work", "--launch-state", "unclaimed", "--reconciliation", "provider confirms no child", session=second_session)
        state = StateStore().load(self.workflow_id)
        self.assertEqual(state["nodes"]["work"]["launch"]["state"], "unclaimed")
        self.assertEqual(state["nodes"]["work"]["attempts"][0]["outcome"], "provider confirmed not launched")

    def test_failed_attempt_can_be_routed_and_relaunched_without_losing_history(self) -> None:
        self.add("work", 1, "add-work")
        self.mutation("node-update", 2, "ready-work", "--node-id", "work", "--status", "ready")
        self.mutation(
            "node-update", 3, "claim-work", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-1",
        )
        self.mutation(
            "node-update", 4, "bind-work", "--node-id", "work",
            "--launch-state", "bound", "--child-id", "child-1",
        )
        self.mutation("node-update", 5, "run-work", "--node-id", "work", "--status", "running")
        self.mutation(
            "node-update", 6, "fail-work", "--node-id", "work",
            "--status", "failed", "--attempt-outcome", "tests failed",
        )
        route = (
            "--node-id", "work", "--role", "fixer", "--model", "gpt-5.6-terra",
            "--effort", "high", "--rationale", "fix the failed attempt",
        )
        self.mutation("node-route", 7, "reroute-work", *route)
        replay = self.mutation("node-route", 7, "reroute-work", *route)
        self.assertEqual(replay["code"], "mutation_reconciled")
        node = StateStore().load(self.workflow_id)["nodes"]["work"]
        self.assertEqual((node["status"], node["launch"]["state"], node["route"]["attempt"]), ("pending", "unclaimed", 2))
        self.assertEqual((node["attempts"][0]["number"], node["attempts"][0]["outcome"]), (1, "tests failed"))
        self.assertIsNotNone(node["attempts"][0]["finished_at"])

        self.mutation("node-update", 8, "ready-retry", "--node-id", "work", "--status", "ready")
        self.mutation(
            "node-update", 9, "claim-retry", "--node-id", "work",
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
        self.mutation(
            "node-update", 5, "claim-absent", "--node-id", "absent",
            "--launch-state", "claimed", "--request-id", "request-absent",
        )
        self.mutation(
            "node-update", 6, "uncertain-absent", "--node-id", "absent",
            "--launch-state", "reconcile_required", "--reconciliation", "provider timeout",
        )
        self.mutation(
            "node-update", 7, "claim-found", "--node-id", "found",
            "--launch-state", "claimed", "--request-id", "request-found",
        )
        self.mutation(
            "node-update", 8, "uncertain-found", "--node-id", "found",
            "--launch-state", "reconcile_required", "--reconciliation", "provider timeout",
        )
        self.mutation(
            "node-update", 9, "claim-running", "--node-id", "running",
            "--launch-state", "claimed", "--request-id", "request-running",
        )
        self.mutation(
            "node-update", 10, "bind-running", "--node-id", "running",
            "--launch-state", "bound", "--child-id", "child-running",
        )
        self.mutation("node-update", 11, "run-running", "--node-id", "running", "--status", "running")
        self.mutation("abort", 12, "abort-workflow", "--reason", "operator stopped")
        self.mutation(
            "node-update", 13, "reject-different-known-child", "--node-id", "running",
            "--launch-state", "bound", "--child-id", "child-different",
            "--reconciliation", "provider reports a different child", expected=2,
        )
        unchanged = StateStore().load(self.workflow_id)
        self.assertEqual(unchanged["revision"], 13)
        self.assertEqual(
            (unchanged["nodes"]["running"]["launch"]["state"], unchanged["nodes"]["running"]["launch"]["child_id"]),
            ("reconcile_required", "child-running"),
        )
        self.mutation(
            "node-update", 13, "reject-known-child-absence", "--node-id", "running",
            "--launch-state", "unclaimed", "--reconciliation", "provider confirms no child", expected=2,
        )
        absent = (
            "--node-id", "absent", "--launch-state", "unclaimed",
            "--reconciliation", "provider confirms no child",
        )
        self.mutation("node-update", 13, "resolve-absent", *absent)
        self.mutation(
            "node-update", 14, "resolve-found", "--node-id", "found",
            "--launch-state", "bound", "--child-id", "child-found",
            "--reconciliation", "provider confirms existing child",
        )
        self.mutation(
            "node-update", 15, "resolve-known-child", "--node-id", "running",
            "--launch-state", "bound", "--child-id", "child-running",
            "--reconciliation", "provider confirms the known child",
        )
        replay = self.mutation("node-update", 13, "resolve-absent", *absent)
        self.assertEqual(replay["code"], "mutation_reconciled")
        self.mutation("event", 16, "terminal-event", "--kind", "test", "--message", "blocked", expected=2)

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
        self.mutation(
            "node-update", 2, "claim-work", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-1",
        )
        self.mutation(
            "node-update", 3, "uncertain-work", "--node-id", "work",
            "--launch-state", "reconcile_required", "--reconciliation", "provider timeout",
        )
        self.mutation("abort", 4, "abort-workflow", "--reason", "controller stopped")

        second_session = self.base / "private" / "second.json"
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(second_session))
        takeover = self.mutation("controller-takeover", 5, "takeover-aborted", session=second_session)
        replayed_takeover = self.mutation("controller-takeover", 5, "takeover-aborted", session=second_session)
        self.assertEqual((takeover["data"]["revision"], replayed_takeover["data"]["revision"]), (6, 6))
        self.mutation("resume", 6, "resume-recovery", "--message", "resume provider recovery", session=second_session)
        replayed_resume = self.mutation(
            "resume", 6, "resume-recovery", "--message", "resume provider recovery", session=second_session
        )
        self.assertEqual(replayed_resume["code"], "mutation_reconciled")
        self.mutation(
            "node-update", 7, "bind-work", "--node-id", "work", "--launch-state", "bound",
            "--child-id", "child-1", "--reconciliation", "provider found existing child", session=second_session,
        )
        self.assertEqual(StateStore().load(self.workflow_id)["controller"]["recovery_status"], "reconcile_required")
        self.mutation(
            "node-update", 8, "terminal-without-evidence", "--node-id", "work",
            "--launch-state", "terminal", "--attempt-outcome", "stopped after abort",
            session=second_session, expected=2,
        )
        complete = (
            "--node-id", "work", "--launch-state", "terminal",
            "--reconciliation", "provider confirms child stopped", "--attempt-outcome", "stopped after abort",
        )
        self.mutation("node-update", 8, "complete-child", *complete, session=second_session)
        replayed = self.mutation("node-update", 8, "complete-child", *complete, session=second_session)
        self.assertEqual(replayed["code"], "mutation_reconciled")

        state = StateStore().load(self.workflow_id)
        attempt = state["nodes"]["work"]["attempts"][0]
        self.assertEqual((state["status"], state["controller"]["recovery_status"]), ("aborted", "clean"))
        self.assertEqual((state["nodes"]["work"]["launch"]["state"], attempt["outcome"]), ("terminal", "stopped after abort"))
        self.assertIsNotNone(attempt["finished_at"])
        third_session = self.base / "private" / "third.json"
        self.cli("session-open", "--repo", str(self.repo), "--session-file", str(third_session))
        self.mutation("controller-takeover", 9, "takeover-clean-abort", session=third_session, expected=2)

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
        self.mutation(
            "node-update", 3, "claim-work", "--node-id", "work",
            "--launch-state", "claimed", "--request-id", "request-1",
        )
        self.mutation(
            "node-update", 4, "bind-work", "--node-id", "work",
            "--launch-state", "bound", "--child-id", "child-1",
        )
        self.mutation("node-update", 5, "run-work", "--node-id", "work", "--status", "running")

        active = StateStore().load(self.workflow_id)
        active["nodes"]["work"]["attempts"][0]["outcome"] = "premature outcome"
        with self.assertRaisesRegex(StateError, "completion fields"):
            validate_state(active)

        self.mutation(
            "node-update", 6, "fail-work", "--node-id", "work",
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
        self.mutation("node-update", 4, "claim-a", "--node-id", "a", "--launch-state", "claimed", "--request-id", "request-shared")
        self.mutation(
            "node-update", 5, "claim-b-duplicate", "--node-id", "b", "--launch-state", "claimed", "--request-id", "request-shared", expected=2
        )
        self.mutation("node-update", 5, "claim-b", "--node-id", "b", "--launch-state", "claimed", "--request-id", "request-b")
        self.mutation("node-update", 6, "uncertain-a", "--node-id", "a", "--launch-state", "reconcile_required")
        self.mutation("node-update", 7, "uncertain-b", "--node-id", "b", "--launch-state", "reconcile_required")
        self.mutation(
            "node-update", 8, "clear-a", "--node-id", "a", "--launch-state", "unclaimed", "--reconciliation", "provider confirms no child"
        )
        self.assertEqual(StateStore().load(self.workflow_id)["controller"]["recovery_status"], "reconcile_required")
        self.mutation(
            "node-update", 9, "bind-b", "--node-id", "b", "--launch-state", "bound", "--child-id", "child-shared", "--reconciliation", "provider confirms child"
        )
        self.assertEqual(StateStore().load(self.workflow_id)["controller"]["recovery_status"], "clean")
        self.mutation("node-update", 10, "claim-c", "--node-id", "c", "--launch-state", "claimed", "--request-id", "request-c")
        self.mutation(
            "node-update", 11, "bind-c-duplicate", "--node-id", "c", "--launch-state", "bound", "--child-id", "child-shared", expected=2
        )
        self.assertEqual(StateStore().load(self.workflow_id)["revision"], 11)

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
