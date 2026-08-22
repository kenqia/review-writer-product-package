"""Focused tests for portable discovery of the optional MinerU parser."""

from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from review_writer.agent import fresh_bootstrap, public_entry
from review_writer.agent import local_pdf_parse
from review_writer.product_foundation import VersionContext
from review_writer.project.parse_quality import (
    apply_parse_quality_decision,
    parse_quality_state,
)
from review_writer.project.source_truth import load_source_truth_bundle


def _write_pdf(folder: Path, name: str, payload: bytes) -> Path:
    path = folder / name
    content = f"BT /F1 12 Tf 72 720 Td ({payload.decode('ascii')}) Tj ET\n".encode(
        "ascii"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"endstream",
    ]
    document = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(document))
        document += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_offset = len(document)
    document += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    document += b"0000000000 65535 f \n"
    document += b"".join(
        f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]
    )
    document += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode("ascii")
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    path.write_bytes(document)
    return path


def _public_parse_project(tmp_path: Path) -> tuple[Path, Path, str]:
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    source = _write_pdf(authorized, "main.pdf", b"portable parser input")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    project = tmp_path / "projects" / "portable-parse"
    (project / "00_brief").mkdir(parents=True)
    (project / "00_brief/review_state.json").write_text(
        json.dumps({"project_id": project.name, "brief": {"topic": "Portable parse"}}),
        encoding="utf-8",
    )
    (project / ".paper_evidence.lock").write_bytes(b"lock")
    source_root = project / "00_sources/manual_upload"
    source_root.mkdir(parents=True)
    shutil.copy2(source, source_root / source.name)
    archive, source_set = fresh_bootstrap._build_authorized_archive((source,), tmp_path)
    archive_destination = project / fresh_bootstrap.SOURCE_ARCHIVE_RELATIVE
    archive_destination.parent.mkdir(parents=True)
    shutil.copy2(archive, archive_destination)
    archive.unlink()
    preflight = fresh_bootstrap._preflight_source_archive(archive_destination, source_set)
    study_id = source_set[0]["study_id"]
    (project / "00_sources/acquisition_final_receipt.json").write_text(
        json.dumps(
            {
                "schema_version": "acquisition-final-receipt.v1",
                "source_origin": "RESEARCHER_MANUAL_UPLOAD",
                "studies": [
                    {
                        "study_id": study_id,
                        "source_id": study_id,
                        "main_pdf": {
                            "path": f"manual_upload/{source.name}",
                            "sha256": source_sha256,
                            "size_bytes": source.stat().st_size,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    brief_sha256 = hashlib.sha256(
        (project / "00_brief/review_state.json").read_bytes()
    ).hexdigest()
    VersionContext.create(
        {
            "currentness": "current",
            "artifact_refs": [
                {"path": "00_brief/review_state.json", "sha256": brief_sha256}
            ],
            "agent_bootstrap": {
                "status": fresh_bootstrap.HUMAN_ACTION_REQUIRED,
                "reason_code": fresh_bootstrap.SOURCE_ROLE_HUMAN_ACTION_REQUIRED,
                "source_archive": preflight,
                "authorized_source_set": source_set,
                "next_action": {
                    "project_id": project.name,
                    "route": "/review",
                    "type": fresh_bootstrap.HUMAN_ACTION_REQUIRED,
                },
            },
        },
        project_id=project.name,
        project_root=project,
    )
    return project, authorized, source_sha256


def _fake_mineru_run(observed: dict[str, object], expected_parser: Path):
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = list(command)
        assert command[1] == str(expected_parser)
        pdf = Path(command[command.index("--pdf") + 1])
        output = Path(command[command.index("--output-dir") + 1])
        slug = "mineru_test"
        markdown = "# Parsed by MinerU\n\nPortable parser output.\n"
        content = [{"type": "text", "text": "Portable parser output.", "page_idx": 0, "bbox": [0, 0, 1, 1]}]
        extracted = output / "extracted" / slug
        (extracted / "images").mkdir(parents=True)
        (output / "markdown").mkdir(parents=True)
        (output / "raw_zips").mkdir(parents=True)
        (output / "markdown" / f"{slug}.md").write_text(markdown, encoding="utf-8")
        (extracted / "full.md").write_text(markdown, encoding="utf-8")
        (extracted / f"{slug}_content_list.json").write_text(json.dumps(content), encoding="utf-8")
        (extracted / f"{slug}_content_list_v2.json").write_text(json.dumps([content]), encoding="utf-8")
        (extracted / "layout.json").write_text(json.dumps({"page_count": 1}), encoding="utf-8")
        with zipfile.ZipFile(output / "raw_zips" / f"{slug}.zip", "w") as archive:
            archive.writestr("full.md", markdown)
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "completed_count": 1,
                    "failed_count": 0,
                    "completed": [{"slug": slug, "state": "done", "pdf_name": pdf.name}],
                    "failed": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    return run


def _dashboard_unavailable(_review_root: Path) -> tuple[str, int]:
    raise fresh_bootstrap.FreshAgentBootstrapError(
        "DASHBOARD_START_FAILED", runtime_diagnostic="TEST_UNAVAILABLE"
    )


def _dashboard_request(
    base_url: str,
    method: str,
    route: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    parsed = urlsplit(base_url)
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        connection.request(
            method,
            route,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        decoded = json.loads(response.read().decode("utf-8"))
        assert isinstance(decoded, dict)
        return response.status, decoded
    finally:
        connection.close()


def test_public_agent_selects_mineru_and_publishes_source_bound_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project, authorized, source_sha256 = _public_parse_project(tmp_path)
    parser = tmp_path / "parse_review_writer_pdfs.py"
    parser.write_text("# fake MinerU adapter\n", encoding="utf-8")
    observed: dict[str, object] = {}
    monkeypatch.setattr(local_pdf_parse, "_resolve_mineru_parser", lambda: parser)
    monkeypatch.setattr(
        local_pdf_parse.subprocess,
        "run",
        _fake_mineru_run(observed, parser),
    )
    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", _dashboard_unavailable)

    result = public_entry.start_or_resume_review(
        "Portable MinerU review", project, authorized
    )

    assert result["result"] == "RESUMED"
    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert result["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
    assert observed["command"]
    parser_record = result["parser"]
    assert parser_record["parser_mode"] == "MINERU"
    assert parser_record["backend"] == "mineru-precise-parse"
    assert parser_record["version"]
    assert parser_record["sources"][0]["input_pdf_sha256"] == source_sha256
    assert parser_record["sources"][0]["output_artifact_sha256"]
    assert parser_record["sources"][0]["page_count"] == 1
    assert parser_record["sources"][0]["locators"]["pages"] == [1]
    assert parser_record["chemical_gaps"]
    current = VersionContext.load(project).view_version(
        VersionContext.load(project).state().current_version_id
    )
    assert current.snapshot["agent_parse"]["parser"]["parser_mode"] == "MINERU"
    bundle = load_source_truth_bundle(project, f"UPLOAD-{source_sha256[:20]}")
    assert bundle["sources"][0]["pdf"]["sha256"] == source_sha256


def test_public_agent_executes_fallback_and_records_truthful_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project, authorized, source_sha256 = _public_parse_project(tmp_path)
    monkeypatch.setattr(local_pdf_parse, "_resolve_mineru_parser", lambda: None)
    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", _dashboard_unavailable)

    result = public_entry.start_or_resume_review(
        "Portable fallback review", project, authorized
    )

    assert result["result"] == "RESUMED"
    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    parser_record = result["parser"]
    assert parser_record["parser_mode"] == "FALLBACK"
    assert parser_record["fallback_reason"] == "MINERU_PARSER_UNAVAILABLE"
    assert parser_record["backend"] == "pdftotext"
    assert parser_record["version"]
    assert parser_record["capability_gaps"]
    assert parser_record["chemical_gaps"]
    source_record = parser_record["sources"][0]
    assert source_record["input_pdf_sha256"] == source_sha256
    assert source_record["output_artifact_sha256"]
    assert source_record["page_count"] == 1
    assert source_record["locators"]["pages"] == [1]
    assert (project / "01_evidence/mineru/manifest.json").is_file()
    bundle = load_source_truth_bundle(project, f"UPLOAD-{source_sha256[:20]}")
    assert bundle["sources"][0]["pdf"]["sha256"] == source_sha256
    current = VersionContext.load(project).view_version(
        VersionContext.load(project).state().current_version_id
    )
    assert current.snapshot["agent_parse"]["parser"]["parser_mode"] == "FALLBACK"


def test_public_agent_reparses_existing_parse_and_reopens_reparse_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project, authorized, _source_sha256 = _public_parse_project(tmp_path)
    monkeypatch.setattr(local_pdf_parse, "_resolve_mineru_parser", lambda: None)
    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", _dashboard_unavailable)

    first = public_entry.start_or_resume_review(
        "Portable fallback reparse review", project, authorized
    )
    study_id = next(iter(first["parse_quality"]))["study_id"]
    before_quality = parse_quality_state(project, study_id)
    old_gate_digest = before_quality["gate_digest"]
    targets = [
        row for row in before_quality["objects"] if row.get("review_state") != "not_required"
    ]
    for target in targets:
        apply_parse_quality_decision(
            project,
            study_id,
            {
                "object_id": target["object_id"],
                "gate_digest": old_gate_digest,
                "object_digest": target["object_digest"],
                "action": "reparse_required",
                "note": "The parse must be rerun before this object can be reviewed.",
                "actor_type": "simulated_researcher_agent",
                "actor_label": "reparse-test",
            },
        )

    second = public_entry.start_or_resume_review(
        "Portable fallback reparse review", project, authorized
    )

    assert second["result"] == "RESUMED"
    assert second["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert second["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
    assert second["write_mode"] == "VERSION_CONTEXT"
    assert second["revision"] > first["revision"]
    after_quality = parse_quality_state(project, study_id)
    after_targets = {
        row["object_id"]: row
        for row in after_quality["objects"]
        if row["object_id"] in {target["object_id"] for target in targets}
    }
    assert all(row["decision"] is None for row in after_targets.values())
    assert all(row["review_state"] == "needs_re_review" for row in after_targets.values())
    assert all(row["re_review_reason"] == "reparse_completed" for row in after_targets.values())
    assert all(row["prior_decisions"][-1]["action"] == "reparse_required" for row in after_targets.values())
    assert after_quality["gate_digest"] == old_gate_digest


def test_public_dashboard_reparse_decision_returns_to_human_parse_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project, authorized, _source_sha256 = _public_parse_project(tmp_path)
    monkeypatch.setattr(local_pdf_parse, "_resolve_mineru_parser", lambda: None)
    monkeypatch.setattr(fresh_bootstrap, "_DASHBOARD_START_TIMEOUT_SECONDS", 10.0)
    dashboard_pid: int | None = None
    try:
        first = public_entry.start_or_resume_review(
            "Portable Dashboard reparse review", project, authorized
        )
        dashboard_pid = (
            first.get("dashboard_pid")
            if isinstance(first.get("dashboard_pid"), int)
            else None
        )
        base_url = first["dashboard_url"]
        project_id = first["project_id"]
        status, quality = _dashboard_request(
            base_url, "GET", f"/api/project/{project_id}/parse-quality"
        )
        assert status == 200
        target_study = next(study for study in quality["studies"] if study["objects"])
        targets = [obj for obj in target_study["objects"] if obj["actions"]]
        for target in targets:
            status, saved = _dashboard_request(
                base_url,
                "PUT",
                f"/api/project/{project_id}/parse-quality",
                {
                    "study_id": target_study["study_id"],
                    "object_id": target["object_id"],
                    "decision_token": target["decision_token"],
                    "action": "reparse_required",
                    "note": "Reparse this object through the Dashboard review seam.",
                },
            )
            assert status == 200
        assert saved["summary"]["reparse_required"] == len(targets)

        resumed = public_entry.start_or_resume_review(
            "Portable Dashboard reparse review", project, authorized
        )

        assert resumed["result"] == "RESUMED"
        assert resumed["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert resumed["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
        assert resumed["revision"] > first["revision"]
        status, after = _dashboard_request(
            base_url, "GET", f"/api/project/{project_id}/parse-quality"
        )
        assert status == 200
        assert after["summary"]["reparse_required"] == 0
        assert after["summary"]["needs_re_review"] == len(targets)
        assert after["next_action"]["code"] == "review_reparsed_objects"
    finally:
        if dashboard_pid is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(dashboard_pid)


def test_reparse_rejects_stale_version_without_replacing_parse_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project, authorized, _source_sha256 = _public_parse_project(tmp_path)
    monkeypatch.setattr(local_pdf_parse, "_resolve_mineru_parser", lambda: None)
    monkeypatch.setattr(fresh_bootstrap, "_start_dashboard", _dashboard_unavailable)

    first = public_entry.start_or_resume_review(
        "Portable stale reparse review", project, authorized
    )
    study_id = next(iter(first["parse_quality"]))["study_id"]
    quality = parse_quality_state(project, study_id)
    for target in quality["objects"]:
        if target.get("review_state") == "not_required":
            continue
        apply_parse_quality_decision(
            project,
            study_id,
            {
                "object_id": target["object_id"],
                "gate_digest": quality["gate_digest"],
                "object_digest": target["object_digest"],
                "action": "reparse_required",
                "note": "The parse must be rerun before this object can be reviewed.",
                "actor_type": "simulated_researcher_agent",
                "actor_label": "stale-reparse-test",
            },
        )
    state_before = VersionContext.load(project).state()
    current_path = project / ".review-writer/version_context/current.json"
    quality_path = project / "01_evidence/source_truth" / study_id / "parse_quality.json"
    current_before = current_path.read_bytes()
    quality_before = quality_path.read_bytes()

    with pytest.raises(local_pdf_parse.LocalPdfParseError) as error:
        local_pdf_parse.reparse_project_sources(
            project,
            session_id=first["session_id"],
            expected_revision=state_before.revision + 1,
            expected_head_id=state_before.active_head_id,
        )

    assert error.value.code == "GENERATOR_VERSION_CONFLICT"
    assert VersionContext.load(project).state() == state_before
    assert current_path.read_bytes() == current_before
    assert quality_path.read_bytes() == quality_before


def test_mineru_parser_resolution_honors_explicit_path(monkeypatch, tmp_path: Path) -> None:
    parser = tmp_path / "parse_review_writer_pdfs.py"
    parser.write_text("# test parser\n", encoding="utf-8")

    monkeypatch.setenv("REVIEW_WRITER_MINERU_PARSER", str(parser))
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert local_pdf_parse._resolve_mineru_parser() == parser


def test_mineru_parser_resolution_uses_package_local_skill_before_home_or_path(
    monkeypatch, tmp_path: Path
) -> None:
    package_root = tmp_path / "package"
    parser = (
        package_root
        / ".agents/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py"
    )
    parser.parent.mkdir(parents=True)
    parser.write_text("# package-local parser\n", encoding="utf-8")

    monkeypatch.delenv("REVIEW_WRITER_MINERU_PARSER", raising=False)
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert (
        local_pdf_parse._resolve_mineru_parser(
            package_root=package_root,
            home=tmp_path / "home",
            path_lookup=lambda _name: None,
        )
        == parser
    )


def test_mineru_parser_resolution_finds_user_skill_root(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    parser = (
        home
        / ".codex/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py"
    )
    parser.parent.mkdir(parents=True)
    parser.write_text("# user-skill parser\n", encoding="utf-8")

    monkeypatch.delenv("REVIEW_WRITER_MINERU_PARSER", raising=False)
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert (
        local_pdf_parse._resolve_mineru_parser(
            package_root=tmp_path / "package",
            home=home,
            path_lookup=lambda _name: None,
        )
        == parser
    )


def test_mineru_parser_resolution_returns_none_for_clean_clone_without_backend(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("REVIEW_WRITER_MINERU_PARSER", raising=False)
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert (
        local_pdf_parse._resolve_mineru_parser(
            package_root=tmp_path / "package",
            home=tmp_path / "home",
            path_lookup=lambda _name: None,
        )
        is None
    )
