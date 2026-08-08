"""Command adapter for the durable workflow-state owner."""

from __future__ import annotations

import argparse
from typing import Any, Sequence

from coordinator.cli.outcome import OutcomeArgumentParser, emit, parse_invocation
from coordinator.state.store import EFFORTS, LAUNCH_STATES, MODELS, NODE_STATUSES, ROLES, StateError, StateStore, execute_command


def _add_mutation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--mutation-id", required=True)
    parser.add_argument("--expected-revision", required=True, type=int)


def _subcommand(subparsers: Any, name: str, *, mutate: bool = False) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name)
    if mutate:
        _add_mutation_arguments(parser)
    parser.add_argument("--json", action="store_true")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = OutcomeArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    opened = _subcommand(sub, "session-open")
    opened.add_argument("--repo", required=True)
    opened.add_argument("--session-file", required=True)
    closed = _subcommand(sub, "session-close")
    closed.add_argument("--session-file", required=True)
    init = _subcommand(sub, "init")
    init.add_argument("--repo", required=True)
    task = init.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file")
    init.add_argument("--profile-file")
    init.add_argument("--session-file", required=True)
    init.add_argument("--mutation-id", required=True)
    listed = _subcommand(sub, "list")
    listed.add_argument("--repo")
    for name in ("status", "context"):
        target = _subcommand(sub, name)
        target.add_argument("--workflow-id", required=True)
    reconcile = _subcommand(sub, "reconcile-commit")
    reconcile.add_argument("--workflow-id", required=True)
    reconcile.add_argument("--mutation-id", required=True)
    reconcile.add_argument("--digest")
    _subcommand(sub, "controller-takeover", mutate=True)
    resume = _subcommand(sub, "resume", mutate=True)
    resume.add_argument("--message", required=True)

    add = _subcommand(sub, "node-add", mutate=True)
    add.add_argument("--node-id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--stage", required=True)
    add.add_argument("--priority", type=int, default=50)
    add.add_argument("--dependency", action="append", default=[])
    add.add_argument("--write-scope", action="append", required=True)
    add.add_argument("--role", choices=ROLES, required=True)
    add.add_argument("--model", choices=MODELS, required=True)
    add.add_argument("--effort", choices=EFFORTS, required=True)
    add.add_argument("--acceptance", action="append", required=True)
    add.add_argument("--rationale", required=True)
    add.add_argument("--estimated-cost", type=float)
    route = _subcommand(sub, "node-route", mutate=True)
    route.add_argument("--node-id", required=True)
    route.add_argument("--role", choices=ROLES, required=True)
    route.add_argument("--model", choices=MODELS, required=True)
    route.add_argument("--effort", choices=EFFORTS, required=True)
    route.add_argument("--rationale", required=True)
    update = _subcommand(sub, "node-update", mutate=True)
    update.add_argument("--node-id", required=True)
    update.add_argument("--status", choices=NODE_STATUSES)
    update.add_argument("--launch-state", choices=LAUNCH_STATES)
    update.add_argument("--request-id")
    update.add_argument("--child-id")
    update.add_argument("--reconciliation")
    update.add_argument("--result")
    update.add_argument("--evidence")
    update.add_argument("--actual-cost", type=float)
    update.add_argument("--attempt-outcome")
    graph = _subcommand(sub, "graph-validate")
    graph.add_argument("--workflow-id", required=True)
    replan = _subcommand(sub, "graph-replan", mutate=True)
    plan = replan.add_mutually_exclusive_group(required=True)
    plan.add_argument("--plan-json")
    plan.add_argument("--plan-file")
    requirement = _subcommand(sub, "requirement-set", mutate=True)
    requirement.add_argument("--requirement-id", required=True)
    requirement.add_argument("--text", required=True)
    requirement.add_argument("--source", required=True)
    requirement.add_argument("--status", choices=("active", "satisfied", "superseded"), required=True)
    requirement.add_argument("--evidence")
    decision = _subcommand(sub, "decision", mutate=True)
    decision.add_argument("--text", required=True)
    decision.add_argument("--rationale", required=True)
    blocked = _subcommand(sub, "block", mutate=True)
    blocked.add_argument("--node-id")
    blocked.add_argument("--reason", required=True)
    blocked.add_argument("--needed", required=True)
    unblocked = _subcommand(sub, "unblock", mutate=True)
    unblocked.add_argument("--blocker-id", required=True)
    unblocked.add_argument("--resolution", required=True)
    event = _subcommand(sub, "event", mutate=True)
    event.add_argument("--kind", required=True)
    event.add_argument("--message", required=True)
    event.add_argument("--node-id")
    finish = _subcommand(sub, "finish", mutate=True)
    finish.add_argument("--summary", required=True)
    finish.add_argument("--validation", required=True)
    finish.add_argument("--commit", required=True)
    abort = _subcommand(sub, "abort", mutate=True)
    abort.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, invalid = parse_invocation(build_parser(), argv)
    if args is None:
        return invalid
    try:
        exit_code, code, data, warnings = execute_command(args, StateStore())
        return emit(args.command, code=code, data=data, warnings=warnings, exit_code=exit_code, as_json=args.json)
    except StateError as exc:
        return emit(args.command, code=exc.code, data={"message": str(exc)}, exit_code=exc.exit_code, as_json=args.json)
    except OSError as exc:
        wrapped = StateError("workflow operation failed at the operating-system boundary", code="io_error", exit_code=20)
        wrapped.__cause__ = exc
        return emit(args.command, code=wrapped.code, data={"message": str(wrapped)}, exit_code=wrapped.exit_code, as_json=args.json)
