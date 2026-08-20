"""Shared identity contract for public-corpus acquisition rows."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit


DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:a-z0-9]+$", re.IGNORECASE)
REQUIRED_FIELDS = frozenset({"download_id", "study_id", "document_role", "url", "target_path", "source_class"})
DOCUMENT_ROLES = frozenset({"MAIN", "SI"})
EXPECTED_FORMATS = frozenset({"PDF", "DOCX", "XLSX"})
MAX_DOWNLOAD_ID_LENGTH = 128
DOWNLOAD_ID_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_DOWNLOAD_ID_LENGTH - 1}}}$",
    re.ASCII,
)
WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


class ManifestIdentityError(ValueError):
    """An acquisition row violates the shared identity contract."""


def windows_portable_name_key(value: str) -> str:
    """Return the manifest-wide comparison key used by Windows-portable names."""

    return unicodedata.normalize("NFKC", value).casefold().rstrip(" .")


def canonical_acquisition_save_as(download_id: str, expected_format: str) -> str:
    """Validate one portable download token and return its canonical ZIP filename."""

    if not isinstance(download_id, str):
        raise ManifestIdentityError("download_id must be a portable ASCII token")
    token = download_id.strip()
    if not DOWNLOAD_ID_RE.fullmatch(token):
        raise ManifestIdentityError("download_id must be a portable ASCII token")
    if not isinstance(expected_format, str) or expected_format not in EXPECTED_FORMATS:
        raise ManifestIdentityError("expected_format must be PDF, DOCX, or XLSX")
    filename = f"{token}.{expected_format.lower()}"
    device_stem = windows_portable_name_key(filename.split(".", 1)[0])
    if device_stem in WINDOWS_RESERVED_STEMS:
        raise ManifestIdentityError("download_id must not form a Windows reserved filename")
    return filename


def normalize_doi(value: str | None) -> str | None:
    """Return a normalized DOI, rejecting decorated or malformed values."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.lower().startswith("doi:"):
        raw = raw[4:].strip()
    elif "://" in raw:
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"doi.org", "dx.doi.org"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return None
        raw = parsed.path.lstrip("/")
    elif any(marker in raw for marker in ("?", "#", "@")):
        return None
    raw = raw.lower().rstrip(".,; ")
    return raw if DOI_RE.fullmatch(raw) else None


def validate_acquisition_row(record: Any) -> dict[str, Any]:
    """Validate and normalize fields shared by acquisition and audits."""

    if not isinstance(record, dict) or not REQUIRED_FIELDS.issubset(record):
        raise ManifestIdentityError(
            "acquisition rows require download_id, study_id, document_role, "
            "url, target_path, and source_class"
        )
    normalized = dict(record)
    for field in ("download_id", "study_id"):
        value = record[field]
        if not isinstance(value, str) or not value.strip():
            raise ManifestIdentityError(f"{field} must be a nonempty string")
        normalized[field] = value.strip()
    for field in ("url", "target_path", "source_class"):
        value = record[field]
        if not isinstance(value, str) or not value.strip():
            raise ManifestIdentityError(f"{field} must be a nonempty string")
    role = record["document_role"]
    if not isinstance(role, str) or role not in DOCUMENT_ROLES:
        raise ManifestIdentityError("document_role must be MAIN or SI")
    expected_format = record.get("expected_format", "PDF")
    if not isinstance(expected_format, str) or expected_format not in EXPECTED_FORMATS:
        raise ManifestIdentityError("expected_format must be PDF, DOCX, or XLSX")
    normalized["expected_format"] = expected_format
    canonical_acquisition_save_as(normalized["download_id"], expected_format)
    for field in ("doi", "publisher_confirmed_parent_doi"):
        if field in record and record[field] is not None:
            doi = normalize_doi(record[field])
            if doi is None:
                raise ManifestIdentityError(f"{field} must be a valid DOI string or null")
            normalized[field] = doi
    return normalized
