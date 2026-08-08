"""Command adapter for the read-only dashboard."""

from __future__ import annotations

import argparse
import pathlib
import time
import webbrowser
from typing import Sequence

from coordinator.cli.outcome import OutcomeArgumentParser, emit, parse_invocation
from coordinator.dashboard.view import DashboardError, make_server, render, select_snapshot, snapshot_digest, validate_interval
from coordinator.state.store import StateError, StateStore


def _selector(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--workflow-id")
    group.add_argument("--repo")


def build_parser() -> argparse.ArgumentParser:
    parser = OutcomeArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    _selector(serve)
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--interval", type=float, default=1.0)
    serve.add_argument("--open", action="store_true")
    serve.add_argument("--json", action="store_true")
    rendered = sub.add_parser("render")
    _selector(rendered)
    rendered.add_argument("--out", required=True)
    rendered.add_argument("--json", action="store_true")
    watch = sub.add_parser("watch")
    _selector(watch)
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, invalid = parse_invocation(build_parser(), argv)
    if args is None:
        return invalid
    repository = pathlib.Path(args.repo) if args.repo else None
    try:
        interval = validate_interval(args.interval) if hasattr(args, "interval") else 1.0
        if args.command == "render":
            snapshot = select_snapshot(StateStore(), workflow_id=args.workflow_id, repo=repository)
            render(snapshot, pathlib.Path(args.out))
            return emit(
                args.command,
                code="report_written",
                data={"output": str(pathlib.Path(args.out).expanduser().resolve()), "workflows": len(snapshot["workflows"])},
                as_json=args.json,
            )
        if args.command == "watch":
            previous = None
            while True:
                snapshot = select_snapshot(StateStore(), workflow_id=args.workflow_id, repo=repository)
                digest = snapshot_digest(snapshot)
                if digest != previous:
                    if args.json:
                        emit(args.command, code="snapshot_observed", data=snapshot, as_json=True)
                    else:
                        print(f"snapshot {snapshot['generated_at']}")
                        for workflow in snapshot["workflows"]:
                            print(f"{workflow['workflow_id']} {workflow['status']}/{workflow['phase']} {workflow['progress_percent']}% ready={','.join(workflow['ready_order']) or '-'} blockers={len(workflow['blockers'])}")
                            print(
                                "  active={} launch={} capacity={}/{} recovery={} cost={}/{}".format(
                                    ",".join(workflow["active_work"]) or "-",
                                    ",".join(f"{key}:{value}" for key, value in workflow["launch"].items() if value) or "-",
                                    workflow["capacity"]["occupied"],
                                    workflow["capacity"]["usable"],
                                    workflow["controller"]["recovery_status"],
                                    workflow["cost"]["estimated_total"] if workflow["cost"]["estimated_total"] is not None else "unknown",
                                    workflow["cost"]["actual_total"] if workflow["cost"]["actual_total"] is not None else "unknown",
                                )
                            )
                        for diagnostic in snapshot["diagnostics"]:
                            print(f"diagnostic {diagnostic['code']}: {diagnostic['message']}")
                    previous = digest
                if args.once:
                    return 0
                time.sleep(interval)
        server = make_server(args.workflow_id, repository, args.port, interval)
        url = f"http://127.0.0.1:{server.server_port}/?capability={server.capability}"
        emit(args.command, code="dashboard_serving", data={"url": url, "address": "127.0.0.1", "port": server.server_port}, as_json=args.json)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=min(interval, 0.5))
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (DashboardError, StateError) as exc:
        return emit(args.command, code=getattr(exc, "code", "dashboard_error"), data={"message": str(exc)}, exit_code=getattr(exc, "exit_code", 20), as_json=args.json)
    except OSError as exc:
        wrapped = DashboardError("dashboard failed at the operating-system boundary", code="io_error", exit_code=20)
        wrapped.__cause__ = exc
        return emit(args.command, code=wrapped.code, data={"message": str(wrapped)}, exit_code=wrapped.exit_code, as_json=args.json)
