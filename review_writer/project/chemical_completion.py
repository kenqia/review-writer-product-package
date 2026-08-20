"""Researcher-only completeness gate for Chemical Paper molecule fields."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from review_writer.project.chemical_paper import (
    CORE_COVERAGE_THRESHOLD,
    CORE_MOLECULE_COUNT,
    ChemicalPaperError,
    FIELD_NAMES,
    _resolved_smiles_resolution,
    _atomic_json,
    _actor_provenance_mismatch,
    _canonical_state_digest,
    _current_value,
    _molecule_by_index,
    _now,
    _resolution_metadata,
    _researcher_safe_provenance,
    _state_path,
    _valid_resolved_smiles,
    _validate_state,
    _version_token,
    load_chemical_paper_state,
)
from review_writer.project.paper_evidence_store import PaperEvidenceStoreError, project_write_lock
from review_writer.project.source_truth import (
    SourceTruthError,
    canonical_digest,
    declared_study_ids,
    study_source_tier,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "schemas/evidence/chemical_completion_gate.v2.schema.json"
ACTOR_TYPES = frozenset({"human_researcher", "simulated_researcher_agent"})
CURRENT_CORE_AGGREGATION_MODE = "project_core_current"
AUTHORITATIVE_VARIABLE_N = "authoritative_variable_n"
LEGACY_THREE_PAPER = "legacy_three_paper"
MIN_VARIABLE_N_STUDIES = 20
MAX_VARIABLE_N_STUDIES = 40
LEGACY_STUDY_COUNT = 3


class ChemicalCompletionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _validate_gate(value: dict[str, Any]) -> dict[str, Any]:
    try:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_SCHEMA_INVALID") from exc
    if list(Draft202012Validator(schema).iter_errors(value)):
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_STATE_INVALID")
    body = {key: item for key, item in value.items() if key != "gate_digest"}
    if canonical_digest(body) != value["gate_digest"]:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_STATE_INVALID")
    return value


def _project_aggregation_mode(root: Path, declared_count: int) -> str | None:
    """Read the authoritative current/legacy project marker from the receipt."""

    receipt_path = root / "00_sources/acquisition_final_receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChemicalCompletionError(
            "CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID"
        ) from exc
    if not isinstance(receipt, dict):
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID")

    marker_keys = {"corpus_kind", "variable_n", "study_count"}
    present_keys = marker_keys.intersection(receipt)
    if not present_keys:
        return None
    if present_keys != marker_keys:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID")
    studies = receipt.get("studies")
    study_count = receipt.get("study_count")
    if (
        not isinstance(studies, list)
        or study_count != len(studies)
        or study_count != declared_count
        or isinstance(study_count, bool)
    ):
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID")

    corpus_kind = receipt["corpus_kind"]
    variable_n = receipt["variable_n"]
    if corpus_kind == AUTHORITATIVE_VARIABLE_N and variable_n is True:
        if not MIN_VARIABLE_N_STUDIES <= study_count <= MAX_VARIABLE_N_STUDIES:
            raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID")
        return CURRENT_CORE_AGGREGATION_MODE
    if corpus_kind == LEGACY_THREE_PAPER and variable_n is False:
        if study_count != LEGACY_STUDY_COUNT:
            raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID")
        return "project_core_309"
    raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_INVALID")


def _core_study_scope(root: Path, study_ids: list[str]) -> tuple[list[str], bool]:
    """Return core studies and whether an explicit tiered candidate pool exists."""

    if not (root / "00_discovery/candidate_pool.json").is_file():
        return list(study_ids), False
    core_study_ids: list[str] = []
    for study_id in study_ids:
        try:
            if study_source_tier(root, study_id) == "core":
                core_study_ids.append(study_id)
        except SourceTruthError as exc:
            raise ChemicalCompletionError(exc.code) from exc
    return core_study_ids, True


def _state_resolution_counts(state: dict[str, Any]) -> dict[str, int | bool]:
    counts = {
        "confirmed_count": 0,
        "ai_provisional_count": 0,
        "blocked_count": 0,
        "legacy_unclassified_count": 0,
    }
    actor_residual = False
    for molecule in state["molecules"]:
        resolution = _resolved_smiles_resolution(state, molecule)
        status = resolution["resolved_smiles_status"]
        if status == "CONFIRMED":
            counts["confirmed_count"] += 1
        elif status == "AI_PROVISIONAL":
            counts["ai_provisional_count"] += 1
        elif status == "BLOCKED":
            counts["blocked_count"] += 1
        if resolution["legacy_unclassified"]:
            counts["legacy_unclassified_count"] += 1
    actor_residual = any(
        event["field"] == "resolved_smiles"
        and event.get("resolution_status") != "AI_PROVISIONAL"
        and (
            _actor_provenance_mismatch(event["actor"])
            or event["actor"]["actor_type"] != "human_researcher"
        )
        for event in state["field_corrections"]
    )
    counts["actor_provenance_residual"] = actor_residual
    return counts


def chemical_completion_state(project: Path, study_id: str) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        state = load_chemical_paper_state(root, study_id)
    except ChemicalPaperError as exc:
        raise ChemicalCompletionError(exc.code) from exc
    try:
        declared = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ChemicalCompletionError(exc.code) from exc
    aggregation_mode = _project_aggregation_mode(root, len(declared))
    if aggregation_mode is None and len(declared) > 1:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_REQUIRED")
    core_study_ids, _ = _core_study_scope(root, declared)
    aggregate_states: list[dict[str, Any]] = []
    for other_study_id in core_study_ids:
        if other_study_id == study_id:
            aggregate_states.append(state)
            continue
        try:
            aggregate_states.append(load_chemical_paper_state(root, other_study_id))
        except ChemicalPaperError as exc:
            if exc.code != "CHEMICAL_PAPER_NOT_IMPORTED":
                raise ChemicalCompletionError(exc.code) from exc
    if not aggregate_states:
        raise ChemicalCompletionError("CORE_MOLECULES_NOT_DECLARED")
    missing = {field: 0 for field in FIELD_NAMES}
    missing_rows: list[dict[str, Any]] = []
    molecules: list[dict[str, Any]] = []
    gap_registry: list[dict[str, Any]] = []
    uncertainty_registry: list[dict[str, Any]] = []
    legacy_unclassified_registry: list[dict[str, Any]] = []
    confirmed_count = 0
    ai_provisional_count = 0
    blocked_count = 0
    actor_provenance_residual = False
    for index, molecule in enumerate(state["molecules"]):
        for field in FIELD_NAMES:
            if _current_value(state, molecule, field) is None:
                missing[field] += 1
                missing_rows.append({
                    "molecule_index": index, "field": field,
                    "page": molecule["page_index"] + 1,
                    "bbox_normalized": molecule["normalized_bbox"],
                })
        resolution = _resolved_smiles_resolution(state, molecule)
        status = resolution["resolved_smiles_status"]
        if status == "CONFIRMED":
            confirmed_count += 1
        elif status == "AI_PROVISIONAL":
            ai_provisional_count += 1
        elif status == "BLOCKED":
            blocked_count += 1
            gap_registry.append({
                "molecule_index": index,
                "status": "BLOCKED",
                "value": None,
                "gap_reason": resolution["gap_reason"],
                "pdf_locator": resolution["pdf_locator"],
            })
        if status in {"CONFIRMED", "AI_PROVISIONAL"}:
            uncertainty_registry.append({
                "molecule_index": index,
                "status": status,
                "value": resolution["resolved_smiles"],
                "confidence": resolution["confidence"],
                "provenance": resolution["provenance"],
                "pdf_locator": resolution["pdf_locator"],
            })
        if resolution["legacy_unclassified"]:
            legacy_unclassified_registry.append({
                "molecule_index": index,
                "legacy_unclassified": True,
                "gap_reason": resolution["gap_reason"],
                "pdf_locator": resolution["pdf_locator"],
            })
        molecules.append({
            "molecule_index": index,
            "resolved_smiles": resolution["resolved_smiles"],
            "resolved_smiles_status": status,
            "confidence": resolution["confidence"],
            "provenance": resolution["provenance"],
            "pdf_locator": resolution["pdf_locator"],
            "gap_reason": resolution["gap_reason"],
            "legacy_unclassified": resolution["legacy_unclassified"],
        })
    actor_provenance_residual = any(
        event["field"] == "resolved_smiles"
        and event.get("resolution_status") != "AI_PROVISIONAL"
        and (
            _actor_provenance_mismatch(event["actor"])
            or event["actor"]["actor_type"] != "human_researcher"
        )
        for event in state["field_corrections"]
    )
    history = [
        {
            "molecule_index": next(
                (
                    index
                    for index, molecule in enumerate(state["molecules"])
                    if molecule["molecule_id"] == event["molecule_id"]
                ),
                None,
            ),
            "field": event["field"], "value": event["value"],
            "actor_type": event["actor"]["actor_type"], "actor_label": event["actor"]["actor_label"],
            "reason": event["reason"], "pdf_locator": event["pdf_locator"],
            "recorded_at": event["recorded_at"],
            **({"resolution_status": event["resolution_status"]} if "resolution_status" in event else {}),
            **({"confidence": event["confidence"]} if "confidence" in event else {}),
            **({"provenance": _researcher_safe_provenance(event["provenance"])} if "provenance" in event else {}),
        }
        for event in state["field_corrections"]
    ]
    molecule_count = len(state["molecules"])
    study_confirmed_count = confirmed_count
    study_ai_provisional_count = ai_provisional_count
    study_blocked_count = blocked_count
    aggregate_counts = {
        "confirmed_count": sum(
            int(_state_resolution_counts(item)["confirmed_count"])
            for item in aggregate_states
        ),
        "ai_provisional_count": sum(
            int(_state_resolution_counts(item)["ai_provisional_count"])
            for item in aggregate_states
        ),
        "blocked_count": sum(
            int(_state_resolution_counts(item)["blocked_count"])
            for item in aggregate_states
        ),
        "legacy_unclassified_count": sum(
            int(_state_resolution_counts(item)["legacy_unclassified_count"])
            for item in aggregate_states
        ),
        "actor_provenance_residual": any(
            bool(_state_resolution_counts(item)["actor_provenance_residual"])
            for item in aggregate_states
        ),
    }
    aggregate_molecule_count = sum(len(item["molecules"]) for item in aggregate_states)
    current_core_aggregation = aggregation_mode == CURRENT_CORE_AGGREGATION_MODE
    fixed_core_denominator = aggregation_mode == "project_core_309"
    coverage_denominator = (
        CORE_MOLECULE_COUNT if fixed_core_denominator else aggregate_molecule_count
    )
    legacy_compatibility_count = (
        int(aggregate_counts["legacy_unclassified_count"])
        if not fixed_core_denominator and not current_core_aggregation
        else 0
    )
    confirmed_count = int(aggregate_counts["confirmed_count"])
    ai_provisional_count = int(aggregate_counts["ai_provisional_count"])
    blocked_count = int(aggregate_counts["blocked_count"])
    actor_provenance_residual = bool(aggregate_counts["actor_provenance_residual"])
    covered_count = confirmed_count + ai_provisional_count + legacy_compatibility_count
    coverage_ratio = covered_count / coverage_denominator if coverage_denominator else 0.0
    core_molecule_count = (
        coverage_denominator
        if current_core_aggregation
        else CORE_MOLECULE_COUNT
    )
    compatibility_aggregation = {
        "mode": aggregation_mode or "legacy_subset",
        "source": (
            "Current imported core Chemical Paper states; background studies are excluded from the denominator."
            if current_core_aggregation
            else "Explicit core/background tiering; background studies are excluded from the 309 core gate."
            if fixed_core_denominator
            else "Legacy subset fixture without a complete 309-molecule core cohort; ratio is compatibility-only."
        ),
    }
    safe_flags = ["actor_provenance_residual"] if actor_provenance_residual else []
    body: dict[str, Any] = {
        "schema_version": "chemical-completion-gate.v2", "project_id": root.name,
        "study_id": study_id, "molecule_count": molecule_count,
        "missing_name_count": missing["mol_idt"],
        "missing_resolved_smiles_count": missing["resolved_smiles"],
        "ai_authored_smiles_count": 0,
        "missing_fields": missing_rows, "history": history,
        "version_token": _version_token(state),
        "workflow_can_continue": coverage_ratio >= CORE_COVERAGE_THRESHOLD,
        "route": "honest_progressive",
        "core_molecule_count": core_molecule_count,
        "coverage_denominator": coverage_denominator,
        "covered_count": covered_count,
        "confirmed_count": confirmed_count,
        "ai_provisional_count": ai_provisional_count,
        "blocked_count": blocked_count,
        "legacy_unclassified_count": int(aggregate_counts["legacy_unclassified_count"]),
        "study_legacy_unclassified_count": len(legacy_unclassified_registry),
        "coverage_ratio": coverage_ratio,
        "coverage_threshold": CORE_COVERAGE_THRESHOLD,
        "coverage_target_count": math.ceil(coverage_denominator * CORE_COVERAGE_THRESHOLD),
        "coverage_scope": "project_core_molecules",
        "project_molecule_count": aggregate_molecule_count,
        "study_molecule_count": molecule_count,
        "study_confirmed_count": study_confirmed_count,
        "study_ai_provisional_count": study_ai_provisional_count,
        "study_blocked_count": study_blocked_count,
        "coverage_aggregation": (
            "Project-level denominator derived from current imported core Chemical Paper states; "
            "background studies are excluded and this study is an explanatory slice."
            if current_core_aggregation
            else "Project-level fixed denominator of 309 core molecules; "
            "only core-tier studies contribute and this study is an explanatory slice."
            if fixed_core_denominator
            else "Legacy subset compatibility: no complete 309-molecule core cohort is present."
        ),
        "compatibility_aggregation": compatibility_aggregation,
        "uncertainty_registry": uncertainty_registry,
        "gap_registry": gap_registry,
        "legacy_unclassified_registry": legacy_unclassified_registry,
        "uncertainty_disclosure": (
            "uncertainty_registry discloses classified molecules; "
            "gap_registry contains BLOCKED molecules only."
        ),
        "molecules": molecules,
        "actor_provenance_residual": actor_provenance_residual,
        "safe_flags": safe_flags,
    }
    return _validate_gate({**body, "gate_digest": canonical_digest(body)})


def _actor(payload: dict[str, Any]) -> dict[str, str]:
    actor_type, actor_label = payload.get("actor_type"), payload.get("actor_label")
    if actor_type not in ACTOR_TYPES or not isinstance(actor_label, str) or not actor_label.strip() or actor_label != actor_label.strip():
        raise ChemicalCompletionError("RESEARCHER_ACTOR_REQUIRED")
    return {"actor_type": actor_type, "actor_label": actor_label}


def _locator(value: object, page_count: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"page", "figure_label", "bbox"}:
        raise ChemicalCompletionError("PDF_LOCATOR_INVALID")
    page = value.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= page_count:
        raise ChemicalCompletionError("PDF_LOCATOR_INVALID")
    label = value.get("figure_label")
    if label is not None and (not isinstance(label, str) or not label.strip() or label != label.strip()):
        raise ChemicalCompletionError("PDF_LOCATOR_INVALID")
    bbox = value.get("bbox")
    if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in bbox)):
        raise ChemicalCompletionError("PDF_LOCATOR_INVALID")
    return copy.deepcopy(value)


def _value(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 20000:
        raise ChemicalCompletionError("CHEMICAL_FIELD_VALUE_INVALID")
    if field == "resolved_smiles" and not _valid_resolved_smiles(value):
        raise ChemicalCompletionError("SMILES_INVALID")
    return value


def apply_chemical_completion_batch(project: Path, study_id: str, payload: object) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    if not isinstance(payload, dict) or set(payload) != {"version_token", "actor_type", "actor_label", "corrections"}:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_BATCH_INVALID")
    actor = _actor(payload)
    corrections = payload.get("corrections")
    if not isinstance(corrections, list) or not corrections:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_BATCH_INVALID")
    try:
        with project_write_lock(root):
            state = load_chemical_paper_state(root, study_id)
            if payload.get("version_token") != _version_token(state):
                raise ChemicalCompletionError("STALE_CHEMICAL_COMPLETION")
            active = state["imports"][state["current_import_digest"]]
            normalized: list[
                tuple[dict[str, Any], str, str | None, str, dict[str, Any], dict[str, Any]]
            ] = []
            seen: set[tuple[int, str]] = set()
            for row in corrections:
                allowed = {
                    "molecule_index", "field", "value", "reason", "pdf_locator",
                    "resolution_status", "confidence", "provenance", "gap_reason",
                }
                required = {"molecule_index", "field", "value", "reason", "pdf_locator"}
                if not isinstance(row, dict) or not required <= set(row) or not set(row) <= allowed:
                    raise ChemicalCompletionError("CHEMICAL_COMPLETION_BATCH_INVALID")
                index, field = row.get("molecule_index"), row.get("field")
                if not isinstance(index, int) or isinstance(index, bool) or field not in FIELD_NAMES or (index, field) in seen:
                    raise ChemicalCompletionError("CHEMICAL_COMPLETION_BATCH_INVALID")
                seen.add((index, field))
                try:
                    molecule = _molecule_by_index(state, index)
                except ChemicalPaperError as exc:
                    raise ChemicalCompletionError(exc.code) from exc
                if _current_value(state, molecule, field) is not None:
                    raise ChemicalCompletionError("CHEMICAL_FIELD_ALREADY_COMPLETE")
                reason = row.get("reason")
                if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
                    raise ChemicalCompletionError("CHEMICAL_COMPLETION_REASON_REQUIRED")
                status = row.get("resolution_status")
                if field == "resolved_smiles" and status is None:
                    raise ChemicalCompletionError("RESOLUTION_STATUS_REQUIRED")
                if status == "BLOCKED":
                    if field != "resolved_smiles" or row.get("value") is not None:
                        raise ChemicalCompletionError("BLOCKED_VALUE_MUST_BE_NULL")
                    gap_reason = row.get("gap_reason")
                    if not isinstance(gap_reason, str) or not gap_reason.strip() or gap_reason != gap_reason.strip():
                        raise ChemicalCompletionError("GAP_REASON_REQUIRED")
                    normalized_value = None
                else:
                    normalized_value = _value(field, row.get("value"))
                    gap_reason = None
                resolution_metadata = _resolution_metadata(
                    status,
                    row.get("confidence"),
                    row.get("provenance"),
                    actor,
                    gap_reason,
                )
                normalized.append((molecule, field, normalized_value, reason, _locator(row.get("pdf_locator"), active["page_count"]), resolution_metadata))
            # Complete the read-only current-project preflight before mutating state.
            chemical_completion_state(root, study_id)
            updated = copy.deepcopy(state)
            for molecule, field, value, reason, locator, resolution_metadata in normalized:
                event = {
                    "molecule_id": molecule["molecule_id"], "field": field,
                    "prior_value": _current_value(updated, molecule, field), "value": value, "actor": actor, "reason": reason,
                    "pdf_locator": locator, "recorded_at": _now(),
                    "bound_import_digest": updated["current_import_digest"],
                    "bound_molecule_digest": molecule["molecule_digest"],
                    "prior_event_digest": updated["field_correction_head_digest"],
                    **resolution_metadata,
                }
                event["event_digest"] = canonical_digest(event)
                updated["field_corrections"].append(event)
                updated["field_correction_head_digest"] = event["event_digest"]
            updated["state_digest"] = _canonical_state_digest(updated)
            _validate_state(updated)
            _atomic_json(_state_path(root, study_id), updated)
    except PaperEvidenceStoreError as exc:
        raise ChemicalCompletionError(exc.code) from exc
    except ChemicalPaperError as exc:
        raise ChemicalCompletionError(exc.code) from exc
    state_view = chemical_completion_state(root, study_id)
    return {"status": "applied", "study_id": study_id, "applied_count": len(corrections), "version_token": state_view["version_token"], "gate_digest": state_view["gate_digest"]}


def require_honest_progressive_projection(
    project: Path,
    study_id: str,
    *,
    allow_provisional: bool = True,
) -> str:
    """Require a current, structurally valid tri-state projection.

    ``allow_provisional`` controls whether an exact downstream consumer may
    use the projection.  BLOCKED rows remain valid limitation disclosures, but
    AI_PROVISIONAL rows never satisfy an exact consumer.
    """

    gate = chemical_completion_state(project, study_id)
    molecules = gate.get("molecules")
    if not isinstance(molecules, list) or len(molecules) != gate.get("molecule_count"):
        raise ChemicalCompletionError("HONEST_PROGRESSIVE_PROJECTION_INVALID")
    for row in molecules:
        if not isinstance(row, dict) or row.get("resolved_smiles_status") not in {
            "CONFIRMED",
            "AI_PROVISIONAL",
            "BLOCKED",
        }:
            raise ChemicalCompletionError("HONEST_PROGRESSIVE_PROJECTION_INVALID")
        status = row["resolved_smiles_status"]
        value = row.get("resolved_smiles")
        if status in {"CONFIRMED", "AI_PROVISIONAL"} and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ChemicalCompletionError("HONEST_PROGRESSIVE_VALUE_REQUIRED")
        if status == "AI_PROVISIONAL":
            confidence = row.get("confidence")
            if (
                not isinstance(row.get("pdf_locator"), dict)
                or not row["pdf_locator"]
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
                or not isinstance(row.get("provenance"), dict)
                or not row["provenance"]
            ):
                raise ChemicalCompletionError(
                    "HONEST_PROGRESSIVE_PROVISIONAL_PROVENANCE_REQUIRED"
                )
        if status == "BLOCKED" and (
            value is not None
            or not isinstance(row.get("gap_reason"), str)
            or not row["gap_reason"].strip()
        ):
            raise ChemicalCompletionError("HONEST_PROGRESSIVE_GAP_REQUIRED")
    if not allow_provisional and gate.get("ai_provisional_count", 0):
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_INCOMPLETE")
    if not allow_provisional and not gate["workflow_can_continue"]:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_INCOMPLETE")
    return str(gate["gate_digest"])


def require_chemical_completion_ready(project: Path, study_id: str) -> str:
    gate = chemical_completion_state(project, study_id)
    if not gate["workflow_can_continue"]:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_INCOMPLETE")
    return str(gate["gate_digest"])


def project_chemical_completion_state(project: Path) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        study_ids = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ChemicalCompletionError(exc.code) from exc
    aggregation_mode = _project_aggregation_mode(root, len(study_ids))
    if aggregation_mode is None and len(study_ids) > 1:
        raise ChemicalCompletionError("CHEMICAL_COMPLETION_PROJECT_MARKER_REQUIRED")
    core_study_ids, explicit_tiering = _core_study_scope(root, study_ids)
    rows = []
    for study_id in study_ids:
        try:
            state = chemical_completion_state(root, study_id)
            rows.append({
                **state,
                "status": (
                    "current" if state["workflow_can_continue"] else "blocked"
                ),
                "ai_authored_smiles_count": 0,
            })
        except ChemicalCompletionError as exc:
            rows.append({
                "study_id": study_id,
                "status": "blocked",
                "workflow_can_continue": False,
                "reason_code": exc.code,
                "missing_name_count": 0,
                "missing_resolved_smiles_count": 0,
                "ai_authored_smiles_count": 0,
            })
    core_rows = [
        row for row in rows
        if isinstance(row, dict) and row.get("study_id") in core_study_ids
    ]
    source = next(
        (row for row in core_rows if "coverage_ratio" in row),
        {},
    )
    confirmed_count = int(source.get("confirmed_count", 0))
    ai_provisional_count = int(source.get("ai_provisional_count", 0))
    blocked_count = int(source.get("blocked_count", 0))
    fixed_core_gate = (
        isinstance(source.get("compatibility_aggregation"), dict)
        and source["compatibility_aggregation"].get("mode") == "project_core_309"
    )
    if fixed_core_gate:
        covered_count = confirmed_count + ai_provisional_count
        coverage_ratio = covered_count / CORE_MOLECULE_COUNT
        coverage_denominator = CORE_MOLECULE_COUNT
        coverage_target_count = math.ceil(CORE_MOLECULE_COUNT * CORE_COVERAGE_THRESHOLD)
        ready = bool(core_rows) and coverage_ratio >= CORE_COVERAGE_THRESHOLD
        coverage_aggregation = (
            "Project-level fixed denominator of 309 core molecules; only core-tier studies contribute and study rows are slices."
        )
    else:
        covered_count = int(source.get("covered_count", 0))
        coverage_ratio = float(source.get("coverage_ratio", 0.0))
        coverage_denominator = int(source.get("coverage_denominator", 0))
        coverage_target_count = math.ceil(coverage_denominator * CORE_COVERAGE_THRESHOLD)
        ready = bool(core_rows) and all(
            bool(row.get("workflow_can_continue")) for row in core_rows
        )
        coverage_aggregation = (
            "Legacy subset compatibility: no complete 309-molecule core cohort is present."
        )
    compatibility_aggregation = source.get(
        "compatibility_aggregation",
        {
            "mode": "project_core_309" if fixed_core_gate else "legacy_subset",
            "source": coverage_aggregation,
        },
    )
    gap_registry = [
        {"study_id": row.get("study_id"), **gap}
        for row in core_rows
        if isinstance(row, dict)
        for gap in row.get("gap_registry", [])
        if isinstance(gap, dict)
    ]
    uncertainty_registry = [
        {"study_id": row.get("study_id"), **item}
        for row in core_rows
        if isinstance(row, dict)
        for item in row.get("uncertainty_registry", [])
        if isinstance(item, dict)
    ]
    return {
        "schema_version": "chemical-completion-project-state.v2",
        "route": "honest_progressive",
        "studies": rows,
        "workflow_can_continue": ready,
        "core_molecule_count": coverage_denominator,
        "confirmed_count": confirmed_count,
        "ai_provisional_count": ai_provisional_count,
        "blocked_count": blocked_count,
        "legacy_unclassified_count": source.get("legacy_unclassified_count", 0),
        "covered_count": covered_count,
        "coverage_ratio": coverage_ratio,
        "coverage_threshold": CORE_COVERAGE_THRESHOLD,
        "coverage_denominator": coverage_denominator,
        "coverage_target_count": coverage_target_count,
        "actor_provenance_residual": bool(source.get("actor_provenance_residual", False)),
        "project_molecule_count": sum(
            int(row.get("study_molecule_count", 0))
            for row in core_rows
            if isinstance(row, dict)
        ),
        "coverage_scope": "project_core_molecules",
        "coverage_aggregation": coverage_aggregation,
        "compatibility_aggregation": compatibility_aggregation,
        "gap_registry": gap_registry,
        "uncertainty_registry": uncertainty_registry,
        "uncertainty_disclosure": (
            "uncertainty_registry discloses classified molecules; "
            "gap_registry contains BLOCKED molecules only."
        ),
    }
