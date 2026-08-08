"""Command adapter for read-only diagnostics."""

from __future__ import annotations

import argparse
import pathlib
from typing import Sequence

from coordinator.cli.outcome import OutcomeArgumentParser, emit, parse_invocation
from coordinator.install.doctor import check


def build_parser() -> argparse.ArgumentParser:
    parser = OutcomeArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "check"):
        command = sub.add_parser(name)
        command.add_argument("--repo")
        command.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, invalid = parse_invocation(build_parser(), argv)
    if args is None:
        return invalid
    result = check(pathlib.Path(args.repo) if args.repo else None)
    data = {key: value for key, value in result.items() if key != "warnings"}
    return emit(
        args.command,
        code="doctor_passed" if result["ok"] else "doctor_failed",
        data=data,
        warnings=[item["message"] for item in result["warnings"]],
        exit_code=0 if result["ok"] else 1,
        as_json=args.json,
    )
