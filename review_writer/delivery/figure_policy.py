"""Release-time policy for manuscript figures."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from review_writer.project.review_figures import (
    ReviewFigureError,
    validate_source_figure_target_binding,
)
from review_writer.project.source_truth import canonical_digest


ORIGINAL_GENERATED = "ORIGINAL_GENERATED"
LICENSED_SOURCE = "LICENSED_SOURCE"
FIGURE_BRIEF_PLACEHOLDER = "FIGURE_BRIEF_PLACEHOLDER"
SOURCE_FIGURE_INTERNAL = "SOURCE_FIGURE_INTERNAL"
SYNTHESIS_FIGURE_PLACEHOLDER = "SYNTHESIS_FIGURE_PLACEHOLDER"
HUMAN_SYNTHESIS_FIGURE = "HUMAN_SYNTHESIS_FIGURE"
_FIGURE_TYPES = {ORIGINAL_GENERATED, LICENSED_SOURCE, FIGURE_BRIEF_PLACEHOLDER}
_CC_LICENSE_RE = re.compile(
    r"^cc(?:\s*-\s*|\s+)by(?P<sa>(?:\s*-\s*|\s+)sa)?"
    r"(?:\s*-\s*|\s+)(?P<version>1\.0|2\.0|2\.5|3\.0|4\.0)"
    r"(?P<international>\s+international)?$",
    flags=re.IGNORECASE,
)
_CC_LONG_LICENSE_RE = re.compile(
    r"^creative\s+commons\s+attribution(?P<sa>(?:-|\s+)sharealike)?\s+"
    r"(?P<version>1\.0|2\.0|2\.5|3\.0|4\.0)(?P<international>\s+international)?$",
    flags=re.IGNORECASE,
)
_CC0_RE = re.compile(
    r"^(?:cc0(?:\s*-\s*|\s+)1\.0|creative\s+commons\s+zero\s+1\.0"
    r"(?:\s+universal)?)$",
    flags=re.IGNORECASE,
)
_PUBLIC_DOMAIN_RE = re.compile(
    r"^public\s+domain(?P<kind>\s+dedication|\s+mark\s+1\.0)?$",
    flags=re.IGNORECASE,
)
_WRITTEN_AUTHORIZATION_RE = re.compile(
    r"^(?:explicit\s+)?written\s+(?:authorization|permission)$",
    flags=re.IGNORECASE,
)
_EXTENSION_FORMATS = {
    ".bmp": "BMP",
    ".gif": "GIF",
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".webp": "WEBP",
}
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000


class FigurePolicyError(ValueError):
    """A figure manifest is not eligible for verified release."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _canonical_sha256(value: Any) -> str:
    try:
        return canonical_digest(value)
    except (TypeError, ValueError) as exc:
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "figure manifest must be finite JSON") from exc


def _required_text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FigurePolicyError("FIGURE_POLICY_INVALID", f"figure {key} must be nonempty text")
    return value.strip()


def _figure_type(row: dict[str, Any]) -> str:
    value = row.get("figure_type")
    if value is None and row.get("license") in _FIGURE_TYPES:
        value = row.get("license")
    if value not in _FIGURE_TYPES:
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "figure_type is unsupported")
    return str(value)


def _required_text_list(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", f"figure {key} must be a nonempty text list")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", f"figure {key} values must be unique")
    return normalized


def _canonical_permitted_license(value: str) -> str | None:
    normalized = " ".join(value.split())
    match = _CC_LICENSE_RE.fullmatch(normalized) or _CC_LONG_LICENSE_RE.fullmatch(normalized)
    if match:
        family = "CC BY-SA" if match.group("sa") else "CC BY"
        international = " International" if match.group("international") else ""
        return f"{family} {match.group('version')}{international}"
    if _CC0_RE.fullmatch(normalized):
        return "CC0 1.0"
    match = _PUBLIC_DOMAIN_RE.fullmatch(normalized)
    if match:
        kind = (match.group("kind") or "").casefold()
        if "mark" in kind:
            return "Public Domain Mark 1.0"
        if "dedication" in kind:
            return "Public Domain Dedication"
        return "Public Domain"
    match = _WRITTEN_AUTHORIZATION_RE.fullmatch(normalized)
    if match:
        return "Written Permission" if "permission" in normalized.casefold() else "Written Authorization"
    return None


def _is_written_permission(value: str) -> bool:
    return _WRITTEN_AUTHORIZATION_RE.fullmatch(" ".join(value.split())) is not None


def _content_sha256(path: Path, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise FigurePolicyError(
                    "FIGURE_IMAGE_INVALID",
                    "figure image exceeds the release input limit",
                )
            digest.update(chunk)
    return digest.hexdigest()


def _image_binding(path: Path, markdown_path: str) -> dict[str, Any]:
    expected_format = _EXTENSION_FORMATS.get(Path(markdown_path).suffix.casefold())
    if expected_format is None:
        raise FigurePolicyError("FIGURE_IMAGE_INVALID", "figure image extension is unsupported")
    try:
        content_sha256 = _content_sha256(path, max_bytes=_MAX_IMAGE_BYTES)
        with Image.open(path) as image:
            image_format = image.format
            width, height = image.size
            if width <= 0 or height <= 0:
                raise FigurePolicyError(
                    "FIGURE_IMAGE_INVALID",
                    "figure image dimensions must be positive",
                )
            if width * height > _MAX_IMAGE_PIXELS:
                raise FigurePolicyError(
                    "FIGURE_IMAGE_INVALID",
                    "figure image exceeds the release pixel limit",
                )
            image.verify()
        with Image.open(path) as decoded:
            decoded.load()
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as exc:
        raise FigurePolicyError("FIGURE_IMAGE_INVALID", "figure image must be decodable") from exc
    if image_format != expected_format:
        raise FigurePolicyError(
            "FIGURE_IMAGE_INVALID",
            "figure image format must match its extension",
        )
    return {
        "content_sha256": content_sha256,
        "image_format": image_format,
        "width": width,
        "height": height,
    }


def validate_figure_policy(
    manifest: Any,
    *,
    approved_claim_ids: list[str],
    manuscript_sha256: str,
    manuscript_image_paths: list[str],
    manuscript_markdown: str,
    image_files_by_markdown_path: dict[str, Path],
) -> dict[str, Any]:
    """Validate release figure provenance and bind it to one manuscript revision."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("figures"), list):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "figure manifest must contain a figures list")
    if not manifest["figures"]:
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "figure manifest must contain at least one figure")
    if not isinstance(manuscript_sha256, str) or not manuscript_sha256:
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "manuscript digest is required")
    if (
        not isinstance(approved_claim_ids, list)
        or not all(isinstance(claim_id, str) and claim_id for claim_id in approved_claim_ids)
        or len(approved_claim_ids) != len(set(approved_claim_ids))
    ):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "approved claim ids must be unique text")
    approved_claim_id_set = set(approved_claim_ids)
    if not all(isinstance(path, str) and path for path in manuscript_image_paths):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "manuscript image paths must be nonempty text")
    if not manuscript_image_paths:
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "manuscript must reference at least one figure")
    if not isinstance(manuscript_markdown, str):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "authoritative manuscript text is required")
    if not isinstance(image_files_by_markdown_path, dict):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "figure image bindings are required")

    normalized: list[dict[str, Any]] = []
    figure_ids: set[str] = set()
    release_paths: set[str] = set()
    for raw in manifest["figures"]:
        if not isinstance(raw, dict):
            raise FigurePolicyError("FIGURE_POLICY_INVALID", "figure entries must be objects")
        figure_id = _required_text(raw, "figure_id")
        if figure_id in figure_ids:
            raise FigurePolicyError("FIGURE_POLICY_INVALID", "figure_id values must be unique")
        figure_ids.add(figure_id)
        figure_type = _figure_type(raw)

        if figure_type == FIGURE_BRIEF_PLACEHOLDER:
            _required_text(raw, "brief")
            raise FigurePolicyError(
                "FIGURE_PLACEHOLDER_PENDING",
                "figure brief placeholders must be resolved before verified release",
            )

        markdown_path = _required_text(raw, "markdown_path")
        if markdown_path in release_paths:
            raise FigurePolicyError("FIGURE_POLICY_INVALID", "markdown_path values must be unique")
        release_paths.add(markdown_path)
        figure = {
            "figure_id": figure_id,
            "figure_type": figure_type,
            "markdown_path": markdown_path,
        }
        if figure_type == ORIGINAL_GENERATED:
            source_claim_ids = _required_text_list(raw, "source_claim_ids")
            if not set(source_claim_ids) <= approved_claim_id_set:
                raise FigurePolicyError(
                    "FIGURE_CLAIM_NOT_APPROVED",
                    "original figure source claims must belong to the current writer whitelist",
                )
            figure["source_claim_ids"] = source_claim_ids
        elif figure_type == LICENSED_SOURCE:
            license_name = _required_text(raw, "license")
            canonical_license = _canonical_permitted_license(license_name)
            if canonical_license is None:
                raise FigurePolicyError(
                    "FIGURE_POLICY_INVALID",
                    "source figures require an open license, public domain status, or written authorization",
                )
            figure["license"] = canonical_license
            if _is_written_permission(license_name):
                for key in (
                    "permission_grantor",
                    "permission_scope",
                    "permission_evidence_reference",
                ):
                    figure[key] = _required_text(raw, key)
                if raw.get("researcher_confirmed") is not True:
                    raise FigurePolicyError(
                        "FIGURE_POLICY_INVALID",
                        "written permission requires explicit researcher confirmation",
                    )
                figure["researcher_confirmed"] = True
            attribution = _required_text(raw, "attribution")
            if attribution not in manuscript_markdown:
                raise FigurePolicyError(
                    "FIGURE_ATTRIBUTION_MISSING",
                    "source figure attribution must appear in the authoritative manuscript",
                )
            figure["attribution"] = attribution
        normalized.append(figure)

    used_paths = set(manuscript_image_paths)
    if len(used_paths) != len(manuscript_image_paths):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "each manuscript figure path must be unique")
    if used_paths != release_paths:
        raise FigurePolicyError(
            "FIGURE_POLICY_INVALID",
            "manuscript images and figure manifest entries must match exactly",
        )
    for figure in normalized:
        markdown_path = figure["markdown_path"]
        image_file = image_files_by_markdown_path.get(markdown_path)
        if not isinstance(image_file, Path):
            raise FigurePolicyError("FIGURE_IMAGE_INVALID", "figure image binding is missing")
        figure.update(_image_binding(image_file, markdown_path))

    return {
        "schema_version": "review-writer-figure-validation.v1",
        "status": "VERIFIED",
        "manuscript_sha256": manuscript_sha256,
        "manifest_sha256": _canonical_sha256(manifest),
        "figures": normalized,
    }


def figure_validation_is_current(
    validation: Any,
    *,
    manuscript_sha256: str,
    manifest: Any,
    image_files_by_markdown_path: dict[str, Path] | None = None,
) -> bool:
    """Return whether a stored figure validation still binds current inputs."""
    if not isinstance(validation, dict) or validation.get("status") != "VERIFIED":
        return False
    try:
        manifest_sha256 = _canonical_sha256(manifest)
    except FigurePolicyError:
        return False
    if not (
        validation.get("manuscript_sha256") == manuscript_sha256
        and validation.get("manifest_sha256") == manifest_sha256
    ):
        return False
    if image_files_by_markdown_path is None:
        return True
    figures = validation.get("figures")
    if not isinstance(figures, list):
        return False
    try:
        for figure in figures:
            if not isinstance(figure, dict):
                return False
            markdown_path = figure.get("markdown_path")
            image_path = image_files_by_markdown_path.get(markdown_path)
            if not isinstance(markdown_path, str) or not isinstance(image_path, Path):
                return False
            content_sha256 = figure.get("content_sha256")
            if not isinstance(content_sha256, str) or _content_sha256(image_path) != content_sha256:
                return False
    except OSError:
        return False
    return True


def source_figure_attribution(row: dict[str, Any]) -> str:
    """Return the stable visible attribution required in a new-route manuscript."""
    return (
        f"Source Figure Attribution: {_required_text(row, 'figure_id')} | "
        f"{_required_text(row, 'source_id')} | page {row.get('page')} | "
        f"{_required_text(row, 'figure_label')}"
    )


def validate_new_route_figure_policy(
    project: Path,
    *,
    source_registry: Any,
    placeholders: Any,
    manuscript_markdown: str,
    manuscript_image_paths: list[str],
    release_level: str,
    lineage_digest: str | None = None,
) -> dict[str, Any]:
    """Validate internal source figures and fail closed for expert release inputs."""
    if release_level not in {"SELF_REVIEWED_DRAFT", "EXPERT_REVIEWED_RELEASE"}:
        raise FigurePolicyError("RELEASE_LEVEL_INVALID", "release level is unsupported")
    if (
        not isinstance(source_registry, dict)
        or not isinstance(source_registry.get("figures"), list)
        or not isinstance(placeholders, list)
        or not isinstance(manuscript_markdown, str)
    ):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "new-route figure state is invalid")

    selected = [
        row
        for row in source_registry["figures"]
        if isinstance(row, dict) and row.get("selection_status") == "selected"
    ]
    # A text-only source may legitimately have no extracted image assets.  A
    # complete, manuscript-visible synthesis placeholder is still a real
    # researcher-facing Figure deliverable for SELF_REVIEWED_DRAFT; expert
    # release remains fail-closed below until a human figure is verified.
    if not selected and not placeholders:
        raise FigurePolicyError(
            "FIGURE_POLICY_INVALID",
            "at least one source figure or synthesis placeholder is required",
        )
    if any(not isinstance(row, dict) for row in source_registry["figures"]):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "source figure entries must be objects")
    if release_level == "EXPERT_REVIEWED_RELEASE" and any(
        isinstance(row, dict) and row.get("status") != "verified" for row in placeholders
    ):
        raise FigurePolicyError(
            "FIGURE_PLACEHOLDER_PENDING", "expert release requires verified human figures"
        )

    expected_paths: set[str] = set()
    normalized: list[dict[str, Any]] = []
    human_synthesis_figures: list[dict[str, Any]] = []
    attributions: list[str] = []
    project_root = project.resolve(strict=True)
    for row in selected:
        if isinstance(row.get("page"), bool) or not isinstance(row.get("page"), int) or row["page"] < 1:
            raise FigurePolicyError("FIGURE_POLICY_INVALID", "source figure page is invalid")
        asset_path = _required_text(row, "asset_path")
        relative = Path(asset_path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in asset_path:
            raise FigurePolicyError("FIGURE_IMAGE_INVALID", "source figure path is unsafe")
        asset = project / relative
        try:
            resolved = asset.resolve(strict=True)
            resolved.relative_to(project_root)
        except (OSError, ValueError) as exc:
            raise FigurePolicyError("FIGURE_IMAGE_INVALID", "source figure is unavailable") from exc
        if asset.is_symlink() or not resolved.is_file():
            raise FigurePolicyError("FIGURE_IMAGE_INVALID", "source figure must be a regular file")
        binding = _image_binding(resolved, asset_path)
        markdown_path = Path(os.path.relpath(resolved, project / "04_manuscript")).as_posix()
        expected_paths.add(markdown_path)
        _required_text(row, "caption")
        attribution = source_figure_attribution(row)
        target_binding = row.get("target_binding")
        if target_binding is not None:
            try:
                current_binding = validate_source_figure_target_binding(
                    row,
                    target_binding,
                    manuscript_markdown,
                    current_asset_sha256=binding["content_sha256"],
                )
            except ReviewFigureError as exc:
                raise FigurePolicyError(
                    "FIGURE_ATTRIBUTION_MISSING",
                    "source figure target binding is missing or stale",
                ) from exc
            if attribution not in manuscript_markdown:
                # The explicit source/evidence marker is the visible attribution
                # anchor for repaired legacy text; no free-text attribution is
                # synthesized or auto-placed by the release guard.
                attribution = current_binding["marker"]
        elif attribution not in manuscript_markdown:
            raise FigurePolicyError("FIGURE_ATTRIBUTION_MISSING", "source attribution is absent")
        if binding["content_sha256"] != row.get("asset_sha256"):
            raise FigurePolicyError("FIGURE_IMAGE_INVALID", "source figure hash is stale")
        if release_level == "EXPERT_REVIEWED_RELEASE":
            rights_status = row.get("rights_status")
            rights_license = row.get("rights_license")
            if rights_status != "cleared" or not isinstance(rights_license, str) or not rights_license.strip():
                raise FigurePolicyError("FIGURE_RIGHTS_NOT_CLEARED", "expert source figures require rights clearance")
        attributions.append(attribution)
        normalized.append(
            {
                "figure_id": _required_text(row, "figure_id"),
                "figure_type": SOURCE_FIGURE_INTERNAL,
                "asset_path": asset_path,
                "markdown_path": markdown_path,
                "content_sha256": binding["content_sha256"],
                "attribution": attribution,
            }
        )

    if release_level == "EXPERT_REVIEWED_RELEASE":
        from review_writer.evaluation.review_benchmark import (
            verified_synthesis_figure_bindings,
        )

        if not isinstance(lineage_digest, str):
            raise FigurePolicyError(
                "FIGURE_PLACEHOLDER_PENDING", "expert release requires human figure verification"
            )
        bindings = verified_synthesis_figure_bindings(
            project,
            placeholders=placeholders,
            lineage_digest=lineage_digest,
            manuscript_text=manuscript_markdown,
        )
        if bindings is None:
            raise FigurePolicyError(
                "FIGURE_PLACEHOLDER_PENDING", "expert release requires current human figure verification"
            )
        for binding in bindings:
            asset = (project / binding["asset_path"]).resolve(strict=True)
            markdown_path = Path(
                os.path.relpath(asset, project / "04_manuscript")
            ).as_posix()
            expected_paths.add(markdown_path)
            human_synthesis_figures.append(
                {
                    "placeholder_id": binding["placeholder_id"],
                    "figure_type": HUMAN_SYNTHESIS_FIGURE,
                    "asset_path": binding["asset_path"],
                    "markdown_path": markdown_path,
                    "content_sha256": binding["asset_sha256"],
                }
            )

    if set(manuscript_image_paths) != expected_paths or len(manuscript_image_paths) != len(expected_paths):
        raise FigurePolicyError("FIGURE_POLICY_INVALID", "manuscript images must match selected source figures")

    pending = 0
    placeholder_ids: set[str] = set()
    for row in placeholders:
        if not isinstance(row, dict):
            raise FigurePolicyError("FIGURE_POLICY_INVALID", "placeholder entries must be objects")
        placeholder_id = _required_text(row, "placeholder_id")
        if placeholder_id in placeholder_ids:
            raise FigurePolicyError("FIGURE_POLICY_INVALID", "placeholder ids must be unique")
        placeholder_ids.add(placeholder_id)
        for key in ("scientific_question", "caption_draft"):
            _required_text(row, key)
        panels = row.get("panels")
        if not isinstance(panels, list) or not panels or any(
            not isinstance(panel, dict) or not isinstance(panel.get("task"), str) or not panel["task"].strip()
            for panel in panels
        ):
            raise FigurePolicyError("FIGURE_POLICY_INVALID", "placeholder tasks are incomplete")
        if row.get("status") != "verified":
            pending += 1
            required_text = (
                f"SYNTHESIS_FIGURE_PLACEHOLDER: {placeholder_id}",
                row["scientific_question"].strip(),
                *[panel["task"].strip() for panel in panels],
            )
            if any(value not in manuscript_markdown for value in required_text):
                raise FigurePolicyError("FIGURE_PLACEHOLDER_INVALID", "placeholder is not visibly complete")
    if release_level == "EXPERT_REVIEWED_RELEASE" and pending:
        raise FigurePolicyError("FIGURE_PLACEHOLDER_PENDING", "expert release requires verified human figures")

    return {
        "schema_version": "review-writer-figure-validation.v2",
        "release_level": release_level,
        "source_figures": normalized,
        "human_synthesis_figures": human_synthesis_figures,
        "required_attributions": attributions,
        "expected_media_sha256": sorted(
            row["content_sha256"]
            for row in [*normalized, *human_synthesis_figures]
        ),
        "placeholder_count": len(placeholders),
        "pending_placeholder_count": pending,
    }
