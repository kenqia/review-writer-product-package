"""Focused regression tests for portable source-relative paths."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import pytest

from review_writer.project import source_truth
from view import serve_review_dashboard as dashboard


def test_source_truth_normalizes_windows_separators_for_read_and_manifest_match(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    pdf = project / "00_sources" / "manual_upload" / "main.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"authorized source")
    manifest_path = project / "01_evidence" / "mineru" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "completed": [
                    {
                        "relative_pdf_path": r"manual_upload\main.pdf",
                        "slug": "main",
                        "state": "done",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert source_truth._safe_file(
        project,
        r"00_sources\manual_upload\main.pdf",
    ) == pdf.resolve()
    row = source_truth._unique_mineru_row(
        project,
        "00_sources/manual_upload/main.pdf",
    )
    assert row["slug"] == "main"
    assert source_truth._unique_mineru_row(
        project,
        r"00_sources\manual_upload\main.pdf",
    )["slug"] == "main"


def test_source_truth_rejects_windows_traversal_without_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    for relative in (r"00_sources\..\outside.pdf", r"C:\outside.pdf", r"\\server\share\outside.pdf"):
        with pytest.raises(source_truth.SourceTruthError) as error:
            source_truth._safe_file(project, relative)
        assert error.value.code == "SOURCE_ASSET_INVALID"
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before


def test_dashboard_source_manifest_path_is_posix_for_windows_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        dashboard,
        "_receipt_source_relative_from_project",
        lambda _project, _study_id: PureWindowsPath(r"manual_upload\main.pdf"),
    )

    assert dashboard._source_manifest_relative_pdf_path(tmp_path, "study-1") == (
        "manual_upload/main.pdf"
    )
    assert dashboard._acquisition_source_relative_path(r"C:\outside.pdf") is None
    assert dashboard._acquisition_source_relative_path(r"..\outside.pdf") is None
