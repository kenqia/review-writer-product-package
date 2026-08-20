"""Source-grounded figure registry and human synthesis-figure briefs.

This module deliberately has no image generation or image composition path. Source
figures are references to the bytes extracted from a verified Source Truth bundle;
cross-study figures remain researcher-owned placeholders.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import heapq
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from review_writer.project.source_truth import (
    REPO_ROOT,
    SourceTruthError,
    canonical_digest,
    declared_study_ids,
    load_source_truth_bundle,
)
from review_writer.project.chemical_paper import (
    STATE_NAME as CHEMICAL_PAPER_STATE_NAME,
    STATE_ROOT as CHEMICAL_PAPER_STATE_ROOT,
    ChemicalPaperError,
    load_chemical_paper_state,
)


SOURCE_FIGURE_SCHEMA = REPO_ROOT / "schemas/figures/source_figure.v1.schema.json"
PLACEHOLDER_SCHEMA = REPO_ROOT / "schemas/figures/synthesis_figure_placeholder.v1.schema.json"
FIGURE_ROOT = Path("03_figures")
REGISTRY_PATH = FIGURE_ROOT / "source_figure_registry.json"
PLACEHOLDER_PATH = FIGURE_ROOT / "synthesis_figure_placeholders.json"
FIGURE_POLICY = "source_figures_or_synthesis_placeholders_only"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReviewFigureError(ValueError):
    """A stable, fail-closed figure registry failure."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _fail(code: str, message: str = "") -> None:
    raise ReviewFigureError(code, message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReviewFigureError("FIGURE_ASSET_INVALID") from exc
    return digest.hexdigest()


def _safe_project(project: Path) -> Path:
    supplied = Path(project)
    if supplied.is_symlink() or not supplied.is_dir():
        _fail("PROJECT_INVALID")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise ReviewFigureError("PROJECT_INVALID") from exc


def _safe_asset(project: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or relative.startswith("/") or "\\" in relative:
        _fail("FIGURE_ASSET_INVALID")
    candidate = project / relative
    try:
        resolved = candidate.resolve(strict=True)
        root = project.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReviewFigureError("FIGURE_ASSET_INVALID") from exc
    if candidate.is_symlink() or not candidate.is_file():
        _fail("FIGURE_ASSET_INVALID")
    return resolved


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewFigureError(code) from exc


def _validate(payload: object, schema_path: Path, code: str) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewFigureError("FIGURE_SCHEMA_INVALID") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        raise ReviewFigureError(code)


_TARGET_BINDING_MISSING = object()
_TARGET_MARKER_RE = re.compile(
    r"\[(?P<bracket_kind>source|evidence):(?P<bracket_id>[A-Za-z0-9._:-]+)\]"
    r"|<!--\s*(?P<comment_kind>source|evidence)\s*:\s*"
    r"(?P<comment_id>[A-Za-z0-9._:-]+)\s*-->",
    flags=re.IGNORECASE,
)
_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _target_section_id(heading: str, seen: dict[str, int]) -> str:
    base = re.sub(r"[^\w]+", "-", heading.casefold(), flags=re.UNICODE).strip("-_")
    base = base or "section"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}-{seen[base]}"


def _target_markers(body: str) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    fence: str | None = None
    counts: dict[str, int] = {}
    for line in body.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            continue
        if fence is not None:
            continue
        for match in _TARGET_MARKER_RE.finditer(line):
            marker = match.group(0)
            occurrence = counts.get(marker, 0) + 1
            counts[marker] = occurrence
            markers.append(
                {
                    "marker": marker,
                    "kind": (match.group("bracket_kind") or match.group("comment_kind")).casefold(),
                    "marker_id": match.group("bracket_id") or match.group("comment_id"),
                    "occurrence": occurrence,
                }
            )
    return markers


def _target_sections(markdown: str) -> list[dict[str, Any]]:
    if not isinstance(markdown, str):
        _fail("FIGURE_TARGET_BINDING_INVALID")
    matches: list[tuple[int, int, str]] = []
    offset = 0
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        fence_match = _FENCE_RE.match(line.rstrip("\r\n"))
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
        elif fence is None:
            heading_match = _ATX_HEADING_RE.match(line.rstrip("\r\n"))
            if heading_match:
                matches.append(
                    (
                        offset,
                        offset + len(line),
                        heading_match.group(2).strip(),
                    )
                )
        offset += len(line)
    if not matches or markdown[: matches[0][0]].strip():
        _fail("FIGURE_TARGET_BINDING_INVALID")
    seen: dict[str, int] = {}
    sections: list[dict[str, Any]] = []
    for index, (_, heading_end, heading) in enumerate(matches):
        body_end = matches[index + 1][0] if index + 1 < len(matches) else len(markdown)
        body = markdown[heading_end:body_end].strip("\r\n")
        sections.append(
            {
                "section_id": _target_section_id(heading, seen),
                "heading": heading,
                "markers": _target_markers(body),
            }
        )
    return sections


def current_manuscript_target_projection(project: Path) -> dict[str, Any]:
    """Return the current manuscript digest and explicit source/evidence targets."""
    root = _safe_project(project)
    manuscript_path = root / "04_manuscript/manuscript.md"
    if manuscript_path.is_symlink() or not manuscript_path.is_file():
        return {"sha256": "", "sections": []}
    try:
        manuscript_bytes = manuscript_path.read_bytes()
        markdown = manuscript_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewFigureError("FIGURE_TARGET_BINDING_INVALID") from exc
    return {
        "sha256": hashlib.sha256(manuscript_bytes).hexdigest(),
        "sections": _target_sections(markdown),
    }


def _target_binding_allowed_marker(row: Mapping[str, Any], marker: Mapping[str, Any]) -> bool:
    marker_kind = marker.get("kind")
    marker_id = marker.get("marker_id")
    if marker_kind == "source":
        return marker_id == row.get("source_id")
    evidence_ids = row.get("evidence_ids")
    return marker_kind == "evidence" and isinstance(evidence_ids, list) and marker_id in evidence_ids


def source_figure_target_options(
    row: Mapping[str, Any], sections: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Project only existing source/evidence markers eligible for one source figure."""
    options: list[dict[str, Any]] = []
    for section in sections:
        section_id = section.get("section_id")
        heading = section.get("heading")
        markers = section.get("markers")
        if not isinstance(section_id, str) or not isinstance(markers, list):
            continue
        for marker in markers:
            if not isinstance(marker, Mapping) or not _target_binding_allowed_marker(row, marker):
                continue
            options.append(
                {
                    "section_id": section_id,
                    "heading": heading,
                    "marker": marker["marker"],
                    "kind": marker["kind"],
                    "marker_id": marker["marker_id"],
                    "occurrence": marker["occurrence"],
                }
            )
    return options


def validate_source_figure_target_binding(
    row: Mapping[str, Any],
    binding: object,
    manuscript_markdown: str,
    *,
    current_asset_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate one explicit, current section/marker binding without writing state."""
    if not isinstance(row, Mapping) or not isinstance(binding, Mapping):
        _fail("FIGURE_TARGET_BINDING_INVALID")
    required = {
        "figure_id",
        "asset_sha256",
        "manuscript_sha256",
        "section_id",
        "marker",
        "occurrence",
    }
    if set(binding) != required:
        _fail("FIGURE_TARGET_BINDING_INVALID")
    figure_id = binding.get("figure_id")
    asset_sha256 = binding.get("asset_sha256")
    manuscript_sha256 = binding.get("manuscript_sha256")
    section_id = binding.get("section_id")
    marker = binding.get("marker")
    occurrence = binding.get("occurrence")
    if (
        not isinstance(figure_id, str)
        or not figure_id
        or figure_id != row.get("figure_id")
        or not isinstance(asset_sha256, str)
        or _SHA256.fullmatch(asset_sha256) is None
        or asset_sha256 != row.get("asset_sha256")
        or not isinstance(manuscript_sha256, str)
        or _SHA256.fullmatch(manuscript_sha256) is None
        or not isinstance(section_id, str)
        or not section_id
        or not isinstance(marker, str)
        or not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 1
    ):
        _fail("FIGURE_TARGET_BINDING_INVALID")
    current_sha256 = hashlib.sha256(manuscript_markdown.encode("utf-8")).hexdigest()
    if manuscript_sha256 != current_sha256:
        _fail("FIGURE_TARGET_BINDING_STALE")
    if current_asset_sha256 is not None and current_asset_sha256 != asset_sha256:
        _fail("FIGURE_TARGET_BINDING_STALE")
    sections = _target_sections(manuscript_markdown)
    matches = [section for section in sections if section.get("section_id") == section_id]
    if len(matches) != 1:
        _fail("FIGURE_TARGET_BINDING_INVALID")
    eligible = [
        target
        for target in source_figure_target_options(row, matches)
        if target.get("marker") == marker and target.get("occurrence") == occurrence
    ]
    if len(eligible) != 1:
        _fail("FIGURE_TARGET_BINDING_INVALID")
    return {
        "figure_id": figure_id,
        "asset_sha256": asset_sha256,
        "manuscript_sha256": manuscript_sha256,
        "section_id": section_id,
        "marker": marker,
        "occurrence": occurrence,
    }


def source_figure_target_binding_status(
    row: Mapping[str, Any],
    manuscript_markdown: str,
    *,
    current_asset_sha256: str | None = None,
) -> str:
    binding = row.get("target_binding") if isinstance(row, Mapping) else None
    if binding is None:
        return "missing"
    try:
        validate_source_figure_target_binding(
            row,
            binding,
            manuscript_markdown,
            current_asset_sha256=current_asset_sha256,
        )
    except ReviewFigureError:
        return "stale"
    return "current"


def current_source_figure_asset_sha256(
    project: Path, row: Mapping[str, Any]
) -> str:
    root = _safe_project(project)
    asset_path = row.get("asset_path") if isinstance(row, Mapping) else None
    if not isinstance(asset_path, str):
        _fail("FIGURE_ASSET_INVALID")
    return _sha256(_safe_asset(root, asset_path))


def source_figure_workspace_revision(
    registry: Mapping[str, Any], row: Mapping[str, Any], manuscript_sha256: str
) -> dict[str, Any]:
    """Return the existing review-figures optimistic-concurrency material."""
    return {
        "figure_id": row.get("figure_id"),
        "asset_sha256": row.get("asset_sha256"),
        "selection_status": row.get("selection_status"),
        "target_binding": copy.deepcopy(row.get("target_binding")),
        "registry_digest": registry.get("registry_digest"),
        "source_truth_digest": registry.get("source_truth_digest"),
        "content_list_v2_digest": registry.get("content_list_v2_digest"),
        "chemical_paper_project_binding_digest": registry.get(
            "chemical_paper_project_binding_digest"
        ),
        "manuscript_sha256": manuscript_sha256,
    }


def source_figure_workspace_token(figure_id: str, revision: Mapping[str, Any]) -> str:
    """Use the same opaque token shape as the Dashboard workspace tokens."""
    material = json.dumps(
        {"kind": "review-figures", "id": figure_id, "value": revision},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(material).digest()).decode("ascii").rstrip("=")


def source_figure_registry_digest(registry: Mapping[str, Any]) -> str:
    """Digest source-figure identity while keeping target binding additive."""
    figures = registry.get("figures", [])
    normalized_figures: list[dict[str, Any]] = []
    if isinstance(figures, list):
        for row in figures:
            if isinstance(row, Mapping):
                normalized = copy.deepcopy(dict(row))
                normalized.pop("target_binding", None)
                normalized_figures.append(normalized)
            else:
                normalized_figures.append(row)
    return canonical_digest(
        {
            "source_truth_digest": registry.get("source_truth_digest"),
            "content_list_v2_digest": registry.get("content_list_v2_digest"),
            "chemical_paper_project_binding_digest": registry.get(
                "chemical_paper_project_binding_digest"
            ),
            "figures": normalized_figures,
            "locator_gaps": registry.get("locator_gaps", []),
        }
    )


def _figure_budget_projection(registry: Mapping[str, Any]) -> dict[str, Any]:
    figures = registry.get("figures")
    rows = figures if isinstance(figures, list) else []
    selected_count = sum(
        isinstance(row, Mapping) and row.get("selection_status") == "selected"
        for row in rows
    )
    available_count = len(rows)
    slots = registry.get("target_figure_slots")
    minimum = slots.get("minimum") if isinstance(slots, Mapping) else None
    maximum = slots.get("maximum") if isinstance(slots, Mapping) else None
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        minimum = 5
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < minimum:
        maximum = 8
    required_count = max(minimum - selected_count, 0)
    if selected_count < minimum:
        status = "needs_human_selection"
        gaps = [
            f"Select {required_count} additional non-duplicative source figure(s) or register a synthesis placeholder."
        ]
    elif selected_count > maximum:
        status = "over_budget"
        gaps = [f"Reduce selected figures by {selected_count - maximum} slot(s)."]
    else:
        status = "within_target"
        gaps = []
    figure_budget = {
        "status": status,
        "selected_count": selected_count,
        "required_count": required_count,
        "minimum": minimum,
        "maximum": maximum,
        "gaps": gaps,
    }
    return {
        "selected_count": selected_count,
        "available_count": available_count,
        "required_count": required_count,
        "figure_budget": figure_budget,
    }


def write_source_figure_selection(
    project: Path,
    *,
    figure_id: object,
    selection_status: object,
    version_token: object,
    target_binding: object = _TARGET_BINDING_MISSING,
) -> dict[str, Any]:
    """Atomically persist selection plus an optional explicit target binding."""
    root = _safe_project(project)
    if not isinstance(figure_id, str) or not figure_id:
        _fail("FIGURE_NOT_FOUND")
    if not isinstance(selection_status, str) or selection_status not in {
        "selected",
        "available",
        "rejected",
    }:
        _fail("FIGURE_SELECTION_INVALID")
    if not isinstance(version_token, str) or not version_token:
        _fail("FIGURE_TARGET_BINDING_STALE")
    registry = load_source_figure_registry(root)
    rows = registry.get("figures")
    matches = [
        row for row in rows if isinstance(row, dict) and row.get("figure_id") == figure_id
    ] if isinstance(rows, list) else []
    if len(matches) != 1:
        _fail("FIGURE_NOT_FOUND")
    row = matches[0]
    manuscript_path = root / "04_manuscript/manuscript.md"
    try:
        manuscript_bytes = manuscript_path.read_bytes()
        manuscript_markdown = manuscript_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewFigureError("FIGURE_TARGET_BINDING_INVALID") from exc
    manuscript_sha256 = hashlib.sha256(manuscript_bytes).hexdigest()
    revision = source_figure_workspace_revision(registry, row, manuscript_sha256)
    expected_token = source_figure_workspace_token(figure_id, revision)
    if version_token != expected_token:
        _fail("FIGURE_TARGET_BINDING_STALE")
    if target_binding is not _TARGET_BINDING_MISSING:
        if selection_status != "selected":
            _fail("FIGURE_TARGET_BINDING_INVALID")
        try:
            current_asset_sha256 = current_source_figure_asset_sha256(root, row)
        except ReviewFigureError as exc:
            raise ReviewFigureError("FIGURE_TARGET_BINDING_STALE") from exc
        row["target_binding"] = validate_source_figure_target_binding(
            row,
            target_binding,
            manuscript_markdown,
            current_asset_sha256=current_asset_sha256,
        )
    row["selection_status"] = selection_status
    budget = _figure_budget_projection(registry)
    registry["selected_count"] = budget["selected_count"]
    registry["available_count"] = budget["available_count"]
    registry["required_count"] = budget["required_count"]
    registry["figure_budget"] = budget["figure_budget"]
    registry["registry_digest"] = source_figure_registry_digest(registry)
    _atomic_json(root, REGISTRY_PATH, registry)
    return load_source_figure_registry(root)


def _atomic_json(project: Path, path: Path, payload: object) -> None:
    target = project / path
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or (os.path.lexists(target) and not target.is_file()):
        _fail("FIGURE_STATE_INVALID")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise ReviewFigureError("FIGURE_STATE_WRITE_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _content_entries(project: Path, source: dict[str, Any]) -> list[dict[str, Any]]:
    descriptor = source.get("content_list")
    if not isinstance(descriptor, dict):
        _fail("FIGURE_CONTENT_LIST_INVALID")
    relative = descriptor.get("path")
    expected_sha256 = descriptor.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        _fail("FIGURE_CONTENT_LIST_INVALID")
    content_path = _safe_asset(project, relative)
    if _sha256(content_path) != expected_sha256:
        _fail("FIGURE_CONTENT_LIST_DRIFT")
    payload = _read_json(content_path, "FIGURE_CONTENT_LIST_INVALID")
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        _fail("FIGURE_CONTENT_LIST_INVALID")
    return payload


def _verify_source_images(project: Path, source: dict[str, Any]) -> None:
    slug = source.get("mineru_slug")
    if not isinstance(slug, str):
        _fail("FIGURE_SOURCE_INVALID")
    image_root = project / "01_evidence/parses/extracted" / slug / "images"
    rows: list[dict[str, Any]] = []
    if image_root.is_dir() and not image_root.is_symlink():
        for path in sorted(image_root.rglob("*")):
            if path.is_symlink():
                _fail("FIGURE_ASSET_INVALID")
            if path.is_file():
                relative = path.relative_to(project).as_posix()
                rows.append({"path": relative, "sha256": _sha256(path), "size_bytes": path.stat().st_size})
    expected = source.get("images")
    if not isinstance(expected, dict) or expected.get("count") != len(rows) or expected.get("digest") != canonical_digest(rows):
        _fail("FIGURE_IMAGE_SET_DRIFT")


_SOURCE_FIGURE_LABEL = re.compile(
    r"(?:Figure|Fig\.?|Scheme|Chart)\s*[A-Za-z]?\s*\d+",
    re.I,
)
_V2_LAYOUT_GAP = 80.0


def _content_v2_pages(project: Path, source: dict[str, Any]) -> list[list[dict[str, Any]]]:
    descriptor = source.get("content_list_v2")
    if not isinstance(descriptor, dict):
        _fail("FIGURE_CONTENT_LIST_V2_INVALID")
    relative = descriptor.get("path")
    expected_sha256 = descriptor.get("sha256")
    if (
        not isinstance(relative, str)
        or not isinstance(expected_sha256, str)
        or not _SHA256.fullmatch(expected_sha256)
    ):
        _fail("FIGURE_CONTENT_LIST_V2_INVALID")
    path = _safe_asset(project, relative)
    if _sha256(path) != expected_sha256:
        _fail("FIGURE_CONTENT_LIST_V2_DRIFT")
    payload = _read_json(path, "FIGURE_CONTENT_LIST_V2_INVALID")
    page_count = source.get("page_count")
    if (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or not isinstance(payload, list)
        or len(payload) != page_count
        or not all(
            isinstance(page, list) and all(isinstance(row, dict) for row in page)
            for page in payload
        )
    ):
        _fail("FIGURE_CONTENT_LIST_V2_INVALID")
    return payload


def _content_list_v2_digest(root: Path) -> str:
    bindings: list[dict[str, str]] = []
    try:
        studies = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ReviewFigureError(exc.code) from exc
    for study_id in studies:
        try:
            bundle = load_source_truth_bundle(root, study_id)
        except SourceTruthError as exc:
            raise ReviewFigureError(exc.code) from exc
        for source in bundle.get("sources", []):
            if not isinstance(source, dict) or source.get("document_role") != "MAIN":
                continue
            _content_v2_pages(root, source)
            descriptor = source["content_list_v2"]
            bindings.append(
                {
                    "study_id": study_id,
                    "source_id": source["source_id"],
                    "sha256": descriptor["sha256"],
                }
            )
    return canonical_digest(bindings)


def _caption_text(raw: object) -> str | None:
    if not isinstance(raw, list):
        return None
    parts: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            return None
        clean = " ".join(item["content"].split())
        if clean:
            parts.append(clean)
    return " ".join(parts)


def _valid_bbox(value: object) -> list[int | float] | None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in value
        )
    ):
        return None
    x0, y0, x1, y1 = value
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        return None
    return list(value)


def _v2_image_blocks(
    root: Path,
    *,
    study_id: str,
    source_id: str,
    source: dict[str, Any],
    extracted: Path,
    image_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocks: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for page_index, page_rows in enumerate(_content_v2_pages(root, source)):
        page = page_index + 1
        for block_index, entry in enumerate(page_rows):
            if entry.get("type") != "image":
                continue
            bbox = _valid_bbox(entry.get("bbox"))
            content = entry.get("content")
            image_source = content.get("image_source") if isinstance(content, dict) else None
            image_path = image_source.get("path") if isinstance(image_source, dict) else None
            caption = _caption_text(
                content.get("image_caption") if isinstance(content, dict) else None
            )
            if bbox is None or not isinstance(image_path, str) or caption is None:
                gaps.append(
                    {
                        "study_id": study_id,
                        "source_id": source_id,
                        "page": page,
                        "reason": "content_list_v2 图块缺少完整 bbox、图片来源或图注关系，已拒绝定位。",
                    }
                )
                continue
            raw_path = Path(image_path)
            if raw_path.is_absolute() or ".." in raw_path.parts:
                _fail("FIGURE_ASSET_INVALID")
            asset = _safe_asset(
                root,
                (extracted / raw_path).relative_to(root).as_posix(),
            )
            try:
                asset.relative_to(image_root)
            except ValueError as exc:
                raise ReviewFigureError("FIGURE_ASSET_INVALID") from exc
            labels = [match.group(0) for match in _SOURCE_FIGURE_LABEL.finditer(caption)]
            blocks.append(
                {
                    "page": page,
                    "block_index": block_index,
                    "bbox": bbox,
                    "asset_path": asset.relative_to(root).as_posix(),
                    "asset_sha256": _sha256(asset),
                    "caption": caption,
                    "labels": labels,
                }
            )
    return blocks, gaps


def _spatially_related(left: dict[str, Any], right: dict[str, Any]) -> bool:
    lx0, ly0, lx1, ly1 = left["bbox"]
    rx0, ry0, rx1, ry1 = right["bbox"]
    overlap_x = min(lx1, rx1) - max(lx0, rx0)
    overlap_y = min(ly1, ry1) - max(ly0, ry0)
    horizontal_gap = max(rx0 - lx1, lx0 - rx1, 0)
    vertical_gap = max(ry0 - ly1, ly0 - ry1, 0)
    return (
        overlap_x > 0 and vertical_gap <= _V2_LAYOUT_GAP
    ) or (
        overlap_y > 0 and horizontal_gap <= _V2_LAYOUT_GAP
    )


def _spatial_cost(left: dict[str, Any], right: dict[str, Any]) -> float:
    if not _spatially_related(left, right):
        return math.inf
    lx0, ly0, lx1, ly1 = left["bbox"]
    rx0, ry0, rx1, ry1 = right["bbox"]
    overlap_x = max(0.0, min(lx1, rx1) - max(lx0, rx0))
    overlap_y = max(0.0, min(ly1, ry1) - max(ly0, ry0))
    horizontal_gap = max(rx0 - lx1, lx0 - rx1, 0)
    vertical_gap = max(ry0 - ly1, ly0 - ry1, 0)
    costs: list[float] = []
    if overlap_x > 0:
        overlap_ratio = overlap_x / min(lx1 - lx0, rx1 - rx0)
        costs.append(vertical_gap / overlap_ratio)
    if overlap_y > 0:
        overlap_ratio = overlap_y / min(ly1 - ly0, ry1 - ry0)
        costs.append(horizontal_gap / overlap_ratio)
    return min(costs, default=math.inf)


def _anchor_distances(
    blocks: list[dict[str, Any]],
    anchor_index: int,
) -> list[float]:
    distances = [math.inf] * len(blocks)
    distances[anchor_index] = 0.0
    queue: list[tuple[float, int]] = [(0.0, anchor_index)]
    while queue:
        distance, current = heapq.heappop(queue)
        if distance != distances[current]:
            continue
        for candidate in range(len(blocks)):
            if candidate == current or (
                candidate != anchor_index and blocks[candidate]["labels"]
            ):
                continue
            edge = _spatial_cost(blocks[current], blocks[candidate])
            proposed = distance + edge
            if proposed < distances[candidate]:
                distances[candidate] = proposed
                heapq.heappush(queue, (proposed, candidate))
    return distances


def _spatial_components(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    remaining = set(range(len(blocks)))
    components: list[list[dict[str, Any]]] = []
    while remaining:
        pending = [remaining.pop()]
        indexes: list[int] = []
        while pending:
            current = pending.pop()
            indexes.append(current)
            neighbours = {
                candidate
                for candidate in remaining
                if not (blocks[current]["labels"] and blocks[candidate]["labels"])
                and _spatially_related(blocks[current], blocks[candidate])
            }
            remaining.difference_update(neighbours)
            pending.extend(neighbours)
        components.append(
            sorted(
                (blocks[index] for index in indexes),
                key=lambda row: (row["bbox"][1], row["bbox"][0], row["block_index"]),
            )
        )
    return sorted(
        components,
        key=lambda rows: (rows[0]["page"], rows[0]["bbox"][1], rows[0]["bbox"][0]),
    )


def _normalized_label(label: str) -> str:
    return re.sub(r"[.\s]+", "", label.casefold())


def _source_truth_digest(root: Path) -> str:
    bindings: list[dict[str, str]] = []
    try:
        study_ids = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ReviewFigureError(exc.code) from exc
    for study_id in study_ids:
        try:
            bundle = load_source_truth_bundle(root, study_id)
        except SourceTruthError as exc:
            raise ReviewFigureError(exc.code) from exc
        bundle_digest = bundle.get("bundle_digest")
        if not isinstance(bundle_digest, str) or not _SHA256.fullmatch(bundle_digest):
            _fail("FIGURE_SOURCE_INVALID")
        bindings.append({"study_id": study_id, "bundle_digest": bundle_digest})
    return canonical_digest(bindings)


def _chemical_paper_bindings(root: Path) -> tuple[str | None, dict[str, str]]:
    state_root = root / CHEMICAL_PAPER_STATE_ROOT
    if not state_root.exists():
        return None, {}
    try:
        studies = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ReviewFigureError(exc.code) from exc
    rows: list[dict[str, str]] = []
    by_study: dict[str, str] = {}
    for study_id in studies:
        path = state_root / study_id / CHEMICAL_PAPER_STATE_NAME
        if not path.exists():
            continue
        try:
            state = load_chemical_paper_state(root, study_id)
        except ChemicalPaperError as exc:
            raise ReviewFigureError(exc.code) from exc
        digest = state["current_import_digest"]
        rows.append({"study_id": study_id, "chemical_paper_import_digest": digest})
        by_study[study_id] = digest
    return canonical_digest(rows), by_study


def load_source_figure_registry(project: Path) -> dict[str, Any]:
    """Load a registry only when it is bound to the current Source Truth."""
    root = _safe_project(project)
    path = root / REGISTRY_PATH
    if path.is_symlink() or not path.is_file():
        _fail("FIGURE_STATE_INVALID")
    payload = _read_json(path, "FIGURE_STATE_INVALID")
    if not isinstance(payload, dict):
        _fail("FIGURE_STATE_INVALID")
    figures = payload.get("figures")
    locator_gaps = payload.get("locator_gaps")
    try:
        content_list_v2_digest = _content_list_v2_digest(root)
    except ReviewFigureError as exc:
        raise ReviewFigureError("FIGURE_REGISTRY_STALE") from exc
    chemical_digest, _ = _chemical_paper_bindings(root)
    if (
        not isinstance(figures, list)
        or not all(isinstance(row, dict) for row in figures)
        or not isinstance(locator_gaps, list)
        or payload.get("source_truth_digest") != _source_truth_digest(root)
        or payload.get("content_list_v2_digest") != content_list_v2_digest
        or payload.get("chemical_paper_project_binding_digest") != chemical_digest
    ):
        _fail("FIGURE_REGISTRY_STALE")
    expected = source_figure_registry_digest(payload)
    if payload.get("registry_digest") != expected:
        _fail("FIGURE_REGISTRY_INVALID")
    figure_ids: set[str] = set()
    for figure in figures:
        _validate(figure, SOURCE_FIGURE_SCHEMA, "FIGURE_REGISTRY_INVALID")
        figure_id = figure.get("figure_id")
        if not isinstance(figure_id, str) or figure_id in figure_ids:
            _fail("FIGURE_REGISTRY_INVALID")
        figure_ids.add(figure_id)
    return copy.deepcopy(payload)


def _evidence_ids(project: Path, study_id: str, source_id: str, page: int, label: str) -> list[str]:
    path = project / "01_evidence/paper_evidence_projection.jsonl"
    if not path.is_file() or path.is_symlink():
        return []
    ids: list[str] = []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("FIGURE_EVIDENCE_INVALID")
    for row in rows:
        if not isinstance(row, dict) or row.get("study_id") != study_id or row.get("source_id") != source_id:
            continue
        locator = row.get("locator")
        if not isinstance(locator, dict) or locator.get("page") != page:
            continue
        located = str(locator.get("figure_or_table") or "")
        if not located or label.lower() in located.lower() or "figure" in located.lower() or "fig" in located.lower():
            if isinstance(row.get("evidence_id"), str):
                ids.append(row["evidence_id"])
    return sorted(set(ids))


def build_source_figure_registry(project: Path) -> dict[str, Any]:
    """Rebuild source-figure entries from current Source Truth bytes."""
    root = _safe_project(project)
    try:
        studies = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ReviewFigureError(exc.code) from exc
    figures: list[dict[str, Any]] = []
    locator_gaps: list[dict[str, Any]] = []
    source_truth_digest = _source_truth_digest(root)
    content_list_v2_digest = _content_list_v2_digest(root)
    chemical_paper_project_binding_digest, chemical_imports = _chemical_paper_bindings(root)
    for study_id in studies:
        try:
            bundle = load_source_truth_bundle(root, study_id)
        except SourceTruthError as exc:
            raise ReviewFigureError(exc.code) from exc
        for source in bundle.get("sources", []):
            if not isinstance(source, dict) or source.get("document_role") != "MAIN":
                continue
            source_id = source.get("source_id")
            slug = source.get("mineru_slug")
            if not isinstance(source_id, str) or not isinstance(slug, str):
                _fail("FIGURE_SOURCE_INVALID")
            pdf_descriptor = source.get("pdf")
            if not isinstance(pdf_descriptor, dict) or not isinstance(pdf_descriptor.get("path"), str):
                _fail("FIGURE_SOURCE_INVALID")
            pdf_path = _safe_asset(root, pdf_descriptor["path"])
            if _sha256(pdf_path) != pdf_descriptor.get("sha256"):
                _fail("SOURCE_PDF_HASH_MISMATCH")
            if study_id in chemical_imports:
                locator_gaps.append(
                    {
                        "study_id": study_id,
                        "source_id": source_id,
                        "page": 1,
                        "reason": (
                            "MinerU Chemical Paper 导出包未提供独立图片文件；"
                            "其页面区域仅作为原始 PDF 定位，Source Figure 仍只使用"
                            "当前 Generic MinerU 的显式 caption 与原图资产。"
                        ),
                    }
                )
            _verify_source_images(root, source)
            extracted = root / "01_evidence/parses/extracted" / slug
            # v1 remains part of Source Truth byte integrity, but is never used
            # for page, caption, label, or grouping decisions.
            _content_entries(root, source)
            image_root = (extracted / "images").resolve(strict=True)
            if image_root.is_symlink() or not image_root.is_dir():
                _fail("FIGURE_ASSET_INVALID")
            blocks, block_gaps = _v2_image_blocks(
                root,
                study_id=study_id,
                source_id=source_id,
                source=source,
                extracted=extracted,
                image_root=image_root,
            )
            locator_gaps.extend(block_gaps)
            label_counts: dict[str, int] = {}
            for block in blocks:
                for label in block["labels"]:
                    normalized = _normalized_label(label)
                    label_counts[normalized] = label_counts.get(normalized, 0) + 1
            duplicate_labels = {
                label for label, count in label_counts.items() if count > 1
            }
            for normalized in sorted(duplicate_labels):
                duplicate_blocks = [
                    block
                    for block in blocks
                    if any(_normalized_label(label) == normalized for label in block["labels"])
                ]
                locator_gaps.append(
                    {
                        "study_id": study_id,
                        "source_id": source_id,
                        "page": min(block["page"] for block in duplicate_blocks),
                        "reason": "检测到重复图号，所有重复定位均已拒绝。",
                    }
                )
            candidates: list[dict[str, Any]] = []
            blocks_by_page: dict[int, list[dict[str, Any]]] = {}
            for block in blocks:
                blocks_by_page.setdefault(block["page"], []).append(block)
            for page, page_blocks in sorted(blocks_by_page.items()):
                anchors = [
                    index
                    for index, block in enumerate(page_blocks)
                    if len(block["labels"]) == 1
                ]
                invalid_anchors = {
                    index
                    for index, block in enumerate(page_blocks)
                    if len(block["labels"]) > 1
                    or (
                        len(block["labels"]) == 1
                        and _normalized_label(block["labels"][0]) in duplicate_labels
                    )
                }
                for index, block in enumerate(page_blocks):
                    if len(block["labels"]) > 1:
                        locator_gaps.append(
                            {
                                "study_id": study_id,
                                "source_id": source_id,
                                "page": page,
                                "reason": "单个 content_list_v2 图块图注包含多个图号，已拒绝定位。",
                            }
                        )
                distances = {
                    anchor: _anchor_distances(page_blocks, anchor)
                    for anchor in anchors
                }
                assignments: dict[int, list[int]] = {anchor: [] for anchor in anchors}
                missing: list[dict[str, Any]] = []
                for index, block in enumerate(page_blocks):
                    if block["labels"]:
                        continue
                    reachable = sorted(
                        (values[index], anchor)
                        for anchor, values in distances.items()
                        if math.isfinite(values[index])
                    )
                    if not reachable:
                        missing.append(block)
                        continue
                    best_distance, best_anchor = reachable[0]
                    ambiguity_limit = best_distance * 1.10 + 5.0
                    ambiguous_anchors = [
                        anchor
                        for distance, anchor in reachable
                        if distance <= ambiguity_limit
                    ]
                    if len(ambiguous_anchors) > 1:
                        invalid_anchors.update(ambiguous_anchors)
                        locator_gaps.append(
                            {
                                "study_id": study_id,
                                "source_id": source_id,
                                "page": page,
                                "reason": "同一图块可关联多个图号，无法可靠确定 caption 聚合关系。",
                            }
                        )
                        continue
                    assignments[best_anchor].append(index)
                for _component in _spatial_components(missing):
                    locator_gaps.append(
                        {
                            "study_id": study_id,
                            "source_id": source_id,
                            "page": page,
                            "reason": "content_list_v2 图块未绑定明确的原论文 Figure/Scheme/Chart 图注。",
                        }
                    )
                for anchor_index in anchors:
                    if anchor_index in invalid_anchors:
                        continue
                    anchor = page_blocks[anchor_index]
                    label = anchor["labels"][0]
                    fragment_indexes = sorted(
                        [anchor_index, *assignments[anchor_index]],
                        key=lambda index: (
                            page_blocks[index]["bbox"][1],
                            page_blocks[index]["bbox"][0],
                            page_blocks[index]["block_index"],
                        ),
                    )
                    candidates.append(
                        {
                            "page": page,
                            "label": label,
                            "caption": anchor["caption"],
                            "anchor": anchor,
                            "fragments": [page_blocks[index] for index in fragment_indexes],
                        }
                    )
            for image_number, candidate in enumerate(candidates, start=1):
                page = candidate["page"]
                label = candidate["label"]
                caption = candidate["caption"]
                anchor = candidate["anchor"]
                figure = {
                    "figure_id": (
                        f"{study_id}:{source_id}:"
                        f"{label.replace(' ', '-').replace('.', '').lower()}"
                    ),
                    "study_id": study_id,
                    "source_id": source_id,
                    "page": page,
                    "figure_label": label,
                    "caption": caption,
                    "asset_path": anchor["asset_path"],
                    "asset_sha256": anchor["asset_sha256"],
                    "source_pdf_sha256": source["pdf"]["sha256"],
                    "evidence_ids": _evidence_ids(root, study_id, source_id, page, label),
                    "selection_status": "selected" if image_number == 1 else "available",
                    "fragments": [
                        {
                            "page": fragment["page"],
                            "block_index": fragment["block_index"],
                            "bbox": fragment["bbox"],
                            "asset_path": fragment["asset_path"],
                            "asset_sha256": fragment["asset_sha256"],
                            "caption_association": (
                                "explicit_caption_anchor"
                                if fragment is anchor
                                else "same_page_spatial_group"
                            ),
                        }
                        for fragment in candidate["fragments"]
                    ],
                }
                _validate(figure, SOURCE_FIGURE_SCHEMA, "FIGURE_REGISTRY_INVALID")
                figures.append(figure)
    figures.sort(key=lambda row: (row["study_id"], row["source_id"], row["page"], row["figure_id"]))
    selected = [row for row in figures if row["selection_status"] == "selected"]
    minimum_slots, maximum_slots = 5, 8
    if len(selected) < minimum_slots:
        budget_status = "needs_human_selection"
        budget_gaps = [
            f"Select {minimum_slots - len(selected)} additional non-duplicative source figure(s) or register a synthesis placeholder."
        ]
    elif len(selected) > maximum_slots:
        budget_status = "over_budget"
        budget_gaps = [f"Reduce selected figures by {len(selected) - maximum_slots} slot(s)."]
    else:
        budget_status = "within_target"
        budget_gaps = []
    registry = {
        "schema_version": "review-writer-source-figure-registry.v1",
        "project_id": root.name,
        "figure_policy": FIGURE_POLICY,
        "figures": figures,
        "selected_count": len(selected),
        "available_count": len(figures),
        "required_count": max(minimum_slots - len(selected), 0),
        "target_figure_slots": {"minimum": minimum_slots, "maximum": maximum_slots},
        "source_truth_digest": source_truth_digest,
        "content_list_v2_digest": content_list_v2_digest,
        "chemical_paper_project_binding_digest": chemical_paper_project_binding_digest,
        "locator_gaps": locator_gaps,
        "figure_budget": {
            "status": budget_status,
            "selected_count": len(selected),
            "required_count": max(minimum_slots - len(selected), 0),
            "minimum": minimum_slots,
            "maximum": maximum_slots,
            "gaps": budget_gaps,
        },
        "registry_digest": canonical_digest(
            {
                "source_truth_digest": source_truth_digest,
                "content_list_v2_digest": content_list_v2_digest,
                "chemical_paper_project_binding_digest": chemical_paper_project_binding_digest,
                "figures": figures,
                "locator_gaps": locator_gaps,
            }
        ),
    }
    _atomic_json(root, REGISTRY_PATH, registry)
    return registry


def _load_placeholders(root: Path) -> list[dict[str, Any]]:
    path = root / PLACEHOLDER_PATH
    if not path.is_file() or path.is_symlink():
        return []
    payload = _read_json(path, "FIGURE_STATE_INVALID")
    if not isinstance(payload, dict) or not isinstance(payload.get("placeholders"), list):
        _fail("FIGURE_STATE_INVALID")
    placeholders = payload["placeholders"]
    if not all(isinstance(item, dict) for item in placeholders):
        _fail("FIGURE_STATE_INVALID")
    for item in placeholders:
        _validate(item, PLACEHOLDER_SCHEMA, "PLACEHOLDER_INVALID")
    return copy.deepcopy(placeholders)


def register_synthesis_figure_placeholder(project: Path, payload: object) -> dict[str, Any]:
    """Persist a researcher-facing brief; never creates an image asset."""
    root = _safe_project(project)
    if not isinstance(payload, dict):
        _fail("PLACEHOLDER_INVALID")
    candidate = copy.deepcopy(payload)
    _validate(candidate, PLACEHOLDER_SCHEMA, "PLACEHOLDER_INVALID")
    placeholders = _load_placeholders(root)
    placeholder_id = candidate["placeholder_id"]
    existing = next((row for row in placeholders if row["placeholder_id"] == placeholder_id), None)
    if existing is not None and existing != candidate:
        _fail("PLACEHOLDER_CONFLICT")
    if existing is None:
        placeholders.append(candidate)
        placeholders.sort(key=lambda row: row["placeholder_id"])
    state = {
        "schema_version": "review-writer-synthesis-figure-placeholders.v1",
        "project_id": root.name,
        "figure_policy": FIGURE_POLICY,
        "placeholders": placeholders,
        "placeholder_count": len(placeholders),
    }
    _atomic_json(root, PLACEHOLDER_PATH, state)
    return candidate


def synthesis_figure_placeholders(project: Path) -> list[dict[str, Any]]:
    return _load_placeholders(_safe_project(project))


# The Figure Plan slice is intentionally metadata-only.  It accepts already
# verified registry/placeholder records, but never follows an asset path.
FIGURE_PLAN_SCHEMA = "review-writer.figure-plan.v1"
FIGURE_PLAN_IMPACT_SCHEMA = "review-writer.figure-plan-impact-preview.v1"
FIGURE_PLAN_TYPES = (
    "source_figure",
    "redrawn_figure",
    "new_synthesis_figure",
    "placeholder",
)
_FIGURE_PLAN_TYPE_SET = frozenset(FIGURE_PLAN_TYPES)
_FIGURE_PLAN_FLAGS = frozenset({"GAP", "NON_COMPARABLE", "AI_PROVISIONAL"})
_FIGURE_PLAN_STALE_STATUSES = frozenset(
    {"STALE", "MISSING", "BLOCKED", "REJECTED", "UNRESOLVED"}
)
_FIGURE_PLAN_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FIGURE_PLAN_DERIVED_ROLES = frozenset(
    {"derived", "redrawn", "synthesis", "placeholder", "figure_plan"}
)
_FIGURE_PLAN_AUTHORIZATION_STATUSES = frozenset(
    {"cleared", "unknown", "pending", "required", "unverified", "not_applicable"}
)
_FIGURE_PLAN_REDRAW_STATUSES = frozenset(
    {"not_requested", "requested", "in_progress", "complete", "verified", "pending", "not_applicable"}
)


@dataclass(frozen=True)
class FigurePlanDownload:
    """A deterministic metadata download; no image or PDF bytes are included."""

    snapshot_id: str
    filename: str
    media_type: str
    content: bytes
    metadata: Mapping[str, Any]


def _figure_plan_copy(value: object, code: str = "FIGURE_PLAN_INVALID") -> Any:
    try:
        copied = copy.deepcopy(value)
        json.dumps(copied, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        _fail(code)
    return copied


def _figure_plan_identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or _FIGURE_PLAN_IDENTIFIER.fullmatch(value) is None:
        _fail(code)
    return value


def _figure_plan_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(code)
    return value.strip()


def _figure_plan_rows(
    value: object,
    *,
    nested_key: str | None,
    code: str,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if nested_key is not None and isinstance(value, Mapping):
        if nested_key not in value:
            _fail(code)
        value = value[nested_key]
    if not isinstance(value, (list, tuple)):
        _fail(code)
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            _fail(code)
        rows.append(_figure_plan_copy(dict(raw), code))
    return rows


def _figure_plan_index(
    rows: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
    code: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        identifier: object = None
        for field in fields:
            if row.get(field) is not None:
                identifier = row[field]
                break
        identifier = _figure_plan_identifier(identifier, code)
        if identifier in indexed:
            _fail(code)
        indexed[identifier] = row
    return indexed


def _figure_plan_ref_ids(
    row: Mapping[str, Any], fields: tuple[str, ...], code: str
) -> list[str]:
    selected: object = None
    for field in fields:
        if field in row:
            selected = row[field]
            break
    if selected is None:
        return []
    if not isinstance(selected, (list, tuple)):
        _fail(code)
    result: list[str] = []
    for raw in selected:
        if isinstance(raw, Mapping):
            raw = raw.get("id") or raw.get("figure_id") or raw.get("evidence_id")
        result.append(_figure_plan_identifier(raw, code))
    if len(result) != len(set(result)):
        _fail(code)
    return sorted(result)


def _figure_plan_roles(row: Mapping[str, Any], code: str) -> list[str]:
    raw_roles: object
    if "source_roles" in row and "source_role" in row:
        role_a = row["source_roles"]
        role_b = row["source_role"]
        if isinstance(role_a, str):
            role_a = [role_a]
        if isinstance(role_b, str):
            role_b = [role_b]
        if role_a != role_b:
            _fail("FIGURE_SOURCE_ROLE_INCONSISTENT")
        raw_roles = role_a
    elif "source_roles" in row:
        raw_roles = row["source_roles"]
    else:
        raw_roles = row.get("source_role")
    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]
    if not isinstance(raw_roles, (list, tuple)) or not raw_roles:
        _fail(code)
    roles = [_figure_plan_text(role, code) for role in raw_roles]
    if len(roles) != len(set(roles)):
        _fail(code)
    return sorted(roles)


def _figure_plan_lineage(row: Mapping[str, Any], code: str) -> dict[str, Any]:
    raw: object = row.get("lineage")
    if raw is None:
        raw = row.get("lineage_id")
    if isinstance(raw, str):
        lineage = {"lineage_id": _figure_plan_identifier(raw, code)}
    elif isinstance(raw, Mapping):
        lineage = _figure_plan_copy(dict(raw), code)
        lineage_id = lineage.get("lineage_id") or lineage.get("id")
        lineage["lineage_id"] = _figure_plan_identifier(lineage_id, code)
    else:
        _fail(code)
    return lineage


def _figure_plan_version(row: Mapping[str, Any], code: str) -> dict[str, Any]:
    raw: object = row.get("version")
    if raw is None:
        raw = row.get("version_id")
    if isinstance(raw, str):
        version = {"version_id": _figure_plan_identifier(raw, code)}
    elif isinstance(raw, Mapping):
        version = _figure_plan_copy(dict(raw), code)
        version_id = version.get("version_id") or version.get("id")
        version["version_id"] = _figure_plan_identifier(version_id, code)
    else:
        _fail(code)
    return version


def _figure_plan_mapping(row: Mapping[str, Any], field: str, code: str) -> dict[str, Any]:
    value = row.get(field)
    if not isinstance(value, Mapping) or not value:
        _fail(code)
    return _figure_plan_copy(dict(value), code)


def _figure_plan_record_id(row: Mapping[str, Any], fields: tuple[str, ...], code: str) -> str:
    for field in fields:
        if row.get(field) is not None:
            return _figure_plan_identifier(row[field], code)
    _fail(code)


def _figure_plan_validate_evidence(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed = _figure_plan_index(
        rows,
        fields=("evidence_id", "id"),
        code="FIGURE_EVIDENCE_INVALID",
    )
    for evidence in indexed.values():
        status = evidence.get("status")
        if not isinstance(status, str) or not status.strip():
            _fail("FIGURE_EVIDENCE_STATUS_REQUIRED")
        status_code = status.strip().upper()
        if (
            evidence.get("stale") is True
            or evidence.get("current") is False
            or status_code in _FIGURE_PLAN_STALE_STATUSES
        ):
            _fail("FIGURE_EVIDENCE_STALE")
        _figure_plan_text(evidence.get("source_id"), "FIGURE_SOURCE_ID_REQUIRED")
        _figure_plan_roles(evidence, "FIGURE_SOURCE_ROLE_REQUIRED")
        _figure_plan_lineage(evidence, "FIGURE_LINEAGE_REQUIRED")
        _figure_plan_mapping(evidence, "locator", "FIGURE_LOCATOR_REQUIRED")
        _figure_plan_mapping(evidence, "provenance", "FIGURE_PROVENANCE_REQUIRED")
    return indexed


def _figure_plan_prepare_context(
    *,
    source_figures: object,
    placeholders: object,
    evidence: object,
    synthesis: object,
    claims: object,
    drafts: object,
    citations: object,
    exports: object,
    releases: object,
) -> dict[str, Any]:
    source_rows = _figure_plan_rows(
        source_figures,
        nested_key="figures",
        code="FIGURE_SOURCE_RECORDS_INVALID",
    )
    for source in source_rows:
        _validate(source, SOURCE_FIGURE_SCHEMA, "FIGURE_SOURCE_RECORD_INVALID")
    placeholder_rows = _figure_plan_rows(
        placeholders,
        nested_key="placeholders",
        code="FIGURE_PLACEHOLDER_RECORDS_INVALID",
    )
    for placeholder in placeholder_rows:
        _validate(placeholder, PLACEHOLDER_SCHEMA, "FIGURE_PLACEHOLDER_RECORD_INVALID")
    evidence_rows = _figure_plan_rows(
        evidence,
        nested_key="rows",
        code="FIGURE_EVIDENCE_RECORDS_INVALID",
    )
    synthesis_rows = _figure_plan_rows(
        synthesis,
        nested_key="claims",
        code="FIGURE_SYNTHESIS_RECORDS_INVALID",
    )
    claim_rows = _figure_plan_rows(
        claims,
        nested_key="claims",
        code="FIGURE_CLAIM_RECORDS_INVALID",
    )
    draft_rows = _figure_plan_rows(
        drafts,
        nested_key="drafts",
        code="FIGURE_DRAFT_RECORDS_INVALID",
    )
    citation_rows = _figure_plan_rows(
        citations,
        nested_key="citations",
        code="FIGURE_CITATION_RECORDS_INVALID",
    )
    export_rows = _figure_plan_rows(
        exports,
        nested_key="exports",
        code="FIGURE_EXPORT_RECORDS_INVALID",
    )
    release_rows = _figure_plan_rows(
        releases,
        nested_key="releases",
        code="FIGURE_RELEASE_RECORDS_INVALID",
    )
    return {
        "source_figures": source_rows,
        "source_by_id": _figure_plan_index(
            source_rows, fields=("figure_id", "id"), code="FIGURE_SOURCE_RECORD_INVALID"
        ),
        "placeholders": placeholder_rows,
        "placeholder_by_id": _figure_plan_index(
            placeholder_rows,
            fields=("placeholder_id", "id"),
            code="FIGURE_PLACEHOLDER_RECORD_INVALID",
        ),
        "evidence": evidence_rows,
        "evidence_by_id": _figure_plan_validate_evidence(evidence_rows),
        "synthesis": synthesis_rows,
        "synthesis_by_id": _figure_plan_index(
            synthesis_rows,
            fields=("synthesis_id", "id"),
            code="FIGURE_SYNTHESIS_RECORD_INVALID",
        ),
        "claims": claim_rows,
        "claim_by_id": _figure_plan_index(
            claim_rows, fields=("claim_id", "id"), code="FIGURE_CLAIM_RECORD_INVALID"
        ),
        "drafts": draft_rows,
        "draft_by_id": _figure_plan_index(
            draft_rows, fields=("draft_id", "id"), code="FIGURE_DRAFT_RECORD_INVALID"
        ),
        "citations": citation_rows,
        "citation_by_id": _figure_plan_index(
            citation_rows,
            fields=("citation_id", "id"),
            code="FIGURE_CITATION_RECORD_INVALID",
        ),
        "exports": export_rows,
        "export_by_id": _figure_plan_index(
            export_rows, fields=("export_id", "id"), code="FIGURE_EXPORT_RECORD_INVALID"
        ),
        "releases": release_rows,
        "release_by_id": _figure_plan_index(
            release_rows, fields=("release_id", "id"), code="FIGURE_RELEASE_RECORD_INVALID"
        ),
    }


def _figure_plan_require_refs(
    refs: list[str], indexed: Mapping[str, dict[str, Any]], code: str
) -> None:
    for identifier in refs:
        if identifier not in indexed:
            _fail(code)


def _figure_plan_validate_upstream_status(
    refs: list[str],
    indexed: Mapping[str, dict[str, Any]],
    *,
    stale_code: str,
) -> None:
    for identifier in refs:
        row = indexed[identifier]
        status = row.get("status")
        if (
            row.get("stale") is True
            or row.get("current") is False
            or (isinstance(status, str) and status.strip().upper() in _FIGURE_PLAN_STALE_STATUSES)
        ):
            _fail(stale_code)


def _figure_plan_status_flags(raw: Mapping[str, Any]) -> set[str]:
    value = raw.get("status_flags", [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        _fail("FIGURE_STATUS_FLAGS_INVALID")
    flags = {str(flag).strip().upper() for flag in value}
    if not flags <= _FIGURE_PLAN_FLAGS:
        _fail("FIGURE_STATUS_FLAGS_INVALID")
    return flags


def _figure_plan_normalize_item(
    raw: Mapping[str, Any], context: Mapping[str, Any]
) -> dict[str, Any]:
    item = _figure_plan_copy(dict(raw), "FIGURE_PLAN_ITEM_INVALID")
    figure_id = _figure_plan_record_id(
        item, ("figure_id", "id"), "FIGURE_ID_REQUIRED"
    )
    raw_type = item.get("type")
    alias_type = item.get("figure_type")
    if raw_type is not None and alias_type is not None and raw_type != alias_type:
        _fail("FIGURE_TYPE_INCONSISTENT")
    figure_type = _figure_plan_text(
        raw_type if raw_type is not None else alias_type,
        "FIGURE_TYPE_REQUIRED",
    )
    if figure_type not in _FIGURE_PLAN_TYPE_SET:
        _fail("FIGURE_TYPE_INVALID")
    purpose = _figure_plan_text(item.get("purpose"), "FIGURE_PURPOSE_REQUIRED")
    research_question = _figure_plan_text(
        item.get("research_question") or item.get("rq") or item.get("rq_id"),
        "FIGURE_RQ_REQUIRED",
    )
    section_id = _figure_plan_identifier(
        item.get("section_id") or item.get("section"), "FIGURE_SECTION_REQUIRED"
    )
    evidence_ids = _figure_plan_ref_ids(
        item, ("evidence_ids", "evidence_refs"), "FIGURE_EVIDENCE_REFS_INVALID"
    )
    synthesis_ids = _figure_plan_ref_ids(
        item, ("synthesis_ids", "synthesis_refs"), "FIGURE_SYNTHESIS_REFS_INVALID"
    )
    claim_ids = _figure_plan_ref_ids(
        item, ("claim_ids", "claim_refs"), "FIGURE_CLAIM_REFS_INVALID"
    )
    draft_ids = _figure_plan_ref_ids(
        item, ("draft_ids", "draft_refs"), "FIGURE_DRAFT_REFS_INVALID"
    )
    citation_ids = _figure_plan_ref_ids(
        item, ("citation_ids", "citation_refs"), "FIGURE_CITATION_REFS_INVALID"
    )
    export_ids = _figure_plan_ref_ids(
        item, ("export_ids", "export_refs"), "FIGURE_EXPORT_REFS_INVALID"
    )
    release_ids = _figure_plan_ref_ids(
        item, ("release_ids", "release_refs"), "FIGURE_RELEASE_REFS_INVALID"
    )
    _figure_plan_require_refs(
        evidence_ids, context["evidence_by_id"], "FIGURE_EVIDENCE_NOT_FOUND"
    )
    _figure_plan_require_refs(
        synthesis_ids, context["synthesis_by_id"], "FIGURE_SYNTHESIS_NOT_FOUND"
    )
    _figure_plan_require_refs(
        claim_ids, context["claim_by_id"], "FIGURE_CLAIM_NOT_FOUND"
    )
    _figure_plan_require_refs(
        draft_ids, context["draft_by_id"], "FIGURE_DRAFT_NOT_FOUND"
    )
    _figure_plan_require_refs(
        citation_ids, context["citation_by_id"], "FIGURE_CITATION_NOT_FOUND"
    )
    _figure_plan_require_refs(
        export_ids, context["export_by_id"], "FIGURE_EXPORT_NOT_FOUND"
    )
    _figure_plan_require_refs(
        release_ids, context["release_by_id"], "FIGURE_RELEASE_NOT_FOUND"
    )
    _figure_plan_validate_upstream_status(
        synthesis_ids,
        context["synthesis_by_id"],
        stale_code="FIGURE_SYNTHESIS_STALE",
    )
    _figure_plan_validate_upstream_status(
        claim_ids,
        context["claim_by_id"],
        stale_code="FIGURE_CLAIM_STALE",
    )
    _figure_plan_validate_upstream_status(
        draft_ids,
        context["draft_by_id"],
        stale_code="FIGURE_DRAFT_STALE",
    )
    caption = item.get("caption")
    if caption is None:
        caption = item.get("caption_draft")
    caption = _figure_plan_text(caption, "FIGURE_CAPTION_REQUIRED")
    legend = item.get("legend")
    if not isinstance(legend, str):
        _fail("FIGURE_LEGEND_REQUIRED")
    version = _figure_plan_version(item, "FIGURE_VERSION_REQUIRED")
    lineage = _figure_plan_lineage(item, "FIGURE_LINEAGE_REQUIRED")
    roles = _figure_plan_roles(item, "FIGURE_SOURCE_ROLE_REQUIRED")
    provenance = _figure_plan_mapping(item, "provenance", "FIGURE_PROVENANCE_REQUIRED")
    locator = _figure_plan_mapping(item, "locator", "FIGURE_LOCATOR_REQUIRED")
    authorization = item.get("authorization_status")
    if authorization is None:
        authorization = item.get("authorization")
    if authorization is None:
        authorization = item.get("rights_status", "unknown")
    authorization = _figure_plan_text(
        authorization, "FIGURE_AUTHORIZATION_STATUS_INVALID"
    ).casefold()
    if authorization not in _FIGURE_PLAN_AUTHORIZATION_STATUSES:
        _fail("FIGURE_AUTHORIZATION_STATUS_INVALID")
    redraw_status = item.get("redraw_status", "not_applicable")
    redraw_status = _figure_plan_text(
        redraw_status, "FIGURE_REDRAW_STATUS_INVALID"
    ).casefold()
    if redraw_status not in _FIGURE_PLAN_REDRAW_STATUSES:
        _fail("FIGURE_REDRAW_STATUS_INVALID")

    source_figure_id = item.get("source_figure_id")
    if source_figure_id is None:
        source_figure_id = provenance.get("source_figure_id")
    if source_figure_id is not None:
        source_figure_id = _figure_plan_identifier(
            source_figure_id, "FIGURE_SOURCE_FIGURE_INVALID"
        )
        source = context["source_by_id"].get(source_figure_id)
        if source is None:
            _fail("FIGURE_SOURCE_FIGURE_NOT_FOUND")
        source_evidence_ids = sorted(
            str(identifier) for identifier in source.get("evidence_ids", [])
        )
        if figure_type in {"source_figure", "redrawn_figure"} and source_evidence_ids != evidence_ids:
            _fail("FIGURE_EVIDENCE_LINEAGE_INCONSISTENT")
        source_page = source.get("page")
        if "page" in locator and locator.get("page") != source_page:
            _fail("FIGURE_LOCATOR_INCONSISTENT")
        source_status = source.get("rights_status")
        if source_status != "cleared":
            authorization = "unknown"
    elif figure_type in {"source_figure", "redrawn_figure"}:
        _fail("FIGURE_SOURCE_FIGURE_REQUIRED")

    placeholder_id = item.get("placeholder_id")
    if placeholder_id is not None:
        placeholder_id = _figure_plan_identifier(
            placeholder_id, "FIGURE_PLACEHOLDER_INVALID"
        )
        placeholder = context["placeholder_by_id"].get(placeholder_id)
        if placeholder is None:
            _fail("FIGURE_PLACEHOLDER_NOT_FOUND")
        for panel in placeholder.get("panels", []):
            _figure_plan_require_refs(
                _figure_plan_ref_ids(
                    panel, ("source_figure_ids",), "FIGURE_PLACEHOLDER_INVALID"
                ),
                context["source_by_id"],
                "FIGURE_SOURCE_FIGURE_NOT_FOUND",
            )
            _figure_plan_require_refs(
                _figure_plan_ref_ids(
                    panel, ("synthesis_claim_ids",), "FIGURE_PLACEHOLDER_INVALID"
                ),
                context["claim_by_id"],
                "FIGURE_CLAIM_NOT_FOUND",
            )
    elif figure_type == "placeholder":
        _fail("FIGURE_PLACEHOLDER_REQUIRED")

    evidence_rows = [context["evidence_by_id"][identifier] for identifier in evidence_ids]
    evidence_roles = {
        role
        for evidence in evidence_rows
        for role in _figure_plan_roles(evidence, "FIGURE_SOURCE_ROLE_REQUIRED")
    }
    if not all(role in evidence_roles or role.casefold() in _FIGURE_PLAN_DERIVED_ROLES for role in roles):
        _fail("FIGURE_SOURCE_ROLE_INCONSISTENT")
    evidence_lineages = {
        _figure_plan_lineage(evidence, "FIGURE_LINEAGE_REQUIRED")["lineage_id"]
        for evidence in evidence_rows
    }
    if evidence_ids and lineage["lineage_id"] not in evidence_lineages:
        _fail("FIGURE_LINEAGE_INCONSISTENT")
    for evidence in evidence_rows:
        evidence_locator = _figure_plan_mapping(
            evidence, "locator", "FIGURE_LOCATOR_REQUIRED"
        )
        if "page" in locator and "page" in evidence_locator and locator["page"] != evidence_locator["page"]:
            _fail("FIGURE_LOCATOR_INCONSISTENT")
        if provenance.get("source_id") is not None and provenance.get("source_id") != evidence.get("source_id"):
            _fail("FIGURE_PROVENANCE_INCONSISTENT")

    flags = _figure_plan_status_flags(item)
    for evidence in evidence_rows:
        status = str(evidence.get("status", "")).strip().upper()
        if status in _FIGURE_PLAN_FLAGS:
            flags.add(status)
    if not evidence_ids:
        flags.update({"GAP", "AI_PROVISIONAL"})
    if figure_type in {"new_synthesis_figure", "placeholder"}:
        flags.update({"GAP", "AI_PROVISIONAL"})
    if figure_type == "redrawn_figure" and redraw_status not in {"complete", "verified"}:
        flags.update({"GAP", "AI_PROVISIONAL"})
    if figure_type == "redrawn_figure" and not any(
        item.get(field) for field in ("asset_ref", "asset_path", "asset_sha256")
    ):
        flags.update({"GAP", "AI_PROVISIONAL"})
    if authorization != "cleared" and authorization != "not_applicable":
        flags.add("GAP")
    source_comparison = item.get("comparison")
    if isinstance(source_comparison, Mapping):
        if str(source_comparison.get("status", "")).upper() == "NON_COMPARABLE":
            flags.add("NON_COMPARABLE")
    if str(item.get("comparability", "")).upper() == "NON_COMPARABLE":
        flags.add("NON_COMPARABLE")
    if str(item.get("comparison_status", "")).upper() == "NON_COMPARABLE":
        flags.add("NON_COMPARABLE")
    if item.get("comparable") is False:
        flags.add("NON_COMPARABLE")
    comparison_status = "NON_COMPARABLE" if "NON_COMPARABLE" in flags else "COMPARABLE"

    normalized = _figure_plan_copy(item, "FIGURE_PLAN_ITEM_INVALID")
    normalized.update(
        {
            "figure_id": figure_id,
            "purpose": purpose,
            "type": figure_type,
            "figure_type": figure_type,
            "research_question": research_question,
            "section_id": section_id,
            "evidence_ids": evidence_ids,
            "synthesis_ids": synthesis_ids,
            "claim_ids": claim_ids,
            "draft_ids": draft_ids,
            "citation_ids": citation_ids,
            "export_ids": export_ids,
            "release_ids": release_ids,
            "caption": caption,
            "legend": legend,
            "version": version,
            "lineage": lineage,
            "source_role": roles[0] if len(roles) == 1 else roles,
            "provenance": provenance,
            "locator": locator,
            "authorization_status": authorization,
            "redraw_status": redraw_status,
            "status_flags": sorted(flags),
            "status": (
                "GAP"
                if "GAP" in flags
                else "NON_COMPARABLE"
                if "NON_COMPARABLE" in flags
                else "AI_PROVISIONAL"
                if "AI_PROVISIONAL" in flags
                else "PLANNED"
            ),
            "comparison": {"status": comparison_status, "merge": "NONE"},
            "promotion": "NONE",
        }
    )
    if source_figure_id is not None:
        normalized["source_figure_id"] = source_figure_id
    if placeholder_id is not None:
        normalized["placeholder_id"] = placeholder_id
    return normalized


def _figure_plan_build_from_context(
    items: object, *, project_id: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    if isinstance(items, Mapping) and "items" in items:
        items = items["items"]
    raw_items = _figure_plan_rows(
        items, nested_key=None, code="FIGURE_PLAN_ITEMS_INVALID"
    )
    if not raw_items:
        _fail("FIGURE_PLAN_EMPTY")
    normalized_items = [_figure_plan_normalize_item(item, context) for item in raw_items]
    if len({item["figure_id"] for item in normalized_items}) != len(normalized_items):
        _fail("FIGURE_ID_CONFLICT")
    normalized_items.sort(key=lambda item: item["figure_id"])
    plan: dict[str, Any] = {
        "schema_version": FIGURE_PLAN_SCHEMA,
        "project_id": project_id,
        "figure_policy": FIGURE_POLICY,
        "items": normalized_items,
        "promotion": "NONE",
        "acceptance": {
            "human_acceptance": "UNKNOWN",
            "scientific_validity": "UNKNOWN",
        },
    }
    plan["plan_digest"] = canonical_digest(plan)
    return _figure_plan_copy(plan)


def build_figure_plan(
    items: object,
    *,
    project_id: object = "project",
    source_figures: object = None,
    placeholders: object = None,
    evidence: object = None,
    synthesis: object = None,
    claims: object = None,
    drafts: object = None,
    citations: object = None,
    exports: object = None,
    releases: object = None,
) -> dict[str, Any]:
    """Build a source-bound Figure Plan in memory without touching asset bytes."""

    project_id = _figure_plan_identifier(project_id, "FIGURE_PROJECT_ID_INVALID")
    context = _figure_plan_prepare_context(
        source_figures=source_figures,
        placeholders=placeholders,
        evidence=evidence,
        synthesis=synthesis,
        claims=claims,
        drafts=drafts,
        citations=citations,
        exports=exports,
        releases=releases,
    )
    return _figure_plan_build_from_context(items, project_id=project_id, context=context)


def figure_plan_digest(plan: Mapping[str, Any]) -> str:
    """Return the deterministic digest of a Figure Plan without its digest field."""

    if not isinstance(plan, Mapping):
        _fail("FIGURE_PLAN_INVALID")
    unsigned = _figure_plan_copy(dict(plan), "FIGURE_PLAN_INVALID")
    unsigned.pop("plan_digest", None)
    return canonical_digest(unsigned)


class FigurePlanWorkspace:
    """Pure-memory Figure Plan history with explicit, pointer-last activation."""

    def __init__(
        self,
        *,
        project_id: str,
        context: Mapping[str, Any],
        plan: Mapping[str, Any],
        snapshot_id: str,
        branch_id: str,
    ) -> None:
        self._project_id = project_id
        self._context = _figure_plan_copy(dict(context), "FIGURE_PLAN_INVALID")
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._branch_heads: dict[str, str] = {}
        self._active_branch_id = branch_id
        self._current_snapshot_id = snapshot_id
        self._revision = 0
        node = self._new_node(
            snapshot_id=snapshot_id,
            parent_snapshot_id=None,
            branch_id=branch_id,
            plan=plan,
        )
        self._snapshots[snapshot_id] = node
        self._branch_heads[branch_id] = snapshot_id

    @classmethod
    def from_records(
        cls,
        *,
        items: object,
        project_id: object = "project",
        source_figures: object = None,
        placeholders: object = None,
        evidence: object = None,
        synthesis: object = None,
        claims: object = None,
        drafts: object = None,
        citations: object = None,
        exports: object = None,
        releases: object = None,
        version_id: object = "v1",
        snapshot_id: object | None = None,
        branch_id: object = "main",
    ) -> "FigurePlanWorkspace":
        project_id = _figure_plan_identifier(project_id, "FIGURE_PROJECT_ID_INVALID")
        initial_snapshot_id = _figure_plan_identifier(
            snapshot_id if snapshot_id is not None else version_id,
            "FIGURE_SNAPSHOT_ID_INVALID",
        )
        branch_id = _figure_plan_identifier(branch_id, "FIGURE_BRANCH_ID_INVALID")
        context = _figure_plan_prepare_context(
            source_figures=source_figures,
            placeholders=placeholders,
            evidence=evidence,
            synthesis=synthesis,
            claims=claims,
            drafts=drafts,
            citations=citations,
            exports=exports,
            releases=releases,
        )
        plan = _figure_plan_build_from_context(
            items, project_id=project_id, context=context
        )
        return cls(
            project_id=project_id,
            context=context,
            plan=plan,
            snapshot_id=initial_snapshot_id,
            branch_id=branch_id,
        )

    @staticmethod
    def _new_node(
        *,
        snapshot_id: str,
        parent_snapshot_id: str | None,
        branch_id: str,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        copied_plan = _figure_plan_copy(dict(plan), "FIGURE_PLAN_INVALID")
        expected_digest = figure_plan_digest(copied_plan)
        if copied_plan.get("plan_digest") != expected_digest:
            _fail("FIGURE_PLAN_DIGEST_INVALID")
        return {
            "snapshot_id": snapshot_id,
            "parent_snapshot_id": parent_snapshot_id,
            "branch_id": branch_id,
            "plan": copied_plan,
            "snapshot_digest": expected_digest,
            "promotion": "NONE",
        }

    def _find_node(self, snapshot_id: object) -> tuple[dict[str, Any], bool]:
        identifier = _figure_plan_identifier(snapshot_id, "FIGURE_SNAPSHOT_ID_INVALID")
        node = self._snapshots.get(identifier)
        if node is not None:
            return node, False
        node = self._pending.get(identifier)
        if node is not None:
            return node, True
        _fail("FIGURE_SNAPSHOT_NOT_FOUND")

    def _view(self, node: Mapping[str, Any]) -> dict[str, Any]:
        snapshot_id = str(node["snapshot_id"])
        branch_id = str(node["branch_id"])
        is_current = snapshot_id == self._current_snapshot_id
        is_active_head = (
            branch_id == self._active_branch_id
            and self._branch_heads.get(branch_id) == snapshot_id
        )
        can_write = is_current and is_active_head and snapshot_id in self._snapshots
        view = _figure_plan_copy(dict(node), "FIGURE_PLAN_INVALID")
        view.update(
            {
                "read_only": not can_write,
                "can_write": can_write,
                "is_current": is_current,
                "is_active_head": is_active_head,
            }
        )
        return view

    def state(self) -> dict[str, Any]:
        return {
            "schema_version": "review-writer.figure-plan-state.v1",
            "project_id": self._project_id,
            "current_snapshot_id": self._current_snapshot_id,
            "active_branch_id": self._active_branch_id,
            "active_head_id": self._branch_heads[self._active_branch_id],
            "writable_snapshot_id": self._branch_heads[self._active_branch_id],
            "revision": self._revision,
            "branch_heads": dict(sorted(self._branch_heads.items())),
            "promotion": "NONE",
        }

    def snapshot(self, snapshot_id: str | None = None) -> dict[str, Any]:
        return self.view_snapshot(snapshot_id or self._current_snapshot_id)

    def view_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        node, _pending = self._find_node(snapshot_id)
        return self._view(node)

    view = view_snapshot

    def compare_snapshots(self, left_snapshot_id: str, right_snapshot_id: str) -> dict[str, Any]:
        left, _left_pending = self._find_node(left_snapshot_id)
        right, _right_pending = self._find_node(right_snapshot_id)
        changes: dict[str, Any] = {}
        if left["plan"] != right["plan"]:
            changes["plan"] = {
                "left": _figure_plan_copy(left["plan"]),
                "right": _figure_plan_copy(right["plan"]),
            }
        return {
            "schema_version": "review-writer.figure-plan-compare.v1",
            "left_snapshot_id": left["snapshot_id"],
            "right_snapshot_id": right["snapshot_id"],
            "changed_fields": sorted(changes),
            "changes": changes,
            "promotion": "NONE",
        }

    compare = compare_snapshots

    def download_snapshot(self, snapshot_id: str) -> FigurePlanDownload:
        node, _pending = self._find_node(snapshot_id)
        payload = {
            "schema_version": "review-writer.figure-plan-download.v1",
            "project_id": self._project_id,
            "snapshot_id": node["snapshot_id"],
            "parent_snapshot_id": node["parent_snapshot_id"],
            "branch_id": node["branch_id"],
            "snapshot_digest": node["snapshot_digest"],
            "plan": _figure_plan_copy(node["plan"]),
            "promotion": "NONE",
        }
        content = (
            json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        return FigurePlanDownload(
            snapshot_id=str(node["snapshot_id"]),
            filename=f"figure-plan-{node['snapshot_id']}.json",
            media_type="application/json",
            content=content,
            metadata={
                "project_id": self._project_id,
                "branch_id": node["branch_id"],
                "snapshot_digest": node["snapshot_digest"],
                "read_only": True,
            },
        )

    download = download_snapshot

    def impact_preview(
        self,
        figure_id: str,
        *,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = _figure_plan_identifier(figure_id, "FIGURE_ID_INVALID")
        node, _pending = self._find_node(snapshot_id or self._current_snapshot_id)
        item = next(
            (row for row in node["plan"]["items"] if row.get("figure_id") == identifier),
            None,
        )
        if item is None:
            _fail("FIGURE_ID_NOT_FOUND")
        refs = {
            "draft": sorted(set(item.get("draft_ids", []))),
            "citation": sorted(set(item.get("citation_ids", []))),
            "export": sorted(set(item.get("export_ids", []))),
            "release": sorted(set(item.get("release_ids", []))),
        }
        refs["draft"].extend(
            _figure_plan_context_refs(
                self._context["drafts"], fields=("draft_id", "id"), figure_id=identifier
            )
        )
        refs["citation"].extend(
            _figure_plan_context_refs(
                self._context["citations"],
                fields=("citation_id", "id"),
                figure_id=identifier,
            )
        )
        refs["export"].extend(
            _figure_plan_context_refs(
                self._context["exports"], fields=("export_id", "id"), figure_id=identifier
            )
        )
        refs["release"].extend(
            _figure_plan_context_refs(
                self._context["releases"], fields=("release_id", "id"), figure_id=identifier
            )
        )
        for key in refs:
            refs[key] = sorted(set(refs[key]))
        would_invalidate = [
            {"kind": kind, "id": ref, "reason": "FIGURE_PLAN_CHANGED"}
            for kind in ("citation", "draft", "export", "release")
            for ref in refs[kind]
        ]
        flags = set(item.get("status_flags", []))
        reasons = []
        if "GAP" in flags:
            reasons.append("FIGURE_GAP")
        if "NON_COMPARABLE" in flags:
            reasons.append("NON_COMPARABLE")
        if "AI_PROVISIONAL" in flags:
            reasons.append("AI_PROVISIONAL")
        if node["snapshot_id"] != self._current_snapshot_id:
            reasons.append("HISTORICAL_SNAPSHOT_READ_ONLY")
        return {
            "schema_version": FIGURE_PLAN_IMPACT_SCHEMA,
            "mode": "PREVIEW_ONLY",
            "mutation": "NONE",
            "promotion": "NONE",
            "target": {
                "figure_id": identifier,
                "snapshot_id": node["snapshot_id"],
            },
            "draft_refs": refs["draft"],
            "citation_refs": refs["citation"],
            "export_refs": refs["export"],
            "release_refs": refs["release"],
            "would_invalidate": would_invalidate,
            "blocking_reasons": sorted(set(reasons)),
            "comparison": _figure_plan_copy(item.get("comparison", {})),
            "acceptance": {
                "human_acceptance": "UNKNOWN",
                "scientific_validity": "UNKNOWN",
            },
        }

    @staticmethod
    def _replacement_items(replacement: object) -> object:
        if isinstance(replacement, Mapping):
            if "items" not in replacement:
                _fail("FIGURE_CHANGE_INVALID")
            return replacement["items"]
        if isinstance(replacement, (list, tuple)):
            return replacement
        _fail("FIGURE_CHANGE_INVALID")

    def propose_change(
        self,
        replacement: object,
        *,
        base_snapshot_id: str | None = None,
        branch_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        base_id = base_snapshot_id or self._current_snapshot_id
        base_node, _pending = self._find_node(base_id)
        if base_id != self._current_snapshot_id:
            if base_node["branch_id"] != self._active_branch_id:
                _fail("FIGURE_NON_HEAD_WRITE")
            _fail("FIGURE_HISTORICAL_READ_ONLY")
        if self._branch_heads.get(self._active_branch_id) != self._current_snapshot_id:
            _fail("FIGURE_NON_HEAD_WRITE")
        new_branch_id = _figure_plan_identifier(
            branch_id or f"figure-change-{base_node['snapshot_digest'][:12]}",
            "FIGURE_BRANCH_ID_INVALID",
        )
        new_snapshot_id = _figure_plan_identifier(
            snapshot_id or f"figure-snapshot-{base_node['snapshot_digest'][:12]}",
            "FIGURE_SNAPSHOT_ID_INVALID",
        )
        if new_branch_id in self._branch_heads or new_branch_id in {
            str(node["branch_id"]) for node in self._pending.values()
        }:
            _fail("FIGURE_BRANCH_CONFLICT")
        if new_snapshot_id in self._snapshots or new_snapshot_id in self._pending:
            _fail("FIGURE_SNAPSHOT_CONFLICT")
        candidate_plan = _figure_plan_build_from_context(
            self._replacement_items(replacement),
            project_id=self._project_id,
            context=self._context,
        )
        return {
            "schema_version": "review-writer.figure-plan-change-proposal.v1",
            "project_id": self._project_id,
            "base_snapshot_id": base_node["snapshot_id"],
            "branch_id": new_branch_id,
            "snapshot_id": new_snapshot_id,
            "candidate_plan": candidate_plan,
            "candidate_digest": candidate_plan["plan_digest"],
            "confirm_required": True,
            "activate_required": True,
            "promotion": "NONE",
        }

    propose = propose_change

    def confirm_change(
        self, proposal: Mapping[str, Any], *, confirm: bool = False, activate: bool = False
    ) -> dict[str, Any]:
        if not confirm:
            _fail("FIGURE_CONFIRMATION_REQUIRED")
        if not isinstance(proposal, Mapping):
            _fail("FIGURE_CHANGE_INVALID")
        base_id = _figure_plan_identifier(
            proposal.get("base_snapshot_id"), "FIGURE_SNAPSHOT_ID_INVALID"
        )
        base_node, _pending = self._find_node(base_id)
        if (
            base_id != self._current_snapshot_id
            or base_node["branch_id"] != self._active_branch_id
            or self._branch_heads.get(self._active_branch_id) != self._current_snapshot_id
        ):
            _fail("FIGURE_PROPOSAL_STALE")
        branch_id = _figure_plan_identifier(
            proposal.get("branch_id"), "FIGURE_BRANCH_ID_INVALID"
        )
        snapshot_id = _figure_plan_identifier(
            proposal.get("snapshot_id"), "FIGURE_SNAPSHOT_ID_INVALID"
        )
        if branch_id in self._branch_heads or branch_id in {
            str(node["branch_id"]) for node in self._pending.values()
        }:
            _fail("FIGURE_BRANCH_CONFLICT")
        if snapshot_id in self._snapshots or snapshot_id in self._pending:
            _fail("FIGURE_SNAPSHOT_CONFLICT")
        candidate = proposal.get("candidate_plan")
        if not isinstance(candidate, Mapping):
            _fail("FIGURE_CHANGE_INVALID")
        candidate = _figure_plan_copy(dict(candidate), "FIGURE_CHANGE_INVALID")
        if proposal.get("candidate_digest") != figure_plan_digest(candidate):
            _fail("FIGURE_PLAN_DIGEST_INVALID")
        node = self._new_node(
            snapshot_id=snapshot_id,
            parent_snapshot_id=base_id,
            branch_id=branch_id,
            plan=candidate,
        )
        self._pending[snapshot_id] = node
        if activate:
            return self.activate_snapshot(snapshot_id, confirm=True)
        return _figure_plan_copy(node)

    confirm = confirm_change

    def branch_from_here(
        self,
        source_snapshot_id: str,
        *,
        branch_id: str | None = None,
        snapshot_id: str | None = None,
        confirm: bool = False,
        activate: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            _fail("FIGURE_CONFIRMATION_REQUIRED")
        source, _pending = self._find_node(source_snapshot_id)
        branch_id = _figure_plan_identifier(
            branch_id or f"figure-branch-{source['snapshot_digest'][:12]}",
            "FIGURE_BRANCH_ID_INVALID",
        )
        snapshot_id = _figure_plan_identifier(
            snapshot_id or f"figure-snapshot-{source['snapshot_digest'][:12]}",
            "FIGURE_SNAPSHOT_ID_INVALID",
        )
        if branch_id in self._branch_heads or branch_id in {
            str(node["branch_id"]) for node in self._pending.values()
        }:
            _fail("FIGURE_BRANCH_CONFLICT")
        if snapshot_id in self._snapshots or snapshot_id in self._pending:
            _fail("FIGURE_SNAPSHOT_CONFLICT")
        node = self._new_node(
            snapshot_id=snapshot_id,
            parent_snapshot_id=str(source["snapshot_id"]),
            branch_id=branch_id,
            plan=source["plan"],
        )
        self._pending[snapshot_id] = node
        if activate:
            return self.activate_snapshot(snapshot_id, confirm=True)
        return _figure_plan_copy(node)

    create_branch = branch_from_here

    def activate_snapshot(
        self,
        snapshot_id: str,
        *,
        confirm: bool = False,
        expected_head_id: str | None = None,
    ) -> dict[str, Any]:
        if not confirm:
            _fail("FIGURE_CONFIRMATION_REQUIRED")
        node, is_pending = self._find_node(snapshot_id)
        snapshot_id = str(node["snapshot_id"])
        branch_id = str(node["branch_id"])
        if not is_pending and self._branch_heads.get(branch_id) != snapshot_id:
            _fail("FIGURE_NON_HEAD_WRITE")
        if expected_head_id is not None and expected_head_id != snapshot_id:
            _fail("FIGURE_STALE_REVISION")
        if is_pending:
            self._snapshots[snapshot_id] = self._pending.pop(snapshot_id)
            self._branch_heads[branch_id] = snapshot_id
        # The current pointer changes only after the immutable node and branch
        # head have been validated and installed.
        self._active_branch_id = branch_id
        self._current_snapshot_id = snapshot_id
        self._revision += 1
        return self.state()

    activate = activate_snapshot

    def activate_branch(
        self,
        branch_id: str,
        *,
        confirm: bool = False,
        expected_head_id: str | None = None,
    ) -> dict[str, Any]:
        branch_id = _figure_plan_identifier(branch_id, "FIGURE_BRANCH_ID_INVALID")
        pending_head = next(
            (
                node
                for node in self._pending.values()
                if node["branch_id"] == branch_id
            ),
            None,
        )
        head_id = (
            str(pending_head["snapshot_id"])
            if pending_head is not None
            else self._branch_heads.get(branch_id)
        )
        if head_id is None:
            _fail("FIGURE_BRANCH_NOT_FOUND")
        return self.activate_snapshot(
            head_id, confirm=confirm, expected_head_id=expected_head_id
        )


def build_figure_plan_impact_preview(
    workspace: FigurePlanWorkspace,
    figure_id: str,
    *,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Functional alias for the read-only Figure Plan impact preview."""

    return workspace.impact_preview(figure_id, snapshot_id=snapshot_id)


def _figure_plan_record_contains_figure(row: Mapping[str, Any], figure_id: str) -> bool:
    direct_fields = {
        "figure_id",
        "figure_ref",
        "figure_plan_id",
        "figure_ids",
        "figure_refs",
        "figure_plan_ids",
        "figures",
    }
    for field, value in row.items():
        if field not in direct_fields:
            continue
        if isinstance(value, str) and value == figure_id:
            return True
        if isinstance(value, (list, tuple)) and figure_id in value:
            return True
        if isinstance(value, Mapping) and (
            value.get("figure_id") == figure_id or value.get("id") == figure_id
        ):
            return True
    return False


def _figure_plan_context_refs(
    rows: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
    figure_id: str,
) -> list[str]:
    refs: list[str] = []
    for row in rows:
        identifier = _figure_plan_record_id(row, fields, "FIGURE_IMPACT_RECORD_INVALID")
        if _figure_plan_record_contains_figure(row, figure_id):
            refs.append(identifier)
    return sorted(set(refs))


figure_plan_impact_preview = build_figure_plan_impact_preview
