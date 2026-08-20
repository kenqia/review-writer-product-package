"""Local, Agent-facing PDF parsing for an already bootstrapped review project.

The fresh-project bootstrap deliberately stops after the researcher has
identified source roles.  This tool is the next native Agent action: it turns
the verified project-owned PDFs into deterministic local text layers, binds
them to Source Truth and publishes the resulting parse gate.  It does not
invent Evidence, call a hosted parser, or create a second project authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.evidence.build_pdf_text_layers import build_layers

from review_writer.product_foundation import ProductFoundationError, VersionContext
from review_writer.product_foundation.contracts import validate_identifier
from review_writer.product_foundation.project_root import resolve_project_root
from review_writer.project.paper_evidence_store import (
    PaperEvidenceStoreError,
    project_write_lock,
)
from review_writer.project.parse_quality import (
    ParseQualityError,
    parse_quality_state,
    write_parse_quality_gate,
)
from review_writer.project.chemical_paper import (
    ChemicalPaperError,
    chemical_paper_current_binding,
)
from review_writer.project.paper_evidence import (
    PaperEvidenceError,
    paper_evidence_state,
    register_manual_pdf_evidence,
    register_paper_evidence_candidates,
)
from review_writer.project.path_safety import PathSafetyError, validate_source_file
from review_writer.project.source_truth import (
    SourceTruthError,
    canonical_digest,
    write_source_truth_bundle,
)


_EVIDENCE_COMPONENTS = ("mineru", "parses", "text_layers", "source_truth")
_RECEIPT = Path("00_sources/acquisition_final_receipt.json")
_SHA256 = set("0123456789abcdef")
_HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
_PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED = "PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED"
_CHEMICAL_GAP_LIMITATION = (
    "Chemical GAP: no verified Chemical Paper binding is available from the PDF-only input; "
    "chemical-field-dependent claims remain unsupported."
)
_MINERU_PARSER = Path(
    "/home/kenqia/.codex/review-writer/skills/"
    "mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py"
)
_MINERU_TIMEOUT_SECONDS = 35 * 60


class LocalPdfParseError(ValueError):
    """Stable, fail-closed result from the local Agent parse tool."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _MinerUParseFailure(ValueError):
    """A private reason that permits the documented local parser fallback."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _registered_project(project: str | Path) -> Path:
    try:
        root = resolve_project_root(project)
    except ProductFoundationError as exc:
        raise LocalPdfParseError("PROJECT_ROOT_INVALID") from exc
    state_path = root / "00_brief/review_state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise LocalPdfParseError("PROJECT_NOT_REGISTERED")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalPdfParseError("PROJECT_REGISTRATION_INVALID") from exc
    if not isinstance(state, dict) or state.get("project_id") != root.name:
        raise LocalPdfParseError("PROJECT_REGISTRATION_INVALID")
    return root


def _load_current(project: Path) -> tuple[VersionContext, Any, Any]:
    try:
        context = VersionContext.load(project)
        state = context.state()
        current = context.view_version(state.current_version_id)
    except (OSError, ProductFoundationError, TypeError, ValueError) as exc:
        raise LocalPdfParseError("VERSION_CONTEXT_INVALID") from exc
    if (
        state.project_id != project.name
        or not current.is_current
        or not current.is_active_head
        or not current.can_write
        or current.snapshot.get("currentness") != "current"
    ):
        raise LocalPdfParseError("VERSION_CONTEXT_INVALID")
    return context, state, current


def _identifier(value: object, code: str) -> str:
    try:
        return validate_identifier(value, field=code.lower())
    except ProductFoundationError as exc:
        raise LocalPdfParseError(code) from exc


def _safe_project_file(project: Path, relative: str) -> Path:
    try:
        return validate_source_file(project, relative)
    except (OSError, PathSafetyError) as exc:
        raise LocalPdfParseError("SOURCE_PDF_INVALID") from exc


def _receipt_sources(project: Path) -> tuple[list[dict[str, str]], str]:
    receipt_path = _safe_project_file(project, _RECEIPT.as_posix())
    try:
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalPdfParseError("ACQUISITION_FINAL_RECEIPT_INVALID") from exc
    if not isinstance(receipt, dict) or not isinstance(receipt.get("studies"), list):
        raise LocalPdfParseError("ACQUISITION_FINAL_RECEIPT_INVALID")

    rows: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    seen_source_ids: set[str] = set()
    for study in receipt["studies"]:
        if not isinstance(study, dict):
            raise LocalPdfParseError("ACQUISITION_FINAL_RECEIPT_INVALID")
        study_id = study.get("study_id")
        source_id = study.get("source_id", study_id)
        if not isinstance(study_id, str) or not isinstance(source_id, str):
            raise LocalPdfParseError("ACQUISITION_FINAL_RECEIPT_INVALID")
        for role, key in (("MAIN", "main_pdf"), ("SI", "si_pdf")):
            descriptor = study.get(key)
            if descriptor is None:
                continue
            if not isinstance(descriptor, dict):
                raise LocalPdfParseError("ACQUISITION_FINAL_RECEIPT_INVALID")
            relative = descriptor.get("path")
            digest = descriptor.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(digest, str)
                or len(digest) != 64
                or set(digest) - _SHA256
            ):
                raise LocalPdfParseError("ACQUISITION_FINAL_RECEIPT_INVALID")
            layer_source_id = source_id if role == "MAIN" else f"{source_id}__SI"
            if relative in seen_paths or layer_source_id in seen_source_ids:
                raise LocalPdfParseError("ACQUISITION_FINAL_RECEIPT_INVALID")
            pdf_relative = f"00_sources/{relative}"
            pdf = _safe_project_file(project, pdf_relative)
            if _sha256_file(pdf) != digest:
                raise LocalPdfParseError("SOURCE_PDF_HASH_MISMATCH")
            seen_paths.add(relative)
            seen_source_ids.add(layer_source_id)
            rows.append(
                {
                    "study_id": study_id,
                    "source_id": layer_source_id,
                    "document_role": role,
                    "relative_pdf_path": relative,
                    "source_pdf_sha256": digest,
                }
            )
    if not rows or not any(row["document_role"] == "MAIN" for row in rows):
        raise LocalPdfParseError("MAIN_SOURCE_MISSING")
    return rows, hashlib.sha256(receipt_bytes).hexdigest()


def _pages(reading_layer: Path, expected_count: int) -> list[str]:
    try:
        pages = reading_layer.read_text(encoding="utf-8").split("\f")
    except (OSError, UnicodeError) as exc:
        raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED") from exc
    if pages and not pages[-1].strip():
        pages.pop()
    if len(pages) != expected_count or not pages:
        raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
    return pages


def _markdown_locators(markdown: str, page_count: int, *, content: list[Any] | None = None) -> dict[str, Any]:
    sections = [
        line.strip().lstrip("#").strip()
        for line in markdown.splitlines()
        if line.lstrip().startswith("#") and line.lstrip().startswith("# ")
    ]
    table_pages: set[int] = set()
    figure_pages: set[int] = set()
    for item in content or []:
        if not isinstance(item, dict):
            continue
        page_value = item.get("page_idx")
        if not isinstance(page_value, int) or page_value < 0 or page_value >= page_count:
            continue
        item_type = str(item.get("type") or "").casefold()
        if item_type in {"table", "table_body"}:
            table_pages.add(page_value + 1)
        if item_type in {"image", "figure", "figure_caption"}:
            figure_pages.add(page_value + 1)
    return {
        "pages": list(range(1, page_count + 1)),
        "sections": sections,
        "tables": sorted(table_pages),
        "figures": sorted(figure_pages),
    }


def _write_fallback_parse_output(
    evidence: Path,
    rows: list[dict[str, str]],
    *,
    fallback_reason: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    layer_root = evidence / "text_layers"
    source_paths = [
        (
            row["source_id"],
            evidence.parent / "00_sources" / row["relative_pdf_path"],
        )
        for row in rows
    ]
    try:
        build_layers(
            source_paths,
            layer_root,
            Path(shutil.which("pdftotext") or "pdftotext"),
            force=False,
        )
        layers = json.loads((layer_root / "text_layers.manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, subprocess.SubprocessError) as exc:
        raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED") from exc
    if not isinstance(layers, dict) or not isinstance(layers.get("sources"), list):
        raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
    by_source = {
        row.get("source_id"): row
        for row in layers["sources"]
        if isinstance(row, dict) and isinstance(row.get("source_id"), str)
    }
    if set(by_source) != {row["source_id"] for row in rows}:
        raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")

    mineru_rows: list[dict[str, Any]] = []
    parse_rows: list[dict[str, Any]] = []
    for row in rows:
        layer = by_source[row["source_id"]]
        page_count = layer.get("page_count")
        reading_name = layer.get("reading_order_path")
        if not isinstance(page_count, int) or page_count < 1 or not isinstance(reading_name, str):
            raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
        pages = _pages(layer_root / reading_name, page_count)
        slug = f"local_{row['source_pdf_sha256'][:20]}_{row['document_role'].lower()}"
        markdown = "\n\n".join(
            f"<!-- source page {index + 1} -->\n{page.strip()}"
            for index, page in enumerate(pages)
            if page.strip()
        ).strip()
        if not markdown:
            raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
        content = [
            {
                "type": "text",
                "text": page.strip(),
                "page_idx": index,
                "bbox": [0, 0, 595.276, 841.89],
            }
            for index, page in enumerate(pages)
            if page.strip()
        ]
        if not content:
            raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
        content_v2 = [
            [
                {
                    "type": "text",
                    "text": page.strip(),
                    "page_idx": index,
                    "bbox": [0, 0, 595.276, 841.89],
                }
            ]
            if page.strip()
            else []
            for index, page in enumerate(pages)
        ]
        extracted = evidence / "parses" / "extracted" / slug
        # Keep the figure producer's source-asset contract explicit even when
        # deterministic local text parsing finds no image assets.  An empty,
        # real directory lets the existing registry return a locator GAP
        # instead of treating a valid text-only parse as a missing path.
        (extracted / "images").mkdir(parents=True, exist_ok=True)
        mineru_markdown = evidence / "mineru" / "markdown" / f"{slug}.md"
        parse_markdown = evidence / "parses" / "markdown" / f"{slug}.md"
        for target in (mineru_markdown, parse_markdown, extracted / "full.md"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(markdown + "\n", encoding="utf-8")
        _atomic_json(extracted / f"{slug}_content_list.json", content)
        _atomic_json(extracted / f"{slug}_content_list_v2.json", content_v2)
        _atomic_json(
            extracted / "layout.json",
            {
                "schema_version": "local-pdftotext-layout.v1",
                "provenance": "AGENT_LOCAL_PDFTEXT",
                "page_count": page_count,
                "source_pdf_sha256": row["source_pdf_sha256"],
            },
        )
        raw_zip = evidence / "mineru" / "raw_zips" / f"{slug}.zip"
        raw_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("full.md", (markdown + "\n").encode("utf-8"))
            archive.writestr(
                f"{slug}_content_list.json",
                json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            archive.writestr(
                f"{slug}_content_list_v2.json",
                json.dumps(content_v2, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
        common = {
            "slug": slug,
            "state": "done",
            "study_id": row["study_id"],
            "source_id": row["source_id"],
            "document_role": row["document_role"],
            "relative_pdf_path": row["relative_pdf_path"],
            "source_pdf_sha256": row["source_pdf_sha256"],
            "provenance": "AGENT_LOCAL_PDFTEXT",
            "parse_status": "deterministic_local_text",
            "parser_mode": "FALLBACK",
            "fallback_reason": fallback_reason,
        }
        mineru_rows.append(common)
        parse_rows.append(
            {
                **common,
                "full_md": f"extracted/{slug}/full.md",
                "extracted_dir": f"extracted/{slug}",
            }
        )
    settings = {
        "language": "en",
        "model_version": "local-pdftotext",
        "enable_formula": False,
        "enable_table": False,
        "ocr": False,
        "provenance": "AGENT_LOCAL_PDFTEXT",
        "parser_mode": "FALLBACK",
        "fallback_reason": fallback_reason,
    }
    _atomic_json(
        evidence / "mineru" / "manifest.json",
        {
            "schema_version": "source-parse-manifest.v1",
            "settings": settings,
            "completed_count": len(mineru_rows),
            "failed_count": 0,
            "completed": mineru_rows,
            "failed": [],
        },
    )
    _atomic_json(
        evidence / "parses" / "manifest.json",
        {
            "schema_version": "source-parse-manifest.v1",
            "settings": settings,
            "completed_count": len(parse_rows),
            "failed_count": 0,
            "completed": parse_rows,
            "failed": [],
        },
    )
    parser_sources = []
    for row, mineru in zip(rows, mineru_rows, strict=True):
        slug = mineru["slug"]
        parser_sources.append(
            {
                "source_id": row["source_id"],
                "source_pdf_sha256": row["source_pdf_sha256"],
                "page_count": by_source[row["source_id"]]["page_count"],
                "output_artifact_sha256": _sha256_file(
                    evidence / "mineru" / "raw_zips" / f"{slug}.zip"
                ),
                "markdown_sha256": _sha256_file(
                    evidence / "mineru" / "markdown" / f"{slug}.md"
                ),
                "locators": _markdown_locators(
                    (evidence / "mineru" / "markdown" / f"{slug}.md").read_text(
                        encoding="utf-8"
                    ),
                    by_source[row["source_id"]]["page_count"],
                    content=json.loads(
                        (
                            evidence
                            / "parses"
                            / "extracted"
                            / slug
                            / f"{slug}_content_list_v2.json"
                        ).read_text(encoding="utf-8")
                    ),
                ),
            }
        )
    return mineru_rows, parser_sources


def _safe_mineru_tree(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    for child in path.rglob("*"):
        if child.is_symlink() or not (child.is_dir() or child.is_file()):
            return False
    return True


def _mineru_failure(completed: subprocess.CompletedProcess[bytes]) -> _MinerUParseFailure:
    message = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    if "Missing MinerU API token" in message:
        return _MinerUParseFailure("MINERU_TOKEN_UNAVAILABLE")
    return _MinerUParseFailure("MINERU_EXECUTION_FAILED")


def _mineru_output_record(
    output: Path,
    row: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _MinerUParseFailure("MINERU_OUTPUT_INVALID") from exc
    completed = manifest.get("completed") if isinstance(manifest, dict) else None
    failed = manifest.get("failed") if isinstance(manifest, dict) else None
    if (
        not isinstance(completed, list)
        or len(completed) != 1
        or failed != []
        or manifest.get("completed_count") != 1
        or manifest.get("failed_count") != 0
        or not isinstance(completed[0], dict)
    ):
        raise _MinerUParseFailure("MINERU_OUTPUT_INVALID")
    result = completed[0]
    slug = result.get("slug")
    if (
        not isinstance(slug, str)
        or not slug
        or slug in {".", ".."}
        or "/" in slug
        or "\\" in slug
        or result.get("state") != "done"
    ):
        raise _MinerUParseFailure("MINERU_OUTPUT_INVALID")
    expected_name = Path(row["relative_pdf_path"]).name
    if result.get("pdf_name") != expected_name:
        raise _MinerUParseFailure("MINERU_SOURCE_MISMATCH")
    markdown = output / "markdown" / f"{slug}.md"
    raw_zip = output / "raw_zips" / f"{slug}.zip"
    extracted = output / "extracted" / slug
    full_markdown = extracted / "full.md"
    layout = extracted / "layout.json"
    content_v1 = sorted(
        path
        for path in extracted.glob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    )
    content_v2 = sorted(extracted.glob("*_content_list_v2.json"))
    required = (markdown, raw_zip, full_markdown, layout)
    if (
        not all(path.is_file() and not path.is_symlink() for path in required)
        or len(content_v1) != 1
        or len(content_v2) != 1
        or not _safe_mineru_tree(extracted)
    ):
        raise _MinerUParseFailure("MINERU_OUTPUT_INVALID")
    try:
        v1 = json.loads(content_v1[0].read_text(encoding="utf-8"))
        v2 = json.loads(content_v2[0].read_text(encoding="utf-8"))
        layout_value = json.loads(layout.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _MinerUParseFailure("MINERU_OUTPUT_INVALID") from exc
    if (
        not isinstance(v1, list)
        or not isinstance(v2, list)
        or not v2
        or not all(isinstance(page, list) for page in v2)
        or not isinstance(layout_value, (dict, list))
    ):
        raise _MinerUParseFailure("MINERU_OUTPUT_INVALID")
    return (
        {
            "slug": slug,
            "markdown": markdown,
            "raw_zip": raw_zip,
            "extracted": extracted,
            "content_v2": v2,
            "content_v1": v1,
            "markdown_text": markdown.read_text(encoding="utf-8"),
            "page_count": len(v2),
        },
        {
            "source_id": row["source_id"],
            "source_pdf_sha256": row["source_pdf_sha256"],
            "page_count": len(v2),
            "output_artifact_sha256": _sha256_file(raw_zip),
            "markdown_sha256": _sha256_file(markdown),
            "locators": _markdown_locators(
                markdown.read_text(encoding="utf-8"),
                len(v2),
                content=v1,
            ),
        },
    )


def _mineru_layer_text(content_v2: list[Any]) -> str:
    pages: list[str] = []
    for page in content_v2:
        values: list[str] = []
        for item in page:
            if not isinstance(item, dict):
                continue
            for key in ("text", "table_body", "latex"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
                    break
        pages.append("\n".join(values))
    return "\f".join(pages) + "\f"


def _write_mineru_parse_output(
    evidence: Path,
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not _MINERU_PARSER.is_file() or _MINERU_PARSER.is_symlink():
        raise _MinerUParseFailure("MINERU_PARSER_UNAVAILABLE")
    workspace = Path(tempfile.mkdtemp(prefix=".mineru-agent-parse.", dir=evidence.parent))
    materialized = workspace / "materialized"
    try:
        mineru_rows: list[dict[str, Any]] = []
        parser_sources: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            pdf = evidence.parent / "00_sources" / row["relative_pdf_path"]
            if not pdf.is_file() or pdf.is_symlink() or _sha256_file(pdf) != row["source_pdf_sha256"]:
                raise LocalPdfParseError("SOURCE_PDF_STALE")
            output = workspace / f"output-{index}"
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(_MINERU_PARSER),
                        "--pdf",
                        str(pdf),
                        "--input-dir",
                        str(pdf.parent),
                        "--output-dir",
                        str(output),
                        "--timeout-minutes",
                        "30",
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=_MINERU_TIMEOUT_SECONDS,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise _MinerUParseFailure("MINERU_PARSER_UNAVAILABLE") from exc
            except subprocess.TimeoutExpired as exc:
                raise _MinerUParseFailure("MINERU_TIMEOUT") from exc
            except OSError as exc:
                raise _MinerUParseFailure("MINERU_EXECUTION_FAILED") from exc
            if completed.returncode != 0:
                raise _mineru_failure(completed)
            record, parser_source = _mineru_output_record(output, row)
            destination_extracted = materialized / "parses" / "extracted" / record["slug"]
            destination_extracted.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(record["extracted"], destination_extracted, copy_function=shutil.copy2)
            for destination in (
                materialized / "mineru" / "markdown" / f"{record['slug']}.md",
                materialized / "parses" / "markdown" / f"{record['slug']}.md",
            ):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(record["markdown"], destination)
            raw_destination = materialized / "mineru" / "raw_zips" / f"{record['slug']}.zip"
            raw_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(record["raw_zip"], raw_destination)
            layer_root = materialized / "text_layers"
            layer_root.mkdir(parents=True, exist_ok=True)
            reading = layer_root / f"{row['source_id']}.reading.txt"
            layout = layer_root / f"{row['source_id']}.layout.txt"
            layer_text = _mineru_layer_text(record["content_v2"])
            reading.write_text(layer_text, encoding="utf-8")
            layout.write_text(layer_text, encoding="utf-8")
            mineru_rows.append(
                {
                    "slug": record["slug"],
                    "state": "done",
                    "study_id": row["study_id"],
                    "source_id": row["source_id"],
                    "document_role": row["document_role"],
                    "relative_pdf_path": row["relative_pdf_path"],
                    "source_pdf_sha256": row["source_pdf_sha256"],
                    "provenance": "MINERU",
                    "parse_status": "mineru_precise_parse",
                    "raw_zip_sha256": parser_source["output_artifact_sha256"],
                }
            )
            parser_sources.append(parser_source)
            text_layers = materialized / "text_layers" / "text_layers.manifest.json"
            layer_rows = []
            if text_layers.is_file():
                try:
                    existing = json.loads(text_layers.read_text(encoding="utf-8"))
                    layer_rows = existing.get("sources", []) if isinstance(existing, dict) else []
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise _MinerUParseFailure("MINERU_OUTPUT_INVALID") from exc
            layer_rows.append(
                {
                    "study_id": row["study_id"],
                    "source_id": row["source_id"],
                    "document_role": row["document_role"],
                    "pdf_name": Path(row["relative_pdf_path"]).name,
                    "pdf_sha256": row["source_pdf_sha256"],
                    "page_count": record["page_count"],
                    "reading_order_path": reading.name,
                    "reading_order_sha256": _sha256_file(reading),
                    "reading_order_method": "mineru-canonical-reading-order",
                    "layout_path": layout.name,
                    "layout_sha256": _sha256_file(layout),
                    "layout_method": "mineru-layout-visual-locator-only",
                }
            )
            _atomic_json(text_layers, {"schema_version": "pdf-text-layers.v1", "sources": layer_rows})
        settings = {
            "language": "en",
            "model_version": "vlm",
            "enable_formula": True,
            "enable_table": True,
            "ocr": False,
            "parser_mode": "MINERU",
            "provenance": "MINERU",
        }
        _atomic_json(
            materialized / "mineru" / "manifest.json",
            {
                "schema_version": "source-parse-manifest.v1",
                "settings": settings,
                "completed_count": len(mineru_rows),
                "failed_count": 0,
                "completed": mineru_rows,
                "failed": [],
            },
        )
        _atomic_json(
            materialized / "parses" / "manifest.json",
            {
                "schema_version": "source-parse-manifest.v1",
                "settings": settings,
                "completed_count": len(mineru_rows),
                "failed_count": 0,
                "completed": [
                    {
                        **row,
                        "full_md": f"extracted/{row['slug']}/full.md",
                        "extracted_dir": f"extracted/{row['slug']}",
                    }
                    for row in mineru_rows
                ],
                "failed": [],
            },
        )
        for component in ("mineru", "parses", "text_layers"):
            shutil.copytree(materialized / component, evidence / component, copy_function=shutil.copy2)
        return mineru_rows, parser_sources
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _publish_components(project: Path, staged_project: Path) -> None:
    source_evidence = staged_project / "01_evidence"
    destination_evidence = project / "01_evidence"
    moved: list[str] = []
    try:
        for component in _EVIDENCE_COMPONENTS:
            source = source_evidence / component
            destination = destination_evidence / component
            if not source.is_dir() or source.is_symlink() or os.path.lexists(destination):
                raise LocalPdfParseError("PARSE_PUBLISH_CONFLICT")
            os.rename(source, destination)
            moved.append(component)
    except BaseException:
        for component in reversed(moved):
            source = source_evidence / component
            destination = destination_evidence / component
            try:
                if not os.path.lexists(source) and destination.is_dir() and not destination.is_symlink():
                    os.rename(destination, source)
            except OSError:
                pass
        raise


def record_agent_tool_outcome(
    explicit_project_root: str | Path,
    *,
    session_id: str,
    tool: str,
    action: str,
    result: object,
    next_action: dict[str, str] | None = None,
    next_reason_code: str | None = None,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
) -> dict[str, Any]:
    """Append one native-Agent tool outcome to the sole current snapshot.

    Producers retain ownership of their artifacts.  This narrow adapter only
    writes the Agent's auditable action record as the next immutable version
    node, after a producer has already completed successfully.
    """

    project = _registered_project(explicit_project_root)
    session = _identifier(session_id, "SESSION_ID_INVALID")
    if (
        not isinstance(tool, str)
        or not tool.strip()
        or len(tool) > 160
        or not isinstance(action, str)
        or not action.strip()
        or len(action) > 160
    ):
        raise LocalPdfParseError("AGENT_TRACE_INVALID")
    try:
        result_digest = canonical_digest(result)
    except (TypeError, ValueError) as exc:
        raise LocalPdfParseError("AGENT_TRACE_INVALID") from exc
    with project_write_lock(project):
        context, state, current = _load_current(project)
        if (
            (expected_revision is not None and expected_revision != state.revision)
            or (expected_head_id is not None and expected_head_id != state.active_head_id)
        ):
            raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
        parse = current.snapshot.get("agent_parse")
        if not isinstance(parse, dict) or parse.get("session_id") != session:
            raise LocalPdfParseError("GENERATOR_SESSION_NOT_FOUND")
        trace = parse.get("tool_trace")
        if not isinstance(trace, list):
            raise LocalPdfParseError("AGENT_TRACE_INVALID")
        event = {
            "tool": tool.strip(),
            "action": action.strip(),
            "status": "SUCCESS",
            "result_digest": result_digest,
            "occurred_at": _now(),
        }
        updated_parse = copy.deepcopy(parse)
        updated_parse["tool_trace"] = [*copy.deepcopy(trace), event]
        if next_action is not None:
            if not isinstance(next_action, dict) or not all(
                isinstance(value, str) and value for value in next_action.values()
            ):
                raise LocalPdfParseError("AGENT_TRACE_INVALID")
            updated_parse["next_action"] = copy.deepcopy(next_action)
        if next_reason_code is not None:
            if (
                not isinstance(next_reason_code, str)
                or not next_reason_code.strip()
                or len(next_reason_code) > 160
            ):
                raise LocalPdfParseError("AGENT_TRACE_INVALID")
            updated_parse["status"] = _HUMAN_ACTION_REQUIRED
            updated_parse["reason_code"] = next_reason_code.strip()
        node = context.publish_active_head(
            {
                **copy.deepcopy(dict(current.snapshot)),
                "agent_parse": updated_parse,
            },
            expected_head_id=state.active_head_id,
            expected_revision=state.revision,
            version_id=_new_id("agent-session"),
        )
    return {
        "project_id": project.name,
        "session_id": session,
        "tool": event["tool"],
        "action": event["action"],
        "current": {
            "version_id": node.version_id,
            "revision": context.state().revision,
            "snapshot_digest": node.snapshot_digest,
        },
    }


def register_pdf_only_evidence(
    explicit_project_root: str | Path,
    *,
    session_id: str,
    study_id: str,
    candidate: object,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
) -> dict[str, Any]:
    """Route PDF-only Evidence through the existing GAP-safe producer.

    A native Agent may make source-bound non-chemical observations from the
    verified PDF/Generic parse.  This adapter deliberately never manufactures
    a Chemical Paper archive, molecule, or structure field.  It records the
    unavailable Chemical lane as the existing Evidence ``GAP`` limitation and
    leaves all molecule/SMILES/molblock candidates on the existing fail-closed
    Chemical import route.
    """

    project = _registered_project(explicit_project_root)
    session = _identifier(session_id, "SESSION_ID_INVALID")
    study = _identifier(study_id, "STUDY_ID_INVALID")
    if not isinstance(candidate, dict):
        raise LocalPdfParseError("PDF_ONLY_EVIDENCE_INVALID")
    normalized = copy.deepcopy(candidate)
    dependencies = normalized.get("field_dependencies")
    if dependencies is not None and dependencies != []:
        raise LocalPdfParseError("CHEMICAL_FIELDS_REQUIRE_IMPORT")
    try:
        chemical_paper_current_binding(project, study)
    except ChemicalPaperError as exc:
        if exc.code != "CHEMICAL_PAPER_NOT_IMPORTED":
            raise LocalPdfParseError(exc.code) from exc
    else:
        raise LocalPdfParseError("CHEMICAL_GAP_ROUTE_NOT_APPLICABLE")

    risks = normalized.get("risk_classes", [])
    limitations = normalized.get("limitations", [])
    if not isinstance(risks, list) or not isinstance(limitations, list):
        raise LocalPdfParseError("PDF_ONLY_EVIDENCE_INVALID")
    normalized["field_dependencies"] = []
    if "GAP" not in risks:
        normalized["risk_classes"] = [*risks, "GAP"]
    if _CHEMICAL_GAP_LIMITATION not in limitations:
        normalized["limitations"] = [*limitations, _CHEMICAL_GAP_LIMITATION]

    next_action = {
        "project_id": project.name,
        "route": "/review",
        "type": _HUMAN_ACTION_REQUIRED,
        "reason_code": _PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED,
    }
    _, state, current = _load_current(project)
    if (
        (expected_revision is not None and expected_revision != state.revision)
        or (expected_head_id is not None and expected_head_id != state.active_head_id)
    ):
        raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
    parse = current.snapshot.get("agent_parse")
    if not isinstance(parse, dict) or parse.get("session_id") != session:
        raise LocalPdfParseError("GENERATOR_SESSION_NOT_FOUND")
    if not isinstance(parse.get("tool_trace"), list):
        raise LocalPdfParseError("AGENT_TRACE_INVALID")
    try:
        quality = parse_quality_state(project, study)
    except ParseQualityError as exc:
        raise LocalPdfParseError(exc.code) from exc
    actions = {
        row.get("decision", {}).get("action")
        for row in quality.get("objects", [])
        if isinstance(row, dict) and isinstance(row.get("decision"), dict)
    }
    try:
        if quality.get("automatic_extraction_allowed"):
            evidence = register_paper_evidence_candidates(project, study, normalized)
            producer = "register_paper_evidence_candidates"
        elif quality.get("workflow_can_continue") and "pdf_locator_only" in actions:
            locator = normalized.get("locator")
            if isinstance(locator, dict):
                normalized["locator"] = {**locator, "source_mode": "original_pdf_manual"}
            normalized.setdefault("study_id", study)
            row = register_manual_pdf_evidence(project, normalized)
            evidence_state = paper_evidence_state(project)
            evidence = {
                "candidate_count": 1,
                "registered_count": 1,
                "status": "needs_review",
                "study_id": study,
                "candidates": [row],
                "project_status": evidence_state["status"],
            }
            producer = "register_manual_pdf_evidence"
        else:
            raise LocalPdfParseError("PARSE_QUALITY_REVIEW_REQUIRED")
    except PaperEvidenceError as exc:
        raise LocalPdfParseError(exc.code) from exc
    trace = record_agent_tool_outcome(
        project,
        session_id=session,
        tool=producer,
        action="REGISTER_PDF_ONLY_EVIDENCE_GAP",
        result=evidence,
        next_action=next_action,
        next_reason_code=_PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED,
        expected_revision=expected_revision,
        expected_head_id=expected_head_id,
    )
    return {
        "status": _HUMAN_ACTION_REQUIRED,
        "reason_code": _PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED,
        "project_id": project.name,
        "session_id": session,
        "evidence": evidence,
        "next_action": next_action,
        "agent_trace": trace,
    }


def parse_project_sources(
    explicit_project_root: str | Path,
    *,
    session_id: str | None = None,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
) -> dict[str, Any]:
    """Run the local parse tool and publish one Agent-traced parse handoff.

    The caller is the native Generator Agent.  The returned human action is
    intentionally the parse-quality review in the existing Dashboard, not an
    automatic approval of extracted scientific content.
    """

    project = _registered_project(explicit_project_root)
    session = _identifier(session_id or _new_id("generator-session"), "SESSION_ID_INVALID")
    context, state, current = _load_current(project)
    if expected_revision is not None and expected_revision != state.revision:
        raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
    if expected_head_id is not None and expected_head_id != state.active_head_id:
        raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
    evidence = project / "01_evidence"
    if any(os.path.lexists(evidence / component) for component in _EVIDENCE_COMPONENTS):
        raise LocalPdfParseError("PARSE_ALREADY_EXISTS")
    rows, receipt_sha256 = _receipt_sources(project)
    staging_parent = Path(tempfile.mkdtemp(prefix=f".{project.name}.local-parse.", dir=project.parent))
    staged_project = staging_parent / project.name
    run_id = _new_id("generator-parse-run")
    published = False
    try:
        shutil.copytree(project, staged_project, copy_function=shutil.copy2)
        staged_rows, staged_receipt_sha256 = _receipt_sources(staged_project)
        if staged_rows != rows or staged_receipt_sha256 != receipt_sha256:
            raise LocalPdfParseError("SOURCE_PDF_STALE")
        try:
            mineru_rows, parser_sources = _write_mineru_parse_output(
                staged_project / "01_evidence", staged_rows
            )
            parser = {
                "parser_mode": "MINERU",
                "sources": parser_sources,
            }
            parser_tool = "mineru_precise_parse"
        except _MinerUParseFailure as exc:
            mineru_rows, parser_sources = _write_fallback_parse_output(
                staged_project / "01_evidence",
                staged_rows,
                fallback_reason=exc.code,
            )
            parser = {
                "parser_mode": "FALLBACK",
                "fallback_reason": exc.code,
                "sources": parser_sources,
            }
            parser_tool = "build_pdf_text_layers"
        bundles = [write_source_truth_bundle(staged_project, row["study_id"]) for row in rows if row["document_role"] == "MAIN"]
        gates = [write_parse_quality_gate(staged_project, row["study_id"]) for row in rows if row["document_role"] == "MAIN"]
        if len(bundles) != len({row["study_id"] for row in rows}) or len(gates) != len(bundles):
            raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
        with project_write_lock(project):
            latest_context, latest_state, latest_current = _load_current(project)
            if (
                latest_state.revision != state.revision
                or latest_state.active_head_id != state.active_head_id
                or _sha256_file(project / _RECEIPT) != receipt_sha256
            ):
                raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
            _publish_components(project, staged_project)
            next_action = {
                "project_id": project.name,
                "route": "/review",
                "type": _HUMAN_ACTION_REQUIRED,
                "reason_code": "PARSE_QUALITY_HUMAN_ACTION_REQUIRED",
            }
            version_id = _new_id("agent-local-parse")
            parser = {
                **parser,
                "authority": {
                    "parent_version_id": latest_current.version_id,
                    "parent_revision": latest_state.revision,
                    "next_version_id": version_id,
                    "next_revision": latest_state.revision + 1,
                    "source_receipt_sha256": receipt_sha256,
                },
            }
            trace = [
                {
                    "tool": parser_tool,
                    "status": "SUCCESS",
                    "source_count": len(rows),
                    **copy.deepcopy(parser),
                },
                {
                    "tool": "write_source_truth_bundle",
                    "status": "SUCCESS",
                    "result_digest": canonical_digest([bundle["bundle_digest"] for bundle in bundles]),
                },
                {
                    "tool": "write_parse_quality_gate",
                    "status": "SUCCESS",
                    "result_digest": canonical_digest([gate["gate_digest"] for gate in gates]),
                },
            ]
            snapshot = {
                **copy.deepcopy(dict(latest_current.snapshot)),
                "agent_parse": {
                    "schema_version": "review-writer.agent-local-parse.v1",
                    "actor_type": "generator_agent",
                    "session_id": session,
                    "run_id": run_id,
                    "status": _HUMAN_ACTION_REQUIRED,
                    "reason_code": "PARSE_QUALITY_HUMAN_ACTION_REQUIRED",
                    "source_receipt_sha256": receipt_sha256,
                    "source_count": len(rows),
                    "parser": copy.deepcopy(parser),
                    "tool_trace": trace,
                    "next_action": next_action,
                },
            }
            node = latest_context.publish_active_head(
                snapshot,
                expected_head_id=latest_state.active_head_id,
                expected_revision=latest_state.revision,
                version_id=version_id,
            )
            published = True
        return {
            "status": _HUMAN_ACTION_REQUIRED,
            "reason_code": "PARSE_QUALITY_HUMAN_ACTION_REQUIRED",
            "project_id": project.name,
            "session_id": session,
            "run_id": run_id,
            "next_action": next_action,
            "current": {
                "version_id": node.version_id,
                "revision": latest_context.state().revision,
                "snapshot_digest": node.snapshot_digest,
            },
            "source_truth": [
                {"study_id": bundle["study_id"], "bundle_digest": bundle["bundle_digest"]}
                for bundle in bundles
            ],
            "parse_quality": [
                {"study_id": gate["study_id"], "gate_digest": gate["gate_digest"], "status": gate["status"]}
                for gate in gates
            ],
            "parser": parser,
            "trace": {"event_count": 3, "parser": parser},
        }
    except LocalPdfParseError:
        raise
    except (OSError, ValueError, PaperEvidenceStoreError, SourceTruthError, ParseQualityError) as exc:
        code = getattr(exc, "code", None)
        raise LocalPdfParseError(code if isinstance(code, str) else "LOCAL_PDF_PARSE_FAILED") from exc
    finally:
        # The staging tree is owned by this invocation and is never a project authority.
        shutil.rmtree(staging_parent, ignore_errors=True)
