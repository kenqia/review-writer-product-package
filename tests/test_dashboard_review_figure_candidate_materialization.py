"""Focused tests for human materialization of candidate-only source figures."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_writer.agent import local_pdf_parse  # noqa: F401 - initialize agent package before Dashboard
from review_writer.project import review_figures
from view import serve_review_dashboard as dashboard


def _candidate(
    project: Path,
    figure_id: str,
    *,
    rights_status: str | None = None,
) -> dict[str, object]:
    asset = project / f"01_evidence/{figure_id}.png"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(figure_id.encode("utf-8"))
    asset_sha256 = hashlib.sha256(asset.read_bytes()).hexdigest()
    row: dict[str, object] = {
        "figure_id": figure_id,
        "study_id": "study-1",
        "source_id": "source-1",
        "page": 2,
        "figure_label": "Figure 1",
        "caption": "Reaction overview.",
        "asset_path": asset.relative_to(project).as_posix(),
        "asset_sha256": asset_sha256,
        "source_pdf_sha256": "a" * 64,
        "evidence_ids": [],
        "selection_status": "available",
        "fragments": [
            {
                "page": 2,
                "block_index": 0,
                "bbox": [1, 2, 10, 20],
                "asset_path": asset.relative_to(project).as_posix(),
                "asset_sha256": asset_sha256,
                "caption_association": "explicit_caption_anchor",
            }
        ],
        "status": "candidate",
        "locator": {
            "source_mode": "parsed_candidate",
            "page": 2,
            "section_or_item": "page 2",
            "figure_or_table": "Figure 1",
            "exact_quote": "Reaction overview.",
        },
    }
    if rights_status == "cleared":
        row.update(
            {
                "rights_status": "cleared",
                "license_or_rights_basis": "CC BY 4.0",
                "attribution": "Source Figure Attribution: study-1:source-1:figure-1",
                "rights_evidence_reference": "rights-record-1",
            }
        )
    return row


def _candidate_set(project: Path) -> dict[str, object]:
    return {
        "schema_version": "review-writer.agent-figure-candidates.v1",
        "project_id": project.name,
        "status": "candidate",
        "figures": [
            _candidate(project, "study-1:source-1:figure-1", rights_status="cleared"),
            _candidate(project, "study-1:source-1:figure-2"),
        ],
        "gaps": [],
    }


class _Context:
    def __init__(self, project: Path, snapshot: dict[str, object]) -> None:
        self.project = project
        self.snapshot = snapshot
        self.published: list[dict[str, object]] = []

    def state(self) -> SimpleNamespace:
        return SimpleNamespace(
            project_id=self.project.name,
            current_version_id="v1",
            active_head_id="v1",
            revision=3,
        )

    def view_version(self, _version_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            version_id="v1",
            snapshot=self.snapshot,
            snapshot_digest="b" * 64,
            is_current=True,
            is_active_head=True,
            can_write=True,
        )

    def publish_active_head(self, snapshot: dict[str, object], **kwargs: object) -> SimpleNamespace:
        self.published.append({"snapshot": snapshot, **kwargs})
        self.snapshot = snapshot
        return SimpleNamespace(version_id="v2", snapshot_digest="c" * 64)


def _patch_dashboard_project(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    project: Path,
    candidate_set: dict[str, object],
) -> tuple[_Context, dict[str, object]]:
    snapshot = {
        "currentness": "current",
        "agent_parse": {"figure_candidates": candidate_set},
    }
    context = _Context(project, snapshot)
    monkeypatch.setattr(dashboard, "VersionContext", SimpleNamespace(load=lambda _project: context))
    monkeypatch.setattr(dashboard, "project_dir", lambda _root, _project_id: project)
    monkeypatch.setattr(dashboard, "workflow_state", lambda _project: {"route": "evidence-to-release.v1"})
    monkeypatch.setattr(dashboard, "current_manuscript_target_projection", lambda _project: {"sha256": "", "sections": []})
    monkeypatch.setattr(dashboard, "synthesis_figure_placeholders", lambda _project: [])
    monkeypatch.setattr(dashboard, "project_write_lock", lambda _project: nullcontext())
    return context, snapshot


def test_candidate_only_get_token_and_human_materialization_publish_version_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "review-project"
    project.mkdir()
    candidate_set = _candidate_set(project)
    context, _snapshot = _patch_dashboard_project(monkeypatch, tmp_path, project, candidate_set)
    monkeypatch.setattr(
        dashboard,
        "materialize_source_figure_registry",
        lambda explicit_project, candidates, *, figure_id, selection_status: {
            "registry_digest": "d" * 64,
            "project_id": explicit_project.name,
            "figures": candidates["figures"],
            "figure_id": figure_id,
            "selection_status": selection_status,
        },
        raising=False,
    )
    visible = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)
    selected = visible["source_figures"][0]
    token = selected["version_token"]
    assert isinstance(token, str) and token

    monkeypatch.setattr(
        dashboard,
        "project_review_figures_workspace_payload",
        lambda _root, _project_id: {"status": "candidate_only"},
    )

    result = dashboard.write_project_workspace_decision(
        tmp_path,
        project.name,
        "review-figures",
        {
            "figure_id": selected["figure_id"],
            "selection_status": "selected",
            "version_token": token,
            "actor_type": "human_researcher",
            "actor_label": "Researcher",
        },
    )

    assert result == {"status": "candidate_only"}
    assert context.published
    published = context.published[-1]
    assert published["expected_head_id"] == "v1"
    assert published["expected_revision"] == 3
    assert published["snapshot"]["review_figures"]["registry_digest"] == "d" * 64
    assert published["snapshot"]["review_figures"]["decision"]["actor_type"] == "human_researcher"


def test_candidate_only_human_rights_overlay_clears_only_selected_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "review-project"
    project.mkdir()
    candidate_set = _candidate_set(project)
    context, _snapshot = _patch_dashboard_project(monkeypatch, tmp_path, project, candidate_set)
    monkeypatch.setattr(review_figures, "_source_truth_digest", lambda _root: "e" * 64)
    monkeypatch.setattr(review_figures, "_content_list_v2_digest", lambda _root: "f" * 64)
    monkeypatch.setattr(review_figures, "_chemical_paper_bindings", lambda _root: ("0" * 64, {}))

    visible = dashboard.project_review_figures_workspace_payload(tmp_path, project.name)
    selected = visible["source_figures"][1]
    result = dashboard.write_project_workspace_decision(
        tmp_path,
        project.name,
        "review-figures",
        {
            "figure_id": selected["figure_id"],
            "selection_status": "selected",
            "version_token": selected["version_token"],
            "actor_type": "human_researcher",
            "actor_label": "Researcher",
            "rights_status": "cleared",
            "license_or_rights_basis": "CC BY 4.0",
            "attribution": "Source Figure Attribution: study-1:source-1:figure-2",
            "rights_evidence_reference": "rights-record-2",
        },
    )

    assert result["status"] == "current"
    registry = json.loads(
        (project / "03_figures/source_figure_registry.json").read_text(encoding="utf-8")
    )
    target = next(row for row in registry["figures"] if row["figure_id"] == selected["figure_id"])
    other = next(row for row in registry["figures"] if row["figure_id"] != selected["figure_id"])
    assert target["selection_status"] == "selected"
    assert target["rights_status"] == "cleared"
    assert target["rights_license"] == "CC BY 4.0"
    assert target["rights_evidence_reference"] == "rights-record-2"
    assert other["selection_status"] == "available"
    assert other["rights_status"] == "cleared"
    assert context.published[-1]["snapshot"]["review_figures"]["decision"]["rights_status"] == "cleared"


def test_materialize_candidate_preserves_rights_and_rejects_unknown_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "review-project"
    project.mkdir()
    monkeypatch.setattr(review_figures, "_source_truth_digest", lambda _root: "e" * 64)
    monkeypatch.setattr(review_figures, "_content_list_v2_digest", lambda _root: "f" * 64)
    monkeypatch.setattr(review_figures, "_chemical_paper_bindings", lambda _root: ("0" * 64, {}))
    candidates = _candidate_set(project)

    registry = review_figures.materialize_source_figure_registry(
        project,
        candidates,
        figure_id="study-1:source-1:figure-1",
        selection_status="selected",
    )

    selected = next(row for row in registry["figures"] if row["selection_status"] == "selected")
    assert selected["rights_status"] == "cleared"
    assert selected["rights_license"] == "CC BY 4.0"
    assert selected["rights_evidence_reference"] == "rights-record-1"
    assert all(row["selection_status"] == "available" for row in registry["figures"] if row is not selected)
    assert "license_or_rights_basis" not in selected
    assert "status" not in selected

    unknown = dict(candidates)
    unknown["figures"] = [candidates["figures"][1]]
    with pytest.raises(review_figures.ReviewFigureError) as error:
        review_figures.materialize_source_figure_registry(
            project,
            unknown,
            figure_id="study-1:source-1:figure-2",
            selection_status="selected",
        )
    assert error.value.code == "FIGURE_RIGHTS_NOT_CLEARED"


def test_candidate_only_stale_token_is_zero_write_and_existing_registry_uses_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "review-project"
    project.mkdir()
    candidate_set = _candidate_set(project)
    _context, _snapshot = _patch_dashboard_project(monkeypatch, tmp_path, project, candidate_set)
    monkeypatch.setattr(dashboard, "project_review_figures_workspace_payload", lambda *_args: {})
    monkeypatch.setattr(dashboard, "materialize_source_figure_registry", lambda *_args, **_kwargs: pytest.fail("stale token must not materialize"), raising=False)
    stale = {"figure_id": "study-1:source-1:figure-1", "selection_status": "selected", "version_token": "stale"}
    with pytest.raises(dashboard.WorkspaceStaleError):
        dashboard.write_project_workspace_decision(tmp_path, project.name, "review-figures", stale)
    assert not (project / "03_figures/source_figure_registry.json").exists()

    registry_path = project / "03_figures/source_figure_registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps({"existing": True}), encoding="utf-8")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(dashboard, "write_source_figure_selection", lambda _project, **kwargs: calls.append(kwargs) or {"status": "current"})
    result = dashboard.write_project_workspace_decision(
        tmp_path,
        project.name,
        "review-figures",
        {"figure_id": "figure-1", "selection_status": "available", "version_token": "current"},
    )
    assert result == {"status": "current"}
    assert calls and calls[0]["figure_id"] == "figure-1"
