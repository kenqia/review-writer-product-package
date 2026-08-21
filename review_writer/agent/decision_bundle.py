"""Read-only public Decision Bundle projection.

The bundle is an aggregation seam only.  It reads the canonical
``VersionContext`` and the existing source/evidence/parse/synthesis/figure/
release projections; it does not create a second state store, move current,
or record a human decision.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from review_writer.delivery.project_release import new_route_release_docx_is_current
from review_writer.product_foundation import ProductFoundationError, VersionContext
from review_writer.project.manuscript_v2 import manuscript_state
from review_writer.project.paper_evidence import paper_evidence_state
from review_writer.project.parse_quality import project_parse_quality_state
from review_writer.project.review_figures import (
    load_source_figure_registry,
    synthesis_figure_placeholders,
)
from review_writer.project.section_contract import section_contract_state
from review_writer.project.source_truth import (
    declared_study_ids,
    load_source_truth_bundle,
)
from review_writer.project.synthesis import (
    comparison_protocol_state,
    coverage_map_state,
    synthesis_state,
)
from review_writer.project.workflow_projection import workflow_state


DECISION_BUNDLE_SCHEMA = "decision-bundle.v1"
HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
_MAX_SAFE_TEXT = 20_000
_SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "cookie",
    "password",
    "credential",
    "authorization",
)
_SOURCE_FIELDS = (
    "member_id",
    "name",
    "sha256",
    "pdf_sha256",
    "size_bytes",
    "download_id",
    "source_id",
    "study_id",
    "document_role",
    "source_type",
    "doi",
    "title",
    "bundle_digest",
    "page_count",
)
_EXPECTED_WRITE_SET = (
    "01_evidence/source_truth/*/parse_quality.json",
    "01_evidence/paper_evidence_projection.jsonl",
    "02_synthesis/comparison_protocol.json",
    "02_synthesis/coverage_map.json",
    "02_synthesis/synthesis_claim_projection.jsonl",
    "02_synthesis/section_contracts.jsonl",
    "03_figures/source_figure_registry.json",
    "03_figures/synthesis_figure_placeholders.json",
    "04_manuscript/section_drafts.jsonl",
    "04_manuscript/manuscript.md",
    "04_manuscript/manuscript_lineage.v2.json",
    "05_release/*",
)


def _safe_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.casefold()
    return not any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _safe_value(value: object, *, depth: int = 0) -> object:
    """Copy JSON-like projection data while excluding secret-shaped fields."""

    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_SAFE_TEXT:
            return value[:_MAX_SAFE_TEXT]
        return value
    if isinstance(value, Mapping):
        return {
            key: _safe_value(item, depth=depth + 1)
            for key, item in value.items()
            if _safe_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value]
    return None


def _code(exc: BaseException, fallback: str) -> str:
    value = getattr(exc, "code", None)
    return value if isinstance(value, str) and value.strip() else fallback


def _gap(component: str, code: str, *, detail: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"component": component, "code": code}
    if detail:
        row["detail"] = detail
    return row


def _component_gap(component: str, code: str) -> dict[str, Any]:
    return {
        "status": "needs_review",
        "workflow_can_continue": False,
        "reason_code": code,
        "gaps": [_gap(component, code)],
    }


def _read_component(
    component: str,
    loader: Callable[[Path], object],
    root: Path,
    *,
    fallback: str,
) -> dict[str, Any]:
    try:
        value = loader(root)
    except Exception as exc:  # projection boundary: preserve a stable gap
        code = _code(exc, fallback)
        return _component_gap(component, code)
    if not isinstance(value, Mapping):
        return _component_gap(component, fallback)
    projected = _safe_value(value)
    if not isinstance(projected, dict):
        return _component_gap(component, fallback)
    projected.setdefault("gaps", [])
    if not isinstance(projected["gaps"], list):
        projected["gaps"] = []
    if projected.get("workflow_can_continue") is not True:
        reason = projected.get("reason_code")
        if isinstance(reason, str) and reason:
            projected["gaps"].append(_gap(component, reason))
    return projected


def _current_payload(state: Any, current: Any) -> dict[str, Any]:
    digest = current.snapshot_digest
    return {
        "version_id": current.version_id,
        "revision": state.revision,
        "digest": digest,
        "snapshot_digest": digest,
    }


def _empty_components() -> dict[str, Any]:
    return {
        "source_identities": [],
        "source_identity_projection": {
            "status": "needs_review",
            "workflow_can_continue": False,
            "reason_code": "SOURCE_IDENTITY_MISSING",
            "studies": [],
            "sources": [],
            "gaps": [_gap("source", "SOURCE_IDENTITY_MISSING")],
        },
        "parse_provenance": _component_gap("parse", "PARSE_PROVENANCE_MISSING"),
        "evidence": _component_gap("evidence", "PAPER_EVIDENCE_NOT_AVAILABLE"),
        "synthesis": _component_gap("synthesis", "SYNTHESIS_NOT_AVAILABLE"),
        "figures": _component_gap("figures", "FIGURE_STATE_INVALID"),
        "release_impacts": _component_gap("release", "RELEASE_NOT_READY"),
    }


def _source_projection(root: Path, snapshot: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[object, object, object]] = set()
    for owner in snapshot.values():
        if not isinstance(owner, Mapping):
            continue
        for key in ("authorized_source_set", "source_identities", "sources"):
            candidates = owner.get(key)
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                row = {
                    field: _safe_value(candidate[field])
                    for field in _SOURCE_FIELDS
                    if field in candidate and _safe_key(field)
                }
                if not row:
                    continue
                identity = (
                    row.get("study_id"),
                    row.get("source_id"),
                    row.get("sha256") or row.get("pdf_sha256"),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(row)

    gaps: list[dict[str, Any]] = []
    studies: list[dict[str, Any]] = []
    try:
        study_ids = declared_study_ids(root)
    except Exception as exc:
        gaps.append(_gap("source", _code(exc, "SOURCE_IDENTITY_MISSING")))
        study_ids = []
    for study_id in study_ids:
        try:
            bundle = load_source_truth_bundle(root, study_id)
        except Exception as exc:
            gaps.append(_gap("source", _code(exc, "SOURCE_TRUTH_INVALID")))
            continue
        if not isinstance(bundle, Mapping):
            gaps.append(_gap("source", "SOURCE_TRUTH_INVALID"))
            continue
        bundle_sources: list[dict[str, Any]] = []
        raw_sources = bundle.get("sources", [])
        if isinstance(raw_sources, list):
            for source in raw_sources:
                if not isinstance(source, Mapping):
                    continue
                source_row = {
                    field: _safe_value(source[field])
                    for field in _SOURCE_FIELDS
                    if field in source
                }
                if source_row:
                    bundle_sources.append(source_row)
                    identity = (
                        bundle.get("study_id", study_id),
                        source_row.get("source_id"),
                        source_row.get("pdf_sha256"),
                    )
                    if identity not in seen:
                        seen.add(identity)
                        rows.append(
                            {
                                "study_id": bundle.get("study_id", study_id),
                                **source_row,
                            }
                        )
        studies.append(
            {
                "study_id": bundle.get("study_id", study_id),
                "study_identity": _safe_value(bundle.get("study_identity", {})),
                "sources": bundle_sources,
                "bundle_digest": bundle.get("bundle_digest"),
            }
        )
    if not rows:
        gaps.append(_gap("source", "SOURCE_IDENTITY_MISSING"))
    projection = {
        "status": "approved" if rows and not gaps else "needs_review",
        "workflow_can_continue": bool(rows and not gaps),
        "reason_code": "SOURCE_IDENTITY_READY" if rows and not gaps else gaps[0]["code"],
        "studies": studies,
        "sources": copy.deepcopy(rows),
        "gaps": gaps,
    }
    return rows, projection


def _parse_projection(root: Path, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    projection = _read_component(
        "parse", project_parse_quality_state, root, fallback="PARSE_PROVENANCE_MISSING"
    )
    owner = snapshot.get("agent_parse")
    if isinstance(owner, Mapping):
        projection["agent"] = _safe_value(owner)
    return projection


def _figure_projection(root: Path) -> dict[str, Any]:
    registry = _read_component(
        "figures", load_source_figure_registry, root, fallback="FIGURE_STATE_INVALID"
    )
    if registry.get("reason_code") == "FIGURE_STATE_INVALID" and (root / "03_figures").is_dir():
        # A missing registry is an honest candidate gap, not a corrupt root.
        registry["reason_code"] = "FIGURE_REGISTRY_MISSING"
        registry["gaps"] = [_gap("figures", "FIGURE_REGISTRY_MISSING")]
    try:
        placeholders = synthesis_figure_placeholders(root)
    except Exception as exc:
        placeholders = []
        registry.setdefault("gaps", []).append(
            _gap("figures", _code(exc, "PLACEHOLDER_INVALID"))
        )
    registry["placeholders"] = _safe_value(placeholders)
    return registry


def _release_projection(root: Path) -> dict[str, Any]:
    workflow = _read_component(
        "release", workflow_state, root, fallback="RELEASE_NOT_READY"
    )
    try:
        manuscript = manuscript_state(root)
        manuscript = _safe_value(manuscript)
    except Exception as exc:
        manuscript = _component_gap("release", _code(exc, "MANUSCRIPT_NOT_APPROVED"))
    releases: list[dict[str, Any]] = []
    for filename, level in (
        ("self_reviewed_draft.docx", "SELF_REVIEWED_DRAFT"),
        ("expert_reviewed_release.docx", "EXPERT_REVIEWED_RELEASE"),
    ):
        path = root / "05_release" / filename
        if path.is_symlink() or not path.is_file():
            continue
        try:
            current = bool(new_route_release_docx_is_current(path))
        except Exception:
            current = False
        releases.append(
            {"release_level": level, "path": f"05_release/{filename}", "current": current}
        )
    gaps = workflow.get("gaps", []) if isinstance(workflow.get("gaps"), list) else []
    if workflow.get("workflow_can_continue") is not True:
        reason = workflow.get("reason_code") or workflow.get("blocker") or "RELEASE_NOT_READY"
        gaps.append(_gap("release", str(reason)))
    if not releases:
        gaps.append(_gap("release", "RELEASE_NOT_READY"))
    return {
        "status": "approved" if releases and not gaps else "needs_review",
        "workflow_can_continue": bool(releases and not gaps),
        "reason_code": "RELEASE_READY" if releases and not gaps else gaps[0]["code"],
        "workflow": workflow,
        "manuscript": manuscript,
        "releases": releases,
        "gaps": gaps,
    }


def _decision_options() -> list[dict[str, Any]]:
    return [
        {
            "decision_id": "SOURCE_IDENTITY",
            "label": "确认 MAIN/SI 来源身份",
            "requires_human": True,
            "candidate_only": True,
        },
        {
            "decision_id": "PARSE_QUALITY",
            "label": "确认解析质量与 provenance",
            "requires_human": True,
            "candidate_only": True,
        },
        {
            "decision_id": "EVIDENCE_AND_SYNTHESIS",
            "label": "审核 Evidence 与 synthesis candidate",
            "requires_human": True,
            "candidate_only": True,
        },
        {
            "decision_id": "FIGURES_AND_RELEASE",
            "label": "审核图件权利、正文与 release 影响",
            "requires_human": True,
            "candidate_only": True,
        },
    ]


def _failure(
    *,
    code: str,
    category: str,
    current: dict[str, Any] | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    components = _empty_components()
    error = {"code": code, "category": category}
    return {
        "schema_version": DECISION_BUNDLE_SCHEMA,
        "status": category,
        "reason_code": code,
        "category": category,
        "error": error,
        "next_action": {
            "type": "RETRY_DECISION_BUNDLE" if category == "VERSION_CONFLICT" else "FIX_PRECONDITION",
            "reason_code": code,
        },
        "current": current,
        "revision": revision,
        "write_mode": "zero_write",
        "current_unchanged": True,
        "source_identities": components["source_identities"],
        "parse_provenance": components["parse_provenance"],
        "evidence": components["evidence"],
        "synthesis": components["synthesis"],
        "figures": components["figures"],
        "release_impacts": components["release_impacts"],
        "decision_options": [],
        "expected_write_set": [],
        "conflicts": [_gap("version", code)],
    }


def build_decision_bundle(
    project_root: str | Path,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Return one read-only Decision Bundle for the explicit project root.

    The response is deliberately candidate-only.  A valid bundle remains at
    ``HUMAN_ACTION_REQUIRED`` even if an upstream projection reports approval;
    stale or invalid current state returns a zero-write structured failure.
    """

    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 0
    ):
        return _failure(code="EXPECTED_REVISION_INVALID", category="PRECONDITION_FAILED")
    try:
        context = VersionContext.load(project_root)
        state = context.state()
        current = context.view_version(state.current_version_id)
        if (
            state.current_version_id != state.active_head_id
            or not current.is_current
            or not current.is_active_head
            or current.read_only
            or not current.can_write
        ):
            return _failure(
                code="VERSION_CONTEXT_INVALID",
                category="PRECONDITION_FAILED",
                current=_current_payload(state, current),
                revision=state.revision,
            )
    except (OSError, ProductFoundationError, TypeError, ValueError):
        return _failure(
            code="VERSION_CONTEXT_INVALID", category="PRECONDITION_FAILED"
        )

    current_payload = _current_payload(state, current)
    if expected_revision is not None and expected_revision != state.revision:
        return _failure(
            code="VERSION_CONFLICT",
            category="VERSION_CONFLICT",
            current=current_payload,
            revision=state.revision,
        )

    snapshot = current.snapshot
    sources, source_projection = _source_projection(Path(project_root).resolve(), snapshot)
    parse = _parse_projection(Path(project_root).resolve(), snapshot)
    evidence = _read_component(
        "evidence", paper_evidence_state, Path(project_root).resolve(), fallback="PAPER_EVIDENCE_NOT_AVAILABLE"
    )
    protocol = _read_component(
        "synthesis", comparison_protocol_state, Path(project_root).resolve(), fallback="SYNTHESIS_PROTOCOL_NOT_AVAILABLE"
    )
    coverage = _read_component(
        "synthesis", coverage_map_state, Path(project_root).resolve(), fallback="COVERAGE_MAP_NOT_AVAILABLE"
    )
    claims = _read_component(
        "synthesis", synthesis_state, Path(project_root).resolve(), fallback="SYNTHESIS_NOT_AVAILABLE"
    )
    contracts = _read_component(
        "synthesis", section_contract_state, Path(project_root).resolve(), fallback="SECTION_CONTRACT_NOT_AVAILABLE"
    )
    synthesis = {
        "status": "needs_review",
        "workflow_can_continue": False,
        "reason_code": "SYNTHESIS_NOT_AVAILABLE",
        "protocol": protocol,
        "coverage": coverage,
        "claims": claims,
        "section_contracts": contracts,
        "gaps": [],
    }
    for value in (protocol, coverage, claims, contracts):
        value_gaps = value.get("gaps") if isinstance(value, Mapping) else None
        if isinstance(value_gaps, list):
            synthesis["gaps"].extend(value_gaps)
    if not synthesis["gaps"]:
        synthesis["gaps"].append(_gap("synthesis", "SYNTHESIS_NOT_AVAILABLE"))
    synthesis["status"] = "approved" if all(
        isinstance(value, Mapping) and value.get("workflow_can_continue") is True
        for value in (protocol, coverage, claims, contracts)
    ) else "needs_review"
    synthesis["workflow_can_continue"] = synthesis["status"] == "approved"
    if synthesis["workflow_can_continue"]:
        synthesis["reason_code"] = "SYNTHESIS_READY"
    else:
        first_gap = synthesis["gaps"][0]
        synthesis["reason_code"] = first_gap.get("code", "SYNTHESIS_NOT_AVAILABLE")

    root = Path(project_root).resolve()
    figures = _figure_projection(root)
    release = _release_projection(root)
    all_gaps: list[dict[str, Any]] = []
    for component in (source_projection, parse, evidence, synthesis, figures, release):
        value = component.get("gaps") if isinstance(component, Mapping) else None
        if isinstance(value, list):
            all_gaps.extend(copy.deepcopy(value))
    reason_code = next(
        (
            owner.get("reason_code")
            for owner in snapshot.values()
            if isinstance(owner, Mapping)
            and owner.get("status") == HUMAN_ACTION_REQUIRED
            and isinstance(owner.get("reason_code"), str)
        ),
        None,
    )
    reason_code = reason_code or (all_gaps[0].get("code") if all_gaps else "DECISION_BUNDLE_REVIEW_REQUIRED")
    return {
        "schema_version": DECISION_BUNDLE_SCHEMA,
        "status": HUMAN_ACTION_REQUIRED,
        "reason_code": reason_code,
        "next_action": {
            "type": HUMAN_ACTION_REQUIRED,
            "route": "/review",
            "reason_code": reason_code,
        },
        "current": current_payload,
        "revision": state.revision,
        "write_mode": "NONE",
        "current_unchanged": True,
        "source_identities": sources,
        "source_identity_projection": source_projection,
        "parse_provenance": parse,
        "evidence": evidence,
        "synthesis": synthesis,
        "figures": figures,
        "release_impacts": release,
        "decision_options": _decision_options(),
        "expected_write_set": list(_EXPECTED_WRITE_SET),
        "write_set_policy": {
            "mode": "human_decision_only",
            "current_pointer": "unchanged_until_human_decision",
            "bundle_writes": [],
        },
        "conflicts": all_gaps,
    }


__all__ = ["DECISION_BUNDLE_SCHEMA", "build_decision_bundle"]
