"""Safe dual-parse HTTP staging and release-currentness projection.

Scientific state remains owned by the project-layer modules.  This module only
stages untrusted Chemical Paper archives, delegates authoritative mutations,
and compares frozen manuscript bindings with current project authority.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from review_writer.project.chemical_paper import (
    MAX_ARCHIVE_BYTES,
    ChemicalPaperError,
    _archive_payload,
    import_chemical_paper,
)
from review_writer.project.source_truth import (
    SourceTruthError,
    canonical_digest,
    declared_study_ids,
    load_source_truth_bundle,
    study_source_tier,
)
from review_writer.project.paper_evidence import (
    HONEST_PROGRESSIVE_ROUTE,
    HONEST_PROGRESSIVE_COVERAGE_THRESHOLD,
    HONEST_PROGRESSIVE_PROJECT_CORE_MOLECULE_COUNT,
    PaperEvidenceError,
    build_honest_progressive_summary,
    honest_progressive_summary_from_projection,
)


STAGING_ROOT = Path(".dual-parse-staging/chemical-paper")
PREFLIGHT_TTL_SECONDS = 30 * 60
PREFLIGHT_TOKEN_PREFIX = "cp-preflight-v1."
CREDITS_STATUS = "NOT_APPLICABLE_BY_CURRENT_SCOPE"
REACTION_UNAVAILABLE = "unavailable_not_provided"
INPUT_COVERAGE_SCHEMA = "dashboard-input-coverage.v1"
SOURCE_READY_STATUSES = frozenset({"DOWNLOADED", "IMPORTED", "VERIFIED_EXISTING"})
FORMAL_INPUT_ARTIFACTS = (
    "00_sources/input_provenance_manifest.json",
    "00_sources/si_resource_registry.json",
    "00_sources/source_coverage.json",
)
FORMAL_ROLE_ALIASES = {
    "MAIN": "MAIN",
    "MAIN_PDF": "MAIN",
    "PDF": "MAIN",
    "SI": "SI",
    "SI_PDF": "SI",
    "SUPPLEMENT": "SI",
    "SUPPLEMENTARY": "SI",
    "SUPPLEMENTARY_INFORMATION": "SI",
}

_TOKEN_RE = re.compile(r"^cp-preflight-v1\.[A-Za-z0-9_-]{32,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^(?!\.\.?$)(?!.*[/\\\x00\r\n])\S{1,240}$")
_BINDING_FIELDS = frozenset(
    {
        "study_id",
        "source_tier",
        "requires_chemical",
        "dual_source_binding_digest",
        "generic_version",
        "chemical_version",
        "chemical_completion_digest",
        "reconciliation_digest",
    }
)
_V2_COMPLETION_COUNTERS = (
    "missing_name_count",
    "missing_resolved_smiles_count",
    "ai_authored_smiles_count",
)
_LEGACY_COMPLETION_COUNTERS = frozenset(
    {
        "unresolved_field_count",
        "missing_smiles_expanded_count",
        "missing_smiles_unexpanded_count",
    }
)
EXACT_CHEMICAL_FIELD_DEPENDENCIES = frozenset({"molecule", "smiles", "molblock"})


def honest_progressive_release_projection(
    rows: object,
    *,
    core_molecule_count: int | None = None,
) -> dict[str, Any]:
    """Project tri-state scientific coverage into release eligibility.

    A blocked molecule is retained in the gap registry and does not become a
    release hard fail when the 80% coverage threshold is met.  This helper is
    intentionally independent of the dual-parse binding checks below.
    """

    effective_core_molecule_count = (
        HONEST_PROGRESSIVE_PROJECT_CORE_MOLECULE_COUNT
        if core_molecule_count is None
        else core_molecule_count
    )
    try:
        summary = build_honest_progressive_summary(
            rows,
            core_molecule_count=effective_core_molecule_count,
        )
    except PaperEvidenceError as exc:
        raise DualParseReleaseError(exc.code) from exc
    hard_fails = (
        []
        if summary["coverage_sufficient"]
        else ["HONEST_PROGRESSIVE_COVERAGE_BELOW_THRESHOLD"]
    )
    return {
        **summary,
        "internal_release_ready": not hard_fails,
        "expert_release_ready": not hard_fails,
        "hard_fails": hard_fails,
        "issues": (
            ["HONEST_PROGRESSIVE_GAPS_PRESENT"]
            if summary["blocked_count"]
            else []
        ),
    }


class DualParseReleaseError(ValueError):
    """Stable, fail-closed error for the dual-parse delivery boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _project(project: Path) -> Path:
    supplied = Path(project)
    if supplied.is_symlink() or not supplied.is_dir():
        raise DualParseReleaseError("PROJECT_INVALID")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise DualParseReleaseError("PROJECT_INVALID") from exc


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise DualParseReleaseError(code)
    return value


def _digest(value: object, code: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise DualParseReleaseError(code)
    return value


def _json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DualParseReleaseError("PREFLIGHT_STAGING_INVALID") from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _staging_dir(project: Path) -> Path:
    root = _project(project)
    current = root
    for part in STAGING_ROOT.parts:
        current = current / part
        if os.path.lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise DualParseReleaseError("PREFLIGHT_STAGING_UNSAFE")
        else:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise DualParseReleaseError("PREFLIGHT_STAGING_FAILED") from exc
    return current


def _atomic_bytes(path: Path, payload: bytes) -> None:
    if path.parent.is_symlink() or (
        os.path.lexists(path) and (path.is_symlink() or not path.is_file())
    ):
        raise DualParseReleaseError("PREFLIGHT_STAGING_UNSAFE")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise DualParseReleaseError("PREFLIGHT_STAGING_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _main_source(project: Path, study_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        bundle = load_source_truth_bundle(project, study_id)
    except SourceTruthError as exc:
        raise DualParseReleaseError("PREFLIGHT_SOURCE_STALE") from exc
    sources = bundle.get("sources")
    matches = [
        row
        for row in sources
        if isinstance(row, dict)
        and row.get("document_role") == "MAIN"
        and isinstance(row.get("pdf"), dict)
        and _SHA256_RE.fullmatch(str(row["pdf"].get("sha256", "")))
    ] if isinstance(sources, list) else []
    if len(matches) != 1:
        raise DualParseReleaseError("PREFLIGHT_SOURCE_AMBIGUOUS")
    return bundle, matches[0]


def _token_key(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise DualParseReleaseError("PREFLIGHT_TOKEN_INVALID")
    return _sha256_bytes(token.encode("ascii"))


def _stage_paths(stage: Path, key: str) -> dict[str, Path]:
    return {
        "archive": stage / f"{key}.zip",
        "ready": stage / f"{key}.json",
        "confirming": stage / f"{key}.confirming.json",
        "consumed": stage / f"{key}.consumed.json",
        "rejected": stage / f"{key}.rejected.json",
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DualParseReleaseError("PREFLIGHT_TOKEN_INVALID")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualParseReleaseError("PREFLIGHT_STAGING_INVALID") from exc
    if not isinstance(value, dict):
        raise DualParseReleaseError("PREFLIGHT_STAGING_INVALID")
    return value


def preflight_chemical_paper_import(
    project: Path,
    study_id: str,
    archive_bytes: bytes,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate untrusted bytes and persist only a non-authoritative stage."""

    root = _project(project)
    study_id = _identifier(study_id, "STUDY_ID_INVALID")
    if (
        not isinstance(archive_bytes, bytes)
        or not archive_bytes
        or len(archive_bytes) > MAX_ARCHIVE_BYTES
    ):
        raise DualParseReleaseError("CHEMICAL_ZIP_SIZE_INVALID")
    bundle, source = _main_source(root, study_id)
    stage = _staging_dir(root)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            dir=stage, prefix=".chemical-preflight.", suffix=".zip.tmp"
        )
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(archive_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        parsed = _archive_payload(temporary, int(source["page_count"]))
        token = PREFLIGHT_TOKEN_PREFIX + secrets.token_urlsafe(32)
        key = _token_key(token)
        paths = _stage_paths(stage, key)
        if any(os.path.lexists(path) for path in paths.values()):
            raise DualParseReleaseError("PREFLIGHT_TOKEN_COLLISION")
        created_at = time.time() if now is None else now
        archive_sha256 = _sha256_bytes(archive_bytes)
        if parsed.get("archive_sha256") != archive_sha256:
            raise DualParseReleaseError("PREFLIGHT_STAGED_BYTES_STALE")
        manifest = {
            "schema_version": "chemical-paper-preflight-stage.v1",
            "status": "ready",
            "token_sha256": key,
            "study_id": study_id,
            "source_id": source["source_id"],
            "source_pdf_sha256": source["pdf"]["sha256"],
            "source_truth_bundle_digest": bundle["bundle_digest"],
            "archive_sha256": archive_sha256,
            "archive_size": len(archive_bytes),
            "created_at_epoch": created_at,
            "expires_at_epoch": created_at + PREFLIGHT_TTL_SECONDS,
            "backend": parsed["backend"],
            "version": parsed["version"],
            "page_count": parsed["page_count"],
            "molecule_count": len(parsed["molecules"]),
        }
        os.replace(temporary, paths["archive"])
        temporary = None
        _atomic_bytes(paths["ready"], _json_bytes(manifest))
    except ChemicalPaperError as exc:
        raise DualParseReleaseError(exc.code) from exc
    except OSError as exc:
        raise DualParseReleaseError("PREFLIGHT_STAGING_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "status": "ready_for_confirmation",
        "study_id": study_id,
        "preflight_token": token,
        "backend": manifest["backend"],
        "version": manifest["version"],
        "page_count": manifest["page_count"],
        "molecule_count": manifest["molecule_count"],
        "file_kinds": ["layout", "markdown", "molecule_info"],
        "reaction_data_status": REACTION_UNAVAILABLE,
    }


def _claim_preflight(paths: dict[str, Path]) -> dict[str, Any]:
    if paths["consumed"].is_file():
        raise DualParseReleaseError("PREFLIGHT_ALREADY_CONFIRMED")
    if paths["confirming"].is_file():
        raise DualParseReleaseError("PREFLIGHT_CONFIRM_IN_PROGRESS")
    if paths["rejected"].is_file():
        raise DualParseReleaseError("PREFLIGHT_REJECTED")
    try:
        os.replace(paths["ready"], paths["confirming"])
    except FileNotFoundError as exc:
        if paths["consumed"].is_file():
            raise DualParseReleaseError("PREFLIGHT_ALREADY_CONFIRMED") from exc
        raise DualParseReleaseError("PREFLIGHT_TOKEN_INVALID") from exc
    except OSError as exc:
        raise DualParseReleaseError("PREFLIGHT_STAGING_FAILED") from exc
    return _read_manifest(paths["confirming"])


def confirm_chemical_paper_import(
    project: Path,
    payload: object,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Consume one stage after revalidating bytes and current source authority."""

    if not isinstance(payload, dict) or set(payload) != {
        "study_id",
        "preflight_token",
        "actor_type",
        "actor_label",
    }:
        raise DualParseReleaseError("CHEMICAL_CONFIRM_REQUEST_INVALID")
    root = _project(project)
    study_id = _identifier(payload.get("study_id"), "STUDY_ID_INVALID")
    token = payload.get("preflight_token")
    if not isinstance(token, str):
        raise DualParseReleaseError("PREFLIGHT_TOKEN_INVALID")
    key = _token_key(token)
    stage = _staging_dir(root)
    paths = _stage_paths(stage, key)
    manifest = _claim_preflight(paths)
    try:
        if (
            manifest.get("schema_version") != "chemical-paper-preflight-stage.v1"
            or manifest.get("status") != "ready"
            or manifest.get("token_sha256") != key
            or manifest.get("study_id") != study_id
        ):
            raise DualParseReleaseError("PREFLIGHT_STUDY_MISMATCH")
        current_time = time.time() if now is None else now
        expires = manifest.get("expires_at_epoch")
        if not isinstance(expires, (int, float)) or isinstance(expires, bool) or current_time > expires:
            raise DualParseReleaseError("PREFLIGHT_EXPIRED")
        archive = paths["archive"]
        if archive.is_symlink() or not archive.is_file():
            raise DualParseReleaseError("PREFLIGHT_STAGED_BYTES_STALE")
        archive_bytes = archive.read_bytes()
        if (
            len(archive_bytes) != manifest.get("archive_size")
            or _sha256_bytes(archive_bytes) != manifest.get("archive_sha256")
        ):
            raise DualParseReleaseError("PREFLIGHT_STAGED_BYTES_STALE")
        bundle, source = _main_source(root, study_id)
        if (
            bundle.get("bundle_digest") != manifest.get("source_truth_bundle_digest")
            or source.get("source_id") != manifest.get("source_id")
            or source.get("pdf", {}).get("sha256") != manifest.get("source_pdf_sha256")
        ):
            raise DualParseReleaseError("PREFLIGHT_SOURCE_STALE")
        parsed = _archive_payload(archive, int(source["page_count"]))
        if (
            parsed.get("archive_sha256") != manifest.get("archive_sha256")
            or parsed.get("backend") != manifest.get("backend")
            or parsed.get("version") != manifest.get("version")
            or parsed.get("page_count") != manifest.get("page_count")
            or len(parsed.get("molecules", [])) != manifest.get("molecule_count")
        ):
            raise DualParseReleaseError("PREFLIGHT_STAGED_BYTES_STALE")
        result = import_chemical_paper(
            root,
            study_id,
            str(manifest["source_pdf_sha256"]),
            archive,
            {
                "actor_type": payload.get("actor_type"),
                "actor_label": payload.get("actor_label"),
            },
        )
        try:
            derived_refresh = refresh_dual_parse_derived_state(root, study_id)
            if not isinstance(derived_refresh, dict):
                derived_refresh = {
                    "status": "failed",
                    "stage": "derived_binding",
                    "reason_code": "DERIVED_REFRESH_INVALID",
                }
        except Exception as exc:
            # The importer has already committed the authoritative Chemical
            # state.  Surface a failed derived refresh without pretending that
            # the import was rolled back or that the dual lane is current.
            reason_code = getattr(exc, "code", None)
            derived_refresh = {
                "status": "failed",
                "stage": "derived_binding",
                "reason_code": reason_code
                if isinstance(reason_code, str)
                else "DERIVED_REFRESH_FAILED",
            }
        try:
            consumed = {
                "schema_version": "chemical-paper-preflight-stage.v1",
                "status": "consumed",
                "token_sha256": key,
                "study_id": study_id,
            }
            _atomic_bytes(paths["consumed"], _json_bytes(consumed))
            paths["confirming"].unlink(missing_ok=True)
            paths["archive"].unlink(missing_ok=True)
        except (DualParseReleaseError, OSError):
            # The authoritative importer has committed successfully.  Staging
            # bookkeeping is best-effort from this point so an HTTP failure can
            # never falsely claim that the authoritative write was rolled back.
            pass
    except ChemicalPaperError as exc:
        try:
            os.replace(paths["confirming"], paths["rejected"])
        except OSError:
            pass
        raise DualParseReleaseError(exc.code) from exc
    except DualParseReleaseError:
        try:
            os.replace(paths["confirming"], paths["rejected"])
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            os.replace(paths["confirming"], paths["rejected"])
        except OSError:
            pass
        raise DualParseReleaseError("PREFLIGHT_STAGING_FAILED") from exc
    return {
        "status": str(result["status"]),
        "study_id": study_id,
        "derived_refresh": derived_refresh,
        "derived_refresh_status": derived_refresh.get("status"),
    }


def _researcher_actor(payload: dict[str, Any], code: str) -> None:
    actor_type = payload.get("actor_type")
    actor_label = payload.get("actor_label")
    if actor_type not in {"human_researcher", "simulated_researcher_agent"}:
        raise DualParseReleaseError(code)
    if (
        not isinstance(actor_label, str)
        or not actor_label
        or actor_label != actor_label.strip()
        or len(actor_label) > 200
    ):
        raise DualParseReleaseError(code)


def _authority_error(exc: Exception, fallback: str) -> DualParseReleaseError:
    code = getattr(exc, "code", fallback)
    return DualParseReleaseError(code if isinstance(code, str) else fallback)


def refresh_dual_parse_derived_state(
    project: Path, study_id: str
) -> dict[str, Any]:
    """Recompute version-bound gates without making a scientific decision."""

    root = _project(project)
    study_id = _identifier(study_id, "STUDY_ID_INVALID")
    try:
        from review_writer.project.chemical_completion import (
            ChemicalCompletionError,
            require_chemical_completion_ready,
        )
        from review_writer.project.dual_source import (
            DualSourceError,
            write_dual_source_binding,
        )
        from review_writer.project.parse_reconciliation import (
            ParseReconciliationError,
            write_parse_reconciliation,
        )
    except ImportError as exc:
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_UNAVAILABLE") from exc

    try:
        write_dual_source_binding(root, study_id)
    except DualSourceError as exc:
        return {
            "status": "blocked",
            "stage": "dual_source",
            "reason_code": exc.code,
        }
    try:
        require_chemical_completion_ready(root, study_id)
    except ChemicalCompletionError as exc:
        return {
            "status": "blocked",
            "stage": "chemical_completion",
            "reason_code": exc.code,
        }
    try:
        registry = write_parse_reconciliation(root, study_id)
    except ParseReconciliationError as exc:
        return {
            "status": "blocked",
            "stage": "reconciliation",
            "reason_code": exc.code,
        }
    return {
        "status": (
            "current" if registry.get("workflow_can_continue") is True else "needs_review"
        ),
        "stage": "reconciliation",
        "workflow_can_continue": registry.get("workflow_can_continue") is True,
    }


def apply_chemical_completion_http(project: Path, payload: object) -> dict[str, Any]:
    """Validate the HTTP contract and delegate the authoritative batch write."""

    required = {
        "study_id",
        "version_token",
        "actor_type",
        "actor_label",
        "corrections",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise DualParseReleaseError("CHEMICAL_COMPLETION_REQUEST_INVALID")
    _researcher_actor(payload, "CHEMICAL_COMPLETION_RESEARCHER_REQUIRED")
    study_id = _identifier(
        payload.get("study_id"), "CHEMICAL_COMPLETION_REQUEST_INVALID"
    )
    root = _project(project)
    if not isinstance(payload.get("version_token"), str) or not isinstance(
        payload.get("corrections"), list
    ):
        raise DualParseReleaseError("CHEMICAL_COMPLETION_REQUEST_INVALID")
    try:
        from review_writer.project.chemical_completion import (
            apply_chemical_completion_batch,
        )
    except ImportError as exc:
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_UNAVAILABLE") from exc
    authority_payload = {key: value for key, value in payload.items() if key != "study_id"}
    try:
        result = apply_chemical_completion_batch(
            root, study_id, authority_payload
        )
    except Exception as exc:
        raise _authority_error(exc, "CHEMICAL_COMPLETION_REQUEST_INVALID") from exc
    if not isinstance(result, dict):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    refreshes: list[dict[str, Any]] = []
    try:
        study_ids = declared_study_ids(root)
    except SourceTruthError as exc:
        raise DualParseReleaseError(exc.code) from exc
    for current_study_id in study_ids:
        try:
            if study_source_tier(root, current_study_id) != "core":
                continue
        except SourceTruthError as exc:
            raise DualParseReleaseError(exc.code) from exc
        refreshes.append({
            "study_id": current_study_id,
            **refresh_dual_parse_derived_state(root, current_study_id),
        })
    current_refresh = next(
        (row for row in refreshes if row["study_id"] == study_id),
        {"study_id": study_id, "status": "blocked", "stage": "reconciliation", "reason_code": "DERIVED_REFRESH_MISSING"},
    )
    return {**result, "derived_refresh": current_refresh, "derived_refreshes": refreshes}


def _opaque_registry_token(value: str) -> str:
    digest = _digest(value, "PARSE_RECONCILIATION_REQUEST_INVALID")
    assert isinstance(digest, str)
    return "rcv1." + base64.urlsafe_b64encode(bytes.fromhex(digest)).decode("ascii").rstrip("=")


def _registry_digest_from_token(value: object) -> str:
    if isinstance(value, str) and value.startswith("rcv1."):
        encoded = value[5:]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError) as exc:
            raise DualParseReleaseError("PARSE_RECONCILIATION_REQUEST_INVALID") from exc
        if len(raw) != 32:
            raise DualParseReleaseError("PARSE_RECONCILIATION_REQUEST_INVALID")
        return raw.hex()
    return str(_digest(value, "PARSE_RECONCILIATION_REQUEST_INVALID"))


def apply_reconciliation_http(project: Path, payload: object) -> dict[str, Any]:
    """Validate a researcher PDF decision and delegate the registry mutation."""

    required = {
        "study_id",
        "object_id",
        "registry_digest",
        "action",
        "selected_lane",
        "note",
        "pdf_locator",
        "actor_type",
        "actor_label",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise DualParseReleaseError("PARSE_RECONCILIATION_REQUEST_INVALID")
    _researcher_actor(payload, "PARSE_RECONCILIATION_RESEARCHER_REQUIRED")
    study_id = _identifier(
        payload.get("study_id"), "PARSE_RECONCILIATION_REQUEST_INVALID"
    )
    authority_payload = {key: value for key, value in payload.items() if key != "study_id"}
    authority_payload["registry_digest"] = _registry_digest_from_token(
        payload.get("registry_digest")
    )
    try:
        from review_writer.project.parse_reconciliation import (
            apply_reconciliation_decision,
        )
    except ImportError as exc:
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_UNAVAILABLE") from exc
    try:
        result = apply_reconciliation_decision(
            _project(project), study_id, authority_payload
        )
    except Exception as exc:
        raise _authority_error(exc, "PARSE_RECONCILIATION_REQUEST_INVALID") from exc
    if not isinstance(result, dict):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    return result


def _dashboard_authority_payloads(
    project: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        from review_writer.project.chemical_completion import (
            project_chemical_completion_state,
        )
        from review_writer.project.parse_reconciliation import (
            project_reconciliation_state,
        )
        from review_writer.project.workflow_projection import (
            _workflow_and_dual_source_state,
        )
        from review_writer.project.chemical_paper import (
            ChemicalPaperError,
            chemical_paper_projection,
        )
    except ImportError as exc:
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_UNAVAILABLE") from exc
    root = _project(project)
    workflow, dual = _workflow_and_dual_source_state(root)
    try:
        chemical = chemical_paper_projection(root)
    except ChemicalPaperError as exc:
        if exc.code not in {
            "SOURCE_ASSET_DRIFT",
            "SOURCE_ASSET_INVALID",
        }:
            raise
        chemical = {
            "schema_version": "chemical-paper-projection.v2",
            "studies": [
                {
                    "study_id": row.get("study_id"),
                    "status": "stale",
                    "pdf_binding_status": "stale",
                    "reaction_data_status": REACTION_UNAVAILABLE,
                }
                for row in _study_rows(dual)
            ]
        }
    values = (
        dual,
        project_chemical_completion_state(root),
        project_reconciliation_state(root),
        chemical,
        workflow,
    )
    if not all(isinstance(value, dict) for value in values):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    return values  # type: ignore[return-value]


def _study_rows(value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("studies")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    return rows


def _project_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _formal_rows(payload: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]] | None:
    for key in keys:
        rows = payload.get(key)
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            return rows
    return None


def _formal_role(row: dict[str, Any]) -> str | None:
    for key in ("document_role", "role", "input_role", "resource_role", "source_kind"):
        value = row.get(key)
        if isinstance(value, str):
            role = FORMAL_ROLE_ALIASES.get(value.strip().upper())
            if role is not None:
                return role
    return None


def _formal_status(row: dict[str, Any]) -> str | None:
    for key in ("status", "currentness", "availability", "state", "si_status"):
        value = row.get(key)
        if not isinstance(value, str):
            continue
        status = value.strip().upper()
        if status in SOURCE_READY_STATUSES | {"ACQUIRED", "AVAILABLE", "CURRENT", "READY", "REGISTERED"}:
            return "current"
        if status in {"BLOCKED", "FAILED", "INCOMPLETE", "MISSING", "NOT_AVAILABLE", "NOT_READY", "STALE"}:
            return "missing"
        return "unknown"
    return None


def _formal_status_for_role(
    row: dict[str, Any], role: str, *, artifact_status: str | None = None
) -> str | None:
    status = _formal_status(row)
    if status is not None:
        return status
    nested_key = "main_pdf" if role == "MAIN" else "si" if role == "SI" else None
    nested = row.get(nested_key) if nested_key is not None else None
    if isinstance(nested, dict):
        nested_status = _formal_status(nested)
        if nested_status is not None:
            return nested_status
        if role == "MAIN" and artifact_status is not None:
            return artifact_status
    return None


def _formal_manifest_matches(row: dict[str, Any], role: str) -> bool:
    if _formal_role(row) == role:
        return True
    if role == "MAIN":
        return isinstance(row.get("main_pdf"), dict)
    if role == "SI":
        return isinstance(row.get("si"), dict)
    return False


def _formal_source_role_currentness(
    project: Path, study_ids: list[str], role: str
) -> dict[str, str] | None:
    paths = [project / relative for relative in FORMAL_INPUT_ARTIFACTS]
    present = [path.is_file() for path in paths]
    if not any(present):
        return None
    if not all(present):
        return {study_id: "unknown" for study_id in study_ids}
    payloads = [_project_json(path) for path in paths]
    if not all(isinstance(payload, dict) for payload in payloads):
        return {study_id: "unknown" for study_id in study_ids}
    manifest, registry, coverage = payloads
    manifest_rows = _formal_rows(manifest, ("inputs", "sources", "records", "studies"))
    registry_rows = _formal_rows(registry, ("resources", "records", "studies"))
    coverage_rows = _formal_rows(coverage, ("studies", "coverage"))
    if manifest_rows is None or coverage_rows is None or (role == "SI" and registry_rows is None):
        return {study_id: "unknown" for study_id in study_ids}

    normalized_role = role.strip().upper()
    manifest_status = _formal_status(manifest)
    projected: dict[str, str] = {}
    for study_id in study_ids:
        manifest_matches = [
            row for row in manifest_rows
            if row.get("study_id") == study_id
            and _formal_manifest_matches(row, normalized_role)
        ]
        registry_matches = [
            row for row in registry_rows or []
            if row.get("study_id") == study_id
            and (_formal_role(row) == normalized_role or (normalized_role == "SI" and _formal_role(row) is None))
        ]
        coverage_matches = [row for row in coverage_rows if row.get("study_id") == study_id]
        required_matches = [manifest_matches, coverage_matches]
        if normalized_role == "SI":
            required_matches.append(registry_matches)
        if any(len(matches) != 1 for matches in required_matches):
            projected[study_id] = "unknown" if any(len(matches) > 1 for matches in required_matches) else "missing"
            continue

        coverage_row = coverage_matches[0]
        available_roles = coverage_row.get("available_roles")
        has_role = isinstance(available_roles, list) and any(
            isinstance(value, str)
            and FORMAL_ROLE_ALIASES.get(value.strip().upper()) == normalized_role
            for value in available_roles
        )
        study_status = str(coverage_row.get("study_status", "")).upper()
        if not has_role or study_status in {"BLOCKED", "FAILED", "INCOMPLETE", "MISSING", "NOT_READY", "STALE"}:
            projected[study_id] = "missing"
            continue
        rows = [manifest_matches[0], coverage_row]
        if normalized_role == "SI":
            rows.append(registry_matches[0])
        states = [
            _formal_status_for_role(
                row,
                normalized_role,
                artifact_status=manifest_status if row is manifest_matches[0] else None,
            )
            for row in rows
        ]
        projected[study_id] = (
            "missing" if "missing" in states
            else "unknown" if "unknown" in states
            else "current"
        )
    return projected


def _source_role_currentness(
    project: Path, study_ids: list[str], role: str
) -> dict[str, str]:
    """Project only current/missing SI availability, never acquisition details."""

    formal = _formal_source_role_currentness(project, study_ids, role)
    if formal is not None:
        return formal

    manifest_path = project / "00_discovery/acquisition_manifest.json"
    receipt_path = project / "00_sources/acquisition_receipt.json"
    manifest = _project_json(manifest_path)
    receipt = _project_json(receipt_path)
    if manifest is not None or receipt is not None:
        if not isinstance(manifest, dict) or not isinstance(receipt, dict):
            return {study_id: "unknown" for study_id in study_ids}
        downloads = manifest.get("downloads")
        results = receipt.get("results")
        if not isinstance(downloads, list) or not isinstance(results, list):
            return {study_id: "unknown" for study_id in study_ids}
        if not all(isinstance(row, dict) for row in [*downloads, *results]):
            return {study_id: "unknown" for study_id in study_ids}
        result_by_id = {
            row.get("download_id"): row
            for row in results
            if isinstance(row.get("download_id"), str)
        }
        if len(result_by_id) != len(results):
            return {study_id: "unknown" for study_id in study_ids}
        projected: dict[str, str] = {}
        for study_id in study_ids:
            matches = [
                row
                for row in downloads
                if row.get("study_id") == study_id
                and str(row.get("document_role", "")).upper() == role
            ]
            if len(matches) != 1:
                projected[study_id] = "unknown" if len(matches) > 1 else "missing"
                continue
            download_id = matches[0].get("download_id")
            result = result_by_id.get(download_id)
            status = str(result.get("status", "")).upper() if isinstance(result, dict) else ""
            projected[study_id] = "current" if status in SOURCE_READY_STATUSES else "missing"
        return projected

    projected = {}
    for study_id in study_ids:
        try:
            bundle = load_source_truth_bundle(project, study_id)
        except SourceTruthError:
            projected[study_id] = "unknown"
            continue
        sources = [
            row
            for row in bundle.get("sources", [])
            if isinstance(row, dict)
            and str(row.get("document_role", "")).upper() == role
        ]
        projected[study_id] = "current" if len(sources) == 1 else "unknown"
    return projected


def _coverage_lane(available: int, total: int | None) -> dict[str, Any]:
    if total is None:
        return {"available": None, "total": None, "status": "unknown"}
    if total <= 0:
        return {"available": None, "total": None, "status": "unknown"}
    status = (
        "current"
        if available == total
        else "missing"
        if available == 0
        else "needs_review"
    )
    return {"available": available, "total": total, "status": status}


def _dashboard_input_coverage(project: Path, studies: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the four researcher-visible hard inputs without raw provenance."""

    core = [row for row in studies if row.get("source_tier") == "core"]
    study_ids = [row["study_id"] for row in core if isinstance(row.get("study_id"), str)]
    total = len(study_ids) or None
    si_by_study = _source_role_currentness(project, study_ids, "SI")
    currentness: list[dict[str, Any]] = []
    for row in core:
        study_id = row.get("study_id")
        if not isinstance(study_id, str):
            continue
        chemical_status = (
            "current"
            if row.get("chemical_binding_status") == "bound"
            else "missing"
            if row.get("chemical_import_status") == "missing"
            else "needs_review"
        )
        currentness.append(
            {
                "study_id": study_id,
                "main_pdf_status": "current"
                if row.get("pdf_status") == "verified"
                else "unknown",
                "si_status": si_by_study.get(study_id, "unknown"),
                "chemical_zip_status": chemical_status,
                "generic_parse_status": "current"
                if row.get("generic_parse_status") == "current"
                else "unknown",
            }
        )
    lanes = {
        "main_pdf": _coverage_lane(
            sum(row["main_pdf_status"] == "current" for row in currentness), total
        ),
        "si": _coverage_lane(
            sum(row["si_status"] == "current" for row in currentness), total
        ),
        "chemical_zip": _coverage_lane(
            sum(row["chemical_zip_status"] == "current" for row in currentness), total
        ),
        "generic_parse": _coverage_lane(
            sum(row["generic_parse_status"] == "current" for row in currentness), total
        ),
    }
    parts = [
        f"{lane['available']}/{lane['total']}"
        if lane["available"] is not None and lane["total"] is not None
        else "未知/未知"
        for lane in lanes.values()
    ]
    gate_values = [
        str(lane["available"]) if lane["available"] is not None else "未知"
        for lane in lanes.values()
    ]
    return {
        "schema_version": INPUT_COVERAGE_SCHEMA,
        "hard_gate": "/".join(gate_values),
        "hard_gate_label": " · ".join(
            f"{label} {part}"
            for label, part in zip(
                ("主 PDF", "SI", "Chemical ZIP", "Generic Parse"), parts, strict=True
            )
        ),
        "ready": bool(lanes) and all(lane["status"] == "current" for lane in lanes.values()),
        "source_disclosure": "当前输入仅披露来源可用性与 currentness；原始 PDF 是科学仲裁来源。",
        "lanes": lanes,
        "studies": currentness,
    }


def _candidate_label(value: object) -> str | None:
    if not isinstance(value, dict):
        return value if isinstance(value, str) and value.strip() else None
    parts = []
    for key, label in (
        ("mol_idt", "名称"),
        ("smiles_expanded", "展开 SMILES"),
        ("smiles_unexpanded", "未展开 SMILES"),
    ):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            parts.append(f"{label}: {item.strip()}")
    return " · ".join(parts) or None


def _researcher_next_action(value: object) -> dict[str, str]:
    text = value.strip() if isinstance(value, str) else ""
    lowered = text.casefold()
    if ("import" in lowered or "确认" in text) and "chemical" in lowered:
        label = "确认下一篇 Chemical Paper 导入"
    elif "completion" in lowered or "missing" in lowered:
        label = "补全下一项化学字段"
    elif "reconciliation" in lowered or "dual-parse conflict" in lowered:
        label = "依据 PDF 仲裁下一项双层解析差异"
    elif "paper evidence" in lowered:
        label = "审查下一篇研究证据"
    else:
        label = text or "等待双层解析权威状态"
    return {
        "label": label,
        "description": "完成这一项并保存后，系统会重新计算 Evidence 门禁。",
    }


def _input_gate_next_action(
    raw_action: object, coverage: dict[str, Any]
) -> tuple[str, str] | None:
    """Make the first unmet hard input own the single visible next action."""

    order = (
        ("main_pdf", "补齐并核验主 PDF"),
        ("si", "补齐并核验 SI"),
        ("chemical_zip", "待 Chemical Paper 导入"),
        ("generic_parse", "继续核对 Generic Parse"),
    )
    lanes = coverage.get("lanes", {})
    for name, label in order:
        lane = lanes.get(name, {})
        if lane.get("status") == "current":
            continue
        if name == "chemical_zip":
            text = raw_action.strip() if isinstance(raw_action, str) else ""
            lowered = text.casefold()
            if "confirm" in lowered or "确认" in text:
                label = "确认第一份 Chemical Paper 导入"
        return label, "先完成当前输入硬门；保存后系统会重新计算后续 Evidence 门禁。"
    return None


def _chemical_import_contract(
    source: dict[str, Any],
    chemistry: dict[str, Any],
    completion: dict[str, Any],
    reconciliation: dict[str, Any],
) -> tuple[str, bool]:
    """Separate a current PDF-bound import from Evidence-lane currentness.

    ``needs_review`` is intentionally a positive import/binding state: the
    current verified PDF has a safe Chemical Paper projection, but researcher
    completion or reconciliation is still required.  ``current`` is reserved
    for a lane that has cleared both gates and can enter Paper Evidence.
    """

    nested = source.get("chemical")
    reported_status = source.get(
        "chemical_status",
        nested.get("status") if isinstance(nested, dict) else chemistry.get("status"),
    )
    if reported_status == "missing" or chemistry.get("status") == "missing":
        return "missing", False
    source_current = (
        source.get("pdf_status") == "verified"
        and source.get("generic_parse_status") == "current"
    )
    # A current Chemical PDF import is not itself a dual-source binding.
    # ``current_generic_only`` is deliberately excluded here: it names a
    # Generic-only authority state and must never be relabeled as bound.
    dual_source_current = source.get("status") == "current"
    bound = (
        source_current
        and dual_source_current
        and chemistry.get("pdf_binding_status") == "bound"
        and chemistry.get("status") in {"needs_review", "ready"}
    )
    if not bound:
        return "stale", False
    evidence_lane_current = (
        chemistry.get("status") == "ready"
        and completion.get("status") == "current"
        and reconciliation.get("status") == "current"
    )
    return ("current" if evidence_lane_current else "needs_review"), True


def dual_parse_dashboard_projection(project: Path) -> dict[str, Any]:
    """Return an explicit researcher-safe whitelist for the dashboard API."""

    dual, completion, reconciliation, chemical, workflow = _dashboard_authority_payloads(
        _project(project)
    )
    if (
        completion.get("schema_version")
        != "chemical-completion-project-state.v2"
        or reconciliation.get("schema_version")
        != "parse-reconciliation-project-state.v2"
        or chemical.get("schema_version") != "chemical-paper-projection.v2"
    ):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    completion_by_id = {row.get("study_id"): row for row in _study_rows(completion)}
    reconciliation_by_id = {
        row.get("study_id"): row for row in _study_rows(reconciliation)
    }
    for row in completion_by_id.values():
        _resolved_smiles_counters(row)
    chemical_by_id = {row.get("study_id"): row for row in _study_rows(chemical)}
    studies: list[dict[str, Any]] = []
    completion_queue: list[dict[str, Any]] = []
    reconciliation_items: list[dict[str, Any]] = []
    for source in _study_rows(dual):
        study_id = source.get("study_id")
        if not isinstance(study_id, str):
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
        complete = completion_by_id.get(study_id)
        if not isinstance(complete, dict):
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
        registry = reconciliation_by_id.get(study_id, {})
        chemistry = chemical_by_id.get(study_id, {})
        chemical_import_status, chemical_bound = _chemical_import_contract(
            source, chemistry, complete, registry
        )
        row: dict[str, Any] = {
            "study_id": study_id,
            "source_tier": source.get("source_tier"),
            "dual_source_status": source.get("status"),
            "pdf_status": source.get("pdf_status", "unknown"),
            "generic_parse_status": source.get(
                "generic_parse_status",
                source.get(
                    "generic_status",
                    source.get("generic", {}).get("status")
                    if isinstance(source.get("generic"), dict)
                    else "unknown",
                ),
            ),
            "chemical_import_status": chemical_import_status,
            "chemical_binding_status": "bound" if chemical_bound else chemical_import_status,
            "completion_status": complete.get("status"),
            "reconciliation_status": registry.get("status"),
            "page_count": source.get("page_count", chemistry.get("page_count")),
            "molecule_count": chemistry.get("molecule_count"),
            "missing_name_count": complete.get("missing_name_count"),
            "missing_resolved_smiles_count": complete.get(
                "missing_resolved_smiles_count"
            ),
            "unresolved_reconciliation_count": registry.get(
                "unresolved_count", registry.get("needs_review_count")
            ),
            "backend": chemistry.get("backend"),
            "version": chemistry.get("version"),
            "reaction_data_status": chemistry.get(
                "reaction_data_status", REACTION_UNAVAILABLE
            ),
            "paper_evidence_status": (
                "available"
                if workflow.get("paper_evidence_ready") is True
                else "blocked"
            ),
            "completion_version_token": complete.get("version_token"),
        }
        registry_digest = registry.get("registry_digest")
        if isinstance(registry_digest, str) and _SHA256_RE.fullmatch(registry_digest):
            row["reconciliation_version_token"] = _opaque_registry_token(
                registry_digest
            )
        for key in ("imported_at", "updated_at", "actor_type", "actor_label", "pdf_page_url"):
            value = chemistry.get(key, complete.get(key, registry.get(key)))
            if value is not None:
                row[key] = value
        studies.append({key: value for key, value in row.items() if value is not None})

        molecules = chemistry.get("molecules")
        molecule_by_index = {
            molecule.get("molecule_index"): molecule
            for molecule in molecules
            if isinstance(molecule, dict)
            and isinstance(molecule.get("molecule_index"), int)
        } if isinstance(molecules, list) else {}
        missing_fields = complete.get("missing_fields")
        for missing in missing_fields if isinstance(missing_fields, list) else []:
            if not isinstance(missing, dict):
                continue
            molecule_index = missing.get("molecule_index")
            field = missing.get("field")
            if (
                not isinstance(molecule_index, int)
                or isinstance(molecule_index, bool)
                or field != "resolved_smiles"
                or not isinstance(complete.get("version_token"), str)
            ):
                continue
            molecule = molecule_by_index.get(molecule_index, {})
            queue_row = {
                "study_id": study_id,
                "molecule_index": molecule_index,
                "version_token": complete["version_token"],
                "field": field,
                "page": missing.get("page"),
                "bbox_normalized": missing.get("bbox_normalized"),
                "pdf_page_url": molecule.get("pdf_page_url"),
            }
            history = molecule.get("history")
            latest = history[-1] if isinstance(history, list) and history and isinstance(history[-1], dict) else {}
            for key in ("actor_label", "recorded_at"):
                if latest.get(key) is not None:
                    queue_row["updated_at" if key == "recorded_at" else key] = latest[key]
            completion_queue.append(
                {key: value for key, value in queue_row.items() if value is not None}
            )

        try:
            from review_writer.project.parse_reconciliation import (
                ParseReconciliationError,
                load_parse_reconciliation,
            )
        except ImportError as exc:
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_UNAVAILABLE") from exc
        try:
            saved_registry = load_parse_reconciliation(_project(project), study_id)
        except ParseReconciliationError as exc:
            if exc.code not in {
                "PARSE_RECONCILIATION_MISSING",
                "PARSE_RECONCILIATION_INVALID",
                "PARSE_RECONCILIATION_STALE",
            }:
                raise
            saved_registry = None
        if isinstance(saved_registry, dict):
            registry_digest = saved_registry.get("registry_digest")
            if isinstance(registry_digest, str) and _SHA256_RE.fullmatch(registry_digest):
                version_token = _opaque_registry_token(registry_digest)
                for item in saved_registry.get("objects", []):
                    if (
                        not isinstance(item, dict)
                        or item.get("status") not in {
                            "conflict", "single_lane_only", "needs_review", "stale", "blocked"
                        }
                        or isinstance(item.get("decision"), dict)
                    ):
                        continue
                    page = item.get("page")
                    page_molecule = next(
                        (
                            molecule
                            for molecule in molecule_by_index.values()
                            if molecule.get("page") == page
                        ),
                        {},
                    )
                    reconciliation_items.append({
                        "study_id": study_id,
                        "object_id": item.get("object_id"),
                        "registry_digest": version_token,
                        "kind": item.get("kind"),
                        "status": item.get("status"),
                        "generic_candidate": _candidate_label(item.get("generic_candidate")),
                        "chemical_candidate": _candidate_label(item.get("chemical_candidate")),
                        "page": page,
                        "pdf_page_url": page_molecule.get("pdf_page_url"),
                    })
    studies.sort(key=lambda row: row["study_id"])
    completion_queue.sort(key=lambda row: (row["study_id"], row["molecule_index"], row["field"]))
    reconciliation_items.sort(key=lambda row: (str(row.get("study_id")), str(row.get("object_id"))))
    core = [row for row in studies if row.get("source_tier") == "core"]
    next_action = workflow.get("unique_next_action", workflow.get("next_action"))
    input_coverage = _dashboard_input_coverage(_project(project), studies)
    gate_action = _input_gate_next_action(next_action, input_coverage)
    researcher_action = (
        {"label": gate_action[0], "description": gate_action[1]}
        if gate_action is not None
        else _researcher_next_action(next_action)
    )
    project_current = bool(studies) and all(
        row.get("generic_parse_status") == "current"
        and (
            row.get("source_tier") != "core"
            or (
                row.get("chemical_import_status") == "current"
                and row.get("completion_status") == "current"
                and row.get("reconciliation_status") == "current"
            )
        )
        for row in studies
    )
    return {
        "schema_version": "dual-parse-projection.v2",
        "status": "ready",
        "next_action": researcher_action,
        "project_status": "current" if project_current else "needs_review",
        "summary": {
            "core_studies": len(core),
            "pdf_verified": sum(
                row.get("pdf_status") == "verified" for row in core
            ),
            "generic_current": sum(
                row.get("generic_parse_status") == "current" for row in core
            ),
            "chemical_current": sum(
                row.get("chemical_import_status") == "current" for row in core
            ),
            "chemical_bound": sum(
                row.get("chemical_binding_status") == "bound" for row in core
            ),
            "reaction_data_status": (
                "available"
                if studies
                and all(row.get("reaction_data_status") == "available" for row in studies)
                else REACTION_UNAVAILABLE
            ),
        },
        "studies": studies,
        "input_coverage": input_coverage,
        "import_preflight": None,
        "completion_queue": completion_queue,
        "reconciliation_items": reconciliation_items,
        "unique_next_action": next_action,
    }


def _normalize_binding(row: object) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != _BINDING_FIELDS:
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    study_id = _identifier(row.get("study_id"), "DUAL_PARSE_BINDING_INVALID")
    source_tier = row.get("source_tier")
    requires_chemical = row.get("requires_chemical")
    if source_tier not in {"core", "background"} or not isinstance(requires_chemical, bool):
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    if source_tier == "core" and not requires_chemical:
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    chemical_version = _digest(
        row.get("chemical_version"),
        "DUAL_PARSE_BINDING_INVALID",
        nullable=not requires_chemical,
    )
    completion = _digest(
        row.get("chemical_completion_digest"),
        "DUAL_PARSE_BINDING_INVALID",
        nullable=not requires_chemical,
    )
    reconciliation = _digest(
        row.get("reconciliation_digest"),
        "DUAL_PARSE_BINDING_INVALID",
        nullable=not requires_chemical,
    )
    if not requires_chemical and any(
        value is not None for value in (chemical_version, completion, reconciliation)
    ):
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    return {
        "study_id": study_id,
        "source_tier": source_tier,
        "requires_chemical": requires_chemical,
        "dual_source_binding_digest": _digest(
            row.get("dual_source_binding_digest"), "DUAL_PARSE_BINDING_INVALID"
        ),
        "generic_version": _digest(
            row.get("generic_version"), "DUAL_PARSE_BINDING_INVALID"
        ),
        "chemical_version": chemical_version,
        "chemical_completion_digest": completion,
        "reconciliation_digest": reconciliation,
    }


def _binding_from_authority(row: dict[str, Any]) -> dict[str, Any]:
    tier = row.get("source_tier")
    requires_chemical = bool(tier == "core" or row.get("requires_chemical") is True)
    return _normalize_binding(
        {
            "study_id": row.get("study_id"),
            "source_tier": tier,
            "requires_chemical": requires_chemical,
            "dual_source_binding_digest": row.get("dual_source_binding_digest"),
            "generic_version": row.get("generic_version"),
            "chemical_version": row.get("chemical_version") if requires_chemical else None,
            "chemical_completion_digest": (
                row.get("chemical_completion_digest") if requires_chemical else None
            ),
            "reconciliation_digest": (
                row.get("reconciliation_digest") if requires_chemical else None
            ),
        }
    )


def build_dual_parse_manuscript_bindings(
    authority_rows: Iterable[dict[str, Any]], study_ids: set[str]
) -> list[dict[str, Any]]:
    """Freeze only studies actually referenced by the manuscript."""

    by_study = {
        str(row.get("study_id")): row
        for row in authority_rows
        if isinstance(row, dict) and isinstance(row.get("study_id"), str)
    }
    if not study_ids or set(by_study).intersection(study_ids) != study_ids:
        raise DualParseReleaseError("DUAL_PARSE_DEPENDENCY_MISSING")
    result = [_binding_from_authority(by_study[study_id]) for study_id in sorted(study_ids)]
    if len({row["study_id"] for row in result}) != len(result):
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    return result


def dual_parse_manuscript_bindings(
    project: Path, study_ids: set[str]
) -> list[dict[str, Any]]:
    """Read current authority and freeze the manuscript's exact study set."""

    root = _project(project)
    return build_dual_parse_manuscript_bindings(
        _current_authority_rows(root), study_ids
    )


def _current_authority_rows(project: Path) -> list[dict[str, Any]]:
    """Load current Scientific State through its public, read-only projections."""

    try:
        from review_writer.project.chemical_completion import (
            project_chemical_completion_state,
        )
        from review_writer.project.dual_source import project_dual_source_state
        from review_writer.project.parse_reconciliation import (
            project_reconciliation_state,
        )
    except ImportError as exc:
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_UNAVAILABLE") from exc
    dual = project_dual_source_state(project)
    completion = project_chemical_completion_state(project)
    reconciliation = project_reconciliation_state(project)
    return authority_rows_from_projections(dual, completion, reconciliation)


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    return next((row[key] for key in keys if row.get(key) is not None), None)


def _v2_completion_counters(row: object) -> dict[str, int]:
    if not isinstance(row, dict):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    if _LEGACY_COMPLETION_COUNTERS.intersection(row):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    honest_fields = {
        "honest_progressive",
        "molecules",
        "molecule_rows",
        "core_molecule_count",
        "confirmed_count",
        "ai_provisional_count",
        "blocked_count",
    }
    honest_payload = bool(honest_fields.intersection(row))
    counters: dict[str, int] = {}
    for key in _V2_COMPLETION_COUNTERS:
        value = row.get(key)
        if value is None and honest_payload:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
        counters[key] = value
    molecule_count = row.get("molecule_count", row.get("core_molecule_count"))
    if molecule_count is not None:
        if (
            isinstance(molecule_count, bool)
            or not isinstance(molecule_count, int)
            or molecule_count < 0
            or any(value > molecule_count for value in counters.values())
        ):
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    return counters


def _resolved_smiles_counters(row: object) -> tuple[int, int]:
    counters = _v2_completion_counters(row)
    return (
        counters["missing_resolved_smiles_count"],
        counters["ai_authored_smiles_count"],
    )


def authority_rows_from_projections(
    dual: object, completion: object, reconciliation: object
) -> list[dict[str, Any]]:
    """Normalize the three public Scientific State projections for delivery."""

    states = (dual, completion, reconciliation)
    if any(
        not isinstance(state, dict) or not isinstance(state.get("studies"), list)
        for state in states
    ) or (
        completion.get("schema_version")
        != "chemical-completion-project-state.v2"
        or reconciliation.get("schema_version")
        != "parse-reconciliation-project-state.v2"
    ):
        raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
    assert isinstance(dual, dict)
    assert isinstance(completion, dict)
    assert isinstance(reconciliation, dict)
    completion_by_id = {
        row.get("study_id"): row for row in completion["studies"] if isinstance(row, dict)
    }
    reconciliation_by_id = {
        row.get("study_id"): row for row in reconciliation["studies"] if isinstance(row, dict)
    }
    for row in completion["studies"]:
        _v2_completion_counters(row)
    rows: list[dict[str, Any]] = []
    for source in dual["studies"]:
        if not isinstance(source, dict):
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
        study_id = source.get("study_id")
        if not isinstance(study_id, str):
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
        chemical = completion_by_id.get(study_id)
        if not isinstance(chemical, dict):
            raise DualParseReleaseError("DUAL_PARSE_AUTHORITY_INVALID")
        registry = reconciliation_by_id.get(study_id, {})
        generic_lane = source.get("generic")
        chemical_lane = source.get("chemical")
        generic_lane = generic_lane if isinstance(generic_lane, dict) else {}
        chemical_lane = chemical_lane if isinstance(chemical_lane, dict) else {}
        generic_version = _first_value(
            source,
            "generic_version",
            "generic_binding_digest",
        ) or _first_value(
            generic_lane,
            "binding_digest",
            "parse_gate_digest",
            "source_truth_bundle_digest",
            "version_digest",
        )
        chemical_version = _first_value(
            source,
            "chemical_version",
            "chemical_state_digest",
            "chemical_import_digest",
        ) or _first_value(
            chemical_lane,
            "state_digest",
            "import_digest",
            "binding_digest",
            "version_digest",
        )
        molecule_count = chemical.get(
            "molecule_count", chemical.get("core_molecule_count")
        )
        honest_progressive = _first_value(
            chemical,
            "honest_progressive",
            "honest_progressive_summary",
        )
        if honest_progressive is None:
            honest_progressive = _first_value(
                chemical_lane,
                "honest_progressive",
                "honest_progressive_summary",
            )
        if honest_progressive is None:
            honest_progressive = _first_value(
                source,
                "honest_progressive",
                "honest_progressive_summary",
            )
        row = {
            "study_id": study_id,
            "source_tier": source.get("source_tier", source.get("tier")),
            "requires_chemical": source.get("requires_chemical") is True,
            "dual_source_binding_digest": _first_value(
                source, "dual_source_binding_digest", "binding_digest"
            ),
            "generic_status": source.get(
                "generic_status", generic_lane.get("status")
            ),
            "generic_version": generic_version,
            "chemical_status": source.get(
                "chemical_status", chemical_lane.get("status")
            ),
            "chemical_version": chemical_version,
            "chemical_completion_digest": _first_value(
                chemical,
                "chemical_completion_digest",
                "completion_digest",
                "gate_digest",
            ),
            "chemical_completion_status": chemical.get("status"),
            "reconciliation_digest": _first_value(
                registry, "registry_digest", "reconciliation_digest"
            ),
            "reconciliation_status": registry.get("status"),
            "content_result_status": source.get(
                "content_result_status", "current"
            ),
            "missing_name_count": chemical.get("missing_name_count"),
            "missing_resolved_smiles_count": chemical.get(
                "missing_resolved_smiles_count"
            ),
            "ai_authored_smiles_count": chemical.get("ai_authored_smiles_count"),
            "reaction_data_status": _first_value(
                source, "reaction_data_status"
            ) or chemical_lane.get("reaction_data_status", REACTION_UNAVAILABLE),
            "reaction_count": source.get(
                "reaction_count", chemical_lane.get("reaction_count")
            ),
            "unreviewed_element_molecule_count": chemical.get(
                "unreviewed_element_molecule_count", 0
            ),
        }
        if molecule_count is not None:
            row["molecule_count"] = molecule_count
        if honest_progressive is not None:
            row["honest_progressive"] = honest_progressive
        rows.append(row)
    return rows


def dual_parse_release_bindings(project: Path) -> dict[str, Any]:
    root = _project(project)
    rows = _current_authority_rows(root)
    study_ids = {
        str(row.get("study_id"))
        for row in rows
        if row.get("source_tier") == "core" or row.get("requires_chemical") is True
    }
    bindings = build_dual_parse_manuscript_bindings(rows, study_ids)
    payload = {
        "schema_version": "dual-parse-release-bindings.v1",
        "dual_parse_bindings": bindings,
    }
    payload["binding_digest"] = canonical_digest(payload)
    return payload


def _hard_fails_for_row(
    current: dict[str, Any] | None,
    frozen: dict[str, Any],
    *,
    allow_non_exact: bool = False,
) -> set[str]:
    hard_fails: set[str] = set()
    core = frozen["source_tier"] == "core"
    if current is None:
        hard_fails.update({"DUAL_PARSE_STALE", "DUAL_SOURCE_BINDING_MISMATCH"})
        if core:
            hard_fails.update(
                {
                    "CORE_GENERIC_PARSE_MISSING_OR_STALE",
                    "CORE_CHEMICAL_IMPORT_MISSING_OR_STALE",
                    "CHEMICAL_COMPLETION_INCOMPLETE",
                    "PARSE_RECONCILIATION_UNRESOLVED",
                }
            )
        return hard_fails
    try:
        current_binding = _binding_from_authority(current)
    except DualParseReleaseError:
        hard_fails.update({"DUAL_PARSE_STALE", "DUAL_SOURCE_BINDING_MISMATCH"})
    else:
        if current_binding != frozen:
            hard_fails.add("DUAL_PARSE_STALE")
    if current.get("dual_source_binding_digest") != frozen["dual_source_binding_digest"]:
        hard_fails.add("DUAL_SOURCE_BINDING_MISMATCH")
    if current.get("generic_status") != "current" or current.get("generic_version") != frozen["generic_version"]:
        if core:
            hard_fails.add("CORE_GENERIC_PARSE_MISSING_OR_STALE")
        hard_fails.add("DUAL_PARSE_STALE")
    try:
        counters = _v2_completion_counters(current)
    except DualParseReleaseError:
        hard_fails.add("CHEMICAL_COMPLETION_INCOMPLETE")
    else:
        if (
            not allow_non_exact
            and (
                counters["missing_name_count"]
                or counters["missing_resolved_smiles_count"]
            )
        ):
            hard_fails.add("CHEMICAL_COMPLETION_INCOMPLETE")
        if counters["ai_authored_smiles_count"]:
            hard_fails.add("AI_AUTHORED_SMILES")
    if frozen["requires_chemical"]:
        if current.get("chemical_status") != "current" or current.get("chemical_version") != frozen["chemical_version"]:
            if core:
                hard_fails.add("CORE_CHEMICAL_IMPORT_MISSING_OR_STALE")
            hard_fails.add("DUAL_PARSE_STALE")
        if (
            not allow_non_exact
            and (
            current.get("chemical_completion_status") != "current"
            or current.get("chemical_completion_digest")
            != frozen["chemical_completion_digest"]
            )
        ):
            hard_fails.update({"CHEMICAL_COMPLETION_INCOMPLETE", "DUAL_PARSE_STALE"})
        if (
            current.get("reconciliation_status") != "current"
            or current.get("reconciliation_digest") != frozen["reconciliation_digest"]
        ):
            hard_fails.update({"PARSE_RECONCILIATION_UNRESOLVED", "DUAL_PARSE_STALE"})
    if current.get("content_result_status", "current") != "current":
        hard_fails.add("STALE_DUAL_PARSE_CONTENT_RESULT")
    reaction_status = current.get("reaction_data_status", REACTION_UNAVAILABLE)
    reaction_count = current.get("reaction_count")
    if reaction_status == REACTION_UNAVAILABLE and reaction_count is not None:
        hard_fails.add("REACTION_ABSENCE_MISREPRESENTED")
    return hard_fails


def validate_dual_parse_release_bindings(
    project: Path,
    bindings: object,
    *,
    allow_non_exact: bool = False,
) -> dict[str, Any]:
    root = _project(project)
    if not isinstance(bindings, dict) or set(bindings) not in (
        {"dual_parse_bindings"},
        {"schema_version", "dual_parse_bindings", "binding_digest"},
    ):
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    rows = bindings.get("dual_parse_bindings")
    if not isinstance(rows, list) or not rows:
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    frozen = [_normalize_binding(row) for row in rows]
    if frozen != sorted(frozen, key=lambda row: row["study_id"]) or len(
        {row["study_id"] for row in frozen}
    ) != len(frozen):
        raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    if "schema_version" in bindings:
        if bindings.get("schema_version") != "dual-parse-release-bindings.v1":
            raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
        expected_digest = canonical_digest(
            {
                "schema_version": bindings["schema_version"],
                "dual_parse_bindings": bindings["dual_parse_bindings"],
            }
        )
        if bindings.get("binding_digest") != expected_digest:
            raise DualParseReleaseError("DUAL_PARSE_BINDING_INVALID")
    current_rows = _current_authority_rows(root)
    current_by_id = {
        row.get("study_id"): row for row in current_rows if isinstance(row, dict)
    }
    hard_fails: set[str] = set()
    for row in frozen:
        hard_fails.update(
            _hard_fails_for_row(
                current_by_id.get(row["study_id"]),
                row,
                allow_non_exact=allow_non_exact,
            )
        )
    return {
        "status": "current" if not hard_fails else "stale",
        "workflow_can_continue": not hard_fails,
        "hard_fails": sorted(hard_fails),
        "dual_parse_bindings": frozen,
    }


def _non_exact_manuscript_release_allowed(
    project: Path, lineage: dict[str, Any]
) -> bool:
    """Return true only when every manuscript-bound Evidence dependency is non-exact."""

    claim_bindings = lineage.get("claim_bindings")
    if not isinstance(claim_bindings, list) or not claim_bindings:
        return False
    try:
        from review_writer.project.paper_evidence import paper_evidence_state
        from review_writer.project.synthesis import synthesis_state

        evidence_rows = paper_evidence_state(project).get("rows", [])
        synthesis_rows = synthesis_state(project).get("rows", [])
    except (OSError, PaperEvidenceError, ValueError, KeyError, TypeError):
        return False
    evidence_by_id = {
        row.get("evidence_id"): row
        for row in evidence_rows
        if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)
    }
    synthesis_by_id = {
        row.get("synthesis_id"): row
        for row in synthesis_rows
        if isinstance(row, dict) and isinstance(row.get("synthesis_id"), str)
    }
    referenced_evidence: set[str] = set()
    for binding in claim_bindings:
        if not isinstance(binding, dict):
            return False
        direct = binding.get("paper_evidence_ids", [])
        if not isinstance(direct, list) or not all(isinstance(item, str) for item in direct):
            return False
        referenced_evidence.update(direct)
        synthesis_ids = binding.get("synthesis_ids", [])
        if not isinstance(synthesis_ids, list) or not all(isinstance(item, str) for item in synthesis_ids):
            return False
        for synthesis_id in synthesis_ids:
            claim = synthesis_by_id.get(synthesis_id)
            if not isinstance(claim, dict):
                return False
            for key in ("supporting_evidence_ids", "counter_evidence_ids"):
                values = claim.get(key, [])
                if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                    return False
                referenced_evidence.update(values)
    if not referenced_evidence:
        return False
    for evidence_id in referenced_evidence:
        row = evidence_by_id.get(evidence_id)
        dependencies = row.get("field_dependencies") if isinstance(row, dict) else None
        if not isinstance(dependencies, list):
            return False
        if EXACT_CHEMICAL_FIELD_DEPENDENCIES.intersection(dependencies):
            return False
    return True


def _awaiting_human_figure(project: Path) -> bool:
    path = project / "03_figures/synthesis_figure_placeholders.json"
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        return True
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    rows = value.get("placeholders") if isinstance(value, dict) else None
    return not isinstance(rows, list) or any(
        not isinstance(row, dict) or row.get("status") == "awaiting_human_figure"
        for row in rows
    )


def _honest_progressive_authority_summary(
    rows: object,
) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    summaries: list[dict[str, Any]] = []
    molecule_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        nested = row.get("honest_progressive")
        if isinstance(nested, dict):
            nested_molecules = nested.get("molecules", nested.get("rows"))
            if isinstance(nested_molecules, list):
                molecule_rows.extend(
                    item
                    for item in nested_molecules
                    if isinstance(item, dict)
                )
                continue
            try:
                normalized = honest_progressive_summary_from_projection(
                    nested, project_scope=True
                )
            except PaperEvidenceError as exc:
                raise DualParseReleaseError(exc.code) from exc
            if normalized is not None:
                summaries.append(normalized)
                continue
        direct_molecules = row.get("molecules", row.get("molecule_rows"))
        if isinstance(direct_molecules, list):
            molecule_rows.extend(
                item for item in direct_molecules if isinstance(item, dict)
            )
    if molecule_rows:
        try:
            inferred_core = (
                rows.get("core_molecule_count")
                if isinstance(rows, dict)
                else None
            )
            return build_honest_progressive_summary(
                molecule_rows,
                core_molecule_count=(
                    inferred_core
                    if isinstance(inferred_core, int) and not isinstance(inferred_core, bool)
                    else None
                ),
            )
        except PaperEvidenceError as exc:
            raise DualParseReleaseError(exc.code) from exc
    if not summaries:
        return None
    if len(summaries) == 1:
        return summaries[0]
    paper_denominators = [
        sum(
            coverage.get("core_molecule_count", 0)
            for coverage in item.get("paper_coverage", [])
            if isinstance(coverage, dict)
            and isinstance(coverage.get("core_molecule_count"), int)
            and not isinstance(coverage.get("core_molecule_count"), bool)
        )
        for item in summaries
    ]
    core = sum(paper_denominators) if paper_denominators and any(paper_denominators) else HONEST_PROGRESSIVE_PROJECT_CORE_MOLECULE_COUNT
    confirmed = sum(item["confirmed_count"] for item in summaries)
    provisional = sum(item["ai_provisional_count"] for item in summaries)
    if confirmed + provisional > core:
        raise DualParseReleaseError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    blocked = core - confirmed - provisional
    return {
        "route": HONEST_PROGRESSIVE_ROUTE,
        "availability": "available",
        "status": (
            "ready"
            if (confirmed + provisional) / core
            >= HONEST_PROGRESSIVE_COVERAGE_THRESHOLD
            else "needs_more_traceable_candidates"
        ),
        "core_molecule_count": core,
        "confirmed_count": confirmed,
        "ai_provisional_count": provisional,
        "blocked_count": blocked,
        "coverage_ratio": 1.0 if core == 0 else (confirmed + provisional) / core,
        "coverage_threshold": HONEST_PROGRESSIVE_COVERAGE_THRESHOLD,
        "coverage_sufficient": (
            True
            if core == 0
            else (confirmed + provisional) / core
            >= HONEST_PROGRESSIVE_COVERAGE_THRESHOLD
        ),
        "paper_coverage": [
            coverage
            for item in summaries
            for coverage in item["paper_coverage"]
        ],
        "uncertainty_statement": "; ".join(
            item["uncertainty_statement"] for item in summaries
        ),
        "gap_registry": [
            gap for item in summaries for gap in item["gap_registry"]
        ],
        "traceability": [
            trace for item in summaries for trace in item["traceability"]
        ],
        "actor_provenance_residual": [
            residual
            for item in summaries
            for residual in item["actor_provenance_residual"]
        ],
        "credits_status": "NOT_APPLICABLE_BY_CURRENT_SCOPE",
    }


def dual_parse_release_state(project: Path) -> dict[str, Any]:
    root = _project(project)
    lineage_path = root / "04_manuscript/manuscript_lineage.v2.json"
    try:
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        lineage = None
    if not isinstance(lineage, dict) or not isinstance(
        lineage.get("dual_parse_bindings"), list
    ):
        return {
            "route": HONEST_PROGRESSIVE_ROUTE,
            "dual_parse_status": "missing",
            "internal_release_ready": False,
            "expert_release_ready": False,
            "hard_fails": ["DUAL_PARSE_STALE"],
            "issues": [],
            "reaction_data_status": REACTION_UNAVAILABLE,
            "reaction_count": None,
            "credits_status": CREDITS_STATUS,
        }
    allow_non_exact = _non_exact_manuscript_release_allowed(root, lineage)
    try:
        binding_payload = {"dual_parse_bindings": lineage["dual_parse_bindings"]}
        if allow_non_exact:
            validation = validate_dual_parse_release_bindings(
                root, binding_payload, allow_non_exact=True
            )
        else:
            # Preserve the original two-argument call for existing test and
            # integration doubles that implement the strict validator shape.
            validation = validate_dual_parse_release_bindings(root, binding_payload)
        current_rows = _current_authority_rows(root)
    except DualParseReleaseError:
        return {
            "route": HONEST_PROGRESSIVE_ROUTE,
            "dual_parse_status": "stale",
            "internal_release_ready": False,
            "expert_release_ready": False,
            "hard_fails": ["DUAL_PARSE_STALE"],
            "issues": [],
            "reaction_data_status": REACTION_UNAVAILABLE,
            "reaction_count": None,
            "credits_status": CREDITS_STATUS,
        }
    hard_fails = set(validation["hard_fails"])
    honest_summary = _honest_progressive_authority_summary(current_rows)
    if honest_summary is not None:
        if honest_summary["coverage_sufficient"]:
            # Honest Progressive gaps are represented in the release surface,
            # not silently promoted to exact evidence or treated as a global
            # completion hard fail.
            hard_fails.difference_update(
                {"CHEMICAL_COMPLETION_INCOMPLETE", "AI_AUTHORED_SMILES"}
            )
        elif not allow_non_exact:
            hard_fails.add("HONEST_PROGRESSIVE_COVERAGE_BELOW_THRESHOLD")
    reaction_statuses = {
        row.get("reaction_data_status", REACTION_UNAVAILABLE)
        for row in current_rows
        if row.get("study_id")
        in {binding["study_id"] for binding in validation["dual_parse_bindings"]}
    }
    reaction_status = (
        "available" if reaction_statuses == {"available"} else REACTION_UNAVAILABLE
    )
    reaction_counts = [
        row.get("reaction_count")
        for row in current_rows
        if row.get("reaction_data_status") == "available"
    ]
    reaction_count = (
        sum(reaction_counts)
        if reaction_status == "available"
        and reaction_counts
        and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in reaction_counts)
        else None
    )
    issues: set[str] = set()
    if reaction_status == REACTION_UNAVAILABLE:
        issues.add("CHEMICAL_REACTION_DATA_UNAVAILABLE")
    if any(int(row.get("unreviewed_element_molecule_count", 0) or 0) > 0 for row in current_rows):
        issues.add("CHEMICAL_ELEMENTS_NOT_REVIEWED")
    internal_ready = not hard_fails
    awaiting_figure = _awaiting_human_figure(root)
    if awaiting_figure:
        issues.add("SYNTHESIS_FIGURE_PENDING")
    result: dict[str, Any] = {
        "route": HONEST_PROGRESSIVE_ROUTE,
        "dual_parse_status": validation["status"],
        "internal_release_ready": internal_ready,
        "expert_release_ready": internal_ready and not awaiting_figure,
        "hard_fails": sorted(hard_fails),
        "issues": sorted(issues),
        "reaction_data_status": reaction_status,
        "reaction_count": reaction_count,
        "credits_status": CREDITS_STATUS,
        "non_exact_release_allowed": allow_non_exact,
    }
    if honest_summary is not None:
        result.update(honest_summary)
        result["issues"] = sorted(
            set(result["issues"])
            | (
                {"HONEST_PROGRESSIVE_GAPS_PRESENT"}
                if honest_summary["blocked_count"]
                else set()
            )
        )
        result["hard_fails"] = sorted(hard_fails)
        result["internal_release_ready"] = not hard_fails
        result["expert_release_ready"] = not hard_fails and not awaiting_figure
    return result
