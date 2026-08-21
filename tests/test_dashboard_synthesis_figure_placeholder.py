"""Focused public seam tests for text-only synthesis figure placeholders."""

from __future__ import annotations

import json
from pathlib import Path

from review_writer.agent import local_pdf_parse  # noqa: F401 - initialize Dashboard imports
from review_writer.product_foundation import ProductFoundationError, VersionContext
from review_writer.project.review_figures import source_figure_registry_digest
from view.serve_review_dashboard import ReviewFigureError, WorkspaceStaleError
from view import serve_review_dashboard as dashboard


def _project(tmp_path: Path, snapshot: dict[str, object] | None = None) -> Path:
    project = tmp_path / "review-project"
    project.mkdir()
    VersionContext.create(
        snapshot or {"currentness": "current"},
        project_id=project.name,
        project_root=project,
    )
    return project


def _empty_agent_figure_candidates(project: Path) -> dict[str, object]:
    return {
        "schema_version": "review-writer.agent-figure-candidates.v1",
        "project_id": project.name,
        "status": "gap",
        "parser_mode": "FALLBACK",
        "figures": [],
        "gaps": [{"code": "FIGURE_ASSET_UNAVAILABLE", "reason": "no images"}],
    }


def _patch_empty_registry_builder(monkeypatch, calls: list[Path]) -> dict[str, object]:
    registry = {
        "schema_version": "review-writer-source-figure-registry.v1",
        "project_id": "review-project",
        "figure_policy": "source_figures_or_synthesis_placeholders_only",
        "figures": [],
        "selected_count": 0,
        "available_count": 0,
        "required_count": 5,
        "target_figure_slots": {"minimum": 5, "maximum": 8},
        "source_truth_digest": "a" * 64,
        "content_list_v2_digest": "b" * 64,
        "chemical_paper_project_binding_digest": None,
        "locator_gaps": [],
        "figure_budget": {
            "status": "needs_human_selection",
            "selected_count": 0,
            "required_count": 5,
            "minimum": 5,
            "maximum": 8,
            "gaps": [
                "Select 5 additional non-duplicative source figure(s) or register a synthesis placeholder."
            ],
        },
    }
    registry["registry_digest"] = source_figure_registry_digest(registry)

    def build(project: Path) -> dict[str, object]:
        calls.append(project)
        path = project / "03_figures/source_figure_registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(registry, sort_keys=True) + "\n", encoding="utf-8")
        return dict(registry)

    monkeypatch.setattr(dashboard, "build_source_figure_registry", build, raising=False)
    return registry


def _patch_project(monkeypatch, project: Path) -> None:
    monkeypatch.setattr(dashboard, "project_dir", lambda _root, _project_id: project)
    monkeypatch.setattr(
        dashboard,
        "_dashboard_route",
        lambda _project: "evidence-to-release.v1",
    )
    monkeypatch.setattr(
        dashboard,
        "current_manuscript_target_projection",
        lambda _project: {"sha256": "", "sections": []},
    )


def test_valid_registration_publishes_placeholder_metadata_in_current_version(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(
        tmp_path,
        {
            "currentness": "current",
            "agent_parse": {
                "figure_candidates": _empty_agent_figure_candidates(Path("review-project"))
            },
        },
    )
    _patch_project(monkeypatch, project)
    builder_calls: list[Path] = []
    registry = _patch_empty_registry_builder(monkeypatch, builder_calls)
    visible = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)
    registration = visible["placeholder_registration"]

    result = dashboard.write_project_workspace_decision(
        tmp_path,
        project.name,
        "review-figures",
        {
            "action": "register_placeholder",
            "placeholder": registration["placeholder"],
            "version_token": registration["version_token"],
            "actor_type": "human_researcher",
            "actor_label": "Researcher",
        },
    )

    assert result["summary"]["placeholder_count"] == 1
    assert result["placeholder_registration"] is None
    assert builder_calls == [project]
    placeholder_path = project / "03_figures/synthesis_figure_placeholders.json"
    assert placeholder_path.is_file()
    registry_path = project / "03_figures/source_figure_registry.json"
    assert registry_path.is_file()
    context = VersionContext.load(project)
    state = context.state()
    assert state.current_version_id != "v1"
    assert state.revision == 1
    current = context.view_version(state.current_version_id)
    review_figures = current.snapshot["review_figures"]
    assert review_figures["placeholder_id"] == "synthesis-figure-1"
    assert review_figures["placeholder_registry_digest"]
    assert review_figures["registry_digest"] == registry["registry_digest"]
    assert review_figures["decision"]["actor_type"] == "human_researcher"


def test_malformed_placeholder_is_rejected_without_writing_or_advancing_current(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    _patch_project(monkeypatch, project)
    visible = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)
    registration = visible["placeholder_registration"]
    malformed = dict(registration["placeholder"])
    malformed.pop("caption_draft")

    try:
        dashboard.write_project_workspace_decision(
            tmp_path,
            project.name,
            "review-figures",
            {
                "action": "register_placeholder",
                "placeholder": malformed,
                "version_token": registration["version_token"],
                "actor_type": "human_researcher",
                "actor_label": "Researcher",
            },
        )
    except ReviewFigureError as exc:
        assert exc.code == "PLACEHOLDER_INVALID"
    else:
        raise AssertionError("malformed placeholder must be rejected")

    assert not (project / "03_figures/synthesis_figure_placeholders.json").exists()
    assert VersionContext.load(project).state().revision == 0


def test_non_researcher_actor_is_rejected_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    _patch_project(monkeypatch, project)
    visible = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)
    registration = visible["placeholder_registration"]

    try:
        dashboard.write_project_workspace_decision(
            tmp_path,
            project.name,
            "review-figures",
            {
                "action": "register_placeholder",
                "placeholder": registration["placeholder"],
                "version_token": registration["version_token"],
                "actor_type": "simulated_researcher_agent",
                "actor_label": "automation",
            },
        )
    except ReviewFigureError as exc:
        assert exc.code == "FIGURE_ACTOR_INVALID"
    else:
        raise AssertionError("non-researcher actor must be rejected")

    assert not (project / "03_figures/synthesis_figure_placeholders.json").exists()
    assert VersionContext.load(project).state().revision == 0


def test_stale_registration_token_is_zero_write(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    _patch_project(monkeypatch, project)
    visible = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)
    registration = visible["placeholder_registration"]

    try:
        dashboard.write_project_workspace_decision(
            tmp_path,
            project.name,
            "review-figures",
            {
                "action": "register_placeholder",
                "placeholder": registration["placeholder"],
                "version_token": "stale-token",
                "actor_type": "human_researcher",
                "actor_label": "Researcher",
            },
        )
    except WorkspaceStaleError:
        pass
    else:
        raise AssertionError("stale registration token must be rejected")

    assert not (project / "03_figures/synthesis_figure_placeholders.json").exists()
    assert VersionContext.load(project).state().revision == 0


def test_publish_failure_restores_placeholder_file_and_current_version(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(
        tmp_path,
        {
            "currentness": "current",
            "agent_parse": {
                "figure_candidates": _empty_agent_figure_candidates(Path("review-project"))
            },
        },
    )
    _patch_project(monkeypatch, project)
    builder_calls: list[Path] = []
    _patch_empty_registry_builder(monkeypatch, builder_calls)
    visible = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)
    registration = visible["placeholder_registration"]

    def fail_publish(self, snapshot, **kwargs):
        raise ProductFoundationError("publish failed")

    monkeypatch.setattr(VersionContext, "publish_active_head", fail_publish)
    try:
        dashboard.write_project_workspace_decision(
            tmp_path,
            project.name,
            "review-figures",
            {
                "action": "register_placeholder",
                "placeholder": registration["placeholder"],
                "version_token": registration["version_token"],
                "actor_type": "human_researcher",
                "actor_label": "Researcher",
            },
        )
    except WorkspaceStaleError:
        pass
    else:
        raise AssertionError("publish failure must stop registration")

    assert not (project / "03_figures/synthesis_figure_placeholders.json").exists()
    assert not (project / "03_figures/source_figure_registry.json").exists()
    assert builder_calls == [project]
    assert VersionContext.load(project).state().revision == 0


def test_unbuilt_workspace_exposes_version_bound_placeholder_registration_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    _patch_project(monkeypatch, project)

    payload = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)

    registration = payload["placeholder_registration"]
    placeholder = registration["placeholder"]
    assert placeholder["status"] == "awaiting_human_figure"
    assert placeholder["placeholder_id"] == "synthesis-figure-1"
    assert registration["version_id"] == "v1"
    assert registration["revision"] == 0
    assert registration["snapshot_digest"]
    assert registration["version_token"]
    assert registration["next_action"] == "HUMAN_ACTION_REQUIRED"
