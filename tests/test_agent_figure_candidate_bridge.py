"""Focused tests for the staging-only Agent figure candidate bridge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from review_writer.agent import local_pdf_parse
from review_writer.product_foundation import VersionContext
from view import serve_review_dashboard as dashboard


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_staging_figure_bridge_reuses_registry_projection_without_selection(
    tmp_path: Path, monkeypatch
) -> None:
    staged = tmp_path / "staged-project"
    pdf = staged / "00_sources/main.pdf"
    asset = staged / "01_evidence/parses/extracted/main/images/figure-1.png"
    pdf.parent.mkdir(parents=True)
    asset.parent.mkdir(parents=True)
    pdf.write_bytes(b"authorized source pdf")
    asset.write_bytes(b"parsed figure bytes")
    source_pdf_sha256 = _sha256(pdf)
    asset_sha256 = _sha256(asset)
    builder_calls: list[Path] = []

    def fake_registry(project: Path) -> dict[str, object]:
        builder_calls.append(project)
        return {
            "figures": [
                {
                    "figure_id": "study-1:source-1:figure-1",
                    "study_id": "study-1",
                    "source_id": "source-1",
                    "page": 2,
                    "figure_label": "Figure 1",
                    "caption": "Reaction overview.",
                    "source_pdf_sha256": source_pdf_sha256,
                    "fragments": [
                        {
                            "page": 2,
                            "block_index": 0,
                            "bbox": [1, 2, 100, 200],
                            "asset_path": "01_evidence/parses/extracted/main/images/figure-1.png",
                            "asset_sha256": asset_sha256,
                            "caption_association": "explicit_caption_anchor",
                        }
                    ],
                }
            ],
            "locator_gaps": [
                {
                    "study_id": "study-1",
                    "source_id": "source-1",
                    "page": 3,
                    "reason": "unbound image block",
                }
            ],
        }

    monkeypatch.setattr(local_pdf_parse, "build_source_figure_registry", fake_registry)

    result = local_pdf_parse._build_staged_figure_candidates(
        staged,
        [
            {
                "study_id": "study-1",
                "source_id": "source-1",
                "document_role": "MAIN",
                "pdf": {
                    "path": "00_sources/main.pdf",
                    "sha256": source_pdf_sha256,
                },
            }
        ],
        parser_mode="MINERU",
    )

    assert builder_calls == [staged]
    assert result["status"] == "candidate"
    assert result["figures"][0]["selection_status"] == "available"
    assert result["figures"][0]["release_status"] == "HOLD"
    assert result["figures"][0]["source_pdf_sha256"] == source_pdf_sha256
    assert result["figures"][0]["asset_sha256"] == asset_sha256
    assert result["figures"][0]["fragments"][0]["asset_path"].endswith(
        "main/images/figure-1.png"
    )
    assert result["gaps"] == [
        {
            "study_id": "study-1",
            "source_id": "source-1",
            "page": 3,
            "reason": "unbound image block",
        },
        {
            "figure_id": "study-1:source-1:figure-1",
            "code": "FIGURE_RIGHTS_UNKNOWN",
            "reason": "source figure rights require human confirmation",
        },
    ]
    assert not (staged / "03_figures/source_figure_registry.json").exists()


def test_fallback_figure_bridge_reports_truthful_gap_when_no_images_exist(
    tmp_path: Path, monkeypatch
) -> None:
    staged = tmp_path / "staged-project"
    pdf = staged / "00_sources/main.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"authorized source pdf")
    source_pdf_sha256 = _sha256(pdf)

    monkeypatch.setattr(
        local_pdf_parse,
        "build_source_figure_registry",
        lambda _project: {"figures": [], "locator_gaps": []},
    )

    result = local_pdf_parse._build_staged_figure_candidates(
        staged,
        [
            {
                "study_id": "study-1",
                "source_id": "source-1",
                "document_role": "MAIN",
                "pdf": {"path": "00_sources/main.pdf", "sha256": source_pdf_sha256},
            }
        ],
        parser_mode="FALLBACK",
    )

    assert result["status"] == "gap"
    assert result["figures"] == []
    assert result["gaps"] == [
        {
            "code": "FIGURE_ASSET_UNAVAILABLE",
            "reason": (
                "fallback parser produced no extracted image assets; "
                "source figure candidates remain unavailable until a visual parser run."
            ),
            "sources": [
                {
                    "study_id": "study-1",
                    "source_id": "source-1",
                    "source_pdf_sha256": source_pdf_sha256,
                }
            ],
        }
    ]


def test_dashboard_projects_agent_candidates_when_registry_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "review-project"
    project.mkdir()
    candidate = {
        "figure_id": "study-1:source-1:figure-1",
        "study_id": "study-1",
        "source_id": "source-1",
        "page": 2,
        "figure_label": "Figure 1",
        "caption": "Reaction overview.",
        "selection_status": "available",
        "release_status": "HOLD",
        "asset_sha256": "b" * 64,
        "source_pdf_sha256": "a" * 64,
        "fragments": [],
    }
    snapshot = {
        "agent_parse": {
            "figure_candidates": {
                "schema_version": "review-writer.agent-figure-candidates.v1",
                "status": "candidate",
                "figures": [candidate],
                "gaps": [
                    {
                        "code": "FIGURE_RIGHTS_UNKNOWN",
                        "reason": "source figure rights require human confirmation",
                    }
                ],
            }
        }
    }

    class _Context:
        def state(self) -> object:
            return SimpleNamespace(current_version_id="v1")

        def view_version(self, _version_id: str) -> object:
            return SimpleNamespace(snapshot=snapshot)

    monkeypatch.setattr(
        dashboard,
        "workflow_state",
        lambda _project: {"route": "evidence-to-release.v1"},
    )
    monkeypatch.setattr(
        dashboard,
        "current_manuscript_target_projection",
        lambda _project: {},
    )
    monkeypatch.setattr(dashboard, "synthesis_figure_placeholders", lambda _project: [])
    monkeypatch.setattr(dashboard.VersionContext, "load", lambda _project: _Context())

    result = dashboard.project_review_figures_workspace_payload(
        tmp_path, project.name
    )

    assert result["status"] == "candidate_only"
    assert result["source_figures"][0]["figure_id"] == candidate["figure_id"]
    assert result["source_figures"][0]["release_status"] == "HOLD"
    assert result["source_figures"][0]["candidate_only"] is True
    assert result["source_figures"][0]["image_url"] is None
    assert result["summary"]["selected_count"] == 0
    assert result["locator_gaps"] == [
        {
            "study_id": "",
            "page": None,
            "reason": "source figure rights require human confirmation",
        }
    ]


def test_parse_persists_figure_candidates_in_same_version_context_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "parse-project"
    (project / "00_brief").mkdir(parents=True)
    (project / "00_brief/review_state.json").write_text(
        json.dumps({"project_id": project.name}), encoding="utf-8"
    )
    (project / ".paper_evidence.lock").write_bytes(b"lock")
    pdf = project / "00_sources/main.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"authorized source pdf")
    pdf_sha256 = _sha256(pdf)
    (project / "00_sources/acquisition_final_receipt.json").write_text(
        json.dumps(
            {
                "studies": [
                    {
                        "study_id": "study-1",
                        "source_id": "source-1",
                        "main_pdf": {"path": "main.pdf", "sha256": pdf_sha256},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    VersionContext.create(
        {"currentness": "current"},
        project_id=project.name,
        project_root=project,
    )
    candidates = {
        "schema_version": "review-writer.agent-figure-candidates.v1",
        "project_id": project.name,
        "status": "gap",
        "parser_mode": "FALLBACK",
        "figures": [],
        "gaps": [{"code": "FIGURE_ASSET_UNAVAILABLE", "reason": "no images"}],
    }
    monkeypatch.setattr(
        local_pdf_parse,
        "_write_mineru_parse_output",
        lambda _evidence, _rows: (
            [
                {
                    "slug": "main",
                    "state": "done",
                    "study_id": "study-1",
                    "source_id": "source-1",
                    "document_role": "MAIN",
                    "relative_pdf_path": "main.pdf",
                    "source_pdf_sha256": pdf_sha256,
                }
            ],
            [{"source_id": "source-1", "source_pdf_sha256": pdf_sha256}],
        ),
    )
    monkeypatch.setattr(
        local_pdf_parse,
        "write_source_truth_bundle",
        lambda _project, _study_id: {
            "study_id": "study-1",
            "bundle_digest": "b" * 64,
            "sources": [
                {
                    "source_id": "source-1",
                    "document_role": "MAIN",
                    "pdf": {"path": "00_sources/main.pdf", "sha256": pdf_sha256},
                }
            ],
        },
    )
    monkeypatch.setattr(
        local_pdf_parse,
        "write_parse_quality_gate",
        lambda _project, _study_id: {
            "study_id": "study-1",
            "gate_digest": "g" * 64,
            "status": "needs_review",
        },
    )
    monkeypatch.setattr(
        local_pdf_parse,
        "_build_staged_figure_candidates",
        lambda staged_project, _sources, *, parser_mode: (
            assert_staging_path(staged_project, project),
            candidates,
        )[1],
    )
    monkeypatch.setattr(local_pdf_parse, "_publish_components", lambda *_args: None)

    result = local_pdf_parse.parse_project_sources(project)

    assert result["figure_candidates"] == candidates
    current = VersionContext.load(project).view_version(
        VersionContext.load(project).state().current_version_id
    )
    assert current.snapshot["agent_parse"]["figure_candidates"] == candidates
    assert current.snapshot["agent_parse"]["tool_trace"][-1]["tool"] == (
        "build_source_figure_registry"
    )
    assert not (project / "03_figures/source_figure_registry.json").exists()


def assert_staging_path(staged_project: Path, canonical: Path) -> None:
    assert staged_project != canonical
    assert staged_project.parent != canonical.parent
