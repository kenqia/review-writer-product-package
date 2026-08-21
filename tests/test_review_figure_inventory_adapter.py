from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from review_writer.delivery import project_release
from review_writer.product_foundation import VersionContext
from review_writer.project import review_figures
from review_writer.project.review_figures import (
    ReviewFigureError,
    project_source_figure_candidates,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_inputs(
    tmp_path: Path,
    *,
    rights_status: str = "cleared",
) -> tuple[Path, list[dict[str, object]], list[dict[str, object]], Path, Path]:
    project = tmp_path / "review-project"
    pdf = project / "00_sources" / "main.pdf"
    asset = project / "01_evidence" / "parses" / "extracted" / "main" / "images" / "figure-1.png"
    pdf.parent.mkdir(parents=True)
    asset.parent.mkdir(parents=True)
    pdf.write_bytes(b"authorized source pdf")
    asset.write_bytes(b"parsed figure bytes")
    source = {
        "study_id": "study-1",
        "source_id": "source-1",
        "document_role": "MAIN",
        "pdf": {"path": "00_sources/main.pdf", "sha256": _sha256(pdf)},
    }
    parsed = {
        "study_id": "study-1",
        "source_id": "source-1",
        "page": 2,
        "figure_label": "Figure 1",
        "caption": "Reaction overview.",
        "asset_path": "01_evidence/parses/extracted/main/images/figure-1.png",
        "asset_sha256": _sha256(asset),
        "block_index": 0,
        "bbox": [1, 2, 100, 200],
        "locator": {
            "source_mode": "parsed_candidate",
            "page": 2,
            "section_or_item": "Results",
            "figure_or_table": "Figure 1",
            "exact_quote": "Reaction overview.",
        },
        "attribution": "Source paper Figure 1",
        "license_or_rights_basis": "CC BY 4.0",
        "rights_status": rights_status,
        "rights_evidence_reference": "source-1-license-record",
    }
    return project, [source], [parsed], pdf, asset


def _project_fingerprint(project: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(project).as_posix(),
                path.read_bytes(),
            )
            for path in project.rglob("*")
            if path.is_file()
        )
    )


def test_projects_one_authorized_parsed_figure_as_a_candidate_only_registry_row(
    tmp_path: Path,
) -> None:
    project = tmp_path / "review-project"
    pdf = project / "00_sources" / "main.pdf"
    asset = project / "01_evidence" / "parses" / "extracted" / "main" / "images" / "figure-1.png"
    pdf.parent.mkdir(parents=True)
    asset.parent.mkdir(parents=True)
    pdf.write_bytes(b"authorized source pdf")
    asset.write_bytes(b"parsed figure bytes")

    result = project_source_figure_candidates(
        project,
        [
            {
                "study_id": "study-1",
                "source_id": "source-1",
                "document_role": "MAIN",
                "pdf": {"path": "00_sources/main.pdf", "sha256": _sha256(pdf)},
            }
        ],
        [
            {
                "study_id": "study-1",
                "source_id": "source-1",
                "page": 2,
                "figure_label": "Figure 1",
                "caption": "Reaction overview.",
                "asset_path": "01_evidence/parses/extracted/main/images/figure-1.png",
                "asset_sha256": _sha256(asset),
                "block_index": 0,
                "bbox": [1, 2, 100, 200],
                "locator": {
                    "source_mode": "parsed_candidate",
                    "page": 2,
                    "section_or_item": "Results",
                    "figure_or_table": "Figure 1",
                    "exact_quote": "Reaction overview.",
                },
                "attribution": "Source paper Figure 1",
                "license_or_rights_basis": "CC BY 4.0",
                "rights_status": "cleared",
                "rights_evidence_reference": "source-1-license-record",
            }
        ],
    )

    assert result["status"] == "candidate"
    assert result["figures"][0] == {
        **result["figures"][0],
        "study_id": "study-1",
        "source_id": "source-1",
        "page": 2,
        "figure_label": "Figure 1",
        "caption": "Reaction overview.",
        "source_pdf_sha256": _sha256(pdf),
        "asset_sha256": _sha256(asset),
        "attribution": "Source paper Figure 1",
        "license_or_rights_basis": "CC BY 4.0",
        "status": "candidate",
    }
    assert result["figures"][0]["selection_status"] == "available"
    assert result["figures"][0]["release_status"] == "CANDIDATE_ONLY"
    assert result["gaps"] == []


def test_unknown_rights_remain_hold_without_promoting_the_candidate(tmp_path: Path) -> None:
    project, sources, figures, _pdf_path, _asset_path = _candidate_inputs(
        tmp_path,
        rights_status="unknown",
    )

    result = project_source_figure_candidates(project, sources, figures)

    assert result["status"] == "candidate"
    assert result["figures"][0]["release_status"] == "HOLD"
    assert result["gaps"] == [
        {
            "figure_id": result["figures"][0]["figure_id"],
            "code": "FIGURE_RIGHTS_UNKNOWN",
            "reason": "source figure rights require human confirmation",
        }
    ]


def test_wrong_source_hash_fails_closed_without_writes(tmp_path: Path) -> None:
    project, sources, figures, _pdf_path, _asset_path = _candidate_inputs(tmp_path)
    sources[0]["pdf"] = {"path": "00_sources/main.pdf", "sha256": "0" * 64}
    before = _project_fingerprint(project)

    with pytest.raises(ReviewFigureError) as error:
        project_source_figure_candidates(project, sources, figures)

    assert error.value.code == "SOURCE_PDF_HASH_MISMATCH"
    assert _project_fingerprint(project) == before
    assert not (project / "03_figures").exists()


def test_duplicate_asset_fails_closed_without_writes(tmp_path: Path) -> None:
    project, sources, figures, _pdf_path, _asset_path = _candidate_inputs(tmp_path)
    duplicate = copy.deepcopy(figures[0])
    duplicate["figure_label"] = "Figure 2"
    duplicate["locator"] = {**duplicate["locator"], "figure_or_table": "Figure 2"}
    figures.append(duplicate)
    before = _project_fingerprint(project)

    with pytest.raises(ReviewFigureError) as error:
        project_source_figure_candidates(project, sources, figures)

    assert error.value.code == "FIGURE_ASSET_DUPLICATE"
    assert _project_fingerprint(project) == before
    assert not (project / "03_figures").exists()


def test_invalid_locator_fails_closed_without_writes(tmp_path: Path) -> None:
    project, sources, figures, _pdf_path, _asset_path = _candidate_inputs(tmp_path)
    figures[0]["locator"] = {**figures[0]["locator"], "page": 3}
    before = _project_fingerprint(project)

    with pytest.raises(ReviewFigureError) as error:
        project_source_figure_candidates(project, sources, figures)

    assert error.value.code == "FIGURE_LOCATOR_INVALID"
    assert _project_fingerprint(project) == before
    assert not (project / "03_figures").exists()


def test_candidate_role_and_supplied_source_hash_cannot_override_main_binding(
    tmp_path: Path,
) -> None:
    project, sources, figures, _pdf_path, _asset_path = _candidate_inputs(tmp_path)
    figures[0]["document_role"] = "SI"
    figures[0]["source_pdf_sha256"] = "0" * 64
    before = _project_fingerprint(project)

    with pytest.raises(ReviewFigureError) as error:
        project_source_figure_candidates(project, sources, figures)

    assert error.value.code == "FIGURE_SOURCE_ROLE_INVALID"
    assert _project_fingerprint(project) == before


@pytest.mark.parametrize("missing_field", ["attribution", "rights_evidence_reference"])
def test_cleared_candidate_requires_attribution_and_rights_evidence(
    tmp_path: Path,
    missing_field: str,
) -> None:
    project, sources, figures, _pdf_path, _asset_path = _candidate_inputs(tmp_path)
    figures[0].pop(missing_field)
    before = _project_fingerprint(project)

    with pytest.raises(ReviewFigureError) as error:
        project_source_figure_candidates(project, sources, figures)

    assert error.value.code == "FIGURE_RIGHTS_INVALID"
    assert _project_fingerprint(project) == before


def test_candidate_projection_preserves_existing_figure_state_without_writes(
    tmp_path: Path,
) -> None:
    project, sources, figures, _pdf_path, _asset_path = _candidate_inputs(tmp_path)
    state = project / "03_figures"
    state.mkdir()
    (state / "source_figure_registry.json").write_text(
        '{"figures": [{"figure_id": "existing"}]}\n', encoding="utf-8"
    )
    (state / "current.json").write_text('{"figure_id": "existing"}\n', encoding="utf-8")
    (state / "history.jsonl").write_text('{"event": "existing"}\n', encoding="utf-8")
    before = _project_fingerprint(project)

    result = project_source_figure_candidates(project, sources, figures)

    assert result["status"] == "candidate"
    assert _project_fingerprint(project) == before


def test_inventory_adapter_cleans_text_preserves_grouped_fragments_and_source_order(
    tmp_path: Path,
) -> None:
    project = tmp_path / "review-project"
    pdf = project / "00_sources" / "main.pdf"
    anchor = project / "01_evidence" / "figures" / "anchor.png"
    continuation = project / "01_evidence" / "figures" / "continuation.png"
    figure_one = project / "01_evidence" / "figures" / "figure-one.png"
    pdf.parent.mkdir(parents=True)
    anchor.parent.mkdir(parents=True)
    pdf.write_bytes(b"authorized source pdf")
    anchor.write_bytes(b"anchor bytes")
    continuation.write_bytes(b"continuation bytes")
    figure_one.write_bytes(b"figure one bytes")
    source_hash = _sha256(pdf)
    anchor_hash = _sha256(anchor)
    continuation_hash = _sha256(continuation)
    figure_one_hash = _sha256(figure_one)

    def parsed(
        label: str,
        block_index: int,
        asset_path: str,
        asset_hash: str,
        *,
        fragments: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "study_id": "study-1",
            "source_id": "source-1",
            "page": 2,
            "figure_label": label,
            "caption": ["  Panel A\n", "  continued\tcaption  "],
            "asset_path": asset_path,
            "asset_sha256": asset_hash,
            "block_index": block_index,
            "bbox": [1, 2, 100, 200],
            "fragments": fragments,
            "locator": {
                "source_mode": "parsed_candidate",
                "page": 2,
                "section_or_item": "  Results\n section ",
                "figure_or_table": label,
                "exact_quote": "  Panel A\n continued caption  ",
            },
        }

    grouped = [
        {
            "page": 2,
            "block_index": 4,
            "bbox": [2, 3, 80, 90],
            "asset_path": "01_evidence/figures/anchor.png",
            "asset_sha256": anchor_hash,
            "caption_association": "explicit_caption_anchor",
        },
        {
            "page": 2,
            "block_index": 5,
            "bbox": [2, 95, 80, 180],
            "asset_path": "01_evidence/figures/continuation.png",
            "asset_sha256": continuation_hash,
            "caption_association": "same_page_spatial_group",
        },
    ]
    parsed_rows = [
        parsed(
            "  Figure   2  ",
            4,
            "01_evidence/figures/anchor.png",
            anchor_hash,
            fragments=grouped,
        ),
        parsed(
            " Figure 1 ",
            1,
            "01_evidence/figures/figure-one.png",
            figure_one_hash,
            fragments=[
                {
                    "page": 2,
                    "block_index": 1,
                    "bbox": [2, 3, 80, 90],
                    "asset_path": "01_evidence/figures/figure-one.png",
                    "asset_sha256": figure_one_hash,
                    "caption_association": "explicit_caption_anchor",
                }
            ],
        ),
    ]

    result = project_source_figure_candidates(
        project,
        [
            {
                "study_id": "study-1",
                "source_id": "source-1",
                "document_role": "MAIN",
                "pdf": {"path": "00_sources/main.pdf", "sha256": source_hash},
            }
        ],
        parsed_rows,
    )

    assert [row["figure_label"] for row in result["figures"]] == [
        "Figure 1",
        "Figure 2",
    ]
    assert result["figures"][1]["caption"] == "Panel A continued caption"
    assert result["figures"][1]["locator"] == {
        "source_mode": "parsed_candidate",
        "page": 2,
        "section_or_item": "Results section",
        "figure_or_table": "Figure 2",
        "exact_quote": "Panel A continued caption",
    }
    assert result["figures"][1]["source_pdf_sha256"] == source_hash
    assert result["figures"][1]["asset_sha256"] == anchor_hash
    assert result["figures"][1]["fragments"] == grouped
    assert result["figures"][1]["release_status"] == "HOLD"


def test_inventory_adapter_surfaces_markdown_license_hint_without_clearing_rights(
    tmp_path: Path,
) -> None:
    project, sources, figures, pdf, _asset = _candidate_inputs(tmp_path, rights_status="unknown")
    markdown = project / "01_evidence" / "mineru" / "markdown" / "main.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text(
        "Copyright 2026 Source Authors.\nLicensed under CC BY 4.0.",
        encoding="utf-8",
    )
    sources[0]["canonical_markdown"] = {
        "path": markdown.relative_to(project).as_posix(),
        "sha256": _sha256(markdown),
        "size_bytes": markdown.stat().st_size,
    }

    result = project_source_figure_candidates(project, sources, figures)
    row = result["figures"][0]

    assert row.get("rights_status", "unknown") == "unknown"
    assert row["release_status"] == "HOLD"
    assert row["reuse_rights_hints"] == {
        "rights_review_status": "license_hint_found",
        "reuse_hint_class": "open_reuse_candidate",
        "license_statement_candidates": [
            "Copyright 2026 Source Authors.",
            "Licensed under CC BY 4.0.",
        ],
        "license_urls": [],
        "instructions": (
            "These are discovery hints only. Verify the article license, the selected figure's "
            "credit line, any third-party exclusion, and whether adaptation is permitted."
        ),
    }
    assert not (project / "03_figures/source_figure_registry.json").exists()
    assert _sha256(pdf) == sources[0]["pdf"]["sha256"]


def test_inventory_adapter_rejects_drifted_markdown_hint_without_writes(
    tmp_path: Path,
) -> None:
    project, sources, figures, _pdf, _asset = _candidate_inputs(tmp_path, rights_status="unknown")
    markdown = project / "01_evidence" / "mineru" / "markdown" / "main.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text("CC BY 4.0", encoding="utf-8")
    sources[0]["canonical_markdown"] = {
        "path": markdown.relative_to(project).as_posix(),
        "sha256": "0" * 64,
        "size_bytes": markdown.stat().st_size,
    }
    before = _project_fingerprint(project)

    with pytest.raises(ReviewFigureError) as error:
        project_source_figure_candidates(project, sources, figures)

    assert error.value.code == "SOURCE_MARKDOWN_HASH_MISMATCH"
    assert _project_fingerprint(project) == before


def test_agent_candidate_is_consumed_by_real_release_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, sources, figures, _pdf, asset = _candidate_inputs(tmp_path)
    image_buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color=(255, 255, 255)).save(image_buffer, format="PNG")
    asset.write_bytes(image_buffer.getvalue())
    figures[0]["asset_sha256"] = _sha256(asset)

    candidate_set = project_source_figure_candidates(project, sources, figures)
    candidate_set["schema_version"] = "review-writer.agent-figure-candidates.v1"
    monkeypatch.setattr(review_figures, "_source_truth_digest", lambda _root: "e" * 64)
    monkeypatch.setattr(review_figures, "_content_list_v2_digest", lambda _root: "f" * 64)
    monkeypatch.setattr(review_figures, "_chemical_paper_bindings", lambda _root: (None, {}))
    registry = review_figures.materialize_source_figure_registry(
        project,
        candidate_set,
        figure_id=candidate_set["figures"][0]["figure_id"],
        selection_status="selected",
    )
    assert (project / "03_figures/source_figure_registry.json").is_file()

    row = registry["figures"][0]
    manuscript = (
        "# Results\n\n"
        f"![Source figure](../{row['asset_path']})\n\n"
        f"Source Figure Attribution: {row['figure_id']} | {row['source_id']} | "
        f"page {row['page']} | {row['figure_label']}\n"
    )
    manuscript_path = project / "04_manuscript/manuscript.md"
    manuscript_path.parent.mkdir(parents=True)
    manuscript_path.write_text(manuscript, encoding="utf-8")
    manuscript_sha256 = hashlib.sha256(manuscript.encode("utf-8")).hexdigest()
    lineage_digest = "b" * 64
    (project / "04_manuscript/manuscript_lineage.v2.json").write_text(
        '{"lineage_digest": "' + lineage_digest + '"}\n', encoding="utf-8"
    )
    VersionContext.create(
        {"currentness": "current"},
        project_id=project.name,
        project_root=project,
    )
    monkeypatch.setattr(
        review_figures,
        "load_source_figure_registry",
        lambda _project: registry,
    )
    monkeypatch.setattr(
        project_release,
        "workflow_state",
        lambda _project: {
            "route": project_release.NEW_ROUTE,
            "parse_ready": True,
            "internal_draft_export_ready": True,
            "workflow_digest": "c" * 64,
        },
    )
    monkeypatch.setattr(
        project_release,
        "manuscript_state",
        lambda _project: {
            "workflow_can_continue": True,
            "manuscript_sha256": manuscript_sha256,
            "lineage_digest": lineage_digest,
        },
    )
    monkeypatch.setattr(project_release, "_authoritative_review_question_binding", lambda *_args: None)
    monkeypatch.setattr(
        project_release,
        "_chemical_paper_release_state",
        lambda *_args: {
            "binding_digest": None,
            "dependency_currentness": {"can_release": True},
        },
    )
    monkeypatch.setattr(project_release, "release_markdown_with_chemical_limitations", lambda text, _state: text)
    monkeypatch.setattr(project_release, "safe_chemical_paper_projection", lambda _state: None)
    monkeypatch.setattr(
        project_release,
        "validate_docx_integrity",
        lambda *_args, **_kwargs: {
            "schema_version": "docx-integrity.v1",
            "zip_valid": True,
            "relationships_valid": True,
            "document_xml_sha256": "d" * 64,
            "media_sha256": [_sha256(asset)],
            "markdown_roundtrip_match": True,
            "attribution_complete": True,
            "workflow_digest_match": True,
            "provenance_valid": True,
            "document_xml_changed": False,
            "media_changed": False,
            "legacy_repackage_only": False,
        },
    )
    monkeypatch.setattr(project_release, "_validate_release_schema", lambda *_args: None)

    released = project_release.build_project_release(project)

    assert released["release_status"] == "SELF_REVIEWED_DRAFT"
    quality = json.loads(
        (project / "05_release/quality_report.json").read_text(encoding="utf-8")
    )
    released_figure = quality["figure_validation"]["source_figures"][0]
    assert released_figure["figure_id"] == row["figure_id"]
    assert released_figure["source_pdf_sha256"] == row["source_pdf_sha256"]
    assert released_figure["page"] == row["page"]
    assert released_figure["asset_sha256"] == row["asset_sha256"]
    assert released_figure["rights_status"] == "cleared"
