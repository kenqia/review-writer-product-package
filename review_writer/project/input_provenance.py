"""Atomic variable-N input and provenance gate.

This module owns only the coordinator-side input contract.  It binds the
declared MAIN PDF and current Generic Parse state to one SI file and one
Chemical Paper ZIP per study.  The public result deliberately contains no
operator paths or raw Chemical payloads; the paths are consumed only while a
formal preflight/import call is running.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from review_writer.acquisition.supplement_identity import audit_source_coverage
from review_writer.project.chemical_paper import (
    ChemicalPaperError,
    MAX_ARCHIVE_BYTES,
    load_chemical_paper_state,
)
from review_writer.delivery.dual_parse_release import (
    DualParseReleaseError,
    confirm_chemical_paper_import,
    preflight_chemical_paper_import,
)
from review_writer.project.parse_quality import (
    ParseQualityError,
    require_parse_quality_current,
)
from review_writer.project.source_truth import (
    SourceTruthError,
    acquisition_receipt_digest,
    build_source_truth_bundle,
    canonical_digest,
    declared_study_ids,
    load_source_truth_bundle,
    source_truth_asset,
    study_source_tier,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_SCHEMA = REPO_ROOT / "schemas/project/input_provenance_manifest.v1.schema.json"
LEGACY_MANIFEST_SCHEMA = REPO_ROOT / "schemas/project/legacy_input_provenance_manifest.v1.schema.json"
INPUT_MANIFEST_PATH = Path("00_sources/input_provenance_manifest.json")
SI_REGISTRY_PATH = Path("00_sources/si_resource_registry.json")
SOURCE_COVERAGE_PATH = Path("00_sources/source_coverage.json")
SI_DESTINATION_ROOT = Path("00_sources/supplements/imported")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INPUT_MAX_BYTES = MAX_ARCHIVE_BYTES
_PREFLIGHT_STAGE_SUFFIXES = (".zip", ".json", ".confirming.json", ".consumed.json", ".rejected.json")

MIN_CORPUS_STUDIES = 20
MAX_CORPUS_STUDIES = 40
LEGACY_STUDY_COUNT = 3
AUTHORITATIVE_VARIABLE_N = "authoritative_variable_n"
LEGACY_THREE_PAPER = "legacy_three_paper"


class InputProvenanceError(ValueError):
    """Stable fail-closed error for the formal input boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_corpus_study_count(value: object) -> int:
    """Validate the authoritative variable-N corpus size boundary."""

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not MIN_CORPUS_STUDIES <= value <= MAX_CORPUS_STUDIES
    ):
        raise InputProvenanceError("CORPUS_STUDY_COUNT_INVALID")
    return value


def corpus_input_counts(value: object) -> dict[str, int]:
    """Return dynamic counts for every required corpus input lane."""

    count = validate_corpus_study_count(value)
    return {
        "main_pdf": count,
        "si": count,
        "chemical_zip": count,
        "generic_parse": count,
    }


@dataclass(frozen=True)
class _ValidatedRow:
    safe: dict[str, Any]
    si_path: Path
    zip_path: Path


@dataclass(frozen=True)
class _ValidatedManifest:
    project_id: str
    manifest_digest: str
    rows: tuple[_ValidatedRow, ...]
    corpus_kind: str
    variable_n: bool
    study_count: int
    core_study_count: int
    acquisition_receipt_digest: str


@dataclass(frozen=True)
class _ProjectMarker:
    corpus_kind: str
    variable_n: bool
    study_count: int
    core_study_count: int
    acquisition_receipt_digest: str


def _project(project: Path) -> Path:
    supplied = Path(project)
    if supplied.is_symlink() or not supplied.is_dir():
        raise InputProvenanceError("PROJECT_INVALID")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise InputProvenanceError("PROJECT_INVALID") from exc


def _project_marker(root: Path) -> _ProjectMarker:
    receipt_path = root / "00_sources/acquisition_final_receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise InputProvenanceError("ACQUISITION_FINAL_RECEIPT_INVALID")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputProvenanceError("ACQUISITION_FINAL_RECEIPT_INVALID") from exc
    studies = receipt.get("studies") if isinstance(receipt, dict) else None
    if not isinstance(studies, list) or not all(isinstance(row, dict) for row in studies):
        raise InputProvenanceError("ACQUISITION_FINAL_RECEIPT_INVALID")
    study_count = len(studies)
    corpus_kind = receipt.get("corpus_kind")
    variable_n = receipt.get("variable_n")
    if receipt.get("study_count") != study_count:
        raise InputProvenanceError("ACQUISITION_FINAL_RECEIPT_INVALID")
    if corpus_kind == AUTHORITATIVE_VARIABLE_N and variable_n is True:
        validate_corpus_study_count(study_count)
    elif corpus_kind == LEGACY_THREE_PAPER and variable_n is False:
        if study_count != LEGACY_STUDY_COUNT:
            raise InputProvenanceError("INPUT_STUDY_SET_INCOMPLETE")
    else:
        raise InputProvenanceError("CORPUS_KIND_INVALID")
    core_study_count = sum(row.get("tier") == "core" for row in studies)
    if any(row.get("tier") not in {"core", "background"} for row in studies):
        raise InputProvenanceError("SOURCE_TIER_INVALID")
    try:
        receipt_digest = acquisition_receipt_digest(root)
    except SourceTruthError as exc:
        raise InputProvenanceError(exc.code) from exc
    return _ProjectMarker(
        corpus_kind=corpus_kind,
        variable_n=variable_n,
        study_count=study_count,
        core_study_count=core_study_count,
        acquisition_receipt_digest=receipt_digest,
    )


def _json_bytes(value: object) -> bytes:
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
        raise InputProvenanceError("INPUT_MANIFEST_INVALID") from exc


def _sha256(path: Path, code: str) -> tuple[str, int]:
    try:
        observed_stat = path.lstat()
    except OSError as exc:
        raise InputProvenanceError(code) from exc
    if not stat.S_ISREG(observed_stat.st_mode) or path.is_symlink():
        raise InputProvenanceError(code)
    if observed_stat.st_size > _INPUT_MAX_BYTES:
        raise InputProvenanceError("INPUT_FILE_TOO_LARGE")
    digest = hashlib.sha256()
    observed_size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                observed_size += len(chunk)
                if observed_size > _INPUT_MAX_BYTES:
                    raise InputProvenanceError("INPUT_FILE_TOO_LARGE")
                digest.update(chunk)
    except InputProvenanceError:
        raise
    except OSError as exc:
        raise InputProvenanceError(code) from exc
    if observed_size != observed_stat.st_size:
        raise InputProvenanceError("INPUT_FILE_STALE")
    return digest.hexdigest(), observed_size


def _regular_input(value: object, code: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise InputProvenanceError(code)
    path = Path(value)
    try:
        observed = path.lstat()
    except OSError as exc:
        raise InputProvenanceError(code) from exc
    if not stat.S_ISREG(observed.st_mode) or path.is_symlink():
        raise InputProvenanceError(code)
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise InputProvenanceError(code) from exc


def _validate_pdf_input(path: Path, expected: str, *, mismatch_code: str, missing_code: str) -> tuple[str, int]:
    observed, size_bytes = _sha256(path, missing_code)
    try:
        with path.open("rb") as handle:
            magic = handle.read(5)
    except OSError as exc:
        raise InputProvenanceError(missing_code) from exc
    if magic != b"%PDF-":
        raise InputProvenanceError("INPUT_PDF_INVALID")
    if observed != expected:
        raise InputProvenanceError(mismatch_code)
    return observed, size_bytes


def _validate_zip_input(path: Path, expected: str, *, mismatch_code: str) -> tuple[str, int]:
    observed, size_bytes = _sha256(path, "CHEMICAL_ZIP_MISSING")
    if observed != expected:
        raise InputProvenanceError(mismatch_code)
    if size_bytes > MAX_ARCHIVE_BYTES:
        raise InputProvenanceError("CHEMICAL_ZIP_SIZE_INVALID")
    return observed, size_bytes


def _schema_manifest(value: object, *, variable_n: bool) -> dict[str, Any]:
    try:
        schema_path = MANIFEST_SCHEMA if variable_n else LEGACY_MANIFEST_SCHEMA
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputProvenanceError("INPUT_MANIFEST_SCHEMA_INVALID") from exc
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors or not isinstance(value, dict):
        raise InputProvenanceError("INPUT_MANIFEST_INVALID")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _manifest_digest(value: dict[str, Any]) -> str:
    return canonical_digest(value)


def _translate_source_error(exc: SourceTruthError, *, stale: bool) -> InputProvenanceError:
    if stale and exc.code in {
        "SOURCE_PDF_HASH_MISMATCH",
        "SOURCE_ASSET_DRIFT",
        "SOURCE_ASSET_INVALID",
        "SOURCE_TRUTH_DIGEST_MISMATCH",
    }:
        return InputProvenanceError("SOURCE_PDF_STALE")
    return InputProvenanceError(exc.code)


def _translate_parse_error(exc: ParseQualityError, *, stale: bool) -> InputProvenanceError:
    if stale and exc.code in {"PARSE_QUALITY_STALE", "SOURCE_TRUTH_MISSING"}:
        return InputProvenanceError("GENERIC_PARSE_STALE")
    return InputProvenanceError(exc.code)


def _validate_variable_si_source(
    root: Path,
    study_id: str,
    bundle: dict[str, Any],
    expected: dict[str, Any],
    *,
    stale: bool,
) -> None:
    """Require one current SI Source Truth row for the authoritative corpus."""
    si_rows = [
        source
        for source in bundle.get("sources", [])
        if isinstance(source, dict) and source.get("document_role") == "SI"
    ]
    if len(si_rows) != 1:
        raise InputProvenanceError("SI_SOURCE_MISSING" if not si_rows else "SI_SOURCE_AMBIGUOUS")
    si_source = si_rows[0]
    si_pdf = si_source.get("pdf")
    if (
        not isinstance(si_pdf, dict)
        or si_pdf.get("sha256") != expected.get("sha256")
        or si_source.get("page_count") != expected.get("page_count")
    ):
        raise InputProvenanceError("SI_SOURCE_STALE" if stale else "SI_SOURCE_BINDING_MISMATCH")
    si_source_id = si_source.get("source_id")
    if not isinstance(si_source_id, str) or not si_source_id:
        raise InputProvenanceError("SI_SOURCE_INVALID")
    try:
        source_truth_asset(root, study_id, si_source_id, "pdf")
    except SourceTruthError as exc:
        raise InputProvenanceError("SI_SOURCE_STALE" if stale else exc.code) from exc


def _safe_destination(root: Path, source_id: str) -> Path:
    if not _TOKEN_RE.fullmatch(source_id):
        raise InputProvenanceError("INPUT_SOURCE_ID_INVALID")
    return root / SI_DESTINATION_ROOT / f"{source_id}.pdf"


def _validate_manifest(
    project: Path,
    value: object,
    *,
    stale: bool,
    contract: str | None = None,
) -> _ValidatedManifest:
    root = _project(project)
    marker = _project_marker(root)
    if contract == "variable_n" and not marker.variable_n:
        raise InputProvenanceError("CORPUS_KIND_MISMATCH")
    if contract == "legacy" and marker.variable_n:
        raise InputProvenanceError("CORPUS_KIND_MISMATCH")
    if marker.variable_n and isinstance(value, dict) and isinstance(value.get("studies"), list):
        validate_corpus_study_count(len(value["studies"]))
    manifest = _schema_manifest(value, variable_n=marker.variable_n)
    if manifest.get("project_id") != root.name:
        raise InputProvenanceError("INPUT_PROJECT_MISMATCH")
    declared: list[str]
    try:
        declared = declared_study_ids(root)
    except SourceTruthError as exc:
        raise _translate_source_error(exc, stale=stale) from exc
    if len(declared) != marker.study_count:
        raise InputProvenanceError("INPUT_STUDY_SET_INCOMPLETE")
    studies = manifest.get("studies")
    if not isinstance(studies, list) or len(studies) != marker.study_count:
        raise InputProvenanceError("INPUT_STUDY_SET_INCOMPLETE")
    study_ids = [row.get("study_id") for row in studies if isinstance(row, dict)]
    source_ids = [row.get("source_id") for row in studies if isinstance(row, dict)]
    if (
        len(set(study_ids)) != marker.study_count
        or len(set(source_ids)) != marker.study_count
    ):
        raise InputProvenanceError("INPUT_BINDING_AMBIGUOUS")
    if set(study_ids) != set(declared):
        raise InputProvenanceError("INPUT_BINDING_MISMATCH")

    rows: list[_ValidatedRow] = []
    seen_si_paths: set[Path] = set()
    seen_zip_paths: set[Path] = set()
    for row in sorted(studies, key=lambda item: item["study_id"]):
        study_id = row["study_id"]
        source_id = row["source_id"]
        try:
            bundle = load_source_truth_bundle(root, study_id)
        except SourceTruthError as exc:
            raise _translate_source_error(exc, stale=stale) from exc
        sources = bundle.get("sources")
        main_rows = [
            source
            for source in sources
            if isinstance(source, dict) and source.get("document_role") == "MAIN"
        ] if isinstance(sources, list) else []
        if len(main_rows) != 1:
            raise InputProvenanceError("MAIN_SOURCE_AMBIGUOUS")
        main = main_rows[0]
        if main.get("source_id") != source_id:
            raise InputProvenanceError("INPUT_BINDING_MISMATCH")
        pdf = main.get("pdf")
        main_page_count = main.get("page_count")
        expected_main = row["main_pdf"]
        if not isinstance(pdf, dict) or pdf.get("sha256") != expected_main["sha256"]:
            raise InputProvenanceError("SOURCE_PDF_HASH_MISMATCH")
        if main_page_count != expected_main["page_count"]:
            raise InputProvenanceError("MAIN_PDF_PAGE_MISMATCH")
        si = row["si"]
        si_path = _regular_input(si["input_path"], "SI_INPUT_MISSING")
        if si_path in seen_si_paths:
            raise InputProvenanceError("INPUT_BINDING_AMBIGUOUS")
        seen_si_paths.add(si_path)
        _validate_pdf_input(
            si_path,
            si["sha256"],
            mismatch_code="SI_INPUT_STALE" if stale else "SI_HASH_MISMATCH",
            missing_code="SI_INPUT_MISSING",
        )
        try:
            source_truth_asset(root, study_id, source_id, "pdf")
            current_bundle = build_source_truth_bundle(root, study_id)
        except SourceTruthError as exc:
            raise _translate_source_error(exc, stale=stale) from exc
        if current_bundle.get("bundle_digest") != bundle.get("bundle_digest"):
            raise InputProvenanceError("GENERIC_PARSE_STALE")
        if marker.variable_n:
            _validate_variable_si_source(
                root,
                study_id,
                bundle,
                si,
                stale=stale,
            )
        try:
            parse_gate_digest = require_parse_quality_current(root, study_id)
        except ParseQualityError as exc:
            raise _translate_parse_error(exc, stale=stale) from exc

        chemical = row["chemical_zip"]
        zip_path = _regular_input(chemical["input_path"], "CHEMICAL_ZIP_MISSING")
        if zip_path in seen_zip_paths:
            raise InputProvenanceError("INPUT_BINDING_AMBIGUOUS")
        seen_zip_paths.add(zip_path)
        _validate_zip_input(
            zip_path,
            chemical["sha256"],
            mismatch_code="CHEMICAL_ZIP_STALE" if stale else "CHEMICAL_ZIP_HASH_MISMATCH",
        )
        destination = _safe_destination(root, source_id)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise InputProvenanceError("SI_TARGET_INVALID")
            destination_hash, _ = _sha256(destination, "SI_TARGET_INVALID")
            if destination_hash != si["sha256"]:
                raise InputProvenanceError("SI_TARGET_CONFLICT")

        try:
            tier = study_source_tier(root, study_id)
        except SourceTruthError as exc:
            raise InputProvenanceError(exc.code) from exc
        rows.append(
            _ValidatedRow(
                safe={
                    "study_id": study_id,
                    "source_id": source_id,
                    "tier": tier,
                    "main_pdf": {
                        "sha256": expected_main["sha256"],
                        "page_count": main_page_count,
                        "source_truth_bundle_digest": bundle["bundle_digest"],
                    },
                    "generic_parse": {
                        "status": "current",
                        "parse_gate_digest": parse_gate_digest,
                        "source_truth_bundle_digest": bundle["bundle_digest"],
                    },
                    "si": {
                        "path": destination.relative_to(root).as_posix(),
                        "sha256": si["sha256"],
                        "page_count": si["page_count"],
                        "size_bytes": si_path.stat().st_size,
                        "status": "current",
                    },
                    "chemical_zip": {
                        "sha256": chemical["sha256"],
                        "page_count": chemical["page_count"],
                        "status": "declared",
                    },
                },
                si_path=si_path,
                zip_path=zip_path,
            )
        )
    return _ValidatedManifest(
        project_id=root.name,
        manifest_digest=_manifest_digest(manifest),
        rows=tuple(rows),
        corpus_kind=marker.corpus_kind,
        variable_n=marker.variable_n,
        study_count=marker.study_count,
        core_study_count=marker.core_study_count,
        acquisition_receipt_digest=marker.acquisition_receipt_digest,
    )


def _counts_for_marker(
    *, study_count: int, variable_n: bool, core_study_count: int
) -> dict[str, int]:
    counts = (
        corpus_input_counts(study_count)
        if variable_n
        else {
            "main_pdf": study_count,
            "si": study_count,
            "chemical_zip": study_count,
            "generic_parse": study_count,
        }
    )
    if variable_n:
        counts.update(
            {
                "generic_main": study_count,
                "generic_si": study_count,
                "chemical_main": study_count,
                "chemical_core_si": core_study_count,
            }
        )
    return counts


def _counts(validated: _ValidatedManifest) -> dict[str, int]:
    return _counts_for_marker(
        study_count=validated.study_count,
        variable_n=validated.variable_n,
        core_study_count=validated.core_study_count,
    )


def _public_preflight(validated: _ValidatedManifest) -> dict[str, Any]:
    return {
        "status": "ready_for_import",
        "project_id": validated.project_id,
        "manifest_digest": validated.manifest_digest,
        "corpus_kind": validated.corpus_kind,
        "variable_n": validated.variable_n,
        "study_count": validated.study_count,
        "counts": _counts(validated),
        "bindings": [row.safe for row in validated.rows],
    }


def _cleanup_preflight_token(root: Path, token: str) -> None:
    try:
        key = hashlib.sha256(token.encode("ascii")).hexdigest()
    except (UnicodeEncodeError, AttributeError) as exc:
        raise InputProvenanceError("INPUT_ROLLBACK_FAILED") from exc
    stage = root / ".dual-parse-staging/chemical-paper"
    try:
        for suffix in _PREFLIGHT_STAGE_SUFFIXES:
            path = stage / f"{key}{suffix}"
            if path.is_symlink() or path.is_dir():
                raise InputProvenanceError("INPUT_ROLLBACK_FAILED")
            path.unlink(missing_ok=True)
        for directory in (stage, stage.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
    except InputProvenanceError:
        raise
    except OSError as exc:
        raise InputProvenanceError("INPUT_ROLLBACK_FAILED") from exc


def _chemical_preflight(root: Path, row: _ValidatedRow) -> dict[str, Any]:
    try:
        archive_bytes = row.zip_path.read_bytes()
    except OSError as exc:
        raise InputProvenanceError("CHEMICAL_ZIP_MISSING") from exc
    if hashlib.sha256(archive_bytes).hexdigest() != row.safe["chemical_zip"]["sha256"]:
        raise InputProvenanceError("CHEMICAL_ZIP_STALE")
    try:
        result = preflight_chemical_paper_import(
            root,
            row.safe["study_id"],
            archive_bytes,
        )
    except DualParseReleaseError as exc:
        raise InputProvenanceError(exc.code) from exc
    if (
        result.get("status") != "ready_for_confirmation"
        or result.get("study_id") != row.safe["study_id"]
        or not isinstance(result.get("preflight_token"), str)
        or result.get("page_count") != row.safe["chemical_zip"]["page_count"]
        or not isinstance(result.get("molecule_count"), int)
        or result.get("molecule_count") < 0
        or not isinstance(result.get("backend"), str)
        or not isinstance(result.get("version"), str)
    ):
        raise InputProvenanceError("CHEMICAL_ZIP_INVALID")
    return result


def _preflight_inputs(
    project: Path,
    manifest: object,
    *,
    contract: str,
) -> dict[str, Any]:
    """Validate every declared MAIN, SI, Generic, and Chemical binding.

    Preflight publishes no authoritative project state.  The public Chemical
    preflight endpoint may create non-authoritative staging briefly; every
    token created here is removed before this function returns or fails.
    """

    root = _project(project)
    validated = _validate_manifest(root, manifest, stale=False, contract=contract)
    tokens: list[str] = []
    try:
        for row in validated.rows:
            result = _chemical_preflight(root, row)
            tokens.append(result["preflight_token"])
        return _public_preflight(validated)
    finally:
        for token in tokens:
            _cleanup_preflight_token(root, token)


def preflight_corpus_inputs(project: Path, manifest: object) -> dict[str, Any]:
    """Validate an authoritative 20–40 study variable-N input manifest."""

    return _preflight_inputs(project, manifest, contract="variable_n")


def preflight_three_paper_inputs(project: Path, manifest: object) -> dict[str, Any]:
    """Legacy compatibility wrapper for a marked three-paper project."""

    return _preflight_inputs(project, manifest, contract="legacy")


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError as exc:
        raise InputProvenanceError("INPUT_ROLLBACK_FAILED") from exc


def _restore_file(path: Path, before: bytes | None) -> None:
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before)
    except OSError as exc:
        raise InputProvenanceError("INPUT_ROLLBACK_FAILED") from exc


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise InputProvenanceError("INPUT_WRITE_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _artifact_body(
    validated: _ValidatedManifest,
    derived_refreshes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "input-provenance-manifest.v1",
        "canonical_artifact": INPUT_MANIFEST_PATH.as_posix(),
        "project_id": validated.project_id,
        "manifest_digest": validated.manifest_digest,
        "corpus_kind": validated.corpus_kind,
        "variable_n": validated.variable_n,
        "study_count": validated.study_count,
        "acquisition_receipt_digest": validated.acquisition_receipt_digest,
        "status": "CURRENT",
        "counts": _counts(validated),
        "derived_refreshes": derived_refreshes,
        "studies": [row.safe for row in validated.rows],
    }


def _registry_body(validated: _ValidatedManifest) -> dict[str, Any]:
    resources = []
    for row in validated.rows:
        si = row.safe["si"]
        resources.append(
            {
                "study_id": row.safe["study_id"],
                "source_id": row.safe["source_id"],
                "document_role": "SI",
                "main_pdf_sha256": row.safe["main_pdf"]["sha256"],
                "path": si["path"],
                "sha256": si["sha256"],
                "size_bytes": si["size_bytes"],
                "page_count": si["page_count"],
                "status": "CURRENT",
                "authority": "INPUT_PROVENANCE_ONLY",
            }
        )
    return {
        "schema_version": "si-resource-registry.v1",
        "canonical_artifact": SI_REGISTRY_PATH.as_posix(),
        "project_id": validated.project_id,
        "corpus_kind": validated.corpus_kind,
        "variable_n": validated.variable_n,
        "study_count": validated.study_count,
        "acquisition_receipt_digest": validated.acquisition_receipt_digest,
        "core_si_required": True,
        "raw_scientific_authority": "CANDIDATE_ONLY",
        "human_chemical_review_required_for_scientific_use": True,
        "integration_status": "CURRENT",
        "manifest_digest": validated.manifest_digest,
        "resources": resources,
    }


def _coverage_body(validated: _ValidatedManifest) -> dict[str, Any]:
    studies = []
    for row in validated.rows:
        coverage = audit_source_coverage(
            study_id=row.safe["study_id"],
            available_roles=["MAIN", "SI"],
            si_policy="REQUIRED",
        )
        coverage.update(
            {
                "main_pdf_sha256": row.safe["main_pdf"]["sha256"],
                "main_pdf_page_count": row.safe["main_pdf"]["page_count"],
                "si": row.safe["si"],
                "generic_parse": row.safe["generic_parse"],
                "chemical_zip": row.safe["chemical_zip"],
                "authority": "INPUT_PROVENANCE_CURRENT",
            }
        )
        studies.append(coverage)
    body: dict[str, Any] = {
        "schema_version": "source-coverage.v1",
        "canonical_artifact": SOURCE_COVERAGE_PATH.as_posix(),
        "project_id": validated.project_id,
        "manifest_digest": validated.manifest_digest,
        "corpus_kind": validated.corpus_kind,
        "variable_n": validated.variable_n,
        "study_count": validated.study_count,
        "acquisition_receipt_digest": validated.acquisition_receipt_digest,
        "studies": studies,
    }
    return body


def _with_digest(body: dict[str, Any], key: str) -> dict[str, Any]:
    return {**body, key: canonical_digest(body)}


def _load_artifact(root: Path, path: Path, schema_version: str, digest_key: str) -> dict[str, Any]:
    target = root / path
    if target.is_symlink() or not target.is_file():
        raise InputProvenanceError("INPUT_PROVENANCE_MISSING")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID") from exc
    if not isinstance(value, dict) or value.get("schema_version") != schema_version:
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
    digest = value.get(digest_key)
    body = {key: item for key, item in value.items() if key != digest_key}
    if not isinstance(digest, str) or digest != canonical_digest(body):
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
    return value


def _validate_imported_state(
    root: Path,
    validated: _ValidatedManifest,
) -> list[dict[str, Any]]:
    artifact = _load_artifact(root, INPUT_MANIFEST_PATH, "input-provenance-manifest.v1", "artifact_digest")
    registry = _load_artifact(root, SI_REGISTRY_PATH, "si-resource-registry.v1", "registry_digest")
    coverage = root / SOURCE_COVERAGE_PATH
    if coverage.is_symlink() or not coverage.is_file():
        raise InputProvenanceError("INPUT_PROVENANCE_MISSING")
    try:
        coverage_value = json.loads(coverage.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID") from exc
    if not isinstance(coverage_value, dict) or coverage_value.get("schema_version") != "source-coverage.v1":
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
    if artifact.get("manifest_digest") != validated.manifest_digest or registry.get("manifest_digest") != validated.manifest_digest:
        raise InputProvenanceError("INPUT_MANIFEST_STALE")
    if coverage_value.get("manifest_digest") != validated.manifest_digest:
        raise InputProvenanceError("INPUT_MANIFEST_STALE")
    if artifact.get("status") != "CURRENT" or registry.get("integration_status") != "CURRENT":
        raise InputProvenanceError("INPUT_PROVENANCE_STALE")
    for value in (artifact, registry, coverage_value):
        if (
            value.get("corpus_kind") != validated.corpus_kind
            or value.get("variable_n") is not validated.variable_n
            or value.get("study_count") != validated.study_count
            or value.get("acquisition_receipt_digest") != validated.acquisition_receipt_digest
        ):
            raise InputProvenanceError("INPUT_MANIFEST_STALE")
    if artifact.get("counts") != _counts(validated):
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
    saved_rows = artifact.get("studies")
    if saved_rows != [row.safe for row in validated.rows]:
        raise InputProvenanceError("INPUT_MANIFEST_STALE")
    expected_registry = _with_digest(_registry_body(validated), "registry_digest")
    if registry != expected_registry:
        raise InputProvenanceError("SI_REGISTRY_INVALID")
    expected_coverage = _coverage_body(validated)
    if coverage_value != expected_coverage:
        raise InputProvenanceError("SOURCE_COVERAGE_INVALID")
    derived_refreshes = artifact.get("derived_refreshes")
    if (
        not isinstance(derived_refreshes, list)
        or len(derived_refreshes) != validated.study_count
        or sorted(
            row.get("study_id") for row in derived_refreshes if isinstance(row, dict)
        ) != sorted(row.safe["study_id"] for row in validated.rows)
    ):
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
    for refresh in derived_refreshes:
        if (
            not isinstance(refresh, dict)
            or refresh.get("status") not in {"current", "blocked", "needs_review", "failed"}
            or not isinstance(refresh.get("stage"), str)
        ):
            raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
    for row in validated.rows:
        si = row.safe["si"]
        destination = root / si["path"]
        observed, size_bytes = _sha256(destination, "SI_INPUT_STALE")
        if observed != si["sha256"] or size_bytes != si["size_bytes"]:
            raise InputProvenanceError("SI_INPUT_STALE")
        try:
            state = load_chemical_paper_state(root, row.safe["study_id"])
        except ChemicalPaperError as exc:
            raise InputProvenanceError("CHEMICAL_IMPORT_STALE") from exc
        event = state["imports"].get(state["current_import_digest"])
        if (
            not isinstance(event, dict)
            or event.get("archive_sha256") != row.safe["chemical_zip"]["sha256"]
            or event.get("source_pdf_sha256") != row.safe["main_pdf"]["sha256"]
            or event.get("page_count") != row.safe["chemical_zip"]["page_count"]
            or state.get("source_id") != row.safe["source_id"]
        ):
            raise InputProvenanceError("CHEMICAL_IMPORT_STALE")
    return derived_refreshes


def _published_validated_manifest(
    root: Path,
    artifact: dict[str, Any],
) -> _ValidatedManifest:
    marker = _project_marker(root)
    expected_counts = _counts_for_marker(
        study_count=marker.study_count,
        variable_n=marker.variable_n,
        core_study_count=marker.core_study_count,
    )
    if (
        artifact.get("project_id") != root.name
        or artifact.get("canonical_artifact") != INPUT_MANIFEST_PATH.as_posix()
        or artifact.get("status") != "CURRENT"
        or artifact.get("counts") != expected_counts
        or artifact.get("corpus_kind") != marker.corpus_kind
        or artifact.get("variable_n") is not marker.variable_n
        or artifact.get("study_count") != marker.study_count
        or artifact.get("acquisition_receipt_digest") != marker.acquisition_receipt_digest
    ):
        raise InputProvenanceError("INPUT_PROVENANCE_STALE")
    studies = artifact.get("studies")
    if not isinstance(studies, list) or len(studies) != marker.study_count:
        raise InputProvenanceError("INPUT_STUDY_SET_INCOMPLETE")
    try:
        declared = declared_study_ids(root)
    except SourceTruthError as exc:
        raise InputProvenanceError(exc.code) from exc
    if len(declared) != marker.study_count:
        raise InputProvenanceError("INPUT_STUDY_SET_INCOMPLETE")
    study_ids = [row.get("study_id") for row in studies if isinstance(row, dict)]
    source_ids = [row.get("source_id") for row in studies if isinstance(row, dict)]
    if (
        len(set(study_ids)) != marker.study_count
        or len(set(source_ids)) != marker.study_count
        or set(study_ids) != set(declared)
    ):
        raise InputProvenanceError("INPUT_BINDING_MISMATCH")

    rows: list[_ValidatedRow] = []
    for raw in sorted(studies, key=lambda item: item["study_id"]):
        if not isinstance(raw, dict):
            raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
        safe = json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
        study_id = safe.get("study_id")
        source_id = safe.get("source_id")
        main = safe.get("main_pdf")
        generic = safe.get("generic_parse")
        si = safe.get("si")
        chemical = safe.get("chemical_zip")
        if (
            not isinstance(study_id, str)
            or not isinstance(source_id, str)
            or not isinstance(main, dict)
            or not isinstance(generic, dict)
            or not isinstance(si, dict)
            or not isinstance(chemical, dict)
            or not _SHA256_RE.fullmatch(str(main.get("sha256", "")))
            or not _SHA256_RE.fullmatch(str(si.get("sha256", "")))
            or not _SHA256_RE.fullmatch(str(chemical.get("sha256", "")))
            or not isinstance(main.get("page_count"), int)
            or not isinstance(si.get("page_count"), int)
            or not isinstance(chemical.get("page_count"), int)
            or not isinstance(si.get("size_bytes"), int)
            or not isinstance(generic.get("parse_gate_digest"), str)
            or not _SHA256_RE.fullmatch(generic["parse_gate_digest"])
            or not isinstance(main.get("source_truth_bundle_digest"), str)
            or not _SHA256_RE.fullmatch(main["source_truth_bundle_digest"])
        ):
            raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
        try:
            bundle = load_source_truth_bundle(root, study_id)
            source_truth_asset(root, study_id, source_id, "pdf")
            current_bundle = build_source_truth_bundle(root, study_id)
            parse_gate_digest = require_parse_quality_current(root, study_id)
        except SourceTruthError as exc:
            raise InputProvenanceError("SOURCE_PDF_STALE") from exc
        except ParseQualityError as exc:
            raise _translate_parse_error(exc, stale=True) from exc
        main_rows = [
            row
            for row in bundle.get("sources", [])
            if isinstance(row, dict) and row.get("document_role") == "MAIN"
        ]
        if (
            len(main_rows) != 1
            or main_rows[0].get("source_id") != source_id
            or not isinstance(main_rows[0].get("pdf"), dict)
            or main_rows[0]["pdf"].get("sha256") != main["sha256"]
            or main_rows[0].get("page_count") != main["page_count"]
            or current_bundle.get("bundle_digest") != main["source_truth_bundle_digest"]
            or parse_gate_digest != generic["parse_gate_digest"]
            or generic.get("source_truth_bundle_digest") != main["source_truth_bundle_digest"]
        ):
            raise InputProvenanceError("GENERIC_PARSE_STALE")
        expected_si = _safe_destination(root, source_id)
        if marker.variable_n:
            _validate_variable_si_source(
                root,
                study_id,
                bundle,
                si,
                stale=True,
            )
        if si.get("path") != expected_si.relative_to(root).as_posix():
            raise InputProvenanceError("SI_REGISTRY_INVALID")
        observed, size_bytes = _sha256(expected_si, "SI_INPUT_STALE")
        if observed != si["sha256"] or size_bytes != si["size_bytes"]:
            raise InputProvenanceError("SI_INPUT_STALE")
        rows.append(
            _ValidatedRow(
                safe=safe,
                si_path=expected_si,
                zip_path=expected_si,
            )
        )
    manifest_digest = artifact.get("manifest_digest")
    if not isinstance(manifest_digest, str) or not _SHA256_RE.fullmatch(manifest_digest):
        raise InputProvenanceError("INPUT_PROVENANCE_INVALID")
    return _ValidatedManifest(
        project_id=root.name,
        manifest_digest=manifest_digest,
        rows=tuple(rows),
        corpus_kind=marker.corpus_kind,
        variable_n=marker.variable_n,
        study_count=marker.study_count,
        core_study_count=marker.core_study_count,
        acquisition_receipt_digest=marker.acquisition_receipt_digest,
    )


def project_input_provenance_state(project: Path) -> dict[str, Any]:
    """Return a redacted, read-only currentness projection for Dashboard callers."""

    root = _project(project)
    artifact = _load_artifact(
        root,
        INPUT_MANIFEST_PATH,
        "input-provenance-manifest.v1",
        "artifact_digest",
    )
    validated = _published_validated_manifest(root, artifact)
    derived_refreshes = _validate_imported_state(root, validated)
    return {
        "status": "current",
        "corpus_kind": validated.corpus_kind,
        "variable_n": validated.variable_n,
        "study_count": validated.study_count,
        "counts": _counts(validated),
        "bindings": [
            {
                "study_id": row.safe["study_id"],
                "source_id": row.safe["source_id"],
                "main_pdf": {
                    "status": "current",
                    "page_count": row.safe["main_pdf"]["page_count"],
                },
                "generic_parse": {"status": "current"},
                "si": {
                    "status": "current",
                    "page_count": row.safe["si"]["page_count"],
                },
                "chemical_zip": {
                    "status": "current",
                    "page_count": row.safe["chemical_zip"]["page_count"],
                },
            }
            for row in validated.rows
        ],
        "derived_refreshes": [
            {
                "study_id": refresh["study_id"],
                "status": refresh["status"],
                "stage": refresh["stage"],
                **(
                    {"reason_code": refresh["reason_code"]}
                    if isinstance(refresh.get("reason_code"), str)
                    else {}
                ),
            }
            for refresh in derived_refreshes
        ],
    }


def input_provenance_state(project: Path, manifest: object) -> dict[str, Any]:
    """Return currentness only after validating the persisted import receipt."""

    validated = _validate_manifest(project, manifest, stale=True)
    root = _project(project)
    derived_refreshes = _validate_imported_state(root, validated)
    return {
        "status": "current",
        "project_id": validated.project_id,
        "manifest_digest": validated.manifest_digest,
        "corpus_kind": validated.corpus_kind,
        "variable_n": validated.variable_n,
        "study_count": validated.study_count,
        "counts": _counts(validated),
        "bindings": [row.safe for row in validated.rows],
        "derived_refreshes": derived_refreshes,
    }


def _derived_refresh_record(result: dict[str, Any], study_id: str) -> dict[str, Any]:
    derived = result.get("derived_refresh")
    if (
        not isinstance(derived, dict)
        or derived.get("status") not in {"current", "blocked", "needs_review", "failed"}
        or not isinstance(derived.get("stage"), str)
    ):
        raise InputProvenanceError("DERIVED_REFRESH_INVALID")
    record: dict[str, Any] = {
        "study_id": study_id,
        "status": derived["status"],
        "stage": derived["stage"],
    }
    if isinstance(derived.get("reason_code"), str):
        record["reason_code"] = derived["reason_code"]
    if isinstance(derived.get("workflow_can_continue"), bool):
        record["workflow_can_continue"] = derived["workflow_can_continue"]
    return record


def _import_inputs(
    project: Path,
    manifest: object,
    actor: object,
    *,
    contract: str,
) -> dict[str, Any]:
    """Import every validated binding and publish provenance exactly once."""

    root = _project(project)
    validated = _validate_manifest(root, manifest, stale=False, contract=contract)
    try:
        existing = input_provenance_state(root, manifest)
    except InputProvenanceError as exc:
        if exc.code not in {"INPUT_PROVENANCE_MISSING", "INPUT_PROVENANCE_INVALID"}:
            raise
        existing = None
    if existing is not None:
        return {
            "status": "unchanged",
            "project_id": root.name,
            "corpus_kind": validated.corpus_kind,
            "variable_n": validated.variable_n,
            "study_count": validated.study_count,
            "counts": existing["counts"],
        }

    target_paths = [
        root / INPUT_MANIFEST_PATH,
        root / SI_REGISTRY_PATH,
        root / SOURCE_COVERAGE_PATH,
        root / ".paper_evidence.lock",
    ]
    for row in validated.rows:
        target_paths.append(root / "01_evidence/chemical_paper" / row.safe["study_id"] / "state.json")
        target_paths.append(root / row.safe["si"]["path"])
        target_paths.append(root / "01_evidence/dual_source" / row.safe["study_id"] / "binding.json")
        target_paths.append(root / "01_evidence/parse_reconciliation" / row.safe["study_id"] / "registry.json")
    before = {path: _read_bytes_or_none(path) for path in target_paths}
    temp_root = Path(tempfile.mkdtemp(prefix=".input-provenance.", dir=root.parent))
    published = False
    tokens: list[str] = []
    imported_rows: list[dict[str, Any]] = []
    derived_refreshes: list[dict[str, Any]] = []
    try:
        staged_si: dict[str, Path] = {}
        for row in validated.rows:
            stage_path = temp_root / f"{row.safe['source_id']}.pdf"
            shutil.copyfile(row.si_path, stage_path)
            observed, _ = _sha256(stage_path, "SI_INPUT_STALE")
            if observed != row.safe["si"]["sha256"]:
                raise InputProvenanceError("SI_INPUT_STALE")
            staged_si[row.safe["source_id"]] = stage_path

        preflights: dict[str, dict[str, Any]] = {}
        for row in validated.rows:
            preflight = _chemical_preflight(root, row)
            token = preflight["preflight_token"]
            tokens.append(token)
            preflights[row.safe["study_id"]] = preflight

        if not isinstance(actor, dict):
            raise InputProvenanceError("ACTOR_INVALID")
        for row in validated.rows:
            try:
                result = confirm_chemical_paper_import(
                    root,
                    {
                        "study_id": row.safe["study_id"],
                        "preflight_token": preflights[row.safe["study_id"]]["preflight_token"],
                        "actor_type": actor.get("actor_type"),
                        "actor_label": actor.get("actor_label"),
                    },
                )
            except DualParseReleaseError as exc:
                raise InputProvenanceError(exc.code) from exc
            if not isinstance(result, dict) or result.get("status") not in {"imported", "unchanged"}:
                raise InputProvenanceError("CHEMICAL_IMPORT_STATUS_INVALID")
            refresh = _derived_refresh_record(result, row.safe["study_id"])
            if refresh["status"] == "failed":
                raise InputProvenanceError(
                    str(refresh.get("reason_code") or "DERIVED_REFRESH_FAILED")
                )
            imported_rows.append(result)
            derived_refreshes.append(refresh)

        for row in validated.rows:
            destination = root / row.safe["si"]["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.replace(staged_si[row.safe["source_id"]], destination)
            else:
                staged_si[row.safe["source_id"]].unlink(missing_ok=True)

        artifact = _with_digest(
            _artifact_body(validated, derived_refreshes),
            "artifact_digest",
        )
        registry = _with_digest(_registry_body(validated), "registry_digest")
        coverage = _coverage_body(validated)
        _atomic_json(root / INPUT_MANIFEST_PATH, artifact)
        _atomic_json(root / SI_REGISTRY_PATH, registry)
        _atomic_json(root / SOURCE_COVERAGE_PATH, coverage)
        published = True
    except InputProvenanceError:
        raise
    except (OSError, shutil.Error) as exc:
        raise InputProvenanceError("INPUT_WRITE_FAILED") from exc
    finally:
        if not published:
            for path, bytes_before in before.items():
                _restore_file(path, bytes_before)
            for token in tokens:
                _cleanup_preflight_token(root, token)
        shutil.rmtree(temp_root, ignore_errors=True)
    return {
        "status": "imported",
        "project_id": root.name,
        "corpus_kind": validated.corpus_kind,
        "variable_n": validated.variable_n,
        "study_count": validated.study_count,
        "counts": _counts(validated),
        "studies": imported_rows,
    }


def import_corpus_inputs(project: Path, manifest: object, actor: object) -> dict[str, Any]:
    """Formally import an authoritative 20–40 study variable-N manifest."""

    return _import_inputs(project, manifest, actor, contract="variable_n")


def import_three_paper_inputs(project: Path, manifest: object, actor: object) -> dict[str, Any]:
    """Legacy compatibility wrapper for a marked three-paper project."""

    return _import_inputs(project, manifest, actor, contract="legacy")


__all__ = [
    "INPUT_MANIFEST_PATH",
    "InputProvenanceError",
    "MAX_CORPUS_STUDIES",
    "MIN_CORPUS_STUDIES",
    "AUTHORITATIVE_VARIABLE_N",
    "LEGACY_THREE_PAPER",
    "SI_REGISTRY_PATH",
    "SOURCE_COVERAGE_PATH",
    "corpus_input_counts",
    "import_corpus_inputs",
    "import_three_paper_inputs",
    "input_provenance_state",
    "project_input_provenance_state",
    "preflight_corpus_inputs",
    "preflight_three_paper_inputs",
    "validate_corpus_study_count",
]
