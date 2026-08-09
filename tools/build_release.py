#!/usr/bin/env python3
"""Build deterministic Coordinator release assets from one source commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import zipfile
from typing import Any, Sequence

OWNER = "alanhoff/agent-coordinator"
ROLES = ("architect", "designer", "documenter", "fixer", "implementer", "researcher", "reviewer", "validator")
ROLE_PATHS = {role: f"agents/roles/{role}.toml" for role in ROLES}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SKILL_FILES = {
    "SKILL.md", "README.md", "agents/openai.yaml",
    "references/model-routing.md", "references/state-schema.md", "references/workflow-protocol.md", "references/dashboard.md",
    "scripts/coordinator_state.py", "scripts/doctor.py", "scripts/install.py", "scripts/model_router.py", "scripts/dashboard.py",
    "scripts/install.sh", "scripts/install.cmd",
    *ROLE_PATHS.values(),
}
RUNTIME_FILES = {
    "__init__.py",
    "cli/__init__.py", "cli/state.py", "cli/routing.py", "cli/dashboard.py", "cli/doctor.py", "cli/outcome.py",
    "dashboard/__init__.py", "dashboard/view.py",
    "install/__init__.py", "install/standalone.py", "install/doctor.py",
    "routing/__init__.py", "routing/selector.py",
    "state/__init__.py", "state/store.py",
}
CANONICAL_FILES = {
    ".coordinator-package.json", "LICENSE", "VERSION", *SKILL_FILES,
    *("scripts/lib/coordinator/" + path for path in RUNTIME_FILES),
}


def _json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: pathlib.Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if result.returncode:
        raise RuntimeError("unable to verify the source Git tree")
    return result.stdout.strip()


def _git_blob(root: pathlib.Path, commit: str, relative: str) -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "blob", f"{commit}:{relative}"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("unable to read a release input from the source commit")
    return result.stdout


def _commit(root: pathlib.Path, supplied: str | None) -> str:
    if pathlib.Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise RuntimeError("release root must be the Git worktree root")
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").lower()
    value = supplied if supplied else head
    if not COMMIT_RE.fullmatch(value):
        raise ValueError("source commit must be a full lowercase Git object ID")
    resolved = _git(root, "rev-parse", "--verify", value + "^{commit}").lower()
    if resolved != value:
        raise RuntimeError("source commit does not resolve to the intended commit object")
    if value != head:
        raise RuntimeError("source commit must match the checked-out HEAD")
    drift = _git(
        root,
        "status", "--porcelain=v1", "--untracked-files=all", "--",
        "skill", "src/coordinator", "LICENSE", "VERSION",
    )
    if drift:
        raise RuntimeError("release inputs differ from the checked-out source commit")
    tracked: set[str] = set()
    tree = _git(
        root,
        "ls-tree", "-r", value, "--",
        "skill", "src/coordinator", "LICENSE", "VERSION",
    )
    for entry in tree.splitlines():
        metadata, separator, relative = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[0] not in ("100644", "100755") or fields[1] != "blob":
            raise RuntimeError("source commit contains an unsafe release input")
        tracked.add(relative)
    expected = {"LICENSE", "VERSION", *("skill/" + path for path in SKILL_FILES), *("src/coordinator/" + path for path in RUNTIME_FILES)}
    if tracked != expected:
        raise RuntimeError("source commit does not contain the canonical release input inventory")
    return value


def _files(root: pathlib.Path, version: str, source_commit: str) -> tuple[dict[str, bytes], dict[str, str]]:
    files: dict[str, bytes] = {}
    modes: dict[str, str] = {}

    def add(relative: str, source: str, executable: bool = False) -> None:
        if relative in files:
            raise RuntimeError(f"duplicate package path: {relative}")
        files[relative] = _git_blob(root, source_commit, source)
        modes[relative] = "executable" if executable else "file"

    for relative in sorted(SKILL_FILES):
        add(relative, "skill/" + relative, relative.startswith("scripts/"))
    add("LICENSE", "LICENSE")
    add("VERSION", "VERSION")
    for relative in sorted(RUNTIME_FILES):
        add("scripts/lib/coordinator/" + relative, "src/coordinator/" + relative)
    marker = {
        "schema_version": 1,
        "name": "coordinator",
        "version": version,
        "source_commit": source_commit,
    }
    files[".coordinator-package.json"] = _json(marker)
    modes[".coordinator-package.json"] = "file"
    if set(files) != CANONICAL_FILES:
        raise RuntimeError("assembled package inventory is not canonical")
    return files, modes


def build(root: pathlib.Path, output: pathlib.Path, source_commit: str | None = None) -> dict[str, str]:
    root = root.resolve()
    output = output.expanduser().absolute()
    if output.is_symlink():
        raise RuntimeError("release output must not be a symlink")
    commit = _commit(root, source_commit)
    version = _git_blob(root, commit, "VERSION").decode("utf-8").strip()
    source_version = re.search(
        r'^VERSION = "([^"]+)"$', _git_blob(root, commit, "src/coordinator/__init__.py").decode("utf-8"), re.MULTILINE
    )
    installer_version = re.search(
        r'^VERSION = "([^"]+)"$',
        _git_blob(root, commit, "src/coordinator/install/standalone.py").decode("utf-8"),
        re.MULTILINE,
    )
    if (
        not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
        or not source_version
        or not installer_version
        or source_version.group(1) != version
        or installer_version.group(1) != version
    ):
        raise RuntimeError("root and source versions must agree on SemVer")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError("release output directory must be empty")
    files, modes = _files(root, version, commit)
    manifest = {
        "schema_version": 1,
        "name": "coordinator",
        "owner": OWNER,
        "version": version,
        "source_commit": commit,
        "files": {
            relative: {"sha256": _sha(data), "size": len(data), "mode": modes[relative]}
            for relative, data in sorted(files.items())
        },
        "roles": ROLE_PATHS,
    }
    manifest_bytes = _json(manifest)
    archive_path = output / f"coordinator-{version}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=True) as archive:
        archive.comment = b""
        for relative, data in sorted({**files, "manifest.json": manifest_bytes}.items()):
            info = zipfile.ZipInfo("coordinator/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            logical = "file" if relative == "manifest.json" else modes[relative]
            permission = 0o755 if logical == "executable" else 0o644
            info.external_attr = (stat.S_IFREG | permission) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    zip_bytes = archive_path.read_bytes()
    latest = output / "coordinator-latest.zip"
    latest.write_bytes(zip_bytes)
    standalone = files["scripts/lib/coordinator/install/standalone.py"]
    (output / "install.py").write_bytes(standalone)
    (output / "manifest.json").write_bytes(manifest_bytes)
    payloads = {
        archive_path.name: zip_bytes,
        latest.name: zip_bytes,
        "install.py": standalone,
        "manifest.json": manifest_bytes,
    }
    sums = "".join(f"{_sha(payloads[name])}  {name}\n" for name in sorted(payloads))
    (output / "SHA256SUMS").write_text(sums, encoding="ascii", newline="\n")
    for asset in (archive_path, latest, output / "manifest.json", output / "SHA256SUMS"):
        os.chmod(asset, 0o644)
    os.chmod(output / "install.py", 0o755)
    return {name: _sha(data) for name, data in payloads.items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit")
    args = parser.parse_args(argv)
    result = build(pathlib.Path(args.root), pathlib.Path(args.output), args.source_commit)
    print(json.dumps({"status": "built", "assets": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
