"""Fresh, source-only bootstrap and Generic MinerU binding for dual-parse projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from review_writer.project.input_provenance import (
    InputProvenanceError,
    validate_corpus_study_count,
)
from review_writer.project.parse_quality import ParseQualityError, write_parse_quality_gate
from review_writer.project.source_truth import (
    SourceTruthError,
    canonical_digest,
    write_source_truth_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REQUEST_SCHEMA = REPO_ROOT / "schemas/project/dual_parse_bootstrap_request.v1.schema.json"
LEGACY_REQUEST_SCHEMA = REPO_ROOT / "schemas/project/legacy_dual_parse_bootstrap_request.v1.schema.json"
CORPUS_REQUEST_SCHEMA = REPO_ROOT / "schemas/project/corpus_manifest.v1.schema.json"


class DualParseBootstrapError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_ANCHOR_DIRECTORY = ".dual_parse_authority"
CANONICAL_ANCHOR_SCHEMA_VERSION = "dual-parse-canonical-anchor.v1"
ACQUISITION_RECEIPT_RELATIVE_PATH = "00_sources/acquisition_final_receipt.json"
CANONICAL_ANCHOR_KEYS = frozenset(
    {
        "schema_version",
        "project_id",
        "project_relative_path",
        "receipt_relative_path",
        "receipt",
        "receipt_sha256",
        "anchor_digest",
    }
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: object) -> str:
    return canonical_digest(value)


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute path without resolving any symlink component."""
    return Path(os.path.abspath(os.fspath(path)))


def _safe_existing_path(path: Path, code: str, *, directory: bool) -> Path:
    lexical = _lexical_absolute(path)
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise DualParseBootstrapError(code) from exc
        if stat.S_ISLNK(mode):
            raise DualParseBootstrapError(code)
        if current != lexical and not stat.S_ISDIR(mode):
            raise DualParseBootstrapError(code)
    try:
        mode = lexical.lstat().st_mode
    except OSError as exc:
        raise DualParseBootstrapError(code) from exc
    if directory and not stat.S_ISDIR(mode):
        raise DualParseBootstrapError(code)
    if not directory and not stat.S_ISREG(mode):
        raise DualParseBootstrapError(code)
    return lexical


def _ensure_safe_directory(path: Path, code: str) -> Path:
    lexical = _lexical_absolute(path)
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            try:
                current.mkdir()
            except OSError as exc:
                raise DualParseBootstrapError(code) from exc
            mode = current.lstat().st_mode
        except OSError as exc:
            raise DualParseBootstrapError(code) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise DualParseBootstrapError(code)
    return lexical


def _safe_project_path(
    project: Path, relative: Path, code: str, *, directory: bool
) -> Path:
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise DualParseBootstrapError(code)
    root = _safe_existing_path(project, code, directory=True)
    return _safe_existing_path(root.joinpath(*relative.parts), code, directory=directory)


def _canonical_anchor_path(project: Path) -> Path:
    return project.parent / CANONICAL_ANCHOR_DIRECTORY / f"{project.name}.json"


def _canonical_anchor_body(project: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": CANONICAL_ANCHOR_SCHEMA_VERSION,
        "project_id": project.name,
        "project_relative_path": project.name,
        "receipt_relative_path": ACQUISITION_RECEIPT_RELATIVE_PATH,
        "receipt": receipt,
        "receipt_sha256": _canonical_digest(receipt),
    }


def _path_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


def _write_json_exclusive(path: Path, value: object, code: str) -> tuple[int, int]:
    temporary: Path | None = None
    anchor_identity: tuple[int, int] | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        anchor_identity = _path_identity(path)
        return anchor_identity
    except OSError as exc:
        if anchor_identity is not None:
            try:
                if _path_identity(path) == anchor_identity:
                    path.unlink()
            except OSError:
                pass
        raise DualParseBootstrapError(code) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _remove_owned_anchor(path: Path, expected_identity: tuple[int, int]) -> None:
    """Remove our anchor without unlinking a competing replacement."""
    try:
        if _path_identity(path) != expected_identity:
            return
    except OSError:
        return

    quarantine: Path | None = None
    try:
        descriptor, quarantine_name = tempfile.mkstemp(
            prefix=f".{path.name}.rollback.", dir=path.parent
        )
        os.close(descriptor)
        quarantine = Path(quarantine_name)
        quarantine.unlink()
        os.rename(path, quarantine)
        moved_identity = _path_identity(quarantine)
        if moved_identity == expected_identity:
            quarantine.unlink()
            quarantine = None
            return
        try:
            os.link(quarantine, path)
        except FileExistsError:
            if _path_identity(path) != moved_identity:
                return
        quarantine.unlink()
        quarantine = None
    except OSError:
        return
    finally:
        if quarantine is not None:
            try:
                if _path_identity(quarantine) == expected_identity:
                    quarantine.unlink()
            except OSError:
                pass


def _read_canonical_anchor(project: Path) -> dict[str, Any]:
    path = _safe_existing_path(
        _canonical_anchor_path(project),
        "ACQUISITION_FINAL_RECEIPT_INVALID",
        directory=False,
    )
    try:
        anchor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID") from exc
    if (
        not isinstance(anchor, dict)
        or set(anchor) != CANONICAL_ANCHOR_KEYS
        or anchor.get("schema_version") != CANONICAL_ANCHOR_SCHEMA_VERSION
        or anchor.get("project_id") != project.name
        or anchor.get("project_relative_path") != project.name
        or anchor.get("receipt_relative_path") != ACQUISITION_RECEIPT_RELATIVE_PATH
        or not isinstance(anchor.get("receipt"), dict)
        or not isinstance(anchor.get("receipt_sha256"), str)
        or SHA256_RE.fullmatch(anchor["receipt_sha256"]) is None
        or not isinstance(anchor.get("anchor_digest"), str)
        or SHA256_RE.fullmatch(anchor["anchor_digest"]) is None
    ):
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
    body = {key: value for key, value in anchor.items() if key != "anchor_digest"}
    if (
        anchor["receipt_sha256"] != _canonical_digest(anchor["receipt"])
        or anchor["anchor_digest"] != _canonical_digest(body)
    ):
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
    return anchor


def _read_bound_receipt(project: Path) -> dict[str, Any]:
    receipt_path = _safe_project_path(
        project,
        Path(ACQUISITION_RECEIPT_RELATIVE_PATH),
        "ACQUISITION_FINAL_RECEIPT_INVALID",
        directory=False,
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID") from exc
    if not isinstance(receipt, dict):
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
    return receipt


def _validate_bound_receipt(receipt: dict[str, Any], anchor: dict[str, Any]) -> None:
    if (
        receipt != anchor["receipt"]
        or _canonical_digest(receipt) != anchor["receipt_sha256"]
    ):
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")


def _generic_source_pdf_sha256(row: dict[str, Any]) -> str:
    source_pdf_sha256 = row.get("source_pdf_sha256")
    pdf_sha256 = row.get("pdf_sha256")
    if (
        not isinstance(source_pdf_sha256, str)
        or SHA256_RE.fullmatch(source_pdf_sha256) is None
        or (
            "pdf_sha256" in row
            and (
                not isinstance(pdf_sha256, str)
                or SHA256_RE.fullmatch(pdf_sha256) is None
                or pdf_sha256 != source_pdf_sha256
            )
        )
    ):
        raise DualParseBootstrapError("GENERIC_SOURCE_BINDING_INVALID")
    return source_pdf_sha256


def _receipt_documents(
    study: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return the current MAIN/SI descriptors in a stable order."""
    documents: list[tuple[str, dict[str, Any]]] = []
    main = study.get("main_pdf")
    if isinstance(main, dict):
        documents.append(("MAIN", main))
    si = study.get("si_pdf")
    if isinstance(si, dict):
        documents.append(("SI", si))
    if not documents or documents[0][0] != "MAIN":
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
    return documents


def _generic_manifest_key(value: object) -> str:
    """Normalize a MinerU path without allowing traversal or platform aliases."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise DualParseBootstrapError("GENERIC_MANIFEST_INVALID")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DualParseBootstrapError("GENERIC_MANIFEST_INVALID")
    return path.as_posix()


def _generic_row_for_descriptor(
    relative_pdf: str,
    by_path: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
    *,
    variable_n: bool,
) -> dict[str, Any]:
    """Resolve one Generic row; variable-N requires a role-safe full path."""
    row = by_path.get(relative_pdf)
    if row is not None:
        return row
    if variable_n:
        raise DualParseBootstrapError("GENERIC_BINDING_MISSING")
    candidates = by_name.get(Path(relative_pdf).name, [])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise DualParseBootstrapError("GENERIC_BINDING_AMBIGUOUS")
    raise DualParseBootstrapError("GENERIC_BINDING_MISSING")


def _source_identity(source_id: str, document_role: str) -> str:
    """Keep MAIN and SI text-layer identities distinct inside one study."""
    return source_id if document_role == "MAIN" else f"{source_id}__SI"


def _validate_generic_source_bindings(
    project: Path,
    studies: list[dict[str, Any]],
    by_path: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
    *,
    variable_n: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Validate every current MAIN/SI PDF and Generic row before staging."""
    bindings: dict[tuple[str, str], dict[str, Any]] = {}
    expected_documents = 0
    for study in studies:
        source_id = study.get("source_id") if isinstance(study, dict) else None
        study_id = study.get("study_id") if isinstance(study, dict) else None
        if not isinstance(source_id, str) or not isinstance(study_id, str):
            raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
        for document_role, descriptor in _receipt_documents(study):
            expected_documents += 1
            relative_pdf = descriptor.get("path")
            expected_hash = descriptor.get("sha256")
            if not isinstance(relative_pdf, str) or not isinstance(expected_hash, str):
                raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
            relative_pdf = _generic_manifest_key(relative_pdf)
            pdf = _safe_project_path(
                project,
                Path("00_sources") / relative_pdf,
                "ACQUISITION_FINAL_RECEIPT_INVALID",
                directory=False,
            )
            if _sha256(pdf) != expected_hash:
                raise DualParseBootstrapError("SOURCE_PDF_HASH_MISMATCH")
            row = _generic_row_for_descriptor(
                relative_pdf,
                by_path,
                by_name,
                variable_n=variable_n,
            )
            if _generic_source_pdf_sha256(row) != expected_hash:
                raise DualParseBootstrapError("GENERIC_SOURCE_PDF_HASH_MISMATCH")
            declared_role = row.get("document_role")
            if declared_role is not None and declared_role != document_role:
                raise DualParseBootstrapError("GENERIC_DOCUMENT_ROLE_MISMATCH")
            declared_study = row.get("study_id")
            if declared_study is not None and declared_study != study_id:
                raise DualParseBootstrapError("GENERIC_STUDY_BINDING_MISMATCH")
            declared_source = row.get("source_id")
            expected_source = _source_identity(source_id, document_role)
            if declared_source is not None and declared_source != expected_source:
                raise DualParseBootstrapError("GENERIC_SOURCE_ID_MISMATCH")
            bindings[(study_id, document_role)] = row
    if len(by_path) != expected_documents:
        raise DualParseBootstrapError("GENERIC_BINDING_AMBIGUOUS")
    return bindings


def _regular_input(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not path.is_symlink()


def _regular_external_input(path: Path) -> bool:
    """Reject symlinks and non-regular components in an external PDF path."""
    try:
        if path.is_absolute():
            current = Path(path.anchor)
            components = path.parts[1:]
        else:
            current = Path.cwd()
            components = path.parts
        if not components:
            return False
        root_mode = current.lstat().st_mode
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            return False
        for index, component in enumerate(components):
            current /= component
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                return False
            if index == len(components) - 1:
                return stat.S_ISREG(mode) and os.access(current, os.R_OK)
            if not stat.S_ISDIR(mode):
                return False
    except (OSError, ValueError):
        return False
    return False


def _validate_request(request: object, *, corpus: bool) -> dict[str, Any]:
    if corpus and isinstance(request, dict) and isinstance(request.get("sources"), list):
        try:
            validate_corpus_study_count(len(request["sources"]))
        except InputProvenanceError as exc:
            raise DualParseBootstrapError(exc.code) from exc
    schema_path = CORPUS_REQUEST_SCHEMA if corpus else LEGACY_REQUEST_SCHEMA
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualParseBootstrapError("BOOTSTRAP_SCHEMA_INVALID") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(request), key=lambda error: list(error.path))
    if errors or not isinstance(request, dict):
        raise DualParseBootstrapError("BOOTSTRAP_REQUEST_INVALID")
    rows = request["sources"]
    if len({row["study_id"] for row in rows}) != len(rows):
        raise DualParseBootstrapError("DUPLICATE_STUDY_ID")
    if len({row["source_id"] for row in rows}) != len(rows):
        raise DualParseBootstrapError("DUPLICATE_SOURCE_ID")
    return request


def _validated_sources(request: dict[str, Any]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    def validate_pdf(
        path_value: object,
        expected_value: object,
        *,
        mismatch_code: str,
    ) -> dict[str, Any]:
        if not isinstance(path_value, str) or not _regular_external_input(Path(path_value)):
            raise DualParseBootstrapError("SOURCE_PDF_INVALID")
        path = Path(path_value)
        try:
            prefix = path.read_bytes()[:5]
            observed = _sha256(path)
            size = path.stat().st_size
        except OSError as exc:
            raise DualParseBootstrapError("SOURCE_PDF_INVALID") from exc
        if prefix != b"%PDF-":
            raise DualParseBootstrapError("SOURCE_PDF_INVALID")
        if observed != expected_value:
            raise DualParseBootstrapError(mismatch_code)
        if observed in seen_hashes:
            raise DualParseBootstrapError("DUPLICATE_SOURCE_PDF")
        seen_hashes.add(observed)
        return {"input": path, "sha256": observed, "size_bytes": size}

    for row in request["sources"]:
        main = validate_pdf(
            row["pdf_input_path"],
            row["expected_pdf_sha256"],
            mismatch_code="SOURCE_PDF_HASH_MISMATCH",
        )
        entry = {**row, **main}
        if "si_pdf_input_path" in row or "expected_si_pdf_sha256" in row:
            if "si_pdf_input_path" not in row or "expected_si_pdf_sha256" not in row:
                raise DualParseBootstrapError("SOURCE_SI_INVALID")
            entry["si_input"] = validate_pdf(
                row["si_pdf_input_path"],
                row["expected_si_pdf_sha256"],
                mismatch_code="SOURCE_SI_HASH_MISMATCH",
            )
        validated.append(entry)
    return validated


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _bootstrap_project(review_root: Path, request: object, *, corpus: bool) -> Path:
    """Validate every PDF, stage a source-only project, and publish exactly once."""
    normalized = _validate_request(request, corpus=corpus)
    sources = _validated_sources(normalized)
    review_root = Path(review_root)
    target = review_root / normalized["project_id"]
    if os.path.lexists(target):
        raise DualParseBootstrapError("TARGET_EXISTS")
    review_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=review_root))
    published = False
    anchor_path = _canonical_anchor_path(target)
    authority_directory = anchor_path.parent
    authority_directory_preexisting = os.path.lexists(authority_directory)
    authority_directory_created = False
    anchor_identity: tuple[int, int] | None = None
    try:
        studies: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        identities: list[dict[str, Any]] = []
        for row in sources:
            descriptors: dict[str, dict[str, Any]] = {}
            document_inputs = [
                ("MAIN", row["input"], row["sha256"], row["size_bytes"], f"papers/{row['source_id']}.pdf")
            ]
            if corpus:
                si = row.get("si_input")
                if not isinstance(si, dict):
                    raise DualParseBootstrapError("SOURCE_SI_INVALID")
                document_inputs.append(
                    (
                        "SI",
                        si["input"],
                        si["sha256"],
                        si["size_bytes"],
                        f"supplements/imported/{row['source_id']}.pdf",
                    )
                )
            for document_role, input_path, expected_hash, expected_size, relative_pdf in document_inputs:
                destination = staging / "00_sources" / relative_pdf
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not _regular_external_input(input_path):
                    raise DualParseBootstrapError(
                        "SOURCE_SI_INVALID" if document_role == "SI" else "SOURCE_PDF_INVALID"
                    )
                try:
                    shutil.copy2(input_path, destination)
                except OSError as exc:
                    if not _regular_external_input(input_path):
                        raise DualParseBootstrapError(
                            "SOURCE_SI_INVALID" if document_role == "SI" else "SOURCE_PDF_INVALID"
                        ) from exc
                    raise
                try:
                    copied_sha256 = _sha256(destination)
                    copied_size_bytes = destination.stat().st_size
                except OSError as exc:
                    raise DualParseBootstrapError("BOOTSTRAP_WRITE_FAILED") from exc
                if copied_sha256 != expected_hash or copied_size_bytes != expected_size:
                    raise DualParseBootstrapError(
                        "SOURCE_SI_HASH_MISMATCH" if document_role == "SI" else "SOURCE_PDF_HASH_MISMATCH"
                    )
                descriptors[document_role] = {
                    "path": relative_pdf,
                    "sha256": copied_sha256,
                    "size_bytes": copied_size_bytes,
                }
            studies.append({
                "study_id": row["study_id"], "source_id": row["source_id"],
                "doi": row["doi"], "title": row["title"], "tier": row["tier"],
                "document_role": "MAIN", "status": "ACQUIRED",
                "main_pdf": descriptors["MAIN"],
                **({"si_pdf": descriptors["SI"]} if corpus else {}),
            })
            candidates.append({
                "candidate_id": row["study_id"], "study_id": row["study_id"],
                "source_id": row["source_id"], "doi": row["doi"], "title": row["title"],
                "tier": row["tier"], "document_role": "MAIN",
            })
            identities.append({
                "candidate_id": row["study_id"], "study_id": row["study_id"],
                "source_id": row["source_id"], "doi": row["doi"], "title": row["title"],
                "document_role": "MAIN", "verdict": "PASS",
            })
        _write_json(staging / "00_brief/review_state.json", {
            "schema_version": "vertical-review-state.v1", "project_id": target.name,
            "brief": normalized["brief"], "current_stage": "source_parse",
            "status": "in_progress", "blockers": [],
            "counts": {"sources": len(sources), "evidence": 0, "claims": 0},
        })
        _write_json(staging / "00_discovery/candidate_pool.json", {
            "schema_version": "candidate-pool.v1", "candidates": candidates,
        })
        receipt = {
            "schema_version": "acquisition-final-receipt.v1",
            "corpus_kind": (
                "authoritative_variable_n" if corpus else "legacy_three_paper"
            ),
            "variable_n": corpus,
            "study_count": len(studies),
            "studies": studies,
        }
        _write_json(staging / ACQUISITION_RECEIPT_RELATIVE_PATH, receipt)
        _write_json(staging / "00_sources/source_identity_audit.json", {
            "schema_version": "source-identity-audit.v1", "results": identities,
        })
        _write_json(staging / "00_sources/source_coverage.json", {
            "schema_version": "source-coverage.v1",
            "canonical_artifact": "00_sources/source_coverage.json",
            "studies": [
                {
                    "study_id": row["study_id"],
                    "available_roles": ["MAIN", "SI"] if corpus else ["MAIN"],
                    "main_policy": "REQUIRED",
                    "si_policy": (
                        "REQUIRED"
                        if corpus or row["tier"] == "core"
                        else "NOT_REQUIRED"
                    ),
                    "study_status": "READY" if corpus or row["tier"] != "core" else "PARTIAL",
                    "blocked_claim_ids": [],
                    "blocking_reasons": (
                        [] if corpus or row["tier"] != "core" else ["SI_REQUIRED_FOR_DECLARED_CLAIMS"]
                    ),
                    "limitations": [],
                }
                for row in sources
            ],
        })
        _ensure_safe_directory(authority_directory, "BOOTSTRAP_WRITE_FAILED")
        authority_directory_created = not authority_directory_preexisting
        if os.path.lexists(anchor_path):
            raise DualParseBootstrapError("TARGET_EXISTS")
        anchor_body = _canonical_anchor_body(target, receipt)
        anchor_identity = _write_json_exclusive(
            anchor_path,
            {**anchor_body, "anchor_digest": _canonical_digest(anchor_body)},
            "BOOTSTRAP_WRITE_FAILED",
        )
        os.replace(staging, target)
        published = True
        return target
    except DualParseBootstrapError:
        raise
    except OSError as exc:
        raise DualParseBootstrapError("BOOTSTRAP_WRITE_FAILED") from exc
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
            if anchor_identity is not None:
                _remove_owned_anchor(anchor_path, anchor_identity)
            if authority_directory_created:
                try:
                    authority_directory.rmdir()
                except OSError:
                    pass


def bootstrap_dual_parse_project(review_root: Path, request: object) -> Path:
    """Legacy three-paper source-only bootstrap retained for fixture regression."""

    return _bootstrap_project(review_root, request, corpus=False)


def bootstrap_corpus_project(review_root: Path, request: object) -> Path:
    """Bootstrap a fresh authoritative 20–40 study source-only project."""

    return _bootstrap_project(review_root, request, corpus=True)


def bind_generic_parse_outputs(project: Path, mineru_output: Path) -> dict[str, object]:
    """Bind only fresh Generic MinerU output matching current project PDF bytes."""
    project = _safe_existing_path(
        Path(project), "ACQUISITION_FINAL_RECEIPT_INVALID", directory=True
    )
    output = Path(mineru_output).resolve(strict=True)
    if os.path.lexists(project / "01_evidence"):
        raise DualParseBootstrapError("GENERIC_BINDING_TARGET_EXISTS")
    anchor = _read_canonical_anchor(project)
    receipt = _read_bound_receipt(project)
    studies = receipt.get("studies")
    if not isinstance(studies, list) or not studies:
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
    expected_count = len(studies)
    if receipt.get("study_count") != expected_count:
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
    variable_n = receipt.get("variable_n") is True
    if variable_n:
        try:
            validate_corpus_study_count(expected_count)
        except InputProvenanceError as exc:
            raise DualParseBootstrapError(exc.code) from exc
    elif receipt.get("variable_n") is not False or expected_count != 3:
        raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
    expected_generic_count = expected_count * 2 if variable_n else expected_count
    try:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualParseBootstrapError("GENERIC_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict):
        raise DualParseBootstrapError("GENERIC_MANIFEST_INVALID")
    completed = manifest.get("completed")
    failed = manifest.get("failed")
    if (
        manifest.get("completed_count") != expected_generic_count
        or manifest.get("failed_count") != 0
        or not isinstance(completed, list)
        or len(completed) != expected_generic_count
        or failed != []
    ):
        raise DualParseBootstrapError("GENERIC_PARSE_INCOMPLETE")
    settings = manifest.get("settings")
    if not isinstance(settings, dict) or settings.get("language") != "en":
        raise DualParseBootstrapError("GENERIC_SETTINGS_INVALID")
    if settings.get("model_version") != "vlm" or settings.get("enable_formula") is not True or settings.get("enable_table") is not True:
        raise DualParseBootstrapError("GENERIC_SETTINGS_INVALID")
    if settings.get("ocr") is not False:
        raise DualParseBootstrapError("GENERIC_SETTINGS_INVALID")

    by_path: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in completed:
        if not isinstance(row, dict) or row.get("state") != "done":
            raise DualParseBootstrapError("GENERIC_PARSE_INCOMPLETE")
        relative = row.get("relative_pdf_path")
        slug = row.get("slug")
        if not isinstance(relative, str) or not isinstance(slug, str) or not slug or "/" in slug or "\\" in slug:
            raise DualParseBootstrapError("GENERIC_MANIFEST_INVALID")
        relative = _generic_manifest_key(relative)
        _generic_source_pdf_sha256(row)
        if relative in by_path:
            raise DualParseBootstrapError("GENERIC_BINDING_AMBIGUOUS")
        by_path[relative] = row
        by_name.setdefault(Path(relative).name, []).append(row)

    bindings = _validate_generic_source_bindings(
        project,
        studies,
        by_path,
        by_name,
        variable_n=variable_n,
    )
    _validate_bound_receipt(receipt, anchor)

    staging_parent = Path(
        tempfile.mkdtemp(prefix=f".{project.name}.generic.", dir=project.parent)
    )
    stage_root = staging_parent / project.name
    published = False
    try:
        shutil.copytree(project, stage_root, dirs_exist_ok=True, copy_function=shutil.copy2)
        evidence = stage_root / "01_evidence"
        mineru_root = evidence / "mineru"
        parse_root = evidence / "parses"
        layer_root = evidence / "text_layers"
        mineru_rows: list[dict[str, Any]] = []
        parse_rows: list[dict[str, Any]] = []
        layer_rows: list[dict[str, Any]] = []
        for study in studies:
            source_id = study.get("source_id") if isinstance(study, dict) else None
            study_id = study.get("study_id") if isinstance(study, dict) else None
            if not isinstance(source_id, str) or not isinstance(study_id, str):
                raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
            for document_role, descriptor in _receipt_documents(study):
                relative_pdf = descriptor.get("path")
                expected_hash = descriptor.get("sha256")
                if not isinstance(relative_pdf, str) or not isinstance(expected_hash, str):
                    raise DualParseBootstrapError("ACQUISITION_FINAL_RECEIPT_INVALID")
                relative_pdf = _generic_manifest_key(relative_pdf)
                pdf = _safe_project_path(
                    project,
                    Path("00_sources") / relative_pdf,
                    "ACQUISITION_FINAL_RECEIPT_INVALID",
                    directory=False,
                )
                if _sha256(pdf) != expected_hash:
                    raise DualParseBootstrapError("SOURCE_PDF_HASH_MISMATCH")
                row = bindings[(study_id, document_role)]
                if _generic_source_pdf_sha256(row) != expected_hash:
                    raise DualParseBootstrapError("GENERIC_SOURCE_PDF_HASH_MISMATCH")
                slug = row["slug"]
                source_extracted = output / "extracted" / slug
                source_markdown = output / "markdown" / f"{slug}.md"
                source_zip = output / "raw_zips" / f"{slug}.zip"
                required = [
                    source_markdown,
                    source_zip,
                    source_extracted / "full.md",
                    source_extracted / "layout.json",
                ]
                if (
                    not all(_regular_input(path) for path in required)
                    or not source_extracted.is_dir()
                    or source_extracted.is_symlink()
                ):
                    raise DualParseBootstrapError("GENERIC_OUTPUT_INVALID")
                for path in source_extracted.rglob("*"):
                    if path.is_symlink() or (path.is_file() and not _regular_input(path)):
                        raise DualParseBootstrapError("GENERIC_OUTPUT_INVALID")
                v1 = sorted(
                    path
                    for path in source_extracted.glob("*_content_list.json")
                    if not path.name.endswith("_content_list_v2.json")
                )
                v2 = sorted(source_extracted.glob("*_content_list_v2.json"))
                if len(v1) != 1 or len(v2) != 1:
                    raise DualParseBootstrapError("GENERIC_OUTPUT_INVALID")
                try:
                    content_v2 = json.loads(v2[0].read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise DualParseBootstrapError("GENERIC_OUTPUT_INVALID") from exc
                if (
                    not isinstance(content_v2, list)
                    or not content_v2
                    or not all(isinstance(page, list) for page in content_v2)
                ):
                    raise DualParseBootstrapError("GENERIC_OUTPUT_INVALID")
                page_count = len(content_v2)
                destination_extracted = parse_root / "extracted" / slug
                destination_extracted.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_extracted, destination_extracted, copy_function=shutil.copy2)
                for destination in (
                    mineru_root / "markdown" / f"{slug}.md",
                    parse_root / "markdown" / f"{slug}.md",
                ):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_markdown, destination)
                raw_destination = mineru_root / "raw_zips" / f"{slug}.zip"
                raw_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_zip, raw_destination)
                layer_source_id = _source_identity(source_id, document_role)
                reading = layer_root / f"{layer_source_id}.reading.txt"
                layout = layer_root / f"{layer_source_id}.layout.txt"
                reading.parent.mkdir(parents=True, exist_ok=True)
                markdown_text = source_markdown.read_text(encoding="utf-8")
                layer_text = markdown_text.rstrip() + "\n" + ("\f" * page_count)
                reading.write_text(layer_text, encoding="utf-8")
                layout.write_text(layer_text, encoding="utf-8")
                layer_rows.append({
                    "study_id": study_id,
                    "source_id": layer_source_id,
                    "document_role": document_role,
                    "pdf_name": Path(relative_pdf).name,
                    "pdf_sha256": expected_hash,
                    "page_count": page_count,
                    "reading_order_path": reading.name,
                    "reading_order_sha256": _sha256(reading),
                    "reading_order_method": "generic-mineru-canonical-reading-order",
                    "layout_path": layout.name,
                    "layout_sha256": _sha256(layout),
                    "layout_method": "generic-mineru-layout-visual-locator-only",
                })
                common = {
                    "data_id": row.get("data_id"),
                    "slug": slug,
                    "state": "done",
                    "study_id": study_id,
                    "source_id": layer_source_id,
                    "document_role": document_role,
                    "relative_pdf_path": relative_pdf,
                    "source_pdf_sha256": expected_hash,
                    "markdown_copy": f"markdown/{slug}.md",
                }
                if isinstance(row.get("raw_zip_sha256"), str):
                    common["raw_zip_sha256"] = row["raw_zip_sha256"]
                mineru_rows.append(common)
                parse_rows.append({
                    **common,
                    "full_md": f"extracted/{slug}/full.md",
                    "extracted_dir": f"extracted/{slug}",
                })
        _write_json(mineru_root / "manifest.json", {
            "schema_version": "mineru-parse-manifest.v1", "settings": settings,
            "completed_count": expected_generic_count, "failed_count": 0, "completed": mineru_rows, "failed": [],
        })
        _write_json(parse_root / "manifest.json", {
            "schema_version": "mineru-batch-parse.v1", "settings": settings,
            "completed_count": expected_generic_count, "failed_count": 0, "completed": parse_rows, "failed": [],
        })
        _write_json(layer_root / "text_layers.manifest.json", {
            "schema_version": "pdf-text-layers.v1", "sources": layer_rows,
        })
        for study in studies:
            write_source_truth_bundle(stage_root, study["study_id"])
            write_parse_quality_gate(stage_root, study["study_id"])
        os.replace(evidence, project / "01_evidence")
        published = True
    except DualParseBootstrapError:
        raise
    except (SourceTruthError, ParseQualityError) as exc:
        raise DualParseBootstrapError(exc.code) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DualParseBootstrapError("GENERIC_BINDING_FAILED") from exc
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
    return {
        "status": "bound", "completed_count": expected_generic_count, "failed_count": 0,
        "source_truth_count": expected_count, "parse_quality_count": expected_count,
    }
