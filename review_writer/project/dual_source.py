"""Same-PDF currentness contract for Generic and Chemical parse lanes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from review_writer.project.chemical_paper import (
    ChemicalPaperError,
    chemical_paper_current_binding,
)
from review_writer.project.paper_evidence_store import PaperEvidenceStoreError, project_write_lock
from review_writer.project.parse_quality import ParseQualityError, require_parse_quality_current
from review_writer.project.source_truth import (
    SourceTruthError,
    build_source_truth_bundle,
    canonical_digest,
    declared_study_ids,
    load_source_truth_bundle,
    source_truth_asset,
    study_source_tier,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "schemas/evidence/dual_source_binding.v1.schema.json"
ROOT = Path("01_evidence/dual_source")


class DualSourceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _path(project: Path, study_id: str) -> Path:
    if not study_id or study_id in {".", ".."} or "/" in study_id or "\\" in study_id:
        raise DualSourceError("STUDY_ID_INVALID")
    return project / ROOT / study_id / "binding.json"


def _validate(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DualSourceError("DUAL_SOURCE_BINDING_INVALID")
    try:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualSourceError("DUAL_SOURCE_SCHEMA_INVALID") from exc
    if list(Draft202012Validator(schema).iter_errors(value)):
        raise DualSourceError("DUAL_SOURCE_BINDING_INVALID")
    body = {key: item for key, item in value.items() if key != "binding_digest"}
    if canonical_digest(body) != value["binding_digest"]:
        raise DualSourceError("DUAL_SOURCE_BINDING_INVALID")
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


def build_dual_source_binding(project: Path, study_id: str) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        tier = study_source_tier(root, study_id)
        bundle = load_source_truth_bundle(root, study_id)
        parse_digest = require_parse_quality_current(root, study_id)
    except (SourceTruthError, ParseQualityError) as exc:
        raise DualSourceError(exc.code) from exc
    main = [row for row in bundle["sources"] if row.get("document_role") == "MAIN"]
    if len(main) != 1:
        raise DualSourceError("MAIN_SOURCE_INVALID")
    generic = {
        "source_pdf_sha256": main[0]["pdf"]["sha256"],
        "source_truth_bundle_digest": bundle["bundle_digest"],
        "parse_gate_digest": parse_digest,
    }
    chemical: dict[str, str] | None
    try:
        chemical = chemical_paper_current_binding(root, study_id)
    except ChemicalPaperError as exc:
        if exc.code == "CHEMICAL_PAPER_NOT_IMPORTED":
            chemical = None
        else:
            raise DualSourceError(exc.code) from exc
    if tier == "core" and chemical is None:
        raise DualSourceError("CORE_CHEMICAL_IMPORT_REQUIRED")
    if chemical is not None and (
        chemical["source_pdf_sha256"] != generic["source_pdf_sha256"]
        or chemical["source_truth_bundle_digest"] != generic["source_truth_bundle_digest"]
    ):
        raise DualSourceError("DUAL_SOURCE_BINDING_MISMATCH")
    body: dict[str, Any] = {
        "schema_version": "dual-source-binding.v1", "project_id": root.name,
        "study_id": study_id, "source_id": main[0]["source_id"], "source_tier": tier,
        "generic": generic, "chemical": chemical,
        "reaction_data_status": "unavailable_not_provided",
        "status": "current" if chemical is not None else "current_generic_only",
    }
    binding = {**body, "binding_digest": canonical_digest(body)}
    return _validate(binding)


def write_dual_source_binding(project: Path, study_id: str) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        with project_write_lock(root):
            binding = build_dual_source_binding(root, study_id)
            _atomic(_path(root, study_id), binding)
    except PaperEvidenceStoreError as exc:
        raise DualSourceError(exc.code) from exc
    return binding


def load_dual_source_binding(project: Path, study_id: str) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    path = _path(root, study_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DualSourceError("DUAL_SOURCE_BINDING_MISSING") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualSourceError("DUAL_SOURCE_BINDING_INVALID") from exc
    return _validate(value)


def require_dual_source_ready(project: Path, study_id: str, *, requires_chemical: bool) -> str:
    current = build_dual_source_binding(project, study_id)
    saved = load_dual_source_binding(project, study_id)
    if current["binding_digest"] != saved["binding_digest"]:
        raise DualSourceError("DUAL_SOURCE_STALE")
    if requires_chemical and saved["chemical"] is None:
        raise DualSourceError("CHEMICAL_ENHANCEMENT_REQUIRED")
    return str(saved["binding_digest"])


def _source_availability(project: Path, study_id: str) -> dict[str, str]:
    """Project PDF and Generic currentness without authorizing dual-source use."""

    result = {"pdf_status": "unknown", "generic_parse_status": "unknown"}
    try:
        saved = load_source_truth_bundle(project, study_id)
    except SourceTruthError:
        return result
    main = [
        row for row in saved["sources"]
        if isinstance(row, dict) and row.get("document_role") == "MAIN"
    ]
    if len(main) != 1 or not isinstance(main[0].get("source_id"), str):
        return result
    try:
        source_truth_asset(
            project, study_id, str(main[0]["source_id"]), "pdf"
        )
    except SourceTruthError as exc:
        if exc.code == "SOURCE_ASSET_DRIFT":
            result["pdf_status"] = "stale"
        return result
    result["pdf_status"] = "verified"
    try:
        current = build_source_truth_bundle(project, study_id)
    except SourceTruthError:
        return result
    saved_body = {key: value for key, value in saved.items() if key != "bundle_digest"}
    current_body = {
        key: value for key, value in current.items() if key != "bundle_digest"
    }
    current_body["project_id"] = saved_body.get("project_id")
    result["generic_parse_status"] = (
        "current"
        if canonical_digest(current_body) == canonical_digest(saved_body)
        else "stale"
    )
    return result


def project_dual_source_state(project: Path) -> dict[str, object]:
    root = Path(project).resolve(strict=True)
    try:
        study_ids = declared_study_ids(root)
    except SourceTruthError as exc:
        raise DualSourceError(exc.code) from exc
    rows: list[dict[str, object]] = []
    for study_id in study_ids:
        row: dict[str, object] = {
            "study_id": study_id,
            **_source_availability(root, study_id),
        }
        try:
            tier = study_source_tier(root, study_id)
        except SourceTruthError as exc:
            rows.append({**row, "status": "blocked", "reason_code": exc.code})
            continue
        row.update({
            "source_tier": tier,
            "requires_chemical": tier == "core",
            "generic": {"status": row["generic_parse_status"]},
        })
        if (
            row["pdf_status"] != "verified"
            or row["generic_parse_status"] != "current"
        ):
            source_statuses = {
                row["pdf_status"], row["generic_parse_status"]
            }
            rows.append({
                **row,
                "status": "blocked",
                "reason_code": (
                    "SOURCE_AUTHORITY_STALE"
                    if "stale" in source_statuses
                    else "SOURCE_AUTHORITY_UNAVAILABLE"
                ),
            })
            continue
        try:
            binding = load_dual_source_binding(root, study_id)
            require_dual_source_ready(root, study_id, requires_chemical=binding["source_tier"] == "core")
            generic = binding["generic"]
            chemical = binding["chemical"]
            rows.append({
                **row,
                "status": binding["status"],
                "source_tier": binding["source_tier"],
                "requires_chemical": binding["source_tier"] == "core",
                "binding_digest": binding["binding_digest"],
                "generic_parse_status": "current",
                "generic": {
                    "status": "current",
                    "binding_digest": generic["parse_gate_digest"],
                },
                "chemical": (
                    {
                        "status": "current",
                        "state_digest": chemical["state_digest"],
                        "reaction_data_status": binding["reaction_data_status"],
                    }
                    if chemical is not None
                    else None
                ),
                "reaction_data_status": binding["reaction_data_status"],
            })
        except DualSourceError as exc:
            rows.append({**row, "status": "blocked", "reason_code": exc.code})
    return {
        "schema_version": "dual-source-project-state.v1", "studies": rows,
        "main_source_available_count": sum(
            row.get("pdf_status") == "verified" for row in rows
        ),
        "generic_source_available_count": sum(
            row.get("generic_parse_status") == "current" for row in rows
        ),
        "workflow_can_continue": bool(rows) and all(row["status"] in {"current", "current_generic_only"} for row in rows),
    }
