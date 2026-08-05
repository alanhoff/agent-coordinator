#!/usr/bin/env python3
"""Validate a coordinator package or bootstrapped project on Unix and Windows."""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

import install


def inspect_project(
    project_root: pathlib.Path,
    source_root: Optional[pathlib.Path] = None,
    activation_token: Optional[str] = None,
    require_activation: bool = False,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    checks: Dict[str, Any] = {}

    try:
        runtime = install.require_runtime_dependencies(require_git=True)
    except install.InstallError as exc:
        runtime = {"error": str(exc)}
        errors.append(str(exc))
    checks["runtime"] = runtime

    root = project_root.expanduser().resolve()
    git = runtime.get("git") if isinstance(runtime, dict) else None
    if git:
        try:
            root, _ = install.resolve_project(root, str(git), initialize=False)
        except install.InstallError as exc:
            errors.append(str(exc))

    source_path = source_root or (root / install.SKILL_REL)
    try:
        source = install.LocalPackageSource(source_path)
        installation = install.inspect_installation(root, source)
        checks["installation"] = installation
        if not installation["current"]:
            errors.append("project installation is missing or outdated: " + json.dumps(installation["mismatches"], sort_keys=True))
    except install.InstallError as exc:
        checks["installation"] = {"current": False, "error": str(exc)}
        errors.append(str(exc))

    config_path = root / install.CONFIG_REL
    if config_path.is_file():
        try:
            assignments = install.parse_simple_toml_assignments(config_path.read_text(encoding="utf-8"))
            config_checks = {
                "parent_model_sol": assignments.get(("", "model")) == "gpt-5.6-sol",
                "parent_reasoning_max": assignments.get(("", "model_reasoning_effort")) == "max",
                "live_web_search": assignments.get(("", "web_search")) == "live",
                "documented_agents_enabled": assignments.get(("agents", "enabled")) is True,
                "documented_worker_limit": assignments.get(("agents", "max_concurrent_threads_per_session")) == 8,
                "hooks_enabled": assignments.get(("features", "hooks")) is True,
                "multi_agent_enabled": assignments.get(("features", "multi_agent")) is True,
                "no_default_child_model": ("agents", "default_subagent_model") not in assignments,
                "no_default_child_effort": ("agents", "default_subagent_reasoning_effort") not in assignments,
            }
            checks["codex_config"] = config_checks
            failed = [name for name, passed in config_checks.items() if not passed]
            if failed:
                errors.append("Codex config invariant failed: " + ", ".join(failed))
        except (OSError, UnicodeDecodeError, install.InstallError) as exc:
            errors.append(f"invalid Codex config: {exc}")

    agent_checks: Dict[str, Any] = {}
    for role in install.REQUIRED_AGENT_TYPES:
        path = root / install.AGENTS_REL / f"{role}.toml"
        if not path.is_file():
            agent_checks[role] = {"ok": False, "reason": "missing"}
            errors.append(f"missing custom agent .codex/agents/{role}.toml")
            continue
        text = path.read_text(encoding="utf-8")
        fields = all(re.search(rf"(?m)^\s*{field}\s*=", text) for field in ("name", "description", "developer_instructions"))
        dynamic = not re.search(r"(?m)^\s*(model|model_reasoning_effort)\s*=", text)
        agent_checks[role] = {"ok": fields and dynamic, "required_fields": fields, "runtime_route": dynamic}
        if not fields or not dynamic:
            errors.append(f"agent {role} must contain required fields and must not pin model or effort")
    checks["custom_agents"] = agent_checks

    script_checks: Dict[str, Any] = {}
    skill_dir = root / install.SKILL_REL
    for path in sorted((skill_dir / "scripts").glob("*.py")) if (skill_dir / "scripts").is_dir() else []:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 8))
            script_checks[path.name] = {"python_3_8_syntax": True}
        except (SyntaxError, OSError, UnicodeDecodeError) as exc:
            script_checks[path.name] = {"python_3_8_syntax": False, "error": str(exc)}
            errors.append(f"script is not Python 3.8 compatible: {path.name}: {exc}")
    checks["scripts"] = script_checks

    if require_activation:
        try:
            activation = install.validate_activation(root, activation_token)
            checks["activation"] = {"ok": True, "session_id": activation.get("session_id"), "model": activation.get("model")}
        except install.InstallError as exc:
            checks["activation"] = {"ok": False, "error": str(exc)}
            errors.append(str(exc))

    if isinstance(runtime, dict) and runtime.get("codex") is None:
        warnings.append(
            "codex executable is not on PATH; this is acceptable only when the current Codex app already exposes live subagent tools"
        )

    return {
        "ok": not errors,
        "project_root": str(root),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--source-root")
    parser.add_argument("--activation-token")
    parser.add_argument("--require-activation", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = inspect_project(
        pathlib.Path(args.project_root),
        pathlib.Path(args.source_root).expanduser().resolve() if args.source_root else None,
        args.activation_token,
        args.require_activation,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("coordinator doctor: " + ("PASS" if result["ok"] else "FAIL"))
        print("project_root=" + result["project_root"])
        for error in result["errors"]:
            print("ERROR: " + error)
        for warning in result["warnings"]:
            print("WARNING: " + warning)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
