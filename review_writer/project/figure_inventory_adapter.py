"""Clean-room projection rules adapted from ChemVellum's figure inventory.

This module contains only deterministic candidate-enrichment semantics.  It
does not own a registry, a manuscript pointer, rights approval, or release
state.  Those authorities remain in :mod:`review_figures` and the delivery
pipeline.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LICENSE_URL_RE = re.compile(
    r"https?://creativecommons\.org/licenses/[A-Za-z0-9_-]+(?:/[0-9.]+)?/?",
    re.IGNORECASE,
)
_LICENSE_LINE_RE = re.compile(
    r"(?:creative commons|\bcc[- ]by\b|open access article|licensed under|copyright|©)",
    re.IGNORECASE,
)
_OPEN_REUSE_RE = re.compile(
    r"(?:creative commons|\bcc[- ]by(?:[- ]nc|[- ]sa|[- ]nd)?\b|public domain)",
    re.IGNORECASE,
)
_RESTRICTED_REUSE_RE = re.compile(r"all rights reserved", re.IGNORECASE)


class FigureInventoryAdapterError(ValueError):
    """A candidate-enrichment input is invalid or source-bound data drifted."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def clean_inventory_text(value: Any) -> str:
    """Collapse whitespace using the upstream inventory's text semantics."""
    if isinstance(value, list):
        value = " ".join(str(item) for item in value if str(item).strip())
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_inventory_label(value: Any) -> str:
    """Return a deterministic, human-readable label without changing its meaning."""
    return clean_inventory_text(value)


def _pending_rights_hints() -> dict[str, Any]:
    return {
        "rights_review_status": "pending",
        "reuse_hint_class": "unknown",
        "license_statement_candidates": [],
        "license_urls": [],
    }


def _safe_source_text_path(project: Path, descriptor: Mapping[str, Any]) -> tuple[Path, str]:
    relative = descriptor.get("path")
    expected_sha256 = descriptor.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative.strip()
        or relative.startswith("/")
        or "\\" in relative
        or ".." in Path(relative).parts
        or not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise FigureInventoryAdapterError("SOURCE_MARKDOWN_INVALID")
    candidate = project / relative.strip()
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise FigureInventoryAdapterError("SOURCE_MARKDOWN_INVALID") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise FigureInventoryAdapterError("SOURCE_MARKDOWN_INVALID")
    return resolved, expected_sha256


def license_hints(project: Path, descriptor: object) -> dict[str, Any]:
    """Read source-bound Markdown for rights hints, never for rights approval."""
    if not isinstance(descriptor, Mapping):
        return _pending_rights_hints()
    path, expected_sha256 = _safe_source_text_path(project, descriptor)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise FigureInventoryAdapterError("SOURCE_MARKDOWN_HASH_MISMATCH")
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeError) as exc:
        raise FigureInventoryAdapterError("SOURCE_MARKDOWN_INVALID") from exc
    statements: list[str] = []
    for line in text.splitlines():
        cleaned = clean_inventory_text(line)
        if cleaned and _LICENSE_LINE_RE.search(cleaned):
            statements.append(cleaned[:600])
        if len(statements) >= 6:
            break
    urls = list(dict.fromkeys(_LICENSE_URL_RE.findall(text)))
    joined = "\n".join([*statements, *urls])
    if _OPEN_REUSE_RE.search(joined):
        hint_class = "open_reuse_candidate"
    elif _RESTRICTED_REUSE_RE.search(joined):
        hint_class = "restricted"
    else:
        hint_class = "unknown"
    return {
        "rights_review_status": "license_hint_found" if statements or urls else "pending",
        "reuse_hint_class": hint_class,
        "license_statement_candidates": list(dict.fromkeys(statements)),
        "license_urls": urls[:6],
        "instructions": (
            "These are discovery hints only. Verify the article license, the selected figure's "
            "credit line, any third-party exclusion, and whether adaptation is permitted."
        ),
    }


def source_order_key(row: Mapping[str, Any]) -> tuple[int, int, str]:
    """Sort candidates by page and first inventory block, then stable id."""
    page = row.get("page") if isinstance(row.get("page"), int) else 0
    fragments = row.get("fragments")
    block_index = 0
    if isinstance(fragments, list) and fragments and isinstance(fragments[0], Mapping):
        value = fragments[0].get("block_index")
        if isinstance(value, int) and not isinstance(value, bool):
            block_index = value
    return page, block_index, clean_inventory_text(row.get("figure_id"))


def normalized_fragment(raw: object) -> dict[str, Any] | None:
    """Normalize an already grouped fragment while retaining its provenance."""
    if not isinstance(raw, Mapping):
        return None
    result = dict(raw)
    for field in ("asset_path", "caption_association"):
        if field in result and isinstance(result[field], str):
            result[field] = clean_inventory_text(result[field])
    return result


def normalized_locator(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    result = dict(raw)
    for field in ("source_mode", "section_or_item", "figure_or_table", "exact_quote"):
        if field in result:
            result[field] = clean_inventory_text(result[field])
    return result
