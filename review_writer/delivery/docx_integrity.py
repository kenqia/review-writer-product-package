"""Fail-closed integrity checks for evidence-bound DOCX releases."""

from __future__ import annotations

import hashlib
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit


_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DRAWING_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CORE_PROPERTIES_NS = (
    "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
)
_DC_NS = "http://purl.org/dc/elements/1.1/"
_REQUIRED_PARTS = frozenset(
    {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
    }
)
_MAX_ENTRIES = 2_000
_MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_XML_BYTES = 16 * 1024 * 1024
_MAX_MEDIA_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_LEVELS = frozenset({"SELF_REVIEWED_DRAFT", "EXPERT_REVIEWED_RELEASE"})
_ALLOWED_EXTERNAL_RELATIONSHIP_TYPES = frozenset(
    {
        f"{_OFFICE_REL_NS}/attachedTemplate",
        f"{_OFFICE_REL_NS}/hyperlink",
    }
)
_IMAGE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(.+)$")
_CLAIM_MARKER_RE = re.compile(r"\[(?:evidence|synthesis):[^\]]+\]", re.IGNORECASE)


class DocxIntegrityError(ValueError):
    """The DOCX package does not bind the declared release inputs."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _safe_package_names(package: zipfile.ZipFile) -> set[str]:
    infos = package.infolist()
    if len(infos) > _MAX_ENTRIES:
        raise DocxIntegrityError("DOCX_ZIP_INVALID", "package contains too many entries")
    names: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        parts = PurePosixPath(name).parts
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in parts)
            or name in names
        ):
            raise DocxIntegrityError("DOCX_ZIP_INVALID", "package entry path is unsafe")
        total += info.file_size
        if info.file_size < 0 or total > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise DocxIntegrityError("DOCX_ZIP_INVALID", "package exceeds the size limit")
        if name.startswith("word/media/") and info.file_size > _MAX_MEDIA_BYTES:
            raise DocxIntegrityError("DOCX_MEDIA_INVALID", "media entry exceeds the size limit")
        names.add(name)
    if not _REQUIRED_PARTS <= names:
        raise DocxIntegrityError("DOCX_ZIP_INVALID", "required package parts are missing")
    return names


def _xml(package: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = package.getinfo(name)
        if info.file_size > _MAX_XML_BYTES:
            raise DocxIntegrityError("DOCX_XML_INVALID", "XML part exceeds the size limit")
        return ET.fromstring(package.read(info))
    except DocxIntegrityError:
        raise
    except (KeyError, OSError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise DocxIntegrityError("DOCX_XML_INVALID", "XML part is missing or malformed") from exc


def _relationship_owner(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path == PurePosixPath("_rels/.rels"):
        return PurePosixPath("")
    if len(path.parts) < 3 or path.parts[-2] != "_rels" or not path.name.endswith(".rels"):
        raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "relationship part path is invalid")
    return PurePosixPath(*path.parts[:-2], path.name[: -len(".rels")])


def _resolve_relationship_target(owner: PurePosixPath, target: str) -> str:
    if not target or target.startswith(("/", "\\")) or "\\" in target:
        raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "relationship target is unsafe")
    combined = owner.parent / PurePosixPath(target)
    normalized: list[str] = []
    for part in combined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not normalized:
                raise DocxIntegrityError(
                    "DOCX_RELATIONSHIPS_INVALID", "relationship target escapes the package"
                )
            normalized.pop()
        else:
            normalized.append(part)
    if not normalized:
        raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "relationship target is empty")
    return PurePosixPath(*normalized).as_posix()


def _external_relationship_target_is_allowed(
    relationship_type: str, target: str
) -> bool:
    if not target or any(ord(character) < 32 for character in target):
        return False
    parsed = urlsplit(target)
    if relationship_type == f"{_OFFICE_REL_NS}/hyperlink":
        if parsed.scheme.casefold() in {"http", "https"}:
            return bool(parsed.netloc)
        return parsed.scheme.casefold() == "mailto" and bool(parsed.path)
    if relationship_type == f"{_OFFICE_REL_NS}/attachedTemplate":
        return (
            parsed.scheme.casefold() == "file"
            and parsed.hostname in {None, "", "localhost"}
            and bool(parsed.path)
            and not parsed.query
            and not parsed.fragment
        )
    return False


def _relationship_state(
    package: zipfile.ZipFile,
    names: set[str],
    document: ET.Element,
) -> tuple[set[str], set[str]]:
    image_targets: set[str] = set()
    document_image_ids: dict[str, str] = {}
    root_office_document = False
    for rels_name in sorted(name for name in names if name.endswith(".rels")):
        owner = _relationship_owner(rels_name)
        root = _xml(package, rels_name)
        if root.tag != f"{{{_PACKAGE_REL_NS}}}Relationships":
            raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "relationship root is invalid")
        identifiers: set[str] = set()
        for row in root:
            if row.tag != f"{{{_PACKAGE_REL_NS}}}Relationship":
                raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "relationship entry is invalid")
            identifier = row.attrib.get("Id", "")
            relationship_type = row.attrib.get("Type", "")
            target = row.attrib.get("Target", "")
            if not identifier or identifier in identifiers:
                raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "relationship is unsupported")
            identifiers.add(identifier)
            if row.attrib.get("TargetMode") == "External":
                if (
                    relationship_type not in _ALLOWED_EXTERNAL_RELATIONSHIP_TYPES
                    or not _external_relationship_target_is_allowed(
                        relationship_type, target
                    )
                ):
                    raise DocxIntegrityError(
                        "DOCX_RELATIONSHIPS_INVALID", "external relationship is unsupported"
                    )
                continue
            resolved = _resolve_relationship_target(owner, target)
            if resolved not in names:
                raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "relationship target is missing")
            if rels_name == "_rels/.rels" and relationship_type.endswith("/officeDocument"):
                root_office_document = resolved == "word/document.xml"
            if relationship_type.endswith("/image"):
                if not resolved.startswith("word/media/"):
                    raise DocxIntegrityError(
                        "DOCX_RELATIONSHIPS_INVALID", "image relationship must target word/media"
                    )
                image_targets.add(resolved)
                if rels_name == "word/_rels/document.xml.rels":
                    document_image_ids[identifier] = resolved
    if not root_office_document:
        raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "office document relationship is missing")
    embedded_ids = {
        value
        for element in document.iter()
        for key in (f"{{{_DRAWING_REL_NS}}}embed", f"{{{_DRAWING_REL_NS}}}link")
        if isinstance((value := element.attrib.get(key)), str) and value
    }
    if embedded_ids != set(document_image_ids):
        raise DocxIntegrityError(
            "DOCX_RELATIONSHIPS_INVALID", "document image relationships are not exact"
        )
    media_names = {name for name in names if name.startswith("word/media/") and not name.endswith("/")}
    if media_names != image_targets:
        raise DocxIntegrityError("DOCX_RELATIONSHIPS_INVALID", "media entries are not exactly related")
    return image_targets, embedded_ids


def _document_text(document: ET.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in document.iter(f"{{{_WORD_NS}}}p"):
        text = "".join(
            node.text or "" for node in paragraph.iter(f"{{{_WORD_NS}}}t")
        )
        if text.strip():
            paragraphs.append(text)
    return _normalize_text(" ".join(paragraphs))


def _visible_markdown_chunks(markdown: str) -> list[str]:
    chunks: list[str] = []
    fenced: str | None = None
    for raw_line in markdown.splitlines():
        fence = _FENCE_RE.match(raw_line)
        if fence:
            marker = fence.group(1)[0]
            fenced = None if fenced == marker else marker
            continue
        if fenced is not None or not raw_line.strip() or _IMAGE_RE.match(raw_line):
            continue
        line = raw_line.strip()
        heading = _HEADING_RE.match(line)
        if heading:
            line = heading.group(1)
        listed = _LIST_RE.match(line)
        if listed:
            line = listed.group(1)
        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and all(re.fullmatch(r"[-: ]+", cell) for cell in cells):
                continue
            line = " ".join(cells)
        line = _CLAIM_MARKER_RE.sub("", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[*_`]+", "", line)
        line = _normalize_text(line)
        if line:
            chunks.append(line)
    return chunks


def _core_properties(package: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    name = "docProps/core.xml"
    if name not in names:
        raise DocxIntegrityError("DOCX_PROVENANCE_INVALID", "core properties are missing")
    try:
        root = _xml(package, name)
    except DocxIntegrityError as exc:
        raise DocxIntegrityError(
            "DOCX_PROVENANCE_INVALID", "core properties are malformed"
        ) from exc
    if root.tag != f"{{{_CORE_PROPERTIES_NS}}}coreProperties":
        raise DocxIntegrityError("DOCX_PROVENANCE_INVALID", "core properties root is invalid")

    def value(namespace: str, field: str) -> str:
        node = root.find(f"{{{namespace}}}{field}")
        return node.text.strip() if node is not None and isinstance(node.text, str) else ""

    return {
        "title": value(_DC_NS, "title"),
        "subject": value(_DC_NS, "subject"),
        "creator": value(_DC_NS, "creator"),
        "last_modified_by": value(_CORE_PROPERTIES_NS, "lastModifiedBy"),
        "keywords": value(_CORE_PROPERTIES_NS, "keywords"),
    }


def _package_state(path: Path, *, read_provenance: bool = False) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DocxIntegrityError("DOCX_ZIP_INVALID", "DOCX must be a regular file")
    try:
        with zipfile.ZipFile(path) as package:
            names = _safe_package_names(package)
            _xml(package, "[Content_Types].xml")
            document_bytes = package.read("word/document.xml")
            document = _xml(package, "word/document.xml")
            image_targets, _ = _relationship_state(package, names, document)
            media_sha256 = sorted(_sha256(package.read(name)) for name in image_targets)
            state = {
                "document_xml_sha256": _sha256(document_bytes),
                "document_text": _document_text(document),
                "media_sha256": media_sha256,
            }
            if read_provenance:
                state["provenance"] = _core_properties(package, names)
            return state
    except DocxIntegrityError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        raise DocxIntegrityError("DOCX_ZIP_INVALID", "DOCX package is unreadable") from exc


def _validated_hashes(values: Iterable[str]) -> list[str]:
    hashes = list(values)
    if any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in hashes):
        raise DocxIntegrityError("DOCX_MEDIA_INVALID", "expected media hashes are invalid")
    if len(hashes) != len(set(hashes)):
        raise DocxIntegrityError("DOCX_MEDIA_INVALID", "expected media hashes must be unique")
    return sorted(hashes)


def validate_docx_integrity(
    docx_path: Path,
    *,
    markdown: str,
    expected_media_sha256: Iterable[str],
    required_attributions: Iterable[str],
    workflow_digest: str,
    snapshot_workflow_digest: str,
    legacy_docx: Path | None = None,
    expected_project_id: str | None = None,
    expected_release_level: str | None = None,
) -> dict[str, Any]:
    """Validate package structure and bind its content to current release inputs."""
    if (
        not isinstance(markdown, str)
        or not _SHA256_RE.fullmatch(workflow_digest)
        or not _SHA256_RE.fullmatch(snapshot_workflow_digest)
    ):
        raise DocxIntegrityError("DOCX_INTEGRITY_INPUT_INVALID")
    if workflow_digest != snapshot_workflow_digest:
        raise DocxIntegrityError("RELEASE_WORKFLOW_STALE")
    provenance_required = expected_project_id is not None or expected_release_level is not None
    if provenance_required and (
        not isinstance(expected_project_id, str)
        or not expected_project_id.strip()
        or expected_project_id != expected_project_id.strip()
        or expected_release_level not in _RELEASE_LEVELS
    ):
        raise DocxIntegrityError("DOCX_INTEGRITY_INPUT_INVALID")
    expected_hashes = _validated_hashes(expected_media_sha256)
    attributions = list(required_attributions)
    if any(not isinstance(value, str) or not value.strip() for value in attributions):
        raise DocxIntegrityError("DOCX_ATTRIBUTION_MISSING")

    current = _package_state(Path(docx_path), read_provenance=provenance_required)
    if provenance_required:
        expected_provenance = {
            "title": f"{expected_project_id} - {expected_release_level}",
            "subject": f"review-writer project {expected_project_id}",
            "creator": "review-writer",
            "last_modified_by": "review-writer",
            "keywords": f"{expected_project_id}; {expected_release_level}; review-writer",
        }
        if current.get("provenance") != expected_provenance:
            raise DocxIntegrityError(
                "DOCX_PROVENANCE_INVALID",
                "core properties do not match the project release",
            )
    if not set(expected_hashes) <= set(current["media_sha256"]):
        raise DocxIntegrityError("DOCX_MEDIA_INVALID", "expected media is absent from the package")
    document_text = current["document_text"]
    if any(_normalize_text(value) not in document_text for value in attributions):
        raise DocxIntegrityError("DOCX_ATTRIBUTION_MISSING")
    roundtrip_text = _normalize_text(
        _CLAIM_MARKER_RE.sub("", re.sub(r"[*_`]+", "", document_text))
    )
    cursor = 0
    for chunk in _visible_markdown_chunks(markdown):
        position = roundtrip_text.find(chunk, cursor)
        if position < 0:
            raise DocxIntegrityError("DOCX_MARKDOWN_ROUNDTRIP_MISMATCH")
        cursor = position + len(chunk)

    document_xml_changed: bool | None = None
    media_changed: bool | None = None
    legacy_repackage_only = False
    if legacy_docx is not None:
        legacy = _package_state(Path(legacy_docx))
        document_xml_changed = (
            current["document_xml_sha256"] != legacy["document_xml_sha256"]
        )
        media_changed = current["media_sha256"] != legacy["media_sha256"]
        legacy_repackage_only = not document_xml_changed
        if legacy_repackage_only:
            raise DocxIntegrityError("LEGACY_REPACKAGE_ONLY")

    return {
        "schema_version": "docx-integrity.v1",
        "zip_valid": True,
        "relationships_valid": True,
        "document_xml_sha256": current["document_xml_sha256"],
        "media_sha256": current["media_sha256"],
        "markdown_roundtrip_match": True,
        "attribution_complete": True,
        "workflow_digest_match": True,
        "provenance_valid": True if provenance_required else None,
        "document_xml_changed": document_xml_changed,
        "media_changed": media_changed,
        "legacy_repackage_only": legacy_repackage_only,
    }
