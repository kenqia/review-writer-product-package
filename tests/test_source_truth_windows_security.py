"""Focused Windows-capability regressions for source asset snapshots."""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
from pathlib import Path

import pytest

from review_writer.project import source_truth
from view import serve_review_dashboard as dashboard


def _descriptor(path: str, payload: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _write_bundle(project: Path, pdf: bytes, markdown: bytes) -> tuple[str, str]:
    study_id = "study-windows"
    source_id = "source-windows"
    content = b"[]"
    layout = b"{}"
    body: dict[str, object] = {
        "schema_version": "source-truth-bundle.v1",
        "project_id": project.name,
        "study_id": study_id,
        "study_identity": {"doi": None, "title": None},
        "sources": [
            {
                "source_id": source_id,
                "document_role": "MAIN",
                "source_type": "primary_study",
                "mineru_slug": "windows",
                "pdf": _descriptor("assets/main.pdf", pdf),
                "canonical_markdown": _descriptor("assets/main.md", markdown),
                "content_list": _descriptor("assets/content.json", content),
                "content_list_v2": _descriptor("assets/content-v2.json", content),
                "layout": _descriptor("assets/layout.json", layout),
                "reading_layer": _descriptor("assets/reading.txt", markdown),
                "layout_layer": _descriptor("assets/layout.txt", markdown),
                "page_count": 1,
                "images": {"count": 0, "digest": "0" * 64},
            }
        ],
        "warnings": [],
    }
    bundle = {**body, "bundle_digest": source_truth.canonical_digest(body)}
    bundle_path = project / "01_evidence/source_truth" / study_id / "bundle.json"
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    return study_id, source_id


def test_windows_capability_fallback_opens_verified_source_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    source = project / "assets/main.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified source")
    identity = (project.stat().st_dev, project.stat().st_ino)
    monkeypatch.delattr(source_truth.os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(source_truth.os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(source_truth.os, "supports_dir_fd", frozenset())

    descriptor = source_truth._secure_source_fd(project, "assets/main.pdf", identity)
    try:
        assert source_truth.os.fstat(descriptor).st_size == source.stat().st_size
    finally:
        source_truth.os.close(descriptor)


def test_windows_capability_fallback_rejects_reparse_escape_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    link = project / "escape.pdf"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    identity = (project.stat().st_dev, project.stat().st_ino)
    monkeypatch.delattr(source_truth.os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(source_truth.os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(source_truth.os, "supports_dir_fd", frozenset())

    with pytest.raises(source_truth.SourceTruthError) as error:
        source_truth._secure_source_fd(project, "escape.pdf", identity)

    assert error.value.code == "SOURCE_ASSET_INVALID"
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before


def test_windows_snapshot_path_skips_unreliable_posix_mode_but_keeps_type_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "snapshot"
    directory.mkdir()
    directory.chmod(0o755)
    monkeypatch.setattr(source_truth.os, "name", "nt")

    source_truth._require_private_snapshot_path(directory, 0o700, directory=True)

    link = tmp_path / "snapshot-link"
    try:
        link.symlink_to(directory, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(source_truth.SourceTruthError) as error:
        source_truth._require_private_snapshot_path(link, 0o700, directory=True)
    assert error.value.code == "SOURCE_ASSET_SECURITY_UNAVAILABLE"


def _request_bytes(server: dashboard.ThreadingHTTPServer, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(*server.server_address, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_dashboard_source_pdf_and_markdown_snapshot_callers_work_without_posix_open_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project-windows"
    project.mkdir()
    pdf = b"%PDF-1.4\nwindows-source\n"
    markdown = b"# Windows parsed source\n"
    assets = project / "assets"
    assets.mkdir()
    (assets / "main.pdf").write_bytes(pdf)
    (assets / "main.md").write_bytes(markdown)
    for name, payload in {
        "content.json": b"[]",
        "content-v2.json": b"[]",
        "layout.json": b"{}",
        "reading.txt": markdown,
        "layout.txt": markdown,
    }.items():
        (assets / name).write_bytes(payload)
    study_id, source_id = _write_bundle(project, pdf, markdown)
    assert study_id and source_id
    monkeypatch.delattr(source_truth.os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(source_truth.os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(source_truth.os, "supports_dir_fd", frozenset())

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pdf_status, pdf_body = _request_bytes(
            server, f"/api/project/{project.name}/source/{source_id}/pdf"
        )
        markdown_status, markdown_body = _request_bytes(
            server, f"/api/project/{project.name}/source/{source_id}/parsed-markdown"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert pdf_status == 200
    assert pdf_body == pdf
    assert markdown_status == 200
    assert markdown_body == markdown
