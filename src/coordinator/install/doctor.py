"""Read-only installation, runtime, repository, privacy, and state diagnostics."""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import sys
from typing import Any

from coordinator.install import standalone
from coordinator.state.store import StateStore


def check(repo: pathlib.Path | None = None) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checks: dict[str, Any] = {}
    checks["python"] = {"ok": sys.version_info >= (3, 11), "version": ".".join(map(str, sys.version_info[:3]))}
    if not checks["python"]["ok"]:
        errors.append({"code": "unsupported_python", "message": "Python 3.11 or newer is required"})

    git = shutil.which("git")
    checks["git"] = {"ok": git is not None, "path": git}
    if git is None:
        errors.append({"code": "git_missing", "message": "Git is required for repository orchestration"})
    if repo is not None:
        resolved = repo.expanduser().resolve()
        if git:
            try:
                result = subprocess.run(
                    [git, "-C", str(resolved), "rev-parse", "--show-toplevel"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                ready = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                result = None
                ready = False
            checks["repository"] = {"ok": ready, "path": str(resolved), "root": result.stdout.strip() if ready and result else None}
            if not ready:
                errors.append({"code": "repository_not_ready", "message": "selected repository is not a readable Git worktree"})

    installation = standalone.inspect()
    checks["installation"] = installation
    if not installation["current"]:
        errors.append({"code": "install_drift", "message": "global Coordinator installation has drift"})

    codex = shutil.which("codex")
    if codex:
        try:
            result = subprocess.run(
                [codex, "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            help_text = (result.stdout + "\n" + result.stderr).casefold()
            capability = result.returncode == 0 and ("agent" in help_text or "thread" in help_text)
            checks["codex"] = {"available": True, "help_ok": result.returncode == 0, "agent_interface_visible": capability}
            if not capability:
                warnings.append({"code": "codex_capability_unconfirmed", "message": "Codex help did not expose a recognizable agent/thread interface"})
        except (OSError, subprocess.TimeoutExpired):
            checks["codex"] = {"available": True, "help_ok": False, "agent_interface_visible": False}
            warnings.append({"code": "codex_capability_unconfirmed", "message": "Codex capability discovery did not complete"})
    else:
        checks["codex"] = {"available": False, "help_ok": False, "agent_interface_visible": False}
        warnings.append({"code": "codex_not_on_path", "message": "Codex is not on PATH; capability discovery is unavailable"})

    owned = standalone.paths()
    privacy: dict[str, Any] = {}
    for label, path in (("control", owned.control), ("metadata", owned.metadata)):
        if not path.exists():
            privacy[label] = {"exists": False, "private": False}
            continue
        info = path.lstat()
        private = not stat.S_ISLNK(info.st_mode) and (
            os.name == "nt" or (info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0)
        )
        privacy[label] = {"exists": True, "private": private}
        if not private:
            errors.append({"code": "unsafe_permissions", "message": f"Coordinator {label} is not current-user private"})
    checks["privacy"] = privacy

    state_records = {"valid": 0, "invalid": 0, "diagnostics": []}
    for _path, state, error in StateStore().iter_records():
        if state is not None:
            state_records["valid"] += 1
        else:
            state_records["invalid"] += 1
            state_records["diagnostics"].append({"code": error.code if error else "invalid_state"})
    checks["workflow_state"] = state_records
    if state_records["invalid"]:
        errors.append({"code": "invalid_workflow_state", "message": "one or more workflow states failed strict validation"})
    try:
        recovery_code, recovery = standalone.recovery_status()
        checks["recovery"] = recovery
        if recovery_code:
            errors.append({"code": "recovery_pending", "message": "an install transaction requires recovery"})
    except standalone.InstallError as exc:
        checks["recovery"] = {"pending": True, "code": exc.code}
        errors.append({"code": exc.code, "message": "install recovery evidence is invalid"})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}
