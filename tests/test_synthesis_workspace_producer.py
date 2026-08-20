"""Focused contract checks for the Agent-facing synthesis workspace producer."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from review_writer.agent import local_pdf_parse
from review_writer.agent.generator_runtime import (
    GeneratorRuntimeError,
    GeneratorSession,
)
from review_writer.project.manuscript_v2 import ManuscriptV2Error


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


if __name__ == "__main__":
    unittest.main()
