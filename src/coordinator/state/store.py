"""Strict, durable workflow state with controller and mutation fencing."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import stat
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

SCHEMA_VERSION = 3
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_NODES = 128
MAX_EVENTS = 512
MAX_RECEIPTS = 2048
MAX_TEXT = 32_768
ROLES = (
    "architect",
    "designer",
    "documenter",
    "fixer",
    "implementer",
    "researcher",
    "reviewer",
    "validator",
)
NODE_STATUSES = ("pending", "ready", "running", "blocked", "done", "failed", "skipped", "cancelled")
TERMINAL_NODE_STATUSES = frozenset(("done", "failed", "skipped", "cancelled"))
SUCCESS_NODE_STATUSES = frozenset(("done", "skipped", "cancelled"))
WORKFLOW_STATUSES = ("planning", "running", "blocked", "completed", "aborted")
LAUNCH_STATES = ("unclaimed", "claimed", "reconcile_required", "bound", "running", "terminal")
ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class StateError(RuntimeError):
    """A stable workflow-state failure."""

    def __init__(self, message: str, *, code: str = "invalid_state", exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _assert_no_link_components(path: pathlib.Path) -> None:
    absolute = path.absolute()
    parts = absolute.parts
    current = pathlib.Path(parts[0])
    for part in parts[1:]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise StateError(f"path contains an unsafe directory: {current}", code="unsafe_path", exit_code=20)


def _parse_time(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise StateError(f"{field} must be an ISO-8601 string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise StateError(f"{field} must include a timezone")
    return parsed


def _decode_json(data: bytes | str, field: str, *, exit_code: int = 20) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise StateError(f"{field} contains duplicate key {key!r}", code="corrupt_state", exit_code=exit_code)
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs)
    except StateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise StateError(f"{field} is not valid UTF-8 JSON", code="corrupt_state", exit_code=exit_code) from exc


def _lock_evidence(path: pathlib.Path) -> tuple[tuple[int, int, int, int, int, int], bytes, int] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError("workflow lock evidence is ambiguous", code="concurrent_controller", exit_code=20) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            raw = handle.read(4097)
        after = path.lstat()
    except OSError as exc:
        raise StateError("workflow lock evidence is ambiguous", code="concurrent_controller", exit_code=20) from exc
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)
    observed = (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size, after.st_mtime_ns)
    if (
        identity != observed
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or _is_reparse(after)
        or info.st_nlink != 1
        or len(raw) > 4096
        or len(raw) != info.st_size
        or (os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))
    ):
        raise StateError("workflow lock evidence is ambiguous", code="concurrent_controller", exit_code=20)
    try:
        value = _decode_json(raw, "workflow lock")
        if not isinstance(value, dict) or set(value) != {"pid", "nonce", "created_at"}:
            raise StateError("workflow lock fields are invalid")
        if not isinstance(value["pid"], int) or isinstance(value["pid"], bool) or value["pid"] <= 0:
            raise StateError("workflow lock process is invalid")
        if not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]):
            raise StateError("workflow lock nonce is invalid")
        _parse_time(value["created_at"], "workflow lock created_at")
    except StateError as exc:
        raise StateError("workflow lock evidence is ambiguous", code="concurrent_controller", exit_code=20) from exc
    return identity, raw, value["pid"]


def _windows_process_is_proven_dead(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value != 259  # STILL_ACTIVE
    finally:
        close_handle(handle)


def _process_is_proven_dead(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_is_proven_dead(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _remove_proven_stale_lock(path: pathlib.Path) -> bool:
    try:
        first = _lock_evidence(path)
        if first is None:
            return True
        if not _process_is_proven_dead(first[2]):
            return False
        second = _lock_evidence(path)
        if second is None:
            return True
        if second != first:
            return False
        path.unlink()
        return True
    except (OSError, StateError):
        return False


def _keys(value: Any, expected: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise StateError(f"{field} must be an object")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise StateError(f"{field} has " + "; ".join(detail))
    return value


def _text(value: Any, field: str, *, blank: bool = False, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        qualifier = "a string" if blank else "a non-blank string"
        raise StateError(f"{field} must be {qualifier} no longer than {maximum} characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise StateError(f"{field} must contain only Unicode scalar values")
    return value


def _identifier(value: Any, field: str) -> str:
    text = _text(value, field, maximum=128)
    if not ID_RE.fullmatch(text):
        raise StateError(f"{field} is not a safe identifier")
    return text


def canonical_repository(path: pathlib.Path) -> dict[str, str]:
    resolved = pathlib.Path(os.path.realpath(path.expanduser())).resolve()
    if not resolved.is_dir():
        raise StateError(f"repository path is not a directory: {resolved}", code="invalid_repository")
    canonical = os.path.normcase(str(resolved)) if os.name == "nt" else str(resolved)
    return {"path": str(resolved), "identity": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _scope(value: Any, field: str, *, case_sensitive: bool) -> str:
    raw = _text(value, field, maximum=512).replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise StateError(f"{field} must be repository-relative")
    parts = pathlib.PurePosixPath(raw).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise StateError(f"{field} contains an unsafe path segment")
    normalized = "/".join(parts).rstrip("/")
    if normalized.endswith("/**"):
        normalized = normalized[:-3].rstrip("/") or "."
    return normalized if case_sensitive else normalized.casefold()


def scopes_overlap(left: str, right: str, *, case_sensitive: bool | None = None) -> bool:
    sensitive = os.name != "nt" if case_sensitive is None else case_sensitive
    a = _scope(left, "write scope", case_sensitive=sensitive)
    b = _scope(right, "write scope", case_sensitive=sensitive)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/") or a == "." or b == "."


def _depends(nodes: Mapping[str, Mapping[str, Any]], node_id: str, possible_parent: str) -> bool:
    pending = list(nodes[node_id]["dependencies"])
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == possible_parent:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(nodes[current]["dependencies"])
    return False


def graph_diagnostics(nodes: Mapping[str, Mapping[str, Any]], *, case_sensitive: bool) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    for node_id, node in nodes.items():
        for dependency in node["dependencies"]:
            if dependency not in nodes:
                missing.append({"node_id": node_id, "dependency": dependency})

    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: list[list[str]] = []

    def visit(node_id: str, trail: list[str]) -> None:
        if node_id in visiting:
            start = trail.index(node_id)
            cycle = trail[start:] + [node_id]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        if node_id in visited or node_id not in nodes:
            return
        visiting.add(node_id)
        for dependency in nodes[node_id]["dependencies"]:
            visit(dependency, trail + [dependency])
        visiting.remove(node_id)
        visited.add(node_id)

    for candidate in nodes:
        visit(candidate, [candidate])

    collisions: list[dict[str, str]] = []
    active = [
        node_id
        for node_id, node in nodes.items()
        if node["status"] not in TERMINAL_NODE_STATUSES
    ]
    if not missing:
        for index, left_id in enumerate(active):
            for right_id in active[index + 1 :]:
                if _depends(nodes, left_id, right_id) or _depends(nodes, right_id, left_id):
                    continue
                for left in nodes[left_id]["write_scopes"]:
                    for right in nodes[right_id]["write_scopes"]:
                        if scopes_overlap(left, right, case_sensitive=case_sensitive):
                            collisions.append(
                                {"left": left_id, "right": right_id, "left_scope": left, "right_scope": right}
                            )
    return {"missing_dependencies": missing, "cycles": cycles, "write_scope_collisions": collisions}


def ready_nodes(state: Mapping[str, Any]) -> list[str]:
    nodes = state["nodes"]
    if state["status"] == "blocked" or any(
        item["status"] == "active" and item["node_id"] is None for item in state["blockers"]
    ):
        return []
    blocked = {item["node_id"] for item in state["blockers"] if item["status"] == "active" and item["node_id"]}
    ready = []
    for node_id, node in nodes.items():
        if (
            node["status"] not in ("pending", "ready")
            or node["launch"]["state"] != "unclaimed"
            or node_id in blocked
        ):
            continue
        if all(nodes[dependency]["status"] in SUCCESS_NODE_STATUSES for dependency in node["dependencies"]):
            ready.append(node_id)
    return sorted(ready, key=lambda item: (-nodes[item]["priority"], item))


def _recovery_required(state: Mapping[str, Any]) -> bool:
    return any(
        node["launch"]["state"] == "reconcile_required"
        or (state["status"] == "aborted" and node["launch"]["state"] == "bound")
        for node in state["nodes"].values()
    )


def validate_state(state: Any) -> dict[str, Any]:
    """Validate one complete deserialized state document without normalizing it."""
    top = _keys(
        state,
        {
            "schema_version", "workflow_id", "repository", "task", "status", "phase",
            "revision", "created_at", "updated_at", "conventions", "nodes", "requirements",
            "decisions", "blockers", "events", "git", "controller", "receipts",
        },
        "state",
    )
    if top["schema_version"] != SCHEMA_VERSION:
        raise StateError("unsupported state schema", code="unsupported_state")
    _identifier(top["workflow_id"], "workflow_id")
    repository = _keys(top["repository"], {"path", "identity"}, "repository")
    _text(repository["path"], "repository.path", maximum=4096)
    if not isinstance(repository["identity"], str) or not SHA256_RE.fullmatch(repository["identity"]):
        raise StateError("repository.identity must be a SHA-256 digest")
    _text(top["task"], "task")
    if top["status"] not in WORKFLOW_STATUSES:
        raise StateError("workflow status is invalid")
    _identifier(top["phase"], "phase")
    if not isinstance(top["revision"], int) or isinstance(top["revision"], bool) or top["revision"] < 0:
        raise StateError("revision must be a non-negative integer")
    created_at = _parse_time(top["created_at"], "created_at")
    updated_at = _parse_time(top["updated_at"], "updated_at")
    if updated_at < created_at:
        raise StateError("updated_at cannot precede created_at")

    conventions = _keys(
        top["conventions"],
        {"max_parallel", "reserve", "platform", "write_scope_case_sensitive"},
        "conventions",
    )
    if not isinstance(conventions["max_parallel"], int) or isinstance(conventions["max_parallel"], bool) or not 1 <= conventions["max_parallel"] <= 8:
        raise StateError("conventions.max_parallel must be between 1 and 8")
    if not isinstance(conventions["reserve"], int) or isinstance(conventions["reserve"], bool) or not 0 <= conventions["reserve"] < conventions["max_parallel"]:
        raise StateError("conventions.reserve must be below max_parallel")
    _text(conventions["platform"], "conventions.platform", maximum=64)
    if not isinstance(conventions["write_scope_case_sensitive"], bool):
        raise StateError("conventions.write_scope_case_sensitive must be boolean")

    if not isinstance(top["nodes"], dict) or len(top["nodes"]) > MAX_NODES:
        raise StateError(f"nodes must be an object with at most {MAX_NODES} entries")
    request_ids: set[str] = set()
    child_ids: set[str] = set()
    for node_key, raw_node in top["nodes"].items():
        node_id = _identifier(node_key, "node key")
        node = _keys(
            raw_node,
            {
                "id", "title", "stage", "priority", "dependencies", "write_scopes", "role", "model",
                "effort", "acceptance", "route", "launch", "attempts", "status", "result", "evidence",
                "estimated_cost", "actual_cost", "superseded_by",
            },
            f"nodes.{node_id}",
        )
        if node["id"] != node_id:
            raise StateError(f"nodes.{node_id}.id must match its key")
        _text(node["title"], f"nodes.{node_id}.title", maximum=1024)
        _identifier(node["stage"], f"nodes.{node_id}.stage")
        if not isinstance(node["priority"], int) or isinstance(node["priority"], bool) or not 0 <= node["priority"] <= 100:
            raise StateError(f"nodes.{node_id}.priority must be 0..100")
        if not isinstance(node["dependencies"], list):
            raise StateError(f"nodes.{node_id}.dependencies must be a unique list")
        for dependency in node["dependencies"]:
            _identifier(dependency, f"nodes.{node_id}.dependency")
            if dependency == node_id:
                raise StateError(f"nodes.{node_id} cannot depend on itself")
        if len(set(node["dependencies"])) != len(node["dependencies"]):
            raise StateError(f"nodes.{node_id}.dependencies must be a unique list")
        if not isinstance(node["write_scopes"], list) or not node["write_scopes"] or len(node["write_scopes"]) > 32:
            raise StateError(f"nodes.{node_id}.write_scopes must contain 1..32 paths")
        for scope in node["write_scopes"]:
            _scope(scope, f"nodes.{node_id}.write_scope", case_sensitive=conventions["write_scope_case_sensitive"])
        if node["role"] not in ROLES:
            raise StateError(f"nodes.{node_id} has an invalid role")
        for field in ("model", "effort"):
            if node[field] is not None:
                _text(node[field], f"nodes.{node_id}.{field}", maximum=256)
        if not isinstance(node["acceptance"], list) or not node["acceptance"]:
            raise StateError(f"nodes.{node_id}.acceptance must be non-empty")
        for item in node["acceptance"]:
            _text(item, f"nodes.{node_id}.acceptance", maximum=2048)
        route = _keys(node["route"], {"rationale", "routed_at", "attempt"}, f"nodes.{node_id}.route")
        _text(route["rationale"], f"nodes.{node_id}.route.rationale", maximum=4096)
        _parse_time(route["routed_at"], f"nodes.{node_id}.route.routed_at")
        if not isinstance(route["attempt"], int) or isinstance(route["attempt"], bool) or route["attempt"] < 1:
            raise StateError(f"nodes.{node_id}.route.attempt must be positive")
        launch = _keys(
            node["launch"],
            {"state", "request_id", "child_id", "claimed_at", "reconciliation"},
            f"nodes.{node_id}.launch",
        )
        if launch["state"] not in LAUNCH_STATES:
            raise StateError(f"nodes.{node_id}.launch.state is invalid")
        for key in ("request_id", "child_id"):
            if launch[key] is not None:
                _identifier(launch[key], f"nodes.{node_id}.launch.{key}")
        if launch["request_id"] is not None:
            if launch["request_id"] in request_ids:
                raise StateError("launch request identifiers must be unique")
            request_ids.add(launch["request_id"])
        if launch["child_id"] is not None:
            if launch["child_id"] in child_ids:
                raise StateError("launch child identifiers must be unique")
            child_ids.add(launch["child_id"])
        if launch["claimed_at"] is not None:
            _parse_time(launch["claimed_at"], f"nodes.{node_id}.launch.claimed_at")
        if launch["reconciliation"] is not None:
            _text(launch["reconciliation"], f"nodes.{node_id}.launch.reconciliation", maximum=4096)
        if launch["state"] == "unclaimed" and any(launch[key] is not None for key in ("request_id", "child_id", "claimed_at")):
            raise StateError(f"nodes.{node_id} has data on an unclaimed launch")
        if launch["state"] in ("claimed", "reconcile_required") and not launch["request_id"]:
            raise StateError(f"nodes.{node_id} claimed launch requires request_id")
        if launch["state"] in ("bound", "running", "terminal") and not launch["child_id"]:
            raise StateError(f"nodes.{node_id} bound launch requires child_id")
        if not isinstance(node["attempts"], list) or len(node["attempts"]) > 32:
            raise StateError(f"nodes.{node_id}.attempts must be a bounded list")
        for index, raw_attempt in enumerate(node["attempts"]):
            attempt = _keys(raw_attempt, {"number", "started_at", "finished_at", "outcome"}, f"nodes.{node_id}.attempts[{index}]")
            if not isinstance(attempt["number"], int) or isinstance(attempt["number"], bool) or attempt["number"] != index + 1:
                raise StateError(f"nodes.{node_id} attempts must be consecutively numbered")
            _parse_time(attempt["started_at"], f"nodes.{node_id}.attempt.started_at")
            if attempt["finished_at"] is not None:
                _parse_time(attempt["finished_at"], f"nodes.{node_id}.attempt.finished_at")
            if attempt["outcome"] is not None:
                _text(attempt["outcome"], f"nodes.{node_id}.attempt.outcome", maximum=4096)
            if (attempt["finished_at"] is None) != (attempt["outcome"] is None):
                raise StateError(f"nodes.{node_id}.attempts[{index}] completion fields must both be null or both be set")
        unfinished = [attempt for attempt in node["attempts"] if attempt["finished_at"] is None]
        if len(unfinished) > 1 or (unfinished and unfinished[0] is not node["attempts"][-1]):
            raise StateError(f"nodes.{node_id} has inconsistent attempt completion")
        if launch["state"] == "unclaimed" and unfinished:
            raise StateError(f"nodes.{node_id} unclaimed launch cannot have an unfinished attempt")
        if launch["state"] in ("claimed", "reconcile_required", "bound", "running") and not unfinished:
            raise StateError(f"nodes.{node_id} active launch requires one unfinished attempt")
        if launch["state"] == "terminal" and (not node["attempts"] or unfinished):
            raise StateError(f"nodes.{node_id} terminal launch requires a completed attempt")
        if launch["state"] != "unclaimed" and node["attempts"] and route["attempt"] != node["attempts"][-1]["number"]:
            raise StateError(f"nodes.{node_id} route attempt does not match the launch attempt")
        if node["status"] not in NODE_STATUSES:
            raise StateError(f"nodes.{node_id}.status is invalid")
        for key in ("result", "evidence", "superseded_by"):
            if node[key] is not None:
                _text(node[key], f"nodes.{node_id}.{key}", maximum=MAX_TEXT if key != "superseded_by" else 128)
        for key in ("estimated_cost", "actual_cost"):
            if node[key] is not None and (
                not isinstance(node[key], (int, float))
                or isinstance(node[key], bool)
                or not math.isfinite(node[key])
                or node[key] < 0
            ):
                raise StateError(f"nodes.{node_id}.{key} must be non-negative or null")
        if node["status"] == "done" and (not node["result"] or not node["evidence"]):
            raise StateError(f"nodes.{node_id} done status requires result and evidence")
        active_launch = launch["state"] in ("claimed", "reconcile_required", "bound", "running")
        aborted_recovery = (
            top["status"] == "aborted"
            and node["status"] == "cancelled"
            and launch["state"] in ("reconcile_required", "bound")
        )
        if node["status"] in TERMINAL_NODE_STATUSES and active_launch and not aborted_recovery:
            raise StateError(f"nodes.{node_id} terminal status cannot retain an active launch")
        if launch["state"] == "terminal" and node["status"] not in TERMINAL_NODE_STATUSES:
            raise StateError(f"nodes.{node_id} terminal launch requires terminal node status")
        if node["status"] == "running" and launch["state"] != "running":
            raise StateError(f"nodes.{node_id} running status requires a running launch")

    for node_id, node in top["nodes"].items():
        if node["superseded_by"] is not None:
            _identifier(node["superseded_by"], f"nodes.{node_id}.superseded_by")
            if node["superseded_by"] not in top["nodes"] or node["superseded_by"] == node_id:
                raise StateError(f"nodes.{node_id}.superseded_by must name another existing node")
            if any(node_id in other["dependencies"] for other in top["nodes"].values()):
                raise StateError(f"nodes.{node_id} is superseded but still has dependents")

    diagnostic = graph_diagnostics(top["nodes"], case_sensitive=conventions["write_scope_case_sensitive"])
    if any(diagnostic.values()):
        raise StateError("invalid workflow graph: " + json.dumps(diagnostic, sort_keys=True))
    occupied = sum(
        node["launch"]["state"] in ("claimed", "reconcile_required", "bound", "running")
        for node in top["nodes"].values()
    )
    if occupied > conventions["max_parallel"] - conventions["reserve"]:
        raise StateError("active launch claims exceed usable controller capacity")
    for node_id, node in top["nodes"].items():
        if node["status"] == "ready" and any(top["nodes"][dependency]["status"] not in SUCCESS_NODE_STATUSES for dependency in node["dependencies"]):
            raise StateError(f"nodes.{node_id} cannot be ready before its dependencies")

    if not isinstance(top["requirements"], dict) or len(top["requirements"]) > 256:
        raise StateError("requirements must be a bounded object")
    for requirement_id, raw_requirement in top["requirements"].items():
        _identifier(requirement_id, "requirement id")
        requirement = _keys(raw_requirement, {"text", "source", "status", "evidence"}, f"requirements.{requirement_id}")
        _text(requirement["text"], f"requirements.{requirement_id}.text")
        _text(requirement["source"], f"requirements.{requirement_id}.source", maximum=256)
        if requirement["status"] not in ("active", "satisfied", "superseded"):
            raise StateError(f"requirements.{requirement_id}.status is invalid")
        if requirement["evidence"] is not None:
            _text(requirement["evidence"], f"requirements.{requirement_id}.evidence")
        if requirement["status"] != "active" and not requirement["evidence"]:
            raise StateError(f"requirements.{requirement_id} resolution requires evidence")

    for field, limit in (("decisions", 256), ("blockers", 256), ("events", MAX_EVENTS)):
        if not isinstance(top[field], list) or len(top[field]) > limit:
            raise StateError(f"{field} must be a list with at most {limit} entries")
    for index, raw_decision in enumerate(top["decisions"]):
        decision = _keys(raw_decision, {"id", "text", "rationale", "at"}, f"decisions[{index}]")
        _identifier(decision["id"], "decision id")
        _text(decision["text"], "decision text")
        _text(decision["rationale"], "decision rationale")
        _parse_time(decision["at"], "decision at")
    for index, raw_blocker in enumerate(top["blockers"]):
        blocker = _keys(raw_blocker, {"id", "node_id", "reason", "needed", "status", "resolution", "at"}, f"blockers[{index}]")
        _identifier(blocker["id"], "blocker id")
        if blocker["node_id"] is not None:
            _identifier(blocker["node_id"], "blocker node_id")
            if blocker["node_id"] not in top["nodes"]:
                raise StateError("blocker references an unknown node")
        _text(blocker["reason"], "blocker reason")
        _text(blocker["needed"], "blocker needed")
        if blocker["status"] not in ("active", "resolved"):
            raise StateError("blocker status is invalid")
        if blocker["resolution"] is not None:
            _text(blocker["resolution"], "blocker resolution")
        if blocker["status"] == "resolved" and not blocker["resolution"]:
            raise StateError("resolved blocker requires resolution")
        _parse_time(blocker["at"], "blocker at")
    for index, raw_event in enumerate(top["events"]):
        event = _keys(raw_event, {"id", "kind", "message", "node_id", "at", "revision"}, f"events[{index}]")
        _identifier(event["id"], "event id")
        _identifier(event["kind"], "event kind")
        _text(event["message"], "event message")
        if event["node_id"] is not None:
            _identifier(event["node_id"], "event node_id")
        _parse_time(event["at"], "event at")
        if not isinstance(event["revision"], int) or isinstance(event["revision"], bool) or event["revision"] < 0:
            raise StateError("event revision must be non-negative")
        if event["revision"] > top["revision"] or (index and event["revision"] < top["events"][index - 1]["revision"]):
            raise StateError("event revisions must be committed and durably ordered")
    for field in ("decisions", "blockers", "events"):
        if len({item["id"] for item in top[field]}) != len(top[field]):
            raise StateError(f"{field} identifiers must be unique")

    git = _keys(top["git"], {"head", "branch", "dirty", "checkpoint"}, "git")
    for key in ("head", "branch", "checkpoint"):
        if git[key] is not None:
            _text(git[key], f"git.{key}", maximum=1024)
    if not isinstance(git["dirty"], bool):
        raise StateError("git.dirty must be boolean")
    controller = _keys(
        top["controller"],
        {"epoch", "session_id", "checkpoint", "resume_required", "recovery_status"},
        "controller",
    )
    if not isinstance(controller["epoch"], int) or isinstance(controller["epoch"], bool) or controller["epoch"] < 1:
        raise StateError("controller.epoch must be positive")
    _identifier(controller["session_id"], "controller.session_id")
    if not isinstance(controller["checkpoint"], int) or isinstance(controller["checkpoint"], bool) or controller["checkpoint"] < 0:
        raise StateError("controller.checkpoint must be non-negative")
    if controller["checkpoint"] > top["revision"]:
        raise StateError("controller checkpoint cannot exceed the state revision")
    if not isinstance(controller["resume_required"], bool):
        raise StateError("controller.resume_required must be boolean")
    if controller["recovery_status"] not in ("clean", "takeover_pending", "reconcile_required"):
        raise StateError("controller.recovery_status is invalid")

    if not isinstance(top["receipts"], dict) or len(top["receipts"]) > MAX_RECEIPTS:
        raise StateError("receipts must be a bounded object")
    for mutation_id, raw_receipt in top["receipts"].items():
        _identifier(mutation_id, "mutation id")
        receipt = _keys(raw_receipt, {"digest", "revision", "at"}, f"receipts.{mutation_id}")
        if not isinstance(receipt["digest"], str) or not SHA256_RE.fullmatch(receipt["digest"]):
            raise StateError("receipt digest must be SHA-256")
        if not isinstance(receipt["revision"], int) or isinstance(receipt["revision"], bool) or receipt["revision"] < 1:
            raise StateError("receipt revision must be positive")
        if receipt["revision"] > top["revision"]:
            raise StateError("receipt revision cannot exceed the state revision")
        _parse_time(receipt["at"], "receipt at")
    reconcile_required = _recovery_required(top)
    if reconcile_required != (controller["recovery_status"] == "reconcile_required"):
        raise StateError("controller recovery status disagrees with launch reconciliation state")
    if controller["recovery_status"] == "takeover_pending" and not controller["resume_required"]:
        raise StateError("takeover_pending requires explicit resume")
    if top["status"] == "completed":
        if top["phase"] != "completed" or not top["nodes"] or any(node["status"] not in SUCCESS_NODE_STATUSES for node in top["nodes"].values()):
            raise StateError("completed workflow requires a terminal-successful graph")
        if any(item["status"] == "active" for item in top["requirements"].values()) or any(item["status"] == "active" for item in top["blockers"]):
            raise StateError("completed workflow cannot retain active requirements or blockers")
        if not isinstance(top["git"]["checkpoint"], str) or not COMMIT_RE.fullmatch(top["git"]["checkpoint"]):
            raise StateError("completed workflow requires a full commit checkpoint")
    if top["status"] == "aborted" and top["phase"] != "aborted":
        raise StateError("aborted workflow phase is inconsistent")
    if top["phase"] == "completed" and top["status"] != "completed":
        raise StateError("completed workflow phase requires completed status")
    if top["phase"] == "aborted" and top["status"] != "aborted":
        raise StateError("aborted workflow phase requires aborted status")
    return state


def new_state(repository: Mapping[str, str], task: str, session_id: str, conventions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    created = now_iso()
    suffix = hashlib.sha256((repository["identity"] + "\0" + task + "\0" + created + secrets.token_hex(8)).encode()).hexdigest()[:20]
    profile = {
        "max_parallel": 4,
        "reserve": 1,
        "platform": os.name,
        "write_scope_case_sensitive": os.name != "nt",
    }
    if conventions:
        profile.update(conventions)
    state = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": f"wf-{suffix}",
        "repository": dict(repository),
        "task": _text(task, "task"),
        "status": "planning",
        "phase": "planning",
        "revision": 0,
        "created_at": created,
        "updated_at": created,
        "conventions": profile,
        "nodes": {},
        "requirements": {},
        "decisions": [],
        "blockers": [],
        "events": [],
        "git": {"head": None, "branch": None, "dirty": False, "checkpoint": None},
        "controller": {
            "epoch": 1,
            "session_id": _identifier(session_id, "session_id"),
            "checkpoint": 0,
            "resume_required": False,
            "recovery_status": "clean",
        },
        "receipts": {},
    }
    return validate_state(state)


def _terminal_reconciliation(state: Mapping[str, Any], operation: Mapping[str, Any]) -> bool:
    if state["status"] != "aborted":
        return False
    if set(operation) == {"command", "message"} and operation["command"] == "resume":
        return (
            state["controller"]["resume_required"]
            and _recovery_required(state)
            and isinstance(operation["message"], str)
            and bool(operation["message"].strip())
        )
    expected = {
        "command", "node_id", "status", "launch_state", "request_id", "child_id",
        "reconciliation", "result", "evidence", "actual_cost", "attempt_outcome",
    }
    if set(operation) != expected or operation["command"] != "node-update":
        return False
    node = state["nodes"].get(operation["node_id"])
    target = operation["launch_state"]
    if (
        not node
        or not isinstance(operation["reconciliation"], str)
        or not operation["reconciliation"].strip()
        or any(operation[key] is not None for key in ("status", "request_id", "result", "evidence", "actual_cost"))
    ):
        return False
    child_id = operation["child_id"]
    if node["launch"]["state"] == "reconcile_required" and target in ("unclaimed", "bound"):
        if operation["attempt_outcome"] is not None:
            return False
        if target == "unclaimed":
            return node["launch"]["child_id"] is None and child_id is None
        known_child = node["launch"]["child_id"]
        return isinstance(child_id, str) and bool(child_id) and (known_child is None or child_id == known_child)
    return (
        node["launch"]["state"] == "bound"
        and target == "terminal"
        and child_id is None
        and isinstance(operation["attempt_outcome"], str)
        and bool(operation["attempt_outcome"].strip())
    )


class StateStore:
    """The sole persistence and concurrency authority for workflow state."""

    def __init__(self, root: pathlib.Path | None = None):
        self.root = (root or (pathlib.Path.home() / ".agent-coordinator")).expanduser().absolute()

    @property
    def workflows(self) -> pathlib.Path:
        return self.root / "workflows"

    @property
    def sessions(self) -> pathlib.Path:
        return self.root / "sessions"

    @property
    def locks(self) -> pathlib.Path:
        return self.root / "locks"

    def _ensure_private_directory(self, path: pathlib.Path) -> None:
        path = path.absolute()
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise StateError("Coordinator path escapes the control root", code="unsafe_path", exit_code=20) from exc
        parent = self.root.parent
        _assert_no_link_components(parent)
        if parent.is_symlink() or not parent.is_dir() or _is_reparse(parent.lstat()):
            raise StateError("control-root parent is unsafe", code="unsafe_path", exit_code=20)
        chain = [self.root]
        current = self.root
        for part in relative.parts:
            current = current / part
            chain.append(current)
        for item in chain:
            if not item.exists():
                if item.is_symlink():
                    raise StateError(f"refusing symlink directory: {item}", code="unsafe_path", exit_code=20)
                item.mkdir(mode=0o700)
            info = item.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise StateError(f"unsafe Coordinator directory: {item}", code="unsafe_path", exit_code=20)
            if os.name != "nt" and info.st_uid != os.getuid():
                raise StateError(f"Coordinator directory has another owner: {item}", code="unsafe_owner", exit_code=20)
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise StateError(f"Coordinator directory is not private: {item}", code="unsafe_permissions", exit_code=20)

    def _state_path(self, workflow_id: str) -> pathlib.Path:
        return self.workflows / (_identifier(workflow_id, "workflow_id") + ".json")

    def _read_json(self, path: pathlib.Path, *, maximum: int = MAX_STATE_BYTES) -> Any:
        try:
            _assert_no_link_components(path.parent)
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or _is_reparse(before)
                or before.st_nlink != 1
                or (os.name != "nt" and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077))
            ):
                raise StateError("private state/session file is unsafe", code="unsafe_path", exit_code=20)
            if before.st_size > maximum:
                raise StateError(f"state exceeds {maximum} bytes", code="state_too_large", exit_code=20)
            raw = path.read_bytes()
            after = path.lstat()
        except FileNotFoundError as exc:
            raise StateError(f"state not found: {path.stem}", code="not_found", exit_code=1) from exc
        except OSError as exc:
            raise StateError("unable to read workflow state", code="io_error", exit_code=20) from exc
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != after.st_size:
            raise StateError("workflow state changed during read", code="changed_during_read", exit_code=20)
        return _decode_json(raw, "workflow state")

    def load(self, workflow_id: str) -> dict[str, Any]:
        expected = _identifier(workflow_id, "workflow_id")
        state = validate_state(self._read_json(self._state_path(expected)))
        if state["workflow_id"] != expected:
            raise StateError("state filename and workflow identifier differ", code="corrupt_state", exit_code=20)
        return state

    def iter_records(self) -> Iterator[tuple[pathlib.Path, dict[str, Any] | None, StateError | None]]:
        try:
            workflows_info = self.workflows.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(workflows_info.st_mode) or stat.S_ISLNK(workflows_info.st_mode) or _is_reparse(workflows_info):
            yield self.workflows, None, StateError("unsafe workflows directory", code="unsafe_path", exit_code=20)
            return
        for path in sorted(self.workflows.glob("*.json")):
            try:
                state = validate_state(self._read_json(path))
                if state["workflow_id"] != path.stem:
                    raise StateError("state filename and workflow identifier differ", code="corrupt_state", exit_code=20)
                yield path, state, None
            except StateError as exc:
                yield path, None, exc

    def list_valid(self) -> list[dict[str, Any]]:
        states = [state for _, state, error in self.iter_records() if state is not None and error is None]
        return sorted(states, key=lambda value: value["updated_at"], reverse=True)

    def _atomic_json(self, path: pathlib.Path, value: Any) -> None:
        try:
            data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StateError("state is not canonical JSON data") from exc
        if len(data) > MAX_STATE_BYTES:
            raise StateError(f"state exceeds {MAX_STATE_BYTES} bytes", code="state_too_large", exit_code=20)
        self._ensure_private_directory(path.parent)
        temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateError("durable state commit failed; reconcile the mutation id", code="commit_uncertain", exit_code=20) from exc

    def _prepare_session_target(self, target: pathlib.Path, repository_path: str) -> pathlib.Path:
        target = target.expanduser().absolute()
        try:
            target.relative_to(pathlib.Path(repository_path).absolute())
        except ValueError:
            pass
        else:
            raise StateError("session bearer file must be outside the repository", code="unsafe_session_file", exit_code=20)
        if target.exists() or target.is_symlink():
            raise StateError("session file already exists", code="unsafe_session_file", exit_code=20)
        missing: list[pathlib.Path] = []
        current = target.parent
        while not current.exists():
            if current.is_symlink():
                raise StateError("session path contains a symlink", code="unsafe_session_file", exit_code=20)
            missing.append(current)
            current = current.parent
        if current.is_symlink() or not current.is_dir() or _is_reparse(current.lstat()):
            raise StateError("session path parent is unsafe", code="unsafe_session_file", exit_code=20)
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        _assert_no_link_components(target.parent)
        parent = target.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or _is_reparse(parent)
            or (os.name != "nt" and (parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077))
        ):
            raise StateError("session file parent must be current-user private", code="unsafe_session_file", exit_code=20)
        return target

    def _write_session_file(self, target: pathlib.Path, value: Mapping[str, Any]) -> None:
        data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise StateError("unable to create private session file", code="io_error", exit_code=20) from exc

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        self._ensure_private_directory(self.locks)
        path = self.locks / (_identifier(name, "lock name") + ".lock")
        nonce = secrets.token_hex(16)
        payload = json.dumps({"pid": os.getpid(), "nonce": nonce, "created_at": now_iso()}).encode()
        for attempt in range(2):
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError as exc:
                if attempt or not _remove_proven_stale_lock(path):
                    raise StateError("workflow is locked by another controller", code="concurrent_controller", exit_code=20) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            try:
                current = _lock_evidence(path)
                if current is not None and current[1] == payload:
                    path.unlink()
            except (OSError, StateError):
                pass

    def open_session(self, repository: Mapping[str, str], session_file: pathlib.Path) -> dict[str, Any]:
        session_id = "session-" + secrets.token_hex(12)
        bearer = secrets.token_urlsafe(32)
        record = {
            "schema_version": 1,
            "session_id": session_id,
            "repository_identity": repository["identity"],
            "bearer_sha256": hashlib.sha256(bearer.encode()).hexdigest(),
            "opened_at": now_iso(),
        }
        target = self._prepare_session_target(session_file, repository["path"])
        record_path = self.sessions / f"{session_id}.json"
        self._atomic_json(record_path, record)
        try:
            self._write_session_file(target, {"session_id": session_id, "bearer": bearer, "repository_identity": repository["identity"]})
        except StateError:
            record_path.unlink(missing_ok=True)
            raise
        return {"session_id": session_id, "repository": dict(repository)}

    def _session(self, session_file: pathlib.Path, repository_identity: str) -> dict[str, Any]:
        session = self._read_json(session_file.expanduser().absolute(), maximum=4096)
        session = _keys(session, {"session_id", "bearer", "repository_identity"}, "session file")
        session_id = _identifier(session["session_id"], "session_id")
        _text(session["bearer"], "session bearer", maximum=256)
        if session["repository_identity"] != repository_identity:
            raise StateError("session belongs to another repository", code="session_mismatch", exit_code=20)
        record = self._read_json(self.sessions / f"{session_id}.json", maximum=4096)
        record = _keys(record, {"schema_version", "session_id", "repository_identity", "bearer_sha256", "opened_at"}, "session record")
        if (
            record["schema_version"] != 1
            or record["session_id"] != session_id
            or record["repository_identity"] != repository_identity
            or not secrets.compare_digest(record["bearer_sha256"], hashlib.sha256(session["bearer"].encode()).hexdigest())
        ):
            raise StateError("session credential is invalid", code="invalid_session", exit_code=20)
        return dict(session)

    def close_session(self, session_file: pathlib.Path) -> dict[str, Any]:
        target = session_file.expanduser().absolute()
        raw = self._read_json(target, maximum=4096)
        if not isinstance(raw, dict) or not isinstance(raw.get("repository_identity"), str):
            raise StateError("session file is invalid", code="invalid_session", exit_code=20)
        session = self._session(target, raw["repository_identity"])
        session_id = session["session_id"]
        record = self.sessions / f"{session_id}.json"
        with self._lock("sessions"):
            record.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
        return {"session_id": session_id, "closed": True}

    def create(
        self,
        repository: Mapping[str, str],
        task: str,
        session_file: pathlib.Path,
        conventions: Mapping[str, Any] | None = None,
        mutation_id: str = "init",
    ) -> dict[str, Any]:
        mutation = _identifier(mutation_id, "mutation_id")
        digest = self._payload_digest({"command": "init", "repository": repository["identity"], "task": task, "conventions": conventions})
        session = self._session(session_file, repository["identity"])
        with self._lock("workflow-create"):
            for existing in self.list_valid():
                receipt = existing["receipts"].get(mutation)
                if receipt:
                    if receipt["digest"] != digest:
                        raise StateError("mutation id was reused for different initialization", code="mutation_conflict", exit_code=20)
                    return existing
            state = new_state(repository, task, session["session_id"], conventions)
            add_event(state, "workflow_created", "workflow initialized")
            state["revision"] = 1
            state["controller"]["checkpoint"] = 1
            state["receipts"][mutation] = {"digest": digest, "revision": 1, "at": state["updated_at"]}
            validate_state(state)
            self._atomic_json(self._state_path(state["workflow_id"]), state)
        return state

    def _payload_digest(self, operation: Mapping[str, Any]) -> str:
        try:
            encoded = json.dumps(operation, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise StateError("mutation payload is not canonical JSON data") from exc
        return hashlib.sha256(encoded).hexdigest()

    def mutate(
        self,
        workflow_id: str,
        *,
        session_file: pathlib.Path,
        mutation_id: str,
        expected_revision: int,
        operation: Mapping[str, Any],
        change: Callable[[dict[str, Any]], Any],
        allow_resume_required: bool = False,
    ) -> tuple[dict[str, Any], Any, bool]:
        mutation = _identifier(mutation_id, "mutation_id")
        digest = self._payload_digest(operation)
        with self._lock(workflow_id):
            state = self.load(workflow_id)
            session = self._session(session_file, state["repository"]["identity"])
            if state["controller"]["session_id"] != session["session_id"]:
                raise StateError("controller epoch is owned by another session", code="controller_fenced", exit_code=20)
            receipt = state["receipts"].get(mutation)
            if receipt:
                if receipt["digest"] != digest:
                    raise StateError("mutation id was reused for different content", code="mutation_conflict", exit_code=20)
                return state, {"applied_revision": receipt["revision"]}, True
            if expected_revision != state["revision"]:
                raise StateError(
                    f"expected revision {expected_revision}, observed {state['revision']}",
                    code="revision_conflict",
                    exit_code=20,
                )
            if isinstance(expected_revision, bool):
                raise StateError("expected revision must be an integer", code="revision_conflict", exit_code=20)
            if state["controller"]["resume_required"] and not allow_resume_required:
                raise StateError("controller takeover requires explicit resume", code="resume_required", exit_code=20)
            if state["status"] in ("completed", "aborted") and not _terminal_reconciliation(state, operation):
                raise StateError("terminal workflow cannot be mutated")
            candidate = copy.deepcopy(state)
            result = change(candidate)
            candidate["revision"] += 1
            candidate["updated_at"] = now_iso()
            candidate["controller"]["checkpoint"] = candidate["revision"]
            candidate["receipts"][mutation] = {"digest": digest, "revision": candidate["revision"], "at": candidate["updated_at"]}
            if len(candidate["receipts"]) > MAX_RECEIPTS:
                raise StateError("workflow mutation receipt capacity is exhausted", code="capacity_exceeded", exit_code=20)
            if len(candidate["events"]) > MAX_EVENTS:
                candidate["events"] = candidate["events"][-MAX_EVENTS:]
            validate_state(candidate)
            self._atomic_json(self._state_path(workflow_id), candidate)
            return candidate, result, False

    def takeover(
        self,
        workflow_id: str,
        *,
        session_file: pathlib.Path,
        mutation_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._lock(workflow_id):
            state = self.load(workflow_id)
            session = self._session(session_file, state["repository"]["identity"])
            digest = self._payload_digest({"command": "controller-takeover", "session_id": session["session_id"]})
            receipt = state["receipts"].get(_identifier(mutation_id, "mutation_id"))
            if receipt:
                if receipt["digest"] != digest:
                    raise StateError("mutation id was reused for another takeover", code="mutation_conflict", exit_code=20)
                return state
            if state["revision"] != expected_revision:
                raise StateError("controller takeover revision conflict", code="revision_conflict", exit_code=20)
            if isinstance(expected_revision, bool):
                raise StateError("expected revision must be an integer", code="revision_conflict", exit_code=20)
            if state["status"] == "completed" or (state["status"] == "aborted" and not _recovery_required(state)):
                raise StateError("terminal workflow cannot change controllers")
            candidate = copy.deepcopy(state)
            candidate["controller"].update(
                {
                    "epoch": candidate["controller"]["epoch"] + 1,
                    "session_id": session["session_id"],
                    "resume_required": True,
                    "recovery_status": (
                        "reconcile_required"
                        if _recovery_required(candidate)
                        else "takeover_pending"
                    ),
                }
            )
            candidate["revision"] += 1
            candidate["updated_at"] = now_iso()
            candidate["controller"]["checkpoint"] = candidate["revision"]
            candidate["receipts"][mutation_id] = {"digest": digest, "revision": candidate["revision"], "at": candidate["updated_at"]}
            if len(candidate["receipts"]) > MAX_RECEIPTS:
                raise StateError("workflow mutation receipt capacity is exhausted", code="capacity_exceeded", exit_code=20)
            validate_state(candidate)
            self._atomic_json(self._state_path(workflow_id), candidate)
            return candidate

    def reconcile_commit(self, workflow_id: str, mutation_id: str, digest: str | None = None) -> dict[str, Any]:
        state = self.load(workflow_id)
        receipt = state["receipts"].get(_identifier(mutation_id, "mutation_id"))
        if receipt is None:
            return {"outcome": "not_applied", "revision": state["revision"]}
        if digest is not None and not secrets.compare_digest(receipt["digest"], digest.lower()):
            raise StateError("observed receipt digest differs", code="mutation_conflict", exit_code=20)
        return {"outcome": "applied", "revision": receipt["revision"], "digest": receipt["digest"]}


def add_event(state: dict[str, Any], kind: str, message: str, node_id: str | None = None) -> dict[str, Any]:
    event = {
        "id": "event-" + secrets.token_hex(8),
        "kind": _identifier(kind, "event kind"),
        "message": _text(message, "event message"),
        "node_id": _identifier(node_id, "event node_id") if node_id else None,
        "at": now_iso(),
        "revision": state["revision"] + 1,
    }
    state["events"].append(event)
    return event


def _read_command_text(value: str | None, path: str | None, name: str) -> str:
    if (value is None) == (path is None):
        raise StateError(f"exactly one of --{name} or --{name}-file is required")
    if value is not None:
        result = value
    elif path == "-":
        result = sys.stdin.read()
    else:
        try:
            result = pathlib.Path(path or "").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StateError(f"unable to read {name} file", code="io_error", exit_code=20) from exc
    if not result.strip():
        raise StateError(f"{name} must not be blank")
    return result


def _read_command_object(value: str | None, path: str | None, name: str) -> dict[str, Any]:
    text = _read_command_text(value, path, name)
    try:
        parsed = _decode_json(text, name, exit_code=2)
    except StateError as exc:
        raise StateError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StateError(f"{name} must be a JSON object")
    return parsed


def _mutate_command(
    store: StateStore,
    args: Any,
    command: str,
    operation: Mapping[str, Any],
    change: Callable[[dict[str, Any]], Any],
    *,
    allow_resume_required: bool = False,
) -> tuple[dict[str, Any], Any, bool]:
    return store.mutate(
        args.workflow_id,
        session_file=pathlib.Path(args.session_file),
        mutation_id=args.mutation_id,
        expected_revision=args.expected_revision,
        operation={"command": command, **operation},
        change=change,
        allow_resume_required=allow_resume_required,
    )


def _public_state(state: Mapping[str, Any], *, full: bool = False) -> dict[str, Any]:
    result = {
        "workflow_id": state["workflow_id"],
        "repository": state["repository"],
        "task": state["task"],
        "status": state["status"],
        "phase": state["phase"],
        "revision": state["revision"],
        "updated_at": state["updated_at"],
        "controller_epoch": state["controller"]["epoch"],
        "resume_required": state["controller"]["resume_required"],
        "ready_nodes": ready_nodes(state),
    }
    if full:
        result["state"] = state
    return result


def _new_node(args: Any) -> dict[str, Any]:
    return {
        "id": args.node_id,
        "title": args.title,
        "stage": args.stage,
        "priority": args.priority,
        "dependencies": list(dict.fromkeys(args.dependency)),
        "write_scopes": list(dict.fromkeys(args.write_scope)),
        "role": args.role,
        "model": args.model,
        "effort": args.effort,
        "acceptance": list(dict.fromkeys(args.acceptance)),
        "route": {"rationale": args.rationale, "routed_at": now_iso(), "attempt": 1},
        "launch": {
            "state": "unclaimed",
            "request_id": None,
            "child_id": None,
            "claimed_at": None,
            "reconciliation": None,
        },
        "attempts": [],
        "status": "pending",
        "result": None,
        "evidence": None,
        "estimated_cost": args.estimated_cost,
        "actual_cost": None,
        "superseded_by": None,
    }


def _refresh_recovery_status(state: dict[str, Any]) -> None:
    if _recovery_required(state):
        state["controller"]["recovery_status"] = "reconcile_required"
    elif state["controller"]["resume_required"]:
        state["controller"]["recovery_status"] = "takeover_pending"
    else:
        state["controller"]["recovery_status"] = "clean"


def execute_command(args: Any, store: StateStore) -> tuple[int, str, Any, list[str]]:
    """Execute one parsed state command at the state owner boundary."""
    command = args.command
    if command == "session-open":
        repository = canonical_repository(pathlib.Path(args.repo))
        return 0, "session_opened", store.open_session(repository, pathlib.Path(args.session_file)), []
    if command == "session-close":
        return 0, "session_closed", store.close_session(pathlib.Path(args.session_file)), []
    if command == "init":
        repository = canonical_repository(pathlib.Path(args.repo))
        task = _read_command_text(args.task, args.task_file, "task")
        profile = None
        if args.profile_file:
            profile = _read_command_object(None, args.profile_file, "profile")
            allowed = {"max_parallel", "reserve", "platform", "write_scope_case_sensitive"}
            if set(profile) - allowed:
                raise StateError("profile contains unknown fields: " + ", ".join(sorted(set(profile) - allowed)))
        state = store.create(repository, task, pathlib.Path(args.session_file), profile, args.mutation_id)
        return 0, "workflow_created", _public_state(state), []
    if command == "list":
        states = store.list_valid()
        if args.repo:
            identity = canonical_repository(pathlib.Path(args.repo))["identity"]
            states = [state for state in states if state["repository"]["identity"] == identity]
        return 0, "workflows_listed", [_public_state(state) for state in states], []
    if command in ("status", "context"):
        state = store.load(args.workflow_id)
        return 0, "workflow_loaded", _public_state(state, full=command == "context"), []
    if command == "reconcile-commit":
        result = store.reconcile_commit(args.workflow_id, args.mutation_id, args.digest)
        exit_code = 0 if result["outcome"] == "applied" else 1
        return exit_code, "mutation_" + result["outcome"], result, []
    if command == "controller-takeover":
        state = store.takeover(
            args.workflow_id,
            session_file=pathlib.Path(args.session_file),
            mutation_id=args.mutation_id,
            expected_revision=args.expected_revision,
        )
        return 0, "controller_taken_over", _public_state(state), []

    if command == "resume":
        def resume(state: dict[str, Any]) -> dict[str, Any]:
            if not state["controller"]["resume_required"]:
                raise StateError("workflow does not require resume")
            state["controller"]["resume_required"] = False
            _refresh_recovery_status(state)
            return add_event(state, "workflow_resumed", args.message)

        state, result, replay = _mutate_command(
            store, args, command, {"message": args.message}, resume, allow_resume_required=True
        )
        return 0, "mutation_reconciled" if replay else "workflow_resumed", {**_public_state(state), "event": result}, []

    if command == "node-add":
        node = _new_node(args)
        operation = {**node, "route": {"rationale": args.rationale, "attempt": 1}}

        def add(state: dict[str, Any]) -> dict[str, Any]:
            if args.node_id in state["nodes"]:
                raise StateError("node already exists")
            if any(dependency not in state["nodes"] for dependency in node["dependencies"]):
                raise StateError("node has an unresolved dependency")
            state["nodes"][args.node_id] = node
            return add_event(state, "node_added", args.title, args.node_id)

        state, result, replay = _mutate_command(store, args, command, operation, add)
        return 0, "mutation_reconciled" if replay else "node_added", {**_public_state(state), "event": result}, []

    if command == "node-route":
        operation = {
            "node_id": args.node_id,
            "role": args.role,
            "model": args.model,
            "effort": args.effort,
            "rationale": args.rationale,
        }

        def route(state: dict[str, Any]) -> dict[str, Any]:
            node = state["nodes"].get(args.node_id)
            retry_launch = bool(node and node["status"] == "failed" and node["launch"]["state"] == "terminal")
            if (
                not node
                or node["status"] not in ("pending", "ready", "failed")
                or (node["launch"]["state"] != "unclaimed" and not retry_launch)
            ):
                raise StateError("only unclaimed future/retry work can be routed")
            if retry_launch:
                node["launch"] = {
                    "state": "unclaimed",
                    "request_id": None,
                    "child_id": None,
                    "claimed_at": None,
                    "reconciliation": None,
                }
            node.update({"role": args.role, "model": args.model, "effort": args.effort})
            node["route"] = {"rationale": args.rationale, "routed_at": now_iso(), "attempt": len(node["attempts"]) + 1}
            if node["status"] == "failed":
                node["status"] = "pending"
                node["result"] = None
                node["evidence"] = None
            return add_event(state, "node_routed", args.rationale, args.node_id)

        state, result, replay = _mutate_command(store, args, command, operation, route)
        return 0, "mutation_reconciled" if replay else "node_routed", {**_public_state(state), "event": result}, []

    if command == "node-update":
        operation = {
            "node_id": args.node_id,
            "status": args.status,
            "launch_state": args.launch_state,
            "request_id": args.request_id,
            "child_id": args.child_id,
            "reconciliation": args.reconciliation,
            "result": args.result,
            "evidence": args.evidence,
            "actual_cost": args.actual_cost,
            "attempt_outcome": args.attempt_outcome,
        }

        def update(state: dict[str, Any]) -> dict[str, Any]:
            node = state["nodes"].get(args.node_id)
            if not node:
                raise StateError("unknown node")
            if not any(value is not None for key, value in operation.items() if key != "node_id"):
                raise StateError("node-update requires a changed field")
            if args.launch_state is not None:
                allowed_launch = {
                    "unclaimed": {"claimed"},
                    "claimed": {"reconcile_required", "bound"},
                    "reconcile_required": {"bound", "unclaimed"},
                    "bound": {"running", "terminal"},
                    "running": {"terminal"},
                    "terminal": set(),
                }
                old_launch = node["launch"]["state"]
                if args.launch_state not in allowed_launch[old_launch]:
                    raise StateError(f"invalid launch transition {old_launch} -> {args.launch_state}")
                if args.launch_state == "claimed":
                    if args.node_id not in ready_nodes(state):
                        raise StateError("launch claim requires ready, dependency-safe, unblocked future work")
                    if not args.request_id:
                        raise StateError("launch claim requires --request-id")
                    if node["attempts"] and node["attempts"][-1]["finished_at"] is None:
                        raise StateError("prior launch attempt must be reconciled before another claim")
                    if node["route"]["attempt"] != len(node["attempts"]) + 1:
                        raise StateError("persist a fresh node-route for this launch attempt")
                    claimed_at = now_iso()
                    node["launch"].update(
                        {"state": "claimed", "request_id": args.request_id, "claimed_at": claimed_at, "child_id": None, "reconciliation": None}
                    )
                    if len(node["attempts"]) >= 32:
                        raise StateError("node attempt limit reached", code="capacity_exceeded", exit_code=20)
                    node["attempts"].append(
                        {"number": len(node["attempts"]) + 1, "started_at": claimed_at, "finished_at": None, "outcome": None}
                    )
                elif args.launch_state == "reconcile_required":
                    node["launch"]["state"] = "reconcile_required"
                    node["launch"]["reconciliation"] = args.reconciliation or "provider outcome is uncertain"
                    state["controller"]["recovery_status"] = "reconcile_required"
                elif args.launch_state == "bound":
                    if not args.child_id:
                        raise StateError("bound launch requires --child-id")
                    if old_launch == "reconcile_required" and not args.reconciliation:
                        raise StateError("reconciled binding requires --reconciliation evidence")
                    node["launch"].update({"state": "bound", "child_id": args.child_id, "reconciliation": args.reconciliation})
                    _refresh_recovery_status(state)
                elif args.launch_state == "unclaimed":
                    if not args.reconciliation:
                        raise StateError("safe retry requires provider reconciliation evidence")
                    if node["attempts"] and node["attempts"][-1]["finished_at"] is None:
                        node["attempts"][-1]["finished_at"] = now_iso()
                        node["attempts"][-1]["outcome"] = "provider confirmed not launched"
                    node["launch"] = {
                        "state": "unclaimed",
                        "request_id": None,
                        "child_id": None,
                        "claimed_at": None,
                        "reconciliation": args.reconciliation,
                    }
                    _refresh_recovery_status(state)
                elif state["status"] == "aborted" and args.launch_state == "terminal":
                    node["launch"].update({"state": "terminal", "reconciliation": args.reconciliation})
                    node["attempts"][-1].update({"finished_at": now_iso(), "outcome": args.attempt_outcome})
                    _refresh_recovery_status(state)
                else:
                    node["launch"]["state"] = args.launch_state
            if args.status is not None:
                allowed_status = {
                    "pending": {"ready", "blocked", "skipped", "cancelled"},
                    "ready": {"running", "blocked", "skipped", "cancelled"},
                    "running": {"done", "failed", "cancelled"},
                    "blocked": {"pending", "ready", "failed", "cancelled"},
                    "failed": {"pending"},
                    "done": set(),
                    "skipped": set(),
                    "cancelled": set(),
                }
                old_status = node["status"]
                if args.status not in allowed_status[old_status]:
                    raise StateError(f"invalid node transition {old_status} -> {args.status}")
                if args.status == "running":
                    if node["launch"]["state"] not in ("bound", "running"):
                        raise StateError("running node requires a bound child launch")
                    if any(state["nodes"][dependency]["status"] not in SUCCESS_NODE_STATUSES for dependency in node["dependencies"]):
                        raise StateError("node dependencies are not terminal-successful")
                    node["launch"]["state"] = "running"
                    state["status"] = "running"
                    state["phase"] = node["stage"]
                if args.status == "ready" and any(
                    state["nodes"][dependency]["status"] not in SUCCESS_NODE_STATUSES for dependency in node["dependencies"]
                ):
                    raise StateError("node dependencies are not terminal-successful")
                node["status"] = args.status
                if args.status in TERMINAL_NODE_STATUSES:
                    if args.status == "done" and (not args.result or not args.evidence):
                        raise StateError("done node requires --result and --evidence")
                    node["result"] = args.result
                    node["evidence"] = args.evidence
                    if node["launch"]["child_id"]:
                        node["launch"]["state"] = "terminal"
                    if node["attempts"]:
                        node["attempts"][-1]["finished_at"] = now_iso()
                        node["attempts"][-1]["outcome"] = args.attempt_outcome or args.status
            if args.actual_cost is not None:
                node["actual_cost"] = args.actual_cost
            return add_event(state, "node_updated", f"node status={node['status']} launch={node['launch']['state']}", args.node_id)

        state, result, replay = _mutate_command(store, args, command, operation, update)
        return 0, "mutation_reconciled" if replay else "node_updated", {**_public_state(state), "event": result}, []

    if command == "graph-validate":
        state = store.load(args.workflow_id)
        diagnostics = graph_diagnostics(state["nodes"], case_sensitive=state["conventions"]["write_scope_case_sensitive"])
        diagnostics["ready_nodes"] = ready_nodes(state)
        return 0, "graph_valid", diagnostics, []

    if command == "graph-replan":
        plan = _read_command_object(args.plan_json, args.plan_file, "plan")
        if set(plan) != {"reason", "operations"} or not isinstance(plan["operations"], list) or not plan["operations"]:
            raise StateError("plan must contain exactly reason and a non-empty operations list")

        def replan(state: dict[str, Any]) -> dict[str, Any]:
            for item in plan["operations"]:
                if not isinstance(item, dict) or item.get("op") not in ("dependency_add", "dependency_remove", "priority", "remove", "supersede"):
                    raise StateError("plan contains an unsupported operation")
                node_id = item.get("node_id")
                if not isinstance(node_id, str) or node_id not in state["nodes"]:
                    raise StateError("plan references an unknown node")
                node = state["nodes"][node_id]
                if node["status"] not in ("pending", "ready", "failed") or node["launch"]["state"] != "unclaimed":
                    raise StateError("replan can change only unclaimed future work")
                if item["op"] in ("dependency_add", "dependency_remove"):
                    dependency = item.get("dependency")
                    if (
                        set(item) != {"op", "node_id", "dependency"}
                        or not isinstance(dependency, str)
                        or dependency not in state["nodes"]
                    ):
                        raise StateError("dependency operation is malformed")
                    dependencies = node["dependencies"]
                    if item["op"] == "dependency_add" and dependency not in dependencies:
                        dependencies.append(dependency)
                    if item["op"] == "dependency_remove" and dependency in dependencies:
                        dependencies.remove(dependency)
                elif item["op"] == "priority":
                    if (
                        set(item) != {"op", "node_id", "value"}
                        or not isinstance(item["value"], int)
                        or isinstance(item["value"], bool)
                        or not 0 <= item["value"] <= 100
                    ):
                        raise StateError("priority operation is malformed")
                    node["priority"] = item["value"]
                elif item["op"] == "remove":
                    if set(item) != {"op", "node_id"} or any(node_id in other["dependencies"] for other in state["nodes"].values()):
                        raise StateError("only an unreferenced node can be removed")
                    del state["nodes"][node_id]
                else:
                    replacement = item.get("replacement")
                    if (
                        set(item) != {"op", "node_id", "replacement"}
                        or not isinstance(replacement, str)
                        or replacement not in state["nodes"]
                    ):
                        raise StateError("supersede operation is malformed")
                    if replacement == node_id:
                        raise StateError("node cannot supersede itself")
                    node["status"] = "skipped"
                    node["result"] = "superseded"
                    node["evidence"] = plan["reason"]
                    node["superseded_by"] = replacement
                    for other in state["nodes"].values():
                        other["dependencies"] = [replacement if value == node_id else value for value in other["dependencies"]]
                        other["dependencies"] = list(dict.fromkeys(other["dependencies"]))
            return add_event(state, "graph_replanned", plan["reason"])

        state, result, replay = _mutate_command(store, args, command, plan, replan)
        return 0, "mutation_reconciled" if replay else "graph_replanned", {**_public_state(state), "event": result}, []

    if command == "requirement-set":
        operation = {
            "requirement_id": args.requirement_id,
            "text": args.text,
            "source": args.source,
            "status": args.status,
            "evidence": args.evidence,
        }

        def requirement(state: dict[str, Any]) -> dict[str, Any]:
            if args.status != "active" and not args.evidence:
                raise StateError("resolved requirement needs evidence")
            state["requirements"][args.requirement_id] = {
                "text": args.text,
                "source": args.source,
                "status": args.status,
                "evidence": args.evidence,
            }
            return add_event(state, "requirement_set", args.requirement_id)

        state, result, replay = _mutate_command(store, args, command, operation, requirement)
        return 0, "mutation_reconciled" if replay else "requirement_set", {**_public_state(state), "event": result}, []

    if command == "decision":
        operation = {"text": args.text, "rationale": args.rationale}

        def decision(state: dict[str, Any]) -> dict[str, Any]:
            item = {
                "id": "decision-" + hashlib.sha256(args.mutation_id.encode()).hexdigest()[:16],
                "text": args.text,
                "rationale": args.rationale,
                "at": now_iso(),
            }
            state["decisions"].append(item)
            add_event(state, "decision_recorded", args.text)
            return item

        state, result, replay = _mutate_command(store, args, command, operation, decision)
        return 0, "mutation_reconciled" if replay else "decision_recorded", {**_public_state(state), "decision": result}, []

    if command == "block":
        operation = {"node_id": args.node_id, "reason": args.reason, "needed": args.needed}

        def block(state: dict[str, Any]) -> dict[str, Any]:
            if args.node_id and args.node_id not in state["nodes"]:
                raise StateError("blocker references unknown node")
            if args.node_id and state["nodes"][args.node_id]["status"] not in ("pending", "ready"):
                raise StateError("only unlaunched future work can be blocked")
            item = {
                "id": "blocker-" + hashlib.sha256(args.mutation_id.encode()).hexdigest()[:16],
                "node_id": args.node_id,
                "reason": args.reason,
                "needed": args.needed,
                "status": "active",
                "resolution": None,
                "at": now_iso(),
            }
            state["blockers"].append(item)
            if args.node_id:
                state["nodes"][args.node_id]["status"] = "blocked"
            else:
                state["status"] = "blocked"
            add_event(state, "workflow_blocked", args.reason, args.node_id)
            return item

        state, result, replay = _mutate_command(store, args, command, operation, block)
        return 0, "mutation_reconciled" if replay else "workflow_blocked", {**_public_state(state), "blocker": result}, []

    if command == "unblock":
        operation = {"blocker_id": args.blocker_id, "resolution": args.resolution}

        def unblock(state: dict[str, Any]) -> dict[str, Any]:
            blocker = next((item for item in state["blockers"] if item["id"] == args.blocker_id), None)
            if not blocker or blocker["status"] != "active":
                raise StateError("active blocker not found")
            blocker["status"] = "resolved"
            blocker["resolution"] = args.resolution
            if blocker["node_id"] and state["nodes"][blocker["node_id"]]["status"] == "blocked":
                state["nodes"][blocker["node_id"]]["status"] = "pending"
            if not any(item["status"] == "active" and item["node_id"] is None for item in state["blockers"]):
                if state["status"] == "blocked":
                    state["status"] = "running" if state["nodes"] else "planning"
            return add_event(state, "workflow_unblocked", args.resolution, blocker["node_id"])

        state, result, replay = _mutate_command(store, args, command, operation, unblock)
        return 0, "mutation_reconciled" if replay else "workflow_unblocked", {**_public_state(state), "event": result}, []

    if command == "event":
        operation = {"kind": args.kind, "message": args.message, "node_id": args.node_id}

        def event(state: dict[str, Any]) -> dict[str, Any]:
            if args.node_id and args.node_id not in state["nodes"]:
                raise StateError("event references unknown node")
            return add_event(state, args.kind, args.message, args.node_id)

        state, result, replay = _mutate_command(store, args, command, operation, event)
        return 0, "mutation_reconciled" if replay else "event_recorded", {**_public_state(state), "event": result}, []

    if command == "finish":
        operation = {"summary": args.summary, "validation": args.validation, "commit": args.commit}

        def finish(state: dict[str, Any]) -> dict[str, Any]:
            if not COMMIT_RE.fullmatch(args.commit):
                raise StateError("finish requires a full lowercase commit checkpoint")
            if not state["nodes"] or any(node["status"] not in SUCCESS_NODE_STATUSES for node in state["nodes"].values()):
                raise StateError("all visible nodes must be terminal-successful")
            if any(item["status"] == "active" for item in state["requirements"].values()):
                raise StateError("all requirements must be resolved")
            if any(item["status"] == "active" for item in state["blockers"]):
                raise StateError("all blockers must be resolved")
            state["status"] = "completed"
            state["phase"] = "completed"
            state["git"]["checkpoint"] = args.commit
            return add_event(state, "workflow_finished", args.summary + "; validation: " + args.validation)

        state, result, replay = _mutate_command(store, args, command, operation, finish)
        return 0, "mutation_reconciled" if replay else "workflow_completed", {**_public_state(state), "event": result}, []

    if command == "abort":
        def abort(state: dict[str, Any]) -> dict[str, Any]:
            state["status"] = "aborted"
            state["phase"] = "aborted"
            for node in state["nodes"].values():
                if node["status"] not in TERMINAL_NODE_STATUSES:
                    node["status"] = "cancelled"
                    node["result"] = args.reason
                    node["evidence"] = "controller abort"
                    if node["launch"]["state"] in ("claimed", "reconcile_required", "bound", "running"):
                        node["launch"]["state"] = "reconcile_required"
                        node["launch"]["reconciliation"] = "abort requires provider outcome reconciliation"
            _refresh_recovery_status(state)
            return add_event(state, "workflow_aborted", args.reason)

        state, result, replay = _mutate_command(store, args, command, {"reason": args.reason}, abort)
        return 0, "mutation_reconciled" if replay else "workflow_aborted", {**_public_state(state), "event": result}, []
    raise StateError("unsupported command")
