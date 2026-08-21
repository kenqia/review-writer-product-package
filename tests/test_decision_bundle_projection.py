"""Public Decision Bundle projection contract."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from review_writer import agent
from review_writer.product_foundation import VersionContext


def _fingerprint_tree(root: Path) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        result[path.relative_to(root).as_posix()] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.st_size,
            stat.st_mtime_ns,
        )
    return result


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "decision-bundle"
    project.mkdir()
    VersionContext.create(
        {
            "currentness": "current",
            "agent_bootstrap": {
                "status": "HUMAN_ACTION_REQUIRED",
                "reason_code": "SOURCE_ROLE_HUMAN_ACTION_REQUIRED",
                "authorized_source_set": [
                    {
                        "member_id": "MEMBER-0001",
                        "name": "main.pdf",
                        "sha256": "a" * 64,
                        "size_bytes": 123,
                        "download_id": "UPLOAD-aaaaaaaaaaaaaaaaaaaa",
                        "source_id": "UPLOAD-aaaaaaaaaaaaaaaaaaaa",
                        "study_id": "UPLOAD-aaaaaaaaaaaaaaaaaaaa",
                    }
                ],
            },
        },
        project_id=project.name,
        project_root=project,
    )
    return project


def test_decision_bundle_public_entry_is_discoverable() -> None:
    """CR-005 exposes one stable read-only Agent caller."""

    assert callable(getattr(agent, "build_decision_bundle", None))


def test_bounded_version_context_projection_is_human_action_required_and_zero_write(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    before = _fingerprint_tree(project)

    result = agent.build_decision_bundle(project)

    assert result["schema_version"] == "decision-bundle.v1"
    assert result["status"] == "HUMAN_ACTION_REQUIRED"
    assert result["current"]["version_id"] == "v1"
    assert result["current"]["revision"] == 0
    assert result["current"]["digest"] == result["current"]["snapshot_digest"]
    assert result["write_mode"] == "NONE"
    assert result["current_unchanged"] is True
    assert result["source_identities"][0]["study_id"] == "UPLOAD-aaaaaaaaaaaaaaaaaaaa"
    assert result["decision_options"]
    assert result["expected_write_set"]
    assert result["conflicts"]
    assert result["evidence"]["workflow_can_continue"] is False
    assert result["synthesis"]["workflow_can_continue"] is False
    assert _fingerprint_tree(project) == before


def test_stale_expected_revision_is_version_conflict_zero_write(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = _fingerprint_tree(project)

    result = agent.build_decision_bundle(project, expected_revision=99)

    assert result["status"] == "VERSION_CONFLICT"
    assert result["reason_code"] == "VERSION_CONFLICT"
    assert result["category"] == "VERSION_CONFLICT"
    assert result["write_mode"] == "zero_write"
    assert result["current_unchanged"] is True
    assert result["current"]["revision"] == 0
    assert _fingerprint_tree(project) == before


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_invalid_or_corrupt_version_context_fails_closed_zero_write(
    tmp_path: Path, kind: str
) -> None:
    project = tmp_path / f"{kind}-project"
    project.mkdir()
    if kind == "corrupt":
        context_root = project / ".review-writer/version_context"
        context_root.mkdir(parents=True)
        (context_root / "current.json").write_text("{broken\n", encoding="utf-8")
    before = _fingerprint_tree(project)

    result = agent.build_decision_bundle(project)

    assert result["category"] == "PRECONDITION_FAILED"
    assert result["reason_code"] == "VERSION_CONTEXT_INVALID"
    assert result["write_mode"] == "zero_write"
    assert result["current_unchanged"] is True
    assert _fingerprint_tree(project) == before
