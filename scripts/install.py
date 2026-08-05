#!/usr/bin/env python3
"""Portable bootstrap, update, restart gate, and project installer for coordinator.

The script is intentionally self-contained and uses only Python's standard
library plus Git. It runs on Python 3.8+ on Linux, macOS, BusyBox-oriented
systems, and Windows 11 with CMD.exe.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

NAME = "coordinator"
INSTALL_SCHEMA_VERSION = 2
MANIFEST_SCHEMA_VERSION = 1
RETENTION_DAYS = 5
ACTIVATION_TTL_HOURS = 24
MIN_PYTHON = (3, 8)
PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_REL = pathlib.Path(".agents") / "skills" / NAME
CONFIG_REL = pathlib.Path(".codex") / "config.toml"
HOOKS_REL = pathlib.Path(".codex") / "hooks.json"
METADATA_REL = pathlib.Path(".codex") / "coordinator-install.json"
AGENTS_REL = pathlib.Path(".codex") / "agents"
HOOK_MARKER = "Coordinator activation gate"
MANAGED_MARKER_PREFIX = "coordinator managed"
REQUIRED_AGENT_TYPES: Tuple[str, ...] = (
    "researcher",
    "designer",
    "architect",
    "documenter",
    "implementer",
    "reviewer",
    "fixer",
    "validator",
)

ROOT_CONFIG: Mapping[str, Any] = {
    "model": "gpt-5.6-sol",
    "model_reasoning_effort": "max",
    "web_search": "live",
}
AGENTS_CONFIG: Mapping[str, Any] = {
    "enabled": True,
    "interrupt_message": True,
    # V1/current documented semantics: spawned threads, excluding the parent.
    "max_concurrent_threads_per_session": 8,
}
FEATURES_CONFIG: Mapping[str, Any] = {
    "hooks": True,
    "multi_agent": True,
}
TARGET_CONFIG: Mapping[str, Mapping[str, Any]] = {
    "": ROOT_CONFIG,
    "agents": AGENTS_CONFIG,
    "features": FEATURES_CONFIG,
}

EXIT_RESTART_REQUIRED = 10
EXIT_ACTIVATION_REQUIRED = 12
EXIT_BLOCKED = 20


class InstallError(RuntimeError):
    """An actionable bootstrap blocker."""

    def __init__(self, message: str, *, code: str = "blocked", examples: Optional[List[str]] = None):
        super().__init__(message)
        self.code = code
        self.examples = examples or []


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def parse_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def print_payload(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(value, (list, dict)):
                print(f"{key}={json.dumps(value, sort_keys=True)}")
            else:
                print(f"{key}={value}")
    else:
        print(payload)


def _is_under(child: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def temp_root() -> pathlib.Path:
    system_temp = pathlib.Path(tempfile.gettempdir()).resolve()
    override = os.environ.get("COORDINATOR_TMP_ROOT")
    root = pathlib.Path(override).expanduser().resolve() if override else system_temp / "codex-coordinator"
    if not _is_under(root, system_temp):
        raise InstallError(
            f"COORDINATOR_TMP_ROOT must remain below the operating-system temporary directory ({system_temp})",
            code="invalid_temp_root",
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_stale(root: pathlib.Path, now: Optional[float] = None) -> List[str]:
    """Delete every Coordinator-owned temporary entry older than five days."""
    current = time.time() if now is None else now
    cutoff = current - RETENTION_DAYS * 24 * 60 * 60
    removed: List[str] = []

    # An old top-level artifact is removed as a unit. Recent containers are
    # then scanned recursively so old tokens/cache entries cannot survive just
    # because a sibling was created recently.
    for child in list(root.iterdir()):
        try:
            modified = child.lstat().st_mtime
        except FileNotFoundError:
            continue
        if modified >= cutoff:
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(str(child))
            else:
                child.unlink()
            removed.append(str(child))
        except FileNotFoundError:
            pass

    descendants = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for child in descendants:
        try:
            modified = child.lstat().st_mtime
        except FileNotFoundError:
            continue
        try:
            if child.is_symlink() or child.is_file():
                if modified < cutoff:
                    child.unlink()
                    removed.append(str(child))
            elif child.is_dir() and modified < cutoff:
                try:
                    child.rmdir()
                except OSError:
                    pass
                else:
                    removed.append(str(child))
        except FileNotFoundError:
            pass
    return sorted(set(removed))


@contextlib.contextmanager
def install_lock(root: pathlib.Path, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Cross-platform lock using an atomic directory, with stale recovery."""
    lock = root / "install.lock"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock.mkdir()
            (lock / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "created_at": iso_now()}), encoding="utf-8"
            )
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > 10 * 60:
                shutil.rmtree(str(lock), ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise InstallError(
                    "another coordinator installation is still running or left a fresh lock",
                    code="install_locked",
                    examples=[f"Remove {lock} only after confirming no installer process is active."],
                )
            time.sleep(0.1)
    try:
        yield
    finally:
        shutil.rmtree(str(lock), ignore_errors=True)


def atomic_write(path: pathlib.Path, data: bytes, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
        if executable and os.name != "nt":
            path.chmod(path.stat().st_mode | 0o111)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def run_command(
    arguments: Sequence[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        list(arguments),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise InstallError(
            f"command failed ({completed.returncode}): {' '.join(arguments)}: {detail}",
            code="command_failed",
        )
    return completed


def require_runtime_dependencies(require_git: bool = True) -> Dict[str, Any]:
    blockers: List[str] = []
    if sys.version_info < MIN_PYTHON:
        blockers.append(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; active interpreter is {sys.version.split()[0]}"
        )
    git = shutil.which("git")
    if require_git and git is None:
        blockers.append("Git is required because project bootstrap and every completed task must be committed")
    if blockers:
        raise InstallError(
            "; ".join(blockers),
            code="missing_dependency",
            examples=[
                "Linux/macOS: put python3 and git on PATH.",
                "Windows 11 CMD: verify `python --version` and `git --version` both succeed.",
            ],
        )
    return {
        "python": sys.version.split()[0],
        "platform": "windows" if os.name == "nt" else sys.platform,
        "git": git,
        "codex": shutil.which("codex"),
    }


def normalize_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InstallError(f"source URL must be an absolute http(s) URL: {url}", code="invalid_source")
    if parsed.query or parsed.fragment:
        raise InstallError("source package base URL cannot contain a query or fragment", code="invalid_source")
    return cleaned


def version_tuple(value: str) -> Tuple[int, ...]:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)*)\s*", value)
    if not match:
        raise InstallError(f"unsupported coordinator version format: {value!r}", code="invalid_manifest")
    return tuple(int(part) for part in match.group(1).split("."))


class PackageSource:
    kind = "abstract"

    def __init__(self, manifest: Mapping[str, Any], manifest_bytes: bytes, label: str):
        self.manifest = dict(manifest)
        self.manifest_bytes = manifest_bytes
        self.label = label
        validate_manifest(self.manifest)

    @property
    def version(self) -> str:
        return str(self.manifest["version"])

    def read_bytes(self, relative: str) -> bytes:
        raise NotImplementedError

    def verify_bytes(self, relative: str, data: bytes) -> None:
        spec = self.manifest.get("files", {}).get(relative)
        if spec is None:
            if relative != "manifest.json":
                raise InstallError(f"manifest has no hash record for {relative}", code="invalid_manifest")
            return
        expected = str(spec["sha256"])
        actual = sha256_bytes(data)
        if actual != expected:
            raise InstallError(
                f"source file hash mismatch for {relative}: expected {expected}, got {actual}",
                code="source_corrupt",
            )


class LocalPackageSource(PackageSource):
    kind = "local"

    def __init__(self, root: pathlib.Path):
        self.root = root.expanduser().resolve()
        manifest_path = self.root / "manifest.json"
        if not manifest_path.is_file():
            raise InstallError(f"coordinator manifest not found: {manifest_path}", code="invalid_source")
        raw = manifest_path.read_bytes()
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError(f"invalid coordinator manifest: {manifest_path}: {exc}", code="invalid_manifest") from exc
        super().__init__(manifest, raw, str(self.root))
        # A local source participates in automatic version selection only when
        # every installable file exists and matches its manifest. This keeps a
        # drifted project copy from outranking a healthy global copy at the
        # same version.
        required = list(self.manifest["project_skill_files"]) + list(
            self.manifest["project_agent_files"].values()
        )
        for relative in dict.fromkeys(required):
            self.read_bytes(str(relative))

    def read_bytes(self, relative: str) -> bytes:
        if relative == "manifest.json":
            return self.manifest_bytes
        path = (self.root / pathlib.PurePosixPath(relative)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise InstallError(f"manifest path escapes package root: {relative}", code="invalid_manifest") from exc
        if not path.is_file():
            raise InstallError(f"source package is missing {relative}", code="source_incomplete")
        data = path.read_bytes()
        self.verify_bytes(relative, data)
        return data


class RemotePackageSource(PackageSource):
    kind = "remote"

    def __init__(self, base_url: str, cache_root: pathlib.Path):
        self.base_url = normalize_url(base_url)
        self.cache_root = cache_root / ("download-" + sha256_bytes(self.base_url.encode("utf-8"))[:16])
        self.cache_root.mkdir(parents=True, exist_ok=True)
        raw = self._download("manifest.json")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError(f"remote manifest is not valid JSON: {exc}", code="invalid_manifest") from exc
        super().__init__(manifest, raw, self.base_url)

    def _download(self, relative: str) -> bytes:
        quoted = "/".join(urllib.parse.quote(part) for part in pathlib.PurePosixPath(relative).parts)
        url = self.base_url + "/" + quoted
        last_error: Optional[BaseException] = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "coordinator-installer/2"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        raise InstallError(f"failed to download {url}: {last_error}", code="download_failed")

    def read_bytes(self, relative: str) -> bytes:
        if relative == "manifest.json":
            return self.manifest_bytes
        cache = self.cache_root / pathlib.PurePosixPath(relative)
        if cache.is_file():
            data = cache.read_bytes()
            try:
                self.verify_bytes(relative, data)
                return data
            except InstallError:
                with contextlib.suppress(FileNotFoundError):
                    cache.unlink()
        data = self._download(relative)
        self.verify_bytes(relative, data)
        atomic_write(cache, data)
        return data


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise InstallError("unsupported or missing manifest schema_version", code="invalid_manifest")
    if manifest.get("name") != NAME:
        raise InstallError("manifest name is not coordinator", code="invalid_manifest")
    version_tuple(str(manifest.get("version", "")))
    files = manifest.get("files")
    project_files = manifest.get("project_skill_files")
    agent_files = manifest.get("project_agent_files")
    if not isinstance(files, dict) or not isinstance(project_files, list) or not isinstance(agent_files, dict):
        raise InstallError("manifest files/project_skill_files/project_agent_files have invalid types", code="invalid_manifest")
    all_paths = list(project_files) + list(agent_files.values())
    for relative in all_paths:
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise InstallError(f"invalid manifest path: {relative!r}", code="invalid_manifest")
        path = pathlib.PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise InstallError(f"unsafe manifest path: {relative!r}", code="invalid_manifest")
        if relative != "manifest.json" and relative not in files:
            raise InstallError(f"missing file hash for {relative}", code="invalid_manifest")
    if set(agent_files) != set(REQUIRED_AGENT_TYPES):
        raise InstallError("manifest project_agent_files must define every coordinator role", code="invalid_manifest")
    for relative, spec in files.items():
        if not isinstance(spec, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("sha256", ""))):
            raise InstallError(f"invalid hash metadata for {relative}", code="invalid_manifest")


def select_source(
    project_root: pathlib.Path,
    cache_root: pathlib.Path,
    explicit_path: Optional[str],
    explicit_url: Optional[str],
) -> PackageSource:
    if explicit_path and explicit_url:
        raise InstallError("use either --source or --source-url, not both", code="invalid_source")
    if explicit_url:
        return RemotePackageSource(explicit_url, cache_root)
    if explicit_path:
        return LocalPackageSource(pathlib.Path(explicit_path))

    candidates: List[Tuple[LocalPackageSource, int]] = []
    declared_versions: List[Tuple[Tuple[int, ...], str]] = []
    project_source = (project_root / SKILL_REL).expanduser().resolve()
    package_root = PACKAGE_ROOT.expanduser().resolve()
    package_priority = 10 if package_root == project_source else 30
    raw_candidates = [
        # A separately loaded package is the strongest same-version source.
        # When Codex loaded the project copy itself, treat it as project data,
        # not as an independent authoritative source.
        (package_root, package_priority),
        # Prefer the global package over a same-version project copy so local
        # drift is repaired rather than treated as the source of truth.
        (pathlib.Path.home() / ".agents" / "skills" / NAME, 20),
        (project_source, 10),
    ]
    seen: set = set()
    for path, priority in raw_candidates:
        resolved = path.expanduser().resolve()
        if str(resolved) in seen:
            continue
        seen.add(str(resolved))
        manifest_path = resolved / "manifest.json"
        if manifest_path.is_file():
            try:
                declared = json.loads(manifest_path.read_text(encoding="utf-8"))
                if declared.get("name") == NAME:
                    declared_versions.append((version_tuple(str(declared.get("version", ""))), str(resolved)))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, InstallError):
                pass
        try:
            candidates.append((LocalPackageSource(resolved), priority))
        except InstallError:
            continue
    if not candidates:
        raise InstallError(
            "no complete coordinator package source was found",
            code="source_missing",
            examples=[
                "Install the package under ~/.agents/skills/coordinator.",
                "Pass --source /path/to/coordinator or --source-url https://host/skills/coordinator.",
            ],
        )
    candidates.sort(
        key=lambda item: (version_tuple(item[0].version), item[1], item[0].label),
        reverse=True,
    )
    selected = candidates[0][0]
    selected_version = version_tuple(selected.version)
    higher_broken = [
        {"version": ".".join(str(part) for part in version), "path": path}
        for version, path in declared_versions
        if version > selected_version
    ]
    if higher_broken:
        raise InstallError(
            "a newer local coordinator package is declared but incomplete or hash-invalid; refusing an automatic downgrade: "
            + json.dumps(higher_broken, sort_keys=True),
            code="source_downgrade_blocked",
            examples=["Replace the newer package with a complete copy or pass an equal/newer --source or --source-url."],
        )
    return selected


def git_output(git: str, root: pathlib.Path, arguments: Sequence[str], check: bool = True) -> str:
    completed = run_command([git, "-C", str(root), *arguments])
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise InstallError(f"git {' '.join(arguments)} failed: {detail}", code="git_failed")
    return completed.stdout.strip()


def require_git_identity(git: str, root: pathlib.Path) -> Dict[str, str]:
    name = git_output(git, root, ["config", "--get", "user.name"], check=False).strip()
    email = git_output(git, root, ["config", "--get", "user.email"], check=False).strip()
    if not name or not email:
        raise InstallError(
            "Git author identity is missing; coordinator cannot create its mandatory installation commit",
            code="git_identity_missing",
            examples=[
                'Project-only: git config user.name "Your Name" && git config user.email "you@example.com"',
                'Global: git config --global user.name "Your Name" && git config --global user.email "you@example.com"',
            ],
        )
    return {"name": name, "email": email}


def resolve_project(input_path: pathlib.Path, git: str, initialize: bool) -> Tuple[pathlib.Path, bool]:
    requested = input_path.expanduser().resolve()
    if not requested.exists():
        requested.mkdir(parents=True)
    if not requested.is_dir():
        raise InstallError(f"project path is not a directory: {requested}", code="invalid_project")
    completed = run_command([git, "-C", str(requested), "rev-parse", "--show-toplevel"])
    if completed.returncode == 0:
        return pathlib.Path(completed.stdout.strip()).resolve(), False
    if not initialize:
        raise InstallError(f"project is not a Git worktree: {requested}", code="git_repository_missing")
    require_git_identity(git, requested)
    init = run_command([git, "-C", str(requested), "init"])
    if init.returncode != 0:
        raise InstallError(
            f"git init failed in {requested}: {(init.stderr or init.stdout).strip()}",
            code="git_init_failed",
        )
    completed = run_command([git, "-C", str(requested), "rev-parse", "--show-toplevel"], check=True)
    return pathlib.Path(completed.stdout.strip()).resolve(), True


def toml_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"unsupported TOML value: {value!r}")


TABLE_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
ARRAY_TABLE_RE = re.compile(r"^\s*\[\[([^\[\]]+)\]\]\s*(?:#.*)?$")
ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=.*$")
ASSIGNMENT_LHS_RE = re.compile(r"^\s*([^=]+?)\s*=")
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def parse_toml_key_path(raw: str) -> Optional[List[str]]:
    """Parse the small TOML key-path subset needed for collision detection.

    Bare, single-quoted, and double-quoted key components are supported. The
    installer does not rewrite nontrivial target-key forms; it detects them and
    blocks so Python 3.8 behaves as safely as runtimes that provide tomllib.
    """
    value = raw.strip()
    if not value:
        return None
    components: List[str] = []
    index = 0
    length = len(value)
    while index < length:
        while index < length and value[index].isspace():
            index += 1
        if index >= length:
            return None
        if value[index] in {'"', "'"}:
            quote = value[index]
            start = index
            index += 1
            escaped = False
            while index < length:
                char = value[index]
                if quote == '"' and char == "\\" and not escaped:
                    escaped = True
                    index += 1
                    continue
                if char == quote and not escaped:
                    break
                escaped = False
                index += 1
            if index >= length or value[index] != quote:
                return None
            token = value[start : index + 1]
            try:
                component = json.loads(token) if quote == '"' else token[1:-1]
            except (TypeError, json.JSONDecodeError):
                return None
            index += 1
        else:
            match = BARE_KEY_RE.match(value, index)
            if not match:
                return None
            component = match.group(0)
            index = match.end()
        components.append(str(component))
        while index < length and value[index].isspace():
            index += 1
        if index == length:
            break
        if value[index] != ".":
            return None
        index += 1
    return components or None


def validate_merge_safety(existing: str) -> None:
    """Reject TOML forms that a Python-3.8-safe surgical merge cannot own."""
    lines = strip_managed_lines(existing.splitlines())
    current_section: List[str] = []
    for line_number, original in enumerate(lines, start=1):
        stripped = original.strip()
        if not stripped or stripped.startswith("#"):
            continue
        array_match = ARRAY_TABLE_RE.match(original)
        if array_match:
            path = parse_toml_key_path(array_match.group(1))
            if path in (["agents"], ["features"]):
                raise InstallError(
                    f"target array table [[{array_match.group(1).strip()}]] at line {line_number} "
                    "cannot coexist with Coordinator's scalar table",
                    code="config_ambiguous",
                    examples=["Convert the target array table to a normal [agents] or [features] table."],
                )
            current_section = path or [f"__unknown_array_line_{line_number}"]
            continue
        table_match = TABLE_RE.match(original)
        if table_match:
            raw_section = table_match.group(1).strip()
            path = parse_toml_key_path(raw_section)
            if path in (["agents"], ["features"]) and raw_section not in {"agents", "features"}:
                raise InstallError(
                    f"quoted or noncanonical target table [{raw_section}] at line {line_number} "
                    "cannot be merged safely",
                    code="config_ambiguous",
                    examples=[f"Rename it to [{path[0]}], then rerun the installer."],
                )
            current_section = path or [f"__unknown_table_line_{line_number}"]
            continue
        assignment = ASSIGNMENT_LHS_RE.match(original)
        if not assignment:
            continue
        raw_key = assignment.group(1).strip()
        path = parse_toml_key_path(raw_key)
        if not path:
            continue
        first = path[0]
        if not current_section:
            if first in ROOT_CONFIG and (len(path) != 1 or raw_key != first):
                raise InstallError(
                    f"noncanonical or dotted managed root key {raw_key!r} at line {line_number} "
                    "cannot be merged safely",
                    code="config_ambiguous",
                    examples=[f"Use the ordinary bare key `{first} = ...` or remove it before installation."],
                )
            if first in {"agents", "features"}:
                raise InstallError(
                    f"root assignment {raw_key!r} at line {line_number} conflicts with Coordinator's [{first}] table",
                    code="config_ambiguous",
                    examples=[f"Convert it to a normal [{first}] table, then rerun."],
                )
        elif current_section in (["agents"], ["features"]):
            section = current_section[0]
            managed = set(TARGET_CONFIG[section])
            if section == "features":
                managed.add("multi_agent_v2")
            if first in managed and (len(path) != 1 or raw_key != first):
                raise InstallError(
                    f"noncanonical or dotted managed key {raw_key!r} in [{section}] at line {line_number} "
                    "cannot be merged safely",
                    code="config_ambiguous",
                    examples=[f"Use the ordinary bare key `{first} = ...` or remove it before installation."],
                )


def strip_managed_lines(lines: List[str]) -> List[str]:
    output: List[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# BEGIN coordinator managed"):
            skipping = True
            continue
        if stripped.startswith("# END coordinator managed"):
            skipping = False
            continue
        if not skipping:
            output.append(line)
    if skipping:
        raise InstallError("unterminated coordinator managed block in .codex/config.toml", code="config_ambiguous")
    return output


def split_toml_blocks(text: str) -> List[Tuple[str, Optional[str], List[str]]]:
    if "\"\"\"" in text or "'''" in text:
        raise InstallError(
            "cannot safely rewrite a config containing multiline TOML strings",
            code="config_ambiguous",
            examples=["Move multiline values to a separate file or convert them to ordinary quoted strings, then rerun."],
        )
    lines = strip_managed_lines(text.splitlines())
    blocks: List[Tuple[str, Optional[str], List[str]]] = [("", None, [])]
    array_index = 0
    for line in lines:
        array_match = ARRAY_TABLE_RE.match(line)
        if array_match:
            array_index += 1
            blocks.append((f"__array__:{array_index}:{array_match.group(1).strip()}", line, []))
            continue
        match = TABLE_RE.match(line)
        if match:
            section = match.group(1).strip()
            blocks.append((section, line, []))
        else:
            blocks[-1][2].append(line)
    return blocks


def merge_config(existing: str) -> str:
    validate_merge_safety(existing)
    blocks = split_toml_blocks(existing)
    seen_targets: Dict[str, int] = {}
    output_blocks: List[Tuple[str, Optional[str], List[str]]] = []
    for section, header, lines in blocks:
        if section in TARGET_CONFIG:
            seen_targets[section] = seen_targets.get(section, 0) + 1
            if seen_targets[section] > 1:
                raise InstallError(f"duplicate [{section}] table prevents safe config merge", code="config_ambiguous")
            targets = set(TARGET_CONFIG[section])
            cleaned: List[str] = []
            for line in lines:
                match = ASSIGNMENT_RE.match(line)
                if match and match.group(1) in targets:
                    continue
                # Remove the legacy scalar written by early Coordinator releases.
                if section == "features" and match and match.group(1) == "multi_agent_v2":
                    continue
                cleaned.append(line)
            lines = cleaned
        output_blocks.append((section, header, lines))

    for section in TARGET_CONFIG:
        if section not in seen_targets:
            header = None if section == "" else f"[{section}]"
            output_blocks.append((section, header, []))

    rendered: List[str] = []
    for section, header, lines in output_blocks:
        if header is not None:
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append(header)
        while lines and lines[-1] == "":
            lines = lines[:-1]
        rendered.extend(lines)
        if section in TARGET_CONFIG:
            if rendered and rendered[-1] != "":
                rendered.append("")
            label = section or "root"
            rendered.append(f"# BEGIN coordinator managed {label}")
            for key, value in TARGET_CONFIG[section].items():
                rendered.append(f"{key} = {toml_value(value)}")
            rendered.append(f"# END coordinator managed {label}")
    result = "\n".join(rendered).strip() + "\n"
    validate_config_text(result)
    return result


def parse_simple_toml_assignments(text: str) -> Dict[Tuple[str, str], Any]:
    section = ""
    assignments: Dict[Tuple[str, str], Any] = {}
    sections_seen: set = set()
    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        array_table = ARRAY_TABLE_RE.fullmatch(line)
        if array_table:
            section = f"__array__:{line_number}:{array_table.group(1).strip()}"
            continue
        table = TABLE_RE.fullmatch(line)
        if table:
            section = table.group(1).strip()
            if section in sections_seen:
                raise InstallError(f"duplicate TOML table [{section}] at line {line_number}", code="invalid_config")
            sections_seen.add(section)
            continue
        assignment = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*(?:#.*)?", line)
        if not assignment:
            continue
        key, raw = assignment.groups()
        raw = raw.strip()
        if raw == "true":
            value: Any = True
        elif raw == "false":
            value = False
        elif re.fullmatch(r"-?\d+", raw):
            value = int(raw)
        elif raw.startswith('"'):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InstallError(f"invalid quoted TOML value at line {line_number}", code="invalid_config") from exc
        else:
            value = raw
        key_tuple = (section, key)
        if key_tuple in assignments:
            raise InstallError(f"duplicate TOML assignment {section}.{key}", code="invalid_config")
        assignments[key_tuple] = value
    return assignments


def validate_config_text(text: str) -> None:
    # Use the built-in full parser when available, while retaining 3.8 support.
    try:
        import tomllib  # type: ignore
    except ImportError:
        parse_simple_toml_assignments(text)
    else:
        try:
            tomllib.loads(text)
        except Exception as exc:
            raise InstallError(f"generated .codex/config.toml is invalid: {exc}", code="invalid_config") from exc


def config_is_current(path: pathlib.Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        if "# BEGIN coordinator managed features.multi_agent_v2" in text:
            return False
        validate_merge_safety(text)
        validate_config_text(text)
        assignments = parse_simple_toml_assignments(text)
    except (OSError, UnicodeDecodeError, InstallError):
        return False
    for section, values in TARGET_CONFIG.items():
        for key, expected in values.items():
            if assignments.get((section, key)) != expected:
                return False
    if ("agents", "default_subagent_model") in assignments:
        return False
    if ("agents", "default_subagent_reasoning_effort") in assignments:
        return False
    return True


def hook_command(python_name: str) -> str:
    marker = "COORDINATOR_ACTIVATION_HOOK"
    code = (
        "import pathlib,runpy,sys;"
        "p=pathlib.Path.cwd().resolve();"
        "r=next((q/'.agents/skills/coordinator/scripts/install.py' for q in (p,*p.parents) "
        "if (q/'.agents/skills/coordinator/scripts/install.py').is_file()),None);"
        f"assert r is not None,'{marker}: project installer not found';"
        "sys.argv=[str(r),'session-hook'];"
        "runpy.run_path(str(r),run_name='__main__')"
    )
    return f'{python_name} -B -c "{code}"'


def managed_hook_handler() -> Dict[str, Any]:
    return {
        "type": "command",
        "command": hook_command("python3"),
        "commandWindows": hook_command("python"),
        "timeout": 15,
        "statusMessage": HOOK_MARKER,
        "additionalContextLimit": 1200,
    }


def is_managed_hook_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    return str(handler.get("statusMessage", "")).startswith(HOOK_MARKER) or "COORDINATOR_ACTIVATION_HOOK" in str(
        handler.get("command", "")
    )


def merge_hooks(existing: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    document: Dict[str, Any] = dict(existing or {})
    hooks_raw = document.get("hooks", {})
    if not isinstance(hooks_raw, dict):
        raise InstallError(".codex/hooks.json has a non-object `hooks` value", code="hooks_ambiguous")
    hooks: Dict[str, Any] = dict(hooks_raw)
    groups_raw = hooks.get("SessionStart", [])
    if not isinstance(groups_raw, list):
        raise InstallError("SessionStart hooks must be a JSON array", code="hooks_ambiguous")
    groups: List[Any] = []
    for index, group in enumerate(groups_raw):
        if not isinstance(group, dict):
            raise InstallError(
                f"SessionStart hook group {index} must be a JSON object",
                code="hooks_ambiguous",
            )
        matcher = group.get("matcher")
        if matcher is not None and not isinstance(matcher, str):
            raise InstallError(
                f"SessionStart hook group {index} has a non-string matcher",
                code="hooks_ambiguous",
            )
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            raise InstallError(
                f"SessionStart hook group {index} has a non-array hooks value",
                code="hooks_ambiguous",
            )
        for handler_index, handler in enumerate(handlers):
            if not isinstance(handler, dict):
                raise InstallError(
                    f"SessionStart hook group {index} handler {handler_index} must be a JSON object",
                    code="hooks_ambiguous",
                )
        retained = [handler for handler in handlers if not is_managed_hook_handler(handler)]
        if retained:
            replacement = dict(group)
            replacement["hooks"] = retained
            groups.append(replacement)
    groups.append({"matcher": "startup|resume", "hooks": [managed_hook_handler()]})
    hooks["SessionStart"] = groups
    document["description"] = document.get("description") or "Project lifecycle hooks."
    document["hooks"] = hooks
    return document


def load_hooks(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot safely parse {path}: {exc}", code="invalid_hooks") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{path} must contain a JSON object", code="invalid_hooks")
    return value


def hooks_are_current(path: pathlib.Path) -> bool:
    try:
        document = load_hooks(path)
    except InstallError:
        return False
    if not document:
        return False
    groups = document.get("hooks", {}).get("SessionStart", []) if isinstance(document.get("hooks"), dict) else []
    expected = managed_hook_handler()
    if not isinstance(groups, list):
        return False
    managed_count = 0
    exact_group_count = 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
            return False
        handlers = group.get("hooks", [])
        managed_handlers = [handler for handler in handlers if is_managed_hook_handler(handler)]
        managed_count += len(managed_handlers)
        if group == {"matcher": "startup|resume", "hooks": [expected]}:
            exact_group_count += 1
    return managed_count == 1 and exact_group_count == 1


def read_metadata(project_root: pathlib.Path) -> Optional[Dict[str, Any]]:
    path = project_root / METADATA_REL
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def expected_project_files(source: PackageSource) -> Dict[pathlib.Path, str]:
    result: Dict[pathlib.Path, str] = {}
    for relative in source.manifest["project_skill_files"]:
        result[SKILL_REL / pathlib.PurePosixPath(relative)] = relative
    for role, relative in source.manifest["project_agent_files"].items():
        result[AGENTS_REL / f"{role}.toml"] = relative
    return result


def inspect_installation(project_root: pathlib.Path, source: PackageSource) -> Dict[str, Any]:
    mismatches: List[Dict[str, Any]] = []
    expected = expected_project_files(source)
    for destination_rel, source_rel in expected.items():
        destination = project_root / destination_rel
        if not destination.is_file():
            mismatches.append({"path": destination_rel.as_posix(), "reason": "missing"})
            continue
        actual = sha256_file(destination)
        expected_hash = sha256_bytes(source.read_bytes(source_rel))
        if actual != expected_hash:
            mismatches.append(
                {"path": destination_rel.as_posix(), "reason": "outdated_or_modified", "actual": actual, "expected": expected_hash}
            )
    metadata = read_metadata(project_root)
    expected_managed_paths = sorted(
        [path.as_posix() for path in expected]
        + [CONFIG_REL.as_posix(), HOOKS_REL.as_posix(), METADATA_REL.as_posix()]
    )
    stale_owned: List[str] = []
    if metadata and isinstance(metadata.get("managed_paths"), list):
        current = set(expected_managed_paths)
        for raw in metadata["managed_paths"]:
            if isinstance(raw, str) and raw not in current and (project_root / pathlib.PurePosixPath(raw)).exists():
                stale_owned.append(raw)
                mismatches.append({"path": raw, "reason": "stale_managed_file"})
    if not config_is_current(project_root / CONFIG_REL):
        mismatches.append({"path": CONFIG_REL.as_posix(), "reason": "missing_or_incompatible"})
    if not hooks_are_current(project_root / HOOKS_REL):
        mismatches.append({"path": HOOKS_REL.as_posix(), "reason": "missing_or_incompatible"})
    metadata_current = bool(
        metadata
        and metadata.get("schema_version") == INSTALL_SCHEMA_VERSION
        and metadata.get("name") == NAME
        and metadata.get("version") == source.version
        and isinstance(metadata.get("generation"), str)
        and bool(metadata.get("generation"))
        and sorted(metadata.get("managed_paths") or []) == expected_managed_paths
    )
    if not metadata_current:
        mismatches.append({"path": METADATA_REL.as_posix(), "reason": "missing_or_outdated"})
    return {
        "current": not mismatches,
        "project_root": str(project_root),
        "source": source.label,
        "source_kind": source.kind,
        "source_version": source.version,
        "installed_version": metadata.get("version") if metadata else None,
        "generation": metadata.get("generation") if metadata else None,
        "mismatches": mismatches,
        "stale_owned": stale_owned,
        "managed_paths": expected_managed_paths,
    }


def backup_paths(project_root: pathlib.Path, paths: Iterable[pathlib.Path], root: pathlib.Path) -> pathlib.Path:
    backup = root / ("backup-" + secrets.token_hex(8))
    backup.mkdir(parents=True)
    manifest: Dict[str, Any] = {}
    for relative in paths:
        source = project_root / relative
        key = relative.as_posix()
        if source.is_file():
            target = backup / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))
            manifest[key] = "file"
        elif source.is_dir():
            target = backup / "files" / relative
            shutil.copytree(str(source), str(target))
            manifest[key] = "dir"
        else:
            manifest[key] = "absent"
    atomic_write(backup / "manifest.json", json_bytes(manifest))
    return backup


def restore_backup(project_root: pathlib.Path, backup: pathlib.Path) -> None:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for raw, kind in manifest.items():
        relative = pathlib.PurePosixPath(raw)
        destination = project_root / relative
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(str(destination))
        else:
            with contextlib.suppress(FileNotFoundError):
                destination.unlink()
        if kind == "file":
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(backup / "files" / relative), str(destination))
        elif kind == "dir":
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(backup / "files" / relative), str(destination))


def install_files(project_root: pathlib.Path, source: PackageSource, inspection: Mapping[str, Any]) -> Dict[str, Any]:
    expected = expected_project_files(source)
    for destination_rel, source_rel in expected.items():
        executable = source_rel.endswith((".py", ".sh"))
        atomic_write(project_root / destination_rel, source.read_bytes(source_rel), executable=executable)
    for raw in inspection.get("stale_owned", []):
        target = project_root / pathlib.PurePosixPath(raw)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(str(target))
        else:
            with contextlib.suppress(FileNotFoundError):
                target.unlink()

    config_path = project_root / CONFIG_REL
    existing_config = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    merged_config = merge_config(existing_config)
    atomic_write(config_path, merged_config.encode("utf-8"))

    hooks_path = project_root / HOOKS_REL
    merged_hooks = merge_hooks(load_hooks(hooks_path))
    atomic_write(hooks_path, json_bytes(merged_hooks))

    generation = secrets.token_hex(16)
    metadata = {
        "schema_version": INSTALL_SCHEMA_VERSION,
        "name": NAME,
        "version": source.version,
        "generation": generation,
        "installed_at": iso_now(),
        "source_kind": source.kind,
        "source": source.label,
        "managed_paths": inspection["managed_paths"],
        "restart_required": True,
    }
    atomic_write(project_root / METADATA_REL, json_bytes(metadata))
    return metadata


def commit_managed_paths(git: str, project_root: pathlib.Path, managed_paths: Sequence[str], version: str) -> str:
    temporary_index = temp_root() / ("index-" + secrets.token_hex(12))
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(temporary_index)
    head = run_command([git, "-C", str(project_root), "rev-parse", "--verify", "HEAD"])
    if head.returncode == 0:
        run_command([git, "-C", str(project_root), "read-tree", "HEAD"], env=env, check=True)
    else:
        run_command([git, "-C", str(project_root), "read-tree", "--empty"], env=env, check=True)
    try:
        add = run_command([git, "-C", str(project_root), "add", "--all", "--", *managed_paths], env=env)
        if add.returncode != 0:
            raise InstallError(f"could not stage coordinator files: {(add.stderr or add.stdout).strip()}", code="git_stage_failed")
        quiet = run_command([git, "-C", str(project_root), "diff", "--cached", "--quiet"], env=env)
        if quiet.returncode == 0:
            raise InstallError("installer detected drift but produced no commit diff", code="empty_install_commit")
        if quiet.returncode not in {0, 1}:
            raise InstallError(f"could not inspect staged coordinator diff: {quiet.stderr.strip()}", code="git_diff_failed")
        commit = run_command(
            [git, "-C", str(project_root), "commit", "-m", f"chore(coordinator): install/update v{version}"],
            env=env,
        )
        if commit.returncode != 0:
            raise InstallError(
                f"mandatory coordinator installation commit failed: {(commit.stderr or commit.stdout).strip()}",
                code="git_commit_failed",
            )
        commit_hash = git_output(git, project_root, ["rev-parse", "HEAD"])
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_index.unlink()

    # Refresh only coordinator-owned entries in the user's real index. Existing
    # staged changes outside these paths remain staged exactly as they were.
    reset = run_command([git, "-C", str(project_root), "reset", "HEAD", "--", *managed_paths])
    if reset.returncode != 0:
        raise InstallError(
            f"installation committed, but the working index could not be refreshed: {(reset.stderr or reset.stdout).strip()}",
            code="git_index_refresh_failed",
        )
    return commit_hash


def project_status_after_install(project_root: pathlib.Path, source: PackageSource) -> Dict[str, Any]:
    result = inspect_installation(project_root, source)
    if not result["current"]:
        raise InstallError(
            "post-install verification failed: " + json.dumps(result["mismatches"], sort_keys=True),
            code="post_install_verification_failed",
        )
    return result


def token_directory(root: pathlib.Path) -> pathlib.Path:
    directory = root / "activation"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def activation_record_path(root: pathlib.Path, token: str) -> pathlib.Path:
    return token_directory(root) / (sha256_bytes(token.encode("utf-8")) + ".json")


def issue_activation(project_root: pathlib.Path, hook_input: Mapping[str, Any]) -> Dict[str, Any]:
    metadata = read_metadata(project_root)
    if not metadata or not metadata.get("generation"):
        raise InstallError("coordinator project metadata is missing", code="installation_missing")
    event_name = str(hook_input.get("hook_event_name") or "")
    source = str(hook_input.get("source") or "")
    if event_name != "SessionStart" or source not in {"startup", "resume"}:
        raise InstallError("activation tokens are issued only by SessionStart startup/resume hooks", code="invalid_activation_event")
    session_id = str(hook_input.get("session_id") or "").strip()
    if not session_id:
        raise InstallError("hook input has no session_id", code="invalid_hook_input")
    model = str(hook_input.get("model") or "").strip()
    token = secrets.token_urlsafe(32)
    record = {
        "token": token,
        "issued_at": iso_now(),
        "project_root": str(project_root.resolve()),
        "generation": metadata["generation"],
        "version": metadata.get("version"),
        "session_id": session_id,
        "model": model,
        "source": source,
        "hook_event_name": event_name,
    }
    atomic_write(activation_record_path(temp_root(), token), json_bytes(record))
    return record


def validate_activation(project_root: pathlib.Path, token: Optional[str]) -> Dict[str, Any]:
    if not token:
        raise InstallError(
            "the project is installed, but this Codex session has not proved that the project config and hook layer were loaded",
            code="activation_required",
            examples=[
                "Fully restart Codex in this project, then invoke $coordinator again.",
                "If Codex reports an untrusted hook, open /hooks, trust the Coordinator activation gate, restart Codex, and retry.",
            ],
        )
    record_path = activation_record_path(temp_root(), token)
    if not record_path.is_file():
        raise InstallError("activation token is unknown or expired", code="activation_required")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"activation record is invalid: {exc}", code="activation_required") from exc
    metadata = read_metadata(project_root)
    if not metadata:
        raise InstallError("coordinator installation metadata disappeared", code="installation_missing")
    checks = {
        "project": record.get("project_root") == str(project_root.resolve()),
        "generation": record.get("generation") == metadata.get("generation"),
        "event": record.get("hook_event_name") == "SessionStart",
        "source": record.get("source") in {"startup", "resume"},
        "session": bool(record.get("session_id")),
        "model": record.get("model") == "gpt-5.6-sol",
    }
    try:
        age = utcnow() - parse_iso(str(record.get("issued_at")))
        checks["fresh"] = dt.timedelta(0) <= age <= dt.timedelta(hours=ACTIVATION_TTL_HOURS)
    except (ValueError, TypeError):
        checks["fresh"] = False
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        detail = "activation proof failed: " + ", ".join(failed)
        if "model" in failed:
            detail += f" (active model was {record.get('model')!r}; required 'gpt-5.6-sol')"
        raise InstallError(detail, code="activation_required")
    return record


def ensure_project(
    project_argument: pathlib.Path,
    source_path: Optional[str],
    source_url: Optional[str],
    activation_token: Optional[str],
    *,
    commit: bool,
    require_activation: bool = True,
) -> Dict[str, Any]:
    runtime = require_runtime_dependencies(require_git=True)
    git = str(runtime["git"])
    project_root, initialized_git = resolve_project(project_argument, git, initialize=True)
    require_git_identity(git, project_root)
    root = temp_root()
    cleanup_stale(root)
    with install_lock(root):
        source = select_source(project_root, root, source_path, source_url)
        inspection = inspect_installation(project_root, source)
        if not inspection["current"]:
            paths = [pathlib.PurePosixPath(raw) for raw in inspection["managed_paths"]]
            for raw in inspection.get("stale_owned", []):
                path = pathlib.PurePosixPath(raw)
                if path not in paths:
                    paths.append(path)
            initial_head = git_output(git, project_root, ["rev-parse", "--verify", "HEAD"], check=False)
            backup = backup_paths(project_root, paths, root)
            try:
                metadata = install_files(project_root, source, inspection)
                if not commit:
                    raise InstallError("installation changes require a commit; --no-commit operation is unsupported", code="commit_required")
                commit_paths = [path.as_posix() for path in paths]
                commit_hash = commit_managed_paths(git, project_root, commit_paths, source.version)
                verified = project_status_after_install(project_root, source)
            except BaseException:
                # Roll back only when HEAD did not move. Once committed, preserving
                # the auditable commit is safer than silently rewriting history.
                current_head = git_output(git, project_root, ["rev-parse", "--verify", "HEAD"], check=False)
                if current_head == initial_head:
                    restore_backup(project_root, backup)
                raise
            finally:
                shutil.rmtree(str(backup), ignore_errors=True)
            return {
                "ok": True,
                "action": "installed" if inspection["installed_version"] is None else "updated",
                "project_root": str(project_root),
                "version": source.version,
                "source": source.label,
                "git_initialized": initialized_git,
                "commit": commit_hash,
                "generation": metadata["generation"],
                "restart_required": True,
                "continue_allowed": False,
                "next_action": (
                    "Fully restart Codex in this project. If the new project hook is marked untrusted, "
                    "open /hooks, trust 'Coordinator activation gate', restart again, then repeat the original $coordinator request."
                ),
                "changed": inspection["mismatches"],
                "verification": verified,
                "runtime": runtime,
            }
        if not require_activation:
            return {
                "ok": True,
                "action": "current",
                "project_root": str(project_root),
                "version": source.version,
                "source": source.label,
                "git_initialized": initialized_git,
                "restart_required": False,
                "continue_allowed": False,
                "next_action": "Coordinator project installation is current. Start or restart Codex before executing a coordinator task.",
                "runtime": runtime,
            }
        activation = validate_activation(project_root, activation_token)
        return {
            "ok": True,
            "action": "ready",
            "project_root": str(project_root),
            "version": source.version,
            "source": source.label,
            "git_initialized": initialized_git,
            "restart_required": False,
            "continue_allowed": True,
            "activation": {
                "session_id": activation["session_id"],
                "model": activation["model"],
                "issued_at": activation["issued_at"],
                "generation": activation["generation"],
            },
            "runtime": runtime,
        }


def command_status(args: argparse.Namespace) -> int:
    runtime = require_runtime_dependencies(require_git=True)
    project_root, _ = resolve_project(pathlib.Path(args.project), str(runtime["git"]), initialize=False)
    source = select_source(project_root, temp_root(), args.source, args.source_url)
    payload = inspect_installation(project_root, source)
    payload["runtime"] = runtime
    print_payload(payload, args.json)
    return 0 if payload["current"] else 1


def command_ensure(args: argparse.Namespace, manual_install: bool = False) -> int:
    try:
        payload = ensure_project(
            pathlib.Path(args.project),
            args.source,
            args.source_url,
            getattr(args, "activation_token", None),
            commit=True,
            require_activation=not manual_install,
        )
        print_payload(payload, args.json)
        if payload["restart_required"]:
            return EXIT_RESTART_REQUIRED
        if manual_install:
            return 0
        return 0
    except InstallError as exc:
        payload = {
            "ok": False,
            "code": exc.code,
            "error": str(exc),
            "examples": exc.examples,
            "continue_allowed": False,
            "restart_required": exc.code == "activation_required",
        }
        print_payload(payload, args.json)
        if exc.code == "activation_required":
            return EXIT_ACTIVATION_REQUIRED
        return EXIT_BLOCKED


def find_project_from_cwd(cwd: pathlib.Path) -> pathlib.Path:
    resolved = cwd.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / METADATA_REL).is_file() and (candidate / SKILL_REL / "SKILL.md").is_file():
            return candidate
    raise InstallError("SessionStart hook could not locate the coordinator project root", code="installation_missing")


def command_session_hook(_args: argparse.Namespace) -> int:
    root = temp_root()
    cleanup_stale(root)
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
        if not isinstance(hook_input, dict):
            raise InstallError("hook input must be a JSON object", code="invalid_hook_input")
        cwd = pathlib.Path(str(hook_input.get("cwd") or os.getcwd()))
        project_root = find_project_from_cwd(cwd)
        # The project copy is the authoritative source for its own integrity check.
        source = LocalPackageSource(project_root / SKILL_REL)
        inspection = inspect_installation(project_root, source)
        if not inspection["current"]:
            context = (
                "COORDINATOR_BOOTSTRAP_REQUIRED: the project coordinator installation is missing or outdated. "
                "Invoke the coordinator bootstrap immediately and do not begin task work. "
                f"Detected mismatches: {json.dumps(inspection['mismatches'], sort_keys=True)}"
            )
        else:
            record = issue_activation(project_root, hook_input)
            context = (
                "COORDINATOR_ACTIVATION_TOKEN=" + record["token"] + "\n"
                "Coordinator project bootstrap is current for this newly loaded Codex session. "
                "Pass this exact token to scripts/install.py ensure before any task work. "
                "Never infer, fabricate, reuse across projects, or bypass the token. "
                f"Active model reported by Codex: {record['model'] or '<missing>'}; required: gpt-5.6-sol."
            )
        event = str(hook_input.get("hook_event_name") or "SessionStart")
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": context,
                    }
                }
            )
        )
        return 0
    except (InstallError, json.JSONDecodeError, OSError) as exc:
        # A failed activation hook must not fabricate readiness. Emit concise
        # developer context so the skill blocks and repairs on invocation.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": (
                            "COORDINATOR_ACTIVATION_FAILED: " + str(exc) + ". "
                            "Do not execute coordinator task work; run bootstrap/status and report the blocker."
                        ),
                    }
                }
            )
        )
        return 0


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=".", help="project directory; an enclosing Git root is used when present")
    parser.add_argument("--source", help="local coordinator package directory")
    parser.add_argument("--source-url", help="remote coordinator package base URL")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser("ensure", help="install/update, commit, and enforce the restart activation gate")
    add_source_arguments(ensure)
    ensure.add_argument("--activation-token")

    install = subparsers.add_parser("install", help="manual install/update; always stops after reporting restart instructions")
    add_source_arguments(install)

    status = subparsers.add_parser("status", help="read-only installation drift report")
    add_source_arguments(status)

    subparsers.add_parser("session-hook", help=argparse.SUPPRESS)
    subparsers.add_parser("cleanup", help="remove and report coordinator temporary artifacts older than five days")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = temp_root()
        removed = cleanup_stale(root)
        if args.command == "ensure":
            return command_ensure(args, manual_install=False)
        if args.command == "install":
            return command_ensure(args, manual_install=True)
        if args.command == "status":
            return command_status(args)
        if args.command == "session-hook":
            return command_session_hook(args)
        if args.command == "cleanup":
            print(json.dumps({"temporary_root": str(root), "retention_days": RETENTION_DAYS, "removed": removed}, indent=2))
            return 0
        raise AssertionError(args.command)
    except InstallError as exc:
        print_payload(
            {"ok": False, "code": exc.code, "error": str(exc), "examples": exc.examples, "continue_allowed": False},
            getattr(args, "json", False),
        )
        return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
