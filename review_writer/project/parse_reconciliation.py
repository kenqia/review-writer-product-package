"""Object-level reconciliation between current Generic and Chemical candidates."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from review_writer.project.chemical_completion import (
    ChemicalCompletionError,
    require_chemical_completion_ready,
    require_honest_progressive_projection,
)
from review_writer.project.chemical_paper import ChemicalPaperError, chemical_paper_projection
from review_writer.project.dual_source import DualSourceError, require_dual_source_ready
from review_writer.project.paper_evidence_store import PaperEvidenceStoreError, project_write_lock
from review_writer.project.source_truth import (
    SourceTruthError,
    canonical_digest,
    declared_study_ids,
    load_source_truth_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "schemas/evidence/parse_reconciliation.v2.schema.json"
ROOT = Path("01_evidence/parse_reconciliation")
UNRESOLVED = frozenset({"conflict", "single_lane_only", "needs_review", "stale", "blocked"})


class ParseReconciliationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path(project: Path, study_id: str) -> Path:
    if not study_id or study_id in {".", ".."} or "/" in study_id or "\\" in study_id:
        raise ParseReconciliationError("STUDY_ID_INVALID")
    return project / ROOT / study_id / "registry.json"


def _validate(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParseReconciliationError("PARSE_RECONCILIATION_INVALID")
    try:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParseReconciliationError("PARSE_RECONCILIATION_SCHEMA_INVALID") from exc
    if list(Draft202012Validator(schema).iter_errors(value)):
        raise ParseReconciliationError("PARSE_RECONCILIATION_INVALID")
    body = {key: item for key, item in value.items() if key != "registry_digest"}
    if canonical_digest(body) != value["registry_digest"]:
        raise ParseReconciliationError("PARSE_RECONCILIATION_INVALID")
    return value


def _atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _candidate(row: dict[str, Any]) -> dict[str, str | None]:
    if not isinstance(row, dict) or {
        "smiles_expanded",
        "smiles_unexpanded",
        "unresolved_field_count",
    } & set(row):
        raise ParseReconciliationError("PARSE_RECONCILIATION_CONTRACT_INVALID")
    resolved = row.get("resolved_smiles")
    if resolved == "":
        resolved = None
    elif resolved is not None and (
        not isinstance(resolved, str) or resolved != resolved.strip()
    ):
        raise ParseReconciliationError("PARSE_RECONCILIATION_CONTRACT_INVALID")
    name = row.get("mol_idt")
    return {
        "mol_idt": name if isinstance(name, str) and name else None,
        "resolved_smiles": resolved,
    }


def _workflow(objects: list[dict[str, Any]]) -> bool:
    return all(row["status"] not in UNRESOLVED or isinstance(row.get("decision"), dict) for row in objects)


def _seal(body: dict[str, Any]) -> dict[str, Any]:
    body["workflow_can_continue"] = _workflow(body["objects"])
    return _validate({**body, "registry_digest": canonical_digest(body)})


def build_parse_reconciliation(project: Path, study_id: str) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        dual_digest = require_dual_source_ready(root, study_id, requires_chemical=True)
        completion_digest = require_honest_progressive_projection(
            root, study_id, allow_provisional=True
        )
        bundle = load_source_truth_bundle(root, study_id)
        chemical_state = chemical_paper_projection(root)
    except (DualSourceError, ChemicalCompletionError, SourceTruthError, ChemicalPaperError) as exc:
        raise ParseReconciliationError(exc.code) from exc
    main = [row for row in bundle["sources"] if row.get("document_role") == "MAIN"]
    if len(main) != 1:
        raise ParseReconciliationError("MAIN_SOURCE_INVALID")
    source = main[0]
    try:
        content = json.loads((root / source["content_list"]["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParseReconciliationError("GENERIC_CONTENT_LIST_INVALID") from exc
    generic_rows = [row for row in content if isinstance(row, dict) and row.get("type") in {"molecule", "chemical_molecule"}]
    chemical_matches = [row for row in chemical_state["studies"] if row["study_id"] == study_id]
    if len(chemical_matches) != 1:
        raise ParseReconciliationError("CHEMICAL_PAPER_NOT_IMPORTED")
    objects: list[dict[str, Any]] = []
    for index, chemical in enumerate(chemical_matches[0]["molecules"]):
        page = chemical["page"]
        page_generic = [row for row in generic_rows if row.get("page_idx") == page - 1]
        raw_generic = page_generic[index] if index < len(page_generic) else None
        generic_candidate = _candidate(raw_generic) if isinstance(raw_generic, dict) else None
        chemical_candidate = _candidate(chemical)
        if generic_candidate is None:
            status = "complementary"
        elif generic_candidate == chemical_candidate:
            status = "corroborated"
        else:
            status = "conflict"
        object_id = f"molecule-{index}"
        object_body = {
            "object_id": object_id, "kind": "molecule", "source_id": source["source_id"],
            "page": page, "generic_candidate": generic_candidate,
            "chemical_candidate": chemical_candidate, "status": status,
        }
        objects.append({
            **object_body,
            "object_digest": canonical_digest({**object_body, "dual_source_binding_digest": dual_digest}),
            "decision": None, "prior_decisions": [],
        })
    body = {
        "schema_version": "parse-reconciliation.v2", "project_id": root.name,
        "study_id": study_id, "dual_source_binding_digest": dual_digest,
        "chemical_completion_digest": completion_digest, "objects": objects,
    }
    return _seal(body)


def load_parse_reconciliation(project: Path, study_id: str) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        value = json.loads(_path(root, study_id).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ParseReconciliationError("PARSE_RECONCILIATION_MISSING") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParseReconciliationError("PARSE_RECONCILIATION_INVALID") from exc
    return _validate(value)


def write_parse_reconciliation(project: Path, study_id: str) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        with project_write_lock(root):
            fresh = build_parse_reconciliation(root, study_id)
            path = _path(root, study_id)
            if path.is_file():
                previous = load_parse_reconciliation(root, study_id)
                previous_by_id = {row["object_id"]: row for row in previous["objects"]}
                for row in fresh["objects"]:
                    prior = previous_by_id.get(row["object_id"])
                    if not isinstance(prior, dict):
                        continue
                    history = copy.deepcopy(prior.get("prior_decisions", []))
                    decision = prior.get("decision")
                    if isinstance(decision, dict) and decision.get("bound_object_digest") == row["object_digest"]:
                        row["decision"] = copy.deepcopy(decision)
                    elif isinstance(decision, dict):
                        history.append(copy.deepcopy(decision))
                    row["prior_decisions"] = history
                fresh = _seal({key: value for key, value in fresh.items() if key not in {"registry_digest", "workflow_can_continue"}})
            _atomic(path, fresh)
    except PaperEvidenceStoreError as exc:
        raise ParseReconciliationError(exc.code) from exc
    return fresh


def _decision(payload: object, target: dict[str, Any], page_count: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"object_id", "registry_digest", "action", "selected_lane", "note", "pdf_locator", "actor_type", "actor_label"}:
        raise ParseReconciliationError("RECONCILIATION_DECISION_INVALID")
    action, lane = payload.get("action"), payload.get("selected_lane")
    if action not in {"pdf_resolved", "pdf_locator_only", "reject_both"}:
        raise ParseReconciliationError("RECONCILIATION_DECISION_INVALID")
    if action == "pdf_resolved" and lane not in {"generic", "chemical"}:
        raise ParseReconciliationError("RECONCILIATION_SELECTED_LANE_REQUIRED")
    if action != "pdf_resolved" and lane is not None:
        raise ParseReconciliationError("RECONCILIATION_SELECTED_LANE_INVALID")
    note = payload.get("note")
    actor_type, actor_label = payload.get("actor_type"), payload.get("actor_label")
    locator = payload.get("pdf_locator")
    if not isinstance(note, str) or not note.strip() or note != note.strip():
        raise ParseReconciliationError("RECONCILIATION_NOTE_REQUIRED")
    if actor_type not in {"human_researcher", "simulated_researcher_agent"} or not isinstance(actor_label, str) or not actor_label.strip():
        raise ParseReconciliationError("RECONCILIATION_ACTOR_INVALID")
    if not isinstance(locator, dict) or set(locator) - {"page", "figure_label", "bbox"}:
        raise ParseReconciliationError("PDF_LOCATOR_INVALID")
    page = locator.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= page_count:
        raise ParseReconciliationError("PDF_LOCATOR_INVALID")
    return {
        "action": action, "selected_lane": lane, "note": note,
        "pdf_locator": copy.deepcopy(locator), "actor_type": actor_type,
        "actor_label": actor_label.strip(), "recorded_at": _now(),
        "bound_object_digest": target["object_digest"],
    }


def apply_reconciliation_decision(project: Path, study_id: str, payload: object) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        with project_write_lock(root):
            registry = load_parse_reconciliation(root, study_id)
            if not isinstance(payload, dict) or payload.get("registry_digest") != registry["registry_digest"]:
                raise ParseReconciliationError("STALE_RECONCILIATION_REGISTRY")
            matches = [row for row in registry["objects"] if row["object_id"] == payload.get("object_id")]
            if len(matches) != 1:
                raise ParseReconciliationError("RECONCILIATION_OBJECT_NOT_FOUND")
            bundle = load_source_truth_bundle(root, study_id)
            page_count = max(source["page_count"] for source in bundle["sources"])
            matches[0]["decision"] = _decision(payload, matches[0], page_count)
            registry = _seal({key: value for key, value in registry.items() if key not in {"registry_digest", "workflow_can_continue"}})
            _atomic(_path(root, study_id), registry)
    except PaperEvidenceStoreError as exc:
        raise ParseReconciliationError(exc.code) from exc
    except SourceTruthError as exc:
        raise ParseReconciliationError(exc.code) from exc
    return registry


def require_reconciliation_ready(project: Path, study_id: str) -> str:
    saved = load_parse_reconciliation(project, study_id)
    fresh = build_parse_reconciliation(project, study_id)
    if (
        saved["dual_source_binding_digest"] != fresh["dual_source_binding_digest"]
        or saved["chemical_completion_digest"] != fresh["chemical_completion_digest"]
        or [(row["object_id"], row["object_digest"]) for row in saved["objects"]]
        != [(row["object_id"], row["object_digest"]) for row in fresh["objects"]]
    ):
        raise ParseReconciliationError("PARSE_RECONCILIATION_STALE")
    if not saved["workflow_can_continue"]:
        raise ParseReconciliationError("PARSE_RECONCILIATION_UNRESOLVED")
    return str(saved["registry_digest"])


def project_reconciliation_state(project: Path) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        studies = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ParseReconciliationError(exc.code) from exc
    rows = []
    for study_id in studies:
        try:
            registry = load_parse_reconciliation(root, study_id)
            require_reconciliation_ready(root, study_id)
            rows.append({
                "study_id": study_id,
                "status": "current",
                "object_count": len(registry["objects"]),
                "registry_digest": registry["registry_digest"],
            })
        except ParseReconciliationError as exc:
            rows.append({"study_id": study_id, "status": "blocked", "reason_code": exc.code})
    return {"schema_version": "parse-reconciliation-project-state.v2", "studies": rows, "workflow_can_continue": bool(rows) and all(row["status"] == "current" for row in rows)}
