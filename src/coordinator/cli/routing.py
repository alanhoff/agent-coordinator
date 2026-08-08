"""Command adapter for task routing."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Sequence

from coordinator.cli.outcome import OutcomeArgumentParser, emit, parse_invocation
from coordinator.routing.selector import RoutingError, choose


def _json_file(path: str) -> Any:
    try:
        text = sys.stdin.read() if path == "-" else pathlib.Path(path).read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoutingError(f"unable to read valid JSON from {path}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = OutcomeArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    choose_parser = sub.add_parser("choose")
    choose_parser.add_argument("--task-file", required=True)
    choose_parser.add_argument("--profile-file")
    choose_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, invalid = parse_invocation(build_parser(), argv)
    if args is None:
        return invalid
    try:
        result = choose(_json_file(args.task_file), _json_file(args.profile_file) if args.profile_file else None)
        return emit(args.command, code="route_selected", data=result, as_json=args.json)
    except RoutingError as exc:
        return emit(args.command, code="invalid_routing_input", data={"message": str(exc)}, exit_code=2, as_json=args.json)
