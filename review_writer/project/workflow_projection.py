"""Authoritative workflow projection for legacy and evidence-to-release projects."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from review_writer.project.paper_evidence import PaperEvidenceError, paper_evidence_state
from review_writer.project.synthesis import SynthesisError, synthesis_state
from review_writer.project.section_contract import SectionContractError, section_contract_state
from review_writer.project.parse_quality import project_parse_quality_state
from review_writer.project.source_truth import (
    SOURCE_TRUTH_ROOT,
    SourceTruthError,
    canonical_digest,
    declared_study_ids,
    study_source_tier,
)
from review_writer.project.dual_source import project_dual_source_state
from review_writer.project.chemical_completion import project_chemical_completion_state
from review_writer.project.parse_reconciliation import project_reconciliation_state


NEW_ROUTE = "evidence-to-release.v1"

# The new route has one stage presentation authority.  Dashboard projections
# may add safe, route-specific facts, but must not invent a second stage list.
STAGE_PRESENTATION: dict[str, dict[str, str]] = {
    "sources": {"label": "整理文献来源"},
    "parsing": {"label": "解析全文与补充信息"},
    "chemical_import": {"label": "导入并绑定 Chemical Paper"},
    "chemical_completion": {"label": "补全 Chemical Completion"},
    "reconciliation": {"label": "核对双层解析差异"},
    "evidence": {"label": "提取并核对逐研究证据"},
    "synthesis": {"label": "完成比较协议与综合判断"},
    "drafting": {"label": "撰写证据约束正文"},
    "final": {"label": "完成终稿与 DOCX"},
}


def _regular_file(project: Path, relative: str) -> bool:
    path = project / relative
    return path.is_file() and not path.is_symlink()


def _chemical_preflight_ready(project: Path) -> bool:
    """Return whether a safe, unexpired Chemical ZIP awaits confirmation."""

    stage = project / ".dual-parse-staging/chemical-paper"
    if stage.is_symlink() or not stage.is_dir():
        return False
    try:
        manifests = sorted(stage.glob("*.json"))
    except OSError:
        return False
    for manifest_path in manifests:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        key = manifest_path.stem
        archive = stage / f"{key}.zip"
        if archive.is_symlink() or not archive.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != "chemical-paper-preflight-stage.v1"
            or manifest.get("status") != "ready"
            or manifest.get("token_sha256") != key
            or not isinstance(manifest.get("study_id"), str)
            or not isinstance(manifest.get("expires_at_epoch"), (int, float))
            or isinstance(manifest.get("expires_at_epoch"), bool)
            or time.time() > float(manifest["expires_at_epoch"])
        ):
            continue
        return True
    return False


def _read_jsonl(project: Path, relative: str) -> list[dict[str, Any]] | None:
    if not _regular_file(project, relative):
        return None
    try:
        lines = (project / relative).read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not rows or not all(isinstance(row, dict) for row in rows):
        return None
    return rows


def _non_exact_evidence_route_allowed(value: dict[str, Any]) -> bool:
    """Allow low-coverage projects to review only explicitly non-exact Evidence."""

    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        return False
    return all(
        isinstance(row, dict)
        and not set(row.get("field_dependencies", []))
        for row in rows
    )


def _non_exact_candidate_only(project: Path, declared: list[str]) -> bool:
    """Return whether every registered candidate is explicitly locator-only.

    Core/background sources normally enter the chemical dual route.  A native
    Agent may, however, register a source-bound Evidence candidate that does
    not request molecule/SMILES/molblock fields.  In that case the candidate
    is intentionally limited to original-PDF locators and must be allowed to
    reach the ordinary Evidence review gate without fabricating a Chemical
    Paper import.  Missing candidates remain blocked so the existing
    chemical-import preflight is still the first action for exact work.
    """
    if not declared:
        return False
    seen = False
    for study_id in declared:
        path = project / "01_evidence" / study_id / "paper_evidence_candidates.json"
        if not path.is_file() or path.is_symlink():
            return False
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        rows = value.get("candidates") if isinstance(value, dict) else value
        if not isinstance(rows, list) or not rows:
            return False
        seen = True
        for row in rows:
            if not isinstance(row, dict):
                return False
            dependencies = row.get("field_dependencies", [])
            if not isinstance(dependencies, list) or dependencies:
                return False
    return seen


def _finalize(state: dict[str, Any]) -> dict[str, Any]:
    state["workflow_digest"] = canonical_digest(state)
    return state


def _legacy_state(project: Path) -> dict[str, Any]:
    evidence_ready = bool(_read_jsonl(project, "01_evidence/evidence_cards.jsonl"))
    manuscript_ready = _regular_file(project, "04_first_draft/first_draft.md")
    verified_release_ready = _regular_file(project, "05_final_audit/final_draft.docx")
    if manuscript_ready:
        active_stage = "final"
    elif evidence_ready:
        active_stage = "drafting"
    elif (project / "00_sources").is_dir():
        active_stage = "evidence"
    else:
        active_stage = "sources"
    return _finalize(
        {
            "schema_version": "evidence-to-release-workflow-state.v1",
            "route": "legacy",
            "active_stage": active_stage,
            "parse_ready": bool((project / "01_evidence/mineru").is_dir()),
            "paper_evidence_ready": evidence_ready,
            "synthesis_ready": evidence_ready,
            "section_contracts_ready": manuscript_ready,
            "manuscript_ready": manuscript_ready,
            "internal_draft_export_ready": manuscript_ready,
            "verified_release_ready": verified_release_ready,
            "blockers": [],
            "blocker": None,
        }
    )


def _validated_precomputed_dual_state(
    value: object, declared: list[str]
) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version",
            "studies",
            "main_source_available_count",
            "generic_source_available_count",
            "workflow_can_continue",
        }
        or value.get("schema_version") != "dual-source-project-state.v1"
        or not isinstance(value.get("studies"), list)
        or not isinstance(value.get("workflow_can_continue"), bool)
    ):
        return None
    studies = value["studies"]
    counts = (
        value.get("main_source_available_count"),
        value.get("generic_source_available_count"),
    )
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in counts
    ):
        return None
    allowed_pdf = {"verified", "stale", "unknown"}
    allowed_generic = {"current", "stale", "unknown"}
    allowed_status = {"blocked", "current", "current_generic_only"}
    ids: list[str] = []
    for row in studies:
        if not isinstance(row, dict):
            return None
        study_id = row.get("study_id")
        tier = row.get("source_tier")
        requires_chemical = row.get("requires_chemical")
        pdf_status = row.get("pdf_status")
        generic_status = row.get("generic_parse_status")
        status = row.get("status")
        generic = row.get("generic")
        if (
            not isinstance(study_id, str)
            or tier not in {"core", "background"}
            or not isinstance(requires_chemical, bool)
            or requires_chemical is not (tier == "core")
            or pdf_status not in allowed_pdf
            or generic_status not in allowed_generic
            or status not in allowed_status
            or not isinstance(generic, dict)
            or generic.get("status") != generic_status
        ):
            return None
        ids.append(study_id)
        if status == "blocked":
            if not isinstance(row.get("reason_code"), str) or not row["reason_code"]:
                return None
            continue
        if (
            pdf_status != "verified"
            or generic_status != "current"
            or not isinstance(row.get("binding_digest"), str)
            or not isinstance(generic.get("binding_digest"), str)
            or (status == "current_generic_only" and tier != "background")
        ):
            return None
        chemical = row.get("chemical")
        if status == "current_generic_only":
            if chemical is not None:
                return None
        elif (
            not isinstance(chemical, dict)
            or chemical.get("status") != "current"
            or not isinstance(chemical.get("state_digest"), str)
            or row.get("reaction_data_status")
            not in {"available", "unavailable_not_provided"}
        ):
            return None
    if sorted(ids) != declared or len(ids) != len(set(ids)):
        return None
    main_count = sum(row["pdf_status"] == "verified" for row in studies)
    generic_count = sum(
        row["generic_parse_status"] == "current" for row in studies
    )
    workflow = bool(studies) and all(
        row["status"] in {"current", "current_generic_only"} for row in studies
    )
    if (
        counts != (main_count, generic_count)
        or value["workflow_can_continue"] is not workflow
    ):
        return None
    return value


def _new_route_state(
    project: Path,
    *,
    _precomputed_dual_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_root = project / SOURCE_TRUTH_ROOT
    bundle_paths = (
        sorted(source_root.glob("*/bundle.json"))
        if source_root.is_dir() and not source_root.is_symlink()
        else []
    )
    try:
        declared = declared_study_ids(project)
    except SourceTruthError:
        declared = []
    actual = [path.parent.name for path in bundle_paths]
    source_ready = bool(declared) and actual == declared and all(
        path.is_file() and not path.is_symlink() for path in bundle_paths
    )
    parse_ready = False
    parse_error = False
    if source_ready:
        try:
            parse_state = project_parse_quality_state(project)
            parse_ready = bool(parse_state.get("workflow_can_continue"))
            parse_error = parse_state.get("status") == "needs_attention"
        except (OSError, ValueError, KeyError, TypeError):
            parse_error = True

    paper_evidence_ready = False
    paper_evidence_error = False
    non_exact_evidence_allowed = False
    tiered_dual_route = False
    if source_ready and declared:
        try:
            tiered_dual_route = all(
                study_source_tier(project, study_id) in {"core", "background"}
                for study_id in declared
            )
            if tiered_dual_route and _non_exact_candidate_only(project, declared):
                tiered_dual_route = False
        except SourceTruthError:
            tiered_dual_route = False
    dual_route = tiered_dual_route or (
        (project / "01_evidence/dual_source").is_dir()
        or (project / "01_evidence/chemical_paper").is_dir()
    )
    dual_source_ready = not dual_route
    chemical_completion_ready = not dual_route
    reconciliation_ready = not dual_route
    dual_blocker: str | None = None
    main_source_available_count = 0
    generic_source_available_count = 0
    dual_state: dict[str, Any] | None = None
    if source_ready and dual_route:
        dual_state = _validated_precomputed_dual_state(
            _precomputed_dual_state, declared
        )
        if dual_state is None:
            dual_state = project_dual_source_state(project)
        main_source_available_count = int(
            dual_state.get("main_source_available_count", 0)
        )
        generic_source_available_count = int(
            dual_state.get("generic_source_available_count", 0)
        )
    if parse_ready and dual_state is not None:
        dual_source_ready = bool(dual_state.get("workflow_can_continue"))
        if not dual_source_ready:
            blocked = next((row for row in dual_state["studies"] if row["status"] == "blocked"), {})
            dual_blocker = str(blocked.get("reason_code") or "DUAL_SOURCE_NOT_READY")
        if dual_source_ready:
            completion_state = project_chemical_completion_state(project)
            chemical_completion_ready = bool(completion_state.get("workflow_can_continue"))
            if not chemical_completion_ready:
                blocked = next((row for row in completion_state["studies"] if not row.get("workflow_can_continue")), {})
                dual_blocker = str(blocked.get("reason_code") or "CHEMICAL_COMPLETION_INCOMPLETE")
        if dual_source_ready:
            reconciliation_state = project_reconciliation_state(project)
            reconciliation_ready = bool(reconciliation_state.get("workflow_can_continue"))
            # Chemical completion remains the first actionable blocker until
            # that gate is closed.  Reconciliation is still evaluated here so
            # an explicitly non-exact Evidence route can use an already
            # current reconciliation state, but it must not overwrite the
            # earlier Chemical Completion reason when both are incomplete.
            if not reconciliation_ready and chemical_completion_ready:
                blocked = next((row for row in reconciliation_state["studies"] if row["status"] == "blocked"), {})
                dual_blocker = str(blocked.get("reason_code") or "PARSE_RECONCILIATION_UNRESOLVED")

    if parse_ready and dual_source_ready and reconciliation_ready:
        try:
            evidence_state = paper_evidence_state(project)
            non_exact_evidence_allowed = (
                not chemical_completion_ready
                and _non_exact_evidence_route_allowed(evidence_state)
            )
            paper_evidence_ready = bool(evidence_state.get("workflow_can_continue"))
        except (PaperEvidenceError, OSError, ValueError, KeyError, TypeError):
            paper_evidence_error = True

    synthesis_ready = False
    section_contracts_ready = False
    synthesis_error = False
    section_contract_error = False
    if paper_evidence_ready:
        try:
            synthesis_ready = bool(synthesis_state(project).get("workflow_can_continue"))
        except (SynthesisError, OSError, ValueError, KeyError, TypeError):
            synthesis_error = True
        if synthesis_ready:
            try:
                section_contracts_ready = bool(section_contract_state(project).get("workflow_can_continue"))
            except (SectionContractError, SynthesisError, OSError, ValueError, KeyError, TypeError):
                section_contract_error = True
    manuscript_ready = False
    internal_draft_export_ready = False
    verified_release_ready = False
    if section_contracts_ready:
        try:
            from review_writer.project.manuscript_v2 import manuscript_state

            manuscript_ready = bool(
                manuscript_state(project).get("workflow_can_continue")
            )
            internal_draft_export_ready = manuscript_ready
        except (OSError, ValueError, KeyError, TypeError):
            manuscript_ready = False
            internal_draft_export_ready = False

    blockers: list[str] = []
    if not source_ready:
        active_stage = "sources"
        blockers.append("SOURCE_TRUTH_MISSING_OR_INVALID")
    elif not parse_ready:
        active_stage = "parsing"
        blockers.append(
            "PARSE_QUALITY_INVALID" if parse_error else "PARSE_QUALITY_REVIEW_REQUIRED"
        )
    elif dual_route and not dual_source_ready:
        active_stage = "chemical_import"
        blockers.append(dual_blocker or "DUAL_SOURCE_NOT_READY")
    elif (
        dual_route
        and not chemical_completion_ready
        and not non_exact_evidence_allowed
    ):
        active_stage = "chemical_completion"
        blockers.append(dual_blocker or "CHEMICAL_COMPLETION_INCOMPLETE")
    elif dual_route and not reconciliation_ready:
        active_stage = "reconciliation"
        blockers.append(dual_blocker or "PARSE_RECONCILIATION_UNRESOLVED")
    elif not paper_evidence_ready:
        active_stage = "evidence"
        blockers.append(
            "PAPER_EVIDENCE_INVALID"
            if paper_evidence_error
            else "PAPER_EVIDENCE_NOT_APPROVED"
        )
    elif not synthesis_ready or not section_contracts_ready:
        active_stage = "synthesis"
        blockers.append("SYNTHESIS_INVALID" if synthesis_error else ("SECTION_CONTRACT_INVALID" if section_contract_error else "SYNTHESIS_NOT_APPROVED"))
    elif not manuscript_ready:
        active_stage = "drafting"
        blockers.append("MANUSCRIPT_NOT_APPROVED")
    else:
        active_stage = "final"
        if not internal_draft_export_ready:
            blockers.append("INTERNAL_DRAFT_EXPORT_NOT_READY")

    next_actions = {
        "sources": "Verify the next source PDF.",
        "parsing": "Review the next Generic parse quality item.",
        "chemical_import": (
            "确认第一份 Chemical Paper 导入"
            if _chemical_preflight_ready(project)
            else "待 Chemical Paper 导入"
        ),
        "chemical_completion": "补全下一项化学字段",
        "reconciliation": "依据 PDF 仲裁下一项双层解析差异",
        "evidence": "Review the next Paper Evidence candidate.",
        "synthesis": "Review the next synthesis object.",
        "drafting": "Review the next manuscript section.",
        "final": "Review internal release readiness.",
    }
    return _finalize(
        {
            "schema_version": "evidence-to-release-workflow-state.v1",
            "route": NEW_ROUTE,
            "dual_route": dual_route,
            "active_stage": active_stage,
            "main_source_available_count": main_source_available_count,
            "generic_source_available_count": generic_source_available_count,
            "parse_ready": parse_ready,
            "dual_source_ready": dual_source_ready,
            "chemical_completion_ready": chemical_completion_ready,
            "non_exact_evidence_allowed": non_exact_evidence_allowed,
            "reconciliation_ready": reconciliation_ready,
            "paper_evidence_ready": paper_evidence_ready,
            "synthesis_ready": synthesis_ready,
            "section_contracts_ready": section_contracts_ready,
            "manuscript_ready": manuscript_ready,
            "internal_draft_export_ready": internal_draft_export_ready,
            "verified_release_ready": verified_release_ready,
            "blockers": blockers,
            "blocker": blockers[0] if blockers else None,
            "unique_next_action": next_actions[active_stage],
        }
    )


def workflow_state(project: Path) -> dict[str, Any]:
    """Project the only workflow state allowed to authorize downstream work."""

    project = project.resolve(strict=True)
    source_root = project / SOURCE_TRUTH_ROOT
    if os.path.lexists(source_root):
        return _new_route_state(project)
    return _legacy_state(project)


def _workflow_and_dual_source_state(
    project: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute and reuse dual authority only within this synchronous request."""

    root = project.resolve(strict=True)
    dual = project_dual_source_state(root)
    source_root = root / SOURCE_TRUTH_ROOT
    workflow = (
        _new_route_state(root, _precomputed_dual_state=dual)
        if os.path.lexists(source_root)
        else _legacy_state(root)
    )
    return workflow, dual
