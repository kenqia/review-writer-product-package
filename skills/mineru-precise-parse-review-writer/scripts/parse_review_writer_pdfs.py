#!/usr/bin/env python3
"""Portable MinerU API adapter for the Review Writer parser contract.

The public Agent invokes this file with one staged PDF.  The adapter talks to
MinerU's documented v4 upload/poll API and materializes only the output shape
that ``review_writer.agent.local_pdf_parse`` already validates.  It never
writes a token, project authority, or partially parsed output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

if sys.platform == "win32":
    try:
        import truststore
    except ImportError:  # pragma: no cover - the Windows package pins truststore
        truststore = None
    else:
        truststore.inject_into_ssl()

import requests


API_BASE_URL = "https://mineru.net/api/v4"
SUCCESS_STATES = {"done", "success", "finished", "completed"}
FAILURE_STATES = {"failed", "error"}
_TOKEN_ENV = "MINERU_API_TOKEN"
_TOKEN_FILE_ENV = "REVIEW_WRITER_MINERU_TOKEN_FILE"
_CA_BUNDLE_ENV = "REVIEW_WRITER_MINERU_CA_BUNDLE"
_REQUESTS_CA_BUNDLE_ENV = "REQUESTS_CA_BUNDLE"


class MinerUAdapterError(RuntimeError):
    """A safe, non-secret failure returned to the local parser caller."""


def _ca_bundle_from_environment() -> Path | None:
    for variable in (_CA_BUNDLE_ENV, _REQUESTS_CA_BUNDLE_ENV):
        configured = os.environ.get(variable, "").strip()
        if not configured:
            continue
        try:
            candidate = Path(configured).expanduser()
            if not candidate.is_file() or not os.access(candidate, os.R_OK):
                raise OSError
        except (OSError, RuntimeError, ValueError):
            raise MinerUAdapterError("MINERU_CA_BUNDLE_INVALID") from None
        return candidate
    return None


def _configure_session(session: requests.Session, ca_bundle: Path | None) -> None:
    if ca_bundle is not None:
        session.verify = str(ca_bundle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _token_file_candidates() -> tuple[Path, ...]:
    configured = os.environ.get(_TOKEN_FILE_ENV, "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "ReviewWriter" / "mineru_api_token")
    home = Path.home()
    candidates.extend(
        (
            home / ".config/review-writer/mineru_api_token",
            home / ".review-writer/mineru_api_token",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return tuple(unique)


def resolve_token() -> str:
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if token:
        return token
    for path in _token_file_candidates():
        try:
            if path.is_file() and not path.is_symlink():
                token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if token:
            return token
    raise MinerUAdapterError(
        "MINERU_TOKEN_UNAVAILABLE: configure MINERU_API_TOKEN in the host environment "
        "or an external user token file"
    )


def _json_response(response: requests.Response, endpoint: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MinerUAdapterError(f"MINERU_HTTP_FAILED: {endpoint}") from exc
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        raise MinerUAdapterError(f"MINERU_API_FAILED: {endpoint}")
    return payload


def _https_url(value: object, code: str) -> str:
    url = value if isinstance(value, str) else ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise MinerUAdapterError(code)
    return url


def _request_upload(
    session: requests.Session,
    token: str,
    pdf: Path,
    data_id: str,
    *,
    language: str,
    model_version: str,
    enable_formula: bool,
    enable_table: bool,
    ocr: bool,
    verify: str | None,
) -> tuple[str, str]:
    payload = {
        "files": [{"name": pdf.name, "data_id": data_id, "is_ocr": ocr}],
        "model_version": model_version,
        "language": language,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
    }
    response = session.post(
        f"{API_BASE_URL}/file-urls/batch",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
        verify=verify,
    )
    body = _json_response(response, "file-urls/batch")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    batch_id = str(data.get("batch_id") or "").strip()
    urls = data.get("file_urls")
    if not batch_id or not isinstance(urls, list) or len(urls) != 1:
        raise MinerUAdapterError("MINERU_UPLOAD_URL_INVALID")
    upload_url = _https_url(urls[0], "MINERU_UPLOAD_URL_INVALID")
    try:
        upload = session.put(upload_url, data=pdf.read_bytes(), timeout=300, verify=verify)
        upload.raise_for_status()
    except (OSError, requests.RequestException) as exc:
        raise MinerUAdapterError("MINERU_UPLOAD_FAILED") from exc
    return batch_id, data_id


def _poll_result(
    session: requests.Session,
    token: str,
    batch_id: str,
    data_id: str,
    *,
    timeout_minutes: int,
    poll_interval: int,
    verify: str | None,
) -> str:
    deadline = time.monotonic() + max(1, timeout_minutes) * 60
    while time.monotonic() < deadline:
        response = session.get(
            f"{API_BASE_URL}/extract-results/batch/{batch_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            verify=verify,
        )
        body = _json_response(response, "extract-results/batch")
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        rows = data.get("extract_result") if isinstance(data.get("extract_result"), list) else []
        row = next((item for item in rows if isinstance(item, dict) and item.get("data_id") == data_id), None)
        if row is None:
            time.sleep(max(1, poll_interval))
            continue
        state = str(row.get("state") or "").casefold()
        if state in SUCCESS_STATES:
            return _https_url(row.get("full_zip_url"), "MINERU_RESULT_MISSING_ZIP")
        if state in FAILURE_STATES:
            raise MinerUAdapterError("MINERU_TASK_FAILED")
        time.sleep(max(1, poll_interval))
    raise MinerUAdapterError("MINERU_TIMEOUT")


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for info in archive.infolist():
        relative = PurePosixPath(info.filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise MinerUAdapterError("MINERU_ARCHIVE_UNSAFE_PATH")
        target = destination.joinpath(*relative.parts)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        unix_mode = (info.external_attr >> 16) & 0o170000
        if unix_mode == 0o120000:
            raise MinerUAdapterError("MINERU_ARCHIVE_SYMLINK")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))


def _one(paths: list[Path], code: str) -> Path:
    if len(paths) != 1:
        raise MinerUAdapterError(code)
    return paths[0]


def _rewrite_image_paths(markdown: str, slug: str) -> str:
    text = markdown.replace("(images/", f"(../extracted/{slug}/images/")
    text = text.replace('src="images/', f'src="../extracted/{slug}/images/')
    text = text.replace("src='images/", f"src='../extracted/{slug}/images/")
    return text


def _materialize(
    output_dir: Path,
    pdf: Path,
    zip_url: str,
    *,
    session: requests.Session,
    verify: str | None = None,
    data_id: str,
    relative_pdf_path: str,
    model_version: str,
) -> None:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", pdf.stem).strip("-._") or "document"
    source_sha256 = _sha256(pdf)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent)
    )
    try:
        raw_zip = temporary / "raw_zips" / f"{slug}.zip"
        raw_zip.parent.mkdir(parents=True, exist_ok=True)
        try:
            with session.get(zip_url, stream=True, timeout=300, verify=verify) as response:
                response.raise_for_status()
                with raw_zip.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
        except requests.exceptions.SSLError as exc:
            raise MinerUAdapterError(
                "MINERU_RESULT_DOWNLOAD_TLS_HOLD: TLS certificate verification failed; "
                "configure REVIEW_WRITER_MINERU_CA_BUNDLE or REQUESTS_CA_BUNDLE with an "
                "existing CA bundle, or install the Windows truststore dependency"
            ) from exc
        except (OSError, requests.RequestException) as exc:
            raise MinerUAdapterError("MINERU_RESULT_DOWNLOAD_FAILED") from exc

        unpacked = temporary / "_archive"
        with zipfile.ZipFile(raw_zip) as archive:
            _safe_extract(archive, unpacked)
        full_candidates = sorted(unpacked.rglob("full.md"))
        full_md = _one(full_candidates, "MINERU_OUTPUT_FULL_MD_INVALID")
        source_dir = full_md.parent
        v1 = _one(
            sorted(source_dir.glob("*_content_list.json")),
            "MINERU_OUTPUT_CONTENT_LIST_INVALID",
        )
        v2 = _one(
            sorted(source_dir.glob("*_content_list_v2.json")),
            "MINERU_OUTPUT_CONTENT_LIST_V2_INVALID",
        )
        try:
            v1_payload = json.loads(v1.read_text(encoding="utf-8"))
            v2_payload = json.loads(v2.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MinerUAdapterError("MINERU_OUTPUT_JSON_INVALID") from exc
        if not isinstance(v1_payload, list) or not isinstance(v2_payload, list) or not v2_payload:
            raise MinerUAdapterError("MINERU_OUTPUT_JSON_INVALID")
        if not all(isinstance(page, list) and all(isinstance(item, dict) for item in page) for page in v2_payload):
            raise MinerUAdapterError("MINERU_OUTPUT_CONTENT_LIST_V2_INVALID")

        extracted = temporary / "extracted" / slug
        shutil.copytree(source_dir, extracted, copy_function=shutil.copy2)
        extracted.mkdir(parents=True, exist_ok=True)
        (extracted / "images").mkdir(parents=True, exist_ok=True)
        extracted_full = extracted / "full.md"
        extracted_full.write_text(full_md.read_text(encoding="utf-8"), encoding="utf-8")
        v1_target = extracted / f"{slug}_content_list.json"
        v2_target = extracted / f"{slug}_content_list_v2.json"
        v1_target.write_text(json.dumps(v1_payload, ensure_ascii=False), encoding="utf-8")
        v2_target.write_text(json.dumps(v2_payload, ensure_ascii=False), encoding="utf-8")
        layout_candidates = sorted(source_dir.glob("layout.json")) + sorted(source_dir.glob("*middle.json"))
        layout_target = extracted / "layout.json"
        if layout_candidates:
            layout_target.write_bytes(layout_candidates[0].read_bytes())
        else:
            layout_target.write_text(
                json.dumps(
                    {
                        "schema_version": "mineru-api-derived-layout.v1",
                        "provenance": "MINERU_API_CONTENT_LIST_V2",
                        "page_count": len(v2_payload),
                        "source_pdf_sha256": source_sha256,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        markdown = temporary / "markdown" / f"{slug}.md"
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(
            _rewrite_image_paths(extracted_full.read_text(encoding="utf-8"), slug),
            encoding="utf-8",
        )
        manifest = {
            "schema_version": "mineru-review-writer-parser.v1",
            "backend": "mineru-api-v4",
            "version": f"api-v4:{model_version}",
            "settings": {"model_version": model_version},
            "completed_count": 1,
            "failed_count": 0,
            "completed": [
                {
                    "pdf_name": pdf.name,
                    "relative_pdf_path": relative_pdf_path,
                    "slug": slug,
                    "data_id": data_id,
                    "state": "done",
                    "source_pdf_sha256": source_sha256,
                    "page_count": len(v2_payload),
                    "backend": "mineru-api-v4",
                    "version": f"api-v4:{model_version}",
                    "capability_gaps": [],
                    "chemical_gaps": [],
                }
            ],
            "failed": [],
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if output_dir.exists():
            raise MinerUAdapterError("MINERU_OUTPUT_ALREADY_EXISTS")
        os.replace(temporary, output_dir)
        temporary = None
    finally:
        if temporary and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse one PDF with the MinerU v4 API.")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-minutes", type=int, default=35)
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument("--language", default="en")
    parser.add_argument("--model-version", default="vlm")
    parser.add_argument("--disable-formula", action="store_true")
    parser.add_argument("--disable-table", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pdf = args.pdf.expanduser().resolve()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not pdf.is_file() or pdf.is_symlink() or pdf.suffix.casefold() != ".pdf":
        raise MinerUAdapterError("MINERU_INPUT_PDF_INVALID")
    try:
        relative_pdf_path = pdf.relative_to(input_dir).as_posix()
    except ValueError as exc:
        raise MinerUAdapterError("MINERU_INPUT_OUTSIDE_DIR") from exc
    ca_bundle = _ca_bundle_from_environment()
    token = resolve_token()
    data_id = f"rw-{_sha256(pdf)[:24]}"
    with requests.Session() as session:
        _configure_session(session, ca_bundle)
        batch_id, returned_id = _request_upload(
            session,
            token,
            pdf,
            data_id,
            language=args.language,
            model_version=args.model_version,
            enable_formula=not args.disable_formula,
            enable_table=not args.disable_table,
            ocr=args.ocr,
            verify=str(ca_bundle) if ca_bundle is not None else None,
        )
        zip_url = _poll_result(
            session,
            token,
            batch_id,
            returned_id,
            timeout_minutes=args.timeout_minutes,
            poll_interval=args.poll_interval,
            verify=str(ca_bundle) if ca_bundle is not None else None,
        )
        _materialize(
            output_dir,
            pdf,
            zip_url,
            session=session,
            verify=str(ca_bundle) if ca_bundle is not None else None,
            data_id=data_id,
            relative_pdf_path=relative_pdf_path,
            model_version=args.model_version,
        )
    print(json.dumps({"status": "done", "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MinerUAdapterError as exc:
        print(str(exc), file=__import__("sys").stderr)
        raise SystemExit(2)
