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
import re
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
from review_writer.project.section_contract import (
    SectionContractError,
    build_section_writer_packet,
    section_contract_state,
    register_section_contracts,
)
from review_writer.project.path_safety import PathSafetyError, validate_source_file
from review_writer.project.review_figures import (
    ReviewFigureError,
    build_source_figure_registry,
    project_source_figure_candidates,
)
from review_writer.project.source_truth import (
    SourceTruthError,
    canonical_digest,
    load_source_truth_bundle,
    source_truth_asset_snapshot,
    write_source_truth_bundle,
)
from review_writer.project.synthesis import (
    SynthesisError,
    comparison_protocol_state,
    coverage_map_state,
    register_comparison_protocol,
    register_coverage_map,
    register_synthesis_candidates,
    synthesis_state,
)


_EVIDENCE_COMPONENTS = ("mineru", "parses", "text_layers", "source_truth")
_RECEIPT = Path("00_sources/acquisition_final_receipt.json")
_SHA256 = set("0123456789abcdef")
_HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
_PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED = "PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED"
_SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED = "SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED"
_SYNTHESIS_CLAIM_HUMAN_ACTION_REQUIRED = "SYNTHESIS_CLAIM_HUMAN_ACTION_REQUIRED"
_SECTION_CONTRACT_HUMAN_ACTION_REQUIRED = "SECTION_CONTRACT_HUMAN_ACTION_REQUIRED"
_DRAFT_HUMAN_ACTION_REQUIRED = "SECTION_DRAFT_HUMAN_ACTION_REQUIRED"
_CHEMICAL_GAP_LIMITATION = (
    "Chemical GAP: no verified Chemical Paper binding is available from the PDF-only input; "
    "chemical-field-dependent claims remain unsupported."
)
_FALLBACK_CAPABILITY_GAPS = (
    "MinerU layout capability is unavailable; pdftotext output is text-only.",
    "MinerU table capability is unavailable; extracted tables require source-PDF review.",
    "MinerU formula capability is unavailable; extracted chemistry notation requires source-PDF review.",
    "MinerU OCR capability is unavailable; scanned or image-only text is not supported.",
)
_CHEMICAL_GAPS = (_CHEMICAL_GAP_LIMITATION,)
_MINERU_PARSER: Path | None = None
_MINERU_PARSER_ENV = "REVIEW_WRITER_MINERU_PARSER"
_MINERU_PARSER_RELATIVE_PATHS = (
    Path(
        ".agents/skills/mineru-precise-parse-review-writer/scripts/"
        "parse_review_writer_pdfs.py"
    ),
    Path(
        "skills/mineru-precise-parse-review-writer/scripts/"
        "parse_review_writer_pdfs.py"
    ),
)
_MINERU_PARSER_SKILL_RELATIVE_PATH = Path(
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


def _mineru_parser_candidates(
    *,
    package_root: Path | None = None,
    home: Path | None = None,
    path_lookup: Any = shutil.which,
) -> tuple[Path, ...]:
    """Return optional MinerU parser locations in portable preference order.

    The product package intentionally does not vendor the external MinerU
    skill.  A user may install that skill next to the package, in a normal
    user skill directory, or provide an explicit parser path.  Keep all of
    those choices outside the project authority and let the existing local
    text fallback handle a missing or unusable parser.
    """

    root = package_root or Path(__file__).resolve().parents[2]
    user_home = home or Path.home()
    candidates: list[Path] = []
    configured = os.environ.get(_MINERU_PARSER_ENV, "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        candidates.append(
            configured_path if configured_path.is_absolute() else root / configured_path
        )
    if _MINERU_PARSER is not None:
        candidates.append(_MINERU_PARSER)
    candidates.extend(root / relative for relative in _MINERU_PARSER_RELATIVE_PATHS)
    for skill_root in (
        user_home / ".codex/skills",
        user_home / ".codex/review-writer/skills",
        user_home / ".agents/skills",
    ):
        candidates.append(skill_root / _MINERU_PARSER_SKILL_RELATIVE_PATH)
    executable = path_lookup("parse_review_writer_pdfs.py")
    if executable:
        candidates.append(Path(executable))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def _resolve_mineru_parser(
    *,
    package_root: Path | None = None,
    home: Path | None = None,
    path_lookup: Any = shutil.which,
) -> Path | None:
    """Resolve an installed MinerU parser without a checkout-specific path."""

    for candidate in _mineru_parser_candidates(
        package_root=package_root,
        home=home,
        path_lookup=path_lookup,
    ):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except OSError:
            continue
    return None


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


def _pdftotext_version(executable: Path) -> str:
    """Return the version reported by the exact fallback executable.

    ``pdftotext -v`` commonly writes its version to stderr.  Keep the parsed
    value small and deterministic, and never make an otherwise usable local
    fallback fail solely because the executable does not report a version.
    """

    try:
        completed = subprocess.run(
            [str(executable), "-v"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return "unreported"
    output_parts: list[str] = []
    for value in (completed.stdout, completed.stderr):
        if isinstance(value, bytes):
            output_parts.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, str):
            output_parts.append(value)
    output = "\n".join(output_parts)
    match = re.search(r"\bpdftotext\s+version\s+([^\s]+)", output, re.IGNORECASE)
    return match.group(1) if match else "unreported"


def _parser_source_provenance(
    row: dict[str, str],
    *,
    backend: str,
    version: str,
    output_artifact_sha256: str,
    page_count: int,
    locators: dict[str, Any],
    capability_gaps: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the source-bound parser record shared by both parser routes."""

    return {
        "source_id": row["source_id"],
        "input_pdf_sha256": row["source_pdf_sha256"],
        # Keep the pre-existing name for consumers that already bind on it.
        "source_pdf_sha256": row["source_pdf_sha256"],
        "output_artifact_sha256": output_artifact_sha256,
        "page_count": page_count,
        "locators": copy.deepcopy(locators),
        "backend": backend,
        "version": version,
        "capability_gaps": list(capability_gaps),
        "chemical_gaps": list(_CHEMICAL_GAPS),
    }


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
    pdftotext = Path(shutil.which("pdftotext") or "pdftotext")
    fallback_version = _pdftotext_version(pdftotext)
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
            pdftotext,
            force=False,
        )
        layers = json.loads((layer_root / "text_layers.manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, subprocess.SubprocessError) as exc:
        raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED") from exc
    if not isinstance(layers, dict) or not isinstance(layers.get("sources"), list):
        raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
    _atomic_json(
        layer_root / "text_layers.manifest.json",
        {
            **layers,
            "backend": "pdftotext",
            "version": fallback_version,
            "capability_gaps": list(_FALLBACK_CAPABILITY_GAPS),
            "chemical_gaps": list(_CHEMICAL_GAPS),
        },
    )
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
            "backend": "pdftotext",
            "version": fallback_version,
            "capability_gaps": list(_FALLBACK_CAPABILITY_GAPS),
            "chemical_gaps": list(_CHEMICAL_GAPS),
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
        "backend": "pdftotext",
        "version": fallback_version,
        "capability_gaps": list(_FALLBACK_CAPABILITY_GAPS),
        "chemical_gaps": list(_CHEMICAL_GAPS),
    }
    _atomic_json(
        evidence / "mineru" / "manifest.json",
        {
            "schema_version": "source-parse-manifest.v1",
            "backend": "pdftotext",
            "version": fallback_version,
            "capability_gaps": list(_FALLBACK_CAPABILITY_GAPS),
            "chemical_gaps": list(_CHEMICAL_GAPS),
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
            "backend": "pdftotext",
            "version": fallback_version,
            "capability_gaps": list(_FALLBACK_CAPABILITY_GAPS),
            "chemical_gaps": list(_CHEMICAL_GAPS),
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
        page_count = by_source[row["source_id"]]["page_count"]
        markdown_path = evidence / "mineru" / "markdown" / f"{slug}.md"
        parser_source = _parser_source_provenance(
            row,
            backend="pdftotext",
            version=fallback_version,
            output_artifact_sha256=_sha256_file(
                evidence / "mineru" / "raw_zips" / f"{slug}.zip"
            ),
            page_count=page_count,
            locators=_markdown_locators(
                markdown_path.read_text(encoding="utf-8"),
                page_count,
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
            capability_gaps=_FALLBACK_CAPABILITY_GAPS,
        )
        parser_source["markdown_sha256"] = _sha256_file(markdown_path)
        parser_sources.append(parser_source)
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
    parser_path = _resolve_mineru_parser()
    if parser_path is None:
        raise _MinerUParseFailure("MINERU_PARSER_UNAVAILABLE")
    try:
        parser_version = f"script-sha256:{_sha256_file(parser_path)}"
    except OSError as exc:
        raise _MinerUParseFailure("MINERU_PARSER_UNAVAILABLE") from exc
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
                        str(parser_path),
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
            record, raw_parser_source = _mineru_output_record(output, row)
            parser_source = _parser_source_provenance(
                row,
                backend="mineru-precise-parse",
                version=parser_version,
                output_artifact_sha256=raw_parser_source["output_artifact_sha256"],
                page_count=raw_parser_source["page_count"],
                locators=raw_parser_source["locators"],
            )
            parser_source["markdown_sha256"] = raw_parser_source["markdown_sha256"]
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
                    "backend": "mineru-precise-parse",
                    "version": parser_version,
                    "capability_gaps": [],
                    "chemical_gaps": list(_CHEMICAL_GAPS),
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
            _atomic_json(
                text_layers,
                {
                    "schema_version": "pdf-text-layers.v1",
                    "backend": "mineru-precise-parse",
                    "version": parser_version,
                    "capability_gaps": [],
                    "chemical_gaps": list(_CHEMICAL_GAPS),
                    "sources": layer_rows,
                },
            )
        settings = {
            "language": "en",
            "model_version": "vlm",
            "enable_formula": True,
            "enable_table": True,
            "ocr": False,
            "parser_mode": "MINERU",
            "provenance": "MINERU",
            "backend": "mineru-precise-parse",
            "version": parser_version,
            "capability_gaps": [],
            "chemical_gaps": list(_CHEMICAL_GAPS),
        }
        _atomic_json(
            materialized / "mineru" / "manifest.json",
            {
                "schema_version": "source-parse-manifest.v1",
                "backend": "mineru-precise-parse",
                "version": parser_version,
                "capability_gaps": [],
                "chemical_gaps": list(_CHEMICAL_GAPS),
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
                "backend": "mineru-precise-parse",
                "version": parser_version,
                "capability_gaps": [],
                "chemical_gaps": list(_CHEMICAL_GAPS),
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


def _publish_components(
    project: Path,
    staged_project: Path,
    *,
    replace_existing: bool = False,
) -> None:
    source_evidence = staged_project / "01_evidence"
    destination_evidence = project / "01_evidence"
    backup_root: Path | None = None
    moved: list[str] = []
    backed_up: list[str] = []
    try:
        if replace_existing:
            backup_root = Path(
                tempfile.mkdtemp(
                    prefix=f".{project.name}.parse-replace.",
                    dir=project.parent,
                )
            )
        for component in _EVIDENCE_COMPONENTS:
            source = source_evidence / component
            destination = destination_evidence / component
            if not source.is_dir() or source.is_symlink():
                raise LocalPdfParseError("PARSE_PUBLISH_CONFLICT")
            if os.path.lexists(destination):
                if (
                    not replace_existing
                    or backup_root is None
                    or destination.is_symlink()
                    or not destination.is_dir()
                ):
                    raise LocalPdfParseError("PARSE_PUBLISH_CONFLICT")
                os.rename(destination, backup_root / component)
                backed_up.append(component)
            os.rename(source, destination)
            moved.append(component)
    except BaseException:
        for component in reversed(moved):
            source = source_evidence / component
            destination = destination_evidence / component
            try:
                if not os.path.lexists(source) and destination.is_dir() and not destination.is_symlink():
                    if replace_existing:
                        shutil.rmtree(destination)
                    else:
                        os.rename(destination, source)
            except OSError:
                pass
        if backup_root is not None:
            for component in reversed(backed_up):
                backup = backup_root / component
                destination = destination_evidence / component
                try:
                    if backup.is_dir() and not backup.is_symlink() and not os.path.lexists(destination):
                        os.rename(backup, destination)
                except OSError:
                    pass
        raise
    finally:
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)


def _figure_candidate_input(row: dict[str, Any]) -> dict[str, Any]:
    fragments = row.get("fragments")
    if not isinstance(fragments, list) or not fragments or not isinstance(fragments[0], dict):
        raise LocalPdfParseError("FIGURE_CANDIDATE_INVALID")
    anchor = fragments[0]
    page = row.get("page")
    label = row.get("figure_label")
    caption = row.get("caption")
    for value in (page, label, caption):
        if value is None:
            raise LocalPdfParseError("FIGURE_CANDIDATE_INVALID")
    parsed: dict[str, Any] = {
        "study_id": row.get("study_id"),
        "source_id": row.get("source_id"),
        "document_role": "MAIN",
        "source_pdf_sha256": row.get("source_pdf_sha256"),
        "page": page,
        "figure_label": label,
        "caption": caption,
        "asset_path": anchor.get("asset_path"),
        "asset_sha256": anchor.get("asset_sha256"),
        "block_index": anchor.get("block_index"),
        "bbox": anchor.get("bbox"),
        # Preserve the registry's source-order/grouping fragments for the
        # candidate adapter.  The registry remains the producer; this is only
        # a candidate-stage projection into the Agent snapshot.
        "fragments": copy.deepcopy(fragments),
        "locator": {
            "source_mode": "parsed_candidate",
            "page": page,
            "section_or_item": f"page {page}",
            "figure_or_table": label,
            "exact_quote": caption,
        },
    }
    for field in (
        "attribution",
        "license_or_rights_basis",
        "rights_status",
        "rights_evidence_reference",
    ):
        if field in row:
            parsed[field] = copy.deepcopy(row[field])
    return parsed


def _fallback_figure_gap(
    authorized_sources: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "code": "FIGURE_ASSET_UNAVAILABLE",
        "reason": reason,
        "sources": [
            {
                "study_id": source.get("study_id"),
                "source_id": source.get("source_id"),
                "source_pdf_sha256": (
                    source.get("pdf", {}).get("sha256")
                    if isinstance(source.get("pdf"), dict)
                    else None
                ),
            }
            for source in authorized_sources
            if isinstance(source, dict) and source.get("document_role") == "MAIN"
        ],
    }


def _build_staged_figure_candidates(
    staged_project: Path,
    authorized_sources: list[dict[str, Any]],
    *,
    parser_mode: str,
) -> dict[str, Any]:
    """Build candidate-only figure metadata from the temporary parse tree.

    ``build_source_figure_registry`` owns the deterministic content-list/image
    grouping algorithm.  Its staging write is discarded with the staging tree;
    only the validated candidate projection is returned to the Agent snapshot.
    """
    try:
        registry = build_source_figure_registry(staged_project)
        raw_figures = registry.get("figures") if isinstance(registry, dict) else None
        raw_gaps = registry.get("locator_gaps") if isinstance(registry, dict) else None
        if not isinstance(raw_figures, list) or not isinstance(raw_gaps, list):
            raise ReviewFigureError("FIGURE_REGISTRY_INVALID")
        parsed_figures = [
            _figure_candidate_input(row)
            for row in raw_figures
            if isinstance(row, dict)
        ]
        projected = project_source_figure_candidates(
            staged_project,
            authorized_sources,
            parsed_figures,
        )
        projected_figures = projected.get("figures")
        projected_gaps = projected.get("gaps")
        if not isinstance(projected_figures, list) or not isinstance(projected_gaps, list):
            raise ReviewFigureError("FIGURE_CANDIDATES_INVALID")
        registry_by_id = {
            row.get("figure_id"): row
            for row in raw_figures
            if isinstance(row, dict) and isinstance(row.get("figure_id"), str)
        }
        figures: list[dict[str, Any]] = []
        for candidate in projected_figures:
            if not isinstance(candidate, dict):
                raise ReviewFigureError("FIGURE_CANDIDATE_INVALID")
            figure_id = candidate.get("figure_id")
            source_row = registry_by_id.get(figure_id)
            if isinstance(source_row, dict) and isinstance(source_row.get("fragments"), list):
                candidate = copy.deepcopy(candidate)
                candidate["fragments"] = copy.deepcopy(source_row["fragments"])
            candidate["selection_status"] = "available"
            figures.append(candidate)
        gaps = [copy.deepcopy(gap) for gap in raw_gaps if isinstance(gap, dict)]
        gaps.extend(copy.deepcopy(gap) for gap in projected_gaps if isinstance(gap, dict))
        if parser_mode == "FALLBACK" and not figures:
            gaps.append(
                _fallback_figure_gap(
                    authorized_sources,
                    reason=(
                        "fallback parser produced no extracted image assets; "
                        "source figure candidates remain unavailable until a visual parser run."
                    ),
                )
            )
        return {
            "schema_version": "review-writer.agent-figure-candidates.v1",
            "project_id": staged_project.name,
            "status": "candidate" if figures else "gap",
            "parser_mode": parser_mode,
            "figures": figures,
            "gaps": gaps,
        }
    except ReviewFigureError as exc:
        if parser_mode == "FALLBACK":
            return {
                "schema_version": "review-writer.agent-figure-candidates.v1",
                "project_id": staged_project.name,
                "status": "gap",
                "parser_mode": parser_mode,
                "figures": [],
                "gaps": [
                    _fallback_figure_gap(
                        authorized_sources,
                        reason=f"fallback figure candidate projection unavailable: {exc.code}",
                    )
                ],
            }
        raise LocalPdfParseError(exc.code) from exc


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


def _verified_text_descriptor(project: Path, descriptor: object) -> str:
    if not isinstance(descriptor, dict):
        raise LocalPdfParseError("SOURCE_TRUTH_SCHEMA_INVALID")
    relative = descriptor.get("path")
    expected_sha256 = descriptor.get("sha256")
    expected_size = descriptor.get("size_bytes")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or set(expected_sha256) - _SHA256
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
    ):
        raise LocalPdfParseError("SOURCE_TRUTH_SCHEMA_INVALID")
    path = _safe_project_file(project, relative)
    try:
        if path.stat().st_size != expected_size or _sha256_file(path) != expected_sha256:
            raise LocalPdfParseError("SOURCE_ASSET_DRIFT")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LocalPdfParseError("SOURCE_ASSET_INVALID") from exc


def _first_source_quote(*texts: str) -> str:
    for text in texts:
        for line in text.splitlines():
            candidate = " ".join(line.strip().split())
            if candidate and not candidate.startswith("<!--"):
                return candidate[:2000]
    raise LocalPdfParseError("SOURCE_TEXT_EMPTY")


def _build_pdf_only_evidence_candidate(
    project: Path,
    study_id: str,
    quality: dict[str, object],
) -> dict[str, Any]:
    try:
        bundle = load_source_truth_bundle(project, study_id)
    except SourceTruthError as exc:
        raise LocalPdfParseError(exc.code) from exc
    if bundle.get("project_id") != project.name or bundle.get("study_id") != study_id:
        raise LocalPdfParseError("SOURCE_TRUTH_IDENTITY_MISMATCH")
    sources = [
        row
        for row in bundle.get("sources", [])
        if isinstance(row, dict) and row.get("document_role") == "MAIN"
    ]
    if len(sources) != 1:
        raise LocalPdfParseError("MAIN_SOURCE_MISSING")
    source = sources[0]
    source_id = source.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise LocalPdfParseError("SOURCE_ID_NOT_FOUND")
    try:
        with source_truth_asset_snapshot(project, study_id, source_id, "pdf") as pdf_snapshot:
            with source_truth_asset_snapshot(
                project, study_id, source_id, "parsed-markdown"
            ) as markdown_snapshot:
                source_pdf_sha256 = pdf_snapshot.sha256
                canonical_text = markdown_snapshot.path.read_text(encoding="utf-8")
    except SourceTruthError as exc:
        raise LocalPdfParseError(exc.code) from exc
    except (OSError, UnicodeError) as exc:
        raise LocalPdfParseError("SOURCE_ASSET_INVALID") from exc
    reading_text = _verified_text_descriptor(project, source.get("reading_layer"))
    if (
        not isinstance(source_pdf_sha256, str)
        or len(source_pdf_sha256) != 64
        or set(source_pdf_sha256) - _SHA256
    ):
        raise LocalPdfParseError("SOURCE_PDF_HASH_INVALID")
    quote = _first_source_quote(reading_text, canonical_text)
    objects = quality.get("objects")
    if not isinstance(objects, list) or not objects:
        raise LocalPdfParseError("PARSE_QUALITY_INVALID")
    bound_digests = sorted(
        row.get("object_digest")
        for row in objects
        if isinstance(row, dict) and isinstance(row.get("object_digest"), str)
    )
    if len(bound_digests) != len(objects) or any(
        len(value) != 64 or set(value) - _SHA256 for value in bound_digests
    ):
        raise LocalPdfParseError("PARSE_OBJECT_DIGESTS_INVALID")
    evidence_id = f"pdf-only-{canonical_digest({'study_id': study_id, 'source_id': source_id})[:24]}"
    return {
        "evidence_id": evidence_id,
        "study_id": study_id,
        "source_id": source_id,
        "epistemic_type": "experimental_observation",
        "statement": (
            "The verified source-bound reading layer records a page-1 observation; "
            "no chemical field is inferred."
        ),
        "locator": {
            "source_mode": "parsed_candidate",
            "page": 1,
            "section_or_item": "page 1 source-bound reading layer",
            "figure_or_table": None,
            "exact_quote": quote,
        },
        "reported_conditions": [],
        "quantitative_results": [],
        "limitations": [_CHEMICAL_GAP_LIMITATION],
        "mechanism_grade": "not_applicable",
        "risk_classes": ["GAP"],
        "field_dependencies": [],
        "bound_parse_object_digests": bound_digests,
        "source_pdf_sha256": source_pdf_sha256,
    }


def register_pdf_only_evidence_for_approved_parse(
    explicit_project_root: str | Path,
    *,
    session_id: str,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
) -> dict[str, Any]:
    """Materialize one conservative candidate per approved MAIN parse.

    This is a bridge only: the existing Evidence producer owns candidate files,
    and the existing Dashboard decision route remains the sole approval path.
    """

    project = _registered_project(explicit_project_root)
    session = _identifier(session_id, "SESSION_ID_INVALID")
    _, state, current = _load_current(project)
    if (
        (expected_revision is not None and expected_revision != state.revision)
        or (expected_head_id is not None and expected_head_id != state.active_head_id)
    ):
        raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
    parse = current.snapshot.get("agent_parse")
    if not isinstance(parse, dict) or parse.get("session_id") != session:
        raise LocalPdfParseError("GENERATOR_SESSION_NOT_FOUND")

    try:
        receipt_rows, _receipt_digest = _receipt_sources(project)
    except LocalPdfParseError:
        raise
    main_rows = [row for row in receipt_rows if row.get("document_role") == "MAIN"]
    if not main_rows:
        raise LocalPdfParseError("MAIN_SOURCE_MISSING")
    study_ids = sorted({row["study_id"] for row in main_rows})
    qualities: dict[str, dict[str, object]] = {}
    for study_id in study_ids:
        try:
            quality = parse_quality_state(project, study_id)
        except ParseQualityError as exc:
            raise LocalPdfParseError(exc.code) from exc
        if quality.get("status") == "stale":
            raise LocalPdfParseError("PARSE_QUALITY_STALE")
        if quality.get("workflow_can_continue") is not True:
            raise LocalPdfParseError("PARSE_QUALITY_REVIEW_REQUIRED")
        # The required bridge produces parsed_candidate rows.  A human-approved
        # PDF-locator-only gate remains fail-closed for this automatic adapter.
        if quality.get("automatic_extraction_allowed") is not True:
            raise LocalPdfParseError("PARSE_PDF_LOCATOR_ONLY")
        qualities[study_id] = quality

    try:
        evidence_before = paper_evidence_state(project)
    except PaperEvidenceError as exc:
        raise LocalPdfParseError(exc.code) from exc
    existing_studies = {
        row.get("study_id")
        for row in evidence_before.get("rows", [])
        if isinstance(row, dict) and isinstance(row.get("study_id"), str)
    }
    candidates = {
        study_id: _build_pdf_only_evidence_candidate(project, study_id, qualities[study_id])
        for study_id in study_ids
        if study_id not in existing_studies
    }

    registered_count = 0
    latest_trace: dict[str, Any] | None = None
    for study_id in study_ids:
        candidate = candidates.get(study_id)
        if candidate is None:
            continue
        _, write_state, _write_current = _load_current(project)
        produced = register_pdf_only_evidence(
            project,
            session_id=session,
            study_id=study_id,
            candidate=candidate,
            expected_revision=write_state.revision,
            expected_head_id=write_state.active_head_id,
        )
        registered_count += 1
        latest_trace = produced.get("agent_trace")

    _latest_context, final_state, final_current = _load_current(project)
    try:
        evidence_after = paper_evidence_state(project)
    except PaperEvidenceError as exc:
        raise LocalPdfParseError(exc.code) from exc
    current_binding = {
        "project_id": project.name,
        "version_id": final_current.version_id,
        "revision": final_state.revision,
        "snapshot_digest": final_current.snapshot_digest,
    }
    next_action = {
        "project_id": project.name,
        "route": "/review",
        "type": _HUMAN_ACTION_REQUIRED,
        "reason_code": _PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED,
    }
    return {
        "status": _HUMAN_ACTION_REQUIRED,
        "reason_code": _PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED,
        "project_id": project.name,
        "session_id": session,
        "current": current_binding,
        "revision": final_state.revision,
        "write_mode": "VERSION_CONTEXT" if registered_count else "NONE",
        "evidence": {
            "candidate_count": evidence_after.get("total_count", 0),
            "registered_count": registered_count,
            "status": evidence_after.get("status"),
            "project_status": evidence_after.get("status"),
            "study_count": evidence_after.get("study_count", len(study_ids)),
        },
        "next_action": next_action,
        "agent_trace": latest_trace,
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


def _approved_pdf_only_evidence_rows(evidence_state: object) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(evidence_state, dict):
        raise LocalPdfParseError("PAPER_EVIDENCE_NOT_APPROVED")
    digest = evidence_state.get("projection_digest")
    rows = evidence_state.get("rows")
    if (
        evidence_state.get("workflow_can_continue") is not True
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in _SHA256 for char in digest)
        or not isinstance(rows, list)
    ):
        raise LocalPdfParseError("PAPER_EVIDENCE_NOT_APPROVED")
    approved = [copy.deepcopy(row) for row in rows if isinstance(row, dict) and row.get("status") == "approved"]
    if not approved or len(approved) != len(rows):
        raise LocalPdfParseError("PAPER_EVIDENCE_NOT_APPROVED")
    for row in approved:
        if (
            not isinstance(row.get("evidence_id"), str)
            or not row["evidence_id"].strip()
            or not isinstance(row.get("study_id"), str)
            or not row["study_id"].strip()
            or row.get("field_dependencies") != []
        ):
            raise LocalPdfParseError("PDF_ONLY_SYNTHESIS_EVIDENCE_INVALID")
    evidence_ids = [row["evidence_id"] for row in approved]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise LocalPdfParseError("PDF_ONLY_SYNTHESIS_EVIDENCE_INVALID")
    source_studies: dict[str, set[str]] = {}
    for row in approved:
        source_id = row.get("source_id")
        if isinstance(source_id, str) and source_id.strip():
            source_studies.setdefault(source_id, set()).add(row["study_id"])
    if any(len(studies) > 1 for studies in source_studies.values()):
        raise LocalPdfParseError("PDF_ONLY_SYNTHESIS_EVIDENCE_INVALID")
    return digest, approved


def build_pdf_only_synthesis_plan(evidence_state: object) -> dict[str, dict[str, Any]]:
    """Create bounded, non-chemical review candidates from approved PDF-only Evidence.

    This producer deliberately produces only candidates.  The existing
    protocol, synthesis, and section-contract decision producers remain the
    sole authorities that can approve the workspace for drafting.
    """

    evidence_digest, rows = _approved_pdf_only_evidence_rows(evidence_state)
    study_ids = sorted({str(row["study_id"]) for row in rows})
    multi_study = len(study_ids) > 1
    evidence_ids = sorted(str(row["evidence_id"]) for row in rows)
    plan_id = canonical_digest(
        {
            "paper_evidence_projection_digest": evidence_digest,
            "evidence_ids": evidence_ids,
            "mode": "pdf_only_multi_study" if multi_study else "pdf_only_single_study",
        }
    )[:16]
    comparison_id = f"pdf-only-multi-{plan_id}" if multi_study else f"pdf-only-case-{plan_id}"
    claim_id = f"pdf-only-multi-claim-{plan_id}" if multi_study else f"pdf-only-case-claim-{plan_id}"
    section_id = f"pdf-only-multi-section-{plan_id}" if multi_study else f"pdf-only-case-section-{plan_id}"
    chemical_gap = (
        "Chemical GAP: no verified Chemical Paper binding is available; "
        "SMILES, molecule, molblock, reaction-structure, and related chemical "
        "claims remain unsupported."
    )
    if multi_study:
        multi_study_limit = (
            "Multi-study source-bound comparison only: retain each observation's "
            "study/source boundary; no unsupported generalization or extrapolation is permitted."
        )
        rows_by_study = {
            study_id: sorted(
                (row for row in rows if row["study_id"] == study_id),
                key=lambda row: row["evidence_id"],
            )
            for study_id in study_ids
        }
        study_gap_disclosures = []
        for study_id in study_ids:
            details = []
            for row in rows_by_study[study_id]:
                limitations = row.get("limitations", [])
                risks = row.get("risk_classes", [])
                if isinstance(limitations, list):
                    details.extend(str(item) for item in limitations if isinstance(item, str) and item.strip())
                if isinstance(risks, list):
                    details.extend(f"risk={item}" for item in risks if isinstance(item, str) and item.strip())
            study_gap_disclosures.append(
                f"{study_id}: " + "; ".join(sorted(set(details)))
                if details
                else f"{study_id}: retain study-specific limitations and unresolved GAPs."
            )
        protocol = {
            "comparison_id": comparison_id,
            "comparison_objects": evidence_ids,
            "axes": [
                "study-specific source-reported observation",
                "cross-study comparability and limitations",
            ],
            "normalization_rules": [
                "Retain each study/source boundary and original PDF locators.",
                "Compare only explicitly shared conditions; preserve NOT_COMPARABLE or GAP otherwise.",
                "Do not infer or normalize unavailable chemical fields.",
            ],
            "missing_value_policy": "Missing values remain unknown and are not imputed.",
            "incomparability_rules": [
                multi_study_limit,
                "Mismatched conditions, units, or endpoints remain NOT_COMPARABLE or GAP.",
                chemical_gap,
            ],
            "counterevidence_rules": [
                "Record absent counterevidence and unresolved per-study limitations explicitly."
            ],
            "claim_strength": "multi-study source-bound comparison candidate; bounded wording only",
            "paper_evidence_projection_digest": evidence_digest,
        }
        coverage_axes = [
            {
                "axis_id": f"study-{study_id}-source-reported-observation",
                "question": f"What does the approved source-bound Evidence report for {study_id}?",
                "study_id": study_id,
                "evidence_ids": [row["evidence_id"] for row in rows_by_study[study_id]],
                "counterevidence_ids": [],
                "incomparable_items": ["Cross-study conditions remain unverified unless explicitly reported."],
                "missing_units": ["Chemical Paper binding", "chemical structure fields"],
                "impact_on_conclusion": "Retain a study-specific source-bound observation and disclose its GAPs.",
            }
            for study_id in study_ids
        ]
        coverage = {
            "comparison_id": comparison_id,
            "corpus_kind": "calibration_corpus",
            "axes": coverage_axes,
            "known_omissions": [multi_study_limit, chemical_gap, *study_gap_disclosures],
        }
        claim = {
            "synthesis_id": claim_id,
            "proposition": "The approved Evidence supports a bounded comparison of source-reported observations across the authorized studies.",
            "comparison_axis": "study-specific source-reported observation",
            "supporting_evidence_ids": evidence_ids,
            "counter_evidence_ids": [],
            "applicability_boundary": multi_study_limit,
            "mechanism_evidence_grade": "not_applicable",
            "uncertainty": f"{multi_study_limit} {chemical_gap}",
            "risk_class": "GAP",
            "single_study": False,
            "paper_evidence_projection_digest": evidence_digest,
        }
        contract = {
            "section_id": section_id,
            "research_question": "What do the approved Evidence rows report across the authorized studies?",
            "comparison_axes": [
                "study-specific source-reported observation",
                "cross-study comparability and limitations",
            ],
            "expected_synthesis": "Present study-specific source-bound observations, state explicit comparability limits, and preserve every GAP without extrapolation.",
            "counterevidence_and_limitations": [multi_study_limit, chemical_gap, *study_gap_disclosures],
            "evidence_budget": len(evidence_ids),
            "synthesis_budget": 1,
            "figure_plan": [
                {
                    "kind": "source_locator_table",
                    "purpose": "List each study's source PDF pages and Evidence locators without any chemical structure depiction.",
                    "source_figure_ids": [],
                    "placeholder_ids": [],
                }
            ],
            "allowed_wording_strength": "bounded multi-study source-bound comparison",
        }
    else:
        single_study_limit = (
            "Single-study case report only: no cross-study comparison, generalization, "
            "or extrapolation is permitted."
        )
        protocol = {
            "comparison_id": comparison_id,
            "comparison_objects": evidence_ids,
            "axes": ["source-reported observation", "study-level limitations"],
            "normalization_rules": [
                "Retain source-bound wording and original PDF locators.",
                "Do not infer or normalize unavailable chemical fields.",
            ],
            "missing_value_policy": "Missing values remain unknown and are not imputed.",
            "incomparability_rules": [single_study_limit, chemical_gap],
            "counterevidence_rules": [
                "Record absent counterevidence and unresolved limitations explicitly."
            ],
            "claim_strength": "single-study case report; bounded source-reported wording only",
            "paper_evidence_projection_digest": evidence_digest,
        }
        coverage = {
            "comparison_id": comparison_id,
            "corpus_kind": "calibration_corpus",
            "axes": [
                {
                    "axis_id": "source-reported-observation",
                    "question": "What does the approved source-bound Evidence report?",
                    "evidence_ids": evidence_ids,
                    "counterevidence_ids": [],
                    "incomparable_items": ["No independent study is available for comparison."],
                    "missing_units": ["Cross-study comparator", "chemical structure fields"],
                    "impact_on_conclusion": "Only a bounded case report may be drafted.",
                },
                {
                    "axis_id": "study-level-limitations",
                    "question": "Which limits prevent broader interpretation?",
                    "evidence_ids": evidence_ids,
                    "counterevidence_ids": [],
                    "incomparable_items": ["Single-study corpus."],
                    "missing_units": ["Chemical Paper binding", "independent replication"],
                    "impact_on_conclusion": "Chemical and comparative conclusions remain unsupported.",
                },
            ],
            "known_omissions": [single_study_limit, chemical_gap],
        }
        claim = {
            "synthesis_id": claim_id,
            "proposition": "The approved Evidence supports one bounded, source-reported case report.",
            "comparison_axis": "source-reported observation",
            "supporting_evidence_ids": evidence_ids,
            "counter_evidence_ids": [],
            "applicability_boundary": single_study_limit,
            "mechanism_evidence_grade": "not_applicable",
            "uncertainty": f"{single_study_limit} {chemical_gap}",
            "risk_class": "GAP",
            "single_study": True,
            "paper_evidence_projection_digest": evidence_digest,
        }
        contract = {
            "section_id": section_id,
            "research_question": "What does the approved Evidence report in this single study?",
            "comparison_axes": ["source-reported observation", "study-level limitations"],
            "expected_synthesis": "Present one bounded case report and state its limitations without extrapolation.",
            "counterevidence_and_limitations": [single_study_limit, chemical_gap],
            "evidence_budget": len(evidence_ids),
            "synthesis_budget": 1,
            "figure_plan": [
                {
                    "kind": "source_locator_table",
                    "purpose": "List source PDF pages and Evidence locators without any chemical structure depiction.",
                    "source_figure_ids": [],
                    "placeholder_ids": [],
                }
            ],
            "allowed_wording_strength": "bounded single-study case report",
        }
    return {
        "comparison_protocol": protocol,
        "coverage_map": coverage,
        "synthesis_claim": claim,
        "section_contract": contract,
    }


def build_pdf_only_v1_request(
    evidence_state: object,
    synthesis: object,
    writer_packet: object,
    *,
    session_id: str,
) -> dict[str, str]:
    """Build one conservative v1 request from the approved PDF-only workspace.

    This is deliberately a request producer, not another draft authority.  The
    existing ``GeneratorSession`` and ``register_section_draft`` remain the
    only writer for v1.  The bounded wording prevents a PDF-only source set
    from becoming an unsupported chemical or comparative conclusion.
    """

    session = _identifier(session_id, "SESSION_ID_INVALID")
    plan = build_pdf_only_synthesis_plan(evidence_state)
    evidence_digest, evidence_rows = _approved_pdf_only_evidence_rows(evidence_state)
    evidence_ids = sorted(str(row["evidence_id"]) for row in evidence_rows)
    multi_study = len({str(row["study_id"]) for row in evidence_rows}) > 1
    if not isinstance(synthesis, dict) or synthesis.get("workflow_can_continue") is not True:
        raise LocalPdfParseError("SYNTHESIS_NOT_APPROVED")
    synthesis_rows = synthesis.get("rows")
    if not isinstance(synthesis_rows, list):
        raise LocalPdfParseError("SYNTHESIS_NOT_APPROVED")
    expected_claim = plan["synthesis_claim"]
    synthesis_id = str(expected_claim["synthesis_id"])
    approved_claim = next(
        (
            row
            for row in synthesis_rows
            if isinstance(row, dict)
            and row.get("synthesis_id") == synthesis_id
            and row.get("status") == "approved"
        ),
        None,
    )
    if (
        approved_claim is None
        or approved_claim.get("supporting_evidence_ids") != evidence_ids
        or approved_claim.get("single_study") is not (not multi_study)
        or approved_claim.get("risk_class") != "GAP"
        or approved_claim.get("paper_evidence_projection_digest") != evidence_digest
    ):
        raise LocalPdfParseError("SYNTHESIS_NOT_APPROVED")
    if not isinstance(writer_packet, dict) or not isinstance(writer_packet.get("sections"), list):
        raise LocalPdfParseError("SECTION_CONTRACT_NOT_APPROVED")
    section_id = plan["section_contract"]["section_id"]
    sections = [
        row
        for row in writer_packet["sections"]
        if isinstance(row, dict) and row.get("section_id") == section_id
    ]
    if len(sections) != 1:
        raise LocalPdfParseError("SECTION_CONTRACT_NOT_APPROVED")

    forbidden_fields = (
        "smiles",
        "molecule",
        "molblock",
        "reaction structure",
        "reaction-structure",
        "main_layout",
    )
    evidence_sentences = []
    for row in evidence_rows:
        statement = row.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise LocalPdfParseError("PDF_ONLY_SYNTHESIS_EVIDENCE_INVALID")
        evidence_id = str(row["evidence_id"])
        normalized_statement = " ".join(statement.split())
        if any(field in normalized_statement.casefold() for field in forbidden_fields):
            evidence_sentence = (
                (
                    f"The approved source-bound Evidence for study {row['study_id']} is retained "
                    "without reproducing unsupported chemical-field content."
                )
                if multi_study
                else "The approved source-bound Evidence is retained without reproducing "
                "unsupported chemical-field content."
            )
        elif multi_study:
            evidence_sentence = (
                f"The approved source-bound Evidence for study {row['study_id']} reports: "
                f"{normalized_statement}"
            )
        else:
            evidence_sentence = f"The approved source-bound Evidence reports: {normalized_statement}"
        evidence_sentences.append(f"{evidence_sentence} [evidence:{evidence_id}]")
    scope_sentence = (
        "Multi-study source-bound comparison only: each observation remains bound to its "
        "study/source; mismatched conditions remain NOT_COMPARABLE or GAP, with no "
        "unsupported generalization or extrapolation."
        if multi_study
        else "Single-study case report only: the approved synthesis permits no "
        "cross-study comparison, generalization, or extrapolation."
    )
    heading = "Source-Bound Multi-Study Synthesis" if multi_study else "Source-Bound Single-Study Case Report"
    v2_addition = (
        "This addition retains the multi-study source/study boundaries and all "
        "comparability and Chemical GAP limitations; no broader claim is added. "
        f"[synthesis:{synthesis_id}]"
        if multi_study
        else "This addition retains the single-study and Chemical GAP limitations; "
        f"no broader claim is added. [synthesis:{synthesis_id}]"
    )
    body = "\n\n".join(
        (
            *evidence_sentences,
            f"{scope_sentence} [synthesis:{synthesis_id}]",
            "Chemical GAP: no verified Chemical Paper binding is available; "
            "chemical-field-dependent claims remain unsupported. "
            f"[evidence:{evidence_ids[0]}]",
        )
    )
    return {
        "session_id": session,
        "section_id": str(section_id),
        "heading": heading,
        "body": body,
        "v2_addition": v2_addition,
    }


def _active_parse_session(project: Path, requested_session_id: str | None) -> tuple[str, Any, Any]:
    _, state, current = _load_current(project)
    parse = current.snapshot.get("agent_parse")
    if not isinstance(parse, dict):
        raise LocalPdfParseError("GENERATOR_SESSION_NOT_FOUND")
    session = parse.get("session_id")
    if not isinstance(session, str) or not session:
        raise LocalPdfParseError("GENERATOR_SESSION_NOT_FOUND")
    if requested_session_id is not None and requested_session_id != session:
        raise LocalPdfParseError("GENERATOR_SESSION_NOT_FOUND")
    if not isinstance(parse.get("tool_trace"), list):
        raise LocalPdfParseError("AGENT_TRACE_INVALID")
    return session, state, current


def _synthesis_handoff(
    project: Path,
    *,
    session_id: str,
    state: Any,
    current: Any,
    action: str,
    reason_code: str,
    result: object | None = None,
) -> dict[str, Any]:
    next_action = {
        "project_id": project.name,
        "route": "/review",
        "type": _HUMAN_ACTION_REQUIRED,
        "reason_code": reason_code,
    }
    trace: dict[str, Any] | None = None
    if result is not None:
        trace = record_agent_tool_outcome(
            project,
            session_id=session_id,
            tool="prepare_pdf_only_synthesis_workspace",
            action=action,
            result=result,
            next_action=next_action,
            next_reason_code=reason_code,
            expected_revision=state.revision,
            expected_head_id=state.active_head_id,
        )
        current_payload = trace["current"]
    else:
        current_payload = {
            "version_id": current.version_id,
            "revision": state.revision,
            "snapshot_digest": current.snapshot_digest,
        }
    return {
        "status": _HUMAN_ACTION_REQUIRED,
        "reason_code": reason_code,
        "project_id": project.name,
        "session_id": session_id,
        "action": action,
        "next_action": next_action,
        "current": current_payload,
        "agent_trace": trace,
    }


def prepare_pdf_only_synthesis_workspace(
    explicit_project_root: str | Path,
    *,
    session_id: str | None = None,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
) -> dict[str, Any]:
    """Advance one approved PDF-only Evidence project to its next review gate.

    The function only creates the next missing canonical candidate.  It never
    signs a protocol, claim, or section contract, and therefore always stops
    at the existing Dashboard human-decision seam until drafting is legal.
    """

    project = _registered_project(explicit_project_root)
    active_session, state, current = _active_parse_session(project, session_id)
    if (
        (expected_revision is not None and expected_revision != state.revision)
        or (expected_head_id is not None and expected_head_id != state.active_head_id)
    ):
        raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
    try:
        evidence = paper_evidence_state(project)
        plan = build_pdf_only_synthesis_plan(evidence)
        protocol = comparison_protocol_state(project)
    except (PaperEvidenceError, SynthesisError) as exc:
        raise LocalPdfParseError(getattr(exc, "code", "SYNTHESIS_WORKSPACE_INVALID")) from exc

    protocol_path = project / "02_synthesis" / "comparison_protocol.json"
    if not protocol_path.exists():
        try:
            created = register_comparison_protocol(project, plan["comparison_protocol"])
        except SynthesisError as exc:
            raise LocalPdfParseError(exc.code) from exc
        return _synthesis_handoff(
            project,
            session_id=active_session,
            state=state,
            current=current,
            action="CREATE_COMPARISON_PROTOCOL_CANDIDATE",
            reason_code=_SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED,
            result=created,
        )
    if not protocol.get("workflow_can_continue"):
        return _synthesis_handoff(
            project,
            session_id=active_session,
            state=state,
            current=current,
            action="AWAIT_COMPARISON_PROTOCOL_DECISION",
            reason_code=_SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED,
        )

    coverage_path = project / "02_synthesis" / "coverage_map.json"
    claims_path = project / "02_synthesis" / "synthesis_claim_projection.jsonl"
    try:
        coverage = coverage_map_state(project)
        synthesis = synthesis_state(project)
    except SynthesisError as exc:
        raise LocalPdfParseError(exc.code) from exc
    if coverage_path.exists() and not coverage.get("workflow_can_continue"):
        raise LocalPdfParseError("SYNTHESIS_WORKSPACE_STALE")
    if not coverage_path.exists():
        try:
            register_coverage_map(project, plan["coverage_map"])
        except SynthesisError as exc:
            raise LocalPdfParseError(exc.code) from exc

    if not claims_path.exists():
        try:
            claims = register_synthesis_candidates(project, plan["synthesis_claim"])
        except SynthesisError as exc:
            raise LocalPdfParseError(exc.code) from exc
        return _synthesis_handoff(
            project,
            session_id=active_session,
            state=state,
            current=current,
            action=(
                "CREATE_SINGLE_STUDY_SYNTHESIS_CANDIDATE"
                if plan["synthesis_claim"]["single_study"]
                else "CREATE_MULTI_STUDY_SYNTHESIS_CANDIDATE"
            ),
            reason_code=_SYNTHESIS_CLAIM_HUMAN_ACTION_REQUIRED,
            result={"coverage_map": plan["coverage_map"], "claims": claims},
        )
    if not synthesis.get("workflow_can_continue"):
        return _synthesis_handoff(
            project,
            session_id=active_session,
            state=state,
            current=current,
            action="AWAIT_SYNTHESIS_CLAIM_DECISION",
            reason_code=_SYNTHESIS_CLAIM_HUMAN_ACTION_REQUIRED,
        )

    contract_path = project / "02_synthesis" / "section_contracts.jsonl"
    try:
        contracts = section_contract_state(project)
    except (SectionContractError, SynthesisError) as exc:
        raise LocalPdfParseError(getattr(exc, "code", "SECTION_CONTRACT_INVALID")) from exc
    if contract_path.exists() and not contracts.get("workflow_can_continue"):
        return _synthesis_handoff(
            project,
            session_id=active_session,
            state=state,
            current=current,
            action="AWAIT_SECTION_CONTRACT_DECISION",
            reason_code=_SECTION_CONTRACT_HUMAN_ACTION_REQUIRED,
        )
    if not contract_path.exists():
        try:
            created = register_section_contracts(project, plan["section_contract"])
        except SectionContractError as exc:
            raise LocalPdfParseError(exc.code) from exc
        return _synthesis_handoff(
            project,
            session_id=active_session,
            state=state,
            current=current,
            action="CREATE_SECTION_CONTRACT_CANDIDATE",
            reason_code=_SECTION_CONTRACT_HUMAN_ACTION_REQUIRED,
            result=created,
        )
    try:
        writer_packet = build_section_writer_packet(
            project, plan["section_contract"]["section_id"]
        )
        request = build_pdf_only_v1_request(
            evidence,
            synthesis,
            writer_packet,
            session_id=active_session,
        )
        from review_writer.agent.generator_runtime import (
            GeneratorRuntimeError,
            GeneratorSession,
        )

        generated = GeneratorSession(project).start(
            request,
            expected_revision=state.revision,
            expected_head_id=state.active_head_id,
        )
    except (SectionContractError, GeneratorRuntimeError) as exc:
        raise LocalPdfParseError(getattr(exc, "code", "SECTION_DRAFT_INVALID")) from exc

    _, generated_state, generated_current = _load_current(project)
    next_action = {
        "project_id": project.name,
        "route": "/draft",
        "type": _HUMAN_ACTION_REQUIRED,
        "reason_code": _DRAFT_HUMAN_ACTION_REQUIRED,
    }
    trace = record_agent_tool_outcome(
        project,
        session_id=active_session,
        tool="build_pdf_only_v1_request",
        action="GENERATE_SOURCE_BOUND_V1",
        result=generated,
        next_action=next_action,
        next_reason_code=_DRAFT_HUMAN_ACTION_REQUIRED,
        expected_revision=generated_state.revision,
        expected_head_id=generated_state.active_head_id,
    )
    return {
        **generated,
        "reason_code": _DRAFT_HUMAN_ACTION_REQUIRED,
        "next_action": next_action,
        "current": trace["current"],
        "agent_trace": trace,
    }


def _parse_project_sources(
    explicit_project_root: str | Path,
    *,
    session_id: str | None = None,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
    replace_existing: bool = False,
    reparse_completed: bool = False,
    expected_gate_digests: dict[str, str] | None = None,
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
    if (
        not replace_existing
        and any(os.path.lexists(evidence / component) for component in _EVIDENCE_COMPONENTS)
    ):
        raise LocalPdfParseError("PARSE_ALREADY_EXISTS")
    if replace_existing:
        if evidence.is_symlink() or not evidence.is_dir():
            raise LocalPdfParseError("PARSE_PUBLISH_CONFLICT")
        for component in _EVIDENCE_COMPONENTS:
            existing = evidence / component
            if os.path.lexists(existing) and (
                existing.is_symlink() or not existing.is_dir()
            ):
                raise LocalPdfParseError("PARSE_PUBLISH_CONFLICT")
    rows, receipt_sha256 = _receipt_sources(project)
    staging_parent = Path(tempfile.mkdtemp(prefix=f".{project.name}.local-parse.", dir=project.parent))
    staged_project = staging_parent / project.name
    run_id = _new_id("generator-parse-run")
    try:
        shutil.copytree(project, staged_project, copy_function=shutil.copy2)
        staged_rows, staged_receipt_sha256 = _receipt_sources(staged_project)
        if staged_rows != rows or staged_receipt_sha256 != receipt_sha256:
            raise LocalPdfParseError("SOURCE_PDF_STALE")
        if replace_existing:
            # Parser output is rebuilt in the staging tree.  Keep
            # ``source_truth`` intact so the quality-gate writer can migrate
            # the previous reparse decision into history, then publish the
            # rebuilt parse components as one replacement set.
            for component in ("mineru", "parses", "text_layers"):
                existing = staged_project / "01_evidence" / component
                if os.path.lexists(existing):
                    if existing.is_symlink() or not existing.is_dir():
                        raise LocalPdfParseError("PARSE_PUBLISH_CONFLICT")
                    shutil.rmtree(existing)
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
        parser_source = parser_sources[0] if parser_sources else {}
        parser["backend"] = (
            parser_source.get("backend")
            if isinstance(parser_source.get("backend"), str)
            else "unreported"
        )
        parser["version"] = (
            parser_source.get("version")
            if isinstance(parser_source.get("version"), str)
            else "unreported"
        )
        parser["capability_gaps"] = (
            copy.deepcopy(parser_source.get("capability_gaps"))
            if isinstance(parser_source.get("capability_gaps"), list)
            else []
        )
        parser["chemical_gaps"] = (
            copy.deepcopy(parser_source.get("chemical_gaps"))
            if isinstance(parser_source.get("chemical_gaps"), list)
            else []
        )
        main_rows = [row for row in rows if row["document_role"] == "MAIN"]
        main_study_ids = {row["study_id"] for row in main_rows}
        bundles = [write_source_truth_bundle(staged_project, row["study_id"]) for row in main_rows]
        if reparse_completed:
            gates = [
                write_parse_quality_gate(
                    staged_project,
                    row["study_id"],
                    reparse_completed=True,
                )
                for row in main_rows
            ]
        else:
            gates = [write_parse_quality_gate(staged_project, row["study_id"]) for row in main_rows]
        if len(bundles) != len(main_study_ids) or len(gates) != len(bundles):
            raise LocalPdfParseError("LOCAL_PDF_PARSE_FAILED")
        figure_sources: list[dict[str, Any]] = []
        for bundle in bundles:
            if not isinstance(bundle, dict) or not isinstance(bundle.get("study_id"), str):
                raise LocalPdfParseError("FIGURE_SOURCE_RECORDS_INVALID")
            sources = bundle.get("sources")
            if not isinstance(sources, list):
                raise LocalPdfParseError("FIGURE_SOURCE_RECORDS_INVALID")
            for source in sources:
                if isinstance(source, dict) and source.get("document_role") == "MAIN":
                    figure_sources.append(
                        {"study_id": bundle["study_id"], **copy.deepcopy(source)}
                    )
        figure_candidates = _build_staged_figure_candidates(
            staged_project,
            figure_sources,
            parser_mode=parser["parser_mode"],
        )
        with project_write_lock(project):
            latest_context, latest_state, latest_current = _load_current(project)
            if (
                latest_state.revision != state.revision
                or latest_state.active_head_id != state.active_head_id
                or _sha256_file(project / _RECEIPT) != receipt_sha256
            ):
                raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
            if expected_gate_digests is not None:
                for study_id, expected_digest in expected_gate_digests.items():
                    try:
                        latest_quality = parse_quality_state(project, study_id)
                    except ParseQualityError as exc:
                        raise LocalPdfParseError(exc.code) from exc
                    if (
                        latest_quality.get("status") == "stale"
                        or latest_quality.get("gate_digest") != expected_digest
                    ):
                        raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
            if replace_existing:
                _publish_components(project, staged_project, replace_existing=True)
            else:
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
                {
                    "tool": "build_source_figure_registry",
                    "status": "SUCCESS",
                    "candidate_count": len(figure_candidates["figures"]),
                    "gap_count": len(figure_candidates["gaps"]),
                    "result_digest": canonical_digest(figure_candidates),
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
                    "figure_candidates": copy.deepcopy(figure_candidates),
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
            "figure_candidates": copy.deepcopy(figure_candidates),
            "trace": {
                "event_count": 4,
                "parser": parser,
                "figure_candidates": copy.deepcopy(figure_candidates),
            },
        }
    except LocalPdfParseError:
        raise
    except (OSError, ValueError, PaperEvidenceStoreError, SourceTruthError, ParseQualityError) as exc:
        code = getattr(exc, "code", None)
        raise LocalPdfParseError(code if isinstance(code, str) else "LOCAL_PDF_PARSE_FAILED") from exc
    finally:
        # The staging tree is owned by this invocation and is never a project authority.
        shutil.rmtree(staging_parent, ignore_errors=True)


def parse_project_sources(
    explicit_project_root: str | Path,
    *,
    session_id: str | None = None,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
) -> dict[str, Any]:
    """Run the first local parse for a project with no parse components."""

    return _parse_project_sources(
        explicit_project_root,
        session_id=session_id,
        expected_revision=expected_revision,
        expected_head_id=expected_head_id,
    )


_REPARSE_QUALITY_HOLD_CODES = frozenset(
    {
        "PARSE_QUALITY_MISSING",
        "PARSE_QUALITY_REVIEW_REQUIRED",
        "PARSE_QUALITY_STALE",
        "PARSE_PDF_LOCATOR_ONLY",
        "SOURCE_TRUTH_MISSING",
    }
)


def _reparse_gate_digests(
    project: Path,
    rows: list[dict[str, str]],
) -> dict[str, str] | None:
    expected: dict[str, str] = {}
    requested = False
    pending = False
    study_ids = sorted(
        {
            row["study_id"]
            for row in rows
            if row.get("document_role") == "MAIN"
        }
    )
    for study_id in study_ids:
        try:
            quality = parse_quality_state(project, study_id)
        except ParseQualityError as exc:
            if exc.code in _REPARSE_QUALITY_HOLD_CODES:
                return None
            raise LocalPdfParseError(exc.code) from exc
        if quality.get("status") == "stale":
            return None
        gate_digest = quality.get("gate_digest")
        if not isinstance(gate_digest, str) or len(gate_digest) != 64:
            raise LocalPdfParseError("PARSE_QUALITY_INVALID")
        expected[study_id] = gate_digest
        for row in quality.get("objects", []):
            if not isinstance(row, dict):
                continue
            decision = row.get("decision")
            if isinstance(decision, dict) and decision.get("action") == "reparse_required":
                requested = True
            elif row.get("review_state") in {"needs_review", "needs_re_review"}:
                pending = True
    return expected if requested and not pending else None


def reparse_project_sources(
    explicit_project_root: str | Path,
    *,
    session_id: str | None = None,
    expected_revision: int | None = None,
    expected_head_id: str | None = None,
) -> dict[str, Any] | None:
    """Re-run the existing local parser after a Parse Quality reparse decision.

    The parser replaces only the existing canonical parse components in one
    staged publication.  ``write_parse_quality_gate`` preserves prior
    decisions as history and turns completed ``reparse_required`` decisions
    into fresh human review, so no second repair state is introduced.
    """

    project = _registered_project(explicit_project_root)
    _context, state, _current = _load_current(project)
    if (
        (expected_revision is not None and expected_revision != state.revision)
        or (expected_head_id is not None and expected_head_id != state.active_head_id)
    ):
        raise LocalPdfParseError("GENERATOR_VERSION_CONFLICT")
    rows, _receipt_digest = _receipt_sources(project)
    expected_gate_digests = _reparse_gate_digests(project, rows)
    if expected_gate_digests is None:
        return None
    return _parse_project_sources(
        project,
        session_id=session_id,
        expected_revision=expected_revision,
        expected_head_id=expected_head_id,
        replace_existing=True,
        reparse_completed=True,
        expected_gate_digests=expected_gate_digests,
    )
