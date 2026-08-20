"""Append-only, account-free credit measurements for one local review project."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas/operations/credit_event.v1.schema.json"
CREDIT_LEDGER_PATH = Path("06_evaluation/credit_ledger.jsonl")
_LOCK_PATH = Path("06_evaluation/.credit_ledger.lock")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CreditLedgerError(ValueError):
    """A stable, fail-closed credit ledger error."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class _LedgerStorage:
    def __init__(self, parent_fd: int, parent_path: Path, lock: Any) -> None:
        self.parent_fd = parent_fd
        self.parent_path = parent_path
        self.lock = lock


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _event_id(payload: dict[str, Any]) -> str:
    material = {key: value for key, value in payload.items() if key != "event_id"}
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CreditLedgerError("CREDIT_SCHEMA_INVALID") from exc
    return Draft202012Validator(schema)


def _validate_event(event: dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(event), key=lambda error: list(error.path))
    if errors or event["consumed"] != event["before"] - event["after"]:
        raise CreditLedgerError("CREDIT_EVENT_INVALID")
    if event["event_id"] != _event_id(event):
        raise CreditLedgerError("CREDIT_EVENT_DIGEST_INVALID")


def _safe_project(project: Path) -> Path:
    supplied = Path(project)
    if supplied.is_symlink() or not supplied.is_dir():
        raise CreditLedgerError("CREDIT_PROJECT_INVALID")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise CreditLedgerError("CREDIT_PROJECT_INVALID") from exc


def _is_reparse(path: Path) -> bool:
    """Reject links and platform reparse points before touching project storage."""
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return False


def _assert_project_path(project: Path, path: Path) -> None:
    current = project
    try:
        relative = path.relative_to(project)
    except ValueError as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
    for part in relative.parts:
        current = current / part
        if _is_reparse(current):
            raise CreditLedgerError("CREDIT_LEDGER_INVALID")
    try:
        path.resolve(strict=False).relative_to(project)
    except (OSError, ValueError) as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc


def _assert_directory_handle(storage: _LedgerStorage) -> None:
    _assert_project_path(storage.parent_path.parent, storage.parent_path)
    try:
        path_stat = storage.parent_path.stat(follow_symlinks=False)
        fd_stat = os.fstat(storage.parent_fd)
    except OSError as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or not stat.S_ISDIR(fd_stat.st_mode)
        or (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino)
    ):
        raise CreditLedgerError("CREDIT_LEDGER_INVALID")


def _ledger_identity(path: Path, *, parent_fd: int | None = None) -> tuple[int, int] | None:
    try:
        if parent_fd is not None and os.stat in getattr(os, "supports_dir_fd", ()):
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID")
    return metadata.st_dev, metadata.st_ino


def _validate_inputs(
    *,
    stage: str,
    before: int,
    after: int,
    source: str,
    study_ids: Sequence[str],
    input_digest: str | None,
    output_digest: str | None,
    forecast: int | float | None,
) -> list[str]:
    if (
        isinstance(before, bool)
        or isinstance(after, bool)
        or not isinstance(before, int)
        or not isinstance(after, int)
        or before < 0
        or after < 0
        or after > before
    ):
        raise CreditLedgerError("CREDIT_MEASUREMENT_INVALID")
    if not isinstance(stage, str) or not _SAFE_NAME.fullmatch(stage):
        raise CreditLedgerError("CREDIT_STAGE_INVALID")
    if not isinstance(source, str) or not _SAFE_NAME.fullmatch(source):
        raise CreditLedgerError("CREDIT_SOURCE_INVALID")
    identifiers = list(study_ids)
    if (
        any(not isinstance(value, str) or not _SAFE_NAME.fullmatch(value) for value in identifiers)
        or len(identifiers) != len(set(identifiers))
    ):
        raise CreditLedgerError("CREDIT_STUDY_IDS_INVALID")
    for digest in (input_digest, output_digest):
        if digest is not None and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            raise CreditLedgerError("CREDIT_DIGEST_INVALID")
    if forecast is not None and (
        isinstance(forecast, bool)
        or not isinstance(forecast, (int, float))
        or not math.isfinite(forecast)
        or forecast < 0
    ):
        raise CreditLedgerError("CREDIT_FORECAST_INVALID")
    return identifiers


@contextmanager
def _ledger_lock(project: Path) -> Iterator[_LedgerStorage]:
    parent_path = project / _LOCK_PATH.parent
    _assert_project_path(project, parent_path)
    parent_path.mkdir(parents=True, exist_ok=True)
    _assert_project_path(project, parent_path)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(parent_path, directory_flags)
    except OSError as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
    storage = _LedgerStorage(parent_fd, parent_path, None)
    lock_file = None
    try:
        _assert_directory_handle(storage)
        lock_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        if os.open in getattr(os, "supports_dir_fd", ()):
            lock_fd = os.open(_LOCK_PATH.name, lock_flags, 0o600, dir_fd=parent_fd)
        else:
            lock_fd = os.open(parent_path / _LOCK_PATH.name, lock_flags, 0o600)
        lock_file = os.fdopen(lock_fd, "a+b")
        storage.lock = lock_file
        if os.fstat(lock_fd).st_nlink != 1:
            raise CreditLedgerError("CREDIT_LEDGER_INVALID")
        _assert_directory_handle(storage)
        if os.name == "nt":
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
                os.fsync(lock_file.fileno())
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield storage
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield storage
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except CreditLedgerError:
        raise
    except OSError as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
    finally:
        if lock_file is not None:
            lock_file.close()
        else:
            os.close(parent_fd)
        if lock_file is not None:
            os.close(parent_fd)


def _read_ledger(
    path: Path,
    *,
    parent_fd: int | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    try:
        if parent_fd is None:
            if not path.exists():
                return []
            if path.is_symlink() or not path.is_file():
                raise CreditLedgerError("CREDIT_LEDGER_INVALID")
            lines = path.read_text(encoding="utf-8").splitlines()
        else:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return []
            with os.fdopen(descriptor, "rb") as handle:
                metadata = os.fstat(handle.fileno())
                identity = (metadata.st_dev, metadata.st_ino)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or expected_identity is None
                    or identity != expected_identity
                ):
                    raise CreditLedgerError("CREDIT_LEDGER_INVALID")
                lines = handle.read().decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            raise CreditLedgerError("CREDIT_LEDGER_INVALID")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
        if not isinstance(row, dict):
            raise CreditLedgerError("CREDIT_LEDGER_INVALID")
        _validate_event(row)
        previous = rows[-1] if rows else None
        if row["previous_event_id"] != (previous["event_id"] if previous else None):
            raise CreditLedgerError("CREDIT_LEDGER_CHAIN_INVALID")
        if previous is not None and row["before"] != previous["after"]:
            raise CreditLedgerError("CREDIT_CONTINUITY_INVALID")
        rows.append(row)
    return rows


def _append_line(
    path: Path,
    event: dict[str, Any],
    *,
    parent_fd: int | None = None,
    expected_identity: tuple[int, int] | None = None,
    post_write_check: Callable[[], None] | None = None,
) -> None:
    if parent_fd is None and (path.is_symlink() or (os.path.lexists(path) and not path.is_file())):
        raise CreditLedgerError("CREDIT_LEDGER_INVALID")
    content = _canonical_bytes(event) + b"\n"
    flags = os.O_WRONLY | os.O_APPEND
    flags |= os.O_CREAT | os.O_EXCL if expected_identity is None else 0
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if parent_fd is None:
            descriptor = os.open(path, flags, 0o600)
        else:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        try:
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (expected_identity is not None and identity != expected_identity)
            ):
                raise CreditLedgerError("CREDIT_LEDGER_INVALID")
            initial_size = metadata.st_size
            try:
                offset = 0
                while offset < len(content):
                    written = os.write(descriptor, content[offset:])
                    if written <= 0:
                        raise OSError("credit ledger append made no progress")
                    offset += written
                os.fsync(descriptor)
                if post_write_check is not None:
                    post_write_check()
            except (CreditLedgerError, OSError) as exc:
                try:
                    os.ftruncate(descriptor, initial_size)
                    os.fsync(descriptor)
                except OSError as rollback_exc:
                    raise CreditLedgerError("CREDIT_LEDGER_ROLLBACK_FAILED") from rollback_exc
                if isinstance(exc, CreditLedgerError):
                    raise
                raise CreditLedgerError("CREDIT_LEDGER_WRITE_FAILED") from exc
        finally:
            os.close(descriptor)
    except CreditLedgerError:
        raise
    except OSError as exc:
        raise CreditLedgerError("CREDIT_LEDGER_WRITE_FAILED") from exc


def record_credit_event(
    project: Path,
    *,
    stage: str = "unspecified",
    before: int,
    after: int,
    source: str,
    study_ids: Sequence[str] = (),
    input_digest: str | None = None,
    output_digest: str | None = None,
    forecast: int | float | None = None,
) -> dict[str, Any]:
    """Append one measured event after validating the existing ledger and continuity."""
    identifiers = _validate_inputs(
        stage=stage,
        before=before,
        after=after,
        source=source,
        study_ids=study_ids,
        input_digest=input_digest,
        output_digest=output_digest,
        forecast=forecast,
    )
    project_path = _safe_project(project)
    ledger_path = project_path / CREDIT_LEDGER_PATH
    _assert_project_path(project_path, ledger_path)
    with _ledger_lock(project_path) as storage:
        _assert_directory_handle(storage)
        _assert_project_path(project_path, ledger_path)
        ledger_identity = _ledger_identity(ledger_path, parent_fd=storage.parent_fd)
        rows = _read_ledger(
            ledger_path,
            parent_fd=storage.parent_fd,
            expected_identity=ledger_identity,
        )
        _assert_directory_handle(storage)
        _assert_project_path(project_path, ledger_path)
        _assert_project_path(project_path, ledger_path)
        if rows and before != rows[-1]["after"]:
            raise CreditLedgerError("CREDIT_CONTINUITY_INVALID")
        event: dict[str, Any] = {
            "schema_version": "credit-event.v1",
            "event_id": "0" * 64,
            "previous_event_id": rows[-1]["event_id"] if rows else None,
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "stage": stage,
            "study_ids": identifiers,
            "input_digest": input_digest,
            "output_digest": output_digest,
            "forecast": forecast,
            "before": before,
            "after": after,
            "consumed": before - after,
            "measurement_source": source,
        }
        event["event_id"] = _event_id(event)
        _validate_event(event)
        _append_line(
            ledger_path,
            event,
            parent_fd=storage.parent_fd,
            expected_identity=ledger_identity,
            post_write_check=lambda: _assert_directory_handle(storage),
        )
        return event


def _validated_ledger_rows(project: Path) -> list[dict[str, Any]]:
    """Read an existing ledger through a directory handle without creating files."""
    project_path = _safe_project(project)
    parent_path = project_path / CREDIT_LEDGER_PATH.parent
    ledger_path = project_path / CREDIT_LEDGER_PATH
    _assert_project_path(project_path, parent_path)
    _assert_project_path(project_path, ledger_path)
    if not os.path.lexists(ledger_path):
        return []
    if _is_reparse(parent_path) or not parent_path.is_dir():
        raise CreditLedgerError("CREDIT_LEDGER_INVALID")
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(parent_path, directory_flags)
    except OSError as exc:
        raise CreditLedgerError("CREDIT_LEDGER_INVALID") from exc
    storage = _LedgerStorage(parent_fd, parent_path, None)
    try:
        _assert_directory_handle(storage)
        identity = _ledger_identity(ledger_path, parent_fd=parent_fd)
        if identity is None:
            return []
        rows = _read_ledger(
            ledger_path,
            parent_fd=parent_fd,
            expected_identity=identity,
        )
        _assert_directory_handle(storage)
        if _ledger_identity(ledger_path, parent_fd=parent_fd) != identity:
            raise CreditLedgerError("CREDIT_LEDGER_INVALID")
        return rows
    finally:
        os.close(parent_fd)


def credit_ledger_summary(project: Path) -> dict[str, Any]:
    """Project validated measurements while making missing operation data explicit."""
    rows = _validated_ledger_rows(project)
    if not rows:
        return {
            "status": "unavailable",
            "continuity": "unavailable",
            "event_count": 0,
            "measured": None,
            "forecast": None,
            "forecast_variance": None,
            "remaining": None,
            "cache": {"status": "unavailable", "hits": None, "misses": None},
            "retries": {"status": "unavailable", "count": None, "events": []},
        }
    first = rows[0]
    last = rows[-1]
    consumed = first["before"] - last["after"]
    forecast = last["forecast"]
    return {
        "status": "available",
        "continuity": "verified",
        "event_count": len(rows),
        "measured": {
            "before": first["before"],
            "after": last["after"],
            "consumed": consumed,
        },
        "forecast": forecast,
        "forecast_variance": consumed - forecast if forecast is not None else None,
        "remaining": last["after"],
        "cache": {"status": "unavailable", "hits": None, "misses": None},
        "retries": {"status": "unavailable", "count": None, "events": []},
    }
