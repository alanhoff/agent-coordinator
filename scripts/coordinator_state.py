#!/usr/bin/env python3
"""Resumable temporary workflow state for the coordinator Codex skill.

All persistent artifacts created by this helper live below the operating
system's temporary directory. Every invocation removes workflow directories
whose last update is older than five days.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
RETENTION_DAYS = 5
TERMINAL_NODE_STATUSES = {"done", "skipped"}
KNOWN_NODE_STATUSES = {
    "pending",
    "ready",
    "running",
    "blocked",
    "failed",
    "done",
    "skipped",
    "interrupted",
}
KNOWN_WORKFLOW_STATUSES = {"planning", "running", "blocked", "completed", "aborted"}
KNOWN_REQUIREMENT_STATUSES = {"active", "satisfied", "superseded"}
KNOWN_MODELS = {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}
KNOWN_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
KNOWN_AGENT_TYPES = {
    "researcher", "designer", "architect", "documenter",
    "implementer", "reviewer", "fixer", "validator",
}
ROUTE_FRESHNESS_MINUTES = 30
STARTABLE_NODE_STATUSES = {"pending", "ready", "failed", "interrupted"}
ALLOWED_NODE_TRANSITIONS = {
    "pending": {"ready", "running", "skipped"},
    "ready": {"running", "skipped"},
    "running": {"done", "failed", "interrupted"},
    "blocked": set(),
    "failed": {"running", "skipped"},
    "interrupted": {"running", "skipped"},
    "done": set(),
    "skipped": set(),
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _is_under(child: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def temp_root() -> pathlib.Path:
    system_temp = pathlib.Path(tempfile.gettempdir()).resolve()
    configured = os.environ.get("COORDINATOR_TMP_ROOT")
    root = pathlib.Path(configured).expanduser().resolve() if configured else system_temp / "codex-coordinator"
    if not _is_under(root, system_temp):
        raise RuntimeError(
            "COORDINATOR_TMP_ROOT must remain below the operating system temporary directory "
            f"({system_temp}); got {root}"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextlib.contextmanager
def root_lock(root: pathlib.Path, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Cross-platform state lock based on atomic directory creation."""
    lock_path = root / ".state.lockdir"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock_path.mkdir()
            (lock_path / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "created_at": iso_now()}),
                encoding="utf-8",
            )
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > 10 * 60:
                shutil.rmtree(str(lock_path), ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for the coordinator state lock")
            time.sleep(0.1)
    try:
        yield
    finally:
        shutil.rmtree(str(lock_path), ignore_errors=True)


def state_path(root: pathlib.Path, workflow_id: str) -> pathlib.Path:
    return root / workflow_id / "state.json"


def _touch_directory(path: pathlib.Path) -> None:
    try:
        os.utime(str(path), None)
    except FileNotFoundError:
        pass


def write_state(root: pathlib.Path, state: MutableMapping[str, Any]) -> None:
    workflow_id = str(state["workflow_id"])
    directory = root / workflow_id
    directory.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = iso_now()
    destination = directory / "state.json"
    fd, temporary_name = tempfile.mkstemp(prefix="state-", suffix=".json.tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        _touch_directory(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def load_state(root: pathlib.Path, workflow_id: str) -> Dict[str, Any]:
    path = state_path(root, workflow_id)
    if not path.is_file():
        raise FileNotFoundError(f"workflow not found: {workflow_id}")
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported state schema {state.get('schema_version')}; expected {SCHEMA_VERSION}"
        )
    return state


def iter_states(root: pathlib.Path) -> Iterator[Dict[str, Any]]:
    for path in root.glob("*/state.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
            if state.get("schema_version") == SCHEMA_VERSION:
                yield state
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def cleanup_stale(root: pathlib.Path, now: Optional[dt.datetime] = None) -> List[str]:
    reference = now or utcnow()
    cutoff = reference - dt.timedelta(days=RETENTION_DAYS)
    cutoff_timestamp = cutoff.timestamp()
    removed: List[str] = []

    for child in list(root.iterdir()):
        if child.name == ".state.lockdir":
            # A live lock is handled by root_lock; only five-day-stale lock
            # artifacts are eligible for generic cleanup below.
            pass
        try:
            if child.is_symlink() or child.is_file():
                if child.lstat().st_mtime < cutoff_timestamp:
                    child.unlink()
                    removed.append(child.name)
                continue
        except FileNotFoundError:
            continue
        if not child.is_dir():
            continue

        updated: Optional[dt.datetime] = None
        path = child / "state.json"
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("updated_at"):
                    updated = parse_time(str(data["updated_at"]))
            except (OSError, ValueError, json.JSONDecodeError):
                updated = None
        if updated is None:
            try:
                updated = dt.datetime.fromtimestamp(child.stat().st_mtime, tz=dt.timezone.utc)
            except FileNotFoundError:
                continue
        if updated < cutoff:
            shutil.rmtree(str(child), ignore_errors=True)
            removed.append(child.name)
            continue

        # The retained container may still hold individually stale activation,
        # download, backup, or interrupted-write artifacts. Never delete the
        # current workflow state merely because its file metadata is unusual;
        # its content timestamp above is authoritative.
        descendants = sorted(child.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for descendant in descendants:
            if descendant == path:
                continue
            try:
                modified = descendant.lstat().st_mtime
            except FileNotFoundError:
                continue
            try:
                if descendant.is_symlink() or descendant.is_file():
                    if modified < cutoff_timestamp:
                        descendant.unlink()
                        removed.append(str(descendant.relative_to(root)))
                elif descendant.is_dir() and modified < cutoff_timestamp:
                    try:
                        descendant.rmdir()
                    except OSError:
                        pass
                    else:
                        removed.append(str(descendant.relative_to(root)))
            except FileNotFoundError:
                pass
    return sorted(set(removed))


def task_digest(repo: str, task: str) -> str:
    normalized = str(pathlib.Path(repo).expanduser().resolve()) + "\0" + task.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _run_git(repo: str, arguments: Sequence[str]) -> Optional[str]:
    if not shutil.which("git") or not pathlib.Path(repo).exists():
        return None
    completed = subprocess.run(
        ["git", "-C", repo] + list(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_snapshot(repo: str) -> Dict[str, Any]:
    head = _run_git(repo, ["rev-parse", "HEAD"])
    branch = _run_git(repo, ["branch", "--show-current"])
    porcelain = _run_git(repo, ["status", "--porcelain=v1"])
    dirty_fingerprint: Optional[str] = None
    if head is not None:
        diff = _run_git(repo, ["diff", "--binary", "--no-ext-diff", "HEAD", "--"])
        untracked = _run_git(repo, ["ls-files", "--others", "--exclude-standard", "-z"])
        if diff is not None and untracked is not None:
            digest = hashlib.sha256()
            digest.update(diff.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0UNTRACKED\0")
            for relative in sorted(item for item in untracked.split("\0") if item):
                digest.update(relative.encode("utf-8", errors="surrogateescape"))
                digest.update(b"\0")
                path = pathlib.Path(repo) / pathlib.PurePosixPath(relative)
                try:
                    if path.is_symlink():
                        digest.update(b"symlink\0")
                        digest.update(os.readlink(str(path)).encode("utf-8", errors="surrogateescape"))
                    elif path.is_file():
                        digest.update(b"file\0")
                        with path.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(chunk)
                    else:
                        digest.update(b"other\0")
                except OSError as exc:
                    digest.update(f"unreadable:{type(exc).__name__}".encode("ascii"))
                digest.update(b"\0")
            dirty_fingerprint = digest.hexdigest()
    return {
        "available": head is not None,
        "head": head,
        "branch": branch,
        "dirty_entries": [] if porcelain is None or porcelain == "" else porcelain.splitlines(),
        "dirty_fingerprint": dirty_fingerprint,
        "captured_at": iso_now(),
    }


def add_event(
    state: MutableMapping[str, Any],
    kind: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    events = state.setdefault("events", [])
    events.append(
        {
            "at": iso_now(),
            "kind": kind,
            "message": message,
            "details": dict(details or {}),
        }
    )
    # State is operational context, not an audit archive. Bound growth while
    # retaining enough history for resume and diagnosis.
    if len(events) > 500:
        del events[:-500]


def make_workflow_id(repo: str, task: str) -> str:
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    digest = task_digest(repo, task)[:10]
    return f"{stamp}-{digest}-{secrets.token_hex(2)}"


def find_matching_state(root: pathlib.Path, repo: str, task: str) -> Optional[Dict[str, Any]]:
    digest = task_digest(repo, task)
    candidates = [
        state
        for state in iter_states(root)
        if state.get("task_digest") == digest and state.get("status") != "completed"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda state: state.get("updated_at", ""), reverse=True)
    return candidates[0]


def latest_state(root: pathlib.Path, include_completed: bool = True) -> Optional[Dict[str, Any]]:
    states = list(iter_states(root))
    if not include_completed:
        states = [state for state in states if state.get("status") != "completed"]
    if not states:
        return None
    states.sort(key=lambda state: state.get("updated_at", ""), reverse=True)
    return states[0]


def new_state(repo: str, task: str) -> Dict[str, Any]:
    absolute_repo = str(pathlib.Path(repo).expanduser().resolve())
    now = iso_now()
    workflow_id = make_workflow_id(absolute_repo, task)
    state: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "task": task.strip(),
        "task_digest": task_digest(absolute_repo, task),
        "repo": absolute_repo,
        "status": "planning",
        "phase": "intake",
        "created_at": now,
        "updated_at": now,
        "retention_days": RETENTION_DAYS,
        "nodes": {},
        "graph_revision": 0,
        "blockers": [],
        "requirements": [],
        "decisions": [],
        "events": [],
        "git": {
            "initial": git_snapshot(absolute_repo),
            "latest": None,
            "final_commit": None,
        },
        "completion": None,
    }
    add_event(state, "workflow_created", "Initialized coordinator workflow state")
    return state


def _normalized_scope(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/") or "."
    # Windows project paths are case-insensitive by default. Treat scopes that
    # differ only by case as colliding on Windows.
    return normalized.casefold() if os.name == "nt" else normalized


def _scope_static_prefix(scope: str) -> str:
    positions = [scope.find(char) for char in "*?[" if char in scope]
    if not positions:
        return scope
    prefix = scope[: min(positions)].rstrip("/")
    return prefix or "."


def _scopes_overlap(left: str, right: str) -> bool:
    a = _normalized_scope(left)
    b = _normalized_scope(right)
    if a.lower() == "none" or b.lower() == "none":
        return False
    if a in {".", "*", "**"} or b in {".", "*", "**"}:
        return True
    a_prefix = _scope_static_prefix(a)
    b_prefix = _scope_static_prefix(b)
    a_glob = any(char in a for char in "*?[")
    b_glob = any(char in b for char in "*?[")
    return (
        a_prefix == b_prefix
        or a_prefix.startswith(b_prefix + "/")
        or b_prefix.startswith(a_prefix + "/")
        or (a_glob and b_prefix.startswith(a_prefix))
        or (b_glob and a_prefix.startswith(b_prefix))
    )


def _depends_transitively(
    nodes: Mapping[str, Mapping[str, Any]], node_id: str, dependency_id: str
) -> bool:
    raw_node = nodes.get(node_id, {})
    raw_dependencies = raw_node.get("dependencies", []) if isinstance(raw_node, Mapping) else []
    stack = list(raw_dependencies) if isinstance(raw_dependencies, list) else []
    visited: set = set()
    while stack:
        current = str(stack.pop())
        if current == dependency_id:
            return True
        if current in visited or current not in nodes:
            continue
        visited.add(current)
        current_node = nodes[current]
        dependencies = current_node.get("dependencies", []) if isinstance(current_node, Mapping) else []
        if isinstance(dependencies, list):
            stack.extend(dependencies)
    return False


def graph_diagnostics(nodes: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate node contracts, topology, and potentially concurrent write scopes."""
    missing: List[Dict[str, str]] = []
    self_edges: List[str] = []
    duplicate_edges: List[Dict[str, str]] = []
    invalid_nodes: List[Dict[str, Any]] = []

    for node_id, raw_node in nodes.items():
        if not isinstance(raw_node, Mapping):
            invalid_nodes.append({"node_id": node_id, "issues": ["node must be an object"]})
            continue
        node = raw_node
        issues: List[str] = []
        if node.get("node_id") != node_id:
            issues.append("node_id field must match the map key")
        if node.get("status") not in KNOWN_NODE_STATUSES:
            issues.append("unknown status")
        priority = node.get("priority", 50)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            issues.append("priority must be an integer from 0 through 100")
        if node.get("model") not in KNOWN_MODELS:
            issues.append("unknown model")
        if node.get("reasoning_effort") not in KNOWN_REASONING_EFFORTS:
            issues.append("unknown reasoning effort")
        if node.get("agent_type") not in KNOWN_AGENT_TYPES:
            issues.append("unknown agent type")
        for field in (
            "dependencies",
            "write_scope",
            "acceptance_criteria",
            "output_contract",
            "validation_commands",
            "artifacts",
            "validation_evidence",
        ):
            value = node.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                issues.append(f"{field} must be a list of nonblank strings")
        if not node.get("write_scope"):
            issues.append("write_scope must contain an exact scope or 'none'")
        if not node.get("acceptance_criteria"):
            issues.append("acceptance_criteria must not be empty")
        if not node.get("output_contract"):
            issues.append("output_contract must not be empty")
        if not node.get("validation_commands"):
            issues.append("validation_commands must not be empty")
        if node.get("status") == "done":
            if not isinstance(node.get("summary"), str) or not node.get("summary", "").strip():
                issues.append("done nodes require a nonblank summary")
            if not node.get("validation_evidence"):
                issues.append("done nodes require concrete validation_evidence")
        if node.get("status") == "skipped":
            if not isinstance(node.get("summary"), str) or not node.get("summary", "").strip():
                issues.append("skipped nodes require a nonblank summary")
        dependencies = node.get("dependencies", []) if isinstance(node.get("dependencies"), list) else []
        seen_dependencies: set = set()
        for dependency in dependencies:
            if not isinstance(dependency, str) or not dependency.strip():
                continue
            if dependency in seen_dependencies:
                duplicate_edges.append({"node_id": node_id, "dependency": dependency})
            seen_dependencies.add(dependency)
            if dependency == node_id:
                self_edges.append(node_id)
            elif dependency not in nodes:
                missing.append({"node_id": node_id, "dependency": dependency})
        if issues:
            invalid_nodes.append({"node_id": node_id, "issues": issues})

    visiting: set = set()
    visited: set = set()
    stack: List[str] = []
    cycles: List[List[str]] = []

    def visit(node_id: str) -> None:
        if node_id in visiting:
            try:
                index = stack.index(node_id)
            except ValueError:
                index = 0
            cycle = stack[index:] + [node_id]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        if node_id in visited:
            return
        visiting.add(node_id)
        stack.append(node_id)
        raw_node = nodes.get(node_id, {})
        raw_dependencies = raw_node.get("dependencies", []) if isinstance(raw_node, Mapping) else []
        dependencies = raw_dependencies if isinstance(raw_dependencies, list) else []
        for dependency in dependencies:
            if isinstance(dependency, str) and dependency in nodes:
                visit(dependency)
        stack.pop()
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(nodes):
        visit(node_id)

    collisions: List[Dict[str, Any]] = []
    active_ids = [
        node_id for node_id, node in nodes.items()
        if isinstance(node, Mapping) and node.get("status") not in TERMINAL_NODE_STATUSES
    ]
    for index, left_id in enumerate(sorted(active_ids)):
        for right_id in sorted(active_ids)[index + 1 :]:
            if _depends_transitively(nodes, left_id, right_id) or _depends_transitively(nodes, right_id, left_id):
                continue
            left_scopes = nodes[left_id].get("write_scope", [])
            right_scopes = nodes[right_id].get("write_scope", [])
            if not isinstance(left_scopes, list) or not isinstance(right_scopes, list):
                continue
            overlaps = [
                {"left": left, "right": right}
                for left in left_scopes
                for right in right_scopes
                if isinstance(left, str) and isinstance(right, str) and _scopes_overlap(left, right)
            ]
            if overlaps:
                collisions.append({"nodes": [left_id, right_id], "scopes": overlaps})

    ok = not (missing or self_edges or duplicate_edges or cycles or invalid_nodes or collisions)
    return {
        "ok": ok,
        "missing_dependencies": missing,
        "self_dependencies": sorted(set(self_edges)),
        "duplicate_dependencies": duplicate_edges,
        "cycles": cycles,
        "invalid_nodes": invalid_nodes,
        "write_scope_collisions": collisions,
    }

def require_valid_graph(nodes: Mapping[str, Mapping[str, Any]]) -> None:
    diagnostics = graph_diagnostics(nodes)
    if not diagnostics["ok"]:
        raise ValueError("invalid workflow DAG: " + json.dumps(diagnostics, sort_keys=True))


def require_replannable_node(node_id: str, node: Mapping[str, Any]) -> None:
    status = node.get("status")
    if status == "running":
        raise ValueError(
            f"interrupt the live child for {node_id} before mutating its plan"
        )
    if status in TERMINAL_NODE_STATUSES:
        raise ValueError(
            f"completed evidence is immutable; add a corrective node downstream of {node_id} instead"
        )


def ready_node_ids(state: Mapping[str, Any]) -> List[str]:
    if state.get("status") not in {"planning", "running"}:
        return []
    nodes: Mapping[str, Mapping[str, Any]] = state.get("nodes", {})
    ready: List[str] = []
    for node_id, node in nodes.items():
        if node.get("status") not in {"pending", "ready", "interrupted", "failed"}:
            continue
        if active_blockers_for_start(state, node_id):
            continue
        dependencies = node.get("dependencies", [])
        if all(nodes.get(dep, {}).get("status") in TERMINAL_NODE_STATUSES for dep in dependencies):
            ready.append(node_id)
    return sorted(
        ready,
        key=lambda node_id: (-int(nodes[node_id].get("priority", 50)), node_id),
    )


def progress_summary(state: Mapping[str, Any]) -> Dict[str, Any]:
    nodes: Mapping[str, Mapping[str, Any]] = state.get("nodes", {})
    counts = {status: 0 for status in KNOWN_NODE_STATUSES}
    for node in nodes.values():
        status = str(node.get("status", "pending"))
        counts[status] = counts.get(status, 0) + 1
    total = len(nodes)
    complete = sum(counts.get(status, 0) for status in TERMINAL_NODE_STATUSES)
    percentage = 100.0 if state.get("status") == "completed" else (100.0 * complete / total if total else 0.0)
    active_blockers = [item for item in state.get("blockers", []) if item.get("status") == "active"]
    estimated_cost = 0.0
    actual_cost = 0.0
    for node in nodes.values():
        routes = node.get("route_history") or []
        if routes:
            for route in routes:
                value = route.get("estimated_cost_usd")
                if isinstance(value, (int, float)):
                    estimated_cost += float(value)
        else:
            value = node.get("estimated_cost_usd")
            if isinstance(value, (int, float)):
                estimated_cost += float(value)
        value = node.get("actual_cost_usd")
        if isinstance(value, (int, float)):
            actual_cost += float(value)
    return {
        "workflow_id": state.get("workflow_id"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "progress_percent": round(percentage, 1),
        "completed_nodes": complete,
        "total_nodes": total,
        "node_counts": {key: value for key, value in counts.items() if value},
        "ready_nodes": ready_node_ids(state),
        "graph_revision": int(state.get("graph_revision", 0)),
        "requirements": {
            status: sum(1 for item in state.get("requirements", []) if item.get("status") == status)
            for status in sorted(KNOWN_REQUIREMENT_STATUSES)
        },
        "active_blockers": active_blockers,
        "estimated_child_cost_usd": round(estimated_cost, 6),
        "recorded_actual_child_cost_usd": round(actual_cost, 6),
        "updated_at": state.get("updated_at"),
        "git": state.get("git"),
    }


def resolve_state(root: pathlib.Path, workflow_id: Optional[str], active_first: bool = True) -> Dict[str, Any]:
    if workflow_id:
        return load_state(root, workflow_id)
    state = latest_state(root, include_completed=not active_first)
    if state is None and active_first:
        state = latest_state(root, include_completed=True)
    if state is None:
        raise FileNotFoundError("no coordinator workflow state found")
    return state


def print_payload(payload: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                print(f"{key}={json.dumps(value, sort_keys=True)}")
            else:
                print(f"{key}={value}")
    else:
        print(payload)


def require_mutable_workflow(state: Mapping[str, Any]) -> None:
    status = state.get("status")
    if status == "completed":
        raise ValueError("completed workflows are immutable; initialize a new workflow for more work")
    if status == "aborted":
        raise ValueError("aborted workflows must be resumed before they can be changed")


def require_nonblank(name: str, value: Optional[str]) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{name} must not be blank")
    return normalized


def require_nonnegative(name: str, value: Optional[float]) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative")


def require_known_route(model: str, effort: str) -> None:
    if model not in KNOWN_MODELS:
        raise ValueError(f"unsupported child model: {model}")
    if effort not in KNOWN_REASONING_EFFORTS:
        raise ValueError(f"unsupported reasoning effort: {effort}")


def require_nonblank_list(name: str, values: Sequence[str]) -> None:
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError(f"{name} requires at least one nonblank value")


def require_fresh_route(route: Mapping[str, Any]) -> None:
    try:
        routed_at = parse_time(str(route.get("at")))
    except (TypeError, ValueError) as exc:
        raise ValueError("the child route has no valid routing timestamp") from exc
    age = utcnow() - routed_at
    if age < -dt.timedelta(minutes=5) or age > dt.timedelta(minutes=ROUTE_FRESHNESS_MINUTES):
        raise ValueError(
            f"child route is stale; rerun node-route within {ROUTE_FRESHNESS_MINUTES} minutes of spawning"
        )


def active_blockers_for_start(state: Mapping[str, Any], node_id: str) -> List[Mapping[str, Any]]:
    return [
        blocker
        for blocker in state.get("blockers", [])
        if blocker.get("status") == "active"
        and (blocker.get("scope") == "workflow" or blocker.get("node_id") == node_id)
    ]


def resume_workflow_status(state: MutableMapping[str, Any]) -> None:
    previous = state.pop("aborted_from_status", None)
    if any(
        blocker.get("status") == "active" and blocker.get("scope") == "workflow"
        for blocker in state.get("blockers", [])
    ):
        state["status"] = "blocked"
        return
    state["status"] = previous if previous in {"planning", "running"} else "running"


def read_text_input(
    inline: Optional[str], filename: Optional[str], inline_flag: str, file_flag: str
) -> str:
    if (inline is None) == (filename is None):
        raise ValueError(f"provide exactly one of {inline_flag} or {file_flag}")
    if filename is None:
        return require_nonblank(inline_flag, inline)
    if filename == "-":
        value = sys.stdin.read()
    else:
        try:
            value = pathlib.Path(filename).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"could not read {file_flag} {filename!r}: {exc}") from exc
    return require_nonblank(file_flag, value)


def read_json_input(
    inline: Optional[str], filename: Optional[str], inline_flag: str, file_flag: str
) -> Any:
    raw = read_text_input(inline, filename, inline_flag, file_flag)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{inline_flag}/{file_flag} is not valid JSON: {exc}") from exc


def command_init(args: argparse.Namespace, root: pathlib.Path) -> int:
    repo = str(pathlib.Path(args.repo).expanduser().resolve())
    task = read_text_input(args.task, args.task_file, "--task", "--task-file")
    with root_lock(root):
        state = None if args.new else find_matching_state(root, repo, task)
        resumed = state is not None
        if state is None:
            state = new_state(repo, task)
        else:
            if state.get("status") == "aborted":
                resume_workflow_status(state)
            add_event(state, "workflow_resumed", "Resumed matching coordinator workflow")
        state["git"]["latest"] = git_snapshot(repo)
        write_state(root, state)
    payload = progress_summary(state)
    payload["resumed"] = resumed
    payload["state_file"] = str(state_path(root, str(state["workflow_id"])))
    print_payload(payload, args.json)
    return 0


def command_resume(args: argparse.Namespace, root: pathlib.Path) -> int:
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        if state.get("status") == "completed":
            raise ValueError("completed workflows are immutable; initialize a new workflow for more work")
        if state.get("status") == "aborted":
            resume_workflow_status(state)
        state["git"]["latest"] = git_snapshot(str(state["repo"]))
        add_event(state, "workflow_resumed", args.message or "Resumed coordinator workflow")
        write_state(root, state)
    print_payload(progress_summary(state), args.json)
    return 0


def command_list(args: argparse.Namespace, root: pathlib.Path) -> int:
    states = sorted(iter_states(root), key=lambda state: state.get("updated_at", ""), reverse=True)
    payload = [
        {
            "workflow_id": state.get("workflow_id"),
            "status": state.get("status"),
            "phase": state.get("phase"),
            "repo": state.get("repo"),
            "task": state.get("task"),
            "updated_at": state.get("updated_at"),
        }
        for state in states
    ]
    if args.json:
        print_payload(payload, True)
    else:
        if not payload:
            print("no workflows")
        for item in payload:
            print(
                f"{item['workflow_id']}  {item['status']:<9}  {item['phase']:<18}  "
                f"{item['repo']}  {item['task']}"
            )
    return 0


def command_status(args: argparse.Namespace, root: pathlib.Path) -> int:
    state = resolve_state(root, args.workflow_id, active_first=True)
    payload = progress_summary(state)
    if args.verbose:
        payload["nodes"] = state.get("nodes", {})
        payload["requirements"] = state.get("requirements", [])
        payload["decisions"] = state.get("decisions", [])
        payload["recent_events"] = state.get("events", [])[-20:]
    print_payload(payload, args.json)
    return 0


def command_context(args: argparse.Namespace, root: pathlib.Path) -> int:
    state = resolve_state(root, args.workflow_id, active_first=True)
    nodes = state.get("nodes", {})
    payload = {
        "workflow": {
            "workflow_id": state.get("workflow_id"),
            "task": state.get("task"),
            "repo": state.get("repo"),
            "status": state.get("status"),
            "phase": state.get("phase"),
            "updated_at": state.get("updated_at"),
        },
        "progress": progress_summary(state),
        "requirements": state.get("requirements", []),
        "unfinished_nodes": {
            node_id: node
            for node_id, node in nodes.items()
            if node.get("status") not in TERMINAL_NODE_STATUSES
        },
        "completed_evidence": {
            node_id: {
                "stage": node.get("stage"),
                "summary": node.get("summary"),
                "artifacts": node.get("artifacts", []),
                "validation_evidence": node.get("validation_evidence", []),
                "superseded_by": node.get("superseded_by"),
            }
            for node_id, node in nodes.items()
            if node.get("status") in TERMINAL_NODE_STATUSES
        },
        "recent_decisions": state.get("decisions", [])[-20:],
        "active_blockers": [
            item for item in state.get("blockers", []) if item.get("status") == "active"
        ],
        "recent_events": state.get("events", [])[-30:],
    }
    print_payload(payload, True if args.json else False)
    return 0


def command_phase(args: argparse.Namespace, root: pathlib.Path) -> int:
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        state["phase"] = args.phase
        if state.get("status") == "planning":
            state["status"] = "running"
        add_event(state, "phase_changed", args.message or f"Phase changed to {args.phase}")
        write_state(root, state)
    print_payload(progress_summary(state), args.json)
    return 0


def command_node_add(args: argparse.Namespace, root: pathlib.Path) -> int:
    require_nonblank("--node-id", args.node_id)
    require_nonblank("--title", args.title)
    require_nonblank("--stage", args.stage)
    require_nonblank("--agent-type", args.agent_type)
    require_nonblank("--model", args.model)
    require_nonblank("--reasoning-effort", args.reasoning_effort)
    require_nonblank("--routing-rationale", args.routing_rationale)
    require_known_route(args.model, args.reasoning_effort)
    if args.agent_type not in KNOWN_AGENT_TYPES:
        raise ValueError(f"unsupported agent type: {args.agent_type}")
    require_nonblank_list("--write-scope", args.write_scope)
    require_nonblank_list("--acceptance-criterion", args.acceptance_criterion)
    require_nonblank_list("--output-contract", args.output_contract)
    require_nonblank_list("--validation-command", args.validation_command)
    if len(set(args.dependency)) != len(args.dependency):
        raise ValueError("duplicate --dependency values are not allowed")
    require_nonnegative("--estimated-cost-usd", args.estimated_cost_usd)
    if not 0 <= args.priority <= 100:
        raise ValueError("--priority must be between 0 and 100")
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        if args.node_id in nodes:
            raise ValueError(f"node already exists: {args.node_id}")
        missing_dependencies = [dependency for dependency in args.dependency if dependency not in nodes]
        if missing_dependencies:
            raise ValueError(
                "dependencies must exist before adding a node: " + ", ".join(missing_dependencies)
            )
        initial_route = {
            "attempt": 1,
            "at": iso_now(),
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "routing_rationale": args.routing_rationale,
            "estimated_cost_usd": args.estimated_cost_usd,
        }
        node = {
            "node_id": args.node_id,
            "stage": args.stage,
            "title": args.title,
            "agent_type": args.agent_type,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "routing_rationale": args.routing_rationale,
            "estimated_cost_usd": args.estimated_cost_usd,
            "route_history": [initial_route],
            "actual_cost_usd": None,
            "attempt_costs": [],
            "dependencies": list(args.dependency),
            "priority": args.priority,
            "revision": 1,
            "supersedes": [],
            "superseded_by": None,
            "write_scope": list(args.write_scope),
            "acceptance_criteria": list(args.acceptance_criterion),
            "output_contract": list(args.output_contract),
            "validation_commands": list(args.validation_command),
            "artifacts": [],
            "validation_evidence": [],
            "status": "pending",
            "attempt": 0,
            "agent_id": None,
            "task_name": None,
            "agent_name": None,
            "agent_nickname": None,
            "created_at": iso_now(),
            "started_at": None,
            "completed_at": None,
            "summary": None,
            "error": None,
            "review_round": args.review_round,
            "finding_ids": list(args.finding_id),
        }
        nodes[args.node_id] = node
        require_valid_graph(nodes)
        state["graph_revision"] = int(state.get("graph_revision", 0)) + 1
        add_event(
            state,
            "node_added",
            f"Added {args.node_id}: {args.title}",
            {
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "stage": args.stage,
            },
        )
        write_state(root, state)
    print_payload(node, args.json)
    return 0


def command_node_route(args: argparse.Namespace, root: pathlib.Path) -> int:
    require_nonblank("--model", args.model)
    require_nonblank("--reasoning-effort", args.reasoning_effort)
    require_nonblank("--routing-rationale", args.routing_rationale)
    require_known_route(args.model, args.reasoning_effort)
    require_nonnegative("--estimated-cost-usd", args.estimated_cost_usd)
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        if args.node_id not in nodes:
            raise ValueError(f"unknown node: {args.node_id}")
        node = nodes[args.node_id]
        if node.get("status") in {"running", "done", "skipped"}:
            raise ValueError(
                f"cannot reroute node {args.node_id} while status is {node.get('status')}"
            )
        next_attempt = int(node.get("attempt", 0)) + 1
        route = {
            "attempt": next_attempt,
            "at": iso_now(),
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "routing_rationale": args.routing_rationale,
            "estimated_cost_usd": args.estimated_cost_usd,
        }
        history = node.setdefault("route_history", [])
        if history and int(history[-1].get("attempt", 0)) == next_attempt:
            history[-1] = route
        else:
            history.append(route)
        node["model"] = args.model
        node["reasoning_effort"] = args.reasoning_effort
        node["routing_rationale"] = args.routing_rationale
        node["estimated_cost_usd"] = args.estimated_cost_usd
        node["agent_id"] = None
        node["task_name"] = None
        node["agent_name"] = None
        node["agent_nickname"] = None
        node["error"] = None
        add_event(
            state,
            "node_routed",
            f"Routed {args.node_id} attempt {next_attempt} to {args.model}/{args.reasoning_effort}",
            {"attempt": next_attempt, "estimated_cost_usd": args.estimated_cost_usd},
        )
        write_state(root, state)
    print_payload(node, args.json)
    return 0


def command_node_update(args: argparse.Namespace, root: pathlib.Path) -> int:
    require_nonnegative("--actual-cost-usd", args.actual_cost_usd)
    incoming_artifacts = (
        None
        if args.artifact is None
        else [require_nonblank("--artifact", item) for item in args.artifact]
    )
    incoming_validation = (
        None
        if args.validation_evidence is None
        else [
            require_nonblank("--validation-evidence", item)
            for item in args.validation_evidence
        ]
    )
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        if args.node_id not in nodes:
            raise ValueError(f"unknown node: {args.node_id}")
        node = nodes[args.node_id]
        old_status = node.get("status")
        if old_status in TERMINAL_NODE_STATUSES:
            raise ValueError(
                f"completed evidence is immutable; add a corrective node downstream of {args.node_id}"
            )
        if args.status:
            if args.status not in KNOWN_NODE_STATUSES:
                raise ValueError(f"unknown node status: {args.status}")
            if args.status == "blocked":
                raise ValueError("use the block command to record a node blocker with evidence")
            if args.status == "ready":
                blockers = active_blockers_for_start(state, args.node_id)
                unfinished_dependencies = {
                    dependency: nodes.get(dependency, {}).get("status")
                    for dependency in node.get("dependencies", [])
                    if nodes.get(dependency, {}).get("status") not in TERMINAL_NODE_STATUSES
                }
                if blockers or unfinished_dependencies:
                    raise ValueError(
                        f"cannot mark {args.node_id} ready while blockers or unfinished dependencies remain"
                    )
            allowed = ALLOWED_NODE_TRANSITIONS.get(str(old_status), set())
            if args.status not in allowed:
                raise ValueError(
                    f"invalid node transition for {args.node_id}: {old_status} -> {args.status}"
                )
            if args.status == "running":
                if old_status not in STARTABLE_NODE_STATUSES:
                    raise ValueError(f"node {args.node_id} cannot start from {old_status}")
                blockers = active_blockers_for_start(state, args.node_id)
                if blockers:
                    raise ValueError(
                        f"cannot start {args.node_id} while active blockers remain: "
                        + ", ".join(str(item.get("blocker_id")) for item in blockers)
                    )
                unfinished_dependencies = {
                    dependency: nodes.get(dependency, {}).get("status")
                    for dependency in node.get("dependencies", [])
                    if nodes.get(dependency, {}).get("status") not in TERMINAL_NODE_STATUSES
                }
                if unfinished_dependencies:
                    raise ValueError(
                        f"cannot start {args.node_id} before dependencies are terminal: "
                        + json.dumps(unfinished_dependencies, sort_keys=True)
                    )
                if not args.increment_attempt:
                    raise ValueError("starting a child requires --increment-attempt")
                if not any((args.agent_id, args.task_name, args.agent_name)):
                    raise ValueError(
                        "starting a child requires at least one returned identifier: "
                        "--agent-id, --task-name, or --agent-name"
                    )
                next_attempt = int(node.get("attempt", 0)) + 1
                history = node.get("route_history") or []
                if not history or int(history[-1].get("attempt", 0)) != next_attempt:
                    raise ValueError(
                        f"route attempt {next_attempt} before spawning {args.node_id} with node-route"
                    )
                require_fresh_route(history[-1])
                node["attempt"] = next_attempt
                node["started_at"] = iso_now()
                node["completed_at"] = None
                node["error"] = None
                if state.get("status") == "planning":
                    state["status"] = "running"
            elif args.increment_attempt:
                raise ValueError("--increment-attempt is valid only when status becomes running")
            if args.status in {"done", "skipped"}:
                require_nonblank("--summary", args.summary)
            if args.status == "done":
                effective_validation = list(node.get("validation_evidence") or [])
                for item in incoming_validation or []:
                    if item not in effective_validation:
                        effective_validation.append(item)
                if not effective_validation:
                    raise ValueError(
                        "marking a node done requires at least one concrete "
                        "--validation-evidence entry"
                    )
            if args.status in {"failed", "interrupted"}:
                require_nonblank("--error", args.error)
            node["status"] = args.status
            if args.status in TERMINAL_NODE_STATUSES | {"failed", "interrupted"}:
                node["completed_at"] = iso_now()
        elif args.increment_attempt:
            raise ValueError("--increment-attempt requires --status running")
        if args.agent_id is not None:
            node["agent_id"] = args.agent_id
        if args.task_name is not None:
            node["task_name"] = args.task_name
        if args.agent_name is not None:
            node["agent_name"] = args.agent_name
        if args.agent_nickname is not None:
            node["agent_nickname"] = args.agent_nickname
        if args.summary is not None:
            node["summary"] = args.summary
        if args.error is not None:
            node["error"] = args.error
        if incoming_artifacts is not None:
            artifacts = node.setdefault("artifacts", [])
            for value in incoming_artifacts:
                if value not in artifacts:
                    artifacts.append(value)
        if incoming_validation is not None:
            evidence = node.setdefault("validation_evidence", [])
            for value in incoming_validation:
                if value not in evidence:
                    evidence.append(value)
        if args.actual_cost_usd is not None:
            attempt = int(node.get("attempt", 0))
            if attempt < 1:
                raise ValueError("actual child cost cannot be recorded before an attempt starts")
            costs = node.setdefault("attempt_costs", [])
            entry = {
                "attempt": attempt,
                "at": iso_now(),
                "actual_cost_usd": args.actual_cost_usd,
            }
            existing = next((item for item in costs if int(item.get("attempt", 0)) == attempt), None)
            if existing is None:
                costs.append(entry)
            else:
                existing.update(entry)
            node["actual_cost_usd"] = sum(
                float(item["actual_cost_usd"])
                for item in costs
                if isinstance(item.get("actual_cost_usd"), (int, float))
            )
        add_event(
            state,
            "node_updated",
            args.message or f"Updated {args.node_id}: {old_status} -> {node.get('status')}",
            {
                "agent_id": node.get("agent_id"),
                "task_name": node.get("task_name"),
                "agent_name": node.get("agent_name"),
                "agent_nickname": node.get("agent_nickname"),
                "attempt": node.get("attempt"),
            },
        )
        require_valid_graph(nodes)
        write_state(root, state)
    print_payload(node, args.json)
    return 0


def _bump_node_revision(node: MutableMapping[str, Any]) -> None:
    node["revision"] = int(node.get("revision", 0)) + 1


def command_node_patch(args: argparse.Namespace, root: pathlib.Path) -> int:
    reason = require_nonblank("--reason", args.reason)
    supplied = any(
        value is not None
        for value in (
            args.title,
            args.stage,
            args.agent_type,
            args.priority,
            args.write_scope,
            args.acceptance_criterion,
            args.output_contract,
            args.validation_command,
        )
    ) or args.requeue
    if not supplied:
        raise ValueError("node-patch requires at least one changed field or --requeue")
    if args.priority is not None and not 0 <= args.priority <= 100:
        raise ValueError("--priority must be between 0 and 100")
    if args.title is not None:
        require_nonblank("--title", args.title)
    if args.stage is not None:
        require_nonblank("--stage", args.stage)
    if args.agent_type is not None and args.agent_type not in KNOWN_AGENT_TYPES:
        raise ValueError(f"unsupported agent type: {args.agent_type}")
    if args.write_scope is not None:
        require_nonblank_list("--write-scope", args.write_scope)
    if args.acceptance_criterion is not None:
        require_nonblank_list("--acceptance-criterion", args.acceptance_criterion)
    if args.output_contract is not None:
        require_nonblank_list("--output-contract", args.output_contract)
    if args.validation_command is not None:
        require_nonblank_list("--validation-command", args.validation_command)
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        if args.node_id not in nodes:
            raise ValueError(f"unknown node: {args.node_id}")
        node = nodes[args.node_id]
        require_replannable_node(args.node_id, node)
        changed: Dict[str, Any] = {}
        for attribute, value in (
            ("title", args.title),
            ("stage", args.stage),
            ("agent_type", args.agent_type),
            ("priority", args.priority),
            ("write_scope", args.write_scope),
            ("acceptance_criteria", args.acceptance_criterion),
            ("output_contract", args.output_contract),
            ("validation_commands", args.validation_command),
        ):
            if value is not None and node.get(attribute) != value:
                changed[attribute] = {"before": node.get(attribute), "after": value}
                node[attribute] = value
        if args.requeue:
            before = node.get("status")
            if before == "blocked" and active_blockers_for_start(state, args.node_id):
                raise ValueError("resolve the node blocker before requeueing it")
            node["status"] = "pending"
            node["error"] = None
            node["completed_at"] = None
            changed["status"] = {"before": before, "after": "pending"}
        _bump_node_revision(node)
        state["graph_revision"] = int(state.get("graph_revision", 0)) + 1
        require_valid_graph(nodes)
        add_event(
            state,
            "node_replanned",
            f"Replanned {args.node_id}: {reason}",
            {"changes": changed, "graph_revision": state["graph_revision"]},
        )
        write_state(root, state)
    print_payload(node, args.json)
    return 0


def command_dependency_add(args: argparse.Namespace, root: pathlib.Path) -> int:
    reason = require_nonblank("--reason", args.reason)
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        if args.node_id not in nodes or args.dependency not in nodes:
            raise ValueError("both --node-id and --dependency must name existing nodes")
        node = nodes[args.node_id]
        require_replannable_node(args.node_id, node)
        dependencies = node.setdefault("dependencies", [])
        if args.dependency in dependencies:
            raise ValueError(f"dependency already exists: {args.node_id} <- {args.dependency}")
        dependencies.append(args.dependency)
        previous_status = node.get("status")
        if previous_status == "ready":
            node["status"] = "pending"
        try:
            require_valid_graph(nodes)
        except Exception:
            dependencies.remove(args.dependency)
            node["status"] = previous_status
            raise
        _bump_node_revision(node)
        state["graph_revision"] = int(state.get("graph_revision", 0)) + 1
        add_event(
            state,
            "dependency_added",
            f"Added {args.node_id} <- {args.dependency}: {reason}",
            {"graph_revision": state["graph_revision"]},
        )
        write_state(root, state)
    print_payload(node, args.json)
    return 0


def command_dependency_remove(args: argparse.Namespace, root: pathlib.Path) -> int:
    reason = require_nonblank("--reason", args.reason)
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        if args.node_id not in nodes:
            raise ValueError(f"unknown node: {args.node_id}")
        node = nodes[args.node_id]
        require_replannable_node(args.node_id, node)
        dependencies = node.setdefault("dependencies", [])
        if args.dependency not in dependencies:
            raise ValueError(f"dependency does not exist: {args.node_id} <- {args.dependency}")
        dependencies.remove(args.dependency)
        _bump_node_revision(node)
        state["graph_revision"] = int(state.get("graph_revision", 0)) + 1
        require_valid_graph(nodes)
        add_event(
            state,
            "dependency_removed",
            f"Removed {args.node_id} <- {args.dependency}: {reason}",
            {"graph_revision": state["graph_revision"]},
        )
        write_state(root, state)
    print_payload(node, args.json)
    return 0


def _apply_supersede(
    nodes: MutableMapping[str, MutableMapping[str, Any]],
    old_id: str,
    replacement_id: str,
    reason: str,
    *,
    validate: bool = True,
) -> Dict[str, Any]:
    if old_id not in nodes or replacement_id not in nodes:
        raise ValueError("superseded and replacement nodes must already exist")
    if old_id == replacement_id:
        raise ValueError("a node cannot supersede itself")
    old = nodes[old_id]
    replacement = nodes[replacement_id]
    require_replannable_node(old_id, old)
    require_replannable_node(replacement_id, replacement)
    old["status"] = "skipped"
    old["summary"] = f"Superseded by {replacement_id}: {reason}"
    old["completed_at"] = iso_now()
    old["superseded_by"] = replacement_id
    replacement.setdefault("supersedes", [])
    if old_id not in replacement["supersedes"]:
        replacement["supersedes"].append(old_id)
    rewritten: List[str] = []
    for node_id, node in nodes.items():
        dependencies = list(node.get("dependencies", []))
        if old_id not in dependencies:
            continue
        if node_id == replacement_id:
            node["dependencies"] = [item for item in dependencies if item != old_id]
        else:
            node["dependencies"] = list(
                dict.fromkeys(replacement_id if item == old_id else item for item in dependencies)
            )
            rewritten.append(node_id)
        if node.get("status") == "ready":
            node["status"] = "pending"
        _bump_node_revision(node)
    _bump_node_revision(old)
    _bump_node_revision(replacement)
    if validate:
        require_valid_graph(nodes)
    return {"old": old_id, "replacement": replacement_id, "rewritten_dependents": rewritten}


def command_node_supersede(args: argparse.Namespace, root: pathlib.Path) -> int:
    reason = require_nonblank("--reason", args.reason)
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        blocked_ids = {
            blocker.get("node_id")
            for blocker in state.get("blockers", [])
            if blocker.get("status") == "active" and blocker.get("node_id")
        }
        if args.node_id in blocked_ids or args.replacement in blocked_ids:
            raise ValueError("resolve active node blockers before superseding either node")
        snapshot = copy.deepcopy(nodes)
        try:
            details = _apply_supersede(nodes, args.node_id, args.replacement, reason)
        except Exception:
            state["nodes"] = snapshot
            raise
        state["graph_revision"] = int(state.get("graph_revision", 0)) + 1
        details["graph_revision"] = state["graph_revision"]
        add_event(
            state,
            "node_superseded",
            f"Superseded {args.node_id} with {args.replacement}: {reason}",
            details,
        )
        write_state(root, state)
    print_payload(details, args.json)
    return 0


def command_node_remove(args: argparse.Namespace, root: pathlib.Path) -> int:
    reason = require_nonblank("--reason", args.reason)
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        nodes = state.setdefault("nodes", {})
        if args.node_id not in nodes:
            raise ValueError(f"unknown node: {args.node_id}")
        node = nodes[args.node_id]
        if node.get("status") not in {"pending", "ready"} or int(node.get("attempt", 0)) != 0:
            raise ValueError("only never-started pending/ready nodes may be removed; supersede attempted work instead")
        dependents = [node_id for node_id, item in nodes.items() if args.node_id in item.get("dependencies", [])]
        if dependents:
            raise ValueError("remove or rewire dependent edges first: " + ", ".join(sorted(dependents)))
        if any(
            blocker.get("status") == "active" and blocker.get("node_id") == args.node_id
            for blocker in state.get("blockers", [])
        ):
            raise ValueError("resolve the node blocker before removing the node")
        removed = nodes.pop(args.node_id)
        state["graph_revision"] = int(state.get("graph_revision", 0)) + 1
        require_valid_graph(nodes)
        add_event(
            state,
            "node_removed",
            f"Removed unstarted node {args.node_id}: {reason}",
            {"graph_revision": state["graph_revision"], "removed_title": removed.get("title")},
        )
        write_state(root, state)
    print_payload({"removed": args.node_id, "graph_revision": state["graph_revision"]}, args.json)
    return 0


def _write_scope_overlaps(nodes: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    owners: Dict[str, List[str]] = {}
    for node_id, node in nodes.items():
        if node.get("status") in TERMINAL_NODE_STATUSES:
            continue
        for scope in node.get("write_scope", []):
            normalized = str(scope).strip()
            if not normalized or normalized.lower() == "none":
                continue
            owners.setdefault(normalized, []).append(node_id)
    return [
        {"write_scope": scope, "nodes": sorted(node_ids)}
        for scope, node_ids in sorted(owners.items())
        if len(node_ids) > 1
    ]


def command_graph_validate(args: argparse.Namespace, root: pathlib.Path) -> int:
    state = resolve_state(root, args.workflow_id, active_first=True)
    nodes = state.get("nodes", {})
    diagnostics = graph_diagnostics(nodes)
    diagnostics["graph_revision"] = int(state.get("graph_revision", 0))
    diagnostics["ready_nodes"] = ready_node_ids(state)
    print_payload(diagnostics, args.json)
    return 0 if diagnostics["ok"] else 2


def command_graph_replan(args: argparse.Namespace, root: pathlib.Path) -> int:
    plan = read_json_input(args.plan_json, args.plan_file, "--plan-json", "--plan-file")
    if not isinstance(plan, dict) or not isinstance(plan.get("operations"), list):
        raise ValueError("plan input must be an object with an operations array")
    reason = require_nonblank("plan.reason", str(plan.get("reason") or ""))
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        original_nodes = state.setdefault("nodes", {})
        nodes: MutableMapping[str, MutableMapping[str, Any]] = copy.deepcopy(original_nodes)
        applied: List[Dict[str, Any]] = []
        for index, operation in enumerate(plan["operations"]):
            if not isinstance(operation, dict):
                raise ValueError(f"operation {index} is not an object")
            kind = operation.get("op")
            node_id = str(operation.get("node_id") or "")
            if kind == "dependency_add":
                dependency = str(operation.get("dependency") or "")
                if node_id not in nodes or dependency not in nodes:
                    raise ValueError(f"operation {index} references an unknown node")
                require_replannable_node(node_id, nodes[node_id])
                if dependency in nodes[node_id].setdefault("dependencies", []):
                    raise ValueError(f"operation {index} adds an existing dependency")
                nodes[node_id]["dependencies"].append(dependency)
                if nodes[node_id].get("status") == "ready":
                    nodes[node_id]["status"] = "pending"
                _bump_node_revision(nodes[node_id])
            elif kind == "dependency_remove":
                dependency = str(operation.get("dependency") or "")
                if node_id not in nodes or dependency not in nodes[node_id].get("dependencies", []):
                    raise ValueError(f"operation {index} removes an unknown dependency")
                require_replannable_node(node_id, nodes[node_id])
                nodes[node_id]["dependencies"].remove(dependency)
                _bump_node_revision(nodes[node_id])
            elif kind == "patch":
                if node_id not in nodes:
                    raise ValueError(f"operation {index} references an unknown node")
                node = nodes[node_id]
                require_replannable_node(node_id, node)
                allowed = {
                    "title", "stage", "agent_type", "priority", "write_scope",
                    "acceptance_criteria", "output_contract", "validation_commands",
                }
                changes = operation.get("changes")
                if not isinstance(changes, dict) or not changes or set(changes) - allowed:
                    raise ValueError(f"operation {index} has invalid patch fields")
                if "priority" in changes:
                    priority = changes["priority"]
                    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
                        raise ValueError(f"operation {index} priority must be an integer from 0 through 100")
                for field in ("title", "stage"):
                    if field in changes and (not isinstance(changes[field], str) or not changes[field].strip()):
                        raise ValueError(f"operation {index} {field} must be a nonblank string")
                if "agent_type" in changes and changes["agent_type"] not in KNOWN_AGENT_TYPES:
                    raise ValueError(f"operation {index} has unsupported agent_type")
                for field in ("write_scope", "acceptance_criteria", "output_contract", "validation_commands"):
                    if field in changes:
                        value = changes[field]
                        if not isinstance(value, list) or not value or any(
                            not isinstance(item, str) or not item.strip() for item in value
                        ):
                            raise ValueError(f"operation {index} {field} must be a nonempty string array")
                node.update(changes)
                if operation.get("requeue"):
                    if node.get("status") == "blocked" and active_blockers_for_start(state, node_id):
                        raise ValueError(f"operation {index} must resolve the node blocker before requeueing")
                    node["status"] = "pending"
                    node["error"] = None
                    node["completed_at"] = None
                _bump_node_revision(node)
            elif kind == "supersede":
                replacement = str(operation.get("replacement") or "")
                blocked_ids = {
                    blocker.get("node_id")
                    for blocker in state.get("blockers", [])
                    if blocker.get("status") == "active" and blocker.get("node_id")
                }
                if node_id in blocked_ids or replacement in blocked_ids:
                    raise ValueError(f"operation {index} must resolve active blockers before supersession")
                _apply_supersede(nodes, node_id, replacement, reason, validate=False)
            elif kind == "remove":
                if node_id not in nodes:
                    raise ValueError(f"operation {index} references an unknown node")
                node = nodes[node_id]
                if node.get("status") not in {"pending", "ready"} or int(node.get("attempt", 0)) != 0:
                    raise ValueError(f"operation {index} may remove only unstarted pending/ready nodes")
                if any(node_id in item.get("dependencies", []) for item in nodes.values()):
                    raise ValueError(f"operation {index} must rewire dependents before removal")
                if any(
                    blocker.get("status") == "active" and blocker.get("node_id") == node_id
                    for blocker in state.get("blockers", [])
                ):
                    raise ValueError(f"operation {index} must resolve the node blocker before removal")
                nodes.pop(node_id)
            else:
                raise ValueError(f"unsupported operation {index}: {kind!r}")
            applied.append(dict(operation))
        require_valid_graph(nodes)
        state["nodes"] = nodes
        state["graph_revision"] = int(state.get("graph_revision", 0)) + 1
        add_event(
            state,
            "graph_replanned",
            reason,
            {"operations": applied, "graph_revision": state["graph_revision"]},
        )
        write_state(root, state)
    print_payload(
        {
            "workflow_id": state["workflow_id"],
            "graph_revision": state["graph_revision"],
            "operations_applied": len(applied),
            "ready_nodes": ready_node_ids(state),
        },
        args.json,
    )
    return 0


def _parse_json_object(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("--details-json must contain a JSON object")
    return parsed


def command_event(args: argparse.Namespace, root: pathlib.Path) -> int:
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        add_event(state, args.kind, args.message, _parse_json_object(args.details_json))
        write_state(root, state)
    print_payload(state["events"][-1], args.json)
    return 0


def command_requirement_set(args: argparse.Namespace, root: pathlib.Path) -> int:
    requirement_id = require_nonblank("--requirement-id", args.requirement_id)
    text = require_nonblank("--text", args.text)
    source = require_nonblank("--source", args.source)
    supplied_evidence = [require_nonblank("--evidence", item) for item in args.evidence]
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        requirements = state.setdefault("requirements", [])
        existing = next(
            (item for item in requirements if item.get("requirement_id") == requirement_id),
            None,
        )
        evidence = list(supplied_evidence)
        if not evidence and existing is not None:
            evidence = [
                require_nonblank("existing requirement evidence", str(item))
                for item in existing.get("evidence", [])
            ]
        if args.status in {"satisfied", "superseded"} and not evidence:
            raise ValueError(
                f"requirement status {args.status} requires at least one concrete --evidence entry"
            )
        now = iso_now()
        record = {
            "requirement_id": requirement_id,
            "text": text,
            "source": source,
            "status": args.status,
            "confidence": args.confidence,
            "evidence": evidence,
            "updated_at": now,
        }
        if existing is None:
            record["created_at"] = now
            requirements.append(record)
            event_kind = "requirement_added"
        else:
            record["created_at"] = existing.get("created_at") or now
            existing.clear()
            existing.update(record)
            record = existing
            event_kind = "requirement_updated"
        add_event(
            state,
            event_kind,
            f"{requirement_id}: {text}",
            {"status": args.status, "confidence": args.confidence, "source": source},
        )
        write_state(root, state)
    print_payload(record, args.json)
    return 0


def command_decision(args: argparse.Namespace, root: pathlib.Path) -> int:
    if args.confidence != "high":
        raise ValueError("medium/low-confidence ambiguity must use the block command")
    question = require_nonblank("--question", args.question)
    resolved = require_nonblank("--decision", args.decision)
    rationale = require_nonblank("--rationale", args.rationale)
    decision = {
        "decision_id": "decision-" + secrets.token_hex(4),
        "at": iso_now(),
        "confidence": args.confidence,
        "question": question,
        "decision": resolved,
        "rationale": rationale,
        "alternatives": [item for item in args.alternative if item.strip()],
    }
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        state.setdefault("decisions", []).append(decision)
        add_event(state, "decision_recorded", resolved, {"confidence": args.confidence})
        write_state(root, state)
    print_payload(decision, args.json)
    return 0


def command_block(args: argparse.Namespace, root: pathlib.Path) -> int:
    reason = require_nonblank("--reason", args.reason)
    needed = require_nonblank("--needed", args.needed)
    recommendation = require_nonblank("--recommendation", args.recommendation)
    examples = [require_nonblank("--example", item) for item in args.example]
    impacts = [require_nonblank("--impact", item) for item in args.impact]
    if not examples:
        raise ValueError("a blocker requires at least one concrete --example")
    if not impacts:
        raise ValueError("a blocker requires at least one concrete --impact")

    scope = "workflow" if args.workflow_wide else "node"
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        node = None
        previous_node_status = None
        previous_workflow_status = state.get("status")
        active_blockers = [
            item for item in state.get("blockers", []) if item.get("status") == "active"
        ]
        if args.node_id:
            node = state.setdefault("nodes", {}).get(args.node_id)
            if node is None:
                raise ValueError(f"unknown node: {args.node_id}")
            if node.get("status") in TERMINAL_NODE_STATUSES:
                raise ValueError(f"cannot block terminal node {args.node_id}")
            if node.get("status") == "running":
                raise ValueError(
                    f"interrupt live child {args.node_id}, mark it interrupted, then record the blocker"
                )
            previous_node_status = node.get("status")
            if previous_node_status == "blocked":
                previous_node_status = next(
                    (
                        item.get("previous_node_status")
                        for item in active_blockers
                        if item.get("node_id") == args.node_id
                        and item.get("previous_node_status") in STARTABLE_NODE_STATUSES
                    ),
                    "pending",
                )
        else:
            if any(node.get("status") == "running" for node in state.get("nodes", {}).values()):
                raise ValueError(
                    "interrupt all live children and mark them interrupted before creating a workflow blocker"
                )
            if previous_workflow_status == "blocked":
                previous_workflow_status = next(
                    (
                        item.get("previous_workflow_status")
                        for item in active_blockers
                        if item.get("scope") == "workflow"
                        and item.get("previous_workflow_status") in {"planning", "running"}
                    ),
                    "running",
                )

        blocker = {
            "blocker_id": "blocker-" + secrets.token_hex(4),
            "status": "active",
            "scope": scope,
            "created_at": iso_now(),
            "resolved_at": None,
            "confidence": args.confidence,
            "reason": reason,
            "needed_to_continue": needed,
            "recommendation": recommendation,
            "examples": examples,
            "impacts": impacts,
            "resolution": None,
            "node_id": args.node_id,
            "previous_workflow_status": previous_workflow_status,
            "previous_node_status": previous_node_status,
        }
        state.setdefault("blockers", []).append(blocker)
        if scope == "workflow":
            state["status"] = "blocked"
        elif node is not None:
            node["status"] = "blocked"
            node["error"] = reason
        add_event(
            state,
            "workflow_blocked" if scope == "workflow" else "node_blocked",
            reason,
            {"blocker_id": blocker["blocker_id"], "scope": scope, "node_id": args.node_id},
        )
        write_state(root, state)
    print_payload(blocker, args.json)
    return 0


def command_unblock(args: argparse.Namespace, root: pathlib.Path) -> int:
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        blocker = next(
            (item for item in state.get("blockers", []) if item.get("blocker_id") == args.blocker_id),
            None,
        )
        if blocker is None:
            raise ValueError(f"unknown blocker: {args.blocker_id}")
        if blocker.get("status") != "active":
            raise ValueError(f"blocker is already resolved: {args.blocker_id}")
        blocker["status"] = "resolved"
        blocker["resolved_at"] = iso_now()
        blocker["resolution"] = args.resolution
        if blocker.get("node_id"):
            same_node_active = [
                item
                for item in state.get("blockers", [])
                if item is not blocker
                and item.get("status") == "active"
                and item.get("node_id") == blocker.get("node_id")
            ]
            node = state.get("nodes", {}).get(blocker["node_id"])
            if node and node.get("status") == "blocked" and not same_node_active:
                restored = blocker.get("previous_node_status")
                node["status"] = restored if restored in STARTABLE_NODE_STATUSES else "pending"
                node["error"] = None
        workflow_blockers = [
            item
            for item in state.get("blockers", [])
            if item.get("status") == "active" and item.get("scope") == "workflow"
        ]
        if state.get("status") == "blocked" and not workflow_blockers:
            restored = blocker.get("previous_workflow_status")
            state["status"] = restored if restored in {"planning", "running"} else "running"
        add_event(state, "workflow_unblocked", args.resolution, {"blocker_id": args.blocker_id})
        write_state(root, state)
    print_payload(blocker, args.json)
    return 0


def command_reconcile_git(args: argparse.Namespace, root: pathlib.Path) -> int:
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        snapshot = git_snapshot(str(state["repo"]))
        state["git"]["latest"] = snapshot
        add_event(state, "git_reconciled", args.message or "Refreshed git snapshot")
        write_state(root, state)
    print_payload(snapshot, args.json)
    return 0


def command_finish(args: argparse.Namespace, root: pathlib.Path) -> int:
    require_nonblank("--summary", args.summary)
    require_nonblank("--commit", args.commit)
    if not args.validation or any(not item.strip() for item in args.validation):
        raise ValueError("every --validation entry must be non-blank")
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        active_blockers = [item for item in state.get("blockers", []) if item.get("status") == "active"]
        if not state.get("nodes"):
            raise ValueError("cannot finish a workflow with no execution nodes")
        require_valid_graph(state.get("nodes", {}))
        unfinished = {
            node_id: node.get("status")
            for node_id, node in state.get("nodes", {}).items()
            if node.get("status") not in TERMINAL_NODE_STATUSES
        }
        if active_blockers:
            raise ValueError("cannot finish while active blockers remain")
        if unfinished:
            raise ValueError("cannot finish with unfinished nodes: " + json.dumps(unfinished, sort_keys=True))
        requirements = state.get("requirements", [])
        if not isinstance(requirements, list) or not requirements:
            raise ValueError(
                "cannot finish without a persisted requirement ledger; use requirement-set"
            )
        malformed_requirements: List[Dict[str, Any]] = []
        active_requirements: List[str] = []
        seen_requirement_ids: set = set()
        for index, requirement in enumerate(requirements):
            if not isinstance(requirement, Mapping):
                malformed_requirements.append({"index": index, "issues": ["entry must be an object"]})
                continue
            issues: List[str] = []
            requirement_id = requirement.get("requirement_id")
            for field in ("requirement_id", "text", "source"):
                value = requirement.get(field)
                if not isinstance(value, str) or not value.strip():
                    issues.append(f"{field} must be a nonblank string")
            if requirement.get("status") not in KNOWN_REQUIREMENT_STATUSES:
                issues.append("unknown status")
            if requirement.get("confidence") not in {"high", "medium", "low"}:
                issues.append("unknown confidence")
            evidence = requirement.get("evidence")
            if (
                not isinstance(evidence, list)
                or any(not isinstance(item, str) or not item.strip() for item in evidence)
            ):
                issues.append("evidence must be a list of nonblank strings")
            if requirement.get("status") in {"satisfied", "superseded"} and not evidence:
                issues.append("resolved requirements require concrete evidence")
            if requirement.get("status") == "active":
                active_requirements.append(str(requirement_id or f"index:{index}"))
            if isinstance(requirement_id, str) and requirement_id.strip():
                if requirement_id in seen_requirement_ids:
                    issues.append("duplicate requirement_id")
                seen_requirement_ids.add(requirement_id)
            if issues:
                malformed_requirements.append(
                    {"requirement_id": requirement_id, "index": index, "issues": issues}
                )
        if malformed_requirements:
            raise ValueError(
                "cannot finish with malformed requirement evidence: "
                + json.dumps(malformed_requirements, sort_keys=True)
            )
        if active_requirements:
            raise ValueError(
                "cannot finish while requirements remain active: "
                + json.dumps(active_requirements)
            )
        latest = git_snapshot(str(state["repo"]))
        if not latest.get("available"):
            raise ValueError("cannot finish without a verifiable Git repository and final commit")
        if latest.get("head") != args.commit:
            raise ValueError(
                f"recorded commit {args.commit} does not match repository HEAD {latest.get('head')}"
            )
        initial = state.get("git", {}).get("initial") or {}
        if initial.get("available") and initial.get("head") == latest.get("head"):
            raise ValueError("final HEAD is unchanged from workflow start; create the required task commit")
        initial_dirty = set(initial.get("dirty_entries") or [])
        new_dirty = sorted(set(latest.get("dirty_entries") or []) - initial_dirty)
        if new_dirty:
            raise ValueError(
                "cannot finish with new uncommitted task changes: " + json.dumps(new_dirty)
            )
        if initial.get("available"):
            initial_fingerprint = initial.get("dirty_fingerprint")
            latest_fingerprint = latest.get("dirty_fingerprint")
            if (
                not isinstance(initial_fingerprint, str)
                or not isinstance(latest_fingerprint, str)
                or latest_fingerprint != initial_fingerprint
            ):
                raise ValueError(
                    "cannot finish because the exact pre-existing dirty-worktree fingerprint changed; "
                    "commit the task while preserving unrelated initial changes exactly"
                )
        state["git"]["latest"] = latest
        state["git"]["final_commit"] = args.commit
        state["status"] = "completed"
        state["phase"] = "completed"
        state["completion"] = {
            "completed_at": iso_now(),
            "summary": args.summary,
            "validation": list(args.validation),
            "commit": state["git"]["final_commit"],
        }
        add_event(state, "workflow_completed", args.summary)
        write_state(root, state)
    print_payload(progress_summary(state), args.json)
    return 0


def command_abort(args: argparse.Namespace, root: pathlib.Path) -> int:
    reason = require_nonblank("--reason", args.reason)
    with root_lock(root):
        state = resolve_state(root, args.workflow_id, active_first=True)
        require_mutable_workflow(state)
        state["aborted_from_status"] = state.get("status")
        state["status"] = "aborted"
        for node in state.get("nodes", {}).values():
            if node.get("status") == "running":
                node["status"] = "interrupted"
                node["completed_at"] = iso_now()
                node["error"] = reason
        add_event(state, "workflow_aborted", reason)
        write_state(root, state)
    print_payload(progress_summary(state), args.json)
    return 0


def command_cleanup(args: argparse.Namespace, root: pathlib.Path, already_removed: List[str]) -> int:
    payload = {
        "removed_workflows": already_removed,
        "retention_days": RETENTION_DAYS,
        "temporary_root": str(root),
    }
    print_payload(payload, args.json)
    return 0


def add_common_workflow_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workflow-id")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="initialize or resume a matching task")
    initialize.add_argument("--repo", default=".")
    task_input = initialize.add_mutually_exclusive_group(required=True)
    task_input.add_argument("--task")
    task_input.add_argument("--task-file", help="UTF-8 task text file, or - for stdin")
    initialize.add_argument("--new", action="store_true")
    initialize.add_argument("--json", action="store_true")

    resume = subparsers.add_parser("resume", help="resume an existing workflow")
    add_common_workflow_argument(resume)
    resume.add_argument("--message")

    listing = subparsers.add_parser("list", help="list retained workflows")
    listing.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="report progress without changing execution")
    add_common_workflow_argument(status)
    status.add_argument("--verbose", action="store_true")

    context = subparsers.add_parser("context", help="emit compact context for session recovery")
    add_common_workflow_argument(context)

    phase = subparsers.add_parser("phase", help="change the active phase")
    add_common_workflow_argument(phase)
    phase.add_argument("--phase", required=True)
    phase.add_argument("--message")

    node_add = subparsers.add_parser("node-add", help="add a routed DAG node")
    add_common_workflow_argument(node_add)
    node_add.add_argument("--node-id", required=True)
    node_add.add_argument("--stage", required=True)
    node_add.add_argument("--title", required=True)
    node_add.add_argument("--agent-type", required=True)
    node_add.add_argument("--model", required=True)
    node_add.add_argument("--reasoning-effort", required=True)
    node_add.add_argument("--routing-rationale", required=True)
    node_add.add_argument("--estimated-cost-usd", type=float)
    node_add.add_argument("--dependency", action="append", default=[])
    node_add.add_argument("--write-scope", action="append", default=[])
    node_add.add_argument("--acceptance-criterion", action="append", default=[])
    node_add.add_argument("--output-contract", action="append", default=[])
    node_add.add_argument("--validation-command", action="append", default=[])
    node_add.add_argument("--priority", type=int, default=50)
    node_add.add_argument("--review-round", type=int)
    node_add.add_argument("--finding-id", action="append", default=[])

    node_route = subparsers.add_parser("node-route", help="record a fresh route before a child attempt")
    add_common_workflow_argument(node_route)
    node_route.add_argument("--node-id", required=True)
    node_route.add_argument("--model", required=True)
    node_route.add_argument("--reasoning-effort", required=True)
    node_route.add_argument("--routing-rationale", required=True)
    node_route.add_argument("--estimated-cost-usd", type=float)

    node_update = subparsers.add_parser("node-update", help="record child lifecycle or result")
    add_common_workflow_argument(node_update)
    node_update.add_argument("--node-id", required=True)
    node_update.add_argument("--status", choices=sorted(KNOWN_NODE_STATUSES))
    node_update.add_argument("--agent-id", help="agent_id returned by the active subagent backend")
    node_update.add_argument("--task-name", help="task_name returned by the active backend")
    node_update.add_argument(
        "--agent-name",
        help="other stable agent name or path returned by the active backend",
    )
    node_update.add_argument(
        "--agent-nickname",
        help="optional user-facing nickname returned by the active backend",
    )
    node_update.add_argument("--summary")
    node_update.add_argument("--error")
    node_update.add_argument("--actual-cost-usd", type=float)
    node_update.add_argument("--artifact", action="append", default=None)
    node_update.add_argument("--validation-evidence", action="append", default=None)
    node_update.add_argument("--increment-attempt", action="store_true")
    node_update.add_argument("--message")

    node_patch = subparsers.add_parser("node-patch", help="mutate a not-running future DAG node")
    add_common_workflow_argument(node_patch)
    node_patch.add_argument("--node-id", required=True)
    node_patch.add_argument("--reason", required=True)
    node_patch.add_argument("--title")
    node_patch.add_argument("--stage")
    node_patch.add_argument("--agent-type")
    node_patch.add_argument("--priority", type=int)
    node_patch.add_argument("--write-scope", action="append", default=None)
    node_patch.add_argument("--acceptance-criterion", action="append", default=None)
    node_patch.add_argument("--output-contract", action="append", default=None)
    node_patch.add_argument("--validation-command", action="append", default=None)
    node_patch.add_argument("--requeue", action="store_true")

    dependency_add = subparsers.add_parser("dependency-add", help="add an edge and reject cycles atomically")
    add_common_workflow_argument(dependency_add)
    dependency_add.add_argument("--node-id", required=True)
    dependency_add.add_argument("--dependency", required=True)
    dependency_add.add_argument("--reason", required=True)

    dependency_remove = subparsers.add_parser("dependency-remove", help="remove a future DAG edge")
    add_common_workflow_argument(dependency_remove)
    dependency_remove.add_argument("--node-id", required=True)
    dependency_remove.add_argument("--dependency", required=True)
    dependency_remove.add_argument("--reason", required=True)

    supersede = subparsers.add_parser("node-supersede", help="replace an invalid future node and rewire dependents")
    add_common_workflow_argument(supersede)
    supersede.add_argument("--node-id", required=True)
    supersede.add_argument("--replacement", required=True)
    supersede.add_argument("--reason", required=True)

    remove = subparsers.add_parser("node-remove", help="remove a never-started, dependency-free node")
    add_common_workflow_argument(remove)
    remove.add_argument("--node-id", required=True)
    remove.add_argument("--reason", required=True)

    graph_validate = subparsers.add_parser("graph-validate", help="validate DAG integrity and report write-scope collisions")
    add_common_workflow_argument(graph_validate)

    graph_replan = subparsers.add_parser("graph-replan", help="apply an atomic JSON mutation batch")
    add_common_workflow_argument(graph_replan)
    plan_input = graph_replan.add_mutually_exclusive_group(required=True)
    plan_input.add_argument("--plan-json")
    plan_input.add_argument("--plan-file", help="UTF-8 JSON plan file, or - for stdin")

    event = subparsers.add_parser("event", help="append a workflow event")
    add_common_workflow_argument(event)
    event.add_argument("--kind", required=True)
    event.add_argument("--message", required=True)
    event.add_argument("--details-json")

    requirement = subparsers.add_parser("requirement-set", help="add or update a persisted requirement-ledger entry")
    add_common_workflow_argument(requirement)
    requirement.add_argument("--requirement-id", required=True)
    requirement.add_argument("--text", required=True)
    requirement.add_argument("--source", required=True)
    requirement.add_argument("--status", choices=sorted(KNOWN_REQUIREMENT_STATUSES), default="active")
    requirement.add_argument("--confidence", choices=("high", "medium", "low"), required=True)
    requirement.add_argument("--evidence", action="append", default=[])

    decision = subparsers.add_parser("decision", help="record an autonomous or user decision")
    add_common_workflow_argument(decision)
    decision.add_argument("--confidence", choices=("high", "medium", "low"), required=True)
    decision.add_argument("--question", required=True)
    decision.add_argument("--decision", required=True)
    decision.add_argument("--rationale", required=True)
    decision.add_argument("--alternative", action="append", default=[])

    block = subparsers.add_parser("block", help="record a medium/low-confidence blocker")
    add_common_workflow_argument(block)
    block.add_argument("--confidence", choices=("medium", "low"), required=True)
    block.add_argument("--reason", required=True)
    block.add_argument("--needed", required=True)
    block.add_argument("--recommendation", required=True)
    block.add_argument("--example", action="append", default=[])
    block.add_argument("--impact", action="append", default=[])
    block_scope = block.add_mutually_exclusive_group(required=True)
    block_scope.add_argument("--node-id")
    block_scope.add_argument("--workflow-wide", action="store_true")

    unblock = subparsers.add_parser("unblock", help="resolve a blocker and continue")
    add_common_workflow_argument(unblock)
    unblock.add_argument("--blocker-id", required=True)
    unblock.add_argument("--resolution", required=True)

    reconcile = subparsers.add_parser("reconcile-git", help="refresh repository state")
    add_common_workflow_argument(reconcile)
    reconcile.add_argument("--message")

    finish = subparsers.add_parser("finish", help="mark the workflow complete")
    add_common_workflow_argument(finish)
    finish.add_argument("--summary", required=True)
    finish.add_argument("--commit", required=True)
    finish.add_argument("--validation", action="append", required=True)

    abort = subparsers.add_parser("abort", help="mark running children interrupted and preserve state")
    add_common_workflow_argument(abort)
    abort.add_argument("--reason", required=True)

    cleanup = subparsers.add_parser("cleanup", help="show artifacts removed by this invocation")
    cleanup.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = temp_root()
        with root_lock(root):
            removed = cleanup_stale(root)
        commands = {
            "init": command_init,
            "resume": command_resume,
            "list": command_list,
            "status": command_status,
            "context": command_context,
            "phase": command_phase,
            "node-add": command_node_add,
            "node-route": command_node_route,
            "node-update": command_node_update,
            "node-patch": command_node_patch,
            "dependency-add": command_dependency_add,
            "dependency-remove": command_dependency_remove,
            "node-supersede": command_node_supersede,
            "node-remove": command_node_remove,
            "graph-validate": command_graph_validate,
            "graph-replan": command_graph_replan,
            "event": command_event,
            "requirement-set": command_requirement_set,
            "decision": command_decision,
            "block": command_block,
            "unblock": command_unblock,
            "reconcile-git": command_reconcile_git,
            "finish": command_finish,
            "abort": command_abort,
        }
        if args.command == "cleanup":
            return command_cleanup(args, root, removed)
        return commands[args.command](args, root)
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
