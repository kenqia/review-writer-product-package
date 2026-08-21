"""Focused public HTTP coverage for project-scoped version comparison."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from review_writer.agent import local_pdf_parse  # noqa: F401 - initialize Dashboard imports
from review_writer.product_foundation import VersionContext
from view import serve_review_dashboard as dashboard


def _request(server: dashboard.ThreadingHTTPServer, path: str) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(*server.server_address, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return response.status, payload
    finally:
        connection.close()


def test_project_compare_returns_read_only_version_bound_diff_without_moving_current(
    tmp_path: Path,
) -> None:
    project = tmp_path / "compare-project"
    project.mkdir()
    VersionContext.create(
        {"topic": "v1", "unchanged": True},
        project_id=project.name,
        version_id="v1",
        project_root=project,
    ).publish_active_head(
        {"topic": "v2", "unchanged": True},
        expected_head_id="v1",
        expected_revision=0,
        version_id="v2",
    )
    before = VersionContext.load(project).state()

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request(
            server,
            f"/api/project/{project.name}/compare?left_version_id=v1&right_version_id=v2",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert status == 200
    assert payload["project_id"] == project.name
    assert payload["revision"] == before.revision
    assert payload["current"]["version_id"] == before.current_version_id
    assert payload["left"]["version_id"] == "v1"
    assert payload["right"]["version_id"] == "v2"
    assert payload["comparison"] == {
        "left_version_id": "v1",
        "right_version_id": "v2",
        "changed_fields": ["topic"],
        "changes": {"topic": {"left": "v1", "right": "v2"}},
    }
    after = VersionContext.load(project).state()
    assert after == before


def test_project_compare_rejects_invalid_or_unknown_versions_without_writing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "compare-negative-project"
    project.mkdir()
    VersionContext.create(
        {"topic": "v1"},
        project_id=project.name,
        version_id="v1",
        project_root=project,
    ).publish_active_head(
        {"topic": "v2"},
        expected_head_id="v1",
        expected_revision=0,
        version_id="v2",
    )
    before = VersionContext.load(project).state()
    before_files = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cases = (
            ("?left_version_id=v1", 400, "HISTORY_COMPARE_REQUEST_INVALID"),
            (
                "?left_version_id=v1&right_version_id=v2&right_version_id=v1",
                400,
                "HISTORY_COMPARE_REQUEST_INVALID",
            ),
            (
                "?left_version_id=../v1&right_version_id=v2",
                400,
                "HISTORY_COMPARE_REQUEST_INVALID",
            ),
            (
                "?left_version_id=v1&right_version_id=unknown",
                409,
                "VERSION_NOT_FOUND",
            ),
        )
        for query, expected_status, expected_code in cases:
            status, payload = _request(
                server,
                f"/api/project/{project.name}/compare{query}",
            )
            assert status == expected_status
            assert payload["error_code"] == expected_code
            assert VersionContext.load(project).state() == before
            assert {
                path.relative_to(project).as_posix(): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            } == before_files
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
