"""Bounded deterministic import of one researcher-provided source ZIP."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unicodedata
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, BinaryIO

from .manifest_identity import canonical_acquisition_save_as, normalize_doi
from .public_corpus import (
    ManifestError,
    _matches_format,
    _now,
    _portable_target_components,
    _preflight_manifest,
    _preflight_metadata_destinations,
    _is_link_or_reparse,
    _safe_target,
    _sha256_bytes,
    _sha256_file,
    _stage_bytes,
    _validate_existing_target_boundary,
    _validate_target_parent,
)


DEFAULT_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_MEMBERS = 1_000
DEFAULT_MAX_MEMBER_BYTES = 250 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
HARD_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
HARD_MAX_MEMBERS = 2_000
HARD_MAX_MEMBER_BYTES = 512 * 1024 * 1024
HARD_MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
RECEIPT_FILENAME = "manual_import_receipt.json"
FORMAT_EXTENSIONS = {"PDF": "pdf", "DOCX": "docx", "XLSX": "xlsx"}
CHUNK_BYTES = 1024 * 1024
DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
PDF_IDENTITY_OUTPUT_BYTES = 2 * 1024 * 1024
PDF_IDENTITY_TIMEOUT_SECONDS = 10
EMBEDDED_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
SOURCE_TRANSACTION_LOCK = threading.RLock()


class ManualArchiveError(ManifestError):
    """The manual archive or its deterministic mapping is unsafe."""


class _BoundedArchiveDescriptor:
    def __init__(self, handle: BinaryIO, size_bytes: int) -> None:
        self._handle = handle
        self._size_bytes = size_bytes
        self.seekable = handle.seekable

    def fileno(self) -> int:
        return self._handle.fileno()

    def tell(self) -> int:
        return self._handle.tell()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self.tell() + offset
        elif whence == os.SEEK_END:
            position = self._size_bytes + offset
        else:
            raise ValueError("unsupported archive seek mode")
        if position < 0:
            raise OSError("negative seek position")
        if position > self._size_bytes:
            raise ManualArchiveError("archive read escaped the bounded descriptor snapshot")
        return self._handle.seek(position, os.SEEK_SET)

    def read(self, size: int = -1) -> bytes:
        remaining = self._size_bytes - self.tell()
        if remaining < 0:
            raise ManualArchiveError("archive read escaped the bounded descriptor snapshot")
        bounded_size = remaining if size is None or size < 0 else min(size, remaining)
        return self._handle.read(bounded_size)


def _validate_archive_descriptor_snapshot(handle: BinaryIO, initial: os.stat_result) -> None:
    current = os.fstat(handle.fileno())
    if (
        current.st_size != initial.st_size
        or current.st_mtime_ns != initial.st_mtime_ns
        or current.st_ctime_ns != initial.st_ctime_ns
    ):
        raise ManualArchiveError("archive changed after bounded read")


def _validate_policy(
    max_archive_bytes: int,
    max_members: int,
    max_member_bytes: int,
    max_total_bytes: int,
) -> None:
    for name, value, ceiling in (
        ("max_archive_bytes", max_archive_bytes, HARD_MAX_ARCHIVE_BYTES),
        ("max_members", max_members, HARD_MAX_MEMBERS),
        ("max_member_bytes", max_member_bytes, HARD_MAX_MEMBER_BYTES),
        ("max_total_bytes", max_total_bytes, HARD_MAX_TOTAL_BYTES),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ManualArchiveError(f"{name} must be a positive integer")
        if value > ceiling:
            raise ManualArchiveError(f"{name} exceeds the hard safety ceiling")


def _normalized_portable_path(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    try:
        _portable_target_components(normalized)
    except ManifestError as exc:
        raise ManualArchiveError("archive alias is not portable") from exc
    return normalized.casefold()


def _normalized_safe_basename(value: Any) -> str:
    if not isinstance(value, str):
        raise ManualArchiveError("archive aliases must be safe basenames")
    normalized = unicodedata.normalize("NFKC", value)
    try:
        components = _portable_target_components(normalized)
    except ManifestError as exc:
        raise ManualArchiveError("archive aliases must be safe basenames") from exc
    if len(components) != 1:
        raise ManualArchiveError("archive aliases must be safe basenames")
    return normalized.casefold()


def _validate_member(info: zipfile.ZipInfo) -> tuple[str, str] | None:
    name = info.filename
    if not isinstance(name, str) or not name:
        raise ManualArchiveError("archive contains an unsafe member name")
    normalized = unicodedata.normalize("NFKC", name)
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized):
        raise ManualArchiveError("archive contains an unsafe member name")
    if "\\" in normalized or normalized.startswith("/") or DRIVE_PREFIX_RE.match(normalized):
        raise ManualArchiveError("archive contains an unsafe member name")
    is_directory = info.is_dir()
    candidate = normalized[:-1] if is_directory and normalized.endswith("/") else normalized
    if not candidate or "//" in candidate:
        raise ManualArchiveError("archive contains an unsafe member name")
    try:
        components = _portable_target_components(candidate)
    except ManifestError as exc:
        raise ManualArchiveError("archive contains an unsafe member name") from exc
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ManualArchiveError("archive contains a link or special member")
    if (is_directory and file_type == stat.S_IFREG) or (not is_directory and file_type == stat.S_IFDIR):
        raise ManualArchiveError("archive member type is inconsistent")
    normalized_name = "/".join(components).casefold()
    if is_directory:
        return None
    return normalized_name, components[-1].casefold()


def _read_member_bounded(
    source: BinaryIO,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    total_so_far: int,
    destination: BinaryIO | None = None,
    digest: Any | None = None,
) -> tuple[int, int]:
    member_bytes = 0
    total_bytes = total_so_far
    while True:
        chunk = source.read(CHUNK_BYTES)
        if not chunk:
            break
        member_bytes += len(chunk)
        total_bytes += len(chunk)
        if member_bytes > max_member_bytes or total_bytes > max_total_bytes:
            raise ManualArchiveError("archive exceeds bounded byte policy")
        if destination is not None:
            destination.write(chunk)
        if digest is not None:
            digest.update(chunk)
    return member_bytes, total_bytes


def _preflight_archive(
    archive: zipfile.ZipFile,
    *,
    max_members: int,
    max_member_bytes: int,
    max_total_bytes: int,
) -> list[dict[str, Any]]:
    infos = archive.infolist()
    if len(infos) > max_members:
        raise ManualArchiveError("archive exceeds bounded member policy")
    declared_total = 0
    seen_names: set[str] = set()
    members: list[dict[str, Any]] = []
    for index, info in enumerate(infos):
        if info.flag_bits & 0x1:
            raise ManualArchiveError("encrypted archive members are forbidden")
        if info.file_size < 0 or info.file_size > max_member_bytes:
            raise ManualArchiveError("archive exceeds bounded member byte policy")
        declared_total += info.file_size
        if declared_total > max_total_bytes:
            raise ManualArchiveError("archive exceeds bounded total byte policy")
        normalized = _validate_member(info)
        duplicate_key = unicodedata.normalize("NFKC", info.filename.rstrip("/")).casefold()
        if duplicate_key in seen_names:
            raise ManualArchiveError("archive contains duplicate normalized member names")
        seen_names.add(duplicate_key)
        if normalized is not None:
            members.append(
                {
                    "index": index,
                    "member_id": f"MEMBER-{len(members) + 1:04d}",
                    "member_display_name": unicodedata.normalize(
                        "NFKC", info.filename.rsplit("/", 1)[-1]
                    ),
                    "normalized_name": normalized[0],
                    "normalized_basename": normalized[1],
                }
            )

    actual_total = 0
    try:
        for record in members:
            info = infos[record["index"]]
            digest = hashlib.sha256()
            with archive.open(info, "r") as source:
                actual_member, actual_total = _read_member_bounded(
                    source,
                    max_member_bytes=max_member_bytes,
                    max_total_bytes=max_total_bytes,
                    total_so_far=actual_total,
                    digest=digest,
                )
            if actual_member != info.file_size:
                raise ManualArchiveError("archive member size is inconsistent")
            record["sha256"] = digest.hexdigest()
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise ManualArchiveError("archive is invalid or unsupported") from exc
    return members


def _url_basename_alias(row: dict[str, Any], expected_extension: str) -> str | None:
    path = urllib.parse.urlsplit(row["url"]).path
    basename = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    if not basename or not basename.casefold().endswith(f".{expected_extension}"):
        return None
    try:
        return _normalized_safe_basename(basename)
    except ManualArchiveError:
        return None


def _build_alias_indexes(
    prepared: list[dict[str, Any]],
) -> tuple[
    dict[str, set[int]],
    dict[str, set[int]],
    dict[str, set[int]],
    dict[str, set[int]],
    dict[str, set[int]],
]:
    full_aliases: dict[str, set[int]] = defaultdict(set)
    basename_aliases: dict[str, set[int]] = defaultdict(set)
    doi_filename_aliases: dict[str, set[int]] = defaultdict(set)
    doi_rows: dict[str, set[int]] = defaultdict(set)
    title_rows: dict[str, set[int]] = defaultdict(set)
    for row_index, item in enumerate(prepared):
        row = item["row"]
        full_aliases[_normalized_portable_path(row["target_path"])].add(row_index)
        basename_aliases[_normalized_safe_basename(row["target_path"].rsplit("/", 1)[-1])].add(row_index)
        save_as = canonical_acquisition_save_as(row["download_id"], item["expected_format"])
        basename_aliases[_normalized_safe_basename(save_as)].add(row_index)
        url_alias = _url_basename_alias(row, FORMAT_EXTENSIONS[item["expected_format"]])
        if url_alias is not None:
            basename_aliases[url_alias].add(row_index)
        archive_names = row.get("archive_names", [])
        if not isinstance(archive_names, list):
            raise ManualArchiveError("archive_names must be a list of safe basenames")
        for alias in archive_names:
            basename_aliases[_normalized_safe_basename(alias)].add(row_index)
        doi = normalize_doi(row.get("doi"))
        if doi is not None and item["expected_format"] == "PDF":
            doi_rows[doi].add(row_index)
            doi_alias = f"{doi.replace('/', '_')}.pdf"
            doi_filename_aliases[_normalized_safe_basename(doi_alias)].add(row_index)
        title = _normalized_title(row.get("title"))
        if title is not None and item["expected_format"] == "PDF":
            title_rows[title].add(row_index)
    return full_aliases, basename_aliases, doi_filename_aliases, doi_rows, title_rows


def _normalized_title(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(character.casefold() if character.isalnum() else " " for character in normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized or None


def _bounded_tool_text(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > PDF_IDENTITY_OUTPUT_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _member_pdf_identity(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_member_bytes: int,
) -> tuple[set[str], str | None]:
    """Extract identity through bounded real-PDF tools and fail closed."""

    if not info.filename.casefold().endswith(".pdf"):
        return set(), None
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        return set(), None
    try:
        with tempfile.TemporaryDirectory(prefix="review-writer-pdf-identity-") as temporary:
            temp_root = Path(temporary)
            pdf_path = temp_root / "source.pdf"
            with archive.open(info, "r") as source, pdf_path.open("wb") as destination:
                size_bytes, _ = _read_member_bounded(
                    source,
                    max_member_bytes=max_member_bytes,
                    max_total_bytes=max_member_bytes,
                    total_so_far=0,
                    destination=destination,
                )
            if size_bytes != info.file_size:
                raise ManualArchiveError("archive member size is inconsistent")

            page_text_path = temp_root / "first-page.txt"
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
                page_text = _bounded_tool_text(page_text_path) if completed.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                page_text = ""

            metadata_text = ""
            pdfinfo = shutil.which("pdfinfo")
            if pdfinfo is not None:
                metadata_path = temp_root / "metadata.txt"
                try:
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
                    metadata_text = ""
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise ManualArchiveError("archive is invalid or unsupported") from exc
    except OSError:
        return set(), None

    text = f"{metadata_text}\n{page_text}"
    dois: set[str] = set()
    for match in EMBEDDED_DOI_RE.finditer(text):
        candidate = match.group(0).rstrip(".,;")
        while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        doi = normalize_doi(candidate)
        if doi is not None:
            dois.add(doi)
    title: str | None = None
    match = re.search(r"(?im)^Title:\s*(?P<title>[^\r\n]{1,1000})", metadata_text)
    if match:
        title = _normalized_title(match.group("title"))
    return dois, title


def _record_unresolved(
    unresolved: list[dict[str, Any]],
    *,
    reason: str,
    rows: set[int],
    prepared: list[dict[str, Any]],
    members: list[dict[str, Any]],
    member_indexes: set[int],
) -> None:
    download_ids = sorted(prepared[row]["row"]["download_id"] for row in rows)
    for member_index in sorted(member_indexes):
        member = members[member_index]
        entry = {
            "reason": reason,
            "member_id": member["member_id"],
            "member_display_name": member["member_display_name"],
            "download_ids": download_ids,
        }
        if entry not in unresolved:
            unresolved.append(entry)


def _apply_match_layer(
    *,
    members: list[dict[str, Any]],
    prepared: list[dict[str, Any]],
    candidates_by_member: dict[int, set[int]],
    basis: str,
    ambiguity_reason: str,
    resolved: dict[int, int],
    match_basis: dict[int, str],
    ambiguous_rows: set[int],
    unresolved: list[dict[str, Any]],
) -> None:
    used_members = set(resolved.values())
    available: dict[int, set[int]] = {}
    for member_index, candidates in candidates_by_member.items():
        if member_index in used_members:
            continue
        remaining = {row for row in candidates if row not in resolved and row not in ambiguous_rows}
        if remaining:
            available[member_index] = remaining

    row_members: dict[int, set[int]] = defaultdict(set)
    for member_index, candidates in available.items():
        if len(candidates) > 1:
            ambiguous_rows.update(candidates)
            _record_unresolved(
                unresolved,
                reason=ambiguity_reason,
                rows=candidates,
                prepared=prepared,
                members=members,
                member_indexes={member_index},
            )
            continue
        row_members[next(iter(candidates))].add(member_index)
    for row_index, member_indexes in row_members.items():
        if row_index in ambiguous_rows:
            continue
        if len(member_indexes) > 1:
            ambiguous_rows.add(row_index)
            _record_unresolved(
                unresolved,
                reason=ambiguity_reason,
                rows={row_index},
                prepared=prepared,
                members=members,
                member_indexes=member_indexes,
            )
            continue
        member_index = next(iter(member_indexes))
        resolved[row_index] = member_index
        match_basis[row_index] = basis


def _map_members(
    members: list[dict[str, Any]],
    prepared: list[dict[str, Any]],
    full_aliases: dict[str, set[int]],
    basename_aliases: dict[str, set[int]],
    doi_filename_aliases: dict[str, set[int]],
    doi_rows: dict[str, set[int]],
    title_rows: dict[str, set[int]],
) -> tuple[dict[int, int], dict[int, str], set[int], list[dict[str, Any]], int]:
    resolved: dict[int, int] = {}
    match_basis: dict[int, str] = {}
    ambiguous_rows: set[int] = set()
    unresolved: list[dict[str, Any]] = []

    layers = (
        ({index: full_aliases.get(member["normalized_name"], set()) for index, member in enumerate(members)}, "EXACT_ALIAS", "ARCHIVE_ALIAS_AMBIGUOUS"),
        ({index: basename_aliases.get(member["normalized_basename"], set()) for index, member in enumerate(members)}, "EXACT_ALIAS", "ARCHIVE_ALIAS_AMBIGUOUS"),
    )
    for candidates, basis, reason in layers:
        _apply_match_layer(
            members=members,
            prepared=prepared,
            candidates_by_member=candidates,
            basis=basis,
            ambiguity_reason=reason,
            resolved=resolved,
            match_basis=match_basis,
            ambiguous_rows=ambiguous_rows,
            unresolved=unresolved,
        )

    doi_filename_candidates = {
        index: doi_filename_aliases.get(member["normalized_basename"], set())
        for index, member in enumerate(members)
    }
    embedded_doi_candidates: dict[int, set[int]] = {}
    title_candidates: dict[int, set[int]] = {}
    for member_index, member in enumerate(members):
        embedded_rows: set[int] = set()
        for doi in member.get("embedded_dois", set()):
            embedded_rows.update(doi_rows.get(doi, set()))
        embedded_doi_candidates[member_index] = embedded_rows
        titles = {
            title
            for title in (
                member.get("embedded_title"),
                _normalized_title(member["normalized_basename"].rsplit(".", 1)[0]),
            )
            if title
        }
        matched_title_rows: set[int] = set()
        for title in titles:
            matched_title_rows.update(title_rows.get(title, set()))
        title_candidates[member_index] = matched_title_rows

        filename_rows = doi_filename_candidates.get(member_index, set())
        if filename_rows and embedded_rows and filename_rows != embedded_rows:
            conflict_rows = filename_rows | embedded_rows
            ambiguous_rows.update(conflict_rows)
            doi_filename_candidates[member_index] = set()
            embedded_doi_candidates[member_index] = set()
            _record_unresolved(
                unresolved,
                reason="CONFLICTING_MEMBER_IDENTITY",
                rows=conflict_rows,
                prepared=prepared,
                members=members,
                member_indexes={member_index},
            )

    for candidates, basis, reason in (
        (doi_filename_candidates, "DOI_FILENAME", "AMBIGUOUS_DOI_FILENAME"),
        (embedded_doi_candidates, "PDF_DOI", "AMBIGUOUS_PDF_DOI"),
        (title_candidates, "PDF_TITLE", "AMBIGUOUS_PDF_TITLE"),
    ):
        _apply_match_layer(
            members=members,
            prepared=prepared,
            candidates_by_member=candidates,
            basis=basis,
            ambiguity_reason=reason,
            resolved=resolved,
            match_basis=match_basis,
            ambiguous_rows=ambiguous_rows,
            unresolved=unresolved,
        )

    matched_members = set(resolved.values())
    unresolved_member_ids = {row["member_id"] for row in unresolved}
    supported_extensions = {f".{extension}" for extension in FORMAT_EXTENSIONS.values()}
    for member_index, member in enumerate(members):
        member_extension = Path(member["member_display_name"]).suffix.casefold()
        if (
            member_index not in matched_members
            and member["member_id"] not in unresolved_member_ids
            and member_extension in supported_extensions
        ):
            compatible_rows = {
                row_index
                for row_index, item in enumerate(prepared)
                if row_index not in resolved
                and row_index not in ambiguous_rows
                and member_extension == f".{FORMAT_EXTENSIONS[item['expected_format']]}"
            }
            _record_unresolved(
                unresolved,
                reason="NO_DETERMINISTIC_MATCH",
                rows=compatible_rows,
                prepared=prepared,
                members=members,
                member_indexes={member_index},
            )
    return resolved, match_basis, ambiguous_rows, unresolved, len(members) - len(matched_members)


def _apply_member_overrides(
    member_overrides: dict[str, str] | None,
    *,
    members: list[dict[str, Any]],
    prepared: list[dict[str, Any]],
    resolved: dict[int, int],
    match_basis: dict[int, str],
    ambiguous_rows: set[int],
    unresolved: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if member_overrides is None:
        return []
    if not isinstance(member_overrides, dict):
        raise ManualArchiveError("member_overrides must be an object")
    if any(
        not isinstance(member_id, str)
        or not re.fullmatch(r"MEMBER-\d{4}", member_id)
        or not isinstance(download_id, str)
        or not download_id
        for member_id, download_id in member_overrides.items()
    ):
        raise ManualArchiveError("member override identifiers are invalid")
    if len(set(member_overrides.values())) != len(member_overrides):
        raise ManualArchiveError("member overrides must select unique downloads")

    members_by_id = {member["member_id"]: index for index, member in enumerate(members)}
    rows_by_download_id = {
        item["row"]["download_id"]: index for index, item in enumerate(prepared)
    }
    unresolved_by_member: dict[str, dict[str, Any]] = {}
    for entry in unresolved:
        member_id = entry["member_id"]
        if member_id in unresolved_by_member:
            raise ManualArchiveError("unresolved member identity is ambiguous")
        unresolved_by_member[member_id] = entry

    selections: list[tuple[str, str, int, int, set[int]]] = []
    for member_id, download_id in member_overrides.items():
        entry = unresolved_by_member.get(member_id)
        row_index = rows_by_download_id.get(download_id)
        member_index = members_by_id.get(member_id)
        if (
            entry is None
            or row_index is None
            or member_index is None
            or download_id not in entry.get("download_ids", [])
            or row_index in resolved
        ):
            raise ManualArchiveError("member override is not a listed unresolved candidate")
        candidate_rows = {
            rows_by_download_id[candidate_id]
            for candidate_id in entry["download_ids"]
            if candidate_id in rows_by_download_id
        }
        selections.append((member_id, download_id, member_index, row_index, candidate_rows))

    selected_rows = {selection[3] for selection in selections}
    if len(selected_rows) != len(selections):
        raise ManualArchiveError("member overrides must select unique downloads")
    for member_id, download_id, member_index, row_index, candidate_rows in selections:
        ambiguous_rows.difference_update(candidate_rows)
        resolved[row_index] = member_index
        match_basis[row_index] = "USER_CONFIRMED"
        unresolved[:] = [entry for entry in unresolved if entry["member_id"] != member_id]
        for entry in unresolved:
            if download_id in entry["download_ids"]:
                entry["download_ids"] = [
                    candidate for candidate in entry["download_ids"] if candidate != download_id
                ]

    return [
        {"member_id": member_id, "download_id": download_id}
        for member_id, download_id in sorted(member_overrides.items())
    ]


def _base_result(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    return {
        "download_id": row["download_id"],
        "study_id": row["study_id"],
        "document_role": row["document_role"],
        "expected_format": item["expected_format"],
        "target_path": row["target_path"],
        "status": None,
        "reason": None,
        "match_basis": None,
        "sha256": None,
        "size_bytes": None,
    }


def _inspect_existing(item: dict[str, Any], output_root: Path) -> dict[str, Any] | None:
    target = item["target"]
    if not target.exists() and not target.is_symlink():
        return None
    result = _base_result(item)
    _validate_existing_target_boundary(output_root, target)
    if not target.is_file():
        result.update(status="INVALID_EXISTING", reason="EXISTING_TARGET_NOT_REGULAR")
        return result
    actual_sha256 = _sha256_file(target)
    if item["expected_sha256"] is not None and actual_sha256 != item["expected_sha256"]:
        result.update(status="INVALID_EXISTING", reason="EXISTING_HASH_MISMATCH")
    elif not _matches_format(target, item["expected_format"]):
        result.update(status="INVALID_EXISTING", reason="EXISTING_FORMAT_MISMATCH")
    else:
        result.update(
            status="VERIFIED_EXISTING",
            sha256=actual_sha256,
            size_bytes=target.stat().st_size,
        )
    return result


def _stage_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    item: dict[str, Any],
    output_root: Path,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    total_so_far: int,
) -> tuple[Path, str, int, int]:
    row = item["row"]
    target, _ = _safe_target(output_root, row["target_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target, _ = _safe_target(output_root, row["target_path"])
    _validate_target_parent(output_root, target)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".manual-import", dir=target.parent
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = None
            with archive.open(info, "r") as source:
                size_bytes, total_bytes = _read_member_bounded(
                    source,
                    max_member_bytes=max_member_bytes,
                    max_total_bytes=max_total_bytes,
                    total_so_far=total_so_far,
                    destination=destination,
                    digest=digest,
                )
            destination.flush()
            os.fsync(destination.fileno())
        if size_bytes != info.file_size:
            raise ManualArchiveError("archive member size is inconsistent")
        result = temporary
        temporary = None
        return result, digest.hexdigest(), size_bytes, total_bytes
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise ManualArchiveError("archive is invalid or unsupported") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stage_receipt(output_root: Path, receipt: dict[str, Any]) -> tuple[Path, Path]:
    destination = output_root / RECEIPT_FILENAME
    _validate_target_parent(output_root, destination)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ManualArchiveError("manual import receipt destination is unsafe")
    payload = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    staged = _stage_bytes(destination, payload)
    return destination, staged


def _publication_root_binding(output_root: Path) -> tuple[os.stat_result, os.stat_result]:
    lexical_root = Path(os.path.abspath(output_root))
    try:
        root_stat = os.lstat(lexical_root)
        parent_stat = os.lstat(lexical_root.parent)
        if _is_link_or_reparse(lexical_root) or _is_link_or_reparse(lexical_root.parent):
            raise ManualArchiveError("manual import publication root is unsafe")
    except OSError as exc:
        raise ManualArchiveError("manual import publication root is unsafe") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ManualArchiveError("manual import publication root is unsafe")
    return root_stat, parent_stat


def _validate_publication_root(
    output_root: Path,
    binding: tuple[os.stat_result, os.stat_result],
) -> None:
    lexical_root = Path(os.path.abspath(output_root))
    expected_root, expected_parent = binding
    try:
        current_root = os.lstat(lexical_root)
        current_parent = os.lstat(lexical_root.parent)
        if (
            _is_link_or_reparse(lexical_root)
            or _is_link_or_reparse(lexical_root.parent)
            or not os.path.samestat(expected_root, current_root)
            or not os.path.samestat(expected_parent, current_parent)
        ):
            raise ManualArchiveError("manual import publication root is unsafe")
    except OSError as exc:
        raise ManualArchiveError("manual import publication root is unsafe") from exc
    try:
        _preflight_metadata_destinations(output_root)
    except ManifestError as exc:
        raise ManualArchiveError("manual import publication root is unsafe") from exc


def _publish_transaction(
    output_root: Path,
    prepared: list[dict[str, Any]],
    staged_targets: dict[int, tuple[Path, str, int]],
    receipt: dict[str, Any],
) -> None:
    publication_binding = _publication_root_binding(output_root)
    receipt_destination, staged_receipt = _stage_receipt(output_root, receipt)
    published: list[tuple[Path, Path]] = []
    try:
        try:
            _validate_publication_root(output_root, publication_binding)
            for row_index, (staged_path, _, _) in staged_targets.items():
                item = prepared[row_index]
                _validate_publication_root(output_root, publication_binding)
                target, _ = _safe_target(output_root, item["row"]["target_path"])
                _validate_target_parent(output_root, target)
                if target.exists() or target.is_symlink():
                    raise ManualArchiveError("target appeared during atomic publication")
                os.link(staged_path, target)
                published.append((target, staged_path))
            _validate_publication_root(output_root, publication_binding)
            if receipt_destination.is_symlink() or (
                receipt_destination.exists() and not receipt_destination.is_file()
            ):
                raise ManualArchiveError("manual import receipt destination is unsafe")
            os.replace(staged_receipt, receipt_destination)
            staged_receipt = None
        except BaseException as publication_error:
            rollback_error: BaseException | None = None
            for target, staged_path in reversed(published):
                try:
                    try:
                        target_exists = target.exists() or target.is_symlink()
                    except OSError:
                        target_exists = True
                    if not target_exists:
                        continue
                    if not os.path.samefile(staged_path, target):
                        raise OSError("published target changed before rollback")
                    target.unlink()
                except BaseException as exc:
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise OSError("manual import target rollback failed") from publication_error
            raise
    finally:
        if staged_receipt is not None:
            staged_receipt.unlink(missing_ok=True)


def _import_manual_archive_unlocked(
    manifest_path: Path | str,
    archive_path: Path | str,
    output_root: Path | str,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    member_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Import uniquely mapped files from one bounded ZIP without network access."""

    _validate_policy(max_archive_bytes, max_members, max_member_bytes, max_total_bytes)
    manifest_path = Path(manifest_path)
    archive_path = Path(archive_path)
    output_root = Path(output_root)
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManualArchiveError("manifest is not valid JSON") from exc
    try:
        prepared = _preflight_manifest(manifest, output_root)
        _preflight_metadata_destinations(output_root)
    except ManifestError as exc:
        raise ManualArchiveError("manifest or output targets are unsafe") from exc

    try:
        archive_handle = archive_path.open("rb")
    except OSError:
        raise
    with archive_handle:
        archive_stat = os.fstat(archive_handle.fileno())
        if not stat.S_ISREG(archive_stat.st_mode):
            raise ManualArchiveError("archive must be a regular file")
        if archive_stat.st_size > max_archive_bytes:
            raise ManualArchiveError("archive exceeds bounded raw byte policy")
        archive_digest = hashlib.sha256()
        archive_bytes = 0
        for chunk in iter(lambda: archive_handle.read(CHUNK_BYTES), b""):
            archive_bytes += len(chunk)
            if archive_bytes > max_archive_bytes:
                raise ManualArchiveError("archive exceeds bounded raw byte policy")
            archive_digest.update(chunk)
        if archive_bytes != archive_stat.st_size:
            raise ManualArchiveError("archive size changed during bounded read")
        archive_handle.seek(0)
        bounded_archive = _BoundedArchiveDescriptor(archive_handle, archive_stat.st_size)
        try:
            archive = zipfile.ZipFile(bounded_archive, "r")
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise ManualArchiveError("archive is not a valid ZIP") from exc
        with archive:
            members = _preflight_archive(
                archive,
                max_members=max_members,
                max_member_bytes=max_member_bytes,
                max_total_bytes=max_total_bytes,
            )
            infos = archive.infolist()
            for member in members:
                dois, title = _member_pdf_identity(
                    archive,
                    infos[member["index"]],
                    max_member_bytes=max_member_bytes,
                )
                member["embedded_dois"] = dois
                member["embedded_title"] = title
            alias_indexes = _build_alias_indexes(prepared)
            resolved_members, match_basis, ambiguous_rows, unresolved, unmatched_count = _map_members(
                members, prepared, *alias_indexes
            )
            confirmed_mappings = _apply_member_overrides(
                member_overrides,
                members=members,
                prepared=prepared,
                resolved=resolved_members,
                match_basis=match_basis,
                ambiguous_rows=ambiguous_rows,
                unresolved=unresolved,
            )
            for mapping in confirmed_mappings:
                row_index = next(
                    index
                    for index, item in enumerate(prepared)
                    if item["row"]["download_id"] == mapping["download_id"]
                )
                selected = prepared[row_index]["target"]
                if selected.exists() or selected.is_symlink():
                    existing = _inspect_existing(prepared[row_index], output_root)
                    member = members[resolved_members[row_index]]
                    if (
                        existing is None
                        or existing["status"] != "VERIFIED_EXISTING"
                        or existing["sha256"] != member["sha256"]
                    ):
                        raise ManualArchiveError("member override target already exists")
            unmatched_count = len(members) - len(set(resolved_members.values()))
            _validate_archive_descriptor_snapshot(archive_handle, archive_stat)

            output_root.mkdir(parents=True, exist_ok=True)
            try:
                _preflight_metadata_destinations(output_root)
            except ManifestError as exc:
                raise ManualArchiveError("manual import metadata destination is unsafe") from exc
            results = [_base_result(item) for item in prepared]
            staged: dict[int, tuple[Path, str, int]] = {}
            staged_total = 0
            try:
                for row_index, item in enumerate(prepared):
                    existing = _inspect_existing(item, output_root)
                    if existing is not None:
                        results[row_index] = existing
                        continue
                    if row_index in ambiguous_rows:
                        results[row_index].update(status="AMBIGUOUS", reason="ARCHIVE_ALIAS_AMBIGUOUS")
                        continue
                    member_index = resolved_members.get(row_index)
                    if member_index is None:
                        results[row_index].update(status="MISSING", reason="NO_UNIQUE_ARCHIVE_MEMBER")
                        continue
                    member = members[member_index]
                    staged_path, digest, size_bytes, staged_total = _stage_member(
                        archive,
                        infos[member["index"]],
                        item,
                        output_root,
                        max_member_bytes=max_member_bytes,
                        max_total_bytes=max_total_bytes,
                        total_so_far=staged_total,
                    )
                    if item["expected_sha256"] is not None and digest != item["expected_sha256"]:
                        staged_path.unlink(missing_ok=True)
                        results[row_index].update(status="HASH_MISMATCH", reason="EXPECTED_HASH_MISMATCH")
                        continue
                    if not _matches_format(staged_path, item["expected_format"]):
                        staged_path.unlink(missing_ok=True)
                        results[row_index].update(status="FORMAT_MISMATCH", reason="EXPECTED_FORMAT_MISMATCH")
                        continue
                    staged[row_index] = (staged_path, digest, size_bytes)

                for row_index, (staged_path, digest, size_bytes) in list(staged.items()):
                    item = prepared[row_index]
                    existing = _inspect_existing(item, output_root)
                    if existing is not None:
                        results[row_index] = existing
                        staged_path.unlink(missing_ok=True)
                        staged.pop(row_index)
                        continue
                    results[row_index].update(
                        status="IMPORTED",
                        match_basis=match_basis[row_index],
                        sha256=digest,
                        size_bytes=size_bytes,
                    )

                counts = Counter(result["status"] for result in results)
                receipt = {
                    "schema_version": "manual-archive-import-receipt.v1",
                    "canonical_artifact": "00_sources/manual_import_receipt.json",
                    "created_at": _now(),
                    "manifest_basename": manifest_path.name,
                    "manifest_sha256": _sha256_bytes(manifest_bytes),
                    "archive_basename": archive_path.name,
                    "archive_sha256": archive_digest.hexdigest(),
                    "policy": {
                        "max_archive_bytes": max_archive_bytes,
                        "max_members": max_members,
                        "max_member_bytes": max_member_bytes,
                        "max_total_bytes": max_total_bytes,
                        "network_enabled": False,
                        "overwrite_existing": False,
                    },
                    "results": results,
                    "counts": dict(sorted(counts.items())),
                    "unmatched_count": unmatched_count,
                    "unresolved": unresolved,
                    "confirmed_mappings": confirmed_mappings,
                }
                _validate_archive_descriptor_snapshot(archive_handle, archive_stat)
                _publish_transaction(output_root, prepared, staged, receipt)
            finally:
                for staged_path, _, _ in staged.values():
                    staged_path.unlink(missing_ok=True)

    return receipt


def import_manual_archive(
    manifest_path: Path | str,
    archive_path: Path | str,
    output_root: Path | str,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    member_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    with SOURCE_TRANSACTION_LOCK:
        return _import_manual_archive_unlocked(
            manifest_path,
            archive_path,
            output_root,
            max_archive_bytes=max_archive_bytes,
            max_members=max_members,
            max_member_bytes=max_member_bytes,
            max_total_bytes=max_total_bytes,
            member_overrides=member_overrides,
        )


__all__ = [
    "DEFAULT_MAX_ARCHIVE_BYTES",
    "DEFAULT_MAX_MEMBER_BYTES",
    "DEFAULT_MAX_MEMBERS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "HARD_MAX_ARCHIVE_BYTES",
    "HARD_MAX_MEMBER_BYTES",
    "HARD_MAX_MEMBERS",
    "HARD_MAX_TOTAL_BYTES",
    "ManualArchiveError",
    "SOURCE_TRANSACTION_LOCK",
    "import_manual_archive",
]
