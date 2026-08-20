"""Focused runtime-root regression checks (stdlib only)."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from view import serve_review_dashboard as dashboard


class DashboardRuntimeRootTest(unittest.TestCase):
    def _request(
        self,
        server: dashboard.ThreadingHTTPServer,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
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

    def _binary_request(
        self,
        server: dashboard.ThreadingHTTPServer,
        path: str,
        body: bytes,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(*server.server_address, timeout=10)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_sidecar_data_root_is_writable_but_foreign_checkout_is_read_only(self) -> None:
        product_sidecar = Path("/home/kenqia/my_folder/test/review-projects")
        product_context = dashboard.configure_runtime(product_sidecar)
        self.assertEqual(product_context.mode, dashboard.WRITABLE)

        code_context = dashboard.configure_runtime(dashboard.REPO_ROOT)
        self.assertEqual(code_context.mode, dashboard.WRITABLE)

        with tempfile.TemporaryDirectory() as temporary:
            aggregate = Path(temporary) / "aggregate"
            aggregate.mkdir()
            sidecar = aggregate / "review-projects"
            sidecar.mkdir()
            code_root = aggregate / "product-package"
            code_root.mkdir()
            (aggregate / ".git").mkdir()

            self.assertTrue(
                dashboard._is_sidecar_review_data_root(sidecar, code_root, aggregate)
            )

            foreign_checkout = Path(temporary) / "foreign-checkout"
            foreign_checkout.mkdir()
            foreign_projects = foreign_checkout / "projects"
            foreign_projects.mkdir()
            (foreign_checkout / ".git").mkdir()

            self.assertFalse(
                dashboard._is_sidecar_review_data_root(
                    foreign_projects,
                    code_root,
                    foreign_checkout,
                )
            )

            foreign_context = dashboard.configure_runtime(foreign_projects)
            self.assertEqual(foreign_context.mode, dashboard.HISTORICAL_READ_ONLY)

    def test_sidecar_source_mapping_posts_a_main_record(self) -> None:
        source_root = Path("/home/kenqia/my_folder/test/review-projects")
        archive = (
            source_root
            / "nickel-coupling-review"
            / "00_sources/manual_upload/inbox/source_bundle.zip"
        )
        self.assertTrue(archive.is_file())
        with tempfile.TemporaryDirectory(dir=source_root, prefix=".runtime-root-test-") as temporary:
            review_root = Path(temporary)
            project_id = "source-mapping-check"
            context = dashboard.configure_runtime(review_root)
            self.assertEqual(context.mode, dashboard.WRITABLE)
            server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, _ = self._request(
                    server,
                    "POST",
                    "/api/projects",
                    {
                        "project_id": project_id,
                        "brief": {
                            "topic": "Runtime writable mapping regression",
                            "review_question": "Can an authorized source be assigned MAIN?",
                        },
                    },
                )
                self.assertEqual(status, 201)
                status, _ = self._request(
                    server,
                    "PUT",
                    f"/api/project/{project_id}/review-state",
                    {"action": "confirm_brief", "project_id": project_id},
                )
                self.assertEqual(status, 200)
                status, _ = self._binary_request(
                    server,
                    f"/api/project/{project_id}/source-archive",
                    archive.read_bytes(),
                )
                self.assertEqual(status, 201)
                status, sources = self._request(
                    server, "GET", f"/api/project/{project_id}/sources"
                )
                self.assertEqual(status, 200)
                preflight = sources["preflight"]
                self.assertIsInstance(preflight, dict)
                member = preflight["member"]
                self.assertIsInstance(member, dict)
                mapping = {
                    key: member[key]
                    for key in ("member_id", "download_id", "source_id", "study_id")
                }
                mapping.update(
                    {"document_role": "MAIN", "archive_sha256": preflight["archive_sha256"]}
                )
                status, mapped = self._request(
                    server,
                    "POST",
                    f"/api/project/{project_id}/source-mapping",
                    mapping,
                )
                self.assertEqual(status, 200)
                self.assertEqual(mapped["status"], "mapped")
                status, selected = self._request(
                    server, "GET", f"/api/project/{project_id}/sources"
                )
                self.assertEqual(status, 200)
                self.assertEqual(selected["sources"][0]["role"], "MAIN")
                self.assertEqual(selected["sources"][0]["currentness"], "current")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
