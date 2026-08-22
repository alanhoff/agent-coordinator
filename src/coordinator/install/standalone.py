#!/usr/bin/env python3
"""Coordinator v3 current-user installer and standalone release bootstrap.

This module intentionally imports only the Python standard library. Release assembly
copies these exact bytes to the standalone ``install.py`` asset.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

NAME = "coordinator"
VERSION = "3.0.0"
INSTALL_SCHEMA = 3
PACKAGE_MARKER_SCHEMA = 1
ROLES = (
    "architect", "designer", "documenter", "fixer", "implementer", "researcher", "reviewer", "validator"
)
DEFAULT_RELEASE = "https://github.com/alanhoff/agent-coordinator/releases/latest/download"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ROLE_PATHS = {role: f"agents/roles/{role}.toml" for role in ROLES}
REQUIRED_PATHS = {
    ".coordinator-package.json", "SKILL.md", "README.md", "LICENSE", "VERSION", "agents/openai.yaml",
    "references/model-routing.md", "references/state-schema.md", "references/workflow-protocol.md", "references/dashboard.md",
    "scripts/coordinator_state.py", "scripts/doctor.py", "scripts/install.py", "scripts/model_router.py", "scripts/dashboard.py",
    "scripts/install.sh", "scripts/install.cmd",
    "scripts/lib/coordinator/__init__.py", "scripts/lib/coordinator/cli/__init__.py",
    "scripts/lib/coordinator/cli/state.py",
    "scripts/lib/coordinator/cli/routing.py", "scripts/lib/coordinator/cli/dashboard.py",
    "scripts/lib/coordinator/cli/doctor.py", "scripts/lib/coordinator/cli/outcome.py",
    "scripts/lib/coordinator/install/__init__.py", "scripts/lib/coordinator/install/standalone.py",
    "scripts/lib/coordinator/install/doctor.py", "scripts/lib/coordinator/state/__init__.py",
    "scripts/lib/coordinator/state/store.py", "scripts/lib/coordinator/routing/__init__.py",
    "scripts/lib/coordinator/routing/selector.py", "scripts/lib/coordinator/dashboard/__init__.py",
    "scripts/lib/coordinator/dashboard/view.py",
    *ROLE_PATHS.values(),
}
class InstallError(RuntimeError):
    def __init__(self, message: str, *, code: str = "install_error", exit_code: int = 20):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class _InvocationError(ValueError):
    pass


class _OutcomeArgumentParser(argparse.ArgumentParser):
    def add_subparsers(self, **kwargs: Any) -> argparse._SubParsersAction[Any]:
        kwargs.setdefault("parser_class", type(self))
        return super().add_subparsers(**kwargs)

    def error(self, _message: str) -> None:
        raise _InvocationError("invalid command line")


def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _strict_json(data: bytes, field: str, *, code: str = "invalid_data") -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise InstallError(f"{field} contains duplicate key {key!r}", code=code)
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InstallError(f"{field} is not valid UTF-8 JSON", code=code) from exc


def _marker(data: bytes) -> dict[str, Any]:
    value = _strict_json(data, "package marker", code="invalid_package")
    expected = {"schema_version", "name", "version", "source_commit"}
    if not isinstance(value, dict) or set(value) != expected:
        raise InstallError("package marker fields are invalid", code="invalid_package")
    if value["schema_version"] != PACKAGE_MARKER_SCHEMA or value["name"] != NAME:
        raise InstallError("package marker identity is invalid", code="invalid_package")
    if not isinstance(value["version"], str) or not SEMVER_RE.fullmatch(value["version"]):
        raise InstallError("package marker version is not SemVer", code="invalid_package")
    if not isinstance(value["source_commit"], str) or not COMMIT_RE.fullmatch(value["source_commit"]):
        raise InstallError("package marker source commit is invalid", code="invalid_package")
    return value


def _logical_mode(mode: int) -> str:
    return "executable" if mode & 0o111 else "file"


def _package_mode(relative: str) -> str:
    parts = pathlib.PurePosixPath(relative).parts
    return "executable" if len(parts) == 2 and parts[0] == "scripts" else "file"


@dataclasses.dataclass(frozen=True)
class VerifiedPackage:
    files: Mapping[str, bytes]
    modes: Mapping[str, str]
    label: str
    package_digest: str
    content_digest: str
    version: str
    source_commit: str


def _verify_contents(
    files: Mapping[str, bytes],
    modes: Mapping[str, str],
    *,
    label: str,
    package_digest: str | None = None,
    enforce_modes: bool = True,
) -> VerifiedPackage:
    actual = set(files)
    missing = REQUIRED_PATHS - actual
    if missing:
        raise InstallError("package is missing required paths: " + ",".join(sorted(missing)), code="invalid_package")
    if set(modes) != actual:
        raise InstallError("package modes do not cover the exact inventory", code="invalid_package")
    canonical_modes = {relative: _package_mode(relative) for relative in files}
    for relative in sorted(files):
        data = files[relative]
        if len(data) > MAX_FILE_BYTES:
            raise InstallError(f"package file exceeds the size limit: {relative}", code="capacity_exceeded")
        if enforce_modes and modes[relative] != canonical_modes[relative]:
            raise InstallError(f"package mode mismatch: {relative}", code="invalid_package")
    try:
        version = files["VERSION"].decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise InstallError("VERSION is not UTF-8", code="invalid_package") from exc
    marker = _marker(files[".coordinator-package.json"])
    if version != marker["version"] or not SEMVER_RE.fullmatch(version):
        raise InstallError("VERSION and package marker do not agree", code="invalid_package")
    for relative in ("scripts/lib/coordinator/__init__.py", "scripts/lib/coordinator/install/standalone.py"):
        try:
            source = files[relative].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InstallError(f"{relative} is not UTF-8", code="invalid_package") from exc
        embedded = re.search(r'^VERSION = "([^"]+)"$', source, re.MULTILINE)
        if not embedded or embedded.group(1) != version:
            raise InstallError(f"{relative} version disagrees with VERSION", code="invalid_package")
    content_digest = _sha256(
        b"".join(
            name.encode("utf-8")
            + b"\0"
            + canonical_modes[name].encode("ascii")
            + b"\0"
            + len(files[name]).to_bytes(8, "big")
            + files[name]
            for name in sorted(files)
        )
    )
    return VerifiedPackage(
        dict(files),
        canonical_modes,
        label,
        package_digest or content_digest,
        content_digest,
        version,
        str(marker["source_commit"]),
    )


def verify_directory(root: pathlib.Path) -> VerifiedPackage:
    root = root.expanduser()
    try:
        info = root.lstat()
    except OSError as exc:
        raise InstallError(f"package directory is unavailable: {root}", code="invalid_source") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise InstallError("package source must be a real directory", code="unsafe_path")
    files: dict[str, bytes] = {}
    modes: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode) or _is_reparse(item) or not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)):
            raise InstallError(f"package contains an unsafe filesystem entry: {relative}", code="unsafe_path")
        if stat.S_ISREG(item.st_mode):
            if item.st_nlink != 1:
                raise InstallError(f"package file has unexpected hard links: {relative}", code="unsafe_path")
            if item.st_size > MAX_FILE_BYTES:
                raise InstallError(f"package file exceeds the size limit: {relative}", code="capacity_exceeded")
            files[relative] = path.read_bytes()
            modes[relative] = _logical_mode(item.st_mode)
    if len(files) > MAX_ARCHIVE_ENTRIES or sum(len(data) for data in files.values()) > MAX_ARCHIVE_BYTES:
        raise InstallError("package directory violates entry or size limits", code="capacity_exceeded")
    return _verify_contents(
        files,
        modes,
        label=str(root.resolve()),
        enforce_modes=os.name != "nt",
    )


def verify_zip(data: bytes, *, label: str) -> VerifiedPackage:
    digest = _sha256(data)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise InstallError("ZIP exceeds the download size limit", code="capacity_exceeded")
    end = data.rfind(b"PK\x05\x06")
    if not data.startswith(b"PK\x03\x04") or end < 0 or end + 22 > len(data):
        raise InstallError("ZIP framing is invalid", code="invalid_package")
    comment_length = int.from_bytes(data[end + 20 : end + 22], "little")
    if end + 22 + comment_length != len(data):
        raise InstallError("ZIP contains leading or trailing data", code="invalid_package")
    import io

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InstallError("source is not a valid ZIP", code="invalid_package") from exc
    with archive:
        entries = archive.infolist()
        if not entries or len(entries) > MAX_ARCHIVE_ENTRIES or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise InstallError("ZIP violates entry or expanded-size limits", code="capacity_exceeded")
        seen: set[str] = set()
        payloads: dict[str, bytes] = {}
        modes: dict[str, str] = {}
        for item in entries:
            name = item.filename
            if "\\" in name or name.startswith("/") or item.file_size > MAX_FILE_BYTES:
                raise InstallError("ZIP contains an unsafe or oversized entry", code="invalid_package")
            parts = pathlib.PurePosixPath(name).parts
            if not parts or parts[0] != "coordinator" or any(part in ("", ".", "..") for part in parts):
                raise InstallError("ZIP must contain one safe coordinator/ package root", code="invalid_package")
            relative = pathlib.PurePosixPath(*parts[1:]).as_posix()
            if not relative or item.is_dir():
                raise InstallError("ZIP contains an unlisted directory entry", code="invalid_package")
            if relative in seen:
                raise InstallError(f"ZIP contains duplicate entry: {relative}", code="invalid_package")
            seen.add(relative)
            unix_mode = (item.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(unix_mode)
            if kind not in (0, stat.S_IFREG):
                raise InstallError(f"ZIP entry is not a regular file: {relative}", code="unsafe_path")
            try:
                content = archive.read(item)
            except RuntimeError as exc:
                raise InstallError("ZIP entry uses unsupported encryption or compression", code="invalid_package") from exc
            if len(content) != item.file_size:
                raise InstallError(f"ZIP entry was truncated: {relative}", code="invalid_package")
            payloads[relative] = content
            modes[relative] = _logical_mode(unix_mode)
        return _verify_contents(payloads, modes, label=label, package_digest=digest)


def _download(url: str, *, maximum: int = MAX_DOWNLOAD_BYTES) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InstallError("network source must be credential-free HTTPS", code="invalid_source", exit_code=2)
    request = urllib.request.Request(url, headers={"User-Agent": "agent-coordinator/3", "Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.username or final.password:
                raise InstallError("download redirected outside credential-free HTTPS", code="invalid_source")
            length = response.headers.get("Content-Length")
            if length and int(length) > maximum:
                raise InstallError("download exceeds size limit", code="capacity_exceeded")
            data = response.read(maximum + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise InstallError("unable to download release asset", code="network_error") from exc
    if len(data) > maximum:
        raise InstallError("download exceeds size limit", code="capacity_exceeded")
    return data


def acquire_source(source: str | None, source_url: str | None) -> VerifiedPackage:
    if source and source_url:
        raise InstallError("--source cannot be combined with network source arguments", code="invalid_invocation", exit_code=2)
    if source:
        path = pathlib.Path(source).expanduser()
        if path.is_dir():
            return verify_directory(path)
        if path.is_file() and not path.is_symlink():
            return verify_zip(path.read_bytes(), label=str(path.resolve()))
        raise InstallError("local source must be a package directory or ZIP", code="invalid_source", exit_code=2)
    if source_url:
        data = _download(source_url)
        return verify_zip(data, label=source_url)
    zip_data = _download(DEFAULT_RELEASE + "/coordinator-latest.zip")
    return verify_zip(zip_data, label=DEFAULT_RELEASE)


@dataclasses.dataclass(frozen=True)
class Paths:
    home: pathlib.Path
    package: pathlib.Path
    roles: pathlib.Path
    config: pathlib.Path
    control: pathlib.Path
    metadata: pathlib.Path
    journal: pathlib.Path
    lock: pathlib.Path


def paths() -> Paths:
    home = pathlib.Path.home()
    return Paths(
        home=home,
        package=home / ".agents" / "skills" / NAME,
        roles=home / ".codex" / "agents" / NAME,
        config=home / ".codex" / "config.toml",
        control=home / ".agent-coordinator",
        metadata=home / ".agent-coordinator" / "install.json",
        journal=home / ".agent-coordinator" / "recovery" / "install.json",
        lock=home / ".agent-coordinator" / "locks" / "install.lock",
    )


def _is_reparse(info: os.stat_result) -> bool:
    attribute = getattr(info, "st_file_attributes", 0)
    return bool(attribute & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_directory(path: pathlib.Path, home: pathlib.Path, *, private: bool = False) -> None:
    path = path.absolute()
    home = home.absolute()
    try:
        relative = path.relative_to(home)
    except ValueError as exc:
        raise InstallError("owned path escapes the home snapshot", code="unsafe_path") from exc
    chain = [home]
    current = home
    for part in relative.parts:
        current = current / part
        chain.append(current)
    for index, ancestor in enumerate(chain):
        existed = ancestor.exists()
        if not existed:
            if ancestor.is_symlink():
                raise InstallError(f"refusing symlink path: {ancestor}", code="unsafe_path")
            ancestor.mkdir(mode=0o700 if private else 0o755)
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise InstallError(f"refusing unsafe directory: {ancestor}", code="unsafe_path")
        if private and index > 0 and os.name != "nt":
            if info.st_uid != os.getuid():
                raise InstallError("control directory belongs to another user", code="unsafe_owner")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise InstallError("control directory is not current-user private", code="unsafe_permissions")


def _atomic(path: pathlib.Path, data: bytes, *, mode: int = 0o600) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise InstallError("atomic host write failed", code="io_error") from exc


def _install_lock_evidence(path: pathlib.Path) -> tuple[tuple[int, int, int, int, int, int], bytes, int] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError("install lock evidence is ambiguous", code="install_locked") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            raw = handle.read(4097)
        after = path.lstat()
    except OSError as exc:
        raise InstallError("install lock evidence is ambiguous", code="install_locked") from exc
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)
    observed = (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size, after.st_mtime_ns)
    if (
        identity != observed
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or _is_reparse(after)
        or info.st_nlink != 1
        or len(raw) > 4096
        or len(raw) != info.st_size
        or (os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))
    ):
        raise InstallError("install lock evidence is ambiguous", code="install_locked")
    try:
        value = _strict_json(raw, "install lock")
        if not isinstance(value, dict) or set(value) != {"pid", "nonce", "created_at"}:
            raise InstallError("install lock fields are invalid")
        if not isinstance(value["pid"], int) or isinstance(value["pid"], bool) or value["pid"] <= 0:
            raise InstallError("install lock process is invalid")
        if not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]):
            raise InstallError("install lock nonce is invalid")
        if not isinstance(value["created_at"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["created_at"]):
            raise InstallError("install lock timestamp is invalid")
    except InstallError as exc:
        raise InstallError("install lock evidence is ambiguous", code="install_locked") from exc
    return identity, raw, value["pid"]


def _windows_process_is_proven_dead(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value != 259  # STILL_ACTIVE
    finally:
        close_handle(handle)


def _process_is_proven_dead(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_is_proven_dead(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _remove_proven_stale_install_lock(path: pathlib.Path) -> bool:
    try:
        first = _install_lock_evidence(path)
        if first is None:
            return True
        if not _process_is_proven_dead(first[2]):
            return False
        second = _install_lock_evidence(path)
        if second is None:
            return True
        if second != first:
            return False
        path.unlink()
        return True
    except (OSError, InstallError):
        return False


@contextmanager
def _install_lock(owned: Paths) -> Iterator[None]:
    _safe_directory(owned.lock.parent, owned.home, private=True)
    nonce = secrets.token_hex(16)
    payload = json.dumps({"pid": os.getpid(), "nonce": nonce, "created_at": _now()}, sort_keys=True).encode("utf-8")
    for attempt in range(2):
        try:
            descriptor = os.open(owned.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError as exc:
            if attempt or not _remove_proven_stale_install_lock(owned.lock):
                raise InstallError("another install controller holds the global lock", code="install_locked") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            current = _install_lock_evidence(owned.lock)
            if current is not None and current[1] == payload:
                owned.lock.unlink()
        except (OSError, InstallError):
            pass


def _config_merge(existing: bytes) -> bytes:
    try:
        text = existing.decode("utf-8")
        parsed = tomllib.loads(text) if text.strip() else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError("existing Codex config is not valid UTF-8 TOML", code="ambiguous_config") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    requirements = {
        "agents": {"enabled": "true", "max_concurrent_threads_per_session": "8"},
        "features": {"multi_agent": "true"},
    }
    for section, assignments in requirements.items():
        begin = f"# >>> agent-coordinator:{section}"
        end = f"# <<< agent-coordinator:{section}"
        begins = [index for index, line in enumerate(lines) if line.strip() == begin]
        ends = [index for index, line in enumerate(lines) if line.strip() == end]
        owned = bool(begins or ends)
        if len(begins) != len(ends) or len(begins) > 1 or (owned and begins[0] >= ends[0]):
            raise InstallError(f"existing Coordinator config marker is ambiguous for [{section}]", code="ambiguous_config")
        header_pattern = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$")
        headers = [index for index, line in enumerate(lines) if header_pattern.fullmatch(line)]
        if len(headers) > 1:
            raise InstallError(f"duplicate [{section}] table is ambiguous", code="ambiguous_config")
        current_table = parsed.get(section)
        if current_table is not None and not isinstance(current_table, dict):
            raise InstallError(f"{section} is not a table", code="ambiguous_config")
        for key in assignments:
            if isinstance(current_table, dict) and key in current_table and not owned:
                raise InstallError(f"existing {section}.{key} is not Coordinator-owned", code="ambiguous_config")
        if owned:
            if not headers:
                raise InstallError(f"Coordinator marker is outside [{section}]", code="ambiguous_config")
            table_end = next((index for index in range(headers[0] + 1, len(lines)) if re.match(r"^\s*\[", lines[index])), len(lines))
            if not (headers[0] < begins[0] < ends[0] < table_end):
                raise InstallError(f"Coordinator marker is outside [{section}]", code="ambiguous_config")
            del lines[begins[0] : ends[0] + 1]
            headers = [index for index, line in enumerate(lines) if header_pattern.fullmatch(line)]
        block = [begin, *(f"{key} = {value}" for key, value in assignments.items()), end]
        if headers:
            start = headers[0] + 1
            next_header = next((index for index in range(start, len(lines)) if re.match(r"^\s*\[", lines[index])), len(lines))
            lines[next_header:next_header] = block
        else:
            if current_table is not None:
                raise InstallError(f"[{section}] uses syntax that cannot be preserved safely", code="ambiguous_config")
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([f"[{section}]", *block])
    merged = newline.join(lines).rstrip() + newline
    try:
        result = tomllib.loads(merged)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError("Coordinator config merge would be invalid", code="ambiguous_config") from exc
    if result.get("agents", {}).get("enabled") is not True or result.get("agents", {}).get("max_concurrent_threads_per_session") != 8 or result.get("features", {}).get("multi_agent") is not True:
        raise InstallError("Coordinator config merge did not establish required semantics", code="ambiguous_config")
    return merged.encode("utf-8")


def _config_current(path: pathlib.Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    lines = text.splitlines()
    for section in ("agents", "features"):
        begin = f"# >>> agent-coordinator:{section}"
        end = f"# <<< agent-coordinator:{section}"
        begins = [index for index, line in enumerate(lines) if line.strip() == begin]
        ends = [index for index, line in enumerate(lines) if line.strip() == end]
        header = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$")
        headers = [index for index, line in enumerate(lines) if header.fullmatch(line)]
        if len(begins) != 1 or len(ends) != 1 or len(headers) != 1:
            return False
        table_end = next((index for index in range(headers[0] + 1, len(lines)) if re.match(r"^\s*\[", lines[index])), len(lines))
        if not headers[0] < begins[0] < ends[0] < table_end:
            return False
    return (
        parsed.get("agents", {}).get("enabled") is True
        and parsed.get("agents", {}).get("max_concurrent_threads_per_session") == 8
        and parsed.get("features", {}).get("multi_agent") is True
    )


def _write_package(directory: pathlib.Path, package: VerifiedPackage) -> None:
    directory.mkdir(mode=0o700)
    for relative in sorted(package.files):
        target = directory.joinpath(*pathlib.PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(package.files[relative])
        os.chmod(target, 0o755 if package.modes[relative] == "executable" else 0o644)


def _write_roles(directory: pathlib.Path, package: VerifiedPackage) -> None:
    directory.mkdir(mode=0o700)
    for role, relative in ROLE_PATHS.items():
        target = directory / f"{role}.toml"
        target.write_bytes(package.files[relative])
        os.chmod(target, 0o600)


def _valid_roles(path: pathlib.Path, package: VerifiedPackage | None = None) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    expected = {f"{role}.toml" for role in ROLES}
    try:
        directory = path.lstat()
        if _is_reparse(directory) or (os.name != "nt" and (directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077)):
            return False
        if {item.name for item in path.iterdir()} != expected:
            return False
        for role, relative in ROLE_PATHS.items():
            item = path / f"{role}.toml"
            info = item.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _is_reparse(info)
                or info.st_nlink != 1
                or (os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))
            ):
                return False
            if package and not secrets.compare_digest(item.read_bytes(), package.files[relative]):
                return False
    except OSError:
        return False
    return True


def _remove_tree(path: pathlib.Path, home: pathlib.Path, *, kind: str) -> None:
    if not _safe_existing_chain(path, home, error_code="unsafe_path"):
        return
    if path.is_symlink() or not path.is_dir():
        raise InstallError("cleanup target is not a safe directory", code="unsafe_path")
    if kind == "package":
        try:
            _marker((path / ".coordinator-package.json").read_bytes())
        except (OSError, InstallError) as exc:
            raise InstallError("refusing to remove an unmarked package tree", code="unsafe_path") from exc
    elif kind == "roles" and not _valid_roles(path):
        raise InstallError("refusing to remove an unknown role tree", code="unsafe_path")
    for item in path.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise InstallError("cleanup tree contains an unsafe entry", code="unsafe_path")
    shutil.rmtree(path)


def _remove_staging(path: pathlib.Path, parent: pathlib.Path) -> None:
    if not path.exists():
        return
    if path.parent != parent or not path.name.startswith(".coordinator.new.") or path.is_symlink() or not path.is_dir():
        raise InstallError("staging cleanup target is unsafe", code="unsafe_path")
    for item in path.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise InstallError("staging tree contains an unsafe entry", code="unsafe_path")
    shutil.rmtree(path)


def _metadata(package: VerifiedPackage, transaction: str) -> dict[str, Any]:
    return {
        "schema_version": INSTALL_SCHEMA,
        "name": NAME,
        "version": package.version,
        "source_commit": package.source_commit,
        "content_digest": package.content_digest,
        "package_digest": package.package_digest,
        "transaction": transaction,
        "installed_at": _now(),
    }


def _read_metadata(owned: Paths) -> dict[str, Any] | None:
    if not owned.metadata.exists():
        return None
    info = owned.metadata.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
        or info.st_nlink != 1
        or info.st_size > 1024 * 1024
        or (os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))
    ):
        raise InstallError("install metadata path is unsafe", code="unsafe_path")
    value = _strict_json(owned.metadata.read_bytes(), "install metadata", code="invalid_metadata")
    expected = {"schema_version", "name", "version", "source_commit", "content_digest", "package_digest", "transaction", "installed_at"}
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != INSTALL_SCHEMA or value["name"] != NAME:
        raise InstallError("install metadata is invalid", code="invalid_metadata")
    return value


def inspect(owned: Paths | None = None) -> dict[str, Any]:
    current_paths = owned or paths()
    mismatches: list[str] = []
    try:
        for parent in (
            current_paths.package.parent,
            current_paths.roles.parent,
            current_paths.config.parent,
            current_paths.metadata.parent,
            current_paths.journal.parent,
            current_paths.lock.parent,
        ):
            _safe_existing_chain(parent, current_paths.home, error_code="unsafe_path")
        _safe_leaf(current_paths.config, directory=False, error_code="unsafe_path")
    except InstallError as exc:
        return {
            "installed": False,
            "current": False,
            "version": None,
            "source_commit": None,
            "mismatches": ["paths:" + exc.code],
            "new_session_required": False,
        }
    package = None
    try:
        package = verify_directory(current_paths.package)
    except InstallError as exc:
        mismatches.append("package:" + exc.code)
    metadata = None
    try:
        metadata = _read_metadata(current_paths)
    except InstallError as exc:
        mismatches.append("metadata:" + exc.code)
    if package and metadata:
        if (
            metadata["version"] != package.version
            or metadata["source_commit"] != package.source_commit
            or metadata["content_digest"] != package.content_digest
        ):
            mismatches.append("metadata:drift")
    elif package or metadata:
        mismatches.append("ownership:incomplete")
    if not _valid_roles(current_paths.roles, package):
        mismatches.append("roles:drift")
    if not _config_current(current_paths.config):
        mismatches.append("config:drift")
    if current_paths.journal.exists():
        mismatches.append("recovery:pending")
    return {
        "installed": package is not None and metadata is not None,
        "current": not mismatches,
        "version": package.version if package else None,
        "source_commit": package.source_commit if package else None,
        "mismatches": mismatches,
        "new_session_required": False,
    }


def _safe_existing_chain(path: pathlib.Path, home: pathlib.Path, *, error_code: str = "recovery_mismatch") -> bool:
    absolute = path.absolute()
    home = home.absolute()
    try:
        relative = absolute.relative_to(home)
    except ValueError as exc:
        raise InstallError("path escapes the home snapshot", code=error_code) from exc
    try:
        home_info = home.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(home_info.st_mode) or stat.S_ISLNK(home_info.st_mode) or _is_reparse(home_info):
        raise InstallError("home path is an unsafe ancestor", code=error_code)
    current = home
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise InstallError("path contains an unsafe ancestor", code=error_code)
    return True


def _safe_leaf(
    path: pathlib.Path, *, directory: bool, private: bool = False, error_code: str = "recovery_mismatch"
) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not expected
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
        or (not directory and info.st_nlink != 1)
        or (os.name != "nt" and info.st_uid != os.getuid())
        or (private and os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077)
    ):
        raise InstallError("recovery target or backup is unsafe", code=error_code)
    return info


def _safe_file_bytes(path: pathlib.Path, *, maximum: int, private: bool = False) -> bytes | None:
    info = _safe_leaf(path, directory=False, private=private)
    if info is None:
        return None
    if info.st_size > maximum:
        raise InstallError("recovery target exceeds its size limit", code="recovery_mismatch")
    before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    data = path.read_bytes()
    after_info = path.lstat()
    after = (after_info.st_dev, after_info.st_ino, after_info.st_size, after_info.st_mtime_ns)
    if before != after or len(data) != info.st_size:
        raise InstallError("recovery target changed during validation", code="recovery_mismatch")
    return data


def _role_digest_from_files(files: Mapping[str, bytes]) -> str:
    return _sha256(
        b"".join(role.encode("ascii") + b"\0" + _sha256(files[ROLE_PATHS[role]]).encode("ascii") + b"\0" for role in ROLES)
    )


def _roles_digest(path: pathlib.Path) -> str:
    if not _valid_roles(path):
        raise InstallError("role backup or target is invalid", code="recovery_mismatch")
    return _sha256(
        b"".join(role.encode("ascii") + b"\0" + _sha256((path / f"{role}.toml").read_bytes()).encode("ascii") + b"\0" for role in ROLES)
    )


def _recovery_paths(owned: Paths, journal: Mapping[str, Any]) -> dict[str, pathlib.Path]:
    transaction = journal["transaction"]
    result = {
        "package_backup": owned.package.parent / f".coordinator.backup.{transaction}",
        "roles_backup": owned.roles.parent / f".coordinator.backup.{transaction}",
        "package_new": owned.package.parent / f".coordinator.new.{transaction}",
        "roles_new": owned.roles.parent / f".coordinator.new.{transaction}",
    }
    if str(result["package_backup"]) != journal["package_backup"] or str(result["roles_backup"]) != journal["roles_backup"]:
        raise InstallError("recovery target identity differs", code="recovery_mismatch")
    return result


def _journal(owned: Paths) -> dict[str, Any] | None:
    if not _safe_existing_chain(owned.journal.parent, owned.home):
        return None
    raw = _safe_file_bytes(owned.journal, maximum=16 * 1024 * 1024, private=True)
    if raw is None:
        return None
    value = _strict_json(raw, "recovery journal", code="invalid_recovery")
    expected = {
        "schema_version", "transaction", "package_digest", "package_content_digest", "roles_content_digest",
        "config_digest", "metadata_digest", "phase", "package_backup", "roles_backup",
        "package_existed", "package_backup_digest", "roles_existed", "roles_backup_digest",
        "config_existed", "config_backup", "config_backup_digest",
        "metadata_existed", "metadata_backup", "metadata_backup_digest",
    }
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != INSTALL_SCHEMA:
        raise InstallError("recovery journal is invalid", code="invalid_recovery")
    if not isinstance(value["transaction"], str) or not re.fullmatch(r"[0-9a-f]{24}", value["transaction"]):
        raise InstallError("recovery transaction is invalid", code="invalid_recovery")
    for key in ("package_digest", "package_content_digest", "roles_content_digest", "config_digest", "metadata_digest"):
        if not isinstance(value[key], str) or not SHA256_RE.fullmatch(value[key]):
            raise InstallError("recovery current digest evidence is invalid", code="invalid_recovery")
    if value["phase"] not in ("prepared", "surface_replaced", "committed"):
        raise InstallError("recovery phase is invalid", code="invalid_recovery")
    for key in ("package_backup", "roles_backup"):
        if not isinstance(value[key], str) or len(value[key]) > 4096:
            raise InstallError("recovery backup identity is invalid", code="invalid_recovery")
    for key in ("package_existed", "roles_existed", "config_existed", "metadata_existed"):
        if not isinstance(value[key], bool):
            raise InstallError("recovery existence evidence is invalid", code="invalid_recovery")
    for existed, digest_key in (
        ("package_existed", "package_backup_digest"),
        ("roles_existed", "roles_backup_digest"),
        ("config_existed", "config_backup_digest"),
        ("metadata_existed", "metadata_backup_digest"),
    ):
        digest_value = value[digest_key]
        if value[existed]:
            valid_digest = isinstance(digest_value, str) and bool(SHA256_RE.fullmatch(digest_value))
        else:
            valid_digest = digest_value is None
        if not valid_digest:
            raise InstallError("recovery backup digest evidence is inconsistent", code="invalid_recovery")
    for key, existed, digest_key in (
        ("config_backup", "config_existed", "config_backup_digest"),
        ("metadata_backup", "metadata_existed", "metadata_backup_digest"),
    ):
        if not isinstance(value[key], str) or len(value[key]) > 16 * 1024 * 1024:
            raise InstallError("recovery backup payload is invalid", code="invalid_recovery")
        try:
            decoded = base64.b64decode(value[key], validate=True)
        except ValueError as exc:
            raise InstallError("recovery backup payload is not valid base64", code="invalid_recovery") from exc
        if value[existed]:
            if not secrets.compare_digest(_sha256(decoded), value[digest_key]):
                raise InstallError("recovery embedded backup digest differs", code="invalid_recovery")
        elif decoded:
            raise InstallError("recovery has a payload for an absent backup", code="invalid_recovery")
    _recovery_paths(owned, value)
    return value


def _revalidate_recovery(owned: Paths, journal: Mapping[str, Any], action: str) -> dict[str, pathlib.Path]:
    transaction_paths = _recovery_paths(owned, journal)
    for path in (
        owned.package, owned.roles, owned.config, owned.metadata, owned.journal,
        *transaction_paths.values(),
    ):
        if not _safe_existing_chain(path.parent, owned.home):
            raise InstallError("recovery path parent is missing", code="recovery_mismatch")
    package_backup = transaction_paths["package_backup"]
    roles_backup = transaction_paths["roles_backup"]
    package_backup_info = _safe_leaf(package_backup, directory=True)
    roles_backup_info = _safe_leaf(roles_backup, directory=True, private=True)
    if package_backup_info is not None:
        backup = verify_directory(package_backup)
        if not secrets.compare_digest(backup.content_digest, journal["package_backup_digest"] or ""):
            raise InstallError("package backup digest differs", code="recovery_mismatch")
    if roles_backup_info is not None and not secrets.compare_digest(_roles_digest(roles_backup), journal["roles_backup_digest"] or ""):
        raise InstallError("role backup digest differs", code="recovery_mismatch")
    if not journal["package_existed"] and package_backup_info is not None:
        raise InstallError("unexpected package backup exists", code="recovery_mismatch")
    if not journal["roles_existed"] and roles_backup_info is not None:
        raise InstallError("unexpected role backup exists", code="recovery_mismatch")

    current_package = None
    if _safe_leaf(owned.package, directory=True) is not None:
        current_package = verify_directory(owned.package)
    current_roles_digest = _roles_digest(owned.roles) if _safe_leaf(owned.roles, directory=True, private=True) is not None else None
    current_config = _safe_file_bytes(owned.config, maximum=4 * 1024 * 1024)
    current_metadata = _safe_file_bytes(owned.metadata, maximum=1024 * 1024, private=True)

    if action == "finalize":
        if current_package is None or not secrets.compare_digest(current_package.content_digest, journal["package_content_digest"]):
            raise InstallError("committed package differs from recovery evidence", code="recovery_mismatch")
        if current_roles_digest is None or not secrets.compare_digest(current_roles_digest, journal["roles_content_digest"]):
            raise InstallError("committed roles differ from recovery evidence", code="recovery_mismatch")
        if current_config is None or not secrets.compare_digest(_sha256(current_config), journal["config_digest"]):
            raise InstallError("committed config differs from recovery evidence", code="recovery_mismatch")
        if current_metadata is None or not secrets.compare_digest(_sha256(current_metadata), journal["metadata_digest"]):
            raise InstallError("committed metadata differs from recovery evidence", code="recovery_mismatch")
    else:
        if journal["phase"] == "surface_replaced" and (
            (journal["package_existed"] and package_backup_info is None)
            or (journal["roles_existed"] and roles_backup_info is None)
            or current_package is None
            or not secrets.compare_digest(current_package.content_digest, journal["package_content_digest"])
            or current_roles_digest is None
            or not secrets.compare_digest(current_roles_digest, journal["roles_content_digest"])
        ):
            raise InstallError("surface-replaced recovery evidence is incomplete", code="recovery_mismatch")
        if current_package is not None:
            if journal["package_existed"] and package_backup_info is None:
                allowed = {journal["package_backup_digest"]}
            else:
                allowed = {journal["package_content_digest"]}
            if current_package.content_digest not in allowed:
                raise InstallError("rollback package target differs", code="recovery_mismatch")
        elif journal["package_existed"] and package_backup_info is None:
            raise InstallError("rollback package and backup are both missing", code="recovery_mismatch")
        if current_roles_digest is not None:
            if journal["roles_existed"] and roles_backup_info is None:
                allowed_roles = {journal["roles_backup_digest"]}
            else:
                allowed_roles = {journal["roles_content_digest"]}
            if current_roles_digest not in allowed_roles:
                raise InstallError("rollback role target differs", code="recovery_mismatch")
        elif journal["roles_existed"] and roles_backup_info is None:
            raise InstallError("rollback roles and backup are both missing", code="recovery_mismatch")
        for current, existed, backup_digest, new_digest in (
            (current_config, "config_existed", "config_backup_digest", "config_digest"),
            (current_metadata, "metadata_existed", "metadata_backup_digest", "metadata_digest"),
        ):
            if current is None:
                if journal[existed]:
                    raise InstallError("rollback file target is missing", code="recovery_mismatch")
                continue
            digest = _sha256(current)
            allowed_files = {journal[new_digest]}
            if journal[existed]:
                allowed_files.add(journal[backup_digest])
            if digest not in allowed_files:
                raise InstallError("rollback file target differs", code="recovery_mismatch")
    return transaction_paths


def _restore(owned: Paths, journal: Mapping[str, Any], transaction_paths: Mapping[str, pathlib.Path]) -> None:
    package_backup = transaction_paths["package_backup"]
    roles_backup = transaction_paths["roles_backup"]
    if _safe_leaf(package_backup, directory=True) is not None:
        if _safe_leaf(owned.package, directory=True) is not None:
            _remove_tree(owned.package, owned.home, kind="package")
        os.replace(package_backup, owned.package)
    elif not journal["package_existed"] and _safe_leaf(owned.package, directory=True) is not None:
        _remove_tree(owned.package, owned.home, kind="package")
    if _safe_leaf(roles_backup, directory=True) is not None:
        if _safe_leaf(owned.roles, directory=True) is not None:
            _remove_tree(owned.roles, owned.home, kind="roles")
        os.replace(roles_backup, owned.roles)
    elif not journal["roles_existed"] and _safe_leaf(owned.roles, directory=True) is not None:
        _remove_tree(owned.roles, owned.home, kind="roles")
    if journal["config_existed"]:
        _atomic(owned.config, base64.b64decode(journal["config_backup"], validate=True), mode=0o600)
    else:
        owned.config.unlink(missing_ok=True)
    if journal["metadata_existed"]:
        _atomic(owned.metadata, base64.b64decode(journal["metadata_backup"], validate=True), mode=0o600)
    else:
        owned.metadata.unlink(missing_ok=True)
    for key, parent in (("package_new", owned.package.parent), ("roles_new", owned.roles.parent)):
        if _safe_leaf(transaction_paths[key], directory=True) is not None:
            _remove_staging(transaction_paths[key], parent)


def _rollback_converged(owned: Paths, journal: Mapping[str, Any]) -> bool:
    package = verify_directory(owned.package) if _safe_leaf(owned.package, directory=True) is not None else None
    roles = _roles_digest(owned.roles) if _safe_leaf(owned.roles, directory=True, private=True) is not None else None
    config = _safe_file_bytes(owned.config, maximum=4 * 1024 * 1024)
    metadata = _safe_file_bytes(owned.metadata, maximum=1024 * 1024, private=True)
    transaction_paths = _recovery_paths(owned, journal)
    return (
        (package is not None) == journal["package_existed"]
        and (not package or secrets.compare_digest(package.content_digest, journal["package_backup_digest"]))
        and (roles is not None) == journal["roles_existed"]
        and (roles is None or secrets.compare_digest(roles, journal["roles_backup_digest"]))
        and (config is not None) == journal["config_existed"]
        and (config is None or secrets.compare_digest(_sha256(config), journal["config_backup_digest"]))
        and (metadata is not None) == journal["metadata_existed"]
        and (metadata is None or secrets.compare_digest(_sha256(metadata), journal["metadata_backup_digest"]))
        and all(_safe_leaf(path, directory=True) is None for path in transaction_paths.values())
    )


def _finalize_converged(owned: Paths, journal: Mapping[str, Any]) -> bool:
    try:
        package = verify_directory(owned.package)
        metadata = _read_metadata(owned)
        config = _safe_file_bytes(owned.config, maximum=4 * 1024 * 1024)
        metadata_bytes = _safe_file_bytes(owned.metadata, maximum=1024 * 1024, private=True)
        return bool(
            metadata
            and config is not None
            and metadata_bytes is not None
            and secrets.compare_digest(package.content_digest, journal["package_content_digest"])
            and secrets.compare_digest(_roles_digest(owned.roles), journal["roles_content_digest"])
            and secrets.compare_digest(_sha256(config), journal["config_digest"])
            and secrets.compare_digest(_sha256(metadata_bytes), journal["metadata_digest"])
            and metadata["package_digest"] == journal["package_digest"]
            and metadata["version"] == package.version
            and metadata["source_commit"] == package.source_commit
            and metadata["content_digest"] == package.content_digest
            and _config_current(owned.config)
        )
    except InstallError:
        return False


def ensure_global(package: VerifiedPackage, *, between_sessions: bool) -> dict[str, Any]:
    if not between_sessions:
        raise InstallError("close Codex and pass --between-sessions", code="between_sessions_required", exit_code=2)
    owned = paths()
    with _install_lock(owned):
        installed = inspect(owned)
        if "paths:unsafe_path" in installed["mismatches"]:
            raise InstallError("owned install path is unsafe", code="unsafe_path")
        package_info = _safe_leaf(owned.package, directory=True, error_code="unsafe_path")
        roles_info = _safe_leaf(owned.roles, directory=True, private=True, error_code="unsafe_path")
        config_info = _safe_leaf(owned.config, directory=False, error_code="unsafe_path")
        metadata_info = _safe_leaf(owned.metadata, directory=False, private=True, error_code="unsafe_path")
        journal_info = _safe_leaf(owned.journal, directory=False, private=True, error_code="unsafe_path")
        if installed["version"] and tuple(map(int, package.version.split("."))) < tuple(map(int, installed["version"].split("."))):
            raise InstallError("downgrade is not supported", code="downgrade_rejected", exit_code=2)
        if installed["current"] and installed["version"] == package.version and installed["source_commit"] == package.source_commit:
            return {**installed, "changed": False, "new_session_required": False}
        if journal_info is not None:
            raise InstallError("an install recovery journal is pending", code="recovery_required")
        _safe_directory(owned.control, owned.home, private=True)
        _safe_directory(owned.package.parent, owned.home)
        _safe_directory(owned.roles.parent, owned.home)
        _safe_directory(owned.config.parent, owned.home)
        prior_metadata = _read_metadata(owned) if metadata_info is not None else None
        prior_package = None
        if package_info is not None:
            try:
                prior_package = verify_directory(owned.package)
            except InstallError as exc:
                raise InstallError("existing package target has unverified drift", code="ambiguous_target") from exc
            if (
                prior_metadata is None
                or prior_metadata["version"] != prior_package.version
                or prior_metadata["source_commit"] != prior_package.source_commit
                or prior_metadata["content_digest"] != prior_package.content_digest
            ):
                raise InstallError("existing package ownership evidence differs", code="ambiguous_target")
        elif prior_metadata is not None:
            raise InstallError("metadata exists without its owned package", code="ambiguous_target")
        if roles_info is not None and (prior_package is None or not _valid_roles(owned.roles, prior_package)):
            raise InstallError("existing role target has unverified drift", code="ambiguous_target")
        prior_roles_digest = _roles_digest(owned.roles) if roles_info is not None else None
        transaction = secrets.token_hex(12)
        package_new = owned.package.parent / f".coordinator.new.{transaction}"
        roles_new = owned.roles.parent / f".coordinator.new.{transaction}"
        package_backup = owned.package.parent / f".coordinator.backup.{transaction}"
        roles_backup = owned.roles.parent / f".coordinator.backup.{transaction}"
        if config_info is not None and config_info.st_size > 4 * 1024 * 1024:
            raise InstallError("Codex config exceeds the safe merge limit", code="capacity_exceeded")
        config_old = owned.config.read_bytes() if config_info is not None else b""
        merged = _config_merge(config_old)
        metadata_old = owned.metadata.read_bytes() if metadata_info is not None else b""
        metadata_new = _json_bytes(_metadata(package, transaction))
        try:
            _write_package(package_new, package)
            _write_roles(roles_new, package)
        except BaseException:
            _remove_staging(package_new, owned.package.parent)
            _remove_staging(roles_new, owned.roles.parent)
            raise
        journal = {
            "schema_version": INSTALL_SCHEMA,
            "transaction": transaction,
            "package_digest": package.package_digest,
            "package_content_digest": package.content_digest,
            "roles_content_digest": _role_digest_from_files(package.files),
            "config_digest": _sha256(merged),
            "metadata_digest": _sha256(metadata_new),
            "phase": "prepared",
            "package_backup": str(package_backup),
            "roles_backup": str(roles_backup),
            "package_existed": package_info is not None,
            "package_backup_digest": prior_package.content_digest if prior_package else None,
            "roles_existed": roles_info is not None,
            "roles_backup_digest": prior_roles_digest,
            "config_existed": config_info is not None,
            "config_backup": base64.b64encode(config_old).decode("ascii"),
            "config_backup_digest": _sha256(config_old) if config_info is not None else None,
            "metadata_existed": metadata_info is not None,
            "metadata_backup": base64.b64encode(metadata_old).decode("ascii"),
            "metadata_backup_digest": _sha256(metadata_old) if metadata_info is not None else None,
        }
        _safe_directory(owned.journal.parent, owned.home, private=True)
        _atomic(owned.journal, _json_bytes(journal))
        try:
            if package_info is not None:
                os.replace(owned.package, package_backup)
            if roles_info is not None:
                os.replace(owned.roles, roles_backup)
            os.replace(package_new, owned.package)
            os.replace(roles_new, owned.roles)
            journal["phase"] = "surface_replaced"
            _atomic(owned.journal, _json_bytes(journal))
            _atomic(owned.config, merged, mode=0o600)
            _atomic(owned.metadata, metadata_new, mode=0o600)
            journal["phase"] = "committed"
            _atomic(owned.journal, _json_bytes(journal))
        except BaseException as exc:
            try:
                transaction_paths = _revalidate_recovery(owned, journal, "rollback")
                _restore(owned, journal, transaction_paths)
                if _rollback_converged(owned, journal):
                    owned.journal.unlink()
            except (InstallError, OSError):
                pass
            if isinstance(exc, OSError):
                raise InstallError("install transaction failed at the filesystem boundary", code="io_error") from exc
            raise
        transaction_paths = _revalidate_recovery(owned, journal, "finalize")
        if not _finalize_converged(owned, journal):
            raise InstallError("install did not converge to the recorded package", code="install_incomplete")
        if _safe_leaf(transaction_paths["package_backup"], directory=True) is not None:
            _remove_tree(transaction_paths["package_backup"], owned.home, kind="package")
        if _safe_leaf(transaction_paths["roles_backup"], directory=True) is not None:
            _remove_tree(transaction_paths["roles_backup"], owned.home, kind="roles")
        for key, parent in (("package_new", owned.package.parent), ("roles_new", owned.roles.parent)):
            if _safe_leaf(transaction_paths[key], directory=True) is not None:
                _remove_staging(transaction_paths[key], parent)
        _revalidate_recovery(owned, journal, "finalize")
        if not _finalize_converged(owned, journal):
            raise InstallError("install cleanup changed the recorded package", code="install_incomplete")
        owned.journal.unlink()
    result = inspect(owned)
    if not result["current"]:
        raise InstallError("post-install verification failed", code="install_incomplete")
    return {**result, "changed": True, "new_session_required": True}


def recovery_status() -> tuple[int, dict[str, Any]]:
    owned = paths()
    journal = _journal(owned)
    if not journal:
        return 0, {"pending": False, "follow_up": None}
    action = "finalize" if journal["phase"] == "committed" else "rollback"
    command = (
        f"install.py recover-install --action {action} --transaction "
        + journal["transaction"]
        + " --digest "
        + journal["package_digest"]
        + " --between-sessions"
    )
    return 1, {"pending": True, "transaction": journal["transaction"], "digest": journal["package_digest"], "phase": journal["phase"], "action": action, "follow_up": command}


def recover(*, action: str, transaction: str, digest: str, between_sessions: bool) -> dict[str, Any]:
    if not between_sessions:
        raise InstallError("close Codex and pass --between-sessions", code="between_sessions_required", exit_code=2)
    owned = paths()
    journal = _journal(owned)
    if not journal:
        raise InstallError("no install recovery is pending", code="no_recovery", exit_code=1)
    expected_action = "finalize" if journal["phase"] == "committed" else "rollback"
    if action != expected_action or transaction != journal["transaction"] or not secrets.compare_digest(digest, journal["package_digest"]):
        raise InstallError("recovery evidence differs from the journal", code="recovery_mismatch")
    with _install_lock(owned):
        observed = _journal(owned)
        if observed != journal:
            raise InstallError("recovery journal changed", code="recovery_mismatch")
        transaction_paths = _revalidate_recovery(owned, journal, action)
        if action == "rollback":
            _restore(owned, journal, transaction_paths)
            if not _rollback_converged(owned, journal):
                raise InstallError("rollback did not converge to the recorded prior install", code="recovery_mismatch")
        else:
            if not _finalize_converged(owned, journal):
                raise InstallError("committed install does not match finalize evidence", code="recovery_mismatch")
            if _safe_leaf(transaction_paths["package_backup"], directory=True) is not None:
                _remove_tree(transaction_paths["package_backup"], owned.home, kind="package")
            if _safe_leaf(transaction_paths["roles_backup"], directory=True) is not None:
                _remove_tree(transaction_paths["roles_backup"], owned.home, kind="roles")
            for key, parent in (("package_new", owned.package.parent), ("roles_new", owned.roles.parent)):
                if _safe_leaf(transaction_paths[key], directory=True) is not None:
                    _remove_staging(transaction_paths[key], parent)
            _revalidate_recovery(owned, journal, "finalize")
            if not _finalize_converged(owned, journal):
                raise InstallError("finalize cleanup did not preserve the committed install", code="recovery_mismatch")
        if _journal(owned) != journal:
            raise InstallError("recovery journal changed before convergence was recorded", code="recovery_mismatch")
        owned.journal.unlink()
    return {"recovered": True, "action": action, "transaction": transaction, "new_session_required": True}


def _emit(command: str, code: str, data: Any, *, exit_code: int = 0, as_json: bool = False, new_session: bool = False) -> int:
    payload = {
        "command": command,
        "status": "ok" if exit_code == 0 else "negative" if exit_code == 1 else "error",
        "code": code,
        "data": data,
        "warnings": [],
        "new_session_required": new_session,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif exit_code in (0, 1):
        print(f"{command}: {code.replace('_', ' ')}")
        if isinstance(data, dict):
            for key, value in data.items():
                if value is not None:
                    print(f"{key}={value}")
    else:
        print(f"{command}: {data.get('message', code)}", file=sys.stderr)
    return exit_code


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source")
    parser.add_argument("--source-url")


def build_parser() -> argparse.ArgumentParser:
    parser = _OutcomeArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ensure = sub.add_parser("ensure-global")
    _source_arguments(ensure)
    ensure.add_argument("--between-sessions", action="store_true")
    ensure.add_argument("--json", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    probe = sub.add_parser("home-probe")
    probe.add_argument("--json", action="store_true")
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--json", action="store_true")
    recovery = sub.add_parser("recovery-status")
    recovery.add_argument("--json", action="store_true")
    recover_parser = sub.add_parser("recover-install")
    recover_parser.add_argument("--action", choices=("rollback", "finalize"), required=True)
    recover_parser.add_argument("--transaction", required=True)
    recover_parser.add_argument("--digest", required=True)
    recover_parser.add_argument("--between-sessions", action="store_true")
    recover_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(arguments)
    except _InvocationError as exc:
        command = next((item for item in arguments if item and not item.startswith("-")), "unknown")
        return _emit(
            command,
            "invalid_invocation",
            {"message": str(exc)},
            exit_code=2,
            as_json="--json" in arguments,
        )
    try:
        if sys.version_info < (3, 11):
            raise InstallError("Python 3.11 or newer is required", code="unsupported_python")
        if args.command == "ensure-global":
            package = acquire_source(args.source, args.source_url)
            result = ensure_global(package, between_sessions=args.between_sessions)
            return _emit(args.command, "install_current" if not result["changed"] else "install_changed", result, as_json=args.json, new_session=result["new_session_required"])
        if args.command == "status":
            result = inspect()
            return _emit(args.command, "install_current" if result["current"] else "install_drift", result, exit_code=0 if result["current"] else 1, as_json=args.json)
        if args.command == "home-probe":
            owned = paths()
            data = {"home": str(owned.home), "package": str(owned.package), "roles": str(owned.roles), "config": str(owned.config), "control": str(owned.control)}
            return _emit(args.command, "home_resolved", data, as_json=args.json)
        if args.command == "cleanup":
            owned = paths()
            cache = owned.control / "cache"
            removed = []
            if _safe_existing_chain(cache, owned.home, error_code="unsafe_path"):
                marker = cache / ".coordinator-cache"
                if cache.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != "coordinator-v3\n":
                    raise InstallError("refusing unknown cache tree", code="unsafe_path")
                _remove_tree(cache, owned.home, kind="cache")
                removed.append("cache")
            return _emit(args.command, "cleanup_complete", {"removed": removed}, as_json=args.json)
        if args.command == "recovery-status":
            exit_code, result = recovery_status()
            return _emit(args.command, "recovery_pending" if exit_code else "recovery_clean", result, exit_code=exit_code, as_json=args.json)
        result = recover(action=args.action, transaction=args.transaction, digest=args.digest, between_sessions=args.between_sessions)
        return _emit(args.command, "install_recovered", result, as_json=args.json, new_session=True)
    except InstallError as exc:
        return _emit(args.command, exc.code, {"message": str(exc)}, exit_code=exc.exit_code, as_json=getattr(args, "json", False))
    except (OSError, UnicodeError, zipfile.BadZipFile, RecursionError) as exc:
        wrapped = InstallError("install operation failed at a validated I/O boundary", code="io_error")
        wrapped.__cause__ = exc
        return _emit(args.command, wrapped.code, {"message": str(wrapped)}, exit_code=wrapped.exit_code, as_json=getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
