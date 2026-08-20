from __future__ import annotations

import csv
import hashlib
import html
import http.client
import io
import ipaddress
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .manifest_identity import (
    ManifestIdentityError,
    canonical_acquisition_save_as,
    validate_acquisition_row,
    windows_portable_name_key,
)


USER_AGENT = "review-writer-public-acquisition/1.0"
PUBLIC_QUERY_KEYS = frozenset({
    "download", "format", "type", "file", "filename", "article", "doi", "id",
    "lang", "locale", "pdf", "view", "inline", "sequence", "isallowed",
})
MANUAL_STATUS = "MANUAL_OR_AUTHORIZED_ACCESS_REQUIRED"
METADATA_FILENAMES = frozenset({"acquisition_receipt.json", "manual_acquisition.tsv", "manual_acquisition.html", "manual_import_receipt.json"})
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
OOXML_CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
OOXML_REQUIREMENTS = {
    "DOCX": ("word/document.xml", b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"),
    "XLSX": ("xl/workbook.xml", b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
}
MAX_OOXML_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_CONTENT_TYPES_BYTES = 1024 * 1024
MAX_ROBOTS_BYTES = 1024 * 1024
MIN_PDF_BYTES = 16
PDF_TAIL_BYTES = 4096
PDF_TARGET_BYTES = 4096
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
WINDOWS_FORBIDDEN_COMPONENT_CHARACTERS = frozenset('<>:"|?*')


class ManifestError(ValueError):
    """The acquisition manifest is unsafe or structurally invalid."""


class _DownloadLimitExceeded(Exception):
    pass


class _NetworkFailure(Exception):
    pass


class _ContentLengthMismatch(Exception):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201 - stdlib handler contract
        return None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _portable_target_components(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative.strip():
        raise ManifestError("target_path must be a nonempty relative path")
    if any(ord(character) <= 0x1F or ord(character) == 0x7F for character in relative):
        raise ManifestError("target_path contains an ASCII control character")
    if "\\" in relative or relative.startswith("/"):
        raise ManifestError("target_path must use portable POSIX relative syntax")
    components = tuple(relative.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise ManifestError("target_path contains an empty or dot component")
    for component in components:
        if any(character in WINDOWS_FORBIDDEN_COMPONENT_CHARACTERS for character in component):
            raise ManifestError("target_path contains a Windows-forbidden character")
        if component.endswith((".", " ")):
            raise ManifestError("target_path components must not end in dot or space")
        device_stem = component.split(".", 1)[0].rstrip(" .").casefold()
        if device_stem in WINDOWS_RESERVED_NAMES:
            raise ManifestError("target_path contains a Windows reserved device name")
    return components


def _is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _reject_existing_link_components(output_root: Path, target: Path) -> None:
    current = output_root
    for component in target.relative_to(output_root).parts:
        current /= component
        try:
            linked = _is_link_or_reparse(current)
        except FileNotFoundError:
            break
        except NotADirectoryError as exc:
            raise ManifestError("target_path has a non-directory parent component") from exc
        if linked:
            raise ManifestError("target_path contains an existing link or reparse point")


def _safe_target(output_root: Path, relative: str) -> tuple[Path, Path]:
    components = _portable_target_components(relative)
    root = output_root.resolve()
    target = root.joinpath(*components)
    _reject_existing_link_components(root, target)
    resolved_target = target.resolve()
    if resolved_target == root or root not in resolved_target.parents:
        raise ManifestError("target_path escapes output root")
    lexical_relative = target.relative_to(root)
    canonical_relative = resolved_target.relative_to(root)
    top_level_components = {lexical_relative.parts[0].casefold(), canonical_relative.parts[0].casefold()}
    if top_level_components & METADATA_FILENAMES:
        raise ManifestError("target_path collides with acquisition metadata")
    return target, resolved_target


def _validate_target_parent(output_root: Path, target: Path) -> None:
    root = output_root.resolve()
    parent = target.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ManifestError("target parent escapes output root")


def _validate_existing_target_boundary(output_root: Path, target: Path) -> None:
    _validate_target_parent(output_root, target)
    if target.is_symlink():
        raise ManifestError("existing target symlinks are forbidden")


def _preflight_metadata_destinations(output_root: Path) -> None:
    lexical_root = Path(os.path.abspath(output_root))
    try:
        if _is_link_or_reparse(lexical_root):
            raise ManifestError("metadata output root must not be a link or reparse point")
    except FileNotFoundError:
        pass
    if lexical_root.exists() and not lexical_root.is_dir():
        raise OSError("metadata output root must be a directory")
    root = lexical_root.resolve()
    for filename in METADATA_FILENAMES:
        destination = root.joinpath(*_portable_target_components(filename))
        _reject_existing_link_components(root, destination)
        _validate_target_parent(root, destination)
        if destination.exists() and not destination.is_file():
            raise ManifestError("metadata destination must be a regular file")


def _preflight_manifest(manifest: dict[str, Any], output_root: Path) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    downloads = manifest.get("downloads")
    if manifest.get("schema_version") != "public-corpus-acquisition.v1" or not isinstance(downloads, list):
        raise ManifestError("manifest must use public-corpus-acquisition.v1 with a downloads list")
    prepared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_save_as: set[str] = set()
    seen_targets: set[str] = set()
    for raw_row in downloads:
        try:
            row = validate_acquisition_row(raw_row)
        except ManifestIdentityError as exc:
            raise ManifestError("manifest contains an invalid acquisition row") from exc
        normalized_id = windows_portable_name_key(row["download_id"])
        if normalized_id in seen_ids:
            raise ManifestError("download_id values must be Windows-portable unique")
        seen_ids.add(normalized_id)
        expected_format = row["expected_format"]
        try:
            save_as = canonical_acquisition_save_as(row["download_id"], expected_format)
        except ManifestIdentityError as exc:
            raise ManifestError("manifest contains an invalid acquisition row") from exc
        normalized_save_as = windows_portable_name_key(save_as)
        if normalized_save_as in seen_save_as:
            raise ManifestError("save_as values must be Windows-portable unique")
        seen_save_as.add(normalized_save_as)
        expected_sha256 = row.get("expected_sha256")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
                raise ManifestError("expected_sha256 must be exactly 64 hexadecimal characters")
            expected_sha256 = expected_sha256.lower()
        target, canonical_target = _safe_target(output_root, row["target_path"])
        if target.is_symlink():
            raise ManifestError("target_path must not be a pre-existing symlink")
        normalized_target = canonical_target.as_posix().casefold()
        if normalized_target in seen_targets:
            raise ManifestError("normalized target_path values must be unique")
        seen_targets.add(normalized_target)
        try:
            allowed, url_reason, report_url = _safe_url(row["url"])
            landing_report_url = None
            if row.get("landing_page_url") is not None:
                _, _, landing_report_url = _safe_url(str(row["landing_page_url"]))
        except ValueError as exc:
            raise ManifestError("manifest contains an invalid URL") from exc
        prepared.append({"row": row, "target": target, "expected_format": expected_format, "expected_sha256": expected_sha256, "save_as": save_as, "allowed": allowed, "url_reason": url_reason, "report_url": report_url, "landing_report_url": landing_report_url})
    return prepared


def _valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    dns_name = hostname[:-1] if hostname.endswith(".") else hostname
    if not dns_name:
        return False
    try:
        ascii_name = dns_name.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_name) > 253 or all(character.isdigit() or character == "." for character in ascii_name):
        return False
    return all(DNS_LABEL_RE.fullmatch(label) for label in ascii_name.split("."))


def _safe_url(url: str) -> tuple[bool, str | None, str]:
    if any(character.isspace() or ord(character) <= 0x1F or ord(character) == 0x7F for character in url):
        raise ValueError("URL contains whitespace or control characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL has an invalid host or port") from exc
    if not hostname or not _valid_hostname(hostname):
        raise ValueError("URL requires a valid hostname")
    has_ambiguous_separator = ";" in parsed.query or re.search(r"%3b", parsed.query, re.IGNORECASE) is not None
    query_pairs = [] if has_ambiguous_separator else urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    has_forbidden_query = bool(parsed.query) and (
        has_ambiguous_separator or not query_pairs or any(not _is_allowed_query_key(key) for key, _ in query_pairs)
    )
    safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
    safe_query = "[REDACTED]" if has_forbidden_query else parsed.query
    report_url = urllib.parse.urlunsplit((parsed.scheme, safe_netloc, parsed.path, safe_query, ""))
    if parsed.username or parsed.password:
        return False, "URL_USERINFO_FORBIDDEN", report_url
    hostname = hostname.lower()
    local_http = parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not local_http:
        return False, "INSECURE_NONLOCAL_HTTP", report_url
    if has_forbidden_query:
        return False, "SENSITIVE_URL_PARAMETER_FORBIDDEN", report_url
    return True, None, report_url


def _is_allowed_query_key(key: str) -> bool:
    return key.casefold() in PUBLIC_QUERY_KEYS


def _open_without_redirect(request: urllib.request.Request, timeout_seconds: float):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        return opener.open(request, timeout=timeout_seconds)  # noqa: S310 - callers apply explicit URL safety checks.
    except urllib.error.HTTPError:
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise _NetworkFailure from exc


def _read_network_chunk(response: Any, size: int) -> bytes:
    try:
        return response.read(size)
    except http.client.IncompleteRead as exc:
        raise _ContentLengthMismatch from exc
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise _NetworkFailure from exc


def _content_length(headers: Any, max_bytes: int) -> tuple[int | None, str | None]:
    values = headers.get_all("Content-Length", [])
    if not values:
        return None, None
    if len(values) != 1:
        return None, "INVALID_CONTENT_LENGTH"
    value = values[0].strip()
    if not re.fullmatch(r"[0-9]+", value):
        return None, "INVALID_CONTENT_LENGTH"
    normalized = value.lstrip("0") or "0"
    limit = str(max_bytes)
    if len(normalized) > len(limit) or (len(normalized) == len(limit) and normalized > limit):
        return None, "FILE_EXCEEDS_SIZE_LIMIT"
    return int(normalized), None


def _read_bounded_response(response: Any, max_bytes: int) -> bytes:
    transfer_codings = response.headers.get_all("Transfer-Encoding", [])
    content_lengths = response.headers.get_all("Content-Length", [])
    if transfer_codings and (
        content_lengths
        or len(transfer_codings) != 1
        or transfer_codings[0].strip().casefold() != "chunked"
    ):
        raise _ContentLengthMismatch
    declared_length, length_reason = _content_length(response.headers, max_bytes)
    if length_reason is not None:
        raise _ContentLengthMismatch
    body = bytearray()
    while True:
        chunk = _read_network_chunk(response, min(64 * 1024, max_bytes + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > max_bytes:
            raise _DownloadLimitExceeded
    if declared_length is not None and len(body) != declared_length:
        raise _ContentLengthMismatch
    return bytes(body)


def _robots_allowed(url: str, timeout_seconds: float, cache: dict[str, urllib.robotparser.RobotFileParser | str]) -> tuple[bool, str | None]:
    parsed = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    cached = cache.get(origin)
    if isinstance(cached, str):
        return False, cached
    if cached is None:
        parser = urllib.robotparser.RobotFileParser(robots_url)
        try:
            request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
            with _open_without_redirect(request, timeout_seconds) as response:
                body = _read_bounded_response(response, MAX_ROBOTS_BYTES)
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                parser.disallow_all = True
            elif 400 <= exc.code <= 499:
                parser.allow_all = True
            else:
                cache[origin] = "ROBOTS_CHECK_FAILED"
                return False, "ROBOTS_CHECK_FAILED"
        except (OSError, urllib.error.URLError, _NetworkFailure, _ContentLengthMismatch, _DownloadLimitExceeded):
            cache[origin] = "ROBOTS_CHECK_FAILED"
            return False, "ROBOTS_CHECK_FAILED"
        cache[origin] = parser
    else:
        parser = cached
    return (True, None) if parser.can_fetch(USER_AGENT, url) else (False, "ROBOTS_DISALLOWED")


def _matches_format(path: Path, expected_format: str) -> bool:
    if expected_format == "PDF":
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False
            size = os.fstat(handle.fileno()).st_size
            if size < MIN_PDF_BYTES:
                return False
            handle.seek(max(0, size - PDF_TAIL_BYTES))
            tail = handle.read(PDF_TAIL_BYTES).rstrip(b"\x00\t\n\f\r ")
            final_reference = re.search(rb"startxref\s+([0-9]+)\s+%%EOF$", tail)
            if final_reference is None:
                return False
            xref_offset = int(final_reference.group(1))
            if xref_offset >= size:
                return False
            handle.seek(xref_offset)
            target = handle.read(PDF_TARGET_BYTES)
            before_startxref = tail[:final_reference.start()]
            tail_offset = max(0, size - PDF_TAIL_BYTES)
            xref_table = re.match(
                rb"xref[ \t]*(?:\r\n|\r|\n)"
                rb"[ \t]*[0-9]+[ \t]+[1-9][0-9]*[ \t]*(?:\r\n|\r|\n)"
                rb"[0-9]{10} [0-9]{5} [nf](?: \n|\r\n| \r)",
                target,
            )
            if xref_table is not None:
                return any(
                    tail_offset + match.start() > xref_offset
                    for match in re.finditer(rb"(?:^|[\r\n])trailer(?:\s|$)", before_startxref)
                )
            object_header = re.match(rb"[1-9][0-9]*\s+[0-9]+\s+obj(?=\s|<)", target)
            xref_type = re.search(rb"/Type\s*/XRef(?=\s|/|<|>|\[|\]|\(|\))", target)
            stream_start = re.search(rb"stream(?:\r\n|\r|\n)", target)
            if object_header is None or xref_type is None or stream_start is None or xref_type.end() > stream_start.start():
                return False
            first_endobj = target.find(b"endobj", object_header.end(), stream_start.start())
            stream_end = re.search(rb"endstream\s+endobj\s*$", before_startxref)
            return (
                first_endobj == -1
                and stream_end is not None
                and tail_offset + stream_end.start() > xref_offset + stream_start.start()
            )
    required_part, required_content_type = OOXML_REQUIREMENTS[expected_format]
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if any(info.flag_bits & 0x1 for info in infos):
                return False
            if sum(info.file_size for info in infos) > MAX_OOXML_UNCOMPRESSED_BYTES:
                return False
            names = {info.filename for info in infos}
            if "[Content_Types].xml" not in names or required_part not in names:
                return False
            content_types_info = archive.getinfo("[Content_Types].xml")
            if content_types_info.file_size > MAX_CONTENT_TYPES_BYTES:
                return False
            if archive.testzip() is not None:
                return False
            content_types = archive.read(content_types_info)
            try:
                root = ET.fromstring(content_types)
            except ET.ParseError:
                return False
            if root.tag != f"{{{OOXML_CONTENT_TYPES_NAMESPACE}}}Types":
                return False
            required_part_name = f"/{required_part}"
            required_content_type_text = required_content_type.decode("ascii")
            return any(
                element.tag == f"{{{OOXML_CONTENT_TYPES_NAMESPACE}}}Override"
                and element.attrib.get("PartName") == required_part_name
                and element.attrib.get("ContentType") == required_content_type_text
                for element in root
            )
    except (KeyError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


def _download_source(url: str, target: Path, output_root: Path, robots_cache: dict[str, urllib.robotparser.RobotFileParser | str], *, expected_format: str, expected_sha256: str | None, timeout_seconds: float, max_bytes: int, retries: int) -> tuple[str, str | None, str | None, int | None, str]:
    _validate_target_parent(output_root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _validate_target_parent(output_root, target)
    accept = {"PDF": "application/pdf", "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[expected_format]
    for attempt in range(retries + 1):
        current_url = url
        report_url = _safe_url(url)[2]
        redirect_count = 0
        while True:
            partial: Path | None = None
            try:
                request = urllib.request.Request(current_url, headers={"User-Agent": USER_AGENT, "Accept": accept + ",application/octet-stream;q=0.8"})
                with _open_without_redirect(request, timeout_seconds) as response:
                    status = getattr(response, "status", 200)
                    declared_length, length_reason = _content_length(response.headers, max_bytes)
                    if length_reason is not None:
                        return MANUAL_STATUS, length_reason, None, status, report_url
                    digest = hashlib.sha256()
                    size = 0
                    descriptor, partial_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
                    partial = Path(partial_name)
                    with os.fdopen(descriptor, "wb") as handle:
                        while True:
                            chunk = _read_network_chunk(response, 1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                raise _DownloadLimitExceeded
                            digest.update(chunk)
                            handle.write(chunk)
                    if declared_length is not None and size != declared_length:
                        return MANUAL_STATUS, "CONTENT_LENGTH_MISMATCH", None, status, report_url
                    actual_sha256 = digest.hexdigest()
                    if expected_sha256 is not None and actual_sha256 != expected_sha256:
                        return MANUAL_STATUS, "DOWNLOADED_HASH_MISMATCH", actual_sha256, status, report_url
                    if not _matches_format(partial, expected_format):
                        reason = "RESPONSE_NOT_PDF" if expected_format == "PDF" else "RESPONSE_FORMAT_MISMATCH"
                        return MANUAL_STATUS, reason, None, status, report_url
                    _validate_target_parent(output_root, target)
                    os.replace(partial, target)
                    partial = None
                    return "DOWNLOADED", None, actual_sha256, status, report_url
            except _DownloadLimitExceeded:
                return MANUAL_STATUS, "FILE_EXCEEDS_SIZE_LIMIT", None, None, report_url
            except _ContentLengthMismatch:
                return MANUAL_STATUS, "CONTENT_LENGTH_MISMATCH", None, None, report_url
            except urllib.error.HTTPError as exc:
                status = exc.code
                headers = exc.headers
                exc.close()
                if status in REDIRECT_STATUS_CODES:
                    location = headers.get("Location") if headers else None
                    if not location:
                        return MANUAL_STATUS, "REDIRECT_LOCATION_MISSING", None, status, report_url
                    if redirect_count >= MAX_REDIRECTS:
                        return MANUAL_STATUS, "TOO_MANY_REDIRECTS", None, status, report_url
                    redirected_url = urllib.parse.urljoin(current_url, location)
                    try:
                        allowed, reason, redirected_report_url = _safe_url(redirected_url)
                    except ValueError:
                        return MANUAL_STATUS, "INVALID_REDIRECT_URL", None, status, report_url
                    report_url = redirected_report_url
                    if not allowed:
                        return MANUAL_STATUS, reason, None, status, report_url
                    robots_ok, robots_reason = _robots_allowed(redirected_url, timeout_seconds, robots_cache)
                    if not robots_ok:
                        return MANUAL_STATUS, robots_reason, None, status, report_url
                    current_url = redirected_url
                    redirect_count += 1
                    continue
                if status in {401, 403}:
                    return MANUAL_STATUS, "AUTHORIZATION_REQUIRED", None, status, report_url
                transient = status == 429 or 500 <= status <= 599
                if transient and attempt < retries:
                    retry_after = headers.get("Retry-After") if headers else None
                    delay = min(float(retry_after), 5.0) if retry_after and retry_after.isdigit() else float(attempt + 1)
                    time.sleep(delay)
                    break
                return MANUAL_STATUS, f"HTTP_{status}", None, status, report_url
            except _NetworkFailure:
                if attempt < retries:
                    time.sleep(float(attempt + 1))
                    break
                return MANUAL_STATUS, "NETWORK_FAILURE", None, None, report_url
            finally:
                if partial is not None:
                    partial.unlink(missing_ok=True)
    raise AssertionError("unreachable")


def _stage_bytes(path: Path, content: bytes) -> Path:
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        result = temporary
        temporary = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _spreadsheet_safe_tsv_cell(value: Any) -> str:
    cell = str(value or "")
    return "'" + cell if cell.lstrip().startswith(("=", "+", "-", "@")) else cell


def _render_manual_queue(rows: list[dict[str, Any]]) -> tuple[str, str]:
    fields = ["download_id", "save_as", "study_id", "doi", "document_role", "landing_page_url", "source_url", "target_path", "reason"]
    tsv = io.StringIO(newline="")
    writer = csv.DictWriter(tsv, fieldnames=fields, delimiter="\t", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(
        {field: _spreadsheet_safe_tsv_cell(row.get(field)) for field in fields}
        for row in rows
    )
    body_rows = []
    for row in rows:
        cells = []
        for field in fields:
            value = str(row.get(field) or "")
            if field in {"landing_page_url", "source_url"} and value.startswith(("https://", "http://")):
                rendered = f'<a href="{html.escape(value, quote=True)}">{html.escape(value)}</a>'
            else:
                rendered = html.escape(value)
            cells.append(f"<td>{rendered}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    document = "<!doctype html><meta charset=\"utf-8\"><title>Manual acquisition queue</title><table><thead><tr>" + "".join(f"<th>{html.escape(field)}</th>" for field in fields) + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>\n"
    return tsv.getvalue(), document


def _publish_metadata(output_root: Path, rows: list[dict[str, Any]], receipt: dict[str, Any]) -> None:
    tsv, html_document = _render_manual_queue(rows)
    payloads = {
        output_root / "manual_acquisition.tsv": tsv.encode("utf-8"),
        output_root / "manual_acquisition.html": html_document.encode("utf-8"),
        output_root / "acquisition_receipt.json": (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    staged: dict[Path, Path] = {}
    published: list[Path] = []
    previous: dict[Path, bytes | None] = {}
    try:
        for destination, content in payloads.items():
            staged[destination] = _stage_bytes(destination, content)
        for destination in payloads:
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise OSError("metadata destination must be a regular file")
            previous[destination] = destination.read_bytes() if destination.is_file() else None
        try:
            for destination in payloads:
                os.replace(staged[destination], destination)
                staged.pop(destination)
                published.append(destination)
        except BaseException as publication_error:
            rollback_error: BaseException | None = None
            for destination in reversed(published):
                try:
                    old_content = previous[destination]
                    if old_content is None:
                        destination.unlink(missing_ok=True)
                    else:
                        restore = _stage_bytes(destination, old_content)
                        try:
                            os.replace(restore, destination)
                        finally:
                            restore.unlink(missing_ok=True)
                except BaseException as exc:
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise OSError("metadata publication rollback failed") from publication_error
            raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def acquire_manifest(manifest_path: Path | str, output_root: Path | str, *, allow_network: bool = True, timeout_seconds: float = 30.0, max_bytes: int = 150 * 1024 * 1024, retries: int = 1) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    output_root = Path(output_root)
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestError("manifest is not valid JSON") from exc
    prepared = _preflight_manifest(manifest, output_root)
    _preflight_metadata_destinations(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    results = []
    robots_cache: dict[str, urllib.robotparser.RobotFileParser | str] = {}
    for item in prepared:
        row = item["row"]
        target = item["target"]
        expected_format = item["expected_format"]
        expected_sha256 = item["expected_sha256"]
        allowed = item["allowed"]
        url_reason = item["url_reason"]
        report_url = item["report_url"]
        result = {"download_id": row["download_id"], "save_as": item["save_as"], "study_id": row["study_id"], "doi": row.get("doi"), "document_role": row["document_role"], "expected_format": expected_format, "target_path": row["target_path"], "source_url": report_url, "landing_page_url": item["landing_report_url"], "source_class": row["source_class"], "status": None, "reason": None, "sha256": None, "size_bytes": None, "http_status": None}
        _validate_existing_target_boundary(output_root, target)
        if target.is_file():
            _validate_existing_target_boundary(output_root, target)
            actual = _sha256_file(target)
            if expected_sha256 is not None and expected_sha256 != actual:
                result.update(status=MANUAL_STATUS, reason="EXISTING_HASH_MISMATCH")
            else:
                _validate_existing_target_boundary(output_root, target)
                if not _matches_format(target, expected_format):
                    reason = "EXISTING_FILE_NOT_PDF" if expected_format == "PDF" else "EXISTING_FILE_FORMAT_MISMATCH"
                    result.update(status=MANUAL_STATUS, reason=reason)
                else:
                    _validate_existing_target_boundary(output_root, target)
                    result.update(status="VERIFIED_EXISTING", sha256=actual, size_bytes=target.stat().st_size)
        elif row["source_class"] != "PUBLIC_DIRECT":
            result.update(status=MANUAL_STATUS, reason="NO_PUBLIC_DIRECT_PDF")
        elif not allow_network:
            result.update(status=MANUAL_STATUS, reason="LOCAL_FILE_MISSING")
        elif not allowed:
            result.update(status=MANUAL_STATUS, reason=url_reason)
        else:
            robots_ok, robots_reason = _robots_allowed(row["url"], timeout_seconds, robots_cache)
            if not robots_ok:
                result.update(status=MANUAL_STATUS, reason=robots_reason)
            else:
                status, reason, digest, http_status, final_report_url = _download_source(row["url"], target, output_root, robots_cache, expected_format=expected_format, expected_sha256=expected_sha256, timeout_seconds=timeout_seconds, max_bytes=max_bytes, retries=retries)
                result.update(status=status, reason=reason, sha256=digest, http_status=http_status, source_url=final_report_url)
                _validate_existing_target_boundary(output_root, target)
                if target.is_file():
                    _validate_existing_target_boundary(output_root, target)
                    result["size_bytes"] = target.stat().st_size
        results.append(result)
    manual = [row for row in results if row["status"] == MANUAL_STATUS]
    counts = Counter(row["status"] for row in results)
    receipt = {"schema_version": "public-corpus-acquisition-receipt.v1", "created_at": _now(), "manifest_path": manifest_path.name, "manifest_sha256": _sha256_bytes(manifest_bytes), "results": results, "counts": dict(sorted(counts.items())), "manual_queue_count": len(manual), "policy": {"public_direct_only": True, "network_enabled": allow_network, "robots_respected": True, "credentials_or_sessions_used": False, "bounded_retries": retries, "max_bytes": max_bytes}}
    _publish_metadata(output_root, manual, receipt)
    return receipt
