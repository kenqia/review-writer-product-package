"""Focused release/version-context binding contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from review_writer.delivery import project_release
from review_writer.product_foundation import VersionContext


def _tree_fingerprint(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _version_binding(project: Path) -> dict[str, object]:
    context = VersionContext.load(project)
    state = context.state()
    current = context.view_version(state.current_version_id)
    return {
        "version_context_version_id": current.version_id,
        "version_context_revision": state.revision,
        "version_context_digest": current.snapshot_digest,
    }


def _patch_readiness_gates(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lineage_digest: str,
    manuscript_sha256: str,
    workflow_digest: str,
) -> dict[str, object]:
    figure_validation = {
        "schema_version": "review-writer-figure-validation.v2",
        "release_level": "SELF_REVIEWED_DRAFT",
        "source_figure_registry_digest": "a" * 64,
        "source_figures": [],
        "human_synthesis_figures": [],
        "required_attributions": [],
        "expected_media_sha256": [],
        "placeholder_count": 0,
        "pending_placeholder_count": 0,
    }
    integrity = {"fixture": True}
    monkeypatch.setattr(
        project_release,
        "workflow_state",
        lambda _project: {
            "route": project_release.NEW_ROUTE,
            "internal_draft_export_ready": True,
            "workflow_digest": workflow_digest,
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
    monkeypatch.setattr(
        project_release,
        "_new_route_figure_state",
        lambda *_args, **_kwargs: figure_validation,
    )
    monkeypatch.setattr(
        project_release,
        "_chemical_paper_release_state",
        lambda *_args, **_kwargs: {
            "binding_digest": None,
            "dependency_currentness": {"can_release": True},
        },
    )
    monkeypatch.setattr(
        project_release,
        "release_markdown_with_chemical_limitations",
        lambda markdown, _chemical: markdown,
    )
    monkeypatch.setattr(
        project_release,
        "safe_chemical_paper_projection",
        lambda _chemical: None,
    )
    monkeypatch.setattr(
        project_release,
        "validate_docx_integrity",
        lambda *_args, **_kwargs: integrity,
    )
    monkeypatch.setattr(project_release, "_validate_release_schema", lambda *_args: None)
    return {"figure_validation": figure_validation, "integrity": integrity}


def _release_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    advance_context: bool = False,
) -> tuple[Path, Path, dict[str, object]]:
    project = tmp_path / "release-project"
    manuscript = "# Introduction\n\nVersion-bound release fixture.\n"
    manuscript_bytes = manuscript.encode("utf-8")
    manuscript_sha256 = hashlib.sha256(manuscript_bytes).hexdigest()
    lineage_digest = "b" * 64
    workflow_digest = "c" * 64
    (project / "04_manuscript").mkdir(parents=True)
    (project / "03_figures").mkdir()
    (project / "05_release").mkdir()
    (project / "04_manuscript/manuscript.md").write_bytes(manuscript_bytes)
    _write_json(
        project / "04_manuscript/manuscript_lineage.v2.json",
        {"lineage_digest": lineage_digest},
    )
    released_markdown = project / "05_release/self_reviewed_draft.md"
    released_markdown.write_bytes(manuscript_bytes)
    released_docx = project / "05_release/self_reviewed_draft.docx"
    released_docx.write_bytes(b"fixture-docx")
    docx_sha256 = hashlib.sha256(released_docx.read_bytes()).hexdigest()
    VersionContext.create(
        {"fixture": "release-version-binding"},
        project_id=project.name,
        project_root=project,
    )
    binding = _version_binding(project)
    gate_state = _patch_readiness_gates(
        monkeypatch,
        lineage_digest=lineage_digest,
        manuscript_sha256=manuscript_sha256,
        workflow_digest=workflow_digest,
    )
    snapshot = {
        "schema_version": "release-snapshot.v1",
        "project_id": project.name,
        "route": project_release.NEW_ROUTE,
        "release_level": "SELF_REVIEWED_DRAFT",
        "status": "SELF_REVIEWED_DRAFT",
        "workflow_digest": workflow_digest,
        "lineage_digest": lineage_digest,
        "manuscript_sha256": manuscript_sha256,
        "release_markdown_sha256": manuscript_sha256,
        "chemical_paper_binding_digest": None,
        "chemical_paper_safe_summary": None,
        "chemical_paper_dependency_can_release": True,
        "markdown_path": "05_release/self_reviewed_draft.md",
        "docx_path": "05_release/self_reviewed_draft.docx",
        "docx_sha256": docx_sha256,
        "figure_validation": gate_state["figure_validation"],
        "placeholder_count": 0,
        "pending_placeholder_count": 0,
        "hard_fail_signals": [],
        "system_generated_synthesis_figure": False,
        "integrity": gate_state["integrity"],
        **binding,
    }
    quality = {
        "schema_version": "project-release.v2",
        "status": "SELF_REVIEWED_DRAFT",
        "release_level": "SELF_REVIEWED_DRAFT",
        "workflow_digest": workflow_digest,
        "lineage_digest": lineage_digest,
        "manuscript_sha256": manuscript_sha256,
        "release_markdown_sha256": manuscript_sha256,
        "chemical_paper_binding_digest": None,
        "chemical_paper_safe_summary": None,
        "chemical_paper_dependency_can_release": True,
        "docx_sha256": docx_sha256,
        "figure_validation": gate_state["figure_validation"],
        "integrity": gate_state["integrity"],
        **binding,
    }
    _write_json(project / "05_release/release_snapshot.json", snapshot)
    _write_json(project / "05_release/quality_report.json", quality)
    if advance_context:
        context = VersionContext.load(project)
        state = context.state()
        context.publish_active_head(
            {"fixture": "release-version-binding", "advanced": True},
            expected_head_id=state.active_head_id,
            expected_revision=state.revision,
            version_id="v2",
        )
    return project, released_docx, binding


def test_new_route_release_requires_version_context_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "missing-version-context"
    project.mkdir()
    monkeypatch.setattr(
        project_release,
        "workflow_state",
        lambda _project: {
            "route": project_release.NEW_ROUTE,
            "parse_ready": True,
            "internal_draft_export_ready": True,
        },
    )
    before = _tree_fingerprint(project)

    with pytest.raises(project_release.ProjectReleaseError) as error:
        project_release.build_project_release(project)

    assert error.value.code == "VERSION_CONTEXT_INVALID"
    assert _tree_fingerprint(project) == before


@pytest.mark.parametrize("binding_case", ["missing", "wrong", "stale"])
def test_new_route_currentness_rejects_missing_wrong_or_stale_version_binding_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_case: str,
) -> None:
    project, docx, binding = _release_fixture(
        tmp_path, monkeypatch, advance_context=binding_case == "stale"
    )
    if binding_case == "missing":
        for path in (
            project / "05_release/release_snapshot.json",
            project / "05_release/quality_report.json",
        ):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for key in binding:
                payload.pop(key, None)
            _write_json(path, payload)
    elif binding_case == "wrong":
        for path in (
            project / "05_release/release_snapshot.json",
            project / "05_release/quality_report.json",
        ):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["version_context_digest"] = "d" * 64
            _write_json(path, payload)

    before = _tree_fingerprint(project)
    assert project_release.new_route_release_docx_is_current(docx) is False
    assert _tree_fingerprint(project) == before


def test_new_route_currentness_accepts_current_version_context_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, docx, _binding = _release_fixture(tmp_path, monkeypatch)

    assert project_release.new_route_release_docx_is_current(docx) is True


def test_new_route_currentness_rejects_missing_release_figure_provenance_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, docx, _binding = _release_fixture(tmp_path, monkeypatch)
    quality_path = project / "05_release/quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["figure_validation"]["source_figures"] = [{"figure_id": "missing-fields"}]
    _write_json(quality_path, quality)
    before = _tree_fingerprint(project)

    assert project_release.new_route_release_docx_is_current(docx) is False
    assert _tree_fingerprint(project) == before
