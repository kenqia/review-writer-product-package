"""Typed, hash-bound single-paper evidence registration and review."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from review_writer.project.parse_quality import (
    ParseQualityError,
    build_parse_quality_gate,
    parse_quality_state,
    project_parse_quality_state,
)
from review_writer.project.source_truth import (
    REPO_ROOT,
    SourceTruthError,
    canonical_digest,
    declared_study_ids,
    load_source_truth_bundle,
    source_truth_asset,
)
from review_writer.project.dual_source import DualSourceError, require_dual_source_ready
from review_writer.project.chemical_completion import (
    ChemicalCompletionError,
    require_honest_progressive_projection,
)
from review_writer.project.parse_reconciliation import (
    ParseReconciliationError,
    require_reconciliation_ready,
)
from review_writer.project.source_truth import study_source_tier
from review_writer.project.verification_decision import (
    VerificationDecisionError,
    verification_decision,
)
from review_writer.project.paper_evidence_store import (
    PaperEvidenceStoreError,
    project_write_lock,
)


PAPER_EVIDENCE_SCHEMA = REPO_ROOT / "schemas/evidence/paper_evidence.v1.schema.json"
EVIDENCE_DECISION_SCHEMA = REPO_ROOT / "schemas/evidence/evidence_decision.v1.schema.json"
DECISIONS_PATH = Path("01_evidence/paper_evidence_decisions.jsonl")
PROJECTION_PATH = Path("01_evidence/paper_evidence_projection.jsonl")
EPISTEMIC_TYPES = frozenset(
    {"experimental_observation", "author_interpretation", "proposed_mechanism"}
)
DECISION_ACTIONS = frozenset({"approve", "revise_and_approve", "reject"})
ACTOR_TYPES = frozenset({"human_researcher", "simulated_researcher_agent"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HONEST_PROGRESSIVE_ROUTE = "honest_progressive"
HONEST_PROGRESSIVE_STATUSES = frozenset(
    {"CONFIRMED", "AI_PROVISIONAL", "BLOCKED"}
)
HONEST_PROGRESSIVE_COVERAGE_THRESHOLD = 0.8
HONEST_PROGRESSIVE_PROJECT_CORE_MOLECULE_COUNT = 309
EXACT_CHEMICAL_FIELD_DEPENDENCIES = frozenset({"molecule", "smiles", "molblock"})

_SENSITIVE_TEXT_MARKERS = (
    "path",
    "sha",
    "hash",
    "digest",
    "token",
    "session",
    "cookie",
    "auth",
    "private",
    "http://",
    "https://",
    "url",
    "uri",
    "json",
    "molblock",
    "begin ct",
    "m  end",
)
_SAFE_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "provider",
        "evidence_id",
        "claim_id",
        "source_id",
        "document_role",
        "actor_type",
        "actor_label",
        "reason_code",
        "status",
        "page",
        "section_or_item",
        "figure_or_table",
    }
)
_SAFE_LOCATOR_KEYS = frozenset(
    {"source_mode", "page", "section_or_item", "figure_or_table"}
)
_SAFE_ACTOR_RESIDUAL_KEYS = frozenset(
    {
        "actor_type",
        "actor_label",
        "expected_actor_type",
        "observed_actor_type",
        "reason_code",
        "status",
        "occurred_at",
    }
)


class PaperEvidenceError(ValueError):
    """A stable, researcher-safe paper evidence failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_text(value: object, *, fallback: str | None = None) -> str | None:
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text or len(text) > 2000:
        return fallback
    lowered = text.casefold()
    if any(marker in lowered for marker in _SENSITIVE_TEXT_MARKERS):
        return fallback
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return fallback
    if SHA256_RE.fullmatch(text):
        return fallback
    if text.startswith(("{", "[")) and text.endswith(("}", "]")):
        return fallback
    return text


def _safe_provenance(value: object) -> dict[str, Any] | list[dict[str, Any]]:
    source = value if isinstance(value, list) else [value]
    safe_rows: list[dict[str, Any]] = []
    for raw in source:
        if not isinstance(raw, dict):
            continue
        safe: dict[str, Any] = {}
        for key, item in raw.items():
            if key not in _SAFE_PROVENANCE_KEYS:
                continue
            if key == "page":
                if isinstance(item, int) and not isinstance(item, bool) and item >= 1:
                    safe[key] = item
                continue
            text = _safe_text(item)
            if text is not None:
                safe[key] = text
        if safe:
            safe_rows.append(safe)
    if isinstance(value, list):
        return safe_rows
    return safe_rows[0] if safe_rows else {}


def _safe_locator(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _SAFE_LOCATOR_KEYS:
            continue
        if key == "page":
            if isinstance(item, int) and not isinstance(item, bool) and item >= 1:
                safe[key] = item
            continue
        text = _safe_text(item)
        if text is not None:
            safe[key] = text
    return safe


def _safe_value(value: object) -> object | None:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _safe_text(value)
    return None


def _safe_actor_provenance_residual(value: object) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) else [value]
    safe_rows: list[dict[str, Any]] = []
    for raw in source:
        if not isinstance(raw, dict):
            continue
        safe: dict[str, Any] = {}
        for key, item in raw.items():
            if key not in _SAFE_ACTOR_RESIDUAL_KEYS:
                continue
            if key == "occurred_at":
                text = _safe_text(item)
                if text is not None:
                    safe[key] = text
            else:
                text = _safe_text(item)
                if text is not None:
                    safe[key] = text
        if safe:
            safe_rows.append(safe)
    return safe_rows


def _safe_gap_registry(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe_rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        reason = _safe_text(raw.get("reason"), fallback="Gap details omitted.")
        item: dict[str, Any] = {
            "status": "BLOCKED",
            "reason": reason or "Gap details omitted.",
        }
        study_id = _safe_text(raw.get("study_id"))
        molecule_id = _safe_text(raw.get("molecule_id"))
        source_id = _safe_text(raw.get("source_id"))
        locator = _safe_locator(raw.get("pdf_locator"))
        if study_id is not None:
            item["study_id"] = study_id
        if molecule_id is not None:
            item["molecule_id"] = molecule_id
        if source_id is not None:
            item["source_id"] = source_id
        if locator:
            item["pdf_locator"] = locator
        if isinstance(raw.get("gap_index"), int) and raw["gap_index"] >= 1:
            item["gap_index"] = raw["gap_index"]
        safe_rows.append(item)
    return safe_rows


def _safe_paper_coverage(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe_rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        study_id = _safe_text(raw.get("study_id"))
        counts = {
            key: raw.get(key)
            for key in (
                "core_molecule_count",
                "confirmed_count",
                "ai_provisional_count",
                "blocked_count",
            )
        }
        denominator = raw.get("coverage_denominator", counts["core_molecule_count"])
        if study_id is None or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in counts.values()
        ) or (
            isinstance(denominator, bool)
            or not isinstance(denominator, int)
            or denominator < 0
        ):
            continue
        ratio = raw.get("coverage_ratio")
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not 0 <= ratio <= 1
        ):
            continue
        safe_rows.append(
            {
                "study_id": study_id,
                **counts,
                "coverage_denominator": denominator,
                "coverage_ratio": float(ratio),
            }
        )
    return safe_rows


def _safe_traceability(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe_rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("study_id", "molecule_id", "status", "source_id"):
            text = _safe_text(raw.get(key))
            if text is not None:
                item[key] = text
        locator = _safe_locator(raw.get("pdf_locator"))
        provenance = _safe_provenance(raw.get("provenance"))
        if locator:
            item["pdf_locator"] = locator
        if provenance:
            item["provenance"] = provenance
        confidence = raw.get("confidence")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 1
        ):
            item["confidence"] = confidence
        if item:
            safe_rows.append(item)
    return safe_rows


def _honest_progressive_status(row: dict[str, Any]) -> tuple[str, bool]:
    """Normalize current and legacy evidence statuses without upgrading AI output."""

    value = row.get("status", row.get("molecule_status", row.get("state")))
    if isinstance(value, str):
        status = value.strip().upper()
        if status in HONEST_PROGRESSIVE_STATUSES:
            return status, False
        if status in {"APPROVED", "HUMAN_APPROVED", "VERIFIED"}:
            return "CONFIRMED", True
        if status in {
            "REJECTED",
            "STALE",
            "NEEDS_REVIEW",
            "MISSING",
            "UNRESOLVED",
        }:
            return "BLOCKED", True
    decision = row.get("decision")
    if isinstance(decision, dict):
        action = decision.get("action")
        if action in {"approve", "revise_and_approve", "verify"}:
            return "CONFIRMED", True
        if action in {"reject", "block"}:
            return "BLOCKED", True
    if row.get("human_verified") is True:
        return "CONFIRMED", True
    return "BLOCKED", True


def _honest_progressive_rows(rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise PaperEvidenceError("HONEST_PROGRESSIVE_ROWS_INVALID")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise PaperEvidenceError("HONEST_PROGRESSIVE_ROW_INVALID")
        status, legacy = _honest_progressive_status(raw)
        study_id = raw.get("study_id", raw.get("paper_id", "unknown-study"))
        molecule_id = raw.get(
            "molecule_id", raw.get("mol_id", raw.get("id", f"molecule-{index + 1}"))
        )
        if not isinstance(study_id, str) or not study_id.strip():
            raise PaperEvidenceError("HONEST_PROGRESSIVE_STUDY_ID_INVALID")
        if not isinstance(molecule_id, str) or not molecule_id.strip():
            raise PaperEvidenceError("HONEST_PROGRESSIVE_MOLECULE_ID_INVALID")
        safe_study_id = _safe_text(study_id)
        safe_molecule_id = _safe_text(molecule_id)
        if safe_study_id is None:
            raise PaperEvidenceError("HONEST_PROGRESSIVE_STUDY_ID_UNSAFE")
        if safe_molecule_id is None:
            raise PaperEvidenceError("HONEST_PROGRESSIVE_MOLECULE_ID_UNSAFE")
        study_id = safe_study_id
        molecule_id = safe_molecule_id
        identity = (study_id, molecule_id)
        if identity in seen:
            raise PaperEvidenceError("HONEST_PROGRESSIVE_MOLECULE_DUPLICATE")
        seen.add(identity)
        locator = raw.get("pdf_locator", raw.get("locator"))
        provenance = raw.get("provenance")
        if provenance is None and isinstance(raw.get("source"), str):
            provenance = {"source": raw["source"]}
        safe_locator = _safe_locator(locator)
        safe_provenance = _safe_provenance(provenance)
        raw_source_id = raw.get("source_id")
        if raw_source_id is not None and (
            not isinstance(raw_source_id, str) or not raw_source_id.strip()
        ):
            raise PaperEvidenceError("HONEST_PROGRESSIVE_SOURCE_ID_INVALID")
        source_id = _safe_text(raw_source_id)
        actual_value = raw.get("value", raw.get("statement"))
        confidence = raw.get("confidence")
        if status == "AI_PROVISIONAL":
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
                or not safe_locator
                or not safe_provenance
            ):
                raise PaperEvidenceError(
                    "HONEST_PROGRESSIVE_PROVISIONAL_PROVENANCE_REQUIRED"
                )
        if status == "CONFIRMED" and not legacy and actual_value is None:
            raise PaperEvidenceError("HONEST_PROGRESSIVE_CONFIRMED_VALUE_REQUIRED")
        safe_actual_value = _safe_value(actual_value)
        if status == "CONFIRMED" and not legacy and safe_actual_value is None:
            raise PaperEvidenceError("HONEST_PROGRESSIVE_VALUE_UNSAFE")
        gap_reason = _safe_text(raw.get("gap_reason"))
        if status == "BLOCKED":
            if actual_value is not None:
                raise PaperEvidenceError("HONEST_PROGRESSIVE_BLOCKED_VALUE_FORBIDDEN")
            if not isinstance(gap_reason, str) or not gap_reason.strip():
                gap_reason = (
                    "Legacy evidence is not currently eligible for an exact conclusion."
                    if legacy
                    else None
                )
            if not isinstance(gap_reason, str) or not gap_reason.strip():
                raise PaperEvidenceError("HONEST_PROGRESSIVE_GAP_REASON_REQUIRED")
            actual_value = None
        traceability_ready = bool(
            isinstance(source_id, str)
            and bool(safe_locator)
            and bool(safe_provenance)
        )
        item: dict[str, Any] = {
            "study_id": study_id,
            "molecule_id": molecule_id,
            "status": status,
            "value": safe_actual_value,
            "source_id": source_id,
            "pdf_locator": safe_locator,
            "provenance": safe_provenance,
            "traceability_ready": traceability_ready,
        }
        if status == "AI_PROVISIONAL":
            item["confidence"] = confidence
            item["provisional"] = True
        elif status == "CONFIRMED":
            item["provisional"] = False
        else:
            item["gap_reason"] = gap_reason
        residual = _safe_actor_provenance_residual(
            raw.get("actor_provenance_residual")
        )
        if residual:
            item["_actor_provenance_residual"] = residual
        normalized.append(item)
    return normalized


def build_honest_progressive_summary(
    rows: object,
    core_molecule_count: int | None = None,
    *,
    uncertainty_statement: str | None = None,
    gap_registry: object | None = None,
    paper_core_molecule_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build the shared tri-state, coverage-bound downstream projection.

    This is deliberately a downstream projection: it never turns an AI value
    into a confirmed value and never exposes a blocked value as scientific data.
    """

    source_rows = rows
    inferred_core: object = None
    if isinstance(rows, dict):
        inferred_core = rows.get("core_molecule_count")
        source_rows = rows.get("molecules", rows.get("rows", rows.get("evidence", [])))
        if gap_registry is None:
            gap_registry = rows.get("gap_registry")
        if uncertainty_statement is None:
            candidate_uncertainty = rows.get("uncertainty_statement")
            if isinstance(candidate_uncertainty, str):
                uncertainty_statement = candidate_uncertainty
        if paper_core_molecule_counts is None and isinstance(
            rows.get("paper_core_molecule_counts"), dict
        ):
            paper_core_molecule_counts = rows["paper_core_molecule_counts"]
    normalized = _honest_progressive_rows(source_rows)
    if core_molecule_count is None:
        core_molecule_count = inferred_core if inferred_core is not None else len(normalized)
    if (
        isinstance(core_molecule_count, bool)
        or not isinstance(core_molecule_count, int)
        or core_molecule_count < 0
        or core_molecule_count < len(normalized)
    ):
        raise PaperEvidenceError("HONEST_PROGRESSIVE_CORE_MOLECULE_COUNT_INVALID")
    confirmed_count = sum(row["status"] == "CONFIRMED" for row in normalized)
    ai_provisional_count = sum(row["status"] == "AI_PROVISIONAL" for row in normalized)
    if confirmed_count + ai_provisional_count > core_molecule_count:
        raise PaperEvidenceError("HONEST_PROGRESSIVE_CORE_MOLECULE_COUNT_INVALID")
    blocked_count = core_molecule_count - confirmed_count - ai_provisional_count
    coverage_ratio = (
        1.0
        if core_molecule_count == 0
        else (confirmed_count + ai_provisional_count) / core_molecule_count
    )
    study_ids = sorted({row["study_id"] for row in normalized})
    if paper_core_molecule_counts is not None:
        safe_paper_core_molecule_counts: dict[str, int] = {}
        for key, value in paper_core_molecule_counts.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise PaperEvidenceError("HONEST_PROGRESSIVE_PAPER_COUNT_INVALID")
            safe_key = _safe_text(key)
            if safe_key is not None:
                safe_paper_core_molecule_counts[safe_key] = value
        paper_core_molecule_counts = safe_paper_core_molecule_counts
        study_ids = sorted(set(study_ids) | set(paper_core_molecule_counts))
    if len(study_ids) == 1 and paper_core_molecule_counts is None:
        paper_counts = {study_ids[0]: core_molecule_count}
    else:
        paper_counts = {
            study_id: (
                paper_core_molecule_counts.get(study_id, 0)
                if paper_core_molecule_counts is not None
                else sum(row["study_id"] == study_id for row in normalized)
            )
            for study_id in study_ids
        }
        assigned = sum(paper_counts.values())
        if assigned < core_molecule_count:
            paper_counts["__unattributed__"] = core_molecule_count - assigned
            study_ids = sorted(set(study_ids) | {"__unattributed__"})
    actor_provenance_residual: list[dict[str, Any]] = []
    for row in normalized:
        actor_provenance_residual.extend(
            copy.deepcopy(row.get("_actor_provenance_residual", []))
        )
    if isinstance(rows, dict):
        actor_provenance_residual.extend(
            _safe_actor_provenance_residual(rows.get("actor_provenance_residual"))
        )
    gaps = [
        {
            "study_id": row["study_id"],
            "molecule_id": row["molecule_id"],
            "status": "BLOCKED",
            "reason": row["gap_reason"],
            **(
                {"source_id": row["source_id"]}
                if row.get("source_id") is not None
                else {}
            ),
            **(
                {"pdf_locator": copy.deepcopy(row["pdf_locator"])}
                if row.get("pdf_locator") is not None
                else {}
            ),
        }
        for row in normalized
        if row["status"] == "BLOCKED"
    ]
    missing_count = blocked_count - len(gaps)
    for index in range(max(0, missing_count)):
        gaps.append(
            {
                "study_id": (
                    study_ids[0]
                    if len(study_ids) == 1
                    else "__unattributed__"
                ),
                "molecule_id": None,
                "status": "BLOCKED",
                "reason": "No current molecule record was provided.",
                "gap_index": index + 1,
            }
        )
    if gap_registry is not None:
        if not isinstance(gap_registry, list) or not all(
            isinstance(item, dict) for item in gap_registry
        ):
            raise PaperEvidenceError("HONEST_PROGRESSIVE_GAP_REGISTRY_INVALID")
        gaps.extend(_safe_gap_registry(gap_registry))
    paper_coverage: list[dict[str, Any]] = []
    for study_id in study_ids:
        study_rows = [row for row in normalized if row["study_id"] == study_id]
        study_confirmed = sum(row["status"] == "CONFIRMED" for row in study_rows)
        study_provisional = sum(row["status"] == "AI_PROVISIONAL" for row in study_rows)
        study_core = paper_counts.get(study_id, len(study_rows))
        study_blocked = max(0, study_core - study_confirmed - study_provisional)
        paper_coverage.append(
            {
                "study_id": study_id,
                "core_molecule_count": study_core,
                "coverage_denominator": study_core,
                "confirmed_count": study_confirmed,
                "ai_provisional_count": study_provisional,
                "blocked_count": study_blocked,
                "coverage_ratio": (
                    1.0
                    if study_core == 0
                    else (study_confirmed + study_provisional) / study_core
                ),
            }
        )
    uncertainty_statement = _safe_text(uncertainty_statement)
    if uncertainty_statement is None:
        uncertainty_statement = (
            f"{confirmed_count + ai_provisional_count}/{core_molecule_count} core "
            "molecules are covered by confirmed or AI-provisional evidence; "
            f"{blocked_count} remain blocked and are disclosed only as gaps or limitations."
        )
    traceability = [
        {
            key: copy.deepcopy(row[key])
            for key in (
                "study_id",
                "molecule_id",
                "status",
                "source_id",
                "pdf_locator",
                "provenance",
                "confidence",
            )
            if key in row and row[key] is not None
        }
        for row in normalized
    ]
    coverage_sufficient = coverage_ratio >= HONEST_PROGRESSIVE_COVERAGE_THRESHOLD
    return {
        "route": HONEST_PROGRESSIVE_ROUTE,
        "availability": "available",
        "status": "ready" if coverage_sufficient else "needs_more_traceable_candidates",
        "core_molecule_count": core_molecule_count,
        "confirmed_count": confirmed_count,
        "ai_provisional_count": ai_provisional_count,
        "blocked_count": blocked_count,
        "coverage_ratio": coverage_ratio,
        "coverage_threshold": HONEST_PROGRESSIVE_COVERAGE_THRESHOLD,
        "coverage_sufficient": coverage_sufficient,
        "paper_coverage": paper_coverage,
        "uncertainty_statement": uncertainty_statement.strip(),
        "gap_registry": gaps,
        "traceability": traceability,
        "actor_provenance_residual": actor_provenance_residual,
        "credits_status": "NOT_APPLICABLE_BY_CURRENT_SCOPE",
    }


def honest_progressive_summary_from_projection(
    value: object, *, project_scope: bool = False
) -> dict[str, Any] | None:
    """Read a future Domain projection or return ``None`` for legacy input."""

    if not isinstance(value, dict):
        return None
    candidate = value.get("honest_progressive", value)
    if not isinstance(candidate, dict):
        return None
    molecules = candidate.get("molecules", candidate.get("rows"))
    if isinstance(molecules, list):
        project_core_molecule_count = candidate.get("core_molecule_count")
        if project_scope and project_core_molecule_count is None:
            project_core_molecule_count = HONEST_PROGRESSIVE_PROJECT_CORE_MOLECULE_COUNT
        return build_honest_progressive_summary(
            {
                "molecules": molecules,
                "actor_provenance_residual": candidate.get(
                    "actor_provenance_residual"
                ),
            },
            core_molecule_count=project_core_molecule_count,
            uncertainty_statement=candidate.get("uncertainty_statement"),
            gap_registry=candidate.get("gap_registry"),
            paper_core_molecule_counts=candidate.get("paper_core_molecule_counts"),
        )
    required = {
        "route",
        "core_molecule_count",
        "confirmed_count",
        "ai_provisional_count",
        "blocked_count",
        "coverage_ratio",
        "coverage_threshold",
        "paper_coverage",
        "uncertainty_statement",
        "gap_registry",
        "traceability",
    }
    if candidate.get("route") != HONEST_PROGRESSIVE_ROUTE or not required.issubset(
        candidate
    ):
        return None
    integer_fields = (
        "core_molecule_count",
        "confirmed_count",
        "ai_provisional_count",
        "blocked_count",
    )
    if any(
        isinstance(candidate.get(key), bool)
        or not isinstance(candidate.get(key), int)
        or candidate[key] < 0
        for key in integer_fields
    ):
        raise PaperEvidenceError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    core_count = candidate["core_molecule_count"]
    confirmed = candidate["confirmed_count"]
    provisional = candidate["ai_provisional_count"]
    blocked = candidate["blocked_count"]
    if confirmed + provisional + blocked != core_count:
        raise PaperEvidenceError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    ratio = candidate["coverage_ratio"]
    threshold = candidate["coverage_threshold"]
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not 0 <= ratio <= 1
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
        or not isinstance(candidate["uncertainty_statement"], str)
        or not candidate["uncertainty_statement"].strip()
        or not isinstance(candidate["paper_coverage"], list)
        or not isinstance(candidate["gap_registry"], list)
        or not isinstance(candidate["traceability"], list)
    ):
        raise PaperEvidenceError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    expected_ratio = 1.0 if core_count == 0 else (confirmed + provisional) / core_count
    if (
        abs(float(ratio) - expected_ratio) > 1e-9
        or abs(float(threshold) - HONEST_PROGRESSIVE_COVERAGE_THRESHOLD) > 1e-9
        or (
            project_scope
            and "core_molecule_count" not in candidate
            and core_count != HONEST_PROGRESSIVE_PROJECT_CORE_MOLECULE_COUNT
        )
    ):
        raise PaperEvidenceError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    uncertainty = _safe_text(candidate["uncertainty_statement"])
    if uncertainty is None:
        uncertainty = (
            f"{confirmed + provisional}/{core_count} core molecules are covered; "
            f"{blocked} remain blocked and are disclosed as gaps or limitations."
        )
    availability = candidate.get("availability", "available")
    if availability not in {"available", "unknown", "unavailable"}:
        raise PaperEvidenceError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    status = candidate.get("status")
    expected_status = (
        "ready"
        if expected_ratio >= HONEST_PROGRESSIVE_COVERAGE_THRESHOLD
        else "needs_more_traceable_candidates"
    )
    benchmark_report = (
        isinstance(value, dict)
        and value.get("schema_version") == "review-benchmark-report.v1"
        and "release_binding" in value
    )
    if status is not None and not benchmark_report and status != expected_status:
        raise PaperEvidenceError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    return {
        "route": HONEST_PROGRESSIVE_ROUTE,
        "availability": availability,
        "status": expected_status,
        "core_molecule_count": core_count,
        "confirmed_count": confirmed,
        "ai_provisional_count": provisional,
        "blocked_count": blocked,
        "coverage_ratio": float(ratio),
        "coverage_threshold": HONEST_PROGRESSIVE_COVERAGE_THRESHOLD,
        "coverage_sufficient": expected_ratio
        >= HONEST_PROGRESSIVE_COVERAGE_THRESHOLD,
        "paper_coverage": _safe_paper_coverage(candidate["paper_coverage"]),
        "uncertainty_statement": uncertainty,
        "gap_registry": _safe_gap_registry(candidate["gap_registry"]),
        "traceability": _safe_traceability(candidate["traceability"]),
        "actor_provenance_residual": _safe_actor_provenance_residual(
            candidate.get("actor_provenance_residual")
        ),
        "credits_status": "NOT_APPLICABLE_BY_CURRENT_SCOPE",
    }


@contextmanager
def _mutation(project: Path):
    project = _project_root(project)
    try:
        with project_write_lock(project):
            yield project
    except PaperEvidenceStoreError as exc:
        raise PaperEvidenceError(exc.code) from exc


def _project_root(project: Path) -> Path:
    supplied = Path(project)
    if supplied.is_symlink() or not supplied.is_dir():
        raise PaperEvidenceError("PROJECT_INVALID")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise PaperEvidenceError("PROJECT_INVALID") from exc


def _identifier(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
        or len(value) > 240
    ):
        raise PaperEvidenceError(code)
    return value


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaperEvidenceError("PAPER_EVIDENCE_SCHEMA_INVALID") from exc
    if not isinstance(value, dict):
        raise PaperEvidenceError("PAPER_EVIDENCE_SCHEMA_INVALID")
    return value


def _validate_schema(value: object, path: Path, code: str) -> None:
    errors = sorted(
        Draft202012Validator(_load_schema(path)).iter_errors(value),
        key=lambda error: [str(part) for part in error.path],
    )
    if errors:
        raise PaperEvidenceError(code)


def _ensure_output_parent(project: Path, path: Path) -> None:
    relative = path.relative_to(project)
    current = project
    for part in relative.parts[:-1]:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise PaperEvidenceError("PAPER_EVIDENCE_PATH_INVALID")
        current.mkdir(exist_ok=True)
        if not current.is_dir() or current.is_symlink():
            raise PaperEvidenceError("PAPER_EVIDENCE_PATH_INVALID")


def _atomic_text(project: Path, path: Path, value: str) -> None:
    _ensure_output_parent(project, path)
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        raise PaperEvidenceError("PAPER_EVIDENCE_PATH_INVALID")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, UnicodeError) as exc:
        raise PaperEvidenceError("PAPER_EVIDENCE_WRITE_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_json(project: Path, path: Path, value: object) -> None:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise PaperEvidenceError("PAPER_EVIDENCE_INVALID") from exc
    _atomic_text(project, path, serialized)


def _atomic_jsonl(project: Path, path: Path, rows: Iterable[dict[str, Any]]) -> None:
    try:
        serialized = "".join(
            json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        )
    except (TypeError, ValueError) as exc:
        raise PaperEvidenceError("PAPER_EVIDENCE_INVALID") from exc
    _atomic_text(project, path, serialized)


def _read_json(path: Path, missing: str, invalid: str) -> Any:
    if not path.is_file() or path.is_symlink():
        if os.path.lexists(path):
            raise PaperEvidenceError("PAPER_EVIDENCE_PATH_INVALID")
        raise PaperEvidenceError(missing)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaperEvidenceError(invalid) from exc


def _read_jsonl(path: Path, *, missing_ok: bool = False) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        if os.path.lexists(path):
            raise PaperEvidenceError("PAPER_EVIDENCE_PATH_INVALID")
        if missing_ok:
            return []
        raise PaperEvidenceError("PAPER_EVIDENCE_MISSING")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaperEvidenceError("PAPER_EVIDENCE_INVALID") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise PaperEvidenceError("PAPER_EVIDENCE_INVALID")
    return rows


def _candidate_path(project: Path, study_id: str) -> Path:
    return project / "01_evidence" / _identifier(study_id, "STUDY_ID_INVALID") / "paper_evidence_candidates.json"


def _source(project: Path, study_id: str, source_id: str) -> dict[str, Any]:
    source = _source_descriptor(project, study_id, source_id)
    try:
        source_truth_asset(project, study_id, source_id, "pdf")
    except SourceTruthError as exc:
        raise PaperEvidenceError(exc.code) from exc
    return source


def _source_descriptor(project: Path, study_id: str, source_id: str) -> dict[str, Any]:
    try:
        bundle = load_source_truth_bundle(project, study_id)
    except SourceTruthError as exc:
        raise PaperEvidenceError(exc.code) from exc
    matches = [
        row
        for row in bundle.get("sources", [])
        if isinstance(row, dict) and row.get("source_id") == source_id
    ]
    if len(matches) != 1:
        raise PaperEvidenceError("SOURCE_ID_NOT_FOUND")
    return matches[0]


def current_source_pdf_sha256(
    project: Path,
    study_id: str | None = None,
    source_id: str | None = None,
) -> str:
    """Return the verified current PDF digest when the requested source is unique."""

    project = _project_root(project)
    try:
        studies = declared_study_ids(project)
    except SourceTruthError as exc:
        raise PaperEvidenceError(exc.code) from exc
    if study_id is None:
        if len(studies) != 1:
            raise PaperEvidenceError("STUDY_ID_REQUIRED")
        study_id = studies[0]
    _identifier(study_id, "STUDY_ID_INVALID")
    if source_id is None:
        try:
            bundle = load_source_truth_bundle(project, study_id)
        except SourceTruthError as exc:
            raise PaperEvidenceError(exc.code) from exc
        sources = bundle.get("sources", [])
        if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
            raise PaperEvidenceError("SOURCE_ID_REQUIRED")
        source_id = sources[0].get("source_id")
    source_id = _identifier(source_id, "SOURCE_ID_INVALID")
    source = _source(project, study_id, source_id)
    return str(source["pdf"]["sha256"])


def _parse_state(project: Path, study_id: str) -> dict[str, Any]:
    try:
        state = parse_quality_state(project, study_id)
    except ParseQualityError as exc:
        raise PaperEvidenceError(exc.code) from exc
    if not isinstance(state, dict) or not isinstance(state.get("objects"), list):
        raise PaperEvidenceError("PARSE_QUALITY_INVALID")
    return state


def _require_project_parse_ready(project: Path) -> None:
    state = project_parse_quality_state(project)
    studies = state.get("studies") if isinstance(state, dict) else None
    if (
        not isinstance(studies, list)
        or not studies
        or not state.get("workflow_can_continue")
        or not all(
            isinstance(row, dict) and row.get("workflow_can_continue") is True
            for row in studies
        )
    ):
        raise PaperEvidenceError("PROJECT_PARSE_QUALITY_NOT_READY")


def _candidate_digest(row: dict[str, Any]) -> str:
    return canonical_digest(
        {key: value for key, value in row.items() if key not in {"candidate_digest", "decision"}}
    )


def _normalize_string_list(value: object, code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 500
        or not all(
            isinstance(item, str) and item.strip() == item and 0 < len(item) <= 20000
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise PaperEvidenceError(code)
    return list(value)


def _field_dependencies(value: object) -> list[str]:
    if value is None:
        return []
    rows = _normalize_string_list(value, "FIELD_DEPENDENCIES_INVALID")
    if not set(rows).issubset({"molecule", "smiles", "molblock"}):
        raise PaperEvidenceError("FIELD_DEPENDENCIES_INVALID")
    return sorted(rows)


def _requires_exact_chemical_fields(row: dict[str, Any]) -> bool:
    dependencies = row.get("field_dependencies", [])
    return bool(EXACT_CHEMICAL_FIELD_DEPENDENCIES.intersection(dependencies))


def require_dual_evidence_ready(
    project: Path,
    study_id: str,
    *,
    requires_chemical: bool,
    allow_provisional: bool = True,
) -> dict[str, str | None]:
    """Fail closed on every current dual-parse dependency before Evidence writes."""
    try:
        chemical_required = study_source_tier(project, study_id) == "core" or requires_chemical
        bindings: dict[str, str | None] = {
            "dual_source_binding_digest": require_dual_source_ready(
                project, study_id, requires_chemical=chemical_required
            ),
            "chemical_completion_digest": None,
            "honest_progressive_digest": None,
            "reconciliation_digest": None,
        }
        if chemical_required:
            honest_digest = require_honest_progressive_projection(
                project,
                study_id,
                allow_provisional=allow_provisional,
            )
            # Keep the historical field populated for existing consumers while
            # making the Honest Progressive binding explicit for new consumers.
            bindings["chemical_completion_digest"] = honest_digest
            bindings["honest_progressive_digest"] = honest_digest
            bindings["reconciliation_digest"] = require_reconciliation_ready(project, study_id)
        return bindings
    except (SourceTruthError, DualSourceError, ChemicalCompletionError, ParseReconciliationError) as exc:
        raise PaperEvidenceError(exc.code) from exc


def _normalize_locator(value: object, expected_mode: str) -> dict[str, Any]:
    required = {"source_mode", "page", "section_or_item", "figure_or_table", "exact_quote"}
    if not isinstance(value, dict) or set(value) != required:
        raise PaperEvidenceError("LOCATOR_INVALID")
    page = value.get("page")
    section = value.get("section_or_item")
    if (
        value.get("source_mode") != expected_mode
        or not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
        or not isinstance(section, str)
        or not section.strip()
        or section != section.strip()
    ):
        raise PaperEvidenceError("LOCATOR_INVALID")
    for key in ("figure_or_table", "exact_quote"):
        item = value.get(key)
        if item is not None and (
            not isinstance(item, str) or not item.strip() or item != item.strip() or len(item) > 20000
        ):
            raise PaperEvidenceError("LOCATOR_INVALID")
    return copy.deepcopy(value)


def _normalize_candidate(
    project: Path,
    study_id: str,
    payload: object,
    *,
    source_mode: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PaperEvidenceError("PAPER_EVIDENCE_INVALID")
    allowed = {
        "evidence_id",
        "study_id",
        "source_id",
        "epistemic_type",
        "statement",
        "locator",
        "reported_conditions",
        "quantitative_results",
        "limitations",
        "mechanism_grade",
        "risk_classes",
        "field_dependencies",
        "bound_parse_object_digests",
        "source_pdf_sha256",
        "candidate_digest",
        "decision",
    }
    if not set(payload).issubset(allowed):
        raise PaperEvidenceError("PAPER_EVIDENCE_UNKNOWN_FIELD")
    if "epistemic_type" not in payload:
        raise PaperEvidenceError("EPISTEMIC_TYPE_REQUIRED")
    if payload.get("epistemic_type") not in EPISTEMIC_TYPES:
        raise PaperEvidenceError("EPISTEMIC_TYPE_INVALID")
    supplied_study = payload.get("study_id", study_id)
    if supplied_study != study_id:
        raise PaperEvidenceError("STUDY_ID_MISMATCH")
    evidence_id = _identifier(payload.get("evidence_id"), "EVIDENCE_ID_INVALID")
    source_id = _identifier(payload.get("source_id"), "SOURCE_ID_INVALID")
    statement = payload.get("statement")
    if (
        not isinstance(statement, str)
        or not statement.strip()
        or statement != statement.strip()
        or len(statement) > 20000
    ):
        raise PaperEvidenceError("STATEMENT_INVALID")
    locator = _normalize_locator(payload.get("locator"), source_mode)
    mechanism_grade = payload.get("mechanism_grade")
    if mechanism_grade not in {
        "not_applicable",
        "proposal",
        "indirect_support",
        "direct_support",
    }:
        raise PaperEvidenceError("MECHANISM_GRADE_INVALID")
    state = _parse_state(project, study_id)
    objects = state["objects"]
    current_digests = {
        row.get("object_digest")
        for row in objects
        if isinstance(row, dict) and isinstance(row.get("object_digest"), str)
    }
    supplied_digests = payload.get("bound_parse_object_digests")
    source_descriptor = _source_descriptor(project, study_id, source_id)
    current_objects = {
        row.get("object_digest"): row
        for row in objects
        if isinstance(row, dict) and isinstance(row.get("object_digest"), str)
    }
    if supplied_digests is None:
        bound_digests = (
            sorted(
                digest
                for digest, row in current_objects.items()
                if row.get("source_id") == source_id
            )
            if source_mode == "parsed_candidate"
            else []
        )
    else:
        bound_digests = _normalize_string_list(supplied_digests, "PARSE_OBJECT_DIGESTS_INVALID")
        if not all(SHA256_RE.fullmatch(value) for value in bound_digests):
            raise PaperEvidenceError("PARSE_OBJECT_DIGESTS_INVALID")
        bound_digests.sort()
    if source_mode == "parsed_candidate":
        if not state.get("automatic_extraction_allowed"):
            raise PaperEvidenceError("PARSED_EVIDENCE_NOT_ALLOWED")
        if not bound_digests or not set(bound_digests).issubset(current_digests):
            raise PaperEvidenceError("PARSE_OBJECT_DIGESTS_STALE")
        if any(
            current_objects[digest].get("source_id") != source_id
            for digest in bound_digests
        ):
            raise PaperEvidenceError("PARSE_OBJECT_SOURCE_MISMATCH")
    elif bound_digests:
        raise PaperEvidenceError("MANUAL_PDF_PARSE_BINDING_INVALID")
    source_sha256 = current_source_pdf_sha256(project, study_id, source_id)
    if locator["page"] > source_descriptor["page_count"]:
        raise PaperEvidenceError("LOCATOR_PAGE_INVALID")
    supplied_sha256 = payload.get("source_pdf_sha256")
    if supplied_sha256 is not None and supplied_sha256 != source_sha256:
        raise PaperEvidenceError("SOURCE_PDF_STALE")
    if payload.get("decision") is not None:
        raise PaperEvidenceError("PAPER_EVIDENCE_DECISION_FORBIDDEN")
    field_dependencies = _field_dependencies(payload.get("field_dependencies"))
    # An explicit empty dependency list is the locator-only Evidence route.
    # Keep it aligned with workflow_projection: only exact chemical fields or
    # an existing dual-source binding may activate the chemical seam.
    dual_enabled = bool(field_dependencies) or (
        project / f"01_evidence/dual_source/{study_id}/binding.json"
    ).is_file()
    dual_parse_bindings = (
        require_dual_evidence_ready(
            project,
            study_id,
            requires_chemical=bool(field_dependencies),
        )
        if dual_enabled
        else None
    )
    row: dict[str, Any] = {
        "evidence_id": evidence_id,
        "study_id": study_id,
        "source_id": source_id,
        "epistemic_type": payload["epistemic_type"],
        "statement": statement,
        "locator": locator,
        "reported_conditions": _normalize_string_list(
            payload.get("reported_conditions"), "REPORTED_CONDITIONS_INVALID"
        ),
        "quantitative_results": _normalize_string_list(
            payload.get("quantitative_results"), "QUANTITATIVE_RESULTS_INVALID"
        ),
        "limitations": _normalize_string_list(payload.get("limitations"), "LIMITATIONS_INVALID"),
        "mechanism_grade": mechanism_grade,
        "risk_classes": _normalize_string_list(payload.get("risk_classes"), "RISK_CLASSES_INVALID"),
        "field_dependencies": field_dependencies,
        "dual_parse_bindings": dual_parse_bindings,
        "bound_parse_object_digests": bound_digests,
        "source_pdf_sha256": source_sha256,
        "candidate_digest": "",
        "decision": None,
    }
    row["candidate_digest"] = _candidate_digest(row)
    supplied_digest = payload.get("candidate_digest")
    if supplied_digest is not None and supplied_digest != row["candidate_digest"]:
        raise PaperEvidenceError("CANDIDATE_DIGEST_MISMATCH")
    _validate_schema(row, PAPER_EVIDENCE_SCHEMA, "PAPER_EVIDENCE_SCHEMA_INVALID")
    return row


def _candidate_rows(payload: object) -> list[object]:
    if isinstance(payload, dict) and set(payload) == {"candidates"}:
        candidates = payload["candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise PaperEvidenceError("PAPER_EVIDENCE_INVALID")
        return list(candidates)
    return [payload]


def _load_candidates(project: Path, study_id: str, *, missing_ok: bool = False) -> list[dict[str, Any]]:
    path = _candidate_path(project, study_id)
    if missing_ok and not os.path.lexists(path):
        return []
    payload = _read_json(path, "PAPER_EVIDENCE_MISSING", "PAPER_EVIDENCE_INVALID")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "study_id", "candidates"}
        or payload.get("schema_version") != "paper-evidence-candidate-set.v1"
        or payload.get("study_id") != study_id
        or not isinstance(payload.get("candidates"), list)
        or not payload["candidates"]
    ):
        raise PaperEvidenceError("PAPER_EVIDENCE_INVALID")
    seen: set[str] = set()
    for row in payload["candidates"]:
        _validate_persisted_candidate(project, study_id, row)
        evidence_id = row.get("evidence_id")
        if evidence_id in seen:
            raise PaperEvidenceError("EVIDENCE_ID_DUPLICATE")
        seen.add(evidence_id)
    return copy.deepcopy(payload["candidates"])


def _validate_persisted_candidate(
    project: Path,
    study_id: str,
    row: object,
) -> None:
    _validate_schema(row, PAPER_EVIDENCE_SCHEMA, "PAPER_EVIDENCE_SCHEMA_INVALID")
    if not isinstance(row, dict):
        raise PaperEvidenceError("PAPER_EVIDENCE_SCHEMA_INVALID")
    if row.get("study_id") != study_id:
        raise PaperEvidenceError("PAPER_EVIDENCE_STUDY_MISMATCH")
    _identifier(row.get("evidence_id"), "EVIDENCE_ID_INVALID")
    _identifier(row.get("study_id"), "STUDY_ID_INVALID")
    _identifier(row.get("source_id"), "SOURCE_ID_INVALID")
    if not isinstance(row.get("statement"), str) or row["statement"] != row["statement"].strip():
        raise PaperEvidenceError("STATEMENT_INVALID")
    _normalize_locator(row.get("locator"), row["locator"]["source_mode"])
    for key, code in (
        ("reported_conditions", "REPORTED_CONDITIONS_INVALID"),
        ("quantitative_results", "QUANTITATIVE_RESULTS_INVALID"),
        ("limitations", "LIMITATIONS_INVALID"),
        ("risk_classes", "RISK_CLASSES_INVALID"),
    ):
        _normalize_string_list(row.get(key), code)
    _field_dependencies(row.get("field_dependencies"))
    _source_descriptor(project, study_id, row["source_id"])
    if row["locator"]["source_mode"] == "parsed_candidate":
        if not row["bound_parse_object_digests"]:
            raise PaperEvidenceError("PARSE_OBJECT_DIGESTS_INVALID")
    elif row["bound_parse_object_digests"]:
        raise PaperEvidenceError("MANUAL_PDF_PARSE_BINDING_INVALID")
    if row.get("decision") is not None:
        raise PaperEvidenceError("PAPER_EVIDENCE_DECISION_FORBIDDEN")
    if _candidate_digest(row) != row.get("candidate_digest"):
        raise PaperEvidenceError("CANDIDATE_DIGEST_MISMATCH")


def _check_candidate_id_conflicts(
    project: Path,
    study_id: str,
    candidates: list[dict[str, Any]],
) -> None:
    ids = [row["evidence_id"] for row in candidates]
    if len(ids) != len(set(ids)):
        raise PaperEvidenceError("EVIDENCE_ID_DUPLICATE")
    try:
        declared = declared_study_ids(project)
    except SourceTruthError as exc:
        raise PaperEvidenceError(exc.code) from exc
    for other_study_id in declared:
        if other_study_id == study_id:
            continue
        for previous in _load_candidates(project, other_study_id, missing_ok=True):
            if previous["evidence_id"] in ids:
                raise PaperEvidenceError("EVIDENCE_ID_DUPLICATE")
    existing = _load_candidates(project, study_id, missing_ok=True)
    existing_by_id = {row["evidence_id"]: row for row in existing}
    for row in candidates:
        previous = existing_by_id.get(row["evidence_id"])
        if previous is not None and previous != row:
            raise PaperEvidenceError("EVIDENCE_ID_CONFLICT")


def _merge_candidates(
    project: Path,
    study_id: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _check_candidate_id_conflicts(project, study_id, candidates)
    existing = _load_candidates(project, study_id, missing_ok=True)
    by_id = {row["evidence_id"]: row for row in existing}
    for row in candidates:
        previous = by_id.get(row["evidence_id"])
        if previous is not None and previous != row:
            raise PaperEvidenceError("EVIDENCE_ID_CONFLICT")
        by_id[row["evidence_id"]] = row
    merged = sorted(by_id.values(), key=lambda row: row["evidence_id"])
    _atomic_json(
        project,
        _candidate_path(project, study_id),
        {
            "schema_version": "paper-evidence-candidate-set.v1",
            "study_id": study_id,
            "candidates": merged,
        },
    )
    return merged


def register_paper_evidence_candidates(
    project: Path,
    study_id: str,
    payload: object,
) -> dict[str, Any]:
    """Register strict parsed candidates without granting approval."""

    project = _project_root(project)
    study_id = _identifier(study_id, "STUDY_ID_INVALID")
    _require_project_parse_ready(project)
    raw_candidates = _candidate_rows(payload)
    # Validate before creating the lockfile so rejected cross-source input is byte-preserving.
    [
        _normalize_candidate(project, study_id, row, source_mode="parsed_candidate")
        for row in raw_candidates
    ]
    candidates = [
        _normalize_candidate(project, study_id, row, source_mode="parsed_candidate")
        for row in raw_candidates
    ]
    _check_candidate_id_conflicts(project, study_id, candidates)
    with _mutation(project) as project:
        _require_project_parse_ready(project)
        candidates = [
            _normalize_candidate(project, study_id, row, source_mode="parsed_candidate")
            for row in raw_candidates
        ]
        merged = _merge_candidates(project, study_id, candidates)
        state = _paper_evidence_state(project, persist=True)
        return {
            "candidate_count": len(merged),
            "registered_count": len(candidates),
            "status": "needs_review",
            "study_id": study_id,
            "candidates": copy.deepcopy(candidates),
            "project_status": state["status"],
        }


def register_manual_pdf_evidence(project: Path, payload: object) -> dict[str, Any]:
    """Register one researcher-created locator against the verified original PDF."""

    project = _project_root(project)
    if not isinstance(payload, dict):
        raise PaperEvidenceError("PAPER_EVIDENCE_INVALID")
    study_id = _identifier(payload.get("study_id"), "STUDY_ID_INVALID")
    _require_project_parse_ready(project)
    prevalidated = _normalize_candidate(
        project, study_id, payload, source_mode="original_pdf_manual"
    )
    _check_candidate_id_conflicts(project, study_id, [prevalidated])
    with _mutation(project) as project:
        _require_project_parse_ready(project)
        state = _parse_state(project, study_id)
        actions = {
            row.get("decision", {}).get("action")
            for row in state["objects"]
            if isinstance(row, dict) and isinstance(row.get("decision"), dict)
        }
        if (
            not state.get("workflow_can_continue")
            or state.get("automatic_extraction_allowed")
            or "pdf_locator_only" not in actions
        ):
            raise PaperEvidenceError("MANUAL_PDF_EVIDENCE_NOT_ALLOWED")
        row = _normalize_candidate(project, study_id, payload, source_mode="original_pdf_manual")
        _merge_candidates(project, study_id, [row])
        _paper_evidence_state(project, persist=True)
        return copy.deepcopy(row)


def _validate_decision_event(row: object) -> dict[str, Any]:
    _validate_schema(row, EVIDENCE_DECISION_SCHEMA, "EVIDENCE_DECISION_SCHEMA_INVALID")
    assert isinstance(row, dict)
    decision = row["decision"]
    if (
        decision.get("bound_object_digest") != row.get("candidate_digest")
        or decision.get("action") not in DECISION_ACTIONS
    ):
        raise PaperEvidenceError("EVIDENCE_DECISION_BINDING_INVALID")
    return row


def _load_decisions(project: Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(project / DECISIONS_PATH, missing_ok=True)
    for row in rows:
        _validate_decision_event(row)
    return rows


def _candidate_index(project: Path) -> dict[str, dict[str, Any]]:
    try:
        studies = declared_study_ids(project)
    except SourceTruthError as exc:
        raise PaperEvidenceError(exc.code) from exc
    result: dict[str, dict[str, Any]] = {}
    for study_id in studies:
        for row in _load_candidates(project, study_id, missing_ok=True):
            evidence_id = row["evidence_id"]
            if evidence_id in result:
                raise PaperEvidenceError("EVIDENCE_ID_DUPLICATE")
            result[evidence_id] = row
    return result


def _normalize_decision_payload(
    candidate: dict[str, Any], payload: object
) -> tuple[dict[str, Any], dict[str, Any]]:
    required = {
        "evidence_id",
        "candidate_digest",
        "bound_parse_object_digests",
        "source_pdf_sha256",
        "action",
        "reason",
    }
    optional = {"replacement_statement", "actor_type", "actor_label"}
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or not set(payload).issubset(required | optional)
        or (("actor_type" in payload) != ("actor_label" in payload))
    ):
        raise PaperEvidenceError("EVIDENCE_DECISION_INVALID")
    action = payload.get("action")
    reason = payload.get("reason")
    replacement = payload.get("replacement_statement")
    actor_type = payload.get("actor_type", "human_researcher")
    actor_label = payload.get("actor_label", "local-researcher")
    bound_digests = payload.get("bound_parse_object_digests")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or reason != reason.strip()
        or len(reason) > 2000
    ):
        raise PaperEvidenceError("EVIDENCE_DECISION_INVALID")
    if (
        action not in DECISION_ACTIONS
        or actor_type not in ACTOR_TYPES
        or not isinstance(actor_label, str)
        or not actor_label.strip()
        or actor_label != actor_label.strip()
        or len(actor_label) > 200
        or not isinstance(bound_digests, list)
        or bound_digests != candidate["bound_parse_object_digests"]
        or payload.get("source_pdf_sha256") != candidate["source_pdf_sha256"]
        or payload.get("candidate_digest") != candidate["candidate_digest"]
    ):
        raise PaperEvidenceError("EVIDENCE_DECISION_STALE")
    if action == "revise_and_approve":
        if (
            not isinstance(replacement, str)
            or not replacement.strip()
            or replacement != replacement.strip()
            or len(replacement) > 20000
        ):
            raise PaperEvidenceError("REPLACEMENT_STATEMENT_REQUIRED")
    elif replacement is not None:
        raise PaperEvidenceError("REPLACEMENT_STATEMENT_FORBIDDEN")
    try:
        decision = verification_decision(
            actor_type=actor_type,
            actor_label=actor_label,
            action=action,
            reason=reason,
            bound_object_digest=candidate["candidate_digest"],
        )
    except VerificationDecisionError as exc:
        raise PaperEvidenceError("EVIDENCE_DECISION_INVALID") from exc
    event = {
        "schema_version": "paper-evidence-decision.v1",
        "evidence_id": candidate["evidence_id"],
        "study_id": candidate["study_id"],
        "candidate_digest": candidate["candidate_digest"],
        "bound_parse_object_digests": list(candidate["bound_parse_object_digests"]),
        "source_pdf_sha256": candidate["source_pdf_sha256"],
        "replacement_statement": replacement,
        "decision": decision,
    }
    _validate_decision_event(event)
    semantic = {
        key: value
        for key, value in event.items()
        if key != "decision"
    }
    semantic["decision"] = {
        key: value for key, value in decision.items() if key != "decided_at"
    }
    return event, semantic


def apply_paper_evidence_decision(project: Path, payload: object) -> dict[str, Any]:
    """Append one current, hash-bound human decision and rebuild the projection."""

    project = _project_root(project)
    if not isinstance(payload, dict):
        raise PaperEvidenceError("EVIDENCE_DECISION_INVALID")
    with _mutation(project) as project:
        evidence_id = _identifier(payload.get("evidence_id"), "EVIDENCE_ID_INVALID")
        candidates = _candidate_index(project)
        candidate = candidates.get(evidence_id)
        if candidate is None:
            raise PaperEvidenceError("EVIDENCE_ID_NOT_FOUND")
        freshness, _ = _freshness(project, candidate)
        if not freshness:
            raise PaperEvidenceError("PAPER_EVIDENCE_STALE")
        bindings = candidate.get("dual_parse_bindings")
        if (
            isinstance(bindings, dict)
            and bindings.get("honest_progressive_digest")
            and _requires_exact_chemical_fields(candidate)
        ):
            try:
                require_honest_progressive_projection(
                    project,
                    candidate["study_id"],
                    allow_provisional=False,
                )
            except ChemicalCompletionError as exc:
                raise PaperEvidenceError("PAPER_EVIDENCE_STALE") from exc
        event, semantic = _normalize_decision_payload(candidate, payload)
        decisions = _load_decisions(project)
        prior_for_evidence = [
            row for row in decisions if row.get("evidence_id") == evidence_id
        ]
        if prior_for_evidence:
            previous = prior_for_evidence[-1]
            previous_semantic = {key: value for key, value in previous.items() if key != "decision"}
            previous_semantic["decision"] = {
                key: value for key, value in previous["decision"].items() if key != "decided_at"
            }
            if previous.get("evidence_id") == evidence_id and previous_semantic == semantic:
                state = _paper_evidence_state(project, persist=True)
                return next(row for row in state["rows"] if row["evidence_id"] == evidence_id)
        decisions.append(event)
        _atomic_jsonl(project, project / DECISIONS_PATH, decisions)
        state = _paper_evidence_state(project, persist=True)
        return next(row for row in state["rows"] if row["evidence_id"] == evidence_id)


def _freshness(project: Path, candidate: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        source = _source(project, candidate["study_id"], candidate["source_id"])
        current_sha = str(source["pdf"]["sha256"])
        state = _parse_state(project, candidate["study_id"])
    except PaperEvidenceError as exc:
        return False, exc.code
    if candidate["source_pdf_sha256"] != current_sha:
        return False, "SOURCE_PDF_STALE"
    if candidate["locator"]["page"] > source["page_count"]:
        return False, "LOCATOR_PAGE_STALE"
    bindings = candidate.get("dual_parse_bindings")
    if isinstance(bindings, dict):
        try:
            current_bindings = require_dual_evidence_ready(
                project,
                candidate["study_id"],
                requires_chemical=bool(candidate.get("field_dependencies")),
            )
        except PaperEvidenceError as exc:
            return False, exc.code
        if bindings != current_bindings:
            return False, "DUAL_PARSE_BINDING_STALE"
    if candidate["locator"]["source_mode"] == "original_pdf_manual":
        return (not candidate["bound_parse_object_digests"], "MANUAL_PDF_PARSE_BINDING_INVALID")
    reviewed_objects = {
        row.get("object_digest"): row
        for row in state["objects"]
        if isinstance(row, dict) and isinstance(row.get("object_digest"), str)
    }
    if state.get("status") == "stale":
        try:
            current_bundle = load_source_truth_bundle(project, candidate["study_id"])
            current_gate = build_parse_quality_gate(project, current_bundle)
        except (ParseQualityError, SourceTruthError) as exc:
            return False, exc.code
        current_rows = current_gate["objects"]
    else:
        current_rows = state["objects"]
    current_objects = {
        row.get("object_digest"): row
        for row in current_rows
        if isinstance(row, dict) and isinstance(row.get("object_digest"), str)
    }
    dependencies = set(candidate["bound_parse_object_digests"])
    if not dependencies.issubset(current_objects) or not dependencies.issubset(reviewed_objects):
        return False, "PARSE_OBJECT_DIGESTS_STALE"
    for digest in dependencies:
        if current_objects[digest].get("source_id") != candidate["source_id"]:
            return False, "PARSE_OBJECT_SOURCE_MISMATCH"
        decision = reviewed_objects[digest].get("decision")
        if isinstance(decision, dict) and decision.get("action") != "approve_candidate_extraction":
            return False, "PARSE_OBJECT_DECISION_STALE"
    return True, None


def _project_row(
    project: Path,
    candidate: dict[str, Any],
    decision: dict[str, Any] | None,
) -> dict[str, Any]:
    row = copy.deepcopy(candidate)
    fresh, reason = _freshness(project, candidate)
    if not fresh:
        row.update({"status": "stale", "reason_code": reason})
        return row
    if decision is None:
        row.update({"status": "needs_review", "reason_code": "PAPER_EVIDENCE_REVIEW_REQUIRED"})
        return row
    binding_matches = (
        decision["candidate_digest"] == candidate["candidate_digest"]
        and decision["bound_parse_object_digests"]
        == candidate["bound_parse_object_digests"]
        and decision["source_pdf_sha256"] == candidate["source_pdf_sha256"]
        and decision["decision"]["bound_object_digest"] == candidate["candidate_digest"]
    )
    if not binding_matches:
        row.update({"status": "stale", "reason_code": "EVIDENCE_DECISION_STALE"})
        return row
    row["decision"] = copy.deepcopy(decision["decision"])
    action = decision["decision"]["action"]
    if action == "reject":
        row.update({"status": "rejected", "reason_code": "PAPER_EVIDENCE_REJECTED"})
    else:
        if action == "revise_and_approve":
            row["statement"] = decision["replacement_statement"]
        row.update({"status": "approved", "reason_code": "PAPER_EVIDENCE_APPROVED"})
    return row


def _paper_evidence_state(project: Path, *, persist: bool) -> dict[str, Any]:
    try:
        studies = declared_study_ids(project)
    except SourceTruthError as exc:
        raise PaperEvidenceError(exc.code) from exc
    decisions = _load_decisions(project)
    latest: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        latest[decision["evidence_id"]] = decision
    rows: list[dict[str, Any]] = []
    missing_studies: list[str] = []
    for study_id in studies:
        candidates = _load_candidates(project, study_id, missing_ok=True)
        if not candidates:
            missing_studies.append(study_id)
            continue
        for row in candidates:
            decision = latest.get(row["evidence_id"])
            if decision is not None and decision.get("study_id") != row["study_id"]:
                raise PaperEvidenceError("EVIDENCE_DECISION_STUDY_MISMATCH")
            rows.append(_project_row(project, row, decision))
    if len({row["evidence_id"] for row in rows}) != len(rows):
        raise PaperEvidenceError("EVIDENCE_ID_DUPLICATE")
    known_ids = {row["evidence_id"] for row in rows}
    if any(row["evidence_id"] not in known_ids for row in decisions):
        raise PaperEvidenceError("EVIDENCE_DECISION_ORPHANED")
    rows.sort(key=lambda row: (row["study_id"], row["evidence_id"]))
    counts = {
        status: sum(row["status"] == status for row in rows)
        for status in ("approved", "rejected", "needs_review", "stale")
    }
    approved_studies = {row["study_id"] for row in rows if row["status"] == "approved"}
    settled = bool(rows) and all(row["status"] in {"approved", "rejected"} for row in rows)
    ready = settled and not missing_studies and approved_studies == set(studies)
    if missing_studies:
        reason_code = "PAPER_EVIDENCE_MISSING"
    elif counts["stale"]:
        reason_code = "PAPER_EVIDENCE_STALE"
    elif counts["needs_review"]:
        reason_code = "PAPER_EVIDENCE_REVIEW_REQUIRED"
    elif approved_studies != set(studies):
        reason_code = "PAPER_EVIDENCE_APPROVED_ROW_MISSING"
    elif ready:
        reason_code = "PAPER_EVIDENCE_READY"
    else:
        reason_code = "PAPER_EVIDENCE_NOT_READY"
    projection_digest = canonical_digest(rows)
    try:
        honest_summary = build_honest_progressive_summary(
            rows, core_molecule_count=len(rows)
        )
    except PaperEvidenceError:
        # Existing v1 projections remain readable even when they lack the
        # optional Honest Progressive provenance fields.
        honest_summary = None
    if persist:
        _atomic_jsonl(project, project / PROJECTION_PATH, rows)
    result: dict[str, Any] = {
        "status": "approved" if ready else "needs_review",
        "reason_code": reason_code,
        "workflow_can_continue": ready,
        "projection_digest": projection_digest,
        "study_count": len(studies),
        "missing_study_count": len(missing_studies),
        "total_count": len(rows),
        "approved_count": counts["approved"],
        "rejected_count": counts["rejected"],
        "needs_review_count": counts["needs_review"],
        "stale_count": counts["stale"],
        "rows": rows,
    }
    if honest_summary is not None:
        result.update(honest_summary)
    else:
        result["route"] = HONEST_PROGRESSIVE_ROUTE
    return result


def paper_evidence_state(project: Path) -> dict[str, Any]:
    """Rebuild the current fail-closed evidence projection."""

    project = _project_root(project)
    return _paper_evidence_state(project, persist=False)


def require_paper_evidence_ready(project: Path) -> str:
    """Return the current projection digest only when every study is reviewed."""

    state = paper_evidence_state(project)
    if not state["workflow_can_continue"]:
        raise PaperEvidenceError(state["reason_code"])
    return str(state["projection_digest"])
