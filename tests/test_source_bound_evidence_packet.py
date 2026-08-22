"""Focused source-bound Evidence packet regression tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from review_writer.agent import local_pdf_parse


def test_pdf_only_candidate_builder_keeps_multiple_source_bound_observations(
    tmp_path: Path, monkeypatch
) -> None:
    """Rich parsed text must become multiple page/section-bound candidates."""

    project = tmp_path / "project"
    project.mkdir()
    study_id = "study-1"
    source_id = "source-1"
    source_pdf_sha256 = "a" * 64
    reading = (
        "Abstract\n"
        "We found an oxidative coupling product.\f"
        "Reaction type and product\n"
        "The nickel-catalyzed cross-coupling produced the reported product.\f"
        "Conditions and mechanism\n"
        "The reaction used base B at 80 C; the authors propose a reductive elimination mechanism.\f"
        "Scope and limitations\n"
        "The scope was limited to the reported substrate class. Figure 2 and Table 1 summarize the scope."
    )
    markdown = (
        "<!-- source page 1 -->\n# Abstract\nWe found an oxidative coupling product.\n\n"
        "<!-- source page 2 -->\n## Reaction type and product\n"
        "The nickel-catalyzed cross-coupling produced the reported product.\n\n"
        "<!-- source page 3 -->\n## Conditions and mechanism\n"
        "The reaction used base B at 80 C; the authors propose a reductive elimination mechanism.\n\n"
        "<!-- source page 4 -->\n## Scope and limitations\n"
        "The scope was limited to the reported substrate class. Figure 2 and Table 1 summarize the scope.\n"
    )
    reading_path = project / "reading.txt"
    reading_path.write_text(reading, encoding="utf-8")
    markdown_path = project / "parsed.md"
    markdown_path.write_text(markdown, encoding="utf-8")

    bundle = {
        "project_id": project.name,
        "study_id": study_id,
        "sources": [
            {
                "source_id": source_id,
                "document_role": "MAIN",
                "page_count": 4,
                "pdf": {"sha256": source_pdf_sha256},
                "reading_layer": {
                    "path": "reading.txt",
                    "sha256": "b" * 64,
                    "size_bytes": len(reading.encode("utf-8")),
                },
            }
        ],
    }
    monkeypatch.setattr(
        local_pdf_parse,
        "load_source_truth_bundle",
        lambda _project, _study_id: bundle,
    )
    monkeypatch.setattr(
        local_pdf_parse,
        "_verified_text_descriptor",
        lambda _project, _descriptor: reading,
    )

    @contextmanager
    def snapshots(_project, _study_id, _source_id, kind):
        if kind == "pdf":
            yield SimpleNamespace(sha256=source_pdf_sha256, path=project / "paper.pdf")
        else:
            yield SimpleNamespace(sha256="c" * 64, path=markdown_path)

    monkeypatch.setattr(local_pdf_parse, "source_truth_asset_snapshot", snapshots)

    quality = {
        "objects": [{"object_digest": f"{index:064x}"} for index in range(1, 8)]
    }

    candidates = local_pdf_parse._build_pdf_only_evidence_candidate(
        project, study_id, quality
    )

    assert isinstance(candidates, list)
    assert len(candidates) >= 5
    assert {row["study_id"] for row in candidates} == {study_id}
    assert {row["source_id"] for row in candidates} == {source_id}
    assert {row["source_pdf_sha256"] for row in candidates} == {source_pdf_sha256}
    assert all(row["bound_parse_object_digests"] == sorted(
        item["object_digest"] for item in quality["objects"]
    ) for row in candidates)
    assert all(row["locator"]["exact_quote"] for row in candidates)
    assert {row["locator"]["page"] for row in candidates} >= {2, 3, 4}
    statements = " ".join(row["statement"] for row in candidates)
    assert "cross-coupling" in statements
    assert "reductive elimination" in statements
    assert any(row["reported_conditions"] for row in candidates)
    assert any(row["mechanism_grade"] == "proposal" for row in candidates)
    assert any(len(row["limitations"]) > 1 for row in candidates)
    assert any("Figure 2" in (row["locator"]["figure_or_table"] or "") for row in candidates)
    assert all(row["field_dependencies"] == [] for row in candidates)
