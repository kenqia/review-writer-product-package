"""Authoritative, offline Source-to-Manuscript review projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from review_writer.product_foundation import ProductFoundationError, VersionContext


PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RISK_LEVELS = frozenset({"R0", "R1", "R2", "R3"})
REVIEWER_VERDICTS = frozenset({"SUPPORT", "REJECT", "AMBIGUOUS"})
AWAITING_BRIEF_CONFIRMATION = "AWAITING_BRIEF_CONFIRMATION"
BRIEF_CONFIRMED = "BRIEF_CONFIRMED"
_REVIEW_STATE_PATH = Path("00_brief/review_state.json")
_INITIALIZATION_OBJECTS = (
    (Path("01_evidence/evidence_cards.jsonl"), "jsonl", []),
    (Path("01_evidence/exception_queue.json"), "json", {"exceptions": []}),
    (Path("02_claims/claim_projection.jsonl"), "jsonl", []),
    (Path("03_review/risk_decisions.json"), "json", {"decisions": []}),
)
_INITIALIZATION_PATHS = frozenset(
    {_REVIEW_STATE_PATH, *(relative for relative, _, _ in _INITIALIZATION_OBJECTS)}
)
_AUTHORITATIVE_PATHS = frozenset(
    {
        _REVIEW_STATE_PATH,
        Path("01_evidence/evidence_cards.jsonl"),
        Path("01_evidence/exception_queue.json"),
        Path("02_claims/claim_projection.jsonl"),
        Path("02_claims/writer_packet.json"),
        Path("03_review/risk_packet.json"),
        Path("03_review/risk_decisions.json"),
    }
)
HIGH_RISK_CATEGORIES = frozenset(
    {
        "CROSS_STUDY_COMPARISON",
        "FIGURE_TABLE_CHEMISTRY",
        "MATERIAL_ASSERTION",
        "MATERIAL_COMPARISON",
        "MECHANISM_CAUSALITY",
        "NEGATIVE_GENERALIZATION",
        "NON_PEER_REVIEWED",
        "SOURCE_CONFLICT",
        "STEREOCHEMISTRY",
        "STRUCTURE",
    }
)


class VerticalReviewError(ValueError):
    """The review projection cannot safely accept the requested state change."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, message: str) -> None:
    raise VerticalReviewError(code, message)


def _json_copy(value: Any, code: str) -> Any:
    try:
        detached = copy.deepcopy(value)
        encoded = json.dumps(
            detached,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        _fail(code, "value must be finite JSON data")


def _json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VerticalReviewError("JSON_INVALID", "value must be finite JSON data") from exc


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VerticalReviewError("JSON_INVALID", "value must be finite JSON data") from exc


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    try:
        lines = [
            json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in rows
        ]
    except (TypeError, ValueError) as exc:
        raise VerticalReviewError("JSONL_INVALID", "row must be finite JSON data") from exc
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    _atomic_write(path, _jsonl_bytes(rows))


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerticalReviewError(code, "required JSON state is missing or invalid") from exc


def _read_jsonl(path: Path, code: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise VerticalReviewError(code, "required JSONL state is missing or invalid") from exc
    if not all(isinstance(row, dict) for row in rows):
        _fail(code, "JSONL state must contain objects")
    return rows


def _validate_project_path_boundary(project: Path, *, allow_missing: bool) -> None:
    if project.is_symlink():
        _fail("PROJECT_PATH_INVALID", "project root must not be a symlink")
    if not project.exists():
        if allow_missing:
            return
        _fail("PROJECT_PATH_INVALID", "project root is missing")
    if not project.is_dir():
        _fail("PROJECT_PATH_INVALID", "project root must be a directory")
    for relative in _AUTHORITATIVE_PATHS:
        component = project
        for part in relative.parts:
            component /= part
            if component.is_symlink():
                _fail("PROJECT_PATH_INVALID", "authoritative path contains a symlink")
            if not component.exists():
                break


def _project_state(project: Path) -> dict[str, Any]:
    _validate_project_path_boundary(project, allow_missing=False)
    state = _read_json(project / _REVIEW_STATE_PATH, "PROJECT_STATE_INVALID")
    if not isinstance(state, dict) or not isinstance(state.get("project_id"), str):
        _fail("PROJECT_STATE_INVALID", "review state does not identify a project")
    return state


def _validate_brief(brief: Any) -> dict[str, Any]:
    brief_copy = _json_copy(brief, "BRIEF_INVALID")
    if not isinstance(brief_copy, dict):
        _fail("BRIEF_INVALID", "brief must be a JSON object")

    string_fields = (
        "topic",
        "review_question",
        "output_language",
        "audience",
        "scope",
        "review_status",
    )
    for field in string_fields:
        if field == "topic" or field in brief_copy:
            value = brief_copy.get(field)
            if not isinstance(value, str) or not value.strip():
                _fail("BRIEF_INVALID", f"{field} must be a nonempty string")

    has_from_year = "from_year" in brief_copy
    has_to_year = "to_year" in brief_copy
    if has_from_year != has_to_year:
        _fail("BRIEF_INVALID", "from_year and to_year must be provided together")
    if has_from_year:
        from_year = brief_copy["from_year"]
        to_year = brief_copy["to_year"]
        if (
            not isinstance(from_year, int)
            or isinstance(from_year, bool)
            or not 1 <= from_year <= 9999
            or not isinstance(to_year, int)
            or isinstance(to_year, bool)
            or not 1 <= to_year <= 9999
            or from_year > to_year
        ):
            _fail("BRIEF_INVALID", "year range must contain ordered years from 1 to 9999")

    target = brief_copy.get("target_primary_studies")
    if "target_primary_studies" in brief_copy and (
        not isinstance(target, int) or isinstance(target, bool) or target <= 0
    ):
        _fail("BRIEF_INVALID", "target_primary_studies must be a positive integer")

    acceptable_range = brief_copy.get("acceptable_core_range")
    if "acceptable_core_range" in brief_copy:
        if not isinstance(acceptable_range, list) or len(acceptable_range) != 2:
            _fail("BRIEF_INVALID", "acceptable_core_range must contain two integers")
        low, high = acceptable_range
        if (
            not isinstance(low, int)
            or isinstance(low, bool)
            or low <= 0
            or not isinstance(high, int)
            or isinstance(high, bool)
            or high <= 0
            or low > high
        ):
            _fail("BRIEF_INVALID", "acceptable_core_range must be positive and ordered")
        if target is not None and not low <= target <= high:
            _fail("BRIEF_INVALID", "target_primary_studies must be within the acceptable range")

    for field in ("required_modes", "exclusions", "deliverables"):
        if field not in brief_copy:
            continue
        values = brief_copy[field]
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or len(values) != len(set(values))
        ):
            _fail("BRIEF_INVALID", f"{field} must contain unique nonempty strings")

    return brief_copy


def initialize_review(review_root: Path, project_id: str, brief: dict) -> Path:
    """Create one deterministic review project using only authorized objects."""
    root = Path(review_root)
    if (
        not isinstance(project_id, str)
        or project_id in {".", ".."}
        or PROJECT_ID_RE.fullmatch(project_id) is None
    ):
        _fail("PROJECT_ID_INVALID", "project_id must be a portable single path component")
    brief_copy = _validate_brief(brief)

    project = root / project_id
    _validate_project_path_boundary(project, allow_missing=True)
    state_path = project / _REVIEW_STATE_PATH
    state = {
        "blockers": [],
        "brief": brief_copy,
        "counts": {"claims": 0, "evidence": 0, "sources": 0},
        "current_stage": "review_brief",
        "project_id": project_id,
        "schema_version": "vertical-review-state.v1",
        "status": AWAITING_BRIEF_CONFIRMATION,
    }
    if project.exists():
        allowed_directories = {relative.parts[0] for relative in _INITIALIZATION_PATHS}
        for directory in project.iterdir():
            if directory.is_symlink():
                _fail("PROJECT_ALREADY_EXISTS", "project contains a symlink")
            if not directory.is_dir() or directory.name not in allowed_directories:
                _fail("PROJECT_ALREADY_EXISTS", "project contains an unknown object")
            for path in directory.iterdir():
                if path.is_symlink():
                    _fail("PROJECT_ALREADY_EXISTS", "project contains a symlink")
                relative = path.relative_to(project)
                if path.is_dir() or relative not in _INITIALIZATION_PATHS:
                    _fail("PROJECT_ALREADY_EXISTS", "project contains an unknown object")

    if state_path.exists() and _read_json(state_path, "PROJECT_STATE_INVALID") != state:
        _fail("PROJECT_ALREADY_EXISTS", "existing project state differs")
    for relative, object_type, expected in _INITIALIZATION_OBJECTS:
        path = project / relative
        if not path.exists():
            continue
        if object_type == "jsonl":
            actual = _read_jsonl(path, "PROJECT_INITIALIZATION_INVALID")
        else:
            actual = _read_json(path, "PROJECT_INITIALIZATION_INVALID")
        if actual != expected:
            _fail("PROJECT_ALREADY_EXISTS", "existing initialization object is not empty")

    for relative, object_type, expected in _INITIALIZATION_OBJECTS:
        path = project / relative
        if path.exists():
            continue
        if object_type == "jsonl":
            _write_jsonl(path, expected)
        else:
            _write_json(path, expected)
    _write_json(state_path, state)
    initial_artifact = Path("01_evidence/evidence_cards.jsonl")
    try:
        VersionContext.create(
            {
                "currentness": "current",
                "version_token": "project-initial-v1",
                "artifact_refs": [
                    {
                        "path": initial_artifact.as_posix(),
                        "sha256": hashlib.sha256(
                            (project / initial_artifact).read_bytes()
                        ).hexdigest(),
                    }
                ],
            },
            project_id=project_id,
            version_id="v1",
            branch_id="main",
            branch_name="Main",
            project_root=project,
        )
    except ProductFoundationError as exc:
        _fail("VERSION_CONTEXT_INITIALIZATION_FAILED", str(exc))
    return project


def confirm_review_brief(project: Path) -> dict[str, Any]:
    """Confirm the stored brief without changing its scope or starting discovery."""
    project = Path(project)
    state = _project_state(project)
    status = state.get("status")
    if status == BRIEF_CONFIRMED:
        if state.get("current_stage") != "ready_for_discovery":
            _fail("BRIEF_CONFIRMATION_STATE_INVALID", "confirmed brief has an invalid stage")
        return state
    if status != AWAITING_BRIEF_CONFIRMATION or state.get("current_stage") != "review_brief":
        _fail("BRIEF_CONFIRMATION_STATE_INVALID", "brief is not awaiting confirmation")
    if not isinstance(state.get("brief"), dict):
        _fail("PROJECT_STATE_INVALID", "review state does not contain a brief")
    confirmed = {
        **state,
        "current_stage": "ready_for_discovery",
        "status": BRIEF_CONFIRMED,
    }
    _write_json(project / _REVIEW_STATE_PATH, confirmed)
    return confirmed


def _source_locators(evidence_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("source_id", "page", "section_or_item", "depiction_locator")
    return [{key: ref[key] for key in keys if key in ref} for ref in evidence_refs]


def _review_target_digest(row: dict[str, Any]) -> str:
    payload = {
        key: row[key]
        for key in (
            "claim_id",
            "study_id",
            "original_text",
            "evidence_refs",
            "lineage",
            "risk_level",
            "risk_categories",
        )
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _validate_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        _fail("CANDIDATE_INVALID", "candidate must be a JSON object")
    study_id = candidate.get("study_id")
    job_id = candidate.get("job_id")
    claims = candidate.get("claims")
    if not isinstance(study_id, str) or not study_id.strip():
        _fail("STUDY_ID_INVALID", "candidate requires a nonempty study_id")
    if not isinstance(job_id, str) or not job_id.strip():
        _fail("CANDIDATE_JOB_ID_INVALID", "candidate requires a nonempty job_id")
    if not isinstance(claims, list) or not claims:
        _fail("CLAIMS_INVALID", "candidate requires grounded claims")
    seen: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            _fail("CLAIM_INVALID", "claims must be JSON objects")
        claim_id = claim.get("claim_id")
        text = claim.get("claim_text")
        refs = claim.get("evidence_refs")
        risk_level = claim.get("risk_level")
        categories = claim.get("risk_categories")
        if not isinstance(claim_id, str) or not claim_id.strip() or claim_id in seen:
            _fail("CLAIM_ID_INVALID", "claim_id values must be nonempty and unique")
        seen.add(claim_id)
        if not isinstance(text, str) or not text.strip():
            _fail("CLAIM_TEXT_INVALID", "claim_text must be nonempty")
        if risk_level not in RISK_LEVELS:
            _fail("CLAIM_RISK_INVALID", "risk_level must be R0, R1, R2, or R3")
        if (
            not isinstance(categories, list)
            or not all(isinstance(category, str) and category for category in categories)
            or len(categories) != len(set(categories))
        ):
            _fail("CLAIM_RISK_INVALID", "risk_categories must be unique nonempty strings")
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, dict) for ref in refs):
            _fail("CLAIM_EVIDENCE_INVALID", "every claim requires evidence_refs")
        for ref in refs:
            source_id = ref.get("source_id")
            page = ref.get("page")
            section = ref.get("section_or_item")
            locator = ref.get("locator")
            depiction = ref.get("depiction_locator")
            if not isinstance(source_id, str) or not source_id.strip():
                _fail("CLAIM_EVIDENCE_INVALID", "evidence refs require source_id")
            if not (
                isinstance(locator, (str, dict))
                and bool(locator)
                or isinstance(page, int)
                and not isinstance(page, bool)
                and page >= 1
                and isinstance(section, str)
                and bool(section.strip())
                or isinstance(depiction, str)
                and bool(depiction.strip())
            ):
                _fail("CLAIM_LOCATOR_INVALID", "evidence refs require a provenance locator")
    return candidate


def _validate_reviewer_findings(
    candidate: dict[str, Any],
    reviewer: dict[str, Any],
) -> None:
    if "findings" not in reviewer:
        return
    findings = reviewer["findings"]
    if not isinstance(findings, list) or not findings:
        _fail("REVIEWER_FINDINGS_INVALID", "findings must be a nonempty list")

    target_ids = [claim["claim_id"] for claim in candidate["claims"]]
    reaction_units = candidate.get("reaction_units", [])
    if not isinstance(reaction_units, list):
        _fail(
            "REVIEWER_FINDINGS_INVALID",
            "candidate reaction_units must expose review targets",
        )
    for reaction_unit in reaction_units:
        if not isinstance(reaction_unit, dict):
            _fail(
                "REVIEWER_FINDINGS_INVALID",
                "candidate reaction_units must expose review targets",
            )
        reaction_unit_id = reaction_unit.get("reaction_unit_id")
        if not isinstance(reaction_unit_id, str) or not reaction_unit_id.strip():
            _fail(
                "REVIEWER_FINDINGS_INVALID",
                "candidate reaction_units must expose review targets",
            )
        target_ids.append(reaction_unit_id)
    expected_targets = set(target_ids)
    if len(expected_targets) != len(target_ids):
        _fail(
            "REVIEWER_FINDINGS_INVALID",
            "candidate review target ids must be unique",
        )

    seen_targets: set[str] = set()
    target_verdicts: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            _fail("REVIEWER_FINDINGS_INVALID", "findings must contain objects")
        target_id = finding.get("target_id")
        if not isinstance(target_id, str) or target_id not in expected_targets:
            _fail(
                "REVIEWER_FINDINGS_INVALID",
                "finding target_id must identify a candidate reaction unit or claim",
            )
        if target_id in seen_targets:
            _fail("REVIEWER_FINDINGS_INVALID", "finding target_id values must be unique")
        verdict = finding.get("verdict")
        if not isinstance(verdict, str) or verdict not in REVIEWER_VERDICTS:
            _fail(
                "REVIEWER_FINDINGS_INVALID",
                "finding verdict must be SUPPORT, REJECT, or AMBIGUOUS",
            )
        reason = finding.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            _fail("REVIEWER_FINDINGS_INVALID", "finding reason must be a nonempty string")
        seen_targets.add(target_id)
        target_verdicts.append(verdict)

    if seen_targets != expected_targets:
        _fail(
            "REVIEWER_FINDINGS_INVALID",
            "findings must exactly cover candidate reaction units and claims",
        )
    reduced_verdict = (
        "REJECT"
        if "REJECT" in target_verdicts
        else "AMBIGUOUS"
        if "AMBIGUOUS" in target_verdicts
        else "SUPPORT"
    )
    if reviewer["verdict"] != reduced_verdict:
        _fail(
            "REVIEWER_FINDINGS_INVALID",
            "reviewer verdict is inconsistent with target findings",
        )


def _validate_identity_bindings(
    candidate: dict[str, Any],
    r0_report: Any,
    reviewer: Any,
) -> None:
    job_id = candidate["job_id"]
    if not isinstance(r0_report, dict) or r0_report.get("status") != "R0_PASS":
        _fail("R0_REJECTED", "study did not pass the grounding contract")
    if r0_report.get("job_id") != job_id or r0_report.get("candidate_job_id") != job_id:
        _fail("R0_BINDING_INVALID", "R0 report does not bind the candidate job")
    if (
        not isinstance(reviewer, dict)
        or not isinstance(reviewer.get("verdict"), str)
        or not reviewer["verdict"]
        or reviewer.get("job_id") != job_id
        or reviewer.get("study_id") != candidate["study_id"]
    ):
        _fail("REVIEWER_BINDING_INVALID", "reviewer does not bind the candidate job and study")
    if reviewer["verdict"] not in REVIEWER_VERDICTS:
        _fail(
            "REVIEWER_VERDICT_INVALID",
            "reviewer verdict must be SUPPORT, REJECT, or AMBIGUOUS",
        )
    _validate_reviewer_findings(candidate, reviewer)


def _reduce_decision(card: dict[str, Any], claim: dict[str, Any]) -> tuple[str, str]:
    if card["r0_report"].get("status") != "R0_PASS":
        return "BLOCKED", "R0_NOT_PASS"
    reviewer = card["reviewer"]
    reviewer_verdict = reviewer.get("verdict")
    if "findings" in reviewer:
        reviewer_verdict = next(
            finding["verdict"]
            for finding in reviewer["findings"]
            if finding["target_id"] == claim["claim_id"]
        )
    if reviewer_verdict != "SUPPORT":
        return "BLOCKED", "REVIEWER_NOT_SUPPORT"
    if claim["risk_level"] == "R3" or set(claim["risk_categories"]) & HIGH_RISK_CATEGORIES:
        return "HUMAN_REQUIRED", "HIGH_RISK_REQUIRES_HUMAN"
    return "APPROVED", "R0_PASS_AND_REVIEWER_SUPPORT"


def _projection_for_card(card: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = card["candidate"]
    projected: list[dict[str, Any]] = []
    for claim in candidate["claims"]:
        decision, reason = _reduce_decision(card, claim)
        row = {
            "claim_id": claim["claim_id"],
            "decision": decision,
            "decision_reason": reason,
            "evidence_refs": copy.deepcopy(claim["evidence_refs"]),
            "lineage": {
                "job_id": candidate.get("job_id"),
                "source_locators": _source_locators(claim["evidence_refs"]),
                "study_id": card["study_id"],
            },
            "original_text": claim["claim_text"],
            "risk_categories": copy.deepcopy(claim.get("risk_categories", [])),
            "risk_level": claim.get("risk_level"),
            "study_id": card["study_id"],
            "text": claim["claim_text"],
        }
        row["review_target_digest"] = _review_target_digest(row)
        projected.append(row)
    return projected


def _read_risk_decisions(project: Path) -> list[dict[str, Any]]:
    payload = _read_json(project / "03_review" / "risk_decisions.json", "RISK_DECISIONS_INVALID")
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        _fail("RISK_DECISIONS_INVALID", "risk decisions must contain a decisions list")
    if not all(isinstance(row, dict) for row in payload["decisions"]):
        _fail("RISK_DECISIONS_INVALID", "risk decision rows must be objects")
    return payload["decisions"]


def _apply_risk_records(
    projection: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    strict_targets: bool = True,
) -> list[dict[str, Any]]:
    reduced = copy.deepcopy(projection)
    by_id = {row["claim_id"]: row for row in reduced}
    targets: list[str] = []
    for record in decisions:
        claim_id = record.get("claim_id")
        action = record.get("action")
        if not isinstance(claim_id, str) or not claim_id:
            _fail("RISK_TARGET_INVALID", "risk decisions require claim_id")
        targets.append(claim_id)
        row = by_id.get(claim_id)
        if row is None:
            if strict_targets:
                _fail("RISK_TARGET_UNKNOWN", "risk decision target is not in the projection")
            continue
        if record.get("review_target_digest") != row["review_target_digest"]:
            continue
        if row["decision"] == "BLOCKED":
            if strict_targets:
                _fail("RISK_TARGET_BLOCKED", "blocked claims cannot be approved by risk review")
            continue
        if action == "APPROVE":
            row["decision"] = "APPROVED"
            row["decision_reason"] = "HUMAN_RISK_APPROVED"
            row["text"] = row["original_text"]
        elif action == "REWORD":
            approved_text = record.get("approved_text")
            if not isinstance(approved_text, str) or not approved_text.strip():
                _fail("APPROVED_TEXT_REQUIRED", "REWORD requires nonempty approved_text")
            row["decision"] = "APPROVED"
            row["decision_reason"] = "HUMAN_RISK_REWORDED"
            row["text"] = approved_text
        elif action == "EXCLUDE":
            row["decision"] = "BLOCKED"
            row["decision_reason"] = "HUMAN_RISK_EXCLUDED"
            row["text"] = row["original_text"]
        elif action == "UNRESOLVED":
            row["decision"] = "HUMAN_REQUIRED"
            row["decision_reason"] = "HUMAN_RISK_UNRESOLVED"
            row["text"] = row["original_text"]
        else:
            _fail("RISK_ACTION_INVALID", "risk action is invalid")
    if len(targets) != len(set(targets)):
        _fail("RISK_TARGET_DUPLICATE", "risk decision targets must be unique")
    return reduced


def _project_cards(
    cards: list[dict[str, Any]],
    risk_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    projection = [row for card in cards for row in _projection_for_card(card)]
    claim_ids = [row["claim_id"] for row in projection]
    if len(claim_ids) != len(set(claim_ids)):
        _fail("CLAIM_ID_DUPLICATE", "claim_id values must be unique across studies")
    projection.sort(key=lambda row: row["claim_id"])
    return _apply_risk_records(projection, risk_decisions, strict_targets=False)


def _validate_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    study_ids: list[str] = []
    for card in cards:
        candidate = _validate_candidate(card.get("candidate"))
        study_id = card.get("study_id")
        r0_report = card.get("r0_report")
        reviewer = card.get("reviewer")
        if study_id != candidate["study_id"]:
            _fail("EVIDENCE_CARD_INVALID", "evidence card study binding is invalid")
        _validate_identity_bindings(candidate, r0_report, reviewer)
        study_ids.append(study_id)
    if len(study_ids) != len(set(study_ids)):
        _fail("EVIDENCE_CARDS_INVALID", "stored study identities must be unique")
    return sorted(cards, key=lambda card: card["study_id"])


def _load_validated_cards(project: Path) -> list[dict[str, Any]]:
    return _validate_cards(
        _read_jsonl(
            project / "01_evidence" / "evidence_cards.jsonl",
            "EVIDENCE_CARDS_INVALID",
        )
    )


def _append_exception(
    project: Path,
    *,
    study_id: str | None,
    error_code: str,
    r0_status: str | None,
    reviewer_verdict: str | None,
) -> None:
    path = project / "01_evidence" / "exception_queue.json"
    queue = _read_json(path, "EXCEPTION_QUEUE_INVALID")
    if not isinstance(queue, dict) or not isinstance(queue.get("exceptions"), list):
        _fail("EXCEPTION_QUEUE_INVALID", "exception queue must contain an exceptions list")
    by_study: dict[str, dict[str, Any]] = {}
    for entry in queue["exceptions"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("study_id"), str)
            or not entry["study_id"]
        ):
            _fail("EXCEPTION_QUEUE_INVALID", "exception entries require study_id")
        by_study[entry["study_id"]] = entry
    failed_study_id = study_id or "UNKNOWN_STUDY"
    by_study[failed_study_id] = {
        "error_code": error_code,
        "r0_status": r0_status,
        "reviewer_verdict": reviewer_verdict,
        "study_id": failed_study_id,
    }
    queue["exceptions"] = [by_study[key] for key in sorted(by_study)]
    _write_json(path, queue)


def _clear_exception(project: Path, study_id: str) -> None:
    path = project / "01_evidence" / "exception_queue.json"
    queue = _read_json(path, "EXCEPTION_QUEUE_INVALID")
    if not isinstance(queue, dict) or not isinstance(queue.get("exceptions"), list):
        _fail("EXCEPTION_QUEUE_INVALID", "exception queue must contain an exceptions list")
    by_study: dict[str, dict[str, Any]] = {}
    for entry in queue["exceptions"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("study_id"), str)
            or not entry["study_id"]
        ):
            _fail("EXCEPTION_QUEUE_INVALID", "exception entries require study_id")
        by_study[entry["study_id"]] = entry
    if by_study.pop(study_id, None) is not None:
        queue["exceptions"] = [by_study[key] for key in sorted(by_study)]
        _write_json(path, queue)


def _invalidate_writer_packet(project: Path) -> None:
    try:
        (project / "02_claims" / "writer_packet.json").unlink(missing_ok=True)
    except OSError as exc:
        raise VerticalReviewError(
            "WRITER_PACKET_INVALIDATION_FAILED",
            "writer packet could not be invalidated before upstream change",
        ) from exc


def _sync_evidence_review_state(project: Path, projection: list[dict[str, Any]]) -> None:
    state = _project_state(project)
    if state.get("status") == AWAITING_BRIEF_CONFIRMATION:
        return
    refs = [
        ref
        for row in projection
        for ref in row.get("evidence_refs", [])
        if isinstance(ref, dict)
    ]
    source_ids = {
        ref["source_id"]
        for ref in refs
        if isinstance(ref.get("source_id"), str) and ref["source_id"]
    }
    synced = {
        **state,
        "counts": {
            "claims": len(projection),
            "evidence": len(refs),
            "sources": len(source_ids),
        },
        "current_stage": "evidence_review",
        "status": (
            "needs_human_review"
            if any(row.get("decision") == "HUMAN_REQUIRED" for row in projection)
            else "in_progress"
        ),
    }
    _write_json(project / _REVIEW_STATE_PATH, synced)


def register_study(
    project: Path,
    candidate: dict,
    r0_report: dict,
    reviewer: dict,
) -> dict:
    """Register or replace one grounded study card and rebuild its projection."""
    project_path = Path(project)
    _project_state(project_path)
    raw_study_id = candidate.get("study_id") if isinstance(candidate, dict) else None
    raw_r0_status = r0_report.get("status") if isinstance(r0_report, dict) else None
    raw_verdict = reviewer.get("verdict") if isinstance(reviewer, dict) else None
    try:
        candidate_copy = _validate_candidate(_json_copy(candidate, "CANDIDATE_INVALID"))
        r0_copy = _json_copy(r0_report, "R0_REPORT_INVALID")
        reviewer_copy = _json_copy(reviewer, "REVIEWER_INVALID")
        _validate_identity_bindings(candidate_copy, r0_copy, reviewer_copy)

        card = {
            "candidate": candidate_copy,
            "r0_report": r0_copy,
            "reviewer": reviewer_copy,
            "study_id": candidate_copy["study_id"],
        }
        cards_path = project_path / "01_evidence" / "evidence_cards.jsonl"
        cards = _validate_cards(_read_jsonl(cards_path, "EVIDENCE_CARDS_INVALID"))
        by_study = {row["study_id"]: row for row in cards}
        by_study[card["study_id"]] = card
        ordered_cards = _validate_cards(list(by_study.values()))
        projection = _project_cards(ordered_cards, _read_risk_decisions(project_path))
    except VerticalReviewError as exc:
        _append_exception(
            project_path,
            study_id=raw_study_id if isinstance(raw_study_id, str) else None,
            error_code=exc.code,
            r0_status=raw_r0_status if isinstance(raw_r0_status, str) else None,
            reviewer_verdict=raw_verdict if isinstance(raw_verdict, str) else None,
        )
        raise

    _invalidate_writer_packet(project_path)
    _write_jsonl(cards_path, ordered_cards)
    _write_jsonl(project_path / "02_claims" / "claim_projection.jsonl", projection)
    if reviewer_copy["verdict"] != "SUPPORT":
        _append_exception(
            project_path,
            study_id=card["study_id"],
            error_code="REVIEWER_NOT_SUPPORT",
            r0_status=r0_copy["status"],
            reviewer_verdict=reviewer_copy["verdict"],
        )
    else:
        _clear_exception(project_path, card["study_id"])
    _sync_evidence_review_state(project_path, projection)
    return {"claim_projection": projection, "study_id": card["study_id"]}


def rebuild_projection(project: Path) -> list[dict]:
    """Rebuild the consumer projection from evidence cards and recorded decisions."""
    project_path = Path(project)
    _project_state(project_path)
    projection = _project_cards(
        _load_validated_cards(project_path),
        _read_risk_decisions(project_path),
    )
    _invalidate_writer_packet(project_path)
    _write_jsonl(project_path / "02_claims" / "claim_projection.jsonl", projection)
    _sync_evidence_review_state(project_path, projection)
    return projection


def _load_projection(project: Path) -> list[dict[str, Any]]:
    expected = _project_cards(
        _load_validated_cards(project),
        _read_risk_decisions(project),
    )
    rows = _read_jsonl(project / "02_claims" / "claim_projection.jsonl", "PROJECTION_INVALID")
    try:
        matches = _json_bytes(rows) == _json_bytes(expected)
    except VerticalReviewError:
        matches = False
    if not matches:
        _fail("PROJECTION_INVALID", "stored projection differs from authoritative state")
    return rows


def build_risk_packet(project: Path, low_risk_sample_rate: float = 0.10) -> dict:
    """Build one de-duplicated packet of required reviews and low-risk audit claims."""
    project_path = Path(project)
    state = _project_state(project_path)
    if (
        isinstance(low_risk_sample_rate, bool)
        or not isinstance(low_risk_sample_rate, (int, float))
        or not math.isfinite(low_risk_sample_rate)
        or not 0 <= low_risk_sample_rate <= 1
    ):
        _fail("SAMPLE_RATE_INVALID", "low-risk sample rate must be finite and within [0, 1]")
    rate = float(low_risk_sample_rate)
    _load_projection(project_path)
    # A newly issued packet always starts from evidence, never from prior human
    # decisions. This makes rebuilding the packet an explicit invalidation.
    projection = _project_cards(_load_validated_cards(project_path), [])
    human = sorted(
        (row for row in projection if row["decision"] == "HUMAN_REQUIRED"),
        key=lambda row: row["claim_id"],
    )
    low_risk = sorted(
        (row for row in projection if row["decision"] == "APPROVED"),
        key=lambda row: (hashlib.sha256(row["claim_id"].encode("utf-8")).hexdigest(), row["claim_id"]),
    )
    sample_count = math.ceil(len(low_risk) * rate) if rate else 0
    selected_low_risk = low_risk[:sample_count]
    selected: dict[str, dict[str, Any]] = {}
    for row in human:
        target = copy.deepcopy(row)
        target["selection_reason"] = "HUMAN_REQUIRED"
        selected[row["claim_id"]] = target
    for row in selected_low_risk:
        target = copy.deepcopy(row)
        target["selection_reason"] = "LOW_RISK_AUDIT"
        selected.setdefault(row["claim_id"], target)
    targets = list(selected.values())
    previous_packet_path = project_path / "03_review" / "risk_packet.json"
    generation = 1
    if previous_packet_path.exists():
        previous_packet = _read_json(previous_packet_path, "RISK_PACKET_INVALID")
        if not isinstance(previous_packet, dict):
            _fail("RISK_PACKET_INVALID", "risk packet must be a JSON object")
        previous_generation = previous_packet.get("generation")
        if isinstance(previous_generation, int) and not isinstance(previous_generation, bool):
            generation = previous_generation + 1
    packet = {
        "generation": generation,
        "human_required_count": len(human),
        "low_risk_sample_count": len(selected_low_risk),
        "low_risk_sample_rate": rate,
        "project_id": state["project_id"],
        "schema_version": "vertical-review-risk-packet.v1",
        "target_count": len(targets),
        "targets": targets,
    }
    packet["packet_digest"] = hashlib.sha256(_canonical_json_bytes(packet)).hexdigest()
    decision_payload = {
        "decisions": [],
        "packet_digest": packet["packet_digest"],
        "project_id": state["project_id"],
        "schema_version": "vertical-review-risk-decisions.v2",
    }
    _invalidate_writer_packet(project_path)
    _write_jsonl(project_path / "02_claims" / "claim_projection.jsonl", projection)
    _write_json(project_path / "03_review" / "risk_decisions.json", decision_payload)
    _write_json(project_path / "03_review" / "risk_packet.json", packet)
    _write_json(
        project_path / _REVIEW_STATE_PATH,
        {
            **state,
            "current_stage": "ready_for_writing" if not targets else "risk_review",
            "status": "risk_decisions_applied" if not targets else "awaiting_risk_decisions",
        },
    )
    return packet


def apply_risk_decisions(project: Path, decisions: dict) -> list[dict]:
    """Apply human risk choices only to projection consumer status and wording."""
    project_path = Path(project)
    state = _project_state(project_path)
    payload = _json_copy(decisions, "RISK_DECISIONS_INVALID")
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        _fail("RISK_DECISIONS_INVALID", "decisions must contain a list")
    packet = _read_json(project_path / "03_review" / "risk_packet.json", "RISK_PACKET_INVALID")
    if not isinstance(packet, dict) or not isinstance(packet.get("targets"), list):
        _fail("RISK_PACKET_INVALID", "current risk packet is missing or invalid")
    packet_digest = packet.get("packet_digest")
    packet_without_digest = {key: value for key, value in packet.items() if key != "packet_digest"}
    expected_packet_digest = hashlib.sha256(
        _canonical_json_bytes(packet_without_digest)
    ).hexdigest()
    if (
        not isinstance(packet_digest, str)
        or packet_digest != expected_packet_digest
        or payload.get("packet_digest") != packet_digest
    ):
        _fail("RISK_PACKET_STALE", "risk decisions must bind to the current risk packet")
    normalized: list[dict[str, Any]] = []
    for row in payload["decisions"]:
        if not isinstance(row, dict):
            _fail("RISK_DECISIONS_INVALID", "decision rows must be objects")
        review_target_digest = row.get("review_target_digest")
        if not isinstance(review_target_digest, str) or not review_target_digest:
            _fail("RISK_TARGET_STALE", "risk decision requires a current target digest")
        record = {
            "action": row.get("action"),
            "claim_id": row.get("claim_id"),
            "review_target_digest": review_target_digest,
        }
        if row.get("action") == "REWORD":
            record["approved_text"] = row.get("approved_text")
        normalized.append(record)
    normalized.sort(key=lambda row: str(row.get("claim_id", "")))

    # Rebuild from cards so a prior human decision never becomes the new scientific baseline.
    base = _project_cards(_load_validated_cards(project_path), [])
    by_id = {row["claim_id"]: row for row in base}
    for record in normalized:
        row = by_id.get(record.get("claim_id"))
        if row is not None and record["review_target_digest"] != row["review_target_digest"]:
            _fail("RISK_TARGET_STALE", "risk decision target digest is stale")
    projected = _apply_risk_records(base, normalized)
    packet_target_ids = {
        row.get("claim_id") for row in packet["targets"] if isinstance(row, dict)
    }
    decision_target_ids = {row.get("claim_id") for row in normalized}
    if (
        None in packet_target_ids
        or len(packet_target_ids) != len(packet["targets"])
        or decision_target_ids != packet_target_ids
    ):
        _fail("RISK_REVIEW_INCOMPLETE", "every current risk target requires one decision")
    decision_payload = {
        "decisions": normalized,
        "packet_digest": packet_digest,
        "project_id": state["project_id"],
        "schema_version": "vertical-review-risk-decisions.v2",
    }
    _invalidate_writer_packet(project_path)
    _write_jsonl(project_path / "02_claims" / "claim_projection.jsonl", projected)
    _write_json(project_path / "03_review" / "risk_decisions.json", decision_payload)
    unresolved = any(row["decision"] == "HUMAN_REQUIRED" for row in projected)
    refs = [
        ref
        for row in projected
        for ref in row.get("evidence_refs", [])
        if isinstance(ref, dict)
    ]
    source_ids = {
        ref["source_id"]
        for ref in refs
        if isinstance(ref.get("source_id"), str) and ref["source_id"]
    }
    _write_json(
        project_path / _REVIEW_STATE_PATH,
        {
            **state,
            "counts": {
                "claims": len(projected),
                "evidence": len(refs),
                "sources": len(source_ids),
            },
            "current_stage": "risk_review" if unresolved else "ready_for_writing",
            "status": "awaiting_risk_decisions" if unresolved else "risk_decisions_applied",
        },
    )
    return projected


def build_writer_packet(project: Path) -> dict:
    """Write the only claim whitelist that manuscript generation may consume."""
    project_path = Path(project)
    # The evidence-to-release route owns its figure policy.  Keep the legacy
    # Pillow comparison map available for old projects, but never let it run
    # when a Source Truth bundle identifies the new route.
    if os.path.lexists(project_path / "01_evidence/source_truth"):
        from review_writer.project.review_figures import (
            FIGURE_POLICY,
            ReviewFigureError,
            build_source_figure_registry,
            synthesis_figure_placeholders,
        )

        try:
            registry = build_source_figure_registry(project_path)
            placeholders = synthesis_figure_placeholders(project_path)
        except ReviewFigureError as exc:
            _fail(exc.code, str(exc))
        return {
            "approved_claim_count": 0,
            "blocked_count": 0,
            "claims": [],
            "figure_policy": FIGURE_POLICY,
            "figures": registry["figures"],
            "synthesis_figure_placeholders": placeholders,
            "human_required_count": 0,
            "known_exclusions": [],
            "project_id": project_path.name,
            "schema_version": "evidence-to-release-writer-packet.v1",
        }
    state = _project_state(project_path)
    # Detect projection tampering before reporting workflow readiness.
    projection = _load_projection(project_path)
    if (
        state.get("status") != "risk_decisions_applied"
        or state.get("current_stage") != "ready_for_writing"
    ):
        _fail("RISK_REVIEW_INCOMPLETE", "current Risk Packet must be completed before writing")
    packet = _read_json(project_path / "03_review" / "risk_packet.json", "RISK_PACKET_INVALID")
    decisions = _read_json(
        project_path / "03_review" / "risk_decisions.json", "RISK_DECISIONS_INVALID"
    )
    if (
        not isinstance(packet, dict)
        or not isinstance(decisions, dict)
        or decisions.get("packet_digest") != packet.get("packet_digest")
    ):
        _fail("RISK_REVIEW_INCOMPLETE", "risk decisions are not bound to the current packet")
    if any(row["decision"] == "HUMAN_REQUIRED" for row in projection):
        _fail("RISK_REVIEW_INCOMPLETE", "unresolved risk targets block writing")
    approved = [copy.deepcopy(row) for row in projection if row["decision"] == "APPROVED"]
    excluded = [
        {
            "claim_id": row["claim_id"],
            "decision": row["decision"],
            "reason": row["decision_reason"],
            "study_id": row["study_id"],
        }
        for row in projection
        if row["decision"] != "APPROVED"
    ]
    figures = _build_comparative_evidence_figure(project_path, projection)
    packet = {
        "approved_claim_count": len(approved),
        "blocked_count": sum(row["decision"] == "BLOCKED" for row in projection),
        "claims": approved,
        "figures": figures,
        "human_required_count": sum(
            row["decision"] == "HUMAN_REQUIRED" for row in projection
        ),
        "known_exclusions": excluded,
        "project_id": state["project_id"],
        "projection_sha256": hashlib.sha256(_canonical_json_bytes(projection)).hexdigest(),
        "schema_version": "vertical-review-writer-packet.v1",
    }
    _write_json(project_path / "02_claims" / "writer_packet.json", packet)
    return packet


def _figure_text(value: Any, limit: int) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = " ".join(text.split()) or "Not recorded"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _build_comparative_evidence_figure(
    project: Path,
    projection: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    approved = [row for row in projection if row.get("decision") == "APPROVED"]
    if not approved:
        return []
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        _fail("FIGURE_RENDERER_MISSING", "Pillow is required for the original comparison figure")
        raise AssertionError from exc

    cards = _load_validated_cards(project)
    approved_by_study: dict[str, list[str]] = {}
    for row in approved:
        approved_by_study.setdefault(row["study_id"], []).append(row["claim_id"])
    rows: list[dict[str, Any]] = []
    for card in cards:
        study_id = card["study_id"]
        claim_ids = sorted(approved_by_study.get(study_id, []))
        if not claim_ids:
            continue
        candidate = card["candidate"]
        rows.append(
            {
                "activation_mode": _figure_text(candidate.get("activation_mode"), 34),
                "approved_claim_count": len(claim_ids),
                "citation": _figure_text(candidate.get("citation") or study_id, 44),
                "claim_ids": claim_ids,
                "reaction_class": _figure_text(candidate.get("reaction_class"), 34),
                "study_id": study_id,
            }
        )
    rows.sort(key=lambda row: row["study_id"])
    if not rows:
        return []

    width = 1400
    header_height = 170
    row_height = 96
    footer_height = 90
    height = header_height + row_height * len(rows) + footer_height
    image = Image.new("RGB", (width, height), "#F7F5EE")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    dark = "#173F35"
    muted = "#5F6F67"
    line = "#CBD4CE"
    accent = "#2D725F"
    draw.rectangle((0, 0, width, 18), fill=dark)
    draw.text((54, 48), "Comparative evidence landscape", fill=dark, font=font)
    draw.text(
        (54, 86),
        "Original figure generated only from approved review-writer evidence",
        fill=muted,
        font=font,
    )
    draw.text((54, 140), "Study", fill=muted, font=font)
    draw.text((450, 140), "Activation / reaction class", fill=muted, font=font)
    draw.text((1020, 140), "Approved claims", fill=muted, font=font)
    max_claims = max(row["approved_claim_count"] for row in rows)
    for index, row in enumerate(rows):
        top = header_height + index * row_height
        draw.line((54, top, width - 54, top), fill=line, width=2)
        draw.text((54, top + 24), row["citation"], fill=dark, font=font)
        draw.text((450, top + 18), row["activation_mode"], fill=dark, font=font)
        draw.text((450, top + 50), row["reaction_class"], fill=muted, font=font)
        bar_width = int(250 * row["approved_claim_count"] / max_claims)
        draw.rounded_rectangle(
            (1020, top + 26, 1020 + bar_width, top + 54),
            radius=10,
            fill=accent,
        )
        draw.text(
            (1034 + bar_width, top + 32),
            str(row["approved_claim_count"]),
            fill=dark,
            font=font,
        )
    draw.line((54, header_height + len(rows) * row_height, width - 54, header_height + len(rows) * row_height), fill=line, width=2)
    draw.text(
        (54, height - 54),
        "Counts describe the approved evidence whitelist, not publication volume or method quality.",
        fill=muted,
        font=font,
    )

    stage = project / "03_figure_redraw"
    stage.mkdir(parents=True, exist_ok=True)
    image_path = stage / "comparative_evidence_map.png"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=stage,
            prefix=".comparative_evidence_map.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            image.save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, image_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    source_claim_ids = sorted(row["claim_id"] for row in approved)
    figure = {
        "caption": (
            "Comparative evidence landscape for the included studies. Bar lengths show "
            "the number of claims admitted to the approved writer whitelist."
        ),
        "figure_id": "comparative-evidence-map",
        "license": "ORIGINAL_GENERATED",
        "markdown_path": "../03_figure_redraw/comparative_evidence_map.png",
        "source_claim_ids": source_claim_ids,
        "study_ids": [row["study_id"] for row in rows],
    }
    _write_json(
        stage / "figure_manifest.json",
        {
            "copied_source_images": False,
            "figures": [figure],
            "schema_version": "review-writer-original-figure-manifest.v1",
        },
    )
    return [figure]


def benchmark_metrics(project: Path) -> dict:
    """Return deterministic counts for the authoritative evidence/claim projection."""
    project_path = Path(project)
    state = _project_state(project_path)
    cards = _read_jsonl(
        project_path / "01_evidence" / "evidence_cards.jsonl",
        "EVIDENCE_CARDS_INVALID",
    )
    queue = _read_json(
        project_path / "01_evidence" / "exception_queue.json",
        "EXCEPTION_QUEUE_INVALID",
    )
    if not isinstance(queue, dict) or not isinstance(queue.get("exceptions"), list):
        _fail("EXCEPTION_QUEUE_INVALID", "exception queue must contain an exceptions list")
    projection = _load_projection(project_path)
    return {
        "approved_claim_count": sum(row["decision"] == "APPROVED" for row in projection),
        "blocked_claim_count": sum(row["decision"] == "BLOCKED" for row in projection),
        "exception_count": len(queue["exceptions"]),
        "human_required_claim_count": sum(
            row["decision"] == "HUMAN_REQUIRED" for row in projection
        ),
        "project_id": state["project_id"],
        "projected_claim_count": len(projection),
        "registered_study_count": len(cards),
    }
