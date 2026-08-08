#!/usr/bin/env python3
"""Independently verify Coordinator release assets and reconstructed inventory."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import re
import stat
import zipfile
from typing import Any, Sequence

ROLES = ("architect", "designer", "documenter", "fixer", "implementer", "researcher", "reviewer", "validator")
ROLE_PATHS = {role: f"agents/roles/{role}.toml" for role in ROLES}
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


class VerificationError(RuntimeError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: bytes, name: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise VerificationError(f"{name} duplicates key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{name} is not valid UTF-8 JSON") from exc


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise VerificationError("manifest or archive contains an unsafe path")
    parts = pathlib.PurePosixPath(value).parts
    if any(part in ("", ".", "..") for part in parts) or pathlib.PurePosixPath(value).as_posix() != value:
        raise VerificationError("manifest or archive contains an unsafe path")
    return value


def verify(directory: pathlib.Path, expected_commit: str | None = None) -> dict[str, Any]:
    directory = directory.expanduser().absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise VerificationError("release directory is unsafe")
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = _json(manifest_bytes, "manifest")
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise VerificationError("manifest version is invalid")
    expected_assets = {f"coordinator-{version}.zip", "coordinator-latest.zip", "install.py", "manifest.json", "SHA256SUMS"}
    children = list(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in children):
        raise VerificationError("release directory contains a non-asset entry")
    actual_assets = {path.name for path in children}
    if actual_assets != expected_assets:
        raise VerificationError(f"release asset set differs: {sorted(actual_assets)}")
    sums: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if not match or match.group(2) in sums:
            raise VerificationError("SHA256SUMS has malformed or duplicate entries")
        sums[match.group(2)] = match.group(1)
    if set(sums) != expected_assets - {"SHA256SUMS"}:
        raise VerificationError("SHA256SUMS does not cover the exact four payloads")
    for name, digest in sums.items():
        if _sha((directory / name).read_bytes()) != digest:
            raise VerificationError(f"asset checksum mismatch: {name}")
    versioned = (directory / f"coordinator-{version}.zip").read_bytes()
    if versioned != (directory / "coordinator-latest.zip").read_bytes():
        raise VerificationError("latest and versioned ZIPs are not byte-identical")
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "name", "owner", "version", "source_commit", "files", "roles"}:
        raise VerificationError("manifest is not the closed schema")
    if manifest["schema_version"] != 1 or manifest["name"] != "coordinator" or manifest["owner"] != "alanhoff/agent-coordinator" or manifest["roles"] != ROLE_PATHS:
        raise VerificationError("manifest identity or roles are invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"]):
        raise VerificationError("manifest source commit is invalid")
    if expected_commit and manifest["source_commit"] != expected_commit:
        raise VerificationError("manifest is bound to another source commit")
    if not isinstance(manifest["files"], dict) or "manifest.json" in manifest["files"]:
        raise VerificationError("manifest inventory is invalid")
    if set(manifest["files"]) != CANONICAL_FILES:
        raise VerificationError("manifest inventory differs from the canonical v3 runtime and layout")
    for relative in manifest["files"]:
        _safe_path(relative)

    end = versioned.rfind(b"PK\x05\x06")
    if not versioned.startswith(b"PK\x03\x04") or end < 0 or end + 22 > len(versioned):
        raise VerificationError("archive framing is invalid")
    if end + 22 + int.from_bytes(versioned[end + 20 : end + 22], "little") != len(versioned):
        raise VerificationError("archive has leading or trailing data")

    with zipfile.ZipFile(io.BytesIO(versioned)) as archive:
        names: set[str] = set()
        files: dict[str, bytes] = {}
        modes: dict[str, str] = {}
        for item in archive.infolist():
            if item.is_dir():
                raise VerificationError("archive contains an unlisted directory entry")
            if "\\" in item.filename or item.filename.startswith("/"):
                raise VerificationError("archive contains an unsafe path")
            parts = pathlib.PurePosixPath(item.filename).parts
            if not parts or parts[0] != "coordinator" or any(part in ("", ".", "..") for part in parts):
                raise VerificationError("archive contains an unsafe path")
            relative = pathlib.PurePosixPath(*parts[1:]).as_posix()
            _safe_path(relative)
            if relative in names:
                raise VerificationError("archive contains a duplicate path")
            names.add(relative)
            unix_mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(unix_mode) != stat.S_IFREG:
                raise VerificationError("archive entry is not a regular file")
            files[relative] = archive.read(item)
            modes[relative] = "executable" if unix_mode & 0o111 else "file"
    if files.pop("manifest.json", None) != manifest_bytes:
        raise VerificationError("standalone and ZIP-root manifests are not byte-identical")
    if modes.pop("manifest.json", None) != "file":
        raise VerificationError("ZIP-root manifest mode is invalid")
    if set(files) != set(manifest["files"]):
        raise VerificationError("reconstructed non-manifest inventory differs")
    for relative, spec in manifest["files"].items():
        if not isinstance(spec, dict) or set(spec) != {"sha256", "size", "mode"}:
            raise VerificationError(f"invalid manifest entry: {relative}")
        if (
            not isinstance(spec["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])
            or not isinstance(spec["size"], int)
            or isinstance(spec["size"], bool)
            or spec["size"] < 0
            or spec["mode"] not in ("file", "executable")
        ):
            raise VerificationError(f"invalid manifest entry: {relative}")
        if len(files[relative]) != spec["size"] or _sha(files[relative]) != spec["sha256"] or modes[relative] != spec["mode"]:
            raise VerificationError(f"content or mode mismatch: {relative}")
    marker = _json(files[".coordinator-package.json"], "package marker")
    if marker != {"schema_version": 1, "name": "coordinator", "version": version, "source_commit": manifest["source_commit"]}:
        raise VerificationError("package marker disagrees with manifest")
    if files["VERSION"].decode("utf-8").strip() != version:
        raise VerificationError("VERSION disagrees with manifest")
    if files["scripts/lib/coordinator/install/standalone.py"] != (directory / "install.py").read_bytes():
        raise VerificationError("standalone installer is not the install owner source")
    return {
        "version": version,
        "source_commit": manifest["source_commit"],
        "files": len(files),
        "assets": sorted(expected_assets),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--source-commit")
    args = parser.parse_args(argv)
    try:
        result = verify(pathlib.Path(args.release_dir), args.source_commit)
    except (OSError, UnicodeError, zipfile.BadZipFile, VerificationError) as exc:
        print(json.dumps({"status": "failed", "code": "verification_failed", "message": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"status": "verified", "data": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
