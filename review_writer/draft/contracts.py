"""Small, JSON-friendly contracts for the evidence-aware draft slice.

The draft package intentionally owns metadata only.  It never opens, parses,
creates, or copies a PDF; a PDF node is a source-bound descriptor.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION = "review-writer.evidence-aware-draft.v1"
IMPACT_PREVIEW_SCHEMA = "review-writer.impact-preview.v1"
NODE_KINDS = ("evidence", "claim", "draft", "pdf")
NODE_ID_FIELDS = {
    "evidence": "evidence_id",
    "claim": "claim_id",
    "draft": "draft_id",
    "pdf": "pdf_id",
}
ACCEPTANCE_LAYERS = (
    "engineering",
    "product_use",
    "public_e2e",
    "independent_quality",
    "human_acceptance",
    "scientific_validity",
)
DEFAULT_ACCEPTANCE_LAYERS = {
    layer: "UNVERIFIED" for layer in ACCEPTANCE_LAYERS
}
STALE_STATUSES = frozenset(
    {"STALE", "MISSING", "BLOCKED", "REJECTED", "UNRESOLVED"}
)
PRESERVED_EVIDENCE_STATUSES = frozenset(
    {"AI_PROVISIONAL", "GAP", "NON_COMPARABLE"}
)
_IDENTIFIER = re.compile(r"^(?!\.\.?$)(?!.*[/\\\x00\r\n])\S{1,240}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]+$")


class DraftError(ValueError):
    """Base error with a stable machine-readable code."""

    code = "DRAFT_ERROR"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        inferred = message if isinstance(message, str) and _ERROR_CODE.fullmatch(message) else None
        self.code = code or inferred or type(self).code
        super().__init__(message or self.code)


class DraftValidationError(DraftError):
    code = "DRAFT_INVALID"


class DraftHistoryError(DraftError):
    code = "HISTORICAL_NODE_READ_ONLY"


class DraftBranchError(DraftError):
    code = "BRANCH_INVALID"


class DraftConflictError(DraftError):
    code = "DRAFT_CONFLICT"


@dataclass(frozen=True)
class DownloadArtifact:
    """A deterministic metadata download; content is never a real PDF."""

    node_kind: str
    node_id: str
    filename: str
    media_type: str
    content: bytes


def copy_json(value: object, *, code: str = "DRAFT_JSON_INVALID") -> Any:
    """Return an isolated JSON-compatible copy without normalizing user fields."""

    try:
        copied = copy.deepcopy(value)
        json.dumps(copied, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise DraftValidationError(code) from exc
    return copied


def canonical_digest(value: object) -> str:
    """Compute a deterministic digest for JSON-compatible metadata."""

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DraftValidationError("DRAFT_JSON_INVALID") from exc
    return hashlib.sha256(payload).hexdigest()


def validate_identifier(value: object, *, field: str = "id") -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DraftValidationError(f"{field.upper()}_INVALID")
    return value


def validate_sha256(value: object, *, field: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DraftValidationError(f"{field.upper()}_INVALID")
    return value


def node_identifier(kind: str, record: Mapping[str, Any]) -> str:
    """Read the canonical id while accepting a generic id for adapters."""

    if kind not in NODE_KINDS:
        raise DraftValidationError("NODE_KIND_INVALID")
    field = NODE_ID_FIELDS[kind]
    value = record.get(field)
    if value is None:
        value = record.get("id")
    return validate_identifier(value, field=field)


def status_of(record: Mapping[str, Any]) -> str:
    value = record.get("status")
    if not isinstance(value, str) or not value.strip():
        raise DraftValidationError("STATUS_REQUIRED")
    return value


def status_code(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def is_historical(record: Mapping[str, Any]) -> bool:
    version_state = record.get("version_state")
    if record.get("historical") is False or record.get("is_historical") is False:
        return False
    return bool(
        record.get("historical") is True
        or record.get("is_historical") is True
        or record.get("current") is False
        or (isinstance(version_state, str) and version_state.upper() in {"HISTORICAL", "ARCHIVED"})
    )


def is_current(record: Mapping[str, Any]) -> bool:
    return not is_historical(record) and record.get("current") is not False


def is_writable(record: Mapping[str, Any]) -> bool:
    if is_historical(record) or not is_current(record):
        return False
    return record.get("writable") is not False


def lineage_ids(record: Mapping[str, Any]) -> tuple[str, ...]:
    value = record.get("lineage")
    if isinstance(value, str) and value.strip():
        return (value,)
    if not isinstance(value, Mapping):
        return ()
    found: list[str] = []
    direct = value.get("lineage_id")
    if isinstance(direct, str) and direct.strip():
        found.append(direct)
    ids = value.get("lineage_ids")
    if isinstance(ids, (list, tuple)):
        found.extend(item for item in ids if isinstance(item, str) and item.strip())
    for field in ("lineage_digest", "digest"):
        digest = value.get(field)
        if isinstance(digest, str) and digest.strip():
            found.append(digest)
    return tuple(sorted(set(found)))


def source_roles(record: Mapping[str, Any]) -> tuple[str, ...]:
    roles: list[str] = []
    role = record.get("source_role")
    if isinstance(role, str) and role.strip():
        roles.append(role)
    raw_roles = record.get("source_roles")
    if isinstance(raw_roles, (list, tuple)):
        roles.extend(item for item in raw_roles if isinstance(item, str) and item.strip())
    return tuple(sorted(set(roles)))


def is_divergent(record: Mapping[str, Any]) -> bool:
    lineage = record.get("lineage")
    return bool(
        record.get("divergent_lineage") is True
        or record.get("lineage_divergent") is True
        or (
            isinstance(lineage, Mapping)
            and lineage.get("divergent") is True
        )
    )
