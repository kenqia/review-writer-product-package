"""Section-level writing contract bound to the current synthesis projection."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .source_truth import REPO_ROOT, canonical_digest
from .paper_evidence_store import project_write_lock
from .synthesis import SynthesisError, _unsigned, _valid_decision, synthesis_state

PATH = Path("02_synthesis/section_contracts.jsonl")
SCHEMA = REPO_ROOT / "schemas/synthesis/section_contract.v1.schema.json"


class SectionContractError(ValueError):
    def __init__(self, code: str):
        super().__init__(code); self.code = code


def _root(project: Path) -> Path:
    p = Path(project)
    if p.is_symlink() or not p.is_dir(): raise SectionContractError("PROJECT_INVALID")
    return p.resolve(strict=True)


def _read(project: Path) -> list[dict[str, Any]]:
    path = project / PATH
    if not path.is_file() or path.is_symlink(): return []
    try: rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise SectionContractError("SECTION_CONTRACT_INVALID") from exc
    if not all(isinstance(x, dict) for x in rows): raise SectionContractError("SECTION_CONTRACT_INVALID")
    return rows


def _write(project: Path, rows: list[dict[str, Any]]) -> None:
    path = project / PATH; path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()): raise SectionContractError("SECTION_CONTRACT_PATH_INVALID")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def register_section_contracts(project: Path, payload: object) -> dict[str, Any]:
    project = _root(project)
    if not isinstance(payload, dict): raise SectionContractError("SECTION_CONTRACT_INVALID")
    synthesis = synthesis_state(project)
    if not synthesis.get("workflow_can_continue"): raise SectionContractError("SYNTHESIS_NOT_APPROVED")
    raw = payload.get("contracts", [payload])
    if not isinstance(raw, list): raise SectionContractError("SECTION_CONTRACT_INVALID")
    rows = _read(project); existing = {r.get("section_id"): r for r in rows}; out = []
    for candidate in raw:
        if not isinstance(candidate, dict): raise SectionContractError("SECTION_CONTRACT_INVALID")
        row = copy.deepcopy(candidate)
        if row.get("decision") is not None:
            raise SectionContractError("SECTION_CONTRACT_DECISION_INVALID")
        row.setdefault("schema_version", "section-contract.v1"); row.setdefault("decision", None); row.setdefault("synthesis_projection_digest", synthesis["projection_digest"])
        # These are mandatory by policy even when an empty-looking plan was supplied.
        if not row.get("counterevidence_and_limitations") or not row.get("figure_plan"): raise SectionContractError("SECTION_CONTRACT_INVALID")
        row["contract_digest"] = canonical_digest(_unsigned(row, "contract_digest"))
        try: schema = json.loads(SCHEMA.read_text(encoding="utf-8")); errors = list(Draft202012Validator(schema).iter_errors(row))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise SectionContractError("SECTION_CONTRACT_SCHEMA_INVALID") from exc
        if errors: raise SectionContractError("SECTION_CONTRACT_INVALID")
        prior = existing.get(row.get("section_id"))
        if prior is not None and prior != row: raise SectionContractError("SECTION_CONTRACT_ID_CONFLICT")
        existing[row["section_id"]] = row; out.append(row)
    merged = sorted(existing.values(), key=lambda r: r.get("section_id", ""))
    with project_write_lock(project): _write(project, merged)
    return {"contracts": out, "status": "needs_review"}


def apply_section_contract_decision(project: Path, payload: object) -> dict[str, Any]:
    project = _root(project)
    if not isinstance(payload, dict): raise SectionContractError("SECTION_CONTRACT_DECISION_INVALID")
    rows = _read(project); sid = payload.get("section_id")
    row = next((r for r in rows if r.get("section_id") == sid), None)
    if row is None: raise SectionContractError("SECTION_ID_NOT_FOUND")
    if row.get("synthesis_projection_digest") != synthesis_state(project).get("projection_digest"): raise SectionContractError("SECTION_CONTRACT_STALE")
    from .synthesis import _decision
    try: row["decision"] = _decision(payload, row["contract_digest"])
    except SynthesisError as exc: raise SectionContractError(exc.code) from exc
    row["status"] = "approved" if row["decision"]["action"] != "reject" else "rejected"
    with project_write_lock(project): _write(project, rows)
    return row


def section_contract_state(project: Path) -> dict[str, Any]:
    project = _root(project); synthesis = synthesis_state(project); rows = _read(project); projected = []
    for row in rows:
        row = copy.deepcopy(row)
        if row.get("contract_digest") != canonical_digest(_unsigned(row, "contract_digest")): row.update(status="stale", reason_code="SECTION_CONTRACT_DIGEST_INVALID")
        elif row.get("synthesis_projection_digest") != synthesis.get("projection_digest"): row.update(status="stale", reason_code="SECTION_CONTRACT_STALE")
        elif not row.get("decision"): row.update(status="needs_review", reason_code="SECTION_CONTRACT_REVIEW_REQUIRED")
        elif not _valid_decision(row["decision"], row["contract_digest"]): row.update(status="stale", reason_code="SECTION_CONTRACT_DECISION_INVALID")
        elif row["decision"].get("action") == "reject": row.update(status="rejected", reason_code="SECTION_CONTRACT_REJECTED")
        else: row.update(status="approved", reason_code="SECTION_CONTRACT_APPROVED")
        projected.append(row)
    ready = bool(projected) and synthesis.get("workflow_can_continue") and all(r.get("status") in {"approved", "rejected"} for r in projected) and any(r.get("status") == "approved" for r in projected)
    return {"status": "approved" if ready else "needs_review", "workflow_can_continue": ready, "reason_code": "SECTION_CONTRACT_APPROVED" if ready else "SECTION_CONTRACT_NOT_APPROVED", "projection_digest": canonical_digest(projected), "rows": projected}


def build_section_writer_packet(project: Path, section_id: str | None = None) -> dict[str, Any]:
    state = section_contract_state(project)
    if not state.get("workflow_can_continue"): raise SectionContractError(state["reason_code"])
    rows = [r for r in state["rows"] if r.get("status") == "approved" and (section_id is None or r.get("section_id") == section_id)]
    if section_id is not None and not rows: raise SectionContractError("SECTION_ID_NOT_FOUND")
    # Deliberately expose only current approved contract fields.
    fields = ("section_id", "research_question", "comparison_axes", "expected_synthesis", "counterevidence_and_limitations", "evidence_budget", "synthesis_budget", "figure_plan", "allowed_wording_strength")
    return {"schema_version": "section-writer-packet.v1", "sections": [{k: r[k] for k in fields} for r in rows], "section_contract_projection_digest": state["projection_digest"]}
