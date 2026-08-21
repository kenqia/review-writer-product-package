"""Focused contract tests for the public Agent intake adapter."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from review_writer.agent import start_or_resume_review
from review_writer.agent import public_entry
from review_writer.agent import fresh_bootstrap
from review_writer.product_foundation import VersionContext


def _pdf(path: Path, label: bytes = b"fixture") -> Path:
    body = b"%PDF-1.7\n% " + label + b"\n1 0 obj\n<< /Length 0 >>\nstream\nendstream\nendobj\n"
    xref_offset = len(body)
    object_offset = len(b"%PDF-1.7\n% ") + len(label) + 1
    path.write_bytes(
        body
        + b"xref\n0 2\n0000000000 65535 f \n"
        + f"{object_offset:010d}".encode()
        + b" 00000 n \n"
        + b"trailer\n<< /Size 2 >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return path


def _fingerprint(path: Path) -> tuple[bytes, str, int, int]:
    stat_result = path.stat()
    payload = path.read_bytes()
    return payload, hashlib.sha256(payload).hexdigest(), stat_result.st_mtime_ns, stat_result.st_ino


def _resume_project(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    project = tmp_path / "projects" / "resume-case"
    project.mkdir(parents=True)
    (project / "00_brief").mkdir()
    (project / "00_brief/review_state.json").write_text(
        json.dumps({"project_id": project.name, "brief": {"topic": "Resume case"}}),
        encoding="utf-8",
    )
    (project / ".paper_evidence.lock").write_bytes(b"lock")
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    pdf = _pdf(authorized / "main.pdf")
    archive, source_set = fresh_bootstrap._build_authorized_archive((pdf,), tmp_path)
    destination = project / fresh_bootstrap.SOURCE_ARCHIVE_RELATIVE
    destination.parent.mkdir(parents=True)
    shutil.copy2(archive, destination)
    preflight = fresh_bootstrap._source_archive_preflight(destination)
    archive.unlink()
    evidence = project / "01_evidence/evidence_cards.jsonl"
    evidence.parent.mkdir()
    evidence.write_bytes(b"{}\n")
    snapshot = {
        "currentness": "current",
        "version_token": "resume-token",
        "artifact_refs": [
            {
                "path": evidence.relative_to(project).as_posix(),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        ],
        "agent_bootstrap": {
            "status": "HUMAN_ACTION_REQUIRED",
            "reason_code": fresh_bootstrap.SOURCE_ROLE_HUMAN_ACTION_REQUIRED,
            "source_archive": preflight,
            "authorized_source_set": source_set,
            "tool_trace": [
                {"tool": "start_dashboard", "dashboard_url": "http://127.0.0.1:43123"}
            ],
            "next_action": {
                "project_id": project.name,
                "route": "/review",
                "type": fresh_bootstrap.HUMAN_ACTION_REQUIRED,
            },
        },
    }
    VersionContext.create(
        snapshot,
        project_id=project.name,
        project_root=project,
    )
    tracked = [
        project / "00_brief/review_state.json",
        project / ".paper_evidence.lock",
        project / "01_evidence/evidence_cards.jsonl",
        project / fresh_bootstrap.SOURCE_ARCHIVE_RELATIVE,
        project / ".review-writer/version_context/current.json",
        *sorted((project / ".review-writer/version_context/versions").iterdir()),
    ]
    return project, authorized, tracked


def test_public_entry_is_discoverable_and_maps_human_action_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    _pdf(authorized / "main.pdf")
    project = tmp_path / "projects" / "fresh-case"
    project.parent.mkdir()
    observed: dict[str, object] = {}

    def fake_start(self: object, *, topic: str, authorized_pdf_folder: Path) -> dict[str, object]:
        observed.update(topic=topic, authorized_pdf_folder=authorized_pdf_folder)
        return {
            "status": fresh_bootstrap.HUMAN_ACTION_REQUIRED,
            "reason_code": fresh_bootstrap.SOURCE_ROLE_HUMAN_ACTION_REQUIRED,
            "project_id": project.name,
            "project_root": str(project),
            "dashboard_url": "http://127.0.0.1:43123",
            "next_action": {"project_id": project.name, "route": "/review"},
            "current": {
                "version_id": "agent-bootstrap-v1",
                "revision": 1,
                "snapshot_digest": "a" * 64,
            },
            "trace": {"run_id": "run-1", "event_count": 4},
        }

    monkeypatch.setattr(public_entry.FreshAgentBootstrap, "start", fake_start)

    result = start_or_resume_review(
        "A bounded topic",
        project,
        authorized,
        rq="What is reported?",
        scope="single study",
        output_format="markdown",
    )

    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert result["write_mode"] == "VERSION_CONTEXT"
    assert result["current"]["project_id"] == project.name
    assert result["current"]["version_id"] == "agent-bootstrap-v1"
    assert result["revision"] == 1
    assert result["dashboard_url"] == "http://127.0.0.1:43123"
    assert observed == {"topic": "A bounded topic", "authorized_pdf_folder": authorized}


def test_resume_reads_current_and_is_zero_write(tmp_path: Path) -> None:
    project, authorized, tracked = _resume_project(tmp_path)
    before = {path: _fingerprint(path) for path in tracked}

    result = start_or_resume_review("Resume topic", project, authorized)

    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert result["write_mode"] == "NONE"
    assert result["current"]["project_id"] == project.name
    assert result["current"]["revision"] == 0
    assert result["revision"] == 0
    assert result["dashboard_url"] == "http://127.0.0.1:43123"
    assert {path: _fingerprint(path) for path in tracked} == before


def test_invalid_root_is_zero_write() -> None:
    with pytest.raises(public_entry.PublicReviewEntryError) as error:
        start_or_resume_review("Topic", "relative/project", "/tmp/authorized")

    assert error.value.code == "PROJECT_ROOT_INVALID"
    assert error.value.category == "INVALID_INPUT"
    assert error.value.write_mode == "zero_write"
    assert error.value.current_unchanged is True


def test_authorized_source_mismatch_is_zero_write(tmp_path: Path) -> None:
    project, authorized, tracked = _resume_project(tmp_path)
    other = tmp_path / "other-authorized"
    other.mkdir()
    _pdf(other / "different.pdf", b"different")
    before = {path: _fingerprint(path) for path in tracked}

    with pytest.raises(public_entry.PublicReviewEntryError) as error:
        start_or_resume_review("Resume topic", project, other)

    assert error.value.code == "AUTHORIZED_PDF_STALE"
    assert error.value.category == "VERSION_CONFLICT"
    assert error.value.write_mode == "zero_write"
    assert error.value.current_unchanged is True
    assert {path: _fingerprint(path) for path in tracked} == before
