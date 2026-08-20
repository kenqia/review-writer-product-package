"""Portable, per-study source truth bundles for legacy review projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator

from review_writer.acquisition.manifest_identity import normalize_doi
from review_writer.project.path_safety import (
    PathSafetyError,
    validate_relative_path,
    validate_source_file,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_TRUTH_ROOT = Path("01_evidence/source_truth")
SOURCE_TRUTH_SCHEMA = REPO_ROOT / "schemas/evidence/source_truth_bundle.v1.schema.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_SOURCE_ASSET_BYTES = 256 * 1024 * 1024
SOURCE_ASSET_SNAPSHOT_SEMAPHORE = threading.BoundedSemaphore(2)
SOURCE_ASSET_SNAPSHOT_ACQUIRE_TIMEOUT = 5.0


class SourceTruthError(ValueError):
    """A stable, researcher-safe source truth failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProjectSourceIndex:
    """One request's validated, whole-directory source lookup."""

    project: Path
    bundles_by_study: dict[str, dict[str, Any]]
    bindings_by_source_id: dict[str, list[tuple[str, dict[str, Any]]]]


@dataclass(frozen=True)
class SourceTruthAssetSnapshot:
    """Private immutable copy of one asset bound to its verified bundle identity."""

    path: Path
    filename: str
    project_id: str
    study_id: str
    source_id: str
    kind: str
    bundle_digest: str
    sha256: str
    size_bytes: int
    page_count: int
    project_instance_root: Path
    project_device: int
    project_inode: int


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceTruthError(code) from exc


def _read_object(project: Path, relative: str, code: str) -> dict[str, Any]:
    path = _safe_file(project, relative, code)
    payload = _read_json(path, code)
    if not isinstance(payload, dict):
        raise SourceTruthError(code)
    return payload


def _safe_file(project: Path, relative: str, code: str = "SOURCE_ASSET_INVALID") -> Path:
    try:
        return validate_source_file(project, relative)
    except (OSError, PathSafetyError) as exc:
        raise SourceTruthError(code) from exc


def _secure_source_fd(
    project: Path,
    relative: str,
    project_identity: tuple[int, int],
) -> int:
    """Open every relative component beneath a stable project directory handle."""

    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "supports_dir_fd")
        or os.open not in os.supports_dir_fd
    ):
        raise SourceTruthError("SOURCE_ASSET_SECURITY_UNAVAILABLE")
    try:
        canonical = validate_relative_path(relative)
    except PathSafetyError as exc:
        raise SourceTruthError("SOURCE_ASSET_INVALID") from exc
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_fds: list[int] = []
    source_fd: int | None = None
    try:
        root_fd = os.open(project, directory_flags)
        directory_fds.append(root_fd)
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or (root_stat.st_dev, root_stat.st_ino) != project_identity
        ):
            raise SourceTruthError("SOURCE_ASSET_INVALID")
        current_fd = root_fd
        parts = canonical.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            directory_fds.append(next_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                raise SourceTruthError("SOURCE_ASSET_INVALID")
            current_fd = next_fd
        source_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise SourceTruthError("SOURCE_ASSET_INVALID")
        result = source_fd
        source_fd = None
        return result
    except SourceTruthError:
        raise
    except OSError as exc:
        raise SourceTruthError("SOURCE_ASSET_INVALID") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _private_snapshot_root() -> Path:
    candidate = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
    try:
        root = candidate.resolve(strict=True)
        root_stat = root.stat()
    except OSError as exc:
        raise SourceTruthError("SOURCE_ASSET_SECURITY_UNAVAILABLE") from exc
    root_mode = stat.S_IMODE(root_stat.st_mode)
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or (root_mode & 0o022 and not (root_stat.st_mode & stat.S_ISVTX))
    ):
        raise SourceTruthError("SOURCE_ASSET_SECURITY_UNAVAILABLE")
    return root


def _require_private_snapshot_path(path: Path, mode: int, *, directory: bool) -> None:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise SourceTruthError("SOURCE_ASSET_SECURITY_UNAVAILABLE") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    owner_matches = not hasattr(os, "geteuid") or observed.st_uid == os.geteuid()
    if (
        not expected_type(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != mode
        or not owner_matches
    ):
        raise SourceTruthError("SOURCE_ASSET_SECURITY_UNAVAILABLE")


def _file_descriptor(project: Path, relative: str) -> dict[str, Any]:
    path = _safe_file(project, relative)
    return {
        "path": relative,
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _rows(payload: dict[str, Any], key: str, code: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise SourceTruthError(code)
    return value


def _study_candidates(project: Path) -> list[dict[str, Any]]:
    path = project / "00_discovery/candidate_pool.json"
    if not path.is_file():
        return []
    payload = _read_json(path, "CANDIDATE_POOL_INVALID")
    if not isinstance(payload, dict):
        raise SourceTruthError("CANDIDATE_POOL_INVALID")
    return _rows(payload, "candidates", "CANDIDATE_POOL_INVALID")


def _requested_doi(project: Path, study_id: str) -> str | None:
    matches = [
        row
        for row in _study_candidates(project)
        if row.get("candidate_id") == study_id or row.get("study_id") == study_id
    ]
    if len(matches) > 1:
        raise SourceTruthError("STUDY_ID_AMBIGUOUS")
    if matches:
        return normalize_doi(matches[0].get("doi"))
    manifest_path = project / "00_discovery/acquisition_manifest.json"
    if manifest_path.is_file():
        payload = _read_json(manifest_path, "ACQUISITION_MANIFEST_INVALID")
        if not isinstance(payload, dict):
            raise SourceTruthError("ACQUISITION_MANIFEST_INVALID")
        downloads = payload.get("downloads", [])
        if not isinstance(downloads, list) or not all(isinstance(row, dict) for row in downloads):
            raise SourceTruthError("ACQUISITION_MANIFEST_INVALID")
        rows = [row for row in downloads if row.get("study_id") == study_id]
        dois = {normalize_doi(row.get("doi")) for row in rows}
        dois.discard(None)
        if len(dois) > 1:
            raise SourceTruthError("STUDY_ID_AMBIGUOUS")
        if dois:
            return next(iter(dois))
    return None


def _receipt_study(project: Path, study_id: str) -> dict[str, Any]:
    receipt = _read_object(
        project,
        "00_sources/acquisition_final_receipt.json",
        "ACQUISITION_FINAL_RECEIPT_INVALID",
    )
    studies = _rows(receipt, "studies", "ACQUISITION_FINAL_RECEIPT_INVALID")
    matches = [row for row in studies if row.get("study_id") == study_id]
    if not matches:
        doi = _requested_doi(project, study_id)
        if doi is not None:
            matches = [row for row in studies if normalize_doi(row.get("doi")) == doi]
    if len(matches) != 1:
        raise SourceTruthError("STUDY_NOT_FOUND" if not matches else "STUDY_ID_AMBIGUOUS")
    return matches[0]


def _receipt_sources(study: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    sources: list[tuple[str, dict[str, Any]]] = []
    main = study.get("main_pdf")
    if isinstance(main, dict):
        sources.append(("MAIN", main))
    elif isinstance(study.get("target_path"), str):
        sources.append(
            (
                "MAIN",
                {
                    "path": study.get("target_path"),
                    "sha256": study.get("sha256"),
                    "size_bytes": study.get("size_bytes"),
                },
            )
        )
    supplement = study.get("si_pdf")
    if isinstance(supplement, dict):
        sources.append(("SI", supplement))
    if not sources or sources[0][0] != "MAIN":
        raise SourceTruthError("MAIN_SOURCE_MISSING")
    return sources


def _receipt_pdf(project: Path, descriptor: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    value = descriptor.get("path")
    expected = descriptor.get("sha256")
    if not isinstance(value, str) or not value or not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise SourceTruthError("ACQUISITION_SOURCE_INVALID")
    relative = f"00_sources/{value}"
    path = _safe_file(project, relative)
    observed = _sha256_file(path)
    if observed != expected:
        raise SourceTruthError("SOURCE_PDF_HASH_MISMATCH")
    return path, {"path": relative, "sha256": observed, "size_bytes": path.stat().st_size}


def _unique_mineru_row(project: Path, pdf_relative: str) -> dict[str, Any]:
    manifest = _read_object(
        project,
        "01_evidence/mineru/manifest.json",
        "MINERU_MANIFEST_INVALID",
    )
    rows = _rows(manifest, "completed", "MINERU_MANIFEST_INVALID")
    expected = pdf_relative.removeprefix("00_sources/")
    matches = [row for row in rows if row.get("relative_pdf_path") == expected]
    if len(matches) != 1:
        raise SourceTruthError("MINERU_BINDING_MISSING" if not matches else "MINERU_BINDING_AMBIGUOUS")
    row = matches[0]
    slug = row.get("slug")
    if row.get("state") != "done" or not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise SourceTruthError("MINERU_BINDING_INVALID")
    return row


def _unique_content_list(extracted: Path) -> Path:
    matches = sorted(
        path
        for path in extracted.glob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    )
    if len(matches) != 1:
        raise SourceTruthError("CONTENT_LIST_MISSING" if not matches else "CONTENT_LIST_AMBIGUOUS")
    return matches[0]


def _unique_content_list_v2(extracted: Path, *, page_count: int) -> Path:
    matches = sorted(extracted.glob("*_content_list_v2.json"))
    if len(matches) != 1:
        raise SourceTruthError(
            "CONTENT_LIST_V2_MISSING" if not matches else "CONTENT_LIST_V2_AMBIGUOUS"
        )
    path = matches[0]
    payload = _read_json(path, "CONTENT_LIST_V2_INVALID")
    if (
        not isinstance(payload, list)
        or len(payload) != page_count
        or not all(
            isinstance(page, list) and all(isinstance(row, dict) for row in page)
            for page in payload
        )
    ):
        raise SourceTruthError("CONTENT_LIST_V2_INVALID")
    return path


def _relative_descriptor(project: Path, path: Path) -> dict[str, Any]:
    try:
        relative = path.relative_to(project).as_posix()
    except ValueError as exc:
        raise SourceTruthError("SOURCE_ASSET_INVALID") from exc
    return _file_descriptor(project, relative)


def _image_summary(project: Path, extracted: Path) -> dict[str, Any]:
    image_root = extracted / "images"
    if not image_root.is_dir() or image_root.is_symlink():
        rows: list[dict[str, Any]] = []
    else:
        rows = []
        for path in sorted(image_root.rglob("*")):
            if path.is_symlink():
                raise SourceTruthError("SOURCE_ASSET_INVALID")
            if path.is_file():
                descriptor = _relative_descriptor(project, path)
                rows.append(descriptor)
    return {"count": len(rows), "digest": canonical_digest(rows)}


def _text_layer(project: Path, pdf_sha256: str) -> dict[str, Any]:
    manifest = _read_object(
        project,
        "01_evidence/text_layers/text_layers.manifest.json",
        "TEXT_LAYER_MANIFEST_INVALID",
    )
    rows = _rows(manifest, "sources", "TEXT_LAYER_MANIFEST_INVALID")
    matches = [row for row in rows if row.get("pdf_sha256") == pdf_sha256]
    if len(matches) != 1:
        raise SourceTruthError(
            "TEXT_LAYER_BINDING_MISSING" if not matches else "TEXT_LAYER_BINDING_AMBIGUOUS"
        )
    row = matches[0]
    source_id = row.get("source_id")
    page_count = row.get("page_count")
    if (
        not isinstance(source_id, str)
        or not source_id
        or not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 1
    ):
        raise SourceTruthError("TEXT_LAYER_BINDING_INVALID")
    layer_root = "01_evidence/text_layers"
    reading = _file_descriptor(project, f"{layer_root}/{row.get('reading_order_path')}")
    layout = _file_descriptor(project, f"{layer_root}/{row.get('layout_path')}")
    if reading["sha256"] != row.get("reading_order_sha256") or layout["sha256"] != row.get("layout_sha256"):
        raise SourceTruthError("TEXT_LAYER_HASH_MISMATCH")
    return {
        "source_id": source_id,
        "page_count": page_count,
        "reading_layer": reading,
        "layout_layer": layout,
    }


def _identity(project: Path, study_id: str, receipt_study: dict[str, Any]) -> dict[str, Any]:
    doi = normalize_doi(receipt_study.get("doi"))
    title: str | None = None
    audit_path = project / "00_sources/source_identity_audit.json"
    if audit_path.is_file():
        audit = _read_object(project, "00_sources/source_identity_audit.json", "SOURCE_IDENTITY_INVALID")
        rows = _rows(audit, "results", "SOURCE_IDENTITY_INVALID")
        matches = [
            row
            for row in rows
            if row.get("candidate_id") == study_id
            or (doi is not None and normalize_doi(row.get("doi")) == doi)
        ]
        if len(matches) == 1 and isinstance(matches[0].get("title"), str):
            title = matches[0]["title"].strip() or None
        elif len(matches) > 1:
            raise SourceTruthError("SOURCE_IDENTITY_AMBIGUOUS")
    return {"doi": doi, "title": title}


def _validate_bundle(bundle: dict[str, Any]) -> None:
    schema = _read_json(SOURCE_TRUTH_SCHEMA, "SOURCE_TRUTH_SCHEMA_INVALID")
    errors = sorted(Draft202012Validator(schema).iter_errors(bundle), key=lambda error: list(error.path))
    if errors:
        raise SourceTruthError("SOURCE_TRUTH_SCHEMA_INVALID")
    body = {key: value for key, value in bundle.items() if key != "bundle_digest"}
    if canonical_digest(body) != bundle.get("bundle_digest"):
        raise SourceTruthError("SOURCE_TRUTH_DIGEST_MISMATCH")


def build_source_truth_bundle(project: Path, study_id: str) -> dict[str, object]:
    project = project.resolve(strict=True)
    if not study_id or study_id in {".", ".."} or "/" in study_id or "\\" in study_id:
        raise SourceTruthError("STUDY_ID_INVALID")
    receipt_study = _receipt_study(project, study_id)
    sources: list[dict[str, Any]] = []
    warnings: set[str] = set()
    seen_source_ids: set[str] = set()
    for document_role, receipt_descriptor in _receipt_sources(receipt_study):
        _, pdf = _receipt_pdf(project, receipt_descriptor)
        mineru = _unique_mineru_row(project, pdf["path"])
        slug = mineru["slug"]
        canonical_markdown = _file_descriptor(
            project,
            f"01_evidence/mineru/markdown/{slug}.md",
        )
        parse_markdown_path = project / f"01_evidence/parses/markdown/{slug}.md"
        if parse_markdown_path.is_file():
            parse_markdown = _relative_descriptor(project, parse_markdown_path)
            if parse_markdown["sha256"] != canonical_markdown["sha256"]:
                warnings.add("duplicate_parse_drift")
        extracted = project / f"01_evidence/parses/extracted/{slug}"
        if not extracted.is_dir() or extracted.is_symlink():
            raise SourceTruthError("PARSE_SIDECAR_MISSING")
        full_markdown = _relative_descriptor(project, extracted / "full.md")
        if full_markdown["sha256"] != canonical_markdown["sha256"]:
            warnings.add("duplicate_parse_drift")
        content_list_path = _unique_content_list(extracted)
        content_payload = _read_json(content_list_path, "CONTENT_LIST_INVALID")
        if not isinstance(content_payload, list):
            raise SourceTruthError("CONTENT_LIST_INVALID")
        layout_path = extracted / "layout.json"
        layout_payload = _read_json(layout_path, "LAYOUT_INVALID")
        if not isinstance(layout_payload, (dict, list)):
            raise SourceTruthError("LAYOUT_INVALID")
        layer = _text_layer(project, pdf["sha256"])
        if layer["source_id"] in seen_source_ids:
            raise SourceTruthError("SOURCE_ID_AMBIGUOUS")
        seen_source_ids.add(layer["source_id"])
        content_list_v2_path = _unique_content_list_v2(
            extracted,
            page_count=layer["page_count"],
        )
        sources.append(
            {
                "source_id": layer["source_id"],
                "document_role": document_role,
                "source_type": "primary_study",
                "mineru_slug": slug,
                "pdf": pdf,
                "canonical_markdown": canonical_markdown,
                "content_list": _relative_descriptor(project, content_list_path),
                "content_list_v2": _relative_descriptor(project, content_list_v2_path),
                "layout": _relative_descriptor(project, layout_path),
                "reading_layer": layer["reading_layer"],
                "layout_layer": layer["layout_layer"],
                "page_count": layer["page_count"],
                "images": _image_summary(project, extracted),
            }
        )
    sources.sort(key=lambda row: (row["document_role"], row["source_id"]))
    body: dict[str, Any] = {
        "schema_version": "source-truth-bundle.v1",
        "project_id": project.name,
        "study_id": study_id,
        "study_identity": _identity(project, study_id, receipt_study),
        "sources": sources,
        "warnings": sorted(warnings),
    }
    bundle = {**body, "bundle_digest": canonical_digest(body)}
    _validate_bundle(bundle)
    return bundle


def _bundle_path(project: Path, study_id: str) -> Path:
    if not study_id or study_id in {".", ".."} or "/" in study_id or "\\" in study_id:
        raise SourceTruthError("STUDY_ID_INVALID")
    return project / SOURCE_TRUTH_ROOT / study_id / "bundle.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
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


def write_source_truth_bundle(project: Path, study_id: str) -> dict[str, object]:
    project = project.resolve(strict=True)
    bundle = build_source_truth_bundle(project, study_id)
    _atomic_json(_bundle_path(project, study_id), bundle)
    return bundle


def load_source_truth_bundle(project: Path, study_id: str) -> dict[str, object]:
    project = project.resolve(strict=True)
    payload = _read_json(_bundle_path(project, study_id), "SOURCE_TRUTH_MISSING")
    if not isinstance(payload, dict):
        raise SourceTruthError("SOURCE_TRUTH_SCHEMA_INVALID")
    _validate_bundle(payload)
    return payload


def _declared_study_ids(project: Path) -> list[str]:
    receipt = _read_object(
        project,
        "00_sources/acquisition_final_receipt.json",
        "ACQUISITION_FINAL_RECEIPT_INVALID",
    )
    studies = _rows(receipt, "studies", "ACQUISITION_FINAL_RECEIPT_INVALID")
    receipt_ids = [row.get("study_id") for row in studies]
    if receipt_ids:
        if any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\0" in value
            for value in receipt_ids
        ) or len(set(receipt_ids)) != len(receipt_ids):
            raise SourceTruthError("ACQUISITION_FINAL_RECEIPT_INVALID")
    identifiers = set(receipt_ids)
    if not identifiers:
        identifiers = {
            row.get("candidate_id") or row.get("study_id")
            for row in _study_candidates(project)
            if isinstance(row.get("candidate_id") or row.get("study_id"), str)
        }
    if not identifiers:
        raise SourceTruthError("STUDY_ID_MISSING")
    return sorted(identifiers)


def declared_study_ids(project: Path) -> list[str]:
    """Return the complete study set declared by the current acquisition record."""

    project = project.resolve(strict=True)
    return _declared_study_ids(project)


def acquisition_receipt_digest(project: Path) -> str:
    """Hash the exact safe acquisition receipt bytes for a commit boundary."""

    project = project.resolve(strict=True)
    path = _safe_file(
        project,
        "00_sources/acquisition_final_receipt.json",
        "ACQUISITION_FINAL_RECEIPT_INVALID",
    )
    try:
        return _sha256_file(path)
    except OSError as exc:
        raise SourceTruthError("ACQUISITION_FINAL_RECEIPT_INVALID") from exc


def source_tier_authority(project: Path) -> dict[str, str]:
    """Return the exact current candidate-to-study tier authority."""

    project = project.resolve(strict=True)
    declared_ids = _declared_study_ids(project)
    declared_set = set(declared_ids)
    rows = _study_candidates(project)
    if len(rows) != len(declared_ids):
        raise SourceTruthError("SOURCE_TIER_INVALID")

    tiers_by_candidate: dict[str, str] = {}
    tiers_by_study: dict[str, str] = {}
    for row in rows:
        candidate_id = row.get("candidate_id")
        study_id = row.get("study_id")
        tier = row.get("tier")
        if (
            not isinstance(candidate_id, str)
            or not isinstance(study_id, str)
            or candidate_id != study_id
            or candidate_id not in declared_set
            or tier not in {"core", "background"}
            or candidate_id in tiers_by_candidate
            or study_id in tiers_by_study
        ):
            raise SourceTruthError("SOURCE_TIER_INVALID")
        tiers_by_candidate[candidate_id] = tier
        tiers_by_study[study_id] = tier

    if (
        set(tiers_by_candidate) != declared_set
        or set(tiers_by_study) != declared_set
        or tiers_by_candidate != tiers_by_study
    ):
        raise SourceTruthError("SOURCE_TIER_INVALID")
    return {study_id: tiers_by_study[study_id] for study_id in declared_ids}


def study_source_tier(project: Path, study_id: str) -> str:
    """Return one tier from the exact current candidate-to-study authority."""

    if not isinstance(study_id, str):
        raise SourceTruthError("SOURCE_TIER_INVALID")
    tiers = source_tier_authority(project)
    tier = tiers.get(study_id)
    if tier is None:
        raise SourceTruthError("SOURCE_TIER_INVALID")
    return tier


def build_all_source_truth(project: Path) -> list[dict[str, object]]:
    project = project.resolve(strict=True)
    return [build_source_truth_bundle(project, study_id) for study_id in _declared_study_ids(project)]


def build_project_source_index(project: Path) -> ProjectSourceIndex:
    """Build a fresh validated source index without retaining cross-request state."""

    project = project.resolve(strict=True)
    bundles_by_study: dict[str, dict[str, Any]] = {}
    bindings_by_source_id: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    root = project / SOURCE_TRUTH_ROOT
    if root.is_dir() and not root.is_symlink():
        for study_dir in sorted(root.iterdir()):
            if not study_dir.is_dir() or study_dir.is_symlink():
                continue
            bundle = load_source_truth_bundle(project, study_dir.name)
            if (
                bundle.get("project_id") != project.name
                or bundle.get("study_id") != study_dir.name
            ):
                raise SourceTruthError("SOURCE_TRUTH_IDENTITY_MISMATCH")
            bundles_by_study[study_dir.name] = bundle
            for row in bundle.get("sources", []):
                if isinstance(row, dict) and isinstance(row.get("source_id"), str):
                    bindings_by_source_id.setdefault(row["source_id"], []).append(
                        (study_dir.name, row)
                    )
    return ProjectSourceIndex(
        project=project,
        bundles_by_study=bundles_by_study,
        bindings_by_source_id=bindings_by_source_id,
    )


def project_source_binding(
    project: Path,
    source_id: str,
    *,
    source_index: ProjectSourceIndex | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve one globally unique source using the Dashboard route boundary."""
    project = project.resolve(strict=True)
    if not isinstance(source_id, str) or not source_id:
        raise SourceTruthError("SOURCE_ID_NOT_FOUND")
    index = source_index or build_project_source_index(project)
    if index.project != project:
        raise SourceTruthError("SOURCE_TRUTH_IDENTITY_MISMATCH")
    matches = index.bindings_by_source_id.get(source_id, [])
    if len(matches) != 1:
        raise SourceTruthError("SOURCE_ID_NOT_FOUND")
    return matches[0]


def source_truth_asset(
    project: Path,
    study_id: str,
    source_id: str,
    kind: str,
    *,
    source_index: ProjectSourceIndex | None = None,
) -> Path:
    project = project.resolve(strict=True)
    if source_index is None:
        bundle = load_source_truth_bundle(project, study_id)
    else:
        if source_index.project != project:
            raise SourceTruthError("SOURCE_TRUTH_IDENTITY_MISMATCH")
        bundle = source_index.bundles_by_study.get(study_id)
        if bundle is None:
            raise SourceTruthError("SOURCE_TRUTH_MISSING")
    sources = [source for source in bundle["sources"] if source.get("source_id") == source_id]
    if len(sources) != 1:
        raise SourceTruthError("SOURCE_ID_NOT_FOUND")
    field = {"pdf": "pdf", "parsed-markdown": "canonical_markdown"}.get(kind)
    if field is None:
        raise SourceTruthError("SOURCE_ASSET_KIND_INVALID")
    descriptor = sources[0][field]
    path = _safe_file(project, descriptor["path"])
    if path.stat().st_size != descriptor["size_bytes"] or _sha256_file(path) != descriptor["sha256"]:
        raise SourceTruthError("SOURCE_ASSET_DRIFT")
    return path


@contextmanager
def _source_truth_asset_snapshot_locked(
    project: Path,
    study_id: str,
    source_id: str,
    kind: str,
    descriptor: dict[str, Any],
    bundle_digest: str,
    expected_sha256: str,
    expected_size: int,
    page_count: int,
    project_identity: tuple[int, int],
) -> Iterator[SourceTruthAssetSnapshot]:
    relative = descriptor.get("path")
    if not isinstance(relative, str):
        raise SourceTruthError("SOURCE_TRUTH_SCHEMA_INVALID")
    path = _safe_file(project, relative)
    if not path.is_relative_to(project):
        raise SourceTruthError("SOURCE_ASSET_INVALID")
    file_descriptor = _secure_source_fd(project, relative, project_identity)

    try:
        opened_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_size != expected_size:
            raise SourceTruthError("SOURCE_ASSET_DRIFT")
        with tempfile.TemporaryDirectory(
            prefix="review-writer-source-asset-",
            dir=_private_snapshot_root(),
        ) as temp_dir:
            os.chmod(temp_dir, 0o700)
            _require_private_snapshot_path(Path(temp_dir), 0o700, directory=True)
            snapshot_path = Path(temp_dir) / Path(relative).name
            digest = hashlib.sha256()
            observed_size = 0
            with os.fdopen(file_descriptor, "rb", closefd=False) as source_handle:
                snapshot_fd: int | None = None
                try:
                    snapshot_fd = os.open(
                        snapshot_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                    )
                    snapshot_handle = os.fdopen(snapshot_fd, "wb")
                    snapshot_fd = None
                    with snapshot_handle:
                        os.fchmod(snapshot_handle.fileno(), 0o600)
                        _require_private_snapshot_path(snapshot_path, 0o600, directory=False)
                        for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                            observed_size += len(chunk)
                            if observed_size > expected_size:
                                raise SourceTruthError("SOURCE_ASSET_DRIFT")
                            digest.update(chunk)
                            snapshot_handle.write(chunk)
                        snapshot_handle.flush()
                        os.fsync(snapshot_handle.fileno())
                finally:
                    if snapshot_fd is not None:
                        os.close(snapshot_fd)
            if observed_size != expected_size or digest.hexdigest() != expected_sha256:
                raise SourceTruthError("SOURCE_ASSET_DRIFT")
            yield SourceTruthAssetSnapshot(
                path=snapshot_path,
                filename=Path(relative).name,
                project_id=project.name,
                study_id=study_id,
                source_id=source_id,
                kind=kind,
                bundle_digest=bundle_digest,
                sha256=expected_sha256,
                size_bytes=expected_size,
                page_count=page_count,
                project_instance_root=project,
                project_device=project_identity[0],
                project_inode=project_identity[1],
            )
    finally:
        os.close(file_descriptor)


@contextmanager
def source_truth_asset_snapshot(
    project: Path,
    study_id: str,
    source_id: str,
    kind: str,
) -> Iterator[SourceTruthAssetSnapshot]:
    """Yield verified bytes while holding the bounded snapshot-use boundary."""

    project = project.resolve(strict=True)
    project_stat = project.stat()
    project_identity = (project_stat.st_dev, project_stat.st_ino)
    bundle = load_source_truth_bundle(project, study_id)
    if bundle.get("project_id") != project.name or bundle.get("study_id") != study_id:
        raise SourceTruthError("SOURCE_ASSET_DRIFT")
    sources = [source for source in bundle["sources"] if source.get("source_id") == source_id]
    if len(sources) != 1:
        raise SourceTruthError("SOURCE_ID_NOT_FOUND")
    field = {"pdf": "pdf", "parsed-markdown": "canonical_markdown"}.get(kind)
    if field is None:
        raise SourceTruthError("SOURCE_ASSET_KIND_INVALID")
    source = sources[0]
    descriptor = source[field]
    expected_size = descriptor.get("size_bytes")
    expected_sha256 = descriptor.get("sha256")
    page_count = source.get("page_count")
    bundle_digest = bundle.get("bundle_digest")
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or not isinstance(expected_sha256, str)
        or not SHA256_RE.fullmatch(expected_sha256)
        or not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 1
        or not isinstance(bundle_digest, str)
        or not SHA256_RE.fullmatch(bundle_digest)
    ):
        raise SourceTruthError("SOURCE_TRUTH_SCHEMA_INVALID")
    if expected_size > MAX_SOURCE_ASSET_BYTES:
        raise SourceTruthError("SOURCE_ASSET_SIZE_INVALID")
    acquired = SOURCE_ASSET_SNAPSHOT_SEMAPHORE.acquire(
        timeout=SOURCE_ASSET_SNAPSHOT_ACQUIRE_TIMEOUT
    )
    if not acquired:
        raise SourceTruthError("SOURCE_ASSET_BUSY")
    try:
        with _source_truth_asset_snapshot_locked(
            project,
            study_id,
            source_id,
            kind,
            descriptor,
            bundle_digest,
            expected_sha256,
            expected_size,
            page_count,
            project_identity,
        ) as snapshot:
            yield snapshot
    finally:
        SOURCE_ASSET_SNAPSHOT_SEMAPHORE.release()
