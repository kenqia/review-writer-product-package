"""Focused runtime-root regression checks (stdlib only)."""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from view import serve_review_dashboard as dashboard
from review_writer.agent.generator_runtime import GeneratorSession, RUNTIME_KEY, RUNTIME_SCHEMA
from review_writer.product_foundation import PersistenceError, VersionContext
from review_writer.project import manuscript_v2
from review_writer.project.source_truth import canonical_digest


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


class DashboardDraftVersionContextTest(unittest.TestCase):
    project_id = "dashboard-draft-context"
    section_id = "section-draft-1"
    original_body = "Original source-bound draft."
    edited_body = "Edited source-bound draft."

    def _project(self, *, candidate_digest: str = "a" * 64) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        review_root = Path(temporary.name)
        project = review_root / self.project_id
        (project / "00_brief").mkdir(parents=True)
        (project / "00_brief" / "review_state.json").write_text(
            json.dumps({"project_id": self.project_id}), encoding="utf-8"
        )
        (project / ".paper_evidence.lock").write_bytes(b"\0")
        manuscript_root = project / "04_manuscript"
        manuscript_root.mkdir()
        (manuscript_root / "section_drafts.jsonl").write_bytes(b'{"before":"draft"}\n')
        (manuscript_root / "manuscript.md").write_bytes(b"# Before\n")
        (manuscript_root / "manuscript_lineage.v2.json").write_bytes(b'{"before":"lineage"}\n')

        draft = {
            "section_id": self.section_id,
            "heading": "Bounded section",
            "body": self.original_body,
            "draft_digest": "a" * 64,
            "status": "needs_human_edit",
            "claim_bindings": [],
            "high_risk_reasons": ["Edited source-bound statement"],
        }
        decision = {
            "actor_type": "human_researcher",
            "actor_label": "研究者",
            "action": "approve",
            "reason": "Checked against the source.",
            "bound_object_digest": "b" * 64,
            "original_expression": self.original_body,
            "edited_expression": self.edited_body,
        }
        approved = {
            **copy.deepcopy(draft),
            "body": self.edited_body,
            "draft_digest": "b" * 64,
            "status": "approved",
            "decision": decision,
        }
        runtime = {
            "schema_version": RUNTIME_SCHEMA,
            "project_id": self.project_id,
            "session_id": "generator-session-dashboard-draft",
            "phase": "v1",
            "status": "HUMAN_ACTION_REQUIRED",
            "last_action": "GENERATE_CANDIDATE_V1",
            "last_run_id": "generator-run-v1",
            "next_action": {"project_id": self.project_id, "route": "/draft", "type": "HUMAN_ACTION_REQUIRED"},
            "input": {
                "section_id": self.section_id,
                "heading": "Bounded section",
                "v2_addition": "A bounded continuation [evidence:case-1].",
                "request_digest": "c" * 64,
            },
            "candidate": {
                "version": "v1",
                "section_id": self.section_id,
                "draft_digest": candidate_digest,
                "body_sha256": hashlib.sha256(self.original_body.encode("utf-8")).hexdigest(),
                "status": "needs_human_edit",
                "generation_digest": "d" * 64,
            },
            "human_decision": None,
            "audit": [],
        }
        VersionContext.create(
            {"currentness": "current", RUNTIME_KEY: runtime},
            project_id=self.project_id,
            version_id="v1",
            branch_id="main",
            branch_name="Main",
            project_root=project,
        )
        return review_root, project, draft, approved

    def _payload(self, draft: dict[str, object]) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "edited_body": self.edited_body,
            "reason": "Checked against the source.",
            "version_token": dashboard._workspace_token(
                "manuscript-section", self.section_id, draft["draft_digest"]
            ),
            "actor_type": "human_researcher",
            "actor_label": "研究者",
        }

    def test_reconfirm_simulated_approval_preserves_history_and_rebinds_a_human_decision(self) -> None:
        evidence = {"projection_digest": "e" * 64}
        synthesis = {"projection_digest": "s" * 64}
        contracts = {"projection_digest": "c" * 64}
        upstream = manuscript_v2._upstream_digest(evidence, synthesis, contracts)
        prior_decision = {
            "actor_type": "simulated_researcher_agent",
            "actor_label": "automated-qa",
            "action": "approve",
            "reason": "Automated QA decision.",
            "bound_object_digest": "a" * 64,
            "upstream_digest": upstream,
            "original_expression": self.original_body,
            "edited_expression": self.original_body,
        }
        row = {
            "section_id": self.section_id,
            "body": self.original_body,
            "draft_digest": "a" * 64,
            "status": "approved",
            "claim_bindings": [{"marker": "evidence:case-1"}],
            "high_risk_reasons": ["Edited source-bound statement"],
            "decision": copy.deepcopy(prior_decision),
        }
        rows = [row]
        writes: list[bytes] = []

        with (
            patch.object(manuscript_v2, "_root", return_value=Path("/legacy-reconfirm")),
            patch.object(manuscript_v2, "_read_jsonl", return_value=rows),
            patch.object(manuscript_v2, "_states", return_value=(evidence, synthesis, contracts)),
            patch.object(manuscript_v2, "_draft_is_current", return_value=True),
            patch.object(manuscript_v2, "project_write_lock", side_effect=lambda _: nullcontext()),
            patch.object(manuscript_v2, "_atomic_bytes", side_effect=lambda _, __, data: writes.append(data)),
        ):
            approved = manuscript_v2.approve_section(
                Path("/legacy-reconfirm"),
                self.section_id,
                {"actor_type": "human_researcher", "actor_label": "研究者"},
                edited_body=self.original_body,
                reason="I independently re-checked the source-bound text.",
                expected_draft_digest="a" * 64,
                reconfirm_simulated_approval=True,
            )

        self.assertEqual(len(writes), 1)
        self.assertEqual(approved["body"], self.original_body)
        self.assertEqual(approved["claim_bindings"], [{"marker": "evidence:case-1"}])
        self.assertEqual(approved["prior_decisions"], [prior_decision])
        self.assertNotEqual(approved["draft_digest"], "a" * 64)
        self.assertEqual(approved["decision"]["actor_type"], "human_researcher")
        self.assertEqual(approved["decision"]["bound_object_digest"], approved["draft_digest"])
        self.assertEqual(approved["decision"]["original_expression"], self.original_body)
        self.assertEqual(approved["decision"]["edited_expression"], self.original_body)

    def test_reconfirm_simulated_approval_refuses_normal_or_simulated_approvals_without_writes(self) -> None:
        evidence = {"projection_digest": "e" * 64}
        synthesis = {"projection_digest": "s" * 64}
        contracts = {"projection_digest": "c" * 64}
        upstream = manuscript_v2._upstream_digest(evidence, synthesis, contracts)

        for existing_actor, requested_actor in (
            ("human_researcher", "human_researcher"),
            ("simulated_researcher_agent", "simulated_researcher_agent"),
        ):
            with self.subTest(existing_actor=existing_actor, requested_actor=requested_actor):
                row = {
                    "section_id": self.section_id,
                    "body": self.original_body,
                    "draft_digest": "a" * 64,
                    "status": "approved",
                    "claim_bindings": [],
                    "high_risk_reasons": [],
                    "decision": {
                        "actor_type": existing_actor,
                        "actor_label": "prior actor",
                        "action": "approve",
                        "reason": "Prior decision.",
                        "bound_object_digest": "a" * 64,
                        "upstream_digest": upstream,
                        "original_expression": self.original_body,
                        "edited_expression": self.original_body,
                    },
                }
                writes: list[bytes] = []
                with (
                    patch.object(manuscript_v2, "_root", return_value=Path("/legacy-reconfirm")),
                    patch.object(manuscript_v2, "_read_jsonl", return_value=[row]),
                    patch.object(manuscript_v2, "_states", return_value=(evidence, synthesis, contracts)),
                    patch.object(manuscript_v2, "_draft_is_current", return_value=True),
                    patch.object(manuscript_v2, "project_write_lock", side_effect=lambda _: nullcontext()),
                    patch.object(manuscript_v2, "_atomic_bytes", side_effect=lambda _, __, data: writes.append(data)),
                ):
                    with self.assertRaises(manuscript_v2.ManuscriptV2Error) as error:
                        manuscript_v2.approve_section(
                            Path("/legacy-reconfirm"),
                            self.section_id,
                            {"actor_type": requested_actor, "actor_label": "request actor"},
                            edited_body=self.original_body,
                            reason="A real reason.",
                            expected_draft_digest="a" * 64,
                            reconfirm_simulated_approval=True,
                        )
                self.assertEqual(error.exception.code, "LEGACY_RECONFIRM_NOT_ALLOWED")
                self.assertEqual(writes, [])

    def test_reconfirm_simulated_approval_requires_a_human_reason_without_writes(self) -> None:
        evidence = {"projection_digest": "e" * 64}
        synthesis = {"projection_digest": "s" * 64}
        contracts = {"projection_digest": "c" * 64}
        row = {
            "section_id": self.section_id,
            "body": self.original_body,
            "draft_digest": "a" * 64,
            "status": "approved",
            "claim_bindings": [],
            "high_risk_reasons": [],
            "decision": {
                "actor_type": "simulated_researcher_agent",
                "actor_label": "automated-qa",
                "action": "approve",
                "reason": "Automated QA decision.",
                "bound_object_digest": "a" * 64,
                "upstream_digest": manuscript_v2._upstream_digest(evidence, synthesis, contracts),
                "original_expression": self.original_body,
                "edited_expression": self.original_body,
            },
        }
        writes: list[bytes] = []
        with (
            patch.object(manuscript_v2, "_root", return_value=Path("/legacy-reconfirm")),
            patch.object(manuscript_v2, "_read_jsonl", return_value=[row]),
            patch.object(manuscript_v2, "_states", return_value=(evidence, synthesis, contracts)),
            patch.object(manuscript_v2, "_draft_is_current", return_value=True),
            patch.object(manuscript_v2, "project_write_lock", side_effect=lambda _: nullcontext()),
            patch.object(manuscript_v2, "_atomic_bytes", side_effect=lambda _, __, data: writes.append(data)),
        ):
            with self.assertRaises(manuscript_v2.ManuscriptV2Error) as error:
                manuscript_v2.approve_section(
                    Path("/legacy-reconfirm"),
                    self.section_id,
                    {"actor_type": "human_researcher", "actor_label": "研究者"},
                    edited_body=self.original_body,
                    reason="",
                    expected_draft_digest="a" * 64,
                    reconfirm_simulated_approval=True,
                )
        self.assertEqual(error.exception.code, "APPROVAL_REASON_REQUIRED")
        self.assertEqual(writes, [])

    def test_new_route_legacy_reconfirm_publishes_human_decision_and_rejects_candidate_mismatch_without_writes(self) -> None:
        review_root, project, _, _ = self._project()
        manuscript_root = project / "04_manuscript"
        legacy = {
            "section_id": self.section_id,
            "heading": "Bounded section",
            "body": self.original_body,
            "draft_digest": "a" * 64,
            "status": "approved",
            "claim_bindings": [],
            "high_risk_reasons": ["Edited source-bound statement"],
            "decision": {
                "actor_type": "simulated_researcher_agent",
                "actor_label": "automated-qa",
                "action": "approve",
                "reason": "Automated QA decision.",
                "bound_object_digest": "a" * 64,
            },
        }
        approved = {
            **copy.deepcopy(legacy),
            "draft_digest": "b" * 64,
            "decision": {
                "actor_type": "human_researcher",
                "actor_label": "研究者",
                "action": "approve",
                "reason": "I independently re-checked the source-bound text.",
                "bound_object_digest": "b" * 64,
                "original_expression": self.original_body,
                "edited_expression": self.original_body,
            },
            "prior_decisions": [copy.deepcopy(legacy["decision"])],
        }
        payload = self._payload(legacy)
        payload.update(
            {
                "edited_body": self.original_body,
                "reason": "I independently re-checked the source-bound text.",
                "reconfirm_simulated_approval": True,
            }
        )

        def approve(_: Path, section_id: str, actor: object, **kwargs: object) -> dict[str, object]:
            self.assertEqual(section_id, self.section_id)
            self.assertEqual(actor, {"actor_type": "human_researcher", "actor_label": "研究者"})
            self.assertTrue(kwargs["reconfirm_simulated_approval"])
            (manuscript_root / "section_drafts.jsonl").write_bytes(b'{"after":"draft"}\n')
            return copy.deepcopy(approved)

        def merge(_: Path) -> dict[str, object]:
            (manuscript_root / "manuscript.md").write_bytes(b"# After\n")
            (manuscript_root / "manuscript_lineage.v2.json").write_bytes(b'{"after":"lineage"}\n')
            return {"status": "approved"}

        with (
            patch.object(dashboard, "build_manuscript_workspace", side_effect=[{"sections": [legacy]}, {"sections": [approved]}]),
            patch.object(dashboard, "approve_section", side_effect=approve),
            patch.object(dashboard, "merge_authoritative_manuscript", side_effect=merge),
        ):
            dashboard._write_new_route_draft_section(review_root, self.project_id, payload)

        context = VersionContext.load(project)
        state = context.state()
        runtime = context.view_version(state.current_version_id).snapshot[RUNTIME_KEY]
        self.assertEqual(state.revision, 1)
        self.assertEqual(runtime["human_decision"]["actor_type"], "human_researcher")
        self.assertEqual(runtime["human_decision"]["draft_digest"], "b" * 64)
        with (
            patch(
                "review_writer.agent.generator_runtime.build_manuscript_workspace",
                return_value={"sections": [approved]},
            ),
            patch(
                "review_writer.agent.generator_runtime.generate_section_draft_v2",
                return_value={
                    "draft_digest": "d" * 64,
                    "body": f"{self.original_body}\n\nA bounded continuation [evidence:case-1].",
                    "status": "needs_human_edit",
                },
            ),
        ):
            continuation = GeneratorSession(project).continue_session(
                "generator-session-dashboard-draft"
            )
        self.assertEqual(continuation["candidate"]["version"], "v2")

        mismatch_root, mismatch_project, _, _ = self._project(candidate_digest="f" * 64)
        before = {
            path: path.read_bytes()
            for path in (
                mismatch_project / "04_manuscript/section_drafts.jsonl",
                mismatch_project / "04_manuscript/manuscript.md",
                mismatch_project / "04_manuscript/manuscript_lineage.v2.json",
                mismatch_project / ".review-writer/version_context/current.json",
            )
        }
        with (
            patch.object(dashboard, "build_manuscript_workspace", return_value={"sections": [legacy]}),
            patch.object(dashboard, "approve_section") as mismatch_approve,
            patch.object(dashboard, "merge_authoritative_manuscript") as mismatch_merge,
        ):
            with self.assertRaises(dashboard.WorkspaceStaleError):
                dashboard._write_new_route_draft_section(mismatch_root, self.project_id, payload)
        mismatch_approve.assert_not_called()
        mismatch_merge.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_new_route_approval_publishes_the_same_generator_session_before_v2_continue(self) -> None:
        review_root, project, draft, approved = self._project()
        manuscript_root = project / "04_manuscript"

        def approve(_: Path, section_id: str, actor: object, **kwargs: object) -> dict[str, object]:
            self.assertEqual(section_id, self.section_id)
            self.assertEqual(actor, {"actor_type": "human_researcher", "actor_label": "研究者"})
            self.assertEqual(kwargs["expected_draft_digest"], draft["draft_digest"])
            (manuscript_root / "section_drafts.jsonl").write_bytes(b'{"after":"draft"}\n')
            return copy.deepcopy(approved)

        def merge(_: Path) -> dict[str, object]:
            (manuscript_root / "manuscript.md").write_bytes(b"# After\n")
            (manuscript_root / "manuscript_lineage.v2.json").write_bytes(b'{"after":"lineage"}\n')
            return {"status": "approved"}

        with (
            patch.object(
                dashboard,
                "build_manuscript_workspace",
                side_effect=[{"sections": [draft]}, {"sections": [approved], "status": "approved"}],
            ),
            patch.object(dashboard, "approve_section", side_effect=approve),
            patch.object(dashboard, "merge_authoritative_manuscript", side_effect=merge),
        ):
            dashboard._write_new_route_draft_section(review_root, self.project_id, self._payload(draft))

        context = VersionContext.load(project)
        state = context.state()
        current = context.view_version(state.current_version_id)
        runtime = current.snapshot[RUNTIME_KEY]
        self.assertEqual(state.revision, 1)
        self.assertEqual(runtime["session_id"], "generator-session-dashboard-draft")
        self.assertEqual(runtime["phase"], "v1")
        self.assertEqual(
            runtime["human_decision"],
            {
                "section_id": self.section_id,
                "decision_digest": canonical_digest(approved["decision"]),
                "edited_body_sha256": hashlib.sha256(self.edited_body.encode("utf-8")).hexdigest(),
                "draft_digest": approved["draft_digest"],
                "actor_type": "human_researcher",
                "actor_label": "研究者",
            },
        )

        with (
            patch(
                "review_writer.agent.generator_runtime.build_manuscript_workspace",
                return_value={"sections": [approved]},
            ),
            patch(
                "review_writer.agent.generator_runtime.generate_section_draft_v2",
                return_value={
                    "draft_digest": "e" * 64,
                    "body": f"{self.edited_body}\n\nA bounded continuation [evidence:case-1].",
                    "status": "needs_human_edit",
                },
            ),
        ):
            continuation = GeneratorSession(project).continue_session(
                "generator-session-dashboard-draft"
            )
        self.assertEqual(continuation["candidate"]["version"], "v2")

    def test_new_route_approval_restores_all_manuscript_bytes_when_context_publish_fails(self) -> None:
        review_root, project, draft, approved = self._project()
        manuscript_root = project / "04_manuscript"
        tracked = [
            manuscript_root / "section_drafts.jsonl",
            manuscript_root / "manuscript.md",
            manuscript_root / "manuscript_lineage.v2.json",
        ]
        before = {path: path.read_bytes() for path in tracked}
        current_path = project / ".review-writer/version_context/current.json"
        current_before = current_path.read_bytes()
        versions_before = sorted((project / ".review-writer/version_context/versions").iterdir())

        def approve(_: Path, __: str, ___: object, **____: object) -> dict[str, object]:
            tracked[0].write_bytes(b'{"after":"draft"}\n')
            return copy.deepcopy(approved)

        def merge(_: Path) -> dict[str, object]:
            tracked[1].write_bytes(b"# After\n")
            tracked[2].write_bytes(b'{"after":"lineage"}\n')
            return {"status": "approved"}

        with (
            patch.object(
                dashboard,
                "build_manuscript_workspace",
                return_value={"sections": [draft]},
            ),
            patch.object(dashboard, "approve_section", side_effect=approve),
            patch.object(dashboard, "merge_authoritative_manuscript", side_effect=merge),
            patch.object(
                VersionContext,
                "publish_active_head",
                side_effect=PersistenceError("injected context failure"),
            ),
        ):
            with self.assertRaises(dashboard.WorkspaceStaleError):
                dashboard._write_new_route_draft_section(review_root, self.project_id, self._payload(draft))

        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(current_path.read_bytes(), current_before)
        self.assertEqual(sorted((project / ".review-writer/version_context/versions").iterdir()), versions_before)

    def test_new_route_approval_rejects_a_runtime_candidate_digest_mismatch_without_writes(self) -> None:
        review_root, project, draft, _ = self._project(candidate_digest="f" * 64)
        manuscript_root = project / "04_manuscript"
        tracked = [
            manuscript_root / "section_drafts.jsonl",
            manuscript_root / "manuscript.md",
            manuscript_root / "manuscript_lineage.v2.json",
        ]
        before = {path: path.read_bytes() for path in tracked}
        current_path = project / ".review-writer/version_context/current.json"
        current_before = current_path.read_bytes()

        with (
            patch.object(
                dashboard,
                "build_manuscript_workspace",
                return_value={"sections": [draft]},
            ),
            patch.object(dashboard, "approve_section") as approve,
            patch.object(dashboard, "merge_authoritative_manuscript") as merge,
        ):
            with self.assertRaises(dashboard.WorkspaceStaleError):
                dashboard._write_new_route_draft_section(review_root, self.project_id, self._payload(draft))

        approve.assert_not_called()
        merge.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(current_path.read_bytes(), current_before)


if __name__ == "__main__":
    unittest.main()
