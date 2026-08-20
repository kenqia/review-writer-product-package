"""Focused contract checks for the Agent-facing synthesis workspace producer."""

from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from review_writer.agent import local_pdf_parse
from review_writer.agent.generator_runtime import (
    GeneratorRuntimeError,
    GeneratorSession,
)
from review_writer.project.manuscript_v2 import ManuscriptV2Error
from review_writer.project import section_contract as section_contract_producer
from review_writer.project import synthesis as synthesis_producer


class PdfOnlySynthesisWorkspacePlanTest(unittest.TestCase):
    def test_one_approved_pdf_only_study_builds_a_bounded_review_plan(self) -> None:
        self.assertTrue(
            hasattr(local_pdf_parse, "build_pdf_only_synthesis_plan"),
            "approved PDF-only Evidence needs an Agent-facing synthesis producer",
        )
        evidence_digest = "a" * 64
        plan = local_pdf_parse.build_pdf_only_synthesis_plan(
            {
                "projection_digest": evidence_digest,
                "workflow_can_continue": True,
                "rows": [
                    {
                        "evidence_id": "evidence-case-1",
                        "study_id": "study-case-1",
                        "status": "approved",
                        "statement": "The source reports a bounded observation.",
                        "field_dependencies": [],
                        "limitations": ["Chemical GAP: structure fields are unsupported."],
                        "risk_classes": ["GAP", "NON_COMPARABLE"],
                    }
                ],
            }
        )

        protocol = plan["comparison_protocol"]
        claim = plan["synthesis_claim"]
        contract = plan["section_contract"]
        self.assertEqual(protocol["paper_evidence_projection_digest"], evidence_digest)
        self.assertEqual(protocol["comparison_objects"], ["evidence-case-1"])
        self.assertIn("single-study", protocol["claim_strength"])
        self.assertEqual(claim["supporting_evidence_ids"], ["evidence-case-1"])
        self.assertTrue(claim["single_study"])
        self.assertEqual(claim["counter_evidence_ids"], [])
        self.assertIn("Chemical GAP", claim["uncertainty"])
        self.assertNotIn("SMILES", claim["proposition"])
        self.assertNotIn("molecule", claim["proposition"].lower())
        self.assertEqual(contract["evidence_budget"], 1)
        self.assertEqual(contract["synthesis_budget"], 1)
        self.assertTrue(contract["figure_plan"])
        self.assertIn("Chemical GAP", " ".join(contract["counterevidence_and_limitations"]))

    def test_three_approved_pdf_only_studies_build_a_bounded_multi_study_plan(self) -> None:
        evidence_digest = "e" * 64
        evidence_rows = [
            {
                "evidence_id": f"evidence-case-{index}",
                "study_id": f"study-case-{index}",
                "status": "approved",
                "statement": f"Study {index} reports a bounded observation.",
                "field_dependencies": [],
                "limitations": ["Chemical GAP: structure fields are unsupported."],
                "risk_classes": ["GAP"],
            }
            for index in range(1, 4)
        ]

        plan = local_pdf_parse.build_pdf_only_synthesis_plan(
            {
                "projection_digest": evidence_digest,
                "workflow_can_continue": True,
                "rows": evidence_rows,
            }
        )

        protocol = plan["comparison_protocol"]
        claim = plan["synthesis_claim"]
        self.assertEqual(
            protocol["comparison_objects"],
            ["evidence-case-1", "evidence-case-2", "evidence-case-3"],
        )
        self.assertIn("multi-study", protocol["claim_strength"])
        self.assertFalse(claim["single_study"])
        self.assertEqual(claim["supporting_evidence_ids"], protocol["comparison_objects"])
        self.assertIn("Chemical GAP", claim["uncertainty"])
        self.assertEqual(plan["section_contract"]["evidence_budget"], 3)

    def test_missing_or_non_approved_evidence_is_rejected_without_a_plan(self) -> None:
        self.assertTrue(
            hasattr(local_pdf_parse, "build_pdf_only_synthesis_plan"),
            "approved PDF-only Evidence needs an Agent-facing synthesis producer",
        )
        if not hasattr(local_pdf_parse, "build_pdf_only_synthesis_plan"):
            return
        with self.assertRaises(local_pdf_parse.LocalPdfParseError) as missing:
            local_pdf_parse.build_pdf_only_synthesis_plan(
                {"projection_digest": "b" * 64, "rows": []}
            )
        self.assertEqual(missing.exception.code, "PAPER_EVIDENCE_NOT_APPROVED")

    def test_stale_or_unapproved_multi_study_evidence_is_rejected(self) -> None:
        for status in ("stale", "needs_review"):
            with self.subTest(status=status):
                with self.assertRaises(local_pdf_parse.LocalPdfParseError) as blocked:
                    local_pdf_parse.build_pdf_only_synthesis_plan(
                        {
                            "projection_digest": "f" * 64,
                            "workflow_can_continue": True,
                            "rows": [
                                {
                                    "evidence_id": "evidence-stale-1",
                                    "study_id": "study-stale-1",
                                    "status": status,
                                    "statement": "A stale or unapproved observation.",
                                    "field_dependencies": [],
                                },
                                {
                                    "evidence_id": "evidence-stale-2",
                                    "study_id": "study-stale-2",
                                    "status": "approved",
                                    "statement": "A second observation.",
                                    "field_dependencies": [],
                                },
                            ],
                        }
                    )
                self.assertEqual(blocked.exception.code, "PAPER_EVIDENCE_NOT_APPROVED")

    def test_duplicate_evidence_or_cross_study_source_binding_is_rejected(self) -> None:
        duplicate_rows = [
            {
                "evidence_id": "evidence-duplicate",
                "study_id": study_id,
                "status": "approved",
                "statement": "A source-bound observation.",
                "field_dependencies": [],
            }
            for study_id in ("study-1", "study-2")
        ]
        mismatched_source_rows = [
            {
                "evidence_id": f"evidence-source-{index}",
                "study_id": study_id,
                "source_id": "source-owned-by-another-study",
                "status": "approved",
                "statement": "A source-bound observation.",
                "field_dependencies": [],
            }
            for index, study_id in enumerate(("study-1", "study-2"), start=1)
        ]
        for rows in (duplicate_rows, mismatched_source_rows):
            with self.subTest(rows=rows):
                with self.assertRaises(local_pdf_parse.LocalPdfParseError) as blocked:
                    local_pdf_parse.build_pdf_only_synthesis_plan(
                        {
                            "projection_digest": "1" * 64,
                            "workflow_can_continue": True,
                            "rows": rows,
                        }
                    )
                self.assertEqual(
                    blocked.exception.code,
                    "PDF_ONLY_SYNTHESIS_EVIDENCE_INVALID",
                )

    def test_native_n3_prepare_persists_only_protocol_candidate_and_stops_for_human_decision(self) -> None:
        evidence_digest = "2" * 64
        evidence = {
            "projection_digest": evidence_digest,
            "workflow_can_continue": True,
            "rows": [
                {
                    "evidence_id": f"evidence-native-{index}",
                    "study_id": f"study-native-{index}",
                    "status": "approved",
                    "statement": f"Study {index} reports a bounded observation.",
                    "field_dependencies": [],
                    "limitations": ["Chemical GAP: structure fields are unsupported."],
                    "risk_classes": ["GAP"],
                }
                for index in range(1, 4)
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "native-n3"
            (project / "01_evidence").mkdir(parents=True)
            (project / "02_synthesis").mkdir()
            (project / ".paper_evidence.lock").write_bytes(b"\0")
            state = SimpleNamespace(revision=4, active_head_id="head-native")
            current = SimpleNamespace(
                snapshot={"agent_parse": {"session_id": "generator-native", "tool_trace": []}},
                version_id="version-native",
                snapshot_digest="3" * 64,
            )
            trace = {
                "current": {
                    "version_id": "version-protocol",
                    "revision": 5,
                    "snapshot_digest": "4" * 64,
                }
            }
            with (
                patch.object(local_pdf_parse, "_registered_project", return_value=project),
                patch.object(
                    local_pdf_parse,
                    "_active_parse_session",
                    return_value=("generator-native", state, current),
                ),
                patch.object(local_pdf_parse, "paper_evidence_state", return_value=evidence),
                patch.object(
                    local_pdf_parse,
                    "comparison_protocol_state",
                    return_value={"workflow_can_continue": False},
                ),
                patch(
                    "review_writer.project.synthesis.paper_evidence_state",
                    return_value=evidence,
                ),
                patch.object(
                    local_pdf_parse,
                    "record_agent_tool_outcome",
                    return_value=trace,
                ) as record,
            ):
                result = local_pdf_parse.prepare_pdf_only_synthesis_workspace(
                    project,
                    session_id="generator-native",
                    expected_revision=4,
                    expected_head_id="head-native",
                )

            self.assertEqual(result["status"], "HUMAN_ACTION_REQUIRED")
            self.assertEqual(result["reason_code"], "SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED")
            record.assert_called_once()
            protocol_path = project / "02_synthesis/comparison_protocol.json"
            self.assertTrue(protocol_path.is_file())
            protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
            self.assertEqual(
                protocol["comparison_objects"],
                ["evidence-native-1", "evidence-native-2", "evidence-native-3"],
            )
            self.assertIn("multi-study", protocol["claim_strength"])
            self.assertFalse((project / "02_synthesis/coverage_map.json").exists())
            self.assertFalse((project / "02_synthesis/synthesis_claim_projection.jsonl").exists())

    def test_second_stage_action_names_multi_study_and_preserves_single_study_label(self) -> None:
        def run_prepare(study_count: int) -> dict[str, object]:
            evidence = {
                "projection_digest": "a" * 64,
                "workflow_can_continue": True,
                "rows": [
                    {
                        "evidence_id": f"evidence-action-{index}",
                        "study_id": f"study-action-{index}",
                        "status": "approved",
                        "statement": f"Study {index} reports a bounded observation.",
                        "field_dependencies": [],
                    }
                    for index in range(1, study_count + 1)
                ],
            }
            with tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary) / f"action-n{study_count}"
                (project / "01_evidence").mkdir(parents=True)
                (project / "02_synthesis").mkdir()
                (project / ".paper_evidence.lock").write_bytes(b"\0")
                (project / "02_synthesis/comparison_protocol.json").write_text(
                    "{}\n", encoding="utf-8"
                )
                state = SimpleNamespace(revision=4, active_head_id="head-action")
                current = SimpleNamespace(
                    snapshot={
                        "agent_parse": {
                            "session_id": "generator-action",
                            "tool_trace": [],
                        }
                    },
                    version_id="version-action",
                    snapshot_digest="b" * 64,
                )
                trace = {
                    "current": {
                        "version_id": "version-coverage",
                        "revision": 5,
                        "snapshot_digest": "c" * 64,
                    }
                }
                with (
                    patch.object(local_pdf_parse, "_registered_project", return_value=project),
                    patch.object(
                        local_pdf_parse,
                        "_active_parse_session",
                        return_value=("generator-action", state, current),
                    ),
                    patch.object(local_pdf_parse, "paper_evidence_state", return_value=evidence),
                    patch.object(
                        local_pdf_parse,
                        "comparison_protocol_state",
                        return_value={"workflow_can_continue": True},
                    ),
                    patch.object(
                        local_pdf_parse,
                        "coverage_map_state",
                        return_value={"workflow_can_continue": True},
                    ),
                    patch.object(
                        local_pdf_parse,
                        "synthesis_state",
                        return_value={"workflow_can_continue": True},
                    ),
                    patch.object(local_pdf_parse, "register_coverage_map", return_value={}),
                    patch.object(
                        local_pdf_parse,
                        "register_synthesis_candidates",
                        return_value={},
                    ),
                    patch.object(
                        local_pdf_parse,
                        "record_agent_tool_outcome",
                        return_value=trace,
                    ),
                ):
                    return local_pdf_parse.prepare_pdf_only_synthesis_workspace(
                        project,
                        session_id="generator-action",
                        expected_revision=4,
                        expected_head_id="head-action",
                    )

        self.assertEqual(
            run_prepare(1)["action"],
            "CREATE_SINGLE_STUDY_SYNTHESIS_CANDIDATE",
        )
        self.assertEqual(
            run_prepare(3)["action"],
            "CREATE_MULTI_STUDY_SYNTHESIS_CANDIDATE",
        )

    def test_multi_study_plan_round_trips_through_existing_synthesis_producers(self) -> None:
        evidence = {
            "projection_digest": "5" * 64,
            "workflow_can_continue": True,
            "rows": [
                {
                    "evidence_id": f"evidence-roundtrip-{index}",
                    "study_id": f"study-roundtrip-{index}",
                    "status": "approved",
                    "statement": f"Study {index} reports a bounded observation.",
                    "field_dependencies": [],
                }
                for index in range(1, 4)
            ],
        }
        plan = local_pdf_parse.build_pdf_only_synthesis_plan(evidence)
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "roundtrip"
            (project / "01_evidence").mkdir(parents=True)
            (project / "02_synthesis").mkdir()
            (project / ".paper_evidence.lock").write_bytes(b"\0")
            with patch(
                "review_writer.project.synthesis.paper_evidence_state",
                return_value=evidence,
            ):
                protocol = synthesis_producer.register_comparison_protocol(
                    project, plan["comparison_protocol"]
                )
                synthesis_producer.apply_comparison_protocol_decision(
                    project,
                    {"action": "approve", "reason": "Reviewed the bounded protocol."},
                )
                coverage = synthesis_producer.register_coverage_map(
                    project, plan["coverage_map"]
                )
                claims = synthesis_producer.register_synthesis_candidates(
                    project, plan["synthesis_claim"]
                )
                synthesis_producer.apply_synthesis_decision(
                    project,
                    {
                        "synthesis_id": plan["synthesis_claim"]["synthesis_id"],
                        "action": "approve",
                        "reason": "Reviewed the bounded multi-study candidate.",
                    },
                )
                contracts = section_contract_producer.register_section_contracts(
                    project, plan["section_contract"]
                )

            self.assertEqual(protocol["paper_evidence_projection_digest"], evidence["projection_digest"])
            self.assertEqual(coverage["comparison_id"], plan["comparison_protocol"]["comparison_id"])
            self.assertEqual(len(claims["claims"]), 1)
            self.assertFalse(claims["claims"][0]["single_study"])
            self.assertEqual(
                claims["claims"][0]["supporting_evidence_ids"],
                ["evidence-roundtrip-1", "evidence-roundtrip-2", "evidence-roundtrip-3"],
            )
            self.assertEqual(len(contracts["contracts"]), 1)
            self.assertEqual(
                contracts["contracts"][0]["evidence_budget"],
                3,
            )

    def test_stale_and_revision_conflict_native_prepare_paths_are_zero_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "zero-write"
            (project / "01_evidence").mkdir(parents=True)
            (project / "02_synthesis").mkdir()
            (project / ".paper_evidence.lock").write_bytes(b"\0")
            sentinel = project / "02_synthesis/sentinel.json"
            sentinel.write_text('{"before": true}\n', encoding="utf-8")
            before = sentinel.read_bytes()
            state = SimpleNamespace(revision=7, active_head_id="head-zero")
            current = SimpleNamespace(
                snapshot={"agent_parse": {"session_id": "generator-zero", "tool_trace": []}},
                version_id="version-zero",
                snapshot_digest="6" * 64,
            )
            stale_evidence = {
                "projection_digest": "7" * 64,
                "workflow_can_continue": False,
                "rows": [
                    {
                        "evidence_id": "evidence-stale",
                        "study_id": "study-stale",
                        "status": "stale",
                        "statement": "A stale observation.",
                        "field_dependencies": [],
                    }
                ],
            }
            with (
                patch.object(local_pdf_parse, "_registered_project", return_value=project),
                patch.object(
                    local_pdf_parse,
                    "_active_parse_session",
                    return_value=("generator-zero", state, current),
                ),
                patch.object(local_pdf_parse, "paper_evidence_state", return_value=stale_evidence),
                patch.object(local_pdf_parse, "register_comparison_protocol") as register,
            ):
                with self.assertRaises(local_pdf_parse.LocalPdfParseError) as stale:
                    local_pdf_parse.prepare_pdf_only_synthesis_workspace(
                        project,
                        session_id="generator-zero",
                        expected_revision=7,
                        expected_head_id="head-zero",
                    )
            self.assertEqual(stale.exception.code, "PAPER_EVIDENCE_NOT_APPROVED")
            register.assert_not_called()
            self.assertEqual(sentinel.read_bytes(), before)

            with (
                patch.object(local_pdf_parse, "_registered_project", return_value=project),
                patch.object(
                    local_pdf_parse,
                    "_active_parse_session",
                    return_value=("generator-zero", state, current),
                ),
                patch.object(local_pdf_parse, "paper_evidence_state") as evidence_state,
            ):
                with self.assertRaises(local_pdf_parse.LocalPdfParseError) as conflict:
                    local_pdf_parse.prepare_pdf_only_synthesis_workspace(
                        project,
                        session_id="generator-zero",
                        expected_revision=8,
                        expected_head_id="head-zero",
                    )
            self.assertEqual(conflict.exception.code, "GENERATOR_VERSION_CONFLICT")
            evidence_state.assert_not_called()
            self.assertEqual(sentinel.read_bytes(), before)

    def test_generator_keeps_the_original_gate_when_pdf_only_producer_is_inapplicable(self) -> None:
        """A non-PDF-only project must not receive a misleading producer error."""

        session = object.__new__(GeneratorSession)
        session.root = Path("/isolated/non-pdf-only-project")
        request = {
            "session_id": "generator-session-case",
            "section_id": "section-case",
            "heading": "Bounded heading",
            "body": "Bounded body.",
            "v2_addition": "Bounded continuation.",
        }
        with (
            patch(
                "review_writer.agent.generator_runtime._load_current",
                return_value=(
                    object(),
                    object(),
                    SimpleNamespace(snapshot={}),
                ),
            ),
            patch("review_writer.agent.generator_runtime._runtime", return_value=None),
            patch(
                "review_writer.agent.generator_runtime._capture_draft_state",
                return_value=(False, None),
            ),
            patch(
                "review_writer.agent.generator_runtime.project_write_lock",
                return_value=nullcontext(),
            ),
            patch("review_writer.agent.generator_runtime._restore_draft_state"),
            patch(
                "review_writer.agent.generator_runtime.register_section_draft",
                side_effect=ManuscriptV2Error("SYNTHESIS_NOT_APPROVED"),
            ),
            patch(
                "review_writer.agent.local_pdf_parse.prepare_pdf_only_synthesis_workspace",
                side_effect=local_pdf_parse.LocalPdfParseError(
                    "PAPER_EVIDENCE_NOT_APPROVED"
                ),
            ),
        ):
            with self.assertRaises(GeneratorRuntimeError) as blocked:
                session.start(request)

        self.assertEqual(blocked.exception.code, "GENERATOR_TOOL_FAILED")
        self.assertEqual(blocked.exception.tool_code, "SYNTHESIS_NOT_APPROVED")

    def test_native_generator_start_routes_the_multi_study_handoff_after_the_draft_gate(self) -> None:
        session = object.__new__(GeneratorSession)
        session.root = Path("/isolated/multi-study-project")
        request = {
            "session_id": "generator-session-multi",
            "section_id": "section-multi",
            "heading": "Bounded multi-study synthesis",
            "body": "Study-bound observations.",
            "v2_addition": "Study-bound continuation.",
        }
        handoff = {
            "status": "HUMAN_ACTION_REQUIRED",
            "reason_code": "SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED",
        }
        with (
            patch(
                "review_writer.agent.generator_runtime._load_current",
                return_value=(object(), object(), SimpleNamespace(snapshot={})),
            ),
            patch("review_writer.agent.generator_runtime._runtime", return_value=None),
            patch(
                "review_writer.agent.generator_runtime._capture_draft_state",
                return_value=(False, None),
            ),
            patch(
                "review_writer.agent.generator_runtime.project_write_lock",
                return_value=nullcontext(),
            ),
            patch("review_writer.agent.generator_runtime._restore_draft_state"),
            patch(
                "review_writer.agent.generator_runtime.register_section_draft",
                side_effect=ManuscriptV2Error("SYNTHESIS_NOT_APPROVED"),
            ),
            patch(
                "review_writer.agent.local_pdf_parse.prepare_pdf_only_synthesis_workspace",
                return_value=handoff,
            ) as prepare,
        ):
            self.assertEqual(session.start(request), handoff)
        prepare.assert_called_once_with(session.root)

    def test_resume_reuses_the_parse_session_to_create_the_next_synthesis_candidate(self) -> None:
        """A root resume must reach the same candidate producer before v1 exists."""

        session = object.__new__(GeneratorSession)
        session.root = Path("/isolated/pdf-only-project")
        state = SimpleNamespace(revision=3, active_head_id="head-case")
        current = SimpleNamespace(snapshot={})
        handoff = {
            "status": "HUMAN_ACTION_REQUIRED",
            "reason_code": "SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED",
        }
        with (
            patch(
                "review_writer.agent.generator_runtime._load_current",
                return_value=(object(), state, current),
            ),
            patch("review_writer.agent.generator_runtime._runtime", return_value=None),
            patch(
                "review_writer.agent.local_pdf_parse.prepare_pdf_only_synthesis_workspace",
                return_value=handoff,
            ) as prepare,
        ):
            self.assertEqual(
                session.continue_session("generator-session-case"),
                handoff,
            )

        prepare.assert_called_once_with(
            session.root,
            session_id="generator-session-case",
            expected_revision=3,
            expected_head_id="head-case",
        )


class PdfOnlyV1RequestTest(unittest.TestCase):
    def test_approved_pdf_only_workspace_builds_a_marked_bounded_v1_request(self) -> None:
        evidence = {
            "projection_digest": "c" * 64,
            "workflow_can_continue": True,
            "rows": [
                {
                    "evidence_id": "evidence-case-1",
                    "study_id": "study-case-1",
                    "status": "approved",
                    "statement": "The source reports a bounded observation.",
                    "field_dependencies": [],
                }
            ],
        }
        plan = local_pdf_parse.build_pdf_only_synthesis_plan(evidence)
        request = local_pdf_parse.build_pdf_only_v1_request(
            evidence,
            {
                "workflow_can_continue": True,
                "rows": [{**plan["synthesis_claim"], "status": "approved"}],
            },
            {"sections": [plan["section_contract"]]},
            session_id="generator-session-case",
        )

        self.assertEqual(request["session_id"], "generator-session-case")
        self.assertEqual(request["section_id"], plan["section_contract"]["section_id"])
        self.assertIn("[evidence:evidence-case-1]", request["body"])
        self.assertIn(
            f"[synthesis:{plan['synthesis_claim']['synthesis_id']}]", request["body"]
        )
        self.assertIn("Chemical GAP", request["body"])
        self.assertIn("Single-study", request["body"])
        self.assertNotIn("SMILES", request["body"])
        self.assertNotIn("molecule", request["body"].lower())
        self.assertIn("[synthesis:", request["v2_addition"])

    def test_unapproved_synthesis_cannot_build_a_v1_request(self) -> None:
        evidence = {
            "projection_digest": "d" * 64,
            "workflow_can_continue": True,
            "rows": [
                {
                    "evidence_id": "evidence-case-1",
                    "study_id": "study-case-1",
                    "status": "approved",
                    "statement": "The source reports a bounded observation.",
                    "field_dependencies": [],
                }
            ],
        }
        plan = local_pdf_parse.build_pdf_only_synthesis_plan(evidence)
        with self.assertRaises(local_pdf_parse.LocalPdfParseError) as blocked:
            local_pdf_parse.build_pdf_only_v1_request(
                evidence,
                {"workflow_can_continue": False, "rows": []},
                {"sections": [plan["section_contract"]]},
                session_id="generator-session-case",
            )

        self.assertEqual(blocked.exception.code, "SYNTHESIS_NOT_APPROVED")

    def test_multi_study_workspace_builds_a_source_bound_v1_request(self) -> None:
        evidence_digest = "1" * 64
        evidence_rows = [
            {
                "evidence_id": f"evidence-multi-{index}",
                "study_id": f"study-multi-{index}",
                "status": "approved",
                "statement": f"Study {index} reports a bounded observation.",
                "field_dependencies": [],
            }
            for index in range(1, 4)
        ]
        evidence = {
            "projection_digest": evidence_digest,
            "workflow_can_continue": True,
            "rows": evidence_rows,
        }
        plan = local_pdf_parse.build_pdf_only_synthesis_plan(evidence)
        request = local_pdf_parse.build_pdf_only_v1_request(
            evidence,
            {
                "workflow_can_continue": True,
                "rows": [{**plan["synthesis_claim"], "status": "approved"}],
            },
            {"sections": [plan["section_contract"]]},
            session_id="generator-session-multi",
        )

        self.assertEqual(request["heading"], "Source-Bound Multi-Study Synthesis")
        self.assertIn("Multi-study source-bound comparison only", request["body"])
        self.assertNotIn("Single-study case report", request["body"])
        for row in evidence_rows:
            self.assertIn(f"[evidence:{row['evidence_id']}]", request["body"])
        self.assertNotIn("molecule", request["body"].lower())


if __name__ == "__main__":
    unittest.main()
