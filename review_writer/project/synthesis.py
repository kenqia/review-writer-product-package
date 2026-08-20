"""Fail-closed, hash-bound comparison and synthesis contracts."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .paper_evidence import (
    HONEST_PROGRESSIVE_ROUTE,
    PaperEvidenceError,
    _honest_progressive_rows,
    paper_evidence_state,
)
from .paper_evidence_store import PaperEvidenceStoreError, project_write_lock
from .chemical_completion import ChemicalCompletionError, require_honest_progressive_projection
from .source_truth import REPO_ROOT, SourceTruthError, canonical_digest, declared_study_ids, study_source_tier
from .verification_decision import VerificationDecisionError, verification_decision

SHA256 = re.compile(r"^[0-9a-f]{64}$")
PROTOCOL_PATH = Path("02_synthesis/comparison_protocol.json")
COVERAGE_PATH = Path("02_synthesis/coverage_map.json")
CLAIM_PATH = Path("02_synthesis/synthesis_claim_projection.jsonl")
EXACT_CHEMICAL_FIELD_DEPENDENCIES = frozenset({"molecule", "smiles", "molblock"})
FROZEN_REVIEW_QUESTIONS = (
    "主要键组合、反应模式及活化策略是什么？",
    "条件如何影响表现，哪些结果不可直接比较？",
    "底物范围、耐受性、选择性和局限是什么？",
    "机制证据处于什么层级，作者解释之间有哪些冲突？",
    "通用性、选择性、放大、资源效率和机制确定性还存在哪些缺口？",
)
FROZEN_REVIEW_QUESTIONS_DIGEST = canonical_digest(list(FROZEN_REVIEW_QUESTIONS))
FROZEN_REVIEW_QUESTION_IDS = tuple(f"RQ{index}" for index in range(1, 6))
FROZEN_REVIEW_QUESTION_DIGESTS = {
    question_id: canonical_digest(
        {"question_id": question_id, "question": question}
    )
    for question_id, question in zip(
        FROZEN_REVIEW_QUESTION_IDS, FROZEN_REVIEW_QUESTIONS
    )
}


class SynthesisError(ValueError):
    def __init__(self, code: str):
        super().__init__(code); self.code = code


def _normalize_authoritative_marker(value: dict[str, Any]) -> None:
    """Accept old callers while normalizing the one authoritative marker."""
    aliases = [key for key in ("authoritative", "run_mode") if key in value]
    if not aliases:
        return
    if "authoritative_run" in value:
        raise SynthesisError("AUTHORITATIVE_RUN_INVALID")
    if len(aliases) > 1:
        first, second = (value[key] for key in aliases)
        if first != second:
            raise SynthesisError("AUTHORITATIVE_RUN_INVALID")
    alias = aliases[0]
    value["authoritative_run"] = (
        value[alias] is True
        if alias == "authoritative"
        else value[alias] == "authoritative"
    )
    value.pop(alias, None)


def validate_authoritative_review_questions(value: object) -> dict[str, Any]:
    """Validate the frozen question contract without changing the input."""
    if not isinstance(value, dict):
        raise SynthesisError("REVIEW_QUESTIONS_INVALID")
    authoritative = value.get("authoritative_run", False)
    if not isinstance(authoritative, bool):
        raise SynthesisError("AUTHORITATIVE_RUN_INVALID")
    if not authoritative:
        return {"authoritative_run": False}
    questions = value.get("review_questions")
    if questions is None:
        raise SynthesisError("REVIEW_QUESTIONS_REQUIRED")
    if questions != list(FROZEN_REVIEW_QUESTIONS):
        raise SynthesisError("REVIEW_QUESTIONS_INVALID")
    digest = value.get("review_questions_digest")
    if digest is not None and digest != FROZEN_REVIEW_QUESTIONS_DIGEST:
        raise SynthesisError("REVIEW_QUESTIONS_STALE")
    if digest is None:
        raise SynthesisError("REVIEW_QUESTIONS_REQUIRED")
    return {
        "authoritative_run": True,
        "review_questions": list(FROZEN_REVIEW_QUESTIONS),
        "review_questions_digest": FROZEN_REVIEW_QUESTIONS_DIGEST,
    }


def _prepare_authoritative_questions(value: dict[str, Any]) -> dict[str, Any]:
    """Validate input questions before computing a protocol digest."""
    _normalize_authoritative_marker(value)
    value.setdefault("authoritative_run", False)
    if value["authoritative_run"] is not True:
        if not isinstance(value["authoritative_run"], bool):
            raise SynthesisError("AUTHORITATIVE_RUN_INVALID")
        return {"authoritative_run": False}
    questions = validate_authoritative_review_questions(
        {**value, "review_questions_digest": value.get("review_questions_digest")}
        if "review_questions_digest" in value
        else {**value, "review_questions_digest": FROZEN_REVIEW_QUESTIONS_DIGEST}
    )
    supplied_digest = value.get("review_questions_digest")
    if supplied_digest is not None and supplied_digest != FROZEN_REVIEW_QUESTIONS_DIGEST:
        raise SynthesisError("REVIEW_QUESTIONS_STALE")
    value.update(questions)
    return questions


def _protocol_authoritative(value: object) -> bool:
    return isinstance(value, dict) and value.get("authoritative_run") is True


def review_question_digest(question_id: object) -> str:
    """Return the frozen digest for one authoritative Review Question ID."""
    if not isinstance(question_id, str) or question_id not in FROZEN_REVIEW_QUESTION_DIGESTS:
        raise SynthesisError("SYNTHESIS_REVIEW_QUESTION_UNKNOWN")
    return FROZEN_REVIEW_QUESTION_DIGESTS[question_id]


def _root(project: Path) -> Path:
    p = Path(project)
    if p.is_symlink() or not p.is_dir(): raise SynthesisError("PROJECT_INVALID")
    return p.resolve(strict=True)


def _schema(name: str) -> dict[str, Any]:
    try: return json.loads((REPO_ROOT / "schemas/synthesis" / name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise SynthesisError("SYNTHESIS_SCHEMA_INVALID") from exc


def _validate(value: object, name: str, code: str = "SYNTHESIS_INVALID") -> None:
    errors = list(Draft202012Validator(_schema(name)).iter_errors(value))
    if errors: raise SynthesisError(code)


def _write(project: Path, rel: Path, value: object) -> None:
    path = project / rel; path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()): raise SynthesisError("SYNTHESIS_PATH_INVALID")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _write_raw(project: Path, rel: Path, text: str) -> None:
    path = project / rel; path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _write_jsonl(project: Path, rel: Path, rows: list[dict[str, Any]]) -> None:
    _write_raw(project, rel, "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows) + ("\n" if rows else ""))


def _read_json(project: Path, rel: Path) -> Any:
    path = project / rel
    if not path.is_file() or path.is_symlink(): return None
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError): raise SynthesisError("SYNTHESIS_INVALID")


def _read_jsonl(project: Path, rel: Path) -> list[dict[str, Any]]:
    path = project / rel
    if not path.is_file() or path.is_symlink(): return []
    try: rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise SynthesisError("SYNTHESIS_INVALID") from exc
    if not all(isinstance(row, dict) for row in rows): raise SynthesisError("SYNTHESIS_INVALID")
    return rows


def _decision(payload: dict[str, Any], digest: str) -> dict[str, Any]:
    action = payload.get("action", "approve")
    reason = payload.get("reason")
    if action not in {"approve", "revise_and_approve", "reject"} or not isinstance(reason, str) or not reason.strip(): raise SynthesisError("SYNTHESIS_DECISION_INVALID")
    try: return verification_decision(actor_type=payload.get("actor_type", "human_researcher"), actor_label=payload.get("actor_label", "local-researcher"), action=action, reason=reason, bound_object_digest=digest)
    except VerificationDecisionError as exc: raise SynthesisError("SYNTHESIS_DECISION_INVALID") from exc


def _valid_decision(value: object, digest: str) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("action") not in {"approve", "revise_and_approve", "reject"}:
        return False
    try:
        normalized = verification_decision(
            actor_type=value.get("actor_type"),
            actor_label=value.get("actor_label"),
            action=value.get("action"),
            reason=value.get("reason"),
            bound_object_digest=value.get("bound_object_digest"),
            bound_gate_digest=value.get("bound_gate_digest"),
            decided_at=value.get("decided_at"),
        )
    except (VerificationDecisionError, TypeError, AttributeError):
        return False
    return normalized == value and value.get("bound_object_digest") == digest


def _unsigned(value: dict[str, Any], digest_key: str) -> dict[str, Any]:
    result = {
        k: v
        for k, v in value.items()
        if k not in {digest_key, "status", "reason_code", "current", "disposition"}
    }
    result["decision"] = None
    return result


def _review_question_lineage_digest(row: dict[str, Any]) -> str:
    return canonical_digest(
        {
            "synthesis_id": row.get("synthesis_id"),
            "review_question_id": row.get("review_question_id"),
            "review_question_digest": row.get("review_question_digest"),
            "review_questions_digest": row.get("review_questions_digest"),
            "comparison_protocol_digest": row.get("comparison_protocol_digest"),
            "paper_evidence_projection_digest": row.get(
                "paper_evidence_projection_digest"
            ),
        }
    )


def _question_gate_result(
    reason_code: str,
    *,
    missing_question_ids: list[str] | None = None,
    duplicate_question_ids: list[str] | None = None,
    unknown_question_ids: list[str] | None = None,
    stale_question_ids: list[str] | None = None,
    digest_mismatch_question_ids: list[str] | None = None,
    lineage_mismatch_question_ids: list[str] | None = None,
    artifact_digest_mismatch_question_ids: list[str] | None = None,
    undispositioned_question_ids: list[str] | None = None,
    current_question_ids: list[str] | None = None,
    row_reason_codes: dict[str, str] | None = None,
) -> dict[str, Any]:
    current = sorted(current_question_ids or [])
    return {
        "status": "approved" if reason_code == "SYNTHESIS_REVIEW_QUESTIONS_APPROVED" else "needs_review",
        "workflow_can_continue": reason_code == "SYNTHESIS_REVIEW_QUESTIONS_APPROVED",
        "reason_code": reason_code,
        "required_question_count": len(FROZEN_REVIEW_QUESTION_IDS),
        "current_question_count": len(current),
        "expected_question_ids": list(FROZEN_REVIEW_QUESTION_IDS),
        "current_question_ids": current,
        "missing_question_ids": sorted(missing_question_ids or []),
        "duplicate_question_ids": sorted(duplicate_question_ids or []),
        "unknown_question_ids": sorted(unknown_question_ids or []),
        "stale_question_ids": sorted(stale_question_ids or []),
        "digest_mismatch_question_ids": sorted(digest_mismatch_question_ids or []),
        "lineage_mismatch_question_ids": sorted(lineage_mismatch_question_ids or []),
        "artifact_digest_mismatch_question_ids": sorted(
            artifact_digest_mismatch_question_ids or []
        ),
        "undispositioned_question_ids": sorted(undispositioned_question_ids or []),
        "row_reason_codes": dict(row_reason_codes or {}),
    }


def validate_authoritative_synthesis_artifacts(
    rows: object,
    *,
    protocol_digest: object,
    review_questions: object,
    review_questions_digest: object,
    evidence_projection_digest: object,
) -> dict[str, Any]:
    """Fail closed unless exactly one current, dispositioned artifact covers each RQ1-RQ5."""
    try:
        binding = validate_authoritative_review_questions(
            {
                "authoritative_run": True,
                "review_questions": review_questions,
                "review_questions_digest": review_questions_digest,
            }
        )
    except SynthesisError as exc:
        return _question_gate_result(exc.code)

    if not isinstance(rows, list):
        return _question_gate_result("SYNTHESIS_REVIEW_QUESTION_MISSING")

    by_question: dict[str, list[dict[str, Any]]] = {
        question_id: [] for question_id in FROZEN_REVIEW_QUESTION_IDS
    }
    unknown_question_ids: list[str] = []
    missing_id_rows: list[str] = []
    for raw in rows:
        if not isinstance(raw, dict):
            missing_id_rows.append("<invalid-artifact>")
            continue
        question_id = raw.get("review_question_id")
        if not isinstance(question_id, str) or not question_id.strip():
            missing_id_rows.append(str(raw.get("synthesis_id", "<missing-artifact-id>")))
        elif question_id not in FROZEN_REVIEW_QUESTION_IDS:
            unknown_question_ids.append(question_id)
        else:
            by_question[question_id].append(raw)

    missing_question_ids = [
        question_id
        for question_id, matches in by_question.items()
        if not matches
    ]
    duplicate_question_ids = [
        question_id
        for question_id, matches in by_question.items()
        if len(matches) > 1
    ]
    if missing_id_rows:
        missing_question_ids = [*missing_question_ids, "<missing-question-id>"]

    row_reason_codes: dict[str, str] = {}
    stale_question_ids: list[str] = []
    digest_mismatch_question_ids: list[str] = []
    lineage_mismatch_question_ids: list[str] = []
    artifact_digest_mismatch_question_ids: list[str] = []
    undispositioned_question_ids: list[str] = []
    current_question_ids: list[str] = []

    for question_id, matches in by_question.items():
        if len(matches) != 1:
            continue
        row = matches[0]
        synthesis_id = row.get("synthesis_id")
        row_key = synthesis_id if isinstance(synthesis_id, str) else question_id
        reason_code: str | None = None
        if row.get("authoritative_run") is not True:
            reason_code = "SYNTHESIS_REVIEW_QUESTION_STALE"
            stale_question_ids.append(question_id)
        elif row.get("review_questions_digest") != binding["review_questions_digest"]:
            reason_code = "SYNTHESIS_REVIEW_QUESTION_STALE"
            stale_question_ids.append(question_id)
        elif (
            row.get("comparison_protocol_digest") != protocol_digest
            or row.get("paper_evidence_projection_digest") != evidence_projection_digest
        ):
            reason_code = "SYNTHESIS_REVIEW_QUESTION_STALE"
            stale_question_ids.append(question_id)
        elif row.get("review_question_digest") != review_question_digest(question_id):
            reason_code = "SYNTHESIS_REVIEW_QUESTION_DIGEST_MISMATCH"
            digest_mismatch_question_ids.append(question_id)
        elif row.get("review_question_lineage_digest") != _review_question_lineage_digest(row):
            reason_code = "SYNTHESIS_REVIEW_QUESTION_LINEAGE_MISMATCH"
            lineage_mismatch_question_ids.append(question_id)
        elif row.get("synthesis_digest") != canonical_digest(_unsigned(row, "synthesis_digest")):
            reason_code = "SYNTHESIS_REVIEW_QUESTION_ARTIFACT_DIGEST_MISMATCH"
            artifact_digest_mismatch_question_ids.append(question_id)
        elif not _valid_decision(row.get("decision"), row.get("synthesis_digest")):
            reason_code = "SYNTHESIS_REVIEW_QUESTION_DISPOSITION_REQUIRED"
            undispositioned_question_ids.append(question_id)
        else:
            current_question_ids.append(question_id)
        if reason_code is not None:
            row_reason_codes[row_key] = reason_code

    details = {
        "missing_question_ids": missing_question_ids,
        "duplicate_question_ids": duplicate_question_ids,
        "unknown_question_ids": unknown_question_ids,
        "stale_question_ids": stale_question_ids,
        "digest_mismatch_question_ids": digest_mismatch_question_ids,
        "lineage_mismatch_question_ids": lineage_mismatch_question_ids,
        "artifact_digest_mismatch_question_ids": artifact_digest_mismatch_question_ids,
        "undispositioned_question_ids": undispositioned_question_ids,
        "current_question_ids": current_question_ids,
        "row_reason_codes": row_reason_codes,
    }
    if missing_question_ids:
        return _question_gate_result("SYNTHESIS_REVIEW_QUESTION_MISSING", **details)
    if unknown_question_ids:
        return _question_gate_result("SYNTHESIS_REVIEW_QUESTION_UNKNOWN", **details)
    if duplicate_question_ids:
        return _question_gate_result("SYNTHESIS_REVIEW_QUESTION_DUPLICATE", **details)
    if stale_question_ids:
        return _question_gate_result("SYNTHESIS_REVIEW_QUESTION_STALE", **details)
    if digest_mismatch_question_ids:
        return _question_gate_result(
            "SYNTHESIS_REVIEW_QUESTION_DIGEST_MISMATCH", **details
        )
    if lineage_mismatch_question_ids:
        return _question_gate_result(
            "SYNTHESIS_REVIEW_QUESTION_LINEAGE_MISMATCH", **details
        )
    if artifact_digest_mismatch_question_ids:
        return _question_gate_result(
            "SYNTHESIS_REVIEW_QUESTION_ARTIFACT_DIGEST_MISMATCH", **details
        )
    if undispositioned_question_ids:
        return _question_gate_result(
            "SYNTHESIS_REVIEW_QUESTION_DISPOSITION_REQUIRED", **details
        )
    return _question_gate_result("SYNTHESIS_REVIEW_QUESTIONS_APPROVED", **details)


def authoritative_synthesis_question_bindings(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Project the five current question artifacts into manuscript lineage."""
    gate = state.get("question_gate") if isinstance(state, dict) else None
    if not isinstance(gate, dict) or gate.get("workflow_can_continue") is not True:
        reason_code = (
            gate.get("reason_code")
            if isinstance(gate, dict)
            else "SYNTHESIS_REVIEW_QUESTION_MISSING"
        )
        raise SynthesisError(str(reason_code))
    rows = state.get("rows")
    if not isinstance(rows, list):
        raise SynthesisError("SYNTHESIS_REVIEW_QUESTION_MISSING")
    by_id = {
        row.get("review_question_id"): row
        for row in rows
        if isinstance(row, dict)
        and row.get("current") is True
        and isinstance(row.get("review_question_id"), str)
    }
    if set(by_id) != set(FROZEN_REVIEW_QUESTION_IDS):
        raise SynthesisError("SYNTHESIS_REVIEW_QUESTION_MISSING")
    return [
        {
            "review_question_id": question_id,
            "review_question_digest": by_id[question_id]["review_question_digest"],
            "review_questions_digest": by_id[question_id]["review_questions_digest"],
            "synthesis_id": by_id[question_id]["synthesis_id"],
            "synthesis_digest": by_id[question_id]["synthesis_digest"],
            "review_question_lineage_digest": by_id[question_id][
                "review_question_lineage_digest"
            ],
            "current": True,
            "disposition": by_id[question_id]["disposition"],
        }
        for question_id in FROZEN_REVIEW_QUESTION_IDS
    ]


def register_comparison_protocol(project: Path, payload: object) -> dict[str, Any]:
    project = _root(project)
    if not isinstance(payload, dict): raise SynthesisError("COMPARISON_PROTOCOL_INVALID")
    value = copy.deepcopy(payload); value.setdefault("schema_version", "comparison-protocol.v1")
    value.setdefault("decision", None)
    _prepare_authoritative_questions(value)
    required = {"comparison_id", "comparison_objects", "axes", "normalization_rules", "missing_value_policy", "incomparability_rules", "counterevidence_rules", "claim_strength"}
    if not required.issubset(value): raise SynthesisError("COMPARISON_PROTOCOL_INVALID")
    comparison_objects = value.get("comparison_objects")
    if (
        not isinstance(comparison_objects, list)
        or any(not isinstance(item, str) for item in comparison_objects)
        or len(comparison_objects) != len(set(comparison_objects))
    ):
        raise SynthesisError(
            "COMPARISON_PROTOCOL_DUPLICATE"
            if isinstance(comparison_objects, list)
            and all(isinstance(item, str) for item in comparison_objects)
            and len(comparison_objects) != len(set(comparison_objects))
            else "COMPARISON_PROTOCOL_INVALID"
        )
    try:
        evidence = paper_evidence_state(project)
    except PaperEvidenceError as exc:
        raise SynthesisError("PAPER_EVIDENCE_NOT_READY") from exc
    current_evidence_digest = evidence.get("projection_digest")
    supplied_evidence_digest = value.get("paper_evidence_projection_digest")
    if (
        supplied_evidence_digest is not None
        and supplied_evidence_digest != current_evidence_digest
    ):
        raise SynthesisError("COMPARISON_PROTOCOL_STALE")
    value["paper_evidence_projection_digest"] = current_evidence_digest
    value["protocol_digest"] = canonical_digest(_unsigned(value, "protocol_digest"))
    _validate(value, "comparison_protocol.v1.schema.json", "COMPARISON_PROTOCOL_INVALID")
    with project_write_lock(project): _write(project, PROTOCOL_PATH, value)
    return value


def apply_comparison_protocol_decision(project: Path, payload: object) -> dict[str, Any]:
    project = _root(project); protocol = _read_json(project, PROTOCOL_PATH)
    if not isinstance(protocol, dict): raise SynthesisError("COMPARISON_PROTOCOL_NOT_FOUND")
    if not isinstance(payload, dict): raise SynthesisError("COMPARISON_PROTOCOL_DECISION_INVALID")
    digest = protocol.get("protocol_digest")
    if not isinstance(digest, str): raise SynthesisError("COMPARISON_PROTOCOL_INVALID")
    protocol["decision"] = _decision(payload, digest)
    with project_write_lock(project): _write(project, PROTOCOL_PATH, protocol)
    return protocol


def comparison_protocol_state(project: Path) -> dict[str, Any]:
    value = _read_json(_root(project), PROTOCOL_PATH)
    if not isinstance(value, dict): return {"status": "needs_review", "workflow_can_continue": False, "reason_code": "COMPARISON_PROTOCOL_NOT_APPROVED"}
    try:
        questions = validate_authoritative_review_questions(value) if _protocol_authoritative(value) else {"authoritative_run": False}
        _validate(value, "comparison_protocol.v1.schema.json", "COMPARISON_PROTOCOL_INVALID")
    except SynthesisError as exc: return {"status": "needs_review", "workflow_can_continue": False, "reason_code": exc.code}
    digest = value.get("protocol_digest")
    unsigned = _unsigned(value, "protocol_digest")
    if not isinstance(digest, str) or digest != canonical_digest(unsigned):
        return {"status": "needs_review", "workflow_can_continue": False, "reason_code": "COMPARISON_PROTOCOL_STALE"}
    decision = value.get("decision") or {}
    ok = _valid_decision(decision, digest) and decision.get("action") == "approve" and value.get("paper_evidence_projection_digest") == paper_evidence_state(_root(project)).get("projection_digest")
    return {
        "status": "approved" if ok else "needs_review",
        "workflow_can_continue": ok,
        "reason_code": "COMPARISON_PROTOCOL_APPROVED" if ok else "COMPARISON_PROTOCOL_NOT_APPROVED",
        "protocol_digest": value.get("protocol_digest"),
        "authoritative_run": questions.get("authoritative_run", False),
        "review_questions": questions.get("review_questions"),
        "review_questions_digest": questions.get("review_questions_digest"),
        "value": value,
    }


def register_coverage_map(project: Path, payload: object) -> dict[str, Any]:
    project = _root(project)
    if not isinstance(payload, dict): raise SynthesisError("COVERAGE_MAP_INVALID")
    protocol = comparison_protocol_state(project)
    if not protocol.get("workflow_can_continue"): raise SynthesisError("COMPARISON_PROTOCOL_NOT_APPROVED")
    value = copy.deepcopy(payload); value.setdefault("schema_version", "coverage-map.v1"); value.setdefault("corpus_kind", "calibration_corpus"); value.setdefault("known_omissions", [])
    value["comparison_protocol_digest"] = protocol.get("protocol_digest")
    _validate(value, "coverage_map.v1.schema.json", "COVERAGE_MAP_INVALID")
    with project_write_lock(project): _write(project, COVERAGE_PATH, value)
    return value


def coverage_map_state(project: Path) -> dict[str, Any]:
    project = _root(project); value = _read_json(project, COVERAGE_PATH); protocol = comparison_protocol_state(project)
    if not isinstance(value, dict):
        return {"status": "needs_review", "workflow_can_continue": False, "reason_code": "COVERAGE_MAP_MISSING"}
    try: _validate(value, "coverage_map.v1.schema.json", "COVERAGE_MAP_INVALID")
    except SynthesisError as exc:
        return {"status": "needs_review", "workflow_can_continue": False, "reason_code": exc.code}
    ok = (
        protocol.get("workflow_can_continue")
        and value.get("comparison_protocol_digest") == protocol.get("protocol_digest")
        and value.get("comparison_id") == (protocol.get("value") or {}).get("comparison_id")
    )
    return {"status": "approved" if ok else "needs_review", "workflow_can_continue": bool(ok), "reason_code": "COVERAGE_MAP_APPROVED" if ok else "COVERAGE_MAP_STALE", "value": value}


def _approved_evidence(project: Path) -> dict[str, dict[str, Any]]:
    state = paper_evidence_state(project)
    return {
        row["evidence_id"]: row
        for row in state.get("rows", [])
        if row.get("status") in {"approved", "CONFIRMED"}
    }


def _candidate_requires_exact_chemical_coverage(
    candidate: dict[str, Any], evidence: dict[str, dict[str, Any]]
) -> bool:
    supporting = candidate.get("supporting_evidence_ids", [])
    if not isinstance(supporting, list):
        return False
    return any(
        EXACT_CHEMICAL_FIELD_DEPENDENCIES.intersection(
            evidence.get(evidence_id, {}).get("field_dependencies", [])
        )
        for evidence_id in supporting
    )


def _require_exact_chemical_coverage(
    project: Path,
    candidates: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> None:
    """Keep exact synthesis candidate registration behind the 80% gate."""

    if not any(
        _candidate_requires_exact_chemical_coverage(candidate, evidence)
        for candidate in candidates
    ):
        return

    if not (project / "01_evidence/dual_source").is_dir():
        return
    try:
        studies = declared_study_ids(project)
        core_studies = [
            study_id
            for study_id in studies
            if study_source_tier(project, study_id) == "core"
        ]
    except SourceTruthError as exc:
        raise SynthesisError(exc.code) from exc
    try:
        for study_id in core_studies:
            require_honest_progressive_projection(
                project, study_id, allow_provisional=False
            )
    except ChemicalCompletionError as exc:
        raise SynthesisError(exc.code) from exc


def partition_honest_progressive_evidence(rows: object) -> dict[str, Any]:
    """Partition evidence by the only downstream uses allowed by its state."""

    normalized = _honest_progressive_rows(rows)
    exact: list[dict[str, Any]] = []
    internal: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    traceability: list[dict[str, Any]] = []
    for raw in normalized:
        row = copy.deepcopy(raw)
        row.pop("traceability_ready", None)
        row.pop("provisional", None)
        status = row.get("status")
        if status == "BLOCKED":
            row["value"] = None
            limitations.append(row)
        elif status in {"CONFIRMED", "AI_PROVISIONAL"}:
            row["provisional"] = status == "AI_PROVISIONAL"
            internal.append(row)
            if status == "CONFIRMED":
                exact.append(copy.deepcopy(row))
        traceability.append(
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
        )
    return {
        "route": HONEST_PROGRESSIVE_ROUTE,
        "exact_conclusions": exact,
        "internal_comparison": internal,
        "limitation_disclosures": limitations,
        "traceability": traceability,
    }


def register_synthesis_candidates(project: Path, payload: object) -> dict[str, Any]:
    project = _root(project); protocol = comparison_protocol_state(project)
    if not protocol.get("workflow_can_continue"): raise SynthesisError("COMPARISON_PROTOCOL_NOT_APPROVED")
    authoritative = protocol.get("authoritative_run") is True
    if authoritative:
        validate_authoritative_review_questions(protocol.get("value") or protocol)
    if not isinstance(payload, dict): raise SynthesisError("SYNTHESIS_INVALID")
    raw = payload.get("claims", [payload])
    if not isinstance(raw, list): raise SynthesisError("SYNTHESIS_INVALID")
    evidence = _approved_evidence(project)
    _require_exact_chemical_coverage(project, raw, evidence)
    current_digest = paper_evidence_state(project).get("projection_digest")
    rows = _read_jsonl(project, CLAIM_PATH); existing = {r.get("synthesis_id"): r for r in rows}
    out = []
    for candidate in raw:
        if not isinstance(candidate, dict): raise SynthesisError("SYNTHESIS_INVALID")
        row = copy.deepcopy(candidate)
        if row.get("decision") is not None:
            raise SynthesisError("SYNTHESIS_DECISION_INVALID")
        row.setdefault("schema_version", "synthesis-claim.v1"); row.setdefault("decision", None); row.setdefault("counter_evidence_ids", []); row.setdefault("single_study", False); row.setdefault("paper_evidence_projection_digest", current_digest); row.setdefault("comparison_protocol_digest", protocol.get("protocol_digest"))
        if authoritative:
            row["authoritative_run"] = True
            question_id = row.get("review_question_id")
            if not isinstance(question_id, str) or not question_id.strip():
                raise SynthesisError("SYNTHESIS_REVIEW_QUESTION_REQUIRED")
            expected_question_digest = review_question_digest(question_id)
            supplied_question_digest = row.get("review_question_digest")
            if supplied_question_digest is not None and supplied_question_digest != expected_question_digest:
                raise SynthesisError("SYNTHESIS_REVIEW_QUESTION_DIGEST_MISMATCH")
            row["review_question_digest"] = expected_question_digest
            expected_questions_digest = protocol.get("review_questions_digest")
            supplied_questions_digest = row.get("review_questions_digest")
            if supplied_questions_digest is not None and supplied_questions_digest != expected_questions_digest:
                raise SynthesisError("SYNTHESIS_REVIEW_QUESTION_STALE")
            row["review_questions_digest"] = expected_questions_digest
            expected_lineage_digest = _review_question_lineage_digest(row)
            supplied_lineage_digest = row.get("review_question_lineage_digest")
            if supplied_lineage_digest is not None and supplied_lineage_digest != expected_lineage_digest:
                raise SynthesisError("SYNTHESIS_REVIEW_QUESTION_LINEAGE_MISMATCH")
            row["review_question_lineage_digest"] = expected_lineage_digest
        supports = row.get("supporting_evidence_ids", []); counters = row.get("counter_evidence_ids", [])
        if not isinstance(supports, list) or not supports or any(eid not in evidence for eid in supports + (counters if isinstance(counters, list) else [])): raise SynthesisError("SYNTHESIS_EVIDENCE_NOT_APPROVED")
        studies = {evidence[eid]["study_id"] for eid in supports}
        if not row.get("single_study") and len(studies) < 2: raise SynthesisError("MULTI_STUDY_SUPPORT_REQUIRED")
        if row.get("single_study") and re.search(r"\b(field|generally|consensus|universal|all)\b", str(row.get("proposition", "")), re.I): raise SynthesisError("SINGLE_STUDY_OVERGENERALIZATION")
        row["synthesis_digest"] = canonical_digest(_unsigned(row, "synthesis_digest"))
        _validate(row, "synthesis_claim.v1.schema.json")
        prior = existing.get(row.get("synthesis_id"))
        if prior is not None and prior != row: raise SynthesisError("SYNTHESIS_ID_CONFLICT")
        existing[row["synthesis_id"]] = row; out.append(row)
    merged = sorted(existing.values(), key=lambda r: r.get("synthesis_id", ""))
    with project_write_lock(project): _write_jsonl(project, CLAIM_PATH, merged)
    return {"claims": out, "status": "needs_review"}


def apply_synthesis_decision(project: Path, payload: object) -> dict[str, Any]:
    project = _root(project)
    if not isinstance(payload, dict): raise SynthesisError("SYNTHESIS_DECISION_INVALID")
    rows = _read_jsonl(project, CLAIM_PATH); sid = payload.get("synthesis_id")
    row = next((r for r in rows if r.get("synthesis_id") == sid), None)
    if row is None: raise SynthesisError("SYNTHESIS_ID_NOT_FOUND")
    if row.get("paper_evidence_projection_digest") != paper_evidence_state(project).get("projection_digest"): raise SynthesisError("SYNTHESIS_STALE")
    row["decision"] = _decision(payload, row["synthesis_digest"]); row["status"] = "approved" if row["decision"]["action"] != "reject" else "rejected"
    with project_write_lock(project): _write_jsonl(project, CLAIM_PATH, rows)
    return row


def synthesis_state(project: Path) -> dict[str, Any]:
    project = _root(project); protocol = comparison_protocol_state(project); evidence = paper_evidence_state(project)
    rows = _read_jsonl(project, CLAIM_PATH); current = evidence.get("projection_digest")
    current_protocol = protocol.get("protocol_digest")
    authoritative = protocol.get("authoritative_run") is True
    projected = []
    for row in rows:
        row = copy.deepcopy(row)
        if authoritative:
            row["current"] = False
            row["disposition"] = None
        if row.get("comparison_protocol_digest") != protocol.get("protocol_digest"): row.update(status="stale", reason_code="SYNTHESIS_PROTOCOL_STALE")
        elif row.get("synthesis_digest") != canonical_digest(_unsigned(row, "synthesis_digest")): row.update(status="stale", reason_code="SYNTHESIS_DIGEST_INVALID")
        elif row.get("paper_evidence_projection_digest") != current: row.update(status="stale", reason_code="SYNTHESIS_STALE")
        elif row.get("comparison_protocol_digest") != current_protocol: row.update(status="stale", reason_code="SYNTHESIS_PROTOCOL_STALE")
        elif not row.get("decision"): row.update(status="needs_review", reason_code="SYNTHESIS_REVIEW_REQUIRED")
        elif not _valid_decision(row["decision"], row["synthesis_digest"]): row.update(status="stale", reason_code="SYNTHESIS_DECISION_INVALID")
        elif row["decision"].get("action") == "reject": row.update(status="rejected", reason_code="SYNTHESIS_REJECTED")
        else:
            row.update(
                status="approved",
                reason_code="SYNTHESIS_APPROVED",
            )
            if authoritative:
                row.update(
                    current=True,
                    disposition=row["decision"].get("action"),
                )
        projected.append(row)
        if authoritative and row.get("status") == "rejected":
            row["current"] = True
            row["disposition"] = row["decision"].get("action")
    question_gate = _question_gate_result("SYNTHESIS_REVIEW_QUESTIONS_NOT_REQUIRED")
    if authoritative:
        question_gate = validate_authoritative_synthesis_artifacts(
            rows,
            protocol_digest=protocol.get("protocol_digest"),
            review_questions=protocol.get("review_questions"),
            review_questions_digest=protocol.get("review_questions_digest"),
            evidence_projection_digest=current,
        )
        row_reason_codes = question_gate.get("row_reason_codes", {})
        current_question_ids = set(question_gate.get("current_question_ids", []))
        for row in projected:
            question_id = row.get("review_question_id")
            if not isinstance(question_id, str) or question_id not in current_question_ids:
                row["current"] = False
                reason_code = row_reason_codes.get(row.get("synthesis_id"))
                if reason_code is not None:
                    row.update(status="stale", reason_code=reason_code)
    ready = bool(projected) and protocol.get("workflow_can_continue") and all(r.get("status") in {"approved", "rejected"} for r in projected) and any(r.get("status") == "approved" for r in projected)
    if authoritative:
        ready = bool(ready and question_gate.get("workflow_can_continue") is True)
    result = {
        "status": "approved" if ready else "needs_review",
        "workflow_can_continue": ready,
        "reason_code": "SYNTHESIS_APPROVED" if ready else (
            question_gate.get("reason_code")
            if authoritative and question_gate.get("workflow_can_continue") is not True
            else "SYNTHESIS_NOT_APPROVED"
        ),
        "projection_digest": canonical_digest(projected),
        "rows": projected,
    }
    if authoritative:
        result.update(
            {
                "authoritative_run": True,
                "review_questions": protocol.get("review_questions"),
                "review_questions_digest": protocol.get("review_questions_digest"),
                "question_gate": question_gate,
            }
        )
    return result


def require_synthesis_ready(project: Path) -> str:
    state = synthesis_state(project)
    if not state["workflow_can_continue"]:
        raise SynthesisError(state["reason_code"])
    return str(state["projection_digest"])
