"""Candidate-only Chemical Completion staging and researcher-safe projection."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .chemical_paper import load_chemical_paper_state
from .source_truth import canonical_digest, load_source_truth_bundle


ROOT = Path("01_evidence/chemical_completion_candidates")
_ID = re.compile(r"^(?!\.\.?$)(?!.*[/\\\x00\r\n])\S{1,240}$")


class ChemicalCompletionCandidateError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_candidate(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    study_id = value.get("study_id")
    molecule_index = value.get("molecule_index")
    if not isinstance(study_id, str) or not _ID.fullmatch(study_id):
        return None
    if not isinstance(molecule_index, int) or isinstance(molecule_index, bool) or molecule_index < 0:
        return None
    if value.get("field") != "resolved_smiles":
        return None
    resolved = value.get("value")
    confidence = value.get("confidence")
    if not isinstance(resolved, str) or not resolved.strip() or resolved != resolved.strip():
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return None
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        return None
    locator = value.get("pdf_locator")
    if not isinstance(locator, dict) or not isinstance(locator.get("page"), int) or locator["page"] < 1:
        return None
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        return None
    # Candidate staging is researcher-visible; reject hidden paths, hashes and tokens.
    encoded = json.dumps({"provenance": provenance, "locator": locator, "reason": reason}, ensure_ascii=False)
    if re.search(r"(?i)(?:^|[\s=:/])(?:/|[a-z]:\\|https?://|file://|[0-9a-f]{64}|token|session|cookie)", encoded):
        return None
    return {
        "study_id": study_id,
        "molecule_index": molecule_index,
        "field": "resolved_smiles",
        "value": resolved,
        "confidence": float(confidence),
        "provenance": copy.deepcopy(provenance),
        "pdf_locator": copy.deepcopy(locator),
        "reason": reason,
    }


def _safe_set(value: object, study_id: str, expected: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or value.get("schema_version") != "chemical-completion-candidate-set.v1":
        return []
    if value.get("study_id") != study_id or not isinstance(value.get("candidates"), list):
        return []
    if any(value.get(key) != expected.get(key) for key in (
        "source_truth_bundle_digest",
        "chemical_import_digest",
        "blocked_molecule_indices_digest",
    )):
        return []
    rows = []
    for candidate in value["candidates"]:
        safe = _safe_candidate(candidate)
        if safe is not None and safe["study_id"] == study_id:
            rows.append(safe)
    return rows


def project_chemical_completion_candidates(project: Path, study_id: str) -> dict[int, list[dict[str, Any]]]:
    """Return only candidate values for still-missing rows; never a decision."""

    root = Path(project).resolve(strict=True)
    try:
        state = load_chemical_paper_state(root, study_id)
        from .chemical_completion import chemical_completion_state

        gate = chemical_completion_state(root, study_id)
        bundle = load_source_truth_bundle(root, study_id)
    except Exception:
        return {}
    missing = {
        row["molecule_index"]
        for row in gate.get("molecules", [])
        if row.get("resolved_smiles_status") == "BLOCKED"
    }
    expected = {
        "source_truth_bundle_digest": str(bundle.get("bundle_digest")),
        "chemical_import_digest": str(state["current_import_digest"]),
        "blocked_molecule_indices_digest": canonical_digest({
            "study_id": study_id,
            "blocked_molecule_indices": sorted(missing),
        }),
    }
    result: dict[int, list[dict[str, Any]]] = {}
    directory = root / ROOT / study_id
    if not directory.is_dir() or directory.is_symlink():
        return result
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for candidate in _safe_set(value, study_id, expected):
            index = candidate["molecule_index"]
            if index not in missing:
                continue
            bucket = result.setdefault(index, [])
            if candidate not in bucket:
                bucket.append(candidate)
    return result


def write_candidate_set(root: Path, payload: dict[str, Any]) -> Path:
    """Write a validated candidate set into a staging/project copy."""

    study_id = payload.get("study_id")
    result_digest = payload.get("result_digest")
    candidates = payload.get("candidates")
    if not isinstance(study_id, str) or not _ID.fullmatch(study_id):
        raise ChemicalCompletionCandidateError("CANDIDATE_STUDY_INVALID")
    if not isinstance(result_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", result_digest):
        raise ChemicalCompletionCandidateError("CANDIDATE_DIGEST_INVALID")
    if not isinstance(candidates, list) or not candidates:
        raise ChemicalCompletionCandidateError("CANDIDATE_SET_INVALID")
    safe_rows = [_safe_candidate(row) for row in candidates]
    if any(row is None for row in safe_rows):
        raise ChemicalCompletionCandidateError("CANDIDATE_SET_INVALID")
    target = root / ROOT / study_id / f"{result_digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    required_metadata = (
        "task_package_digest",
        "source_truth_bundle_digest",
        "chemical_import_digest",
        "blocked_molecule_indices_digest",
    )
    if any(
        not isinstance(payload.get(key), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload[key])
        for key in required_metadata
    ):
        raise ChemicalCompletionCandidateError("CANDIDATE_BINDING_INVALID")
    payload = {
        "schema_version": "chemical-completion-candidate-set.v1",
        "project_id": root.name,
        "study_id": study_id,
        "result_digest": result_digest,
        "agent_label": str(payload.get("agent_label") or "candidate-agent"),
        "task_package_digest": payload["task_package_digest"],
        "source_truth_bundle_digest": payload["source_truth_bundle_digest"],
        "chemical_import_digest": payload["chemical_import_digest"],
        "blocked_molecule_indices_digest": payload["blocked_molecule_indices_digest"],
        "candidates": safe_rows,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") != encoded:
        raise ChemicalCompletionCandidateError("CANDIDATE_SET_CONFLICT")
    target.write_text(encoded, encoding="utf-8")
    return target
