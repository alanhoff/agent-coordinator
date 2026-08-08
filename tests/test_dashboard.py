from __future__ import annotations

import http.client
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))

from coordinator.cli import dashboard as dashboard_cli  # noqa: E402
from coordinator.dashboard.view import Replay, make_server, render, select_snapshot  # noqa: E402
from coordinator.state.store import StateStore, canonical_repository, new_state, ready_nodes, validate_state  # noqa: E402


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.environment = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.environment.start()
        state = new_state(canonical_repository(self.repo), '<script src="https://evil.invalid/x.js">attack</script>', "session-dashboard")
        state["nodes"]["work"] = {
            "id": "work", "title": "<img src=x onerror=alert(1)>", "stage": "implementation", "priority": 50,
            "dependencies": [], "write_scopes": ["src/work.py"], "role": "implementer", "model": "gpt-5.6-terra",
            "effort": "high", "acceptance": ["safe"],
            "route": {"rationale": "bounded", "routed_at": state["created_at"], "attempt": 1},
            "launch": {"state": "claimed", "request_id": "request-1", "child_id": None, "claimed_at": state["created_at"], "reconciliation": None},
            "attempts": [{"number": 1, "started_at": state["created_at"], "finished_at": None, "outcome": None}],
            "status": "ready", "result": None, "evidence": None, "estimated_cost": None, "actual_cost": None, "superseded_by": None,
        }
        validate_state(state)
        self.workflow_id = state["workflow_id"]
        StateStore()._atomic_json(StateStore()._state_path(self.workflow_id), state)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_watch_render_and_selection_share_facts_and_report_escapes_text(self) -> None:
        snapshot = select_snapshot(StateStore(), workflow_id=self.workflow_id)
        facts = snapshot["workflows"][0]
        self.assertEqual(facts["progress_percent"], 0)
        self.assertEqual(facts["launch"]["awaiting_external_call"], 1)
        self.assertEqual(facts["ready_order"], [])
        self.assertIsNone(facts["cost"]["estimated_total"])

        state = StateStore().load(self.workflow_id)
        state["nodes"]["work"]["launch"]["state"] = "reconcile_required"
        state["nodes"]["work"]["launch"]["reconciliation"] = "provider timeout"
        state["controller"]["recovery_status"] = "reconcile_required"
        validate_state(state)
        self.assertEqual(ready_nodes(state), [])
        state["nodes"]["work"]["launch"] = {
            "state": "unclaimed", "request_id": None, "child_id": None,
            "claimed_at": None, "reconciliation": None,
        }
        state["nodes"]["work"]["attempts"] = []
        state["controller"]["recovery_status"] = "clean"
        validate_state(state)
        self.assertEqual(ready_nodes(state), ["work"])

        output = self.base / "report.html"
        before = {path.relative_to(self.home) for path in self.home.rglob("*")}
        render(snapshot, output)
        after = {path.relative_to(self.home) for path in self.home.rglob("*")}
        self.assertEqual(before, after)
        page = output.read_text(encoding="utf-8")
        self.assertIn("&lt;script", page)
        self.assertNotIn('<script src="https://evil.invalid', page)
        self.assertIn("default-src 'none'", page)
        for heading in (
            "Overview", "Dependency DAG and node detail", "Requirements", "Decisions and blockers",
            "Git, checkpoint, controller, resume, and recovery", "Graph and state diagnostics",
            "Capacity, reserve, launch, and cost", "Latest attempt timeline", "Bounded event history",
        ):
            self.assertIn(f">{heading}<", page)

        cli_output = io.StringIO()
        with contextlib.redirect_stdout(cli_output):
            code = dashboard_cli.main(
                ["render", "--workflow-id", self.workflow_id, "--out", str(self.base / "cli-report.html"), "--json"]
            )
        self.assertEqual(code, 0)
        outcome = json.loads(cli_output.getvalue())
        self.assertEqual(set(outcome["data"]), {"output", "workflows"})

    def test_replay_is_bounded_and_reports_eviction(self) -> None:
        replay = Replay(maximum=2)
        for index in range(3):
            replay.observe({"generated_at": str(index), "workflows": [{"revision": index}], "diagnostics": []})
        payload = replay.get()
        self.assertEqual(payload["replay"]["retained"], 2)
        self.assertEqual(payload["replay"]["evicted"], 1)
        self.assertEqual(payload["snapshot"]["workflows"][0]["revision"], 2)

    def test_all_repository_and_workflow_selection_are_ordered_and_corruption_is_diagnostic(self) -> None:
        store = StateStore()
        newer = new_state(canonical_repository(self.repo), "newer task", "session-newer")
        newer["updated_at"] = "2099-01-01T00:00:00Z"
        store._atomic_json(store._state_path(newer["workflow_id"]), newer)
        corrupt = store.workflows / "corrupt.json"
        corrupt.write_text("{", encoding="utf-8")
        os.chmod(corrupt, 0o600)
        before = corrupt.read_bytes()
        all_snapshot = select_snapshot(store)
        repo_snapshot = select_snapshot(store, repo=self.repo)
        one_snapshot = select_snapshot(store, workflow_id=self.workflow_id)
        self.assertEqual(all_snapshot["workflows"][0]["workflow_id"], newer["workflow_id"])
        self.assertEqual([item["workflow_id"] for item in repo_snapshot["workflows"]], [newer["workflow_id"], self.workflow_id])
        self.assertEqual([item["workflow_id"] for item in one_snapshot["workflows"]], [self.workflow_id])
        self.assertEqual(all_snapshot["diagnostics"], [{"code": "corrupt_state", "message": "one stored workflow could not be validated"}])
        self.assertEqual(corrupt.read_bytes(), before)

        saved = store.root / "workflows-saved"
        os.replace(store.workflows, saved)
        missing = self.base / "missing-workflows"
        store.workflows.symlink_to(missing, target_is_directory=True)
        link_before = (store.workflows.readlink(), store.workflows.lstat().st_mtime_ns)
        unsafe_snapshot = select_snapshot(store)
        self.assertEqual(unsafe_snapshot["workflows"], [])
        self.assertEqual(
            unsafe_snapshot["diagnostics"],
            [{"code": "unsafe_path", "message": "one stored workflow could not be validated"}],
        )
        self.assertEqual((store.workflows.readlink(), store.workflows.lstat().st_mtime_ns), link_before)
        self.assertFalse(missing.exists())

    def test_workflow_selector_reads_only_its_exact_state_file(self) -> None:
        store = StateStore()
        selected = store._state_path(self.workflow_id)
        valid = selected.read_bytes()
        selected.write_text("{", encoding="utf-8")
        alias = store.workflows / "alias.json"
        alias.write_bytes(valid)
        os.chmod(alias, 0o600)
        snapshot = select_snapshot(store, workflow_id=self.workflow_id)
        self.assertEqual(snapshot["workflows"], [])
        self.assertEqual(snapshot["diagnostics"][0]["code"], "corrupt_state")

    def test_loopback_server_requires_authority_capability_and_read_only_method(self) -> None:
        server = make_server(self.workflow_id, None, 0, 0.25)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            expected_host = f"127.0.0.1:{server.server_port}"
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", f"/?capability={server.capability}", headers={"Host": expected_host})
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertNotIn(server.capability.encode(), body)
            self.assertIn(b"Git, checkpoint, controller, resume, and recovery", body)
            self.assertIn(b"},250);", body)
            connection.request("GET", "/api/snapshot", headers={"Host": expected_host, "X-Coordinator-Capability": server.capability})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
            self.assertEqual(payload["snapshot"]["workflows"][0]["progress_percent"], 0)
            state = StateStore().load(self.workflow_id)
            node = state["nodes"]["work"]
            node["status"] = "done"
            node["result"] = "complete"
            node["evidence"] = "focused validation passed"
            node["launch"].update({"state": "terminal", "child_id": "child-1"})
            node["attempts"][-1].update({"finished_at": "2099-01-01T00:00:00Z", "outcome": "done"})
            state.update({"status": "completed", "phase": "completed", "revision": 1, "updated_at": "2099-01-01T00:00:00Z"})
            state["controller"]["checkpoint"] = 1
            state["git"]["checkpoint"] = "a" * 40
            validate_state(state)
            StateStore()._atomic_json(StateStore()._state_path(self.workflow_id), state)
            connection.request("GET", "/api/snapshot", headers={"Host": expected_host, "X-Coordinator-Capability": server.capability})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            refreshed = json.loads(response.read())
            self.assertEqual(refreshed["snapshot"]["workflows"][0]["progress_percent"], 100)
            self.assertEqual(refreshed["replay"]["retained"], 2)
            connection.request("GET", "/api/snapshot", headers={"Host": expected_host})
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.request("GET", "/api/snapshot", headers={"Host": expected_host, "Origin": "http://evil.invalid", "X-Coordinator-Capability": server.capability})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request("POST", "/api/snapshot", headers={"Host": expected_host, "X-Coordinator-Capability": server.capability})
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
            connection.request("GET", "/api/snapshot", headers={"Host": "evil.invalid", "X-Coordinator-Capability": server.capability})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_watch_summary_is_actionable_and_interval_must_be_finite(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = dashboard_cli.main(["watch", "--workflow-id", self.workflow_id, "--once"])
        self.assertEqual(code, 0)
        self.assertIn("launch=", output.getvalue())
        self.assertIn("recovery=", output.getvalue())
        invalid = io.StringIO()
        with contextlib.redirect_stdout(invalid):
            code = dashboard_cli.main(["serve", "--interval", "nan", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(invalid.getvalue())["code"], "dashboard_error")


if __name__ == "__main__":
    unittest.main()
