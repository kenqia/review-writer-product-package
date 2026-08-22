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


def _request_json(
    server: dashboard.ThreadingHTTPServer,
    method: str,
    path: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection(*server.server_address, timeout=10)
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def test_project_history_branch_posts_new_immutable_version_and_preserves_original_head(
    tmp_path: Path,
) -> None:
    project = tmp_path / "branch-project"
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

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request_json(
            server,
            "POST",
            f"/api/project/{project.name}/history/branch",
            {
                "source_version_id": "v1",
                "branch_id": "experiment",
                "branch_name": "Experiment",
                "version_id": "experiment-v1",
                "activate": True,
                "confirm": True,
                "expected_revision": before.revision,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert status == 200
    assert payload["result"] == "BRANCHED"
    assert payload["write_mode"] == "VERSION_CONTEXT"
    assert payload["revision"] == before.revision + 1
    assert payload["current"]["version_id"] == "experiment-v1"
    assert payload["current"]["branch_id"] == "experiment"
    history = {row["version_id"]: row for row in payload["history"]}
    assert set(history) == {"v1", "v2", "experiment-v1"}
    assert history["experiment-v1"]["parent_version_id"] == "v1"
    assert history["experiment-v1"]["is_current"] is True
    assert history["experiment-v1"]["is_active_head"] is True
    assert history["v2"]["branch_id"] == "main"
    assert history["v2"]["is_active_head"] is False
    assert history["v2"]["read_only"] is True

    after = VersionContext.load(project).state()
    assert after.current_version_id == "experiment-v1"
    assert after.active_branch_id == "experiment"
    assert after.branch_heads == {"main": "v2", "experiment": "experiment-v1"}
    assert after.revision == before.revision + 1


def test_project_history_undo_posts_rollback_leaf_and_preserves_main_lineage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "undo-project"
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
    VersionContext.load(project).publish_active_head(
        {"topic": "v3"},
        expected_head_id="v2",
        expected_revision=1,
        version_id="v3",
    )
    before = VersionContext.load(project).state()

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request_json(
            server,
            "POST",
            f"/api/project/{project.name}/history/undo",
            {
                "target_version_id": "v1",
                "branch_id": "rollback-v1",
                "branch_name": "Rollback v1",
                "version_id": "rollback-v1",
                "expected_head_id": "v3",
                "confirm": True,
                "expected_revision": before.revision,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert status == 200
    assert payload["result"] == "UNDONE"
    assert payload["write_mode"] == "VERSION_CONTEXT"
    assert payload["revision"] == before.revision + 1
    assert payload["current"]["version_id"] == "rollback-v1"
    assert payload["current"]["branch_id"] == "rollback-v1"
    history = {row["version_id"]: row for row in payload["history"]}
    assert set(history) == {"v1", "v2", "v3", "rollback-v1"}
    assert history["rollback-v1"]["parent_version_id"] == "v1"
    assert history["rollback-v1"]["is_current"] is True
    assert history["rollback-v1"]["is_active_head"] is True
    assert history["v3"]["branch_id"] == "main"
    assert history["v3"]["is_active_head"] is False
    assert history["v3"]["read_only"] is True

    after = VersionContext.load(project).state()
    assert after.current_version_id == "rollback-v1"
    assert after.active_branch_id == "rollback-v1"
    assert after.branch_heads == {"main": "v3", "rollback-v1": "rollback-v1"}
    assert after.revision == before.revision + 1


def test_project_history_branch_and_undo_reject_stale_invalid_unknown_without_writes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-negative-project"
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
    cases = (
        (
            "/history/branch",
            {
                "source_version_id": "v1",
                "branch_id": "invalid-branch",
                "branch_name": "Invalid branch",
                "version_id": "invalid-v1",
                "activate": False,
                "confirm": False,
                "expected_revision": before.revision,
            },
            400,
            "HISTORY_REQUEST_INVALID",
        ),
        (
            "/history/branch",
            {
                "source_version_id": "v1",
                "branch_id": "stale-branch",
                "branch_name": "Stale branch",
                "version_id": "stale-v1",
                "activate": False,
                "confirm": True,
                "expected_revision": before.revision + 1,
            },
            409,
            "STALE_REVISION",
        ),
        (
            "/history/branch",
            {
                "source_version_id": "unknown",
                "branch_id": "unknown-branch",
                "branch_name": "Unknown branch",
                "version_id": "unknown-v1",
                "activate": False,
                "confirm": True,
                "expected_revision": before.revision,
            },
            409,
            "VERSION_NOT_FOUND",
        ),
        (
            "/history/undo",
            {
                "target_version_id": "v1",
                "branch_id": "invalid-rollback",
                "branch_name": "Invalid rollback",
                "version_id": "invalid-rollback-v1",
                "expected_head_id": "v2",
                "confirm": False,
                "expected_revision": before.revision,
            },
            400,
            "HISTORY_REQUEST_INVALID",
        ),
        (
            "/history/undo",
            {
                "target_version_id": "v1",
                "branch_id": "stale-rollback",
                "branch_name": "Stale rollback",
                "version_id": "stale-rollback-v1",
                "expected_head_id": "v2",
                "confirm": True,
                "expected_revision": before.revision + 1,
            },
            409,
            "STALE_REVISION",
        ),
        (
            "/history/undo",
            {
                "target_version_id": "unknown",
                "branch_id": "unknown-rollback",
                "branch_name": "Unknown rollback",
                "version_id": "unknown-rollback-v1",
                "expected_head_id": "v2",
                "confirm": True,
                "expected_revision": before.revision,
            },
            409,
            "VERSION_NOT_FOUND",
        ),
    )

    dashboard.configure_runtime(tmp_path)
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path, payload, expected_status, expected_code in cases:
            status, response = _request_json(
                server,
                "POST",
                f"/api/project/{project.name}{path}",
                payload,
            )
            assert status == expected_status
            assert response["error_code"] == expected_code
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
