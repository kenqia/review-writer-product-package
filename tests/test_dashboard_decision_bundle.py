"""Focused public HTTP coverage for the read-only Decision Bundle caller."""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
from pathlib import Path

from review_writer.agent import local_pdf_parse  # noqa: F401 - initialize Dashboard imports
from review_writer.product_foundation import VersionContext
from view import serve_review_dashboard as dashboard


def _tree_fingerprint(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _request(
    server: dashboard.ThreadingHTTPServer, path: str
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(*server.server_address, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        assert isinstance(payload, dict)
        return response.status, payload
    finally:
        connection.close()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "decision-bundle-http"
    project.mkdir()
    VersionContext.create(
        {
            "currentness": "current",
            "agent_bootstrap": {
                "status": "HUMAN_ACTION_REQUIRED",
                "reason_code": "SOURCE_ROLE_HUMAN_ACTION_REQUIRED",
            },
        },
        project_id=project.name,
        project_root=project,
    )
    return project


def test_dashboard_decision_bundle_is_read_only_and_stale_revision_fails_closed(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    before_state = VersionContext.load(project).state()
    before_files = _tree_fingerprint(project)

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request(
            server,
            f"/api/project/{project.name}/decision-bundle?expected_revision=0",
        )
        assert status == 200
        assert payload["schema_version"] == "decision-bundle.v1"
        assert payload["status"] == "HUMAN_ACTION_REQUIRED"
        assert payload["write_mode"] == "NONE"
        assert payload["current_unchanged"] is True

        stale_status, stale_payload = _request(
            server,
            f"/api/project/{project.name}/decision-bundle?expected_revision=99",
        )
        assert stale_status == 409
        assert stale_payload["reason_code"] == "VERSION_CONFLICT"
        assert stale_payload["category"] == "VERSION_CONFLICT"
        assert stale_payload["write_mode"] == "zero_write"
        assert stale_payload["current_unchanged"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert VersionContext.load(project).state() == before_state
    assert _tree_fingerprint(project) == before_files


def test_dashboard_decision_bundle_rejects_unknown_query_without_writing(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    before_state = VersionContext.load(project).state()
    before_files = _tree_fingerprint(project)

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request(
            server,
            f"/api/project/{project.name}/decision-bundle?unexpected=1",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert status == 400
    assert payload["error_code"] == "DECISION_BUNDLE_REQUEST_INVALID"
    assert payload["write_mode"] == "zero_write"
    assert payload["current_unchanged"] is True
    assert VersionContext.load(project).state() == before_state
    assert _tree_fingerprint(project) == before_files
