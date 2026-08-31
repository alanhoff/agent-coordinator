"""Strict, durable workflow state with controller and mutation fencing."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import stat
import sys
import unicodedata
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from coordinator.routing.selector import STAGES, RoutingError, choose

SCHEMA_VERSION = 6
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_COMMAND_BYTES = MAX_STATE_BYTES
MAX_NODES = 128
MAX_EVENTS = 512
MAX_RECEIPTS = 2048
MAX_ATTEMPTS = 32
MAX_TEXT = 32_768
ROLES = (
    "architect",
    "designer",
    "documenter",
    "fixer",
    "implementer",
    "researcher",
    "reviewer",
    "validator",
)
NODE_STATUSES = ("pending", "ready", "running", "blocked", "done", "failed", "skipped", "cancelled")
TERMINAL_NODE_STATUSES = frozenset(("done", "failed", "skipped", "cancelled"))
SUCCESS_NODE_STATUSES = frozenset(("done", "skipped", "cancelled"))
WORKFLOW_STATUSES = ("planning", "running", "blocked", "completed", "aborted")
LAUNCH_STATES = ("unclaimed", "claimed", "reconcile_required", "bound", "running", "terminal")
COMPLEXITY_DIMENSIONS = ("breadth", "change_surface", "coupling", "novelty", "verification")
AMBIGUITY_FACTORS = ("objective", "inputs", "boundaries", "dependencies", "acceptance")
ASSESSMENT_STATES = ("executable", "split_required", "refinement_required", "stale", "decomposed")
ASSESSMENT_RUBRIC_VERSION = 2
COVERAGE_FIELDS = ("requirements", "outputs", "acceptance")
OBLIGATION_FIELDS = (
    "objectives",
    "requirements",
    "inputs",
    "outputs",
    "constraints",
    "non_goals",
    "acceptance",
    "write_scopes",
)
PLANNING_FIXED_POINT_ERROR = (
    "workflow planning fixed point requires every non-blocked assessable leaf "
    "to have a current executable assessment"
)
FINISH_EVENT_SEPARATOR = "; validation: "
ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_RESERVED_SCOPE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {f"COM{index}" for index in "¹²³"}
    | {f"LPT{index}" for index in "¹²³"}
)
WINDOWS_FORBIDDEN_SCOPE_CHARACTERS = frozenset('<>:"|?*')


class StateError(RuntimeError):
    """A stable workflow-state failure."""

    def __init__(self, message: str, *, code: str = "invalid_state", exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _assert_no_link_components(path: pathlib.Path) -> None:
    absolute = path.absolute()
    parts = absolute.parts
    current = pathlib.Path(parts[0])
    for part in parts[1:]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise StateError(f"path contains an unsafe directory: {current}", code="unsafe_path", exit_code=20)


def _parse_time(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise StateError(f"{field} must be an ISO-8601 string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise StateError(f"{field} must include a timezone")
    return parsed


def _decode_json(data: bytes | str, field: str, *, exit_code: int = 20) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise StateError(f"{field} contains duplicate key {key!r}", code="corrupt_state", exit_code=exit_code)
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise StateError(
            f"{field} contains non-standard numeric constant {value}",
            code="corrupt_state",
            exit_code=exit_code,
        )

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except StateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise StateError(f"{field} is not valid UTF-8 JSON", code="corrupt_state", exit_code=exit_code) from exc


def _json_bytes(value: Any, *, indent: int | None = 2) -> bytes:
    try:
        suffix = "\n" if indent is not None else ""
        return (
            json.dumps(
                value,
                indent=indent,
                sort_keys=True,
                separators=None if indent is not None else (",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + suffix
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StateError("state is not canonical JSON data") from exc


def _acquire_advisory_lock(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError, OSError) as exc:
        raise StateError(
            "workflow is locked by another controller",
            code="concurrent_controller",
            exit_code=20,
        ) from exc


def _release_advisory_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _keys(value: Any, expected: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise StateError(f"{field} must be an object")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise StateError(f"{field} has " + "; ".join(detail))
    return value


def _text(value: Any, field: str, *, blank: bool = False, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        qualifier = "a string" if blank else "a non-blank string"
        raise StateError(f"{field} must be {qualifier} no longer than {maximum} characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise StateError(f"{field} must contain only Unicode scalar values")
    return value


def _identifier(value: Any, field: str) -> str:
    text = _text(value, field, maximum=128)
    if not ID_RE.fullmatch(text):
        raise StateError(f"{field} is not a safe identifier")
    return text


def _repository_identity(path: pathlib.Path, info: os.stat_result) -> str:
    inode = getattr(info, "st_ino", 0)
    device = getattr(info, "st_dev", None)
    if not isinstance(inode, int) or inode <= 0 or not isinstance(device, int):
        raise StateError(
            "repository filesystem does not expose a stable object identity",
            code="invalid_repository",
            exit_code=20,
        )
    canonical = os.path.normcase(str(path)) if os.name == "nt" else str(path)
    digest = hashlib.sha256()
    for field in (
        b"repository-object-v1",
        os.fsencode(canonical),
        str(device).encode(),
        str(inode).encode(),
    ):
        digest.update(len(field).to_bytes(8, "big"))
        digest.update(field)
    return digest.hexdigest()


def canonical_repository(path: pathlib.Path) -> dict[str, str]:
    resolved = pathlib.Path(os.path.realpath(path.expanduser())).resolve()
    try:
        info = resolved.lstat()
        if os.name != "nt":
            _assert_no_link_components(resolved)
    except (FileNotFoundError, OSError) as exc:
        raise StateError(
            f"unable to inspect repository path: {resolved}",
            code="invalid_repository",
            exit_code=20,
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise StateError(f"repository path is not a directory: {resolved}", code="invalid_repository")
    return {"path": str(resolved), "identity": _repository_identity(resolved, info)}


def _repository_case_sensitive(repository_path: str) -> bool:
    if os.name == "nt":
        return False
    token = f".agent-coordinator-case-{secrets.token_hex(8)}"
    probe = pathlib.Path(repository_path) / token
    alternate = pathlib.Path(repository_path) / token.upper()
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    close_error: OSError | None = None
    try:
        case_sensitive = not alternate.exists()
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            close_error = exc
        try:
            probe.unlink()
        except OSError as exc:
            raise StateError(
                "unable to remove repository case-sensitivity probe",
                code="io_error",
                exit_code=20,
            ) from exc
        if close_error is not None:
            raise StateError(
                "unable to close repository case-sensitivity probe",
                code="io_error",
                exit_code=20,
            ) from close_error
    return case_sensitive


def _scope_path(repository_path: str, scope: str) -> pathlib.Path | None:
    current = pathlib.Path(repository_path)
    parts = pathlib.PurePosixPath(scope).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateError("unable to inspect node write scope", code="io_error", exit_code=20) from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise StateError("node write scope contains an unsafe link", code="unsafe_path", exit_code=20)
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise StateError("node write scope has a non-directory parent", code="unsafe_path", exit_code=20)
        if index == len(parts) - 1 and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise StateError("node write scope is not a regular file or directory", code="unsafe_path", exit_code=20)
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise StateError("node write scope contains a hard-linked file", code="unsafe_path", exit_code=20)
    return current


def _hash_scope_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _hash_scope_entry(digest: Any, path: pathlib.Path, relative: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise StateError("unable to inspect node write scope", code="io_error", exit_code=20) from exc
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not _is_reparse(info):
        _hash_scope_field(digest, b"directory")
        _hash_scope_field(digest, os.fsencode(relative))
        _hash_scope_field(digest, f"{mode:o}".encode("ascii"))
        try:
            children = sorted(path.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError as exc:
            raise StateError("unable to enumerate node write scope", code="io_error", exit_code=20) from exc
        for child in children:
            child_relative = f"{relative}/{child.name}" if relative else child.name
            _hash_scope_entry(digest, child, child_relative)
        return
    if stat.S_ISREG(info.st_mode):
        content_digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    content_digest.update(chunk)
        except OSError as exc:
            raise StateError("unable to read node write scope", code="io_error", exit_code=20) from exc
        _hash_scope_field(digest, b"file")
        _hash_scope_field(digest, os.fsencode(relative))
        _hash_scope_field(digest, f"{mode:o}".encode("ascii"))
        _hash_scope_field(digest, content_digest.digest())
        return
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise StateError("unable to inspect link in node write scope", code="io_error", exit_code=20) from exc
        _hash_scope_field(digest, b"link")
        _hash_scope_field(digest, os.fsencode(relative))
        _hash_scope_field(digest, os.fsencode(target))
        return
    _hash_scope_field(digest, b"special")
    _hash_scope_field(digest, os.fsencode(relative))
    _hash_scope_field(digest, f"{info.st_mode:o}".encode("ascii"))


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _open_anchored_entry(parent: int, name: str, expected: os.stat_result) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if stat.S_ISDIR(expected.st_mode):
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
        actual = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise StateError(
            "node write scope changed while it was inspected",
            code="io_error",
            exit_code=20,
        ) from exc
    if not _same_object(expected, actual):
        os.close(descriptor)
        raise StateError(
            "node write scope changed while it was inspected",
            code="io_error",
            exit_code=20,
        )
    return descriptor


def _hash_anchored_entry(
    digest: Any,
    parent: int,
    name: str,
    relative: str,
    info: os.stat_result,
) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not _is_reparse(info):
        descriptor = _open_anchored_entry(parent, name, info)
        try:
            _hash_scope_field(digest, b"directory")
            _hash_scope_field(digest, os.fsencode(relative))
            _hash_scope_field(digest, f"{mode:o}".encode("ascii"))
            try:
                children = sorted(os.listdir(descriptor), key=os.fsencode)
            except OSError as exc:
                raise StateError(
                    "unable to enumerate node write scope",
                    code="io_error",
                    exit_code=20,
                ) from exc
            for child in children:
                try:
                    child_info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
                except OSError as exc:
                    raise StateError(
                        "unable to inspect node write scope",
                        code="io_error",
                        exit_code=20,
                    ) from exc
                child_relative = f"{relative}/{child}" if relative else child
                _hash_anchored_entry(digest, descriptor, child, child_relative, child_info)
        finally:
            os.close(descriptor)
        return
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise StateError(
                "node write scope contains a hard-linked file",
                code="unsafe_path",
                exit_code=20,
            )
        descriptor = _open_anchored_entry(parent, name, info)
        content_digest = hashlib.sha256()
        try:
            while chunk := os.read(descriptor, 1024 * 1024):
                content_digest.update(chunk)
        except OSError as exc:
            raise StateError("unable to read node write scope", code="io_error", exit_code=20) from exc
        finally:
            os.close(descriptor)
        _hash_scope_field(digest, b"file")
        _hash_scope_field(digest, os.fsencode(relative))
        _hash_scope_field(digest, f"{mode:o}".encode("ascii"))
        _hash_scope_field(digest, content_digest.digest())
        return
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        try:
            target = os.readlink(name, dir_fd=parent)
        except OSError as exc:
            raise StateError("unable to inspect link in node write scope", code="io_error", exit_code=20) from exc
        _hash_scope_field(digest, b"link")
        _hash_scope_field(digest, os.fsencode(relative))
        _hash_scope_field(digest, os.fsencode(target))
        return
    _hash_scope_field(digest, b"special")
    _hash_scope_field(digest, os.fsencode(relative))
    _hash_scope_field(digest, f"{info.st_mode:o}".encode("ascii"))


def _anchored_scope_fingerprint(root: int, scope: str) -> str | None:
    current = os.dup(root)
    try:
        parts = pathlib.PurePosixPath(scope).parts
        for index, part in enumerate(parts):
            try:
                info = os.stat(part, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise StateError(
                    "unable to inspect node write scope",
                    code="io_error",
                    exit_code=20,
                ) from exc
            final = index == len(parts) - 1
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise StateError(
                    "node write scope contains an unsafe link",
                    code="unsafe_path",
                    exit_code=20,
                )
            if not final and not stat.S_ISDIR(info.st_mode):
                raise StateError(
                    "node write scope has a non-directory parent",
                    code="unsafe_path",
                    exit_code=20,
                )
            if final:
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise StateError(
                        "node write scope is not a regular file or directory",
                        code="unsafe_path",
                        exit_code=20,
                    )
                digest = hashlib.sha256()
                _hash_anchored_entry(digest, current, part, "", info)
                return digest.hexdigest()
            child = _open_anchored_entry(current, part, info)
            os.close(current)
            current = child
    finally:
        os.close(current)
    return None


@contextmanager
def _anchored_repository(repository: Mapping[str, str]) -> Iterator[int | None]:
    path = pathlib.Path(repository["path"])
    if os.name == "nt":
        current = canonical_repository(path)
        if current["identity"] != repository["identity"]:
            raise StateError(
                "workflow repository object changed",
                code="invalid_repository",
                exit_code=20,
            )
        yield None
        after = canonical_repository(path)
        if after["identity"] != repository["identity"]:
            raise StateError(
                "workflow repository object changed while it was inspected",
                code="invalid_repository",
                exit_code=20,
            )
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise StateError(
            "unable to open workflow repository object",
            code="invalid_repository",
            exit_code=20,
        ) from exc
    try:
        if (
            not stat.S_ISDIR(info.st_mode)
            or _repository_identity(path, info) != repository["identity"]
        ):
            raise StateError(
                "workflow repository object changed",
                code="invalid_repository",
                exit_code=20,
            )
        yield descriptor
    finally:
        os.close(descriptor)


def _scope_fingerprint(
    repository_path: str,
    scope: str,
    *,
    expected_identity: str | None = None,
) -> str | None:
    repository = canonical_repository(pathlib.Path(repository_path))
    if expected_identity is not None and repository["identity"] != expected_identity:
        raise StateError(
            "workflow repository object changed",
            code="invalid_repository",
            exit_code=20,
        )
    if os.name != "nt":
        with _anchored_repository(repository) as descriptor:
            assert descriptor is not None
            return _anchored_scope_fingerprint(descriptor, scope)
    path = _scope_path(repository_path, scope)
    if path is None:
        return None
    digest = hashlib.sha256()
    _hash_scope_entry(digest, path, "")
    return digest.hexdigest()


def _scope_snapshot(state: Mapping[str, Any], node: Mapping[str, Any]) -> dict[str, str | None]:
    repository = state["repository"]
    with _anchored_repository(repository) as descriptor:
        if descriptor is not None:
            return {
                scope: _anchored_scope_fingerprint(descriptor, scope)
                for scope in node["write_scopes"]
            }
        return {
            scope: _scope_fingerprint(
                repository["path"],
                scope,
                expected_identity=repository["identity"],
            )
            for scope in node["write_scopes"]
        }


def _complete_scope_evidence(state: Mapping[str, Any], node: Mapping[str, Any]) -> dict[str, dict[str, str | None]]:
    attempt = node["attempts"][-1]
    current = _scope_snapshot(state, node)
    missing = [scope for scope, fingerprint in current.items() if fingerprint is None]
    if missing:
        raise StateError(
            f"done node {node['id']} has no materialized file or directory in write scope(s): "
            + ", ".join(missing)
        )
    unchanged = [
        scope
        for scope, fingerprint in current.items()
        if fingerprint == attempt["scope_baseline"][scope]
    ]
    if unchanged:
        raise StateError(
            f"done node {node['id']} has no attempt-scoped change in write scope(s): "
            + ", ".join(unchanged)
        )
    return {
        scope: {"before": attempt["scope_baseline"][scope], "after": fingerprint}
        for scope, fingerprint in current.items()
    }


def _verify_finish_scopes(state: Mapping[str, Any]) -> None:
    missing = [
        f"{node_id}:{scope}"
        for node_id, node in state["nodes"].items()
        if node["status"] == "done"
        for scope, fingerprint in _scope_snapshot(state, node).items()
        if fingerprint is None
    ]
    if missing:
        raise StateError(
            "finish requires every done artifact scope to remain materialized: "
            + ", ".join(missing)
        )


def _canonical_scope(value: Any, field: str, *, platform: str | None = None) -> str:
    raw = unicodedata.normalize("NFC", _text(value, field, maximum=512).replace("\\", "/"))
    if "\0" in raw:
        raise StateError(f"{field} contains a NUL byte")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise StateError(f"{field} must be repository-relative")
    parts = pathlib.PurePosixPath(raw).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise StateError(f"{field} contains an unsafe path segment")
    if (os.name if platform is None else platform) == "nt":
        for part in parts:
            if (
                part.endswith((" ", "."))
                or any(character in WINDOWS_FORBIDDEN_SCOPE_CHARACTERS for character in part)
                or any(ord(character) < 32 for character in part)
                or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_SCOPE_NAMES
            ):
                raise StateError(f"{field} contains a non-portable path segment: {part!r}")
    return "/".join(parts).rstrip("/")


def _scope(value: Any, field: str, *, case_sensitive: bool, platform: str | None = None) -> str:
    normalized = _canonical_scope(value, field, platform=platform)
    return normalized if case_sensitive else normalized.casefold()


def scopes_overlap(
    left: str,
    right: str,
    *,
    case_sensitive: bool | None = None,
    platform: str | None = None,
) -> bool:
    sensitive = os.name != "nt" if case_sensitive is None else case_sensitive
    a = _scope(left, "write scope", case_sensitive=sensitive, platform=platform)
    b = _scope(right, "write scope", case_sensitive=sensitive, platform=platform)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _text_list(
    value: Any,
    field: str,
    *,
    required: bool = False,
    identifiers: bool = False,
    maximum: int = 128,
    item_maximum: int = 2048,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or (required and not value):
        qualifier = "1.." if required else "0.."
        raise StateError(f"{field} must be a list with {qualifier}{maximum} entries")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(
            _identifier(item, f"{field}[{index}]")
            if identifiers
            else _text(item, f"{field}[{index}]", maximum=item_maximum)
        )
    if len(set(result)) != len(result):
        raise StateError(f"{field} must not contain duplicates")
    return result


def _validate_spec(value: Any, field: str = "spec") -> Mapping[str, Any]:
    spec = _keys(
        value,
        {"objective", "inputs", "outputs", "constraints", "non_goals", "requirement_ids", "open_questions"},
        field,
    )
    _text(spec["objective"], f"{field}.objective")
    _text_list(spec["inputs"], f"{field}.inputs")
    _text_list(spec["outputs"], f"{field}.outputs", required=True)
    _text_list(spec["constraints"], f"{field}.constraints")
    _text_list(spec["non_goals"], f"{field}.non_goals")
    _text_list(spec["requirement_ids"], f"{field}.requirement_ids", identifiers=True)
    _text_list(spec["open_questions"], f"{field}.open_questions")
    return spec


def _validate_dimensions(value: Any, field: str) -> dict[str, int]:
    dimensions = _keys(value, set(COMPLEXITY_DIMENSIONS), field)
    for name in COMPLEXITY_DIMENSIONS:
        score = dimensions[name]
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 4:
            raise StateError(f"{field}.{name} must be an integer from 0 through 4")
    return {name: dimensions[name] for name in COMPLEXITY_DIMENSIONS}


def _validate_ambiguity_factors(value: Any, field: str) -> dict[str, int]:
    factors = _keys(value, set(AMBIGUITY_FACTORS), field)
    for name in AMBIGUITY_FACTORS:
        score = factors[name]
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 4:
            raise StateError(f"{field}.{name} must be an integer from 0 through 4")
    return {name: factors[name] for name in AMBIGUITY_FACTORS}


def _validate_assessment_inputs(value: Any, field: str = "assessment") -> Mapping[str, Any]:
    assessment = _keys(value, {"dimensions", "ambiguity_factors", "rationale"}, field)
    _validate_dimensions(assessment["dimensions"], f"{field}.dimensions")
    _validate_ambiguity_factors(assessment["ambiguity_factors"], f"{field}.ambiguity_factors")
    _text(assessment["rationale"], f"{field}.rationale", maximum=4096)
    return assessment


def _planning_policy(state: Mapping[str, Any]) -> dict[str, int]:
    conventions = state["conventions"]
    return {
        "node_complexity_split_threshold": conventions["node_complexity_split_threshold"],
        "dimension_complexity_split_threshold": conventions["dimension_complexity_split_threshold"],
        "node_ambiguity_refine_threshold": conventions["node_ambiguity_refine_threshold"],
        "factor_ambiguity_refine_threshold": conventions["factor_ambiguity_refine_threshold"],
        "max_refinement_depth": conventions["max_refinement_depth"],
    }


def _ordered_union(*values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for value in values for item in value))


def _effective_obligations(node: Mapping[str, Any]) -> dict[str, list[str]]:
    carried = node["lineage"]["obligations"]
    return {
        "objectives": _ordered_union([node["spec"]["objective"]], carried["objectives"]),
        "requirements": _ordered_union(node["spec"]["requirement_ids"], carried["requirements"]),
        "inputs": _ordered_union(node["spec"]["inputs"], carried["inputs"]),
        "outputs": _ordered_union(node["spec"]["outputs"], carried["outputs"]),
        "constraints": _ordered_union(node["spec"]["constraints"], carried["constraints"]),
        "non_goals": _ordered_union(node["spec"]["non_goals"], carried["non_goals"]),
        "acceptance": _ordered_union(node["acceptance"], carried["acceptance"]),
        "write_scopes": _ordered_union(node["write_scopes"], carried["write_scopes"]),
    }


def _dependency_snapshot(node_id: str, node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "outputs": _effective_obligations(node)["outputs"],
        "disposition": node["status"] if node["status"] in TERMINAL_NODE_STATUSES else "nonterminal",
        "result": node["result"],
        "evidence": node["evidence"],
    }


def _assessment_input_digest(state: Mapping[str, Any], node: Mapping[str, Any]) -> str:
    obligations = _effective_obligations(node)
    requirements = {}
    for requirement_id in obligations["requirements"]:
        requirement = state["requirements"].get(requirement_id)
        requirements[requirement_id] = (
            None
            if requirement is None
            else {"text": requirement["text"], "source": requirement["source"]}
        )
    dependencies = [
        _dependency_snapshot(dependency, state["nodes"][dependency])
        for dependency in node["dependencies"]
    ]
    payload = {
        "spec": node["spec"],
        "acceptance": node["acceptance"],
        "obligations": node["lineage"]["obligations"],
        "requirements": requirements,
        "dependencies": dependencies,
        "write_scopes": node["write_scopes"],
        "dimensions": node["assessment"]["dimensions"],
        "ambiguity_factors": node["assessment"]["ambiguity_factors"],
        "planning_policy": _planning_policy(state),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _future_leaf(node: Mapping[str, Any]) -> bool:
    return (
        not node["lineage"]["child_ids"]
        and node["launch"]["state"] == "unclaimed"
        and node["status"] in ("pending", "ready", "blocked")
    )


def _assessable_leaf(node: Mapping[str, Any]) -> bool:
    return _future_leaf(node) or (
        not node["lineage"]["child_ids"]
        and node["status"] == "failed"
        and node["launch"]["state"] in ("unclaimed", "terminal")
    )


def _is_resolution_endpoint(node: Mapping[str, Any]) -> bool:
    return (
        _assessable_leaf(node)
        or node["launch"]["state"] in ("claimed", "reconcile_required", "bound", "running")
        or node["status"] == "done"
    )


def _node_resolves_completion(node: Mapping[str, Any]) -> bool:
    return node["status"] == "done" or (
        node["status"] == "skipped"
        and (bool(node["lineage"]["child_ids"]) or node["superseded_by"] is not None)
    )


def _raw_over_budget(state: Mapping[str, Any], node: Mapping[str, Any]) -> bool:
    policy = state["conventions"]
    dimensions = node["assessment"]["dimensions"]
    return (
        node["assessment"]["total"] >= policy["node_complexity_split_threshold"]
        or any(
            dimensions[name] >= policy["dimension_complexity_split_threshold"]
            for name in COMPLEXITY_DIMENSIONS
        )
    )


def _raw_requires_refinement(state: Mapping[str, Any], node: Mapping[str, Any]) -> bool:
    policy = state["conventions"]
    factors = node["assessment"]["ambiguity_factors"]
    return (
        bool(node["spec"]["open_questions"])
        or node["assessment"]["ambiguity_total"] >= policy["node_ambiguity_refine_threshold"]
        or any(
            factors[name] >= policy["factor_ambiguity_refine_threshold"]
            for name in AMBIGUITY_FACTORS
        )
    )


def _derived_assessment_state(state: Mapping[str, Any], node: Mapping[str, Any]) -> str:
    if node["lineage"]["child_ids"]:
        return "decomposed"
    if not _assessable_leaf(node):
        return node["assessment"]["state"]
    if node["assessment"]["input_digest"] != _assessment_input_digest(state, node):
        return "stale"
    if _raw_requires_refinement(state, node):
        return "refinement_required"
    if _raw_over_budget(state, node):
        return "split_required"
    return "executable"


def _assessment_shell(inputs: Mapping[str, Any]) -> dict[str, Any]:
    checked = _validate_assessment_inputs(inputs)
    dimensions = {name: checked["dimensions"][name] for name in COMPLEXITY_DIMENSIONS}
    ambiguity_factors = {
        name: checked["ambiguity_factors"][name]
        for name in AMBIGUITY_FACTORS
    }
    return {
        "rubric_version": ASSESSMENT_RUBRIC_VERSION,
        "dimensions": dimensions,
        "total": sum(dimensions.values()),
        "ambiguity_factors": ambiguity_factors,
        "ambiguity_total": sum(ambiguity_factors.values()),
        "ambiguity_peak": max(ambiguity_factors.values()),
        "rationale": checked["rationale"],
        "input_digest": "0" * 64,
        "state": "stale",
    }


def _build_assessment(
    state: Mapping[str, Any],
    node: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    assessment = _assessment_shell(inputs)
    mutable = dict(node)
    mutable["assessment"] = assessment
    assessment["input_digest"] = _assessment_input_digest(state, mutable)
    assessment["state"] = _derived_assessment_state(state, mutable)
    return assessment


def _assessment_is_current_executable(state: Mapping[str, Any], node_id: str) -> bool:
    node = state["nodes"][node_id]
    return (
        _assessable_leaf(node)
        and node["assessment"]["state"] == "executable"
        and node["assessment"]["input_digest"] == _assessment_input_digest(state, node)
        and _derived_assessment_state(state, node) == "executable"
    )


def _node_is_executable(state: Mapping[str, Any], node_id: str) -> bool:
    return _future_leaf(state["nodes"][node_id]) and _assessment_is_current_executable(state, node_id)


def _invalidate_assessment(state: Mapping[str, Any], node: dict[str, Any]) -> None:
    if _assessable_leaf(node):
        node["assessment"]["state"] = _derived_assessment_state(state, node)
        if node["status"] == "ready" and node["assessment"]["state"] != "executable":
            node["status"] = "pending"


def _invalidate_direct_dependents(state: dict[str, Any], node_id: str) -> None:
    for node in state["nodes"].values():
        if node_id in node["dependencies"]:
            _invalidate_assessment(state, node)


def _active_blocked_node_ids(state: Mapping[str, Any]) -> set[str]:
    return {
        item["node_id"]
        for item in state["blockers"]
        if item["status"] == "active" and item["node_id"] is not None
    }


def _workflow_dispatch_blocked(state: Mapping[str, Any]) -> bool:
    return state["status"] == "blocked" or any(
        item["status"] == "active" and item["node_id"] is None for item in state["blockers"]
    )


def _planning_at_fixed_point(state: Mapping[str, Any]) -> bool:
    blocked = _active_blocked_node_ids(state)
    return all(
        node["status"] == "blocked"
        or node_id in blocked
        or not _assessable_leaf(node)
        or _assessment_is_current_executable(state, node_id)
        for node_id, node in state["nodes"].items()
    )


def _live_depends(nodes: Mapping[str, Mapping[str, Any]], node_id: str, possible_parent: str) -> bool:
    pending = list(nodes[node_id]["dependencies"])
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == possible_parent:
            return True
        if current in seen or nodes[current]["status"] in SUCCESS_NODE_STATUSES:
            continue
        seen.add(current)
        pending.extend(nodes[current]["dependencies"])
    return False


def _depends_on(
    nodes: Mapping[str, Mapping[str, Any]],
    node_id: str,
    prerequisite: str,
) -> bool:
    if node_id not in nodes:
        return False
    if node_id == prerequisite:
        return True
    pending = list(nodes[node_id]["dependencies"])
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == prerequisite:
            return True
        if current in seen or current not in nodes:
            continue
        seen.add(current)
        pending.extend(nodes[current]["dependencies"])
    return False


def _child_reaches_prerequisite(
    nodes: Mapping[str, Mapping[str, Any]],
    child_id: str,
    prerequisite: str,
    child_ids: set[str],
) -> bool:
    pending = [child_id]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for dependency in nodes[current]["dependencies"]:
            if dependency == prerequisite:
                return True
            if dependency in child_ids:
                pending.append(dependency)
    return False


def graph_diagnostics(
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    case_sensitive: bool,
    platform: str | None = None,
) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    for node_id, node in nodes.items():
        for dependency in node["dependencies"]:
            if dependency not in nodes:
                missing.append({"node_id": node_id, "dependency": dependency})

    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: list[list[str]] = []

    def visit(node_id: str, trail: list[str]) -> None:
        if node_id in visiting:
            start = trail.index(node_id)
            cycle = trail[start:] + [node_id]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        if node_id in visited or node_id not in nodes:
            return
        visiting.add(node_id)
        for dependency in nodes[node_id]["dependencies"]:
            visit(dependency, trail + [dependency])
        visiting.remove(node_id)
        visited.add(node_id)

    for candidate in nodes:
        visit(candidate, [candidate])

    collisions: list[dict[str, str]] = []
    active = [
        node_id
        for node_id, node in nodes.items()
        if node["status"] not in TERMINAL_NODE_STATUSES
    ]
    if not missing:
        for index, left_id in enumerate(active):
            for right_id in active[index + 1 :]:
                if _live_depends(nodes, left_id, right_id) or _live_depends(nodes, right_id, left_id):
                    continue
                for left in nodes[left_id]["write_scopes"]:
                    for right in nodes[right_id]["write_scopes"]:
                        if scopes_overlap(
                            left,
                            right,
                            case_sensitive=case_sensitive,
                            platform=platform,
                        ):
                            collisions.append(
                                {"left": left_id, "right": right_id, "left_scope": left, "right_scope": right}
                            )
    return {"missing_dependencies": missing, "cycles": cycles, "write_scope_collisions": collisions}


def _frontier_nodes(state: Mapping[str, Any]) -> list[str]:
    nodes = state["nodes"]
    if _workflow_dispatch_blocked(state):
        return []
    blocked = _active_blocked_node_ids(state)
    ready = []
    for node_id, node in nodes.items():
        if (
            node["status"] not in ("pending", "ready")
            or node["launch"]["state"] != "unclaimed"
            or node["lineage"]["child_ids"]
            or node_id in blocked
        ):
            continue
        if all(nodes[dependency]["status"] in SUCCESS_NODE_STATUSES for dependency in node["dependencies"]):
            ready.append(node_id)
    return ready


def _critical_path_loads(state: Mapping[str, Any]) -> dict[str, int]:
    nodes = state["nodes"]
    dependents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        for dependency in node["dependencies"]:
            if dependency in dependents:
                dependents[dependency].append(node_id)
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def load(node_id: str) -> int:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            return 0
        visiting.add(node_id)
        node = nodes[node_id]
        own = 0
        if node["status"] not in SUCCESS_NODE_STATUSES and not node["lineage"]["child_ids"]:
            own = node["assessment"]["total"]
        downstream = 0
        if node["status"] not in SUCCESS_NODE_STATUSES:
            downstream = max((load(item) for item in dependents[node_id]), default=0)
        visiting.remove(node_id)
        memo[node_id] = own + downstream
        return memo[node_id]

    for candidate in nodes:
        load(candidate)
    return {node_id: memo[node_id] for node_id in sorted(memo)}


def ready_nodes(state: Mapping[str, Any]) -> list[str]:
    """Return executable, dependency-safe leaves in critical-path dispatch order."""
    if not _planning_at_fixed_point(state):
        return []
    nodes = state["nodes"]
    loads = _critical_path_loads(state)
    ready = [node_id for node_id in _frontier_nodes(state) if _node_is_executable(state, node_id)]
    return sorted(ready, key=lambda item: (-loads[item], -nodes[item]["priority"], item))


def planning_diagnostics(state: Mapping[str, Any]) -> dict[str, Any]:
    """Describe planning violations and capacity without treating drafts as an invalid DAG."""
    nodes = state["nodes"]
    split_required = []
    ambiguous = []
    refinement_required = []
    stale = []
    decomposed = []
    for node_id, node in nodes.items():
        if _assessable_leaf(node) and _raw_over_budget(state, node):
            split_required.append(node_id)
        if _assessable_leaf(node) and _raw_requires_refinement(state, node):
            ambiguous.append(node_id)
        assessment_state = _derived_assessment_state(state, node)
        if _assessable_leaf(node):
            if assessment_state == "refinement_required":
                refinement_required.append(node_id)
            elif assessment_state == "stale":
                stale.append(node_id)
        elif assessment_state == "decomposed":
            decomposed.append(node_id)
    usable = state["conventions"]["max_parallel"] - state["conventions"]["reserve"]
    occupied = sum(
        node["launch"]["state"] in ("claimed", "reconcile_required", "bound", "running")
        for node in nodes.values()
    )
    dispatch_order = ready_nodes(state)
    frontier_width = len(dispatch_order)
    return {
        "split_required_nodes": sorted(split_required),
        "ambiguous_nodes": sorted(ambiguous),
        "ambiguity_scores": {
            node_id: {
                "total": node["assessment"]["ambiguity_total"],
                "peak": node["assessment"]["ambiguity_peak"],
                "factors": dict(node["assessment"]["ambiguity_factors"]),
            }
            for node_id, node in sorted(nodes.items())
            if _assessable_leaf(node)
        },
        "refinement_required_nodes": sorted(refinement_required),
        "stale_nodes": sorted(stale),
        "decomposed_nodes": sorted(decomposed),
        "frontier_width": frontier_width,
        "usable_parallelism": usable,
        "available_parallelism": min(frontier_width, max(0, usable - occupied)),
        "critical_path_load": _critical_path_loads(state),
        "dispatch_order": dispatch_order,
    }


def _recovery_required(state: Mapping[str, Any]) -> bool:
    return any(
        node["launch"]["state"] == "reconcile_required"
        or (state["status"] == "aborted" and node["launch"]["state"] == "bound")
        for node in state["nodes"].values()
    )


def validate_state(state: Any) -> dict[str, Any]:
    """Validate one complete deserialized state document without normalizing it."""
    top = _keys(
        state,
        {
            "schema_version", "workflow_id", "repository", "task", "status", "phase",
            "revision", "created_at", "updated_at", "conventions", "nodes", "requirements",
            "decisions", "blockers", "events", "controller", "receipts",
        },
        "state",
    )
    if top["schema_version"] != SCHEMA_VERSION:
        raise StateError("unsupported state schema", code="unsupported_state")
    _identifier(top["workflow_id"], "workflow_id")
    repository = _keys(top["repository"], {"path", "identity"}, "repository")
    _text(repository["path"], "repository.path", maximum=4096)
    if not isinstance(repository["identity"], str) or not SHA256_RE.fullmatch(repository["identity"]):
        raise StateError("repository.identity must be a SHA-256 digest")
    _text(top["task"], "task")
    if top["status"] not in WORKFLOW_STATUSES:
        raise StateError("workflow status is invalid")
    _identifier(top["phase"], "phase")
    if not isinstance(top["revision"], int) or isinstance(top["revision"], bool) or top["revision"] < 0:
        raise StateError("revision must be a non-negative integer")
    created_at = _parse_time(top["created_at"], "created_at")
    updated_at = _parse_time(top["updated_at"], "updated_at")
    if updated_at < created_at:
        raise StateError("updated_at cannot precede created_at")

    conventions = _keys(
        top["conventions"],
        {
            "max_parallel", "reserve", "platform", "write_scope_case_sensitive",
            "node_complexity_split_threshold", "dimension_complexity_split_threshold",
            "node_ambiguity_refine_threshold", "factor_ambiguity_refine_threshold",
            "max_refinement_depth",
        },
        "conventions",
    )
    if not isinstance(conventions["max_parallel"], int) or isinstance(conventions["max_parallel"], bool) or not 1 <= conventions["max_parallel"] <= 8:
        raise StateError("conventions.max_parallel must be between 1 and 8")
    if not isinstance(conventions["reserve"], int) or isinstance(conventions["reserve"], bool) or not 0 <= conventions["reserve"] < conventions["max_parallel"]:
        raise StateError("conventions.reserve must be below max_parallel")
    if conventions["platform"] != os.name:
        raise StateError("workflow state belongs to a different filesystem platform")
    if not isinstance(conventions["write_scope_case_sensitive"], bool):
        raise StateError("conventions.write_scope_case_sensitive must be boolean")
    if os.name == "nt" and conventions["write_scope_case_sensitive"]:
        raise StateError("Windows write-scope comparison must be case-insensitive")
    integer_policies = {
        "node_complexity_split_threshold": (1, len(COMPLEXITY_DIMENSIONS) * 4),
        "dimension_complexity_split_threshold": (1, 4),
        "node_ambiguity_refine_threshold": (1, len(AMBIGUITY_FACTORS) * 4),
        "factor_ambiguity_refine_threshold": (1, 4),
        "max_refinement_depth": (1, 32),
    }
    for name, (minimum, maximum) in integer_policies.items():
        value = conventions[name]
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise StateError(f"conventions.{name} must be an integer from {minimum} through {maximum}")

    if not isinstance(top["nodes"], dict) or len(top["nodes"]) > MAX_NODES:
        raise StateError(f"nodes must be an object with at most {MAX_NODES} entries")
    request_ids: set[str] = set()
    child_ids: set[str] = set()
    for node_key, raw_node in top["nodes"].items():
        node_id = _identifier(node_key, "node key")
        node = _keys(
            raw_node,
            {
                "id", "title", "stage", "priority", "dependencies", "write_scopes", "role", "model",
                "effort", "acceptance", "route", "launch", "attempts", "status", "result", "evidence",
                "estimated_cost", "actual_cost", "superseded_by", "spec", "assessment", "lineage",
            },
            f"nodes.{node_id}",
        )
        if node["id"] != node_id:
            raise StateError(f"nodes.{node_id}.id must match its key")
        _text(node["title"], f"nodes.{node_id}.title", maximum=1024)
        _identifier(node["stage"], f"nodes.{node_id}.stage")
        if not isinstance(node["priority"], int) or isinstance(node["priority"], bool) or not 0 <= node["priority"] <= 100:
            raise StateError(f"nodes.{node_id}.priority must be 0..100")
        if not isinstance(node["dependencies"], list):
            raise StateError(f"nodes.{node_id}.dependencies must be a unique list")
        for dependency in node["dependencies"]:
            _identifier(dependency, f"nodes.{node_id}.dependency")
            if dependency == node_id:
                raise StateError(f"nodes.{node_id} cannot depend on itself")
        if len(set(node["dependencies"])) != len(node["dependencies"]):
            raise StateError(f"nodes.{node_id}.dependencies must be a unique list")
        if not isinstance(node["write_scopes"], list) or len(node["write_scopes"]) > 32:
            raise StateError(f"nodes.{node_id}.write_scopes must contain 0..32 paths")
        for scope in node["write_scopes"]:
            field = f"nodes.{node_id}.write_scope"
            _scope(
                scope,
                field,
                case_sensitive=conventions["write_scope_case_sensitive"],
                platform=conventions["platform"],
            )
            if scope != _canonical_scope(scope, field, platform=conventions["platform"]):
                raise StateError(f"{field} must use canonical forward-slash form")
        if node["role"] not in ROLES:
            raise StateError(f"nodes.{node_id} has an invalid role")
        for field in ("model", "effort"):
            if node[field] is not None:
                _text(node[field], f"nodes.{node_id}.{field}", maximum=256)
        if not isinstance(node["acceptance"], list) or not node["acceptance"]:
            raise StateError(f"nodes.{node_id}.acceptance must be non-empty")
        for item in node["acceptance"]:
            _text(item, f"nodes.{node_id}.acceptance", maximum=2048)
        if len(set(node["acceptance"])) != len(node["acceptance"]):
            raise StateError(f"nodes.{node_id}.acceptance must not contain duplicates")
        spec = _validate_spec(node["spec"], f"nodes.{node_id}.spec")
        assessment = _keys(
            node["assessment"],
            {
                "rubric_version", "dimensions", "total", "ambiguity_factors",
                "ambiguity_total", "ambiguity_peak", "rationale", "input_digest", "state",
            },
            f"nodes.{node_id}.assessment",
        )
        if (
            not isinstance(assessment["rubric_version"], int)
            or isinstance(assessment["rubric_version"], bool)
            or assessment["rubric_version"] != ASSESSMENT_RUBRIC_VERSION
        ):
            raise StateError(f"nodes.{node_id}.assessment.rubric_version must be {ASSESSMENT_RUBRIC_VERSION}")
        dimensions = _validate_dimensions(assessment["dimensions"], f"nodes.{node_id}.assessment.dimensions")
        if not node["write_scopes"] and dimensions["change_surface"] != 0:
            raise StateError(
                f"nodes.{node_id} evidence-only work requires "
                "assessment.dimensions.change_surface=0"
            )
        if node["write_scopes"] and dimensions["change_surface"] == 0:
            raise StateError(
                f"nodes.{node_id} artifact-scoped work requires "
                "assessment.dimensions.change_surface at least 1"
            )
        if (
            not isinstance(assessment["total"], int)
            or isinstance(assessment["total"], bool)
            or assessment["total"] != sum(dimensions.values())
        ):
            raise StateError(f"nodes.{node_id}.assessment.total must equal the five dimension scores")
        ambiguity_factors = _validate_ambiguity_factors(
            assessment["ambiguity_factors"],
            f"nodes.{node_id}.assessment.ambiguity_factors",
        )
        material_ambiguity = any(score >= 2 for score in ambiguity_factors.values())
        if bool(spec["open_questions"]) != material_ambiguity:
            raise StateError(
                f"nodes.{node_id} open questions and material ambiguity scores must agree"
            )
        if (
            not isinstance(assessment["ambiguity_total"], int)
            or isinstance(assessment["ambiguity_total"], bool)
            or assessment["ambiguity_total"] != sum(ambiguity_factors.values())
        ):
            raise StateError(
                f"nodes.{node_id}.assessment.ambiguity_total must equal the five factor scores"
            )
        if (
            not isinstance(assessment["ambiguity_peak"], int)
            or isinstance(assessment["ambiguity_peak"], bool)
            or assessment["ambiguity_peak"] != max(ambiguity_factors.values())
        ):
            raise StateError(
                f"nodes.{node_id}.assessment.ambiguity_peak must equal the highest factor score"
            )
        _text(assessment["rationale"], f"nodes.{node_id}.assessment.rationale", maximum=4096)
        if not isinstance(assessment["input_digest"], str) or not SHA256_RE.fullmatch(assessment["input_digest"]):
            raise StateError(f"nodes.{node_id}.assessment.input_digest must be a SHA-256 digest")
        if assessment["state"] not in ASSESSMENT_STATES:
            raise StateError(f"nodes.{node_id}.assessment.state is invalid")
        lineage = _keys(
            node["lineage"],
            {"parent_id", "depth", "child_ids", "split_reason", "obligations"},
            f"nodes.{node_id}.lineage",
        )
        if lineage["parent_id"] is not None:
            _identifier(lineage["parent_id"], f"nodes.{node_id}.lineage.parent_id")
        if (
            not isinstance(lineage["depth"], int)
            or isinstance(lineage["depth"], bool)
            or not 0 <= lineage["depth"] <= conventions["max_refinement_depth"]
        ):
            raise StateError(f"nodes.{node_id}.lineage.depth exceeds the planning depth limit")
        _text_list(lineage["child_ids"], f"nodes.{node_id}.lineage.child_ids", identifiers=True)
        if lineage["split_reason"] is not None:
            _text(lineage["split_reason"], f"nodes.{node_id}.lineage.split_reason", maximum=4096)
        obligations = _keys(
            lineage["obligations"], set(OBLIGATION_FIELDS), f"nodes.{node_id}.lineage.obligations"
        )
        _text_list(
            obligations["requirements"],
            f"nodes.{node_id}.lineage.obligations.requirements",
            identifiers=True,
        )
        _text_list(
            obligations["objectives"],
            f"nodes.{node_id}.lineage.obligations.objectives",
            item_maximum=MAX_TEXT,
        )
        for field in ("inputs", "outputs", "constraints", "non_goals", "acceptance"):
            _text_list(obligations[field], f"nodes.{node_id}.lineage.obligations.{field}")
        carried_scopes = _text_list(
            obligations["write_scopes"],
            f"nodes.{node_id}.lineage.obligations.write_scopes",
        )
        for scope in carried_scopes:
            field = f"nodes.{node_id}.lineage.obligations.write_scopes"
            _scope(
                scope,
                field,
                case_sensitive=conventions["write_scope_case_sensitive"],
                platform=conventions["platform"],
            )
            if scope != _canonical_scope(scope, field, platform=conventions["platform"]):
                raise StateError(f"{field} must use canonical forward-slash form")
        route = _keys(node["route"], {"rationale", "routed_at", "attempt"}, f"nodes.{node_id}.route")
        _text(route["rationale"], f"nodes.{node_id}.route.rationale", maximum=4096)
        _parse_time(route["routed_at"], f"nodes.{node_id}.route.routed_at")
        if not isinstance(route["attempt"], int) or isinstance(route["attempt"], bool) or route["attempt"] < 0:
            raise StateError(f"nodes.{node_id}.route.attempt must be non-negative")
        launch = _keys(
            node["launch"],
            {"state", "request_id", "child_id", "claimed_at", "reconciliation"},
            f"nodes.{node_id}.launch",
        )
        if launch["state"] not in LAUNCH_STATES:
            raise StateError(f"nodes.{node_id}.launch.state is invalid")
        for key in ("request_id", "child_id"):
            if launch[key] is not None:
                _identifier(launch[key], f"nodes.{node_id}.launch.{key}")
        if launch["claimed_at"] is not None:
            _parse_time(launch["claimed_at"], f"nodes.{node_id}.launch.claimed_at")
        if launch["reconciliation"] is not None:
            _text(launch["reconciliation"], f"nodes.{node_id}.launch.reconciliation", maximum=4096)
        if launch["state"] == "unclaimed" and any(launch[key] is not None for key in ("request_id", "child_id", "claimed_at")):
            raise StateError(f"nodes.{node_id} has data on an unclaimed launch")
        if launch["state"] in ("claimed", "reconcile_required") and not launch["request_id"]:
            raise StateError(f"nodes.{node_id} claimed launch requires request_id")
        if launch["state"] in ("bound", "running", "terminal") and not launch["child_id"]:
            raise StateError(f"nodes.{node_id} bound launch requires child_id")
        if not isinstance(node["attempts"], list) or len(node["attempts"]) > MAX_ATTEMPTS:
            raise StateError(f"nodes.{node_id}.attempts must be a bounded list")
        for index, raw_attempt in enumerate(node["attempts"]):
            attempt = _keys(
                raw_attempt,
                {
                    "number", "request_id", "child_id", "started_at", "finished_at", "outcome",
                    "scope_baseline", "scope_evidence",
                },
                f"nodes.{node_id}.attempts[{index}]",
            )
            if (
                not isinstance(attempt["number"], int)
                or isinstance(attempt["number"], bool)
                or attempt["number"] != index + 1
            ):
                raise StateError(f"nodes.{node_id} attempts must be consecutively numbered")
            _identifier(attempt["request_id"], f"nodes.{node_id}.attempt.request_id")
            if attempt["request_id"] in request_ids:
                raise StateError("launch request identifiers must be unique across attempt history")
            request_ids.add(attempt["request_id"])
            if attempt["child_id"] is not None:
                _identifier(attempt["child_id"], f"nodes.{node_id}.attempt.child_id")
                if attempt["child_id"] in child_ids:
                    raise StateError("launch child identifiers must be unique across attempt history")
                child_ids.add(attempt["child_id"])
            _parse_time(attempt["started_at"], f"nodes.{node_id}.attempt.started_at")
            if attempt["finished_at"] is not None:
                _parse_time(attempt["finished_at"], f"nodes.{node_id}.attempt.finished_at")
            if attempt["outcome"] is not None:
                _text(attempt["outcome"], f"nodes.{node_id}.attempt.outcome", maximum=4096)
            if (attempt["finished_at"] is None) != (attempt["outcome"] is None):
                raise StateError(f"nodes.{node_id}.attempts[{index}] completion fields must both be null or both be set")
            baseline = attempt["scope_baseline"]
            if not isinstance(baseline, dict) or len(baseline) > 32:
                raise StateError(f"nodes.{node_id}.attempts[{index}].scope_baseline must be bounded")
            for scope, fingerprint in baseline.items():
                if scope != _canonical_scope(
                    scope,
                    f"nodes.{node_id}.attempts[{index}].scope_baseline",
                    platform=conventions["platform"],
                ):
                    raise StateError("attempt scope baseline keys must be canonical")
                if fingerprint is not None and (
                    not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint)
                ):
                    raise StateError("attempt scope baseline values must be SHA-256 digests or null")
            evidence = attempt["scope_evidence"]
            if not isinstance(evidence, dict) or set(evidence) - set(baseline):
                raise StateError(f"nodes.{node_id}.attempts[{index}].scope_evidence has unknown scopes")
            for scope, raw_scope_evidence in evidence.items():
                scope_evidence = _keys(
                    raw_scope_evidence,
                    {"before", "after"},
                    f"nodes.{node_id}.attempts[{index}].scope_evidence.{scope}",
                )
                before = scope_evidence["before"]
                after = scope_evidence["after"]
                if before != baseline[scope]:
                    raise StateError("attempt scope evidence must retain its baseline fingerprint")
                if before is not None and (
                    not isinstance(before, str) or not SHA256_RE.fullmatch(before)
                ):
                    raise StateError("attempt scope evidence before value must be a SHA-256 digest or null")
                if not isinstance(after, str) or not SHA256_RE.fullmatch(after):
                    raise StateError("attempt scope evidence after value must be a SHA-256 digest")
                if before == after:
                    raise StateError("attempt scope evidence must record a changed fingerprint")
            if attempt["finished_at"] is None and evidence:
                raise StateError("unfinished attempt cannot contain completion evidence")
        unfinished = [attempt for attempt in node["attempts"] if attempt["finished_at"] is None]
        if len(unfinished) > 1 or (unfinished and unfinished[0] is not node["attempts"][-1]):
            raise StateError(f"nodes.{node_id} has inconsistent attempt completion")
        if launch["state"] == "unclaimed" and unfinished:
            raise StateError(f"nodes.{node_id} unclaimed launch cannot have an unfinished attempt")
        if launch["state"] in ("claimed", "reconcile_required", "bound", "running") and not unfinished:
            raise StateError(f"nodes.{node_id} active launch requires one unfinished attempt")
        if launch["state"] == "terminal" and (not node["attempts"] or unfinished):
            raise StateError(f"nodes.{node_id} terminal launch requires a completed attempt")
        if launch["state"] != "unclaimed" and node["attempts"]:
            latest_attempt = node["attempts"][-1]
            if launch["request_id"] != latest_attempt["request_id"]:
                raise StateError(f"nodes.{node_id} launch request does not match its attempt")
            if launch["child_id"] != latest_attempt["child_id"]:
                raise StateError(f"nodes.{node_id} launch child does not match its attempt")
        if launch["state"] != "unclaimed" and node["attempts"] and route["attempt"] != node["attempts"][-1]["number"]:
            raise StateError(f"nodes.{node_id} route attempt does not match the launch attempt")
        if launch["state"] in ("claimed", "reconcile_required", "bound", "running") and (
            set(node["attempts"][-1]["scope_baseline"]) != set(node["write_scopes"])
        ):
            raise StateError(f"nodes.{node_id} active attempt must cover current write scopes")
        if node["status"] not in NODE_STATUSES:
            raise StateError(f"nodes.{node_id}.status is invalid")
        for key in ("result", "evidence", "superseded_by"):
            if node[key] is not None:
                _text(node[key], f"nodes.{node_id}.{key}", maximum=MAX_TEXT if key != "superseded_by" else 128)
        for key in ("estimated_cost", "actual_cost"):
            if node[key] is not None and (
                not isinstance(node[key], (int, float))
                or isinstance(node[key], bool)
                or not math.isfinite(node[key])
                or node[key] < 0
            ):
                raise StateError(f"nodes.{node_id}.{key} must be non-negative or null")
        if node["status"] == "done" and (not node["result"] or not node["evidence"]):
            raise StateError(f"nodes.{node_id} done status requires result and evidence")
        if node["status"] == "done" and (
            not node["attempts"]
            or set(node["attempts"][-1]["scope_baseline"]) != set(node["write_scopes"])
            or set(node["attempts"][-1]["scope_evidence"]) != set(node["write_scopes"])
        ):
            raise StateError(f"nodes.{node_id} done status requires attempt evidence for every write scope")
        active_launch = launch["state"] in ("claimed", "reconcile_required", "bound", "running")
        aborted_recovery = (
            top["status"] == "aborted"
            and node["status"] == "cancelled"
            and launch["state"] in ("reconcile_required", "bound")
        )
        if node["status"] in TERMINAL_NODE_STATUSES and active_launch and not aborted_recovery:
            raise StateError(f"nodes.{node_id} terminal status cannot retain an active launch")
        if node["status"] in ("pending", "blocked") and active_launch:
            raise StateError(f"nodes.{node_id} pending/blocked status cannot retain an active launch")
        if launch["state"] == "terminal" and node["status"] not in TERMINAL_NODE_STATUSES:
            raise StateError(f"nodes.{node_id} terminal launch requires terminal node status")
        if node["status"] == "running" and launch["state"] not in (
            "running",
            "reconcile_required",
        ):
            raise StateError(
                f"nodes.{node_id} running status requires a running or takeover-reconciliation launch"
            )

    superseded_ids: list[str] = []
    for node_id, node in top["nodes"].items():
        if (
            top["status"] != "aborted"
            and node["status"] in ("skipped", "cancelled")
            and not node["lineage"]["child_ids"]
            and node["superseded_by"] is None
        ):
            raise StateError(
                f"nodes.{node_id} skipped/cancelled work must be decomposed or superseded"
            )
        if node["superseded_by"] is None:
            continue
        _identifier(node["superseded_by"], f"nodes.{node_id}.superseded_by")
        if node["superseded_by"] not in top["nodes"] or node["superseded_by"] == node_id:
            raise StateError(f"nodes.{node_id}.superseded_by must name another existing node")
        superseded_ids.append(node_id)

    for start in superseded_ids:
        seen: set[str] = set()
        current = start
        while top["nodes"][current]["superseded_by"] is not None:
            if current in seen:
                raise StateError("superseded_by cycle is not allowed")
            seen.add(current)
            current = top["nodes"][current]["superseded_by"]

    for node_id in superseded_ids:
        node = top["nodes"][node_id]
        if (
            node["status"] != "skipped"
            or node["result"] != "superseded"
            or not node["evidence"]
            or node["launch"]["state"] != "unclaimed"
            or node["lineage"]["child_ids"]
        ):
            raise StateError(f"nodes.{node_id} superseded_by source must be a skipped superseded leaf")
        if any(node_id in other["dependencies"] for other in top["nodes"].values()):
            raise StateError(f"nodes.{node_id} is superseded but still has dependents")
        replacement = top["nodes"][node["superseded_by"]]
        source_obligations = _effective_obligations(node)
        replacement_obligations = _effective_obligations(replacement)
        if any(
            not set(source_obligations[field]).issubset(replacement_obligations[field])
            for field in OBLIGATION_FIELDS
        ):
            raise StateError(f"nodes.{node_id} supersede replacement loses effective obligations")
        missing_prerequisites = [
            dependency
            for dependency in node["dependencies"]
            if not _depends_on(top["nodes"], node["superseded_by"], dependency)
        ]
        if missing_prerequisites:
            raise StateError(
                f"nodes.{node_id} supersede replacement loses prerequisite(s): "
                + ", ".join(missing_prerequisites)
            )

    if top["status"] != "aborted":
        for start in superseded_ids:
            terminal_id = start
            while top["nodes"][terminal_id]["superseded_by"] is not None:
                terminal_id = top["nodes"][terminal_id]["superseded_by"]
            terminal = top["nodes"][terminal_id]
            if not (_is_resolution_endpoint(terminal) or terminal["lineage"]["child_ids"]):
                raise StateError("superseded_by chain must terminate in resolvable work")

    diagnostic = graph_diagnostics(
        top["nodes"],
        case_sensitive=conventions["write_scope_case_sensitive"],
        platform=conventions["platform"],
    )
    if any(diagnostic.values()):
        raise StateError("invalid workflow graph: " + json.dumps(diagnostic, sort_keys=True))
    occupied = sum(
        node["launch"]["state"] in ("claimed", "reconcile_required", "bound", "running")
        for node in top["nodes"].values()
    )
    if occupied > conventions["max_parallel"] - conventions["reserve"]:
        raise StateError("active launch claims exceed usable controller capacity")
    raw_over_budget_leaves: list[str] = []
    for node_id, node in top["nodes"].items():
        if node["status"] == "ready" and any(top["nodes"][dependency]["status"] not in SUCCESS_NODE_STATUSES for dependency in node["dependencies"]):
            raise StateError(f"nodes.{node_id} cannot be ready before its dependencies")

    if not isinstance(top["requirements"], dict) or len(top["requirements"]) > 256:
        raise StateError("requirements must be a bounded object")
    for requirement_id, raw_requirement in top["requirements"].items():
        _identifier(requirement_id, "requirement id")
        requirement = _keys(raw_requirement, {"text", "source", "status", "evidence"}, f"requirements.{requirement_id}")
        _text(requirement["text"], f"requirements.{requirement_id}.text")
        _text(requirement["source"], f"requirements.{requirement_id}.source", maximum=256)
        if requirement["status"] not in ("active", "satisfied", "superseded"):
            raise StateError(f"requirements.{requirement_id}.status is invalid")
        if requirement["evidence"] is not None:
            _text(requirement["evidence"], f"requirements.{requirement_id}.evidence")
        if requirement["status"] != "active" and not requirement["evidence"]:
            raise StateError(f"requirements.{requirement_id} resolution requires evidence")

    for node_id, node in top["nodes"].items():
        lineage = node["lineage"]
        parent_id = lineage["parent_id"]
        if parent_id is None:
            if lineage["depth"] != 0:
                raise StateError(f"nodes.{node_id} root lineage depth must be zero")
        else:
            parent = top["nodes"].get(parent_id)
            if parent is None:
                raise StateError(f"nodes.{node_id}.lineage.parent_id is unknown")
            if lineage["depth"] != parent["lineage"]["depth"] + 1:
                raise StateError(f"nodes.{node_id}.lineage.depth must follow its parent")
            if node_id not in parent["lineage"]["child_ids"]:
                raise StateError(f"nodes.{node_id} is absent from its parent's lineage")
        if lineage["child_ids"]:
            if not lineage["split_reason"]:
                raise StateError(f"nodes.{node_id} decomposed lineage requires a split reason")
            if node["assessment"]["state"] != "decomposed" or node["status"] != "skipped":
                raise StateError(f"nodes.{node_id} decomposed lineage requires skipped/decomposed state")
            if node["dependencies"]:
                raise StateError(f"nodes.{node_id} decomposed node retains live dependencies")
            if any(node_id in other["dependencies"] for other in top["nodes"].values()):
                raise StateError(f"nodes.{node_id} decomposed node still has a dependent")
            for child_id in lineage["child_ids"]:
                child = top["nodes"].get(child_id)
                if child is None or child["lineage"]["parent_id"] != node_id:
                    raise StateError(f"nodes.{node_id} lineage references a non-child node")
            children = [top["nodes"][child_id] for child_id in lineage["child_ids"]]
            effective = _effective_obligations(node)
            if any(
                not any(
                    item in child["lineage"]["obligations"][field] for child in children
                )
                for field in OBLIGATION_FIELDS
                for item in effective[field]
            ):
                raise StateError(f"nodes.{node_id} decomposed children lose carried obligations")
        else:
            if lineage["split_reason"] is not None:
                raise StateError(f"nodes.{node_id} leaf lineage cannot have a split reason")
            if node["assessment"]["state"] == "decomposed":
                raise StateError(f"nodes.{node_id} decomposed assessment requires child lineage")
        unknown_requirements = set(_effective_obligations(node)["requirements"]) - set(top["requirements"])
        if unknown_requirements:
            raise StateError(
                f"nodes.{node_id} effective obligations reference unknown requirements: "
                + ", ".join(sorted(unknown_requirements))
            )
        assessment_state = _derived_assessment_state(top, node)
        if (_assessable_leaf(node) or lineage["child_ids"]) and node["assessment"]["state"] != assessment_state:
            raise StateError(f"nodes.{node_id}.assessment.state is not derived from its current inputs")
        if (
            _is_resolution_endpoint(node)
            and not _assessable_leaf(node)
            and node["assessment"]["input_digest"] != _assessment_input_digest(top, node)
        ):
            raise StateError(f"nodes.{node_id}.assessment is stale for active or completed work")
        if node["status"] == "ready" and assessment_state != "executable":
            raise StateError(f"nodes.{node_id} ready status requires a current executable assessment")
        if (
            top["status"] != "aborted"
            and node["status"] in ("skipped", "cancelled")
            and not lineage["child_ids"]
            and node["superseded_by"] is None
            and any(lineage["obligations"][field] for field in OBLIGATION_FIELDS)
        ):
            raise StateError(f"nodes.{node_id} cannot discard carried obligations")
        if _assessable_leaf(node) and _raw_over_budget(top, node):
            raw_over_budget_leaves.append(node_id)
        if node["launch"]["state"] in ("claimed", "reconcile_required", "bound", "running") and (
            node["assessment"]["state"] != "executable"
        ):
            raise StateError(f"nodes.{node_id} active launch requires an executable assessment")

    if top["status"] != "aborted":
        obligation_sets: dict[str, dict[str, set[str]]] = {}
        carriers_by_field: dict[str, dict[str, set[str]]] = {
            field: {} for field in OBLIGATION_FIELDS
        }
        for node_id, node in top["nodes"].items():
            effective = _effective_obligations(node)
            obligation_sets[node_id] = {
                field: set(effective[field]) for field in OBLIGATION_FIELDS
            }
            for field in OBLIGATION_FIELDS:
                for obligation in effective[field]:
                    carriers_by_field[field].setdefault(obligation, set()).add(node_id)
        for field in OBLIGATION_FIELDS:
            for obligation, carriers in carriers_by_field[field].items():
                resolved = {
                    node_id
                    for node_id in carriers
                    if _is_resolution_endpoint(top["nodes"][node_id])
                    and (
                        field != "write_scopes"
                        or bool(top["nodes"][node_id]["write_scopes"])
                    )
                }
                predecessors: dict[str, set[str]] = {}
                for node_id in carriers:
                    node = top["nodes"][node_id]
                    if node["lineage"]["child_ids"]:
                        targets = (
                            child_id
                            for child_id in node["lineage"]["child_ids"]
                            if obligation in obligation_sets[child_id][field]
                        )
                    elif node["superseded_by"] is not None:
                        targets = iter((node["superseded_by"],))
                    else:
                        targets = iter(())
                    for target in targets:
                        if target in carriers:
                            predecessors.setdefault(target, set()).add(node_id)
                pending = list(resolved)
                while pending:
                    target = pending.pop()
                    for predecessor in predecessors.get(target, ()):
                        if predecessor not in resolved:
                            resolved.add(predecessor)
                            pending.append(predecessor)
                unresolved = sorted(carriers - resolved)
                if unresolved:
                    raise StateError(
                        f"effective {field} obligation has no acyclic resolution path: "
                        f"{obligation!r} at " + ", ".join(unresolved)
                    )

    max_depth = conventions["max_refinement_depth"]
    stranded = [
        node_id
        for node_id in raw_over_budget_leaves
        if top["nodes"][node_id]["lineage"]["depth"] >= max_depth
    ]
    if stranded:
        raise StateError(
            "max_refinement_depth requires bounded final children/leaves: "
            + ", ".join(sorted(stranded))
        )
    if MAX_NODES - len(top["nodes"]) < 2 * len(raw_over_budget_leaves):
        raise StateError(
            "workflow capacity must reserve two node records per split-required leaf or "
            "raw-over-budget draft; "
            "produce more bounded nodes"
        )

    for field, limit in (("decisions", 256), ("blockers", 256), ("events", MAX_EVENTS)):
        if not isinstance(top[field], list) or len(top[field]) > limit:
            raise StateError(f"{field} must be a list with at most {limit} entries")
    for index, raw_decision in enumerate(top["decisions"]):
        decision = _keys(raw_decision, {"id", "text", "rationale", "at"}, f"decisions[{index}]")
        _identifier(decision["id"], "decision id")
        _text(decision["text"], "decision text")
        _text(decision["rationale"], "decision rationale")
        _parse_time(decision["at"], "decision at")
    for index, raw_blocker in enumerate(top["blockers"]):
        blocker = _keys(raw_blocker, {"id", "node_id", "reason", "needed", "status", "resolution", "at"}, f"blockers[{index}]")
        _identifier(blocker["id"], "blocker id")
        if blocker["node_id"] is not None:
            _identifier(blocker["node_id"], "blocker node_id")
            if blocker["node_id"] not in top["nodes"]:
                raise StateError("blocker references an unknown node")
        _text(blocker["reason"], "blocker reason")
        _text(blocker["needed"], "blocker needed")
        if blocker["status"] not in ("active", "resolved"):
            raise StateError("blocker status is invalid")
        if blocker["resolution"] is not None:
            _text(blocker["resolution"], "blocker resolution")
        if blocker["status"] == "resolved" and not blocker["resolution"]:
            raise StateError("resolved blocker requires resolution")
        _parse_time(blocker["at"], "blocker at")
    for index, raw_event in enumerate(top["events"]):
        event = _keys(raw_event, {"id", "kind", "message", "node_id", "at", "revision"}, f"events[{index}]")
        _identifier(event["id"], "event id")
        _identifier(event["kind"], "event kind")
        _text(event["message"], "event message")
        if event["node_id"] is not None:
            _identifier(event["node_id"], "event node_id")
        _parse_time(event["at"], "event at")
        if not isinstance(event["revision"], int) or isinstance(event["revision"], bool) or event["revision"] < 0:
            raise StateError("event revision must be non-negative")
        if event["revision"] > top["revision"] or (index and event["revision"] < top["events"][index - 1]["revision"]):
            raise StateError("event revisions must be persisted and durably ordered")
    for field in ("decisions", "blockers", "events"):
        if len({item["id"] for item in top[field]}) != len(top[field]):
            raise StateError(f"{field} identifiers must be unique")

    controller = _keys(
        top["controller"],
        {
            "epoch",
            "origin_session_id",
            "session_id",
            "checkpoint",
            "resume_required",
            "recovery_status",
        },
        "controller",
    )
    if not isinstance(controller["epoch"], int) or isinstance(controller["epoch"], bool) or controller["epoch"] < 1:
        raise StateError("controller.epoch must be positive")
    _identifier(controller["origin_session_id"], "controller.origin_session_id")
    _identifier(controller["session_id"], "controller.session_id")
    if not isinstance(controller["checkpoint"], int) or isinstance(controller["checkpoint"], bool) or controller["checkpoint"] < 0:
        raise StateError("controller.checkpoint must be non-negative")
    if controller["checkpoint"] > top["revision"]:
        raise StateError("controller checkpoint cannot exceed the state revision")
    if not isinstance(controller["resume_required"], bool):
        raise StateError("controller.resume_required must be boolean")
    if controller["recovery_status"] not in ("clean", "takeover_pending", "reconcile_required"):
        raise StateError("controller.recovery_status is invalid")

    if not isinstance(top["receipts"], dict) or len(top["receipts"]) > MAX_RECEIPTS:
        raise StateError("receipts must be a bounded object")
    for mutation_id, raw_receipt in top["receipts"].items():
        _identifier(mutation_id, "mutation id")
        receipt = _keys(raw_receipt, {"digest", "revision", "at"}, f"receipts.{mutation_id}")
        if not isinstance(receipt["digest"], str) or not SHA256_RE.fullmatch(receipt["digest"]):
            raise StateError("receipt digest must be SHA-256")
        if not isinstance(receipt["revision"], int) or isinstance(receipt["revision"], bool) or receipt["revision"] < 1:
            raise StateError("receipt revision must be positive")
        if receipt["revision"] > top["revision"]:
            raise StateError("receipt revision cannot exceed the state revision")
        _parse_time(receipt["at"], "receipt at")
    reconcile_required = _recovery_required(top)
    if reconcile_required != (controller["recovery_status"] == "reconcile_required"):
        raise StateError("controller recovery status disagrees with launch reconciliation state")
    if controller["recovery_status"] == "takeover_pending" and not controller["resume_required"]:
        raise StateError("takeover_pending requires explicit resume")
    if top["status"] == "completed":
        if (
            top["phase"] != "completed"
            or not top["nodes"]
            or any(not _node_resolves_completion(node) for node in top["nodes"].values())
        ):
            raise StateError("completed workflow requires every node to resolve through done work")
        if any(item["status"] == "active" for item in top["requirements"].values()) or any(item["status"] == "active" for item in top["blockers"]):
            raise StateError("completed workflow cannot retain active requirements or blockers")
    if top["status"] == "aborted" and top["phase"] != "aborted":
        raise StateError("aborted workflow phase is inconsistent")
    if top["phase"] == "completed" and top["status"] != "completed":
        raise StateError("completed workflow phase requires completed status")
    if top["phase"] == "aborted" and top["status"] != "aborted":
        raise StateError("aborted workflow phase requires aborted status")
    return state


def new_state(
    repository: Mapping[str, str],
    task: str,
    session_id: str,
    conventions: Mapping[str, Any] | None = None,
    *,
    write_scope_case_sensitive: bool | None = None,
) -> dict[str, Any]:
    created = now_iso()
    suffix = hashlib.sha256((repository["identity"] + "\0" + task + "\0" + created + secrets.token_hex(8)).encode()).hexdigest()[:20]
    profile = {
        "max_parallel": 4,
        "reserve": 1,
        "platform": os.name,
        "write_scope_case_sensitive": (
            os.name != "nt"
            if write_scope_case_sensitive is None
            else write_scope_case_sensitive
        ),
        "node_complexity_split_threshold": 6,
        "dimension_complexity_split_threshold": 3,
        "node_ambiguity_refine_threshold": 4,
        "factor_ambiguity_refine_threshold": 2,
        "max_refinement_depth": 8,
    }
    if conventions:
        derived = {"platform", "write_scope_case_sensitive"} & set(conventions)
        if derived:
            raise StateError(
                "state-owner-derived conventions cannot be overridden: "
                + ", ".join(sorted(derived))
            )
        profile.update(conventions)
    state = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": f"wf-{suffix}",
        "repository": dict(repository),
        "task": _text(task, "task"),
        "status": "planning",
        "phase": "planning",
        "revision": 0,
        "created_at": created,
        "updated_at": created,
        "conventions": profile,
        "nodes": {},
        "requirements": {},
        "decisions": [],
        "blockers": [],
        "events": [],
        "controller": {
            "epoch": 1,
            "origin_session_id": _identifier(session_id, "session_id"),
            "session_id": _identifier(session_id, "session_id"),
            "checkpoint": 0,
            "resume_required": False,
            "recovery_status": "clean",
        },
        "receipts": {},
    }
    return validate_state(state)


def _terminal_reconciliation(state: Mapping[str, Any], operation: Mapping[str, Any]) -> bool:
    if state["status"] != "aborted":
        return False
    if set(operation) == {"command", "message"} and operation["command"] == "resume":
        return (
            state["controller"]["resume_required"]
            and _recovery_required(state)
            and isinstance(operation["message"], str)
            and bool(operation["message"].strip())
        )
    expected = {
        "command", "node_id", "status", "launch_state", "request_id", "child_id",
        "reconciliation", "result", "evidence", "actual_cost", "attempt_outcome",
    }
    if set(operation) != expected or operation["command"] != "node-update":
        return False
    node = state["nodes"].get(operation["node_id"])
    target = operation["launch_state"]
    if (
        not node
        or not isinstance(operation["reconciliation"], str)
        or not operation["reconciliation"].strip()
        or any(operation[key] is not None for key in ("status", "request_id", "result", "evidence", "actual_cost"))
    ):
        return False
    child_id = operation["child_id"]
    if node["launch"]["state"] == "reconcile_required" and target in ("unclaimed", "bound"):
        if operation["attempt_outcome"] is not None:
            return False
        if target == "unclaimed":
            return node["launch"]["child_id"] is None and child_id is None
        known_child = node["launch"]["child_id"]
        return isinstance(child_id, str) and bool(child_id) and (known_child is None or child_id == known_child)
    return (
        node["launch"]["state"] == "bound"
        and target == "terminal"
        and child_id is None
        and isinstance(operation["attempt_outcome"], str)
        and bool(operation["attempt_outcome"].strip())
    )


class StateStore:
    """The sole persistence and concurrency authority for workflow state."""

    def __init__(self, root: pathlib.Path | None = None):
        self.root = (root or (pathlib.Path.home() / ".agent-coordinator")).expanduser().absolute()

    @property
    def workflows(self) -> pathlib.Path:
        return self.root / "workflows"

    @property
    def sessions(self) -> pathlib.Path:
        return self.root / "sessions"

    @property
    def locks(self) -> pathlib.Path:
        return self.root / "locks"

    def _ensure_private_directory(self, path: pathlib.Path) -> None:
        path = path.absolute()
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise StateError("Coordinator path escapes the control root", code="unsafe_path", exit_code=20) from exc
        parent = self.root.parent
        _assert_no_link_components(parent)
        if parent.is_symlink() or not parent.is_dir() or _is_reparse(parent.lstat()):
            raise StateError("control-root parent is unsafe", code="unsafe_path", exit_code=20)
        chain = [self.root]
        current = self.root
        for part in relative.parts:
            current = current / part
            chain.append(current)
        for item in chain:
            if not item.exists():
                if item.is_symlink():
                    raise StateError(f"refusing symlink directory: {item}", code="unsafe_path", exit_code=20)
                item.mkdir(mode=0o700)
            info = item.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise StateError(f"unsafe Coordinator directory: {item}", code="unsafe_path", exit_code=20)
            if os.name != "nt" and info.st_uid != os.getuid():
                raise StateError(f"Coordinator directory has another owner: {item}", code="unsafe_owner", exit_code=20)
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise StateError(f"Coordinator directory is not private: {item}", code="unsafe_permissions", exit_code=20)

    def _state_path(self, workflow_id: str) -> pathlib.Path:
        return self.workflows / (_identifier(workflow_id, "workflow_id") + ".json")

    def _read_json(self, path: pathlib.Path, *, maximum: int = MAX_STATE_BYTES) -> Any:
        try:
            _assert_no_link_components(path.parent)
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or _is_reparse(before)
                or before.st_nlink != 1
                or (os.name != "nt" and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077))
            ):
                raise StateError("private state/session file is unsafe", code="unsafe_path", exit_code=20)
            if before.st_size > maximum:
                raise StateError(f"state exceeds {maximum} bytes", code="state_too_large", exit_code=20)
            raw = path.read_bytes()
            after = path.lstat()
        except FileNotFoundError as exc:
            raise StateError(f"state not found: {path.stem}", code="not_found", exit_code=1) from exc
        except OSError as exc:
            raise StateError("unable to read workflow state", code="io_error", exit_code=20) from exc
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != after.st_size:
            raise StateError("workflow state changed during read", code="changed_during_read", exit_code=20)
        return _decode_json(raw, "workflow state")

    def load(self, workflow_id: str) -> dict[str, Any]:
        expected = _identifier(workflow_id, "workflow_id")
        state = validate_state(self._read_json(self._state_path(expected)))
        if state["workflow_id"] != expected:
            raise StateError("state filename and workflow identifier differ", code="corrupt_state", exit_code=20)
        return state

    def iter_records(self) -> Iterator[tuple[pathlib.Path, dict[str, Any] | None, StateError | None]]:
        try:
            workflows_info = self.workflows.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(workflows_info.st_mode) or stat.S_ISLNK(workflows_info.st_mode) or _is_reparse(workflows_info):
            yield self.workflows, None, StateError("unsafe workflows directory", code="unsafe_path", exit_code=20)
            return
        for path in sorted(self.workflows.glob("*.json")):
            try:
                state = validate_state(self._read_json(path))
                if state["workflow_id"] != path.stem:
                    raise StateError("state filename and workflow identifier differ", code="corrupt_state", exit_code=20)
                yield path, state, None
            except StateError as exc:
                yield path, None, exc

    def list_valid(self) -> list[dict[str, Any]]:
        states = [state for _, state, error in self.iter_records() if state is not None and error is None]
        return sorted(states, key=lambda value: value["updated_at"], reverse=True)

    def _atomic_json(self, path: pathlib.Path, value: Any) -> None:
        data = _json_bytes(value)
        if len(data) > MAX_STATE_BYTES:
            raise StateError(f"state exceeds {MAX_STATE_BYTES} bytes", code="state_too_large", exit_code=20)
        self._ensure_private_directory(path.parent)
        temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
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
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateError(
                "durable state persistence failed; reconcile the mutation id",
                code="persistence_uncertain",
                exit_code=20,
            ) from exc

    def _prepare_session_target(self, target: pathlib.Path, repository_path: str) -> pathlib.Path:
        target = target.expanduser().absolute()
        try:
            target.relative_to(pathlib.Path(repository_path).absolute())
        except ValueError:
            pass
        else:
            raise StateError("session bearer file must be outside the repository", code="unsafe_session_file", exit_code=20)
        if target.exists() or target.is_symlink():
            raise StateError("session file already exists", code="unsafe_session_file", exit_code=20)
        missing: list[pathlib.Path] = []
        current = target.parent
        while not current.exists():
            if current.is_symlink():
                raise StateError("session path contains a symlink", code="unsafe_session_file", exit_code=20)
            missing.append(current)
            current = current.parent
        if current.is_symlink() or not current.is_dir() or _is_reparse(current.lstat()):
            raise StateError("session path parent is unsafe", code="unsafe_session_file", exit_code=20)
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        _assert_no_link_components(target.parent)
        parent = target.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or _is_reparse(parent)
            or (os.name != "nt" and (parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077))
        ):
            raise StateError("session file parent must be current-user private", code="unsafe_session_file", exit_code=20)
        return target

    def _write_session_file(self, target: pathlib.Path, value: Mapping[str, Any]) -> None:
        data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise StateError("unable to create private session file", code="io_error", exit_code=20) from exc

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        self._ensure_private_directory(self.locks)
        path = self.locks / (_identifier(name, "lock name") + ".lock")
        nonce = secrets.token_hex(16)
        payload = json.dumps({"pid": os.getpid(), "nonce": nonce, "created_at": now_iso()}).encode()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            info = os.fstat(descriptor)
            observed = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or _is_reparse(observed)
                or (info.st_dev, info.st_ino) != (observed.st_dev, observed.st_ino)
                or info.st_nlink != 1
                or (
                    os.name != "nt"
                    and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)
                )
            ):
                raise StateError(
                    "workflow lock file is unsafe",
                    code="concurrent_controller",
                    exit_code=20,
                )
            _acquire_advisory_lock(descriptor)
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            yield
        except StateError:
            raise
        except OSError as exc:
            raise StateError(
                "unable to acquire workflow lock",
                code="concurrent_controller",
                exit_code=20,
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    _release_advisory_lock(descriptor)
                except OSError:
                    pass
                os.close(descriptor)

    def open_session(self, repository: Mapping[str, str], session_file: pathlib.Path) -> dict[str, Any]:
        session_id = "session-" + secrets.token_hex(12)
        bearer = secrets.token_urlsafe(32)
        record = {
            "schema_version": 1,
            "session_id": session_id,
            "repository_identity": repository["identity"],
            "bearer_sha256": hashlib.sha256(bearer.encode()).hexdigest(),
            "opened_at": now_iso(),
        }
        target = self._prepare_session_target(session_file, repository["path"])
        record_path = self.sessions / f"{session_id}.json"
        self._atomic_json(record_path, record)
        try:
            self._write_session_file(target, {"session_id": session_id, "bearer": bearer, "repository_identity": repository["identity"]})
        except StateError:
            record_path.unlink(missing_ok=True)
            raise
        return {"session_id": session_id, "repository": dict(repository)}

    def _session(self, session_file: pathlib.Path, repository_identity: str) -> dict[str, Any]:
        session = self._read_json(session_file.expanduser().absolute(), maximum=4096)
        session = _keys(session, {"session_id", "bearer", "repository_identity"}, "session file")
        session_id = _identifier(session["session_id"], "session_id")
        _text(session["bearer"], "session bearer", maximum=256)
        if session["repository_identity"] != repository_identity:
            raise StateError("session belongs to another repository", code="session_mismatch", exit_code=20)
        record = self._read_json(self.sessions / f"{session_id}.json", maximum=4096)
        record = _keys(record, {"schema_version", "session_id", "repository_identity", "bearer_sha256", "opened_at"}, "session record")
        if (
            record["schema_version"] != 1
            or record["session_id"] != session_id
            or record["repository_identity"] != repository_identity
            or not secrets.compare_digest(record["bearer_sha256"], hashlib.sha256(session["bearer"].encode()).hexdigest())
        ):
            raise StateError("session credential is invalid", code="invalid_session", exit_code=20)
        return dict(session)

    def close_session(self, session_file: pathlib.Path) -> dict[str, Any]:
        target = session_file.expanduser().absolute()
        raw = self._read_json(target, maximum=4096)
        if not isinstance(raw, dict) or not isinstance(raw.get("repository_identity"), str):
            raise StateError("session file is invalid", code="invalid_session", exit_code=20)
        session = self._session(target, raw["repository_identity"])
        session_id = session["session_id"]
        record = self.sessions / f"{session_id}.json"
        with self._lock("sessions"):
            record.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
        return {"session_id": session_id, "closed": True}

    def create(
        self,
        repository: Mapping[str, str],
        task: str,
        session_file: pathlib.Path,
        conventions: Mapping[str, Any] | None = None,
        mutation_id: str = "init",
    ) -> dict[str, Any]:
        mutation = _identifier(mutation_id, "mutation_id")
        session = self._session(session_file, repository["identity"])
        digest = self._payload_digest(
            {
                "command": "init",
                "repository": repository["identity"],
                "session_id": session["session_id"],
                "task": task,
                "conventions": conventions,
            }
        )
        with self._lock("workflow-create"):
            for existing in self.list_valid():
                if existing["controller"]["origin_session_id"] != session["session_id"]:
                    continue
                receipt = existing["receipts"].get(mutation)
                if receipt:
                    if receipt["digest"] != digest:
                        raise StateError("mutation id was reused for different initialization", code="mutation_conflict", exit_code=20)
                    return existing
            state = new_state(
                repository,
                task,
                session["session_id"],
                conventions,
                write_scope_case_sensitive=_repository_case_sensitive(repository["path"]),
            )
            add_event(state, "workflow_created", "workflow initialized")
            state["revision"] = 1
            state["controller"]["checkpoint"] = 1
            state["receipts"][mutation] = {"digest": digest, "revision": 1, "at": state["updated_at"]}
            validate_state(state)
            self._atomic_json(self._state_path(state["workflow_id"]), state)
        return state

    def _payload_digest(self, operation: Mapping[str, Any]) -> str:
        try:
            encoded = json.dumps(operation, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise StateError("mutation payload is not canonical JSON data") from exc
        return hashlib.sha256(encoded).hexdigest()

    def mutate(
        self,
        workflow_id: str,
        *,
        session_file: pathlib.Path,
        mutation_id: str,
        expected_revision: int,
        operation: Mapping[str, Any],
        change: Callable[[dict[str, Any]], Any],
        allow_resume_required: bool = False,
    ) -> tuple[dict[str, Any], Any, bool]:
        mutation = _identifier(mutation_id, "mutation_id")
        digest = self._payload_digest(operation)
        with self._lock(workflow_id):
            state = self.load(workflow_id)
            session = self._session(session_file, state["repository"]["identity"])
            if state["controller"]["session_id"] != session["session_id"]:
                raise StateError("controller epoch is owned by another session", code="controller_fenced", exit_code=20)
            receipt = state["receipts"].get(mutation)
            if receipt:
                if receipt["digest"] != digest:
                    raise StateError("mutation id was reused for different content", code="mutation_conflict", exit_code=20)
                return state, {"applied_revision": receipt["revision"]}, True
            if expected_revision != state["revision"]:
                raise StateError(
                    f"expected revision {expected_revision}, observed {state['revision']}",
                    code="revision_conflict",
                    exit_code=20,
                )
            if isinstance(expected_revision, bool):
                raise StateError("expected revision must be an integer", code="revision_conflict", exit_code=20)
            if state["controller"]["resume_required"] and not allow_resume_required:
                raise StateError("controller takeover requires explicit resume", code="resume_required", exit_code=20)
            if state["status"] in ("completed", "aborted") and not _terminal_reconciliation(state, operation):
                raise StateError("terminal workflow cannot be mutated")
            candidate = copy.deepcopy(state)
            result = change(candidate)
            candidate["revision"] += 1
            candidate["updated_at"] = now_iso()
            candidate["controller"]["checkpoint"] = candidate["revision"]
            candidate["receipts"][mutation] = {"digest": digest, "revision": candidate["revision"], "at": candidate["updated_at"]}
            if len(candidate["receipts"]) > MAX_RECEIPTS:
                raise StateError("workflow mutation receipt capacity is exhausted", code="capacity_exceeded", exit_code=20)
            if len(candidate["events"]) > MAX_EVENTS:
                candidate["events"] = candidate["events"][-MAX_EVENTS:]
            validate_state(candidate)
            self._atomic_json(self._state_path(workflow_id), candidate)
            return candidate, result, False

    def takeover(
        self,
        workflow_id: str,
        *,
        session_file: pathlib.Path,
        mutation_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._lock(workflow_id):
            state = self.load(workflow_id)
            session = self._session(session_file, state["repository"]["identity"])
            digest = self._payload_digest({"command": "controller-takeover", "session_id": session["session_id"]})
            mutation = _identifier(mutation_id, "mutation_id")
            receipt = state["receipts"].get(mutation)
            if receipt:
                if receipt["digest"] != digest:
                    raise StateError("mutation id was reused for another takeover", code="mutation_conflict", exit_code=20)
                return state
            if state["revision"] != expected_revision:
                raise StateError("controller takeover revision conflict", code="revision_conflict", exit_code=20)
            if isinstance(expected_revision, bool):
                raise StateError("expected revision must be an integer", code="revision_conflict", exit_code=20)
            if state["status"] == "completed" or (state["status"] == "aborted" and not _recovery_required(state)):
                raise StateError("terminal workflow cannot change controllers")
            candidate = copy.deepcopy(state)
            for node in candidate["nodes"].values():
                if node["launch"]["state"] in ("claimed", "bound", "running"):
                    node["launch"]["state"] = "reconcile_required"
                    node["launch"]["reconciliation"] = (
                        "controller takeover requires provider outcome reconciliation"
                    )
            candidate["controller"].update(
                {
                    "epoch": candidate["controller"]["epoch"] + 1,
                    "session_id": session["session_id"],
                    "resume_required": True,
                    "recovery_status": (
                        "reconcile_required"
                        if _recovery_required(candidate)
                        else "takeover_pending"
                    ),
                }
            )
            candidate["revision"] += 1
            candidate["updated_at"] = now_iso()
            candidate["controller"]["checkpoint"] = candidate["revision"]
            candidate["receipts"][mutation] = {"digest": digest, "revision": candidate["revision"], "at": candidate["updated_at"]}
            if len(candidate["receipts"]) > MAX_RECEIPTS:
                raise StateError("workflow mutation receipt capacity is exhausted", code="capacity_exceeded", exit_code=20)
            validate_state(candidate)
            self._atomic_json(self._state_path(workflow_id), candidate)
            return candidate

    def reconcile_mutation(self, workflow_id: str, mutation_id: str, digest: str | None = None) -> dict[str, Any]:
        state = self.load(workflow_id)
        mutation = _identifier(mutation_id, "mutation_id")
        receipt = state["receipts"].get(mutation)
        if receipt is None:
            return {"outcome": "not_applied", "revision": state["revision"]}
        if digest is not None and not secrets.compare_digest(receipt["digest"], digest.lower()):
            raise StateError("observed receipt digest differs", code="mutation_conflict", exit_code=20)
        return {"outcome": "applied", "revision": receipt["revision"], "digest": receipt["digest"]}


def add_event(state: dict[str, Any], kind: str, message: str, node_id: str | None = None) -> dict[str, Any]:
    event = {
        "id": "event-" + secrets.token_hex(8),
        "kind": _identifier(kind, "event kind"),
        "message": _text(message, "event message"),
        "node_id": _identifier(node_id, "event node_id") if node_id else None,
        "at": now_iso(),
        "revision": state["revision"] + 1,
    }
    state["events"].append(event)
    return event


def _read_command_text(
    value: str | None,
    path: str | None,
    name: str,
    *,
    maximum: int = MAX_COMMAND_BYTES,
) -> str:
    if (value is None) == (path is None):
        raise StateError(f"exactly one of --{name} or --{name}-file is required")
    try:
        if value is not None:
            result = value
        elif path == "-":
            result = sys.stdin.read(maximum + 1)
        else:
            with pathlib.Path(path or "").open("rb") as handle:
                raw = handle.read(maximum + 1)
            if len(raw) > maximum:
                raise StateError(
                    f"{name} exceeds {maximum} UTF-8 bytes",
                    code="input_too_large",
                    exit_code=2,
                )
            result = raw.decode("utf-8")
        encoded_size = len(result.encode("utf-8"))
    except StateError:
        raise
    except (OSError, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise StateError(
            f"unable to read {name} as UTF-8",
            code="io_error",
            exit_code=20,
        ) from exc
    if encoded_size > maximum:
        raise StateError(
            f"{name} exceeds {maximum} UTF-8 bytes",
            code="input_too_large",
            exit_code=2,
        )
    if not result.strip():
        raise StateError(f"{name} must not be blank")
    return result


def _read_command_object(value: str | None, path: str | None, name: str) -> dict[str, Any]:
    text = _read_command_text(value, path, name)
    try:
        parsed = _decode_json(text, name, exit_code=2)
    except StateError as exc:
        raise StateError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StateError(f"{name} must be a JSON object")
    return parsed


def _mutate_command(
    store: StateStore,
    args: Any,
    command: str,
    operation: Mapping[str, Any],
    change: Callable[[dict[str, Any]], Any],
    *,
    allow_resume_required: bool = False,
) -> tuple[dict[str, Any], Any, bool]:
    return store.mutate(
        args.workflow_id,
        session_file=pathlib.Path(args.session_file),
        mutation_id=args.mutation_id,
        expected_revision=args.expected_revision,
        operation={"command": command, **operation},
        change=change,
        allow_resume_required=allow_resume_required,
    )


def _public_state(state: Mapping[str, Any], *, full: bool = False) -> dict[str, Any]:
    result = {
        "workflow_id": state["workflow_id"],
        "repository": state["repository"],
        "task": state["task"],
        "status": state["status"],
        "phase": state["phase"],
        "revision": state["revision"],
        "updated_at": state["updated_at"],
        "controller_epoch": state["controller"]["epoch"],
        "resume_required": state["controller"]["resume_required"],
        "ready_nodes": ready_nodes(state),
    }
    if full:
        result["state"] = state
    return result


def _node_record(
    *,
    node_id: str,
    title: str,
    stage: str,
    priority: int,
    dependencies: list[str],
    write_scopes: list[str],
    role: str,
    model: str | None,
    effort: str | None,
    acceptance: list[str],
    route_rationale: str,
    estimated_cost: float | None,
    spec: Mapping[str, Any],
    assessment: Mapping[str, Any],
    parent_id: str | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    stage = _identifier(stage, "node stage")
    if stage not in STAGES:
        raise StateError("node stage is not supported")
    checked_spec = _validate_spec(spec)
    return {
        "id": node_id,
        "title": title,
        "stage": stage,
        "priority": priority,
        "dependencies": list(dependencies),
        "write_scopes": [_canonical_scope(scope, "write_scope") for scope in write_scopes],
        "role": role,
        "model": model,
        "effort": effort,
        "acceptance": list(acceptance),
        "spec": copy.deepcopy(dict(checked_spec)),
        "assessment": _assessment_shell(assessment),
        "lineage": {
            "parent_id": parent_id,
            "depth": depth,
            "child_ids": [],
            "split_reason": None,
            "obligations": {field: [] for field in OBLIGATION_FIELDS},
        },
        "route": {"rationale": route_rationale, "routed_at": now_iso(), "attempt": 0},
        "launch": {
            "state": "unclaimed",
            "request_id": None,
            "child_id": None,
            "claimed_at": None,
            "reconciliation": None,
        },
        "attempts": [],
        "status": "pending",
        "result": None,
        "evidence": None,
        "estimated_cost": estimated_cost,
        "actual_cost": None,
        "superseded_by": None,
    }


def _new_node(args: Any) -> dict[str, Any]:
    return _node_record(
        node_id=args.node_id,
        title=args.title,
        stage=args.stage,
        priority=args.priority,
        dependencies=args.dependency,
        write_scopes=args.write_scope,
        role=args.role,
        model=args.model,
        effort=args.effort,
        acceptance=args.acceptance,
        route_rationale=args.rationale,
        estimated_cost=args.estimated_cost,
        spec={
            "objective": args.objective,
            "inputs": args.input,
            "outputs": args.output,
            "constraints": args.constraint,
            "non_goals": args.non_goal,
            "requirement_ids": args.requirement_id,
            "open_questions": args.open_question,
        },
        assessment={
            "dimensions": {name: getattr(args, name) for name in COMPLEXITY_DIMENSIONS},
            "ambiguity_factors": {
                name: getattr(args, f"ambiguity_{name}")
                for name in AMBIGUITY_FACTORS
            },
            "rationale": args.complexity_rationale,
        },
    )


def _refresh_node_assessment(state: dict[str, Any], node_id: str) -> None:
    node = state["nodes"][node_id]
    inputs = {
        "dimensions": node["assessment"]["dimensions"],
        "ambiguity_factors": node["assessment"]["ambiguity_factors"],
        "rationale": node["assessment"]["rationale"],
    }
    node["assessment"] = _build_assessment(state, node, inputs)


def _require_rewritable_leaf(node: Mapping[str, Any], operation: str) -> None:
    failed_retry = node["status"] == "failed" and node["launch"]["state"] in ("unclaimed", "terminal")
    future = node["status"] in ("pending", "ready", "blocked") and node["launch"]["state"] == "unclaimed"
    if node["lineage"]["child_ids"] or not (future or failed_retry):
        raise StateError(f"{operation} requires a non-active future or failed leaf")


def _reset_failed_leaf(node: dict[str, Any]) -> None:
    if node["status"] != "failed":
        return
    node["status"] = "pending"
    node["result"] = None
    node["evidence"] = None
    node["launch"] = {
        "state": "unclaimed",
        "request_id": None,
        "child_id": None,
        "claimed_at": None,
        "reconciliation": None,
    }


SPLIT_CHILD_KEYS = {
    "id", "title", "stage", "priority", "dependencies", "write_scopes", "role", "model", "effort",
    "acceptance", "route_rationale", "estimated_cost", "spec", "assessment",
}


def _split_child_record(raw: Any, *, parent_id: str, depth: int, index: int) -> dict[str, Any]:
    field = f"plan.children[{index}]"
    child = _keys(raw, SPLIT_CHILD_KEYS, field)
    dependencies = _text_list(child["dependencies"], f"{field}.dependencies", identifiers=True)
    if parent_id in dependencies:
        raise StateError(f"{field} cannot depend on the node being decomposed")
    write_scopes = _text_list(child["write_scopes"], f"{field}.write_scopes", maximum=32)
    acceptance = _text_list(child["acceptance"], f"{field}.acceptance", required=True)
    return _node_record(
        node_id=_identifier(child["id"], f"{field}.id"),
        title=_text(child["title"], f"{field}.title", maximum=1024),
        stage=_identifier(child["stage"], f"{field}.stage"),
        priority=child["priority"],
        dependencies=dependencies,
        write_scopes=write_scopes,
        role=child["role"],
        model=child["model"],
        effort=child["effort"],
        acceptance=acceptance,
        route_rationale=_text(child["route_rationale"], f"{field}.route_rationale", maximum=4096),
        estimated_cost=child["estimated_cost"],
        spec=child["spec"],
        assessment=child["assessment"],
        parent_id=parent_id,
        depth=depth,
    )


def _coverage_mapping(
    value: Any,
    expected: list[str],
    child_ids: set[str],
    field: str,
) -> Mapping[str, list[str]]:
    if not isinstance(value, dict):
        raise StateError(f"{field} must be an object")
    if set(value) != set(expected):
        missing = set(expected) - set(value)
        extra = set(value) - set(expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unknown " + ", ".join(sorted(extra)))
        raise StateError(f"{field} does not exactly cover the parent items: " + "; ".join(detail))
    checked: dict[str, list[str]] = {}
    for item, replacements in value.items():
        selected = _text_list(replacements, f"{field}.{item}", required=True, identifiers=True)
        unknown = set(selected) - child_ids
        if unknown:
            raise StateError(f"{field}.{item} references non-child nodes: " + ", ".join(sorted(unknown)))
        checked[item] = selected
    return {item: checked[item] for item in expected}


def _refresh_recovery_status(state: dict[str, Any]) -> None:
    if _recovery_required(state):
        state["controller"]["recovery_status"] = "reconcile_required"
    elif state["controller"]["resume_required"]:
        state["controller"]["recovery_status"] = "takeover_pending"
    else:
        state["controller"]["recovery_status"] = "clean"


PLAN_REQUIREMENT_KEYS = {"id", "text", "source"}


def _plan_requirement_records(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 256:
        raise StateError("plan.requirements must be a list with at most 256 entries")
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        field = f"plan.requirements[{index}]"
        requirement = _keys(raw, PLAN_REQUIREMENT_KEYS, field)
        requirement_id = _identifier(requirement["id"], f"{field}.id")
        if requirement_id in result:
            raise StateError("plan requirement identifiers must be unique")
        result[requirement_id] = {
            "text": _text(requirement["text"], f"{field}.text"),
            "source": _text(requirement["source"], f"{field}.source", maximum=256),
            "status": "active",
            "evidence": None,
        }
    return result


def _plan_node_record(raw: Any, index: int) -> dict[str, Any]:
    field = f"plan.nodes[{index}]"
    node = _keys(raw, SPLIT_CHILD_KEYS, field)
    node_id = _identifier(node["id"], f"{field}.id")
    stage = _identifier(node["stage"], f"{field}.stage")
    priority = node["priority"]
    if (
        not isinstance(priority, int)
        or isinstance(priority, bool)
        or not 0 <= priority <= 100
    ):
        raise StateError(f"{field}.priority must be 0..100")
    role = node["role"]
    if role not in ROLES:
        raise StateError(f"{field}.role is invalid")
    model = node["model"]
    effort = node["effort"]
    if model is not None:
        model = _text(model, f"{field}.model", maximum=256)
    if effort is not None:
        effort = _text(effort, f"{field}.effort", maximum=256)
    estimated_cost = node["estimated_cost"]
    if estimated_cost is not None and (
        not isinstance(estimated_cost, (int, float))
        or isinstance(estimated_cost, bool)
        or not math.isfinite(estimated_cost)
        or estimated_cost < 0
    ):
        raise StateError(f"{field}.estimated_cost must be non-negative or null")
    return _node_record(
        node_id=node_id,
        title=_text(node["title"], f"{field}.title", maximum=1024),
        stage=stage,
        priority=priority,
        dependencies=_text_list(
            node["dependencies"], f"{field}.dependencies", identifiers=True
        ),
        write_scopes=_text_list(
            node["write_scopes"], f"{field}.write_scopes", maximum=32
        ),
        role=role,
        model=model,
        effort=effort,
        acceptance=_text_list(
            node["acceptance"], f"{field}.acceptance", required=True
        ),
        route_rationale=_text(
            node["route_rationale"], f"{field}.route_rationale", maximum=4096
        ),
        estimated_cost=estimated_cost,
        spec=node["spec"],
        assessment=node["assessment"],
    )


def _require_routable_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = state["nodes"].get(node_id)
    future = bool(
        node
        and node["status"] in ("pending", "ready")
        and node["launch"]["state"] == "unclaimed"
    )
    failed_retry = bool(
        node
        and node["status"] == "failed"
        and node["launch"]["state"] in ("unclaimed", "terminal")
    )
    if (
        not node
        or not (future or failed_retry)
        or not _assessment_is_current_executable(state, node_id)
    ):
        raise StateError(
            "routing requires current executable future or failed leaf work"
        )
    if _workflow_dispatch_blocked(state) or node_id in _active_blocked_node_ids(state):
        raise StateError("routing requires an unblocked workflow and node")
    if not _planning_at_fixed_point(state):
        raise StateError(PLANNING_FIXED_POINT_ERROR)
    return node


def _route_node(
    state: dict[str, Any],
    node_id: str,
    *,
    role: str,
    model: str | None,
    effort: str | None,
    rationale: str,
) -> dict[str, Any]:
    node = _require_routable_node(state, node_id)
    prior_dependency_snapshot = _dependency_snapshot(node_id, node)
    _reset_failed_leaf(node)
    if _dependency_snapshot(node_id, node) != prior_dependency_snapshot:
        _invalidate_direct_dependents(state, node_id)
    node.update({"role": role, "model": model, "effort": effort})
    node["route"] = {
        "rationale": rationale,
        "routed_at": now_iso(),
        "attempt": len(node["attempts"]) + 1,
    }
    return add_event(state, "node_routed", rationale, node_id)


def _routing_task(
    node: Mapping[str, Any],
    *,
    criticality: int,
    determinism: int,
) -> dict[str, Any]:
    assessment = node["assessment"]
    return {
        "summary": node["spec"]["objective"],
        "stage": node["stage"],
        "complexity": max(1, min(5, math.ceil(assessment["total"] / 4))),
        "ambiguity": max(1, min(5, assessment["ambiguity_total"] + 1)),
        "criticality": criticality,
        "coupling": assessment["dimensions"]["coupling"] + 1,
        "novelty": assessment["dimensions"]["novelty"] + 1,
        "determinism": determinism,
    }


def _compact_routing_selection(selection: Mapping[str, Any]) -> dict[str, Any]:
    """Keep routing diagnostics useful without echoing the full candidate catalog."""
    profile = selection.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    candidates = profile.get("candidates")
    candidate_count = profile.get("candidate_count")
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool):
        candidate_count = len(candidates) if isinstance(candidates, list) else 0
    alternatives = selection.get("alternatives")
    return {
        "task_digest": selection.get("task_digest"),
        "route": copy.deepcopy(selection.get("route")),
        "rationale": selection.get("rationale"),
        "inputs": copy.deepcopy(selection.get("inputs")),
        "profile": {
            "budget": profile.get("budget"),
            "candidate_count": candidate_count,
        },
        "alternatives": copy.deepcopy(alternatives[:5])
        if isinstance(alternatives, list)
        else [],
        "caveat": selection.get("caveat"),
    }


def _launch_identifiers(
    workflow_id: str,
    node_id: str,
    mutation_id: str,
) -> tuple[str, str]:
    seed = f"{workflow_id}\0{node_id}\0{mutation_id}".encode()
    request_digest = hashlib.sha256(seed + b"\0request").hexdigest()
    child_digest = hashlib.sha256(seed + b"\0child").hexdigest()
    fragment = node_id[:48]
    request_id = f"req-{fragment}-{request_digest}"
    child_id = f"child-{fragment}-{child_digest}"
    _identifier(request_id, "generated request_id")
    _identifier(child_id, "generated child_id")
    return request_id, child_id


def _claim_node(
    state: dict[str, Any],
    node_id: str,
    request_id: str,
) -> dict[str, Any]:
    node = state["nodes"].get(node_id)
    if node is None:
        raise StateError("unknown node")
    if not _planning_at_fixed_point(state):
        raise StateError(PLANNING_FIXED_POINT_ERROR)
    if node_id not in ready_nodes(state):
        raise StateError(
            "node-claim requires ready, dependency-safe, unblocked future work"
        )
    _identifier(request_id, "request_id")
    if node["attempts"] and node["attempts"][-1]["finished_at"] is None:
        raise StateError("prior launch attempt must be reconciled before another claim")
    if node["route"]["attempt"] != len(node["attempts"]) + 1:
        raise StateError("persist a fresh node route for this launch attempt")
    if len(node["attempts"]) >= MAX_ATTEMPTS:
        raise StateError(
            "node attempt limit reached", code="capacity_exceeded", exit_code=20
        )
    if node["status"] == "pending":
        node["status"] = "ready"
    claimed_at = now_iso()
    node["launch"].update(
        {
            "state": "claimed",
            "request_id": request_id,
            "claimed_at": claimed_at,
            "child_id": None,
            "reconciliation": None,
        }
    )
    node["attempts"].append(
        {
            "number": node["route"]["attempt"],
            "request_id": request_id,
            "child_id": None,
            "started_at": claimed_at,
            "finished_at": None,
            "outcome": None,
            "scope_baseline": _scope_snapshot(state, node),
            "scope_evidence": {},
        }
    )
    return add_event(state, "node_claimed", "launch claim persisted", node_id)


def _start_node(state: dict[str, Any], node_id: str, child_id: str) -> dict[str, Any]:
    node = state["nodes"].get(node_id)
    if node is None:
        raise StateError("unknown node")
    if node["launch"]["state"] != "claimed" or node["status"] != "ready":
        raise StateError("node-start requires a claimed ready node")
    child_id = _identifier(child_id, "child_id")
    historical = {
        attempt["child_id"]
        for other in state["nodes"].values()
        for attempt in other["attempts"]
        if attempt["child_id"] is not None
    }
    if child_id in historical:
        raise StateError("child_id was already used by an earlier attempt")
    if any(
        state["nodes"][dependency]["status"] not in SUCCESS_NODE_STATUSES
        for dependency in node["dependencies"]
    ):
        raise StateError("node dependencies are not terminal-successful")
    node["launch"].update(
        {
            "state": "running",
            "child_id": child_id,
            "reconciliation": None,
        }
    )
    node["attempts"][-1]["child_id"] = child_id
    node["status"] = "running"
    state["status"] = "running"
    state["phase"] = node["stage"]
    _refresh_recovery_status(state)
    return add_event(state, "node_started", "child bound and running", node_id)


def _complete_node(
    state: dict[str, Any],
    node_id: str,
    *,
    outcome: str,
    result: str,
    evidence: str,
    actual_cost: float | None,
) -> dict[str, Any]:
    node = state["nodes"].get(node_id)
    if node is None:
        raise StateError("unknown node")
    if node["status"] != "running" or node["launch"]["state"] != "running":
        raise StateError("node-complete requires a running node and launch")
    if outcome not in ("succeeded", "failed"):
        raise StateError("node-complete outcome is invalid")
    result = _text(result, "node result")
    evidence = _text(evidence, "node evidence")
    if actual_cost is not None and (
        not isinstance(actual_cost, (int, float))
        or isinstance(actual_cost, bool)
        or not math.isfinite(actual_cost)
        or actual_cost < 0
    ):
        raise StateError("actual_cost must be non-negative or null")
    prior_dependency_snapshot = _dependency_snapshot(node_id, node)
    if outcome == "succeeded":
        node["attempts"][-1]["scope_evidence"] = _complete_scope_evidence(
            state, node
        )
        node["status"] = "done"
    else:
        node["status"] = "failed"
    node["result"] = result
    node["evidence"] = evidence
    node["actual_cost"] = actual_cost
    node["launch"]["state"] = "terminal"
    node["attempts"][-1].update(
        {"finished_at": now_iso(), "outcome": outcome}
    )
    if node["status"] == "failed" and _assessable_leaf(node):
        _invalidate_assessment(state, node)
    if _dependency_snapshot(node_id, node) != prior_dependency_snapshot:
        _invalidate_direct_dependents(state, node_id)
    return add_event(
        state,
        "node_completed",
        f"node outcome={outcome}",
        node_id,
    )


def _finish_workflow_state(
    state: dict[str, Any],
    *,
    summary: str,
    validation: str,
) -> dict[str, Any]:
    summary = _text(summary, "finish summary", maximum=4096)
    validation = _text(
        validation,
        "finish validation",
        maximum=MAX_TEXT - len(summary) - len(FINISH_EVENT_SEPARATOR),
    )
    if not state["nodes"] or any(
        not _node_resolves_completion(node) for node in state["nodes"].values()
    ):
        raise StateError(
            "all visible nodes must resolve through done work, decomposition, or supersede"
        )
    if any(item["status"] == "active" for item in state["requirements"].values()):
        raise StateError("all requirements must be resolved")
    if any(item["status"] == "active" for item in state["blockers"]):
        raise StateError("all blockers must be resolved")
    _verify_finish_scopes(state)
    state["status"] = "completed"
    state["phase"] = "completed"
    return add_event(
        state,
        "workflow_finished",
        summary + FINISH_EVENT_SEPARATOR + validation,
    )


def next_action(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return one compact, deterministic controller action without mutating state."""
    base: dict[str, Any] = {
        "revision": state["revision"],
        "workflow_status": state["status"],
        "phase": state["phase"],
        "available_parallelism": planning_diagnostics(state)[
            "available_parallelism"
        ],
        "warnings": [],
    }

    def action(
        name: str,
        reason: str,
        *,
        command: str | None = None,
        node_ids: list[str] | None = None,
        required: list[str] | None = None,
        blocker_ids: list[str] | None = None,
        requirement_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            **base,
            "action": name,
            "reason": reason,
            "command": command,
            "node_ids": node_ids or [],
            "blocker_ids": blocker_ids or [],
            "requirement_ids": requirement_ids or [],
            "required": required or [],
        }

    if state["status"] == "completed":
        return action("done", "workflow is terminal")
    if state["controller"]["resume_required"]:
        return action(
            "resume",
            "controller takeover requires explicit resume",
            command="resume",
            required=["message"],
        )
    uncertain = sorted(
        node_id
        for node_id, node in state["nodes"].items()
        if node["launch"]["state"] == "reconcile_required"
    )
    if uncertain:
        return action(
            "reconcile",
            "provider outcomes must be reconciled before retry or completion",
            command="node-update",
            node_ids=uncertain,
            required=["launch_state", "reconciliation"],
        )
    aborted_bound = sorted(
        node_id
        for node_id, node in state["nodes"].items()
        if state["status"] == "aborted" and node["launch"]["state"] == "bound"
    )
    if aborted_bound:
        return action(
            "reconcile",
            "aborted child outcomes must become terminal before recovery is clean",
            command="node-update",
            node_ids=aborted_bound,
            required=["launch_state=terminal", "reconciliation", "attempt_outcome"],
        )
    if state["status"] == "aborted":
        return action("done", "aborted workflow has no unresolved provider outcomes")
    workflow_blockers = [
        item
        for item in state["blockers"]
        if item["status"] == "active" and item["node_id"] is None
    ]
    if workflow_blockers:
        return action(
            "resolve_blocker",
            "a workflow-level blocker prevents dispatch",
            command="unblock",
            blocker_ids=sorted(item["id"] for item in workflow_blockers),
            required=["blocker_id", "resolution"],
        )
    if not state["nodes"]:
        return action(
            "plan",
            "workflow has no work graph; apply a non-empty atomic plan",
            command="plan-apply",
            requirement_ids=sorted(
                requirement_id
                for requirement_id, requirement in state["requirements"].items()
                if requirement["status"] == "active"
            ),
            required=["plan_file"],
        )
    blocked_nodes = _active_blocked_node_ids(state)
    assessable = [
        (node_id, node)
        for node_id, node in state["nodes"].items()
        if _assessable_leaf(node) and node_id not in blocked_nodes
    ]
    refinement = sorted(
        node_id
        for node_id, node in assessable
        if _derived_assessment_state(state, node) == "refinement_required"
    )
    if refinement:
        return action(
            "refine",
            "material ambiguity must be resolved before dispatch",
            command="node-refine",
            node_ids=refinement,
            required=["refinement_file"],
        )
    split = sorted(
        node_id
        for node_id, node in assessable
        if _derived_assessment_state(state, node) == "split_required"
    )
    if split:
        return action(
            "split",
            "complexity policy requires bounded child work",
            command="node-split",
            node_ids=split,
            required=["plan_file"],
        )
    stale = sorted(
        node_id
        for node_id, node in assessable
        if _derived_assessment_state(state, node) == "stale"
    )
    if stale:
        return action(
            "reassess",
            "assessment inputs changed and must be recalculated",
            command="node-refine",
            node_ids=stale,
            required=["refinement_file"],
        )
    claimed = sorted(
        node_id
        for node_id, node in state["nodes"].items()
        if node["launch"]["state"] == "claimed"
    )
    if claimed:
        return action(
            "spawn_and_start",
            "claimed work must be delegated and bound before execution",
            command="node-start",
            node_ids=claimed,
            required=["child_id"],
        )
    bound = sorted(
        node_id
        for node_id, node in state["nodes"].items()
        if node["launch"]["state"] == "bound"
    )
    if bound:
        return action(
            "start",
            "bound work must transition to running",
            command="node-update",
            node_ids=bound,
            required=["status=running"],
        )
    dispatch = ready_nodes(state)
    failed_retry = sorted(
        node_id
        for node_id, node in assessable
        if node["status"] == "failed"
        and _assessment_is_current_executable(state, node_id)
    )
    candidates = list(dict.fromkeys([*dispatch, *failed_retry]))
    needs_route = [
        node_id
        for node_id in candidates
        if state["nodes"][node_id]["route"]["attempt"]
        != len(state["nodes"][node_id]["attempts"]) + 1
    ]
    if needs_route:
        return action(
            "route",
            "ready work needs a fresh route for its next attempt",
            command="node-route-auto",
            node_ids=needs_route,
            required=["node_id", "criticality", "determinism"],
        )
    if candidates and base["available_parallelism"] > 0:
        capacity = base["available_parallelism"]
        return action(
            "claim",
            "routed dependency-safe work is ready for launch within available capacity",
            command="node-claim",
            node_ids=candidates[:capacity],
            required=["node_id"],
        )
    active = sorted(
        node_id
        for node_id, node in state["nodes"].items()
        if node["launch"]["state"] in ("bound", "running")
    )
    if active:
        return action(
            "wait",
            "active attempts must return evidence before the next transition",
            node_ids=active,
        )
    active_requirements = sorted(
        requirement_id
        for requirement_id, requirement in state["requirements"].items()
        if requirement["status"] == "active"
    )
    unresolved_nodes = sorted(
        node_id
        for node_id, node in state["nodes"].items()
        if not _node_resolves_completion(node)
    )
    active_blockers = sorted(
        item["id"] for item in state["blockers"] if item["status"] == "active"
    )
    if not unresolved_nodes and active_requirements and not active_blockers:
        return action(
            "complete_requirements",
            "all work resolves; active requirements need evidence at closeout",
            command="workflow-complete",
            requirement_ids=active_requirements,
            required=["summary", "validation", "requirements"],
        )
    if not unresolved_nodes and not active_requirements and not active_blockers:
        return action(
            "finish",
            "all completion gates are resolved",
            command="workflow-complete",
            required=["summary", "validation", "requirements"],
        )
    node_blockers = [
        item
        for item in state["blockers"]
        if item["status"] == "active" and item["node_id"] is not None
    ]
    if node_blockers:
        return action(
            "resolve_blocker",
            "blocked nodes require external resolution",
            command="unblock",
            blocker_ids=sorted(item["id"] for item in node_blockers),
            node_ids=sorted(
                item["node_id"] for item in node_blockers if item["node_id"]
            ),
            required=["blocker_id", "resolution"],
        )
    return action(
        "blocked",
        "no legal automatic progress is available; inspect unresolved nodes",
        node_ids=unresolved_nodes,
    )


def execute_command(args: Any, store: StateStore) -> tuple[int, str, Any, list[str]]:
    """Execute one parsed state command at the state owner boundary."""
    command = args.command
    if command == "session-open":
        repository = canonical_repository(pathlib.Path(args.repo))
        return 0, "session_opened", store.open_session(repository, pathlib.Path(args.session_file)), []
    if command == "session-close":
        return 0, "session_closed", store.close_session(pathlib.Path(args.session_file)), []
    if command == "init":
        repository = canonical_repository(pathlib.Path(args.repo))
        task = _read_command_text(args.task, args.task_file, "task")
        profile = None
        if args.profile_file:
            profile = _read_command_object(None, args.profile_file, "profile")
            allowed = {
                "max_parallel", "reserve",
                "node_complexity_split_threshold", "dimension_complexity_split_threshold",
                "node_ambiguity_refine_threshold", "factor_ambiguity_refine_threshold",
                "max_refinement_depth",
            }
            if set(profile) - allowed:
                raise StateError("profile contains unknown fields: " + ", ".join(sorted(set(profile) - allowed)))
        state = store.create(repository, task, pathlib.Path(args.session_file), profile, args.mutation_id)
        return 0, "workflow_created", _public_state(state), []
    if command == "list":
        states = store.list_valid()
        if args.repo:
            identity = canonical_repository(pathlib.Path(args.repo))["identity"]
            states = [state for state in states if state["repository"]["identity"] == identity]
        return 0, "workflows_listed", [_public_state(state) for state in states], []
    if command in ("status", "context", "next"):
        state = store.load(args.workflow_id)
        if command == "next":
            return 0, "next_action_selected", next_action(state), []
        return 0, "workflow_loaded", _public_state(state, full=command == "context"), []
    if command == "reconcile-mutation":
        result = store.reconcile_mutation(args.workflow_id, args.mutation_id, args.digest)
        exit_code = 0 if result["outcome"] == "applied" else 1
        return exit_code, "mutation_" + result["outcome"], result, []
    if command == "controller-takeover":
        state = store.takeover(
            args.workflow_id,
            session_file=pathlib.Path(args.session_file),
            mutation_id=args.mutation_id,
            expected_revision=args.expected_revision,
        )
        return 0, "controller_taken_over", _public_state(state), []

    if command == "resume":
        def resume(state: dict[str, Any]) -> dict[str, Any]:
            if not state["controller"]["resume_required"]:
                raise StateError("workflow does not require resume")
            state["controller"]["resume_required"] = False
            _refresh_recovery_status(state)
            return add_event(state, "workflow_resumed", args.message)

        state, result, replay = _mutate_command(
            store, args, command, {"message": args.message}, resume, allow_resume_required=True
        )
        return 0, "mutation_reconciled" if replay else "workflow_resumed", {**_public_state(state), "event": result}, []

    if command == "plan-apply":
        plan = _read_command_object(args.plan_json, args.plan_file, "plan")
        _keys(plan, {"requirements", "nodes"}, "plan")
        requirements = _plan_requirement_records(plan["requirements"])
        if not isinstance(plan["nodes"], list) or len(plan["nodes"]) > MAX_NODES:
            raise StateError(
                f"plan.nodes must be a list with at most {MAX_NODES} entries"
            )
        nodes = [
            _plan_node_record(raw, index)
            for index, raw in enumerate(plan["nodes"])
        ]
        node_ids = [node["id"] for node in nodes]
        if len(set(node_ids)) != len(node_ids):
            raise StateError("plan node identifiers must be unique")
        if not requirements and not nodes:
            raise StateError("plan must add at least one requirement or node")

        def apply_plan(state: dict[str, Any]) -> dict[str, Any]:
            requirement_collisions = set(requirements) & set(state["requirements"])
            if requirement_collisions:
                raise StateError(
                    "plan requirement identifiers already exist: "
                    + ", ".join(sorted(requirement_collisions))
                )
            node_collisions = set(node_ids) & set(state["nodes"])
            if node_collisions:
                raise StateError(
                    "plan node identifiers already exist: "
                    + ", ".join(sorted(node_collisions))
                )
            if len(state["requirements"]) + len(requirements) > 256:
                raise StateError(
                    "requirement capacity would be exceeded",
                    code="capacity_exceeded",
                    exit_code=20,
                )
            if len(state["nodes"]) + len(nodes) > MAX_NODES:
                raise StateError(
                    "node capacity would be exceeded",
                    code="capacity_exceeded",
                    exit_code=20,
                )
            for requirement_id, requirement in requirements.items():
                state["requirements"][requirement_id] = copy.deepcopy(requirement)
            for node in nodes:
                state["nodes"][node["id"]] = copy.deepcopy(node)
            known_ids = set(state["nodes"])
            for node in nodes:
                unknown = set(node["dependencies"]) - known_ids
                if unknown:
                    raise StateError(
                        f"plan node {node['id']} has unresolved dependencies: "
                        + ", ".join(sorted(unknown))
                    )
            for node in nodes:
                _refresh_node_assessment(state, node["id"])
            event = add_event(
                state,
                "plan_applied",
                f"added {len(requirements)} requirements and {len(nodes)} nodes",
            )
            return {
                "requirement_ids": sorted(requirements),
                "node_ids": node_ids,
                "event": event,
            }

        state, result, replay = _mutate_command(
            store, args, command, plan, apply_plan
        )
        return (
            0,
            "mutation_reconciled" if replay else "plan_applied",
            {
                **_public_state(state),
                "plan": result,
                "planning": planning_diagnostics(state),
            },
            [],
        )

    if command == "node-add":
        node = _new_node(args)
        operation = {**node, "route": {"rationale": args.rationale, "attempt": 0}}

        def add(state: dict[str, Any]) -> dict[str, Any]:
            if args.node_id in state["nodes"]:
                raise StateError("node already exists")
            if any(dependency not in state["nodes"] for dependency in node["dependencies"]):
                raise StateError("node has an unresolved dependency")
            state["nodes"][args.node_id] = copy.deepcopy(node)
            _refresh_node_assessment(state, args.node_id)
            return add_event(state, "node_added", args.title, args.node_id)

        state, result, replay = _mutate_command(store, args, command, operation, add)
        return 0, "mutation_reconciled" if replay else "node_added", {**_public_state(state), "event": result}, []

    if command == "node-refine":
        refinement = _read_command_object(args.refinement_json, args.refinement_file, "refinement")
        _keys(refinement, {"spec", "acceptance", "write_scopes", "assessment"}, "refinement")

        def refine(state: dict[str, Any]) -> dict[str, Any]:
            node = state["nodes"].get(args.node_id)
            if node is None:
                raise StateError("unknown node")
            _require_rewritable_leaf(node, "node-refine")
            if _derived_assessment_state(state, node) == "split_required":
                raise StateError("node-refine cannot replace required decomposition; use node-split")
            prior_obligations = _effective_obligations(node)
            prior_dependency_snapshot = _dependency_snapshot(args.node_id, node)
            spec = _validate_spec(refinement["spec"], "refinement.spec")
            acceptance = _text_list(refinement["acceptance"], "refinement.acceptance", required=True)
            write_scopes = _text_list(
                refinement["write_scopes"], "refinement.write_scopes", maximum=32
            )
            write_scopes = [
                _canonical_scope(scope, "refinement.write_scope") for scope in write_scopes
            ]
            assessment = _validate_assessment_inputs(refinement["assessment"], "refinement.assessment")
            node["spec"] = copy.deepcopy(dict(spec))
            node["acceptance"] = acceptance
            node["write_scopes"] = write_scopes
            for field in OBLIGATION_FIELDS:
                node["lineage"]["obligations"][field] = _ordered_union(
                    node["lineage"]["obligations"][field],
                    prior_obligations[field],
                )
            node["assessment"] = _assessment_shell(assessment)
            node["model"] = None
            node["effort"] = None
            node["route"]["attempt"] = len(node["attempts"])
            if node["status"] == "ready":
                node["status"] = "pending"
            _reset_failed_leaf(node)
            _refresh_node_assessment(state, args.node_id)
            if (
                _raw_over_budget(state, node)
                and node["lineage"]["depth"] >= state["conventions"]["max_refinement_depth"]
            ):
                raise StateError("max_refinement_depth must produce a bounded final leaf")
            if _dependency_snapshot(args.node_id, node) != prior_dependency_snapshot:
                _invalidate_direct_dependents(state, args.node_id)
            return add_event(state, "node_refined", node["assessment"]["rationale"], args.node_id)

        operation = {"node_id": args.node_id, "refinement": refinement}
        state, result, replay = _mutate_command(store, args, command, operation, refine)
        return 0, "mutation_reconciled" if replay else "node_refined", {**_public_state(state), "event": result}, []

    if command == "node-split":
        plan = _read_command_object(args.plan_json, args.plan_file, "plan")
        _keys(plan, {"parent_id", "reason", "children", "coverage", "dependent_replacements"}, "plan")

        def split(state: dict[str, Any]) -> dict[str, Any]:
            parent_id = _identifier(plan["parent_id"], "plan.parent_id")
            parent = state["nodes"].get(parent_id)
            if parent is None:
                raise StateError("split parent is unknown")
            _require_rewritable_leaf(parent, "node-split")
            reason = _text(plan["reason"], "plan.reason", maximum=4096)
            if (
                parent["assessment"]["input_digest"] != _assessment_input_digest(state, parent)
                or parent["assessment"]["state"] != _derived_assessment_state(state, parent)
            ):
                raise StateError("node-split requires a current parent assessment")
            failed_executable = (
                parent["status"] == "failed" and parent["assessment"]["state"] == "executable"
            )
            if parent["assessment"]["state"] != "split_required" and not failed_executable:
                raise StateError("node-split requires split-required or current executable failed work")
            depth = parent["lineage"]["depth"] + 1
            if depth > state["conventions"]["max_refinement_depth"]:
                raise StateError("node-split exceeds max_refinement_depth")
            if not isinstance(plan["children"], list) or not 2 <= len(plan["children"]) <= MAX_NODES:
                raise StateError("plan.children must contain at least two bounded child definitions")
            if len(state["nodes"]) + len(plan["children"]) > MAX_NODES:
                raise StateError("node capacity would be exceeded", code="capacity_exceeded", exit_code=20)

            children = [
                _split_child_record(raw, parent_id=parent_id, depth=depth, index=index)
                for index, raw in enumerate(plan["children"])
            ]
            child_ids = [child["id"] for child in children]
            child_id_set = set(child_ids)
            if len(child_id_set) != len(child_ids):
                raise StateError("plan.children identifiers must be unique")
            collisions = child_id_set & set(state["nodes"])
            if collisions:
                raise StateError("plan.children identifiers already exist: " + ", ".join(sorted(collisions)))
            known_ids = set(state["nodes"]) | child_id_set
            for child in children:
                unknown = set(child["dependencies"]) - known_ids
                if unknown:
                    raise StateError(
                        f"plan child {child['id']} has unresolved dependencies: " + ", ".join(sorted(unknown))
                    )

            coverage = _keys(plan["coverage"], set(COVERAGE_FIELDS), "plan.coverage")
            effective_obligations = _effective_obligations(parent)
            coverage_mappings = {
                field: _coverage_mapping(
                    coverage[field],
                    effective_obligations[field],
                    child_id_set,
                    f"plan.coverage.{field}",
                )
                for field in COVERAGE_FIELDS
            }
            artifact_children = [child for child in children if child["write_scopes"]]
            if effective_obligations["write_scopes"] and not artifact_children:
                raise StateError(
                    "node-split cannot convert artifact-scoped work into evidence-only children"
                )
            direct_dependents = sorted(
                node_id for node_id, node in state["nodes"].items() if parent_id in node["dependencies"]
            )
            rewritable_dependents: list[str] = []
            terminal_dependents: list[str] = []
            for dependent_id in direct_dependents:
                dependent = state["nodes"][dependent_id]
                if dependent["status"] in SUCCESS_NODE_STATUSES:
                    terminal_dependents.append(dependent_id)
                else:
                    _require_rewritable_leaf(dependent, "node-split dependent rewiring")
                    rewritable_dependents.append(dependent_id)
            dependent_replacements = _coverage_mapping(
                plan["dependent_replacements"], rewritable_dependents, child_id_set,
                "plan.dependent_replacements",
            )

            for child in children:
                state["nodes"][child["id"]] = child
            for field, mapping in coverage_mappings.items():
                for obligation, selected in mapping.items():
                    for child_id in selected:
                        child_obligations = state["nodes"][child_id]["lineage"]["obligations"]
                        child_obligations[field] = _ordered_union(
                            child_obligations[field], [obligation]
                        )
            for child_id in child_ids:
                child_obligations = state["nodes"][child_id]["lineage"]["obligations"]
                for field in ("objectives", "inputs", "constraints", "non_goals"):
                    child_obligations[field] = _ordered_union(
                        child_obligations[field], effective_obligations[field]
                    )
            for child in artifact_children:
                child_obligations = state["nodes"][child["id"]]["lineage"]["obligations"]
                child_obligations["write_scopes"] = _ordered_union(
                    child_obligations["write_scopes"],
                    effective_obligations["write_scopes"],
                )
            for child_id in child_ids:
                _refresh_node_assessment(state, child_id)
            if any(
                _raw_over_budget(state, state["nodes"][child_id])
                and state["nodes"][child_id]["lineage"]["depth"]
                >= state["conventions"]["max_refinement_depth"]
                for child_id in child_ids
            ):
                raise StateError("max_refinement_depth requires bounded final children")
            if any(
                state["nodes"][child_id]["assessment"]["total"] >= parent["assessment"]["total"]
                for child_id in child_ids
            ):
                raise StateError("every split child must have lower total complexity than its parent")
            parent_dependencies = list(parent["dependencies"])
            parent["dependencies"] = []

            for dependent_id in rewritable_dependents:
                dependent = state["nodes"][dependent_id]
                rewired: list[str] = []
                for dependency in dependent["dependencies"]:
                    replacements = dependent_replacements[dependent_id] if dependency == parent_id else [dependency]
                    for replacement in replacements:
                        if replacement not in rewired:
                            rewired.append(replacement)
                dependent["dependencies"] = rewired
                _invalidate_assessment(state, dependent)
            for dependent_id in terminal_dependents:
                dependent = state["nodes"][dependent_id]
                dependent["dependencies"] = [
                    dependency for dependency in dependent["dependencies"] if dependency != parent_id
                ]

            for dependency in parent_dependencies:
                if not any(
                    _child_reaches_prerequisite(
                        state["nodes"], child_id, dependency, child_id_set
                    )
                    for child_id in child_ids
                ):
                    raise StateError(f"split silently drops parent prerequisite {dependency}")

            parent["lineage"]["child_ids"] = child_ids
            parent["lineage"]["split_reason"] = reason
            parent["assessment"]["state"] = "decomposed"
            parent["status"] = "skipped"
            if parent["result"] is None:
                parent["result"] = "decomposed"
            if parent["evidence"] is None:
                parent["evidence"] = reason
            return add_event(state, "node_split", reason, parent_id)

        state, result, replay = _mutate_command(store, args, command, plan, split)
        return 0, "mutation_reconciled" if replay else "node_split", {**_public_state(state), "event": result}, []

    if command == "node-route":
        operation = {
            "node_id": args.node_id,
            "role": args.role,
            "model": args.model,
            "effort": args.effort,
            "rationale": args.rationale,
        }

        def route(state: dict[str, Any]) -> dict[str, Any]:
            return _route_node(
                state,
                args.node_id,
                role=args.role,
                model=args.model,
                effort=args.effort,
                rationale=args.rationale,
            )

        state, result, replay = _mutate_command(store, args, command, operation, route)
        return 0, "mutation_reconciled" if replay else "node_routed", {**_public_state(state), "event": result}, []

    if command == "node-route-auto":
        profile = (
            _read_command_object(None, args.profile_file, "profile")
            if args.profile_file
            else None
        )
        operation = {
            "node_id": args.node_id,
            "criticality": args.criticality,
            "determinism": args.determinism,
            "profile": profile,
        }
        def auto_route(state: dict[str, Any]) -> dict[str, Any]:
            node = _require_routable_node(state, args.node_id)
            task = _routing_task(
                node,
                criticality=args.criticality,
                determinism=args.determinism,
            )
            try:
                selection = choose(task, profile)
            except RoutingError as exc:
                raise StateError(
                    str(exc), code="invalid_routing_input", exit_code=2
                ) from exc
            compact_selection = _compact_routing_selection(selection)
            route = selection["route"]
            event = _route_node(
                state,
                args.node_id,
                role=route["role"],
                model=route["model"],
                effort=route["effort"],
                rationale=selection["rationale"],
            )
            return {"event": event, "selection": compact_selection}

        state, result, replay = _mutate_command(
            store, args, command, operation, auto_route
        )
        node = state["nodes"][args.node_id]
        raw_selection = (
            result.get("selection")
            if isinstance(result, dict) and isinstance(result.get("selection"), dict)
            else {
                "route": {
                    "role": node["role"],
                    "model": node["model"],
                    "effort": node["effort"],
                },
                "rationale": node["route"]["rationale"],
                "profile": {"budget": None, "candidate_count": 0},
            }
        )
        routing = _compact_routing_selection(raw_selection)
        return (
            0,
            "mutation_reconciled" if replay else "node_routed",
            {**_public_state(state), "routing": routing},
            [],
        )

    if command == "node-claim":
        request_id, suggested_child_id = _launch_identifiers(
            args.workflow_id, args.node_id, args.mutation_id
        )
        operation = {
            "node_id": args.node_id,
            "request_id": request_id,
            "suggested_child_id": suggested_child_id,
        }

        def claim(state: dict[str, Any]) -> dict[str, Any]:
            return _claim_node(state, args.node_id, request_id)

        state, result, replay = _mutate_command(
            store, args, command, operation, claim
        )
        node = state["nodes"][args.node_id]
        attempt = next(
            (
                item["number"]
                for item in node["attempts"]
                if item["request_id"] == request_id
            ),
            None,
        )
        return (
            0,
            "mutation_reconciled" if replay else "node_claimed",
            {
                **_public_state(state),
                "node_id": args.node_id,
                "request_id": request_id,
                "suggested_child_id": suggested_child_id,
                "attempt": attempt,
                "event": result,
            },
            [],
        )

    if command == "node-start":
        operation = {"node_id": args.node_id, "child_id": args.child_id}

        def start(state: dict[str, Any]) -> dict[str, Any]:
            return _start_node(state, args.node_id, args.child_id)

        state, result, replay = _mutate_command(
            store, args, command, operation, start
        )
        return (
            0,
            "mutation_reconciled" if replay else "node_started",
            {**_public_state(state), "event": result},
            [],
        )

    if command == "node-complete":
        if args.result_file == "-" and args.evidence_file == "-":
            raise StateError(
                "result and evidence cannot both read from standard input; "
                "use a regular file or inline value for one of them",
                code="invalid_invocation",
                exit_code=2,
            )
        result_text = _read_command_text(
            args.result, args.result_file, "result"
        )
        evidence_text = _read_command_text(
            args.evidence, args.evidence_file, "evidence"
        )
        operation = {
            "node_id": args.node_id,
            "outcome": args.outcome,
            "result": result_text,
            "evidence": evidence_text,
            "actual_cost": args.actual_cost,
        }

        def complete(state: dict[str, Any]) -> dict[str, Any]:
            return _complete_node(
                state,
                args.node_id,
                outcome=args.outcome,
                result=result_text,
                evidence=evidence_text,
                actual_cost=args.actual_cost,
            )

        state, result, replay = _mutate_command(
            store, args, command, operation, complete
        )
        return (
            0,
            "mutation_reconciled" if replay else "node_completed",
            {**_public_state(state), "event": result},
            [],
        )

    if command == "node-update":
        operation = {
            "node_id": args.node_id,
            "status": args.status,
            "launch_state": args.launch_state,
            "request_id": args.request_id,
            "child_id": args.child_id,
            "reconciliation": args.reconciliation,
            "result": args.result,
            "evidence": args.evidence,
            "actual_cost": args.actual_cost,
            "attempt_outcome": args.attempt_outcome,
        }

        def update(state: dict[str, Any]) -> dict[str, Any]:
            node = state["nodes"].get(args.node_id)
            if not node:
                raise StateError("unknown node")
            prior_dependency_snapshot = _dependency_snapshot(args.node_id, node)
            if not any(value is not None for key, value in operation.items() if key != "node_id"):
                raise StateError("node-update requires a changed field")
            if args.launch_state is not None:
                allowed_launch = {
                    "unclaimed": {"claimed"},
                    "claimed": {"reconcile_required", "bound"},
                    "reconcile_required": {"bound", "running", "terminal", "unclaimed"},
                    "bound": {"running", "terminal"},
                    "running": {"terminal"},
                    "terminal": set(),
                }
                old_launch = node["launch"]["state"]
                if args.launch_state not in allowed_launch[old_launch]:
                    raise StateError(f"invalid launch transition {old_launch} -> {args.launch_state}")
                if args.launch_state == "claimed":
                    if not _planning_at_fixed_point(state):
                        raise StateError(PLANNING_FIXED_POINT_ERROR)
                    if args.node_id not in ready_nodes(state):
                        raise StateError("launch claim requires ready, dependency-safe, unblocked future work")
                    if not args.request_id:
                        raise StateError("launch claim requires --request-id")
                    _identifier(args.request_id, "request_id")
                    if node["attempts"] and node["attempts"][-1]["finished_at"] is None:
                        raise StateError("prior launch attempt must be reconciled before another claim")
                    if node["route"]["attempt"] != len(node["attempts"]) + 1:
                        raise StateError("persist a fresh node-route for this launch attempt")
                    if node["status"] == "pending":
                        node["status"] = "ready"
                    claimed_at = now_iso()
                    node["launch"].update(
                        {"state": "claimed", "request_id": args.request_id, "claimed_at": claimed_at, "child_id": None, "reconciliation": None}
                    )
                    if len(node["attempts"]) >= MAX_ATTEMPTS:
                        raise StateError("node attempt limit reached", code="capacity_exceeded", exit_code=20)
                    node["attempts"].append(
                        {
                            "number": node["route"]["attempt"],
                            "request_id": args.request_id,
                            "child_id": None,
                            "started_at": claimed_at,
                            "finished_at": None,
                            "outcome": None,
                            "scope_baseline": _scope_snapshot(state, node),
                            "scope_evidence": {},
                        }
                    )
                elif args.launch_state == "reconcile_required":
                    node["launch"]["state"] = "reconcile_required"
                    node["launch"]["reconciliation"] = args.reconciliation or "provider outcome is uncertain"
                    state["controller"]["recovery_status"] = "reconcile_required"
                elif args.launch_state in ("bound", "running"):
                    child_id = args.child_id or node["launch"]["child_id"]
                    if not child_id:
                        raise StateError("bound launch requires --child-id")
                    if old_launch == "reconcile_required" and not args.reconciliation:
                        raise StateError("reconciled binding requires --reconciliation evidence")
                    known_child = node["launch"]["child_id"]
                    if known_child is not None and child_id != known_child:
                        raise StateError("reconciled binding must retain the known child identifier")
                    _identifier(child_id, "child_id")
                    node["launch"].update(
                        {
                            "state": args.launch_state,
                            "child_id": child_id,
                            "reconciliation": args.reconciliation,
                        }
                    )
                    node["attempts"][-1]["child_id"] = child_id
                    _refresh_recovery_status(state)
                elif args.launch_state == "unclaimed":
                    if not args.reconciliation:
                        raise StateError("safe retry requires provider reconciliation evidence")
                    if node["attempts"] and node["attempts"][-1]["finished_at"] is None:
                        node["attempts"][-1]["finished_at"] = now_iso()
                        node["attempts"][-1]["outcome"] = "provider confirmed not launched"
                    node["launch"] = {
                        "state": "unclaimed",
                        "request_id": None,
                        "child_id": None,
                        "claimed_at": None,
                        "reconciliation": args.reconciliation,
                    }
                    if node["status"] == "running":
                        node["status"] = "pending"
                        node["result"] = None
                        node["evidence"] = None
                    _invalidate_assessment(state, node)
                    _refresh_recovery_status(state)
                elif args.launch_state == "terminal" and (
                    old_launch == "reconcile_required" or state["status"] == "aborted"
                ):
                    if not args.reconciliation or not args.attempt_outcome:
                        raise StateError(
                            "reconciled terminal launch requires reconciliation and attempt outcome"
                        )
                    node["launch"].update({"state": "terminal", "reconciliation": args.reconciliation})
                    node["attempts"][-1].update({"finished_at": now_iso(), "outcome": args.attempt_outcome})
                    _refresh_recovery_status(state)
                else:
                    node["launch"]["state"] = args.launch_state
            if args.status is not None:
                allowed_status = {
                    "pending": {"ready", "blocked"},
                    "ready": {"running", "blocked"},
                    "running": {"done", "failed"},
                    "blocked": {"pending", "ready", "failed"},
                    "failed": {"pending"},
                    "done": set(),
                    "skipped": set(),
                    "cancelled": set(),
                }
                old_status = node["status"]
                if args.status not in allowed_status[old_status]:
                    raise StateError(f"invalid node transition {old_status} -> {args.status}")
                if args.status == "blocked" and node["launch"]["state"] != "unclaimed":
                    raise StateError("blocked transition requires an unclaimed launch")
                if args.status == "running":
                    if node["launch"]["state"] not in ("bound", "running"):
                        raise StateError("running node requires a bound child launch")
                    if any(state["nodes"][dependency]["status"] not in SUCCESS_NODE_STATUSES for dependency in node["dependencies"]):
                        raise StateError("node dependencies are not terminal-successful")
                    node["launch"]["state"] = "running"
                    state["status"] = "running"
                    state["phase"] = node["stage"]
                if args.status == "ready":
                    if (
                        node["launch"]["state"] != "unclaimed"
                        or node["lineage"]["child_ids"]
                        or args.node_id in _active_blocked_node_ids(state)
                        or not _assessment_is_current_executable(state, args.node_id)
                    ):
                        raise StateError(
                            "ready transition requires an unclaimed leaf with a current executable assessment"
                        )
                    if not _planning_at_fixed_point(state):
                        raise StateError(PLANNING_FIXED_POINT_ERROR)
                    if any(
                        state["nodes"][dependency]["status"] not in SUCCESS_NODE_STATUSES
                        for dependency in node["dependencies"]
                    ):
                        raise StateError("node dependencies are not terminal-successful")
                if args.status == "done":
                    if not args.result or not args.evidence:
                        raise StateError("done node requires --result and --evidence")
                    node["attempts"][-1]["scope_evidence"] = _complete_scope_evidence(state, node)
                node["status"] = args.status
                if args.status in TERMINAL_NODE_STATUSES:
                    node["result"] = args.result
                    node["evidence"] = args.evidence
                    if node["launch"]["child_id"]:
                        node["launch"]["state"] = "terminal"
                    if node["attempts"] and node["attempts"][-1]["finished_at"] is None:
                        node["attempts"][-1]["finished_at"] = now_iso()
                        node["attempts"][-1]["outcome"] = args.attempt_outcome or args.status
            if args.actual_cost is not None:
                node["actual_cost"] = args.actual_cost
            if node["status"] == "failed" and _assessable_leaf(node):
                _invalidate_assessment(state, node)
            if _dependency_snapshot(args.node_id, node) != prior_dependency_snapshot:
                _invalidate_direct_dependents(state, args.node_id)
            return add_event(state, "node_updated", f"node status={node['status']} launch={node['launch']['state']}", args.node_id)

        state, result, replay = _mutate_command(store, args, command, operation, update)
        return 0, "mutation_reconciled" if replay else "node_updated", {**_public_state(state), "event": result}, []

    if command == "graph-validate":
        state = store.load(args.workflow_id)
        diagnostics = graph_diagnostics(
            state["nodes"],
            case_sensitive=state["conventions"]["write_scope_case_sensitive"],
            platform=state["conventions"]["platform"],
        )
        diagnostics.update(planning_diagnostics(state))
        diagnostics["ready_nodes"] = diagnostics["dispatch_order"]
        return 0, "graph_valid", diagnostics, []

    if command == "graph-replan":
        plan = _read_command_object(args.plan_json, args.plan_file, "plan")
        if set(plan) != {"reason", "operations"} or not isinstance(plan["operations"], list) or not plan["operations"]:
            raise StateError("plan must contain exactly reason and a non-empty operations list")

        def replan(state: dict[str, Any]) -> dict[str, Any]:
            for item in plan["operations"]:
                if not isinstance(item, dict) or item.get("op") not in (
                    "dependency_add",
                    "dependency_remove",
                    "priority",
                    "supersede",
                ):
                    raise StateError("plan contains an unsupported operation")
                node_id = item.get("node_id")
                if not isinstance(node_id, str) or node_id not in state["nodes"]:
                    raise StateError("plan references an unknown node")
                node = state["nodes"][node_id]
                if node["status"] not in ("pending", "ready", "failed") or node["launch"]["state"] != "unclaimed":
                    raise StateError("replan can change only unclaimed future work")
                if item["op"] in ("dependency_add", "dependency_remove"):
                    dependency = item.get("dependency")
                    if (
                        set(item) != {"op", "node_id", "dependency"}
                        or not isinstance(dependency, str)
                        or dependency not in state["nodes"]
                    ):
                        raise StateError("dependency operation is malformed")
                    dependencies = node["dependencies"]
                    if item["op"] == "dependency_add" and dependency not in dependencies:
                        dependencies.append(dependency)
                        _invalidate_assessment(state, node)
                    if item["op"] == "dependency_remove" and dependency in dependencies:
                        dependencies.remove(dependency)
                        _invalidate_assessment(state, node)
                elif item["op"] == "priority":
                    if (
                        set(item) != {"op", "node_id", "value"}
                        or not isinstance(item["value"], int)
                        or isinstance(item["value"], bool)
                        or not 0 <= item["value"] <= 100
                    ):
                        raise StateError("priority operation is malformed")
                    node["priority"] = item["value"]
                else:
                    replacement = item.get("replacement")
                    if (
                        set(item) != {"op", "node_id", "replacement"}
                        or not isinstance(replacement, str)
                        or replacement not in state["nodes"]
                    ):
                        raise StateError("supersede operation is malformed")
                    if replacement == node_id:
                        raise StateError("node cannot supersede itself")
                    if _raw_over_budget(state, node):
                        raise StateError("split-policy work must use node-split, not supersede")
                    cursor = replacement
                    while cursor != node_id and state["nodes"][cursor]["superseded_by"] is not None:
                        cursor = state["nodes"][cursor]["superseded_by"]
                    if cursor == node_id:
                        raise StateError("superseded_by cycle is not allowed")
                    replacement_node = state["nodes"][replacement]
                    missing_prerequisites = [
                        dependency
                        for dependency in node["dependencies"]
                        if not _depends_on(state["nodes"], replacement, dependency)
                    ]
                    if missing_prerequisites:
                        _require_rewritable_leaf(
                            replacement_node, "graph-replan supersede replacement"
                        )
                        prior_replacement_snapshot = _dependency_snapshot(
                            replacement, replacement_node
                        )
                        replacement_node["dependencies"] = _ordered_union(
                            replacement_node["dependencies"], missing_prerequisites
                        )
                        _invalidate_assessment(state, replacement_node)
                        if (
                            _dependency_snapshot(replacement, replacement_node)
                            != prior_replacement_snapshot
                        ):
                            _invalidate_direct_dependents(state, replacement)
                    source_obligations = _effective_obligations(node)
                    replacement_obligations = _effective_obligations(replacement_node)
                    transfer_required = any(
                        not set(source_obligations[field]).issubset(
                            replacement_obligations[field]
                        )
                        for field in OBLIGATION_FIELDS
                    )
                    if transfer_required:
                        _require_rewritable_leaf(
                            replacement_node, "graph-replan supersede replacement"
                        )
                        prior_replacement_snapshot = _dependency_snapshot(
                            replacement, replacement_node
                        )
                        for field in OBLIGATION_FIELDS:
                            missing = [
                                value
                                for value in source_obligations[field]
                                if value not in replacement_obligations[field]
                            ]
                            replacement_node["lineage"]["obligations"][field] = _ordered_union(
                                replacement_node["lineage"]["obligations"][field],
                                missing,
                            )
                        _invalidate_assessment(state, replacement_node)
                        if (
                            _dependency_snapshot(replacement, replacement_node)
                            != prior_replacement_snapshot
                        ):
                            _invalidate_direct_dependents(state, replacement)
                    node["status"] = "skipped"
                    node["result"] = "superseded"
                    node["evidence"] = plan["reason"]
                    node["superseded_by"] = replacement
                    for other in state["nodes"].values():
                        if node_id in other["dependencies"]:
                            other["dependencies"] = [replacement if value == node_id else value for value in other["dependencies"]]
                            other["dependencies"] = list(dict.fromkeys(other["dependencies"]))
                            _invalidate_assessment(state, other)
            return add_event(state, "graph_replanned", plan["reason"])

        state, result, replay = _mutate_command(store, args, command, plan, replan)
        return 0, "mutation_reconciled" if replay else "graph_replanned", {**_public_state(state), "event": result}, []

    if command == "requirement-set":
        operation = {
            "requirement_id": args.requirement_id,
            "text": args.text,
            "source": args.source,
            "status": args.status,
            "evidence": args.evidence,
        }

        def requirement(state: dict[str, Any]) -> dict[str, Any]:
            if args.status != "active" and not args.evidence:
                raise StateError("resolved requirement needs evidence")
            replacement = {
                "text": args.text,
                "source": args.source,
                "status": args.status,
                "evidence": args.evidence,
            }
            prior = state["requirements"].get(args.requirement_id)
            semantic_change = prior is not None and (
                prior["text"] != replacement["text"]
                or prior["source"] != replacement["source"]
            )
            if semantic_change:
                locked = sorted(
                    node_id
                    for node_id, node in state["nodes"].items()
                    if args.requirement_id in _effective_obligations(node)["requirements"]
                    and _is_resolution_endpoint(node)
                    and not _assessable_leaf(node)
                )
                if locked:
                    raise StateError(
                        "referenced requirement text/source is immutable after work starts or completes: "
                        + ", ".join(locked)
                    )
            changed = prior != replacement
            state["requirements"][args.requirement_id] = replacement
            if changed and (prior is None or semantic_change):
                for node in state["nodes"].values():
                    if args.requirement_id in _effective_obligations(node)["requirements"]:
                        _invalidate_assessment(state, node)
            return add_event(state, "requirement_set", args.requirement_id)

        state, result, replay = _mutate_command(store, args, command, operation, requirement)
        return 0, "mutation_reconciled" if replay else "requirement_set", {**_public_state(state), "event": result}, []

    if command == "decision":
        operation = {"text": args.text, "rationale": args.rationale}

        def decision(state: dict[str, Any]) -> dict[str, Any]:
            item = {
                "id": "decision-" + hashlib.sha256(args.mutation_id.encode()).hexdigest()[:16],
                "text": args.text,
                "rationale": args.rationale,
                "at": now_iso(),
            }
            state["decisions"].append(item)
            add_event(state, "decision_recorded", args.text)
            return item

        state, result, replay = _mutate_command(store, args, command, operation, decision)
        return 0, "mutation_reconciled" if replay else "decision_recorded", {**_public_state(state), "decision": result}, []

    if command == "block":
        operation = {"node_id": args.node_id, "reason": args.reason, "needed": args.needed}

        def block(state: dict[str, Any]) -> dict[str, Any]:
            if args.node_id and args.node_id not in state["nodes"]:
                raise StateError("blocker references unknown node")
            if args.node_id:
                node = state["nodes"][args.node_id]
                if node["status"] not in ("pending", "ready") or node["launch"]["state"] != "unclaimed":
                    raise StateError("only unlaunched future work can be blocked")
            item = {
                "id": "blocker-" + hashlib.sha256(args.mutation_id.encode()).hexdigest()[:16],
                "node_id": args.node_id,
                "reason": args.reason,
                "needed": args.needed,
                "status": "active",
                "resolution": None,
                "at": now_iso(),
            }
            state["blockers"].append(item)
            if args.node_id:
                state["nodes"][args.node_id]["status"] = "blocked"
            else:
                state["status"] = "blocked"
            add_event(state, "workflow_blocked", args.reason, args.node_id)
            return item

        state, result, replay = _mutate_command(store, args, command, operation, block)
        return 0, "mutation_reconciled" if replay else "workflow_blocked", {**_public_state(state), "blocker": result}, []

    if command == "unblock":
        operation = {"blocker_id": args.blocker_id, "resolution": args.resolution}

        def unblock(state: dict[str, Any]) -> dict[str, Any]:
            blocker = next((item for item in state["blockers"] if item["id"] == args.blocker_id), None)
            if not blocker or blocker["status"] != "active":
                raise StateError("active blocker not found")
            blocker["status"] = "resolved"
            blocker["resolution"] = args.resolution
            if blocker["node_id"] and state["nodes"][blocker["node_id"]]["status"] == "blocked":
                state["nodes"][blocker["node_id"]]["status"] = "pending"
            if not any(item["status"] == "active" and item["node_id"] is None for item in state["blockers"]):
                if state["status"] == "blocked":
                    state["status"] = "running" if state["nodes"] else "planning"
            return add_event(state, "workflow_unblocked", args.resolution, blocker["node_id"])

        state, result, replay = _mutate_command(store, args, command, operation, unblock)
        return 0, "mutation_reconciled" if replay else "workflow_unblocked", {**_public_state(state), "event": result}, []

    if command == "event":
        operation = {"kind": args.kind, "message": args.message, "node_id": args.node_id}

        def event(state: dict[str, Any]) -> dict[str, Any]:
            if args.node_id and args.node_id not in state["nodes"]:
                raise StateError("event references unknown node")
            return add_event(state, args.kind, args.message, args.node_id)

        state, result, replay = _mutate_command(store, args, command, operation, event)
        return 0, "mutation_reconciled" if replay else "event_recorded", {**_public_state(state), "event": result}, []

    if command == "workflow-complete":
        completion = _read_command_object(
            args.completion_json, args.completion_file, "completion"
        )
        completion = _keys(
            completion,
            {"summary", "validation", "requirements"},
            "completion",
        )
        summary = _text(completion["summary"], "completion.summary", maximum=4096)
        validation = _text(
            completion["validation"],
            "completion.validation",
            maximum=MAX_TEXT - len(summary) - len(FINISH_EVENT_SEPARATOR),
        )
        raw_evidence = completion["requirements"]
        if not isinstance(raw_evidence, dict) or len(raw_evidence) > 256:
            raise StateError("completion.requirements must be a bounded object")
        requirement_evidence = {
            _identifier(requirement_id, "completion requirement id"): _text(
                evidence,
                f"completion.requirements.{requirement_id}",
            )
            for requirement_id, evidence in raw_evidence.items()
        }
        operation = {
            "summary": summary,
            "validation": validation,
            "requirements": requirement_evidence,
        }

        def complete_workflow(state: dict[str, Any]) -> dict[str, Any]:
            active = {
                requirement_id
                for requirement_id, requirement in state["requirements"].items()
                if requirement["status"] == "active"
            }
            supplied = set(requirement_evidence)
            if supplied != active:
                missing = active - supplied
                unknown = supplied - active
                detail = []
                if missing:
                    detail.append("missing " + ", ".join(sorted(missing)))
                if unknown:
                    detail.append("unknown or already resolved " + ", ".join(sorted(unknown)))
                raise StateError(
                    "completion requirement evidence must exactly cover active requirements: "
                    + "; ".join(detail)
                )
            for requirement_id, evidence in requirement_evidence.items():
                state["requirements"][requirement_id]["status"] = "satisfied"
                state["requirements"][requirement_id]["evidence"] = evidence
            return _finish_workflow_state(
                state,
                summary=summary,
                validation=validation,
            )

        state, result, replay = _mutate_command(
            store, args, command, operation, complete_workflow
        )
        return (
            0,
            "mutation_reconciled" if replay else "workflow_completed",
            {**_public_state(state), "event": result},
            [],
        )

    if command == "finish":
        operation = {"summary": args.summary, "validation": args.validation}

        def finish(state: dict[str, Any]) -> dict[str, Any]:
            return _finish_workflow_state(
                state,
                summary=args.summary,
                validation=args.validation,
            )

        state, result, replay = _mutate_command(store, args, command, operation, finish)
        return 0, "mutation_reconciled" if replay else "workflow_completed", {**_public_state(state), "event": result}, []

    if command == "abort":
        def abort(state: dict[str, Any]) -> dict[str, Any]:
            state["status"] = "aborted"
            state["phase"] = "aborted"
            for node in state["nodes"].values():
                if node["status"] not in TERMINAL_NODE_STATUSES:
                    node["status"] = "cancelled"
                    node["result"] = args.reason
                    node["evidence"] = "controller abort"
                    if node["launch"]["state"] in ("claimed", "reconcile_required", "bound", "running"):
                        node["launch"]["state"] = "reconcile_required"
                        node["launch"]["reconciliation"] = "abort requires provider outcome reconciliation"
            _refresh_recovery_status(state)
            return add_event(state, "workflow_aborted", args.reason)

        state, result, replay = _mutate_command(store, args, command, {"reason": args.reason}, abort)
        return 0, "mutation_reconciled" if replay else "workflow_aborted", {**_public_state(state), "event": result}, []
    raise StateError("unsupported command")
