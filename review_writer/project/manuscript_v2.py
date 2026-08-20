"""Evidence-bound, section-level manuscript workflow for the new route."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .paper_evidence import PaperEvidenceError, paper_evidence_state
from .paper_evidence_store import PaperEvidenceStoreError, project_write_lock
from .parse_quality import project_parse_quality_state
from .section_contract import SectionContractError, section_contract_state
from .source_truth import REPO_ROOT, canonical_digest
from .synthesis import (
    SynthesisError,
    authoritative_synthesis_question_bindings,
    synthesis_state,
    validate_authoritative_review_questions,
)
from .workflow_projection import NEW_ROUTE, workflow_state
from .chemical_paper import chemical_paper_manuscript_bindings
from review_writer.delivery.dual_parse_release import (
    DualParseReleaseError,
    _non_exact_manuscript_release_allowed,
    dual_parse_manuscript_bindings,
    validate_dual_parse_release_bindings,
)


DRAFTS_PATH = Path("04_manuscript/section_drafts.jsonl")
MANUSCRIPT_PATH = Path("04_manuscript/manuscript.md")
LINEAGE_PATH = Path("04_manuscript/manuscript_lineage.v2.json")
LINEAGE_SCHEMA = REPO_ROOT / "schemas/delivery/manuscript_lineage.v2.schema.json"
SOURCE_FIGURE_PATH = Path("03_figures/source_figure_registry.json")
PLACEHOLDER_PATH = Path("03_figures/synthesis_figure_placeholders.json")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^(?!\.\.?$)(?!.*[/\\\x00\r\n])\S{1,240}$")
_MARKER = re.compile(
    r"\[(claim|evidence|synthesis):([A-Za-z0-9._:-]+)\]"
    r"|<!--\s*(claim|evidence|synthesis)\s*:\s*([A-Za-z0-9._:-]+)\s*-->",
    flags=re.IGNORECASE,
)
_SCIENTIFIC = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:%|°?C|K|h|min|s|mol|mmol|M|mM|nm|ppm|equiv)(?!\w)"
    r"|\b(?:reaction|yield|selectivit|conversion|catalyst|substrate|product|mechanis|"
    r"radical|oxidation|reduction|kinetic|spectrum|spectra|observed|reported|"
    r"afforded|produced|increased|decreased|supported|proposed)\w*\b)",
    flags=re.IGNORECASE,
)
_HIGH_RISK = re.compile(
    r"(?:\b(?:prove[sd]?|establish(?:es|ed)?|demonstrat(?:e|es|ed) that|"
    r"cause[sd]?|because of|mechanis\w*|always|never|universal|definitive)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:%|°?C|K|h|min|s|mol|mmol|M|mM|nm|ppm|equiv)(?!\w))",
    flags=re.IGNORECASE,
)
_TRANSITION_ONLY = re.compile(
    r"^(?:this section (?:introduces|outlines|is organized|turns to)|"
    r"the (?:next|following) section (?:introduces|examines|compares|discusses)|"
    r"we (?:next|now) (?:turn to|consider|compare|discuss)|"
    r"the discussion (?:next|now) turns to)\b",
    flags=re.IGNORECASE,
)
_REFERENCE_ENTRY = re.compile(r"^\s*(?:\[\d+\]|\d+[.)])\s+\S")


class ManuscriptV2Error(ValueError):
    """Stable fail-closed error for the evidence-bound manuscript route."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _root(project: Path) -> Path:
    supplied = Path(project)
    if supplied.is_symlink() or not supplied.is_dir():
        raise ManuscriptV2Error("PROJECT_INVALID")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise ManuscriptV2Error("PROJECT_INVALID") from exc
    source_truth = root / "01_evidence/source_truth"
    if not source_truth.is_dir() or source_truth.is_symlink():
        raise ManuscriptV2Error("NEW_ROUTE_REQUIRED")
    for relative in (Path("04_manuscript"), Path("03_figures")):
        current = root / relative
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise ManuscriptV2Error("PROJECT_PATH_INVALID")
    return root


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ManuscriptV2Error(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ManuscriptV2Error(code)
    return value


def _read_json(path: Path, code: str) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ManuscriptV2Error(code)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManuscriptV2Error(code) from exc


def _read_jsonl(project: Path) -> list[dict[str, Any]]:
    path = project / DRAFTS_PATH
    if not path.exists():
        return []
    if not path.is_file() or path.is_symlink():
        raise ManuscriptV2Error("SECTION_DRAFT_STATE_INVALID")
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManuscriptV2Error("SECTION_DRAFT_STATE_INVALID") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise ManuscriptV2Error("SECTION_DRAFT_STATE_INVALID")
    ids = [row.get("section_id") for row in rows]
    if any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
        raise ManuscriptV2Error("SECTION_DRAFT_STATE_INVALID")
    return rows


def _json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManuscriptV2Error("MANUSCRIPT_STATE_INVALID") from exc


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    try:
        return "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID") from exc


def _atomic_bytes(project: Path, relative: Path, payload: bytes) -> None:
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
        raise ManuscriptV2Error("MANUSCRIPT_PATH_INVALID")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise ManuscriptV2Error("MANUSCRIPT_WRITE_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _states(project: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        evidence = paper_evidence_state(project)
        synthesis = synthesis_state(project)
        contracts = section_contract_state(project)
    except (PaperEvidenceError, SynthesisError, SectionContractError) as exc:
        raise ManuscriptV2Error(getattr(exc, "code", "UPSTREAM_INVALID")) from exc
    if not isinstance(evidence, dict) or not evidence.get("workflow_can_continue"):
        raise ManuscriptV2Error("PAPER_EVIDENCE_NOT_APPROVED")
    if not isinstance(synthesis, dict) or not synthesis.get("workflow_can_continue"):
        raise ManuscriptV2Error("SYNTHESIS_NOT_APPROVED")
    if not isinstance(contracts, dict):
        raise ManuscriptV2Error("SECTION_CONTRACT_NOT_APPROVED")
    for state, key in ((evidence, "projection_digest"), (synthesis, "projection_digest"), (contracts, "projection_digest")):
        _digest(state.get(key), "UPSTREAM_DIGEST_INVALID")
    return evidence, synthesis, contracts


def _approved_contract(contracts: dict[str, Any], section_id: str) -> dict[str, Any]:
    rows = contracts.get("rows", [])
    if not isinstance(rows, list):
        raise ManuscriptV2Error("SECTION_CONTRACT_NOT_APPROVED")
    matches = [row for row in rows if isinstance(row, dict) and row.get("section_id") == section_id]
    if len(matches) != 1 or matches[0].get("status") != "approved":
        raise ManuscriptV2Error("SECTION_CONTRACT_NOT_APPROVED")
    _digest(matches[0].get("contract_digest"), "SECTION_CONTRACT_NOT_APPROVED")
    return matches[0]


def _approved_objects(
    evidence: dict[str, Any], synthesis: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    evidence_rows = evidence.get("rows", [])
    synthesis_rows = synthesis.get("rows", [])
    if not isinstance(evidence_rows, list) or not isinstance(synthesis_rows, list):
        raise ManuscriptV2Error("UPSTREAM_INVALID")
    approved_evidence = {
        row["evidence_id"]: row
        for row in evidence_rows
        if isinstance(row, dict)
        and row.get("status") == "approved"
        and isinstance(row.get("evidence_id"), str)
    }
    approved_synthesis = {
        row["synthesis_id"]: row
        for row in synthesis_rows
        if isinstance(row, dict)
        and row.get("status") == "approved"
        and isinstance(row.get("synthesis_id"), str)
    }
    return approved_evidence, approved_synthesis


def _line_has_science(value: str) -> bool:
    if _REFERENCE_ENTRY.match(value):
        return False
    cleaned = _MARKER.sub("", value)
    cleaned = re.sub(r"[`*_>#\-]", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned or _TRANSITION_ONLY.search(cleaned):
        return False
    if value.lstrip().startswith(("#", "![", "<!-- SYNTHESIS_FIGURE_PLACEHOLDER")):
        return False
    # Scientific vocabulary is an explicit signal.  Otherwise prose remains
    # fail-closed unless it is one of the narrow transition forms above.
    return bool(_SCIENTIFIC.search(cleaned) or re.search(r"[A-Za-z]{2,}", cleaned))


def _claim_bindings(
    body: str,
    evidence: dict[str, Any],
    synthesis: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(body, str) or not body.strip():
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID")
    approved_evidence, approved_synthesis = _approved_objects(evidence, synthesis)
    bindings: list[dict[str, Any]] = []
    high_risk: list[str] = []
    for block in re.split(r"\n\s*\n|\n", body):
        if not block.strip():
            continue
        markers = list(_MARKER.finditer(block))
        if _line_has_science(block) and not markers:
            raise ManuscriptV2Error("SCIENTIFIC_CLAIM_UNMARKED")
        for match in markers:
            marker_kind = (match.group(1) or match.group(3)).casefold()
            object_id = match.group(2) or match.group(4)
            paper_ids: list[str] = []
            synthesis_ids: list[str] = []
            if marker_kind == "evidence":
                if object_id not in approved_evidence:
                    raise ManuscriptV2Error("CLAIM_NOT_APPROVED")
                paper_ids = [object_id]
            elif marker_kind == "synthesis":
                if object_id not in approved_synthesis:
                    raise ManuscriptV2Error("CLAIM_NOT_APPROVED")
                synthesis_ids = [object_id]
            else:
                in_evidence = object_id in approved_evidence
                in_synthesis = object_id in approved_synthesis
                if in_evidence == in_synthesis:
                    raise ManuscriptV2Error("CLAIM_NOT_APPROVED")
                paper_ids = [object_id] if in_evidence else []
                synthesis_ids = [object_id] if in_synthesis else []
            bindings.append(
                {
                    "marker": match.group(0),
                    "paper_evidence_ids": paper_ids,
                    "synthesis_ids": synthesis_ids,
                }
            )
            if paper_ids and approved_evidence[object_id].get("risk_classes"):
                high_risk.append(f"paper_evidence:{object_id}")
            if synthesis_ids and approved_synthesis[object_id].get("risk_class"):
                high_risk.append(f"synthesis:{object_id}")
        if markers and _HIGH_RISK.search(_MARKER.sub("", block)):
            high_risk.append("high_risk_wording")
    return bindings, sorted(set(high_risk))


def _upstream_digest(evidence: dict[str, Any], synthesis: dict[str, Any], contracts: dict[str, Any]) -> str:
    return canonical_digest(
        {
            "paper_evidence_projection_digest": evidence["projection_digest"],
            "synthesis_projection_digest": synthesis["projection_digest"],
            "section_contract_projection_digest": contracts["projection_digest"],
        }
    )


def _draft_digest(row: dict[str, Any]) -> str:
    return canonical_digest(
        {
            key: value
            for key, value in row.items()
            if key not in {"draft_digest", "status", "reason_code", "decision"}
        }
    )


def _draft_is_current(
    row: dict[str, Any], evidence: dict[str, Any], synthesis: dict[str, Any], contracts: dict[str, Any]
) -> bool:
    try:
        contract = _approved_contract(contracts, str(row.get("section_id")))
    except ManuscriptV2Error:
        return False
    return (
        row.get("contract_digest") == contract.get("contract_digest")
        and row.get("paper_evidence_projection_digest") == evidence.get("projection_digest")
        and row.get("synthesis_projection_digest") == synthesis.get("projection_digest")
        and row.get("section_contract_projection_digest") == contracts.get("projection_digest")
        and row.get("draft_digest") == _draft_digest(row)
    )


def register_section_draft(project: Path, payload: object) -> dict[str, Any]:
    """Register one Content Agent section as an unapproved, evidence-bound draft."""
    root = _root(project)
    if not isinstance(payload, dict):
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID")
    allowed = {
        "section_id", "heading", "body", "markdown", "content_agent_result_digest",
        "generation_content_agent_result_digest", "decision",
    }
    if not set(payload) <= allowed or payload.get("decision") is not None:
        raise ManuscriptV2Error("CONTENT_AGENT_CANNOT_APPROVE")
    section_id = _identifier(payload.get("section_id"), "SECTION_ID_INVALID")
    heading = payload.get("heading")
    if not isinstance(heading, str) or not heading.strip() or heading != heading.strip() or "\n" in heading:
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID")
    body = payload.get("body", payload.get("markdown"))
    if "body" in payload and "markdown" in payload:
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID")
    if not isinstance(body, str) or not body.strip():
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID")
    generation_digest = payload.get(
        "content_agent_result_digest", payload.get("generation_content_agent_result_digest")
    )
    if (
        "content_agent_result_digest" in payload
        and "generation_content_agent_result_digest" in payload
    ):
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID")
    generation_digest = _digest(generation_digest, "CONTENT_AGENT_RESULT_DIGEST_REQUIRED")
    evidence, synthesis, contracts = _states(root)
    contract = _approved_contract(contracts, section_id)
    bindings, high_risk = _claim_bindings(body, evidence, synthesis)
    row: dict[str, Any] = {
        "schema_version": "manuscript-section-draft.v1",
        "section_id": section_id,
        "heading": heading,
        "body": body,
        "contract_digest": contract["contract_digest"],
        "paper_evidence_projection_digest": evidence["projection_digest"],
        "synthesis_projection_digest": synthesis["projection_digest"],
        "section_contract_projection_digest": contracts["projection_digest"],
        "generation_content_agent_result_digest": generation_digest,
        "claim_bindings": bindings,
        "high_risk_reasons": high_risk,
        "decision": None,
    }
    row["draft_digest"] = _draft_digest(row)
    row["status"] = "needs_human_edit" if high_risk else "needs_review"
    rows = _read_jsonl(root)
    prior = next((item for item in rows if item.get("section_id") == section_id), None)
    if prior is not None:
        if prior == row:
            return copy.deepcopy(prior)
        raise ManuscriptV2Error("SECTION_DRAFT_CONFLICT")
    rows.append(row)
    rows.sort(key=lambda item: item["section_id"])
    try:
        with project_write_lock(root):
            _atomic_bytes(root, DRAFTS_PATH, _jsonl_bytes(rows))
    except PaperEvidenceStoreError as exc:
        raise ManuscriptV2Error(exc.code) from exc
    return copy.deepcopy(row)


def generate_section_draft_v2(project: Path, payload: object) -> dict[str, Any]:
    """Generate a new review candidate from the current approved section.

    The Agent supplies only an additional, marker-bound candidate paragraph.
    The approved researcher body and heading are read from the canonical draft
    row and retained verbatim before the candidate is returned to Dashboard
    review.  This reuses the existing section-draft state and does not create a
    second history or current store.
    """
    root = _root(project)
    if not isinstance(payload, dict) or set(payload) != {
        "section_id",
        "body",
        "content_agent_result_digest",
    }:
        raise ManuscriptV2Error("SECTION_DRAFT_V2_INVALID")
    section_id = _identifier(payload.get("section_id"), "SECTION_ID_INVALID")
    addition = payload.get("body")
    if not isinstance(addition, str) or not addition.strip():
        raise ManuscriptV2Error("SECTION_DRAFT_V2_INVALID")
    generation_digest = _digest(
        payload.get("content_agent_result_digest"),
        "CONTENT_AGENT_RESULT_DIGEST_REQUIRED",
    )
    with project_write_lock(root):
        evidence, synthesis, contracts = _states(root)
        rows = _read_jsonl(root)
        prior = next((row for row in rows if row.get("section_id") == section_id), None)
        if prior is None:
            raise ManuscriptV2Error("SECTION_DRAFT_NOT_FOUND")
        if prior.get("status") != "approved" or not isinstance(prior.get("decision"), dict):
            raise ManuscriptV2Error("SECTION_DRAFT_BASE_NOT_APPROVED")
        if not _draft_is_current(prior, evidence, synthesis, contracts):
            raise ManuscriptV2Error("SECTION_DRAFT_STALE")
        decision = prior["decision"]
        if (
            decision.get("action") != "approve"
            or decision.get("bound_object_digest") != prior.get("draft_digest")
            or decision.get("upstream_digest") != _upstream_digest(evidence, synthesis, contracts)
        ):
            raise ManuscriptV2Error("SECTION_DRAFT_STALE")

        combined_body = f"{str(prior.get('body') or '').rstrip()}\n\n{addition.strip()}"
        bindings, high_risk = _claim_bindings(combined_body, evidence, synthesis)
        row = copy.deepcopy(prior)
        row["body"] = combined_body
        row["generation_content_agent_result_digest"] = generation_digest
        row["claim_bindings"] = bindings
        row["high_risk_reasons"] = high_risk
        row["parent_draft_digest"] = prior.get("draft_digest")
        row["parent_approval_digest"] = canonical_digest(decision)
        row["decision"] = None
        row["status"] = "needs_human_edit" if high_risk else "needs_review"
        row["draft_digest"] = _draft_digest(row)
        replacement = [
            row if item.get("section_id") == section_id else item
            for item in rows
        ]
        try:
            _atomic_bytes(root, DRAFTS_PATH, _jsonl_bytes(replacement))
        except PaperEvidenceStoreError as exc:
            raise ManuscriptV2Error(exc.code) from exc
    return copy.deepcopy(row)


def _actor(value: object) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ManuscriptV2Error("ACTOR_REQUIRED")
    actor_type = value.get("actor_type")
    actor_label = value.get("actor_label")
    if actor_type not in {"human_researcher", "simulated_researcher_agent"}:
        raise ManuscriptV2Error("ACTOR_INVALID")
    if not isinstance(actor_label, str) or not actor_label.strip() or len(actor_label) > 200:
        raise ManuscriptV2Error("ACTOR_INVALID")
    return actor_type, actor_label.strip()


def approve_section(
    project: Path,
    section_id: str,
    actor: object,
    *,
    edited_body: str | None = None,
    reason: str | None = None,
    expected_draft_digest: str | None = None,
) -> dict[str, Any]:
    """Record a researcher approval, requiring an explicit edit for high-risk text."""
    root = _root(project)
    section_id = _identifier(section_id, "SECTION_ID_INVALID")
    rows = _read_jsonl(root)
    row = next((item for item in rows if item.get("section_id") == section_id), None)
    if row is None:
        raise ManuscriptV2Error("SECTION_DRAFT_NOT_FOUND")
    if row.get("status") != "approved" and row.get("high_risk_reasons") and actor is None:
        raise ManuscriptV2Error("HIGH_RISK_EDIT_PENDING")
    if expected_draft_digest is not None and expected_draft_digest != row.get("draft_digest"):
        raise ManuscriptV2Error("SECTION_DRAFT_STALE")
    evidence, synthesis, contracts = _states(root)
    if not _draft_is_current(row, evidence, synthesis, contracts):
        raise ManuscriptV2Error("SECTION_DRAFT_STALE")
    if row.get("status") == "approved":
        decision = row.get("decision")
        if not isinstance(decision, dict) or decision.get("upstream_digest") != _upstream_digest(
            evidence, synthesis, contracts
        ):
            raise ManuscriptV2Error("SECTION_DRAFT_STALE")
        if edited_body is None or edited_body == row.get("body"):
            return copy.deepcopy(row)
    actor_type, actor_label = _actor(actor)
    original = row["body"]
    replacement = edited_body if edited_body is not None else original
    if not isinstance(replacement, str) or not replacement.strip():
        raise ManuscriptV2Error("SECTION_DRAFT_INVALID")
    if row.get("high_risk_reasons") and replacement == original:
        raise ManuscriptV2Error("HIGH_RISK_EDIT_PENDING")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ManuscriptV2Error("APPROVAL_REASON_REQUIRED")
    bindings, high_risk = _claim_bindings(replacement, evidence, synthesis)
    row["body"] = replacement
    row["claim_bindings"] = bindings
    row["high_risk_reasons"] = high_risk
    row["draft_digest"] = _draft_digest(row)
    upstream = _upstream_digest(evidence, synthesis, contracts)
    row["decision"] = {
        "actor_type": actor_type,
        "actor_label": actor_label,
        "action": "approve",
        "reason": reason.strip(),
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "bound_object_digest": row["draft_digest"],
        "upstream_digest": upstream,
        "original_expression": original,
        "edited_expression": replacement,
    }
    row["status"] = "approved"
    try:
        with project_write_lock(root):
            _atomic_bytes(root, DRAFTS_PATH, _jsonl_bytes(rows))
    except PaperEvidenceStoreError as exc:
        raise ManuscriptV2Error(exc.code) from exc
    return copy.deepcopy(row)


def _projected_drafts(project: Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(project)
    try:
        evidence, synthesis, contracts = _states(project)
    except ManuscriptV2Error:
        return [dict(row, status="stale", reason_code="UPSTREAM_NOT_APPROVED") for row in rows]
    projected: list[dict[str, Any]] = []
    for row in rows:
        value = copy.deepcopy(row)
        if not _draft_is_current(value, evidence, synthesis, contracts):
            value.update(status="stale", reason_code="SECTION_DRAFT_STALE")
        elif value.get("decision") is not None:
            decision = value["decision"]
            if not isinstance(decision, dict) or (
                decision.get("bound_object_digest") != value.get("draft_digest")
                or decision.get("upstream_digest") != _upstream_digest(evidence, synthesis, contracts)
            ):
                value.update(status="stale", reason_code="SECTION_APPROVAL_STALE")
        projected.append(value)
    return projected


def build_manuscript_workspace(project: Path) -> dict[str, Any]:
    """Return only new-route section/manuscript state; legacy draft bytes are ignored."""
    root = _root(project)
    sections = _projected_drafts(root)
    manuscript: str | None = None
    manuscript_path = root / MANUSCRIPT_PATH
    lineage_path = root / LINEAGE_PATH
    if manuscript_path.is_file() and not manuscript_path.is_symlink():
        try:
            manuscript = manuscript_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ManuscriptV2Error("MANUSCRIPT_INVALID") from exc
    lineage_present = lineage_path.is_file() and not lineage_path.is_symlink()
    authoritative = manuscript_state(root)
    return {
        "schema_version": "manuscript-workspace.v2",
        "route": NEW_ROUTE,
        "project_id": root.name,
        "status": "approved" if authoritative["workflow_can_continue"] else "in_progress",
        "reason_code": authoritative["reason_code"],
        "sections": sections,
        "manuscript": manuscript,
        "lineage_present": lineage_present,
    }


def manuscript_state(project: Path) -> dict[str, Any]:
    """Validate the current authoritative pair against every non-circular upstream binding."""
    root = _root(project)
    manuscript_path = root / MANUSCRIPT_PATH
    lineage_path = root / LINEAGE_PATH
    if not manuscript_path.is_file() or manuscript_path.is_symlink() or not lineage_path.is_file() or lineage_path.is_symlink():
        return {
            "status": "needs_review",
            "workflow_can_continue": False,
            "reason_code": "MANUSCRIPT_NOT_APPROVED",
        }
    try:
        manuscript_bytes = manuscript_path.read_bytes()
        manuscript_bytes.decode("utf-8")
        lineage = _read_json(lineage_path, "MANUSCRIPT_LINEAGE_INVALID")
        if not isinstance(lineage, dict):
            raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_INVALID")
        _validate_lineage(lineage)
        expected_lineage = canonical_digest(
            {key: value for key, value in lineage.items() if key != "lineage_digest"}
        )
        if lineage.get("lineage_digest") != expected_lineage:
            raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
        if lineage.get("manuscript_sha256") != hashlib.sha256(manuscript_bytes).hexdigest():
            raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
        evidence, synthesis, contracts = _states(root)
        if (
            lineage.get("paper_evidence_projection_digest") != evidence["projection_digest"]
            or lineage.get("synthesis_projection_digest") != synthesis["projection_digest"]
            or lineage.get("section_contract_projection_digest") != contracts["projection_digest"]
            or lineage.get("parse_object_digests") != _parse_object_digests(root)
        ):
            raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
        if synthesis.get("authoritative_run") is True:
            try:
                question_binding = validate_authoritative_review_questions(
                    {
                        "authoritative_run": synthesis.get("authoritative_run"),
                        "review_questions": synthesis.get("review_questions"),
                        "review_questions_digest": synthesis.get("review_questions_digest"),
                    }
                )
            except SynthesisError as exc:
                raise ManuscriptV2Error(exc.code) from exc
            if any(lineage.get(key) != value for key, value in question_binding.items()):
                raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
            try:
                expected_question_artifacts = authoritative_synthesis_question_bindings(
                    synthesis
                )
            except SynthesisError as exc:
                raise ManuscriptV2Error(exc.code) from exc
            if lineage.get("synthesis_question_bindings") != expected_question_artifacts:
                raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
        registry_digest, placeholder_digest = _figure_digests(root)
        if (
            lineage.get("source_figure_registry_digest") != registry_digest
            or lineage.get("synthesis_figure_placeholder_digest") != placeholder_digest
        ):
            raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
        drafts = _projected_drafts(root)
        if not drafts or any(row.get("status") != "approved" for row in drafts):
            raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
        expected_sections = [
            {
                "section_id": row["section_id"],
                "contract_digest": row["contract_digest"],
                "draft_digest": row["draft_digest"],
                "generation_content_agent_result_digest": row["generation_content_agent_result_digest"],
                "approval": row["decision"],
            }
            for row in drafts
        ]
        expected_claims: list[dict[str, Any]] = []
        for row in drafts:
            approval_digest = canonical_digest(row["decision"])
            for binding in row["claim_bindings"]:
                expected_claims.append(
                    {
                        "section_id": row["section_id"],
                        "marker": binding["marker"],
                        "paper_evidence_ids": binding["paper_evidence_ids"],
                        "synthesis_ids": binding["synthesis_ids"],
                        "section_approval_digest": approval_digest,
                    }
                )
        dual_source_root = root / "01_evidence/dual_source"
        if dual_source_root.exists():
            if dual_source_root.is_symlink() or not dual_source_root.is_dir():
                raise ManuscriptV2Error("MANUSCRIPT_DUAL_PARSE_STALE")
            expected_dual_studies = _manuscript_dependency_studies(
                evidence, synthesis, expected_claims
            )
            if {
                row.get("study_id")
                for row in lineage.get("dual_parse_bindings", [])
                if isinstance(row, dict)
            } != expected_dual_studies:
                raise ManuscriptV2Error("MANUSCRIPT_DUAL_PARSE_STALE")
            try:
                dual_currentness = validate_dual_parse_release_bindings(
                    root,
                    {"dual_parse_bindings": lineage.get("dual_parse_bindings")},
                    allow_non_exact=_non_exact_manuscript_release_allowed(
                        root, {"claim_bindings": expected_claims}
                    ),
                )
            except DualParseReleaseError as exc:
                raise ManuscriptV2Error("MANUSCRIPT_DUAL_PARSE_STALE") from exc
            if dual_currentness.get("workflow_can_continue") is not True:
                raise ManuscriptV2Error("MANUSCRIPT_DUAL_PARSE_STALE")
        chemical_root = root / "01_evidence/chemical_paper"
        if chemical_root.exists():
            chemical_bindings = chemical_paper_manuscript_bindings(root)
            expected_chemical_claims = _chemical_claim_dependencies(
                expected_claims,
                _chemical_evidence_dependencies(evidence),
            )
            if (
                lineage.get("chemical_paper_import_digests")
                != chemical_bindings["chemical_paper_import_digests"]
                or lineage.get("chemical_paper_safe_summary")
                != chemical_bindings["chemical_paper_safe_summary"]
                or lineage.get("chemical_paper_claim_dependencies")
                != expected_chemical_claims
            ):
                raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
        contract_rows = contracts.get("rows", [])
        expected_contracts = {
            row["section_id"]: row["contract_digest"]
            for row in contract_rows
            if isinstance(row, dict) and row.get("status") == "approved"
        }
        expected_generation = sorted(
            {row["generation_content_agent_result_digest"] for row in drafts}
        )
        if (
            lineage.get("sections") != expected_sections
            or lineage.get("claim_bindings") != expected_claims
            or lineage.get("section_contract_digests") != expected_contracts
            or lineage.get("generation_content_agent_result_digests") != expected_generation
        ):
            raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_STALE")
    except (OSError, UnicodeError, ManuscriptV2Error) as exc:
        code = exc.code if isinstance(exc, ManuscriptV2Error) else "MANUSCRIPT_INVALID"
        return {"status": "needs_review", "workflow_can_continue": False, "reason_code": code}
    return {
        "status": "approved",
        "workflow_can_continue": True,
        "reason_code": "MANUSCRIPT_APPROVED",
        "manuscript_sha256": lineage["manuscript_sha256"],
        "lineage_digest": lineage["lineage_digest"],
    }


def _figure_digests(project: Path) -> tuple[str | None, str | None]:
    registry_digest: str | None = None
    registry_path = project / SOURCE_FIGURE_PATH
    if registry_path.exists():
        try:
            # Source Figure registry digests bind the current Source Truth,
            # content_list_v2, Chemical Paper imports, figures, and locator
            # gaps.  Reuse the formal loader so manuscript lineage cannot
            # silently accept the older figures-only digest contract.
            from .review_figures import ReviewFigureError, load_source_figure_registry

            registry = load_source_figure_registry(project)
        except ReviewFigureError as exc:
            raise ManuscriptV2Error(exc.code) from exc
        registry_digest = _digest(
            registry.get("registry_digest"), "SOURCE_FIGURE_REGISTRY_INVALID"
        )
    placeholder_digest: str | None = None
    placeholder_path = project / PLACEHOLDER_PATH
    if placeholder_path.exists():
        placeholders = _read_json(placeholder_path, "FIGURE_PLACEHOLDER_INVALID")
        if not isinstance(placeholders, dict) or not isinstance(placeholders.get("placeholders"), list):
            raise ManuscriptV2Error("FIGURE_PLACEHOLDER_INVALID")
        placeholder_digest = canonical_digest(placeholders["placeholders"])
    return registry_digest, placeholder_digest


def _parse_object_digests(project: Path) -> list[str]:
    state = project_parse_quality_state(project)
    if not isinstance(state, dict) or not state.get("workflow_can_continue"):
        raise ManuscriptV2Error("PARSE_QUALITY_NOT_APPROVED")
    digests: list[str] = []
    for study in state.get("studies", []):
        if not isinstance(study, dict):
            raise ManuscriptV2Error("PARSE_QUALITY_INVALID")
        for row in study.get("objects", []):
            if not isinstance(row, dict):
                raise ManuscriptV2Error("PARSE_QUALITY_INVALID")
            digests.append(_digest(row.get("object_digest"), "PARSE_QUALITY_INVALID"))
    if not digests or len(digests) != len(set(digests)):
        raise ManuscriptV2Error("PARSE_QUALITY_INVALID")
    return sorted(digests)


def _chemical_evidence_dependencies(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for row in evidence.get("rows", []):
        if not isinstance(row, dict):
            continue
        evidence_id = row.get("evidence_id")
        for dependency in row.get("chemical_dependencies", []):
            if isinstance(dependency, dict):
                dependencies.append({"evidence_id": evidence_id, **copy.deepcopy(dependency)})
    return dependencies


def _chemical_claim_dependencies(
    claim_bindings: list[dict[str, Any]], evidence_dependencies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_evidence: dict[str, list[dict[str, Any]]] = {}
    for row in evidence_dependencies:
        evidence_id = row.get("evidence_id")
        if isinstance(evidence_id, str):
            by_evidence.setdefault(evidence_id, []).append(row)
    rows: list[dict[str, Any]] = []
    for binding in claim_bindings:
        evidence_ids = binding.get("paper_evidence_ids", [])
        for evidence_id in evidence_ids:
            for dependency in by_evidence.get(evidence_id, []):
                required_fields = dependency.get("required_fields", [])
                if (
                    not isinstance(required_fields, list)
                    or required_fields != sorted(set(required_fields))
                    or any(field != "resolved_smiles" for field in required_fields)
                ):
                    raise ManuscriptV2Error(
                        "CHEMICAL_DEPENDENCY_REQUIRED_FIELDS_LEGACY"
                    )
                row = {
                    "claim_id": canonical_digest(binding),
                    "study_id": dependency.get("study_id"),
                    "molecule_index": dependency.get("molecule_index"),
                    "required_fields": list(required_fields),
                    "requires_element_review": dependency.get("requires_element_review", False),
                    "requires_reaction_data": dependency.get("requires_reaction_data", False),
                }
                if row not in rows:
                    rows.append(row)
    return sorted(rows, key=lambda row: (row["claim_id"], row["study_id"], row["molecule_index"]))


def _manuscript_dependency_studies(
    evidence: dict[str, Any],
    synthesis: dict[str, Any],
    claim_bindings: list[dict[str, Any]],
) -> set[str]:
    evidence_study = {
        row.get("evidence_id"): row.get("study_id")
        for row in evidence.get("rows", [])
        if isinstance(row, dict)
        and isinstance(row.get("evidence_id"), str)
        and isinstance(row.get("study_id"), str)
    }
    synthesis_evidence: dict[str, set[str]] = {}
    for row in synthesis.get("rows", []):
        if not isinstance(row, dict) or not isinstance(row.get("synthesis_id"), str):
            continue
        identifiers = {
            value
            for key in ("supporting_evidence_ids", "counter_evidence_ids")
            for value in (row.get(key) if isinstance(row.get(key), list) else [])
            if isinstance(value, str)
        }
        synthesis_evidence[row["synthesis_id"]] = identifiers
    evidence_ids: set[str] = set()
    for binding in claim_bindings:
        evidence_ids.update(
            value
            for value in binding.get("paper_evidence_ids", [])
            if isinstance(value, str)
        )
        for synthesis_id in binding.get("synthesis_ids", []):
            if isinstance(synthesis_id, str):
                evidence_ids.update(synthesis_evidence.get(synthesis_id, set()))
    studies = {
        evidence_study[evidence_id]
        for evidence_id in evidence_ids
        if evidence_id in evidence_study
    }
    if not studies:
        raise ManuscriptV2Error("MANUSCRIPT_DUAL_PARSE_DEPENDENCY_MISSING")
    return studies


def _validate_lineage(value: dict[str, Any]) -> None:
    try:
        schema = json.loads(LINEAGE_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_SCHEMA_INVALID") from exc
    if list(Draft202012Validator(schema).iter_errors(value)):
        raise ManuscriptV2Error("MANUSCRIPT_LINEAGE_INVALID")


def _write_authoritative_pair(
    project: Path, manuscript_bytes: bytes, lineage_bytes: bytes
) -> None:
    snapshots: dict[Path, bytes | None] = {}
    for relative in (MANUSCRIPT_PATH, LINEAGE_PATH):
        path = project / relative
        snapshots[relative] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    written: list[Path] = []
    try:
        for relative, payload in (
            (MANUSCRIPT_PATH, manuscript_bytes),
            (LINEAGE_PATH, lineage_bytes),
        ):
            _atomic_bytes(project, relative, payload)
            written.append(relative)
    except Exception:
        for relative in reversed(written):
            previous = snapshots[relative]
            if previous is None:
                (project / relative).unlink(missing_ok=True)
            else:
                _atomic_bytes(project, relative, previous)
        raise


def merge_authoritative_manuscript(project: Path) -> dict[str, Any]:
    """Merge all current approved section drafts and atomically publish lineage v2."""
    root = _root(project)
    evidence, synthesis, contracts = _states(root)
    contract_rows = contracts.get("rows", [])
    if (
        not contracts.get("workflow_can_continue")
        or not isinstance(contract_rows, list)
        or not contract_rows
        or any(not isinstance(row, dict) or row.get("status") != "approved" for row in contract_rows)
    ):
        raise ManuscriptV2Error("SECTION_CONTRACT_NOT_APPROVED")
    drafts = _projected_drafts(root)
    by_id = {row.get("section_id"): row for row in drafts}
    contract_ids = [row["section_id"] for row in contract_rows]
    if set(by_id) != set(contract_ids) or any(by_id[sid].get("status") != "approved" for sid in contract_ids):
        raise ManuscriptV2Error("SECTION_DRAFT_NOT_APPROVED")
    ordered = [by_id[sid] for sid in contract_ids]
    manuscript = f"# {root.name}\n\n" + "\n\n".join(
        f"## {row['heading']}\n\n{row['body'].strip()}" for row in ordered
    ) + "\n"
    manuscript_bytes = manuscript.encode("utf-8")
    workflow = workflow_state(root)
    if not isinstance(workflow, dict) or workflow.get("route") != NEW_ROUTE:
        raise ManuscriptV2Error("WORKFLOW_STATE_INVALID")
    workflow_digest = _digest(workflow.get("workflow_digest"), "WORKFLOW_STATE_INVALID")
    registry_digest, placeholder_digest = _figure_digests(root)
    section_rows: list[dict[str, Any]] = []
    claim_bindings: list[dict[str, Any]] = []
    for row in ordered:
        decision = row.get("decision")
        if not isinstance(decision, dict):
            raise ManuscriptV2Error("SECTION_DRAFT_NOT_APPROVED")
        section_rows.append(
            {
                "section_id": row["section_id"],
                "contract_digest": row["contract_digest"],
                "draft_digest": row["draft_digest"],
                "generation_content_agent_result_digest": row["generation_content_agent_result_digest"],
                "approval": copy.deepcopy(decision),
            }
        )
        approval_digest = canonical_digest(decision)
        for binding in row["claim_bindings"]:
            claim_bindings.append(
                {
                    "section_id": row["section_id"],
                    "marker": binding["marker"],
                    "paper_evidence_ids": binding["paper_evidence_ids"],
                    "synthesis_ids": binding["synthesis_ids"],
                    "section_approval_digest": approval_digest,
                }
            )
    lineage: dict[str, Any] = {
        "schema_version": "manuscript-lineage.v2",
        "route": NEW_ROUTE,
        "project_id": root.name,
        "workflow_digest": workflow_digest,
        "parse_object_digests": _parse_object_digests(root),
        "paper_evidence_projection_digest": evidence["projection_digest"],
        "synthesis_projection_digest": synthesis["projection_digest"],
        "section_contract_projection_digest": contracts["projection_digest"],
        "section_contract_digests": {
            row["section_id"]: row["contract_digest"] for row in contract_rows
        },
        "source_figure_registry_digest": registry_digest,
        "synthesis_figure_placeholder_digest": placeholder_digest,
        "generation_content_agent_result_digests": sorted(
            {row["generation_content_agent_result_digest"] for row in ordered}
        ),
        "sections": section_rows,
        "claim_bindings": claim_bindings,
        "manuscript_sha256": hashlib.sha256(manuscript_bytes).hexdigest(),
    }
    if synthesis.get("authoritative_run") is True:
        try:
            lineage.update(
                validate_authoritative_review_questions(
                    {
                        "authoritative_run": synthesis.get("authoritative_run"),
                        "review_questions": synthesis.get("review_questions"),
                        "review_questions_digest": synthesis.get("review_questions_digest"),
                    }
                )
            )
            lineage["synthesis_question_bindings"] = authoritative_synthesis_question_bindings(
                synthesis
            )
        except SynthesisError as exc:
            raise ManuscriptV2Error(exc.code) from exc
    dual_source_root = root / "01_evidence/dual_source"
    if dual_source_root.exists():
        if dual_source_root.is_symlink() or not dual_source_root.is_dir():
            raise ManuscriptV2Error("MANUSCRIPT_DUAL_PARSE_STALE")
        study_ids = _manuscript_dependency_studies(evidence, synthesis, claim_bindings)
        try:
            lineage["dual_parse_bindings"] = dual_parse_manuscript_bindings(
                root, study_ids
            )
        except DualParseReleaseError as exc:
            raise ManuscriptV2Error("MANUSCRIPT_DUAL_PARSE_STALE") from exc
    if (root / "01_evidence/chemical_paper").exists():
        lineage.update(chemical_paper_manuscript_bindings(root))
        lineage["chemical_paper_claim_dependencies"] = _chemical_claim_dependencies(
            claim_bindings,
            _chemical_evidence_dependencies(evidence),
        )
    lineage["lineage_digest"] = canonical_digest(lineage)
    _validate_lineage(lineage)
    try:
        with project_write_lock(root):
            _write_authoritative_pair(root, manuscript_bytes, _json_bytes(lineage))
    except PaperEvidenceStoreError as exc:
        raise ManuscriptV2Error(exc.code) from exc
    return {
        "status": "approved",
        "manuscript_path": MANUSCRIPT_PATH.as_posix(),
        "lineage_path": LINEAGE_PATH.as_posix(),
        "manuscript_sha256": lineage["manuscript_sha256"],
        "lineage_digest": lineage["lineage_digest"],
    }
