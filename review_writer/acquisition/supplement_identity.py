"""Pure DOI supplement identity parsing and non-mutating manifest audits."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .manifest_identity import DOI_RE as _DOI_RE
from .manifest_identity import ManifestIdentityError, normalize_doi, validate_acquisition_row

_SUPPLEMENT_RE = re.compile(r"^(?P<parent>.+)\.(?P<suffix>s\d+|supp\d*)$", re.IGNORECASE)


class SupplementAuditError(ValueError):
    """The supplement audit inputs are structurally invalid."""


SOURCE_COVERAGE_ARTIFACT = "00_sources/source_coverage.json"
SI_POLICIES = frozenset({"REQUIRED", "RECOMMENDED", "NOT_REQUIRED"})


def audit_source_coverage(
    *,
    study_id: str,
    available_roles: list[str],
    si_policy: str,
    si_dependent_claim_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Apply the pure MAIN/SI availability policy for one study."""

    if not isinstance(study_id, str) or not study_id.strip():
        raise SupplementAuditError("study_id must be nonempty")
    if (
        not isinstance(available_roles, list)
        or any(role not in {"MAIN", "SI"} for role in available_roles)
        or len(set(available_roles)) != len(available_roles)
    ):
        raise SupplementAuditError("available_roles must contain unique MAIN/SI values")
    if si_policy not in SI_POLICIES:
        raise SupplementAuditError("si_policy must be REQUIRED, RECOMMENDED, or NOT_REQUIRED")
    claim_ids = [] if si_dependent_claim_ids is None else si_dependent_claim_ids
    if (
        not isinstance(claim_ids, list)
        or any(not isinstance(claim_id, str) or not claim_id.strip() for claim_id in claim_ids)
        or len(set(claim_ids)) != len(claim_ids)
    ):
        raise SupplementAuditError("si_dependent_claim_ids must contain unique nonempty strings")

    roles = set(available_roles)
    blocking_reasons: list[str] = []
    blocked_claim_ids: list[str] = []
    limitations: list[str] = []
    if "MAIN" not in roles:
        study_status = "BLOCKED"
        blocking_reasons.append("MAIN_REQUIRED")
    elif si_policy == "REQUIRED" and "SI" not in roles:
        study_status = "PARTIAL"
        blocked_claim_ids = list(claim_ids)
        blocking_reasons.append("SI_REQUIRED_FOR_DECLARED_CLAIMS")
    elif si_policy == "RECOMMENDED" and "SI" not in roles:
        study_status = "READY_WITH_LIMITATION"
        limitations.append("SI_RECOMMENDED_NOT_AVAILABLE")
    else:
        study_status = "READY"

    return {
        "schema_version": "source-coverage.v1",
        "canonical_artifact": SOURCE_COVERAGE_ARTIFACT,
        "study_id": study_id.strip(),
        "main_policy": "MAIN_REQUIRED",
        "si_policy": si_policy,
        "available_roles": sorted(roles),
        "study_status": study_status,
        "blocked_claim_ids": blocked_claim_ids,
        "blocking_reasons": blocking_reasons,
        "limitations": limitations,
    }


def supplement_parent_relation(
    value: str | None, *, publisher_confirmed_parent_doi: str | None = None
) -> dict[str, str | None]:
    """Classify only terminal supplement suffixes; confirmation requires outside evidence."""

    doi = normalize_doi(value)
    if not doi:
        return {
            "normalized_doi": None,
            "candidate_parent_doi": None,
            "confirmed_parent_doi": None,
            "relation_status": "NOT_A_VALID_TERMINAL_SUPPLEMENT_DOI",
        }
    match = _SUPPLEMENT_RE.fullmatch(doi)
    if not match or not _DOI_RE.fullmatch(match["parent"]):
        return {
            "normalized_doi": doi,
            "candidate_parent_doi": None,
            "confirmed_parent_doi": None,
            "relation_status": "NOT_A_TERMINAL_SUPPLEMENT_SUFFIX",
        }
    parent = match["parent"].lower()
    confirmed = normalize_doi(publisher_confirmed_parent_doi)
    is_confirmed = confirmed == parent
    return {
        "normalized_doi": doi,
        "candidate_parent_doi": parent,
        "confirmed_parent_doi": parent if is_confirmed else None,
        "relation_status": "PUBLISHER_CONFIRMED_PARENT" if is_confirmed else "PARENT_CANDIDATE_STRING_DERIVED",
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_input_basename(path: Path, fallback: str) -> str:
    name = path.name
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        return fallback
    return name


def _validated_identity_record(record: Any, *, id_field: str, source: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SupplementAuditError(f"{source} records must be JSON objects")
    stable_id = record.get(id_field)
    if not isinstance(stable_id, str) or not stable_id.strip():
        raise SupplementAuditError(f"{source} records require a nonempty {id_field}")
    doi = record.get("doi")
    if doi is not None and not isinstance(doi, str):
        raise SupplementAuditError(f"{source} records require a string or null doi")
    if isinstance(doi, str) and normalize_doi(doi) is None:
        raise SupplementAuditError(f"{source} records contain an invalid doi")
    confirmation = record.get("publisher_confirmed_parent_doi")
    if confirmation is not None and (not isinstance(confirmation, str) or normalize_doi(confirmation) is None):
        raise SupplementAuditError(f"{source} records contain an invalid publisher confirmation")
    validated = dict(record)
    validated[id_field] = stable_id.strip()
    if isinstance(confirmation, str):
        validated["publisher_confirmed_parent_doi"] = normalize_doi(confirmation)
    return validated


def _load_candidate_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SupplementAuditError(f"candidate pool line {line_number} is not valid JSON") from exc
            record = _validated_identity_record(parsed, id_field="candidate_id", source="candidate pool")
            if record["candidate_id"] in seen:
                raise SupplementAuditError("candidate_id values must be unique")
            seen.add(record["candidate_id"])
            records.append(record)
    return records


def _load_acquisition_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SupplementAuditError("acquisition manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "public-corpus-acquisition.v1" or not isinstance(manifest.get("downloads"), list):
        raise SupplementAuditError("acquisition manifest must use public-corpus-acquisition.v1 with a downloads list")
    downloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for parsed in manifest["downloads"]:
        try:
            download = validate_acquisition_row(parsed)
        except ManifestIdentityError as exc:
            raise SupplementAuditError("acquisition manifest contains an invalid row") from exc
        if download["download_id"] in seen:
            raise SupplementAuditError("download_id values must be unique")
        seen.add(download["download_id"])
        downloads.append(download)
    return downloads


def audit_supplement_reports(candidate_pool: Path | str, acquisition_manifest: Path | str) -> dict[str, Any]:
    """Inventory suffix reports without changing either frozen input."""

    pool_path, manifest_path = Path(candidate_pool), Path(acquisition_manifest)
    candidates = _load_candidate_records(pool_path)
    downloads = _load_acquisition_manifest(manifest_path)
    rows: list[dict[str, Any]] = []
    for record in candidates:
        relation = supplement_parent_relation(record.get("doi"), publisher_confirmed_parent_doi=record.get("publisher_confirmed_parent_doi"))
        if relation["candidate_parent_doi"]:
            rows.append({
                "source": "candidate_pool",
                "stable_identity": record["candidate_id"],
                "candidate_id": record["candidate_id"],
                "doi": relation["normalized_doi"],
                "report_role": "SUPPLEMENTARY_REPORT",
                "study_count_role": "NOT_AN_INDEPENDENT_STUDY" if relation["relation_status"] == "PUBLISHER_CONFIRMED_PARENT" else "REQUIRES_REVIEW",
                "future_acquisition_document_role": "SI_OR_REPORT_ROLE_REQUIRED",
                **relation,
            })
    for download in downloads:
        relation = supplement_parent_relation(download.get("doi"), publisher_confirmed_parent_doi=download.get("publisher_confirmed_parent_doi"))
        if relation["candidate_parent_doi"]:
            rows.append({
                "source": "acquisition_manifest",
                "stable_identity": download["download_id"],
                "candidate_id": download.get("study_id"),
                "doi": relation["normalized_doi"],
                "frozen_document_role": download.get("document_role"),
                "report_role": "SUPPLEMENTARY_REPORT",
                "study_count_role": "NOT_AN_INDEPENDENT_STUDY" if relation["relation_status"] == "PUBLISHER_CONFIRMED_PARENT" else "REQUIRES_REVIEW",
                "future_acquisition_document_role": "SI_OR_REPORT_ROLE_REQUIRED",
                **relation,
            })
    counts = {
        "candidate_pool_suffix_reports": sum(row["source"] == "candidate_pool" for row in rows),
        "acquisition_manifest_suffix_reports": sum(row["source"] == "acquisition_manifest" for row in rows),
    }
    return {
        "schema_version": "supplement-parent-relation-audit.v1",
        "non_mutating": True,
        "inputs": {
            "candidate_pool": {
                "path": _safe_input_basename(pool_path, "candidate-pool"),
                "sha256": sha256_file(pool_path),
            },
            "acquisition_manifest": {
                "path": _safe_input_basename(manifest_path, "acquisition-manifest"),
                "sha256": sha256_file(manifest_path),
            },
        },
        "counts": counts,
        "records": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit terminal DOI supplement reports without mutating inputs.")
    parser.add_argument("--candidate-pool", required=True, type=Path)
    parser.add_argument("--acquisition-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit_supplement_reports(args.candidate_pool, args.acquisition_manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
