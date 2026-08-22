"""Focused contract tests for the public Agent intake adapter."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
from pathlib import Path
from urllib.error import URLError

import pytest

from review_writer.agent import start_or_resume_review
from review_writer.agent import public_entry
from review_writer.agent import fresh_bootstrap
from review_writer.agent import local_pdf_parse
from review_writer.product_foundation import VersionContext


class _FastHealthResponse:
    status = 200

    def __enter__(self) -> "_FastHealthResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


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


def _complete_source_mapping(project: Path, authorized: Path) -> None:
    source = authorized / "main.pdf"
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_relative = "manual_upload/main.pdf"
    destination = project / "00_sources" / source_relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    study_id = f"UPLOAD-{source_digest[:20]}"
    manifest = {
        "schema_version": "public-corpus-acquisition.v1",
        "downloads": [
            {
                "download_id": study_id,
                "study_id": study_id,
                "source_id": study_id,
                "document_role": "MAIN",
                "target_path": f"00_sources/{source_relative}",
                "sha256": source_digest,
            }
        ],
    }
    receipt = {
        "schema_version": "acquisition-final-receipt.v1",
        "source_origin": "RESEARCHER_MANUAL_UPLOAD",
        "total_studies": 1,
        "studies": [
            {
                "study_id": study_id,
                "source_id": study_id,
                "download_id": study_id,
                "document_role": "MAIN",
                "archive_sha256": "a" * 64,
                "main_pdf": {
                    "path": source_relative,
                    "sha256": source_digest,
                    "size_bytes": destination.stat().st_size,
                },
            }
        ],
    }
    identity_audit = {
        "schema_version": "source-identity-audit.v1",
        "results": [
            {
                "candidate_id": study_id,
                "study_id": study_id,
                "source_id": study_id,
                "document_role": "MAIN",
                "source_sha256": source_digest,
                "verdict": "PASS",
            }
        ],
    }
    discovery = project / "00_discovery"
    discovery.mkdir(parents=True, exist_ok=True)
    (discovery / "acquisition_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (project / "00_sources/acquisition_final_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    (project / "00_sources/source_identity_audit.json").write_text(
        json.dumps(identity_audit), encoding="utf-8"
    )


def _replace_current_snapshot(project: Path, snapshot: dict[str, object]) -> None:
    context = VersionContext.load(project)
    state = context.state()
    context.view_version(state.current_version_id)
    context.publish_active_head(
        copy.deepcopy(snapshot),
        expected_head_id=state.active_head_id,
        expected_revision=state.revision,
        version_id="resume-test-current",
    )


def test_dashboard_health_probe_uses_static_health_when_projects_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(url: str, *, timeout: float) -> _FastHealthResponse:
        calls.append((url, timeout))
        if url.endswith("/api/projects"):
            time.sleep(0.25)
            raise URLError("projects listing is intentionally slow")
        assert url.endswith("/api/health")
        return _FastHealthResponse()

    monkeypatch.setattr(public_entry, "urlopen", fake_urlopen)

    assert public_entry._dashboard_is_healthy("http://127.0.0.1:43123") is True
    assert calls == [("http://127.0.0.1:43123/api/health", 0.2)]


def test_dashboard_start_probe_uses_static_health_when_projects_is_slow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(url: str, *, timeout: float) -> _FastHealthResponse:
        calls.append((url, timeout))
        if url.endswith("/api/projects"):
            time.sleep(0.25)
            raise URLError("projects listing is intentionally slow")
        assert url.endswith("/api/health")
        return _FastHealthResponse()

    monkeypatch.setattr(fresh_bootstrap, "urlopen", fake_urlopen)
    monkeypatch.setattr(fresh_bootstrap, "_DASHBOARD_START_TIMEOUT_SECONDS", 0.1)

    dashboard_url, dashboard_pid = fresh_bootstrap._start_dashboard(tmp_path)
    try:
        assert dashboard_url.startswith("http://127.0.0.1:")
        assert dashboard_pid > 0
        assert calls == [(f"{dashboard_url}/api/health", 0.2)]
    finally:
        fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(dashboard_pid)


def test_public_entry_is_discoverable_and_maps_human_action_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    _pdf(authorized / "main.pdf")
    project = tmp_path / "projects" / "fresh-case"
    project.parent.mkdir()
    observed: dict[str, object] = {}

    def fake_start(
        self: object,
        *,
        topic: str,
        authorized_pdf_folder: Path,
        rq: str | None = None,
        scope: str | None = None,
        output_format: str | None = None,
    ) -> dict[str, object]:
        observed.update(
            topic=topic,
            authorized_pdf_folder=authorized_pdf_folder,
            rq=rq,
            scope=scope,
            output_format=output_format,
        )
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
    assert observed == {
        "topic": "A bounded topic",
        "authorized_pdf_folder": authorized,
        "rq": "What is reported?",
        "scope": "single study",
        "output_format": "markdown",
    }


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("sidecar", None),
        ("duplicate_hash", "AUTHORIZED_PDF_DUPLICATE_HASH"),
        ("malformed", "SOURCE_ARCHIVE_PDF_INVALID"),
    ],
)
def test_public_fresh_authorized_pdf_folder_ignores_sidecar_and_rejects_invalid_inputs(
    tmp_path: Path,
    case: str,
    expected_code: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    if case == "sidecar":
        _pdf(authorized / "main.pdf")
        (authorized / "SOURCES.md").write_text("researcher notes\n", encoding="utf-8")
    elif case == "duplicate_hash":
        _pdf(authorized / "a.pdf")
        _pdf(authorized / "b.pdf")
    else:
        (authorized / "malformed.pdf").write_bytes(b"%PDF-1.7\nnot a complete PDF")
    project = tmp_path / "projects" / f"public-{case}"
    project.parent.mkdir()
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result: dict[str, object] | None = None
    try:
        if case == "sidecar":
            monkeypatch.setattr(fresh_bootstrap, "_DASHBOARD_START_TIMEOUT_SECONDS", 10.0)
            result = start_or_resume_review(
                "A bounded source-set review",
                project,
                authorized,
            )
            assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
            assert result["reason_code"] == fresh_bootstrap.SOURCE_ROLE_HUMAN_ACTION_REQUIRED
            assert result["write_mode"] == "VERSION_CONTEXT"
            assert result["current"]["project_id"] == project.name
        else:
            with pytest.raises(public_entry.PublicReviewEntryError) as error:
                start_or_resume_review(
                    "A bounded source-set review",
                    project,
                    authorized,
                )
            assert error.value.code == expected_code
            assert error.value.write_mode == "zero_write"
            assert error.value.current_unchanged is True
            assert not project.exists()
            assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before
    finally:
        if result is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(result["dashboard_pid"])


def test_resume_reads_current_and_is_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, tracked = _resume_project(tmp_path)
    before = {path: _fingerprint(path) for path in tracked}
    monkeypatch.setattr(public_entry, "_dashboard_is_healthy", lambda value: True)

    result = start_or_resume_review("Resume topic", project, authorized)

    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert result["write_mode"] == "NONE"
    assert result["current"]["project_id"] == project.name
    assert result["current"]["revision"] == 0
    assert result["revision"] == 0
    assert result["dashboard_url"] == "http://127.0.0.1:43123"
    assert {path: _fingerprint(path) for path in tracked} == before


def test_resume_restarts_stale_dashboard_under_project_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, _ = _resume_project(tmp_path)
    observed: list[Path] = []

    def fake_start(review_root: Path) -> tuple[str, int]:
        observed.append(review_root)
        return "http://127.0.0.1:43210", 901

    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", fake_start)

    result = start_or_resume_review("Resume topic", project, authorized)

    assert observed == [project.parent]
    assert result["dashboard_url"] == "http://127.0.0.1:43210"
    assert result["current"]["project_id"] == project.name
    assert result["write_mode"] == "NONE"


def test_resume_never_returns_external_dashboard_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, _ = _resume_project(tmp_path)
    context = VersionContext.load(project)
    state = context.state()
    current = context.view_version(state.current_version_id)
    snapshot = copy.deepcopy(dict(current.snapshot))
    snapshot["agent_bootstrap"]["tool_trace"] = [
        {"tool": "start_dashboard", "dashboard_url": "https://example.com/review"}
    ]
    _replace_current_snapshot(project, snapshot)

    monkeypatch.setattr(
        fresh_bootstrap,
        "_start_dashboard",
        lambda review_root: ("http://127.0.0.1:43211", 902),
    )

    result = start_or_resume_review("Resume topic", project, authorized)

    assert result["dashboard_url"] == "http://127.0.0.1:43211"
    assert result["dashboard_url"] != "https://example.com/review"
    assert result["current"]["revision"] == state.revision + 1


def test_resume_dashboard_start_failure_holds_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, tracked = _resume_project(tmp_path)
    before = {path: _fingerprint(path) for path in tracked}
    expected_current = {
        "project_id": project.name,
        "version_id": VersionContext.load(project).view_version(
            VersionContext.load(project).state().current_version_id
        ).version_id,
        "revision": 0,
        "snapshot_digest": VersionContext.load(project).view_version(
            VersionContext.load(project).state().current_version_id
        ).snapshot_digest,
    }

    def fail_start(review_root: Path) -> tuple[str, int]:
        raise fresh_bootstrap.FreshAgentBootstrapError(
            "DASHBOARD_START_FAILED", runtime_diagnostic="CHILD_EARLY_EXIT"
        )

    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", fail_start)

    result = start_or_resume_review("Resume topic", project, authorized)

    assert result["result"] == "RESUMED"
    assert result["status"] == "HOLD"
    assert result["reason_code"] == "DASHBOARD_START_FAILED"
    assert result["dashboard_url"] is None
    assert result["current"] == expected_current
    assert result["revision"] == expected_current["revision"]
    assert result["write_mode"] == "NONE"
    assert result["next_action"] == {
        "project_id": project.name,
        "route": "/review",
        "type": "HOLD",
        "reason_code": "DASHBOARD_START_FAILED",
    }
    assert {path: _fingerprint(path) for path in tracked} == before


def test_resume_after_source_mapping_enters_local_parse_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, _ = _resume_project(tmp_path)
    _complete_source_mapping(project, authorized)
    state = VersionContext.load(project).state()
    observed: dict[str, object] = {}

    def fail_start(review_root: Path) -> tuple[str, int]:
        raise fresh_bootstrap.FreshAgentBootstrapError(
            "DASHBOARD_START_FAILED", runtime_diagnostic="CHILD_EARLY_EXIT"
        )

    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", fail_start)

    def fake_parse(
        explicit_project_root: Path,
        *,
        session_id: str | None = None,
        expected_revision: int | None = None,
        expected_head_id: str | None = None,
    ) -> dict[str, object]:
        observed.update(
            project=explicit_project_root,
            session_id=session_id,
            expected_revision=expected_revision,
            expected_head_id=expected_head_id,
        )
        return {
            "status": "HUMAN_ACTION_REQUIRED",
            "reason_code": "PARSE_QUALITY_HUMAN_ACTION_REQUIRED",
            "project_id": project.name,
            "current": {
                "version_id": "agent-local-parse-test",
                "revision": state.revision + 1,
                "snapshot_digest": "b" * 64,
            },
            "trace": {"event_count": 3},
        }

    monkeypatch.setattr(local_pdf_parse, "parse_project_sources", fake_parse)

    result = start_or_resume_review("Resume topic", project, authorized)

    assert observed == {
        "project": project,
        "session_id": None,
        "expected_revision": state.revision,
        "expected_head_id": state.active_head_id,
    }
    assert result["result"] == "RESUMED"
    assert result["status"] == "HUMAN_ACTION_REQUIRED"
    assert result["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
    assert result["current"]["project_id"] == project.name
    assert result["revision"] == state.revision + 1
    assert result["write_mode"] == "VERSION_CONTEXT"


def test_resume_parse_failure_is_not_swallowed_or_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, tracked = _resume_project(tmp_path)
    _complete_source_mapping(project, authorized)
    before = {path: _fingerprint(path) for path in tracked}

    def fail_start(review_root: Path) -> tuple[str, int]:
        raise fresh_bootstrap.FreshAgentBootstrapError(
            "DASHBOARD_START_FAILED", runtime_diagnostic="CHILD_EARLY_EXIT"
        )

    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", fail_start)

    def fail_parse(*args: object, **kwargs: object) -> object:
        raise local_pdf_parse.LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")

    monkeypatch.setattr(local_pdf_parse, "parse_project_sources", fail_parse)

    with pytest.raises(local_pdf_parse.LocalPdfParseError) as error:
        start_or_resume_review("Resume topic", project, authorized)

    assert error.value.code == "LOCAL_PDF_PARSE_FAILED"
    assert {path: _fingerprint(path) for path in tracked} == before


def test_resume_with_existing_parse_artifact_does_not_repeat_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, tracked = _resume_project(tmp_path)
    _complete_source_mapping(project, authorized)
    artifact = project / "01_evidence/mineru/manifest.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}\n", encoding="utf-8")
    before = {path: _fingerprint(path) for path in tracked}
    calls: list[object] = []
    monkeypatch.setattr(public_entry, "_dashboard_is_healthy", lambda value: True)

    def fail_if_called(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("parse seam must not run when parse artifacts exist")

    monkeypatch.setattr(local_pdf_parse, "parse_project_sources", fail_if_called)

    result = start_or_resume_review("Resume topic", project, authorized)

    assert calls == []
    assert result["result"] == "RESUMED"
    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert result["reason_code"] == fresh_bootstrap.SOURCE_ROLE_HUMAN_ACTION_REQUIRED
    assert result["write_mode"] == "NONE"
    assert {path: _fingerprint(path) for path in tracked} == before


def test_resume_reparses_existing_artifacts_when_parse_quality_requires_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, authorized, _ = _resume_project(tmp_path)
    _complete_source_mapping(project, authorized)
    artifact = project / "01_evidence/mineru/manifest.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}\n", encoding="utf-8")
    snapshot = copy.deepcopy(
        dict(
            VersionContext.load(project)
            .view_version(VersionContext.load(project).state().current_version_id)
            .snapshot
        )
    )
    snapshot["agent_parse"] = {
        "status": fresh_bootstrap.HUMAN_ACTION_REQUIRED,
        "reason_code": "PARSE_QUALITY_HUMAN_ACTION_REQUIRED",
        "session_id": "generator-session-existing",
        "tool_trace": [],
        "next_action": {
            "project_id": project.name,
            "route": "/review",
            "type": fresh_bootstrap.HUMAN_ACTION_REQUIRED,
        },
    }
    _replace_current_snapshot(project, snapshot)
    state = VersionContext.load(project).state()
    observed: dict[str, object] = {}

    monkeypatch.setattr(public_entry, "_dashboard_is_healthy", lambda _value: True)
    monkeypatch.setattr(
        local_pdf_parse,
        "parse_quality_state",
        lambda _project, _study_id: {
            "objects": [{"decision": {"action": "reparse_required"}}]
        },
    )

    def fake_reparse(
        explicit_project_root: Path,
        *,
        session_id: str | None = None,
        expected_revision: int | None = None,
        expected_head_id: str | None = None,
    ) -> dict[str, object]:
        observed.update(
            project=explicit_project_root,
            session_id=session_id,
            expected_revision=expected_revision,
            expected_head_id=expected_head_id,
        )
        return {
            "status": fresh_bootstrap.HUMAN_ACTION_REQUIRED,
            "reason_code": "PARSE_QUALITY_HUMAN_ACTION_REQUIRED",
            "project_id": project.name,
            "current": {
                "version_id": "agent-reparse-test",
                "revision": state.revision + 1,
                "snapshot_digest": "c" * 64,
            },
            "trace": {"event_count": 4},
        }

    monkeypatch.setattr(
        local_pdf_parse,
        "reparse_project_sources",
        fake_reparse,
        raising=False,
    )

    result = start_or_resume_review("Resume topic", project, authorized)

    assert observed == {
        "project": project,
        "session_id": "generator-session-existing",
        "expected_revision": state.revision,
        "expected_head_id": state.active_head_id,
    }
    assert result["result"] == "RESUMED"
    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert result["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
    assert result["current"]["project_id"] == project.name
    assert result["revision"] == state.revision + 1
    assert result["write_mode"] == "VERSION_CONTEXT"


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
