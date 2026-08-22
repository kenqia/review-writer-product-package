"""Focused contract tests for the optional Chemical Paper enhancement route."""

from __future__ import annotations

from pathlib import Path

import pytest

from review_writer.delivery import dual_parse_release
from review_writer.project import chemical_completion, parse_reconciliation
from review_writer.project import dual_source, paper_evidence, workflow_projection
from review_writer.delivery import dual_parse_release
from view import serve_review_dashboard


def test_core_dual_binding_is_generic_only_without_chemical_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "generic-only"
    project.mkdir()
    source_pdf_sha256 = "a" * 64
    monkeypatch.setattr(dual_source, "study_source_tier", lambda *_: "core")
    monkeypatch.setattr(
        dual_source,
        "load_source_truth_bundle",
        lambda *_: {
            "bundle_digest": "b" * 64,
            "sources": [
                {
                    "source_id": "source-1",
                    "document_role": "MAIN",
                    "pdf": {"sha256": source_pdf_sha256},
                }
            ],
        },
    )
    monkeypatch.setattr(dual_source, "require_parse_quality_current", lambda *_: "c" * 64)
    monkeypatch.setattr(
        dual_source,
        "chemical_paper_current_binding",
        lambda *_: (_ for _ in ()).throw(
            dual_source.ChemicalPaperError("CHEMICAL_PAPER_NOT_IMPORTED")
        ),
    )

    binding = dual_source.build_dual_source_binding(project, "study-1")

    assert binding["status"] == "current_generic_only"
    assert binding["chemical"] is None


def test_generic_evidence_does_not_upgrade_core_study_to_chemical_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[bool] = []
    monkeypatch.setattr(paper_evidence, "study_source_tier", lambda *_: "core")

    def require_binding(*_args: object, requires_chemical: bool) -> str:
        observed.append(requires_chemical)
        return "d" * 64

    monkeypatch.setattr(paper_evidence, "require_dual_source_ready", require_binding)
    monkeypatch.setattr(
        paper_evidence,
        "require_honest_progressive_projection",
        lambda *_args, **_kwargs: "e" * 64,
    )
    monkeypatch.setattr(
        paper_evidence,
        "require_reconciliation_ready",
        lambda *_args, **_kwargs: "f" * 64,
    )

    bindings = paper_evidence.require_dual_evidence_ready(
        tmp_path, "study-1", requires_chemical=False
    )

    assert observed == [False]
    assert bindings == {
        "dual_source_binding_digest": "d" * 64,
        "chemical_completion_digest": None,
        "honest_progressive_digest": None,
        "reconciliation_digest": None,
    }


def test_workflow_accepts_core_generic_only_projection() -> None:
    state = {
        "schema_version": "dual-source-project-state.v1",
        "studies": [
            {
                "study_id": "study-1",
                "source_tier": "core",
                "requires_chemical": False,
                "pdf_status": "verified",
                "generic_parse_status": "current",
                "status": "current_generic_only",
                "generic": {"status": "current", "binding_digest": "a" * 64},
                "chemical": None,
                "binding_digest": "b" * 64,
                "reaction_data_status": "unavailable_not_provided",
            }
        ],
        "main_source_available_count": 1,
        "generic_source_available_count": 1,
        "workflow_can_continue": True,
    }

    assert workflow_projection._validated_precomputed_dual_state(state, ["study-1"]) == state


def test_chemical_route_is_inactive_until_exact_chemical_dependency_is_registered(
    tmp_path: Path,
) -> None:
    project = tmp_path / "route-selection"
    project.mkdir()

    assert workflow_projection._chemical_route_requested(project, ["study-1"]) is False

    generic_binding_dir = project / "01_evidence" / "dual_source" / "study-1"
    generic_binding_dir.mkdir(parents=True)
    (generic_binding_dir / "binding.json").write_text(
        '{"status":"current_generic_only","chemical":null}\n',
        encoding="utf-8",
    )
    assert workflow_projection._chemical_route_requested(project, ["study-1"]) is False

    candidate_dir = project / "01_evidence" / "study-1"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "paper_evidence_candidates.json").write_text(
        '{"candidates":[{"field_dependencies":["smiles"]}]}\n',
        encoding="utf-8",
    )

    assert workflow_projection._chemical_route_requested(project, ["study-1"]) is True


@pytest.mark.parametrize(
    "dependency",
    ["reaction-structure", "reaction_structure", "chemical_structure"],
)
def test_chemical_route_accepts_supported_structure_dependency_aliases(
    tmp_path: Path, dependency: str
) -> None:
    project = tmp_path / dependency.replace("-", "_")
    candidate_dir = project / "01_evidence" / "study-1"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "paper_evidence_candidates.json").write_text(
        f'{{"candidates":[{{"field_dependencies":["{dependency}"]}}]}}\n',
        encoding="utf-8",
    )

    assert workflow_projection._chemical_route_requested(project, ["study-1"]) is True


def test_default_stage_presentation_omits_optional_chemical_stages() -> None:
    kwargs = {
        "sources_complete": False,
        "parsing_complete": False,
        "evidence_complete": False,
        "draft_complete": False,
        "final_complete": False,
    }

    generic = serve_review_dashboard._new_route_stage_definitions(
        {"dual_route": False}, **kwargs
    )
    chemical = serve_review_dashboard._new_route_stage_definitions(
        {
            "dual_route": True,
            "dual_source_ready": False,
            "chemical_completion_ready": False,
            "reconciliation_ready": False,
        },
        **kwargs,
    )

    assert [stage_id for stage_id, _, _ in generic] == [
        "sources",
        "parsing",
        "evidence",
        "synthesis",
        "drafting",
        "final",
    ]
    assert [stage_id for stage_id, _, _ in chemical][2:5] == [
        "chemical_import",
        "chemical_completion",
        "reconciliation",
    ]
    assert generic[2][1] == "提取并核对逐研究证据"


def test_generic_authority_does_not_load_chemical_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generic_state = {
        "schema_version": "dual-source-project-state.v1",
        "studies": [
            {
                "study_id": "study-1",
                "source_tier": "core",
                "requires_chemical": False,
                "generic_parse_status": "current",
                "generic": {"status": "current", "binding_digest": "a" * 64},
                "binding_digest": "b" * 64,
                "chemical": None,
                "reaction_data_status": "unavailable_not_provided",
            }
        ],
    }
    monkeypatch.setattr(dual_parse_release, "declared_study_ids", lambda *_: ["study-1"])
    monkeypatch.setattr(
        dual_parse_release,
        "project_dual_source_state",
        lambda *_: generic_state,
        raising=False,
    )
    monkeypatch.setattr(
        dual_source,
        "project_dual_source_state",
        lambda *_: generic_state,
    )
    monkeypatch.setattr(
        dual_source,
        "_chemical_route_requested",
        lambda *_: False,
    )
    from review_writer.project import chemical_completion, parse_reconciliation

    monkeypatch.setattr(
        chemical_completion,
        "project_chemical_completion_state",
        lambda *_: pytest.fail("Generic-only authority must not read Chemical Completion"),
    )
    monkeypatch.setattr(
        parse_reconciliation,
        "project_reconciliation_state",
        lambda *_: pytest.fail("Generic-only authority must not read reconciliation"),
    )

    rows = dual_parse_release._current_authority_rows(tmp_path)

    assert rows[0]["requires_chemical"] is False
    assert rows[0]["chemical_version"] is None
    assert rows[0]["chemical_completion_digest"] is None


def test_generic_only_manuscript_binding_skips_chemical_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Generic-only binding must not require Chemical marker state."""
    project = tmp_path / "generic-only"
    project.mkdir()
    dual_state = {
        "schema_version": "dual-source-project-state.v1",
        "studies": [
            {
                "study_id": "study-1",
                "source_tier": "core",
                "requires_chemical": False,
                "pdf_status": "verified",
                "generic_parse_status": "current",
                "status": "current_generic_only",
                "generic": {"status": "current", "binding_digest": "c" * 64},
                "chemical": None,
                "binding_digest": "b" * 64,
                "reaction_data_status": "unavailable_not_provided",
            }
        ],
        "main_source_available_count": 1,
        "generic_source_available_count": 1,
        "workflow_can_continue": True,
    }
    monkeypatch.setattr(dual_source, "project_dual_source_state", lambda *_: dual_state)

    def chemical_projection_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Generic-only manuscript binding invoked Chemical state")

    monkeypatch.setattr(
        chemical_completion,
        "project_chemical_completion_state",
        chemical_projection_must_not_run,
    )
    monkeypatch.setattr(
        parse_reconciliation,
        "project_reconciliation_state",
        chemical_projection_must_not_run,
    )

    assert dual_parse_release.dual_parse_manuscript_bindings(
        project, {"study-1"}
    ) == [
        {
            "study_id": "study-1",
            "source_tier": "core",
            "requires_chemical": False,
            "dual_source_binding_digest": "b" * 64,
            "generic_version": "c" * 64,
            "chemical_version": None,
            "chemical_completion_digest": None,
            "reconciliation_digest": None,
        }
    ]
