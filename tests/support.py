from __future__ import annotations

import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def committed_source(target: pathlib.Path) -> tuple[pathlib.Path, str]:
    target.mkdir()
    for name in ("skill", "src"):
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("LICENSE", "VERSION"):
        shutil.copy2(ROOT / name, target / name)
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "Coordinator Tests"),
        ("git", "config", "user.email", "tests@example.invalid"),
        ("git", "add", "skill", "src/coordinator", "LICENSE", "VERSION"),
        ("git", "commit", "-qm", "fixture"),
    ):
        subprocess.run(command, cwd=target, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=target, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()
    return target, commit
