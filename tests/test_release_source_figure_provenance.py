"""Focused release source-figure provenance and fail-closed checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from review_writer.delivery.figure_policy import (
    FigurePolicyError,
    validate_new_route_figure_policy,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_fingerprint(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _figure_fixture(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object], str]:
    project = tmp_path / "source-figure-project"
    (project / "00_sources").mkdir(parents=True)
    (project / "01_evidence").mkdir()
    (project / "04_manuscript").mkdir()
    source_pdf = project / "00_sources/main.pdf"
    source_pdf.write_bytes(b"authorized MAIN PDF")
    asset = project / "01_evidence/figure-1.png"
    Image.new("RGB", (8, 8), "white").save(asset)
    attribution = "Source Figure Attribution: study-1:source-1:figure-1 | source-1 | page 3 | Figure 1"
    markdown = f"# Results\n\n{attribution}\n\n![Figure 1](../01_evidence/figure-1.png)\n"
    registry_row: dict[str, object] = {
        "figure_id": "study-1:source-1:figure-1",
        "study_id": "study-1",
        "source_id": "source-1",
        "page": 3,
        "figure_label": "Figure 1",
        "caption": "Reaction overview.",
        "asset_path": "01_evidence/figure-1.png",
        "asset_sha256": _sha256(asset),
        "source_pdf_sha256": _sha256(source_pdf),
        "selection_status": "selected",
        "rights_status": "cleared",
        "rights_license": "CC BY 4.0",
        "rights_evidence_reference": "rights-record-1",
        "fragments": [],
    }
    registry = {
        "schema_version": "review-writer-source-figure-registry.v1",
        "project_id": project.name,
        "registry_digest": "a" * 64,
        "figures": [registry_row],
    }
    return project, registry, registry_row, markdown


def test_release_projection_preserves_source_bound_figure_provenance(tmp_path: Path) -> None:
    project, registry, row, markdown = _figure_fixture(tmp_path)

    result = validate_new_route_figure_policy(
        project,
        source_registry=registry,
        placeholders=[],
        manuscript_markdown=markdown,
        manuscript_image_paths=["../01_evidence/figure-1.png"],
        release_level="SELF_REVIEWED_DRAFT",
    )

    assert result["source_figure_registry_digest"] == registry["registry_digest"]
    projected = result["source_figures"][0]
    for key in (
        "study_id",
        "source_id",
        "page",
        "figure_label",
        "caption",
        "source_pdf_sha256",
        "asset_sha256",
        "rights_status",
        "rights_license",
        "rights_evidence_reference",
    ):
        assert projected[key] == row[key]
    assert projected["content_sha256"] == row["asset_sha256"]
    assert projected["markdown_path"] == "../01_evidence/figure-1.png"


@pytest.mark.parametrize("field", ["source_pdf_sha256", "study_id", "source_id", "caption"])
def test_release_projection_rejects_missing_source_provenance_zero_write(
    tmp_path: Path, field: str
) -> None:
    project, registry, _row, markdown = _figure_fixture(tmp_path)
    registry["figures"][0].pop(field)
    before = _tree_fingerprint(project)

    with pytest.raises(FigurePolicyError) as error:
        validate_new_route_figure_policy(
            project,
            source_registry=registry,
            placeholders=[],
            manuscript_markdown=markdown,
            manuscript_image_paths=["../01_evidence/figure-1.png"],
            release_level="SELF_REVIEWED_DRAFT",
        )

    assert error.value.code == "FIGURE_POLICY_INVALID"
    assert _tree_fingerprint(project) == before
