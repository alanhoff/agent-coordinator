"""Read-only workflow selection, derivation, rendering, watch, and loopback serving."""

from __future__ import annotations

import hashlib
import html
import json
import math
import pathlib
import secrets
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from coordinator.state.store import SUCCESS_NODE_STATUSES, StateError, StateStore, canonical_repository, graph_diagnostics, now_iso, ready_nodes

MAX_REPLAY = 32


class DashboardError(RuntimeError):
    def __init__(self, message: str, *, code: str = "dashboard_error", exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _critical_path(nodes: Mapping[str, Mapping[str, Any]]) -> tuple[list[list[str]], list[str]]:
    remaining = set(nodes)
    completed: set[str] = set()
    layers: list[list[str]] = []
    while remaining:
        layer = sorted(node_id for node_id in remaining if set(nodes[node_id]["dependencies"]) <= completed)
        if not layer:
            return layers, []
        layers.append(layer)
        completed.update(layer)
        remaining.difference_update(layer)
    distance: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    for layer in layers:
        for node_id in layer:
            dependencies = nodes[node_id]["dependencies"]
            if dependencies:
                best = max(dependencies, key=lambda item: distance[item])
                distance[node_id] = distance[best] + 1
                parent[node_id] = best
            else:
                distance[node_id] = 1
                parent[node_id] = None
    if not distance:
        return layers, []
    current: str | None = max(distance, key=distance.get)  # type: ignore[arg-type]
    path: list[str] = []
    while current is not None:
        path.append(current)
        current = parent[current]
    return layers, list(reversed(path))


def derive_workflow(state: Mapping[str, Any]) -> dict[str, Any]:
    nodes = state["nodes"]
    visible = {node_id: node for node_id, node in nodes.items() if node["superseded_by"] is None}
    if state["status"] == "completed":
        progress = 100
    elif not visible:
        progress = 0
    else:
        progress = round(100 * sum(node["status"] in SUCCESS_NODE_STATUSES for node in visible.values()) / len(visible))
    estimated = [node["estimated_cost"] for node in visible.values() if node["estimated_cost"] is not None]
    actual = [node["actual_cost"] for node in visible.values() if node["actual_cost"] is not None]
    layers, critical = _critical_path(visible)
    launch_counts = {key: 0 for key in ("awaiting_external_call", "reconcile_required", "bound_or_running", "terminal", "unclaimed")}
    for node in visible.values():
        launch = node["launch"]["state"]
        if launch == "claimed":
            launch_counts["awaiting_external_call"] += 1
        elif launch == "reconcile_required":
            launch_counts["reconcile_required"] += 1
        elif launch in ("bound", "running"):
            launch_counts["bound_or_running"] += 1
        elif launch == "terminal":
            launch_counts["terminal"] += 1
        else:
            launch_counts["unclaimed"] += 1
    occupied = launch_counts["awaiting_external_call"] + launch_counts["reconcile_required"] + launch_counts["bound_or_running"]
    diagnostics = graph_diagnostics(nodes, case_sensitive=state["conventions"]["write_scope_case_sensitive"])
    active_blockers = [item for item in state["blockers"] if item["status"] == "active"]
    timeline = []
    for node_id, node in nodes.items():
        for attempt in node["attempts"]:
            timeline.append({"node_id": node_id, **attempt})
    timeline.sort(key=lambda item: item["started_at"], reverse=True)
    task_preview = state["task"].replace("\r", " ").replace("\n", " ")[:160]
    return {
        "workflow_id": state["workflow_id"],
        "repository": state["repository"],
        "task": state["task"],
        "task_preview": task_preview,
        "status": state["status"],
        "phase": state["phase"],
        "updated_at": state["updated_at"],
        "revision": state["revision"],
        "progress_percent": progress,
        "active_work": sorted(node_id for node_id, node in visible.items() if node["status"] == "running"),
        "ready_order": ready_nodes(state),
        "blockers": active_blockers,
        "nodes": nodes,
        "requirements": state["requirements"],
        "decisions": state["decisions"],
        "event_history": state["events"],
        "event_history_truncated": len(state["events"]) >= 512,
        "git": state["git"],
        "controller": {
            "epoch": state["controller"]["epoch"],
            "checkpoint": state["controller"]["checkpoint"],
            "resume_required": state["controller"]["resume_required"],
            "recovery_status": state["controller"]["recovery_status"],
        },
        "diagnostics": diagnostics,
        "layers": layers,
        "critical_path": critical,
        "launch": launch_counts,
        "capacity": {
            "max_parallel": state["conventions"]["max_parallel"],
            "reserve": state["conventions"]["reserve"],
            "usable": state["conventions"]["max_parallel"] - state["conventions"]["reserve"],
            "running": len([node for node in visible.values() if node["status"] == "running"]),
            "occupied": occupied,
            "available": state["conventions"]["max_parallel"] - state["conventions"]["reserve"] - occupied,
        },
        "cost": {
            "estimated_total": round(sum(estimated), 8) if estimated else None,
            "estimated_complete": len(estimated) == len(visible),
            "actual_total": round(sum(actual), 8) if actual else None,
            "actual_complete": len(actual) == len(visible),
        },
        "latest_attempts": timeline[:32],
    }


def select_snapshot(
    store: StateStore,
    *,
    workflow_id: str | None = None,
    repo: pathlib.Path | None = None,
) -> dict[str, Any]:
    if workflow_id and repo:
        raise DashboardError("--workflow-id and --repo are mutually exclusive")
    generated_at = now_iso()
    if workflow_id:
        try:
            state = store.load(workflow_id)
        except StateError as exc:
            if exc.code == "not_found":
                raise DashboardError("workflow not found", code="workflow_not_found", exit_code=1) from exc
            return {
                "generated_at": generated_at,
                "workflows": [],
                "diagnostics": [{"code": exc.code, "message": "selected workflow could not be validated"}],
            }
        return {"generated_at": generated_at, "workflows": [derive_workflow(state)], "diagnostics": []}
    repository_identity = canonical_repository(repo)["identity"] if repo else None
    workflows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    for _path, state, error in store.iter_records():
        if error is not None:
            diagnostics.append({"code": error.code, "message": "one stored workflow could not be validated"})
            continue
        assert state is not None
        if repository_identity and state["repository"]["identity"] != repository_identity:
            continue
        workflows.append(derive_workflow(state))
    workflows.sort(key=lambda item: item["updated_at"], reverse=True)
    return {"generated_at": generated_at, "workflows": workflows, "diagnostics": diagnostics}


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    stable = {"workflows": snapshot["workflows"], "diagnostics": snapshot["diagnostics"]}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class Replay:
    def __init__(self, maximum: int = MAX_REPLAY):
        self.maximum = maximum
        self.snapshots: list[dict[str, Any]] = []
        self.digests: list[str] = []
        self.evicted = 0
        self._lock = threading.Lock()

    def observe(self, snapshot: dict[str, Any]) -> None:
        digest = snapshot_digest(snapshot)
        with self._lock:
            if self.digests and self.digests[-1] == digest:
                return
            self.snapshots.append(snapshot)
            self.digests.append(digest)
            if len(self.snapshots) > self.maximum:
                self.snapshots.pop(0)
                self.digests.pop(0)
                self.evicted += 1

    def get(self, index: int | None = None) -> dict[str, Any]:
        with self._lock:
            if not self.snapshots:
                raise DashboardError("no committed snapshot is available", code="no_snapshot", exit_code=1)
            selected = len(self.snapshots) - 1 if index is None else index
            if selected < 0 or selected >= len(self.snapshots):
                raise DashboardError("replay index is outside retained history", code="replay_evicted", exit_code=1)
            return {
                "snapshot": self.snapshots[selected],
                "replay": {
                    "selected_index": selected,
                    "live_index": len(self.snapshots) - 1,
                    "retained": len(self.snapshots),
                    "evicted": self.evicted,
                    "bounded": True,
                },
            }


def _report(snapshot: Mapping[str, Any]) -> str:
    def e(value: Any) -> str:
        return html.escape(str(value), quote=True)

    def detail(title: str, value: Any) -> str:
        encoded = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
        return f"<h3>{e(title)}</h3><pre>{e(encoded)}</pre>"

    sections = []
    for workflow in snapshot["workflows"]:
        overview = {
            "active_work": workflow["active_work"],
            "blockers": workflow["blockers"],
            "cost": workflow["cost"],
            "last_change": workflow["updated_at"],
            "progress_percent": workflow["progress_percent"],
            "ready_order": workflow["ready_order"],
            "status": workflow["status"],
            "phase": workflow["phase"],
        }
        sections.append(
            f"<section><h2>{e(workflow['workflow_id'])}</h2>"
            f"<p><strong>Repository:</strong> {e(workflow['repository']['path'])}</p>"
            f"<p><strong>Task:</strong> {e(workflow['task'])}</p>"
            + detail("Overview", overview)
            + detail("Dependency DAG and node detail", {"layers": workflow["layers"], "critical_path": workflow["critical_path"], "nodes": workflow["nodes"]})
            + detail("Requirements", workflow["requirements"])
            + detail("Decisions and blockers", {"decisions": workflow["decisions"], "blockers": workflow["blockers"]})
            + detail("Git, checkpoint, controller, resume, and recovery", {"git": workflow["git"], "controller": workflow["controller"]})
            + detail("Graph and state diagnostics", workflow["diagnostics"])
            + detail("Capacity, reserve, launch, and cost", {"capacity": workflow["capacity"], "launch": workflow["launch"], "cost": workflow["cost"]})
            + detail("Latest attempt timeline", workflow["latest_attempts"])
            + detail("Bounded event history", {"truncated": workflow["event_history_truncated"], "events": workflow["event_history"]})
            + "</section>"
        )
    diagnostic = "".join(f"<li>{e(item['code'])}: {e(item['message'])}</li>" for item in snapshot["diagnostics"])
    empty = "<p>No matching workflows.</p>" if not sections else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta http-equiv=Content-Security-Policy content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\">"
        "<title>Coordinator report</title><style>body{font:15px system-ui;max-width:1100px;margin:auto;padding:2rem;color:#17202a}"
        "section{border:1px solid #ccd4dc;border-radius:10px;padding:1rem;margin:1rem 0}pre{white-space:pre-wrap;word-break:break-word;background:#f6f8fa;padding:.75rem;border-radius:6px}"
        "h1,h2,h3{line-height:1.2}</style></head><body><h1>Coordinator workflow report</h1>"
        f"<p>Committed snapshot: {e(snapshot['generated_at'])}</p>{empty}{''.join(sections)}"
        f"<h2>Diagnostics</h2><ul>{diagnostic or '<li>None</li>'}</ul></body></html>"
    )


def render(snapshot: Mapping[str, Any], output: pathlib.Path) -> None:
    target = output.expanduser().absolute()
    if not target.parent.is_dir() or target.parent.is_symlink() or target.is_symlink():
        raise DashboardError("render output parent/target is unsafe", code="unsafe_output", exit_code=20)
    data = _report(snapshot).encode("utf-8")
    temporary = target.parent / f".{target.name}.{secrets.token_hex(6)}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise DashboardError("unable to write standalone report", code="io_error", exit_code=20) from exc


INTERACTIVE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coordinator dashboard</title><style nonce="{nonce}">
body{{font:15px system-ui;margin:0;background:#f4f6f8;color:#15202b}}main{{max-width:1100px;margin:auto;padding:1.5rem}}
.card{{background:white;border:1px solid #d5dce3;border-radius:12px;padding:1rem;margin:1rem 0}}button{{margin-right:.5rem}}pre{{white-space:pre-wrap;word-break:break-word}}
</style></head><body><main><h1>Coordinator dashboard</h1><div><button id="previous">Previous</button><button id="next">Next</button><button id="live">Live</button><span id="replay"></span></div><div id="content"></div></main>
<script nonce="{nonce}">'use strict';
const token=new URL(location.href).searchParams.get('capability'); history.replaceState(null,'',location.pathname);
let index=null,liveIndex=null; const content=document.getElementById('content'), replay=document.getElementById('replay');
function text(tag,value){{const node=document.createElement(tag);node.textContent=String(value);return node}}
function detail(card,title,value){{card.append(text('h3',title));card.append(text('pre',JSON.stringify(value,null,2)))}}
function show(payload){{content.replaceChildren();const meta=payload.replay;liveIndex=meta.live_index;replay.textContent=` retained ${{meta.retained}}, evicted ${{meta.evicted}}, showing ${{meta.selected_index+1}}/${{meta.retained}}`;
for(const workflow of payload.snapshot.workflows){{const card=document.createElement('section');card.className='card';card.append(text('h2',workflow.workflow_id));card.append(text('p',workflow.repository.path));card.append(text('p',workflow.task));detail(card,'Overview',{{status:workflow.status,phase:workflow.phase,last_change:workflow.updated_at,progress_percent:workflow.progress_percent,active_work:workflow.active_work,ready_order:workflow.ready_order,blockers:workflow.blockers,cost:workflow.cost}});detail(card,'Dependency DAG and node detail',{{layers:workflow.layers,critical_path:workflow.critical_path,nodes:workflow.nodes}});detail(card,'Requirements',workflow.requirements);detail(card,'Decisions and blockers',{{decisions:workflow.decisions,blockers:workflow.blockers}});detail(card,'Git, checkpoint, controller, resume, and recovery',{{git:workflow.git,controller:workflow.controller}});detail(card,'Graph and state diagnostics',workflow.diagnostics);detail(card,'Capacity, reserve, launch, and cost',{{capacity:workflow.capacity,launch:workflow.launch,cost:workflow.cost}});detail(card,'Latest attempt timeline',workflow.latest_attempts);detail(card,'Bounded event history',{{truncated:workflow.event_history_truncated,events:workflow.event_history}});content.append(card)}}
for(const diagnostic of payload.snapshot.diagnostics){{content.append(text('p',`${{diagnostic.code}}: ${{diagnostic.message}}`))}} index=meta.selected_index}}
async function load(requested){{let path='/api/snapshot';if(requested!==null)path+='?index='+requested;const response=await fetch(path,{{headers:{{'X-Coordinator-Capability':token}}}});const data=await response.json();if(!response.ok)throw new Error(data.code);show(data)}}
document.getElementById('previous').onclick=()=>load(Math.max(0,index-1));document.getElementById('next').onclick=()=>load(index+1).catch(()=>load(null));document.getElementById('live').onclick=()=>load(null);load(null);setInterval(()=>{{if(index===liveIndex)load(null)}},{interval_ms});
</script></body></html>"""


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], selector: dict[str, Any], interval: float):
        self.capability = secrets.token_urlsafe(32)
        self.selector = selector
        self.interval_ms = round(interval * 1000)
        self.replay = Replay()
        super().__init__(address, DashboardHandler)

    def observe(self) -> None:
        self.replay.observe(select_snapshot(StateStore(), **self.selector))


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _headers(self, status: int, content_type: str, *, nonce: str | None = None) -> None:
        inline_source = f"'nonce-{nonce}'" if nonce else "'none'"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; script-src {inline_source}; style-src {inline_source}; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _reject(self, status: int, code: str) -> None:
        payload = json.dumps({"code": code}).encode()
        self._headers(status, "application/json; charset=utf-8")
        self.wfile.write(payload)

    def _authority_ok(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        return (
            self.client_address[0] == "127.0.0.1"
            and self.headers.get("Host") == expected
            and (origin is None or origin == "http://" + expected)
        )

    def do_GET(self) -> None:  # noqa: N802
        if not self._authority_ok():
            self._reject(HTTPStatus.FORBIDDEN, "invalid_authority")
            return
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=False)
        if parsed.path == "/":
            supplied = query.get("capability", [])
            if set(query) != {"capability"} or len(supplied) != 1 or not secrets.compare_digest(supplied[0], self.server.capability):
                self._reject(HTTPStatus.UNAUTHORIZED, "unauthorized")
                return
            nonce = secrets.token_urlsafe(32)
            body = INTERACTIVE_HTML.format(nonce=nonce, interval_ms=self.server.interval_ms).encode()
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", nonce=nonce)
            self.wfile.write(body)
            return
        if parsed.path != "/api/snapshot" or not secrets.compare_digest(self.headers.get("X-Coordinator-Capability", ""), self.server.capability):
            self._reject(HTTPStatus.UNAUTHORIZED if parsed.path == "/api/snapshot" else HTTPStatus.NOT_FOUND, "unauthorized" if parsed.path == "/api/snapshot" else "not_found")
            return
        try:
            self.server.observe()
            index_values = query.get("index")
            if set(query) - {"index"} or (index_values is not None and len(index_values) != 1):
                raise DashboardError("invalid query", code="invalid_query")
            index = int(index_values[0]) if index_values else None
            body = json.dumps(self.server.replay.get(index), ensure_ascii=False).encode()
            self._headers(HTTPStatus.OK, "application/json; charset=utf-8")
            self.wfile.write(body)
        except (DashboardError, StateError, ValueError) as exc:
            self._reject(HTTPStatus.CONFLICT, getattr(exc, "code", "invalid_query"))

    def _read_only(self) -> None:
        if not self._authority_ok():
            self._reject(HTTPStatus.FORBIDDEN, "invalid_authority")
            return
        if not secrets.compare_digest(self.headers.get("X-Coordinator-Capability", ""), self.server.capability):
            self._reject(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "read_only")

    do_POST = do_PUT = do_PATCH = do_DELETE = _read_only  # type: ignore[assignment]


def validate_interval(interval: float) -> float:
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not math.isfinite(interval) or not 0.1 <= interval <= 3600:
        raise DashboardError("interval must be from 0.1 through 3600 seconds")
    return float(interval)


def make_server(workflow_id: str | None, repo: pathlib.Path | None, port: int, interval: float = 1.0) -> DashboardServer:
    if not 0 <= port <= 65535:
        raise DashboardError("port must be from 0 through 65535")
    server = DashboardServer(("127.0.0.1", port), {"workflow_id": workflow_id, "repo": repo}, validate_interval(interval))
    server.observe()
    return server
