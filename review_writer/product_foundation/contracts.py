"""Stable contracts for the product-foundation version context.

This module deliberately contains no Dashboard, HTTP, receipt, lease, or
promotion concepts.  A version node is an immutable copy of an existing
project-instance snapshot; unknown snapshot fields are retained verbatim so
downstream Evidence and release layers can keep their own provenance.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


VERSION_CONTEXT_SCHEMA = "review-writer.version-context.v1"
CURRENT_POINTER_SCHEMA = "review-writer.current-pointer.v1"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProductFoundationError(ValueError):
    """Base error with a deterministic machine-readable code."""

    code = "PRODUCT_FOUNDATION_ERROR"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(message or self.code)


class InvalidContextError(ProductFoundationError):
    code = "VERSION_CONTEXT_INVALID"


class SnapshotInvalidError(ProductFoundationError):
    code = "SNAPSHOT_INVALID"


class VersionNotFoundError(ProductFoundationError):
    code = "VERSION_NOT_FOUND"


class ReadOnlyVersionError(ProductFoundationError):
    code = "HISTORICAL_VERSION_READ_ONLY"


class NonHeadWriteError(ReadOnlyVersionError):
    """Explicit alias for callers that distinguish non-head from history."""


class StaleRevisionError(ProductFoundationError):
    code = "STALE_REVISION"


class ConfirmationRequiredError(ProductFoundationError):
    code = "CONFIRMATION_REQUIRED"

    def __init__(self, preview: object | None = None) -> None:
        self.preview = preview
        super().__init__(self.code)


class BranchConflictError(ProductFoundationError):
    code = "BRANCH_CONFLICT"


class UndoUnavailableError(ProductFoundationError):
    code = "UNDO_UNAVAILABLE"


class UndoTargetError(ProductFoundationError):
    code = "UNDO_TARGET_INVALID"


class PersistenceError(ProductFoundationError):
    code = "VERSION_CONTEXT_PERSISTENCE_FAILED"


def validate_identifier(value: object, *, field: str = "identifier") -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise InvalidContextError(f"{field} is invalid")
    return value


def copy_snapshot(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotInvalidError("snapshot must be an object")
    try:
        copied = copy.deepcopy(dict(value))
        json.dumps(copied, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise SnapshotInvalidError("snapshot must be JSON-compatible") from exc
    return copied


def copy_value(value: object) -> Any:
    try:
        return copy.deepcopy(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotInvalidError("value cannot be copied") from exc


@dataclass(frozen=True)
class VersionNode:
    """One immutable snapshot and its lineage metadata."""

    version_id: str
    parent_version_id: str | None
    branch_id: str
    branch_name: str
    snapshot: Mapping[str, Any]
    created_at: str
    snapshot_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "parent_version_id": self.parent_version_id,
            "branch_id": self.branch_id,
            "branch_name": self.branch_name,
            "snapshot": copy_snapshot(self.snapshot),
            "created_at": self.created_at,
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True)
class ContextState:
    """The writable project-instance pointer plus a separate inspected node."""

    project_id: str
    current_version_id: str
    active_branch_id: str
    active_head_id: str
    writable_version_id: str
    inspected_version_id: str | None
    revision: int
    branch_heads: Mapping[str, str]

    @property
    def current_instance_version_id(self) -> str:
        return self.current_version_id

    @property
    def inspected_historical_version_id(self) -> str | None:
        return self.inspected_version_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "current_version_id": self.current_version_id,
            "active_branch_id": self.active_branch_id,
            "active_head_id": self.active_head_id,
            "writable_version_id": self.writable_version_id,
            "inspected_version_id": self.inspected_version_id,
            "revision": self.revision,
            "branch_heads": dict(sorted(self.branch_heads.items())),
        }


@dataclass(frozen=True)
class VersionView:
    """A read projection; callers never receive the service's live snapshot."""

    version_id: str
    parent_version_id: str | None
    branch_id: str
    branch_name: str
    snapshot: Mapping[str, Any]
    snapshot_digest: str
    read_only: bool
    can_write: bool
    is_current: bool
    is_active_head: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "parent_version_id": self.parent_version_id,
            "branch_id": self.branch_id,
            "branch_name": self.branch_name,
            "snapshot": copy_snapshot(self.snapshot),
            "snapshot_digest": self.snapshot_digest,
            "read_only": self.read_only,
            "can_write": self.can_write,
            "is_current": self.is_current,
            "is_active_head": self.is_active_head,
        }


@dataclass(frozen=True)
class VersionComparison:
    left_version_id: str
    right_version_id: str
    changed_fields: tuple[str, ...]
    changes: Mapping[str, Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_version_id": self.left_version_id,
            "right_version_id": self.right_version_id,
            "changed_fields": list(self.changed_fields),
            "changes": copy_value(self.changes),
        }


@dataclass(frozen=True)
class DownloadArtifact:
    version_id: str
    filename: str
    media_type: str
    content: bytes
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class BranchPreview:
    source_version_id: str
    new_branch_id: str
    branch_name: str
    new_version_id: str
    current_version_id: str
    active_branch_id: str
    activates_branch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version_id": self.source_version_id,
            "new_branch_id": self.new_branch_id,
            "branch_name": self.branch_name,
            "new_version_id": self.new_version_id,
            "current_version_id": self.current_version_id,
            "active_branch_id": self.active_branch_id,
            "activates_branch": self.activates_branch,
        }


@dataclass(frozen=True)
class UndoPreview:
    current_version_id: str
    target_version_id: str
    discarded_version_ids: tuple[str, ...]
    active_branch_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version_id": self.current_version_id,
            "target_version_id": self.target_version_id,
            "discarded_version_ids": list(self.discarded_version_ids),
            "active_branch_id": self.active_branch_id,
        }
