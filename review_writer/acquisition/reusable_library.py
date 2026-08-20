"""Read-only audit of reusable source and parse assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .manifest_identity import normalize_doi
from .manual_archive import (
    EMBEDDED_DOI_RE,
    PDF_IDENTITY_OUTPUT_BYTES,
    PDF_IDENTITY_TIMEOUT_SECONDS,
)


CANONICAL_ARTIFACT = "00_sources/reusable_library_audit.json"
REUSABLE_ASSET_NAMES = ("mineru", "text", "atom")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DOCUMENT_ROLES = frozenset({"MAIN", "SI"})
MAX_REUSABLE_ASSET_BYTES = 2 * 1024 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


class ReusableLibraryError(ValueError):
    """A reusable-library audit input is structurally invalid."""


def _is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _validate_asset_components(root: Path, path: Path) -> None:
    current = root
    for component in ("", *path.relative_to(root).parts):
        if component:
            current /= component
        try:
            if _is_link_or_reparse(current):
                raise ReusableLibraryError("reusable asset paths must not contain links")
        except FileNotFoundError:
            return


def _verified_asset_sha256(
    root: Path,
    path: Path,
    *,
    copy_to: Any | None = None,
) -> str | None:
    _validate_asset_components(root, path)
    try:
        handle = path.open("rb")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    with handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ReusableLibraryError("reusable assets must be regular files")
        _validate_asset_components(root, path)
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise ReusableLibraryError("reusable asset changed while opening") from exc
        if _is_link_or_reparse(path) or not os.path.samestat(opened, current):
            raise ReusableLibraryError("reusable asset changed while opening")
        if opened.st_size > MAX_REUSABLE_ASSET_BYTES:
            raise ReusableLibraryError("reusable asset exceeds bounded byte policy")
        digest = hashlib.sha256()
        size_bytes = 0
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            size_bytes += len(chunk)
            if size_bytes > MAX_REUSABLE_ASSET_BYTES:
                raise ReusableLibraryError("reusable asset exceeds bounded byte policy")
            digest.update(chunk)
            if copy_to is not None:
                copy_to.write(chunk)
        if size_bytes != opened.st_size:
            raise ReusableLibraryError("reusable asset changed during bounded read")
        _validate_asset_components(root, path)
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise ReusableLibraryError("reusable asset changed during bounded read") from exc
        if _is_link_or_reparse(path) or not os.path.samestat(opened, current):
            raise ReusableLibraryError("reusable asset changed during bounded read")
        return digest.hexdigest()


def _bounded_tool_text(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > PDF_IDENTITY_OUTPUT_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _observed_dois(text: str) -> set[str]:
    dois: set[str] = set()
    for match in EMBEDDED_DOI_RE.finditer(text):
        candidate = match.group(0).rstrip(".,;")
        while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        doi = normalize_doi(candidate)
        if doi is not None:
            dois.add(doi)
    return dois


def _pdf_identity_dois(pdf_path: Path) -> tuple[set[str], set[str]]:
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        return set(), set()
    page_text_path = pdf_path.with_name("first-page.txt")
    try:
        completed = subprocess.run(
            [
                pdftotext,
                "-f",
                "1",
                "-l",
                "1",
                "-enc",
                "UTF-8",
                str(pdf_path),
                str(page_text_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PDF_IDENTITY_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            return set(), set()
        page_text = _bounded_tool_text(page_text_path)
        metadata_text = ""
        pdfinfo = shutil.which("pdfinfo")
        if pdfinfo is not None:
            metadata_path = pdf_path.with_name("metadata.txt")
            with metadata_path.open("wb") as output:
                completed = subprocess.run(
                    [pdfinfo, "-enc", "UTF-8", str(pdf_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=PDF_IDENTITY_TIMEOUT_SECONDS,
                    check=False,
                )
            if completed.returncode == 0:
                metadata_text = _bounded_tool_text(metadata_path)
    except (OSError, subprocess.SubprocessError):
        return set(), set()

    return _observed_dois(page_text), _observed_dois(metadata_text)


def _verified_pdf_hash_and_dois(
    root: Path,
    path: Path,
) -> tuple[str | None, set[str], set[str]]:
    with tempfile.TemporaryDirectory(prefix="review-writer-reuse-identity-") as temporary:
        pdf_copy = Path(temporary) / "source.pdf"
        with pdf_copy.open("wb") as destination:
            digest = _verified_asset_sha256(root, path, copy_to=destination)
        if digest is None:
            return None, set(), set()
        page_dois, metadata_dois = _pdf_identity_dois(pdf_copy)
        return digest, page_dois, metadata_dois


def _safe_asset(root: Path, descriptor: Any) -> tuple[Path, dict[str, str]]:
    if not isinstance(descriptor, dict):
        raise ReusableLibraryError("asset descriptors must be objects")
    relative = descriptor.get("path")
    expected = descriptor.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ReusableLibraryError("asset paths must be nonempty relative paths")
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise ReusableLibraryError("asset sha256 values must be lowercase hexadecimal")
    root_resolved = Path(os.path.abspath(root))
    path = root_resolved / relative
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise ReusableLibraryError("asset paths must stay within the library root") from exc
    return path, {"path": relative, "sha256": expected}


def _document_role(value: Any, *, subject: str) -> str:
    if not isinstance(value, str) or value not in DOCUMENT_ROLES:
        raise ReusableLibraryError(f"{subject} document_role must be MAIN or SI")
    return value


def _request_identity(request: Any) -> tuple[str, str | None, str | None, str]:
    if not isinstance(request, dict):
        raise ReusableLibraryError("requests must be objects")
    study_id = request.get("study_id")
    if not isinstance(study_id, str) or not study_id.strip():
        raise ReusableLibraryError("requests require a nonempty study_id")
    doi = normalize_doi(request.get("doi"))
    supplied_doi = request.get("doi")
    if supplied_doi is not None and doi is None:
        raise ReusableLibraryError("request DOI is invalid")
    pdf_sha256 = request.get("pdf_sha256")
    if pdf_sha256 is not None and (
        not isinstance(pdf_sha256, str) or not SHA256_RE.fullmatch(pdf_sha256)
    ):
        raise ReusableLibraryError("request pdf_sha256 is invalid")
    if doi is None and pdf_sha256 is None:
        raise ReusableLibraryError("requests require a DOI or pdf_sha256")
    return study_id.strip(), doi, pdf_sha256, _document_role(
        request.get("document_role"), subject="request"
    )


def _record_identity(record: Any) -> tuple[str, str | None, str, str]:
    if not isinstance(record, dict):
        raise ReusableLibraryError("library records must be objects")
    library_id = record.get("library_id")
    if not isinstance(library_id, str) or not library_id.strip():
        raise ReusableLibraryError("library records require a nonempty library_id")
    doi = normalize_doi(record.get("doi"))
    if record.get("doi") is not None and doi is None:
        raise ReusableLibraryError("library record DOI is invalid")
    pdf = record.get("pdf")
    if not isinstance(pdf, dict) or not isinstance(pdf.get("sha256"), str):
        raise ReusableLibraryError("library records require a PDF descriptor")
    pdf_sha256 = pdf["sha256"]
    if not SHA256_RE.fullmatch(pdf_sha256):
        raise ReusableLibraryError("library PDF sha256 is invalid")
    return library_id.strip(), doi, pdf_sha256, _document_role(
        record.get("document_role"), subject="library record"
    )


def canonical_reusable_requests(requests: Any) -> list[dict[str, Any]]:
    """Normalize and order the request set used by audit and prepare."""

    if not isinstance(requests, list):
        raise ReusableLibraryError("requests must be a list")
    canonical: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for request in requests:
        study_id, doi, pdf_sha256, document_role = _request_identity(request)
        identity = (study_id, document_role)
        if identity in seen:
            raise ReusableLibraryError("request study/document roles must be unique")
        seen.add(identity)
        row: dict[str, Any] = {
            "document_role": document_role,
            "study_id": study_id,
        }
        if doi is not None:
            row["doi"] = doi
        if pdf_sha256 is not None:
            row["pdf_sha256"] = pdf_sha256
        canonical.append(row)
    return sorted(
        canonical,
        key=lambda row: (
            row["study_id"],
            row["document_role"],
            row.get("doi", ""),
            row.get("pdf_sha256", ""),
        ),
    )


def reusable_requests_from_downloads(downloads: Any) -> list[dict[str, Any]]:
    if not isinstance(downloads, list):
        raise ReusableLibraryError("acquisition manifest downloads must be a list")
    requests: list[dict[str, Any]] = []
    for row in downloads:
        if not isinstance(row, dict):
            raise ReusableLibraryError("acquisition manifest downloads must be objects")
        request = {
            key: row[key]
            for key in ("study_id", "doi", "document_role")
            if key in row
        }
        if row.get("expected_sha256") is not None:
            request["pdf_sha256"] = row["expected_sha256"]
        requests.append(request)
    return canonical_reusable_requests(requests)


def reusable_request_set_digest(requests: Any) -> str:
    canonical = canonical_reusable_requests(requests)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def audit_reusable_library(
    *,
    requests: list[dict[str, Any]],
    library_root: Path | str,
    library_records: list[dict[str, Any]],
    required_parser_contract: str,
) -> dict[str, Any]:
    """Return verified reuse candidates without changing the source library."""

    if not isinstance(requests, list) or not isinstance(library_records, list):
        raise ReusableLibraryError("requests and library_records must be lists")
    if not isinstance(required_parser_contract, str) or not required_parser_contract:
        raise ReusableLibraryError("required_parser_contract must be nonempty")
    root = Path(os.path.abspath(library_root))
    canonical_requests = canonical_reusable_requests(requests)
    indexed = [(record, *_record_identity(record)) for record in library_records]
    results: list[dict[str, Any]] = []

    for request in canonical_requests:
        study_id, doi, pdf_sha256, document_role = _request_identity(request)
        no_match_reason = "NO_LIBRARY_MATCH"
        if pdf_sha256 is not None:
            matches = [entry for entry in indexed if entry[3] == pdf_sha256]
            match_basis = "PDF_SHA256"
            if not matches and doi is not None and any(entry[2] == doi for entry in indexed):
                no_match_reason = "REQUEST_PDF_HASH_MISMATCH"
        elif doi is not None:
            matches = [entry for entry in indexed if entry[2] == doi]
            match_basis = "DOI"
        else:
            raise AssertionError("canonical reusable requests require an identity")
        base = {
            "study_id": study_id,
            "document_role": document_role,
            "status": "NOT_REUSABLE",
            "reason": no_match_reason,
            "match_basis": match_basis,
            "library_id": None,
            "assets": {},
        }
        if not matches:
            results.append(base)
            continue
        role_matches = [entry for entry in matches if entry[4] == document_role]
        if not role_matches:
            base["reason"] = "DOCUMENT_ROLE_MISMATCH"
            results.append(base)
            continue
        matches = role_matches
        if len(matches) != 1:
            base.update(status="UNRESOLVED", reason="AMBIGUOUS_LIBRARY_MATCH")
            results.append(base)
            continue

        record, library_id, _, record_pdf_sha256, _ = matches[0]
        base["library_id"] = library_id
        pdf_path, pdf_projection = _safe_asset(root, record["pdf"])
        if pdf_sha256 is None:
            measured_pdf_sha256, page_dois, metadata_dois = _verified_pdf_hash_and_dois(
                root,
                pdf_path,
            )
        else:
            measured_pdf_sha256 = _verified_asset_sha256(root, pdf_path)
            page_dois = set()
            metadata_dois = set()
        if measured_pdf_sha256 != pdf_projection["sha256"]:
            base["reason"] = "PDF_HASH_MISMATCH"
            results.append(base)
            continue
        if pdf_sha256 is not None and measured_pdf_sha256 != pdf_sha256:
            base["reason"] = "REQUEST_PDF_HASH_MISMATCH"
            results.append(base)
            continue
        if pdf_sha256 is None:
            if not page_dois:
                base.update(status="UNRESOLVED", reason="PDF_IDENTITY_UNRESOLVED")
                results.append(base)
                continue
            if len(page_dois) != 1:
                base.update(status="UNRESOLVED", reason="PDF_IDENTITY_AMBIGUOUS")
                results.append(base)
                continue
            if doi not in page_dois:
                base["reason"] = "PDF_DOI_MISMATCH"
                results.append(base)
                continue
            if metadata_dois and metadata_dois != {doi}:
                base.update(status="UNRESOLVED", reason="PDF_IDENTITY_AMBIGUOUS")
                results.append(base)
                continue
        base["assets"] = {"pdf": pdf_projection}
        if record.get("parser_contract") != required_parser_contract:
            base.update(status="PDF_ONLY", reason="PARSER_CONTRACT_MISMATCH")
            results.append(base)
            continue

        reusable = record.get("reusable_assets")
        if not isinstance(reusable, dict):
            base.update(status="PDF_ONLY", reason="DERIVED_ASSET_INVALID")
            results.append(base)
            continue
        projected = dict(base["assets"])
        valid = True
        binding_valid = True
        for name in REUSABLE_ASSET_NAMES:
            descriptor_value = reusable.get(name)
            if not isinstance(descriptor_value, dict) or (
                descriptor_value.get("source_pdf_sha256") != record_pdf_sha256
                or descriptor_value.get("parser_contract") != required_parser_contract
            ):
                binding_valid = False
                break
            try:
                asset_path, descriptor = _safe_asset(root, descriptor_value)
            except ReusableLibraryError:
                valid = False
                break
            if _verified_asset_sha256(root, asset_path) != descriptor["sha256"]:
                valid = False
                break
            projected[name] = {
                **descriptor,
                "source_pdf_sha256": record_pdf_sha256,
                "parser_contract": required_parser_contract,
            }
        if not binding_valid:
            base.update(status="PDF_ONLY", reason="DERIVED_ASSET_BINDING_MISMATCH")
            results.append(base)
            continue
        if not valid:
            base.update(status="PDF_ONLY", reason="DERIVED_ASSET_INVALID")
            results.append(base)
            continue
        base.update(status="REUSABLE", reason=None, assets=projected)
        results.append(base)

    return {
        "schema_version": "reusable-library-audit.v1",
        "canonical_artifact": CANONICAL_ARTIFACT,
        "non_mutating": True,
        "request_set_digest": reusable_request_set_digest(canonical_requests),
        "required_parser_contract": required_parser_contract,
        "results": results,
    }
