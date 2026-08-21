from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

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
